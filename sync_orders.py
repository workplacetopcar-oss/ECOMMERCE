"""Read an authorized, deployment-provided RSales adapter; publish only complete windows."""
import importlib
import json
import os
import secrets
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

BOGOTA = ZoneInfo("America/Bogota")
CHANNELS = ("nacional", "internacional", "por_clasificar")


def local_day(value):
    raw = str(value or "")
    if len(raw) == 10:
        return date.fromisoformat(raw)
    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    return (dt if dt.tzinfo else dt.replace(tzinfo=BOGOTA)).astimezone(BOGOTA).date()


def init_orders(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS rs_orders(
      id INTEGER PRIMARY KEY, account TEXT NOT NULL, code TEXT NOT NULL, number TEXT NOT NULL,
      type TEXT NOT NULL, channel TEXT NOT NULL, created TEXT NOT NULL, data TEXT NOT NULL,
      shipped_at TEXT, active INTEGER NOT NULL DEFAULT 1, UNIQUE(account,type,code));
    CREATE INDEX IF NOT EXISTS rs_number ON rs_orders(number,channel);
    CREATE INDEX IF NOT EXISTS rs_code ON rs_orders(code,channel);
    CREATE INDEX IF NOT EXISTS rs_created ON rs_orders(created,channel);
    CREATE TABLE IF NOT EXISTS rs_runs(
      id INTEGER PRIMARY KEY, account TEXT NOT NULL, start TEXT NOT NULL, end TEXT NOT NULL,
      page_size INTEGER NOT NULL, cursor TEXT, pages INTEGER NOT NULL DEFAULT 0,
      state TEXT NOT NULL, began TEXT NOT NULL, checked TEXT, finished TEXT, error TEXT,
      scope TEXT NOT NULL, lease TEXT, lease_until TEXT);
    CREATE TABLE IF NOT EXISTS rs_pages(
      run INTEGER NOT NULL, cursor TEXT NOT NULL, PRIMARY KEY(run,cursor));
    CREATE TABLE IF NOT EXISTS rs_stage(
      run INTEGER NOT NULL, code TEXT NOT NULL, doc TEXT NOT NULL, PRIMARY KEY(run,code));
    CREATE TABLE IF NOT EXISTS rs_settings(
      id INTEGER PRIMARY KEY CHECK(id=1), target_days INTEGER NOT NULL, calendar TEXT NOT NULL);
    INSERT OR IGNORE INTO rs_settings VALUES(1,15,'calendario');
    """)


def load_adapter():
    path = os.getenv("RECLAMOS_RSALES_ADAPTER")
    if not path:
        raise ValueError("Falta configurar el adaptador privado de lectura.")
    adapter = importlib.import_module(path)
    if not isinstance(adapter.account, str) or not adapter.account.strip():
        raise ValueError("Falta la cuenta de origen verificada del adaptador.")
    return adapter


def stamp():
    return datetime.now(timezone.utc).isoformat()


def run_sync(conn, adapter, start, end, page_size=100):
    """Adapter contract in READMEs. Each checkpoint and its next cursor commit together."""
    start, end = date.fromisoformat(start), date.fromisoformat(end)
    if start > end or end > datetime.now(BOGOTA).date():
        raise ValueError("Revisa las fechas de creación de pedidos.")
    scope = sorted(set(adapter.covered_channels))
    if any(c not in CHANNELS for c in scope):
        raise ValueError("Cobertura del adaptador inválida.")
    owner = secrets.token_hex(16)
    def lease_end():
        return (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
    conn.execute("BEGIN IMMEDIATE")
    try:
        if conn.execute("SELECT 1 FROM rs_runs WHERE account=? AND lease_until>? LIMIT 1",
                        (adapter.account, stamp())).fetchone():
            raise ValueError("Ya hay una lectura en curso para esta cuenta.")
        run = conn.execute("""SELECT id,cursor FROM rs_runs WHERE account=? AND start=? AND end=?
                              AND page_size=? AND scope=? AND state IN ('parcial','leyendo')
                              ORDER BY id DESC LIMIT 1""",
                           (adapter.account, start.isoformat(), end.isoformat(), page_size, json.dumps(scope))).fetchone()
        if run:
            rid, serialized = run
            cursor = json.loads(serialized)
            conn.execute("UPDATE rs_runs SET state='leyendo',lease=?,lease_until=?,error=NULL WHERE id=?",
                         (owner, lease_end(), rid))
        else:
            cursor = 1
            rid = conn.execute("""INSERT INTO rs_runs(account,start,end,page_size,cursor,state,began,scope,lease,lease_until)
                                  VALUES(?,?,?,?,?,'leyendo',?,?,?,?)""",
                               (adapter.account, start.isoformat(), end.isoformat(), page_size, "1", stamp(),
                                json.dumps(scope), owner, lease_end())).lastrowid
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    try:
        # The private adapter establishes read-only access and supplies verified channel mappings.
        if adapter.estado_conexion().get("solo_lectura") is not True:
            raise ValueError("Verifica el modo de solo lectura en la conexión.")
        while True:
            result = adapter.listar_pedidos(tipo="P", desde=start.isoformat(), hasta=end.isoformat(),
                                            pagina=cursor, tamano=page_size)
            docs = result["pedidos"]
            following = result["pagina_siguiente"]
            if not isinstance(docs, list):
                raise ValueError("La página recibida tiene un formato inesperado.")
            if following is None and result.get("periodo_completo") is not True:
                raise ValueError("El proveedor no confirma la cobertura de esta ventana.")
            conn.execute("BEGIN IMMEDIATE")
            if not conn.execute("SELECT 1 FROM rs_runs WHERE id=? AND lease=?", (rid, owner)).fetchone():
                raise ValueError("Otra ejecución retomó esta lectura.")
            conn.execute("INSERT INTO rs_pages VALUES(?,?)", (rid, json.dumps(cursor)))
            for d in docs:
                if d["type"] != "P" or not start <= local_day(d["created_at"]) <= end:
                    continue
                if not isinstance(d["code"], str) or not d["code"].strip():
                    raise ValueError("Pedido sin identidad estable.")
                ch = d.get("canal_verificado") or "por_clasificar"
                if ch not in CHANNELS:
                    raise ValueError("La asignación de canal requiere revisión.")
                # The deployment adapter may supply actual departure only with documentary proof.
                if d.get("fecha_salida_verificada") and d.get("soporte_salida"):
                    local_day(d["fecha_salida_verificada"])
                conn.execute("INSERT OR REPLACE INTO rs_stage VALUES(?,?,?)", (rid,d["code"],json.dumps(d)))
            if following is not None and conn.execute("SELECT 1 FROM rs_pages WHERE run=? AND cursor=?",
                                                        (rid,json.dumps(following))).fetchone():
                raise ValueError("El proveedor repitió el cursor de paginación.")
            conn.execute("UPDATE rs_runs SET pages=pages+1,cursor=?,checked=?,lease_until=? WHERE id=?",
                         (json.dumps(following),stamp(),lease_end(),rid))
            if following is None:
                conn.execute("UPDATE rs_orders SET active=0 WHERE account=? AND created BETWEEN ? AND ?",
                             (adapter.account,start.isoformat(),end.isoformat()))
                for row in conn.execute("SELECT doc FROM rs_stage WHERE run=?", (rid,)):
                    d = json.loads(row[0])
                    conn.execute("""INSERT INTO rs_orders(account,code,number,type,channel,created,data,shipped_at)
                                  VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(account,type,code) DO UPDATE SET
                                  number=excluded.number,channel=excluded.channel,created=excluded.created,
                                  data=excluded.data,shipped_at=excluded.shipped_at,active=1""",
                                 (adapter.account,d["code"],str(d.get("order_number") or ""),"P",
                                  d.get("canal_verificado") or "por_clasificar",local_day(d["created_at"]).isoformat(),
                                  row[0],d.get("fecha_salida_verificada") if d.get("soporte_salida") else None))
                conn.execute("UPDATE rs_runs SET state='completa',finished=?,lease=NULL,lease_until=NULL WHERE id=?",
                             (stamp(),rid))
                conn.execute("DELETE FROM rs_stage WHERE run=?",(rid,))
            conn.commit()
            if following is None:
                return rid
            cursor = following
    except Exception as exc:
        conn.rollback()
        # Do not persist provider messages, which can include URLs, credentials or customer data.
        conn.execute("""UPDATE rs_runs SET state='parcial',error=?,lease=NULL,lease_until=NULL
                        WHERE id=? AND lease=?""", ("Lectura incompleta · "+type(exc).__name__,rid,owner))
        raise


def refresh(conn, adapter, days=7):
    """For a periodically invoked scheduler, revisit the recent window and all known open dates."""
    if days < 1:
        raise ValueError("La ventana debe ser positiva.")
    end = datetime.now(BOGOTA).date()
    start = end - timedelta(days=days-1)
    ids = [run_sync(conn,adapter,start.isoformat(),end.isoformat())]
    # Only use a normalized, business-verified indication that an order is closed.
    for row in conn.execute("""SELECT DISTINCT created FROM rs_orders WHERE account=? AND active=1 AND
                               created<? AND COALESCE(json_extract(data,'$.cerrado_verificado'),0)<>1
                               ORDER BY created""",(adapter.account,start.isoformat())).fetchall():
        ids.append(run_sync(conn,adapter,row[0],row[0]))
    return ids
