"""Measure the HTTP inbox and local search on an isolated copy of the schema."""
import os
import threading
import random
import secrets
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import create_app
from auth import bootstrap
from common import db, now, transaction


def measure():
    with tempfile.TemporaryDirectory() as directory:
        app = create_app({"DATA_DIR": directory, "TESTING": True})
        pin = secrets.token_urlsafe(16)
        with app.app_context():
            bootstrap({k: pin for k in ["RECLAMOS_PIN_NACIONAL", "RECLAMOS_PIN_INTERNACIONAL", "RECLAMOS_PIN_ADMIN"]})
            stamp = now()
            conn = db()
            with transaction(conn):
                conn.executemany("""INSERT INTO cases(id,channel,origin,client,description,status,created_at,updated_at)
                    VALUES(?,'nacional','nacional_manual',?,'Prueba de rendimiento','borrador',?,?)""",
                                 ((i, f"Cliente de prueba {i}", stamp, stamp) for i in range(1, 10001)))
                conn.executemany("""INSERT INTO case_lines(case_id,code,description,presentation,quantity,unit,reason,lot,fields,incidence)
                    VALUES(?,?,'Referencia de prueba','UNIDAD',1,'unidad','Reporte de prueba',?,'{}','daño físico')""",
                                 ((i, f"SKU-{i}", f"LOTE-{i // 10}") for i in range(1, 10001)))
        client = app.test_client()
        client.get("/login")
        with client.session_transaction() as s:
            token = s["csrf"]
        assert client.post("/login", data={"username": "nacional", "password": pin, "csrf_token": token}).status_code == 302
        start = time.perf_counter()
        assert client.get("/").status_code == 200
        inbox = client.get("/api/cases?limit=25")
        assert len(inbox.json) == 25 and inbox.headers["X-Total-Count"] == "10000"
        first = time.perf_counter() - start
        measurements = []
        rng = random.Random(21)
        for _ in range(60):
            query = rng.choice(["SKU-", "LOTE-", "Cliente de prueba "]) + str(rng.randint(1, 999))
            start = time.perf_counter()
            response = client.get("/api/cases", query_string={"q": query, "limit": 25})
            assert response.status_code == 200 and response.json
            measurements.append(time.perf_counter()-start)
        p95 = statistics.quantiles(measurements, n=100, method="inclusive")[94]
        print(f"10.000 expedientes y líneas · HTML + API bandeja {first * 1000:.1f} ms · "
              f"60 búsquedas HTTP locales, p95 {p95 * 1000:.1f} ms")


        if os.environ.get("CHROMIUM"):
            from playwright.sync_api import sync_playwright
            from werkzeug.serving import make_server
            server = make_server("127.0.0.1", 0, app, threaded=True)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                with sync_playwright() as pw:
                    browser = pw.chromium.launch(executable_path=os.environ["CHROMIUM"])
                    page = browser.new_page()
                    page.goto(f"http://127.0.0.1:{server.server_port}/login")
                    page.locator("#username").fill("nacional")
                    page.locator("#password").fill(pin)
                    with page.expect_navigation():
                        page.get_by_role("button", name="Ingresar").click()
                    page.wait_for_selector("#list-results .case-no")
                    print(f"Bandeja utilizable en Chromium con 10.000 expedientes: {page.evaluate('performance.now()'):.0f} ms desde el inicio de la navegación.")
                    browser.close()
            finally:
                server.shutdown()


if __name__ == "__main__":
    measure()
