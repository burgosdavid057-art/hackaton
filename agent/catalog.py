"""
Acceso al catalogo de Haceb ya ingerido.

Toda herramienta del agente lee de aqui. Ningun dato se inventa: si un campo
no existe en la fuente, se devuelve None y el agente debe decirlo.
"""

from __future__ import annotations

import json
import re
import unicodedata
from functools import lru_cache
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
CATALOGO = RAIZ / "data" / "catalog.json"


def normaliza(texto: str) -> str:
    """minusculas sin tildes, para comparar sin sorpresas"""
    texto = unicodedata.normalize("NFD", (texto or "").lower())
    return "".join(c for c in texto if unicodedata.category(c) != "Mn")


@lru_cache(maxsize=1)
def productos() -> list[dict]:
    if not CATALOGO.exists():
        raise FileNotFoundError(
            f"No existe {CATALOGO}. Corre primero:  python ingest.py"
        )
    return json.loads(CATALOGO.read_text(encoding="utf-8"))


def por_referencia(referencia: str) -> dict | None:
    ref = normaliza(referencia).strip()
    for p in productos():
        if normaliza(p["referencia"]) == ref or p["id"] == referencia:
            return p
    # busqueda tolerante: la referencia aparece dentro del nombre
    for p in productos():
        if ref and ref in normaliza(p["nombre"]):
            return p
    return None


def buscar(
    consulta: str = "",
    categoria: str | None = None,
    litros_min: float | None = None,
    litros_max: float | None = None,
    litros_aprox: float | None = None,
    precio_max: float | None = None,
    limite: int = 6,
) -> list[dict]:
    """Busca y FILTRA productos por texto, categoria, precio y capacidad.

    Capacidad (todos SUAVES: si nada calza exacto, devuelve lo mas cercano, para
    que el agente pueda decir "no hay de X, lo mas cercano es Y" en vez de
    "no existe"):
      - litros_aprox: capacidad deseada aproximada ("de 400 litros") -> ordena
        por cercania a ese valor.
      - litros_min: capacidad minima ("mas de 400", "al menos 400").
      - litros_max: capacidad maxima ("menos de 400", "hasta 400").
    """
    terminos = [t for t in re.split(r"\W+", normaliza(consulta)) if len(t) > 2]
    genericas = {"nevera", "neveras", "lavadora", "lavadoras", "congelador",
                 "congeladores", "electrodomestico", "litros", "litro"}
    utiles = [t for t in terminos if t not in genericas]

    candidatos = []
    for p in productos():
        if categoria and normaliza(categoria) not in normaliza(p["categoria"]):
            continue
        if precio_max and (p["precio"] or 0) > precio_max:
            continue
        heno = normaliza(
            p["nombre"] + " " + p["descripcion"] + " " + " ".join(p["specs"].values())
        )
        puntaje = sum(2 for t in utiles if t in heno)
        puntaje += sum(1 for t in terminos if t in genericas and t in heno)
        if terminos and puntaje == 0:
            continue
        candidatos.append((puntaje, litros_de(p), p))

    if not candidatos:
        return []

    con_litros = [c for c in candidatos if c[1] is not None]

    # 1) Capacidad aproximada: ordenar por cercania al valor deseado.
    if litros_aprox is not None and con_litros:
        con_litros.sort(key=lambda c: (abs(c[1] - litros_aprox), -c[0]))
        return [c[2] for c in con_litros[:limite]]

    # 2) Rango [min, max]: filtro suave.
    if (litros_min is not None or litros_max is not None) and con_litros:
        def en_rango(v):
            if litros_min is not None and v < litros_min:
                return False
            if litros_max is not None and v > litros_max:
                return False
            return True

        dentro = [c for c in con_litros if en_rango(c[1])]
        if dentro:
            dentro.sort(key=lambda x: (-x[0], -(x[1] or 0)))
            return [c[2] for c in dentro[:limite]]
        # Nada en el rango: devolver lo mas cercano al limite pedido.
        objetivo = litros_min if litros_min is not None else litros_max
        con_litros.sort(key=lambda c: (abs(c[1] - objetivo), -c[0]))
        return [c[2] for c in con_litros[:limite]]

    # 3) Sin criterio de capacidad: por relevancia de texto.
    candidatos.sort(key=lambda x: (-x[0], -(x[2]["precio"] or 0)))
    return [c[2] for c in candidatos[:limite]]


def litros_de(p: dict) -> float | None:
    """Capacidad en litros que el CLIENTE reconoce.

    El numero que el cliente conoce y pregunta es el del NOMBRE del producto
    (capacidad bruta / de marketing): "Nevera ... 404 Litros ...". Se prefiere
    ese; si no, la capacidad bruta y luego la neta de las especificaciones. Asi,
    si el cliente pide "404 litros", encuentra la nevera que Haceb vende como de
    404 litros, aunque su capacidad neta publicada sea otra.
    """
    m = re.search(r"(\d{2,4})\s*(?:litros|lts|l\b)", normaliza(p.get("nombre", "")))
    if m:
        return float(m.group(1))

    litros = (
        p["specs"].get("Capacidad bruta En Litros")
        or p["specs"].get("Capacidad neta en litros")
        or p["specs"].get("Capacidad de lavado")
    )
    if not litros:
        return None
    m = re.search(r"(\d+)", str(litros))
    return float(m.group(1)) if m else None


def resumen(p: dict) -> dict:
    """Vista compacta de un producto, la que ve el modelo."""
    return {
        "referencia": p["referencia"],
        "nombre": p["nombre"],
        "categoria": p["categoria"],
        "precio_cop": p["precio"],
        "disponible": p["disponible"],
        "dimensiones_cm": p["dim_cm"],
        "peso_kg": p["peso_kg"],
        "consumo_kwh_mes": p["consumo_kwh_mes"],
        "clase_energetica": p["clase_energetica"],
        "garantia": p["specs"].get("Garantía"),
        "tiene_manual": bool(p.get("manual_file")),
        "url": p["url"],
    }
