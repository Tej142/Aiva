import asyncio

from services.memory_service import MemoryService
from services.tavily_service import TavilyService
from services.mistral_service import MistralService
from services.knowledge_service import KnowledgeService


async def main():

    knowledge = KnowledgeService(

        MemoryService(),

        TavilyService(),

        MistralService()

    )

    result = await (

        knowledge.research(

            "Government Polytechnic Pillaripattu"

        )

    )

    print()

    print(
        result["summary"]
    )

    print()

    print(result)


asyncio.run(main())