"""
Las herramientas del agente.

Cada funcion devuelve un dict que SIEMPRE incluye la clave `fuente`, para que
la respuesta final pueda citarse. Si el dato no existe en el catalogo, se
devuelve `disponible: False` y un motivo: el agente no debe rellenar el hueco.

Las declaraciones para el modelo se generan al final del archivo.
"""

from __future__ import annotations

from . import catalog, knowledge

# Tarifa media de energia residencial en Antioquia (COP/kWh), estrato 3-4.
# Es un supuesto del equipo, no un dato de Haceb: se declara como tal.
TARIFA_KWH_COP = 950.0


def _no_encontrado(referencia: str) -> dict:
    return {
        "encontrado": False,
        "motivo": f"No existe el producto '{referencia}' en el catalogo cargado.",
        "sugerencia": "Usa buscar_productos para encontrar la referencia correcta.",
        "fuente": "catalogo Haceb (data/catalog.json)",
    }


def buscar_productos(
    consulta: str = "",
    categoria: str | None = None,
    litros_min: float | None = None,
    litros_max: float | None = None,
    litros_aprox: float | None = None,
    precio_max: float | None = None,
) -> dict:
    """Busca y filtra productos en el catalogo real de Haceb."""
    encontrados = catalog.buscar(
        consulta, categoria, litros_min, litros_max, litros_aprox, precio_max
    )
    respuesta = {
        "encontrado": bool(encontrados),
        "cantidad": len(encontrados),
        "productos": [catalog.resumen(p) for p in encontrados],
        "fuente": "catalogo publico de Haceb (API VTEX)",
    }

    # Si se pidio una capacidad concreta, aclarar si el resultado no la cumple
    # exacta, con la capacidad REAL mas cercana. Asi el agente ofrece el producto
    # real (con su referencia y litros exactos) en vez de inventar una cifra.
    objetivo = litros_aprox or litros_min or litros_max
    if objetivo and encontrados:
        caps = [c for c in (catalog.litros_de(p) for p in encontrados) if c]
        if caps and objetivo not in caps:
            mas_cercana = min(caps, key=lambda c: abs(c - objetivo))
            respuesta["nota"] = (
                f"No hay una nevera de exactamente {objetivo:.0f} litros. La de "
                f"capacidad mas cercana es de {mas_cercana:.0f} litros. Ofrece esa "
                f"(usa su referencia y litros EXACTOS de la lista); NO inventes "
                f"una capacidad ni una referencia que no este aqui."
            )
    return respuesta


def ficha_tecnica(referencia: str) -> dict:
    """Devuelve todas las especificaciones publicadas de un producto."""
    p = catalog.por_referencia(referencia)
    if not p:
        return _no_encontrado(referencia)
    return {
        "encontrado": True,
        "producto": catalog.resumen(p),
        "especificaciones": p["specs"],
        "certificado_conformidad": p.get("certificado_url"),
        "fuente": p["url"] or "catalogo publico de Haceb",
    }


def validar_espacio(
    referencia: str,
    ancho_disponible_cm: float,
    alto_disponible_cm: float,
    profundo_disponible_cm: float | None = None,
    ancho_puerta_cm: float | None = None,
    holgura_cm: float = 5.0,
) -> dict:
    """Verifica si un producto cabe en un espacio y si pasa por la puerta.

    La holgura por defecto (5 cm) es la ventilacion que piden los manuales de
    Haceb para el condensador. Se reporta explicitamente.
    """
    p = catalog.por_referencia(referencia)
    if not p:
        return _no_encontrado(referencia)

    d = p["dim_cm"]
    if not all(d.values()):
        return {
            "encontrado": True,
            "evaluable": False,
            "motivo": "El catalogo no publica las tres dimensiones de este producto.",
            "dimensiones_publicadas": d,
            "fuente": p["url"],
        }

    checks = []

    def check(nombre, requerido, disponible, es_puerta=False):
        if disponible is None:
            return
        cabe = disponible >= requerido
        sobra = round(disponible - requerido, 1)
        # Veredicto en palabras, para que el modelo NO pueda invertir la logica.
        if es_puerta:
            veredicto = (
                f"SÍ PASA por la puerta: el equipo mide {requerido:.0f} cm de ancho "
                f"y la puerta {disponible:.0f} cm (sobran {sobra:.0f} cm)."
                if cabe else
                f"NO PASA por la puerta: el equipo mide {requerido:.0f} cm de ancho "
                f"pero la puerta solo tiene {disponible:.0f} cm (faltan {abs(sobra):.0f} cm)."
            )
        else:
            veredicto = (
                f"CABE en {nombre}: necesita {requerido:.1f} cm y hay {disponible:.0f} cm "
                f"(sobran {sobra:.1f} cm)."
                if cabe else
                f"NO CABE en {nombre}: necesita {requerido:.1f} cm pero solo hay "
                f"{disponible:.0f} cm (faltan {abs(sobra):.1f} cm)."
            )
        checks.append({
            "medida": nombre,
            "necesita_cm": round(requerido, 1),
            "disponible_cm": disponible,
            "cabe": cabe,
            "sobra_cm": sobra,
            "veredicto": veredicto,
        })

    check("ancho", d["ancho"] + holgura_cm, ancho_disponible_cm)
    check("alto", d["alto"] + holgura_cm, alto_disponible_cm)
    check("profundo", d["profundo"] + holgura_cm, profundo_disponible_cm)
    # Para pasar por una puerta basta el ancho del equipo, sin holgura.
    check("paso por la puerta", d["ancho"], ancho_puerta_cm, es_puerta=True)

    problemas = [c for c in checks if not c["cabe"]]
    if not problemas:
        resumen = "CABE en el espacio y pasa por la puerta. Todas las medidas dan."
    else:
        faltas = "; ".join(c["veredicto"] for c in problemas)
        resumen = f"NO se puede instalar tal cual. Problemas: {faltas}"

    return {
        "encontrado": True,
        "evaluable": True,
        "cabe": not problemas,
        "resumen": resumen,
        "dimensiones_producto_cm": d,
        "holgura_aplicada_cm": holgura_cm,
        "verificaciones": checks,
        "problemas": problemas,
        "fuente": p["url"],
    }


def recomendar_para_espacio(
    ancho_disponible_cm: float,
    alto_disponible_cm: float,
    profundo_disponible_cm: float | None = None,
    ancho_puerta_cm: float | None = None,
    categoria: str = "Neveras",
    holgura_cm: float = 5.0,
) -> dict:
    """Devuelve los productos que SÍ caben en un espacio, ordenados por capacidad.

    Calcula el ajuste del lado del servidor sobre todo el catalogo, en vez de
    dejar que el modelo pruebe uno por uno (que es fragil). Ideal cuando el
    usuario pregunta "que nevera me cabe" con las medidas de su cocina.
    """
    candidatos = catalog.buscar("", categoria=categoria, limite=100)

    def _evaluar(ancho, alto):
        caben = []
        for p in candidatos:
            d = p["dim_cm"]
            if not all(d.values()):
                continue
            r = validar_espacio(
                p["referencia"], ancho, alto,
                profundo_disponible_cm, ancho_puerta_cm, holgura_cm,
            )
            if r.get("evaluable") and r["cabe"]:
                caben.append({
                    "referencia": p["referencia"],
                    "nombre": p["nombre"],
                    "litros": catalog.litros_de(p),
                    "dimensiones_cm": d,
                    "precio_cop": p["precio"],
                })
        caben.sort(key=lambda x: -(x["litros"] or 0))
        return caben

    ancho, alto = ancho_disponible_cm, alto_disponible_cm
    caben = _evaluar(ancho, alto)
    nota = None

    # Guarda anti-error del modelo: toda nevera es mas ALTA que ANCHA. Si no
    # cabe ninguna y el "alto" recibido es menor que el "ancho", lo mas probable
    # es que vinieran intercambiados. Se reintenta al reves, de forma transparente.
    if not caben and alto < ancho:
        caben_swap = _evaluar(alto, ancho)
        if caben_swap:
            caben = caben_swap
            ancho, alto = alto, ancho
            nota = (
                f"Interpreté ancho={ancho:.0f} cm y alto={alto:.0f} cm: venían al "
                f"revés (una nevera es más alta que ancha)."
            )

    resultado = {
        "espacio_cm": {
            "ancho": ancho, "alto": alto,
            "profundo": profundo_disponible_cm, "puerta": ancho_puerta_cm,
        },
        "holgura_aplicada_cm": holgura_cm,
        "hay_opciones": bool(caben),
        "cuantas_caben": len(caben),
        "caben": caben[:6],
        "mensaje": (
            f"Encontré {len(caben)} {categoria.lower()} que caben; la de mayor "
            f"capacidad que cabe es de {caben[0]['litros']:.0f} litros."
            if caben else
            f"Ninguna {categoria.lower().rstrip('s')} del catálogo cabe en ese "
            f"espacio con {holgura_cm:.0f} cm de holgura."
        ),
        "fuente": "catalogo publico de Haceb",
    }
    if nota:
        resultado["nota"] = nota
    return resultado


def costo_energia(
    referencia: str,
    anios: float = 10.0,
    tarifa_kwh_cop: float | None = None,
) -> dict:
    """Calcula el costo electrico acumulado y el costo total de propiedad."""
    p = catalog.por_referencia(referencia)
    if not p:
        return _no_encontrado(referencia)

    kwh_mes = p.get("consumo_kwh_mes")
    if kwh_mes is None:
        return {
            "encontrado": True,
            "calculable": False,
            "motivo": "El catalogo no publica el consumo energetico de este producto.",
            "fuente": p["url"],
        }

    tarifa = tarifa_kwh_cop or TARIFA_KWH_COP
    meses = anios * 12
    costo_energia_cop = kwh_mes * meses * tarifa
    precio = p["precio"] or 0

    return {
        "encontrado": True,
        "calculable": True,
        "consumo_kwh_mes": kwh_mes,
        "clase_energetica": p["clase_energetica"],
        "anios": anios,
        "tarifa_kwh_cop": tarifa,
        "tarifa_es_supuesto_del_equipo": tarifa_kwh_cop is None,
        "precio_compra_cop": precio,
        "costo_energia_cop": round(costo_energia_cop),
        "costo_total_propiedad_cop": round(precio + costo_energia_cop),
        "fuente": f"consumo publicado en {p['url']}",
    }


def comparar_costo_total(referencias: list[str], anios: float = 10.0) -> dict:
    """Compara varios productos por costo total de propiedad, no por precio."""
    filas = []
    for ref in referencias:
        r = costo_energia(ref, anios)
        if r.get("calculable"):
            p = catalog.por_referencia(ref)
            filas.append({
                "referencia": ref,
                "nombre": p["nombre"],
                "precio_compra_cop": r["precio_compra_cop"],
                "costo_energia_cop": r["costo_energia_cop"],
                "costo_total_cop": r["costo_total_propiedad_cop"],
                "clase_energetica": r["clase_energetica"],
            })
    filas.sort(key=lambda f: f["costo_total_cop"])
    resultado = {
        "comparados": len(filas),
        "anios": anios,
        "ranking_por_costo_total": filas,
        "fuente": "catalogo publico de Haceb",
    }
    if len(filas) >= 2 and filas[0]["precio_compra_cop"] > filas[-1]["precio_compra_cop"]:
        resultado["hallazgo"] = (
            "El de menor costo total NO es el mas barato de comprar: "
            "el ahorro de energia compensa la diferencia."
        )
    return resultado


def consultar_manual(referencia: str, pregunta: str) -> dict:
    """Busca en el manual de usuario real del producto (RAG)."""
    p = catalog.por_referencia(referencia)
    if not p:
        return _no_encontrado(referencia)
    if not p.get("manual_file"):
        return {
            "encontrado": True,
            "hay_manual": False,
            "motivo": "Este producto no tiene manual publicado en el catalogo.",
            "fuente": p["url"],
        }
    pasajes = knowledge.buscar(pregunta, p["manual_file"])
    return {
        "encontrado": True,
        "hay_manual": True,
        "pasajes": pasajes,
        "sin_resultados": not pasajes,
        "fuente": f"Manual de usuario Haceb — {p['nombre']}",
        "manual_url": p.get("manual_url"),
    }


def _parsear_garantia(texto: str) -> dict[str, int]:
    """Convierte el texto legal de garantia en {parte: anios}.

    Ejemplo real de Haceb:
        "10 años* en el compresor y 1 años en los demas componentes,
         *1 año correspondiente a garantia legal y 9 años a garantia
         suplementaria."

    Ojo con dos trampas del texto: el asterisco pegado a "años" y las
    clausulas al final que son notas al pie sobre el desglose legal, no
    coberturas de partes distintas. Si se cuelan, el agente responde mal.
    """
    import re

    # Notas al pie sobre el desglose legal. No son partes cubiertas.
    PIE = r"garant[ií]a\s+(?:legal|suplementaria)|primer\s+año\s+corresponde|corresponde\s+a\s+(?:la\s+)?garant"
    # Corta el pie de pagina cuando viene pegado a la clausula sin coma:
    # "5 años* en las tarjetas *el primer año corresponde a la garantia legal"
    CORTE_PIE = re.compile(rf"\*(?=[^*]*(?:{PIE}))", re.I)

    cobertura: dict[str, int] = {}
    # El separador " y " puede venir en mayuscula ("... el motor Y 5 años ...").
    clausulas = re.split(r"\s*(?:,|;|\sy\s)\s*", texto, flags=re.I)

    for clausula in clausulas:
        # Quita comillas y asteriscos de adorno en los extremos. Ojo: NO se
        # puede descartar una clausula por empezar con '*', porque hay fichas
        # que escriben la cobertura real como "**10 años en el compresor".
        c = CORTE_PIE.split(clausula, maxsplit=1)[0]
        c = c.strip().strip('"“”').strip()
        if not c or re.search(PIE, c, re.I):
            continue

        m = re.match(r"[*\s]*(\d+)\s*años?\**\s*(.*)", c, re.I)
        if not m:
            continue

        anios = int(m.group(1))
        parte = m.group(2).strip()
        # "garantia en el motor" -> "motor" ; "de garantia en todos sus
        # componentes" -> "todos sus componentes"
        parte = re.sub(r"^(?:de\s+)?garant[ií]a\s*", "", parte, flags=re.I)
        parte = re.sub(r"^en\s+", "", parte, flags=re.I)
        parte = re.sub(r"^(?:el|la|los|las)\s+", "", parte, flags=re.I)
        parte = parte.strip(" .*\"“”")

        if parte:
            cobertura.setdefault(parte.lower(), anios)
    return cobertura


def verificar_garantia(
    referencia: str,
    componente: str,
    anios_de_uso: float,
) -> dict:
    """Determina si un componente sigue cubierto por la garantia publicada."""
    p = catalog.por_referencia(referencia)
    if not p:
        return _no_encontrado(referencia)

    texto = p["specs"].get("Garantía")
    if not texto:
        return {
            "encontrado": True,
            "determinable": False,
            "motivo": "El catalogo no publica los terminos de garantia de este producto.",
            "fuente": p["url"],
        }

    cobertura = _parsear_garantia(texto)
    comp = catalog.normaliza(componente)

    # 1) coincidencia directa con una parte nombrada (ej. "compresor")
    aplica = None
    for parte, anios in cobertura.items():
        np = catalog.normaliza(parte)
        if np in ("demas componentes", "componentes"):
            continue
        if comp in np or np in comp:
            aplica = (parte, anios)
            break

    # 2) si no se nombra, aplica el cajon por defecto ("los demas componentes")
    if aplica is None:
        for parte, anios in cobertura.items():
            if "demas" in catalog.normaliza(parte) or "component" in catalog.normaliza(parte):
                aplica = (parte, anios)
                break

    if aplica is None:
        return {
            "encontrado": True,
            "determinable": False,
            "motivo": f"No se pudo mapear '{componente}' a los terminos publicados.",
            "texto_garantia": texto,
            "fuente": p["url"],
        }

    parte, anios = aplica
    return {
        "encontrado": True,
        "determinable": True,
        "componente_consultado": componente,
        "cubierto_bajo": parte,
        "cobertura_anios": anios,
        "anios_de_uso": anios_de_uso,
        "en_garantia": anios_de_uso <= anios,
        "texto_garantia": texto,
        "fuente": p["url"],
    }


def escalar_a_servicio_tecnico(motivo: str, referencia: str | None = None) -> dict:
    """Deriva a un tecnico humano cuando el agente no puede responder con fundamento.

    Se usa deliberadamente cuando hace falta identificar un repuesto: el
    catalogo publico de Haceb no publica datos de compatibilidad de repuestos,
    asi que afirmar cual sirve seria inventarlo.
    """
    p = catalog.por_referencia(referencia) if referencia else None
    return {
        "escalado": True,
        "motivo": motivo,
        "producto": p["nombre"] if p else None,
        "referencia": p["referencia"] if p else referencia,
        "canal": "Linea de servicio Haceb 01 8000 51 22 22 · haceb.com/servicio-tecnico",
        "nota": (
            "El catalogo publico no incluye compatibilidad de repuestos, "
            "por eso no se afirma cual corresponde."
        ),
        "fuente": "politica del agente: no afirmar sin dato verificable",
    }


# --- Registro: nombre -> funcion ---------------------------------------------

DISPONIBLES = {
    "buscar_productos": buscar_productos,
    "ficha_tecnica": ficha_tecnica,
    "validar_espacio": validar_espacio,
    "recomendar_para_espacio": recomendar_para_espacio,
    "costo_energia": costo_energia,
    "comparar_costo_total": comparar_costo_total,
    "consultar_manual": consultar_manual,
    "verificar_garantia": verificar_garantia,
    "escalar_a_servicio_tecnico": escalar_a_servicio_tecnico,
}


def ejecutar(nombre: str, argumentos: dict) -> dict:
    """Ejecuta una herramienta por nombre, capturando errores."""
    fn = DISPONIBLES.get(nombre)
    if fn is None:
        return {"error": f"herramienta desconocida: {nombre}"}
    try:
        return fn(**argumentos)
    except TypeError as e:
        return {"error": f"argumentos invalidos para {nombre}: {e}"}
    except Exception as e:
        return {"error": f"fallo {nombre}: {type(e).__name__}: {e}"}
