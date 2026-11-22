"""Unit tests for Phase 2: Model-driven escalation, router tier discipline, and ambiguity handling."""

import asyncio
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.agent.llm_gateway import LLMResponse, LLMToolCall
from core.agent.orchestrator import AgentOrchestrator
from core.agent.router import TIER0_MAX_RULES, TieredRouter
from core.agent.telemetry import (
    ESCALATION_REASONS,
    TelemetryRecorder,
    log_agent,
    normalize_escalation_reason,
)
from core.agent.tools.builtin import register_builtin_tools
from core.agent.tools.registry import ToolContext, ToolRegistry
from core.execution.capability_registry import CapabilityRegistry
from services.database_service import DatabaseService


class TestPhase2Escalation(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="aiva_p2_test_")
        self.db_path = os.path.join(self.tmpdir, "test_p2.db")
        self.db = DatabaseService(self.db_path)
        self.user_id = "p2_user_test_1"
        self.db.create_user(self.user_id, "charan@example.com", "Charan")
        self.tel = TelemetryRecorder(self.db)
        self.caps = CapabilityRegistry()
        self.caps.register("launch_app", AsyncMock(return_value={"status": "success", "result": "Opened"}))

        self.registry = ToolRegistry()
        register_builtin_tools(
            self.registry,
            capability_registry=self.caps,
            db=self.db,
        )

    def tearDown(self):
        try:
            conn = getattr(getattr(self.db, "_local", None), "conn", None)
            if conn is not None:
                conn.close()
                self.db._local.conn = None
        except Exception:
            pass
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_router_rule_count_budget_and_tier_discipline(self):
        router = TieredRouter()
        self.assertLessEqual(router.RULE_COUNT, 15)
        self.assertLessEqual(router.RULE_COUNT, TIER0_MAX_RULES)

        # 1. Deterministic Tier 0 actions
        r_time = router.route("what time is it")
        self.assertEqual(r_time.tier, 0)
        self.assertTrue(r_time.direct_response)

        r_lock = router.route("lock the screen")
        self.assertEqual(r_lock.tier, 0)
        self.assertEqual(r_lock.tool_name, "power_control")

        r_snap = router.route("take a screenshot")
        self.assertEqual(r_snap.tier, 0)
        self.assertEqual(r_snap.tool_name, "screenshot")

        r_mute = router.route("mute volume")
        self.assertEqual(r_mute.tier, 0)
        self.assertEqual(r_mute.tool_name, "media_volume")

        r_open = router.route("open notepad")
        self.assertEqual(r_open.tier, 0)
        self.assertEqual(r_open.tool_name, "open_apps_urls")

        # 2. Multi-clause queries do NOT escalate to Tier 2 purely by clause count
        r_multi = router.route("open chrome and then open notepad and then play music")
        self.assertEqual(r_multi.tier, 1)

        # 3. Generic keywords do NOT escalate to Tier 2
        r_kw = router.route("research python tips and send to my friend")
        self.assertEqual(r_kw.tier, 1)

        # 4. Direct Tier 2 ONLY for explicit deep research and browser workflows
        r_deep = router.route("deep dive into PostgreSQL indexing internals")
        self.assertEqual(r_deep.tier, 2)

        r_browser = router.route("fill the form on the demo portal via browser automation")
        self.assertEqual(r_browser.tier, 2)

    def test_escalation_reason_normalization_and_telemetry(self):
        self.assertIn("multi_step_planning", ESCALATION_REASONS)
        self.assertIn("empty_contradictory_results", ESCALATION_REASONS)
        self.assertIn("unsure", ESCALATION_REASONS)
        self.assertIn("complex_synthesis", ESCALATION_REASONS)

        self.assertEqual(normalize_escalation_reason("multi_step_planning"), "multi_step_planning")
        self.assertEqual(normalize_escalation_reason("Multi Step Planning"), "multi_step_planning")
        self.assertEqual(normalize_escalation_reason("empty results"), "empty_contradictory_results")
        self.assertEqual(normalize_escalation_reason("I am unsure"), "unsure")
        self.assertEqual(normalize_escalation_reason("need deep synthesis"), "complex_synthesis")
        self.assertEqual(normalize_escalation_reason(""), "multi_step_planning")

        # Telemetry log formatting must strictly emit reason_code=<enum> and forbid free text
        line = log_agent(
            "escalate",
            duration_ms=0.0,
            status="ok",
            reason_code="multi_step_planning",
            reason="this is free text that should not appear",
            prompt="secret prompt",
        )
        self.assertIn("stage=escalate", line)
        self.assertIn("reason_code=multi_step_planning", line)
        self.assertNotIn("secret prompt", line)
        self.assertNotIn("free text", line)

    def test_escalate_tool_spec_and_execution(self):
        spec = self.registry.get("escalate")
        self.assertIsNotNone(spec)
        self.assertIn("multi_step_planning", spec.parameters["properties"]["reason_code"]["enum"])
        self.assertIn("reason_code", spec.parameters["required"])

        ctx = ToolContext(user_id=self.user_id, channel="hud")
        res = asyncio.run(self.registry.execute("escalate", {"reason_code": "unsure"}, ctx))
        self.assertEqual(res["status"], "escalate")
        self.assertEqual(res["reason_code"], "unsure")

    def test_orchestrator_escalation_handoff_and_continuation(self):
        step_calls = []

        async def fake_complete(messages, **kwargs):
            tier = kwargs.get("tier")
            step_calls.append((tier, len(messages)))
            sys_msg = str(messages[0].get("content", "")) if messages else ""
            if "planning module" in sys_msg:
                return LLMResponse(text='{"plan": ["Plan step 1", "Plan step 2"]}', provider="gemini", model="gemini-tier2")
            if tier == 1:
                # Step 1 on Tier 1 calls escalate
                return LLMResponse(
                    tool_calls=[
                        LLMToolCall(id="tc_esc", name="escalate", arguments={"reason_code": "multi_step_planning"}),
                    ],
                    provider="groq",
                    model="groq-tier1",
                )
            # Step on Tier 2 completes with answer
            self.assertTrue(any("[Tier Handoff]" in str(m.get("content", "")) for m in messages))
            return LLMResponse(
                text="Completed multi-step plan on Tier 2.",
                tool_calls=[],
                provider="gemini",
                model="gemini-tier2",
            )

        gw = MagicMock()
        gw.complete = AsyncMock(side_effect=fake_complete)
        orch = AgentOrchestrator(
            registry=self.registry,
            llm_gateway=gw,
            telemetry=self.tel,
        )

        outcome = asyncio.run(orch.handle_turn(
            "plan a multi-step database cutover",
            user_id=self.user_id,
            channel="hud",
        ))

        self.assertEqual(outcome["tier"], 2)
        self.assertEqual(step_calls[0][0], 1)  # Started on Tier 1
        self.assertEqual(step_calls[-1][0], 2)  # Escalated to Tier 2
        self.assertIn("Completed multi-step plan on Tier 2", outcome["response"])
        self.assertIn("escalate", outcome["tools"])


if __name__ == "__main__":
    unittest.main()

