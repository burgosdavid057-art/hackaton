"""Copiloto de Produccion HACEB.

Agente local para planta: consolida reportes de turno, detecta patrones
recurrentes y responde preguntas de supervisores.

La regla que organiza todo el paquete: el LLM no calcula. Se usa para dos cosas
que hace bien —convertir texto sucio en filas estructuradas, y redactar— y para
nada mas. Toda la aritmetica vive en herramientas.py, sobre SQLite y pandas.

Ver CONTRATO.md para las firmas de cada modulo.
"""
