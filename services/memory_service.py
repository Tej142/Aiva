from core.shared.config_loader import load_json, save_json
from core.shared.logger import get_logger

logger = get_logger("MemoryService")

class MemoryService:
    def __init__(self):
        self.memory_file = "memory/runtime_memory.json"
        self.memory = load_json(self.memory_file, default={})

    def remember(self, key, value):
        self.memory[key] = value
        self.save_memory()

    def recall(self, key, default=None):
        return self.memory.get(key, default)

    def all(self):
        return self.memory

    def save_memory(self):
        save_json(self.memory_file, self.memory)

    def load_memory(self):
        self.memory = load_json(self.memory_file, default={})
        return self.memory