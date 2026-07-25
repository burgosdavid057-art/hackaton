"""
Recuperacion sobre los manuales de usuario (la parte RAG).

Los manuales se parten en pasajes por seccion y se recuperan por relevancia.
Cada pasaje vuelve con su ubicacion en el documento, para poder citarlo: sin
cita no hay respuesta fundamentada.

Se usa BM25 sobre el texto, que no consume cuota de API y es reproducible.
`buscar()` es el unico punto de entrada; cambiar a embeddings solo requiere
reemplazar `_ranking`.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from functools import lru_cache
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent

# Encabezados tipicos de los manuales de Haceb; sirven para cortar por seccion.
ENCABEZADO = re.compile(
    r"^\s*(?:[0-9]+[\.\)]\s*)?("
    r"POSIBLE CAUSA|SOLUCI[OÓ]N|GARANT[IÍ]A|EXCLUSIONES|INSTALACI[OÓ]N|"
    r"LIMPIEZA|MANTENIMIENTO|USO|FUNCIONES|PROGRAMAS|ESPECIFICACIONES|"
    r"ADVERTENCIA|PRECAUCI[OÓ]N|CUIDADO|RECOMENDACIONES|CARACTER[IÍ]STICAS"
    r")[^\n]*$",
    re.I | re.M,
)

VACIAS = set("""
de la el en que los del se con para por una las no es su al lo como mas pero sus
si ya cuando todo esta son entre hasta donde muy sin sobre tambien o y a un uno
este esta estos estas ser hacer puede debe cada otro otra
""".split())


def _norm(texto: str) -> str:
    texto = unicodedata.normalize("NFD", (texto or "").lower())
    return "".join(c for c in texto if unicodedata.category(c) != "Mn")


def _tokens(texto: str) -> list[str]:
    return [t for t in re.split(r"\W+", _norm(texto)) if len(t) > 2 and t not in VACIAS]


def _partir(texto: str, objetivo: int = 900) -> list[dict]:
    """Parte el manual en pasajes, respetando los encabezados de seccion."""
    cortes = [0] + [m.start() for m in ENCABEZADO.finditer(texto)] + [len(texto)]
    secciones = []
    for i in range(len(cortes) - 1):
        bloque = texto[cortes[i]:cortes[i + 1]].strip()
        if bloque:
            titulo_m = ENCABEZADO.match(bloque)
            titulo = (titulo_m.group(0).strip() if titulo_m else "")[:70]
            secciones.append((titulo, bloque))

    pasajes = []
    for titulo, bloque in secciones:
        # Secciones largas se subdividen por parrafos hasta el tamano objetivo.
        actual, tam = [], 0
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
    return [p for p in pasajes if len(p["texto"]) > 80]


@lru_cache(maxsize=32)
def _indice(manual_file: str) -> tuple:
    """Construye (pasajes, frecuencias, idf, largo_medio) para un manual."""
    ruta = RAIZ / manual_file
    if not ruta.exists():
        return ((), (), {}, 0.0)

    texto = ruta.read_text(encoding="utf-8")
    pasajes = _partir(texto)
    frec = [Counter(_tokens(p["texto"])) for p in pasajes]

    n = len(pasajes) or 1
    docs_con = Counter()
    for f in frec:
        docs_con.update(f.keys())
    idf = {
        t: math.log(1 + (n - c + 0.5) / (c + 0.5))
        for t, c in docs_con.items()
    }
    largo_medio = sum(sum(f.values()) for f in frec) / n
    return (tuple(pasajes), tuple(frec), idf, largo_medio)


def _ranking(pregunta: str, manual_file: str, k: int) -> list[tuple[float, dict]]:
    """BM25 clasico."""
    pasajes, frec, idf, largo_medio = _indice(manual_file)
    if not pasajes:
        return []

    consulta = _tokens(pregunta)
    if not consulta:
        return []

    k1, b = 1.5, 0.75
    puntuados = []
    for i, f in enumerate(frec):
        largo = sum(f.values()) or 1
        s = 0.0
        for t in consulta:
            if t not in f:
                continue
            tf = f[t]
            s += idf.get(t, 0.0) * (tf * (k1 + 1)) / (
                tf + k1 * (1 - b + b * largo / (largo_medio or 1))
            )
        if s > 0:
            puntuados.append((s, pasajes[i]))

    puntuados.sort(key=lambda x: -x[0])
    return puntuados[:k]


# --- Capa semantica ----------------------------------------------------------
#
# BM25 solo falla cuando el usuario y el manual usan palabras distintas para lo
# mismo ("no enfria" vs "poco frio en el compartimiento inferior"). Los
# embeddings cierran esa brecha. Se combinan ambos: BM25 acierta en referencias
# y codigos exactos, los embeddings en sintomas descritos con otras palabras.

INDICE_DIR = RAIZ / "data" / "index"


LOTE = 16          # la capa gratuita rechaza lotes grandes
PAUSA_SEG = 4.0    # ritmo entre lotes para no chocar con el limite por minuto


def embeber(
    textos: list[str],
    tipo: str = "RETRIEVAL_DOCUMENT",
    verboso: bool = False,
) -> list[list[float]]:
    """Vectoriza en lotes con el proveedor activo (Ollama local o Gemini).

    Con Ollama los embeddings son locales e ilimitados. Con Gemini hay cuota,
    por eso se espera entre lotes y el indice se precomputa una sola vez.
    """
    import time as _t

    from . import llm

    es_ollama = llm.proveedor() == "ollama"
    vectores: list[list[float]] = []
    for i in range(0, len(textos), LOTE):
        lote = textos[i:i + LOTE]
        vectores.extend(llm.embed_textos(lote, tipo))
        if verboso:
            print(f"      +{len(lote)} vectores")
        if i + LOTE < len(textos) and not es_ollama:
            _t.sleep(PAUSA_SEG)  # solo Gemini necesita respirar entre lotes
    return vectores


def _normalizar(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def _ruta_indice(manual_file: str) -> Path:
    return INDICE_DIR / (Path(manual_file).stem + ".json")


def construir_indice(manual_file: str, forzar: bool = False) -> int:
    """Precomputa y guarda los vectores de un manual. Devuelve cuantos pasajes."""
    import json

    destino = _ruta_indice(manual_file)
    pasajes, *_ = _indice(manual_file)
    if not pasajes:
        return 0
    if destino.exists() and not forzar:
        return len(pasajes)

    from . import llm

    vectores = embeber([p["texto"] for p in pasajes], "RETRIEVAL_DOCUMENT", verboso=True)
    INDICE_DIR.mkdir(parents=True, exist_ok=True)
    # Se anota el embedder REAL que produjo estos vectores, no una constante.
    # Sin esto no hay forma de saber si un indice en disco es comparable con el
    # modelo que esta activo hoy.
    destino.write_text(
        json.dumps(
            {"modelo": llm.modelo_embed(),
             "dim": len(vectores[0]) if vectores else 0,
             "vectores": [[round(x, 5) for x in _normalizar(v)] for v in vectores]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return len(pasajes)


_AVISADOS: set[str] = set()


@lru_cache(maxsize=32)
def _vectores(manual_file: str) -> tuple:
    """Vectores precomputados del manual, SOLO si los produjo el embedder activo.

    Un indice construido con otro modelo no sirve: las dimensiones pueden ni
    coincidir, y si coinciden por casualidad las similitudes salen sin sentido.
    Nada de eso lanza una excepcion, asi que hay que atajarlo aqui: si el
    indice no corresponde, se ignora y la busqueda cae a BM25 sola. Degradar es
    aceptable; comparar vectores incompatibles en silencio no.
    """
    import json

    from . import llm

    ruta = _ruta_indice(manual_file)
    if not ruta.exists():
        return ()
    datos = json.loads(ruta.read_text(encoding="utf-8"))

    construido_con = datos.get("modelo")
    activo = llm.modelo_embed()
    if construido_con != activo:
        if manual_file not in _AVISADOS:
            _AVISADOS.add(manual_file)
            print(
                f"  aviso: el indice de {Path(manual_file).name} se construyo con "
                f"'{construido_con}' y el embedder activo es '{activo}'. Se ignora "
                f"y se usa solo BM25. Reconstruye con: python build_index.py"
            )
        return ()

    return tuple(tuple(v) for v in datos["vectores"])


def _semantico(pregunta: str, manual_file: str) -> dict[int, float]:
    """Similitud semantica pasaje-pregunta, mapeada al indice de pasaje.

    Preferimos la base vectorial Chroma (agent/vectordb.py). Si no esta poblada,
    caemos a la similitud coseno sobre los vectores en JSON, y si tampoco hay,
    a nada (el hibrido usa solo BM25). Los tres caminos degradan sin romper.
    """
    # 1) Base vectorial Chroma: la ruta preferida.
    try:
        from . import vectordb

        if vectordb.disponible():
            pasajes, *_ = _indice(manual_file)
            por_texto = {p["texto"]: i for i, p in enumerate(pasajes)}
            puntajes: dict[int, float] = {}
            for hit in vectordb.buscar(pregunta, manual_file, k=len(pasajes) or 4):
                i = por_texto.get(hit["texto"])
                if i is not None:
                    puntajes[i] = hit["similitud"]
            if puntajes:
                return puntajes
    except Exception:
        pass  # cualquier problema con Chroma: seguimos con el fallback

    # 2) Fallback: coseno sobre los vectores guardados en JSON.
    vecs = _vectores(manual_file)
    if not vecs:
        return {}
    try:
        q = _normalizar(embeber([pregunta], "RETRIEVAL_QUERY")[0])
    except Exception:
        return {}  # sin red o sin cuota: seguimos con BM25 solo
    return {i: sum(a * b for a, b in zip(q, v)) for i, v in enumerate(vecs)}


def _escala(valores: dict[int, float]) -> dict[int, float]:
    if not valores:
        return {}
    lo, hi = min(valores.values()), max(valores.values())
    if hi - lo < 1e-9:
        return {i: 1.0 for i in valores}
    return {i: (v - lo) / (hi - lo) for i, v in valores.items()}


def buscar(pregunta: str, manual_file: str, k: int = 3, semantico: bool = True) -> list[dict]:
    """Devuelve los pasajes mas relevantes del manual, listos para citar.

    Combina BM25 (bueno con codigos y terminos exactos) con similitud semantica
    (buena cuando el usuario describe el sintoma con sus propias palabras).
    """
    pasajes, *_ = _indice(manual_file)
    if not pasajes:
        return []

    lexico = {}
    for score, pasaje in _ranking(pregunta, manual_file, len(pasajes)):
        lexico[pasajes.index(pasaje)] = score

    semantica = _semantico(pregunta, manual_file) if semantico else {}
    lex_n, sem_n = _escala(lexico), _escala(semantica)

    combinado: dict[int, float] = {}
    for i in set(lex_n) | set(sem_n):
        combinado[i] = 0.4 * lex_n.get(i, 0.0) + 0.6 * sem_n.get(i, 0.0)

    mejores = sorted(combinado.items(), key=lambda x: -x[1])[:k]
    if not mejores:
        return []

    tope = mejores[0][1] or 1.0
    return [
        {
            "seccion": pasajes[i]["seccion"] or "(sin encabezado)",
            "texto": pasajes[i]["texto"][:1100],
            "relevancia": round(s / tope, 2),
            "metodo": "hibrido" if sem_n else "lexico",
        }
        for i, s in mejores
    ]
