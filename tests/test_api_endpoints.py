import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import aiva_server
from services.auth_service import AuthService
from services.database_service import DatabaseService
from services.device_manager import DeviceManager
from services.whatsapp_config import clear_dotenv_cache


class TestApiEndpoints(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="aiva_api_test_")
        self.db_path = os.path.join(self.tmpdir, "api_test.db")
        self.env_path = os.path.join(self.tmpdir, ".env")
        with open(self.env_path, "w", encoding="utf-8") as f:
            f.write(
                "AIVA_ADMIN_EMAIL=owner@example.com\n"
                "AIVA_WHATSAPP_NUMBER=919000000000\n"
                "AIVA_ACCESS_EPOCH=1\n"
            )
        self._old_env_file = os.environ.get("AIVA_ENV_FILE")
        os.environ["AIVA_ENV_FILE"] = self.env_path
        clear_dotenv_cache()

        self.db = DatabaseService(db_path=self.db_path)
        self.auth = AuthService(db=self.db)
        self.device_manager = DeviceManager(self.db)
        aiva_server.invalidate_user_gate_cache()
        self.client = aiva_server.app.test_client()

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
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_full_auth_and_device_api_flow(self):
        import uuid
        email = f"api_test_{uuid.uuid4().hex[:6]}@example.com"

        with patch.object(aiva_server, "db", self.db), \
             patch.object(aiva_server, "auth_service", self.auth), \
             patch.object(aiva_server, "device_manager", self.device_manager):
            # 1. Register / Request OTP
            resp = self.client.post("/api/auth/register", json={"email": email})
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertEqual(data["status"], "success")

            # 2. Get OTP from mock/debug store
            otp = self.auth._last_generated_otp_debug.get(email)
            self.assertIsNotNone(otp)

            # 3. Verify OTP -> starts in server-side verification gate ('number_pending')
            resp = self.client.post("/api/auth/verify-otp", json={"email": email, "otp": otp})
            self.assertEqual(resp.status_code, 200)
            verify_data = resp.get_json()
            self.assertEqual(verify_data["status"], "success")
            access_token = verify_data["tokens"]["access_token"]
            refresh_token = verify_data["tokens"]["refresh_token"]
            user_id = verify_data["user"]["id"]
            headers = {"Authorization": f"Bearer {access_token}"}

            # 4. Check /api/auth/me (gate-exempt)
            resp = self.client.get("/api/auth/me", headers=headers)
            self.assertEqual(resp.status_code, 200)
            me_data = resp.get_json()
            self.assertEqual(me_data["user"]["email"], email)
            self.assertEqual(me_data["user"]["gate_state"], "number_pending")

            # 4b. Gate enforcement blocks /api/devices/register before WhatsApp verification
            gated_resp = self.client.post("/api/devices/register", headers=headers, json={
                "name": "Blocked Before Gate",
                "platform": "windows",
                "app_version": "1.0.0"
            })
            self.assertEqual(gated_resp.status_code, 403)
            self.assertEqual(gated_resp.get_json().get("code"), "verification_required")

            # Complete server-side verification gate -> active
            self.db.upsert_verified_whatsapp_link(user_id, "919876500099", verified_by_epoch=1)
            self.db.set_user_gate_state(user_id, "active", access_epoch=1)
            aiva_server.invalidate_user_gate_cache(user_id)

            # 5. Register device via API
            resp = self.client.post("/api/devices/register", headers=headers, json={
                "name": "Living Room PC",
                "platform": "windows",
                "app_version": "1.0.0"
            })
            self.assertEqual(resp.status_code, 201)
            dev_data = resp.get_json()
            device_id = dev_data["device"]["id"]
            device_token = dev_data["device_token"]
            self.assertTrue(device_token.startswith("aiva_dev_"))

            # 6. List devices via API
            resp = self.client.get("/api/devices", headers=headers)
            self.assertEqual(resp.status_code, 200)
            devices_list = resp.get_json()["devices"]
            self.assertEqual(len(devices_list), 1)
            self.assertEqual(devices_list[0]["name"], "Living Room PC")

            # 7. Rename device via API
            resp = self.client.patch(f"/api/devices/{device_id}", headers=headers, json={"name": "Office Desk PC"})
            self.assertEqual(resp.status_code, 200)

            # 8. Refresh access token via API
            resp = self.client.post("/api/auth/refresh", json={"refresh_token": refresh_token})
            self.assertEqual(resp.status_code, 200)
            new_token = resp.get_json()["tokens"]["access_token"]
            self.assertNotEqual(new_token, access_token)
            headers = {"Authorization": f"Bearer {new_token}"}

            # 9. Revoke device via API
            resp = self.client.delete(f"/api/devices/{device_id}", headers=headers)
            self.assertEqual(resp.status_code, 200)

            # Verify device list is now empty
            resp = self.client.get("/api/devices", headers=headers)
            self.assertEqual(len(resp.get_json()["devices"]), 0)

            # 10. Voice endpoints validation
            unauthed_client = aiva_server.app.test_client()
            resp = unauthed_client.post("/api/voice/transcribe")
            self.assertEqual(resp.status_code, 401)

            resp = self.client.post("/api/voice/transcribe", headers=headers)
            self.assertEqual(resp.status_code, 400)

            resp = self.client.get("/api/voice/tts")
            self.assertEqual(resp.status_code, 400)


if __name__ == "__main__":
    unittest.main()
