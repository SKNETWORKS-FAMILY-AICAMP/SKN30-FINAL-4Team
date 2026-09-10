"""한 요청서를 CPL → FIT → Retrieval → SIM 으로 연결하는 워커 조립기.

각 단계의 판정은 기존 모듈이 맡고, 이 파일은 순서·부분 실패·결과 묶음만
관리한다. 워커의 판정 함수들이 동기 경계(``asyncio.run``)를 사용하므로
함수 자체도 동기다. ``run_once`` 콜백에서 호출할 때는 ``asyncio.to_thread``
로 감싸면 된다.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
import tempfile
from datetime import datetime, timezone
from collections.abc import Mapping, Sequence
from copy import deepcopy
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, text

from worker.execution_log import record_profile_run

from app.ports.embedding_client import (
    EmbeddingClient,
    EmbeddingInvalidResponseError,
    EmbeddingTimeoutError,
    EmbeddingUnavailableError,
)
from app.ports.llm_client import LLMClient, LLMUnavailableError
from app.ports.object_storage import ObjectStorage

from .contracts.profile_snapshot import (
    LLM_UNAVAILABLE,
    CommonIrArtifact,
    StageDiagnostic,
)
from .contracts.sim_result import (
    InternalRanking,
    SimAxis,
    SimAxisResult,
    SimCandidateResult,
    SimComparisonResult,
    SimCommonProfile,
    SimReviewGrade,
    SimStatus,
    SIM_AXIS_IDS,
)
from .kb_ingest import (
    CandidateComparison,
    KbCandidate,
    compare_kb_candidates,
    profile_search_text,
    search_candidates,
)
from .persistence import AnalysisResults
from .profiles import (
    StageError,
    build_pack,
    make_vllm_selector,
    parse_to_common_ir,
    structure_request_profile,
)
from .sim import SIM_COMPARISON_PROMPT_VERSION, SIM_SCORING_VERSION
from .sim_inputs import SIM_RULESET_VERSION, build_common_profile
from .cpl import build_cpl_result
from .fit import analyze_fit

__all__ = ["analyse_case", "run_analysis"]


_RETRIEVAL_UNAVAILABLE = "RETRIEVAL_UNAVAILABLE"
_RETRIEVAL_NOT_READY = "RETRIEVAL_NOT_READY"
_RETRIEVAL_INPUT_INVALID = "RETRIEVAL_INPUT_INVALID"
_RETRIEVAL_TIMEOUT = "RETRIEVAL_TIMEOUT"
_RETRIEVAL_INVALID_RESPONSE = "RETRIEVAL_INVALID_RESPONSE"
_RETRIEVAL_FAILED = "RETRIEVAL_FAILED"
_SIM_INPUT_FAILED = "SIM_INPUT_FAILED"
_SIM_CANDIDATE_FAILED = "SIM_CANDIDATE_FAILED"


class _UnavailableLLM:
    """LLM 미설정을 기존 단계의 계약 오류로 전달한다."""

    async def generate_structured(self, **_kwargs: Any) -> Any:
        raise LLMUnavailableError("LLM client is not configured")


def _diagnostic(
    *, stage: str, reason_code: str, message: str, unit: str | None = None
) -> StageDiagnostic:
    # 외부 응답·원문을 진단 메시지에 넣지 않는다. 결과에 필요한 것은 오류
    # 종류와 어느 단계에서 격리됐는지다.
    return StageDiagnostic(
        stage=stage,
        unit=unit,
        reason_code=reason_code,
        message=message[:2000],
    )


def _common_ir_identity(
    common_ir: CommonIrArtifact | Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Artifact 또는 raw Common IR에서 결과 계보에 필요한 값만 읽는다."""

    if common_ir is None:
        return {}

    if isinstance(common_ir, CommonIrArtifact):
        document = common_ir.document if isinstance(common_ir.document, Mapping) else {}
        document_meta = document.get("document")
        document_meta = document_meta if isinstance(document_meta, Mapping) else {}
        provenance = document_meta.get("provenance")
        provenance = provenance if isinstance(provenance, Mapping) else {}
        identity = {
            "document_id": common_ir.common_ir_document_id
            or document_meta.get("document_id"),
            "source_kind": common_ir.source_kind or document_meta.get("source_kind"),
            "source_sha256": common_ir.source_sha256
            or document_meta.get("source_sha256")
            or provenance.get("source_sha256"),
            "source_location": common_ir.source_path
            or provenance.get("source_location"),
            "schema_version": document.get("schema_version")
            or document_meta.get("schema_version"),
            "artifact_role": document_meta.get("artifact_role"),
        }
    elif isinstance(common_ir, Mapping):
        document = common_ir
        document_meta = document.get("document")
        document_meta = document_meta if isinstance(document_meta, Mapping) else {}
        provenance = document_meta.get("provenance")
        provenance = provenance if isinstance(provenance, Mapping) else {}
        identity = {
            "document_id": document_meta.get("document_id") or document.get("document_id"),
            "source_kind": document_meta.get("source_kind") or document.get("source_kind"),
            "source_sha256": document_meta.get("source_sha256")
            or document.get("source_sha256")
            or provenance.get("source_sha256"),
            "source_location": document_meta.get("source_location")
            or provenance.get("source_location"),
            "schema_version": document.get("schema_version")
            or document_meta.get("schema_version"),
            "artifact_role": document_meta.get("artifact_role"),
        }
    else:
        raise TypeError("common_ir must be CommonIrArtifact, mapping, or None")

    return {key: value for key, value in identity.items() if value not in (None, "")}


def _profile_with_lineage(
    request_profile: dict[str, Any],
    common_ir: CommonIrArtifact | Mapping[str, Any] | None,
) -> dict[str, Any]:
    """판정 모듈에 넘길 프로파일에만 Common IR 계보를 보완한다.

    호출자 프로파일을 변경하지 않는다. 이미 있는 값을 새 Artifact 값으로
    덮어쓰지 않으며, 프로파일에 계보가 없을 때만 additive metadata를 넣는다.
    """

    profile = deepcopy(request_profile)
    identity = _common_ir_identity(common_ir)
    if not identity:
        return profile

    metadata = profile.get("processing_metadata")
    metadata = dict(metadata) if isinstance(metadata, Mapping) else {}
    metadata.setdefault("common_ir_document_id", identity.get("document_id"))
    metadata.setdefault("common_ir_source_sha256", identity.get("source_sha256"))
    metadata.setdefault("common_ir_schema_version", identity.get("schema_version"))
    profile["processing_metadata"] = metadata

    documents = profile.get("source_documents")
    if isinstance(documents, list) and documents:
        first = dict(documents[0]) if isinstance(documents[0], Mapping) else {}
        source = first.get("common_ir")
        source = dict(source) if isinstance(source, Mapping) else {}
        for key, value in (
            ("document_id", identity.get("document_id")),
            ("source_kind", identity.get("source_kind")),
            ("source_sha256", identity.get("source_sha256")),
            ("source_location", identity.get("source_location")),
            ("schema_version", identity.get("schema_version")),
            ("artifact_role", identity.get("artifact_role")),
        ):
            if value not in (None, ""):
                source.setdefault(key, value)
        first["common_ir"] = source
        documents[0] = first
    else:
        profile["source_documents"] = [
            {
                "format": identity.get("source_kind", "common_ir"),
                "common_ir": identity,
            }
        ]
    return profile


def _empty_sim(
    *,
    request_profile_id: str | None,
    model_profile: str,
    diagnostics: Sequence[StageDiagnostic] = (),
) -> SimComparisonResult:
    return SimComparisonResult(
        request_profile_id=request_profile_id,
        candidates=[],
        model_profile=model_profile,
        ruleset_version=SIM_RULESET_VERSION,
        prompt_version=SIM_COMPARISON_PROMPT_VERSION,
        scoring_version=SIM_SCORING_VERSION,
        diagnostics=list(diagnostics),
    )


def _failed_candidate(
    comparison: CandidateComparison,
    candidate: KbCandidate | None,
) -> SimCandidateResult:
    """비교할 수 없는 후보도 ``INSUFFICIENT`` 후보로 보존한다."""

    profile_id = comparison.source_profile_id or (
        candidate.source_profile_id if candidate is not None else None
    )
    reason = comparison.reason_code or _SIM_CANDIDATE_FAILED
    axes = [
        SimAxisResult(
            axis=axis,
            axis_id=SIM_AXIS_IDS[axis],
            status=SimStatus.INSUFFICIENT,
            reason_code=reason,
            diagnostics=list(comparison.diagnostics),
        )
        for axis in SimAxis
    ]
    return SimCandidateResult(
        candidate_profile_id=profile_id,
        candidate_notice_id=None,
        axes=axes,
        internal_ranking=InternalRanking(
            weighted_score=None,
            review_grade=SimReviewGrade.ON_HOLD,
            assessable_axis_count=0,
            scoring_version=SIM_SCORING_VERSION,
        ),
        diagnostics=list(comparison.diagnostics),
    )


def _failure_reason(error: BaseException) -> tuple[str, str]:
    if isinstance(error, EmbeddingTimeoutError):
        return _RETRIEVAL_TIMEOUT, "임베딩 호출 시간이 초과되어 검색을 격리했다."
    if isinstance(error, EmbeddingInvalidResponseError):
        return _RETRIEVAL_INVALID_RESPONSE, "임베딩 응답 계약이 맞지 않아 검색을 격리했다."
    if isinstance(error, EmbeddingUnavailableError):
        return _RETRIEVAL_UNAVAILABLE, "임베딩 클라이언트가 없어 검색을 수행하지 않았다."
    if isinstance(error, ValueError):
        return _RETRIEVAL_INPUT_INVALID, "검색 입력 또는 임베딩 프로파일을 확인하지 못했다."
    return _RETRIEVAL_FAILED, "검색 단계에서 오류가 발생해 SIM을 부분 결과로 남겼다."


def _resolve_model_profiles(
    model_profile: str | None,
    *,
    cpl_model_profile: str | None,
    fit_model_profile: str | None,
    sim_model_profile: str | None,
) -> tuple[str, str, str, str]:
    base = model_profile or "analysis"
    return (
        base,
        cpl_model_profile or base,
        fit_model_profile or base,
        sim_model_profile or base,
    )


def _request_program_name(profile: Mapping[str, Any]) -> str | None:
    """읽을 수 있는 프로필 식별자만 결과 스냅샷으로 옮긴다.

    Request Profile v0.1.2의 ``identity.title_raw``가 사업명 후보 위치다.
    현재 생산기는 이 값을 ``None``으로 두므로, 계층 노드·첫 heading·파일명으로
    보완하지 않는다. 사업명 추출은 별도 파서/프로필 계약의 후속 범위다.
    """

    identity = profile.get("identity")
    if not isinstance(identity, Mapping):
        return None
    title = identity.get("title_raw")
    if not isinstance(title, str):
        return None
    title = title.strip()
    return title or None


def run_analysis(
    request_profile: dict[str, Any],
    common_ir: CommonIrArtifact | Mapping[str, Any] | None,
    engine: Engine,
    llm_client: LLMClient | None,
    embedding_client: EmbeddingClient | None = None,
    *,
    embedding_profile_id: int | None = None,
    model_profile: str = "analysis",
    top_k: int = 5,
    max_repairs: int = 1,
    cpl_model_profile: str | None = None,
    fit_model_profile: str | None = None,
    sim_model_profile: str | None = None,
) -> AnalysisResults:
    """한 검사의 분석 결과를 조립한다.

    ``request_profile``은 이미 서버 검증을 통과한 dict여야 한다. 이 함수는
    파싱·구조화를 다시 실행하지 않는다. Retrieval/SIM이 실패해도 CPL과 FIT은
    결과로 돌려주며, 실패 단위는 SIM 진단에 남긴다.
    """

    if not isinstance(request_profile, dict):
        raise TypeError("request_profile must be a dict")
    if top_k < 1:
        raise ValueError("top_k must be positive")
    if max_repairs < 0:
        raise ValueError("max_repairs must not be negative")

    profile = _profile_with_lineage(request_profile, common_ir)
    active_llm: LLMClient = llm_client or _UnavailableLLM()
    model_profile, _, fit_model_profile, sim_model_profile = _resolve_model_profiles(
        model_profile,
        cpl_model_profile=cpl_model_profile,
        fit_model_profile=fit_model_profile,
        sim_model_profile=sim_model_profile,
    )
    diagnostics: list[StageDiagnostic] = []

    # 이 단계는 결정적이고, 입력 프로파일이 이미 검증됐다는 전제에서 예외를
    # 만들지 않는다.
    cpl = build_cpl_result(profile)

    try:
        fit = analyze_fit(
            profile,
            active_llm,
            model_profile=fit_model_profile,
            max_repairs=max_repairs,
        )
    except Exception as error:  # noqa: BLE001 - CPL을 보존하는 국소 실패
        fit = None
        diagnostics.append(
            _diagnostic(
                stage="analyze_fit",
                reason_code="FIT_FAILED",
                message=f"FIT 단계가 실패해 CPL을 보존했다 ({type(error).__name__}).",
            )
        )

    request_profile_id = profile.get("source_profile_id") or profile.get("profile_id")
    sim_profiles: dict[str, SimCommonProfile] = {}

    try:
        request_common = build_common_profile(
            profile,
            active_llm,
            model_profile=sim_model_profile,
        )
    except Exception as error:  # noqa: BLE001 - SIM 입력 단계 국소 실패
        diagnostics.append(
            _diagnostic(
                stage="build_common_profile",
                reason_code=_SIM_INPUT_FAILED,
                message=(
                    "요청서 공통 프로파일을 만들지 못해 CPL/FIT을 보존했다 "
                    f"({type(error).__name__})."
                ),
            )
        )
        sim = _empty_sim(
            request_profile_id=request_profile_id,
            model_profile=sim_model_profile,
            diagnostics=diagnostics,
        )
        return AnalysisResults(cpl=cpl, fit=fit, sim=sim)

    # persistence.py는 요청서 SIM 근거를 request_profile_id로 찾는다. 실제
    # id가 없는 입력도 빈 키로 보존해 ``None``을 다른 후보 근거로 빌리지 않는다.
    sim_profiles[request_common.source_profile_id or ""] = request_common

    if embedding_profile_id is None:
        diagnostics.append(
            _diagnostic(
                stage="search_candidates",
                reason_code=_RETRIEVAL_NOT_READY,
                message="임베딩 프로파일이 없어 공고 검색을 수행하지 않았다.",
            )
        )
        sim = _empty_sim(
            request_profile_id=request_common.source_profile_id,
            model_profile=sim_model_profile,
            diagnostics=[*request_common.diagnostics, *diagnostics],
        )
        return AnalysisResults(cpl=cpl, fit=fit, sim=sim, sim_profiles=sim_profiles)

    query_text = profile_search_text(profile)
    if not query_text.strip():
        diagnostics.append(
            _diagnostic(
                stage="search_candidates",
                reason_code=_RETRIEVAL_INPUT_INVALID,
                message="검색에 사용할 원문 입력이 없어 공고 검색을 수행하지 않았다.",
            )
        )
        sim = _empty_sim(
            request_profile_id=request_common.source_profile_id,
            model_profile=sim_model_profile,
            diagnostics=[*request_common.diagnostics, *diagnostics],
        )
        return AnalysisResults(cpl=cpl, fit=fit, sim=sim, sim_profiles=sim_profiles)

    try:
        candidates = search_candidates(
            engine,
            embedding_profile_id=embedding_profile_id,
            query_text=query_text,
            top_k=top_k,
            embedding_client=embedding_client,
        )
    except Exception as error:  # noqa: BLE001 - retrieval은 국소 실패로 격리
        reason, message = _failure_reason(error)
        diagnostics.append(
            _diagnostic(stage="search_candidates", reason_code=reason, message=message)
        )
        sim = _empty_sim(
            request_profile_id=request_common.source_profile_id,
            model_profile=sim_model_profile,
            diagnostics=[*request_common.diagnostics, *diagnostics],
        )
        return AnalysisResults(cpl=cpl, fit=fit, sim=sim, sim_profiles=sim_profiles)

    try:
        comparisons = compare_kb_candidates(
            engine,
            request_common,
            list(candidates),
            active_llm,
            model_profile=sim_model_profile,
        )
    except Exception as error:  # noqa: BLE001 - 후보 비교는 국소 실패
        diagnostics.append(
            _diagnostic(
                stage="compare_kb_candidates",
                reason_code=_SIM_CANDIDATE_FAILED,
                message=(
                    "공고 후보 비교가 실패해 후보 부재 진단만 남겼다 "
                    f"({type(error).__name__})."
                ),
            )
        )
        comparisons = [
            CandidateComparison(
                announcement_version_id=candidate.announcement_version_id,
                source_profile_id=candidate.source_profile_id,
                reason_code=_SIM_CANDIDATE_FAILED,
                diagnostics=[diagnostics[-1]],
                announcement_profile_id=candidate.announcement_profile_id,
            )
            for candidate in candidates
        ]

    # KB 비교기가 실제로 사용한 Existing 공통 프로파일을 결과 저장 경계까지
    # 전달한다. 없던 프로파일을 재구성하지 않으며, 실패 후보는 기존 진단만
    # 남긴다.
    for comparison in comparisons:
        candidate_common = comparison.candidate_common
        if candidate_common is None:
            continue
        candidate_profile_id = (
            candidate_common.source_profile_id or comparison.source_profile_id
        )
        if candidate_profile_id:
            sim_profiles[candidate_profile_id] = candidate_common

    by_version = {
        comparison.announcement_version_id: comparison for comparison in comparisons
    }
    sim_candidates: list[SimCandidateResult] = []
    for candidate in candidates:
        comparison = by_version.get(candidate.announcement_version_id)
        if comparison is None:
            comparison = CandidateComparison(
                announcement_version_id=candidate.announcement_version_id,
                source_profile_id=candidate.source_profile_id,
                reason_code=_SIM_CANDIDATE_FAILED,
                diagnostics=[
                    _diagnostic(
                        stage="compare_kb_candidates",
                        unit=str(candidate.announcement_version_id),
                        reason_code=_SIM_CANDIDATE_FAILED,
                        message="후보 비교 응답이 없어 해당 후보만 격리했다.",
                    )
                ],
                announcement_profile_id=candidate.announcement_profile_id,
            )
        result = (
            comparison.result
            if comparison.result is not None
            else _failed_candidate(comparison, candidate)
        )
        # 검색 행의 원문 공고명만 신뢰한다. 비교 결과나 프로파일에서 제목을
        # 역추론하지 않는다.
        sim_candidates.append(replace(result, title=candidate.pblanc_nm))

    # compare_kb_candidates가 검색 목록에 없는 행을 돌려주는 경우에도 그
    # 응답을 버리지 않는다. 다만 순위는 검색 결과 순서가 기준이므로 뒤에 둔다.
    candidate_versions = {candidate.announcement_version_id for candidate in candidates}
    sim_candidates.extend(
        comparison.result
        if comparison.result is not None
        else _failed_candidate(comparison, None)
        for comparison in comparisons
        if comparison.announcement_version_id not in candidate_versions
    )

    sim = SimComparisonResult(
        request_profile_id=request_common.source_profile_id,
        candidates=sim_candidates,
        model_profile=sim_model_profile,
        ruleset_version=SIM_RULESET_VERSION,
        prompt_version=SIM_COMPARISON_PROMPT_VERSION,
        scoring_version=SIM_SCORING_VERSION,
        diagnostics=[*request_common.diagnostics, *diagnostics],
    )
    return AnalysisResults(
        cpl=cpl,
        fit=fit,
        sim=sim,
        sim_profiles=sim_profiles,
    )


# ------------------------------------------------------------ 케이스 진입점

# 업로드된 원본 하나. 케이스 → 업로드 문서 → 파일 자산은 1:1 이다.
_CASE_SOURCE = text(
    """
    SELECT f.storage_key, f.extension, f.original_filename
      FROM sims.inspection_case c
      JOIN sims.uploaded_document d ON d.inspection_case_id = c.id
      JOIN sims.file_asset f ON f.id = d.file_asset_id
     WHERE c.id = :case_id
    """
)

# 활성 SUMMARY 임베딩 프로파일은 정확히 하나라는 기존 검색 계약을 그대로
# 읽는다. 하나가 아니면 고르지 않는다 — 검색을 건너뛰고 SIM 은 진단만 남긴다.
_ACTIVE_EMBEDDING_PROFILE = text(
    """
    SELECT p.id
      FROM sims.embedding_profile p
      JOIN sims.embedding_model m ON m.id = p.embedding_model_id
     WHERE p.profile_kind = 'SUMMARY' AND p.is_active AND m.is_enabled
    """
)


def _active_embedding_profile_id(engine: Engine) -> int | None:
    with engine.connect() as connection:
        rows = connection.execute(_ACTIVE_EMBEDDING_PROFILE).scalars().all()
    return int(rows[0]) if len(rows) == 1 else None


_CASE_ANALYSIS_RUN = text(
    """
    SELECT analysis_run_id FROM sims.inspection_case WHERE id = :case_id
    """
)


def analyse_case(
    engine: Engine,
    storage: ObjectStorage,
    llm_client: LLMClient | None,
    case_id: int,
    *,
    embedding_client: EmbeddingClient | None = None,
    embedding_profile_id: int | None = None,
    model_profile: str = "analysis",
    top_k: int = 5,
    max_repairs: int = 1,
    cpl_model_profile: str | None = None,
    fit_model_profile: str | None = None,
    sim_model_profile: str | None = None,
) -> AnalysisResults:
    """업로드된 요청서 한 건을 원본 → Common IR → 프로파일 → 결과로 잇는다.

    초안 §3 의 순서 그대로다. 이 함수는 순서와 재료만 맡고 판정은 하지 않는다.
    ``run_analysis`` 와 마찬가지로 **동기** 다. 큐 콜백에서는
    ``asyncio.to_thread`` 로 감싼다.

    요청서 프로파일을 만들지 못하면 결과를 반쪽으로 지어내지 않고
    ``StageError`` 로 올린다. 호출자가 그 건을 실패로 남길지, 레거시 결과만
    남길지 정한다.
    """

    if llm_client is None:
        raise StageError(
            _diagnostic(
                stage="analyse_case",
                unit=str(case_id),
                reason_code=LLM_UNAVAILABLE,
                message="LLM 클라이언트가 없어 요청서 구조화를 시작하지 않았다.",
            )
        )

    model_profile, cpl_model_profile, fit_model_profile, sim_model_profile = (
        _resolve_model_profiles(
            model_profile,
            cpl_model_profile=cpl_model_profile,
            fit_model_profile=fit_model_profile,
            sim_model_profile=sim_model_profile,
        )
    )

    with engine.connect() as connection:
        source = (
            connection.execute(_CASE_SOURCE, {"case_id": case_id})
            .mappings()
            .one_or_none()
        )
    if source is None:
        raise LookupError(f"sims.inspection_case {case_id} has no uploaded document")

    extension = (source["extension"] or "").lower()
    profile_id = f"case:{case_id}"
    # 파싱 러너는 파일 경로를 받는다. 저장소 구현에 기대지 않도록 포트로 읽어
    # 임시 파일에 내려놓고, 끝나면 작업 디렉터리째 지운다.
    with tempfile.TemporaryDirectory(prefix=f"case{case_id}-") as work_dir:
        source_path = Path(work_dir) / f"source.{extension}"
        handle = asyncio.run(storage.open(source["storage_key"]))
        try:
            source_path.write_bytes(handle.read())
        finally:
            handle.close()

        artifact = parse_to_common_ir(
            input_path=source_path,
            notice_id=f"case{case_id}",
            source_kind=extension,
            run_dir=Path(work_dir) / "run",
        )
        pack = build_pack(artifact.document)
        started_at = datetime.now(timezone.utc)
        snapshot = structure_request_profile(
            document=artifact.document,
            pack=pack,
            profile_id=profile_id,
            selector=make_vllm_selector(
                llm_client,
                model_profile=cpl_model_profile,
                pack=pack,
                document=artifact.document,
                profile_id=profile_id,
            ),
            model_id=cpl_model_profile,
            max_repairs=max_repairs,
            common_ir=artifact,
        )
        finished_at = datetime.now(timezone.utc)

    # 성공하든 실패하든 남긴다. 성공 경로에서 진단을 버리면 "보완이 몇 번
    # 돌았고 무엇이 걸렸나" 가 사라지고, 재료화 실패를 묶음 단위로 격리할 때
    # "무엇을 덜어냈는지" 를 적을 자리도 없어진다 (초안 §9.5).
    with engine.begin() as connection:
        record_profile_run(
            connection,
            snapshot,
            source_analysis_run_id=connection.scalar(
                _CASE_ANALYSIS_RUN, {"case_id": case_id}
            ),
            started_at=started_at,
            finished_at=finished_at,
        )

    if snapshot.status != "OK" or snapshot.profile is None:
        raise StageError(
            snapshot.diagnostics[-1]
            if snapshot.diagnostics
            else _diagnostic(
                stage="structure_request_profile",
                unit=profile_id,
                reason_code="PROFILE_FAILED",
                message="요청서 프로파일을 만들지 못했다.",
            )
        )

    results = run_analysis(
        snapshot.profile,
        artifact,
        engine,
        llm_client,
        embedding_client,
        embedding_profile_id=(
            embedding_profile_id
            if embedding_profile_id is not None
            else _active_embedding_profile_id(engine)
        ),
        model_profile=model_profile,
        cpl_model_profile=cpl_model_profile,
        fit_model_profile=fit_model_profile,
        sim_model_profile=sim_model_profile,
        top_k=top_k,
        max_repairs=max_repairs,
    )
    return replace(
        results,
        program_name=_request_program_name(snapshot.profile),
        original_filename=source["original_filename"],
    )
