import asyncio
import time


class Scheduler:

    def __init__(self):

        self.jobs = []

    def add_job(self, delay, coro, *args):

        run_at = time.time() + delay

        self.jobs.append({
            "run_at": run_at,
            "coro": coro,
            "args": args
        })

    async def tick(self):

        current_time = time.time()

        ready_jobs = []

        for job in self.jobs:

            if current_time >= job["run_at"]:
                ready_jobs.append(job)

        for job in ready_jobs:

            asyncio.create_task(
                job["coro"](*job["args"])
            )

            self.jobs.remove(job)
    
    async def wait(
        self,
        seconds
    ):

        await asyncio.sleep(
            seconds
        )