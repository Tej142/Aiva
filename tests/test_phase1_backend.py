import os
import sys
import time
import json
import uuid
import asyncio
import tempfile
import unittest

# Ensure project root is in sys.path
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from services.database_service import DatabaseService
from services.auth_service import AuthService
from services.device_manager import DeviceManager
from services.device_channel import DeviceChannel
from aiva_server import app


class TestPhase1Backend(unittest.TestCase):
    def setUp(self):
        # Create a temporary isolated database for each test
        self.temp_db_fd, self.temp_db_path = tempfile.mkstemp(suffix=".db")
        os.close(self.temp_db_fd)
        self.db = DatabaseService(self.temp_db_path)
        self.auth_service = AuthService(self.db)
        self.device_manager = DeviceManager(self.db)
        self.device_channel = DeviceChannel(self.db, self.device_manager)
        self.client = app.test_client()

    def tearDown(self):
        try:
            if hasattr(self.db, "_local") and hasattr(self.db._local, "conn") and self.db._local.conn:
                self.db._local.conn.close()
                self.db._local.conn = None
            if os.path.exists(self.temp_db_path):
                os.remove(self.temp_db_path)
        except Exception:
            pass

    # -------------------------------------------------------------
    # 1. OTP LIFECYCLE & VERIFICATION
    # -------------------------------------------------------------
    def test_valid_otp_registration_and_login(self):
        email = "charan@example.com"
        ok, msg = self.auth_service.request_otp(email)
        self.assertTrue(ok)
        self.assertIn("sent", msg.lower())

        # Retrieve debug OTP generated
        otp_code = self.auth_service._last_generated_otp_debug.get(email)
        self.assertIsNotNone(otp_code)
        self.assertEqual(len(otp_code), 6)

        # Verify OTP
        verify_ok, verify_msg, data = self.auth_service.verify_otp(email, otp_code)
        self.assertTrue(verify_ok)
        self.assertIsNotNone(data)
        self.assertEqual(data["user"]["email"], email)
        self.assertIn("access_token", data["tokens"])
        self.assertIn("refresh_token", data["tokens"])

        # Check access token verification
        payload = self.auth_service.verify_access_token(data["tokens"]["access_token"])
        self.assertIsNotNone(payload)
        self.assertEqual(payload["email"], email)

    def test_wrong_otp_and_max_attempts(self):
        email = "wrong_test@example.com"
        self.auth_service.request_otp(email)

        # 1-4 incorrect attempts
        for attempt in range(4):
            ok, msg, data = self.auth_service.verify_otp(email, "000000")
            self.assertFalse(ok)
            self.assertIn("Incorrect verification code", msg)

        # 5th attempt - should exceed maximum attempts
        ok, msg, data = self.auth_service.verify_otp(email, "000000")
        self.assertFalse(ok)
        self.assertIn("exceeded", msg.lower())

        # Now even the correct code should be rejected
        correct_otp = self.auth_service._last_generated_otp_debug.get(email)
        ok, msg, data = self.auth_service.verify_otp(email, correct_otp)
        self.assertFalse(ok)
        self.assertIn("exceeded", msg.lower())

    def test_expired_otp(self):
        email = "expired_test@example.com"
        self.auth_service.request_otp(email)
        otp_code = self.auth_service._last_generated_otp_debug.get(email)

        # Force expiry in the database
        conn = self.db.get_connection()
        conn.execute("UPDATE otps SET expires_at = ? WHERE email = ?", (time.time() - 100, email))
        conn.commit()

        ok, msg, data = self.auth_service.verify_otp(email, otp_code)
        self.assertFalse(ok)
        self.assertIn("expired", msg.lower())

    def test_otp_rate_limiting(self):
        email = "ratelimit@example.com"
        # 3 requests succeed
        for i in range(3):
            ok, msg = self.auth_service.request_otp(email)
            self.assertTrue(ok)

        # 4th request must be rate limited
        ok, msg = self.auth_service.request_otp(email)
        self.assertFalse(ok)
        self.assertIn("too many", msg.lower())

    # -------------------------------------------------------------
    # 2. REFRESH TOKEN LIFECYCLE
    # -------------------------------------------------------------
    def test_refresh_token_rotation_and_revocation(self):
        email = "refresh@example.com"
        self.auth_service.request_otp(email)
        otp = self.auth_service._last_generated_otp_debug.get(email)
        _, _, data = self.auth_service.verify_otp(email, otp)
        refresh_token = data["tokens"]["refresh_token"]

        # Refresh
        ok, msg, new_tokens = self.auth_service.refresh_access_token(refresh_token)
        self.assertTrue(ok)
        self.assertIn("access_token", new_tokens)

        # Old refresh token should now be revoked (rotated)
        ok_old, _, _ = self.auth_service.refresh_access_token(refresh_token)
        self.assertFalse(ok_old)

        # Explicit revocation
        new_refresh = new_tokens["refresh_token"]
        self.auth_service.revoke_refresh_token(new_refresh)
        ok_revoked, _, _ = self.auth_service.refresh_access_token(new_refresh)
        self.assertFalse(ok_revoked)

    # -------------------------------------------------------------
    # 3. DEVICE REGISTRATION & REVOCATION
    # -------------------------------------------------------------
    def test_device_registry(self):
        user = self.db.create_user(str(uuid.uuid4()), "dev_user@example.com")
        user_id = user["id"]

        # Register device
        ok, msg, res = self.device_manager.register_device(
            user_id=user_id,
            name="Office ThinkPad",
            platform="windows",
            app_version="1.2.0"
        )
        self.assertTrue(ok)
        device = res["device"]
        raw_token = res["device_token"]
        self.assertTrue(raw_token.startswith("aiva_dev_"))

        # Verify plaintext token is never stored in DB
        device_row = self.db.get_device_by_id(device["id"])
        self.assertNotEqual(device_row["device_token_hash"], raw_token)
        self.assertEqual(len(device_row["device_token_hash"]), 64)  # SHA-256

        # Verify token verification works
        verified_dev = self.device_manager.verify_device_token(raw_token)
        self.assertIsNotNone(verified_dev)
        self.assertEqual(verified_dev["id"], device["id"])

        # Rename device
        ok_rename, _ = self.device_manager.rename_device(device["id"], user_id, "Home Desktop")
        self.assertTrue(ok_rename)
        renamed = self.db.get_device_by_id(device["id"])
        self.assertEqual(renamed["name"], "Home Desktop")

        # List devices
        devices = self.device_manager.list_devices(user_id)
        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0]["name"], "Home Desktop")

        # Revoke device
        ok_revoke, _ = self.device_manager.revoke_device(device["id"], user_id)
        self.assertTrue(ok_revoke)
        self.assertIsNone(self.db.get_device_by_id(device["id"]))

    # -------------------------------------------------------------
    # 4. COMMAND ROUTING & CROSS-USER ISOLATION
    # -------------------------------------------------------------
    def test_offline_device_handling(self):
        user = self.db.create_user(str(uuid.uuid4()), "offline_user@example.com")
        # No device registered or online
        loop = asyncio.new_event_loop()
        res = loop.run_until_complete(
            self.device_channel.dispatch_command(
                user_id=user["id"],
                action="open_app",
                params={"app_name": "notepad"}
            )
        )
        loop.close()

        self.assertEqual(res["status"], "offline")
        self.assertEqual(res["error"], "device_offline")
        self.assertIn("No Windows device is currently online", res["message"])

    def test_cross_user_isolation(self):
        user_a = self.db.create_user(str(uuid.uuid4()), "user_a@example.com")
        user_b = self.db.create_user(str(uuid.uuid4()), "user_b@example.com")

        # User B registers a device
        _, _, res_b = self.device_manager.register_device(user_b["id"], "User B Laptop")
        dev_b = res_b["device"]

        # User A attempts to list or command User B's device
        user_a_devices = self.device_manager.list_devices(user_a["id"])
        self.assertEqual(len(user_a_devices), 0)

        # User A cannot rename User B's device
        renamed, _ = self.device_manager.rename_device(dev_b["id"], user_a["id"], "Hacked Name")
        self.assertFalse(renamed)

        # User A cannot revoke User B's device
        revoked, _ = self.device_manager.revoke_device(dev_b["id"], user_a["id"])
        self.assertFalse(revoked)


if __name__ == "__main__":
    unittest.main()

