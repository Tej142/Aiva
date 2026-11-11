"""AIVA Phase 1 Agent Core package."""

import os


def is_agent_core_enabled() -> bool:
    """Return True when the new `core/agent` orchestrator path is enabled via env flag."""
    raw = os.getenv("AIVA_AGENT_CORE", "0").strip().lower()
    return raw in ("1", "true", "yes", "on")


__all__ = ["is_agent_core_enabled"]

