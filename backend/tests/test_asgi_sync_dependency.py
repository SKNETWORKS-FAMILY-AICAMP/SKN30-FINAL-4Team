"""Regression test for the reproduced ASGI sync-dependency threadpool timeout.

v0.2 spec section 15 requires validating that "ASGI transport에서
result/chat/analysis dependency 호출이 제한 시간 안에 끝나는지 검증하고,
재현된 sync dependency threadpool timeout을 제거한다."

Before this fix, ``result_repository``/``conversation_repository``/
``analysis_run_service`` (app/api/v1/results.py, conversations.py,
analysis_runs.py) were plain ``def`` functions used as FastAPI dependencies.
FastAPI/Starlette dispatch every plain ``def`` dependency through anyio's
default thread pool (``anyio.to_thread.run_sync``) -- the same bounded,
process-wide ``CapacityLimiter`` used for every other blocking call in the
app, including ``UploadFile``'s spooled-file reads on the upload path. A
dependency that does nothing but read ``request.app.state`` has no business
sharing that bounded pool: under concurrent load it can queue behind a
genuinely slow holder and blow a caller's timeout for no reason.

The first test proves the class of bug directly with a minimal, self
contained app (so it does not depend on timing assumptions about the full
route graph). The second test proves the actual fix landed in this codebase.
"""

from __future__ import annotations

import asyncio
import inspect
import time

import anyio.to_thread
import httpx
import pytest
from fastapi import Depends, FastAPI, Request


def _build_probe_app() -> FastAPI:
    app = FastAPI()
    app.state.value = "ok"

    def sync_dependency(request: Request) -> str:
        # Equivalent to the pre-fix result_repository/conversation_repository/
        # analysis_run_service: a plain ``def`` that only reads app.state.
        return request.app.state.value

    async def async_dependency(request: Request) -> str:
        return request.app.state.value

    @app.get("/sync")
    async def sync_route(value: str = Depends(sync_dependency)) -> dict[str, str]:
        return {"value": value}

    @app.get("/async")
    async def async_route(value: str = Depends(async_dependency)) -> dict[str, str]:
        return {"value": value}

    return app


async def _call(app: FastAPI, path: str, *, headers: dict[str, str] | None = None) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        return await client.get(path, headers=headers)


@pytest.mark.parametrize(
    ("path", "should_stall"),
    [("/sync", True), ("/async", False)],
)
def test_sync_dependency_queues_behind_a_saturated_thread_limiter(
    path: str, should_stall: bool
) -> None:
    app = _build_probe_app()

    async def run() -> None:
        limiter = anyio.to_thread.current_default_thread_limiter()
        original_tokens = limiter.total_tokens
        limiter.total_tokens = 1
        try:

            async def hold_the_only_thread() -> None:
                await anyio.to_thread.run_sync(time.sleep, 0.4)

            holder = asyncio.create_task(hold_the_only_thread())
            await asyncio.sleep(0.05)  # let the holder claim the sole token first
            try:
                response = await asyncio.wait_for(_call(app, path), timeout=0.2)
            except asyncio.TimeoutError:
                assert should_stall, f"{path} unexpectedly stalled behind the saturated limiter"
            else:
                assert response.status_code == 200
                assert not should_stall, f"{path} unexpectedly did not stall"
            await holder
        finally:
            limiter.total_tokens = original_tokens

    asyncio.run(run())


def test_business_dependency_providers_are_async_and_never_touch_the_thread_pool() -> None:
    """The actual fix: these three providers must not be plain ``def``."""

    from app.api.v1 import analysis_runs, conversations, results

    assert inspect.iscoroutinefunction(results.result_repository)
    assert inspect.iscoroutinefunction(conversations.conversation_repository)
    assert inspect.iscoroutinefunction(analysis_runs.analysis_run_service)


def test_result_repository_dependency_resolves_promptly_under_a_saturated_limiter() -> None:
    """End-to-end: the real app's result_repository dependency stays instant."""

    from main import create_app

    class _StubResultRepository:
        # ``ResultRepository`` is a ``runtime_checkable`` Protocol: isinstance()
        # only passes when every member name is present, so every method must
        # exist here even though this probe only exercises get_active_session.
        async def get_analysis_case(self, *, owner_id: str, analysis_case_id: str):
            raise NotImplementedError

        async def get_sim_candidate(self, *, owner_id: str, sim_candidate_id: str):
            raise NotImplementedError

        async def get_active_session(self, *, owner_id: str):
            return None

        async def get_current(self, *, owner_id: str):
            raise NotImplementedError

        async def close_session(self, *, owner_id: str, analysis_session_id: str):
            raise NotImplementedError

        async def list_analysis_history_page(self, *, owner_id: str, snapshot_at, after, limit: int):
            raise NotImplementedError

    app = create_app()
    app.state.offline_mode = True
    app.state.result_repository = _StubResultRepository()

    async def run() -> None:
        limiter = anyio.to_thread.current_default_thread_limiter()
        original_tokens = limiter.total_tokens
        limiter.total_tokens = 1
        try:

            async def hold_the_only_thread() -> None:
                await anyio.to_thread.run_sync(time.sleep, 0.4)

            holder = asyncio.create_task(hold_the_only_thread())
            await asyncio.sleep(0.05)
            response = await asyncio.wait_for(
                _call(
                    app,
                    "/api/v1/analysis-sessions/active",
                    headers={"X-PreReview-Dev-User": "11111111-1111-1111-1111-111111111111"},
                ),
                timeout=0.2,
            )
            assert response.status_code == 204
            await holder
        finally:
            limiter.total_tokens = original_tokens

    asyncio.run(run())
