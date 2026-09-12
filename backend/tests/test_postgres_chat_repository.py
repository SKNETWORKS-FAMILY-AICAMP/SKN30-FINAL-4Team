from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from unittest.mock import ANY
from uuid import UUID, uuid4

import pytest

from worker.postgres_chat_repository import (
    ChatWorkerDatabaseUnavailable,
    ChatWorkerQueueContractError,
    PostgresChatJobRepository,
)
from worker.runtime import JobFailure


@dataclass
class FakeCursor:
    rows: list[Mapping[str, Any] | None]
    calls: list[tuple[str, tuple[object, ...]]] = field(default_factory=list)

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        return None

    def execute(self, query: str, params: tuple[object, ...]) -> None:
        self.calls.append((query, params))

    def fetchone(self) -> Mapping[str, Any] | None:
        return self.rows.pop(0)


@dataclass
class FakeConnection:
    rows: list[Mapping[str, Any] | None]
    cursor_instance: FakeCursor = field(init=False)

    def __post_init__(self) -> None:
        self.cursor_instance = FakeCursor(self.rows)

    def __enter__(self) -> "FakeConnection":
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        return None

    def cursor(self) -> FakeCursor:
        return self.cursor_instance


@dataclass
class FakeConnect:
    row_sets: list[list[Mapping[str, Any] | None]]
    connections: list[FakeConnection] = field(default_factory=list)
    calls: list[tuple[str, dict[str, object]]] = field(default_factory=list)

    def __call__(self, database_url: str, **kwargs: object) -> FakeConnection:
        self.calls.append((database_url, dict(kwargs)))
        connection = FakeConnection(self.row_sets.pop(0))
        self.connections.append(connection)
        return connection


def _repository(
    *row_sets: list[Mapping[str, Any] | None],
) -> tuple[PostgresChatJobRepository, FakeConnect]:
    factory = FakeConnect(list(row_sets))
    return (
        PostgresChatJobRepository(
            "postgresql://worker:do-not-log@database.example:5432/postgres",
            connect_timeout_seconds=7,
            connect=factory,
        ),
        factory,
    )


def _claim_row() -> dict[str, object]:
    return {
        "assistant_message_id": uuid4(),
        "analysis_case_id": uuid4(),
        "analysis_session_id": uuid4(),
        "user_message_id": uuid4(),
        "owner_id": uuid4(),
        "question": "예측금액이 얼마야?",
        "result_payload": {
            "ml": {
                "model_2": {
                    "status": "OK",
                    "predicted_amount_won": 1000000,
                }
            }
        },
        "conversation": [
            {"role": "user", "content": "이 사업은 어떤 유형이야?"},
            {"role": "assistant", "content": "지원유형 결과를 확인했습니다."},
        ],
        "processing_run_pk": uuid4(),
        "attempt_count": 1,
        "lease_expires_at": datetime(2026, 9, 10, tzinfo=timezone.utc),
        "heartbeat_interval_seconds": 30,
    }


def test_claim_maps_migration_26_payload_for_storage_free_handler() -> None:
    row = _claim_row()
    repository, factory = _repository([row])

    job = repository.claim(worker_id="chat-worker-a", lease_seconds=120)

    assert job is not None
    assert job.job_pk == row["assistant_message_id"]
    assert job.processing_run_pk == row["processing_run_pk"]
    assert job.payload["question"] == "예측금액이 얼마야?"
    assert job.payload["result_payload"] == row["result_payload"]
    assert job.payload["conversation"] == row["conversation"]
    assert factory.calls == [
        (
            "postgresql://worker:do-not-log@database.example:5432/postgres",
            {"connect_timeout": 7, "row_factory": ANY},
        )
    ]
    query, params = factory.connections[0].cursor_instance.calls[0]
    assert "workspace.claim_next_conversation_message" in query
    assert params == ("chat-worker-a", 120)


def test_complete_passes_only_content_and_uuid_evidence_references() -> None:
    message_id = uuid4()
    processing_id = uuid4()
    evidence_id = uuid4()
    repository, factory = _repository([{"is_completed": True}])

    assert repository.complete(
        job_pk=message_id,
        processing_run_pk=processing_id,
        worker_id="chat-worker-a",
        result={
            "content": "확인된 결과입니다.",
            "intent": "MODEL_2",
            "warnings": ["ignored"],
            "prompt_version": "chat-v0.1",
            "references": [
                {"evidence_id": str(evidence_id)},
                {"evidence_id": "not-a-uuid"},
                {"evidence_id": None},
            ],
        },
    )
    _query, params = factory.connections[0].cursor_instance.calls[0]
    assert params[:2] == (message_id, processing_id)
    assert params[2] == "확인된 결과입니다."
    assert params[3] == [evidence_id]


def test_fenced_transitions_and_failure_redact_internal_error() -> None:
    message_id = uuid4()
    processing_id = uuid4()
    repository, factory = _repository(
        [{"is_live": True}],
        [{"is_failed": True}],
    )

    assert repository.heartbeat(
        job_pk=message_id,
        processing_run_pk=processing_id,
        worker_id="chat-worker-a",
        lease_seconds=120,
    )
    assert repository.fail(
        job_pk=message_id,
        processing_run_pk=processing_id,
        worker_id="chat-worker-a",
        failure=JobFailure(
            kind="RuntimeError",
            message="postgresql://worker:secret@db/api_key=another-secret",
        ),
    )
    heartbeat_query, heartbeat_params = factory.connections[0].cursor_instance.calls[0]
    fail_query, fail_params = factory.connections[1].cursor_instance.calls[0]
    assert "heartbeat_conversation_message" in heartbeat_query
    assert "fail_conversation_message" in fail_query
    assert heartbeat_params[:2] == fail_params[:2] == (message_id, processing_id)
    assert fail_params[2:4] == (
        "CHAT_LLM_FAILED",
        "답변을 생성하지 못했습니다. 잠시 후 다시 시도해 주세요.",
    )
    assert "secret" not in fail_params[4]
    assert "another-secret" not in fail_params[4]


def test_invalid_result_is_rejected_before_database_call() -> None:
    repository, factory = _repository()
    with pytest.raises(ChatWorkerQueueContractError, match="content"):
        repository.complete(
            job_pk=uuid4(),
            processing_run_pk=uuid4(),
            worker_id="chat-worker-a",
            result={"warnings": []},
        )
    assert factory.calls == []


def test_database_error_has_safe_text_and_repr() -> None:
    def broken_connect(*_args: object, **_kwargs: object) -> FakeConnection:
        raise OSError("could not connect postgresql://worker:secret@db/postgres")

    repository = PostgresChatJobRepository(
        "postgresql://worker:do-not-log@database.example:5432/postgres",
        connect=broken_connect,
    )
    with pytest.raises(ChatWorkerDatabaseUnavailable) as error:
        repository.claim(worker_id="chat-worker-a", lease_seconds=120)
    assert str(error.value) == "Chat worker database is unavailable"
    assert "secret" not in repr(error.value)
    assert "do-not-log" not in repr(repository)
