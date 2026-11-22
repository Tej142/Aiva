import path from 'path';
import { fileURLToPath } from 'url';
import baileysPackage, { useMultiFileAuthState, fetchLatestBaileysVersion, Browsers } from '@whiskeysockets/baileys';

const makeWASocket = typeof baileysPackage === 'function' ? baileysPackage : (baileysPackage.default || baileysPackage.makeWASocket);
const __dirname = path.dirname(fileURLToPath(import.meta.url));
const authDir = path.resolve(__dirname, '../../storage/whatsapp_auth');

const { state, saveCreds } = await useMultiFileAuthState(authDir);
const { version } = await fetchLatestBaileysVersion().catch(() => ({ version: [2, 3000, 1015901307] }));

console.log('Registered in state?', state.creds.registered);
console.log('Me in state?', state.creds.me);

const sock = makeWASocket({
  version,
  auth: state,
  browser: Browsers.macOS('Desktop'),
});

sock.ev.on('creds.update', saveCreds);
sock.ev.on('connection.update', (u) => {
  console.log('CONN UPDATE:', JSON.stringify(u, null, 2));
});

