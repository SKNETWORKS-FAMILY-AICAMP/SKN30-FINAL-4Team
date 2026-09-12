"""구역에서 어디가 선택되지 않았는지를 좌표로만 센다.

실제 라이브 산출물 4 건을 그대로 읽는다 (``fixtures/coverage``). 인라인 dict 로
줄이면 이번 문제 — 구조화가 구역 일부만 고르는 것 — 를 다시 숨긴다.

기대값은 다른 실행 결과가 아니라 원문 좌표에서 왔다. 실행 id 와 절대 경로는
비교하지 않는다.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from worker.coverage_map import RegionSpan, Selection, build_coverage_map

_FIXTURES = Path(__file__).parent / "fixtures" / "coverage"

# 원문에서 직접 읽은 좌표 (fixtures/coverage/README.md).
_PURPOSE_OCC = "occ:rhwp:t4:c5:p0"
_PURPOSE_CONTENT = (9, 82)


def _load(name: str, file: str) -> dict[str, Any]:
    return json.loads((_FIXTURES / name / file).read_text(encoding="utf-8"))


def _texts(name: str) -> dict[str, str]:
    ir = _load(name, "01_common_ir.json")
    ir = ir.get("common_ir") or ir
    return {
        occurrence["occurrence_id"]: occurrence.get("text") or ""
        for block in ir["blocks"]
        for occurrence in block.get("occurrences", [])
    }


def _selections(name: str, field: str) -> list[Selection]:
    profile = _load(name, "02_structured_profile.json")
    profile = profile.get("profile") or profile
    rows = (
        (profile.get("comparison_profile") or {}).get(field)
        or (profile.get("request_context") or {}).get(field)
        or []
    )
    out: list[Selection] = []
    for row in rows:
        source = row.get("value_source") or {}
        for evidence in row.get("evidence") or []:
            for occurrence_id in evidence.get("common_ir_occurrence_ids") or []:
                out.append(
                    Selection(
                        occurrence_id=occurrence_id,
                        start=source["start_char"],
                        end=source["end_char"],
                        text=row.get("value_raw") or "",
                    )
                )
    return out


def _purpose_region(name: str) -> RegionSpan:
    return RegionSpan(
        occurrence_id=_PURPOSE_OCC,
        text=_texts(name)[_PURPOSE_OCC],
        content_start=_PURPOSE_CONTENT[0],
        content_end=_PURPOSE_CONTENT[1],
    )


# --- A. occurrence 하나 안에서의 부분 선택 --------------------------------


def test_the_partial_run_leaves_the_leading_clause_uncovered() -> None:
    region = _purpose_region("mockup08-partial")
    result = build_coverage_map(
        [region], _selections("mockup08-partial", "purpose_goal")
    )

    (purpose,) = result.regions
    assert purpose.covered == ((61, 82),)
    # 대상과 방향이 모두 이 구간에 있다. 비율이 아니라 구간이 답이다.
    assert purpose.uncovered == ((9, 61),)
    assert region.text[9:61] == (
        "부산 관내 제조·정보통신 중소기업의 기술경쟁력을 강화하고 시제품 사업화까지의 단계를 지원하여 "
    )


def test_the_full_run_leaves_nothing_uncovered() -> None:
    result = build_coverage_map(
        [_purpose_region("mockup08-full")], _selections("mockup08-full", "purpose_goal")
    )

    (purpose,) = result.regions
    assert purpose.covered == ((9, 82),)
    assert purpose.uncovered == ()


# --- B. 독립 occurrence 통째 누락 ------------------------------------------

# 본문 구역은 ``□ 기대효과`` 아래 세 문장이다. 요약표는 다른 자리의 근거라
# 구역 밖이다.
_EFFECT_BODY = ("occ:rhwp:p75", "occ:rhwp:p77", "occ:rhwp:p79")


def _effect_regions(name: str) -> list[RegionSpan]:
    texts = _texts(name)
    return [
        RegionSpan(
            occurrence_id=occurrence_id,
            text=texts[occurrence_id],
            content_start=0,
            content_end=len(texts[occurrence_id]),
        )
        for occurrence_id in _EFFECT_BODY
    ]


def test_the_summary_run_covers_none_of_the_body_sentences() -> None:
    name = "new-hwp-summary-only"
    regions = _effect_regions(name)
    result = build_coverage_map(regions, _selections(name, "expected_effect"))

    # 세 문장 모두 통째로 남는다. 각 구역 안에서는 100% 가 아니라 0% 다.
    assert [row.covered for row in result.regions] == [(), (), ()]
    assert [row.uncovered for row in result.regions] == [
        ((0, len(region.text)),) for region in regions
    ]
    # 요약표 선택은 구역 밖이다. 그 자체로는 결함이 아니라 다른 자리의 근거다.
    assert [row.occurrence_id for row in result.unattributed] == ["occ:rhwp:t4:c9:p0"]


def test_the_body_run_covers_one_sentence_and_leaves_two() -> None:
    name = "new-hwp-body-selected"
    result = build_coverage_map(
        _effect_regions(name), _selections(name, "expected_effect")
    )

    p75, p77, p79 = result.regions
    assert (p75.covered, p77.covered) == ((), ())
    assert p79.covered == ((74, 99),)
    assert result.unattributed == ()


# --- 계층과 겹침 -----------------------------------------------------------


def test_an_ancestor_selection_moves_only_when_the_text_is_unique() -> None:
    region = RegionSpan(
        occurrence_id="occ:t4:c5:p0", text="가나다라마", content_start=0, content_end=5
    )

    moved = build_coverage_map([region], [Selection("occ:t4:c5", 100, 103, text="다라")])
    assert moved.regions[0].covered == ((2, 4),)
    assert moved.unresolved == ()

    twice = RegionSpan(
        occurrence_id="occ:t4:c5:p0", text="가나가나", content_start=0, content_end=4
    )
    ambiguous = build_coverage_map([twice], [Selection("occ:t4:c5", 0, 2, text="가나")])
    # 어느 자리인지 문서가 말해 주지 않는다. 고르지 않는다.
    assert ambiguous.regions[0].covered == ()
    assert len(ambiguous.unresolved) == 1


def test_a_same_text_different_place_selection_is_not_folded_in() -> None:
    region = RegionSpan(
        occurrence_id="occ:p10", text="가나다", content_start=0, content_end=3
    )

    # id 가 계층 관계가 아니면 문구가 같아도 남의 자리다.
    result = build_coverage_map([region], [Selection("occ:p99", 0, 3, text="가나다")])

    assert result.regions[0].covered == ()
    assert [row.occurrence_id for row in result.unattributed] == ["occ:p99"]


@pytest.mark.parametrize(
    ("spans", "covered", "uncovered"),
    [
        ([(0, 4), (2, 8)], ((0, 8),), ((8, 10),)),
        ([(4, 6), (6, 9)], ((4, 9),), ((0, 4), (9, 10))),
        ([(-5, 3), (7, 40)], ((0, 3), (7, 10)), ((3, 7),)),
        ([(5, 5)], (), ((0, 10),)),
    ],
    ids=["겹침", "맞닿음", "구역 밖으로 넘침", "빈 선택"],
)
def test_spans_are_merged_and_clipped_to_the_region(
    spans: list[tuple[int, int]],
    covered: tuple[tuple[int, int], ...],
    uncovered: tuple[tuple[int, int], ...],
) -> None:
    region = RegionSpan(
        occurrence_id="occ:x", text="0123456789", content_start=0, content_end=10
    )

    result = build_coverage_map([region], [Selection("occ:x", a, b) for a, b in spans])

    assert result.regions[0].covered == covered
    assert result.regions[0].uncovered == uncovered


# --- 계층 판정의 경계 -------------------------------------------------------


@pytest.mark.parametrize(
    "sibling",
    ["occ:t4:c5:p10", "occ:t4:c50:p0", "occ:t4:c5:p1x"],
    ids=["p1 vs p10", "c5 vs c50", "구분자 없는 꼬리"],
)
def test_a_longer_id_is_not_a_child_without_a_separator(sibling: str) -> None:
    region = RegionSpan(
        occurrence_id=sibling, text="가나다", content_start=0, content_end=3
    )

    # ``occ:t4:c5:p1`` 은 ``occ:t4:c5:p10`` 의 부모가 아니라 형제다.
    result = build_coverage_map(
        [region], [Selection("occ:t4:c5:p1", 0, 3, text="가나다")]
    )

    assert result.regions[0].covered == ()
    assert [row.occurrence_id for row in result.unattributed] == ["occ:t4:c5:p1"]


def test_a_parent_matching_two_regions_is_assigned_to_neither() -> None:
    regions = [
        RegionSpan(
            occurrence_id="occ:t4:c5:p0", text="같은 문구", content_start=0, content_end=5
        ),
        RegionSpan(
            occurrence_id="occ:t4:c5:p1", text="같은 문구", content_start=0, content_end=5
        ),
    ]

    result = build_coverage_map(regions, [Selection("occ:t4:c5", 0, 5, text="같은 문구")])

    # 각 구역 안에서는 한 번씩이지만 전체에서는 유일하지 않다. 양쪽에 넣으면
    # 같은 근거를 두 번 세는 것이다.
    assert [row.covered for row in result.regions] == [(), ()]
    assert [row.occurrence_id for row in result.unresolved] == ["occ:t4:c5"]


def test_a_parent_matching_exactly_one_of_two_regions_moves_there() -> None:
    regions = [
        RegionSpan(
            occurrence_id="occ:t4:c5:p0", text="다른 문구", content_start=0, content_end=5
        ),
        RegionSpan(
            occurrence_id="occ:t4:c5:p1", text="찾는 문구", content_start=0, content_end=5
        ),
    ]

    result = build_coverage_map(regions, [Selection("occ:t4:c5", 0, 5, text="찾는 문구")])

    assert [row.covered for row in result.regions] == [(), ((0, 5),)]
    assert result.unresolved == ()


# --- 좌표 단위 -------------------------------------------------------------

# 절차표 화살표처럼 원문에 PUA 글리프가 섞여 있다. 좌표는 코드포인트 단위이고
# 바이트나 UTF-16 단위가 아니다.
_PUA_TEXT = "단계\U000f003b다음 단계"


def test_char_offsets_count_codepoints_not_bytes() -> None:
    # 글리프는 BMP 밖이지만 한 글자다.
    assert len(_PUA_TEXT) == 8
    assert len(_PUA_TEXT.encode("utf-8")) == 23  # 바이트로 세면 좌표가 달라진다
    region = RegionSpan(
        occurrence_id="occ:t91:c2",
        text=_PUA_TEXT,
        content_start=0,
        content_end=len(_PUA_TEXT),
    )

    result = build_coverage_map([region], [Selection("occ:t91:c2", 3, 8)])

    assert result.regions[0].covered == ((3, 8),)
    assert result.regions[0].uncovered == ((0, 3),)
    assert _PUA_TEXT[0:3] == "단계\U000f003b"


def test_a_parent_selection_lands_on_codepoint_offsets_past_a_pua_glyph() -> None:
    region = RegionSpan(
        occurrence_id="occ:t91:c2:p1",
        text=_PUA_TEXT,
        content_start=0,
        content_end=len(_PUA_TEXT),
    )

    result = build_coverage_map(
        [region], [Selection("occ:t91:c2", 999, 1000, text="음 단계")]
    )

    # 글리프를 한 글자로 세야 4 에서 시작한다. 바이트로 세면 10, UTF-16
    # 단위로 세면 5 다.
    assert result.regions[0].covered == ((4, 8),)
