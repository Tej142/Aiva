"""Domain models and shared utilities for AIVA's unified memory engine."""

from dataclasses import dataclass, field
from enum import Enum
import hashlib
import re
import time
import uuid
from typing import Any, Dict, List, Optional


class MemoryType(str, Enum):
    WORKING = "working"
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    PROCEDURAL = "procedural"
    PROSPECTIVE = "prospective"
    KNOWLEDGE = "knowledge"


class MemorySource(str, Enum):
    USER_SAID = "user_said"
    INFERRED = "inferred"
    WEB = "web"
    SYSTEM = "system"


class Channel(str, Enum):
    VOICE = "voice"
    HUD = "hud"
    WHATSAPP = "whatsapp"


class MemoryStatus(str, Enum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    FORGOTTEN = "forgotten"


class EntityKind(str, Enum):
    PERSON = "person"
    DEVICE = "device"
    PROJECT = "project"
    PLACE = "place"
    ORG = "org"
    OTHER = "other"


class ReminderStatus(str, Enum):
    PENDING = "pending"
    SENT = "sent"
    DONE = "done"
    SNOOZED = "snoozed"
    CANCELLED = "cancelled"


VALID_MEMORY_TYPES = {m.value for m in MemoryType}
VALID_MEMORY_SOURCES = {s.value for s in MemorySource}
VALID_CHANNELS = {c.value for c in Channel}
VALID_MEMORY_STATUSES = {s.value for s in MemoryStatus}
VALID_ENTITY_KINDS = {k.value for k in EntityKind}
VALID_REMINDER_STATUSES = {s.value for s in ReminderStatus}


def new_id() -> str:
    return str(uuid.uuid4())


def estimate_tokens(text: Optional[str]) -> int:
    """Fast heuristic token count (~4 chars/token) safe for sub-millisecond hot paths."""
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


def normalize_text(text: Optional[str]) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", text.strip().lower())


def content_hash(text: Optional[str]) -> str:
    norm = normalize_text(text)
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


@dataclass
class MemoryItem:
    id: str
    user_id: str
    type: str
    content: str
    summary: str = ""
    entities: List[str] = field(default_factory=list)
    importance: float = 0.5
    confidence: float = 0.8
    source: str = MemorySource.USER_SAID.value
    channel: str = Channel.HUD.value
    created_at: float = field(default_factory=time.time)
    last_accessed: float = field(default_factory=time.time)
    access_count: int = 0
    valid_from: Optional[float] = None
    superseded_by: Optional[str] = None
    expires_at: Optional[float] = None
    status: str = MemoryStatus.ACTIVE.value
    # Transient ranking metadata (not persisted as columns)
    score: float = 0.0
    relevance: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "type": self.type,
            "content": self.content,
            "summary": self.summary,
            "entities": self.entities,
            "importance": round(float(self.importance), 4),
            "confidence": round(float(self.confidence), 4),
            "source": self.source,
            "channel": self.channel,
            "created_at": self.created_at,
            "last_accessed": self.last_accessed,
            "access_count": int(self.access_count),
            "valid_from": self.valid_from,
            "superseded_by": self.superseded_by,
            "expires_at": self.expires_at,
            "status": self.status,
            "score": round(float(self.score), 4),
            "relevance": round(float(self.relevance), 4),
        }


@dataclass
class EntityItem:
    id: str
    user_id: str
    name: str
    kind: str = EntityKind.OTHER.value
    aliases: List[str] = field(default_factory=list)
    attributes: Dict[str, Any] = field(default_factory=dict)
    whatsapp_contact_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "name": self.name,
            "kind": self.kind,
            "aliases": self.aliases,
            "attributes": self.attributes,
            "whatsapp_contact_id": self.whatsapp_contact_id,
        }


@dataclass
class EntityLink:
    id: str
    user_id: str
    from_id: str
    to_id: str
    relation: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "from_id": self.from_id,
            "to_id": self.to_id,
            "relation": self.relation,
        }


@dataclass
class ReminderItem:
    id: str
    user_id: str
    text: str
    due_at: float
    recurrence: Optional[str] = None
    channel: str = Channel.HUD.value
    status: str = ReminderStatus.PENDING.value
    created_from_memory_id: Optional[str] = None
    urgent: bool = False
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "text": self.text,
            "due_at": self.due_at,
            "recurrence": self.recurrence,
            "channel": self.channel,
            "status": self.status,
            "created_from_memory_id": self.created_from_memory_id,
            "urgent": bool(self.urgent),
            "updated_at": self.updated_at,
        }


@dataclass
class WorkingTurn:
    role: str
    content: str
    channel: str = Channel.HUD.value
    timestamp: float = field(default_factory=time.time)
    session_id: str = "default"
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "role": self.role,
            "content": self.content,
            "channel": self.channel,
            "timestamp": self.timestamp,
            "session_id": self.session_id,
            "metadata": self.metadata,
        }


@dataclass
class MemorySettings:
    memory_enabled: bool = True
    learning_paused: bool = False
    quiet_hours_start: str = "23:00"
    quiet_hours_end: str = "07:00"
    morning_briefing_enabled: bool = False
    morning_briefing_time: str = "08:00"
    preferred_reminder_channel: str = Channel.HUD.value

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "MemorySettings":
        if not data or not isinstance(data, dict):
            return cls()
        pref = data.get("preferred_reminder_channel", Channel.HUD.value)
        if pref not in VALID_CHANNELS:
            pref = Channel.HUD.value
        return cls(
            memory_enabled=bool(data.get("memory_enabled", True)),
            learning_paused=bool(data.get("learning_paused", False)),
            quiet_hours_start=str(data.get("quiet_hours_start", "23:00") or "23:00"),
            quiet_hours_end=str(data.get("quiet_hours_end", "07:00") or "07:00"),
            morning_briefing_enabled=bool(data.get("morning_briefing_enabled", False)),
            morning_briefing_time=str(data.get("morning_briefing_time", "08:00") or "08:00"),
            preferred_reminder_channel=pref,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "memory_enabled": self.memory_enabled,
            "learning_paused": self.learning_paused,
            "quiet_hours_start": self.quiet_hours_start,
            "quiet_hours_end": self.quiet_hours_end,
            "morning_briefing_enabled": self.morning_briefing_enabled,
            "morning_briefing_time": self.morning_briefing_time,
            "preferred_reminder_channel": self.preferred_reminder_channel,
        }

