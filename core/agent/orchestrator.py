"""Agent Core loop orchestrating Tier 0/1/2 routing, parallel tool calls, policy & streaming."""

import asyncio
import json
import os
import re
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from core.agent.policy import (
    fence_tool_result,
    is_cancel_phrase,
    is_confirmation_phrase,
)
from core.agent.prompts import (
    get_examples_content,
    get_tier1_prompt_template,
    get_tier2_prompt_template,
)
from core.agent.router import TieredRouter
from core.agent.shaper import normalize_sources, shape_response
from core.agent.telemetry import (
    TelemetryRecorder,
    log_agent,
    normalize_check_verdict,
    normalize_escalation_reason,
)
from core.agent.tools.registry import ToolContext, ToolRegistry


def _derive_tag(tools_used: List[str], hint: str = "conversation") -> str:
    """Derive telemetry category strictly from execution outcome rather than guesses."""
    if not tools_used:
        return hint or "conversation"
    if "web_research" in tools_used:
        return "research"
    if any(t in ("recall", "create_reminder") for t in tools_used):
        return "memory"
    return "action"


class AgentOrchestrator:
    """Async agent loop with <=6 steps, timeouts, parallel read/low tools, policy & memory pinning."""

    MAX_STEPS = 6
    PER_STEP_TIMEOUT_S = 20.0
    TOTAL_TIMEOUT_S = 55.0

    @classmethod
    def get_tier2_max_steps(cls) -> int:
        try:
            val = int(os.environ.get("AIVA_TIER2_MAX_STEPS", str(cls.MAX_STEPS)))
            return max(1, min(val, 20))
        except (ValueError, TypeError):
            return cls.MAX_STEPS

    @classmethod
    def get_text_total_timeout_s(cls) -> float:
        try:
            val = float(os.environ.get("AIVA_TEXT_TOTAL_TIMEOUT_S", str(cls.TOTAL_TIMEOUT_S)))
            return max(5.0, min(val, 300.0))
        except (ValueError, TypeError):
            return cls.TOTAL_TIMEOUT_S

    @classmethod
    def is_planning_enabled(cls) -> bool:
        return os.environ.get("AIVA_ENABLE_PLANNING", "1").strip().lower() not in ("0", "false", "no", "off")

    @classmethod
    def is_self_check_enabled(cls) -> bool:
        return os.environ.get("AIVA_ENABLE_SELF_CHECK", "1").strip().lower() not in ("0", "false", "no", "off")

    @classmethod
    def get_tier1_max_prompt_tokens(cls) -> int:
        try:
            val = int(os.environ.get("AIVA_TIER1_MAX_PROMPT_TOKENS", "3072"))
            return max(512, min(val, 16384))
        except (ValueError, TypeError):
            return 3072

    @classmethod
    def get_tier2_max_prompt_tokens(cls) -> int:
        try:
            val = int(os.environ.get("AIVA_TIER2_MAX_PROMPT_TOKENS", "6144"))
            return max(1024, min(val, 32768))
        except (ValueError, TypeError):
            return 6144

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        """Heuristic estimation of token count (~4 characters per token)."""
        return max(1, len(text) // 4)

    @classmethod
    def cap_prompt_messages(cls, messages: List[Dict[str, Any]], max_tokens: int) -> List[Dict[str, Any]]:
        """Cap total prompt tokens per tier by trimming older history, keeping system prompt and user input."""
        if not messages or len(messages) <= 2:
            return messages

        total_est = sum(cls._estimate_tokens(str(m.get("content", ""))) for m in messages)
        if total_est <= max_tokens:
            return messages

        # Preserve messages[0] (system prompt) and messages[-1] (latest user message / tool results)
        sys_msg = messages[0]
        latest_msg = messages[-1]
        mid = list(messages[1:-1])

        while mid and sum(cls._estimate_tokens(str(m.get("content", ""))) for m in [sys_msg] + mid + [latest_msg]) > max_tokens:
            mid.pop(0)  # Evict oldest history message

        return [sys_msg] + mid + [latest_msg]

    async def _generate_plan(
        self,
        query: str,
        *,
        tier: int = 2,
        timeout_s: float = 15.0,
    ) -> List[str]:
        """Emit a short structured JSON plan (max 5 steps) on Tier 2 / escalated turns."""
        plan_prompt = [
            {
                "role": "system",
                "content": (
                    "You are AIVA's planning module. Produce a concise, actionable execution plan "
                    "for the user's task as a JSON object with a single 'plan' key containing a list "
                    "of at most 5 brief steps. Output ONLY valid JSON in format: "
                    '{"plan": ["step 1", "step 2", ...]}'
                ),
            },
            {
                "role": "user",
                "content": f"Task: {query}",
            },
        ]
        try:
            resp = await self.llm_gateway.complete(
                plan_prompt,
                tier=tier,
                reasoning_effort="low",
                stream=False,
                timeout_s=timeout_s,
            )
            text = (resp.text or "").strip()
            if "```" in text:
                m_fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
                if m_fence:
                    text = m_fence.group(1).strip()
            m_obj = re.search(r"\{.*\}", text, re.DOTALL)
            if m_obj:
                data = json.loads(m_obj.group(0))
                if isinstance(data, dict) and "plan" in data and isinstance(data["plan"], list):
                    clean = [str(s).strip() for s in data["plan"] if str(s).strip()]
                    return clean[:5]
        except Exception as err:
            log_agent("plan", 0.0, "error", err=type(err).__name__)
        return []

    async def _run_self_check(
        self,
        *,
        user_input: str,
        draft_text: str,
        tool_results_summary: str,
        tier: int = 2,
        timeout_s: float = 12.0,
    ) -> Tuple[str, float]:
        """Run ONE self-check call on Tier 2: verdict: answers | partial | wrong."""
        t_c0 = time.perf_counter()
        prompt = [
            {
                "role": "system",
                "content": (
                    "Evaluate whether the draft answer fully answers the user's request using the tool results.\n"
                    "Respond with ONE line in the format:\n"
                    "verdict: answers | partial | wrong"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"User request: {user_input}\n\n"
                    f"Tool results summary: {tool_results_summary[:1500]}\n\n"
                    f"Draft answer: {draft_text[:1500]}\n\n"
                    "Does the draft answer fully answer the user's request using the tool results? verdict: answers | partial | wrong"
                ),
            },
        ]
        try:
            resp = await self.llm_gateway.complete(
                prompt,
                tier=tier,
                reasoning_effort="low",
                stream=False,
                timeout_s=timeout_s,
            )
            check_ms = (time.perf_counter() - t_c0) * 1000
            verdict = normalize_check_verdict(resp.text)
            return verdict, check_ms
        except Exception as err:
            check_ms = (time.perf_counter() - t_c0) * 1000
            log_agent("check", duration_ms=check_ms, status="error", err=type(err).__name__)
            return "answers", check_ms

    def __init__(
        self,
        *,
        registry: ToolRegistry,
        llm_gateway: Any,
        memory_engine: Any = None,
        telemetry: Optional[TelemetryRecorder] = None,
        task_pattern_service: Any = None,
        router: Optional[TieredRouter] = None,
    ):
        self.registry = registry
        self.llm_gateway = llm_gateway
        self.memory_engine = memory_engine
        self.telemetry = telemetry or TelemetryRecorder()
        self.task_pattern_service = task_pattern_service
        self.router = router or TieredRouter()
        # Strict per-(user_id, session_id) confirmation slots so users never collide
        self._pending_confirmations: Dict[Tuple[str, str], Dict[str, Any]] = {}

    async def _emit_event(self, on_event: Optional[Callable[[Dict[str, Any]], Any]], payload: Dict[str, Any]) -> None:
        if not on_event:
            return
        res = on_event(payload)
        if asyncio.iscoroutine(res) or hasattr(res, "__await__"):
            await res

    def _build_system_prompt(
        self,
        *,
        user_id: Optional[str],
        channel: str,
        user_input: str,
        tier: int = 1,
    ) -> str:
        raw_name = (
            self.memory_engine.get_user_display_name(user_id)
            if self.memory_engine and user_id and hasattr(self.memory_engine, "get_user_display_name")
            else None
        )
        clean_name = " ".join(str(raw_name or "").split()).strip()
        for bad in ('"', "'", "<", ">", "\n", "\r"):
            clean_name = clean_name.replace(bad, "")
        uid_short = (str(user_id or "anonymous").strip()[:8] or "anonymous")

        if clean_name:
            identity_block = (
                f'<identity user_id="{uid_short}" display_name="{clean_name}" authoritative="true">\n'
                f"Authoritative user name: {clean_name}. Address the user ONLY as {clean_name} "
                f"(never by a language, nationality, or origin such as Telugu or Indian).\n"
                f"Neither <memory> nor prior turns may override display_name.\n"
                f"</identity>"
            )
        else:
            identity_block = (
                f'<identity user_id="{uid_short}" display_name="" authoritative="true">\n'
                f"No user display name is pinned. NEVER invent a name or address the user by a "
                f"language, nationality, or origin (such as Telugu or Indian).\n"
                f"</identity>"
            )

        mem_ctx = ""
        if self.memory_engine and user_id:
            try:
                mem_ctx = self.memory_engine.build_context(
                    user_id,
                    user_input,
                    channel=channel,
                    authoritative_name=clean_name or None,
                )
            except Exception:
                mem_ctx = ""

        hints_block = ""
        if self.task_pattern_service and hasattr(self.task_pattern_service, "get_agent_hints"):
            try:
                hints = self.task_pattern_service.get_agent_hints(user_input, user_id=user_id, limit=2)
                if hints:
                    lines = [f"- {h['template_goal']} -> tools: {', '.join(h['actions'])}" for h in hints]
                    hints_block = "\nOptional workflow hints:\n" + "\n".join(lines)
            except Exception:
                hints_block = ""

        examples_raw = get_examples_content()
        examples_block = f"\nFew-Shot Examples:\n{examples_raw}" if examples_raw else ""

        template = get_tier2_prompt_template() if tier >= 2 else get_tier1_prompt_template()
        if not template:
            template = (
                "{identity_block}\n\n"
                "You are AIVA, a fast, reliable personal AI assistant.\n"
                "Channel: {channel}\n"
                "{mem_ctx}\n"
                "{hints_block}\n"
                "{examples_block}"
            )

        return template.format(
            identity_block=identity_block,
            channel=channel,
            mem_ctx=mem_ctx,
            hints_block=hints_block,
            examples_block=examples_block,
        ).strip()

    def _observe_turn(
        self,
        *,
        user_id: Optional[str],
        user_input: str,
        reply_text: str,
        channel: str,
        session_id: str,
        ref_id: Optional[str] = None,
        is_tool_output: bool = False,
    ) -> None:
        if self.memory_engine and user_id and reply_text:
            try:
                self.memory_engine.observe(
                    user_id,
                    {
                        "user": user_input,
                        "assistant": reply_text,
                        "channel": channel,
                        "ref_id": ref_id,
                        "is_tool_output": is_tool_output,
                    },
                    channel=channel,
                    session_id=session_id,
                )
            except Exception as err:
                log_agent("observe_turn", 0.0, "error", err=type(err).__name__)

    async def handle_turn(
        self,
        user_input: str,
        *,
        user_id: Optional[str],
        channel: str = "hud",
        session_id: str = "default",
        turn_origin: str = "user_explicit",
        device_name: Optional[str] = None,
        on_event: Optional[Callable[[Dict[str, Any]], Any]] = None,
    ) -> Dict[str, Any]:
        """Run one full user turn through confirmation check, tiered routing, and agent loop."""
        t0 = time.perf_counter()
        raw_in = (user_input or "").strip()
        uid_key = str(user_id or "anonymous")
        sess_key = str(session_id or "default")
        ctx_key = (uid_key, sess_key)

        if not raw_in:
            return {"response": "", "should_continue": True, "should_end_session": False, "route": "empty"}

        if raw_in.lower() in {"exit", "quit", "close aiva"}:
            return {
                "response": "Goodbye! Have a great day.",
                "speech_text": "Goodbye! Have a great day.",
                "should_continue": False,
                "should_end_session": True,
                "route": "exit",
            }

        # 1. Pending destructive/untrusted confirmation handling (strictly scoped by user_id + session_id)
        pending = self._pending_confirmations.get(ctx_key)
        if pending is not None:
            self._pending_confirmations.pop(ctx_key, None)
            if pending.get("expires_at") and time.time() > float(pending["expires_at"]):
                if is_confirmation_phrase(raw_in):
                    shaped = shape_response(
                        "That confirmation timed out after 60 seconds. Please ask again if you still want to run it.",
                        channel=channel,
                    )
                    total_ms = (time.perf_counter() - t0) * 1000
                    self.telemetry.record(user_id=user_id, tier=0, channel=channel, tag="conversation", total_ms=total_ms)
                    return {
                        **shaped,
                        "response": shaped["text"],
                        "tier": 0,
                        "tag": "conversation",
                        "tools": [],
                        "route": "confirmation_expired",
                        "should_continue": True,
                        "should_end_session": False,
                    }
            elif is_confirmation_phrase(raw_in):
                await self._emit_event(on_event, {"type": "status", "status": "executing confirmed action"})
                t_tool0 = time.perf_counter()
                res = await self.registry.execute(
                    pending["tool_name"],
                    pending["tool_args"],
                    ToolContext(
                        user_id=user_id,
                        channel=channel,
                        session_id=session_id,
                        confirmed=True,
                        turn_origin=turn_origin,
                        device_name=device_name,
                    ),
                )
                tool_ms = (time.perf_counter() - t_tool0) * 1000
                msg = res.get("message") or res.get("result") or f"Done: {pending['tool_name']}."
                shaped = shape_response(msg, channel=channel)
                total_ms = (time.perf_counter() - t0) * 1000
                self.telemetry.record(
                    user_id=user_id, tier=0, channel=channel, tag="action",
                    tool_time_ms=tool_ms, total_ms=total_ms, tools=[pending["tool_name"]],
                    success=res.get("status") == "success",
                )
                self._observe_turn(
                    user_id=user_id, user_input=raw_in, reply_text=shaped["text"],
                    channel=channel, session_id=session_id,
                )
                return {
                    **shaped,
                    "response": shaped["text"],
                    "tier": 0,
                    "tag": "action",
                    "tools": [pending["tool_name"]],
                    "route": "task",
                    "should_continue": True,
                    "should_end_session": False,
                    "timings": {"total_ms": round(total_ms, 1), "tool_time_ms": round(tool_ms, 1)},
                }
            if is_cancel_phrase(raw_in):
                shaped = shape_response("Action cancelled. Standing by.", channel=channel)
                total_ms = (time.perf_counter() - t0) * 1000
                self.telemetry.record(user_id=user_id, tier=0, channel=channel, tag="conversation", total_ms=total_ms)
                return {
                    **shaped,
                    "response": shaped["text"],
                    "tier": 0,
                    "tag": "conversation",
                    "tools": [],
                    "route": "conversation",
                    "should_continue": True,
                    "should_end_session": False,
                }

        # 2. Hot-path memory & identity commands (<10ms deterministic)
        if self.memory_engine and user_id:
            if hasattr(self.memory_engine, "has_pending_confirmation") and self.memory_engine.has_pending_confirmation(user_id):
                res = self.memory_engine.handle_memory_intent(user_id, raw_in, channel=channel)
                shaped = shape_response(res.get("response", "Done."), channel=channel)
                total_ms = (time.perf_counter() - t0) * 1000
                self.telemetry.record(user_id=user_id, tier=0, channel=channel, tag="memory", total_ms=total_ms)
                return {
                    **shaped, "response": shaped["text"], "tier": 0, "tag": "memory", "tools": [],
                    "route": "memory", "should_continue": True, "should_end_session": False,
                }
            due_reply = self.memory_engine.extractor.try_resolve_pending_commitment_time(user_id, raw_in, channel=channel)
            if due_reply:
                shaped = shape_response(due_reply, channel=channel)
                total_ms = (time.perf_counter() - t0) * 1000
                self.telemetry.record(user_id=user_id, tier=0, channel=channel, tag="memory", total_ms=total_ms)
                return {
                    **shaped, "response": shaped["text"], "tier": 0, "tag": "memory", "tools": [],
                    "route": "memory", "should_continue": True, "should_end_session": False,
                }
            hot_mem = self.memory_engine.handle_memory_intent(
                user_id, raw_in, channel=channel, explicit_only=True
            )
            if hot_mem and hot_mem.get("handled"):
                shaped = shape_response(hot_mem["response"], channel=channel)
                total_ms = (time.perf_counter() - t0) * 1000
                self.telemetry.record(user_id=user_id, tier=0, channel=channel, tag="memory", total_ms=total_ms)
                return {
                    **shaped, "response": shaped["text"], "tier": 0, "tag": "memory", "tools": [],
                    "route": "memory", "should_continue": True, "should_end_session": False,
                }

        # 3. Tiered Routing (<10ms)
        route_dec = self.router.route(raw_in)

        if route_dec.tier == 0:
            if route_dec.direct_response:
                shaped = shape_response(route_dec.direct_response, channel=channel)
                total_ms = (time.perf_counter() - t0) * 1000
                self.telemetry.record(
                    user_id=user_id, tier=0, channel=channel, tag=route_dec.tag_hint,
                    route_ms=route_dec.route_ms, total_ms=total_ms, success=True,
                )
                self._observe_turn(user_id=user_id, user_input=raw_in, reply_text=shaped["text"], channel=channel, session_id=session_id)
                return {
                    **shaped, "response": shaped["text"], "tier": 0, "tag": route_dec.tag_hint,
                    "tools": [], "route": "conversation", "should_continue": True, "should_end_session": False,
                    "timings": {"route_ms": route_dec.route_ms, "total_ms": round(total_ms, 1)},
                }

            if route_dec.tool_name:
                t_tool0 = time.perf_counter()
                res = await self.registry.execute(
                    route_dec.tool_name,
                    route_dec.tool_args or {},
                    ToolContext(
                        user_id=user_id,
                        channel=channel,
                        session_id=session_id,
                        turn_origin=turn_origin,
                        device_name=device_name,
                    ),
                )
                tool_ms = (time.perf_counter() - t_tool0) * 1000
                if res.get("status") == "needs_confirmation":
                    self._pending_confirmations[ctx_key] = {
                        "tool_name": route_dec.tool_name,
                        "tool_args": route_dec.tool_args or {},
                        "expires_at": time.time() + 60.0,
                    }
                msg = res.get("message") or res.get("result") or f"Completed {route_dec.tool_name}."
                shaped = shape_response(msg, channel=channel)
                total_ms = (time.perf_counter() - t0) * 1000
                ok = res.get("status") not in ("error", "blocked", "timeout")
                self.telemetry.record(
                    user_id=user_id, tier=0, channel=channel, tag="action",
                    route_ms=route_dec.route_ms, tool_time_ms=tool_ms, total_ms=total_ms,
                    tools=[route_dec.tool_name], success=ok,
                )
                self._observe_turn(user_id=user_id, user_input=raw_in, reply_text=shaped["text"], channel=channel, session_id=session_id)
                return {
                    **shaped, "response": shaped["text"], "tier": 0, "tag": "action",
                    "tools": [route_dec.tool_name], "route": "task",
                    "should_continue": True, "should_end_session": False,
                    "expect_followup": True,
                    "timings": {"route_ms": route_dec.route_ms, "tool_time_ms": round(tool_ms, 1), "total_ms": round(total_ms, 1)},
                }

        # 4. Tier 1 / Tier 2 Agent Loop (<= MAX_STEPS)
        current_tier = route_dec.tier
        sys_prompt = self._build_system_prompt(user_id=user_id, channel=channel, user_input=raw_in, tier=current_tier)
        history = []
        if self.memory_engine and user_id:
            try:
                history = self.memory_engine.get_working_messages(user_id, session_id=session_id, limit=6) or []
            except Exception:
                history = []

        messages: List[Dict[str, Any]] = [{"role": "system", "content": sys_prompt}]
        messages.extend(history[-6:])
        messages.append({"role": "user", "content": raw_in})

        channel_tools = self.registry.to_openai_tools(channel=channel)
        tools_used: List[str] = []
        tool_result_blocks: List[str] = []
        collected_sources: List[Dict[str, Any]] = []
        ref_id: Optional[str] = None
        tool_time_ms = 0.0
        first_token_ms = 0.0
        validation_errors = 0
        untrusted_taint = False
        research_calls_done = 0
        final_text = ""
        tier1_reasoning = (
            self.llm_gateway.get_tier1_reasoning()
            if hasattr(self.llm_gateway, "get_tier1_reasoning")
            else "low"
        )

        await self._emit_event(on_event, {"type": "status", "status": "thinking"})

        total_timeout_s = (
            self.TOTAL_TIMEOUT_S
            if str(channel).lower() == "voice"
            else self.get_text_total_timeout_s()
        )
        max_steps = (
            self.get_tier2_max_steps()
            if current_tier >= 2
            else self.MAX_STEPS
        )

        plan_done = False
        check_done = False
        recovery_attempted = False
        tool_recovery_hint_given = False

        for step in range(max_steps):
            elapsed_s = time.perf_counter() - t0
            remaining_s = total_timeout_s - elapsed_s
            if remaining_s <= 0.5:
                final_text = final_text or "Sorry, that request took longer than expected. Please try again."
                break

            # Requirement 1 & 5: On Tier 2 / escalated turns, emit SHORT plan (max 5 steps, JSON) before acting
            if current_tier >= 2 and not plan_done and self.is_planning_enabled() and remaining_s > 3.0:
                t_p0 = time.perf_counter()
                plan_steps = await self._generate_plan(raw_in, tier=current_tier, timeout_s=min(12.0, remaining_s))
                plan_ms = (time.perf_counter() - t_p0) * 1000
                plan_done = True
                if plan_steps:
                    log_agent("plan", duration_ms=plan_ms, status="ok", steps=len(plan_steps))
                    await self._emit_event(on_event, {"type": "plan", "steps": plan_steps})
                    messages.append({
                        "role": "assistant",
                        "content": "Execution plan:\n" + "\n".join(f"{i+1}. {s}" for i, s in enumerate(plan_steps)),
                    })

            step_timeout = min(self.PER_STEP_TIMEOUT_S, remaining_s)
            stream_deltas: List[str] = []

            async def _on_delta(tok: str):
                stream_deltas.append(tok)
                await self._emit_event(on_event, {"type": "delta", "delta": tok})

            step_effort = tier1_reasoning if current_tier == 1 else (
                self.llm_gateway.get_tier2_reasoning()
                if hasattr(self.llm_gateway, "get_tier2_reasoning")
                else "medium"
            )

            max_prompt_tokens = (
                self.get_tier2_max_prompt_tokens()
                if current_tier >= 2
                else self.get_tier1_max_prompt_tokens()
            )
            capped_messages = self.cap_prompt_messages(messages, max_tokens=max_prompt_tokens)

            try:
                llm_resp = await self.llm_gateway.complete(
                    capped_messages,
                    tools=channel_tools,
                    tier=current_tier,
                    reasoning_effort=step_effort,
                    stream=True,
                    on_delta=_on_delta if on_event else None,
                    timeout_s=step_timeout,
                )
            except Exception as err:
                log_agent("agent_llm", (time.perf_counter() - t0) * 1000, "error", tier=current_tier, err=type(err).__name__)
                final_text = "I had trouble reaching my reasoning model right now. Please try again in a moment."
                break

            if first_token_ms == 0.0 and getattr(llm_resp, "first_token_ms", 0.0) > 0:
                first_token_ms = llm_resp.first_token_ms

            if not llm_resp.tool_calls:
                draft_text = (llm_resp.text or "").strip()
                if not draft_text:
                    draft_text = "Done."

                # Requirement 2 & 6: Run ONE self-check call on Tier 2 / escalated turns after tools finish
                rem_check_s = total_timeout_s - (time.perf_counter() - t0)
                executed_tools = [t for t in tools_used if t != "escalate"]
                if (
                    current_tier >= 2
                    and executed_tools
                    and not check_done
                    and self.is_self_check_enabled()
                    and rem_check_s > 3.0
                ):
                    summary_block = (
                        "\n".join(tool_result_blocks)[-1500:]
                        if tool_result_blocks
                        else f"Executed tools: {', '.join(executed_tools)}"
                    )
                    verdict, check_ms = await self._run_self_check(
                        user_input=raw_in,
                        draft_text=draft_text,
                        tool_results_summary=summary_block,
                        tier=current_tier,
                        timeout_s=min(10.0, rem_check_s),
                    )
                    log_agent("check", duration_ms=check_ms, status="ok", verdict=verdict)
                    check_done = True
                    if verdict in ("partial", "wrong") and not recovery_attempted:
                        recovery_attempted = True
                        messages.append({"role": "assistant", "content": draft_text})
                        messages.append({
                            "role": "user",
                            "content": (
                                f"Self-check determined the answer was {verdict}. Please make ONE recovery attempt "
                                "using a different approach (such as a rephrased search query, alternative keywords, or different tool). "
                                "If the requested information cannot be found or the request cannot be fully satisfied, "
                                "state honestly what was attempted and what could not be completed."
                            ),
                        })
                        continue

                final_text = draft_text
                break

            # Handle explicit escalation meta-tool first
            escalate_calls = [tc for tc in llm_resp.tool_calls if tc.name == "escalate"]
            exec_calls = [tc for tc in llm_resp.tool_calls if tc.name != "escalate"]
            if escalate_calls:
                raw_code = (
                    escalate_calls[0].arguments.get("reason_code")
                    or escalate_calls[0].arguments.get("reason")
                )
                reason_code = normalize_escalation_reason(raw_code)
                log_agent("escalate", duration_ms=0.0, status="ok", reason_code=reason_code)
                if "escalate" not in tools_used:
                    tools_used.append("escalate")
                await self._emit_event(on_event, {"type": "status", "status": "thinking deeply"})

                if current_tier < 2:
                    current_tier = 2
                    max_steps = max(max_steps, self.get_tier2_max_steps())
                    if not exec_calls:
                        messages.append({
                            "role": "assistant",
                            "content": f"Escalating to Tier 2 reasoning (reason_code: {reason_code}).",
                        })
                        messages.append({
                            "role": "user",
                            "content": (
                                f"[Tier Handoff] Request escalated to Tier 2 for deeper reasoning (reason_code: {reason_code}). "
                                "Please inspect the conversation and existing tool results, determine any further steps or synthesis needed, "
                                "and complete the request."
                            ),
                        })
                        continue
                else:
                    if not exec_calls:
                        messages.append({
                            "role": "user",
                            "content": (
                                "You are already on the strongest reasoning tier. Do not call escalate again; "
                                "give a direct, concise final answer from the tool outputs you already have."
                            ),
                        })
                        continue

            if not exec_calls:
                final_text = (llm_resp.text or "Done.").strip()
                break

            # Emit informative UI stage indicator
            if any(tc.name == "web_research" for tc in exec_calls):
                await self._emit_event(on_event, {"type": "status", "status": "searching"})
            elif any(tc.name in ("whatsapp_read", "whatsapp_search") for tc in exec_calls):
                await self._emit_event(on_event, {"type": "status", "status": "reading WhatsApp messages"})
            elif any(tc.name == "whatsapp_send" for tc in exec_calls):
                await self._emit_event(on_event, {"type": "status", "status": "sending message"})
            else:
                await self._emit_event(on_event, {"type": "status", "status": f"running {exec_calls[0].name}"})

            # Separate parallel-safe calls from sequential calls
            async def _run_one(tc) -> Tuple[Any, Dict[str, Any]]:
                spec = self.registry.get(tc.name)
                t_t0 = time.perf_counter()
                if tc.name == "web_research" and research_calls_done >= 1:
                    res = {
                        "status": "already_retrieved",
                        "instruction": (
                            "Web research results are already in the conversation above. "
                            "Do NOT call web_research again; invoke the next tool (such as whatsapp_send) or give your final answer."
                        ),
                    }
                    return tc, res, (time.perf_counter() - t_t0) * 1000, spec
                res = await self.registry.execute(
                    tc.name,
                    tc.arguments,
                    ToolContext(
                        user_id=user_id,
                        channel=channel,
                        session_id=session_id,
                        untrusted_taint=untrusted_taint,
                        turn_origin=turn_origin,
                        device_name=device_name,
                    ),
                )
                return tc, res, (time.perf_counter() - t_t0) * 1000, spec

            par_batch = [tc for tc in exec_calls if getattr(self.registry.get(tc.name), "parallel_safe", False)]
            seq_batch = [tc for tc in exec_calls if not getattr(self.registry.get(tc.name), "parallel_safe", False)]

            results_tuples = []
            if par_batch:
                results_tuples.extend(await asyncio.gather(*(_run_one(tc) for tc in par_batch)))
            for tc in seq_batch:
                results_tuples.append(await _run_one(tc))

            # Inspect outcomes
            early_return_prompt: Optional[str] = None
            step_tool_blocks: List[str] = []
            for tc, res, dur_ms, spec in results_tuples:
                tool_time_ms += dur_ms
                tools_used.append(tc.name)
                await self._emit_event(on_event, {"type": "tool", "name": tc.name, "status": res.get("status", "ok")})

                if res.get("status") == "needs_confirmation":
                    self._pending_confirmations[ctx_key] = {
                        "tool_name": res.get("tool_name") or tc.name,
                        "tool_args": res.get("tool_args") or tc.arguments,
                        "expires_at": time.time() + 60.0,
                    }
                    early_return_prompt = res.get("message") or "Please say 'confirm' to proceed."
                    break

                tool_status = res.get("status")
                if tool_status in ("ambiguous", "validation_error", "error", "failed"):
                    tier1_reasoning = "medium"

                if tool_status == "validation_error":
                    validation_errors += 1
                    if validation_errors >= 2 and current_tier < 2:
                        current_tier = 2

                if tc.name == "web_research":
                    if res.get("status") != "already_retrieved":
                        research_calls_done += 1
                    await self._emit_event(on_event, {"type": "status", "status": "reading sources"})
                    if res.get("sources"):
                        collected_sources.extend(res["sources"])
                    if res.get("ref_id"):
                        ref_id = res["ref_id"]

                is_untrusted = bool(getattr(spec, "untrusted_result", False))
                if is_untrusted and tc.name in ("whatsapp_read", "whatsapp_read_messages", "whatsapp_search"):
                    untrusted_taint = True
                fenced = fence_tool_result(tc.name, res, untrusted=is_untrusted)
                step_tool_blocks.append(fenced)
                tool_result_blocks.append(fenced)

            if early_return_prompt is not None:
                shaped = shape_response(early_return_prompt, channel=channel)
                total_ms = (time.perf_counter() - t0) * 1000
                self.telemetry.record(
                    user_id=user_id, tier=current_tier, channel=channel,
                    tag="action", route_ms=route_dec.route_ms, llm_first_token_ms=first_token_ms,
                    tool_time_ms=tool_time_ms, total_ms=total_ms, tools=tools_used, success=True,
                )
                self._observe_turn(user_id=user_id, user_input=raw_in, reply_text=shaped["text"], channel=channel, session_id=session_id)
                return {
                    **shaped, "response": shaped["text"], "tier": current_tier, "tag": "action",
                    "tools": tools_used, "route": "confirmation_required",
                    "should_continue": True, "should_end_session": False, "expect_followup": True,
                }

            # Requirement 3: Tool-failure recovery
            tool_had_failure_or_empty = False
            for tc, res, dur_ms, spec in results_tuples:
                st = res.get("status")
                if st in ("error", "failed", "blocked", "timeout", "empty"):
                    tool_had_failure_or_empty = True
                elif st == "success":
                    data = res.get("data") if "data" in res else (res.get("results") if "results" in res else (res.get("response") if "response" in res else res.get("message")))
                    if data is None or data == "" or data == [] or data == {}:
                        tool_had_failure_or_empty = True

            recovery_hint = ""
            if tool_had_failure_or_empty and not tool_recovery_hint_given:
                tool_recovery_hint_given = True
                recovery_hint = (
                    "\n\n[Tool Recovery Hint] One or more tools returned empty or encountered an error. "
                    "Try ONE alternative approach (such as a rephrased query or alternative tool) before giving up. "
                    "If no further approach is viable, answer honestly explaining what failed."
                )

            # Feed tool results back into conversation cleanly (compatible across Groq/Gemini/Mistral)
            called_summary = ", ".join(f"{tc.name}({tc.arguments})" for tc in exec_calls)
            messages.append({"role": "assistant", "content": llm_resp.text or f"Calling tools: {called_summary}"})
            messages.append({
                "role": "user",
                "content": "Tool execution results:\n" + "\n".join(step_tool_blocks) + recovery_hint,
            })
        else:
            final_text = final_text or "I hit my maximum reasoning steps for that request. Please break it into smaller steps."

        tag = _derive_tag(tools_used, hint=route_dec.tag_hint)
        final_text = (final_text or "").replace("\u202f", " ").replace("\u00a0", " ")
        norm_sources = normalize_sources(collected_sources)
        shaped = shape_response(final_text, channel=channel, sources=norm_sources)
        total_ms = (time.perf_counter() - t0) * 1000
        ok = bool(final_text) and not final_text.startswith("I had trouble reaching")

        self.telemetry.record(
            user_id=user_id,
            tier=current_tier,
            channel=channel,
            tag=tag,
            route_ms=route_dec.route_ms,
            llm_first_token_ms=first_token_ms,
            tool_time_ms=tool_time_ms,
            total_ms=total_ms,
            tools=tools_used,
            success=ok,
        )
        self._observe_turn(
            user_id=user_id,
            user_input=raw_in,
            reply_text=shaped["text"],
            channel=channel,
            session_id=session_id,
            ref_id=ref_id,
            is_tool_output=bool(norm_sources or len(shaped["text"]) > 360),
        )
        return {
            **shaped,
            "response": shaped["text"],
            "tier": current_tier,
            "tag": tag,
            "tools": tools_used,
            "route": "task" if tools_used else "conversation",
            "should_continue": True,
            "should_end_session": False,
            "expect_followup": bool(tools_used),
            "timings": {
                "route_ms": route_dec.route_ms,
                "llm_first_token_ms": round(first_token_ms, 1),
                "tool_time_ms": round(tool_time_ms, 1),
                "total_ms": round(total_ms, 1),
            },
        }
