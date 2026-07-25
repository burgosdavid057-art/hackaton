# Copiloto de Producción HACEB

Agente local para planta. Recibe reportes de turno en cualquier formato, los
consolida, detecta lo que se está repitiendo, y responde preguntas de un
supervisor. Corre 100% en la red interna: el modelo es Ollama, los embeddings
son locales, y ningún dato de planta sale de la máquina.

> Estado: **núcleo determinista verificado, pipeline con LLM sin probar
> end-to-end.** Ver [Qué está probado y qué no](#qué-está-probado-y-qué-no).
> No lo lleves a planta sin correr esa parte.

---

## La decisión que organiza todo: el LLM no calcula

Un modelo local de 7B no suma columnas de forma confiable. Peor: *quiere*
consolidar. En nuestras pruebas, ante un reporte que decía que la remachadora
paró tres veces (25, 8 y 20 min), el modelo devolvió **una sola parada de 53
minutos** — la suma. No inventó un número al azar; hizo aritmética servicial y
con eso destruyó exactamente la señal que este producto existe para detectar:
que la misma máquina paró tres veces.

Por eso el reparto es estricto:

| El LLM hace | Python hace |
|---|---|
| convertir texto sucio en filas | **toda** la aritmética |
| redactar el resumen | agrupar, contar, ordenar |
| elegir qué herramienta llamar | detectar recurrencia |

Si en algún momento necesitas el LLM para calcular algo, el diseño está mal.

```
produccion/inbox/          el supervisor suelta .xlsx .pdf .csv .txt
       │
       ├─ ingest.py        → texto plano (todas las hojas, tablas de PDF)
       ├─ extraer.py       → LLM + JSON estricto  ← único paso con modelo
       │                     + verificación de literalidad
       ├─ normalizar.py    → causa libre → código canónico (fuzzy, SIN LLM)
       └─ db.py            → SQLite con trazabilidad al archivo origen
                                    │
              ┌─────────────────────┴─────────────────────┐
              ▼                                           ▼
      herramientas.py                                  rag.py
      pandas/SQL, cero LLM                     Chroma, 3 colecciones
      cuánto · cuántas veces                   qué escribió la gente
      Pareto · recurrencia                     qué dice el procedimiento
              └─────────────────────┬─────────────────────┘
                                    ▼
                       agente.py (tool calling) + validador
                                    ▼
                          app_produccion.py (Streamlit)
```

**El guard más importante** es `verificar_literalidad`: todo número que el
modelo extraiga tiene que aparecer literalmente en el documento fuente. El que
no aparece se marca con confianza 0 y va a la cola de revisión — no se borra,
para que un humano lo corrija. Así fue como atrapamos los 53 minutos.

**La regla del RAG:** el filtro por metadata (línea, rango de fechas) va *antes*
de la búsqueda vectorial, usando `where=` de Chroma. El recorte es determinista;
lo semántico solo ordena lo que ya quedó dentro. RAG sin ese filtro produce
respuestas que suenan bien sobre la línea equivocada.

---

## Instalación en una PC nueva

### 1. Requisitos

- Python 3.12
- [Ollama](https://ollama.com)
- ~10 GB libres para los modelos

### 2. Clonar y crear el entorno

```bash
git clone https://github.com/burgosdavid057-art/hackaton.git
cd hackaton
git checkout copiloto-produccion
python -m venv .venv
```

Activar el entorno — Windows PowerShell:
```bash
.venv\Scripts\Activate.ps1
```
macOS o Linux:
```bash
source .venv/bin/activate
```

Instalar dependencias:
```bash
pip install -r requirements.txt
```

### 3. Bajar los modelos

```bash
ollama pull qwen2.5:7b
```
```bash
ollama pull bge-m3
```

`bge-m3` es el de embeddings y es multilingüe de verdad — importa, porque el
texto de planta es español coloquial. No lo cambies por `nomic-embed-text` sin
reconstruir los índices: son 1024 dimensiones contra 768 y no son compatibles.

### 4. Configurar el `.env`

Crea un `.env` en la raíz (no se versiona):

```bash
OLLAMA_HOST=localhost:11434
OLLAMA_MODEL=qwen2.5:7b
OLLAMA_EMBED_MODEL=bge-m3
OLLAMA_NUM_CTX=8192
```

`OLLAMA_NUM_CTX` no es opcional: el default de Ollama son 2048 tokens y no
alcanza para el prompt del sistema más las herramientas más el historial. Sin
esto el agente pierde la conversación a mitad de camino.

**Si el modelo corre en otra máquina** (recomendado — ver
[Hardware](#hardware-lo-que-medimos-de-verdad)), en esa máquina levanta Ollama
expuesto a la red:

```bash
OLLAMA_HOST=0.0.0.0 ollama serve
```

y en el `.env` de esta pon su IP:

```bash
OLLAMA_HOST=192.168.1.50:11434
```

### 5. Inicializar la base

```bash
python -c "from produccion import db; db.inicializar()"
```

Debe cargar 4 líneas, 26 estaciones y 45 causas desde `produccion/taxonomia.yaml`.

### 6. Arrancar

```bash
streamlit run app_produccion.py
```

---

## Uso

**Cargar** — arrastra los reportes de turno. Acepta `.xlsx .xls .csv .pdf .txt
.md` y no asume columnas fijas: cada archivo pasa por el extractor con esquema
estricto. Reprocesar la misma carpeta no duplica nada (idempotencia por SHA-256).

**Revisar** — la pestaña que hace confiable el producto. Muestra lo que el
modelo no supo clasificar y los números que no aparecían en el texto original.
El supervisor corrige, y esa corrección es la que evita que el resumen del lunes
mienta con seguridad.

**Preguntar** — chat. *"¿cuánto scrap llevamos este mes en L3 y por qué?"* El
número sale de pandas, el "por qué" sale de lo que escribieron los supervisores.
Debajo de cada respuesta, el panel de confianza muestra qué herramientas se
llamaron y el dictamen del auditor.

**Resumen ejecutivo** — rango de fechas y línea. Python llama las herramientas en
orden fijo, arma la tabla de hechos, y solo entonces el modelo redacta. El modelo
nunca ve la pregunta "¿cuánto scrap hubo?": ve la respuesta ya calculada.

Sin interfaz:

```bash
python -m produccion.reporte 2026-07-20 2026-07-25
```
```bash
python -m produccion.extraer ejemplos/turno_2026-07-20_L2_whatsapp.txt
```
```bash
python -m evals_produccion.run
```

---

## Hardware: lo que medimos de verdad

Ollama solo acelera con NVIDIA (CUDA), AMD (ROCm) o Apple Silicon (Metal). Una
gráfica integrada Intel **no** cuenta: todo corre en CPU.

Medido en un i5-1235U con 16 GB, sin GPU dedicada, extrayendo un reporte:

| Modelo | Tiempo por reporte |
|---|---|
| `qwen2.5:3b` | ~120 s |
| `qwen2.5:7b` | **> 10 min** |

Con esos números, un portátil sin GPU sirve para desarrollar y verificar
plomería, no para operar. Pon Ollama en una máquina con GPU o en un Apple
Silicon, y apunta el resto por red.

Los embeddings de `bge-m3` sí corren bien en CPU: es un solo forward pass, no
generación autoregresiva.

---

## Qué está probado y qué no

### Verificado, corriendo de verdad

| Qué | Resultado |
|---|---|
| Carga de catálogos | 4 líneas · 26 estaciones · 45 causas (25 parada / 15 scrap / 5 calidad) |
| Normalización de causas | **15/15** sobre frases reales de los ejemplos |
| Rechazo correcto | `"el gato de la vecina"` → sin clasificar, no fuerza nada |
| Verificación de literalidad | marca **2/2** números inventados, **0 falsos positivos** |
| Embeddings locales | `bge-m3` responde, 1024 dim, ranking semántico correcto |
| Imports de los 11 módulos | limpios |

### Sin probar — esto es lo que falta

- **El pipeline completo end-to-end.** Cargar los 3 reportes de `ejemplos/` por
  `ingest.procesar_inbox()` y ver que lleguen bien a la DB.
- **`causas_recurrentes()` sobre datos reales.** Los ejemplos tienen sembrado el
  patrón de la R-02 (5 atascos en 2 turnos) y el de la burbuja de poliuretano en
  L1. Es la prueba de la Fase 2 y no se ha corrido.
- **El agente conversacional.** `agente.responder()` nunca se ejecutó.
- **El resumen ejecutivo.** `reporte.resumen_ejecutivo()` nunca se ejecutó.
- **El RAG poblado.** `rag.py` importa, pero no se ha indexado nada.
- **La app de Streamlit.** Compila; no se ha abierto.
- **`evals_produccion.run`.** Escrito, no ejecutado.

---

## Límites conocidos

**El modelo tiende a consolidar.** Ante tres paradas de la misma máquina las
suma en una. Los prompts ya piden explícitamente una fila por parada, y el guard
de literalidad lo detecta, pero con un modelo pequeño hay que revisar la cola.
Un modelo más grande en la máquina de inferencia mejora esto bastante.

**Recurrencia no es causa raíz.** El agente dice qué se repite y con qué
evidencia. El diagnóstico lo pone el técnico. El prompt se lo prohíbe
explícitamente, y cuando los reportes traen hipótesis en conflicto (el
supervisor culpa al alimentador, mantenimiento al sensor) las presenta ambas sin
elegir ganador.

**La taxonomía es inventada.** Las 45 causas de `taxonomia.yaml` son plausibles
para línea blanca pero no son las de HACEB. Corregirlas con planta es la tarea
de mayor impacto pendiente, y lo que más rinde no son los nombres sino los
`alias` — cómo se escribe de verdad a las 2 a.m. Los tres fallos que tuvimos en
normalización se arreglaron agregando alias, no tocando código.

**Sin costos no hay priorización por plata.** La tabla `costos` está vacía, así
que `impacto_costo()` devuelve `disponible: False` y las recomendaciones se
ordenan por minutos perdidos. Cargar COP/minuto de parada y COP/unidad de scrap
es lo que convierte esto en un argumento de ahorro.

**Fotos de tableros no están soportadas.** OCR de letra manuscrita en español
sin internet daría malos resultados y se decidió no prometerlo.

---

## Mapa de módulos

| Archivo | Qué hace |
|---|---|
| `CONTRATO.md` | firmas de cada módulo — la fuente de verdad |
| `esquema.sql` | SQLite; cada hecho apunta al documento que lo produjo |
| `taxonomia.yaml` | 4 líneas, 26 estaciones, 45 causas con alias |
| `db.py` | conexión, catálogos, escritura idempotente |
| `ingest.py` | archivo → texto; orquesta el pipeline completo |
| `extraer.py` | LLM + JSON estricto + **verificación de literalidad** |
| `normalizar.py` | causa libre → código canónico, determinista |
| `herramientas.py` | 8 herramientas de cálculo, **cero LLM** |
| `rag.py` | 3 colecciones Chroma con filtro por metadata |
| `declaraciones.py` | esquemas de herramientas para el modelo |
| `agente.py` | loop de tool calling + validador |
| `reporte.py` | resumen ejecutivo con orden de llamadas fijo |
| `prompts.py` | los tres prompts: extractor, copiloto, validador |

Comparte con el agente de postventa: `agent/llm.py`, `agent/openai_backend.py`,
`agent/knowledge.py`.
