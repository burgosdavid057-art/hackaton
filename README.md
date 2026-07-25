# Agente de ciclo de vida Haceb

Un agente que acompaña un electrodoméstico durante toda su vida, no solo hasta el carrito de compras.

**Antes de comprar:** ¿cabe en mi cocina y pasa por la puerta? ¿cuánto me va a costar de luz en 10 años?
**Después de comprar:** se dañó, ¿qué reviso? ¿lo cubre la garantía? ¿qué hago ahora?

Todo fundamentado en el **catálogo público real de Haceb** y en los **manuales de usuario oficiales**. El agente no responde nunca desde la memoria del modelo.

> Haceb da 10 años de garantía en el compresor. El acompañamiento debería durar lo mismo que el producto.

---

> **¿Buscas el Copiloto de Producción?** Es el otro agente de este repo: uso
> interno, para planta, con su propia app. Va aparte porque tiene otra audiencia
> y otras herramientas, y comparte con este el motor de Ollama y el validador.
> Documentación en [produccion/README.md](produccion/README.md).

---

## La idea

El proyecto obvio para una marca de electrodomésticos es un asistente de compra: un chatbot sobre el catálogo. Ese agente resuelve el minuto en que alguien decide comprar, e ignora los diez años siguientes.

Este agente cubre el ciclo completo, y su diferencial no es lo que sabe sino **lo que se niega a inventar**. El catálogo público de Haceb no publica datos de compatibilidad de repuestos; cuando hace falta identificar uno, el agente lo dice y escala a servicio técnico en vez de arriesgar una referencia equivocada. Un número de parte inventado le cuesta dinero real a la persona que lo compra.

## Arquitectura

```
                    ┌──────────────────────────────┐
   pregunta  ─────► │  loop del agente             │
                    │  (Gemini + function calling) │
                    └───────────┬──────────────────┘
                                │ elige herramientas
        ┌───────────────────────┼────────────────────────┐
        ▼                       ▼                        ▼
  catálogo VTEX          manuales oficiales        cálculo directo
  47 productos           17 manuales · 1.1 MB      espacio y energía
  specs, precio,         recuperación híbrida      sobre datos reales
  garantía, dims         BM25 + embeddings
        └───────────────────────┼────────────────────────┘
                                ▼
                    ┌──────────────────────────────┐
                    │  sub-agente validador        │
                    │  ¿cada cifra está respaldada?│
                    └───────────┬──────────────────┘
                                ▼
                        respuesta con fuentes
```

**Un solo agente con ocho herramientas**, más un sub-agente validador que corre aparte. El validador es un agente distinto a propósito: un modelo que se autoevalúa en el mismo turno tiende a darse la razón.

| Componente | Dónde está | Qué hace |
|---|---|---|
| Loop de agente | [agent/loop.py](agent/loop.py) | ciclo pensar → usar herramienta → leer → repetir |
| Herramientas | [agent/tools.py](agent/tools.py) | 8 funciones sobre datos reales, nunca mock |
| RAG + base vectorial | [agent/knowledge.py](agent/knowledge.py) · [agent/vectordb.py](agent/vectordb.py) | Chroma (746 pasajes) + BM25, búsqueda híbrida |
| Memoria | [agent/loop.py](agent/loop.py) | historial de conversación entre turnos |
| Context engineering | [agent/declarations.py](agent/declarations.py) | descripciones que enseñan *cuándo* usar cada herramienta |
| Sub-agente validador | [agent/validator.py](agent/validator.py) | audita cada afirmación contra la evidencia |
| Entrada multimodal | [agent/vision.py](agent/vision.py) | foto de la placa de datos → producto identificado |
| Canal WhatsApp | [channels/whatsapp.py](channels/whatsapp.py) | webhook Twilio: el mismo agente por WhatsApp |
| Harness de evals | [evals/](evals/) | mide alucinaciones sobre preguntas adversariales |

### Las herramientas

| Herramienta | Para qué |
|---|---|
| `buscar_productos` | identificar de qué producto se habla |
| `ficha_tecnica` | especificaciones publicadas |
| `validar_espacio` | ¿cabe en el hueco y pasa por la puerta? |
| `costo_energia` | costo eléctrico y costo total de propiedad |
| `comparar_costo_total` | comparar modelos por costo total, no por precio |
| `consultar_manual` | diagnóstico y uso, desde el manual oficial |
| `verificar_garantia` | cobertura real según los términos publicados |
| `escalar_a_servicio_tecnico` | derivar cuando no hay dato verificable |

## Los datos

Del catálogo público de Haceb (API VTEX, sin autenticación) y de sus manuales oficiales:

```
47 productos   neveras · congeladores · lavadoras · lavadora-secadora
17 manuales    1.13 MB de texto técnico real

manual        72%      dimensiones   82%
consumo kWh   82%      certificado   91%
garantía      87%      precio       100%
```

Se eligió ese corte de categorías porque es donde la cobertura de datos es alta **en las cuatro dimensiones a la vez**. En el catálogo completo solo el 17% de los productos publica consumo energético, lo que haría imposible el cálculo de costo total.

### Un detalle del camino

Los manuales en PDF traen la fuente embebida **sin tabla ToUnicode**, así que los extractores devuelven los códigos de glifo en vez del texto. Ni `pypdf` ni `pymupdf` lo resuelven:

```
HQ\x03OD\x03SRVLFLyQ\x03PiV\x03IUtR   →   "en la posición más frío"
```

Es un desplazamiento de +29 sobre todo el juego de caracteres, espacios (`\x03`) y paréntesis incluidos. [fontfix.py](fontfix.py) lo revierte con 0,007% de residuo. Sin eso, el RAG habría indexado basura y el agente respondería incoherencias sin que nadie lo notara.

## Cómo correrlo

```bash
pip install -r requirements.txt
cp .env.example .env        # y pon tu llave de https://aistudio.google.com/apikey
python ingest.py            # descarga catálogo y manuales  (~3 min)
python build_index.py       # precomputa los embeddings     (~10 min, hay rate limit)
python build_vectordb.py    # puebla la base vectorial Chroma (sin API, instantáneo)
streamlit run app.py        # interfaz web
```

`ingest.py` y `build_index.py` son idempotentes: si se interrumpen, al volver a correrlos retoman donde iban.

**Otros modos:**

```bash
python -m evals.run              # corre el harness de evaluación y muestra el tablero
python -m channels.whatsapp      # levanta el webhook de WhatsApp en :5000
```

El canal WhatsApp se prueba en local con `curl` (ver cabecera de [channels/whatsapp.py](channels/whatsapp.py)) y se conecta a WhatsApp real con el sandbox de Twilio + ngrok.

### Modelo

Se usa `gemini-flash-latest`. **Los IDs `gemini-2.0-flash` y `gemini-2.5-flash` devuelven 404/429** en cuentas de capa gratuita aunque aparezcan en `models.list()`, cosa que cuesta un buen rato descubrir.

## Decisiones

**Function calling plano, sin framework de agentes.** Un solo loop de ~40 líneas. LangGraph o un Agents SDK habrían agregado una capa que aprender sin resolver ningún problema que tuviéramos.

**BM25 + embeddings, no solo embeddings.** El usuario dice *"no enfría"* y el manual dice *"POCO FRÍO EN EL COMPARTIMIENTO INFERIOR"*: cero palabras en común, así que la búsqueda léxica sola fallaba. Pero BM25 sigue siendo mejor para referencias y códigos exactos. Se combinan 40/60.

**El validador falla en cerrado.** Si no puede auditar —sin red, sin cuota, JSON ilegible— marca la respuesta como no verificada en vez de aprobarla. Un auditor que no pudo trabajar no es un aval.

**Las herramientas devuelven `disponible: False` con motivo, no valores por defecto.** Si el catálogo no publica un dato, el agente tiene que decirlo. Rellenar el hueco con un supuesto es exactamente el fallo que este proyecto quiere evitar.

## Límites conocidos

- 8 de los manuales son aplicaciones HTML con JavaScript; su contenido no se puede extraer con el pipeline actual. Quedan 34 de 47 productos con manual.
- El catálogo público no publica compatibilidad de repuestos. Es deliberado que el agente escale en vez de adivinar.
- La tarifa eléctrica usada en el costo total es un supuesto del equipo (950 COP/kWh, Antioquia), no un dato de Haceb. La herramienta lo declara explícitamente en su respuesta.

## Licencia

MIT — ver [LICENSE](LICENSE).

Datos de producto y manuales: propiedad de Industrias Haceb S.A., obtenidos de sus canales públicos.
