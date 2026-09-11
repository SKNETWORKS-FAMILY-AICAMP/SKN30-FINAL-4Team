"""구조화가 라벨 구역을 비우고 지나갔는지 본다.

실문서에서 관측한 형태: 원문에 ``○ (사업목적) …`` 구역이 있고 다른 필드 27 개가
정상인데 ``purpose_goal`` 만 ``not_found`` 로 나왔다. 문서에 없어서 빈 것과
구조화가 놓쳐서 빈 것은 사용자가 해야 할 다음 행동이 다른데, 지금은 둘 다
``not_found`` 한 값으로 보인다.

여기서 하는 일은 **후보 표시까지**다. 라벨이 있는데 값이 비었다는 사실은
결정적으로 셀 수 있지만, 그것이 기술적 추출 실패인지 값을 특정할 수 없는
서술인지(``추후 결정`` 같은) 명시적 부재인지는 이 정보만으로 갈리지 않는다.
재검이나 상태 확정은 이 신호를 받은 쪽이 정한다.

라벨 문법은 닫혀 있다. 서식이 정해 둔 표기라 표현이 열려 있지 않고, 실문서
10 건에서 세 가지 표기가 관측됐다.

    ○ 사업목적 :        ○ (사업목적)        ○ 사업 목적 :

의미 키워드 목록을 늘리는 것이 아니다. 새 필드를 다루려면 그 필드의 라벨
문법을 확인하고 여기에 더한다.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import re
from typing import Any

from .analysis_inputs import field_states_by_name, read_path


# 글머리 기호·괄호·콜론은 서식마다 다르고 라벨 안 공백도 문서마다 다르다.
# 라벨 단어 자체는 바꾸지 않는다.
# 줄머리로 한정하지 않는다. 셀 안 항목을 줄바꿈 없이 이어 쓴 서식이 있어서
# (``○ 사업기간 … ○ 사업목적 : …``) 줄머리만 보면 그 문서를 통째로 놓친다.
# 대신 글머리 기호나 줄 경계를 앞에 요구해 본문 속 같은 낱말과 섞이지 않게 한다.
_LABEL_GRAMMAR = r"(?:^|\n|[○□■●▪·])\s*\(?\s*{label}\s*\)?\s*[:：)]?"
# 라벨 뒤에 실제 내용이 있어야 한다. 라벨만 찍히고 비어 있는 구역은 문서가
# 정말 비운 것이지 구조화가 놓친 것이 아니다.
_NEXT_LABEL = re.compile(r"[\n○□■●▪]")
_MIN_REGION_CHARS = 2

# 필드 하나에 라벨 하나. 별칭이 필요하면 그 필드의 표기를 실제로 확인한 뒤 더한다.
_FIELD_LABELS: dict[str, str] = {
    "comparison_profile.purpose_goal": r"사업\s*목적",
}

_PATTERNS = {
    path: re.compile(_LABEL_GRAMMAR.format(label=label))
    for path, label in _FIELD_LABELS.items()
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


def _region_end(text: str, label_end: int) -> int:
    """라벨이 지배하는 구역의 끝. 다음 라벨이나 줄 경계에서 끊는다.

    구역을 블록 전체로 잡으면 400 자 한 문단에 여러 항목이 이어진 서식에서
    사업목적 구역이 예산·지원대상까지 삼킨다. 재검 입력이 그러면 다시 blob 을
    주는 셈이라 나눈 의미가 없다.
    """

    boundary = _NEXT_LABEL.search(text, label_end)
    return boundary.start() if boundary else len(text)


def _region_has_content(text: str, label_end: int) -> bool:
    """라벨 바로 뒤 구역에 실제 글이 있는지 본다.

    ``○ 사업목적 :`` 만 찍혀 있고 곧바로 다음 항목이 오면 문서가 그 구역을
    비운 것이다. 그것을 추출 누락으로 표시하면 문서의 빈칸을 우리 결함으로
    되돌린다.
    """

    region = text[label_end : _region_end(text, label_end)]
    return len(region.strip()) >= _MIN_REGION_CHARS


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

    pattern = _PATTERNS.get(profile_field)
    if pattern is None:
        return []
    document_id = (common_ir.get("document") or {}).get("document_id")
    fragments: list[CplFragment] = []
    # 표는 같은 본문을 계층마다 다시 싣는다(t4 / t4:c5 / t4:c5:p0). 구역 하나를
    # 계층 수만큼 재검에 넣으면 같은 원문을 여러 번 묻는다. 가장 좁은 occurrence
    # 하나만 남긴다 — 좌표가 가장 정확하고 그 자리를 유일하게 가리킨다.
    seen: set[str] = set()
    by_text: dict[tuple[str, str], CplFragment] = {}
    for block_id, occurrence_id, text in _occurrences(common_ir):
        for match in pattern.finditer(text):
            start = match.start()
            end = _region_end(text, match.end())
            if len(text[match.end() : end].strip()) < _MIN_REGION_CHARS:
                continue
            ref = evidence_ref(document_id, occurrence_id, start, end)
            if ref in seen:
                continue
            seen.add(ref)
            fragment = CplFragment(
                evidence_ref=ref,
                profile_field=profile_field,
                raw_text=text[start:end],
                source_role=None,
                common_ir_document_id=document_id,
                common_ir_block_id=block_id,
                common_ir_occurrence_id=occurrence_id,
                start_char=start,
                end_char=end,
            )
            key = (block_id, fragment.raw_text)
            kept = by_text.get(key)
            if kept is None or len(occurrence_id) > len(kept.common_ir_occurrence_id):
                by_text[key] = fragment
    fragments = list(by_text.values())
    return fragments


def detect_coverage_gaps(
    profile: Mapping[str, Any], common_ir: Mapping[str, Any]
) -> list[CoverageGap]:
    """라벨 구역이 있는데 값이 비어 있는 필드를 모은다. 상태를 바꾸지 않는다."""

    states = field_states_by_name(profile)
    texts = _occurrences(common_ir)
    gaps: list[CoverageGap] = []
    for path, pattern in _PATTERNS.items():
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
            if any(
                _region_has_content(text, match.end())
                for match in pattern.finditer(text)
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
