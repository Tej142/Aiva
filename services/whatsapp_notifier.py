"""
Structurally isolated metadata-only WhatsApp inbound capture & notification engine.

Security invariants:
- NEVER queries or inspects `whatsapp_inbox.body`.
- NEVER imports LLM gateways, conversation services, or memory extractors.
- NEVER sends replies to unknown senders or exposes full phone numbers in logs.
"""

from __future__ import annotations

from datetime import datetime
import json
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional

from core.shared.logger import get_logger
from services.whatsapp_config import (
    WhatsAppConfig,
    get_wa_config,
    mask_phone,
    normalize_phone,
)

logger = get_logger("WhatsAppNotifier")

DEFAULT_DEBOUNCE_SECONDS = 120.0
DEFAULT_PURGE_INTERVAL_SECONDS = 1800.0


def _parse_hhmm(value: str, fallback_hour: int, fallback_min: int) -> int:
    raw = (value or "").strip()
    try:
        parts = raw.split(":", 1)
        hh = max(0, min(23, int(parts[0])))
        mm = max(0, min(59, int(parts[1]))) if len(parts) > 1 else 0
        return hh * 60 + mm
    except (TypeError, ValueError, IndexError):
        return fallback_hour * 60 + fallback_min


def is_quiet_hours_now(
    cfg: Optional[WhatsAppConfig] = None,
    now_dt: Optional[datetime] = None,
) -> bool:
    """Return True if local wall-clock time falls inside quiet hours [start, end)."""
    conf = cfg or get_wa_config()
    start_m = _parse_hhmm(conf.quiet_hours_start, 23, 0)
    end_m = _parse_hhmm(conf.quiet_hours_end, 7, 0)
    if start_m == end_m:
        return False

    cur = now_dt or datetime.now()
    cur_m = cur.hour * 60 + cur.minute
    if start_m < end_m:
        return start_m <= cur_m < end_m
    return cur_m >= start_m or cur_m < end_m


def _sanitize_sender_label(label: str, phone: str) -> str:
    clean = " ".join(str(label or "").replace("\r", " ").replace("\n", " ").split())
    if not clean:
        return mask_phone(phone)
    clean = clean.replace("<", "").replace(">", "")
    return clean[:50] or mask_phone(phone)


def _map_inbox_kind(media_type: str) -> str:
    mt = str(media_type or "text").strip().lower()
    if mt in ("audio", "voice", "ptt"):
        return "voice"
    if mt in ("text", "image", "document"):
        return mt
    if mt == "video":
        return "other"
    return "text"


class WhatsAppInboundNotifier:
    """
    Captures third-party 1:1 inbound WhatsApp messages into `whatsapp_inbox` and
    schedules debounced, metadata-only alerts without ever reading message bodies.
    """

    def __init__(
        self,
        db: Any,
        send_raw_fn: Optional[Callable[[str, str, Optional[str], Optional[str]], Awaitable[Dict[str, Any]]]] = None,
        rate_check_fn: Optional[Callable[[str], bool]] = None,
        debounce_seconds: float = DEFAULT_DEBOUNCE_SECONDS,
    ):
        self.db = db
        self.send_raw_fn = send_raw_fn
        self.rate_check_fn = rate_check_fn
        self.debounce_seconds = float(debounce_seconds)
        self._last_purge_at: float = 0.0

    def capture_third_party_message(
        self,
        payload: Dict[str, Any],
        cfg: Optional[WhatsAppConfig] = None,
        now_ts: Optional[float] = None,
        now_dt: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """
        Capture a 1:1 inbound message from a non-user sender into `whatsapp_inbox`.
        Unknown senders are dropped immediately without storing message text.
        """
        conf = cfg or get_wa_config()
        now = float(now_ts if now_ts is not None else time.time())

        if payload.get("from_me"):
            return {"status": "ignored_echo"}

        chat_jid = str(payload.get("chat_jid") or "").strip()
        if chat_jid.endswith("@g.us") or chat_jid == "status@broadcast" or chat_jid.endswith("@broadcast"):
            return {"status": "ignored_non_direct"}

        sender_raw = str(payload.get("sender") or "")
        sender_phone = normalize_phone(sender_raw.split("@")[0] if "@" in sender_raw else sender_raw)

        if not sender_phone or (conf.aiva_whatsapp_number and sender_phone == conf.aiva_whatsapp_number):
            return {"status": "ignored_own_or_invalid"}

        message_id = str(payload.get("message_id") or "").strip()
        media_type = str(payload.get("media_type") or "text").strip().lower() or "text"
        inbox_kind = _map_inbox_kind(media_type)
        media_mime = str(payload.get("media_mime") or "").strip()
        media_filename = str(payload.get("media_filename") or "").strip()
        media_seconds = int(payload.get("media_seconds") or 0)
        body = str(payload.get("text") or "").strip()
        push_name = str(payload.get("push_name") or "").strip()
        msg_ts = float(payload.get("timestamp") or now)

        # Metadata reference for lazy bridge fetch / read receipt (never eager media download)
        media_ref_meta = json.dumps({
            "jid": chat_jid or f"{sender_phone}@s.whatsapp.net",
            "seconds": media_seconds,
        })

        # Resolve saved contacts matching this sender across active verified users
        raw_matches = self.db.resolve_contacts_by_phone(sender_phone)
        matches = [
            c for c in (raw_matches or [])
            if c.get("gate_state", "active") == "active"
        ]

        if not matches and conf.unknown_sender_user_email:
            fallback_user = self.db.get_user_by_email(conf.unknown_sender_user_email)
            if (
                fallback_user
                and fallback_user.get("is_active", 1)
                and fallback_user.get("gate_state", "active") == "active"
            ):
                matches = [{
                    "id": "",
                    "user_id": fallback_user["id"],
                    "display_name": _sanitize_sender_label(push_name, sender_phone),
                    "notify_inbound": False,
                    "priority": False,
                    "is_unknown_fallback": True,
                }]

        if not matches:
            self.db.record_unknown_sender_event(sender_phone[-4:], kind=inbox_kind, now=now)
            logger.info(f"[WhatsApp] stage=inbound_unknown sender={mask_phone(sender_phone)} status=dropped")
            return {"status": "dropped_unknown_sender"}

        captured_users: List[str] = []
        quiet_now = is_quiet_hours_now(conf, now_dt=now_dt)

        for contact in matches:
            uid = contact["user_id"]
            contact_id = str(contact.get("id") or "")
            sender_label = _sanitize_sender_label(contact.get("display_name") or push_name, sender_phone)
            inserted = self.db.insert_whatsapp_inbox_message(
                user_id=uid,
                contact_id=contact_id,
                sender_e164=sender_phone,
                sender_name=sender_label,
                message_id=message_id,
                received_at=msg_ts,
                kind=inbox_kind,
                body=body,
                media_ref=media_ref_meta,
                filename=media_filename,
                mime_type=media_mime,
                status="unread",
            )
            if not inserted:
                continue  # Duplicate message_id

            captured_users.append(uid)

            # Skip alerts when contact is muted or fallback unknown capture
            if not bool(contact.get("notify_inbound", True)) or not contact_id:
                continue

            is_priority = bool(contact.get("priority", False))
            if quiet_now and not is_priority:
                self.db.enqueue_pending_wa_notification(
                    user_id=uid,
                    contact_id=contact_id,
                    sender_name=sender_label,
                    kind=inbox_kind,
                    received_at=msg_ts,
                    ready_at=now + self.debounce_seconds,
                    is_priority=False,
                    held_for_quiet=True,
                )
            else:
                delay = 0.0 if is_priority else self.debounce_seconds
                self.db.enqueue_pending_wa_notification(
                    user_id=uid,
                    contact_id=contact_id,
                    sender_name=sender_label,
                    kind=inbox_kind,
                    received_at=msg_ts,
                    ready_at=now + delay,
                    is_priority=is_priority,
                    held_for_quiet=False,
                )

        if not captured_users:
            return {"status": "ignored_duplicate"}

        logger.info(
            f"[WhatsApp] stage=inbox_captured sender={mask_phone(sender_phone)} "
            f"owners={len(captured_users)} kind={inbox_kind} status=ok"
        )
        return {
            "status": "captured_inbox",
            "owners_count": len(captured_users),
        }

    async def notify_user(
        self,
        user_id: str,
        text: str,
        urgency: str = "normal",
        *,
        bypass_quiet: bool = False,
        open_reply_scope: str = "",
        cfg: Optional[WhatsAppConfig] = None,
        now_dt: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """
        Send a proactive WhatsApp notification to a user's verified personal number.
        Honors gate_state=='active', quiet hours (unless urgency=='urgent'),
        hourly rate caps, and optional scoped reply windows.
        """
        conf = cfg or get_wa_config()
        clean_text = (text or "").strip()
        if not user_id or not clean_text:
            return {"status": "error", "error": "invalid_args"}

        user = self.db.get_user_by_id(user_id)
        if not user or not user.get("is_active", 1) or user.get("gate_state", "active") != "active":
            return {"status": "blocked_user_gate"}

        link = self.db.get_verified_link_for_user(user_id)
        if not link or not link.get("phone_e164"):
            return {"status": "no_verified_number"}

        is_urgent = str(urgency or "normal").strip().lower() == "urgent" or bypass_quiet
        if not is_urgent and is_quiet_hours_now(conf, now_dt=now_dt):
            logger.info(f"[WhatsApp] stage=notify_user user={user_id} status=deferred_quiet_hours")
            return {"status": "deferred_quiet_hours"}

        if self.rate_check_fn and not self.rate_check_fn(user_id):
            logger.warning(f"[WhatsApp] stage=notify_user user={user_id} status=rate_limited")
            return {"status": "rate_limit_exceeded"}

        if not self.send_raw_fn:
            return {"status": "error", "error": "send_fn_unavailable"}

        res = await self.send_raw_fn(
            link["phone_e164"],
            clean_text,
            user_id,
            link.get("wa_jid") or None,
        )
        if res.get("status") == "success" and open_reply_scope:
            self.db.set_whatsapp_reply_window(
                user_id,
                open_reply_scope,
                duration_minutes=max(1, int(conf.reply_window_minutes)),
            )
        return res

    async def flush_due_notifications(
        self,
        now_ts: Optional[float] = None,
        now_dt: Optional[datetime] = None,
        cfg: Optional[WhatsAppConfig] = None,
    ) -> Dict[str, int]:
        """Deliver held quiet-hours digests (when quiet hours end) and debounced inbox alerts."""
        conf = cfg or get_wa_config()
        now = float(now_ts if now_ts is not None else time.time())
        stats = {"digests_sent": 0, "alerts_sent": 0, "purged": 0}

        quiet_now = is_quiet_hours_now(conf, now_dt=now_dt)

        # 1. Deliver morning digest once quiet hours end
        if not quiet_now:
            for uid in self.db.get_held_quiet_user_ids():
                held_rows = self.db.pop_held_quiet_wa_notifications(uid, now=now)
                if not held_rows:
                    continue
                total_count = sum(int(r.get("count") or 1) for r in held_rows)
                parts = []
                for r in held_rows[:6]:
                    cnt = int(r.get("count") or 1)
                    sname = _sanitize_sender_label(r.get("sender_name") or "", "")
                    parts.append(f"{cnt} from {sname}")
                summary_str = ", ".join(parts)
                if len(held_rows) > 6:
                    summary_str += f", +{len(held_rows) - 6} more"
                digest_text = (
                    f"Good morning. While quiet hours were active, you received {total_count} "
                    f"WhatsApp message(s) ({summary_str}).\n"
                    f"Reply 'read' to check them, or 'ignore' to skip."
                )
                res = await self.notify_user(
                    uid,
                    digest_text,
                    urgency="normal",
                    open_reply_scope="inbox_prompt:all",
                    cfg=conf,
                    now_dt=now_dt,
                )
                if res.get("status") == "success":
                    stats["digests_sent"] += 1

        # 2. Deliver debounced non-quiet (or priority) inbox alerts
        due_rows = self.db.pop_due_wa_notifications(now=now)
        for row in due_rows:
            uid = row["user_id"]
            cid = row.get("contact_id") or ""
            contact = self.db.get_whatsapp_contact_by_id(uid, cid) if cid else None
            if contact and not bool(contact.get("notify_inbound", True)):
                continue

            is_priority = bool(row.get("is_priority")) or bool(contact and contact.get("priority"))
            sname = _sanitize_sender_label(
                (contact or {}).get("display_name") or row.get("sender_name") or "",
                (contact or {}).get("phone_e164") or "",
            )
            count = int(row.get("count") or 1)
            latest_kind = str(row.get("latest_kind") or "text")
            if count == 1 and latest_kind == "voice":
                prompt_text = (
                    f"New voice note from *{sname}* on WhatsApp.\n"
                    f"Reply 'read' to transcribe and read it, or 'ignore' to skip."
                )
            else:
                msg_word = "message" if count == 1 else "messages"
                prompt_text = (
                    f"You have {count} unread WhatsApp {msg_word} from *{sname}*.\n"
                    f"Reply 'read' to read them, or 'ignore' to skip."
                )

            scope_target = cid or sname
            res = await self.notify_user(
                uid,
                prompt_text,
                urgency="urgent" if is_priority else "normal",
                bypass_quiet=is_priority,
                open_reply_scope=f"inbox_prompt:{scope_target}",
                cfg=conf,
                now_dt=now_dt,
            )
            if res.get("status") == "success":
                stats["alerts_sent"] += 1

        stats["purged"] = self.purge_expired(now_ts=now, cfg=conf)
        return stats

    def purge_expired(
        self,
        now_ts: Optional[float] = None,
        cfg: Optional[WhatsAppConfig] = None,
        force: bool = False,
    ) -> int:
        conf = cfg or get_wa_config()
        now = float(now_ts if now_ts is not None else time.time())
        if not force and (now - self._last_purge_at) < DEFAULT_PURGE_INTERVAL_SECONDS:
            return 0
        self._last_purge_at = now
        return int(self.db.purge_expired_whatsapp_inbox(retention_days=conf.inbox_retention_days, now=now))
