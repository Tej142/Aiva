import time
import uuid
import hashlib
import secrets
from typing import Optional, List, Dict, Any, Tuple

from services.database_service import DatabaseService
from core.shared.logger import get_logger

logger = get_logger("DeviceManager")


class DeviceManager:
    def __init__(self, db: Optional[DatabaseService] = None):
        self.db = db or DatabaseService()

    @staticmethod
    def hash_token(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def register_device(
        self,
        user_id: str,
        name: str,
        platform: str = "windows",
        app_version: str = "1.0.0"
    ) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        """
        Creates a new device registration for a user, generates a long-lived cryptographically
        secure device token, stores its hash, and returns the device details with the raw token once.
        """
        if not user_id:
            return False, "User ID is required.", None

        name = name.strip() if name else "My Windows PC"
        platform = platform.strip().lower() if platform else "windows"

        device_id = str(uuid.uuid4())
        raw_token = f"aiva_dev_{secrets.token_hex(24)}"
        token_hash = self.hash_token(raw_token)

        try:
            device = self.db.create_device(
                device_id=device_id,
                user_id=user_id,
                name=name,
                platform=platform,
                app_version=app_version,
                device_token_hash=token_hash
            )
            logger.info(f"Registered new device '{name}' ({device_id}) for user {user_id}")
            return True, "Device registered successfully.", {
                "device": device,
                "device_token": raw_token
            }
        except Exception as e:
            logger.error(f"Failed to register device: {e}")
            return False, f"Failed to register device: {str(e)}", None

    def verify_device_token(self, raw_token: str) -> Optional[Dict[str, Any]]:
        """
        Verifies a raw device token against the database.
        Returns the device record dict if valid, else None.
        """
        if not raw_token or not raw_token.startswith("aiva_dev_"):
            return None

        token_hash = self.hash_token(raw_token.strip())
        device = self.db.get_device_by_token_hash(token_hash)
        return device

    def list_devices(self, user_id: str) -> List[Dict[str, Any]]:
        return self.db.list_devices_for_user(user_id)

    def rename_device(self, device_id: str, user_id: str, new_name: str) -> Tuple[bool, str]:
        if not new_name or not new_name.strip():
            return False, "Device name cannot be empty."

        success = self.db.rename_device(device_id=device_id, user_id=user_id, new_name=new_name.strip())
        if success:
            return True, "Device renamed successfully."
        return False, "Device not found or not owned by user."

    def revoke_device(self, device_id: str, user_id: str) -> Tuple[bool, str]:
        success = self.db.delete_device(device_id=device_id, user_id=user_id)
        if success:
            logger.info(f"Revoked device {device_id} for user {user_id}")
            return True, "Device revoked successfully."
        return False, "Device not found or not owned by user."

    def update_heartbeat(self, device_id: str, status: str = "online"):
        self.db.update_device_status(device_id=device_id, status=status, last_seen=time.time())

