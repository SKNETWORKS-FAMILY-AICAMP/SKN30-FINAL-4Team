"""Trusted PostgreSQL adapter for owner-scoped analysis-result reads.

FastAPI first validates the Supabase Auth cookie.  This adapter then creates a
short-lived PostgreSQL connection and sets only the verified UUID as the local
``request.jwt.claim.sub`` value.  The existing ``api`` views and RPCs continue
to own every result-table join and ownership predicate through ``auth.uid()``.

DB RPC assumptions (v0.2, section 13/16 ``db-lifecycle``/``db-result-retrieval``)
-----------------------------------------------------------------------------
The functions below are named for the v0.2 contract but have not shipped on
this branch; the DB migration work is a parallel commit unit. Until that
lands, any online (non-offline-mode) call into this adapter will fail with a
plain ``undefined_function``/``42883`` error, which this adapter maps to
:class:`ResultRepositoryUnavailable` like any other database failure — it
never fabricates an empty or fake-success result.  The assumed signatures,
all executed on the owner-scoped connection (``SET LOCAL ROLE authenticated``
+ ``request.jwt.claim.sub``) exactly like the existing v1 RPCs below:

- ``api.rpc_get_analysis_result_v2(p_analysis_case_id uuid) -> jsonb`` —
  same ``payload`` shape as the legacy ``rpc_get_analysis_result``, extended
  with typed CPL/FIT ``detail`` and SIM section status/reason/summary.
- ``api.rpc_get_sim_candidate_detail_v2(p_sim_candidate_id uuid) -> jsonb`` —
  returns the section-9.2 shape (``metadata``/``comparison``/``axes``)
  instead of the legacy flat/raw-axis shape.
- ``api.rpc_get_analysis_current_v2() -> jsonb`` — no arguments; owner comes
  from the connection's ``auth.uid()``. Returns the section-5.4 discriminated
  ``{"state": "processing"|"ready"|"idle", "run": ..., "session": ...}``
  snapshot computed atomically (one statement, one point-in-time read).
- ``api.rpc_close_analysis_session_v2(p_analysis_session_id uuid) -> void`` —
  closes the session (``closed_at``, ``reason='new_analysis'``) if it is the
  caller's and still active; is a silent no-op if it is the caller's and
  already closed/expired; raises SQLSTATE ``P0002`` if it does not exist or
  belongs to another owner.
- ``api.rpc_list_analysis_history_v2(p_snapshot_at timestamptz,
  p_after_completed_at timestamptz, p_after_analysis_case_id uuid, p_limit
  int) -> jsonb`` — returns ``{"snapshot_at": <timestamptz>, "items": [...]}``.
  When ``p_snapshot_at`` is NULL the function picks and returns the pinned
  snapshot (the reference point later pages must keep re-sending); passing a
  previously-returned ``snapshot_at`` back must not change what "now" means
  for that pagination run. ``items`` are ordered
  ``(completed_at DESC, analysis_case_id DESC)`` and exclude any
  active/unexpired result session per spec section 6.
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


_SET_OWNER_SQL = "SELECT set_config('request.jwt.claim.sub', %s, true)"
_SET_AUTHENTICATED_ROLE_SQL = "SET LOCAL ROLE authenticated"
_GET_CASE_SQL = "SELECT api.rpc_get_analysis_result_v2(%s) AS payload"
_GET_SIM_CANDIDATE_SQL = "SELECT api.rpc_get_sim_candidate_detail_v2(%s) AS payload"
_GET_CURRENT_SQL = "SELECT api.rpc_get_analysis_current_v2() AS payload"
_CLOSE_SESSION_SQL = "SELECT api.rpc_close_analysis_session_v2(%s)"
_GET_ACTIVE_SESSION_SQL = """
SELECT analysis_session_id, analysis_case_id, program_name, original_filename,
       session_expires_at
  FROM api.v_active_analysis_session
 LIMIT 1
"""
_LIST_HISTORY_PAGE_SQL = """
SELECT api.rpc_list_analysis_history_v2(%s, %s, %s, %s) AS payload
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
        return await self._one_with_owner(
            owner_id=owner_id,
            query=_GET_ACTIVE_SESSION_SQL,
            params=(),
        )

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
            params=(snapshot_at, after_completed_at, after_case_id, limit),
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
        return AnalysisHistoryPage(rows=[dict(item) for item in items], snapshot_at=resolved_snapshot_at)

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
                        await cursor.execute(_SET_OWNER_SQL, (verified_owner_id,))
                        # The API process uses a privileged DSN for queue
                        # writes, but result reads must execute with the same
                        # role/RLS boundary exercised by browser-facing
                        # Supabase APIs.  The verified UUID remains the only
                        # JWT claim material copied into PostgreSQL.
                        await cursor.execute(_SET_AUTHENTICATED_ROLE_SQL)
                        await cursor.execute(query, params)
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
