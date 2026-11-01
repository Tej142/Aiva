import os
import json
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
        self._create_schema()
        self._migrate_schema()
        self._migrate_existing_data()
        logger.info(f"Database initialized at: {self.db_path}")

    def get_connection(self) -> sqlite3.Connection:
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

        CREATE TABLE IF NOT EXISTS user_memories (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            key TEXT NOT NULL,
            value_json TEXT NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
            UNIQUE(user_id, key)
        );

        CREATE INDEX IF NOT EXISTS idx_user_memories_user ON user_memories(user_id);

        CREATE TABLE IF NOT EXISTS conversation_history (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            role TEXT NOT NULL,
            message TEXT NOT NULL,
            route TEXT,
            created_at REAL NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_conv_user ON conversation_history(user_id, created_at);

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
        """)
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

        conn.commit()

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

        # 1. Migrate memory/user_memory.json to user_memories
        user_mem_path = os.path.join(base_dir, "memory", "user_memory.json")
        if os.path.exists(user_mem_path):
            try:
                with open(user_mem_path, "r", encoding="utf-8") as f:
                    mem_data = json.load(f)
                now = time.time()
                for target_uid in target_ids:
                    for k, v in mem_data.items():
                        mem_id = f"mem_{uuid.uuid4().hex[:12]}"
                        cursor.execute(
                            """INSERT OR IGNORE INTO user_memories (id, user_id, key, value_json, created_at, updated_at)
                               VALUES (?, ?, ?, ?, ?, ?)""",
                            (mem_id, target_uid, k, json.dumps(v), now, now)
                        )
                conn.commit()
            except Exception as e:
                logger.warning(f"Failed migrating user_memory.json: {e}")

        # 2. Migrate memory/conversation_memory.json to conversation_history
        conv_mem_path = os.path.join(base_dir, "memory", "conversation_memory.json")
        if os.path.exists(conv_mem_path):
            try:
                existing_count = cursor.execute(
                    "SELECT count(*) as c FROM conversation_history WHERE user_id = ?",
                    (primary_id,)
                ).fetchone()["c"]
                if existing_count == 0:
                    with open(conv_mem_path, "r", encoding="utf-8") as f:
                        conv_data = json.load(f)
                    history_list = conv_data.get("history", [])
                    now = time.time() - (len(history_list) * 60)
                    for item in history_list:
                        u_msg = item.get("user")
                        a_msg = item.get("aiva")
                        if u_msg:
                            cursor.execute(
                                """INSERT INTO conversation_history (id, user_id, role, message, route, created_at)
                                   VALUES (?, ?, 'user', ?, 'general', ?)""",
                                (str(uuid.uuid4()), primary_id, u_msg, now)
                            )
                            now += 1.0
                        if a_msg:
                            cursor.execute(
                                """INSERT INTO conversation_history (id, user_id, role, message, route, created_at)
                                   VALUES (?, ?, 'aiva', ?, 'general', ?)""",
                                (str(uuid.uuid4()), primary_id, a_msg, now)
                            )
                            now += 1.0
                    conn.commit()
            except Exception as e:
                logger.warning(f"Failed migrating conversation_memory.json: {e}")

        # 3. Migrate memory/task_patterns.json to user_task_patterns
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
    # PER-USER MEMORY
    # -------------------------------------------------------------
    def get_user_memories(self, user_id: str) -> Dict[str, Any]:
        """Returns all persistent memory facts scoped strictly to this user_id."""
        conn = self.get_connection()
        rows = conn.execute(
            "SELECT key, value_json FROM user_memories WHERE user_id = ?",
            (user_id,)
        ).fetchall()
        result = {}
        for r in rows:
            try:
                result[r["key"]] = json.loads(r["value_json"])
            except Exception:
                result[r["key"]] = r["value_json"]
        return result

    def set_user_memory(self, user_id: str, key: str, value: Any) -> bool:
        """Stores a persistent memory fact strictly scoped to user_id."""
        conn = self.get_connection()
        now = time.time()
        mem_id = f"mem_{uuid.uuid4().hex[:12]}"
        val_json = json.dumps(value)
        conn.execute(
            """INSERT INTO user_memories (id, user_id, key, value_json, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(user_id, key) DO UPDATE SET value_json = excluded.value_json, updated_at = excluded.updated_at""",
            (mem_id, user_id, key, val_json, now, now)
        )
        conn.commit()
        return True

    def delete_user_memory(self, user_id: str, key: str) -> bool:
        """Deletes a persistent memory fact strictly scoped to user_id."""
        conn = self.get_connection()
        cursor = conn.execute(
            "DELETE FROM user_memories WHERE user_id = ? AND key = ?",
            (user_id, key)
        )
        conn.commit()
        return cursor.rowcount > 0

    # -------------------------------------------------------------
    # CONVERSATION HISTORY
    # -------------------------------------------------------------
    def add_conversation_turn(
        self,
        user_id: str,
        role: str,
        message: str,
        route: Optional[str] = None
    ) -> str:
        """Appends a turn to conversation_history strictly scoped to user_id."""
        conn = self.get_connection()
        turn_id = str(uuid.uuid4())
        now = time.time()
        conn.execute(
            """INSERT INTO conversation_history (id, user_id, role, message, route, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (turn_id, user_id, role, message, route, now)
        )
        conn.commit()
        return turn_id

    def get_conversation_history(self, user_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        """Returns conversation turns for this user_id in chronological order."""
        conn = self.get_connection()
        rows = conn.execute(
            """SELECT * FROM (
                   SELECT id, user_id, role, message, route, created_at 
                   FROM conversation_history 
                   WHERE user_id = ? 
                   ORDER BY created_at DESC 
                   LIMIT ?
               ) ORDER BY created_at ASC""",
            (user_id, limit)
        ).fetchall()
        return [dict(r) for r in rows]

    def clear_conversation_history(self, user_id: str) -> bool:
        """Deletes all conversation history for user_id."""
        conn = self.get_connection()
        conn.execute("DELETE FROM conversation_history WHERE user_id = ?", (user_id,))
        conn.commit()
        return True

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
