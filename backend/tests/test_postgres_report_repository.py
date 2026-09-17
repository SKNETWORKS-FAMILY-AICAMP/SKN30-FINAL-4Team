from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Self
from unittest.mock import ANY
from uuid import uuid4

import pytest
from worker.postgres_report_repository import (
    PostgresReportJobRepository,
    ReportWorkerDatabaseUnavailable,
    ReportWorkerQueueContractError,
)
from worker.runtime import JobFailure


@dataclass
class FakeCursor:
    rows: list[Mapping[str, Any] | None]
    calls: list[tuple[str, tuple[object, ...]]] = field(default_factory=list)

    def __enter__(self) -> Self:
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

    def __enter__(self) -> Self:
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
) -> tuple[PostgresReportJobRepository, FakeConnect]:
    factory = FakeConnect(list(row_sets))
    return (
        PostgresReportJobRepository(
            "postgresql://worker:do-not-log@database.example:5432/postgres",
            connect_timeout_seconds=7,
            connect=factory,
        ),
        factory,
    )


def _claim_row() -> dict[str, object]:
    return {
        "report_artifact_id": uuid4(),
        "analysis_case_id": uuid4(),
        "owner_id": uuid4(),
        "source_analysis_run_id": uuid4(),
        "result_payload": {
            "case": {"analysis_case_id": str(uuid4())},
            "cpl": {"items": []},
            "fit": {"items": []},
            "sim": {"candidates": []},
        },
        "sim_details": [
            {"sim_candidate_id": str(uuid4()), "comparison": {"status": "partial"}}
        ],
        "processing_run_pk": uuid4(),
        "storage_object_key": "owner/case/pdf/processing-run.pdf",
        "attempt_count": 1,
        "lease_expires_at": datetime(2026, 9, 16, tzinfo=timezone.utc),
        "heartbeat_interval_seconds": 30,
    }


def test_claim_maps_public_projection_sim_details_and_source_run() -> None:
    row = _claim_row()
    repository, factory = _repository([row])

    job = repository.claim(worker_id="report-worker-a", lease_seconds=120)

    assert job is not None
    assert job.job_pk == row["report_artifact_id"]
    assert job.processing_run_pk == row["processing_run_pk"]
    assert job.payload["analysis_case_id"] == row["analysis_case_id"]
    assert job.payload["owner_id"] == row["owner_id"]
    assert job.payload["source_analysis_run_id"] == row["source_analysis_run_id"]
    assert job.payload["result_payload"] == row["result_payload"]
    assert job.payload["sim_details"] == row["sim_details"]
    assert factory.calls == [
        (
            "postgresql://worker:do-not-log@database.example:5432/postgres",
            {"connect_timeout": 7, "row_factory": ANY},
        )
    ]
    query, params = factory.connections[0].cursor_instance.calls[0]
    assert "workspace.claim_next_pdf_report_v1" in query
    assert params == ("report-worker-a", 120)


def test_complete_passes_only_immutable_pdf_artifact_metadata() -> None:
    report_id = uuid4()
    processing_id = uuid4()
    repository, factory = _repository([{"is_completed": True}])

    assert repository.complete(
        job_pk=report_id,
        processing_run_pk=processing_id,
        worker_id="report-worker-a",
        result={
            "storage_bucket": "analysis-reports",
            "storage_object_key": "case/pdf/" + "a" * 64 + ".pdf",
            "content_sha256": "a" * 64,
            "mime_type": "application/pdf",
            "size_bytes": 1234,
        },
    )
    query, params = factory.connections[0].cursor_instance.calls[0]
    assert "workspace.complete_pdf_report_v1" in query
    assert params == (
        report_id,
        processing_id,
        "analysis-reports",
        "case/pdf/" + "a" * 64 + ".pdf",
        "a" * 64,
        "application/pdf",
        1234,
    )


def test_fenced_transitions_and_failure_redact_internal_error() -> None:
    report_id = uuid4()
    processing_id = uuid4()
    repository, factory = _repository(
        [{"is_live": True}],
        [{"is_failed": True}],
    )

    assert repository.heartbeat(
        job_pk=report_id,
        processing_run_pk=processing_id,
        worker_id="report-worker-a",
        lease_seconds=120,
    )
    assert repository.fail(
        job_pk=report_id,
        processing_run_pk=processing_id,
        worker_id="report-worker-a",
        failure=JobFailure(
            kind="RuntimeError",
            message="postgresql://worker:secret@db/api_key=another-secret",
        ),
    )
    heartbeat_query, heartbeat_params = factory.connections[0].cursor_instance.calls[0]
    fail_query, fail_params = factory.connections[1].cursor_instance.calls[0]
    assert "heartbeat_pdf_report_v1" in heartbeat_query
    assert "fail_pdf_report_v1" in fail_query
    assert heartbeat_params[:2] == fail_params[:2] == (report_id, processing_id)
    assert fail_params[2:4] == (
        "PDF_REPORT_FAILED",
        "PDF 보고서를 생성하지 못했습니다. 잠시 후 다시 시도해 주세요.",
    )
    assert "secret" not in fail_params[4]
    assert "another-secret" not in fail_params[4]


def test_invalid_artifact_is_rejected_before_database_call() -> None:
    repository, factory = _repository()
    with pytest.raises(ReportWorkerQueueContractError, match="shape"):
        repository.complete(
            job_pk=uuid4(),
            processing_run_pk=uuid4(),
            worker_id="report-worker-a",
            result={"storage_bucket": "analysis-reports"},
        )
    assert factory.calls == []


def test_database_error_has_safe_text_and_repr() -> None:
    def broken_connect(*_args: object, **_kwargs: object) -> FakeConnection:
        raise OSError("could not connect postgresql://worker:secret@db/postgres")

    repository = PostgresReportJobRepository(
        "postgresql://worker:do-not-log@database.example:5432/postgres",
        connect=broken_connect,
    )
    with pytest.raises(ReportWorkerDatabaseUnavailable) as error:
        repository.claim(worker_id="report-worker-a", lease_seconds=120)
    assert str(error.value) == "Report worker database is unavailable"
    assert "secret" not in repr(error.value)
    assert "do-not-log" not in repr(repository)


def test_cleanup_deletes_only_the_claimed_object_then_completes_the_fence() -> None:
    cleanup_id = uuid4()
    cleanup_run_id = uuid4()
    repository, factory = _repository(
        [{
            "report_pdf_object_cleanup_id": cleanup_id,
            "storage_bucket": "analysis-reports",
            "storage_object_key": "owner/case/pdf/attempt.pdf",
            "cleanup_run_pk": cleanup_run_id,
        }],
        [{"is_completed": True}],
    )

    class Storage:
        deletes: list[tuple[str, str]] = []

        def delete(self, *, bucket: str, object_key: str) -> None:
            self.deletes.append((bucket, object_key))

    storage = Storage()
    assert repository.cleanup_once(
        storage=storage, worker_id="report-worker-a", lease_seconds=120
    )
    assert storage.deletes == [
        ("analysis-reports", "owner/case/pdf/attempt.pdf")
    ]
    claim_query, claim_params = factory.connections[0].cursor_instance.calls[0]
    complete_query, complete_params = factory.connections[1].cursor_instance.calls[0]
    assert "claim_next_pdf_report_cleanup_v1" in claim_query
    assert claim_params == ("report-worker-a", 120)
    assert "complete_pdf_report_cleanup_v1" in complete_query
    assert complete_params == (cleanup_id, cleanup_run_id)
