"""Slice 3: Request Profile v0.1.2 → FIT 7관계 판정 (초안 §7.1, §9.0–§9.2.1).

좌우 근거는 CPL이 확정한 ``CplResult`` 에서 만든다. 다른 관계의 판정 결과를
근거로 쓰지 않고, 없는 값을 만들어 비교를 성립시키지도 않는다. 그래서
``app.services.fit`` 아래의 옛 FIT 구현을 import 하지 않는다. 워커 CPL 계약이
확정한 값·상태·근거를 직접 소비하고, FastAPI 표시 모델을 다시 해석하지 않는다.
(테스트가 worker 소스에 그 모듈 경로 문자열이 없는지도 함께 고정한다.)

수단 배치는 초안 §7.1 표 그대로다.

- FIT-1·2·3·5·6: 의미 비교라 LLM. 표현이 열려 있어 Rule 로 닫히지 않는다.
- FIT-4: 비교 기준이 확정되지 않았다. 호출 자체를 하지 않는다.
- FIT-7: 정량 값 집합 비교라 Rule. LLM 으로 값을 추측하지 않는다.

점수·확인율·비율·등급은 계산하지 않는다.
"""

from __future__ import annotations


from typing import Any

from pydantic import BaseModel, Field

from worker.llm_call import generate as shared_generate, salvage_rows
from .ports.llm import (
    LLMClient,
    LLMInvalidResponseError,
    LLMTimeoutError,
    LLMUnavailableError,
)

from .analysis_inputs import field_name_of
from .quantities import comparison_key
from .contracts.cpl_result import CplFact, CplResult
from .contracts.fit_result import (
    COMPARISON_EVIDENCE_MISSING,
    COMPARISON_VALUE_INVALID,
    EVIDENCE_REF_UNRESOLVED,
    FIT_REASON_CODES,
    HIERARCHY_COMPARISON_NOT_AVAILABLE,
    LLM_INVALID_RESPONSE,
    LLM_TIMEOUT,
    LLM_UNAVAILABLE,
    NO_CONDITIONS_SPECIFIED,
    NUMERIC_MISMATCH,
    PURPOSE_AXIS_UNRESOLVED,
    PURPOSE_AXIS_CODES,
    SELF_COMPARISON,
    SINGLE_SIDED_NO_CONFLICT,
    FitEvidenceRef,
    FitRelationId,
    FitRelationResult,
    FitResult,
    FitSide,
    FitStatus,
    PurposeAxisAssignment,
    PurposeAxisClassification,
    CplAxisCode,
    StageDiagnostic,
)

__all__ = [
    "FIT_PROMPT_VERSION",
    "FIT_RULESET_VERSION",
    "analyze_fit",
]

_STAGE = "analyze_fit"

FIT_RULESET_VERSION = "fit-rules-v0.1"
FIT_PROMPT_VERSION = "fit-relations-v0.1"


# ------------------------------------------------------------ 입력 경로표

_PURPOSE_PATH = "comparison_profile.purpose_goal"
_TARGET_PATHS = (
    "comparison_profile.support_target",
    "comparison_profile.beneficiary",
)
# 초안 §7.1 FIT-5. 세 필드의 상태를 각각 본다: 미기재와 추출 실패는 다르다.
_CONDITION_PATHS = (
    "comparison_profile.eligibility_conditions",
    "comparison_profile.participation_requirements",
    "comparison_profile.exclusions",
)
_MEANS_PATHS = (
    "comparison_profile.support_activities",
    "comparison_profile.support_methods",
    "comparison_profile.support_items",
)
_EFFECT_PATHS = (
    "request_context.expected_effect",
    "request_context.performance_indicator",
)
_CONTENT_PATH = "comparison_profile.support_content"
_SCALE_PATH = "comparison_profile.support_scale"
_DELIVERY_PATH = "comparison_profile.delivery_relations"
_DELIVERY_METHODS_PATH = "comparison_profile.delivery_methods"

# 좌측이 목적 의미 축에서 오는 관계와, 그 관계가 요구하는 축.
_PURPOSE_AXIS_OF = {
    FitRelationId.FIT_1: CplAxisCode.TARGET_CONDITION,
    FitRelationId.FIT_2: CplAxisCode.DIRECTION,
    FitRelationId.FIT_3: CplAxisCode.DIRECTION,
}

# 관계별 한 줄 질문. LLM payload 에만 쓰이고 결과에는 실리지 않는다.
_RELATION_QUESTION = {
    FitRelationId.FIT_1: "목적이 말하는 대상 조건과 실제 지원 대상이 같은 대상을 가리키는가.",
    FitRelationId.FIT_2: "목적이 말하는 방향과 지원 활동·수단·품목이 같은 방향인가.",
    FitRelationId.FIT_3: "목적이 말하는 방향과 기대효과·성과지표가 같은 방향인가.",
    FitRelationId.FIT_5: "지원 대상군과 신청 조건이 같은 집단을 가리키는가.",
    FitRelationId.FIT_6: "수행기관과 그 역할·절차·전달 방식이 서로 맞물리는가.",
}


# ------------------------------------------------------------ 근거 만들기


def _facts_at(cpl: CplResult, path: str) -> list[CplFact]:
    """CPL이 확정한 한 프로파일 경로의 fact만 읽는다."""

    return [
        fact
        for item in cpl.items
        for subfield in item.subfields
        if subfield.profile_field == path
        for fact in subfield.facts
    ]


def _all_facts(cpl: CplResult) -> list[CplFact]:
    return [fact for item in cpl.items for subfield in item.subfields for fact in subfield.facts]


def _fit_fact_id(fact: CplFact) -> str | None:
    """CPL fact 좌표를 기존 FIT fact_id 표기로 옮긴다.

    일반 fact는 원래 id를 그대로 쓰고, id가 없는 delivery 멤버만 CPL이
    보존한 relation/member 좌표를 기존 공개 표기로 렌더링한다.
    """

    if fact.relation_id and fact.member in {"actor", "role"}:
        return f"{fact.relation_id}.{fact.member}"
    if (
        fact.relation_id
        and fact.member == "action"
        and fact.member_index is not None
    ):
        return f"{fact.relation_id}.actions[{fact.member_index}]"
    if fact.fact_id:
        return fact.fact_id
    # CPL 재검으로 복구한 값은 구조화가 만든 id 가 없다. 서버가 검증한 구역
    # 참조를 그대로 쓴다 — 식별자를 지어내지 않으면서 기존 FIT JSON 키를
    # 유지한다.
    return fact.evidence_ref


def _fact_id_registry(cpl: CplResult) -> set[str]:
    """CPL facts에 실제로 존재하는 id와 delivery relation id."""

    found: set[str] = set()
    for fact in _all_facts(cpl):
        if fact.fact_id:
            found.add(fact.fact_id)
        if fact.relation_id:
            found.add(fact.relation_id)
        rendered = _fit_fact_id(fact)
        if rendered:
            found.add(rendered)
    return found


def _base_id(fact_id: str) -> str:
    """``delivery:x.actor`` → ``delivery:x``. 프로파일 id 는 그대로 돌아온다."""

    return fact_id.split(".", 1)[0]


def _refs_at(cpl: CplResult, path: str) -> list[FitEvidenceRef]:
    """CPL fact를 기존 FIT 근거 모양으로 편다."""

    name = field_name_of(path)
    refs: list[FitEvidenceRef] = []
    for fact in _facts_at(cpl, path):
        fact_id = _fit_fact_id(fact)
        if fact_id:
            refs.append(
                FitEvidenceRef(
                    fact_id=fact_id,
                    field_name=name,
                    value_raw=fact.value_raw,
                    evidence=list(fact.evidence),
                    primary_component_id=fact.primary_component_id,
                    quantities=fact.quantities,
                )
            )
    return refs


def _side(cpl: CplResult, paths: tuple[str, ...]) -> FitSide:
    refs: list[FitEvidenceRef] = []
    for path in paths:
        refs.extend(_refs_at(cpl, path))
    return FitSide(field_names=[field_name_of(path) for path in paths], facts=refs)


def _delivery_sides(cpl: CplResult) -> tuple[FitSide, FitSide]:
    """FIT-6 의 좌우. ``delivery_relations`` 컨테이너 안에서 갈린다.

    actor 와 role 은 같은 relation 안에 있어 ``fact_id`` 가 없다. 컨테이너 id
    에 ``.actor`` / ``.role`` 접미사를 붙여 좌우를 구분하되, 접미사를 떼면
    프로파일의 실제 relation id 로 돌아간다. 여기서 새 id 를 발명하는 것이
    아니라 한 컨테이너의 두 자리를 이름 붙이는 것이다.

    ``actions`` 가 비어 있고 ``delivery_methods`` 가 없다는 사실은 모순이
    아니다. Rule 로 CONFLICT 를 만들지 않는다 (초안 §7.1 FIT-6).
    """

    left: list[FitEvidenceRef] = []
    right: list[FitEvidenceRef] = []
    for fact in _facts_at(cpl, _DELIVERY_PATH):
        fact_id = _fit_fact_id(fact)
        if not fact_id:
            continue
        if fact.member == "actor":
            field_name = "delivery_relations.actor"
            target = left
        elif fact.member in {"role", "action"}:
            field_name = (
                "delivery_relations.role"
                if fact.member == "role"
                else "delivery_relations.actions"
            )
            target = right
        else:
            continue
        target.append(
            FitEvidenceRef(
                fact_id=fact_id,
                field_name=field_name,
                value_raw=fact.value_raw,
                evidence=list(fact.evidence),
                primary_component_id=fact.primary_component_id,
                quantities=fact.quantities,
            )
        )
    right.extend(_refs_at(cpl, _DELIVERY_METHODS_PATH))
    return (
        FitSide(field_names=["delivery_relations.actor"], facts=left),
        FitSide(
            field_names=[
                "delivery_relations.role",
                "delivery_relations.actions",
                "delivery_methods",
            ],
            facts=right,
        ),
    )


# ------------------------------------------------------------- 사전 게이트


def _gate(left: FitSide, right: FitSide, registry: set[str]) -> str | None:
    """LLM payload 를 만들기 전 검사 (초안 §7.1 마지막 문단).

    통과하면 None, 걸리면 reason code 다. 걸린 관계는 호출하지 않는다.
    참조 검사가 의미적 타당성을 증명하는 것은 아니다 — 초안이 그렇게 적어 둔
    대로, 여기서 보는 것은 "비교 입력이 성립하는가" 까지다.
    """

    if not left.facts or not right.facts:
        return COMPARISON_EVIDENCE_MISSING
    left_ids = {ref.fact_id for ref in left.facts}
    if left_ids & {ref.fact_id for ref in right.facts}:
        return SELF_COMPARISON
    if any(
        _base_id(ref.fact_id) not in registry
        for ref in (*left.facts, *right.facts)
    ):
        return EVIDENCE_REF_UNRESOLVED
    return None


def _fit5_reason(cpl: CplResult, left: FitSide) -> str | None:
    """FIT-5 만의 부재·실패 구분 (초안 §7.1 "단순 조건 미기재와 추출 실패").

    문서에 있는 조건을 "조건 없음" 으로 단정하지 않는다. 그래서 상태가
    ``extraction_failed`` / ``mentioned_unresolved`` 이면 부재가 아니라 근거
    확보 실패로 남긴다.
    """

    if not left.facts:
        return COMPARISON_EVIDENCE_MISSING
    statuses = [
        subfield.status
        for path in _CONDITION_PATHS
        for item in cpl.items
        for subfield in item.subfields
        if subfield.profile_field == path
    ]
    if any(status in ("extraction_failed", "mentioned_unresolved") for status in statuses):
        return COMPARISON_EVIDENCE_MISSING
    if all(status == "not_found" for status in statuses):
        return NO_CONDITIONS_SPECIFIED
    return None


# --------------------------------------------------------- FIT-7 정량 비교
#
# 정량 표현을 읽고 비교 맥락을 붙이는 일은 ``worker.quantities`` 가 하고, CPL 이
# 그것을 fact 에 붙여 둔다. 여기서는 그 결과를 읽기만 한다 — 원문을 다시 파싱하면
# 같은 문법이 두 벌이 되어 한쪽만 고쳐지는 상태가 생긴다.


def _span_place(ref: FitEvidenceRef, span) -> tuple[str | None, int, int]:
    """숫자 구간의 원문 자리. 같은 자리면 같은 근거다."""

    block = ref.evidence[0].source_block_id if ref.evidence else None
    return (block, span.start, span.end)


def _shared_spans(
    left: list[FitEvidenceRef], right: list[FitEvidenceRef]
) -> frozenset[tuple[str | None, int, int]]:
    """좌우가 함께 인용한 원문 구간. fact_id 가 달라도 자리가 같으면 같다."""

    def places(refs: list[FitEvidenceRef]) -> set[tuple[str | None, int, int]]:
        return {
            _span_place(ref, span)
            for ref in refs
            for span in ref.quantities
            if ref.evidence and ref.evidence[0].source_block_id
        }

    return frozenset(places(left) & places(right))


def _axis_values(
    refs: list[FitEvidenceRef],
    shared: frozenset[tuple[str | None, int, int]] | None = None,
) -> tuple[
    dict[str | None, dict[tuple[str, tuple[str, ...]], set[int]]],
    list[str],
    list[str],
]:
    """component → 축 → 값 집합. 정규화하지 못한 fact_id 를 함께 돌려준다.

    ``primary_component_id`` 가 None 인 fact 는 None 그룹에 남는다. 초안 §7.1
    이 "총사업비와 직접 지원금을 자동 비교하지 않는다" 라고 못박은 것과 같은
    이유로, 소속이 없는 값을 특정 component 값과 붙이지 않는다.
    """

    grouped: dict[str | None, dict[tuple[str, tuple[str, ...]], set[int]]] = {}
    invalid: list[str] = []
    withheld: list[str] = []
    excluded = shared or frozenset()
    for ref in refs:
        if not ref.quantities:
            invalid.append(ref.fact_id)
            continue
        used = False
        for span in ref.quantities:
            if _span_place(ref, span) in excluded:
                # 좌우가 같은 자리를 인용했다. 숫자 구간 단위로만 뺀다 — 같은
                # occurrence 의 다른 숫자까지 통째로 버리지 않는다.
                continue
            context = comparison_key(span)
            if context is None:
                # 값은 읽었지만 같은 수량이라고 말할 근거가 없다. 원문 부재와
                # 다르므로 따로 센다. 기본값을 채워 비교에 넣지 않는다.
                continue
            used = True
            axes = grouped.setdefault(ref.primary_component_id, {})
            axes.setdefault((span.axis, context), set()).add(span.value)
        if not used:
            withheld.append(ref.fact_id)
    return grouped, invalid, withheld


def _fit7(cpl: CplResult) -> FitRelationResult:
    """FIT-7: 같은 component 안에서 같은 축의 값 집합을 비교한다.

    ``total_budget`` · ``cost_sharing`` 은 입력이 아니다 (초안 §7.1).
    대표값을 고르지 않고 집합을 통째로 비교한다. 대표값을 고르는 순간
    "400만원 vs 150만원" 같은 분할 지급 단계액이 거짓 충돌이 된다.
    """

    left = _side(cpl, (_CONTENT_PATH,))
    right = _side(cpl, (_SCALE_PATH,))
    diagnostics: list[StageDiagnostic] = []

    def result(status: FitStatus, reason: str | None) -> FitRelationResult:
        return FitRelationResult(
            relation_id=FitRelationId.FIT_7,
            status=status,
            reason_code=reason,
            left=left,
            right=right,
            diagnostics=diagnostics,
        )

    if not left.facts or not right.facts:
        return result(FitStatus.INSUFFICIENT, COMPARISON_EVIDENCE_MISSING)

    shared = _shared_spans(left.facts, right.facts)
    if shared:
        diagnostics.append(
            StageDiagnostic(
                stage=_STAGE,
                unit=sorted(shared)[0][0] or "",
                reason_code=SELF_COMPARISON,
                message=(
                    "좌우가 같은 원문 구간을 인용했다. 그 값은 자기 자신과 "
                    "비교되므로 제외한다. 같은 문구라도 자리가 다르면 남긴다."
                ),
            )
        )
    left_groups, left_invalid, left_withheld = _axis_values(left.facts, shared)
    right_groups, right_invalid, right_withheld = _axis_values(right.facts, shared)
    for fact_id in (*left_withheld, *right_withheld):
        # 숫자는 읽었는데 같은 수량이라고 말할 근거가 없다. 정규화 실패와 구분해
        # 내부 진단으로만 남긴다 — 공개 reason code 연결은 아직 확정 전이다.
        diagnostics.append(
            StageDiagnostic(
                stage=_STAGE,
                unit=fact_id,
                reason_code=COMPARISON_EVIDENCE_MISSING,
                message=(
                    "정량 값의 비교 맥락(기간·대상·성격)을 확정하지 못해 "
                    "비교에서 보류했다. 표현이 없다는 이유로 기본값을 채우지 "
                    "않는다."
                ),
            )
        )
    for fact_id in (*left_invalid, *right_invalid):
        diagnostics.append(
            StageDiagnostic(
                stage=_STAGE,
                unit=fact_id,
                reason_code=COMPARISON_VALUE_INVALID,
                message=(
                    "원문에서 정량 값을 인식하지 못해 비교에서 제외했다. "
                    "값을 추측해 채우지 않는다 (초안 §7.1 FIT-7)."
                ),
            )
        )

    if not left_groups and not right_groups:
        invalid = bool(left_invalid or right_invalid)
        return result(
            FitStatus.INSUFFICIENT,
            COMPARISON_VALUE_INVALID if invalid else COMPARISON_EVIDENCE_MISSING,
        )

    mismatch = False
    single_sided = False
    agreed = False
    for component in set(left_groups) | set(right_groups):
        left_axes = left_groups.get(component, {})
        right_axes = right_groups.get(component, {})
        for axis in set(left_axes) | set(right_axes):
            if axis not in left_axes or axis not in right_axes:
                single_sided = True
            elif left_axes[axis] == right_axes[axis]:
                agreed = True
            else:
                mismatch = True

    if mismatch:
        # 같은 맥락에서 확인된 불일치는 보존한다. 보류된 값이 있어도 이 판정은
        # 이미 성립한 것이다.
        return result(FitStatus.NEEDS_REVIEW, NUMERIC_MISMATCH)
    if left_withheld or right_withheld:
        # 맥락을 확정하지 못해 뺀 값이 있으면, 남은 축이 모두 같아도 전체를
        # 일치로 올리지 않는다. 뺀 값이 충돌이었을 수 있다.
        return result(FitStatus.INSUFFICIENT, COMPARISON_EVIDENCE_MISSING)
    if left_invalid or right_invalid:
        # 인식하지 못해 버린 값이 있으면 남은 축의 일치를 전체 일치로 올리지
        # 않고, "한쪽에만 있다" 고 말하지도 않는다. 버린 값이 충돌이었을 수
        # 있고, 그것을 확인할 방법이 없다.
        return result(FitStatus.INSUFFICIENT, COMPARISON_VALUE_INVALID)
    if single_sided:
        # 충돌이 확인되지 않았다는 사실은 대응했다는 뜻이 아니다.
        return result(FitStatus.INSUFFICIENT, SINGLE_SIDED_NO_CONFLICT)
    if agreed:
        return result(FitStatus.FIT, None)
    return result(FitStatus.INSUFFICIENT, COMPARISON_EVIDENCE_MISSING)


# ------------------------------------------------------- 목적 의미 축 보완


# axis_code 를 enum 이 아니라 str 로 받는 이유: 어휘 밖의 값이 오면 스키마
# 단계에서 조용히 터지는 대신 서버가 그 항목만 떨어뜨리고 진단을 남긴다.
_FIT_COMPARISON_INSTRUCTION = (
    "You compare two grounded evidence sides of a Korean public-program request document. "
    "For each relation in the payload return {relation_id, status, reason_code, "
    "left_fact_ids, right_fact_ids}. "
    f"status must be one of {[status.value for status in FitStatus]}. "
    "Cite only fact_ids that appear on that relation's own side in the payload. "
    "Never invent a fact_id, a value, or a relation that was not asked for. "
    "Do not return any score, percentage, ratio, or grade."
)


class _FitVerdictModel(BaseModel):
    relation_id: str
    status: str
    reason_code: str | None = None
    left_fact_ids: list[str] = Field(default_factory=list)
    right_fact_ids: list[str] = Field(default_factory=list)


class _FitComparisonResponse(BaseModel):
    # 엔벨로프 키 부재는 최상위 오류다 (_PurposeAxisResponse 와 같은 이유).
    relations: list[_FitVerdictModel]


_TRANSPORT_REASONS = {
    LLMTimeoutError: LLM_TIMEOUT,
    LLMUnavailableError: LLM_UNAVAILABLE,
    LLMInvalidResponseError: LLM_INVALID_RESPONSE,
}


def _generate(
    llm_client: LLMClient,
    *,
    task_name: str,
    instructions: str,
    payload: dict[str, Any],
    response_schema: type[BaseModel],
    model_profile: str,
) -> BaseModel:
    """공용 래퍼에 위임한다. 오류 격리 계약은 worker/llm_call.py 한 곳에 있다."""

    return shared_generate(
        llm_client,
        task_name=task_name,
        instructions=instructions,
        payload=payload,
        response_schema=response_schema,
        model_profile=model_profile,
    )


def _purpose_side(cpl: CplResult, axis: CplAxisCode) -> FitSide:
    """그 축이 붙은 목적 근거만 좌측으로 만든다.

    축은 CPL 이 확정한다. 여기서 다시 분류하지 않는다 — 값을 확정하는 곳과
    축을 확정하는 곳이 다르면 화면과 판정이 갈라진다. 축이 비어 있으면 좌측이
    비고 게이트가 ``INSUFFICIENT`` 로 내린다. 비교를 성립시키려고 목적 문장
    전체를 한 축으로 취급하지 않는다 (초안 §7.1 이 금지한다).

    ``value_raw`` 는 CPL 이 검증한 인용문이다. 목적 문장 전체가 아니라 그 축에
    해당하는 부분만 좌측에 놓는다 (초안 §7.1 FIT-1).
    """

    # CPL 재검으로 복구한 값은 fact_id 가 없고 evidence_ref 로 접지된다.
    # 식별자가 없다는 이유로 검증된 근거를 버리지 않는다.
    refs = [
        FitEvidenceRef(
            fact_id=identifier,
            field_name="purpose_goal",
            value_raw=fact.axis_quoted_text,
            evidence=list(fact.evidence),
            primary_component_id=fact.primary_component_id,
        )
        for fact, identifier in (
            (row, _fit_fact_id(row)) for row in _facts_at(cpl, _PURPOSE_PATH)
        )
        if fact.axis_code == axis.value and identifier
    ]
    return FitSide(field_names=[f"purpose_goal[{axis.value}]"], facts=refs)


# ------------------------------------------------------------ 의미 비교 호출


def _comparison_payload(
    pending: dict[FitRelationId, tuple[FitSide, FitSide]],
    errors: dict[FitRelationId, str] | None = None,
) -> dict[str, Any]:
    def rows(side: FitSide) -> list[dict[str, Any]]:
        return [
            {
                "fact_id": ref.fact_id,
                "field_name": ref.field_name,
                "value_raw": ref.value_raw,
            }
            for ref in side.facts
        ]

    relations = []
    for relation_id, (left, right) in pending.items():
        entry: dict[str, Any] = {
            "relation_id": relation_id.value,
            "question": _RELATION_QUESTION[relation_id],
            "left": rows(left),
            "right": rows(right),
        }
        if errors and relation_id in errors:
            entry["previous_response_error"] = errors[relation_id]
        relations.append(entry)
    return {"relations": relations}


def _validate_verdict(
    row: _FitVerdictModel,
    left: FitSide,
    right: FitSide,
) -> tuple[FitStatus, str | None, list[str], list[str]] | str:
    """응답 한 줄을 검사한다.

    통과하면 (상태, reason, 좌측에서 실제로 인용한 id, 우측에서 인용한 id) 고,
    아니면 오류 설명이다. 인용된 id 는 입력 근거 전체와 구분해 보존한다.
    """

    try:
        status = FitStatus(row.status)
    except ValueError:
        return f"알 수 없는 status {row.status!r}"
    left_ids = {ref.fact_id for ref in left.facts}
    right_ids = {ref.fact_id for ref in right.facts}
    unknown = [
        fact_id
        for fact_id in (*row.left_fact_ids, *row.right_fact_ids)
        if fact_id not in left_ids | right_ids
    ]
    if unknown:
        return f"허용 밖 근거 참조 {unknown}"
    crossed = [fact_id for fact_id in row.left_fact_ids if fact_id not in left_ids]
    crossed += [fact_id for fact_id in row.right_fact_ids if fact_id not in right_ids]
    if crossed:
        return f"좌우가 뒤바뀐 근거 참조 {crossed}"
    if status is not FitStatus.INSUFFICIENT and not (
        row.left_fact_ids and row.right_fact_ids
    ):
        # 인용이 허용 범위 안인지만 보고 인용이 있는지를 보지 않으면, 근거를
        # 하나도 대지 않은 CONFLICT 가 그대로 통과한다.
        return "판정을 내렸는데 한쪽 근거를 인용하지 않았다"
    reason = row.reason_code if row.reason_code in FIT_REASON_CODES else None
    return status, reason, list(row.left_fact_ids), list(row.right_fact_ids)


def _compare_relations(
    llm_client: LLMClient,
    pending: dict[FitRelationId, tuple[FitSide, FitSide]],
    *,
    model_profile: str,
    max_repairs: int,
    diagnostics: list[StageDiagnostic],
) -> dict[FitRelationId, tuple[FitStatus, str | None, list[str], list[str]]]:
    """게이트를 통과한 관계를 한 번에 비교하고, 결함만 격리한다 (초안 §9.2.1).

    - 최상위 응답 자체를 해석할 수 없으면 그 호출 범위(= 남은 관계)만 내린다.
      본문이 JSON 이 아니거나 엔벨로프 키가 없는 경우가 여기다.
    - 관계를 식별할 수 있는 누락·중복·스키마 위반·허용 밖 근거는 그 관계만
      내린다. 정상 관계와 그 근거는 그대로 남는다.
    - 통신 오류는 이미 확정된 관계 결과를 지우지 않는다.

    ponytail: 전송 재시도는 여기서 하지 않는다. 초안 §9.0 이 통신 재시도를
    의미 보완과 별도 카운터로 두라고 했고, 재시도의 자리는 포트 어댑터다.
    """

    verdicts: dict[FitRelationId, tuple[FitStatus, str | None, list[str], list[str]]] = {}
    errors: dict[FitRelationId, str] = {}
    remaining = dict(pending)
    attempt = 0
    while remaining and attempt <= max_repairs:
        attempt += 1
        broken: list[str] = []
        dropped = 0
        try:
            response = _generate(
                llm_client,
                task_name="fit_relation_comparison",
                instructions=_FIT_COMPARISON_INSTRUCTION,
                payload=_comparison_payload(remaining, errors),
                response_schema=_FitComparisonResponse,
                model_profile=model_profile,
            )
        except (LLMTimeoutError, LLMUnavailableError, LLMInvalidResponseError) as error:
            # 포트는 배치를 한 번에 검증한다. 행 하나의 계약 위반으로 정상
            # 관계까지 내려가지 않도록, 식별 가능한 행은 여기서 되살린다.
            recovered = salvage_rows(
                error,
                envelope="relations",
                row_model=_FitVerdictModel,
                id_field="relation_id",
            )
            if recovered is None:
                reason = _TRANSPORT_REASONS[type(error)]
                for relation_id in remaining:
                    diagnostics.append(
                        StageDiagnostic(
                            stage=_STAGE,
                            unit=relation_id.value,
                            reason_code=reason,
                            message=str(error)[:2000],
                            attempt=attempt,
                            terminated_because=reason,
                        )
                    )
                    verdicts[relation_id] = (FitStatus.INSUFFICIENT, reason, [], [])
                return verdicts
            response_rows, broken, dropped = recovered
        else:
            response_rows = list(response.relations)

        if dropped:
            diagnostics.append(
                StageDiagnostic(
                    stage=_STAGE,
                    unit=None,
                    reason_code=LLM_INVALID_RESPONSE,
                    message=f"관계 id 를 알 수 없는 응답 행 {dropped}건을 버렸다.",
                    attempt=attempt,
                )
            )

        seen: dict[FitRelationId, int] = {}
        rows: dict[FitRelationId, _FitVerdictModel] = {}
        invalid_rows: set[FitRelationId] = set()
        # 행이 None 이면 id 는 읽혔지만 그 행이 계약을 어겼다는 뜻이다.
        identified: list[tuple[str, _FitVerdictModel | None]] = [
            (row.relation_id, row) for row in response_rows
        ]
        identified += [(relation_id_raw, None) for relation_id_raw in broken]
        for relation_id_raw, row in identified:
            try:
                relation_id = FitRelationId(relation_id_raw)
            except ValueError:
                diagnostics.append(
                    StageDiagnostic(
                        stage=_STAGE,
                        unit=relation_id_raw,
                        reason_code=LLM_INVALID_RESPONSE,
                        message="요청하지 않은 관계 id 라 무시했다. 정상 관계는 그대로 둔다.",
                        attempt=attempt,
                    )
                )
                continue
            seen[relation_id] = seen.get(relation_id, 0) + 1
            if row is None:
                invalid_rows.add(relation_id)
            else:
                rows[relation_id] = row

        errors = {}
        for relation_id, (left, right) in remaining.items():
            row = rows.get(relation_id)
            if relation_id in invalid_rows:
                errors[relation_id] = "응답 행이 스키마를 어겼다"
                continue
            if row is None:
                errors[relation_id] = "응답에 해당 관계가 없다"
                continue
            if seen[relation_id] > 1:
                errors[relation_id] = "응답에 같은 관계가 중복으로 있다"
                continue
            checked = _validate_verdict(row, left, right)
            if isinstance(checked, str):
                errors[relation_id] = checked
                continue
            verdicts[relation_id] = checked

        for relation_id, message in errors.items():
            diagnostics.append(
                StageDiagnostic(
                    stage=_STAGE,
                    unit=relation_id.value,
                    reason_code=LLM_INVALID_RESPONSE,
                    message=message,
                    attempt=attempt,
                )
            )
        remaining = {
            relation_id: remaining[relation_id] for relation_id in errors
        }

    # 예산을 다 쓰고도 남은 관계는 그 관계만 정보 부족으로 남는다.
    for relation_id in remaining:
        verdicts[relation_id] = (FitStatus.INSUFFICIENT, LLM_INVALID_RESPONSE, [], [])
    return verdicts


# ------------------------------------------------------------------ 진입점


def analyze_fit(
    cpl: CplResult,
    llm_client: LLMClient,
    *,
    model_profile: str,
    max_repairs: int = 1,
) -> FitResult:
    """CPL 결과 하나를 FIT 7관계 결과로 옮긴다. 예외를 던지지 않는다."""

    registry = _fact_id_registry(cpl)
    diagnostics: list[StageDiagnostic] = []
    results: dict[FitRelationId, FitRelationResult] = {}

    # FIT-4: 정책 게이트. payload 를 만들지 않고 포트도 부르지 않는다.
    # 계층 노드가 있다는 이유만으로 비교를 활성화하지 않는다 (초안 §7.1).
    results[FitRelationId.FIT_4] = FitRelationResult(
        relation_id=FitRelationId.FIT_4,
        status=FitStatus.INSUFFICIENT,
        reason_code=HIERARCHY_COMPARISON_NOT_AVAILABLE,
        diagnostics=[
            StageDiagnostic(
                stage=_STAGE,
                unit=FitRelationId.FIT_4.value,
                reason_code=HIERARCHY_COMPARISON_NOT_AVAILABLE,
                message=(
                    "상위·하위 사업 계층 비교 기준이 확정되지 않았다. "
                    "계층 노드 존재는 비교 활성화 근거가 아니다 (초안 §7.1)."
                ),
            )
        ],
    )

    # FIT-7: Rule. LLM 을 타지 않으므로 응답 실패의 영향도 받지 않는다.
    results[FitRelationId.FIT_7] = _fit7(cpl)

    # 나머지 다섯 관계의 우측(그리고 FIT-5·6 의 좌측)은 CPL에서 바로 나온다.
    delivery_left, delivery_right = _delivery_sides(cpl)
    sides: dict[FitRelationId, tuple[FitSide, FitSide]] = {
        FitRelationId.FIT_1: (FitSide(), _side(cpl, _TARGET_PATHS)),
        FitRelationId.FIT_2: (FitSide(), _side(cpl, _MEANS_PATHS)),
        FitRelationId.FIT_3: (FitSide(), _side(cpl, _EFFECT_PATHS)),
        FitRelationId.FIT_5: (
            _side(cpl, _TARGET_PATHS),
            _side(cpl, _CONDITION_PATHS),
        ),
        FitRelationId.FIT_6: (delivery_left, delivery_right),
    }

    # 목적 의미 축 보완은 문서당 한 번이다. 우측 근거가 하나도 없으면 세
    # 관계 모두 어차피 게이트에서 걸리므로 호출하지 않는다.
    # 축은 CPL 이 확정해 왔다. FIT 은 고르기만 한다.
    for relation_id, axis in _PURPOSE_AXIS_OF.items():
        sides[relation_id] = (_purpose_side(cpl, axis), sides[relation_id][1])

    pending: dict[FitRelationId, tuple[FitSide, FitSide]] = {}
    for relation_id, (left, right) in sides.items():
        reason = (
            _fit5_reason(cpl, left) if relation_id is FitRelationId.FIT_5 else None
        )
        if reason is None:
            reason = _gate(left, right, registry)
        if (
            reason == COMPARISON_EVIDENCE_MISSING
            and relation_id in _PURPOSE_AXIS_OF
            and right.facts
            and not left.facts
        ):
            # 우측은 있는데 좌측 축이 안 나온 경우다. 근거 부재가 아니라
            # 의미 분류 미해결로 남긴다 (초안 §9.2).
            reason = PURPOSE_AXIS_UNRESOLVED
        if reason is not None:
            results[relation_id] = FitRelationResult(
                relation_id=relation_id,
                status=FitStatus.INSUFFICIENT,
                reason_code=reason,
                left=left,
                right=right,
                diagnostics=[
                    StageDiagnostic(
                        stage=_STAGE,
                        unit=relation_id.value,
                        reason_code=reason,
                        message="비교 입력이 성립하지 않아 LLM 을 호출하지 않았다.",
                    )
                ],
            )
            continue
        pending[relation_id] = (left, right)

    verdicts = (
        _compare_relations(
            llm_client,
            pending,
            model_profile=model_profile,
            max_repairs=max_repairs,
            diagnostics=diagnostics,
        )
        if pending
        else {}
    )
    for relation_id, (left, right) in pending.items():
        status, reason, used_left, used_right = verdicts.get(
            relation_id, (FitStatus.INSUFFICIENT, LLM_INVALID_RESPONSE, [], [])
        )
        results[relation_id] = FitRelationResult(
            relation_id=relation_id,
            status=status,
            reason_code=reason,
            left=left,
            right=right,
            used_left_fact_ids=used_left,
            used_right_fact_ids=used_right,
        )

    return FitResult(
        relations=[results[relation_id] for relation_id in FitRelationId],
        purpose_axis=cpl.purpose_axis,
        profile_id=cpl.profile_id,
        common_ir_document_id=cpl.common_ir_document_id,
        model_profile=model_profile,
        ruleset_version=FIT_RULESET_VERSION,
        prompt_version=FIT_PROMPT_VERSION,
        diagnostics=diagnostics,
    )
