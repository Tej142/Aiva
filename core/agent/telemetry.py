"""Structured non-PII telemetry logging and aggregate latency/tier storage."""

import json
import math
import time
import uuid
from typing import Any, Dict, List, Optional

from core.shared.logger import get_logger

_LOGGER = get_logger("Agent")

_FORBIDDEN_LOG_KEYS = frozenset({
    "message", "text", "query", "content", "transcript", "prompt",
    "response", "reply", "phone", "phone_e164", "email", "address",
    "reason", "explanation",
})

ESCALATION_REASONS = frozenset({
    "multi_step_planning",
    "empty_contradictory_results",
    "unsure",
    "complex_synthesis",
})


def normalize_escalation_reason(raw: Any) -> str:
    """Normalize any raw string/enum into one of the 4 approved non-PII escalation enum codes."""
    if not raw:
        return "multi_step_planning"
    low = str(raw).lower().strip().replace("-", "_").replace(" ", "_")
    if low in ESCALATION_REASONS:
        return low
    if any(k in low for k in ("empty", "contradict", "conflict", "missing", "failed", "no_result", "null")):
        return "empty_contradictory_results"
    if any(k in low for k in ("unsure", "uncertain", "confidence", "unclear", "doubt", "ambiguous", "clarify")):
        return "unsure"
    if any(k in low for k in ("synth", "complex", "deep", "analyze", "reasoning", "research")):
        return "complex_synthesis"
    return "multi_step_planning"


def log_agent(stage: str, duration_ms: float = 0.0, status: str = "ok", **fields: Any) -> str:
    """Emit structured `[Agent]` logs without any message content, transcripts, or phone numbers."""
    parts = [
        f"stage={stage}",
        f"duration_ms={round(float(duration_ms), 1)}",
        f"status={status}",
    ]
    for key, val in fields.items():
        lk = str(key).lower()
        if lk in _FORBIDDEN_LOG_KEYS or val is None:
            continue
        if lk == "user_id":
            s = str(val)
            parts.append(f"user={s[:8]}")
        elif lk == "reason_code":
            parts.append(f"reason_code={normalize_escalation_reason(val)}")
        else:
            parts.append(f"{key}={val}")
    line = "[Agent] " + " ".join(parts)
    if status in ("error", "failed", "timeout", "blocked"):
        _LOGGER.warning(line)
    else:
        _LOGGER.info(line)
    return line


def log_agent_startup(
    agent_core: Optional[bool] = None,
    tier1_models: Optional[List[str]] = None,
    tier2_models: Optional[List[str]] = None,
) -> str:
    """Emit structured startup log: [Agent] stage=startup agent_core=<on|off> tier1_models=<list> tier2_models=<list> (no keys)."""
    from core.agent import is_agent_core_enabled
    from core.agent.llm_gateway import LLMGateway

    core_enabled = is_agent_core_enabled() if agent_core is None else bool(agent_core)
    t1 = list(tier1_models) if tier1_models is not None else LLMGateway.get_tier1_models()
    t2 = list(tier2_models) if tier2_models is not None else LLMGateway.get_tier2_models()
    return log_agent(
        stage="startup",
        duration_ms=0.0,
        status="ok",
        agent_core="on" if core_enabled else "off",
        tier1_models=t1,
        tier2_models=t2,
    )


def percentile(values: List[float], p: float) -> float:
    """Compute exact p-th percentile (0..100) using linear interpolation."""
    clean = sorted(float(v) for v in values if v is not None and float(v) >= 0.0)
    if not clean:
        return 0.0
    if len(clean) == 1:
        return round(clean[0], 2)
    rank = (max(0.0, min(100.0, float(p))) / 100.0) * (len(clean) - 1)
    lo = int(math.floor(rank))
    hi = int(math.ceil(rank))
    if lo == hi:
        return round(clean[lo], 2)
    frac = rank - lo
    return round(clean[lo] + (clean[hi] - clean[lo]) * frac, 2)


class TelemetryRecorder:
    """Records aggregate non-PII agent latency, tier, tool, and success metrics."""

    def __init__(self, db=None):
        self.db = db

    def record(
        self,
        user_id: Optional[str],
        tier: int,
        channel: str = "hud",
        tag: str = "conversation",
        route_ms: float = 0.0,
        llm_first_token_ms: float = 0.0,
        tool_time_ms: float = 0.0,
        total_ms: float = 0.0,
        tts_first_audio_ms: float = 0.0,
        tools: Optional[List[str]] = None,
        success: bool = True,
    ) -> str:
        """Persist non-PII metrics row in SQLite and emit structured `[Agent]` log."""
        row_id = f"tel_{uuid.uuid4().hex[:12]}"
        clean_tools = [str(t) for t in (tools or []) if t][:12]
        log_agent(
            stage="turn_complete",
            duration_ms=total_ms,
            status="ok" if success else "error",
            tier=int(tier),
            tag=tag,
            channel=channel,
            route_ms=round(float(route_ms), 1),
            ttft_ms=round(float(llm_first_token_ms), 1),
            tool_ms=round(float(tool_time_ms), 1),
            tools=len(clean_tools),
        )
        if self.db and hasattr(self.db, "record_agent_telemetry"):
            try:
                self.db.record_agent_telemetry(
                    telemetry_id=row_id,
                    user_id=user_id,
                    tier=int(tier),
                    channel=str(channel or "hud"),
                    tag=str(tag or "conversation"),
                    route_ms=float(route_ms or 0.0),
                    llm_first_token_ms=float(llm_first_token_ms or 0.0),
                    tool_time_ms=float(tool_time_ms or 0.0),
                    total_ms=float(total_ms or 0.0),
                    tts_first_audio_ms=float(tts_first_audio_ms or 0.0),
                    tools=clean_tools,
                    success=bool(success),
                )
            except Exception as err:
                log_agent("telemetry_write", 0.0, "error", err=type(err).__name__)
        return row_id

    def get_summary(
        self,
        user_id: Optional[str] = None,
        window_hours: float = 24.0,
    ) -> Dict[str, Any]:
        """Return tier distribution, p50/p95 latencies, and success rate from aggregates."""
        empty = {
            "total_requests": 0,
            "success_rate": 100.0,
            "tiers": {"tier_0": 0, "tier_1": 0, "tier_2": 0},
            "tier_pct": {"tier_0": 0.0, "tier_1": 0.0, "tier_2": 0.0},
            "latency_ms": {
                "p50": 0.0,
                "p95": 0.0,
                "route_p50": 0.0,
                "llm_first_token_p50": 0.0,
                "llm_first_token_p95": 0.0,
                "tool_p50": 0.0,
                "tts_first_audio_p50": 0.0,
            },
            "tags": {},
            "top_tools": {},
            "window_hours": window_hours,
        }
        if not self.db or not hasattr(self.db, "get_agent_telemetry_rows"):
            return empty

        since = time.time() - max(0.1, float(window_hours)) * 3600.0
        rows = self.db.get_agent_telemetry_rows(user_id=user_id, since=since, limit=2000)
        if not rows:
            return empty

        total_n = len(rows)
        ok_n = sum(1 for r in rows if int(r.get("success", 1)) == 1)
        t0_n = sum(1 for r in rows if int(r.get("tier", 1)) == 0)
        t1_n = sum(1 for r in rows if int(r.get("tier", 1)) == 1)
        t2_n = sum(1 for r in rows if int(r.get("tier", 1)) >= 2)

        totals = [float(r.get("total_ms") or 0.0) for r in rows]
        routes = [float(r.get("route_ms") or 0.0) for r in rows]
        ttfts = [float(r.get("llm_first_token_ms") or 0.0) for r in rows if float(r.get("llm_first_token_ms") or 0.0) > 0]
        tools_ms = [float(r.get("tool_time_ms") or 0.0) for r in rows if float(r.get("tool_time_ms") or 0.0) > 0]
        tts_ms = [float(r.get("tts_first_audio_ms") or 0.0) for r in rows if float(r.get("tts_first_audio_ms") or 0.0) > 0]

        tags: Dict[str, int] = {}
        top_tools: Dict[str, int] = {}
        for r in rows:
            tag = str(r.get("tag") or "conversation")
            tags[tag] = tags.get(tag, 0) + 1
            try:
                tlist = json.loads(r.get("tools_json") or "[]")
                for tname in tlist:
                    top_tools[tname] = top_tools.get(tname, 0) + 1
            except Exception:
                pass

        return {
            "total_requests": total_n,
            "success_rate": round((100.0 * ok_n) / total_n, 1),
            "tiers": {"tier_0": t0_n, "tier_1": t1_n, "tier_2": t2_n},
            "tier_pct": {
                "tier_0": round((100.0 * t0_n) / total_n, 1),
                "tier_1": round((100.0 * t1_n) / total_n, 1),
                "tier_2": round((100.0 * t2_n) / total_n, 1),
            },
            "latency_ms": {
                "p50": percentile(totals, 50),
                "p95": percentile(totals, 95),
                "route_p50": percentile(routes, 50),
                "llm_first_token_p50": percentile(ttfts, 50),
                "llm_first_token_p95": percentile(ttfts, 95),
                "tool_p50": percentile(tools_ms, 50),
                "tts_first_audio_p50": percentile(tts_ms, 50),
            },
            "tags": tags,
            "top_tools": top_tools,
            "window_hours": window_hours,
        }

