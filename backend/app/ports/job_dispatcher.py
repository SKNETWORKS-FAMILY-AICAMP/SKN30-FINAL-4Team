from typing import Protocol


class JobDispatcher(Protocol):
    async def enqueue_analysis(self, case_id: int) -> str: ...
