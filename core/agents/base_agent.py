from abc import ABC, abstractmethod
from typing import Dict, Any, List


class BaseAgent(ABC):

    def __init__(
        self,
        name: str,
        event_bus=None,
        state_manager=None,
        memory_service=None
    ):
        self.name = name

        self.event_bus = event_bus
        self.state_manager = state_manager
        self.memory_service = memory_service

        self.capabilities: List[str] = []
        self.subscriptions: List[str] = []

        self.state: Dict[str, Any] = {
            "status": "idle"
        }

    async def initialize(self):
        """
        Called during runtime startup.
        """
        pass

    @abstractmethod
    async def handle_event(self, event: Dict[str, Any]):
        """
        Handle incoming runtime events.
        """
        pass

    @abstractmethod
    async def execute(self, task: Dict[str, Any]):
        """
        Execute agent-specific tasks.
        """
        pass

    async def shutdown(self):
        """
        Graceful shutdown.
        """
        self.state["status"] = "offline"

    async def emit_event(
        self,
        event_type: str,
        data: Dict[str, Any]
    ):
        """
        Emit runtime event.
        """

        if not self.event_bus:
            return

        await self.event_bus.emit({
            "source": self.name,
            "type": event_type,
            "data": data
        })