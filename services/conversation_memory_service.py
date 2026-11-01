from typing import Optional, List, Dict, Any
from services.database_service import DatabaseService
from core.shared.logger import get_logger

logger = get_logger("ConversationMemoryService")

class ConversationMemoryService:
    """
    Per-user conversation memory service backed by SQLite.
    Guarantees that a user's dialogue history is isolated to their account.
    """
    def __init__(self, db: Optional[DatabaseService] = None):
        self.db = db or DatabaseService()
        self.active_user_id: Optional[str] = None
        self._current_topics: Dict[str, Optional[str]] = {}

    def _resolve_user_id(self, user_id: Optional[str] = None) -> Optional[str]:
        if user_id:
            return user_id
        if self.active_user_id:
            return self.active_user_id
        primary = self.db.get_primary_user()
        return primary["id"] if primary else None

    @property
    def history(self) -> List[Dict[str, str]]:
        """Returns the in-memory/DB history for the active user."""
        return self.get_history()

    @history.setter
    def history(self, val):
        pass

    def add_turn(
        self,
        user_message: str,
        aiva_message: str,
        user_id: Optional[str] = None,
        route: Optional[str] = None
    ):
        uid = self._resolve_user_id(user_id)
        if not uid:
            logger.warning("No user_id resolved for conversation turn.")
            return

        self.db.add_conversation_turn(uid, "user", user_message, route)
        self.db.add_conversation_turn(uid, "aiva", aiva_message, route)

    def get_history(
        self,
        limit: Optional[int] = None,
        user_id: Optional[str] = None
    ) -> List[Dict[str, str]]:
        uid = self._resolve_user_id(user_id)
        if not uid:
            return []

        # Read chronological turns
        raw_rows = self.db.get_conversation_history(uid, limit=(limit * 2 if limit else 100))
        formatted = []
        pending_user = None

        for row in raw_rows:
            role = row["role"]
            msg = row["message"]
            if role == "user":
                if pending_user is not None:
                    formatted.append({"user": pending_user, "aiva": ""})
                pending_user = msg
            elif role == "aiva":
                formatted.append({"user": pending_user or "", "aiva": msg})
                pending_user = None

        if pending_user is not None:
            formatted.append({"user": pending_user, "aiva": ""})

        if limit is not None:
            return formatted[-limit:]
        return formatted

    def set_current_topic(self, topic: str, user_id: Optional[str] = None):
        uid = self._resolve_user_id(user_id) or "default"
        self._current_topics[uid] = topic

    def get_current_topic(self, user_id: Optional[str] = None) -> Optional[str]:
        uid = self._resolve_user_id(user_id) or "default"
        return self._current_topics.get(uid)

    def clear(self, user_id: Optional[str] = None):
        uid = self._resolve_user_id(user_id)
        if uid:
            self.db.clear_conversation_history(uid)
            self._current_topics.pop(uid, None)
