"""
Extractor de reportes de turno: el paso NO agentico del pipeline.

Aqui el modelo no elige nada. No hay herramientas, no hay loop, no hay decision:
entra un reporte sucio (WhatsApp, formato impreso, export de Excel) y sale JSON
con la forma que declara `prompts.EXTRACTOR`. Un solo trabajo, temperatura 0.

Y despues del modelo viene el guard que sostiene todo el proyecto:
`verificar_literalidad`. Un modelo de 7B corriendo local es perfectamente capaz
de escribir 431 donde el reporte decia 413, o de inventar un plan de 480 porque
480 es lo que suele ser un turno. Ese error no se ve: el JSON queda bien formado,
la cifra es plausible, y termina en el resumen ejecutivo del gerente.

El guard es tonto a proposito: comprueba que cada numero aparezca TEXTUALMENTE en
el documento fuente. No entiende de produccion, no valida rangos, no compara
contra el historico. Solo pregunta si esa cifra esta en el papel. Es una prueba
debil (un 40 puede venir de la hora 09:40) pero atrapa justo el error que un
humano no detectaria leyendo el JSON, y no marca nada que no pueda sustentar.

Lo que NO hace: borrar. Un valor no literal se marca con confianza 0.0 y se
nombra en `campos_no_literales`, y sigue ahi para que la pantalla de revision lo
muestre en rojo con el texto original al lado. Borrarlo seria decidir por el
supervisor; marcarlo es darle el trabajo hecho salvo la decision.
"""

from __future__ import annotations

import json
import re
import sys
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path

if __name__ == "__main__" and not __package__:
    # `python produccion/extraer.py x.txt` deja el archivo como modulo suelto y
    # ahi no resuelven ni el paquete hermano ni `agent`. Se reentra como modulo
    # antes de importar nada, para no morir con un ImportError que no le dice
    # nada a quien solo queria depurar un reporte.
    import runpy
    import sys as _sys
    from pathlib import Path as _Path

    _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
    runpy.run_module("produccion.extraer", run_name="__main__")
    raise SystemExit(0)

from agent import llm

from . import prompts

# Tres intentos: con un modelo local, el primer JSON malo casi siempre se corrige
# al decirle que fallo. Del tercero en adelante ya no mejora, solo cuesta tiempo.
INTENTOS = 3

# El prompt del extractor ya trae 45 causas + 4 lineas con sus estaciones. Si
# ademas entra un Excel de 200 filas, no cabe en la ventana y Ollama recorta por
# el principio, que es justo donde vive el system prompt. Preferimos recortar
# nosotros el documento y decirlo, a que nos lo recorte el runtime en silencio.
MAX_CHARS_DOCUMENTO = 24_000

TIMEOUT_S = 300.0

_cliente_cache: dict[str, object] = {}


# --- Acceso al modelo --------------------------------------------------------

def _cliente() -> tuple[object, str, dict]:
    """Cliente OpenAI apuntando a Ollama (o a Groq), modelo y extras del motor."""
    cfg = llm.config_openai()
    clave = cfg["base_url"] + "|" + cfg["api_key"][:8]
    if clave not in _cliente_cache:
        from openai import OpenAI

        # Timeout largo a proposito: un 7B local parseando un reporte completo se
        # demora, y un timeout corto se ve igual que un modelo caido.
        _cliente_cache[clave] = OpenAI(
            base_url=cfg["base_url"], api_key=cfg["api_key"], timeout=TIMEOUT_S
        )

    extra: dict = {}
    if cfg.get("proveedor") == "ollama":
        # 8192 es el piso para este prompt: por debajo, el modelo deja de ver la
        # lista de causas y devuelve codigos inventados.
        ctx = max(int(cfg.get("num_ctx") or 0), 8192)
        extra = {"extra_body": {"options": {"num_ctx": ctx}}}
    return _cliente_cache[clave], cfg["modelo"], extra


def _json_del_texto(texto: str) -> dict | list | None:
    """Parsea la salida del modelo tolerando el envoltorio tipico.

    No se reusa `agent.validator._extraer_json` para no arrastrar el SDK de
    Gemini a un modulo que corre 100% local.
    """
    limpio = re.sub(r"^```(?:json)?|```$", "", (texto or "").strip(), flags=re.M).strip()
    try:
        return json.loads(limpio)
    except json.JSONDecodeError:
        pass
    # Modelos pequenos a veces anteponen "Aqui esta el JSON:". Se rescata el
    # bloque mas externo que abra y cierre llave.
    m = re.search(r"\{.*\}", limpio, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    return None


def _forma_valida(datos) -> tuple[dict | None, str]:
    """Acepta el JSON solo si tiene la forma del contrato, o si se puede salvar.

    Devuelve (datos_normalizados, motivo_de_rechazo). El rescate cubre los dos
    errores que mas comete un modelo pequeno: devolver la lista de turnos pelada,
    o devolver un unico turno sin envolverlo.
    """
    if isinstance(datos, list):
        return {"turnos": datos}, ""
    if not isinstance(datos, dict):
        return None, "la respuesta no es un objeto JSON"

    if isinstance(datos.get("turnos"), list):
        return datos, ""
    if "turnos" in datos and isinstance(datos["turnos"], dict):
        # devolvio {"turnos": {...}} en vez de una lista de uno
        return {**datos, "turnos": [datos["turnos"]]}, ""

    # Un turno suelto: se reconoce por sus campos propios, no por su ausencia.
    marcas = ("unidades_plan", "unidades_producidas", "paradas", "scrap", "minutos_turno")
    if any(k in datos for k in marcas):
        return {"turnos": [datos]}, ""

    return None, "falta la clave 'turnos' con una lista de turnos"


def _saneado(datos: dict) -> dict:
    """Garantiza las listas del contrato para que normalizar/db no revienten.

    Ojo: crear la lista vacia aqui NO es inventar un cero. `paradas: []` significa
    "el extractor no encontro filas de parada", que es exactamente lo que dijo el
    modelo al omitir la clave; el cero prohibido seria escribir `minutos: 0`.
    """
    turnos = []
    for t in datos.get("turnos") or []:
        if not isinstance(t, dict):
            continue
        for clave in ("paradas", "scrap", "calidad", "observaciones"):
            v = t.get(clave)
            if not isinstance(v, list):
                t[clave] = [] if v in (None, "") else [v]
        # Las filas tienen que ser dicts; una cadena suelta se descarta antes de
        # llegar a la DB, no despues.
        for clave in ("paradas", "scrap", "calidad"):
            t[clave] = [f for f in t[clave] if isinstance(f, dict)]
        turnos.append(t)
    datos["turnos"] = turnos
    return datos


def _recortar(texto: str) -> tuple[str, bool]:
    """Recorta el documento al tamano que cabe en la ventana, avisando."""
    if len(texto) <= MAX_CHARS_DOCUMENTO:
        return texto, False
    return (
        texto[:MAX_CHARS_DOCUMENTO]
        + "\n\n[...documento recortado por longitud: faltan "
        + str(len(texto) - MAX_CHARS_DOCUMENTO)
        + " caracteres...]"
    ), True


def extraer(texto: str) -> dict:
    """Convierte un reporte de turno en JSON estructurado. Sin herramientas.

    Reintenta hasta INTENTOS veces si el JSON no parsea o no trae 'turnos',
    diciendole al modelo en el reintento que fue lo que fallo. Si agota los
    intentos devuelve {"turnos": [], "error": ...}: nunca lanza, porque quien
    llama es el pipeline de ingesta procesando una carpeta y un archivo raro no
    puede tumbar los otros nueve.
    """
    if not texto or not texto.strip():
        return {"turnos": [], "error": "El documento llegó vacío: no hay texto que extraer."}

    # La taxonomia se inyecta desde la DB, no desde el YAML: lo que vale es lo
    # que quedo cargado (alguien pudo agregar una causa desde la UI).
    try:
        from . import db

        bloque_causas = db.texto_causas_para_prompt()
        bloque_catalogo = db.texto_catalogo_para_prompt()
    except Exception as e:  # noqa: BLE001
        return {
            "turnos": [],
            "error": (
                f"No pude leer la taxonomía de la base ({type(e).__name__}: {e}). "
                f"Corre db.inicializar() antes de extraer."
            ),
        }

    documento, truncado = _recortar(texto)
    mensajes = [
        {"role": "system", "content": prompts.extractor(bloque_causas, bloque_catalogo)},
        {"role": "user", "content": documento},
    ]

    motivo = "sin intentos"
    for intento in range(1, INTENTOS + 1):
        try:
            cliente, modelo, extra = _cliente()
            resp = cliente.chat.completions.create(
                model=modelo,
                messages=mensajes,
                temperature=0,
                response_format={"type": "json_object"},
                **extra,
            )
            crudo = resp.choices[0].message.content or ""
        except Exception as e:  # noqa: BLE001
            crudo = ""
            motivo = f"{type(e).__name__}: {e}"
        else:
            datos, motivo = _forma_valida(_json_del_texto(crudo))
            if datos is not None:
                datos = _saneado(datos)
                datos["modelo"] = modelo
                datos["intentos"] = intento
                if truncado:
                    datos["documento_truncado"] = True
                return datos

        if intento < INTENTOS:
            # Reinyectar el fallo importa mas de lo que parece: un 7B que ve su
            # propia salida rota junto al motivo la corrige en el siguiente tiro
            # mucho mas seguido que uno al que simplemente se le repite el prompt.
            mensajes.append({"role": "assistant", "content": crudo[:600]})
            mensajes.append({
                "role": "user",
                "content": (
                    f"Ese JSON no sirvio: {motivo}. Devuelve UNICAMENTE un objeto "
                    f"JSON valido con la clave \"turnos\" (una lista de turnos), "
                    f"con la forma exacta que te di. Sin markdown, sin explicacion."
                ),
            })

    return {
        "turnos": [],
        "error": f"El extractor no devolvió JSON válido en {INTENTOS} intentos ({motivo}).",
    }


# --- Guard de literalidad ----------------------------------------------------

# Campos numericos que se verifican, por nivel. `calidad` no guarda kg en el
# esquema, pero si el modelo lo emite igual se verifica: marcar un campo de mas
# no cuesta nada, dejar uno sin mirar si.
CAMPOS_TURNO = ("unidades_plan", "unidades_producidas", "minutos_turno")
CAMPOS_FILA = {
    "paradas": ("minutos",),
    "scrap": ("unidades", "kg"),
    "calidad": ("unidades", "kg"),
}

# Numeros tal como aparecen en un reporte: con separador de miles (punto, coma o
# espacio) y/o decimales. La primera alternativa tiene que ir antes que la
# segunda para que "1.234" no se lea como "1".
_TOKEN_NUM = re.compile(r"\d{1,3}(?:[.,\s]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?")

@lru_cache(maxsize=4096)
def _patron(variante: str) -> re.Pattern[str]:
    """Regex de la variante con frontera de digitos, cacheada.

    Las dos guardas de la izquierda evitan que 25 "aparezca" dentro de 425 (digito
    pegado) o dentro de 1.25 (parte decimal de otro numero). La de la derecha
    evita que 58 case con 58.7. Sin esto el guard aprueba casi cualquier cifra:
    en un reporte con 30 numeros, dos digitos cualesquiera caen adentro de alguno.

    La cache es acotada porque la UI de Streamlit vive dias en el mismo proceso y
    cada documento nuevo trae numeros nuevos.
    """
    return re.compile(
        r"(?<!\d)(?<![\d][.,])" + re.escape(variante) + r"(?!\d)(?![.,]\d)"
    )


def _grupos_de_miles(entero: str) -> list[str]:
    grupos, resto = [], entero
    while len(resto) > 3:
        grupos.insert(0, resto[-3:])
        resto = resto[:-3]
    grupos.insert(0, resto)
    return grupos


def _separadores_decimales(base: str) -> tuple[str, ...]:
    """El separador decimal nunca puede ser el mismo que el de miles."""
    if "." in base:
        return (",",)
    if "," in base:
        return (".",)
    return (".", ",")


def _variantes(d: Decimal) -> list[str]:
    """Todas las formas en que ese numero pudo quedar escrito en el reporte.

    1234 se escribe 1234, 1.234, 1,234 o 1 234; y un export de Excel lo escribe
    1234.0 o 1234.00. Son el mismo dato: si no generamos las variantes, marcamos
    en rojo cifras que si estaban y el supervisor deja de creerle a la pantalla.
    """
    signo = "-" if d < 0 else ""
    entero, _, frac = f"{abs(d):f}".partition(".")
    frac = frac.rstrip("0")

    bases = [entero]
    if len(entero) > 3:
        g = _grupos_de_miles(entero)
        bases += [".".join(g), ",".join(g), " ".join(g)]

    salidas: list[str] = []
    for base in bases:
        if frac:
            for sep in _separadores_decimales(base):
                salidas.append(base + sep + frac)
                salidas.append(base + sep + frac + "0")  # 8.5 escrito 8.50
        else:
            salidas.append(base)
            for sep in _separadores_decimales(base):
                salidas.append(base + sep + "0")   # 58 escrito 58.0
                salidas.append(base + sep + "00")  # 58 escrito 58.00

    vistas, unicas = set(), []
    for s in salidas:
        s = signo + s
        if s not in vistas:
            vistas.add(s)
            unicas.append(s)
    return unicas


def _interpretaciones(token: str) -> list[Decimal]:
    """Valores posibles de un token del texto. Puede ser mas de uno.

    "1.234" es ambiguo de verdad: en un reporte colombiano son mil doscientos
    treinta y cuatro, y en un export en ingles es uno coma dos tres cuatro. Se
    aceptan las dos lecturas en vez de elegir una y marcar en rojo la buena.
    """
    t = token.replace(" ", "")
    crudos: list[str] = []
    hay_punto, hay_coma = "." in t, "," in t

    if hay_punto and hay_coma:
        # El separador que va mas a la derecha es el decimal: 1.234,50 / 1,234.50
        if t.rfind(".") > t.rfind(","):
            crudos.append(t.replace(",", ""))
        else:
            crudos.append(t.replace(".", "").replace(",", "."))
    elif hay_punto or hay_coma:
        sep = "." if hay_punto else ","
        partes = t.split(sep)
        if len(partes) > 2:
            if len(partes[-1]) == 3:
                crudos.append("".join(partes))  # 1.234.567 solo puede ser miles
            else:
                crudos.append("".join(partes[:-1]) + "." + partes[-1])  # 1.234,50
        else:
            entero, resto = partes
            # Grupo de miles valido: 1 a 3 digitos, sin cero a la izquierda.
            if len(resto) == 3 and 1 <= len(entero) <= 3 and not entero.startswith("0"):
                crudos.append(entero + resto)
            crudos.append(entero + "." + resto)

        # Tercera lectura, y en un export crudo la correcta: no es un separador
        # de miles sino el delimitador de campos de un CSV ("...,480,402,..."),
        # o sea varios numeros pegados. El CSV es uno de los tres formatos de
        # entrada; sin esta lectura, cada export entra completo a la cola de
        # revision y una cola que siempre esta en rojo no la mira nadie.
        if all(len(p) == 3 for p in partes[1:]) and len(partes[0]) <= 3:
            crudos.extend(partes)
    else:
        crudos.append(t)

    valores = []
    for c in crudos:
        try:
            valores.append(Decimal(c))
        except InvalidOperation:
            continue
    return valores


def _numeros_del_texto(texto: str) -> set[Decimal]:
    """Indice de todo numero presente en el documento, ya normalizado.

    Segunda red, despues de la busqueda por substring: cubre formatos que no
    generamos como variante ($ 1.234,00 dentro de una celda, 1'234, etc.). No
    afloja el guard (el numero sigue teniendo que estar en el texto), solo evita
    marcar en rojo por culpa del formato.
    """
    presentes: set[Decimal] = set()
    for m in _TOKEN_NUM.finditer(texto or ""):
        presentes.update(_interpretaciones(m.group(0)))
    return presentes


def _a_decimal(valor) -> Decimal | None:
    """Convierte el valor que devolvio el modelo a Decimal, o None si no es numero."""
    if valor is None or isinstance(valor, bool):
        return None
    if isinstance(valor, (int, float)):
        try:
            d = Decimal(str(valor))
        except InvalidOperation:
            return None
        return d if d.is_finite() else None
    if isinstance(valor, str):
        # El modelo a veces devuelve "25 min" o "1.234 und" en vez del numero.
        m = _TOKEN_NUM.search(valor)
        if not m:
            return None
        posibles = _interpretaciones(m.group(0))
        return posibles[0] if posibles else None
    return None


def _es_literal(valor, texto: str, presentes: set[Decimal]) -> bool:
    """True si el valor aparece textualmente en el documento fuente."""
    d = _a_decimal(valor)
    if d is None:
        return True  # no es una cifra: no hay nada que verificar aqui

    # Un digito solo (0..9) esta en cualquier texto: en una fecha, en una hora,
    # en el numero de turno. Verificarlo no prueba nada y llenaria la cola de
    # revision de falsos positivos, que es como se mata una pantalla de revision.
    if abs(d) < 10 and d == d.to_integral_value():
        return True

    candidatos = _variantes(d)
    if isinstance(valor, str) and valor.strip():
        candidatos.append(valor.strip())  # tal cual lo escribio el modelo

    if any(_patron(v).search(texto) for v in candidatos):
        return True
    return d in presentes


def verificar_literalidad(datos: dict, texto: str) -> dict:
    """Marca todo campo numerico que no aparezca literal en el documento fuente.

    Modifica `datos` en sitio y lo devuelve. Por cada turno agrega
    `campos_no_literales` (lista de rutas tipo "paradas[1].minutos") y pone
    `confianza = 0.0` en la fila del campo marcado. Al nivel raiz agrega
    `total_no_literales`.

    El valor NO se borra. La pantalla de revision necesita mostrarlo en rojo
    junto al texto original para que el supervisor corrija en dos segundos; sin
    el valor sospechoso al lado, tendria que volver a leer el reporte entero.
    """
    if not isinstance(datos, dict):
        return {"turnos": [], "total_no_literales": 0,
                "error": "verificar_literalidad recibió algo que no es un dict"}

    texto = texto or ""
    presentes = _numeros_del_texto(texto)
    total = 0

    for turno in datos.get("turnos") or []:
        if not isinstance(turno, dict):
            continue
        marcados: list[str] = []

        for campo in CAMPOS_TURNO:
            if campo in turno and not _es_literal(turno[campo], texto, presentes):
                marcados.append(campo)
                # Los campos de cabecera no viven en una fila hija: su "fila" es
                # el turno. La confianza queda aqui para que la UI pinte el
                # encabezado, aunque la tabla turnos no tenga la columna.
                turno["confianza"] = 0.0

        for coleccion, campos in CAMPOS_FILA.items():
            filas = turno.get(coleccion)
            if not isinstance(filas, list):
                continue
            for i, fila in enumerate(filas):
                if not isinstance(fila, dict):
                    continue
                for campo in campos:
                    if campo in fila and not _es_literal(fila[campo], texto, presentes):
                        marcados.append(f"{coleccion}[{i}].{campo}")
                        fila["confianza"] = 0.0

        # Siempre se escribe la clave, tambien vacia: la UI y las evals pueden
        # asumir que existe en vez de andar preguntando.
        turno["campos_no_literales"] = marcados
        total += len(marcados)

    datos["total_no_literales"] = total
    return datos


# --- Depuracion desde consola ------------------------------------------------

def _texto_de(ruta: Path) -> str:
    """Lee el archivo con ingest si ya esta disponible; si no, como texto plano."""
    try:
        from . import ingest

        texto, _formato = ingest.a_texto(ruta)
        if texto:
            return texto
    except Exception:  # noqa: BLE001
        pass  # ingest aun no existe o no soporta el formato: seguimos en plano
    datos = ruta.read_bytes()
    for codec in ("utf-8", "latin-1"):
        try:
            return datos.decode(codec)
        except UnicodeDecodeError:
            continue
    return datos.decode("utf-8", errors="replace")


def _main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("uso: python -m produccion.extraer <archivo>", file=sys.stderr)
        return 2

    ruta = Path(argv[0]).expanduser()
    if not ruta.is_file():
        print(f"no existe el archivo: {ruta}", file=sys.stderr)
        return 2

    texto = _texto_de(ruta)
    datos = verificar_literalidad(extraer(texto), texto)

    # El JSON va a stdout y el resumen a stderr, para poder hacer
    # `python -m produccion.extraer x.txt > salida.json` sin ensuciar el archivo.
    # El flush es para que en consola el resumen salga DESPUES del JSON: son dos
    # buffers distintos y sin esto aparecen al reves.
    print(json.dumps(datos, ensure_ascii=False, indent=2, default=str))
    sys.stdout.flush()

    if datos.get("error"):
        print(f"\nERROR: {datos['error']}", file=sys.stderr)
        return 1

    turnos = datos.get("turnos") or []
    print(f"\n{len(turnos)} turno(s) · {datos.get('total_no_literales', 0)} campo(s) "
          f"no literal(es) · modelo {datos.get('modelo', '?')} · "
          f"{datos.get('intentos', '?')} intento(s)", file=sys.stderr)
    for i, t in enumerate(turnos):
        dudosos = t.get("campos_no_literales") or []
        etiqueta = f"  turno #{i} {t.get('fecha')} T{t.get('turno')} {t.get('linea')}"
        if dudosos:
            print(f"{etiqueta} -> REVISAR: {', '.join(dudosos)}", file=sys.stderr)
        else:
            print(f"{etiqueta} -> todos los números aparecen en el documento",
                  file=sys.stderr)
    return 0


if __name__ == "__main__":
    # Consola de Windows en cp1252: sin esto, imprimir el JSON con tildes revienta.
    for flujo in (sys.stdout, sys.stderr):
        try:
            flujo.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass
    raise SystemExit(_main(sys.argv[1:]))
