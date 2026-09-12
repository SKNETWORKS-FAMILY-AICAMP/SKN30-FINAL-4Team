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
import re
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, Field

from .cpl_coverage import (
    CplFragment,
    absent_form_fields,
    build_fragments,
    detect_coverage_gaps,
)
from .quantities import read_quantities
from .cpl_effect import (
    EFFECT_FIELD,
    EffectRegion,
    build_effect_candidates,
)
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
    code: CplFieldCode,
) -> CplSubfield:
    """경로 하나를 하위 필드 하나로 옮긴다.

    상태는 ``field_states`` 문자열 그대로다. 사실 목록이 비었다고 상태를
    만들어내지 않고, 반대로 상태가 identified 라고 값을 지어내지도 않는다.
    """

    name = field_name_of(path)
    if name == "request_type":
        return _request_type_subfield(profile, path)
    if path == "program_hierarchy.nodes":
        if code is CplFieldCode.NEW_OR_CHANGED_CONTENT:
            return _new_unit_subfield(profile, path)
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


# 요청유형이 지목한 신설 등급 -> 그 등급의 계층 노드.
# 어휘는 벤더 ``RequestTypeCode`` · ``ProgramLevel`` 이 1:1 로 짝짓는다.
_NEW_UNIT_LEVEL = {
    "detail_program_new": "detail_program",
    "sub_program_new": "sub_program",
    "sub_sub_program_new": "sub_sub_program",
}
PRIOR_PLAN_UNAVAILABLE = "PRIOR_PLAN_UNAVAILABLE"


def _new_unit_subfield(profile: dict[str, Any], path: str) -> CplSubfield:
    """신설·변경 주요내용(CPL-05)의 계층 하위 필드.

    ``_program_nodes_subfield`` 와 경로는 같지만 묻는 것이 다르다. 저쪽은
    "내역사업별 추진계획을 볼 수 있는가" 라서 AGENTS.md 계약대로 세부사업만
    있으면 확인 필요로 둔다. 여기서는 "요청한 신설 단위가 문서에 있는가" 이므로,
    세부사업 신설 요청에 세부사업 노드가 있으면 그것으로 충분하다. 같은 경로에
    같은 규칙을 쓰면 세부사업 신설이 영영 확인되지 않는다.

    ``program_content_change`` 는 판별기준 §6.2 가 기존 사업계획과의 비교를
    요구하는데 그 입력이 파이프라인에 없다. 계층이 있다고 변경내용을 확인한
    것처럼 올리지 않고, 근거를 못 대는 이유를 사유로 남긴다.
    """

    facts = facts_at(profile, path)
    name = field_name_of(path)
    selected = read_path(profile, "request_type.selected_code")
    nodes = read_path(profile, path)
    rows = [row for row in nodes if isinstance(row, dict)] if isinstance(nodes, list) else []

    if selected == "program_content_change":
        return CplSubfield(
            profile_field=path,
            profile_field_name=name,
            status="mentioned_unresolved" if rows else "not_found",
            reason_codes=[PRIOR_PLAN_UNAVAILABLE],
            facts=facts,
        )

    wanted = _NEW_UNIT_LEVEL.get(selected or "")
    if wanted is None:
        # 요청유형을 못 읽었다. 어느 등급을 찾아야 하는지 모르므로 계층이
        # 있다는 사실만으로 확인됨으로 올리지 않는다.
        status = "mentioned_unresolved" if rows else "not_found"
    elif any(row.get("level") == wanted for row in rows):
        status = "identified"
    elif rows:
        status = "mentioned_unresolved"
    else:
        status = "not_found"
    return CplSubfield(
        profile_field=path,
        profile_field_name=name,
        status=status,
        reason_codes=[SERVER_DERIVED_HIERARCHY_STATE],
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
        subfields = [_subfield(profile, path, states, code) for path in paths]
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


# 값 앞의 절 경계. 수식어를 이 너머로 끌어오지 않는다.
_CLAUSE_START = re.compile(r"[\n○◦□■●▪‣]|(?:^|\s)-\s")


QUANTITY_CONTEXT_UNAVAILABLE = "QUANTITY_CONTEXT_UNAVAILABLE"


def _with_quantities(
    result: CplResult,
    candidate_pack: Any | None,
    hold_reason: str | None = None,
) -> CplResult:
    """값 안의 정량 표현에 비교 맥락을 붙인다. 값·상태·관계·근거는 그대로다.

    맥락은 ``value_raw`` 만으로는 부족하다. ``- 기업당 한도: 최대 5,000만원``
    에서 값이 된 것은 ``최대 5,000만원`` 뿐이라 대상 기준이 빠진다. 그래서 그
    값이 나온 블록에서 **값 앞 절까지** 다시 읽고, 값 구간 안의 숫자만 남긴다.

    원문은 **그 실행이 실제로 쓴 CandidatePack** 에서 읽는다. 정량 근거의
    ``source_block_id`` 와 좌표가 가리키는 것이 그 팩이기 때문이다. 여기서 팩을
    다시 만들면 "재생성 결과가 늘 같다" 는 가정이 하나 늘어난다.

    팩이 없거나 좌표가 그 블록을 가리키지 않으면 그 값의 파생만 건너뛴다.

    파생은 여기서 끝난다. FIT 은 결과를 읽기만 한다.
    """

    if candidate_pack is None:
        if hold_reason is None:
            return result
        # 왜 파생하지 않았는지 남긴다. 값이 없는 것과 맥락을 못 붙인 것은
        # 사용자가 해야 할 다음 행동이 다르다.
        return replace(
            result,
            diagnostics=[
                *result.diagnostics,
                StageDiagnostic(
                    stage="analyze_cpl",
                    unit=None,
                    reason_code=QUANTITY_CONTEXT_UNAVAILABLE,
                    message=hold_reason,
                ),
            ],
        )
    blocks = {row.block_id: row.text for row in candidate_pack.blocks}

    def enrich(fact: CplFact) -> CplFact:
        text = blocks.get(fact.source_block_id or "")
        start, end = fact.start_char, fact.end_char
        if text is None or start is None or end is None:
            return fact
        if text[start:end] != (fact.value_raw or ""):
            # 좌표가 그 블록을 가리키지 않는다. 추측해서 읽지 않는다.
            return fact
        head = text[:start]
        cuts = [row.end() for row in _CLAUSE_START.finditer(head)]
        floor = cuts[-1] if cuts else 0
        window = text[floor:end]
        # 좌표를 블록 기준 절대값으로 되돌린다. 창 상대 좌표로 두면 서로 다른
        # fact 의 숫자가 같은 자리인지 판별할 수 없어 자기비교를 못 막는다.
        inside = tuple(
            replace(row, start=row.start + floor, end=row.end + floor)
            for row in read_quantities(window)
            if row.start >= start - floor
        )
        return replace(fact, quantities=inside) if inside else fact

    return replace(
        result,
        items=[
            replace(
                item,
                subfields=[
                    replace(subfield, facts=[enrich(row) for row in subfield.facts])
                    for subfield in item.subfields
                ],
            )
            for item in result.items
        ],
    )


FORM_SLOT_ABSENT = "FORM_SLOT_ABSENT"


def _with_form_absence(
    result: CplResult, common_ir: Mapping[str, Any]
) -> CplResult:
    """양식에 칸이 없어 빈 하위 필드를 ``not_applicable`` 로 올린다.

    판별기준 §11.3 은 "지원조건이 별도로 없다고 해서 자동으로 오류로 판단하지
    않는다", §12.3 은 "일부 정보가 없을 경우 지원내용 부적절로 판단하지
    않는다" 고 못 박는다. 그런데 칸이 없는 필드가 ``not_found`` 로 남아 있으면
    ``aggregate_display`` 가 항목 전체를 끌어내린다 — 서식에 없는 행을
    "확인 필요" 로 표시하는 것이다.

    승격 조건은 셋 다 필요하다.

    1. 문서 어디에도 그 필드의 라벨이 없다 (``absent_form_fields``)
    2. 구조화가 그 필드를 ``not_found`` 로 냈다
    3. 그 필드에 값이 하나도 없다

    3 을 함께 보는 이유는, 라벨 없이도 값이 나왔다면 그 값이 사실이기 때문이다.
    라벨이 **있는데** 빈 경우는 여기 안 온다 — 그것은 추출 누락이고
    ``EXTRACTION_COVERAGE_GAP`` 이 맡는다.
    """

    absent = absent_form_fields(common_ir)
    if not absent:
        return result

    promoted: list[str] = []

    def mark(subfield: CplSubfield) -> CplSubfield:
        if subfield.profile_field not in absent or subfield.status != "not_found":
            return subfield
        if subfield.facts:
            # 라벨은 없는데 값은 있다. 값이 이긴다 — 없는 칸을 근거 있는 값보다
            # 위에 두지 않는다.
            return subfield
        promoted.append(subfield.profile_field)
        return replace(
            subfield,
            status="not_applicable",
            reason_codes=[*subfield.reason_codes, FORM_SLOT_ABSENT],
        )

    items = []
    for item in result.items:
        subfields = [mark(row) for row in item.subfields]
        if subfields == item.subfields:
            items.append(item)
            continue
        status, reason = _representative(subfields)
        items.append(
            replace(
                item,
                subfields=subfields,
                representative_status=status,
                status_reason=reason,
            )
        )
    if not promoted:
        return result
    diagnostics = [
        *result.diagnostics,
        *(
            StageDiagnostic(
                stage=_STAGE,
                unit=path,
                reason_code=FORM_SLOT_ABSENT,
                message=(
                    "요청서 원문에 이 필드의 라벨 구역이 없다. 추출 실패가 아니라 "
                    "양식에 대응 행이 없는 것으로 본다."
                ),
            )
            for path in promoted
        ),
    ]
    return replace(result, items=items, diagnostics=diagnostics)


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


class _EffectRow(BaseModel):
    evidence_ref: str = ""
    raw_text: str = ""
    # 같은 문구가 구역에 두 번 나오면 어디를 고른 것인지 좌표로만 말할 수 있다.
    # 좌표는 그 구역의 ``raw_text`` 기준이다.
    start: int | None = None
    end: int | None = None


class _RecheckResponse(BaseModel):
    purpose_occurrences: list[_RecheckRow] = Field(default_factory=list)
    delivery_decisions: list[_DeliveryDecision] = Field(default_factory=list)
    effect_occurrences: list[_EffectRow] = Field(default_factory=list)


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
    effect_facts: list[CplFact] = field(default_factory=list)
    effect_reason: str | None = None
    effect_dropped: list[str] = field(default_factory=list)


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


def _recovered_effect_facts(regions, rows):
    """기대효과로 확인된 추가 인용만 fact 로 만든다.

    셋을 구분해 돌려준다 — 승격한 fact, 기존 근거와 같아 더하지 않은 중복,
    검증에서 떨어진 행. 중복은 실패가 아니다. 모델이 "기존 근거로 충분하다" 고
    판단하면 정상적으로 그런 응답이 온다.
    """

    by_ref = {row.evidence_ref: row for row in regions}
    facts: list[CplFact] = []
    duplicates = 0
    dropped: list[str] = []
    taken: set[tuple[str, int, int]] = set()
    for row in rows:
        region = by_ref.get(row.evidence_ref)
        if region is None:
            dropped.append(f"{row.evidence_ref}: 기대효과 섹션에 준 참조가 아니다")
            continue
        quote = row.raw_text or ""
        if not quote:
            dropped.append(f"{row.evidence_ref}: 인용문이 비었다")
            continue
        if row.start is not None and row.end is not None:
            # 좌표를 줬으면 그 자리여야 한다. 틀렸다고 다른 자리를 찾아 살리면
            # 모델이 가리킨 곳과 서버가 저장한 곳이 달라진다.
            if region.raw_text[row.start : row.end] != quote:
                dropped.append(f"{row.evidence_ref}: 좌표가 인용문과 맞지 않는다")
                continue
            offset = row.start
        else:
            first = region.raw_text.find(quote)
            if first < 0:
                dropped.append(f"{row.evidence_ref}: 구역 원문의 부분문자열이 아니다")
                continue
            if region.raw_text.find(quote, first + 1) >= 0:
                dropped.append(f"{row.evidence_ref}: 같은 문구가 여럿이라 자리를 정할 수 없다")
                continue
            offset = first
        place = (row.evidence_ref, offset, offset + len(quote))
        if any(
            (row.evidence_ref, span.start, span.end) == place and span.raw_text == quote
            for span in region.already_selected
        ):
            duplicates += 1
            continue
        if place in taken:
            duplicates += 1
            continue
        taken.add(place)
        start = region.start_char + offset
        facts.append(
            CplFact(
                fact_id=None,
                evidence_ref=region.evidence_ref,
                value_raw=quote,
                status="identified",
                source_block_id=region.common_ir_block_id,
                start_char=start,
                end_char=start + len(quote),
                text_basis="cpl_effect_recheck",
                evidence=[
                    CplEvidence(
                        source_block_id=region.common_ir_block_id,
                        common_ir_document_id=region.common_ir_document_id,
                        common_ir_block_id=region.common_ir_block_id,
                        common_ir_occurrence_ids=[region.common_ir_occurrence_id],
                    )
                ],
            )
        )
    return facts, duplicates, dropped


def _recheck(fragments, candidates, effects, llm_client, *, model_profile):
    """문서당 한 번. 목적 구역과 수행관계 후보를 타입별 섹션으로 함께 묻는다.

    섹션을 나눠 호출하면 gap 이 둘 다 나온 문서에서 재검이 두 번 돌아 "문서당
    1 회" 가 깨진다. 대신 실패는 섹션끼리 옮지 않는다 — 응답 자체를 못 읽으면
    보낸 섹션이 모두 실패하고, 한 섹션의 행이 검증에서 떨어지면 그 섹션만
    실패한다. 보내지 않은 섹션은 판정도 경고도 만들지 않는다.
    """

    empty = _RecheckOutcome()
    if not fragments and not candidates and not effects:
        return empty
    try:
        prompt = load_recheck_prompt()
    except PromptUnavailableError:
        return _RecheckOutcome(
            purpose_reason=PROMPT_UNAVAILABLE if fragments else None,
            delivery_reason=PROMPT_UNAVAILABLE if candidates else None,
            effect_reason=PROMPT_UNAVAILABLE if effects else None,
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
                "effect_regions": [
                    {
                        "evidence_ref": row.evidence_ref,
                        "raw_text": row.raw_text,
                        "already_selected": [
                            {
                                "start": span.start,
                                "end": span.end,
                                "raw_text": span.raw_text,
                            }
                            for span in row.already_selected
                        ],
                    }
                    for row in effects
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
            effect_reason=reason if effects else None,
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

    effect_facts: list[CplFact] = []
    effect_reason = None
    effect_dropped: list[str] = []
    if effects:
        effect_facts, duplicates, effect_dropped = _recovered_effect_facts(
            effects, list(response.effect_occurrences)
        )
        # 추가가 없는 것과 검증이 다 떨어진 것은 다르다. 기존 근거만 다시
        # 돌려줬거나 아무것도 안 골랐으면 정상이다.
        if not effect_facts and effect_dropped and not duplicates:
            effect_reason = RECHECK_NO_VALID_OCCURRENCE

    return _RecheckOutcome(
        facts=facts,
        purpose_reason=purpose_reason,
        purpose_dropped=purpose_dropped,
        delivery=drafts,
        delivery_reason=delivery_reason,
        delivery_dropped=delivery_dropped,
        effect_facts=effect_facts,
        effect_reason=effect_reason,
        effect_dropped=effect_dropped,
    )


def _with_recheck(result, common_ir, llm_client, *, model_profile, candidate_pack=None):
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
    # 기대효과는 값이 없어서가 아니라 구역의 일부만 값이 돼서 묻는다. gap 과
    # 무관하게 미대응 occurrence 가 있을 때만 후보가 선다.
    effect_facts_now = [
        fact
        for item in result.items
        for subfield in item.subfields
        if subfield.profile_field == EFFECT_FIELD
        for fact in subfield.facts
    ]
    effects = build_effect_candidates(common_ir, effect_facts_now, candidate_pack)
    effect_regions = list(effects.regions) if effects.needs_recheck else []
    if not fragments and not candidates and not effect_regions:
        return result

    outcome = _recheck(
        fragments, candidates, effect_regions, llm_client, model_profile=model_profile
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
        if subfield.profile_field == EFFECT_FIELD and effect_regions:
            # 기존 Fact 는 보존하고 추가분만 더한다. 상태는 낮추지 않는다 —
            # 미대응 occurrence 가 있었다는 사실이 값이 틀렸다는 뜻은 아니다.
            codes = (
                [*subfield.reason_codes, outcome.effect_reason]
                if outcome.effect_reason
                else list(subfield.reason_codes)
            )
            return replace(
                subfield,
                reason_codes=codes,
                facts=[*subfield.facts, *outcome.effect_facts],
            )
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
    if effect_regions:
        detail += (
            f" / 기대효과 구역 {len(effect_regions)}개: 추가 "
            f"{len(outcome.effect_facts)}건"
        )
        if outcome.effect_dropped:
            # 개수만 남기면 왜 떨어졌는지 사후에 알 수 없다. 목적 쪽과 같이
            # 사유를 싣는다 — 추가가 있어 사유 코드가 안 붙는 경우에도 남는다.
            detail += (
                f", 탈락 {len(outcome.effect_dropped)}건 "
                f"({'; '.join(outcome.effect_dropped[:3])})"
            )
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
    candidate_pack: Any | None = None,
    quantity_hold_reason: str | None = None,
) -> CplResult:
    """CPL 13항목에 의미 축까지 확정한다. 예외를 던지지 않는다.

    ``build_cpl_result`` 는 그대로 결정적이다. 축만 이 위에서 붙인다. 축이
    비어도 13항목 값·근거·상태는 바뀌지 않으므로, LLM 이 죽어도 Rule 결과가
    통째로 사라지지 않는다 (초안 §9.4).

    축을 판정 시점이 아니라 확정 시점에 붙이는 이유는, 같은 원문을 소비하는
    두 단계가 각자 다시 해석하면 화면과 판정이 갈라지기 때문이다.
    """

    result = build_cpl_result(profile)
    result = _with_quantities(result, candidate_pack, quantity_hold_reason)
    if common_ir is not None:
        # 순서가 계약이다. 칸이 없는 필드를 먼저 해당 없음으로 확정해야
        # 누락 감지가 그 자리에 추출 실패 사유를 달지 않는다.
        result = _with_form_absence(result, common_ir)
        result = _with_coverage_gaps(result, profile, common_ir)
    facts = _purpose_facts(result)
    if not facts and common_ir is not None:
        # 1 차 추출이 값을 못 냈고 구역은 있다. 축만 붙일 대상이 없으므로 값과
        # 축을 함께 되찾는 재검으로 간다. 재검은 문서당 한 번이고, 그 한 번이
        # 목적 구역과 수행관계 후보를 함께 나른다.
        return _with_recheck(
        result, common_ir, llm_client,
        model_profile=model_profile, candidate_pack=candidate_pack,
    )
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
    return _with_recheck(
        result, common_ir, llm_client,
        model_profile=model_profile, candidate_pack=candidate_pack,
    )
