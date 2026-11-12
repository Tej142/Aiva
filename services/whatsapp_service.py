"""
WhatsApp Channel Service — Multi-user Identity, Access Challenge, Session Control,
Contact-only Sending, Admin Controls, and Guarded Inbox Reading.
"""

from __future__ import annotations

import asyncio
import base64
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
import random
import re
import secrets
import tempfile
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote
import uuid

from core.shared.logger import get_logger
from services.whatsapp_config import (
    WhatsAppConfig,
    get_wa_config,
    is_admin_email,
    is_admin_user,
    mask_phone,
    match_start_trigger,
    match_stop_trigger,
    normalize_phone,
    validate_preferred_display_name,
    verify_access_credentials,
)
from services.whatsapp_notifier import WhatsAppInboundNotifier

logger = get_logger("WhatsAppService")

# Backwards-compatible module constants (runtime config always hot-reloads via get_wa_config())
_init_cfg = get_wa_config()
WHATSAPP_ENABLED = _init_cfg.whatsapp_enabled
AIVA_WHATSAPP_NUMBER = _init_cfg.aiva_whatsapp_number
WHATSAPP_BRIDGE_PORT = _init_cfg.bridge_port
WHATSAPP_BRIDGE_SECRET = _init_cfg.bridge_secret
WHATSAPP_BACKEND_URL = _init_cfg.backend_url
WHATSAPP_REPLY_DELAY_MS = _init_cfg.reply_delay_ms
WHATSAPP_MAX_OUTBOUND_PER_HOUR = _init_cfg.max_outbound_per_hour
WHATSAPP_MAX_MESSAGE_CHARS = _init_cfg.max_message_chars
WHATSAPP_ALLOW_POWER_CONTROL = _init_cfg.allow_power_control

_ADMIN_NOTICE_BATCH_INTERVAL_SEC = 900.0  # Max 1 batched admin notice every 15 minutes
_CHALLENGE_START_COOLDOWN_SEC = 60.0      # 60s cooldown per phone between challenge starts


def validate_config() -> None:
    """Validate WhatsApp configuration when enabled (no WHATSAPP_OWNER_EMAIL required)."""
    cfg = get_wa_config()
    if not cfg.whatsapp_enabled:
        return
    required = {
        "AIVA_WHATSAPP_NUMBER": cfg.aiva_whatsapp_number,
        "AIVA_ADMIN_EMAIL": cfg.admin_email,
        "WHATSAPP_BRIDGE_SECRET": cfg.bridge_secret,
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        raise ValueError(
            f"[WhatsAppConfigError] WHATSAPP_ENABLED is true but required settings are missing: {', '.join(missing)}"
        )


def verify_bridge_secret(provided_secret: str) -> bool:
    """Constant-time comparison for the bridge shared secret."""
    cfg = get_wa_config()
    secret = (cfg.bridge_secret or WHATSAPP_BRIDGE_SECRET or "").strip()
    if not secret or not provided_secret:
        return False
    return hmac.compare_digest(str(provided_secret).strip(), secret)


def format_for_whatsapp(text: str, max_chars: Optional[int] = None) -> str:
    """Format markdown text cleanly for WhatsApp while keeping URLs intact."""
    if not text:
        return ""
    limit = int(max_chars if max_chars is not None else get_wa_config().max_message_chars)
    s = text.strip()

    urls: List[str] = []

    def _url_repl(match):
        urls.append(match.group(0))
        return f"__WA_URL_TOKEN_{len(urls) - 1}__"

    s = re.compile(r"https?://[^\s]+", re.IGNORECASE).sub(_url_repl, s)
    s = re.sub(r"<[^>]+>", "", s)
    s = re.sub(r"^#{1,6}\s*(.+)$", r"**\1**", s, flags=re.MULTILINE)
    s = re.sub(r"(?<!\*)\*(?!\*)([^\*\n]+?)(?<!\*)\*(?!\*)", r"_\1_", s)
    s = re.sub(r"\*\*([^\*]+?)\*\*", r"*\1*", s)
    s = re.sub(r"~~([^~]+)~~", r"~\1~", s)

    for idx, u in enumerate(urls):
        s = s.replace(f"__WA_URL_TOKEN_{idx}__", u)

    s = re.sub(r"\n{3,}", "\n\n", s)

    if len(s) > limit:
        candidate = s[:limit]
        split_idx = -1
        for punct in ["\n\n", "\n", ". ", "! ", "? ", "; "]:
            last_pos = candidate.rfind(punct)
            if last_pos > limit - 200:
                split_idx = last_pos + len(punct.rstrip())
                break
        if split_idx > 0:
            s = candidate[:split_idx].rstrip() + "..."
        else:
            s = candidate.rsplit(" ", 1)[0].rstrip() + "..."

    return s.strip()


def _sanitize_xml_attr(value: str) -> str:
    return (
        str(value or "")
        .replace("&", "&amp;")
        .replace('"', "'")
        .replace("<", "")
        .replace(">", "")
        .strip()
    )


def _fence_inbox_item(sender_name: str, received_at_iso: str, kind: str, content: str) -> str:
    from core.agent.policy import sanitize_untrusted_text

    safe_sender = _sanitize_xml_attr(sender_name)
    safe_ts = _sanitize_xml_attr(received_at_iso)
    safe_kind = _sanitize_xml_attr(kind)
    safe_body = sanitize_untrusted_text(content or "", max_chars=2500)
    return (
        f'<whatsapp_message untrusted="true" sender="{safe_sender}" '
        f'received_at="{safe_ts}" kind="{safe_kind}">\n'
        f"{safe_body}\n"
        f"</whatsapp_message>"
    )


class WhatsAppService:
    """Central multi-user WhatsApp service coordinating access, sessions, inbox, and admin controls."""

    def __init__(
        self,
        db: Any,
        kernel: Any = None,
        hud_deliver: Optional[Callable[[str, dict], bool]] = None,
        session_invalidator: Optional[Callable[[str, str], None]] = None,
    ):
        self.db = db
        self.kernel = kernel
        self.hud_deliver = hud_deliver
        self.session_invalidator = session_invalidator

        self._outbound_timestamps: Dict[str, List[float]] = defaultdict(list)
        self._processed_message_ids: Dict[str, float] = {}
        self._pending_power_actions: Dict[str, Dict[str, Any]] = {}
        # Ephemeral RAM-only username store between AWAIT_USERNAME -> AWAIT_PASSWORD; never logged or persisted to SQLite
        self._ephemeral_usernames: Dict[str, Tuple[str, float]] = {}
        self._pending_approval_notice_ts: Dict[str, float] = {}
        self._last_admin_notice_flush: float = 0.0

        self.notifier = WhatsAppInboundNotifier(
            db=self.db,
            send_raw_fn=self._notifier_send_raw,
            rate_check_fn=self.check_outbound_rate_limit,
        )

    async def _notifier_send_raw(
        self,
        to_phone: str,
        text: str,
        user_id: Optional[str],
        to_jid: Optional[str],
    ) -> Dict[str, Any]:
        return await self.send_whatsapp_message(to_phone, text, user_id=user_id, to_jid=to_jid)

    # ─────────────────────────────────────────────────────────────
    # RATE LIMITING & DEDUPLICATION
    # ─────────────────────────────────────────────────────────────
    def check_outbound_rate_limit(self, user_id: str) -> bool:
        cfg = get_wa_config()
        now = time.time()
        ts_list = [ts for ts in self._outbound_timestamps[user_id] if now - ts < 3600.0]
        self._outbound_timestamps[user_id] = ts_list
        return len(ts_list) < max(1, int(cfg.max_outbound_per_hour))

    def record_outbound_send(self, user_id: str) -> None:
        self._outbound_timestamps[user_id].append(time.time())

    def is_message_duplicate(self, message_id: str) -> bool:
        if not message_id:
            return False
        now = time.time()
        self._processed_message_ids = {
            m_id: ts for m_id, ts in self._processed_message_ids.items() if now - ts < 600.0
        }
        if message_id in self._processed_message_ids:
            return True
        self._processed_message_ids[message_id] = now
        return False

    # ─────────────────────────────────────────────────────────────
    # ADMIN & BRIDGE STATUS / PAIRING (ADMIN ONLY)
    # ─────────────────────────────────────────────────────────────
    def is_bridge_owner(self, user: Optional[Dict[str, Any]]) -> bool:
        """Strictly matches configured AIVA_ADMIN_EMAIL."""
        return is_admin_user(user, cfg=get_wa_config())

    async def _bridge_request(
        self,
        method: str,
        path: str,
        payload: Optional[Dict[str, Any]] = None,
        timeout: float = 5.0,
    ) -> Tuple[int, Dict[str, Any]]:
        import urllib.error
        import urllib.request

        cfg = get_wa_config()
        url = f"http://127.0.0.1:{cfg.bridge_port}{path}"
        headers = {"X-Bridge-Secret": cfg.bridge_secret}
        data_bytes = None
        if payload is not None:
            data_bytes = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=data_bytes, headers=headers, method=method.upper())
        loop = asyncio.get_running_loop()

        def _do_req():
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    raw = resp.read().decode("utf-8")
                    return resp.status, (json.loads(raw) if raw else {})
            except urllib.error.HTTPError as e:
                raw = e.read().decode("utf-8", errors="ignore")
                try:
                    return e.code, json.loads(raw)
                except Exception:
                    return e.code, {"error": raw or str(e)}
            except Exception as e:
                return 503, {"error": "bridge_offline", "message": str(e)}

        return await loop.run_in_executor(None, _do_req)

    async def get_bridge_status(self) -> Dict[str, Any]:
        cfg = get_wa_config()
        code, body = await self._bridge_request("GET", "/status", timeout=2.5)
        if code >= 500 or not isinstance(body, dict):
            return {
                "connected": False,
                "status": "bridge_offline",
                "bridge_state": "bridge_offline",
                "phone_masked": "",
                "configured_phone_masked": mask_phone(cfg.aiva_whatsapp_number) if cfg.aiva_whatsapp_number else "",
                "aiva_number": cfg.aiva_whatsapp_number,
                "mismatch": False,
                "error": body.get("error", "Bridge offline") if isinstance(body, dict) else "Bridge offline",
                "pairing": {"active": False, "phase": "idle"},
            }

        raw_phone = normalize_phone(body.get("phone", "") or "")
        mismatch = bool(cfg.aiva_whatsapp_number and raw_phone and raw_phone != cfg.aiva_whatsapp_number)
        return {
            "connected": bool(body.get("connected", False)) and not mismatch,
            "status": "number_mismatch" if mismatch else body.get("status", "disconnected"),
            "bridge_state": "number_mismatch" if mismatch else body.get("bridge_state", body.get("status", "disconnected")),
            "phone_masked": mask_phone(raw_phone) if raw_phone else "",
            "configured_phone_masked": mask_phone(cfg.aiva_whatsapp_number) if cfg.aiva_whatsapp_number else "",
            "aiva_number": cfg.aiva_whatsapp_number or raw_phone,
            "mismatch": mismatch,
            "error": "Paired number does not match AIVA_WHATSAPP_NUMBER" if mismatch else body.get("error"),
            "pairing": body.get("pairing") or {"active": False, "phase": "idle"},
        }

    async def get_user_status(self, user_id: str, user: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        cfg = get_wa_config()
        if user is None and self.db and hasattr(self.db, "get_user_by_id"):
            user = self.db.get_user_by_id(user_id)
        is_owner = self.is_bridge_owner(user)
        bridge_info = await self.get_bridge_status()

        # Non-admins never see QR payloads or admin pairing secrets
        if not is_owner:
            bridge_info = {
                "connected": bridge_info.get("connected", False),
                "status": bridge_info.get("status", "disconnected"),
                "aiva_number": bridge_info.get("aiva_number", cfg.aiva_whatsapp_number),
                "phone_masked": bridge_info.get("phone_masked", ""),
                "configured_phone_masked": bridge_info.get("configured_phone_masked", ""),
                "pairing": {"active": False, "phase": "idle"},
            }

        links = self.db.get_whatsapp_links_for_user(user_id)
        masked_links = []
        active_session = None
        for l in links:
            item = dict(l)
            item["phone_masked"] = mask_phone(item.get("phone_e164", ""))
            item.pop("phone_e164", None)
            masked_links.append(item)
            if item.get("status") == "verified":
                active_session = {
                    "session_state": item.get("session_state", "INACTIVE"),
                    "last_activity_at": item.get("last_activity_at", 0),
                    "phone_masked": item["phone_masked"],
                    "verified_by_epoch": item.get("verified_by_epoch", 0),
                }

        contacts = self.db.get_whatsapp_contacts(user_id)
        inbox_summary = self.db.get_whatsapp_inbox_unread_summary(user_id)
        pending_challenge = self.db.get_challenge_by_user(user_id)
        return {
            "whatsapp_enabled": cfg.whatsapp_enabled,
            "is_owner": is_owner,
            "is_admin": is_owner,
            "verify_mode": cfg.verify_mode,
            "gate_state": (user or {}).get("gate_state", "active"),
            "user_can_send_whatsapp": is_owner or cfg.user_can_send_whatsapp,
            "daily_message_cap": cfg.user_daily_message_cap,
            "daily_msg_count": int((user or {}).get("daily_msg_count") or 0),
            "inbox_retention_days": cfg.inbox_retention_days,
            "bridge": bridge_info,
            "links": masked_links,
            "session": active_session or {"session_state": "UNLINKED", "last_activity_at": 0},
            "contacts_count": len(contacts),
            "inbox_summary": inbox_summary,
            "pending_code": (pending_challenge or {}).get("verify_code") if pending_challenge else None,
        }

    async def start_bridge_pairing(
        self,
        user: Dict[str, Any],
        mode: str = "qr",
        raw_phone: str = "",
    ) -> Tuple[int, Dict[str, Any]]:
        if not self.is_bridge_owner(user):
            return 403, {
                "status": "error",
                "message": "Only the AIVA admin can pair or manage the central WhatsApp bridge.",
            }

        clean_mode = (mode or "qr").strip().lower()
        if clean_mode not in ("qr", "code"):
            return 400, {"status": "error", "message": "Invalid pairing mode. Use 'qr' or 'code'."}

        digits = normalize_phone(raw_phone)
        if clean_mode == "code" and (not digits or len(digits) < 8 or len(digits) > 15):
            return 400, {
                "status": "error",
                "message": "Enter a valid phone number including country code (8-15 digits).",
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
                "message": resp.get("message") or resp.get("error") or "Failed to start WhatsApp pairing.",
            }
        return 200, {"status": "success", "data": {"pairing": resp.get("pairing") or {}}}

    async def cancel_bridge_pairing(self, user: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        if not self.is_bridge_owner(user):
            return 403, {"status": "error", "message": "Only the AIVA admin can cancel bridge pairing."}
        code, resp = await self._bridge_request("POST", "/pair/cancel", timeout=5.0)
        if code >= 400:
            return code, {"status": "error", "message": resp.get("message") or "Failed to cancel pairing."}
        return 200, {"status": "success", "data": {"pairing": resp.get("pairing") or {}}}

    async def logout_bridge(self, user: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        if not self.is_bridge_owner(user):
            return 403, {"status": "error", "message": "Only the AIVA admin can unpair the WhatsApp bridge."}
        code, resp = await self._bridge_request("POST", "/logout", timeout=8.0)
        if code >= 400:
            return code, {"status": "error", "message": resp.get("message") or "Failed to unpair bridge."}
        return 200, {"status": "success", "data": resp}

    # ─────────────────────────────────────────────────────────────
    # ENTRY B VERIFICATION START & UNLINK
    # ─────────────────────────────────────────────────────────────
    def start_link(self, user_id: str, raw_phone: str) -> Dict[str, Any]:
        """Generate an Entry B verification code (e.g. AIVA-483920) + wa.me deep link."""
        cfg = get_wa_config()
        phone = normalize_phone(raw_phone)
        if not phone or len(phone) < 7 or len(phone) > 15:
            raise ValueError("Invalid personal phone number. Include country code (e.g. 919876543210).")
        if cfg.aiva_whatsapp_number and phone == cfg.aiva_whatsapp_number:
            raise ValueError("Cannot link AIVA's own service number as your personal number.")
        if self.db.is_number_blocked(phone):
            raise ValueError("This phone number is blocked.")

        existing = self.db.get_whatsapp_link_by_phone(phone)
        if existing and existing.get("user_id") != user_id:
            raise ValueError("This phone number is already linked to another AIVA account.")

        existing_ch = self.db.get_challenge_by_phone(phone)
        if existing_ch and float(existing_ch.get("locked_until") or 0) > time.time():
            raise ValueError("Verification for this phone number is temporarily locked.")

        now = time.time()
        outbound_sent = False
        if cfg.verify_mode == "outbound":
            allowed = self.db.count_and_record_verify_send(user_id, cfg.max_verify_sends_per_day, now=now)
            if not allowed:
                raise ValueError("Daily verification send limit reached. Please try again tomorrow.")

        code = f"AIVA-{secrets.randbelow(900000) + 100000}"
        expires_at = now + 600.0  # 10 minutes
        self.db.create_or_update_whatsapp_link(user_id, phone, code, expires_at)
        self.db.upsert_challenge({
            "id": (existing_ch or {}).get("id") or f"wch_{uuid.uuid4().hex[:12]}",
            "phone_e164": phone,
            "lid_jid": (existing_ch or {}).get("lid_jid") or "",
            "user_id": user_id,
            "entry_mode": "B",
            "verify_code": code,
            "code_expires_at": expires_at,
            "state": "AWAIT_CODE",
            "attempts": 0,
            "last_started_at": now,
            "expires_at": expires_at,
            "updated_at": now,
            "created_at": (existing_ch or {}).get("created_at") or now,
        })

        # Pre-resolve personal number -> WhatsApp @lid via bridge USync (best-effort)
        self._schedule_coro(self.resolve_phone_lid(phone))

        user = self.db.get_user_by_id(user_id)
        if user and user.get("gate_state") not in ("blocked", "revoked", "active"):
            self.db.set_user_gate_state(user_id, "number_pending")

        aiva_num = cfg.aiva_whatsapp_number
        wa_me_url = f"https://wa.me/{aiva_num}?text={quote(code)}" if aiva_num else ""

        if cfg.verify_mode == "outbound":
            # Schedule outbound verification send asynchronously when loop exists
            msg_text = f"Your AIVA verification code is {code}. Enter it in the HUD or reply here to verify."
            self._schedule_coro(self.send_whatsapp_message(phone, msg_text, user_id=None))
            outbound_sent = True

        logger.info(f"[WhatsApp] stage=link_start user={user_id} phone={mask_phone(phone)} mode={cfg.verify_mode} status=pending")
        return {
            "phone_masked": mask_phone(phone),
            "link_code": code,
            "expires_at": expires_at,
            "verify_mode": cfg.verify_mode,
            "aiva_number": aiva_num,
            "wa_me_url": wa_me_url,
            "outbound_sent": outbound_sent,
            "instructions": (
                f"Send '{code}' on WhatsApp to AIVA's number from your personal number."
                if cfg.verify_mode == "inbound"
                else f"A verification code was sent to {mask_phone(phone)}. Enter it or reply on WhatsApp."
            ),
        }

    def verify_outbound_hud_code(self, user_id: str, raw_code: str) -> Tuple[bool, str, Dict[str, Any]]:
        """Handle HUD-entered code when WHATSAPP_VERIFY_MODE=outbound."""
        cfg = get_wa_config()
        clean_code = (raw_code or "").strip().upper()
        if clean_code.isdigit() and len(clean_code) == 6:
            clean_code = f"AIVA-{clean_code}"
        ch = self.db.get_challenge_by_code(clean_code, now=time.time())
        if not ch or ch.get("user_id") != user_id:
            return False, "Invalid or expired verification code.", {}

        phone = ch["phone_e164"]
        user = self.db.get_user_by_id(user_id)
        if not user:
            return False, "User not found.", {}

        if is_admin_user(user, cfg):
            self.db.upsert_verified_whatsapp_link(
                user_id, phone, verified_by_epoch=cfg.access_epoch, session_state="INACTIVE"
            )
            self.db.set_user_gate_state(user_id, "active", access_epoch=cfg.access_epoch)
            self._clear_challenge(ch["id"], phone)
            self._emit_hud_unlock(user_id, "active")
            return True, "Owner WhatsApp verified.", {"gate_state": "active"}

        now = time.time()
        self.db.set_user_gate_state(user_id, "credential_pending")
        self.db.upsert_challenge({
            **ch,
            "state": "AWAIT_USERNAME",
            "expires_at": now + cfg.access_challenge_ttl_minutes * 60.0,
            "attempts": 0,
        })
        self._schedule_coro(
            self.send_whatsapp_message(phone, "Code verified. Enter your username:", user_id=None)
        )
        return True, "Code verified. Check WhatsApp and enter your access credentials.", {"gate_state": "credential_pending"}

    def unlink_number(self, user_id: str, raw_phone: str) -> bool:
        phone = normalize_phone(raw_phone)
        if not phone:
            link = self.db.get_verified_link_for_user(user_id)
            phone = (link or {}).get("phone_e164", "")
        res = self.db.unlink_whatsapp(user_id, phone) if phone else False
        logger.info(f"[WhatsApp] stage=unlink user={user_id} phone={mask_phone(phone)} status={'ok' if res else 'noop'}")
        return res

    def _schedule_coro(self, coro) -> None:
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(coro)
            return
        except RuntimeError:
            pass
        k_loop = getattr(self.kernel, "loop", None)
        if k_loop and getattr(k_loop, "is_running", lambda: False)():
            asyncio.run_coroutine_threadsafe(coro, k_loop)
            return
        try:
            coro.close()
        except Exception:
            pass

    async def resolve_phone_lid(self, phone_e164: str) -> str:
        """Best-effort bridge USync lookup to pre-cache phone -> WhatsApp @lid mapping."""
        phone = normalize_phone(phone_e164)
        if not phone:
            return ""
        try:
            code, body = await self._bridge_request(
                "POST",
                "/resolve-lid",
                payload={"phones": [phone]},
                timeout=3.5,
            )
            if code != 200 or not isinstance(body, dict):
                return ""
            for m in (body.get("mappings") or []):
                m_phone = normalize_phone(m.get("phone") or m.get("jid", "").split("@")[0])
                m_lid = str(m.get("lid") or "").strip()
                if m_phone == phone and m_lid.endswith("@lid"):
                    self.db.bind_challenge_lid(phone, m_lid)
                    self.db.bind_whatsapp_link_lid(phone, lid_jid=m_lid)
                    logger.info(f"[WhatsApp] stage=resolve_lid phone={mask_phone(phone)} status=bound")
                    return m_lid
        except Exception as e:
            logger.debug(f"[WhatsApp] stage=resolve_lid status=skip err={type(e).__name__}")
        return ""

    def _emit_hud_unlock(self, user_id: str, gate_state: str) -> None:
        try:
            import aiva_server
            aiva_server.invalidate_user_gate_cache(user_id)
        except Exception:
            pass
        deliver = self.hud_deliver
        if not deliver:
            try:
                import aiva_server
                deliver = aiva_server._deliver_to_hud
            except Exception:
                deliver = None
        if deliver:
            try:
                deliver(user_id, {
                    "type": "verification_unlocked" if gate_state == "active" else "gate_state_updated",
                    "gate_state": gate_state,
                })
            except Exception:
                pass

    def _clear_challenge(self, challenge_id: str, phone: str) -> None:
        self._ephemeral_usernames.pop(phone, None)
        self.db.upsert_challenge({
            "id": challenge_id,
            "phone_e164": phone,
            "state": "IDLE",
            "verify_code": "",
            "code_expires_at": 0,
            "username_hmac": "",
            "attempts": 0,
            "expires_at": 0,
        })

    # ─────────────────────────────────────────────────────────────
    # OUTBOUND SEND TO NODE BRIDGE
    # ─────────────────────────────────────────────────────────────
    async def send_whatsapp_message(
        self,
        to_phone: str,
        message: str,
        user_id: Optional[str] = None,
        to_jid: Optional[str] = None,
        *,
        raw_verbatim: bool = False,
    ) -> Dict[str, Any]:
        """Send an outbound WhatsApp message through AIVA's Baileys bridge."""
        cfg = get_wa_config()
        phone = normalize_phone(to_phone)
        if not phone:
            return {"status": "error", "error": "invalid_phone"}
        if cfg.aiva_whatsapp_number and phone == cfg.aiva_whatsapp_number:
            return {"status": "error", "error": "cannot_message_own_aiva_number"}

        if user_id and not self.check_outbound_rate_limit(user_id):
            logger.warning(f"[WhatsApp] stage=send user={user_id} status=rate_limited")
            return {"status": "error", "error": "rate_limit_exceeded"}

        out_text = str(message or "").strip() if raw_verbatim else format_for_whatsapp(message, max_chars=cfg.max_message_chars)
        if not out_text:
            return {"status": "error", "error": "empty_message"}

        delay_sec = max(0.0, cfg.reply_delay_ms / 1000.0) + random.uniform(0.02, 0.15)
        if delay_sec > 0:
            await asyncio.sleep(delay_sec)

        import urllib.request

        target_jid = to_jid if (to_jid and "@" in to_jid) else f"{phone}@s.whatsapp.net"
        payload = json.dumps({"to": phone, "to_jid": target_jid, "text": out_text}).encode("utf-8")
        url = f"http://127.0.0.1:{cfg.bridge_port}/send"
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json", "X-Bridge-Secret": cfg.bridge_secret},
            method="POST",
        )

        t0 = time.time()
        try:
            loop = asyncio.get_running_loop()

            def _post():
                with urllib.request.urlopen(req, timeout=10.0) as resp:
                    return json.loads(resp.read().decode("utf-8"))

            bridge_resp = await loop.run_in_executor(None, _post)
            duration_ms = (time.time() - t0) * 1000.0

            if user_id:
                self.record_outbound_send(user_id)
                self.db.log_whatsapp_event(user_id, "outbound", "send", "ok", duration_ms)

            logger.info(f"[WhatsApp] stage=send to={mask_phone(phone)} duration_ms={duration_ms:.1f} status=ok")
            return {"status": "success", "bridge_response": bridge_resp}
        except Exception as e:
            duration_ms = (time.time() - t0) * 1000.0
            if user_id:
                self.db.log_whatsapp_event(user_id, "outbound", "send", "error", duration_ms)
            logger.error(f"[WhatsApp] stage=send to={mask_phone(phone)} duration_ms={duration_ms:.1f} status=error err={type(e).__name__}")
            return {"status": "error", "error": str(e)}

    # ─────────────────────────────────────────────────────────────
    # CONTACT RESOLUTION, PREFERENCES & CONTACT-ONLY SENDING
    # ─────────────────────────────────────────────────────────────
    def resolve_contact(self, user_id: str, query: str) -> Dict[str, Any]:
        matches = self.db.resolve_whatsapp_contact(user_id, query)
        if not matches:
            return {
                "status": "not_found",
                "message": f"I couldn't find '{query}' in your contacts. Please add them in the WhatsApp tab or send: save <number> as {query}.",
            }
        if len(matches) == 1:
            return {"status": "found", "contact": matches[0]}

        names = [f"'{c['display_name']}' ({mask_phone(c['phone_e164'])})" for c in matches]
        return {
            "status": "ambiguous",
            "matches": matches,
            "message": f"Found multiple contacts matching '{query}': {', '.join(names)}. Which one did you mean?",
        }

    def update_contact_preference(
        self,
        user_id: str,
        contact_query: str,
        *,
        notify_inbound: Optional[bool] = None,
        priority: Optional[bool] = None,
    ) -> Dict[str, Any]:
        res = self.resolve_contact(user_id, contact_query)
        if res["status"] != "found":
            return res
        c = res["contact"]
        updated = self.db.update_whatsapp_contact(
            user_id,
            c["id"],
            notify_inbound=notify_inbound,
            priority=priority,
        )
        return {"status": "updated", "contact": updated or c}

    async def send_to_contact(self, user_id: str, target_query: str, message_text: str) -> Dict[str, Any]:
        """Send verbatim text to a saved contact from AIVA's account; confirm back to user."""
        cfg = get_wa_config()
        user = self.db.get_user_by_id(user_id)
        if not user or not user.get("is_active", 1) or user.get("gate_state", "active") != "active":
            return {"status": "blocked", "message": "Account verification required before sending messages."}

        if not is_admin_user(user, cfg) and not cfg.user_can_send_whatsapp:
            return {"status": "blocked", "message": "Sending WhatsApp messages to third-party contacts is restricted to admin."}

        resolution = self.resolve_contact(user_id, target_query)
        if resolution["status"] != "found":
            return resolution

        contact = resolution["contact"]
        out_msg = str(message_text or "").strip()
        if not out_msg:
            return {"status": "error", "message": "Message text cannot be empty."}

        try:
            send_res = await self.send_whatsapp_message(
                contact["phone_e164"],
                out_msg,
                user_id=user_id,
                raw_verbatim=True,  # Send exact message text without "Sent via AIVA" signature
            )
        except TypeError:
            send_res = await self.send_whatsapp_message(
                contact["phone_e164"],
                out_msg,
                user_id=user_id,
            )
        if send_res.get("status") != "success":
            return {
                "status": "error",
                "contact": contact["display_name"],
                "message": f"Failed to send message to {contact['display_name']} ({send_res.get('error', 'send_failed')}).",
            }
        confirm_line = f"Sent to {contact['display_name']}: {out_msg}"
        return {
            "status": "success",
            "contact": contact["display_name"],
            "sent_text": out_msg,
            "message": confirm_line,
        }

    # ─────────────────────────────────────────────────────────────
    # INBOX STATUS, SMART READ, LAZY VOICE TRANSCRIPTION & READ RECEIPTS
    # ─────────────────────────────────────────────────────────────
    def get_inbox_status(
        self,
        user_id: str,
        *,
        sender: Optional[str] = None,
        turn_origin: str = "user_explicit",
    ) -> Dict[str, Any]:
        """Metadata-only unread counts grouped by sender. Enforces explicit turn_origin."""
        if turn_origin != "user_explicit":
            return {"status": "blocked", "error": "policy_denied_turn_origin", "message": "Inbox queries require an explicit user turn."}
        summary = self.db.get_whatsapp_inbox_unread_summary(user_id, sender_filter=sender)
        return {"status": "success", **summary}

    async def _lazy_fetch_and_transcribe_voice(
        self,
        user_id: str,
        row: Dict[str, Any],
    ) -> str:
        """Download voice note on explicit read, transcribe via Whisper, and wipe temp data."""
        existing = (row.get("body") or "").strip()
        if existing:
            return existing
        msg_id = (row.get("message_id") or "").strip()
        if not msg_id:
            return "[Voice note unavailable]"

        code, body = await self._bridge_request(
            "POST",
            "/media/download",
            payload={"message_id": msg_id},
            timeout=15.0,
        )
        if code != 200 or not isinstance(body, dict) or not body.get("base64"):
            return "[Voice note could not be downloaded from bridge]"

        tmp_path = None
        transcript = ""
        try:
            audio_bytes = base64.b64decode(body["base64"])
            # Create ephemeral temp file and erase immediately in finally
            fd, tmp_path = tempfile.mkstemp(prefix="aiva_va_", suffix=".ogg")
            with os.fdopen(fd, "wb") as f:
                f.write(audio_bytes)
            if self.kernel and hasattr(self.kernel, "voice_service"):
                vs = self.kernel.voice_service
                if hasattr(vs, "transcribe_bytes"):
                    transcript = (await vs.transcribe_bytes(audio_bytes, filename="voice.ogg")) or ""
            transcript = transcript.strip()
            if transcript:
                self.db.update_whatsapp_inbox_transcript(user_id, row["id"], transcript)
        except Exception as e:
            logger.warning(f"[WhatsApp] stage=voice_transcribe err={type(e).__name__}")
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
        return transcript or "[Voice note received - transcription unavailable]"

    async def read_inbox_messages(
        self,
        user_id: str,
        *,
        contact: Optional[str] = None,
        unread_only: bool = True,
        hours_back: Optional[int] = None,
        limit: int = 15,
        include_voice_transcript: bool = True,
        mark_read: bool = True,
        turn_origin: str = "user_explicit",
    ) -> Dict[str, Any]:
        """
        Read user's WhatsApp inbox with strict turn_origin enforcement, lazy Whisper
        transcription on voice notes, XML untrusted fencing, smart formatting,
        and post-read mark_read + read-receipt dispatch.
        """
        if turn_origin != "user_explicit":
            return {
                "status": "blocked",
                "error": "policy_denied_turn_origin",
                "message": "Reading third-party WhatsApp messages is allowed only on an explicit user request.",
            }

        cfg = get_wa_config()
        contact_id = None
        sender_e164 = None
        if contact:
            res = self.resolve_contact(user_id, contact)
            if res["status"] == "ambiguous":
                return {
                    "status": "ambiguous",
                    "matches": [{"display_name": m["display_name"]} for m in res.get("matches", [])],
                    "messages": [],
                    "count": 0,
                    "message": res["message"],
                }
            if res["status"] == "found":
                contact_id = res["contact"]["id"]
                sender_e164 = res["contact"]["phone_e164"]
            else:
                digits = normalize_phone(contact)
                if digits and len(digits) >= 4:
                    sender_e164 = digits
                else:
                    return {
                        "status": "empty",
                        "count": 0,
                        "messages": [],
                        "message": f"No saved contact named '{contact}'.",
                    }

        since_ts = (time.time() - int(hours_back) * 3600.0) if hours_back else None
        rows = self.db.read_whatsapp_inbox_messages(
            user_id,
            sender_e164=sender_e164,
            contact_id=contact_id,
            since=since_ts,
            unread_only=unread_only,
            limit=max(1, min(int(limit), 50)),
        )
        if not rows:
            empty_msg = f"No {'unread ' if unread_only else ''}WhatsApp messages" + (f" from {contact}." if contact else ".")
            return {"status": "empty", "count": 0, "messages": [], "fenced_xml": "", "message": empty_msg}

        formatted_msgs: List[Dict[str, Any]] = []
        fenced_blocks: List[str] = []
        receipt_batches: Dict[str, List[str]] = defaultdict(list)
        unread_ids: List[str] = []
        latest_sender_name = ""

        for r in rows:
            kind = str(r.get("kind") or "text")
            content = str(r.get("body") or "").strip()
            if kind == "voice" and include_voice_transcript and not content:
                content = await self._lazy_fetch_and_transcribe_voice(user_id, r)
            elif kind in ("image", "document") and not content:
                fn = r.get("filename") or kind
                content = f"[{kind.title()}: {fn}]"

            ts = float(r.get("received_at") or time.time())
            ts_iso = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            sname = str(r.get("sender_name") or "").strip() or mask_phone(r.get("sender_e164") or "")
            latest_sender_name = sname
            fenced = _fence_inbox_item(sname, ts_iso, kind, content)
            fenced_blocks.append(fenced)

            formatted_msgs.append({
                "id": r["id"],
                "sender_name": sname,
                "phone_masked": mask_phone(r.get("sender_e164") or ""),
                "kind": kind,
                "text": content,
                "filename": r.get("filename") or "",
                "received_at": ts,
                "received_at_iso": ts_iso,
                "status": r.get("status", "unread"),
            })
            if r.get("status") == "unread":
                unread_ids.append(r["id"])
                try:
                    meta = json.loads(r.get("media_ref") or "{}")
                    jid = meta.get("jid") or f"{r.get('sender_e164')}@s.whatsapp.net"
                except Exception:
                    jid = f"{r.get('sender_e164')}@s.whatsapp.net"
                if r.get("message_id"):
                    receipt_batches[jid].append(r["message_id"])

        # Track last_read_sender for convenient follow-up drafting
        if latest_sender_name:
            self.db.set_whatsapp_last_read_sender(user_id, latest_sender_name)

        # Smart condensed human report (never dumping huge raw logs inline)
        grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for m in formatted_msgs:
            grouped[m["sender_name"]].append(m)

        report_lines: List[str] = []
        for sname, items in grouped.items():
            if len(items) == 1:
                report_lines.append(f"• *{sname}*: {items[0]['text'][:240]}")
            else:
                snippets = [it["text"][:120] for it in items[-3:]]
                report_lines.append(f"• *{sname}* ({len(items)} msgs): " + " | ".join(snippets))

        # ONLY mark read & send read receipt AFTER read formatting succeeded cleanly
        if mark_read and unread_ids:
            self.db.mark_whatsapp_inbox_read(user_id, unread_ids, status="read")
            if cfg.mark_read_on_read and receipt_batches:
                for jid, ids in receipt_batches.items():
                    await self._bridge_request("POST", "/read", payload={"jid": jid, "message_ids": ids}, timeout=3.0)

        return {
            "status": "success",
            "count": len(formatted_msgs),
            "messages": formatted_msgs,
            "fenced_xml": "\n".join(fenced_blocks),
            "last_read_sender": latest_sender_name,
            "report": "\n".join(report_lines),
            "message": "\n".join(report_lines),
        }

    def _resolve_filter_target(self, user_id: str, contact: Optional[str]):
        if not contact:
            return None, None, None
        res = self.resolve_contact(user_id, contact)
        if res.get("status") == "ambiguous":
            return None, None, {
                "status": "ambiguous",
                "matches": [{"display_name": m.get("display_name", "")} for m in (res.get("matches") or [])],
                "messages": [],
                "count": 0,
                "message": res.get("message") or "Multiple contacts matched.",
            }
        if res.get("status") == "found":
            return res["contact"].get("phone_e164"), None, None
        digits = normalize_phone(contact)
        if digits and len(digits) >= 4:
            return digits, None, None
        return None, str(contact).strip(), None

    # Backwards-compatible aliases used by existing HUD routes & builtin tools
    def read_messages(
        self,
        user_id: str,
        *,
        contact: Optional[str] = None,
        unread_only: bool = False,
        hours_back: Optional[int] = None,
        limit: int = 15,
        mark_read: bool = False,
    ) -> Dict[str, Any]:
        target_phone, peer_query, early = self._resolve_filter_target(user_id, contact)
        if early is not None:
            return early
        since_ts = (time.time() - int(hours_back) * 3600.0) if hours_back else None

        inbox_rows = self.db.read_whatsapp_inbox_messages(
            user_id,
            sender_e164=target_phone,
            since=since_ts,
            unread_only=unread_only,
            limit=limit,
        )
        legacy_rows = []
        if hasattr(self.db, "get_whatsapp_messages"):
            legacy_rows = self.db.get_whatsapp_messages(
                user_id,
                peer_phone=target_phone,
                peer_query=peer_query,
                unread_only=unread_only,
                since_ts=since_ts,
                limit=limit,
            )

        merged: List[Dict[str, Any]] = []
        seen_ids = set()
        for r in inbox_rows or []:
            rid = r["id"]
            seen_ids.add(rid)
            if peer_query and peer_query.lower() not in (r.get("sender_name") or "").lower():
                continue
            merged.append({
                "id": rid,
                "_src": "inbox",
                "sender_name": r.get("sender_name") or mask_phone(r.get("sender_e164") or ""),
                "peer_name": r.get("sender_name") or "",
                "phone_masked": mask_phone(r.get("sender_e164") or ""),
                "direction": "inbound",
                "text": r.get("body", ""),
                "media_type": r.get("kind", "text"),
                "is_read": r.get("status") == "read",
                "timestamp": float(r.get("received_at") or 0),
            })
        for r in legacy_rows or []:
            rid = r["id"]
            if rid in seen_ids:
                continue
            seen_ids.add(rid)
            phone = str(r.get("peer_phone") or "")
            merged.append({
                "id": rid,
                "_src": "legacy",
                "sender_name": str(r.get("peer_name") or "").strip() or mask_phone(phone),
                "peer_name": str(r.get("peer_name") or "").strip(),
                "phone_masked": mask_phone(phone) if phone else "****",
                "direction": r.get("direction", "inbound"),
                "text": r.get("text", ""),
                "media_type": r.get("media_type", "text"),
                "is_read": bool(r.get("is_read")),
                "timestamp": float(r.get("msg_timestamp") or r.get("created_at") or 0),
            })

        merged.sort(key=lambda m: m["timestamp"], reverse=True)
        merged = merged[: max(1, int(limit))]

        if mark_read and merged:
            inbox_ids = [m["id"] for m in merged if m["_src"] == "inbox" and not m["is_read"]]
            legacy_ids = [m["id"] for m in merged if m["_src"] == "legacy" and not m["is_read"]]
            if inbox_ids:
                self.db.mark_whatsapp_inbox_read(user_id, inbox_ids)
            if legacy_ids and hasattr(self.db, "mark_whatsapp_messages_read"):
                self.db.mark_whatsapp_messages_read(user_id, message_ids=legacy_ids)

        for m in merged:
            m.pop("_src", None)

        return {
            "status": "success" if merged else "empty",
            "count": len(merged),
            "messages": merged,
            "message": f"Found {len(merged)} message(s)." if merged else "No messages found.",
        }

    def search_messages(
        self,
        user_id: str,
        query: str,
        *,
        contact: Optional[str] = None,
        hours_back: Optional[int] = None,
        limit: int = 15,
    ) -> Dict[str, Any]:
        target_phone, peer_query, early = self._resolve_filter_target(user_id, contact)
        if early is not None:
            return early
        clean_q = (query or "").strip().lower()
        since_ts = (time.time() - int(hours_back) * 3600.0) if hours_back else None

        matched: List[Dict[str, Any]] = []
        seen_ids = set()
        if hasattr(self.db, "search_whatsapp_messages"):
            fts_rows = self.db.search_whatsapp_messages(
                user_id,
                query,
                peer_phone=target_phone,
                peer_query=peer_query,
                since_ts=since_ts,
                limit=limit,
            )
            for r in fts_rows or []:
                rid = r["id"]
                seen_ids.add(rid)
                phone = str(r.get("peer_phone") or "")
                matched.append({
                    "id": rid,
                    "sender_name": str(r.get("peer_name") or "").strip() or mask_phone(phone),
                    "peer_name": str(r.get("peer_name") or "").strip(),
                    "phone_masked": mask_phone(phone) if phone else "****",
                    "direction": r.get("direction", "inbound"),
                    "text": r.get("text", ""),
                    "media_type": r.get("media_type", "text"),
                    "is_read": bool(r.get("is_read")),
                    "timestamp": float(r.get("msg_timestamp") or r.get("created_at") or 0),
                })

        inbox_res = self.read_messages(user_id, contact=contact, unread_only=False, hours_back=hours_back, limit=50)
        for m in inbox_res.get("messages", []):
            if m["id"] in seen_ids:
                continue
            if all(tok in (m.get("text") or "").lower() for tok in clean_q.split() if tok):
                seen_ids.add(m["id"])
                matched.append(m)

        matched = matched[: max(1, int(limit))]
        return {
            "status": "success" if matched else "empty",
            "count": len(matched),
            "messages": matched,
            "message": f"Found {len(matched)} matching message(s)." if matched else "No matching messages found.",
        }

    def mark_messages_read(self, user_id: str, *, contact: Optional[str] = None, message_ids: Optional[List[str]] = None) -> Dict[str, Any]:
        ids = list(message_ids or [])
        inbox_count = 0
        legacy_count = 0
        if not ids:
            rows = self.db.list_whatsapp_inbox_metadata(user_id, unread_only=True, limit=100)
            if contact:
                cq = contact.strip().lower()
                inbox_ids = [r["id"] for r in rows if cq in (r.get("sender_name") or "").lower()]
            else:
                inbox_ids = [r["id"] for r in rows]
            if inbox_ids:
                inbox_count = self.db.mark_whatsapp_inbox_read(user_id, inbox_ids)
        else:
            inbox_count = self.db.mark_whatsapp_inbox_read(user_id, ids)

        if hasattr(self.db, "mark_whatsapp_messages_read"):
            peer_phone = None
            if contact:
                res = self.resolve_contact(user_id, contact)
                if res.get("status") == "found":
                    peer_phone = res["contact"].get("phone_e164")
            legacy_count = self.db.mark_whatsapp_messages_read(
                user_id, peer_phone=peer_phone, message_ids=(ids or None)
            )
        total = inbox_count + legacy_count
        return {"status": "success", "marked_count": total, "updated": total}

    # ─────────────────────────────────────────────────────────────
    # ADMIN CONTROLS (SHARED BY WHATSAPP & ADMIN HUD)
    # ─────────────────────────────────────────────────────────────
    def execute_admin_command(self, actor_user_id: str, raw_command: str) -> Dict[str, Any]:
        """Execute strict admin control command; rejects non-admins and records PII-free audit."""
        cfg = get_wa_config()
        actor = self.db.get_user_by_id(actor_user_id)
        if not is_admin_user(actor, cfg):
            return {"status": "forbidden", "message": "Admin permission required."}

        cmd = " ".join((raw_command or "").strip().split())
        low = cmd.lower()

        if low == "list users" or low == "users":
            users = self.db.list_all_users_admin_summary()
            unknown_24h = self.db.get_unknown_sender_count(86400)
            lines = [f"👥 *Users ({len(users)}):*"]
            for u in users:
                lines.append(
                    f"• {u['name'] or u['email']} [{u['gate_state']}] "
                    f"({u['phone_masked']}, {u['session_state']})"
                )
            lines.append(f"Unknown senders (24h): {unknown_24h}")
            self.db.record_admin_audit(actor_user_id, "list_users", "")
            return {"status": "success", "users": users, "message": "\n".join(lines)}

        m_app = re.match(r"^(approve|deny|revoke)\s+(.+)$", cmd, re.IGNORECASE)
        if m_app:
            verb = m_app.group(1).lower()
            target_q = m_app.group(2).strip()
            matches = self.db.find_users_by_name_or_id(target_q)
            if not matches:
                return {"status": "not_found", "message": f"No user found matching '{target_q}'."}
            if len(matches) > 1:
                names = [u.get("name") or u["email"] for u in matches]
                return {"status": "ambiguous", "message": f"Multiple matches: {', '.join(names)}. Use ID."}
            target = matches[0]
            if is_admin_user(target, cfg) and verb in ("deny", "revoke"):
                return {"status": "error", "message": "Cannot revoke or deny the configured admin account."}

            if verb == "approve":
                self.db.set_user_gate_state(
                    target["id"],
                    "active",
                    approved_by=actor_user_id,
                    approved_at=time.time(),
                    access_epoch=cfg.access_epoch,
                    is_active=1,
                )
                self.db.record_admin_audit(actor_user_id, "approve_user", target["id"])
                self._emit_hud_unlock(target["id"], "active")
                link = self.db.get_verified_link_for_user(target["id"])
                if link and link.get("phone_e164"):
                    self._schedule_coro(
                        self.send_whatsapp_message(
                            link["phone_e164"],
                            "Your AIVA access has been approved. Send aiva\\ to start chatting.",
                            user_id=None,
                        )
                    )
                return {"status": "success", "message": f"Approved {target.get('name') or target['email']}."}

            # deny or revoke
            self.db.set_user_gate_state(
                target["id"],
                "revoked",
                blocked_reason=verb,
                is_active=0,
                bump_token_version=True,
            )
            self.db.revoke_all_user_tokens(target["id"])
            self.db.set_whatsapp_session_state(target["id"], "INACTIVE")
            self.db.record_admin_audit(actor_user_id, f"{verb}_user", target["id"])
            self._invalidate_user(target["id"], reason="revoked")
            return {"status": "success", "message": f"{verb.title()}d access for {target.get('name') or target['email']}."}

        m_blk = re.match(r"^block\s+(.+)$", cmd, re.IGNORECASE)
        if m_blk:
            phone = normalize_phone(m_blk.group(1))
            if not phone:
                return {"status": "error", "message": "Provide a valid phone number to block."}
            if cfg.aiva_whatsapp_number and phone == cfg.aiva_whatsapp_number:
                return {"status": "error", "message": "Cannot block AIVA's own number."}
            link = self.db.get_whatsapp_link_by_phone(phone)
            target_uid = link["user_id"] if link else ""
            if target_uid:
                t_user = self.db.get_user_by_id(target_uid)
                if is_admin_user(t_user, cfg):
                    return {"status": "error", "message": "Cannot block the admin's own number."}
                self.db.set_user_gate_state(
                    target_uid,
                    "blocked",
                    blocked_reason="admin_blocked",
                    is_active=0,
                    bump_token_version=True,
                )
                self.db.revoke_all_user_tokens(target_uid)
                self.db.set_whatsapp_session_state(target_uid, "INACTIVE")
                self._invalidate_user(target_uid, reason="blocked")
            self.db.block_number(phone, blocked_by=actor_user_id, reason="admin_command")
            self.db.record_admin_audit(actor_user_id, "block_number", target_uid)
            return {"status": "success", "message": f"Blocked number {mask_phone(phone)}."}

        m_unblk = re.match(r"^unblock\s+(.+)$", cmd, re.IGNORECASE)
        if m_unblk:
            phone = normalize_phone(m_unblk.group(1))
            if not phone:
                return {"status": "error", "message": "Provide a valid phone number to unblock."}
            ok = self.db.unblock_number(phone)
            self.db.record_admin_audit(actor_user_id, "unblock_number", mask_phone(phone))
            return {"status": "success" if ok else "not_found", "message": f"Unblocked number {mask_phone(phone)}." if ok else "Number not found in blocklist."}

        return {"status": "unknown_admin_cmd", "message": "Unknown admin command."}

    def _invalidate_user(self, user_id: str, reason: str = "revoked") -> None:
        if self.session_invalidator:
            try:
                self.session_invalidator(user_id, reason)
            except Exception:
                pass
        try:
            import aiva_server
            aiva_server.invalidate_user_sessions(user_id, reason=reason)
        except Exception:
            pass

    # ─────────────────────────────────────────────────────────────
    # BACKGROUND TICK (IDLE SESSIONS, ADMIN NOTICES, NOTIFIER)
    # ─────────────────────────────────────────────────────────────
    async def run_periodic_tick(self, now_ts: Optional[float] = None, now_dt: Optional[datetime] = None) -> Dict[str, Any]:
        now = float(now_ts if now_ts is not None else time.time())
        expired = await self.expire_idle_sessions(now_ts=now)
        notif_stats = await self.notifier.flush_due_notifications(now_ts=now, now_dt=now_dt)
        notices_flushed = await self.flush_admin_notices(now_ts=now)
        return {"idle_expired": expired, **notif_stats, "admin_notices": notices_flushed}

    async def expire_idle_sessions(self, now_ts: Optional[float] = None) -> int:
        cfg = get_wa_config()
        if cfg.session_idle_minutes <= 0:
            return 0
        now = float(now_ts if now_ts is not None else time.time())
        cutoff = cfg.session_idle_minutes * 60.0
        expired_count = 0
        for row in self.db.get_active_whatsapp_sessions():
            last_act = float(row.get("last_activity_at") or 0.0)
            if last_act > 0 and (now - last_act) >= cutoff and not int(row.get("idle_notified") or 0):
                uid = row["user_id"]
                self.db.set_whatsapp_session_state(uid, "INACTIVE", idle_notified=1)
                self.db.clear_whatsapp_reply_window(uid)
                if row.get("phone_e164"):
                    await self.send_whatsapp_message(
                        row["phone_e164"],
                        "Session ended due to inactivity.",
                        user_id=None,
                        to_jid=row.get("wa_jid") or None,
                    )
                expired_count += 1
        return expired_count

    async def flush_admin_notices(self, now_ts: Optional[float] = None, force: bool = False) -> int:
        now = float(now_ts if now_ts is not None else time.time())
        if not force and (now - self._last_admin_notice_flush) < _ADMIN_NOTICE_BATCH_INTERVAL_SEC:
            return 0
        cfg = get_wa_config()
        if not cfg.admin_email:
            return 0
        admin_user = self.db.get_user_by_email(cfg.admin_email)
        if not admin_user:
            return 0
        admin_link = self.db.get_verified_link_for_user(admin_user["id"])
        if not admin_link or not admin_link.get("phone_e164"):
            return 0
        notices = self.db.pop_pending_admin_notices()
        if not notices:
            return 0
        self._last_admin_notice_flush = now
        lines = [f"🛡 *Admin Summary ({len(notices)} events):*"]
        for n in notices[:15]:
            lines.append(f"• {n.get('summary') or n.get('event_type')}")
        if len(notices) > 15:
            lines.append(f"…and {len(notices) - 15} more.")
        await self.send_whatsapp_message(admin_link["phone_e164"], "\n".join(lines), user_id=None)
        return len(notices)

    # ─────────────────────────────────────────────────────────────
    # INBOUND PIPELINE
    # ─────────────────────────────────────────────────────────────
    async def handle_inbound(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Process inbound WhatsApp message strictly without PII logging.
        Order:
        1. Dedupe, blocklist & own-account filter
        2. Entry B code / active credential challenge / epoch reauth / Entry A trigger
        3. Unlinked non-trigger -> third-party inbox capture (metadata-separated notifier)
        4. Verified user session/reply-window/admin/agent routing
        """
        t0 = time.time()
        cfg = get_wa_config()

        if bool(payload.get("from_me", False)):
            return {"status": "ignored_echo"}

        chat_jid = str(payload.get("chat_jid") or "").strip()
        if chat_jid.endswith("@g.us") or chat_jid == "status@broadcast" or chat_jid.endswith("@broadcast"):
            return {"status": "ignored_non_direct"}

        message_id = str(payload.get("message_id") or "").strip()
        if self.is_message_duplicate(message_id):
            return {"status": "ignored_duplicate"}

        sender_raw = str(payload.get("sender") or "")
        lid_jid = str(payload.get("lid_jid") or "").strip()
        sender_phone = normalize_phone(sender_raw.split("@")[0] if "@" in sender_raw else sender_raw)

        now = time.time()

        # Resolve LID -> linked user phone or pre-verified challenge phone (via USync/sender_pn)
        link = None
        if sender_phone:
            link = self.db.get_whatsapp_link_by_phone(sender_phone)
        if not link and lid_jid:
            link = self.db.get_whatsapp_link_by_lid(lid_jid)
            if link and not sender_phone:
                sender_phone = normalize_phone(link.get("phone_e164", ""))
        if not sender_phone and lid_jid:
            ch_by_lid = self.db.get_challenge_by_lid(lid_jid, now=now)
            if ch_by_lid:
                sender_phone = normalize_phone(ch_by_lid.get("phone_e164", ""))

        if not sender_phone:
            return {"status": "ignored_invalid_sender"}
        if cfg.aiva_whatsapp_number and sender_phone == cfg.aiva_whatsapp_number:
            return {"status": "ignored_own_number"}
        if self.db.is_number_blocked(sender_phone):
            return {"status": "ignored_blocked"}

        masked = mask_phone(sender_phone)
        media_type = str(payload.get("media_type") or "text").strip().lower() or "text"
        text = str(payload.get("text") or "").strip()
        reply_jid = chat_jid if chat_jid else None

        async def _send_reply(msg: str, uid_for_cap: Optional[str] = None) -> Dict[str, Any]:
            return await self.send_whatsapp_message(sender_phone, msg, user_id=uid_for_cap, to_jid=reply_jid)

        logger.info(f"[WhatsApp] stage=inbound sender={masked} media={media_type} linked={bool(link)} status=recv")

        # Keep LID/WA JID binding fresh on linked accounts and pending challenges
        if link and (lid_jid or chat_jid):
            self.db.bind_whatsapp_link_lid(sender_phone, lid_jid=lid_jid, wa_jid=chat_jid)
        elif lid_jid:
            self.db.bind_challenge_lid(sender_phone, lid_jid)

        user = self.db.get_user_by_id(link["user_id"]) if link else None
        if user and (not user.get("is_active", 1) or user.get("gate_state") in ("blocked", "revoked")):
            return {"status": "ignored_revoked"}

        # ── A. Active deterministic credential challenge (never hits LLM/memory/inbox) ──
        ch = self.db.get_challenge_by_phone(sender_phone)
        if ch and float(ch.get("locked_until") or 0) > now:
            # Silent while locked out
            return {"status": "ignored_locked"}

        # Check Epoch Re-authentication for non-admin linked users
        if (
            link
            and user
            and not is_admin_user(user, cfg)
            and int(link.get("verified_by_epoch") or 0) < cfg.access_epoch
            and (not ch or ch.get("state") not in ("AWAIT_USERNAME", "AWAIT_PASSWORD"))
        ):
            self.db.set_user_gate_state(user["id"], "credential_pending")
            self._invalidate_user(user["id"], reason="verification_required")
            start_res = self._begin_credential_challenge(
                sender_phone,
                entry_mode="reauth",
                user_id=user["id"],
                lid_jid=lid_jid,
                cfg=cfg,
                now=now,
                existing_ch=ch,
            )
            if start_res == "ok":
                await _send_reply("Security credentials were updated. Please verify again.\nEnter your username:")
                return {"status": "reauth_started"}
            return {"status": start_res}

        if ch and ch.get("state") in ("AWAIT_USERNAME", "AWAIT_PASSWORD", "AWAIT_NAME"):
            return await self._handle_challenge_step(
                ch=ch,
                sender_phone=sender_phone,
                lid_jid=lid_jid,
                text=text,
                cfg=cfg,
                now=now,
                send_fn=_send_reply,
            )

        # ── B. Entry B verification code arrival ('AIVA-123456' or 'LINK AIVA-123456') ──
        code_candidate = self._extract_entry_b_code(text)
        if code_candidate:
            pending_ch = self.db.get_challenge_by_code(code_candidate, now=now)
            pending_lnk = self.db.get_whatsapp_link_by_code(code_candidate)
            target_uid = (pending_ch or pending_lnk or {}).get("user_id")
            expected_phone = normalize_phone((pending_ch or pending_lnk or {}).get("phone_e164", ""))
            if target_uid and expected_phone == sender_phone:
                target_user = self.db.get_user_by_id(target_uid)
                if not target_user or target_user.get("gate_state") in ("blocked", "revoked"):
                    return {"status": "ignored_revoked"}
                if is_admin_user(target_user, cfg):
                    # Admin owner exemption: skip credential challenge
                    self.db.upsert_verified_whatsapp_link(
                        target_uid,
                        sender_phone,
                        verified_by_epoch=cfg.access_epoch,
                        session_state="ACTIVE",
                        lid_jid=lid_jid,
                        wa_jid=chat_jid,
                    )
                    self.db.set_user_gate_state(target_uid, "active", access_epoch=cfg.access_epoch, is_active=1)
                    if pending_ch:
                        self._clear_challenge(pending_ch["id"], sender_phone)
                    self._emit_hud_unlock(target_uid, "active")
                    await _send_reply("Code verified! Owner WhatsApp linked and HUD unlocked.")
                    return {"status": "owner_linked"}

                # Non-admin Entry B -> proceed to deterministic credential challenge
                self.db.set_user_gate_state(target_uid, "credential_pending")
                self._invalidate_user(target_uid, reason="verification_required")
                self.db.upsert_challenge({
                    "id": (pending_ch or {}).get("id") or f"wch_{uuid.uuid4().hex[:12]}",
                    "phone_e164": sender_phone,
                    "lid_jid": lid_jid,
                    "user_id": target_uid,
                    "entry_mode": "B",
                    "verify_code": "",
                    "code_expires_at": 0,
                    "state": "AWAIT_USERNAME",
                    "attempts": 0,
                    "lockout_count": int((pending_ch or {}).get("lockout_count") or 0),
                    "last_started_at": now,
                    "expires_at": now + cfg.access_challenge_ttl_minutes * 60.0,
                })
                await _send_reply("Code verified. Enter your username:")
                return {"status": "entry_b_await_username"}

        # ── C. Unlinked number: Entry A start trigger OR third-party inbox capture ──
        if not link or not user:
            is_start, _ = match_start_trigger(text, cfg.start_trigger)
            if is_start:
                res = self._begin_credential_challenge(
                    sender_phone,
                    entry_mode="A",
                    user_id="",
                    lid_jid=lid_jid,
                    cfg=cfg,
                    now=now,
                    existing_ch=ch,
                )
                if res == "ok":
                    await _send_reply("Welcome. Enter your username:")
                    return {"status": "entry_a_started"}
                return {"status": res}

            # Not a verified user and not a start trigger -> capture as third-party 1:1 inbound (never reply)
            return self.notifier.capture_third_party_message(payload, cfg=cfg, now_ts=now)

        # ── D. Verified user awaiting owner approval ──
        if user.get("gate_state") == "approval_pending":
            is_start, _ = match_start_trigger(text, cfg.start_trigger)
            if is_start:
                last_n = self._pending_approval_notice_ts.get(user["id"], 0.0)
                if now - last_n >= 300.0:
                    self._pending_approval_notice_ts[user["id"]] = now
                    await _send_reply("Your account is awaiting admin approval.")
            return {"status": "approval_pending"}

        if user.get("gate_state", "active") != "active":
            return {"status": "verification_required"}

        # ── E. Active user session, triggers & reply window scoping ──
        user_id = user["id"]
        session_state = str(link.get("session_state") or "INACTIVE").upper()
        last_activity_at = float(link.get("last_activity_at") or 0.0)

        # Lazy idle check before routing current message
        if (
            session_state == "ACTIVE"
            and cfg.session_idle_minutes > 0
            and last_activity_at > 0
            and (now - last_activity_at) >= cfg.session_idle_minutes * 60.0
        ):
            session_state = "INACTIVE"
            self.db.set_whatsapp_session_state(user_id, "INACTIVE", idle_notified=1)
            if not int(link.get("idle_notified") or 0):
                await _send_reply("Session ended due to inactivity.")

        if match_stop_trigger(text, cfg.stop_trigger):
            self.db.set_whatsapp_session_state(user_id, "INACTIVE")
            self.db.clear_whatsapp_reply_window(user_id)
            await _send_reply("Session ended. Send aiva\\ whenever you're ready to chat again.")
            return {"status": "session_stopped"}

        is_start, inline_cmd = match_start_trigger(text, cfg.start_trigger)

        if session_state != "ACTIVE":
            if is_start:
                self.db.set_whatsapp_session_state(user_id, "ACTIVE", last_activity_at=now, idle_notified=0)
                self.db.refresh_whatsapp_activity(user_id, now=now)
                if not inline_cmd:
                    await _send_reply("Session started. How can I help?")
                    return {"status": "session_started"}
                text = inline_cmd
            else:
                # Check scoped reply window ONLY (never execute freeform LLM while INACTIVE)
                win_until = float(link.get("reply_window_until") or 0.0)
                win_scope = str(link.get("reply_window_scope") or "")
                if win_until > now and win_scope:
                    return await self._handle_reply_window(
                        user_id=user_id,
                        scope=win_scope,
                        text=text,
                        send_fn=_send_reply,
                    )
                return {"status": "ignored_inactive_session"}

        # Now in ACTIVE session (also handle inline command after start trigger)
        self.db.refresh_whatsapp_activity(user_id, now=now)

        # Lazy-download & transcribe user's own voice note inside active command session
        if media_type == "audio" and not text and message_id:
            text = await self._lazy_transcribe_active_user_voice(message_id)

        if not text:
            return {"status": "ignored_empty"}

        # Enforce daily user message cap (non-admin)
        if not is_admin_user(user, cfg):
            allowed, used = self.db.increment_and_check_daily_message_cap(user_id, cfg.user_daily_message_cap)
            if not allowed:
                await _send_reply("Daily message limit reached for today.")
                return {"status": "daily_cap_reached", "used": used}

        # Admin commands via WhatsApp (restricted strictly to admin)
        if is_admin_user(user, cfg) and self._looks_like_admin_command(text):
            adm_res = self.execute_admin_command(user_id, text)
            await _send_reply(adm_res["message"])
            return {"status": f"admin_{adm_res['status']}"}

        clean_upper = text.strip().upper()
        if clean_upper == "HELP":
            await _send_reply(
                "🤖 *AIVA Commands*\n"
                "• *Session*: `aiva\\` starts, `stop\\` ends.\n"
                "• *Read Inbox*: 'read unread WhatsApp messages' or 'any messages from dad?'\n"
                "• *Contacts*: 'save <number> as <name>', 'add alias <alias> for <name>', 'list contacts'\n"
                "• *Alerts*: 'mute <name>', 'unmute <name>', 'priority <name>'\n"
                "• *Send*: 'message <contact> <text>'\n"
                "• *Status*: 'status'"
            )
            return {"status": "command_help"}

        if clean_upper == "STATUS":
            contacts = self.db.get_whatsapp_contacts(user_id)
            inbox_summary = self.db.get_whatsapp_inbox_unread_summary(user_id)
            cap_note = "" if is_admin_user(user, cfg) else f"\n• Daily Usage: {int(user.get('daily_msg_count') or 0)}/{cfg.user_daily_message_cap}"
            await _send_reply(
                f"🟢 *AIVA Status*\n"
                f"• Session: ACTIVE\n"
                f"• Verified Number: {masked}\n"
                f"• Contacts: {len(contacts)}\n"
                f"• Unread Inbox: {inbox_summary['unread_total']}{cap_note}"
            )
            return {"status": "command_status"}

        # Contact save / alias / list / mute / unmute / priority deterministic shortcuts
        pref_res = await self._maybe_handle_contact_command(user_id, text, _send_reply)
        if pref_res is not None:
            return pref_res

        # Power control guard
        lower_text = text.lower()
        if (
            any(w in lower_text for w in ("shutdown", "restart", "reboot", "hibernate", "sleep pc"))
            or (clean_upper == "CONFIRM" and user_id in self._pending_power_actions)
        ):
            return await self._handle_power_command(user_id, text, clean_upper, cfg, _send_reply)

        # Agent Core / conversational routing (always explicit user turn + user_id)
        response_text = await self._handle_general_query(user_id, text)
        if response_text:
            await _send_reply(response_text, uid_for_cap=user_id)

        duration_ms = (time.time() - t0) * 1000.0
        self.db.log_whatsapp_event(user_id, "inbound", "handle_inbound", "ok", duration_ms)
        return {"status": "processed"}

    @staticmethod
    def _extract_entry_b_code(text: str) -> Optional[str]:
        clean = (text or "").strip().upper()
        if clean.startswith("LINK "):
            clean = clean[5:].strip()
        if re.match(r"^AIVA-\d{6}$", clean):
            return clean
        return None

    def _begin_credential_challenge(
        self,
        phone: str,
        *,
        entry_mode: str,
        user_id: str,
        lid_jid: str,
        cfg: WhatsAppConfig,
        now: float,
        existing_ch: Optional[Dict[str, Any]],
    ) -> str:
        if existing_ch:
            if float(existing_ch.get("locked_until") or 0) > now:
                return "ignored_locked"
            last_start = float(existing_ch.get("last_started_at") or 0)
            if (now - last_start) < _CHALLENGE_START_COOLDOWN_SEC and existing_ch.get("state") in ("IDLE", "LOCKED"):
                return "ignored_cooldown"

        if self.db.count_recent_challenge_starts(3600, now=now) >= cfg.access_max_challenges_per_hour:
            return "ignored_hourly_cap"

        cid = (existing_ch or {}).get("id") or f"wch_{uuid.uuid4().hex[:12]}"
        self.db.upsert_challenge({
            "id": cid,
            "phone_e164": phone,
            "lid_jid": lid_jid,
            "user_id": user_id,
            "entry_mode": entry_mode,
            "state": "AWAIT_USERNAME",
            "challenge_nonce": secrets.token_hex(8),
            "username_hmac": "",
            "attempts": 0,
            "lockout_count": int((existing_ch or {}).get("lockout_count") or 0),
            "locked_until": 0,
            "last_started_at": now,
            "expires_at": now + max(1, int(cfg.access_challenge_ttl_minutes)) * 60.0,
            "updated_at": now,
            "created_at": (existing_ch or {}).get("created_at") or now,
        })
        logger.info(f"[WhatsApp] stage=challenge_start mode={entry_mode} phone={mask_phone(phone)} status=ok")
        return "ok"

    async def _handle_challenge_step(
        self,
        *,
        ch: Dict[str, Any],
        sender_phone: str,
        lid_jid: str,
        text: str,
        cfg: WhatsAppConfig,
        now: float,
        send_fn: Callable[[str], Any],
    ) -> Dict[str, Any]:
        state = ch.get("state")
        if now > float(ch.get("expires_at") or 0):
            # Allow re-trigger seamlessly if user sent start trigger
            is_start, _ = match_start_trigger(text, cfg.start_trigger)
            self._clear_challenge(ch["id"], sender_phone)
            if is_start:
                res = self._begin_credential_challenge(
                    sender_phone,
                    entry_mode=ch.get("entry_mode") or "A",
                    user_id=ch.get("user_id") or "",
                    lid_jid=lid_jid,
                    cfg=cfg,
                    now=now,
                    existing_ch=ch,
                )
                if res == "ok":
                    await send_fn("Welcome. Enter your username:")
                    return {"status": "challenge_restarted"}
            await send_fn("Verification timed out. Send the start trigger again when ready.")
            return {"status": "challenge_expired"}

        if state == "AWAIT_USERNAME":
            cleaned_u = (text or "").strip().lower()
            nonce = ch.get("challenge_nonce") or secrets.token_hex(8)
            u_hmac = hmac.new(nonce.encode("utf-8"), cleaned_u.encode("utf-8"), hashlib.sha256).hexdigest()
            self._ephemeral_usernames[sender_phone] = (text or "", now + 600.0)
            self.db.upsert_challenge({
                **ch,
                "state": "AWAIT_PASSWORD",
                "challenge_nonce": nonce,
                "username_hmac": u_hmac,
                "updated_at": now,
            })
            await send_fn("Enter your password:")
            return {"status": "await_password"}

        if state == "AWAIT_PASSWORD":
            pair = self._ephemeral_usernames.pop(sender_phone, None)
            typed_username = pair[0] if pair and pair[1] >= now else ""
            ok = verify_access_credentials(typed_username, text, cfg=cfg)
            if not ok:
                attempts = int(ch.get("attempts") or 0) + 1
                if attempts >= max(1, int(cfg.access_max_attempts)):
                    lock_count = int(ch.get("lockout_count") or 0) + 1
                    lock_min = min(cfg.access_lock_minutes * (2 ** (lock_count - 1)), 24 * 60)
                    locked_until = now + lock_min * 60.0
                    self.db.upsert_challenge({
                        **ch,
                        "state": "LOCKED",
                        "username_hmac": "",
                        "attempts": 0,
                        "lockout_count": lock_count,
                        "locked_until": locked_until,
                        "updated_at": now,
                    })
                    self.db.record_admin_notice("lockout", f"Credential lockout on {mask_phone(sender_phone)} ({lock_min}m)", ch.get("user_id") or "")
                    logger.warning(f"[WhatsApp] stage=challenge_lockout phone={mask_phone(sender_phone)} tier={lock_count} status=locked")
                    await send_fn("Incorrect credentials")
                    return {"status": "challenge_locked_out"}

                self.db.upsert_challenge({
                    **ch,
                    "state": "AWAIT_USERNAME",
                    "username_hmac": "",
                    "attempts": attempts,
                    "updated_at": now,
                })
                logger.info(f"[WhatsApp] stage=challenge_attempt phone={mask_phone(sender_phone)} status=failed")
                await send_fn("Incorrect credentials\n\nEnter your username:")
                return {"status": "challenge_retry"}

            # Credentials succeeded!
            entry_mode = ch.get("entry_mode") or "A"
            existing_uid = (ch.get("user_id") or "").strip()
            if entry_mode in ("B", "reauth") and existing_uid:
                user_row = self.db.get_user_by_id(existing_uid)
                if not user_row:
                    self._clear_challenge(ch["id"], sender_phone)
                    return {"status": "error_missing_user"}
                self.db.upsert_verified_whatsapp_link(
                    existing_uid,
                    sender_phone,
                    verified_by_epoch=cfg.access_epoch,
                    session_state="ACTIVE",
                    lid_jid=lid_jid,
                )
                self._clear_challenge(ch["id"], sender_phone)
                need_approval = cfg.require_owner_approval and entry_mode == "B" and not is_admin_user(user_row, cfg)
                new_gate = "approval_pending" if need_approval else "active"
                self.db.set_user_gate_state(existing_uid, new_gate, access_epoch=cfg.access_epoch)
                self._emit_hud_unlock(existing_uid, new_gate)
                self.db.record_admin_notice(
                    "link_verified",
                    f"WhatsApp linked ({entry_mode}): {user_row.get('name') or mask_phone(sender_phone)}",
                    existing_uid,
                )
                if need_approval:
                    await send_fn("Access verified. Waiting for admin approval.\n\nYou can delete your credential messages from this chat.")
                    return {"status": "approval_pending"}
                await send_fn("Access granted. HUD unlocked. Send stop\\ when you're done.\n\nYou can delete your credential messages from this chat.")
                return {"status": "entry_b_unlocked"}

            # Entry A -> ask explicit preferred display name once
            self.db.upsert_challenge({
                **ch,
                "state": "AWAIT_NAME",
                "username_hmac": "",
                "attempts": 0,
                "lockout_count": 0,
                "expires_at": now + max(1, int(cfg.access_challenge_ttl_minutes)) * 60.0,
                "updated_at": now,
            })
            await send_fn("What should I call you?\n\nYou can delete your credential messages from this chat.")
            return {"status": "await_name"}

        if state == "AWAIT_NAME":
            valid, clean_name = validate_preferred_display_name(text)
            if not valid:
                await send_fn("Please enter a valid display name (letters or numbers, 1-40 characters):")
                return {"status": "invalid_name"}

            wa_uid = f"wa_{uuid.uuid4().hex[:12]}"
            gate = "approval_pending" if cfg.require_owner_approval else "active"
            self.db.create_user(
                user_id=wa_uid,
                email=f"{wa_uid}@wa.invalid",
                name=clean_name,
                onboarded=1,
                gate_state=gate,
                auth_source="whatsapp",
                access_epoch=cfg.access_epoch,
            )
            self.db.upsert_verified_whatsapp_link(
                wa_uid,
                sender_phone,
                verified_by_epoch=cfg.access_epoch,
                session_state="INACTIVE" if gate == "approval_pending" else "ACTIVE",
                lid_jid=lid_jid,
            )
            self._clear_challenge(ch["id"], sender_phone)
            self.db.record_admin_notice(
                "new_wa_user",
                f"New WhatsApp user: {clean_name} ({mask_phone(sender_phone)}) [{gate}]",
                wa_uid,
            )
            logger.info(f"[WhatsApp] stage=entry_a_registered user={wa_uid} gate={gate} status=ok")
            if gate == "approval_pending":
                await send_fn(f"Thank you, {clean_name}. Your account is waiting for admin approval.")
                return {"status": "entry_a_pending_approval"}
            await send_fn(f"Access granted, {clean_name}. Send stop\\ when you're done.")
            return {"status": "entry_a_granted"}

        return {"status": "ignored"}

    async def _handle_reply_window(
        self,
        *,
        user_id: str,
        scope: str,
        text: str,
        send_fn: Callable[[str], Any],
    ) -> Dict[str, Any]:
        clean_low = (text or "").strip().lower()
        self.db.clear_whatsapp_reply_window(user_id)

        if scope.startswith("power_confirm:"):
            action = scope.split(":", 1)[1]
            if clean_low in ("confirm", "yes"):
                pending = self._pending_power_actions.pop(user_id, None)
                if pending and time.time() <= pending["expires_at"] and self.kernel and getattr(self.kernel, "device_channel", None):
                    await self.kernel.device_channel.dispatch_command(user_id, "system_power", {"action": action})
                    await send_fn(f"Executed {action}.")
                    return {"status": "power_executed"}
                await send_fn("Confirmation expired.")
                return {"status": "power_expired"}
            await send_fn("Power action cancelled.")
            return {"status": "power_cancelled"}

        if scope.startswith("inbox_prompt:"):
            target = scope.split(":", 1)[1]
            if clean_low in ("ignore", "no", "skip", "dismiss"):
                await send_fn("Okay, skipped.")
                return {"status": "inbox_ignored"}
            if clean_low in ("read", "yes", "read all", "show", "check"):
                contact_filter = None if target == "all" else target
                # Look up contact display name if target is a contact_id
                if contact_filter and contact_filter.startswith("wac_"):
                    c_obj = self.db.get_whatsapp_contact_by_id(user_id, contact_filter)
                    contact_filter = c_obj["display_name"] if c_obj else None
                res = await self.read_inbox_messages(
                    user_id,
                    contact=contact_filter,
                    unread_only=True,
                    mark_read=True,
                    turn_origin="user_explicit",
                )
                await send_fn(res.get("message") or "No unread messages found.")
                return {"status": "inbox_read_via_window"}

        return {"status": "ignored_outside_window_scope"}

    async def _lazy_transcribe_active_user_voice(self, message_id: str) -> str:
        code, body = await self._bridge_request("POST", "/media/download", payload={"message_id": message_id}, timeout=15.0)
        if code != 200 or not isinstance(body, dict) or not body.get("base64"):
            return ""
        try:
            audio_bytes = base64.b64decode(body["base64"])
            if self.kernel and hasattr(self.kernel, "voice_service"):
                vs = self.kernel.voice_service
                if hasattr(vs, "transcribe_bytes"):
                    return (await vs.transcribe_bytes(audio_bytes, filename="command.ogg")) or ""
        except Exception as e:
            logger.warning(f"[WhatsApp] stage=active_voice_transcribe err={type(e).__name__}")
        return ""

    @staticmethod
    def _looks_like_admin_command(text: str) -> bool:
        low = " ".join((text or "").strip().lower().split())
        return bool(
            low == "list users"
            or re.match(r"^(approve|deny|revoke|block|unblock)\s+\S+", low)
        )

    async def _maybe_handle_contact_command(
        self,
        user_id: str,
        text: str,
        send_fn: Callable[[str], Any],
    ) -> Optional[Dict[str, Any]]:
        save_match = re.match(r"^save\s+([+0-9\s-]+)\s+as\s+(.+)$", text, re.IGNORECASE)
        if save_match:
            c_phone = normalize_phone(save_match.group(1))
            c_name = save_match.group(2).strip()
            if c_phone and c_name:
                self.db.add_whatsapp_contact(user_id, c_name, c_phone, aliases=[c_name.lower()])
                await send_fn(f"✅ Contact saved: *{c_name}* ({mask_phone(c_phone)}).")
                return {"status": "contact_saved"}

        alias_match = re.match(r"^add\s+alias\s+(.+?)\s+for\s+(.+)$", text, re.IGNORECASE)
        if alias_match:
            alias_val = alias_match.group(1).strip()
            target_name = alias_match.group(2).strip()
            resolution = self.resolve_contact(user_id, target_name)
            if resolution["status"] == "found":
                c = resolution["contact"]
                cur = list(c.get("aliases", []))
                if alias_val.lower() not in [a.lower() for a in cur]:
                    cur.append(alias_val)
                    self.db.update_whatsapp_contact(user_id, c["id"], aliases=cur)
                await send_fn(f"✅ Added alias *{alias_val}* for *{c['display_name']}*.")
                return {"status": "alias_added"}
            await send_fn(resolution["message"])
            return {"status": "alias_failed"}

        if re.match(r"^list\s+(my\s+)?contacts$", text, re.IGNORECASE):
            contacts = self.db.get_whatsapp_contacts(user_id)
            if not contacts:
                await send_fn("You have no saved WhatsApp contacts yet.")
            else:
                lines = ["📒 *Your WhatsApp Contacts:*"]
                for c in contacts:
                    flags = []
                    if not c.get("notify_inbound", True):
                        flags.append("muted")
                    if c.get("priority", False):
                        flags.append("priority")
                    flag_str = f" [{', '.join(flags)}]" if flags else ""
                    lines.append(f"• {c['display_name']} ({mask_phone(c['phone_e164'])}){flag_str}")
                await send_fn("\n".join(lines))
            return {"status": "contacts_listed"}

        pref_match = re.match(r"^(mute|unmute|priority|unpriority)\s+(.+)$", text.strip(), re.IGNORECASE)
        if pref_match:
            verb = pref_match.group(1).lower()
            target = pref_match.group(2).strip()
            kwargs: Dict[str, Any] = {}
            if verb == "mute":
                kwargs["notify_inbound"] = False
            elif verb == "unmute":
                kwargs["notify_inbound"] = True
            elif verb == "priority":
                kwargs["priority"] = True
                kwargs["notify_inbound"] = True
            elif verb == "unpriority":
                kwargs["priority"] = False
            res = self.update_contact_preference(user_id, target, **kwargs)
            if res["status"] == "updated":
                cname = res["contact"]["display_name"]
                await send_fn(f"✅ Updated *{cname}*: {verb}.")
                return {"status": "contact_pref_updated"}
            await send_fn(res.get("message") or "Could not update contact preference.")
            return {"status": "contact_pref_failed"}

        return None

    async def _handle_power_command(
        self,
        user_id: str,
        text: str,
        clean_upper: str,
        cfg: WhatsAppConfig,
        send_fn: Callable[[str], Any],
    ) -> Dict[str, Any]:
        if not cfg.allow_power_control:
            await send_fn("🔒 Power control commands are disabled via WhatsApp for security.")
            return {"status": "power_blocked"}

        pending = self._pending_power_actions.get(user_id)
        if clean_upper == "CONFIRM" and pending and time.time() < pending["expires_at"]:
            cmd_to_run = pending["command"]
            del self._pending_power_actions[user_id]
            self.db.clear_whatsapp_reply_window(user_id)
            await send_fn(f"⚡ Executing {cmd_to_run}...")
            if self.kernel and getattr(self.kernel, "device_channel", None):
                res = await self.kernel.device_channel.dispatch_command(user_id, "system_power", {"action": cmd_to_run})
                return {"status": "power_executed", "result": res}
            return {"status": "power_no_device"}

        cmd = "shutdown" if "shutdown" in text.lower() else ("hibernate" if "hibernate" in text.lower() else "restart")
        self._pending_power_actions[user_id] = {"command": cmd, "expires_at": time.time() + 60.0}
        self.db.set_whatsapp_reply_window(user_id, f"power_confirm:{cmd}", duration_minutes=1)
        await send_fn("⚠️ Reply *CONFIRM* within 60 seconds to execute system power control.")
        return {"status": "power_confirmation_requested"}

    def recall(self, user_id: str, query: str) -> Dict[str, Any]:
        if self.kernel and getattr(self.kernel, "memory_engine", None):
            return self.kernel.memory_engine.recall(user_id, query)
        return {"status": "unavailable", "memories": [], "entities": [], "summary": ""}

    def create_reminder(self, user_id: str, text: str, due_at: Any, recurrence: Optional[str] = None) -> Dict[str, Any]:
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
        """Process general query cleanly through AIVA runtime with explicit user_id & user_explicit origin."""
        if not self.kernel:
            return "AIVA kernel is currently offline."

        try:
            if hasattr(self.kernel, "input_handler") and self.kernel.input_handler:
                res = await self.kernel.input_handler.process_query(
                    query,
                    speak=False,
                    user_id=user_id,
                    channel="whatsapp",
                    turn_origin="user_explicit",
                )
                if isinstance(res, dict):
                    return res.get("response") or res.get("message") or "Done."
                return str(res)
            elif hasattr(self.kernel, "conversation_service") and self.kernel.conversation_service:
                mem_engine = getattr(self.kernel, "memory_engine", None)
                display_name = (
                    mem_engine.get_user_display_name(user_id)
                    if mem_engine and hasattr(mem_engine, "get_user_display_name")
                    else None
                )
                mem_ctx = (
                    mem_engine.build_context(user_id, query, channel="whatsapp", authoritative_name=display_name)
                    if mem_engine
                    else ""
                )
                return await self.kernel.conversation_service.respond(
                    query, [], mem_ctx, user_name=display_name, user_id=user_id, channel="whatsapp"
                )
            return "Received your request."
        except TypeError:
            # Fallback if input_handler mock doesn't accept turn_origin kwarg yet
            res = await self.kernel.input_handler.process_query(query, speak=False, user_id=user_id, channel="whatsapp")
            if isinstance(res, dict):
                return res.get("response") or res.get("message") or "Done."
            return str(res)
        except Exception as e:
            logger.error(f"[WhatsApp] stage=general_query err={type(e).__name__}")
            return "I encountered an issue processing that request. Please try again."

    def archive_messages(self, items: List[Dict[str, Any]], *, user_id: Optional[str] = None) -> Dict[str, int]:
        """Backwards-compatible archival no-op (messages are stored on inbound webhook via DB)."""
        return {"archived": 0}
