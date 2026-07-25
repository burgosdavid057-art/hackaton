"""
Ingesta de reportes de turno: de archivo suelto a filas en la base.

El formato de los reportes NO esta estandarizado, y ese es justamente el punto:
cada linea manda lo que puede (un Excel con tres hojas, el PDF del formato de
turno, un CSV exportado del MES, un mensaje de WhatsApp pegado en un .txt).
Buscar columnas fijas aqui seria construir sobre arena: el reporte 47 llega con
otro encabezado y se rompe todo.

Por eso este modulo hace una sola cosa con el archivo: convertirlo en texto
plano legible sin perder nada. Quien entiende el contenido es el extractor
(LLM, temperatura 0) y quien lo aterriza al catalogo es normalizar.py. Aqui no
hay ninguna heuristica sobre que significa una columna, y por lo tanto no hay
nada que se rompa cuando cambie el formato.

Dos consecuencias practicas de esa decision:

  - Las tablas se renderizan ALINEADAS, no como texto corrido. Un PDF de planta
    con la tabla de paradas pierde la correspondencia fila-columna si solo se
    saca extract_text(), y el extractor termina cruzando los minutos de una
    parada con la causa de otra.
  - El texto no se recorta. `texto_crudo` es lo que despues usa
    verificar_literalidad para comprobar que ninguna cifra fue inventada; si
    aqui se truncara, el verificador marcaria como no-literal un dato que si
    estaba en el archivo. Si algun documento no cabe en la ventana del modelo,
    el recorte es problema de extraer.py, no de aqui.
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import re
from pathlib import Path

INBOX = Path(__file__).parent / "inbox"

# Orden de intento para decodificar texto. utf-8-sig va primero porque tambien
# lee utf-8 normal y de paso se come el BOM que mete Excel al "Guardar como
# CSV". cp1252 antes que latin-1 porque los exports de sistemas colombianos
# (SAP, Siesa, Excel en Windows-ES) traen la ñ y las comillas tipograficas en
# cp1252; latin-1 nunca falla, pero convertiria esos bytes en basura silenciosa,
# asi que va de ultimo y solo como red de seguridad.
ENCODINGS = ("utf-8-sig", "cp1252", "latin-1")

# Tope de relleno por columna al alinear tablas. No trunca contenido: una celda
# mas larga simplemente desborda su columna. Sin este tope, una sola celda de
# observaciones de 400 caracteres convierte toda la tabla en una sabana.
ANCHO_MAX_COLUMNA = 60

# Por debajo de esto la fila entra a la cola de revision. El extractor marca
# ~0.9 cuando esta comodo y baja explicitamente cuando duda; verificar_literalidad
# pone 0.0 en lo que no aparece literal en el documento. 0.7 separa esas dos
# poblaciones sin inundar la cola de revision con filas correctas.
UMBRAL_DUDOSO = 0.7

EXTENSIONES_TEXTO = (".txt", ".md")
EXTENSIONES_EXCEL = (".xlsx", ".xlsm", ".xls")

# Ruta de campo que escribe verificar_literalidad: "paradas[1].minutos".
_RUTA_CAMPO = re.compile(r"^(paradas|scrap|calidad)\[(\d+)\]")


# --- Utilidades de renderizado -----------------------------------------------

def _vacio(v) -> bool:
    """True si la celda no tiene contenido.

    El truco `v != v` atrapa de un golpe a NaN de numpy y a NaT de pandas, que
    son los dos valores que devuelve una celda vacia de Excel. pandas.NA no
    responde a comparaciones booleanas y revienta: tambien es una celda vacia.
    """
    if v is None:
        return True
    try:
        return bool(v != v)
    except (TypeError, ValueError):
        return True


def _celda(v) -> str:
    """Convierte un valor de celda en el texto que vera el extractor.

    Las dos conversiones que importan:

    - 480.0 -> "480". Excel devuelve todo entero como float. Si se escribiera
      "480.0", verificar_literalidad buscaria el 480 que reporto el modelo y no
      lo encontraria tal cual, y un dato bueno quedaria marcado en rojo.
    - fechas -> ISO. Un Timestamp impreso crudo sale "2026-07-23 00:00:00" y el
      extractor tiene que adivinar; en ISO lo copia y ya.
    """
    if _vacio(v):
        return ""
    if isinstance(v, dt.datetime):
        if (v.hour, v.minute, v.second) == (0, 0, 0):
            return v.date().isoformat()
        return v.strftime("%Y-%m-%d %H:%M")
    if isinstance(v, dt.date):
        return v.isoformat()
    if isinstance(v, dt.time):
        return v.strftime("%H:%M")
    if isinstance(v, bool):
        return "SI" if v else "NO"
    if isinstance(v, float):
        if v.is_integer():
            return str(int(v))
        # rstrip contra el ruido binario del float (0.30000000000000004).
        return f"{v:.6f}".rstrip("0").rstrip(".")
    # Los saltos de linea dentro de una celda destruyen la alineacion de la
    # tabla; se aplanan a espacio. No se pierde contenido.
    return " ".join(str(v).split())


def _tabla_a_texto(filas: list[list]) -> str:
    """Renderiza una matriz como tabla alineada con separador '|'.

    El separador explicito no es adorno: alinear solo con espacios deja
    ambiguo donde termina una columna que viene vacia, y el modelo corre los
    valores una casilla a la izquierda.
    """
    # isinstance y no `if fila`: una fila que llegue como string se recorreria
    # caracter por caracter y produciria una tabla de 400 columnas de una letra.
    matriz = [[_celda(c) for c in fila] for fila in filas
              if isinstance(fila, (list, tuple))]
    if not matriz:
        return ""

    ancho_fila = max(len(f) for f in matriz)
    matriz = [f + [""] * (ancho_fila - len(f)) for f in matriz]

    # Columnas 100% vacias: sobran siempre en Excel (formato, celdas combinadas,
    # columnas de relleno) y solo gastan contexto del modelo.
    vivas = [j for j in range(ancho_fila) if any(f[j] for f in matriz)]
    if not vivas:
        return ""
    matriz = [[f[j] for j in vivas] for f in matriz]

    anchos = [
        min(max(len(f[j]) for f in matriz), ANCHO_MAX_COLUMNA)
        for j in range(len(vivas))
    ]

    lineas: list[str] = []
    vacias_seguidas = 0
    for fila in matriz:
        if not any(fila):
            # Una fila en blanco separa bloques y hay que conservarla (arriba
            # suele venir el encabezado de otra tabla). Mil filas en blanco al
            # final de la hoja no aportan nada: se colapsan a una.
            vacias_seguidas += 1
            if vacias_seguidas == 1:
                lineas.append("")
            continue
        vacias_seguidas = 0
        lineas.append(" | ".join(c.ljust(a) for c, a in zip(fila, anchos)).rstrip())

    return "\n".join(lineas).strip("\n")


def _leer_texto(ruta: Path) -> str:
    """Lee un archivo de texto probando la cadena de encodings."""
    datos = ruta.read_bytes()
    for enc in ENCODINGS:
        try:
            return datos.decode(enc)
        except UnicodeDecodeError:
            continue
    # latin-1 no lanza nunca, asi que aqui no se llega; queda por si alguien
    # cambia ENCODINGS y deja solo codecs estrictos.
    return datos.decode("latin-1", errors="replace")


def _separador(muestra: str) -> str:
    """Detecta el delimitador de un CSV."""
    try:
        return csv.Sniffer().sniff(muestra, delimiters=",;\t|").delimiter
    except csv.Error:
        # El Sniffer se rinde con archivos de una sola columna o con comillas
        # mal cerradas. Se cuenta a mano sobre la primera linea con contenido:
        # el encabezado es la fila donde el separador aparece mas veces.
        primera = next((l for l in muestra.splitlines() if l.strip()), "")
        conteos = {s: primera.count(s) for s in (";", ",", "\t", "|")}
        mejor = max(conteos, key=lambda s: conteos[s])
        return mejor if conteos[mejor] else ","


# --- Lectores por formato ----------------------------------------------------

def _excel_a_texto(ruta: Path) -> str:
    """Rinde todas las hojas de un Excel como tablas de texto."""
    try:
        import pandas as pd
    except ImportError as e:  # pragma: no cover - depende del entorno
        raise RuntimeError(f"falta pandas para leer {ruta.name}: {e}") from e

    motor = "openpyxl" if ruta.suffix.lower() in (".xlsx", ".xlsm") else None
    try:
        # header=None es la clave del asunto: NO se promueve la primera fila a
        # encabezado. Los formatos de planta ponen titulo, logo y "Fecha:" en
        # las primeras filas y el encabezado real aparece en la fila 6. Con
        # header=0 esa fila se convierte en nombres de columna y las de arriba
        # en "Unnamed: 3": se pierde justo la informacion que ubica la tabla.
        hojas = pd.read_excel(ruta, sheet_name=None, header=None, engine=motor)
    except ImportError as e:
        # El .xls viejo necesita xlrd, que no esta en requirements a proposito.
        # Se dice con nombre propio y con la salida concreta, en vez de dejar
        # salir un ImportError cripto en la pantalla de carga.
        salida = (
            "Ábrelo en Excel y guárdalo como .xlsx"
            if motor is None else
            "Instala el motor: pip install openpyxl"
        )
        raise RuntimeError(f"no hay motor para leer {ruta.suffix}: {e}. {salida}") from e

    partes: list[str] = []
    for nombre, df in hojas.items():
        cuerpo = _tabla_a_texto(df.values.tolist())
        if not cuerpo:
            # Hoja vacia: se anuncia igual. Que el reporte traiga una hoja
            # "SCRAP" sin filas es informacion (no hubo scrap registrado), no
            # un hueco que haya que esconder.
            partes.append(f"=== Hoja: {nombre} ===\n(hoja sin datos)")
            continue
        partes.append(f"=== Hoja: {nombre} ===\n{cuerpo}")
    return "\n\n".join(partes)


def _csv_a_texto(ruta: Path) -> str:
    """Rinde un CSV como tabla alineada, detectando separador y encoding."""
    crudo = _leer_texto(ruta)
    if not crudo.strip():
        return ""
    sep = _separador(crudo[:4096])
    # Se usa csv.reader y no pandas a proposito: pandas tipifica las columnas y
    # "0480" se vuelve 480, "1.234" se vuelve 1234.0. El extractor tiene que ver
    # exactamente lo que escribio quien exporto el archivo.
    filas = list(csv.reader(io.StringIO(crudo), delimiter=sep))
    cuerpo = _tabla_a_texto(filas)
    return f"=== Archivo CSV: {ruta.name} ===\n{cuerpo}" if cuerpo else ""


def _pdf_a_texto(ruta: Path) -> str:
    """Texto por pagina MAS las tablas detectadas, rendidas aparte."""
    try:
        import pdfplumber
    except ImportError as e:  # pragma: no cover - depende del entorno
        raise RuntimeError(f"falta pdfplumber para leer {ruta.name}: {e}") from e

    partes: list[str] = []
    with pdfplumber.open(str(ruta)) as pdf:
        for i, pagina in enumerate(pdf.pages, 1):
            bloque = [f"=== Pagina {i} ==="]
            texto = pagina.extract_text() or ""
            if texto.strip():
                bloque.append(texto.strip())
            try:
                tablas = pagina.extract_tables() or []
            except Exception:
                # extract_tables tropieza con PDFs con lineas raras. Perder las
                # tablas de una pagina es malo; perder el documento entero por
                # eso es peor.
                tablas = []
            for j, tabla in enumerate(tablas, 1):
                cuerpo = _tabla_a_texto(tabla)
                if cuerpo:
                    bloque.append(f"--- Tabla {j} (pagina {i}) ---\n{cuerpo}")
            partes.append("\n\n".join(bloque))

    # Si, el contenido de las tablas puede quedar dos veces: extract_text() ya
    # lo trae, pero corrido y sin columnas. Se acepta la redundancia porque la
    # version alineada es la unica en la que el extractor puede saber que ese
    # "35" son los minutos de esa parada y no de la de abajo.
    return "\n\n".join(partes)


# --- API publica: archivo -> texto -------------------------------------------

def sha256_archivo(ruta: Path) -> str:
    """Huella del contenido del archivo, leyendo por bloques."""
    h = hashlib.sha256()
    with open(ruta, "rb") as f:
        # Por bloques y no read(): un Excel de planta con imagenes pesa decenas
        # de MB y no hay razon para cargarlo entero en memoria.
        for bloque in iter(lambda: f.read(1024 * 1024), b""):
            h.update(bloque)
    return h.hexdigest()


def a_texto(ruta: Path) -> tuple[str, str]:
    """Convierte cualquier reporte soportado en (texto_plano, formato).

    Formato desconocido no es una excepcion: devuelve ("", "desconocido") para
    que un .zip olvidado en el inbox no tumbe el lote. Un formato conocido que
    falla al leerse SI lanza, porque el motivo (archivo corrupto, protegido con
    clave, motor faltante) tiene que llegar al reporte del usuario y no
    confundirse con "el archivo venia vacio".
    """
    ruta = Path(ruta)
    suf = ruta.suffix.lower()

    if suf in EXTENSIONES_EXCEL:
        return _excel_a_texto(ruta), suf.lstrip(".")
    if suf == ".csv":
        return _csv_a_texto(ruta), "csv"
    if suf == ".pdf":
        return _pdf_a_texto(ruta), "pdf"
    if suf in EXTENSIONES_TEXTO:
        return _leer_texto(ruta), suf.lstrip(".")
    return "", "desconocido"


# --- Modulos hermanos --------------------------------------------------------

def _modulos():
    """Importa db, extraer y normalizar tarde y no al inicio del archivo.

    Dos razones: (1) `a_texto` y `sha256_archivo` son utiles por si solas y no
    tienen por que arrastrar la DB ni el cliente del modelo cada vez que alguien
    importa este modulo; (2) el paquete se usa tanto como `produccion.ingest`
    (Streamlit) como corriendo el archivo suelto, y solo el primer caso admite
    imports relativos.

    Se decide por `__package__` y no con try/except: si `extraer` fallara al
    importarse por dentro, un except ImportError se comeria ese error real y
    reportaria uno falso sobre el import relativo.
    """
    if __package__:
        from . import db, extraer, normalizar
    else:
        import db, extraer, normalizar  # type: ignore[no-redef]
    return db, extraer, normalizar


def _rag_opcional():
    """Devuelve rag.py, o None si no se puede importar.

    El RAG es infraestructura opcional por contrato: sin Chroma el copiloto
    sigue respondiendo con las herramientas numericas. Que falte no puede
    impedir que los turnos entren a la base, asi que aqui si se traga cualquier
    error de import.
    """
    try:
        if __package__:
            from . import rag
        else:
            import rag  # type: ignore[no-redef]
        return rag
    except Exception:
        return None


_db_lista = False


def _asegurar_db(db) -> None:
    """Crea el esquema si aun no existe. Una sola vez por proceso.

    inicializar(forzar=False) es idempotente. Se llama aqui y no en el arranque
    de la app porque el pipeline se dispara tambien desde el CLI y desde los
    tests, y ninguno de los dos deberia tener que acordarse de inicializar.
    """
    global _db_lista
    if _db_lista:
        return
    db.inicializar()
    _db_lista = True


def _mapa_lineas(db) -> dict[int, str]:
    try:
        return {int(f["id"]): str(f["nombre"]) for f in db.lineas()}
    except Exception:
        # Sin catalogo la metadata cae al texto crudo de la linea. Degradar es
        # aceptable; tumbar la carga por el catalogo no.
        return {}


def _nombre_linea(turno: dict, mapa: dict[int, str]) -> str:
    """Nombre canonico de la linea para la metadata del RAG.

    Pesa mas de lo que parece: rag.buscar_observaciones filtra por metadata
    ANTES de la busqueda vectorial, asi que si aqui queda "l2 " o "Linea 2" el
    where= de Chroma no empata con nada y la busqueda vuelve vacia sin decir por
    que. El linea_id ya lo resolvio normalizar.py contra el catalogo: se traduce
    de vuelta a nombre y solo si no hay id se usa lo que escribio el supervisor.
    """
    lid = turno.get("linea_id")
    try:
        if lid is not None and int(lid) in mapa:
            return mapa[int(lid)]
    except (TypeError, ValueError):
        pass
    return str(turno.get("linea") or "").strip()


def _fecha_iso(valor) -> str | None:
    """'YYYY-MM-DD' real, o None. "lunes 20" no es una fecha; "2026-13-45" tampoco."""
    m = re.search(r"\d{4}-\d{2}-\d{2}", str(valor or ""))
    if not m:
        return None
    try:
        return dt.date.fromisoformat(m.group(0)).isoformat()
    except ValueError:
        return None


def _fecha_del_nombre(turno: dict, ruta: Path) -> None:
    """Rellena la fecha del turno desde el nombre del archivo, si falta.

    El reporte de WhatsApp dice "lunes 20" y nada mas: el extractor hace bien en
    dejar `fecha` en null, porque inventar el mes y el ano seria exactamente lo
    que el prompt le prohibe. Pero el archivo se llama
    turno_2026-07-20_L2_whatsapp.txt, o sea que el dato SI esta — solo que en el
    nombre y no en el cuerpo.

    Sin esto, db.guardar_turno rechaza el turno y el reporte se pierde entero.
    Perder un turno de planta por eso es peor que usar el nombre del archivo, que
    es justamente donde la planta pone la fecha cuando exporta.

    Solo rellena lo que falta: si el extractor encontro una fecha en el texto,
    esa manda. El texto es el documento; el nombre es apenas la etiqueta.
    """
    if _fecha_iso(turno.get("fecha")):
        return
    del_nombre = _fecha_iso(ruta.name)
    if del_nombre:
        turno["fecha"] = del_nombre
        turno["fecha_desde_nombre"] = True


def _observaciones(turno: dict) -> list[str]:
    """Texto libre del turno, limpio y sin entradas vacias."""
    obs = turno.get("observaciones")
    if isinstance(obs, str):
        # El extractor a veces devuelve un parrafo en vez de una lista pese al
        # formato pedido. Es un modelo de 7B: se acomoda, no se pelea.
        obs = [obs]
    return [t.strip() for t in (obs or []) if isinstance(t, str) and t.strip()]


def _confianza(fila: dict) -> float:
    v = fila.get("confianza")
    if v is None:
        return 1.0  # el default del esquema: sin marca explicita, se cree
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0  # confianza ilegible es peor que confianza baja


def _filas_marcadas(turno: dict) -> set[tuple[str, int]]:
    """Filas que verificar_literalidad ya marco, leidas de sus rutas.

    Las rutas vienen como "paradas[1].minutos" (formato documentado en
    extraer.verificar_literalidad). Hacen falta porque ese mismo verificador
    pone `confianza = 0.0` en la fila que marca: sin esta lista, un unico dato
    inventado se contaria dos veces, como campo no literal y como fila de baja
    confianza, y el contador de la cola de revision mentiria hacia arriba.
    """
    marcadas: set[tuple[str, int]] = set()
    for ruta in turno.get("campos_no_literales") or []:
        m = _RUTA_CAMPO.match(str(ruta))
        if m:
            marcadas.add((m.group(1), int(m.group(2))))
    return marcadas


def _contar_dudosos(turno: dict) -> int:
    """Cuenta lo que un humano tiene que mirar a mano en este turno.

    Suman tres cosas distintas porque terminan en la misma cola de la pantalla
    de revision: un numero que no aparece literal en el documento (lo marca
    verificar_literalidad), una causa que no se pudo mapear al catalogo
    (normalizar la deja con causa_id None) y una fila que el propio extractor
    reporto con poca confianza.
    """
    marcadas = _filas_marcadas(turno)
    dudosos = len(turno.get("campos_no_literales") or [])
    for clave in ("paradas", "scrap", "calidad"):
        for i, fila in enumerate(turno.get(clave) or []):
            if not isinstance(fila, dict):
                continue
            # calidad no tiene causa_id en el esquema: se clasifica por
            # tipo_defecto, asi que ahi solo pesa la confianza.
            sin_causa = clave != "calidad" and fila.get("causa_id") is None
            # La duda del extractor solo cuenta si no la puso el verificador:
            # esa ya esta contada arriba, campo por campo.
            baja = _confianza(fila) < UMBRAL_DUDOSO and (clave, i) not in marcadas
            if sin_causa or baja:
                dudosos += 1
    return dudosos


# --- Pipeline ----------------------------------------------------------------

def _resultado(ruta: Path, formato: str, estado: str, turnos: int = 0,
               dudosos: int = 0, motivo: str | None = None,
               documento_id: int | None = None) -> dict:
    return {
        "archivo": ruta.name,
        "ruta": str(ruta),
        "formato": formato,
        "estado": estado,
        "turnos_guardados": turnos,
        "campos_dudosos": dudosos,
        "motivo": motivo,
        "documento_id": documento_id,
    }


def procesar(ruta: Path) -> dict:
    """Pipeline completo de un archivo: texto -> extraccion -> base -> RAG.

    Nunca lanza. Un reporte corrupto en la mitad del lote no puede impedir que
    entren los otros catorce, y el supervisor necesita ver cual fallo y por que,
    no un traceback en la consola de Streamlit.

    La variable `etapa` existe para eso: el motivo del error dice en que paso se
    cayo, que es la diferencia entre "el PDF venia protegido" y "Ollama no esta
    arriba".
    """
    ruta = Path(ruta)
    formato = "desconocido"
    etapa = "lectura"

    try:
        # Primero el archivo y solo despues los modulos hermanos: un .zip
        # olvidado en el inbox no tiene por que levantar la DB ni el cliente del
        # modelo, y asi el `formato` del resultado es el real y no "desconocido"
        # por haber fallado antes de mirar el archivo.
        sha = sha256_archivo(ruta)
        texto, formato = a_texto(ruta)

        if formato == "desconocido":
            return _resultado(
                ruta, formato, "error",
                motivo=f"formato no soportado ({ruta.suffix or 'sin extensión'}). "
                       f"Se aceptan .xlsx .xls .csv .pdf .txt .md",
            )
        if not texto.strip():
            # Sintoma tipico del PDF escaneado sin OCR. Decirlo con nombre
            # propio evita que alguien pase media hora revisando el extractor.
            return _resultado(
                ruta, formato, "error",
                motivo="no se extrajo texto del archivo (¿PDF escaneado sin OCR "
                       "o archivo vacío?)",
            )

        etapa = "registro del documento"
        db, extraer, normalizar = _modulos()
        _asegurar_db(db)
        documento_id = db.registrar_documento(str(ruta), sha, formato, texto)
        if documento_id is None:
            # Mismo sha256 = mismo contenido. Reprocesar duplicaria turnos y
            # ensuciaria toda la aritmetica aguas abajo.
            return _resultado(
                ruta, formato, "duplicado",
                motivo=f"ya se había cargado antes (sha256 {sha[:12]}…)",
            )

        etapa = "extraccion"
        datos = extraer.extraer(texto)

        etapa = "verificacion de literalidad"
        datos = extraer.verificar_literalidad(datos, texto)

        etapa = "normalizacion"
        datos = normalizar.normalizar(datos)

        turnos = datos.get("turnos") or []
        if not turnos:
            motivo = datos.get("error") or (
                "el extractor no encontró ningún turno en el documento"
            )
            return _resultado(ruta, formato, "error", motivo=str(motivo),
                              documento_id=documento_id)

        etapa = "guardado"
        mapa = _mapa_lineas(db)
        guardados = 0
        dudosos = 0
        avisos: list[str] = []

        for i, turno in enumerate(turnos, 1):
            _fecha_del_nombre(turno, ruta)
            try:
                turno_id = db.guardar_turno(documento_id, turno)
            except Exception as e:  # noqa: BLE001
                # Un turno con la fecha ilegible no puede llevarse los otros.
                avisos.append(f"turno {i} no se guardó: {type(e).__name__}: {e}")
                continue

            guardados += 1
            dudosos += _contar_dudosos(turno)

            textos = _observaciones(turno)
            if not textos:
                continue
            # rag se importa aqui y no antes del bucle: un reporte sin texto
            # libre no tiene por que levantar Chroma (arranca ~2 s y carga
            # librerias nativas). Python cachea el modulo, asi que a partir del
            # segundo turno esto es un lookup.
            rag = _rag_opcional()
            if rag is None:
                continue
            try:
                # La metadata es lo que despues permite recortar por linea y
                # fecha ANTES de buscar. Solo van claves con valor real: Chroma
                # no acepta None y un "" en el filtro no empata con nada.
                meta = {
                    "linea": _nombre_linea(turno, mapa),
                    "fecha": str(turno.get("fecha") or ""),
                    "turno": turno.get("turno"),
                    "supervisor": str(turno.get("supervisor") or ""),
                    "archivo": ruta.name,
                }
                meta = {k: v for k, v in meta.items() if v not in (None, "")}
                rag.indexar_observaciones(turno_id, documento_id, meta, textos)
            except Exception as e:  # noqa: BLE001
                # Sin RAG la busqueda semantica pierde este turno, pero los
                # numeros ya estan en la base y las herramientas los ven.
                avisos.append(f"observaciones del turno {i} sin indexar: {type(e).__name__}")

        if guardados == 0:
            return _resultado(ruta, formato, "error", dudosos=dudosos,
                              motivo="; ".join(avisos) or "ningún turno se pudo guardar",
                              documento_id=documento_id)

        return _resultado(ruta, formato, "ok", turnos=guardados, dudosos=dudosos,
                          motivo="; ".join(avisos) or None,
                          documento_id=documento_id)

    except Exception as e:  # noqa: BLE001
        return _resultado(ruta, formato, "error",
                          motivo=f"falló en {etapa}: {type(e).__name__}: {e}")


def procesar_inbox(carpeta: Path = INBOX) -> list[dict]:
    """Procesa todos los archivos de una carpeta y devuelve un resultado por cada uno."""
    carpeta = Path(carpeta)
    if not carpeta.exists():
        carpeta.mkdir(parents=True, exist_ok=True)
        return []

    archivos = []
    for p in sorted(carpeta.iterdir(), key=lambda x: x.name.lower()):
        if not p.is_file():
            continue
        # "~$reporte.xlsx" es el archivo de bloqueo que deja Excel cuando el
        # libro esta abierto, y ".DS_Store" viaja en los zips que manda gente
        # con Mac. Ninguno es un reporte y los dos ensucian el lote.
        if p.name.startswith(("~$", ".")):
            continue
        archivos.append(p)

    # Orden estable: el mismo inbox tiene que producir el mismo reporte dos
    # veces seguidas, si no es imposible comparar corridas.
    return [procesar(p) for p in archivos]


def main() -> None:
    resultados = procesar_inbox()
    if not resultados:
        print(f"No hay archivos para procesar en {INBOX}")
        return

    marcas = {"ok": "ok  ", "duplicado": "dup ", "error": "ERR "}
    for r in resultados:
        print(
            f"  {marcas.get(r['estado'], '?   ')} {r['archivo'][:44]:<44} "
            f"{r['formato']:<11} turnos={r['turnos_guardados']:<3} "
            f"dudosos={r['campos_dudosos']}"
        )
        if r["motivo"]:
            print(f"       -> {r['motivo']}")

    ok = sum(1 for r in resultados if r["estado"] == "ok")
    turnos = sum(r["turnos_guardados"] for r in resultados)
    dudosos = sum(r["campos_dudosos"] for r in resultados)
    print(f"\n  {ok}/{len(resultados)} archivos · {turnos} turnos · "
          f"{dudosos} campos por revisar")


if __name__ == "__main__":
    if __package__:
        main()
    else:
        # `python produccion/ingest.py` deja el archivo como modulo suelto y ahi
        # los hermanos no resuelven (extraer.py hace `from . import prompts`).
        # Se reentra como modulo del paquete en vez de reventar a mitad del lote
        # con un ImportError que no dice nada del reporte que se estaba cargando.
        import runpy
        import sys

        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        runpy.run_module("produccion.ingest", run_name="__main__")
