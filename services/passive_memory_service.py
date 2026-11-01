"""
PassiveMemoryService — Extracts personal facts from any conversation turn.

Unlike MemoryExtractionService (which requires explicit "remember X" commands),
this service quietly scans every user message for things a good friend would
naturally pick up and remember — mood, interests, plans, preferences, etc.

Called silently in the background after every conversation turn.
Never interrupts the conversation. Just listens.
"""

import re
import asyncio
from core.shared.logger import get_logger

logger = get_logger("PassiveMemoryService")

# Each entry: (regex_pattern, key_name, value_group_index)
# These are signals a real friend would pick up without being asked
_PASSIVE_PATTERNS = [

    # Identity
    (r"\bmy name(?:'s| is) ([A-Za-z]+)", "name", 1),
    (r"\bcall me ([A-Za-z]+)\b", "name", 1),
    (r"\bi(?:'m| am) ([A-Za-z]+)\b(?!.*\b(?:from|in|at|going|trying|not)\b)", "name", 1),
    (r"\bpeople call me ([A-Za-z]+)", "name", 1),

    # Age
    (r"\bi(?:'m| am) (\d{1,2}) years? old", "age", 1),
    (r"\bmy age is (\d{1,2})", "age", 1),

    # Location
    (r"\bi(?:'m| am) from ([A-Za-z\s]+)", "location", 1),
    (r"\bi live in ([A-Za-z\s]+)", "location", 1),
    (r"\bi(?:'m| am) in ([A-Za-z\s]+)", "location", 1),

    # Profession / study
    (r"\bi(?:'m| am) a[n]? ([a-z\s]+(?:student|developer|engineer|doctor|designer|teacher|manager|entrepreneur|writer|artist))", "profession", 1),
    (r"\bi work (?:as|at) ([A-Za-z\s]+)", "work", 1),
    (r"\bi study ([A-Za-z\s]+)", "study", 1),
    (r"\bi(?:'m| am) studying ([A-Za-z\s]+)", "study", 1),

    # Likes / interests
    (r"\bi (?:love|really love) ([a-z][a-z\s,]+)", "likes", 1),
    (r"\bi like ([a-z][a-z\s,]+)", "likes", 1),
    (r"\bi(?:'m| am) into ([a-z][a-z\s,]+)", "interests", 1),
    (r"\bi enjoy ([a-z][a-z\s,]+)", "likes", 1),
    (r"\bmy favorite (?:is|are) ([a-z][a-z\s,]+)", "favorite", 1),
    (r"\bmy hobby is ([a-z][a-z\s,]+)", "hobby", 1),

    # Dislikes
    (r"\bi (?:hate|don't like|dislike) ([a-z][a-z\s,]+)", "dislikes", 1),
    (r"\bi(?:'m| am) not a fan of ([a-z][a-z\s,]+)", "dislikes", 1),

    # Goals / plans
    (r"\bi(?:'m| am) (?:trying to|planning to|going to|about to) ([a-z][a-z\s]+)", "current_goal", 1),
    (r"\bi want to ([a-z][a-z\s]+)", "goal", 1),
    (r"\bmy goal is (?:to )?([a-z][a-z\s]+)", "goal", 1),

    # Mood / feeling
    (r"\bi(?:'m| am) feeling ([a-z]+)", "mood", 1),
    (r"\bi feel ([a-z]+)", "mood", 1),
    (r"\bi(?:'m| am) (?:very |really |so )?([a-z]+) (?:today|right now|lately|these days)", "mood", 1),
]

# Words that are too generic to store
_IGNORE_VALUES = {
    "a", "the", "this", "that", "just", "good", "okay", "ok", "fine",
    "going", "trying", "doing", "thinking", "here", "there", "not", "sure",
    "happy", "sad", "tired", "excited", "busy"  # too vague without context
}

# Min length for a value to be worth storing
_MIN_VALUE_LEN = 2


class PassiveMemoryService:
    """
    Silently extracts personal facts from any user message during conversation.
    Should be called as a fire-and-forget background task.
    """

    def __init__(self, user_memory_service=None):
        self.user_memory_service = user_memory_service
        logger.info("PassiveMemoryService initialized")

    def bind(self, user_memory_service):
        self.user_memory_service = user_memory_service

    def extract_and_save(self, user_input: str) -> dict:
        """
        Scan user message for facts and save any found.
        Returns dict of {key: value} pairs saved, or empty dict.
        """
        if not self.user_memory_service:
            return {}

        text = user_input.strip().lower()
        saved = {}

        for pattern, key, group_idx in _PASSIVE_PATTERNS:
            match = re.search(pattern, text, re.IGNORECASE)
            if not match:
                continue

            try:
                value = match.group(group_idx).strip().rstrip(".,!?")
            except IndexError:
                continue

            # Skip too-short or blacklisted values
            if len(value) < _MIN_VALUE_LEN or value.lower() in _IGNORE_VALUES:
                continue

            # Don't overwrite explicit name with vague match
            if key == "name" and len(value) < 3:
                continue

            # Capitalize names, keep other values as-is
            if key in ("name", "location", "profession", "work", "study"):
                value = value.title()

            # Don't save if already know this exact value
            existing = self.user_memory_service.get(key)
            if existing and existing.lower() == value.lower():
                continue

            self.user_memory_service.remember(key, value)
            saved[key] = value
            logger.info(f"[PassiveMemory] Learned: {key} = '{value}'")

        return saved

    async def extract_and_save_async(self, user_input: str) -> dict:
        """Async wrapper — run in background so it never blocks the response."""
        return await asyncio.to_thread(self.extract_and_save, user_input)
