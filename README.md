# Kit Lubricación Extrema · Propuesta animada

Prototipo independiente en `dist/index.html`. Abrir ese archivo en un navegador manteniendo las dos fotografías en la misma carpeta. HTML, CSS y JavaScript sin dependencias de compilación. Fuentes de Google opcionales, con fuentes de sistema de respaldo.

## Alcance
- Diseño responsive inspirado en la narrativa editorial y el movimiento de la referencia La Poción; no copia su marca ni sus piezas.
- Fotografías originales Lubristone sin reconstruir etiquetas.
- Rutina interactiva de tres pasos, aparición al desplazarse, fotografía flotante, cinta animada, CTA fijo, FAQ y simulador de oferta.
- Respeta `prefers-reduced-motion`, controles de teclado y modal con cierre Escape y foco contenido.
- **No está conectado a Shopify, Releasit ni a pedidos. No recauda datos ni hace cobros.**

## Oferta verificada el 17 de noviembre de 2026
Fuente: producto público `https://lubristone.tv/products/kit-lubricacion-extrema.js` y fotografía de oferta vigente.

| Selección | Total COP |
|---|---:|
| 1 kit, envío estándar | $99.900 |
| 1 kit, prioridad | $107.900 |
| 2 kits, envío estándar | $169.830 |
| 2 kits, prioridad | $177.830 |

30% únicamente en el segundo kit. Prioridad: $8.000 por pedido e incluye un llavero por pedido. Envío estándar gratis sin llavero. Pago contra entrega. Cada kit incluye Graphenox aerosol 400 ml, desengrasante 500 ml tapa push, Renovador Partes Negras 50 ml y sachet Carwash 50 ml como obsequios, y bolsa práctica.

Fotografías:
- `kit.jpg`: https://cdn.shopify.com/s/files/1/0748/2851/3370/files/kit-graphenox-nueva-oferta-sin-llavero-2026-11.jpg?v=1794410394
- `kit-prioridad.jpg`: https://cdn.shopify.com/s/files/1/0748/2851/3370/files/kit-graphenox-llavero-foto-original-2026-11.jpg?v=1794411527

## Validación
JavaScript parseado sin errores. Prueba automatizada con DOM simulado de cuatro totales, selección, cambio de fotografía, apertura/cierre del resumen, foco y pasos de rutina. Sin formularios ni llamadas de envío de pedidos. Pendiente inspección visual en navegador móvil y escritorio.

## Integración futura
El botón abre un resumen simulado. Su conexión a formulario Shopify/Releasit requiere trabajo separado y prueba completa. No modificar la tienda publicada desde este prototipo. Antes de producción, confirmar cobertura, tiempos de entrega, políticas y cualquier promesa adicional. No se incorporan reseñas, urgencia ni resultados técnicos inventados.

## QA de esta entrega
- Verificación estática: IDs únicos, anclas internas válidas e imágenes locales presentes.
- Pruebas de JavaScript con DOM simulado: 4 totales, selección de cantidad y envío, rutina y apertura/cierre de resumen correctos.
- Ajustes preventivos de overflow horizontal, navegación estrecha y modal en pantallas bajas.
- Inspección visual real en escritorio/móvil pendiente: Chromium no pudo iniciar por restricciones del entorno. No se afirma validación visual ni medición de rendimiento.
