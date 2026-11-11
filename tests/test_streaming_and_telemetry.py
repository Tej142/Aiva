import asyncio
import json
import os
import re
import shutil
import tempfile
import time
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import aiva_server
from services.auth_service import AuthService
from services.database_service import DatabaseService


def _parse_sse(raw_text: str):
    events = []
    for block in re.split(r"\r?\n\r?\n", raw_text.strip()):
        if not block.strip():
            continue
        ev_name = "message"
        data_lines = []
        for line in block.splitlines():
            if line.startswith("event:"):
                ev_name = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].strip())
        if data_lines:
            events.append((ev_name, json.loads("\n".join(data_lines))))
    return events


class TestStreamingAndTelemetry(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="aiva_stream_test_")
        self.db_path = os.path.join(self._tmp, "test_stream.db")
        self.db = DatabaseService(db_path=self.db_path)
        self.auth = AuthService(self.db)
        now = time.time()
        conn = self.db.get_connection()
        self.user_a = {"id": "usr_alice_1", "email": "alice@example.com", "name": "Alice"}
        self.user_b = {"id": "usr_bob_2", "email": "bob@example.com", "name": "Bob"}
        for u in (self.user_a, self.user_b):
            conn.execute(
                "INSERT INTO users (id, email, name, onboarded, created_at, is_active) VALUES (?, ?, ?, 1, ?, 1)",
                (u["id"], u["email"], u["name"], now),
            )
        conn.commit()

    def tearDown(self):
        try:
            conn = getattr(getattr(self.db, "_local", None), "conn", None)
            if conn:
                conn.close()
        except Exception:
            pass
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_chat_stream_requires_auth_and_message(self):
        with patch.object(aiva_server, "db", self.db), \
             patch.object(aiva_server, "auth_service", self.auth):
            client = aiva_server.app.test_client()
            resp_no_msg = client.post("/api/chat/stream", json={})
            self.assertEqual(resp_no_msg.status_code, 400)

            resp_no_auth = client.post("/api/chat/stream", json={"message": "hi"})
            self.assertEqual(resp_no_auth.status_code, 401)

    def test_chat_stream_sse_agent_core_with_chunked_tts(self):
        async def fake_handle_turn(user_input, *, user_id, channel="hud", session_id="default", device_name=None, on_event=None):
            if on_event:
                await on_event({"type": "status", "status": "searching"})
                await on_event({"type": "status", "status": "reading sources"})
                await on_event({"type": "delta", "delta": "First sentence is ready. "})
                await on_event({"type": "delta", "delta": "Second sentence follows closely."})
            return {
                "response": "First sentence is ready. Second sentence follows closely.",
                "speech_text": "First sentence is ready. Second sentence follows closely.",
                "sources": [{"title": "Docs", "url": "https://example.com/docs", "retrieved_at": "2026-11-11"}],
                "tier": 2,
                "tag": "research",
                "tools": ["web_research"],
                "timings": {"total_ms": 120.0},
                "route": "research",
            }

        fake_voice = types.SimpleNamespace(generate_tts_audio=AsyncMock(return_value=b"MP3BYTES"))
        fake_orch = types.SimpleNamespace(handle_turn=fake_handle_turn)
        fake_kernel = types.SimpleNamespace(
            running=True,
            set_active_user=MagicMock(),
            agent_orchestrator=fake_orch,
            input_handler=types.SimpleNamespace(),
            voice_service=fake_voice,
            capabilities={},
            telemetry=None,
        )

        with patch.object(aiva_server, "db", self.db), \
             patch.object(aiva_server, "auth_service", self.auth), \
             patch.object(aiva_server, "kernel", fake_kernel), \
             patch.object(aiva_server, "kernel_loop", None), \
             patch.object(aiva_server.auth_service, "verify_access_token", return_value={"sub": self.user_a["id"]}), \
             patch.dict(os.environ, {"AIVA_AGENT_CORE": "1"}):
            client = aiva_server.app.test_client()
            resp = client.post(
                "/api/chat/stream",
                json={"message": "search test", "include_audio": True},
                headers={"Authorization": "Bearer fake-token"},
            )
            self.assertEqual(resp.status_code, 200)
            self.assertIn("text/event-stream", resp.content_type)
            events = _parse_sse(resp.get_data(as_text=True))

        names = [e[0] for e in events]
        self.assertIn("status", names)
        self.assertIn("delta", names)
        self.assertIn("audio_chunk", names)
        self.assertEqual(names[-1], "final")

        audio_events = [p for n, p in events if n == "audio_chunk"]
        self.assertEqual(len(audio_events), 2)
        self.assertEqual(audio_events[0]["index"], 0)
        self.assertEqual(audio_events[1]["index"], 1)

        final_payload = events[-1][1]
        self.assertEqual(final_payload["tier"], 2)
        self.assertEqual(final_payload["tag"], "research")
        self.assertEqual(final_payload["sources"][0]["url"], "https://example.com/docs")
        self.assertGreater(final_payload["timings"].get("tts_first_audio_ms", 0), 0)

    def test_chat_stream_legacy_fallback_records_telemetry(self):
        async def fake_process_query(msg, speak=False, user_id=None, channel="hud", session_id="default"):
            return {"response": "Volume increased to 60 percent.", "route": "device"}

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
             patch.object(aiva_server.auth_service, "verify_access_token", return_value={"sub": self.user_a["id"]}), \
             patch.dict(os.environ, {"AIVA_AGENT_CORE": "0"}):
            client = aiva_server.app.test_client()
            resp = client.post(
                "/api/chat/stream",
                json={"message": "volume up", "include_audio": False},
                headers={"Authorization": "Bearer fake-token"},
            )
            self.assertEqual(resp.status_code, 200)
            events = _parse_sse(resp.get_data(as_text=True))

        self.assertEqual(events[-1][0], "final")
        self.assertEqual(events[-1][1]["tier"], 0)
        rows = self.db.get_agent_telemetry_rows(user_id=self.user_a["id"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["tier"], 0)

    def test_telemetry_endpoint_and_status_per_user_isolation(self):
        self.db.record_agent_telemetry("t1", self.user_a["id"], tier=0, channel="hud", tag="action", total_ms=12.0, success=True)
        self.db.record_agent_telemetry("t2", self.user_a["id"], tier=2, channel="hud", tag="research", total_ms=320.0, success=True)
        self.db.record_agent_telemetry("t3", self.user_b["id"], tier=1, channel="whatsapp", tag="task", total_ms=900.0, success=False)

        with patch.object(aiva_server, "db", self.db), \
             patch.object(aiva_server, "auth_service", self.auth), \
             patch.object(aiva_server, "kernel", None):
            client = aiva_server.app.test_client()

            # Unauthenticated telemetry must be 401; /api/status must not expose user telemetry
            self.assertEqual(client.get("/api/telemetry").status_code, 401)
            unauthed_status = client.get("/api/status").get_json()
            self.assertNotIn("telemetry", unauthed_status)
            self.assertEqual(unauthed_status["pools"], {"groq": 7, "gemini": 5, "mistral": 5})

            # Authenticated as User A sees ONLY User A metrics
            with patch.object(aiva_server.auth_service, "verify_access_token", return_value={"sub": self.user_a["id"]}):
                tel_a = client.get("/api/telemetry?hours=24", headers={"Authorization": "Bearer tok-a"}).get_json()["data"]
                self.assertEqual(tel_a["total_requests"], 2)
                self.assertEqual(tel_a["tiers"], {"tier_0": 1, "tier_1": 0, "tier_2": 1})
                self.assertEqual(tel_a["success_rate"], 100.0)

                stat_a = client.get("/api/status", headers={"Authorization": "Bearer tok-a"}).get_json()
                self.assertEqual(stat_a["telemetry"]["total_requests"], 2)

            # Authenticated as User B sees ONLY User B metrics
            with patch.object(aiva_server.auth_service, "verify_access_token", return_value={"sub": self.user_b["id"]}):
                tel_b = client.get("/api/telemetry", headers={"Authorization": "Bearer tok-b"}).get_json()["data"]
                self.assertEqual(tel_b["total_requests"], 1)
                self.assertEqual(tel_b["success_rate"], 0.0)

    def test_frontend_vendored_markdown_sources_and_tokens_only_css(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        html_path = os.path.join(root, "ui", "index.html")
        css_path = os.path.join(root, "ui", "style.css")
        js_path = os.path.join(root, "ui", "app.js")
        vm_path = os.path.join(root, "ui", "voice-manager.js")

        self.assertTrue(os.path.exists(os.path.join(root, "ui", "vendor", "purify.min.js")))
        self.assertTrue(os.path.exists(os.path.join(root, "ui", "vendor", "marked.min.js")))

        html = open(html_path, "r", encoding="utf-8").read()
        self.assertIn('src="vendor/purify.min.js"', html)
        self.assertIn('src="vendor/marked.min.js"', html)
        self.assertNotRegex(html, r'<script[^>]+src=["\']https?://')
        self.assertIn('id="stat-tier-mix"', html)
        self.assertIn('id="stat-latency-p5095"', html)
        self.assertIn('id="stat-success-rate"', html)
        self.assertNotIn('id="stat-patterns-count"', html)
        self.assertIn("Groq Pool", html)
        self.assertIn("Gemini", html)
        self.assertIn("Mistral", html)

        css = open(css_path, "r", encoding="utf-8").read()
        added_section = css[css.index(".chat-ol {"):css.index("/* Typing indicator */")]
        self.assertNotRegex(added_section, r"#[0-9a-fA-F]{3,8}\b")
        self.assertNotIn("rgb(", added_section)
        self.assertIn(".chat-sources", added_section)
        self.assertIn(".chat-table", added_section)

        js = open(js_path, "r", encoding="utf-8").read()
        self.assertIn("DOMPurify.sanitize", js)
        self.assertIn("marked.parse", js)
        self.assertIn("target', '_blank'", js.replace('"', "'"))
        self.assertIn("noopener noreferrer", js)
        self.assertIn("/api/chat/stream", js)
        self.assertIn("enqueueAudioChunk", js)

        vm = open(vm_path, "r", encoding="utf-8").read()
        self.assertIn("enqueueAudioChunk", vm)
        self.assertIn("onBargeInAbort", vm)
