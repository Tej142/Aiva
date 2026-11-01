import asyncio

from services.memory_service import (
    MemoryService
)

from services.tavily_service import (
    TavilyService
)

from services.knowledge_service import (
    KnowledgeService
)


async def main():

    memory = MemoryService()

    tavily = TavilyService()

    knowledge = KnowledgeService(

        memory,
        tavily

    )

    result = await (

        knowledge.research(

            "Government Polytechnic Pillaripattu"

        )

    )

    print(result)


asyncio.run(main())