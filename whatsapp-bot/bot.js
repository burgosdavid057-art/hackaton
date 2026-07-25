/*
 * Puente WhatsApp <-> agente Haceb, usando open-wa.
 *
 * Recibe mensajes de WhatsApp, se los pasa al agente Python (que corre en
 * http://localhost:5000/message) y devuelve la respuesta fundamentada.
 *
 * ADVERTENCIA: open-wa automatiza WhatsApp Web de forma NO OFICIAL. Va contra
 * los terminos de WhatsApp y el numero puede ser BANEADO. Usa un numero de
 * repuesto, NUNCA tu numero personal.
 *
 * Uso:
 *   1) En una terminal:  python -m channels.whatsapp     (el agente, puerto 5000)
 *   2) En otra:          cd whatsapp-bot && npm install && npm start
 *   3) Escanea el QR que aparece con WhatsApp (Dispositivos vinculados).
 *   4) Escribe al numero vinculado desde otro celular.
 */

const { create, ev } = require('@open-wa/wa-automate');

const AGENTE = process.env.AGENTE_URL || 'http://localhost:5000/message';

// Muestra el QR como texto en la terminal para escanearlo.
ev.on('qr.**', async (qrcode) => {
  const imageBuffer = Buffer.from(
    qrcode.replace('data:image/png;base64,', ''),
    'base64'
  );
  require('fs').writeFileSync('qr.png', imageBuffer);
  console.log('\n>> QR guardado en whatsapp-bot/qr.png — ábrelo y escanéalo con WhatsApp.\n');
});

create({
  sessionId: 'haceb',
  headless: false,       // Chrome visible: escaneas el QR directo en la ventana
  qrTimeout: 0,          // no expira el QR
  authTimeout: 60,
  cacheEnabled: false,
  useChrome: true,
  killProcessOnBrowserClose: false,
  throwErrorOnTosBlock: false,
  disableSpins: true,
  popup: false,
}).then((client) => start(client)).catch((e) => {
  console.error('No se pudo iniciar:', e.message);
});

async function start(client) {
  console.log('>> Agente Haceb conectado a WhatsApp. Esperando mensajes...');

  await client.onMessage(async (message) => {
    // Solo chats individuales, con texto.
    if (message.isGroupMsg) return;
    const body = (message.body || '').trim();
    if (!body) return;

    console.log(`<- ${message.from}: ${body}`);
    try {
      await client.simulateTyping(message.from, true);
      const resp = await fetch(AGENTE, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ from: message.from, body }),
      });
      const data = await resp.json();
      const reply = data.reply || 'No pude responder en este momento.';
      await client.simulateTyping(message.from, false);
      await client.sendText(message.from, reply);
      console.log(`-> ${message.from}: ${reply.slice(0, 60)}...`);
    } catch (e) {
      console.error('error:', e.message);
      await client.sendText(
        message.from,
        'Tuve un problema técnico. Intenta de nuevo en un momento.'
      );
    }
  });
}
