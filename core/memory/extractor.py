"""Working-memory session buffer, hot-path explicit commands, and async LLM extractor."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
import json
import queue
import re
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.memory.models import (
    EntityItem,
    MemoryItem,
    MemorySource,
    MemoryStatus,
    MemoryType,
    ReminderItem,
    ReminderStatus,
    VALID_ENTITY_KINDS,
    WorkingTurn,
    estimate_tokens,
    new_id,
    normalize_text,
)
from core.memory.safety import (
    classify_non_name_identity,
    contains_sensitive_data,
    log_memory,
    reclassify_invalid_name_memory,
)
from core.memory.store import MemoryStore


@dataclass
class ExtractionJob:
    user_id: str
    channel: str
    turns: List[WorkingTurn]
    enqueued_at: float = field(default_factory=time.time)
    attempt: int = 0
    next_retry_at: float = 0.0


class _RateLimitError(RuntimeError):
    """Raised when all LLM providers hit rate limit / quota errors."""


def parse_natural_due_and_recurrence(
    text: str,
    now: Optional[float] = None,
) -> Tuple[Optional[float], Optional[str], str, bool]:
    """Parse due timestamp and recurrence from text.

    Returns `(due_at_epoch, recurrence, clean_task_text, urgent)`.
    """
    cur_ts = now if now is not None else time.time()
    base_dt = datetime.fromtimestamp(cur_ts)
    raw = (text or "").strip()
    urgent = bool(re.search(r"\b(urgent|asap|immediately|emergency|critical)\b", raw, re.I))

    recurrence: Optional[str] = None
    due_at: Optional[float] = None
    cleaned = raw

    # Strip leading "remind me (to)?" prefix
    cleaned = re.sub(
        r"^(?:please\s+)?remind\s+me\s+(?:to\s+)?",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip()

    # Check recurrence phrases
    if re.search(r"\bevery\s+(?:day|morning|night|evening)\b|\bdaily\b", cleaned, re.I):
        recurrence = "daily"
        cleaned = re.sub(r"\b(?:every\s+(?:day|morning|night|evening)|daily)\b", "", cleaned, flags=re.I).strip()
    elif re.search(r"\bweekly\b|\bevery\s+week\b|\bevery\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", cleaned, re.I):
        recurrence = "weekly"
        cleaned = re.sub(r"\b(?:weekly|every\s+week)\b", "", cleaned, flags=re.I).strip()
    elif re.search(r"\bhourly\b|\bevery\s+hour\b", cleaned, re.I):
        recurrence = "hourly"
        cleaned = re.sub(r"\b(?:hourly|every\s+hour)\b", "", cleaned, flags=re.I).strip()
    elif re.search(r"\bmonthly\b|\bevery\s+month\b", cleaned, re.I):
        recurrence = "monthly"
        cleaned = re.sub(r"\b(?:monthly|every\s+month)\b", "", cleaned, flags=re.I).strip()

    # Pattern 1: "in N minute(s) / hour(s) / day(s) / second(s)"
    rel_match = re.search(
        r"\bin\s+(\d+)\s+(seconds?|secs?|minutes?|mins?|hours?|hrs?|days?)\b",
        cleaned,
        flags=re.IGNORECASE,
    )
    if rel_match:
        qty = int(rel_match.group(1))
        unit = rel_match.group(2).lower()
        if unit.startswith("sec"):
            due_at = cur_ts + qty
        elif unit.startswith("min"):
            due_at = cur_ts + qty * 60
        elif unit.startswith("h"):
            due_at = cur_ts + qty * 3600
        elif unit.startswith("d"):
            due_at = cur_ts + qty * 86400
        cleaned = (cleaned[: rel_match.start()] + cleaned[rel_match.end() :]).strip()

    # Pattern 2: "tomorrow (at HH(:MM)? (am|pm)?)?" or "today at HH..."
    if due_at is None:
        day_offset = 0
        has_day_word = False
        if re.search(r"\btomorrow\b", cleaned, re.I):
            day_offset = 1
            has_day_word = True
            cleaned = re.sub(r"\btomorrow\b", "", cleaned, flags=re.I).strip()
        elif re.search(r"\btonight\b", cleaned, re.I):
            has_day_word = True
            due_at = base_dt.replace(hour=20, minute=0, second=0, microsecond=0).timestamp()
            if due_at <= cur_ts:
                due_at += 86400
            cleaned = re.sub(r"\btonight\b", "", cleaned, flags=re.I).strip()
        elif re.search(r"\btoday\b", cleaned, re.I):
            has_day_word = True
            cleaned = re.sub(r"\btoday\b", "", cleaned, flags=re.I).strip()

        time_match = re.search(
            r"\b(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b",
            cleaned,
            flags=re.IGNORECASE,
        )
        if time_match and (time_match.group(3) or ":" in time_match.group(0) or "at " in time_match.group(0).lower() or has_day_word):
            hour = int(time_match.group(1))
            minute = int(time_match.group(2) or 0)
            ampm = (time_match.group(3) or "").lower()
            if ampm == "pm" and hour < 12:
                hour += 12
            elif ampm == "am" and hour == 12:
                hour = 0
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                target_dt = (base_dt + timedelta(days=day_offset)).replace(
                    hour=hour, minute=minute, second=0, microsecond=0
                )
                if day_offset == 0 and target_dt.timestamp() <= cur_ts:
                    target_dt += timedelta(days=1)
                due_at = target_dt.timestamp()
                cleaned = (cleaned[: time_match.start()] + cleaned[time_match.end() :]).strip()
        elif has_day_word and due_at is None:
            target_dt = (base_dt + timedelta(days=day_offset)).replace(
                hour=9, minute=0, second=0, microsecond=0
            )
            due_at = target_dt.timestamp()

    # Clean dangling connectors ("to", "about", "on", "at")
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .,:;-")
    cleaned = re.sub(r"^(?:to|that|about)\s+", "", cleaned, flags=re.I).strip()
    cleaned = re.sub(r"\s+(?:at|on|by)$", "", cleaned, flags=re.I).strip()
    return due_at, recurrence, cleaned or raw, urgent


def _is_rate_limit_error(err: Exception) -> bool:
    msg = str(err).lower()
    return any(
        tok in msg
        for tok in ("429", "rate_limit", "rate limit", "too many requests", "quota", "resource_exhausted")
    )


class MemoryExtractor:
    """Manages session working buffer, hot-path explicit memory, and async LLM batch extraction."""

    def __init__(
        self,
        store: MemoryStore,
        on_memory_created: Optional[Callable[[str, MemoryItem], None]] = None,
        llm_call: Optional[Callable[[List[Dict[str, str]]], str]] = None,
        batch_size: int = 4,
        idle_flush_s: float = 30.0,
        working_token_cap: int = 1200,
        start_worker: bool = True,
    ):
        self.store = store
        self._on_memory_created = on_memory_created
        self._custom_llm_call = llm_call
        self.batch_size = max(1, int(batch_size))
        self.idle_flush_s = float(idle_flush_s)
        self.working_token_cap = int(working_token_cap)

        self._buf_lock = threading.RLock()
        # (user_id, session_id) -> {"turns": [WorkingTurn], "summary": str, "last_ts": float, "pending_extract": [WorkingTurn]}
        self._buffers: Dict[Tuple[str, str], Dict[str, Any]] = {}
        # user_id -> {"memory_id": str, "text": str, "asked": bool}
        self._pending_commitment_due: Dict[str, Dict[str, Any]] = {}

        self._queue: queue.Queue = queue.Queue()
        self._deferred: List[ExtractionJob] = []
        self._running = start_worker
        self._worker: Optional[threading.Thread] = None
        self._secrets: Optional[Dict[str, Any]] = None

        if start_worker:
            self._worker = threading.Thread(
                target=self._worker_loop,
                name="aiva-memory-extractor",
                daemon=True,
            )
            self._worker.start()

    def stop(self) -> None:
        self._running = False
        if self._worker and self._worker.is_alive():
            self._queue.put(None)
            self._worker.join(timeout=1.5)

    # ------------------------------------------------------------------
    # WORKING BUFFER & SESSION CAPTURE
    # ------------------------------------------------------------------

    def append_turn(
        self,
        user_id: str,
        role: str,
        content: str,
        channel: str = "hud",
        session_id: str = "default",
        enqueue_extraction: bool = True,
    ) -> Optional[MemoryItem]:
        """Append a turn to token-capped per-session working buffer and persist non-sensitive working turn."""
        t0 = time.perf_counter()
        if not user_id or not content or not content.strip():
            return None

        settings = self.store.get_memory_settings(user_id)
        if not settings.memory_enabled:
            return None

        sensitive, cat = contains_sensitive_data(content)
        if sensitive:
            log_memory("capture", (time.perf_counter() - t0) * 1000, "blocked", user_id=user_id, reason=cat)
            return None

        now = time.time()
        turn = WorkingTurn(
            role=role,
            content=content.strip(),
            channel=channel or "hud",
            timestamp=now,
            session_id=session_id or "default",
        )

        # Persist to SQLite working memory so `/api/history` stays intact
        source = MemorySource.SYSTEM.value if role == "assistant" else MemorySource.USER_SAID.value
        w_item = MemoryItem(
            id=new_id(),
            user_id=user_id,
            type=MemoryType.WORKING.value,
            content=f"{role}: {turn.content}",
            summary=turn.content[:180],
            entities=[],
            importance=0.35,
            confidence=0.9,
            source=source,
            channel=channel if channel in ("voice", "hud", "whatsapp") else "hud",
            created_at=now,
            last_accessed=now,
            status=MemoryStatus.ACTIVE.value,
        )
        ok, _ = self.store.insert_memory(w_item, is_verified_user=True, skip_sensitive_check=True)

        # Update token-capped in-memory rolling buffer & check batch extraction
        if not settings.learning_paused and enqueue_extraction:
            with self._buf_lock:
                key = (user_id, session_id or "default")
                buf = self._buffers.setdefault(
                    key,
                    {"turns": [], "summary": "", "last_ts": now, "pending_extract": []},
                )
                buf["turns"].append(turn)
                buf["last_ts"] = now
                if role == "user":
                    buf["pending_extract"].append(turn)

                # Enforce token cap with rolling summary of oldest turns
                total_toks = estimate_tokens(buf["summary"]) + sum(
                    estimate_tokens(t.content) for t in buf["turns"]
                )
                while total_toks > self.working_token_cap and len(buf["turns"]) > 2:
                    evicted = buf["turns"].pop(0)
                    snippet = f"{evicted.role}: {evicted.content[:120]}"
                    buf["summary"] = (buf["summary"] + " | " + snippet).strip(" |")[-600:]
                    total_toks = estimate_tokens(buf["summary"]) + sum(
                        estimate_tokens(t.content) for t in buf["turns"]
                    )

                if len(buf["pending_extract"]) >= self.batch_size:
                    batch = list(buf["pending_extract"])
                    buf["pending_extract"].clear()
                    self._queue.put(ExtractionJob(user_id=user_id, channel=channel, turns=batch))

        log_memory("capture", (time.perf_counter() - t0) * 1000, "ok", user_id=user_id, channel=channel)
        return w_item if ok else None

    def get_working_context(self, user_id: str, session_id: str = "default", limit: int = 8) -> List[Dict[str, str]]:
        settings = self.store.get_memory_settings(user_id)
        if not settings.memory_enabled:
            return []
        key = (user_id, session_id or "default")
        with self._buf_lock:
            buf = self._buffers.get(key)
            if buf and buf["turns"]:
                msgs: List[Dict[str, str]] = []
                if buf["summary"]:
                    msgs.append({"role": "system", "content": f"Earlier session summary: {buf['summary']}"})
                for t in buf["turns"][-limit:]:
                    msgs.append({"role": "assistant" if t.role == "assistant" else "user", "content": t.content})
                return msgs

        # Fallback to persisted working turns from SQLite
        items = self.store.get_working_turns(user_id, limit=limit)
        out: List[Dict[str, str]] = []
        for item in items:
            if item.content.startswith("assistant: "):
                out.append({"role": "assistant", "content": item.content[len("assistant: ") :]})
            elif item.content.startswith("aiva: "):
                out.append({"role": "assistant", "content": item.content[len("aiva: ") :]})
            elif item.content.startswith("user: "):
                out.append({"role": "user", "content": item.content[len("user: ") :]})
            else:
                out.append({"role": "user", "content": item.content})
        return out

    # ------------------------------------------------------------------
    # PENDING COMMITMENT DUE-TIME PROMPT ("ASK ONCE INSTEAD OF GUESSING")
    # ------------------------------------------------------------------

    def get_pending_commitment_question(self, user_id: str) -> Optional[str]:
        with self._buf_lock:
            entry = self._pending_commitment_due.get(user_id)
            if not entry or entry.get("asked"):
                return None
            entry["asked"] = True
            return f"When should I remind you to {entry['text']}?"

    def try_resolve_pending_commitment_time(
        self,
        user_id: str,
        user_text: str,
        channel: str = "hud",
    ) -> Optional[str]:
        with self._buf_lock:
            entry = self._pending_commitment_due.get(user_id)
            if not entry:
                return None
        due_at, recurrence, _, urgent = parse_natural_due_and_recurrence(user_text)
        if due_at is None:
            return None
        with self._buf_lock:
            entry = self._pending_commitment_due.pop(user_id, None)
        if not entry:
            return None
        rem = ReminderItem(
            id=new_id(),
            user_id=user_id,
            text=entry["text"],
            due_at=due_at,
            recurrence=recurrence,
            channel=channel or "hud",
            status=ReminderStatus.PENDING.value,
            created_from_memory_id=entry.get("memory_id"),
            urgent=urgent,
        )
        self.store.create_reminder(rem)
        when_str = datetime.fromtimestamp(due_at).strftime("%Y-%m-%d %H:%M")
        return f"Got it, I've scheduled your reminder '{entry['text']}' for {when_str}."

    # ------------------------------------------------------------------
    # HOT-PATH EXPLICIT COMMANDS (<10 ms)
    # ------------------------------------------------------------------

    def handle_explicit_remember(
        self,
        user_id: str,
        text: str,
        channel: str = "hud",
    ) -> Dict[str, Any]:
        """Synchronously process explicit `'remember that ...'` commands in <10 ms."""
        t0 = time.perf_counter()
        settings = self.store.get_memory_settings(user_id)
        if not settings.memory_enabled:
            return {
                "handled": True,
                "stored": False,
                "response": "Memory is currently turned off in your settings.",
            }

        raw = (text or "").strip()
        cleaned = re.sub(
            r"^(?:please\s+)?(?:remember\s+(?:that\s+)?|don'?t\s+forget\s+(?:that\s+)?|save\s+this\s*:?\s*|keep\s+in\s+mind\s+(?:that\s+)?)",
            "",
            raw,
            flags=re.IGNORECASE,
        ).strip(" .!?")

        if not cleaned or cleaned.lower() in {"this", "something", "that", "my name", "what i like"}:
            return {
                "handled": True,
                "stored": False,
                "response": "Sure, what would you like me to remember?",
            }

        sensitive, cat = contains_sensitive_data(cleaned)
        if sensitive:
            log_memory("hot_remember", (time.perf_counter() - t0) * 1000, "blocked", user_id=user_id, reason=cat)
            return {
                "handled": True,
                "stored": False,
                "reason": f"sensitive_{cat}",
                "response": "For your security, I don't store passwords, OTPs, API keys, card numbers, or IDs.",
            }

        # Check if this explicit statement is actually a timed commitment
        if re.search(r"\b(?:remind|appointment|meeting|deadline|due)\b", cleaned, re.I):
            due_at, recurrence, task_text, urgent = parse_natural_due_and_recurrence(cleaned)
            mem = MemoryItem(
                id=new_id(),
                user_id=user_id,
                type=MemoryType.PROSPECTIVE.value,
                content=cleaned,
                summary=cleaned[:180],
                entities=[],
                importance=0.8,
                confidence=0.95,
                source=MemorySource.USER_SAID.value,
                channel=channel if channel in ("voice", "hud", "whatsapp") else "hud",
            )
            self.store.insert_memory(mem)
            if self._on_memory_created:
                self._on_memory_created(user_id, mem)
            if due_at is not None:
                rem = ReminderItem(
                    id=new_id(),
                    user_id=user_id,
                    text=task_text,
                    due_at=due_at,
                    recurrence=recurrence,
                    channel=channel or "hud",
                    status=ReminderStatus.PENDING.value,
                    created_from_memory_id=mem.id,
                    urgent=urgent,
                )
                self.store.create_reminder(rem)
                when_str = datetime.fromtimestamp(due_at).strftime("%Y-%m-%d %H:%M")
                log_memory("hot_remember", (time.perf_counter() - t0) * 1000, "ok", user_id=user_id, type="prospective")
                return {
                    "handled": True,
                    "stored": True,
                    "memory_id": mem.id,
                    "reminder_id": rem.id,
                    "response": f"Got it, I'll remember and remind you to {task_text} at {when_str}.",
                }
            # No due time -> ask ONCE instead of guessing
            with self._buf_lock:
                self._pending_commitment_due[user_id] = {
                    "memory_id": mem.id,
                    "text": task_text,
                    "asked": True,
                }
            log_memory("hot_remember", (time.perf_counter() - t0) * 1000, "ok", user_id=user_id, type="prospective")
            return {
                "handled": True,
                "stored": True,
                "memory_id": mem.id,
                "needs_due_time": True,
                "response": f"Got it, I've noted '{task_text}'. What time should I remind you?",
            }

        reclassified_content, reclassified_summary, was_reclassified, _ = reclassify_invalid_name_memory(
            cleaned, cleaned[:180]
        )
        mem = MemoryItem(
            id=new_id(),
            user_id=user_id,
            type=MemoryType.SEMANTIC.value,
            content=reclassified_content,
            summary=reclassified_summary if was_reclassified else cleaned[:180],
            entities=[],
            importance=0.75 if was_reclassified else 0.85,
            confidence=0.98,
            source=MemorySource.USER_SAID.value,
            channel=channel if channel in ("voice", "hud", "whatsapp") else "hud",
        )
        ok, err = self.store.insert_memory(mem)
        if ok and self._on_memory_created:
            self._on_memory_created(user_id, mem)
        log_memory("hot_remember", (time.perf_counter() - t0) * 1000, "ok" if ok else "rejected", user_id=user_id, reason=err)
        if not ok:
            return {"handled": True, "stored": False, "response": "I couldn't store that safely."}
        return {
            "handled": True,
            "stored": True,
            "memory_id": mem.id,
            "response": "Got it, I'll remember that.",
        }

    def handle_explicit_reminder(
        self,
        user_id: str,
        text: str,
        channel: str = "hud",
    ) -> Dict[str, Any]:
        """Synchronously process explicit `'remind me ...'` requests in <10 ms."""
        t0 = time.perf_counter()
        settings = self.store.get_memory_settings(user_id)
        if not settings.memory_enabled:
            return {
                "handled": True,
                "stored": False,
                "response": "Memory is currently turned off in your settings.",
            }

        sensitive, cat = contains_sensitive_data(text)
        if sensitive:
            log_memory("hot_reminder", (time.perf_counter() - t0) * 1000, "blocked", user_id=user_id, reason=cat)
            return {
                "handled": True,
                "stored": False,
                "response": "For your security, I can't create reminders containing passwords, OTPs, keys, or IDs.",
            }

        due_at, recurrence, task_text, urgent = parse_natural_due_and_recurrence(text)
        mem = MemoryItem(
            id=new_id(),
            user_id=user_id,
            type=MemoryType.PROSPECTIVE.value,
            content=task_text,
            summary=task_text[:180],
            importance=0.85,
            confidence=0.95,
            source=MemorySource.USER_SAID.value,
            channel=channel if channel in ("voice", "hud", "whatsapp") else "hud",
        )
        self.store.insert_memory(mem)
        if self._on_memory_created:
            self._on_memory_created(user_id, mem)

        if due_at is None:
            with self._buf_lock:
                self._pending_commitment_due[user_id] = {
                    "memory_id": mem.id,
                    "text": task_text,
                    "asked": True,
                }
            log_memory("hot_reminder", (time.perf_counter() - t0) * 1000, "needs_time", user_id=user_id)
            return {
                "handled": True,
                "stored": True,
                "memory_id": mem.id,
                "needs_due_time": True,
                "response": f"Got it. When should I remind you to {task_text}?",
            }

        pref_chan = (
            "whatsapp"
            if re.search(r"\bwhatsapp\b", text, re.I)
            else (channel or settings.preferred_reminder_channel or "hud")
        )
        rem = ReminderItem(
            id=new_id(),
            user_id=user_id,
            text=task_text,
            due_at=due_at,
            recurrence=recurrence,
            channel=pref_chan,
            status=ReminderStatus.PENDING.value,
            created_from_memory_id=mem.id,
            urgent=urgent,
        )
        self.store.create_reminder(rem)
        when_str = datetime.fromtimestamp(due_at).strftime("%Y-%m-%d %H:%M")
        rec_note = f" ({recurrence})" if recurrence else ""
        log_memory("hot_reminder", (time.perf_counter() - t0) * 1000, "ok", user_id=user_id)
        return {
            "handled": True,
            "stored": True,
            "memory_id": mem.id,
            "reminder_id": rem.id,
            "response": f"Reminder set for {when_str}{rec_note}: {task_text}.",
        }

    # ------------------------------------------------------------------
    # BACKGROUND WORKER & STRICT-JSON LLM EXTRACTION WITH BACKOFF
    # ------------------------------------------------------------------

    def _load_secrets(self) -> Dict[str, Any]:
        if self._secrets is not None:
            return self._secrets
        try:
            with open("config/secrets.json", "r", encoding="utf-8") as fh:
                self._secrets = json.load(fh)
        except Exception:
            self._secrets = {}
        return self._secrets

    def _call_llm_pool(self, messages: List[Dict[str, str]]) -> str:
        """Call existing multi-LLM pool (Groq -> Mistral -> Gemini) or test hook."""
        if self._custom_llm_call is not None:
            return self._custom_llm_call(messages)

        secrets = self._load_secrets()
        saw_rate_limit = False

        # 1. Groq pool
        groq_keys = list(secrets.get("groq_api_keys") or [])
        if secrets.get("groq_api_key") and secrets["groq_api_key"] not in groq_keys:
            groq_keys.append(secrets["groq_api_key"])
        if groq_keys:
            try:
                from groq import Groq
                for key in groq_keys:
                    try:
                        resp = Groq(api_key=key).chat.completions.create(
                            model="openai/gpt-oss-20b",
                            messages=messages,
                            temperature=0.1,
                            max_completion_tokens=700,
                        )
                        text = resp.choices[0].message.content
                        if text:
                            return text
                    except Exception as e:
                        if _is_rate_limit_error(e):
                            saw_rate_limit = True
                            continue
            except Exception:
                pass

        # 2. Mistral pool
        mistral_keys = list(secrets.get("mistral_api_keys") or [])
        if secrets.get("mistral_api_key") and secrets["mistral_api_key"] not in mistral_keys:
            mistral_keys.append(secrets["mistral_api_key"])
        if mistral_keys:
            try:
                from mistralai import Mistral
                for key in mistral_keys:
                    try:
                        resp = Mistral(api_key=key).chat.complete(
                            model="mistral-small-latest",
                            messages=messages,
                        )
                        text = resp.choices[0].message.content
                        if text:
                            return text
                    except Exception as e:
                        if _is_rate_limit_error(e):
                            saw_rate_limit = True
                            continue
            except Exception:
                pass

        # 3. Gemini pool
        gemini_keys = list(secrets.get("gemini_api_keys") or [])
        if secrets.get("gemini_api_key") and secrets["gemini_api_key"] not in gemini_keys:
            gemini_keys.append(secrets["gemini_api_key"])
        if gemini_keys:
            try:
                from google import genai
                prompt_text = "\n\n".join(f"{m['role'].upper()}:\n{m['content']}" for m in messages)
                for key in gemini_keys:
                    try:
                        resp = genai.Client(api_key=key).models.generate_content(
                            model="gemini-2.0-flash",
                            contents=prompt_text,
                        )
                        if resp and getattr(resp, "text", None):
                            return resp.text.strip()
                    except Exception as e:
                        if _is_rate_limit_error(e):
                            saw_rate_limit = True
                            continue
            except Exception:
                pass

        if saw_rate_limit:
            raise _RateLimitError("All LLM providers rate-limited")
        raise RuntimeError("No LLM provider succeeded during memory extraction")

    def _build_extraction_messages(self, turns: List[WorkingTurn]) -> List[Dict[str, str]]:
        convo = "\n".join(f"{t.role.upper()}: {t.content}" for t in turns)
        system = (
            "You are AIVA's strict JSON memory extractor. Extract ONLY durable, non-sensitive memories.\n"
            "NEVER extract passwords, OTPs, PINs, API keys, card numbers, or government IDs.\n"
            "CRITICAL IDENTITY RULE: NEVER populate `name` from a language, nationality, ethnicity, "
            "religion, or regional statement (for example 'I am Telugu', 'I'm Indian', 'I speak Tamil' "
            "must be stored as `language/origin: ...` or `nationality/origin: ...`, NEVER `name: ...`).\n"
            "Do not extract conversational `name:` guesses that override the user's account or preferred name.\n"
            "Output ONLY valid JSON matching this exact schema:\n"
            "{\n"
            '  "memories": [\n'
            '    {"type": "semantic|episodic|procedural|prospective", "content": "...", "summary": "...", '
            '"entities": ["..."], "importance": 0.0-1.0, "confidence": 0.0-1.0, '
            '"source": "user_said|inferred", "due_time": null or "natural or ISO time", "recurrence": null or "daily|weekly|monthly|hourly"}\n'
            "  ],\n"
            '  "entities": [\n'
            '    {"name": "...", "kind": "person|device|project|place|org|other", "aliases": [], "attributes": {}}\n'
            "  ],\n"
            '  "relations": [\n'
            '    {"from": "...", "to": "...", "relation": "..."}\n'
            "  ]\n"
            "}"
        )
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": f"Turns:\n{convo}"},
        ]

    @staticmethod
    def validate_extraction_payload(raw_text: str) -> Dict[str, Any]:
        """Parse and strictly validate extracted JSON payload; raise ValueError if malformed."""
        if not raw_text or not isinstance(raw_text, str):
            raise ValueError("Empty LLM response")
        cleaned = raw_text.strip()
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE).strip()
        # Locate outermost JSON object if wrapped in chatter
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start == -1 or end == -1 or end < start:
            raise ValueError("No JSON object found")
        data = json.loads(cleaned[start : end + 1])
        if not isinstance(data, dict):
            raise ValueError("Root JSON must be object")

        memories_out: List[Dict[str, Any]] = []
        raw_memories = data.get("memories", [])
        if not isinstance(raw_memories, list):
            raise ValueError("'memories' must be a list")

        allowed_types = {
            MemoryType.SEMANTIC.value,
            MemoryType.EPISODIC.value,
            MemoryType.PROCEDURAL.value,
            MemoryType.PROSPECTIVE.value,
        }
        for entry in raw_memories:
            if not isinstance(entry, dict):
                continue
            mtype = str(entry.get("type", "")).lower().strip()
            content = str(entry.get("content", "")).strip()
            if mtype not in allowed_types or not content:
                continue
            summary = str(entry.get("summary") or content[:180]).strip()
            if mtype == MemoryType.SEMANTIC.value:
                content, summary, _was_reclassified, _bad = reclassify_invalid_name_memory(content, summary)
            source = str(entry.get("source", MemorySource.USER_SAID.value)).lower().strip()
            if source not in (MemorySource.USER_SAID.value, MemorySource.INFERRED.value):
                source = MemorySource.INFERRED.value
            try:
                imp = max(0.0, min(1.0, float(entry.get("importance", 0.6))))
                conf = max(0.0, min(1.0, float(entry.get("confidence", 0.75))))
            except (TypeError, ValueError):
                continue
            ents = [str(e).strip() for e in (entry.get("entities") or []) if str(e).strip()]
            memories_out.append(
                {
                    "type": mtype,
                    "content": content,
                    "summary": summary,
                    "entities": ents,
                    "importance": imp,
                    "confidence": conf,
                    "source": source,
                    "due_time": entry.get("due_time"),
                    "recurrence": entry.get("recurrence"),
                }
            )

        entities_out: List[Dict[str, Any]] = []
        for ent in data.get("entities") or []:
            if not isinstance(ent, dict):
                continue
            name = str(ent.get("name", "")).strip()
            if not name:
                continue
            kind = str(ent.get("kind", "other")).lower().strip()
            if kind not in VALID_ENTITY_KINDS:
                kind = "other"
            if kind == "person" and classify_non_name_identity(name):
                kind = "other"
            aliases = [str(a).strip() for a in (ent.get("aliases") or []) if str(a).strip()]
            attrs = ent.get("attributes") if isinstance(ent.get("attributes"), dict) else {}
            entities_out.append({"name": name, "kind": kind, "aliases": aliases, "attributes": attrs})

        relations_out: List[Dict[str, str]] = []
        for rel in data.get("relations") or []:
            if not isinstance(rel, dict):
                continue
            f_name = str(rel.get("from", "")).strip()
            t_name = str(rel.get("to", "")).strip()
            r_name = str(rel.get("relation", "")).strip()
            if f_name and t_name and r_name:
                relations_out.append({"from": f_name, "to": t_name, "relation": r_name})

        return {"memories": memories_out, "entities": entities_out, "relations": relations_out}

    def process_job(self, job: ExtractionJob) -> int:
        """Execute one extraction job synchronously; requeues with backoff on rate limit."""
        t0 = time.perf_counter()
        if not job.turns:
            return 0
        settings = self.store.get_memory_settings(job.user_id)
        if not settings.memory_enabled or settings.learning_paused:
            return 0

        try:
            messages = self._build_extraction_messages(job.turns)
            raw = self._call_llm_pool(messages)
            parsed = self.validate_extraction_payload(raw)
        except Exception as err:
            dur_ms = (time.perf_counter() - t0) * 1000
            if _is_rate_limit_error(err) or isinstance(err, _RateLimitError):
                backoff = min(300.0, float(2 ** min(job.attempt, 7)))
                requeued = ExtractionJob(
                    user_id=job.user_id,
                    channel=job.channel,
                    turns=job.turns,
                    enqueued_at=job.enqueued_at,
                    attempt=job.attempt + 1,
                    next_retry_at=time.time() + backoff,
                )
                with self._buf_lock:
                    self._deferred.append(requeued)
                log_memory("extract", dur_ms, "requeued_rate_limit", user_id=job.user_id)
                return 0
            log_memory("extract", dur_ms, "invalid_payload", user_id=job.user_id)
            return 0

        stored = 0
        for ent in parsed["entities"]:
            sens, _ = contains_sensitive_data(ent["name"])
            if not sens:
                self.store.upsert_entity(
                    user_id=job.user_id,
                    name=ent["name"],
                    kind=ent["kind"],
                    aliases=ent["aliases"],
                    attributes=ent["attributes"],
                )

        for rel in parsed["relations"]:
            self.store.link_entities(job.user_id, rel["from"], rel["to"], rel["relation"])

        for m in parsed["memories"]:
            sens, _ = contains_sensitive_data(m["content"])
            if sens:
                continue
            item = MemoryItem(
                id=new_id(),
                user_id=job.user_id,
                type=m["type"],
                content=m["content"],
                summary=m["summary"],
                entities=m["entities"],
                importance=m["importance"],
                confidence=m["confidence"],
                source=m["source"],
                channel=job.channel if job.channel in ("voice", "hud", "whatsapp") else "hud",
            )
            ok, _ = self.store.insert_memory(item)
            if not ok:
                continue
            stored += 1
            if self._on_memory_created:
                self._on_memory_created(job.user_id, item)

            # Prospective commitments -> reminder if due time known, else track one-time question
            if item.type == MemoryType.PROSPECTIVE.value:
                due_at: Optional[float] = None
                recurrence = m.get("recurrence")
                due_hint = m.get("due_time")
                if due_hint:
                    due_at, parsed_rec, _, urgent = parse_natural_due_and_recurrence(str(due_hint))
                    recurrence = recurrence or parsed_rec
                else:
                    due_at, parsed_rec, _, urgent = parse_natural_due_and_recurrence(item.content)
                    recurrence = recurrence or parsed_rec

                if due_at is not None:
                    rem = ReminderItem(
                        id=new_id(),
                        user_id=job.user_id,
                        text=item.summary or item.content,
                        due_at=due_at,
                        recurrence=recurrence,
                        channel=settings.preferred_reminder_channel or job.channel,
                        status=ReminderStatus.PENDING.value,
                        created_from_memory_id=item.id,
                        urgent=urgent,
                    )
                    self.store.create_reminder(rem)
                else:
                    with self._buf_lock:
                        if user_id_key := job.user_id:
                            if user_id_key not in self._pending_commitment_due:
                                self._pending_commitment_due[user_id_key] = {
                                    "memory_id": item.id,
                                    "text": item.summary or item.content,
                                    "asked": False,
                                }

        log_memory(
            "extract",
            (time.perf_counter() - t0) * 1000,
            "ok",
            user_id=job.user_id,
            count=stored,
        )
        return stored

    def flush(self, user_id: Optional[str] = None, now: Optional[float] = None) -> int:
        """Flush buffered turns and due deferred jobs immediately (useful in shutdown and tests)."""
        cur = now if now is not None else time.time()
        jobs: List[ExtractionJob] = []
        with self._buf_lock:
            for (uid, _sid), buf in list(self._buffers.items()):
                if user_id and uid != user_id:
                    continue
                if buf["pending_extract"]:
                    channel = buf["pending_extract"][-1].channel
                    jobs.append(
                        ExtractionJob(
                            user_id=uid,
                            channel=channel,
                            turns=list(buf["pending_extract"]),
                        )
                    )
                    buf["pending_extract"].clear()

            ready_deferred: List[ExtractionJob] = []
            still_waiting: List[ExtractionJob] = []
            for d_job in self._deferred:
                if (user_id is None or d_job.user_id == user_id) and cur >= d_job.next_retry_at:
                    ready_deferred.append(d_job)
                else:
                    still_waiting.append(d_job)
            self._deferred = still_waiting
            jobs.extend(ready_deferred)

        total = 0
        for job in jobs:
            total += self.process_job(job)
        return total

    def pending_retry_count(self, user_id: Optional[str] = None) -> int:
        with self._buf_lock:
            if user_id:
                return sum(1 for j in self._deferred if j.user_id == user_id)
            return len(self._deferred)

    def _worker_loop(self) -> None:
        while self._running:
            now = time.time()
            # 1. Promote idle buffers past idle_flush_s & ready rate-limited retries
            with self._buf_lock:
                for (uid, _sid), buf in list(self._buffers.items()):
                    if buf["pending_extract"] and (now - buf["last_ts"]) >= self.idle_flush_s:
                        channel = buf["pending_extract"][-1].channel
                        self._queue.put(
                            ExtractionJob(
                                user_id=uid,
                                channel=channel,
                                turns=list(buf["pending_extract"]),
                            )
                        )
                        buf["pending_extract"].clear()

                still_waiting: List[ExtractionJob] = []
                for d_job in self._deferred:
                    if now >= d_job.next_retry_at:
                        self._queue.put(d_job)
                    else:
                        still_waiting.append(d_job)
                self._deferred = still_waiting

            try:
                job = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            if job is None:
                break
            try:
                self.process_job(job)
            except Exception:
                log_memory("extract_worker", 0.0, "error", user_id=getattr(job, "user_id", None))
