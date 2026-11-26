"""Exercise both intake forms in Chromium with the operator-provided private examples."""
import json
import logging
import os
import secrets
import sys
import tempfile
import threading
from pathlib import Path

from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import create_app
from auth import bootstrap
from catalog import import_catalog
from common import db


def verify(root, directory, browser):
    app=create_app({"DATA_DIR":directory})
    creds={name:secrets.token_urlsafe(24) for name in ("nacional","internacional","administrador")}
    with app.app_context():
        bootstrap({"RECLAMOS_PIN_"+("ADMIN" if k=="administrador" else k.upper()):v for k,v in creds.items()})
        import_catalog(db(),root/"datos_privados")
    server=make_server("127.0.0.1",0,app,threaded=True)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    errors=[]
    base=f"http://127.0.0.1:{server.server_port}"
    def login(name):
        page=browser.new_page(viewport={"width":390,"height":844})
        page.set_default_timeout(5000)
        page.on("pageerror",lambda err:errors.append(str(err)))
        page.goto(base)
        page.locator("#username").fill(name)
        page.locator("#password").fill(creds[name])
        page.get_by_role("button",name="Ingresar").click()
        page.wait_for_selector("#list-results")
        return page
    try:
        national=login("nacional")
        national.get_by_role("link",name="Revisar CONTACTO de RSales").click()
        national.locator("input[type=file]").set_input_files(root/"ejemplos/CONTACTO_019.pdf")
        national.get_by_role("button",name="Ver vista previa").click()
        thumbnails=national.locator("#pdf-data .contact-photos img")
        expect(thumbnails).to_have_count(9)
        for photo in thumbnails.all():
            photo.scroll_into_view_if_needed()
            photo.evaluate("node => node.decode()")
            assert photo.evaluate("node => node.naturalWidth > 0")
        national.get_by_role("button",name="Guardar comprobante y revisar").click()
        lines=national.locator("[data-line]")
        expect(lines).to_have_count(3)
        first=lines.first
        expect(first.locator('[name="reported_code"]')).to_have_value("3601")
        expect(first.locator('[name="code"]')).to_have_value("")
        expect(first.locator('[name="quantity"]')).to_have_value("22")
        expect(first).to_contain_text("Pregunta")
        expect(national.locator('[name="f_city"]')).not_to_have_value("")
        expect(national.locator('[name="f_seller"]')).not_to_have_value("")
        first.locator(".search-input").fill("3601")
        first.locator("[data-pick]").filter(has_text="3601M").click()
        expect(first.locator('[name="code"]')).to_have_value("3601M")
        expect(first.locator('[name="quantity"]')).to_have_value("22")
        expect(first.locator('[name="unit"]')).to_have_value("unidad")
        first.get_by_text("Datos de la selección comercial",exact=True).click()
        expect(first.locator("[data-choice-info]")).to_contain_text("Sin dato en RSales")
        national.get_by_text("Consultar fichas y presentaciones Lubristone").click()
        national.locator("#card-query").fill("graphenox")
        expect(national.locator("#card-results details").first).to_be_visible()
        international=login("internacional")
        international.get_by_role("link",name="Nuevo reclamo internacional").click()
        clients=json.loads((root/"datos_privados/clientes_internacionales.json").read_text())
        codes=json.loads((root/"datos_privados/codigos_documentados_internacional.json").read_text())
        owner=next(c for c in clients if c["id"]==codes[0]["cliente_id"])
        lookup=international.locator('[data-lookup="clients"]')
        lookup.locator(".search-input").fill(owner["alias"][0])
        lookup.locator("[data-pick]").filter(has_text=owner["razon_social"]).click()
        expect(international.locator('[name="f_tax_id"]')).to_have_value(owner["identificacion_fiscal"])
        intl_line=international.locator("[data-line]").first
        intl_line.get_by_text("Códigos del cliente y equivalencias verificadas").click()
        intl_line.locator("select[data-document-code]").select_option("0")
        expect(intl_line.locator('[name="reported_code"]')).not_to_have_value("")
        expect(intl_line.locator('[name="code"]')).to_have_value("")
        intl_line.locator(".search-input").fill("3601D")
        intl_line.locator("[data-pick]").first.click()
        intl_line.locator('[name="match_reason"]').fill("Correspondencia de prueba")
        intl_line.locator('[data-confirm-match]').click()
        expect(international.locator("#notice")).to_contain_text("Versión 1")
        international.locator('[name="f_contact"]').fill("Contacto de prueba")
        international.locator('[name="profile_source"]').fill("Confirmación de prueba")
        international.locator('[data-command="save-client"]').click()
        expect(international.locator("#notice")).to_contain_text("Contacto guardado")
        assert international.request.get(base+"/api/catalog/clients").json()["total"]==20
        with app.app_context():
            assert db().execute("SELECT count(*) FROM catalog_profiles").fetchone()[0]==1
        assert international.request.get(base+"/maestros").status==403
        admin=browser.new_page()
        admin.goto(base)
        admin.locator("#username").fill("administrador")
        admin.locator("#password").fill(creds["administrador"])
        admin.get_by_role("button",name="Ingresar").click()
        admin.goto(base+"/maestros")
        expect(admin.locator("main")).to_contain_text("8822")
        assert not errors, errors
        print("Chromium · CONTACTO, fuente original, SKU y unidad, fichas, códigos por cliente y perfil confirmados.")
    finally:
        server.shutdown()


if __name__=="__main__":
    root=Path(os.environ["RECLAMOS_PRIVATE_FIXTURES"])
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    with tempfile.TemporaryDirectory() as directory,sync_playwright() as p:
        browser=p.chromium.launch(executable_path=os.environ.get("CHROMIUM"))
        try:
            verify(root,directory,browser)
        finally:
            browser.close()
