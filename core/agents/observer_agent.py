from core.agents.base_agent import BaseAgent


class ObserverAgent(BaseAgent):

    def __init__(self, **kwargs):

        super().__init__(
            name="observer_agent",
            **kwargs
        )

        self.capabilities = [
            "observation",
            "event_analysis",
            "runtime_monitoring"
        ]

        self.subscriptions = [
            "browser_url_changed",
            "youtube.detected",
            "workflow.failed",
            "workflow.completed"
        ]

    async def initialize(self):

        self.state["status"] = "ready"

        print("[ObserverAgent] Initialized")

    async def handle_event(self, event):

        event_type = event.get("type")

        print(f"[ObserverAgent] Event received: {event_type}")

        # ======================================
        # Browser URL Observation
        # ======================================

        if event_type == "browser_url_changed":

            data = event.get("data", {})

            url = data.get("url", "")

            await self.analyze_url(url)

        # ======================================
        # YouTube Detection
        # ======================================

        elif event_type == "youtube.detected":

            print("[ObserverAgent] YouTube detected")

            await self.emit_event(
                "observation.youtube_active",
                {
                    "status": True
                }
            )

        # ======================================
        # Workflow Failure
        # ======================================

        elif event_type == "workflow.failed":

            print("[ObserverAgent] Workflow failure observed")

            await self.emit_event(
                "runtime.recovery_required",
                {
                    "source_event": event
                }
            )

    async def execute(self, task):

        print(f"[ObserverAgent] Executing task: {task}")

    async def analyze_url(self, url):

        print(f"[ObserverAgent] Analyzing URL: {url}")

        # ======================================
        # Intelligent Observation Rules
        # ======================================

        if "youtube.com" in url:

            await self.emit_event(
                "youtube.detected",
                {
                    "url": url
                }
            )

        elif "google.com" in url:

            await self.emit_event(
                "google.detected",
                {
                    "url": url
                }
            )