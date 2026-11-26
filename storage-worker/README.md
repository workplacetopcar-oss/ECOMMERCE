# Almacenamiento compartido de Reclamos

El servidor de Reclamos es el único cliente de este Worker. El servidor autentica a la persona y comprueba el rol, canal y expediente antes de devolver información. El Worker autentica al servidor con un secreto Bearer, conserva los registros en D1 y recibe originales, fotografías y diarios de cambios por SHA-256 en R2. Las descargas pasan por el servidor de Reclamos.

## Desarrollo local

Requiere Node 22.12 o superior. Desde `storage-worker/`:

```sh
npm ci
npm test
```

La prueba usa Miniflare y el runtime workerd con D1 y R2 locales. Genera un secreto efímero y comprueba una escritura simultánea, el rollback completo de un lote con error, la recuperación de un reintento, el rechazo de accesos sin credencial y la integridad binaria de R2.

Para trabajar con el servidor Python, guarda el valor privado de `STORAGE_TOKEN` en `.dev.vars` local con permisos de lectura solo para el propietario. Configura el mismo valor en el proceso del servidor, utilizando el nombre indicado en la guía principal. Arranca `npm run dev` desde esta carpeta. Wrangler escucha en el puerto local 8787 y conserva los datos de prueba en `.wrangler/state`. Utiliza `--persist-to` para elegir un directorio aislado por prueba.

## En una cuenta de Cloudflare

La configuración versionada sirve para desarrollo local y lleva un UUID de ejemplo. Para un despliegue autorizado, copia `wrangler.jsonc` como `wrangler.local.jsonc` y configura el ID real de D1 en el binding `DB`, el nombre del bucket privado en `FILES` y la ruta HTTPS a la que tiene acceso el servidor. El bucket debe tener deshabilitados el dominio público y el acceso por r2.dev. Registra `STORAGE_TOKEN` como secreto del Worker y en el gestor de secretos del servidor. La aplicación web usa su propia identidad y permisos, y el secreto del almacenamiento queda exclusivamente en el servidor. El archivo de configuración local está excluido de Git.

## Contrato del servidor

| Método y ruta | Solicitud y respuesta |
|---|---|
| GET /revision | Devuelve `{"revision":0}` en una base nueva. |
| POST /schema | Recibe `{"sql":[...]}`, crea tablas, índices y vistas de forma idempotente y ejecuta las dos inicializaciones del módulo. D1 aplica las sentencias en una transacción. |
| POST /snapshot | Recibe `{"after":0,"limit":50}`, devuelve `revision`, `entries` ordenadas con su revisión y cadena de cambios, y `more`. La revisión y la página pertenecen a la misma lectura de D1. Para continuar utiliza la última revisión recibida. |
| POST /commit | Recibe `base`, `id`, `changes` y `statements`, cada una con `sql` y `params`. La comparación de versión y las mutaciones se ejecutan en el mismo lote D1. Una repetición exacta devuelve la revisión original. Un conflicto devuelve HTTP 409. |
| PUT /blobs/{sha256} | Recibe el cuerpo binario. R2 comprueba SHA-256 antes de aceptar la escritura. Al reintentar se conserva el mismo contenido. |
| GET /blobs/{sha256} | Devuelve el cuerpo binario o HTTP 404. |

El gateway admite JSON de hasta 16 MiB, un máximo de 998 sentencias de mutación por lote, 100 parámetros por sentencia, 100.000 bytes de SQL por sentencia y 1,75 MiB por parámetro de texto. La cadena de cambios admite 64 KiB y el adaptador la utiliza para referenciar el diario por hash. Cada página del diario contiene hasta 100 revisiones. R2 recibe archivos de hasta 64 MiB con longitud conocida y hasta 8 MiB con longitud desconocida. El plan de Cloudflare y los límites de filas, consultas y tiempo de D1 también se aplican. Un error de almacenamiento devuelve HTTP 503 sin incluir las sentencias, los valores o el secreto en la respuesta.
