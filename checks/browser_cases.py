"""Extra browser acceptance checks with private fixtures and a disposable database."""
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

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import create_app
from auth import bootstrap
from catalog import import_catalog
from common import db


def login(browser, base, user, password):
    page = browser.new_page(viewport={"width": 1280, "height": 840})
    page.set_default_timeout(8000)
    page.goto(base)
    page.locator("#username").fill(user)
    page.locator("#password").fill(password)
    page.get_by_role("button", name="Ingresar").click()
    return page


def open_action(page, name):
    form = page.locator('[data-action="' + name + '"]')
    form.locator("..").locator("summary").first.click()
    return form


def saved(page, click):
    click.click()
    expect(page.locator("#notice")).to_contain_text("Actuación guardada")


def international(page, client):
    page.get_by_role("link", name="Nuevo reclamo internacional").click()
    lookup = page.locator('[data-lookup="clients"]')
    lookup.locator('[type=search]').fill(client["alias"][0])
    choice = lookup.locator("[data-pick]").filter(has_text=client["razon_social"]).first
    choice.click()
    expect(page.locator('[name=f_country]')).to_have_value(client["pais"])
    for kind, number in [("factura", "FAC-1"), ("factura", "FAC-2"), ("oc", "OC-1")]:
        page.get_by_role("button", name="Agregar factura u otro documento").click()
        doc = page.locator("[data-document]").last
        doc.locator("[name=type]").select_option(kind)
        doc.locator("[name=number]").fill(number)
    page.locator('textarea[name=description]').fill("Prueba de manejo de derrame.")
    for i, measure in enumerate(["galón", "litro"]):
        if i:
            page.get_by_role("button", name="Agregar otra referencia").click()
        line = page.locator("[data-line]").last
        line.locator('[name=description]').fill("Referencia de prueba " + str(i))
        line.locator('[name=quantity]').fill("2")
        line.locator('[name=unit]').fill(measure)
        line.locator('[name=reason]').fill("Derrame reportado")
    page.get_by_role("button", name="Preparar la carga de archivos").click()
    expect(page.locator("#notice")).to_contain_text("Borrador guardado")
    page.locator("[name=evidence_line]").select_option(index=1)
    page.locator("[name=editor_file]").set_input_files({"name":"prueba.txt","mimeType":"text/plain","buffer":b"Evidencia de prueba"})
    page.get_by_role("button", name="Adjuntar archivo", exact=True).click()
    expect(page.locator("#saved-evidence")).to_contain_text("prueba.txt")
    page.get_by_role("button", name="Revisar antes de enviar").click()
    expect(page.locator("#review-preview")).to_contain_text("FAC-1")
    expect(page.locator("#review-preview")).to_contain_text("prueba.txt")
    page.get_by_role("button", name="Confirmar envío a Calidad").click()
    expect(page.locator("#notice")).to_contain_text("Enviado a Calidad")
    case_url = page.url
    return case_url


def technical_cycle(quality, origin, admin, case_url):
    quality.set_viewport_size({"width":390,"height":844})
    quality.goto(case_url)
    saved(quality, quality.get_by_role("button", name="Acusar recibo"))
    admin.goto(case_url)
    early = open_action(admin, "decision")
    early.locator('[name=replacement]').fill("2")
    early.locator('[name=authorized_by]').fill("Gerencia de prueba")
    early.locator('[name=note]').fill("Reposición comercial anticipada para esta referencia.")
    saved(admin, early.get_by_role("button"))
    expect(admin.get_by_text("Cliente atendido · Calidad pendiente", exact=True)).to_be_visible()
    quality.reload()
    form = open_action(quality, "request_info")
    form.locator("[name=note]").fill("Aporta el lote.")
    saved(quality, form.get_by_role("button"))
    origin.reload()
    form = origin.locator("[data-action=provide_info]")
    form.locator("[name=note]").fill("Lote confirmado por el distribuidor.")
    saved(origin, form.get_by_role("button"))
    quality.reload()
    for count in [1, 2]:
        form = open_action(quality, "report")
        form.locator("[name=report]").fill("Informe de prueba.")
        form.locator("[name=summary]").fill("Explicación para el cliente.")
        form.locator("[name=note]").fill("Actualización según la evidencia.")
        findings = form.locator("[data-finding]")
        for i in range(count):
            line = findings.nth(i)
            line.locator("[name=cover]").check()
            line.locator("[name=finding]").fill("Resultado observado en muestra")
            line.locator("[name=result]").fill("Daño confirmado")
            line.locator("[name=confirmed]").fill("2")
        saved(quality, form.get_by_role("button", name="Publicar respuesta"))
        expect(quality.locator(".badge").filter(has_text="Respuesta técnica parcial" if count == 1 else "Respuesta recibida").first).to_be_visible()
    origin.reload()
    reports = origin.locator("details[data-report-id]")
    reports.first.locator("summary").first.click()
    with origin.expect_download():
        reports.first.get_by_role("link", name="Descargar carta").click()


def commercial_cycle(page):
    choices = ["replacement", "credit"]
    for i, choice in enumerate(choices):
        form = open_action(page, "decision")
        form.locator('[name=line_id]').select_option(index=i)
        form.locator('[name=' + choice + ']').fill("2")
        form.locator('[name=authorized_by]').fill("Gerencia de prueba")
        form.locator('[name=note]').fill("Resolución documentada.")
        saved(page, form.get_by_role("button"))
        if choice == "replacement":
            form = open_action(page, "dispatch")
            form.locator('[name=line_id]').select_option(index=i)
            form.locator('[name=quantity]').fill("2")
            form.locator('[name=support]').fill("SOPORTE-TEST")
            saved(page, form.get_by_role("button"))
        else:
            form = open_action(page, "credit_apply")
            form.locator('[name=number]').fill("NC-PRUEBA")
            form.locator('[name=total]').fill("120.25")
            form.locator('[name=currency]').fill("USD")
            form.get_by_role("button",name="Agregar otro documento").click()
            for row, number, amount in zip(form.locator("[data-credit-row]").all(),["FAC-1","FAC-2"],["120","0.25"]):
                row.locator('[name=document_number]').fill(number)
                row.locator('[name=document_amount]').fill(amount)
            form.locator('[data-credit-line]').fill("2")
            saved(page, form.get_by_role("button",name="Registrar aplicación"))
            applied=page.locator("section").filter(has=page.get_by_role("heading",name="Notas crédito aplicadas"))
            expect(applied).to_contain_text("Factura FAC-1")
            expect(applied).to_contain_text("Factura FAC-2")
            expect(applied).to_contain_text("Saldo por aplicar · USD 0")
    form = open_action(page, "amend")
    form.locator('[name=category]').select_option("inspeccion")
    form.locator('[name=warehouse]').fill("Bodega de prueba")
    form.locator('[name=note]').fill("Inspección de seguimiento")
    saved(page, form.get_by_role("button"))
    form = open_action(page, "communicate")
    for key, value in {"sender": "Vendedor", "medium": "Llamada", "note": "Comunicado al distribuidor"}.items():
        form.locator('[name=' + key + ']').fill(value)
    saved(page, form.get_by_role("button"))
    saved(page, open_action(page, "close").get_by_role("button"))
    expect(page.locator(".badge").first).to_have_text("Cerrado")
    form = open_action(page, "reopen")
    form.locator('[name=note]').fill("Reincidencia reportada")
    saved(page, form.get_by_role("button"))
    expect(page.locator(".badge").first).to_have_text("Sin acuse")


def national_pdf(page, path):
    page.get_by_role("link", name="Revisar CONTACTO de RSales").click()
    page.locator('[name=file]').set_input_files(path)
    page.get_by_role("button", name="Ver vista previa").click()
    rows = page.locator('#pdf-data table').first.locator('tbody tr')
    rows.first.wait_for()
    source_quantities = [int(row.locator("td").nth(2).inner_text().split()[0]) for row in rows.all()]
    photos = page.locator("#pdf-data .contact-photos img")
    assert photos.count() > 0
    for photo in photos.all():
        photo.scroll_into_view_if_needed()
        photo.evaluate("img => img.decode()")
        assert photo.evaluate("img => img.naturalWidth > 0")
    page.get_by_role("button", name="Guardar comprobante y revisar").click()
    expect(page.locator('[data-line]')).to_have_count(len(source_quantities))
    assert [int(x.input_value()) for x in page.locator('[data-line] [name=quantity]').all()] == source_quantities
    page.locator('[name=note]').fill("Verificación de prueba del formulario y sus líneas.")
    page.get_by_role("button", name="Revisar antes de enviar").click()
    expect(page.locator("#review-preview")).to_contain_text("Revisión del expediente")
    page.get_by_role("button", name="Confirmar envío a Calidad").click()
    expect(page.locator("#notice")).to_contain_text("Enviado a Calidad")


def main():
    fixtures = Path(os.environ["RECLAMOS_PRIVATE_FIXTURES"])
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    with tempfile.TemporaryDirectory() as directory, sync_playwright() as p:
        app = create_app({"DATA_DIR":directory})
        keys = {user:secrets.token_urlsafe(24) for user in ["nacional","internacional","calidad","administrador"]}
        with app.app_context():
            bootstrap({"RECLAMOS_PIN_" + ("ADMIN" if k=="administrador" else k.upper()):v for k,v in keys.items()})
            import_catalog(db(), fixtures / "datos_privados")
        server = make_server("127.0.0.1",0,app,threaded=True)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        browser = p.chromium.launch(executable_path=os.environ.get("CHROMIUM"))
        try:
            base = f"http://127.0.0.1:{server.server_port}"
            adm = login(browser,base,"administrador",keys["administrador"])
            adm.goto(base+"/accesos")
            adm.locator("#p-calidad").fill(keys["calidad"])
            adm.get_by_role("button",name="Activar acceso").click()
            users = {u:login(browser,base,u,keys[u]) for u in ["internacional","calidad","nacional"]}
            errors=[]
            for page in users.values():
                page.on("pageerror",lambda err:errors.append(str(err)))
                page.on("console",lambda msg: errors.append(msg.text) if msg.type=="error" else None)
            national_pdf(users["nacional"], next((fixtures / "ejemplos").glob("*.pdf")))
            masters = fixtures / "datos_privados"
            customers = json.loads((masters / "clientes_internacionales.json").read_text())
            codes = json.loads((masters / "codigos_documentados_internacional.json").read_text())
            documented = {row["cliente_id"] for row in codes}
            client = next(row for row in customers if row["id"] in documented and row["alias"])
            url = international(users["internacional"], client)
            technical_cycle(users["calidad"],users["internacional"],adm,url)
            commercial_cycle(users["internacional"])
            assert not errors, errors
            print("Chromium: CONTACTO nacional y ciclo internacional con dos líneas, adjunto, dos informes, carta, crédito, despacho, comunicación, cierre y reapertura.")
        finally:
            browser.close()
            server.shutdown()

if __name__ == "__main__":
    main()
