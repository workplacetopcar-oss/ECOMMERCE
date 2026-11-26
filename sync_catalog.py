"""Refresh the two national RSales masters through a configured read-only adapter.

Public entry point for a scheduled run: python sync_catalog.py --db /private/reclamos.sqlite3
The adapter supplies buscar_productos / buscar_clientes, with keyword-only pagina and
tamano_pagina. It normalizes its authorized provider results to {items,pagina_siguiente}
and marks its final unfiltered page with maestro_completo=True and total_unicos.
"""
import argparse
import json
import os
import re
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from catalog import PRODUCT_FIELDS, client_record, client_values, encode, indexed, init_catalog, norm
from common import now, transaction
from sync_orders import load_adapter


def collect(fetch_page, size):
    rows, seen = {}, set()
    cursor = 1
    while True:
        marker = encode(cursor)
        if marker in seen:
            raise ValueError("El proveedor repitió una página del maestro.")
        seen.add(marker)
        page = fetch_page(pagina=cursor,tamano_pagina=size)
        if not isinstance(page.get("items"),list):
            raise ValueError("Página de maestro incompleta.")
        for item in page["items"]:
            code = item.get("code")
            if not isinstance(code,str) or not code.strip():
                raise ValueError("Se requiere código textual estable por registro.")
            if code in rows and rows[code] != item:
                raise ValueError("El maestro contiene un código con valores contradictorios.")
            rows[code] = item
        cursor = page.get("pagina_siguiente")
        if cursor is None:
            if page.get("maestro_completo") is not True or page.get("total_unicos") != len(rows):
                raise ValueError("El proveedor no confirmó la cobertura del maestro.")
            return list(rows.values()), len(seen)


def refresh_masters(conn, adapter, page_size=100):
    if not 1 <= page_size <= 500:
        raise ValueError("El tamaño de página debe estar entre 1 y 500.")
    if not callable(getattr(adapter, "buscar_clientes", None)) or not callable(getattr(adapter, "buscar_productos", None)):
        raise ValueError("El adaptador requiere lecturas paginadas autorizadas de clientes y productos.")
    previous = conn.execute("SELECT loaded_at,metadata FROM catalog_snapshot").fetchone()
    if not previous:
        raise ValueError("Carga primero el corte privado con el directorio de exportación y sus fichas.")
    meta = json.loads(previous[1])
    rid = conn.execute("INSERT INTO catalog_runs(account,started,state) VALUES(?,?,'leyendo')", (adapter.account,now())).lastrowid
    try:
        if adapter.estado_conexion().get("solo_lectura") is not True:
            raise ValueError("No se verificó la conexión de solo lectura.")
        products,np = collect(adapter.buscar_productos,page_size)
        pa = now()
        clients,nc = collect(adapter.buscar_clientes,page_size)
        ca = now()
        counts, labels = {},{}
        def product_rows():
            for raw in products:
                product = {k:raw[k] for k in PRODUCT_FIELDS}
                if not product["description"]:
                    raise ValueError("Falta la descripción de un producto.")
                label = re.search(r"\[\s*(CAJA|UNIDAD|DISPLAY|TAMBOR|TANQUE|BALDE)\s*\]",product["description"],re.I)
                label = label[1].upper() if label else None
                product["presentacion_etiquetada"] = label
                counts[product["state"]] = counts.get(product["state"],0)+1
                labels[label or "Sin etiqueta"] = labels.get(label or "Sin etiqueta",0)+1
                yield (product["code"],product["state"]=="Y",indexed(*product.values()),encode(product))
        confirmed = 0
        def client_rows():
            nonlocal confirmed
            for raw in clients:
                row = {k:raw.get(k) for k in ("code","name","name_est","short_name","city","region","country","state")}
                if not isinstance(row["name"],str) or not row["name"].strip():
                    raise ValueError("Falta la razón social de un cliente.")
                row["pais_confirmado"] = "Colombia" if norm(row["country"]).strip() == "colombia" else None
                confirmed += int(bool(row["pais_confirmado"]))
                yield client_values(client_record(row, "nacional"), "nacional")
        with transaction(conn):
            if conn.execute("SELECT loaded_at FROM catalog_snapshot").fetchone()[0] != previous[0]:
                raise ValueError("Otra carga publicó un corte. Reinicia la lectura.")
            conn.execute("DELETE FROM catalog_products")
            conn.execute("DELETE FROM catalog_clients WHERE channel='nacional' AND custom=0")
            conn.executemany("INSERT INTO catalog_products VALUES(?,?,?,?)", product_rows())
            conn.executemany("INSERT INTO catalog_clients(channel,id,name,verified,search,data) VALUES(?,?,?,?,?,?)",client_rows())
            meta.update(fecha=datetime.now(ZoneInfo("America/Bogota")).date().isoformat(),
                        productos=dict(consulta_final_utc=pa,paginas=np,codigos=len(products),estado=counts,
                                       presentacion=labels,campos_originales=list(PRODUCT_FIELDS)),
                        clientes_rsales=dict(consulta_final_utc=ca,paginas=nc,codigos=len(clients),
                                             colombia_explicito=confirmed,pais_por_verificar=len(clients)-confirmed))
            conn.execute("UPDATE catalog_snapshot SET metadata=?,loaded_at=?", (encode(meta),now()))
            conn.execute("UPDATE catalog_runs SET finished=?,state='completa',pages=?,products=?,clients=? WHERE id=?",
                         (now(), np+nc,len(products),len(clients),rid))
        return rid
    except Exception as exc:
        conn.execute("UPDATE catalog_runs SET finished=?,state='parcial',error=? WHERE id=?",(now(),type(exc).__name__,rid))
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Refresca productos y clientes por el adaptador de lectura privado")
    parser.add_argument("--db",required=True)
    args = parser.parse_args()
    os.umask(0o077)
    conn = sqlite3.connect(args.db,isolation_level=None,timeout=15)
    try:
        init_catalog(conn)
        print("Corte completo. Corrida",refresh_masters(conn,load_adapter()))
    finally:
        conn.close()
