"""Private workload, claim and order cohorts with traceable record-level results."""
import csv
import hashlib
import io
import json
import math
import os
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from urllib.parse import urlencode

import click
from flask import Blueprint, Response, abort, jsonify, redirect, render_template, request

from common import db, require, role
from sync_orders import BOGOTA, CHANNELS, init_orders, load_adapter, local_day, refresh, run_sync

bp = Blueprint("metrics", __name__, cli_group=None)
init_metrics = init_orders

ORIGINS = {"rsales_formulario": "Formulario RSales nacional", "nacional_manual": "Manual nacional",
           "internacional_manual": "Manual internacional", "historico": "Histórico sin origen verificable"}
LABELS = {"pendientes":"Enviados a Calidad sin respuesta completa","informacion":"Esperando información del área",
          "comunicar":"Respuestas por comunicar","reponer":"Reposiciones por verificar","casos":"Expedientes registrados",
          "enviados":"Enviados a Calidad","completos":"Con respuesta completa","sin_acuse":"Sin acuse de Calidad",
          "analisis":"En análisis","parcial":"Respuesta técnica parcial","vinculados":"Con pedido vinculado",
          "sin_vinculo":"Con daño reportado sin pedido vinculado","creados":"Pedidos creados",
          "reportados":"Pedidos con daño reportado","despachados":"Pedidos con despacho comprobado",
          "confirmados":"Pedidos despachados con daño confirmado","historico":"Expedientes históricos por verificar",
          "tardios":"Pedidos con reclamos registrados después del período","anulados":"Envíos anulados",
          "formularios":"Formularios importados","revisados":"Revisiones del formulario","publicados":"Con respuesta publicada",
          "atendidos":"Comunicación registrada","credito":"Créditos documentados"}
QUEUES={"cola_borradores":"Borradores sin enviar","cola_calidad":"En Calidad sin respuesta completa",
        "cola_info":"Falta información del área",
        "cola_comunicar":"Respuestas por comunicar","cola_reponer":"Reposiciones por verificar"}
LABELS.update(QUEUES)
DIMS={"client":"Cliente","seller":"Vendedor","country":"País","city":"Ciudad","code":"Referencia",
      "presentation":"Presentación","lot":"Lote","stage":"Etapa de detección declarada","incidence":"Incidencia","cause":"Causa técnica"}
STATES = {"borrador":"Borrador","enviado":"Sin acuse","analisis":"En análisis","informacion":"Falta información",
          "parcial":"Respuesta parcial","respondido":"Respuesta completa","cerrado":"Cerrado","anulado":"Anulado"}
PEOPLE = {"nacional": "Zulay", "internacional": "Angélica", "calidad": "Juan Diego", "administrador": "Don Carlos"}
NEXT_STEP = {
    "borrador": "Completar el borrador y enviar a Calidad",
    "enviado": "Acusar recibo del expediente",
    "analisis": "Analizar las referencias y publicar el informe",
    "informacion": "Aportar la información solicitada",
    "parcial": "Completar la respuesta técnica pendiente",
    "respondido": "Comunicar la respuesta y verificar la solución",
    "cerrado": "Expediente cerrado",
}
TIMES={"submit":"Registro a envío","complete":"Envío a respuesta completa","technical":"Registro a respuesta completa",
       "communicate":"Registro a comunicación","dispatch":"Registro a último despacho"}
BALANCES={"quantity":"Reclamado","confirmed":"Daño confirmado","replacement":"Reposición aprobada",
          "dispatched":"Con despacho registrado","pending":"Reposición pendiente"}
def today():
    return datetime.now(BOGOTA).date()


def window(args):
    anchor = date.fromisoformat(args.get("fecha") or str(today()))
    period = args.get("periodo","mes")
    if period == "rango":
        a,b = date.fromisoformat(args["desde"]),date.fromisoformat(args["hasta"])
    elif period == "dia":
        a=b=anchor
    elif period == "semana":
        a=anchor-timedelta(days=anchor.weekday())
        b=a+timedelta(days=6)
    elif period in ("mes","trimestre","anio"):
        month=1 if period=="anio" else (1+3*((anchor.month-1)//3) if period=="trimestre" else anchor.month)
        length={"mes":1,"trimestre":3,"anio":12}[period]
        a=date(anchor.year,month,1)
        b=date(a.year+(month+length-1)//12,(month+length-1)%12+1,1)-timedelta(days=1)
    else:
        raise ValueError("Período inválido.")
    if a>b or a>today() or (b-a).days>3660:
        raise ValueError("Revisa el rango (hasta diez años y fecha inicial no futura).")
    return a,min(b,today())


def json_dict(s):
    return json.loads(s or "{}") if isinstance(s,str) else (s or {})


def verified_coverage(runs, a,b, channel):
    spans=sorted((r["start"],r["end"]) for r in runs if channel in json.loads(r["scope"]))
    needed=a
    for start,end in spans:
        if start>str(needed):
            break
        if end>=str(needed):
            needed=date.fromisoformat(end)+timedelta(days=1)
        if needed>b:
            return True
    return False


def recordsets():
    return {k:[] for k in LABELS}


def order_row(o):
    d=json_dict(o['data'])
    return {"id":o['id'],"ref":o["number"] or o["code"],"client":d.get("client_name") or d.get("client_id") or "Por verificar",
            "channel":o["channel"],"date":o["created"],"state":d.get("state") or "Sin estado",
            "actor":d.get("seller_id") or "—","next":o["code"],"url":None}


def percentile(values, q):
    if not values:
        return None
    vals=sorted(values)
    i=(len(vals)-1)*q
    return round(vals[math.floor(i)]+(vals[math.ceil(i)]-vals[math.floor(i)])*(i-math.floor(i)),2)


def age(start,end,calendar="calendario"):
    a,b=local_day(start),local_day(end)
    n=(b-a).days
    if n<0:
        return None
    if calendar=="laboral":
        weeks,remaining=divmod(n,7)
        return weeks*5+sum((a+timedelta(days=i+1)).weekday()<5 for i in range(remaining))
    return n


def aggregate(conn,a,b,channel="",origin="",dim="client",query="",cohort="creacion"):
    where="channel=?" if channel else "1=1"
    ch_args=(channel,) if channel else ()
    rows=recordsets()
    filter_text=query.casefold().strip()
    settings=dict(conn.execute("SELECT * FROM rs_settings WHERE id=1").fetchone())
    def matches(row):
        return not filter_text or filter_text in str(row.get(dim) or "Por verificar").casefold()
    buckets=defaultdict(lambda:{"ids":set(),"units":Counter(),"orders":set()})
    months=defaultdict(lambda:{"cases":set(),"orders":set(),"units":Counter()})
    balances=defaultdict(Counter)
    balance_details=defaultdict(list)
    time_values=defaultdict(list)
    case_ids_for_timing=defaultdict(list)
    order_ids={name:set() for name in ("reportados","confirmados","tardios")}
    state_counts=Counter()
    orders={}
    if cohort=="salida": # Date offsets must be converted before comparing departure days.
        order_query=f"SELECT * FROM rs_orders WHERE active=1 AND type='P' AND {where} AND shipped_at IS NOT NULL"
        order_params=ch_args
    else:
        order_query=f"SELECT * FROM rs_orders WHERE active=1 AND type='P' AND {where} AND created BETWEEN ? AND ?"
        order_params=(*ch_args,str(a),str(b))
    for o in conn.execute(order_query,order_params):
        when=local_day(o["shipped_at"]) if cohort=="salida" else date.fromisoformat(o["created"])
        if a<=when<=b:
            rec=order_row(o)
            rec["date"]=str(when)
            orders[o["id"]]=rec
            rows['creados'].append(rec)
            state_counts[rec['state']]+=1
            months[str(when)[:7]]["orders"].add(o["id"])
            if o['shipped_at']:
                rows["despachados"].append(rec)
    shipped_ids={r["id"] for r in rows["despachados"]}
    # Include later claims linked to orders in the selected creation cohort, even across months.
    # Data for unrelated channels never contributes to the selected report.
    wherecases=where
    case_args=list(ch_args)
    if origin:
        wherecases+=" AND origin=?"
        case_args.append(origin)
    cases=[dict(c) for c in conn.execute(f"SELECT * FROM cases WHERE {wherecases}",case_args)]
    def children(table,key="case_id",order="id"):
        return conn.execute(f"""SELECT t.* FROM {table} t JOIN (
            SELECT id FROM cases WHERE {wherecases}) c ON t.{key}=c.id ORDER BY t.{order}""",case_args)
    lines=defaultdict(list)
    for l in children("case_lines"):
        lines[l["case_id"]].append(dict(l))
    events=defaultdict(list)
    for e in children("events"):
        events[e["case_id"]].append(e)
    links=defaultdict(list)
    for link in children("order_links",order="line_id"):
        links[link["line_id"]].append(link)
    reports=defaultdict(dict)
    for report in children("reports"):
        findings=json.loads(report["findings"] or "[]")
        if isinstance(findings,list) and report["status"] not in ("draft","borrador"):
            for f in findings:
                if isinstance(f,dict):
                    reports[report["case_id"]][f.get("line_id")]=f
    def clock(key,begin,end,rec):
        if begin and end:
            n=age(begin,end,settings["calendar"])
            if n is not None:
                time_values[key].append(n)
                case_ids_for_timing[key].append(dict(rec,measure=n))
    for c in cases:
        cid=c["id"]
        cf=json_dict(c["fields"])
        reg=local_day(c["created_at"])
        timeline=events[cid]
        by_date=[e for e in timeline if local_day(e["created_at"])<=b]
        ev=defaultdict(list)
        for e in timeline:
            ev[e["action"]].append(e)
        valid_lines=[]
        for l in lines[cid]:
            dims={**cf,**json_dict(l["fields"]),**l,"client":c["client"],
                  "cause":reports[cid].get(l["id"],{}).get("cause")}
            if matches(dims):
                valid_lines.append((l,dims))
        if filter_text and not valid_lines and (lines[cid] or not matches({**cf,"client":c["client"]})):
            continue
        in_period=a<=reg<=b
        sent=c["submitted_at"]
        is_active=sent and c["status"] not in ("anulado","cancelado","borrador")
        anchor = next((e["created_at"] for e in reversed(timeline) if e["action"] in ("submit","reopen")),sent)
        responsible = "calidad" if c["status"] in ("enviado","analisis","parcial") else c["channel"]
        rec={"id":cid,"ref":f"RC-{cid}","client":c["client"] or "Sin cliente","channel":c["channel"],
             "state":STATES.get(c["status"],c["status"]),"date":str(reg),"sent":str(local_day(anchor)) if anchor else None,
             "age":age(anchor,today()) if anchor else None,"actor":timeline[-1]["actor"] if timeline else "—",
             "last":timeline[-1]["action"] if timeline else None,
             "case_age":age(c["created_at"],today()),
             "responsible":PEOPLE[responsible] if c["status"]!="cerrado" else "—",
             "next":NEXT_STEP.get(c["status"],"Revisar expediente"),
             "url":f"/#caso/{cid}"}
        if in_period:
            rows["casos"].append(rec)
            months[str(reg)[:7]]["cases"].add(cid)
            if c["origin"]=="rsales_formulario":
                rows["formularios"].append(rec)
            for key,names in [("revisados",("review","review_contact","review_import")),
                              ("publicados",("publish",)),("atendidos",("communicate",))]:
                if any(ev[n] for n in names): rows[key].append(rec)
            if any(json_dict(e["payload"]).get("type")=="credit" for e in ev["resolve"]): rows["credito"].append(rec)
            clock("submit",c["created_at"],sent,rec)
            if ev["communicate"]: clock("communicate",c["created_at"],ev["communicate"][-1]["created_at"],rec)
            if ev["dispatch"]: clock("dispatch",c["created_at"],ev["dispatch"][-1]["created_at"],rec)
            if valid_lines and any(links[l["id"]] for l,d in valid_lines): rows["vinculados"].append(rec)
        if c["status"]=="borrador" and not sent:
            rows["cola_borradores"].append(rec)
        if not is_active:
            if sent and a<=local_day(sent)<=b: rows["anulados"].append(rec)
            continue
        if c["status"] in ("enviado","analisis","informacion","parcial"):
            rows["cola_calidad"].append(rec)
        if c["status"]=="informacion": rows["cola_info"].append(rec)
        current_ev=[e for e in timeline if e["cycle"]==c["cycle"]]
        pub_ids={json_dict(e["payload"]).get("report_id") for e in current_ev if e["action"]=="publish"}
        comm_ids={json_dict(e["payload"]).get("report_id") for e in current_ev if e["action"]=="communicate"}
        if pub_ids and max(x or 0 for x in pub_ids) not in comm_ids:
            rows["cola_comunicar"].append(dict(rec,responsible=PEOPLE[c["channel"]], next="Registrar la comunicación de la respuesta con el vendedor"))
        if any(l["replacement"]>l["dispatched"] for l,d in valid_lines):
            rows["cola_reponer"].append(dict(rec,responsible=PEOPLE[c["channel"]], next="Verificar el despacho y registrar el soporte de reposición"))
        # Reconstruct the open cycle at the inclusive cut, based on verified events.
        cycle_events=[]
        for e in by_date:
            if e['action'] in ('submit','reopen'): cycle_events=[]
            cycle_events.append(e)
        actions=[e['action'] for e in cycle_events]
        submits=[e for e in cycle_events if e["action"] in ("submit","reopen")]
        cycle_start=submits[0]["created_at"] if submits else None
        published=[e for e in cycle_events if e["action"]=="publish"]
        full_pub=None
        covered=set()
        for e in published:
            p=json_dict(e["payload"])
            findings=p.get("findings",[]) if isinstance(p,dict) else []
            covered.update(f.get("line_id") for f in findings if isinstance(f,dict))
            covered.update(p.get("line_ids",[]))
            if p.get("status")=="respondido" or not lines[cid] or all(l["id"] in covered for l in lines[cid]):
                full_pub=e["created_at"]
                break
        closed="close" in actions
        if not full_pub and published and (closed or (c["status"] in ("respondido","cerrado") and len(by_date)==len(timeline))):
            full_pub=published[-1]["created_at"]
        if cycle_start and a<=local_day(cycle_start)<=b:
            rows["enviados"].append(rec)
            if full_pub or closed:
                rows["completos"].append(rec)
                clock("complete",cycle_start,full_pub,rec)
            else:
                rows["pendientes"].append(rec)
                state="sin_acuse"
                for act in actions:
                    state={"ack":"analisis","request_info":"informacion","provide_info":"analisis","publish":"parcial"}.get(act,state)
                rows[state].append(rec)
        if in_period:
            clock("technical",c["created_at"],full_pub,rec)
            if published and not any(e["action"]=="communicate" and e["id"]>published[-1]["id"] for e in by_date):
                rows["comunicar"].append(rec)
        missing_link=False
        pending=False
        for l,d in valid_lines:
            qty=l["quantity"] or 0
            unit=l["unit"] or "Sin unidad"
            allocated=links[l["id"]]
            dam=str(l["incidence"] or "").casefold().strip() in ("daño físico","dano_fisico","daño_fisico")
            if dam and qty>0:
                if sum(x["quantity"] for x in allocated)<qty: missing_link=True
                for x in allocated:
                    if x["order_id"] not in orders: continue
                    order_ids["reportados"].add(x["order_id"])
                    if reg>b: order_ids["tardios"].add(x["order_id"])
            for x in allocated:
                if x["order_id"] in shipped_ids and (l["confirmed"] or 0)>qty-x["quantity"]:
                    order_ids["confirmados"].add(x["order_id"])
            if in_period:
                bucket=str(d.get(dim) or "Por verificar")
                buckets[bucket]["ids"].add(cid)
                buckets[bucket]["units"][unit]+=qty
                buckets[bucket]["orders"].update(x["order_id"] for x in allocated)
                months[str(reg)[:7]]["units"][unit]+=qty
                for key in BALANCES:
                    value=max(0,(l["replacement"] or 0)-(l["dispatched"] or 0)) if key=="pending" else (l[key] or 0)
                    balances[unit][key]+=value
                    if value:
                        balance_details[unit,key].append(dict(rec,measure=value,line=l["code"] or l["description"]))
                    if key=="pending" and value: pending=True
        if in_period and pending: rows['reponer'].append(rec)
        if in_period and missing_link: rows['sin_vinculo'].append(rec)
    hist_amounts=defaultdict(Counter)
    history_rows=defaultdict(list)
    if not origin or origin=="historico":
        sql="""SELECT * FROM history_records WHERE case_date BETWEEN ? AND ?"""
        params=[str(a),str(b)]
        if channel:
            sql+=" AND channel=?"; params.append(channel)
        seen=set()
        for h in conn.execute(sql,params):
            if filter_text and not matches(dict(h)): continue
            cid=h["case_id"]
            historical={"id":cid,"ref":h["number"],"channel":h["channel"],"date":h["case_date"],
                    "client":h["client"],"state":"Histórico por verificar","next":"Conciliar soportes","actor":"—",
                    "url":"/historico?"+urlencode({"caso":cid})}
            if not h["cancelled"]:
                for key,value in (("hist_quantity",h["quantity"]),("hist_approved",h["approved"]),
                                  ("hist_undated",h["approved"] if not h["dispatch_date"] else 0)):
                    if value:
                        unit=h["unit"] or "Sin unidad"
                        hist_amounts[unit][key]+=value
                        history_rows[unit,key].append(dict(historical,measure=value,line=h["code"]))
            if cid not in seen:
                rows["historico"].append(historical)
                seen.add(cid)
    for key in QUEUES:
        rows[key].sort(key=lambda r:(-(r["case_age"] or 0),r["id"]))
    for k,ids in order_ids.items():
        rows[k]=[orders[i] for i in sorted(ids)]
    runs=list(conn.execute("SELECT start,end,scope FROM rs_runs WHERE state='completa'"))
    coverage={ch:verified_coverage(runs,a,b,ch) for ch in CHANNELS}
    denominator_ok=(coverage.get(channel,False) if channel else all(coverage[c] for c in CHANNELS[:2]))
    if channel in CHANNELS[:2] and conn.execute("SELECT 1 FROM rs_orders WHERE active=1 AND channel='por_clasificar' AND created BETWEEN ? AND ? LIMIT 1",(str(a),str(b))).fetchone():
        denominator_ok=False
    denominator_ok=denominator_ok and not origin and not filter_text and cohort=="creacion"
    def rate(top,base):
        return 100*len(rows[top])/len(rows[base]) if denominator_ok and rows[base] else None
    return dict(rows=rows,counts={k:len(v) for k,v in rows.items()},balances=dict(balances),balance_details=balance_details,
                times={k:{"n":len(time_values[k]),"median":percentile(time_values[k],.5),"p90":percentile(time_values[k],.9),
                          "open":max(0,len(rows["enviados"] if k=="complete" else rows["casos"])-len(time_values[k]))} for k in TIMES},
                time_rows=case_ids_for_timing,hist_amounts=dict(hist_amounts),history_rows=history_rows,buckets=dict(buckets),months=dict(months),rates={"reportados":rate("reportados","creados"),
                "confirmados":rate("confirmados","despachados")},coverage=coverage,states=dict(state_counts),settings=settings)


def revision(conn):
    result=conn.execute("""SELECT (SELECT max(id) FROM events), (SELECT max(id) FROM cases),
           (SELECT max(confirmed) FROM hist_books),(SELECT max(id) FROM rs_runs),
           (SELECT max(checked) FROM rs_runs),(SELECT max(finished) FROM rs_runs),
           (SELECT state FROM rs_runs ORDER BY id DESC LIMIT 1),
           (SELECT target_days FROM rs_settings),(SELECT calendar FROM rs_settings)""").fetchone()
    return hashlib.sha256(json.dumps(tuple(result)).encode()).hexdigest()


@bp.get("/api/indicadores/revision")
@require("administrador")
def revision_view():
    return jsonify(revision=revision(db()))


@bp.get("/api/pedidos/<int:oid>")
@require("nacional","administrador","calidad")
def linked_order(oid):
    where="o.id=?"
    if role()=="calidad":
        where+=" AND EXISTS(SELECT 1 FROM order_links k JOIN cases c ON c.id=k.case_id WHERE k.order_id=o.id AND c.submitted_at IS NOT NULL)"
    if role()=="nacional":
        where+=" AND o.channel='nacional'"
    r=db().execute("SELECT o.* FROM rs_orders o WHERE "+where,(oid,)).fetchone()
    if not r: abort(404,"Pedido no disponible.")
    entry=dict(r)
    entry["data"]=json_dict(entry["data"])
    return jsonify(entry)


def format_rate(value):
    return "Sin base suficiente" if value is None else f"{value:.2f}".rstrip("0").rstrip(".").replace(".",",")+" %"


@bp.get("/api/pedidos")
@require("nacional","administrador")
def lookup():
    term=request.args.get("q","").strip()
    try: page=max(1,int(request.args.get("page",1)))
    except ValueError: abort(400,"Página inválida.")
    # Exact identities only, with scope applied to both alternatives.
    if not term:
        return jsonify(items=[],total=0,page=page)
    sql=" FROM rs_orders WHERE active=1 AND (code=? OR number=?)"
    if role()=="nacional": sql+=" AND channel='nacional'"
    total=db().execute("SELECT count(*)"+sql,(term,term)).fetchone()[0]
    result=[]
    for row in db().execute("SELECT *"+sql+" ORDER BY id LIMIT 25 OFFSET ?",(term,term,25*(page-1))):
        o=dict(row); o["data"]=json_dict(o["data"])
        result.append(o)
    return jsonify(items=result,total=total,page=page)


@bp.get("/indicadores")
@bp.get("/api/indicadores")
@require("administrador")
def dashboard():
    args=request.args
    channel=args.get("canal","")
    origin=args.get("origen","")
    dim=args.get("dimension","client")
    query=args.get("buscar","")
    cohort=args.get("cohorte","creacion")
    if channel not in ("",*CHANNELS) or origin not in ("",*ORIGINS) or dim not in DIMS or cohort not in ("creacion","salida"):
        abort(400,"Filtro inválido.")
    try:
        a,b=window(args)
        page=max(1,int(args.get("pagina",1)))
        prev_end=a-timedelta(days=1)
        prev_start=prev_end-(b-a)
    except (ValueError,KeyError,OverflowError):
        abort(400,"Revisa los filtros de fecha y página.")
    conn=db()
    # One read snapshot keeps cards, detail and exports in agreement during concurrent writes.
    conn.execute("BEGIN")
    try:
        cur=aggregate(conn,a,b,channel,origin,dim,query,cohort)
        prev=aggregate(conn,prev_start,prev_end,channel,origin,dim,query,cohort)
        rev=revision(conn)
        runs=[dict(r) for r in conn.execute("SELECT * FROM rs_runs ORDER BY id DESC LIMIT 10")]
        last=conn.execute("SELECT max(finished) FROM rs_runs WHERE state='completa'").fetchone()[0]
    finally: conn.rollback()
    metric=args.get("detalle","cola_calidad")
    title=LABELS.get(metric)
    results=cur["rows"].get(metric,[])
    if metric in TIMES:
        title=TIMES[metric]; results=cur["time_rows"][metric]
    if metric in BALANCES:
        title=BALANCES[metric]+" · "+args.get("unidad","")
        results=cur["balance_details"][args.get("unidad",""),metric]
    historical_labels={"hist_quantity":"Cantidad histórica reclamada","hist_approved":"Marcado para reponer en el histórico",
                       "hist_undated":"Reposición histórica sin fecha declarada de despacho"}
    if metric in historical_labels:
        title=historical_labels[metric]+" · "+args.get("unidad","")
        results=cur["history_rows"][args.get("unidad",""),metric]
    if metric=="grupo":
        group=args.get("grupo","")
        ids=cur["buckets"].get(group,{"ids":set()})["ids"]
        results=[r for r in cur["rows"]["casos"] if r["id"] in ids]
        title=DIMS[dim]+" · "+group
    if not title: abort(400,"Detalle desconocido.")
    if args.get("estado_pedido") and metric=="creados":
        results=[r for r in results if r["state"]==args["estado_pedido"]]
    def link(**values):
        merged=args.to_dict()
        merged.pop("pagina",None)
        merged.pop("csv",None)
        if "detalle" in values: merged.pop("estado_pedido",None)
        merged.update(values)
        return "/indicadores?"+urlencode(merged)
    if args.get("csv")=="1":
        buf=io.StringIO()
        out=csv.writer(buf)
        out.writerow(["Indicador","Desde","Hasta","Identificador","Cliente","Canal","Fecha","Estado","Antigüedad (días desde registro)","Responsable","Siguiente acción","Importe o cantidad","Línea"])
        def safe_cell(s):
            return "'"+s if isinstance(s,str) and s.lstrip().startswith(("=","+","-","@")) else s
        for r in results:
            out.writerow([safe_cell(v) for v in (title,str(a),str(b),r["ref"],r["client"],r["channel"],r["date"],
                                                 r["state"],r.get("case_age",""),r.get("responsible",""),r.get("next",""),r.get("measure",""),r.get("line",""))])
        return Response("\ufeff"+buf.getvalue(),mimetype="text/csv; charset=utf-8",
                        headers={"Content-Disposition":"attachment; filename=indicadores-reclamos.csv"})
    if request.path.startswith("/api/"):
        return jsonify(desde=str(a),hasta=str(b),cuentas=cur["counts"],tasas=cur["rates"],cobertura=cur["coverage"],
                       detalle=metric,items=results[25*(page-1):25*page],total=len(results),pagina=page)
    groups=[]
    for k in set(cur['buckets'])|set(prev['buckets']):
        g=cur['buckets'].get(k,{"ids":set(),"units":{},"orders":set()})
        old=prev['buckets'].get(k,{"ids":set()})
        groups.append(dict(name=k,count=len(g["ids"]),delta=len(g["ids"])-len(old["ids"]),
                           units=g["units"],orders=len(g["orders"])))
    rank=args.get("orden","cambio")
    unit_rank=args.get("medida","")
    if rank=="expedientes": groups.sort(key=lambda g:(-g["count"],g["name"]))
    elif rank=="cantidad" and unit_rank: groups.sort(key=lambda g:(-g["units"].get(unit_rank,0),g["name"]))
    else: groups.sort(key=lambda g:(-abs(g["delta"]),-g["count"],g["name"]))
    month_windows={}
    for month in cur["months"]:
        start=date.fromisoformat(month+"-01")
        following=date(start.year+(start.month==12),start.month%12+1,1)
        month_windows[month]=dict(periodo="rango",desde=str(max(a,start)),hasta=str(min(b,following-timedelta(days=1))))
    group_page=args.get("grupos",1,type=int)
    group_page=max(1,group_page)
    return render_template("indicadores.html",data=cur,previous=prev,queues=QUEUES,labels=LABELS,origins=ORIGINS,dimensions=DIMS,
        timings=TIMES,amounts=BALANCES,a=a,b=b,prev_start=prev_start,prev_end=prev_end,link=link,rate=format_rate,
        group_rows=groups[50*(group_page-1):50*group_page],group_count=len(groups),group_page=group_page,month_windows=month_windows,scope=channel or "Todos",detail_title=title,page=page,
        items=results[(page-1)*25:page*25],total=len(results),runs=runs,last=last,adapter=bool(os.getenv("RECLAMOS_RSALES_ADAPTER")),
        revision=rev,today=today(),note=args.get("aviso",""),args=args)


@bp.post("/indicadores/objetivo")
@require("administrador")
def set_goal():
    try: days=int(request.form["dias"])
    except (ValueError,KeyError): abort(400,"Especifica el número de días.")
    cal=request.form.get("calendario")
    if days<1 or days>3650 or cal not in ("calendario","laboral"):
        abort(400,"Revisa el número de días o el calendario.")
    db().execute("UPDATE rs_settings SET target_days=?,calendar=? WHERE id=1",(days,cal))
    return redirect("/indicadores?aviso=Objetivo+guardado",303)


@bp.post("/indicadores/sincronizar")
@require("administrador")
def sync_view():
    try:
        plugin=load_adapter()
        run_sync(db(),plugin,request.form["desde"],request.form["hasta"])
        message="Corte completo disponible."
    except (KeyError,ValueError,ImportError):
        message="Revisa las fechas, el adaptador de lectura y el estado de la corrida."
    except Exception:
        message="Lectura interrumpida. Puedes reintentar la ventana."
    return redirect("/indicadores?"+urlencode({"aviso":message}),303)


@bp.cli.command("rsales-refresh")
@click.option("--dias",type=click.IntRange(1,366),default=7,show_default=True)
def scheduled(dias):
    """Sincroniza la ventana solapada y reconcilia los pedidos abiertos."""
    try: result=refresh(db(),load_adapter(),dias)
    except Exception as e:
        raise click.ClickException("Lectura incompleta. Revisa el adaptador y el historial de corridas ("+type(e).__name__+").") from None
    click.echo(f"{len(result)} ventanas completas.")
