import json
import sqlite3
import unittest
from datetime import date

from cases import init_cases
from history import init_history
from metrics import aggregate
from sync_orders import init_orders, local_day, run_sync


class Reader:
    account="prueba"
    covered_channels=["nacional"]
    def __init__(self, fail=False):
        self.fail=fail
        self.version=1

    def estado_conexion(self):
        return {"solo_lectura":True}

    def listar_pedidos(self,tipo,desde,hasta,pagina,tamano):
        assert tipo=="P" and tamano==100
        if pagina==2 and self.fail: raise TimeoutError()
        if pagina==1:
            return {"pedidos":[{"type":"P","code":"otro","created_at":"2020-01-01"}],"pagina_siguiente":2}
        docs=[{"type":"P","code":"P"+str(i),"order_number":str(i),"canal_verificado":"nacional","state":"prueba",
               "created_at":"2026-10-05","version":self.version,"fecha_salida_verificada":"2026-10-06",
               "soporte_salida":"acta" if i<80 else None} for i in range(100)]
        return {"pedidos":docs+[docs[0]],"pagina_siguiente":None,"periodo_completo":True}


class MetricsTest(unittest.TestCase):
    def setUp(self):
        self.c=sqlite3.connect(":memory:",isolation_level=None)
        self.c.row_factory=sqlite3.Row
        init_cases(self.c)
        init_orders(self.c)
        init_history(self.c)
        self.a,self.b=date(2026,10,1),date(2026,10,31)
        self.reader=Reader()

    def sync(self):
        return run_sync(self.c,self.reader,str(self.a),str(self.b))

    def agg(self):
        return aggregate(self.c,self.a,self.b,"nacional")

    def claim(self,number,order,qty,confirmed,created="2026-10-10T15:00:00Z"):
        c=self.c.execute("""INSERT INTO cases(channel,origin,client,status,created_at,updated_at,submitted_at)
             VALUES('nacional','nacional_manual','Cliente de prueba','analisis',?,?,?)""",(created,created,created)).lastrowid
        line=self.c.execute("""INSERT INTO case_lines(case_id,code,description,quantity,unit,lot,presentation,fields,incidence,reason,
                 confirmed,replacement,dispatched) VALUES(?,'Ref','Referencia',?,'unidad','','','{}','daño físico','prueba',?,1,0)""",
                (c,qty,confirmed)).lastrowid
        self.c.execute("""INSERT INTO events(case_id,actor,channel,created_at,version,cycle,action,payload)
                     VALUES(?,'nacional','nacional',?,1,1,'submit','{}')""",(c,created))
        if order is not None:
            oid=self.c.execute("SELECT id FROM rs_orders WHERE code=?",("P"+str(order),)).fetchone()[0]
            self.c.execute("INSERT INTO order_links VALUES(?,?,?,?)",(c,line,oid,qty))
        return c,line

    def test_resume_publication_and_atomic_last_cut(self):
        self.reader.fail=True
        with self.assertRaises(TimeoutError): self.sync()
        r=self.c.execute("SELECT * FROM rs_runs").fetchone()
        self.assertEqual((r["state"],r["cursor"],r["pages"]),("parcial","2",1))
        self.assertFalse(self.agg()['coverage']['nacional'])
        self.reader.fail=False
        self.assertEqual(self.sync(),r["id"])
        previous=[tuple(x) for x in self.c.execute("SELECT id,code,data FROM rs_orders ORDER BY id")]
        self.assertEqual(len(previous),100)
        self.reader.version=2
        self.reader.fail=True
        with self.assertRaises(TimeoutError): self.sync()
        self.assertEqual(previous,[tuple(x) for x in self.c.execute("SELECT id,code,data FROM rs_orders ORDER BY id")])
        self.reader.fail=False
        self.sync()
        new=[tuple(x) for x in self.c.execute("SELECT id,code,data FROM rs_orders ORDER BY id")]
        self.assertEqual([r[:2] for r in new],[r[:2] for r in previous])
        self.assertEqual(json.loads(new[0][2])["version"],2)

    def test_rates_late_claim_mixed_and_missing_denominator(self):
        self.sync()
        for i in [0,1,2,0,1]:
            self.claim(i,i,2,1)
        self.claim(3,3,2,0,created="2026-11-03T05:30:00Z")
        self.claim(100,None,2,1)
        g=self.agg()
        self.assertEqual((g['counts']['creados'],g['counts']['despachados']),(100,80))
        self.assertEqual(g["rates"],{"reportados":4.0,"confirmados":3.75})
        self.assertEqual((g['counts']['sin_vinculo'],g['counts']['tardios']),(1,1))
        self.assertEqual(g['balances']['unidad']['quantity'],12)
        self.assertEqual(g['counts']['reponer'],6)
        self.assertIsNone(aggregate(self.c,self.a,self.b,"internacional")['rates']['reportados'])
        self.assertIsNone(aggregate(self.c,self.a,self.b,"nacional",dim="code",query="Ref")['rates']['reportados'])
        self.assertEqual(local_day("2026-11-01T02:00:00Z"),self.b)

    def test_quality_cohort_at_historic_cut(self):
        c,l=self.claim(0,None,3,0)
        for i,action,status,dt in [(2,"ack","","2026-10-15"),(3,"publish","respondido","2026-10-25"),
                                    (4,"communicate","","2026-11-01")]:
            self.c.execute("INSERT INTO events(case_id,actor,channel,created_at,version,cycle,action,payload) VALUES(?,'calidad','nacional',?,?,1,?,?)",
                         (c,dt,i,action,json.dumps({"line_ids":[l],"status":status})))
        g=self.agg()
        self.assertEqual([g['counts'][k] for k in ('enviados','completos','pendientes','comunicar')],[1,1,0,1])
        self.assertEqual(g['times']['complete']['median'],15)

if __name__=="__main__":
    unittest.main()


def test_inbox_precedes_month_and_drafts_do_not_publish():
    t=MetricsTest()
    t.setUp()
    c,l=t.claim(0,None,3,0,created="2026-09-02")
    t.c.execute("UPDATE cases SET status='informacion' WHERE id=?",(c,))
    c2,l2=t.claim(0,None,4,0)
    t.c.execute("UPDATE cases SET status='borrador',submitted_at=NULL WHERE id=?",(c2,))
    t.c.execute("DELETE FROM events WHERE case_id=?",(c2,))
    g=t.agg()
    assert g["counts"]["cola_calidad"]==g["counts"]["cola_info"]==1
    assert g["counts"]["enviados"]==g["counts"]["publicados"]==g["counts"]["atendidos"]==0
    assert g["counts"]["casos"]==1 and g["counts"]["cola_reponer"]==1
    t.c.close()


def test_live_board_refresh_preserves_filter(tmp_path):
    """Browser acceptance when Playwright and system Chromium are available on the machine."""
    import secrets
    import shutil
    import threading
    import pytest
    from werkzeug.serving import make_server
    from auth import bootstrap
    from app import create_app
    from common import db
    pw=pytest.importorskip("playwright.sync_api")
    chromium=shutil.which("chromium")
    if not chromium:
        pytest.skip("El navegador Chromium no está instalado.")
    app=create_app({"DATA_DIR":tmp_path,"TESTING":True})
    secret=secrets.token_urlsafe(16)
    with app.app_context():
        bootstrap({"RECLAMOS_PIN_"+k:secret for k in ("ADMIN","INTERNACIONAL","NACIONAL")})
    server=make_server("127.0.0.1",0,app,threaded=True)
    thread=threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    try:
        with pw.sync_playwright() as p:
            browser=p.chromium.launch(executable_path=chromium)
            page=browser.new_page(viewport={"width":390,"height":844})
            page.goto(f"http://127.0.0.1:{server.server_port}/indicadores")
            page.locator("#username").fill("administrador")
            page.locator("#password").fill(secret)
            page.get_by_role("button",name="Ingresar").click()
            page.goto(f"http://127.0.0.1:{server.server_port}/indicadores")
            for width in (360,390,430,768,1440):
                page.set_viewport_size({"width":width,"height":900})
                nav=page.locator(".navigation").bounding_box()
                main=page.locator("main").bounding_box()
                assert (nav["x"]+nav["width"]<=main["x"]+1 if width>820 else nav["y"]+nav["height"]<=main["y"]+1)
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), width
            page.set_viewport_size({"width":390,"height":844})
            assert all(label=="Sin base suficiente" for label in page.locator(".missing-base").all_text_contents())
            assert page.locator(".missing-base").count()>=2
            first=page.locator("a[href*='detalle=cola_calidad'] strong")
            pw.expect(first).to_have_text("0")
            page.locator("input[name=buscar]").fill("texto en edición")
            with app.app_context():
                helper=MetricsTest()
                helper.c=db()
                helper.claim(0,None,1,0,created="2026-01-10")
            pw.expect(first).to_have_text("1",timeout=12000)
            assert page.locator("input[name=buscar]").input_value()=="texto en edición"
            assert page.locator("input[name=buscar]").evaluate("e => e===document.activeElement")
            browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=3)


def test_admin_five_queues_link_exact_cases_and_actions(tmp_path):
    import secrets
    from uuid import uuid4
    from app import create_app
    from auth import bootstrap
    from test_access import login, csrf
    from test_cases import opening

    app=create_app({"DATA_DIR":tmp_path,"TESTING":True})
    passwords={name:secrets.token_urlsafe(16) for name in ("administrador","calidad","nacional","internacional")}
    with app.app_context():
        bootstrap({"RECLAMOS_PIN_ADMIN":passwords["administrador"],
                   "RECLAMOS_PIN_NACIONAL":passwords["nacional"],
                   "RECLAMOS_PIN_INTERNACIONAL":passwords["internacional"]})
    clients={name:app.test_client() for name in passwords}
    admin=clients["administrador"]
    login(admin,"administrador",passwords["administrador"])
    assert admin.post("/accesos",data={"account":"calidad","password":passwords["calidad"],
                                       "csrf_token":csrf(admin,"/accesos")}).status_code==200
    for name in ("nacional","internacional","calidad"):
        assert login(clients[name],name,passwords[name]).status_code==302
        assert clients[name].get("/").status_code==200
        assert clients[name].get("/api/indicadores").status_code==403

    def post(name,path,body):
        client=clients[name]
        with client.session_transaction() as session: token=session["csrf"]
        r=client.post(path,json=body,headers={"Idempotency-Key":uuid4().hex,"X-CSRF-Token":token})
        assert r.status_code==200,r.data
        return r.json
    def act(name,c,action,**fields):
        return post(name,f"/api/cases/{c['id']}/actions",dict(action=action,version=c["version"],**fields))
    def queue(key,expected,person=None,**filters):
        r=admin.get("/api/indicadores",query_string=dict(detalle=key,**filters))
        assert r.status_code==200
        result=r.json
        assert result["cuentas"][key]==result["total"]==len(expected)
        assert {i["id"] for i in result["items"]}==set(expected)
        for i in result["items"]:
            assert i["case_age"]>=0 and i["next"]
            assert i["url"]==f"/#caso/{i['id']}"
            if person: assert i["responsible"]==person
    board=admin.get("/indicadores").get_data(as_text=True)
    for title in ("Borradores sin enviar","En Calidad sin respuesta completa","Falta información del área",
                  "Respuestas por comunicar","Reposiciones por verificar"):
        assert f"<span>{title}</span>" in board
    a=post("nacional","/api/cases",opening())
    b=post("internacional","/api/cases",opening())
    queue("cola_borradores",[a["id"],b["id"]])
    queue("cola_borradores",[b["id"]],"Angélica",canal="internacional")
    a=act("nacional",a,"submit")
    queue("cola_borradores",[b["id"]],"Angélica")
    queue("cola_calidad",[a["id"]],"Juan Diego")
    a=act("calidad",a,"ack")
    lid=a["lines"][0]["id"]
    a=act("calidad",a,"request_info",note="Confirma lote",line_id=lid)
    queue("cola_info",[a["id"]],"Zulay")
    a=act("nacional",a,"provide_info",note="Lote verificado")
    queue("cola_info",[])
    a=act("administrador",a,"decision",line_id=lid,replacement=2,authorized_by="Gerencia",note="Adelanto")
    queue("cola_reponer",[a["id"]],"Zulay")
    a=act("calidad",a,"publish",report="Primer análisis",summary="Confirmada una referencia",
          findings=[dict(line_id=lid,finding="Envase roto",result="Confirmado",confirmed=1)])
    queue("cola_calidad",[a["id"]],"Juan Diego")
    queue("cola_comunicar",[a["id"]],"Zulay")
    r=a["reports"][-1]
    a=act("nacional",a,"communicate",note="Se explicó",medium="Teléfono",sender="Vendedor",
          date=a["date"],report_id=r["id"])
    queue("cola_comunicar",[])
    for qty in (1,1):
        a=act("nacional",a,"dispatch",line_id=lid,quantity=qty,date=a["date"],support="Guía de prueba")
        queue("cola_reponer",[a["id"]] if a["lines"][0]["dispatched"]==1 else [])
