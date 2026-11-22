"""Unit tests for Phase 4: Plan -> Act -> Check loop for Tier 2 and escalated turns."""

import asyncio
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from core.agent.llm_gateway import LLMResponse, LLMToolCall
from core.agent.orchestrator import AgentOrchestrator
from core.agent.telemetry import (
    CHECK_VERDICTS,
    log_agent,
    normalize_check_verdict,
)
from core.agent.tools.registry import ToolRegistry, ToolSpec


class TestPhase4PlanActCheck(unittest.TestCase):
    """Test suite verifying Plan -> Act -> Check loop, recovery, timeouts, and non-PII logging."""

    def setUp(self):
        self.orig_env = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.orig_env)

    def test_env_config_and_voice_timeout_invariance(self):
        """Test env configuration for max steps, text timeout, and voice invariant 55s."""
        # 1. Defaults
        os.environ.pop("AIVA_TIER2_MAX_STEPS", None)
        os.environ.pop("AIVA_TEXT_TOTAL_TIMEOUT_S", None)
        os.environ.pop("AIVA_ENABLE_PLANNING", None)
        os.environ.pop("AIVA_ENABLE_SELF_CHECK", None)

        self.assertEqual(AgentOrchestrator.get_tier2_max_steps(), 6)
        self.assertEqual(AgentOrchestrator.get_text_total_timeout_s(), 55.0)
        self.assertTrue(AgentOrchestrator.is_planning_enabled())
        self.assertTrue(AgentOrchestrator.is_self_check_enabled())

        # 2. Configured values
        os.environ["AIVA_TIER2_MAX_STEPS"] = "8"
        os.environ["AIVA_TEXT_TOTAL_TIMEOUT_S"] = "90"
        os.environ["AIVA_ENABLE_PLANNING"] = "0"
        os.environ["AIVA_ENABLE_SELF_CHECK"] = "off"

        self.assertEqual(AgentOrchestrator.get_tier2_max_steps(), 8)
        self.assertEqual(AgentOrchestrator.get_text_total_timeout_s(), 90.0)
        self.assertFalse(AgentOrchestrator.is_planning_enabled())
        self.assertFalse(AgentOrchestrator.is_self_check_enabled())

    def test_verdict_normalization_and_non_pii_logging(self):
        """Test check verdict normalization and non-PII telemetry logging."""
        self.assertEqual(normalize_check_verdict("answers"), "answers")
        self.assertEqual(normalize_check_verdict("partial"), "partial")
        self.assertEqual(normalize_check_verdict("wrong"), "wrong")
        self.assertEqual(normalize_check_verdict("Verdict: partial\nExplanation: missed date"), "partial")
        self.assertEqual(normalize_check_verdict("Verdict: WRONG"), "wrong")
        self.assertEqual(normalize_check_verdict("random gibberish"), "answers")
        self.assertEqual(normalize_check_verdict(None), "answers")

        # Verify log_agent strictly strips forbidden keys and logs normalized verdict enum
        line = log_agent(
            stage="check",
            duration_ms=15.4,
            status="ok",
            verdict="Verdict: partial",
            draft_answer="SECRET DRAFT DO NOT LOG",
            reason="CONFIDENTIAL REASON",
            prompt="SECRET PROMPT",
        )
        self.assertIn("stage=check", line)
        self.assertIn("verdict=partial", line)
        self.assertIn("duration_ms=15.4", line)
        self.assertNotIn("SECRET", line)
        self.assertNotIn("CONFIDENTIAL", line)
        self.assertNotIn("draft_answer", line)

        # Plan logging
        line_plan = log_agent(
            stage="plan",
            duration_ms=8.2,
            status="ok",
            steps=3,
            plan=["Step 1 secret", "Step 2 secret"],
        )
        self.assertIn("stage=plan", line_plan)
        self.assertIn("steps=3", line_plan)
        self.assertNotIn("secret", line_plan)

    def test_plan_emission_on_tier2_and_skip_on_tier1(self):
        """Test plan emits on Tier 2 / escalated turns, and is completely skipped on Tier 1."""
        reg = ToolRegistry()
        llm = MagicMock()

        orch = AgentOrchestrator(registry=reg, llm_gateway=llm)

        # Case 1: Tier 1 simple turn -> planning MUST NOT run
        events_t1 = []

        async def _t1_events(e):
            events_t1.append(e)

        llm.complete = AsyncMock(return_value=LLMResponse(text="Simple answer", provider="mock", model="m1"))
        res_t1 = asyncio.run(
            orch.handle_turn(
                "what is the capital of France",
                user_id="u_t1",
                channel="hud",
                on_event=_t1_events,
            )
        )
        self.assertEqual(res_t1["response"], "Simple answer")
        self.assertFalse(any(e.get("type") == "plan" for e in events_t1))

        # Case 2: Explicit Tier 2 turn -> plan MUST emit before tools act
        events_t2 = []

        async def _t2_events(e):
            events_t2.append(e)

        async def _mock_tier2_complete(messages, **kwargs):
            sys_msg = str(messages[0].get("content", "")) if messages else ""
            if "planning module" in sys_msg:
                return LLMResponse(
                    text='{"plan": ["Analyze codebase", "Search documentation", "Synthesize findings"]}',
                    provider="mock",
                    model="m2",
                )
            return LLMResponse(text="Detailed deep research findings.", provider="mock", model="m2")

        llm.complete = AsyncMock(side_effect=_mock_tier2_complete)
        # Deep research query routes to Tier 2
        res_t2 = asyncio.run(
            orch.handle_turn(
                "deep research on distributed vector indexing algorithms",
                user_id="u_t2",
                channel="hud",
                on_event=_t2_events,
            )
        )
        self.assertTrue(any(e.get("type") == "plan" for e in events_t2))
        plan_ev = next(e for e in events_t2 if e.get("type") == "plan")
        self.assertEqual(len(plan_ev["steps"]), 3)

    def test_self_check_verdict_and_single_recovery_attempt(self):
        """Test self-check call on Tier 2 with partial verdict triggers ONE recovery attempt."""
        reg = ToolRegistry()

        # Register a test tool
        async def _mock_tool_exec(args, ctx):
            return {"status": "success", "data": "Initial partial data."}

        spec = ToolSpec(
            name="data_fetcher",
            description="Fetches data",
            parameters={"type": "object", "properties": {}},
            handler=_mock_tool_exec,
        )
        reg.register(spec)

        llm = MagicMock()
        orch = AgentOrchestrator(registry=reg, llm_gateway=llm)

        call_count = {"llm": 0, "check": 0}

        async def _mock_complete(messages, **kwargs):
            sys_msg = str(messages[0].get("content", "")) if messages else ""
            call_count["llm"] += 1

            # Planning call
            if "planning module" in sys_msg:
                return LLMResponse(text='{"plan": ["Fetch data", "Check completeness"]}', provider="mock")

            # Self-check evaluator call
            if "critical answer evaluator" in sys_msg or "verdict: answers | partial | wrong" in sys_msg:
                call_count["check"] += 1
                return LLMResponse(text="verdict: partial", provider="mock")

            # Check if this is the recovery attempt
            last_user = str(messages[-1].get("content", ""))
            if "Self-check determined the answer was partial" in last_user:
                return LLMResponse(
                    text="Honest answer: Found initial data, but complete records could not be retrieved.",
                    provider="mock",
                )

            # Round 1: Call data_fetcher tool
            if not any(m.get("role") == "assistant" and "Calling tools" in str(m.get("content", "")) for m in messages):
                return LLMResponse(
                    tool_calls=[LLMToolCall(id="tc_df", name="data_fetcher", arguments={})],
                    provider="mock",
                )

            # Round 2: Return first draft answer
            return LLMResponse(text="Here is the initial draft.", provider="mock")

        llm.complete = AsyncMock(side_effect=_mock_complete)

        res = asyncio.run(
            orch.handle_turn(
                "deep research on missing data records",
                user_id="u_rec",
                channel="hud",
            )
        )
        # Check that self-check ran exactly ONCE
        self.assertEqual(call_count["check"], 1)
        # Check that the recovery attempt answered honestly
        self.assertIn("Honest answer", res["response"])

    def test_tool_failure_recovery_hint(self):
        """Test tool returning empty/error injects recovery hint into execution context."""
        reg = ToolRegistry()

        # Tool returns empty
        async def _empty_tool_exec(args, ctx):
            return {"status": "success", "results": []}

        reg.register(
            ToolSpec(
                name="empty_searcher",
                description="Searcher returning empty",
                parameters={"type": "object", "properties": {}},
                handler=_empty_tool_exec,
            )
        )

        llm = MagicMock()
        orch = AgentOrchestrator(registry=reg, llm_gateway=llm)

        captured_messages = []

        async def _mock_complete(messages, **kwargs):
            sys_msg = str(messages[0].get("content", "")) if messages else ""
            if "planning module" in sys_msg:
                return LLMResponse(text='{"plan": ["Search"]}', provider="mock")

            # Check if tool execution results are in messages
            for m in messages:
                if str(m.get("content", "")).startswith("Tool execution results:"):
                    captured_messages.append(m["content"])

            if not captured_messages:
                return LLMResponse(
                    tool_calls=[LLMToolCall(id="tc_es", name="empty_searcher", arguments={})],
                    provider="mock",
                )

            return LLMResponse(text="No results were found after trying.", provider="mock")

        llm.complete = AsyncMock(side_effect=_mock_complete)

        res = asyncio.run(
            orch.handle_turn(
                "deep research search for nonexistent data",
                user_id="u_empty",
                channel="hud",
            )
        )
        self.assertTrue(len(captured_messages) > 0)
        self.assertIn("[Tool Recovery Hint]", captured_messages[0])


if __name__ == "__main__":
    unittest.main()

