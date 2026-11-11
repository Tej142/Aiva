"""Regression & feature tests for Phase 1(D) identity pinning and greeting bug fix."""

import asyncio
import glob
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.interaction.input_handler import InputHandler
from core.memory.engine import MemoryEngine
from core.memory.extractor import MemoryExtractor
from core.memory.models import Channel, MemoryItem, MemorySource, MemoryStatus, MemoryType
from core.memory.safety import (
    classify_non_name_identity,
    parse_call_me_command,
    reclassify_invalid_name_memory,
    scrub_vocative_identity_turn,
)
from services.conversation_service import ConversationService
from services.database_service import DatabaseService
from services.intent_router_service import IntentRouterService


class _FakeEventBus:
    def __init__(self):
        self.events = []

    async def emit(self, *args, **kwargs):
        self.events.append((args, kwargs))


class TestIdentityGreetingFix(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="aiva_identity_test_")
        self.db_path = os.path.join(self.tmpdir, "test_aiva.db")
        self._engines = []
        self._dbs = []

    def tearDown(self):
        for eng in self._engines:
            try:
                eng.shutdown()
            except Exception:
                pass
        for db in self._dbs:
            try:
                conn = getattr(getattr(db, "_local", None), "conn", None)
                if conn is not None:
                    conn.close()
                    db._local.conn = None
            except Exception:
                pass
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _track(self, db: DatabaseService, start_worker: bool = False) -> MemoryEngine:
        self._dbs.append(db)
        eng = MemoryEngine(db, start_worker=start_worker, enable_async_embeddings=False)
        self._engines.append(eng)
        return eng

    def test_legacy_upgrade_remediation_backup_idempotent_and_hi_prompt(self):
        """End-to-end regression: legacy `name: Telugu` + assistant greeting -> Charan."""
        uid = "11111111-1111-1111-1111-111111111111"
        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            """
            CREATE TABLE users (
                id TEXT PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                name TEXT DEFAULT '',
                onboarded INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE user_memories (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                key TEXT NOT NULL,
                value_json TEXT NOT NULL,
                created_at REAL,
                updated_at REAL
            );
            CREATE TABLE conversation_history (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                role TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at REAL
            );
            """
        )
        conn.execute(
            "INSERT INTO users (id, email, name, onboarded, created_at, is_active) VALUES (?, ?, ?, ?, ?, ?)",
            (uid, "charan@example.com", "Charan", 1, 900.0, 1),
        )
        conn.execute(
            "INSERT INTO user_memories (id, user_id, key, value_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("legacy-mem-1", uid, "name", json.dumps("Telugu"), 1000.0, 1000.0),
        )
        conn.execute(
            "INSERT INTO conversation_history (id, user_id, role, message, created_at) VALUES (?, ?, ?, ?, ?)",
            ("h1", uid, "user", "I am Telugu.", 1001.0),
        )
        conn.execute(
            "INSERT INTO conversation_history (id, user_id, role, message, created_at) VALUES (?, ?, ?, ?, ?)",
            ("h2", uid, "aiva", "Good day, Telugu! How's everything going?", 1002.0),
        )
        conn.execute(
            "INSERT INTO conversation_history (id, user_id, role, message, created_at) VALUES (?, ?, ?, ?, ?)",
            ("h3", uid, "aiva", "Nenu Telugu matladutunnanu, Charan.", 1003.0),
        )
        conn.commit()
        conn.close()

        # Boot DatabaseService (triggers legacy migration -> drop -> v3_identity_remediation + backup)
        db = DatabaseService(self.db_path)
        engine = self._track(db)

        backups = glob.glob(self.db_path + ".pre_identity_v3.*")
        self.assertTrue(backups, "Expected pre_identity_v3 SQLite backup before data migration")
        self.assertTrue(os.path.exists(backups[0]))

        # Legacy upgrade normalizes `name: Telugu` -> `language/origin: Telugu` cleanly
        all_sem = engine.store.list_memories(uid, mem_type=MemoryType.SEMANTIC.value, status=None, limit=50)
        by_content = {m.content: m for m in all_sem}
        self.assertIn("language/origin: Telugu", by_content)
        self.assertEqual(by_content["language/origin: Telugu"].status, MemoryStatus.ACTIVE.value)

        # Working turns: assistant greeting scrubbed to Charan, while user turn & language mention remain intact
        working = engine.store.list_memories(uid, mem_type=MemoryType.WORKING.value, status="active", limit=50)
        w_contents = [m.content for m in working]
        self.assertIn("user: I am Telugu.", w_contents)
        self.assertIn("aiva: Nenu Telugu matladutunnanu, Charan.", w_contents)
        self.assertTrue(
            any("Good day, Charan!" in c for c in w_contents),
            f"Expected assistant vocative 'Telugu' scrubbed to 'Charan' in {w_contents}",
        )
        self.assertFalse(any("Good day, Telugu!" in c for c in w_contents))

        # Idempotent re-run must be a clean no-op
        second = engine.remediate_identity_memories()
        self.assertTrue(second.get("already_applied"))
        self.assertEqual(second.get("reclassified"), 0)
        self.assertEqual(second.get("scrubbed"), 0)

        # Verify `"hi"` prompt sent through InputHandler -> ConversationService
        captured = {}

        async def _fake_create(**kwargs):
            captured["messages"] = kwargs["messages"]
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="Hey Charan, how's it going?"))]
            )

        conv = ConversationService()
        fake_client = MagicMock()
        fake_client.chat.completions.create = AsyncMock(side_effect=_fake_create)
        conv._get_client = lambda _key=None: fake_client

        handler = InputHandler(
            event_bus=_FakeEventBus(),
            intent_router_service=IntentRouterService(),
            conversation_service=conv,
            memory_engine=engine,
        )
        result = asyncio.run(handler.process_query("hi", speak=False, user_id=uid, channel="hud"))

        self.assertIn("Charan", result["response"])
        self.assertNotIn("Telugu", result["response"])

        sys_msg = captured["messages"][0]["content"]
        self.assertIn('<identity user_id="11111111" display_name="Charan" authoritative="true">', sys_msg)
        self.assertLess(sys_msg.index("<identity"), sys_msg.index("<memory"))
        self.assertIn("language/origin: Telugu", sys_msg)
        self.assertNotIn("name: Telugu", sys_msg)

    def test_v2_db_remediation_supersedes_without_deleting_bogus_name(self):
        """Production VM upgrade path: pre-existing `name: Telugu` in `memories` is superseded, never deleted."""
        uid = "33333333-3333-3333-3333-333333333333"
        v2_path = os.path.join(self.tmpdir, "v2_vm.db")
        db_setup = DatabaseService(v2_path)
        db_setup.create_user(uid, "charan@example.com", name="Charan", onboarded=1)

        # Simulate pre-fix VM state where `name: Telugu` & vocative working turn already exist in `memories`
        conn = db_setup.get_connection()
        conn.execute(
            """
            INSERT INTO memories (
                id, user_id, type, content, summary, entities_json, importance, confidence,
                source, channel, created_at, last_accessed, access_count, valid_from, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "314af5b7-0000-0000-0000-000000000001",
                uid,
                "semantic",
                "name: Telugu",
                "name: Telugu",
                "[]",
                0.9,
                0.9,
                "inferred",
                "hud",
                1000.0,
                1000.0,
                1,
                1000.0,
                "active",
            ),
        )
        conn.execute(
            """
            INSERT INTO memories (
                id, user_id, type, content, summary, entities_json, importance, confidence,
                source, channel, created_at, last_accessed, access_count, valid_from, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "turn-bad-1",
                uid,
                "working",
                "aiva: Hey Telugu, how's it going?",
                "aiva: Hey Telugu, how's it going?",
                "[]",
                0.3,
                0.9,
                "system",
                "hud",
                1001.0,
                1001.0,
                0,
                1001.0,
                "active",
            ),
        )
        conn.execute("DELETE FROM memory_migrations WHERE id = 'v3_identity_remediation'")
        conn.commit()
        raw_conn = getattr(getattr(db_setup, "_local", None), "conn", None)
        if raw_conn is not None:
            raw_conn.close()
            db_setup._local.conn = None

        # Rebooting DatabaseService runs v3_identity_remediation + creates pre-migration backup
        db = DatabaseService(v2_path)
        engine = self._track(db)

        backups = glob.glob(v2_path + ".pre_identity_v3.*")
        self.assertTrue(backups, "Expected pre_identity_v3 SQLite backup before v2->v3 remediation")

        all_sem = engine.store.list_memories(uid, mem_type=MemoryType.SEMANTIC.value, status=None, limit=50)
        by_content = {m.content: m for m in all_sem}
        self.assertIn("name: Telugu", by_content)
        old_row = by_content["name: Telugu"]
        self.assertEqual(old_row.id, "314af5b7-0000-0000-0000-000000000001")
        self.assertEqual(old_row.status, MemoryStatus.SUPERSEDED.value)
        self.assertIsNotNone(old_row.superseded_by)

        self.assertIn("language/origin: Telugu", by_content)
        new_row = by_content["language/origin: Telugu"]
        self.assertEqual(new_row.status, MemoryStatus.ACTIVE.value)
        self.assertEqual(old_row.superseded_by, new_row.id)

        working = engine.store.list_memories(uid, mem_type=MemoryType.WORKING.value, status="active", limit=50)
        w_contents = [m.content for m in working]
        self.assertIn("aiva: Hey Charan, how's it going?", w_contents)
        self.assertFalse(any("Hey Telugu" in c for c in w_contents))

        again = engine.remediate_identity_memories()
        self.assertTrue(again.get("already_applied"))
        self.assertEqual(again.get("reclassified"), 0)
        self.assertEqual(again.get("scrubbed"), 0)

    def test_extractor_hardening_blocks_language_and_nationality_as_name(self):
        """Language/nationality/ethnicity statements can never populate `name`."""
        payload = json.dumps(
            {
                "memories": [
                    {"type": "semantic", "content": "name: Telugu", "summary": "User is Telugu", "entities": ["Telugu"], "importance": 0.9, "confidence": 0.9},
                    {"type": "semantic", "content": "name: Tamil", "summary": "User name is Tamil", "entities": ["Tamil"], "importance": 0.8, "confidence": 0.9},
                    {"type": "semantic", "content": "name: Indian", "summary": "User is Indian", "entities": ["Indian"], "importance": 0.8, "confidence": 0.9},
                    {"type": "semantic", "content": "city: Hyderabad", "summary": "Lives in Hyderabad", "entities": ["Hyderabad"], "importance": 0.8, "confidence": 0.9},
                ],
                "entities": [
                    {"name": "Telugu", "kind": "person", "aliases": [], "attributes": {}},
                ],
                "relations": [],
            }
        )
        parsed = MemoryExtractor.validate_extraction_payload(payload)
        contents = [m["content"] for m in parsed["memories"]]
        self.assertIn("language/origin: Telugu", contents)
        self.assertIn("language/origin: Tamil", contents)
        self.assertIn("nationality/origin: Indian", contents)
        self.assertIn("city: Hyderabad", contents)
        self.assertFalse(any(c.lower().startswith("name:") for c in contents))
        self.assertEqual(parsed["entities"][0]["kind"], "other")

        # Hot-path remember command + DB insert defense-in-depth
        db = DatabaseService(self.db_path)
        uid_noname = "22222222-2222-2222-2222-222222222222"
        db.create_user(uid_noname, "noname@example.com", name="", onboarded=1)
        db.update_user(uid_noname, name="")
        engine = self._track(db)

        engine.handle_memory_intent(uid_noname, "remember that my name is Telugu")
        engine.handle_memory_intent(uid_noname, "remember that my name is Indian")
        active_sem = [
            m.content
            for m in engine.store.list_memories(uid_noname, mem_type="semantic", status="active")
        ]
        self.assertIn("language/origin: Telugu", active_sem)
        self.assertIn("nationality/origin: Indian", active_sem)
        self.assertFalse(any(c.lower().startswith("name:") for c in active_sem))
        self.assertIsNone(engine.get_user_display_name(uid_noname))

    def test_call_me_preferred_name_precedence_guards_and_user_isolation(self):
        """`call me X` sets immutable preferred_name over extracted memories and respects isolation."""
        db = DatabaseService(self.db_path)
        uid_a = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        uid_b = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
        db.create_user(uid_a, "a@example.com", name="Charan", onboarded=1)
        db.create_user(uid_b, "b@example.com", name="Priya", onboarded=1)
        engine = self._track(db)

        # Router handles valid "call me X" locally to `memory`, rejects temporal/action phrases
        router = IntentRouterService()
        self.assertEqual(asyncio.run(router.route("call me Sunny"))["route"], "memory")
        self.assertEqual(asyncio.run(router.route("please address me as Captain"))["route"], "memory")
        self.assertNotEqual(asyncio.run(router.route("call me back in 10 minutes"))["route"], "memory")
        self.assertNotEqual(asyncio.run(router.route("call me tomorrow at 9am"))["route"], "memory")
        self.assertIsNone(parse_call_me_command("call me later"))

        # Explicit "call me Sunny" updates preferred_name without mutating users.name
        res = engine.handle_memory_intent(uid_a, "call me Sunny")
        self.assertTrue(res["handled"])
        self.assertEqual(res["preferred_name"], "Sunny")
        self.assertIn("Sunny", res["response"])
        self.assertEqual(engine.get_preferred_name(uid_a), "Sunny")
        self.assertEqual(engine.get_user_display_name(uid_a), "Sunny")
        self.assertEqual(db.get_user_by_id(uid_a)["name"], "Charan")

        # Background extracted name memory cannot override preferred_name and is suppressed from prompt context
        bogus_extract = MemoryItem(
            id="ext-name-1",
            user_id=uid_a,
            type=MemoryType.SEMANTIC.value,
            content="name: Vikram",
            summary="name: Vikram",
            source=MemorySource.INFERRED.value,
            channel=Channel.HUD.value,
        )
        ok, _ = engine.store.insert_memory(bogus_extract)
        self.assertTrue(ok)
        self.assertEqual(engine.get_user_display_name(uid_a), "Sunny")
        ctx_a = engine.build_context(uid_a, "who am I", channel="hud")
        self.assertNotIn("name: Vikram", ctx_a)

        # Per-user isolation: User B unaffected
        self.assertIsNone(engine.get_preferred_name(uid_b))
        self.assertEqual(engine.get_user_display_name(uid_b), "Priya")

        # Reset preferred name returns cleanly to users.name ("Charan")
        reset_res = engine.handle_memory_intent(uid_a, "call me by my real name")
        self.assertTrue(reset_res["handled"])
        self.assertEqual(reset_res["preferred_name"], "Charan")
        self.assertIsNone(engine.get_preferred_name(uid_a))
        self.assertEqual(engine.get_user_display_name(uid_a), "Charan")

    def test_non_name_classification_and_vocative_scrub_helpers(self):
        """Unit checks on identity safety helpers."""
        self.assertEqual(classify_non_name_identity("Telugu"), "language/origin")
        self.assertEqual(classify_non_name_identity("South Indian"), "nationality/origin")
        self.assertIsNone(classify_non_name_identity("Charan"))

        c, s, changed, val = reclassify_invalid_name_memory("my name is Telugu", "name: Telugu")
        self.assertTrue(changed)
        self.assertEqual(c, "language/origin: Telugu")
        self.assertEqual(val, "Telugu")

        rewritten, scrubbed = scrub_vocative_identity_turn("aiva: Hey Telugu, how's it going?", "Telugu", "Charan")
        self.assertTrue(scrubbed)
        self.assertEqual(rewritten, "aiva: Hey Charan, how's it going?")

        untouched, was_scrubbed = scrub_vocative_identity_turn("aiva: I speak Telugu fluently, Charan.", "Telugu", "Charan")
        self.assertFalse(was_scrubbed)
        self.assertEqual(untouched, "aiva: I speak Telugu fluently, Charan.")


if __name__ == "__main__":
    unittest.main()
