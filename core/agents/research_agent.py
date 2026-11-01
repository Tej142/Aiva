from core.agents.base_agent import BaseAgent


class ResearchAgent(BaseAgent):

    def __init__(
        self,
        knowledge_service=None,
        conversation_memory_service=None,
        **kwargs
    ):

        super().__init__(
            name="research_agent",
            **kwargs
        )

        self.knowledge_service = (
            knowledge_service
        )

        self.conversation_memory_service = (
            conversation_memory_service
        )

        self.capabilities = [
            "research",
            "knowledge_lookup",
            "context_enrichment"
        ]

        self.subscriptions = [
            "research.requested",
            "goal.research",
            "workflow.research_needed"
        ]

    async def initialize(self):

        self.state["status"] = "ready"

        print(
            "[ResearchAgent] Initialized"
        )

    async def handle_event(
        self,
        event
    ):

        event_type = event.get(
            "type"
        )

        print(
            f"[ResearchAgent] Event: {event_type}"
        )

        if event_type == "research.requested":

            data = event.get(
                "data",
                {}
            )

            topic = (
                data.get("topic")
                or
                data.get("query")
            )

            if topic:

                await self.perform_research(
                    topic
                )

        elif event_type == "goal.research":

            data = event.get(
                "data",
                {}
            )

            topic = (
                data.get("topic")
                or
                data.get("query")
            )

            if topic:

                await self.perform_research(
                    topic
                )

        elif event_type == "workflow.research_needed":

            data = event.get(
                "data",
                {}
            )

            topic = (
                data.get("topic")
                or
                data.get("query")
            )

            if topic:

                await self.perform_research(
                    topic
                )

    async def execute(
        self,
        task
    ):

        print(
            f"[ResearchAgent] Executing task: {task}"
        )

        topic = (
            task.get("topic")
            or
            task.get("query")
        )

        if not topic:

            parameters = task.get(
                "parameters",
                {}
            )

            topic = (
                parameters.get("topic")
                or
                parameters.get("query")
            )

        if not topic:

            print(
                "[ResearchAgent] No topic provided"
            )

            return None

        return await self.perform_research(
            topic
        )

    async def perform_research(
        self,
        topic
    ):

        print(
            f"[ResearchAgent] Researching: {topic}"
        )

        if not self.knowledge_service:

            print(
                "[ResearchAgent] "
                "KnowledgeService missing"
            )

            return None

        try:

            result = await (
                self.knowledge_service
                .research(
                    topic
                )
            )

            if not result:

                print(
                    "[ResearchAgent] "
                    "Research returned no result"
                )

                return None

            summary = result.get(
                "summary",
                ""
            )

            print(
                "\n=============================="
            )

            print(
                "[AIVA RESEARCH RESULT]"
            )

            print(
                summary
            )

            print(
                "==============================\n"
            )

            # =====================================
            # UPDATE CONVERSATION MEMORY
            # =====================================

            if self.conversation_memory_service:

                self.conversation_memory_service\
                    .set_current_topic(
                        topic
                    )

                self.conversation_memory_service\
                    .add_turn(
                        f"Research {topic}",
                        summary
                    )

                print(
                    "[ResearchAgent] "
                    "Conversation context updated"
                )

            # =====================================
            # EMIT RESEARCH COMPLETED EVENT
            # =====================================

            await self.emit_event(

                "research.completed",

                result

            )

            print(
                "[ResearchAgent] Research completed"
            )

            return result

        except Exception as error:

            print(
                "[ResearchAgent Error]",
                error
            )

            return None