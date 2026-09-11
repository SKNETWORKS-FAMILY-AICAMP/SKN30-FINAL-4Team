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
from dataclasses import dataclass
from typing import Any

from semantic_structuring.field_regions import find_text_regions

from .analysis_inputs import field_states_by_name, read_path


# 감시 대상. 프로파일 경로 -> 공용 계산기의 필드 이름.
# 지금은 사업목적 하나다. 공용 ``FIELD_LABELS`` 에 다른 필드가 있다고 해서 여기를
# 자동으로 늘리지 않는다. 재검기가 사업목적 의미 축을 대상으로 만들어져 있어서,
# 다른 필드를 붙이면 재검 범위와 LLM 입력 계약이 함께 달라진다.
_WATCHED_FIELDS: dict[str, str] = {
    "comparison_profile.purpose_goal": "purpose_goal",
}


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


def build_fragments(
    common_ir: Mapping[str, Any], *, profile_field: str
) -> list[CplFragment]:
    """그 필드의 라벨이 지배하는 원문 구역을 모은다. 값을 만들지 않는다."""

    field_name = _WATCHED_FIELDS.get(profile_field)
    if field_name is None:
        return []
    document_id = (common_ir.get("document") or {}).get("document_id")
    found: list[CplFragment] = []
    for block_id, occurrence_id, text in _occurrences(common_ir):
        for span in find_text_regions(text, field_name=field_name):
            start, end = span.label_start, span.content_end
            found.append(
                CplFragment(
                    evidence_ref=evidence_ref(document_id, occurrence_id, start, end),
                    profile_field=profile_field,
                    raw_text=text[start:end],
                    source_role=None,
                    common_ir_document_id=document_id,
                    common_ir_block_id=block_id,
                    common_ir_occurrence_id=occurrence_id,
                    start_char=start,
                    end_char=end,
                )
            )
    return _drop_nested_duplicates(found)


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
            and other.common_ir_occurrence_id != fragment.common_ir_occurrence_id
            and other.common_ir_occurrence_id.startswith(
                fragment.common_ir_occurrence_id
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
    texts = _occurrences(common_ir)
    gaps: list[CoverageGap] = []
    for path, field_name in _WATCHED_FIELDS.items():
        name = path.rsplit(".", 1)[-1]
        rows = read_path(profile, path)
        if rows:
            continue
        state = (states.get(name) or {}).get("status")
        # 명시적 부재는 후보가 아니다. 문서가 "해당 없음" 이라고 말한 것을
        # 추출 누락으로 되돌리지 않는다.
        if state == "not_applicable":
            continue
        found = tuple(
            block_id
            for block_id, _occurrence_id, text in texts
            if find_text_regions(text, field_name=field_name)
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
