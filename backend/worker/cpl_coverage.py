"""구조화가 라벨 구역을 비우고 지나갔는지 본다.

실문서에서 관측한 형태: 원문에 ``○ (사업목적) …`` 구역이 있고 다른 필드 27 개가
정상인데 ``purpose_goal`` 만 ``not_found`` 로 나왔다. 문서에 없어서 빈 것과
구조화가 놓쳐서 빈 것은 사용자가 해야 할 다음 행동이 다른데, 지금은 둘 다
``not_found`` 한 값으로 보인다.

여기서 하는 일은 **후보 표시까지**다. 라벨이 있는데 값이 비었다는 사실은
결정적으로 셀 수 있지만, 그것이 기술적 추출 실패인지 값을 특정할 수 없는
서술인지(``추후 결정`` 같은) 명시적 부재인지는 이 정보만으로 갈리지 않는다.
재검이나 상태 확정은 이 신호를 받은 쪽이 정한다.

라벨 문법은 여기 있지 않다. ``semantic_structuring.field_regions`` 의 공개
``find_text_regions()`` 하나를 쓴다. 값 span 후보를 만드는 쪽과 이 감지기가 서로
다른 구역 정의를 갖고 있으면, 한쪽이 찾는 구역을 다른 쪽이 못 찾는 상태가
조용히 생긴다.

좌표는 그대로 Common IR occurrence 기준이다. 저쪽은 CandidatePack 블록 좌표를
쓰지만 규칙만 공유하고 좌표 원천은 각자 유지한다 — ``evidence_ref`` 가 재검
요청·응답 대조와 FIT fallback 의 식별값이라 기준이 바뀌면 과거 결과와 이어지지
않는다.
"""

from __future__ import annotations

from collections.abc import Mapping
import re
from dataclasses import dataclass
from typing import Any

from semantic_structuring.field_regions import (
    find_text_regions,
    starts_with_form_label,
)

from .analysis_inputs import field_states_by_name, read_path


# 감시 대상. 프로파일 경로 -> 공용 계산기의 필드 이름.
# 지금은 사업목적 하나다. 공용 ``FIELD_LABELS`` 에 다른 필드가 있다고 해서 여기를
# 자동으로 늘리지 않는다. 재검기가 사업목적 의미 축을 대상으로 만들어져 있어서,
# 다른 필드를 붙이면 재검 범위와 LLM 입력 계약이 함께 달라진다.
# 구역을 찾을 대상. 프로파일 경로 -> 공용 계산기의 필드 이름.
# 라벨 문법은 여기 없다. ``semantic_structuring.field_regions`` 한 곳에만 둔다.
_FRAGMENT_FIELDS: dict[str, str] = {
    "comparison_profile.purpose_goal": "purpose_goal",
    "comparison_profile.delivery_relations": "delivery_relations",
    "request_context.expected_effect": "expected_effect",
}

# 누락 상태를 판정할 대상. 구역을 찾는 것과 역할이 다르다 — 기대효과는 이미
# 값이 있는 필드의 보완이라, 구역의 일부가 값이 안 됐다는 사실만으로
# ``EXTRACTION_COVERAGE_GAP`` 을 붙이거나 상태를 낮추지 않는다.
_GAP_FIELDS: dict[str, str] = {
    "comparison_profile.purpose_goal": "purpose_goal",
    "comparison_profile.delivery_relations": "delivery_relations",
}


# 수행체계 구역의 표 제목. 실제로 확인된 표본은 ``< 사업추진 체계도 >`` 하나다.
# 제목은 구역 안에 있어도 내용이 아니므로 세지 않지만, 거기서 구역이 끝나지도
# 않는다 — 실제 내용은 그 아래에 온다.
#
# 문법을 확인된 범위로 좁게 잡는다. 모르는 괄호 표현은 버리지 않고 남긴다.
# ``[주무부처]`` · ``<수행기관>`` 은 수행기관 이름일 수 있고, coverage 가 값을
# 놓치지 않는 편이 제목 한 줄을 잘못 세는 것보다 낫다. 대괄호 표기는 캡션 표본이
# 확인되기 전까지 제외하지 않는다. ``[ 	]`` 로 한 줄에 묶어 줄바꿈이 들어간
# 문자열은 제목으로 인정하지 않는다.
_DELIVERY_CAPTION = re.compile(
    r"^[ 	]*[<〈][ 	]*(?:사업[ 	]*추진[ 	]*)?"
    r"(?:체계[ 	]*도|절차[ 	]*도|절차[ 	]*표|체계[ 	]*표|흐름도)"
    r"[ 	]*[>〉][ 	]*$"
)


def _is_delivery_caption(text: str, field_name: str) -> bool:
    """수행체계 구역의 표 제목인가. 다른 필드에는 적용하지 않는다."""

    return field_name == "delivery_relations" and bool(
        _DELIVERY_CAPTION.match(text)
    )


@dataclass(frozen=True, slots=True)
class CoverageGap:
    """라벨 구역은 있는데 값이 비어 있는 필드 하나."""

    profile_field: str
    field_name: str
    status: str | None
    label_block_ids: tuple[str, ...]


def _occurrences(common_ir: Mapping[str, Any]) -> list[tuple[str, str, str]]:
    """블록 id, occurrence id, 본문. 표 셀 occurrence 도 각각 본다."""

    rows: list[tuple[str, str, str]] = []
    for block in common_ir.get("blocks") or []:
        if not isinstance(block, Mapping):
            continue
        block_id = str(block.get("block_id") or "")
        for occurrence in block.get("occurrences") or []:
            if isinstance(occurrence, Mapping) and occurrence.get("text"):
                rows.append((
                    block_id,
                    str(occurrence.get("occurrence_id") or ""),
                    str(occurrence["text"]),
                ))
    return rows


def evidence_ref(
    document_id: str | None, occurrence_id: str, start: int, end: int
) -> str:
    """원문 구역 하나를 가리키는 결정적 참조.

    같은 문서를 다시 처리하면 같은 ref 가 나와야 재검 요청과 응답을 대조할 수
    있다. 임의 UUID 를 쓰면 실행마다 달라져 감사 기록이 서로 연결되지 않는다.

    프로필의 ``fact_id`` 와는 형식이 겹치지 않는다. 저쪽은 프로필 내부 좌표라
    다른 프로필에서 같은 값이 다시 나오지만, 이쪽은 문서 좌표라 전역이다.
    """

    return f"{document_id or 'unknown'}#{occurrence_id}@{start}-{end}"


@dataclass(frozen=True, slots=True)
class CplFragment:
    """재검 입력으로 쓰는 원문 구역 하나.

    ``CplFact`` 를 대신하지 않는다. 저쪽은 검증을 통과해 CPL 결과에 실린 값이고
    이쪽은 아직 값이 되지 못한 원문이다. 둘을 한 타입으로 합치면 화면에 나가는
    것과 재검에 넣는 것이 같은 자리에서 섞인다.

    ``source_role`` 은 이 슬라이스에서 ``None`` 이다. 사업목적 FIT 관계는 role 을
    요구하지 않으므로 전역 role 체계를 함께 만들지 않는다.
    """

    evidence_ref: str
    profile_field: str
    raw_text: str
    source_role: str | None
    common_ir_document_id: str | None
    common_ir_block_id: str
    common_ir_occurrence_id: str
    start_char: int
    end_char: int
    # 라벨이 앞 블록에 있고 내용이 뒤따라온 경우의 라벨 좌표. ``evidence_ref`` 는
    # 언제나 내용 occurrence 를 가리킨다 — 저것은 "어느 원문 span 인가" 를 말하고
    # 필드 귀속은 ``profile_field`` 가 따로 담는다. 라벨 좌표를 ref 문자열에
    # 합치면 같은 span 이 라벨에 따라 다른 id 를 갖게 되어 대조가 깨진다.
    label_block_id: str | None = None
    label_occurrence_id: str | None = None


def build_fragments(
    common_ir: Mapping[str, Any], *, profile_field: str
) -> list[CplFragment]:
    """그 필드의 라벨이 지배하는 원문 구역을 모은다. 값을 만들지 않는다."""

    field_name = _FRAGMENT_FIELDS.get(profile_field)
    if field_name is None:
        return []
    document_id = (common_ir.get("document") or {}).get("document_id")
    rows = _occurrences(common_ir)
    found: list[CplFragment] = []
    for index, (block_id, occurrence_id, text) in enumerate(rows):
        # 기본 호출이 곧 "내용이 있는 구간" 이다. 빈 구간의 판단 기준을 여기서
        # 다시 만들지 않는다.
        filled = find_text_regions(text, field_name=field_name)
        for span in filled:
            start, end = span.label_start, span.content_end
            found.append(
                _fragment(
                    document_id, profile_field, block_id, occurrence_id,
                    text, start, end,
                )
            )
        if filled:
            continue
        # 라벨은 있는데 텍스트가 끝나서 비었으면 지배가 다음 occurrence 로
        # 넘어간다. 다음 라벨 때문에 비었다면 넘기지 않는다.
        spans = find_text_regions(text, field_name=field_name, include_empty=True)
        if not any(span.continues_to_sibling for span in spans):
            continue
        collected = _sibling_content(rows, index, field_name)
        for next_block_id, next_occurrence_id, next_text in collected:
            found.append(
                _fragment(
                    document_id, profile_field, next_block_id, next_occurrence_id,
                    next_text, 0, len(next_text),
                    label_block_id=block_id, label_occurrence_id=occurrence_id,
                )
            )
    return _drop_nested_duplicates(found)


def _fragment(
    document_id: str | None,
    profile_field: str,
    block_id: str,
    occurrence_id: str,
    text: str,
    start: int,
    end: int,
    *,
    label_block_id: str | None = None,
    label_occurrence_id: str | None = None,
) -> CplFragment:
    return CplFragment(
        evidence_ref=evidence_ref(document_id, occurrence_id, start, end),
        profile_field=profile_field,
        raw_text=text[start:end],
        source_role=None,
        common_ir_document_id=document_id,
        common_ir_block_id=block_id,
        common_ir_occurrence_id=occurrence_id,
        start_char=start,
        end_char=end,
        label_block_id=label_block_id,
        label_occurrence_id=label_occurrence_id,
    )


def _sibling_content(
    rows: list[tuple[str, str, str]], index: int, field_name: str
) -> list[tuple[str, str, str]]:
    """라벨 occurrence 다음에서 그 라벨이 지배하는 내용 occurrence 들을 모은다.

    첫 블록 하나만 가져오면 안 된다. 수행체계는 표 제목 블록
    (``< 사업추진 체계도 >``)이 먼저 오고 실제 내용은 그 아래 중첩 표에 있어서,
    하나만 가져오면 제목에서 끝난다. 제목 블록은 건너뛰되 거기서 멈추지 않는다.

    다음 항목 라벨을 만나면 지배가 끝난다 — 남의 값을 가져오지 않는다.
    """

    collected: list[tuple[str, str, str]] = []
    for block_id, occurrence_id, text in rows[index + 1 :]:
        if not text.strip():
            continue
        if starts_with_form_label(text):
            break
        if _is_delivery_caption(text, field_name):
            continue
        collected.append((block_id, occurrence_id, text))
    return collected




def is_nested_occurrence(outer: str, inner: str) -> bool:
    """``inner`` 가 ``outer`` 안에 있는 표 계층인가.

    표는 한 본문을 부모·셀·문단 occurrence 로 다시 싣는다
    (``occ:rhwp:t4`` / ``occ:rhwp:t4:c5`` / ``occ:rhwp:t4:c5:p0``). 같은 자리를
    계층 수만큼 싣는 것을 접으려면 계층을 알아야 한다.

    구분자 경계까지 본다. 맨 글자 접두사로 보면 ``…:p1`` 이 ``…:p10`` 의
    부모가 되고 ``…:c5`` 가 ``…:c50`` 의 부모가 된다. 둘 다 형제다.
    """

    return outer != inner and inner.startswith(outer + ":")


def _drop_nested_duplicates(fragments: list[CplFragment]) -> list[CplFragment]:
    """표 계층이 같은 자리를 다시 실은 것만 접는다.

    표는 한 본문을 부모·셀·문단 occurrence 로 다시 싣는다
    (``occ:rhwp:t4`` / ``occ:rhwp:t4:c5`` / ``occ:rhwp:t4:c5:p0``). 같은 자리를
    계층 수만큼 재검에 넣으면 같은 원문을 여러 번 묻는다. 가장 좁은 occurrence
    하나만 남긴다 — 좌표가 가장 정확하고 그 자리를 유일하게 가리킨다.

    문구가 같다는 이유만으로 접지 않는다. 한 문서에 같은 문구가 서로 다른
    자리에 두 번 나올 수 있고 그 둘은 서로 다른 근거다. occurrence id 가
    **서로 다르면서** 한쪽이 다른 쪽의 접두사일 때만 접는다.
    """

    kept: list[CplFragment] = []
    for fragment in sorted(fragments, key=lambda f: -len(f.common_ir_occurrence_id)):
        nested = any(
            other.common_ir_block_id == fragment.common_ir_block_id
            and other.raw_text == fragment.raw_text
            and is_nested_occurrence(
                fragment.common_ir_occurrence_id, other.common_ir_occurrence_id
            )
            for other in kept
        )
        if not nested:
            kept.append(fragment)
    return kept


def detect_coverage_gaps(
    profile: Mapping[str, Any], common_ir: Mapping[str, Any]
) -> list[CoverageGap]:
    """라벨 구역이 있는데 값이 비어 있는 필드를 모은다. 상태를 바꾸지 않는다."""

    states = field_states_by_name(profile)
    gaps: list[CoverageGap] = []
    for path, field_name in _GAP_FIELDS.items():
        name = path.rsplit(".", 1)[-1]
        rows = read_path(profile, path)
        if rows:
            continue
        state = (states.get(name) or {}).get("status")
        # 명시적 부재는 후보가 아니다. 문서가 "해당 없음" 이라고 말한 것을
        # 추출 누락으로 되돌리지 않는다.
        if state == "not_applicable":
            continue
        # 판정 기준을 ``build_fragments`` 와 합친다. 따로 세면 라벨과 내용이
        # 다른 블록에 있는 서식에서 fragment 는 만들어지는데 gap 이 안 잡혀,
        # 재검이 target 을 못 찾고 그 fragment 가 쓰이지 않는다.
        found = tuple(
            dict.fromkeys(
                fragment.label_block_id or fragment.common_ir_block_id
                for fragment in build_fragments(common_ir, profile_field=path)
            )
        )
        if found:
            gaps.append(
                CoverageGap(
                    profile_field=path,
                    field_name=name,
                    status=state,
                    label_block_ids=found,
                )
            )
    return gaps


__all__ = [
    "CoverageGap",
    "CplFragment",
    "build_fragments",
    "detect_coverage_gaps",
    "evidence_ref",
]
