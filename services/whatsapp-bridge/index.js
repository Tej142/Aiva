import http from 'http';
import path from 'path';
import fs from 'fs';
import crypto from 'crypto';
import { fileURLToPath } from 'url';
import dotenv from 'dotenv';
import pino from 'pino';
import { patchBaileys } from './patch-baileys.js';

// Apply Baileys LID patch before initializing socket
patchBaileys();

import baileysPackage, {
  DisconnectReason,
  useMultiFileAuthState,
  downloadMediaMessage,
  fetchLatestBaileysVersion,
  Browsers
} from '@whiskeysockets/baileys';

const makeWASocket = typeof baileysPackage === 'function'
  ? baileysPackage
  : (baileysPackage.default || baileysPackage.makeWASocket);

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

// 1. Load environment variables
const envPath = path.resolve(__dirname, '../../.env');
if (fs.existsSync(envPath)) {
  dotenv.config({ path: envPath });
} else {
  dotenv.config();
}

const WHATSAPP_BRIDGE_PORT = parseInt(process.env.WHATSAPP_BRIDGE_PORT || '3001', 10);
const WHATSAPP_BRIDGE_SECRET = (process.env.WHATSAPP_BRIDGE_SECRET || '').trim();
const WHATSAPP_BACKEND_URL = (process.env.WHATSAPP_BACKEND_URL || 'http://127.0.0.1:5000').replace(/\/+$/, '');
const AIVA_WHATSAPP_NUMBER = (process.env.AIVA_WHATSAPP_NUMBER || '').replace(/\D/g, '');

const logger = pino({ level: process.env.LOG_LEVEL || 'warn' });

// Ensure auth directory exists with 0700 permissions
const authDir = path.resolve(__dirname, '../../storage/whatsapp_auth');
if (!fs.existsSync(authDir)) {
  fs.mkdirSync(authDir, { recursive: true, mode: 0o700 });
} else {
  try {
    fs.chmodSync(authDir, 0o700);
  } catch (_) {
    // Windows chmod may be a no-op
  }
}

// State
let sock = null;
let isConnected = false;
let connectionState = 'connecting';
let lastError = null;
let pairedPhone = '';
let reconnectAttempts = 0;
const sentMessageIds = new Set();
const messageStore = new Map(); // id -> proto.IMessage for decryption retry
const mediaMsgStore = new Map(); // id -> full WAMessage for lazy /media/download
let _pairingManagerPromise = null;

async function getPairingManager() {
  if (!_pairingManagerPromise) {
    _pairingManagerPromise = (async () => {
      const { fileURLToPath } = await import('url');
      const pathMod = await import('path');
      const { createPairingManager } = await import('./pairing.js');
      const here = pathMod.dirname(fileURLToPath(import.meta.url));
      const authDir = pathMod.resolve(here, '../../storage/whatsapp_auth');
      return createPairingManager({
        authDir,
        logger,
        onBeforeStart: async () => {
          detachMainSocket();
          isConnected = false;
          connectionState = 'pairing';
          lastError = null;
        },
        onStateChange: (pState) => {
          if (pState.active) {
            isConnected = false;
            connectionState = 'pairing';
          } else if (pState.phase === 'error' && !isConnected) {
            connectionState = 're_pair_required';
            lastError = pState.error || lastError;
          } else if (pState.phase === 'cancelled' && !isConnected) {
            connectionState = 're_pair_required';
          }
        },
        onPaired: async ({ phone }) => {
          pairedPhone = phone || pairedPhone;
          lastError = null;
          reconnectAttempts = 0;
          connectionState = 'connecting';
          await connectToWhatsApp();
        }
      });
    })();
  }
  return _pairingManagerPromise;
}

function detachMainSocket() {
  const current = sock;
  sock = null;
  if (!current) return;
  try { current.ev?.removeAllListeners?.('connection.update'); } catch (_) {}
  try { current.ev?.removeAllListeners?.('creds.update'); } catch (_) {}
  try { current.ev?.removeAllListeners?.('messages.upsert'); } catch (_) {}
  try { current.ev?.removeAllListeners?.('messaging-history.set'); } catch (_) {}
  const client = current.ws;
  if (client) {
    try { client.on?.('error', () => {}); } catch (_) {}
    const rawWs = client.socket;
    if (rawWs) {
      try { rawWs.on?.('error', () => {}); } catch (_) {}
      if (client.isConnecting || rawWs.readyState === 0) {
        try { rawWs.terminate?.(); } catch (_) {}
        client.socket = null;
        return;
      }
    }
  }
  try { current.ws?.close?.(); } catch (_) {}
  try { current.end?.(); } catch (_) {}
}

function readJsonBody(req) {
  return new Promise((resolve, reject) => {
    let body = '';
    req.on('data', (chunk) => {
      body += chunk;
      if (body.length > 65536) {
        reject(new Error('Request body too large'));
      }
    });
    req.on('end', () => {
      try {
        resolve(JSON.parse(body || '{}'));
      } catch (err) {
        reject(err);
      }
    });
    req.on('error', reject);
  });
}

// Clean sent message ids older than 1 hour
setInterval(() => {
  if (sentMessageIds.size > 2000) {
    sentMessageIds.clear();
  }
}, 60 * 60 * 1000);

// Constant-time secret compare
function safeCompare(a, b) {
  if (!a || !b) return false;
  const bufA = Buffer.from(String(a));
  const bufB = Buffer.from(String(b));
  if (bufA.length !== bufB.length) return false;
  return crypto.timingSafeEqual(bufA, bufB);
}

// ─────────────────────────────────────────────────────────────────
// BAILEYS WHATSAPP CONNECTION
// ─────────────────────────────────────────────────────────────────
async function connectToWhatsApp() {
  if (sock) {
    try {
      sock.ws?.close();
      sock.end?.();
    } catch (_) {}
    sock = null;
  }
  connectionState = 'connecting';
  lastError = null;

  const { state, saveCreds } = await useMultiFileAuthState(authDir);
  const { version } = await fetchLatestBaileysVersion().catch(() => ({ version: [2, 3000, 1015901307] }));

  sock = makeWASocket({
    version,
    auth: state,
    logger,
    printQRInTerminal: false,
    syncFullHistory: false,
    markOnlineOnConnect: true,
    browser: Browsers.macOS('Desktop'),
    getMessage: async (key) => {
      if (key?.id && messageStore.has(key.id)) {
        return messageStore.get(key.id);
      }
      return undefined;
    }
  });

  sock.ev.on('creds.update', saveCreds);

  sock.ev.on('connection.update', async (update) => {
    const { connection, lastDisconnect, qr } = update;

    if (connection === 'close') {
      isConnected = false;
      const statusCode = lastDisconnect?.error?.output?.statusCode;
      const errorMsg = lastDisconnect?.error?.message || 'Connection closed';
      lastError = errorMsg;
      console.error('[WhatsAppBridge] Disconnect details:', JSON.stringify(lastDisconnect?.error, Object.getOwnPropertyNames(lastDisconnect?.error || {})));

      if (statusCode === DisconnectReason.loggedOut) {
        connectionState = 're_pair_required';
        console.error('[WhatsAppBridge] ❌ Logged out from WhatsApp (401). Re-pair is required. Run `npm run pair`.');
        return;
      }

      connectionState = 'disconnected';
      reconnectAttempts++;
      const delayMs = Math.min(1000 * Math.pow(1.5, reconnectAttempts), 30000);
      console.warn(`[WhatsAppBridge] Disconnected (${statusCode || errorMsg}). Reconnecting in ${(delayMs / 1000).toFixed(1)}s (attempt ${reconnectAttempts})...`);
      setTimeout(connectToWhatsApp, delayMs);
    } else if (connection === 'open') {
      isConnected = true;
      connectionState = 'connected';
      reconnectAttempts = 0;
      lastError = null;

      // Extract paired phone number
      const fullJid = sock.user?.id || '';
      pairedPhone = fullJid.split(':')[0].split('@')[0].replace(/\D/g, '');

      console.log(`[WhatsAppBridge] ✅ Connected to WhatsApp as ${maskPeer(pairedPhone)}`);

      // Enforce AIVA_WHATSAPP_NUMBER identity invariant: bridge only runs AIVA's own account
      if (AIVA_WHATSAPP_NUMBER && (!pairedPhone || pairedPhone !== AIVA_WHATSAPP_NUMBER)) {
        console.error(
          `[WhatsAppBridge] ❌ SECURITY ERROR: Paired number (${maskPeer(pairedPhone)}) does not match AIVA_WHATSAPP_NUMBER (${maskPeer(AIVA_WHATSAPP_NUMBER)})! Refusing to run.`
        );
        try { sock?.ws?.close?.(); } catch (_) {}
        process.exit(1);
      }
    }
  });

  // Extract text / caption + media metadata ONLY (never eager-download media)
  function extractMessageMeta(m) {
    const msg = m?.message || {};
    const inner = msg.ephemeralMessage?.message || msg.viewOnceMessage?.message || msg.viewOnceMessageV2?.message || msg;
    const text = (
      inner.conversation ||
      inner.extendedTextMessage?.text ||
      inner.imageMessage?.caption ||
      inner.videoMessage?.caption ||
      inner.documentWithCaptionMessage?.message?.documentMessage?.caption ||
      ''
    ).trim();
    let mediaType = 'text';
    let mediaMime = '';
    let mediaFilename = '';
    let mediaSeconds = 0;

    if (inner.audioMessage) {
      mediaType = 'audio';
      mediaMime = inner.audioMessage.mimetype || 'audio/ogg';
      mediaSeconds = Number(inner.audioMessage.seconds || 0);
    } else if (inner.imageMessage) {
      mediaType = 'image';
      mediaMime = inner.imageMessage.mimetype || 'image/jpeg';
    } else if (inner.videoMessage) {
      mediaType = 'video';
      mediaMime = inner.videoMessage.mimetype || 'video/mp4';
      mediaSeconds = Number(inner.videoMessage.seconds || 0);
    } else if (inner.documentMessage || inner.documentWithCaptionMessage) {
      mediaType = 'document';
      const docMsg = inner.documentMessage || inner.documentWithCaptionMessage?.message?.documentMessage || {};
      mediaMime = docMsg.mimetype || 'application/octet-stream';
      mediaFilename = docMsg.fileName || '';
    }
    return { text, mediaType, mediaMime, mediaFilename, mediaSeconds, rawInner: inner };
  }

  async function resolvePeerInfo(m) {
    const remoteJid = m?.key?.remoteJid || '';
    const altJid = m?.key?.remoteJidAlt || m?.key?.senderPn || m?.key?.participantPn || '';
    const ownLid = (sock?.user?.lid || '').split('@')[0].split(':')[0];
    const ownPhone = (sock?.user?.id || '').split('@')[0].split(':')[0].replace(/\D/g, '') || AIVA_WHATSAPP_NUMBER;
    const remoteDigits = remoteJid.split('@')[0].split(':')[0].replace(/\D/g, '');
    let altDigits = String(altJid).split('@')[0].split(':')[0].replace(/\D/g, '');
    const lidJid = remoteJid.endsWith('@lid') ? remoteJid : (String(altJid).endsWith('@lid') ? String(altJid) : '');

    if (remoteJid.endsWith('@lid') && !altDigits && sock?.signalRepository?.lidMapping?.getPNForLID) {
      try {
        const pn = await sock.signalRepository.lidMapping.getPNForLID(remoteJid);
        if (pn) {
          altDigits = String(pn).split('@')[0].split(':')[0].replace(/\D/g, '');
        }
      } catch (_) {}
    }

    const peerDigits = (!remoteJid.endsWith('@lid') ? remoteDigits : (altDigits || ''));
    const isOwnAccount = Boolean(m?.key?.fromMe) ||
      Boolean(ownPhone && peerDigits && peerDigits === ownPhone) ||
      Boolean(ownLid && remoteDigits && remoteDigits === ownLid);

    return { remoteJid, ownPhone, ownLid, peerDigits, lidJid, isOwnAccount };
  }

  function maskPeer(digits) {
    const clean = String(digits || '').replace(/\D/g, '');
    return clean.length > 4 ? `+${'*'.repeat(clean.length - 4)}${clean.slice(-4)}` : '****';
  }

  sock.ev.on('messaging-history.set', async () => {
    // History sync replays are ignored in multi-user mode to avoid ingesting stale pre-link messages.
  });

  // Handle Live Inbound 1:1 Messages
  sock.ev.on('messages.upsert', async ({ messages, type }) => {
    for (const m of (messages || [])) {
      if (m.key?.id && m.message) {
        messageStore.set(m.key.id, m.message);
        if (messageStore.size > 2000) {
          const firstKey = messageStore.keys().next().value;
          messageStore.delete(firstKey);
        }
        mediaMsgStore.set(m.key.id, m);
        if (mediaMsgStore.size > 1000) {
          const firstMediaKey = mediaMsgStore.keys().next().value;
          mediaMsgStore.delete(firstMediaKey);
        }
      }
      if (!m.message) continue;

      const remoteJid = m.key?.remoteJid || '';
      const messageId = m.key?.id || '';

      // Skip own AIVA-sent messages & echoes
      if (sentMessageIds.has(messageId)) continue;
      if (m.key?.fromMe) continue;

      // Skip history replays
      if (type && type !== 'notify') continue;

      // Skip groups, status broadcasts, reactions, protocol messages
      if (!remoteJid || remoteJid.endsWith('@g.us')) continue;
      if (remoteJid === 'status@broadcast' || remoteJid.endsWith('@broadcast')) continue;
      if (m.message.reactionMessage || m.message.protocolMessage) continue;

      const info = await resolvePeerInfo(m);
      if (info.isOwnAccount) continue;

      const meta = extractMessageMeta(m);
      console.log(
        `[WhatsAppBridge] Inbound peer=${maskPeer(info.peerDigits)} media=${meta.mediaType} len=${meta.text.length}`
      );

      forwardToBackend({
        message_id: messageId,
        sender: info.peerDigits,
        lid_jid: info.lidJid,
        chat_jid: info.remoteJid,
        push_name: (m.pushName || '').trim(),
        text: meta.text,
        media_type: meta.mediaType,
        media_mime: meta.mediaMime,
        media_filename: meta.mediaFilename,
        media_seconds: meta.mediaSeconds,
        from_me: false,
        timestamp: Number(m.messageTimestamp || Math.floor(Date.now() / 1000))
      });
    }
  });
}

// ─────────────────────────────────────────────────────────────────
// FORWARD TO PYTHON BACKEND
// ─────────────────────────────────────────────────────────────────
async function forwardArchiveBatch(items) {
  if (!Array.isArray(items) || items.length === 0) return;
  try {
    const url = `${WHATSAPP_BACKEND_URL}/internal/whatsapp/archive`;
    const res = await fetch(url, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Bridge-Secret': WHATSAPP_BRIDGE_SECRET
      },
      body: JSON.stringify({ messages: items })
    });
    if (!res.ok) {
      logger.warn(`[WhatsAppBridge] Backend /internal/whatsapp/archive responded ${res.status}`);
    }
  } catch (err) {
    logger.warn(`[WhatsAppBridge] Archive batch error: ${err.message}`);
  }
}

async function forwardToBackend(payload) {
  try {
    const url = `${WHATSAPP_BACKEND_URL}/internal/whatsapp/inbound`;
    const res = await fetch(url, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Bridge-Secret': WHATSAPP_BRIDGE_SECRET
      },
      body: JSON.stringify(payload)
    });

    if (!res.ok) {
      const errText = await res.text();
      logger.warn(`[WhatsAppBridge] Backend /internal/whatsapp/inbound responded ${res.status}: ${errText}`);
    }
  } catch (err) {
    logger.error(`[WhatsAppBridge] Error forwarding inbound to backend: ${err.message}`);
  }
}

// ─────────────────────────────────────────────────────────────────
// HTTP API SERVER (127.0.0.1 ONLY)
// ─────────────────────────────────────────────────────────────────
const server = http.createServer(async (req, res) => {
  // Constant-time auth verification
  const clientSecret = req.headers['x-bridge-secret'] || '';
  if (!safeCompare(clientSecret, WHATSAPP_BRIDGE_SECRET)) {
    res.writeHead(401, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ error: 'unauthorized', message: 'Invalid or missing X-Bridge-Secret' }));
    return;
  }

  const { method, url } = req;

  // GET /status
  if (method === 'GET' && url === '/status') {
    const mgr = await getPairingManager();
    const pairingState = mgr.getState();
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({
      connected: isConnected,
      status: pairingState.active ? 'pairing' : connectionState,
      bridge_state: connectionState,
      phone: pairedPhone,
      error: pairingState.active ? (pairingState.error || lastError) : lastError,
      pairing: pairingState
    }));
    return;
  }

  // POST /pair/start
  if (method === 'POST' && url === '/pair/start') {
    try {
      const data = await readJsonBody(req);
      const mode = (data.mode || 'qr').toLowerCase();
      const phone = data.phone || '';
      const mgr = await getPairingManager();
      await mgr.start({ mode, phone, clearAuth: true });

      // Wait briefly (up to 4s) for initial QR SVG or 8-char pairing code so first UI frame renders immediately
      const waitStart = Date.now();
      while (Date.now() - waitStart < 4000) {
        const cur = mgr.getState();
        if (cur.phase === 'qr_ready' || cur.phase === 'code_ready' || cur.phase === 'error' || cur.phase === 'paired') {
          break;
        }
        await new Promise((r) => setTimeout(r, 150));
      }

      const finalState = mgr.getState();
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({
        status: 'success',
        pairing: finalState
      }));
    } catch (err) {
      const statusCode = /invalid|phone number/i.test(err?.message || '') ? 400 : 500;
      res.writeHead(statusCode, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: 'pair_start_failed', message: err?.message || 'Failed to start pairing' }));
    }
    return;
  }

  // POST /pair/cancel
  if (method === 'POST' && url === '/pair/cancel') {
    try {
      const mgr = await getPairingManager();
      const pairingState = await mgr.cancel();
      if (!isConnected) {
        connectionState = 're_pair_required';
      }
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ status: 'success', pairing: pairingState }));
    } catch (err) {
      res.writeHead(500, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: 'pair_cancel_failed', message: err?.message || 'Failed to cancel pairing' }));
    }
    return;
  }

  // POST /logout
  if (method === 'POST' && url === '/logout') {
    try {
      const mgr = await getPairingManager();
      await mgr.cancel();
      if (sock) {
        try { await sock.logout(); } catch (_) {}
      }
      detachMainSocket();

      const { fileURLToPath } = await import('url');
      const pathMod = await import('path');
      const { clearAuthDirectory } = await import('./pairing.js');
      const here = pathMod.dirname(fileURLToPath(import.meta.url));
      clearAuthDirectory(pathMod.resolve(here, '../../storage/whatsapp_auth'));

      isConnected = false;
      pairedPhone = '';
      connectionState = 're_pair_required';
      lastError = 'Session unpaired by user. Scan QR or use Phone Code to pair.';

      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ status: 'success', bridge_state: connectionState }));
    } catch (err) {
      res.writeHead(500, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: 'logout_failed', message: err?.message || 'Failed to logout bridge' }));
    }
    return;
  }

  // POST /send
  if (method === 'POST' && url === '/send') {
    let body = '';
    req.on('data', chunk => { body += chunk; });
    req.on('end', async () => {
      try {
        const data = JSON.parse(body || '{}');
        const to = data.to;
        const text = data.text;

        if (!to || !text) {
          res.writeHead(400, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({ error: 'bad_request', message: 'Missing to or text parameter' }));
          return;
        }

        if (!isConnected || !sock) {
          res.writeHead(503, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({ error: 'whatsapp_disconnected', message: 'Bridge is not connected to WhatsApp' }));
          return;
        }

        const toJid = data.to_jid || (to.includes('@') ? to : `${to.replace(/\D/g, '')}@s.whatsapp.net`);
        const toDigits = String(toJid).split('@')[0].split(':')[0].replace(/\D/g, '');
        const maskedTo = toDigits.length > 4 ? `+${'*'.repeat(toDigits.length - 4)}${toDigits.slice(-4)}` : '****';
        console.log(`[WhatsAppBridge] Outbound send to=${maskedTo} len=${String(text).length}`);
        const sent = await sock.sendMessage(toJid, { text });

        if (sent?.key?.id) {
          sentMessageIds.add(sent.key.id);
          if (sent.message) {
            messageStore.set(sent.key.id, sent.message);
            if (messageStore.size > 2000) {
              const firstKey = messageStore.keys().next().value;
              messageStore.delete(firstKey);
            }
          }
        }

        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({
          status: 'success',
          messageId: sent?.key?.id || null
        }));
      } catch (err) {
        logger.error(`[WhatsAppBridge] /send error: ${err.message}`);
        res.writeHead(500, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: 'send_failed', message: err.message }));
      }
    });
    return;
  }

  // POST /presence
  if (method === 'POST' && url === '/presence') {
    let body = '';
    req.on('data', chunk => { body += chunk; });
    req.on('end', async () => {
      try {
        const data = JSON.parse(body || '{}');
        const to = data.to;
        const presence = data.presence || 'composing';
        if (sock && to) {
          const jid = to.includes('@') ? to : `${to.replace(/\D/g, '')}@s.whatsapp.net`;
          await sock.sendPresenceUpdate(presence, jid);
        }
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ status: 'ok' }));
      } catch (err) {
        res.writeHead(500, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: err.message }));
      }
    });
    return;
  }

  // POST /read (explicit read receipt after backend read succeeds)
  if (method === 'POST' && url === '/read') {
    try {
      const data = await readJsonBody(req);
      const jid = data.jid || data.chat_jid || '';
      const ids = Array.isArray(data.message_ids) ? data.message_ids.filter(Boolean) : [];
      if (!isConnected || !sock) {
        res.writeHead(503, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: 'whatsapp_disconnected' }));
        return;
      }
      if (jid && ids.length > 0) {
        const keys = ids.map((id) => ({ remoteJid: jid, id, fromMe: false }));
        await sock.readMessages(keys);
      }
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ status: 'ok', count: ids.length }));
    } catch (err) {
      res.writeHead(500, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: err.message }));
    }
    return;
  }

  // POST /media/download (lazy media fetch on explicit user read only)
  if (method === 'POST' && url === '/media/download') {
    try {
      const data = await readJsonBody(req);
      const messageId = data.message_id || '';
      const msgObj = messageId ? mediaMsgStore.get(messageId) : null;
      if (!msgObj) {
        res.writeHead(404, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: 'media_not_cached', message: 'Media message is no longer cached in bridge memory.' }));
        return;
      }
      const buffer = await downloadMediaMessage(msgObj, 'buffer', {});
      if (!buffer || buffer.length === 0) {
        res.writeHead(404, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: 'empty_media' }));
        return;
      }
      if (buffer.length > 15 * 1024 * 1024) {
        res.writeHead(413, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: 'media_too_large' }));
        return;
      }
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({
        status: 'success',
        bytes: buffer.length,
        base64: buffer.toString('base64')
      }));
    } catch (err) {
      res.writeHead(500, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: 'media_download_failed', message: err.message }));
    }
    return;
  }

  res.writeHead(404, { 'Content-Type': 'application/json' });
  res.end(JSON.stringify({ error: 'not_found' }));
});

// Bind exclusively to 127.0.0.1
server.listen(WHATSAPP_BRIDGE_PORT, '127.0.0.1', () => {
  console.log(`[WhatsAppBridge] 🚀 Listening exclusively on 127.0.0.1:${WHATSAPP_BRIDGE_PORT}`);
  connectToWhatsApp().catch(err => {
    console.error('[WhatsAppBridge] Failed to initialize Baileys:', err);
  });
});

