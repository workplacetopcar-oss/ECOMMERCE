"""Expedientes privados, versiones, evidencias y saldos por referencia."""
import hashlib
import json
import math
from datetime import date
from io import BytesIO
from pathlib import PurePath

from flask import Blueprint, abort, jsonify, request, send_file

from common import db, now, require, role, transaction

bp = Blueprint("cases", __name__)
CHANNELS = ("nacional", "internacional")
ROLES = (*CHANNELS, "calidad", "administrador")
ALLOCATION = ("replacement", "rejected", "credit", "other")
MAX_FILE = 30 * 1024 * 1024
SCHEMA = """
CREATE TABLE IF NOT EXISTS cases(
 id INTEGER PRIMARY KEY, channel TEXT NOT NULL, origin TEXT NOT NULL, client TEXT NOT NULL DEFAULT '',
 date TEXT NOT NULL DEFAULT '', description TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL DEFAULT 'producto',
 fields TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT 'borrador', cycle INTEGER NOT NULL DEFAULT 1,
 version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 submitted_at TEXT, closed_at TEXT
);
CREATE INDEX IF NOT EXISTS cases_bandeja ON cases(channel,status,updated_at DESC);
CREATE INDEX IF NOT EXISTS cases_actualizados ON cases(updated_at DESC);
CREATE INDEX IF NOT EXISTS cases_cliente ON cases(client);
CREATE TABLE IF NOT EXISTS case_lines(
 id INTEGER PRIMARY KEY, case_id INTEGER NOT NULL REFERENCES cases(id), code TEXT NOT NULL, description TEXT NOT NULL,
 presentation TEXT NOT NULL, quantity REAL NOT NULL, unit TEXT NOT NULL, reason TEXT NOT NULL, lot TEXT NOT NULL,
 fields TEXT NOT NULL, incidence TEXT NOT NULL, confirmed REAL NOT NULL DEFAULT 0, replacement REAL NOT NULL DEFAULT 0,
 rejected REAL NOT NULL DEFAULT 0, credit REAL NOT NULL DEFAULT 0, other REAL NOT NULL DEFAULT 0,
 dispatched REAL NOT NULL DEFAULT 0, received REAL NOT NULL DEFAULT 0, credit_done REAL NOT NULL DEFAULT 0,
 other_done REAL NOT NULL DEFAULT 0, report_cycle INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS case_lines_cases ON case_lines(case_id);
CREATE INDEX IF NOT EXISTS case_lines_code ON case_lines(code);
CREATE INDEX IF NOT EXISTS case_lines_lot ON case_lines(lot);
CREATE TABLE IF NOT EXISTS events(
 id INTEGER PRIMARY KEY, case_id INTEGER NOT NULL REFERENCES cases(id), actor TEXT NOT NULL, channel TEXT NOT NULL,
 created_at TEXT NOT NULL, version INTEGER NOT NULL, cycle INTEGER NOT NULL, action TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS case_events ON events(case_id,id);
CREATE INDEX IF NOT EXISTS case_event_types ON events(case_id,cycle,action,id);
CREATE TABLE IF NOT EXISTS reports(
 id INTEGER PRIMARY KEY,case_id INTEGER NOT NULL REFERENCES cases(id),cycle INTEGER NOT NULL,actor TEXT NOT NULL,
 created_at TEXT NOT NULL,status TEXT NOT NULL,report TEXT NOT NULL,summary TEXT NOT NULL, findings TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS case_reports ON reports(case_id,cycle,status);
CREATE TABLE IF NOT EXISTS case_files(
 id INTEGER PRIMARY KEY,case_id INTEGER NOT NULL REFERENCES cases(id),line_id INTEGER REFERENCES case_lines(id),
 actor TEXT NOT NULL,created_at TEXT NOT NULL,filename TEXT NOT NULL,size INTEGER NOT NULL, digest TEXT NOT NULL,
 content BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS case_file_metadata ON case_files(case_id);
CREATE TABLE IF NOT EXISTS case_file_policy(
 file_id INTEGER PRIMARY KEY REFERENCES case_files(id), kind TEXT NOT NULL CHECK(kind IN ('technical','evidence'))
);
CREATE TABLE IF NOT EXISTS case_report_files(
 report_id INTEGER PRIMARY KEY REFERENCES reports(id), file_id INTEGER NOT NULL REFERENCES case_files(id)
);
CREATE INDEX IF NOT EXISTS files_by_report ON case_report_files(file_id,report_id);
INSERT OR IGNORE INTO case_report_files(report_id,file_id)
 SELECT r.id,f.id FROM events e JOIN reports r ON r.case_id=e.case_id AND r.id=json_extract(e.payload,'$.report_id')
 JOIN case_files f ON f.case_id=e.case_id AND f.id=json_extract(e.payload,'$.file_id')
 WHERE e.action IN ('publish','report_draft') AND json_extract(e.payload,'$.file_id') IS NOT NULL;

CREATE TABLE IF NOT EXISTS case_requests(
 actor TEXT NOT NULL, key TEXT NOT NULL, fingerprint TEXT NOT NULL, case_id INTEGER NOT NULL REFERENCES cases(id),
 PRIMARY KEY(actor,key)
);
CREATE TABLE IF NOT EXISTS contact_sources(
 account TEXT NOT NULL, request_id TEXT NOT NULL,case_id INTEGER NOT NULL REFERENCES cases(id),PRIMARY KEY(account,request_id)
);
CREATE TABLE IF NOT EXISTS contact_versions(
 case_id INTEGER NOT NULL REFERENCES cases(id), digest TEXT NOT NULL, file_id INTEGER NOT NULL REFERENCES case_files(id),
 parsed TEXT NOT NULL, PRIMARY KEY(case_id,digest)
);
CREATE TABLE IF NOT EXISTS order_links(
 case_id INTEGER NOT NULL REFERENCES cases(id), line_id INTEGER NOT NULL REFERENCES case_lines(id),order_id INTEGER NOT NULL,
 quantity REAL NOT NULL,PRIMARY KEY(line_id,order_id)
);
CREATE INDEX IF NOT EXISTS case_order_links ON order_links(case_id,order_id);
CREATE TABLE IF NOT EXISTS case_order_snapshots(
 line_id INTEGER NOT NULL, order_id INTEGER NOT NULL, snapshot TEXT NOT NULL,
 PRIMARY KEY(line_id,order_id), FOREIGN KEY(line_id,order_id) REFERENCES order_links(line_id,order_id)
);
"""


def init_cases(conn):
    conn.executescript(SCHEMA)


def encode(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError):
        abort(400, "El registro contiene un valor inválido.")


def object_value(value):
    if not isinstance(value, dict):
        abort(400, "Se esperaba un objeto con los campos del formulario.")
    return value


def items(data, key):
    value = data.get(key, [])
    if not isinstance(value, list) or len(value) > 500:
        abort(400, "Comprueba la lista de registros.")
    return [object_value(item) for item in value]


def string(data, key, required=False):
    value = data.get(key) or ""
    if not isinstance(value, str):
        abort(400, f"Revisa el campo {key}.")
    value = value.strip()
    if required and not value:
        abort(400, f"Completa el campo {key}.")
    return value


def number(data, key, positive=False):
    value = data.get(key, 0)
    try:
        if isinstance(value, bool):
            raise ValueError
        n = float(value)
        if not math.isfinite(n) or not 0 <= n <= 10**10 or round(n, 3) != n:
            raise ValueError
    except (ValueError, TypeError, OverflowError):
        abort(400, f"Cantidad inválida en {key} (máximo tres decimales).")
    if positive and not n:
        abort(400, "La cantidad debe ser positiva.")
    return n


def day(data, key, required=True):
    value = string(data, key, required)
    if value:
        try:
            if date.fromisoformat(value).isoformat() != value:
                raise ValueError
        except ValueError:
            abort(400, f"Fecha inválida en {key}.")
    return value


def one(query, params=()):
    return db().execute(query, params).fetchone()


def rows(query, params=()):
    return [dict(r) for r in db().execute(query, params)]


def get_case(cid):
    c = one("SELECT * FROM cases WHERE id=?", (cid,))
    if not c or (role() in CHANNELS and role() != c["channel"]) or (role() == "calidad" and not c["submitted_at"]):
        abort(404, "Expediente no disponible.")
    return dict(c)


def line_for(cid, lid):
    if not isinstance(lid, (int, str)) or not str(lid).isdigit():
        abort(400, "Selecciona una línea del expediente.")
    line = one("SELECT * FROM case_lines WHERE case_id=? AND id=?", (cid, lid))
    if not line:
        abort(404, "Línea no disponible.")
    return dict(line)


def event(c, action, payload):
    db().execute("INSERT INTO events(case_id,actor,channel,created_at,version,cycle,action,payload) VALUES(?,?,?,?,?,?,?,?)",
                 (c["id"],role(),c["channel"],now(),c["version"],c["cycle"],action,encode(payload)))


def bump(c, **updates):
    # Column names originate exclusively in the handlers below.
    updates.update(updated_at=now(),version=c["version"]+1)
    db().execute("UPDATE cases SET "+",".join(f"{k}=?" for k in updates)+" WHERE id=?",(*updates.values(),c["id"]))
    c.update(updates)


def mutate(cid, data, handler):
    key = request.headers.get("Idempotency-Key", "").strip()
    if not key or len(key)>160:
        abort(400, "Falta una clave válida para confirmar la operación.")
    fingerprint = hashlib.sha256(encode([request.method,request.path,data]).encode()).hexdigest()
    with transaction(db()):
        c = get_case(cid) if cid else None
        seen = one("SELECT * FROM case_requests WHERE actor=? AND key=?", (role(),key))
        if seen:
            if seen["fingerprint"] != fingerprint:
                abort(409, "La clave ya confirmó una operación distinta.")
            cid = seen["case_id"]
        else:
            if c and (type(data.get("version")) is not int or data["version"] != c["version"]):
                abort(409, "El expediente cambió. Revisa la versión actual antes de guardar.")
            cid = handler(c)
            db().execute("INSERT INTO case_requests VALUES(?,?,?,?)", (role(),key,fingerprint,cid))
        result = detail(cid)
    return jsonify(result)


def case_values(data, previous=None):
    fields = dict(object_value(data.get("fields", {})))
    if "client_snapshot" in fields:
        from catalog import profile
        selected = object_value(fields["client_snapshot"]).get("id")
        previous_fields = json.loads(previous["fields"]) if previous else {}
        previous_snapshot = previous_fields.get("client_snapshot") or {}
        if selected and previous_snapshot.get("id") == selected:
            fields["client_snapshot"] = previous_snapshot
        else:
            snapshot = one("SELECT data FROM catalog_clients WHERE channel=? AND id=?", (role(), selected))
            if not snapshot:
                abort(404, "Ficha de cliente no disponible para este canal.")
            fields["client_snapshot"] = {**json.loads(snapshot["data"]), **profile(db(), role(), selected)}
    kind = string(data, "kind") or "producto"
    if kind not in ("producto","servicio"):
        abort(400, "Selecciona si la novedad corresponde a producto o servicio.")
    return dict(client=string(data,"client"),date=day(data,"date",False),description=string(data,"description"),
                kind=kind,fields=encode(fields))


def save_lines(cid, data):
    existing = {r["id"]:r for r in rows("SELECT id,code,fields FROM case_lines WHERE case_id=?", (cid,))}
    used = set()
    for item in items(data,"lines"):
        vals = {k:string(item,k) for k in ("code","description","presentation","unit","reason","lot","incidence")}
        extras = dict(object_value(item.get("fields", {})))
        lid = item.get("id")
        if lid is not None and (type(lid) is not int or lid < 1):
            abort(400, "Identificador de línea inválido.")
        if "product_snapshot" in extras:
            from catalog import product_snapshot
            code = object_value(extras["product_snapshot"]).get("code")
            if vals["code"] != code:
                abort(400, "Vuelve a seleccionar el SKU del catálogo o elimina su selección.")
            previous = json.loads(existing[lid]["fields"]).get("product_snapshot", {}) if lid in existing else {}
            snapshot = previous if previous.get("code") == code else product_snapshot(db(), code)
            if not snapshot:
                abort(400, "El SKU seleccionado ya no está en el catálogo. Puedes ingresarlo manualmente.")
            extras["product_snapshot"] = snapshot
        vals.update(quantity=number(item,"quantity"),fields=encode(extras))
        if lid:
            if lid not in existing or lid in used:
                abort(400, "Revisa las líneas editadas.")
            assigned=one("SELECT COALESCE(SUM(quantity),0) n FROM order_links WHERE line_id=?", (lid,))
            if vals["quantity"] < round(assigned["n"],3):
                abort(409, "La cantidad debe incluir las unidades ya vinculadas a pedidos.")
            db().execute("UPDATE case_lines SET "+",".join(f"{k}=?" for k in vals)+" WHERE id=?", (*vals.values(),lid))
            used.add(lid)
        else:
            db().execute("INSERT INTO case_lines(case_id,"+",".join(vals)+") VALUES(?"+",?"*len(vals)+")",(cid,*vals.values()))
    for lid in existing.keys()-used:
        if one("SELECT 1 FROM case_files WHERE line_id=? UNION ALL SELECT 1 FROM order_links WHERE line_id=?", (lid,lid)):
            abort(409, "Esta línea ya tiene archivos o pedidos vinculados.")
        db().execute("DELETE FROM case_lines WHERE id=?", (lid,))


def new_case(data, origin):
    vals = case_values(data)
    created = now()
    cur = db().execute("INSERT INTO cases(channel,origin,created_at,updated_at,"+",".join(vals)+") VALUES(?,?,?,?"+",?"*len(vals)+")",
                       (role(),origin,created,created,*vals.values()))
    cid = cur.lastrowid
    save_lines(cid, data)
    return get_case(cid)


def record_file(c, name, content, lid=None, kind="evidence"):
    filename = PurePath(str(name or "documento").replace("\\","/")).name
    filename = "".join(ch for ch in filename if ch.isprintable())[:200]
    if not filename or not content or len(content)>MAX_FILE:
        abort(400, "Adjunta un archivo válido de hasta 30 MiB.")
    digest = hashlib.sha256(content).hexdigest()
    fid=db().execute("""INSERT INTO case_files(case_id,line_id,actor,created_at,filename,size,digest,content)
        VALUES(?,?,?,?,?,?,?,?)""",(c["id"],lid,role(),now(),filename,len(content),digest,content)).lastrowid
    db().execute("INSERT INTO case_file_policy VALUES(?,?)",(fid,kind))
    return fid


def save_contact(parsed, original, filename, source_account):
    if role()!="nacional":
        abort(403)
    digest = hashlib.sha256(original).hexdigest()
    source_id = str(parsed.get("request_id") or "documento:"+digest)
    with transaction(db()):
        source = one("SELECT case_id FROM contact_sources WHERE account=? AND request_id=?", (source_account,source_id))
        if source:
            c = get_case(source["case_id"])
            if one("SELECT 1 FROM contact_versions WHERE case_id=? AND digest=?", (c["id"],digest)):
                return detail(c["id"])
            bump(c)
        else:
            c = new_case(parsed,"rsales_formulario")
            db().execute("INSERT INTO contact_sources VALUES(?,?,?)",(source_account,source_id,c["id"]))
        fid = record_file(c,filename,original)
        previous = one("SELECT parsed FROM contact_versions WHERE case_id=? ORDER BY file_id DESC LIMIT 1",(c["id"],))
        changes = {}
        if previous:
            before = json.loads(previous["parsed"])
            changes = {k:{"anterior":before.get(k),"actual":parsed.get(k)} for k in sorted(before.keys()|parsed.keys()) if before.get(k)!=parsed.get(k)}
        db().execute("INSERT INTO contact_versions VALUES(?,?,?,?)",(c["id"],digest,fid,encode(parsed)))
        event(c,"import_revision" if previous else "import",dict(file_id=fid,request_id=source_id,changes=changes))
        return detail(c["id"])


def labels(c, commercially_done=False):
    if c["status"] == "cerrado":
        return "Cerrado",""
    if commercially_done and c["status"] not in ("borrador","respondido"):
        return "Cliente atendido · Calidad pendiente","calidad"
    return {
        "borrador": ("Borrador",c["channel"]), "enviado": ("Sin acuse","calidad"),
        "analisis": ("En análisis","calidad"), "informacion": ("Falta información del área",c["channel"]),
        "parcial": ("Respuesta técnica parcial","calidad"), "respondido": ("Respuesta recibida",c["channel"]),
    }[c["status"]]


def balance(line):
    line["pending"]=round(line["quantity"]-sum(line[k] for k in ALLOCATION),3)
    return line


def commercial_complete(c, lines):
    if not lines:
        decision=one("SELECT id,payload FROM events WHERE case_id=? AND action='decision' AND cycle=? ORDER BY id DESC LIMIT 1",
                     (c["id"],c["cycle"]))
        if not decision:
            return False
        if json.loads(decision["payload"]).get("type")=="rejected":
            return True
        return bool(one("SELECT id FROM events WHERE case_id=? AND action='resolve' AND cycle=? AND id>? LIMIT 1",
                        (c["id"],c["cycle"],decision["id"])))
    return all(round(line["quantity"]-sum(line[k] for k in ALLOCATION),3)==0 and
               line["dispatched"]==line["replacement"] and line["credit_done"]==line["credit"] and
               line["other_done"]==line["other"] for line in lines)


def accessible_files(cid, fid=None):
    # Older quality uploads default to internal. A published version grants access to that exact file.
    kind="COALESCE(p.kind, CASE WHEN f.actor='calidad' THEN 'technical' ELSE 'evidence' END)"
    columns="f.id,f.line_id,f.actor,f.created_at,f.filename,f.size,"+kind+" AS kind"
    if fid is not None:
        columns+=",f.content"
    query="SELECT "+columns+" FROM case_files f LEFT JOIN case_file_policy p ON p.file_id=f.id WHERE f.case_id=?"
    args=[cid]
    if role() not in ("calidad","administrador"):
        query+=" AND ("+kind+"""='evidence' OR EXISTS (
          SELECT 1 FROM case_report_files rf JOIN reports r ON rf.report_id=r.id
          WHERE rf.file_id=f.id AND r.case_id=f.case_id AND r.status='published'))"""
    if fid is not None:
        query+=" AND f.id=?"
        args.append(fid)
    return rows(query+" ORDER BY f.id",args)


def detail(cid):
    c = get_case(cid)
    c["fields"] = json.loads(c["fields"])
    c["lines"] = [balance(r) for r in rows("SELECT * FROM case_lines WHERE case_id=? ORDER BY id",(cid,))]
    for line in c["lines"]:
        line["fields"] = json.loads(line["fields"])
    draft_filter = "" if role() in ("calidad","administrador") else " AND action != 'report_draft'"
    c["files"] = accessible_files(cid)
    allowed_file_ids = {f["id"] for f in c["files"]}
    c["events"] = rows("SELECT * FROM events WHERE case_id=?"+draft_filter+" ORDER BY id",(cid,))
    for e in c["events"]:
        e["payload"] = json.loads(e["payload"])
    c["events"] = [e for e in c["events"] if e["action"]!="file" or e["payload"].get("file_id") in allowed_file_ids]
    visibility = "" if role() in ("calidad","administrador") else " AND status='published'"
    c["reports"] = rows("SELECT r.*,rf.file_id FROM reports r LEFT JOIN case_report_files rf ON rf.report_id=r.id WHERE r.case_id=?"+visibility+" ORDER BY r.id",(cid,))
    for rpt in c["reports"]:
        rpt["findings"]=json.loads(rpt["findings"])
    c["order_links"]=rows("""SELECT k.*,s.snapshot FROM order_links k LEFT JOIN case_order_snapshots s
                             USING(line_id,order_id) WHERE case_id=? ORDER BY line_id,order_id""",(cid,))
    for link in c["order_links"]:
        link["snapshot"]=json.loads(link["snapshot"] or "{}")
        link["number"]=link["snapshot"].get("number")
        link["code"]=link["snapshot"].get("code")
    c["label"],c["next_actor"]=labels(c, commercial_complete(c,c["lines"]) and is_communicated(c))
    return c


def is_communicated(c):
    # A subsequent decision must be communicated again. Technical and operational work has separate milestones.
    row=one("""SELECT action FROM events WHERE case_id=? AND cycle=? AND action IN ('communicate','decision')
               ORDER BY id DESC LIMIT 1""",(c["id"],c["cycle"]))
    return bool(row and row["action"]=="communicate")


@bp.get("/api/cases")
@require(*ROLES)
def list_cases():
    where=[]
    args=[]
    if role() in CHANNELS:
        where.append("channel=?")
        args.append(role())
    elif role()=="calidad":
        where.append("submitted_at IS NOT NULL")
    for k in ("channel","origin","status"):
        if value:=request.args.get(k):
            if k=="status" and value=="por_verificar":
                where.append("""(submitted_at IS NOT NULL AND status!='cerrado' AND (
                    EXISTS (SELECT 1 FROM case_lines l WHERE l.case_id=cases.id AND
                    (l.replacement>l.dispatched OR l.credit>l.credit_done OR l.other>l.other_done))
                    OR EXISTS (SELECT 1 FROM events e WHERE e.case_id=cases.id AND e.cycle=cases.cycle
                    AND e.action='decision' AND json_extract(e.payload,'$.type') IN ('credit','other')
                    AND NOT EXISTS (SELECT 1 FROM events later WHERE later.case_id=e.case_id AND later.cycle=e.cycle
                    AND later.id>e.id AND later.action IN ('decision','resolve')))))""")
            else:
                where.append(f"{k}=?")
                args.append(value)
    if term:=request.args.get("q","").strip():
        match="%"+term[:220].replace("!","!!").replace("%","!%").replace("_","!_")+"%"
        where.append("""(client LIKE ? ESCAPE '!' OR CAST(id AS TEXT)=?
          OR EXISTS (SELECT 1 FROM case_lines l WHERE l.case_id=cases.id AND (l.code LIKE ? ESCAPE '!'
          OR l.description LIKE ? ESCAPE '!' OR l.lot LIKE ? ESCAPE '!')))""")
        args.extend([match,term.removeprefix("RC-").lstrip("0"),match,match,match])
        if one("SELECT 1 FROM sqlite_master WHERE name='rs_orders'"):
            where[-1]="("+where[-1]+""" OR EXISTS (SELECT 1 FROM order_links k JOIN rs_orders o ON o.id=k.order_id
              WHERE k.case_id=cases.id AND (o.code LIKE ? ESCAPE '!' OR o.number LIKE ? ESCAPE '!')))"""
            args.extend([match,match])
    where_clause=(" WHERE "+" AND ".join(where)) if where else ""
    total=one("SELECT COUNT(*) n FROM cases"+where_clause,args)["n"]
    limit=max(1,min(request.args.get("limit",50,type=int),200))
    page=max(1,request.args.get("page",1,type=int))
    sorting = {
        "age": "COALESCE(submitted_at,created_at) ASC,id",
        "channel": "channel,COALESCE(submitted_at,created_at) ASC,id",
        "next": """CASE WHEN status='cerrado' THEN 'zz' WHEN status IN ('enviado','analisis','parcial')
                 THEN 'calidad' ELSE channel END,COALESCE(submitted_at,created_at) ASC,id""",
        "updated": "updated_at DESC,id DESC",
    }
    order=sorting.get(request.args.get("sort") or ("age" if role()=="calidad" else "updated"))
    if not order:
        abort(400,"Criterio de ordenación desconocido.")
    result=rows("SELECT id,channel,origin,client,date,kind,status,version,cycle,created_at,updated_at,submitted_at,closed_at FROM cases"+
                where_clause+" ORDER BY "+order+" LIMIT ? OFFSET ?",(*args,limit,limit*(page-1)))
    for c in result:
        completed = c["status"] not in ("borrador","cerrado","respondido") and is_communicated(c) and commercial_complete(
            c, rows("SELECT * FROM case_lines WHERE case_id=?", (c["id"],)))
        c["label"],c["next_actor"]=labels(c, completed)
    response=jsonify(result)
    response.headers["X-Total-Count"]=str(total)
    return response


@bp.get("/api/cases/<int:cid>")
@require(*ROLES)
def read_case(cid):
    conn=db()
    conn.execute("BEGIN")
    try:
        return jsonify(detail(cid))
    finally:
        conn.rollback()


@bp.post("/api/cases")
@require(*CHANNELS)
def create_case():
    data=object_value(request.get_json())
    def create(_):
        c=new_case(data,role()+"_manual")
        event(c,"create",{})
        if data.get("submit") is True:
            submit(c)
        return c["id"]
    return mutate(None,data,create)


def corrects_source(old, new):
    """Adding optional metadata is normal review; changing a recorded answer needs a reason."""
    if isinstance(old, dict) and isinstance(new, dict):
        return any(corrects_source(value, new.get(key)) for key, value in old.items())
    if old is None or old == "" or old == [] or old == {}:
        return False
    return old != new


@bp.put("/api/cases/<int:cid>")
@require(*CHANNELS)
def edit_case(cid):
    data=object_value(request.get_json())
    def edit(c):
        if c["status"]!="borrador":
            abort(409,"Después del envío, registra una ampliación.")
        old=detail(cid)
        vals=case_values(data,c)
        save_lines(cid,data)
        after_lines=rows("SELECT * FROM case_lines WHERE case_id=? ORDER BY id",(cid,))
        if c["origin"]=="rsales_formulario":
            reason=string(data,"note")
            changes={k:{"anterior":c[k],"actual":v} for k,v in vals.items() if v!=c[k]}
            old_lines=[{k:v for k,v in l.items() if k!="pending"} for l in old["lines"]]
            for l in old_lines:
                l["fields"]=encode(l["fields"])
            if old_lines!=after_lines:
                changes["lines"]={"anterior":old_lines,"actual":after_lines}
            original_by_id={l["id"]:l for l in old_lines}
            revised_by_id={l["id"]:l for l in after_lines}
            corrected=original_by_id.keys()!=revised_by_id.keys()
            for lid in original_by_id.keys() & revised_by_id.keys():
                original={**original_by_id[lid],"fields":json.loads(original_by_id[lid]["fields"])}
                revised={**revised_by_id[lid],"fields":json.loads(revised_by_id[lid]["fields"])}
                corrected |= corrects_source(original,revised)
            for key in vals:
                original, revised = ((json.loads(c[key]),json.loads(vals[key])) if key=="fields" else (c[key],vals[key]))
                corrected |= corrects_source(original,revised)
            if corrected and not reason:
                abort(400,"Explica el motivo de la corrección del comprobante.")
        else:
            changes={}
            reason=string(data,"note")
        bump(c,**vals)
        event(c,"review" if c["origin"]=="rsales_formulario" else "edit",{"note":reason,"changes":changes})
        return cid
    return mutate(cid,data,edit)


def submit(c):
    if c["status"]!="borrador":
        abort(409,"El expediente ya fue enviado.")
    if not all(c[k] for k in ("client","date","description")):
        abort(400,"Completa el cliente, la fecha y el relato antes de enviar.")
    lines=rows("SELECT * FROM case_lines WHERE case_id=?",(c["id"],))
    if (not lines and c["kind"]=="producto") or any(not(l["code"] or l["description"]) or l["quantity"]<=0 or not l["reason"] or not l["unit"] for l in lines):
        abort(400,"Cada referencia requiere identificación, cantidad positiva, unidad y motivo.")
    if c["origin"]=="rsales_formulario" and not one("SELECT 1 FROM events WHERE case_id=? AND action='review'",(c["id"],)):
        abort(409,"Guarda primero la revisión del formulario RSales.")
    bump(c,status="enviado",submitted_at=now())
    event(c,"submit",{})


@bp.post("/api/cases/<int:cid>/files")
@require(*ROLES)
def add_file(cid):
    get_case(cid)
    f=request.files.get("file")
    if f is None:
        abort(400,"Selecciona un archivo.")
    blob=f.read(MAX_FILE+1)
    data={"version":request.form.get("version",type=int),"line_id":request.form.get("line_id"),"digest":hashlib.sha256(blob).hexdigest(),"filename":f.filename}
    kind=request.form.get("kind") or ("technical" if role()=="calidad" else "evidence")
    if kind not in ("technical","evidence") or (kind=="technical" and role()!="calidad"):
        abort(400,"Selecciona una clase de archivo válida para tu rol.")
    data["kind"]=kind
    def save(c):
        if c["status"]=="cerrado":
            abort(409,"Reabre el expediente antes de adjuntar.")
        lid=line_for(cid,data["line_id"])["id"] if data["line_id"] else None
        fid=record_file(c,f.filename,blob,lid,kind)
        bump(c)
        event(c,"file",dict(file_id=fid,line_id=lid,filename=f.filename))
        return cid
    return mutate(cid,data,save)


@bp.get("/api/cases/<int:cid>/files/<int:fid>")
@require(*ROLES)
def download(cid,fid):
    get_case(cid)
    records=accessible_files(cid,fid)
    if not records:
        abort(404)
    row=records[0]
    return send_file(BytesIO(row["content"]),download_name=row["filename"],as_attachment=True,mimetype="application/octet-stream",max_age=0)


def assert_actor(*roles):
    if role() not in roles:
        abort(403,"Tu acceso no permite esta acción.")


def stage(c, *states):
    if c["status"] not in states:
        abort(409,"El expediente cambió o esta acción aún no está disponible.")


def set_line(lid, **values):
    db().execute("UPDATE case_lines SET "+",".join(f"{key}=?" for key in values)+" WHERE id=?",(*values.values(),lid))


def handle_report(c,data,action):
    assert_actor("calidad")
    stage(c,"analisis","parcial","respondido","informacion")
    publish=action=="publish"
    findings=items(data,"findings")
    report=string(data,"report")
    summary=string(data,"summary",publish)
    if publish and not report and not data.get("file_id"):
        abort(400,"Redacta el informe o indica su archivo adjunto.")
    if data.get("file_id") and not one("""SELECT 1 FROM case_files f LEFT JOIN case_file_policy p ON p.file_id=f.id
                         WHERE f.case_id=? AND f.id=? AND f.actor='calidad' AND COALESCE(p.kind,'technical')='technical'""",
                         (c["id"],data["file_id"])):
        abort(400,"El informe adjunto debe pertenecer a Calidad y al expediente.")
    if publish and not findings and c["kind"]=="producto":
        abort(400,"Selecciona las líneas que cubre el informe.")
    covered=set()
    for f in findings:
        line=line_for(c["id"],f.get("line_id"))
        if line["id"] in covered:
            abort(400,"Una referencia está repetida en el informe.")
        covered.add(line["id"])
        f["line_id"]=line["id"]
        string(f,"finding",publish)
        string(f,"result",publish)
        f["cause"]=string(f,"cause") or "Pendiente"
        confirmed=number(f,"confirmed")
        if confirmed>line["quantity"]:
            abort(400,"La cantidad confirmada supera la reclamada.")
        if publish:
            if line["report_cycle"]==c["cycle"] and not string(data,"note"):
                abort(400,"Indica el motivo de la revisión del informe publicado.")
            set_line(line["id"],confirmed=confirmed,report_cycle=c["cycle"])
    if c["kind"]=="servicio" and c["status"]=="respondido" and publish:
        string(data,"note",True)
    status="published" if publish else "draft"
    rid=db().execute("""INSERT INTO reports(case_id,cycle,actor,created_at,status,report,summary,findings)
              VALUES(?,?,?,?,?,?,?,?)""",(c["id"],c["cycle"],role(),now(),status,report,summary,encode(findings))).lastrowid
    if data.get("file_id"):
        db().execute("INSERT INTO case_report_files VALUES(?,?)",(rid,data["file_id"]))
    new_state=c["status"]
    if publish:
        incomplete=one("SELECT 1 FROM case_lines WHERE case_id=? AND report_cycle!=? LIMIT 1",(c["id"],c["cycle"]))
        new_state="parcial" if incomplete else "respondido"
    bump(c,status=new_state)
    event(c,action,dict(report_id=rid,line_ids=sorted(covered),note=string(data,"note"),
                        destination=c["channel"],status=new_state,file_id=data.get("file_id")))


def service_without_lines(c):
    return c["kind"]=="servicio" and not one("SELECT id FROM case_lines WHERE case_id=? LIMIT 1",(c["id"],))


def handle_decision(c,data):
    if not data.get("line_id") and service_without_lines(c):
        kind=string(data,"type",True)
        if kind not in ("rejected","credit","other"):
            abort(400,"Selecciona rechazo, crédito u otra solución.")
        payload=dict(type=kind,authorized_by=string(data,"authorized_by",True),note=string(data,"note",True))
        bump(c)
        event(c,"decision",payload)
        return
    line=line_for(c["id"],data.get("line_id"))
    allocations={key:number(data,key) for key in ALLOCATION}
    if round(sum(allocations.values()),3)>line["quantity"]:
        abort(400,"La decisión supera la cantidad reclamada.")
    for key,used in (("replacement","dispatched"),("credit","credit_done"),("other","other_done")):
        if allocations[key]<line[used]:
            abort(409,"La decisión debe incluir el cumplimiento ya documentado.")
    note=string(data,"note",True)
    author=string(data,"authorized_by",True)
    set_line(line["id"],**allocations)
    bump(c)
    event(c,"decision",dict(line_id=line["id"],note=note,authorized_by=author,**allocations))


def handle_movement(c,data,action):
    if action=="resolve" and not data.get("line_id") and service_without_lines(c):
        decision=one("SELECT payload FROM events WHERE case_id=? AND action='decision' AND cycle=? ORDER BY id DESC LIMIT 1",
                     (c["id"],c["cycle"]))
        if not decision or commercial_complete(c,[]):
            abort(409,"Este caso no tiene un compromiso pendiente.")
        payload=dict(type=json.loads(decision["payload"])["type"],date=day(data,"date"),
                     support=string(data,"support",True),note=string(data,"note"))
        bump(c)
        event(c,action,payload)
        return
    line=line_for(c["id"],data.get("line_id"))
    qty=number(data,"quantity",True)
    movement={"quantity":qty,"line_id":line["id"],"date":day(data,"date"),"support":string(data,"support",True)}
    if action=="dispatch":
        balance_key,ceiling="dispatched","replacement"
    elif action=="receipt":
        balance_key,ceiling="received","dispatched"
    else:
        movement["type"]=string(data,"type",True)
        if movement["type"] not in ("credit","other"):
            abort(400,"Selecciona crédito u otra solución.")
        ceiling=movement["type"]
        balance_key=ceiling+"_done"
    updated=round(line[balance_key]+qty,3)
    if updated>line[ceiling]:
        abort(409,"El movimiento excede el saldo pendiente de la línea.")
    if data.get("file_id"):
        if not any(str(f["id"])==str(data["file_id"]) for f in accessible_files(c["id"])):
            abort(404,"Soporte no disponible.")
        movement["file_id"]=data["file_id"]
    set_line(line["id"],**{balance_key:updated})
    bump(c)
    event(c,action,movement)


def link_order(c,data):
    assert_actor("nacional","administrador")
    if c["channel"]!="nacional":
        abort(403)
    line=line_for(c["id"],data.get("line_id"))
    note=string(data,"note",True)
    if not one("SELECT 1 FROM sqlite_master WHERE name='rs_orders'"):
        abort(409,"No hay un corte de pedidos disponible.")
    order=one("SELECT * FROM rs_orders WHERE id=? AND channel='nacional' AND active=1 AND type='P'",(data.get("order_id"),))
    if not order:
        abort(404,"Pedido no disponible para este canal.")
    qty=number(data,"quantity",True)
    already=one("SELECT COALESCE(SUM(quantity),0) n FROM order_links WHERE line_id=? AND order_id!=?",(line["id"],order["id"]))
    if round(qty+already["n"],3)>line["quantity"]:
        abort(400,"Las cantidades vinculadas superan las reclamadas.")
    db().execute("""INSERT INTO order_links(case_id,line_id,order_id,quantity) VALUES(?,?,?,?)
                  ON CONFLICT(line_id,order_id) DO UPDATE SET quantity=excluded.quantity""",(c["id"],line["id"],order["id"],qty))
    snapshot=dict(order)
    snapshot["data"]=json.loads(snapshot["data"])
    db().execute("INSERT OR IGNORE INTO case_order_snapshots(line_id,order_id,snapshot) VALUES(?,?,?)",
                 (line["id"],order["id"],encode(snapshot)))
    bump(c)
    event(c,"link_order",dict(line_id=line["id"],order_id=order["id"],quantity=qty,note=note,
                              reference_status="Referencia del pedido por verificar"))


def report_by_id(c,rid):
    rpt=one("SELECT * FROM reports WHERE case_id=? AND id=? AND status='published'",(c["id"],rid))
    if not rpt:
        abort(404,"Versión publicada no disponible.")
    return rpt


def handle_action(c,data):
    action=string(data,"action",True)
    if action=="report_read":
        rpt=report_by_id(c,data.get("report_id"))
        if role()==c["channel"] and not one("""SELECT 1 FROM events WHERE case_id=? AND action='report_read'
                               AND json_extract(payload,'$.report_id')=? AND actor=?""",(c["id"],rpt["id"],role())):
            event(c,action,{"report_id":rpt["id"]})
        return
    if action=="reopen":
        assert_actor(c["channel"],"administrador")
        stage(c,"cerrado")
        note=string(data,"note",True)
        bump(c,status="enviado",cycle=c["cycle"]+1,closed_at=None)
        event(c,action,{"note":note})
        return
    if c["status"]=="cerrado":
        abort(409,"Reabre el expediente para registrar nuevas actuaciones.")
    if action=="submit":
        assert_actor(c["channel"])
        submit(c)
        return
    if action=="link_order":
        link_order(c,data)
        return
    if c["status"]=="borrador":
        abort(409,"Guarda y envía el expediente antes de esta actuación.")
    if action=="ack":
        assert_actor("calidad")
        stage(c,"enviado")
        bump(c,status="analisis")
        event(c,action,{})
    elif action=="request_info":
        assert_actor("calidad")
        stage(c,"analisis","parcial")
        lid=line_for(c["id"],data["line_id"])["id"] if data.get("line_id") else None
        note=string(data,"note",True)
        bump(c,status="informacion")
        event(c,action,dict(note=note,line_id=lid))
    elif action=="provide_info":
        assert_actor(c["channel"])
        stage(c,"informacion")
        note=string(data,"note",True)
        has_answer=one("SELECT 1 FROM reports WHERE case_id=? AND cycle=? AND status='published'",(c["id"],c["cycle"]))
        bump(c,status="parcial" if has_answer else "analisis")
        event(c,action,{"note":note})
    elif action in ("report_draft","publish"):
        handle_report(c,data,action)
    elif action=="decision":
        assert_actor(c["channel"],"administrador")
        handle_decision(c,data)
    elif action in ("dispatch","receipt","resolve"):
        assert_actor(c["channel"],"administrador")
        handle_movement(c,data,action)
    elif action=="communicate":
        assert_actor(c["channel"],"administrador")
        comm={k:string(data,k,True) for k in ("note","sender","medium")}
        comm["date"]=day(data,"date")
        if data.get("report_id"):
            comm["report_id"]=report_by_id(c,data["report_id"])["id"]
        bump(c)
        event(c,action,comm)
    elif action=="amend":
        assert_actor(c["channel"],"administrador")
        string(data,"note",True)
        if data.get("line_id"):
            line=line_for(c["id"],data["line_id"])
            if data.get("lot"):
                set_line(line["id"],lot=string(data,"lot",True))
        payload={k:v for k,v in data.items() if k not in ("action","version","difference")}
        if data.get("date"):
            day(data,"date")
        for key in ("quantity","good_box","good_without_box","transferred","unrecovered","declared_amount","calculated_amount"):
            if key in data and data[key] not in ("",None):
                payload[key]=number(data,key)
        money_fields=[key for key in ("declared_amount","calculated_amount") if isinstance(payload.get(key),(float,int))]
        if money_fields:
            string(data,"currency",True)
        if len(money_fields)==2:
            payload["difference"]=round(payload["declared_amount"]-payload["calculated_amount"],3)
        bump(c)
        event(c,action,payload)
    elif action=="close":
        assert_actor(c["channel"],"administrador")
        stage(c,"respondido")
        if not is_communicated(c):
            abort(409,"Registra la comunicación de la solución vigente.")
        if not commercial_complete(c,rows("SELECT * FROM case_lines WHERE case_id=?",(c["id"],))):
            abort(409,"Cada referencia necesita una decisión y el cumplimiento de sus compromisos.")
        bump(c,status="cerrado",closed_at=now())
        event(c,action,{"note":string(data,"note")})
    else:
        abort(400,"Acción desconocida.")


@bp.post("/api/cases/<int:cid>/actions")
@require(*ROLES)
def act(cid):
    data=object_value(request.get_json())
    def apply(c):
        handle_action(c,data)
        return cid
    return mutate(cid,data,apply)


@bp.get("/api/cases/<int:cid>/letter/<int:rid>")
@require(*ROLES)
def letter(cid,rid):
    with transaction(db()):
        c=get_case(cid)
        rpt=report_by_id(c,rid)
        # Only the customer-facing text approved in this immutable version belongs in the letter.
        content=[f"Lubristone · Respuesta al reclamo RC-{cid:06d}",c["client"],"",
                 f"Informe de Calidad {rid} · {rpt['created_at']}","",rpt["summary"]]
        document=("\n".join(content)+"\n").encode()
        event(c,"letter",{"report_id":rid,"sha256":hashlib.sha256(document).hexdigest()})
    return send_file(BytesIO(document),mimetype="text/plain; charset=utf-8",
                     download_name=f"Respuesta-RC-{cid:06d}-v{rid}.txt",as_attachment=True,max_age=0)
