from typing import Optional, Dict, Any
from services.database_service import DatabaseService
from core.shared.logger import get_logger

logger = get_logger("UserMemoryService")

class UserMemoryService:
    """
    Per-user persistent memory service backed by SQLite.
    Ensures absolute data isolation: user A never sees user B's facts.
    """
    def __init__(self, db: Optional[DatabaseService] = None):
        self.db = db or DatabaseService()
        self.active_user_id: Optional[str] = None

    def _resolve_user_id(self, user_id: Optional[str] = None) -> Optional[str]:
        if user_id:
            return user_id
        if self.active_user_id:
            return self.active_user_id
        primary = self.db.get_primary_user()
        return primary["id"] if primary else None

    def remember(self, arg1: str, arg2: Any, arg3: Any = None) -> bool:
        """
        Supports both:
          remember(key, value) -> uses active_user_id
          remember(user_id, key, value)
        """
        if arg3 is not None:
            user_id = arg1
            key = str(arg2)
            value = arg3
        else:
            user_id = self._resolve_user_id()
            key = str(arg1)
            value = arg2

        if not user_id:
            logger.warning(f"Cannot remember fact '{key}': No active or provided user_id.")
            return False

        self.db.set_user_memory(user_id, key, value)
        logger.info(f"Remembered user fact: {key} (User: {user_id})")
        return True

    def forget(self, arg1: str, arg2: Optional[str] = None) -> bool:
        """
        Supports both:
          forget(key) -> uses active_user_id
          forget(user_id, key)
        """
        if arg2 is not None:
            user_id = arg1
            key = arg2
        else:
            user_id = self._resolve_user_id()
            key = arg1

        if not user_id:
            return False

        return self.db.delete_user_memory(user_id, key)

    def get(self, arg1: str, arg2: Any = None, arg3: Any = None) -> Any:
        """
        Supports both:
          get(key, default=None) -> uses active_user_id
          get(user_id, key, default=None)
        """
        if arg3 is not None or (arg2 is not None and not isinstance(arg2, (str, type(None))) and arg1 and "-" in arg1):
            user_id = arg1
            key = str(arg2)
            default = arg3
        elif arg2 is not None and isinstance(arg2, str) and not arg3 and ("user_" in arg1 or len(arg1) > 20):
            user_id = arg1
            key = arg2
            default = None
        else:
            user_id = self._resolve_user_id()
            key = arg1
            default = arg2

        if not user_id:
            return default

        memories = self.db.get_user_memories(user_id)
        return memories.get(key, default)

    def get_all(self, user_id: Optional[str] = None) -> Dict[str, Any]:
        """Returns all persistent memories for the given user."""
        uid = self._resolve_user_id(user_id)
        if not uid:
            return {}
        return self.db.get_user_memories(uid)

    def build_context(self, user_id: Optional[str] = None) -> str:
        """Constructs prompt context from user memory facts."""
        mem = self.get_all(user_id)
        if not mem:
            return "No persistent user information has been stored yet."
        return "\n".join([f"{k}: {v}" for k, v in mem.items()])
