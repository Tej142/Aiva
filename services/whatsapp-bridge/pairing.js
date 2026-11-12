import * as baileys from '@whiskeysockets/baileys';
const {
  makeWASocket: _namedMakeWASocket,
  useMultiFileAuthState,
  DisconnectReason,
  fetchLatestBaileysVersion,
  Browsers
} = baileys;
const makeWASocket =
  typeof _namedMakeWASocket === 'function'
    ? _namedMakeWASocket
    : (typeof baileys.default === 'function' ? baileys.default : baileys.default?.default);
import { Boom } from '@hapi/boom';
import pino from 'pino';
import fs from 'fs';
import path from 'path';
import { createRequire } from 'module';

const require = createRequire(import.meta.url);
const QRCode = require('qrcode-terminal/vendor/QRCode');
const QRErrorCorrectLevel = require('qrcode-terminal/vendor/QRCode/QRErrorCorrectLevel');

const PAIRING_TTL_MS = 120_000;

function maskPhone(raw) {
  const digits = String(raw || '').replace(/\D/g, '');
  if (digits.length <= 4) return '****';
  return `+${'*'.repeat(digits.length - 4)}${digits.slice(-4)}`;
}

/**
 * Generates a crisp black-on-white SVG (and base64 data URI) directly from
 * qrcode-terminal's bundled QRCode generator (zero extra runtime dependencies).
 */
export function renderQrSvg(qrText) {
  if (!qrText) return null;
  const qr = new QRCode(-1, QRErrorCorrectLevel.L);
  qr.addData(String(qrText));
  qr.make();

  const count = qr.getModuleCount();
  const margin = 4;
  const total = count + margin * 2;
  let pathParts = [];

  for (let r = 0; r < count; r += 1) {
    for (let c = 0; c < count; c += 1) {
      if (qr.isDark(r, c)) {
        pathParts.push(`M${c + margin},${r + margin}h1v1h-1z`);
      }
    }
  }

  const svg =
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${total} ${total}" ` +
    `width="240" height="240" shape-rendering="crispEdges" role="img" aria-label="WhatsApp QR Code">` +
    `<rect width="${total}" height="${total}" fill="#FFFFFF"/>` +
    `<path d="${pathParts.join('')}" fill="#000000"/>` +
    `</svg>`;

  const dataUrl = `data:image/svg+xml;base64,${Buffer.from(svg, 'utf8').toString('base64')}`;
  return { svg, dataUrl };
}

export function clearAuthDirectory(authDir) {
  fs.mkdirSync(authDir, { recursive: true });
  for (const entry of fs.readdirSync(authDir)) {
    try {
      fs.unlinkSync(path.join(authDir, entry));
    } catch (_) {}
  }
}

export function createPairingManager({ authDir, logger, onBeforeStart, onPaired, onStateChange }) {
  const silentLogger = logger || pino({ level: 'silent' });
  let pairingSock = null;
  let activeToken = 0;
  let expiryTimer = null;
  let codeRequestedForToken = 0;

  let state = {
    active: false,
    phase: 'idle',
    mode: null,
    qr: null,
    qr_data_url: null,
    pairing_code: null,
    target_phone_masked: null,
    error: null,
    started_at: null,
    updated_at: null,
    expires_at: null
  };

  function emitState(patch) {
    state = {
      ...state,
      ...patch,
      updated_at: Date.now()
    };
    if (typeof onStateChange === 'function') {
      try {
        onStateChange(getState());
      } catch (_) {}
    }
  }

  function clearTimers() {
    if (expiryTimer) {
      clearTimeout(expiryTimer);
      expiryTimer = null;
    }
  }

  function closePairingSocket() {
    if (!pairingSock) return;
    const s = pairingSock;
    pairingSock = null;
    try { s.ev?.removeAllListeners?.('connection.update'); } catch (_) {}
    try { s.ev?.removeAllListeners?.('creds.update'); } catch (_) {}
    const client = s.ws;
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
    try { s.ws?.close?.(); } catch (_) {}
    try { s.end?.(); } catch (_) {}
  }

  function getState() {
    return { ...state };
  }

  function isActive() {
    return Boolean(state.active);
  }

  async function openPairingSocket(token, targetDigits, mode, restartCount = 0) {
    if (token !== activeToken) return;

    const { state: authState, saveCreds } = await useMultiFileAuthState(authDir);
    if (token !== activeToken) return;

    const { version } = await fetchLatestBaileysVersion();
    if (token !== activeToken) return;

    closePairingSocket();

    pairingSock = makeWASocket({
      version,
      logger: silentLogger,
      printQRInTerminal: false,
      auth: authState,
      browser: Browsers.macOS('Desktop'),
      syncFullHistory: false,
      markOnlineOnConnect: false
    });

    pairingSock.ev.on('creds.update', async () => {
      if (token !== activeToken) return;
      try {
        await saveCreds();
      } catch (err) {
        console.warn('[WhatsAppPairing] Failed saving creds:', err?.message || err);
      }
    });

    pairingSock.ev.on('connection.update', async (update) => {
      if (token !== activeToken) return;
      const { connection, lastDisconnect, qr } = update;

      if (qr) {
        if (mode === 'qr') {
          try {
            const rendered = renderQrSvg(qr);
            emitState({
              phase: 'qr_ready',
              qr,
              qr_data_url: rendered?.dataUrl || null,
              error: null
            });
          } catch (err) {
            emitState({
              phase: 'error',
              active: false,
              error: `Failed to render QR code: ${err?.message || err}`
            });
            clearTimers();
            closePairingSocket();
          }
        } else if (mode === 'code' && codeRequestedForToken !== token && targetDigits) {
          codeRequestedForToken = token;
          try {
            await new Promise((r) => setTimeout(r, 800));
            if (token !== activeToken || !pairingSock) return;
            const rawCode = await pairingSock.requestPairingCode(targetDigits);
            const clean = String(rawCode || '').trim().toUpperCase();
            const formatted = clean.length === 8 ? `${clean.slice(0, 4)}-${clean.slice(4)}` : clean;
            emitState({
              phase: 'code_ready',
              pairing_code: formatted,
              qr: null,
              qr_data_url: null,
              error: null
            });
          } catch (err) {
            emitState({
              active: false,
              phase: 'error',
              error: `Failed to request pairing code: ${err?.message || err}`
            });
            clearTimers();
            closePairingSocket();
          }
        }
      }

      if (connection === 'open') {
        const currentSock = pairingSock;
        console.log('[WhatsAppPairing] Handshake opened; waiting briefly for credential registration flush...');
        await new Promise((r) => setTimeout(r, 1200));
        if (token !== activeToken) return;

        const registered = Boolean(currentSock?.authState?.creds?.registered);
        const rawJid = currentSock?.user?.id || '';
        const pairedDigits = rawJid ? rawJid.split(':')[0].split('@')[0].replace(/\D/g, '') : '';

        if (!registered && !pairedDigits) {
          return;
        }

        const expectedDigits = (process.env.AIVA_WHATSAPP_NUMBER || '').replace(/\D/g, '');
        if (expectedDigits && pairedDigits && pairedDigits !== expectedDigits) {
          clearTimers();
          try { await currentSock?.logout?.(); } catch (_) {}
          closePairingSocket();
          clearAuthDirectory(authDir);
          emitState({
            active: false,
            phase: 'error',
            qr: null,
            qr_data_url: null,
            pairing_code: null,
            error: `Paired account (${maskPhone(pairedDigits)}) does not match AIVA_WHATSAPP_NUMBER (${maskPhone(expectedDigits)}). Scan with AIVA's dedicated account.`
          });
          return;
        }

        clearTimers();
        closePairingSocket();
        emitState({
          active: false,
          phase: 'paired',
          qr: null,
          qr_data_url: null,
          pairing_code: null,
          error: null
        });

        if (typeof onPaired === 'function') {
          try {
            await onPaired({ phone: pairedDigits });
          } catch (err) {
            console.error('[WhatsAppPairing] Post-pair handoff failed:', err?.message || err);
          }
        }
        return;
      }

      if (connection === 'close') {
        const statusCode = new Boom(lastDisconnect?.error)?.output?.statusCode;
        const isRegistered = Boolean(pairingSock?.authState?.creds?.registered);

        if (statusCode === DisconnectReason.restartRequired || statusCode === 515 || isRegistered) {
          console.log('[WhatsAppPairing] Stream restart required during handshake (normal after scan); continuing...');
          emitState({ phase: 'restarting' });
          setTimeout(() => {
            openPairingSocket(token, targetDigits, mode, restartCount + 1).catch((err) => {
              if (token !== activeToken) return;
              emitState({
                active: false,
                phase: 'error',
                error: err?.message || 'Pairing restart failed'
              });
              clearTimers();
            });
          }, 800);
          return;
        }

        if (statusCode === DisconnectReason.loggedOut || statusCode === 401) {
          clearTimers();
          closePairingSocket();
          emitState({
            active: false,
            phase: 'error',
            qr: null,
            qr_data_url: null,
            pairing_code: null,
            error: 'Pairing was rejected or expired (401). Start pairing again.'
          });
          return;
        }

        const withinWindow = state.expires_at && Date.now() < state.expires_at;
        if (withinWindow && restartCount < 3) {
          setTimeout(() => {
            openPairingSocket(token, targetDigits, mode, restartCount + 1).catch(() => {});
          }, 1500);
        } else {
          clearTimers();
          closePairingSocket();
          emitState({
            active: false,
            phase: 'error',
            qr: null,
            qr_data_url: null,
            pairing_code: null,
            error: lastDisconnect?.error?.message || 'Pairing connection closed unexpectedly.'
          });
        }
      }
    });
  }

  async function start({ mode = 'qr', phone = '', clearAuth = true } = {}) {
    const normalizedMode = String(mode || 'qr').toLowerCase();
    if (normalizedMode !== 'qr' && normalizedMode !== 'code') {
      throw new Error("Invalid pairing mode. Expected 'qr' or 'code'.");
    }

    const digits = String(phone || '').replace(/\D/g, '');
    if (normalizedMode === 'code' && (digits.length < 8 || digits.length > 15)) {
      throw new Error('Phone number must include country code without + (8-15 digits).');
    }

    activeToken += 1;
    const myToken = activeToken;
    codeRequestedForToken = 0;
    clearTimers();
    closePairingSocket();

    if (typeof onBeforeStart === 'function') {
      await onBeforeStart();
    }

    if (clearAuth) {
      clearAuthDirectory(authDir);
    } else {
      fs.mkdirSync(authDir, { recursive: true });
    }

    const now = Date.now();
    emitState({
      active: true,
      phase: 'starting',
      mode: normalizedMode,
      qr: null,
      qr_data_url: null,
      pairing_code: null,
      target_phone_masked: digits ? maskPhone(digits) : null,
      error: null,
      started_at: now,
      expires_at: now + PAIRING_TTL_MS
    });

    expiryTimer = setTimeout(() => {
      if (myToken !== activeToken || !state.active) return;
      console.warn('[WhatsAppPairing] Pairing session timed out after 120s.');
      closePairingSocket();
      activeToken += 1;
      emitState({
        active: false,
        phase: 'error',
        qr: null,
        qr_data_url: null,
        pairing_code: null,
        error: 'Pairing timed out after 2 minutes. Click Start Pairing to try again.'
      });
    }, PAIRING_TTL_MS);

    try {
      await openPairingSocket(myToken, digits, normalizedMode, 0);
    } catch (err) {
      clearTimers();
      closePairingSocket();
      activeToken += 1;
      emitState({
        active: false,
        phase: 'error',
        qr: null,
        qr_data_url: null,
        pairing_code: null,
        error: err?.message || 'Failed to start WhatsApp pairing socket.'
      });
      throw err;
    }
    return getState();
  }

  async function cancel() {
    activeToken += 1;
    clearTimers();
    closePairingSocket();
    emitState({
      active: false,
      phase: 'cancelled',
      mode: null,
      qr: null,
      qr_data_url: null,
      pairing_code: null,
      error: null,
      expires_at: null
    });
    return getState();
  }

  return {
    start,
    cancel,
    getState,
    isActive
  };
}

