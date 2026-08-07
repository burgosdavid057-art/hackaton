"""Tests de las herramientas del agente.

Cubren la logica que sostiene la promesa del producto: no inventar datos,
verificar espacio y garantia bien, y calcular costo total (no solo precio).
Todo corre contra el catalogo sintetico de conftest.py.
"""

from __future__ import annotations

from agent import tools


class TestNoInventar:
    """El contrato central: si el producto no existe, se dice, no se inventa."""

    def test_ficha_tecnica_inexistente(self):
        r = tools.ficha_tecnica("NO-EXISTE")
        assert r["encontrado"] is False
        assert "sugerencia" in r
        assert r["fuente"]

    def test_costo_energia_inexistente(self):
        assert tools.costo_energia("NO-EXISTE")["encontrado"] is False


class TestFichaTecnica:
    def test_devuelve_specs_y_fuente(self):
        r = tools.ficha_tecnica("AL404")
        assert r["encontrado"] is True
        assert r["especificaciones"]["Capacidad bruta En Litros"] == "410"
        assert r["fuente"].startswith("http")


class TestValidarEspacio:
    def test_cabe_holgado(self):
        r = tools.validar_espacio("AL404", 80, 190, 80, ancho_puerta_cm=75)
        assert r["evaluable"] is True
        assert r["cabe"] is True

    def test_no_cabe_por_ancho(self):
        # Necesita 70 + 5 de holgura = 75; solo hay 72.
        r = tools.validar_espacio("AL404", 72, 190, 80)
        assert r["cabe"] is False
        assert any(c["medida"] == "ancho" and not c["cabe"] for c in r["verificaciones"])

    def test_no_pasa_por_la_puerta(self):
        # La puerta usa el ancho del equipo sin holgura: 70 cm > 65 cm de puerta.
        r = tools.validar_espacio("AL404", 80, 190, 80, ancho_puerta_cm=65)
        assert r["cabe"] is False
        assert any("puerta" in c["medida"] for c in r["problemas"])

    def test_no_evaluable_si_faltan_dimensiones(self):
        r = tools.validar_espacio("CG200", 100, 100, 100)
        assert r["evaluable"] is False


class TestRecomendarParaEspacio:
    def test_devuelve_las_que_caben_ordenadas(self):
        r = tools.recomendar_para_espacio(80, 190, 80, categoria="Neveras")
        assert r["hay_opciones"] is True
        litros = [c["litros"] for c in r["caben"]]
        assert litros == sorted(litros, reverse=True)

    def test_corrige_ancho_y_alto_intercambiados(self):
        # Pasando alto<ancho (190 de ancho, 80 de alto) deberia detectar el
        # intercambio y resolver igual.
        r = tools.recomendar_para_espacio(190, 80, 80, categoria="Neveras")
        assert r["hay_opciones"] is True
        assert "nota" in r


class TestCostoEnergia:
    def test_calcula_costo_total_de_propiedad(self):
        r = tools.costo_energia("AL404", anios=10)
        # 10 kWh/mes * 120 meses * 950 COP = 1_140_000 de energia.
        assert r["costo_energia_cop"] == 1_140_000
        assert r["costo_total_propiedad_cop"] == 2_500_000 + 1_140_000
        assert r["tarifa_es_supuesto_del_equipo"] is True

    def test_tarifa_personalizada_no_es_supuesto(self):
        r = tools.costo_energia("AL404", anios=1, tarifa_kwh_cop=1000)
        assert r["tarifa_es_supuesto_del_equipo"] is False
        assert r["costo_energia_cop"] == 10 * 12 * 1000

    def test_no_calculable_sin_consumo(self):
        r = tools.costo_energia("CG200")
        assert r["calculable"] is False


class TestCompararCostoTotal:
    def test_el_mas_barato_de_comprar_no_es_el_de_menor_costo_total(self):
        r = tools.comparar_costo_total(["AL404", "AL350"], anios=10)
        ranking = r["ranking_por_costo_total"]
        # AL404 cuesta mas comprarla pero gasta menos: gana en costo total.
        assert ranking[0]["referencia"] == "AL404"
        assert ranking[0]["precio_compra_cop"] > ranking[-1]["precio_compra_cop"]
        assert "hallazgo" in r

    def test_ignora_los_no_calculables(self):
        r = tools.comparar_costo_total(["AL404", "CG200"], anios=10)
        assert r["comparados"] == 1


class TestParsearGarantia:
    def test_compresor_y_demas_componentes(self):
        cobertura = tools._parsear_garantia(
            "10 años* en el compresor y 1 años en los demas componentes, "
            "*1 año correspondiente a garantia legal y 9 años a garantia "
            "suplementaria."
        )
        assert cobertura == {"compresor": 10, "demas componentes": 1}

    def test_nota_al_pie_no_se_cuela_como_parte(self):
        # "5 años en las tarjetas" con nota al pie legal pegada.
        cobertura = tools._parsear_garantia(
            "5 años* en las tarjetas *el primer año corresponde a la garantia legal"
        )
        assert cobertura == {"tarjetas": 5}


class TestVerificarGarantia:
    def test_compresor_dentro_de_garantia(self):
        r = tools.verificar_garantia("AL404", "compresor", anios_de_uso=5)
        assert r["determinable"] is True
        assert r["cobertura_anios"] == 10
        assert r["en_garantia"] is True

    def test_componente_no_nombrado_cae_en_el_cajon_por_defecto(self):
        # "motor" no esta nombrado en AL404 -> aplica "los demas componentes" (1 año).
        r = tools.verificar_garantia("AL404", "motor", anios_de_uso=3)
        assert r["cobertura_anios"] == 1
        assert r["en_garantia"] is False

    def test_fuera_de_garantia_por_tiempo(self):
        r = tools.verificar_garantia("AL404", "compresor", anios_de_uso=12)
        assert r["en_garantia"] is False

    def test_no_determinable_sin_texto_de_garantia(self, monkeypatch):
        from agent import catalog

        p = dict(catalog.por_referencia("AL404"))
        p["specs"] = dict(p["specs"])
        p["specs"].pop("Garantía")
        monkeypatch.setattr(catalog, "por_referencia", lambda ref: p)
        r = tools.verificar_garantia("AL404", "compresor", 1)
        assert r["determinable"] is False


class TestRadicarYConsultarCaso:
    def _redirigir_casos(self, tmp_path, monkeypatch):
        destino = tmp_path / "casos.json"
        monkeypatch.setattr(tools, "_casos_path", lambda: destino)
        return destino

    def test_radica_componente_cubierto(self, tmp_path, monkeypatch):
        self._redirigir_casos(tmp_path, monkeypatch)
        r = tools.radicar_garantia("AL404", "compresor", anios_de_uso=2)
        assert r["radicado"] is True
        assert r["ticket"].startswith("GAR-")

    def test_no_radica_fuera_de_garantia(self, tmp_path, monkeypatch):
        self._redirigir_casos(tmp_path, monkeypatch)
        r = tools.radicar_garantia("AL404", "compresor", anios_de_uso=12)
        assert r["radicado"] is False
        assert r["en_garantia"] is False

    def test_consultar_caso_recien_radicado(self, tmp_path, monkeypatch):
        self._redirigir_casos(tmp_path, monkeypatch)
        ticket = tools.radicar_garantia("AL404", "compresor", 2)["ticket"]
        r = tools.consultar_caso(ticket)
        assert r["encontrado"] is True
        assert r["caso"]["componente"] == "compresor"

    def test_consultar_caso_inexistente(self, tmp_path, monkeypatch):
        self._redirigir_casos(tmp_path, monkeypatch)
        assert tools.consultar_caso("GAR-XXXXXX")["encontrado"] is False


class TestBuscarProductos:
    def test_avisa_capacidad_mas_cercana(self):
        # No hay nevera de exactamente 400 L: debe ofrecer la mas cercana (404).
        r = tools.buscar_productos(litros_aprox=400)
        assert r["encontrado"] is True
        assert "404" in r.get("nota", "")


class TestEscalarYEjecutar:
    def test_escalar_a_servicio_tecnico(self):
        r = tools.escalar_a_servicio_tecnico("necesito un repuesto", "AL404")
        assert r["escalado"] is True
        assert r["referencia"] == "AL404"

    def test_ejecutar_herramienta_desconocida(self):
        assert "error" in tools.ejecutar("no_existe", {})

    def test_ejecutar_argumentos_invalidos(self):
        r = tools.ejecutar("costo_energia", {"parametro_malo": 1})
        assert "error" in r

    def test_ejecutar_ok(self):
        r = tools.ejecutar("costo_energia", {"referencia": "AL404", "anios": 10})
        assert r["calculable"] is True
