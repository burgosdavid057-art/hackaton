"""
Canal WhatsApp para el agente Haceb.

Es un webhook compatible con el sandbox de WhatsApp de Twilio: recibe el mensaje
del usuario, lo pasa por el MISMO agente + validador que la web, y responde en
el formato TwiML que Twilio espera. Es el canal donde de verdad ocurre el
soporte de electrodomesticos en Colombia.

No esta mockeado: procesa un POST real con formato Twilio y produce una
respuesta fundamentada. Se prueba en local con curl (ver abajo) y se conecta a
WhatsApp de verdad con el sandbox de Twilio + un tunel ngrok.

    # Local, sin Twilio:
    python -m channels.whatsapp
    curl -s -X POST localhost:5000/whatsapp \
        --data-urlencode "From=whatsapp:+573001112233" \
        --data-urlencode "Body=mi nevera 9003548 no enfria abajo, que reviso?"

    # En vivo con WhatsApp real:
    #   1) ngrok http 5000
    #   2) en el sandbox de Twilio, "WHEN A MESSAGE COMES IN" -> https://<ngrok>/whatsapp
    #   3) escribe al numero del sandbox desde tu WhatsApp
"""

from __future__ import annotations

import html
from xml.sax.saxutils import escape

from flask import Flask, request, Response

from agent import loop, validator

app = Flask(__name__)

# Memoria por remitente: cada numero de WhatsApp conserva su conversacion.
# En produccion iria a una base; para el demo, en memoria basta.
_memoria: dict[str, list] = {}

# WhatsApp corta mensajes largos; mantenemos las respuestas legibles en el chat.
LIMITE_CHARS = 1400


def _twiml(mensaje: str) -> Response:
    cuerpo = escape(mensaje[:LIMITE_CHARS])
    xml = f'<?xml version="1.0" encoding="UTF-8"?><Response><Message>{cuerpo}</Message></Response>'
    return Response(xml, mimetype="application/xml")


def responder_texto(remitente: str, texto: str) -> str:
    """Corre el agente para un remitente, conservando su memoria."""
    historial = _memoria.get(remitente, [])
    respuesta, traza, historial = loop.responder(texto, historial)
    _memoria[remitente] = historial

    dictamen = validator.validar(respuesta, loop.evidencia_json(traza))
    return validator.aplicar(respuesta, dictamen)


@app.route("/whatsapp", methods=["POST"])
def whatsapp():
    remitente = request.form.get("From", "desconocido")
    cuerpo = (request.form.get("Body") or "").strip()

    if not cuerpo:
        return _twiml("Hola 👋 Soy el asistente de Haceb. Cuéntame qué "
                      "electrodoméstico tienes o qué necesitas.")

    if cuerpo.lower() in ("reiniciar", "reset", "empezar"):
        _memoria.pop(remitente, None)
        return _twiml("Listo, empezamos de nuevo. ¿En qué te ayudo?")

    try:
        respuesta = responder_texto(remitente, cuerpo)
    except Exception as e:
        respuesta = ("Tuve un problema consultando la información. "
                     "Intenta de nuevo o escribe *reiniciar*.")
        app.logger.error("fallo agente: %s", e)

    return _twiml(respuesta)


# Nombres de campo que usan distintos chatbots de WhatsApp para el remitente
# y el texto. El endpoint acepta cualquiera, para conectarse sin fricciones.
_CAMPOS_FROM = ("from", "sender", "phone", "number", "chatId", "chat_id", "wa_id", "author")
_CAMPOS_BODY = ("body", "message", "text", "content", "msg", "prompt")


def _primer_campo(datos: dict, claves) -> str:
    for k in claves:
        v = datos.get(k)
        if isinstance(v, dict):  # a veces viene anidado, ej. {"message":{"text":...}}
            v = v.get("text") or v.get("body") or v.get("content")
        if v:
            return str(v)
    return ""


@app.route("/message", methods=["POST"])
def message():
    """Endpoint JSON para conectar cualquier chatbot de WhatsApp al agente.

    Entrada flexible (acepta from/sender/phone/... y body/message/text/...):
        {"from": "573001112233", "body": "mi nevera no enfria"}
    Salida:
        {"reply": "...", "from": "573001112233"}

    Mantiene memoria por remitente. Escribe 'reiniciar' para limpiarla.
    """
    datos = request.get_json(silent=True) or request.form.to_dict() or {}
    remitente = _primer_campo(datos, _CAMPOS_FROM) or "desconocido"
    cuerpo = _primer_campo(datos, _CAMPOS_BODY).strip()

    def responder(texto):
        # Devuelve varias claves comunes para encajar con distintos chatbots.
        return {"reply": texto, "response": texto, "message": texto, "from": remitente}

    if not cuerpo:
        return responder("Hola 👋 Soy el asistente de electrodomésticos Haceb. "
                         "¿En qué te ayudo con tu equipo?")

    if cuerpo.lower() in ("reiniciar", "reset", "empezar", "/reset"):
        _memoria.pop(remitente, None)
        return responder("Listo, empezamos de nuevo. ¿En qué te ayudo?")

    try:
        texto = responder_texto(remitente, cuerpo)
    except Exception as e:
        app.logger.error("fallo agente: %s", e)
        texto = ("Tuve un problema consultando la información. "
                 "Intenta de nuevo o escribe *reiniciar*.")
    return responder(texto)


@app.route("/", methods=["GET"])
def salud():
    return {"servicio": "agente Haceb WhatsApp", "estado": "ok"}


if __name__ == "__main__":
    print("Webhook WhatsApp en http://localhost:5000/whatsapp")
    app.run(host="0.0.0.0", port=5000, debug=False)
