# Cómo contribuir

Gracias por el interés en el proyecto. Esta guía es corta a propósito.

## Correr los tests

La suite cubre la lógica pura del agente (reparación de texto de los manuales,
catálogo y herramientas) y **no** necesita las dependencias pesadas de
`requirements.txt` ni ninguna API: corre en segundos y sin red.

```bash
pip install -r requirements-dev.txt
pytest
```

Los tests usan un catálogo sintético (ver `conftest.py`), así que son
deterministas y no tocan `data/catalog.json`.

## Antes de abrir un PR

- Que `pytest` pase en verde.
- Si cambias o agregas lógica en `agent/tools.py`, `agent/catalog.py` o
  `fontfix.py`, acompáñala de un test que fije el comportamiento.
- Mantené el estilo del código existente (nombres en español, funciones
  pequeñas, docstrings que expliquen *por qué*).

El mismo `pytest` corre en CI (GitHub Actions) contra Python 3.11, 3.12 y 3.13.

## Principio del proyecto

La regla de oro del agente es **no inventar datos**: si el catálogo no publica
algo, la herramienta devuelve `disponible: False` con un motivo, en vez de
rellenar el hueco. Cualquier contribución debería respetar ese principio.
