"""Read-only result-query boundary for the cookie-authenticated API.

The public API never accepts an owner id.  Each repository method receives
the already authenticated principal only so an adapter can establish the
equivalent Supabase ``auth.uid()`` context before using the curated database
views/RPCs.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, runtime_checkable


class ResultQueryError(RuntimeError):
    """Base class for safe result-read failures."""


class ResultNotFound(ResultQueryError):
    """The result does not exist or is not visible to the authenticated user."""


class ResultRepositoryUnavailable(ResultQueryError):
    """The internal database is unavailable or has not been configured."""


@dataclass(frozen=True, slots=True)
class AnalysisHistoryPage:
    """One page of the owner's analysis history plus the pinned DB snapshot.

    ``snapshot_at`` is chosen by the database on the *first* page (``after``
    is ``None``) and then threaded back through every subsequent page's
    cursor unchanged, so a session that closes/expires or a new result that
    completes mid-pagination never reshuffles a page the caller has not
    fetched yet (spec section 6).
    """

    rows: list[Mapping[str, Any]]
    snapshot_at: datetime


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

    async def get_current(self, *, owner_id: str) -> Mapping[str, Any]:
        """Return the single discriminated processing/ready/idle snapshot."""
        ...

    async def close_session(self, *, owner_id: str, analysis_session_id: str) -> None:
        """Close an owned session; a no-op (not an error) if already closed.

        Raises :class:`ResultNotFound` only when the session does not exist
        or belongs to another owner.
        """
        ...

    async def list_analysis_history_page(
        self,
        *,
        owner_id: str,
        snapshot_at: datetime | None,
        after: tuple[datetime, str] | None,
        limit: int,
    ) -> AnalysisHistoryPage:
        """Return up to ``limit`` rows ordered by ``(completed_at DESC, analysis_case_id DESC)``.

        ``snapshot_at=None`` means "first page": the adapter picks and
        returns the DB snapshot to pin. ``after`` is the ``(completed_at,
        analysis_case_id)`` of the last row already returned, or ``None`` for
        the first page.
        """
        ...
