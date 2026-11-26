"""Domain acceptance tests using isolated private sessions and a temporary database."""
import io
import secrets
import time
from uuid import uuid4

import pytest
from flask import Flask, g
from werkzeug.exceptions import HTTPException

import cases
from common import db, session_digest


@pytest.fixture
def team(tmp_path):
    app = Flask(__name__)
    app.config.update(TESTING=True,SECRET_KEY=secrets.token_bytes(32),DATABASE=tmp_path / "test.sqlite")
    app.register_blueprint(cases.bp)
    with app.app_context():
        conn=db()
        conn.executescript("CREATE TABLE accounts(name TEXT PRIMARY KEY,person TEXT); CREATE TABLE sessions(digest TEXT,account TEXT,expires INT);")
        cases.init_cases(conn)
        clients={}
        for user in cases.ROLES:
            token=secrets.token_urlsafe(20)
            conn.execute("INSERT INTO accounts VALUES(?,?)",(user,user.title()))
            conn.execute("INSERT INTO sessions VALUES(?,?,?)",(session_digest(token),user,time.time()+200))
            clients[user]=app.test_client()
            with clients[user].session_transaction() as sess:
                sess["sid"]=token
    @app.teardown_appcontext
    def close(_):
        if conn:=g.pop("db",None):
            conn.close()
    @app.errorhandler(HTTPException)
    def message(exc):
        return {"error":exc.description},exc.code
    return clients


def post(client,path,data,key=None,expected=200):
    r=client.post(path,json=data,headers={"Idempotency-Key":key or uuid4().hex})
    assert r.status_code==expected,r.data
    return r.json


def opening():
    return {"client":"Cliente Demo","date":"2026-11-24","description":"Aviso de prueba",
            "fields":{"documentos":["FO-A","FO-B"],"pais":"Ecuador"},
            "lines":[{"code":"verde","quantity":2,"unit":"caja","reason":"Daño","incidence":"daño físico"},
                     {"description":"rojo","quantity":1,"unit":"galón","reason":"Envase","incidence":"daño físico"}]}


def act(client,c,action,expect=200,key=None,**data):
    return post(client,f"/api/cases/{c['id']}/actions",dict(version=c["version"],action=action,**data),key,expect)


@pytest.mark.parametrize("area",cases.CHANNELS)
def test_full_case(team,area):
    source=team[area]
    other=team["nacional" if area=="internacional" else "internacional"]
    quality,admin=team["calidad"],team["administrador"]
    c=post(source,"/api/cases",opening())
    url=f"/api/cases/{c['id']}"
    assert c["origin"]==area+"_manual"
    assert quality.get(url).status_code==404
    assert other.get(url).status_code==404
    assert other.get("/api/cases?q=verde").json==[]
    upload={"version":str(c["version"]),"line_id":str(c["lines"][0]["id"]),
            "file":(io.BytesIO(b"Documento de prueba"),"soporte.txt")}
    up=source.post(url+"/files",data=upload,headers={"Idempotency-Key":uuid4().hex})
    assert up.status_code==200
    c=up.json
    fid=c["files"][0]["id"]
    assert other.get(url+f"/files/{fid}").status_code==404
    assert source.get(url+f"/files/{fid}").data==b"Documento de prueba"
    old=c
    key=uuid4().hex
    c=act(source,old,"submit",key=key)
    assert act(source,old,"submit",key=key)==c
    assert quality.get(url).json["status"]=="enviado"
    assert admin.get("/api/cases?q=verde").json[0]["id"]==c["id"]
    act(source,old,"submit",409)
    act(source,c,"ack",403)
    c=act(quality,c,"ack")
    l1,l2=[l["id"] for l in c["lines"]]
    c=act(quality,c,"request_info",note="Aclara el lote",line_id=l1)
    c=act(source,c,"provide_info",note="Se confirma el lote de bodega")
    c=act(source,c,"amend",note="Lote confirmado",line_id=l1,lot="DEMO-1")
    assert c["lines"][0]["lot"]=="DEMO-1"
    c=act(admin,c,"decision",line_id=l1,replacement=2,authorized_by="Gerencia",note="Apoyo anticipado")
    c=act(source,c,"decision",line_id=l2,rejected=1,authorized_by="Gerencia",note="Rechazo sustentado")
    movement=dict(line_id=l1,quantity=1,date=c["date"],support="S-001")
    c=act(source,c,"dispatch",**movement)
    before=c
    key=uuid4().hex
    c=act(source,c,"dispatch",key=key,**movement)
    assert act(source,before,"dispatch",key=key,**movement)["lines"][0]["dispatched"]==2
    act(source,c,"dispatch",409,**movement)
    c=act(source,c,"communicate",note="Explicó la solución",sender="Vendedor demo",medium="Teléfono",date=c["date"])
    assert c["label"]=="Cliente atendido · Calidad pendiente"
    findings=[dict(line_id=l1,finding="Daño",cause="Pendiente",result="Confirmado",confirmed=1)]
    technical=dict(report="Informe inicial",summary="Revisión completada",findings=findings)
    c=act(quality,c,"report_draft",**technical)
    assert not source.get(url).json["reports"]
    c=act(quality,c,"publish",**technical)
    assert c["status"]=="parcial"
    rid=c["reports"][-1]["id"]
    assert other.get(url+f"/letter/{rid}").status_code==404
    assert b"Informe de Calidad" in source.get(url+f"/letter/{rid}").data
    c=act(source,c,"report_read",report_id=rid)
    act(source,c,"close",409)
    findings[0].update(line_id=l2,confirmed=0)
    c=act(quality,c,"publish",**technical)
    assert c["status"]=="respondido"
    act(quality,c,"publish",400,**technical)
    c=act(quality,c,"publish",**technical,note="Precisión solicitada")
    c=act(source,c,"close")
    assert c["status"]=="cerrado"
    assert "submit" in [ev["action"] for ev in c["events"]]
    c=act(admin,c,"reopen",note="Reincidencia documentada")
    assert c["cycle"]==2 and c["status"]=="enviado"
    assert len(c["reports"])==4


def test_service_and_credit_resolution(team):
    source, quality=team["nacional"],team["calidad"]
    data=opening()
    data["submit"]=True
    data["lines"]=data["lines"][:1]
    c=post(source,"/api/cases",data)
    lid=c["lines"][0]["id"]
    c=act(quality,c,"ack")
    c=act(quality,c,"publish",report="Informe",summary="Resumen",findings=[dict(line_id=lid,finding="Análisis",result="Aceptada",confirmed=2)])
    act(source,c,"decision",400,line_id=lid,credit=3,note="NC",authorized_by="Responsable")
    c=act(source,c,"decision",line_id=lid,credit=2,note="NC",authorized_by="Responsable")
    c=act(source,c,"communicate",sender="Vendedora",medium="Correo",date=c["date"],note="Comunicada")
    act(source,c,"close",409)
    act(source,c,"resolve",400,line_id=lid,type="credit",quantity=2,date=c["date"])
    c=act(source,c,"resolve",line_id=lid,type="credit",quantity=2,date=c["date"],support="NC-1")
    assert act(source,c,"close")["status"]=="cerrado"
    data.update(kind="servicio",lines=[])
    svc=post(source,"/api/cases",data)
    assert svc["status"]=="enviado" and not svc["lines"]


def test_service_requires_communication_and_documented_commitment(team):
    source, quality=team["internacional"],team["calidad"]
    c=post(source,"/api/cases",dict(opening(),kind="servicio",lines=[],submit=True))
    c=act(quality,c,"ack")
    c=act(quality,c,"publish",report="Revisión de servicio",summary="Causa identificada",findings=[])
    comm=dict(sender="Distribuidor",medium="Correo",date=c["date"],note="Confirmación")
    c=act(source,c,"communicate",**comm)
    act(source,c,"close",409)
    c=act(source,c,"decision",type="credit",note="Costo logístico",authorized_by="Gerencia")
    assert source.get("/api/cases?status=por_verificar").json[0]["id"]==c["id"]
    c=act(source,c,"resolve",date=c["date"],support="NC-2")
    assert source.get("/api/cases?status=por_verificar").json==[]
    act(source,c,"close",409)
    c=act(source,c,"communicate",**comm)
    assert act(source,c,"close")["status"]=="cerrado"


def test_revision_and_concurrent_save(team):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    app=team["nacional"].application
    with app.test_request_context():
        g.identity={"name":"nacional"}
        parsed=dict(opening(),request_id="TEST-019")
        before=cases.save_contact(parsed,b"PDF original","test.pdf","instancia-prueba")
        retried=cases.save_contact(parsed,b"PDF original","test.pdf","instancia-prueba")
        assert retried["id"]==before["id"] and retried["version"]==before["version"]
        after=cases.save_contact(dict(parsed,client="Dato revisado"),b"PDF revisado","test.pdf","instancia-prueba")
        assert after["id"]==before["id"] and after["client"]==before["client"]
        assert len(after["files"])==2 and after["events"][-1]["payload"]["changes"]["client"]["actual"]=="Dato revisado"
    url=f"/api/cases/{before['id']}"
    source=team["nacional"]
    act(source,after,"submit",409)
    payload={k:v for k,v in after.items() if k in ("version","client","date","description","kind","fields","lines")}
    payload["fields"]["opcional"]=""
    assert source.put(url,json=dict(payload,client="Corrección"),headers={"Idempotency-Key":uuid4().hex}).status_code==400
    with source.session_transaction() as s:
        session_token=s["sid"]
    barrier=Barrier(2)
    def submit_review():
        client=app.test_client()
        with client.session_transaction() as sess:
            sess["sid"]=session_token
        barrier.wait()
        return client.put(url,json=payload,headers={"Idempotency-Key":uuid4().hex}).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures=[pool.submit(submit_review) for _ in range(2)]
        assert sorted(f.result() for f in futures)==[200,409]
    current=source.get(url).json
    assert act(source,current,"submit")["status"]=="enviado"


def test_report_attachment_published_by_version(team):
    source,quality=team["nacional"],team["calidad"]
    c=post(source,"/api/cases",dict(opening(),submit=True))
    c=act(quality,c,"ack")
    path=f"/api/cases/{c['id']}"
    def upload(c,kind,name):
        r=quality.post(path+"/files",data={"version":str(c["version"]),"kind":kind,
            "file":(io.BytesIO(b"Version "+name.encode()),name)},headers={"Idempotency-Key":uuid4().hex})
        assert r.status_code==200
        return r.json
    c=upload(c,"technical","informe.txt")
    fid=c["files"][-1]["id"]
    assert not source.get(path).json["files"]
    assert not any(e["action"]=="file" for e in source.get(path).json["events"])
    assert source.get(f"{path}/files/{fid}").status_code==404
    assert team["administrador"].get(f"{path}/files/{fid}").status_code==200
    c=act(quality,c,"report_draft",file_id=fid)
    assert c["reports"][0]["file_id"]==fid
    assert not source.get(path).json["reports"]
    assert source.get(f"{path}/files/{fid}").status_code==404
    findings=[dict(line_id=c["lines"][0]["id"],finding="Inspección",result="Confirmado",confirmed=1)]
    c=act(quality,c,"publish",file_id=fid,summary="Respuesta para el cliente",findings=findings)
    assert source.get(f"{path}/files/{fid}").data==b"Version informe.txt"
    assert source.get(path).json["reports"][-1]["file_id"]==fid
    assert team["internacional"].get(f"{path}/files/{fid}").status_code==404
    c=upload(c,"technical","revision.txt")
    assert len(source.get(path).json["files"])==1
    assert source.get(f"{path}/files/{c['files'][-1]['id']}").status_code==404
    c=upload(c,"evidence","evidencia.txt")
    assert source.get(f"{path}/files/{c['files'][-1]['id']}").data==b"Version evidencia.txt"


def test_optional_order_quantities_snapshot_and_international_amendment(team):
    import json

    app=team["nacional"].application
    with app.app_context():
        conn=db()
        conn.executescript("""CREATE TABLE rs_orders(id INTEGER PRIMARY KEY,active INT,type TEXT,channel TEXT,
                          number TEXT,code TEXT,data TEXT)""")
        conn.executemany("INSERT INTO rs_orders VALUES(?,?,?,?,?,?,?)",[
            (1,1,"P","nacional","A","Pedido-A",json.dumps({"line_count":3,"seller":"Demo"})),
            (2,1,"P","nacional","B","Pedido-B","{}"),(3,1,"P","internacional","C","Pedido-C","{}"),
            (4,0,"P","nacional","D","Pedido-D","{}"),(5,1,"F","nacional","E","Factura-E","{}")])
    c=post(team["nacional"],"/api/cases",dict(opening(),submit=True))
    params=dict(line_id=c["lines"][0]["id"],note="Soporte verificado",quantity=1)
    for oid in (3,4,5):
        act(team["nacional"],c,"link_order",404,order_id=oid,**params)
    for oid in (1,2):
        c=act(team["nacional"],c,"link_order",order_id=oid,**params)
    assert sum(l["quantity"] for l in c["order_links"])==2
    assert c["order_links"][0]["snapshot"]["data"]["line_count"]==3
    assert team["nacional"].get("/api/cases?q=Pedido-A").json[0]["id"]==c["id"]
    with app.app_context():
        db().execute("UPDATE rs_orders SET data='{}' WHERE id=1")
    assert team["nacional"].get(f"/api/cases/{c['id']}").json["order_links"][0]["snapshot"]["data"]["line_count"]==3
    params["quantity"]=2
    act(team["nacional"],c,"link_order",400,order_id=1,**params)
    i=post(team["internacional"],"/api/cases",dict(opening(),submit=True))
    act(team["internacional"],i,"link_order",403,order_id=1,**params)
    lid=i["lines"][0]["id"]
    i=act(team["internacional"],i,"amend",category="inspeccion",note="Inspección de prueba",warehouse="A",
          line_id=lid,unit="caja",date=i["date"],good_box=1,document="FO-A")
    i=act(team["internacional"],i,"amend",category="financiero",note="Liquidación de prueba",currency="USD",
          declared_amount=112.15,calculated_amount=111.15,document="FO-B",credit_stage="Solicitado")
    assert i["events"][-2]["payload"]["unit"]=="caja"
    assert i["events"][-1]["payload"]["declared_amount"]==112.15
    assert i["events"][-1]["payload"]["difference"]==1
    assert [l["unit"] for l in i["lines"]]==["caja","galón"]
    assert [r["channel"] for r in team["calidad"].get("/api/cases?sort=channel").json]==["internacional","nacional"]


def test_international_invoice_allocations_atomic_and_separate_from_units(team):
    source, admin, quality = team["internacional"], team["administrador"], team["calidad"]
    c=post(source,"/api/cases",dict(opening(),submit=True))
    first,second=[l["id"] for l in c["lines"]]
    c=act(admin,c,"decision",line_id=first,replacement=2,note="Gesto anticipado de prueba",authorized_by="Gerencia")
    assert c["label"]=="Cliente atendido · Calidad pendiente" and c["lines"][0]["dispatched"]==0
    assert quality.get("/api/cases").json[0]["status"]=="enviado"
    assert source.get("/api/cases?status=por_verificar").json[0]["id"]==c["id"]
    c=act(admin,c,"decision",line_id=second,credit=1,note="Nota acordada",authorized_by="Gerencia")
    base=dict(number="NC-001",currency="USD",date=c["date"],total="100.75")
    apps=[dict(type="factura",number="000A",amount="30.00"),dict(type="oc",number="OC-02",amount="25.50")]
    act(team["nacional"],c,"credit_apply",404,applications=apps,**base)
    act(quality,c,"credit_apply",403,applications=apps,**base)
    key=uuid4().hex
    initial=c
    c=act(source,initial,"credit_apply",key=key,applications=apps,**base)
    same=act(source,initial,"credit_apply",key=key,applications=apps,**base)
    assert len(same["credit_notes"][0]["applications"])==2
    assert c["credit_notes"][0]["remaining"]=="45.25"
    assert c["lines"][1]["credit_done"]==0
    before_version=c["version"]
    act(source,c,"credit_apply",409,applications=[dict(type="factura",number="000A",amount="46")],**base)
    act(source,c,"credit_apply",409,applications=apps,**dict(base,currency="COP"))
    last=[dict(type="factura",number="000B",amount="45.25")]
    act(source,c,"credit_apply",409,applications=last,covered_lines=[dict(line_id=second,quantity=2)],**base)
    assert source.get(f"/api/cases/{c['id']}").json["version"]==before_version
    c=act(source,c,"credit_apply",applications=last,covered_lines=[dict(line_id=second,quantity=1)],**base)
    assert c["credit_notes"][0]["remaining"]=="0" and len(c["credit_notes"][0]["applications"])==3
    assert c["lines"][1]["credit_done"]==1 and c["lines"][1]["confirmed"]==0
    assert "dispatch" not in [ev["action"] for ev in c["events"]]
    domestic=post(team["nacional"],"/api/cases",dict(opening(),submit=True))
    act(admin,domestic,"credit_apply",403,applications=apps,**base)


def test_edit_and_submit_draft_together(team):
    source,quality=team["nacional"],team["calidad"]
    c=post(source,"/api/cases",{"client":"Prueba"})
    url=f"/api/cases/{c['id']}"
    result=source.put(url,json=dict(version=c["version"],submit=True),headers={"Idempotency-Key":uuid4().hex})
    assert result.status_code==400
    assert source.get(url).json["client"]=="Prueba" and quality.get(url).status_code==404
    key=uuid4().hex
    data=dict(opening(),version=c["version"],submit=True)
    res=source.put(url,json=data,headers={"Idempotency-Key":key})
    assert res.status_code==200 and quality.get(url).json["status"]=="enviado"
    retry=source.put(url,json=data,headers={"Idempotency-Key":key})
    assert retry.json["version"]==res.json["version"]
    assert [e["action"] for e in retry.json["events"]]==["create","edit","submit"]


def test_attachments_must_belong_to_the_case_and_reference(team):
    source=team["internacional"]
    c=post(source,"/api/cases",dict(opening(),submit=True))
    first,second=c["lines"]
    url=f"/api/cases/{c['id']}/files"
    response=source.post(url,data={"version":str(c["version"]),"line_id":str(first["id"]),
        "file":(io.BytesIO(b"Inspeccion"),"inspeccion.txt")},headers={"Idempotency-Key":uuid4().hex})
    assert response.status_code==200
    c=response.json
    fid=c["files"][-1]["id"]
    other=post(source,"/api/cases",dict(opening(),submit=True))
    act(source,other,"amend",404,line_id=other["lines"][0]["id"],file_id=fid,note="Inspección")
    act(source,c,"amend",400,line_id=second["id"],file_id=fid,note="Inspección")
    c=act(source,c,"amend",line_id=first["id"],file_id=fid,note="Inspección")
    assert c["events"][-1]["payload"]["file_id"]==fid
