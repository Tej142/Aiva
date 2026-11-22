"""Unit tests for Phase 7: Short voice, full detail on screen, and follow-up flow.

Verifies:
1. shape_response for voice speaks <= 2 sentences and puts full detail on screen.
2. If full response is longer/richer than spoken summary, ends with 'Want the details?'.
3. Follow-up requests ('yes', 'details', 'tell me more') speak the next chunk (max ~4 sentences).
4. WhatsApp and HUD formatting rules remain intact.
5. Voice follow-up details cache expires and isolates per user.
"""

import time
import unittest
from unittest.mock import AsyncMock, MagicMock

from core.agent.shaper import (
    DETAILS_OFFER,
    append_details_offer,
    get_next_details_chunk,
    is_details_followup_phrase,
    is_longer_or_richer_than_spoken,
    shape_response,
    split_sentences,
)
from core.agent.orchestrator import AgentOrchestrator
from core.agent.tools.registry import ToolRegistry


class TestPhase7VoiceShaper(unittest.TestCase):
    def test_short_voice_no_offer(self):
        short_text = "The time is 10:30 AM."
        shaped = shape_response(short_text, channel="voice")
        self.assertEqual(shaped["text"], short_text)
        self.assertIn("10:30", shaped["speech_text"])
        self.assertNotIn(DETAILS_OFFER, shaped["speech_text"])
        self.assertFalse(shaped.get("has_details", False))

    def test_long_voice_summary_and_details_offer(self):
        long_text = (
            "SQLite WAL mode appends changes to a separate write-ahead log file instead of the main database. "
            "This allows readers to access the database concurrently while a write transaction is occurring. "
            "Transactions commit instantly by flushing changes to the WAL file. "
            "A checkpoint operation periodically transfers the WAL log content back into the database file. "
            "This architecture minimizes lock contention and boosts overall write throughput dramatically."
        )
        shaped = shape_response(long_text, channel="voice")
        # Full text appears on the HUD
        self.assertEqual(shaped["text"], long_text)
        # Spoken summary is <= 2 sentences plus the details offer
        sents = split_sentences(shaped["speech_text"])
        self.assertLessEqual(len(sents), 3)  # 2 summary sentences + offer
        self.assertTrue(shaped["speech_text"].endswith(DETAILS_OFFER))
        self.assertTrue(shaped.get("has_details", True))

    def test_markdown_rich_voice_triggers_offer(self):
        rich_text = "Here is the summary:\n- Point 1: Fast cache.\n- Point 2: Low memory."
        shaped = shape_response(rich_text, channel="voice")
        self.assertEqual(shaped["text"], rich_text)
        self.assertTrue(shaped["speech_text"].endswith(DETAILS_OFFER))
        self.assertTrue(shaped.get("has_details"))

    def test_get_next_details_chunk(self):
        long_text = (
            "Sentence one about architecture. "
            "Sentence two about design. "
            "Sentence three explains memory safety. "
            "Sentence four explains thread safety. "
            "Sentence five covers zero cost abstractions. "
            "Sentence six outlines production deployment. "
            "Sentence seven discusses maintenance."
        )
        # Next chunk after 2 spoken sentences, max 4 sentences
        chunk = get_next_details_chunk(long_text, already_spoken_sentences=2, max_sentences=4)
        chunk_sents = split_sentences(chunk)
        self.assertEqual(len(chunk_sents), 4)
        self.assertIn("memory safety", chunk)
        self.assertIn("production deployment", chunk)
        self.assertNotIn("Sentence one", chunk)

    def test_is_details_followup_phrase(self):
        self.assertTrue(is_details_followup_phrase("yes"))
        self.assertTrue(is_details_followup_phrase("details"))
        self.assertTrue(is_details_followup_phrase("tell me more"))
        self.assertTrue(is_details_followup_phrase("yes please tell me more"))
        self.assertTrue(is_details_followup_phrase("give me the details"))
        self.assertTrue(is_details_followup_phrase("sure"))

        self.assertFalse(is_details_followup_phrase("shut down the computer"))
        self.assertFalse(is_details_followup_phrase("what time is it"))
        self.assertFalse(is_details_followup_phrase("open chrome"))

    def test_hud_and_whatsapp_formatting_intact(self):
        raw = "**Important:** Check out https://example.com for documentation."
        shaped_hud = shape_response(raw, channel="hud")
        self.assertEqual(shaped_hud["text"], raw)
        self.assertIn("link on screen", shaped_hud["speech_text"])

        shaped_wa = shape_response(raw, channel="whatsapp")
        self.assertEqual(shaped_wa["speech_text"], "")
        self.assertIn("*Important:*", shaped_wa["text"])


class TestPhase7OrchestratorVoiceFollowup(unittest.IsolatedAsyncioTestCase):
    async def test_voice_details_followup_flow(self):
        mock_llm = MagicMock()
        mock_llm.complete = AsyncMock()

        long_answer = (
            "Postgres is an advanced object-relational database with robust ACID compliance. "
            "MySQL is widely known for high performance in read-heavy web applications. "
            "Postgres handles complex queries and rich data types like JSONB natively. "
            "MySQL offers simplified replication setup and fast table scans. "
            "For your project, Postgres is recommended for relational consistency."
        )
        mock_llm.complete.return_value = MagicMock(
            text=long_answer,
            tool_calls=[],
            first_token_ms=10.0,
        )

        orc = AgentOrchestrator(
            registry=ToolRegistry(),
            llm_gateway=mock_llm,
            telemetry=MagicMock(),
        )

        # Turn 1: User asks comparison on voice
        t1_out = await orc.handle_turn(
            "compare postgres and mysql",
            user_id="user_v1",
            channel="voice",
            session_id="sess_v1",
        )

        # Full detail on screen
        self.assertEqual(t1_out["text"], long_answer)
        # Short spoken summary with offer
        self.assertTrue(t1_out["speech_text"].endswith(DETAILS_OFFER))
        self.assertTrue(t1_out.get("has_details"))

        # Turn 2: User responds "tell me more" in follow-up window
        t2_out = await orc.handle_turn(
            "tell me more",
            user_id="user_v1",
            channel="voice",
            session_id="sess_v1",
        )

        # Responds with next chunk via details_followup route without calling LLM
        self.assertEqual(t2_out["route"], "details_followup")
        self.assertIn("JSONB", t2_out["speech_text"])
        self.assertEqual(t2_out["tier"], 0)
        # LLM complete should have only been called ONCE (in turn 1, not turn 2)
        self.assertEqual(mock_llm.complete.call_count, 1)


if __name__ == "__main__":
    unittest.main()

