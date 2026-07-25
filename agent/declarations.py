"""
Declaraciones de las herramientas, en un formato neutral por proveedor.

La especificacion se escribe una sola vez (ESPECIFICACIONES) y de ahi se
generan los esquemas de Gemini y de Groq/OpenAI. Asi el agente corre con
cualquiera de los dos proveedores sin duplicar las descripciones — que son
parte del prompt y deciden si el modelo elige bien la herramienta.
"""

from __future__ import annotations

# Formato neutral: cada parametro es {nombre: (tipo, descripcion)}.
# tipos: "string" | "number" | "array:string"
ESPECIFICACIONES = [
    {
        "nombre": "buscar_productos",
        "descripcion": (
            "Busca electrodomesticos en el catalogo real de Haceb. Usala siempre "
            "primero cuando el usuario no ha dado una referencia concreta, para "
            "identificar de que producto se habla."
        ),
        "parametros": {
            "consulta": ("string", "que busca el usuario, en sus palabras"),
            "categoria": ("string", "Neveras, Lavadoras, Congeladores"),
            "litros_aprox": ("number", "capacidad aproximada deseada, ej. 'de 400 litros' -> 400"),
            "litros_min": ("number", "capacidad MINIMA: 'mas de 400', 'al menos 400'"),
            "litros_max": ("number", "capacidad MAXIMA: 'menos de 400', 'hasta 400', 'pequena'"),
            "precio_max": ("number", "presupuesto maximo en pesos"),
        },
        "requeridos": ["consulta"],
    },
    {
        "nombre": "ficha_tecnica",
        "descripcion": (
            "Devuelve todas las especificaciones publicadas de un producto por su "
            "referencia. Usala cuando necesites un dato tecnico exacto."
        ),
        "parametros": {
            "referencia": ("string", "referencia del producto, ej. 9003189"),
        },
        "requeridos": ["referencia"],
    },
    {
        "nombre": "validar_espacio",
        "descripcion": (
            "Verifica si un producto cabe en un espacio dado y si pasa por la "
            "puerta. Usala SIEMPRE que el usuario mencione medidas de su cocina, "
            "un hueco, un nicho o una puerta. Nunca estimes tu si cabe: llama a "
            "esta herramienta."
        ),
        "parametros": {
            "referencia": ("string", "referencia del producto"),
            "ancho_disponible_cm": ("number", "ancho del espacio en cm"),
            "alto_disponible_cm": ("number", "alto del espacio en cm"),
            "profundo_disponible_cm": ("number", "profundo del espacio en cm"),
            "ancho_puerta_cm": ("number", "ancho de la puerta por la que debe entrar"),
        },
        "requeridos": ["referencia", "ancho_disponible_cm", "alto_disponible_cm"],
    },
    {
        "nombre": "recomendar_para_espacio",
        "descripcion": (
            "Devuelve TODOS los productos que caben en un espacio dado, ya "
            "calculado. USA ESTA, no validar_espacio, cuando el usuario pregunta "
            "'qué nevera me cabe / me sirve' con las medidas de su cocina y NO ha "
            "dado una referencia. Evita tener que probar producto por producto."
        ),
        "parametros": {
            "ancho_disponible_cm": ("number", "ancho del espacio en cm"),
            "alto_disponible_cm": ("number", "alto del espacio en cm"),
            "profundo_disponible_cm": ("number", "profundo del espacio en cm"),
            "ancho_puerta_cm": ("number", "ancho de la puerta por la que debe entrar"),
            "categoria": ("string", "Neveras (por defecto), Lavadoras, Congeladores"),
        },
        "requeridos": ["ancho_disponible_cm", "alto_disponible_cm"],
    },
    {
        "nombre": "costo_energia",
        "descripcion": (
            "Calcula cuanto cuesta la electricidad de un producto en varios años y "
            "su costo total de propiedad (precio + energia). Usala cuando "
            "pregunten por consumo, ahorro, cuanto gasta o si conviene."
        ),
        "parametros": {
            "referencia": ("string", "referencia del producto"),
            "anios": ("number", "horizonte en años, por defecto 10"),
            "tarifa_kwh_cop": ("number", "tarifa real si el usuario la sabe"),
        },
        "requeridos": ["referencia"],
    },
    {
        "nombre": "comparar_costo_total",
        "descripcion": (
            "Compara varios productos por costo total de propiedad en vez de por "
            "precio de lista. Usala cuando el usuario dude entre dos o mas modelos."
        ),
        "parametros": {
            "referencias": ("array:string", "lista de referencias a comparar"),
            "anios": ("number", "horizonte en años"),
        },
        "requeridos": ["referencias"],
    },
    {
        "nombre": "consultar_manual",
        "descripcion": (
            "Busca en el manual de usuario oficial del producto. Usala SIEMPRE que "
            "el usuario describa un sintoma, una falla, un ruido, un error, o "
            "pregunte como usar, instalar o limpiar algo. Es la unica fuente "
            "valida para instrucciones y diagnostico."
        ),
        "parametros": {
            "referencia": ("string", "referencia del producto"),
            "pregunta": ("string", "el sintoma o duda, en palabras del usuario"),
        },
        "requeridos": ["referencia", "pregunta"],
    },
    {
        "nombre": "verificar_garantia",
        "descripcion": (
            "Determina si un componente sigue cubierto por la garantia segun los "
            "terminos publicados. Usala SIEMPRE que se mencione garantia, y nunca "
            "afirmes de memoria cuantos años cubre algo."
        ),
        "parametros": {
            "referencia": ("string", "referencia del producto"),
            "componente": ("string", "compresor, motor, termostato, bandeja..."),
            "anios_de_uso": ("number", "antiguedad del equipo en años"),
        },
        "requeridos": ["referencia", "componente", "anios_de_uso"],
    },
    {
        "nombre": "escalar_a_servicio_tecnico",
        "descripcion": (
            "Deriva a un tecnico humano. Usala obligatoriamente cuando haga falta "
            "identificar un repuesto concreto: el catalogo publico no trae datos "
            "de compatibilidad, asi que afirmar cual sirve seria inventarlo. Usala "
            "tambien cuando el manual no resuelva el problema."
        ),
        "parametros": {
            "motivo": ("string", "por que se escala"),
            "referencia": ("string", "referencia del producto, si se conoce"),
        },
        "requeridos": ["motivo"],
    },
]


# --- Esquema para Gemini (google-genai) --------------------------------------

def _gemini_tipo(tipo: str):
    return {
        "string": "STRING",
        "number": "NUMBER",
        "array:string": "ARRAY",
    }[tipo]


def herramientas_gemini():
    from google.genai import types

    decls = []
    for esp in ESPECIFICACIONES:
        props = {}
        for nombre, (tipo, desc) in esp["parametros"].items():
            if tipo == "array:string":
                props[nombre] = {"type": "ARRAY", "items": {"type": "STRING"}, "description": desc}
            else:
                props[nombre] = {"type": _gemini_tipo(tipo), "description": desc}
        decls.append(
            types.FunctionDeclaration(
                name=esp["nombre"],
                description=esp["descripcion"],
                parameters={"type": "OBJECT", "properties": props, "required": esp["requeridos"]},
            )
        )
    return [types.Tool(function_declarations=decls)]


# --- Esquema para Groq / OpenAI ----------------------------------------------

def _openai_tipo(tipo: str) -> str:
    return {"string": "string", "number": "number", "array:string": "array"}[tipo]


def herramientas_openai():
    tools = []
    for esp in ESPECIFICACIONES:
        props = {}
        for nombre, (tipo, desc) in esp["parametros"].items():
            if tipo == "array:string":
                props[nombre] = {"type": "array", "items": {"type": "string"}, "description": desc}
            else:
                props[nombre] = {"type": _openai_tipo(tipo), "description": desc}
        tools.append({
            "type": "function",
            "function": {
                "name": esp["nombre"],
                "description": esp["descripcion"],
                "parameters": {
                    "type": "object",
                    "properties": props,
                    "required": esp["requeridos"],
                },
            },
        })
    return tools


# Compatibilidad: el codigo Gemini existente importa HERRAMIENTAS.
HERRAMIENTAS = herramientas_gemini()
