"""
Las herramientas numericas del Copiloto de Produccion.

Aqui vive TODA la aritmetica del producto. Ni una linea de este archivo habla
con un modelo: si el agente dice un numero, ese numero salio de una consulta a
SQLite y de una suma hecha en Python. Esa separacion es el producto, no un
detalle de implementacion.

Tres convenciones que aplican a las ocho funciones:

1. El retorno siempre es
       {"disponible": True, "datos": ..., "cobertura": {...}, "periodo": {...}}
   o
       {"disponible": False, "motivo": "..."}.
   Nunca 0 ni [] en lugar de `disponible: False`. Un cero medido y un dato que
   nadie cargo son cosas distintas, y el agente tiene que poder distinguirlas.

2. Un valor que no existe es None, no 0. Si en el rango hay 5 registros de scrap
   y los 5 traen `unidades` en NULL, la respuesta es `unidades: None` con
   `registros: 5`, no `unidades: 0`. Sumar NULLs como ceros es la forma mas
   silenciosa de mentir.

3. Lo que no se pudo clasificar no se esconde. Una causa que se repite seis
   veces pero que nadie mapeo al catalogo sigue siendo una senal; ocultarla
   porque `causa_id` es NULL seria exactamente el error que este producto
   quiere evitar. Va en la respuesta marcada con `"clasificada": False`.
"""

from __future__ import annotations

import difflib
import re
import sqlite3
import unicodedata
from collections import Counter
from datetime import date, datetime, timedelta
from typing import Any

try:  # uso normal: paquete `produccion`
    from . import db
except ImportError:  # ejecucion suelta (python produccion/herramientas.py)
    import db  # type: ignore


# Turnos por dia que asume la estimacion de cobertura. La planta opera 3x8.
TURNOS_POR_DIA = 3

# Umbral para decidir que dos textos libres describen la misma causa. Es mas
# exigente que el 0.72 de normalizar.py a proposito: alla hay un catalogo que
# valida el match, aqui no hay contra que contrastar, asi que antes de decir
# "esto se repitio" se pide mas parecido. Falso negativo (dos grupos separados)
# se ve feo; falso positivo (juntar dos problemas distintos) manda al tecnico a
# la maquina equivocada.
UMBRAL_SIMILITUD = 0.80

# Cuantos textos crudos se guardan como evidencia por grupo. Suficiente para que
# el supervisor reconozca de que se esta hablando, sin inflar el JSON que ve el
# modelo.
MAX_EVIDENCIA = 5


# ── Helpers de retorno y formato ────────────────────────────────────────────

def _no(motivo: str) -> dict:
    """El unico constructor de respuestas negativas. Nunca devuelve datos."""
    return {"disponible": False, "motivo": motivo}


def _si(datos: Any, cobertura: dict, periodo: dict) -> dict:
    return {"disponible": True, "datos": datos, "cobertura": cobertura, "periodo": periodo}


def _r(valor: Any, decimales: int = 2) -> float | None:
    """Redondea conservando el None. Redondear un None a 0 es perder el dato."""
    if valor is None:
        return None
    return round(float(valor), decimales)


def _pct(parte: float | None, total: float | None, decimales: int = 1) -> float | None:
    if parte is None or not total:
        return None
    return round(parte * 100.0 / total, decimales)


def _sumar(valores: list) -> tuple[float | None, int]:
    """Suma ignorando None y devuelve (suma, cuantos aportaron).

    Si ninguno aporto, la suma es None y no 0.0: la diferencia entre "sumo cero"
    y "no habia nada que sumar" es justo la que este producto no puede perder.
    """
    presentes = [float(v) for v in valores if v is not None]
    if not presentes:
        return None, 0
    return sum(presentes), len(presentes)


def _normalizar_texto(texto: str | None) -> str:
    """Minusculas, sin tildes, sin puntuacion, espacios colapsados.

    No se tocan los digitos: "R-02" y "R-04" tienen que seguir siendo distintos,
    que es justo lo que le importa al que va a ir a mirar la maquina.
    """
    base = unicodedata.normalize("NFKD", texto or "")
    base = "".join(c for c in base if not unicodedata.combining(c))
    base = re.sub(r"[^a-z0-9]+", " ", base.lower())
    return " ".join(base.split())


def _codigos(texto_normalizado: str) -> frozenset[str]:
    """Tokens con digitos de un texto ya normalizado: 'r 02' -> {'02'}.

    Son los que identifican la maquina, la estacion o el numero de equipo. Se
    extraen aparte porque pesan distinto que las palabras: dos frases pueden ser
    identicas salvo por el numero y describir problemas de dos maquinas.
    """
    return frozenset(t for t in texto_normalizado.split() if any(c.isdigit() for c in t))


def _similitud(a: str, b: str) -> float:
    """Parecido entre dos textos normalizados: literal o por conjunto de tokens.

    Se toma el maximo de los dos porque el supervisor reordena las palabras sin
    darse cuenta: "se trabo la R-02" y "la R-02 se volvio a trabar" son la misma
    frase para cualquiera menos para una comparacion literal.
    """
    literal = difflib.SequenceMatcher(None, a, b).ratio()
    tokens_a = " ".join(sorted(set(a.split())))
    tokens_b = " ".join(sorted(set(b.split())))
    return max(literal, difflib.SequenceMatcher(None, tokens_a, tokens_b).ratio())


def _clusters(textos: list[str], umbral: float = UMBRAL_SIMILITUD) -> list[list[int]]:
    """Agrupa indices de textos parecidos entre si (difflib, determinista).

    Es lo que permite juntar "R-02 atascada otra vez" con "la R-02 se volvio a
    trabar" cuando ninguna de las dos quedo clasificada. Gana el grupo mas
    parecido por encima del umbral; con el orden de entrada fijo (fecha, id) el
    resultado es reproducible, que es requisito para que el agente sea auditable.

    Regla dura por encima del parecido: si los dos textos nombran codigos
    distintos, no se juntan aunque el texto sea casi identico. "falla del sensor
    de la R-02" y "falla del sensor de la R-04" se parecen en un 96% y son dos
    maquinas; fundirlas mandaria al tecnico a la que no es.
    """
    grupos: list[tuple[str, frozenset[str], list[int]]] = []
    for i, texto in enumerate(textos):
        codigos = _codigos(texto)
        destino, mejor = None, 0.0
        for grupo in grupos:
            # Codigos presentes en ambos y distintos: son cosas distintas.
            if codigos and grupo[1] and codigos != grupo[1]:
                continue
            ratio = _similitud(grupo[0], texto)
            if ratio >= umbral and ratio > mejor:
                mejor, destino = ratio, grupo
        if destino is None:
            grupos.append((texto, codigos, [i]))
        else:
            destino[2].append(i)
    return [indices for _, _, indices in grupos]


def _representante(textos: list[str]) -> str:
    """Texto mas repetido del grupo; en empate, el primero que aparecio."""
    if not textos:
        return ""
    conteo = Counter(textos)
    tope = max(conteo.values())
    for t in textos:
        if conteo[t] == tope:
            return t
    return textos[0]


# ── Fechas y rangos ─────────────────────────────────────────────────────────

def _fecha_iso(valor: Any) -> str | None:
    """Normaliza a 'YYYY-MM-DD'. Devuelve None si no se puede interpretar."""
    if valor is None:
        return None
    if isinstance(valor, datetime):
        return valor.date().isoformat()
    if isinstance(valor, date):
        return valor.isoformat()
    texto = str(valor).strip()
    if not texto:
        return None
    texto = texto.replace("/", "-")
    for patron in ("%Y-%m-%d", "%d-%m-%Y", "%Y-%m-%dT%H:%M:%S", "%d-%m-%y"):
        try:
            return datetime.strptime(texto[:19] if "T" in texto else texto, patron).date().isoformat()
        except ValueError:
            continue
    return None


def _dias(desde: str, hasta: str) -> int:
    """Dias calendario del rango, ambos extremos incluidos."""
    return (date.fromisoformat(hasta) - date.fromisoformat(desde)).days + 1


def _ambito(linea_nombre: str | None) -> str:
    return f" para {linea_nombre}" if linea_nombre else ""


# ── Acceso a la DB ──────────────────────────────────────────────────────────

def _conexion() -> tuple[sqlite3.Connection | None, dict | None]:
    """Abre la DB por db.conectar() y verifica que el esquema este puesto.

    Se comprueba aqui y no en cada funcion para que el mensaje de "la base no
    esta inicializada" salga una sola vez y sea siempre el mismo.
    """
    try:
        con = db.conectar()
    except Exception as e:  # noqa: BLE001 - archivo ausente, permisos, etc.
        return None, _no(f"no se pudo abrir la base de datos: {type(e).__name__}: {e}")
    try:
        tablas = {f[0] for f in con.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()}
    except Exception as e:  # noqa: BLE001
        con.close()
        return None, _no(f"no se pudo leer el esquema de la base: {e}")
    faltantes = {
        "turnos", "paradas", "scrap", "calidad",
        "lineas", "estaciones", "causas", "documentos", "costos",
    } - tablas
    if faltantes:
        con.close()
        return None, _no(
            "la base de datos no esta inicializada (faltan tablas: "
            + ", ".join(sorted(faltantes)) + "). Corre db.inicializar()."
        )
    return con, None


def _resolver_linea(con: sqlite3.Connection, linea: Any) -> tuple[int | None, str | None, dict | None]:
    """Convierte 'L2' / 'linea 2' / 2 en (linea_id, nombre). None = todas.

    Si el nombre no existe se devuelve error con la lista de lineas reales, en
    vez de un resultado vacio: "no hay datos de L9" y "L9 no existe" llevan a
    acciones distintas.
    """
    if linea is None or (isinstance(linea, str) and not linea.strip()):
        return None, None, None

    filas = con.execute("SELECT id, nombre FROM lineas ORDER BY id").fetchall()
    if not filas:
        return None, None, _no("no hay lineas cargadas; falta correr db.inicializar()")

    if isinstance(linea, int) and not isinstance(linea, bool):
        for f in filas:
            if f["id"] == linea:
                return f["id"], f["nombre"], None

    objetivo = _normalizar_texto(str(linea)).replace("linea", "").strip().replace(" ", "")
    for f in filas:
        candidato = _normalizar_texto(f["nombre"]).replace(" ", "")
        if candidato == objetivo:
            return f["id"], f["nombre"], None
    # "2" tambien debe encontrar "L2": se comparan solo los digitos.
    digitos = re.sub(r"\D", "", objetivo)
    if digitos:
        for f in filas:
            if re.sub(r"\D", "", f["nombre"]) == digitos:
                return f["id"], f["nombre"], None

    disponibles = ", ".join(f["nombre"] for f in filas)
    return None, None, _no(f"no existe la linea '{linea}'. Lineas cargadas: {disponibles}")


def _rango(
    con: sqlite3.Connection,
    desde: Any,
    hasta: Any,
    linea_id: int | None,
    linea_nombre: str | None,
) -> tuple[str | None, str | None, dict | None]:
    """Resuelve el rango efectivo. Sin fechas, usa todo lo cargado."""
    d = _fecha_iso(desde)
    h = _fecha_iso(hasta)
    if desde is not None and d is None:
        return None, None, _no(f"no entiendo la fecha 'desde': {desde!r}. Usa YYYY-MM-DD.")
    if hasta is not None and h is None:
        return None, None, _no(f"no entiendo la fecha 'hasta': {hasta!r}. Usa YYYY-MM-DD.")

    sql = "SELECT MIN(fecha) AS a, MAX(fecha) AS b FROM turnos"
    params: list = []
    if linea_id is not None:
        sql += " WHERE linea_id = ?"
        params.append(linea_id)
    limites = con.execute(sql, params).fetchone()

    if d is None or h is None:
        if limites["a"] is None:
            return None, None, _no(
                f"no hay ningun turno cargado{_ambito(linea_nombre)}; "
                "primero hay que ingerir reportes de turno"
            )
        # Un extremo abierto se cierra contra lo que hay cargado, nunca contra
        # 'hoy': la DB de una planta casi siempre va unos dias atrasada y anclar
        # en hoy devolveria vacio un lunes por la manana.
        if d is None:
            d = min(limites["a"], h) if h else limites["a"]
        if h is None:
            h = max(limites["b"], d)

    if d > h:
        return None, None, _no(f"el rango va al reves: desde {d} es posterior a hasta {h}")
    return d, h, None


def _where(desde: str, hasta: str, linea_id: int | None, alias: str = "t") -> tuple[str, list]:
    cond = [f"{alias}.fecha BETWEEN ? AND ?"]
    params: list = [desde, hasta]
    if linea_id is not None:
        cond.append(f"{alias}.linea_id = ?")
        params.append(linea_id)
    return " AND ".join(cond), params


def _cobertura(con: sqlite3.Connection, desde: str, hasta: str, linea_id: int | None) -> dict:
    """El bloque que hace honesto al agente.

    `turnos_esperados` es una ESTIMACION, no un dato: dias del rango x 3 turnos
    x lineas activas (x1 si se filtro una linea). Asume operacion 7 dias a la
    semana, asi que sobreestima cuando el rango incluye domingos o festivos y la
    planta no programo. Se prefiere ese sesgo: exagerar lo que falta empuja a
    cargar reportes; subestimarlo haria pasar por completo un mes a medias.
    """
    filtro, params = _where(desde, hasta, linea_id)
    encontrados = con.execute(
        f"SELECT COUNT(*) FROM turnos t WHERE {filtro}", params
    ).fetchone()[0]

    if linea_id is not None:
        lineas_consideradas = 1
    else:
        lineas_consideradas = con.execute(
            "SELECT COUNT(*) FROM lineas WHERE activa = 1"
        ).fetchone()[0] or 1

    dias = _dias(desde, hasta)
    esperados = dias * TURNOS_POR_DIA * lineas_consideradas

    sin_paradas = con.execute(
        f"SELECT COUNT(*) FROM paradas p JOIN turnos t ON t.id = p.turno_id "
        f"WHERE {filtro} AND p.causa_id IS NULL", params
    ).fetchone()[0]
    sin_scrap = con.execute(
        f"SELECT COUNT(*) FROM scrap s JOIN turnos t ON t.id = s.turno_id "
        f"WHERE {filtro} AND s.causa_id IS NULL", params
    ).fetchone()[0]

    return {
        "turnos_encontrados": encontrados,
        "turnos_esperados": esperados,
        "sin_clasificar": sin_paradas + sin_scrap,
        "parcial": encontrados < esperados,
        # Contexto para que el agente pueda explicar el numero en vez de solo
        # repetirlo ("12 de 15" no dice nada sin saber de donde sale el 15).
        "dias_del_rango": dias,
        "lineas_consideradas": lineas_consideradas,
        "turnos_esperados_es_estimacion": True,
        "base_estimacion": f"{dias} dias x {TURNOS_POR_DIA} turnos x {lineas_consideradas} linea(s)",
        "sin_clasificar_detalle": {"paradas": sin_paradas, "scrap": sin_scrap},
    }


def _periodo(desde: str, hasta: str, linea_nombre: str | None) -> dict:
    return {
        "desde": desde,
        "hasta": hasta,
        "dias": _dias(desde, hasta),
        "linea": linea_nombre or "todas",
    }


def _filas_paradas(con, desde, hasta, linea_id) -> list[sqlite3.Row]:
    filtro, params = _where(desde, hasta, linea_id)
    return con.execute(
        f"""
        SELECT p.id, p.minutos, p.causa_id, p.causa_texto, p.estacion_id,
               p.confianza, p.revisado,
               c.codigo AS causa_codigo, c.nombre AS causa_nombre,
               c.categoria AS causa_categoria,
               e.nombre AS estacion, l.nombre AS linea, t.linea_id,
               t.fecha, t.turno, t.id AS turno_id
          FROM paradas p
          JOIN turnos t ON t.id = p.turno_id
          LEFT JOIN causas c     ON c.id = p.causa_id
          LEFT JOIN estaciones e ON e.id = p.estacion_id
          LEFT JOIN lineas l     ON l.id = t.linea_id
         WHERE {filtro}
         ORDER BY t.fecha, t.turno, p.id
        """,
        params,
    ).fetchall()


def _filas_scrap(con, desde, hasta, linea_id) -> list[sqlite3.Row]:
    filtro, params = _where(desde, hasta, linea_id)
    return con.execute(
        f"""
        SELECT s.id, s.unidades, s.kg, s.causa_id, s.causa_texto, s.estacion_id,
               s.confianza, s.revisado,
               c.codigo AS causa_codigo, c.nombre AS causa_nombre,
               c.categoria AS causa_categoria,
               e.nombre AS estacion, l.nombre AS linea, t.linea_id,
               t.fecha, t.turno, t.id AS turno_id
          FROM scrap s
          JOIN turnos t ON t.id = s.turno_id
          LEFT JOIN causas c     ON c.id = s.causa_id
          LEFT JOIN estaciones e ON e.id = s.estacion_id
          LEFT JOIN lineas l     ON l.id = t.linea_id
         WHERE {filtro}
         ORDER BY t.fecha, t.turno, s.id
        """,
        params,
    ).fetchall()


def _preparar(con, linea, desde, hasta):
    """Resuelve linea + rango + cobertura, el preambulo de casi toda herramienta.

    Devuelve (contexto, error). El contexto trae ya todo lo que la funcion
    necesita para consultar y para armar el retorno.
    """
    linea_id, linea_nombre, err = _resolver_linea(con, linea)
    if err:
        return None, err
    d, h, err = _rango(con, desde, hasta, linea_id, linea_nombre)
    if err:
        return None, err
    cobertura = _cobertura(con, d, h, linea_id)
    return {
        "linea_id": linea_id,
        "linea_nombre": linea_nombre,
        "desde": d,
        "hasta": h,
        "cobertura": cobertura,
        "periodo": _periodo(d, h, linea_nombre),
    }, None


def _sin_turnos(ctx) -> dict:
    return _no(
        f"no hay turnos cargados{_ambito(ctx['linea_nombre'])} "
        f"entre {ctx['desde']} y {ctx['hasta']}"
    )


# ── 1. Scrap por linea ──────────────────────────────────────────────────────

def scrap_por_linea(linea=None, desde=None, hasta=None) -> dict:
    """Scrap total por linea y desglose por causa, en el rango dado.

    Unidades y kilogramos NO se suman entre si. Son magnitudes distintas y el
    total combinado seria un numero sin significado fisico; se reportan en
    columnas separadas y quien lea decide.
    """
    con, err = _conexion()
    if err:
        return err
    try:
        ctx, err = _preparar(con, linea, desde, hasta)
        if err:
            return err
        if ctx["cobertura"]["turnos_encontrados"] == 0:
            return _sin_turnos(ctx)

        filas = _filas_scrap(con, ctx["desde"], ctx["hasta"], ctx["linea_id"])

        if not filas:
            # Hay turnos pero ninguno registro scrap. No es lo mismo que "no hay
            # datos" (eso ya se descarto arriba) ni que "hubo cero scrap": el
            # extractor deja en NULL lo que el reporte no menciona. Se dice tal
            # cual y el agente decide como redactarlo.
            datos = {
                "lineas": [],
                "total_unidades": None,
                "total_kg": None,
                "registros": 0,
                "nota": (
                    f"Hay {ctx['cobertura']['turnos_encontrados']} turno(s) cargados en el "
                    "rango y ninguno trae registros de scrap. Puede ser cero real o que los "
                    "reportes no lo mencionen; los reportes no lo distinguen."
                ),
            }
            return _si(datos, ctx["cobertura"], ctx["periodo"])

        por_linea: dict[str, dict] = {}
        for f in filas:
            nombre = f["linea"] or "sin linea"
            bloque = por_linea.setdefault(nombre, {"filas": []})
            bloque["filas"].append(f)

        lineas_salida = []
        for nombre, bloque in por_linea.items():
            grupo = bloque["filas"]
            unidades, n_unid = _sumar([f["unidades"] for f in grupo])
            kg, n_kg = _sumar([f["kg"] for f in grupo])
            lineas_salida.append({
                "linea": nombre,
                "unidades": _r(unidades),
                "kg": _r(kg, 3),
                "registros": len(grupo),
                "registros_con_unidades": n_unid,
                "registros_con_kg": n_kg,
                # Filas que llegaron sin ninguna magnitud: existe el evento pero
                # no la cantidad. Contarlas evita que un scrap sin cifra se lea
                # como scrap inexistente.
                "registros_sin_cantidad": sum(
                    1 for f in grupo if f["unidades"] is None and f["kg"] is None
                ),
                "por_causa": _desglose_causas_scrap(grupo),
            })

        # Orden por impacto: primero unidades, luego kg. Nunca alfabetico.
        lineas_salida.sort(key=lambda x: (-(x["unidades"] or 0), -(x["kg"] or 0)))

        total_unidades, _ = _sumar([f["unidades"] for f in filas])
        total_kg, _ = _sumar([f["kg"] for f in filas])

        datos = {
            "lineas": lineas_salida,
            "total_unidades": _r(total_unidades),
            "total_kg": _r(total_kg, 3),
            "registros": len(filas),
            "nota_unidades": (
                "Unidades y kg son magnitudes distintas y no se suman entre si."
            ),
        }
        return _si(datos, ctx["cobertura"], ctx["periodo"])
    finally:
        con.close()


def _desglose_causas_scrap(filas: list[sqlite3.Row]) -> list[dict]:
    """Agrupa por causa; lo no clasificado se agrupa por texto y se marca."""
    salida: list[dict] = []

    clasificadas: dict[int, list] = {}
    sin_clasificar: list = []
    for f in filas:
        if f["causa_id"] is None:
            sin_clasificar.append(f)
        else:
            clasificadas.setdefault(f["causa_id"], []).append(f)

    for grupo in clasificadas.values():
        unidades, _ = _sumar([f["unidades"] for f in grupo])
        kg, _ = _sumar([f["kg"] for f in grupo])
        salida.append({
            "clasificada": True,
            "codigo": grupo[0]["causa_codigo"],
            "causa": grupo[0]["causa_nombre"],
            "categoria": grupo[0]["causa_categoria"],
            "unidades": _r(unidades),
            "kg": _r(kg, 3),
            "registros": len(grupo),
            "estaciones": sorted({f["estacion"] for f in grupo if f["estacion"]}),
        })

    textos = [_normalizar_texto(f["causa_texto"]) for f in sin_clasificar]
    for indices in _clusters(textos):
        grupo = [sin_clasificar[i] for i in indices]
        unidades, _ = _sumar([f["unidades"] for f in grupo])
        kg, _ = _sumar([f["kg"] for f in grupo])
        salida.append({
            "clasificada": False,
            "codigo": None,
            "causa": _representante([f["causa_texto"] for f in grupo]),
            "categoria": None,
            "unidades": _r(unidades),
            "kg": _r(kg, 3),
            "registros": len(grupo),
            "estaciones": sorted({f["estacion"] for f in grupo if f["estacion"]}),
            "textos": [f["causa_texto"] for f in grupo][:MAX_EVIDENCIA],
        })

    salida.sort(key=lambda x: (-(x["unidades"] or 0), -(x["kg"] or 0), -x["registros"]))
    return salida


# ── 2. Pareto de paradas ────────────────────────────────────────────────────

def pareto_paradas(linea=None, desde=None, hasta=None, top=10) -> dict:
    """Top de causas de parada por minutos perdidos, con porcentaje acumulado.

    El acumulado es lo que convierte una lista en un Pareto: sirve para decir
    "tres causas explican el 80% de los minutos", que es una decision, mientras
    que "la causa X tuvo 138 minutos" es solo un dato.

    Las paradas sin clasificar entran al calculo agrupadas por su texto. Si se
    excluyeran, los porcentajes se calcularian sobre un total falso y cada causa
    se veria mas grande de lo que es.
    """
    con, err = _conexion()
    if err:
        return err
    try:
        try:
            top = max(1, int(top))
        except (TypeError, ValueError):
            top = 10

        ctx, err = _preparar(con, linea, desde, hasta)
        if err:
            return err
        if ctx["cobertura"]["turnos_encontrados"] == 0:
            return _sin_turnos(ctx)

        filas = _filas_paradas(con, ctx["desde"], ctx["hasta"], ctx["linea_id"])
        if not filas:
            datos = {
                "causas": [],
                "total_minutos": None,
                "eventos": 0,
                "nota": (
                    f"Hay {ctx['cobertura']['turnos_encontrados']} turno(s) cargados en el "
                    "rango y ninguno registro paradas."
                ),
            }
            return _si(datos, ctx["cobertura"], ctx["periodo"])

        grupos: list[dict] = []

        clasificadas: dict[int, list] = {}
        sin_clasificar: list = []
        for f in filas:
            if f["causa_id"] is None:
                sin_clasificar.append(f)
            else:
                clasificadas.setdefault(f["causa_id"], []).append(f)

        for grupo in clasificadas.values():
            grupos.append(_grupo_pareto(grupo, clasificada=True))
        textos = [_normalizar_texto(f["causa_texto"]) for f in sin_clasificar]
        for indices in _clusters(textos):
            grupos.append(_grupo_pareto([sin_clasificar[i] for i in indices], clasificada=False))

        total_minutos, _ = _sumar([f["minutos"] for f in filas])
        grupos.sort(key=lambda g: (-(g["minutos"] or 0), -g["eventos"]))

        acumulado = 0.0
        for i, g in enumerate(grupos, start=1):
            g["posicion"] = i
            g["porcentaje"] = _pct(g["minutos"], total_minutos)
            acumulado += g["minutos"] or 0.0
            g["acumulado_pct"] = _pct(acumulado, total_minutos)
            # Si ninguna parada trajo duracion no hay acumulado que reportar;
            # un 0.0 aqui se leeria como "no se perdio tiempo".
            g["acumulado_minutos"] = _r(acumulado) if total_minutos is not None else None

        # Cuantas causas hacen falta para llegar al 80% de los minutos. Es la
        # lectura util del Pareto y se calcula aqui para que el modelo no tenga
        # que contar (contar es calcular).
        corte_80 = None
        for g in grupos:
            if g["acumulado_pct"] is not None and g["acumulado_pct"] >= 80.0:
                corte_80 = g["posicion"]
                break

        mostrados = grupos[:top]
        resto = grupos[top:]
        minutos_resto, _ = _sumar([g["minutos"] for g in resto])

        sin_minutos = sum(1 for f in filas if f["minutos"] is None)
        minutos_sin_clasificar, _ = _sumar(
            [f["minutos"] for f in filas if f["causa_id"] is None]
        )

        datos = {
            "causas": mostrados,
            "total_minutos": _r(total_minutos),
            "eventos": len(filas),
            "causas_distintas": len(grupos),
            "top": top,
            "causas_para_80_pct": corte_80,
            "minutos_sin_clasificar": _r(minutos_sin_clasificar),
            "pct_sin_clasificar": _pct(minutos_sin_clasificar, total_minutos),
            "eventos_sin_minutos": sin_minutos,
            "resto": {
                "causas": len(resto),
                "minutos": _r(minutos_resto),
                "porcentaje": _pct(minutos_resto, total_minutos),
            } if resto else None,
        }
        if sin_minutos:
            datos["nota_eventos_sin_minutos"] = (
                f"{sin_minutos} parada(s) quedaron registradas sin duracion; cuentan como "
                "evento pero no aportan minutos, asi que el total esta subestimado."
            )
        return _si(datos, ctx["cobertura"], ctx["periodo"])
    finally:
        con.close()


def _grupo_pareto(grupo: list[sqlite3.Row], clasificada: bool) -> dict:
    minutos, _ = _sumar([f["minutos"] for f in grupo])
    fechas = sorted({f["fecha"] for f in grupo})
    return {
        "clasificada": clasificada,
        "codigo": grupo[0]["causa_codigo"] if clasificada else None,
        "causa": grupo[0]["causa_nombre"] if clasificada
                 else _representante([f["causa_texto"] for f in grupo]),
        "categoria": grupo[0]["causa_categoria"] if clasificada else None,
        "minutos": _r(minutos),
        "eventos": len(grupo),
        "estaciones": sorted({f["estacion"] for f in grupo if f["estacion"]}),
        "lineas": sorted({f["linea"] for f in grupo if f["linea"]}),
        "primera_aparicion": fechas[0],
        "ultima_aparicion": fechas[-1],
        "textos": [f["causa_texto"] for f in grupo][:MAX_EVIDENCIA],
    }


# ── 3. Causas recurrentes (el corazon de la Fase 2) ─────────────────────────

def causas_recurrentes(dias=7, min_repeticiones=3, linea=None) -> dict:
    """Combinaciones causa+estacion que se repiten en la ventana de N dias.

    La ventana se ancla en la ULTIMA fecha con datos, no en la fecha de hoy. Una
    DB de planta casi siempre va unos dias atrasada; anclar en hoy haria que la
    herramienta devolviera vacio un lunes por la manana y el agente concluyera
    que no hay recurrencias, que es lo contrario de la verdad.

    Se agrupa por (tipo, causa, estacion, linea). La linea entra a la clave solo
    para el caso en que la estacion es NULL: con estacion conocida la linea ya
    queda determinada, pero sin ella mezclar lineas juntaria dos maquinas
    distintas bajo una misma "recurrencia".

    Los grupos sin clasificar (causa_id NULL, agrupados por texto) tambien se
    devuelven, marcados con "clasificada": False. Una causa que se repite y que
    nadie clasifico sigue siendo una senal: esconderla porque no tiene codigo
    seria justo el error que este producto existe para evitar.
    """
    con, err = _conexion()
    if err:
        return err
    try:
        try:
            dias = max(1, int(dias))
        except (TypeError, ValueError):
            dias = 7
        try:
            min_repeticiones = max(2, int(min_repeticiones))
        except (TypeError, ValueError):
            min_repeticiones = 3

        linea_id, linea_nombre, err = _resolver_linea(con, linea)
        if err:
            return err

        sql = "SELECT MAX(fecha) AS b FROM turnos"
        params: list = []
        if linea_id is not None:
            sql += " WHERE linea_id = ?"
            params.append(linea_id)
        ultima = con.execute(sql, params).fetchone()["b"]
        if ultima is None:
            return _no(
                f"no hay ningun turno cargado{_ambito(linea_nombre)}; "
                "sin turnos no hay recurrencia que detectar"
            )

        hasta = ultima
        desde = (date.fromisoformat(hasta) - timedelta(days=dias - 1)).isoformat()

        cobertura = _cobertura(con, desde, hasta, linea_id)
        periodo = _periodo(desde, hasta, linea_nombre)
        periodo["ventana_dias"] = dias
        periodo["anclada_en_ultima_fecha_con_datos"] = True

        if cobertura["turnos_encontrados"] == 0:
            return _no(
                f"no hay turnos cargados{_ambito(linea_nombre)} entre {desde} y {hasta}"
            )

        paradas = _filas_paradas(con, desde, hasta, linea_id)
        scrap = _filas_scrap(con, desde, hasta, linea_id)

        grupos: list[dict] = []
        grupos += _grupos_recurrencia(paradas, tipo="parada")
        grupos += _grupos_recurrencia(scrap, tipo="scrap")

        evaluados = len(grupos)
        recurrentes = [g for g in grupos if g["repeticiones"] >= min_repeticiones]

        # Orden por impacto. Los grupos de scrap no tienen minutos, asi que
        # quedan despues de los de parada y se ordenan entre si por unidades:
        # comparar minutos con unidades no significa nada.
        recurrentes.sort(
            key=lambda g: (
                -(g["minutos_totales"] or 0),
                -(g["unidades_totales"] or 0),
                -(g["kg_totales"] or 0),
                -g["repeticiones"],
            )
        )

        datos = {
            # La lista se llama "recurrentes" (y no "grupos") porque es la clave
            # por la que reporte.py encuentra las filas de un resultado sin
            # conocer cada herramienta. Alinearse con esa convencion sale gratis.
            "recurrentes": recurrentes,
            "total_recurrentes": len(recurrentes),
            "grupos_evaluados": evaluados,
            "min_repeticiones": min_repeticiones,
            "sin_clasificar_entre_recurrentes": sum(
                1 for g in recurrentes if not g["clasificada"]
            ),
            "orden": (
                "minutos perdidos desc; los grupos de scrap, que no tienen minutos, "
                "van despues ordenados por unidades"
            ),
        }
        if not recurrentes:
            datos["nota"] = (
                f"Se revisaron {evaluados} combinaciones causa-estacion en los ultimos "
                f"{dias} dias y ninguna llego a {min_repeticiones} repeticiones. "
                "No es falta de datos: es que no hay recurrencia en esa ventana."
            )
        return _si(datos, cobertura, periodo)
    finally:
        con.close()


def _grupos_recurrencia(filas: list[sqlite3.Row], tipo: str) -> list[dict]:
    """Arma los grupos (causa, estacion, linea) de una tabla de hechos."""
    es_parada = tipo == "parada"

    clasificadas: dict[tuple, list] = {}
    sin_clasificar: dict[tuple, list] = {}
    for f in filas:
        cubo = (f["estacion_id"], f["linea_id"])
        if f["causa_id"] is None:
            sin_clasificar.setdefault(cubo, []).append(f)
        else:
            clasificadas.setdefault((f["causa_id"], *cubo), []).append(f)

    grupos = [_grupo_recurrencia(g, tipo, True, es_parada) for g in clasificadas.values()]

    for cubo, pendientes in sin_clasificar.items():
        textos = [_normalizar_texto(f["causa_texto"]) for f in pendientes]
        for indices in _clusters(textos):
            grupo = [pendientes[i] for i in indices]
            grupos.append(_grupo_recurrencia(grupo, tipo, False, es_parada))
    return grupos


def _grupo_recurrencia(grupo, tipo, clasificada, es_parada) -> dict:
    fechas = sorted({f["fecha"] for f in grupo})
    minutos = _sumar([f["minutos"] for f in grupo])[0] if es_parada else None
    unidades = None if es_parada else _sumar([f["unidades"] for f in grupo])[0]
    kg = None if es_parada else _sumar([f["kg"] for f in grupo])[0]
    turnos = sorted({(f["fecha"], f["turno"]) for f in grupo})

    return {
        "tipo": tipo,
        "clasificada": clasificada,
        "causa": {
            "codigo": grupo[0]["causa_codigo"],
            "nombre": grupo[0]["causa_nombre"],
            "categoria": grupo[0]["causa_categoria"],
        } if clasificada else None,
        "causa_texto": _representante([f["causa_texto"] for f in grupo]),
        "estacion": grupo[0]["estacion"],
        "linea": grupo[0]["linea"],
        "repeticiones": len(grupo),
        "minutos_totales": _r(minutos),
        "unidades_totales": _r(unidades),
        "kg_totales": _r(kg, 3),
        "primera_aparicion": fechas[0],
        "ultima_aparicion": fechas[-1],
        "turnos_afectados": fechas,
        "turnos_afectados_detalle": [
            {"fecha": fe, "turno": tu} for fe, tu in turnos
        ],
        "textos": [f["causa_texto"] for f in grupo][:MAX_EVIDENCIA],
        "sin_revisar": sum(1 for f in grupo if not f["revisado"]),
    }


# ── 4. Produccion vs plan ───────────────────────────────────────────────────

def produccion_vs_plan(linea=None, desde=None, hasta=None) -> dict:
    """Plan, real, cumplimiento y minutos de parada del mismo rango.

    Los minutos de parada van en la misma respuesta a proposito: el cumplimiento
    solo, sin las paradas al lado, invita a explicar el faltante con lo primero
    que se le ocurra a alguien. Con las dos cifras juntas se puede correlacionar
    (y sigue siendo correlacion, no causa: eso lo dice el prompt del agente).
    """
    con, err = _conexion()
    if err:
        return err
    try:
        ctx, err = _preparar(con, linea, desde, hasta)
        if err:
            return err
        if ctx["cobertura"]["turnos_encontrados"] == 0:
            return _sin_turnos(ctx)

        filtro, params = _where(ctx["desde"], ctx["hasta"], ctx["linea_id"])
        turnos = con.execute(
            f"""
            SELECT t.id, t.fecha, t.turno, t.linea_id, t.unidades_plan,
                   t.unidades_producidas, t.minutos_turno, l.nombre AS linea
              FROM turnos t
              LEFT JOIN lineas l ON l.id = t.linea_id
             WHERE {filtro}
             ORDER BY t.fecha, t.turno
            """,
            params,
        ).fetchall()

        # Minutos de parada por turno, en SQL, para no traer todas las filas.
        paradas = {
            f["turno_id"]: (f["minutos"], f["eventos"])
            for f in con.execute(
                f"""
                SELECT p.turno_id, SUM(p.minutos) AS minutos, COUNT(*) AS eventos
                  FROM paradas p JOIN turnos t ON t.id = p.turno_id
                 WHERE {filtro}
                 GROUP BY p.turno_id
                """,
                params,
            ).fetchall()
        }

        def bloque(subconjunto: list, etiqueta: str | None = None) -> dict:
            plan, n_plan = _sumar([t["unidades_plan"] for t in subconjunto])
            real, n_real = _sumar([t["unidades_producidas"] for t in subconjunto])
            minutos_turno, _ = _sumar([t["minutos_turno"] for t in subconjunto])
            minutos_parada, _ = _sumar(
                [paradas.get(t["id"], (None, 0))[0] for t in subconjunto]
            )
            eventos = sum(paradas.get(t["id"], (None, 0))[1] for t in subconjunto)

            # El cumplimiento se calcula SOLO sobre los turnos que traen las dos
            # cifras. Dividir toda la produccion entre el plan de los turnos que
            # si lo tienen da cumplimientos por encima del 100% que no existen:
            # el numerador incluye turnos que el denominador no cuenta.
            comparables = [
                t for t in subconjunto
                if t["unidades_plan"] is not None and t["unidades_producidas"] is not None
            ]
            plan_base, _ = _sumar([t["unidades_plan"] for t in comparables])
            real_base, _ = _sumar([t["unidades_producidas"] for t in comparables])

            resultado = {
                "unidades_plan": _r(plan),
                "unidades_producidas": _r(real),
                "cumplimiento_pct": _pct(real_base, plan_base),
                "diferencia": _r(
                    None if (plan_base is None or real_base is None)
                    else real_base - plan_base
                ),
                "base_cumplimiento": {
                    "turnos": len(comparables),
                    "unidades_plan": _r(plan_base),
                    "unidades_producidas": _r(real_base),
                },
                "turnos": len(subconjunto),
                "turnos_con_plan": n_plan,
                "turnos_con_produccion": n_real,
                "minutos_parada": _r(minutos_parada),
                "eventos_parada": eventos,
                "minutos_turno": _r(minutos_turno),
                "disponibilidad_pct": _pct(
                    None if minutos_turno is None
                    else minutos_turno - (minutos_parada or 0.0),
                    minutos_turno,
                ),
            }
            if etiqueta is not None:
                resultado["linea"] = etiqueta
            if len(comparables) < len(subconjunto):
                resultado["nota_plan"] = (
                    f"{len(subconjunto) - len(comparables)} de {len(subconjunto)} turnos no "
                    "traen plan o produccion; el cumplimiento y la diferencia salen solo de "
                    f"los {len(comparables)} turnos que traen ambas cifras."
                )
            return resultado

        por_linea_map: dict[str, list] = {}
        for t in turnos:
            por_linea_map.setdefault(t["linea"] or "sin linea", []).append(t)
        por_linea = [bloque(v, k) for k, v in por_linea_map.items()]
        por_linea.sort(key=lambda x: -(x["unidades_producidas"] or 0))

        # La serie diaria va recortada a lo que sirve para ver la tendencia. Un
        # rango de un mes son 30 filas y este resultado lo termina leyendo un
        # modelo de 7B con 8k de contexto: cada campo de mas en la serie es
        # contexto que se le quita al resto de la evidencia.
        por_dia_map: dict[str, list] = {}
        for t in turnos:
            por_dia_map.setdefault(t["fecha"], []).append(t)
        por_dia = []
        for fecha in sorted(por_dia_map):
            b = bloque(por_dia_map[fecha])
            por_dia.append({
                "fecha": fecha,
                "unidades_plan": b["unidades_plan"],
                "unidades_producidas": b["unidades_producidas"],
                "cumplimiento_pct": b["cumplimiento_pct"],
                "minutos_parada": b["minutos_parada"],
                "eventos_parada": b["eventos_parada"],
                "turnos": b["turnos"],
            })

        datos = {
            "total": bloque(turnos),
            "por_linea": por_linea,
            "por_dia": por_dia,
        }
        return _si(datos, ctx["cobertura"], ctx["periodo"])
    finally:
        con.close()


# ── 5. Comparacion de periodos ──────────────────────────────────────────────

# (clave, etiqueta legible, unidad). El orden es el de lectura del supervisor:
# primero lo que salio, despues lo que costo.
_METRICAS = [
    ("unidades_plan", "Unidades planeadas", "unidades"),
    ("unidades_producidas", "Unidades producidas", "unidades"),
    ("cumplimiento_pct", "Cumplimiento del plan", "%"),
    ("minutos_parada", "Minutos de parada", "minutos"),
    ("eventos_parada", "Eventos de parada", "eventos"),
    ("scrap_unidades", "Scrap en unidades", "unidades"),
    ("scrap_kg", "Scrap en kg", "kg"),
    ("turnos", "Turnos con datos", "turnos"),
]


def _metricas(con, desde: str, hasta: str, linea_id: int | None) -> dict:
    """Las mismas ocho cifras para cualquier rango. Base de la comparacion."""
    filtro, params = _where(desde, hasta, linea_id)
    t = con.execute(
        f"""
        SELECT COUNT(*) AS turnos,
               SUM(t.unidades_plan) AS plan,
               COUNT(t.unidades_plan) AS n_plan,
               SUM(t.unidades_producidas) AS producido,
               COUNT(t.unidades_producidas) AS n_producido,
               -- Base del cumplimiento: solo turnos con plan Y produccion. Ver
               -- el comentario de produccion_vs_plan: mezclar bases da
               -- cumplimientos de 109% que no existieron.
               SUM(CASE WHEN t.unidades_plan IS NOT NULL
                         AND t.unidades_producidas IS NOT NULL
                        THEN t.unidades_plan END) AS plan_base,
               SUM(CASE WHEN t.unidades_plan IS NOT NULL
                         AND t.unidades_producidas IS NOT NULL
                        THEN t.unidades_producidas END) AS producido_base
          FROM turnos t WHERE {filtro}
        """,
        params,
    ).fetchone()
    p = con.execute(
        f"""
        SELECT SUM(p.minutos) AS minutos, COUNT(*) AS eventos
          FROM paradas p JOIN turnos t ON t.id = p.turno_id WHERE {filtro}
        """,
        params,
    ).fetchone()
    s = con.execute(
        f"""
        SELECT SUM(s.unidades) AS unidades, SUM(s.kg) AS kg, COUNT(*) AS registros
          FROM scrap s JOIN turnos t ON t.id = s.turno_id WHERE {filtro}
        """,
        params,
    ).fetchone()

    plan = t["plan"]
    producido = t["producido"]
    return {
        "unidades_plan": _r(plan),
        "unidades_producidas": _r(producido),
        "cumplimiento_pct": _pct(t["producido_base"], t["plan_base"]),
        "minutos_parada": _r(p["minutos"]),
        "eventos_parada": p["eventos"] or 0,
        "scrap_unidades": _r(s["unidades"]),
        "scrap_kg": _r(s["kg"], 3),
        "turnos": t["turnos"],
        "_registros_scrap": s["registros"] or 0,
    }


def comparar_periodos(desde_a, hasta_a, desde_b, hasta_b, linea=None) -> dict:
    """Mismas metricas en dos rangos, con variacion absoluta y porcentual.

    Si un periodo tiene cobertura parcial y el otro no, o si tienen distinta
    cantidad de dias, se dice en la cobertura con una advertencia explicita.
    Comparar un mes completo contra medio mes y reportar "-40%" es la clase de
    cifra que hace que alguien tome una decision sobre nada.
    """
    con, err = _conexion()
    if err:
        return err
    try:
        linea_id, linea_nombre, err = _resolver_linea(con, linea)
        if err:
            return err

        rangos = {}
        for etiqueta, (d, h) in {"a": (desde_a, hasta_a), "b": (desde_b, hasta_b)}.items():
            di, hi = _fecha_iso(d), _fecha_iso(h)
            if di is None or hi is None:
                return _no(
                    f"el periodo {etiqueta.upper()} tiene fechas que no entiendo "
                    f"({d!r}, {h!r}). Usa YYYY-MM-DD; ambos extremos son obligatorios."
                )
            if di > hi:
                return _no(f"el periodo {etiqueta.upper()} va al reves: {di} es posterior a {hi}")
            rangos[etiqueta] = (di, hi)

        cob_a = _cobertura(con, *rangos["a"], linea_id)
        cob_b = _cobertura(con, *rangos["b"], linea_id)

        if cob_a["turnos_encontrados"] == 0 or cob_b["turnos_encontrados"] == 0:
            vacios = [
                f"{k.upper()} ({rangos[k][0]} a {rangos[k][1]})"
                for k, c in (("a", cob_a), ("b", cob_b))
                if c["turnos_encontrados"] == 0
            ]
            return _no(
                f"no hay turnos cargados{_ambito(linea_nombre)} en el periodo "
                + " ni en el periodo ".join(vacios)
                + "; sin datos en los dos lados no hay comparacion posible"
            )

        met_a = _metricas(con, *rangos["a"], linea_id)
        met_b = _metricas(con, *rangos["b"], linea_id)

        comparacion = []
        for clave, etiqueta, unidad in _METRICAS:
            va, vb = met_a[clave], met_b[clave]
            fila = {
                "metrica": clave,
                "etiqueta": etiqueta,
                "unidad": unidad,
                "a": va,
                "b": vb,
                "variacion": None,
                "variacion_pct": None,
            }
            if va is None or vb is None:
                falta = "A" if va is None else "B"
                fila["motivo"] = (
                    f"sin dato en el periodo {falta}; no se resta contra un vacio"
                )
            else:
                fila["variacion"] = _r(vb - va)
                if unidad == "%":
                    # Variar un porcentaje en porcentaje no significa nada util;
                    # se reporta en puntos porcentuales y se dice.
                    fila["variacion_puntos_pct"] = _r(vb - va)
                    fila["nota"] = "la variacion va en puntos porcentuales"
                elif va == 0:
                    fila["motivo"] = "el periodo A es cero: la variacion porcentual no existe"
                else:
                    fila["variacion_pct"] = _pct(vb - va, abs(va))
            comparacion.append(fila)

        dias_a, dias_b = _dias(*rangos["a"]), _dias(*rangos["b"])
        advertencias = []
        if dias_a != dias_b:
            advertencias.append(
                f"los periodos no son del mismo largo: A tiene {dias_a} dias y B {dias_b}. "
                "Las variaciones absolutas no son comparables directamente."
            )
        if cob_a["parcial"] != cob_b["parcial"]:
            parcial, completo = ("A", "B") if cob_a["parcial"] else ("B", "A")
            flaco = cob_a if parcial == "A" else cob_b
            advertencias.append(
                f"el periodo {parcial} tiene cobertura parcial "
                f"({flaco['turnos_encontrados']} de {flaco['turnos_esperados']} turnos "
                f"estimados) y el {completo} no. La diferencia puede ser de reportes que "
                "faltan por cargar, no de lo que paso en la planta."
            )
        elif cob_a["parcial"] and cob_b["parcial"]:
            advertencias.append(
                "los dos periodos tienen cobertura parcial; las cifras son sobre lo cargado, "
                "no sobre lo que produjo la planta."
            )

        cobertura = {
            "turnos_encontrados": cob_a["turnos_encontrados"] + cob_b["turnos_encontrados"],
            "turnos_esperados": cob_a["turnos_esperados"] + cob_b["turnos_esperados"],
            "sin_clasificar": cob_a["sin_clasificar"] + cob_b["sin_clasificar"],
            "parcial": cob_a["parcial"] or cob_b["parcial"],
            "periodo_a": cob_a,
            "periodo_b": cob_b,
            "comparables": not advertencias,
            "advertencias": advertencias,
        }
        periodo = {
            "a": _periodo(*rangos["a"], linea_nombre),
            "b": _periodo(*rangos["b"], linea_nombre),
            "desde": min(rangos["a"][0], rangos["b"][0]),
            "hasta": max(rangos["a"][1], rangos["b"][1]),
            "linea": linea_nombre or "todas",
        }
        datos = {
            "comparacion": comparacion,
            "periodo_a": {k: v for k, v in met_a.items() if not k.startswith("_")},
            "periodo_b": {k: v for k, v in met_b.items() if not k.startswith("_")},
        }
        return _si(datos, cobertura, periodo)
    finally:
        con.close()


# ── 6. Impacto en costo ─────────────────────────────────────────────────────

_MOTIVO_SIN_TARIFAS = (
    "no hay tarifas cargadas en la tabla costos; cargar COP/minuto de parada y "
    "COP/unidad de scrap"
)

_PREFIJO_MINUTO = "cop_por_minuto_parada"
_PREFIJO_UNIDAD = "cop_por_unidad_scrap"
_PREFIJO_KG = "cop_por_kg_scrap"


def _tarifas_vigentes(con, fecha_corte: str) -> dict[str, dict]:
    """Ultima tarifa por clave con vigente_desde <= fecha_corte.

    Se toma la vigente al CIERRE del rango. Si una tarifa cambio a mitad del
    periodo, el costeo usa la nueva para todo el rango; se expone
    `vigente_desde` en la respuesta para que el agente lo pueda decir en vez de
    presentar la cifra como exacta.
    """
    filas = con.execute(
        "SELECT clave, valor, unidad, vigente_desde, fuente FROM costos "
        "WHERE vigente_desde <= ? ORDER BY clave, vigente_desde",
        (fecha_corte,),
    ).fetchall()
    vigentes: dict[str, dict] = {}
    for f in filas:
        vigentes[f["clave"]] = dict(f)  # orden ascendente: la ultima gana
    return vigentes


def _tarifa(vigentes: dict[str, dict], prefijo: str, linea_nombre: str | None):
    """Busca tarifa especifica de linea, luego generica, luego unica del prefijo.

    Devuelve (tarifa | None, motivo_si_no). No se promedia entre varias tarifas
    candidatas: promediar tarifas es inventar una que nadie cargo.
    """
    indice = {k.lower(): v for k, v in vigentes.items()}
    if linea_nombre:
        exacta = indice.get(f"{prefijo}_{linea_nombre}".lower())
        if exacta:
            return exacta, None
    generica = indice.get(prefijo.lower())
    if generica:
        return generica, None
    candidatas = [v for k, v in indice.items() if k.startswith(prefijo.lower() + "_")]
    if len(candidatas) == 1:
        return candidatas[0], None
    if not candidatas:
        return None, f"no hay ninguna tarifa '{prefijo}' cargada"
    claves = ", ".join(sorted(c["clave"] for c in candidatas))
    return None, (
        f"hay {len(candidatas)} tarifas '{prefijo}' y ninguna aplica a "
        f"{linea_nombre or 'esta linea'} ni es generica ({claves}); "
        "no se elige una por cuenta propia"
    )


def impacto_costo(desde=None, hasta=None, linea=None) -> dict:
    """Valoriza minutos de parada y scrap con las tarifas de la tabla costos.

    Si la tabla esta vacia devuelve `disponible: False`. No hay tarifa por
    defecto en el codigo y no la va a haber: un COP/minuto inventado se propaga
    al resumen ejecutivo y nadie vuelve a preguntar de donde salio.
    """
    con, err = _conexion()
    if err:
        return err
    try:
        cargadas = con.execute("SELECT COUNT(*) FROM costos").fetchone()[0]
        if not cargadas:
            return _no(_MOTIVO_SIN_TARIFAS)

        ctx, err = _preparar(con, linea, desde, hasta)
        if err:
            return err
        if ctx["cobertura"]["turnos_encontrados"] == 0:
            return _sin_turnos(ctx)

        vigentes = _tarifas_vigentes(con, ctx["hasta"])
        if not vigentes:
            primera = con.execute("SELECT MIN(vigente_desde) FROM costos").fetchone()[0]
            return _no(
                f"hay {cargadas} tarifas cargadas pero ninguna vigente al {ctx['hasta']}; "
                f"la mas antigua rige desde {primera}"
            )

        paradas = _filas_paradas(con, ctx["desde"], ctx["hasta"], ctx["linea_id"])
        scrap = _filas_scrap(con, ctx["desde"], ctx["hasta"], ctx["linea_id"])

        # Se costea por linea porque las tarifas son por linea: un minuto de L1
        # no vale lo mismo que uno de L4.
        lineas: dict[str, dict] = {}
        for f in paradas:
            lineas.setdefault(f["linea"] or "sin linea", {"paradas": [], "scrap": []})["paradas"].append(f)
        for f in scrap:
            lineas.setdefault(f["linea"] or "sin linea", {"paradas": [], "scrap": []})["scrap"].append(f)

        salida_lineas = []
        no_costeado = []
        tarifas_usadas: dict[str, dict] = {}
        total = 0.0
        algo_costeado = False

        for nombre, bloques in lineas.items():
            nombre_tarifa = nombre if nombre != "sin linea" else None
            componentes = []
            subtotal = 0.0

            minutos, _ = _sumar([f["minutos"] for f in bloques["paradas"]])
            unidades, _ = _sumar([f["unidades"] for f in bloques["scrap"]])
            kilos, _ = _sumar([f["kg"] for f in bloques["scrap"]])

            for cantidad, unidad, prefijo, concepto in (
                (minutos, "minutos", _PREFIJO_MINUTO, "minutos de parada"),
                (unidades, "unidades", _PREFIJO_UNIDAD, "scrap en unidades"),
                (kilos, "kg", _PREFIJO_KG, "scrap en kg"),
            ):
                if cantidad is None:
                    continue
                tarifa, motivo = _tarifa(vigentes, prefijo, nombre_tarifa)
                if tarifa is None:
                    no_costeado.append({
                        "linea": nombre,
                        "concepto": concepto,
                        "cantidad": _r(cantidad, 3),
                        "unidad": unidad,
                        "motivo": motivo,
                    })
                    continue
                costo = cantidad * float(tarifa["valor"])
                subtotal += costo
                algo_costeado = True
                tarifas_usadas[tarifa["clave"]] = {
                    "clave": tarifa["clave"],
                    "valor": tarifa["valor"],
                    "unidad": tarifa["unidad"],
                    "vigente_desde": tarifa["vigente_desde"],
                    "fuente": tarifa["fuente"],
                }
                componentes.append({
                    "concepto": concepto,
                    "cantidad": _r(cantidad, 3),
                    "unidad": unidad,
                    "tarifa_cop": tarifa["valor"],
                    "clave_tarifa": tarifa["clave"],
                    "vigente_desde": tarifa["vigente_desde"],
                    "fuente_tarifa": tarifa["fuente"],
                    "costo_cop": round(costo),
                })

            total += subtotal
            # Si no se pudo costear nada de esta linea, el costo es None y no 0:
            # un cero aqui se leeria como "L1 no costo nada", cuando lo que pasa
            # es que falta la tarifa. El detalle ya quedo en `no_costeado`.
            fila_linea = {
                "linea": nombre,
                "costo_cop": round(subtotal) if componentes else None,
                "componentes": componentes,
            }
            if not componentes:
                fila_linea["motivo"] = "ningun concepto de esta linea tiene tarifa aplicable"
            salida_lineas.append(fila_linea)

        if not algo_costeado:
            return _no(
                "hay tarifas cargadas pero ninguna aplica a los datos de este rango: "
                + "; ".join(dict.fromkeys(x["motivo"] for x in no_costeado))
            )

        salida_lineas.sort(key=lambda x: -(x["costo_cop"] or 0))

        datos = {
            "total_cop": round(total),
            "por_linea": salida_lineas,
            "top_causas_por_costo": _costo_por_causa(paradas, vigentes),
            "tarifas_aplicadas": list(tarifas_usadas.values()),
            "no_costeado": no_costeado,
        }
        if no_costeado:
            datos["nota_cobertura_costo"] = (
                "El total NO incluye los conceptos listados en 'no_costeado': falta la "
                "tarifa correspondiente. El costo real es mayor que esta cifra."
            )
        return _si(datos, ctx["cobertura"], ctx["periodo"])
    finally:
        con.close()


def _costo_por_causa(paradas: list[sqlite3.Row], vigentes: dict[str, dict]) -> list[dict]:
    """Minutos de parada valorizados y agrupados por causa, top 5.

    Solo paradas: el scrap se costea con tarifas por unidad o kg que no se
    pueden repartir por causa sin conocer el material, y estimarlo seria
    inventar.
    """
    costeables: list[tuple[sqlite3.Row, float]] = []
    for f in paradas:
        if f["minutos"] is None:
            continue
        tarifa, _ = _tarifa(vigentes, _PREFIJO_MINUTO, f["linea"])
        if tarifa is None:
            continue
        costeables.append((f, float(f["minutos"]) * float(tarifa["valor"])))

    clasificadas: dict[str, list] = {}
    sueltas: list[tuple[sqlite3.Row, float]] = []
    for par in costeables:
        if par[0]["causa_id"] is None:
            sueltas.append(par)
        else:
            clasificadas.setdefault(par[0]["causa_codigo"], []).append(par)

    def fila(items: list, clasificada: bool) -> dict:
        minutos, _ = _sumar([f["minutos"] for f, _ in items])
        return {
            "clasificada": clasificada,
            "codigo": items[0][0]["causa_codigo"] if clasificada else None,
            "causa": items[0][0]["causa_nombre"] if clasificada
                     else _representante([f["causa_texto"] for f, _ in items]),
            "minutos": _r(minutos),
            "eventos": len(items),
            "costo_cop": round(sum(c for _, c in items)),
        }

    filas = [fila(items, True) for items in clasificadas.values()]
    # Las sin clasificar se agrupan con el mismo criterio que el resto del
    # modulo: si en el Pareto son un solo problema, aqui no pueden salir como
    # dos costos distintos.
    textos = [_normalizar_texto(f["causa_texto"]) for f, _ in sueltas]
    for indices in _clusters(textos):
        filas.append(fila([sueltas[i] for i in indices], False))

    return sorted(filas, key=lambda x: -x["costo_cop"])[:5]


# ── 7. Estado de los datos ──────────────────────────────────────────────────

def estado_datos(desde=None, hasta=None) -> dict:
    """Que hay cargado, de que lineas, y que quedo pendiente de revisar.

    Es la herramienta que el agente usa ANTES de prometer una respuesta: sirve
    para saber que puede contestar y que no. Tambien es la que sostiene las
    frases del tipo "de L4 no tengo nada cargado esta semana", que valen mas que
    un numero inventado.
    """
    con, err = _conexion()
    if err:
        return err
    try:
        totales = con.execute(
            "SELECT COUNT(*) AS turnos, MIN(fecha) AS a, MAX(fecha) AS b FROM turnos"
        ).fetchone()
        documentos = con.execute("SELECT COUNT(*) FROM documentos").fetchone()[0]

        if not totales["turnos"]:
            extra = (
                f" Hay {documentos} documento(s) registrados que no produjeron ningun "
                "turno: revisar la extraccion."
                if documentos else ""
            )
            return _no(
                "la base no tiene ningun turno cargado; hay que ingerir reportes de turno "
                "antes de poder responder nada numerico." + extra
            )

        d = _fecha_iso(desde) or totales["a"]
        h = _fecha_iso(hasta) or totales["b"]
        if d > h:
            return _no(f"el rango va al reves: desde {d} es posterior a hasta {h}")

        cobertura = _cobertura(con, d, h, None)
        periodo = _periodo(d, h, None)
        filtro, params = _where(d, h, None)

        por_linea = [dict(f) for f in con.execute(
            f"""
            SELECT l.nombre AS linea, l.activa,
                   COUNT(t.id) AS turnos,
                   MIN(t.fecha) AS primera, MAX(t.fecha) AS ultima,
                   COUNT(DISTINCT t.fecha) AS dias_con_datos
              FROM lineas l
              LEFT JOIN turnos t ON t.linea_id = l.id AND t.fecha BETWEEN ? AND ?
             GROUP BY l.id
             ORDER BY l.id
            """,
            [d, h],
        ).fetchall()]

        # Hechos por linea, en consultas aparte: meterlos al LEFT JOIN de arriba
        # multiplicaria las filas y el COUNT de turnos saldria inflado.
        for tabla, campo in (("paradas", "paradas"), ("scrap", "scrap"), ("calidad", "calidad")):
            conteos = {
                f["linea"]: f["n"] for f in con.execute(
                    f"""
                    SELECT l.nombre AS linea, COUNT(*) AS n
                      FROM {tabla} x
                      JOIN turnos t ON t.id = x.turno_id
                      LEFT JOIN lineas l ON l.id = t.linea_id
                     WHERE {filtro}
                     GROUP BY l.nombre
                    """,
                    params,
                ).fetchall()
            }
            for fila in por_linea:
                fila[campo] = conteos.get(fila["linea"], 0)

        sin_datos = [f["linea"] for f in por_linea if f["activa"] and not f["turnos"]]

        pendientes = {}
        for tabla in ("paradas", "scrap", "calidad"):
            pendientes[tabla] = con.execute(
                f"SELECT COUNT(*) FROM {tabla} x JOIN turnos t ON t.id = x.turno_id "
                f"WHERE {filtro} AND x.revisado = 0", params
            ).fetchone()[0]
        pendientes["total"] = sum(pendientes.values())

        baja_confianza = sum(
            con.execute(
                f"SELECT COUNT(*) FROM {tabla} x JOIN turnos t ON t.id = x.turno_id "
                f"WHERE {filtro} AND x.confianza < 0.7", params
            ).fetchone()[0]
            for tabla in ("paradas", "scrap", "calidad")
        )

        # Dias del rango sin ningun turno: es la forma concreta de la cobertura
        # parcial, la que le permite al agente decir que dias faltan cargar.
        con_datos = {f[0] for f in con.execute(
            f"SELECT DISTINCT t.fecha FROM turnos t WHERE {filtro}", params
        ).fetchall()}
        inicio = date.fromisoformat(d)
        faltantes = [
            (inicio + timedelta(days=i)).isoformat()
            for i in range(_dias(d, h))
            if (inicio + timedelta(days=i)).isoformat() not in con_datos
        ]

        ultimo_doc = con.execute(
            "SELECT ruta, formato, cargado_en FROM documentos ORDER BY id DESC LIMIT 1"
        ).fetchone()
        tarifas = con.execute("SELECT COUNT(*) FROM costos").fetchone()[0]

        datos = {
            "rango_cargado": {
                "desde": totales["a"],
                "hasta": totales["b"],
                "turnos": totales["turnos"],
            },
            "en_el_rango_consultado": {
                "turnos": cobertura["turnos_encontrados"],
                "dias_con_datos": len(con_datos),
                "dias_sin_datos": len(faltantes),
                "fechas_sin_datos": faltantes[:15],
                "fechas_sin_datos_truncadas": len(faltantes) > 15,
            },
            "por_linea": por_linea,
            "lineas_activas_sin_datos": sin_datos,
            "pendientes_revision": pendientes,
            "registros_baja_confianza": baja_confianza,
            "sin_clasificar": cobertura["sin_clasificar_detalle"],
            "documentos_cargados": documentos,
            "ultimo_documento": dict(ultimo_doc) if ultimo_doc else None,
            "tarifas_de_costo_cargadas": tarifas,
            # Lo que el agente necesita saber antes de prometer una cifra en pesos.
            "puede_costear": tarifas > 0,
        }
        return _si(datos, cobertura, periodo)
    finally:
        con.close()


# ── 8. Despacho ─────────────────────────────────────────────────────────────

DISPONIBLES = {
    "scrap_por_linea": scrap_por_linea,
    "pareto_paradas": pareto_paradas,
    "causas_recurrentes": causas_recurrentes,
    "produccion_vs_plan": produccion_vs_plan,
    "comparar_periodos": comparar_periodos,
    "impacto_costo": impacto_costo,
    "estado_datos": estado_datos,
}


def ejecutar(nombre: str, argumentos: dict) -> dict:
    """Ejecuta una herramienta por nombre y no deja que nada tumbe el loop.

    Una excepcion aqui adentro sale como `disponible: False` con el motivo: el
    agente puede decir "esa consulta fallo" y seguir, que es infinitamente mejor
    que un traceback en la cara del supervisor. El tipo de excepcion va en el
    motivo porque sin el, depurar en planta es imposible.
    """
    fn = DISPONIBLES.get(nombre)
    if fn is None:
        conocidas = ", ".join(sorted(DISPONIBLES))
        return _no(f"herramienta desconocida: '{nombre}'. Disponibles: {conocidas}")
    if not isinstance(argumentos, dict):
        argumentos = {}
    try:
        resultado = fn(**argumentos)
    except TypeError as e:
        # Casi siempre es el modelo mandando un argumento que no existe. Se
        # nombra el error para que el agente pueda reintentar bien.
        return _no(f"error interno: argumentos invalidos para {nombre}: {e}")
    except Exception as e:  # noqa: BLE001
        return _no(f"error interno: {nombre} fallo con {type(e).__name__}: {e}")

    if not isinstance(resultado, dict) or "disponible" not in resultado:
        return _no(f"error interno: {nombre} devolvio algo que no cumple el contrato")
    return resultado
