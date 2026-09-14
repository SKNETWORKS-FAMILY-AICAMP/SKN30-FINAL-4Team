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
    """결과 화면의 제목·이력 표시용 분석 식별 정보."""

    analysis_case_id: UUID = Field(description="결과/대화 이력을 조회할 때 사용하는 분석 case ID.")
    program_name: str | None = Field(description="분석된 공고·사업명. 원문에 없으면 null.")
    original_filename: str | None = Field(description="업로드 당시 요청서 파일명. 없으면 null.")
    completed_at: datetime | None = Field(description="분석 완료 시각(ISO 8601). 아직 완료되지 않았으면 null.")


# --- CPL / FIT / SIM public status enums (spec section 7) ------------------


class CplStatus(str, Enum):
    """CPL UI 배지 값. 숫자 점수·등급으로 환산하지 않는다.

    ``confirmed``=확인됨/초록, ``needs_confirmation``=확인 필요/노랑,
    ``no_content``=내용 없음/빨강, ``not_applicable``=해당 없음/회색.
    """

    confirmed = "confirmed"
    needs_confirmation = "needs_confirmation"
    no_content = "no_content"
    not_applicable = "not_applicable"


class FitStatus(str, Enum):
    """FIT UI 배지 값. 숫자 점수·등급으로 환산하지 않는다.

    ``FIT``=적합/초록, ``NEEDS_REVIEW``=검토 필요/주황,
    ``CONFLICT``=충돌/빨강, ``INSUFFICIENT``=근거 부족/주황,
    ``NOT_APPLICABLE``=해당 없음/회색.
    """

    fit = "FIT"
    needs_review = "NEEDS_REVIEW"
    conflict = "CONFLICT"
    insufficient = "INSUFFICIENT"
    not_applicable = "NOT_APPLICABLE"


class SimAxisStatus(str, Enum):
    """SIM UI 배지 값. 숫자 유사도 점수로 환산하지 않는다.

    ``similar``=유사/초록, ``partial``=일부 유사/노랑,
    ``different``=상이/빨강, ``insufficient``=근거 부족/회색.
    """

    similar = "similar"
    partial = "partial"
    different = "different"
    insufficient = "insufficient"


# --- CPL / FIT typed public detail (spec section 8.1) -----------------------


class CplValueItem(_ReadModel):
    label: str = Field(description="화면에 표시할 확인 항목명.")
    value: str = Field(description="원문에서 추출·확인된 항목 값.")
    evidence_ids: list[UUID] = Field(
        description="이 값의 근거 ID 목록. 결과 최상위 evidences[]의 evidence_id와 조인한다.",
        examples=[["77777777-7777-7777-7777-777777777777"]],
    )


class CplAxisDetail(_ReadModel):
    reason_code: str | None = Field(description="기계 판독용 판단 사유 코드. 없으면 null.")
    reason: str | None = Field(description="사용자에게 보여줄 판단 사유. 없으면 null.")
    values: list[CplValueItem] = Field(description="CPL 판단에 표시할 확인 값 목록.")
    source_fields: list[str] = Field(description="판단에 사용한 원문 필드명 목록.")
    evidence_ids: list[UUID] = Field(
        description="이 CPL 축 전체의 근거 ID 목록. 결과 최상위 evidences[]의 evidence_id와 조인한다.",
    )


class FitSideDetail(_ReadModel):
    value_summary: str | None = Field(description="비교 한쪽의 표시용 값 요약. 없으면 null.")
    evidence_ids: list[UUID] = Field(
        description="이쪽 값의 근거 ID 목록. 결과 최상위 evidences[]의 evidence_id와 조인한다.",
    )


class FitAxisDetail(_ReadModel):
    # True only when both sides had grounded evidence and a real comparison
    # judgement was made — never just "a provider call happened" (spec 7).
    comparison_performed: bool = Field(
        description="양쪽 모두에 근거가 있어 실제 비교 판단을 했으면 true. 외부 모델 호출 여부를 뜻하지 않는다."
    )
    reason_code: str | None = Field(description="기계 판독용 비교 사유 코드. 없으면 null.")
    reason: str | None = Field(description="사용자에게 보여줄 비교 판단 근거. 없으면 null.")
    left: FitSideDetail = Field(description="관계 비교의 왼쪽 값과 원문 근거.")
    right: FitSideDetail = Field(description="관계 비교의 오른쪽 값과 원문 근거.")
    evidence_ids: list[UUID] = Field(
        description="이 FIT 축 전체의 근거 ID 목록. 결과 최상위 evidences[]의 evidence_id와 조인한다.",
    )


class AnalysisCplAxisItem(_ReadModel):
    code: str = Field(
        description=(
            "CPL 표시 항목 코드: CPL-01 요청유형, CPL-02 사업 목적·목표, "
            "CPL-03 추진계획, CPL-04 사업기간, CPL-05 신설·변경 주요내용, "
            "CPL-06 사업필요성, CPL-07 지원근거, CPL-08 연계정책, "
            "CPL-09 사업예산, CPL-10 지원대상·조건, CPL-11 지원내용·규모, "
            "CPL-12 수행기관·방식·체계, CPL-13 기대효과·성과."
        ),
        examples=["CPL-01"],
    )
    status: CplStatus = Field(
        description=(
            "CPL UI 배지: confirmed(확인됨), needs_confirmation(확인 필요), "
            "no_content(내용 없음), not_applicable(해당 없음). 숫자 점수로 환산하지 않는다."
        ),
        examples=["confirmed"],
    )
    summary: str | None = Field(
        description="목록/배지 옆에 보여줄 짧은 판단 요약. 없으면 null."
    )
    detail: CplAxisDetail = Field(description="CPL 판단 근거와 evidences[] 조인 ID.")


class AnalysisFitAxisItem(_ReadModel):
    code: str = Field(
        description=(
            "FIT 관계 코드: FIT-1 목적↔지원대상, FIT-2 목적↔지원내용, "
            "FIT-3 목적↔기대효과·성과지표, FIT-4 세부사업↔하위사업, "
            "FIT-5 지원대상↔지원조건, FIT-6 수행기관↔역할·절차, "
            "FIT-7 지원내용↔지원규모 수치·조건."
        ),
        examples=["FIT-1"],
    )
    status: FitStatus = Field(
        description=(
            "FIT UI 배지: FIT(적합), NEEDS_REVIEW(검토 필요), CONFLICT(충돌), "
            "INSUFFICIENT(근거 부족), NOT_APPLICABLE(해당 없음). 숫자 점수로 환산하지 않는다."
        ),
        examples=["FIT"],
    )
    summary: str | None = Field(
        description="목록/배지 옆에 보여줄 짧은 비교 요약. 없으면 null."
    )
    detail: FitAxisDetail = Field(description="양쪽 값, 비교 근거, evidences[] 조인 ID.")


class AnalysisCplSection(_ReadModel):
    items: list[AnalysisCplAxisItem] = Field(
        description="CPL-01부터 CPL-13까지의 항목별 확인 결과.",
    )


class AnalysisFitSection(_ReadModel):
    items: list[AnalysisFitAxisItem] = Field(
        description="FIT-1부터 FIT-7까지의 관계별 정합성 결과.",
    )


class AnalysisSimCandidateSummary(_ReadModel):
    """결과 화면의 유사 공고 후보 목록 행.

    사용자는 ``sim_candidate_id``로 GET /api/v1/sim-candidates/{sim_candidate_id}를
    호출해 상세 렌더링 필드(metadata, comparison, axes, evidences)를 받는다.
    """

    sim_candidate_id: UUID = Field(description="후보 상세 조회에 그대로 사용할 후보 ID.")
    rank: int = Field(description="후보 목록 표시 순서(1이 첫 번째). 점수나 유사도 값이 아니다.")
    title: str | None = Field(description="후보 공고 제목. 없으면 null.")
    comparison_status: SimAxisStatus = Field(
        description=(
            "후보 목록 UI 배지: similar(유사), partial(일부 유사), "
            "different(상이), insufficient(근거 부족). 숫자 점수로 환산하지 않는다."
        ),
        examples=["partial"],
    )
    comparison_summary: str | None = Field(
        description="후보 목록에 보여줄 한 줄 비교 요약. 없으면 null.",
    )


class AnalysisSimSection(_ReadModel):
    # Pipeline-level status ("completed"/"failed"/...), independent of each
    # candidate's own comparison_status (spec section 9.1).
    status: Literal["completed", "skipped"] = Field(
        description=(
            "SIM 후보 탐색 파이프라인 상태. completed는 검색 수행 완료, skipped는 "
            "검색 미수행 상태다. skipped의 세부 원인은 reason_code로 구분하며 후보별 "
            "comparison_status와 별개다."
        )
    )
    reason_code: str | None = Field(description="SIM 파이프라인 상태 사유 코드. 없으면 null.")
    summary: str | None = Field(description="SIM 후보 탐색 결과의 표시용 요약. 없으면 null.")
    candidates: list[AnalysisSimCandidateSummary] = Field(
        description="유사 공고 후보 목록. 각 항목의 sim_candidate_id로 후보 상세 API를 호출해 metadata/comparison/axes/evidences를 렌더링한다.",
    )


class ReportReadModel(_ReadModel):
    status: Literal["generating", "ready", "failed"] = Field(
        description="보고서 상태: generating(생성 중), ready(생성 완료), failed(생성 실패)."
    )
    can_download: bool = Field(
        description="보고서가 ready이고 저장 경로도 준비되어 true일 때만 다운로드 동작을 활성화한다."
    )
    can_regenerate: bool = Field(
        description="true이면 보고서 재생성 동작을 활성화한다. 현재 서버는 false를 반환한다."
    )
    retry_count: int = Field(ge=0, description="보고서 생성 재시도 횟수.")


class AnalysisSessionReadModel(_ReadModel):
    analysis_session_id: UUID | None = Field(
        description="결과에 연결된 분석 세션 ID. 세션이 없으면 null."
    )
    is_active: bool = Field(description="세션이 현재 active이고 만료 전이면 true.")
    # This is session metadata.  It does not imply that a chat HTTP API is
    # currently mounted.
    can_chat: bool = Field(description="true이면 새 질문과 실패 답변 재시도를 허용한다.")
    expires_at: datetime | None = Field(description="세션 만료 시각. 세션이 없으면 null.")


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
    message: str | None = Field(
        description="모델 1 참고 문구. 제공할 수 없는 legacy 결과이면 null."
    )
    reason_code: str | None = Field(exclude=True)


class MlModel2ReadModel(_ReadModel):
    status: Literal["OK", "UNAVAILABLE", "FAILED"] = Field(exclude=True)
    predicted_amount_won: int | None = Field(exclude=True)
    message: str | None = Field(
        description="모델 2 참고 문구. 제공할 수 없는 legacy 결과이면 null."
    )
    reason_code: str | None = Field(exclude=True)


class MlModel3ReadModel(_ReadModel):
    status: Literal["OK", "UNAVAILABLE", "FAILED"] = Field(exclude=True)
    anomaly_level: str | None = Field(exclude=True)
    cause_axes: list[str] = Field(default_factory=list, exclude=True)
    message: str | None = Field(
        description="모델 3 참고 문구. 제공할 수 없는 legacy 결과이면 null."
    )
    reason_code: str | None = Field(exclude=True)


class MlReferenceReadModel(_ReadModel):
    model_1: MlModel1ReadModel
    model_2: MlModel2ReadModel
    model_3: MlModel3ReadModel


class ResultEvidenceReadModel(_ReadModel):
    """CPL/FIT 판단 근거. CPL/FIT의 evidence_ids와 evidence_id로 조인한다."""

    evidence_id: UUID = Field(description="CPL/FIT detail의 evidence_ids가 참조하는 근거 고유 ID.")
    side: Literal["request"] = Field(
        description="CPL/FIT 근거 출처. 두 축 모두 요청서 내부를 확인·비교하므로 request 고정값이다."
    )
    field_name: str | None = Field(description="근거가 나온 정규화 필드명. 없으면 null.")
    raw_value: str = Field(description="근거 원문 값.")
    excerpt: str | None = Field(description="화면에 강조 표시할 원문 문맥 발췌. 없으면 null.")


class AnalysisResultReadModel(_ReadModel):
    """완료된 분석 결과 화면 모델.

    CPL/FIT의 ``detail.evidence_ids`` 및 ``values[].evidence_ids``는 이 모델의
    ``evidences[].evidence_id``에 조인한다. SIM 후보 근거는 섞이지 않으며 후보
    상세 API의 ``evidences``에서만 제공한다.
    """

    case: AnalysisCaseSummary = Field(description="분석 결과 제목과 식별 정보.")
    cpl: AnalysisCplSection = Field(description="CPL 판단 축과 근거 조인 ID.")
    fit: AnalysisFitSection = Field(description="FIT 비교 축과 근거 조인 ID.")
    sim: AnalysisSimSection = Field(description="유사 공고 후보 목록. 상세는 후보 상세 API로 조회한다.")
    ml: MlReferenceReadModel = Field(description="모델 1·2·3의 화면 표시용 참고 문구.")
    report: ReportReadModel = Field(description="보고서 생성·다운로드 가능 상태.")
    session: AnalysisSessionReadModel = Field(description="현재 결과의 대화 세션 상태.")
    # CPL/FIT evidence only: sim_candidate_pk IS NULL. SIM evidence never
    # appears here — it is only ever returned from its own candidate detail,
    # so evidence from different candidates can never be cross-referenced
    # (spec section 8.2).
    evidences: list[ResultEvidenceReadModel] = Field(
        description=(
            "CPL/FIT 전용 근거 풀. CPL/FIT detail의 evidence_ids와 evidence_id로 "
            "조인해 원문/발췌를 표시한다. SIM 후보 근거는 포함하지 않는다."
        ),
    )


# --- Similar-notice candidate detail (spec section 9.2) ---------------------


class SimCandidateMetadata(_ReadModel):
    """Exact snapshot of the Existing profile/source at analysis time.

    Never re-joined against the current KB row — the metadata a user sees
    must match what was actually compared, even if the source notice is
    edited or removed afterward.
    """

    title: str | None = Field(description="상세 화면의 후보 공고 제목. 없으면 null.")
    support_field: str | None = Field(description="지원 분야 표시값. 없으면 null.")
    apply_period: str | None = Field(description="신청 기간 표시 문자열. 없으면 null.")
    ministry: str | None = Field(description="소관 부처 표시값. 없으면 null.")
    executing_agency: str | None = Field(description="수행/전담 기관 표시값. 없으면 null.")
    registered_at: str | None = Field(description="후보 공고 등록일 표시 문자열. 없으면 null.")
    notice_status: str | None = Field(description="후보 공고 상태 표시값. 없으면 null.")
    source_url: str | None = Field(description="원문 공고로 이동할 URL. 없으면 null.")


class SimCandidateComparison(_ReadModel):
    """후보 상세 상단의 전체 비교 결과. delivery는 이 상태 계산에 포함되지 않는다."""

    status: SimAxisStatus = Field(
        description=(
            "상세 상단 UI 배지: similar/partial/different/insufficient. "
            "숫자 유사도 점수가 아니다."
        ),
        examples=["partial"],
    )
    summary: str | None = Field(description="상세 상단에 표시할 전체 비교 요약. 없으면 null.")
    # Only the three core axes feed the overall comparison status
    # (insufficient > different > partial > similar); delivery is shown
    # separately and never changes this value (spec section 9.2).
    comparable_axes: list[Literal["purpose", "target", "support"]] = Field(
        description=(
            "전체 상태 계산에 실제 비교된 핵심 축 목록. delivery는 표시만 하며 "
            "전체 상태에는 반영하지 않는다."
        ),
    )


class SimCandidateAxisDetail(_ReadModel):
    code: str = Field(description="SIM 축 코드.", examples=["SIM-1"])
    status: SimAxisStatus = Field(
        description=(
            "축별 UI 배지: similar(유사), partial(일부 유사), different(차이), "
            "insufficient(근거 부족). 숫자 점수로 환산하지 않는다."
        ),
        examples=["similar"],
    )
    summary: str | None = Field(description="축별 짧은 표시 요약. 없으면 null.")
    reason_code: str | None = Field(description="기계 판독용 축별 판단 사유 코드. 없으면 null.")
    reason: str | None = Field(description="상세 화면에 표시할 축별 판단 근거. 없으면 null.")
    common_points: list[str] = Field(description="두 공고의 공통점 표시 목록.")
    differences: list[str] = Field(description="두 공고의 차이점 표시 목록.")
    request_evidence_ids: list[UUID] = Field(
        description=(
            "요청서(request) 근거 ID. 이 후보 상세 응답의 "
            "evidences[].evidence_id와 조인한다."
        ),
    )
    existing_evidence_ids: list[UUID] = Field(
        description=(
            "기존 공고(existing) 근거 ID. 이 후보 상세 응답의 "
            "evidences[].evidence_id와 조인한다."
        ),
    )


class SimCandidateAxes(_ReadModel):
    # Internal "content" axis is exposed publicly only as ``support``.
    purpose: SimCandidateAxisDetail = Field(description="목적 축 상세.")
    target: SimCandidateAxisDetail = Field(description="지원 대상 축 상세.")
    support: SimCandidateAxisDetail = Field(description="지원 내용 축 상세. 내부 content 축의 공개 이름이다.")
    delivery: SimCandidateAxisDetail = Field(description="전달/수행 방식 축 상세. 전체 comparison.status 계산에는 포함되지 않는다.")


class CandidateEvidenceReadModel(ResultEvidenceReadModel):
    evidence_id: UUID = Field(
        description="SIM 축의 request_evidence_ids 또는 existing_evidence_ids가 참조하는 후보 전용 근거 ID."
    )
    side: Literal["request", "existing"] = Field(
        description="SIM 근거 출처: request(요청서) 또는 existing(비교 후보 기존 공고)."
    )
    axis_type: str | None = Field(description="근거가 연결된 비교 축 유형. 현재 SIM 또는 null.")


class SimCandidateDetailReadModel(_ReadModel):
    """유사 공고 후보 상세 렌더링 모델.

    결과의 ``sim.candidates[]`` 항목에서 받은 ``sim_candidate_id``로 조회한다.
    metadata는 공고 카드, comparison은 상세 상단 배지, axes는 축별 근거/차이,
    evidences는 axes의 request_evidence_ids·existing_evidence_ids 조인에 사용한다.
    """

    sim_candidate_id: UUID = Field(description="조회한 후보 ID.")
    analysis_case_id: UUID = Field(description="이 후보가 속한 분석 case ID.")
    rank: int = Field(description="결과 후보 목록에서의 표시 순서(1이 첫 번째). 점수가 아니다.")
    metadata: SimCandidateMetadata = Field(description="후보 공고 카드/정보 영역 렌더링 필드.")
    comparison: SimCandidateComparison = Field(description="상세 상단 전체 비교 배지와 요약.")
    axes: SimCandidateAxes = Field(description="목적·대상·지원·전달 방식별 비교 근거와 표시 내용.")
    evidences: list[CandidateEvidenceReadModel] = Field(
        description=(
            "이 후보 전용 근거 풀. axes의 요청서/기존공고 evidence ID와 조인한다. "
            "다른 후보나 CPL/FIT 근거와 섞지 않는다."
        ),
    )


class ActiveAnalysisSessionReadModel(_ReadModel):
    """기존 화면 호환을 위한 활성 세션 읽기 모델. 새 화면은 /analysis/current를 사용한다."""

    analysis_session_id: UUID = Field(description="현재 활성 분석 세션 ID.")
    analysis_case_id: UUID = Field(description="결과·대화 조회에 사용할 분석 case ID.")
    program_name: str | None = Field(description="결과 화면 사업명. 없으면 null.")
    original_filename: str | None = Field(description="업로드한 요청서 파일명. 없으면 null.")
    session_expires_at: datetime = Field(description="현재 세션 만료 시각(ISO 8601).")


class AnalysisHistoryEntryReadModel(_ReadModel):
    analysis_case_id: UUID = Field(description="과거 결과/대화 상세 조회에 사용할 분석 case ID.")
    program_name: str | None = Field(description="이력 카드에 표시할 사업명. 없으면 null.")
    original_filename: str | None = Field(description="이력 카드에 표시할 업로드 파일명. 없으면 null.")
    completed_at: datetime = Field(description="분석 완료 시각(ISO 8601). 최신 완료 건부터 정렬된다.")


class AnalysisHistoryEnvelope(_ReadModel):
    """완료·종료된 분석만 담는 고정 5건 cursor 이력 페이지."""

    items: list[AnalysisHistoryEntryReadModel] = Field(
        description="완료·종료된 과거 분석 최대 5건. 현재 활성 세션은 이력에 포함하지 않는다.",
    )
    next_cursor: str | None = Field(
        description="다음 고정 5건을 위한 서명된 불투명 cursor. 값 변경 없이 cursor 쿼리에 그대로 전달하며, null이면 마지막 페이지다.",
    )


# --- GET /analysis/current discriminated union (spec section 5.4) ----------


class CurrentProcessingRun(_ReadModel):
    analysis_run_id: UUID = Field(description="진행 중인 분석 run ID.")
    status: Literal["uploading", "queued", "running"] = Field(
        description="진행 단계: uploading(업로드 중), queued(대기 중), running(분석 중)."
    )
    original_filename: str | None = Field(description="진행 화면에 표시할 업로드 파일명. 없으면 null.")
    created_at: datetime = Field(description="분석 run 생성 시각(ISO 8601).")
    updated_at: datetime = Field(description="분석 run 마지막 상태 변경 시각(ISO 8601).")


class CurrentReadySession(_ReadModel):
    analysis_session_id: UUID = Field(description="열려 있는 세션 ID. 새 분석 시작 전 정확한 ID로 close API를 호출할 때 사용한다.")
    analysis_case_id: UUID = Field(description="결과 및 과거 대화 조회에 사용할 case ID.")
    program_name: str | None = Field(description="현재 결과 화면 제목. 없으면 null.")
    original_filename: str | None = Field(description="현재 분석의 업로드 파일명. 없으면 null.")
    session_expires_at: datetime = Field(description="활성 세션 만료 시각(ISO 8601).")


class AnalysisCurrentProcessing(_ReadModel):
    """UI: 분석 진행 화면을 유지하고 결과/대화 화면으로 진입하지 않는다."""

    state: Literal["processing"] = Field(
        description=(
            "UI 상태 processing: 업로드·대기·분석 진행 중이므로 run으로 진행 정보를 표시한다."
        ),
        examples=["processing"],
    )
    run: CurrentProcessingRun = Field(description="진행 화면에 렌더링할 run 정보.")
    session: None = Field(description="processing에서는 항상 null.")


class AnalysisCurrentReady(_ReadModel):
    """UI: 기존 결과/대화 화면을 열 수 있는 활성 세션이 있다."""

    state: Literal["ready"] = Field(description="UI 상태 ready: session의 case ID로 기존 결과/대화 화면을 연다.", examples=["ready"])
    run: None = Field(description="ready에서는 항상 null.")
    session: CurrentReadySession = Field(description="열람 가능한 활성 세션과 새 분석 전 종료에 쓸 정확한 세션 ID.")


class AnalysisCurrentIdle(_ReadModel):
    """UI: 진행 중인 run과 활성 세션이 없으므로 업로드/새 분석 시작 화면을 보여준다."""

    state: Literal["idle"] = Field(
        description=(
            "UI 상태 idle: 진행 중 분석과 활성 세션이 없으므로 업로드/새 분석 시작 화면을 표시한다."
        ),
        examples=["idle"],
    )
    run: None = Field(description="idle에서는 항상 null.")
    session: None = Field(description="idle에서는 항상 null.")


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
    description=(
        "완료된 분석 결과를 조회합니다. CPL/FIT의 `detail.evidence_ids`와 "
        "`values[].evidence_ids`는 응답 최상위 `evidences[].evidence_id`에 조인해 "
        "원문 근거를 표시합니다. CPL/FIT/SIM status는 UI 배지용 분류값이며 숫자 점수로 "
        "환산하지 않습니다. SIM 후보 상세·근거는 후보 ID로 별도 API에서 조회합니다."
    ),
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
    description=(
        "분석 결과의 `sim.candidates[]`에서 받은 `sim_candidate_id`로 호출합니다. "
        "`metadata`는 공고 정보 카드, `comparison`은 상세 상단 배지/요약, `axes`는 "
        "축별 공통점·차이·판단 근거, `evidences`는 축별 evidence ID 조인에 사용합니다. "
        "SIM status는 UI 배지용 분류값이고 숫자 유사도 점수가 아닙니다."
    ),
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
    description=(
        "기존 클라이언트 호환용 읽기 전용 endpoint입니다. 새 화면의 상태 분기는 "
        "GET /analysis/current를 사용합니다. 이 경로에는 close 동작이나 "
        "/active/close 별칭이 없습니다."
    ),
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
    description=(
        "프론트 최초 진입 시 단일 상태를 조회합니다. `processing`이면 `run`으로 업로드/대기/분석 "
        "진행 화면을 유지하고 결과·대화 화면은 열지 않습니다. `ready`이면 `session.analysis_case_id`로 "
        "기존 결과·대화 화면을 엽니다. `idle`이면 `run`과 `session`이 모두 null이므로 업로드/새 분석 "
        "시작 화면을 표시합니다."
    ),
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
    description=(
        "새 분석을 시작할 때 `GET /analysis/current`의 `ready.session.analysis_session_id`처럼 "
        "명시적으로 받은 정확한 세션 ID를 경로에 넣어 호출합니다. 성공하면 204이며, 같은 소유자의 "
        "같은 ID를 다시 종료해도 204입니다(idempotent). 성공 후 기존 결과는 과거 이력으로 조회합니다. "
        "`/analysis-sessions/active/close` 별칭은 제공하지 않습니다."
    ),
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
    description=(
        "완료·종료된 과거 분석만 최신 완료 순으로 고정 5건씩 반환합니다. 현재 활성 세션은 포함하지 "
        "않습니다. `next_cursor`는 사용자·endpoint·스냅샷에 서명된 불투명 값이므로 수정하지 말고 다음 "
        "요청의 `cursor`에 그대로 전달합니다. `next_cursor`가 null이면 마지막 페이지입니다."
    ),
    dependencies=[Security(access_cookie_scheme)],
    responses=error_responses(401, 403, 422, 429, 500, 502, 503),
)
async def list_analysis_history(
    request: Request,
    principal: PrincipalDep,
    repository: ResultRepositoryDep,
    cursor: Annotated[
        str | None,
        Query(
            max_length=2_000,
            description="이전 응답의 서명된 next_cursor를 변경 없이 그대로 전달한다. 임의 생성·수정하거나 다른 사용자/endpoint cursor를 사용하면 422다.",
        ),
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
