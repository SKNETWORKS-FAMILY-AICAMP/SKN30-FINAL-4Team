"""Slice 2: Request Profile v0.1.2 → CPL 13항목 결과 (초안 §6, §6.1).

추출은 여기서 하지 않는다. 팀 프로파일이 이미 값·근거·상태를 확정해 두었고,
이 모듈은 그것을 13항목 자리에 놓고 무엇이 비었는지 그대로 보고할 뿐이다.
그래서 ``app.services`` 아래의 옛 CPL 추출기를 import 하지 않는다 —
초안 §13 단계 2 통과 조건의 "추출 이중 실행 없음" 이 그 이유다.
(테스트가 worker 소스에 그 모듈 경로 문자열이 없는지도 함께 고정한다.)

점수·확인율은 계산하지 않는다 (초안 §6.1).
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from pydantic import BaseModel

from .cpl_prompt import PURPOSE_AXIS_PROMPT_VERSION, purpose_axis_instruction
from .llm_call import generate
from .ports.llm import (
    LLMClient,
    LLMInvalidResponseError,
    LLMTimeoutError,
    LLMUnavailableError,
)

from .analysis_inputs import (
    CPL_FIELD_SOURCES,
    facts_at,
    field_name_of,
    field_states_by_name,
    read_path,
    unmapped_profile_fields,
)
from .contracts.profile_snapshot import (
    LLM_INVALID_RESPONSE,
    LLM_TIMEOUT,
    LLM_UNAVAILABLE,
)
from .contracts.cpl_result import (
    NEEDS_CONFIRMATION,
    NO_PROFILE_FIELD,
    PROFILE_FIELD_STATE_MISSING,
    PURPOSE_AXIS_CODES,
    PURPOSE_AXIS_UNRESOLVED,
    PurposeAxisAssignment,
    PurposeAxisClassification,
    SERVER_RESOLVED_CHECKBOX,
    SERVER_DERIVED_HIERARCHY_STATE,
    UNMAPPED_PROFILE_FIELD,
    CplFieldCode,
    CplItem,
    CplResult,
    CplSubfield,
    StageDiagnostic,
    aggregate_display,
    display_status,
)

_STAGE = "build_cpl_result"
# AGENTS.md IMPLEMENTATION_PLAN: 「내역사업」 또는 「내내역사업」이 명시됐는지가
# 계층 확인의 기준이다. 세부사업만으로는 내역사업 존재를 추론하지 않는다.
_SUB_PROGRAM_LEVELS = frozenset({"sub_program", "sub_sub_program"})


def _subfield(
    profile: dict[str, Any],
    path: str,
    states: dict[str, dict[str, Any]],
) -> CplSubfield:
    """경로 하나를 하위 필드 하나로 옮긴다.

    상태는 ``field_states`` 문자열 그대로다. 사실 목록이 비었다고 상태를
    만들어내지 않고, 반대로 상태가 identified 라고 값을 지어내지도 않는다.
    """

    name = field_name_of(path)
    if name == "request_type":
        return _request_type_subfield(profile, path)
    if path == "program_hierarchy.nodes":
        return _program_nodes_subfield(profile, path)
    state = states.get(name)
    if state is None:
        # 컨테이너가 아예 없는 경우와 field_states 에만 없는 경우가 같은 결론이다:
        # 상태를 알 수 없다. 다른 필드의 상태를 빌려오지 않는다.
        return CplSubfield(
            profile_field=path,
            profile_field_name=name,
            status=None,
            reason_codes=[PROFILE_FIELD_STATE_MISSING],
            facts=facts_at(profile, path),
        )
    return CplSubfield(
        profile_field=path,
        profile_field_name=name,
        status=state.get("status"),
        reason_codes=list(state.get("reason_codes") or []),
        facts=facts_at(profile, path),
    )


def _request_type_subfield(profile: dict[str, Any], path: str) -> CplSubfield:
    """요청유형만 상태를 Rule 로 정한다.

    ``field_states`` 25 개에 ``request_type`` 은 없다. 누락이 아니라 이 필드가
    LLM 의미 선택 대상이 아니기 때문이다. 초안 §6 이 "서버 체크박스 결과 사용"
    으로 못 박은 값이라 판정 입력이 닫혀 있고 결정적이다. 여기에
    PROFILE_FIELD_STATE_MISSING 을 붙이면 완전히 접지된 Rule 결과가 화면에서
    "상태 모름" 으로 보인다. 그래서 체크박스 해소 여부로 직접 정한다.
    LLM 이 미해결 체크를 추정하는 경로는 만들지 않는다.
    """

    facts = facts_at(profile, path)
    request_type = profile.get(path) if isinstance(profile.get(path), dict) else None
    if request_type is None:
        status = "not_found"
    elif request_type.get("selected_code"):
        status = "identified"
    else:
        # 요청유형 영역은 있는데 선택을 확정하지 못한 경우다. 부재와 구분한다.
        status = "mentioned_unresolved"
    return CplSubfield(
        profile_field=path,
        profile_field_name="request_type",
        status=status,
        reason_codes=[SERVER_RESOLVED_CHECKBOX],
        facts=facts,
    )


def _program_nodes_subfield(profile: dict[str, Any], path: str) -> CplSubfield:
    """LLM 이 고른 계층에서 서버가 표시 상태를 계산한다.

    ``request_type`` 과는 다르다. 그쪽은 체크박스라 값 자체를 서버가 판정하지만,
    계층은 LLM 이 고른다 — ``ProgramNodeSelection.level`` 이 응답 계약에 있다.
    여기서 서버가 하는 일은 그 노드를 읽어 표시 상태를 정하는 것뿐이고, 노드를
    만들거나 등급을 바꾸지 않는다. 검증을 설계할 때 이 구분이 중요하다:
    계층이 틀리면 그것은 추출 문제이지 이 함수의 문제가 아니다.

    ``field_states`` 25 개에 ``nodes`` 는 없다. 누락이 아니라 그것이 Raw Fact
    필드가 아니라서다. 그대로 두면 완전히 접지된 노드가 화면에서 "상태 모름"
    으로 보이고, 그 하나 때문에 IMPLEMENTATION_PLAN 이 무슨 근거를 확보하든
    ``needs_confirmation`` 을 벗어나지 못한다.

    등급은 AGENTS.md ``IMPLEMENTATION_PLAN`` 절을 따른다: 세부사업만 명시된
    경우 내역사업 존재를 추론하지 않고 확인 필요로 둔다. 그 절의 나머지 분기
    (연차별·내역사업별 계획을 ``source_role`` 로 나눠 판정)는 계층이 아니라
    추진내용에 걸린 규칙이라 여기서 구현하지 않는다.
    """

    facts = facts_at(profile, path)
    nodes = read_path(profile, path)
    rows = [row for row in nodes if isinstance(row, dict)] if isinstance(nodes, list) else []
    levels = {row.get("level") for row in rows}
    if not rows:
        status = "not_found"
    elif levels & _SUB_PROGRAM_LEVELS:
        status = "identified"
    else:
        # 세부사업만 있거나 계층을 읽지 못한 경우다. 둘 다 내역사업 존재를
        # 추론할 근거가 아니므로 부재와 구분해 확인 필요로 둔다.
        status = "mentioned_unresolved"
    return CplSubfield(
        profile_field=path,
        profile_field_name=field_name_of(path),
        status=status,
        reason_codes=[SERVER_DERIVED_HIERARCHY_STATE],
        facts=facts,
    )


def _representative(subfields: list[CplSubfield]) -> tuple[str, str | None]:
    """대표 신호등 표시값과 그 사유.

    하위 필드가 여럿이면 AGENTS.md ``IMPLEMENTATION_PLAN`` 절의 집계 규칙을
    일반화해 하나로 접는다. 그 항목에만 걸린 규칙(세부사업만 명시되면 내역사업
    존재를 추론하지 않는다 등)은 그 항목의 것이므로 여기로 옮기지 않는다.

    대응 필드가 아예 없는 항목(``NEW_OR_CHANGED_CONTENT``, 초안 §6)은
    ``needs_confirmation`` 이다. ``no_content`` 는 "적용 대상인데 문서에서
    내용을 찾지 못했다" 는 문서에 대한 판정인데, 여기서는 문서를 그 항목으로
    읽어본 적이 없다. 확인할 근거를 확보하지 못했다는 사실을 문서가 비었다는
    판정으로 바꾸지 않는다 (초안 §6.1 과 같은 이유).
    """

    if not subfields:
        return NEEDS_CONFIRMATION, NO_PROFILE_FIELD
    return aggregate_display(display_status(sub.status) for sub in subfields), None


def build_cpl_result(profile: dict[str, Any]) -> CplResult:
    """프로파일 dict 하나를 CPL 13항목 결과로 옮긴다. 예외를 던지지 않는다."""

    states = field_states_by_name(profile)
    metadata = profile.get("processing_metadata") or {}
    documents = profile.get("source_documents") or []
    first_ir = documents[0].get("common_ir", {}) if documents else {}

    items: list[CplItem] = []
    diagnostics: list[StageDiagnostic] = []
    for code, paths in CPL_FIELD_SOURCES.items():
        subfields = [_subfield(profile, path, states) for path in paths]
        status, reason = _representative(subfields)
        items.append(
            CplItem(
                field_code=code,
                representative_status=status,
                status_reason=reason,
                subfields=subfields,
            )
        )
        if not paths:
            diagnostics.append(
                StageDiagnostic(
                    stage=_STAGE,
                    unit=code.value,
                    reason_code=NO_PROFILE_FIELD,
                    message=(
                        "초안 §6 은 이 항목을 대응 필드 공백으로 기록한다. "
                        "프로파일에 옮겨올 값이 없으므로 근사 필드로 채우지 않으며, "
                        "변경내용 근거 보존 계약이 별도로 필요하다."
                    ),
                )
            )

    unmapped = unmapped_profile_fields(profile)
    diagnostics.extend(
        StageDiagnostic(
            stage=_STAGE,
            unit=name,
            reason_code=UNMAPPED_PROFILE_FIELD,
            message=(
                f"프로파일 필드 {name} 은 어떤 CPL 항목에도 매핑되지 않는다. "
                "표시하지 않을 뿐 버리지 않는다 (초안 §6)."
            ),
        )
        for name in unmapped
    )

    return CplResult(
        items=items,
        profile_id=profile.get("profile_id"),
        common_ir_document_id=(
            metadata.get("common_ir_document_id") or first_ir.get("document_id")
        ),
        common_ir_source_sha256=(
            metadata.get("common_ir_source_sha256") or first_ir.get("source_sha256")
        ),
        candidate_pack_id=read_path(
            profile, "processing_metadata.candidate_pack.candidate_pack_id"
        ),
        pipeline_version=metadata.get("pipeline_version"),
        structured_schema_version=metadata.get("structured_schema_version"),
        model_id=metadata.get("model_id"),
        prompt_version=metadata.get("prompt_version"),
        unmapped_profile_fields=unmapped,
        diagnostics=diagnostics,
    )


# --------------------------------------------------- 의미 축 (LLM 조립 계층)

_PURPOSE_STAGE = "classify_purpose_axis"
_PURPOSE_FIELD = "comparison_profile.purpose_goal"
_TRANSPORT_REASONS = {
    LLMUnavailableError: LLM_UNAVAILABLE,
    LLMTimeoutError: LLM_TIMEOUT,
    LLMInvalidResponseError: LLM_INVALID_RESPONSE,
}


class _AxisRow(BaseModel):
    fact_id: str
    axis_code: str
    quoted_text: str


class _AxisResponse(BaseModel):
    # 엔벨로프 키는 필수다. 기본값을 주면 ``{}`` 가 "빈 배치" 로 조용히
    # 통과해 최상위 오류가 정상 응답으로 둔갑한다.
    assignments: list[_AxisRow]


def _purpose_facts(result: CplResult) -> list[CplFact]:
    return [
        fact
        for item in result.items
        for subfield in item.subfields
        if subfield.profile_field == _PURPOSE_FIELD
        for fact in subfield.facts
        if fact.fact_id
    ]


def _classify(
    facts: list[CplFact],
    llm_client: LLMClient,
    *,
    model_profile: str,
) -> PurposeAxisClassification:
    """축 이름과 인용문만 받는다. 값·오프셋·근거는 CPL 것을 그대로 쓴다.

    서버가 fact_id 존재·어휘 소속·인용문 부분문자열을 검사하고, 통과하지
    못한 행은 버린다. 버린 사실은 ``dropped`` 에 남긴다. 예산은 1 이며 같은
    입력으로 재시도하지 않는다.
    """

    if not facts:
        return PurposeAxisClassification(attempted=False)
    try:
        response = generate(
            llm_client,
            task_name="cpl_purpose_axis_classification",
            instructions=purpose_axis_instruction(sorted(PURPOSE_AXIS_CODES)),
            payload={
                "axis_vocabulary": sorted(PURPOSE_AXIS_CODES),
                "facts": [
                    {"fact_id": fact.fact_id, "value_raw": fact.value_raw}
                    for fact in facts
                ],
            },
            response_schema=_AxisResponse,
            model_profile=model_profile,
        )
    except (LLMTimeoutError, LLMUnavailableError, LLMInvalidResponseError) as error:
        return PurposeAxisClassification(
            attempted=True,
            reason_code=_TRANSPORT_REASONS[type(error)],
            prompt_version=PURPOSE_AXIS_PROMPT_VERSION,
        )

    by_id = {fact.fact_id: fact for fact in facts}
    assignments: list[PurposeAxisAssignment] = []
    dropped: list[str] = []
    for row in response.assignments:
        fact = by_id.get(row.fact_id)
        if (
            fact is None
            or row.axis_code not in PURPOSE_AXIS_CODES
            or not row.quoted_text
            or row.quoted_text not in (fact.value_raw or "")
        ):
            dropped.append(row.fact_id)
            continue
        assignments.append(
            PurposeAxisAssignment(
                fact_id=row.fact_id,
                axis_code=row.axis_code,
                quoted_text=row.quoted_text,
            )
        )
    return PurposeAxisClassification(
        attempted=True,
        assignments=assignments,
        reason_code=None if assignments else PURPOSE_AXIS_UNRESOLVED,
        dropped=dropped,
        prompt_version=PURPOSE_AXIS_PROMPT_VERSION,
    )


def _with_axes(
    result: CplResult, classification: PurposeAxisClassification
) -> CplResult:
    """축이 붙은 목적 fact 를 축마다 한 줄로 보존한다.

    한 원문이 축을 둘 가지면 fact 를 둘로 남긴다. 축이 단수라야 소비 쪽
    필터가 (필드, 축) 한 쌍으로 끝난다. 축을 못 받은 fact 는 ``axis_code``
    없이 그대로 남는다 — 축은 값이 아니므로 분류 실패가 값을 지우지 않는다.
    """

    if not classification.assignments:
        return replace(result, purpose_axis=classification)
    by_fact: dict[str, list[tuple[str, str]]] = {}
    for row in classification.assignments:
        by_fact.setdefault(row.fact_id, []).append((row.axis_code, row.quoted_text))

    def expand(subfield: CplSubfield) -> CplSubfield:
        if subfield.profile_field != _PURPOSE_FIELD:
            return subfield
        facts: list[CplFact] = []
        for fact in subfield.facts:
            assigned = by_fact.get(fact.fact_id or "")
            if not assigned:
                facts.append(fact)
                continue
            facts.extend(
                replace(fact, axis_code=code, axis_quoted_text=quoted)
                for code, quoted in assigned
            )
        return replace(subfield, facts=facts)

    return replace(
        result,
        items=[
            replace(item, subfields=[expand(row) for row in item.subfields])
            for item in result.items
        ],
        purpose_axis=classification,
    )


def analyze_cpl(
    profile: dict[str, Any],
    llm_client: LLMClient,
    *,
    model_profile: str,
) -> CplResult:
    """CPL 13항목에 의미 축까지 확정한다. 예외를 던지지 않는다.

    ``build_cpl_result`` 는 그대로 결정적이다. 축만 이 위에서 붙인다. 축이
    비어도 13항목 값·근거·상태는 바뀌지 않으므로, LLM 이 죽어도 Rule 결과가
    통째로 사라지지 않는다 (초안 §9.4).

    축을 판정 시점이 아니라 확정 시점에 붙이는 이유는, 같은 원문을 소비하는
    두 단계가 각자 다시 해석하면 화면과 판정이 갈라지기 때문이다.
    """

    result = build_cpl_result(profile)
    return _with_axes(
        result,
        _classify(_purpose_facts(result), llm_client, model_profile=model_profile),
    )
