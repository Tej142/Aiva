import json
import os

class KnowledgeService:

    def __init__(
        self,
        memory_service,
        tavily_service,
        mistral_service
    ):
        self.knowledge_file = (
            "memory/knowledge_memory.json"
        )

        self.knowledge_memory = (
            self.load_knowledge_memory()
        )

        self.memory_service = (
            memory_service
        )

        self.tavily_service = (
            tavily_service
        )

        self.mistral_service = (
            mistral_service
        )

    def load_knowledge_memory(self):

        if os.path.exists(
            self.knowledge_file
        ):

            with open(
                self.knowledge_file,
                "r"
            ) as file:

                return json.load(
                    file
                )

        return {}
    
    def save_knowledge_memory(self):

        with open(
            self.knowledge_file,
            "w"
        ) as file:

            json.dump(
                self.knowledge_memory,
                file,
                indent=4
            )

    async def research(
        self,
        query
    ):

        memory_key = (
            f"research:{query}"
        )

        cached = (
            self.memory_service
            .recall(memory_key)
        )

        if cached:

            print(
                "[KnowledgeService] Memory hit"
            )

            return cached

        print(
            "[KnowledgeService] Searching web..."
        )

        search_result = await (
            self.tavily_service.search(
                query
            )
        )

        results = (
            search_result.get(
                "results",
                []
            )
        )

        summary = await (

            self.mistral_service
            .summarize_research(

                query,

                results

            )

        )

        research_report = {

            "query":
            query,

            "summary":
            summary,

            "sources": [

                result.get("url")

                for result in results

            ]

        }

        self.memory_service.remember(

            memory_key,

            research_report

        )

        return research_report