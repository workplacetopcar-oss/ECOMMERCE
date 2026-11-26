"""Authenticated transport used only by the server for private D1/R2 bindings."""
import hashlib
import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from werkzeug.exceptions import Conflict, ServiceUnavailable


def storage_url():
    return os.environ.get("RECLAMOS_STORAGE_URL", "").rstrip("/")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def gateway(path, *, method="GET", data=None, binary=False):
    base = storage_url()
    url = urlsplit(base)
    local = url.hostname in {"127.0.0.1", "localhost", "::1"}
    if (not url.hostname or url.username or url.password or url.query or url.fragment
            or not (url.scheme == "https" or local and url.scheme == "http")):
        raise RuntimeError("Configura la URL privada del gateway D1/R2.")
    token = os.environ.get("RECLAMOS_STORAGE_TOKEN")
    if not token:
        raise RuntimeError("Falta el secreto privado del gateway D1/R2.")
    payload = data if isinstance(data, bytes) or data is None else json.dumps(
        data, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
    req = Request(base + path, data=payload, method=method, headers={
        "Authorization": "Bearer " + token,
        "Content-Type": "application/octet-stream" if isinstance(data, bytes) else "application/json"})
    handlers = [NoRedirect()]
    if local:
        handlers.append(ProxyHandler({}))
    try:
        with build_opener(*handlers).open(req, timeout=60) as response:
            body = response.read()
        return body if binary else (json.loads(body) if body else {})
    except HTTPError as exc:
        if exc.code == 409:
            raise Conflict("Otra sesión guardó cambios. Consulta la versión actual y vuelve a confirmar.") from None
        raise ServiceUnavailable(f"El almacenamiento privado devolvió HTTP {exc.code}.") from None
    except (OSError, URLError, ValueError):
        raise ServiceUnavailable("No se pudo verificar el almacenamiento privado.") from None


def save_object(data):
    digest = hashlib.sha256(data).hexdigest()
    gateway("/blobs/" + digest, method="PUT", data=data, binary=True)
    return digest


def read_object(digest):
    if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
        raise ServiceUnavailable("La referencia privada del archivo es inválida.")
    data = gateway("/blobs/" + digest, binary=True)
    if hashlib.sha256(data).hexdigest() != digest:
        raise ServiceUnavailable("El archivo recibido difiere de la copia registrada.")
    return data


def pack_blob(content):
    return "r2:" + save_object(bytes(content)) if storage_url() else content


def unpack_blob(value):
    if isinstance(value, str) and value.startswith("r2:"):
        return read_object(value[3:])
    return value
