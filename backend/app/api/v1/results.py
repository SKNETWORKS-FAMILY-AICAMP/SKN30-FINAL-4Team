"""Polling read endpoints for completed analysis results.

These endpoints intentionally expose no caller-supplied user id and do not
accept bearer tokens. ``PrincipalDep`` is the same HttpOnly-cookie boundary
used by every business API route.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, Security, status
from pydantic import BaseModel, ConfigDict, Field

from app.ports.results import (
    ResultNotFound,
    ResultRepository,
    ResultRepositoryUnavailable,
)

from ..auth import PrincipalDep, access_cookie_scheme
from .openapi_models import error_responses


router = APIRouter(tags=["Analysis results"])


class _ReadModel(BaseModel):
    """Strict fixed response boundary around the trusted ``api`` SQL shape.

    The detailed judgement JSON is intentionally extensible: its exact fields
    belong to versioned CPL/FIT/SIM contracts rather than the transport model.
    Everything else is explicit so generated OpenAPI clients do not receive an
    opaque ``additionalProperties`` response.
    """

    model_config = ConfigDict(extra="forbid")


class AnalysisCaseSummary(_ReadModel):
    analysis_case_id: UUID
    program_name: str | None
    original_filename: str | None
    completed_at: datetime | None


class AnalysisAxisItem(_ReadModel):
    code: str
    status: str
    summary: str | None
    detail: dict[str, Any] = Field(default_factory=dict)


class AnalysisCplSection(_ReadModel):
    items: list[AnalysisAxisItem] = Field(default_factory=list)


class AnalysisFitSection(_ReadModel):
    items: list[AnalysisAxisItem] = Field(default_factory=list)


class AnalysisSimCandidateSummary(_ReadModel):
    sim_candidate_id: UUID
    rank: int
    title: str | None


class AnalysisSimSection(_ReadModel):
    candidates: list[AnalysisSimCandidateSummary] = Field(default_factory=list)


class ReportReadModel(_ReadModel):
    status: str
    can_download: bool
    can_regenerate: bool
    retry_count: int


class AnalysisSessionReadModel(_ReadModel):
    analysis_session_id: UUID | None
    is_active: bool
    # This is session metadata.  It does not imply that a chat HTTP API is
    # currently mounted.
    can_chat: bool
    expires_at: datetime | None


# 저장된 ML payload 는 공개 표면보다 넓다. 프론트에 내리는 것은 message 하나뿐이고
# 나머지는 DB 와 챗봇 컨텍스트에 그대로 남는다.
#
# message 만 남기는 이유. 이 문구는 모델이 준 자유 문장이 아니라 _validate_reference
# 가 검증한 구조값으로 서버가 조립한 문장이다(worker/ml_reference.py). 그래서
# support_type·anomaly_level 은 같은 출처를 두 번 내리는 중복이고, message 는
# status 가 무엇이든 항상 채워진다 — OK 면 조립한 문장, 아니면 reason_code 문구다.
#
# 지우지 않고 exclude 로 빼는 이유가 있다. extra="forbid" 가 confidence·percentile
# 같은 내부 점수가 새는 것을 막는 가드인데, 필드를 선언에서 지우면 저장 payload 의
# 그 키들이 "모르는 키" 가 되어 가드가 통째로 무력해진다. 선언은 남겨 검증을 계속
# 받게 하고, 직렬화에서만 제외한다.
class MlModel1ReadModel(_ReadModel):
    status: Literal["OK", "UNAVAILABLE", "FAILED"] = Field(exclude=True)
    support_type: str | None = Field(exclude=True)
    message: str | None
    reason_code: str | None = Field(exclude=True)


class MlModel2ReadModel(_ReadModel):
    status: Literal["OK", "UNAVAILABLE", "FAILED"] = Field(exclude=True)
    predicted_amount_won: int | None = Field(exclude=True)
    message: str | None
    reason_code: str | None = Field(exclude=True)


class MlModel3ReadModel(_ReadModel):
    status: Literal["OK", "UNAVAILABLE", "FAILED"] = Field(exclude=True)
    anomaly_level: str | None = Field(exclude=True)
    cause_axes: list[str] = Field(default_factory=list, exclude=True)
    message: str | None
    reason_code: str | None = Field(exclude=True)


class MlReferenceReadModel(_ReadModel):
    model_1: MlModel1ReadModel
    model_2: MlModel2ReadModel
    model_3: MlModel3ReadModel


class ResultEvidenceReadModel(_ReadModel):
    evidence_id: UUID
    side: Literal["request", "existing"]
    field_name: str | None
    raw_value: str
    excerpt: str | None


class AnalysisResultReadModel(_ReadModel):
    case: AnalysisCaseSummary
    cpl: AnalysisCplSection
    fit: AnalysisFitSection
    sim: AnalysisSimSection
    ml: MlReferenceReadModel
    report: ReportReadModel
    session: AnalysisSessionReadModel
    evidences: list[ResultEvidenceReadModel] = Field(default_factory=list)


class CandidateAxesReadModel(_ReadModel):
    # Axis detail is produced by the comparison contract and may gain fields
    # without a FastAPI route change.  The four top-level axes are fixed.
    purpose: dict[str, Any] = Field(default_factory=dict)
    target: dict[str, Any] = Field(default_factory=dict)
    support: dict[str, Any] = Field(default_factory=dict)
    delivery: dict[str, Any] = Field(default_factory=dict)


class CandidateEvidenceReadModel(ResultEvidenceReadModel):
    axis_type: str | None


class SimCandidateDetailReadModel(_ReadModel):
    sim_candidate_id: UUID
    analysis_case_id: UUID
    rank: int
    title: str | None
    issuing_organization: str | None
    source_url: str | None
    notice_status: str | None
    status: str
    summary: str | None
    comparable_axes: list[str] = Field(default_factory=list)
    axes: CandidateAxesReadModel
    evidences: list[CandidateEvidenceReadModel] = Field(default_factory=list)


class ActiveAnalysisSessionReadModel(_ReadModel):
    analysis_session_id: UUID
    analysis_case_id: UUID
    program_name: str | None
    original_filename: str | None
    session_expires_at: datetime


class AnalysisHistoryEntryReadModel(_ReadModel):
    analysis_case_id: UUID
    program_name: str | None
    original_filename: str | None
    completed_at: datetime
    report_status: str | None
    report_completed_at: datetime | None


def result_repository(request: Request) -> ResultRepository:
    repository = getattr(request.app.state, "result_repository", None)
    if not isinstance(repository, ResultRepository):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Result service is not configured",
        )
    return repository


ResultRepositoryDep = Annotated[ResultRepository, Depends(result_repository)]


def _not_found(detail: str) -> HTTPException:
    # Ownership is deliberately indistinguishable from absence.
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


def _database_unavailable(exc: ResultRepositoryUnavailable) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Result database is temporarily unavailable",
    )


@router.get(
    "/analysis-cases/{analysis_case_id}",
    response_model=AnalysisResultReadModel,
    summary="분석 결과 전체 조회",
    dependencies=[Security(access_cookie_scheme)],
    responses=error_responses(401, 403, 404, 422, 500, 503),
)
async def get_analysis_case(
    analysis_case_id: UUID,
    principal: PrincipalDep,
    repository: ResultRepositoryDep,
) -> AnalysisResultReadModel:
    try:
        payload = await repository.get_analysis_case(
            owner_id=principal.user_id,
            analysis_case_id=str(analysis_case_id),
        )
    except ResultNotFound as exc:
        raise _not_found("Analysis result not found") from exc
    except ResultRepositoryUnavailable as exc:
        raise _database_unavailable(exc) from exc
    return AnalysisResultReadModel.model_validate(payload)


@router.get(
    "/sim-candidates/{sim_candidate_id}",
    response_model=SimCandidateDetailReadModel,
    summary="유사 공고 후보 상세 조회",
    dependencies=[Security(access_cookie_scheme)],
    responses=error_responses(401, 403, 404, 422, 500, 503),
)
async def get_sim_candidate(
    sim_candidate_id: UUID,
    principal: PrincipalDep,
    repository: ResultRepositoryDep,
) -> SimCandidateDetailReadModel:
    try:
        payload = await repository.get_sim_candidate(
            owner_id=principal.user_id,
            sim_candidate_id=str(sim_candidate_id),
        )
    except ResultNotFound as exc:
        raise _not_found("Similarity candidate not found") from exc
    except ResultRepositoryUnavailable as exc:
        raise _database_unavailable(exc) from exc
    return SimCandidateDetailReadModel.model_validate(payload)


@router.get(
    "/analysis-sessions/active",
    response_model=ActiveAnalysisSessionReadModel,
    dependencies=[Security(access_cookie_scheme)],
    responses={
        204: {"description": "No active analysis session"},
        **error_responses(401, 403, 422, 500, 503),
    },
    summary="현재 활성 분석 세션 조회",
)
async def get_active_analysis_session(
    principal: PrincipalDep,
    repository: ResultRepositoryDep,
) -> ActiveAnalysisSessionReadModel | Response:
    try:
        payload = await repository.get_active_session(owner_id=principal.user_id)
    except ResultRepositoryUnavailable as exc:
        raise _database_unavailable(exc) from exc
    if payload is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    return ActiveAnalysisSessionReadModel.model_validate(payload)


@router.get(
    "/analysis-history",
    response_model=list[AnalysisHistoryEntryReadModel],
    summary="보관 기간 내 분석 이력 조회",
    dependencies=[Security(access_cookie_scheme)],
    responses=error_responses(401, 403, 422, 500, 503),
)
async def list_analysis_history(
    principal: PrincipalDep,
    repository: ResultRepositoryDep,
) -> list[AnalysisHistoryEntryReadModel]:
    try:
        records = await repository.list_analysis_history(owner_id=principal.user_id)
    except ResultRepositoryUnavailable as exc:
        raise _database_unavailable(exc) from exc
    return [AnalysisHistoryEntryReadModel.model_validate(record) for record in records]
