"""
Gold set de extraccion: lo que la extraccion DEBE producir para cada ejemplo.

Los tres reportes de `ejemplos/` tienen trampas sembradas a proposito. Este
archivo es la respuesta correcta a cada una, escrita a mano leyendo el texto
fuente. Si alguien cambia un ejemplo, cambia esto primero.

## Por que un gold set y no un "se ve bien"

La extraccion falla en silencio. Un scrap de 12 unidades leido como 2 no rompe
nada: entra a la DB, suma mal, y el resumen ejecutivo del viernes dice una cifra
que nadie puede rastrear. Las cuatro formas en que falla, y que este set aisla:

  NUMERO EQUIVOCADO   el modelo lee 2 donde dice 12, o promedia dos paradas.
  CAUSA EQUIVOCADA    fuerza un codigo canonico que no corresponde, en vez de
                      dejarlo en null para la cola de revision.
  FALSO CERO          no hay dato y escribe 0. Es la peor: un cero se ve como
                      un hecho ("no hubo scrap") y un null se ve como lo que es
                      ("nadie lo reporto"). Metrica critica del tablero.
  FILA COLAPSADA      dos paradas con la misma causa en el mismo turno se
                      funden en una. Ahi muere la recurrencia, que es todo el
                      producto.

## Forma de una fila esperada

    {
      "tipo": "parada" | "scrap" | "calidad",
      "buscar_en": ["scrap", "calidad"],   # opcional; por defecto [tipo]
      "pistas": [...],        # substrings (sin tildes, minusculas) de causa_texto.
                              # Basta que aparezca UNA. Sirven para emparejar la
                              # fila extraida con la esperada antes de comparar
                              # numeros: si emparejaramos por numero, un numero
                              # mal leido pareceria una fila faltante.
      "pistas_todas": [...],  # variante: deben aparecer TODAS
      "estacion": "Remachado",     # nombre canonico de taxonomia.yaml
      "codigos": ["PAR-MEC-02"],   # aceptados. None dentro de la lista significa
                                   # "dejarlo sin clasificar tambien es correcto"
      "valores": {"minutos": 25},  # esperado no nulo y no cero
      "ceros":   ["minutos"],      # esperado exactamente 0 -> CERO REAL
      "nulos":   ["unidades","kg"],# esperado null -> si sale 0 es FALSO CERO
      "hora_inicio": "09:40",
      "confianza_max": 0.8,        # ambiguedad sembrada: la extraccion debe
                                   # marcarla bajando la confianza
    }

Los tres cubos (`valores`, `ceros`, `nulos`) estan separados a proposito: cada
uno alimenta una metrica distinta del tablero, y mezclarlos es justamente el
error que se quiere medir.
"""

from __future__ import annotations

from pathlib import Path

EJEMPLOS = Path(__file__).resolve().parent.parent / "ejemplos"

# Cuantos campos puede marcar verificar_literalidad sin que se considere que el
# guard tiene falsos positivos. Es 1 y no 0 por un caso legitimo: el panel que
# "no paro produccion" son 0 minutos DERIVADOS de una frase, no un "0" escrito
# en el reporte. El resto de cifras de los tres ejemplos si estan literales, asi
# que si el contador sube, el problema es del guard y no del extractor.
LITERALIDAD_ESPERADA_MAX = 1


CASOS = [
    # =========================================================================
    {
        "id": "ej01",
        "archivo": "turno_2026-07-20_L2_whatsapp.txt",
        "titulo": "WhatsApp del supervisor, texto libre",
        "trampas": [
            "fecha 'lunes 20' sin mes ni ano",
            "tres paradas de la misma maquina (recurrencia intra-turno)",
            "'20 min mas o menos' -> aproximado",
            "panel que se reinicio pero NO paro produccion -> 0 minutos reales",
        ],
        "turno": {
            # No se puede saber el mes ni el ano leyendo el texto: "lunes 20" y
            # nada mas. null es la respuesta honesta; 2026-07-20 se acepta
            # porque el pipeline puede tomarla del nombre del archivo. Cualquier
            # otra fecha es una invencion.
            "fecha_aceptadas": [None, "2026-07-20"],
            "fecha_ambigua": True,
            "turno": 2,
            "linea": "L2",
            "valores": {"unidades_plan": 480, "unidades_producidas": 431},
            # El texto no dice cuantos minutos duro el turno. Ponerle 480 seria
            # inventar un dato plausible, que es peor que dejarlo vacio.
            "nulos": ["minutos_turno"],
        },
        "observaciones_pistas": ["alimentador", "desalineado"],
        "filas": [
            {
                "tipo": "parada",
                "pistas": ["remachadora", "r-02", "trab"],
                "estacion": "Remachado",
                "codigos": ["PAR-MEC-02"],
                "valores": {"minutos": 25},
                "hora_inicio": "09:40",
                # "casi 25 min": aproximado, pero el numero esta escrito.
                "confianza_max": 0.9,
            },
            {
                "tipo": "parada",
                "pistas": ["remachadora", "r-02", "trab", "rapidita"],
                "estacion": "Remachado",
                "codigos": ["PAR-MEC-02"],
                "valores": {"minutos": 8},
            },
            {
                "tipo": "parada",
                "pistas": ["remachadora", "r-02", "trab", "mas o menos"],
                "estacion": "Remachado",
                "codigos": ["PAR-MEC-02"],
                "valores": {"minutos": 20},
                # "otros 20 min mas o menos": el supervisor mismo dice que es
                # una estimacion. Si la extraccion la trata igual que un 8 exacto
                # perdimos la unica senal de que ese numero es blando.
                "confianza_max": 0.8,
            },
            {
                "tipo": "parada",
                "pistas": ["material", "estiba", "gabinete"],
                # "esperando material del gabinete" no dice en que estacion se
                # paro: el gabinete pasa por varias. Dejarla sin estacion es lo
                # correcto.
                "estacion": None,
                # PAR-LOG-01 (espera de logistica) es el vecino tentador porque
                # tiene "no hay estiba" como alias. Se descarta: lo que paro la
                # linea fue no tener material, y la estiba es el motivo detras
                # del motivo. Registrar la causa de la causa rompe el pareto.
                "codigos": ["PAR-MAT-01"],
                "valores": {"minutos": 40},
            },
            {
                "tipo": "parada",
                "pistas": ["panel", "reinici", "tablero"],
                "estacion": "Panel de control",
                "codigos": ["PAR-ELE-03"],
                # CERO REAL: "se reinicio solo dos veces, no paro produccion".
                # El evento existe, el impacto en minutos es cero. Si sale null
                # perdimos un evento que la recurrencia necesita contar; si sale
                # 40 nos inventamos una parada que no ocurrio.
                "ceros": ["minutos"],
            },
            {
                "tipo": "scrap",
                "pistas": ["remaches mal puestos", "remache"],
                "estacion": "Remachado",
                "codigos": ["SCR-ENS-01"],
                "valores": {"unidades": 12},
                "nulos": ["kg"],
            },
            {
                "tipo": "scrap",
                "pistas": ["rayad", "gabinete"],
                # "en el traslado" apunta a manipulacion; "rayados" a lamina
                # rayada. Las dos lecturas son defendibles y ninguna dana el
                # pareto, asi que las dos pasan.
                "codigos": ["SCR-LAM-01", "SCR-MAN-01"],
                "valores": {"unidades": 3},
                "nulos": ["kg"],
            },
        ],
        "prohibidas": [],
    },

    # =========================================================================
    {
        "id": "ej02",
        "archivo": "turno_2026-07-22_L1_formato.txt",
        "titulo": "Formato impreso de planta, tabla fija",
        "trampas": [
            "dos paradas con la misma causa: no se pueden colapsar",
            "scrap con unidades Y kg en la misma fila",
            "'no enfria' no tiene codigo de scrap claro",
        ],
        "turno": {
            "fecha_aceptadas": ["2026-07-22"],
            "fecha_ambigua": False,
            "turno": 1,
            "linea": "L1",
            "supervisor_pistas": ["restrepo"],
            "valores": {
                "unidades_plan": 320,
                "unidades_producidas": 296,
                "minutos_turno": 480,
            },
        },
        "observaciones_pistas": ["burbuja", "poliol", "compresor"],
        "filas": [
            {
                "tipo": "parada",
                "pistas": ["ajuste temperatura", "temperatura de molde"],
                "estacion": "Inyeccion de poliuretano",
                "codigos": ["PAR-SET-02"],
                "valores": {"minutos": 35},
                "hora_inicio": "07:15",
            },
            {
                "tipo": "parada",
                "pistas": ["aire comprimido"],
                "estacion": "Carga de gas",
                "codigos": ["PAR-SER-01"],
                "valores": {"minutos": 18},
                "hora_inicio": "09:50",
            },
            {
                "tipo": "parada",
                "pistas": ["cambio de referencia"],
                "estacion": "Conformado de lamina",
                "codigos": ["PAR-SET-01"],
                "valores": {"minutos": 45},
                "hora_inicio": "13:20",
            },
            {
                # La misma causa que la primera fila, otra vez a las 14:40. Dos
                # filas, no una de 57 minutos: que el mismo ajuste se repita en
                # el mismo turno es EL dato (las observaciones lo confirman:
                # "ya se ajusto temperatura dos veces en el turno"). Sumarlas
                # borra la senal de recurrencia y deja un promedio inutil.
                "tipo": "parada",
                "pistas": ["ajuste temperatura", "temperatura de molde"],
                "estacion": "Inyeccion de poliuretano",
                "codigos": ["PAR-SET-02"],
                "valores": {"minutos": 22},
                "hora_inicio": "14:40",
            },
            {
                "tipo": "scrap",
                "pistas": ["burbuja"],
                "estacion": "Inyeccion de poliuretano",
                "codigos": ["SCR-PU-01"],
                "valores": {"unidades": 14, "kg": 58.0},
            },
            {
                "tipo": "scrap",
                "pistas": ["rayado"],
                "estacion": "Ensamble final",
                "codigos": ["SCR-LAM-01"],
                "valores": {"unidades": 5, "kg": 21.0},
            },
            {
                "tipo": "scrap",
                "pistas": ["no enfria"],
                "estacion": "Prueba funcional",
                # Ninguna causa de scrap dice "no enfria": el catalogo lo tiene
                # como defecto de calidad (CAL-TEM-01), pero el reporte lo cuenta
                # como scrap con unidades y kg. null es una respuesta correcta
                # aqui — la regla 4 del extractor es explicita: forzar un codigo
                # equivocado es peor que dejarlo para revision.
                "codigos": [None, "SCR-GAS-01", "SCR-FUG-01"],
                "valores": {"unidades": 2, "kg": 8.5},
            },
        ],
        "prohibidas": [],
    },

    # =========================================================================
    {
        "id": "ej03",
        "archivo": "turno_2026-07-23_L2_export.csv",
        "titulo": "Export de sistema, CSV con columna 'tipo'",
        "trampas": [
            "fila de scrap SIN cantidad -> null, nunca 0",
            "'Ruido raro' viene etiquetado SCRAP pero es calidad",
            "la fila NOTA es una observacion, no un hecho",
        ],
        "turno": {
            "fecha_aceptadas": ["2026-07-23"],
            "fecha_ambigua": False,
            "turno": 2,
            "linea": "L2",
            # "Plan 480 / Real 402" va en la descripcion, no en columnas propias:
            # hay que leerlo del texto y no de `unidades`, que ademas trae 402.
            "valores": {
                "unidades_plan": 480,
                "unidades_producidas": 402,
                "minutos_turno": 480,
            },
        },
        "observaciones_pistas": ["alimentador", "repuesto"],
        "filas": [
            {
                "tipo": "parada",
                "pistas": ["atascada", "destrabo", "r-02"],
                "estacion": "Remachado",
                "codigos": ["PAR-MEC-02"],
                "valores": {"minutos": 32},
            },
            {
                "tipo": "parada",
                "pistas": ["volvio a trabar", "trabar", "r-02"],
                "estacion": "Remachado",
                "codigos": ["PAR-MEC-02"],
                "valores": {"minutos": 41},
            },
            {
                "tipo": "parada",
                "pistas": ["tablero", "no responde", "reinicio"],
                "estacion": "Panel de control",
                "codigos": ["PAR-ELE-03"],
                "valores": {"minutos": 15},
            },
            {
                "tipo": "parada",
                "pistas": ["falto material", "canastilla"],
                "estacion": "Ensamble de tina",
                # Aqui si se acepta PAR-LOG-01: el texto nombra la canastilla
                # como el faltante concreto ("sin canastilla" es alias literal),
                # no como explicacion de por que falto el material.
                "codigos": ["PAR-MAT-01", "PAR-LOG-01"],
                "valores": {"minutos": 28},
            },
            {
                "tipo": "scrap",
                "pistas": ["remaches mal puestos", "atasco"],
                "estacion": "Remachado",
                "codigos": ["SCR-ENS-01"],
                "valores": {"unidades": 18},
                "nulos": ["kg"],
            },
            {
                "tipo": "scrap",
                "pistas": ["lamina golpeada", "golpead"],
                "estacion": "Conformado de gabinete",
                "codigos": ["SCR-LAM-01"],
                "valores": {"unidades": 4},
                "nulos": ["kg"],
            },
            {
                # La trampa doble del CSV. (1) La columna `tipo` dice SCRAP pero
                # un ruido en el ciclo de prueba no es material perdido: es un
                # defecto funcional (CAL-RUI-01). Obedecer la etiqueta de origen
                # infla el scrap con unidades que nadie boto. (2) Las columnas
                # unidades y kg vienen vacias: null, no 0.
                "tipo": "calidad",
                "buscar_en": ["calidad", "scrap"],
                "pistas": ["ruido"],
                # Sin `estacion` esperada a proposito: la tabla `calidad` del
                # esquema no tiene estacion_id. Exigirla seria castigar a la
                # extraccion por respetar el esquema.
                "codigos": ["CAL-RUI-01"],
                # Caer en la tabla `calidad` ya ES la clasificacion correcta:
                # ese esquema no tiene causa_id, tiene tipo_defecto.
                "equivale_bucket": "calidad",
                "nulos": ["unidades", "kg"],
            },
        ],
        "prohibidas": [
            {
                # La ultima fila del CSV es tipo NOTA: contexto para el RAG, no
                # un hecho con minutos. Si aparece como parada o scrap, el
                # extractor esta fabricando eventos a partir de comentarios.
                "pistas_todas": ["alimentador", "repuesto"],
                "motivo": "la fila NOTA es observacion, no una parada ni scrap",
            },
        ],
    },
]


# --- Recurrencia (se mide con los 3 turnos ya cargados en la DB) -------------
#
# Es la prueba de que la extraccion sirve para algo. La R-02 se traba 3 veces el
# lunes y 2 veces el jueves: ningun turno por si solo lo hace evidente, y el
# supervisor del jueves no leyo el reporte del lunes. Si el agrupado
# (causa_id, estacion_id) no junta esas 5, el producto no existe.
#
# Contra-caso igual de importante: el panel de control aparece 2 veces (una por
# turno). Con min_repeticiones=3 NO debe salir. Un detector de patrones que
# reporta todo no es un detector de patrones.
RECURRENCIA = {
    "linea": "L2",
    "min_repeticiones": 3,
    "codigo": "PAR-MEC-02",
    "estacion": "Remachado",
    "repeticiones_esperadas": 5,      # 3 del lunes + 2 del jueves
    "minutos_esperados": 126.0,       # 25 + 8 + 20 + 32 + 41
    "no_esperados": ["PAR-ELE-03"],   # solo 2 apariciones: bajo el umbral
}
