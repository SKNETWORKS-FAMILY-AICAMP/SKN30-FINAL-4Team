"""Trusted PostgreSQL adapter for owner-scoped v0.2 result RPCs.

FastAPI validates the Supabase Auth cookie, then passes only that verified
UUID to service-only SQL functions.  Browser roles cannot execute these RPCs,
and the adapter never switches to ``authenticated`` or relies on
``auth.uid()``; every function applies its own explicit owner predicate.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from app.ports.results import (
    AnalysisHistoryPage,
    ResultNotFound,
    ResultRepositoryUnavailable,
)


_GET_CASE_SQL = "SELECT api.rpc_get_analysis_result_v2(%s, %s) AS payload"
_GET_SIM_CANDIDATE_SQL = "SELECT api.rpc_get_sim_candidate_detail_v2(%s, %s) AS payload"
_GET_CURRENT_SQL = "SELECT api.rpc_get_analysis_current_v2(%s) AS payload"
_CLOSE_SESSION_SQL = "SELECT * FROM api.rpc_close_analysis_session_v2(%s, %s)"
_LIST_HISTORY_PAGE_SQL = """
SELECT api.rpc_get_analysis_history_v2(%s, %s, %s, %s) AS payload
"""


class PostgresResultRepository:
    """Use the stable ``api`` schema rather than directly reading base tables."""

    def __init__(self, database_url: str, *, connect_timeout_seconds: int = 10) -> None:
        self._database_url = database_url
        self._connect_timeout = connect_timeout_seconds

    def _ensure_configured(self) -> None:
        if not self._database_url:
            raise ResultRepositoryUnavailable("Result database is not configured")

    @staticmethod
    def _owner_uuid(owner_id: str) -> str:
        try:
            return str(UUID(owner_id))
        except (TypeError, ValueError, AttributeError) as exc:
            # An Auth user id is always UUID-shaped. Do not place arbitrary
            # development headers into PostgreSQL request claim settings.
            raise ResultRepositoryUnavailable("Authenticated identity is invalid") from exc

    async def get_analysis_case(
        self, *, owner_id: str, analysis_case_id: str
    ) -> Mapping[str, Any]:
        row = await self._one_with_owner(
            owner_id=owner_id,
            query=_GET_CASE_SQL,
            params=(analysis_case_id,),
        )
        payload = row.get("payload") if row is not None else None
        if not isinstance(payload, Mapping):
            raise ResultNotFound("Analysis result not found")
        return dict(payload)

    async def get_sim_candidate(
        self, *, owner_id: str, sim_candidate_id: str
    ) -> Mapping[str, Any]:
        row = await self._one_with_owner(
            owner_id=owner_id,
            query=_GET_SIM_CANDIDATE_SQL,
            params=(sim_candidate_id,),
        )
        payload = row.get("payload") if row is not None else None
        if not isinstance(payload, Mapping):
            raise ResultNotFound("Similarity candidate not found")
        return dict(payload)

    async def get_active_session(self, *, owner_id: str) -> Mapping[str, Any] | None:
        current = await self.get_current(owner_id=owner_id)
        session = current.get("session")
        if current.get("state") != "ready" or not isinstance(session, Mapping):
            return None
        return dict(session)

    async def get_current(self, *, owner_id: str) -> Mapping[str, Any]:
        row = await self._one_with_owner(
            owner_id=owner_id,
            query=_GET_CURRENT_SQL,
            params=(),
        )
        payload = row.get("payload") if row is not None else None
        if not isinstance(payload, Mapping):
            raise ResultRepositoryUnavailable("Current analysis snapshot was not returned")
        return dict(payload)

    async def close_session(self, *, owner_id: str, analysis_session_id: str) -> None:
        await self._one_with_owner(
            owner_id=owner_id,
            query=_CLOSE_SESSION_SQL,
            params=(analysis_session_id,),
        )

    async def list_analysis_history_page(
        self,
        *,
        owner_id: str,
        snapshot_at: datetime | None,
        after: tuple[datetime, str] | None,
        limit: int,
    ) -> AnalysisHistoryPage:
        after_completed_at, after_case_id = after if after is not None else (None, None)
        row = await self._one_with_owner(
            owner_id=owner_id,
            query=_LIST_HISTORY_PAGE_SQL,
            params=(snapshot_at, after_completed_at, after_case_id),
        )
        payload = row.get("payload") if row is not None else None
        if not isinstance(payload, Mapping):
            raise ResultRepositoryUnavailable("Analysis history snapshot was not returned")
        returned_snapshot_at = payload.get("snapshot_at")
        items = payload.get("items")
        if not isinstance(returned_snapshot_at, (str, datetime)) or not isinstance(items, list):
            raise ResultRepositoryUnavailable("Analysis history snapshot was malformed")
        resolved_snapshot_at = (
            returned_snapshot_at
            if isinstance(returned_snapshot_at, datetime)
            else datetime.fromisoformat(returned_snapshot_at)
        )
        next_completed_at = payload.get("next_completed_at")
        next_case_id = payload.get("next_analysis_case_id")
        if (next_completed_at is None) != (next_case_id is None):
            raise ResultRepositoryUnavailable("Analysis history cursor was malformed")
        next_after = None
        if next_completed_at is not None:
            resolved_next_completed_at = (
                next_completed_at
                if isinstance(next_completed_at, datetime)
                else datetime.fromisoformat(str(next_completed_at))
            )
            next_after = (resolved_next_completed_at, str(next_case_id))
        return AnalysisHistoryPage(
            rows=[dict(item) for item in items],
            snapshot_at=resolved_snapshot_at,
            next_after=next_after,
        )

    async def _one_with_owner(
        self,
        *,
        owner_id: str,
        query: str,
        params: tuple[object, ...],
    ) -> Mapping[str, Any] | None:
        rows = await self._execute_with_owner(owner_id=owner_id, query=query, params=params, one=True)
        return rows

    async def _execute_with_owner(
        self,
        *,
        owner_id: str,
        query: str,
        params: tuple[object, ...],
        one: bool,
    ) -> Mapping[str, Any] | None | list[Mapping[str, Any]]:
        self._ensure_configured()
        verified_owner_id = self._owner_uuid(owner_id)
        try:
            async with await psycopg.AsyncConnection.connect(
                self._database_url,
                connect_timeout=self._connect_timeout,
                row_factory=dict_row,
            ) as connection:
                async with connection.transaction():
                    async with connection.cursor() as cursor:
                        await cursor.execute(query, (verified_owner_id, *params))
                        if one:
                            row = await cursor.fetchone()
                            return dict(row) if row is not None else None
                        rows = await cursor.fetchall()
                        return [dict(row) for row in rows]
        except psycopg.Error as exc:
            # The api RPCs intentionally raise P0002 for absent or foreign
            # objects. Map both to the same HTTP 404 without disclosing which.
            if getattr(exc, "sqlstate", None) == "P0002":
                raise ResultNotFound("Result not found") from exc
            raise ResultRepositoryUnavailable("Result database is unavailable") from exc
        except OSError as exc:
            raise ResultRepositoryUnavailable("Result database is unavailable") from exc
