"""Configuracion compartida de pytest.

Pone la raiz del repo en sys.path (para importar `fontfix` y el paquete
`agent`) y ofrece un catalogo sintetico determinista, para que los tests no
dependan de descargar el catalogo real ni de ninguna API externa.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

RAIZ = pathlib.Path(__file__).resolve().parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))


# Catalogo minimo que ejercita todas las ramas de la logica de herramientas:
#   - AL404: nevera completa, con litros en el nombre y garantia de compresor.
#   - AL350: mas barata de comprar pero mas cara de operar (para costo total).
#   - CG200: sin una dimension y sin consumo publicado (ramas "no evaluable").
#   - LV20 : lavadora sin litros en el nombre (capacidad desde specs).
PRODUCTOS = [
    {
        "id": "1001",
        "referencia": "AL404",
        "nombre": "Nevera No Frost 404 Litros Inverter",
        "categoria": "Neveras",
        "descripcion": "Nevera de dos puertas con tecnologia inverter",
        "precio": 2_500_000,
        "disponible": True,
        "dim_cm": {"ancho": 70.0, "alto": 180.0, "profundo": 70.0},
        "peso_kg": 65.0,
        "consumo_kwh_mes": 10.0,
        "clase_energetica": "A",
        "specs": {
            "Garantía": (
                "10 años* en el compresor y 1 años en los demas componentes, "
                "*1 año correspondiente a garantia legal y 9 años a garantia "
                "suplementaria."
            ),
            "Capacidad bruta En Litros": "410",
        },
        "url": "https://haceb.com/nevera-404",
        "certificado_url": "https://haceb.com/cert-404.pdf",
        "manual_file": "data/manuals/nevera-404.txt",
        "manual_url": "https://haceb.com/manual-404.pdf",
    },
    {
        "id": "1002",
        "referencia": "AL350",
        "nombre": "Nevera Congelador Superior",
        "categoria": "Neveras",
        "descripcion": "Nevera economica de una puerta",
        "precio": 2_000_000,
        "disponible": True,
        "dim_cm": {"ancho": 60.0, "alto": 150.0, "profundo": 65.0},
        "peso_kg": 50.0,
        "consumo_kwh_mes": 30.0,
        "clase_energetica": "C",
        "specs": {
            "Garantía": "1 año en todos sus componentes.",
            "Capacidad neta en litros": "350",
        },
        "url": "https://haceb.com/nevera-350",
        "certificado_url": None,
        "manual_file": None,
        "manual_url": None,
    },
    {
        "id": "1003",
        "referencia": "CG200",
        "nombre": "Congelador Horizontal",
        "categoria": "Congeladores",
        "descripcion": "Congelador tipo baul",
        "precio": 1_500_000,
        "disponible": False,
        "dim_cm": {"ancho": 90.0, "alto": None, "profundo": 60.0},
        "peso_kg": 40.0,
        "consumo_kwh_mes": None,
        "clase_energetica": None,
        "specs": {
            "Garantía": (
                "5 años* en las tarjetas *el primer año corresponde a la "
                "garantia legal"
            ),
        },
        "url": "https://haceb.com/congelador-200",
        "certificado_url": None,
        "manual_file": None,
        "manual_url": None,
    },
    {
        "id": "1004",
        "referencia": "LV20",
        "nombre": "Lavadora Carga Superior",
        "categoria": "Lavadoras",
        "descripcion": "Lavadora automatica",
        "precio": 1_800_000,
        "disponible": True,
        "dim_cm": {"ancho": 60.0, "alto": 100.0, "profundo": 60.0},
        "peso_kg": 35.0,
        "consumo_kwh_mes": 5.0,
        "clase_energetica": "A",
        "specs": {
            "Garantía": "10 años* en el motor y 1 años en los demas componentes.",
            "Capacidad de lavado": "20 kg",
        },
        "url": "https://haceb.com/lavadora-20",
        "certificado_url": None,
        "manual_file": None,
        "manual_url": None,
    },
]


@pytest.fixture(autouse=True)
def catalogo_fake(monkeypatch):
    """Reemplaza el catalogo real por el sintetico durante cada test.

    Es autouse para que ningun test toque `data/catalog.json` ni la red por
    accidente. Los tests de `fontfix` simplemente lo ignoran.
    """
    from agent import catalog

    # Limpia la cache del catalogo real antes de sustituirlo; monkeypatch
    # restaura la funcion original (con su lru_cache) al terminar cada test.
    catalog.productos.cache_clear()
    monkeypatch.setattr(catalog, "productos", lambda: [dict(p) for p in PRODUCTOS])
