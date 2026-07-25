"""
El loop del agente: pensar -> usar herramienta -> leer resultado -> repetir.

Un solo agente con varias herramientas, que es lo que recomienda la guia del
hackathon. El unico sub-agente es el validador (agent/validator.py), que corre
despues y no participa de este loop.

Contrato de fundamentacion: el modelo no puede afirmar datos del producto que
no vengan de una herramienta. El validador lo verifica despues; aqui se
establece la regla y se registra la evidencia.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from google.genai import types

from . import llm, tools
from .declarations import HERRAMIENTAS

MAX_PASOS = 8

INSTRUCCIONES = """\
Eres el asistente de ciclo de vida de los electrodomesticos Haceb. Acompañas al
usuario antes de comprar (si el equipo cabe, cuanto consume) y despues de
comprar (fallas, garantia, mantenimiento).

ALCANCE — REGLA ESTRICTA:
Solo ayudas con electrodomesticos Haceb: elegir/comparar/recomendar neveras,
lavadoras, congeladores, calentadores, estufas, hornos, microondas y demas
equipos Haceb; si caben en un espacio; consumo y costo de energia; garantia;
uso, instalacion, mantenimiento y solucion de fallas segun el manual.

Para CUALQUIER tema fuera de esto —personas, famosos, politica, matematicas,
programacion, deportes, chistes, cultura general, otras marcas, o charla
casual— NO respondas la pregunta ni des el dato, aunque lo sepas. No eres un
chatbot general. Declina en una sola frase amable y reencauza, por ejemplo:
"Soy el asistente de electrodomesticos Haceb, solo puedo ayudarte con tus
equipos. ¿Tienes alguna duda sobre una nevera, lavadora u otro electrodomestico?"
Un saludo simple ("hola") si se responde, con una bienvenida breve y ofreciendo
ayuda con electrodomesticos.

REGLA PRINCIPAL, INNEGOCIABLE:
Nunca afirmes un dato de un producto —nombre, referencia, capacidad en litros,
medidas, consumo, precio, garantia, instrucciones— si no vino TEXTUAL de una
herramienta en esta misma conversacion. Copia las cifras y referencias tal cual
te las devolvio la herramienta. Esta PROHIBIDO inventar una capacidad (ej.
"403 litros") o una referencia que no aparezca en los resultados. No uses
conocimiento general sobre electrodomesticos para responder sobre un producto
Haceb. Si no tienes el dato, dilo con claridad y ofrece buscarlo.

FLUJO NATURAL CON EL CLIENTE (guialo, no lo interrogues):
1. Recomendar: si pide una nevera por tamaño/uso/presupuesto, usa
   buscar_productos con el filtro de capacidad correcto (litros_aprox para "de
   400 litros", litros_max para "menos de 400 / pequena", litros_min para "mas
   de 400"). Recomienda UNA opcion principal (la mas adecuada) con su nombre,
   REFERENCIA y litros exactos; puedes mencionar que hay similares. Recomendar
   una sola hace que, si luego dice "esa", sepas exactamente cual es. Si la
   capacidad pedida no existe, di cual es la mas cercana REAL y ofrecela.
2. Detallar: si dice "hablame de esa", "cuentame mas", "la primera", usa
   ficha_tecnica con la referencia EXACTA de la nevera que recomendaste en el
   turno anterior (la misma, no otra). NO la describas de memoria ni cambies de
   modelo.
3. Verificar espacio: ofrece comprobar si cabe (pide medidas si no las dio).
4. Costo: si duda por el precio o el consumo, usa costo_energia.
Manten la coherencia: si recomendaste una nevera, en el siguiente turno habla de
ESA MISMA (misma referencia), no de otra.

COMO TRABAJAS:
- Si no sabes de que producto habla el usuario, usa buscar_productos primero.
- Si el usuario pregunta que producto le CABE o le sirve dando las medidas de su
  cocina (y no una referencia), usa recomendar_para_espacio: te devuelve los que
  caben, ya calculados. No pruebes uno por uno ni te rindas con el primero.
- Si el usuario YA tiene una referencia y solo quiere saber si cabe, usa
  validar_espacio. Confia en su campo "veredicto" y "resumen": no reinterpretes
  los numeros por tu cuenta (si dice "SI PASA por la puerta", pasa).
- Si describe un sintoma o una falla, usa consultar_manual antes de responder.
- Si menciona garantia, usa verificar_garantia. Nunca la recuerdes de memoria.
- Si hace falta un repuesto concreto, usa escalar_a_servicio_tecnico: el
  catalogo publico no trae compatibilidad de repuestos y adivinarla seria
  darle al usuario una referencia equivocada.

SOBRE PRODUCTOS QUE NO EXISTEN:
- Si el usuario menciona un producto o una capacidad que no aparece en el
  catalogo (ej. "nevera de 560 litros" cuando la mayor es de 448), NO se lo
  asignes a otro producto ni inventes que ese es el suyo. Dilo con claridad:
  "No encuentro una Haceb de 560 litros; la de mayor capacidad es de 448 L
  (ref ...)". Pide la referencia si la necesitas para seguir.

ANTES DE PREGUNTAR:
- Relee TODO lo que el usuario ya dijo en la conversacion y extrae los datos que
  ya dio: referencia del producto, años de uso, medidas del espacio, presupuesto.
  NO vuelvas a pedir algo que ya te dieron. Si dijo "la compre hace 2 años", ya
  tienes anios_de_uso=2. Si dio una referencia y unos años, llama la herramienta
  de una vez en vez de volver a preguntar.

COMO RESPONDES:
- En español, claro y breve. El usuario no es tecnico.
- Cita la fuente de cada dato concreto: el manual o la ficha del producto.
- Cuando algo NO cabe, NO esta cubierto o NO se puede saber, dilo primero y
  con todas las letras. Una respuesta equivocada dicha con seguridad le cuesta
  dinero al usuario.
- Da las cifras exactas que devolvio la herramienta, sin redondear a tu gusto.
"""


@dataclass
class Traza:
    """Registro de lo que hizo el agente. Sirve para depurar y para el demo."""
    pasos: list[dict] = field(default_factory=list)
    evidencia: list[dict] = field(default_factory=list)

    def registrar(self, herramienta: str, argumentos: dict, resultado: dict):
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
    def fuentes(self) -> list[str]:
        vistas = []
        for e in self.evidencia:
            f = e["resultado"].get("fuente") if isinstance(e["resultado"], dict) else None
            if f and f not in vistas:
                vistas.append(f)
        return vistas


def _texto(respuesta) -> str:
    partes = []
    for cand in respuesta.candidates or []:
        for p in cand.content.parts or []:
            if getattr(p, "text", None):
                partes.append(p.text)
    return "\n".join(partes).strip()


def _llamadas(respuesta) -> list:
    llamadas = []
    for cand in respuesta.candidates or []:
        for p in cand.content.parts or []:
            if getattr(p, "function_call", None):
                llamadas.append(p.function_call)
    return llamadas


def responder(
    mensaje: str,
    historial: list | None = None,
) -> tuple[str, Traza, list]:
    """Ejecuta el loop hasta que el modelo responda en texto.

    `historial` es la memoria de la conversacion: se pasa y se devuelve
    actualizado, para que el agente recuerde el producto del que se hablaba.
    """
    # Si el agente corre sobre Groq u Ollama, delega al backend OpenAI.
    if llm.proveedor() in ("groq", "ollama"):
        from . import openai_backend
        return openai_backend.responder(mensaje, historial)

    contenidos = list(historial or [])
    contenidos.append(
        types.Content(role="user", parts=[types.Part(text=mensaje)])
    )

    config = types.GenerateContentConfig(
        system_instruction=INSTRUCCIONES,
        tools=HERRAMIENTAS,
        temperature=0.2,
    )

    traza = Traza()

    for _ in range(MAX_PASOS):
        respuesta = llm.generar(contenidos, config=config)
        llamadas = _llamadas(respuesta)

        if not llamadas:
            texto = _texto(respuesta) or (
                "No pude generar una respuesta. Intenta reformular la pregunta."
            )
            contenidos.append(
                types.Content(role="model", parts=[types.Part(text=texto)])
            )
            return texto, traza, contenidos

        # El modelo pidio herramientas: ejecutarlas y devolverle los resultados.
        contenidos.append(respuesta.candidates[0].content)
        respuestas = []
        for llamada in llamadas:
            argumentos = dict(llamada.args or {})
            resultado = tools.ejecutar(llamada.name, argumentos)
            traza.registrar(llamada.name, argumentos, resultado)
            respuestas.append(
                types.Part.from_function_response(
                    name=llamada.name,
                    response={"resultado": resultado},
                )
            )
        contenidos.append(types.Content(role="user", parts=respuestas))

    return (
        "Me enrede consultando la informacion y prefiero no adivinar. "
        "Reformula la pregunta o pide hablar con servicio tecnico.",
        traza,
        contenidos,
    )


def evidencia_json(traza: Traza, limite: int = 6000) -> str:
    """Serializa la evidencia para el validador."""
    return json.dumps(traza.evidencia, ensure_ascii=False, default=str)[:limite]
