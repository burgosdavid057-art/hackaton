"""
Acceso a produccion.db: esquema, catalogos y escritura de hechos.

Todo el resto del Copiloto de Produccion entra a la base por aqui. Ningun otro
modulo abre sqlite3 por su cuenta ni conoce la ruta del archivo.

Tres cosas que este modulo garantiza y de las que el resto depende:

  1. Idempotencia real. Reprocesar la misma carpeta dos veces no duplica nada:
     `registrar_documento` corta por sha256 y `guardar_turno` reemplaza los
     hechos del turno (fecha, turno, linea) en vez de agregarlos de nuevo. Sin
     esto, la demo de "arrastra los archivos otra vez" infla todas las cifras.
  2. NULL no es 0. Un campo que el reporte no menciona se guarda NULL; un cero
     explicito se guarda 0. Las herramientas numericas necesitan distinguirlos
     para poder decir "no cargaron el dato" en vez de "hubo cero".
  3. Cada hecho cuelga de un documento_id. Cualquier cifra del resumen se
     rastrea hasta el archivo que la produjo.

Este modulo no importa 'openai' ni llama a ningun modelo: es puro SQL.

La ruta de la base se puede sobreescribir con la variable de entorno
PRODUCCION_DB (util para las evaluaciones, que corren contra una base temporal).
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

import yaml

_AQUI = Path(__file__).resolve().parent
_RAIZ = _AQUI.parent

RUTA_DB = Path(os.environ.get("PRODUCCION_DB") or (_RAIZ / "data" / "produccion.db"))
RUTA_ESQUEMA = _AQUI / "esquema.sql"
RUTA_TAXONOMIA = _AQUI / "taxonomia.yaml"

# Debajo de este umbral una fila entra a la cola de revision aunque tenga causa
# asignada. 0.75 no es magico: `normalizar_causa` corta el fuzzy match en 0.72,
# y `verificar_literalidad` pone 0.0 a los numeros que no aparecen en el texto.
# Con 0.75 caen las dos poblaciones dudosas y no la clasificacion limpia (1.0).
UMBRAL_REVISION = 0.75

_TABLAS_REVISABLES = ("paradas", "scrap", "calidad")
_TIPOS_CAUSA = ("parada", "scrap", "calidad")

# Rutas de base cuyo esquema ya se verifico en este proceso. Evita pagar una
# consulta a sqlite_master en cada conectar() sin cachear un booleano global que
# mienta si alguien reapunta RUTA_DB (las evals lo hacen).
_VERIFICADAS: set[str] = set()


# --- Conexion ----------------------------------------------------------------

def _conexion_cruda() -> sqlite3.Connection:
    """Conexion sin verificar el esquema. Solo para uso interno de inicializar."""
    RUTA_DB.parent.mkdir(parents=True, exist_ok=True)
    cx = sqlite3.connect(RUTA_DB, timeout=30.0)
    cx.row_factory = sqlite3.Row
    # foreign_keys es por conexion, no se persiste en el archivo: si no se
    # prende aqui, las FK del esquema son decorativas y entran turno_id huerfanos.
    cx.execute("PRAGMA foreign_keys = ON")
    # Streamlit corre varios hilos; con la carga escribiendo y el chat leyendo,
    # el journal por defecto da "database is locked". WAL + espera lo evita.
    cx.execute("PRAGMA busy_timeout = 30000")
    return cx


def conectar() -> sqlite3.Connection:
    """Devuelve una conexion lista: row_factory = sqlite3.Row y FK activas.

    Si la base todavia no tiene esquema, lo crea. Es deliberado: cualquier
    modulo (o la app) puede arrancar sin acordarse de llamar inicializar(), y
    una base vacia responde "no hay turnos" en vez de reventar con
    "no such table: turnos", que no le dice nada a nadie.
    """
    clave = str(RUTA_DB)
    if clave not in _VERIFICADAS:
        with closing(_conexion_cruda()) as cx:
            hay_esquema = _tabla_existe(cx, "causas")
        if not hay_esquema:
            inicializar()
        _VERIFICADAS.add(clave)
    return _conexion_cruda()


def _tabla_existe(cx: sqlite3.Connection, nombre: str) -> bool:
    fila = cx.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (nombre,)
    ).fetchone()
    return fila is not None


# --- Inicializacion ----------------------------------------------------------

_SQL_LINEA = {
    False: "INSERT INTO lineas (nombre, activa) VALUES (?, 1) "
           "ON CONFLICT(nombre) DO NOTHING",
    True:  "INSERT INTO lineas (nombre, activa) VALUES (?, 1) "
           "ON CONFLICT(nombre) DO UPDATE SET activa = 1",
}

_SQL_ESTACION = {
    False: "INSERT INTO estaciones (linea_id, nombre, alias) VALUES (?, ?, ?) "
           "ON CONFLICT(linea_id, nombre) DO NOTHING",
    True:  "INSERT INTO estaciones (linea_id, nombre, alias) VALUES (?, ?, ?) "
           "ON CONFLICT(linea_id, nombre) DO UPDATE SET alias = excluded.alias",
}

_SQL_CAUSA = {
    False: "INSERT INTO causas (tipo, codigo, nombre, categoria, alias) "
           "VALUES (?, ?, ?, ?, ?) ON CONFLICT(codigo) DO NOTHING",
    True:  "INSERT INTO causas (tipo, codigo, nombre, categoria, alias) "
           "VALUES (?, ?, ?, ?, ?) ON CONFLICT(codigo) DO UPDATE SET "
           "tipo = excluded.tipo, nombre = excluded.nombre, "
           "categoria = excluded.categoria, alias = excluded.alias",
}


def inicializar(forzar: bool = False) -> None:
    """Crea el esquema si falta y sincroniza los catalogos con taxonomia.yaml.

    Idempotente: correrla dos veces no duplica lineas, estaciones ni causas.

    `forzar` reescribe nombres, categorias y alias de lo que ya esta cargado
    (es el caso de uso real: planta revisa la taxonomia y cambia los alias).
    Lo que NO hace, ni con forzar, es borrar filas de catalogo: hay paradas y
    scrap apuntando a esas causas por FK, y borrarlas para "limpiar" perderia
    la clasificacion de meses de reportes. Una causa que sale del yaml queda
    huerfana en la tabla, que es exactamente lo que se quiere.
    """
    if not RUTA_ESQUEMA.exists():
        raise FileNotFoundError(f"No encuentro el esquema en {RUTA_ESQUEMA}")
    if not RUTA_TAXONOMIA.exists():
        raise FileNotFoundError(f"No encuentro la taxonomía en {RUTA_TAXONOMIA}")

    with closing(_conexion_cruda()) as cx:
        if not _tabla_existe(cx, "causas"):
            # executescript hace commit implicito de lo pendiente y corre el
            # archivo tal cual, con sus PRAGMA y sus indices parciales.
            cx.executescript(RUTA_ESQUEMA.read_text(encoding="utf-8"))
        # WAL sobrevive al archivo, asi que basta con ponerlo una vez, pero es
        # barato repetirlo y cubre bases creadas antes de esta linea.
        cx.execute("PRAGMA journal_mode = WAL")

        with cx:
            _cargar_taxonomia(cx, forzar)

    _VERIFICADAS.add(str(RUTA_DB))


def _cargar_taxonomia(cx: sqlite3.Connection, forzar: bool) -> None:
    datos = yaml.safe_load(RUTA_TAXONOMIA.read_text(encoding="utf-8")) or {}

    for linea in datos.get("lineas") or []:
        nombre = _texto(linea.get("nombre"))
        if not nombre:
            continue
        cx.execute(_SQL_LINEA[forzar], (nombre,))
        # No se usa lastrowid: con DO NOTHING sobre una linea existente vale 0.
        linea_id = cx.execute(
            "SELECT id FROM lineas WHERE nombre = ?", (nombre,)
        ).fetchone()["id"]

        for est in linea.get("estaciones") or []:
            nombre_est = _texto(est.get("nombre"))
            if not nombre_est:
                continue
            cx.execute(
                _SQL_ESTACION[forzar],
                (linea_id, nombre_est, _alias_json(est.get("alias"))),
            )

    for causa in datos.get("causas") or []:
        codigo = _texto(causa.get("codigo"))
        tipo = _texto(causa.get("tipo"))
        nombre = _texto(causa.get("nombre"))
        if not codigo or tipo not in _TIPOS_CAUSA or not nombre:
            # Una causa mal escrita en el yaml se salta en silencio en vez de
            # tumbar la carga entera: el CHECK del esquema la rechazaria igual.
            continue
        cx.execute(
            _SQL_CAUSA[forzar],
            (tipo, codigo, nombre, _texto(causa.get("categoria")),
             _alias_json(causa.get("alias"))),
        )


def _alias_json(alias) -> str | None:
    """Serializa la lista de alias a JSON. ensure_ascii=False para no guardar
    'l\\u00e1mina' y que el fuzzy match de normalizar.py lea texto normal."""
    if not alias:
        return None
    if isinstance(alias, str):
        alias = [alias]
    limpios = [str(a).strip() for a in alias if str(a).strip()]
    return json.dumps(limpios, ensure_ascii=False) if limpios else None


# --- Coercion de valores del extractor ---------------------------------------

def _texto(valor) -> str | None:
    if valor is None:
        return None
    s = str(valor).strip()
    return s or None


# El separador inicial es opcional pero se captura: sin el, ",5 kg" se leeria
# como 5 kg, un error de 10x en silencio.
_NUMERO = re.compile(r"-?[.,]?\d[\d.,]*")


def _a_numero(valor) -> float | None:
    """Convierte a float lo que venga del extractor, o None si no hay numero.

    Existe por una trampa de SQLite: las columnas REAL tienen afinidad, no tipo.
    Si el modelo devuelve "25 min" en `minutos`, sqlite lo guarda como TEXT sin
    quejarse y meses despues un SUM() lo cuenta como 0. Se limpia en la frontera.

    Los separadores no se pueden resolver por locale porque en la misma carpeta
    llegan exports de Excel en es-CO ("1.234") y en en-US ("1,234"). La regla es
    posicional y no depende de cual simbolo sea: un ultimo grupo de exactamente
    tres digitos con algo delante es separador de miles; cualquier otro es
    decimal. Asi "1.234" y "1,234" son 1234 (la misma equivalencia que ya asume
    el validador) y "2,5" / "2.5" son dos y medio.

    Lo que esta regla lee mal es "0,500 kg" (lo toma como 500). Se acepta: en un
    reporte de turno nadie escribe tres decimales, y en cambio los miles con
    separador aparecen en cada export.
    """
    if valor is None or isinstance(valor, bool):
        return None
    if isinstance(valor, (int, float)):
        return float(valor)

    m = _NUMERO.search(str(valor))
    if not m:
        return None
    crudo = m.group(0).rstrip(".,")  # "25." al final de frase

    if "." in crudo and "," in crudo:
        # Vienen los dos: el que este mas a la derecha es el decimal.
        decimal = "," if crudo.rindex(",") > crudo.rindex(".") else "."
        miles = "." if decimal == "," else ","
        crudo = crudo.replace(miles, "").replace(decimal, ".")
    elif "." in crudo or "," in crudo:
        sep = "." if "." in crudo else ","
        partes = crudo.split(sep)
        if len(partes[-1]) == 3 and partes[0]:
            crudo = "".join(partes)                              # miles
        else:
            crudo = "".join(partes[:-1]) + "." + partes[-1]      # decimal
    try:
        return float(crudo)
    except ValueError:
        return None


def _confianza(valor) -> float:
    """Confianza acotada a [0, 1]. Ausente se lee como 1.0 porque la columna es
    NOT NULL: si el extractor no dudo, no marcamos duda nosotros."""
    n = _a_numero(valor)
    if n is None:
        return 1.0
    return max(0.0, min(1.0, n))


def _numero_turno(valor) -> int | None:
    """1, 2 o 3; cualquier otra cosa queda NULL.

    El CHECK del esquema rechaza un turno 4 y tumbaria el documento completo.
    Preferimos guardar el turno sin numerar (queda visible como hueco) a perder
    las paradas y el scrap de ese reporte por un dato mal transcrito.
    """
    n = _a_numero(valor)
    if n is None:
        return None
    i = int(n)
    return i if i in (1, 2, 3) else None


def _entero(valor) -> int | None:
    n = _a_numero(valor)
    return int(n) if n is not None else None


# --- Documentos --------------------------------------------------------------

def registrar_documento(ruta: str, sha256: str, formato: str,
                        texto_crudo: str) -> int | None:
    """Registra el archivo de origen. Devuelve None si ese sha256 ya estaba.

    None no es un error: es la senal de "este archivo ya se proceso". Quien
    llama debe reportarlo como duplicado y no volver a extraer, que es lo que
    hace que reprocesar el inbox sea gratis y seguro.
    """
    with closing(conectar()) as cx:
        try:
            with cx:
                cur = cx.execute(
                    "INSERT INTO documentos (ruta, sha256, formato, cargado_en, texto_crudo) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (str(ruta), sha256, formato or "desconocido",
                     datetime.now().isoformat(timespec="seconds"), texto_crudo or ""),
                )
            return int(cur.lastrowid)
        except sqlite3.IntegrityError:
            # Se deja que choque contra el UNIQUE en vez de consultar antes:
            # asi tambien es correcto si dos hilos cargan el mismo archivo.
            return None


# --- Turnos y hechos ---------------------------------------------------------

def guardar_turno(documento_id: int, turno: dict) -> int:
    """Escribe un turno ya normalizado y sus hechos, en una sola transaccion.

    Espera el dict que sale de normalizar.py: con `linea_id`, y con `causa_id` /
    `estacion_id` ya resueltos en cada parada y cada scrap (None si no se pudo
    clasificar; el texto original nunca se pierde).

    Idempotente por (fecha, turno, linea_id): si ese turno ya existe, se
    actualizan sus cabeceras y se REEMPLAZAN sus paradas, scrap y calidad. Es
    reemplazo y no merge a proposito: si llega una version corregida del reporte,
    lo correcto es que mande la ultima, no que convivan las dos versiones de la
    misma parada y el pareto la cuente doble.

    Lanza ValueError si el turno no trae fecha: sin fecha no hay clave por la
    cual des-duplicar ni rango en el cual contarlo.
    """
    fecha = _texto(turno.get("fecha"))
    if not fecha:
        raise ValueError(
            "El turno no tiene fecha; sin fecha no se puede guardar ni consultar."
        )

    numero = _numero_turno(turno.get("turno"))
    linea_id = _entero(turno.get("linea_id"))

    with closing(conectar()) as cx:
        with cx:
            # 'IS' en vez de '=' porque turno y linea_id pueden ser NULL, y en
            # SQL NULL = NULL es NULL: con '=' nunca encontrariamos el turno
            # existente y duplicariamos en cada recarga.
            fila = cx.execute(
                "SELECT id FROM turnos WHERE fecha = ? AND turno IS ? AND linea_id IS ?",
                (fecha, numero, linea_id),
            ).fetchone()

            cabecera = (
                documento_id,
                _texto(turno.get("supervisor")),
                _a_numero(turno.get("unidades_plan")),
                _a_numero(turno.get("unidades_producidas")),
                _a_numero(turno.get("minutos_turno")),
            )

            if fila:
                turno_id = int(fila["id"])
                # documento_id se reapunta al archivo que trae la version
                # vigente: la trazabilidad debe llevar al papel que manda hoy.
                cx.execute(
                    "UPDATE turnos SET documento_id = ?, supervisor = ?, "
                    "unidades_plan = ?, unidades_producidas = ?, minutos_turno = ? "
                    "WHERE id = ?",
                    (*cabecera, turno_id),
                )
                for tabla in _TABLAS_REVISABLES:
                    cx.execute(f"DELETE FROM {tabla} WHERE turno_id = ?", (turno_id,))
            else:
                cur = cx.execute(
                    "INSERT INTO turnos (documento_id, fecha, turno, linea_id, supervisor, "
                    "unidades_plan, unidades_producidas, minutos_turno) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (cabecera[0], fecha, numero, linea_id, *cabecera[1:]),
                )
                turno_id = int(cur.lastrowid)

            _insertar_paradas(cx, turno_id, turno.get("paradas"))
            _insertar_scrap(cx, turno_id, turno.get("scrap"))
            _insertar_calidad(cx, turno_id, turno.get("calidad"))

    return turno_id


# Cuando el extractor trae una fila sin texto de causa igual se guarda: el
# minuto perdido o la unidad de scrap son reales y deben contar en los totales.
# Lo que queda es una fila sin clasificar en la cola de revision.
_SIN_TEXTO = "(sin descripción en el reporte)"


def _insertar_paradas(cx: sqlite3.Connection, turno_id: int, filas) -> None:
    for f in filas or []:
        if not isinstance(f, dict):
            continue
        cx.execute(
            "INSERT INTO paradas (turno_id, estacion_id, causa_id, causa_texto, "
            "minutos, hora_inicio, confianza) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                turno_id,
                _entero(f.get("estacion_id")),
                _entero(f.get("causa_id")),
                _texto(f.get("causa_texto")) or _SIN_TEXTO,
                _a_numero(f.get("minutos")),
                _texto(f.get("hora_inicio")),
                _confianza(f.get("confianza")),
            ),
        )


def _insertar_scrap(cx: sqlite3.Connection, turno_id: int, filas) -> None:
    for f in filas or []:
        if not isinstance(f, dict):
            continue
        cx.execute(
            "INSERT INTO scrap (turno_id, estacion_id, causa_id, causa_texto, "
            "unidades, kg, confianza) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                turno_id,
                _entero(f.get("estacion_id")),
                _entero(f.get("causa_id")),
                _texto(f.get("causa_texto")) or _SIN_TEXTO,
                _a_numero(f.get("unidades")),
                _a_numero(f.get("kg")),
                _confianza(f.get("confianza")),
            ),
        )


def _insertar_calidad(cx: sqlite3.Connection, turno_id: int, filas) -> None:
    for f in filas or []:
        if not isinstance(f, dict):
            continue
        cx.execute(
            "INSERT INTO calidad (turno_id, tipo_defecto, unidades, descripcion, confianza) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                turno_id,
                _texto(f.get("tipo_defecto")) or _SIN_TEXTO,
                _a_numero(f.get("unidades")),
                _texto(f.get("descripcion")),
                _confianza(f.get("confianza")),
            ),
        )


# --- Cola de revision --------------------------------------------------------

# Las tres consultas proyectan las mismas columnas para poder unirse. Cada tabla
# aporta NULL en las que no tiene: `calidad` no lleva causa_id (no se clasifica
# contra la taxonomia de paradas y scrap) y entra a la cola solo por confianza.
#
# Las columnas numericas van con su nombre real (minutos, unidades, kg) y no
# bajo un 'cantidad' generico: la pantalla de revision escribe de vuelta sobre
# esas columnas, y un alias obligaria a traducir el nombre en el camino de
# escritura, que es justo donde un error se guarda en la columna equivocada.
_SQL_PENDIENTES = """
SELECT 'paradas' AS tabla, p.id AS fila_id, p.causa_texto AS texto,
       p.causa_id, c.codigo AS causa_codigo, c.nombre AS causa_nombre,
       p.confianza, p.minutos, NULL AS unidades, NULL AS kg, p.hora_inicio,
       NULL AS descripcion, e.nombre AS estacion, t.id AS turno_id,
       t.fecha, t.turno, l.nombre AS linea, t.documento_id, d.ruta AS documento
  FROM paradas p
  JOIN turnos t          ON t.id = p.turno_id
  JOIN documentos d      ON d.id = t.documento_id
  LEFT JOIN lineas l     ON l.id = t.linea_id
  LEFT JOIN estaciones e ON e.id = p.estacion_id
  LEFT JOIN causas c     ON c.id = p.causa_id
 WHERE p.revisado = 0 AND (p.causa_id IS NULL OR p.confianza < :umbral)

UNION ALL

SELECT 'scrap', s.id, s.causa_texto,
       s.causa_id, c.codigo, c.nombre,
       s.confianza, NULL, s.unidades, s.kg, NULL,
       NULL, e.nombre, t.id,
       t.fecha, t.turno, l.nombre, t.documento_id, d.ruta
  FROM scrap s
  JOIN turnos t          ON t.id = s.turno_id
  JOIN documentos d      ON d.id = t.documento_id
  LEFT JOIN lineas l     ON l.id = t.linea_id
  LEFT JOIN estaciones e ON e.id = s.estacion_id
  LEFT JOIN causas c     ON c.id = s.causa_id
 WHERE s.revisado = 0 AND (s.causa_id IS NULL OR s.confianza < :umbral)

UNION ALL

SELECT 'calidad', q.id, q.tipo_defecto,
       NULL, NULL, NULL,
       q.confianza, NULL, q.unidades, NULL, NULL,
       q.descripcion, NULL, t.id,
       t.fecha, t.turno, l.nombre, t.documento_id, d.ruta
  FROM calidad q
  JOIN turnos t      ON t.id = q.turno_id
  JOIN documentos d  ON d.id = t.documento_id
  LEFT JOIN lineas l ON l.id = t.linea_id
 WHERE q.revisado = 0 AND q.confianza < :umbral

 ORDER BY confianza ASC, fecha DESC, tabla, fila_id
 LIMIT :limite
"""


def pendientes_revision(limite: int = 100) -> list[dict]:
    """Cola de trabajo humano: filas sin clasificar o con confianza baja.

    Cada fila trae su contexto (fecha, turno, linea, estacion) y la ruta del
    documento del que salio: sin eso la pantalla muestra "confianza 0.30" sobre
    un texto suelto y nadie puede ir a mirar el reporte original para decidir.

    Ordenada por confianza ascendente: primero lo que el extractor mismo marco
    como dudoso (los numeros que no aparecian literalmente en el texto quedan en
    0.0), y dentro de eso lo mas reciente, que es lo que todavia se puede
    verificar preguntandole al supervisor del turno.

    Devuelve [] cuando no hay nada pendiente. Aqui la lista vacia si es la
    respuesta correcta: significa "no queda nada por revisar", no "no hay datos".
    """
    with closing(conectar()) as cx:
        filas = cx.execute(
            _SQL_PENDIENTES, {"umbral": UMBRAL_REVISION, "limite": int(limite)}
        ).fetchall()

    pendientes = []
    for f in filas:
        d = dict(f)
        motivos = []
        if d["tabla"] != "calidad" and d["causa_id"] is None:
            motivos.append("causa sin clasificar")
        if d["confianza"] < UMBRAL_REVISION:
            motivos.append(f"confianza {d['confianza']:.2f}")
        d["motivo"] = " · ".join(motivos)
        pendientes.append(d)
    return pendientes


def marcar_revisado(tabla: str, fila_id: int, causa_id: int | None) -> None:
    """Cierra una fila de la cola con el veredicto humano.

    `causa_id` puede ser None y eso tambien es un veredicto: "revisado, ninguna
    causa del catalogo aplica". La fila sale de la cola igual, si no la persona
    la volveria a ver cada vez que abre la pantalla.

    La confianza pasa a 1.0 porque ya no es una estimacion del modelo sino un
    dato confirmado por alguien: las herramientas que descartan filas dudosas
    tienen que empezar a contarla.
    """
    if tabla not in _TABLAS_REVISABLES:
        raise ValueError(
            f"Tabla no revisable: {tabla!r}. Debe ser una de {_TABLAS_REVISABLES}."
        )

    with closing(conectar()) as cx:
        with cx:
            if tabla == "calidad":
                # calidad no tiene columna causa_id: el defecto se describe con
                # tipo_defecto libre. Se ignora el argumento, no se falla.
                cx.execute(
                    "UPDATE calidad SET revisado = 1, confianza = 1.0 WHERE id = ?",
                    (int(fila_id),),
                )
            else:
                cx.execute(
                    f"UPDATE {tabla} SET causa_id = ?, revisado = 1, confianza = 1.0 "
                    "WHERE id = ?",
                    (_entero(causa_id), int(fila_id)),
                )


# --- Catalogos ---------------------------------------------------------------

def causas(tipo: str | None = None) -> list[dict]:
    """Catalogo de causas canonicas, con los alias ya deserializados.

    `alias` vuelve como lista de python porque quien lo consume (el fuzzy match
    de normalizar.py y los selectores de la pantalla de revision) siempre lo
    quiere asi; que en la tabla sea JSON es un detalle de almacenamiento.
    """
    if tipo is not None and tipo not in _TIPOS_CAUSA:
        raise ValueError(f"Tipo de causa inválido: {tipo!r}. Use uno de {_TIPOS_CAUSA}.")

    sql = ("SELECT id, tipo, codigo, nombre, categoria, alias FROM causas "
           "{filtro} ORDER BY tipo, codigo")
    sql = sql.format(filtro="WHERE tipo = ?" if tipo else "")

    with closing(conectar()) as cx:
        filas = cx.execute(sql, (tipo,) if tipo else ()).fetchall()
    return [_con_alias(f) for f in filas]


def lineas() -> list[dict]:
    """Lineas de la planta, activas primero."""
    with closing(conectar()) as cx:
        filas = cx.execute(
            "SELECT id, nombre, activa FROM lineas ORDER BY activa DESC, nombre"
        ).fetchall()
    return [dict(f) for f in filas]


def estaciones(linea: str | None = None) -> list[dict]:
    """Estaciones, opcionalmente filtradas por nombre de linea ('L2').

    El filtro es COLLATE NOCASE porque quien escribe la consulta pone 'l2' tan
    seguido como 'L2', y hacerle fallar el filtro por una mayuscula es gratis
    de evitar.
    """
    sql = (
        "SELECT e.id, e.linea_id, l.nombre AS linea, e.nombre, e.alias "
        "FROM estaciones e JOIN lineas l ON l.id = e.linea_id "
        "{filtro} ORDER BY l.nombre, e.id"
    )
    sql = sql.format(filtro="WHERE l.nombre = ? COLLATE NOCASE" if linea else "")

    with closing(conectar()) as cx:
        filas = cx.execute(sql, (linea,) if linea else ()).fetchall()
    return [_con_alias(f) for f in filas]


def _con_alias(fila: sqlite3.Row) -> dict:
    d = dict(fila)
    crudo = d.get("alias")
    try:
        d["alias"] = json.loads(crudo) if crudo else []
    except (json.JSONDecodeError, TypeError):
        # Un alias corrupto no debe tumbar el catalogo entero: la fila sigue
        # siendo util por codigo y nombre, solo pierde el fuzzy match.
        d["alias"] = []
    return d


# --- Bloques para el prompt del extractor ------------------------------------

def texto_causas_para_prompt(tipo: str | None = None) -> str:
    """Lista canonica de causas para inyectar en el prompt del extractor.

    Una linea por causa: "PAR-MEC-02 | parada | Atasco de producto".

    Sin alias, a proposito. Los 45 codigos con sus alias son ~4.000 tokens y el
    contexto del modelo local es de 8.192 en total: no cabe el prompt, el
    catalogo y el reporte. Los alias no se pierden, trabajan despues en
    normalizar.py, que es donde ademas rinden mas (match determinista sobre el
    texto que el modelo copio literal).
    """
    with closing(conectar()) as cx:
        filas = cx.execute(
            "SELECT codigo, tipo, nombre FROM causas "
            + ("WHERE tipo = ? " if tipo else "")
            # Orden estable y agrupado por tipo: el modelo elige mejor cuando
            # los codigos de parada estan juntos y no intercalados con scrap.
            + "ORDER BY CASE tipo WHEN 'parada' THEN 0 WHEN 'scrap' THEN 1 ELSE 2 END, codigo",
            (tipo,) if tipo else (),
        ).fetchall()

    if not filas:
        return "(catálogo de causas vacío: ejecute db.inicializar())"
    return "\n".join(f"{f['codigo']} | {f['tipo']} | {f['nombre']}" for f in filas)


def texto_catalogo_para_prompt() -> str:
    """Lineas y estaciones para el prompt del extractor, una linea por linea.

    Formato: "L2: Conformado de gabinete, Remachado, Ensamble de tina, ...".

    Tampoco lleva alias, por el mismo presupuesto de contexto. El extractor solo
    necesita saber que estaciones existen para copiar el nombre parecido;
    resolver "R-02" -> Remachado es trabajo de normalizar.py.
    """
    with closing(conectar()) as cx:
        filas = cx.execute(
            "SELECT l.nombre AS linea, e.nombre AS estacion "
            "FROM lineas l LEFT JOIN estaciones e ON e.linea_id = l.id "
            "WHERE l.activa = 1 ORDER BY l.nombre, e.id"
        ).fetchall()

    if not filas:
        return "(catálogo de líneas vacío: ejecute db.inicializar())"

    orden: list[str] = []
    por_linea: dict[str, list[str]] = {}
    for f in filas:
        nombre = f["linea"]
        if nombre not in por_linea:
            por_linea[nombre] = []
            orden.append(nombre)
        if f["estacion"]:
            por_linea[nombre].append(f["estacion"])

    return "\n".join(
        f"{nombre}: {', '.join(por_linea[nombre]) or '(sin estaciones registradas)'}"
        for nombre in orden
    )
