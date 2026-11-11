"""Idle/nightly consolidation: dedupe, conflict supersession, session rollups, decay, expiry, embeddings."""

import re
import time
from typing import Dict, List, Optional, Tuple

from core.memory.models import (
    MemoryItem,
    MemorySource,
    MemoryStatus,
    MemoryType,
    new_id,
    normalize_text,
)
from core.memory.retriever import HybridRetriever
from core.memory.safety import log_memory
from core.memory.store import MemoryStore


def _jaccard(a: str, b: str) -> float:
    ta = set(re.findall(r"[a-z0-9]{2,}", normalize_text(a)))
    tb = set(re.findall(r"[a-z0-9]{2,}", normalize_text(b)))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


# Deterministic attribute-slot patterns for conflict supersession (preserves history)
_FACT_SLOT_PATTERNS = [
    (re.compile(r"^(?:name|my name is|i am called)\s*(?::|is)?\s*(.+)$", re.I), "profile:name"),
    (re.compile(r"^(?:location|city|i live in|i moved to|i stay in|i am from)\s*(?::|is)?\s*(.+)$", re.I), "profile:location"),
    (re.compile(r"^(?:work|occupation|profession|i work at|i work as|my job is)\s*(?::|is)?\s*(.+)$", re.I), "profile:work"),
    (re.compile(r"^(?:favorite\s+[a-z0-9_ ]+?)\s*(?::|is|are)\s*(.+)$", re.I), None),  # dynamic prefix key
    (re.compile(r"^(?:preferred\s+[a-z0-9_ ]+?)\s*(?::|is)\s*(.+)$", re.I), None),
    (re.compile(r"^([a-z][a-z0-9_ ]{1,30}):\s*(.+)$", re.I), None),  # "key: value" legacy & structured format
]


def extract_conflict_slot(content: str) -> Optional[Tuple[str, str]]:
    """Return `(normalized_slot_key, normalized_value)` when statement expresses a single-valued attribute."""
    text = (content or "").strip()
    if not text:
        return None
    for pat, fixed_slot in _FACT_SLOT_PATTERNS:
        m = pat.match(text)
        if not m:
            continue
        if fixed_slot:
            return fixed_slot, normalize_text(m.group(1))
        if m.lastindex and m.lastindex >= 2:
            return f"kv:{normalize_text(m.group(1))}", normalize_text(m.group(2))
        # Dynamic phrase prefix before ':'/'is'/'are'
        prefix_end = m.start(1)
        slot_prefix = normalize_text(text[:prefix_end])
        slot_prefix = re.sub(r"\b(?:is|are|:)$", "", slot_prefix).strip()
        if slot_prefix:
            return f"attr:{slot_prefix}", normalize_text(m.group(1))
    return None


class MemoryConsolidator:
    """Periodic/idle maintenance over per-user memories."""

    def __init__(self, store: MemoryStore, retriever: Optional[HybridRetriever] = None):
        self.store = store
        self.retriever = retriever

    def resolve_conflicts_for_user(self, user_id: str) -> int:
        """Mark older active semantic memories superseded when a newer conflicting fact arrives."""
        items = self.store.list_memories(
            user_id,
            mem_type=MemoryType.SEMANTIC.value,
            status=MemoryStatus.ACTIVE.value,
            limit=300,
        )
        # Process oldest -> newest so latest wins while preserving superseded chain
        items.sort(key=lambda m: m.created_at)
        latest_by_slot: Dict[str, Tuple[MemoryItem, str]] = {}
        superseded_count = 0

        for item in items:
            slot_info = extract_conflict_slot(item.content)
            if not slot_info:
                continue
            slot, val = slot_info
            prev = latest_by_slot.get(slot)
            if prev is not None:
                prev_item, _prev_val = prev
                if prev_item.id != item.id:
                    if self.store.supersede_memory(user_id, prev_item.id, item.id):
                        superseded_count += 1
            latest_by_slot[slot] = (item, val)
        return superseded_count

    def deduplicate_for_user(self, user_id: str, similarity_threshold: float = 0.85) -> int:
        """Deduplicate near-identical active memories within the same type."""
        items = self.store.list_memories(
            user_id,
            status=MemoryStatus.ACTIVE.value,
            limit=300,
        )
        items = [m for m in items if m.type != MemoryType.WORKING.value]
        items.sort(key=lambda m: (m.importance, m.created_at), reverse=True)

        keepers: List[MemoryItem] = []
        removed = 0
        for candidate in items:
            matched_keeper: Optional[MemoryItem] = None
            cand_norm = normalize_text(candidate.content)
            for k in keepers:
                if k.type != candidate.type:
                    continue
                if cand_norm == normalize_text(k.content) or _jaccard(candidate.content, k.content) >= similarity_threshold:
                    matched_keeper = k
                    break
            if matched_keeper:
                if self.store.supersede_memory(user_id, candidate.id, matched_keeper.id):
                    removed += 1
            else:
                keepers.append(candidate)
        return removed

    def merge_working_session(
        self,
        user_id: str,
        min_turns_to_merge: int = 8,
        keep_recent: int = 4,
    ) -> Optional[MemoryItem]:
        """Fold older working turns into one concise episodic summary memory."""
        turns = self.store.get_working_turns(user_id, limit=100)
        if len(turns) < min_turns_to_merge:
            return None

        to_merge = turns[:-keep_recent] if keep_recent > 0 else turns
        if not to_merge:
            return None

        snippets: List[str] = []
        entities = set()
        for t in to_merge:
            txt = t.content
            if txt.startswith("user: "):
                snippets.append(txt[6:].strip()[:120])
            for e in t.entities or []:
                entities.add(e)

        if not snippets:
            return None

        summary_text = "Session summary: " + " | ".join(snippets[:6])
        episodic = MemoryItem(
            id=new_id(),
            user_id=user_id,
            type=MemoryType.EPISODIC.value,
            content=summary_text[:1000],
            summary=summary_text[:200],
            entities=sorted(entities),
            importance=0.55,
            confidence=0.85,
            source=MemorySource.SYSTEM.value,
            channel=to_merge[-1].channel,
            created_at=to_merge[-1].created_at,
            last_accessed=time.time(),
            status=MemoryStatus.ACTIVE.value,
        )
        ok, _ = self.store.insert_memory(episodic)
        if not ok:
            return None

        # Mark folded working turns superseded so buffer shrinks cleanly
        for old_turn in to_merge:
            self.store.supersede_memory(user_id, old_turn.id, episodic.id)
        return episodic

    def decay_unused_memories(
        self,
        user_id: str,
        stale_days: float = 14.0,
        decay_factor: float = 0.9,
        now: Optional[float] = None,
    ) -> int:
        cur = now if now is not None else time.time()
        cutoff = cur - stale_days * 86400.0
        items = self.store.list_memories(user_id, status=MemoryStatus.ACTIVE.value, limit=300)
        decayed = 0
        for item in items:
            if item.type == MemoryType.WORKING.value:
                continue
            if item.access_count == 0 and item.last_accessed < cutoff and item.importance < 0.75 and item.importance > 0.15:
                new_imp = max(0.1, round(item.importance * decay_factor, 4))
                if new_imp < item.importance:
                    self.store.update_memory(user_id, item.id, importance=new_imp)
                    decayed += 1
        return decayed

    def backfill_embeddings(self, user_id: str, limit: int = 10) -> int:
        if not self.retriever:
            return 0
        missing = self.store.list_memories_missing_embeddings(user_id, limit=limit)
        done = 0
        for item in missing:
            if self.retriever.embeddings.embed_memory_item(user_id, item, timeout_s=1.5):
                done += 1
            else:
                break  # Stop early if rate-limited/unavailable
        return done

    def consolidate_user(self, user_id: str, backfill_limit: int = 10) -> Dict[str, int]:
        t0 = time.perf_counter()
        expired = self.store.expire_stale_memories(user_id)
        superseded = self.resolve_conflicts_for_user(user_id)
        deduped = self.deduplicate_for_user(user_id)
        episodic = 1 if self.merge_working_session(user_id) else 0
        decayed = self.decay_unused_memories(user_id)
        embedded = self.backfill_embeddings(user_id, limit=backfill_limit)

        self.store.record_audit(user_id, "consolidate", None)
        stats = {
            "expired": expired,
            "superseded": superseded,
            "deduped": deduped,
            "episodic_merged": episodic,
            "decayed": decayed,
            "embedded": embedded,
        }
        log_memory(
            "consolidate",
            (time.perf_counter() - t0) * 1000,
            "ok",
            user_id=user_id,
            count=expired + superseded + deduped + decayed,
        )
        return stats

    def run_all(self, backfill_per_user: int = 5) -> Dict[str, int]:
        agg = {"users": 0, "expired": 0, "superseded": 0, "deduped": 0}
        for uid in self.store.get_distinct_user_ids():
            res = self.consolidate_user(uid, backfill_limit=backfill_per_user)
            agg["users"] += 1
            agg["expired"] += res["expired"]
            agg["superseded"] += res["superseded"]
            agg["deduped"] += res["deduped"]
        return agg

