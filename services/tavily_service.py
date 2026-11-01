import asyncio
from tavily import TavilyClient
from core.shared.config_loader import load_secrets
from core.shared.logger import get_logger

logger = get_logger("TavilyService")

class TavilyService:
    def __init__(self):
        secrets = load_secrets()
        api_key = secrets.get("tavily_api_key", "")
        self.client = TavilyClient(api_key=api_key) if api_key else None

    async def search(self, query: str, max_results: int = 5) -> dict:
        if not self.client:
            logger.warning("Tavily API key missing or client uninitialized.")
            return {"results": []}

        try:
            result = await asyncio.to_thread(
                self.client.search,
                query=query,
                max_results=max_results,
                search_depth="advanced"
            )
            return result
        except Exception as err:
            logger.error(f"Tavily search error for '{query}': {err}")
            return {"results": []}