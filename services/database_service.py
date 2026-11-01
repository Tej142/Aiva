import os
import sqlite3
import threading
import time
from typing import Optional, List, Dict, Any

from core.shared.config_loader import ensure_dir, get_path
from core.shared.logger import get_logger

logger = get_logger("DatabaseService")

class DatabaseService:
    """
    Thread-safe SQLite database manager for AIVA with WAL mode.
    Handles users, devices, OTP verification, tokens, and command audit logs.
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
            storage_dir = ensure_dir("storage")
            self.db_path = str(get_path("storage", "aiva.db"))
        else:
            self.db_path = db_path

        self._local = threading.local()
        self._create_schema()
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
            created_at REAL NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1
        );

        CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);

        CREATE TABLE IF NOT EXISTS otps (
            id TEXT PRIMARY KEY,
            email TEXT NOT NULL,
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
        """)
        conn.commit()

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
        row = conn.execute(
            "SELECT * FROM users WHERE is_active = 1 ORDER BY created_at ASC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None

    def create_user(self, user_id: str, email: str) -> Dict[str, Any]:
        conn = self.get_connection()
        now = time.time()
        conn.execute(
            "INSERT INTO users (id, email, created_at, is_active) VALUES (?, lower(?), ?, 1)",
            (user_id, email.strip(), now)
        )
        conn.commit()
        return {"id": user_id, "email": email.strip().lower(), "created_at": now, "is_active": 1}

    # -------------------------------------------------------------
    # OTPS
    # -------------------------------------------------------------
    def store_otp(self, otp_id: str, email: str, code_hash: str, salt: str, expires_at: float, ip_address: Optional[str] = None):
        conn = self.get_connection()
        now = time.time()
        conn.execute(
            """INSERT INTO otps (id, email, code_hash, salt, expires_at, attempts, used, ip_address, created_at)
               VALUES (?, lower(?), ?, ?, ?, 0, 0, ?, ?)""",
            (otp_id, email.strip(), code_hash, salt, expires_at, ip_address, now)
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
