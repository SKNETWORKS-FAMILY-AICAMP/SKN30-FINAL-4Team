"""Slice 2: Request Profile v0.1.2 → CPL 13항목 결과 (초안 §6, §6.1).

추출은 여기서 하지 않는다. 팀 프로파일이 이미 값·근거·상태를 확정해 두었고,
이 모듈은 그것을 13항목 자리에 놓고 무엇이 비었는지 그대로 보고할 뿐이다.
그래서 ``app.services`` 아래의 옛 CPL 추출기를 import 하지 않는다 —
초안 §13 단계 2 통과 조건의 "추출 이중 실행 없음" 이 그 이유다.
(테스트가 worker 소스에 그 모듈 경로 문자열이 없는지도 함께 고정한다.)

점수·확인율은 계산하지 않는다 (초안 §6.1).
"""

from __future__ import annotations

from typing import Any

from .analysis_inputs import (
    CPL_FIELD_SOURCES,
    facts_at,
    field_name_of,
    field_states_by_name,
    read_path,
    unmapped_profile_fields,
)
from .contracts.cpl_result import (
    NEEDS_CONFIRMATION,
    NO_PROFILE_FIELD,
    PROFILE_FIELD_STATE_MISSING,
    SERVER_RESOLVED_CHECKBOX,
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
