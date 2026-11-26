"""Acceso privado: probar por HTTP con credenciales efímeras."""
import base64
import hmac
import os
import re
import secrets
import struct
import time

import pytest

from app import create_app
from auth import bootstrap


@pytest.fixture
def site(tmp_path):
    app = create_app({"TESTING": True, "DATA_DIR": tmp_path})
    pins = {name: secrets.token_urlsafe(12) for name in ("NACIONAL", "INTERNACIONAL", "ADMIN")}
    with app.app_context():
        bootstrap({"RECLAMOS_PIN_" + key: val for key, val in pins.items()})
    return app, pins


def csrf(client, url):
    response = client.get(url)
    token = re.search(rb'name="csrf_token" value="([^"]+)"', response.data)
    assert token is not None
    return token[1].decode()


def login(client, name, pin, otp=""):
    return client.post("/login", data={"username": name, "password": pin, "otp": otp,
                                       "csrf_token": csrf(client, "/login")})


def test_activation_reset_and_restart(site, tmp_path):
    app, pins = site
    admin, quality, national = [app.test_client() for _ in range(3)]
    assert admin.get("/api/cases").status_code == 401
    assert login(admin, "administrador", pins["ADMIN"]).status_code == 302
    assert b"Pendiente de activaci" in admin.get("/accesos").data
    assert login(national, "nacional", pins["NACIONAL"]).status_code == 302
    assert national.get("/accesos").status_code == 403
    assert admin.post("/accesos", data={"account": "calidad", "password": "unused"}).status_code == 400
    assigned = secrets.token_urlsafe(18)
    def reset(value):
        return admin.post("/accesos", data={"account": "calidad", "password": value,
                                           "csrf_token": csrf(admin, "/accesos")})
    assert reset(assigned).status_code == 200
    assert login(quality, "calidad", assigned).status_code == 302
    assert quality.get("/api/cases").status_code == 200
    assert reset(secrets.token_urlsafe(18)).status_code == 200
    assert quality.get("/api/cases").status_code == 401
    restarted = create_app({"TESTING": True, "DATA_DIR": tmp_path})
    with restarted.app_context():
        bootstrap({})
    assert login(restarted.test_client(), "nacional", pins["NACIONAL"]).status_code == 302


def test_rate_limit(site):
    app, _ = site
    client = app.test_client()
    for _ in range(6):
        assert login(client, "nacional", "incorrecta").status_code == 401
    assert login(client, "nacional", "incorrecta").status_code == 429


def test_public_requires_totp_and_blocks_reuse(site, monkeypatch):
    app, pins = site
    app.config["PUBLIC"] = True
    client = app.test_client()
    secret = os.urandom(20)
    monkeypatch.setenv("RECLAMOS_TOTP_NACIONAL", base64.b32encode(secret).decode())
    assert login(client, "nacional", pins["NACIONAL"]).status_code == 401
    digest = hmac.digest(secret, struct.pack(">Q", int(time.time()) // 30), "sha1")
    offset = digest[-1] & 15
    code = f"{(int.from_bytes(digest[offset:offset+4], 'big') & 0x7fffffff) % 1000000:06d}"
    assert login(client, "nacional", pins["NACIONAL"], code).status_code == 302
    assert login(app.test_client(), "nacional", pins["NACIONAL"], code).status_code == 401
