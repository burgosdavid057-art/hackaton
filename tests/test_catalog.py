"""Tests de catalog: normalizacion, capacidad y busqueda/filtrado.

Corren contra el catalogo sintetico de conftest.py (autouse), asi que son
deterministas y no tocan la red ni data/catalog.json.
"""

from __future__ import annotations

from agent import catalog


class TestNormaliza:
    def test_baja_y_quita_tildes(self):
        assert catalog.normaliza("Nevera Eléctrica ÑOÑA") == "nevera electrica nona"

    def test_none_no_revienta(self):
        assert catalog.normaliza(None) == ""


class TestLitrosDe:
    def test_desde_el_nombre(self):
        p = catalog.por_referencia("AL404")
        assert catalog.litros_de(p) == 404.0

    def test_desde_specs_cuando_no_esta_en_el_nombre(self):
        p = catalog.por_referencia("AL350")
        assert catalog.litros_de(p) == 350.0

    def test_none_si_no_hay_capacidad(self):
        p = catalog.por_referencia("CG200")
        assert catalog.litros_de(p) is None


class TestPorReferencia:
    def test_match_exacto(self):
        assert catalog.por_referencia("AL404")["id"] == "1001"

    def test_por_id(self):
        assert catalog.por_referencia("1002")["referencia"] == "AL350"

    def test_tolerante_por_nombre(self):
        # La referencia aparece dentro del nombre del producto.
        assert catalog.por_referencia("congelador horizontal")["id"] == "1003"

    def test_inexistente(self):
        assert catalog.por_referencia("NO-EXISTE") is None


class TestBuscar:
    def test_filtra_por_categoria(self):
        res = catalog.buscar("", categoria="Congeladores")
        assert [p["referencia"] for p in res] == ["CG200"]

    def test_litros_aprox_ordena_por_cercania(self):
        res = catalog.buscar("", litros_aprox=400)
        # AL404 (404) es la mas cercana a 400, antes que AL350 (350).
        assert res[0]["referencia"] == "AL404"

    def test_precio_max_filtra(self):
        res = catalog.buscar("", precio_max=1_600_000)
        assert [p["referencia"] for p in res] == ["CG200"]

    def test_texto_relevante(self):
        res = catalog.buscar("inverter")
        assert [p["referencia"] for p in res] == ["AL404"]

    def test_sin_coincidencias_de_texto(self):
        assert catalog.buscar("xyzzy") == []


class TestResumen:
    def test_expone_campos_clave(self):
        r = catalog.resumen(catalog.por_referencia("AL404"))
        assert r["referencia"] == "AL404"
        assert r["precio_cop"] == 2_500_000
        assert r["tiene_manual"] is True
        assert "compresor" in r["garantia"]
