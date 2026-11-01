import asyncio


class TaskQueue:

    def __init__(self):

        self.queue = asyncio.Queue()

    async def add_task(self, task):

        await self.queue.put(task)

    async def get_task(self):

        return await self.queue.get()

    def empty(self):

        return self.queue.empty()