"""Trusted PostgreSQL adapter for the asynchronous conversation API."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg
from psycopg import errors
from psycopg.rows import dict_row

from app.ports.conversations import (
    ConversationError,
    ConversationConflict,
    ConversationMessageRecord,
    ConversationNotFound,
    ConversationRepositoryUnavailable,
    ConversationRetryCooldown,
    ConversationRetryExhausted,
    ConversationTurnRecord,
)


_PREPARE_SQL = """
SELECT user_message_id, assistant_message_id, analysis_session_id
  FROM workspace.prepare_conversation_messages(%s, %s, %s)
"""
_RETRY_SQL = """
SELECT assistant_message_id, user_message_id, analysis_case_id,
       analysis_session_id, retry_count
  FROM workspace.retry_conversation_message(%s, %s, %s)
"""
_LIST_SQL = """
SELECT m.message_pk AS message_id,
       c.analysis_case_pk AS analysis_case_id,
       m.role,
       m.sequence_no,
       m.content,
       m.status,
       m.reply_to_message_pk AS reply_to_message_id,
       (m.auto_retry_count + m.manual_retry_count) AS retry_count,
       m.error_code,
       m.error_message,
       m.created_at,
       m.updated_at
  FROM result.conversation_message m
  JOIN result.analysis_session s
    ON s.analysis_session_pk = m.analysis_session_pk
  JOIN result.analysis_case c
    ON c.analysis_case_pk = s.analysis_case_pk
 WHERE c.analysis_case_pk = %s
       AND c.user_id = %s
       AND c.retention_expires_at > now()
       AND m.updated_at >= COALESCE(%s::timestamptz, '-infinity'::timestamptz)
 ORDER BY m.sequence_no, m.message_pk
 LIMIT %s
"""
_VISIBLE_CASE_SQL = """
SELECT 1
  FROM result.analysis_case c
 WHERE c.analysis_case_pk = %s
   AND c.user_id = %s
   AND c.retention_expires_at > now()
"""


class PostgresConversationRepository:
    """Use service-role RPCs for writes and an owner predicate for reads.

    The API has already validated the cookie principal.  The only identity
    value sent to a mutation RPC is that verified UUID; callers cannot supply
    a database role or an arbitrary SQL fragment.
    """

    def __init__(self, database_url: str, *, connect_timeout_seconds: int = 10) -> None:
        self._database_url = database_url
        self._connect_timeout = connect_timeout_seconds

    def _ensure_configured(self) -> None:
        if not self._database_url:
            raise ConversationRepositoryUnavailable("Conversation database is not configured")

    @staticmethod
    def _uuid(value: str, *, label: str) -> str:
        try:
            return str(UUID(value))
        except (TypeError, ValueError, AttributeError) as exc:
            raise ConversationNotFound(f"Conversation {label} was not found") from exc

    async def create_message(
        self, *, owner_id: str, analysis_case_id: str, content: str
    ) -> ConversationTurnRecord:
        self._ensure_configured()
        owner = self._uuid(owner_id, label="owner")
        case_id = self._uuid(analysis_case_id, label="case")
        try:
            async with await psycopg.AsyncConnection.connect(
                self._database_url,
                connect_timeout=self._connect_timeout,
                row_factory=dict_row,
            ) as connection:
                async with connection.transaction():
                    async with connection.cursor() as cursor:
                        await cursor.execute(_PREPARE_SQL, (owner, case_id, content))
                        row = await cursor.fetchone()
            if row is None:
                raise ConversationRepositoryUnavailable(
                    "Conversation create returned no message ids"
                )
            return ConversationTurnRecord(
                user_message_id=str(row["user_message_id"]),
                assistant_message_id=str(row["assistant_message_id"]),
                analysis_session_id=str(row["analysis_session_id"]),
                status="generating",
            )
        except ConversationError:
            raise
        except (psycopg.Error, OSError) as exc:
            raise _map_database_error(exc) from exc

    async def list_messages(
        self,
        *,
        owner_id: str,
        analysis_case_id: str,
        limit: int = 50,
        updated_since: datetime | None = None,
    ) -> list[ConversationMessageRecord]:
        self._ensure_configured()
        owner = self._uuid(owner_id, label="owner")
        case_id = self._uuid(analysis_case_id, label="case")
        try:
            async with await psycopg.AsyncConnection.connect(
                self._database_url,
                connect_timeout=self._connect_timeout,
                row_factory=dict_row,
            ) as connection:
                async with connection.transaction():
                    async with connection.cursor() as cursor:
                        await cursor.execute(
                            _LIST_SQL,
                            (case_id, owner, updated_since, min(max(limit, 1), 100)),
                        )
                        rows = await cursor.fetchall()
                        if not rows:
                            await cursor.execute(_VISIBLE_CASE_SQL, (case_id, owner))
                            if await cursor.fetchone() is None:
                                raise ConversationNotFound("Conversation was not found")
            return [_message_record(row) for row in rows]
        except (psycopg.Error, OSError) as exc:
            raise _map_database_error(exc) from exc

    async def retry_message(
        self, *, owner_id: str, analysis_case_id: str, assistant_message_id: str
    ) -> ConversationTurnRecord:
        self._ensure_configured()
        owner = self._uuid(owner_id, label="owner")
        case_id = self._uuid(analysis_case_id, label="case")
        assistant_id = self._uuid(assistant_message_id, label="message")
        try:
            async with await psycopg.AsyncConnection.connect(
                self._database_url,
                connect_timeout=self._connect_timeout,
                row_factory=dict_row,
            ) as connection:
                async with connection.transaction():
                    async with connection.cursor() as cursor:
                        await cursor.execute(_RETRY_SQL, (owner, assistant_id, case_id))
                        row = await cursor.fetchone()
            if row is None:
                raise ConversationNotFound("Conversation message was not found")
            returned_case_id = str(row["analysis_case_id"])
            if returned_case_id != case_id:
                # The SQL function also fences by case id.  Keep this second
                # check in the adapter so a future RPC cannot widen the route.
                raise ConversationNotFound("Conversation message was not found")
            return ConversationTurnRecord(
                user_message_id=str(row["user_message_id"]),
                assistant_message_id=str(row["assistant_message_id"]),
                analysis_session_id=str(row["analysis_session_id"]),
                status="generating",
                retry_count=int(row.get("retry_count") or 0),
            )
        except ConversationError:
            raise
        except (psycopg.Error, OSError) as exc:
            raise _map_database_error(exc) from exc


def _message_record(row: Mapping[str, Any]) -> ConversationMessageRecord:
    return ConversationMessageRecord(
        message_id=str(row["message_id"]),
        analysis_case_id=str(row["analysis_case_id"]),
        role=str(row["role"]),
        sequence_no=int(row["sequence_no"]),
        content=(
            None
            if str(row["status"]) == "generating" and row["content"] == ""
            else str(row["content"])
        ),
        status=str(row["status"]),
        reply_to_message_id=(
            str(row["reply_to_message_id"])
            if row.get("reply_to_message_id") is not None
            else None
        ),
        retry_count=int(row.get("retry_count") or 0),
        error_code=row.get("error_code"),
        error_message=row.get("error_message"),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _database_error_message(exc: psycopg.Error) -> str:
    diagnostic = getattr(exc, "diag", None)
    primary = getattr(diagnostic, "message_primary", None)
    return str(primary or exc).strip()


def _map_database_error(exc: BaseException) -> ConversationError:
    if isinstance(exc, psycopg.Error):
        sqlstate = getattr(exc, "sqlstate", None)
        message = _database_error_message(exc)
        if sqlstate == "P0002" or any(
            token in message
            for token in (
                "ANALYSIS_SESSION_EXPIRED",
                "MESSAGE_NOT_FOUND",
                "CASE_NOT_FOUND",
            )
        ):
            return ConversationNotFound("Conversation was not found")
        if "CHAT_RETRY_EXHAUSTED" in message:
            return ConversationRetryExhausted("Conversation retry budget is exhausted")
        if "CHAT_RETRY_COOLDOWN" in message:
            return ConversationRetryCooldown("Conversation retry is temporarily unavailable")
        if "CHAT_MESSAGE_BUSY" in message or "CHAT_MESSAGE_NOT_RETRYABLE" in message:
            return ConversationConflict("Conversation message cannot be retried")
        if sqlstate == "55P03":
            return ConversationRetryCooldown("Conversation retry is temporarily unavailable")
        if sqlstate == "22023":
            return ConversationConflict("Conversation request is invalid")
        if isinstance(exc, errors.UniqueViolation):
            return ConversationConflict("Conversation request conflicts with an existing message")
    return ConversationRepositoryUnavailable("Conversation database is temporarily unavailable")


__all__ = ["PostgresConversationRepository"]
