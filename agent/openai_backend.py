"""
Backend del agente sobre el protocolo OpenAI.

Sirve para cualquier motor que hable ese protocolo:
  - Groq  (nube gratuita, cuota generosa)
  - Ollama (modelo local en la Mac del equipo: sin cuota, sin costo, datos
            que no salen de la red local)

Reusa exactamente las mismas herramientas (agent/tools.py), el mismo prompt de
sistema y la misma clase Traza que el camino Gemini, para que el comportamiento
—y la regla de fundamentacion— sean identicos sin importar el motor.
"""

from __future__ import annotations

import json

from . import llm, tools
from .declarations import herramientas_openai
from .loop import INSTRUCCIONES, MAX_PASOS, Traza

_cliente_cache: dict[str, object] = {}


def _cliente():
    cfg = llm.config_openai()
    clave = cfg["base_url"] + "|" + cfg["api_key"][:8]
    if clave not in _cliente_cache:
        from openai import OpenAI

        _cliente_cache[clave] = OpenAI(
            base_url=cfg["base_url"], api_key=cfg["api_key"], timeout=120.0
        )
    return _cliente_cache[clave], cfg["modelo"]


def _extra(cfg: dict) -> dict:
    """Opciones que Ollama acepta por fuera del estandar OpenAI (ventana de
    contexto). Vacio para Groq, que no las necesita."""
    if cfg.get("proveedor") == "ollama" and cfg.get("num_ctx"):
        return {"extra_body": {"options": {"num_ctx": cfg["num_ctx"]}}}
    return {}


def _mensajes_iniciales(historial, mensaje):
    # historial es una lista de mensajes estilo OpenAI (dicts). Si viene vacio o
    # de otro backend (Content de Gemini), arrancamos limpio con el system.
    ok = isinstance(historial, list) and all(isinstance(m, dict) for m in (historial or []))
    if historial and ok:
        mensajes = list(historial)
    else:
        mensajes = [{"role": "system", "content": INSTRUCCIONES}]
    mensajes.append({"role": "user", "content": mensaje})
    return mensajes


def responder(mensaje: str, historial: list | None = None) -> tuple[str, Traza, list]:
    """Loop de tool calling estilo OpenAI. Mismo contrato que loop.responder."""
    cliente, modelo = _cliente()
    extra = _extra(llm.config_openai())
    mensajes = _mensajes_iniciales(historial, mensaje)
    herramientas = herramientas_openai()
    traza = Traza()

    for _ in range(MAX_PASOS):
        resp = cliente.chat.completions.create(
            model=modelo,
            messages=mensajes,
            tools=herramientas,
            tool_choice="auto",
            temperature=0.2,
            **extra,
        )
        msg = resp.choices[0].message

        if not msg.tool_calls:
            texto = (msg.content or "").strip() or (
                "No pude generar una respuesta. Intenta reformular la pregunta."
            )
            mensajes.append({"role": "assistant", "content": texto})
            return texto, traza, mensajes

        # El modelo pidio herramientas: registrar el turno del asistente y correrlas.
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
            resultado = tools.ejecutar(tc.function.name, argumentos)
            traza.registrar(tc.function.name, argumentos, resultado)
            mensajes.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": json.dumps(resultado, ensure_ascii=False, default=str),
            })

    return (
        "Me enrede consultando la informacion y prefiero no adivinar. "
        "Reformula la pregunta o pide hablar con servicio tecnico.",
        traza,
        mensajes,
    )


INSTRUCCIONES_VALIDADOR = None  # se importa perezosamente para evitar ciclos


def validar(respuesta: str, evidencia_json: str) -> dict:
    """Validador sobre el mismo motor OpenAI. Devuelve el mismo dict que
    agent.validator.validar, con la misma regla de reconciliacion."""
    from .validator import INSTRUCCIONES as INSTR_VAL, _extraer_json

    if not evidencia_json or evidencia_json == "[]":
        # Sin herramientas = respuesta conversacional (saludo, aclaracion o
        # rechazo de tema fuera de alcance). No hay datos de producto que auditar.
        return {
            "verificado": True,
            "fundamentada": True,
            "afirmaciones_sin_respaldo": [],
            "explicacion": "Respuesta conversacional, sin datos de producto que verificar.",
        }

    cliente, modelo = _cliente()
    contenido = (
        f"EVIDENCIA (salida de las herramientas):\n{evidencia_json}\n\n"
        f"RESPUESTA a auditar:\n{respuesta}"
    )
    try:
        resp = cliente.chat.completions.create(
            model=modelo,
            messages=[
                {"role": "system", "content": INSTR_VAL},
                {"role": "user", "content": contenido},
            ],
            temperature=0.0,
            response_format={"type": "json_object"},
            **_extra(llm.config_openai()),
        )
        datos = _extraer_json(resp.choices[0].message.content or "")
    except Exception as e:  # noqa: BLE001
        return {
            "verificado": False,
            "fundamentada": None,
            "afirmaciones_sin_respaldo": [],
            "explicacion": f"No se pudo validar ({type(e).__name__}).",
        }

    if not isinstance(datos, dict) or "fundamentada" not in datos:
        return {
            "verificado": False,
            "fundamentada": None,
            "afirmaciones_sin_respaldo": [],
            "explicacion": "El validador no devolvio un dictamen legible.",
        }

    sin_respaldo = datos.get("afirmaciones_sin_respaldo") or []
    fundamentada = bool(datos.get("fundamentada"))
    if not fundamentada and not sin_respaldo:
        fundamentada = True
        explicacion = "Sin afirmaciones sin respaldo identificadas."
    else:
        explicacion = datos.get("explicacion", "")

    return {
        "verificado": True,
        "fundamentada": fundamentada,
        "afirmaciones_sin_respaldo": sin_respaldo,
        "explicacion": explicacion,
    }
