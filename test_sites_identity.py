"""Pruebas de identidad con un firmante RSA y un JWKS sintéticos."""
import base64
import hmac
import json
import os
import re
import secrets
import struct
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

import sites_identity
from app import create_app
from auth import PEOPLE, bootstrap


@pytest.fixture
def project(tmp_path, monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
    jwk.update(kid="ephemeral", alg="RS256", use="sig")
    monkeypatch.setattr(sites_identity, "read_keys", lambda url: [jwk])
    subjects = {name: secrets.token_urlsafe(15) for name in PEOPLE}
    settings = dict(IDENTITY="oidc", ISSUER="https://id.example.invalid",
                    JWKS_URL="https://id.example.invalid/test-keys", AUDIENCE="test-site",
                    ALGORITHM="RS256", JWT_HEADER="X-Test-Project-Identity",
                    JWT_FORMAT="bearer", SUBJECTS=json.dumps(subjects))
    for field, value in settings.items():
        monkeypatch.setenv("RECLAMOS_SITES_" + field, value)
    monkeypatch.setenv("RECLAMOS_PUBLIC", "1")
    otp_key = secrets.token_bytes(20)
    monkeypatch.setenv("RECLAMOS_TOTP_NACIONAL", base64.b32encode(otp_key).decode())
    pins = {key: secrets.token_urlsafe(18) for key in ("NACIONAL", "INTERNACIONAL", "ADMIN")}
    app = create_app({"TESTING": True, "DATA_DIR": tmp_path})
    with app.app_context():
        bootstrap({"RECLAMOS_PIN_" + key: value for key, value in pins.items()})

    def headers(account="nacional", signing_key=key, **override):
        issued = int(time.time())
        claims = dict(iss=settings["ISSUER"], aud=settings["AUDIENCE"], sub=subjects[account],
                      iat=issued, exp=issued + 120)
        claims.update(override)
        token = jwt.encode(claims, signing_key, algorithm="RS256", headers={"kid": "ephemeral"})
        return {settings["JWT_HEADER"]: "Bearer " + token}

    return app, headers, pins, otp_key


def test_request_verification(project):
    app, headers, _, _ = project
    client = app.test_client()
    assert client.get("/login", headers=headers()).status_code == 200
    for route in ("/login", "/", "/api/cases/1/files/1", "/static/portal.css"):
        assert client.get(route).status_code == 401
    assert client.get("/login", headers=headers(aud="otro")).status_code == 401
    assert client.get("/login", headers=headers(iss="https://otro.example.invalid")).status_code == 401
    assert client.get("/login", headers=headers(exp=int(time.time()) - 1)).status_code == 401
    assert client.get("/login", headers=headers(sub="sin-cuenta")).status_code == 403
    foreign = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    assert client.get("/login", headers=headers(signing_key=foreign)).status_code == 401


def test_cookie_bound_to_account_and_signed_identity(project):
    app, headers, pins, otp_key = project
    client = app.test_client()

    def login(name, password, otp, token_headers):
        html = client.get("/login", headers=token_headers).data
        csrf = re.search(rb'name="csrf_token" value="([^"]+)"', html)[1].decode()
        return client.post("/login", headers=token_headers,
                           data=dict(username=name, password=password, otp=otp, csrf_token=csrf))

    assert login("administrador", pins["ADMIN"], "", headers()).status_code == 403
    assert login("calidad", secrets.token_hex(10), "", headers("calidad")).status_code == 401
    assert login("nacional", pins["NACIONAL"], "", headers()).status_code == 401
    digest = hmac.digest(otp_key, struct.pack(">Q", int(time.time()) // 30), "sha1")
    offset = digest[-1] & 15
    otp = f"{(int.from_bytes(digest[offset:offset + 4], 'big') & 0x7fffffff) % 1000000:06d}"
    assert login("nacional", pins["NACIONAL"], otp, headers()).status_code == 302
    assert client.get("/api/cases", headers=headers()).status_code == 200
    assert client.get("/api/cases/1/files/1").status_code == 401
    assert client.get("/api/cases/1/files/1", headers=headers("internacional")).status_code == 403
    assert client.get("/api/cases", headers=headers()).status_code == 401


def test_incomplete_configuration_and_unavailable_keys(project, tmp_path, monkeypatch):
    app, headers, _, _ = project
    def unavailable(_):
        raise OSError()
    monkeypatch.setattr(sites_identity, "read_keys", unavailable)
    assert app.test_client().get("/login", headers=headers()).status_code == 503
    for field, bad in (("JWKS_URL", "http://id.example.invalid"), ("SUBJECTS", '{"inexistente":"sujeto"}'),
                       ("ALGORITHM", "HS256"), ("IDENTITY", "")):
        with monkeypatch.context() as env:
            env.setenv("RECLAMOS_SITES_" + field, bad)
            with pytest.raises(RuntimeError, match="Sitios|RECLAMOS_SITES"):
                create_app({"TESTING": True, "DATA_DIR": tmp_path})


def test_verification_uses_configured_https_jwks(tmp_path, monkeypatch):
    import ssl
    import threading
    from datetime import datetime, timedelta, timezone
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(timezone.utc)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(hours=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    pem = tmp_path / "ca.pem"
    pem.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    keyfile = tmp_path / "test-key.pem"
    keyfile.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption()))
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
    jwk.update(kid="test-key", alg="RS256", use="sig")

    class Endpoint(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/keys":
                self.send_error(404)
                return
            payload = json.dumps({"keys": [jwk]}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Endpoint)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(pem, keyfile)
    keyfile.unlink()
    server.socket = tls.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    monkeypatch.setenv("SSL_CERT_FILE", str(pem))
    monkeypatch.setenv("no_proxy", "localhost")
    try:
        site = sites_identity.SiteIdentity(
            dict(JWT_HEADER="Authorization", JWT_FORMAT="raw", ALGORITHM="RS256",
                 ISSUER="https://issuer.example.invalid", AUDIENCE="trial",
                 JWKS_URL=f"https://localhost:{server.server_port}/keys",
                 SUBJECTS=json.dumps({"nacional": "persona-sintetica"})), ["nacional"])
        token = jwt.encode(dict(sub="persona-sintetica", iss=site.issuer, aud=site.audience,
                                iat=int(time.time()), exp=int(time.time())+120),
                           key, algorithm="RS256", headers={"kid": "test-key"})
        assert site.verify(token)[0] == "nacional"
    finally:
        server.shutdown()
        thread.join()
        server.server_close()

def test_only_verified_owner_subject_can_reach_its_login(project, tmp_path, monkeypatch):
    _, headers, _, _ = project
    subjects = json.loads(os.environ["RECLAMOS_SITES_SUBJECTS"])
    monkeypatch.setenv("RECLAMOS_SITES_SUBJECTS", json.dumps({"administrador": subjects["administrador"]}))
    app = create_app({"TESTING": True, "DATA_DIR": tmp_path})
    client = app.test_client()
    owner_login = client.get("/login", headers=headers("administrador"))
    assert owner_login.status_code == 200
    assert b'value="administrador" readonly' in owner_login.data
    for path in ("/login", "/api/cases", "/api/cases/1/files/1"):
        response = client.get(path, headers=headers("nacional"))
        assert response.status_code == 403 and b"Acceso pendiente" in response.data
