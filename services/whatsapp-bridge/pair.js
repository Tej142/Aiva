import path from 'path';
import fs from 'fs';
import { fileURLToPath } from 'url';
import dotenv from 'dotenv';
import pino from 'pino';
import qrcode from 'qrcode-terminal';
import { patchBaileys } from './patch-baileys.js';

patchBaileys();

import baileysPackage, {
  DisconnectReason,
  useMultiFileAuthState,
  fetchLatestBaileysVersion,
  Browsers
} from '@whiskeysockets/baileys';

const makeWASocket = typeof baileysPackage === 'function'
  ? baileysPackage
  : (baileysPackage.default || baileysPackage.makeWASocket);

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

// Load .env
const envPath = path.resolve(__dirname, '../../.env');
if (fs.existsSync(envPath)) {
  dotenv.config({ path: envPath });
} else {
  dotenv.config();
}

const AIVA_WHATSAPP_NUMBER = (process.env.AIVA_WHATSAPP_NUMBER || '').replace(/\D/g, '');
const authDir = path.resolve(__dirname, '../../storage/whatsapp_auth');

// Ensure clean auth directory for a fresh pairing session
if (fs.existsSync(authDir)) {
  try {
    const files = fs.readdirSync(authDir);
    for (const f of files) {
      fs.unlinkSync(path.join(authDir, f));
    }
  } catch (_) {}
} else {
  fs.mkdirSync(authDir, { recursive: true, mode: 0o700 });
}

console.log('====================================================');
console.log('       AIVA WhatsApp Pairing Tool (Baileys)         ');
console.log('====================================================');

if (!AIVA_WHATSAPP_NUMBER) {
  console.warn('⚠️ AIVA_WHATSAPP_NUMBER is not set in .env.');
  console.warn('   Will show terminal QR code for scanning.\n');
} else {
  console.log(`📱 Target Phone: +${AIVA_WHATSAPP_NUMBER}`);
}

async function startPairing() {
  const { state, saveCreds } = await useMultiFileAuthState(authDir);
  const { version } = await fetchLatestBaileysVersion().catch(() => ({ version: [2, 3000, 1015901307] }));

  const sock = makeWASocket({
    version,
    auth: state,
    logger: pino({ level: 'silent' }),
    printQRInTerminal: false,
    syncFullHistory: false,
    browser: Browsers.macOS('Desktop')
  });

  let isFullyRegistered = false;
  let pairingCodeRequested = false;

  sock.ev.on('creds.update', async () => {
    try {
      await saveCreds();
    } catch (_) {}

    // When pairing code is used, companion registration completes when registered is true
    if (sock.authState?.creds?.registered && !isFullyRegistered) {
      isFullyRegistered = true;
      const pairedJid = sock.user?.id || sock.authState?.creds?.me?.id || '';
      const phone = pairedJid.split(':')[0].replace(/\D/g, '') || AIVA_WHATSAPP_NUMBER;
      console.log('\n====================================================');
      console.log(`🎉 SUCCESS: WhatsApp Registration Complete!`);
      console.log(`   Account: +${phone}`);
      console.log('   All encryption keys synchronized and saved.');
      console.log('====================================================\n');
      console.log('⏳ Allowing 8 seconds for initial sync to finalize...');
      setTimeout(() => {
        console.log('🚀 Ready! You can now start the bridge service:');
        console.log('   sudo systemctl start aiva-whatsapp\n');
        process.exit(0);
      }, 8000);
    }
  });

  sock.ev.on('connection.update', async (update) => {
    const { connection, lastDisconnect, qr } = update;

    if (qr) {
      console.log('\n====================================================');
      console.log('  📷 OPTION 1 (Recommended): Scan with Camera');
      console.log('  WhatsApp -> Linked Devices -> Link a Device');
      console.log('====================================================');
      qrcode.generate(qr, { small: true });
      console.log('====================================================');
    }

    if (connection === 'open') {
      console.log('\n📡 WhatsApp connection open. Finalizing companion sync...');
      const hasMe = Boolean(sock.user?.id || sock.authState?.creds?.me?.id);
      if (hasMe && !isFullyRegistered) {
        isFullyRegistered = true;
        sock.authState.creds.registered = true;
        try {
          await saveCreds();
        } catch (_) {}
        const pairedJid = sock.user?.id || sock.authState?.creds?.me?.id || '';
        const phone = pairedJid.split(':')[0].replace(/\D/g, '') || AIVA_WHATSAPP_NUMBER;
        console.log('\n====================================================');
        console.log(`🎉 SUCCESS: Paired with WhatsApp account: +${phone}`);
        console.log('   Credentials saved to storage/whatsapp_auth/');
        console.log('====================================================\n');
        console.log('⏳ Allowing 6 seconds for initial sync to finalize...');
        setTimeout(() => {
          console.log('🚀 Ready! Start the bridge service:');
          console.log('   sudo systemctl start aiva-whatsapp\n');
          process.exit(0);
        }, 6000);
      }
    }

    if (connection === 'close') {
      const statusCode = lastDisconnect?.error?.output?.statusCode;
      if (statusCode === DisconnectReason.loggedOut) {
        console.warn('⚠️ Stale uncompleted session detected. Cleaning credentials and restarting fresh...');
        try {
          const files = fs.readdirSync(authDir);
          for (const f of files) {
            fs.unlinkSync(path.join(authDir, f));
          }
        } catch (_) {}
        setTimeout(startPairing, 1000);
        return;
      }
      if (statusCode === DisconnectReason.restartRequired || statusCode === 515) {
        setTimeout(startPairing, 1000);
      }
    }
  });

  // Request 8-character Pairing Code only if not already paired/registered
  const alreadyHasMe = Boolean(sock.authState?.creds?.me?.id);
  if (AIVA_WHATSAPP_NUMBER && !alreadyHasMe && !sock.authState?.creds?.registered && !pairingCodeRequested) {
    pairingCodeRequested = true;
    setTimeout(async () => {
      try {
        console.log(`\n⏳ Requesting pairing code for +${AIVA_WHATSAPP_NUMBER}...`);
        const code = await sock.requestPairingCode(AIVA_WHATSAPP_NUMBER);
        const formatted = code.match(/.{1,4}/g)?.join('-') || code;
        console.log('====================================================');
        console.log(`🔑 OPTION 2: Enter Pairing Code:  \x1b[1m\x1b[32m${formatted}\x1b[0m`);
        console.log('====================================================');
        console.log('Steps on WhatsApp Business:');
        console.log('  1. Open WhatsApp -> Settings -> Linked Devices');
        console.log('  2. Tap "Link a device"');
        console.log('  3. Tap "Link with phone number instead"');
        console.log(`  4. Enter the code: ${formatted}`);
        console.log('====================================================\n');
      } catch (err) {
        console.warn('Pairing code note:', err.message);
      }
    }, 2500);
  }
}

startPairing().catch((err) => {
  console.error('Pairing error:', err);
  process.exit(1);
});

