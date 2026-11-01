import os
import unittest
import tempfile
import json
import time

from aiva_server import app
from services.database_service import DatabaseService
from services.auth_service import AuthService
from services.device_manager import DeviceManager
from services.user_memory_service import UserMemoryService
from services.conversation_memory_service import ConversationMemoryService


class TestDataIsolation(unittest.TestCase):
    def setUp(self):
        # Create an isolated temporary SQLite database for each test
        self.temp_db_fd, self.temp_db_path = tempfile.mkstemp(suffix=".db")
        os.close(self.temp_db_fd)

        self.db = DatabaseService(db_path=self.temp_db_path)
        self.auth_service = AuthService(db=self.db)
        self.device_manager = DeviceManager(db=self.db)
        self.user_memory = UserMemoryService(db=self.db)
        self.conv_memory = ConversationMemoryService(db=self.db)

        # Flask test client
        app.config["TESTING"] = True
        import aiva_server
        aiva_server.db = self.db
        aiva_server.auth_service = self.auth_service
        aiva_server.device_manager = self.device_manager
        self.client = app.test_client()

        # Create two distinct test users
        self.user_a = self.db.create_user("user_aaa_111", "alice@example.com", name="Alice", onboarded=1)
        self.tokens_a = self.auth_service.generate_tokens("user_aaa_111", "alice@example.com")

        self.user_b = self.db.create_user("user_bbb_222", "bob@example.com", name="Bob", onboarded=1)
        self.tokens_b = self.auth_service.generate_tokens("user_bbb_222", "bob@example.com")

    def tearDown(self):
        try:
            if hasattr(self.db, "_local") and hasattr(self.db._local, "conn") and self.db._local.conn:
                self.db._local.conn.close()
                self.db._local.conn = None
            if os.path.exists(self.temp_db_path):
                os.remove(self.temp_db_path)
        except Exception:
            pass

    def test_memory_isolation_between_users(self):
        """User A and User B store separate facts; neither can see the other's memory."""
        # 1. User A stores fact
        self.user_memory.remember("user_aaa_111", "favorite_food", "pizza")
        self.user_memory.remember("user_aaa_111", "city", "San Francisco")

        # 2. User B stores fact
        self.user_memory.remember("user_bbb_222", "favorite_food", "sushi")
        self.user_memory.remember("user_bbb_222", "pet", "dog")

        # 3. Read via /api/memory as User A
        resp_a = self.client.get(
            "/api/memory",
            headers={"Authorization": f"Bearer {self.tokens_a['access_token']}"}
        )
        self.assertEqual(resp_a.status_code, 200)
        data_a = resp_a.get_json()
        self.assertEqual(data_a["user_memory"].get("favorite_food"), "pizza")
        self.assertEqual(data_a["user_memory"].get("city"), "San Francisco")
        self.assertNotIn("pet", data_a["user_memory"])

        # 4. Read via /api/memory as User B
        resp_b = self.client.get(
            "/api/memory",
            headers={"Authorization": f"Bearer {self.tokens_b['access_token']}"}
        )
        self.assertEqual(resp_b.status_code, 200)
        data_b = resp_b.get_json()
        self.assertEqual(data_b["user_memory"].get("favorite_food"), "sushi")
        self.assertEqual(data_b["user_memory"].get("pet"), "dog")
        self.assertNotIn("city", data_b["user_memory"])

    def test_unauthenticated_memory_access_blocked(self):
        """Access to /api/memory without token is rejected with 401."""
        resp = self.client.get("/api/memory")
        self.assertEqual(resp.status_code, 401)

    def test_conversation_history_isolation(self):
        """User A conversation turns cannot be accessed or deleted by User B."""
        # Add turns for User A
        self.conv_memory.add_turn("Hello AIVA, I am Alice", "Hello Alice!", user_id="user_aaa_111")
        # Add turns for User B
        self.conv_memory.add_turn("What is quantum computing?", "It is...", user_id="user_bbb_222")

        # User A reads history
        resp_a = self.client.get(
            "/api/history",
            headers={"Authorization": f"Bearer {self.tokens_a['access_token']}"}
        )
        self.assertEqual(resp_a.status_code, 200)
        hist_a = resp_a.get_json()["history"]
        self.assertTrue(any("Alice" in h["message"] for h in hist_a))
        self.assertFalse(any("quantum" in h["message"] for h in hist_a))

        # User B reads history
        resp_b = self.client.get(
            "/api/history",
            headers={"Authorization": f"Bearer {self.tokens_b['access_token']}"}
        )
        self.assertEqual(resp_b.status_code, 200)
        hist_b = resp_b.get_json()["history"]
        self.assertTrue(any("quantum" in h["message"] for h in hist_b))
        self.assertFalse(any("Alice" in h["message"] for h in hist_b))

        # User B tries to delete User A's history -> delete only clears User B's history
        del_b = self.client.delete(
            "/api/history",
            headers={"Authorization": f"Bearer {self.tokens_b['access_token']}"}
        )
        self.assertEqual(del_b.status_code, 200)

        # User A's history must still be intact
        resp_a_after = self.client.get(
            "/api/history",
            headers={"Authorization": f"Bearer {self.tokens_a['access_token']}"}
        )
        hist_a_after = resp_a_after.get_json()["history"]
        self.assertTrue(any("Alice" in h["message"] for h in hist_a_after))

    def test_device_cross_user_protection(self):
        """User B cannot rename or revoke User A's registered device."""
        # User A registers a device
        ok, msg, res = self.device_manager.register_device(
            user_id="user_aaa_111",
            name="Alice MacBook",
            platform="darwin"
        )
        device_a_id = res["device"]["id"]

        # User B attempts to rename User A's device
        resp_rename = self.client.patch(
            f"/api/devices/{device_a_id}",
            headers={"Authorization": f"Bearer {self.tokens_b['access_token']}"},
            json={"name": "Hacked Name"}
        )
        self.assertEqual(resp_rename.status_code, 404)

        # User B attempts to delete User A's device
        resp_delete = self.client.delete(
            f"/api/devices/{device_a_id}",
            headers={"Authorization": f"Bearer {self.tokens_b['access_token']}"}
        )
        self.assertEqual(resp_delete.status_code, 404)

        # Device A still exists and is untouched
        dev = self.db.get_device_by_id(device_a_id)
        self.assertIsNotNone(dev)
        self.assertEqual(dev["name"], "Alice MacBook")

    def test_new_user_starts_with_clean_slate(self):
        """A newly registered user has 0 devices, empty memory, and empty history."""
        new_uid = "user_new_999"
        new_user = self.db.create_user(new_uid, "newbie@example.com", name="Newbie", onboarded=0)
        new_tokens = self.auth_service.generate_tokens(new_uid, "newbie@example.com")

        # 1. /api/auth/me confirms onboarded=0
        me_resp = self.client.get(
            "/api/auth/me",
            headers={"Authorization": f"Bearer {new_tokens['access_token']}"}
        )
        self.assertEqual(me_resp.status_code, 200)
        user_info = me_resp.get_json()["user"]
        self.assertEqual(user_info["onboarded"], 0)
        self.assertEqual(user_info["name"], "Newbie")
        self.assertEqual(len(me_resp.get_json()["devices"]), 0)

        # 2. /api/memory is completely empty
        mem_resp = self.client.get(
            "/api/memory",
            headers={"Authorization": f"Bearer {new_tokens['access_token']}"}
        )
        self.assertEqual(mem_resp.status_code, 200)
        self.assertEqual(mem_resp.get_json()["user_memory"], {})
        self.assertEqual(mem_resp.get_json()["task_patterns"], {})

        # 3. /api/history is completely empty
        hist_resp = self.client.get(
            "/api/history",
            headers={"Authorization": f"Bearer {new_tokens['access_token']}"}
        )
        self.assertEqual(hist_resp.status_code, 200)
        self.assertEqual(hist_resp.get_json()["history"], [])


if __name__ == "__main__":
    unittest.main()
