"""
Resumen ejecutivo: Python decide que consultar, el modelo solo redacta.

La diferencia con `agente.py` es el reparto de trabajo. Ahi el modelo elige
herramientas; aqui no elige nada. Este modulo llama las siete herramientas en un
orden fijo, arma un bloque de hechos ya calculados, y recien entonces hace UN
solo llamado al LLM para que lo escriba en prosa.

El motivo es que un resumen ejecutivo tiene una forma conocida de antemano:
cuanto se produjo, que paro la linea, cuanto se boto, que se esta repitiendo.
No hace falta que un modelo de 7B en CPU descubra esa secuencia turno a turno
—se equivoca, se salta llamadas y tarda tres minutos en hacerlo—. La secuencia
esta escrita aqui, en Python, y es la misma cada vez.

El efecto de fondo: el modelo nunca ve la pregunta "cuanto scrap hubo". Ve la
tabla con la respuesta. No puede equivocarse en una cuenta que no hizo.

Prueba sin UI:
    python -m produccion.reporte 2026-07-20 2026-07-25
    python -m produccion.reporte 2026-07-20 2026-07-25 L2
"""

from __future__ import annotations

import json
import sys
from datetime import date

from agent import llm

from . import agente, herramientas, prompts, rag

# --- Parametros de la corrida ------------------------------------------------

TOP_PARADAS = 5              # el pareto que pide el resumen, no el completo
MIN_REPETICIONES = 2         # en una ventana corta, dos veces ya es un patron
TOP_CAUSAS_RAG = 3           # sobre cuantas causas se va a buscar texto libre
K_OBSERVACIONES = 3          # pasajes por causa
MAX_FILAS_EN_HECHOS = 8      # recorte del bloque que ve el modelo
MAX_CHARS_TEXTO = 400        # truncado de cada observacion

# El modelo local corre en CPU (qwen2.5:7b): redactar ~700 tokens puede tomar
# varios minutos. Un timeout corto aqui se ve como "el resumen no sirve" cuando
# en realidad solo faltaba esperar.
TIMEOUT_LLM = 300.0
NUM_CTX_MINIMO = 8192
# Mas baja que la del agente (0.2): aqui no se quiere variedad, se quiere que
# respete una estructura fija y copie cifras sin adornarlas.
TEMPERATURA = 0.1

# Dictamen para los resumenes que redacta Python sin pasar por el modelo (no hay
# datos, o el modelo no respondio). No es una aprobacion complaciente: si el
# texto lo genero el codigo a partir de la salida de las herramientas, no hay
# afirmacion que auditar.
DICTAMEN_SIN_MODELO = {
    "verificado": True,
    "fundamentada": True,
    "afirmaciones_sin_respaldo": [],
    "explicacion": "Texto armado en Python desde la salida de las herramientas; el modelo no intervino.",
}

AVISO_NO_VALIDADO = (
    "\n\n---\n"
    "⚠️ *El auditor no respaldó todo este resumen contra la evidencia: {detalle} "
    "Contrástalo con los reportes de turno antes de decidir sobre él.*"
)


# --- Utilidades sobre la convencion de retorno de las herramientas -----------

def _disponible(resultado: object) -> bool:
    return bool(isinstance(resultado, dict) and resultado.get("disponible"))


def _invocar(nombre: str, funcion, **argumentos) -> dict:
    """Llama una herramienta y normaliza cualquier fallo a la convencion del repo.

    No se usa `herramientas.ejecutar` a proposito: el orden de llamadas de este
    modulo es fijo y explicito, y no tiene por que depender del registro de
    nombres de otro modulo. Lo que si se respeta es la convencion de retorno —
    una excepcion se convierte en `disponible: False` con motivo, nunca en un
    cero ni en una lista vacia que despues alguien lea como "no hubo paradas".
    """
    try:
        resultado = funcion(**argumentos)
    except Exception as e:  # noqa: BLE001
        return {
            "disponible": False,
            "motivo": f"la herramienta {nombre} fallo: {type(e).__name__}: {e}",
        }
    if not isinstance(resultado, dict):
        return {
            "disponible": False,
            "motivo": f"la herramienta {nombre} no devolvio un dict",
        }
    return resultado


CLAVES_LISTA = (
    "filas", "causas", "recurrentes", "paradas", "scrap", "top", "items",
    "lineas", "registros", "detalle", "resultados", "pasajes", "observaciones",
)


def _filas(resultado: dict) -> list[dict]:
    """Saca la lista de filas de un resultado, sin casarse con un nombre de clave.

    El contrato fija la forma externa (`disponible`, `datos`, `cobertura`) pero
    no como se llaman las columnas de cada herramienta. Este modulo se escribe
    en paralelo a `herramientas.py`, asi que lee de forma tolerante: si un dia
    `datos` pasa de lista a `{"causas": [...]}`, el resumen sigue saliendo en vez
    de romperse en la demo.
    """
    if not _disponible(resultado):
        return []
    datos = resultado.get("datos")
    if isinstance(datos, list):
        return [f for f in datos if isinstance(f, dict)]
    if isinstance(datos, dict):
        for clave in CLAVES_LISTA:
            valor = datos.get(clave)
            if isinstance(valor, list):
                return [f for f in valor if isinstance(f, dict)]
        # Un dict de agregados (ej. el scrap total de una linea) es una sola fila.
        return [datos]
    return []


# Orden de preferencia para medir "impacto". El costo manda cuando existe porque
# es lo unico que permite comparar minutos de parada con unidades de scrap en la
# misma escala; si no hay tarifas cargadas se cae a minutos y despues a unidades.
CLAVES_IMPACTO = (
    "costo_cop", "costo_total_cop", "impacto_cop", "costo",
    "minutos_totales", "total_minutos", "minutos_perdidos", "minutos",
    "unidades_totales", "total_unidades", "unidades_scrap", "unidades",
    "kg_totales", "kg",
    "repeticiones", "ocurrencias", "veces", "conteo",
)


def _impacto(fila: dict) -> tuple[float, str]:
    """(valor, clave) del primer campo de impacto que traiga la fila."""
    for clave in CLAVES_IMPACTO:
        valor = fila.get(clave)
        if isinstance(valor, (int, float)) and not isinstance(valor, bool):
            return float(valor), clave
    return 0.0, ""


def _unidad(clave: str) -> str:
    if "cop" in clave or clave == "costo":
        return "COP"
    if "minuto" in clave:
        return "min"
    if "kg" in clave:
        return "kg"
    if "unidad" in clave:
        return "u"
    if clave:
        return "veces"
    return ""


def _num(valor: object) -> str:
    """Miles con punto, decimales con coma. Como se lee un reporte en planta."""
    try:
        v = float(valor)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return str(valor)
    if abs(v - round(v)) < 1e-9:
        return f"{int(round(v)):,}".replace(",", ".")
    return f"{v:,.1f}".replace(",", "@").replace(".", ",").replace("@", ".")


CLAVES_NOMBRE = ("causa", "causa_nombre", "nombre", "causa_texto", "descripcion", "tipo_defecto")
CLAVES_CODIGO = ("codigo", "causa_codigo", "codigo_causa")
CLAVES_ESTACION = ("estacion", "estacion_nombre", "nombre_estacion")
CLAVES_LINEA = ("linea", "linea_nombre", "nombre_linea")


def _primero(fila: dict, claves: tuple[str, ...]) -> str:
    for clave in claves:
        valor = fila.get(clave)
        if isinstance(valor, str) and valor.strip():
            return valor.strip()
    return ""


def _describir(fila: dict) -> str:
    """Etiqueta legible de una fila: codigo, causa, estacion y linea."""
    partes = [p for p in (_primero(fila, CLAVES_CODIGO), _primero(fila, CLAVES_NOMBRE)) if p]
    etiqueta = " ".join(partes)
    ubicacion = " · ".join(p for p in (_primero(fila, CLAVES_ESTACION), _primero(fila, CLAVES_LINEA)) if p)
    if not etiqueta:
        # Las filas de produccion no traen causa: ahi la ubicacion ES la etiqueta.
        return ubicacion or "(fila sin identificar)"
    return f"{etiqueta} — {ubicacion}" if ubicacion else etiqueta


def _resumen_numerico(fila: dict, maximo: int = 3) -> str:
    """Primeros campos numericos de la fila, crudos.

    Solo se usa en el camino degradado, cuando la fila no trae ninguna de las
    claves de impacto conocidas. Un `unidades_plan=480 · unidades_producidas=431`
    sin formato es feo, pero es informacion; una vineta sin una sola cifra no.
    """
    piezas = []
    for clave, valor in fila.items():
        if len(piezas) >= maximo:
            break
        if clave == "id" or clave.endswith("_id"):
            continue
        if isinstance(valor, (int, float)) and not isinstance(valor, bool):
            piezas.append(f"{clave}={_num(valor)}")
    return " · ".join(piezas)


def _compactar(obj: object, limite: int = MAX_CHARS_TEXTO) -> object:
    """Poda un resultado para que quepa en la ventana de contexto.

    Trunca textos largos y tira vectores de embedding: un solo vector de bge-m3
    son 1024 flotantes que no aportan nada al redactor y se comen la ventana.
    """
    if isinstance(obj, dict):
        limpio = {}
        for clave, valor in obj.items():
            if clave in ("embedding", "embeddings", "vector", "vectores", "texto_crudo"):
                continue
            limpio[clave] = _compactar(valor, limite)
        return limpio
    if isinstance(obj, list):
        numeros = [v for v in obj if isinstance(v, (int, float)) and not isinstance(v, bool)]
        if len(obj) > 16 and len(numeros) == len(obj):
            return f"<{len(obj)} numeros omitidos>"
        return [_compactar(v, limite) for v in obj]
    if isinstance(obj, str) and len(obj) > limite:
        return obj[:limite].rstrip() + "..."
    return obj


def _recortar(resultado: dict, n: int = MAX_FILAS_EN_HECHOS) -> dict:
    """Deja como maximo n filas, conservando `cobertura` y `motivo` intactos."""
    if not _disponible(resultado):
        return _compactar(resultado)  # type: ignore[return-value]
    copia = dict(resultado)
    datos = copia.get("datos")
    if isinstance(datos, list):
        copia["datos"] = datos[:n]
        if len(datos) > n:
            copia["filas_omitidas"] = len(datos) - n
    elif isinstance(datos, dict):
        nuevo = dict(datos)
        for clave in CLAVES_LISTA:
            valor = nuevo.get(clave)
            if isinstance(valor, list):
                nuevo[clave] = valor[:n]
                if len(valor) > n:
                    nuevo[f"{clave}_omitidas"] = len(valor) - n
                break
        copia["datos"] = nuevo
    return _compactar(copia)  # type: ignore[return-value]


# --- Cobertura ----------------------------------------------------------------

def _cobertura_consolidada(resultados: dict[str, dict]) -> dict:
    """Una sola lectura de cobertura a partir de la de cada herramienta.

    Cada herramienta reporta la suya y no tienen por que coincidir (una filtra
    por linea, otra no). La regla es conservadora a proposito: los conteos salen
    de `estado_datos`, que mira el rango completo, pero basta que UNA herramienta
    se declare parcial para que todo el resumen se declare parcial. Prefiero
    avisar de menos cobertura de la que hubo, y no al reves.
    """
    base = resultados.get("estado_datos", {})
    cob_base = base.get("cobertura") if isinstance(base.get("cobertura"), dict) else {}

    parcial = False
    encontrados: list[int] = []
    esperados: list[int] = []
    sin_clasificar: list[int] = []

    for resultado in resultados.values():
        cob = resultado.get("cobertura") if isinstance(resultado, dict) else None
        if not isinstance(cob, dict):
            continue
        parcial = parcial or bool(cob.get("parcial"))
        for clave, destino in (
            ("turnos_encontrados", encontrados),
            ("turnos_esperados", esperados),
            ("sin_clasificar", sin_clasificar),
        ):
            valor = cob.get(clave)
            if isinstance(valor, (int, float)) and not isinstance(valor, bool):
                destino.append(int(valor))

    def _elegir(clave: str, candidatos: list[int], agregador) -> int | None:
        valor = cob_base.get(clave)
        if isinstance(valor, (int, float)) and not isinstance(valor, bool):
            return int(valor)
        return agregador(candidatos) if candidatos else None

    return {
        "turnos_encontrados": _elegir("turnos_encontrados", encontrados, max),
        "turnos_esperados": _elegir("turnos_esperados", esperados, max),
        "sin_clasificar": _elegir("sin_clasificar", sin_clasificar, max),
        "parcial": parcial,
    }


def _linea_cobertura(cob: dict) -> str:
    """Frase de estado de cobertura. Vacia si la cobertura esta completa.

    Se calcula aqui y se le entrega hecha al modelo. Si se la dejara redactar a
    el, tendria que comparar 12 contra 15 —o sea, calcular— y esa es justo la
    operacion que este diseno le prohibe.
    """
    encontrados = cob.get("turnos_encontrados")
    esperados = cob.get("turnos_esperados")
    sin_clasificar = cob.get("sin_clasificar") or 0

    # Turnos que faltan y filas sin clasificar son dos huecos distintos y se
    # nombran distinto: en el primero faltan datos, en el segundo estan cargados
    # pero no se sabe a que causa van. Confundirlos hace que el supervisor busque
    # el reporte que si esta cargado.
    piezas: list[str] = []
    if cob.get("parcial") and isinstance(encontrados, int) and isinstance(esperados, int):
        piezas.append(f"{_num(encontrados)} de {_num(esperados)} turnos cargados")
    elif cob.get("parcial"):
        piezas.append("faltan turnos por cargar en el rango")
    if isinstance(sin_clasificar, int) and sin_clasificar > 0:
        piezas.append(f"{_num(sin_clasificar)} registros sin causa clasificada")

    if not piezas:
        return ""
    if cob.get("parcial"):
        return (
            "> Cobertura parcial: " + " y ".join(piezas)
            + ". Las cifras de abajo son sobre eso, no sobre el rango completo."
        )
    return (
        "> Cobertura completa en turnos, pero " + " y ".join(piezas)
        + ": esos registros no entran en el desglose por causa."
    )


# --- Titulo y markdown deterministico ----------------------------------------

def _titulo(desde: str, hasta: str, linea: str | None) -> str:
    return f"Resumen de producción — {desde} a {hasta} · {linea or 'todas las líneas'}"


def _bloque_filas(resultado: dict, encabezado: str, vacio: str) -> list[str]:
    """Render generico de una herramienta como vinetas con su impacto."""
    lineas = [f"**{encabezado}**", ""]
    if not _disponible(resultado):
        motivo = resultado.get("motivo") if isinstance(resultado, dict) else None
        lineas.append(f"- No disponible: {motivo or 'sin motivo declarado'}")
        lineas.append("")
        return lineas
    filas = _filas(resultado)
    if not filas:
        lineas.append(f"- {vacio}")
        lineas.append("")
        return lineas
    for fila in filas[:TOP_PARADAS]:
        valor, clave = _impacto(fila)
        cifra = f"{_num(valor)} {_unidad(clave)}".strip() if clave else _resumen_numerico(fila)
        lineas.append(f"- {_describir(fila)}" + (f" — {cifra}" if cifra else ""))
    lineas.append("")
    return lineas


# Los tres encabezados que el resumen tiene que traer siempre. El texto de la
# izquierda es el que se compara (sin tildes, en minuscula); el de la derecha el
# que escribe Python cuando le toca reponer la seccion.
SECCIONES = (
    ("lo que paso", "## Lo que pasó"),
    ("lo que se esta repitiendo", "## Lo que se está repitiendo"),
    ("3 acciones", "## 3 acciones, ordenadas por impacto"),
)


def _sin_tildes(texto: str) -> str:
    import unicodedata

    return "".join(
        c for c in unicodedata.normalize("NFD", texto.lower())
        if unicodedata.category(c) != "Mn"
    )


def _seccion_lo_que_paso(resultados: dict[str, dict]) -> list[str]:
    partes = _bloque_filas(resultados.get("produccion_vs_plan", {}), "Producción vs plan", "Sin registros de producción.")
    partes += _bloque_filas(resultados.get("pareto_paradas", {}), f"Top {TOP_PARADAS} de paradas", "Cero paradas registradas.")
    partes += _bloque_filas(resultados.get("scrap_por_linea", {}), "Scrap", "Cero scrap registrado.")
    if _disponible(resultados.get("impacto_costo", {})):
        partes += _bloque_filas(resultados["impacto_costo"], "Impacto en costo", "Sin costo calculable.")
    return partes


def _seccion_repeticion(resultados: dict[str, dict]) -> list[str]:
    return _bloque_filas(
        resultados.get("causas_recurrentes", {}),
        f"Causas con {MIN_REPETICIONES} o más apariciones",
        "Ninguna causa superó el umbral de repeticiones.",
    )


def _seccion_acciones(motivo: str, recurrentes: list[dict]) -> list[str]:
    """Lo que Python puede poner donde iban las acciones: el ranking, no consejos.

    Priorizar es leer una lista ordenada; recomendar es otra cosa y no le
    corresponde al codigo. Se entrega el orden por impacto y se dice de frente
    que las acciones no se redactaron.
    """
    partes = [f"No se redactaron acciones: {motivo}.", ""]
    if recurrentes:
        partes += ["Lo que más pesó en el período, en orden, para decidir por dónde empezar:", ""]
        for orden, fila in enumerate(recurrentes[:3], start=1):
            valor, clave = _impacto(fila)
            cifra = f" — {_num(valor)} {_unidad(clave)}".rstrip() if clave else ""
            partes.append(f"{orden}. {_describir(fila)}{cifra}")
        partes.append("")
    return partes


def _markdown_deterministico(titulo: str, cobertura: str, resultados: dict[str, dict],
                             recurrentes: list[dict], motivo_fallo: str) -> str:
    """Resumen armado en Python cuando el redactor no esta.

    Mantiene los tres encabezados que espera quien lo lee. Los numeros salieron
    de las herramientas y siguen siendo validos: lo que falto fue la prosa, no
    el dato. Por eso se entrega igual, en vez de un mensaje de error.
    """
    partes = [f"# {titulo}", ""]
    if cobertura:
        partes += [cobertura, ""]
    partes += [SECCIONES[0][1], ""] + _seccion_lo_que_paso(resultados)
    partes += [SECCIONES[1][1], ""] + _seccion_repeticion(resultados)
    partes += [SECCIONES[2][1], ""] + _seccion_acciones(motivo_fallo, recurrentes)
    return "\n".join(partes).rstrip() + "\n"


def _frase(texto: str) -> str:
    """Mayuscula inicial y punto final. Los motivos vienen en minuscula y sueltos
    ('no hay turnos cargados'), y aqui se leen como una frase de un informe."""
    texto = (texto or "").strip()
    if not texto:
        return ""
    if texto[0].islower():
        texto = texto[0].upper() + texto[1:]
    return texto if texto[-1] in ".!?" else texto + "."


def _markdown_sin_datos(titulo: str, motivo: str, sugerencia: str) -> str:
    return (
        f"# {titulo}\n\n"
        f"**No hay datos para armar este resumen.** {_frase(motivo)}\n\n"
        f"{_frase(sugerencia)}\n"
    )


def _sin_resumen(titulo: str, motivo: str, sugerencia: str, evidencia: dict) -> dict:
    return {
        "markdown": _markdown_sin_datos(titulo, motivo, sugerencia),
        "evidencia": evidencia,
        "validacion": dict(DICTAMEN_SIN_MODELO),
    }


# --- El unico llamado al modelo ----------------------------------------------

INSTRUCCION_REDACCION = """\
Redacta el resumen ejecutivo de produccion a partir de los HECHOS de abajo.

Los hechos YA ESTAN CALCULADOS. Tu trabajo es redactarlos, no recalcularlos.

PROHIBIDO HACER CUENTAS. Copia las cifras del JSON tal como estan:
- No conviertas unidades. Si el JSON dice 214 minutos, escribes "214 minutos".
  NO escribes "3,5 horas" ni "9 horas".
- No restes. Si el JSON trae plan 480 y producidas 431, escribes esas dos
  cifras. NO escribes "faltaron 49": esa resta no esta en el JSON.
- No sumes totales entre filas, no promedies, no saques porcentajes, no
  proyectes.
- Si una cifra que te haria falta no esta en el JSON, no la estimes: di que no
  esta. Un numero mal calculado hace que alguien pare una linea que no debia.

ESTRUCTURA EXACTA: los tres encabezados de abajo, con ese texto, en ese orden,
todos presentes y ninguno mas. No agregues secciones propias ni preambulo.

# <copia aqui, literal, el valor de titulo_exacto>
<si linea_de_cobertura NO viene vacia, copiala literal en esta linea; si viene
vacia, no escribas nada aqui y pasa directo al primer encabezado>

## Lo que paso
## Lo que se esta repitiendo
## 3 acciones, ordenadas por impacto

QUE VA EN CADA SECCION

- "Lo que paso": produccion contra plan, las paradas que mas tiempo costaron y
  el scrap. Empieza por lo mas grande. Si un bloque trae disponible=false, dilo
  en una linea con lo que habria que cargar para tenerlo, y sigue.
- "Lo que se esta repitiendo": las causas recurrentes, con cuantas veces y desde
  cuando. Si hay observaciones de planta sobre alguna, cita lo que escribieron y
  di de que turno y fecha salio. Recurrencia no es causa raiz: describe la
  repeticion, no diagnostiques la falla.
- "3 acciones": maximo tres, ordenadas por {criterio}. Cada una tiene que
  apuntar a una cifra que ya escribiste arriba y ser algo que alguien pueda
  hacer manana en una estacion concreta. "Mejorar el proceso" no es una accion.

Frases cortas, espanol de planta, sin adjetivos de relleno. Devuelve solo el
markdown, sin ```."""


def _redactar(hechos: dict, criterio: str) -> tuple[str, str]:
    """Un solo llamado al LLM. Devuelve (markdown, motivo_de_fallo)."""
    cfg = llm.config_openai()
    extra: dict = {}
    if cfg.get("proveedor") == "ollama":
        # El bloque de hechos + COPILOTO no cabe en los 2048 por defecto de Ollama.
        ctx = max(int(cfg.get("num_ctx") or 0), NUM_CTX_MINIMO)
        extra = {"extra_body": {"options": {"num_ctx": ctx}}}

    contenido = (
        INSTRUCCION_REDACCION.replace("{criterio}", criterio)
        + "\n\nHECHOS (JSON, ya calculados por las herramientas):\n"
        + json.dumps(hechos, ensure_ascii=False, indent=1, default=str)
    )
    try:
        from openai import OpenAI

        cliente = OpenAI(base_url=cfg["base_url"], api_key=cfg["api_key"], timeout=TIMEOUT_LLM)
        respuesta = cliente.chat.completions.create(
            model=cfg["modelo"],
            messages=[
                {"role": "system", "content": prompts.COPILOTO},
                {"role": "user", "content": contenido},
            ],
            temperature=TEMPERATURA,
            **extra,
        )
        texto = (respuesta.choices[0].message.content or "").strip()
    except Exception as e:  # noqa: BLE001
        return "", f"el modelo no respondió ({type(e).__name__}: {e})"

    if not texto:
        return "", "el modelo devolvió una respuesta vacía"
    return _limpiar(texto), ""


def _limpiar(texto: str) -> str:
    """Quita las cercas de codigo con las que algunos modelos envuelven todo."""
    lineas = texto.strip().splitlines()
    if lineas and lineas[0].lstrip().startswith("```"):
        lineas = lineas[1:]
    if lineas and lineas[-1].strip().startswith("```"):
        lineas = lineas[:-1]
    return "\n".join(lineas).strip()


def _forzar_titulo(markdown: str, titulo: str) -> str:
    """El H1 lo pone Python, no el modelo.

    El titulo carga el rango de fechas y la linea: es el unico lugar donde el
    lector confirma de que trata lo que esta leyendo. Un modelo pequeno tiende a
    reescribirlo ("Resumen semanal de la planta") y ahi se pierde el alcance. Se
    reemplaza el H1 si lo hay, se antepone si no.
    """
    lineas = markdown.splitlines()
    for i, linea in enumerate(lineas):
        if linea.lstrip().startswith("# "):
            lineas[i] = f"# {titulo}"
            return "\n".join(lineas).strip() + "\n"
        if linea.strip():
            break
    return f"# {titulo}\n\n{markdown.strip()}\n"


def _forzar_cobertura(markdown: str, cobertura: str) -> str:
    """Mete la linea de cobertura debajo del titulo si el modelo la ignoro.

    Es el aviso que convierte "hubo 37 unidades de scrap" en "hubo 37 sobre 12
    de 15 turnos". Un modelo pequeno la trata como decoracion y la borra; el
    lector, en cambio, decide distinto con ella y sin ella. La pone Python.
    """
    if not cobertura:
        return markdown
    # La frase completa es larga y el modelo suele parafrasearla. Se busca por el
    # arranque ("Cobertura parcial") para no duplicarla cuando si la copio.
    marca = _sin_tildes(cobertura.lstrip("> ").split(":")[0])
    if marca and marca in _sin_tildes(markdown):
        return markdown
    lineas = markdown.splitlines()
    for i, linea in enumerate(lineas):
        if linea.lstrip().startswith("# "):
            lineas.insert(i + 1, "")
            lineas.insert(i + 2, cobertura)
            return "\n".join(lineas).strip() + "\n"
    return cobertura + "\n\n" + markdown


def _secciones_faltantes(markdown: str) -> list[str]:
    plano = _sin_tildes(markdown)
    return [titulo for clave, titulo in SECCIONES if clave not in plano]


def _completar_secciones(markdown: str, resultados: dict[str, dict],
                         recurrentes: list[dict]) -> tuple[str, list[str]]:
    """Repone las secciones que el modelo no escribio, con datos, no con prosa.

    Un 7B en CPU a veces cambia los encabezados por otros que le parecen mejores
    y se come la seccion de acciones. Descartar toda la redaccion por eso seria
    caro —son minutos de CPU— y quedarse callado seria peor: el lector no tiene
    como saber que falta un pedazo. Se completa con el mismo material que arma el
    resumen deterministico y se devuelve la lista de lo que hubo que reponer,
    para que quede en la evidencia.
    """
    faltantes = _secciones_faltantes(markdown)
    if not faltantes:
        return markdown, []

    partes = [markdown.rstrip(), ""]
    constructores = {
        SECCIONES[0][1]: lambda: _seccion_lo_que_paso(resultados),
        SECCIONES[1][1]: lambda: _seccion_repeticion(resultados),
        SECCIONES[2][1]: lambda: _seccion_acciones("el redactor no produjo esta sección", recurrentes),
    }
    for titulo in faltantes:
        partes += [titulo, ""] + constructores[titulo]()
    return "\n".join(partes).rstrip() + "\n", faltantes


def _marcar(markdown: str, dictamen: dict) -> str:
    """Un resumen que el auditor no pudo respaldar se marca; no se oculta."""
    if dictamen.get("verificado") and dictamen.get("fundamentada"):
        return markdown
    sin_respaldo = dictamen.get("afirmaciones_sin_respaldo") or []
    if sin_respaldo:
        citas = "; ".join(f'"{c}"' for c in sin_respaldo[:3])
        detalle = f"{citas}."
    else:
        detalle = (dictamen.get("explicacion") or "no se pudo completar la auditoría.").rstrip(".") + "."
    return markdown.rstrip() + AVISO_NO_VALIDADO.format(detalle=detalle)


# --- Orquestacion -------------------------------------------------------------

def _fecha(valor: str, etiqueta: str) -> date:
    try:
        return date.fromisoformat(str(valor).strip())
    except ValueError:
        raise ValueError(f"la fecha '{valor}' de `{etiqueta}` no es un YYYY-MM-DD valido") from None


def _consulta_rag(fila: dict) -> str:
    """Texto de busqueda para una causa recurrente: causa + estacion + linea."""
    partes = [
        _primero(fila, CLAVES_NOMBRE),
        _primero(fila, CLAVES_ESTACION),
        _primero(fila, CLAVES_LINEA),
    ]
    consulta = " ".join(p for p in partes if p).strip()
    return consulta or _primero(fila, CLAVES_CODIGO)


def resumen_ejecutivo(desde: str, hasta: str, linea: str | None = None) -> dict:
    """Consolida un rango de turnos en un resumen ejecutivo redactado.

    Devuelve {"markdown", "evidencia", "validacion"}. `evidencia` trae la salida
    cruda de cada herramienta —de ahi sale cada cifra del texto— y `validacion`
    el dictamen del auditor sobre el markdown final.
    """
    titulo = _titulo(desde, hasta, linea)
    evidencia: dict = {
        "periodo": {"desde": desde, "hasta": hasta},
        "linea": linea,
        "herramientas": {},
        "observaciones": [],
    }

    # 0. Fechas. Un rango invertido no es "sin datos": es una llamada mal hecha,
    #    y conviene decirlo distinto para que se corrija arriba.
    try:
        d_desde = _fecha(desde, "desde")
        d_hasta = _fecha(hasta, "hasta")
    except ValueError as e:
        return _sin_resumen(titulo, str(e), "Usa el formato YYYY-MM-DD en ambas fechas.", evidencia)
    if d_hasta < d_desde:
        return _sin_resumen(
            titulo,
            f"El rango está invertido: `desde` ({desde}) es posterior a `hasta` ({hasta}).",
            "Invierte los dos parámetros y vuelve a pedirlo.",
            evidencia,
        )

    resultados: dict[str, dict] = {}

    # 1. Estado de los datos. Si no hay turnos cargados se corta aqui: un resumen
    #    ejecutivo redactado sobre la nada es exactamente el error que este
    #    proyecto existe para no cometer.
    estado = _invocar("estado_datos", herramientas.estado_datos, desde=desde, hasta=hasta)
    resultados["estado_datos"] = estado
    evidencia["herramientas"]["estado_datos"] = estado
    if not _disponible(estado):
        return _sin_resumen(
            titulo,
            estado.get("motivo") or "no hay turnos cargados en ese rango.",
            "Carga los reportes de turno del rango en la pestaña **Cargar** y vuelve a pedir el resumen.",
            evidencia,
        )

    # 2-4. Las tres cifras duras del turno. Orden fijo: produccion, paradas, scrap.
    resultados["produccion_vs_plan"] = _invocar(
        "produccion_vs_plan", herramientas.produccion_vs_plan,
        linea=linea, desde=desde, hasta=hasta,
    )
    resultados["pareto_paradas"] = _invocar(
        "pareto_paradas", herramientas.pareto_paradas,
        linea=linea, desde=desde, hasta=hasta, top=TOP_PARADAS,
    )
    resultados["scrap_por_linea"] = _invocar(
        "scrap_por_linea", herramientas.scrap_por_linea,
        linea=linea, desde=desde, hasta=hasta,
    )

    # 5. Recurrencia. `causas_recurrentes` razona en dias hacia atras, no en un
    #    rango cerrado, asi que la ventana se estira hasta hoy cuando el reporte
    #    es de un rango pasado: pedir 6 dias para un rango que termino hace un mes
    #    devolveria una ventana que no lo toca. Se declara cuantos dias se usaron
    #    y si eso excede el rango, para que el texto no de a entender que la
    #    recurrencia se midio exactamente sobre el periodo del titulo.
    dias_rango = (d_hasta - d_desde).days + 1
    dias_ventana = max(dias_rango, (date.today() - d_desde).days + 1)
    resultados["causas_recurrentes"] = _invocar(
        "causas_recurrentes", herramientas.causas_recurrentes,
        dias=dias_ventana, min_repeticiones=MIN_REPETICIONES, linea=linea,
    )

    # 6. Costo. Puede no estar: la tabla `costos` arranca vacia y nadie inventa
    #    tarifas. Su ausencia cambia el criterio con el que se ordenan las acciones.
    resultados["impacto_costo"] = _invocar(
        "impacto_costo", herramientas.impacto_costo,
        desde=desde, hasta=hasta, linea=linea,
    )

    for nombre, resultado in resultados.items():
        evidencia["herramientas"][nombre] = resultado

    # Si ninguna herramienta numerica trajo algo, hay turnos en el rango pero no
    # para este corte (tipico al filtrar por una linea que no reporto).
    numericas = ("produccion_vs_plan", "pareto_paradas", "scrap_por_linea", "causas_recurrentes")
    if not any(_disponible(resultados[n]) for n in numericas):
        motivos = {resultados[n].get("motivo") for n in numericas if resultados[n].get("motivo")}
        return _sin_resumen(
            titulo,
            "Hay turnos en el rango, pero ninguna herramienta pudo calcular sobre este corte: "
            + "; ".join(sorted(str(m) for m in motivos if m)),
            "Revisa si la línea seleccionada reportó en esas fechas o amplía el rango.",
            evidencia,
        )

    # 7. Texto libre sobre las causas que mas pesan. El numero dice cuanto duele;
    #    la observacion del supervisor dice de que se trata. Solo las 3 primeras:
    #    cada busqueda son k pasajes que compiten por la ventana de contexto.
    recurrentes = sorted(_filas(resultados["causas_recurrentes"]), key=lambda f: -_impacto(f)[0])
    observaciones: list[dict] = []
    for fila in recurrentes[:TOP_CAUSAS_RAG]:
        consulta = _consulta_rag(fila)
        if not consulta:
            continue
        hallazgo = _invocar(
            "buscar_observaciones", rag.buscar_observaciones,
            consulta=consulta, linea=linea, desde=desde, hasta=hasta, k=K_OBSERVACIONES,
        )
        observaciones.append({
            "causa": _describir(fila),
            "consulta": consulta,
            "resultado": hallazgo,
        })
    evidencia["observaciones"] = observaciones

    # --- Bloque de hechos: todo calculado, nada por calcular ------------------
    cobertura = _cobertura_consolidada(resultados)
    linea_cobertura = _linea_cobertura(cobertura)
    hay_costo = _disponible(resultados["impacto_costo"])
    criterio = "costo en COP" if hay_costo else "minutos perdidos y unidades de scrap (no hay tarifas cargadas, dilo)"

    hechos = {
        "titulo_exacto": titulo,
        "linea_de_cobertura": linea_cobertura,
        "periodo": {"desde": desde, "hasta": hasta, "dias": dias_rango},
        "linea": linea or "todas las lineas",
        "criterio_para_ordenar_acciones": criterio,
        "cobertura": cobertura,
        "produccion_vs_plan": _recortar(resultados["produccion_vs_plan"]),
        "pareto_paradas": _recortar(resultados["pareto_paradas"], TOP_PARADAS),
        "scrap": _recortar(resultados["scrap_por_linea"]),
        "causas_recurrentes": {
            "ventana_dias": dias_ventana,
            "min_repeticiones": MIN_REPETICIONES,
            "la_ventana_excede_el_rango": dias_ventana > dias_rango,
            "resultado": _recortar(resultados["causas_recurrentes"]),
        },
        "impacto_costo": _recortar(resultados["impacto_costo"]),
        "observaciones_de_planta": _compactar(observaciones),
    }
    evidencia["hechos_entregados_al_modelo"] = hechos

    # --- Redaccion ------------------------------------------------------------
    markdown, fallo = _redactar(hechos, criterio)
    evidencia["redaccion"] = {
        "modelo": llm.config_openai().get("modelo"),
        "proveedor": llm.config_openai().get("proveedor"),
        "fallo": fallo or None,
    }
    if fallo:
        return {
            "markdown": _markdown_deterministico(titulo, linea_cobertura, resultados, recurrentes, fallo),
            "evidencia": evidencia,
            "validacion": dict(DICTAMEN_SIN_MODELO),
        }

    markdown = _forzar_titulo(markdown, titulo)

    # Cuantas de las tres secciones pedidas trajo. Es el termometro barato de si
    # el modelo hizo el trabajo que se le pidio o escribio otra cosa.
    faltantes = _secciones_faltantes(markdown)
    if len(faltantes) >= 2:
        # Se salto la estructura entera: lo que devolvio es un texto libre suyo,
        # y un modelo que ignora el formato tambien suele ignorar la regla de no
        # calcular. Reponerle las secciones dejaria los datos buenos pegados
        # debajo de su prosa —con las cifras que se invento por el camino— en un
        # mismo documento. Se descarta la redaccion completa: el resumen
        # deterministico dice menos, pero no dice nada falso.
        motivo = f"el modelo no respetó la estructura pedida (le faltaron {len(faltantes)} de 3 secciones)"
        evidencia["redaccion"]["fallo"] = motivo
        evidencia["redaccion"]["descartada"] = True
        return {
            "markdown": _markdown_deterministico(titulo, linea_cobertura, resultados, recurrentes, motivo),
            "evidencia": evidencia,
            "validacion": dict(DICTAMEN_SIN_MODELO),
        }

    # Correcciones deterministas sobre lo que escribio el modelo. Ninguna agrega
    # una cifra que el no tuviera: reponen el aviso de cobertura y la seccion que
    # se salto, desde el mismo bloque de hechos. Es mas barato que reintentar la
    # redaccion completa en CPU.
    markdown = _forzar_cobertura(markdown, linea_cobertura)
    markdown, repuestas = _completar_secciones(markdown, resultados, recurrentes)
    evidencia["redaccion"]["secciones_repuestas_por_python"] = repuestas

    # --- Auditoria ------------------------------------------------------------
    # Se audita contra el mismo bloque que vio el modelo, no contra la evidencia
    # completa: lo que hay que verificar es si escribio algo que no estaba en su
    # insumo. Darle al auditor mas contexto del que tuvo el redactor haria pasar
    # cifras que el redactor no pudo haber leido.
    dictamen = agente.validar(markdown, json.dumps(hechos, ensure_ascii=False, default=str))
    return {
        "markdown": _marcar(markdown, dictamen),
        "evidencia": evidencia,
        "validacion": dictamen,
    }


# --- CLI ----------------------------------------------------------------------

USO = "uso: python -m produccion.reporte <desde YYYY-MM-DD> <hasta YYYY-MM-DD> [linea]"


def _sello(validacion: dict) -> str:
    if not validacion.get("verificado"):
        return f"[auditor] no pudo validar — {validacion.get('explicacion', '')}"
    if validacion.get("fundamentada"):
        return f"[auditor] fundamentado — {validacion.get('explicacion') or 'sin hallazgos'}"
    sin_respaldo = validacion.get("afirmaciones_sin_respaldo") or []
    return f"[auditor] {len(sin_respaldo)} afirmacion(es) sin respaldo — {validacion.get('explicacion', '')}"


def main(argv: list[str]) -> int:
    # La consola de Windows es cp1252 y el markdown lleva tildes y guiones largos:
    # sin esto el resumen revienta al imprimirse, no al generarse.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass

    if len(argv) < 3:
        print(USO)
        return 2

    desde, hasta = argv[1], argv[2]
    linea = argv[3] if len(argv) > 3 else None

    resultado = resumen_ejecutivo(desde, hasta, linea)
    print(resultado["markdown"])
    print()
    print("-" * 70)
    print(_sello(resultado["validacion"]))

    faltantes = [
        f"{nombre}: {r.get('motivo')}"
        for nombre, r in resultado["evidencia"].get("herramientas", {}).items()
        if not _disponible(r)
    ]
    if faltantes:
        print("[sin datos] " + " | ".join(faltantes))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
