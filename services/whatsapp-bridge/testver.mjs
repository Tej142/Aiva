import { fetchLatestBaileysVersion } from '@whiskeysockets/baileys';
fetchLatestBaileysVersion().then(r => console.log('VERSION_RESULT:', JSON.stringify(r))).catch(e => console.error('ERR:', e));

