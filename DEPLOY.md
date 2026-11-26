# Despliegue privado de Gestión de Reclamos

## Publicación en Sitios de ChatGPT

El destino solicitado es un Sitio privado con el slug preferido `gestion-de-reclamos`. El dominio y el identificador de versión se registran cuando Sitios confirma la publicación. Esta máquina ofrece una utilidad de previsualización local; al comprobar su estado, el servicio de previsualización estaba indisponible. La herramienta de configuración del entorno de desarrollo solo administra ese entorno. Desde esta sesión quedaron pendientes la creación o selección del proyecto de Sitios, la asignación de los recursos y la publicación.

**Qué verificar en el proyecto de destino**

1. Obtener el contrato oficial de ejecución y despliegue que muestre el proyecto. Identificar qué código puede ejecutarse en el servidor, cómo se configuran secretos, cómo se aplican las migraciones y cómo se restringe la audiencia. La compatibilidad de la aplicación con ese runtime requiere una prueba en el proyecto.
2. Obtener el contrato oficial de identidad. Identificar el identificador estable de cada persona y el mecanismo que permite al servidor comprobar quién realizó una solicitud. Si el destino entrega tokens firmados, verificar el emisor, la audiencia, las claves de verificación y la vigencia que especifique ese contrato. Si usa una sesión administrada, usar la API oficial de verificación. Estos parámetros dependen del proyecto. El navegador nunca decide el perfil.
3. Vincular por un canal privado las cuatro identidades confirmadas con Nacional, Internacional, Calidad y Administración. Configurar la audiencia restringida en Sitios, comprobar el rechazo de sesiones desconocidas y verificar que cada identidad accede solo a los expedientes y adjuntos autorizados. La cuenta de Calidad requiere activación por Administración.
4. Verificar los enlaces de recursos del proyecto a su base D1 y su almacenamiento R2 privado. Aplicar las migraciones a esa D1, probar reintentos y concurrencia, subir y recuperar una evidencia desde R2 con autorización por expediente. Comprobar que un usuario de otro canal carece de acceso y que la evidencia sigue disponible al reabrir el expediente desde otro dispositivo.
5. Con la versión publicada, registrar el dominio HTTPS y la versión entregados por Sitios, el commit, el resultado por perfil y el resultado de la lectura autorizada de RSales. También probar el rechazo de un visitante ajeno a la audiencia usando la URL publicada.

[Especificación de publicación y aceptación en GitHub](https://github.com/workplacetopcar-oss/ECOMMERCE/issues/1)

### Verificador opcional de identidad firmada

Si el proyecto entrega un JWT OIDC firmado en todas las solicitudes al servidor, esta aplicación puede comprobarlo antes de procesar la entrada, las APIs, los archivos estáticos y las descargas. El contrato real de Sitios sigue pendiente de confirmación. Configura en el proceso de la aplicación los valores que indique el proyecto y entrega el mapa de sujetos por un canal privado.

| Variable | Configuración del operador |
|---|---|
| `RECLAMOS_PUBLIC` | `1` para conservar el segundo factor y las cookies seguras. |
| `RECLAMOS_SITES_IDENTITY` | `oidc` para activar la verificación. |
| `RECLAMOS_SITES_JWT_HEADER` | Nombre exacto de la cabecera que entrega el token firmado en cada solicitud. |
| `RECLAMOS_SITES_JWT_FORMAT` | `raw` para el JWT directo o `bearer` para el formato HTTP Bearer. |
| `RECLAMOS_SITES_ISSUER` | Emisor HTTPS exacto del proyecto. |
| `RECLAMOS_SITES_AUDIENCE` | Identificador de audiencia exacto. Esta integración exige un único `aud` de tipo texto. |
| `RECLAMOS_SITES_JWKS_URL` | URL HTTPS directa del conjunto de claves de firma del emisor. El proceso necesita acceso a esa URL y certificados de confianza. |
| `RECLAMOS_SITES_ALGORITHM` | Algoritmo asimétrico publicado por el emisor. Se admite `RS256`, `PS256` o `ES256`. |
| `RECLAMOS_SITES_SUBJECTS` | Objeto JSON con las claves `nacional`, `internacional`, `calidad` y `administrador`. El valor de cada una es el `sub` individual confirmado. |

El JWT debe incluir `kid`, firma válida y los campos `iss`, `aud`, `sub`, `iat` y `exp`. La aplicación verifica también `nbf` si está presente. El proveedor debe publicar sus nuevas claves con antelación; el servidor conserva el conjunto verificado durante un máximo de 60 segundos. Configurar parcialmente el modo de identidad impide que la aplicación arranque.

Tras validar la identidad firmada, el servidor exige la clave local y el TOTP de la cuenta vinculada. Una sesión abierta con otra identidad requiere un nuevo ingreso. El perfil Calidad se habilita desde Accesos. Para el flujo local previo, deja sin definir las variables `RECLAMOS_SITES_*`.

Si habilitas este modo en un despliegue con Docker, pasa estas variables mediante su configuración privada, autoriza el acceso HTTPS al JWKS y ajusta el control de salud. En un contenedor sin la cabecera firmada, `GET /login` debe responder 401. La prueba funcional de entrada se realiza a través del destino que entrega la identidad.

## Ejecución en un servidor Linux con Docker

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

## Almacenamiento compartido D1 y R2

El servicio Python accede a un Worker de almacenamiento privado. D1 guarda las tablas del sistema y una secuencia de revisiones. R2 guarda los archivos y los diarios comprimidos que permiten reconstruir una caché. Cada confirmación de escritura aplica un lote atómico en D1 condicionado a la revisión leída. Si otro proceso ya la modificó, la aplicación devuelve un conflicto recuperable. Las lecturas necesitan acceso al gateway para verificar la última revisión. Todos los procesos de la aplicación y las herramientas administrativas que escriben en esa base deben pasar por este gateway.

| Configuración privada | Función |
|---|---|
| `RECLAMOS_STORAGE_URL` | URL HTTPS del Worker accesible desde el servidor de la aplicación. El desarrollo permite HTTP en loopback. |
| `RECLAMOS_STORAGE_TOKEN` | Secreto del servidor para autenticarse con el Worker. Debe corresponder a su secreto `STORAGE_TOKEN`. |
| `RECLAMOS_SESSION_KEY` | Secreto aleatorio de al menos 32 caracteres común a todas las instancias de la misma aplicación. |
| `RECLAMOS_DATA_DIR` | Directorio privado escribible para la caché de lectura y transacciones. La primera conexión aplica el esquema y recupera las revisiones ya confirmadas. |
| `RECLAMOS_DEPLOYMENT` | En el destino de Sitios, usar `sites`. El arranque exige acceso público protegido, gateway HTTPS e identidad de Sitios configurada. |

El directorio privado de 20 clientes, los productos y el corte nacional se cargan con el mismo comando de catálogo del README, utilizando las variables de almacenamiento del proyecto. La cuenta de servicio debe poder leer los archivos privados durante la importación. El gateway conserva las referencias en D1 y recibe los nuevos adjuntos en R2 antes de confirmar el expediente. Una restauración de desastre requiere respaldar ambos recursos con un corte coherente. Un objeto subido antes de una transacción fallida puede quedar sin referencia. La base SQLite de un despliegue anterior se conserva en su ruta original y necesita una migración explícita si contiene casos operativos.

La configuración del Worker incluida es local. El administrador del destino debe seleccionar los bindings reales `DB` y `FILES`, establecer el secreto e identificar una ruta privada compatible con el contrato de Sitios. Mantener el bucket privado y los tokens en el servidor. [Configuración del gateway](storage-worker/README.md)

Para probar la integración en una máquina de desarrollo con Node y Python:

```sh
(cd storage-worker && npm ci && npm test)
.venv/bin/python checks/storage_flow.py
```

La prueba levanta un Worker local y dos procesos de aplicación con directorios separados. Comprueba acceso por rol, escritura concurrente, acuse, publicación, consulta gerencial y descarga privada después de reconstruir la caché.
