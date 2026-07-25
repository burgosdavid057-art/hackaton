# Conectar tu chatbot de WhatsApp al agente Haceb

Para el compañero que enlaza el número: el agente ya está expuesto por HTTP.
Tu chatbot solo tiene que hacer un **POST** por cada mensaje entrante y responder
al usuario con lo que devuelva.

## Endpoint

```
POST https://haceb-agente.loca.lt/message
Content-Type: application/json
```

> Si tu cliente HTTP recibe una página de aviso de localtunnel, agrega el header
> `bypass-tunnel-reminder: 1` (o un `User-Agent` personalizado). Si alguna vez
> pide una "contraseña" de túnel, es la IP pública del equipo; se obtiene con
> `curl https://loca.lt/mytunnelpassword`.

## Request (acepta varios formatos, usa el que te sirva)

```json
{ "from": "573001112233", "body": "mi nevera no enfría, ¿qué reviso?" }
```

Campos aceptados para el **remitente**: `from`, `sender`, `phone`, `number`,
`chatId`, `wa_id`. Para el **texto**: `body`, `message`, `text`, `content`, `msg`.

El `from` identifica la conversación: el agente **recuerda el hilo** por ese
valor. Manda siempre el mismo id por usuario para que haya memoria.

## Response

```json
{
  "reply":    "Sí, el compresor está cubierto...",
  "response": "Sí, el compresor está cubierto...",
  "message":  "Sí, el compresor está cubierto...",
  "from":     "573001112233"
}
```

Usa cualquiera de `reply` / `response` / `message` (los tres traen lo mismo) y
envíalo al usuario por WhatsApp. Eso es todo.

Comando especial: si el usuario escribe **reiniciar**, el agente borra la memoria
de ese hilo.

## Ejemplo (Node)

```js
const r = await fetch("https://haceb-agente.loca.lt/message", {
  method: "POST",
  headers: { "Content-Type": "application/json", "bypass-tunnel-reminder": "1" },
  body: JSON.stringify({ from: msg.from, body: msg.body }),
});
const { reply } = await r.json();
await enviarWhatsApp(msg.from, reply);
```

## Notas

- **Tiempo de respuesta:** ~5-15 s por mensaje (el modelo corre local en el PC
  del equipo). Si tu chatbot tiene timeout, súbelo a 60 s.
- El agente solo responde de **electrodomésticos Haceb**; rechaza otros temas.
- Cada respuesta ya viene **fundamentada** en el catálogo/manuales reales.

## Para levantar el servicio (lado del equipo Haceb)

```bash
# 1) el agente (puerto 5000)
python -m channels.whatsapp
# 2) el túnel público (URL fija)
npx localtunnel --port 5000 --subdomain haceb-agente
```

Si `haceb-agente` estuviera tomado, sale otra URL en la consola; pásasela al
compañero.
