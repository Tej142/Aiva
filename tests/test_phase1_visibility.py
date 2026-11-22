"""Unit tests for Phase 1: Make the active path visible in logs, status, HUD, and stream."""

import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import aiva_server
from core.agent import is_agent_core_enabled, log_agent_startup
from core.agent.llm_gateway import LLMGateway
from services.database_service import DatabaseService
from services.auth_service import AuthService


def _parse_sse(raw_text: str):
    events = []
    current_event = None
    current_data = []
    for line in raw_text.splitlines():
        if line.startswith("event: "):
            current_event = line[7:].strip()
        elif line.startswith("data: "):
            current_data.append(line[6:].strip())
        elif line == "":
            if current_event:
                payload = json.loads("\n".join(current_data)) if current_data else {}
                events.append((current_event, payload))
            current_event = None
            current_data = []
    if current_event:
        payload = json.loads("\n".join(current_data)) if current_data else {}
        events.append((current_event, payload))
    return events


class TestPhase1Visibility(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="aiva_p1_test_")
        self.db_path = os.path.join(self.tmpdir, "p1_test.db")
        self.db = DatabaseService(db_path=self.db_path)
        self.auth = AuthService(self.db)
        self.user = self.db.create_user("user_phase1", "phase1@example.com", "Phase1 User")
        self.db.set_user_gate_state("user_phase1", "active")
        aiva_server.invalidate_user_gate_cache()

    def tearDown(self):
        aiva_server.invalidate_user_gate_cache()
        try:
            conn = getattr(getattr(self.db, "_local", None), "conn", None)
            if conn:
                conn.close()
        except Exception:
            pass
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_startup_log_format_and_non_pii(self):
        with patch.dict(os.environ, {"AIVA_AGENT_CORE": "1"}):
            line_on = log_agent_startup()
            self.assertIn("[Agent] stage=startup", line_on)
            self.assertIn("duration_ms=0.0", line_on)
            self.assertIn("status=ok", line_on)
            self.assertIn("agent_core=on", line_on)
            self.assertIn("tier1_models=", line_on)
            self.assertIn("tier2_models=", line_on)
            # Ensure model names are present
            self.assertIn("openai/gpt-oss-20b", line_on)
            self.assertIn("gemini-3-flash-preview", line_on)
            # Ensure no secret keys logged
            self.assertNotIn("gsk_", line_on)
            self.assertNotIn("AIza", line_on)

        with patch.dict(os.environ, {"AIVA_AGENT_CORE": "0"}):
            line_off = log_agent_startup()
            self.assertIn("agent_core=off", line_off)

    def test_status_endpoint_returns_agent_core_and_model_lists(self):
        with patch.object(aiva_server, "db", self.db), \
             patch.object(aiva_server, "auth_service", self.auth), \
             patch.object(aiva_server, "kernel", None), \
             patch.dict(os.environ, {"AIVA_AGENT_CORE": "1"}):
            client = aiva_server.app.test_client()
            resp = client.get("/api/status")
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()

            self.assertIn("agent_core", data)
            self.assertIs(data["agent_core"], True)
            self.assertIn("tier1_models", data)
            self.assertIsInstance(data["tier1_models"], list)
            self.assertEqual(data["tier1_models"], LLMGateway.get_tier1_models())
            self.assertIn("tier2_models", data)
            self.assertIsInstance(data["tier2_models"], list)
            self.assertEqual(data["tier2_models"], LLMGateway.get_tier2_models())

        with patch.object(aiva_server, "db", self.db), \
             patch.object(aiva_server, "auth_service", self.auth), \
             patch.object(aiva_server, "kernel", None), \
             patch.dict(os.environ, {"AIVA_AGENT_CORE": "0"}):
            client = aiva_server.app.test_client()
            resp = client.get("/api/status")
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertIs(data["agent_core"], False)

    def test_chat_stream_final_event_has_path_agent_core(self):
        import types

        async def fake_handle_turn(query, user_id=None, channel="hud", session_id="default", on_event=None):
            if on_event:
                await on_event({"type": "delta", "delta": "Agent core response."})
            return {"response": "Agent core response.", "route": "general", "tier": 1, "tag": "conversation"}

        fake_kernel = types.SimpleNamespace(
            running=True,
            set_active_user=MagicMock(),
            agent_orchestrator=types.SimpleNamespace(handle_turn=fake_handle_turn),
            input_handler=None,
            voice_service=None,
            capabilities={},
            telemetry=None,
        )

        with patch.object(aiva_server, "db", self.db), \
             patch.object(aiva_server, "auth_service", self.auth), \
             patch.object(aiva_server, "kernel", fake_kernel), \
             patch.object(aiva_server, "kernel_loop", None), \
             patch.object(aiva_server.auth_service, "verify_access_token", return_value={"sub": "user_phase1"}), \
             patch.dict(os.environ, {"AIVA_AGENT_CORE": "1"}):
            client = aiva_server.app.test_client()
            resp = client.post(
                "/api/chat/stream",
                json={"message": "hello agent core", "include_audio": False},
                headers={"Authorization": "Bearer fake-token"},
            )
            self.assertEqual(resp.status_code, 200)
            events = _parse_sse(resp.get_data(as_text=True))
            self.assertEqual(events[-1][0], "final")
            final_data = events[-1][1]
            self.assertEqual(final_data.get("path"), "agent_core")

    def test_chat_stream_final_event_has_path_legacy(self):
        import types

        async def fake_process_query(msg, speak=False, user_id=None, channel="hud", session_id="default"):
            return {"response": "Legacy response.", "route": "device"}

        fake_kernel = types.SimpleNamespace(
            running=True,
            set_active_user=MagicMock(),
            agent_orchestrator=None,
            input_handler=types.SimpleNamespace(process_query=fake_process_query),
            voice_service=None,
            capabilities={},
            telemetry=None,
        )

        with patch.object(aiva_server, "db", self.db), \
             patch.object(aiva_server, "auth_service", self.auth), \
             patch.object(aiva_server, "kernel", fake_kernel), \
             patch.object(aiva_server, "kernel_loop", None), \
             patch.object(aiva_server.auth_service, "verify_access_token", return_value={"sub": "user_phase1"}), \
             patch.dict(os.environ, {"AIVA_AGENT_CORE": "0"}):
            client = aiva_server.app.test_client()
            resp = client.post(
                "/api/chat/stream",
                json={"message": "hello legacy", "include_audio": False},
                headers={"Authorization": "Bearer fake-token"},
            )
            self.assertEqual(resp.status_code, 200)
            events = _parse_sse(resp.get_data(as_text=True))
            self.assertEqual(events[-1][0], "final")
            final_data = events[-1][1]
            self.assertEqual(final_data.get("path"), "legacy")

    def test_hud_elements_present_and_tokens_only(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "ui", "index.html"), "r", encoding="utf-8") as f:
            html = f.read()
        self.assertIn('id="stat-box-agent-core"', html)
        self.assertIn('id="stat-agent-core"', html)
        self.assertIn('id="stat-agent-core-sub"', html)
        self.assertIn("Agent Core", html)

        with open(os.path.join(root, "ui", "app.js"), "r", encoding="utf-8") as f:
            js = f.read()
        self.assertIn("stat-agent-core", js)
        self.assertIn("path-pill", js)

        with open(os.path.join(root, "ui", "style.css"), "r", encoding="utf-8") as f:
            css = f.read()
        self.assertIn(".path-pill", css)
        self.assertIn("var(--accent-2)", css)


if __name__ == "__main__":
    unittest.main()
