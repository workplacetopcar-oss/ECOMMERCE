"""Exercise the real four-role UI with throwaway accounts, cases and a private HTTP port."""
import os
import secrets
import sys
import tempfile
import threading
from pathlib import Path

from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import create_app
from auth import bootstrap
from catalog import import_catalog
from common import db


def exercise(directory, browser):
    app = create_app({"DATA_DIR": directory})
    names = ["nacional", "internacional", "calidad", "administrador"]
    credentials = {role: secrets.token_urlsafe(18) for role in names}
    with app.app_context():
        bootstrap({"RECLAMOS_PIN_" + ("ADMIN" if k == "administrador" else k.upper()): v
                   for k, v in credentials.items()})
        fixtures = os.environ.get("RECLAMOS_PRIVATE_FIXTURES")
        if fixtures:
            import_catalog(db(), Path(fixtures) / "datos_privados")
    server = make_server("127.0.0.1", 0, app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    pages = {}
    errors = []
    try:
        for who in ("administrador", "calidad", "internacional", "nacional"):
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.on("pageerror", lambda err: errors.append(str(err)))
            page.set_default_timeout(6000)
            page.goto(base)
            page.locator("#username").fill(who)
            page.locator("#password").fill(credentials[who])
            page.get_by_role("button", name="Ingresar").click()
            pages[who] = page
            if who == "administrador":
                page.wait_for_url("**/indicadores")
                assert page.get_by_text("En Calidad sin respuesta completa", exact=False).count()
                page.goto(base + "/accesos")
                page.locator("#p-calidad").fill(credentials["calidad"])
                page.get_by_role("button", name="Activar acceso").click()
                expect(page.get_by_role("status")).to_contain_text("Clave guardada")
            else:
                page.wait_for_selector("#list-results")
        national = pages["nacional"]
        for width in (360, 390, 430, 768, 1440):
            national.set_viewport_size({"width": width, "height": 850})
            national.get_by_role("link", name="Crear reclamo manual").click()
            national.wait_for_selector("#editor")
            assert national.locator("#editor > section > h2").all_text_contents() == [
                "Cliente y origen", "Problema reportado", "Productos y evidencias", "Revisión y envío"
            ]
            assert national.evaluate("document.documentElement.scrollWidth<=innerWidth"), width
            if width <= 430:
                assert national.locator("#block-client .grid").first.evaluate(
                    "el => getComputedStyle(el).gridTemplateColumns.split(' ').length===1"
                ), width
            national.locator('#editor a[href="#bandeja"]').click()
            national.wait_for_selector("#list-results")
        national.get_by_role("link", name="Crear reclamo manual").click()
        national.locator('input[name="client"]').fill("Empresa de prueba")
        national.locator('textarea[name="description"]').fill("Reporte de prueba de recepción.")
        line = national.locator("[data-line]").first
        line.locator('[name="code"]').fill("PRUEBA")
        line.locator('[name="quantity"]').fill("3")
        line.locator('[name="reason"]').fill("Envase roto al recibir.")
        national.get_by_role("button", name="Revisar antes de enviar").click()
        review = national.locator("#review-preview")
        expect(review).to_contain_text("Empresa de prueba")
        expect(review).to_contain_text("3 unidad")
        # Simulate loss of the confirmation over the network after the server commits.
        def drop_response(route):
            response = route.fetch()
            assert response.ok
            route.abort("connectionreset")
        national.route("**/api/cases", drop_response, times=1)
        review.get_by_role("button", name="Confirmar envío a Calidad").click()
        expect(national.locator("#notice")).to_contain_text("No se recibió la respuesta")
        expect(national.locator('#editor [name="client"]')).to_have_value("Empresa de prueba")
        review.get_by_role("button", name="Confirmar envío a Calidad").click()
        expect(national.locator("#notice")).to_contain_text("Enviado a Calidad")
        national.wait_for_url("**/#caso/*")
        cid = national.url.split("/")[-1]
        returned = national.request.get(base + "/api/cases").json()
        assert len(returned) == 1 and returned[0]["status"] == "enviado"
        expect(national.locator("#content")).to_contain_text("Sin acuse")
        quality = pages["calidad"]
        quality.get_by_role("link", name="RC-" + cid, exact=True).click(timeout=25000)
        quality.get_by_role("button", name="Acusar recibo").click()
        expect(quality.locator("#content")).to_contain_text("En análisis")
        assert pages["internacional"].request.get(base + "/api/cases/" + cid).status == 404
        assert pages["administrador"].request.get(base + "/api/cases/" + cid).json()["status"] == "analisis"
        quality.get_by_text("Redactar informe técnico", exact=True).click()
        report = quality.locator('[data-action="report"]')
        report.locator('[name="report"]').fill("Se revisó la evidencia del expediente.")
        report.locator('[name="summary"]').fill("El informe confirma el daño reportado.")
        report.locator('[name="cover"]').check()
        report.locator('[name="finding"]').fill("Daño visible")
        report.locator('[name="result"]').fill("Daño confirmado")
        report.locator('[name="confirmed"]').fill("3")
        report.get_by_role("button", name="Publicar respuesta").click()
        expect(quality.locator("#content")).to_contain_text("Respuesta recibida")
        expect(national.locator("#content")).to_contain_text("Respuesta recibida", timeout=25000)
        admin = pages["administrador"]
        state = admin.request.get(base + "/api/cases/" + cid).json()
        assert state["status"] == "respondido" and state["reports"][-1]["status"] == "published"
        assert state["lines"][0]["confirmed"] == 3
        admin.goto(base + "/indicadores")
        admin.get_by_role("link", name="Respuestas por comunicar").click()
        expect(admin.locator("#detalle")).to_contain_text("Empresa de prueba")
        assert not errors, errors
        print("Cuatro sesiones, activación, cinco anchos, alta, envío, acuse, respuesta compartida y detalle de gerencia verificados en Chromium.")
    finally:
        server.shutdown()
        for page in pages.values():
            page.context.close()


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as directory, sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ.get("CHROMIUM"))
        try:
            exercise(directory, browser)
        finally:
            browser.close()
