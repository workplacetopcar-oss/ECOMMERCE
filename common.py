"""Request-local connections and authorization shared by the private module."""
import hashlib
import sqlite3
from pathlib import Path
from functools import lru_cache
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import wraps

from flask import abort, current_app, g, session


def now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def schema_setup(conn):
    import cases
    import catalog
    import history
    import metrics
    conn.executescript((Path(__file__).parent / "migrations" / "001_access.sql").read_text())
    for init in (cases.init_cases, catalog.init_catalog, history.init_history, metrics.init_metrics):
        init(conn)


@lru_cache(maxsize=1)
def shared_schema():
    from cloud_db import Schema
    schema = Schema()
    schema_setup(schema)
    return schema


def open_database(path):
    from blobs import storage_url
    if storage_url():
        from cloud_db import CloudConnection
        schema = shared_schema()
        # A D1 cache can never overwrite a local deployment's database.
        cache = Path(path).with_name(Path(path).name + ".d1-cache")
        conn = CloudConnection(cache, schema)
        conn.known_schema = set(schema.statements)
        return conn
    conn = sqlite3.connect(path, timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def db():
    if "db" not in g:
        g.db = open_database(current_app.config["DATABASE"])
    return g.db


@contextmanager
def transaction(conn):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def session_digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def role():
    if "identity" not in g:
        token = session.get("sid")
        g.identity = None if not token else db().execute(
            "SELECT a.name,a.person FROM accounts a JOIN sessions s ON s.account=a.name "
            "WHERE digest=? AND expires>?", (session_digest(token), int(time.time()))
        ).fetchone()
    return g.identity["name"] if g.identity else None


def require(*roles):
    def decorate(view):
        @wraps(view)
        def guarded(*args, **kwargs):
            identity = role()
            if not identity:
                abort(401)
            if roles and identity not in roles:
                abort(403)
            return view(*args, **kwargs)
        return guarded
    return decorate
