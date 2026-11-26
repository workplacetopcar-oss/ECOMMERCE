FROM python:3.13-slim-bookworm@sha256:f0fa218d5b06f9b9235024fa061cde2cb141f726de52134471053d2d9b5324a3 AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 RECLAMOS_DATA_DIR=/data
WORKDIR /app
COPY requirements.txt .
RUN --mount=type=secret,id=proxy_ca \
    if [ -s /run/secrets/proxy_ca ]; then export PIP_CERT=/run/secrets/proxy_ca; fi \
    && pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid 10001 reclamos \
    && useradd --uid 10001 --gid 10001 --no-create-home reclamos \
    && install -d -m 0700 -o reclamos -g reclamos /data

FROM runtime
COPY --chown=10001:10001 app.py auth.py blobs.py cases.py catalog.py cloud_db.py common.py contacto.py credit_notes.py evidencia.py \
    history.py metrics.py sites_identity.py sync_catalog.py sync_orders.py ./
COPY --chown=10001:10001 migrations/ migrations/
COPY --chown=10001:10001 templates/ templates/
COPY --chown=10001:10001 static/ static/
USER 10001:10001
EXPOSE 8000
CMD ["waitress-serve", "--listen=0.0.0.0:8000", "--url-scheme=https", "--call", "app:create_app"]
