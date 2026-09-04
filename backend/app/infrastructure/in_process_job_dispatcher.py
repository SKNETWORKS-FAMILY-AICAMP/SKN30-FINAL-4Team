import asyncio
from collections.abc import Awaitable, Callable
from uuid import uuid4


class InProcessJobDispatcher:
    def __init__(self, run_analysis: Callable[[int], Awaitable[None]]) -> None:
        self._run_analysis = run_analysis
        self._tasks: set[asyncio.Task[None]] = set()

    async def enqueue_analysis(self, case_id: int) -> str:
        job_id = uuid4().hex
        task = asyncio.create_task(
            self._run_analysis(case_id),
            name=f"analysis-{job_id}",
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return job_id

    async def shutdown(self) -> None:
        if not self._tasks:
            return
        await asyncio.gather(*tuple(self._tasks), return_exceptions=True)
