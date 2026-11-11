"""MemoryEngine facade — single entry point replacing all legacy AIVA memory services."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import re
import time
from typing import Any, Callable, Dict, List, Optional

from core.memory.consolidator import MemoryConsolidator
from core.memory.extractor import MemoryExtractor, parse_natural_due_and_recurrence
from core.memory.injector import MemoryInjector
from core.memory.models import (
    Channel,
    MemoryItem,
    MemorySettings,
    MemorySource,
    MemoryStatus,
    MemoryType,
    ReminderItem,
    ReminderStatus,
    new_id,
)
from core.memory.retriever import EmbeddingProvider, HybridRetriever
from core.memory.safety import (
    classify_non_name_identity,
    contains_sensitive_data,
    log_memory,
    parse_call_me_command,
)
from core.memory.scheduler import MemoryScheduler, parse_snooze_duration
from core.memory.store import MemoryStore


class MemoryEngine:
    """Unified Jarvis-style memory subsystem strictly isolated by `user_id`."""

    def __init__(
        self,
        db,
        embed_fn: Optional[Callable[[str], Optional[List[float]]]] = None,
        llm_call: Optional[Callable[[List[Dict[str, str]]], str]] = None,
        embedding_timeout_s: float = 0.300,
        start_worker: bool = True,
        whatsapp_deliver: Optional[Callable[[str, str], Any]] = None,
        hud_deliver: Optional[Callable[[str, Dict[str, Any]], Any]] = None,
        voice_deliver: Optional[Callable[[str, str], Any]] = None,
        enable_async_embeddings: bool = True,
    ):
        self.db = db
        self.store = MemoryStore(db)
        self.embeddings = EmbeddingProvider(
            self.store,
            embed_fn=embed_fn,
            timeout_s=embedding_timeout_s,
            enabled=True,
        )
        self.retriever = HybridRetriever(self.store, self.embeddings)
        self.injector = MemoryInjector(self.store, self.retriever)
        self.consolidator = MemoryConsolidator(self.store, self.retriever)

        self._enable_async_embeddings = enable_async_embeddings
        self._embed_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mem-async-emb")

        self.extractor = MemoryExtractor(
            self.store,
            on_memory_created=self._on_memory_created,
            llm_call=llm_call,
            start_worker=start_worker,
        )
        self.scheduler = MemoryScheduler(
            self.store,
            consolidator=self.consolidator,
            whatsapp_deliver=whatsapp_deliver,
            hud_deliver=hud_deliver,
            voice_deliver=voice_deliver,
        )
        self._pending_forget_all: Dict[str, float] = {}

    def _on_memory_created(self, user_id: str, item: MemoryItem) -> None:
        if not self._enable_async_embeddings or item.type == MemoryType.WORKING.value:
            return
        try:
            self._embed_pool.submit(self.embeddings.embed_memory_item, user_id, item, 2.0)
        except Exception:
            pass

    def shutdown(self) -> None:
        self.scheduler.stop()
        self.extractor.stop()
        try:
            self._embed_pool.shutdown(wait=False)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # MIGRATIONS
    # ------------------------------------------------------------------

    def migrate_legacy_data(self, knowledge_json_path: str = "memory/knowledge_memory.json") -> int:
        return self.store.migrate_legacy_data(knowledge_json_path=knowledge_json_path)

    def remediate_identity_memories(self) -> Dict[str, Any]:
        return self.store.remediate_identity_memories()

    def drop_legacy_tables(self) -> None:
        self.store.drop_legacy_tables()

    # ------------------------------------------------------------------
    # LIFECYCLE HOT PATH (chat_runtime / input_handler / voice / WhatsApp)
    # ------------------------------------------------------------------

    def build_context(
        self,
        user_id: Optional[str],
        query: str,
        channel: str = Channel.HUD.value,
        embedding_timeout_s: Optional[float] = None,
    ) -> str:
        """Build `<memory>` fenced context block quickly for prompt injection."""
        if not user_id:
            return ""
        return self.injector.build_context(
            user_id=user_id,
            query=query,
            channel=channel,
            embedding_timeout_s=embedding_timeout_s,
        )

    def observe(
        self,
        user_id: Optional[str],
        turn: Any,
        channel: str = Channel.HUD.value,
        session_id: str = "default",
    ) -> None:
        """Non-blocking observation hook called after every conversation turn."""
        if not user_id or not turn:
            return
        if isinstance(turn, dict):
            chan = turn.get("channel") or channel
            sid = turn.get("session_id") or session_id
            u_text = turn.get("user") or turn.get("user_message") or turn.get("input")
            a_text = turn.get("assistant") or turn.get("assistant_response") or turn.get("response")
            single_role = turn.get("role")
            single_text = turn.get("content") or turn.get("text")
            if u_text:
                self.extractor.append_turn(user_id, "user", str(u_text), channel=chan, session_id=sid)
            if a_text:
                self.extractor.append_turn(
                    user_id,
                    "assistant",
                    str(a_text),
                    channel=chan,
                    session_id=sid,
                    enqueue_extraction=False,
                )
            if single_role and single_text:
                self.extractor.append_turn(
                    user_id,
                    str(single_role),
                    str(single_text),
                    channel=chan,
                    session_id=sid,
                    enqueue_extraction=(str(single_role) == "user"),
                )
        elif isinstance(turn, str):
            self.extractor.append_turn(user_id, "user", turn, channel=channel, session_id=session_id)

    def get_working_messages(
        self,
        user_id: Optional[str],
        session_id: str = "default",
        limit: int = 8,
    ) -> List[Dict[str, str]]:
        if not user_id:
            return []
        return self.extractor.get_working_context(user_id, session_id=session_id, limit=limit)

    def get_history(self, user_id: Optional[str], limit: int = 100) -> List[Dict[str, Any]]:
        """Compatibility shape for `/api/history` using stored working turns."""
        if not user_id:
            return []
        turns = self.store.get_working_turns(user_id, limit=limit)
        out: List[Dict[str, Any]] = []
        for t in turns:
            role = "user"
            msg = t.content
            if msg.startswith("assistant: "):
                role = "assistant"
                msg = msg[len("assistant: ") :]
            elif msg.startswith("user: "):
                role = "user"
                msg = msg[len("user: ") :]
            out.append(
                {
                    "id": t.id,
                    "role": role,
                    "message": msg,
                    "channel": t.channel,
                    "created_at": t.created_at,
                }
            )
        return out

    def clear_history(self, user_id: str) -> int:
        return self.store.clear_working_turns(user_id)

    def get_preferred_name(self, user_id: Optional[str]) -> Optional[str]:
        return self.store.get_preferred_name(user_id)

    def set_preferred_name(self, user_id: str, name: Optional[str]) -> Optional[str]:
        return self.store.set_preferred_name(user_id, name)

    def clear_preferred_name(self, user_id: str) -> None:
        self.store.set_preferred_name(user_id, None)

    def get_user_display_name(self, user_id: Optional[str]) -> Optional[str]:
        if not user_id:
            return None
        preferred = self.store.get_preferred_name(user_id)
        if preferred:
            return preferred
        user = self.db.get_user_by_id(user_id)
        if user and (user.get("name") or "").strip():
            return user["name"].strip()
        for m in self.store.get_core_profile_memories(user_id, limit=10):
            low = m.content.lower()
            candidate = None
            if low.startswith("name:"):
                candidate = m.content.split(":", 1)[1].strip()
            elif "my name is " in low:
                candidate = m.content.split("is", 1)[1].strip()
            if candidate and not classify_non_name_identity(candidate):
                return candidate
        return None

    # ------------------------------------------------------------------
    # CONVERSATIONAL MEMORY & PROSPECTIVE CONTROLS
    # ------------------------------------------------------------------

    def has_pending_confirmation(self, user_id: Optional[str]) -> bool:
        if not user_id:
            return False
        ts = self._pending_forget_all.get(user_id)
        if ts and (time.time() - ts) < 120:
            return True
        self._pending_forget_all.pop(user_id, None)
        return False

    def handle_memory_intent(
        self,
        user_id: Optional[str],
        text: str,
        channel: str = Channel.HUD.value,
    ) -> Dict[str, Any]:
        """Handle remember, remind me, snooze, forget, what do you know, settings toggles."""
        t0 = time.perf_counter()
        if not user_id:
            return {"handled": True, "response": "Please log in to use personal memory."}

        raw = (text or "").strip()
        low = raw.lower().rstrip(".!?")

        # 0. Check pending commitment follow-up due time answer
        resolved_msg = self.extractor.try_resolve_pending_commitment_time(user_id, raw, channel=channel)
        if resolved_msg:
            return {"handled": True, "route": "memory", "reminder": True, "response": resolved_msg}

        # 1. Confirm pending forget-all
        if self.has_pending_confirmation(user_id):
            if any(w in low for w in ("confirm", "yes", "proceed", "do it")):
                self._pending_forget_all.pop(user_id, None)
                deleted = self.store.forget_all(user_id)
                log_memory("forget_all", (time.perf_counter() - t0) * 1000, "ok", user_id=user_id, count=deleted)
                return {
                    "handled": True,
                    "route": "memory",
                    "deleted": deleted,
                    "response": f"Done. I've permanently erased all {deleted} stored memories, entities, and reminders.",
                }
            if any(w in low for w in ("cancel", "no", "stop", "nevermind")):
                self._pending_forget_all.pop(user_id, None)
                return {"handled": True, "route": "memory", "response": "Cancelled. Your memories were kept."}

        # 2. Forget everything (asks confirmation first unless explicit "confirm forget everything")
        if re.match(r"^(?:confirm\s+)?(?:forget|erase|delete|clear)\s+(?:all\s+(?:my\s+)?)?(?:everything|all\s+memories)\b", low):
            if low.startswith("confirm "):
                deleted = self.store.forget_all(user_id)
                return {
                    "handled": True,
                    "route": "memory",
                    "deleted": deleted,
                    "response": f"Done. Permanently cleared {deleted} memories.",
                }
            self._pending_forget_all[user_id] = time.time()
            return {
                "handled": True,
                "route": "memory",
                "confirmation_required": True,
                "response": "Are you sure you want to erase all your memories, entities, and reminders? Reply 'confirm forget everything' to proceed.",
            }

        # 2b. Explicit "call me X" / "address me as X" / preferred name reset
        call_me = parse_call_me_command(raw)
        if call_me is not None:
            action_kind, target_name = call_me
            if action_kind == "reset":
                self.clear_preferred_name(user_id)
                fallback = self.get_user_display_name(user_id)
                msg = (
                    f"Got it — I've cleared your preferred name and will call you {fallback}."
                    if fallback
                    else "Got it — I've cleared your preferred name."
                )
                return {
                    "handled": True,
                    "route": "memory",
                    "preferred_name": fallback,
                    "response": msg,
                }
            try:
                saved = self.set_preferred_name(user_id, target_name)
                return {
                    "handled": True,
                    "route": "memory",
                    "preferred_name": saved,
                    "response": f"Got it — I'll call you {saved} from now on.",
                }
            except ValueError as err:
                return {
                    "handled": True,
                    "route": "memory",
                    "error": str(err),
                    "response": "Please choose a short, valid name for me to call you.",
                }

        # 3. Settings voice/chat toggles
        if re.search(r"\b(?:pause|stop)\s+learning\b", low):
            self.store.update_memory_settings(user_id, {"learning_paused": True})
            return {"handled": True, "route": "memory", "response": "Okay, I've paused background memory learning."}
        if re.search(r"\b(?:resume|start)\s+learning\b", low):
            self.store.update_memory_settings(user_id, {"learning_paused": False, "memory_enabled": True})
            return {"handled": True, "route": "memory", "response": "Memory learning is resumed."}
        if re.search(r"\bturn\s+off\s+memory\b|\bdisable\s+memory\b", low):
            self.store.update_memory_settings(user_id, {"memory_enabled": False})
            return {"handled": True, "route": "memory", "response": "Memory is now turned off."}
        if re.search(r"\bturn\s+on\s+memory\b|\benable\s+memory\b", low):
            self.store.update_memory_settings(user_id, {"memory_enabled": True})
            return {"handled": True, "route": "memory", "response": "Memory is now turned on."}

        # 4. Snooze reminder
        if re.match(r"^(?:please\s+)?snooze\b", low):
            secs = parse_snooze_duration(raw, default_seconds=600)
            rem = self.scheduler.snooze_reminder(user_id, duration_seconds=secs)
            if not rem:
                return {"handled": True, "route": "memory", "response": "You don't have any active reminder to snooze."}
            mins = max(1, round(secs / 60))
            return {
                "handled": True,
                "route": "memory",
                "reminder": rem.to_dict(),
                "response": f"Snoozed '{rem.text}' for {mins} minute{'s' if mins != 1 else ''}.",
            }

        # 5. Forget <thing>
        forget_match = re.match(r"^(?:please\s+)?forget\s+(?:that\s+|about\s+)?(.+)$", raw, flags=re.I)
        if forget_match:
            target = forget_match.group(1).strip(" .!?")
            if not target:
                return {"handled": True, "route": "memory", "response": "What would you like me to forget?"}
            hits = self.retriever.search(user_id, target, top_k=10, include_working=False, touch_access=False)
            if not hits:
                hits = self.store.list_memories(user_id, search=target, limit=10)
            removed = 0
            for item in hits:
                if item.type == MemoryType.WORKING.value:
                    continue
                if self.store.forget_memory(user_id, item.id):
                    removed += 1
            log_memory("forget", (time.perf_counter() - t0) * 1000, "ok", user_id=user_id, count=removed)
            if removed == 0:
                return {"handled": True, "route": "memory", "response": f"I didn't find any stored memories about '{target}'."}
            return {
                "handled": True,
                "route": "memory",
                "deleted": removed,
                "response": f"Forgotten {removed} memor{'y' if removed == 1 else 'ies'} matching '{target}'.",
            }

        # 6. Explicit remember
        if re.match(r"^(?:please\s+)?(?:remember|don'?t\s+forget|save\s+this|keep\s+in\s+mind)\b", low):
            res = self.extractor.handle_explicit_remember(user_id, raw, channel=channel)
            res["route"] = "memory"
            return res

        # 7. Explicit remind me
        if re.match(r"^(?:please\s+)?remind\s+me\b", low):
            res = self.extractor.handle_explicit_reminder(user_id, raw, channel=channel)
            res["route"] = "memory"
            return res

        # 8. List reminders
        if re.search(r"\b(?:list|show|what\s+are)\s+(?:my\s+)?reminders\b", low):
            pending = self.store.list_reminders(user_id, status=ReminderStatus.PENDING.value, limit=15)
            snoozed = self.store.list_reminders(user_id, status=ReminderStatus.SNOOZED.value, limit=15)
            active_rems = pending + snoozed
            if not active_rems:
                return {"handled": True, "route": "memory", "response": "You have no pending reminders."}
            lines = [
                f"- {r.text} ({datetime.fromtimestamp(r.due_at).strftime('%Y-%m-%d %H:%M')})"
                for r in active_rems[:6]
            ]
            return {"handled": True, "route": "memory", "response": "Upcoming reminders:\n" + "\n".join(lines)}

        # 9. "What do you know about me?" / "What do you know about <entity>?"
        know_entity = re.match(
            r"^(?:what\s+do\s+you\s+(?:know|remember)\s+about|tell\s+me\s+what\s+you\s+know\s+about)\s+(.+)$",
            low,
        )
        if know_entity:
            subject = know_entity.group(1).strip(" ?.!\"'")
            if subject in ("me", "myself"):
                items = [
                    m
                    for m in self.store.list_memories(user_id, limit=40)
                    if m.type != MemoryType.WORKING.value
                ]
                if not items:
                    return {
                        "handled": True,
                        "route": "memory",
                        "response": "I don't have any personal memories stored for you yet.",
                    }
                top_facts = [m.summary or m.content for m in items[:8]]
                return {
                    "handled": True,
                    "route": "memory",
                    "response": "Here's what I remember about you:\n- " + "\n- ".join(top_facts),
                }

            # Entity query
            entities = self.store.match_entities(user_id, subject)
            hits = self.retriever.search(user_id, subject, top_k=6, include_working=False)
            lines: List[str] = []
            for ent in entities:
                desc = f"{ent['name']} ({ent['kind']})"
                if ent.get("aliases"):
                    desc += f" [aliases: {', '.join(ent['aliases'])}]"
                if ent.get("whatsapp_contact"):
                    desc += " [linked WhatsApp contact]"
                lines.append(desc)
            for h in hits:
                lines.append(h.content)
            if not lines:
                return {
                    "handled": True,
                    "route": "memory",
                    "response": f"I don't have any stored memories about {subject}.",
                }
            return {
                "handled": True,
                "route": "memory",
                "response": f"What I know about {subject}:\n- " + "\n- ".join(lines[:6]),
            }

        # 10. General recall query routed to `memory` intent
        recalled = self.recall(user_id, raw, top_k=5)
        if not recalled["memories"]:
            return {
                "handled": True,
                "route": "memory",
                "response": "I couldn't find anything about that in your memory.",
            }
        top = [m["content"] for m in recalled["memories"][:4]]
        return {
            "handled": True,
            "route": "memory",
            "memories": recalled["memories"],
            "response": "From memory:\n- " + "\n- ".join(top),
        }

    # ------------------------------------------------------------------
    # TOOLS FOR AGENTS & WHATSAPP FLOWS
    # ------------------------------------------------------------------

    def recall(
        self,
        user_id: Optional[str],
        query: str,
        top_k: int = 5,
    ) -> Dict[str, Any]:
        """Tool `recall(query)` usable by PlannerAgent, ResearchAgent, capabilities, and WhatsApp."""
        if not user_id:
            return {"status": "error", "error": "unauthenticated", "memories": [], "entities": []}
        settings = self.store.get_memory_settings(user_id)
        if not settings.memory_enabled:
            return {"status": "disabled", "memories": [], "entities": [], "summary": ""}
        items = self.retriever.search(user_id, query, top_k=top_k, include_working=False)
        entities = self.store.match_entities(user_id, query)
        return {
            "status": "success",
            "memories": [m.to_dict() for m in items],
            "entities": entities,
            "summary": "\n".join(m.content for m in items),
        }

    def create_reminder(
        self,
        user_id: Optional[str],
        text: str,
        due_at: Any,
        recurrence: Optional[str] = None,
        channel: Optional[str] = None,
        urgent: bool = False,
    ) -> Dict[str, Any]:
        """Tool `create_reminder(text, due_at)` usable by agents and WhatsApp."""
        if not user_id:
            return {"status": "error", "error": "unauthenticated"}
        settings = self.store.get_memory_settings(user_id)
        if not settings.memory_enabled:
            return {"status": "disabled", "error": "memory_disabled"}
        sens, cat = contains_sensitive_data(text)
        if sens:
            return {"status": "error", "error": f"sensitive_{cat}"}

        due_ts: Optional[float] = None
        if isinstance(due_at, (int, float)):
            due_ts = float(due_at)
        elif isinstance(due_at, str) and due_at.strip():
            # Try ISO-8601 first, then natural language
            try:
                due_ts = datetime.fromisoformat(due_at.strip()).timestamp()
            except ValueError:
                due_ts, parsed_rec, _, parsed_urgent = parse_natural_due_and_recurrence(due_at)
                recurrence = recurrence or parsed_rec
                urgent = urgent or parsed_urgent

        if due_ts is None:
            return {
                "status": "needs_due_time",
                "message": f"When should I remind you to {text}?",
            }

        pref_chan = channel or settings.preferred_reminder_channel or Channel.HUD.value
        mem = MemoryItem(
            id=new_id(),
            user_id=user_id,
            type=MemoryType.PROSPECTIVE.value,
            content=text.strip(),
            summary=text.strip()[:180],
            importance=0.8,
            confidence=0.95,
            source=MemorySource.USER_SAID.value,
            channel=pref_chan if pref_chan in ("voice", "hud", "whatsapp") else "hud",
        )
        self.store.insert_memory(mem)
        self._on_memory_created(user_id, mem)

        rem = ReminderItem(
            id=new_id(),
            user_id=user_id,
            text=text.strip(),
            due_at=due_ts,
            recurrence=recurrence,
            channel=pref_chan,
            status=ReminderStatus.PENDING.value,
            created_from_memory_id=mem.id,
            urgent=urgent,
        )
        self.store.create_reminder(rem)
        return {"status": "success", "reminder": rem.to_dict(), "memory_id": mem.id}

    # ------------------------------------------------------------------
    # RESEARCH KNOWLEDGE & SYSTEM EVENTS
    # ------------------------------------------------------------------

    def store_knowledge(
        self,
        user_id: Optional[str],
        topic: str,
        summary: str,
        facts: Optional[List[str]] = None,
        ttl_days: int = 30,
        channel: str = Channel.HUD.value,
    ) -> Optional[MemoryItem]:
        """Store web/research output strictly as `knowledge` with `source='web'` and `expires_at`."""
        if not user_id or not summary:
            return None
        settings = self.store.get_memory_settings(user_id)
        if not settings.memory_enabled:
            return None

        now = time.time()
        full_text = f"{topic}: {summary}"
        if facts:
            full_text += " (" + "; ".join(str(f) for f in facts[:5]) + ")"
        item = MemoryItem(
            id=new_id(),
            user_id=user_id,
            type=MemoryType.KNOWLEDGE.value,
            content=full_text.strip(),
            summary=summary[:200],
            entities=[topic] if topic else [],
            importance=0.55,
            confidence=0.8,
            source=MemorySource.WEB.value,
            channel=channel,
            created_at=now,
            last_accessed=now,
            expires_at=now + ttl_days * 86400.0,
            status=MemoryStatus.ACTIVE.value,
        )
        ok, _ = self.store.insert_memory(item, is_verified_user=True)
        if ok:
            self._on_memory_created(user_id, item)
            return item
        return None

    def record_system_event(
        self,
        user_id: Optional[str],
        event_summary: str,
        entities: Optional[List[str]] = None,
        importance: float = 0.4,
        channel: str = Channel.HUD.value,
    ) -> Optional[MemoryItem]:
        """Record internal runtime events (MemoryAgent / kernel) safely if learning is active."""
        if not user_id or not event_summary:
            return None
        settings = self.store.get_memory_settings(user_id)
        if not settings.memory_enabled or settings.learning_paused:
            return None
        sens, _ = contains_sensitive_data(event_summary)
        if sens:
            return None
        item = MemoryItem(
            id=new_id(),
            user_id=user_id,
            type=MemoryType.EPISODIC.value,
            content=event_summary.strip(),
            summary=event_summary.strip()[:180],
            entities=entities or [],
            importance=importance,
            confidence=0.85,
            source=MemorySource.SYSTEM.value,
            channel=channel,
        )
        ok, _ = self.store.insert_memory(item)
        return item if ok else None

    # ------------------------------------------------------------------
    # CRUD, EXPORT & SETTINGS FOR API / HUD
    # ------------------------------------------------------------------

    def list_memories(
        self,
        user_id: str,
        mem_type: Optional[str] = None,
        search: Optional[str] = None,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        return [m.to_dict() for m in self.store.list_memories(user_id, mem_type=mem_type, search=search, limit=limit)]

    def update_memory(self, user_id: str, memory_id: str, **fields) -> Optional[Dict[str, Any]]:
        updated = self.store.update_memory(
            user_id,
            memory_id,
            content=fields.get("content"),
            summary=fields.get("summary"),
            importance=fields.get("importance"),
            mem_type=fields.get("type"),
            entities=fields.get("entities"),
        )
        if updated and "content" in fields and fields["content"] is not None:
            self._on_memory_created(user_id, updated)
        return updated.to_dict() if updated else None

    def forget_memory(self, user_id: str, memory_id: str) -> bool:
        return self.store.forget_memory(user_id, memory_id)

    def forget_all(self, user_id: str) -> int:
        return self.store.forget_all(user_id)

    def get_settings(self, user_id: str) -> Dict[str, Any]:
        return self.store.get_memory_settings(user_id).to_dict()

    def update_settings(self, user_id: str, patch: Dict[str, Any]) -> Dict[str, Any]:
        return self.store.update_memory_settings(user_id, patch).to_dict()

    def list_entities(self, user_id: str) -> List[Dict[str, Any]]:
        return self.store.list_entities(user_id)

    def list_reminders(self, user_id: str, status: Optional[str] = None) -> List[Dict[str, Any]]:
        return [r.to_dict() for r in self.store.list_reminders(user_id, status=status)]

    def export_user_data(self, user_id: str) -> Dict[str, Any]:
        items = [m.to_dict() for m in self.store.list_memories(user_id, status=None, limit=1000)]
        entities = self.store.list_entities(user_id)
        reminders = [r.to_dict() for r in self.store.list_reminders(user_id, limit=500)]
        settings = self.get_settings(user_id)
        self.store.record_audit(user_id, "export", None)
        return {
            "exported_at": time.time(),
            "user_id": user_id,
            "settings": settings,
            "memories": items,
            "entities": entities,
            "reminders": reminders,
        }
