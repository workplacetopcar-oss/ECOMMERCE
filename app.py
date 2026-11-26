import os
import secrets
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import NamedTemporaryFile
from zoneinfo import ZoneInfo

from flask import Flask, g, jsonify, redirect, render_template, request
from werkzeug.exceptions import HTTPException

import auth
from common import db, role

ROOT = Path(__file__).resolve().parent


def session_key(directory):
    path = directory / "session.key"
    if not path.exists():
        with NamedTemporaryFile(dir=directory) as temp:
            temp.write(secrets.token_bytes(48))
            temp.flush()
            try:
                os.link(temp.name, path)
            except FileExistsError:
                pass
    key = path.read_bytes()
    if len(key) < 32:
        raise RuntimeError("Revisa la clave de sesión en el volumen privado.")
    return key


def local_date(value):
    if not value:
        return "—"
    try:
        date = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return date.astimezone(ZoneInfo("America/Bogota")).strftime("%d/%m/%Y %H:%M")
    except (ValueError, TypeError):
        return value


def create_app(config=None):
    import cases
    import catalog
    import history
    import metrics

    app = Flask(__name__)
    public = os.environ.get("RECLAMOS_PUBLIC") == "1"
    app.config.from_mapping(DATA_DIR=os.environ.get("RECLAMOS_DATA_DIR", str(ROOT / "instance")),
                            PUBLIC=public, SESSION_COOKIE_NAME="reclamos_session",
                            SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                            PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
                            SESSION_REFRESH_EACH_REQUEST=False,
                            MAX_CONTENT_LENGTH=32 * 1024 * 1024)
    if config:
        app.config.update(config)
    directory = Path(app.config["DATA_DIR"]).resolve()
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    app.config.setdefault("DATABASE", str(directory / "reclamos.sqlite3"))
    app.config["SESSION_COOKIE_SECURE"] = app.config["PUBLIC"]
    app.secret_key = app.secret_key or session_key(directory)
    app.jinja_env.globals["csrf_token"] = auth.csrf_token
    app.jinja_env.filters["bogota"] = local_date
    for module in (auth, cases, catalog, history, metrics):
        app.register_blueprint(module.bp)
    auth.register_cli(app)

    @app.teardown_appcontext
    def close_connection(_error=None):
        conn = g.pop("db", None)
        if conn:
            conn.close()

    with app.app_context():
        conn = db()
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript((ROOT / "migrations" / "001_access.sql").read_text())
        for init in (cases.init_cases, catalog.init_catalog, history.init_history, metrics.init_metrics):
            init(conn)

    @app.before_request
    def private_entry():
        if request.endpoint == "static":
            return None
        if request.endpoint != "auth.login" and not role():
            if request.path.startswith("/api/"):
                return jsonify(error="Inicia sesión para continuar."), 401
            return redirect("/login")
        auth.check_csrf()
        return None

    @app.after_request
    def protect(response):
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob:; object-src 'none'; frame-ancestors 'none'; "
            "base-uri 'none'; form-action 'self'"
        )
        if app.config["PUBLIC"]:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    @app.errorhandler(HTTPException)
    def http_error(error):
        if request.path.startswith("/api/"):
            return jsonify(error=error.description), error.code
        return render_template("error.html", error=error), error.code

    @app.get("/")
    def portal():
        return render_template("app.html", role=role(), person=g.identity["person"])

    return app


if __name__ == "__main__":
    from waitress import serve
    serve(create_app(), host="127.0.0.1", port=int(os.environ.get("PORT", 8001)))
