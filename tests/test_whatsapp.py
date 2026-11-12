import os
import shutil
import tempfile
import time
import unittest
from unittest.mock import patch, MagicMock

import aiva_server
from services.database_service import DatabaseService
from services.whatsapp_config import clear_dotenv_cache
from services.whatsapp_service import (
    WhatsAppService,
    normalize_phone,
    mask_phone,
    format_for_whatsapp,
    verify_bridge_secret,
)


class TestWhatsAppChannel(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="aiva_wa_unit_")
        self._env_path = os.path.join(self._tmpdir, ".env")
        self._old_env_file = os.environ.get("AIVA_ENV_FILE")
        os.environ["AIVA_ENV_FILE"] = self._env_path
        self._write_env()

        # Create an in-memory SQLite DatabaseService instance for isolation
        self.db = DatabaseService(db_path=":memory:")
        self.whatsapp_svc = WhatsAppService(db=self.db, kernel=None)
        aiva_server.invalidate_user_gate_cache()

        # Set up two test users to verify data isolation
        self.user_a = "user_alpha_111"
        self.user_b = "user_beta_222"
        now = time.time()
        conn = self.db.get_connection()
        conn.execute(
            "INSERT INTO users (id, email, name, onboarded, created_at, is_active, gate_state, access_epoch) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (self.user_a, "alpha@example.com", "Alpha", 1, now, 1, "active", 1),
        )
        conn.execute(
            "INSERT INTO users (id, email, name, onboarded, created_at, is_active, gate_state, access_epoch) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (self.user_b, "beta@example.com", "Beta", 1, now, 1, "active", 1),
        )
        conn.commit()

    def _write_env(self, **overrides):
        defaults = {
            "AIVA_WHATSAPP_NUMBER": "919000000000",
            "AIVA_ADMIN_EMAIL": "alpha@example.com",
            "WHATSAPP_BRIDGE_SECRET": "test-bridge-secret",
            "AIVA_ACCESS_USERNAME": "tester",
            "AIVA_ACCESS_PASSWORD": "secret",
            "AIVA_ACCESS_EPOCH": "1",
            "WHATSAPP_MAX_OUTBOUND_PER_HOUR": "20",
            "WHATSAPP_ALLOW_POWER_CONTROL": "false",
        }
        defaults.update(overrides)
        with open(self._env_path, "w", encoding="utf-8") as f:
            for k, v in defaults.items():
                f.write(f'{k}="{v}"\n')
        clear_dotenv_cache()

    def tearDown(self):
        aiva_server.invalidate_user_gate_cache()
        if self._old_env_file is None:
            os.environ.pop("AIVA_ENV_FILE", None)
        else:
            os.environ["AIVA_ENV_FILE"] = self._old_env_file
        clear_dotenv_cache()
        try:
            conn = getattr(getattr(self.db, "_local", None), "conn", None)
            if conn:
                conn.close()
                self.db._local.conn = None
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    # 1. Number Normalization & Masking
    def test_normalize_phone(self):
        self.assertEqual(normalize_phone("+91 98765-43210"), "919876543210")
        self.assertEqual(normalize_phone("00919876543210"), "919876543210")
        self.assertEqual(normalize_phone("(+1) 555-0199"), "15550199")
        self.assertEqual(normalize_phone(""), "")

    def test_mask_phone(self):
        self.assertEqual(mask_phone("919876543210"), "+********3210")
        self.assertEqual(mask_phone("1234"), "****")
        self.assertEqual(mask_phone("123"), "****")

    # 2. Markdown Formatting for WhatsApp
    def test_format_for_whatsapp(self):
        # Bold and italics
        src = "Here is **bold text** and *italic text* and ~~deleted text~~."
        formatted = format_for_whatsapp(src)
        self.assertEqual(formatted, "Here is *bold text* and _italic text_ and ~deleted text~.")

        # Preserving URLs
        src_url = "Check this site: https://example.com/path?arg=1&val=2 for info!"
        formatted_url = format_for_whatsapp(src_url)
        self.assertIn("https://example.com/path?arg=1&val=2", formatted_url)

        # HTML stripping & markdown headers
        src_html = "### Header 3\n<p>Hello <b>World</b></p>"
        formatted_html = format_for_whatsapp(src_html)
        self.assertNotIn("###", formatted_html)
        self.assertNotIn("<p>", formatted_html)
        self.assertNotIn("<b>", formatted_html)
        self.assertIn("*Header 3*", formatted_html)

        # Truncation cleanly at max_chars
        long_text = "Word " * 500  # 2500 chars
        truncated = format_for_whatsapp(long_text, max_chars=100)
        self.assertLessEqual(len(truncated), 103)
        self.assertTrue(truncated.endswith("..."))

    # 3. Secret Verification (Constant-Time)
    def test_verify_bridge_secret(self):
        self._write_env(WHATSAPP_BRIDGE_SECRET="super-secret-token-xyz")
        self.assertTrue(verify_bridge_secret("super-secret-token-xyz"))
        self.assertFalse(verify_bridge_secret("wrong-token"))
        self.assertFalse(verify_bridge_secret(""))
        self.assertFalse(verify_bridge_secret(None))

    # 4. Linking Flow and OTP Expiry
    def test_linking_flow_and_expiry(self):
        # Start link
        start_res = self.whatsapp_svc.start_link(self.user_a, "+91 99999 11111")
        code = start_res["link_code"]
        self.assertRegex(code, r"^AIVA-\d{6}$")

        # Verify with wrong code -> False
        self.assertFalse(self.db.verify_whatsapp_link("919999911111", "AIVA-000000"))

        # Verify with expired code
        conn = self.db.get_connection()
        conn.execute(
            "UPDATE whatsapp_links SET link_code_expires_at = ? WHERE phone_e164 = ?",
            (time.time() - 100, "919999911111")
        )
        conn.commit()
        self.assertFalse(self.db.verify_whatsapp_link("919999911111", code))

        # Reset expiry and verify -> True
        conn.execute(
            "UPDATE whatsapp_links SET link_code_expires_at = ? WHERE phone_e164 = ?",
            (time.time() + 600, "919999911111")
        )
        conn.commit()
        self.assertTrue(self.db.verify_whatsapp_link("919999911111", code))

        # Link is now verified
        link = self.db.get_whatsapp_link_by_phone("919999911111")
        self.assertIsNotNone(link)
        self.assertEqual(link["status"], "verified")
        self.assertEqual(link["user_id"], self.user_a)

    # 5. User Isolation on Contacts
    def test_per_user_contact_isolation(self):
        # User A saves contact "dad"
        self.db.add_whatsapp_contact(
            user_id=self.user_a,
            display_name="Dad",
            phone_e164="919876543210",
            aliases=["papa", "nanna"]
        )
        # User B saves contact "dad" with different number
        self.db.add_whatsapp_contact(
            user_id=self.user_b,
            display_name="Dad",
            phone_e164="919111122222",
            aliases=["father"]
        )

        # User A queries
        res_a = self.whatsapp_svc.resolve_contact(self.user_a, "dad")
        self.assertEqual(res_a["status"], "found")
        self.assertEqual(res_a["contact"]["phone_e164"], "919876543210")

        # User B queries
        res_b = self.whatsapp_svc.resolve_contact(self.user_b, "dad")
        self.assertEqual(res_b["status"], "found")
        self.assertEqual(res_b["contact"]["phone_e164"], "919111122222")

        # User B cannot access User A's contact by alias "papa"
        res_b_papa = self.whatsapp_svc.resolve_contact(self.user_b, "papa")
        self.assertEqual(res_b_papa["status"], "not_found")

    # 6. Contact Resolution & Ambiguity
    def test_contact_disambiguation(self):
        self.db.add_whatsapp_contact(
            user_id=self.user_a,
            display_name="John Doe Work",
            phone_e164="919000000001",
            aliases=["john"]
        )
        self.db.add_whatsapp_contact(
            user_id=self.user_a,
            display_name="John Smith Personal",
            phone_e164="919000000002",
            aliases=["john"]
        )

        # Ambiguous match
        res = self.whatsapp_svc.resolve_contact(self.user_a, "john")
        self.assertEqual(res["status"], "ambiguous")
        self.assertEqual(len(res["matches"]), 2)

    # 7. Unknown Sender Rejection & Deduplication
    @patch("services.whatsapp_service.WhatsAppService.send_whatsapp_message")
    def test_unknown_sender_rejection_and_dedupe(self, mock_send):
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            # 1. Unknown sender sends a message -> silently dropped (never reply to strangers)
            payload = {
                "message_id": "msg_001",
                "sender": "919888877777@s.whatsapp.net",
                "text": "Hello AIVA"
            }
            res = loop.run_until_complete(self.whatsapp_svc.handle_inbound(payload))
            self.assertEqual(res["status"], "dropped_unknown_sender")
            self.assertEqual(mock_send.call_count, 0)

            # 2. Duplicate message id is ignored immediately without sending anything
            res_dup = loop.run_until_complete(self.whatsapp_svc.handle_inbound(payload))
            self.assertEqual(res_dup["status"], "ignored_duplicate")
            self.assertEqual(mock_send.call_count, 0)
        finally:
            loop.close()

    # 8. Outbound Rate Limiting
    def test_outbound_rate_limit(self):
        self._write_env(WHATSAPP_MAX_OUTBOUND_PER_HOUR="3")
        self.assertTrue(self.whatsapp_svc.check_outbound_rate_limit(self.user_a))
        self.whatsapp_svc.record_outbound_send(self.user_a)
        self.whatsapp_svc.record_outbound_send(self.user_a)
        self.assertTrue(self.whatsapp_svc.check_outbound_rate_limit(self.user_a))
        self.whatsapp_svc.record_outbound_send(self.user_a)
        # Now at limit (3 sends in 1 hour)
        self.assertFalse(self.whatsapp_svc.check_outbound_rate_limit(self.user_a))
        # Other user is unaffected
        self.assertTrue(self.whatsapp_svc.check_outbound_rate_limit(self.user_b))

    # 9. Power Control Block
    @patch("services.whatsapp_service.WhatsAppService.send_whatsapp_message")
    def test_power_control_blocked(self, mock_send):
        import asyncio
        # Link & activate user_a at current access_epoch=1 with ACTIVE session
        self.db.upsert_verified_whatsapp_link(self.user_a, "919999911111", verified_by_epoch=1, session_state="ACTIVE")
        self.db.set_user_gate_state(self.user_a, "active", access_epoch=1)
        self._write_env(WHATSAPP_ALLOW_POWER_CONTROL="false")

        loop = asyncio.new_event_loop()
        try:
            payload = {
                "message_id": "msg_power_1",
                "sender": "919999911111@s.whatsapp.net",
                "text": "shutdown computer"
            }
            res = loop.run_until_complete(self.whatsapp_svc.handle_inbound(payload))
            self.assertEqual(res["status"], "power_blocked")
        finally:
            loop.close()

    # 10. Bridge Status Masking & Pairing Privacy Isolation
    def test_bridge_status_masks_phone_and_isolates_pairing_secret(self):
        import asyncio

        bridge_payload = {
            "connected": True,
            "status": "connected",
            "bridge_state": "connected",
            "phone": "919493867075",
            "error": None,
            "pairing": {
                "active": True,
                "phase": "qr_ready",
                "mode": "qr",
                "qr": "2@RAW_SECRET_QR",
                "qr_data_url": "data:image/svg+xml;base64,test",
                "pairing_code": "ABCD-EFGH",
            },
        }

        async def _fake_req(method, path, payload=None, timeout=5.0):
            return 200, bridge_payload

        self._write_env(AIVA_ADMIN_EMAIL="alpha@example.com")
        loop = asyncio.new_event_loop()
        try:
            with patch.object(self.whatsapp_svc, "_bridge_request", side_effect=_fake_req):
                owner_status = loop.run_until_complete(self.whatsapp_svc.get_user_status(self.user_a))
                other_status = loop.run_until_complete(self.whatsapp_svc.get_user_status(self.user_b))

            self.assertTrue(owner_status["is_owner"])
            self.assertEqual(owner_status["bridge"]["phone_masked"], "+********7075")
            self.assertNotIn("phone", owner_status["bridge"])
            self.assertEqual(owner_status["bridge"]["pairing"]["qr_data_url"], "data:image/svg+xml;base64,test")

            self.assertFalse(other_status["is_owner"])
            self.assertEqual(other_status["bridge"]["phone_masked"], "+********7075")
            self.assertNotIn("qr", other_status["bridge"]["pairing"])
            self.assertNotIn("qr_data_url", other_status["bridge"]["pairing"])
            self.assertNotIn("pairing_code", other_status["bridge"]["pairing"])
            self.assertFalse(other_status["bridge"]["pairing"]["active"])
        finally:
            loop.close()

    # 11. Pairing Mode Lifecycle & Owner Authorization
    def test_bridge_pairing_owner_guard_and_modes(self):
        import asyncio

        calls = []

        async def _fake_req(method, path, payload=None, timeout=5.0):
            calls.append((method, path, payload))
            if path == "/pair/start":
                return 200, {
                    "status": "success",
                    "pairing": {
                        "active": True,
                        "phase": "code_ready" if (payload or {}).get("mode") == "code" else "qr_ready",
                        "mode": (payload or {}).get("mode"),
                        "pairing_code": "WXYZ-1234" if (payload or {}).get("mode") == "code" else None,
                    },
                }
            return 200, {"status": "success", "pairing": {"active": False, "phase": "cancelled"}}

        owner_user = {"id": self.user_a, "email": "alpha@example.com"}
        other_user = {"id": self.user_b, "email": "beta@example.com"}
        self._write_env(AIVA_ADMIN_EMAIL="alpha@example.com")

        loop = asyncio.new_event_loop()
        try:
            with patch.object(self.whatsapp_svc, "_bridge_request", side_effect=_fake_req):
                # Non-owner blocked from all pairing mutations
                c1, _ = loop.run_until_complete(self.whatsapp_svc.start_bridge_pairing(other_user, mode="qr"))
                c2, _ = loop.run_until_complete(self.whatsapp_svc.cancel_bridge_pairing(other_user))
                c3, _ = loop.run_until_complete(self.whatsapp_svc.logout_bridge(other_user))
                self.assertEqual((c1, c2, c3), (403, 403, 403))
                self.assertEqual(len(calls), 0)

                # Invalid mode & invalid phone rejected
                c_bad_mode, _ = loop.run_until_complete(self.whatsapp_svc.start_bridge_pairing(owner_user, mode="sms"))
                c_bad_phone, _ = loop.run_until_complete(
                    self.whatsapp_svc.start_bridge_pairing(owner_user, mode="code", raw_phone="123")
                )
                self.assertEqual((c_bad_mode, c_bad_phone), (400, 400))

                # QR mode succeeds
                c_qr, r_qr = loop.run_until_complete(self.whatsapp_svc.start_bridge_pairing(owner_user, mode="qr"))
                self.assertEqual(c_qr, 200)
                self.assertEqual(r_qr["data"]["pairing"]["phase"], "qr_ready")

                # Code mode normalizes phone & succeeds
                c_code, r_code = loop.run_until_complete(
                    self.whatsapp_svc.start_bridge_pairing(owner_user, mode="code", raw_phone="+91 94938-67075")
                )
                self.assertEqual(c_code, 200)
                self.assertEqual(r_code["data"]["pairing"]["pairing_code"], "WXYZ-1234")
                self.assertEqual(calls[-1][2], {"mode": "code", "phone": "919493867075"})

                # Cancel & logout succeed
                c_can, _ = loop.run_until_complete(self.whatsapp_svc.cancel_bridge_pairing(owner_user))
                c_log, _ = loop.run_until_complete(self.whatsapp_svc.logout_bridge(owner_user))
                self.assertEqual((c_can, c_log), (200, 200))
        finally:
            loop.close()

    # 12. Flask API Endpoints for WhatsApp Status & Pairing Mode
    def test_flask_whatsapp_status_and_pairing_routes(self):
        import asyncio

        async def _fake_req(method, path, payload=None, timeout=5.0):
            if method == "GET" and path == "/status":
                return 200, {
                    "connected": False,
                    "status": "re_pair_required",
                    "bridge_state": "re_pair_required",
                    "phone": "",
                    "error": "Logged out (401)",
                    "pairing": {"active": False, "phase": "idle"},
                }
            if method == "POST" and path == "/pair/start":
                return 200, {"status": "success", "pairing": {"active": True, "phase": "qr_ready", "mode": "qr"}}
            if method == "POST" and path == "/pair/cancel":
                return 200, {"status": "success", "pairing": {"active": False, "phase": "cancelled"}}
            return 200, {"status": "success", "bridge_state": "re_pair_required"}

        mock_kernel = MagicMock()
        mock_kernel.whatsapp_service = self.whatsapp_svc
        self._write_env(AIVA_ADMIN_EMAIL="alpha@example.com")

        client = aiva_server.app.test_client()
        with patch.object(aiva_server, "db", self.db), \
             patch.object(aiva_server, "kernel", mock_kernel), \
             patch.object(aiva_server, "run_async", side_effect=lambda coro, timeout=25.0: asyncio.run(coro)), \
             patch.object(aiva_server.auth_service, "verify_access_token", return_value={"sub": self.user_a}), \
             patch.object(self.whatsapp_svc, "_bridge_request", side_effect=_fake_req):
            # Unauthenticated fails
            self.assertEqual(client.get("/api/whatsapp/status").status_code, 401)
            self.assertEqual(client.post("/api/whatsapp/pair/start", json={"mode": "qr"}).status_code, 401)

            headers = {"Authorization": "Bearer fake.jwt.token"}
            res_status = client.get("/api/whatsapp/status", headers=headers)
            self.assertEqual(res_status.status_code, 200)
            status_data = res_status.get_json()["data"]
            self.assertEqual(status_data["bridge"]["status"], "re_pair_required")
            self.assertTrue(status_data["is_owner"])

            res_start = client.post("/api/whatsapp/pair/start", json={"mode": "qr"}, headers=headers)
            self.assertEqual(res_start.status_code, 200)
            self.assertTrue(res_start.get_json()["data"]["pairing"]["active"])

            res_cancel = client.post("/api/whatsapp/pair/cancel", json={}, headers=headers)
            self.assertEqual(res_cancel.status_code, 200)

            res_logout = client.post("/api/whatsapp/pair/logout", json={}, headers=headers)
            self.assertEqual(res_logout.status_code, 200)

            # Non-owner user gets 403 on pairing mutations
            with patch.object(aiva_server.auth_service, "verify_access_token", return_value={"sub": self.user_b}):
                res_forbidden = client.post("/api/whatsapp/pair/start", json={"mode": "qr"}, headers=headers)
                self.assertEqual(res_forbidden.status_code, 403)


if __name__ == "__main__":
    unittest.main()

