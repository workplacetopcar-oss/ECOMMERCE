# Despliegue privado de Gestión de Reclamos

Un único servidor Linux mantiene la base SQLite y los adjuntos. Nacional, Internacional, Calidad y Administración pueden entrar desde el navegador del celular o del computador usando el mismo dominio. Caddy recibe las conexiones HTTPS. Waitress y el puerto 8000 quedan en la red interna de Docker. Cada ingreso requiere la clave de la cuenta y el código temporal de su autenticador.

## Preparar el servidor

Usa un servidor persistente con Docker Engine y Docker Compose v2, disco cifrado, reloj sincronizado y espacio para los adjuntos y respaldos. En DNS, apunta el dominio elegido a la IP del servidor. Si hay un registro AAAA, comprueba también su ruta IPv6. Para el certificado público automático, Caddy necesita poder salir al servicio de certificados y recibir conexiones en los puertos TCP 80 y 443. Ejecuta los comandos desde la raíz del proyecto. El dominio se escribe sin protocolo, puerto ni ruta.

## Alta de los segundos factores

El siguiente paso crea `.env` en el servidor con una semilla aleatoria diferente de 160 bits para cada cuenta. Ejecútalo desde Bash en la primera instalación. Registra cada semilla en la aplicación autenticadora de su titular por un canal privado, con TOTP, SHA-1, seis dígitos y periodo de 30 segundos. Conserva la recuperación en el gestor de secretos de la organización. Quien administra Docker en el servidor puede acceder a estas variables.

```bash
read -rp 'Dominio de reclamos: ' dominio_reclamos
(
  set -e -C
  umask 077
  docker run --rm -i --network none \
    -e RECLAMOS_DOMAIN="$dominio_reclamos" \
    python:3.13-slim-bookworm@sha256:f0fa218d5b06f9b9235024fa061cde2cb141f726de52134471053d2d9b5324a3 python - > .env <<'PY'
import base64
import os
import secrets

print("RECLAMOS_DOMAIN=" + os.environ["RECLAMOS_DOMAIN"])
for cuenta in ("NACIONAL", "INTERNACIONAL", "CALIDAD", "ADMINISTRADOR"):
    semilla = base64.b32encode(secrets.token_bytes(20)).decode("ascii")
    print(f"RECLAMOS_TOTP_{cuenta}={semilla}")
PY
)
docker compose config --quiet
docker compose build
```

## Dar acceso y cargar maestros

Inicializa las tres claves de alta desde la terminal privada del operador. Bash oculta la entrada. Estas variables solo se entregan al contenedor de inicialización. El comando se puede repetir y respeta las claves ya asignadas.

```bash
(
  set -e
  for perfil in NACIONAL INTERNACIONAL ADMIN; do
    read -rsp "Clave inicial para $perfil: " "RECLAMOS_PIN_$perfil"
    echo
    export "RECLAMOS_PIN_$perfil"
  done
  docker compose run --rm --no-deps \
    -e RECLAMOS_PIN_NACIONAL -e RECLAMOS_PIN_INTERNACIONAL -e RECLAMOS_PIN_ADMIN \
    app flask --app app init-accounts
)
```

Don Carlos entra con la cuenta `administrador` y activa `calidad` en la pantalla de accesos asignándole su clave. El titular de Calidad necesita además su propio autenticador.

Sube al servidor por un canal privado el directorio de maestros aprobado. Utiliza una ruta absoluta fuera del checkout y permite al usuario del contenedor (UID 10001) leer los archivos. Para importar o actualizar el corte, monta ese directorio únicamente durante el comando.

```bash
read -rp 'Ruta absoluta de los maestros en el servidor: ' maestros_reclamos
docker compose run --rm --no-deps \
  --volume "$maestros_reclamos:/import:ro" \
  app python catalog.py --db /data/reclamos.sqlite3 --data /import
```

## Arranque y comprobación

```bash
docker compose up -d
docker compose ps
docker compose logs --tail 50 https
```

Espera a que `app` aparezca como `healthy`. Abre el dominio con HTTPS desde una red autorizada en el computador y el celular. Verifica el certificado, la redirección al ingreso, el rechazo de una clave sin código temporal y el acceso con la cuenta y su autenticador. La página de acceso puede ser visible públicamente cuando el dominio es público. Si el despliegue requiere acceso exclusivo por red corporativa, configura el acceso de red y un método de validación del certificado compatible con ella.

## Persistencia y recuperación

El volumen `reclamos_data` contiene el directorio privado de la aplicación, incluida la base y `session.key`. Conserva también los volúmenes `caddy_data` y `caddy_config`, que mantienen el estado de certificados. Usa el mismo directorio o nombre de proyecto de Compose en cada actualización para reutilizar estos volúmenes. `docker compose stop` detiene el servicio y los conserva.

Para obtener un respaldo coherente, detén la aplicación durante la copia o usa una instantánea consistente del servidor. Conserva una copia cifrada y restringida del volumen completo, las semillas, la configuración DNS y los maestros; comprueba la restauración en un servidor aislado. Al cambiar una semilla, entrega el nuevo factor al titular y recrea el contenedor con `docker compose up -d --force-recreate app`.

## Actualización desde RSales

La sincronización en vivo con RSales requiere que el operador configure un adaptador de lectura autorizado, sus credenciales y acceso al origen. El servicio base tiene una red interna sin salida a Internet. Guarda estas integraciones en la configuración privada del servidor.

Después de importar el corte inicial, monta el módulo del adaptador en el contenedor de sincronización, configura su ruta de importación en `PYTHONPATH` y su nombre en `RECLAMOS_RSALES_ADAPTER`. Autoriza la salida al origen solo para ese proceso. Con dicha configuración activa, ejecuta `docker compose run --rm --no-deps app python sync_catalog.py --db /data/reclamos.sqlite3` desde el directorio del proyecto. El adaptador debe proveer las lecturas paginadas de clientes y productos, confirmar los totales y declarar su acceso de solo lectura. El directorio internacional se actualiza mediante el corte privado.
