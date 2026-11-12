import os
import json
import re
import shutil
import sqlite3
import threading
import time
import uuid
from typing import Optional, List, Dict, Any

from core.shared.config_loader import ensure_dir, get_path
from core.shared.logger import get_logger
from services.whatsapp_config import get_wa_config, mask_phone, normalize_phone

logger = get_logger("DatabaseService")

class DatabaseService:
    """
    Thread-safe SQLite database manager for AIVA with WAL mode.
    Handles users, devices, OTP verification, tokens, per-user memory,
    conversation history, task patterns, user settings, and command audit logs.
    """
    _instance = None
    _lock = threading.Lock()

    def __new__(cls, db_path: Optional[str] = None):
        with cls._lock:
            if db_path is not None:
                inst = super(DatabaseService, cls).__new__(cls)
                inst._init_db(db_path)
                return inst
            if cls._instance is None:
                cls._instance = super(DatabaseService, cls).__new__(cls)
                cls._instance._init_db(None)
            return cls._instance

    def _init_db(self, db_path: Optional[str] = None):
        if db_path is None:
            ensure_dir("storage")
            self.db_path = str(get_path("storage", "aiva.db"))
        else:
            self.db_path = db_path

        self._local = threading.local()
        self._mem_lock = threading.RLock()
        self._mem_conn = None
        self._backup_before_migration_if_needed()
        self._create_schema()
        self._migrate_schema()
        self._migrate_existing_data()
        try:
            from core.memory.store import MemoryStore
            mem_store = MemoryStore(self)
            mem_store.migrate_legacy_data()
            mem_store.drop_legacy_tables()
            mem_store.remediate_identity_memories()
        except Exception as e:
            logger.warning(f"MemoryStore schema/migration warning: {e}")
        logger.info(f"Database initialized at: {self.db_path}")

    def get_connection(self) -> sqlite3.Connection:
        if self.db_path == ":memory:":
            with self._mem_lock:
                if self._mem_conn is None:
                    conn = sqlite3.connect(
                        ":memory:",
                        check_same_thread=False,
                        timeout=20.0
                    )
                    conn.row_factory = sqlite3.Row
                    conn.execute("PRAGMA foreign_keys = ON;")
                    self._mem_conn = conn
                return self._mem_conn
        if not hasattr(self._local, "conn") or self._local.conn is None:
            conn = sqlite3.connect(
                self.db_path,
                check_same_thread=False,
                timeout=20.0
            )
            conn.row_factory = sqlite3.Row
            # Enable WAL mode for high concurrency read/write
            conn.execute("PRAGMA journal_mode = WAL;")
            conn.execute("PRAGMA foreign_keys = ON;")
            conn.execute("PRAGMA busy_timeout = 20000;")
            self._local.conn = conn
        return self._local.conn

    def _backup_before_migration_if_needed(self) -> None:
        """Create an automatic DB backup prior to running new schema migrations on existing data."""
        if not self.db_path or self.db_path == ":memory:" or not os.path.exists(self.db_path):
            return
        try:
            if os.path.getsize(self.db_path) <= 0:
                return
            conn = sqlite3.connect(self.db_path, timeout=10.0)
            try:
                has_users = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='users'"
                ).fetchone()
                if not has_users:
                    return
                user_cols = [r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
                if "gate_state" in user_cols:
                    return
                user_cnt = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
                if user_cnt <= 0:
                    return
            finally:
                conn.close()

            backup_path = f"{self.db_path}.pre_wa_rbac_{int(time.time())}.bak"
            if not os.path.exists(backup_path):
                shutil.copy2(self.db_path, backup_path)
                logger.info(f"[DB] Created pre-migration backup: {os.path.basename(backup_path)}")
        except Exception as err:
            logger.warning(f"[DB] Pre-migration backup skipped: {err}")

    @staticmethod
    def _ensure_column(cursor: sqlite3.Cursor, table: str, column: str, col_def: str) -> None:
        cols = [row["name"] for row in cursor.execute(f"PRAGMA table_info({table})").fetchall()]
        if column not in cols:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_def}")

    def _create_schema(self):
        conn = self.get_connection()
        cursor = conn.cursor()

        cursor.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            email TEXT UNIQUE NOT NULL,
            name TEXT DEFAULT '',
            onboarded INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1,
            gate_state TEXT NOT NULL DEFAULT 'active',
            auth_source TEXT NOT NULL DEFAULT 'email_otp',
            token_version INTEGER NOT NULL DEFAULT 1,
            access_epoch INTEGER NOT NULL DEFAULT 0,
            last_active_at REAL,
            approved_by TEXT DEFAULT '',
            approved_at REAL,
            blocked_reason TEXT DEFAULT '',
            daily_msg_count INTEGER NOT NULL DEFAULT 0,
            daily_msg_date TEXT DEFAULT ''
        );

        CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);

        CREATE TABLE IF NOT EXISTS schema_migrations (
            name TEXT PRIMARY KEY,
            applied_at REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS otps (
            id TEXT PRIMARY KEY,
            email TEXT NOT NULL,
            name TEXT DEFAULT '',
            code_hash TEXT NOT NULL,
            salt TEXT NOT NULL,
            expires_at REAL NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            used INTEGER NOT NULL DEFAULT 0,
            ip_address TEXT,
            created_at REAL NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_otps_email ON otps(email);

        CREATE TABLE IF NOT EXISTS refresh_tokens (
            token_hash TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            expires_at REAL NOT NULL,
            created_at REAL NOT NULL,
            revoked INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_refresh_tokens_user ON refresh_tokens(user_id);

        CREATE TABLE IF NOT EXISTS devices (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            name TEXT NOT NULL,
            platform TEXT NOT NULL,
            app_version TEXT,
            device_token_hash TEXT UNIQUE NOT NULL,
            last_seen REAL,
            status TEXT NOT NULL DEFAULT 'offline',
            created_at REAL NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_devices_user ON devices(user_id);
        CREATE INDEX IF NOT EXISTS idx_devices_token ON devices(device_token_hash);

        CREATE TABLE IF NOT EXISTS command_audit_logs (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            device_id TEXT,
            action TEXT NOT NULL,
            params_json TEXT,
            status TEXT NOT NULL,
            result_json TEXT,
            created_at REAL NOT NULL,
            completed_at REAL
        );

        CREATE INDEX IF NOT EXISTS idx_audit_user ON command_audit_logs(user_id);
        CREATE INDEX IF NOT EXISTS idx_audit_created ON command_audit_logs(created_at);

        CREATE TABLE IF NOT EXISTS user_task_patterns (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            pattern_name TEXT NOT NULL,
            pattern_json TEXT NOT NULL,
            created_at REAL NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
            UNIQUE(user_id, pattern_name)
        );

        CREATE INDEX IF NOT EXISTS idx_patterns_user ON user_task_patterns(user_id);

        CREATE TABLE IF NOT EXISTS user_settings (
            user_id TEXT PRIMARY KEY,
            settings_json TEXT NOT NULL,
            updated_at REAL NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS whatsapp_links (
            phone_e164 TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            verified_at REAL,
            link_code TEXT,
            link_code_expires_at REAL,
            created_at REAL NOT NULL,
            session_state TEXT NOT NULL DEFAULT 'INACTIVE',
            last_activity_at REAL DEFAULT 0,
            verified_by_epoch INTEGER NOT NULL DEFAULT 0,
            reply_window_until REAL DEFAULT 0,
            reply_window_scope TEXT DEFAULT '',
            last_read_sender TEXT DEFAULT '',
            lid_jid TEXT DEFAULT '',
            wa_jid TEXT DEFAULT '',
            idle_notified INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_whatsapp_links_user ON whatsapp_links(user_id);

        CREATE TABLE IF NOT EXISTS whatsapp_contacts (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            display_name TEXT NOT NULL,
            aliases_json TEXT NOT NULL DEFAULT '[]',
            phone_e164 TEXT NOT NULL,
            is_self INTEGER NOT NULL DEFAULT 0,
            trusted INTEGER NOT NULL DEFAULT 1,
            notify_inbound INTEGER NOT NULL DEFAULT 1,
            priority INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_whatsapp_contacts_user ON whatsapp_contacts(user_id);
        CREATE INDEX IF NOT EXISTS idx_whatsapp_contacts_phone ON whatsapp_contacts(user_id, phone_e164);

        CREATE TABLE IF NOT EXISTS whatsapp_events (
            id TEXT PRIMARY KEY,
            user_id TEXT,
            direction TEXT NOT NULL,
            stage TEXT NOT NULL,
            status TEXT NOT NULL,
            duration_ms REAL DEFAULT 0.0,
            created_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_whatsapp_events_user ON whatsapp_events(user_id);
        CREATE INDEX IF NOT EXISTS idx_whatsapp_events_created ON whatsapp_events(created_at);

        CREATE TABLE IF NOT EXISTS agent_telemetry (
            id TEXT PRIMARY KEY,
            user_id TEXT,
            tier INTEGER NOT NULL,
            channel TEXT NOT NULL,
            tag TEXT NOT NULL,
            route_ms REAL DEFAULT 0.0,
            llm_first_token_ms REAL DEFAULT 0.0,
            tool_time_ms REAL DEFAULT 0.0,
            total_ms REAL DEFAULT 0.0,
            tts_first_audio_ms REAL DEFAULT 0.0,
            tools_json TEXT NOT NULL DEFAULT '[]',
            success INTEGER NOT NULL DEFAULT 1,
            created_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_agent_telemetry_user ON agent_telemetry(user_id);
        CREATE INDEX IF NOT EXISTS idx_agent_telemetry_created ON agent_telemetry(created_at);
        """)
        self._ensure_whatsapp_messages_schema(cursor)
        self._ensure_whatsapp_rbac_schema(cursor)
        conn.commit()

    def _migrate_schema(self):
        """Ensures any existing database tables get new columns and tables gracefully."""
        conn = self.get_connection()
        cursor = conn.cursor()

        # Check existing columns on users table
        self._ensure_column(cursor, "users", "name", "TEXT DEFAULT ''")
        self._ensure_column(cursor, "users", "onboarded", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column(cursor, "users", "gate_state", "TEXT NOT NULL DEFAULT 'active'")
        self._ensure_column(cursor, "users", "auth_source", "TEXT NOT NULL DEFAULT 'email_otp'")
        self._ensure_column(cursor, "users", "token_version", "INTEGER NOT NULL DEFAULT 1")
        self._ensure_column(cursor, "users", "access_epoch", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column(cursor, "users", "last_active_at", "REAL")
        self._ensure_column(cursor, "users", "approved_by", "TEXT DEFAULT ''")
        self._ensure_column(cursor, "users", "approved_at", "REAL")
        self._ensure_column(cursor, "users", "blocked_reason", "TEXT DEFAULT ''")
        self._ensure_column(cursor, "users", "daily_msg_count", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column(cursor, "users", "daily_msg_date", "TEXT DEFAULT ''")

        # Check existing columns on otps table
        self._ensure_column(cursor, "otps", "name", "TEXT DEFAULT ''")

        # Ensure WhatsApp tables exist
        cursor.executescript("""
        CREATE TABLE IF NOT EXISTS whatsapp_links (
            phone_e164 TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            verified_at REAL,
            link_code TEXT,
            link_code_expires_at REAL,
            created_at REAL NOT NULL,
            session_state TEXT NOT NULL DEFAULT 'INACTIVE',
            last_activity_at REAL DEFAULT 0,
            verified_by_epoch INTEGER NOT NULL DEFAULT 0,
            reply_window_until REAL DEFAULT 0,
            reply_window_scope TEXT DEFAULT '',
            last_read_sender TEXT DEFAULT '',
            lid_jid TEXT DEFAULT '',
            wa_jid TEXT DEFAULT '',
            idle_notified INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_whatsapp_links_user ON whatsapp_links(user_id);

        CREATE TABLE IF NOT EXISTS whatsapp_contacts (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            display_name TEXT NOT NULL,
            aliases_json TEXT NOT NULL DEFAULT '[]',
            phone_e164 TEXT NOT NULL,
            is_self INTEGER NOT NULL DEFAULT 0,
            trusted INTEGER NOT NULL DEFAULT 1,
            notify_inbound INTEGER NOT NULL DEFAULT 1,
            priority INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_whatsapp_contacts_user ON whatsapp_contacts(user_id);
        CREATE INDEX IF NOT EXISTS idx_whatsapp_contacts_phone ON whatsapp_contacts(user_id, phone_e164);

        CREATE TABLE IF NOT EXISTS whatsapp_events (
            id TEXT PRIMARY KEY,
            user_id TEXT,
            direction TEXT NOT NULL,
            stage TEXT NOT NULL,
            status TEXT NOT NULL,
            duration_ms REAL DEFAULT 0.0,
            created_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_whatsapp_events_user ON whatsapp_events(user_id);
        CREATE INDEX IF NOT EXISTS idx_whatsapp_events_created ON whatsapp_events(created_at);

        CREATE TABLE IF NOT EXISTS agent_telemetry (
            id TEXT PRIMARY KEY,
            user_id TEXT,
            tier INTEGER NOT NULL,
            channel TEXT NOT NULL,
            tag TEXT NOT NULL,
            route_ms REAL DEFAULT 0.0,
            llm_first_token_ms REAL DEFAULT 0.0,
            tool_time_ms REAL DEFAULT 0.0,
            total_ms REAL DEFAULT 0.0,
            tts_first_audio_ms REAL DEFAULT 0.0,
            tools_json TEXT NOT NULL DEFAULT '[]',
            success INTEGER NOT NULL DEFAULT 1,
            created_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_agent_telemetry_user ON agent_telemetry(user_id);
        CREATE INDEX IF NOT EXISTS idx_agent_telemetry_created ON agent_telemetry(created_at);
        """)
        self._ensure_whatsapp_messages_schema(cursor)
        self._ensure_whatsapp_rbac_schema(cursor)

        # Idempotent new columns on existing WhatsApp tables
        self._ensure_column(cursor, "whatsapp_links", "session_state", "TEXT NOT NULL DEFAULT 'INACTIVE'")
        self._ensure_column(cursor, "whatsapp_links", "last_activity_at", "REAL DEFAULT 0")
        self._ensure_column(cursor, "whatsapp_links", "verified_by_epoch", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column(cursor, "whatsapp_links", "reply_window_until", "REAL DEFAULT 0")
        self._ensure_column(cursor, "whatsapp_links", "reply_window_scope", "TEXT DEFAULT ''")
        self._ensure_column(cursor, "whatsapp_links", "last_read_sender", "TEXT DEFAULT ''")
        self._ensure_column(cursor, "whatsapp_links", "lid_jid", "TEXT DEFAULT ''")
        self._ensure_column(cursor, "whatsapp_links", "wa_jid", "TEXT DEFAULT ''")
        self._ensure_column(cursor, "whatsapp_links", "idle_notified", "INTEGER NOT NULL DEFAULT 0")

        self._ensure_column(cursor, "whatsapp_contacts", "notify_inbound", "INTEGER NOT NULL DEFAULT 1")
        self._ensure_column(cursor, "whatsapp_contacts", "priority", "INTEGER NOT NULL DEFAULT 0")

        # One-time idempotent backfill migration for pre-RBAC databases
        mig_row = cursor.execute(
            "SELECT 1 FROM schema_migrations WHERE name = 'wa_rbac_v1'"
        ).fetchone()
        if not mig_row:
            try:
                cfg = get_wa_config()
                if cfg.aiva_whatsapp_number:
                    cursor.execute(
                        "DELETE FROM whatsapp_links WHERE phone_e164 = ?",
                        (cfg.aiva_whatsapp_number,),
                    )
                existing_users = cursor.execute("SELECT id FROM users").fetchall()
                if existing_users:
                    for u_row in existing_users:
                        uid = u_row["id"]
                        ver = cursor.execute(
                            "SELECT 1 FROM whatsapp_links WHERE user_id = ? AND status = 'verified' LIMIT 1",
                            (uid,),
                        ).fetchone()
                        if not ver:
                            cursor.execute(
                                "UPDATE users SET gate_state = 'number_pending' WHERE id = ?",
                                (uid,),
                            )
                cursor.execute(
                    "INSERT INTO schema_migrations (name, applied_at) VALUES ('wa_rbac_v1', ?)",
                    (time.time(),),
                )
            except Exception as err:
                logger.warning(f"[DB] wa_rbac_v1 backfill warning: {err}")

        conn.commit()

    def _ensure_whatsapp_rbac_schema(self, cursor: sqlite3.Cursor) -> None:
        """Create tables for multi-user WhatsApp access challenges, inbox, notifications, blocking, and audit logs."""
        cursor.executescript("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            name TEXT PRIMARY KEY,
            applied_at REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS whatsapp_access_challenges (
            id TEXT PRIMARY KEY,
            phone_e164 TEXT DEFAULT '',
            lid_jid TEXT DEFAULT '',
            user_id TEXT DEFAULT '',
            entry_mode TEXT NOT NULL DEFAULT 'A',
            verify_code TEXT DEFAULT '',
            code_expires_at REAL DEFAULT 0,
            state TEXT NOT NULL DEFAULT 'IDLE',
            challenge_nonce TEXT DEFAULT '',
            username_hmac TEXT DEFAULT '',
            attempts INTEGER NOT NULL DEFAULT 0,
            lockout_count INTEGER NOT NULL DEFAULT 0,
            locked_until REAL DEFAULT 0,
            last_started_at REAL DEFAULT 0,
            expires_at REAL DEFAULT 0,
            updated_at REAL NOT NULL,
            created_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_wa_chal_phone ON whatsapp_access_challenges(phone_e164);
        CREATE INDEX IF NOT EXISTS idx_wa_chal_user ON whatsapp_access_challenges(user_id);
        CREATE INDEX IF NOT EXISTS idx_wa_chal_code ON whatsapp_access_challenges(verify_code);

        CREATE TABLE IF NOT EXISTS whatsapp_inbox (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            contact_id TEXT DEFAULT '',
            sender_e164 TEXT NOT NULL,
            sender_name TEXT NOT NULL DEFAULT '',
            message_id TEXT NOT NULL DEFAULT '',
            received_at REAL NOT NULL,
            kind TEXT NOT NULL DEFAULT 'text',
            body TEXT NOT NULL DEFAULT '',
            media_ref TEXT NOT NULL DEFAULT '',
            filename TEXT NOT NULL DEFAULT '',
            mime_type TEXT NOT NULL DEFAULT '',
            read_at REAL,
            status TEXT NOT NULL DEFAULT 'unread',
            notified_at REAL,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_wa_inbox_user_msgid
            ON whatsapp_inbox(user_id, message_id)
            WHERE message_id IS NOT NULL AND message_id != '';
        CREATE INDEX IF NOT EXISTS idx_wa_inbox_user_status_ts
            ON whatsapp_inbox(user_id, status, received_at DESC);
        CREATE INDEX IF NOT EXISTS idx_wa_inbox_user_sender_ts
            ON whatsapp_inbox(user_id, sender_e164, received_at DESC);

        CREATE TABLE IF NOT EXISTS whatsapp_pending_notifications (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            contact_id TEXT NOT NULL,
            sender_name TEXT NOT NULL DEFAULT '',
            count INTEGER NOT NULL DEFAULT 1,
            latest_kind TEXT NOT NULL DEFAULT 'text',
            first_received_at REAL NOT NULL,
            latest_received_at REAL NOT NULL,
            ready_at REAL NOT NULL,
            is_priority INTEGER NOT NULL DEFAULT 0,
            held_for_quiet INTEGER NOT NULL DEFAULT 0,
            delivered INTEGER NOT NULL DEFAULT 0,
            delivered_at REAL
        );
        CREATE INDEX IF NOT EXISTS idx_wa_pending_notif_due
            ON whatsapp_pending_notifications(delivered, ready_at);

        CREATE TABLE IF NOT EXISTS whatsapp_unknown_counters (
            id TEXT PRIMARY KEY,
            phone_last4 TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'text',
            received_at REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS whatsapp_blocked_numbers (
            phone_e164 TEXT PRIMARY KEY,
            blocked_by TEXT DEFAULT '',
            reason TEXT DEFAULT '',
            created_at REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS whatsapp_admin_notice_queue (
            id TEXT PRIMARY KEY,
            event_type TEXT NOT NULL,
            summary TEXT NOT NULL,
            target_user_id TEXT DEFAULT '',
            created_at REAL NOT NULL,
            flushed INTEGER NOT NULL DEFAULT 0,
            flushed_at REAL
        );

        CREATE TABLE IF NOT EXISTS admin_audit_logs (
            id TEXT PRIMARY KEY,
            actor_user_id TEXT NOT NULL,
            action TEXT NOT NULL,
            target_user_id TEXT DEFAULT '',
            created_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_admin_audit_created ON admin_audit_logs(created_at DESC);

        CREATE TABLE IF NOT EXISTS whatsapp_verify_sends (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            sent_date TEXT NOT NULL,
            created_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_wa_verify_sends_user_date ON whatsapp_verify_sends(user_id, sent_date);
        """)

    def _ensure_whatsapp_messages_schema(self, cursor: sqlite3.Cursor) -> None:
        """Create per-user WhatsApp message store and FTS5 search index idempotently."""
        cursor.executescript("""
        CREATE TABLE IF NOT EXISTS whatsapp_messages (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            wa_message_id TEXT,
            chat_jid TEXT NOT NULL,
            peer_phone TEXT NOT NULL,
            peer_name TEXT NOT NULL DEFAULT '',
            sender_phone TEXT NOT NULL DEFAULT '',
            direction TEXT NOT NULL DEFAULT 'inbound',
            text TEXT NOT NULL,
            media_type TEXT NOT NULL DEFAULT 'text',
            is_read INTEGER NOT NULL DEFAULT 0,
            msg_timestamp REAL NOT NULL,
            created_at REAL NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_wa_msg_user_waid
            ON whatsapp_messages(user_id, wa_message_id)
            WHERE wa_message_id IS NOT NULL AND wa_message_id != '';
        CREATE INDEX IF NOT EXISTS idx_wa_msg_user_ts
            ON whatsapp_messages(user_id, msg_timestamp DESC);
        CREATE INDEX IF NOT EXISTS idx_wa_msg_user_peer_ts
            ON whatsapp_messages(user_id, peer_phone, msg_timestamp DESC);
        """)
        try:
            cursor.executescript("""
            CREATE VIRTUAL TABLE IF NOT EXISTS whatsapp_messages_fts USING fts5(
                text,
                peer_name,
                content='whatsapp_messages',
                content_rowid='rowid'
            );
            CREATE TRIGGER IF NOT EXISTS wa_msg_ai AFTER INSERT ON whatsapp_messages BEGIN
                INSERT INTO whatsapp_messages_fts(rowid, text, peer_name)
                VALUES (new.rowid, new.text, new.peer_name);
            END;
            CREATE TRIGGER IF NOT EXISTS wa_msg_ad AFTER DELETE ON whatsapp_messages BEGIN
                INSERT INTO whatsapp_messages_fts(whatsapp_messages_fts, rowid, text, peer_name)
                VALUES ('delete', old.rowid, old.text, old.peer_name);
            END;
            CREATE TRIGGER IF NOT EXISTS wa_msg_au AFTER UPDATE ON whatsapp_messages BEGIN
                INSERT INTO whatsapp_messages_fts(whatsapp_messages_fts, rowid, text, peer_name)
                VALUES ('delete', old.rowid, old.text, old.peer_name);
                INSERT INTO whatsapp_messages_fts(rowid, text, peer_name)
                VALUES (new.rowid, new.text, new.peer_name);
            END;
            """)
        except sqlite3.OperationalError:
            pass

    def _migrate_existing_data(self):
        """Assigns pre-existing pattern files to the configured admin or first active user."""
        conn = self.get_connection()
        cursor = conn.cursor()

        cfg = get_wa_config()
        primary = None
        if cfg.admin_email:
            primary = cursor.execute(
                "SELECT * FROM users WHERE lower(email) = lower(?) AND is_active = 1 LIMIT 1",
                (cfg.admin_email,),
            ).fetchone()
        if not primary:
            primary = cursor.execute(
                "SELECT * FROM users WHERE is_active = 1 ORDER BY created_at ASC LIMIT 1"
            ).fetchone()

        if not primary:
            return

        primary_id = primary["id"]
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

        # Migrate memory/task_patterns.json to user_task_patterns for the primary/admin account once
        task_pat_path = os.path.join(base_dir, "memory", "task_patterns.json")
        if os.path.exists(task_pat_path):
            try:
                with open(task_pat_path, "r", encoding="utf-8") as f:
                    patterns = json.load(f)
                now = time.time()
                for pat_name, pat_data in patterns.items():
                    pat_id = f"pat_{uuid.uuid4().hex[:12]}"
                    cursor.execute(
                        """INSERT OR IGNORE INTO user_task_patterns (id, user_id, pattern_name, pattern_json, created_at)
                           VALUES (?, ?, ?, ?, ?)""",
                        (pat_id, primary_id, pat_name, json.dumps(pat_data), now)
                    )
                conn.commit()
            except Exception as e:
                logger.warning(f"Failed migrating task_patterns.json: {e}")

    # -------------------------------------------------------------
    # USERS
    # -------------------------------------------------------------
    def get_user_by_email(self, email: str) -> Optional[Dict[str, Any]]:
        conn = self.get_connection()
        row = conn.execute(
            "SELECT * FROM users WHERE lower(email) = lower(?)",
            (email.strip(),)
        ).fetchone()
        return dict(row) if row else None

    def get_user_by_id(self, user_id: str) -> Optional[Dict[str, Any]]:
        conn = self.get_connection()
        row = conn.execute(
            "SELECT * FROM users WHERE id = ?",
            (user_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_primary_user(self) -> Optional[Dict[str, Any]]:
        """Returns the configured AIVA_ADMIN_EMAIL account if present, else first active user."""
        conn = self.get_connection()
        cfg = get_wa_config()
        if cfg.admin_email:
            row = conn.execute(
                "SELECT * FROM users WHERE lower(email) = lower(?) AND is_active = 1 LIMIT 1",
                (cfg.admin_email,),
            ).fetchone()
            if row:
                return dict(row)
        row = conn.execute(
            "SELECT * FROM users WHERE is_active = 1 ORDER BY created_at ASC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None

    def create_user(
        self,
        user_id: str,
        email: str,
        name: str = "",
        onboarded: int = 0,
        gate_state: str = "active",
        auth_source: str = "email_otp",
        access_epoch: int = 0,
    ) -> Dict[str, Any]:
        conn = self.get_connection()
        now = time.time()
        conn.execute(
            """INSERT INTO users (id, email, name, onboarded, created_at, is_active, gate_state, auth_source, token_version, access_epoch, last_active_at)
               VALUES (?, lower(?), ?, ?, ?, 1, ?, ?, 1, ?, ?)""",
            (user_id, email.strip(), name.strip(), onboarded, now, gate_state, auth_source, int(access_epoch), now)
        )
        conn.commit()

        cfg = get_wa_config()
        if (cfg.admin_email and email.strip().lower() == cfg.admin_email) or conn.execute("SELECT count(*) as c FROM users").fetchone()["c"] == 1:
            try:
                self._migrate_existing_data()
            except Exception as e:
                logger.warning(f"Failed to auto-migrate data: {e}")

        return {
            "id": user_id,
            "email": email.strip().lower(),
            "name": name.strip(),
            "onboarded": onboarded,
            "created_at": now,
            "is_active": 1,
            "gate_state": gate_state,
            "auth_source": auth_source,
            "token_version": 1,
            "access_epoch": int(access_epoch),
        }

    def update_user(self, user_id: str, name: Optional[str] = None, onboarded: Optional[int] = None) -> bool:
        conn = self.get_connection()
        updates = []
        params = []
        if name is not None:
            updates.append("name = ?")
            params.append(name.strip())
        if onboarded is not None:
            updates.append("onboarded = ?")
            params.append(onboarded)
        if not updates:
            return False
        params.append(user_id)
        cursor = conn.execute(
            f"UPDATE users SET {', '.join(updates)} WHERE id = ?",
            tuple(params)
        )
        conn.commit()
        return cursor.rowcount > 0

    # -------------------------------------------------------------
    # OTPS
    # -------------------------------------------------------------
    def store_otp(
        self,
        otp_id: str,
        email: str,
        code_hash: str,
        salt: str,
        expires_at: float,
        ip_address: Optional[str] = None,
        name: str = ""
    ):
        conn = self.get_connection()
        now = time.time()
        conn.execute(
            """INSERT INTO otps (id, email, name, code_hash, salt, expires_at, attempts, used, ip_address, created_at)
               VALUES (?, lower(?), ?, ?, ?, ?, 0, 0, ?, ?)""",
            (otp_id, email.strip(), name.strip(), code_hash, salt, expires_at, ip_address, now)
        )
        conn.commit()

    def get_latest_valid_otp(self, email: str) -> Optional[Dict[str, Any]]:
        conn = self.get_connection()
        now = time.time()
        row = conn.execute(
            """SELECT * FROM otps 
               WHERE lower(email) = lower(?) AND used = 0 AND expires_at > ?
               ORDER BY created_at DESC LIMIT 1""",
            (email.strip(), now)
        ).fetchone()
        return dict(row) if row else None

    def increment_otp_attempts(self, otp_id: str) -> int:
        conn = self.get_connection()
        conn.execute(
            "UPDATE otps SET attempts = attempts + 1 WHERE id = ?",
            (otp_id,)
        )
        conn.commit()
        row = conn.execute("SELECT attempts FROM otps WHERE id = ?", (otp_id,)).fetchone()
        return row["attempts"] if row else 0

    def mark_otp_used(self, otp_id: str):
        conn = self.get_connection()
        conn.execute("UPDATE otps SET used = 1 WHERE id = ?", (otp_id,))
        conn.commit()

    def count_recent_otps_by_email(self, email: str, window_seconds: float = 600.0) -> int:
        conn = self.get_connection()
        cutoff = time.time() - window_seconds
        row = conn.execute(
            "SELECT count(*) as count FROM otps WHERE lower(email) = lower(?) AND created_at > ?",
            (email.strip(), cutoff)
        ).fetchone()
        return row["count"] if row else 0

    def count_recent_otps_by_ip(self, ip_address: str, window_seconds: float = 600.0) -> int:
        if not ip_address:
            return 0
        conn = self.get_connection()
        cutoff = time.time() - window_seconds
        row = conn.execute(
            "SELECT count(*) as count FROM otps WHERE ip_address = ? AND created_at > ?",
            (ip_address, cutoff)
        ).fetchone()
        return row["count"] if row else 0

    # -------------------------------------------------------------
    # REFRESH TOKENS
    # -------------------------------------------------------------
    def store_refresh_token(self, token_hash: str, user_id: str, expires_at: float):
        conn = self.get_connection()
        now = time.time()
        conn.execute(
            "INSERT OR REPLACE INTO refresh_tokens (token_hash, user_id, expires_at, created_at, revoked) VALUES (?, ?, ?, ?, 0)",
            (token_hash, user_id, expires_at, now)
        )
        conn.commit()

    def get_refresh_token(self, token_hash: str) -> Optional[Dict[str, Any]]:
        conn = self.get_connection()
        row = conn.execute(
            "SELECT * FROM refresh_tokens WHERE token_hash = ? AND revoked = 0 AND expires_at > ?",
            (token_hash, time.time())
        ).fetchone()
        return dict(row) if row else None

    def revoke_refresh_token(self, token_hash: str):
        conn = self.get_connection()
        conn.execute("UPDATE refresh_tokens SET revoked = 1 WHERE token_hash = ?", (token_hash,))
        conn.commit()

    def revoke_all_user_refresh_tokens(self, user_id: str):
        conn = self.get_connection()
        conn.execute("UPDATE refresh_tokens SET revoked = 1 WHERE user_id = ?", (user_id,))
        conn.commit()

    # -------------------------------------------------------------
    # DEVICES
    # -------------------------------------------------------------
    def create_device(
        self,
        device_id: str,
        user_id: str,
        name: str,
        platform: str,
        app_version: str,
        device_token_hash: str
    ) -> Dict[str, Any]:
        conn = self.get_connection()
        now = time.time()
        conn.execute(
            """INSERT INTO devices (id, user_id, name, platform, app_version, device_token_hash, last_seen, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'online', ?)""",
            (device_id, user_id, name, platform, app_version, device_token_hash, now, now)
        )
        conn.commit()
        return {
            "id": device_id,
            "user_id": user_id,
            "name": name,
            "platform": platform,
            "app_version": app_version,
            "last_seen": now,
            "status": "online",
            "created_at": now
        }

    def get_device_by_token_hash(self, token_hash: str) -> Optional[Dict[str, Any]]:
        conn = self.get_connection()
        row = conn.execute(
            "SELECT * FROM devices WHERE device_token_hash = ?",
            (token_hash,)
        ).fetchone()
        return dict(row) if row else None

    def get_device_by_id(self, device_id: str) -> Optional[Dict[str, Any]]:
        conn = self.get_connection()
        row = conn.execute(
            "SELECT * FROM devices WHERE id = ?",
            (device_id,)
        ).fetchone()
        return dict(row) if row else None

    def list_devices_for_user(self, user_id: str) -> List[Dict[str, Any]]:
        conn = self.get_connection()
        rows = conn.execute(
            "SELECT id, user_id, name, platform, app_version, last_seen, status, created_at FROM devices WHERE user_id = ? ORDER BY last_seen DESC, created_at DESC",
            (user_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def rename_device(self, device_id: str, user_id: str, new_name: str) -> bool:
        conn = self.get_connection()
        cursor = conn.execute(
            "UPDATE devices SET name = ? WHERE id = ? AND user_id = ?",
            (new_name.strip(), device_id, user_id)
        )
        conn.commit()
        return cursor.rowcount > 0

    def update_device_status(self, device_id: str, status: str, last_seen: Optional[float] = None):
        conn = self.get_connection()
        ts = last_seen or time.time()
        conn.execute(
            "UPDATE devices SET status = ?, last_seen = ? WHERE id = ?",
            (status, ts, device_id)
        )
        conn.commit()

    def delete_device(self, device_id: str, user_id: str) -> bool:
        conn = self.get_connection()
        cursor = conn.execute(
            "DELETE FROM devices WHERE id = ? AND user_id = ?",
            (device_id, user_id)
        )
        conn.commit()
        return cursor.rowcount > 0

    # -------------------------------------------------------------
    # AUDIT LOGS
    # -------------------------------------------------------------
    def log_command(
        self,
        log_id: str,
        user_id: str,
        device_id: Optional[str],
        action: str,
        params_json: str,
        status: str = "dispatched"
    ):
        conn = self.get_connection()
        now = time.time()
        conn.execute(
            """INSERT INTO command_audit_logs (id, user_id, device_id, action, params_json, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (log_id, user_id, device_id, action, params_json, status, now)
        )
        conn.commit()

    def update_command_log(
        self,
        log_id: str,
        status: str,
        result_json: Optional[str] = None
    ):
        conn = self.get_connection()
        now = time.time()
        conn.execute(
            "UPDATE command_audit_logs SET status = ?, result_json = ?, completed_at = ? WHERE id = ?",
            (status, result_json, now, log_id)
        )
        conn.commit()

    def get_recent_audit_logs(self, user_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        conn = self.get_connection()
        rows = conn.execute(
            "SELECT * FROM command_audit_logs WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
            (user_id, limit)
        ).fetchall()
        return [dict(r) for r in rows]

    # -------------------------------------------------------------
    # USER TASK PATTERNS
    # -------------------------------------------------------------
    def get_user_task_patterns(self, user_id: str) -> Dict[str, Any]:
        """Returns learned task patterns scoped to user_id."""
        conn = self.get_connection()
        rows = conn.execute(
            "SELECT pattern_name, pattern_json FROM user_task_patterns WHERE user_id = ?",
            (user_id,)
        ).fetchall()
        result = {}
        for r in rows:
            try:
                result[r["pattern_name"]] = json.loads(r["pattern_json"])
            except Exception:
                result[r["pattern_name"]] = r["pattern_json"]
        return result

    def set_user_task_pattern(self, user_id: str, pattern_name: str, pattern_data: Any) -> bool:
        conn = self.get_connection()
        now = time.time()
        pat_id = f"pat_{uuid.uuid4().hex[:12]}"
        val_json = json.dumps(pattern_data)
        conn.execute(
            """INSERT INTO user_task_patterns (id, user_id, pattern_name, pattern_json, created_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(user_id, pattern_name) DO UPDATE SET pattern_json = excluded.pattern_json""",
            (pat_id, user_id, pattern_name, val_json, now)
        )
        conn.commit()
        return True

    # -------------------------------------------------------------
    # USER SETTINGS & PREFERENCES
    # -------------------------------------------------------------
    def get_user_settings(self, user_id: str) -> Dict[str, Any]:
        conn = self.get_connection()
        row = conn.execute(
            "SELECT settings_json FROM user_settings WHERE user_id = ?",
            (user_id,)
        ).fetchone()
        if not row:
            return {}
        try:
            return json.loads(row["settings_json"])
        except Exception:
            return {}

    def set_user_settings(self, user_id: str, settings: Dict[str, Any]) -> bool:
        conn = self.get_connection()
        now = time.time()
        val_json = json.dumps(settings)
        conn.execute(
            """INSERT INTO user_settings (user_id, settings_json, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET settings_json = excluded.settings_json, updated_at = excluded.updated_at""",
            (user_id, val_json, now)
        )
        conn.commit()
        return True

    # -------------------------------------------------------------
    # WHATSAPP LINKS & CONTACTS (SCOPED BY USER_ID)
    # -------------------------------------------------------------
    def create_or_update_whatsapp_link(
        self, user_id: str, phone_e164: str, link_code: str, expires_at: float
    ) -> bool:
        conn = self.get_connection()
        now = time.time()
        conn.execute(
            """INSERT INTO whatsapp_links (phone_e164, user_id, status, link_code, link_code_expires_at, created_at)
               VALUES (?, ?, 'pending', ?, ?, ?)
               ON CONFLICT(phone_e164) DO UPDATE SET
                   user_id = excluded.user_id,
                   status = 'pending',
                   link_code = excluded.link_code,
                   link_code_expires_at = excluded.link_code_expires_at""",
            (phone_e164, user_id, link_code, expires_at, now)
        )
        conn.commit()
        return True

    def get_whatsapp_link_by_code(self, code: str) -> Optional[Dict[str, Any]]:
        conn = self.get_connection()
        now = time.time()
        row = conn.execute(
            """SELECT * FROM whatsapp_links
               WHERE link_code = ? AND link_code_expires_at > ? AND status = 'pending'""",
            (code.strip().upper(), now)
        ).fetchone()
        return dict(row) if row else None

    def verify_whatsapp_link(
        self,
        phone_e164: str,
        code: str,
        *,
        verified_by_epoch: int = 0,
        session_state: str = "INACTIVE",
        lid_jid: str = "",
        wa_jid: str = "",
    ) -> bool:
        conn = self.get_connection()
        now = time.time()
        clean_phone = normalize_phone(phone_e164)
        pending = conn.execute(
            "SELECT user_id FROM whatsapp_links WHERE phone_e164 = ? AND link_code = ? AND link_code_expires_at > ?",
            (clean_phone, code.strip().upper(), now),
        ).fetchone()
        if not pending:
            return False
        uid = pending["user_id"]
        # Enforce 1:1 verified number uniqueness
        existing_other = conn.execute(
            "SELECT user_id FROM whatsapp_links WHERE phone_e164 = ? AND status = 'verified' AND user_id != ?",
            (clean_phone, uid),
        ).fetchone()
        if existing_other:
            return False
        # Replace any older link for this same user so each user has one verified personal number
        conn.execute(
            "DELETE FROM whatsapp_links WHERE user_id = ? AND phone_e164 != ?",
            (uid, clean_phone),
        )
        cursor = conn.execute(
            """UPDATE whatsapp_links
               SET status = 'verified',
                   verified_at = ?,
                   link_code = NULL,
                   link_code_expires_at = NULL,
                   verified_by_epoch = ?,
                   session_state = ?,
                   last_activity_at = ?,
                   lid_jid = COALESCE(NULLIF(?, ''), lid_jid),
                   wa_jid = COALESCE(NULLIF(?, ''), wa_jid),
                   idle_notified = 0
               WHERE phone_e164 = ? AND user_id = ?""",
            (now, int(verified_by_epoch), session_state, now, lid_jid or "", wa_jid or "", clean_phone, uid)
        )
        conn.commit()
        return cursor.rowcount > 0

    def upsert_verified_whatsapp_link(
        self,
        user_id: str,
        phone_e164: str,
        *,
        verified_by_epoch: int = 1,
        session_state: str = "ACTIVE",
        lid_jid: str = "",
        wa_jid: str = "",
    ) -> bool:
        clean_phone = normalize_phone(phone_e164)
        if not user_id or not clean_phone:
            return False
        conn = self.get_connection()
        # Reject if another user already holds this verified personal number
        other = conn.execute(
            "SELECT user_id FROM whatsapp_links WHERE phone_e164 = ? AND status = 'verified' AND user_id != ?",
            (clean_phone, user_id),
        ).fetchone()
        if other:
            return False
        now = time.time()
        # Each user holds at most one verified personal number
        conn.execute(
            "DELETE FROM whatsapp_links WHERE user_id = ? AND phone_e164 != ?",
            (user_id, clean_phone),
        )
        conn.execute(
            """INSERT INTO whatsapp_links (
                   phone_e164, user_id, status, verified_at, created_at,
                   session_state, last_activity_at, verified_by_epoch,
                   lid_jid, wa_jid, idle_notified
               ) VALUES (?, ?, 'verified', ?, ?, ?, ?, ?, ?, ?, 0)
               ON CONFLICT(phone_e164) DO UPDATE SET
                   user_id = excluded.user_id,
                   status = 'verified',
                   verified_at = excluded.verified_at,
                   link_code = NULL,
                   link_code_expires_at = NULL,
                   session_state = excluded.session_state,
                   last_activity_at = excluded.last_activity_at,
                   verified_by_epoch = excluded.verified_by_epoch,
                   lid_jid = COALESCE(NULLIF(excluded.lid_jid, ''), whatsapp_links.lid_jid),
                   wa_jid = COALESCE(NULLIF(excluded.wa_jid, ''), whatsapp_links.wa_jid),
                   idle_notified = 0""",
            (
                clean_phone,
                user_id,
                now,
                now,
                session_state,
                now,
                int(verified_by_epoch),
                lid_jid or "",
                wa_jid or "",
            ),
        )
        conn.commit()
        return True

    def get_whatsapp_link_by_phone(self, phone_e164: str) -> Optional[Dict[str, Any]]:
        conn = self.get_connection()
        clean_phone = normalize_phone(phone_e164)
        row = conn.execute(
            "SELECT * FROM whatsapp_links WHERE phone_e164 = ? AND status = 'verified'",
            (clean_phone or phone_e164,)
        ).fetchone()
        return dict(row) if row else None

    def get_whatsapp_link_by_lid(self, lid_jid: str) -> Optional[Dict[str, Any]]:
        if not lid_jid:
            return None
        conn = self.get_connection()
        row = conn.execute(
            "SELECT * FROM whatsapp_links WHERE lid_jid = ? AND status = 'verified' LIMIT 1",
            (lid_jid.strip(),),
        ).fetchone()
        return dict(row) if row else None

    def bind_whatsapp_link_lid(self, phone_e164: str, lid_jid: str = "", wa_jid: str = "") -> None:
        clean_phone = normalize_phone(phone_e164)
        if not clean_phone or (not lid_jid and not wa_jid):
            return
        conn = self.get_connection()
        conn.execute(
            """UPDATE whatsapp_links
               SET lid_jid = COALESCE(NULLIF(?, ''), lid_jid),
                   wa_jid = COALESCE(NULLIF(?, ''), wa_jid)
               WHERE phone_e164 = ? AND status = 'verified'""",
            (lid_jid or "", wa_jid or "", clean_phone),
        )
        conn.commit()

    def get_whatsapp_links_for_user(self, user_id: str) -> List[Dict[str, Any]]:
        conn = self.get_connection()
        rows = conn.execute(
            "SELECT * FROM whatsapp_links WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def unlink_whatsapp(self, user_id: str, phone_e164: str) -> bool:
        conn = self.get_connection()
        clean = normalize_phone(phone_e164) or phone_e164
        cursor = conn.execute(
            "DELETE FROM whatsapp_links WHERE user_id = ? AND phone_e164 = ?",
            (user_id, clean)
        )
        conn.commit()
        return cursor.rowcount > 0

    def add_whatsapp_contact(
        self,
        user_id: str,
        display_name: str,
        phone_e164: str,
        aliases: Optional[List[str]] = None,
        is_self: bool = False,
        trusted: bool = True,
        notify_inbound: bool = True,
        priority: bool = False,
    ) -> Dict[str, Any]:
        conn = self.get_connection()
        contact_id = f"wac_{uuid.uuid4().hex[:12]}"
        now = time.time()
        aliases_list = [a.strip() for a in (aliases or []) if a.strip()]
        aliases_json = json.dumps(aliases_list)
        clean_phone = normalize_phone(phone_e164) or phone_e164.strip()
        conn.execute(
            """INSERT INTO whatsapp_contacts
               (id, user_id, display_name, aliases_json, phone_e164, is_self, trusted, notify_inbound, priority, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                contact_id,
                user_id,
                display_name.strip(),
                aliases_json,
                clean_phone,
                1 if is_self else 0,
                1 if trusted else 0,
                1 if notify_inbound else 0,
                1 if priority else 0,
                now,
                now,
            )
        )
        conn.commit()
        return {
            "id": contact_id,
            "user_id": user_id,
            "display_name": display_name.strip(),
            "aliases": aliases_list,
            "phone_e164": clean_phone,
            "is_self": is_self,
            "trusted": trusted,
            "notify_inbound": notify_inbound,
            "priority": priority,
            "created_at": now,
            "updated_at": now
        }

    def get_whatsapp_contacts(self, user_id: str) -> List[Dict[str, Any]]:
        conn = self.get_connection()
        rows = conn.execute(
            "SELECT * FROM whatsapp_contacts WHERE user_id = ? ORDER BY display_name ASC",
            (user_id,)
        ).fetchall()
        contacts = []
        for r in rows:
            d = dict(r)
            try:
                d["aliases"] = json.loads(d.get("aliases_json") or "[]")
            except Exception:
                d["aliases"] = []
            d["is_self"] = bool(d.get("is_self"))
            d["trusted"] = bool(d.get("trusted"))
            d["notify_inbound"] = bool(d.get("notify_inbound", 1))
            d["priority"] = bool(d.get("priority", 0))
            contacts.append(d)
        return contacts

    def get_whatsapp_contact_by_id(self, user_id: str, contact_id: str) -> Optional[Dict[str, Any]]:
        conn = self.get_connection()
        row = conn.execute(
            "SELECT * FROM whatsapp_contacts WHERE user_id = ? AND id = ?",
            (user_id, contact_id)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        try:
            d["aliases"] = json.loads(d.get("aliases_json") or "[]")
        except Exception:
            d["aliases"] = []
        d["is_self"] = bool(d.get("is_self"))
        d["trusted"] = bool(d.get("trusted"))
        d["notify_inbound"] = bool(d.get("notify_inbound", 1))
        d["priority"] = bool(d.get("priority", 0))
        return d

    def update_whatsapp_contact(
        self,
        user_id: str,
        contact_id: str,
        display_name: Optional[str] = None,
        phone_e164: Optional[str] = None,
        aliases: Optional[List[str]] = None,
        trusted: Optional[bool] = None,
        is_self: Optional[bool] = None,
        notify_inbound: Optional[bool] = None,
        priority: Optional[bool] = None,
    ) -> Optional[Dict[str, Any]]:
        conn = self.get_connection()
        current = self.get_whatsapp_contact_by_id(user_id, contact_id)
        if not current:
            return None

        new_name = display_name.strip() if display_name is not None else current["display_name"]
        new_phone = (normalize_phone(phone_e164) or phone_e164.strip()) if phone_e164 is not None else current["phone_e164"]
        new_aliases = aliases if aliases is not None else current["aliases"]
        new_trusted = trusted if trusted is not None else current["trusted"]
        new_is_self = is_self if is_self is not None else current["is_self"]
        new_notify = notify_inbound if notify_inbound is not None else current.get("notify_inbound", True)
        new_priority = priority if priority is not None else current.get("priority", False)
        now = time.time()

        conn.execute(
            """UPDATE whatsapp_contacts
               SET display_name = ?, phone_e164 = ?, aliases_json = ?, trusted = ?, is_self = ?, notify_inbound = ?, priority = ?, updated_at = ?
               WHERE user_id = ? AND id = ?""",
            (
                new_name,
                new_phone,
                json.dumps([a.strip() for a in new_aliases if a.strip()]),
                1 if new_trusted else 0,
                1 if new_is_self else 0,
                1 if new_notify else 0,
                1 if new_priority else 0,
                now,
                user_id,
                contact_id
            )
        )
        conn.commit()
        return self.get_whatsapp_contact_by_id(user_id, contact_id)

    def delete_whatsapp_contact(self, user_id: str, contact_id: str) -> bool:
        conn = self.get_connection()
        cursor = conn.execute(
            "DELETE FROM whatsapp_contacts WHERE user_id = ? AND id = ?",
            (user_id, contact_id)
        )
        conn.commit()
        return cursor.rowcount > 0

    def resolve_whatsapp_contact(self, user_id: str, query: str) -> List[Dict[str, Any]]:
        """Resolves contact by display_name, alias, or phone_e164. Exact matches prioritized."""
        all_contacts = self.get_whatsapp_contacts(user_id)
        if not all_contacts:
            return []

        q = query.strip().lower()
        # Digits-only check for phone matching
        q_digits = "".join(ch for ch in q if ch.isdigit())

        exact_matches = []
        partial_matches = []

        for c in all_contacts:
            c_name = c["display_name"].lower()
            c_aliases = [a.lower() for a in c.get("aliases", [])]
            c_phone_digits = "".join(ch for ch in c["phone_e164"] if ch.isdigit())

            # 1. Exact match on display name or any alias
            if q == c_name or q in c_aliases:
                exact_matches.append(c)
                continue

            # 2. Exact match on phone digits (suffix or full)
            if q_digits and (c_phone_digits == q_digits or c_phone_digits.endswith(q_digits)):
                exact_matches.append(c)
                continue

            # 3. Partial substring match
            if q in c_name or any(q in a for a in c_aliases):
                partial_matches.append(c)

        if exact_matches:
            return exact_matches
        return partial_matches

    def log_whatsapp_event(
        self,
        user_id: Optional[str],
        direction: str,
        stage: str,
        status: str,
        duration_ms: float = 0.0
    ) -> bool:
        """Logs WhatsApp metadata event. NEVER stores message content or raw audio."""
        conn = self.get_connection()
        event_id = f"waev_{uuid.uuid4().hex[:12]}"
        now = time.time()
        conn.execute(
            """INSERT INTO whatsapp_events (id, user_id, direction, stage, status, duration_ms, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (event_id, user_id, direction, stage, status, float(duration_ms), now)
        )
        conn.commit()
        return True

    def record_agent_telemetry(
        self,
        telemetry_id: str,
        user_id: Optional[str],
        tier: int,
        channel: str,
        tag: str,
        route_ms: float = 0.0,
        llm_first_token_ms: float = 0.0,
        tool_time_ms: float = 0.0,
        total_ms: float = 0.0,
        tts_first_audio_ms: float = 0.0,
        tools: Optional[List[str]] = None,
        success: bool = True,
    ) -> bool:
        """Store non-PII agent latency/tier aggregate event."""
        conn = self.get_connection()
        now = time.time()
        conn.execute(
            """INSERT INTO agent_telemetry
               (id, user_id, tier, channel, tag, route_ms, llm_first_token_ms,
                tool_time_ms, total_ms, tts_first_audio_ms, tools_json, success, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                telemetry_id,
                user_id,
                int(tier),
                str(channel or "hud"),
                str(tag or "conversation"),
                float(route_ms or 0.0),
                float(llm_first_token_ms or 0.0),
                float(tool_time_ms or 0.0),
                float(total_ms or 0.0),
                float(tts_first_audio_ms or 0.0),
                json.dumps(list(tools or [])),
                1 if success else 0,
                now,
            ),
        )
        conn.commit()
        return True

    def get_agent_telemetry_rows(
        self,
        user_id: Optional[str] = None,
        since: float = 0.0,
        limit: int = 2000,
    ) -> List[Dict[str, Any]]:
        """Retrieve non-PII telemetry aggregates scoped by user_id when provided."""
        conn = self.get_connection()
        if user_id:
            rows = conn.execute(
                """SELECT tier, channel, tag, route_ms, llm_first_token_ms, tool_time_ms,
                          total_ms, tts_first_audio_ms, tools_json, success, created_at
                   FROM agent_telemetry
                   WHERE user_id = ? AND created_at >= ?
                   ORDER BY created_at DESC LIMIT ?""",
                (user_id, float(since), int(limit)),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT tier, channel, tag, route_ms, llm_first_token_ms, tool_time_ms,
                          total_ms, tts_first_audio_ms, tools_json, success, created_at
                   FROM agent_telemetry
                   WHERE created_at >= ?
                   ORDER BY created_at DESC LIMIT ?""",
                (float(since), int(limit)),
            ).fetchall()
        return [dict(r) for r in rows]

    # -------------------------------------------------------------
    # WHATSAPP MESSAGES STORE & FTS5 SEARCH (SCOPED BY USER_ID)
    # -------------------------------------------------------------
    @staticmethod
    def _digits_only(raw: Any) -> str:
        digits = "".join(ch for ch in str(raw or "") if ch.isdigit())
        if digits.startswith("00") and len(digits) > 10:
            digits = digits[2:]
        return digits

    def find_whatsapp_contact_by_phone(self, user_id: str, raw_phone: str) -> Optional[Dict[str, Any]]:
        """Match a saved contact of `user_id` by normalized digits or 10-digit suffix."""
        digits = self._digits_only(raw_phone)
        if not user_id or not digits:
            return None
        contacts = self.get_whatsapp_contacts(user_id)
        for c in contacts:
            c_digits = self._digits_only(c.get("phone_e164", ""))
            if not c_digits:
                continue
            if c_digits == digits:
                return c
            if len(digits) >= 10 and len(c_digits) >= 10 and c_digits[-10:] == digits[-10:]:
                return c
        return None

    def store_whatsapp_message(
        self,
        user_id: str,
        *,
        chat_jid: str = "",
        peer_phone: str = "",
        peer_name: str = "",
        sender_phone: str = "",
        direction: str = "inbound",
        text: str = "",
        media_type: str = "text",
        wa_message_id: str = "",
        is_read: Optional[bool] = None,
        msg_timestamp: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        """Persist a normalized WhatsApp message scoped by `user_id` with dedupe."""
        clean_text = str(text or "").strip()
        if not user_id or not clean_text:
            return None

        clean_peer = self._digits_only(peer_phone or (chat_jid.split("@")[0].split(":")[0] if "@" in chat_jid else ""))
        clean_sender = self._digits_only(sender_phone) or clean_peer
        clean_jid = str(chat_jid or (f"{clean_peer}@s.whatsapp.net" if clean_peer else "")).strip()
        clean_dir = "outbound" if str(direction).lower() == "outbound" else "inbound"
        clean_wa_id = str(wa_message_id or "").strip()

        # Enrich peer_name from saved user contacts when possible
        resolved_name = str(peer_name or "").strip()
        if clean_peer:
            contact = self.find_whatsapp_contact_by_phone(user_id, clean_peer)
            if contact and contact.get("display_name"):
                resolved_name = contact["display_name"]

        read_flag = (1 if clean_dir == "outbound" else 0) if is_read is None else (1 if is_read else 0)
        now = time.time()
        try:
            ts_val = float(msg_timestamp) if msg_timestamp is not None else now
            if ts_val > 1e12:  # Convert millisecond timestamps if supplied
                ts_val = ts_val / 1000.0
        except (TypeError, ValueError):
            ts_val = now

        conn = self.get_connection()
        if clean_wa_id:
            existing = conn.execute(
                "SELECT * FROM whatsapp_messages WHERE user_id = ? AND wa_message_id = ?",
                (user_id, clean_wa_id),
            ).fetchone()
            if existing:
                return dict(existing)

        msg_id = f"wam_{uuid.uuid4().hex[:14]}"
        conn.execute(
            """INSERT OR IGNORE INTO whatsapp_messages
               (id, user_id, wa_message_id, chat_jid, peer_phone, peer_name, sender_phone,
                direction, text, media_type, is_read, msg_timestamp, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                msg_id,
                user_id,
                clean_wa_id,
                clean_jid,
                clean_peer,
                resolved_name,
                clean_sender,
                clean_dir,
                clean_text[:4000],
                str(media_type or "text")[:24],
                read_flag,
                ts_val,
                now,
            ),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM whatsapp_messages WHERE id = ? AND user_id = ?", (msg_id, user_id)).fetchone()
        return dict(row) if row else None

    def _peer_phone_clause(self, peer_phone: str, col: str = "peer_phone") -> tuple[str, List[Any]]:
        """Build SQL condition + params matching normalized peer phone or 10-digit suffix."""
        digits = self._digits_only(peer_phone)
        if len(digits) >= 10:
            return f"({col} = ? OR {col} LIKE ?)", [digits, f"%{digits[-10:]}"]
        if digits:
            return f"{col} = ?", [digits]
        return "", []

    def get_whatsapp_messages(
        self,
        user_id: str,
        *,
        peer_phone: Optional[str] = None,
        peer_query: Optional[str] = None,
        unread_only: bool = False,
        since_ts: Optional[float] = None,
        limit: int = 15,
    ) -> List[Dict[str, Any]]:
        """Fetch recent WhatsApp messages strictly scoped to `user_id`."""
        if not user_id:
            return []
        cap = max(1, min(int(limit or 15), 50))
        clauses = ["user_id = ?"]
        params: List[Any] = [user_id]

        if peer_phone:
            p_sql, p_args = self._peer_phone_clause(peer_phone, col="peer_phone")
            if p_sql:
                clauses.append(p_sql)
                params.extend(p_args)
        elif peer_query:
            pq = str(peer_query).strip().lower()
            if pq:
                clauses.append("LOWER(peer_name) LIKE ?")
                params.append(f"%{pq}%")

        if unread_only:
            clauses.append("is_read = 0")
        if since_ts is not None:
            clauses.append("msg_timestamp >= ?")
            params.append(float(since_ts))

        sql = (
            "SELECT * FROM whatsapp_messages WHERE "
            + " AND ".join(clauses)
            + " ORDER BY msg_timestamp DESC, created_at DESC LIMIT ?"
        )
        params.append(cap)
        rows = self.get_connection().execute(sql, tuple(params)).fetchall()
        return [dict(r) for r in rows]

    def search_whatsapp_messages(
        self,
        user_id: str,
        query: str,
        *,
        peer_phone: Optional[str] = None,
        peer_query: Optional[str] = None,
        since_ts: Optional[float] = None,
        limit: int = 15,
    ) -> List[Dict[str, Any]]:
        """Search `user_id`'s WhatsApp messages via FTS5 with safe LIKE fallback."""
        if not user_id:
            return []
        raw_q = str(query or "").strip()
        if not raw_q:
            return self.get_whatsapp_messages(
                user_id, peer_phone=peer_phone, peer_query=peer_query, since_ts=since_ts, limit=limit
            )
        cap = max(1, min(int(limit or 15), 50))
        conn = self.get_connection()

        extra_clauses = ["m.user_id = ?"]
        extra_params: List[Any] = [user_id]
        if peer_phone:
            p_sql, p_args = self._peer_phone_clause(peer_phone, col="m.peer_phone")
            if p_sql:
                extra_clauses.append(p_sql)
                extra_params.extend(p_args)
        elif peer_query:
            pq = str(peer_query).strip().lower()
            if pq:
                extra_clauses.append("LOWER(m.peer_name) LIKE ?")
                extra_params.append(f"%{pq}%")
        if since_ts is not None:
            extra_clauses.append("m.msg_timestamp >= ?")
            extra_params.append(float(since_ts))

        tokens = re.findall(r"[0-9a-zA-Z\u0c00-\u0c7f]+", raw_q)
        rows = []
        if tokens:
            fts_match = " AND ".join(f'"{tok}"' for tok in tokens[:8])
            try:
                sql_fts = (
                    "SELECT m.* FROM whatsapp_messages m "
                    "WHERE m.rowid IN ("
                    "  SELECT rowid FROM whatsapp_messages_fts WHERE whatsapp_messages_fts MATCH ?"
                    ") AND "
                    + " AND ".join(extra_clauses)
                    + " ORDER BY m.msg_timestamp DESC LIMIT ?"
                )
                rows = conn.execute(sql_fts, tuple([fts_match] + extra_params + [cap])).fetchall()
            except sqlite3.OperationalError:
                rows = []

        if not rows:
            like_val = f"%{raw_q.lower()}%"
            sql_like = (
                "SELECT m.* FROM whatsapp_messages m "
                "WHERE (LOWER(m.text) LIKE ? OR LOWER(m.peer_name) LIKE ?) AND "
                + " AND ".join(extra_clauses)
                + " ORDER BY m.msg_timestamp DESC LIMIT ?"
            )
            rows = conn.execute(sql_like, tuple([like_val, like_val] + extra_params + [cap])).fetchall()

        return [dict(r) for r in rows]

    def mark_whatsapp_messages_read(
        self,
        user_id: str,
        *,
        peer_phone: Optional[str] = None,
        message_ids: Optional[List[str]] = None,
    ) -> int:
        """Mark stored WhatsApp messages read strictly within `user_id`."""
        if not user_id:
            return 0
        clauses = ["user_id = ?", "is_read = 0"]
        params: List[Any] = [user_id]
        if peer_phone:
            p_sql, p_args = self._peer_phone_clause(peer_phone, col="peer_phone")
            if p_sql:
                clauses.append(p_sql)
                params.extend(p_args)
        if message_ids:
            clean_ids = [str(m).strip() for m in message_ids if str(m).strip()]
            if not clean_ids:
                return 0
            placeholders = ",".join("?" for _ in clean_ids)
            clauses.append(f"id IN ({placeholders})")
            params.extend(clean_ids)
        conn = self.get_connection()
        cur = conn.execute(f"UPDATE whatsapp_messages SET is_read = 1 WHERE {' AND '.join(clauses)}", tuple(params))
        conn.commit()
        return int(cur.rowcount or 0)

    def get_whatsapp_unread_count(self, user_id: str) -> int:
        if not user_id:
            return 0
        row = self.get_connection().execute(
            "SELECT COUNT(*) AS cnt FROM whatsapp_messages WHERE user_id = ? AND is_read = 0",
            (user_id,),
        ).fetchone()
        return int(row["cnt"]) if row else 0

    # -------------------------------------------------------------
    # MULTI-USER ACCESS GATE, CHALLENGES, INBOX & ADMIN AUDIT
    # -------------------------------------------------------------
    def set_user_gate_state(
        self,
        user_id: str,
        gate_state: str,
        *,
        blocked_reason: Optional[str] = None,
        approved_by: Optional[str] = None,
        approved_at: Optional[float] = None,
        access_epoch: Optional[int] = None,
        is_active: Optional[int] = None,
        bump_token_version: bool = False,
        name: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        if not user_id:
            return None
        conn = self.get_connection()
        clauses = ["gate_state = ?", "last_active_at = ?"]
        params: List[Any] = [gate_state, time.time()]
        if blocked_reason is not None:
            clauses.append("blocked_reason = ?")
            params.append(blocked_reason)
        if approved_by is not None:
            clauses.append("approved_by = ?")
            params.append(approved_by)
        if approved_at is not None:
            clauses.append("approved_at = ?")
            params.append(float(approved_at))
        if access_epoch is not None:
            clauses.append("access_epoch = ?")
            params.append(int(access_epoch))
        if is_active is not None:
            clauses.append("is_active = ?")
            params.append(int(is_active))
        if bump_token_version:
            clauses.append("token_version = COALESCE(token_version, 1) + 1")
        if name is not None:
            clauses.append("name = ?")
            clauses.append("onboarded = 1")
            params.append(name.strip())
        params.append(user_id)
        conn.execute(f"UPDATE users SET {', '.join(clauses)} WHERE id = ?", tuple(params))
        if bump_token_version or gate_state in ("blocked", "revoked"):
            conn.execute("UPDATE refresh_tokens SET revoked = 1 WHERE user_id = ?", (user_id,))
        conn.commit()
        return self.get_user_by_id(user_id)

    def revoke_all_user_tokens(self, user_id: str) -> None:
        if not user_id:
            return
        conn = self.get_connection()
        conn.execute(
            "UPDATE users SET token_version = COALESCE(token_version, 1) + 1 WHERE id = ?",
            (user_id,),
        )
        conn.execute("UPDATE refresh_tokens SET revoked = 1 WHERE user_id = ?", (user_id,))
        conn.commit()

    def increment_and_check_daily_message_cap(
        self,
        user_id: str,
        cap: int,
        date_str: Optional[str] = None,
    ) -> tuple[bool, int]:
        """Atomically check & increment per-user daily message count. Returns (allowed, new_count)."""
        if not user_id or cap <= 0:
            return True, 0
        today = date_str or time.strftime("%Y-%m-%d", time.gmtime())
        conn = self.get_connection()
        row = conn.execute(
            "SELECT daily_msg_count, daily_msg_date FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        if not row:
            return False, 0
        cur_date = str(row["daily_msg_date"] or "")
        cur_count = int(row["daily_msg_count"] or 0) if cur_date == today else 0
        if cur_count >= cap:
            return False, cur_count
        new_count = cur_count + 1
        conn.execute(
            "UPDATE users SET daily_msg_count = ?, daily_msg_date = ?, last_active_at = ? WHERE id = ?",
            (new_count, today, time.time(), user_id),
        )
        conn.commit()
        return True, new_count

    def find_users_by_name_or_id(self, query: str) -> List[Dict[str, Any]]:
        q = (query or "").strip().lower()
        if not q:
            return []
        conn = self.get_connection()
        rows = conn.execute("SELECT * FROM users ORDER BY created_at DESC").fetchall()
        exact = []
        partial = []
        for r in rows:
            d = dict(r)
            uid = str(d.get("id") or "").lower()
            uname = str(d.get("name") or "").strip().lower()
            if uid == q or (uname and uname == q):
                exact.append(d)
            elif (uname and q in uname) or uid.startswith(q):
                partial.append(d)
        return exact if exact else partial

    def list_all_users_admin_summary(self) -> List[Dict[str, Any]]:
        from services.whatsapp_config import mask_phone

        conn = self.get_connection()
        rows = conn.execute(
            "SELECT id, name, email, gate_state, auth_source, is_active, access_epoch, last_active_at, created_at FROM users ORDER BY created_at DESC"
        ).fetchall()
        links = conn.execute(
            "SELECT user_id, phone_e164, session_state, last_activity_at FROM whatsapp_links WHERE status = 'verified'"
        ).fetchall()
        link_by_user: Dict[str, Dict[str, Any]] = {}
        for l in links:
            link_by_user[l["user_id"]] = dict(l)

        out = []
        for r in rows:
            d = dict(r)
            lnk = link_by_user.get(d["id"])
            last_act = d.get("last_active_at") or (lnk.get("last_activity_at") if lnk else None) or d.get("created_at")
            out.append({
                "id": d["id"],
                "name": d.get("name") or "",
                "email": d.get("email") or "",
                "gate_state": d.get("gate_state") or "active",
                "auth_source": d.get("auth_source") or "email_otp",
                "is_active": bool(d.get("is_active", 1)),
                "access_epoch": int(d.get("access_epoch") or 0),
                "last_active_at": last_act,
                "phone_masked": mask_phone(lnk["phone_e164"]) if lnk else "Unlinked",
                "session_state": (lnk.get("session_state") or "INACTIVE") if lnk else "UNLINKED",
            })
        return out

    def record_admin_audit(self, actor_user_id: str, action: str, target_user_id: str = "") -> str:
        """Record strictly PII-free admin action audit entry."""
        audit_id = f"aud_{uuid.uuid4().hex[:12]}"
        conn = self.get_connection()
        conn.execute(
            "INSERT INTO admin_audit_logs (id, actor_user_id, action, target_user_id, created_at) VALUES (?, ?, ?, ?, ?)",
            (audit_id, str(actor_user_id or ""), str(action or ""), str(target_user_id or ""), time.time()),
        )
        conn.commit()
        return audit_id

    def list_admin_audit_logs(self, limit: int = 100) -> List[Dict[str, Any]]:
        rows = self.get_connection().execute(
            "SELECT id, actor_user_id, action, target_user_id, created_at FROM admin_audit_logs ORDER BY created_at DESC LIMIT ?",
            (max(1, min(int(limit), 500)),),
        ).fetchall()
        return [dict(r) for r in rows]

    def set_whatsapp_session_state(
        self,
        user_id: str,
        session_state: str,
        *,
        last_activity_at: Optional[float] = None,
        idle_notified: Optional[int] = None,
    ) -> bool:
        if not user_id:
            return False
        conn = self.get_connection()
        clauses = ["session_state = ?"]
        params: List[Any] = [session_state]
        if last_activity_at is not None:
            clauses.append("last_activity_at = ?")
            params.append(float(last_activity_at))
        if idle_notified is not None:
            clauses.append("idle_notified = ?")
            params.append(int(idle_notified))
        params.append(user_id)
        cur = conn.execute(
            f"UPDATE whatsapp_links SET {', '.join(clauses)} WHERE user_id = ? AND status = 'verified'",
            tuple(params),
        )
        conn.commit()
        return cur.rowcount > 0

    def refresh_whatsapp_activity(self, user_id: str, now: Optional[float] = None) -> None:
        ts = float(now if now is not None else time.time())
        conn = self.get_connection()
        conn.execute(
            "UPDATE whatsapp_links SET last_activity_at = ?, idle_notified = 0 WHERE user_id = ? AND status = 'verified'",
            (ts, user_id),
        )
        conn.execute("UPDATE users SET last_active_at = ? WHERE id = ?", (ts, user_id))
        conn.commit()

    def set_whatsapp_reply_window(
        self,
        user_id: str,
        scope: str,
        duration_minutes: int = 10,
        now: Optional[float] = None,
    ) -> float:
        ts = float(now if now is not None else time.time())
        until = ts + max(1, int(duration_minutes)) * 60.0
        conn = self.get_connection()
        conn.execute(
            "UPDATE whatsapp_links SET reply_window_until = ?, reply_window_scope = ? WHERE user_id = ? AND status = 'verified'",
            (until, str(scope or ""), user_id),
        )
        conn.commit()
        return until

    def clear_whatsapp_reply_window(self, user_id: str) -> None:
        conn = self.get_connection()
        conn.execute(
            "UPDATE whatsapp_links SET reply_window_until = 0, reply_window_scope = '' WHERE user_id = ?",
            (user_id,),
        )
        conn.commit()

    def set_whatsapp_last_read_sender(self, user_id: str, sender_display: str) -> None:
        conn = self.get_connection()
        conn.execute(
            "UPDATE whatsapp_links SET last_read_sender = ? WHERE user_id = ? AND status = 'verified'",
            (str(sender_display or "")[:120], user_id),
        )
        conn.commit()

    def get_verified_link_for_user(self, user_id: str) -> Optional[Dict[str, Any]]:
        if not user_id:
            return None
        row = self.get_connection().execute(
            "SELECT * FROM whatsapp_links WHERE user_id = ? AND status = 'verified' ORDER BY verified_at DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        return dict(row) if row else None

    def get_active_whatsapp_sessions(self) -> List[Dict[str, Any]]:
        rows = self.get_connection().execute(
            "SELECT * FROM whatsapp_links WHERE status = 'verified' AND session_state = 'ACTIVE'"
        ).fetchall()
        return [dict(r) for r in rows]

    def update_link_epoch(self, user_id: str, epoch: int) -> None:
        conn = self.get_connection()
        conn.execute(
            "UPDATE whatsapp_links SET verified_by_epoch = ? WHERE user_id = ? AND status = 'verified'",
            (int(epoch), user_id),
        )
        conn.execute(
            "UPDATE users SET access_epoch = ? WHERE id = ?",
            (int(epoch), user_id),
        )
        conn.commit()

    def is_number_blocked(self, phone_e164: str) -> bool:
        clean = normalize_phone(phone_e164)
        if not clean:
            return False
        row = self.get_connection().execute(
            "SELECT 1 FROM whatsapp_blocked_numbers WHERE phone_e164 = ? LIMIT 1",
            (clean,),
        ).fetchone()
        return row is not None

    def block_number(self, phone_e164: str, blocked_by: str = "", reason: str = "") -> None:
        clean = normalize_phone(phone_e164)
        if not clean:
            return
        conn = self.get_connection()
        conn.execute(
            """INSERT INTO whatsapp_blocked_numbers (phone_e164, blocked_by, reason, created_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(phone_e164) DO UPDATE SET blocked_by = excluded.blocked_by, reason = excluded.reason""",
            (clean, blocked_by or "", reason or "", time.time()),
        )
        conn.commit()

    def unblock_number(self, phone_e164: str = "", *, phone_token: str = "") -> bool:
        conn = self.get_connection()
        clean = normalize_phone(phone_e164) if phone_e164 else ""
        if clean:
            cur = conn.execute("DELETE FROM whatsapp_blocked_numbers WHERE phone_e164 = ?", (clean,))
            conn.commit()
            return cur.rowcount > 0
        if phone_token:
            import hashlib
            rows = conn.execute("SELECT phone_e164 FROM whatsapp_blocked_numbers").fetchall()
            for r in rows:
                tok = hashlib.sha256(str(r["phone_e164"]).encode("utf-8")).hexdigest()[:16]
                if tok == phone_token:
                    cur = conn.execute("DELETE FROM whatsapp_blocked_numbers WHERE phone_e164 = ?", (r["phone_e164"],))
                    conn.commit()
                    return cur.rowcount > 0
        return False

    def list_blocked_numbers(self) -> List[Dict[str, Any]]:
        import hashlib
        rows = self.get_connection().execute(
            "SELECT phone_e164, reason, created_at FROM whatsapp_blocked_numbers ORDER BY created_at DESC"
        ).fetchall()
        out = []
        for r in rows:
            p = str(r["phone_e164"] or "")
            out.append({
                "phone_token": hashlib.sha256(p.encode("utf-8")).hexdigest()[:16],
                "phone_masked": mask_phone(p),
                "reason": r["reason"] or "",
                "created_at": r["created_at"] or 0,
            })
        return out

    def get_challenge_by_phone(self, phone_e164: str) -> Optional[Dict[str, Any]]:
        clean = normalize_phone(phone_e164)
        if not clean:
            return None
        row = self.get_connection().execute(
            "SELECT * FROM whatsapp_access_challenges WHERE phone_e164 = ? ORDER BY updated_at DESC LIMIT 1",
            (clean,),
        ).fetchone()
        return dict(row) if row else None

    def get_challenge_by_code(self, verify_code: str, now: Optional[float] = None) -> Optional[Dict[str, Any]]:
        code = (verify_code or "").strip().upper()
        if not code:
            return None
        ts = float(now if now is not None else time.time())
        row = self.get_connection().execute(
            "SELECT * FROM whatsapp_access_challenges WHERE UPPER(verify_code) = ? AND code_expires_at > ? ORDER BY updated_at DESC LIMIT 1",
            (code, ts),
        ).fetchone()
        return dict(row) if row else None

    def get_challenge_by_user(self, user_id: str) -> Optional[Dict[str, Any]]:
        if not user_id:
            return None
        row = self.get_connection().execute(
            "SELECT * FROM whatsapp_access_challenges WHERE user_id = ? ORDER BY updated_at DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        return dict(row) if row else None

    def upsert_challenge(self, challenge: Dict[str, Any]) -> Dict[str, Any]:
        now = time.time()
        cid = challenge.get("id") or f"wch_{uuid.uuid4().hex[:12]}"
        rec = {
            "id": cid,
            "phone_e164": normalize_phone(challenge.get("phone_e164") or ""),
            "lid_jid": str(challenge.get("lid_jid") or ""),
            "user_id": str(challenge.get("user_id") or ""),
            "entry_mode": str(challenge.get("entry_mode") or "A"),
            "verify_code": str(challenge.get("verify_code") or "").upper(),
            "code_expires_at": float(challenge.get("code_expires_at") or 0),
            "state": str(challenge.get("state") or "IDLE"),
            "challenge_nonce": str(challenge.get("challenge_nonce") or ""),
            "username_hmac": str(challenge.get("username_hmac") or ""),
            "attempts": int(challenge.get("attempts") or 0),
            "lockout_count": int(challenge.get("lockout_count") or 0),
            "locked_until": float(challenge.get("locked_until") or 0),
            "last_started_at": float(challenge.get("last_started_at") or 0),
            "expires_at": float(challenge.get("expires_at") or 0),
            "updated_at": float(challenge.get("updated_at") or now),
            "created_at": float(challenge.get("created_at") or now),
        }
        conn = self.get_connection()
        conn.execute(
            """INSERT INTO whatsapp_access_challenges (
                   id, phone_e164, lid_jid, user_id, entry_mode, verify_code, code_expires_at,
                   state, challenge_nonce, username_hmac, attempts, lockout_count,
                   locked_until, last_started_at, expires_at, updated_at, created_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                   phone_e164 = excluded.phone_e164,
                   lid_jid = excluded.lid_jid,
                   user_id = excluded.user_id,
                   entry_mode = excluded.entry_mode,
                   verify_code = excluded.verify_code,
                   code_expires_at = excluded.code_expires_at,
                   state = excluded.state,
                   challenge_nonce = excluded.challenge_nonce,
                   username_hmac = excluded.username_hmac,
                   attempts = excluded.attempts,
                   lockout_count = excluded.lockout_count,
                   locked_until = excluded.locked_until,
                   last_started_at = excluded.last_started_at,
                   expires_at = excluded.expires_at,
                   updated_at = excluded.updated_at""",
            tuple(rec.values()),
        )
        conn.commit()
        return rec

    def count_recent_challenge_starts(self, window_seconds: int = 3600, now: Optional[float] = None) -> int:
        ts = float(now if now is not None else time.time())
        cutoff = ts - max(1, int(window_seconds))
        row = self.get_connection().execute(
            "SELECT COUNT(*) AS cnt FROM whatsapp_access_challenges WHERE last_started_at >= ?",
            (cutoff,),
        ).fetchone()
        return int(row["cnt"]) if row else 0

    def record_admin_notice(self, event_type: str, summary: str, target_user_id: str = "") -> str:
        nid = f"adm_{uuid.uuid4().hex[:12]}"
        conn = self.get_connection()
        conn.execute(
            """INSERT INTO whatsapp_admin_notice_queue (id, event_type, summary, target_user_id, created_at, flushed)
               VALUES (?, ?, ?, ?, ?, 0)""",
            (nid, str(event_type or "notice"), str(summary or ""), str(target_user_id or ""), time.time()),
        )
        conn.commit()
        return nid

    def pop_pending_admin_notices(self) -> List[Dict[str, Any]]:
        conn = self.get_connection()
        rows = conn.execute(
            "SELECT id, event_type, summary, target_user_id, created_at FROM whatsapp_admin_notice_queue WHERE flushed = 0 ORDER BY created_at ASC"
        ).fetchall()
        if not rows:
            return []
        ids = [r["id"] for r in rows]
        placeholders = ",".join("?" for _ in ids)
        conn.execute(
            f"UPDATE whatsapp_admin_notice_queue SET flushed = 1, flushed_at = ? WHERE id IN ({placeholders})",
            tuple([time.time()] + ids),
        )
        conn.commit()
        return [dict(r) for r in rows]

    def count_and_record_verify_send(self, user_id: str, daily_cap: int, now: Optional[float] = None) -> bool:
        ts = float(now if now is not None else time.time())
        today = time.strftime("%Y-%m-%d", time.gmtime(ts))
        conn = self.get_connection()
        cnt = conn.execute(
            "SELECT COUNT(*) AS c FROM whatsapp_verify_sends WHERE user_id = ? AND sent_date = ?",
            (user_id, today),
        ).fetchone()["c"]
        if cnt >= max(1, int(daily_cap)):
            return False
        conn.execute(
            "INSERT INTO whatsapp_verify_sends (id, user_id, sent_date, created_at) VALUES (?, ?, ?, ?)",
            (f"vs_{uuid.uuid4().hex[:12]}", user_id, today, ts),
        )
        conn.commit()
        return True

    def resolve_contacts_by_phone(self, phone_e164: str) -> List[Dict[str, Any]]:
        clean = normalize_phone(phone_e164)
        if not clean:
            return []
        conn = self.get_connection()
        rows = conn.execute(
            """SELECT c.*, u.gate_state, u.is_active AS user_active
               FROM whatsapp_contacts c
               JOIN users u ON u.id = c.user_id
               WHERE u.is_active = 1"""
        ).fetchall()
        matches = []
        for r in rows:
            d = dict(r)
            c_phone = normalize_phone(d.get("phone_e164") or "")
            if c_phone and (c_phone == clean or (len(clean) >= 10 and len(c_phone) >= 10 and c_phone[-10:] == clean[-10:])):
                d["notify_inbound"] = bool(d.get("notify_inbound", 1))
                d["priority"] = bool(d.get("priority", 0))
                d["trusted"] = bool(d.get("trusted", 1))
                d["is_self"] = bool(d.get("is_self", 0))
                matches.append(d)
        return matches

    def record_unknown_sender_event(self, phone_last4: str, kind: str = "text", now: Optional[float] = None) -> None:
        conn = self.get_connection()
        conn.execute(
            "INSERT INTO whatsapp_unknown_counters (id, phone_last4, kind, received_at) VALUES (?, ?, ?, ?)",
            (f"unk_{uuid.uuid4().hex[:12]}", str(phone_last4 or "****")[-4:], str(kind or "text"), float(now if now is not None else time.time())),
        )
        conn.commit()

    def get_unknown_sender_count(self, window_seconds: Optional[int] = None, now: Optional[float] = None) -> int:
        conn = self.get_connection()
        if window_seconds is not None:
            ts = float(now if now is not None else time.time())
            row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM whatsapp_unknown_counters WHERE received_at >= ?",
                (ts - max(1, int(window_seconds)),),
            ).fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) AS cnt FROM whatsapp_unknown_counters").fetchone()
        return int(row["cnt"]) if row else 0

    def insert_whatsapp_inbox_message(
        self,
        *,
        user_id: str,
        contact_id: str,
        sender_e164: str,
        sender_name: str,
        message_id: str,
        received_at: float,
        kind: str = "text",
        body: str = "",
        media_ref: str = "",
        filename: str = "",
        mime_type: str = "",
        status: str = "unread",
    ) -> Optional[Dict[str, Any]]:
        """Insert third-party message into whatsapp_inbox; returns None on duplicate message_id."""
        if not user_id or not sender_e164:
            return None
        clean_sender = normalize_phone(sender_e164) or sender_e164
        clean_msg_id = (message_id or "").strip()
        conn = self.get_connection()
        if clean_msg_id:
            dup = conn.execute(
                "SELECT id FROM whatsapp_inbox WHERE user_id = ? AND message_id = ?",
                (user_id, clean_msg_id),
            ).fetchone()
            if dup:
                return None

        row_id = f"inb_{uuid.uuid4().hex[:14]}"
        rec_kind = kind if kind in ("text", "voice", "image", "document", "other") else "other"
        conn.execute(
            """INSERT INTO whatsapp_inbox (
                   id, user_id, contact_id, sender_e164, sender_name, message_id,
                   received_at, kind, body, media_ref, filename, mime_type, read_at, status, notified_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, NULL)""",
            (
                row_id,
                user_id,
                contact_id or "",
                clean_sender,
                (sender_name or "").strip(),
                clean_msg_id,
                float(received_at),
                rec_kind,
                body or "",
                media_ref or "",
                filename or "",
                mime_type or "",
                status if status in ("unread", "read", "ignored") else "unread",
            ),
        )
        conn.commit()
        return {
            "id": row_id,
            "user_id": user_id,
            "contact_id": contact_id or "",
            "sender_e164": clean_sender,
            "sender_name": (sender_name or "").strip(),
            "message_id": clean_msg_id,
            "received_at": float(received_at),
            "kind": rec_kind,
            "status": status,
        }

    def list_whatsapp_inbox_metadata(
        self,
        user_id: str,
        *,
        sender_e164: Optional[str] = None,
        unread_only: bool = True,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Metadata-only query on whatsapp_inbox (explicitly excludes body and media_ref)."""
        if not user_id:
            return []
        clauses = ["user_id = ?"]
        params: List[Any] = [user_id]
        if unread_only:
            clauses.append("status = 'unread'")
        if sender_e164:
            clauses.append("sender_e164 = ?")
            params.append(normalize_phone(sender_e164) or sender_e164)
        params.append(max(1, min(int(limit), 200)))
        rows = self.get_connection().execute(
            f"""SELECT id, user_id, contact_id, sender_e164, sender_name, message_id,
                       received_at, kind, filename, mime_type, read_at, status, notified_at
                FROM whatsapp_inbox
                WHERE {' AND '.join(clauses)}
                ORDER BY received_at DESC
                LIMIT ?""",
            tuple(params),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_whatsapp_inbox_unread_summary(
        self,
        user_id: str,
        *,
        sender_filter: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Return unread counts grouped by sender (names and counts ONLY, no body text)."""
        if not user_id:
            return {"unread_total": 0, "senders": []}
        from services.whatsapp_config import mask_phone

        clauses = ["user_id = ?", "status = 'unread'"]
        params: List[Any] = [user_id]
        if sender_filter:
            q_low = sender_filter.strip().lower()
            clauses.append("(LOWER(sender_name) LIKE ? OR sender_e164 = ?)")
            params.extend([f"%{q_low}%", normalize_phone(sender_filter) or sender_filter])
        rows = self.get_connection().execute(
            f"""SELECT contact_id, sender_e164, sender_name,
                       COUNT(*) AS unread_count,
                       MAX(received_at) AS latest_received_at,
                       MAX(kind) AS latest_kind
                FROM whatsapp_inbox
                WHERE {' AND '.join(clauses)}
                GROUP BY sender_e164, contact_id, sender_name
                ORDER BY latest_received_at DESC""",
            tuple(params),
        ).fetchall()
        senders = []
        total = 0
        for r in rows:
            cnt = int(r["unread_count"] or 0)
            total += cnt
            senders.append({
                "contact_id": r["contact_id"] or "",
                "sender_name": r["sender_name"] or mask_phone(r["sender_e164"]),
                "phone_masked": mask_phone(r["sender_e164"]),
                "unread_count": cnt,
                "latest_received_at": r["latest_received_at"],
                "latest_kind": r["latest_kind"] or "text",
            })
        return {"unread_total": total, "senders": senders}

    def read_whatsapp_inbox_messages(
        self,
        user_id: str,
        *,
        sender_e164: Optional[str] = None,
        contact_id: Optional[str] = None,
        since: Optional[float] = None,
        unread_only: bool = True,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """Explicit reader returning message bodies ONLY for user-initiated read tools."""
        if not user_id:
            return []
        clauses = ["user_id = ?"]
        params: List[Any] = [user_id]
        if unread_only:
            clauses.append("status = 'unread'")
        if sender_e164 and contact_id:
            clauses.append("(contact_id = ? OR sender_e164 = ?)")
            params.extend([contact_id, normalize_phone(sender_e164) or sender_e164])
        elif sender_e164:
            clauses.append("sender_e164 = ?")
            params.append(normalize_phone(sender_e164) or sender_e164)
        elif contact_id:
            clauses.append("contact_id = ?")
            params.append(contact_id)
        if since is not None:
            clauses.append("received_at >= ?")
            params.append(float(since))
        params.append(max(1, min(int(limit), 100)))
        rows = self.get_connection().execute(
            f"""SELECT id, user_id, contact_id, sender_e164, sender_name, message_id,
                       received_at, kind, body, media_ref, filename, mime_type, read_at, status
                FROM whatsapp_inbox
                WHERE {' AND '.join(clauses)}
                ORDER BY received_at ASC
                LIMIT ?""",
            tuple(params),
        ).fetchall()
        return [dict(r) for r in rows]

    def mark_whatsapp_inbox_read(
        self,
        user_id: str,
        message_ids: List[str],
        *,
        now: Optional[float] = None,
        status: str = "read",
    ) -> int:
        if not user_id or not message_ids:
            return 0
        clean_ids = [str(m).strip() for m in message_ids if str(m).strip()]
        if not clean_ids:
            return 0
        ts = float(now if now is not None else time.time())
        placeholders = ",".join("?" for _ in clean_ids)
        conn = self.get_connection()
        cur = conn.execute(
            f"""UPDATE whatsapp_inbox
                SET status = ?, read_at = COALESCE(read_at, ?)
                WHERE user_id = ? AND id IN ({placeholders})""",
            tuple([status, ts, user_id] + clean_ids),
        )
        conn.commit()
        return int(cur.rowcount or 0)

    def update_whatsapp_inbox_transcript(self, user_id: str, inbox_id: str, transcript: str) -> None:
        if not user_id or not inbox_id:
            return
        conn = self.get_connection()
        conn.execute(
            "UPDATE whatsapp_inbox SET body = ? WHERE user_id = ? AND id = ?",
            (transcript or "", user_id, inbox_id),
        )
        conn.commit()

    def purge_expired_whatsapp_inbox(self, retention_days: int = 7, now: Optional[float] = None) -> int:
        ts = float(now if now is not None else time.time())
        cutoff = ts - max(1, int(retention_days)) * 86400.0
        conn = self.get_connection()
        cur = conn.execute("DELETE FROM whatsapp_inbox WHERE received_at < ?", (cutoff,))
        conn.execute("DELETE FROM whatsapp_pending_notifications WHERE delivered = 1 AND latest_received_at < ?", (cutoff,))
        conn.commit()
        return int(cur.rowcount or 0)

    def enqueue_pending_wa_notification(
        self,
        *,
        user_id: str,
        contact_id: str,
        sender_name: str,
        kind: str,
        received_at: float,
        ready_at: float,
        is_priority: bool = False,
        held_for_quiet: bool = False,
    ) -> str:
        conn = self.get_connection()
        existing = conn.execute(
            """SELECT id, count, is_priority, held_for_quiet FROM whatsapp_pending_notifications
               WHERE user_id = ? AND contact_id = ? AND delivered = 0
               LIMIT 1""",
            (user_id, contact_id),
        ).fetchone()
        if existing:
            new_cnt = int(existing["count"] or 1) + 1
            new_prio = 1 if (existing["is_priority"] or is_priority) else 0
            new_quiet = 0 if new_prio else (1 if (existing["held_for_quiet"] or held_for_quiet) else 0)
            conn.execute(
                """UPDATE whatsapp_pending_notifications
                   SET count = ?, latest_kind = ?, latest_received_at = ?,
                       is_priority = ?, held_for_quiet = ?,
                       ready_at = CASE WHEN ? < ready_at THEN ? ELSE ready_at END
                   WHERE id = ?""",
                (
                    new_cnt,
                    kind,
                    float(received_at),
                    new_prio,
                    new_quiet,
                    float(ready_at),
                    float(ready_at),
                    existing["id"],
                ),
            )
            conn.commit()
            return existing["id"]

        nid = f"wnf_{uuid.uuid4().hex[:12]}"
        conn.execute(
            """INSERT INTO whatsapp_pending_notifications (
                   id, user_id, contact_id, sender_name, count, latest_kind,
                   first_received_at, latest_received_at, ready_at, is_priority, held_for_quiet, delivered
               ) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, 0)""",
            (
                nid,
                user_id,
                contact_id,
                sender_name or "Someone",
                kind or "text",
                float(received_at),
                float(received_at),
                float(ready_at),
                1 if is_priority else 0,
                1 if (held_for_quiet and not is_priority) else 0,
            ),
        )
        conn.commit()
        return nid

    def pop_due_wa_notifications(self, now: Optional[float] = None, include_quiet: bool = False) -> List[Dict[str, Any]]:
        ts = float(now if now is not None else time.time())
        conn = self.get_connection()
        if include_quiet:
            rows = conn.execute(
                """SELECT id, user_id, contact_id, sender_name, count, latest_kind,
                          first_received_at, latest_received_at, ready_at, is_priority, held_for_quiet
                   FROM whatsapp_pending_notifications
                   WHERE delivered = 0 AND ready_at <= ?
                   ORDER BY latest_received_at ASC""",
                (ts,),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT id, user_id, contact_id, sender_name, count, latest_kind,
                          first_received_at, latest_received_at, ready_at, is_priority, held_for_quiet
                   FROM whatsapp_pending_notifications
                   WHERE delivered = 0 AND held_for_quiet = 0 AND ready_at <= ?
                   ORDER BY latest_received_at ASC""",
                (ts,),
            ).fetchall()
        if not rows:
            return []
        ids = [r["id"] for r in rows]
        placeholders = ",".join("?" for _ in ids)
        conn.execute(
            f"UPDATE whatsapp_pending_notifications SET delivered = 1, delivered_at = ? WHERE id IN ({placeholders})",
            tuple([ts] + ids),
        )
        conn.commit()
        return [dict(r) for r in rows]

    def pop_held_quiet_wa_notifications(self, user_id: str, now: Optional[float] = None) -> List[Dict[str, Any]]:
        ts = float(now if now is not None else time.time())
        conn = self.get_connection()
        rows = conn.execute(
            """SELECT id, user_id, contact_id, sender_name, count, latest_kind,
                      first_received_at, latest_received_at, ready_at, is_priority, held_for_quiet
               FROM whatsapp_pending_notifications
               WHERE user_id = ? AND delivered = 0 AND held_for_quiet = 1
               ORDER BY latest_received_at ASC""",
            (user_id,),
        ).fetchall()
        if not rows:
            return []
        ids = [r["id"] for r in rows]
        placeholders = ",".join("?" for _ in ids)
        conn.execute(
            f"UPDATE whatsapp_pending_notifications SET delivered = 1, delivered_at = ? WHERE id IN ({placeholders})",
            tuple([ts] + ids),
        )
        conn.commit()
        return [dict(r) for r in rows]

    def get_held_quiet_user_ids(self) -> List[str]:
        rows = self.get_connection().execute(
            "SELECT DISTINCT user_id FROM whatsapp_pending_notifications WHERE delivered = 0 AND held_for_quiet = 1"
        ).fetchall()
        return [r["user_id"] for r in rows]
