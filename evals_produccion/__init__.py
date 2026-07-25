"""
Harness de evaluacion de la EXTRACCION del Copiloto de Produccion.

Hermano de `evals/` (que mide al agente de postventa). Este mide el paso
anterior: convertir un reporte de turno sucio en filas estructuradas. Es donde
un modelo local de 7B falla en silencio, y el silencio es el problema: un
numero mal leido no se ve, se propaga hasta el resumen ejecutivo.

    python -m evals_produccion.run
"""
