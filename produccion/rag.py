"""
Busqueda semantica sobre lo que la gente ESCRIBIO en los reportes.

Las herramientas numericas dicen cuanto y cuantas veces. Aqui vive la otra
mitad: el texto libre del supervisor, el procedimiento de mantenimiento y lo
que se hizo la vez pasada. Tres colecciones Chroma, persistentes en
data/chroma_produccion/:

    observaciones  - texto libre de cada turno, con linea/fecha/turno
    procedimientos - .txt/.md de data/procedimientos/, troceados por seccion
    resoluciones   - casos cerrados: que paso y que se hizo

REGLA QUE SOSTIENE TODO EL MODULO: el filtro por metadata va ANTES de la
busqueda vectorial, con el parametro `where=` de Chroma. El recorte por linea y
por rango de fechas es determinista; lo semantico solo ORDENA lo que ya quedo
dentro del universo correcto. Un RAG sin ese filtro devuelve parrafos que suenan
perfectos sobre la linea equivocada, y el supervisor no tiene como notarlo. Cada
respuesta incluye `universo_filtrado` justamente para que ese recorte sea
auditable: "de 34 observaciones de L2 en el rango, estas 5 son las mas parecidas".

Las fechas se guardan dos veces: `fecha` como texto ISO (para citar) y
`fecha_int` como entero YYYYMMDD (para filtrar). Chroma no compara strings de
fecha con $gte; sin el entero, el rango de fechas simplemente no recorta.

Nada de esto lanza. Si Chroma no esta instalado, si la base esta vacia o si
Ollama no responde el embedding, se devuelve {"disponible": False, "motivo": ...}
y el agente sigue trabajando solo con los numeros, que es el 80% de su valor.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
CHROMA_DIR = RAIZ / "data" / "chroma_produccion"
PROCEDIMIENTOS_DIR = RAIZ / "data" / "procedimientos"

COLECCIONES = ("observaciones", "procedimientos", "resoluciones")

# Lote de vectorizacion. Con Ollama local no hay cuota, pero mandar 500 textos
# en una sola llamada agota el contexto del servidor de embeddings.
LOTE = 32

# Extensiones que se indexan como procedimiento. Un PDF de procedimiento se
# convierte antes con ingest.a_texto; aqui no se parsea nada binario.
EXTENSIONES = (".txt", ".md")

_cliente = None
_colecciones: dict[str, object] = {}
_ultimo_motivo: str | None = None


# --- Normalizacion y utilidades ---------------------------------------------

def _norm(texto: str) -> str:
    """Minusculas sin tildes. La misma idea que agent/knowledge.py:_norm."""
    texto = unicodedata.normalize("NFD", (texto or "").lower())
    return "".join(c for c in texto if unicodedata.category(c) != "Mn").strip()


def _clave_linea(valor: str) -> str:
    """Reduce 'L2', 'l2', 'Linea 2' y 'LINEA-2' a la misma clave: '2'.

    El filtro por linea es exacto contra la metadata, y un exacto que falla en
    silencio es el segundo peor error posible aqui (el peor es no filtrar). El
    supervisor escribe 'linea 2' y la taxonomia dice 'L2': si no se reconcilian,
    la busqueda devuelve cero y parece que no hay datos.
    """
    n = _norm(valor)
    # Sin \b de cierre a proposito: "linea2" viene pegado y ahi el limite de
    # palabra no existe, que es justo el caso que se escribe a mano.
    n = re.sub(r"\b(?:lineas?|line|ln)", "", n)
    n = re.sub(r"[^a-z0-9]", "", n)
    return n[1:] if (len(n) > 1 and n[0] == "l" and n[1:].isdigit()) else n


def _fecha_int(fecha) -> int | None:
    """'2025-07-14' -> 20250714. Devuelve None si no es una fecha ISO.

    Deliberadamente NO adivina formatos ambiguos: '07/14/2025' y '14/07/2025'
    son indistinguibles y equivocarse mueve el dato de mes. Si no reconoce el
    formato devuelve None, y el documento queda invisible para las busquedas con
    rango de fechas. Invisible es recuperable; mal fechado no: una observacion
    de julio contada como del 14 de julio del ano 1407 no la nota nadie.

    El ano se acota a 2000-2100 justamente para que '14/07/2025' no se cuele
    como 1407-20-25 y quede fuera de todo rango sin que se note.
    """
    if fecha is None:
        return None
    digitos = str(fecha) if isinstance(fecha, int) else re.sub(r"\D", "", str(fecha))
    if len(digitos) != 8:
        return None
    valor = int(digitos)
    anio, mes, dia = valor // 10000, (valor // 100) % 100, valor % 100
    if not (2000 <= anio <= 2100 and 1 <= mes <= 12 and 1 <= dia <= 31):
        return None
    return valor


def _limpiar_metadata(metadata: dict | None) -> dict:
    """Deja la metadata como Chroma la acepta: str, int, float o bool.

    Los None se descartan en vez de convertirse en "": un turno sin linea
    identificada NO debe quedar atribuido a ninguna linea, y descartar la clave
    lo saca del filtro `where` en vez de meterlo en el cajon equivocado.
    """
    limpia: dict = {}
    for clave, valor in (metadata or {}).items():
        if valor is None or clave is None:
            continue
        if isinstance(valor, (bool, int, float, str)):
            limpia[str(clave)] = valor
        else:
            limpia[str(clave)] = str(valor)
    return limpia


# Encabezados de un procedimiento de planta. No se reusa el regex de
# agent/knowledge.py porque ese esta hecho para manuales de usuario (GARANTIA,
# EXCLUSIONES, POSIBLE CAUSA) y aqui los documentos hablan de EPP, frecuencia y
# pasos. Se corta por tres formas, y solo por esas tres:
#   (a) encabezado markdown, que es como vienen los .md;
#   (b) una palabra clave de procedimiento sola en su linea (con o sin ':');
#   (c) cualquier linea corta en MAYUSCULAS, que es como se titula en planta.
# La forma (b) exige que la palabra este SOLA para no partir un parrafo que
# empieza con "Cambio de dado cada 500 golpes"; sin esa restriccion el troceo se
# vuelve arbitrario y los pasajes citados quedan cortados a la mitad.
PALABRAS_ENCABEZADO = (
    r"OBJETIVO|ALCANCE|RESPONSABLES?|FRECUENCIA|SEGURIDAD|EPP|"
    r"HERRAMIENTAS?|MATERIALES|REPUESTOS?|PROCEDIMIENTO|PASOS?|"
    r"DIAGN[OÓ]STICO|S[IÍ]NTOMAS?|CAUSAS?(?:[ \t]+PROBABLES?)?|"
    r"SOLUCI[OÓ]N(?:ES)?|ACCI[OÓ]N(?:ES)?[ \t]+CORRECTIVAS?|"
    r"VERIFICACI[OÓ]N|REGISTRO|ADVERTENCIA|PRECAUCI[OÓ]N|"
    r"MANTENIMIENTO|LIMPIEZA|CRITERIOS?(?:[ \t]+DE[ \t]+ACEPTACI[OÓ]N)?"
)
ENCABEZADO = re.compile(
    r"^[ \t]*(?:"
    r"#{1,6}[ \t]+\S[^\n]*"
    r"|(?:\d+(?:\.\d+)*[.)][ \t]*)?(?:" + PALABRAS_ENCABEZADO + r")[ \t]*:?[ \t]*"
    r"|(?-i:[A-Z0-9ÁÉÍÓÚÑ][A-Z0-9ÁÉÍÓÚÑ \t.:_/\-]{2,69})"
    r")$",
    re.I | re.M,
)


def _partir(texto: str, objetivo: int = 900) -> list[dict]:
    """Trocea un procedimiento por secciones, igual que agent/knowledge.py.

    El corte por seccion importa mas que el tamano: un pasaje que empieza en
    'PASOS' y termina a mitad de la lista deja al agente citando medio
    procedimiento. Por eso se corta primero por encabezado y solo despues se
    subdivide por parrafos si la seccion quedo muy larga.
    """
    cortes = [0] + [m.start() for m in ENCABEZADO.finditer(texto)] + [len(texto)]
    secciones = []
    for i in range(len(cortes) - 1):
        bloque = texto[cortes[i]:cortes[i + 1]].strip()
        if not bloque:
            continue
        cabecera = ENCABEZADO.match(bloque)
        titulo = (cabecera.group(0).strip().lstrip("#").strip() if cabecera else "")[:70]
        secciones.append((titulo, bloque))

    pasajes: list[dict] = []
    for titulo, bloque in secciones:
        actual: list[str] = []
        tam = 0
        for parrafo in re.split(r"\n\s*\n", bloque):
            parrafo = parrafo.strip()
            if not parrafo:
                continue
            if tam + len(parrafo) > objetivo and actual:
                pasajes.append({"seccion": titulo, "texto": "\n".join(actual)})
                actual, tam = [], 0
            actual.append(parrafo)
            tam += len(parrafo)
        if actual:
            pasajes.append({"seccion": titulo, "texto": "\n".join(actual)})
    # Umbral bajo (40) a proposito: en un procedimiento un paso de dos lineas
    # ("purgar la linea de aire antes de abrir el tablero") es justo lo que hay
    # que citar. En un manual de usuario ese fragmento seria ruido; aqui no.
    return [p for p in pasajes if len(p["texto"]) > 40]


CLAVES_CABECERA = {
    "maquina": "maquina", "equipo": "maquina", "estacion": "maquina",
    "linea": "linea", "version": "version", "area": "area",
}


def _cabecera(texto: str) -> dict:
    """Lee el bloque 'clave: valor' del inicio del archivo, si lo hay.

    Convencion del repo: un procedimiento puede empezar con lineas tipo
    `maquina: Remachadora R-02` y `linea: L2`. Es opcional; si no estan, la
    maquina se deduce del nombre del archivo.
    """
    encontrado: dict = {}
    for linea in (texto or "").splitlines():
        crudo = linea.strip()
        if not crudo:
            if encontrado:
                break          # linea en blanco cierra la cabecera
            continue
        m = re.match(r"^([A-Za-zÀ-ſ_]{3,20})\s*:\s*(.+)$", crudo)
        if not m:
            break              # la primera linea que no es clave:valor corta
        clave = CLAVES_CABECERA.get(_norm(m.group(1)))
        if clave and clave not in encontrado:
            encontrado[clave] = m.group(2).strip()
    return encontrado


# --- Acceso a Chroma ---------------------------------------------------------

def _client():
    """Cliente persistente, con el mismo patron de agent/vectordb.py."""
    global _cliente, _ultimo_motivo
    if _cliente is None:
        import chromadb  # se importa perezoso: sin chromadb el modulo igual carga

        CHROMA_DIR.mkdir(parents=True, exist_ok=True)
        _cliente = chromadb.PersistentClient(path=str(CHROMA_DIR))
        _ultimo_motivo = None
    return _cliente


def _coleccion(nombre: str):
    """Devuelve la coleccion, o None si Chroma no esta disponible.

    `embedding_function=None` es intencional: los vectores siempre los calcula
    agent.llm (bge-m3 local). Si se dejara la funcion por defecto de Chroma,
    intentaria bajar un modelo ONNX de internet — y este agente corre en una
    planta sin internet.
    """
    global _ultimo_motivo
    if nombre in _colecciones:
        return _colecciones[nombre]
    try:
        col = _client().get_or_create_collection(
            nombre, metadata={"hnsw:space": "cosine"}, embedding_function=None
        )
    except ImportError:
        _ultimo_motivo = (
            "chromadb no esta instalado (pip install chromadb). Sin el no hay "
            "busqueda semantica, pero las herramientas numericas no dependen de esto"
        )
        return None
    except TypeError:
        # Versiones de Chroma que no aceptan embedding_function=None.
        try:
            col = _client().get_or_create_collection(
                nombre, metadata={"hnsw:space": "cosine"}
            )
        except Exception as e:  # noqa: BLE001
            _ultimo_motivo = f"no se pudo abrir la coleccion '{nombre}': {type(e).__name__}: {e}"
            return None
    except Exception as e:  # noqa: BLE001
        _ultimo_motivo = f"no se pudo abrir la coleccion '{nombre}': {type(e).__name__}: {e}"
        return None
    _colecciones[nombre] = col
    return col


def _sin_chroma(detalle: str | None = None) -> dict:
    motivo = detalle or _ultimo_motivo or "Chroma no responde en data/chroma_produccion/"
    return {
        "disponible": False,
        "motivo": (
            f"La búsqueda semántica está fuera de servicio: {motivo}. "
            "Las herramientas numéricas siguen funcionando."
        ),
    }


def _where(condiciones: list[dict]) -> dict | None:
    """Arma el filtro de Chroma. `$and` explicito, nunca varias claves sueltas.

    Chroma cambio de opinion entre versiones sobre si {"a": 1, "b": 2} es un AND
    implicito o un error. Con `$and` explicito funciona en todas.
    """
    if not condiciones:
        return None
    if len(condiciones) == 1:
        return condiciones[0]
    return {"$and": condiciones}


def _ids_que_pasan(col, where: dict | None) -> list[str]:
    """Ids que sobreviven al filtro de metadata, ANTES de tocar los vectores."""
    try:
        r = col.get(where=where, include=[])
    except Exception:
        # Algunas versiones no aceptan include vacio; se paga el costo de traer
        # documentos porque saber el tamano del universo filtrado no es opcional.
        r = col.get(where=where)
    return list((r or {}).get("ids") or [])


def _valores_distintos(col, clave: str) -> tuple:
    """Valores distintos de una clave de metadata, para reconciliar lo que pide
    el usuario con lo que de verdad hay indexado.

    Sin cache a proposito. Se cacheaba por (coleccion, clave, count), y ese
    conteo no cambia en el flujo mas normal del producto: el supervisor corrige
    una causa en la pestana Revisar, se reprocesa el archivo, indexar_observaciones
    borra y reinserta las MISMAS observaciones del turno. Mismo count, cache
    intacto, valores viejos. A partir de ahi la busqueda contesta "no hay
    observaciones de la linea L4" con los datos de L4 ahi mismo. Un falso
    negativo silencioso es peor que una consulta lenta: medido, el cache ahorra
    9 ms sobre 5000 pasajes.
    """
    try:
        metas = (col.get(include=["metadatas"]) or {}).get("metadatas") or []
    except Exception:
        return ()
    vistos = []
    for meta in metas:
        v = (meta or {}).get(clave)
        if isinstance(v, str) and v and v not in vistos:
            vistos.append(v)
    return tuple(vistos)


def _resolver(pedido: str, valores: tuple, es_linea: bool = False) -> list[str]:
    """Traduce lo que pidio el usuario a los valores exactos que hay indexados.

    Devuelve lista porque 'remachadora' puede corresponder a dos documentos
    ('Remachadora R-02' y 'Remachadora R-03') y recortar a uno seria inventar
    una decision que nadie tomo.
    """
    if es_linea:
        clave = _clave_linea(pedido)
        return [v for v in valores if _clave_linea(v) == clave]

    p = _norm(pedido)
    exactos = [v for v in valores if _norm(v) == p]
    if exactos:
        return exactos
    return [v for v in valores if p and (p in _norm(v) or _norm(v) in p)]


# --- Embeddings --------------------------------------------------------------

def _embeber(textos: list[str], tipo: str = "RETRIEVAL_DOCUMENT") -> list[list[float]]:
    """Vectoriza por lotes con el proveedor activo (bge-m3 en Ollama local).

    Lanza si el servidor de embeddings no responde; todos los llamadores lo
    capturan y lo convierten en disponible: False.
    """
    from agent import llm

    vectores: list[list[float]] = []
    for i in range(0, len(textos), LOTE):
        vectores.extend(llm.embed_textos(textos[i:i + LOTE], tipo))
    if len(vectores) != len(textos):
        raise RuntimeError(
            f"el proveedor devolvio {len(vectores)} vectores para {len(textos)} textos"
        )
    return vectores


# --- Estado ------------------------------------------------------------------

def estado() -> dict:
    """Cuantos documentos hay en cada coleccion. Para el panel de la app."""
    conteos: dict[str, int] = {}
    motivo = None
    for nombre in COLECCIONES:
        col = _coleccion(nombre)
        if col is None:
            motivo = _ultimo_motivo
            continue
        try:
            conteos[nombre] = col.count()
        except Exception as e:  # noqa: BLE001
            motivo = f"{type(e).__name__}: {e}"
    return {
        "disponible": bool(conteos) and any(v > 0 for v in conteos.values()),
        "directorio": str(CHROMA_DIR),
        "colecciones": conteos,
        "motivo": motivo,
    }


def disponible() -> bool:
    """True si Chroma responde y al menos una coleccion tiene documentos."""
    return bool(estado()["disponible"])


# --- Indexacion --------------------------------------------------------------

def indexar_observaciones(
    turno_id: int,
    documento_id: int,
    metadata: dict,
    textos: list[str],
) -> int:
    """Indexa el texto libre de UN turno. Devuelve cuantos pasajes quedaron.

    Los ids son deterministicos (`turno_id:i`) y antes de escribir se borra todo
    lo que ya existia de ese turno. Reprocesar el mismo reporte no duplica, y si
    la segunda extraccion sacó menos observaciones que la primera, las viejas no
    quedan colgando: la coleccion refleja el ultimo estado del turno, no la
    union de todos los intentos.
    """
    global _ultimo_motivo
    col = _coleccion("observaciones")
    if col is None:
        return 0

    base = _limpiar_metadata(metadata)
    base["turno_id"] = int(turno_id)
    base["documento_id"] = int(documento_id)
    # fecha_int es lo que hace posible filtrar por rango; si el llamador no lo
    # mando, se deriva de `fecha` aqui y no en cada consulta.
    if "fecha_int" not in base:
        fi = _fecha_int(base.get("fecha"))
        if fi is not None:
            base["fecha_int"] = fi

    # El indice original (i) se conserva aunque se salten textos vacios: el id
    # tiene que apuntar siempre a la misma observacion entre reprocesos.
    utiles = [(i, t.strip()) for i, t in enumerate(textos or []) if (t or "").strip()]

    try:
        col.delete(where={"turno_id": int(turno_id)})
    except Exception:
        pass  # si nunca se indexo, no hay nada que borrar

    if not utiles:
        return 0

    try:
        vectores = _embeber([t for _, t in utiles], "RETRIEVAL_DOCUMENT")
        col.add(
            ids=[f"{turno_id}:{i}" for i, _ in utiles],
            embeddings=vectores,
            documents=[t for _, t in utiles],
            metadatas=[dict(base) for _ in utiles],
        )
    except Exception as e:  # noqa: BLE001
        _ultimo_motivo = f"fallo al indexar el turno {turno_id}: {type(e).__name__}: {e}"
        return 0
    return len(utiles)


def indexar_procedimientos(carpeta: Path | None = None) -> int:
    """Indexa los .txt/.md de data/procedimientos/. Devuelve pasajes indexados.

    Carpeta vacia o inexistente devuelve 0 y no es un error: al arrancar el
    proyecto todavia no hay procedimientos cargados y el agente debe poder
    responder igual con observaciones y numeros.
    """
    global _ultimo_motivo
    carpeta = Path(carpeta) if carpeta else PROCEDIMIENTOS_DIR
    if not carpeta.is_dir():
        return 0

    archivos = sorted(
        p for p in carpeta.iterdir()
        if p.is_file() and p.suffix.lower() in EXTENSIONES
    )
    if not archivos:
        return 0

    col = _coleccion("procedimientos")
    if col is None:
        return 0

    total = 0
    for ruta in archivos:
        try:
            texto = ruta.read_text(encoding="utf-8", errors="replace")
        except Exception as e:  # noqa: BLE001
            _ultimo_motivo = f"no se pudo leer {ruta.name}: {type(e).__name__}: {e}"
            continue

        pasajes = _partir(texto)
        if not pasajes:
            continue

        cabecera = _cabecera(texto)
        # Sin cabecera explicita, el nombre del archivo ES la maquina. Es la
        # convencion mas barata de sostener para quien deja el .md en la carpeta.
        maquina = cabecera.get("maquina") or re.sub(r"[_\-]+", " ", ruta.stem).strip()

        base = _limpiar_metadata({
            "archivo": ruta.name,
            "documento": ruta.stem,
            "maquina": maquina,
            "linea": cabecera.get("linea"),
            "version": cabecera.get("version"),
        })

        try:
            col.delete(where={"archivo": ruta.name})
        except Exception:
            pass  # primera vez que se indexa este archivo: no hay nada que borrar

        try:
            vectores = _embeber([p["texto"] for p in pasajes], "RETRIEVAL_DOCUMENT")
            col.add(
                # ruta.name y no ruta.stem: con el stem, "bomba.txt" y "bomba.md"
                # generan los mismos ids y col.add NO lanza — se queda con el
                # documento viejo y descarta el nuevo en silencio, mientras la
                # funcion sigue reportando que lo indexo.
                ids=[f"{ruta.name}#{j}" for j in range(len(pasajes))],
                embeddings=vectores,
                documents=[p["texto"] for p in pasajes],
                metadatas=[
                    dict(base, seccion=(p["seccion"] or "(sin encabezado)"))
                    for p in pasajes
                ],
            )
        except Exception as e:  # noqa: BLE001
            _ultimo_motivo = f"fallo al indexar {ruta.name}: {type(e).__name__}: {e}"
            continue
        total += len(pasajes)
    return total


def indexar_resolucion(caso_id: str, texto: str, metadata: dict | None = None) -> int:
    """Guarda un caso cerrado: que paso y que se hizo. Devuelve 1 o 0.

    No esta en el contrato, pero sin esto la coleccion `resoluciones` nunca se
    puebla y `casos_similares` no tendria de donde responder. El caso_id lo pone
    quien cierra el caso (turno_id, ticket de mantenimiento, lo que sea) y hace
    la escritura idempotente.
    """
    global _ultimo_motivo
    texto = (texto or "").strip()
    if not texto:
        return 0
    col = _coleccion("resoluciones")
    if col is None:
        return 0

    base = _limpiar_metadata(metadata)
    base["caso_id"] = str(caso_id)
    if "fecha_int" not in base:
        fi = _fecha_int(base.get("fecha"))
        if fi is not None:
            base["fecha_int"] = fi

    try:
        vector = _embeber([texto], "RETRIEVAL_DOCUMENT")[0]
        col.upsert(
            ids=[f"res:{caso_id}"],
            embeddings=[vector],
            documents=[texto],
            metadatas=[base],
        )
    except Exception as e:  # noqa: BLE001
        _ultimo_motivo = f"fallo al indexar la resolucion {caso_id}: {type(e).__name__}: {e}"
        return 0
    return 1


# --- Busqueda ----------------------------------------------------------------

def _hit(doc: str, meta: dict, distancia: float | None) -> dict:
    """Un resultado listo para citar: texto, de donde salio, y que tan parecido."""
    meta = meta or {}
    partes = [
        str(meta[c]) for c in ("linea", "estacion", "maquina", "seccion")
        if meta.get(c)
    ]
    if meta.get("turno"):
        partes.append(f"turno {meta['turno']}")
    if meta.get("fecha"):
        partes.append(str(meta["fecha"]))
    return {
        "texto": doc,
        # La coleccion es coseno, asi que distancia = 1 - similitud.
        "similitud": round(1.0 - distancia, 3) if distancia is not None else None,
        "fecha": meta.get("fecha"),
        "turno": meta.get("turno"),
        "linea": meta.get("linea"),
        "cita": " · ".join(partes) if partes else "(sin ubicacion registrada)",
        "metadata": meta,
    }


def _sin_consulta(consulta: str) -> dict | None:
    """Corta antes de tocar nada si no hay pregunta que vectorizar."""
    if (consulta or "").strip():
        return None
    return {"disponible": False, "motivo": "La consulta viene vacía; no hay qué buscar."}


def _buscar(
    nombre: str,
    consulta: str,
    condiciones: list[dict],
    k: int,
    filtro_legible: str,
) -> dict:
    """Nucleo comun: filtra por metadata, mide el universo, y solo despues busca.

    El orden de las tres etapas es el contrato entero de este modulo:
      1. `where` recorta el universo (determinista, auditable),
      2. se mide cuanto quedo dentro,
      3. el vector ORDENA ese universo. Nunca lo amplia.
    """
    vacia = _sin_consulta(consulta)
    if vacia:
        return vacia

    col = _coleccion(nombre)
    if col is None:
        return _sin_chroma()

    try:
        total = col.count()
    except Exception as e:  # noqa: BLE001
        return _sin_chroma(f"{type(e).__name__}: {e}")

    if total == 0:
        return {
            "disponible": False,
            "motivo": (
                f"La colección '{nombre}' está vacía: todavía no se ha indexado nada. "
                "Hay que cargar reportes (o procedimientos) antes de poder buscar texto."
            ),
        }

    where = _where(condiciones)
    try:
        universo = len(_ids_que_pasan(col, where))
    except Exception as e:  # noqa: BLE001
        return {
            "disponible": False,
            "motivo": f"Chroma rechazó el filtro {where}: {type(e).__name__}: {e}",
        }

    if universo == 0:
        return {
            "disponible": False,
            "motivo": (
                f"No hay nada indexado en '{nombre}' que cumpla {filtro_legible}. "
                f"La colección tiene {total} pasajes, pero ninguno de ese universo. "
                "Ojo: esto no significa que no haya pasado nada; significa que no "
                "hay reportes cargados para ese recorte."
            ),
            "total_indexado": total,
        }

    try:
        vector = _embeber([consulta], "RETRIEVAL_QUERY")[0]
    except Exception as e:  # noqa: BLE001
        return {
            "disponible": False,
            "motivo": (
                "No se pudo vectorizar la consulta (¿Ollama está arriba y tiene el "
                f"modelo de embeddings?): {type(e).__name__}: {e}"
            ),
        }

    try:
        r = col.query(
            query_embeddings=[vector],
            n_results=max(1, min(int(k), universo)),
            where=where,  # el filtro entra en la consulta, no despues de ella
            include=["documents", "metadatas", "distances"],
        )
    except Exception as e:  # noqa: BLE001
        return {
            "disponible": False,
            "motivo": f"Falló la consulta a Chroma: {type(e).__name__}: {e}",
        }

    docs = (r.get("documents") or [[]])[0]
    metas = (r.get("metadatas") or [[]])[0]
    dist = (r.get("distances") or [[]])[0] or [None] * len(docs)

    datos = [_hit(d, m, x) for d, m, x in zip(docs, metas, dist)]
    if not datos:
        return {
            "disponible": False,
            "motivo": (
                f"El filtro dejó {universo} pasajes dentro, pero la búsqueda "
                "semántica no devolvió ninguno. Reintentar o revisar el índice."
            ),
        }

    return {
        "disponible": True,
        "datos": datos,
        "coleccion": nombre,
        "filtro": filtro_legible,
        # Lo que el agente necesita para no exagerar: sobre cuantos pasajes se
        # eligieron estos, y cuantos hay en total sin filtrar.
        "universo_filtrado": universo,
        "total_indexado": total,
    }


def buscar_observaciones(
    consulta: str,
    linea: str | None = None,
    desde: str | None = None,
    hasta: str | None = None,
    k: int = 5,
) -> dict:
    """Busca en el texto libre que escribieron los supervisores.

    `linea`, `desde` y `hasta` recortan ANTES de comparar vectores. Si se pide
    una linea que no existe en lo indexado, o una fecha que no se entiende, se
    devuelve disponible: False en vez de buscar sin filtro: devolver los pasajes
    mas parecidos de OTRA linea es precisamente el error que este modulo evita.
    """
    vacia = _sin_consulta(consulta)
    if vacia:
        return vacia

    col = _coleccion("observaciones")
    if col is None:
        return _sin_chroma()

    condiciones: list[dict] = []
    legibles: list[str] = []

    if linea:
        conocidas = _valores_distintos(col, "linea")
        coincidencias = _resolver(str(linea), conocidas, es_linea=True)
        if not coincidencias:
            if not conocidas:
                return {
                    "disponible": False,
                    "motivo": (
                        "Todavía no hay observaciones indexadas: no se ha cargado "
                        "ningún reporte de turno con texto libre."
                    ),
                }
            return {
                "disponible": False,
                "motivo": (
                    f"No hay observaciones indexadas de la línea '{linea}'. "
                    f"Las líneas con texto cargado son: {', '.join(conocidas)}."
                ),
            }
        condiciones.append(
            {"linea": {"$eq": coincidencias[0]}} if len(coincidencias) == 1
            else {"linea": {"$in": list(coincidencias)}}
        )
        legibles.append(f"linea {'/'.join(coincidencias)}")

    for valor, operador, etiqueta in ((desde, "$gte", "desde"), (hasta, "$lte", "hasta")):
        if not valor:
            continue
        fi = _fecha_int(valor)
        if fi is None:
            return {
                "disponible": False,
                "motivo": (
                    f"No entendí la fecha '{valor}'. Se espera formato ISO "
                    "YYYY-MM-DD; sin eso el rango no recorta y la respuesta "
                    "saldría sobre el período equivocado."
                ),
            }
        condiciones.append({"fecha_int": {operador: fi}})
        legibles.append(f"{etiqueta} {valor}")

    return _buscar(
        "observaciones", consulta, condiciones, k,
        " y ".join(legibles) if legibles else "sin filtro (todas las lineas y fechas)",
    )


def buscar_procedimiento(consulta: str, maquina: str | None = None, k: int = 3) -> dict:
    """Busca en los procedimientos de planta, opcionalmente de una sola maquina.

    El nombre de maquina se reconcilia contra lo que hay indexado antes de
    filtrar ('remachadora' encuentra 'Remachadora R-02'). Si no hay ninguna
    coincidencia se dice cuales existen, en vez de responder con el
    procedimiento de otra maquina, que es peor que no responder.
    """
    vacia = _sin_consulta(consulta)
    if vacia:
        return vacia

    col = _coleccion("procedimientos")
    if col is None:
        return _sin_chroma()

    condiciones: list[dict] = []
    legible = "todos los procedimientos"

    if maquina:
        conocidas = _valores_distintos(col, "maquina")
        coincidencias = _resolver(str(maquina), conocidas)
        if not coincidencias:
            if not conocidas:
                return {
                    "disponible": False,
                    "motivo": (
                        "No hay ningún procedimiento indexado. Hay que dejar los "
                        f".txt/.md en {PROCEDIMIENTOS_DIR} y correr "
                        "indexar_procedimientos()."
                    ),
                }
            return {
                "disponible": False,
                "motivo": (
                    f"No hay procedimiento cargado para '{maquina}'. "
                    f"Hay procedimientos de: {', '.join(conocidas)}."
                ),
            }
        condiciones.append(
            {"maquina": {"$eq": coincidencias[0]}} if len(coincidencias) == 1
            else {"maquina": {"$in": list(coincidencias)}}
        )
        legible = f"máquina {'/'.join(coincidencias)}"

    return _buscar("procedimientos", consulta, condiciones, k, legible)


def casos_similares(descripcion: str, k: int = 3) -> dict:
    """Busca casos parecidos ya cerrados: "esto ya pasó, esto fue lo que se hizo".

    Sin filtro de linea ni de fecha a proposito: la gracia es encontrar el caso
    de L4 del mes pasado que se parece al de hoy en L2. Aqui lo que recorta es la
    coleccion, no la metadata.

    Si todavia no hay resoluciones registradas, cae a `observaciones` y lo dice
    en el resultado. Un caso similar contado por el supervisor que lo vivio vale
    mas que un "no disponible", pero el agente tiene que saber que esta leyendo
    una observacion cruda y no una resolucion verificada.
    """
    r = _buscar("resoluciones", descripcion, [], k, "casos cerrados")
    if r.get("disponible"):
        for hit in r["datos"]:
            hit["fuente"] = "resolucion registrada"
        return r

    alterno = _buscar("observaciones", descripcion, [], k, "observaciones de turno")
    if not alterno.get("disponible"):
        # Ninguna de las dos colecciones sirve: se reporta el motivo original,
        # que es el que explica por que no hay resoluciones.
        return r

    for hit in alterno["datos"]:
        hit["fuente"] = "observacion de turno (no es una resolucion verificada)"
    alterno["nota"] = (
        "No hay resoluciones registradas todavía, así que estos casos salen del "
        "texto libre de los turnos: describen qué pasó, no necesariamente qué se "
        "hizo ni si funcionó. Cítalos como observación del turno y la fecha."
    )
    alterno["coleccion"] = "observaciones (fallback de resoluciones)"
    return alterno
