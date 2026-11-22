"""Unit tests for Phase 3: Configurable, quota-aware model selection, cooldowns, and reasoning effort."""

import asyncio
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.agent.llm_gateway import LLMGateway, LLMResponse, LLMToolCall
from core.agent.orchestrator import AgentOrchestrator
from core.agent.telemetry import TelemetryRecorder
from core.agent.tools.builtin import register_builtin_tools
from core.agent.tools.registry import ToolRegistry
from core.execution.capability_registry import CapabilityRegistry
from services.database_service import DatabaseService


class TestPhase3ModelSelection(unittest.TestCase):
    def setUp(self):
        self._saved_env = {
            "AIVA_TIER1_MODELS": os.environ.get("AIVA_TIER1_MODELS"),
            "AIVA_TIER2_MODELS": os.environ.get("AIVA_TIER2_MODELS"),
            "AIVA_TIER1_REASONING": os.environ.get("AIVA_TIER1_REASONING"),
            "AIVA_TIER2_REASONING": os.environ.get("AIVA_TIER2_REASONING"),
            "AIVA_429_COOLDOWN_S": os.environ.get("AIVA_429_COOLDOWN_S"),
            "AIVA_COOLDOWN_SECONDS": os.environ.get("AIVA_COOLDOWN_SECONDS"),
        }
        for k in self._saved_env:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_env_defaults_match_current_values(self):
        """When no env vars are set, defaults must exactly equal current production values."""
        self.assertEqual(
            LLMGateway.get_tier1_models(),
            ["openai/gpt-oss-20b", "qwen/qwen3.8-27b", "openai/gpt-oss-120b"],
        )
        self.assertEqual(
            LLMGateway.get_tier2_models(),
            ["gemini-3-flash-preview", "gemini-3.1-flash-lite", "gemini-2.5-flash-lite"],
        )
        self.assertEqual(LLMGateway.get_tier1_reasoning(), "low")
        self.assertEqual(LLMGateway.get_tier2_reasoning(), "medium")
        self.assertEqual(LLMGateway.get_cooldown_seconds(), 60.0)

    def test_env_custom_models_and_reasoning_configuration(self):
        """Tier 1 & Tier 2 models, reasoning effort, and cooldown are env-configurable."""
        os.environ["AIVA_TIER1_MODELS"] = "custom-model-1, custom-model-2"
        os.environ["AIVA_TIER2_MODELS"] = "gemini-2.5-pro, gemini-3-flash-preview"
        os.environ["AIVA_TIER1_REASONING"] = "medium"
        os.environ["AIVA_TIER2_REASONING"] = "high"
        os.environ["AIVA_429_COOLDOWN_S"] = "90"

        self.assertEqual(LLMGateway.get_tier1_models(), ["custom-model-1", "custom-model-2"])
        # Stronger model put at front of Tier 2 via env only
        self.assertEqual(LLMGateway.get_tier2_models(), ["gemini-2.5-pro", "gemini-3-flash-preview"])
        self.assertEqual(LLMGateway.get_tier1_reasoning(), "medium")
        self.assertEqual(LLMGateway.get_tier2_reasoning(), "high")
        self.assertEqual(LLMGateway.get_cooldown_seconds(), 90.0)

    def test_cooldown_on_429_skips_exhausted_key(self):
        """On HTTP 429 / rate_limit, key cooldown is set and rotation skips exhausted key."""
        gateway = LLMGateway(secrets={"groq_api_keys": ["key_rate_limited", "key_healthy"]})
        self.assertEqual(len(gateway.groq_keys), 2)

        calls_made = []

        def mock_make_groq_client(api_key: str):
            client = MagicMock()
            calls_made.append(api_key)

            async def mock_create(**kwargs):
                if api_key == "key_rate_limited":
                    raise Exception("Error code: 429 - {'error': {'message': 'Rate limit reached'}}")
                # Healthy key returns valid response
                resp = MagicMock()
                resp.choices = [MagicMock()]
                resp.choices[0].message = MagicMock()
                resp.choices[0].message.content = "Answer from healthy key"
                resp.choices[0].message.tool_calls = []
                resp.choices[0].finish_reason = "stop"
                return resp

            client.chat.completions.create = AsyncMock(side_effect=mock_create)
            return client

        gateway._make_groq_client = mock_make_groq_client

        # Turn 1: key_rate_limited fails with 429, marks cooldown, falls over to key_healthy
        resp1 = asyncio.run(gateway._call_groq(
            messages=[{"role": "user", "content": "hi"}],
            tools=None,
            models=("openai/gpt-oss-20b",),
            tier=1,
            stream=False,
            on_delta=None,
            temperature=0.35,
            max_tokens=800,
        ))
        self.assertEqual(resp1.text, "Answer from healthy key")
        self.assertIn("key_rate_limited", calls_made)
        self.assertIn("key_healthy", calls_made)
        self.assertTrue(gateway.is_key_on_cooldown("groq", "key_rate_limited", "openai/gpt-oss-20b"))

        # Turn 2: key_rate_limited is on cooldown and MUST BE SKIPPED entirely
        calls_made.clear()
        resp2 = asyncio.run(gateway._call_groq(
            messages=[{"role": "user", "content": "hello again"}],
            tools=None,
            models=("openai/gpt-oss-20b",),
            tier=1,
            stream=False,
            on_delta=None,
            temperature=0.35,
            max_tokens=800,
        ))
        self.assertEqual(resp2.text, "Answer from healthy key")
        self.assertNotIn("key_rate_limited", calls_made)
        self.assertEqual(calls_made, ["key_healthy"])

    def test_fallback_model_ordering_on_404(self):
        """On 404 / model_not_found, gateway fast-falls over to next fallback model."""
        gateway = LLMGateway(secrets={"groq_api_keys": ["key1", "key2"]})
        models_attempted = []

        def mock_make_groq_client(api_key: str):
            client = MagicMock()

            async def mock_create(**kwargs):
                m = kwargs.get("model")
                models_attempted.append(m)
                if m == "model_deprecated_404":
                    raise Exception("Error 404: The model `model_deprecated_404` does not exist")
                resp = MagicMock()
                resp.choices = [MagicMock()]
                resp.choices[0].message = MagicMock()
                resp.choices[0].message.content = "Answer from fallback model"
                resp.choices[0].message.tool_calls = []
                resp.choices[0].finish_reason = "stop"
                return resp

            client.chat.completions.create = AsyncMock(side_effect=mock_create)
            return client

        gateway._make_groq_client = mock_make_groq_client

        resp = asyncio.run(gateway._call_groq(
            messages=[{"role": "user", "content": "hi"}],
            tools=None,
            models=("model_deprecated_404", "model_working_fallback"),
            tier=1,
            stream=False,
            on_delta=None,
            temperature=0.35,
            max_tokens=800,
        ))
        self.assertEqual(resp.text, "Answer from fallback model")
        self.assertEqual(resp.model, "model_working_fallback")
        # On 404, it immediately broke from model_deprecated_404 and tried model_working_fallback
        self.assertEqual(models_attempted[0], "model_deprecated_404")
        self.assertEqual(models_attempted[1], "model_working_fallback")

    def test_tier1_dynamic_reasoning_effort_escalates_on_ambiguity_or_failure(self):
        """Tier 1 starts with 'low' reasoning effort and automatically raises to 'medium' on ambiguous/failed tool."""
        tmpdir = tempfile.mkdtemp(prefix="aiva_p3_test_")
        try:
            db_path = os.path.join(tmpdir, "p3_test.db")
            db = DatabaseService(db_path)
            uid = "user_p3"
            db.create_user(uid, "test@example.com", "TestUser")
            tel = TelemetryRecorder(db)
            registry = ToolRegistry()

            # Register mock tool that returns ambiguous status
            async def handle_mock_contact(args, ctx):
                if args.get("name") == "ambiguous":
                    return {"status": "ambiguous", "message": "Multiple contacts found"}
                return {"status": "success", "message": "Found 1 contact"}

            from core.agent.tools.registry import ToolSpec
            from core.agent.policy import RISK_READ
            registry.register(ToolSpec(
                name="find_contact",
                description="Find contact",
                parameters={"type": "object", "properties": {"name": {"type": "string"}}},
                handler=handle_mock_contact,
                risk=RISK_READ,
                allowed_channels=("hud", "voice", "whatsapp"),
            ))

            efforts_observed = []

            class MockGateway:
                def get_tier1_reasoning(self):
                    return "low"

                def get_tier2_reasoning(self):
                    return "medium"

                async def complete(self, messages, **kwargs):
                    effort = kwargs.get("reasoning_effort")
                    efforts_observed.append(effort)
                    if len(efforts_observed) == 1:
                        # Step 1: Call find_contact with ambiguous argument
                        return LLMResponse(
                            tool_calls=[LLMToolCall(id="tc_1", name="find_contact", arguments={"name": "ambiguous"})],
                            provider="groq",
                            model="openai/gpt-oss-20b",
                        )
                    # Step 2: Conclude
                    return LLMResponse(text="Which one did you mean?", provider="groq", model="openai/gpt-oss-20b")

            orch = AgentOrchestrator(
                registry=registry,
                llm_gateway=MockGateway(),
                telemetry=tel,
            )

            res = asyncio.run(orch.handle_turn(
                "find contact John",
                user_id=uid,
                channel="hud",
            ))

            self.assertEqual(len(efforts_observed), 2)
            self.assertEqual(efforts_observed[0], "low")     # Step 1: simple turn starts with "low"
            self.assertEqual(efforts_observed[1], "medium")  # Step 2: raised to "medium" after ambiguous tool result
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()

