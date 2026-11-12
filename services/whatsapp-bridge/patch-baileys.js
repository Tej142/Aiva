import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

export function patchBaileys() {
  let anyModified = false;

  // 1. Patch Baileys messages-send.js
  const msPath = path.resolve(__dirname, 'node_modules/@whiskeysockets/baileys/lib/Socket/messages-send.js');
  if (fs.existsSync(msPath)) {
    let msContent = fs.readFileSync(msPath, 'utf8');

    const needle1 = `                const { user: meUser } = (0, WABinary_1.jidDecode)(meId);
                if (!participant) {
                    devices.push({ user });
                    if (user !== meUser) {
                        devices.push({ user: meUser });
                    }
                    if ((additionalAttributes === null || additionalAttributes === void 0 ? void 0 : additionalAttributes['category']) !== 'peer') {
                        const additionalDevices = await getUSyncDevices([meId, jid], !!useUserDevicesCache, true);
                        devices.push(...additionalDevices);
                    }
                }
                const allJids = [];
                const meJids = [];
                const otherJids = [];
                for (const { user, device } of devices) {
                    const isMe = user === meUser;`;

    const needle_prev = `                const { user: meUser } = (0, WABinary_1.jidDecode)(meId);
                const meLidUser = authState.creds.me?.lid ? (0, WABinary_1.jidDecode)(authState.creds.me.lid)?.user : undefined;
                const isTargetMe = user === meUser || (meLidUser && user === meLidUser);
                if (!participant) {
                    devices.push({ user });
                    if (!isTargetMe) {
                        devices.push({ user: meUser });
                    }
                    if ((additionalAttributes === null || additionalAttributes === void 0 ? void 0 : additionalAttributes['category']) !== 'peer') {
                        const additionalDevices = await getUSyncDevices([meId, jid], !!useUserDevicesCache, true);
                        devices.push(...additionalDevices);
                    }
                }
                const allJids = [];
                const meJids = [];
                const otherJids = [];
                for (const { user, device } of devices) {
                    const isMe = user === meUser || (meLidUser && user === meLidUser);`;

    const replacement1 = `                const { user: meUser, device: meDevice } = (0, WABinary_1.jidDecode)(meId);
                const meLidUser = authState.creds.me?.lid ? (0, WABinary_1.jidDecode)(authState.creds.me.lid)?.user : undefined;
                const meLidDevice = authState.creds.me?.lid ? (0, WABinary_1.jidDecode)(authState.creds.me.lid)?.device : undefined;
                const isTargetMe = user === meUser || (meLidUser && user === meLidUser);
                if (!participant) {
                    devices.push({ user });
                    if (!isTargetMe) {
                        devices.push({ user: meUser });
                    }
                    if ((additionalAttributes === null || additionalAttributes === void 0 ? void 0 : additionalAttributes['category']) !== 'peer') {
                        const additionalDevices = await getUSyncDevices([meId, jid], !!useUserDevicesCache, true);
                        devices.push(...additionalDevices);
                    }
                }
                const filteredDevices = devices.filter(d => {
                    const dev = d.device || 0;
                    const isOurDevice = (d.user === meUser && dev === (meDevice || 0)) ||
                                        (meLidUser && d.user === meLidUser && dev === (meLidDevice || meDevice || 0));
                    return !isOurDevice;
                });
                const allJids = [];
                const meJids = [];
                const otherJids = [];
                for (const { user, device } of filteredDevices) {
                    const isMe = user === meUser || (meLidUser && user === meLidUser);`;

    if (msContent.includes(needle1)) {
      msContent = msContent.replace(needle1, replacement1);
      anyModified = true;
    } else if (msContent.includes(needle_prev)) {
      msContent = msContent.replace(needle_prev, replacement1);
      anyModified = true;
    }

    const needle2 = `else if ((0, WABinary_1.areJidsSameUser)(participant.jid, meId)) {`;
    const replacement2 = `else if ((0, WABinary_1.areJidsSameUser)(participant.jid, meId) || (0, WABinary_1.areJidsSameUser)(participant.jid, authState.creds.me?.lid)) {`;
    if (msContent.includes(needle2)) {
      msContent = msContent.replace(needle2, replacement2);
      anyModified = true;
    }

    if (anyModified) {
      fs.writeFileSync(msPath, msContent, 'utf8');
      console.log('[WhatsAppBridge] Patched Baileys messages-send.js (LID & companion device filtering)');
    }
  }

  // 2. Patch libsignal session_cipher.js to reopen sessions actively used by sender
  const scPath = path.resolve(__dirname, 'node_modules/libsignal/src/session_cipher.js');
  if (fs.existsSync(scPath)) {
    let scContent = fs.readFileSync(scPath, 'utf8');
    const scNeedle = `            if (record.isClosed(result.session)) {
                // It's possible for this to happen when processing a backlog of messages.
                // The message was, hopefully, just sent back in a time when this session
                // was the most current.  Simply make a note of it and continue.  If our
                // actual open session is for reason invalid, that must be handled via
                // a full SessionError response.
                console.warn("Decrypted message with closed session.");
            }
            await this.storeRecord(record);`;

    const scReplacement = `            if (record.isClosed(result.session)) {
                console.info("[Signal] Decrypted message with closed session. Reopening session actively used by sender...");
                const openSession = record.getOpenSession();
                if (openSession && openSession !== result.session) {
                    record.closeSession(openSession);
                }
                record.openSession(result.session);
            }
            await this.storeRecord(record);`;

    if (scContent.includes(scNeedle)) {
      scContent = scContent.replace(scNeedle, scReplacement);
      fs.writeFileSync(scPath, scContent, 'utf8');
      console.log('[WhatsAppBridge] Patched libsignal session_cipher.js (auto-reopen active session)');
      anyModified = true;
    }
  }

  // 3. Patch Baileys decode-wa-message.js to preserve sender_pn / sender_lid / participant_pn on key
  const dwmPath = path.resolve(__dirname, 'node_modules/@whiskeysockets/baileys/lib/Utils/decode-wa-message.js');
  if (fs.existsSync(dwmPath)) {
    let dwmContent = fs.readFileSync(dwmPath, 'utf8');
    const dwmNeedle = `    const key = {
        remoteJid: chatId,
        fromMe,
        id: msgId,
        participant
    };`;
    const dwmReplacement = `    const stanzaAttrs = (stanza && stanza.attrs) || {};
    const key = {
        remoteJid: chatId,
        fromMe,
        id: msgId,
        senderLid: stanzaAttrs.sender_lid,
        senderPn: stanzaAttrs.sender_pn,
        participant,
        participantPn: stanzaAttrs.participant_pn,
        participantLid: stanzaAttrs.participant_lid,
        remoteJidAlt: stanzaAttrs.sender_pn || stanzaAttrs.recipient_pn || stanzaAttrs.peer_recipient_pn
    };`;
    if (dwmContent.includes(dwmNeedle)) {
      dwmContent = dwmContent.replace(dwmNeedle, dwmReplacement);
      fs.writeFileSync(dwmPath, dwmContent, 'utf8');
      console.log('[WhatsAppBridge] Patched Baileys decode-wa-message.js (preserve senderPn/senderLid)');
      anyModified = true;
    }
  }

  return anyModified;
}

// Run immediately if executed directly
if (process.argv[1] === fileURLToPath(import.meta.url)) {
  patchBaileys();
}
