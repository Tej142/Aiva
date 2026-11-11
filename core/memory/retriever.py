"""Hybrid retriever combining FTS5, entity/contact matching, and numpy cosine embeddings."""

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
import json
import math
import re
import time
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np

from core.memory.models import MemoryItem, MemoryStatus, MemoryType, normalize_text
from core.memory.safety import log_memory
from core.memory.store import MemoryStore

DEFAULT_EMBEDDING_MODEL = "gemini-embedding-001"
DEFAULT_EMBEDDING_TIMEOUT_S = 0.300


class EmbeddingProvider:
    """Gemini free-tier embedding client with key-pool failover and disk cache."""

    def __init__(
        self,
        store: MemoryStore,
        model: str = DEFAULT_EMBEDDING_MODEL,
        embed_fn: Optional[Callable[[str], Optional[List[float]]]] = None,
        timeout_s: float = DEFAULT_EMBEDDING_TIMEOUT_S,
        enabled: bool = True,
    ):
        self.store = store
        self.model = model
        self._custom_embed_fn = embed_fn
        self.timeout_s = float(timeout_s)
        self.enabled = enabled
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="mem-embed")
        self._api_keys: Optional[List[str]] = None
        self._key_idx = 0

    def _load_gemini_keys(self) -> List[str]:
        if self._api_keys is not None:
            return self._api_keys
        keys: List[str] = []
        try:
            with open("config/secrets.json", "r", encoding="utf-8") as fh:
                sec = json.load(fh)
            raw_list = sec.get("gemini_api_keys") or []
            if isinstance(raw_list, list):
                keys.extend(k for k in raw_list if k)
            single = sec.get("gemini_api_key")
            if single and single not in keys:
                keys.append(single)
        except Exception:
            pass
        self._api_keys = keys
        return self._api_keys

    def _call_remote(self, text: str) -> Optional[List[float]]:
        if self._custom_embed_fn is not None:
            return self._custom_embed_fn(text)
        keys = self._load_gemini_keys()
        if not keys:
            return None
        try:
            from google import genai
        except Exception:
            return None

        for offset in range(len(keys)):
            idx = (self._key_idx + offset) % len(keys)
            key = keys[idx]
            try:
                client = genai.Client(api_key=key)
                resp = client.models.embed_content(model=self.model, contents=text)
                if resp and getattr(resp, "embeddings", None):
                    vec = list(resp.embeddings[0].values)
                    self._key_idx = idx
                    return vec
            except Exception:
                continue
        return None

    def get_embedding_sync(
        self,
        text: str,
        timeout_s: Optional[float] = None,
    ) -> Optional[np.ndarray]:
        """Return embedding vector from cache or API within `timeout_s`, else None."""
        if not self.enabled or not text or not text.strip():
            return None

        cached = self.store.get_cached_embedding(text, self.model)
        if cached is not None:
            return cached

        if self._custom_embed_fn is None and not self._load_gemini_keys():
            return None

        budget = self.timeout_s if timeout_s is None else float(timeout_s)
        t0 = time.perf_counter()
        fut = self._executor.submit(self._call_remote, text)
        try:
            vec = fut.result(timeout=budget)
            dur_ms = (time.perf_counter() - t0) * 1000
            if not vec:
                log_memory("embed", dur_ms, "fallback", reason="unavailable")
                return None
            self.store.set_cached_embedding(text, self.model, vec)
            log_memory("embed", dur_ms, "ok")
            return np.asarray(vec, dtype=np.float32)
        except FuturesTimeoutError:
            dur_ms = (time.perf_counter() - t0) * 1000
            log_memory("embed", dur_ms, "timeout")
            return None
        except Exception:
            dur_ms = (time.perf_counter() - t0) * 1000
            log_memory("embed", dur_ms, "error")
            return None

    def embed_memory_item(self, user_id: str, item: MemoryItem, timeout_s: float = 2.0) -> bool:
        """Compute and persist embedding for one memory (used by consolidator/background)."""
        vec = self.get_embedding_sync(item.content, timeout_s=timeout_s)
        if vec is None:
            return False
        self.store.upsert_memory_embedding(user_id, item.id, vec.tolist(), self.model)
        return True


class HybridRetriever:
    """Hybrid search: FTS5 + Entity/Alias Match + Numpy Cosine Embeddings fused via RRF."""

    def __init__(self, store: MemoryStore, embeddings: Optional[EmbeddingProvider] = None):
        self.store = store
        self.embeddings = embeddings or EmbeddingProvider(store)

    def _entity_search(
        self,
        user_id: str,
        query: str,
        include_working: bool = False,
        limit: int = 25,
    ) -> List[MemoryItem]:
        matched_entities = self.store.match_entities(user_id, query)
        candidates = self.store.list_memories(
            user_id,
            status=MemoryStatus.ACTIVE.value,
            limit=250,
        )
        if not include_working:
            candidates = [c for c in candidates if c.type != MemoryType.WORKING.value]

        if not candidates:
            return []

        entity_terms = set()
        for ent in matched_entities:
            entity_terms.add(normalize_text(ent.get("name")))
            for al in ent.get("aliases") or []:
                if al:
                    entity_terms.add(normalize_text(al))
            wa = ent.get("whatsapp_contact")
            if wa:
                entity_terms.add(normalize_text(wa.get("display_name")))
                for al in wa.get("aliases") or []:
                    if al:
                        entity_terms.add(normalize_text(al))

        q_tokens = set(re.findall(r"[A-Za-z0-9_]{2,}", normalize_text(query)))
        scored: List[Tuple[float, MemoryItem]] = []
        for item in candidates:
            hay_entities = {normalize_text(e) for e in (item.entities or []) if e}
            hay_text = normalize_text(f"{item.content} {item.summary}")
            overlap = 0.0
            for term in entity_terms:
                if term and (term in hay_entities or re.search(rf"\b{re.escape(term)}\b", hay_text)):
                    overlap += 2.0
            if q_tokens:
                mem_tokens = set(re.findall(r"[A-Za-z0-9_]{2,}", hay_text))
                shared = len(q_tokens & mem_tokens)
                if shared > 0:
                    overlap += shared / max(1, len(q_tokens))
            if overlap > 0:
                scored.append((overlap, item))

        scored.sort(key=lambda pair: (pair[0], pair[1].importance, pair[1].created_at), reverse=True)
        return [item for _, item in scored[:limit]]

    def _vector_search(
        self,
        user_id: str,
        query: str,
        include_working: bool = False,
        timeout_s: Optional[float] = None,
        limit: int = 25,
    ) -> List[Tuple[MemoryItem, float]]:
        stored = self.store.get_user_embeddings(user_id)
        if not include_working:
            stored = [(m, v) for (m, v) in stored if m.type != MemoryType.WORKING.value]

        # Run query embedding when vectors exist OR an explicit test/custom embedder is injected
        if not stored and self.embeddings._custom_embed_fn is None:
            return []

        q_vec = self.embeddings.get_embedding_sync(query, timeout_s=timeout_s)
        if q_vec is None or not stored:
            return []

        # Keep vectors whose dimension matches the query vector
        compatible = [(m, v) for (m, v) in stored if v.size == q_vec.size]
        if not compatible:
            return []

        q_norm = float(np.linalg.norm(q_vec))
        if q_norm <= 1e-12:
            return []
        q_unit = q_vec / q_norm

        mat = np.vstack([v for _, v in compatible])
        norms = np.linalg.norm(mat, axis=1)
        valid_mask = norms > 1e-12
        sims = np.zeros(len(compatible), dtype=np.float32)
        sims[valid_mask] = (mat[valid_mask] @ q_unit) / norms[valid_mask]

        ranked: List[Tuple[MemoryItem, float]] = []
        for idx, sim in enumerate(sims):
            if float(sim) > 0.15:
                ranked.append((compatible[idx][0], float(sim)))
        ranked.sort(key=lambda x: x[1], reverse=True)
        return ranked[:limit]

    def search(
        self,
        user_id: str,
        query: str,
        top_k: int = 5,
        include_working: bool = False,
        use_embeddings: bool = True,
        embedding_timeout_s: Optional[float] = None,
        touch_access: bool = True,
        now: Optional[float] = None,
    ) -> List[MemoryItem]:
        """Run hybrid retrieval (FTS5 + Entity + Vector) fused with RRF and composite scoring."""
        t0 = time.perf_counter()
        cur_now = now if now is not None else time.time()
        exclude_types = None if include_working else (MemoryType.WORKING.value,)

        if not query or not query.strip():
            return []

        # 1. FTS5 search
        fts_hits = self.store.search_fts(user_id, query, limit=30, exclude_types=exclude_types)
        fts_list = [item for item, _ in fts_hits]

        # 2. Entity & token-overlap search
        entity_list = self._entity_search(user_id, query, include_working=include_working, limit=30)

        # 3. Optional numpy cosine vector search (hard 300ms timeout)
        vec_list: List[MemoryItem] = []
        if use_embeddings:
            vec_hits = self._vector_search(
                user_id,
                query,
                include_working=include_working,
                timeout_s=embedding_timeout_s,
                limit=30,
            )
            vec_list = [item for item, _ in vec_hits]

        # Collect candidates & fuse ranks via Reciprocal Rank Fusion (RRF, k=60)
        by_id: Dict[str, MemoryItem] = {}
        rrf_scores: Dict[str, float] = {}
        k_rrf = 60.0

        for ranked_list in (fts_list, entity_list, vec_list):
            if not ranked_list:
                continue
            for rank_idx, item in enumerate(ranked_list, start=1):
                by_id[item.id] = item
                rrf_scores[item.id] = rrf_scores.get(item.id, 0.0) + (1.0 / (k_rrf + rank_idx))

        if not by_id:
            log_memory("retrieve", (time.perf_counter() - t0) * 1000, "empty", user_id=user_id)
            return []

        max_rrf = max(rrf_scores.values()) or 1.0
        scored_items: List[MemoryItem] = []
        for mid, item in by_id.items():
            relevance = rrf_scores[mid] / max_rrf
            age_days = max(0.0, (cur_now - float(item.last_accessed or item.created_at)) / 86400.0)
            recency_decay = math.exp(-age_days / 30.0)
            importance = max(0.0, min(1.0, float(item.importance)))
            usage = min(1.0, math.log1p(max(0, int(item.access_count))) / math.log1p(20))

            composite = (
                0.5 * relevance
                + 0.2 * recency_decay
                + 0.2 * importance
                + 0.1 * usage
            )
            item.relevance = relevance
            item.score = composite
            scored_items.append(item)

        scored_items.sort(key=lambda m: m.score, reverse=True)
        selected = scored_items[:top_k]

        if touch_access and selected:
            self.store.touch_access(user_id, [m.id for m in selected])

        dur_ms = (time.perf_counter() - t0) * 1000
        log_memory("retrieve", dur_ms, "ok", user_id=user_id, count=len(selected))
        return selected

