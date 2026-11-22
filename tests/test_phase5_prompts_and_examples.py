"""Tests for Phase 5: Better prompts, few-shot examples, and tool descriptions.

Verifies:
1. Versionable markdown prompt files (tier1.md, tier2.md, examples.md) load cleanly and format properly.
2. Identity pinning (<identity authoritative="true">), memory fencing, and untrusted tool fencing are preserved.
3. Every builtin tool description includes 'WHEN to use', 'WHEN NOT to use', and an example call, with schemas intact.
4. Prompt token capping limits conversation size per tier while preserving system prompt and latest turn.
5. Environment variables AIVA_TIER1_MAX_PROMPT_TOKENS and AIVA_TIER2_MAX_PROMPT_TOKENS configure caps.
"""

import os
import unittest
from unittest.mock import MagicMock, patch

from core.agent.prompts import (
    get_tier1_prompt_template,
    get_tier2_prompt_template,
    get_examples_content,
    clear_prompts_cache,
)
from core.agent.tools.builtin import register_builtin_tools
from core.agent.tools.registry import ToolRegistry
from core.agent.orchestrator import AgentOrchestrator


class TestPhase5PromptsAndExamples(unittest.TestCase):
    def setUp(self):
        clear_prompts_cache()

    def tearDown(self):
        clear_prompts_cache()

    def test_versionable_prompt_files_exist_and_load(self):
        t1 = get_tier1_prompt_template()
        t2 = get_tier2_prompt_template()
        ex = get_examples_content()

        self.assertTrue(len(t1) > 100, "Tier 1 prompt template should not be empty")
        self.assertTrue(len(t2) > 100, "Tier 2 prompt template should not be empty")
        self.assertTrue(len(ex) > 100, "Few-shot examples should not be empty")

        # Verify placeholders exist in templates
        for placeholder in ("{identity_block}", "{channel}", "{mem_ctx}", "{hints_block}", "{examples_block}"):
            self.assertIn(placeholder, t1, f"Tier 1 prompt missing placeholder: {placeholder}")
            self.assertIn(placeholder, t2, f"Tier 2 prompt missing placeholder: {placeholder}")

        # Verify general reasoning instructions
        for req in (
            "Think carefully about what the user actually wants",
            "Prefer doing over asking",
            "Ask ONE short",
            "Never invent or hallucinate",
            "Say plainly and honestly",
        ):
            self.assertIn(req, t1)

    def test_few_shot_examples_cover_required_scenarios(self):
        ex = get_examples_content()
        # 1. Direct message dad to come early
        self.assertIn("message dad to come early", ex)
        # 2. Ask dad to bring snacks (natural composed message, not literal copy)
        self.assertIn("ask dad to bring snacks this evening", ex)
        self.assertIn("Hi Dad, could you please bring some snacks", ex)
        # 3. Search and send to second number
        self.assertIn("send it to my second number", ex)
        # 4. Ambiguous "open that"
        self.assertIn("open that", ex)
        # 5. Reading WhatsApp messages
        self.assertIn("read unread messages", ex)
        # 6. Multi-step research request
        self.assertIn("vector databases", ex)
        # 7. Vague request needing clarification
        self.assertIn("send it to him", ex)
        # 8. Prompt injection inside web content ignored
        self.assertIn("SYSTEM OVERRIDE", ex)
        self.assertIn("treating retrieved third-party text strictly as data", ex)

    def test_system_prompt_identity_pinning_and_fencing(self):
        mem_mock = MagicMock()
        mem_mock.get_user_display_name.return_value = "Charan"
        mem_mock.build_context.return_value = "<memory>User prefers concise code.</memory>"

        orc = AgentOrchestrator(
            llm_gateway=MagicMock(),
            registry=ToolRegistry(),
            telemetry=MagicMock(),
            memory_engine=mem_mock,
        )

        sys_prompt = orc._build_system_prompt(
            user_id="user_12345",
            channel="hud",
            user_input="hello aiva",
            tier=1,
        )

        self.assertIn('<identity user_id="user_123" display_name="Charan" authoritative="true">', sys_prompt)
        self.assertIn("<memory>User prefers concise code.</memory>", sys_prompt)
        self.assertIn("Channel: hud", sys_prompt)
        self.assertIn("Few-Shot Examples:", sys_prompt)

    def test_all_18_builtin_tool_descriptions_structured(self):
        registry = ToolRegistry()
        register_builtin_tools(
            registry,
            capability_registry=MagicMock(),
            research_agent=MagicMock(),
            whatsapp_service=MagicMock(),
            memory_engine=MagicMock(),
        )

        tools = registry.list_specs()
        self.assertEqual(len(tools), 18, f"Expected 18 builtin tools, found {len(tools)}")

        for tool in tools:
            desc = tool.description
            self.assertIn("WHEN to use:", desc, f"Tool '{tool.name}' missing 'WHEN to use:'")
            self.assertIn("WHEN NOT to use:", desc, f"Tool '{tool.name}' missing 'WHEN NOT to use:'")
            self.assertIn("Example:", desc, f"Tool '{tool.name}' missing 'Example:'")
            self.assertIsInstance(tool.parameters, dict, f"Tool '{tool.name}' parameters must be valid dict")

    def test_prompt_token_capping_logic(self):
        orc = AgentOrchestrator(
            llm_gateway=MagicMock(),
            registry=ToolRegistry(),
            telemetry=MagicMock(),
        )

        sys_msg = {"role": "system", "content": "System prompt with identity " * 10}
        user_msg = {"role": "user", "content": "What is the latest score?"}

        # Under budget: messages should be untouched
        msgs = [sys_msg, user_msg]
        capped = orc.cap_prompt_messages(msgs, max_tokens=2000)
        self.assertEqual(len(capped), 2)

        # Over budget: intermediate messages should be trimmed, keeping first and last
        intermediate = [
            {"role": "assistant", "content": f"Old step {i} response with verbose logging " * 20}
            for i in range(10)
        ]
        long_msgs = [sys_msg] + intermediate + [user_msg]
        self.assertEqual(len(long_msgs), 12)

        # Cap to a small token count (e.g., 200 tokens)
        capped = orc.cap_prompt_messages(long_msgs, max_tokens=150)
        # Must retain sys_msg and user_msg
        self.assertEqual(capped[0], sys_msg)
        self.assertEqual(capped[-1], user_msg)
        self.assertLess(len(capped), len(long_msgs))

    def test_env_configurable_prompt_caps(self):
        orc = AgentOrchestrator(
            llm_gateway=MagicMock(),
            registry=ToolRegistry(),
            telemetry=MagicMock(),
        )

        with patch.dict(os.environ, {"AIVA_TIER1_MAX_PROMPT_TOKENS": "4096", "AIVA_TIER2_MAX_PROMPT_TOKENS": "8192"}):
            self.assertEqual(orc.get_tier1_max_prompt_tokens(), 4096)
            self.assertEqual(orc.get_tier2_max_prompt_tokens(), 8192)

        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(orc.get_tier1_max_prompt_tokens(), 3072)
            self.assertEqual(orc.get_tier2_max_prompt_tokens(), 6144)


if __name__ == "__main__":
    unittest.main()
