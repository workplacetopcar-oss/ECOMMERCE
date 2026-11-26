"""Importación privada de datos históricos declarados. El original y sus decisiones se conservan."""
from collections import Counter, defaultdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import hashlib
from io import BytesIO
import json
import posixpath
from pathlib import Path
import re
import unicodedata
from zipfile import ZipFile, BadZipFile
from xml.etree.ElementTree import ParseError
from defusedxml.ElementTree import iterparse, parse as safe_parse
from defusedxml.common import DefusedXmlException

from flask import Blueprint, abort, redirect, render_template, request, send_file, url_for
from openpyxl import load_workbook
from openpyxl.utils.datetime import from_excel
from openpyxl.utils.cell import range_boundaries
from openpyxl.utils.exceptions import InvalidFileException

from common import db, now, require, role

bp = Blueprint("history", __name__)
LIMIT = 12 * 1024 * 1024

# Alias de distintas hojas; el mapeo manual permite adaptar un encabezado nuevo.
HEADERS = {
    "number": "número de caso|caso|n reclamacion|numero de reclamacion|no caso|n de reclamo|n reclamo|n pqr|consecutivo|numero|reclamo|nro|no|nº",
    "client": "cliente|nombre del cliente|empresa|razon social|distribuidor",
    "date": "fecha|fecha de reclamo|fecha de reclamacion|fecha de queja|fecha ingreso|fecha recepcion|fecha de registro",
    "code": "codigo|codigo de producto|codigo producto|codigo historico|sku|referencia",
    "description": "producto|descripcion|nombre del producto|descripcion de producto|referencia afectada",
    "quantity": "cantidad|cantidad reclamada|unidades reclamadas|cantidad afectada|unidades|cantidad producto",
    "unit": "unidad de medida|medida|presentacion|unidad",
    "approved": "cantidad a reponer|unidades a reponer|cantidad aprobada|cantidad reposicion|unidades para reposicion",
    "decision": "decision|reponer|solucion|respuesta|decision comercial|accion|reposición",
    "dispatch": "fecha despacho|fecha de despacho|fecha despacho reposicion|fecha de envio de reposicion",
    "support": "soporte|numero de soporte|soporte despacho|soporte de despacho|documento soporte|soporte de reposicion",
    "report": "fecha de informe|fecha informe|fecha informe calidad",
    "notification": "fecha de notificacion|fecha notificacion|fecha de respuesta",
    "reason": "motivo|novedad|causa|motivo del reclamo",
    "status": "estado|estado del caso"
}
LABELS = dict(zip(HEADERS, (
    "Número original", "Cliente", "Fecha del reclamo", "Código histórico", "Producto",
    "Cantidad reclamada", "Unidad de medida", "Cantidad a reponer", "Decisión declarada",
    "Fecha de despacho", "Soporte declarado", "Fecha de informe", "Fecha de notificación",
    "Motivo", "Estado")))
REQUIRED = {"number", "client", "date", "quantity"}


def norm(value):
    plain = unicodedata.normalize("NFKD", str(value or "").casefold())
    return " ".join(re.findall(r"[a-z0-9]+", "".join(c for c in plain if not unicodedata.combining(c))))


ALIASES = {norm(alias): field for field, options in HEADERS.items() for alias in options.split("|")}


def init_history(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS hist_books(
      id INTEGER PRIMARY KEY, digest TEXT UNIQUE, name TEXT, original BLOB NOT NULL,
      actor TEXT, imported TEXT, confirmed TEXT, base_revision INTEGER, notes TEXT NOT NULL DEFAULT '[]');
    CREATE TABLE IF NOT EXISTS hist_rows(
      id INTEGER PRIMARY KEY, book INTEGER REFERENCES hist_books, sheet TEXT, row_no INTEGER,
      source TEXT NOT NULL, data TEXT NOT NULL, errors TEXT, warnings TEXT,
      mode TEXT NOT NULL DEFAULT 'auto', target INTEGER, reason TEXT, corrected TEXT,
      applied_to INTEGER, UNIQUE(book,sheet,row_no));
    CREATE INDEX IF NOT EXISTS hist_rows_book ON hist_rows(book);
    CREATE TABLE IF NOT EXISTS hist_cases(
      id INTEGER PRIMARY KEY, channel TEXT NOT NULL, number TEXT NOT NULL, client TEXT NOT NULL,
      case_date TEXT NOT NULL, identity TEXT NOT NULL UNIQUE);
    CREATE TABLE IF NOT EXISTS hist_lines(
      id INTEGER PRIMARY KEY, case_id INTEGER NOT NULL REFERENCES hist_cases, identity TEXT NOT NULL,
      source_row INTEGER NOT NULL REFERENCES hist_rows, data TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS hist_lines_key ON hist_lines(case_id, identity);
    CREATE VIEW IF NOT EXISTS history_records AS
      SELECT c.id case_id,l.id line_id,c.channel,'historico' origin,c.number,c.client,c.case_date,
      json_extract(l.data,'$.code') code,json_extract(l.data,'$.description') description,
      json_extract(l.data,'$.unit') unit,json_extract(l.data,'$.quantity') quantity,
      json_extract(l.data,'$.approved') approved,json_extract(l.data,'$.dispatch') dispatch_date,
      json_extract(l.data,'$.support') support,json_extract(l.data,'$.cancelled') cancelled,
      json_extract(l.data,'$.decision') decision,l.source_row
      FROM hist_cases c JOIN hist_lines l ON l.case_id=c.id;
    """)


def packed(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def read_date(raw, epoch):
    if isinstance(raw, (datetime, date)):
        return raw.strftime("%Y-%m-%d")
    if isinstance(raw, (int, float)) and 20000 < raw < 100000:
        return from_excel(raw, epoch).date().isoformat()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(str(raw).strip(), fmt).date().isoformat()
        except ValueError:
            pass
    return None


def number(raw):
    if isinstance(raw, bool):
        raise ValueError()
    value = str(raw).strip().replace(" ", "")
    if "," in value:
        value = value.replace(".", "").replace(",", ".")
    try:
        result = Decimal(value)
        if not result.is_finite() or not 0 <= result <= 10**12:
            raise ValueError()
        return int(result) if result == result.to_integral_value() else float(result)
    except InvalidOperation as exc:
        raise ValueError() from exc


def original(value):
    return value.isoformat() if isinstance(value, (date, datetime)) else str(value if value is not None else "").strip()


def read_row(fields, channel, epoch):
    errors, warnings = [], []
    data = {k: original(fields.get(k)) for k in HEADERS}
    data["channel"] = channel
    data["cancelled"] = int(any("anulad" in norm(data[k]) for k in ("decision", "status", "description")))
    if not data["number"] or not data["client"]:
        errors.append("Falta el número de caso o el cliente.")
    if not data["code"] and not data["description"] and not data["cancelled"]:
        errors.append("Falta el código histórico o la descripción de la línea.")
    for key in ("date", "dispatch", "report", "notification"):
        value = read_date(fields.get(key), epoch)
        if not value and key == "date":
            errors.append("Fecha del caso ausente o ilegible.")
        elif data[key] and not value:
            warnings.append(f"{LABELS[key]} ilegible; se conserva el texto original.")
        data[key] = value
    for key in ("report", "notification", "dispatch"):
        if data[key] and data["date"] and data[key] < data["date"]:
            warnings.append(f"{LABELS[key]} anterior al reclamo; verificar el año.")
    try:
        data["quantity"] = number(fields.get("quantity"))
    except ValueError:
        errors.append("Cantidad reclamada inválida.")
        data["quantity"] = None
    try:
        data["approved"] = number(fields["approved"]) if data["approved"] else None
        decision = norm(data["decision"])
        if data["approved"] is None:
            if decision in ("reponer", "si", "si reponer", "reposicion", "se repone"):
                data["approved"] = data["quantity"]
            elif decision in ("no", "no reponer", "no se repone"):
                data["approved"] = 0
        if data["approved"] is not None and data["quantity"] is not None and data["approved"] > data["quantity"]:
            raise ValueError()
    except ValueError:
        errors.append("Revisar cantidad de reposición.")
    if data["approved"] and not data["dispatch"]:
        warnings.append("Reposición declarada sin fecha de despacho registrada.")
    if norm(data["support"]) in ("nc", "producto") or "@" in data["support"]:
        warnings.append("Verificar qué acredita el soporte declarado.")
    if not data["unit"]:
        data["unit"] = "Sin especificar"
    return data, errors, warnings


def is_summary(name):
    return any(k in norm(name) for k in ("dinamica", "pivote", "pivot", "indicador", "resumen", "grafico"))


def merged_case_cells(archive, sheet, columns):
    """Only inherit header values where the workbook explicitly merges vertical cells."""
    spans = defaultdict(list)
    xml_path = sheet._worksheet_path  # read-only worksheet in the pinned openpyxl version
    with archive.open(xml_path) as xml:
        for _, element in iterparse(xml, events=("end",)):
            if element.tag.rsplit("}", 1)[-1] == "mergeCell":
                c1, r1, c2, r2 = range_boundaries(element.attrib["ref"])
                if c1 == c2 and c1 - 1 in columns and r2 > r1:
                    spans[r1].append((c1-1,r2))
            element.clear()
    return spans


def tag(element):
    return element.tag.rsplit("}", 1)[-1]


def cache_value(element):
    kind = tag(element)
    value = element.get("v", "")
    if kind == "n":
        dec = Decimal(value)
        if not dec.is_finite():
            raise ValueError("Número inválido en la copia de tabla dinámica.")
        return int(dec) if dec == dec.to_integral_value() else float(dec)
    if kind == "d":
        return datetime.fromisoformat(value)
    if kind == "m":
        return None
    return value


class CachedSheet:
    """A read-only source for raw pivot cache records, distinct from displayed totals."""
    sheet_state = "visible"
    max_row = max_column = None

    def __init__(self, archive, path, label):
        self.title = label
        with archive.open(path) as xml:
            definition = safe_parse(xml).getroot()
        fields = next((e for e in definition if tag(e) == "cacheFields"), None)
        source = next((e for e in definition if tag(e) == "cacheSource"), None)
        if fields is None or source is None or source.get("type") != "worksheet":
            raise ValueError("La copia no contiene campos de una hoja de origen.")
        self.names, self.items = [], []
        for field in fields:
            shared = next((e for e in field if tag(e)=="sharedItems"), ())
            self.names.append(field.get("name",""))
            self.items.append([cache_value(e) for e in shared if tag(e) in {"n","s","d","b","m","e"}])
        if not self.names or len(self.names)>256:
            raise ValueError("Cantidad de columnas no admitida en la copia.")
        parent, filename=posixpath.split(path)
        rid = next((v for k,v in definition.attrib.items() if k.endswith("}id")), None)
        try:
            with archive.open(posixpath.join(parent,"_rels",filename+".rels")) as rel:
                relationships = safe_parse(rel).getroot()
        except KeyError as exc:
            raise ValueError("La tabla dinámica carece de registros guardados.") from exc
        target = next((r.get("Target") for r in relationships if r.get("Id")==rid
                       and r.get("Type","").endswith("/pivotCacheRecords") and r.get("TargetMode")!="External"), None)
        if not target:
            raise ValueError("La tabla dinámica carece de registros guardados.")
        target = target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join(parent,target))
        if not target.startswith("xl/pivotCache/") or target not in archive.namelist():
            raise ValueError("No se encontró la copia de registros en este libro.")
        self.archive, self.path = archive, target

    @property
    def values(self):
        yield tuple(self.names)
        with self.archive.open(self.path) as xml:
            for _, element in iterparse(xml,events=("end",)):
                if tag(element)!="r":
                    continue
                values=[]
                for index,item in enumerate(element):
                    if index>=len(self.names):
                        raise ValueError("El registro de la copia excede los encabezados.")
                    if tag(item)=="x":
                        pos=int(item.attrib["v"])
                        if not 0<=pos<len(self.items[index]):
                            raise ValueError("Índice de campo inválido en la copia.")
                        values.append(self.items[index][pos])
                    else:
                        values.append(cache_value(item))
                yield tuple(values)
                element.clear()


def xlsx(data, options=None):
    options = options or {}
    if len(data) > LIMIT:
        raise ValueError("El archivo supera 12 MiB.")
    try:
        with ZipFile(BytesIO(data)) as archive:
            if len(archive.infolist()) > 2000 or sum(x.file_size for x in archive.infolist()) > 64 * 1024**2:
                raise ValueError("El libro supera el límite de lectura.")
        workbook = load_workbook(BytesIO(data), data_only=True, read_only=True, keep_links=False)
    except (BadZipFile, KeyError, OSError, InvalidFileException, ParseError, DefusedXmlException) as exc:
        raise ValueError("Se necesita un archivo .xlsx válido.") from exc
    rows, notes = [], []
    archive = ZipFile(BytesIO(data))
    try:
        sheets = list(workbook.worksheets)
        for path in sorted(archive.namelist()):
            cache = re.fullmatch(r"xl/pivotCache/pivotCacheDefinition([0-9]+)\.xml",path)
            if cache:
                label = "Copia dinámica " + cache[1]
                if options.get("sheet")==label:
                    sheets.append(CachedSheet(archive,path,label))
                else:
                    notes.append(f"{label}: registros en caché disponibles. Contrasta el año y la fuente antes de incorporar esta copia por separado.")
        for sheet in sheets:
            if options.get("sheet") and sheet.title != options["sheet"]:
                continue
            if sheet.sheet_state != "visible" or (not isinstance(sheet, CachedSheet) and is_summary(sheet.title)):
                notes.append(f"{sheet.title}: hoja auxiliar omitida.")
                continue
            if (sheet.max_row or 0) > 30000 or (sheet.max_column or 0) > 256:
                raise ValueError(f"{sheet.title}: demasiadas filas o columnas.")
            head = None
            held, spans = {}, {}
            channel = options.get("channel")
            if not channel:
                if re.search(r"\b(exp|exportacion|exportaciones|internacional)\b", norm(sheet.title)):
                    channel = "internacional"
                elif "seguimiento" in norm(sheet.title) or "nacional" in norm(sheet.title):
                    channel = "nacional"
                else:
                    notes.append(f"{sheet.title}: selecciona explícitamente la hoja y su canal para importarla.")
                    continue
            for row_no, cells in enumerate(sheet.values, 1):
                if row_no > 30000 or len(rows) >= 10000:
                    raise ValueError("La importación admite hasta 10.000 líneas por archivo.")
                if not head:
                    if options.get("header") and row_no != options["header"]:
                        continue
                    names = [original(v) for v in cells]
                    mapped = {i: ALIASES[norm(v)] for i, v in enumerate(cells) if norm(v) in ALIASES}
                    for field, col in options.get("map", {}).items():
                        mapped = {i: k for i, k in mapped.items() if k != field}
                        mapped[col-1] = field
                    if REQUIRED.issubset(mapped.values()) and {"description","code"}.intersection(mapped.values()):
                        if len(mapped) != len(set(mapped.values())):
                            raise ValueError(f"{sheet.title}: hay encabezados ambiguos. Indica las columnas manualmente.")
                        head = mapped
                        identifiers = {i for i, field in head.items() if field in {"number","client","date"}}
                        if not isinstance(sheet,CachedSheet):
                            spans = merged_case_cells(archive, sheet, identifiers)
                    elif row_no >= 30 and not options.get("header"):
                        break
                    continue
                if not any(v is not None and v != "" for v in cells):
                    continue
                src = {f"{i+1} · {names[i] if i < len(names) else ''}": original(v)
                       for i, v in enumerate(cells) if v is not None}
                for i, end_row in spans.get(row_no, []):
                    if i < len(cells):
                        held[i] = end_row, cells[i], row_no
                fields = {key: cells[i] if i < len(cells) else None for i, key in head.items()}
                for i, (end, value, start) in held.items():
                    if start < row_no <= end and fields[head[i]] is None:
                        fields[head[i]] = value
                        src[f"{i+1} · {names[i]} (celda combinada, fila {start})"] = original(value)
                item, errors, warnings = read_row(fields, channel, workbook.epoch)
                rows.append((sheet.title, row_no, src, item, errors, warnings))
            if not head:
                notes.append(f"{sheet.title}: encabezados no reconocidos. Usa la selección manual de columnas.")
    finally:
        workbook.close()
        archive.close()
    if not rows:
        raise ValueError("No se encontraron filas importables. " + " ".join(notes))
    return rows, notes


def case_key(d, correction=""):
    return packed([d["channel"], norm(correction or d["number"]), norm(d["client"]), d["date"]])


def line_key(d):
    return packed([norm(d["code"] or d["description"]), norm(d["unit"])])


def revision(conn):
    return conn.execute("SELECT COUNT(*) FROM hist_books WHERE confirmed IS NOT NULL").fetchone()[0]


def stage(conn, data, name, actor, stamp, options=None):
    digest = hashlib.sha256(data).hexdigest()
    existing = conn.execute("SELECT id, confirmed FROM hist_books WHERE digest=?", (digest,)).fetchone()
    if existing and (existing["confirmed"] or not options):
        return existing["id"]
    parsed, notes = xlsx(data, options)
    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = conn.execute("SELECT id, confirmed FROM hist_books WHERE digest=?", (digest,)).fetchone()
        if existing:
            bid = existing["id"]
            if existing["confirmed"] or not options:
                conn.commit()
                return bid
            if conn.execute("SELECT 1 FROM hist_rows WHERE book=? AND sheet=? AND mode!='auto' LIMIT 1",(bid,options["sheet"])).fetchone():
                raise ValueError("Hay decisiones guardadas; completa esa vista previa antes de cambiar el mapa.")
            # Remap just the selected sheet, preserving other sheets in the preview.
            sheet_name = options["sheet"]
            conn.execute("DELETE FROM hist_rows WHERE book=? AND sheet=?", (bid,sheet_name))
            conn.execute("UPDATE hist_books SET notes=?,base_revision=? WHERE id=?",(packed(notes),revision(conn),bid))
        else:
            bid = conn.execute("""INSERT INTO hist_books(digest,name,original,actor,imported,base_revision,notes)
                                  VALUES(?,?,?,?,?,?,?)""",
                               (digest,name,data,actor,stamp,revision(conn),packed(notes))).lastrowid
        conn.executemany("INSERT INTO hist_rows(book,sheet,row_no,source,data,errors,warnings) VALUES(?,?,?,?,?,?,?)",
                         ((bid,s,n,packed(src),packed(d),packed(e),packed(w)) for s,n,src,d,e,w in parsed))
        conn.commit()
        return bid
    except Exception:
        conn.rollback()
        raise


def inspect(conn, bid):
    rows = []
    by_number, by_support, by_line = defaultdict(set), defaultdict(set), Counter()
    saved, previous_position = defaultdict(list), defaultdict(list)
    for r in conn.execute("""SELECT l.id,l.identity lind,l.data,c.identity cind,hr.sheet,hr.row_no
                             FROM hist_lines l JOIN hist_cases c ON c.id=l.case_id
                             JOIN hist_rows hr ON hr.id=l.source_row"""):
        d=json.loads(r["data"])
        saved[r["cind"], r["lind"]].append(dict(r))
        previous_position[r["cind"], norm(r["sheet"]), r["row_no"]].append(dict(r))
        ck=json.loads(r["cind"])
        by_number[d["channel"],ck[1]].add(r["cind"])
        if d["support"]:
            by_support[d["channel"],norm(d["client"]),d["date"],norm(d["support"])].add(ck[1])
    for r in conn.execute("SELECT * FROM hist_rows WHERE book=? ORDER BY id",(bid,)):
        r=dict(r)
        for field in ("data","errors","warnings","source"):
            r[field]=json.loads(r[field])
        d=r["data"]
        ck=case_key(d,r["corrected"])
        num=json.loads(ck)[1]
        lk=line_key(d)
        r.update(ck=ck,lk=lk,num=num)
        rows.append(r)
        if r["mode"]=="skip":
            continue
        by_number[d["channel"],num].add(ck)
        if d["support"]:
            by_support[d["channel"],norm(d["client"]),d["date"],norm(d["support"])].add(num)
        by_line[ck,lk]+=1
    for r in rows:
        d=r["data"]
        candidates=saved[r["ck"],r["lk"]]
        r["conflicts"]=[]
        if not candidates:
            candidates=previous_position[r["ck"],norm(r["sheet"]),r["row_no"]]
            if candidates:
                r["conflicts"].append("Esta fila del caso tenía otra referencia o medida. Revisa el original anterior.")
        r["candidates"]=[c["id"] for c in candidates]
        if len(by_number[d["channel"],r["num"]])>1:
            r["conflicts"].append("Este número también aparece con otro cliente o fecha.")
        related=by_support[d["channel"],norm(d["client"]),d["date"],norm(d["support"])]
        if d["support"] and len(related)>1:
            r["conflicts"].append("Mismo cliente, fecha y soporte con números " + ", ".join(sorted(related)) + ".")
        if by_line[r["ck"],r["lk"]]>1:
            r["conflicts"].append("Hay varias líneas con la misma referencia en este caso.")
        if len(candidates)>1 or candidates and candidates[0]["data"]!=packed(d):
            r["conflicts"].append("La línea guardada difiere o tiene varias coincidencias.")
        r["automatic_target"]=candidates[0]["id"] if len(candidates)==1 else None
        if r["mode"]=="update" and r["target"] not in r["candidates"]:
            r["errors"].append("La línea de destino ya no coincide con este caso y referencia.")
        r["blocked"]=r["mode"]!="skip" and bool(r["errors"] or (r["mode"]=="auto" and r["conflicts"]))
    return rows


def commit_book(conn, bid, stamp):
    conn.execute("BEGIN IMMEDIATE")
    try:
        b=conn.execute("SELECT * FROM hist_books WHERE id=?",(bid,)).fetchone()
        if not b:
            raise ValueError("No se encontró la importación.")
        if b["confirmed"]:
            conn.commit()
            return
        if b["base_revision"]!=revision(conn):
            raise ValueError("Otra importación cambió el histórico. Actualiza la conciliación antes de confirmar.")
        rows=inspect(conn,bid)
        if any(r["blocked"] for r in rows):
            raise ValueError("Hay filas pendientes de conciliación.")
        touched=set()
        for r in rows:
            if r["mode"]=="skip":
                continue
            d=r["data"]
            conn.execute("""INSERT INTO hist_cases(channel,number,client,case_date,identity) VALUES(?,?,?,?,?)
                          ON CONFLICT(identity) DO NOTHING""",(d["channel"],r["corrected"] or d["number"],d["client"],d["date"],r["ck"]))
            cid=conn.execute("SELECT id FROM hist_cases WHERE identity=?",(r["ck"],)).fetchone()[0]
            target=r["target"] if r["mode"]=="update" else r["automatic_target"] if r["mode"] in ("auto","keep") else None
            if target:
                if target in touched:
                    raise ValueError("Dos filas se asignaron al mismo destino. Omite la duplicada o consérvala como línea independiente.")
                if r["mode"]=="keep" and (len(r["candidates"])!=1 or any("guardada difiere" in c for c in r["conflicts"])):
                    raise ValueError("Para sustituir una línea con diferencias elige actualizar su versión.")
                conn.execute("UPDATE hist_lines SET data=?, source_row=?, identity=? WHERE id=? AND case_id=?",
                             (packed(d),r["id"],r["lk"],target,cid))
            else:
                target=conn.execute("INSERT INTO hist_lines(case_id,identity,source_row,data) VALUES(?,?,?,?)",
                                    (cid,r["lk"],r["id"],packed(d))).lastrowid
            touched.add(target)
            conn.execute("UPDATE hist_rows SET applied_to=? WHERE id=?",(target,r["id"]))
        conn.execute("UPDATE hist_books SET confirmed=? WHERE id=?",(stamp,bid))
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def book_meta(conn,bid):
    b=conn.execute("SELECT id,name,digest,imported,confirmed,base_revision,notes FROM hist_books WHERE id=?", (bid,)).fetchone()
    if not b:
        abort(404)
    return b


def worksheet_options(form):
    sheet=form.get("sheet","").strip()
    if not sheet:
        return {}
    channel=form.get("channel")
    if channel not in ("nacional","internacional"):
        raise ValueError("Indica el canal de la hoja.")
    columns={}
    for key in HEADERS:
        col=form.get("map_"+key,"").strip().upper()
        if col:
            from openpyxl.utils import column_index_from_string
            idx=column_index_from_string(col)
            if idx>256 or idx in columns.values():
                raise ValueError("Usa columnas distintas de la A a la IV.")
            columns[key]=idx
    header=int(form.get("header") or 1)
    if not 1<=header<=30000:
        raise ValueError("Indica el número de fila del encabezado.")
    return {"sheet":sheet,"channel":channel,"header":header,"map":columns}


def render_home(error=None):
    conn=db()
    cid=request.args.get("caso",type=int)
    query=request.args.get("q","").strip()
    recent=conn.execute("SELECT id,name,imported,confirmed FROM hist_books ORDER BY id DESC").fetchall()
    page=max(1,request.args.get("p",default=1,type=int) or 1)
    cases=conn.execute("""SELECT case_id,channel,number,client,case_date,count(*) lines FROM history_records
             WHERE instr(lower(client || ' ' || number), lower(?))>0 GROUP BY case_id ORDER BY case_date DESC,case_id
             LIMIT 51 OFFSET ?""",(query,(page-1)*50)).fetchall()
    detail=conn.execute("SELECT * FROM history_records WHERE case_id=? ORDER BY line_id",(cid,)).fetchall() if cid else []
    versions=conn.execute("""SELECT r.*,b.name, b.confirmed FROM hist_rows r JOIN hist_books b ON r.book=b.id
           WHERE applied_to IN (SELECT line_id FROM history_records WHERE case_id=?) ORDER BY b.confirmed,r.id""",(cid,)).fetchall() if cid else []
    return render_template("historico.html",view="home",cases=cases[:50],more=len(cases)>50,page=page,books=recent,detail=detail,versions=versions,
                           labels=LABELS,error=error,q=query)


@bp.get("/historico")
@require("administrador")
def home():
    return render_home()


@bp.post("/historico/importar")
@require("administrador")
def upload():
    f=request.files.get("archivo")
    if not f or not (f.filename or "").lower().endswith(".xlsx"):
        return render_home("Selecciona un libro .xlsx."),400
    try:
        bid=stage(db(),f.read(LIMIT+1),Path(f.filename).name,role(),now(),worksheet_options(request.form))
    except ValueError as exc:
        return render_home(str(exc)),400
    except (OSError,BadZipFile,KeyError,ParseError,DefusedXmlException,InvalidFileException,TypeError,OverflowError):
        return render_home("El libro está dañado o contiene datos no admitidos. Guarda una copia válida .xlsx."),400
    return redirect(url_for(".preview",bid=bid),code=303)


def render_preview(bid,error=None):
    conn=db()
    book=book_meta(conn,bid)
    rows=inspect(conn,bid)
    counts=defaultdict(lambda: {"cases":set(),"rows":0,"units":defaultdict(float),"approved":defaultdict(float),"pending":defaultdict(float),"pending_cases":set()})
    for r in rows:
        if r["mode"]=="skip":
            continue
        c=counts[r["sheet"]]
        d=r["data"]
        c["cases"].add(r["ck"])
        c["rows"]+=1
        c["units"][d["unit"]]+=d["quantity"] or 0
        c["approved"][d["unit"]]+=d["approved"] or 0
        if d["approved"] and not d["dispatch"]:
            c["pending"][d["unit"]]+=d["approved"]
            c["pending_cases"].add(r["ck"])
    pending=sum(r["blocked"] for r in rows)
    if request.args.get("vista")=="pendientes":
        rows=[r for r in rows if r["blocked"]]
    page=max(1,request.args.get("p",default=1,type=int) or 1)
    return render_template("historico.html",view="preview",book=book,notes=json.loads(book["notes"]),
      labels=LABELS,rows=rows[(page-1)*40:page*40],counts=counts,page=page,pages=(len(rows)+39)//40,pending=pending,
      error=error,stale=revision(conn)!=book["base_revision"])


@bp.get("/historico/importaciones/<int:bid>")
@require("administrador")
def preview(bid):
    return render_preview(bid)


@bp.get("/historico/importaciones/<int:bid>/original")
@require("administrador")
def original_file(bid):
    b=db().execute("SELECT original,name FROM hist_books WHERE id=?",(bid,)).fetchone()
    if not b:
        abort(404)
    return send_file(BytesIO(b["original"]),as_attachment=True,download_name=b["name"],
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",max_age=0)


@bp.post("/historico/importaciones/<int:bid>/actualizar")
@require("administrador")
def refresh(bid):
    conn=db()
    book_meta(conn,bid)
    # Las decisiones se deben revisar si se confirmó otro libro entretanto.
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("""UPDATE hist_rows SET mode='auto',target=NULL,reason=NULL,corrected=NULL
                      WHERE book=? AND EXISTS(SELECT 1 FROM hist_books WHERE id=? AND confirmed IS NULL AND base_revision!=?)""",
                     (bid,bid,revision(conn)))
        conn.execute("UPDATE hist_books SET base_revision=? WHERE id=? AND confirmed IS NULL",(revision(conn),bid))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return redirect(url_for(".preview",bid=bid),code=303)


@bp.post("/historico/importaciones/<int:bid>/confirmar")
@require("administrador")
def confirm(bid):
    book_meta(db(),bid)
    try:
        commit_book(db(),bid,now())
    except ValueError as exc:
        return render_preview(bid,str(exc)),409
    return redirect(url_for(".preview",bid=bid),code=303)


@bp.route("/historico/importaciones/<int:bid>/filas/<int:rid>",methods=["GET","POST"])
@require("administrador")
def resolve(bid,rid):
    conn=db()
    book=book_meta(conn,bid)
    error=None
    row=next((r for r in inspect(conn,bid) if r["id"]==rid),None)
    if not row:
        abort(404)
    if request.method=="POST":
        mode=request.form.get("mode")
        target=request.form.get("target",type=int)
        reason=request.form.get("reason","").strip()
        corrected=request.form.get("corrected","").strip()
        if mode not in ("auto","skip","keep","insert","update"):
            error="Selecciona una decisión."
        elif mode!="auto" and not reason:
            error="Indica el motivo para la auditoría."
        elif mode=="update" and not target:
            error="Falta seleccionar una línea guardada."
        elif mode=="keep" and len(row["candidates"])>1:
            error="Selecciona la línea guardada exacta o conserva una línea independiente."
        elif len(reason)>3000 or len(corrected)>100:
            error="Revisa la longitud del número o del motivo."
        else:
            result=conn.execute("""UPDATE hist_rows SET mode=?,target=?,reason=?,corrected=?
                WHERE id=? AND book=? AND coalesce(reason,'')=? AND mode=? AND coalesce(corrected,'')=?
                AND coalesce(target,0)=? AND EXISTS(SELECT 1 FROM hist_books WHERE id=? AND confirmed IS NULL)""",
                 (mode,target if mode=="update" else None,reason,corrected,rid,bid,
                  request.form.get("old_reason",""),request.form.get("old_mode",""),request.form.get("old_corrected",""),
                  request.form.get("old_target",type=int) or 0,bid))
            if result.rowcount:
                return redirect(url_for(".preview",bid=bid),code=303)
            error="La fila cambió o ya se confirmó el libro. Vuelve a abrir la fila."
    differences = {}
    for target in row["candidates"]:
        old = conn.execute("SELECT data FROM hist_lines WHERE id=?", (target,)).fetchone()
        if old:
            earlier = json.loads(old["data"])
            differences[target] = [(LABELS[key], earlier.get(key), row["data"].get(key))
                                  for key in HEADERS if earlier.get(key) != row["data"].get(key)]
    return render_template("historico.html",view="row",row=row,book=book,error=error,
                           differences=differences),409 if error else 200


@bp.post("/historico/importaciones/<int:bid>/hoja")
@require("administrador")
def add_sheet(bid):
    conn = db()
    book = conn.execute("SELECT name,original,confirmed FROM hist_books WHERE id=?",(bid,)).fetchone()
    if not book:
        abort(404)
    if book["confirmed"]:
        abort(409)
    try:
        options = worksheet_options(request.form)
        if not options:
            raise ValueError("Escribe el nombre exacto de la hoja o copia dinámica.")
        stage(conn, book["original"], book["name"], role(), now(), options)
    except (ValueError,OSError,KeyError,BadZipFile,DefusedXmlException,ParseError,InvalidFileException) as exc:
        return render_preview(bid,str(exc)),400
    return redirect(url_for(".preview",bid=bid),code=303)
