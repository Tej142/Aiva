import asyncio

from services.mistral_service import (
    MistralService
)

API_KEY = "9D4i1LFH9umnl1SM6gwktuC15JhGZeYW"


async def main():

    service = MistralService(
        API_KEY
    )

    workflow = await (
        service.generate_workflow(
            "Open YouTube"
        )
    )

    print(workflow)


asyncio.run(main())