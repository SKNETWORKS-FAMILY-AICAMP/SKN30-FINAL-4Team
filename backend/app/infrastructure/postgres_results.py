"""Trusted PostgreSQL adapter for owner-scoped analysis-result reads.

FastAPI first validates the Supabase Auth cookie.  This adapter then creates a
short-lived PostgreSQL connection and sets only the verified UUID as the local
``request.jwt.claim.sub`` value.  The existing ``api`` views and RPCs continue
to own every result-table join and ownership predicate through ``auth.uid()``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from app.ports.results import (
    ResultNotFound,
    ResultRepositoryUnavailable,
)


_SET_OWNER_SQL = "SELECT set_config('request.jwt.claim.sub', %s, true)"
_SET_AUTHENTICATED_ROLE_SQL = "SET LOCAL ROLE authenticated"
_GET_CASE_SQL = "SELECT api.rpc_get_analysis_result(%s) AS payload"
_GET_SIM_CANDIDATE_SQL = "SELECT api.rpc_get_sim_candidate_detail(%s) AS payload"
_GET_ACTIVE_SESSION_SQL = """
SELECT analysis_session_id, analysis_case_id, program_name, original_filename,
       session_expires_at
  FROM api.v_active_analysis_session
 LIMIT 1
"""
_GET_HISTORY_SQL = """
SELECT analysis_case_id, program_name, original_filename, completed_at,
       report_status, report_completed_at
  FROM api.v_my_analysis_history
 ORDER BY completed_at DESC, analysis_case_id DESC
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

    async def list_analysis_history(self, *, owner_id: str) -> list[Mapping[str, Any]]:
        return await self._many_with_owner(
            owner_id=owner_id,
            query=_GET_HISTORY_SQL,
            params=(),
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

    async def _many_with_owner(
        self,
        *,
        owner_id: str,
        query: str,
        params: tuple[object, ...],
    ) -> list[Mapping[str, Any]]:
        rows = await self._execute_with_owner(owner_id=owner_id, query=query, params=params, one=False)
        assert isinstance(rows, list)
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
