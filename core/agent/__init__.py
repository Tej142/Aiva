"""AIVA Phase 1 Agent Core package."""

import os


def is_agent_core_enabled() -> bool:
    """Return True when the new `core/agent` orchestrator path is enabled via env flag."""
    raw = os.getenv("AIVA_AGENT_CORE", "0").strip().lower()
    return raw in ("1", "true", "yes", "on")


def log_agent_startup(*args, **kwargs):
    from core.agent.telemetry import log_agent_startup as _startup
    return _startup(*args, **kwargs)


def normalize_escalation_reason(raw):
    from core.agent.telemetry import normalize_escalation_reason as _norm
    return _norm(raw)


__all__ = ["is_agent_core_enabled", "log_agent_startup", "normalize_escalation_reason"]

