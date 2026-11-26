"""Validate the operator-provided real cut and CONTACTO PDF in a temporary loopback D1/R2 emulator."""
import hashlib
import json
import os
import secrets
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

from storage_flow import Browser, ROOT, WORKER, WRANGLER, free_port, server


def verify(temp, fixtures):
    token=secrets.token_urlsafe(40)
    pin=secrets.token_urlsafe(18)
    worker_url="http://127.0.0.1:"+str(free_port())
    worker_env={k:v for k,v in os.environ.items() if k in ("PATH","HOME","TMPDIR","LANG","LD_LIBRARY_PATH")}
    worker_env.update(WRANGLER_SEND_METRICS="false",XDG_CONFIG_HOME=str(temp/"config"),CI="true")
    secrets_file=temp/".dev.vars"
    secrets_file.write_text("STORAGE_TOKEN="+token+"\n",encoding="utf-8")
    secrets_file.chmod(0o600)
    auth={"Authorization":"Bearer "+token}
    state=temp/"state"
    install=[str(WRANGLER),"dev","--local","--ip","127.0.0.1","--port",worker_url.rsplit(":",1)[1],
             "--env-file",str(secrets_file),"--persist-to",str(state),"--show-interactive-dev-session=false"]
    app_env=dict(worker_env, RECLAMOS_STORAGE_URL=worker_url, RECLAMOS_STORAGE_TOKEN=token,
                 RECLAMOS_PUBLIC="0",RECLAMOS_SESSION_KEY=secrets.token_hex(32))
    cache=temp/"primera"
    cache.mkdir()
    data=(fixtures/"ejemplos/CONTACTO_019.pdf").read_bytes()

    def app_server(cache_dir):
        port=free_port()
        url="http://127.0.0.1:"+str(port)
        env=dict(app_env, RECLAMOS_DATA_DIR=str(cache_dir), PORT=str(port))
        return server([sys.executable,"app.py"],ROOT,env,url,"/login"),url

    def run(args, env, cwd=ROOT):
        result=subprocess.run(args,cwd=cwd,env=env,capture_output=True,timeout=120)
        if result.returncode:
            # SQL data, URLs and credentials must not enter terminal logs.
            failures=result.stderr.decode(errors="replace").splitlines()
            final=failures[-1][:220] if failures else "Sin detalle"
            raise RuntimeError(f"Falló {Path(args[0]).name}: {final}")
        return result.stdout

    with server(install,WORKER,worker_env,worker_url,"/revision",headers=auth):
        run([sys.executable,"catalog.py","--db",str(cache/"reclamos.sqlite3"),"--data",str(fixtures/"datos_privados")],app_env)
        run([sys.executable,"-m","flask","--app","app","init-accounts"],
            dict(app_env,RECLAMOS_DATA_DIR=str(cache),RECLAMOS_PIN_NACIONAL=pin,
                 RECLAMOS_PIN_ADMIN=secrets.token_urlsafe(18),RECLAMOS_PIN_INTERNACIONAL=secrets.token_urlsafe(18)))
        instance,url=app_server(cache)
        with instance:
            client=Browser(url)
            client.login("nacional",pin)
            boundary=uuid.uuid4().hex
            upload=f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="contacto.pdf"\r\nContent-Type: application/pdf\r\n\r\n'.encode()+data+f'\r\n--{boundary}--\r\n'.encode()
            _,body=client.request("/api/contacto/preview",method="POST",data=upload,headers={
                "Content-Type":"multipart/form-data; boundary="+boundary,"X-CSRF-Token":client.token})
            preview=json.loads(body)
            photos=[p for l in preview["parsed"]["lineas"] for p in l["fotos"]]
            assert len(photos)==9 and all(p["disponible"] for p in photos)
            first_photo=client.request("/api/contacto/preview/"+preview["token"]+"/fotos/13")[1]
            assert first_photo.startswith(b"\xff\xd8")
            _,saved=client.change("POST","/api/contacto/import",{"token":preview["token"]})
            cid=saved["id"]
            assert client.change("POST","/api/contacto/import",{"token":preview["token"]})[1]["id"]==cid

        # A separate directory and process must reconstruct its cache solely from the shared state.
        replica,url=app_server(temp/"replica")
        with replica:
            client=Browser(url)
            client.login("nacional",pin)
            assert client.get("/api/catalog/products?inactive=1&page_size=1")["total"]==1398
            assert client.get("/api/catalog/clients?unverified=1&page_size=1")["total"]==8822
            assert client.get("/api/catalog/cards?page_size=1")["total"]==38
            c=client.get(f"/api/cases/{cid}")
            assert len(c["lines"])==3 and sum(l["quantity"] for l in c["lines"])==32
            assert len(c["files"])==1
            fid=c["files"][0]["id"]
            assert client.request(f"/api/cases/{cid}/files/{fid}")[1]==data
            assert client.request(f"/api/cases/{cid}/contacto/{fid}/fotos/13")[1]==first_photo
        private_object="/blobs/"+hashlib.sha256(data).hexdigest()
        Browser(worker_url).request(private_object,expected=401)
        assert Browser(worker_url).request(private_object,headers=auth)[1]==data
    sql="""SELECT (SELECT count(*) FROM catalog_products) AS productos,
           (SELECT count(*) FROM catalog_clients WHERE channel='nacional' AND custom=0) AS nacionales,
           (SELECT count(*) FROM catalog_clients WHERE channel='internacional' AND custom=0) AS internacionales,
           (SELECT count(*) FROM case_files WHERE typeof(content)='text' AND content LIKE 'r2:%') AS adjuntos_r2,
           (SELECT count(*) FROM contacto_previews WHERE typeof(original)='text' AND original LIKE 'r2:%') AS previas_r2"""
    result=run([str(WRANGLER),"d1","execute","gestion-reclamos","--local","--persist-to",str(state),
                "--command",sql,"--json"],worker_env,WORKER)
    assert json.loads(result)[0]["results"]==[dict(productos=1398,nacionales=8822,internacionales=20,
                                                     adjuntos_r2=1,previas_r2=1)]
    print("D1/R2 emulados: corte completo restaurado en otra instancia. 1.398 productos, 38 fichas, "
          "8.822 clientes nacionales y 20 internacionales. CONTACTO: 3 líneas, 32 unidades, "
          "9 fotos vinculadas, original íntegro en R2, acceso anónimo denegado.")


if __name__=="__main__":
    fixtures=Path(os.environ["RECLAMOS_PRIVATE_FIXTURES"])
    os.umask(0o077)
    with tempfile.TemporaryDirectory(prefix="reclamos-catalogo-") as folder:
        verify(Path(folder),fixtures)
