"""Tests de fontfix: la reparacion del texto cifrado de los manuales.

Este modulo es el que salva al RAG de indexar basura, asi que conviene fijar su
comportamiento con casos reales tomados de los manuales de Haceb.
"""

from __future__ import annotations

import fontfix


class TestReparar:
    def test_frase_real_desplazada(self):
        # Cifrado +29 -> "en la posicion mas frio refrigerador" (con tildes).
        cifrado = "HQ\x03OD\x03SRVLFLyQ\x03PiV\x03IUtR\x03UHIULJHUDGRU"
        assert fontfix.reparar(cifrado) == "en la posición más frío refrigerador"

    def test_digitos_y_acentos(self):
        # Los digitos tambien se desplazan (0x14 -> '1', 0x18 -> '5').
        cifrado = "6L\x03D\x03ORV\x03\x14\x18\x03GtDV\x03GH\x03KDEHU\x03UHDOL]DGR"
        assert fontfix.reparar(cifrado) == "Si a los 15 días de haber realizado"

    def test_texto_sano_no_se_toca(self):
        sano = "El condensador esta poco ventilado. COMPARTIMIENTO INFERIOR"
        assert fontfix.reparar(sano) == sano

    def test_espacio_real_es_frontera(self):
        # Un espacio real (0x20) separa fragmentos: cada uno se descifra aparte.
        assert fontfix.reparar("HQ\x03OD HQ\x03OD") == "en la en la"

    def test_grado_tras_digito(self):
        # 0x83 es grado cuando sigue a un numero...
        assert fontfix.reparar("18\x83") == "18°"

    def test_vinneta_al_inicio(self):
        # ...y vinneta cuando no.
        assert fontfix.reparar("\x83 item de lista") == "• item de lista"

    def test_idempotente_sobre_texto_ya_reparado(self):
        una_vez = fontfix.reparar("HQ\x03OD\x03SRVLFLyQ")
        assert fontfix.reparar(una_vez) == una_vez

    def test_cadena_vacia(self):
        assert fontfix.reparar("") == ""


class TestStats:
    def test_detecta_cifrado(self):
        s = fontfix.stats("HQ\x03OD\x03SRVLFLyQ")
        assert s["cifrado"] is True
        assert s["fragmentos"] == 1
        assert s["caracteres"] > 0

    def test_texto_sano_sin_fragmentos(self):
        s = fontfix.stats("texto perfectamente normal")
        assert s["cifrado"] is False
        assert s["fragmentos"] == 0
        assert s["caracteres"] == 0


class TestResiduos:
    def test_no_marca_caracteres_validos(self):
        # Tildes, signos y simbolos esperados no deben contarse como residuo.
        assert fontfix.residuos("cámara ¿pregunta? 25°C — “cita”") == {}

    def test_marca_lo_raro(self):
        # Un caracter fuera del set esperado si aparece como residuo.
        res = fontfix.residuos("hola \x9f mundo")
        assert res.get("\x9f") == 1
