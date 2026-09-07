"""Slice 4a: 공통 SIM 비교 프로파일 → 4축 유사성 판정 (초안 §7.2, §9.2).

입력은 ``sim_inputs.build_common_profile`` 이 만든 공통 프로파일 두 개다. KB
적재·임베딩·벡터 검색·DB 접근은 이 슬라이스에 없다 (Slice 4b). 여기서 하는
일은 게이트 → 축별 의미 비교 → 격리 → 내부 순위, 네 가지뿐이다.

**``InternalRanking`` 의 점수·등급은 내부 순위 전용이다. 사용자 응답·리포트로
내보내지 않는다.** 초안 §7.2: "내부 순위 계산은 사용자에게 퍼센트·중복 확률로
노출하지 않는다." 축 결과(``SimAxisResult``)에는 점수 필드가 아예 없다.

게이트는 payload 를 만들기 전에 돈다. 걸린 축은 LLM 을 호출하지 않고
``INSUFFICIENT`` + reason code 로 남으며, **절대 ``DIFFERENT`` 가 되지 않는다.**
초안 §7.2: "후보의 정보 부족과 낮은 유사도를 구분한다."
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.ports.llm_client import (
    LLMClient,
    LLMInvalidResponseError,
    LLMTimeoutError,
    LLMUnavailableError,
)

from .contracts.sim_result import (
    CANDIDATE_EVIDENCE_MISSING,
    LLM_INVALID_RESPONSE,
    LLM_TIMEOUT,
    LLM_UNAVAILABLE,
    NO_MEANING_OVERLAP,
    PARTIAL_OVERLAP,
    REQUEST_EVIDENCE_MISSING,
    SIM_AXIS_IDS,
    SIM_VERDICT_REASON_CODES,
    STRUCTURING_INCOMPLETE,
    InternalRanking,
    SimAxis,
    SimAxisResult,
    SimCandidateResult,
    SimCommonProfile,
    SimComparisonResult,
    SimReviewGrade,
    SimStatus,
    StageDiagnostic,
)
from .sim_inputs import SIM_RULESET_VERSION, generate

__all__ = [
    "SIM_COMPARISON_PROMPT_VERSION",
    "SIM_SCORING_VERSION",
    "compare_candidate",
    "compare_candidates",
]

_STAGE = "compare_sim_axes"
_COMPARISON_TASK = "sim_axis_comparison"

SIM_COMPARISON_PROMPT_VERSION = "sim-axis-comparison-v0.1"
SIM_SCORING_VERSION = "sim-alpha-v0.2"

# backend/config/sim_scoring.json 의 sim-alpha-v0.2 정책을 그대로 옮긴 값이다.
# ponytail: 워커가 실행 시점에 설정 파일을 읽게 만들지 않는다. 대신 테스트가
# 두 값이 어긋나면 터지도록 고정한다.
_AXIS_WEIGHTS: dict[SimAxis, float] = {
    SimAxis.PURPOSE: 0.30,
    SimAxis.TARGET: 0.30,
    SimAxis.CONTENT: 0.30,
    SimAxis.DELIVERY: 0.10,
}
_STATUS_SCORES: dict[SimStatus, int | None] = {
    SimStatus.SIMILAR: 100,
    SimStatus.PARTIAL: 50,
    SimStatus.DIFFERENT: 0,
    SimStatus.INSUFFICIENT: None,
}
_FOCUS_REVIEW_MIN = 70
_GENERAL_REVIEW_MIN = 40

# 핵심 축. 하나라도 정보 부족이면 순위 계산 자체를 하지 않는다.
_CORE_AXES = (SimAxis.PURPOSE, SimAxis.TARGET, SimAxis.CONTENT)

_AXIS_QUESTION = {
    SimAxis.PURPOSE: "두 사업이 같은 문제 영역에서 같은 방향의 목적을 말하는가.",
    SimAxis.TARGET: "두 사업의 대상 조건이 같은 집단을 가리키는가.",
    SimAxis.CONTENT: "두 사업의 지원 활동·수단·품목이 같은 내용인가.",
    SimAxis.DELIVERY: "두 사업의 수행기관·전달 방식·절차·역할이 같은 방식인가.",
}


# ------------------------------------------------------------- 사전 게이트


def _gate(
    axis: SimAxis, request: SimCommonProfile, candidate: SimCommonProfile
) -> str | None:
    """payload 를 만들기 전 검사. 통과하면 None, 걸리면 reason code 다.

    세 사유를 구분한다. 요청서 쪽 부재, 공고 쪽 부재, 그리고 원문은 있는데
    상위 구조화가 근거 참조를 확정하지 못한 경우다. 공고 v0.2 는
    ``delivery_relations`` · ``delivery_methods`` 컨테이너 자체가 없으므로
    SIM-4 가 항상 ``CANDIDATE_EVIDENCE_MISSING`` 으로 걸린다. 이것은 "덜
    비슷하다" 가 아니라 "볼 근거가 없다" 다.
    """

    request_entries = request.entries(axis)
    candidate_entries = candidate.entries(axis)
    if not request_entries:
        return REQUEST_EVIDENCE_MISSING
    if not candidate_entries:
        return CANDIDATE_EVIDENCE_MISSING
    for profile, entries in ((request, request_entries), (candidate, candidate_entries)):
        if any(entry.fact_id not in profile.fact_id_registry for entry in entries):
            return STRUCTURING_INCOMPLETE
    return None


# ------------------------------------------------------------- 의미 비교 호출


class _AxisVerdictModel(BaseModel):
    axis: str
    status: str
    reason_code: str | None = None
    request_fact_ids: list[str] = Field(default_factory=list)
    candidate_fact_ids: list[str] = Field(default_factory=list)
    common_points: list[str] = Field(default_factory=list)
    differences: list[str] = Field(default_factory=list)


class _SimComparisonResponse(BaseModel):
    axes: list[_AxisVerdictModel] = Field(default_factory=list)


_COMPARISON_INSTRUCTION = (
    "You compare one Korean support-program request against one public announcement, "
    "axis by axis, using only the grounded entries in the payload. "
    "For each axis return {axis, status, reason_code, request_fact_ids, "
    "candidate_fact_ids, common_points, differences}. "
    f"status must be one of {[status.value for status in SimStatus]}. "
    f"reason_code must be one of {sorted(SIM_VERDICT_REASON_CODES)} or null. "
    "Cite only fact_ids that appear on that axis's own side in the payload, and cite "
    "at least one on each side unless the status is INSUFFICIENT. "
    "Never invent a fact_id, a value, or an axis that was not asked for. "
    "Do not return any score, percentage, ratio, probability, or grade."
)

_TRANSPORT_REASONS = {
    LLMTimeoutError: LLM_TIMEOUT,
    LLMUnavailableError: LLM_UNAVAILABLE,
    LLMInvalidResponseError: LLM_INVALID_RESPONSE,
}


def _payload(
    pending: dict[SimAxis, None],
    request: SimCommonProfile,
    candidate: SimCommonProfile,
    errors: dict[SimAxis, str],
) -> dict[str, Any]:
    def rows(profile: SimCommonProfile, axis: SimAxis) -> list[dict[str, Any]]:
        return [
            {
                "fact_id": entry.fact_id,
                "common_key": entry.common_key,
                "source_field": entry.source_field,
                "value_raw": entry.value_raw,
            }
            for entry in profile.entries(axis)
        ]

    axes = []
    for axis in pending:
        entry: dict[str, Any] = {
            "axis": axis.value,
            "axis_id": SIM_AXIS_IDS[axis],
            "question": _AXIS_QUESTION[axis],
            "request": rows(request, axis),
            "candidate": rows(candidate, axis),
        }
        if axis in errors:
            entry["previous_response_error"] = errors[axis]
        axes.append(entry)
    return {"axes": axes}


def _validate(
    row: _AxisVerdictModel,
    axis: SimAxis,
    request: SimCommonProfile,
    candidate: SimCommonProfile,
) -> tuple[SimStatus, str | None, list[str], list[str], list[str], list[str]] | str:
    """응답 한 줄을 검사한다. 통과하면 판정 튜플, 아니면 오류 설명이다."""

    try:
        status = SimStatus(row.status)
    except ValueError:
        return f"알 수 없는 status {row.status!r}"
    request_ids = {entry.fact_id for entry in request.entries(axis)}
    candidate_ids = {entry.fact_id for entry in candidate.entries(axis)}
    unknown = [
        fact_id for fact_id in row.request_fact_ids if fact_id not in request_ids
    ] + [fact_id for fact_id in row.candidate_fact_ids if fact_id not in candidate_ids]
    if unknown:
        return f"허용 밖 근거 참조 {unknown}"
    if status is not SimStatus.INSUFFICIENT and not (
        row.request_fact_ids and row.candidate_fact_ids
    ):
        return "판정 가능한 축인데 한쪽 근거를 인용하지 않았다"

    reason = row.reason_code if row.reason_code in SIM_VERDICT_REASON_CODES else None
    if status is not SimStatus.SIMILAR and reason is None:
        # 어휘 밖 reason 은 버리되, 상태에서 곧바로 따라 나오는 이름은 붙인다.
        # 근거를 지어내는 것이 아니라 상태에 라벨을 다는 것이다.
        reason = {
            SimStatus.PARTIAL: PARTIAL_OVERLAP,
            SimStatus.DIFFERENT: NO_MEANING_OVERLAP,
            SimStatus.INSUFFICIENT: LLM_INVALID_RESPONSE,
        }[status]
    return (
        status,
        reason,
        list(row.request_fact_ids),
        list(row.candidate_fact_ids),
        list(row.common_points),
        list(row.differences),
    )


def _compare_axes(
    llm_client: LLMClient,
    pending: dict[SimAxis, None],
    request: SimCommonProfile,
    candidate: SimCommonProfile,
    *,
    model_profile: str,
    max_repairs: int,
    diagnostics: list[StageDiagnostic],
) -> dict[SimAxis, SimAxisResult]:
    """게이트를 통과한 축을 한 번에 비교하고, 결함만 격리한다 (초안 §9.2.1).

    - 최상위 응답 자체를 해석할 수 없으면 그 호출 범위(= 남은 축)만 내린다.
    - 축을 식별할 수 있는 누락·중복·허용 밖 근거는 그 축만 내린다.
    - 통신 오류는 이미 확정된 축 결과를 지우지 않는다.
    """

    results: dict[SimAxis, SimAxisResult] = {}
    errors: dict[SimAxis, str] = {}
    remaining = dict(pending)
    attempt = 0
    while remaining and attempt <= max_repairs:
        attempt += 1
        try:
            response = generate(
                llm_client,
                task_name=_COMPARISON_TASK,
                instructions=_COMPARISON_INSTRUCTION,
                payload=_payload(remaining, request, candidate, errors),
                response_schema=_SimComparisonResponse,
                model_profile=model_profile,
            )
        except (LLMTimeoutError, LLMUnavailableError, LLMInvalidResponseError) as error:
            reason = _TRANSPORT_REASONS[type(error)]
            for axis in remaining:
                diagnostics.append(
                    StageDiagnostic(
                        stage=_STAGE,
                        unit=SIM_AXIS_IDS[axis],
                        reason_code=reason,
                        message=str(error)[:2000],
                        attempt=attempt,
                        terminated_because=reason,
                    )
                )
                results[axis] = _insufficient(axis, reason)
            return results

        seen: dict[SimAxis, int] = {}
        rows: dict[SimAxis, _AxisVerdictModel] = {}
        for row in response.axes:
            try:
                axis = SimAxis(row.axis)
            except ValueError:
                diagnostics.append(
                    StageDiagnostic(
                        stage=_STAGE,
                        unit=row.axis,
                        reason_code=LLM_INVALID_RESPONSE,
                        message="요청하지 않은 축 이름이라 무시했다. 정상 축은 그대로 둔다.",
                        attempt=attempt,
                    )
                )
                continue
            seen[axis] = seen.get(axis, 0) + 1
            rows[axis] = row

        errors = {}
        for axis in remaining:
            row = rows.get(axis)
            if row is None:
                errors[axis] = "응답에 해당 축이 없다"
                continue
            if seen[axis] > 1:
                errors[axis] = "응답에 같은 축이 중복으로 있다"
                continue
            checked = _validate(row, axis, request, candidate)
            if isinstance(checked, str):
                errors[axis] = checked
                continue
            status, reason, left, right, common, different = checked
            results[axis] = SimAxisResult(
                axis=axis,
                axis_id=SIM_AXIS_IDS[axis],
                status=status,
                reason_code=reason,
                request_fact_ids=left,
                candidate_fact_ids=right,
                common_points=common,
                differences=different,
            )

        for axis, message in errors.items():
            diagnostics.append(
                StageDiagnostic(
                    stage=_STAGE,
                    unit=SIM_AXIS_IDS[axis],
                    reason_code=LLM_INVALID_RESPONSE,
                    message=message,
                    attempt=attempt,
                )
            )
        remaining = {axis: None for axis in errors}

    # 예산을 다 쓰고도 남은 축은 그 축만 정보 부족으로 남는다.
    for axis in remaining:
        results[axis] = _insufficient(axis, LLM_INVALID_RESPONSE)
    return results


def _insufficient(axis: SimAxis, reason: str) -> SimAxisResult:
    return SimAxisResult(
        axis=axis,
        axis_id=SIM_AXIS_IDS[axis],
        status=SimStatus.INSUFFICIENT,
        reason_code=reason,
    )


# ------------------------------------------------------------------ 내부 순위


def _internal_ranking(results: list[SimAxisResult]) -> InternalRanking:
    """내부 순위 전용 집계 (초안 §7.2). 사용자 표면으로 내보내지 않는다.

    핵심 축(SIM-1·2·3) 중 하나라도 정보 부족이면 점수를 만들지 않는다.
    근거가 없다는 사실을 낮은 점수로 바꿔치기하면 "정보 부족" 과 "덜 비슷함"
    이 다시 섞인다. 축 결과와 판정 가능 축 수는 그대로 보존된다.

    SIM-4 만 정보 부족이면 핵심 3축을 30/30/30 으로 재정규화한다. 공고 v0.2
    에는 전달체계 컨테이너가 없으므로 실제 데이터가 타는 경로가 이쪽이다.
    """

    assessable = [row for row in results if row.status is not SimStatus.INSUFFICIENT]
    count = len(assessable)
    by_axis = {row.axis: row.status for row in results}
    if any(by_axis.get(axis) is SimStatus.INSUFFICIENT for axis in _CORE_AXES):
        return InternalRanking(
            weighted_score=None,
            review_grade=SimReviewGrade.ON_HOLD,
            assessable_axis_count=count,
            scoring_version=SIM_SCORING_VERSION,
        )

    total = sum(_AXIS_WEIGHTS[row.axis] for row in assessable)
    weights = {row.axis.value: _AXIS_WEIGHTS[row.axis] / total for row in assessable}
    score = sum(_STATUS_SCORES[row.status] * weights[row.axis.value] for row in assessable)
    if score >= _FOCUS_REVIEW_MIN:
        grade = SimReviewGrade.FOCUS_REVIEW
    elif score >= _GENERAL_REVIEW_MIN:
        grade = SimReviewGrade.GENERAL_REVIEW
    else:
        grade = SimReviewGrade.LOW_PRIORITY
    return InternalRanking(
        weighted_score=score,
        review_grade=grade,
        assessable_axis_count=count,
        axis_weights=weights,
        scoring_version=SIM_SCORING_VERSION,
    )


# ------------------------------------------------------------------- 진입점


def compare_candidate(
    request_common: SimCommonProfile,
    candidate_common: SimCommonProfile,
    llm_client: LLMClient,
    *,
    model_profile: str,
    max_repairs: int = 1,
) -> SimCandidateResult:
    """후보 하나의 4축 결과. 예외를 던지지 않는다."""

    diagnostics: list[StageDiagnostic] = []
    results: dict[SimAxis, SimAxisResult] = {}
    pending: dict[SimAxis, None] = {}
    for axis in SimAxis:
        reason = _gate(axis, request_common, candidate_common)
        if reason is None:
            pending[axis] = None
            continue
        results[axis] = SimAxisResult(
            axis=axis,
            axis_id=SIM_AXIS_IDS[axis],
            status=SimStatus.INSUFFICIENT,
            reason_code=reason,
            diagnostics=[
                StageDiagnostic(
                    stage=_STAGE,
                    unit=SIM_AXIS_IDS[axis],
                    reason_code=reason,
                    message="비교 입력이 성립하지 않아 LLM 을 호출하지 않았다.",
                )
            ],
        )

    if pending:
        results.update(
            _compare_axes(
                llm_client,
                pending,
                request_common,
                candidate_common,
                model_profile=model_profile,
                max_repairs=max_repairs,
                diagnostics=diagnostics,
            )
        )

    axes = [results[axis] for axis in SimAxis]
    return SimCandidateResult(
        candidate_profile_id=candidate_common.source_profile_id,
        axes=axes,
        internal_ranking=_internal_ranking(axes),
        diagnostics=diagnostics,
    )


def compare_candidates(
    request_common: SimCommonProfile,
    candidate_commons: list[SimCommonProfile],
    llm_client: LLMClient,
    *,
    model_profile: str,
    max_repairs: int = 1,
) -> SimComparisonResult:
    """후보 목록을 하나씩 비교한다. 후보 하나가 실패해도 나머지는 남는다.

    후보 선정·검색은 이 슬라이스에 없다. 이미 만들어진 공통 프로파일을
    인자로 받는다 (KB·임베딩·벡터 검색은 Slice 4b).
    """

    return SimComparisonResult(
        request_profile_id=request_common.source_profile_id,
        candidates=[
            compare_candidate(
                request_common,
                candidate,
                llm_client,
                model_profile=model_profile,
                max_repairs=max_repairs,
            )
            for candidate in candidate_commons
        ],
        model_profile=model_profile,
        ruleset_version=SIM_RULESET_VERSION,
        prompt_version=SIM_COMPARISON_PROMPT_VERSION,
        scoring_version=SIM_SCORING_VERSION,
        diagnostics=list(request_common.diagnostics),
    )
