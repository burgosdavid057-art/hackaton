# Agente Haceb en WhatsApp (open-wa)

Conecta el agente a un **WhatsApp real** usando [open-wa](https://www.open-wa.org/):
recibe mensajes, los pasa al agente y responde con datos fundamentados.

## ⚠️ Advertencia importante

open-wa automatiza WhatsApp Web de forma **NO OFICIAL**. Va contra los términos
de servicio de WhatsApp y **el número puede ser BANEADO**. 

**Usa un número de repuesto / secundario. NUNCA tu número personal.**

Para un demo puntual el riesgo es bajo, pero existe. Para producción real se usa
la API oficial de WhatsApp Business (Twilio, Meta), no open-wa. El webhook para
esa vía ya está en [`../channels/whatsapp.py`](../channels/whatsapp.py).

## Cómo correrlo

Necesitas: Python (el agente ya configurado), Node.js y Chrome.

```bash
# 1. Terminal A — el agente Python (endpoint JSON en el puerto 5000)
cd ..
python -m channels.whatsapp

# 2. Terminal B — el puente de WhatsApp
cd whatsapp-bot
npm install          # solo la primera vez (baja Chromium, ~1-2 min)
npm start

# 3. Se genera whatsapp-bot/qr.png. Ábrelo y escanéalo:
#    WhatsApp en el celular > Dispositivos vinculados > Vincular un dispositivo

# 4. Escribe al número vinculado desde OTRO celular. El agente responde.
```

Escribe **reiniciar** en el chat para borrar la memoria de esa conversación.

## Cómo funciona

```
WhatsApp  ──►  bot.js (open-wa, Node)  ──HTTP──►  channels/whatsapp.py (agente)
   ▲                                                        │
   └────────────────  respuesta fundamentada  ◄─────────────┘
```

El agente que responde es el mismo de la app web: mismas herramientas, mismo RAG,
mismo validador, misma memoria por remitente. Solo cambia el canal de entrada.

Config del agente (proveedor, modelo) se toma del `.env` del proyecto — funciona
igual con Ollama local o con Groq.
