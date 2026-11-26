# Conexión privada con RSales

El módulo puede trabajar con los maestros importados del ZIP y la captura manual desde el primer día. En el entorno de implementación se probaron los pedidos con un proveedor de prueba. La lectura en vivo requiere el conector autorizado de la empresa, la identidad de lectura, las cuotas acordadas y la salida de red del servidor hacia su servicio. La persona que opera la integración confirma el alcance histórico y de canales.

## Adaptador del servidor

Configura `RECLAMOS_RSALES_ADAPTER` con el nombre importable del módulo Python de confianza que traduce las operaciones disponibles en tu cuenta. Instala o monta ese módulo en el servidor y configura sus credenciales mediante el almacén privado del despliegue. El adaptador se ejecuta dentro del proceso de la aplicación, con los mismos permisos de acceso a la base. Mantén versiones revisadas, límites de tiempo por solicitud y un tamaño de página compatible con el proveedor.

| Elemento del módulo | Contrato local |
|---|---|
| `account` | Identidad textual y estable de la cuenta de origen. |
| `covered_channels` | Lista de canales cuya población completa puede acreditar el origen para la ventana consultada. Valores admitidos: `nacional`, `internacional`, `por_clasificar`. Si la cobertura está pendiente de verificación, usa una lista vacía. |
| `estado_conexion()` | Consulta el modo efectivo del conector. Devuelve `{"solo_lectura": true}` cuando se confirma. |
| `listar_pedidos(tipo, desde, hasta, pagina, tamano)` | Lee una página mediante la operación autorizada para pedidos. Recibe tipo P, fechas de creación inclusivas y un cursor inicial 1. Devuelve `pedidos` como lista, `pagina_siguiente` con el cursor o null al terminar y `periodo_completo` verdadero únicamente cuando se confirma la cobertura de toda la ventana. Sigue paginando aunque la página actual carezca de coincidencias. |
| `buscar_clientes(pagina, tamano_pagina)`, `buscar_productos(pagina, tamano_pagina)` | Para refrescar maestros, devuelven `items`, `pagina_siguiente` y al terminar `maestro_completo: true` y `total_unicos` con el total sin filtros en ese corte. Los ocho campos de producto y los campos del cliente conservan los nombres del corte en el paquete original. |

Conserva los campos originales de cada pedido, incluidos `code` textual, `order_number`, `type`, `created_at`, `state`, `client_id`, `seller_id`, `updated_at`, `delivery_date`, `document_number`, `warehouse`, `last_erp_state`, `importe_neto_antes_de_impuestos` y `cantidad_de_lineas`. El adaptador puede enriquecer la cabecera con nombres resueltos por el identificador autorizado de cliente o vendedor. Confirma una identidad estable antes de asignar `code`. Para las fechas usa ISO 8601 con zona o una fecha comercial de Colombia.

Añade `canal_verificado` solo cuando el mapeo empresarial del pedido esté validado. Un valor ausente queda en «Por clasificar». Cuando existan pruebas de la salida real, aporta `fecha_salida_verificada` y `soporte_salida`. `cerrado_verificado: true` indica que el proveedor y la empresa respaldan que el pedido está cerrado. Conserva la cabecera cuando falten líneas o cantidades vendidas. En ese caso la correspondencia de cada referencia y las tasas sobre unidades vendidas siguen por verificar.

## Ejecución

1. Carga primero el corte privado con la guía de arranque del proyecto. Conecta el adaptador de lectura al proceso web y al planificador que usa la misma base privada.
2. En **Indicadores → Lectura de RSales → Historial y sincronización**, ejecuta una ventana pequeña. Comprueba la conexión, la lectura efectiva, el número de páginas y las cabeceras. Contrasta pedidos únicos de tipo P, canal, estado y fechas contra la fuente autorizada. La aplicación conserva la última ventana publicada si una página falla. Repite la misma ventana para retomar su cursor.
3. Desde la raíz del proyecto y con el entorno Python activado, ejecuta `flask --app app rsales-refresh --dias 7` para releer siete fechas solapadas y reconciliar fechas anteriores de pedidos abiertos. Una vez aprobada la cuota, programa ese comando en el planificador del servidor, inicialmente una vez por hora. Ajusta la frecuencia en ese planificador y los días de solapamiento en el argumento. El proceso web mantiene el botón de actualización manual.
4. Para refrescar productos y clientes usa la vista **Maestros** o `python sync_catalog.py --db /ruta/privada/reclamos.sqlite3`. La programación de esta lectura también depende de la cuota aprobada. Las fichas internacionales y las instantáneas de los expedientes se conservan.
5. Comprueba el enlace de un pedido con una línea desde una cuenta nacional. Confirma desde Calidad que el pedido está en un expediente enviado. Internacional sigue con su captura y sus documentos propios. El CONTACTO por PDF está disponible aunque el entorno de despliegue carezca de una fuente de formularios remotos.

## Verificación reproducible

Ejecuta `python -m pytest -q test_metrics.py test_catalog.py`. Se comprueban el aislamiento entre canales, la publicación atómica de los cortes, la reanudación, la deduplicación, la fecha de Colombia, los catálogos y las tasas con un conjunto controlado de pedidos. Las cifras de esta prueba viven en bases temporales.
