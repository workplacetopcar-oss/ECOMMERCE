"""Verificación opcional de una identidad firmada proporcionada por el proyecto."""
import hashlib
import json
import os
import re
import threading
import time
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, build_opener

import jwt
from flask import abort, current_app, g, request, session

from common import role


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def https_url(value):
    parts = urlsplit(value)
    return (parts.scheme == "https" and parts.hostname and not parts.username
            and not parts.password and not parts.fragment and not parts.query)


def read_keys(url):
    # The operator selects this single HTTPS destination. A token cannot change it.
    with build_opener(NoRedirect()).open(url, timeout=4) as response:
        raw = response.read(256 * 1024 + 1)
    if len(raw) > 256 * 1024:
        raise ValueError("El conjunto de claves excede el límite.")
    return json.loads(raw)["keys"]


class SiteIdentity:
    def __init__(self, settings, accounts):
        self.header = settings["JWT_HEADER"]
        self.format = settings["JWT_FORMAT"]
        self.issuer = settings["ISSUER"]
        self.audience = settings["AUDIENCE"]
        self.jwks = settings["JWKS_URL"]
        self.algorithm = settings["ALGORITHM"]
        if (not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]*", self.header)
                or self.format not in ("raw", "bearer")
                or not https_url(self.issuer) or not https_url(self.jwks)
                or not self.audience.strip()
                or self.algorithm not in ("RS256", "PS256", "ES256")):
            raise ValueError("Revisa la configuración de transporte y verificación.")
        subjects = json.loads(settings["SUBJECTS"])
        if (not isinstance(subjects, dict) or set(subjects) != set(accounts)
                or not all(isinstance(sub, str) and sub.strip() for sub in subjects.values())
                or len(set(subjects.values())) != len(accounts)):
            raise ValueError("Cada cuenta requiere un sujeto distinto.")
        self.accounts = {subject: account for account, subject in subjects.items()}
        self.keys = {}
        self.refresh_after = 0
        self.lock = threading.Lock()

    def signing_key(self, kid):
        with self.lock:
            if time.monotonic() >= self.refresh_after:
                self.keys = {}
                self.refresh_after = time.monotonic() + 5
                try:
                    entries = read_keys(self.jwks)
                    if not isinstance(entries, list):
                        raise ValueError("Conjunto inválido.")
                    keys = {}
                    for entry in entries:
                        if (entry.get("use", "sig") != "sig" or "verify" not in entry.get("key_ops", ["verify"])
                                or entry.get("alg", self.algorithm) != self.algorithm):
                            continue
                        key_id = entry.get("kid")
                        if not isinstance(key_id, str) or not key_id:
                            continue
                        if key_id in keys:
                            raise ValueError("Identificador de clave repetido.")
                        keys[key_id] = entry
                    self.keys = keys
                    self.refresh_after = time.monotonic() + 60
                except (OSError, URLError, KeyError, ValueError, TypeError, AttributeError):
                    abort(503, "La verificación de identidad está temporalmente indisponible.")
            if not self.keys:
                abort(503, "La verificación de identidad está temporalmente indisponible.")
            entry = self.keys.get(kid)
        if entry is None:
            raise jwt.InvalidTokenError()
        return jwt.PyJWK.from_dict(entry, algorithm=self.algorithm).key

    def verify(self, raw):
        if len(raw) > 16384:
            raise jwt.InvalidTokenError()
        if self.format == "bearer":
            scheme, sep, raw = raw.partition(" ")
            if not sep or scheme.casefold() != "bearer":
                raise jwt.InvalidTokenError()
        header = jwt.get_unverified_header(raw)
        kid = header.get("kid")
        if (header.get("alg") != self.algorithm or header.get("crit")
                or not isinstance(kid, str) or not kid):
            raise jwt.InvalidTokenError()
        claims = jwt.decode(raw, self.signing_key(kid), algorithms=[self.algorithm],
                            issuer=self.issuer, audience=self.audience,
                            options={"require": ["exp", "iat", "iss", "aud", "sub"], "strict_aud": True})
        subject = claims["sub"]
        if subject not in self.accounts:
            abort(403, "La identidad no está vinculada a una cuenta autorizada.")
        binding = hashlib.sha256((self.issuer + "\0" + subject).encode()).hexdigest()
        return self.accounts[subject], binding


def configure(app, accounts):
    settings = {k.removeprefix("RECLAMOS_SITES_"): v for k, v in os.environ.items()
                if k.startswith("RECLAMOS_SITES_")}
    if not settings:
        return
    if settings.get("IDENTITY") != "oidc" or not app.config["PUBLIC"]:
        raise RuntimeError("La identidad de Sitios requiere el modo oidc y RECLAMOS_PUBLIC=1.")
    try:
        app.extensions["sites_identity"] = SiteIdentity(settings, accounts)
    except (KeyError, TypeError, ValueError):
        raise RuntimeError("Revisa la configuración privada RECLAMOS_SITES del servidor.") from None


def check_request():
    verifier = current_app.extensions.get("sites_identity")
    if verifier is None:
        return
    raw = request.headers.get(verifier.header, "")
    if not raw:
        abort(401, "El proyecto debe proporcionar una identidad firmada.")
    try:
        account, binding = verifier.verify(raw)
    except (jwt.PyJWTError, ValueError, TypeError, OverflowError):
        abort(401, "La identidad no se pudo verificar.")
    if session.get("sid") and (session.get("sites_binding") != binding or role() != account):
        session.clear()
        abort(403, "La sesión no corresponde a la identidad del proyecto. Inicia sesión de nuevo.")
    g.sites_account = account
    g.sites_binding = binding
