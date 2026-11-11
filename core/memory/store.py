"""SQLite storage layer for AIVA's unified memory engine.

All queries and tables (except the content-hash embedding cache) are strictly
scoped by ``user_id``. No user memory content is ever logged.
"""

from datetime import datetime
import json
import os
import re
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional, Set, Tuple

import numpy as np

from core.memory.models import (
    EntityItem,
    EntityLink,
    MemoryItem,
    MemorySettings,
    MemoryStatus,
    MemoryType,
    ReminderItem,
    ReminderStatus,
    VALID_CHANNELS,
    VALID_ENTITY_KINDS,
    VALID_MEMORY_SOURCES,
    VALID_MEMORY_STATUSES,
    VALID_MEMORY_TYPES,
    VALID_REMINDER_STATUSES,
    content_hash,
    new_id,
)
from core.memory.safety import (
    contains_sensitive_data,
    log_memory,
    reclassify_invalid_name_memory,
    sanitize_for_prompt,
    scrub_vocative_identity_turn,
    validate_source_for_memory,
)


def _safe_json_loads(raw: Optional[str], default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


def _build_fts_query(query: str) -> str:
    """Convert free-text user input into a safe FTS5 OR prefix/term expression."""
    if not query:
        return ""
    tokens = re.findall(r"[A-Za-z0-9_]{2,}", query.lower())
    if not tokens:
        return ""
    # Deduplicate preserving order, cap to keep FTS query tiny and fast
    seen = set()
    terms: List[str] = []
    for tok in tokens:
        if tok in seen:
            continue
        seen.add(tok)
        terms.append(f'"{tok}"*')
        if len(terms) >= 10:
            break
    return " OR ".join(terms)


class MemoryStore:
    """Thread-safe, user-isolated SQLite persistence for AIVA's memory engine."""

    def __init__(self, db):
        self.db = db
        self._lock = threading.RLock()
        self.ensure_schema()

    def _conn(self) -> sqlite3.Connection:
        return self.db.get_connection()

    # ------------------------------------------------------------------
    # SCHEMA & MIGRATIONS
    # ------------------------------------------------------------------

    def ensure_schema(self) -> None:
        with self._lock:
            conn = self._conn()
            cur = conn.cursor()

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_migrations (
                    id TEXT PRIMARY KEY,
                    applied_at REAL NOT NULL
                )
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    type TEXT NOT NULL,
                    content TEXT NOT NULL,
                    summary TEXT DEFAULT '',
                    entities_json TEXT DEFAULT '[]',
                    importance REAL NOT NULL DEFAULT 0.5,
                    confidence REAL NOT NULL DEFAULT 0.8,
                    source TEXT NOT NULL DEFAULT 'user_said',
                    channel TEXT NOT NULL DEFAULT 'hud',
                    created_at REAL NOT NULL,
                    last_accessed REAL NOT NULL,
                    access_count INTEGER NOT NULL DEFAULT 0,
                    valid_from REAL,
                    superseded_by TEXT,
                    expires_at REAL,
                    status TEXT NOT NULL DEFAULT 'active',
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                )
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_memories_user_status_type ON memories(user_id, status, type)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_memories_user_expires ON memories(user_id, expires_at)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_memories_user_accessed ON memories(user_id, last_accessed)"
            )

            cur.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
                    memory_id UNINDEXED,
                    user_id UNINDEXED,
                    content,
                    summary,
                    entities
                )
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_embeddings (
                    memory_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    vector BLOB NOT NULL,
                    model TEXT NOT NULL,
                    dim INTEGER NOT NULL,
                    FOREIGN KEY (memory_id) REFERENCES memories(id) ON DELETE CASCADE,
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                )
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_memory_embeddings_user ON memory_embeddings(user_id)"
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS embedding_cache (
                    content_hash TEXT PRIMARY KEY,
                    vector BLOB NOT NULL,
                    model TEXT NOT NULL,
                    dim INTEGER NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS entities (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'other',
                    aliases_json TEXT DEFAULT '[]',
                    attributes_json TEXT DEFAULT '{}',
                    whatsapp_contact_id TEXT,
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                )
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_entities_user_name ON entities(user_id, name)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_entities_wa_contact ON entities(user_id, whatsapp_contact_id)"
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS entity_links (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    from_id TEXT NOT NULL,
                    to_id TEXT NOT NULL,
                    relation TEXT NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                    FOREIGN KEY (from_id) REFERENCES entities(id) ON DELETE CASCADE,
                    FOREIGN KEY (to_id) REFERENCES entities(id) ON DELETE CASCADE
                )
                """
            )
            cur.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_entity_links_uniq ON entity_links(user_id, from_id, to_id, relation)"
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS reminders (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    text TEXT NOT NULL,
                    due_at REAL NOT NULL,
                    recurrence TEXT,
                    channel TEXT NOT NULL DEFAULT 'hud',
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_from_memory_id TEXT,
                    urgent INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                )
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(status, due_at)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_reminders_user ON reminders(user_id, status, due_at)"
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_audit (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    memory_id TEXT,
                    created_at REAL NOT NULL
                )
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_memory_audit_user ON memory_audit(user_id, created_at)"
            )

            conn.commit()

    def _has_migration(self, migration_id: str) -> bool:
        row = self._conn().execute(
            "SELECT 1 FROM memory_migrations WHERE id = ?",
            (migration_id,),
        ).fetchone()
        return row is not None

    def _mark_migration(self, migration_id: str) -> None:
        conn = self._conn()
        conn.execute(
            "INSERT OR IGNORE INTO memory_migrations (id, applied_at) VALUES (?, ?)",
            (migration_id, time.time()),
        )
        conn.commit()

    def migrate_legacy_data(self, knowledge_json_path: str = "memory/knowledge_memory.json") -> int:
        """Idempotently migrate data from old SQLite tables and knowledge JSON without dropping old tables."""
        t0 = time.perf_counter()
        migrated_count = 0
        with self._lock:
            if self._has_migration("v1_backfill_legacy"):
                return 0

            conn = self._conn()
            now = time.time()
            tables = {
                r["name"]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }

            # 1. Migrate legacy user_memories -> semantic memories
            if "user_memories" in tables:
                rows = conn.execute(
                    "SELECT user_id, key, value_json, created_at, updated_at FROM user_memories"
                ).fetchall()
                for row in rows:
                    uid = row["user_id"]
                    key = row["key"]
                    val = _safe_json_loads(row["value_json"], row["value_json"])
                    if val is None or val == "":
                        continue
                    text_val = ", ".join(str(x) for x in val) if isinstance(val, list) else str(val)
                    content = f"{key.replace('_', ' ')}: {text_val}"
                    sensitive, _ = contains_sensitive_data(content)
                    if sensitive:
                        continue
                    item = MemoryItem(
                        id=new_id(),
                        user_id=uid,
                        type=MemoryType.SEMANTIC.value,
                        content=content,
                        summary=content[:180],
                        entities=[],
                        importance=0.85 if key in ("name", "location", "profession") else 0.65,
                        confidence=0.95,
                        source="user_said",
                        channel="hud",
                        created_at=float(row["created_at"] or now),
                        last_accessed=float(row["updated_at"] or now),
                        access_count=0,
                        valid_from=float(row["created_at"] or now),
                        status=MemoryStatus.ACTIVE.value,
                    )
                    self.insert_memory(item, skip_sensitive_check=True)
                    migrated_count += 1

            # 2. Migrate legacy conversation_history -> working memories (preserving turns)
            if "conversation_history" in tables:
                rows = conn.execute(
                    "SELECT user_id, role, message, created_at FROM conversation_history ORDER BY created_at ASC"
                ).fetchall()
                for row in rows:
                    msg = (row["message"] or "").strip()
                    if not msg:
                        continue
                    sensitive, _ = contains_sensitive_data(msg)
                    if sensitive:
                        continue
                    role = row["role"] or "user"
                    item = MemoryItem(
                        id=new_id(),
                        user_id=row["user_id"],
                        type=MemoryType.WORKING.value,
                        content=f"{role}: {msg}",
                        summary=msg[:160],
                        entities=[],
                        importance=0.35,
                        confidence=0.85,
                        source="system" if role == "assistant" else "user_said",
                        channel="hud",
                        created_at=float(row["created_at"] or now),
                        last_accessed=float(row["created_at"] or now),
                        status=MemoryStatus.ACTIVE.value,
                    )
                    self.insert_memory(item, skip_sensitive_check=True)
                    migrated_count += 1

            # 3. Link existing whatsapp_contacts -> entities for all users (no duplicate phones)
            if "whatsapp_contacts" in tables:
                c_rows = conn.execute(
                    "SELECT id, user_id, display_name, aliases_json FROM whatsapp_contacts"
                ).fetchall()
                for c in c_rows:
                    self.sync_whatsapp_contact_entity(
                        user_id=c["user_id"],
                        contact={
                            "id": c["id"],
                            "display_name": c["display_name"],
                            "aliases": _safe_json_loads(c["aliases_json"], []),
                        },
                    )

            # 4. Migrate legacy memory/knowledge_memory.json -> primary user's knowledge entries
            if knowledge_json_path and os.path.exists(knowledge_json_path):
                primary = self.db.get_primary_user()
                if primary:
                    try:
                        with open(knowledge_json_path, "r", encoding="utf-8") as fh:
                            knowledge_data = json.load(fh)
                        if isinstance(knowledge_data, dict):
                            expires = now + 30 * 86400
                            for entity, entry in knowledge_data.items():
                                if isinstance(entry, dict):
                                    summary = entry.get("summary") or ""
                                    facts = entry.get("facts") or []
                                    text = f"{entity}: {summary}"
                                    if facts:
                                        text += " (" + "; ".join(str(x) for x in facts[:5]) + ")"
                                else:
                                    text = f"{entity}: {entry}"
                                    summary = str(entry)[:180]
                                item = MemoryItem(
                                    id=new_id(),
                                    user_id=primary["id"],
                                    type=MemoryType.KNOWLEDGE.value,
                                    content=text.strip(),
                                    summary=str(summary)[:200],
                                    entities=[str(entity)],
                                    importance=0.5,
                                    confidence=0.8,
                                    source="web",
                                    channel="hud",
                                    created_at=now,
                                    last_accessed=now,
                                    expires_at=expires,
                                    status=MemoryStatus.ACTIVE.value,
                                )
                                self.insert_memory(item, skip_sensitive_check=True)
                                migrated_count += 1
                    except Exception:
                        pass

            self._mark_migration("v1_backfill_legacy")

        log_memory("migrate_v1", (time.perf_counter() - t0) * 1000, "ok", count=migrated_count)
        return migrated_count

    def backup_database(self, suffix: str = "bak") -> Optional[str]:
        """Create an atomic WAL-safe SQLite backup of file-backed storage before migrations."""
        db_path = getattr(self.db, "db_path", None)
        if not db_path or db_path == ":memory:" or not os.path.exists(db_path):
            return None
        try:
            if os.path.getsize(db_path) <= 0:
                return None
        except OSError:
            return None

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = f"{db_path}.{suffix}.{ts}"
        counter = 1
        while os.path.exists(backup_path):
            backup_path = f"{db_path}.{suffix}.{ts}_{counter}"
            counter += 1

        with self._lock:
            src_conn = self._conn()
            src_conn.commit()
            dst_conn = sqlite3.connect(backup_path)
            try:
                src_conn.backup(dst_conn)
            finally:
                dst_conn.close()
        return backup_path

    def remediate_identity_memories(self) -> Dict[str, Any]:
        """Idempotently reclassify language/origin-as-name rows and scrub bogus vocative working turns."""
        t0 = time.perf_counter()
        reclassified = 0
        scrubbed = 0
        backup_path: Optional[str] = None

        with self._lock:
            if self._has_migration("v3_identity_remediation"):
                return {"already_applied": True, "reclassified": 0, "scrubbed": 0, "backup_path": None}

            conn = self._conn()
            has_rows = conn.execute("SELECT 1 FROM memories LIMIT 1").fetchone()
            if not has_rows:
                return {"already_applied": False, "reclassified": 0, "scrubbed": 0, "backup_path": None}

            sem_rows = conn.execute(
                "SELECT * FROM memories WHERE type = ? AND status = ?",
                (MemoryType.SEMANTIC.value, MemoryStatus.ACTIVE.value),
            ).fetchall()

            bad_semantic_ops: List[Tuple[MemoryItem, str, str, str]] = []
            bad_tokens_by_user: Dict[str, Set[str]] = {}
            for row in sem_rows:
                item = self._row_to_memory(row)
                new_content, new_summary, changed, bad_tok = reclassify_invalid_name_memory(
                    item.content, item.summary
                )
                if changed and bad_tok:
                    bad_semantic_ops.append((item, new_content, new_summary, bad_tok))
                    bad_tokens_by_user.setdefault(item.user_id, set()).add(bad_tok)

            work_rows = conn.execute(
                "SELECT * FROM memories WHERE type = ? AND status = ?",
                (MemoryType.WORKING.value, MemoryStatus.ACTIVE.value),
            ).fetchall()

            # Inspect assistant/aiva working turns for bogus vocative salutations (including historical Telugu)
            user_target_cache: Dict[str, str] = {}
            working_updates: List[Tuple[MemoryItem, str]] = []
            for row in work_rows:
                item = self._row_to_memory(row)
                uid = item.user_id
                if uid not in user_target_cache:
                    pref = self.get_preferred_name(uid)
                    u_rec = self.db.get_user_by_id(uid) if hasattr(self.db, "get_user_by_id") else None
                    u_name = (u_rec.get("name") or "").strip() if u_rec else ""
                    user_target_cache[uid] = pref or u_name or "there"
                target_name = user_target_cache[uid]
                tokens = set(bad_tokens_by_user.get(uid, set())) | {"Telugu"}
                # Avoid rewriting if the user's actual authoritative name happens to be one of those tokens
                tokens = {tok for tok in tokens if tok.lower() != target_name.lower()}
                updated_text = item.content
                did_change = False
                for tok in sorted(tokens, key=len, reverse=True):
                    updated_text, step_changed = scrub_vocative_identity_turn(updated_text, tok, target_name)
                    if step_changed:
                        did_change = True
                if did_change and updated_text != item.content:
                    working_updates.append((item, updated_text))

            if bad_semantic_ops or working_updates:
                backup_path = self.backup_database(suffix="pre_identity_v3")

            for old_item, new_content, new_summary, bad_tok in bad_semantic_ops:
                existing_reclass = conn.execute(
                    """
                    SELECT id FROM memories
                    WHERE user_id = ? AND type = ? AND status = ? AND lower(trim(content)) = lower(trim(?))
                    LIMIT 1
                    """,
                    (old_item.user_id, MemoryType.SEMANTIC.value, MemoryStatus.ACTIVE.value, new_content),
                ).fetchone()
                if existing_reclass:
                    target_id = existing_reclass["id"]
                else:
                    reclass_item = MemoryItem(
                        id=new_id(),
                        user_id=old_item.user_id,
                        type=MemoryType.SEMANTIC.value,
                        content=new_content,
                        summary=new_summary,
                        entities=[bad_tok],
                        importance=min(0.75, float(old_item.importance)),
                        confidence=old_item.confidence,
                        source=old_item.source,
                        channel=old_item.channel,
                        created_at=old_item.created_at,
                        last_accessed=time.time(),
                        valid_from=old_item.valid_from or old_item.created_at,
                        status=MemoryStatus.ACTIVE.value,
                    )
                    ok, _ = self.insert_memory(reclass_item, skip_sensitive_check=True)
                    if not ok:
                        continue
                    target_id = reclass_item.id

                if self.supersede_memory(old_item.user_id, old_item.id, target_id):
                    reclassified += 1

            for work_item, new_text in working_updates:
                new_body = re.sub(r"^(?:aiva|assistant)\s*:\s*", "", new_text, flags=re.IGNORECASE)
                work_item.content = new_text
                work_item.summary = new_body[:160]
                conn.execute(
                    "UPDATE memories SET content = ?, summary = ? WHERE id = ? AND user_id = ?",
                    (work_item.content, work_item.summary, work_item.id, work_item.user_id),
                )
                conn.execute(
                    "DELETE FROM memory_embeddings WHERE memory_id = ? AND user_id = ?",
                    (work_item.id, work_item.user_id),
                )
                self._sync_fts(conn, work_item)
                conn.commit()
                self.record_audit(work_item.user_id, "remediate_working", work_item.id)
                scrubbed += 1

            self._mark_migration("v3_identity_remediation")

        log_memory(
            "remediate_identity",
            (time.perf_counter() - t0) * 1000,
            "ok",
            count=reclassified + scrubbed,
        )
        return {
            "already_applied": False,
            "reclassified": reclassified,
            "scrubbed": scrubbed,
            "backup_path": backup_path,
        }

    def drop_legacy_tables(self) -> None:
        """Final migration step: drop obsolete user_memories and conversation_history tables."""
        t0 = time.perf_counter()
        with self._lock:
            if self._has_migration("v2_drop_legacy_tables"):
                return
            conn = self._conn()
            conn.execute("DROP TABLE IF EXISTS user_memories")
            conn.execute("DROP TABLE IF EXISTS conversation_history")
            conn.commit()
            self._mark_migration("v2_drop_legacy_tables")
        log_memory("migrate_v2_drop", (time.perf_counter() - t0) * 1000, "ok")

    # ------------------------------------------------------------------
    # AUDIT LOGGING (NO CONTENT EVER)
    # ------------------------------------------------------------------

    def record_audit(self, user_id: str, action: str, memory_id: Optional[str] = None) -> None:
        with self._lock:
            conn = self._conn()
            conn.execute(
                """
                INSERT INTO memory_audit (id, user_id, action, memory_id, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (new_id(), user_id, action, memory_id, time.time()),
            )
            conn.commit()

    def get_audit_logs(self, user_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        rows = self._conn().execute(
            """
            SELECT id, user_id, action, memory_id, created_at
            FROM memory_audit
            WHERE user_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # SETTINGS (reuses user_settings table)
    # ------------------------------------------------------------------

    def get_memory_settings(self, user_id: str) -> MemorySettings:
        settings_dict = self.db.get_user_settings(user_id) or {}
        mem_cfg = settings_dict.get("memory") if isinstance(settings_dict.get("memory"), dict) else settings_dict
        return MemorySettings.from_dict(mem_cfg)

    def update_memory_settings(self, user_id: str, patch: Dict[str, Any]) -> MemorySettings:
        with self._lock:
            full = self.db.get_user_settings(user_id) or {}
            current = self.get_memory_settings(user_id).to_dict()
            for key in current:
                if key in patch and patch[key] is not None:
                    current[key] = patch[key]
            updated = MemorySettings.from_dict(current)
            full["memory"] = updated.to_dict()
            self.db.set_user_settings(user_id, full)
            self.record_audit(user_id, "settings_update", None)
            return updated

    def get_preferred_name(self, user_id: Optional[str]) -> Optional[str]:
        """Return user's explicit `call me X` preferred name (never set by background extractors)."""
        if not user_id:
            return None
        settings_dict = self.db.get_user_settings(user_id) or {}
        val = settings_dict.get("preferred_name")
        if isinstance(val, str) and val.strip():
            return val.strip()
        return None

    def set_preferred_name(self, user_id: str, name: Optional[str]) -> Optional[str]:
        """Persist or clear an explicit `preferred_name` in per-user settings."""
        with self._lock:
            full = self.db.get_user_settings(user_id) or {}
            if not name or not str(name).strip():
                full.pop("preferred_name", None)
                self.db.set_user_settings(user_id, full)
                self.record_audit(user_id, "clear_preferred_name", None)
                return None

            raw = str(name).strip(" \t\r\n\"'“”‘’")
            if not raw or len(raw) > 40:
                raise ValueError("invalid_preferred_name")
            sensitive, cat = contains_sensitive_data(raw)
            if sensitive:
                raise ValueError(f"sensitive_{cat}")
            cleaned = sanitize_for_prompt(raw).strip()
            if not cleaned or len(cleaned) > 40 or "redacted-instruction" in cleaned:
                raise ValueError("invalid_preferred_name")

            full["preferred_name"] = cleaned
            self.db.set_user_settings(user_id, full)
            self.record_audit(user_id, "set_preferred_name", None)
            return cleaned

    # ------------------------------------------------------------------
    # MEMORY CRUD + FTS SYNC
    # ------------------------------------------------------------------

    def _row_to_memory(self, row: sqlite3.Row) -> MemoryItem:
        return MemoryItem(
            id=row["id"],
            user_id=row["user_id"],
            type=row["type"],
            content=row["content"],
            summary=row["summary"] or "",
            entities=_safe_json_loads(row["entities_json"], []),
            importance=float(row["importance"]),
            confidence=float(row["confidence"]),
            source=row["source"],
            channel=row["channel"],
            created_at=float(row["created_at"]),
            last_accessed=float(row["last_accessed"]),
            access_count=int(row["access_count"]),
            valid_from=row["valid_from"],
            superseded_by=row["superseded_by"],
            expires_at=row["expires_at"],
            status=row["status"],
        )

    def _sync_fts(self, conn: sqlite3.Connection, item: MemoryItem) -> None:
        conn.execute(
            "DELETE FROM memories_fts WHERE memory_id = ? AND user_id = ?",
            (item.id, item.user_id),
        )
        if item.status == MemoryStatus.ACTIVE.value:
            conn.execute(
                """
                INSERT INTO memories_fts (memory_id, user_id, content, summary, entities)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    item.id,
                    item.user_id,
                    item.content,
                    item.summary or "",
                    " ".join(item.entities or []),
                ),
            )

    def insert_memory(
        self,
        item: MemoryItem,
        is_verified_user: bool = True,
        skip_sensitive_check: bool = False,
    ) -> Tuple[bool, Optional[str]]:
        if item.type not in VALID_MEMORY_TYPES:
            return False, "invalid_type"
        if item.source not in VALID_MEMORY_SOURCES:
            return False, "invalid_source"
        if item.channel not in VALID_CHANNELS:
            item.channel = "hud"
        if item.status not in VALID_MEMORY_STATUSES:
            item.status = MemoryStatus.ACTIVE.value

        ok_src, src_err = validate_source_for_memory(
            item.type, item.source, is_verified_user=is_verified_user, expires_at=item.expires_at
        )
        if not ok_src:
            return False, src_err

        if not skip_sensitive_check:
            sensitive, category = contains_sensitive_data(item.content)
            if sensitive:
                log_memory("safety_filter", 0.0, "blocked", reason=category, user_id=item.user_id)
                return False, f"sensitive_{category}"

        if item.type == MemoryType.SEMANTIC.value:
            re_content, re_summary, was_reclassified, bad_tok = reclassify_invalid_name_memory(
                item.content, item.summary
            )
            if was_reclassified:
                item.content = re_content
                item.summary = re_summary
                if bad_tok and bad_tok not in item.entities:
                    item.entities = list(item.entities or []) + [bad_tok]

        item.importance = max(0.0, min(1.0, float(item.importance)))
        item.confidence = max(0.0, min(1.0, float(item.confidence)))

        with self._lock:
            conn = self._conn()
            conn.execute(
                """
                INSERT INTO memories (
                    id, user_id, type, content, summary, entities_json,
                    importance, confidence, source, channel,
                    created_at, last_accessed, access_count,
                    valid_from, superseded_by, expires_at, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.id,
                    item.user_id,
                    item.type,
                    item.content,
                    item.summary or "",
                    json.dumps(item.entities or []),
                    item.importance,
                    item.confidence,
                    item.source,
                    item.channel,
                    item.created_at,
                    item.last_accessed,
                    item.access_count,
                    item.valid_from,
                    item.superseded_by,
                    item.expires_at,
                    item.status,
                ),
            )
            self._sync_fts(conn, item)
            conn.commit()
        self.record_audit(item.user_id, "create", item.id)
        return True, None

    def get_memory(self, user_id: str, memory_id: str) -> Optional[MemoryItem]:
        row = self._conn().execute(
            "SELECT * FROM memories WHERE id = ? AND user_id = ?",
            (memory_id, user_id),
        ).fetchone()
        return self._row_to_memory(row) if row else None

    def list_memories(
        self,
        user_id: str,
        mem_type: Optional[str] = None,
        status: Optional[str] = MemoryStatus.ACTIVE.value,
        search: Optional[str] = None,
        limit: int = 100,
    ) -> List[MemoryItem]:
        now = time.time()
        sql = "SELECT * FROM memories WHERE user_id = ?"
        params: List[Any] = [user_id]
        if status:
            sql += " AND status = ?"
            params.append(status)
        if mem_type:
            sql += " AND type = ?"
            params.append(mem_type)
        sql += " AND (expires_at IS NULL OR expires_at > ?)"
        params.append(now)
        if search:
            like = f"%{search.strip()}%"
            sql += " AND (content LIKE ? OR summary LIKE ? OR entities_json LIKE ?)"
            params.extend([like, like, like])
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(int(limit))
        rows = self._conn().execute(sql, params).fetchall()
        return [self._row_to_memory(r) for r in rows]

    def get_working_turns(self, user_id: str, limit: int = 50) -> List[MemoryItem]:
        rows = self._conn().execute(
            """
            SELECT * FROM memories
            WHERE user_id = ? AND type = ? AND status = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (user_id, MemoryType.WORKING.value, MemoryStatus.ACTIVE.value, int(limit)),
        ).fetchall()
        items = [self._row_to_memory(r) for r in rows]
        items.reverse()
        return items

    def clear_working_turns(self, user_id: str) -> int:
        with self._lock:
            conn = self._conn()
            ids = [
                r["id"]
                for r in conn.execute(
                    "SELECT id FROM memories WHERE user_id = ? AND type = ?",
                    (user_id, MemoryType.WORKING.value),
                ).fetchall()
            ]
            if not ids:
                return 0
            for mid in ids:
                conn.execute("DELETE FROM memories_fts WHERE memory_id = ? AND user_id = ?", (mid, user_id))
                conn.execute("DELETE FROM memory_embeddings WHERE memory_id = ? AND user_id = ?", (mid, user_id))
                conn.execute("DELETE FROM memories WHERE id = ? AND user_id = ?", (mid, user_id))
            conn.commit()
        self.record_audit(user_id, "clear_working", None)
        return len(ids)

    def update_memory(
        self,
        user_id: str,
        memory_id: str,
        content: Optional[str] = None,
        summary: Optional[str] = None,
        importance: Optional[float] = None,
        mem_type: Optional[str] = None,
        entities: Optional[List[str]] = None,
    ) -> Optional[MemoryItem]:
        with self._lock:
            existing = self.get_memory(user_id, memory_id)
            if not existing:
                return None

            if content is not None:
                sensitive, category = contains_sensitive_data(content)
                if sensitive:
                    raise ValueError(f"sensitive_{category}")
                existing.content = content.strip()
            if summary is not None:
                existing.summary = summary.strip()
            if importance is not None:
                existing.importance = max(0.0, min(1.0, float(importance)))
            if mem_type is not None and mem_type in VALID_MEMORY_TYPES:
                existing.type = mem_type
            if entities is not None:
                existing.entities = entities

            conn = self._conn()
            conn.execute(
                """
                UPDATE memories
                SET content = ?, summary = ?, importance = ?, type = ?, entities_json = ?
                WHERE id = ? AND user_id = ?
                """,
                (
                    existing.content,
                    existing.summary,
                    existing.importance,
                    existing.type,
                    json.dumps(existing.entities),
                    memory_id,
                    user_id,
                ),
            )
            self._sync_fts(conn, existing)
            if content is not None:
                # Re-embed updated content cleanly
                conn.execute(
                    "DELETE FROM memory_embeddings WHERE memory_id = ? AND user_id = ?",
                    (memory_id, user_id),
                )
            conn.commit()
        self.record_audit(user_id, "update", memory_id)
        return existing

    def supersede_memory(self, user_id: str, old_memory_id: str, new_memory_id: str) -> bool:
        with self._lock:
            conn = self._conn()
            cur = conn.execute(
                """
                UPDATE memories
                SET status = ?, superseded_by = ?
                WHERE id = ? AND user_id = ? AND status = ?
                """,
                (
                    MemoryStatus.SUPERSEDED.value,
                    new_memory_id,
                    old_memory_id,
                    user_id,
                    MemoryStatus.ACTIVE.value,
                ),
            )
            conn.execute(
                "DELETE FROM memories_fts WHERE memory_id = ? AND user_id = ?",
                (old_memory_id, user_id),
            )
            conn.commit()
            changed = cur.rowcount > 0
        if changed:
            self.record_audit(user_id, "supersede", old_memory_id)
        return changed

    def forget_memory(self, user_id: str, memory_id: str) -> bool:
        """Permanently purge content from memories, FTS5, and embeddings, leaving a content-free audit row."""
        with self._lock:
            conn = self._conn()
            row = conn.execute(
                "SELECT id FROM memories WHERE id = ? AND user_id = ?",
                (memory_id, user_id),
            ).fetchone()
            if not row:
                return False
            conn.execute(
                "DELETE FROM memory_embeddings WHERE memory_id = ? AND user_id = ?",
                (memory_id, user_id),
            )
            conn.execute(
                "DELETE FROM memories_fts WHERE memory_id = ? AND user_id = ?",
                (memory_id, user_id),
            )
            conn.execute(
                "DELETE FROM memories WHERE id = ? AND user_id = ?",
                (memory_id, user_id),
            )
            conn.commit()
        self.record_audit(user_id, "forget", memory_id)
        return True

    def forget_all(self, user_id: str) -> int:
        """Delete all memories, FTS entries, embeddings, entities, links, and reminders for one user."""
        with self._lock:
            conn = self._conn()
            cnt = conn.execute(
                "SELECT COUNT(*) FROM memories WHERE user_id = ?",
                (user_id,),
            ).fetchone()[0]
            conn.execute("DELETE FROM memory_embeddings WHERE user_id = ?", (user_id,))
            conn.execute("DELETE FROM memories_fts WHERE user_id = ?", (user_id,))
            conn.execute("DELETE FROM memories WHERE user_id = ?", (user_id,))
            conn.execute("DELETE FROM entity_links WHERE user_id = ?", (user_id,))
            conn.execute("DELETE FROM entities WHERE user_id = ?", (user_id,))
            conn.execute("DELETE FROM reminders WHERE user_id = ?", (user_id,))
            conn.commit()
        self.record_audit(user_id, "forget_all", None)
        return int(cnt)

    def touch_access(self, user_id: str, memory_ids: List[str]) -> None:
        if not memory_ids:
            return
        now = time.time()
        with self._lock:
            conn = self._conn()
            placeholders = ",".join("?" for _ in memory_ids)
            conn.execute(
                f"""
                UPDATE memories
                SET access_count = access_count + 1, last_accessed = ?
                WHERE user_id = ? AND id IN ({placeholders})
                """,
                [now, user_id] + list(memory_ids),
            )
            conn.commit()

    def search_fts(
        self,
        user_id: str,
        query: str,
        limit: int = 20,
        exclude_types: Optional[Tuple[str, ...]] = None,
    ) -> List[Tuple[MemoryItem, float]]:
        fts_expr = _build_fts_query(query)
        if not fts_expr:
            return []
        now = time.time()
        sql = """
            SELECT m.*, bm25(memories_fts) AS rank_score
            FROM memories_fts
            JOIN memories m ON m.id = memories_fts.memory_id AND m.user_id = memories_fts.user_id
            WHERE memories_fts.user_id = ?
              AND memories_fts MATCH ?
              AND m.status = 'active'
              AND (m.expires_at IS NULL OR m.expires_at > ?)
        """
        params: List[Any] = [user_id, fts_expr, now]
        if exclude_types:
            ph = ",".join("?" for _ in exclude_types)
            sql += f" AND m.type NOT IN ({ph})"
            params.extend(exclude_types)
        sql += " ORDER BY rank_score ASC LIMIT ?"
        params.append(int(limit))
        try:
            rows = self._conn().execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            return []
        return [(self._row_to_memory(r), float(r["rank_score"] or 0.0)) for r in rows]

    def get_core_profile_memories(self, user_id: str, limit: int = 12) -> List[MemoryItem]:
        now = time.time()
        rows = self._conn().execute(
            """
            SELECT * FROM memories
            WHERE user_id = ?
              AND status = 'active'
              AND type = 'semantic'
              AND (expires_at IS NULL OR expires_at > ?)
            ORDER BY importance DESC, confidence DESC, access_count DESC, created_at DESC
            LIMIT ?
            """,
            (user_id, now, int(limit)),
        ).fetchall()
        return [self._row_to_memory(r) for r in rows]

    def expire_stale_memories(self, user_id: Optional[str] = None) -> int:
        now = time.time()
        with self._lock:
            conn = self._conn()
            if user_id:
                rows = conn.execute(
                    """
                    SELECT id, user_id FROM memories
                    WHERE user_id = ? AND status = 'active' AND expires_at IS NOT NULL AND expires_at <= ?
                    """,
                    (user_id, now),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT id, user_id FROM memories
                    WHERE status = 'active' AND expires_at IS NOT NULL AND expires_at <= ?
                    """,
                    (now,),
                ).fetchall()
            for r in rows:
                conn.execute("DELETE FROM memory_embeddings WHERE memory_id = ? AND user_id = ?", (r["id"], r["user_id"]))
                conn.execute("DELETE FROM memories_fts WHERE memory_id = ? AND user_id = ?", (r["id"], r["user_id"]))
                conn.execute("DELETE FROM memories WHERE id = ? AND user_id = ?", (r["id"], r["user_id"]))
                conn.execute(
                    "INSERT INTO memory_audit (id, user_id, action, memory_id, created_at) VALUES (?, ?, 'expire', ?, ?)",
                    (new_id(), r["user_id"], r["id"], now),
                )
            if rows:
                conn.commit()
            return len(rows)

    # ------------------------------------------------------------------
    # EMBEDDINGS & CONTENT-HASH ON-DISK CACHE
    # ------------------------------------------------------------------

    def get_cached_embedding(self, text: str, model: str) -> Optional[np.ndarray]:
        chash = content_hash(text)
        row = self._conn().execute(
            "SELECT vector, dim FROM embedding_cache WHERE content_hash = ? AND model = ?",
            (chash, model),
        ).fetchone()
        if not row:
            return None
        vec = np.frombuffer(row["vector"], dtype=np.float32)
        if vec.size != int(row["dim"]):
            return None
        return vec.copy()

    def set_cached_embedding(self, text: str, model: str, vector: List[float]) -> None:
        arr = np.asarray(vector, dtype=np.float32)
        if arr.size == 0:
            return
        chash = content_hash(text)
        with self._lock:
            conn = self._conn()
            conn.execute(
                """
                INSERT INTO embedding_cache (content_hash, vector, model, dim, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(content_hash) DO UPDATE SET
                    vector = excluded.vector,
                    model = excluded.model,
                    dim = excluded.dim,
                    updated_at = excluded.updated_at
                """,
                (chash, arr.tobytes(), model, int(arr.size), time.time()),
            )
            conn.commit()

    def upsert_memory_embedding(self, user_id: str, memory_id: str, vector: List[float], model: str) -> None:
        arr = np.asarray(vector, dtype=np.float32)
        if arr.size == 0:
            return
        with self._lock:
            conn = self._conn()
            conn.execute(
                """
                INSERT INTO memory_embeddings (memory_id, user_id, vector, model, dim)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(memory_id) DO UPDATE SET
                    vector = excluded.vector,
                    model = excluded.model,
                    dim = excluded.dim
                """,
                (memory_id, user_id, arr.tobytes(), model, int(arr.size)),
            )
            conn.commit()

    def get_user_embeddings(self, user_id: str) -> List[Tuple[MemoryItem, np.ndarray]]:
        now = time.time()
        rows = self._conn().execute(
            """
            SELECT m.*, e.vector AS emb_vec, e.dim AS emb_dim
            FROM memory_embeddings e
            JOIN memories m ON m.id = e.memory_id AND m.user_id = e.user_id
            WHERE e.user_id = ?
              AND m.status = 'active'
              AND (m.expires_at IS NULL OR m.expires_at > ?)
            """,
            (user_id, now),
        ).fetchall()
        out: List[Tuple[MemoryItem, np.ndarray]] = []
        for r in rows:
            arr = np.frombuffer(r["emb_vec"], dtype=np.float32)
            if arr.size == int(r["emb_dim"]):
                out.append((self._row_to_memory(r), arr))
        return out

    def list_memories_missing_embeddings(self, user_id: str, limit: int = 25) -> List[MemoryItem]:
        now = time.time()
        rows = self._conn().execute(
            """
            SELECT m.*
            FROM memories m
            LEFT JOIN memory_embeddings e ON e.memory_id = m.id
            WHERE m.user_id = ?
              AND m.status = 'active'
              AND m.type != 'working'
              AND (m.expires_at IS NULL OR m.expires_at > ?)
              AND e.memory_id IS NULL
            ORDER BY m.importance DESC, m.created_at DESC
            LIMIT ?
            """,
            (user_id, now, int(limit)),
        ).fetchall()
        return [self._row_to_memory(r) for r in rows]

    # ------------------------------------------------------------------
    # ENTITIES & LINKS (WITH WHATSAPP CONTACTS REUSE)
    # ------------------------------------------------------------------

    def upsert_entity(
        self,
        user_id: str,
        name: str,
        kind: str = "other",
        aliases: Optional[List[str]] = None,
        attributes: Optional[Dict[str, Any]] = None,
        whatsapp_contact_id: Optional[str] = None,
    ) -> EntityItem:
        clean_name = (name or "").strip()
        if not clean_name:
            raise ValueError("Entity name cannot be empty")
        if kind not in VALID_ENTITY_KINDS:
            kind = "other"

        # Never persist sensitive data in entity attributes
        if attributes:
            for k, v in list(attributes.items()):
                sens, _ = contains_sensitive_data(f"{k}: {v}")
                if sens:
                    del attributes[k]

        with self._lock:
            conn = self._conn()
            row = None
            if whatsapp_contact_id:
                row = conn.execute(
                    "SELECT * FROM entities WHERE user_id = ? AND whatsapp_contact_id = ?",
                    (user_id, whatsapp_contact_id),
                ).fetchone()
            if not row:
                row = conn.execute(
                    "SELECT * FROM entities WHERE user_id = ? AND LOWER(name) = LOWER(?)",
                    (user_id, clean_name),
                ).fetchone()

            if row:
                cur_aliases = _safe_json_loads(row["aliases_json"], [])
                for al in aliases or []:
                    if al and al not in cur_aliases:
                        cur_aliases.append(al)
                cur_attrs = _safe_json_loads(row["attributes_json"], {})
                if attributes:
                    cur_attrs.update(attributes)
                wa_id = whatsapp_contact_id or row["whatsapp_contact_id"]
                new_kind = kind if kind != "other" else row["kind"]
                conn.execute(
                    """
                    UPDATE entities
                    SET name = ?, kind = ?, aliases_json = ?, attributes_json = ?, whatsapp_contact_id = ?
                    WHERE id = ? AND user_id = ?
                    """,
                    (
                        clean_name,
                        new_kind,
                        json.dumps(cur_aliases),
                        json.dumps(cur_attrs),
                        wa_id,
                        row["id"],
                        user_id,
                    ),
                )
                conn.commit()
                return EntityItem(
                    id=row["id"],
                    user_id=user_id,
                    name=clean_name,
                    kind=new_kind,
                    aliases=cur_aliases,
                    attributes=cur_attrs,
                    whatsapp_contact_id=wa_id,
                )

            eid = new_id()
            ent = EntityItem(
                id=eid,
                user_id=user_id,
                name=clean_name,
                kind=kind,
                aliases=list(aliases or []),
                attributes=dict(attributes or {}),
                whatsapp_contact_id=whatsapp_contact_id,
            )
            conn.execute(
                """
                INSERT INTO entities (id, user_id, name, kind, aliases_json, attributes_json, whatsapp_contact_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ent.id,
                    user_id,
                    ent.name,
                    ent.kind,
                    json.dumps(ent.aliases),
                    json.dumps(ent.attributes),
                    ent.whatsapp_contact_id,
                ),
            )
            conn.commit()
            return ent

    def sync_whatsapp_contact_entity(self, user_id: str, contact: Dict[str, Any]) -> EntityItem:
        """Link a person entity directly to whatsapp_contacts row by ID without duplicating phone data."""
        aliases = contact.get("aliases")
        if aliases is None and contact.get("aliases_json"):
            aliases = _safe_json_loads(contact["aliases_json"], [])
        return self.upsert_entity(
            user_id=user_id,
            name=contact.get("display_name") or "Contact",
            kind="person",
            aliases=aliases or [],
            attributes={"channel": "whatsapp"},
            whatsapp_contact_id=contact.get("id"),
        )

    def link_entities(self, user_id: str, from_name: str, to_name: str, relation: str) -> Optional[EntityLink]:
        if not from_name or not to_name or not relation:
            return None
        with self._lock:
            from_ent = self.upsert_entity(user_id, from_name)
            to_ent = self.upsert_entity(user_id, to_name)
            lid = new_id()
            conn = self._conn()
            conn.execute(
                """
                INSERT OR IGNORE INTO entity_links (id, user_id, from_id, to_id, relation)
                VALUES (?, ?, ?, ?, ?)
                """,
                (lid, user_id, from_ent.id, to_ent.id, relation.strip().lower()),
            )
            conn.commit()
            return EntityLink(
                id=lid,
                user_id=user_id,
                from_id=from_ent.id,
                to_id=to_ent.id,
                relation=relation.strip().lower(),
            )

    def list_entities(self, user_id: str) -> List[Dict[str, Any]]:
        """List user entities enriched dynamically from whatsapp_contacts when linked."""
        rows = self._conn().execute(
            "SELECT * FROM entities WHERE user_id = ? ORDER BY name ASC",
            (user_id,),
        ).fetchall()
        wa_by_id: Dict[str, Dict[str, Any]] = {}
        try:
            for c in self.db.get_whatsapp_contacts(user_id):
                wa_by_id[c["id"]] = c
        except Exception:
            pass

        result: List[Dict[str, Any]] = []
        for r in rows:
            item = EntityItem(
                id=r["id"],
                user_id=user_id,
                name=r["name"],
                kind=r["kind"],
                aliases=_safe_json_loads(r["aliases_json"], []),
                attributes=_safe_json_loads(r["attributes_json"], {}),
                whatsapp_contact_id=r["whatsapp_contact_id"],
            ).to_dict()
            wa_id = r["whatsapp_contact_id"]
            if wa_id and wa_id in wa_by_id:
                wa = wa_by_id[wa_id]
                item["whatsapp_contact"] = {
                    "id": wa["id"],
                    "display_name": wa["display_name"],
                    "aliases": wa.get("aliases", []),
                }
            result.append(item)
        return result

    def match_entities(self, user_id: str, query: str) -> List[Dict[str, Any]]:
        """Match entities mentioned in a query, including aliases and whatsapp_contacts."""
        q_low = (query or "").lower()
        if not q_low:
            return []
        matched: List[Dict[str, Any]] = []
        seen_ids = set()

        for ent in self.list_entities(user_id):
            names = [ent["name"]] + (ent.get("aliases") or [])
            if ent.get("whatsapp_contact"):
                names.append(ent["whatsapp_contact"].get("display_name", ""))
                names.extend(ent["whatsapp_contact"].get("aliases") or [])
            for nm in names:
                nm_clean = (nm or "").strip().lower()
                if nm_clean and re.search(rf"\b{re.escape(nm_clean)}\b", q_low):
                    if ent["id"] not in seen_ids:
                        seen_ids.add(ent["id"])
                        matched.append(ent)
                    break

        # Also check whatsapp_contacts not yet synced into entities
        try:
            for wc in self.db.get_whatsapp_contacts(user_id):
                names = [wc["display_name"]] + (wc.get("aliases") or [])
                for nm in names:
                    nm_clean = (nm or "").strip().lower()
                    if nm_clean and re.search(rf"\b{re.escape(nm_clean)}\b", q_low):
                        ent = self.sync_whatsapp_contact_entity(user_id, wc).to_dict()
                        if ent["id"] not in seen_ids:
                            seen_ids.add(ent["id"])
                            ent["whatsapp_contact"] = {
                                "id": wc["id"],
                                "display_name": wc["display_name"],
                                "aliases": wc.get("aliases", []),
                            }
                            matched.append(ent)
                        break
        except Exception:
            pass
        return matched

    # ------------------------------------------------------------------
    # REMINDERS
    # ------------------------------------------------------------------

    def _row_to_reminder(self, row: sqlite3.Row) -> ReminderItem:
        return ReminderItem(
            id=row["id"],
            user_id=row["user_id"],
            text=row["text"],
            due_at=float(row["due_at"]),
            recurrence=row["recurrence"],
            channel=row["channel"],
            status=row["status"],
            created_from_memory_id=row["created_from_memory_id"],
            urgent=bool(row["urgent"]),
            updated_at=float(row["updated_at"]),
        )

    def create_reminder(self, item: ReminderItem) -> ReminderItem:
        if item.channel not in VALID_CHANNELS:
            item.channel = "hud"
        if item.status not in VALID_REMINDER_STATUSES:
            item.status = ReminderStatus.PENDING.value
        sensitive, category = contains_sensitive_data(item.text)
        if sensitive:
            raise ValueError(f"sensitive_{category}")
        with self._lock:
            conn = self._conn()
            conn.execute(
                """
                INSERT INTO reminders (
                    id, user_id, text, due_at, recurrence, channel, status,
                    created_from_memory_id, urgent, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.id,
                    item.user_id,
                    item.text.strip(),
                    float(item.due_at),
                    item.recurrence,
                    item.channel,
                    item.status,
                    item.created_from_memory_id,
                    1 if item.urgent else 0,
                    time.time(),
                ),
            )
            conn.commit()
        self.record_audit(item.user_id, "reminder_create", item.created_from_memory_id)
        return item

    def list_reminders(
        self,
        user_id: str,
        status: Optional[str] = None,
        limit: int = 100,
    ) -> List[ReminderItem]:
        sql = "SELECT * FROM reminders WHERE user_id = ?"
        params: List[Any] = [user_id]
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY due_at ASC LIMIT ?"
        params.append(int(limit))
        rows = self._conn().execute(sql, params).fetchall()
        return [self._row_to_reminder(r) for r in rows]

    def get_due_reminders(self, now: Optional[float] = None) -> List[ReminderItem]:
        ts = now if now is not None else time.time()
        rows = self._conn().execute(
            """
            SELECT * FROM reminders
            WHERE status IN ('pending', 'snoozed') AND due_at <= ?
            ORDER BY due_at ASC
            """,
            (ts,),
        ).fetchall()
        return [self._row_to_reminder(r) for r in rows]

    def update_reminder(
        self,
        user_id: str,
        reminder_id: str,
        status: Optional[str] = None,
        due_at: Optional[float] = None,
        text: Optional[str] = None,
    ) -> Optional[ReminderItem]:
        with self._lock:
            conn = self._conn()
            row = conn.execute(
                "SELECT * FROM reminders WHERE id = ? AND user_id = ?",
                (reminder_id, user_id),
            ).fetchone()
            if not row:
                return None
            new_status = status if status in VALID_REMINDER_STATUSES else row["status"]
            new_due = float(due_at) if due_at is not None else float(row["due_at"])
            new_text = text.strip() if text is not None else row["text"]
            now = time.time()
            conn.execute(
                """
                UPDATE reminders
                SET status = ?, due_at = ?, text = ?, updated_at = ?
                WHERE id = ? AND user_id = ?
                """,
                (new_status, new_due, new_text, now, reminder_id, user_id),
            )
            conn.commit()
            updated = conn.execute(
                "SELECT * FROM reminders WHERE id = ? AND user_id = ?",
                (reminder_id, user_id),
            ).fetchone()
            return self._row_to_reminder(updated) if updated else None

    def get_distinct_user_ids(self) -> List[str]:
        rows = self._conn().execute("SELECT id FROM users").fetchall()
        return [r["id"] for r in rows]

