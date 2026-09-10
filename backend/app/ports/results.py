"""Read-only result-query boundary for the cookie-authenticated API.

The public API never accepts an owner id.  Each repository method receives
the already authenticated principal only so an adapter can establish the
equivalent Supabase ``auth.uid()`` context before using the curated database
views/RPCs.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable


class ResultQueryError(RuntimeError):
    """Base class for safe result-read failures."""


class ResultNotFound(ResultQueryError):
    """The result does not exist or is not visible to the authenticated user."""


class ResultRepositoryUnavailable(ResultQueryError):
    """The internal database is unavailable or has not been configured."""


@runtime_checkable
class ResultRepository(Protocol):
    """Curated, owner-scoped read operations exposed to HTTP routes."""

    async def get_analysis_case(
        self, *, owner_id: str, analysis_case_id: str
    ) -> Mapping[str, Any]: ...

    async def get_sim_candidate(
        self, *, owner_id: str, sim_candidate_id: str
    ) -> Mapping[str, Any]: ...

    async def get_active_session(self, *, owner_id: str) -> Mapping[str, Any] | None: ...

    async def list_analysis_history(self, *, owner_id: str) -> list[Mapping[str, Any]]: ...
