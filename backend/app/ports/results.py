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


class ReportStorageUnavailable(ResultQueryError):
    """The private report object store cannot safely serve a PDF."""


@dataclass(frozen=True, slots=True)
class ReadyReportArtifact:
    """Server-only location of the latest downloadable report artifact.

    This is deliberately not a public response model. The API route passes
    it straight to private Storage and never serializes its bucket or object
    key to a browser.
    """

    storage_bucket: str
    storage_object_key: str
    content_sha256: str
    size_bytes: int | None


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
    next_after: tuple[datetime, str] | None = None

@runtime_checkable
class ResultRepository(Protocol):
    """Curated, owner-scoped read operations exposed to HTTP routes."""

    async def get_analysis_case(
        self, *, owner_id: str, analysis_case_id: str
    ) -> Mapping[str, Any]: ...

    async def get_sim_candidate(
        self, *, owner_id: str, sim_candidate_id: str
    ) -> Mapping[str, Any]: ...

    async def get_ready_report_artifact(
        self, *, owner_id: str, analysis_case_id: str
    ) -> ReadyReportArtifact:
        """Return only the owner's unexpired latest artifact when it is ready.

        Absence, foreign ownership, expired retention, and a latest artifact
        that is still generating/failed intentionally raise ResultNotFound so
        the HTTP boundary does not disclose state.
        """
        ...

    async def get_report_status(
        self, *, owner_id: str, analysis_case_id: str
    ) -> Mapping[str, Any]:
        """Return the lightweight public PDF lifecycle state for one owned case.

        This query must not load the analysis result body. Absence, foreign
        ownership, and expired retention all raise :class:`ResultNotFound`.
        """
        ...

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



@runtime_checkable
class ReportObjectStorage(Protocol):
    """Server-only bounded reads from the private report bucket."""

    async def get(
        self,
        *,
        bucket: str,
        object_key: str,
        max_bytes: int,
    ) -> bytes: ...
