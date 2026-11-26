"""Aplicaciones monetarias internacionales. La moneda y las unidades tienen saldos independientes."""
import json
import re
from decimal import Decimal, InvalidOperation

from flask import abort

from common import db, now, role

SCHEMA = """
CREATE TABLE IF NOT EXISTS credit_notes (
 id INTEGER PRIMARY KEY, case_id INTEGER NOT NULL REFERENCES cases(id),
 number TEXT NOT NULL, identity TEXT NOT NULL, currency TEXT NOT NULL, total_milli INTEGER NOT NULL CHECK(total_milli>0),
 UNIQUE(case_id,identity)
);
CREATE TABLE IF NOT EXISTS credit_applications (
 id INTEGER PRIMARY KEY, note_id INTEGER NOT NULL REFERENCES credit_notes(id),
 doc_type TEXT NOT NULL, doc_number TEXT NOT NULL, amount_milli INTEGER NOT NULL CHECK(amount_milli>0),
 applied_on TEXT NOT NULL, actor TEXT NOT NULL, recorded_at TEXT NOT NULL, file_id INTEGER REFERENCES case_files(id)
);
CREATE INDEX IF NOT EXISTS applications_for_credit ON credit_applications(note_id,id);
"""
# D1 and SQLite both store integers exactly in this range. Three decimals cover currencies such as BHD.
def milli(value):
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or not Decimal(0) < amount <= Decimal("1000000000000"):
            raise ValueError
        scaled = amount * 1000
        if scaled != scaled.to_integral_value():
            raise ValueError
        return int(scaled)
    except (InvalidOperation, ValueError):
        abort(400, "El importe debe ser positivo y tener como máximo tres decimales.")


def display_amount(millis):
    return format(Decimal(millis) / 1000, "f")


def listed(cid):
    notes = []
    by_id = {}
    for n in db().execute("SELECT * FROM credit_notes WHERE case_id=? ORDER BY id", (cid,)):
        note = dict(id=n["id"], number=n["number"], currency=n["currency"], total=display_amount(n["total_milli"]),
                    applications=[], remaining_milli=n["total_milli"])
        by_id[n["id"]] = note
        notes.append(note)
    for r in db().execute("""SELECT a.* FROM credit_applications a JOIN credit_notes n ON n.id=a.note_id
                             WHERE n.case_id=? ORDER BY a.id""",(cid,)):
        note = by_id[r["note_id"]]
        note["applications"].append(dict(type=r["doc_type"], number=r["doc_number"], amount=display_amount(r["amount_milli"]),
                                         date=r["applied_on"], file_id=r["file_id"], actor=r["actor"]))
        note["remaining_milli"] -= r["amount_milli"]
    for note in notes:
        note["remaining"] = display_amount(note.pop("remaining_milli"))
    return notes


def record(c, data):
    from cases import (bump, day, event, evidence_for, items,
                       line_for, number, one, service_without_lines, set_line, string)

    if c["channel"] != "internacional" or role() not in ("internacional","administrador"):
        abort(403)
    note_number = string(data,"number",True)
    currency = string(data,"currency",True).upper()
    if not re.fullmatch("[A-Z]{3}", currency):
        abort(400,"Indica la moneda con su código de tres letras.")
    if len(note_number) > 150:
        abort(400,"Revisa el número de la nota crédito.")
    total = milli(data.get("total"))
    date = day(data,"date")
    file_id = data.get("file_id")
    distribution = items(data,"applications")
    if not distribution:
        abort(400,"Agrega al menos una factura u orden de compra.")
    applications = []
    documents = set()
    for app in distribution:
        kind, ref = string(app,"type",True), string(app,"number",True)
        if kind not in ("factura","oc") or len(ref)>150:
            abort(400,"Selecciona una factura u orden de compra y escribe su número.")
        document = (kind, ref.casefold())
        if document in documents:
            abort(400,"Reúne la aplicación a cada documento en un solo renglón.")
        documents.add(document)
        applications.append((kind,ref,milli(app.get("amount"))))
    identity = note_number.casefold()
    existing = one("SELECT * FROM credit_notes WHERE case_id=? AND identity=?", (c["id"], identity))
    if existing:
        if existing["currency"]!=currency or existing["total_milli"]!=total:
            abort(409,"La moneda o el importe total difieren de la nota ya registrada.")
        note_id = existing["id"]
    else:
        note_id = db().execute("INSERT INTO credit_notes(case_id,number,identity,currency,total_milli) VALUES(?,?,?,?,?)",
                               (c["id"],note_number,identity,currency,total)).lastrowid
    previous = one("SELECT COALESCE(SUM(amount_milli),0) AS total FROM credit_applications WHERE note_id=?",(note_id,))["total"]
    applied = sum(amount for _,_,amount in applications)
    if previous + applied > total:
        abort(409,"La aplicación supera el saldo disponible de la nota crédito.")
    declared_lines = items(data,"covered_lines")
    covered_ids = set()
    settlements = []
    for item in declared_lines:
        line = line_for(c["id"],item.get("line_id"))
        qty = number(item,"quantity",True)
        if line["id"] in covered_ids:
            abort(400,"Una referencia figura dos veces en la misma aplicación.")
        covered_ids.add(line["id"])
        updated = round(line["credit_done"] + qty, 3)
        if updated>line["credit"]:
            abort(409,"Las unidades superan el crédito autorizado para esta referencia.")
        set_line(line["id"],credit_done=updated)
        settlements.append(dict(type="credit",line_id=line["id"],quantity=qty,date=date,support=note_number,
                                credit_note_id=note_id,file_id=file_id))
    if file_id:
        file_id=evidence_for(c,file_id,covered_ids)
    if data.get("settle_service") is True:
        if not service_without_lines(c):
            abort(409,"Este caso no tiene un compromiso de servicio por acreditar.")
        decision=one("""SELECT id,payload FROM events WHERE case_id=? AND cycle=? AND action='decision'
                         ORDER BY id DESC LIMIT 1""",(c["id"],c["cycle"]))
        if not decision or json.loads(decision["payload"]).get("type")!="credit":
            abort(409,"Registra primero la decisión de crédito para el servicio.")
        if one("SELECT id FROM events WHERE case_id=? AND cycle=? AND action='resolve' AND id>? LIMIT 1",
               (c["id"],c["cycle"],decision["id"])):
            abort(409,"El servicio ya tiene un cumplimiento registrado.")
        settlements.append(dict(type="credit",date=date,support=note_number,credit_note_id=note_id,file_id=file_id))
    stamp = now()
    db().executemany("""INSERT INTO credit_applications(note_id,doc_type,doc_number,amount_milli,applied_on,actor,recorded_at,file_id)
                        VALUES(?,?,?,?,?,?,?,?)""",((note_id,kind,ref,amt,date,role(),stamp,file_id) for kind,ref,amt in applications))
    bump(c)
    event(c,"credit_apply",dict(number=note_number,currency=currency,date=date,file_id=file_id,
          total=display_amount(total),remaining=display_amount(total-previous-applied),
          applications=[dict(type=kind,number=ref,amount=display_amount(amt)) for kind,ref,amt in applications]))
    for payload in settlements:
        event(c,"resolve",payload)
