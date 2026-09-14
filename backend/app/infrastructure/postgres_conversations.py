"""Trusted PostgreSQL adapter for the asynchronous v0.2 conversation RPCs."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

import psycopg
from psycopg import errors
from psycopg.rows import dict_row

from app.ports.conversations import (
    ConversationError,
    ConversationConflict,
    ConversationIdempotencyKeyConflict,
    ConversationMessagePage,
    ConversationMessageRecord,
    ConversationNotFound,
    ConversationQueueCapacityExceeded,
    ConversationRepositoryUnavailable,
    ConversationRetryCooldown,
    ConversationRetryExhausted,
    ConversationTurnRecord,
)


DEFAULT_GLOBAL_QUEUE_MAX = 25
MAX_GLOBAL_QUEUE_MAX = 10_000
_SET_GLOBAL_QUEUE_LIMIT_SQL = """
SELECT set_config('prereview.global_queue_max', %s, TRUE)
"""

_PREPARE_SQL = """
SELECT user_message_id, assistant_message_id, analysis_session_id, replayed
  FROM workspace.prepare_conversation_messages_v2(%s, %s, %s, %s)
"""
_RETRY_SQL = """
SELECT assistant_message_id, user_message_id, analysis_case_id,
       analysis_session_id, retry_count, replayed
  FROM workspace.retry_conversation_message_v2(%s, %s, %s, %s)
"""
_GET_MESSAGE_SQL = """
SELECT api.rpc_get_conversation_message_v2(%s, %s, %s) AS payload
"""
_LIST_PAGE_SQL = """
SELECT api.rpc_get_conversation_history_v2(%s, %s, %s, %s, %s) AS payload
"""


class PostgresConversationRepository:
    """Use service-role RPCs for writes and an owner predicate for reads.

    The API has already validated the cookie principal.  The only identity
    value sent to a mutation RPC is that verified UUID; callers cannot supply
    a database role or an arbitrary SQL fragment.
    """

    def __init__(
        self,
        database_url: str,
        *,
        connect_timeout_seconds: int = 10,
        global_queue_max: int | None = None,
    ) -> None:
        if global_queue_max is not None and (
            isinstance(global_queue_max, bool)
            or not isinstance(global_queue_max, int)
            or not 1 <= global_queue_max <= MAX_GLOBAL_QUEUE_MAX
        ):
            raise ValueError(
                f"global_queue_max must be between 1 and {MAX_GLOBAL_QUEUE_MAX}"
            )
        self._database_url = database_url
        self._connect_timeout = connect_timeout_seconds
        self._global_queue_max = global_queue_max

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
        self,
        *,
        owner_id: str,
        analysis_case_id: str,
        content: str,
        idempotency_key: str,
    ) -> ConversationTurnRecord:
        self._ensure_configured()
        owner = self._uuid(owner_id, label="owner")
        case_id = self._uuid(analysis_case_id, label="case")
        key = self._uuid(idempotency_key, label="idempotency key")
        try:
            async with await psycopg.AsyncConnection.connect(
                self._database_url,
                connect_timeout=self._connect_timeout,
                row_factory=dict_row,
            ) as connection:
                async with connection.transaction():
                    async with connection.cursor() as cursor:
                        if self._global_queue_max is not None:
                            await cursor.execute(
                                _SET_GLOBAL_QUEUE_LIMIT_SQL,
                                (str(self._global_queue_max),),
                            )
                        await cursor.execute(_PREPARE_SQL, (owner, case_id, content, key))
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

    async def get_message(
        self, *, owner_id: str, analysis_case_id: str, message_id: str
    ) -> ConversationMessageRecord:
        self._ensure_configured()
        owner = self._uuid(owner_id, label="owner")
        case_id = self._uuid(analysis_case_id, label="case")
        target = self._uuid(message_id, label="message")
        try:
            async with await psycopg.AsyncConnection.connect(
                self._database_url,
                connect_timeout=self._connect_timeout,
                row_factory=dict_row,
            ) as connection:
                async with connection.transaction():
                    async with connection.cursor() as cursor:
                        await cursor.execute(_GET_MESSAGE_SQL, (owner, case_id, target))
                        row = await cursor.fetchone()
        except (psycopg.Error, OSError) as exc:
            raise _map_database_error(exc) from exc
        payload = row.get("payload") if row is not None else None
        if not isinstance(payload, Mapping):
            raise ConversationNotFound("Conversation message was not found")
        return _message_record(payload)

    async def list_messages(
        self,
        *,
        owner_id: str,
        analysis_case_id: str,
        limit: int = 50,
        cursor: tuple[int, str] | None = None,
    ) -> ConversationMessagePage:
        self._ensure_configured()
        owner = self._uuid(owner_id, label="owner")
        case_id = self._uuid(analysis_case_id, label="case")
        bounded_limit = min(max(limit, 1), 100)
        try:
            async with await psycopg.AsyncConnection.connect(
                self._database_url,
                connect_timeout=self._connect_timeout,
                row_factory=dict_row,
            ) as connection:
                async with connection.transaction():
                    async with connection.cursor() as cursor_obj:
                        before_sequence_no, before_message_id = (
                            cursor if cursor is not None else (None, None)
                        )
                        if before_message_id is not None:
                            before_message_id = self._uuid(
                                before_message_id, label="cursor message"
                            )
                        await cursor_obj.execute(
                            _LIST_PAGE_SQL,
                            (
                                owner,
                                case_id,
                                before_sequence_no,
                                before_message_id,
                                bounded_limit,
                            ),
                        )
                        row = await cursor_obj.fetchone()
        except ConversationError:
            raise
        except (psycopg.Error, OSError) as exc:
            raise _map_database_error(exc) from exc
        payload = row.get("payload") if row is not None else None
        if not isinstance(payload, Mapping) or not isinstance(payload.get("items"), list):
            raise ConversationRepositoryUnavailable(
                "Conversation history payload was malformed"
            )
        items = [_message_record(item) for item in payload["items"]]
        next_sequence_no = payload.get("next_sequence_no")
        next_message_id = payload.get("next_message_id")
        if (next_sequence_no is None) != (next_message_id is None):
            raise ConversationRepositoryUnavailable(
                "Conversation history cursor was malformed"
            )
        next_cursor = (
            (int(next_sequence_no), str(next_message_id))
            if next_sequence_no is not None
            else None
        )
        return ConversationMessagePage(items=items, next_cursor=next_cursor)

    async def retry_message(
        self,
        *,
        owner_id: str,
        analysis_case_id: str,
        assistant_message_id: str,
        idempotency_key: str,
    ) -> ConversationTurnRecord:
        self._ensure_configured()
        owner = self._uuid(owner_id, label="owner")
        case_id = self._uuid(analysis_case_id, label="case")
        assistant_id = self._uuid(assistant_message_id, label="message")
        key = self._uuid(idempotency_key, label="idempotency key")
        try:
            async with await psycopg.AsyncConnection.connect(
                self._database_url,
                connect_timeout=self._connect_timeout,
                row_factory=dict_row,
            ) as connection:
                async with connection.transaction():
                    async with connection.cursor() as cursor:
                        if self._global_queue_max is not None:
                            await cursor.execute(
                                _SET_GLOBAL_QUEUE_LIMIT_SQL,
                                (str(self._global_queue_max),),
                            )
                        await cursor.execute(_RETRY_SQL, (owner, case_id, assistant_id, key))
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
        content=None if row.get("content") is None else str(row["content"]),
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
        if "IDEMPOTENCY_KEY_CONFLICT" in message:
            return ConversationIdempotencyKeyConflict(
                "Idempotency-Key was reused with different input"
            )
        if "GLOBAL_QUEUE_CAPACITY_EXCEEDED" in message:
            return ConversationQueueCapacityExceeded(
                "Conversation queue is temporarily full"
            )
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
