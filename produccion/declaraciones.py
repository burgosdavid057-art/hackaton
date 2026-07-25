"""
Declaraciones de herramientas del Copiloto, en el esquema de tools de OpenAI
(que es el que habla Ollama por su endpoint /v1).

Este archivo es prompt, no codigo. La descripcion de cada herramienta es lo
unico que tiene el modelo para elegir, y con un 7B local esa eleccion es el
punto mas fragil de todo el sistema. Por eso ninguna descripcion se limita a
decir que hace la funcion: dice CUANDO usarla y, sobre todo, contra que otra
herramienta se confunde. El cruce ("para el POR QUE no uses esta, usa aquella")
es lo que hace que un modelo pequeno acierte, mucho mas que la lista de
parametros.

Dos marcas que el modelo lee literalmente al principio de cada descripcion:

    [NUMEROS] - calcula en Python/SQL y devuelve cifras verificables.
    [TEXTO]   - busca y devuelve lo que alguien escribio, sin agregar nada.

Marcarlo asi ataca el error mas caro del agente: citar una observacion de un
supervisor como si fuera una medicion, o intentar sacar un total sumando lo que
leyo en un parrafo. Los numeros salen de las herramientas numericas; el texto
explica de que se trata el numero. Nunca al reves.

Este modulo no importa herramientas.py ni rag.py a proposito: es data pura, se
puede cargar aunque Chroma no este instalado y no arrastra la DB en tiempo de
import. El despacho por nombre vive en agente.py.
"""

from __future__ import annotations

# Formato neutral, igual que agent/declarations.py: cada parametro es
# {nombre: (tipo, descripcion)}. Tipos soportados:
#   "string" | "number" | "integer" | "array:string"
#
# `modulo` dice quien ejecuta la herramienta ("herramientas" -> aritmetica,
# "rag" -> busqueda semantica). agente.py lo usa para enrutar sin tener que
# hardcodear la lista de nombres en dos sitios.
#
# OJO con lo que NO esta aqui: `herramientas.ejecutar` es el despachador
# interno, no una capacidad. Declararsela al modelo lo invita a llamar
# ejecutar(nombre="scrap_por_linea", argumentos={...}) —una indireccion que un
# 7B arma mal la mitad de las veces— cuando puede llamar la herramienta
# directamente. Se queda fuera deliberadamente.
ESPECIFICACIONES = [
    # ── Numericas (produccion/herramientas.py) ───────────────────────────────
    {
        "nombre": "scrap_por_linea",
        "modulo": "herramientas",
        "descripcion": (
            "[NUMEROS] Cuanto scrap se genero: totales por linea, en unidades y "
            "kg, con el desglose por causa. Usala cuando pregunten cuanto scrap "
            "hubo, cuanto se perdio, cuantas piezas se fueron a la basura o que "
            "causa aporta mas desperdicio. "
            "NO la uses para saber POR QUE paso algo: para eso esta "
            "buscar_observaciones, que trae lo que el supervisor escribio a mano. "
            "Devuelve un bloque 'cobertura' con cuantos turnos entraron en el "
            "calculo: si viene parcial, dilo en la respuesta."
        ),
        "parametros": {
            "linea": ("string", "L1, L2, L3 o L4. Omitela para todas las lineas."),
            "desde": ("string", "fecha inicial YYYY-MM-DD, inclusive"),
            "hasta": ("string", "fecha final YYYY-MM-DD, inclusive"),
        },
        "requeridos": [],
    },
    {
        "nombre": "pareto_paradas",
        "modulo": "herramientas",
        "descripcion": (
            "[NUMEROS] Ranking de causas de parada por minutos perdidos, de mayor "
            "a menor, con el acumulado tipo Pareto. Usala para 'que nos esta "
            "parando mas', 'donde perdemos mas tiempo', 'top de paradas', 'cuanto "
            "tiempo muerto hubo'. "
            "Ordena por IMPACTO, no por frecuencia: una parada de 4 horas pesa mas "
            "que seis de 10 minutos. Si lo que preguntan es que se REPITE aunque "
            "sea corto, esa es causas_recurrentes, no esta. "
            "Da minutos y conteos, no explica el motivo: el relato lo pone "
            "buscar_observaciones."
        ),
        "parametros": {
            "linea": ("string", "L1, L2, L3 o L4. Omitela para todas las lineas."),
            "desde": ("string", "fecha inicial YYYY-MM-DD, inclusive"),
            "hasta": ("string", "fecha final YYYY-MM-DD, inclusive"),
            "top": ("integer", "cuantas causas devolver, por defecto 10"),
        },
        "requeridos": [],
    },
    {
        "nombre": "causas_recurrentes",
        "modulo": "herramientas",
        "descripcion": (
            "[NUMEROS] Detecta el mismo problema repitiendose en la misma estacion "
            "dentro de una ventana de dias: cuantas veces, desde cuando, primera y "
            "ultima aparicion, minutos y unidades acumuladas. Es la herramienta de "
            "'que se nos esta repitiendo', 'esto ya habia pasado?', 'hay algun "
            "patron'. "
            "Devuelve REPETICION, no causa raiz: puedes decir que la remachadora de "
            "L2 paro 6 veces por el mismo motivo registrado, no puedes decir por "
            "que se dana. Para saber de que se trata cada repeticion, encadena con "
            "buscar_observaciones; para saber si ya se resolvio antes, con "
            "casos_similares."
        ),
        "parametros": {
            "dias": ("integer", "tamano de la ventana hacia atras, por defecto 7"),
            "min_repeticiones": (
                "integer",
                "minimo de apariciones para considerarlo recurrente, por defecto 3",
            ),
            "linea": ("string", "L1, L2, L3 o L4. Omitela para todas las lineas."),
        },
        "requeridos": [],
    },
    {
        "nombre": "produccion_vs_plan",
        "modulo": "herramientas",
        "descripcion": (
            "[NUMEROS] Unidades planeadas contra producidas y el cumplimiento, por "
            "linea y turno. Usala para 'cumplimos el plan', 'cuanto produjimos', "
            "'cuanto nos falto', 'como vamos'. "
            "Esta herramienta dice CUANTO falto, no por que falto. Si preguntan la "
            "razon del incumplimiento, encadena: pareto_paradas para los minutos "
            "perdidos y scrap_por_linea para las piezas perdidas."
        ),
        "parametros": {
            "linea": ("string", "L1, L2, L3 o L4. Omitela para todas las lineas."),
            "desde": ("string", "fecha inicial YYYY-MM-DD, inclusive"),
            "hasta": ("string", "fecha final YYYY-MM-DD, inclusive"),
        },
        "requeridos": [],
    },
    {
        "nombre": "comparar_periodos",
        "modulo": "herramientas",
        "descripcion": (
            "[NUMEROS] Compara dos rangos de fechas (esta semana contra la "
            "anterior, este mes contra el pasado) en produccion, paradas y scrap, "
            "con la diferencia y la variacion ya calculadas. "
            "Usala SIEMPRE que la pregunta traiga un 'vs', 'mejoro', 'empeoro', "
            "'comparado con', 'subio o bajo'. Nunca llames dos veces otra "
            "herramienta y restes tu los resultados: tu no calculas, y una resta "
            "mal hecha sobre datos buenos es indistinguible de un dato inventado."
        ),
        "parametros": {
            "desde_a": ("string", "inicio del primer periodo, YYYY-MM-DD"),
            "hasta_a": ("string", "fin del primer periodo, YYYY-MM-DD"),
            "desde_b": ("string", "inicio del segundo periodo, YYYY-MM-DD"),
            "hasta_b": ("string", "fin del segundo periodo, YYYY-MM-DD"),
            "linea": ("string", "L1, L2, L3 o L4. Omitela para todas las lineas."),
        },
        "requeridos": ["desde_a", "hasta_a", "desde_b", "hasta_b"],
    },
    {
        "nombre": "impacto_costo",
        "modulo": "herramientas",
        "descripcion": (
            "[NUMEROS] Traduce minutos de parada y unidades de scrap a pesos, con "
            "las tarifas cargadas en la tabla de costos. Usala cuando pregunten "
            "cuanto costo, cuanta plata se perdio, o cuando haya que priorizar "
            "acciones por dinero. "
            "Si no hay tarifas cargadas devuelve disponible: false. En ese caso NO "
            "estimes un costo con tarifas de tu memoria: prioriza por minutos "
            "perdidos o unidades de scrap y di explicitamente que estas usando esa "
            "medida porque no hay costos cargados."
        ),
        "parametros": {
            "desde": ("string", "fecha inicial YYYY-MM-DD, inclusive"),
            "hasta": ("string", "fecha final YYYY-MM-DD, inclusive"),
            "linea": ("string", "L1, L2, L3 o L4. Omitela para todas las lineas."),
        },
        "requeridos": [],
    },
    {
        "nombre": "estado_datos",
        "modulo": "herramientas",
        "descripcion": (
            "[NUMEROS] Que hay cargado y que falta: turnos encontrados contra "
            "esperados, lineas y fechas sin reportar, y cuantas filas quedaron con "
            "la causa sin clasificar. "
            "Usala cuando pregunten 'tenemos todo?', 'que falta por cargar', 'de "
            "que dias hay datos'. Usala tambien ANTES de dar una cifra de un rango "
            "largo, y siempre que otra herramienta devuelva cobertura parcial y "
            "necesites explicar sobre que universo estas hablando. Es la "
            "herramienta que evita que una cifra incompleta se lea como completa."
        ),
        "parametros": {
            "desde": ("string", "fecha inicial YYYY-MM-DD, inclusive"),
            "hasta": ("string", "fecha final YYYY-MM-DD, inclusive"),
        },
        "requeridos": [],
    },

    # ── Busqueda semantica (produccion/rag.py) ───────────────────────────────
    {
        "nombre": "buscar_observaciones",
        "modulo": "rag",
        "descripcion": (
            "[TEXTO] Busca en las observaciones que escribieron los supervisores en "
            "sus reportes de turno. Devuelve parrafos literales con su fecha, turno "
            "y linea. NO devuelve totales ni promedios: de aqui no sale ninguna "
            "cifra agregada. "
            "Es la herramienta del POR QUE. Usala cuando pregunten que paso, que "
            "dijeron, si alguien reporto algo raro, o para ponerle relato a un "
            "numero que ya trajiste de scrap_por_linea, pareto_paradas o "
            "causas_recurrentes. "
            "Pasale siempre la linea y el rango de fechas si la pregunta los tiene: "
            "el filtro recorta antes de buscar, y sin el puedes traer un comentario "
            "cierto pero de la linea equivocada. Cuando cites, di de que fecha y "
            "turno salio."
        ),
        "parametros": {
            "consulta": ("string", "que buscar, en palabras de planta"),
            "linea": ("string", "L1, L2, L3 o L4. Omitela solo si la pregunta no la acota."),
            "desde": ("string", "fecha inicial YYYY-MM-DD, inclusive"),
            "hasta": ("string", "fecha final YYYY-MM-DD, inclusive"),
            "k": ("integer", "cuantos pasajes traer, por defecto 5"),
        },
        "requeridos": ["consulta"],
    },
    {
        "nombre": "buscar_procedimiento",
        "modulo": "rag",
        "descripcion": (
            "[TEXTO] Busca en los procedimientos, instructivos y fichas de "
            "mantenimiento cargados en la planta. Devuelve el texto del documento, "
            "no una interpretacion. "
            "Usala cuando pregunten como se hace algo, cual es el estandar, cada "
            "cuanto toca el mantenimiento de una maquina, que dice el instructivo o "
            "cual es el parametro correcto. Es la UNICA fuente valida para un "
            "'como se hace': no lo respondas de memoria ni por analogia con otra "
            "maquina. Si no hay procedimiento cargado, dilo."
        ),
        "parametros": {
            "consulta": ("string", "la duda o el procedimiento buscado"),
            "maquina": ("string", "maquina o estacion, si la pregunta la nombra"),
            "k": ("integer", "cuantos pasajes traer, por defecto 3"),
        },
        "requeridos": ["consulta"],
    },
    {
        "nombre": "casos_similares",
        "modulo": "rag",
        "descripcion": (
            "[TEXTO] Busca casos anteriores parecidos y como se resolvieron en su "
            "momento. Devuelve antecedentes textuales, no una receta. "
            "Usala DESPUES de haber identificado un problema (con "
            "causas_recurrentes o buscar_observaciones), cuando quieran saber si "
            "esto ya paso antes y que se hizo. "
            "Un caso viejo parecido es una pista de donde mirar, no un diagnostico: "
            "presentalo como antecedente y di de cuando es. Si lo que se hizo "
            "entonces no encaja con lo de ahora, dilo en vez de recomendarlo."
        ),
        "parametros": {
            "descripcion": ("string", "el problema actual, descrito como lo contaria un supervisor"),
            "k": ("integer", "cuantos casos traer, por defecto 3"),
        },
        "requeridos": ["descripcion"],
    },
]


# Nombres de las herramientas que ejecuta rag.py. agente.py enruta con esto en
# vez de repetir la lista: si alguien agrega una busqueda aqui, el despacho la
# toma sola.
NOMBRES_RAG = frozenset(
    e["nombre"] for e in ESPECIFICACIONES if e["modulo"] == "rag"
)

NOMBRES_NUMERICAS = frozenset(
    e["nombre"] for e in ESPECIFICACIONES if e["modulo"] == "herramientas"
)


# --- Esquema para Ollama / OpenAI --------------------------------------------

_TIPOS = {
    "string": "string",
    "number": "number",
    # Diferenciar integer de number no es cosmetico: sin esto los modelos
    # pequenos mandan dias=7.0 o top=10.0, y un float donde va un LIMIT o un
    # range() revienta la herramienta por TypeError.
    "integer": "integer",
    "array:string": "array",
}


def herramientas_openai() -> list[dict]:
    """Esquema de tools de OpenAI para las herramientas del Copiloto."""
    tools = []
    for esp in ESPECIFICACIONES:
        props = {}
        for nombre, (tipo, desc) in esp["parametros"].items():
            if tipo == "array:string":
                props[nombre] = {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": desc,
                }
            else:
                props[nombre] = {"type": _TIPOS[tipo], "description": desc}
        tools.append({
            "type": "function",
            "function": {
                "name": esp["nombre"],
                "description": esp["descripcion"],
                "parameters": {
                    "type": "object",
                    "properties": props,
                    "required": esp["requeridos"],
                },
            },
        })
    return tools


def nombres() -> list[str]:
    """Nombres declarados, en el orden en que los ve el modelo."""
    return [e["nombre"] for e in ESPECIFICACIONES]
