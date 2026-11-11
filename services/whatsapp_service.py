import asyncio
import base64
import hmac
import json
import os
import random
import re
import time
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple
from dotenv import load_dotenv

from core.shared.logger import get_logger

# Load environment variables
load_dotenv()

logger = get_logger("WhatsAppService")

# Configuration keys
WHATSAPP_ENABLED = os.getenv("WHATSAPP_ENABLED", "false").lower() in ("true", "1", "yes")
AIVA_WHATSAPP_NUMBER = re.sub(r"\D", "", os.getenv("AIVA_WHATSAPP_NUMBER", ""))
WHATSAPP_OWNER_EMAIL = os.getenv("WHATSAPP_OWNER_EMAIL", "").strip().lower()
WHATSAPP_BRIDGE_PORT = int(os.getenv("WHATSAPP_BRIDGE_PORT", "3001"))
WHATSAPP_BRIDGE_SECRET = os.getenv("WHATSAPP_BRIDGE_SECRET", "")
WHATSAPP_BACKEND_URL = os.getenv("WHATSAPP_BACKEND_URL", "http://127.0.0.1:5000").rstrip("/")
WHATSAPP_REPLY_DELAY_MS = int(os.getenv("WHATSAPP_REPLY_DELAY_MS", "700"))
WHATSAPP_MAX_OUTBOUND_PER_HOUR = int(os.getenv("WHATSAPP_MAX_OUTBOUND_PER_HOUR", "20"))
WHATSAPP_MAX_MESSAGE_CHARS = int(os.getenv("WHATSAPP_MAX_MESSAGE_CHARS", "1500"))
WHATSAPP_ALLOW_POWER_CONTROL = os.getenv("WHATSAPP_ALLOW_POWER_CONTROL", "false").lower() in ("true", "1", "yes")


def validate_config():
    """Validates configuration at startup if WhatsApp is enabled."""
    if not WHATSAPP_ENABLED:
        return
    required = {
        "AIVA_WHATSAPP_NUMBER": AIVA_WHATSAPP_NUMBER,
        "WHATSAPP_OWNER_EMAIL": WHATSAPP_OWNER_EMAIL,
        "WHATSAPP_BRIDGE_SECRET": WHATSAPP_BRIDGE_SECRET,
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        raise ValueError(
            f"[WhatsAppConfigError] WHATSAPP_ENABLED is true but required settings are missing: {', '.join(missing)}"
        )


def normalize_phone(raw: str) -> str:
    """Normalizes phone number to digits only (E.164 without leading +)."""
    if not raw:
        return ""
    digits = re.sub(r"\D", "", str(raw))
    # Remove leading 00 if international prefix
    if digits.startswith("00"):
        digits = digits[2:]
    return digits


def mask_phone(phone: str) -> str:
    """Masks all but the last 4 digits of a phone number for privacy."""
    p = normalize_phone(phone)
    if len(p) <= 4:
        return "****"
    return f"+{'*' * (len(p) - 4)}{p[-4:]}"


def verify_bridge_secret(provided_secret: str) -> bool:
    """Constant-time comparison for the bridge shared secret."""
    if not WHATSAPP_BRIDGE_SECRET or not provided_secret:
        return False
    return hmac.compare_digest(str(provided_secret).strip(), WHATSAPP_BRIDGE_SECRET.strip())


def format_for_whatsapp(text: str, max_chars: int = WHATSAPP_MAX_MESSAGE_CHARS) -> str:
    """
    Formats text for WhatsApp messaging:
    - Retains URLs intact.
    - Converts Markdown bold (**word**) to WhatsApp bold (*word*).
    - Converts Markdown italic (*word*) to WhatsApp italic (_word_).
    - Strips HTML tags and markdown headers (#, ##).
    - Normalizes excess blank lines.
    - Cleanly truncates at sentence or word boundaries at max_chars.
    """
    if not text:
        return ""

    s = text.strip()

    # 1. Protect URLs by replacing with placeholder tokens
    urls = []
    def _url_repl(match):
        urls.append(match.group(0))
        return f"__WA_URL_TOKEN_{len(urls)-1}__"

    url_pattern = re.compile(r"https?://[^\s]+", re.IGNORECASE)
    s = url_pattern.sub(_url_repl, s)

    # 2. Strip HTML tags
    s = re.sub(r"<[^>]+>", "", s)

    # 3. Markdown headers: ### Heading -> **Heading** (treated as bold)
    s = re.sub(r"^#{1,6}\s*(.+)$", r"**\1**", s, flags=re.MULTILINE)

    # 4. Convert single *italic* to _italic_ first (so newly formed *bold* isn't affected)
    s = re.sub(r"(?<!\*)\*(?!\*)([^\*\n]+?)(?<!\*)\*(?!\*)", r"_\1_", s)

    # 5. Convert **bold** to *bold*
    s = re.sub(r"\*\*([^\*]+?)\*\*", r"*\1*", s)

    # 6. Convert ~~strike~~ to ~strike~
    s = re.sub(r"~~([^~]+)~~", r"~\1~", s)

    # 7. Restore protected URLs
    for idx, u in enumerate(urls):
        s = s.replace(f"__WA_URL_TOKEN_{idx}__", u)

    # 8. Collapse 3+ newlines into 2
    s = re.sub(r"\n{3,}", "\n\n", s)

    # 9. Clean truncation at boundary if exceeding max_chars
    if len(s) > max_chars:
        candidate = s[:max_chars]
        # Look for the last newline or punctuation in the last 150 chars
        split_idx = -1
        for punct in ["\n\n", "\n", ". ", "! ", "? ", "; "]:
            last_pos = candidate.rfind(punct)
            if last_pos > max_chars - 200:
                split_idx = last_pos + len(punct.rstrip())
                break
        if split_idx == -1:
            last_space = candidate.rfind(" ")
            if last_space > max_chars - 100:
                split_idx = last_space
            else:
                split_idx = max_chars - 3
        s = candidate[:split_idx].rstrip() + "..."

    return s.strip()


class WhatsAppService:
    def __init__(self, db, kernel=None):
        self.db = db
        self.kernel = kernel
        # Sliding-window rate limit: user_id -> [timestamp, ...]
        self._outbound_timestamps: Dict[str, List[float]] = defaultdict(list)
        # Rate limit unlinked polite notices: phone -> last_notified_ts
        self._unlinked_notice_ts: Dict[str, float] = {}
        # Inbound deduplication: message_id -> timestamp
        self._processed_message_ids: Dict[str, float] = {}
        # Power control 60s pending confirmations: user_id -> {action, command, expires_at}
        self._pending_power_actions: Dict[str, Dict[str, Any]] = {}

        # Validate configuration if enabled
        try:
            validate_config()
        except ValueError as err:
            logger.error(str(err))

    # ─────────────────────────────────────────────────────────────
    # RATE LIMITING & DEDUPLICATION
    # ─────────────────────────────────────────────────────────────
    def check_outbound_rate_limit(self, user_id: str) -> bool:
        """Checks if user has exceeded WHATSAPP_MAX_OUTBOUND_PER_HOUR within the last hour."""
        now = time.time()
        window = 3600.0
        ts_list = [ts for ts in self._outbound_timestamps[user_id] if now - ts < window]
        self._outbound_timestamps[user_id] = ts_list
        return len(ts_list) < WHATSAPP_MAX_OUTBOUND_PER_HOUR

    def record_outbound_send(self, user_id: str):
        self._outbound_timestamps[user_id].append(time.time())

    def is_message_duplicate(self, message_id: str) -> bool:
        """Returns True if message_id was seen in the last 10 minutes."""
        if not message_id:
            return False
        now = time.time()
        # Clean older than 600s
        self._processed_message_ids = {
            m_id: ts for m_id, ts in self._processed_message_ids.items() if now - ts < 600
        }
        if message_id in self._processed_message_ids:
            return True
        self._processed_message_ids[message_id] = now
        return False

    # ─────────────────────────────────────────────────────────────
    # STATUS & LINKING
    # ─────────────────────────────────────────────────────────────
    def is_bridge_owner(self, user: Optional[Dict[str, Any]]) -> bool:
        """True if the user is authorized to manage Baileys bridge-level pairing."""
        if not user or not user.get("id"):
            return False
        if WHATSAPP_OWNER_EMAIL:
            return str(user.get("email", "")).strip().lower() == WHATSAPP_OWNER_EMAIL
        return True

    async def _bridge_request(
        self,
        method: str,
        path: str,
        payload: Optional[Dict[str, Any]] = None,
        timeout: float = 5.0
    ) -> Tuple[int, Dict[str, Any]]:
        """Performs an authenticated loopback HTTP request to the Baileys Node bridge."""
        import urllib.request
        import urllib.error

        url = f"http://127.0.0.1:{WHATSAPP_BRIDGE_PORT}{path}"
        headers = {"X-Bridge-Secret": WHATSAPP_BRIDGE_SECRET}
        data_bytes = None
        if payload is not None:
            data_bytes = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=data_bytes, headers=headers, method=method.upper())
        loop = asyncio.get_event_loop()

        def _do_req():
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    raw = resp.read().decode("utf-8")
                    return resp.status, json.loads(raw) if raw else {}
            except urllib.error.HTTPError as he:
                raw_err = he.read().decode("utf-8", errors="replace")
                try:
                    return he.code, json.loads(raw_err)
                except Exception:
                    return he.code, {"error": "bridge_http_error", "message": raw_err or str(he)}

        try:
            return await loop.run_in_executor(None, _do_req)
        except Exception as e:
            logger.debug(f"[WhatsApp] Bridge request failed method={method} path={path}: {e}")
            return 503, {
                "connected": False,
                "status": "bridge_offline",
                "error": "WhatsApp bridge service is unreachable.",
                "message": str(e),
            }

    async def get_bridge_status(self) -> Dict[str, Any]:
        """Pings the Node.js Baileys bridge for connection and pairing status."""
        code, body = await self._bridge_request("GET", "/status", timeout=2.5)
        if code >= 500 or not isinstance(body, dict):
            return {
                "connected": False,
                "status": "bridge_offline",
                "bridge_state": "bridge_offline",
                "phone_masked": "",
                "configured_phone_masked": mask_phone(AIVA_WHATSAPP_NUMBER) if AIVA_WHATSAPP_NUMBER else "",
                "error": body.get("error", "Bridge offline") if isinstance(body, dict) else "Bridge offline",
                "pairing": {"active": False, "phase": "idle"},
            }

        raw_phone = body.get("phone", "") or ""
        return {
            "connected": bool(body.get("connected", False)),
            "status": body.get("status", "disconnected"),
            "bridge_state": body.get("bridge_state", body.get("status", "disconnected")),
            "phone_masked": mask_phone(raw_phone) if raw_phone else "",
            "configured_phone_masked": mask_phone(AIVA_WHATSAPP_NUMBER) if AIVA_WHATSAPP_NUMBER else "",
            "error": body.get("error"),
            "pairing": body.get("pairing") or {"active": False, "phase": "idle"},
        }

    async def get_user_status(self, user_id: str, user: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if user is None and self.db and hasattr(self.db, "get_user_by_id"):
            user = self.db.get_user_by_id(user_id)
        is_owner = self.is_bridge_owner(user)
        bridge_info = await self.get_bridge_status()

        # Never leak active pairing QR/code payload to non-owner users
        if not is_owner and isinstance(bridge_info.get("pairing"), dict):
            p = bridge_info["pairing"]
            bridge_info["pairing"] = {
                "active": bool(p.get("active", False)),
                "phase": p.get("phase", "idle"),
            }

        links = self.db.get_whatsapp_links_for_user(user_id)
        masked_links = []
        for l in links:
            item = dict(l)
            item["phone_masked"] = mask_phone(item.get("phone_e164", ""))
            masked_links.append(item)

        contacts = self.db.get_whatsapp_contacts(user_id)
        return {
            "whatsapp_enabled": WHATSAPP_ENABLED,
            "is_owner": is_owner,
            "bridge": bridge_info,
            "links": masked_links,
            "contacts_count": len(contacts),
        }

    async def start_bridge_pairing(
        self,
        user: Dict[str, Any],
        mode: str = "qr",
        raw_phone: str = ""
    ) -> Tuple[int, Dict[str, Any]]:
        """Starts Baileys QR or 8-digit phone pairing mode on the Node bridge (owner-only)."""
        if not self.is_bridge_owner(user):
            return 403, {
                "status": "error",
                "message": "Only the primary AIVA account owner can pair or reset the WhatsApp bridge."
            }

        clean_mode = (mode or "qr").strip().lower()
        if clean_mode not in ("qr", "code"):
            return 400, {"status": "error", "message": "Invalid pairing mode. Use 'qr' or 'code'."}

        digits = normalize_phone(raw_phone)
        if clean_mode == "code" and (not digits or len(digits) < 8 or len(digits) > 15):
            return 400, {
                "status": "error",
                "message": "Enter a valid phone number including country code (8-15 digits, e.g. 919876543210)."
            }

        code, resp = await self._bridge_request(
            "POST",
            "/pair/start",
            payload={"mode": clean_mode, "phone": digits},
            timeout=8.0,
        )
        logger.info(f"[WhatsApp] stage=pair_start user={user['id']} mode={clean_mode} http={code}")
        if code >= 400:
            return code, {
                "status": "error",
                "message": resp.get("message") or resp.get("error") or "Failed to start WhatsApp pairing mode."
            }
        return 200, {"status": "success", "data": {"pairing": resp.get("pairing") or {}}}

    async def cancel_bridge_pairing(self, user: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        """Cancels an active WhatsApp bridge pairing session (owner-only)."""
        if not self.is_bridge_owner(user):
            return 403, {
                "status": "error",
                "message": "Only the primary AIVA account owner can cancel bridge pairing."
            }
        code, resp = await self._bridge_request("POST", "/pair/cancel", payload={}, timeout=5.0)
        logger.info(f"[WhatsApp] stage=pair_cancel user={user['id']} http={code}")
        if code >= 400:
            return code, {
                "status": "error",
                "message": resp.get("message") or resp.get("error") or "Failed to cancel pairing."
            }
        return 200, {"status": "success", "data": {"pairing": resp.get("pairing") or {}}}

    async def logout_bridge(self, user: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        """Logs out the active Baileys session and clears auth credentials for re-pairing (owner-only)."""
        if not self.is_bridge_owner(user):
            return 403, {
                "status": "error",
                "message": "Only the primary AIVA account owner can unpair the WhatsApp bridge."
            }
        code, resp = await self._bridge_request("POST", "/logout", payload={}, timeout=6.0)
        logger.info(f"[WhatsApp] stage=bridge_logout user={user['id']} http={code}")
        if code >= 400:
            return code, {
                "status": "error",
                "message": resp.get("message") or resp.get("error") or "Failed to unpair WhatsApp bridge."
            }
        return 200, {"status": "success", "data": resp}

    def start_link(self, user_id: str, raw_phone: str) -> Dict[str, Any]:
        """Generates a 6-digit OTP code valid for 10 minutes to link a WhatsApp phone."""
        phone = normalize_phone(raw_phone)
        if not phone or len(phone) < 7:
            raise ValueError("Invalid phone number. Include country code without '+' (e.g. 919876543210).")

        code = f"{random.randint(100000, 999999)}"
        expires_at = time.time() + 600.0  # 10 minutes
        self.db.create_or_update_whatsapp_link(user_id, phone, code, expires_at)
        logger.info(f"[WhatsApp] stage=link_start user={user_id} phone={mask_phone(phone)} status=pending")
        return {
            "phone_e164": phone,
            "link_code": code,
            "expires_at": expires_at,
            "instructions": f"Send 'LINK {code}' from your WhatsApp to AIVA's number."
        }

    def unlink_number(self, user_id: str, raw_phone: str) -> bool:
        phone = normalize_phone(raw_phone)
        res = self.db.unlink_whatsapp(user_id, phone)
        logger.info(f"[WhatsApp] stage=unlink user={user_id} phone={mask_phone(phone)} success={res}")
        return res

    # ─────────────────────────────────────────────────────────────
    # OUTBOUND SEND TO NODE BRIDGE
    # ─────────────────────────────────────────────────────────────
    async def send_whatsapp_message(
        self,
        to_phone: str,
        message: str,
        user_id: Optional[str] = None,
        to_jid: Optional[str] = None
    ) -> Dict[str, Any]:
        """Formats and sends an outbound WhatsApp message via the Baileys bridge."""
        phone = normalize_phone(to_phone)
        if not phone:
            return {"status": "error", "error": "invalid_phone"}

        if user_id:
            if not self.check_outbound_rate_limit(user_id):
                logger.warning(f"[WhatsApp] stage=send status=rate_limited user={user_id}")
                return {"status": "error", "error": "rate_limit_exceeded"}

        clean_text = format_for_whatsapp(message)
        if not clean_text:
            return {"status": "error", "error": "empty_message"}

        # Human-like delay with random jitter (e.g. 700ms - 1000ms)
        delay_sec = (WHATSAPP_REPLY_DELAY_MS / 1000.0) + (random.uniform(0.05, 0.25))
        await asyncio.sleep(delay_sec)

        import urllib.request
        import urllib.error

        target_jid = to_jid if (to_jid and "@" in to_jid) else f"{phone}@s.whatsapp.net"
        payload = json.dumps({
            "to": phone,
            "to_jid": target_jid,
            "text": clean_text
        }).encode("utf-8")

        url = f"http://127.0.0.1:{WHATSAPP_BRIDGE_PORT}/send"
        req = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "X-Bridge-Secret": WHATSAPP_BRIDGE_SECRET
            },
            method="POST"
        )

        t0 = time.time()
        try:
            loop = asyncio.get_event_loop()
            def _post():
                with urllib.request.urlopen(req, timeout=10.0) as resp:
                    return json.loads(resp.read().decode("utf-8"))

            bridge_resp = await loop.run_in_executor(None, _post)
            duration_ms = (time.time() - t0) * 1000.0

            if user_id:
                self.record_outbound_send(user_id)
                self.db.log_whatsapp_event(user_id, "outbound", "send", "ok", duration_ms)

            logger.info(
                f"[WhatsApp] stage=send to={mask_phone(phone)} duration_ms={duration_ms:.1f} status=ok"
            )
            return {"status": "success", "bridge_response": bridge_resp}

        except Exception as e:
            duration_ms = (time.time() - t0) * 1000.0
            if user_id:
                self.db.log_whatsapp_event(user_id, "outbound", "send", "error", duration_ms)
            logger.error(
                f"[WhatsApp] stage=send to={mask_phone(phone)} duration_ms={duration_ms:.1f} status=error error={e}"
            )
            return {"status": "error", "error": str(e)}

    # ─────────────────────────────────────────────────────────────
    # CONTACT RESOLUTION
    # ─────────────────────────────────────────────────────────────
    def resolve_contact(self, user_id: str, query: str) -> Dict[str, Any]:
        """
        Resolves contact for user:
        - 1 match: returns {"status": "found", "contact": c}
        - >1 matches: returns {"status": "ambiguous", "matches": [...], "message": ...}
        - 0 matches: returns {"status": "not_found", "message": ...}
        """
        matches = self.db.resolve_whatsapp_contact(user_id, query)
        if not matches:
            return {
                "status": "not_found",
                "message": (
                    f"I couldn't find '{query}' in your contacts. "
                    f"Please save them first (e.g. 'save 9876543210 as {query}')."
                )
            }
        if len(matches) == 1:
            return {"status": "found", "contact": matches[0]}

        # Disambiguation
        names = [f"'{c['display_name']}' ({mask_phone(c['phone_e164'])})" for c in matches]
        return {
            "status": "ambiguous",
            "matches": matches,
            "message": f"Found multiple contacts matching '{query}': {', '.join(names)}. Which one did you mean?"
        }

    # ─────────────────────────────────────────────────────────────
    # INBOUND MESSAGE PROCESSING
    # ─────────────────────────────────────────────────────────────
    async def handle_inbound(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Processes inbound WhatsApp messages received from Node bridge:
        - Deduplication and loop prevention
        - Sender normalization and account resolution
        - Linking authorization ('LINK <code>')
        - Built-in commands (HELP, STATUS, STOP/UNLINK, contact management)
        - Voice note audio transcription via Whisper
        - Goal & compound command execution (Research, messaging, device commands)
        """
        t0 = time.time()
        message_id = payload.get("message_id", "")
        sender_raw = payload.get("sender", "")
        chat_jid = payload.get("chat_jid") or (sender_raw if "@" in sender_raw else "")
        text = (payload.get("text") or "").strip()
        audio_base64 = payload.get("audio_base64")
        from_me = bool(payload.get("from_me", False))

        # 1. Deduplication
        if self.is_message_duplicate(message_id):
            return {"status": "ignored_duplicate"}

        # 2. Extract digits phone
        sender_phone = normalize_phone(sender_raw.split("@")[0] if "@" in sender_raw else sender_raw)
        if not sender_phone:
            return {"status": "ignored_invalid_sender"}

        masked = mask_phone(sender_phone)
        logger.info(f"[WhatsApp] stage=inbound_recv sender={masked} has_audio={bool(audio_base64)}")

        # 3. Handle Voice Note Audio Transcription if present
        if audio_base64:
            try:
                audio_bytes = base64.b64decode(audio_base64)
                if self.kernel and hasattr(self.kernel, "voice_service"):
                    voice_svc = self.kernel.voice_service
                    if hasattr(voice_svc, "transcribe_bytes"):
                        transcript = await voice_svc.transcribe_bytes(audio_bytes, filename="voice.ogg")
                        if transcript:
                            text = transcript
                            logger.info(f"[WhatsApp] stage=audio_transcribed text_len={len(text)}")
            except Exception as e:
                logger.warning(f"[WhatsApp] stage=audio_transcribe_err error={e}")

        clean_upper = text.strip().upper()

        # 4. Check Linking Command: 'LINK <code>'
        if clean_upper.startswith("LINK "):
            code = clean_upper.split(" ", 1)[1].strip()
            verified = self.db.verify_whatsapp_link(sender_phone, code)
            if verified:
                reply = (
                    "🎉 Success! Your WhatsApp is now linked to your AIVA account.\n\n"
                    "You can now talk to AIVA directly, ask questions, or run commands.\n"
                    "Send *help* to see available features."
                )
                await self.send_whatsapp_message(sender_phone, reply, to_jid=chat_jid if chat_jid else None)
                return {"status": "link_verified", "phone": sender_phone}
            else:
                reply = (
                    "⚠️ Invalid or expired link code. Please start linking again from "
                    "the AIVA HUD Settings to get a fresh 6-digit code."
                )
                await self.send_whatsapp_message(sender_phone, reply, to_jid=chat_jid if chat_jid else None)
                return {"status": "link_rejected"}

        # 5. Resolve User Account
        user_id = None
        # Check self-chat on AIVA's own number
        if from_me or sender_phone == AIVA_WHATSAPP_NUMBER:
            sender_phone = AIVA_WHATSAPP_NUMBER
            if WHATSAPP_OWNER_EMAIL:
                owner = self.db.get_user_by_email(WHATSAPP_OWNER_EMAIL)
                if owner:
                    user_id = owner["id"]

        if not user_id:
            link = self.db.get_whatsapp_link_by_phone(sender_phone)
            if link:
                user_id = link["user_id"]

        # Helper to reply directly to the inbound sender in the correct chat thread
        async def _reply_sender(msg: str) -> Dict[str, Any]:
            return await self.send_whatsapp_message(
                sender_phone,
                msg,
                user_id=user_id,
                to_jid=chat_jid if chat_jid else None
            )

        # 6. Reject unknown / unlinked senders (NO LLM OR TOOLS EXECUTED)
        if not user_id:
            now = time.time()
            last_sent = self._unlinked_notice_ts.get(sender_phone, 0.0)
            if now - last_sent > 3600.0:  # Rate limit polite notice: 1/hr
                self._unlinked_notice_ts[sender_phone] = now
                polite_notice = (
                    "👋 Hello! This WhatsApp number is not linked to an active AIVA account.\n\n"
                    "To use AIVA over WhatsApp, please open your AIVA HUD Settings, "
                    "click 'Link WhatsApp', and follow the instructions."
                )
                await _reply_sender(polite_notice)
            return {"status": "ignored_unlinked"}

        # 7. Built-in Commands: HELP, STATUS, STOP/UNLINK
        if clean_upper == "HELP":
            help_msg = (
                "🤖 *AIVA WhatsApp Assistant*\n\n"
                "• *Tasks & Chat*: Send any question or device instruction.\n"
                "• *Send a Message*: 'message dad come early this evening'\n"
                "• *Research & Share*: 'search about Tirupati and send details to my second number'\n"
                "• *Save Contact*: 'save 9876543210 as dad'\n"
                "• *Add Alias*: 'add alias papa for dad'\n"
                "• *List Contacts*: 'list my contacts'\n"
                "• *Status*: 'status'\n"
                "• *Unlink*: 'unlink' or 'stop'"
            )
            await _reply_sender(help_msg)
            return {"status": "command_help"}

        if clean_upper in ("STOP", "UNLINK"):
            self.db.unlink_whatsapp(user_id, sender_phone)
            await _reply_sender(
                "👋 Your WhatsApp number has been unlinked from AIVA. You can reconnect anytime in HUD Settings."
            )
            return {"status": "unlinked"}

        if clean_upper == "STATUS":
            contacts = self.db.get_whatsapp_contacts(user_id)
            status_msg = (
                "🟢 *AIVA Status: Connected*\n"
                f"• Account: Linked\n"
                f"• Verified Number: {masked}\n"
                f"• Saved Contacts: {len(contacts)}\n"
                "All systems nominal."
            )
            await _reply_sender(status_msg)
            return {"status": "command_status"}

        # 8. Contact Management by Chat
        # "save <number> as <name>"
        save_match = re.match(r"^save\s+([+0-9\s-]+)\s+as\s+(.+)$", text, re.IGNORECASE)
        if save_match:
            c_phone = normalize_phone(save_match.group(1))
            c_name = save_match.group(2).strip()
            if c_phone and c_name:
                saved_contact = self.db.add_whatsapp_contact(user_id, c_name, c_phone, aliases=[c_name.lower()])
                if self.kernel and getattr(self.kernel, "memory_engine", None) and saved_contact:
                    try:
                        self.kernel.memory_engine.store.sync_whatsapp_contact_entity(user_id, saved_contact)
                    except Exception:
                        pass
                reply = f"✅ Contact saved: *{c_name}* ({mask_phone(c_phone)})."
                await _reply_sender(reply)
                return {"status": "contact_saved"}

        # "add alias <alias> for <name>"
        alias_match = re.match(r"^add\s+alias\s+(.+?)\s+for\s+(.+)$", text, re.IGNORECASE)
        if alias_match:
            alias_val = alias_match.group(1).strip()
            target_name = alias_match.group(2).strip()
            resolution = self.resolve_contact(user_id, target_name)
            if resolution["status"] == "found":
                c = resolution["contact"]
                current_aliases = c.get("aliases", [])
                if alias_val.lower() not in [a.lower() for a in current_aliases]:
                    current_aliases.append(alias_val)
                    updated_contact = self.db.update_whatsapp_contact(user_id, c["id"], aliases=current_aliases)
                    if self.kernel and getattr(self.kernel, "memory_engine", None):
                        try:
                            self.kernel.memory_engine.store.sync_whatsapp_contact_entity(
                                user_id,
                                updated_contact or {**c, "aliases": current_aliases},
                            )
                        except Exception:
                            pass
                reply = f"✅ Added alias *{alias_val}* for contact *{c['display_name']}*."
                await _reply_sender(reply)
                return {"status": "alias_added"}
            else:
                await _reply_sender(resolution["message"])
                return {"status": "alias_failed"}

        # "list my contacts" / "list contacts"
        if re.match(r"^list\s+(my\s+)?contacts$", text, re.IGNORECASE):
            contacts = self.db.get_whatsapp_contacts(user_id)
            if not contacts:
                reply = "You have no saved WhatsApp contacts yet. Save one with: 'save <number> as <name>'."
            else:
                lines = ["📒 *Your WhatsApp Contacts:*"]
                for c in contacts:
                    aliases_str = f" [aliases: {', '.join(c['aliases'])}]" if c.get("aliases") else ""
                    trusted_icon = "⭐" if c.get("trusted") else ""
                    lines.append(f"• {c['display_name']} ({mask_phone(c['phone_e164'])}){trusted_icon}{aliases_str}")
                reply = "\n".join(lines)
            await _reply_sender(reply)
            return {"status": "contacts_listed"}

        # 9. Power Control Guard
        lower_text = text.lower()
        is_power_cmd = any(w in lower_text for w in ["shutdown", "restart", "reboot", "hibernate", "sleep pc"])
        if is_power_cmd:
            if not WHATSAPP_ALLOW_POWER_CONTROL:
                reply = "🔒 Power control commands are disabled via WhatsApp for security."
                await _reply_sender(reply)
                return {"status": "power_blocked"}

            # Two-step confirmation within 60s
            pending = self._pending_power_actions.get(user_id)
            if clean_upper == "CONFIRM" and pending and time.time() < pending["expires_at"]:
                cmd_to_run = pending["command"]
                del self._pending_power_actions[user_id]
                # Execute power command through kernel
                await _reply_sender("⚡ Executing power command...")
                if self.kernel and hasattr(self.kernel, "device_channel"):
                    res = await self.kernel.device_channel.dispatch_command_async("system_power", {"action": cmd_to_run})
                    return {"status": "power_executed", "result": res}
            else:
                # Stage confirmation
                self._pending_power_actions[user_id] = {
                    "command": "shutdown" if "shutdown" in lower_text else "restart",
                    "expires_at": time.time() + 60.0
                }
                reply = "⚠️ Warning: You requested a system power action. Reply *CONFIRM* within 60 seconds to execute."
                await _reply_sender(reply)
                return {"status": "power_confirmation_requested"}

        # 10. Compound Command 1: "message <contact> <text>"
        # e.g. "message dad to come early this evening", "message dad please come early"
        msg_match = re.match(
            r"^(?:message|send a message to|text|tell)\s+([a-zA-Z0-9\s]+?)\s+(?:to\s+|that\s+|saying\s+|:)?\s*(.+)$",
            text,
            re.IGNORECASE
        )
        if msg_match and not any(k in lower_text for k in ["search", "find", "research"]):
            target_name = msg_match.group(1).strip()
            msg_content = msg_match.group(2).strip()

            # Clean up leading "to " if caught
            if msg_content.lower().startswith("to "):
                msg_content = msg_content[3:].strip()

            resolution = self.resolve_contact(user_id, target_name)
            if resolution["status"] != "found":
                await _reply_sender(resolution["message"])
                return {"status": "contact_resolution_failed"}

            contact = resolution["contact"]
            out_res = await self.send_whatsapp_message(contact["phone_e164"], msg_content, user_id=user_id)
            if out_res.get("status") == "success":
                confirm_reply = f"✅ Sent to *{contact['display_name']}*: \"{msg_content}\""
            else:
                confirm_reply = f"❌ Failed to send message to *{contact['display_name']}* ({out_res.get('error')})."
            await _reply_sender(confirm_reply)
            return {"status": "message_sent", "contact": contact["display_name"]}

        # 11. Compound Command 2: "search about <X> and send ... to <Y>"
        # e.g. "search about Government Polytechnic, Tirupati and send the details to my second number"
        compound_search_match = re.match(
            r"^(?:search|find|research)(?:\s+about|\s+for)?\s+(.+?)\s+and\s+(?:send|forward|message)\s+(?:the\s+details\s+|it\s+)?to\s+(.+)$",
            text,
            re.IGNORECASE
        )
        if compound_search_match:
            search_query = compound_search_match.group(1).strip()
            dest_target = compound_search_match.group(2).strip()

            resolution = self.resolve_contact(user_id, dest_target)
            if resolution["status"] != "found":
                await _reply_sender(resolution["message"])
                return {"status": "contact_resolution_failed"}

            target_contact = resolution["contact"]

            # Immediate acknowledgment for slow research task
            await _reply_sender("🔍 On it, researching now...")

            summary = await self._run_research_and_summarize(search_query, user_id=user_id)

            # Send research summary to target contact
            send_res = await self.send_whatsapp_message(target_contact["phone_e164"], summary, user_id=user_id)
            if send_res.get("status") == "success":
                done_reply = f"✅ Sent research on *{search_query}* to *{target_contact['display_name']}*."
            else:
                done_reply = f"❌ Found research but failed to send to *{target_contact['display_name']}*."
            await _reply_sender(done_reply)
            return {"status": "research_sent", "target": target_contact["display_name"]}

        # 12. General conversational query or device command
        response_text = await self._handle_general_query(user_id, text)
        if response_text:
            await _reply_sender(response_text)

        duration_ms = (time.time() - t0) * 1000.0
        self.db.log_whatsapp_event(user_id, "inbound", "handle_inbound", "ok", duration_ms)
        return {"status": "processed"}

    async def _run_research_and_summarize(self, query: str, user_id: Optional[str] = None) -> str:
        """Executes research using Tavily/ResearchAgent and formats a short WhatsApp summary with sources."""
        raw_result = ""
        source_url = ""

        # Try existing ResearchAgent in kernel
        if self.kernel and hasattr(self.kernel, "research_agent") and self.kernel.research_agent:
            try:
                res = await self.kernel.research_agent.execute({"query": query, "topic": query, "user_id": user_id})
                if isinstance(res, dict):
                    raw_result = res.get("summary") or res.get("content") or str(res)
                    source_url = res.get("source") or res.get("url") or ""
                elif isinstance(res, str):
                    raw_result = res
            except Exception as e:
                logger.warning(f"[WhatsApp] ResearchAgent execute error: {e}")

        # Fallback to Tavily directly if needed
        if not raw_result:
            try:
                from core.shared.config_loader import load_secrets
                secrets = load_secrets()
                tavily_key = secrets.get("tavily_api_key")
                if tavily_key:
                    from tavily import TavilyClient
                    tclient = TavilyClient(api_key=tavily_key)
                    t_res = tclient.search(query=query, max_results=2)
                    if t_res and "results" in t_res and len(t_res["results"]) > 0:
                        first = t_res["results"][0]
                        raw_result = first.get("content", "")
                        source_url = first.get("url", "")
            except Exception as e:
                logger.warning(f"[WhatsApp] Tavily direct search error: {e}")

        if not raw_result:
            return f"Couldn't find reliable details about '{query}' right now."

        # Store web research cleanly as time-bounded knowledge (never personal user memory)
        if user_id and self.kernel and getattr(self.kernel, "memory_engine", None):
            try:
                self.kernel.memory_engine.store_knowledge(
                    user_id=user_id,
                    topic=query,
                    summary=raw_result[:500],
                    ttl_days=30,
                    channel="whatsapp",
                )
            except Exception:
                pass

        # Keep summary short (plain text, a few lines, include source link)
        summary_clean = raw_result.strip()
        if len(summary_clean) > 800:
            summary_clean = summary_clean[:800].rstrip() + "..."

        out = f"📚 *Details for {query}:*\n\n{summary_clean}"
        if source_url:
            out += f"\n\nSource: {source_url}"
        return out

    def recall(self, user_id: str, query: str) -> Dict[str, Any]:
        """WhatsApp tool wrapper for `MemoryEngine.recall(user_id, query)`."""
        if self.kernel and getattr(self.kernel, "memory_engine", None):
            return self.kernel.memory_engine.recall(user_id, query)
        return {"status": "unavailable", "memories": [], "entities": [], "summary": ""}

    def create_reminder(self, user_id: str, text: str, due_at: Any, recurrence: Optional[str] = None) -> Dict[str, Any]:
        """WhatsApp tool wrapper for `MemoryEngine.create_reminder(...)`."""
        if self.kernel and getattr(self.kernel, "memory_engine", None):
            return self.kernel.memory_engine.create_reminder(
                user_id=user_id,
                text=text,
                due_at=due_at,
                recurrence=recurrence,
                channel="whatsapp",
            )
        return {"status": "error", "error": "memory_unavailable"}

    async def _handle_general_query(self, user_id: str, query: str) -> str:
        """Processes conversational or device queries through the AIVA runtime."""
        if not self.kernel:
            return "AIVA kernel is currently offline."

        # Set active user id in kernel for per-user isolation
        self.kernel.active_user_id = user_id

        try:
            # Check if input_handler is available
            if hasattr(self.kernel, "input_handler") and self.kernel.input_handler:
                res = await self.kernel.input_handler.process_query(
                    query,
                    speak=False,
                    user_id=user_id,
                    channel="whatsapp",
                )
                if isinstance(res, dict):
                    return res.get("response") or res.get("message") or "Done."
                return str(res)
            elif hasattr(self.kernel, "conversation_service") and self.kernel.conversation_service:
                mem_engine = getattr(self.kernel, "memory_engine", None)
                raw_name = (
                    mem_engine.get_user_display_name(user_id)
                    if mem_engine and user_id and hasattr(mem_engine, "get_user_display_name")
                    else None
                )
                display_name = (
                    raw_name.strip()
                    if isinstance(raw_name, str) and raw_name.strip()
                    else None
                )
                mem_ctx = (
                    mem_engine.build_context(
                        user_id,
                        query,
                        channel="whatsapp",
                        authoritative_name=display_name,
                    )
                    if mem_engine and user_id
                    else ""
                )
                return await self.kernel.conversation_service.respond(
                    query,
                    [],
                    mem_ctx,
                    user_name=display_name,
                    user_id=user_id,
                    channel="whatsapp",
                )
            return "Received your request."
        except Exception as e:
            logger.error(f"[WhatsApp] General query processing error: {e}")
            return "I encountered an issue processing that request. Please try again."
