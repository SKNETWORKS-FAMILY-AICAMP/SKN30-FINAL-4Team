"""Slice 4a: 요청서·공고 프로파일 → 공통 SIM 비교 프로파일 (초안 §7.2, §9.2).

한 장의 변환표가 전부다. 요청서 v0.1.2 와 공고 v0.2 는 필드 집합이 다르지만
**변환표는 하나** 다. 없는 키는 아무것도 만들지 않고 컨테이너가 빈 목록으로
남는다. 한쪽에만 있는 필드를 다른 쪽 필드로 대신 채우지 않는다.

수단 배치 (CLAUDE.md "Rule·LLM 선택 원칙").

- Rule: 출처 필드가 해석 없이 공통 키 하나로 떨어지는 자리. 지원활동·수단·
  품목, 지원대상·수혜자, 제외대상, 전달체계 컨테이너.
- LLM: ``purpose_goal`` 의 의미 축 분해와, 신청자격·자격조건을 대상 조건
  하위 축으로 나누는 자리. 표현이 열려 있어 정규식으로 닫히지 않는다.
  AGENTS.md: "신청자격은 단일 비교축이 아니라 대상 조건 컨테이너로 보고
  하위 조건을 공통 target 축에 매핑한다."

LLM 은 ``{fact_id, common_key, quoted_text}`` 만 돌려준다. 값·오프셋·근거·
정규화형을 만들지 않는다. 서버가 (1) fact_id 가 그 출처 필드에 있는지,
(2) 인용문이 그 Fact 원문의 부분문자열인지, (3) common_key 가 그 컨테이너의
어휘인지를 검사하고, 통과하지 못한 배정은 진단만 남기고 버린다.

같은 (fact, 인용문) 을 여러 하위 키에 복제하는 것도 막는다. 초안 §7.2:
"공고 컨테이너 원문 전체를 모든 하위 키에 반복 복제하지 않는다."
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from worker.llm_call import generate as shared_generate, salvage_rows
from app.ports.llm_client import (
    LLMClient,
    LLMInvalidResponseError,
    LLMTimeoutError,
    LLMUnavailableError,
)

from .analysis_inputs import facts_at, read_path
from .contracts.sim_result import (
    CLASSIFICATION_PARTIAL,
    CLASSIFICATION_UNRESOLVED,
    COMMON_KEYS,
    DUPLICATE_SPAN_ASSIGNMENT,
    LLM_INVALID_RESPONSE,
    LLM_TIMEOUT,
    LLM_UNAVAILABLE,
    SIM_AXIS_IDS,
    STRUCTURING_COMPLETED,
    STRUCTURING_FAILED,
    STRUCTURING_NOT_ATTEMPTED,
    UNMAPPED_SOURCE_FIELD,
    CplEvidence,
    SimAxis,
    SimAxisStructuring,
    SimCommonEntry,
    SimCommonProfile,
    StageDiagnostic,
)

__all__ = [
    "SIM_CLASSIFICATION_PROMPT_VERSION",
    "SIM_RULESET_VERSION",
    "build_common_profile",
]

_STAGE = "build_sim_common_profile"
_CLASSIFY_STAGE = "classify_sim_common_keys"
_CLASSIFY_TASK = "sim_common_key_classification"

SIM_RULESET_VERSION = "sim-common-rules-v0.1"
SIM_CLASSIFICATION_PROMPT_VERSION = "sim-common-classification-v0.1"


# ------------------------------------------------------------------- 변환표

# Rule 변환. 출처 필드 → (컨테이너, 하위 키). 해석이 필요 없는 자리만 있다.
_RULE_MAP: dict[str, tuple[SimAxis, str]] = {
    "support_activities": (SimAxis.CONTENT, "activity"),
    "support_methods": (SimAxis.CONTENT, "instrument"),
    "support_items": (SimAxis.CONTENT, "item"),
    "support_target": (SimAxis.TARGET, "target_group"),
    "beneficiary": (SimAxis.TARGET, "target_group"),
    "exclusions": (SimAxis.TARGET, "exclusion"),
    "delivery_methods": (SimAxis.DELIVERY, "method"),
}

# LLM 분류. 출처 필드 → 컨테이너. 하위 키는 모델이 고르고 서버가 검사한다.
_LLM_MAP: dict[str, SimAxis] = {
    "purpose_goal": SimAxis.PURPOSE,
    "applicant_eligibility": SimAxis.TARGET,
    "eligibility_conditions": SimAxis.TARGET,
}

# 컨테이너 안에서 actor/role/actions 가 갈리는 유일한 필드.
_DELIVERY_RELATIONS = "delivery_relations"
_DELIVERY_SLOTS = {"actor": "organization", "role": "step_role"}

# 컨테이너 밖에 보존하는 필드 (AGENTS.md 제외 계약).
_PRESERVED: dict[str, str] = {
    # 중복제한은 SIM-2 수혜 가능 집합 비교에 넣지 않고 BEN 근거로 분리한다.
    "duplicate_support_conditions": "benefit_overlap_evidence",
    # 지원규모 정량값은 Evidence 로만 보존한다. SIM-3 은 활동·수단·품목을 본다.
    "support_scale": "scale_reference_evidence",
}

_CONSUMED = frozenset(
    {*_RULE_MAP, *_LLM_MAP, *_PRESERVED, _DELIVERY_RELATIONS}
)


# ------------------------------------------------------------------- 도우미


def _empty_containers() -> dict[SimAxis, dict[str, list[SimCommonEntry]]]:
    """네 컨테이너를 모든 하위 키가 빈 목록인 상태로 연다."""

    return {axis: {key: [] for key in keys} for axis, keys in COMMON_KEYS.items()}


def _evidence_rows(entry: dict[str, Any]) -> list[CplEvidence]:
    """delivery_relations 하위 항목의 접지. 없으면 빈 목록이고 지어내지 않는다."""

    rows = entry.get("evidence")
    return [
        CplEvidence(
            source_block_id=row.get("source_block_id"),
            common_ir_document_id=row.get("common_ir_document_id"),
            common_ir_block_id=row.get("common_ir_block_id"),
            common_ir_occurrence_ids=list(row.get("common_ir_occurrence_ids") or []),
        )
        for row in (rows if isinstance(rows, list) else [])
        if isinstance(row, dict)
    ]


def _rule_entries(
    profile: dict[str, Any], field_name: str, key: str
) -> list[SimCommonEntry]:
    facts = facts_at(profile, f"comparison_profile.{field_name}")
    return [
        SimCommonEntry(
            fact_id=fact.fact_id,
            source_field=field_name,
            common_key=key,
            value_raw=fact.value_raw,
            evidence=list(fact.evidence),
            origin="RULE",
            status=fact.status,
        )
        for fact in facts
        if fact.fact_id
    ]


def _delivery_entries(
    profile: dict[str, Any],
) -> tuple[list[tuple[str, SimCommonEntry]], set[str]]:
    """``delivery_relations`` 한 컨테이너를 organization·step_role·procedure 로 편다.

    actor 와 role 은 같은 relation 안에 있어 각자의 ``fact_id`` 가 없다. fit.py
    가 하는 것과 같은 방식으로 relation id 에 ``.actor`` 같은 접미사를 붙여
    자리를 구분한다. 새 id 를 발명하는 것이 아니라 한 컨테이너의 두 자리에
    이름을 붙이는 것이고, 접미사를 떼면 프로파일의 실제 id 로 돌아간다.

    공고 v0.2 에는 이 컨테이너가 아예 없다. 그러면 빈 목록이고, SIM-4 는
    게이트에서 ``CANDIDATE_EVIDENCE_MISSING`` 으로 걸린다.
    """

    rows = read_path(profile, f"comparison_profile.{_DELIVERY_RELATIONS}")
    entries: list[tuple[str, SimCommonEntry]] = []
    ids: set[str] = set()
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        relation_id = row.get("delivery_relation_id")
        if not relation_id:
            continue
        container = row.get("relation_container")
        fallback = (
            _evidence_rows({"evidence": [container]}) if isinstance(container, dict) else []
        )

        def add(fact_id: str, key: str, node: dict[str, Any]) -> None:
            ids.add(fact_id)
            entries.append(
                (
                    key,
                    SimCommonEntry(
                        fact_id=fact_id,
                        source_field=_DELIVERY_RELATIONS,
                        common_key=key,
                        value_raw=node.get("value_raw"),
                        evidence=_evidence_rows(node) or fallback,
                        origin="RULE",
                        status=node.get("status"),
                    ),
                )
            )

        for slot, key in _DELIVERY_SLOTS.items():
            node = row.get(slot)
            if isinstance(node, dict):
                add(f"{relation_id}.{slot}", key, node)
        actions = row.get("actions")
        for index, action in enumerate(actions if isinstance(actions, list) else []):
            if isinstance(action, dict):
                add(f"{relation_id}.actions[{index}]", "procedure", action)
    return entries, ids


def _registry(profile: dict[str, Any]) -> set[str]:
    """``comparison_profile`` 안에 실제로 존재하는 항목 id.

    게이트 3번(근거 참조가 실제 Fact 로 해소되는가)이 이 집합을 본다.
    ponytail: SIM 이 보는 것은 comparison_profile 한 겹뿐이라 fit.py 처럼
    프로파일 전체를 재귀로 훑지 않는다.
    """

    found: set[str] = set()
    containers = profile.get("comparison_profile")
    for rows in (containers or {}).values():
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            for key in ("fact_id", "delivery_relation_id"):
                value = row.get(key)
                if isinstance(value, str):
                    found.add(value)
    return found


# ------------------------------------------------------------- LLM 의미 분류


class _AssignmentModel(BaseModel):
    fact_id: str
    common_key: str
    quoted_text: str


class _ClassificationResponse(BaseModel):
    # 엔벨로프 키는 필수다. 기본값을 주면 ``{}`` 가 "빈 배치" 로 통과한다.
    assignments: list[_AssignmentModel]


# common_key 를 enum 이 아니라 str 로 받는 이유는 fit.py 의 axis_code 와 같다.
# 어휘 밖 값이 스키마 단계에서 조용히 터지는 대신 서버가 그 배정만 떨어뜨리고
# 진단을 남긴다.
_CLASSIFY_INSTRUCTION = (
    "You map Korean public-program profile facts onto a fixed comparison vocabulary. "
    "Return only {fact_id, common_key, quoted_text} for facts given in the payload. "
    "common_key must come from that fact's own allowed_common_keys list. "
    "quoted_text must be copied verbatim from that fact's value_raw, and must be the "
    "narrowest span that supports the key. "
    "Never reuse one span for more than one common_key, and never repeat a whole "
    "container value across every sub-key. "
    "Never invent a fact_id, a new value, an offset, evidence, or a normalised form. "
    "Omit any fact you cannot ground; an empty list is a valid answer."
)

_TRANSPORT_REASONS = {
    LLMTimeoutError: LLM_TIMEOUT,
    LLMUnavailableError: LLM_UNAVAILABLE,
    LLMInvalidResponseError: LLM_INVALID_RESPONSE,
}


# sim.py 가 여기서 가져다 쓰던 이름을 유지한다. 구현은 공용 래퍼 한 곳이다.
generate = shared_generate


def _classify(
    profile: dict[str, Any],
    llm_client: LLMClient,
    *,
    model_profile: str,
    diagnostics: list[StageDiagnostic],
) -> tuple[list[tuple[SimAxis, SimCommonEntry]], dict[SimAxis, SimAxisStructuring]]:
    """열린 의미 필드를 공통 하위 키로 분류한다. **문서당 한 번** (초안 §9.2).

    네 축이 이 결과를 나눠 쓰지만 호출은 하나다. 예산은 1이고 같은 입력으로
    재시도하지 않는다.

    배정과 함께 **축별 구조화 상태** 를 돌려준다. 컨테이너가 비었다는 사실만
    보면 원문이 없어서 빈 것과 이 호출이 실패해서 빈 것이 같아 보인다.
    게이트가 그 둘을 구분하려면 여기서 남겨야 한다 (초안 §7.2, §9.5).
    """

    facts: dict[str, tuple[str, SimAxis, Any]] = {}
    for field_name, axis in _LLM_MAP.items():
        for fact in facts_at(profile, f"comparison_profile.{field_name}"):
            if fact.fact_id and fact.value_raw:
                facts[fact.fact_id] = (field_name, axis, fact)
    # 원문이 있는 축과 없는 축을 여기서 가른다. 원문이 없으면 호출이 실패해도
    # 그 축은 "구조화 실패" 가 아니라 애초에 시도할 것이 없던 축이다.
    with_input = {axis for _, axis, _ in facts.values()}
    if not facts:
        return [], _structuring(with_input, set(), None, diagnostics)

    payload = {
        "facts": [
            {
                "fact_id": fact_id,
                "source_field": field_name,
                "container": axis.value,
                "allowed_common_keys": list(COMMON_KEYS[axis]),
                "value_raw": fact.value_raw,
            }
            for fact_id, (field_name, axis, fact) in facts.items()
        ]
    }
    try:
        response = generate(
            llm_client,
            task_name=_CLASSIFY_TASK,
            instructions=_CLASSIFY_INSTRUCTION,
            payload=payload,
            response_schema=_ClassificationResponse,
            model_profile=model_profile,
        )
    except (LLMTimeoutError, LLMUnavailableError, LLMInvalidResponseError) as error:
        # 배정 하나가 계약을 어겼다고 나머지 배정을 버리지 않는다.
        recovered = salvage_rows(
            error,
            envelope="assignments",
            row_model=_AssignmentModel,
            id_field="fact_id",
        )
        if recovered is None:
            reason = _TRANSPORT_REASONS[type(error)]
            return [], _structuring(
                with_input, set(), reason, diagnostics, detail=str(error)[:2000]
            )
        response_rows, broken, dropped = recovered
        for fact_id in broken:
            diagnostics.append(
                StageDiagnostic(
                    stage=_CLASSIFY_STAGE,
                    unit=fact_id,
                    reason_code=LLM_INVALID_RESPONSE,
                    message="응답 행이 스키마를 어겨 공통 프로파일에 넣지 않았다.",
                    attempt=1,
                )
            )
        if dropped:
            diagnostics.append(
                StageDiagnostic(
                    stage=_CLASSIFY_STAGE,
                    unit=None,
                    reason_code=LLM_INVALID_RESPONSE,
                    message=f"fact_id 를 알 수 없는 응답 행 {dropped}건을 버렸다.",
                    attempt=1,
                )
            )
    else:
        response_rows = list(response.assignments)

    accepted: list[tuple[SimAxis, SimCommonEntry]] = []
    # (fact_id, 인용문) → 이미 배정된 하위 키. 같은 span 의 재사용을 막는다.
    spans: dict[tuple[str, str], str] = {}
    for row in response_rows:
        found = facts.get(row.fact_id)
        if found is None:
            problem = ("제공하지 않은 fact_id", LLM_INVALID_RESPONSE)
        else:
            field_name, axis, fact = found
            if row.common_key not in COMMON_KEYS[axis]:
                problem = (f"{axis.value} 컨테이너 밖 common_key", LLM_INVALID_RESPONSE)
            elif not row.quoted_text or row.quoted_text not in (fact.value_raw or ""):
                problem = ("원문 부분문자열이 아닌 인용문", LLM_INVALID_RESPONSE)
            elif (row.fact_id, row.quoted_text) in spans:
                problem = (
                    "같은 인용문을 "
                    f"{spans[(row.fact_id, row.quoted_text)]} 에 이어 다시 배정",
                    DUPLICATE_SPAN_ASSIGNMENT,
                )
            else:
                spans[(row.fact_id, row.quoted_text)] = row.common_key
                accepted.append(
                    (
                        axis,
                        SimCommonEntry(
                            fact_id=row.fact_id,
                            source_field=field_name,
                            common_key=row.common_key,
                            value_raw=row.quoted_text,
                            evidence=list(fact.evidence),
                            origin="LLM",
                            status=fact.status,
                        ),
                    )
                )
                continue
        message, reason = problem
        diagnostics.append(
            StageDiagnostic(
                stage=_CLASSIFY_STAGE,
                unit=row.fact_id,
                reason_code=reason,
                message=f"{message} 이므로 공통 프로파일에 넣지 않았다.",
                attempt=1,
            )
        )

    # 축별로 "제출한 원문 fact 가 전부 하나 이상 접지됐는가" 를 본다.
    # 한 건만 접지돼도 완료로 보면 남은 원문의 의미가 빠진 채 비교가 돈다.
    submitted: dict[SimAxis, set[str]] = {}
    for fact_id, (_, axis, _fact) in facts.items():
        submitted.setdefault(axis, set()).add(fact_id)
    covered: dict[SimAxis, set[str]] = {}
    for axis, entry in accepted:
        covered.setdefault(axis, set()).add(entry.fact_id)
    filled = {
        axis
        for axis, fact_ids in submitted.items()
        if covered.get(axis, set()) >= fact_ids
    }
    partial = {
        axis
        for axis, fact_ids in submitted.items()
        if axis not in filled and covered.get(axis)
    }
    return accepted, _structuring(
        with_input, filled, None, diagnostics, partial=partial
    )


def _structuring(
    with_input: set[SimAxis],
    filled: set[SimAxis],
    failure: str | None,
    diagnostics: list[StageDiagnostic],
    *,
    detail: str | None = None,
    partial: set[SimAxis] | None = None,
) -> dict[SimAxis, SimAxisStructuring]:
    """LLM 분류가 담당하는 축의 상태를 정하고, 실패한 축에 진단을 남긴다.

    진단의 ``unit`` 은 축 id 다 (sim.py 가 쓰는 것과 같은 규약). 그래야 나중에
    후보 진단을 읽는 쪽이 그것이 어느 비교를 설명하는지 알 수 있다.
    """

    fields_by_axis: dict[SimAxis, list[str]] = {}
    for field_name, axis in _LLM_MAP.items():
        fields_by_axis.setdefault(axis, []).append(field_name)

    statuses: dict[SimAxis, SimAxisStructuring] = {}
    for axis, field_names in fields_by_axis.items():
        if axis not in with_input:
            # 분류할 원문 자체가 없었다. 실패가 아니라 시도할 것이 없던 축이다.
            statuses[axis] = SimAxisStructuring(
                status=STRUCTURING_NOT_ATTEMPTED, source=_CLASSIFY_STAGE
            )
            continue
        if axis in filled and failure is None:
            statuses[axis] = SimAxisStructuring(
                status=STRUCTURING_COMPLETED, source=_CLASSIFY_STAGE
            )
            continue
        if failure is None and partial and axis in partial:
            # 일부만 접지됐다. 아무것도 못 건진 것과 구분해 남긴다.
            reason = CLASSIFICATION_PARTIAL
        else:
            reason = failure or CLASSIFICATION_UNRESOLVED
        statuses[axis] = SimAxisStructuring(
            status=STRUCTURING_FAILED, source=_CLASSIFY_STAGE, reason_code=reason
        )
        diagnostics.append(
            StageDiagnostic(
                stage=_CLASSIFY_STAGE,
                unit=SIM_AXIS_IDS[axis],
                reason_code=reason,
                message=(
                    f"{','.join(field_names)} 에 원문이 있는데 분류가 끝나지 "
                    "않아 이 컨테이너를 채우지 못했다. 근거 없이 Rule 로 "
                    "덮지 않는다." + (f" {detail}" if detail else "")
                ),
                attempt=1,
                terminated_because=failure,
            )
        )
    return statuses


# ------------------------------------------------------------------ 진입점


def build_common_profile(
    profile: dict[str, Any],
    llm_client: LLMClient,
    *,
    model_profile: str,
) -> SimCommonProfile:
    """프로파일 dict 하나를 공통 SIM 비교 프로파일로 옮긴다. 예외를 던지지 않는다.

    요청서 v0.1.2 든 공고 v0.2 든 같은 표를 탄다. 없는 필드는 아무 일도
    일어나지 않고, 어느 컨테이너에도 넣지 않은 필드는
    ``unmapped_source_fields`` 와 진단으로 남는다 (Slice 2 와 같은 계약).
    """

    diagnostics: list[StageDiagnostic] = []
    containers = _empty_containers()
    registry = _registry(profile)

    for field_name, (axis, key) in _RULE_MAP.items():
        containers[axis][key].extend(_rule_entries(profile, field_name, key))

    delivery, delivery_ids = _delivery_entries(profile)
    registry |= delivery_ids
    for key, entry in delivery:
        containers[SimAxis.DELIVERY][key].append(entry)

    classified, structuring = _classify(
        profile, llm_client, model_profile=model_profile, diagnostics=diagnostics
    )
    for axis, entry in classified:
        containers[axis][entry.common_key].append(entry)

    # Rule 만으로 채워지는 축은 표를 다 돌았으면 그 자리에서 끝난 것이다.
    # 상태를 정한 주체가 다르므로 source 로 남긴다.
    for axis in SimAxis:
        structuring.setdefault(
            axis,
            SimAxisStructuring(status=STRUCTURING_COMPLETED, source=_STAGE),
        )

    preserved: dict[str, list[SimCommonEntry]] = {
        name: _rule_entries(profile, field_name, name)
        for field_name, name in _PRESERVED.items()
    }

    source_fields = list(profile.get("comparison_profile") or {})
    unmapped = [name for name in source_fields if name not in _CONSUMED]
    diagnostics.extend(
        StageDiagnostic(
            stage=_STAGE,
            unit=name,
            reason_code=UNMAPPED_SOURCE_FIELD,
            message=(
                f"원본 필드 {name} 은 이번 슬라이스의 네 컨테이너 어디에도 "
                "매핑하지 않는다. 갈 곳을 추측하지 않고 목록으로 남긴다 "
                "(초안 §7.2)."
            ),
        )
        for name in unmapped
    )

    metadata = profile.get("processing_metadata") or {}
    documents = profile.get("source_documents") or []
    first_ir = documents[0].get("common_ir", {}) if documents else {}
    # 공고 프로파일은 ``source_profile_id`` (hwp:... / pdf:...) 와 ``notice_id``
    # (bizinfo:...) 를 둘 다 들고 온다. 앞의 것이 이 프로파일의 출처고 뒤의
    # 것은 공고의 정체다. 하나로 겹쳐 쓰면 같은 공고의 hwp 본과 pdf 본이
    # 구분되지 않아 4b 의 후보 저장·근거 연결이 어긋난다.
    return SimCommonProfile(
        source_profile_id=(
            profile.get("source_profile_id")
            or profile.get("profile_id")
            or profile.get("notice_id")
        ),
        notice_id=profile.get("notice_id"),
        structuring=structuring,
        schema_version=profile.get("schema_version"),
        common_ir_document_id=(
            metadata.get("common_ir_document_id") or first_ir.get("document_id")
        ),
        purpose=containers[SimAxis.PURPOSE],
        target=containers[SimAxis.TARGET],
        content=containers[SimAxis.CONTENT],
        delivery=containers[SimAxis.DELIVERY],
        benefit_overlap_evidence=preserved["benefit_overlap_evidence"],
        scale_reference_evidence=preserved["scale_reference_evidence"],
        unmapped_source_fields=unmapped,
        fact_id_registry=frozenset(registry),
        ruleset_version=SIM_RULESET_VERSION,
        prompt_version=SIM_CLASSIFICATION_PROMPT_VERSION,
        diagnostics=diagnostics,
    )
