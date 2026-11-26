"""D1 is the authority. SQLite serves as a rebuildable private read/transaction cache.

The same parameterized writes run locally and in a single revision-guarded D1
batch. Immutable R2 journals let a second process replay committed revisions.
The cache advances only after D1 accepts the batch. An ambiguous network result
is recovered on the next read, and case idempotency keys guard client retries.
"""
import gzip
import hashlib
import json
import re
import sqlite3
import uuid
from itertools import groupby

from werkzeug.exceptions import ServiceUnavailable

from blobs import gateway, read_object, save_object, storage_url

BULK_INSERT = re.compile(r"^(\s*INSERT(?:\s+OR\s+\w+)?\s+INTO\s+[\w]+(?:\s*\([\w\s,]+\))?)\s+VALUES\s*\(([^()]+)\)(.*)$", re.I | re.S)


def bindings(statements):
    """Compact consecutive inserts so a complete directory fits in one D1 batch."""
    result = []
    for sql, group in groupby(statements, key=lambda item: item["sql"]):
        rows = [item["params"] for item in group]
        match = BULK_INSERT.fullmatch(sql)
        if not match or len(rows) == 1:
            result.extend({"sql": sql, "params": row} for row in rows)
            continue
        fields = match[2].split(",")
        pos = 0
        columns = []
        for field in fields:
            if field.strip() == "?":
                columns.append(f"json_extract(packed.value,'$[{pos}]')")
                pos += 1
            else:
                columns.append(field)
        if pos != len(rows[0]) or "?" in match[3]:
            result.extend({"sql": sql, "params": row} for row in rows)
            continue
        query = match[1] + " SELECT " + ",".join(columns) + " FROM json_each(?) AS packed WHERE 1 " + match[3]
        parts = []
        size = 0
        for row in rows:
            part = json.dumps(row, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            if parts and size + len(part.encode()) > 96000:
                result.append({"sql": query, "params": ["[" + ",".join(parts) + "]"]})
                parts, size = [], 0
            parts.append(part)
            size += len(part.encode()) + 1
        if parts:
            result.append({"sql": query, "params": ["[" + ",".join(parts) + "]"]})
    return result


def split_schema(script):
    pending = ""
    for char in script:
        pending += char
        if char == ";" and sqlite3.complete_statement(pending):
            yield pending.strip().rstrip(";")
            pending = ""
    if pending.strip():
        raise ValueError("Sentencia de migración incompleta.")


class CloudConnection:
    def __init__(self, path, initialise):
        self.local = sqlite3.connect(path, isolation_level=None, timeout=60)
        self.local.row_factory = sqlite3.Row
        self.pending = []
        try:
            self.local.execute("PRAGMA foreign_keys=ON")
            self.local.execute("PRAGMA journal_mode=WAL")
            self.local.execute("BEGIN IMMEDIATE")
            marker = self.local.execute("SELECT name FROM sqlite_master WHERE name='_cloud_cache'").fetchone()
            if not marker and self.local.execute("SELECT 1 FROM sqlite_master WHERE type='table' LIMIT 1").fetchone():
                raise RuntimeError("D1 requiere una caché independiente. Conserva tu base local anterior.")
            self.local.execute("CREATE TABLE IF NOT EXISTS _cloud_cache(id INTEGER PRIMARY KEY, endpoint TEXT, revision INTEGER, schema_hash TEXT)")
            self.local.execute("INSERT OR IGNORE INTO _cloud_cache VALUES (1,?,0,'')", (storage_url(),))
            state = self.state()
            if state["endpoint"] != storage_url():
                raise RuntimeError("Este directorio de caché pertenece a otro gateway.")
            if state["schema_hash"] != initialise.digest:
                gateway("/schema", method="POST", data={"sql": initialise.statements})
                for sql in initialise.statements:
                    self.local.execute(sql)
                self.local.execute("UPDATE _cloud_cache SET schema_hash=?", (initialise.digest,))
            self.pull()
            self.local.commit()
        except BaseException:
            self.local.close()
            raise

    def state(self):
        return self.local.execute("SELECT * FROM _cloud_cache WHERE id=1").fetchone()

    def pull(self):
        rev = self.state()["revision"]
        while True:
            page = gateway("/snapshot", method="POST", data={"after": rev, "limit": 50})
            for entry in page["entries"]:
                if entry["revision"] != rev + 1:
                    raise ServiceUnavailable("Falta una revisión del registro compartido.")
                statements = json.loads(gzip.decompress(read_object(entry["changes"])))
                for stmt in statements:
                    self.local.execute(stmt["sql"], stmt["params"])
                rev += 1
            if not page["more"]:
                if rev != page["revision"]:
                    raise ServiceUnavailable("La caché y la base D1 tienen revisiones incompatibles.")
                break
        self.local.execute("UPDATE _cloud_cache SET revision=? WHERE id=1", (rev,))

    def begin(self):
        self.local.execute("BEGIN IMMEDIATE")
        try:
            self.pull()
        except BaseException:
            self.rollback()
            raise

    def execute(self, sql, params=()):
        verb = sql.lstrip().split(None, 1)[0].upper()
        if verb == "BEGIN":
            self.begin()
            return self.local.cursor()
        if verb in {"SELECT", "PRAGMA", "EXPLAIN"}:
            return self.local.execute(sql, params)
        if verb not in {"INSERT", "UPDATE", "DELETE", "REPLACE"}:
            raise ValueError("Sentencia fuera del contrato de almacenamiento: " + verb)
        if any(isinstance(p, (bytes, bytearray, memoryview)) for p in params):
            raise ValueError("El archivo debe guardarse en R2 antes de registrar su referencia.")
        params = tuple(int(p) if isinstance(p, bool) else p for p in params)
        implicit = not self.local.in_transaction
        if implicit:
            self.begin()
        try:
            cur = self.local.execute(sql, params)
            self.pending.append({"sql": sql, "params": list(params)})
            if implicit:
                self.commit()
            return cur
        except BaseException:
            if implicit:
                self.rollback()
            raise

    def executemany(self, sql, values):
        implicit = not self.local.in_transaction
        if implicit:
            self.begin()
        try:
            cur = None
            for value in values:
                cur = self.execute(sql, value)
            if implicit:
                self.commit()
            return cur
        except BaseException:
            if implicit:
                self.rollback()
            raise

    def executescript(self, script):
        # Module entry points repeat their own schema setup; the versioned
        # startup path has already applied these idempotent statements to D1.
        for sql in split_schema(script):
            if sql not in self.known_schema:
                raise RuntimeError("Registra la migración en el esquema compartido.")
        return self.local.cursor()

    def commit(self):
        try:
            if self.pending:
                base = self.state()["revision"]
                batch = bindings(self.pending)
                packed = json.dumps(batch, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
                digest = save_object(gzip.compress(packed, mtime=0))
                result = gateway("/commit", method="POST", data={"id": uuid.uuid4().hex, "base": base,
                                 "changes": digest, "statements": batch})
                if result["revision"] != base + 1:
                    raise ServiceUnavailable("La confirmación de D1 contiene una revisión inesperada.")
                self.local.execute("UPDATE _cloud_cache SET revision=? WHERE id=1", (base+1,))
            self.local.commit()
        except BaseException:
            self.local.rollback()
            raise
        finally:
            self.pending.clear()

    def rollback(self):
        self.pending.clear()
        self.local.rollback()

    def close(self):
        self.local.close()


class Schema:
    def __init__(self):
        self.statements = []

    def executescript(self, sql):
        self.statements.extend(split_schema(sql))

    @property
    def digest(self):
        return hashlib.sha256("\n".join(self.statements).encode()).hexdigest()
