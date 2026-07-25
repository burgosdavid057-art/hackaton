# Contrato de interfaces — Copiloto de Producción

Cada módulo se implementa contra estas firmas. Nadie las cambia sin cambiar este
archivo primero.

## Convención de retorno de herramientas

**Toda** función de `herramientas.py` y `rag.py` devuelve un dict con esta forma:

```python
# éxito
{"disponible": True, "datos": ..., "cobertura": {...}, "periodo": {...}}

# sin datos suficientes
{"disponible": False, "motivo": "no hay turnos cargados para L3 en ese rango"}
```

Nunca se devuelve `0` ni `[]` en lugar de `disponible: False`. Un cero real
("hubo cero scrap") y una ausencia de datos ("no cargaron el reporte") son cosas
distintas y el agente tiene que poder distinguirlas.

El bloque `cobertura` es obligatorio en toda herramienta numérica:

```python
"cobertura": {
    "turnos_encontrados": 12,
    "turnos_esperados": 15,       # días hábiles × turnos × líneas del rango
    "sin_clasificar": 3,          # filas con causa_id NULL en el rango
    "parcial": True,              # turnos_encontrados < turnos_esperados
}
```

Es lo que le permite al agente decir "esto es sobre 12 de 15 turnos" en vez de
dar una cifra que parece completa.

## Módulos

### `db.py`
```python
RUTA_DB: Path                                    # data/produccion.db

def conectar() -> sqlite3.Connection             # row_factory = sqlite3.Row, FK ON
def inicializar(forzar: bool = False) -> None    # esquema.sql + taxonomia.yaml
def registrar_documento(ruta: str, sha256: str, formato: str,
                        texto_crudo: str) -> int | None   # None si ya existía
def guardar_turno(documento_id: int, turno: dict) -> int  # devuelve turno_id
def pendientes_revision(limite: int = 100) -> list[dict]
def marcar_revisado(tabla: str, fila_id: int, causa_id: int | None) -> None
def causas(tipo: str | None = None) -> list[dict]
def lineas() -> list[dict]
def estaciones(linea: str | None = None) -> list[dict]
def texto_causas_para_prompt(tipo: str | None = None) -> str  # bloque del prompt
def texto_catalogo_para_prompt() -> str
```

`guardar_turno` recibe el dict ya normalizado (con `causa_id` y `estacion_id`
resueltos) y es idempotente por `(fecha, turno, linea_id)`.

### `ingest.py`
```python
def sha256_archivo(ruta: Path) -> str
def a_texto(ruta: Path) -> tuple[str, str]       # (texto_plano, formato)
def procesar(ruta: Path) -> dict                 # pipeline completo de 1 archivo
def procesar_inbox(carpeta: Path = INBOX) -> list[dict]
```

`a_texto` soporta `.xlsx .xls .csv .pdf .txt .md`. Para Excel: todas las hojas,
renderizadas como tabla de texto legible. Para PDF: texto + tablas. Nunca lanza
por formato desconocido: devuelve `("", "desconocido")`.

`procesar` devuelve:
```python
{"archivo": str, "formato": str, "estado": "ok" | "duplicado" | "error",
 "turnos_guardados": int, "campos_dudosos": int, "motivo": str | None}
```

### `extraer.py`
```python
def extraer(texto: str) -> dict                  # {"turnos": [...]}
def verificar_literalidad(datos: dict, texto: str) -> dict
```

`extraer` llama a Ollama con `format=json`, temperatura 0, y **reintenta hasta 3
veces** si el JSON no parsea o no cumple la forma. Si agota reintentos devuelve
`{"turnos": [], "error": "..."}`.

`verificar_literalidad` recorre todo campo numérico y comprueba que el valor
aparezca como substring en `texto` (tolerando `1.234` / `1234` / `1,234`). Los
que no aparecen: pone `confianza = 0.0` y agrega el nombre del campo a una lista
`campos_no_literales` en ese turno. **No borra el valor** — lo marca, para que la
pantalla de revisión lo muestre en rojo.

### `normalizar.py`
```python
def normalizar_causa(texto: str, tipo: str) -> tuple[int | None, float]
def normalizar_estacion(texto: str | None, linea: str | None) -> int | None
def normalizar_linea(texto: str | None) -> int | None
def normalizar(datos: dict) -> dict              # aplica todo sobre extraer()
```

`normalizar_causa` en tres pasos, en orden: (1) match exacto de código,
(2) match sobre alias normalizados sin tildes, (3) similitud por tokens
(`difflib`). Umbral 0.72 — por debajo devuelve `(None, 0.0)` y la fila queda
pendiente de revisión. **No usa LLM**: es determinista y reproducible.

### `herramientas.py` — toda la aritmética, cero LLM
```python
def scrap_por_linea(linea=None, desde=None, hasta=None) -> dict
def pareto_paradas(linea=None, desde=None, hasta=None, top=10) -> dict
def causas_recurrentes(dias=7, min_repeticiones=3, linea=None) -> dict
def produccion_vs_plan(linea=None, desde=None, hasta=None) -> dict
def comparar_periodos(desde_a, hasta_a, desde_b, hasta_b, linea=None) -> dict
def impacto_costo(desde=None, hasta=None, linea=None) -> dict
def estado_datos(desde=None, hasta=None) -> dict
def ejecutar(nombre: str, argumentos: dict) -> dict   # despacho por nombre
```

`impacto_costo` devuelve `disponible: False` si la tabla `costos` está vacía, con
motivo explícito. No inventa tarifas.

`causas_recurrentes` es el corazón de la Fase 2: agrupa por
`(causa_id, estacion_id)` y devuelve las que superan `min_repeticiones` en la
ventana, con primera y última aparición y total de minutos/unidades.

### `rag.py` — Chroma, 3 colecciones
```python
COLECCIONES = ("observaciones", "procedimientos", "resoluciones")

def disponible() -> bool
def indexar_observaciones(turno_id: int, documento_id: int,
                          metadata: dict, textos: list[str]) -> int
def indexar_procedimientos(carpeta: Path) -> int
def buscar_observaciones(consulta, linea=None, desde=None, hasta=None, k=5) -> dict
def buscar_procedimiento(consulta, maquina=None, k=3) -> dict
def casos_similares(descripcion, k=3) -> dict
```

**Regla no negociable:** el filtro por metadata va ANTES de la búsqueda
vectorial (`where=` de Chroma). El recorte por línea y fecha es determinista;
lo semántico solo ordena lo que ya quedó dentro del universo correcto. RAG sin
ese filtro es lo que produce respuestas que suenan bien sobre la línea
equivocada.

Embeddings vía `agent.llm.embed_textos` (bge-m3 local). Si Chroma no está
disponible, las tres funciones devuelven `disponible: False` con motivo — nunca
lanzan.

### `declaraciones.py`
```python
def herramientas_openai() -> list[dict]          # esquema tools de OpenAI
```
Cubre las 8 de `herramientas.py` + las 3 de `rag.py`. Las descripciones enseñan
*cuándo* usar cada una, no solo qué hace — mismo criterio que
`agent/declarations.py`.

### `agente.py`
```python
MAX_PASOS = 8
class Traza:  # .registrar(nombre, args, resultado); .evidencia_json(); .pasos
def responder(mensaje: str, historial: list | None = None) -> tuple[str, Traza, list]
def validar(respuesta: str, evidencia_json: str) -> dict
```
Mismo contrato que `agent/openai_backend.py:responder`. Reutiliza el cliente
OpenAI apuntando a Ollama vía `agent.llm.config_openai()`.

### `reporte.py`
```python
def resumen_ejecutivo(desde: str, hasta: str, linea: str | None = None) -> dict
```
Devuelve `{"markdown": str, "evidencia": dict, "validacion": dict}`. Llama las
herramientas numéricas **primero** (en Python, sin que el modelo elija), arma un
bloque de hechos, y solo entonces le pide al LLM que redacte. El modelo nunca ve
la pregunta "¿cuánto scrap hubo?" — ve la tabla ya calculada.

### `app_produccion.py` (raíz del repo)
Streamlit, app separada de `app.py`. Tres pestañas:
1. **Cargar** — arrastrar archivos → correr pipeline → resultado por archivo
2. **Revisar** — cola de campos dudosos y causas sin clasificar, editables
3. **Preguntar** — chat con el agente + panel de traza y sello del validador

Más un botón "Resumen ejecutivo" con selector de rango.

## Dependencias entre módulos

```
esquema.sql + taxonomia.yaml
        ↓
      db.py ──────────────┐
        ↓                 │
  normalizar.py           │
        ↓                 ↓
  ingest.py → extraer.py  herramientas.py    rag.py
        └────────┬────────────────┴────────────┘
                 ↓
        declaraciones.py → agente.py → reporte.py
                                ↓
                       app_produccion.py
```

## Reglas transversales

- **Sin tildes en identificadores y docstrings de código** — el repo ya es así
  (`agent/tools.py`), y evita líos de encoding en Windows.
- Comentarios en español, densidad como la del repo: explican *por qué*, no qué.
- Ninguna función numérica llama al LLM. Ninguna. Si se necesita el LLM para
  calcular algo, el diseño está mal.
- Todo acceso a la DB pasa por `db.conectar()`. Nada de rutas hardcodeadas.
