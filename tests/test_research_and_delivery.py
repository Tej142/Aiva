"""Tests for Step 2: ResearchAgent async/freshness fixes, working-memory summary+ref, workflow delivery & pattern filtering."""

import asyncio
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.agents.planner_agent import PlannerAgent
from core.agents.research_agent import ResearchAgent
from core.memory.engine import MemoryEngine
from core.memory.models import MemoryItem, MemorySource, MemoryStatus, MemoryType, new_id
from core.planning.workflow_engine import WorkflowEngine
from core.planning.workflow_graph import WorkflowGraph
from core.planning.workflow_node import WorkflowNode
from services.database_service import DatabaseService
from services.outcome_reporter_service import OutcomeReporterService
from services.task_pattern_service import TaskPatternService


class TestResearchAndDelivery(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="aiva_step2_test_")
        self.db_path = os.path.join(self.tmpdir, "test_aiva.db")
        self.db = DatabaseService(self.db_path)
        self.user_id = "user_step2_test"
        self.db.create_user(self.user_id, "step2@example.com", "Charan")
        self.engine = MemoryEngine(db=self.db, start_worker=False, enable_async_embeddings=False)

    def tearDown(self):
        try:
            self.engine.shutdown()
        except Exception:
            pass
        try:
            conn = getattr(getattr(self.db, "_local", None), "conn", None)
            if conn is not None:
                conn.close()
                self.db._local.conn = None
        except Exception:
            pass
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_research_awaits_tavily_and_rejects_stale_cache(self):
        # Insert an old 5-day stale knowledge item
        stale_ts = time.time() - 5 * 86400
        stale_item = MemoryItem(
            id=new_id(),
            user_id=self.user_id,
            type=MemoryType.KNOWLEDGE.value,
            content="ai news: Old news from days ago [source: https://old.example]",
            summary="Old news from days ago",
            entities=["ai news"],
            importance=0.6,
            confidence=0.8,
            source=MemorySource.WEB.value,
            channel="hud",
            created_at=stale_ts,
            last_accessed=stale_ts,
            expires_at=stale_ts + 30 * 86400,
            status=MemoryStatus.ACTIVE.value,
        )
        self.engine.store.insert_memory(stale_item, is_verified_user=True)

        fake_tavily = MagicMock()
        fake_tavily.search = AsyncMock(
            return_value={
                "results": [
                    {
                        "title": "Latest AI Breakthroughs Today",
                        "url": "https://fresh.example.org/ai",
                        "content": "Fresh breakthrough published today on agentic tool calling.",
                    }
                ]
            }
        )
        fake_mistral = MagicMock()
        fake_mistral.summarize_research = AsyncMock(
            return_value="Fresh synthesis: Agentic tool calling improved latency today."
        )
        fake_mistral.extract_knowledge = AsyncMock(
            return_value={"facts": ["Latency dropped 40%"], "summary": ""}
        )

        agent = ResearchAgent(
            tavily_service=fake_tavily,
            mistral_service=fake_mistral,
            memory_engine=self.engine,
        )
        res = asyncio.run(agent.perform_research("latest ai news", user_id=self.user_id, max_age_hours=24.0))

        fake_tavily.search.assert_awaited_once()
        self.assertIsNotNone(res)
        self.assertTrue(res["fresh"])
        self.assertFalse(res["cached"])
        self.assertIn("Fresh synthesis", res["summary"])
        self.assertEqual(len(res["sources"]), 1)
        self.assertEqual(res["sources"][0]["url"], "https://fresh.example.org/ai")
        self.assertTrue(res["retrieved_at"])

    def test_research_fresh_cache_is_marked_cached_not_fresh(self):
        fake_tavily = MagicMock()
        fake_tavily.search = AsyncMock(
            return_value={
                "results": [
                    {"title": "Quantum Computing Update", "url": "https://q.example", "content": "Quantum error correction milestone."}
                ]
            }
        )
        agent = ResearchAgent(tavily_service=fake_tavily, memory_engine=self.engine)
        first = asyncio.run(agent.perform_research("quantum computing update", user_id=self.user_id))
        self.assertTrue(first["fresh"])

        # Second call within TTL uses cache but explicitly tags cached=True, fresh=False
        second = asyncio.run(agent.perform_research("quantum computing update", user_id=self.user_id, max_age_hours=24.0))
        fake_tavily.search.assert_awaited_once()  # Still only 1 live call
        self.assertTrue(second["cached"])
        self.assertFalse(second["fresh"])
        self.assertEqual(second["source"], "memory_knowledge_cache")

    def test_working_memory_compacts_large_research_reports_with_ref_id(self):
        huge_report = "## Deep Research\n" + ("Detailed factual finding about distributed AI pipelines. " * 25)
        self.engine.observe(
            self.user_id,
            {
                "user": "research distributed AI",
                "assistant": huge_report,
                "channel": "hud",
                "ref_id": "know_abc123",
                "is_tool_output": True,
            },
        )
        turns = self.engine.store.get_working_turns(self.user_id, limit=10)
        assistant_turns = [t for t in turns if t.content.startswith("assistant:")]
        self.assertEqual(len(assistant_turns), 1)
        stored_text = assistant_turns[0].content
        self.assertIn("[ref:know_abc123]", stored_text)
        self.assertLess(len(stored_text), 300)

    def test_workflow_outcome_pushes_to_hud_and_originating_whatsapp_channel(self):
        hud_calls = []
        wa_calls = []

        reporter = OutcomeReporterService()
        reporter._generate_outcome = AsyncMock(return_value="Researched quantum updates and sent summary.")
        reporter.memory_engine = self.engine
        reporter.hud_deliver = lambda uid, payload: hud_calls.append((uid, payload)) or True
        reporter.whatsapp_deliver = lambda uid, text: wa_calls.append((uid, text)) or True

        class _DummyQueue:
            has_consumer = False
            async def add_task(self, t):
                return None

        class _DummyBus:
            async def emit(self, *a, **kw):
                return None

        wf_engine = WorkflowEngine(_DummyBus(), _DummyQueue(), outcome_reporter=reporter)
        wf = WorkflowGraph("wf_1", goal="research quantum", user_id=self.user_id, channel="whatsapp")
        wf.add_node(WorkflowNode("step1", "research", {"topic": "quantum"}))

        async def _run():
            await wf_engine.execute_workflow_graph(wf)
            await asyncio.sleep(0.05)

        asyncio.run(_run())
        self.assertEqual(len(hud_calls), 1)
        self.assertEqual(hud_calls[0][1]["type"], "workflow_result")
        self.assertEqual(len(wa_calls), 1)
        self.assertIn("Researched quantum", wa_calls[0][1])

    def test_task_pattern_filtering_blocks_noisy_and_phone_patterns(self):
        svc = TaskPatternService(memory_file=os.path.join(self.tmpdir, "none.json"), db=self.db)
        svc.active_user_id = self.user_id

        noisy = {
            "id": "p_noisy_greet",
            "template_goal": "hi open chrome",
            "slots": [],
            "workflow_template": [{"node_id": "s1", "action": "launch_app", "parameters": {"app_name": "chrome"}}],
        }
        hardcoded_wa = {
            "id": "p_phone",
            "template_goal": "send hello to 8639473796 in whatsapp",
            "slots": [],
            "workflow_template": [{"node_id": "s1", "action": "whatsapp_send", "parameters": {"contact": "8639473796", "message": "hello"}}],
        }
        clean_macro = {
            "id": "p_clean",
            "template_goal": "open chrome and search for {query}",
            "slots": ["query"],
            "workflow_template": [
                {"node_id": "s1", "action": "launch_app", "parameters": {"app_name": "chrome"}},
                {"node_id": "s2", "action": "open_url", "parameters": {"url": "https://www.google.com/search?q={query_encoded}"}},
            ],
            "examples": ["open chrome and search for aiva"],
            "frequency": 4,
        }

        self.db.set_user_task_pattern(self.user_id, "p_noisy_greet", noisy)
        self.db.set_user_task_pattern(self.user_id, "p_phone", hardcoded_wa)
        self.db.set_user_task_pattern(self.user_id, "p_clean", clean_macro)

        self.assertFalse(TaskPatternService.is_clean_pattern("p_noisy_greet", noisy))
        self.assertFalse(TaskPatternService.is_clean_pattern("p_phone", hardcoded_wa))
        self.assertTrue(TaskPatternService.is_clean_pattern("p_clean", clean_macro))

        self.assertIsNone(svc.match_pattern("hi open chrome"))
        self.assertIsNone(svc.match_pattern("send hello to 8639473796 in whatsapp"))
        hints = svc.get_agent_hints("open chrome and search for python", user_id=self.user_id)
        hint_ids = [h["pattern_id"] for h in hints]
        self.assertIn("p_clean", hint_ids)
        self.assertNotIn("p_noisy_greet", hint_ids)
        self.assertNotIn("p_phone", hint_ids)


if __name__ == "__main__":
    unittest.main()
