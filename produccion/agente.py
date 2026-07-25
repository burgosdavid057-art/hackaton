"""
El agente del Copiloto de Produccion: loop de tool calling sobre Ollama.

Calca agent/openai_backend.py:responder — mismo contrato de entrada y salida,
mismo manejo de tool_calls, misma clase Traza — y cambia lo que tenia que
cambiar para planta:

  - el system es prompts.COPILOTO (no vende neveras, consolida turnos);
  - despacha a herramientas.ejecutar (aritmetica) y a las tres busquedas de
    rag.py, que viven en modulos distintos y por eso hay un enrutador;
  - MAX_PASOS = 8, porque hay 10 herramientas y una pregunta buena casi siempre
    encadena una numerica con una de texto;
  - fuerza la ventana de contexto de Ollama (ver _extra).

El agente no calcula nada. Ni una suma. Todo numero que aparece en la respuesta
salio de una herramienta y quedo registrado en la Traza, que es lo que el
validador audita y lo que la UI muestra en el panel de confianza.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field

from agent import llm

from . import herramientas, prompts
from .declaraciones import ESPECIFICACIONES, NOMBRES_RAG, herramientas_openai

# rag.py es opcional en tiempo de import. Si chromadb no esta instalado en la
# maquina de planta, el Copiloto tiene que seguir respondiendo con las
# herramientas numericas en vez de no arrancar: las busquedas pasan a devolver
# disponible: False, que es un caso que el prompt ya sabe manejar. Un agente que
# no levanta es peor que uno que responde "esa parte no la tengo".
try:
    from . import rag
except Exception:  # noqa: BLE001
    rag = None

MAX_PASOS = 8
TEMPERATURA = 0.2

# Un 7B local con num_ctx grande tarda en el primer token mucho mas que una API
# en la nube. 120 s (lo que usa el agente de postventa contra Groq) corta
# llamadas que iban bien.
TIMEOUT_SEGUNDOS = 180.0

# Tope de evidencia que se le manda al validador. Ver Traza.evidencia_json.
LIMITE_EVIDENCIA = 12000

_cliente_cache: dict[str, object] = {}


# --- Traza --------------------------------------------------------------------

@dataclass
class Traza:
    """Que herramientas llamo el agente, con que argumentos y que le devolvieron.

    Es la pieza que hace auditable al Copiloto: la UI la pinta en el panel de
    confianza y el validador la recibe serializada. Sin traza, una cifra de
    produccion es la palabra de un modelo.
    """

    pasos: list[dict] = field(default_factory=list)
    evidencia: list[dict] = field(default_factory=list)

    def registrar(self, herramienta: str, argumentos: dict, resultado: dict) -> None:
        self.pasos.append({"herramienta": herramienta, "argumentos": argumentos})
        self.evidencia.append({
            "herramienta": herramienta,
            "argumentos": argumentos,
            "resultado": resultado,
        })

    @property
    def herramientas_usadas(self) -> list[str]:
        return [p["herramienta"] for p in self.pasos]

    @property
    def coberturas(self) -> list[dict]:
        """Los bloques `cobertura` que devolvio cada herramienta numerica.

        No se agregan ni se suman entre si a proposito: dos herramientas sobre
        el mismo rango cuentan los mismos turnos, y sumarlos daria un total
        inflado. La UI decide como mostrarlos.
        """
        salida = []
        for e in self.evidencia:
            r = e["resultado"]
            if isinstance(r, dict) and isinstance(r.get("cobertura"), dict):
                salida.append({"herramienta": e["herramienta"], **r["cobertura"]})
        return salida

    @property
    def cobertura_parcial(self) -> bool:
        """True si alguna herramienta trabajo sobre menos turnos de los esperados."""
        return any(c.get("parcial") for c in self.coberturas)

    @property
    def faltantes(self) -> list[str]:
        """Motivos de las herramientas que no pudieron responder.

        Es lo que la UI muestra como "para responder esto falta cargar X". Se
        alimenta solo de `disponible: False`, nunca de un resultado en cero: un
        cero real es un dato, no una ausencia.
        """
        motivos = []
        for e in self.evidencia:
            r = e["resultado"]
            if isinstance(r, dict) and r.get("disponible") is False:
                motivo = r.get("motivo") or "sin motivo declarado"
                texto = f"{e['herramienta']}: {motivo}"
                if texto not in motivos:
                    motivos.append(texto)
        return motivos

    def evidencia_json(self, limite: int = LIMITE_EVIDENCIA) -> str:
        """Serializa la evidencia para el validador.

        El recorte se hace por herramienta y no cortando el string final. Cortar
        a pelo produce un JSON roto; el validador que recibe JSON roto falla en
        cerrado y marca como no verificada una respuesta que estaba bien. Aqui
        se recorta el resultado de cada herramienta a su presupuesto y se deja
        una marca explicita, para que el auditor sepa que vio menos y no lo
        confunda con evidencia ausente.
        """
        texto = json.dumps(self.evidencia, ensure_ascii=False, default=str)
        if len(texto) <= limite or not self.evidencia:
            return texto

        # +1 en el divisor: deja aire para las claves, los argumentos y las
        # comas, que tambien ocupan y no se recortan.
        presupuesto = max(400, limite // (len(self.evidencia) + 1))
        recortada = []
        for e in self.evidencia:
            crudo = json.dumps(e["resultado"], ensure_ascii=False, default=str)
            if len(crudo) <= presupuesto:
                recortada.append(e)
                continue
            item = dict(e)
            item["resultado"] = {
                "_recortado": True,
                "_motivo": (
                    f"resultado de {len(crudo)} caracteres recortado a "
                    f"{presupuesto} para el auditor"
                ),
                "fragmento": crudo[:presupuesto],
            }
            recortada.append(item)
        return json.dumps(recortada, ensure_ascii=False, default=str)


# --- Cliente ------------------------------------------------------------------

def _cliente():
    cfg = llm.config_openai()
    clave = cfg["base_url"] + "|" + cfg["api_key"][:8]
    if clave not in _cliente_cache:
        from openai import OpenAI

        _cliente_cache[clave] = OpenAI(
            base_url=cfg["base_url"], api_key=cfg["api_key"], timeout=TIMEOUT_SEGUNDOS
        )
    return _cliente_cache[clave], cfg["modelo"]


def _extra(cfg: dict) -> dict:
    """Ventana de contexto de Ollama, que no es parametro del estandar OpenAI.

    Sin esto Ollama usa num_ctx=2048 y trunca en silencio: primero se come el
    historial, despues los resultados de las herramientas, y el agente empieza a
    responder sobre datos que ya no tiene delante. Con COPILOTO + 10
    declaraciones + tablas de resultados, 2048 tokens se agotan en el primer
    turno. Ollama respeta `options` por su endpoint compatible con OpenAI.

    Se lee OLLAMA_NUM_CTX del entorno; si no esta, se usa el valor que ya
    resolvio llm.config_openai(). El orden importa: config_openai() hace
    load_dotenv(), asi que para cuando miramos os.environ el .env ya entro.
    """
    if cfg.get("proveedor") != "ollama":
        return {}
    crudo = os.environ.get("OLLAMA_NUM_CTX") or cfg.get("num_ctx")
    try:
        n = int(crudo)
    except (TypeError, ValueError):
        return {}
    if n <= 0:
        return {}
    return {"extra_body": {"options": {"num_ctx": n}}}


# --- Despacho de herramientas -------------------------------------------------

_DECLARADAS = frozenset(esp["nombre"] for esp in ESPECIFICACIONES)

# (herramienta, parametro) -> tipo declarado. Se arma de ESPECIFICACIONES para
# no repetir la tabla de tipos en dos archivos.
_TIPOS_PARAM = {
    (esp["nombre"], param): tipo
    for esp in ESPECIFICACIONES
    for param, (tipo, _) in esp["parametros"].items()
}

# Valores con los que un modelo pequeno dice "sin filtro" cuando deberia omitir
# el parametro. Si llegaran tal cual a la herramienta, la consulta buscaria una
# linea llamada "todas" y devolveria cero filas — un falso "no hubo scrap".
_SIN_FILTRO = {"", "todas", "todos", "all", "todas las lineas", "cualquiera",
               "n/a", "na", "none", "null", "ninguna", "-"}

# Solo estos parametros son filtros opcionales. En `consulta` o `descripcion`
# la palabra "todas" puede ser parte legitima de la busqueda.
_PARAMS_FILTRO = {"linea", "maquina", "estacion"}


def _limpiar_argumentos(nombre: str, argumentos: dict) -> dict:
    """Normaliza lo que manda el modelo antes de llegar a la herramienta.

    Tres correcciones, todas por fallas reales de modelos pequenos:
    (1) los nulos y cadenas vacias se omiten, para que aplique el default de la
        firma en vez de propagar un None a la consulta;
    (2) los sentinelas tipo "todas" en un filtro se omiten (ver _SIN_FILTRO);
    (3) los numeros que vienen como texto ("7", "10.0") se convierten al tipo
        declarado: un float donde va un LIMIT revienta la herramienta.
    """
    limpios = {}
    for clave, valor in (argumentos or {}).items():
        if valor is None:
            continue
        if isinstance(valor, str):
            v = valor.strip()
            if not v or v.lower() in ("null", "none", "n/a"):
                continue
            if clave in _PARAMS_FILTRO and v.lower() in _SIN_FILTRO:
                continue
            valor = v

        tipo = _TIPOS_PARAM.get((nombre, clave))
        if tipo in ("integer", "number") and isinstance(valor, (str, float, int, bool)):
            try:
                valor = int(float(valor)) if tipo == "integer" else float(valor)
            except (TypeError, ValueError):
                # Si no se puede convertir se pasa tal cual: que falle la
                # herramienta con un motivo claro y no aqui en silencio.
                pass
        limpios[clave] = valor
    return limpios


def _ejecutar_rag(nombre: str, argumentos: dict) -> dict:
    """Ejecuta una de las tres busquedas de rag.py, capturando todo."""
    if rag is None:
        return {
            "disponible": False,
            "motivo": (
                "el modulo de busqueda semantica no se pudo cargar en esta "
                "maquina (falta chromadb o su indice)"
            ),
        }
    fn = getattr(rag, nombre, None)
    if fn is None:
        return {"disponible": False, "motivo": f"rag.py no expone {nombre}"}
    try:
        return fn(**argumentos)
    except TypeError as e:
        return {"disponible": False, "motivo": f"argumentos invalidos para {nombre}: {e}"}
    except Exception as e:  # noqa: BLE001
        return {"disponible": False, "motivo": f"fallo {nombre}: {type(e).__name__}: {e}"}


def _despachar(nombre: str, argumentos: dict) -> dict:
    """Enruta la herramienta al modulo que la implementa.

    Todo fallo se devuelve como `disponible: False` con motivo, nunca como un
    resultado vacio. Es la misma regla del contrato aplicada al enrutador: si el
    agente recibe [] no distingue "no hubo paradas" de "la consulta se cayo", y
    la primera lectura de esa ambiguedad siempre es la optimista.
    """
    if nombre not in _DECLARADAS:
        # Nombre alucinado. Se corta aqui y no en herramientas.ejecutar para
        # devolverle al modelo la lista real: con ella suele recuperarse en el
        # siguiente paso, y sin ella insiste con el mismo nombre inventado.
        return {
            "disponible": False,
            "motivo": (
                f"no existe la herramienta '{nombre}'. Las disponibles son: "
                + ", ".join(sorted(_DECLARADAS))
            ),
        }

    argumentos = _limpiar_argumentos(nombre, argumentos)
    if nombre in NOMBRES_RAG:
        return _ejecutar_rag(nombre, argumentos)
    try:
        return herramientas.ejecutar(nombre, argumentos)
    except Exception as e:  # noqa: BLE001
        return {"disponible": False, "motivo": f"fallo {nombre}: {type(e).__name__}: {e}"}


# --- Loop ---------------------------------------------------------------------

def _mensajes_iniciales(historial, mensaje):
    # historial es una lista de mensajes estilo OpenAI (dicts). Si viene vacio o
    # de otro backend, arrancamos limpio con el system.
    ok = isinstance(historial, list) and all(isinstance(m, dict) for m in (historial or []))
    if historial and ok:
        mensajes = list(historial)
    else:
        mensajes = [{"role": "system", "content": prompts.COPILOTO}]
    mensajes.append({"role": "user", "content": mensaje})
    return mensajes


def responder(mensaje: str, historial: list | None = None) -> tuple[str, Traza, list]:
    """Loop de tool calling. Mismo contrato que agent.openai_backend.responder.

    Devuelve (texto, traza, historial_actualizado). El historial se devuelve
    para que la UI lo guarde y el agente recuerde de que linea y de que rango de
    fechas se venia hablando.
    """
    cliente, modelo = _cliente()
    extra = _extra(llm.config_openai())
    mensajes = _mensajes_iniciales(historial, mensaje)
    declaradas = herramientas_openai()
    traza = Traza()

    # Cache de llamadas identicas dentro del turno. El modo de falla tipico de
    # un modelo pequeno con 8 pasos es repetir la misma llamada esperando otro
    # resultado y agotar el presupuesto sin llegar a responder. Se le devuelve
    # el resultado ya calculado con una nota que le dice que siga.
    vistas: dict[str, dict] = {}

    for _ in range(MAX_PASOS):
        resp = cliente.chat.completions.create(
            model=modelo,
            messages=mensajes,
            tools=declaradas,
            tool_choice="auto",
            temperature=TEMPERATURA,
            **extra,
        )
        msg = resp.choices[0].message

        if not msg.tool_calls:
            texto = (msg.content or "").strip() or (
                "No pude generar una respuesta. Reformula la pregunta indicando "
                "la línea y el rango de fechas."
            )
            mensajes.append({"role": "assistant", "content": texto})
            return texto, traza, mensajes

        # El modelo pidio herramientas: registrar su turno y correrlas.
        mensajes.append({
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in msg.tool_calls
            ],
        })
        for tc in msg.tool_calls:
            try:
                argumentos = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                argumentos = {}
            if not isinstance(argumentos, dict):
                argumentos = {}
            # Se sanean aqui, antes de la firma y del registro: la traza tiene
            # que mostrar los argumentos con los que se ejecuto la consulta, que
            # son los que explican la cifra. Ademas hace que dos llamadas que
            # solo difieren en ruido ("linea": "todas" contra omitida) colapsen
            # en la misma firma y no gasten dos pasos.
            argumentos = _limpiar_argumentos(tc.function.name, argumentos)

            firma = tc.function.name + "|" + json.dumps(argumentos, sort_keys=True, default=str)
            if firma in vistas:
                resultado = dict(vistas[firma])
                resultado["_nota"] = (
                    "Ya consultaste esto en este mismo turno con los mismos "
                    "argumentos. El resultado no va a cambiar: responde con lo "
                    "que tienes o cambia de herramienta."
                )
            else:
                resultado = _despachar(tc.function.name, argumentos)
                vistas[firma] = resultado

            traza.registrar(tc.function.name, argumentos, resultado)
            mensajes.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": json.dumps(resultado, ensure_ascii=False, default=str),
            })

    return (
        "Me enredé consultando los datos y prefiero no adivinar. Hazme la "
        "pregunta más concreta (una línea y un rango de fechas), o revisa en la "
        "pestaña Cargar que los reportes de esos turnos ya estén cargados.",
        traza,
        mensajes,
    )


# --- Validador ----------------------------------------------------------------

def _extraer_json(texto: str) -> dict | None:
    """Saca el objeto JSON de la respuesta del auditor.

    Se reimplementa aqui en vez de reusar agent.validator._extraer_json porque
    ese modulo importa google.genai en el tope, y el Copiloto tiene que poder
    correr en una planta sin internet ni dependencias de nube.
    """
    texto = re.sub(r"^```(?:json)?|```$", "", texto.strip(), flags=re.M).strip()
    try:
        return json.loads(texto)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", texto, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
    return None


def _no_auditado(explicacion: str) -> dict:
    """Dictamen cuando el auditor no pudo trabajar.

    Falla en cerrado: verificado False y fundamentada None. Un auditor que no
    corrio no es un aval, y `fundamentada: False` seria mentir en la otra
    direccion —acusar de inventada una respuesta que nadie reviso—. None es el
    unico valor honesto y la UI lo pinta distinto de un rechazo.
    """
    return {
        "verificado": False,
        "fundamentada": None,
        "afirmaciones_sin_respaldo": [],
        "explicacion": explicacion,
    }


def validar(respuesta: str, evidencia_json: str) -> dict:
    """Audita si la respuesta esta respaldada por la evidencia de las herramientas.

    Corre como sub-agente aparte, con temperatura 0 y salida JSON forzada. Es un
    turno limpio a proposito: un modelo que se autoevalua dentro de la misma
    conversacion tiende a darse la razon.
    """
    if not evidencia_json or evidencia_json.strip() in ("", "[]", "{}"):
        # No es un fallo del auditor: es un dictamen que se puede emitir sin
        # llamar al modelo. Una respuesta sobre la planta que no consulto nada
        # sale de la memoria del modelo, y eso es exactamente lo que el prompt
        # prohibe. Por eso verificado True y fundamentada False.
        return {
            "verificado": True,
            "fundamentada": False,
            "afirmaciones_sin_respaldo": [],
            "explicacion": "El agente respondió sin consultar ninguna herramienta.",
        }

    contenido = (
        f"EVIDENCIA (salida de las herramientas):\n{evidencia_json}\n\n"
        f"RESPUESTA a auditar:\n{respuesta}"
    )
    try:
        cliente, modelo = _cliente()
        resp = cliente.chat.completions.create(
            model=modelo,
            messages=[
                {"role": "system", "content": prompts.VALIDADOR},
                {"role": "user", "content": contenido},
            ],
            temperature=0.0,
            response_format={"type": "json_object"},
            **_extra(llm.config_openai()),
        )
        datos = _extraer_json(resp.choices[0].message.content or "")
    except Exception as e:  # noqa: BLE001
        return _no_auditado(f"No se pudo validar ({type(e).__name__}).")

    if not isinstance(datos, dict) or "fundamentada" not in datos:
        return _no_auditado("El validador no devolvió un dictamen legible.")

    sin_respaldo = datos.get("afirmaciones_sin_respaldo") or []
    if not isinstance(sin_respaldo, list):
        return _no_auditado("El validador devolvió las afirmaciones en un formato ilegible.")
    sin_respaldo = [str(a) for a in sin_respaldo if str(a).strip()]
    fundamentada = bool(datos.get("fundamentada"))

    # Reconciliacion en los dos sentidos, porque el auditor tambien es un modelo
    # pequeno y a veces el veredicto no concuerda con la lista:
    #
    #  - Si nombro afirmaciones sin respaldo, manda la lista aunque haya marcado
    #    fundamentada true. En planta, un numero inventado dicho con seguridad
    #    hace que alguien pare una linea que no debia.
    #  - Si dijo "no fundamentada" pero no pudo nombrar ni una afirmacion, no
    #    hay hallazgo: lo que no se puede senalar, se aprueba. Si no, cualquier
    #    respuesta correcta saldria con una alerta encima y la alerta dejaria de
    #    significar algo.
    if sin_respaldo:
        fundamentada = False
        explicacion = datos.get("explicacion") or "Hay afirmaciones sin respaldo en la evidencia."
    elif not fundamentada:
        fundamentada = True
        explicacion = "Sin afirmaciones sin respaldo identificadas."
    else:
        explicacion = datos.get("explicacion") or "Respuesta respaldada por la evidencia."

    return {
        "verificado": True,
        "fundamentada": fundamentada,
        "afirmaciones_sin_respaldo": sin_respaldo,
        "explicacion": str(explicacion),
    }
