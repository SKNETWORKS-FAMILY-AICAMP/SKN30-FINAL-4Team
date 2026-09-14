"""Unit tests for the FastAPI chat admission adapter boundary."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Self
from uuid import uuid4

import psycopg

from app.api.v1.conversations import _unavailable
from app.infrastructure import postgres_conversations
from app.infrastructure.postgres_conversations import (
    PostgresConversationRepository,
    _map_database_error,
)
from app.ports.conversations import ConversationQueueCapacityExceeded


DATABASE_URL = "postgresql://api:do-not-log@database.example:5432/postgres"
OWNER_ID = "11111111-1111-1111-1111-111111111111"
CASE_ID = "22222222-2222-2222-2222-222222222222"
USER_MESSAGE_ID = "33333333-3333-3333-3333-333333333333"
ASSISTANT_MESSAGE_ID = "44444444-4444-4444-4444-444444444444"
SESSION_ID = "55555555-5555-5555-5555-555555555555"


@dataclass
class FakeCursor:
    row: dict[str, Any]
    calls: list[tuple[str, tuple[object, ...]]] = field(default_factory=list)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_values: object) -> None:
        return None

    async def execute(self, query: str, params: tuple[object, ...]) -> None:
        self.calls.append((query, params))

    async def fetchone(self) -> dict[str, Any]:
        return self.row


class AsyncTransaction:
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_values: object) -> None:
        return None


@dataclass
class FakeConnection:
    row: dict[str, Any]
    cursor_instance: FakeCursor = field(init=False)

    def __post_init__(self) -> None:
        self.cursor_instance = FakeCursor(self.row)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_values: object) -> None:
        return None

    def cursor(self) -> FakeCursor:
        return self.cursor_instance

    def transaction(self) -> AsyncTransaction:
        return AsyncTransaction()


@dataclass
class FakeConnect:
    connections: list[FakeConnection]

    async def connect(self, _database_url: str, **_kwargs: object) -> FakeConnection:
        return self.connections.pop(0)


def _install_connect(monkeypatch, *rows: dict[str, Any]) -> list[FakeConnection]:
    connections = [FakeConnection(row) for row in rows]
    factory = FakeConnect(list(connections))

    class AsyncConnection:
        connect = staticmethod(factory.connect)

    monkeypatch.setattr(postgres_conversations.psycopg, "AsyncConnection", AsyncConnection)
    return connections


def test_create_and_retry_set_the_same_transaction_local_queue_limit(monkeypatch) -> None:
    key = str(uuid4())
    retry_key = str(uuid4())
    create_row = {
        "user_message_id": USER_MESSAGE_ID,
        "assistant_message_id": ASSISTANT_MESSAGE_ID,
        "analysis_session_id": SESSION_ID,
        "replayed": False,
    }
    retry_row = {
        **create_row,
        "analysis_case_id": CASE_ID,
        "retry_count": 1,
        "replayed": False,
    }
    create_connection, retry_connection = _install_connect(
        monkeypatch, create_row, retry_row
    )
    repository = PostgresConversationRepository(DATABASE_URL, global_queue_max=3)

    created = asyncio.run(
        repository.create_message(
            owner_id=OWNER_ID,
            analysis_case_id=CASE_ID,
            content="질문",
            idempotency_key=key,
        )
    )
    retried = asyncio.run(
        repository.retry_message(
            owner_id=OWNER_ID,
            analysis_case_id=CASE_ID,
            assistant_message_id=ASSISTANT_MESSAGE_ID,
            idempotency_key=retry_key,
        )
    )

    assert created.assistant_message_id == ASSISTANT_MESSAGE_ID
    assert retried.retry_count == 1
    for connection, rpc_name in (
        (create_connection, "prepare_conversation_messages_v2"),
        (retry_connection, "retry_conversation_message_v2"),
    ):
        assert "set_config" in connection.cursor_instance.calls[0][0]
        assert connection.cursor_instance.calls[0][1] == ("3",)
        assert rpc_name in connection.cursor_instance.calls[1][0]


def test_global_queue_capacity_maps_to_the_safe_retryable_error() -> None:
    mapped = _map_database_error(
        psycopg.OperationalError("GLOBAL_QUEUE_CAPACITY_EXCEEDED")
    )
    assert isinstance(mapped, ConversationQueueCapacityExceeded)
    response = _unavailable(mapped)
    assert response.status_code == 503
    assert response.code == "SERVICE_UNAVAILABLE"
