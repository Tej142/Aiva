from datetime import datetime, timezone
import inspect
import os
import re
import time
from typing import Any, Dict, List, Optional
from core.agents.base_agent import BaseAgent

_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "how",
    "in", "is", "it", "of", "on", "or", "that", "the", "this", "to", "was",
    "what", "when", "where", "which", "who", "why", "with", "latest", "recent",
}


def _topic_tokens(text: str) -> set:
    return {
        tok for tok in re.findall(r"[a-z0-9]{2,}", (text or "").lower())
        if tok not in _STOPWORDS
    }


def _default_cache_ttl_hours() -> float:
    try:
        return max(0.0, float(os.getenv("AIVA_KNOWLEDGE_CACHE_TTL_HOURS", "24")))
    except (TypeError, ValueError):
        return 24.0


def _default_min_relevance() -> float:
    try:
        return max(0.0, min(1.0, float(os.getenv("AIVA_KNOWLEDGE_MIN_RELEVANCE", "0.45"))))
    except (TypeError, ValueError):
        return 0.45


def _default_storage_ttl_days() -> int:
    try:
        return max(1, int(os.getenv("AIVA_KNOWLEDGE_TTL_DAYS", "7")))
    except (TypeError, ValueError):
        return 7


def _format_iso(ts: Optional[float]) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class ResearchAgent(BaseAgent):
    """Performs web research via Tavily + Mistral and saves TTL-bounded knowledge to MemoryEngine."""

    def __init__(
        self,
        tavily_service=None,
        mistral_service=None,
        context_resolver_service=None,
        get_active_user_id=None,
        **kwargs,
    ):
        super().__init__(name="research_agent", **kwargs)
        self.tavily_service = tavily_service
        self.mistral_service = mistral_service
        self.context_resolver_service = context_resolver_service
        self.get_active_user_id = get_active_user_id

        self.capabilities = [
            "research",
            "knowledge_lookup",
            "context_enrichment",
        ]
        self.subscriptions = [
            "research.requested",
            "goal.research",
            "workflow.research_needed",
        ]

    async def initialize(self):
        self.state["status"] = "ready"
        print("[ResearchAgent] Initialized")

    async def handle_event(self, event):
        event_type = event.get("type")
        if event_type in ("research.requested", "goal.research", "workflow.research_needed"):
            data = event.get("data", {}) or {}
            topic = data.get("topic") or data.get("query")
            if topic:
                await self.perform_research(topic, user_id=data.get("user_id"))

    async def execute(self, task):
        topic = (task or {}).get("topic") or (task or {}).get("query")
        uid = (task or {}).get("user_id")
        parameters = (task or {}).get("parameters", {}) or {}
        if not topic:
            topic = parameters.get("topic") or parameters.get("query")
        if not topic:
            return None
        return await self.perform_research(
            topic,
            user_id=uid,
            force_refresh=bool((task or {}).get("force_refresh") or parameters.get("force_refresh")),
        )

    def _is_fresh_cache_hit(
        self,
        topic: str,
        item: Dict[str, Any],
        now: float,
        max_age_hours: float,
        min_relevance: float,
    ) -> bool:
        if item.get("type") != "knowledge":
            return False
        exp = item.get("expires_at")
        if exp and now > float(exp):
            return False

        retrieved_ts = float(item.get("created_at") or 0.0)
        if retrieved_ts <= 0 or max_age_hours <= 0:
            return False
        if (now - retrieved_ts) > (max_age_hours * 3600.0):
            return False

        score = float(item.get("relevance") or item.get("score") or 0.0)
        if score < min_relevance:
            return False

        q_toks = _topic_tokens(topic)
        if not q_toks:
            return False
        item_text = " ".join(
            [str(item.get("content") or ""), str(item.get("summary") or ""), " ".join(item.get("entities") or [])]
        )
        i_toks = _topic_tokens(item_text)
        overlap = len(q_toks & i_toks) / max(1, len(q_toks))
        return overlap >= 0.5

    async def perform_research(
        self,
        topic: str,
        user_id: Optional[str] = None,
        force_refresh: bool = False,
        max_age_hours: Optional[float] = None,
    ):
        print(f"[ResearchAgent] Researching topic")
        uid = user_id or (self.get_active_user_id() if self.get_active_user_id else None)
        now = time.time()
        ttl_hours = _default_cache_ttl_hours() if max_age_hours is None else max(0.0, float(max_age_hours))
        min_rel = _default_min_relevance()

        # 1. Check fresh knowledge cache (similarity + age-gated; never disguised as fresh)
        if not force_refresh and self.memory_engine and uid and ttl_hours > 0:
            recalled = self.memory_engine.recall(uid, topic, top_k=3)
            for item in recalled.get("memories") or []:
                if self._is_fresh_cache_hit(topic, item, now, ttl_hours, min_rel):
                    cached_ts = float(item.get("created_at") or now)
                    cached_iso = _format_iso(cached_ts)
                    cached_summary = item.get("summary") or item.get("content") or ""
                    return {
                        "query": topic,
                        "summary": cached_summary,
                        "facts": [],
                        "sources": [],
                        "source": "memory_knowledge_cache",
                        "cached": True,
                        "fresh": False,
                        "retrieved_at": cached_iso,
                        "retrieved_at_ts": cached_ts,
                        "ref_id": item.get("id"),
                    }

        # 2. Live search via TavilyService (properly awaited; supports dict {"results": [...]} or list)
        search_results: List[Dict[str, Any]] = []
        if self.tavily_service:
            try:
                raw_resp = self.tavily_service.search(topic)
                if inspect.isawaitable(raw_resp):
                    raw_resp = await raw_resp
                if isinstance(raw_resp, dict):
                    search_results = list(raw_resp.get("results") or [])
                elif isinstance(raw_resp, list):
                    search_results = list(raw_resp)
            except Exception as err:
                print(f"[ResearchAgent] Search error: {err}")

        if not search_results:
            return None

        retrieved_ts = time.time()
        retrieved_iso = _format_iso(retrieved_ts)
        sources: List[Dict[str, str]] = []
        for entry in search_results[:5]:
            if not isinstance(entry, dict):
                continue
            url = str(entry.get("url") or "").strip()
            title = str(entry.get("title") or url or "Source").strip()
            if url or title:
                sources.append({"title": title, "url": url, "retrieved_at": retrieved_iso})

        source_url = sources[0]["url"] if sources else ""
        first_content = str((search_results[0] if isinstance(search_results[0], dict) else {}).get("content") or "").strip()
        summary = first_content[:600]
        facts: List[str] = []

        if self.mistral_service:
            try:
                summ = await self.mistral_service.summarize_research(topic, search_results)
                if summ:
                    summary = str(summ).strip()
                extracted = await self.mistral_service.extract_knowledge(topic, summary)
                if isinstance(extracted, dict):
                    facts = [str(f) for f in (extracted.get("facts") or []) if f]
                    if extracted.get("summary") and len(str(extracted["summary"])) >= 80:
                        summary = str(extracted["summary"]).strip()
            except Exception as err:
                print(f"[ResearchAgent] Summarization fallback: {err}")

        # 3. Store as source='web', type='knowledge' with configurable TTL (never dump raw report into working rows)
        ref_id = None
        if self.memory_engine and uid and summary:
            stored = self.memory_engine.store_knowledge(
                user_id=uid,
                topic=topic,
                summary=summary,
                facts=facts,
                ttl_days=_default_storage_ttl_days(),
                source_url=source_url,
                retrieved_at=retrieved_iso,
            )
            if stored is not None:
                ref_id = getattr(stored, "id", None)
            if self.context_resolver_service:
                self.context_resolver_service.set_current_topic(topic, uid)

        result = {
            "query": topic,
            "summary": summary,
            "facts": facts,
            "sources": sources,
            "source": source_url or "web",
            "cached": False,
            "fresh": True,
            "retrieved_at": retrieved_iso,
            "retrieved_at_ts": retrieved_ts,
            "ref_id": ref_id,
        }
        await self.emit_event("research.completed", result)
        return result
