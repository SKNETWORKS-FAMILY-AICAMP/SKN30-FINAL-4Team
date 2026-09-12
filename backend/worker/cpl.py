"""Slice 2: Request Profile v0.1.2 → CPL 13항목 결과 (초안 §6, §6.1).

추출은 여기서 하지 않는다. 팀 프로파일이 이미 값·근거·상태를 확정해 두었고,
이 모듈은 그것을 13항목 자리에 놓고 무엇이 비었는지 그대로 보고할 뿐이다.
그래서 ``app.services`` 아래의 옛 CPL 추출기를 import 하지 않는다 —
초안 §13 단계 2 통과 조건의 "추출 이중 실행 없음" 이 그 이유다.
(테스트가 worker 소스에 그 모듈 경로 문자열이 없는지도 함께 고정한다.)

점수·확인율은 계산하지 않는다 (초안 §6.1).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from hashlib import sha256
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, Field

from .cpl_coverage import CplFragment, build_fragments, detect_coverage_gaps
from .cpl_delivery import (
    ALLOWED_MEMBER_KINDS,
    DeliveryPairOccurrence,
    build_delivery_pair_candidates,
)
from .cpl_prompt import (
    PromptUnavailableError,
    load_purpose_axis_prompt,
    load_recheck_prompt,
)
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
    EXTRACTION_COVERAGE_GAP,
    RECHECK_NO_VALID_OCCURRENCE,
    RECHECK_RECOVERED,
    CplEvidence,
    CplFact,
    PROMPT_UNAVAILABLE,
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
_RECHECK_STAGE = "cpl_purpose_recheck"
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
    return aggregate_display(_display(sub) for sub in subfields), None


def _display(subfield: CplSubfield) -> str:
    """하위 필드 하나의 표시값.

    구조화 상태는 ``not_found`` 그대로 두되, 원문에 그 구역이 있는데 값이
    비었다면 표시는 "내용 없음" 이 아니다. 실패 원인이 무엇인지는 몰라도
    문서에 내용이 없다고 확정할 수 없다는 것은 확실하다 (초안 §6.1).
    """

    if RECHECK_RECOVERED in subfield.reason_codes:
        # status 는 구조화 1차 결과(not_found)로 남겨 이력을 지우지 않는다.
        # 표시는 재검이 확보한 값을 따른다.
        return display_status("identified")
    if EXTRACTION_COVERAGE_GAP in subfield.reason_codes:
        return NEEDS_CONFIRMATION
    return display_status(subfield.status)


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
_DELIVERY_FIELD = "comparison_profile.delivery_relations"
_TRANSPORT_REASONS = {
    LLMUnavailableError: LLM_UNAVAILABLE,
    LLMTimeoutError: LLM_TIMEOUT,
    LLMInvalidResponseError: LLM_INVALID_RESPONSE,
}


class _AxisRow(BaseModel):
    evidence_ref: str
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


def _purpose_fragments(
    facts: list[CplFact], common_ir: Mapping[str, Any] | None
) -> list[CplFragment]:
    """축을 고를 원문 구역. 값이 아니라 구역을 준다.

    구조화가 구역의 일부만 값으로 고르는 것이 관측됐다. mockup_08 사업목적은
    구역 73 자 중 21 자만 값이 된 실행이 30 회 중 16 회였고, 남은 앞부분에 대상과
    방향이 둘 다 들어 있었다. 그 잘린 문자열만 주면 모델이 고를 수 있는 축이
    남은 절에 갇혀, FIT-1 이 ``지원기업 120개사`` 를 대상 조건으로 쓰는 결과가
    나온다. 근거가 부족해서가 아니라 잘린 입력으로 확정한 판정이다.

    이미 값이 나온 구역만 고른다. 값이 하나도 없는 구역은 축이 아니라 재검
    대상이고, 그 경로는 따로 있다.
    """

    if common_ir is None:
        return []
    occurrences = {
        occurrence_id
        for fact in facts
        for evidence in fact.evidence
        for occurrence_id in evidence.common_ir_occurrence_ids
    }
    # 정확히 같은 occurrence 만 붙인다. 구조화가 셀을 근거로 적으면 구역 계산도
    # 셀에서 구역을 찾으므로 계층이 갈리지 않는다 (저장 프로필 10 건과 트레이스
    # 38 건 모두 정확 일치). 계층까지 허용하면 부모 하나가 자식 구역 여럿에
    # 걸릴 때 어느 구역인지 추측하게 되고, 그 추측이 잘못된 근거 연결을 만든다.
    return [
        fragment
        for fragment in build_fragments(common_ir, profile_field=_PURPOSE_FIELD)
        if fragment.common_ir_occurrence_id in occurrences
    ]


def _classify(
    fragments: list[CplFragment],
    llm_client: LLMClient,
    *,
    model_profile: str,
) -> PurposeAxisClassification:
    """구역 원문에서 축과 인용문을 받는다. 재검과 같은 occurrence 계약이다.

    서버가 요청한 ``evidence_ref`` 인지, 어휘에 있는 축인지, 인용문이 그 구역
    원문의 부분문자열인지 검사하고 통과하지 못한 행은 버린다. 버린 사실은
    ``dropped`` 에 남긴다. 예산은 1 이며 같은 입력으로 재시도하지 않는다.

    인용문의 좌표는 승격할 때 구역 안에서 다시 찾는다. 잘린 값의 좌표를 그대로
    물려받으면 FIT 이 쓰는 문구와 역추적하는 자리가 어긋난다.
    """

    if not fragments:
        return PurposeAxisClassification(attempted=False)
    try:
        prompt = load_purpose_axis_prompt()
    except PromptUnavailableError:
        # 문구를 못 읽은 채 낸 분류는 버전을 신뢰할 수 없다. 값·근거는 그대로
        # 두고 축만 비운다.
        return PurposeAxisClassification(
            attempted=False, reason_code=PROMPT_UNAVAILABLE
        )
    try:
        response = generate(
            llm_client,
            task_name="cpl_purpose_axis_classification",
            instructions=prompt.text,
            payload={
                "axis_vocabulary": sorted(PURPOSE_AXIS_CODES),
                "purpose_regions": [
                    {
                        "evidence_ref": fragment.evidence_ref,
                        "raw_text": fragment.raw_text,
                    }
                    for fragment in fragments
                ],
            },
            response_schema=_AxisResponse,
            model_profile=model_profile,
        )
    except (LLMTimeoutError, LLMUnavailableError, LLMInvalidResponseError) as error:
        return PurposeAxisClassification(
            attempted=True,
            reason_code=_TRANSPORT_REASONS[type(error)],
            prompt_version=prompt.version,
            prompt_sha256=prompt.sha256,
        )

    by_ref = {fragment.evidence_ref: fragment for fragment in fragments}
    assignments: list[PurposeAxisAssignment] = []
    dropped: list[str] = []
    for row in response.assignments:
        fragment = by_ref.get(row.evidence_ref)
        if (
            fragment is None
            or row.axis_code not in PURPOSE_AXIS_CODES
            or not row.quoted_text
            or row.quoted_text not in fragment.raw_text
        ):
            dropped.append(row.evidence_ref)
            continue
        assignments.append(
            PurposeAxisAssignment(
                evidence_ref=row.evidence_ref,
                axis_code=row.axis_code,
                quoted_text=row.quoted_text,
            )
        )
    return PurposeAxisClassification(
        attempted=True,
        assignments=assignments,
        reason_code=None if assignments else PURPOSE_AXIS_UNRESOLVED,
        dropped=dropped,
        prompt_version=prompt.version,
        prompt_sha256=prompt.sha256,
    )


def _with_axes(
    result: CplResult,
    classification: PurposeAxisClassification,
    fragments: list[CplFragment],
) -> CplResult:
    """축마다 인용 구간을 가리키는 fact 를 새로 만든다.

    잘린 1 차 값에 ``axis_quoted_text`` 만 덧붙이면 안 된다. 인용문이 그 값
    밖에서 나올 수 있는데, 그러면 FIT 은 ``[9,27)`` 문구를 쓰면서 근거 id 와
    span 은 ``[61,82)`` 를 가리킨다. 값은 좋아져도 근거 역추적이 틀어진다.

    그래서 재검과 같은 계약으로 승격한다: ``value_raw`` 는 인용문, ``fact_id``
    는 없음, ``evidence_ref`` 는 구역 참조, 좌표는 구역 안에서 다시 찾은 실제
    인용 위치다. 없는 id 를 지어내지 않는다.

    한 구역이 축을 여럿 가지면 축마다 fact 를 남긴다. 축이 단수라야 소비 쪽
    필터가 (필드, 축) 한 쌍으로 끝난다.
    """

    if not classification.assignments:
        return replace(result, purpose_axis=classification)
    by_ref = {fragment.evidence_ref: fragment for fragment in fragments}
    promoted: list[CplFact] = []
    materialized: set[str] = set()
    for row in classification.assignments:
        fragment = by_ref.get(row.evidence_ref)
        if fragment is None:
            continue
        promoted.append(_axis_fact(fragment, row))
        materialized.add(fragment.common_ir_occurrence_id)

    def expand(subfield: CplSubfield) -> CplSubfield:
        if subfield.profile_field != _PURPOSE_FIELD:
            return subfield
        # 축이 구체화한 구역의 1 차 값은 승격된 fact 가 대신한다. 축을 하나도
        # 못 받은 값은 축 없이 그대로 남는다 — 축은 값이 아니므로 분류 실패가
        # 값을 지우지 않는다.
        kept = [
            fact
            for fact in subfield.facts
            if not _covers(fact, materialized)
        ]
        return replace(subfield, facts=[*kept, *promoted])

    return replace(
        result,
        items=[
            replace(item, subfields=[expand(row) for row in item.subfields])
            for item in result.items
        ],
        purpose_axis=classification,
    )


def _covers(fact: CplFact, occurrence_ids: set[str]) -> bool:
    return any(
        occurrence_id in occurrence_ids
        for evidence in fact.evidence
        for occurrence_id in evidence.common_ir_occurrence_ids
    )


def _axis_fact(fragment: CplFragment, row: PurposeAxisAssignment) -> CplFact:
    """인용문 하나를 그 자리 좌표와 함께 fact 로 올린다."""

    offset = fragment.raw_text.index(row.quoted_text)
    start = fragment.start_char + offset
    return CplFact(
        # 구조화가 만든 값이 아니다. 가짜 id 를 지어내지 않고 그 인용문이 나온
        # 구역 참조를 그대로 둔다.
        fact_id=None,
        evidence_ref=fragment.evidence_ref,
        value_raw=row.quoted_text,
        status="identified",
        source_block_id=fragment.common_ir_block_id,
        start_char=start,
        end_char=start + len(row.quoted_text),
        text_basis="cpl_purpose_axis",
        evidence=[
            CplEvidence(
                source_block_id=fragment.common_ir_block_id,
                common_ir_document_id=fragment.common_ir_document_id,
                common_ir_block_id=fragment.common_ir_block_id,
                common_ir_occurrence_ids=[fragment.common_ir_occurrence_id],
            )
        ],
        axis_code=row.axis_code,
        axis_quoted_text=row.quoted_text,
    )


def _with_coverage_gaps(
    result: CplResult, profile: dict[str, Any], common_ir: Mapping[str, Any]
) -> CplResult:
    """라벨 구역은 있는데 값이 빈 필드에 사유를 단다. 값·축은 만들지 않는다."""

    gaps = {gap.profile_field: gap for gap in detect_coverage_gaps(profile, common_ir)}
    if not gaps:
        return result

    def mark(subfield: CplSubfield) -> CplSubfield:
        gap = gaps.get(subfield.profile_field)
        if gap is None or EXTRACTION_COVERAGE_GAP in subfield.reason_codes:
            return subfield
        # 상태는 구조화가 낸 것을 그대로 둔다. 사유만 더한다.
        return replace(
            subfield,
            reason_codes=[*subfield.reason_codes, EXTRACTION_COVERAGE_GAP],
        )

    items = []
    for item in result.items:
        subfields = [mark(row) for row in item.subfields]
        if subfields == item.subfields:
            items.append(item)
            continue
        # 사유가 붙었으면 대표 표시도 다시 접는다. 상태를 바꾸지 않고 사유만
        # 더해 놓으면 화면은 여전히 "내용 없음" 이다.
        status, reason = _representative(subfields)
        items.append(
            replace(
                item,
                subfields=subfields,
                representative_status=status,
                status_reason=reason,
            )
        )
    diagnostics = [
        *result.diagnostics,
        *(
            StageDiagnostic(
                stage=_STAGE,
                unit=gap.profile_field,
                reason_code=EXTRACTION_COVERAGE_GAP,
                message=(
                    f"원문 {', '.join(gap.label_block_ids)} 에 라벨 구역이 있으나 "
                    f"값이 비었다 (상태 {gap.status})."
                ),
            )
            for gap in gaps.values()
        ),
    ]
    return replace(result, items=items, diagnostics=diagnostics)


# ------------------------------------------------------- 구역 재검 (1회 한정)


# 필드에 기본값을 둔다. 한 행이 구조적으로 어긋났을 때 응답 전체를 무효로 만들면
# 목적 응답 하나 때문에 수행관계 판정까지 같이 죽는다. 빠진 값은 빈 문자열로 받아
# 서버 검증에서 사유와 함께 떨어뜨리고, 섹션끼리는 서로를 무너뜨리지 않는다.
class _RecheckRow(BaseModel):
    evidence_ref: str = ""
    raw_text: str = ""
    axis_code: str = ""


class _DeliveryDecision(BaseModel):
    candidate_id: str = ""
    accept: bool = False
    actor_evidence_refs: list[str] = Field(default_factory=list)
    member_evidence_refs: list[str] = Field(default_factory=list)
    member_kind: str | None = None


class _RecheckResponse(BaseModel):
    purpose_occurrences: list[_RecheckRow] = Field(default_factory=list)
    delivery_decisions: list[_DeliveryDecision] = Field(default_factory=list)


@dataclass(frozen=True, slots=True)
class DeliveryRelationDraft:
    """검증을 통과한 수행관계 판정 하나.

    아직 Fact 가 아니다. 원문 좌표와 member 종류까지 확정했을 뿐이고, 관계를
    CPL 결과에 올리는 것은 별도 단계다.
    """

    candidate_id: str
    common_ir_block_id: str
    member_kind: str
    actor: tuple[DeliveryPairOccurrence, ...]
    member: tuple[DeliveryPairOccurrence, ...]


@dataclass(frozen=True, slots=True)
class _RecheckOutcome:
    """한 번의 재검이 낸 것. 섹션마다 사유가 따로다."""

    facts: list[CplFact] = field(default_factory=list)
    purpose_reason: str | None = None
    purpose_dropped: list[str] = field(default_factory=list)
    delivery: list[DeliveryRelationDraft] = field(default_factory=list)
    delivery_reason: str | None = None
    delivery_dropped: list[str] = field(default_factory=list)


def _accepted_delivery(candidates, decisions):
    """모델이 고른 판정을 검증한다. 문자열을 새로 만들게 하지 않는다."""

    by_id = {row.candidate_id: row for row in candidates}
    drafts: list[DeliveryRelationDraft] = []
    dropped: list[str] = []
    seen: set[str] = set()
    for decision in decisions:
        candidate = by_id.get(decision.candidate_id)
        if candidate is None:
            dropped.append(f"{decision.candidate_id}: 요청에 없는 candidate_id")
            continue
        if decision.candidate_id in seen:
            dropped.append(f"{decision.candidate_id}: 같은 후보에 판정이 둘")
            continue
        seen.add(decision.candidate_id)
        if not decision.accept:
            continue
        if decision.member_kind not in ALLOWED_MEMBER_KINDS:
            dropped.append(f"{decision.candidate_id}: 어휘 밖 member_kind")
            continue
        sides = []
        for side, refs in (
            ("actor", decision.actor_evidence_refs),
            ("member", decision.member_evidence_refs),
        ):
            allowed = candidate.evidence_refs(side)
            chosen = [ref for ref in refs if ref in allowed]
            if not chosen or len(chosen) != len(set(refs)):
                sides = []
                dropped.append(f"{decision.candidate_id}: {side} evidence_ref 가 그 칸의 것이 아니다")
                break
            sides.append(chosen)
        if not sides:
            continue
        drafts.append(
            DeliveryRelationDraft(
                candidate_id=decision.candidate_id,
                common_ir_block_id=candidate.common_ir_block_id,
                member_kind=decision.member_kind,
                actor=tuple(
                    row for row in candidate.actor.occurrences
                    if row.evidence_ref in sides[0]
                ),
                member=tuple(
                    row for row in candidate.member.occurrences
                    if row.evidence_ref in sides[1]
                ),
            )
        )
    return drafts, dropped


def _recovered_facts(fragments, rows):
    """검증을 통과한 행만 fact 로 만든다. 탈락 행은 사유와 함께 돌려준다."""

    by_ref = {fragment.evidence_ref: fragment for fragment in fragments}
    facts = []
    dropped = []
    for row in rows:
        fragment = by_ref.get(row.evidence_ref)
        if fragment is None:
            dropped.append(f"{row.evidence_ref}: 요청에 없는 evidence_ref")
            continue
        if row.axis_code not in PURPOSE_AXIS_CODES:
            dropped.append(f"{row.evidence_ref}: 어휘 밖 axis_code")
            continue
        offset = fragment.raw_text.find(row.raw_text)
        if not row.raw_text or offset < 0:
            dropped.append(f"{row.evidence_ref}: 구역 원문의 부분문자열이 아니다")
            continue
        start = fragment.start_char + offset
        facts.append(
            CplFact(
                # 구조화가 만든 id 가 없다. 가짜 id 를 지어내지 않고 그 값이 나온
                # 구역 참조를 그대로 둔다.
                fact_id=None,
                evidence_ref=fragment.evidence_ref,
                value_raw=row.raw_text,
                status="identified",
                source_block_id=fragment.common_ir_block_id,
                start_char=start,
                end_char=start + len(row.raw_text),
                text_basis="cpl_purpose_recheck",
                evidence=[
                    CplEvidence(
                        source_block_id=fragment.common_ir_block_id,
                        common_ir_document_id=fragment.common_ir_document_id,
                        common_ir_block_id=fragment.common_ir_block_id,
                        common_ir_occurrence_ids=[fragment.common_ir_occurrence_id],
                    )
                ],
                axis_code=row.axis_code,
                axis_quoted_text=row.raw_text,
            )
        )
    return facts, dropped


def _delivery_facts(drafts):
    """채택된 판정을 관계 Fact 로 올린다. 모델이 고르지 않은 문자열은 만들지 않는다.

    한 actor 에 member 가 여럿이면 한 관계로 묶고 순서를 ``member_index`` 로
    남긴다. actor 가 여럿이면 actor 마다 관계를 나눈다 — 서로 다른 기관을 한
    관계에 넣으면 누가 무엇을 하는지가 사라진다. 양쪽 모두 여럿인 경우는 후보
    단계에서 이미 ``DELIVERY_PAIR_AMBIGUOUS`` 로 빠졌다.
    """

    facts: list[CplFact] = []
    for draft in drafts:
        for actor in draft.actor:
            # 관계 id 는 후보와 actor 좌표에서 결정적으로 나온다. 같은 문서를
            # 다시 처리하면 같은 id 가 나와야 저장된 결과와 대조된다.
            relation_id = "recheck_" + sha256(
                f"{draft.candidate_id}{actor.evidence_ref}".encode("utf-8")
            ).hexdigest()[:16]
            facts.append(
                _delivery_fact(draft, actor, member="actor", index=None,
                               relation_id=relation_id)
            )
            for index, member in enumerate(draft.member):
                facts.append(
                    _delivery_fact(draft, member, member=draft.member_kind,
                                   index=index, relation_id=relation_id)
                )
    return facts


def _delivery_fact(draft, occurrence, *, member, index, relation_id):
    return CplFact(
        # 구조화가 만든 id 가 없다. 가짜 id 를 지어내지 않는다.
        fact_id=None,
        relation_id=relation_id,
        member=member,
        member_index=index,
        evidence_ref=occurrence.evidence_ref,
        value_raw=occurrence.raw_text,
        status="identified",
        source_block_id=draft.common_ir_block_id,
        start_char=0,
        end_char=len(occurrence.raw_text),
        text_basis="cpl_delivery_recheck",
        evidence=[
            CplEvidence(
                source_block_id=draft.common_ir_block_id,
                common_ir_document_id=None,
                common_ir_block_id=draft.common_ir_block_id,
                common_ir_occurrence_ids=[occurrence.common_ir_occurrence_id],
            )
        ],
    )


def _recheck(fragments, candidates, llm_client, *, model_profile):
    """문서당 한 번. 목적 구역과 수행관계 후보를 타입별 섹션으로 함께 묻는다.

    섹션을 나눠 호출하면 gap 이 둘 다 나온 문서에서 재검이 두 번 돌아 "문서당
    1 회" 가 깨진다. 대신 실패는 섹션끼리 옮지 않는다 — 응답 자체를 못 읽으면
    보낸 섹션이 모두 실패하고, 한 섹션의 행이 검증에서 떨어지면 그 섹션만
    실패한다. 보내지 않은 섹션은 판정도 경고도 만들지 않는다.
    """

    empty = _RecheckOutcome()
    if not fragments and not candidates:
        return empty
    try:
        prompt = load_recheck_prompt()
    except PromptUnavailableError:
        return _RecheckOutcome(
            purpose_reason=PROMPT_UNAVAILABLE if fragments else None,
            delivery_reason=PROMPT_UNAVAILABLE if candidates else None,
        )
    try:
        response = generate(
            llm_client,
            task_name="cpl_recheck",
            instructions=prompt.text,
            payload={
                "axis_vocabulary": sorted(PURPOSE_AXIS_CODES),
                "purpose_regions": [
                    {"evidence_ref": row.evidence_ref, "raw_text": row.raw_text}
                    for row in fragments
                ],
                "delivery_pairs": [
                    {
                        "candidate_id": row.candidate_id,
                        "allowed_member_kinds": list(row.allowed_member_kinds),
                        "actor": [
                            {"evidence_ref": item.evidence_ref, "raw_text": item.raw_text}
                            for item in row.actor.occurrences
                        ],
                        "member": [
                            {"evidence_ref": item.evidence_ref, "raw_text": item.raw_text}
                            for item in row.member.occurrences
                        ],
                    }
                    for row in candidates
                ],
            },
            response_schema=_RecheckResponse,
            model_profile=model_profile,
        )
    except (LLMTimeoutError, LLMUnavailableError, LLMInvalidResponseError) as error:
        # 최상위 응답을 식별할 수 없다. 보낸 섹션이 모두 실패한다.
        reason = _TRANSPORT_REASONS[type(error)]
        return _RecheckOutcome(
            purpose_reason=reason if fragments else None,
            delivery_reason=reason if candidates else None,
        )

    facts: list[CplFact] = []
    purpose_reason = None
    purpose_dropped: list[str] = []
    if fragments:
        facts, purpose_dropped = _recovered_facts(
            fragments, list(response.purpose_occurrences)
        )
        # 형식은 정상인데 통과한 것이 하나도 없는 경우를 전송 실패와 구분한다.
        purpose_reason = None if facts else RECHECK_NO_VALID_OCCURRENCE

    drafts: list[DeliveryRelationDraft] = []
    delivery_reason = None
    delivery_dropped: list[str] = []
    if candidates:
        drafts, delivery_dropped = _accepted_delivery(
            candidates, list(response.delivery_decisions)
        )
        delivery_reason = None if drafts else RECHECK_NO_VALID_OCCURRENCE

    return _RecheckOutcome(
        facts=facts,
        purpose_reason=purpose_reason,
        purpose_dropped=purpose_dropped,
        delivery=drafts,
        delivery_reason=delivery_reason,
        delivery_dropped=delivery_dropped,
    )


def _with_recheck(result, common_ir, llm_client, *, model_profile):
    """구역 누락 후보가 있으면 그 구역만 한 번 재검한다."""

    def gapped(path):
        return next(
            (
                subfield
                for item in result.items
                for subfield in item.subfields
                if EXTRACTION_COVERAGE_GAP in subfield.reason_codes
                and subfield.profile_field == path
            ),
            None,
        )

    target = gapped(_PURPOSE_FIELD)
    fragments = (
        build_fragments(common_ir, profile_field=_PURPOSE_FIELD) if target else []
    )
    delivery_target = gapped(_DELIVERY_FIELD)
    candidates: list = []
    if delivery_target is not None:
        candidates, _ = build_delivery_pair_candidates(common_ir)
    if not fragments and not candidates:
        return result

    outcome = _recheck(
        fragments, candidates, llm_client, model_profile=model_profile
    )
    # 이미 값이 있는 필드에는 재검 Fact 를 더하지 않는다. 1 차 추출이 낸 관계와
    # 재검이 낸 관계가 한 자리에서 섞이면 어느 쪽이 근거인지 알 수 없다.
    delivery_facts = (
        _delivery_facts(outcome.delivery)
        if delivery_target is not None and not delivery_target.facts
        else []
    )
    facts, reason, dropped = (
        outcome.facts, outcome.purpose_reason, outcome.purpose_dropped
    )

    def revise(subfield):
        if subfield is delivery_target:
            if not delivery_facts:
                if outcome.delivery_reason:
                    return replace(
                        subfield,
                        reason_codes=[*subfield.reason_codes, outcome.delivery_reason],
                    )
                return subfield
            # 되찾았으므로 활성 사유에서 후보 표시를 걷는다. status 는 구조화
            # 1 차 결과라 그대로 둔다 — 처음에 놓쳤다는 이력이 사라진다.
            codes = [c for c in subfield.reason_codes if c != EXTRACTION_COVERAGE_GAP]
            codes.append(RECHECK_RECOVERED)
            return replace(
                subfield,
                reason_codes=codes,
                facts=[*subfield.facts, *delivery_facts],
            )
        if subfield is not target:
            return subfield
        if facts:
            # 되찾았으므로 활성 사유에서 후보 표시를 걷는다. 원래 진단은
            # diagnostics 에 이력으로 남는다.
            codes = [c for c in subfield.reason_codes if c != EXTRACTION_COVERAGE_GAP]
            codes.append(RECHECK_RECOVERED)
        elif reason:
            codes = [*subfield.reason_codes, reason]
        else:
            codes = list(subfield.reason_codes)
        # status 는 구조화 1차 결과다. 재검이 되찾았어도 덮어쓰지 않는다 —
        # 처음에 놓쳤다는 이력이 사라진다.
        return replace(subfield, reason_codes=codes, facts=[*subfield.facts, *facts])

    items = []
    for item in result.items:
        subfields = [revise(row) for row in item.subfields]
        if subfields == item.subfields:
            items.append(item)
            continue
        status, status_reason = _representative(subfields)
        items.append(
            replace(
                item,
                subfields=subfields,
                representative_status=status,
                status_reason=status_reason,
            )
        )
    detail = f"구역 {len(fragments)}개 재검: 복구 {len(facts)}건"
    if dropped:
        detail += f", 탈락 {len(dropped)}건 ({'; '.join(dropped[:3])})"
    if candidates:
        detail += (
            f" / 수행관계 후보 {len(candidates)}건: 채택 {len(outcome.delivery)}건"
        )
        if outcome.delivery_dropped:
            detail += f", 탈락 {len(outcome.delivery_dropped)}건"
    diagnostics = [
        *result.diagnostics,
        StageDiagnostic(
            stage=_RECHECK_STAGE,
            unit=_PURPOSE_FIELD,
            reason_code=RECHECK_RECOVERED if facts else (reason or ""),
            message=detail,
            attempt=1,
        ),
    ]
    return replace(result, items=items, diagnostics=diagnostics)


def analyze_cpl(
    profile: dict[str, Any],
    llm_client: LLMClient,
    *,
    model_profile: str,
    common_ir: Mapping[str, Any] | None = None,
) -> CplResult:
    """CPL 13항목에 의미 축까지 확정한다. 예외를 던지지 않는다.

    ``build_cpl_result`` 는 그대로 결정적이다. 축만 이 위에서 붙인다. 축이
    비어도 13항목 값·근거·상태는 바뀌지 않으므로, LLM 이 죽어도 Rule 결과가
    통째로 사라지지 않는다 (초안 §9.4).

    축을 판정 시점이 아니라 확정 시점에 붙이는 이유는, 같은 원문을 소비하는
    두 단계가 각자 다시 해석하면 화면과 판정이 갈라지기 때문이다.
    """

    result = build_cpl_result(profile)
    if common_ir is not None:
        result = _with_coverage_gaps(result, profile, common_ir)
    facts = _purpose_facts(result)
    if not facts and common_ir is not None:
        # 1 차 추출이 값을 못 냈고 구역은 있다. 축만 붙일 대상이 없으므로 값과
        # 축을 함께 되찾는 재검으로 간다. 재검은 문서당 한 번이고, 그 한 번이
        # 목적 구역과 수행관계 후보를 함께 나른다.
        return _with_recheck(result, common_ir, llm_client, model_profile=model_profile)
    fragments = _purpose_fragments(facts, common_ir)
    classification = _classify(fragments, llm_client, model_profile=model_profile)
    if facts and not fragments and classification.reason_code is None:
        # 값은 있는데 그 값이 나온 구역을 짚을 수 없다. 축을 ``value_raw`` 로
        # 대신 물으면 좌표 없는 축이 다시 생긴다. 묻지 않고 미해결로 남긴다.
        classification = replace(
            classification, reason_code=PURPOSE_AXIS_UNRESOLVED
        )
    result = _with_axes(result, classification, fragments)
    if common_ir is None:
        return result
    # 목적은 1 차에서 나왔지만 수행체계 구역이 비어 있을 수 있다. 그 경우에도
    # 재검은 한 번이다 — 축 분류와 재검은 입력도 응답 스키마도 달라 한 호출에
    # 합치지 않는다. 재검할 것이 없으면 ``_with_recheck`` 가 호출 없이 돌아온다.
    return _with_recheck(result, common_ir, llm_client, model_profile=model_profile)
