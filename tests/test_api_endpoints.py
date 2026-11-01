import os
import sys
import json
import unittest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from aiva_server import app, auth_service, db


class TestApiEndpoints(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    def test_full_auth_and_device_api_flow(self):
        import uuid
        email = f"api_test_{uuid.uuid4().hex[:6]}@example.com"

        # 1. Register / Request OTP
        resp = self.client.post("/api/auth/register", json={"email": email})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["status"], "success")

        # 2. Get OTP from mock/debug store
        otp = auth_service._last_generated_otp_debug.get(email)
        self.assertIsNotNone(otp)

        # 3. Verify OTP
        resp = self.client.post("/api/auth/verify-otp", json={"email": email, "otp": otp})
        self.assertEqual(resp.status_code, 200)
        verify_data = resp.get_json()
        self.assertEqual(verify_data["status"], "success")
        access_token = verify_data["tokens"]["access_token"]
        refresh_token = verify_data["tokens"]["refresh_token"]
        headers = {"Authorization": f"Bearer {access_token}"}

        # 4. Check /api/auth/me
        resp = self.client.get("/api/auth/me", headers=headers)
        self.assertEqual(resp.status_code, 200)
        me_data = resp.get_json()
        self.assertEqual(me_data["user"]["email"], email)

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


if __name__ == "__main__":
    unittest.main()
