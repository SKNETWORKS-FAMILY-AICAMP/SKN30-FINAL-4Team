"""Offline E2E contract for the real polling-worker orchestration."""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
import json
from pathlib import Path
import subprocess
from typing import Any
from uuid import uuid4

import httpx
import pytest

from worker.analysis_job import (
    AnalysisJobContractError,
    AnalysisJobHandler,
    AnalysisJobUnavailable,
    ArtifactRef,
    CachedRequestProfile,
    EmbeddingConfiguration,
    ExistingCandidate,
    ProducedRequestProfile,
)
from worker.contracts.cpl_result import CplItem, CplResult, CplFieldCode
from worker.contracts.fit_result import (
    FitRelationId,
    FitRelationResult,
    FitResult,
    FitSide,
    FitStatus,
    PurposeAxisClassification,
)
from worker.contracts.sim_result import (
    InternalRanking,
    SimAxis,
    SimAxisResult,
    SimCandidateResult,
    SimComparisonResult,
    SimReviewGrade,
    SimStatus,
)
from worker.ports.embedding import EmbeddingBatch
from worker.result_payload import build_result_payload
from worker.runtime import ClaimedJob
from worker.supabase_storage import SupabaseWorkerStorage


def _json(value: dict[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _profile(run_id: str, source_sha256: str = "a" * 64) -> dict[str, Any]:
    def fact(field: str, value: str, ordinal: int) -> dict[str, Any]:
        return {
            "fact_id": f"fact:{field}",
            "field_name": field,
            "value_raw": value,
            "status": "identified",
            "value_source": {
                "source_block_id": f"block-{ordinal}",
                "start_char": 0,
                "end_char": len(value),
            },
            "evidence": [],
        }

    return {
        "schema_version": "pre_review_request_profile/v0.1",
        "profile_id": f"request:{run_id}",
        "identity": {"title_raw": "테스트 요청 사업"},
        "processing_metadata": {
            "common_ir_document_id": f"hwpx:{run_id}",
            "common_ir_source_sha256": source_sha256,
        },
        "comparison_profile": {
            "purpose_goal": [fact("purpose_goal", "지역 기업의 성장을 지원", 1)],
            "support_target": [fact("support_target", "지역 중소기업", 2)],
            "support_activities": [fact("support_activities", "기술 컨설팅", 3)],
        },
        "support_components": [],
        "request_context": {},
        "field_states": [],
    }


@dataclass
class FakeStorage:
    objects: dict[tuple[str, str], bytes]
    gets: list[tuple[str, str]] = field(default_factory=list)
    puts: list[tuple[str, str]] = field(default_factory=list)
    deletes: list[tuple[str, str]] = field(default_factory=list)

    def get(self, *, bucket: str, object_key: str, max_bytes: int) -> bytes:
        self.gets.append((bucket, object_key))
        value = self.objects[(bucket, object_key)]
        assert len(value) <= max_bytes
        return value

    def put_if_absent(
        self, *, bucket: str, object_key: str, content: bytes, content_type: str
    ) -> bool:
        assert content_type == "application/json"
        self.puts.append((bucket, object_key))
        key = (bucket, object_key)
        if key in self.objects:
            return False
        self.objects[key] = content
        return True

    def delete(self, *, bucket: str, object_key: str) -> None:
        self.deletes.append((bucket, object_key))
        self.objects.pop((bucket, object_key), None)


@dataclass
class FakeStore:
    candidate: ExistingCandidate
    cached: CachedRequestProfile | None = None
    registrations: int = 0
    match_calls: int = 0

    def cached_request_profile(self, *, analysis_run_id: str) -> CachedRequestProfile | None:
        return self.cached

    def register_request_profile(self, **values: Any) -> None:
        self.registrations += 1
        assert values["processing_run_id"]
        assert values["profile"]["schema_version"] == "pre_review_request_profile/v0.1"
        self.cached = CachedRequestProfile(
            common_ir=values["common_ir"],
            structured_profile=values["structured_profile"],
        )

    def active_embedding_configuration(self) -> EmbeddingConfiguration:
        return EmbeddingConfiguration(
            configuration_id=str(uuid4()),
            provider="openai",
            model_id="text-embedding-3-small",
            dimensions=2,
            max_input_tokens=8192,
            assembly_version="approved-facts-role-aware-v1",
        )

    def match_existing_profiles(self, **values: Any) -> list[ExistingCandidate]:
        self.match_calls += 1
        assert len(values["purpose"]) == len(values["target"]) == len(values["support"]) == 2
        return [self.candidate]


class FakeProducer:
    def __init__(self) -> None:
        self.calls = 0

    def produce(self, **values: Any) -> ProducedRequestProfile:
        self.calls += 1
        run_id = values["analysis_run_id"]
        source_sha256 = sha256(values["source_path"].read_bytes()).hexdigest()
        profile = _profile(run_id, source_sha256)
        common = {
            "schema_version": "common_ir_v1",
            "document": {
                "document_id": f"hwpx:{run_id}",
                "provenance": {"source_sha256": source_sha256},
            },
        }
        return ProducedRequestProfile(
            common_ir=common,
            profile=profile,
            common_ir_content=_json(common),
            profile_content=_json(profile),
            common_ir_logical_id=f"hwpx:{run_id}",
            profile_logical_id=f"request:{run_id}",
            common_ir_schema="common_ir_v1",
            profile_schema="pre_review_request_profile/v0.1",
        )


class FakeEmbedding:
    def __init__(self) -> None:
        self.calls = 0

    async def embed(self, texts: list[str]) -> EmbeddingBatch:
        self.calls += 1
        assert texts and all(text.strip() for text in texts)
        return EmbeddingBatch(
            model_name="text-embedding-3-small",
            vectors=[[1.0, 1.0] for _ in texts],
        )


class FakeEngine:
    def __init__(self) -> None:
        self.calls = 0

    def build_payload(self, **values: Any) -> dict[str, Any]:
        self.calls += 1
        assert values["profile"]["schema_version"] == "pre_review_request_profile/v0.1"
        assert values["common_ir"]["schema_version"] == "common_ir_v1"
        assert values["candidates"][0].candidate.source_profile_id == "hwp:PBLN_TEST"
        return {"program_name": "테스트 요청 사업", "axes": [], "candidates": [], "evidences": []}


def _fixture() -> tuple[AnalysisJobHandler, FakeStorage, FakeStore, FakeProducer, FakeEmbedding, FakeEngine, ClaimedJob]:
    run_id = str(uuid4())
    processing_id = str(uuid4())
    source = b"fake-hwpx-content"
    source_key = f"{run_id}/source/{sha256(source).hexdigest()}.hwpx"
    existing = {
        "schema_version": "existing_program_profile/v0.2",
        "source_profile_id": "hwp:PBLN_TEST",
        "notice_id": "bizinfo:PBLN_TEST",
        "comparison_profile": {},
    }
    existing_bytes = _json(existing)
    existing_ref = ArtifactRef(
        bucket="existing-kb",
        object_key="PBLN_TEST/structured.json",
        content_sha256=sha256(existing_bytes).hexdigest(),
        artifact_type="structured_profile",
        mime_type="application/json",
        size_bytes=len(existing_bytes),
        schema_version="existing_program_profile/v0.2",
    )
    candidate = ExistingCandidate(
        profile_version_id=str(uuid4()),
        source_profile_id="hwp:PBLN_TEST",
        notice_id="bizinfo:PBLN_TEST",
        average_similarity=0.9,
        purpose_similarity=0.9,
        target_similarity=0.9,
        support_similarity=0.9,
        profile_artifact=existing_ref,
        title="테스트 공고",
    )
    storage = FakeStorage(
        {
            ("request-temp", source_key): source,
            (existing_ref.bucket, existing_ref.object_key): existing_bytes,
        }
    )
    store = FakeStore(candidate)
    producer = FakeProducer()
    embedding = FakeEmbedding()
    engine = FakeEngine()
    handler = AnalysisJobHandler(
        storage=storage,
        store=store,
        producer=producer,
        embedding_client=embedding,
        analysis_engine=engine,
    )
    job = ClaimedJob(
        job_pk=run_id,
        processing_run_pk=processing_id,
        payload={
            "source_bucket": "request-temp",
            "source_object_key": source_key,
            "source_content_sha256": sha256(source).hexdigest(),
        },
    )
    return handler, storage, store, producer, embedding, engine, job


def test_handler_offline_e2e_and_retry_reuses_committed_profile() -> None:
    handler, storage, store, producer, embedding, engine, job = _fixture()

    first = handler.handle(job)
    first_get_count = len(storage.gets)
    second = handler.handle(job)

    assert first == second
    assert producer.calls == 1
    assert store.registrations == 1
    assert store.match_calls == 2
    assert embedding.calls == 6
    assert engine.calls == 2
    # Retry reads the two committed derived artifacts and candidate profile,
    # but does not fetch or parse the source again.
    retry_gets = storage.gets[first_get_count:]
    assert ("request-temp", job.payload["source_object_key"]) not in retry_gets
    assert store.cached is not None
    assert (store.cached.common_ir.bucket, store.cached.common_ir.object_key) in retry_gets
    assert (store.cached.structured_profile.bucket, store.cached.structured_profile.object_key) in retry_gets


def test_source_hash_mismatch_fails_before_pipeline_or_upload() -> None:
    handler, storage, store, producer, _embedding, _engine, job = _fixture()
    storage.objects[("request-temp", job.payload["source_object_key"])] = b"tampered"

    with pytest.raises(AnalysisJobContractError, match="hash"):
        handler.handle(job)

    assert producer.calls == 0
    assert store.registrations == 0
    assert storage.puts == []


def test_failed_registration_keeps_content_addressed_objects_for_a_new_attempt() -> None:
    handler, storage, store, _producer, _embedding, _engine, job = _fixture()

    def reject_registration(**_values: Any) -> None:
        raise RuntimeError("processing attempt was fenced")

    store.register_request_profile = reject_registration  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="fenced"):
        handler.handle(job)

    assert len(storage.puts) == 2
    assert storage.deletes == []
    assert all(key in storage.objects for key in storage.puts)


def test_result_payload_uses_frontend_axis_and_status_vocabulary() -> None:
    profile = {"profile_id": "request:run", "identity": {"title_raw": "사업"}}
    cpl = CplResult(
        items=[CplItem(CplFieldCode.REQUEST_TYPE, "confirmed", None)],
        profile_id="request:run",
        common_ir_document_id="ir:run",
        common_ir_source_sha256="a" * 64,
        candidate_pack_id="pack:run",
        pipeline_version="pipeline",
        structured_schema_version="pre_review_request_profile/v0.1",
        model_id="model",
        prompt_version="prompt",
    )
    fit = FitResult(
        relations=[
            FitRelationResult(
                FitRelationId.FIT_1, FitStatus.FIT, None, FitSide(), FitSide()
            )
        ],
        purpose_axis=PurposeAxisClassification(attempted=False),
        profile_id="request:run",
        common_ir_document_id="ir:run",
        model_profile="model",
        ruleset_version="rules",
        prompt_version="prompt",
    )
    axes = [
        SimAxisResult(axis, f"SIM-{index}", status, None)
        for index, (axis, status) in enumerate(
            (
                (SimAxis.PURPOSE, SimStatus.SIMILAR),
                (SimAxis.TARGET, SimStatus.SIMILAR),
                (SimAxis.CONTENT, SimStatus.PARTIAL),
                (SimAxis.DELIVERY, SimStatus.INSUFFICIENT),
            ),
            1,
        )
    ]
    candidate = SimCandidateResult(
        candidate_profile_id="hwp:PBLN_TEST",
        candidate_notice_id="bizinfo:PBLN_TEST",
        axes=axes,
        internal_ranking=InternalRanking(
            0.85, SimReviewGrade.FOCUS_REVIEW, 3, scoring_version="score"
        ),
    )
    sim = SimComparisonResult(
        request_profile_id="request:run",
        candidates=[candidate],
        model_profile="model",
        ruleset_version="rules",
        prompt_version="prompt",
        scoring_version="score",
    )

    payload = build_result_payload(
        profile=profile,
        cpl=cpl,
        fit=fit,
        sim=sim,
        sim_profiles={},
        retrieval_similarities={"hwp:PBLN_TEST": 0.91},
        profile_version_ids={"hwp:PBLN_TEST": "11111111-1111-1111-1111-111111111111"},
    )

    assert payload["axes"][1]["axis_code"] == "FIT-1"
    result = payload["candidates"][0]
    assert result["profile_version_pk"] == "11111111-1111-1111-1111-111111111111"
    assert result["status"] == "similar"
    assert result["comparable_axes"] == ["purpose", "target", "support"]
    assert result["delivery_result"]["status"] == "insufficient"


def test_freetype_preload_must_be_an_existing_absolute_file(monkeypatch: pytest.MonkeyPatch) -> None:
    from worker.profiles import _subprocess_env

    monkeypatch.setenv("PREREVIEW_FREETYPE_LIB", "relative/libfreetype.so")
    with pytest.raises(RuntimeError, match="absolute"):
        _subprocess_env()

    library = Path("/lib/x86_64-linux-gnu/libfreetype.so.6")
    if library.is_file():
        monkeypatch.setenv("PREREVIEW_FREETYPE_LIB", str(library))
        monkeypatch.setenv("LD_PRELOAD", "/tmp/operator-owned.so")
        value = _subprocess_env()["LD_PRELOAD"]
        assert value.split() == [str(library), "/tmp/operator-owned.so"]


def test_parser_subprocess_does_not_receive_worker_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from worker.profiles import _subprocess_env

    monkeypatch.setenv("OPENAI_API_KEY", "must-not-cross-parser-boundary")
    monkeypatch.setenv("DATABASE_URL", "must-not-cross-parser-boundary")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "must-not-cross-parser-boundary")

    parser_env = _subprocess_env()

    assert "OPENAI_API_KEY" not in parser_env
    assert "DATABASE_URL" not in parser_env
    assert "SUPABASE_SERVICE_ROLE_KEY" not in parser_env


def test_parser_deadline_kills_and_reaps_the_process_group(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from worker.contracts.profile_snapshot import PARSE_FAILED
    from worker.profiles import StageError, parse_to_common_ir

    class HungParser:
        pid = 4242
        returncode: int | None = None
        calls = 0

        def communicate(self, timeout: float | None = None) -> tuple[str, str]:
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired("parser", timeout)
            self.returncode = -9
            return "", ""

    process = HungParser()
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(
        "worker.profiles.os.killpg",
        lambda pid, sig: killed.append((pid, sig)),
    )

    with pytest.raises(StageError) as caught:
        parse_to_common_ir(
            input_path=tmp_path / "request.hwp",
            notice_id="timeout-test",
            source_kind="hwp",
            run_dir=tmp_path / "run",
            timeout_seconds=0.01,
        )

    assert caught.value.diagnostic.reason_code == PARSE_FAILED
    assert "deadline" in caught.value.diagnostic.message
    assert killed == [(process.pid, __import__("signal").SIGKILL)]
    assert process.calls == 2


def test_storage_conflict_is_idempotent_only_when_existing_bytes_match() -> None:
    content = b"derived-json"
    requests: list[httpx.Request] = []

    def matching(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(409)
        return httpx.Response(200, content=content)

    storage = SupabaseWorkerStorage(
        supabase_url="http://supabase.local",
        service_role_key="private-test-key",
        transport=httpx.MockTransport(matching),
    )
    assert not storage.put_if_absent(
        bucket="request-temp",
        object_key="run/common_ir/hash.json",
        content=content,
        content_type="application/json",
    )
    assert [request.method for request in requests] == ["POST", "GET"]
    assert all(request.headers["authorization"] == "Bearer private-test-key" for request in requests)

    def poisoned(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409) if request.method == "POST" else httpx.Response(200, content=b"other")

    storage = SupabaseWorkerStorage(
        supabase_url="http://supabase.local",
        service_role_key="private-test-key",
        transport=httpx.MockTransport(poisoned),
    )
    with pytest.raises(AnalysisJobUnavailable, match="different content"):
        storage.put_if_absent(
            bucket="request-temp",
            object_key="run/common_ir/hash.json",
            content=content,
            content_type="application/json",
        )
