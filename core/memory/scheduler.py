"""Proactive prospective-memory scheduler: due reminders, recurrence, snooze, quiet hours, morning briefings."""

import asyncio
from datetime import datetime, timedelta
import re
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.memory.models import (
    MemorySettings,
    MemoryStatus,
    MemoryType,
    ReminderItem,
    ReminderStatus,
    new_id,
)
from core.memory.safety import log_memory
from core.memory.store import MemoryStore


def _parse_hhmm(value: str, fallback: Tuple[int, int]) -> Tuple[int, int]:
    try:
        parts = str(value).strip().split(":")
        h = int(parts[0])
        m = int(parts[1]) if len(parts) > 1 else 0
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h, m
    except Exception:
        pass
    return fallback


def is_in_quiet_hours(now_ts: float, start_hhmm: str, end_hhmm: str) -> bool:
    """Return True if local wall-clock time at `now_ts` falls inside quiet hours window."""
    dt = datetime.fromtimestamp(now_ts)
    cur_mins = dt.hour * 60 + dt.minute
    sh, sm = _parse_hhmm(start_hhmm, (23, 0))
    eh, em = _parse_hhmm(end_hhmm, (7, 0))
    start_mins = sh * 60 + sm
    end_mins = eh * 60 + em

    if start_mins == end_mins:
        return False
    if start_mins < end_mins:
        return start_mins <= cur_mins < end_mins
    # Overnight wraparound (e.g. 23:00 -> 07:00)
    return cur_mins >= start_mins or cur_mins < end_mins


def compute_next_recurrence(due_at: float, recurrence: Optional[str], now_ts: float) -> Optional[float]:
    if not recurrence:
        return None
    rec = recurrence.strip().lower()
    step: Optional[timedelta] = None
    if rec == "hourly":
        step = timedelta(hours=1)
    elif rec == "daily":
        step = timedelta(days=1)
    elif rec == "weekly":
        step = timedelta(weeks=1)
    elif rec == "monthly":
        step = timedelta(days=30)
    else:
        m = re.match(r"^every\s+(\d+)\s*(min|mins|minute|minutes|hour|hours|day|days)$", rec)
        if m:
            qty = int(m.group(1))
            unit = m.group(2)
            if unit.startswith("min"):
                step = timedelta(minutes=qty)
            elif unit.startswith("h"):
                step = timedelta(hours=qty)
            else:
                step = timedelta(days=qty)
    if step is None:
        return None
    nxt = due_at + step.total_seconds()
    while nxt <= now_ts:
        nxt += step.total_seconds()
    return nxt


def parse_snooze_duration(text: str, default_seconds: int = 600) -> int:
    m = re.search(
        r"\b(\d+)\s*(sec|secs|second|seconds|min|mins|minute|minutes|hr|hrs|hour|hours)\b",
        text or "",
        flags=re.IGNORECASE,
    )
    if not m:
        return default_seconds
    qty = max(1, int(m.group(1)))
    unit = m.group(2).lower()
    if unit.startswith("sec"):
        return qty
    if unit.startswith("min"):
        return qty * 60
    return qty * 3600


class MemoryScheduler:
    """30-second proactive reminder, morning briefing, and consolidation scheduler."""

    def __init__(
        self,
        store: MemoryStore,
        consolidator=None,
        whatsapp_deliver: Optional[Callable[[str, str], Any]] = None,
        hud_deliver: Optional[Callable[[str, Dict[str, Any]], Any]] = None,
        voice_deliver: Optional[Callable[[str, str], Any]] = None,
        check_interval_s: float = 30.0,
        consolidation_interval_s: float = 6 * 3600.0,
    ):
        self.store = store
        self.consolidator = consolidator
        self.whatsapp_deliver = whatsapp_deliver
        self.hud_deliver = hud_deliver
        self.voice_deliver = voice_deliver
        self.check_interval_s = float(check_interval_s)
        self.consolidation_interval_s = float(consolidation_interval_s)

        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._last_briefing_date: Dict[str, str] = {}
        self._last_consolidation_ts: float = 0.0
        # user_id -> list of undelivered HUD notification dicts for polling/reconnect
        self.recent_notifications: Dict[str, List[Dict[str, Any]]] = {}

    def _deliver_notification(
        self,
        user_id: str,
        channel: str,
        text: str,
        reminder: Optional[ReminderItem] = None,
        kind: str = "reminder",
    ) -> bool:
        payload: Dict[str, Any] = {
            "type": kind,
            "user_id": user_id,
            "channel": channel,
            "text": text,
            "timestamp": time.time(),
            "speak": channel in ("hud", "voice"),
        }
        if reminder:
            payload["reminder"] = reminder.to_dict()

        bucket = self.recent_notifications.setdefault(user_id, [])
        bucket.append(payload)
        if len(bucket) > 30:
            del bucket[:-30]

        delivered = False
        if channel == "whatsapp" and self.whatsapp_deliver:
            try:
                res = self.whatsapp_deliver(user_id, text)
                if asyncio.iscoroutine(res):
                    try:
                        loop = asyncio.get_running_loop()
                        loop.create_task(res)
                        delivered = True
                    except RuntimeError:
                        pass
                else:
                    delivered = bool(res) if res is not None else True
            except Exception:
                delivered = False

        if self.hud_deliver:
            try:
                hud_ok = self.hud_deliver(user_id, payload)
                if channel in ("hud", "voice") or not delivered:
                    delivered = delivered or bool(hud_ok)
            except Exception:
                pass

        if channel == "voice" and self.voice_deliver:
            try:
                vres = self.voice_deliver(user_id, text)
                if asyncio.iscoroutine(vres):
                    try:
                        loop = asyncio.get_running_loop()
                        loop.create_task(vres)
                    except RuntimeError:
                        pass
                delivered = True
            except Exception:
                pass

        return delivered

    def tick(self, now: Optional[float] = None) -> Dict[str, int]:
        """Run one synchronous scheduler check pass (safe to call from async loop or unit tests)."""
        t0 = time.perf_counter()
        cur = now if now is not None else time.time()
        stats = {"checked": 0, "sent": 0, "deferred_quiet": 0, "briefings": 0}

        due = self.store.get_due_reminders(now=cur)
        stats["checked"] = len(due)

        for rem in due:
            settings: MemorySettings = self.store.get_memory_settings(rem.user_id)
            if not settings.memory_enabled:
                continue

            # Quiet hours guard (unless marked urgent)
            if not rem.urgent and is_in_quiet_hours(cur, settings.quiet_hours_start, settings.quiet_hours_end):
                stats["deferred_quiet"] += 1
                continue

            target_channel = rem.channel or settings.preferred_reminder_channel or "hud"
            msg = f"Reminder: {rem.text}"
            self._deliver_notification(
                user_id=rem.user_id,
                channel=target_channel,
                text=msg,
                reminder=rem,
                kind="reminder",
            )
            stats["sent"] += 1
            self.store.record_audit(rem.user_id, "reminder_fire", rem.created_from_memory_id)

            next_due = compute_next_recurrence(rem.due_at, rem.recurrence, cur)
            if next_due is not None:
                self.store.update_reminder(
                    rem.user_id,
                    rem.id,
                    status=ReminderStatus.PENDING.value,
                    due_at=next_due,
                )
            else:
                self.store.update_reminder(
                    rem.user_id,
                    rem.id,
                    status=ReminderStatus.SENT.value,
                )

        # Opt-in Morning Briefing check
        stats["briefings"] = self._check_morning_briefings(cur)

        # Periodic consolidation trigger
        if self.consolidator and (cur - self._last_consolidation_ts) >= self.consolidation_interval_s:
            try:
                self.consolidator.run_all()
                self._last_consolidation_ts = cur
            except Exception:
                pass

        if stats["sent"] or stats["deferred_quiet"] or stats["briefings"]:
            log_memory(
                "scheduler_tick",
                (time.perf_counter() - t0) * 1000,
                "ok",
                count=stats["sent"] + stats["briefings"],
            )
        return stats

    def build_morning_briefing(self, user_id: str, now: Optional[float] = None) -> Optional[str]:
        cur = now if now is not None else time.time()
        dt = datetime.fromtimestamp(cur)
        end_of_day = dt.replace(hour=23, minute=59, second=59, microsecond=0).timestamp()

        pending = self.store.list_reminders(user_id, status=ReminderStatus.PENDING.value, limit=20)
        today_rems = [r for r in pending if r.due_at <= end_of_day]

        commitments = [
            m
            for m in self.store.list_memories(
                user_id, mem_type=MemoryType.PROSPECTIVE.value, status=MemoryStatus.ACTIVE.value, limit=15
            )
        ]

        if not today_rems and not commitments:
            return None

        parts: List[str] = ["Good morning! Here's your brief update:"]
        if today_rems:
            parts.append("Due today: " + "; ".join(r.text for r in today_rems[:4]) + ".")
        rem_texts = {r.text.lower() for r in today_rems}
        open_comm = [
            (m.summary or m.content)
            for m in commitments
            if (m.summary or m.content).lower() not in rem_texts
        ][:3]
        if open_comm:
            parts.append("Open commitments: " + "; ".join(open_comm) + ".")
        return " ".join(parts)

    def _check_morning_briefings(self, cur_ts: float) -> int:
        sent = 0
        dt = datetime.fromtimestamp(cur_ts)
        today_str = dt.strftime("%Y-%m-%d")
        cur_mins = dt.hour * 60 + dt.minute

        for uid in self.store.get_distinct_user_ids():
            if self._last_briefing_date.get(uid) == today_str:
                continue
            settings = self.store.get_memory_settings(uid)
            if not settings.memory_enabled or not settings.morning_briefing_enabled:
                continue
            bh, bm = _parse_hhmm(settings.morning_briefing_time, (8, 0))
            target_mins = bh * 60 + bm
            if cur_mins < target_mins or cur_mins > target_mins + 120:
                continue
            if is_in_quiet_hours(cur_ts, settings.quiet_hours_start, settings.quiet_hours_end):
                continue
            brief = self.build_morning_briefing(uid, now=cur_ts)
            self._last_briefing_date[uid] = today_str
            if brief:
                pref = settings.preferred_reminder_channel or "hud"
                self._deliver_notification(uid, pref, brief, reminder=None, kind="morning_briefing")
                sent += 1
        return sent

    def snooze_reminder(
        self,
        user_id: str,
        reminder_id: Optional[str] = None,
        duration_seconds: int = 600,
        now: Optional[float] = None,
    ) -> Optional[ReminderItem]:
        cur = now if now is not None else time.time()
        target_id = reminder_id
        if not target_id:
            all_rems = self.store.list_reminders(user_id, limit=50)
            candidates = [r for r in all_rems if r.status in ("sent", "pending", "snoozed")]
            if not candidates:
                return None
            # Prefer most recently fired/updated sent or earliest pending reminder
            sent_list = [r for r in candidates if r.status == "sent"]
            if sent_list:
                sent_list.sort(key=lambda r: r.updated_at, reverse=True)
                target_id = sent_list[0].id
            else:
                target_id = candidates[0].id

        return self.store.update_reminder(
            user_id,
            target_id,
            status=ReminderStatus.SNOOZED.value,
            due_at=cur + max(10, int(duration_seconds)),
        )

    def start(self, loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
        if self._running:
            return
        self._running = True
        target_loop = loop
        if target_loop is None:
            try:
                target_loop = asyncio.get_running_loop()
            except RuntimeError:
                target_loop = None
        if target_loop is not None:
            self._task = target_loop.create_task(self._async_loop())

    async def _async_loop(self) -> None:
        while self._running:
            try:
                self.tick()
            except Exception:
                log_memory("scheduler_loop", 0.0, "error")
            await asyncio.sleep(self.check_interval_s)

    def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()

