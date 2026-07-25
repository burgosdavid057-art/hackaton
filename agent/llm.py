"""
Capa unica de acceso al modelo, con rotacion de llaves.

Todo el agente (loop, validador, vision, embeddings) pasa por aqui. El motivo
es operativo: la capa gratuita de Gemini tiene una cuota agresiva y una sola
llave se agota. Si el equipo pone varias llaves, el agente rota a la siguiente
en cuanto una devuelve 429, y el demo no se cae en mitad del pitch.

Las llaves se leen del .env, en cualquiera de estas formas:

    GOOGLE_API_KEY=key1
    GOOGLE_API_KEY_2=key2
    GOOGLE_API_KEY_3=key3
    # o, equivalente:
    GOOGLE_API_KEYS=key1,key2,key3

El orden de intento es estable dentro de un proceso; cuando una llave se marca
agotada se manda al final de la fila.
"""

from __future__ import annotations

import os
import threading
import time

from dotenv import load_dotenv

MODELO = "gemini-flash-latest"
MODELO_EMBED = "gemini-embedding-001"

_lock = threading.Lock()
_pool: list[str] | None = None
_clientes: dict[str, object] = {}


# --- Seleccion de proveedor del AGENTE (generacion de texto + tool calling) --
#
# El agente puede correr sobre tres motores, en este orden de preferencia:
#   1. Ollama  -> modelo local en otra maquina (Mac del equipo). Sin cuota ni
#                 costo, y los datos no salen de la red local.
#   2. Groq    -> nube gratuita con cuota generosa.
#   3. Gemini  -> el proveedor nativo (google-genai).
#
# Ollama y Groq hablan el protocolo OpenAI, asi que comparten backend
# (agent/openai_backend.py). Gemini usa su SDK nativo (agent/loop.py).
#
# Los embeddings (RAG) y la vision (foto) siguen en Gemini; si no hay cuota, el
# RAG cae a BM25 y la vision queda deshabilitada, sin romper nada.

def proveedor() -> str:
    load_dotenv()
    if os.environ.get("OLLAMA_HOST") or os.environ.get("OLLAMA_MODEL"):
        return "ollama"
    if os.environ.get("GROQ_API_KEY"):
        return "groq"
    return "gemini"


def config_openai() -> dict:
    """base_url, api_key y modelo para el backend compatible con OpenAI."""
    load_dotenv()
    prov = proveedor()
    if prov == "ollama":
        host = os.environ.get("OLLAMA_HOST", "localhost:11434").strip()
        if not host.startswith("http"):
            host = "http://" + host
        return {
            "base_url": host.rstrip("/") + "/v1",
            "api_key": os.environ.get("OLLAMA_API_KEY", "ollama"),
            "modelo": os.environ.get("OLLAMA_MODEL", "llama3.3"),
            "proveedor": "ollama",
            # Ventana de contexto: Ollama usa 2048 por defecto, muy poco para el
            # system prompt + 9 herramientas + resultados + historial. Se sube
            # para conversaciones naturales de varios turnos.
            "num_ctx": int(os.environ.get("OLLAMA_NUM_CTX", "8192")),
        }
    # groq
    return {
        "base_url": "https://api.groq.com/openai/v1",
        "api_key": os.environ.get("GROQ_API_KEY", ""),
        "modelo": os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile"),
        "proveedor": "groq",
    }


def _cargar_llaves() -> list[str]:
    load_dotenv()
    llaves: list[str] = []

    # GOOGLE_API_KEYS separadas por coma
    for k in (os.environ.get("GOOGLE_API_KEYS") or "").split(","):
        k = k.strip()
        if k:
            llaves.append(k)

    # GOOGLE_API_KEY, GOOGLE_API_KEY_2, GOOGLE_API_KEY_3, ...
    principal = os.environ.get("GOOGLE_API_KEY", "").strip()
    if principal:
        llaves.append(principal)
    for i in range(2, 11):
        k = (os.environ.get(f"GOOGLE_API_KEY_{i}") or "").strip()
        if k:
            llaves.append(k)

    # de-duplicar conservando orden
    vistas, unicas = set(), []
    for k in llaves:
        if k not in vistas:
            vistas.add(k)
            unicas.append(k)
    return unicas


def _llaves() -> list[str]:
    global _pool
    if _pool is None:
        _pool = _cargar_llaves()
        if not _pool:
            raise RuntimeError(
                "No hay ninguna GOOGLE_API_KEY en el .env. Copia .env.example "
                "a .env y pon al menos una llave de https://aistudio.google.com/apikey"
            )
    return _pool


def cuantas_llaves() -> int:
    try:
        return len(_llaves())
    except RuntimeError:
        return 0


def _cliente(llave: str):
    if llave not in _clientes:
        from google import genai
        _clientes[llave] = genai.Client(api_key=llave)
    return _clientes[llave]


def _es_reintentable(e: Exception) -> bool:
    """Errores donde conviene rotar de llave o reintentar, no abortar.

    - 429 / RESOURCE_EXHAUSTED: cuota agotada.
    - 403 SERVICE_DISABLED: el proyecto de esa llave acaba de habilitar la API
      de Gemini y aun esta propagando (o no la tiene activa). Rotar a otra llave
      resuelve, y si es propagacion, un reintento tras la espera la toma.
    - 500 / 503 / UNAVAILABLE: hipos transitorios del servicio.
    """
    s = str(e)
    return (
        "429" in s or "RESOURCE_EXHAUSTED" in s
        # 403: proyecto recien habilitado que aun propaga permisos, o una llave
        # sin acceso a esta operacion. Rotar a otra llave es la salida.
        or "403" in s or "PERMISSION_DENIED" in s
        or "SERVICE_DISABLED" in s or "has not been used in project" in s
        or "503" in s or "UNAVAILABLE" in s or "500" in s
    )


def _degradar(llave: str) -> None:
    """Manda la llave agotada al final de la fila, sin perderla."""
    with _lock:
        if _pool and llave in _pool and len(_pool) > 1:
            _pool.remove(llave)
            _pool.append(llave)


def _ejecutar(operacion, espera_por_ronda: float = 8.0):
    """Corre `operacion(cliente)` rotando llaves ante 429.

    Da una vuelta completa a las llaves; si todas estan agotadas, espera y
    reintenta unas rondas, por si el limite es por minuto y se libera.
    """
    llaves = _llaves()
    ultimo_error: Exception | None = None

    for ronda in range(4):
        for llave in list(llaves):
            try:
                return operacion(_cliente(llave))
            except Exception as e:  # noqa: BLE001
                ultimo_error = e
                if _es_reintentable(e):
                    _degradar(llave)
                    continue
                raise
        # todas las llaves dieron 429 en esta vuelta
        if ronda < 3:
            time.sleep(espera_por_ronda * (ronda + 1))

    raise ultimo_error if ultimo_error else RuntimeError("fallo desconocido en llm")


# --- API publica -------------------------------------------------------------

def generar(contents, config=None):
    """generate_content con rotacion de llaves."""
    return _ejecutar(
        lambda c: c.models.generate_content(model=MODELO, contents=contents, config=config)
    )


def embed(contents, config=None):
    """embed_content de Gemini, con rotacion de llaves."""
    return _ejecutar(
        lambda c: c.models.embed_content(model=MODELO_EMBED, contents=contents, config=config)
    )


def modelo_embed() -> str:
    """Nombre del embedder activo. Lo usa el RAG para etiquetar sus indices.

    Importa porque los indices NO son intercambiables entre embedders: bge-m3
    da 1024 dimensiones y gemini-embedding-001 da 768. Comparar vectores de dos
    modelos distintos no lanza ningun error, solo devuelve similitudes sin
    sentido — el peor tipo de falla, porque se ve igual que una que funciona.
    """
    load_dotenv()
    if proveedor() == "ollama":
        return os.environ.get("OLLAMA_EMBED_MODEL", "nomic-embed-text").strip()
    return MODELO_EMBED


def embed_textos(textos, tipo: str = "RETRIEVAL_DOCUMENT") -> list[list[float]]:
    """Vectoriza con el proveedor de embeddings activo.

    Si el agente corre sobre Ollama, los embeddings tambien son locales
    (bge-m3 o nomic-embed-text): RAG 100% en la maquina, sin cuota ni nube.
    Si no, usa Gemini.

    La dimension DEPENDE del modelo (bge-m3: 1024, nomic-embed-text: 768,
    gemini-embedding-001: 768 por configuracion). Quien guarde estos vectores
    tiene que anotar con que modelo los produjo.
    """
    load_dotenv()
    textos = list(textos)

    if proveedor() == "ollama":
        modelo = os.environ.get("OLLAMA_EMBED_MODEL", "nomic-embed-text").strip()
        from openai import OpenAI

        # nomic-embed-text rinde bastante mejor con sus prefijos de tarea:
        # distingue el texto que se guarda del que se consulta.
        entrada = textos
        if "nomic" in modelo:
            pref = "search_query: " if tipo == "RETRIEVAL_QUERY" else "search_document: "
            entrada = [pref + t for t in textos]

        cfg = config_openai()
        cli = OpenAI(base_url=cfg["base_url"], api_key=cfg["api_key"], timeout=120.0)
        r = cli.embeddings.create(model=modelo, input=entrada)
        return [d.embedding for d in r.data]

    # Gemini
    from google.genai import types

    r = embed(
        textos,
        config=types.EmbedContentConfig(task_type=tipo, output_dimensionality=768),
    )
    return [list(e.values) for e in r.embeddings]


def disponible() -> bool:
    """True si hay al menos una llave y responde. Gasta una llamada minima."""
    try:
        generar("ok")
        return True
    except Exception:
        return False
