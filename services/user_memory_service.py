from core.shared.config_loader import load_json, save_json
from core.shared.logger import get_logger

logger = get_logger("UserMemoryService")

class UserMemoryService:
    def __init__(self):
        self.memory_file = "memory/user_memory.json"
        self.memory = load_json(self.memory_file, default={})

    def remember(self, key, value):
        self.memory[key] = value
        save_json(self.memory_file, self.memory)
        logger.info(f"Remembered user fact: {key}")

    def forget(self, key):
        if key in self.memory:
            del self.memory[key]
            save_json(self.memory_file, self.memory)
            return True
        return False

    def get(self, key, default=None):
        return self.memory.get(key, default)

    def get_all(self):
        return self.memory.copy()

    def build_context(self) -> str:
        if not self.memory:
            return "No persistent user information has been stored yet."
        return "\n".join([f"{k}: {v}" for k, v in self.memory.items()])