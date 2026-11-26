"""Private account provisioning, access throttling, sessions and optional TOTP."""
import base64
import binascii
import hashlib
import hmac
import os
import secrets
import struct
import time

import click
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from flask import (Blueprint, abort, current_app, g, redirect, render_template,
                   request, session, url_for)

import sites_identity
from common import db, now, require, role, session_digest, transaction

bp = Blueprint("auth", __name__)
HASHER = PasswordHasher()
PEOPLE = {"nacional": "Zulay", "internacional": "Angélica",
          "calidad": "Juan Diego", "administrador": "Don Carlos"}
# Make timing comparable even for accounts with no verifier.
DUMMY = HASHER.hash(secrets.token_urlsafe(32))


@bp.record_once
def configure_identity(state):
    sites_identity.configure(state.app, PEOPLE)


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


def check_csrf():
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        value = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token", "")
        if not value or not secrets.compare_digest(value.encode(), session.get("csrf", "").encode()):
            abort(400, "La sesión del formulario venció. Vuelve a abrir la página.")


def pin_ok(pin):
    return 4 <= len(pin) <= 128 and pin.strip() == pin


def bootstrap(values):
    with transaction(db()):
        for name, person in PEOPLE.items():
            db().execute("INSERT OR IGNORE INTO accounts(name,person) VALUES (?,?)", (name, person))
            row = db().execute("SELECT verifier FROM accounts WHERE name=?", (name,)).fetchone()
            if row["verifier"] or name == "calidad":
                continue
            key = "RECLAMOS_PIN_" + ("ADMIN" if name == "administrador" else name.upper())
            value = values.get(key, "")
            if not pin_ok(value):
                raise ValueError(f"Configura {key} en el entorno privado del servidor.")
            db().execute("UPDATE accounts SET verifier=?,changed_at=? WHERE name=?",
                         (HASHER.hash(value), now(), name))
            log("alta privada", "servidor", name)


def log(action, actor, target):
    db().execute("INSERT INTO access_log(action,actor,target,created_at) VALUES (?,?,?,?)",
                 (action, actor, target, now()))


def verify_totp(name, code, clock):
    key = os.environ.get("RECLAMOS_TOTP_" + name.upper(), "").replace(" ", "").upper()
    if len(key) < 26 or len(code) != 6 or not code.isascii() or not code.isdigit():
        return None
    try:
        key_bytes = base64.b32decode(key + "=" * (-len(key) % 8))
    except (ValueError, binascii.Error):
        return None
    for counter in range(clock // 30 - 1, clock // 30 + 2):
        digest = hmac.digest(key_bytes, struct.pack(">Q", counter), "sha1")
        offset = digest[-1] & 15
        number = (int.from_bytes(digest[offset:offset + 4], "big") & 0x7fffffff) % 1000000
        if hmac.compare_digest(code, f"{number:06d}"):
            return counter
    return None


def throttle(name, clock):
    addr = request.remote_addr or "privado"
    buckets = [("red:" + addr, 35), ("cuenta:" + name, 6)]
    with transaction(db()):
        db().execute("DELETE FROM login_limits WHERE since<=?", (clock - 900,))
        for bucket, cap in buckets:
            row = db().execute("SELECT attempts FROM login_limits WHERE bucket=?", (bucket,)).fetchone()
            if row and row["attempts"] >= cap:
                return False
        for bucket, _ in buckets:
            db().execute("INSERT INTO login_limits VALUES (?,1,?) ON CONFLICT(bucket) "
                         "DO UPDATE SET attempts=attempts+1", (bucket, clock))
    return True


@bp.route("/login", methods=["GET", "POST"])
def login():
    error = None
    status = 200
    if request.method == "POST":
        name = request.form.get("username", "").strip().lower()
        if getattr(g, "sites_account", name) != name:
            abort(403, "La identidad del proyecto no corresponde a esta cuenta.")
        account = db().execute("SELECT verifier FROM accounts WHERE name=?", (name,)).fetchone()
        verifier = account["verifier"] if account else None
        pin = request.form.get("password", "")
        clock = int(time.time())
        if not throttle(name if name in PEOPLE else "desconocida", clock):
            error, status = "Demasiados intentos. Intenta de nuevo en 15 minutos.", 429
        else:
            try:
                verified = HASHER.verify(verifier or DUMMY, pin[:129]) and bool(verifier)
            except VerificationError:
                verified = False
            counter = None
            if verified and current_app.config["PUBLIC"]:
                counter = verify_totp(name, request.form.get("otp", ""), clock)
                verified = counter is not None
            if verified:
                with transaction(db()):
                    if counter is not None:
                        used = db().execute("INSERT INTO used_totp VALUES (?,?) ON CONFLICT(account) "
                                            "DO UPDATE SET counter=excluded.counter WHERE counter<excluded.counter",
                                            (name, counter)).rowcount
                        if not used:
                            abort(401, "El código temporal ya se utilizó.")
                    # A reset racing with a password check must invalidate that login.
                    if db().execute("SELECT verifier FROM accounts WHERE name=?", (name,)).fetchone()[0] != verifier:
                        abort(401)
                    db().execute("DELETE FROM login_limits WHERE bucket=?", ("cuenta:" + name,))
                    db().execute("UPDATE login_limits SET attempts=max(0,attempts-1) WHERE bucket=?",
                                 ("red:" + (request.remote_addr or "privado"),))
                    db().execute("DELETE FROM sessions WHERE expires<=?", (clock,))
                    if session.get("sid"):
                        db().execute("DELETE FROM sessions WHERE digest=?", (session_digest(session["sid"]),))
                    session.clear()
                    token = secrets.token_urlsafe(32)
                    db().execute("INSERT INTO sessions VALUES (?,?,?)",
                                 (session_digest(token), name, clock + 8 * 3600))
                    session["sid"] = token
                    if "sites_binding" in g:
                        session["sites_binding"] = g.sites_binding
                    session.permanent = True
                    log("ingreso", name, name)
                return redirect("/indicadores" if name == "administrador" else "/")
            error, status = "Revisa tus datos de acceso.", 401
    return render_template("login.html", error=error, public=current_app.config["PUBLIC"]), status


@bp.post("/logout")
def logout():
    if session.get("sid"):
        db().execute("DELETE FROM sessions WHERE digest=?", (session_digest(session["sid"]),))
    session.clear()
    return redirect(url_for("auth.login"))


@bp.route("/accesos", methods=["GET", "POST"])
@require("administrador")
def access():
    message = None
    if request.method == "POST":
        target = request.form.get("account", "")
        pin = request.form.get("password", "")
        if target not in PEOPLE or not pin_ok(pin):
            abort(400, "Selecciona una cuenta y escribe una clave de 4 a 128 caracteres.")
        with transaction(db()):
            db().execute("UPDATE accounts SET verifier=?,changed_at=? WHERE name=?",
                         (HASHER.hash(pin), now(), target))
            db().execute("DELETE FROM sessions WHERE account=?", (target,))
            db().execute("DELETE FROM login_limits WHERE bucket=?", ("cuenta:" + target,))
            log("asignación de clave", role(), target)
        if target == "administrador":
            session.clear()
            return redirect("/login")
        message = "Clave guardada. La persona ya puede ingresar con su nueva clave."
    accounts = db().execute("SELECT name,person,verifier IS NOT NULL AS active FROM accounts ORDER BY rowid")
    events = db().execute("SELECT * FROM access_log ORDER BY id DESC LIMIT 100")
    return render_template("access.html", accounts=accounts, events=events, message=message)


def register_cli(app):
    @app.cli.command("init-accounts")
    def init_accounts():
        try:
            bootstrap(os.environ)
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo("Accesos preparados.")
