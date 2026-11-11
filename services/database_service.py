import os
import json
import re
import sqlite3
import threading
import time
import uuid
from typing import Optional, List, Dict, Any

from core.shared.config_loader import ensure_dir, get_path
from core.shared.logger import get_logger

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
            is_active INTEGER NOT NULL DEFAULT 1
        );

        CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);

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
        conn.commit()

    def _migrate_schema(self):
        """Ensures any existing database tables get new columns and tables gracefully."""
        conn = self.get_connection()
        cursor = conn.cursor()

        # Check existing columns on users table
        user_cols = [row["name"] for row in cursor.execute("PRAGMA table_info(users)").fetchall()]
        if "name" not in user_cols:
            cursor.execute("ALTER TABLE users ADD COLUMN name TEXT DEFAULT ''")
        if "onboarded" not in user_cols:
            cursor.execute("ALTER TABLE users ADD COLUMN onboarded INTEGER NOT NULL DEFAULT 0")

        # Check existing columns on otps table
        otp_cols = [row["name"] for row in cursor.execute("PRAGMA table_info(otps)").fetchall()]
        if "name" not in otp_cols:
            cursor.execute("ALTER TABLE otps ADD COLUMN name TEXT DEFAULT ''")

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
        conn.commit()

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
        """Assigns pre-existing memory, conversation, and pattern files to the primary account."""
        conn = self.get_connection()
        cursor = conn.cursor()

        # Find or establish primary user (supports both of Charan's emails)
        primary = cursor.execute(
            "SELECT * FROM users WHERE lower(email) IN ('charanpc1420091@gmail.com', 'charante153624@gmail.com') ORDER BY created_at ASC LIMIT 1"
        ).fetchone()
        if not primary:
            primary = cursor.execute(
                "SELECT * FROM users WHERE lower(email) LIKE '%charan%' OR is_active = 1 ORDER BY created_at ASC LIMIT 1"
            ).fetchone()

        if not primary:
            return

        primary_id = primary["id"]

        # Ensure primary user has name Charan and is marked onboarded
        cursor.execute("UPDATE users SET name = 'Charan', onboarded = 1 WHERE id = ?", (primary_id,))
        # Mark other existing accounts as onboarded
        cursor.execute("UPDATE users SET onboarded = 1 WHERE onboarded = 0")
        conn.commit()

        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

        # Target all primary/charan users so both email variants have full access to patterns
        target_users = cursor.execute(
            "SELECT id FROM users WHERE lower(email) IN ('charanpc1420091@gmail.com', 'charante153624@gmail.com') OR lower(email) LIKE '%charan%'"
        ).fetchall()
        target_ids = [u["id"] for u in target_users]
        if primary_id not in target_ids:
            target_ids.append(primary_id)

        # Migrate memory/task_patterns.json to user_task_patterns
        task_pat_path = os.path.join(base_dir, "memory", "task_patterns.json")
        if os.path.exists(task_pat_path):
            try:
                with open(task_pat_path, "r", encoding="utf-8") as f:
                    patterns = json.load(f)
                now = time.time()
                for target_uid in target_ids:
                    for pat_name, pat_data in patterns.items():
                        pat_id = f"pat_{uuid.uuid4().hex[:12]}"
                        cursor.execute(
                            """INSERT OR IGNORE INTO user_task_patterns (id, user_id, pattern_name, pattern_json, created_at)
                               VALUES (?, ?, ?, ?, ?)""",
                            (pat_id, target_uid, pat_name, json.dumps(pat_data), now)
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
        """Returns the primary (first active) user in the system."""
        conn = self.get_connection()
        # Prefer Charan if exists, otherwise first active user
        row = conn.execute(
            "SELECT * FROM users WHERE lower(email) = 'charanpc1420091@gmail.com' AND is_active = 1 LIMIT 1"
        ).fetchone()
        if not row:
            row = conn.execute(
                "SELECT * FROM users WHERE is_active = 1 ORDER BY created_at ASC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    def create_user(self, user_id: str, email: str, name: str = "", onboarded: int = 0) -> Dict[str, Any]:
        conn = self.get_connection()
        now = time.time()
        conn.execute(
            "INSERT INTO users (id, email, name, onboarded, created_at, is_active) VALUES (?, lower(?), ?, ?, ?, 1)",
            (user_id, email.strip(), name.strip(), onboarded, now)
        )
        conn.commit()

        # If Charan or primary user, ensure patterns and memories are migrated to this user
        if "charan" in email.lower() or conn.execute("SELECT count(*) as c FROM users").fetchone()["c"] == 1:
            try:
                self._migrate_existing_data()
            except Exception as e:
                logger.warning(f"Failed to auto-migrate data for new user {email}: {e}")

        return {
            "id": user_id,
            "email": email.strip().lower(),
            "name": name.strip(),
            "onboarded": onboarded,
            "created_at": now,
            "is_active": 1
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

    def verify_whatsapp_link(self, phone_e164: str, code: str) -> bool:
        conn = self.get_connection()
        now = time.time()
        cursor = conn.execute(
            """UPDATE whatsapp_links
               SET status = 'verified', verified_at = ?, link_code = NULL, link_code_expires_at = NULL
               WHERE phone_e164 = ? AND link_code = ? AND link_code_expires_at > ?""",
            (now, phone_e164, code.strip().upper(), now)
        )
        conn.commit()
        return cursor.rowcount > 0

    def get_whatsapp_link_by_phone(self, phone_e164: str) -> Optional[Dict[str, Any]]:
        conn = self.get_connection()
        row = conn.execute(
            "SELECT * FROM whatsapp_links WHERE phone_e164 = ? AND status = 'verified'",
            (phone_e164,)
        ).fetchone()
        return dict(row) if row else None

    def get_whatsapp_links_for_user(self, user_id: str) -> List[Dict[str, Any]]:
        conn = self.get_connection()
        rows = conn.execute(
            "SELECT * FROM whatsapp_links WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def unlink_whatsapp(self, user_id: str, phone_e164: str) -> bool:
        conn = self.get_connection()
        cursor = conn.execute(
            "DELETE FROM whatsapp_links WHERE user_id = ? AND phone_e164 = ?",
            (user_id, phone_e164)
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
        trusted: bool = True
    ) -> Dict[str, Any]:
        conn = self.get_connection()
        contact_id = f"wac_{uuid.uuid4().hex[:12]}"
        now = time.time()
        aliases_list = [a.strip() for a in (aliases or []) if a.strip()]
        aliases_json = json.dumps(aliases_list)
        conn.execute(
            """INSERT INTO whatsapp_contacts (id, user_id, display_name, aliases_json, phone_e164, is_self, trusted, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (contact_id, user_id, display_name.strip(), aliases_json, phone_e164.strip(), 1 if is_self else 0, 1 if trusted else 0, now, now)
        )
        conn.commit()
        return {
            "id": contact_id,
            "user_id": user_id,
            "display_name": display_name.strip(),
            "aliases": aliases_list,
            "phone_e164": phone_e164.strip(),
            "is_self": is_self,
            "trusted": trusted,
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
        return d

    def update_whatsapp_contact(
        self,
        user_id: str,
        contact_id: str,
        display_name: Optional[str] = None,
        phone_e164: Optional[str] = None,
        aliases: Optional[List[str]] = None,
        trusted: Optional[bool] = None,
        is_self: Optional[bool] = None
    ) -> Optional[Dict[str, Any]]:
        conn = self.get_connection()
        current = self.get_whatsapp_contact_by_id(user_id, contact_id)
        if not current:
            return None

        new_name = display_name.strip() if display_name is not None else current["display_name"]
        new_phone = phone_e164.strip() if phone_e164 is not None else current["phone_e164"]
        new_aliases = aliases if aliases is not None else current["aliases"]
        new_trusted = trusted if trusted is not None else current["trusted"]
        new_is_self = is_self if is_self is not None else current["is_self"]
        now = time.time()

        conn.execute(
            """UPDATE whatsapp_contacts
               SET display_name = ?, phone_e164 = ?, aliases_json = ?, trusted = ?, is_self = ?, updated_at = ?
               WHERE user_id = ? AND id = ?""",
            (
                new_name,
                new_phone,
                json.dumps([a.strip() for a in new_aliases if a.strip()]),
                1 if new_trusted else 0,
                1 if new_is_self else 0,
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
