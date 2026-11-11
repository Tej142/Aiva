from core.agents.base_agent import BaseAgent


class MemoryAgent(BaseAgent):

    def __init__(self, get_active_user_id=None, **kwargs):
        super().__init__(name="memory_agent", **kwargs)
        self.get_active_user_id = get_active_user_id
        self.capabilities = [
            "memory_management",
            "context_tracking",
            "runtime_persistence",
        ]
        self.subscriptions = ["*"]

    async def initialize(self):
        self.state["status"] = "ready"
        print("[MemoryAgent] Initialized")

    async def handle_event(self, event):
        event_type = event.get("type")
        important_events = {
            "goal.created",
            "workflow.completed",
            "workflow.failed",
            "youtube.detected",
            "browser.navigation.completed",
        }
        if event_type in important_events:
            await self.store_runtime_event(event)

    async def execute(self, task):
        query = (task or {}).get("query") or ""
        uid = (task or {}).get("user_id") or (self.get_active_user_id() if self.get_active_user_id else None)
        if self.memory_engine and uid and query:
            return self.memory_engine.recall(uid, query)
        return None

    async def store_runtime_event(self, event):
        if not self.memory_engine:
            return
        event_type = event.get("type")
        data = event.get("data", {}) or {}
        uid = data.get("user_id") or (self.get_active_user_id() if self.get_active_user_id else None)
        if not uid:
            return

        goal_or_url = data.get("goal") or data.get("url") or event_type
        summary = f"{event_type}: {str(goal_or_url)[:160]}"
        self.memory_engine.record_system_event(
            user_id=uid,
            event_summary=summary,
            importance=0.35,
            channel=data.get("channel") or "hud",
        )
