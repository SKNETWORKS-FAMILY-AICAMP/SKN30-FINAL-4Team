"""Offline E2E contract for the real polling-worker orchestration."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field, replace
from hashlib import sha256
from pathlib import Path
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
    CoreAnalysisEngine,
    EmbeddingConfiguration,
    ExistingCandidate,
    ProducedRequestProfile,
    ExistingProfileDocument,
    RetrievalDecision,
    VendoredRequestProfileProducer,
)
from worker.contracts.cpl_result import CplFieldCode, CplItem, CplResult
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
from worker.ports.llm import LLMInvalidResponseError, LLMTimeoutError, LLMUnavailableError
from worker.contracts.profile_snapshot import CommonIrArtifact
from worker import analysis_job as analysis_job_module


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
    config_calls: int = 0
    embedding_provenance: list[dict[str, object]] = field(default_factory=list)
    matched_vectors: list[dict[str, list[float]]] = field(default_factory=list)

    def cached_request_profile(
        self, *, analysis_run_id: str
    ) -> CachedRequestProfile | None:
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
        self.config_calls += 1
        return EmbeddingConfiguration(
            configuration_id=str(uuid4()),
            provider="openai",
            model_id="text-embedding-3-small",
            dimensions=2,
            max_input_tokens=8192,
            assembly_version="approved-facts-components-role-aware-v2",
        )

    def record_embedding_configuration(self, **values: Any) -> None:
        self.embedding_provenance.append(dict(values))

    def match_existing_profiles(self, **values: Any) -> list[ExistingCandidate]:
        self.match_calls += 1
        assert set(values) == {"configuration_id", "vectors", "limit"}
        assert 1 <= len(values["vectors"]) <= 3
        assert all(len(vector) == 2 for vector in values["vectors"].values())
        self.matched_vectors.append(dict(values["vectors"]))
        return [self.candidate]


class FakeProducer:
    def __init__(self) -> None:
        self.calls = 0
        self.available_axes = {"purpose", "target", "support"}

    def produce(self, **values: Any) -> ProducedRequestProfile:
        self.calls += 1
        run_id = values["analysis_run_id"]
        source_sha256 = sha256(values["source_path"].read_bytes()).hexdigest()
        profile = _profile(run_id, source_sha256)
        comparison = profile["comparison_profile"]
        if "purpose" not in self.available_axes:
            comparison.pop("purpose_goal")
        if "target" not in self.available_axes:
            comparison.pop("support_target")
        if "support" not in self.available_axes:
            comparison.pop("support_activities")
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
        self.last_values: dict[str, Any] | None = None

    def build_payload(self, **values: Any) -> dict[str, Any]:
        self.calls += 1
        self.last_values = values
        assert values["profile"]["schema_version"] == "pre_review_request_profile/v0.1"
        assert values["common_ir"]["schema_version"] == "common_ir_v1"
        if values["candidates"]:
            assert (
                values["candidates"][0].candidate.source_profile_id == "hwp:PBLN_TEST"
            )
        return {
            "program_name": "테스트 요청 사업",
            "axes": [],
            "candidates": [],
            "evidences": [],
        }


class CoreEngineLLM:
    """Return only the structured rows needed by the real CoreAnalysisEngine."""

    def __init__(self, *, fail_task: str | None = None, failure: Exception | None = None):
        self.fail_task = fail_task
        self.failure = failure
        self.tasks: list[str] = []
        self.payloads: list[dict[str, Any]] = []

    async def generate_structured(self, **values: Any) -> Any:
        task_name = values["task_name"]
        self.tasks.append(task_name)
        payload = json.loads(values["messages"][-1].content)
        self.payloads.append(payload)
        if task_name == self.fail_task:
            assert self.failure is not None
            raise self.failure
        schema = values["response_schema"]
        if task_name == "sim_common_key_classification":
            return schema.model_validate(
                {
                    "assignments": [
                        {
                            "fact_id": row["fact_id"],
                            "common_key": row["allowed_common_keys"][0],
                            "quoted_text": row["value_raw"],
                        }
                        for row in payload["facts"]
                    ]
                }
            )
        if task_name == "sim_axis_comparison":
            return schema.model_validate(
                {
                    "axes": [
                        {
                            "axis": row["axis"],
                            "status": "SIMILAR",
                            "request_fact_ids": [
                                entry["fact_id"] for entry in row["request"]
                            ],
                            "candidate_fact_ids": [
                                entry["fact_id"] for entry in row["candidate"]
                            ],
                        }
                        for row in payload["axes"]
                    ]
                }
            )
        if task_name == "cpl_purpose_axis_classification":
            return schema.model_validate(
                {
                    "assignments": [
                        {
                            "evidence_ref": row["evidence_ref"],
                            "axis_code": "PURPOSE_TARGET_CONDITION",
                            "quoted_text": row["raw_text"],
                        }
                        for row in payload["purpose_regions"]
                    ]
                }
            )
        if task_name == "fit_relation_comparison":
            return schema.model_validate(
                {
                    "relations": [
                        {
                            "relation_id": row["relation_id"],
                            "status": "INSUFFICIENT",
                            "summary": "관계 판정에 필요한 근거가 부족합니다.",
                            "reason_code": "COMPARISON_EVIDENCE_MISSING",
                        }
                        for row in payload["relations"]
                    ]
                }
            )
        raise AssertionError(f"unexpected LLM task: {task_name}")


_CORE_PURPOSE_TEXT = "○ (사업목적) 부산 관내 제조 중소기업의 기술경쟁력을 강화"


def _core_ir() -> dict[str, Any]:
    return {
        "document": {"document_id": "hwpx:core"},
        "blocks": [
            {
                "block_id": "hwpx:purpose",
                "occurrences": [
                    {"occurrence_id": "occ:purpose", "text": _CORE_PURPOSE_TEXT}
                ],
            }
        ],
    }


def _core_profile(profile_id: str = "request:core") -> dict[str, Any]:
    evidence = [
        {
            "source_block_id": "hwpx:purpose",
            "common_ir_document_id": "hwpx:core",
            "common_ir_block_id": "hwpx:purpose",
            "common_ir_occurrence_ids": ["occ:purpose"],
        }
    ]

    def fact(fact_id: str, value: str, facts_evidence: list[dict[str, Any]]):
        return {
            "fact_id": fact_id,
            "value_raw": value,
            "status": "identified",
            "evidence": facts_evidence,
        }

    return {
        "schema_version": "pre_review_request_profile/v0.1",
        "profile_id": profile_id,
        "processing_metadata": {"common_ir_document_id": "hwpx:core"},
        "comparison_profile": {
            "purpose_goal": [fact("fact:purpose", _CORE_PURPOSE_TEXT, evidence)],
            "support_target": [
                fact("fact:target", "부산 소재 중소기업", [])
            ],
            "support_activities": [fact("fact:content", "기술 컨설팅", [])],
        },
        "request_context": {},
        "support_components": [],
        "field_states": [
            {"field_name": name, "status": "identified"}
            for name in ("purpose_goal", "support_target", "support_activities")
        ],
    }


def _fixture() -> tuple[
    AnalysisJobHandler,
    FakeStorage,
    FakeStore,
    FakeProducer,
    FakeEmbedding,
    FakeEngine,
    ClaimedJob,
]:
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
    assert len(store.embedding_provenance) == 2
    assert all(item["configuration"] is not None for item in store.embedding_provenance)
    # Retry reads the two committed derived artifacts and candidate profile,
    # but does not fetch or parse the source again.
    retry_gets = storage.gets[first_get_count:]
    assert ("request-temp", job.payload["source_object_key"]) not in retry_gets
    assert store.cached is not None
    assert (
        store.cached.common_ir.bucket,
        store.cached.common_ir.object_key,
    ) in retry_gets
    assert (
        store.cached.structured_profile.bucket,
        store.cached.structured_profile.object_key,
    ) in retry_gets


def test_old_embedding_assembly_version_fails_before_embedding() -> None:
    handler, _storage, store, _producer, embedding, _engine, job = _fixture()

    store.active_embedding_configuration = lambda: EmbeddingConfiguration(  # type: ignore[method-assign]
        configuration_id=str(uuid4()),
        provider="openai",
        model_id="text-embedding-3-small",
        dimensions=2,
        max_input_tokens=8192,
        assembly_version="approved-facts-role-aware-v1",
    )

    with pytest.raises(AnalysisJobContractError, match="assembly version"):
        handler.handle(job)

    assert embedding.calls == 0
    assert store.match_calls == 0
    assert len(store.embedding_provenance) == 1
    assert store.embedding_provenance[0]["analysis_run_id"] == str(job.job_pk)
    assert store.embedding_provenance[0]["processing_run_id"] == str(
        job.processing_run_pk
    )
    assert store.embedding_provenance[0]["configuration"] is not None


def test_empty_existing_match_is_retrieval_readiness_failure() -> None:
    handler, _storage, store, _producer, _embedding, engine, job = _fixture()

    store.match_existing_profiles = lambda **_values: []  # type: ignore[method-assign]

    with pytest.raises(AnalysisJobUnavailable, match="embeddings are not ready"):
        handler.handle(job)

    assert engine.calls == 0
    assert len(store.embedding_provenance) == 1
    assert store.embedding_provenance[0]["configuration"] is not None


@pytest.mark.parametrize(
    "available_axes",
    (
        {"purpose"},
        {"purpose", "target"},
        {"purpose", "target", "support"},
    ),
)
def test_partial_axis_retrieval_embeds_only_grounded_request_axes(
    available_axes: set[str],
) -> None:
    handler, _storage, store, producer, embedding, engine, job = _fixture()
    producer.available_axes = available_axes

    handler.handle(job)

    assert store.config_calls == 1
    assert store.match_calls == 1
    assert set(store.matched_vectors[0]) == available_axes
    assert embedding.calls == len(available_axes)
    assert engine.last_values is not None
    decision = engine.last_values["retrieval"]
    assert decision.status == "completed"
    assert decision.reason_code is None
    assert decision.available_axes == tuple(
        axis for axis in ("purpose", "target", "support") if axis in available_axes
    )


def test_partial_retrieval_reaches_public_payload_with_missing_sim_axes_without_llm(
) -> None:
    """The real engine must preserve retrieval's missing-axis fence to output."""

    handler, storage, store, producer, _embedding, _engine, job = _fixture()
    producer.available_axes = {"purpose"}
    existing_ref = store.candidate.profile_artifact
    existing = {
        "schema_version": "existing_program_profile/v0.2",
        "source_profile_id": store.candidate.source_profile_id,
        "notice_id": store.candidate.notice_id,
        "comparison_profile": {
            "purpose_goal": [
                {
                    "fact_id": "candidate:purpose",
                    "value_raw": "지역 기업의 성장 지원",
                    "status": "identified",
                }
            ],
            "support_target": [
                {
                    "fact_id": "candidate:target",
                    "value_raw": "지역 중소기업",
                    "status": "identified",
                }
            ],
            "support_activities": [
                {
                    "fact_id": "candidate:content",
                    "value_raw": "기술 컨설팅",
                    "status": "identified",
                }
            ],
        },
        "field_states": [],
    }
    existing_bytes = _json(existing)
    storage.objects[(existing_ref.bucket, existing_ref.object_key)] = existing_bytes
    store.candidate = replace(
        store.candidate,
        profile_artifact=replace(
            existing_ref,
            content_sha256=sha256(existing_bytes).hexdigest(),
            size_bytes=len(existing_bytes),
        ),
    )
    llm = CoreEngineLLM()
    handler._engine = CoreAnalysisEngine(
        llm,
        cpl_model_profile="cpl",
        fit_model_profile="fit",
        sim_model_profile="sim",
    )

    payload = handler.handle(job)

    candidate = payload["candidates"][0]
    assert candidate["public_axes"]["purpose"]["status"] == "similar"
    for axis in ("target", "support"):
        assert candidate["public_axes"][axis]["status"] == "insufficient"
        assert candidate["public_axes"][axis]["reason_code"] == "REQUEST_AXIS_MISSING"

    comparisons = [
        row
        for task, row in zip(llm.tasks, llm.payloads, strict=True)
        if task == "sim_axis_comparison"
    ]
    assert len(comparisons) == 1
    assert [row["axis"] for row in comparisons[0]["axes"]] == ["purpose"]


def _request_common_ir(source_sha256: str) -> CommonIrArtifact:
    text = f"☑ 세부사업 신설\n{_CORE_PURPOSE_TEXT}"
    document = {
        "schema_version": "common_ir_v1",
        "document": {
            "document_id": "hwpx:producer",
            "source_kind": "hwpx",
            "artifact_role": "production",
            "page_count": 1,
            "raw_artifact_ids": [],
            "provenance": {"source_sha256": source_sha256},
        },
        "blocks": [
            {
                "block_id": "hwpx:producer-block",
                "kind": "paragraph",
                "reading_order": 0,
                "text": text,
                "boundary_markers": [],
                "page": None,
                "section_path": "section[0]/para[0]",
                "source_block_label": "paragraph",
                "structure_status": "explicit",
                "text_occurrence_ids": ["occ:producer"],
                "provenance": {},
                "occurrences": [
                    {
                        "occurrence_id": "occ:producer",
                        "text": text,
                        "role": None,
                        "provenance": {},
                    }
                ],
            }
        ],
        "relations": [],
        "conflicts": [],
    }
    return CommonIrArtifact(
        run_dir="",
        notice_id="producer",
        source_kind="hwpx",
        source_path="",
        source_sha256=source_sha256,
        common_ir_path="",
        common_ir_document_id="hwpx:producer",
        manifest={},
        block_count=1,
        document=document,
    )


class _FailingLLM:
    def __init__(self, failure: Exception) -> None:
        self.failure = failure

    async def generate_structured(self, **_values: Any) -> Any:
        raise self.failure


@pytest.mark.parametrize(
    "failure", [LLMTimeoutError("timeout"), LLMUnavailableError("unavailable")]
)
def test_request_profile_transport_failure_is_job_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: Exception
) -> None:
    source = b"request source"
    source_sha256 = sha256(source).hexdigest()
    common = _request_common_ir(source_sha256)
    monkeypatch.setattr(
        analysis_job_module, "parse_to_common_ir", lambda **_values: common
    )
    producer = VendoredRequestProfileProducer(
        _FailingLLM(failure), model_profile="request_profile", model_id="test"
    )

    with pytest.raises(AnalysisJobUnavailable, match="request profile structuring"):
        producer.produce(
            source_path=tmp_path / "request.hwpx",
            source_kind="hwpx",
            analysis_run_id="run-producer",
            run_dir=tmp_path / "pipeline",
        )


def _core_candidate() -> ExistingProfileDocument:
    artifact = ArtifactRef(
        bucket="existing-kb",
        object_key="core.json",
        content_sha256="0" * 64,
        artifact_type="structured_profile",
        mime_type="application/json",
        size_bytes=0,
    )
    candidate = ExistingCandidate(
        profile_version_id="version:core",
        source_profile_id="hwp:core-candidate",
        notice_id="bizinfo:core-candidate",
        average_similarity=0.9,
        purpose_similarity=0.9,
        target_similarity=0.9,
        support_similarity=0.9,
        profile_artifact=artifact,
    )
    return ExistingProfileDocument(
        candidate=candidate,
        profile=_core_profile("hwp:core-candidate"),
    )


@pytest.mark.parametrize(
    ("failure_task", "stage", "with_candidate"),
    [
        ("fit_relation_comparison", "FIT", False),
        ("sim_axis_comparison", "SIM comparison", True),
    ],
)
def test_engine_transport_failure_is_job_unavailable_at_stage_boundary(
    failure_task: str,
    stage: str,
    with_candidate: bool,
) -> None:
    llm = CoreEngineLLM(
        fail_task=failure_task, failure=LLMUnavailableError("provider down")
    )
    engine = CoreAnalysisEngine(
        llm,
        cpl_model_profile="cpl",
        fit_model_profile="fit",
        sim_model_profile="sim",
    )
    candidates = [_core_candidate()] if with_candidate else []
    retrieval = (
        RetrievalDecision.completed(("purpose", "target", "support"))
        if with_candidate
        else None
    )

    with pytest.raises(AnalysisJobUnavailable, match=f"during {stage}"):
        engine.build_payload(
            profile=_core_profile(),
            common_ir=_core_ir(),
            candidates=candidates,
            retrieval=retrieval,
        )


@pytest.mark.parametrize(
    ("failure_task", "with_candidate"),
    [("fit_relation_comparison", False), ("sim_axis_comparison", True)],
)
def test_invalid_llm_response_stays_axis_local_at_engine_boundary(
    failure_task: str, with_candidate: bool
) -> None:
    llm = CoreEngineLLM(
        fail_task=failure_task,
        failure=LLMInvalidResponseError("malformed response"),
    )
    engine = CoreAnalysisEngine(
        llm,
        cpl_model_profile="cpl",
        fit_model_profile="fit",
        sim_model_profile="sim",
    )
    candidates = [_core_candidate()] if with_candidate else []
    retrieval = (
        RetrievalDecision.completed(("purpose", "target", "support"))
        if with_candidate
        else None
    )

    payload = engine.build_payload(
        profile=_core_profile(),
        common_ir=_core_ir(),
        candidates=candidates,
        retrieval=retrieval,
    )

    assert payload["axes"]
    if not with_candidate:
        assert payload.get("candidates", []) == []
    else:
        assert payload["candidates"]
    if with_candidate:
        assert all(
            row["reason_code"] == "LLM_INVALID_RESPONSE"
            for name, row in payload["candidates"][0]["public_axes"].items()
            if name in {"purpose", "target", "support"}
        )
    else:
        assert any(
            row["axis_type"] == "FIT"
            and row["result_data"]["reason_code"] == "LLM_INVALID_RESPONSE"
            for row in payload["axes"]
        )


def test_zero_axis_retrieval_skips_config_provider_and_kb_but_runs_analysis() -> None:
    handler, _storage, store, producer, embedding, engine, job = _fixture()
    producer.available_axes = set()

    handler.handle(job)

    assert store.config_calls == store.match_calls == embedding.calls == 0
    assert store.embedding_provenance == [
        {
            "analysis_run_id": str(job.job_pk),
            "processing_run_id": str(job.processing_run_pk),
            "configuration": None,
        }
    ]
    assert engine.calls == 1
    assert engine.last_values is not None
    decision = engine.last_values["retrieval"]
    assert decision.status == "skipped"
    assert decision.reason_code == "RETRIEVAL_INPUT_MISSING"
    assert engine.last_values["candidates"] == []


def test_optional_empty_kb_materialises_a_completed_empty_sim_result() -> None:
    handler, _storage, store, _producer, _embedding, engine, job = _fixture()
    handler._existing_kb_required = False  # test the deployment setting branch
    store.match_existing_profiles = lambda **_values: []  # type: ignore[method-assign]

    handler.handle(job)

    assert engine.last_values is not None
    decision = engine.last_values["retrieval"]
    assert decision.status == "completed"
    assert decision.reason_code == "KB_EMPTY"


def test_source_hash_mismatch_fails_before_pipeline_or_upload() -> None:
    handler, storage, store, producer, _embedding, _engine, job = _fixture()
    storage.objects[("request-temp", job.payload["source_object_key"])] = b"tampered"

    with pytest.raises(AnalysisJobContractError, match="hash"):
        handler.handle(job)

    assert producer.calls == 0
    assert store.registrations == 0
    assert storage.puts == []


def test_failed_registration_keeps_content_addressed_objects_for_a_new_attempt() -> (
    None
):
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
                FitRelationId.FIT_1,
                FitStatus.INSUFFICIENT,
                "COMPARISON_EVIDENCE_MISSING",
                FitSide(),
                FitSide(),
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
    assert result["status"] == "partial"
    assert result["comparable_axes"] == ["purpose", "target", "support"]
    assert result["delivery_result"]["status"] == "insufficient"


def test_freetype_preload_must_be_an_existing_absolute_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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


@pytest.mark.parametrize("source_kind", ["pdf", "docx", "HWPX", ""])
def test_request_parser_rejects_unsupported_source_kind_before_subprocess(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    source_kind: str,
) -> None:
    from worker.contracts.profile_snapshot import PARSE_FAILED
    from worker.profiles import StageError, parse_to_common_ir

    def unexpected_popen(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("unsupported request input must not start a subprocess")

    monkeypatch.setattr(subprocess, "Popen", unexpected_popen)

    with pytest.raises(StageError) as caught:
        parse_to_common_ir(
            input_path=tmp_path / "request.pdf",
            notice_id="unsupported-source-kind",
            source_kind=source_kind,
            run_dir=tmp_path / "run",
        )

    assert caught.value.diagnostic.stage == "parse_to_common_ir"
    assert caught.value.diagnostic.reason_code == PARSE_FAILED
    assert "unsupported request parser source_kind" in caught.value.diagnostic.message
    assert "hwp, hwpx" in caught.value.diagnostic.message


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
    assert all(
        request.headers["authorization"] == "Bearer private-test-key"
        for request in requests
    )

    def poisoned(request: httpx.Request) -> httpx.Response:
        return (
            httpx.Response(409)
            if request.method == "POST"
            else httpx.Response(200, content=b"other")
        )

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
