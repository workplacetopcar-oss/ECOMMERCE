"""Prueba privada en Chromium con un libro sintético y una base temporal."""
from pathlib import Path
import logging
import os
import secrets
import sys
import tempfile
import threading
from time import perf_counter

from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import create_app
from auth import bootstrap
from test_history import snapshot_rows, workbook


def main():
    with tempfile.TemporaryDirectory() as directory:
        app = create_app({"DATA_DIR":directory})
        admin_pin, national_pin = secrets.token_urlsafe(13),secrets.token_urlsafe(13)
        with app.app_context():
            bootstrap({"RECLAMOS_PIN_ADMIN":admin_pin, "RECLAMOS_PIN_NACIONAL":national_pin,
                       "RECLAMOS_PIN_INTERNACIONAL":secrets.token_urlsafe(13)})
        logging.getLogger("werkzeug").setLevel(logging.ERROR)
        server=make_server("127.0.0.1",0,app,threaded=True)
        url=f"http://127.0.0.1:{server.server_port}"
        threading.Thread(target=server.serve_forever,daemon=True).start()
        try:
            with sync_playwright() as p:
                b=p.chromium.launch(executable_path=os.environ.get("CHROMIUM"))
                page=b.new_page(viewport={"width":360,"height":800})
                def login(page, username, password):
                    page.goto(url+"/login")
                    page.locator('[name="username"]').fill(username)
                    page.locator('[name="password"]').fill(password)
                    page.locator('[name="password"]').press("Enter")
                    page.wait_for_url(url+("/indicadores" if username=="administrador" else "/"))
                def layout():
                    assert page.evaluate("document.documentElement.scrollWidth<=window.innerWidth")
                login(page,"administrador",admin_pin)
                page.goto(url+"/historico")
                page.keyboard.press("Tab")
                assert page.locator(".skip").evaluate("e=>e===document.activeElement")
                page.keyboard.press("Enter")
                page.get_by_label("Libro Excel").set_input_files({
                  "name":"Prueba.xlsx", "mimeType":"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                  "buffer":workbook(snapshot_rows(),extras=True)})
                layout()
                start=perf_counter()
                page.get_by_role("button",name="Preparar vista previa").press("Enter")
                page.get_by_role("heading",name="Vista previa del libro").wait_for()
                layout()
                elapsed=1000*(perf_counter()-start)
                page.get_by_role("button",name="Confirmar importación").press("Enter")
                page.get_by_role("heading",name="Importación confirmada").wait_for()
                preview_url=page.url
                page.get_by_role("link",name="Volver al histórico").click()
                page.get_by_role("link",name="105",exact=True).click()
                page.get_by_text("Cliente C · 105",exact=True).wait_for()
                assert "Sin fecha registrada" in page.locator("main").inner_text()
                layout()
                for w in (390,430,768,1440):
                    page.set_viewport_size({"width":w,"height":800})
                    layout()
                restricted=b.new_page()
                login(restricted,"nacional",national_pin)
                assert restricted.goto(preview_url).status==403
                assert restricted.goto(preview_url+"/original").status==403
                b.close()
                print(f"Vista previa de 12 filas en {elapsed:.0f} ms. Teclado, confirmación, consulta y acceso privado correctos. Anchos 360/390/430/768/1440 px.")
        finally:
            server.shutdown()


if __name__=="__main__":
    main()
