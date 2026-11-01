from core.agents.base_agent import BaseAgent


class MemoryAgent(BaseAgent):

    def __init__(self, **kwargs):

        super().__init__(
            name="memory_agent",
            **kwargs
        )

        self.capabilities = [
            "memory_management",
            "context_tracking",
            "runtime_persistence"
        ]

        self.subscriptions = [
            "*"
        ]

    async def initialize(self):

        self.state["status"] = "ready"

        print("[MemoryAgent] Initialized")

    async def handle_event(self, event):

        event_type = event.get("type")

        print(f"[MemoryAgent] Observed event: {event_type}")

        # ======================================
        # Store Important Runtime Events
        # ======================================

        important_events = [
            "goal.created",
            "workflow.completed",
            "workflow.failed",
            "youtube.detected",
            "browser.navigation.completed"
        ]

        if event_type in important_events:

            await self.store_runtime_event(event)

    async def execute(self, task):

        print(f"[MemoryAgent] Executing task: {task}")

    async def store_runtime_event(self, event):

        event_type = event.get("type")

        data = event.get("data", {})

        memory_key = f"event:{event_type}"

        self.memory_service.remember(
            memory_key,
            data
        )

        print(
            f"[MemoryAgent] Stored memory: {memory_key}"
        )