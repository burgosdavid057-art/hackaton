"""
Mapeo de texto libre a la taxonomia canonica. Determinista, sin LLM.

El extractor devuelve lo que el supervisor escribio a las 2 a.m.: "se trabo la
remachadora", "R-02", "no llego material". Este modulo lo convierte en
`causa_id`, `estacion_id` y `linea_id`. Nada de esto pasa por un modelo: es
aritmetica de cadenas, y por eso el mismo reporte cargado dos veces produce
exactamente la misma clasificacion.

Tres pasos para una causa, el primero que acierta gana:

  1. El texto ES un codigo canonico ("PAR-MEC-02")            -> confianza 1.0
  2. Un alias esta contenido en el texto (o el texto en el alias) -> 0.9
  3. Similitud de cadenas/tokens contra nombre + alias        -> el ratio

Por debajo de UMBRAL (0.72) devuelve `(None, 0.0)` y la fila queda pendiente de
revision. Es deliberado que no adivine: una causa mal clasificada no se nota
como error, se nota como una recurrencia que no existe o como una que se
diluyo. La deteccion de patrones recurrentes es el producto; contaminarla con
suposiciones lo vacia.

Los catalogos se leen de la DB una sola vez por proceso (`lru_cache`), porque
`normalizar()` corre una vez por fila y no puede abrir la DB en cada una. Si
alguien recarga la taxonomia con `db.inicializar(forzar=True)` en el mismo
proceso, tiene que llamar `refrescar_catalogos()`.
"""

from __future__ import annotations

import copy
import json
import re
import unicodedata
from collections import Counter
from difflib import SequenceMatcher
from functools import lru_cache
from typing import Any, NamedTuple

from . import db

# Por debajo de esto, la fila va a la cola de revision en vez de a una causa.
UMBRAL = 0.72

CONFIANZA_CODIGO = 1.0
CONFIANZA_ALIAS = 0.9

TIPOS = ("parada", "scrap", "calidad")

# Lo que se guarda en causa_texto cuando el reporte no trajo ninguna
# descripcion. La columna es NOT NULL y el humano que revisa necesita ver algo.
SIN_TEXTO = "(sin descripción en el reporte)"

_NO_ALFANUM = re.compile(r"[^0-9a-z]+")
# Un codigo canonico despues de normalizar: "par mec 02", "parmec02", "scr-fug-01".
_CODIGO_EMBEBIDO = re.compile(r"\b([a-z]{3})\s?([a-z]{3})\s?(\d{2})\b")


# --- Normalizacion de cadenas ------------------------------------------------

def _sin_tildes(texto: str) -> str:
    # NFD separa la tilde de la letra y la deja como caracter combinante (Mn),
    # asi que basta con filtrar esa categoria. De paso "ñ" queda en "n", que es
    # justo lo que se quiere para comparar ("dano" == "daño").
    descompuesto = unicodedata.normalize("NFD", texto)
    return "".join(c for c in descompuesto if unicodedata.category(c) != "Mn")


def _norm(texto: Any) -> str:
    """minusculas, sin tildes, sin puntuacion, espacios colapsados."""
    if texto is None:
        return ""
    return _NO_ALFANUM.sub(" ", _sin_tildes(str(texto)).lower()).strip()


def _tokens(texto_norm: str) -> tuple[str, ...]:
    return tuple(texto_norm.split())


def _clave_codigo(texto: Any) -> str:
    """'PAR-MEC-02', 'par mec 02' y 'PARMEC02' colapsan a la misma clave."""
    if texto is None:
        return ""
    return re.sub(r"[^0-9a-z]", "", _sin_tildes(str(texto)).lower())


def _es_subsecuencia(tokens: tuple[str, ...], patron: tuple[str, ...]) -> bool:
    """True si `patron` aparece como tokens consecutivos dentro de `tokens`.

    A nivel de token y no de substring cruda a proposito: el alias "PU" esta
    contenido en "purga" como texto, pero son cosas distintas (inyeccion de
    poliuretano vs producto de arranque). Comparar tokens completos evita esa
    familia de falsos positivos, que es la mas frecuente con alias cortos.
    """
    n, m = len(tokens), len(patron)
    if m == 0 or m > n:
        return False
    return any(tokens[i:i + m] == patron for i in range(n - m + 1))


# --- Catalogos (se leen de la DB una vez por proceso) -------------------------

class _Etiqueta(NamedTuple):
    texto: str                    # normalizada
    tokens: tuple[str, ...]


class _Item(NamedTuple):
    id: int
    clave: str                    # desempate estable: codigo de causa / linea+nombre
    etiquetas: tuple[_Etiqueta, ...]


def _alias_de(fila: dict) -> list[str]:
    """Los alias vienen como TEXT con JSON en la DB, pero db.py podria devolverlos
    ya parseados. Se aceptan las dos formas para no acoplarse a ese detalle."""
    crudo = fila.get("alias")
    if not crudo:
        return []
    if isinstance(crudo, (list, tuple)):
        return [str(a) for a in crudo if a]
    try:
        datos = json.loads(crudo)
    except (TypeError, ValueError):
        # Ultimo recurso: alguien lo cargo como lista separada por comas.
        return [p.strip() for p in str(crudo).split(",") if p.strip()]
    if isinstance(datos, list):
        return [str(a) for a in datos if a]
    return [str(datos)] if datos else []


def _etiquetas(*textos: Any) -> tuple[_Etiqueta, ...]:
    """Nombre canonico + alias, normalizados y sin repetidos."""
    vistas: set[str] = set()
    salida: list[_Etiqueta] = []
    for t in textos:
        n = _norm(t)
        if n and n not in vistas:
            vistas.add(n)
            salida.append(_Etiqueta(n, _tokens(n)))
    return tuple(salida)


@lru_cache(maxsize=1)
def _catalogo_causas() -> dict[str, tuple[_Item, ...]]:
    """{tipo: (items,)} — el universo de busqueda separado por tipo.

    Separarlo aqui y no filtrar despues es lo que hace que "fuga" resuelva a
    SCR-FUG-01 cuando viene de una fila de scrap y a CAL-EST-01 cuando viene de
    una de calidad. La misma palabra, dos causas, y el tipo decide.
    """
    por_tipo: dict[str, list[_Item]] = {t: [] for t in TIPOS}
    for fila in db.causas():
        tipo = str(fila.get("tipo") or "").strip().lower()
        if tipo not in por_tipo:
            continue
        por_tipo[tipo].append(_Item(
            id=int(fila["id"]),
            clave=str(fila.get("codigo") or fila["id"]),
            etiquetas=_etiquetas(fila.get("nombre"), *_alias_de(fila)),
        ))
    return {t: tuple(sorted(items, key=lambda i: i.clave)) for t, items in por_tipo.items()}


@lru_cache(maxsize=1)
def _catalogo_codigos() -> dict[str, tuple[int, str]]:
    """{clave_codigo: (causa_id, tipo)} para el paso 1."""
    indice: dict[str, tuple[int, str]] = {}
    for fila in db.causas():
        clave = _clave_codigo(fila.get("codigo"))
        if clave:
            indice[clave] = (int(fila["id"]), str(fila.get("tipo") or "").lower())
    return indice


@lru_cache(maxsize=1)
def _catalogo_lineas() -> tuple[dict[str, int], dict[int, int]]:
    """({nombre_normalizado: id}, {numero_de_linea: id})."""
    por_nombre: dict[str, int] = {}
    por_numero: dict[int, int] = {}
    for fila in db.lineas():
        ident = int(fila["id"])
        nombre = _norm(fila.get("nombre"))
        if not nombre:
            continue
        por_nombre[nombre] = ident
        m = re.fullmatch(r"l\s*0*(\d{1,2})", nombre)
        if m:
            por_numero[int(m.group(1))] = ident
    return por_nombre, por_numero


@lru_cache(maxsize=1)
def _catalogo_estaciones() -> dict[int | None, tuple[_Item, ...]]:
    """{linea_id: (items,)} mas la clave None con todas.

    La entrada None existe porque un reporte puede no decir de que linea es la
    estacion; ahi se busca en toda la planta y se exige que el resultado sea
    unico (ver `normalizar_estacion`).
    """
    por_nombre, _ = _catalogo_lineas()
    agrupadas: dict[int | None, list[_Item]] = {}
    todas: list[_Item] = []
    for fila in db.estaciones():
        # db.estaciones() podria traer linea_id o el nombre de la linea; ambos sirven.
        linea_id = fila.get("linea_id")
        if linea_id is None:
            linea_id = por_nombre.get(_norm(fila.get("linea")))
        linea_id = int(linea_id) if linea_id is not None else None
        item = _Item(
            id=int(fila["id"]),
            clave=f"{linea_id}|{_norm(fila.get('nombre'))}",
            etiquetas=_etiquetas(fila.get("nombre"), *_alias_de(fila)),
        )
        agrupadas.setdefault(linea_id, []).append(item)
        todas.append(item)
    catalogo: dict[int | None, tuple[_Item, ...]] = {
        k: tuple(sorted(v, key=lambda i: i.clave)) for k, v in agrupadas.items()
    }
    catalogo[None] = tuple(sorted(todas, key=lambda i: i.clave))
    return catalogo


def refrescar_catalogos() -> None:
    """Invalida los catalogos cacheados. Llamar tras recargar la taxonomia.

    La app de Streamlit es un proceso largo: sin esto, un `db.inicializar(
    forzar=True)` cambiaria la DB y el normalizador seguiria clasificando contra
    el catalogo viejo, con ids que ya no existen.
    """
    _catalogo_causas.cache_clear()
    _catalogo_codigos.cache_clear()
    _catalogo_lineas.cache_clear()
    _catalogo_estaciones.cache_clear()
    normalizar_causa.cache_clear()


# --- Los dos criterios de match ----------------------------------------------

class _Marca(NamedTuple):
    puntaje: tuple[float, ...]    # comparable; mayor es mejor
    clave: str
    id: int


def _por_alias(texto_norm: str, tokens: tuple[str, ...], items) -> list[_Marca]:
    """Contencion por tokens en las dos direcciones, ordenada de mejor a peor.

    El peso es la longitud del fragmento que efectivamente coincidio, para que
    "falla electrica" le gane a "falla" cuando los dos son alias de causas
    distintas: el alias mas largo describe mejor lo que dice el reporte.

    La direccion inversa (el texto cabe dentro del alias) se acepta solo si el
    texto cubre al menos la mitad del alias. Sin ese freno, un texto generico
    como "material" quedaria "contenido" en el nombre canonico "Falta de
    material en linea" y se llevaria confianza 0.9 sin merecerla; con el freno
    cae al paso 3, que le da el ratio honesto que le corresponde.
    """
    marcas: list[_Marca] = []
    for item in items:
        mejor: tuple[float, float] | None = None
        for etq in item.etiquetas:
            if _es_subsecuencia(tokens, etq.tokens):
                peso = float(len(etq.texto))
            elif _es_subsecuencia(etq.tokens, tokens) and len(texto_norm) * 2 >= len(etq.texto):
                peso = float(len(texto_norm))
            else:
                continue
            # A igual fragmento gana el alias de tamano mas parecido al texto:
            # "fuga" debe resolver por el alias "fuga", no por "fuga de gas".
            candidato = (peso, -abs(len(etq.texto) - len(texto_norm)))
            if mejor is None or candidato > mejor:
                mejor = candidato
        if mejor is not None:
            marcas.append(_Marca(mejor, item.clave, item.id))
    return _ordenar(marcas)


def _por_similitud(texto_norm: str, tokens: tuple[str, ...], items) -> list[_Marca]:
    """Similitud difflib contra nombre + alias, ordenada de mejor a peor.

    Se toma el maximo entre el ratio por caracteres y el ratio por tokens: el
    primero perdona errores de digitacion ("atazco"), el segundo perdona
    palabras sobrantes o en otro orden ("se atoro el producto"). Los reportes
    de turno traen las dos cosas.
    """
    marcas: list[_Marca] = []
    for item in items:
        mejor = 0.0
        for etq in item.etiquetas:
            ratio = max(
                SequenceMatcher(None, texto_norm, etq.texto, autojunk=False).ratio(),
                SequenceMatcher(None, tokens, etq.tokens, autojunk=False).ratio(),
            )
            if ratio > mejor:
                mejor = ratio
        if mejor > 0.0:
            marcas.append(_Marca((round(mejor, 4),), item.clave, item.id))
    return _ordenar(marcas)


def _ordenar(marcas: list[_Marca]) -> list[_Marca]:
    # El desempate final por `clave` (el codigo canonico) es lo que hace la
    # clasificacion reproducible: sin el, el orden dependeria del orden de las
    # filas de la DB.
    return sorted(marcas, key=lambda m: (tuple(-x for x in m.puntaje), m.clave))


def _unico(marcas: list[_Marca]) -> int | None:
    """El id del mejor, o None si hay empate entre ids distintos."""
    if not marcas:
        return None
    empatados = {m.id for m in marcas if m.puntaje == marcas[0].puntaje}
    return marcas[0].id if len(empatados) == 1 else None


# --- API publica --------------------------------------------------------------

@lru_cache(maxsize=4096)
def normalizar_causa(texto: str, tipo: str) -> tuple[int | None, float]:
    """Mapea texto libre a una causa canonica del `tipo` dado.

    Devuelve (causa_id, confianza) o (None, 0.0) si nada supera el UMBRAL.
    El cacheo es seguro porque la funcion es pura: mismo texto y mismo tipo,
    misma respuesta. Y hace falta: un reporte de un mes repite "cambio de
    referencia" decenas de veces y cada evaluacion recorre 25 causas.
    """
    tipo = (tipo or "").strip().lower()
    if tipo not in TIPOS:
        return None, 0.0

    texto_norm = _norm(texto)
    if not texto_norm:
        return None, 0.0

    items = _catalogo_causas().get(tipo, ())
    if not items:
        return None, 0.0

    # Paso 1 — el texto es (o contiene) un codigo canonico.
    # Se valida contra el catalogo y contra el tipo: un "SCR-FUG-01" escrito en
    # una fila de parada es un error de captura, no una clasificacion.
    indice = _catalogo_codigos()
    encontrado = indice.get(_clave_codigo(texto))
    if encontrado is None:
        m = _CODIGO_EMBEBIDO.search(texto_norm)
        if m:
            encontrado = indice.get("".join(m.groups()))
    if encontrado is not None and encontrado[1] == tipo:
        return encontrado[0], CONFIANZA_CODIGO

    tokens = _tokens(texto_norm)

    # Paso 2 — contencion de alias.
    marcas = _por_alias(texto_norm, tokens, items)
    if marcas:
        # A diferencia de las estaciones, un empate aqui no anula el match: se
        # resuelve por codigo. Dos causas que empatan en el mismo alias son un
        # problema de la taxonomia, y mandarlo todo a revision no lo arregla.
        return marcas[0].id, CONFIANZA_ALIAS

    # Paso 3 — similitud.
    marcas = _por_similitud(texto_norm, tokens, items)
    if marcas and marcas[0].puntaje[0] >= UMBRAL:
        return marcas[0].id, round(float(marcas[0].puntaje[0]), 3)

    return None, 0.0


def normalizar_estacion(texto: str | None, linea: str | None = None) -> int | None:
    """Mapea texto libre a una estacion. None si no hay match o si es ambiguo.

    Cuando llega la linea, el universo se recorta a sus estaciones: "empaque"
    existe en las cuatro lineas y solo el contexto lo desambigua. Sin linea (o
    con una linea que no se pudo resolver) se busca en toda la planta y un
    empate devuelve None — es preferible una estacion vacia a una estacion de
    la linea equivocada, que despues aparece en un Pareto que nadie entiende.
    """
    texto_norm = _norm(texto)
    if not texto_norm:
        return None

    # La linea llega como la escribio el reporte ("L2", "Línea 2"), no como id.
    linea_id = normalizar_linea(linea) if linea else None
    items = _catalogo_estaciones().get(linea_id, ())
    if not items:
        return None

    tokens = _tokens(texto_norm)

    # Si el paso de alias encontro candidatos, decide ahi y no sigue: cuando
    # empatan, la similitud SIEMPRE los desempata (el nombre canonico de una de
    # ellas se parece un poco mas) y ese desempate es exactamente la adivinanza
    # que se quiere evitar. "conformado" sin linea empata en las cuatro lineas;
    # que gane L3 porque su estacion se llama justo "Conformado" no significa
    # que la parada haya sido en L3.
    marcas = _por_alias(texto_norm, tokens, items)
    if marcas:
        return _unico(marcas)

    marcas = _por_similitud(texto_norm, tokens, items)
    if marcas and marcas[0].puntaje[0] >= UMBRAL:
        return _unico(marcas)
    return None


def _lineas_donde_existe(texto_estacion: str) -> set[int]:
    """Lineas en las que esa estacion existe, resolviendola contra cada una."""
    _, por_numero = _catalogo_lineas()
    return {
        lid for numero, lid in por_numero.items()
        if normalizar_estacion(texto_estacion, str(numero)) is not None
    }


def _linea_por_estaciones(turno: dict, linea_id: int | None) -> int | None:
    """Linea que explican las estaciones nombradas, si contradice a la del modelo.

    Existe por un caso real: el reporte de WhatsApp del 20/07 nunca dice de que
    linea es —solo el nombre del archivo lo dice—, el extractor puso "L1", y
    "Remachado" solo existe en L2. Resultado: estacion_id None, la causa cae por
    similitud en "falla de sensor" (el texto menciona un sensor sucio), y las
    cinco paradas de la remachadora quedan repartidas entre dos lineas. El
    patron que el producto existe para detectar se vuelve invisible.

    Solo votan las estaciones que existen en UNA sola linea. "Empaque" esta en
    las cuatro y no dice nada; "Remachado" solo en L2 y por eso pesa. Un empate
    no elige: es preferible dejar la linea del modelo y que el supervisor lo vea
    en la cola, a cambiarla por una adivinanza.

    Cuenta votos y compara. No llama al modelo: si hubiera que preguntarle al
    LLM cual linea es, estariamos usandolo para decidir un dato, que es
    justamente lo que este proyecto no hace.
    """
    votos: Counter = Counter()
    for clave in ("paradas", "scrap", "calidad"):
        for fila in turno.get(clave) or []:
            if not isinstance(fila, dict):
                continue
            texto = str(fila.get("estacion") or "").strip()
            if not texto:
                continue
            posibles = _lineas_donde_existe(texto)
            if len(posibles) == 1:
                votos[next(iter(posibles))] += 1

    if not votos:
        return None
    orden = votos.most_common()
    ganadora, n = orden[0]
    if len(orden) > 1 and orden[1][1] == n:
        return None
    # La del modelo explica lo mismo o mas: no hay contradiccion que corregir.
    if linea_id is not None and votos.get(linea_id, 0) >= n:
        return None
    return ganadora


def normalizar_linea(texto: str | None) -> int | None:
    """'L2', 'l2', 'linea 2', 'LINEA 2', '2', 'Línea 2 - Lavadoras' -> id de L2."""
    texto_norm = _norm(texto)
    if not texto_norm:
        return None

    por_nombre, por_numero = _catalogo_lineas()
    if texto_norm in por_nombre:
        return por_nombre[texto_norm]

    # El campo trae solo la referencia de linea, en cualquiera de sus formas.
    m = re.fullmatch(r"(?:linea|line|l)?\s*0*(\d{1,2})", texto_norm)
    if m:
        return por_numero.get(int(m.group(1)))

    # El campo trae una frase ("reporte linea 2 lavadoras"). Se aceptan solo
    # numeros pegados a 'l' o precedidos de 'linea', para no confundir el 2 de
    # "turno 2" con la linea 2. Si aparece mas de una linea distinta, no se
    # elige ninguna.
    candidatos = {
        por_numero[int(n)]
        for n in re.findall(r"(?:\blinea\s*|\bl)0*(\d{1,2})\b", texto_norm)
        if int(n) in por_numero
    }
    return candidatos.pop() if len(candidatos) == 1 else None


# --- Recorrido de la salida del extractor -------------------------------------

def _texto_causa(fila: dict, *claves: str) -> str:
    for clave in claves:
        valor = fila.get(clave)
        if isinstance(valor, str) and valor.strip():
            return valor.strip()
        if isinstance(valor, (int, float)):
            return str(valor)
    return ""


def _clasificar(fila: dict, texto: str, tipo: str) -> tuple[int | None, float]:
    """Codigo primero, texto despues.

    Si el extractor puso un `causa_codigo`, es una clasificacion mas precisa que
    el texto libre. Pero no se cree a ciegas: pasa por el paso 1, que lo valida
    contra el catalogo y contra el tipo. Un codigo inventado por el modelo no
    resuelve, y la fila cae al texto como si nunca hubiera venido.
    """
    codigo = fila.get("causa_codigo")
    if codigo:
        causa_id, confianza = normalizar_causa(str(codigo), tipo)
        if causa_id is not None:
            return causa_id, confianza
    return normalizar_causa(texto, tipo)


def normalizar(datos: dict) -> dict:
    """Resuelve linea_id, estacion_id y causa_id sobre toda la salida de extraer().

    Trabaja sobre una copia: el texto crudo que vio el extractor se guarda tal
    cual en `documentos.texto_crudo` y su salida se usa tambien para verificar
    literalidad, asi que este modulo no puede mutarla por debajo.

    En cada fila deja `causa_id` (int|None) y `confianza_causa` (float), y nunca
    toca `causa_texto`: lo que no se clasifica no se pierde, se muestra en la
    cola de revision con lo que el supervisor escribio. La `confianza` que puso
    el extractor tampoco se toca — son dos cosas distintas (que tan literal es
    el dato vs que tan segura es la clasificacion) y quien las combine al
    guardar es `db.guardar_turno`.
    """
    resultado = copy.deepcopy(datos) if isinstance(datos, dict) else {"turnos": []}
    sin_clasificar = 0

    for turno in resultado.get("turnos") or []:
        if not isinstance(turno, dict):
            continue

        linea_texto = turno.get("linea")
        turno["linea_id"] = normalizar_linea(linea_texto)

        # Las estaciones que nombro el reporte mandan sobre la linea que dijo el
        # modelo: son un dato del documento, no una inferencia suya.
        corregida = _linea_por_estaciones(turno, turno["linea_id"])
        if corregida is not None:
            turno["linea_id"] = corregida
            turno["linea_corregida_por_estaciones"] = True
            # Desde aqui la linea de referencia es la corregida: si se siguiera
            # usando el texto del modelo, normalizar_estacion volveria a buscar
            # en la linea equivocada y dejaria estacion_id en None.
            _, por_numero = _catalogo_lineas()
            linea_texto = next(
                (str(num) for num, lid in por_numero.items() if lid == corregida),
                linea_texto,
            )

        for tipo, clave in (("parada", "paradas"), ("scrap", "scrap")):
            for fila in turno.get(clave) or []:
                if not isinstance(fila, dict):
                    continue
                texto = _texto_causa(fila, "causa_texto", "causa", "descripcion")
                if not texto:
                    # Sin descripcion pero con codigo, el codigo es lo unico que
                    # queda para que el revisor entienda de que se trataba.
                    texto = str(fila.get("causa_codigo") or "").strip()
                fila["causa_texto"] = texto or SIN_TEXTO
                fila["estacion_id"] = normalizar_estacion(fila.get("estacion"), linea_texto)
                causa_id, confianza = _clasificar(fila, texto, tipo)
                fila["causa_id"] = causa_id
                fila["confianza_causa"] = confianza
                if causa_id is None:
                    sin_clasificar += 1

        for fila in turno.get("calidad") or []:
            if not isinstance(fila, dict):
                continue
            # calidad no tiene causa_texto propio: el defecto viene en
            # `tipo_defecto` y el detalle en `descripcion`. Se intentan por
            # separado y no concatenados, porque una descripcion larga diluye
            # el ratio de similitud del tipo de defecto, que es lo informativo.
            fila["estacion_id"] = normalizar_estacion(fila.get("estacion"), linea_texto)
            causa_id, confianza = _clasificar(fila, _texto_causa(fila, "tipo_defecto"), "calidad")
            if causa_id is None:
                causa_id, confianza = normalizar_causa(
                    _texto_causa(fila, "descripcion"), "calidad"
                )
            fila["causa_id"] = causa_id
            fila["confianza_causa"] = confianza
            if causa_id is None:
                sin_clasificar += 1

    resultado["turnos"] = _consolidar(resultado.get("turnos") or [])
    resultado["sin_clasificar"] = sin_clasificar
    return resultado


# Listas de hechos de un turno. Al fusionar dos objetos del mismo turno se
# concatenan; el resto de campos se toma del primero que los traiga.
_LISTAS_TURNO = ("paradas", "scrap", "calidad", "observaciones", "campos_no_literales")


def _clave_turno(turno: dict) -> tuple:
    """La misma clave por la que la DB considera que dos turnos son el mismo.

    Tiene que empatar con el UNIQUE (fecha, turno, linea_id) de esquema.sql: si
    aqui se agrupara por otra cosa, se consolidarian turnos que la base habria
    guardado por separado, o al reves.
    """
    return (turno.get("fecha"), turno.get("turno"), turno.get("linea_id"))


def _consolidar(turnos: list) -> list:
    """Funde los objetos que son el MISMO turno (misma fecha, turno y linea).

    Existe por un caso real: un CSV export con nueve filas de detalle de un solo
    turno, del que el extractor devolvio SIETE turnos —uno por fila—. Cada uno
    se guardo aparte y `turnos_encontrados` paso de 1 a 7, que es la cifra con
    la que despues se calcula la cobertura ("esto es sobre 12 de 15 turnos").
    Inflarla es peor que un numero equivocado: le da al supervisor una confianza
    que no corresponde.

    Se consolida aqui, en Python, y no pidiendoselo al modelo: agrupar y contar
    es trabajo de Python (README.md, "La decision que organiza todo"). El LLM ya
    demostro que ante repeticiones prefiere sumar, y sumar es justo lo que
    destruye la senal de recurrencia.

    Las filas de detalle NO se deduplican entre si: tres paradas iguales de la
    misma maquina son tres paradas, y esa repeticion es exactamente lo que el
    producto existe para detectar.

    Los turnos sin fecha no se agrupan: ahi la clave es (None, None, None) para
    todos y fundiria turnos que no tienen nada que ver. Van tal cual y que
    guardar_turno decida.
    """
    fundidos: dict[tuple, dict] = {}
    salida: list = []
    for turno in turnos:
        if not isinstance(turno, dict):
            continue
        clave = _clave_turno(turno)
        if clave[0] is None:
            salida.append(turno)
            continue
        previo = fundidos.get(clave)
        if previo is None:
            fundidos[clave] = turno
            salida.append(turno)
            continue
        for campo in _LISTAS_TURNO:
            extra = turno.get(campo)
            if isinstance(extra, list) and extra:
                previo.setdefault(campo, [])
                if isinstance(previo[campo], list):
                    previo[campo].extend(extra)
        # Un escalar que el primer objeto no traia (el plan quedo en la fila 1 y
        # el producido en la fila 2) se recupera del siguiente que si lo tenga.
        for campo, valor in turno.items():
            if campo in _LISTAS_TURNO or valor is None:
                continue
            if previo.get(campo) is None:
                previo[campo] = valor
    return salida
