"""Tests for Step 3: Non-PII TelemetryRecorder, percentiles, channel shaper & sentence segmentation."""

import json
import logging
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.agent import is_agent_core_enabled
from core.agent.shaper import (
    normalize_sources,
    pop_complete_sentences,
    shape_for_voice,
    shape_response,
    split_sentences,
)
from core.agent.telemetry import TelemetryRecorder, log_agent, percentile
from services.database_service import DatabaseService


class TestTelemetryAndShaper(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="aiva_step3_test_")
        self.db_path = os.path.join(self.tmpdir, "test_aiva.db")
        self.db = DatabaseService(self.db_path)
        self.user_a = "user_a_tel"
        self.user_b = "user_b_tel"
        self.db.create_user(self.user_a, "a@example.com", "Charan")
        self.db.create_user(self.user_b, "b@example.com", "Other")
        self.rec = TelemetryRecorder(self.db)

    def tearDown(self):
        try:
            conn = getattr(getattr(self.db, "_local", None), "conn", None)
            if conn is not None:
                conn.close()
                self.db._local.conn = None
        except Exception:
            pass
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_feature_flag_defaults_off_and_honors_env(self):
        prev = os.environ.pop("AIVA_AGENT_CORE", None)
        try:
            self.assertFalse(is_agent_core_enabled())
            os.environ["AIVA_AGENT_CORE"] = "1"
            self.assertTrue(is_agent_core_enabled())
            os.environ["AIVA_AGENT_CORE"] = "0"
            self.assertFalse(is_agent_core_enabled())
        finally:
            if prev is not None:
                os.environ["AIVA_AGENT_CORE"] = prev
            else:
                os.environ.pop("AIVA_AGENT_CORE", None)

    def test_non_pii_log_and_scoped_telemetry_aggregates(self):
        line = log_agent(
            stage="route",
            duration_ms=4.2,
            status="ok",
            tier=0,
            user_id="user_a_secret_id",
            message="SECRET TRANSCRIPT MUST NOT LOG",
            phone="+919999999999",
        )
        self.assertIn("[Agent] stage=route", line)
        self.assertIn("user=user_a_s", line)
        self.assertNotIn("SECRET TRANSCRIPT", line)
        self.assertNotIn("9999999999", line)

        # Record samples for user_a and user_b
        self.rec.record(self.user_a, tier=0, channel="hud", tag="action", route_ms=3.0, total_ms=25.0, tools=["launch_app"], success=True)
        self.rec.record(self.user_a, tier=1, channel="hud", tag="conversation", route_ms=5.0, llm_first_token_ms=210.0, total_ms=420.0, success=True)
        self.rec.record(self.user_a, tier=2, channel="whatsapp", tag="research", route_ms=8.0, llm_first_token_ms=520.0, tool_time_ms=900.0, total_ms=1600.0, tools=["web_research"], success=False)
        self.rec.record(self.user_b, tier=1, channel="hud", tag="conversation", route_ms=4.0, total_ms=9999.0, success=True)

        sum_a = self.rec.get_summary(user_id=self.user_a, window_hours=24)
        self.assertEqual(sum_a["total_requests"], 3)
        self.assertEqual(sum_a["tiers"], {"tier_0": 1, "tier_1": 1, "tier_2": 1})
        self.assertAlmostEqual(sum_a["success_rate"], 66.7, places=1)
        self.assertEqual(sum_a["latency_ms"]["p50"], 420.0)
        self.assertLess(sum_a["latency_ms"]["p95"], 2000.0)  # Isolated from user_b's 9999ms!

        sum_b = self.rec.get_summary(user_id=self.user_b, window_hours=24)
        self.assertEqual(sum_b["total_requests"], 1)
        self.assertEqual(sum_b["latency_ms"]["p50"], 9999.0)

        self.assertEqual(percentile([10, 20, 30, 40], 50), 25.0)

    def test_shaper_and_sentence_splitter(self):
        prose = "AIVA checked Dr. Rao's schedule. You have two meetings today! Check https://example.com/cal for details."
        sents = split_sentences(prose)
        self.assertEqual(len(sents), 3)
        self.assertTrue(sents[0].startswith("AIVA checked Dr. Rao"))

        ready, rem = pop_complete_sentences("First sentence done. Second part is still")
        self.assertEqual(ready, ["First sentence done."])
        self.assertEqual(rem, "Second part is still")

        voice_out = shape_for_voice("### Heading\nFirst sentence here. Second sentence here. Third extra sentence here with **bold** and https://x.test.", max_sentences=2)
        self.assertLessEqual(len(split_sentences(voice_out)), 2)
        self.assertNotIn("###", voice_out)
        self.assertNotIn("https://", voice_out)

        wa_out = shape_response(
            "**Quantum News**\nBreakthrough achieved today.",
            channel="whatsapp",
            sources=[{"title": "Nature", "url": "https://nature.com/q", "retrieved_at": "2026-11-11T10:00:00Z"}],
        )
        self.assertIn("Quantum News", wa_out["text"])
        self.assertNotIn("**", wa_out["text"])
        self.assertIn("https://nature.com/q", wa_out["text"])

        srcs = normalize_sources([
            {"title": "A", "url": "https://a.test", "retrieved_at": "t1"},
            {"title": "A dup", "url": "https://a.test", "retrieved_at": "t2"},
        ])
        self.assertEqual(len(srcs), 1)


if __name__ == "__main__":
    unittest.main()
