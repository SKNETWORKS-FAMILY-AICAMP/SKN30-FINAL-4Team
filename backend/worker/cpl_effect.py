"""기대효과 구역에서 아직 값이 되지 않은 원문을 재검 후보로 만든다.

목적·수행체계 재검은 "값이 하나도 없을 때" 돌았다. 기대효과는 다르다 — 값은
이미 있고 구역의 일부만 값이 됐다. 새 HWP 는 기대효과를 두 곳에 싣는데, 구조화가
요약표 한 줄만 고르고 본문 세 문단을 지나간 실행이 7 회 중 6 회였다.

**미선택은 재검 후보 신호이지 확인된 누락이 아니다.** 본문에 독립적인 기대효과가
있는지, 기존 근거가 이미 그 의미를 담았는지는 의미 판단이라 모델이 정한다. 여기서는
어디가 대응됐고 어디가 안 됐는지만 좌표로 센다.

좌표계가 둘이다. 기존 Fact 는 CandidatePack 블록 기준이고 구역은 Common IR
occurrence 기준이다. 실측에서 팩 블록 2,540 개 중 344 개가 occurrence 와 텍스트가
달랐다. 그래서 이전은 두 단계로 하고, 확정할 수 없으면 확정하지 않는다.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .cpl_coverage import CplFragment, build_fragments, is_nested_occurrence


EFFECT_FIELD = "request_context.expected_effect"


@dataclass(frozen=True, slots=True)
class SelectedSpan:
    """구역 안에서 이미 값이 된 구간. 좌표는 그 구역의 ``raw_text`` 기준이다."""

    start: int
    end: int
    raw_text: str


@dataclass(frozen=True, slots=True)
class EffectRegion:
    """재검에 실어 보낼 기대효과 구역 하나."""

    evidence_ref: str
    raw_text: str
    common_ir_document_id: str | None
    common_ir_block_id: str
    common_ir_occurrence_id: str
    start_char: int
    already_selected: tuple[SelectedSpan, ...] = ()

    @property
    def covered(self) -> bool:
        return bool(self.already_selected)


@dataclass(frozen=True, slots=True)
class EffectCandidates:
    regions: tuple[EffectRegion, ...] = ()
    # 좌표를 확정하지 못한 기존 선택의 문구. 어느 자리인지 말할 수 없으므로
    # ``already_selected`` 에는 넣지 않지만, 모델이 기존 내용 자체를 모르는
    # 상태는 피한다.
    unplaced_selections: tuple[str, ...] = ()

    @property
    def needs_recheck(self) -> bool:
        """대응되지 않은 구역이 하나라도 있으면 물어볼 값어치가 있다.

        요약표가 부분 인용됐다는 사실만으로는 부르지 않는다. 이번 보완의 단위는
        occurrence 다.
        """

        return any(not row.covered for row in self.regions)


@dataclass(frozen=True, slots=True)
class _Placed:
    occurrence_id: str
    start: int
    end: int
    raw_text: str


def _occurrence_text(common_ir: Mapping[str, Any]) -> dict[str, str]:
    return {
        occurrence["occurrence_id"]: occurrence.get("text") or ""
        for block in common_ir.get("blocks") or []
        for occurrence in block.get("occurrences") or []
    }


def _unique_index(haystack: str, needle: str) -> int | None:
    if not needle:
        return None
    first = haystack.find(needle)
    if first < 0 or haystack.find(needle, first + 1) >= 0:
        return None
    return first


def _to_occurrence(fact, blocks: Mapping[str, Any], texts: Mapping[str, str]) -> _Placed | None:
    """팩 블록 좌표의 기존 선택을 occurrence 좌표로 옮긴다. 확정될 때만.

    블록 본문과 occurrence 본문이 같으면 좌표가 그대로 간다. 다르면 고른 문구를
    occurrence 안에서 찾되 유일할 때만 인정한다.
    """

    block = blocks.get(fact.source_block_id or "")
    value = fact.value_raw or ""
    if block is None or not value:
        return None
    ids = list(getattr(block, "common_ir_occurrence_ids", None) or [])
    if len(ids) != 1:
        return None
    occurrence_id = ids[0]
    text = texts.get(occurrence_id)
    if text is None:
        return None
    if block.text == text and fact.start_char is not None and fact.end_char is not None:
        if text[fact.start_char : fact.end_char] == value:
            return _Placed(occurrence_id, fact.start_char, fact.end_char, value)
    index = _unique_index(text, value)
    if index is None:
        return None
    return _Placed(occurrence_id, index, index + len(value), value)


def _placements(
    placed: _Placed, fragments: list[CplFragment]
) -> list[tuple[CplFragment, SelectedSpan]]:
    """그 선택이 어느 구역의 어디인지. 전체 후보에서 유일할 때만 확정한다.

    부모 셀의 문구가 자식 문단 여럿에 각각 한 번씩 나올 수 있다. 문단마다
    유일하다는 이유로 둘 다 선택됨으로 처리하면 실제로는 하나만 값이 된 것을
    둘 다 대응된 것으로 본다.
    """

    same = [
        row
        for row in fragments
        if row.common_ir_occurrence_id == placed.occurrence_id
    ]
    if same:
        out: list[tuple[CplFragment, SelectedSpan]] = []
        for row in same:
            start = placed.start - row.start_char
            end = placed.end - row.start_char
            if 0 <= start < end <= len(row.raw_text):
                out.append((row, SelectedSpan(start, end, placed.raw_text)))
        return out

    hits: list[tuple[CplFragment, SelectedSpan]] = []
    for row in fragments:
        if not (
            is_nested_occurrence(placed.occurrence_id, row.common_ir_occurrence_id)
            or is_nested_occurrence(row.common_ir_occurrence_id, placed.occurrence_id)
        ):
            continue
        index = row.raw_text.find(placed.raw_text)
        while index >= 0:
            hits.append(
                (row, SelectedSpan(index, index + len(placed.raw_text), placed.raw_text))
            )
            index = row.raw_text.find(placed.raw_text, index + 1)
    return hits


def build_effect_candidates(
    common_ir: Mapping[str, Any] | None,
    facts: list,
    candidate_pack: Any | None,
) -> EffectCandidates:
    """기대효과 구역과, 각 구역에서 이미 값이 된 구간을 모은다."""

    if common_ir is None:
        return EffectCandidates()
    fragments = build_fragments(common_ir, profile_field=EFFECT_FIELD)
    if not fragments:
        return EffectCandidates()

    texts = _occurrence_text(common_ir)
    blocks = {row.block_id: row for row in (candidate_pack.blocks if candidate_pack else [])}
    selected: dict[str, list[SelectedSpan]] = {}
    unplaced: list[str] = []
    for fact in facts:
        placed = _to_occurrence(fact, blocks, texts)
        if placed is None:
            if fact.value_raw:
                unplaced.append(fact.value_raw)
            continue
        hits = _placements(placed, fragments)
        if len(hits) != 1:
            unplaced.append(placed.raw_text)
            continue
        fragment, span = hits[0]
        selected.setdefault(fragment.evidence_ref, []).append(span)

    return EffectCandidates(
        regions=tuple(
            EffectRegion(
                evidence_ref=row.evidence_ref,
                raw_text=row.raw_text,
                common_ir_document_id=row.common_ir_document_id,
                common_ir_block_id=row.common_ir_block_id,
                common_ir_occurrence_id=row.common_ir_occurrence_id,
                start_char=row.start_char,
                already_selected=tuple(
                    sorted(selected.get(row.evidence_ref, []), key=lambda s: s.start)
                ),
            )
            for row in fragments
        ),
        unplaced_selections=tuple(dict.fromkeys(unplaced)),
    )
