"""Offline transaction/fencing tests for worker profile registration."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import pytest

from worker.analysis_job import AnalysisJobContractError, ArtifactRef
from worker.postgres_analysis_store import PostgresAnalysisStore


@dataclass
class FakeCursor:
    run_id: str
    processing_id: str
    existing_profile: dict[str, Any] | None = None
    artifact_ids: list[Any] = field(default_factory=lambda: [uuid4(), uuid4()])
    current: dict[str, Any] | None = None
    calls: list[tuple[str, tuple[Any, ...]]] = field(default_factory=list)

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *_values: object) -> None:
        return None

    def execute(self, query: str, params: tuple[Any, ...]) -> None:
        self.calls.append((query, params))
        if "lease_expires_at >" in query:
            self.current = {"is_live": 1}
        elif "artifact_type = 'source'" in query:
            self.current = {"artifact_pk": uuid4()}
        elif "INSERT INTO workspace.source_artifact" in query:
            self.current = {"artifact_pk": self.artifact_ids.pop(0)}
        elif "FROM workspace.request_profile profile" in query:
            self.current = self.existing_profile
        elif "workspace.ingest_request_profile_core" in query:
            self.current = {"request_profile_pk": uuid4()}
        else:
            self.current = None

    def fetchone(self) -> dict[str, Any] | None:
        value, self.current = self.current, None
        return value


@dataclass
class FakeConnection:
    cursor_value: FakeCursor
    committed: bool = False

    def __enter__(self) -> "FakeConnection":
        return self

    def __exit__(self, exc_type: object, *_values: object) -> None:
        self.committed = exc_type is None

    def cursor(self) -> FakeCursor:
        return self.cursor_value


def _artifacts(run_id: str) -> tuple[ArtifactRef, ArtifactRef, dict[str, Any]]:
    profile = {
        "schema_version": "pre_review_request_profile/v0.1",
        "profile_id": f"request:{run_id}",
        "processing_metadata": {},
    }
    common = ArtifactRef(
        "request-temp", f"{run_id}/common/a.json", "a" * 64,
        "common_ir", "application/json", 10, "common_ir_v1", run_id,
    )
    structured = ArtifactRef(
        "request-temp", f"{run_id}/profile/b.json", "b" * 64,
        "structured_profile", "application/json", 20,
        "pre_review_request_profile/v0.1", f"request:{run_id}",
    )
    return common, structured, profile


def test_registration_is_fenced_and_materialises_once() -> None:
    run_id, processing_id = str(uuid4()), str(uuid4())
    cursor = FakeCursor(run_id, processing_id)
    connection = FakeConnection(cursor)
    store = PostgresAnalysisStore("postgresql://private", connect=lambda *_a, **_k: connection)
    common, structured, profile = _artifacts(run_id)

    store.register_request_profile(
        analysis_run_id=run_id,
        processing_run_id=processing_id,
        source_bucket="request-temp",
        source_object_key=f"{run_id}/source/input.hwpx",
        common_ir=common,
        structured_profile=structured,
        profile=profile,
    )

    assert connection.committed
    queries = [query for query, _params in cursor.calls]
    assert "lease_expires_at >" in queries[0]
    assert sum("workspace.ingest_request_profile_core" in query for query in queries) == 1
    assert sum("INSERT INTO workspace.artifact_lineage" in query for query in queries) == 2


def test_retry_accepts_only_the_exact_committed_profile_hash_and_skips_ingest() -> None:
    run_id, processing_id = str(uuid4()), str(uuid4())
    common, structured, profile = _artifacts(run_id)
    cursor = FakeCursor(
        run_id,
        processing_id,
        existing_profile={
            "request_profile_pk": uuid4(),
            "profile_id": profile["profile_id"],
            "schema_version": profile["schema_version"],
            "structured_sha256": structured.content_sha256,
        },
    )
    connection = FakeConnection(cursor)
    store = PostgresAnalysisStore("postgresql://private", connect=lambda *_a, **_k: connection)

    store.register_request_profile(
        analysis_run_id=run_id,
        processing_run_id=processing_id,
        source_bucket="request-temp",
        source_object_key=f"{run_id}/source/input.hwpx",
        common_ir=common,
        structured_profile=structured,
        profile=profile,
    )

    assert connection.committed
    assert not any(
        "workspace.ingest_request_profile_core" in query for query, _params in cursor.calls
    )

    cursor = FakeCursor(
        run_id,
        processing_id,
        existing_profile={
            "request_profile_pk": uuid4(),
            "profile_id": profile["profile_id"],
            "schema_version": profile["schema_version"],
            "structured_sha256": "c" * 64,
        },
    )
    store = PostgresAnalysisStore(
        "postgresql://private",
        connect=lambda *_a, **_k: FakeConnection(cursor),
    )
    with pytest.raises(AnalysisJobContractError, match="different"):
        store.register_request_profile(
            analysis_run_id=run_id,
            processing_run_id=processing_id,
            source_bucket="request-temp",
            source_object_key=f"{run_id}/source/input.hwpx",
            common_ir=common,
            structured_profile=structured,
            profile=profile,
        )
