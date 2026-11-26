"""Lectura del comprobante digital de RSales. El PDF original es la evidencia."""
import io
import logging
import re
import unicodedata
from datetime import datetime

from pypdf import PdfReader

logging.getLogger("pypdf").setLevel(logging.ERROR)

def imagenes_de_preguntas(page, preguntas):
    """Match embedded image placements with the download labels on the same page.

    The CONTACTO template places a caption below the photograph. Several questions
    may share a page; a page number or the ordering of XObject resources alone is
    insufficient. Leave ambiguous placements for review in the original document.
    """
    objetos = page["/Resources"].get_object().get("/XObject", {})
    if hasattr(objetos, "get_object"):
        objetos = objetos.get_object()
    imagenes, etiquetas = [], []

    def dibujo(operador, args, matriz, _texto):
        if operador != b"Do" or not args or args[0] not in objetos:
            return
        obj = objetos[args[0]].get_object()
        if obj.get("/Subtype") != "/Image":
            return
        a, b, c, d, x, y = matriz
        # Reject rotated graphics and the tiny download-button icons.
        if b or c or a < 48 or abs(d) < 48:
            return
        width, height = int(obj["/Width"]), int(obj["/Height"])
        if min(width,height) < 64 or width * height > 20_000_000:
            return
        imagenes.append((str(args[0]),x,x+a,min(y,y+d)))

    def texto(valor, matriz, t, *_):
        if not re.fullmatch(r"Descargar\s*Imagen", valor.strip(), re.I):
            return
        a,b,c,d,e,f = matriz
        etiquetas.append((t[4]*a+t[5]*c+e,t[4]*b+t[5]*d+f))

    page.extract_text(visitor_operand_before=dibujo, visitor_text=texto)
    if len(etiquetas) != len(preguntas):
        return {}
    seleccion = {}
    for pregunta, (x,y) in zip(sorted(preguntas),sorted(etiquetas,key=lambda pos:-pos[1])):
        candidatos = [key for key,left,right,bottom in imagenes
                      if left-4 <= x <= right+4 and 0 <= bottom-y <= 50]
        if len(candidatos) == 1:
            seleccion[pregunta] = candidatos[0]
    return seleccion


def foto_incrustada(contenido, pagina, pregunta, preguntas):
    doc = PdfReader(io.BytesIO(contenido), strict=False)
    if doc.is_encrypted or not 1 <= pagina <= len(doc.pages) <= 80:
        raise ValueError("Página no disponible.")
    page = doc.pages[pagina-1]
    key = imagenes_de_preguntas(page, preguntas).get(pregunta)
    if not key:
        raise ValueError("La fotografía requiere consulta en el comprobante original.")
    entry = page.images[key]
    # Generate a passive image for display. The original stays unaltered in the case file.
    imagen = entry.image.convert("RGB")
    imagen.thumbnail((1800, 1800))
    buffer = io.BytesIO()
    imagen.save(buffer, "JPEG", quality=90)
    return buffer.getvalue()


def extraer(contenido):
    doc = PdfReader(io.BytesIO(contenido), strict=False)
    if doc.is_encrypted or not 1 <= len(doc.pages) <= 80:
        raise ValueError("Se requiere un comprobante legible de hasta 80 páginas.")
    paginas = [unicodedata.normalize("NFKC", p.extract_text(extraction_mode="layout") or "") for p in doc.pages]
    cabecera = paginas[0] if paginas else ""
    for anexo in doc.pages[0].get("/Annots") or []:
        nota = anexo.get_object()
        if nota.get("/Subtype") == "/FreeText":
            cabecera += "\n" + str(nota.get("/Contents", ""))
    def campo_cabecera(patron):
        hallado = re.search(patron, cabecera, re.IGNORECASE)
        return hallado[1] if hallado else ""
    codigo = campo_cabecera(r"C[oó]d\.?\s*Formulario\s*:\s*(\d+)")
    solicitud = campo_cabecera(r"ID\s*Solicitud\s*:\s*([^\s]+)")
    version = campo_cabecera(r"Versi[oó]n\s*:\s*(\d+)")
    if codigo != "34813027" or version != "1" or not solicitud:
        return dict(id_solicitud=solicitud, formulario=codigo, version_formulario=version, lineas=[], revision_manual=True, paginas=len(paginas))
    respuestas, fotos = {}, {}
    campo = 0
    for pagina, texto in enumerate(paginas, 1):
        for renglon in texto.splitlines():
            valor = renglon.strip()
            if not valor:
                continue
            rotulo = re.match(r"^(\d{1,2})\)\s*(.*)", valor)
            if rotulo:
                campo = int(rotulo[1])
                respuestas.setdefault(campo, [])
            elif valor.startswith("#") or valor.startswith("ESPACIO PARA EL PROCESO"):
                campo = 0
            elif re.search(r"Descargar\s*Imagen",valor,re.I) and 9 <= campo <= 43:
                fotos[campo] = pagina
            elif campo and not re.match(r"^P[áa]g\.?\s*\d+\s*de\s*\d+",valor,re.I):
                if valor.casefold() != "sin respuesta":
                    respuestas[campo].append(" ".join(valor.split()))
    def valor(n):
        return " ".join(respuestas.get(n,[]))
    disponibles = {}
    for pagina in sorted(set(fotos.values())):
        preguntas = [p for p, n in fotos.items() if n == pagina]
        try:
            disponibles.update(imagenes_de_preguntas(doc.pages[pagina-1], preguntas))
        except (KeyError, TypeError, ValueError):
            pass
    lineas = []
    for i in range(9, 44, 7):
        codigo_fuente, descripcion, cuantia, motivo = [valor(i+j) for j in range(4)]
        evidencia = [dict(campo=j,pagina=fotos[j],disponible=j in disponibles) for j in range(i+4, i+7) if j in fotos]
        if any((codigo_fuente, descripcion, cuantia, motivo, evidencia)):
            lineas.append(dict(codigo_origen=codigo_fuente,descripcion=descripcion,cantidad=cuantia,
                               unidad="unidad",motivo=motivo,fotos=evidencia,sku="",lote=""))
    fecha = valor(1)
    try:
        fecha = datetime.strptime(fecha,"%d/%m/%Y").date().isoformat()
    except ValueError:
        fecha = ""
    return dict(origen="rsales_formulario", id_solicitud=solicitud, formulario=codigo, version_formulario=version,
                marcas_fuente={k:campo_cabecera(k+r"\s*:\s*(\d{2}/\d{2}/\d{4}[^\n]*?[ap]\.m\.)") for k in ("Creado","Guardado","Transmitido","Editado")},
                numero_contacto=campo_cabecera(r"CONTACTO[ \t]+(\d+)\b"), fecha=fecha, fecha_origen=valor(1),
                vendedor=valor(2),cliente=valor(3),contacto=valor(4),ciudad=valor(5),telefono=valor(6),
                direccion=valor(7),correo=valor(8), lineas=lineas,
                calidad_fuente={str(n):valor(n) or None for n in range(44,50)},paginas=len(paginas))


if __name__ == "__main__":
    # Parse documents in a separate, resource-limited process, including for threaded WSGI.
    import json
    import resource
    import sys
    resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_CPU, (8, 8))
    original = sys.stdin.buffer.read()
    if len(sys.argv) == 5 and sys.argv[1] == "foto":
        pagina, pregunta = map(int, sys.argv[2:4])
        preguntas = [int(n) for n in sys.argv[4].split(",")]
        sys.stdout.buffer.write(foto_incrustada(original, pagina, pregunta, preguntas))
    else:
        print(json.dumps(extraer(original), ensure_ascii=False))
