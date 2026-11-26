"""Check search isolation, atomic refresh, and the private PDF import contract."""
import hashlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from flask import Flask, g, session

import cases
import catalog
from common import db


class CatalogTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        directory = Path(self.temp.name)
        self.data = directory / "cut"
        self.data.mkdir()
        self.app = Flask(__name__)
        self.app.config.update(TESTING=True, DATABASE=str(directory/"db.sqlite"), SECRET_KEY=os.urandom(32))
        self.app.register_blueprint(catalog.bp)
        self.app.register_blueprint(cases.bp)
        @self.app.before_request
        def test_session():
            g.identity = {"name":session["test_role"]} if "test_role" in session else None
        @self.app.teardown_appcontext
        def close(_):
            conn = g.pop("db", None)
            if conn:
                conn.close()
        with self.app.app_context():
            cases.init_cases(db())
            catalog.init_catalog(db())
        self.client = self.app.test_client()
        self.login("nacional")

    def login(self,role):
        with self.client.session_transaction() as s:
            s.update(test_role=role,sid=os.urandom(16).hex())

    def write_cut(self):
        def product(code, state, label):
            return dict(code=code, description="Motor " + (f"[{label}]" if label else ""),
                        reference=None, group_code="MOTOR", family_code="MOTOR", line_code="MOTOR",
                        tax=0,state=state,presentacion_etiquetada=label)
        national = [dict(code="01",name="Prueba",pais_confirmado=None,city="Bogotá"),
                    dict(code="02",name="Sucursal",pais_confirmado="Colombia",city="Bogotá")]
        exports = [dict(id="01",razon_social="Abastece",pais="Ecuador",identificacion_fiscal="000ABC",alias=["TEST"])]
        meta = dict(fecha="2026-01-01",
                    productos=dict(codigos=2,estado={"Y":1,"N":1},presentacion={"UNIDAD":1,"Sin etiqueta":1}),
                    clientes_rsales=dict(codigos=2,colombia_explicito=1),
                    directorio_internacional=dict(clientes=1,paises=1),
                    catalogo_complementario=dict(fichas=0))
        for name,obj in (("productos_rsales",[product("01M","Y","UNIDAD"),product("09D","N",None)]),
                         ("clientes_rsales",national),("clientes_internacionales",exports),
                         ("control_de_fuentes",meta),("presentaciones_catalogo",[]),
                         ("codigos_documentados_internacional",[])):
            (self.data / (name+".json")).write_text(json.dumps(obj),encoding="utf-8")
        with self.app.app_context():
            catalog.import_catalog(db(),self.data)

    def test_scopes_and_source(self):
        self.write_cut()
        rows = self.client.get("/api/catalog/products").get_json()
        self.assertEqual(rows["total"],1)
        self.assertEqual(rows["items"][0]["presentacion_etiquetada"],"UNIDAD")
        self.assertEqual(self.client.get("/api/catalog/products?inactive=1").get_json()["total"],2)
        self.assertEqual(self.client.get("/api/catalog/clients").get_json()["total"],1)
        rows = self.client.get("/api/catalog/clients?q=01&unverified=1").get_json()["items"]
        self.assertEqual(rows[0]["warning"], "País por verificar")
        self.login("internacional")
        self.assertEqual(self.client.get("/api/catalog/clients?channel=nacional").status_code,403)
        rows = self.client.get("/api/catalog/clients?q=test").get_json()["items"]
        self.assertEqual(rows[0]["tax_id"],"000ABC")
        self.assertEqual(self.client.post("/api/contacto/preview").status_code,403)

    def test_verified_profile_and_versioned_equivalence(self):
        self.write_cut()
        response=self.client.put("/api/catalog/clients/01/profile",json={
            "profile_version":0,"country":"Colombia","source_note":"Documento de prueba"})
        self.assertEqual(response.status_code,200)
        item=self.client.get("/api/catalog/clients?q=01").get_json()["items"][0]
        self.assertEqual(item["country"],"Colombia")
        self.assertIsNone(item["raw"]["pais_confirmado"])
        self.assertEqual(item["source_note"],"Documento de prueba")
        link=dict(client_id="01",reported_code="PROV",presentation="Unidad",sku="01M",reason="Ficha de prueba")
        self.assertEqual(self.client.post("/api/catalog/equivalences",json=link).status_code,201)
        self.assertEqual(self.client.post("/api/catalog/equivalences",json=link).status_code,409)
        self.assertEqual(self.client.post("/api/catalog/equivalences",json=dict(link,version=1,reason="Cotejo")).status_code,201)
        self.assertEqual(len(self.client.get("/api/catalog/equivalences?client_id=01").get_json()["items"]),2)
        self.login("internacional")
        self.assertEqual(self.client.get("/api/catalog/equivalences?client_id=01").get_json()["items"],[])

    def test_invalid_refresh_preserves_directory(self):
        self.write_cut()
        path = self.data / "productos_rsales.json"
        products = json.loads(path.read_text())
        products[1]["code"] = products[0]["code"]
        path.write_text(json.dumps(products))
        with self.app.app_context(), self.assertRaises(sqlite3.IntegrityError):
            catalog.import_catalog(db(), self.data)
        self.assertEqual(self.client.get("/api/catalog/products?inactive=1").get_json()["total"],2)

    def test_authorized_master_refresh(self):
        from sync_catalog import refresh_masters
        self.write_cut()
        class Adapter:
            account = "qa"
            broken = False

            def estado_conexion(self):
                return {"solo_lectura":True}

            def buscar_productos(self, pagina, tamano_pagina):
                product = dict(code="Q"+str(pagina), description="Prueba [CAJA]", state="Y",
                               reference=None, group_code="", line_code="", family_code="", tax=0)
                return dict(items=[product],pagina_siguiente=2 if pagina==1 else None,
                            maestro_completo=(pagina==2), total_unicos=2)

            def buscar_clientes(self, pagina, tamano_pagina):
                if self.broken:
                    raise ConnectionError()
                return dict(items=[dict(code="n1",name="Distribuidora de prueba",country="Colombia")],
                            pagina_siguiente=None,maestro_completo=True,total_unicos=1)
        adapter = Adapter()
        with self.app.app_context():
            conn = db()
            adapter.broken=True
            with self.assertRaises(ConnectionError):
                refresh_masters(conn,adapter,1)
            self.assertIsNotNone(catalog.product_snapshot(conn,"01M"))
            adapter.broken=False
            refresh_masters(conn,adapter,1)
            self.assertIsNone(catalog.product_snapshot(conn,"01M"))
            self.assertEqual(catalog.product_snapshot(conn,"Q2")["presentacion_etiquetada"],"CAJA")
            self.assertEqual(conn.execute("SELECT count(*) FROM catalog_clients WHERE channel='internacional'").fetchone()[0],1)
            self.assertEqual([row[0] for row in conn.execute("SELECT state FROM catalog_runs ORDER BY id")],
                             ["parcial","completa"])

    def preview(self, data):
        response = self.client.post("/api/contacto/preview",data={"file":(io.BytesIO(data),"contacto.pdf")})
        self.assertEqual(response.status_code,200,response.get_data())
        body = response.get_json()
        response.request.input_stream.close()
        response.close()
        return body

    def test_manual_original_and_private_preview(self):
        raw = b"%PDF-1.5\nNeeds manual review\n"
        p = self.preview(raw)
        self.assertTrue(p["warning"])
        saved = self.client.post("/api/contacto/import",json={"token":p["token"]})
        self.assertEqual(saved.status_code,200,saved.get_data())
        cid = saved.get_json()["id"]
        self.assertEqual(self.preview(raw)["existing_case"],cid)
        self.assertEqual(self.client.post("/api/contacto/import",json={"token":p["token"]}).get_json()["id"],cid)
        with self.app.app_context():
            self.assertEqual(db().execute("SELECT content FROM case_files").fetchone()[0],raw)
            self.assertEqual(db().execute("SELECT count(*) FROM cases").fetchone()[0],1)
        self.login("nacional")
        self.assertEqual(self.client.post("/api/contacto/import",json={"token":p["token"]}).status_code,404)

    @unittest.skipUnless(os.environ.get("RECLAMOS_PRIVATE_FIXTURES"),"Requiere los ejemplos privados")
    def test_real_cut_and_contact(self):
        base = Path(os.environ["RECLAMOS_PRIVATE_FIXTURES"])
        with self.app.app_context():
            catalog.import_catalog(db(),base/"datos_privados")
        products = self.client.get("/api/catalog/products?inactive=1").get_json()
        self.assertEqual(products["total"],1398)
        self.assertEqual(self.client.get("/api/catalog/clients?unverified=1").get_json()["total"],8822)
        self.assertEqual(self.client.get("/api/catalog/clients").get_json()["total"],6682)
        sku = self.client.get("/api/catalog/products?q=4416M").get_json()["items"][0]
        self.assertEqual(sku["presentacion_etiquetada"], "UNIDAD")
        self.assertEqual(self.client.get("/api/catalog/cards").get_json()["total"],38)
        raw = (base / "ejemplos/CONTACTO_019.pdf").read_bytes()
        p = self.preview(raw)
        parsed = p["parsed"]
        self.assertEqual(parsed["numero_contacto"],"19")
        self.assertEqual(parsed["paginas"],7)
        self.assertEqual([l["codigo_origen"] for l in parsed["lineas"]],["3601","3600","3584"])
        self.assertEqual([int(l["cantidad"]) for l in parsed["lineas"]],[22,7,3])
        self.assertEqual([len(l["fotos"]) for l in parsed["lineas"]],[3,3,3])
        self.assertEqual(list(parsed["calidad_fuente"].values()),[None]*6)
        cid = self.client.post("/api/contacto/import",json={"token":p["token"]}).get_json()["id"]
        self.assertEqual(cid,self.client.post("/api/contacto/import",json={"token":p["token"]}).get_json()["id"])
        with self.app.app_context():
            self.assertEqual(db().execute("SELECT count(*) FROM case_files").fetchone()[0],1)
            self.assertEqual(db().execute("SELECT digest FROM case_files").fetchone()[0],hashlib.sha256(raw).hexdigest())
        self.login("internacional")
        clients = json.loads((base/"datos_privados/clientes_internacionales.json").read_text())
        codes = json.loads((base/"datos_privados/codigos_documentados_internacional.json").read_text())
        owner = next(c for c in clients if c["id"] == codes[0]["cliente_id"])
        pres = self.client.get("/api/catalog/clients",query_string={"q":owner["alias"][0]}).get_json()["items"][0]
        self.assertEqual(pres["id"],owner["id"])
        self.assertEqual(self.client.get("/api/catalog/codes",query_string={"client_id":pres["id"]}).get_json()["total"],6)
        padded = next(c for c in clients if c["identificacion_fiscal"].startswith("0"))
        found = self.client.get("/api/catalog/clients",query_string={"q":padded["identificacion_fiscal"]}).get_json()["items"]
        self.assertTrue(any(c["tax_id"] == padded["identificacion_fiscal"] for c in found))
        repeated = next(c for c in clients if c["identificacion_por_verificar"])
        duplicated = self.client.get("/api/catalog/clients",query_string={"q":repeated["identificacion_fiscal"]}).get_json()["items"]
        self.assertEqual(len(duplicated),2)
        self.assertTrue(all(d["warning"] for d in duplicated))


if __name__ == "__main__":
    unittest.main()
