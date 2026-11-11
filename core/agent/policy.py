"""Risk policy, confirmation helpers, and untrusted tool-result XML fencing."""

from dataclasses import dataclass, field
import json
import re
from typing import Any, Dict, Optional

RISK_READ = "read"
RISK_LOW = "low"
RISK_CONFIRM = "confirm"

_CONFIRM_PHRASES = frozenset({
    "confirm", "yes confirm", "proceed", "yes proceed", "yes do it", "do it",
})
_CANCEL_PHRASES = frozenset({
    "cancel", "abort", "stop", "no", "nevermind", "never mind", "don't", "dont",
})

_INJECTION_PATTERNS = (
    re.compile(r"(?i)ignore\s+(?:all\s+)?previous\s+instructions"),
    re.compile(r"(?i)disregard\s+(?:the\s+)?system\s+prompt"),
    re.compile(r"(?i)</?\s*(?:identity|system|assistant|tool_result|memory)\b[^>]*>"),
    re.compile(r"<\|(?:im_start|im_end|system|user|assistant)\|>"),
)


def is_confirmation_phrase(text: str) -> bool:
    clean = (text or "").strip().lower().rstrip(".!?,")
    return clean in _CONFIRM_PHRASES


def is_cancel_phrase(text: str) -> bool:
    clean = (text or "").strip().lower().rstrip(".!?,")
    return clean in _CANCEL_PHRASES


def sanitize_untrusted_text(text: str, max_chars: int = 4000) -> str:
    """Neutralize prompt-injection markers inside web/third-party tool output."""
    clean = str(text or "")
    for pat in _INJECTION_PATTERNS:
        clean = pat.sub("[redacted-directive]", clean)
    if len(clean) > max_chars:
        clean = clean[:max_chars] + "... [truncated]"
    return clean


def fence_tool_result(tool_name: str, data: Any, untrusted: bool = False) -> str:
    """Wrap tool output as inert data XML, fencing untrusted web/third-party text."""
    try:
        raw = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, default=str)
    except Exception:
        raw = str(data)
    safe_body = sanitize_untrusted_text(raw, max_chars=4500 if untrusted else 3500)
    tag_name = re.sub(r"[^a-zA-Z0-9_]", "_", str(tool_name or "tool"))
    if untrusted:
        return (
            f'<tool_result name="{tag_name}" untrusted="true" provenance="data_only">\n'
            f"{safe_body}\n"
            f"</tool_result>"
        )
    return (
        f'<tool_result name="{tag_name}" untrusted="false">\n'
        f"{safe_body}\n"
        f"</tool_result>"
    )


@dataclass
class RiskDecision:
    allowed: bool
    risk_level: str = RISK_READ
    requires_confirmation: bool = False
    reason: str = ""
    confirmation_prompt: str = ""
    action_label: str = ""
    blocked_message: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)


def evaluate_tool_risk(
    spec: Any,
    args: Dict[str, Any],
    ctx: Any,
    preflight: Optional[Dict[str, Any]] = None,
) -> RiskDecision:
    """Evaluate channel allowlists, confirmation rules, and contact trust before executing a tool."""
    chan = (getattr(ctx, "channel", "hud") or "hud").lower()
    allowed_channels = tuple(getattr(spec, "allowed_channels", ("hud", "voice", "whatsapp")))
    if chan not in allowed_channels:
        return RiskDecision(
            allowed=False,
            risk_level=RISK_CONFIRM,
            reason="channel_blocked",
            blocked_message=f"'{spec.name}' is disabled on the {chan} channel for security.",
        )

    confirmed = bool(getattr(ctx, "confirmed", False))

    # Power control: lock is low-risk; shutdown/restart/sleep/hibernate require explicit confirm
    if spec.name == "power_control":
        op = str(args.get("action") or "lock").strip().lower()
        if op in ("shutdown", "restart", "reboot", "hibernate", "sleep", "logoff"):
            if not confirmed:
                return RiskDecision(
                    allowed=False,
                    risk_level=RISK_CONFIRM,
                    requires_confirmation=True,
                    reason="destructive_power_action",
                    action_label=op,
                    confirmation_prompt=(
                        f"I'm ready to {op} your PC. Please say 'confirm' to proceed, or 'cancel' to abort."
                    ),
                )
            return RiskDecision(allowed=True, risk_level=RISK_CONFIRM)
        return RiskDecision(allowed=True, risk_level=RISK_LOW)

    # File/App closing or destructive file actions
    if spec.name == "file_access":
        op = str(args.get("action") or "open").strip().lower()
        if op in ("close_app", "delete", "remove"):
            if not confirmed:
                target = args.get("app_name") or args.get("path") or "that target"
                return RiskDecision(
                    allowed=False,
                    risk_level=RISK_CONFIRM,
                    requires_confirmation=True,
                    reason="destructive_app_or_file_action",
                    action_label=f"{op} {target}",
                    confirmation_prompt=f"Please say 'confirm' to {op.replace('_', ' ')} '{target}', or 'cancel' to abort.",
                )
            return RiskDecision(allowed=True, risk_level=RISK_CONFIRM)
        return RiskDecision(allowed=True, risk_level=RISK_LOW)

    # WhatsApp sending: check contact resolution, disambiguation, and trust flag
    if spec.name == "whatsapp_send" and preflight is not None:
        status = preflight.get("status")
        if status == "not_found":
            return RiskDecision(
                allowed=False,
                risk_level=RISK_CONFIRM,
                reason="contact_not_found",
                blocked_message=preflight.get("message") or "Contact not found in your WhatsApp contacts.",
            )
        if status == "ambiguous":
            return RiskDecision(
                allowed=False,
                risk_level=RISK_CONFIRM,
                reason="contact_ambiguous",
                blocked_message=preflight.get("message") or "Multiple matching contacts found. Which one did you mean?",
                extra={"matches": preflight.get("matches") or []},
            )
        if status == "found":
            contact = preflight.get("contact") or {}
            if not bool(contact.get("trusted", True)) and not confirmed:
                cname = contact.get("display_name") or "this contact"
                return RiskDecision(
                    allowed=False,
                    risk_level=RISK_CONFIRM,
                    requires_confirmation=True,
                    reason="untrusted_recipient",
                    action_label=f"send WhatsApp message to {cname}",
                    confirmation_prompt=(
                        f"'{cname}' is marked as a non-trusted contact. Say 'confirm' to send the message, or 'cancel' to abort."
                    ),
                )
            return RiskDecision(allowed=True, risk_level=RISK_LOW)

    base_risk = getattr(spec, "risk", RISK_READ)
    if base_risk == RISK_CONFIRM and not confirmed:
        return RiskDecision(
            allowed=False,
            risk_level=RISK_CONFIRM,
            requires_confirmation=True,
            reason="confirm_risk_tool",
            action_label=spec.name,
            confirmation_prompt=f"Please say 'confirm' to run {spec.name}, or 'cancel' to abort.",
        )

    return RiskDecision(allowed=True, risk_level=base_risk)
