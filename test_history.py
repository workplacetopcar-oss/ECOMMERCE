import sqlite3
import unittest
from datetime import date
from io import BytesIO
from unittest.mock import patch
from zipfile import ZipFile
from xml.sax.saxutils import quoteattr

from flask import Flask
from openpyxl import Workbook

import history


COLUMNS = ["NÚMERO de CASO", "Cliénté", "Fecha de   reclamo", "Código histórico",
           "Cantidad reclamada", "Decisión comercial", "Fecha despacho", "Soporte", "Fecha informe"]


def workbook(rows, filename="Seguimiento 2026", extras=False):
    wb = Workbook()
    sheet = wb.active
    sheet.title = filename
    sheet.append(["Corte de prueba"])
    sheet.append(COLUMNS)
    for row in rows:
        sheet.append(row)
    if extras:
        ws = wb.create_sheet("Indicadores")
        ws.append(COLUMNS)
        ws.append(rows[0])
        ws = wb.create_sheet("Segui 2025 EXP")
        ws.append(COLUMNS)
        ws.append([1, "Distribuidor de prueba", date(2025,4,12), "X", 12, "", "", "NC"])
    f = BytesIO()
    wb.save(f)
    return f.getvalue()


def snapshot_rows():
    rows = [[56,"Cliente A",date(2026,1,5),"R"+str(i),qty,"Reponer"]
            for i,qty in enumerate([50,30,20,10,8,4,3])]
    return rows + [
        [98,"Cliente B",date(2026,2,1),"B1",86,"No reponer"],
        [98,"Cliente B",date(2026,2,1),"B2",24,"Reponer"],
        [105,"Cliente C",date(2026,3,1),"C1",54,"Reponer",date(2026,3,15),"4154"],
        [105,"Cliente C",date(2026,3,1),"C2",12,"Reponer","","",date(2006,3,1)]
    ]


class HistoricalImport(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:", isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        history.init_history(self.conn)

    def tearDown(self):
        self.conn.close()

    def stage(self, book, options=None):
        return history.stage(self.conn, book, "prueba.xlsx", "administrador", "2026-11-26T17:00:00Z", options)

    def test_por_linea_y_canal(self):
        bid=self.stage(workbook(snapshot_rows(),extras=True))
        preview=history.inspect(self.conn,bid)
        self.assertTrue(any("anterior" in w for r in preview for w in r["warnings"]))
        self.assertTrue(any("soporte declarado" in w for r in preview for w in r["warnings"]))
        self.assertFalse(any(r["blocked"] for r in preview))
        history.commit_book(self.conn,bid,"2026-11-26T17:03:00Z")
        result=self.conn.execute("""SELECT number,count(*),sum(quantity),sum(approved),
            sum(CASE WHEN dispatch_date is null THEN approved ELSE 0 END)
            FROM history_records WHERE channel='nacional' GROUP BY number ORDER BY CAST(number AS INT)""")
        self.assertEqual([tuple(r) for r in result],[("56",7,125,125,125),("98",2,110,24,24),("105",2,66,66,12)])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM hist_cases").fetchone()[0],4)

    def test_reimportacion_cambio_y_concurrencia(self):
        rows=snapshot_rows()
        original=workbook(rows)
        first=self.stage(original)
        history.commit_book(self.conn,first,"t1")
        self.assertEqual(self.stage(original),first)
        rows[-1][4]=13
        second=self.stage(workbook(rows))
        changed=[r for r in history.inspect(self.conn,second) if r["blocked"]]
        self.assertEqual(len(changed),1)
        r=changed[0]
        with self.assertRaisesRegex(ValueError,"pendientes"):
            history.commit_book(self.conn,second,"t2")
        self.conn.execute("UPDATE hist_rows SET mode='update',target=?,reason=? WHERE id=?",
                          (r["candidates"][0],"Cifra contrastada",r["id"]))
        third=self.stage(workbook(rows+[[400,"Prueba",date(2026,2,1),"X",1]]))
        history.commit_book(self.conn,second,"t2")
        history.commit_book(self.conn,second,"t2")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM hist_lines").fetchone()[0],11)
        self.assertEqual(self.conn.execute("SELECT max(quantity) FROM history_records WHERE code='C2'").fetchone()[0],13)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM hist_rows WHERE applied_to=?",r["candidates"]).fetchone()[0],2)
        with self.assertRaisesRegex(ValueError,"Otra importación"):
            history.commit_book(self.conn,third,"t3")

    def test_numero_conciliado_y_reutilizado(self):
        rows=[[107,"Prueba",date(2026,1,1),"A",8,"","","S"],
              [7,"Prueba",date(2026,1,1),"B",1,"","","S"],
              [96,"Otra",date(2025,3,1),"X",2,"","","Z1"],
              [96,"Otra",date(2025,5,1),"X",3,"","","Z2"]]
        bid=self.stage(workbook(rows))
        records=history.inspect(self.conn,bid)
        self.assertTrue(all(r["blocked"] for r in records))
        self.conn.execute("UPDATE hist_rows SET mode='keep',reason='Soporte conciliado' WHERE book=?",(bid,))
        self.conn.execute("UPDATE hist_rows SET corrected='107' WHERE id=?",(records[1]["id"],))
        history.commit_book(self.conn,bid,"t1")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM hist_cases").fetchone()[0],3)
        self.assertEqual(self.conn.execute("SELECT sum(quantity) FROM history_records WHERE number='107'").fetchone()[0],9)
        self.assertEqual(history.inspect(self.conn,bid)[1]["data"]["number"],"7")

    def test_mapeo_explicito_y_errores(self):
        rows=[[1,"Cliente","fecha dudosa","A",-3], [2,"Otro",date(2026,2,2),"X",0,"Anulado"]]
        b=workbook(rows,filename="Marzo")
        with self.assertRaisesRegex(ValueError,"selecciona explícitamente"):
            self.stage(b)
        bid=self.stage(b,{"sheet":"Marzo","header":2,"channel":"internacional","map":{"number":1}})
        records=history.inspect(self.conn,bid)
        self.assertTrue(records[0]["errors"])
        self.assertTrue(records[1]["data"]["cancelled"])
        with self.assertRaisesRegex(ValueError,"pendientes"):
            history.commit_book(self.conn,bid,"t1")
        self.conn.execute("UPDATE hist_rows SET mode='skip',reason='Fila ilegible' WHERE id=?",(records[0]["id"],))
        history.commit_book(self.conn,bid,"t1")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM hist_lines").fetchone()[0],1)

    def test_copia_dinamica_explicita_sin_duplicar_hojas(self):
        f=BytesIO(workbook(snapshot_rows(),extras=True))
        path="xl/pivotCache/"
        with ZipFile(f,"a") as zip_file:
            fields="".join(f'<cacheField name={quoteattr(name)}><sharedItems/></cacheField>' for name in COLUMNS)
            fields=fields.replace('<cacheField name="Cliénté"><sharedItems/>',
                      '<cacheField name="Cliénté"><sharedItems><s v="Histórico"/></sharedItems>')
            zip_file.writestr(path+"pivotCacheDefinition1.xml",
                '<pivotCacheDefinition xmlns:r="urn:relations" r:id="r1"><cacheSource type="worksheet"/>'
                f'<cacheFields>{fields}</cacheFields></pivotCacheDefinition>')
            zip_file.writestr(path+"_rels/pivotCacheDefinition1.xml.rels",
                '<Relationships><Relationship Id="r1" Type="urn:t/pivotCacheRecords" Target="pivotCacheRecords1.xml"/></Relationships>')
            zip_file.writestr(path+"pivotCacheRecords1.xml",'<pivotCacheRecords><r><n v="96"/><x v="0"/>'
                '<d v="2025-03-19T00:00:00"/><s v="AB"/><n v="6"/><s v="reponer"/><m/><s v="NC"/></r></pivotCacheRecords>')
        buf=f.getvalue()
        bid=self.stage(buf)
        self.assertIn("Copia dinámica 1",self.conn.execute("SELECT notes FROM hist_books").fetchone()[0])
        self.assertEqual(len(history.inspect(self.conn,bid)),12)
        self.assertEqual(self.stage(buf,{"sheet":"Copia dinámica 1","header":1,"channel":"nacional"}),bid)
        rows=history.inspect(self.conn,bid)
        self.assertEqual(len(rows),13)
        self.assertEqual(rows[-1]["data"]["client"],"Histórico")
        self.assertEqual(rows[-1]["data"]["date"],"2025-03-19")
        self.assertIsNone(rows[-1]["data"]["dispatch"])
        history.commit_book(self.conn,bid,"t1")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM hist_lines").fetchone()[0],13)

    def test_celdas_combinadas_explicitas(self):
        wb=Workbook()
        w=wb.active
        w.title="Seguimiento 2026"
        w.append(COLUMNS)
        w.append([17,"Cliente",date(2026,1,1),"A",10])
        w.append([None,None,None,"B",8])
        for col in ("A","B","C"):
            w.merge_cells(f"{col}2:{col}3")
        f=BytesIO()
        wb.save(f)
        bid=self.stage(f.getvalue())
        row=history.inspect(self.conn,bid)[1]
        self.assertFalse(row["blocked"])
        self.assertTrue(any("celda combinada" in k for k in row["source"]))

    def test_interfaz_privada_y_descarga(self):
        app=Flask(__name__)
        app.jinja_env.globals["csrf_token"]=lambda:"token"
        app.register_blueprint(history.bp)
        c=app.test_client()
        with patch("common.role",return_value="nacional"):
            self.assertEqual(c.get("/historico").status_code,403)
            self.assertEqual(c.get("/historico/importaciones/1/original").status_code,403)
        with patch("common.role",return_value="administrador"), patch("history.role",return_value="administrador"),patch("history.db",return_value=self.conn):
            original=workbook(snapshot_rows())
            resp=c.post("/historico/importar",data={"archivo":(BytesIO(original),"prueba.xlsx")},follow_redirects=True)
            self.assertEqual(resp.status_code,200)
            self.assertIn(b"Confirmar importaci",resp.data)
            bid=self.conn.execute("SELECT id FROM hist_books").fetchone()[0]
            url=f"/historico/importaciones/{bid}"
            self.assertEqual(c.get(url+"/original").data,original)
            self.assertEqual(c.post(url+"/confirmar",follow_redirects=True).status_code,200)
            self.assertIn(b"Cliente A",c.get("/historico?caso=1").data)


if __name__=="__main__":
    unittest.main()
