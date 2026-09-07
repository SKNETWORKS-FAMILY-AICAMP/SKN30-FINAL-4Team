"""Slice 3: Request Profile v0.1.2 → FIT 7관계 판정 (초안 §7.1, §9.0–§9.2.1).

좌우 근거는 전부 프로파일에서 만든다. 다른 관계의 판정 결과를 근거로 쓰지
않고, 없는 값을 만들어 비교를 성립시키지도 않는다. 그래서
``app.services.fit`` 아래의 옛 FIT 구현을 import 하지 않는다 — 저쪽은
CplResult 표시 구조를 다시 해석해서 관계를 만들었고, 그 재해석이 여기서
금지된 "다른 단계 출력에서 근거 만들기" 다.
(테스트가 worker 소스에 그 모듈 경로 문자열이 없는지도 함께 고정한다.)

수단 배치는 초안 §7.1 표 그대로다.

- FIT-1·2·3·5·6: 의미 비교라 LLM. 표현이 열려 있어 Rule 로 닫히지 않는다.
- FIT-4: 비교 기준이 확정되지 않았다. 호출 자체를 하지 않는다.
- FIT-7: 정량 값 집합 비교라 Rule. LLM 으로 값을 추측하지 않는다.

점수·확인율·비율·등급은 계산하지 않는다.
"""

from __future__ import annotations

from decimal import Decimal

import re
from typing import Any

from pydantic import BaseModel, Field

from worker.llm_call import generate as shared_generate, salvage_rows
from app.ports.llm_client import (
    LLMClient,
    LLMInvalidResponseError,
    LLMTimeoutError,
    LLMUnavailableError,
)

from .analysis_inputs import facts_at, field_name_of, field_states_by_name, read_path
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
    CplEvidence,
    FitEvidenceRef,
    FitRelationId,
    FitRelationResult,
    FitResult,
    FitSide,
    FitStatus,
    PurposeAxisAssignment,
    PurposeAxisClassification,
    PurposeAxisCode,
    StageDiagnostic,
)

__all__ = [
    "FIT_PROMPT_VERSION",
    "FIT_RULESET_VERSION",
    "PURPOSE_AXIS_PROMPT_VERSION",
    "analyze_fit",
]

_STAGE = "analyze_fit"
_PURPOSE_STAGE = "classify_purpose_axis"

FIT_RULESET_VERSION = "fit-rules-v0.1"
FIT_PROMPT_VERSION = "fit-relations-v0.1"
PURPOSE_AXIS_PROMPT_VERSION = "fit-purpose-axis-v0.1"


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
    FitRelationId.FIT_1: PurposeAxisCode.TARGET_CONDITION,
    FitRelationId.FIT_2: PurposeAxisCode.DIRECTION,
    FitRelationId.FIT_3: PurposeAxisCode.DIRECTION,
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


def _fact_id_registry(profile: dict[str, Any]) -> set[str]:
    """프로파일 안에 실제로 존재하는 모든 항목 id.

    게이트 3번(근거 참조가 실제 fact 로 해소되는가)과 LLM 응답 접지 검사가
    같은 집합을 본다. 한쪽만 통과하는 id 가 생기지 않게 한 곳에서 만든다.
    """

    found: set[str] = set()
    id_keys = (
        "fact_id",
        "delivery_relation_id",
        "program_node_id",
        "support_component_id",
    )

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key in id_keys:
                value = node.get(key)
                if isinstance(value, str):
                    found.add(value)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(profile)
    return found


def _base_id(fact_id: str) -> str:
    """``delivery:x.actor`` → ``delivery:x``. 프로파일 id 는 그대로 돌아온다."""

    return fact_id.split(".", 1)[0]


def _refs_at(profile: dict[str, Any], path: str) -> list[FitEvidenceRef]:
    """경로 하나를 근거 목록으로 편다. id 가 없는 항목은 인용할 수 없으므로 뺀다."""

    name = field_name_of(path)
    return [
        FitEvidenceRef(
            fact_id=fact.fact_id,
            field_name=name,
            value_raw=fact.value_raw,
            evidence=list(fact.evidence),
            primary_component_id=fact.primary_component_id,
        )
        for fact in facts_at(profile, path)
        if fact.fact_id
    ]


def _side(profile: dict[str, Any], paths: tuple[str, ...]) -> FitSide:
    refs: list[FitEvidenceRef] = []
    for path in paths:
        refs.extend(_refs_at(profile, path))
    return FitSide(field_names=[field_name_of(path) for path in paths], facts=refs)


def _evidence_rows(entry: dict[str, Any]) -> list[CplEvidence]:
    """항목 하나의 Common IR 접지. 없으면 빈 목록이고 지어내지 않는다."""

    rows = entry.get("evidence")
    if not isinstance(rows, list):
        rows = []
    return [
        CplEvidence(
            source_block_id=row.get("source_block_id"),
            common_ir_document_id=row.get("common_ir_document_id"),
            common_ir_block_id=row.get("common_ir_block_id"),
            common_ir_occurrence_ids=list(row.get("common_ir_occurrence_ids") or []),
        )
        for row in rows
        if isinstance(row, dict)
    ]


def _delivery_sides(profile: dict[str, Any]) -> tuple[FitSide, FitSide]:
    """FIT-6 의 좌우. ``delivery_relations`` 컨테이너 안에서 갈린다.

    actor 와 role 은 같은 relation 안에 있어 ``fact_id`` 가 없다. 컨테이너 id
    에 ``.actor`` / ``.role`` 접미사를 붙여 좌우를 구분하되, 접미사를 떼면
    프로파일의 실제 relation id 로 돌아간다. 여기서 새 id 를 발명하는 것이
    아니라 한 컨테이너의 두 자리를 이름 붙이는 것이다.

    ``actions`` 가 비어 있고 ``delivery_methods`` 가 없다는 사실은 모순이
    아니다. Rule 로 CONFLICT 를 만들지 않는다 (초안 §7.1 FIT-6).
    """

    relations = read_path(profile, _DELIVERY_PATH)
    rows = relations if isinstance(relations, list) else []
    left: list[FitEvidenceRef] = []
    right: list[FitEvidenceRef] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        relation_id = row.get("delivery_relation_id")
        if not relation_id:
            continue
        container = row.get("relation_container")
        container_evidence = (
            _evidence_rows({"evidence": [container]}) if isinstance(container, dict) else []
        )
        actor = row.get("actor")
        if isinstance(actor, dict):
            left.append(
                FitEvidenceRef(
                    fact_id=f"{relation_id}.actor",
                    field_name="delivery_relations.actor",
                    value_raw=actor.get("value_raw"),
                    evidence=_evidence_rows(actor) or container_evidence,
                )
            )
        role = row.get("role")
        if isinstance(role, dict):
            right.append(
                FitEvidenceRef(
                    fact_id=f"{relation_id}.role",
                    field_name="delivery_relations.role",
                    value_raw=role.get("value_raw"),
                    evidence=_evidence_rows(role) or container_evidence,
                )
            )
        actions = row.get("actions")
        for index, action in enumerate(actions if isinstance(actions, list) else []):
            if not isinstance(action, dict):
                continue
            right.append(
                FitEvidenceRef(
                    fact_id=f"{relation_id}.actions[{index}]",
                    field_name="delivery_relations.actions",
                    value_raw=action.get("value_raw"),
                    evidence=_evidence_rows(action) or container_evidence,
                )
            )
    right.extend(_refs_at(profile, _DELIVERY_METHODS_PATH))
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


def _fit5_reason(states: dict[str, dict[str, Any]], left: FitSide) -> str | None:
    """FIT-5 만의 부재·실패 구분 (초안 §7.1 "단순 조건 미기재와 추출 실패").

    문서에 있는 조건을 "조건 없음" 으로 단정하지 않는다. 그래서 상태가
    ``extraction_failed`` / ``mentioned_unresolved`` 이면 부재가 아니라 근거
    확보 실패로 남긴다.
    """

    if not left.facts:
        return COMPARISON_EVIDENCE_MISSING
    statuses = [
        (states.get(field_name_of(path)) or {}).get("status")
        for path in _CONDITION_PATHS
    ]
    if any(status in ("extraction_failed", "mentioned_unresolved") for status in statuses):
        return COMPARISON_EVIDENCE_MISSING
    if all(status == "not_found" for status in statuses):
        return NO_CONDITIONS_SPECIFIED
    return None


# --------------------------------------------------------- FIT-7 정량 비교

# 금액 단위. ponytail: 이 정규식들은 단위 하나짜리 값만 읽는다. "1억 5000만원"
# 처럼 단위가 두 번 붙은 복합 표현은 읽지 못한다 — 그리고 **읽지 못한 것을
# 부분값으로 만들지 않는다** (아래 숫자 시퀀스 소비 규칙). 복합 단위가 필요해
# 지면 정규식에 예외를 더하는 대신 금액 파서를 따로 둔다.
_AMOUNT_SCALES = {"조": 10**12, "억": 10**8, "만": 10**4, "천": 10**3}
_AMOUNT_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*([조억만천])?\s*원")
_COUNT_RE = re.compile(r"(\d[\d,]*)\s*개?\s*(팀|명|개사|건|개)")
# 기간 수식어가 붙은 횟수(월 2회)는 총 횟수(총 8회)와 같은 축이 아니다.
_TIMES_RE = re.compile(r"([월주년일])?\s*(\d[\d,]*)\s*회")
_DIGITS_RE = re.compile(r"\d+")


# 천 단위 쉼표는 세 자리씩만 인정한다. 정규식의 [\d,]* 가 자리수를 보지 않아
# "1,2,3만원" 이 1,230,000 원으로, "1,,000원" 이 1,000 원으로 조용히 바뀐다.
# 잘못된 표기에서 유효한 숫자를 만들지 않는다 (숫자 소비 규칙과 같은 취지).
_GROUPED_NUMBER_RE = re.compile(r"^\d{1,3}(?:,\d{3})*(?:\.\d+)?$|^\d+(?:\.\d+)?$")


def _plain_number(literal: str) -> str | None:
    """쉼표 문법이 올바르면 쉼표를 뗀 문자열, 아니면 None."""

    if not _GROUPED_NUMBER_RE.match(literal):
        return None
    return literal.replace(",", "")


def _quantities(value_raw: str | None) -> set[tuple[str, int]]:
    """원문에서 (축, 정규화 값) 집합을 뽑는다. Rule 만 쓴다.

    축을 함께 달아 두는 이유는 금액과 팀수가 절대 같은 자리에서 비교되지
    않게 하기 위해서다. 인식하지 못하면 빈 집합이고, 호출자가 그 사실을
    진단으로 남긴다.

    **숫자 시퀀스 소비 규칙**: 원문의 숫자 하나라도 어떤 매치에도 걸리지
    않았으면 이 값 전체를 버린다. 문자열 전체를 소비하라는 뜻이 아니라
    (설명 문구는 무방하다) 숫자 문법을 일부만 읽고 다른 값을 만들지 말라는
    뜻이다. "10~20개사" 에서 20 만, "1억 5000만원" 에서 5000만원만 읽으면
    서로 다른 값이 일치로 판정된다.
    """

    if not value_raw:
        return set()
    found: set[tuple[str, int]] = set()
    spans: list[tuple[int, int]] = []
    for match in _AMOUNT_RE.finditer(value_raw):
        # float 로 곱하면 0.29억원이 28,999,999 가 되어 2,900만원과 불일치로
        # 판정된다. 같은 금액을 다르게 만드는 것은 B 와 같은 종류의 오판이라
        # 10 진 고정소수로 계산한다.
        literal = _plain_number(match.group(1))
        if literal is None:
            return set()
        number = Decimal(literal)
        won = number * _AMOUNT_SCALES.get(match.group(2), 1)
        if won != won.to_integral_value():
            # 원 단위 정수가 아니면 반올림 방향을 추측하지 않고 값을 버린다.
            return set()
        found.add(("AMOUNT_KRW", int(won)))
        spans.append(match.span())
    for match in _COUNT_RE.finditer(value_raw):
        literal = _plain_number(match.group(1))
        if literal is None:
            return set()
        found.add((f"COUNT:{match.group(2)}", int(literal)))
        spans.append(match.span())
    for match in _TIMES_RE.finditer(value_raw):
        period = match.group(1) or "TOTAL"
        literal = _plain_number(match.group(2))
        if literal is None:
            return set()
        found.add((f"TIMES:{period}", int(literal)))
        spans.append(match.span())

    for digits in _DIGITS_RE.finditer(value_raw):
        if not any(
            start <= digits.start() and digits.end() <= end for start, end in spans
        ):
            return set()
    return found


def _axis_values(
    refs: list[FitEvidenceRef],
) -> tuple[dict[str | None, dict[str, set[int]]], list[str]]:
    """component → 축 → 값 집합. 정규화하지 못한 fact_id 를 함께 돌려준다.

    ``primary_component_id`` 가 None 인 fact 는 None 그룹에 남는다. 초안 §7.1
    이 "총사업비와 직접 지원금을 자동 비교하지 않는다" 라고 못박은 것과 같은
    이유로, 소속이 없는 값을 특정 component 값과 붙이지 않는다.
    """

    grouped: dict[str | None, dict[str, set[int]]] = {}
    invalid: list[str] = []
    for ref in refs:
        quantities = _quantities(ref.value_raw)
        if not quantities:
            invalid.append(ref.fact_id)
            continue
        axes = grouped.setdefault(ref.primary_component_id, {})
        for axis, value in quantities:
            axes.setdefault(axis, set()).add(value)
    return grouped, invalid


def _fit7(profile: dict[str, Any]) -> FitRelationResult:
    """FIT-7: 같은 component 안에서 같은 축의 값 집합을 비교한다.

    ``total_budget`` · ``cost_sharing`` 은 입력이 아니다 (초안 §7.1).
    대표값을 고르지 않고 집합을 통째로 비교한다. 대표값을 고르는 순간
    "400만원 vs 150만원" 같은 분할 지급 단계액이 거짓 충돌이 된다.
    """

    left = _side(profile, (_CONTENT_PATH,))
    right = _side(profile, (_SCALE_PATH,))
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

    left_groups, left_invalid = _axis_values(left.facts)
    right_groups, right_invalid = _axis_values(right.facts)
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
        return result(FitStatus.NEEDS_REVIEW, NUMERIC_MISMATCH)
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


class _PurposeAxisAssignmentModel(BaseModel):
    fact_id: str
    axis_code: str
    quoted_text: str


class _PurposeAxisResponse(BaseModel):
    # 엔벨로프 키는 필수다. 기본값을 주면 ``{}`` 가 "빈 배치" 로 조용히
    # 통과해 최상위 오류가 정상 응답으로 둔갑한다.
    assignments: list[_PurposeAxisAssignmentModel]


# axis_code 를 enum 이 아니라 str 로 받는 이유: 어휘 밖의 값이 오면 스키마
# 단계에서 조용히 터지는 대신 서버가 그 항목만 떨어뜨리고 진단을 남긴다.
_PURPOSE_AXIS_INSTRUCTION = (
    "You classify existing purpose statements into meaning axes. "
    "Return only {fact_id, axis_code, quoted_text} for facts given in the payload. "
    f"axis_code must be one of {sorted(PURPOSE_AXIS_CODES)}. "
    "quoted_text must be copied verbatim from that fact's value_raw. "
    "Never invent a fact_id, a new value, an offset, or evidence. "
    "Omit any fact you cannot classify; an empty list is a valid answer."
)

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


def _classify_purpose_axes(
    profile: dict[str, Any],
    llm_client: LLMClient,
    *,
    model_profile: str,
    diagnostics: list[StageDiagnostic],
) -> PurposeAxisClassification:
    """목적 fact 를 의미 축으로 분류한다. 문서당 한 번 (초안 §9.2).

    프로파일에는 목적의 대상조건·방향 축이 없다. 없는 축을 코드가 지어낼 수는
    없고, 목적 전체를 한 축으로 취급하는 것도 초안 §7.1 이 금지한다. 그래서
    이 한 건만 "의미 분류 미완료" 보완에 해당한다.

    모델은 축 이름과 인용문만 돌려준다. 값·오프셋·근거는 프로파일 것을 그대로
    쓴다. 서버가 fact_id 존재·인용문 부분문자열·어휘 소속을 검사하고, 통과하지
    못한 항목은 진단만 남기고 버린다. 예산은 1이며 같은 입력으로 재시도하지
    않는다.
    """

    facts = [fact for fact in facts_at(profile, _PURPOSE_PATH) if fact.fact_id]
    if not facts:
        return PurposeAxisClassification(
            attempted=False, reason_code=COMPARISON_EVIDENCE_MISSING
        )

    payload = {
        "axis_vocabulary": sorted(PURPOSE_AXIS_CODES),
        "facts": [
            {"fact_id": fact.fact_id, "value_raw": fact.value_raw} for fact in facts
        ],
    }
    try:
        response = _generate(
            llm_client,
            task_name="fit_purpose_axis_classification",
            instructions=_PURPOSE_AXIS_INSTRUCTION,
            payload=payload,
            response_schema=_PurposeAxisResponse,
            model_profile=model_profile,
        )
    except (LLMTimeoutError, LLMUnavailableError, LLMInvalidResponseError) as error:
        # 배정 하나가 계약을 어겼다고 나머지 배정을 버리지 않는다.
        recovered = salvage_rows(
            error,
            envelope="assignments",
            row_model=_PurposeAxisAssignmentModel,
            id_field="fact_id",
        )
        if recovered is None:
            reason = _TRANSPORT_REASONS[type(error)]
            diagnostics.append(
                StageDiagnostic(
                    stage=_PURPOSE_STAGE,
                    unit=_PURPOSE_PATH,
                    reason_code=reason,
                    message=str(error)[:2000],
                    attempt=1,
                    terminated_because=reason,
                )
            )
            return PurposeAxisClassification(
                attempted=True,
                reason_code=reason,
                prompt_version=PURPOSE_AXIS_PROMPT_VERSION,
            )
        response_rows, broken, dropped_rows = recovered
        for fact_id in broken:
            diagnostics.append(
                StageDiagnostic(
                    stage=_PURPOSE_STAGE,
                    unit=fact_id,
                    reason_code=LLM_INVALID_RESPONSE,
                    message="응답 행이 스키마를 어겨 축 분류에서 제외했다.",
                    attempt=1,
                )
            )
        if dropped_rows:
            diagnostics.append(
                StageDiagnostic(
                    stage=_PURPOSE_STAGE,
                    unit=None,
                    reason_code=LLM_INVALID_RESPONSE,
                    message=f"fact_id 를 알 수 없는 응답 행 {dropped_rows}건을 버렸다.",
                    attempt=1,
                )
            )
    else:
        response_rows = list(response.assignments)
        broken = []

    by_id = {fact.fact_id: fact for fact in facts}
    assignments: list[PurposeAxisAssignment] = []
    dropped: list[str] = list(broken)
    for row in response_rows:
        fact = by_id.get(row.fact_id)
        if fact is None:
            problem = "프로파일에 없는 fact_id"
        elif row.axis_code not in PURPOSE_AXIS_CODES:
            problem = "어휘 밖 axis_code"
        elif not row.quoted_text or row.quoted_text not in (fact.value_raw or ""):
            problem = "원문 부분문자열이 아닌 인용문"
        else:
            assignments.append(
                PurposeAxisAssignment(
                    fact_id=row.fact_id,
                    axis_code=row.axis_code,
                    quoted_text=row.quoted_text,
                )
            )
            continue
        dropped.append(row.fact_id)
        diagnostics.append(
            StageDiagnostic(
                stage=_PURPOSE_STAGE,
                unit=row.fact_id,
                reason_code=LLM_INVALID_RESPONSE,
                message=f"{problem} 이므로 축 분류에서 제외했다.",
                attempt=1,
            )
        )

    return PurposeAxisClassification(
        attempted=True,
        assignments=assignments,
        reason_code=None if assignments else PURPOSE_AXIS_UNRESOLVED,
        dropped=dropped,
        prompt_version=PURPOSE_AXIS_PROMPT_VERSION,
    )


def _purpose_side(
    profile: dict[str, Any],
    classification: PurposeAxisClassification,
    axis: PurposeAxisCode,
) -> FitSide:
    """분류된 축에 해당하는 목적 근거만 좌측으로 만든다.

    ``value_raw`` 는 검증된 인용문이다. 목적 문장 전체가 아니라 그 축에
    해당하는 부분만 좌측에 놓는다 (초안 §7.1 FIT-1).
    """

    by_id = {fact.fact_id: fact for fact in facts_at(profile, _PURPOSE_PATH)}
    refs = [
        FitEvidenceRef(
            fact_id=row.fact_id,
            field_name="purpose_goal",
            value_raw=row.quoted_text,
            evidence=list(by_id[row.fact_id].evidence),
            primary_component_id=by_id[row.fact_id].primary_component_id,
        )
        for row in classification.assignments
        if row.axis_code == axis.value and row.fact_id in by_id
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
    profile: dict[str, Any],
    llm_client: LLMClient,
    *,
    model_profile: str,
    max_repairs: int = 1,
) -> FitResult:
    """프로파일 dict 하나를 FIT 7관계 결과로 옮긴다. 예외를 던지지 않는다."""

    registry = _fact_id_registry(profile)
    states = field_states_by_name(profile)
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
    results[FitRelationId.FIT_7] = _fit7(profile)

    # 나머지 다섯 관계의 우측(그리고 FIT-5·6 의 좌측)은 프로파일에서 바로 나온다.
    delivery_left, delivery_right = _delivery_sides(profile)
    sides: dict[FitRelationId, tuple[FitSide, FitSide]] = {
        FitRelationId.FIT_1: (FitSide(), _side(profile, _TARGET_PATHS)),
        FitRelationId.FIT_2: (FitSide(), _side(profile, _MEANS_PATHS)),
        FitRelationId.FIT_3: (FitSide(), _side(profile, _EFFECT_PATHS)),
        FitRelationId.FIT_5: (
            _side(profile, _TARGET_PATHS),
            _side(profile, _CONDITION_PATHS),
        ),
        FitRelationId.FIT_6: (delivery_left, delivery_right),
    }

    # 목적 의미 축 보완은 문서당 한 번이다. 우측 근거가 하나도 없으면 세
    # 관계 모두 어차피 게이트에서 걸리므로 호출하지 않는다.
    needs_axis = [
        relation_id
        for relation_id in _PURPOSE_AXIS_OF
        if sides[relation_id][1].facts
    ]
    if needs_axis:
        classification = _classify_purpose_axes(
            profile, llm_client, model_profile=model_profile, diagnostics=diagnostics
        )
    else:
        classification = PurposeAxisClassification(
            attempted=False, reason_code=COMPARISON_EVIDENCE_MISSING
        )
    for relation_id, axis in _PURPOSE_AXIS_OF.items():
        sides[relation_id] = (
            _purpose_side(profile, classification, axis),
            sides[relation_id][1],
        )

    pending: dict[FitRelationId, tuple[FitSide, FitSide]] = {}
    for relation_id, (left, right) in sides.items():
        reason = (
            _fit5_reason(states, left) if relation_id is FitRelationId.FIT_5 else None
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

    metadata = profile.get("processing_metadata") or {}
    documents = profile.get("source_documents") or []
    first_ir = documents[0].get("common_ir", {}) if documents else {}
    return FitResult(
        relations=[results[relation_id] for relation_id in FitRelationId],
        purpose_axis=classification,
        profile_id=profile.get("profile_id"),
        common_ir_document_id=(
            metadata.get("common_ir_document_id") or first_ir.get("document_id")
        ),
        model_profile=model_profile,
        ruleset_version=FIT_RULESET_VERSION,
        prompt_version=FIT_PROMPT_VERSION,
        diagnostics=diagnostics,
    )
