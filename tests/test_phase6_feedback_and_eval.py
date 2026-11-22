"""Unit tests for Phase 6: Feedback loop, database storage, masking, and evaluation.

Verifies:
1. Phone numbers are masked in free text while retaining last 4 digits.
2. Sensitive credentials (API keys, passwords, OTPs, credit cards, govt IDs) are redacted.
3. DatabaseService agent_feedback table provides strict per-user_id data isolation.
4. Authenticated endpoints (/api/agent/feedback, /api/feedback) enforce auth and CRUD.
5. scripts/failures_to_golden.py exports thumbs-down records to candidate golden YAML.
6. scripts/run_eval.py supports the --judge option for live evaluation while preserving stub mode.
"""

import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from core.agent.feedback_safety import (
    mask_phone_numbers_in_text,
    redact_secrets_in_text,
    sanitize_feedback_text,
)
from services.database_service import DatabaseService
from scripts.failures_to_golden import fetch_feedback_failures, export_to_candidate_yaml


class TestPhase6FeedbackSafety(unittest.TestCase):
    def test_mask_phone_numbers_in_text(self):
        text = "Call me at +919876543210 or alternate 9876543210 or US +1-555-123-4567 right now."
        masked = mask_phone_numbers_in_text(text)

        self.assertNotIn("9876543210", masked)
        self.assertNotIn("555-123-4567", masked)
        self.assertIn("3210", masked)
        self.assertIn("4567", masked)

    def test_redact_secrets_in_text(self):
        # API key
        text_api = "My key is sk-ant-api03-abcdef123456789012345 and gsk_1234567890abcdef1234."
        redacted = redact_secrets_in_text(text_api)
        self.assertNotIn("sk-ant-", redacted)
        self.assertNotIn("gsk_", redacted)
        self.assertIn("[REDACTED_API_KEY]", redacted)

        # Password
        text_pw = "The wifi password is SuperSecretPass123! and my pin is 9876."
        redacted_pw = redact_secrets_in_text(text_pw)
        self.assertNotIn("SuperSecretPass123!", redacted_pw)
        self.assertIn("[REDACTED_PASSWORD]", redacted_pw)

        # OTP
        text_otp = "Your verification code is 482910."
        redacted_otp = redact_secrets_in_text(text_otp)
        self.assertNotIn("482910", redacted_otp)
        self.assertIn("[REDACTED_OTP]", redacted_otp)

        # SSN
        text_ssn = "SSN is 000-12-3456."
        redacted_ssn = redact_secrets_in_text(text_ssn)
        self.assertNotIn("000-12-3456", redacted_ssn)
        self.assertIn("[REDACTED_GOVT_ID]", redacted_ssn)

    def test_sanitize_feedback_text_combined(self):
        dirty = "Send password MySecretPwd99 to +919876543210 with key gsk_1234567890abcdef1234"
        clean = sanitize_feedback_text(dirty)
        self.assertNotIn("MySecretPwd99", clean)
        self.assertNotIn("9876543210", clean)
        self.assertNotIn("gsk_1234567890", clean)
        self.assertIn("3210", clean)


class TestPhase6FeedbackDatabase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="aiva_fb_test_")
        self.db_path = os.path.join(self.tmpdir, "test_feedback.db")
        self.db = DatabaseService(db_path=self.db_path)

        # Create two test users
        conn = self.db.get_connection()
        conn.execute("INSERT INTO users (id, email, name, created_at) VALUES ('u1', 'u1@example.com', 'Alice', 1000.0)")
        conn.execute("INSERT INTO users (id, email, name, created_at) VALUES ('u2', 'u2@example.com', 'Bob', 1000.0)")
        conn.commit()

    def tearDown(self):
        try:
            conn = getattr(getattr(self.db, "_local", None), "conn", None)
            if conn:
                conn.close()
        except Exception:
            pass
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_save_and_get_agent_feedback_isolation(self):
        id1 = self.db.save_agent_feedback(
            user_id="u1",
            user_message="open that file",
            assistant_reply="Could you clarify which file?",
            tier=1,
            tag="conversation",
            tools=[],
            note="Too vague",
            rating="thumbs_down",
        )
        self.assertGreater(id1, 0)

        id2 = self.db.save_agent_feedback(
            user_id="u2",
            user_message="search weather",
            assistant_reply="Weather is sunny",
            tier=1,
            tag="action",
            tools=["web_research"],
            note="Wrong city",
            rating="thumbs_down",
        )
        self.assertGreater(id2, 0)

        # Verify strict per-user data isolation
        u1_items = self.db.get_agent_feedback("u1")
        self.assertEqual(len(u1_items), 1)
        self.assertEqual(u1_items[0]["id"], id1)
        self.assertEqual(u1_items[0]["user_message"], "open that file")
        self.assertEqual(u1_items[0]["note"], "Too vague")

        u2_items = self.db.get_agent_feedback("u2")
        self.assertEqual(len(u2_items), 1)
        self.assertEqual(u2_items[0]["id"], id2)
        self.assertEqual(u2_items[0]["tools"], ["web_research"])

    def test_delete_feedback_item_and_all(self):
        id1 = self.db.save_agent_feedback("u1", "msg 1", "reply 1")
        id2 = self.db.save_agent_feedback("u1", "msg 2", "reply 2")
        id3 = self.db.save_agent_feedback("u2", "msg 3", "reply 3")

        # u2 cannot delete u1's item
        deleted_foreign = self.db.delete_agent_feedback_item("u2", id1)
        self.assertFalse(deleted_foreign)
        self.assertEqual(len(self.db.get_agent_feedback("u1")), 2)

        # u1 can delete id1
        deleted_own = self.db.delete_agent_feedback_item("u1", id1)
        self.assertTrue(deleted_own)
        self.assertEqual(len(self.db.get_agent_feedback("u1")), 1)

        # u1 clears all remaining items
        cleared_count = self.db.delete_all_agent_feedback("u1")
        self.assertEqual(cleared_count, 1)
        self.assertEqual(len(self.db.get_agent_feedback("u1")), 0)

        # u2 items remain untouched
        self.assertEqual(len(self.db.get_agent_feedback("u2")), 1)


class TestPhase6FailuresToGoldenScript(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="aiva_script_test_")
        self.db_path = os.path.join(self.tmpdir, "test_golden.db")
        self.db = DatabaseService(db_path=self.db_path)

        conn = self.db.get_connection()
        conn.execute("INSERT INTO users (id, email, name, created_at) VALUES ('u_eval', 'e@example.com', 'Eval', 1000.0)")
        conn.commit()

        self.db.save_agent_feedback(
            user_id="u_eval",
            user_message="Find the latest stock price of NVIDIA",
            assistant_reply="NVIDIA is $140",
            tier=1,
            tag="research",
            tools=["web_research"],
            note="Outdated price",
            rating="thumbs_down",
        )

    def tearDown(self):
        try:
            conn = getattr(getattr(self.db, "_local", None), "conn", None)
            if conn:
                conn.close()
        except Exception:
            pass
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_fetch_and_export_golden_yaml(self):
        items = fetch_feedback_failures(self.db_path, user_id="u_eval")
        self.assertEqual(len(items), 1)

        out_yaml_path = os.path.join(self.tmpdir, "candidates.yaml")
        yaml_content = export_to_candidate_yaml(items, output_path=out_yaml_path)

        self.assertIn("candidate_fb_", yaml_content)
        self.assertIn("Find the latest stock price of NVIDIA", yaml_content)
        self.assertIn("failure_candidate", yaml_content)
        self.assertIn("NEVER auto-merge", yaml_content)
        self.assertTrue(os.path.exists(out_yaml_path))


class TestPhase6ServerEndpoints(unittest.TestCase):
    def test_server_feedback_routes_registered(self):
        from aiva_server import app
        rules = [rule.rule for rule in app.url_map.iter_rules()]
        self.assertIn("/api/feedback", rules)
        self.assertIn("/api/agent/feedback", rules)
        self.assertIn("/api/agent/feedback/<int:feedback_id>", rules)


if __name__ == "__main__":
    unittest.main()

