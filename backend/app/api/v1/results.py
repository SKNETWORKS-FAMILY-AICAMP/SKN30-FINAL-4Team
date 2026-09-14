"""Polling read endpoints for completed analysis results and lifecycle state.

These endpoints intentionally expose no caller-supplied user id and do not
accept bearer tokens. ``PrincipalDep`` is the same HttpOnly-cookie boundary
used by every business API route.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response, Security, status
from pydantic import BaseModel, ConfigDict, Field

from app.api.cursor import decode_cursor, encode_cursor
from app.api.errors import not_found, service_unavailable, validation_error
from app.ports.results import (
    AnalysisHistoryPage,
    ResultNotFound,
    ResultRepository,
    ResultRepositoryUnavailable,
)

from ..auth import PrincipalDep, access_cookie_scheme, require_trusted_origin
from .openapi_models import error_responses


router = APIRouter(tags=["Analysis results"])

HISTORY_PAGE_SIZE = 5
_HISTORY_CURSOR_ENDPOINT = "analysis-history"
_HISTORY_CURSOR_VERSION = 1
_HISTORY_CURSOR_KEYS = frozenset({"snapshot_at", "completed_at", "analysis_case_id"})


class _ReadModel(BaseModel):
    """Strict fixed response boundary around the trusted ``api`` SQL shape.

    Everything is explicit so generated OpenAPI clients never receive an
    opaque ``additionalProperties`` response, and so a raw score, fact id, or
    diagnostics payload cannot silently ride along in an unvalidated field
    (v0.2 spec section 1, rule 6).
    """

    model_config = ConfigDict(extra="forbid")


class AnalysisCaseSummary(_ReadModel):
    analysis_case_id: UUID
    program_name: str | None
    original_filename: str | None
    completed_at: datetime | None


# --- CPL / FIT / SIM public status enums (spec section 7) ------------------


class CplStatus(str, Enum):
    confirmed = "confirmed"
    needs_confirmation = "needs_confirmation"
    no_content = "no_content"
    not_applicable = "not_applicable"


class FitStatus(str, Enum):
    fit = "FIT"
    needs_review = "NEEDS_REVIEW"
    conflict = "CONFLICT"
    insufficient = "INSUFFICIENT"
    not_applicable = "NOT_APPLICABLE"


class SimAxisStatus(str, Enum):
    similar = "similar"
    partial = "partial"
    different = "different"
    insufficient = "insufficient"


# --- CPL / FIT typed public detail (spec section 8.1) -----------------------


class CplValueItem(_ReadModel):
    label: str
    value: str
    evidence_ids: list[UUID] = Field(default_factory=list)


class CplAxisDetail(_ReadModel):
    reason_code: str | None = None
    reason: str | None = None
    values: list[CplValueItem] = Field(default_factory=list)
    source_fields: list[str] = Field(default_factory=list)
    evidence_ids: list[UUID] = Field(default_factory=list)


class FitSideDetail(_ReadModel):
    value_summary: str | None = None
    evidence_ids: list[UUID] = Field(default_factory=list)


class FitAxisDetail(_ReadModel):
    # True only when both sides had grounded evidence and a real comparison
    # judgement was made — never just "a provider call happened" (spec 7).
    comparison_performed: bool
    reason_code: str | None = None
    reason: str | None = None
    left: FitSideDetail
    right: FitSideDetail
    evidence_ids: list[UUID] = Field(default_factory=list)


class AnalysisCplAxisItem(_ReadModel):
    code: str
    status: CplStatus
    summary: str | None
    detail: CplAxisDetail


class AnalysisFitAxisItem(_ReadModel):
    code: str
    status: FitStatus
    summary: str | None
    detail: FitAxisDetail


class AnalysisCplSection(_ReadModel):
    items: list[AnalysisCplAxisItem] = Field(default_factory=list)


class AnalysisFitSection(_ReadModel):
    items: list[AnalysisFitAxisItem] = Field(default_factory=list)


class AnalysisSimCandidateSummary(_ReadModel):
    sim_candidate_id: UUID
    rank: int
    title: str | None
    comparison_status: SimAxisStatus
    comparison_summary: str | None = None


class AnalysisSimSection(_ReadModel):
    # Pipeline-level status ("completed"/"failed"/...), independent of each
    # candidate's own comparison_status (spec section 9.1).
    status: str
    reason_code: str | None = None
    summary: str | None = None
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
    # CPL/FIT evidence only: sim_candidate_pk IS NULL. SIM evidence never
    # appears here — it is only ever returned from its own candidate detail,
    # so evidence from different candidates can never be cross-referenced
    # (spec section 8.2).
    evidences: list[ResultEvidenceReadModel] = Field(default_factory=list)


# --- Similar-notice candidate detail (spec section 9.2) ---------------------


class SimCandidateMetadata(_ReadModel):
    """Exact snapshot of the Existing profile/source at analysis time.

    Never re-joined against the current KB row — the metadata a user sees
    must match what was actually compared, even if the source notice is
    edited or removed afterward.
    """

    title: str | None
    support_field: str | None
    apply_period: str | None
    ministry: str | None
    executing_agency: str | None
    registered_at: str | None
    notice_status: str | None
    source_url: str | None


class SimCandidateComparison(_ReadModel):
    status: SimAxisStatus
    summary: str | None
    # Only the three core axes feed the overall comparison status
    # (insufficient > different > partial > similar); delivery is shown
    # separately and never changes this value (spec section 9.2).
    comparable_axes: list[Literal["purpose", "target", "support"]] = Field(default_factory=list)


class SimCandidateAxisDetail(_ReadModel):
    code: str
    status: SimAxisStatus
    summary: str | None
    reason_code: str | None = None
    reason: str | None = None
    common_points: list[str] = Field(default_factory=list)
    differences: list[str] = Field(default_factory=list)
    request_evidence_ids: list[UUID] = Field(default_factory=list)
    existing_evidence_ids: list[UUID] = Field(default_factory=list)


class SimCandidateAxes(_ReadModel):
    # Internal "content" axis is exposed publicly only as ``support``.
    purpose: SimCandidateAxisDetail
    target: SimCandidateAxisDetail
    support: SimCandidateAxisDetail
    delivery: SimCandidateAxisDetail


class CandidateEvidenceReadModel(ResultEvidenceReadModel):
    axis_type: str | None


class SimCandidateDetailReadModel(_ReadModel):
    sim_candidate_id: UUID
    analysis_case_id: UUID
    rank: int
    metadata: SimCandidateMetadata
    comparison: SimCandidateComparison
    axes: SimCandidateAxes
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


class AnalysisHistoryEnvelope(_ReadModel):
    items: list[AnalysisHistoryEntryReadModel] = Field(default_factory=list)
    next_cursor: str | None = None


# --- GET /analysis/current discriminated union (spec section 5.4) ----------


class CurrentProcessingRun(_ReadModel):
    analysis_run_id: UUID
    status: Literal["uploading", "queued", "running"]
    original_filename: str | None
    created_at: datetime
    updated_at: datetime


class CurrentReadySession(_ReadModel):
    analysis_session_id: UUID
    analysis_case_id: UUID
    program_name: str | None
    original_filename: str | None
    session_expires_at: datetime


class AnalysisCurrentProcessing(_ReadModel):
    state: Literal["processing"]
    run: CurrentProcessingRun
    session: None = None


class AnalysisCurrentReady(_ReadModel):
    state: Literal["ready"]
    run: None = None
    session: CurrentReadySession


class AnalysisCurrentIdle(_ReadModel):
    state: Literal["idle"]
    run: None = None
    session: None = None


AnalysisCurrentReadModel = Annotated[
    AnalysisCurrentProcessing | AnalysisCurrentReady | AnalysisCurrentIdle,
    Field(discriminator="state"),
]


async def result_repository(request: Request) -> ResultRepository:
    # ``async def``, not a threadpool-hopping ``def``: see the identical note
    # on ``analysis_run_service`` in app/api/v1/analysis_runs.py.
    repository = getattr(request.app.state, "result_repository", None)
    if not isinstance(repository, ResultRepository):
        raise service_unavailable("Result service is not configured")
    return repository


ResultRepositoryDep = Annotated[ResultRepository, Depends(result_repository)]
TrustedOriginDep = Annotated[None, Depends(require_trusted_origin)]


def _not_found(detail: str) -> Exception:
    # Ownership is deliberately indistinguishable from absence.
    return not_found(detail)


def _database_unavailable(exc: ResultRepositoryUnavailable) -> Exception:
    return service_unavailable("Result database is temporarily unavailable")


def _cursor_secret(request: Request) -> str:
    secret = str(getattr(request.app.state, "cursor_signing_secret", "") or "").strip()
    if not secret:
        raise service_unavailable("Cursor signing is not configured")
    return secret


@router.get(
    "/analysis-cases/{analysis_case_id}",
    response_model=AnalysisResultReadModel,
    summary="분석 결과 전체 조회",
    dependencies=[Security(access_cookie_scheme)],
    responses=error_responses(401, 403, 404, 422, 429, 500, 502, 503),
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
    responses=error_responses(401, 403, 404, 422, 429, 500, 502, 503),
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
        **error_responses(401, 403, 422, 429, 500, 502, 503),
    },
    summary="현재 활성 분석 세션 조회 (호환용 legacy endpoint)",
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
    "/analysis/current",
    response_model=AnalysisCurrentReadModel,
    dependencies=[Security(access_cookie_scheme)],
    responses=error_responses(401, 403, 422, 429, 500, 502, 503),
    summary="현재 처리 중/열람 가능한 분석 상태 조회 (단일 스냅샷 3상태)",
)
async def get_analysis_current(
    principal: PrincipalDep,
    repository: ResultRepositoryDep,
) -> Any:
    try:
        payload = await repository.get_current(owner_id=principal.user_id)
    except ResultRepositoryUnavailable as exc:
        raise _database_unavailable(exc) from exc
    return payload


@router.post(
    "/analysis-sessions/{analysis_session_id}/close",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Security(access_cookie_scheme)],
    responses=error_responses(401, 403, 404, 422, 429, 500, 502, 503),
    summary="지정한 분석 세션 종료 (owner-scoped, idempotent)",
)
async def close_analysis_session(
    analysis_session_id: UUID,
    principal: PrincipalDep,
    _: TrustedOriginDep,
    repository: ResultRepositoryDep,
) -> Response:
    try:
        await repository.close_session(
            owner_id=principal.user_id,
            analysis_session_id=str(analysis_session_id),
        )
    except ResultNotFound as exc:
        # Also covers "session already closed/expired but not this owner's":
        # existence is deliberately indistinguishable from absence.
        raise _not_found("Analysis session not found") from exc
    except ResultRepositoryUnavailable as exc:
        raise _database_unavailable(exc) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/analysis-history",
    response_model=AnalysisHistoryEnvelope,
    summary="보관 기간 내 분석 이력 조회 (page size 5, signed snapshot cursor)",
    dependencies=[Security(access_cookie_scheme)],
    responses=error_responses(401, 403, 422, 429, 500, 502, 503),
)
async def list_analysis_history(
    request: Request,
    principal: PrincipalDep,
    repository: ResultRepositoryDep,
    cursor: Annotated[
        str | None,
        Query(max_length=2_000, description="이전 응답의 next_cursor를 그대로 전달한다."),
    ] = None,
) -> AnalysisHistoryEnvelope:
    secret = _cursor_secret(request)
    snapshot_at: datetime | None = None
    after: tuple[datetime, str] | None = None
    if cursor is not None:
        fields = decode_cursor(
            cursor,
            secret=secret,
            endpoint=_HISTORY_CURSOR_ENDPOINT,
            scope=principal.user_id,
            version=_HISTORY_CURSOR_VERSION,
            required_keys=_HISTORY_CURSOR_KEYS,
        )
        snapshot_at = _parse_cursor_datetime(fields["snapshot_at"])
        after = (
            _parse_cursor_datetime(fields["completed_at"]),
            _cursor_uuid(fields["analysis_case_id"]),
        )
    try:
        page: AnalysisHistoryPage = await repository.list_analysis_history_page(
            owner_id=principal.user_id,
            snapshot_at=snapshot_at,
            after=after,
            limit=HISTORY_PAGE_SIZE,
        )
    except ResultRepositoryUnavailable as exc:
        raise _database_unavailable(exc) from exc
    items = [AnalysisHistoryEntryReadModel.model_validate(row) for row in page.rows]
    next_cursor = None
    if page.next_after is not None:
        next_completed_at, next_case_id = page.next_after
        next_cursor = encode_cursor(
            secret=secret,
            endpoint=_HISTORY_CURSOR_ENDPOINT,
            scope=principal.user_id,
            version=_HISTORY_CURSOR_VERSION,
            fields={
                "snapshot_at": _isoformat(page.snapshot_at),
                "completed_at": _isoformat(next_completed_at),
                "analysis_case_id": next_case_id,
            },
        )
    return AnalysisHistoryEnvelope(items=items, next_cursor=next_cursor)


def _isoformat(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _parse_cursor_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise validation_error("cursor is invalid") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise validation_error("cursor is invalid")
        return parsed
    raise validation_error("cursor is invalid")


def _cursor_uuid(value: object) -> str:
    if not isinstance(value, str):
        raise validation_error("cursor is invalid")
    try:
        return str(UUID(value))
    except ValueError as exc:
        raise validation_error("cursor is invalid") from exc
