"""Run from the repo with its Python environment, after npm ci in storage-worker.

Starts an isolated local Wrangler D1/R2 instance and two separate HTTP application processes.
All records, identities, keys, caches and worker persistence are generated under a temporary directory.
"""
import concurrent.futures
import contextlib
import hashlib
import http.cookiejar
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "storage-worker"
WRANGLER = WORKER / "node_modules/.bin/wrangler"


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Browser:
    def __init__(self, origin):
        self.origin = origin
        self.token = ""
        self.transport = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )

    def request(self, path, *, method="GET", data=None, headers=None, expected=200):
        request = urllib.request.Request(self.origin + path, data=data, method=method, headers=headers or {})
        try:
            response = self.transport.open(request, timeout=90)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            body = response.read()
            status = response.status
        if expected is not None and status != expected:
            # These fixture endpoints report useful errors in JSON, without including headers or auth.
            detail = ""
            if path.startswith("/api/") and status >= 400:
                try:
                    detail = json.loads(body).get("error", "")
                except ValueError:
                    pass
            raise AssertionError(f"{method} {path}: HTTP {status}, esperaba {expected}. {detail}")
        return status, body

    def token_from(self, path):
        _, html = self.request(path)
        match = re.search(rb'name="csrf_token" value="([^"]+)"|name="csrf-token" content="([^"]+)"', html)
        assert match, f"Falta token de formulario en {path}"
        self.token = next(x for x in match.groups() if x).decode()

    def form(self, path, **values):
        body = urllib.parse.urlencode(dict(values, csrf_token=self.token)).encode()
        self.request(path, method="POST", data=body, headers={"Content-Type": "application/x-www-form-urlencoded"})

    def login(self, account, pin):
        self.token_from("/login")
        self.form("/login", username=account, password=pin)
        self.token_from("/")
        self.request("/api/cases")

    def change(self, method, path, body, expected=200):
        status, raw = self.request(path, method=method, expected=expected, data=json.dumps(body).encode(),
                                   headers={"Content-Type": "application/json","X-CSRF-Token": self.token,
                                            "Idempotency-Key": uuid.uuid4().hex})
        return status, json.loads(raw)

    def get(self, path):
        return json.loads(self.request(path)[1])

    def upload(self, path, version, line_id, asset):
        boundary = "evidence-" + uuid.uuid4().hex
        segments = []
        for key, value in (("version", version), ("line_id", line_id)):
            segments.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
        segments.extend((f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="evidencia.txt"\r\nContent-Type: text/plain\r\n\r\n'.encode(),
                         asset, f'\r\n--{boundary}--\r\n'.encode()))
        return json.loads(self.request(path, method="POST", data=b"".join(segments), headers={
            "Content-Type": "multipart/form-data; boundary="+boundary, "X-CSRF-Token": self.token,
            "Idempotency-Key": uuid.uuid4().hex})[1])


@contextlib.contextmanager
def server(command, cwd, env, origin, readiness, *, headers=None):
    # A separate process group also releases Wrangler's workerd child on interruption.
    with tempfile.TemporaryFile() as logs:
        proc = subprocess.Popen(command, cwd=cwd, env=env, stdout=logs, stderr=logs, start_new_session=True)
        try:
            client = Browser(origin)
            for _ in range(250):
                if proc.poll() is not None:
                    logs.seek(0)
                    lines = logs.read().decode(errors="replace").splitlines()
                    diagnostic = lines[-1] if lines else ""
                    for k, v in env.items():
                        if v and any(word in k for word in ("TOKEN","KEY","PIN","SECRET")):
                            diagnostic = diagnostic.replace(v,"[privado]")
                    raise RuntimeError(f"El proceso de {readiness} terminó con código {proc.returncode}. {diagnostic}")
                try:
                    status,_ = client.request(readiness, headers=headers, expected=None)
                    if status == 200:
                        break
                    if status not in (502,503):
                        raise RuntimeError(f"{readiness} devolvió HTTP {status}.")
                except (OSError, urllib.error.URLError):
                    pass
                time.sleep(.2)
            else:
                raise TimeoutError(f"No respondió {readiness} en {origin}.")
            yield
        finally:
            if proc.poll() is None:
                os.killpg(proc.pid,signal.SIGTERM)
                try:
                    proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid,signal.SIGKILL)
                    proc.wait(timeout=5)


def check_shared_case(first, second, pins):
    national = Browser(first)
    national.login("nacional", pins["nacional"])
    national.origin = second
    assert national.get("/api/cases") == []
    national.origin = first
    international = Browser(second)
    international.login("internacional", pins["internacional"])
    admin = Browser(first)
    admin.login("administrador", pins["administrador"])
    admin.token_from("/accesos")
    admin.form("/accesos", account="calidad", password=pins["calidad"])
    quality = Browser(second)
    quality.login("calidad", pins["calidad"])
    draft = {"client":"Cliente de prueba D1","date":"2026-11-24","description":"Referencia de prueba",
             "lines":[{"code":"TEST","quantity":1,"unit":"unidad","reason":"Envase de prueba","incidence":"daño físico"}]}
    _, case = national.change("POST","/api/cases",draft)
    url = f"/api/cases/{case['id']}"
    line_id = case["lines"][0]["id"]
    # Concurrent edits to the same draft from two independent workers retain one version.
    peer = Browser(second)
    peer.login("nacional",pins["nacional"])
    assert peer.get(url)["version"] == case["version"]
    barrier = threading.Barrier(2)
    def race(client, text):
        payload = dict(draft, description=text, version=case["version"],
                       lines=[dict(draft["lines"][0], id=line_id)])
        barrier.wait(timeout=10)
        return client.change("PUT", url, payload, expected=None)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        first_write = pool.submit(race,national,"Pestaña A")
        second_write = pool.submit(race,peer,"Pestaña B")
        edits = [first_write.result(),second_write.result()]
    assert sorted(result[0] for result in edits)==[200,409], f"Ediciones simultáneas: {[r[0] for r in edits]}"
    saved = next(result[1] for result in edits if result[0]==200)
    assert peer.get(url)["description"] == saved["description"] == national.get(url)["description"]

    asset = b"Evidencia sintetica privada de control R2.\n"+secrets.token_bytes(32)
    case = national.upload(url+"/files",saved["version"],line_id,asset)
    file_url = url + f"/files/{case['files'][0]['id']}"
    _,case=national.change("POST",url+"/actions",{"action":"submit","version":case["version"]})
    assert quality.get(url)["status"]=="enviado"
    assert quality.request(file_url)[1] == asset
    international.request(url,expected=404)
    international.request(file_url,expected=404)
    Browser(second).request(file_url,expected=401)
    _,case=quality.change("POST",url+"/actions",{"action":"ack","version":case["version"]})
    _,case=quality.change("POST",url+"/actions",{
        "action":"publish","version":case["version"],"report":"Informe de prueba","summary":"Respuesta verificada",
        "findings":[{"line_id":line_id,"finding":"Daño documentado","result":"Confirmado","confirmed":1}]})
    assert national.get(url)["reports"][-1]["summary"]=="Respuesta verificada"
    q=admin.get("/api/indicadores?detalle=cola_comunicar")
    assert q["total"]==1 and q["items"][0]["id"]==case["id"]
    return case["id"],url,file_url,asset


def run(folder):
    if not WRANGLER.is_file():
        raise RuntimeError("Primero ejecuta npm ci en storage-worker.")
    # Always choose an empty local D1 namespace; never use inherited production gateway settings.
    worker_url="http://127.0.0.1:"+str(free_port())
    token=secrets.token_urlsafe(40)
    secret_path=folder/".dev.vars"
    secret_path.write_text("STORAGE_TOKEN="+token+"\n",encoding="utf-8")
    secret_path.chmod(0o600)
    worker_env={k:v for k,v in os.environ.items() if k in ("PATH","HOME","TMPDIR","LANG","LD_LIBRARY_PATH")}
    worker_env.update(WRANGLER_SEND_METRICS="false",XDG_CONFIG_HOME=str(folder/"config"),CI="true")
    store=folder/"worker-state"
    start_worker=[str(WRANGLER),"dev","--local","--ip","127.0.0.1","--port",worker_url.rsplit(":",1)[1],
                  "--env-file",str(secret_path),"--persist-to",str(store),"--show-interactive-dev-session=false"]
    auth={"Authorization":"Bearer "+token}
    app_env=dict(worker_env, RECLAMOS_STORAGE_URL=worker_url, RECLAMOS_STORAGE_TOKEN=token, RECLAMOS_SESSION_KEY=secrets.token_hex(32), RECLAMOS_PUBLIC="0")
    pins={key:secrets.token_urlsafe(16) for key in ("nacional","internacional","administrador","calidad")}
    cache_a=folder/"instance-a"
    cache_b=folder/"instance-b"
    def start_app(cache,port):
        return server([sys.executable,"app.py"],ROOT,dict(app_env,RECLAMOS_DATA_DIR=str(cache),PORT=str(port)),
                      f"http://127.0.0.1:{port}","/login")
    with server(start_worker,WORKER,worker_env,worker_url,"/revision",headers=auth):
        Browser(worker_url).request("/revision",expected=401)
        boot_env=dict(app_env,RECLAMOS_DATA_DIR=str(cache_a),RECLAMOS_PIN_NACIONAL=pins["nacional"],
                      RECLAMOS_PIN_INTERNACIONAL=pins["internacional"],RECLAMOS_PIN_ADMIN=pins["administrador"])
        boot=subprocess.run([sys.executable,"-m","flask","--app","app","init-accounts"],cwd=ROOT,env=boot_env,
                            stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,timeout=120)
        if boot.returncode:
            raise RuntimeError("El alta privada falló: "+boot.stderr.decode(errors="replace").splitlines()[-1])
        port_a=free_port()
        with start_app(cache_a,port_a):
            port_b=free_port()
            with start_app(cache_b,port_b):
                cid,url,file_url,asset=check_shared_case(f"http://127.0.0.1:{port_a}",f"http://127.0.0.1:{port_b}",pins)
            shutil.rmtree(cache_b)  # Generated inside the test; the replacement process starts with an empty directory.
            with start_app(cache_b,port_b):
                fresh=Browser(f"http://127.0.0.1:{port_b}")
                fresh.login("calidad",pins["calidad"])
                assert fresh.get(url)["status"]=="respondido"
                assert fresh.request(file_url)[1]==asset
        obj="/blobs/"+hashlib.sha256(asset).hexdigest()
        gateway=Browser(worker_url)
        gateway.request(obj,expected=401)
        assert gateway.request(obj,headers=auth)[1]==asset

    # Check application rows through the local D1 binding itself, independent of the app and journal reader.
    query=f"SELECT c.status,f.content AS object_ref FROM cases c JOIN case_files f ON f.case_id=c.id WHERE c.id={cid}"
    result=subprocess.run([str(WRANGLER),"d1","execute","gestion-reclamos","--local","--persist-to",str(store),
                           "--command",query,"--json"],cwd=WORKER,env=worker_env,check=True,capture_output=True,timeout=60)
    assert json.loads(result.stdout)[0]["results"]==[{"status":"respondido","object_ref":"r2:"+hashlib.sha256(asset).hexdigest()}]
    print("D1 y R2 locales: dos procesos independientes, cuatro roles, edición concurrente 200/409, "
          "informe visible desde otra sesión, adjunto privado y recuperación tras descartar la caché. "
          "Fila confirmada con D1; descarga R2 sin credencial rechazada.")


if __name__=="__main__":
    with tempfile.TemporaryDirectory(prefix="reclamos-d1-r2-") as temp:
        run(Path(temp))
