"""Private, atomic catalogue snapshots and channel-scoped directory lookups."""
import argparse
import hashlib
import json
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import time
import unicodedata
import uuid
from collections import Counter
from pathlib import Path

from flask import Blueprint, abort, jsonify, redirect, render_template, request, session, url_for
from werkzeug.utils import secure_filename

from common import db, now, require, role, transaction

bp = Blueprint("catalog", __name__)
PDF_LIMIT = 12 * 1024 * 1024
SOURCE = "rsales_lubristone"
PRODUCT_FIELDS = ("code", "description", "reference", "group_code", "family_code", "line_code", "tax", "state")
ALIASES = {
    "graphenox": ("grafeno", "graphenox"), "grafenox": ("grafeno", "graphenox"),
    "galon": ("galon", "gl"), "litro": ("litro", "lt"),
    "refrigerante": ("refriger", "refri"), "grafitada": ("grafit",),
    "lubristone": ("lubrist",),
}


def init_catalog(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS catalog_runs (
      id INTEGER PRIMARY KEY,account TEXT NOT NULL,started TEXT NOT NULL,finished TEXT,state TEXT NOT NULL,
      pages INTEGER,products INTEGER,clients INTEGER,error TEXT);
    CREATE TABLE IF NOT EXISTS catalog_snapshot (
      singleton INTEGER PRIMARY KEY CHECK (singleton=1), metadata TEXT NOT NULL, loaded_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS catalog_products (
      code TEXT PRIMARY KEY, active INTEGER NOT NULL, search TEXT NOT NULL, data TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS cp_active ON catalog_products(active, code);
    CREATE TABLE IF NOT EXISTS catalog_clients (
      channel TEXT NOT NULL, id TEXT NOT NULL, name TEXT NOT NULL, verified INTEGER NOT NULL,
      search TEXT NOT NULL, data TEXT NOT NULL, custom INTEGER NOT NULL DEFAULT 0,
      PRIMARY KEY(channel,id));
    CREATE INDEX IF NOT EXISTS cc_scope ON catalog_clients(channel,verified,name);
    CREATE TABLE IF NOT EXISTS catalog_profiles (
      channel TEXT NOT NULL, id TEXT NOT NULL, version INTEGER NOT NULL, actor TEXT NOT NULL,
      data TEXT NOT NULL, at TEXT NOT NULL, PRIMARY KEY(channel,id,version));
    CREATE TABLE IF NOT EXISTS catalog_cards (
      id TEXT PRIMARY KEY,name TEXT NOT NULL,search TEXT NOT NULL,data TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS catalog_codes (
      client_id TEXT NOT NULL,code TEXT NOT NULL, data TEXT NOT NULL,PRIMARY KEY(client_id,code));
    CREATE TABLE IF NOT EXISTS catalog_equivalences (
      channel TEXT NOT NULL,client_id TEXT NOT NULL,reported_code TEXT NOT NULL,
      presentation TEXT NOT NULL,version INTEGER NOT NULL,sku TEXT NOT NULL,reason TEXT NOT NULL,
      actor TEXT NOT NULL,at TEXT NOT NULL,PRIMARY KEY(channel,client_id,reported_code,presentation,version));
    CREATE TABLE IF NOT EXISTS contacto_previews (
      token TEXT PRIMARY KEY,owner TEXT NOT NULL,created INTEGER NOT NULL,filename TEXT NOT NULL,
      parsed TEXT NOT NULL,original BLOB NOT NULL);
    CREATE INDEX IF NOT EXISTS preview_age ON contacto_previews(created);
    """)


def norm(value):
    return "".join(c for c in unicodedata.normalize("NFKD", str(value or "").casefold())
                   if not unicodedata.combining(c))


def indexed(*values):
    return norm(" ".join(str(v or "") for v in values))


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def client_record(row, channel):
    if channel == "nacional":
        country = row.get("pais_confirmado")
        return dict(id=row["code"], name=row["name"], country=country or "", city=row.get("city"),
                    tax_id="", warning="" if country else "País por verificar", source="RSales",
                    raw=row)
    return dict(id=row["id"], name=row["razon_social"], country=row["pais"], city=row.get("ciudad"),
                tax_id=row["identificacion_fiscal"], source="Directorio internacional", raw=row,
                warning="Identificación por verificar" if row.get("identificacion_por_verificar") else "")


def client_values(record, channel):
    raw = record["raw"]
    search = indexed(record["id"], record["name"], record["city"], record["country"],
                     record["tax_id"], raw.get("alias"), raw.get("name_est"), raw.get("short_name"),
                     raw.get("country"), raw.get("region"))
    return channel, record["id"], record["name"], bool(record["country"]), search, encode(record)


def import_catalog(conn, private_dir):
    """Publish the whole new cut, preserving manual clients and profile revisions."""
    private_dir = Path(private_dir)
    def read(name):
        return json.loads((private_dir / (name + ".json")).read_text(encoding="utf-8"))
    products = read("productos_rsales")
    national = read("clientes_rsales")
    international = read("clientes_internacionales")
    cards = read("presentaciones_catalogo")
    codes = read("codigos_documentados_internacional")
    meta = read("control_de_fuentes")
    def check(ok, message):
        if not ok:
            raise ValueError(message)
    pm, cm, im = (meta[k] for k in ("productos", "clientes_rsales", "directorio_internacional"))
    check(len(products) == pm["codigos"] and len(national) == cm["codigos"]
          and len(international) == im["clientes"] and len(cards) == meta["catalogo_complementario"]["fichas"],
          "Corte incompleto: los totales no concuerdan con el control de fuentes.")
    check(Counter(p["state"] for p in products) == pm["estado"] and
          Counter(p["presentacion_etiquetada"] or "Sin etiqueta" for p in products) == pm["presentacion"],
          "Los estados o presentaciones del catálogo no concuerdan con el control.")
    check(sum(c.get("pais_confirmado") == "Colombia" for c in national) == cm["colombia_explicito"],
          "El directorio nacional difiere del control de países.")
    check(len({c["pais"] for c in international}) == im["paises"] and
          all(isinstance(c["identificacion_fiscal"], str) for c in international),
          "Revise los países y el formato textual de las identificaciones de exportación.")
    check(all(all(k in p for k in PRODUCT_FIELDS) for p in products), "Faltan campos de origen en el catálogo.")
    check(all(row["cliente_id"] in {c["id"] for c in international} for row in codes),
          "Código documentado sin cliente de exportación.")
    conn.execute("BEGIN IMMEDIATE")
    try:
        for table in ("catalog_products", "catalog_cards", "catalog_codes"):
            conn.execute("DELETE FROM " + table)
        conn.execute("DELETE FROM catalog_clients WHERE custom=0")
        conn.executemany("INSERT INTO catalog_products VALUES(?,?,?,?)", (
            (p["code"], p["state"] == "Y", indexed(*(p[k] for k in PRODUCT_FIELDS),
                                                     p["presentacion_etiquetada"]), encode(p)) for p in products))
        for channel, rows in (("nacional", national), ("internacional", international)):
            conn.executemany("INSERT INTO catalog_clients(channel,id,name,verified,search,data) VALUES(?,?,?,?,?,?)",
                             (client_values(client_record(r, channel), channel) for r in rows))
        conn.executemany("INSERT INTO catalog_cards VALUES(?,?,?,?)", (
            (c["id"], c["nombre"], indexed(c["nombre"], c["familia"], c["alias"], c["presentaciones_catalogo"]),
             encode(c)) for c in cards))
        conn.executemany("INSERT INTO catalog_codes VALUES(?,?,?)", (
            (c["cliente_id"], c["codigo"], encode(c)) for c in codes))
        conn.execute("INSERT OR REPLACE INTO catalog_snapshot VALUES(1,?,?)", (encode(meta), now()))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return meta


def product_snapshot(conn, code):
    row = conn.execute("SELECT data FROM catalog_products WHERE code=?", (code,)).fetchone()
    return json.loads(row[0]) if row else None


def channel_scope():
    current = role()
    channel = request.args.get("channel", current if current in ("nacional", "internacional") else "nacional")
    if channel not in ("nacional", "internacional") or (current != "administrador" and current != channel):
        abort(403)
    return channel


def search_rows(table, exact, filters, args, synonyms=False):
    page = max(1, request.args.get("page", 1, type=int))
    limit = max(1, min(100, request.args.get("page_size", 25, type=int)))
    query = norm(request.args.get("q", "").strip()[:180])
    for word in query.split()[:12]:
        terms = ALIASES.get(word, (word,)) if synonyms else (word,)
        filters.append("(" + " OR ".join("instr(search,?)>0" for _ in terms) + ")")
        args.extend(terms)
    where = " AND ".join(filters) or "1"
    conn = db()
    total = conn.execute(f"SELECT count(*) FROM {table} WHERE {where}", args).fetchone()[0]
    rows = conn.execute(f"SELECT data FROM {table} WHERE {where} ORDER BY ({exact}=? COLLATE NOCASE) DESC,"
                        f"(instr(lower({exact}),?)=1) DESC,{exact} COLLATE NOCASE LIMIT ? OFFSET ?",
                        [*args, query, query, limit, (page-1)*limit])
    return dict(items=[json.loads(r[0]) for r in rows], total=total, page=page, page_size=limit)


def profile(conn, channel, rid):
    row = conn.execute("SELECT data,version FROM catalog_profiles WHERE channel=? AND id=? ORDER BY version DESC LIMIT 1",
                       (channel, rid)).fetchone()
    return dict(json.loads(row[0]), profile_version=row[1]) if row else {"profile_version": 0}


@bp.get("/api/catalog/status")
@require("nacional", "internacional", "administrador", "calidad")
def status():
    row = db().execute("SELECT metadata,loaded_at FROM catalog_snapshot").fetchone()
    meta = json.loads(row[0]) if row else {}
    return jsonify(complete=bool(row), date=meta.get("fecha"),
                   products_at=meta.get("productos", {}).get("consulta_final_utc"),
                   clients_at=meta.get("clientes_rsales", {}).get("consulta_final_utc"),
                   loaded_at=row[1] if row else None, metadata=meta)


@bp.route("/api/catalog/refresh",methods=["GET","POST"])
@require("administrador")
def refresh_catalog():
    from sync_catalog import refresh_masters
    from sync_orders import load_adapter
    if request.method == "GET":
        runs = db().execute("SELECT * FROM catalog_runs ORDER BY id DESC LIMIT 30")
        return jsonify(items=[dict(row) for row in runs], configured=bool(os.getenv("RECLAMOS_RSALES_ADAPTER")))
    try:
        adapter = load_adapter()
    except (ValueError, ImportError, AttributeError):
        abort(503, description="El adaptador privado de RSales requiere configuración.")
    try:
        run = refresh_masters(db(), adapter)
    except Exception:
        abort(503, description="Lectura de maestros incompleta. El último corte sigue disponible.")
    return jsonify(id=run,status="completa")


@bp.route("/maestros",methods=["GET","POST"])
@require("administrador")
def masters_admin():
    conn = db()
    configured = bool(os.getenv("RECLAMOS_RSALES_ADAPTER"))
    if request.method == "POST":
        from sync_catalog import refresh_masters
        from sync_orders import load_adapter
        try:
            refresh_masters(conn, load_adapter())
            state = "completa"
        except Exception:
            state = "parcial"
        return redirect(url_for("catalog.masters_admin",resultado=state), code=303)
    row = conn.execute("SELECT metadata,loaded_at FROM catalog_snapshot").fetchone()
    runs = conn.execute("SELECT * FROM catalog_runs ORDER BY id DESC LIMIT 30").fetchall()
    return render_template("maestros.html",meta=json.loads(row[0]) if row else {},
                           loaded=row[1] if row else None,runs=runs,configured=configured,
                           result=request.args.get("resultado"))


@bp.get("/api/catalog/products")
@require("nacional", "internacional", "administrador", "calidad")
def products():
    filters = [] if request.args.get("inactive") == "1" else ["active=1"]
    return jsonify(search_rows("catalog_products", "code", filters, [], True))


@bp.get("/api/catalog/cards")
@require("nacional", "internacional", "administrador", "calidad")
def cards():
    return jsonify(search_rows("catalog_cards", "name", [], []))


@bp.get("/api/catalog/clients")
@require("nacional", "internacional", "administrador")
def clients():
    channel = channel_scope()
    filters = ["channel=?"]
    if channel == "nacional" and request.args.get("unverified") != "1":
        filters.append("""(verified=1 OR (SELECT json_extract(p.data,'$.country') FROM catalog_profiles p
                        WHERE p.channel=catalog_clients.channel AND p.id=catalog_clients.id
                        ORDER BY p.version DESC LIMIT 1)='Colombia')""")
    result = search_rows("catalog_clients", "id", filters, [channel])
    for c in result["items"]:
        c.update(profile(db(), channel, c["id"]))
    return jsonify(result)


@bp.get("/api/catalog/codes")
@require("internacional", "administrador")
def codes():
    items = [json.loads(r[0]) for r in db().execute(
        "SELECT data FROM catalog_codes WHERE client_id=? ORDER BY code", (request.args.get("client_id", ""),))]
    return jsonify(items=items, total=len(items), page=1, page_size=len(items))


def text_fields(data, fields):
    result = {}
    for name in fields:
        value = data.get(name, "")
        if not isinstance(value, str) or len(value) > 500:
            abort(400, description="Revisá el texto del campo " + name)
        result[name] = value.strip()
    return result


@bp.post("/api/catalog/clients")
@require("nacional", "internacional")
def save_client():
    channel = role()
    form = request.get_json()
    if not isinstance(form, dict):
        abort(400)
    if form.get("id"):
        abort(409, description="La ficha ya existe. Guardá los datos de contacto en su perfil.")
    record = text_fields(form, ("name","country","tax_id","city","contact","phone","email","address","source_note"))
    if not record["name"]:
        abort(400, description="Ingresá la razón social.")
    if channel == "nacional" and record["country"] and norm(record["country"]) != "colombia":
        abort(400, description="El directorio nacional admite Colombia o país por verificar.")
    record.update(id="MAN-" + uuid.uuid4().hex, source="Alta manual", raw={}, warning="" if record["country"] else "País por verificar")
    db().execute("INSERT INTO catalog_clients(channel,id,name,verified,search,data,custom) VALUES(?,?,?,?,?,?,1)",
                 client_values(record, channel))
    return jsonify(record), 201


@bp.put("/api/catalog/clients/<rid>/profile")
@require("nacional", "internacional")
def save_profile(rid):
    conn, channel = db(), role()
    form = request.get_json()
    if not isinstance(form, dict):
        abort(400)
    updates = text_fields(form, ("contact","phone","email","address","city","source_note"))
    if channel == "nacional" and "country" in form:
        country = text_fields(form, ("country",))["country"]
        if norm(country) not in ("", "colombia"):
            abort(400, description="Verificá si este cliente corresponde al canal nacional.")
        if country:
            updates.update(country="Colombia", warning="")
    if not updates["source_note"]:
        abort(400, description="Indicá la fuente comprobada de los datos de contacto.")
    conn.execute("BEGIN IMMEDIATE")
    try:
        exists = conn.execute("SELECT 1 FROM catalog_clients WHERE channel=? AND id=?", (channel, rid)).fetchone()
        if not exists:
            abort(404)
        previous = profile(conn, channel, rid)
        version = previous["profile_version"]
        if form.get("profile_version") != version:
            abort(409, description="El contacto cambió. Volvé a consultar el directorio.")
        if "country" in previous and "country" not in updates:
            updates.update(country=previous["country"], warning=previous.get("warning",""))
        conn.execute("INSERT INTO catalog_profiles VALUES(?,?,?,?,?,?)",
                     (channel, rid, version+1, role(), encode(updates), now()))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return jsonify(dict(updates, profile_version=version+1))


@bp.route("/api/catalog/equivalences",methods=["GET","POST"])
@require("nacional", "internacional", "administrador")
def equivalences():
    channel = channel_scope()
    if request.method == "GET":
        client = request.args.get("client_id", "")
        rows = db().execute("SELECT * FROM catalog_equivalences WHERE channel=? AND client_id=? "
                            "ORDER BY reported_code,presentation,version DESC", (channel, client))
        return jsonify(items=[dict(r) for r in rows])
    data = request.get_json()
    if not isinstance(data, dict):
        abort(400)
    values = text_fields(data, ("client_id","reported_code","presentation","sku","reason"))
    if not all(values.values()) or not product_snapshot(db(), values["sku"]):
        abort(400, description="Seleccioná cliente, producto y presentación, e indicá el respaldo del vínculo.")
    key = (channel, values["client_id"], values["reported_code"], values["presentation"])
    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    try:
        if not conn.execute("SELECT 1 FROM catalog_clients WHERE channel=? AND id=?", key[:2]).fetchone():
            abort(404)
        version = conn.execute("SELECT coalesce(max(version),0) FROM catalog_equivalences "
                               "WHERE channel=? AND client_id=? AND reported_code=? AND presentation=?", key).fetchone()[0]
        if data.get("version", 0) != version:
            abort(409)
        conn.execute("INSERT INTO catalog_equivalences VALUES(?,?,?,?,?,?,?,?,?)",
                     (*key, version+1, values["sku"], values["reason"], role(), now()))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return jsonify(version=version+1), 201


def read_contact(raw):
    if not raw or not raw[:1024].lstrip().startswith(b"%PDF-"):
        abort(400, description="Elegí un archivo PDF.")
    if len(raw) > PDF_LIMIT:
        abort(413, description="El máximo para un comprobante PDF es 12 MiB.")
    warning = ""
    try:
        result = subprocess.run([sys.executable, str(Path(__file__).with_name("contacto.py"))], input=raw,
                                timeout=12, check=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        parsed = json.loads(result.stdout)
        if parsed.get("revision_manual"):
            warning = "El formato requiere revisión manual. Podés conservar el PDF original y completar el borrador."
    except (subprocess.SubprocessError, ValueError):
        parsed = dict(id_solicitud="", lineas=[], revision_manual=True)
        warning = "El PDF requiere revisión manual. Podés conservar el original y completar el borrador."
    return parsed, warning


def preview_owner():
    return hashlib.sha256(session["sid"].encode()).hexdigest()


def proposed_client(conn, parsed):
    name = norm(parsed.get("cliente", "")).strip()
    if not name:
        return []
    # Expose candidates, including source/confirmed addresses, without making an automatic link.
    words = name.split()[:2]
    where = " AND ".join("instr(search,?)>0" for _ in words)
    return [json.loads(row[0]) for row in conn.execute(
        f"SELECT data FROM catalog_clients WHERE channel='nacional' AND {where} "
        "ORDER BY (lower(name)=?) DESC, name LIMIT 5", (*words,name))]


def contact_key(parsed, original):
    return parsed.get("id_solicitud") or "documento:" + hashlib.sha256(original).hexdigest()


@bp.post("/api/contacto/preview")
@require("nacional")
def preview_contact():
    file = request.files.get("file")
    if not file:
        abort(400, description="Elegí un comprobante PDF.")
    raw = file.read(PDF_LIMIT+1)
    parsed, warning = read_contact(raw)
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    conn = db()
    stamp = int(time.time())
    owner = preview_owner()
    with transaction(conn):
        conn.execute("DELETE FROM contacto_previews WHERE created<?", (stamp-1800,))
        # Keep recent previews from the same session even when two tabs are open.
        conn.execute("DELETE FROM contacto_previews WHERE owner=? AND token NOT IN "
                     "(SELECT token FROM contacto_previews WHERE owner=? ORDER BY created DESC, rowid DESC LIMIT 3)",
                     (owner, owner))
        conn.execute("INSERT INTO contacto_previews VALUES(?,?,?,?,?,?)",
                     (token_hash,owner,stamp,secure_filename(file.filename or "") or "comprobante.pdf",
                      encode(parsed),raw))
    existing = conn.execute("SELECT case_id FROM contact_sources WHERE account=? AND request_id=?",
                            (SOURCE, contact_key(parsed,raw))).fetchone()
    return jsonify(parsed=parsed, warning=warning, token=token, existing_case=existing[0] if existing else None,
                   client_candidates=proposed_client(conn, parsed))


def case_from_contact(parsed):
    fields = dict(contacto=parsed)
    for source, target in (("vendedor","seller"),("ciudad","city"),("contacto","contact"),
                           ("telefono","phone"),("direccion","address"),("correo","email")):
        fields[target] = parsed.get(source, "")
    lines = []
    for row in parsed["lineas"]:
        qty = str(row.get("cantidad", "")).strip()
        # Invalid source quantities stay in the evidence and await the operator's review.
        qty = int(qty) if re.fullmatch(r"[0-9]{1,8}", qty) else 0
        lines.append(dict(code="",description=row["descripcion"],quantity=qty,
                          unit="unidad",reason=row["motivo"],lot="",presentation="",
                          fields=dict(reported_code=row["codigo_origen"],source_quantity=row["cantidad"],
                                      fotos=row["fotos"])))
    return dict(request_id=parsed.get("id_solicitud"), client=parsed.get("cliente",""),
                date=parsed.get("fecha",""), description="; ".join(dict.fromkeys(
                    line["reason"] for line in lines if line["reason"])), fields=fields, lines=lines)


@bp.post("/api/contacto/import")
@require("nacional")
def import_contact():
    from cases import save_contact
    form = request.get_json()
    if not isinstance(form,dict) or not isinstance(form.get("token"),str):
        abort(400)
    token_hash = hashlib.sha256(form["token"].encode()).hexdigest()
    conn = db()
    row = conn.execute("SELECT original,parsed,filename FROM contacto_previews WHERE token=? AND owner=? "
                       "AND created>=?", (token_hash,preview_owner(),int(time.time())-1800)).fetchone()
    if not row:
        abort(404, description="La vista previa venció. Volvé a seleccionar el archivo.")
    raw, parsed, filename = row[0], json.loads(row[1]), row[2]
    case = save_contact(case_from_contact(parsed), raw, filename, SOURCE)
    return jsonify(id=case["id"],version=case["version"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Carga atómica del corte privado de clientes y productos")
    parser.add_argument("--db",required=True)
    parser.add_argument("--data",required=True,help="Directorio privado con los seis maestros JSON")
    args = parser.parse_args()
    os.umask(0o077)
    conn = sqlite3.connect(args.db, isolation_level=None)
    try:
        init_catalog(conn)
        result = import_catalog(conn,args.data)
        print(f"Corte {result['fecha']} publicado. Productos {result['productos']['codigos']}. "
              f"Clientes nacionales {result['clientes_rsales']['codigos']}. "
              f"Clientes internacionales {result['directorio_internacional']['clientes']}.")
    finally:
        conn.close()
