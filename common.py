"""Request-local connections and authorization shared by the private module."""
import hashlib
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import wraps

from flask import abort, current_app, g, session


def now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def db():
    if "db" not in g:
        conn = sqlite3.connect(current_app.config["DATABASE"], timeout=15, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        g.db = conn
    return g.db


@contextmanager
def transaction(conn):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def session_digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def role():
    if "identity" not in g:
        token = session.get("sid")
        g.identity = None if not token else db().execute(
            "SELECT a.name,a.person FROM accounts a JOIN sessions s ON s.account=a.name "
            "WHERE digest=? AND expires>?", (session_digest(token), int(time.time()))
        ).fetchone()
    return g.identity["name"] if g.identity else None


def require(*roles):
    def decorate(view):
        @wraps(view)
        def guarded(*args, **kwargs):
            identity = role()
            if not identity:
                abort(401)
            if roles and identity not in roles:
                abort(403)
            return view(*args, **kwargs)
        return guarded
    return decorate
