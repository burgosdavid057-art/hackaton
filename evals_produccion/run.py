"""
Corre el gold set de extraccion y mide donde falla el modelo local.

    python -m evals_produccion.run             # extrae, evalua y imprime tablero
    python -m evals_produccion.run --tablero   # solo imprime lo ya medido
    python -m evals_produccion.run --forzar    # ignora el cache y vuelve a extraer
    python -m evals_produccion.run --db RUTA   # usa otra DB de trabajo

Mismo espiritu que `evals/run.py`: no se afirma que el pipeline funciona, se
mide. La diferencia es el objeto medido. Alla se mide al agente respondiendo;
aca se mide el paso de antes — leer el reporte de turno — porque un agente
perfecto sobre datos mal extraidos es un generador de cifras equivocadas con
buena redaccion.

Las cinco metricas del tablero, y por que cada una:

  CAMPOS NUMERICOS     lo obvio: leyo 431 donde dice 431.
  CLASIFICACION CAUSA  mapeo a la taxonomia. Un codigo forzado ensucia el
                       pareto mas de lo que lo ensucia un null.
  FALSOS CEROS         nulls esperados que salieron 0. Es la metrica critica:
                       un 0 se lee como un hecho ("no hubo scrap") y viaja
                       hasta el resumen ejecutivo sin que nadie lo cuestione.
  LITERALIDAD MARCADA  cuantos campos marco `verificar_literalidad`. En estos
                       ejemplos casi todo numero esta escrito en el texto, asi
                       que un contador alto no acusa al extractor: acusa al
                       guard de tener falsos positivos.
  RECURRENCIA          con los 3 turnos cargados, `causas_recurrentes` tiene
                       que ver el patron de la R-02 en L2. Es la unica metrica
                       de fin a fin: si esta falla, el producto no existe.

Los resultados se guardan en evals_produccion/resultados.json a medida que
corren. Volver a correr reutiliza la extraccion cacheada (que es la parte lenta
y la que consume el modelo) y vuelve a evaluar contra el gold set, asi que
ajustar el dataset no obliga a re-extraer.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

from evals_produccion.dataset import (
    CASOS,
    EJEMPLOS,
    LITERALIDAD_ESPERADA_MAX,
    RECURRENCIA,
)

RESULTADOS = Path(__file__).resolve().parent / "resultados.json"

# DB propia del harness. Se recrea en cada corrida completa a proposito: la
# recurrencia se mide en repeticiones y un turno guardado dos veces la infla.
# Reproducible > incremental. Y nunca toca data/produccion.db, que es la del demo.
DB_EVAL = Path(__file__).resolve().parent.parent / "data" / "eval_produccion.db"

# Pesos del emparejador (ver _puntaje). El texto pesa mas que el numero a
# proposito: si emparejaramos por numero, una cifra mal leida se veria como una
# fila faltante y no como lo que es, un numero equivocado.
PUNTO_PISTA = 5
PUNTO_ESTACION = 2
PUNTO_VALOR = 2
PUNTO_BUCKET = 1
UMBRAL_EMPAREJAR = 4

_BUCKETS = {"paradas": "parada", "scrap": "scrap", "calidad": "calidad"}
_VACIAS = {"de", "del", "la", "el", "los", "las", "en", "por", "con"}


# --- Utilidades de texto y numero --------------------------------------------

def _norm(texto: object) -> str:
    """Minusculas, sin tildes, espacios colapsados. Para comparar contra pistas."""
    if not isinstance(texto, str):
        return ""
    plano = unicodedata.normalize("NFKD", texto)
    plano = "".join(c for c in plano if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", plano.lower()).strip()


def _num(valor: object) -> float | None:
    """Convierte a float lo que se pueda; None si no hay numero.

    Distingue ausencia de cero: devolver None aqui es una respuesta, no un
    fallo. Acepta "25", "25 min", "58.0" y "8,5" — que es como escribe la gente
    en un reporte de turno. No maneja separador de miles porque en estos datos
    no lo hay y "1.234" seria ambiguo.
    """
    if valor is None or isinstance(valor, bool):
        return None
    if isinstance(valor, (int, float)):
        return float(valor)
    if isinstance(valor, str):
        m = re.search(r"-?\d+(?:[.,]\d+)?", valor.replace(" ", ""))
        if m:
            return float(m.group(0).replace(",", "."))
    return None


def _iguales(a: float | None, b: float | None) -> bool:
    return a is not None and b is not None and abs(a - b) < 0.01


def _tokens(texto: str) -> set[str]:
    return {
        t for t in re.split(r"[^a-z0-9]+", _norm(texto))
        if len(t) >= 3 and t not in _VACIAS
    }


def _mismo_lugar(esperada: str | None, obtenida: str | None) -> bool:
    """Compara estaciones tolerando alias y abreviaturas.

    "Inyeccion de poliuretano" contra "Inyeccion PU", o "Remachado" contra
    "remachadora": el reporte nunca escribe el nombre canonico completo. Basta
    un token de fondo compartido.
    """
    a, b = _tokens(esperada or ""), _tokens(obtenida or "")
    if not a or not b:
        return False
    if a & b:
        return True
    return any(
        x.startswith(y) or y.startswith(x)
        for x in a if len(x) >= 4
        for y in b if len(y) >= 4
    )


def _hora(valor: object) -> str | None:
    """Normaliza "9:40", "09.40", "9h40" a "09:40"."""
    if not isinstance(valor, str):
        return None
    m = re.search(r"(\d{1,2})\s*[:.hH]\s*(\d{2})", valor)
    return f"{int(m.group(1)):02d}:{m.group(2)}" if m else None


def _fecha_iso(valor: object) -> str | None:
    """Lleva a YYYY-MM-DD lo que se pueda; None si no es una fecha completa."""
    if not isinstance(valor, str) or not valor.strip():
        return None
    t = valor.strip()
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", t)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.search(r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})", t)
    if m:  # dd/mm/yyyy: en planta nadie escribe mm/dd
        return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    return None


def _pct(ok: int, total: int) -> str:
    return f"{100 * ok / total:3.0f}%" if total else " n/a"


# --- Acceso tolerante a la forma de la extraccion ----------------------------
#
# El contrato fija las firmas, no los nombres exactos de cada clave intermedia.
# El harness no puede fallar porque `normalizar` deje `causa_id` en vez de
# `causa_codigo`: eso seria medir el estilo del modulo, no la extraccion.

def _primero(d: dict, *claves: str) -> object:
    for k in claves:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return None


def _mapas(mods) -> dict:
    """id -> codigo/nombre, para leer filas ya normalizadas."""
    mapa = {"causas": {}, "estaciones": {}, "lineas": {}}
    try:
        for c in mods.db.causas():
            mapa["causas"][c["id"]] = c["codigo"]
        for e in mods.db.estaciones():
            mapa["estaciones"][e["id"]] = e["nombre"]
        for ln in mods.db.lineas():
            mapa["lineas"][ln["id"]] = ln["nombre"]
    except Exception:
        # Sin catalogo cargado se comparan solo los campos de texto. Degradar es
        # preferible a abortar: el resto de metricas sigue siendo valido.
        pass
    return mapa


# PAR-MEC-02, SCR-PU-01, CAL-RUI-01: el bloque del medio no siempre son tres
# letras (SCR-PU-01), y darlo por hecho descarta codigos validos en silencio.
_RE_CODIGO = re.compile(r"^[A-Z]{3}-[A-Z]{2,4}-\d{2}$")


def _codigo(fila: dict, mapas: dict) -> str | None:
    """Codigo canonico de una fila ya normalizada (o de un grupo de recurrencia).

    Manda `causa_id` sobre `causa_codigo` cuando esta presente, aunque venga en
    None. Es a proposito: `causa_codigo` es lo que PROPUSO el modelo y
    `causa_id` es lo que la taxonomia acepto. Lo que entra a la DB, y por tanto
    lo que sostiene el pareto y la recurrencia, es el segundo. Medir la
    propuesta seria darle credito al extractor por un codigo que normalizar
    descarto.
    """
    if "causa_id" in fila:
        cid = fila["causa_id"]
        return mapas["causas"].get(cid) if isinstance(cid, int) else None

    v = _primero(fila, "causa_codigo", "codigo", "codigo_causa")
    if not isinstance(v, str):
        # Los grupos de causas_recurrentes traen la causa anidada.
        anidada = fila.get("causa")
        v = anidada.get("codigo") if isinstance(anidada, dict) else anidada
    if isinstance(v, str) and _RE_CODIGO.match(v.strip().upper()):
        return v.strip().upper()
    return None


def _estacion(fila: dict, mapas: dict) -> str | None:
    v = _primero(fila, "estacion", "estacion_nombre")
    if isinstance(v, str):
        return v
    eid = fila.get("estacion_id")
    return mapas["estaciones"].get(eid) if isinstance(eid, int) else None


def _linea(turno: dict, mapas: dict) -> str | None:
    v = _primero(turno, "linea", "linea_nombre")
    if isinstance(v, str):
        return v.strip().upper()
    lid = turno.get("linea_id")
    n = mapas["lineas"].get(lid) if isinstance(lid, int) else None
    return n.upper() if isinstance(n, str) else None


def _texto_fila(fila: dict) -> str:
    partes = [
        fila.get(k) for k in
        ("causa_texto", "tipo_defecto", "descripcion", "detalle", "nota", "observacion")
    ]
    return _norm(" ".join(p for p in partes if isinstance(p, str)))


def _aplanar(datos: dict, mapas: dict) -> list[dict]:
    """Todas las filas de todos los turnos, con su cubo de origen."""
    filas = []
    for turno in datos.get("turnos") or []:
        if not isinstance(turno, dict):
            continue
        for clave, bucket in _BUCKETS.items():
            for f in turno.get(clave) or []:
                if isinstance(f, dict):
                    filas.append({
                        "bucket": bucket,
                        "texto": _texto_fila(f),
                        "estacion": _estacion(f, mapas),
                        "codigo": _codigo(f, mapas),
                        "cruda": f,
                    })
    return filas


def _contar_no_literales(datos: object) -> list[str]:
    """Recorre la estructura y junta todo lo que marco verificar_literalidad.

    Recursivo porque el contrato dice "en ese turno" pero una implementacion
    razonable puede marcarlo tambien en la fila. Contar de mas en el nivel
    equivocado seria peor que recorrer.
    """
    marcados: list[str] = []
    if isinstance(datos, dict):
        for k, v in datos.items():
            if k == "campos_no_literales" and isinstance(v, list):
                marcados += [str(x) for x in v]
            else:
                marcados += _contar_no_literales(v)
    elif isinstance(datos, list):
        for v in datos:
            marcados += _contar_no_literales(v)
    return marcados


# --- Emparejador gold <-> extraccion -----------------------------------------

def _puntaje(gold: dict, ext: dict) -> int:
    """Que tanto se parecen una fila esperada y una extraida."""
    if ext["bucket"] not in gold.get("buscar_en", [gold["tipo"]]):
        return 0

    s = PUNTO_BUCKET
    pistas = [_norm(p) for p in gold.get("pistas", [])]
    todas = [_norm(p) for p in gold.get("pistas_todas", [])]
    if pistas and any(p in ext["texto"] for p in pistas):
        s += PUNTO_PISTA
    if todas and all(p in ext["texto"] for p in todas):
        s += PUNTO_PISTA
    if _mismo_lugar(gold.get("estacion"), ext["estacion"]):
        s += PUNTO_ESTACION

    # El numero desempata dentro de un grupo de filas con la misma causa: las
    # tres paradas de la R-02 comparten pistas y solo se distinguen por minutos.
    for campo, esperado in (gold.get("valores") or {}).items():
        if _iguales(_num(ext["cruda"].get(campo)), float(esperado)):
            s += PUNTO_VALOR
    for campo in gold.get("ceros") or []:
        v = _num(ext["cruda"].get(campo))
        if v is not None and abs(v) < 1e-9:
            s += PUNTO_VALOR
    return s


def _emparejar(golds: list[dict], extraidas: list[dict]) -> dict[int, int]:
    """Asignacion voraz por puntaje. Devuelve {indice_gold: indice_extraida}."""
    pares = []
    for gi, g in enumerate(golds):
        for pi, p in enumerate(extraidas):
            s = _puntaje(g, p)
            if s >= UMBRAL_EMPAREJAR:
                pares.append((s, gi, pi))
    pares.sort(key=lambda x: (-x[0], x[1], x[2]))

    asignado: dict[int, int] = {}
    usadas: set[int] = set()
    for _, gi, pi in pares:
        if gi in asignado or pi in usadas:
            continue
        asignado[gi] = pi
        usadas.add(pi)
    return asignado


# --- Evaluacion ---------------------------------------------------------------

def _metricas() -> dict:
    return {
        "numericos_ok": 0, "numericos_total": 0,
        "cabecera_ok": 0, "cabecera_total": 0,
        "nulos_ok": 0, "nulos_total": 0,
        "ceros_ok": 0, "ceros_total": 0,
        "causas_ok": 0, "causas_total": 0,
        "estaciones_ok": 0, "estaciones_total": 0,
        "horas_ok": 0, "horas_total": 0,
        "ambiguedad_ok": 0, "ambiguedad_total": 0,
        "filas_ok": 0, "filas_total": 0,
        "no_literales": 0,
        "fallos": [],            # numeros equivocados
        "falsos_ceros": [],      # null esperado -> salio 0   (CRITICO)
        "ceros_perdidos": [],    # 0 esperado -> salio null
        "inventados": [],        # null esperado -> salio otro numero
        "causas_malas": [],
        "faltantes": [],
        "extra": [],
        "prohibidas": [],
        "observaciones_ok": False,
    }


def _revisar_campos(gold: dict, cruda: dict, etiqueta: str, m: dict) -> None:
    """Compara los tres cubos de campos numericos de una fila (o del turno)."""
    for campo, esperado in (gold.get("valores") or {}).items():
        m["numericos_total"] += 1
        obtenido = _num(cruda.get(campo))
        if _iguales(obtenido, float(esperado)):
            m["numericos_ok"] += 1
        else:
            m["fallos"].append(
                f"{etiqueta}.{campo}: esperaba {esperado}, extrajo {cruda.get(campo)!r}"
            )

    for campo in gold.get("ceros") or []:
        m["ceros_total"] += 1
        obtenido = _num(cruda.get(campo))
        if obtenido is not None and abs(obtenido) < 1e-9:
            m["ceros_ok"] += 1
        elif obtenido is None:
            m["ceros_perdidos"].append(
                f"{etiqueta}.{campo}: era un cero real y quedo en null"
            )
        else:
            m["fallos"].append(
                f"{etiqueta}.{campo}: esperaba 0, extrajo {cruda.get(campo)!r}"
            )

    for campo in gold.get("nulos") or []:
        m["nulos_total"] += 1
        obtenido = _num(cruda.get(campo))
        if obtenido is None:
            m["nulos_ok"] += 1
        elif abs(obtenido) < 1e-9:
            m["falsos_ceros"].append(
                f"{etiqueta}.{campo}: no habia dato y escribio 0"
            )
        else:
            m["inventados"].append(
                f"{etiqueta}.{campo}: no habia dato y escribio {cruda.get(campo)!r}"
            )


def _revisar_turno(caso: dict, datos: dict, mapas: dict, m: dict) -> dict:
    """Cabecera del turno: fecha, linea, plan/producido. Devuelve el turno elegido."""
    turnos = [t for t in (datos.get("turnos") or []) if isinstance(t, dict)]
    esperado = caso["turno"]
    if not turnos:
        m["fallos"].append("no se extrajo ningun turno del documento")
        return {}

    # Cada ejemplo es un solo turno. Si salieron varios se evalua el que coincide
    # con la linea esperada (los demas se reportan como filas extra a nivel doc).
    elegido = next(
        (t for t in turnos if _linea(t, mapas) == esperado.get("linea")), turnos[0]
    )
    if len(turnos) > 1:
        m["extra"].append(f"documento partido en {len(turnos)} turnos, se esperaba 1")

    _revisar_campos(esperado, elegido, "turno", m)

    # Fecha, turno y linea van aparte de los campos numericos: no son cifras del
    # proceso, son la llave del hecho. Equivocarlas no da un numero malo, da un
    # numero bueno colgado del turno equivocado, que es mas dificil de detectar.
    fecha = _fecha_iso(elegido.get("fecha")) or (
        elegido.get("fecha") if elegido.get("fecha") else None
    )
    aceptadas = esperado.get("fecha_aceptadas") or []
    m["cabecera_total"] += 1
    if fecha in aceptadas:
        m["cabecera_ok"] += 1
    else:
        m["fallos"].append(
            f"turno.fecha: esperaba una de {aceptadas}, extrajo {elegido.get('fecha')!r}"
        )

    for campo in ("turno", "linea"):
        if esperado.get(campo) is None:
            continue
        m["cabecera_total"] += 1
        obtenido = _linea(elegido, mapas) if campo == "linea" else _num(elegido.get("turno"))
        ref = esperado[campo] if campo == "linea" else float(esperado[campo])
        ok = obtenido == ref if campo == "linea" else _iguales(obtenido, ref)
        if ok:
            m["cabecera_ok"] += 1
        else:
            m["fallos"].append(f"turno.{campo}: esperaba {ref}, extrajo {obtenido!r}")

    # La fecha ambigua ("lunes 20", sin mes ni ano) tiene que dejar rastro: o
    # confianza baja o una nota de revision. Extraerla con seguridad plena es el
    # fallo, aunque el numero termine siendo el correcto.
    if esperado.get("fecha_ambigua"):
        m["ambiguedad_total"] += 1
        confianza = _num(elegido.get("confianza"))
        nota = elegido.get("nota_revision")
        if (confianza is not None and confianza < 1.0) or (isinstance(nota, str) and nota.strip()):
            m["ambiguedad_ok"] += 1
        else:
            m["fallos"].append("turno.fecha: ambigua en el texto y no quedo marcada")

    # Las observaciones alimentan el RAG: si se pierden, el agente responde con
    # numeros y sin el "por que" que escribio el supervisor.
    obs = " ".join(
        o for o in (elegido.get("observaciones") or []) if isinstance(o, str)
    )
    obs = _norm(obs) or _norm(str(elegido.get("nota_revision") or ""))
    m["observaciones_ok"] = any(
        _norm(p) in obs for p in caso.get("observaciones_pistas", [])
    )
    return elegido


def evaluar_caso(caso: dict, datos: dict, no_literales: list[str], mapas: dict) -> dict:
    """Compara una extraccion contra su gold set. Cero LLM: todo determinista."""
    m = _metricas()
    m["no_literales"] = len(no_literales)
    m["campos_no_literales"] = no_literales

    _revisar_turno(caso, datos, mapas, m)

    golds = caso["filas"]
    extraidas = _aplanar(datos, mapas)
    asignado = _emparejar(golds, extraidas)
    m["filas_total"] = len(golds)
    m["filas_ok"] = len(asignado)

    for gi, gold in enumerate(golds):
        etiqueta = f"{gold['tipo']}[{(gold.get('pistas') or gold.get('pistas_todas') or ['?'])[0]}]"
        pi = asignado.get(gi)
        if pi is None:
            # Fila no extraida: sus campos cuentan como fallo, no se ignoran. Un
            # numero que nunca se leyo esta tan mal como uno leido al reves.
            m["faltantes"].append(etiqueta)
            m["numericos_total"] += len(gold.get("valores") or {})
            m["ceros_total"] += len(gold.get("ceros") or [])
            m["nulos_total"] += len(gold.get("nulos") or [])
            # La causa cuenta como fallida, pero sin duplicar el mensaje: la
            # linea de "fila no extraida" ya lo dice todo.
            if gold.get("codigos") is not None:
                m["causas_total"] += 1
            if gold.get("estacion"):
                m["estaciones_total"] += 1
            if gold.get("hora_inicio"):
                m["horas_total"] += 1
            if "confianza_max" in gold:
                m["ambiguedad_total"] += 1
            continue

        ext = extraidas[pi]
        _revisar_campos(gold, ext["cruda"], etiqueta, m)

        if gold.get("codigos") is not None:
            m["causas_total"] += 1
            codigo = ext["codigo"]
            # Caer en el cubo correcto puede ser la clasificacion: la tabla
            # `calidad` no tiene causa_id, tiene tipo_defecto.
            por_bucket = gold.get("equivale_bucket") == ext["bucket"] and codigo is None
            if codigo in gold["codigos"] or por_bucket:
                m["causas_ok"] += 1
            else:
                m["causas_malas"].append(
                    f"{etiqueta}: esperaba {gold['codigos']}, clasifico {codigo!r} "
                    f"en {ext['bucket']}"
                )

        if gold.get("estacion"):
            m["estaciones_total"] += 1
            if _mismo_lugar(gold["estacion"], ext["estacion"]):
                m["estaciones_ok"] += 1

        if gold.get("hora_inicio"):
            m["horas_total"] += 1
            if _hora(ext["cruda"].get("hora_inicio")) == gold["hora_inicio"]:
                m["horas_ok"] += 1

        if "confianza_max" in gold:
            m["ambiguedad_total"] += 1
            c = _num(ext["cruda"].get("confianza"))
            if c is not None and c <= gold["confianza_max"] + 1e-9:
                m["ambiguedad_ok"] += 1

    # Filas inventadas y filas que no debian existir (observaciones convertidas
    # en hechos con minutos).
    usadas = set(asignado.values())
    for pi, ext in enumerate(extraidas):
        if pi in usadas or ext["bucket"] == "calidad":
            continue
        m["extra"].append(f"{ext['bucket']}: {ext['texto'][:60] or '(sin texto)'}")
    for prohibida in caso.get("prohibidas", []):
        pistas = [_norm(p) for p in prohibida.get("pistas_todas", [])]
        for ext in extraidas:
            if ext["bucket"] == "calidad" or not pistas:
                continue
            if all(p in ext["texto"] for p in pistas):
                m["prohibidas"].append(
                    f"{ext['bucket']}: {prohibida['motivo']} -> {ext['texto'][:60]}"
                )
    return m


# --- Pipeline de extraccion ---------------------------------------------------

def _modulos(ruta_db: Path) -> SimpleNamespace:
    """Importa produccion/* y apunta la DB al sandbox del harness."""
    # Antes del import: db.py resuelve RUTA_DB al importarse. Se fija tambien el
    # atributo despues, por si alguien importo db antes que este harness.
    import os
    os.environ["PRODUCCION_DB"] = str(ruta_db)
    try:
        from produccion import db, extraer, herramientas, ingest, normalizar
    except ImportError as e:
        raise SystemExit(
            f"\n  No pude importar los modulos de produccion: {e}\n"
            "  Este harness mide produccion/extraer.py + normalizar.py + db.py +\n"
            "  herramientas.py. Corre desde la raiz del repo y con esos archivos ya\n"
            "  escritos.\n"
        )

    ruta_db.parent.mkdir(parents=True, exist_ok=True)
    db.RUTA_DB = ruta_db          # sandbox: no se toca la DB del demo
    return SimpleNamespace(
        db=db, extraer=extraer, herramientas=herramientas,
        ingest=ingest, normalizar=normalizar,
    )


def extraer_caso(caso: dict, mods) -> dict:
    """a_texto -> extraer -> verificar_literalidad -> normalizar, en ese orden.

    La literalidad se verifica ANTES de normalizar: se compara contra el texto
    crudo, que es la unica referencia honesta. Despues de normalizar los valores
    ya pasaron por transformaciones y el guard mediria su propio trabajo.
    """
    ruta = EJEMPLOS / caso["archivo"]
    texto, formato = mods.ingest.a_texto(ruta)

    crudo = mods.extraer.extraer(texto)
    if not (crudo.get("turnos") or []):
        return {
            "formato": formato, "texto": texto, "extraccion": crudo,
            "no_literales": [], "error": crudo.get("error") or "la extraccion vino vacia",
        }

    marcado = mods.extraer.verificar_literalidad(crudo, texto)
    no_literales = _contar_no_literales(marcado)
    final = mods.normalizar.normalizar(marcado)
    return {
        "formato": formato, "texto": texto, "extraccion": final,
        "no_literales": no_literales, "error": None,
    }


def _completar_desde_nombre(caso: dict, turno: dict) -> list[str]:
    """Rellena fecha y linea desde el nombre del archivo si faltan.

    No es hacerle trampa a la metrica: la extraccion ya se midio con lo que el
    modelo produjo, y esto ocurre despues. Es lo mismo que hace el pipeline real
    — `ingest` conoce la ruta del archivo, `extraer` solo ve el texto — y sin
    esto el turno del WhatsApp ("lunes 20", sin ano) no entra a la DB y la
    metrica de recurrencia queda sin medir por una razon que no es la suya.
    """
    completados = []
    nombre = caso["archivo"]
    if not _fecha_iso(turno.get("fecha")):
        m = re.search(r"(\d{4}-\d{2}-\d{2})", nombre)
        if m:
            turno["fecha"] = m.group(1)
            completados.append("fecha")
    if not turno.get("linea") and not turno.get("linea_id"):
        m = re.search(r"_(L\d)_", nombre)
        if m:
            turno["linea"] = m.group(1)
            completados.append("linea")
    if turno.get("turno") is None and caso["turno"].get("turno"):
        turno["turno"] = caso["turno"]["turno"]
        completados.append("turno")
    return completados


def cargar_en_db(caso: dict, datos: dict, formato: str, texto: str, mods) -> dict:
    """Guarda los turnos extraidos para poder medir recurrencia sobre los 3."""
    ruta = EJEMPLOS / caso["archivo"]
    try:
        sha = mods.ingest.sha256_archivo(ruta)
        doc_id = mods.db.registrar_documento(str(ruta), sha, formato, texto)
        if doc_id is None:
            # Ya estaba registrado (corrida anterior sobre la misma DB): se
            # recupera el id para poder guardar los turnos igual, porque
            # guardar_turno es idempotente y registrar_documento no.
            cx = mods.db.conectar()
            try:
                fila = cx.execute(
                    "SELECT id FROM documentos WHERE sha256 = ?", (sha,)
                ).fetchone()
                doc_id = fila["id"] if fila else None
            finally:
                cx.close()   # `with` sobre una conexion sqlite maneja la
                             # transaccion, no la cierra. Aqui hay que cerrarla.
        if doc_id is None:
            return {"guardados": 0, "motivo": "no se pudo registrar el documento"}

        guardados, completados = 0, []
        for turno in datos.get("turnos") or []:
            completados += _completar_desde_nombre(caso, turno)
            mods.db.guardar_turno(doc_id, turno)
            guardados += 1
        return {"guardados": guardados, "completados": sorted(set(completados)), "motivo": None}
    except Exception as e:  # noqa: BLE001
        return {"guardados": 0, "motivo": f"{type(e).__name__}: {e}"}


def medir_recurrencia(mods, mapas: dict) -> dict:
    """La prueba de fin a fin: el patron de la R-02 sobre los 3 turnos."""
    # La ventana se calcula desde la fecha mas vieja del gold set hasta hoy, y no
    # se deja en 7 dias fijos: si no, este eval empieza a fallar solo por el paso
    # del tiempo y nadie sabe si se rompio el codigo o el calendario.
    fechas = [
        f for caso in CASOS for f in (caso["turno"].get("fecha_aceptadas") or []) if f
    ]
    vieja = min(datetime.strptime(f, "%Y-%m-%d").date() for f in fechas)
    dias = max(7, (date.today() - vieja).days + 2)

    try:
        r = mods.herramientas.causas_recurrentes(
            dias=dias,
            min_repeticiones=RECURRENCIA["min_repeticiones"],
            linea=RECURRENCIA["linea"],
        )
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "motivo": f"{type(e).__name__}: {e}", "dias": dias}

    if not r.get("disponible"):
        return {"ok": False, "motivo": r.get("motivo") or "disponible: False", "dias": dias}

    grupos = r.get("datos") or []
    if isinstance(grupos, dict):
        grupos = grupos.get("grupos") or grupos.get("causas") or []

    encontrado, repeticiones, minutos = None, None, None
    for g in grupos:
        if not isinstance(g, dict):
            continue
        cod = _codigo(g, mapas)
        est = _estacion(g, mapas)
        if cod == RECURRENCIA["codigo"] and (
            _mismo_lugar(RECURRENCIA["estacion"], est) or est is None
        ):
            encontrado = g
            repeticiones = _num(_primero(
                g, "repeticiones", "veces", "apariciones", "conteo", "eventos", "n"
            ))
            minutos = _num(_primero(
                g, "minutos_totales", "minutos", "minutos_total", "total_minutos",
                "minutos_perdidos"
            ))
            break

    # El contra-caso: lo que esta por debajo del umbral no puede aparecer.
    ruido = [
        c for c in RECURRENCIA["no_esperados"]
        if any(isinstance(g, dict) and _codigo(g, mapas) == c for g in grupos)
    ]

    minimo = RECURRENCIA["min_repeticiones"]
    return {
        "ok": bool(encontrado) and (repeticiones is None or repeticiones >= minimo) and not ruido,
        "dias": dias,
        "grupos": len(grupos),
        "repeticiones": repeticiones,
        "repeticiones_esperadas": RECURRENCIA["repeticiones_esperadas"],
        "minutos": minutos,
        "minutos_esperados": RECURRENCIA["minutos_esperados"],
        "exacto": _iguales(repeticiones, float(RECURRENCIA["repeticiones_esperadas"])),
        "falsos_positivos": ruido,
        "motivo": None if encontrado else (
            f"no aparecio {RECURRENCIA['codigo']} en {RECURRENCIA['estacion']} "
            f"de {RECURRENCIA['linea']}"
        ),
        "cobertura": r.get("cobertura"),
    }


# --- Persistencia -------------------------------------------------------------

def cargar() -> dict:
    if RESULTADOS.exists():
        try:
            return json.loads(RESULTADOS.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def guardar(datos: dict) -> None:
    RESULTADOS.write_text(
        json.dumps(datos, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )


def correr(forzar: bool = False, ruta_db: Path = DB_EVAL) -> dict:
    previo = cargar()
    cache = previo.get("casos", {}) if not forzar else {}

    mods = _modulos(ruta_db)
    if forzar or not ruta_db.exists():
        # DB limpia: la recurrencia cuenta repeticiones y una carga doble miente.
        mods.db.inicializar(forzar=True)
    else:
        mods.db.inicializar()
    mapas = _mapas(mods)

    salida = {"generado_en": datetime.now().isoformat(timespec="seconds"),
              "db": str(ruta_db), "casos": {}}

    for i, caso in enumerate(CASOS, 1):
        etiqueta = f"  [{i}/{len(CASOS)}] {caso['id']} {caso['archivo'][:38]:<40}"
        guardado = cache.get(caso["id"], {})
        crudo = guardado.get("crudo")

        if crudo and not forzar:
            print(etiqueta + "(cache)", flush=True)
        else:
            print(etiqueta + "extrayendo...", end=" ", flush=True)
            try:
                crudo = extraer_caso(caso, mods)
            except Exception as e:  # noqa: BLE001
                print(f"ERROR {type(e).__name__}: {e}")
                salida["casos"][caso["id"]] = {"error": f"{type(e).__name__}: {e}"}
                continue
            print(("ERROR: " + crudo["error"]) if crudo.get("error") else "ok")

        m = evaluar_caso(caso, crudo["extraccion"], crudo.get("no_literales") or [], mapas)
        carga = cargar_en_db(caso, crudo["extraccion"], crudo["formato"], crudo["texto"], mods)

        salida["casos"][caso["id"]] = {
            "id": caso["id"],
            "archivo": caso["archivo"],
            "titulo": caso["titulo"],
            "trampas": caso["trampas"],
            "error": crudo.get("error"),
            "metricas": m,
            "carga": carga,
            # Se guarda la extraccion completa: es el cache que evita volver a
            # llamar al modelo, y ademas es lo que se mira cuando un numero falla.
            "crudo": crudo,
        }
        guardar(salida)

    salida["recurrencia"] = medir_recurrencia(mods, mapas)
    salida["totales"] = _totales(salida["casos"])
    guardar(salida)
    return salida


def _totales(casos: dict) -> dict:
    llaves_num = [
        "numericos_ok", "numericos_total", "cabecera_ok", "cabecera_total",
        "nulos_ok", "nulos_total",
        "ceros_ok", "ceros_total", "causas_ok", "causas_total",
        "estaciones_ok", "estaciones_total", "horas_ok", "horas_total",
        "ambiguedad_ok", "ambiguedad_total", "filas_ok", "filas_total",
        "no_literales",
    ]
    llaves_lista = [
        "falsos_ceros", "ceros_perdidos", "inventados", "faltantes", "extra",
        "prohibidas", "causas_malas", "fallos",
    ]
    t = {k: 0 for k in llaves_num} | {k: [] for k in llaves_lista}
    for c in casos.values():
        m = c.get("metricas")
        if not m:
            continue
        for k in llaves_num:
            t[k] += m.get(k, 0)
        for k in llaves_lista:
            t[k] += m.get(k, [])
    return t


# --- Tablero ------------------------------------------------------------------

def tablero(datos: dict) -> None:
    casos = datos.get("casos") or {}
    if not casos:
        print("Sin resultados todavia. Corre: python -m evals_produccion.run")
        return

    print("\n" + "=" * 68)
    print(" TABLERO DE EXTRACCION — Copiloto de Produccion HACEB")
    print("=" * 68)

    for c in casos.values():
        m = c.get("metricas")
        print(f"\n  {c['id']}  {c['archivo']}")
        print(f"        {c.get('titulo','')}")
        if not m:
            print(f"        ERROR: {c.get('error')}")
            continue

        print(f"        numericos {m['numericos_ok']}/{m['numericos_total']}"
              f"   causas {m['causas_ok']}/{m['causas_total']}"
              f"   nulls {m['nulos_ok']}/{m['nulos_total']}"
              f"   ceros {m['ceros_ok']}/{m['ceros_total']}"
              f"   cabecera {m.get('cabecera_ok',0)}/{m.get('cabecera_total',0)}")
        print(f"        filas {m['filas_ok']}/{m['filas_total']} emparejadas"
              f"   estaciones {m['estaciones_ok']}/{m['estaciones_total']}"
              f"   horas {m['horas_ok']}/{m['horas_total']}")

        detalles = (
            [("!", x) for x in m["falsos_ceros"]]
            + [("!", x) for x in m["prohibidas"]]
            + [("x", x) for x in m["ceros_perdidos"]]
            + [("x", x) for x in m["inventados"]]
            + [("x", x) for x in m["fallos"]]
            + [("x", x) for x in m["causas_malas"]]
            + [("-", f"fila no extraida: {x}") for x in m["faltantes"]]
            + [("-", f"fila de mas: {x}") for x in m["extra"]]
        )
        for marca, texto in detalles[:12]:
            print(f"          {marca} {texto}")
        if len(detalles) > 12:
            print(f"          ... y {len(detalles) - 12} mas (ver resultados.json)")
        if not detalles:
            print("          ✓ sin hallazgos: las trampas del ejemplo pasaron")
        if not m.get("observaciones_ok"):
            print("          - observaciones clave no capturadas (el RAG se queda sin contexto)")
        carga = c.get("carga") or {}
        if carga.get("motivo"):
            print(f"          - no entro a la DB: {carga['motivo']}")
        elif carga.get("completados"):
            print(f"          - completado desde el nombre del archivo: "
                  f"{', '.join(carga['completados'])}")

    t = datos.get("totales") or {}
    r = datos.get("recurrencia") or {}
    print("\n" + "-" * 68)
    print(f"  CAMPOS NUMERICOS       {t.get('numericos_ok',0)}/{t.get('numericos_total',0)}"
          f"   ({_pct(t.get('numericos_ok',0), t.get('numericos_total',0))})")
    print(f"  CABECERA DE TURNO      {t.get('cabecera_ok',0)}/{t.get('cabecera_total',0)}"
          f"   ({_pct(t.get('cabecera_ok',0), t.get('cabecera_total',0))})"
          "   fecha, turno y linea")
    print(f"  CLASIFICACION CAUSA    {t.get('causas_ok',0)}/{t.get('causas_total',0)}"
          f"   ({_pct(t.get('causas_ok',0), t.get('causas_total',0))})")

    fc = len(t.get("falsos_ceros", []))
    print(f"  FALSOS CEROS           {fc}"
          + ("        <- CRITICO: un 0 inventado se lee como un hecho"
             if fc else "        <- ninguno; los nulls se preservaron"))
    print(f"  CEROS REALES PERDIDOS  {len(t.get('ceros_perdidos', []))}"
          f"        (0 que se volvio null)")
    print(f"  NUMEROS INVENTADOS     {len(t.get('inventados', []))}")

    nl = t.get("no_literales", 0)
    veredicto = "OK" if nl <= LITERALIDAD_ESPERADA_MAX else (
        "REVISAR: el guard tiene falsos positivos, estos numeros SI estan en el texto"
    )
    print(f"  LITERALIDAD MARCADA    {nl} campos (umbral {LITERALIDAD_ESPERADA_MAX})   {veredicto}")
    print(f"  FILAS                  {t.get('filas_ok',0)}/{t.get('filas_total',0)} emparejadas"
          f"   ·  {len(t.get('extra', []))} de mas"
          f"   ·  {len(t.get('prohibidas', []))} prohibidas")
    print(f"  AMBIGUEDAD MARCADA     {t.get('ambiguedad_ok',0)}/{t.get('ambiguedad_total',0)}"
          f"   (fechas y minutos aproximados)")

    if r:
        # Tres estados, no dos: el patron puede aparecer con menos eventos de los
        # que hay (una parada quedo sin clasificar y no entro al grupo). Eso pasa
        # la prueba pero no es un exito limpio, y marcarlo con un ✓ lo esconde.
        marca = "✓" if r.get("ok") and r.get("exacto") else ("~" if r.get("ok") else "✗")
        detalle = (
            f"{r.get('repeticiones')} repeticiones / {r.get('minutos')} min "
            f"(esperado {r['repeticiones_esperadas']} / {r['minutos_esperados']})"
            if r.get("repeticiones") is not None
            else (r.get("motivo") or "sin patron")
        )
        print(f"  RECURRENCIA R-02 L2    {marca} {detalle}")
        if r.get("ok") and not r.get("exacto"):
            print("                         ~ patron detectado, pero no cuadran los "
                  "eventos: hay paradas de la R-02 que no entraron al grupo")
        if r.get("falsos_positivos"):
            print(f"                         ! reporto bajo el umbral: "
                  f"{', '.join(r['falsos_positivos'])}")
        if not r.get("ok") and r.get("motivo"):
            print(f"                         motivo: {r['motivo']}")
    print("=" * 68 + "\n")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    ap = argparse.ArgumentParser(description="Evalua la extraccion de reportes de turno")
    ap.add_argument("--tablero", action="store_true", help="solo mostrar lo ya medido")
    ap.add_argument("--forzar", action="store_true", help="ignorar el cache y re-extraer")
    ap.add_argument("--db", default=str(DB_EVAL), help="DB de trabajo del harness")
    args = ap.parse_args()

    if args.tablero:
        tablero(cargar())
    else:
        tablero(correr(forzar=args.forzar, ruta_db=Path(args.db)))
