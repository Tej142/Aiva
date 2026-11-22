"""Tiered query router (Tier 0 deterministic <10ms rules -> Tier 1 fast Groq -> Tier 2 Gemini/stronger)."""

from dataclasses import dataclass
from datetime import datetime
import re
import time
from typing import Any, Dict, Optional

from core.agent.telemetry import log_agent

# Keep strictly <= 15 high-precision single-step Tier 0 rules.
# Compound ("... and ...", "... then ...") queries bypass Tier 0 for agent planning.
TIER0_MAX_RULES = 15

_COMPOUND_RE = re.compile(r"\b(?:and\s+then|and|then|after\s+that|also)\b", re.IGNORECASE)
_TIME_DATE_RE = re.compile(
    r"^(?:what(?:'s|\s+is)\s+(?:the\s+)?(?:current\s+)?time|what\s+time\s+is\s+it|what(?:'s|\s+is)\s+today'?s?\s+date)\??$",
    re.IGNORECASE,
)
_OPEN_APP_RE = re.compile(
    r"^(?:please\s+)?(?:open|launch|start)\s+([a-zA-Z0-9 ._-]{2,28})$",
    re.IGNORECASE,
)
_VOL_SET_RE = re.compile(
    r"^(?:please\s+)?(?:set\s+)?(?:the\s+)?volume\s+(?:to\s+)?(\d{1,3})%?$",
    re.IGNORECASE,
)
_VOL_MUTE_RE = re.compile(
    r"^(?:mute|unmute)(?:\s+(?:the\s+)?(?:volume|audio|sound|pc))?$",
    re.IGNORECASE,
)
_VOL_STEP_RE = re.compile(
    r"^(?:turn\s+)?(volume\s+up|volume\s+down|increase\s+volume|decrease\s+volume|louder|quieter)$",
    re.IGNORECASE,
)
_MEDIA_PLAY_PAUSE_RE = re.compile(
    r"^(?:(play|pause|resume)(?:\s+(?:music|media|video))?|stop\s+(?:music|media)|(next|previous)\s+(?:track|song))$",
    re.IGNORECASE,
)
_LOCK_RE = re.compile(
    r"^(?:please\s+)?lock(?:\s+(?:my|the))?\s+(?:screen|pc|computer|laptop|workstation)$",
    re.IGNORECASE,
)
_SCREENSHOT_RE = re.compile(
    r"^(?:please\s+)?(?:take\s+(?:a\s+)?screenshot|screenshot|capture\s+screen)$",
    re.IGNORECASE,
)
_QUICK_SEARCH_URL_RE = re.compile(
    r"^(?:search\s+(?:on\s+)?(youtube|google)\s+(?:for\s+)?|open\s+(youtube|google|github)\s+for\s+)(.+)$",
    re.IGNORECASE,
)

# Direct Tier 2 triggers: explicit deep research and browser workflows only
_TIER2_RESEARCH_RE = re.compile(
    r"\b(?:deep\s+research|deep\s+dive|in-depth\s+research|comprehensive\s+research)\b",
    re.IGNORECASE,
)
_TIER2_BROWSER_RE = re.compile(
    r"\b(?:fill\s+(?:the\s+|out\s+)?form|book\s+a\s+(?:flight|ticket|hotel|table)|sign\s+in\s+on\s+website|log\s+into\s+website|browser\s+automation|scrape)\b",
    re.IGNORECASE,
)

_URL_ALIASES = {
    "youtube": "https://www.youtube.com",
    "google": "https://www.google.com",
    "github": "https://github.com",
    "gmail": "https://mail.google.com",
}


@dataclass
class RouteDecision:
    tier: int  # 0 = deterministic, 1 = fast Groq, 2 = deeper Gemini/stronger
    rule_name: str = ""
    direct_response: Optional[str] = None
    tool_name: Optional[str] = None
    tool_args: Optional[Dict[str, Any]] = None
    tag_hint: str = "conversation"
    route_ms: float = 0.0


class TieredRouter:
    """Fast <10ms router separating deterministic single-step rules from Tier 1/2 LLM reasoning."""

    RULE_COUNT = 9

    def route(self, user_input: str) -> RouteDecision:
        t0 = time.perf_counter()
        raw = (user_input or "").strip()
        low = raw.lower().rstrip(".!?")

        def _finish(dec: RouteDecision) -> RouteDecision:
            dec.route_ms = round((time.perf_counter() - t0) * 1000, 2)
            log_agent("route", dec.route_ms, "ok", tier=dec.tier, rule=dec.rule_name or "llm")
            return dec

        if not raw:
            return _finish(RouteDecision(tier=1, rule_name="empty"))

        has_compound = bool(_COMPOUND_RE.search(raw))

        if not has_compound:
            # Rule 1: Time / Date check
            if _TIME_DATE_RE.match(low):
                now = datetime.now()
                if "date" in low:
                    ans = f"Today is {now.strftime('%A, %B %d, %Y')}."
                else:
                    ans = f"It's {now.strftime('%I:%M %p').lstrip('0')}."
                return _finish(RouteDecision(tier=0, rule_name="time_date", direct_response=ans, tag_hint="conversation"))

            # Rule 2: Lock screen
            if _LOCK_RE.match(low):
                return _finish(RouteDecision(
                    tier=0, rule_name="lock_screen", tool_name="power_control", tool_args={"action": "lock"}, tag_hint="action"
                ))

            # Rule 3: Screenshot
            if _SCREENSHOT_RE.match(low):
                return _finish(RouteDecision(
                    tier=0, rule_name="screenshot", tool_name="screenshot", tool_args={"mode": "capture"}, tag_hint="action"
                ))

            # Rule 4: Mute
            if _VOL_MUTE_RE.match(low):
                return _finish(RouteDecision(
                    tier=0, rule_name="volume_mute", tool_name="media_volume", tool_args={"kind": "volume", "mute": True}, tag_hint="action"
                ))

            # Rule 5: Set volume N
            m_vol = _VOL_SET_RE.match(low)
            if m_vol:
                lvl = max(0, min(100, int(m_vol.group(1))))
                return _finish(RouteDecision(
                    tier=0, rule_name="volume_set", tool_name="media_volume", tool_args={"kind": "volume", "level": lvl}, tag_hint="action"
                ))

            # Rule 6: Volume up/down
            m_step = _VOL_STEP_RE.match(low)
            if m_step:
                txt = m_step.group(1).lower()
                delta = -10 if ("down" in txt or "decrease" in txt or "quieter" in txt) else 10
                return _finish(RouteDecision(
                    tier=0, rule_name="volume_step", tool_name="media_volume", tool_args={"kind": "volume", "delta": delta}, tag_hint="action"
                ))

            # Rule 7: Media play/pause/next/prev
            m_media = _MEDIA_PLAY_PAUSE_RE.match(low)
            if m_media:
                if "stop" in low:
                    act = "stop"
                elif m_media.group(2):
                    act = m_media.group(2).lower()
                else:
                    act = "play_pause"
                return _finish(RouteDecision(
                    tier=0, rule_name="media_control", tool_name="media_volume", tool_args={"kind": "media", "media_action": act}, tag_hint="action"
                ))

            # Rule 8: Single site search URL
            m_q = _QUICK_SEARCH_URL_RE.match(low)
            if m_q:
                from urllib.parse import quote_plus
                site = (m_q.group(1) or m_q.group(2) or "google").lower()
                q_str = quote_plus(m_q.group(3).strip())
                url = (
                    f"https://www.youtube.com/results?search_query={q_str}"
                    if site == "youtube"
                    else f"https://www.google.com/search?q={q_str}"
                )
                return _finish(RouteDecision(
                    tier=0, rule_name="quick_url_search", tool_name="open_apps_urls", tool_args={"url": url}, tag_hint="action"
                ))

            # Rule 9: Open single desktop app or named URL site (avoid swallowing web research phrases)
            m_open = _OPEN_APP_RE.match(low)
            if m_open:
                target = m_open.group(1).strip()
                words = set(target.split())
                vague_or_non_app = {"that", "this", "it", "one", "something", "them", "file", "folder", "document"}
                if not (words & vague_or_non_app) and not any(
                    bad in target for bad in ("browser for", "settings", "whatsapp chat")
                ):
                    if target in _URL_ALIASES:
                        return _finish(RouteDecision(
                            tier=0, rule_name="open_known_site", tool_name="open_apps_urls", tool_args={"url": _URL_ALIASES[target]}, tag_hint="action"
                        ))
                    return _finish(RouteDecision(
                        tier=0, rule_name="open_app", tool_name="open_apps_urls", tool_args={"app_name": target}, tag_hint="action"
                    ))

        # Direct Tier 2 ONLY for explicit deep research and browser workflows.
        # Stop escalating to Tier 2 purely because of clause count or keywords;
        # Tier 1 models escalate dynamically using the escalate tool.
        if _TIER2_RESEARCH_RE.search(raw) or _TIER2_BROWSER_RE.search(raw):
            return _finish(RouteDecision(tier=2, rule_name="tier2_complex_or_research", tag_hint="research" if _TIER2_RESEARCH_RE.search(raw) else "action"))

        # Default: Tier 1 fast Groq LLM with tools
        return _finish(RouteDecision(tier=1, rule_name="tier1_fast_agent", tag_hint="conversation"))
