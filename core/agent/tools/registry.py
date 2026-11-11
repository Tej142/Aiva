"""Typed tool registry with strict JSON-schema validation, channel allowlists, and timeouts."""

import asyncio
from dataclasses import dataclass, field
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from core.agent.policy import RISK_READ, evaluate_tool_risk
from core.agent.telemetry import log_agent


class ToolValidationError(ValueError):
    """Raised when LLM-generated tool arguments fail strict JSON-schema validation."""


@dataclass
class ToolContext:
    user_id: Optional[str]
    channel: str = "hud"
    session_id: str = "default"
    confirmed: bool = False
    untrusted_taint: bool = False
    device_name: Optional[str] = None
    kernel: Any = None


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: Dict[str, Any]
    handler: Callable[[Dict[str, Any], ToolContext], Awaitable[Dict[str, Any]]]
    risk: str = RISK_READ
    allowed_channels: Tuple[str, ...] = ("hud", "voice", "whatsapp")
    timeout_s: float = 15.0
    untrusted_result: bool = False
    parallel_safe: bool = True
    category: str = "action"
    preflight: Optional[Callable[[Dict[str, Any], ToolContext], Optional[Dict[str, Any]]]] = None


_TYPE_MAP = {
    "string": str,
    "boolean": bool,
    "integer": int,
    "number": (int, float),
    "array": list,
    "object": dict,
}


def validate_against_schema(schema: Dict[str, Any], raw_args: Any) -> Dict[str, Any]:
    """Validate and coerce tool call arguments strictly against JSON Schema properties."""
    if raw_args is None:
        raw_args = {}
    if not isinstance(raw_args, dict):
        raise ToolValidationError("Tool arguments must be a JSON object.")

    props = schema.get("properties") or {}
    required = schema.get("required") or []

    for req_key in required:
        if req_key not in raw_args or raw_args[req_key] is None or raw_args[req_key] == "":
            raise ToolValidationError(f"Missing required parameter '{req_key}'.")

    cleaned: Dict[str, Any] = {}
    for key, val in raw_args.items():
        if key not in props:
            continue
        p_schema = props[key] or {}
        if val is None:
            continue
        expected_type = p_schema.get("type")
        if expected_type == "integer":
            if isinstance(val, bool) or not isinstance(val, int):
                try:
                    val = int(val)
                except (TypeError, ValueError) as exc:
                    raise ToolValidationError(f"Parameter '{key}' must be an integer.") from exc
        elif expected_type == "number":
            if isinstance(val, bool) or not isinstance(val, (int, float)):
                try:
                    val = float(val)
                except (TypeError, ValueError) as exc:
                    raise ToolValidationError(f"Parameter '{key}' must be a number.") from exc
        elif expected_type == "boolean":
            if not isinstance(val, bool):
                if isinstance(val, str) and val.lower() in ("true", "false"):
                    val = val.lower() == "true"
                else:
                    raise ToolValidationError(f"Parameter '{key}' must be a boolean.")
        elif expected_type in _TYPE_MAP:
            py_type = _TYPE_MAP[expected_type]
            if not isinstance(val, py_type):
                raise ToolValidationError(f"Parameter '{key}' must be of type {expected_type}.")

        if "enum" in p_schema and val not in p_schema["enum"]:
            raise ToolValidationError(
                f"Parameter '{key}' value '{val}' must be one of {p_schema['enum']}."
            )
        if expected_type in ("integer", "number"):
            if "minimum" in p_schema and val < p_schema["minimum"]:
                raise ToolValidationError(f"Parameter '{key}' must be >= {p_schema['minimum']}.")
            if "maximum" in p_schema and val > p_schema["maximum"]:
                raise ToolValidationError(f"Parameter '{key}' must be <= {p_schema['maximum']}.")

        cleaned[key] = val

    return cleaned


class ToolRegistry:
    """Central typed registry for AIVA tools (capabilities, memory, WhatsApp, research)."""

    def __init__(self):
        self._tools: Dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        self._tools[spec.name] = spec

    def get(self, name: str) -> Optional[ToolSpec]:
        return self._tools.get(name)

    def names(self) -> List[str]:
        return list(self._tools.keys())

    def list_specs(self, channel: Optional[str] = None) -> List[ToolSpec]:
        if not channel:
            return list(self._tools.values())
        ch = channel.strip().lower()
        return [s for s in self._tools.values() if ch in s.allowed_channels]

    def to_openai_tools(self, channel: str = "hud") -> List[Dict[str, Any]]:
        """Serialize channel-allowed tool specs as OpenAI/Groq/Mistral function schemas."""
        tools = []
        for spec in self.list_specs(channel=channel):
            tools.append({
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": spec.parameters,
                },
            })
        return tools

    async def execute(
        self,
        name: str,
        raw_args: Dict[str, Any],
        ctx: ToolContext,
    ) -> Dict[str, Any]:
        """Validate schema, enforce risk & channel policy, and run tool within timeout."""
        t0 = time.perf_counter()
        spec = self.get(name)
        if spec is None:
            return {"status": "error", "error": "unknown_tool", "message": f"Unknown tool '{name}'."}

        try:
            args = validate_against_schema(spec.parameters, raw_args)
        except ToolValidationError as err:
            dur = (time.perf_counter() - t0) * 1000
            log_agent("tool_validate", dur, "invalid", tool=name, channel=ctx.channel)
            return {"status": "validation_error", "error": "validation_error", "message": str(err)}

        preflight_res = spec.preflight(args, ctx) if spec.preflight else None
        decision = evaluate_tool_risk(spec, args, ctx, preflight=preflight_res)
        if not decision.allowed:
            dur = (time.perf_counter() - t0) * 1000
            if decision.requires_confirmation:
                log_agent("tool_policy", dur, "confirm_required", tool=name, reason=decision.reason)
                return {
                    "status": "needs_confirmation",
                    "error": "confirmation_required",
                    "reason": decision.reason,
                    "action_label": decision.action_label,
                    "message": decision.confirmation_prompt,
                    "tool_name": name,
                    "tool_args": args,
                }
            log_agent("tool_policy", dur, "blocked", tool=name, reason=decision.reason)
            return {
                "status": "blocked",
                "error": decision.reason,
                "message": decision.blocked_message,
                **decision.extra,
            }

        try:
            res = await asyncio.wait_for(spec.handler(args, ctx), timeout=float(spec.timeout_s))
            if not isinstance(res, dict):
                res = {"status": "success", "result": res}
            dur = (time.perf_counter() - t0) * 1000
            log_agent("tool_exec", dur, str(res.get("status") or "ok"), tool=name, channel=ctx.channel)
            return res
        except asyncio.TimeoutError:
            dur = (time.perf_counter() - t0) * 1000
            log_agent("tool_exec", dur, "timeout", tool=name, channel=ctx.channel)
            return {
                "status": "timeout",
                "error": "tool_timeout",
                "message": f"'{name}' timed out after {spec.timeout_s}s.",
            }
        except Exception as err:
            dur = (time.perf_counter() - t0) * 1000
            log_agent("tool_exec", dur, "error", tool=name, err=type(err).__name__)
            return {
                "status": "error",
                "error": type(err).__name__,
                "message": f"Failed running '{name}': {err}",
            }

