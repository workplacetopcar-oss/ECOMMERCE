# Gestión de Reclamos · Lubristone

Aplicación privada en español para recepción nacional y de exportación, investigación de Calidad y seguimiento de gerencia. Las cuatro cuentas trabajan sobre la misma base persistente. El servidor obtiene la identidad y el canal de la sesión y autoriza cada consulta, descarga y modificación.

El trabajo diario se realiza en la aplicación. Los adjuntos se cargan desde cada expediente. El importador de histórico es una herramienta administrativa opcional. En modo compartido la aplicación usa D1 para registros y R2 para evidencias, con permisos de servidor por canal y expediente. Las instrucciones para este modo y para vincular identidades están en la [guía de despliegue](DEPLOY.md).

## Arranque en una estación privada

Se requiere Python 3.12 o 3.13 con SQLite y la zona horaria America/Bogota. Desde la raíz del proyecto:

```sh
umask 077
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/flask --app app init-accounts
.venv/bin/python app.py
```

Antes del primer `init-accounts`, inyecta las tres claves del propietario mediante `RECLAMOS_PIN_NACIONAL`, `RECLAMOS_PIN_INTERNACIONAL` y `RECLAMOS_PIN_ADMIN` en el entorno privado de ese comando. El alta guarda verificadores Argon2id y puede repetirse. Don Carlos activa a Juan Diego desde **Accesos**. Un restablecimiento de clave revoca todas las sesiones de la cuenta.

El proceso escucha en la interfaz local, puerto 8001. Abre la ruta `/login` en ese puerto. Para varios dispositivos, instala un único servidor HTTPS con volumen persistente y segundo factor individual según la [guía de despliegue](DEPLOY.md).

## Datos y catálogos

`RECLAMOS_DATA_DIR` selecciona el directorio privado. Por defecto es `instance/`. La aplicación prepara las tablas al arrancar. Conserva el directorio en un volumen persistente con permisos exclusivos de la cuenta de servicio. Las evidencias, las versiones y las relaciones de los expedientes quedan en `reclamos.sqlite3`. Los datos de prueba se crean en directorios temporales independientes.

Para cargar o actualizar los maestros, usa el directorio `datos_privados/` del paquete original fuera del repositorio. Usa la misma ruta de base que el servidor.

```sh
.venv/bin/python catalog.py --db instance/reclamos.sqlite3 --data /ruta/privada/datos_privados
```

La carga valida el corte antes de sustituir los maestros. Cada expediente conserva su información histórica. El PDF de CONTACTO se importa desde la bandeja nacional. Los archivos antiguos de Excel se revisan en **Histórico**, donde se concilian antes de confirmarlos.

## Recorrido de trabajo

1. **Zulay** revisa el PDF del vendedor o abre un reclamo manual. **Angélica** selecciona un cliente de exportación e incorpora las facturas, referencias y medidas declaradas. Ambas pueden recuperar borradores.
2. El área de origen envía el caso a Calidad. **Juan Diego** acusa recibo, solicita evidencia si hace falta y publica su dictamen por referencia.
3. El área registra la comunicación, la decisión comercial y sus soportes. El expediente admite reposiciones parciales, créditos y reapertura con motivo. Cada actuación conserva autor y fecha.
4. **Don Carlos** consulta la cola de trabajo, los indicadores, sus registros de origen, el histórico y los accesos. La configuración de lectura y el contrato del adaptador privado se describen en [RSales](RSALES.md).

## Verificación

```sh
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
.venv/bin/python checks/benchmark.py
.venv/bin/python -m playwright install chromium
.venv/bin/python checks/browser_flow.py
```

Para ejecutar también los controles del corte real y de CONTACTO, define `RECLAMOS_PRIVATE_FIXTURES` como la ruta privada que contiene `datos_privados/` y `ejemplos/`. Puedes elegir un Chromium ya instalado mediante `CHROMIUM`. El benchmark crea 10.000 expedientes sintéticos, mide la respuesta local de la bandeja y el p95 de 60 búsquedas. La prueba de navegador usa cuatro sesiones independientes y una base temporal.

Para repetir los recorridos completos de navegador con los soportes privados ya disponibles:

```sh
.venv/bin/python checks/catalog_browser.py
.venv/bin/python checks/browser_cases.py
.venv/bin/python checks/history_browser.py
```

Para verificar el almacenamiento compartido, ejecuta `npm ci` y `npm test` dentro de `storage-worker/`. Desde la raíz ejecuta `.venv/bin/python checks/storage_flow.py`. Con el paquete privado disponible, `.venv/bin/python checks/storage_catalog.py` comprueba además el corte completo y el CONTACTO real en D1/R2 locales. Ambas pruebas usan directorios temporales, puertos de loopback y credenciales efímeras.
