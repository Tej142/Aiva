import os
import unittest
import tempfile
import json
import time

from aiva_server import app
from services.database_service import DatabaseService
from services.auth_service import AuthService
from services.device_manager import DeviceManager
from core.memory import MemoryEngine
from core.memory.models import MemoryItem, MemoryType, MemorySource, new_id


class TestDataIsolation(unittest.TestCase):
    def setUp(self):
        # Create an isolated temporary SQLite database for each test
        self.temp_db_fd, self.temp_db_path = tempfile.mkstemp(suffix=".db")
        os.close(self.temp_db_fd)

        self.db = DatabaseService(db_path=self.temp_db_path)
        self.auth_service = AuthService(db=self.db)
        self.device_manager = DeviceManager(db=self.db)
        self.memory_engine = MemoryEngine(db=self.db, start_worker=False)

        # Flask test client
        app.config["TESTING"] = True
        import aiva_server
        aiva_server.kernel = None
        aiva_server.db = self.db
        aiva_server.auth_service = self.auth_service
        aiva_server.device_manager = self.device_manager
        aiva_server._fallback_memory_engine = self.memory_engine
        self.client = app.test_client()

        # Create two distinct test users
        self.user_a = self.db.create_user("user_aaa_111", "alice@example.com", name="Alice", onboarded=1)
        self.tokens_a = self.auth_service.generate_tokens("user_aaa_111", "alice@example.com")

        self.user_b = self.db.create_user("user_bbb_222", "bob@example.com", name="Bob", onboarded=1)
        self.tokens_b = self.auth_service.generate_tokens("user_bbb_222", "bob@example.com")

    def tearDown(self):
        try:
            self.memory_engine.shutdown()
            if hasattr(self.db, "_local") and hasattr(self.db._local, "conn") and self.db._local.conn:
                self.db._local.conn.close()
                self.db._local.conn = None
            if os.path.exists(self.temp_db_path):
                os.remove(self.temp_db_path)
        except Exception:
            pass

    def test_memory_isolation_between_users(self):
        """User A and User B store separate memories; neither can read, edit, or forget the other's."""
        mem_a_id = new_id()
        self.memory_engine.store.insert_memory(
            MemoryItem(
                id=mem_a_id,
                user_id="user_aaa_111",
                type=MemoryType.SEMANTIC.value,
                content="Alice's favorite food is pizza and she lives in San Francisco.",
                importance=0.9,
                source=MemorySource.USER_SAID.value,
            )
        )
        mem_b_id = new_id()
        self.memory_engine.store.insert_memory(
            MemoryItem(
                id=mem_b_id,
                user_id="user_bbb_222",
                type=MemoryType.SEMANTIC.value,
                content="Bob's favorite food is sushi and he owns a dog.",
                importance=0.9,
                source=MemorySource.USER_SAID.value,
            )
        )

        # Read via /api/memory as User A
        resp_a = self.client.get(
            "/api/memory",
            headers={"Authorization": f"Bearer {self.tokens_a['access_token']}"}
        )
        self.assertEqual(resp_a.status_code, 200)
        contents_a = [m["content"] for m in resp_a.get_json()["memories"]]
        self.assertTrue(any("pizza" in c for c in contents_a))
        self.assertFalse(any("sushi" in c or "dog" in c for c in contents_a))

        # Read via /api/memory as User B
        resp_b = self.client.get(
            "/api/memory",
            headers={"Authorization": f"Bearer {self.tokens_b['access_token']}"}
        )
        self.assertEqual(resp_b.status_code, 200)
        contents_b = [m["content"] for m in resp_b.get_json()["memories"]]
        self.assertTrue(any("sushi" in c for c in contents_b))
        self.assertFalse(any("pizza" in c or "San Francisco" in c for c in contents_b))

        # Cross-user PATCH and DELETE rejected (404)
        patch_cross = self.client.patch(
            f"/api/memory/{mem_a_id}",
            headers={"Authorization": f"Bearer {self.tokens_b['access_token']}"},
            json={"content": "Tampered"}
        )
        self.assertEqual(patch_cross.status_code, 404)

        del_cross = self.client.delete(
            f"/api/memory/{mem_a_id}",
            headers={"Authorization": f"Bearer {self.tokens_b['access_token']}"}
        )
        self.assertEqual(del_cross.status_code, 404)

        # Reminder isolation check
        rem_res = self.memory_engine.create_reminder("user_aaa_111", "Alice dentist", time.time() + 3600)
        rem_id = rem_res["reminder"]["id"]
        rem_cross = self.client.patch(
            f"/api/reminders/{rem_id}",
            headers={"Authorization": f"Bearer {self.tokens_b['access_token']}"},
            json={"status": "done"}
        )
        self.assertEqual(rem_cross.status_code, 404)

    def test_unauthenticated_memory_access_blocked(self):
        """Access to /api/memory without token is rejected with 401."""
        resp = self.client.get("/api/memory")
        self.assertEqual(resp.status_code, 401)

    def test_conversation_history_isolation(self):
        """User A conversation turns cannot be accessed or deleted by User B."""
        self.memory_engine.observe("user_aaa_111", {"user": "Hello AIVA, I am Alice", "assistant": "Hello Alice!"})
        self.memory_engine.observe("user_bbb_222", {"user": "What is quantum computing?", "assistant": "It is..."})

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
        mem_json = mem_resp.get_json()
        self.assertEqual(mem_json["memories"], [])
        self.assertEqual(mem_json["entities"], [])
        self.assertEqual(mem_json["reminders"], [])
        self.assertEqual(mem_json["task_patterns"], {})

        # 3. /api/history is completely empty
        hist_resp = self.client.get(
            "/api/history",
            headers={"Authorization": f"Bearer {new_tokens['access_token']}"}
        )
        self.assertEqual(hist_resp.status_code, 200)
        self.assertEqual(hist_resp.get_json()["history"], [])


if __name__ == "__main__":
    unittest.main()

