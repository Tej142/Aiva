"""Prompt context builder that fences core user profile and retrieved memories."""

import time
from typing import List, Optional

from core.memory.models import MemoryType, estimate_tokens
from core.memory.retriever import HybridRetriever
from core.memory.safety import fence_memory_block, log_memory
from core.memory.store import MemoryStore


class MemoryInjector:
    """Assembles token-budgeted `<memory>` blocks for LLM system prompts."""

    def __init__(
        self,
        store: MemoryStore,
        retriever: HybridRetriever,
        core_token_budget: int = 150,
        total_token_budget: int = 450,
        max_relevant: int = 5,
    ):
        self.store = store
        self.retriever = retriever
        self.core_token_budget = core_token_budget
        self.total_token_budget = total_token_budget
        self.max_relevant = max_relevant

    def _resolve_authoritative_name(
        self, user_id: str, authoritative_name: Optional[str]
    ) -> Optional[str]:
        if isinstance(authoritative_name, str) and authoritative_name.strip():
            return authoritative_name.strip()
        try:
            pref = self.store.get_preferred_name(user_id)
            if isinstance(pref, str) and pref.strip():
                return pref.strip()
        except Exception:
            pass
        db = getattr(self.store, "db", None)
        if db is not None and hasattr(db, "get_user_by_id"):
            try:
                user = db.get_user_by_id(user_id)
                if isinstance(user, dict):
                    db_name = user.get("name")
                    if isinstance(db_name, str) and db_name.strip():
                        return db_name.strip()
            except Exception:
                pass
        return None

    @staticmethod
    def _sanitize_injected_line(
        item, raw_line: str, authoritative_name: Optional[str]
    ) -> Optional[str]:
        from .safety import (
            classify_non_name_identity,
            extract_name_assignment_value,
            reclassify_invalid_name_memory,
        )

        for probe in (getattr(item, "content", ""), raw_line):
            cand = extract_name_assignment_value(probe)
            if not cand:
                continue
            if classify_non_name_identity(cand):
                re_c, _re_s, changed, _ = reclassify_invalid_name_memory(probe, raw_line)
                return re_c if changed else None
            if authoritative_name and cand.strip().lower() != authoritative_name.strip().lower():
                return None
        return raw_line

    def build_context(
        self,
        user_id: str,
        query: str,
        channel: str = "hud",
        embedding_timeout_s: Optional[float] = None,
        authoritative_name: Optional[str] = None,
    ) -> str:
        """Build `<memory>` fenced context block or empty string when memory is off."""
        t0 = time.perf_counter()
        if not user_id:
            return ""

        settings = self.store.get_memory_settings(user_id)
        if not settings.memory_enabled:
            log_memory("inject", (time.perf_counter() - t0) * 1000, "disabled", user_id=user_id, channel=channel)
            return ""

        auth_name = self._resolve_authoritative_name(user_id, authoritative_name)
        core_lines: List[str] = []
        included_ids = set()
        used_core_tokens = 0

        # 1. Always-on Core Profile (~150 tokens)
        core_candidates = self.store.get_core_profile_memories(user_id, limit=15)
        for item in core_candidates:
            raw_line = item.summary.strip() if item.summary and len(item.summary) <= len(item.content) else item.content.strip()
            line = self._sanitize_injected_line(item, raw_line, auth_name)
            if not line or line in core_lines:
                continue
            t_cost = estimate_tokens(line) + 2
            if core_lines and (used_core_tokens + t_cost > self.core_token_budget):
                continue
            if used_core_tokens + t_cost > self.core_token_budget:
                break
            core_lines.append(line)
            included_ids.add(item.id)
            used_core_tokens += t_cost

        # 2. Up to 5 query-relevant memories within total token budget
        relevant_lines: List[str] = []
        used_total_tokens = used_core_tokens
        if query and query.strip():
            hits = self.retriever.search(
                user_id=user_id,
                query=query,
                top_k=self.max_relevant + 4,
                include_working=False,
                use_embeddings=True,
                embedding_timeout_s=embedding_timeout_s,
                touch_access=True,
            )
            for item in hits:
                if item.id in included_ids:
                    continue
                if len(relevant_lines) >= self.max_relevant:
                    break
                prefix = ""
                if item.type == MemoryType.KNOWLEDGE.value:
                    prefix = "[knowledge] "
                elif item.type == MemoryType.PROSPECTIVE.value:
                    prefix = "[commitment] "
                elif item.type == MemoryType.PROCEDURAL.value:
                    prefix = "[routine] "
                sanitized = self._sanitize_injected_line(item, item.content.strip(), auth_name)
                if not sanitized:
                    continue
                line = f"{prefix}{sanitized}"
                t_cost = estimate_tokens(line) + 2
                if used_total_tokens + t_cost > self.total_token_budget:
                    if not relevant_lines and not core_lines:
                        # Hard-truncate oversized single memory so context isn't dropped completely
                        max_chars = max(40, (self.total_token_budget - 10) * 4)
                        line = line[:max_chars].rstrip() + "..."
                        relevant_lines.append(line)
                        included_ids.add(item.id)
                    break
                relevant_lines.append(line)
                included_ids.add(item.id)
                used_total_tokens += t_cost

        block = fence_memory_block(core_lines, relevant_lines)
        dur_ms = (time.perf_counter() - t0) * 1000
        log_memory(
            "inject",
            dur_ms,
            "ok" if block else "empty",
            user_id=user_id,
            channel=channel,
            count=len(core_lines) + len(relevant_lines),
        )
        return block

