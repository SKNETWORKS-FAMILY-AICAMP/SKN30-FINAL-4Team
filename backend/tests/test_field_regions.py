"""라벨이 지배하는 원문 구역 계산을 고정한다.

후보 생성기와 워커의 누락 감지기가 이 한 정의를 공유한다. 여기가 어긋나면
한쪽이 찾는 구역을 다른 쪽이 못 찾는 상태가 조용히 생긴다.
"""

from __future__ import annotations

import pytest

from worker import vendor  # noqa: F401  (vendored 경로를 sys.path 에 넣는다)

from semantic_structuring.field_regions import build_field_regions, find_text_regions
from semantic_structuring.models import CandidatePack, SourceBlock, SourceRelation


def _pack(*blocks: tuple[str, str], **kwargs: object) -> CandidatePack:
    rows = []
    for order, (block_id, text) in enumerate(blocks):
        rows.append(
            SourceBlock(
                block_id=block_id,
                text=text,
                relation=SourceRelation.CANDIDATE,
                source_order=order,
                common_ir_block_id=kwargs.get("common_ir_block_id") or block_id,
            )
        )
    return CandidatePack(
        pack_id="test-pack", notice_id="test", question="test", blocks=rows,
    )


def _regions(*blocks: tuple[str, str], field: str = "program_period"):
    return build_field_regions(_pack(*blocks), field_name=field)


def _contents(*blocks: tuple[str, str], field: str = "program_period") -> list[str]:
    return [r.content_text.strip() for r in _regions(*blocks, field=field)]


# 라벨과 값이 같은 블록에 있는 흔한 형태들.
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("사업기간 2024~2028년", "2024~2028년"),
        ("사업기간(2024~2028년)", "(2024~2028년)"),
        ("○ (사업기간) 2026. 01. 01. ~ 2028. 12. 31.", "2026. 01. 01. ~ 2028. 12. 31."),
        ("◦(사업기간) `24~`28년(5년)", "`24~`28년(5년)"),
        ("ㅇ 사업기간 : ’24 ~ ’28(5년)", "’24 ~ ’28(5년)"),
        ("○ 사업 수행 기간 : 2026~2028년", "2026~2028년"),
        ("□ 전체 추진기간 : 2024~2028년", "2024~2028년"),
    ],
)
def test_label_and_value_in_one_block(text: str, expected: str) -> None:
    assert _contents(("b0", text)) == [expected]


# 라벨만 있는 제목 블록 뒤에 내용이 따라오는 서식.
def test_a_label_only_block_takes_the_following_block() -> None:
    regions = _regions(("b0", "□ 사업목적"), ("b1", " ㅇ ICT혁신기업의 기술개발 지원"))
    assert [r.content_text.strip() for r in regions] == []

    regions = _regions(
        ("b0", "□ 사업목적"), ("b1", " ㅇ ICT혁신기업의 기술개발 지원"),
        field="purpose_goal",
    )
    assert len(regions) == 1
    assert regions[0].block_id == "b1"           # 좌표는 내용 블록 기준
    assert regions[0].label_block_id == "b0"     # 라벨은 앞 블록
    assert regions[0].content_text.strip() == "ㅇ ICT혁신기업의 기술개발 지원"


# 지배는 다음 라벨에서 끝난다. 남의 값을 가져오지 않는다.
def test_a_label_only_block_stops_at_the_next_label() -> None:
    assert _regions(
        ("b0", "□ 사업목적"), ("b1", "○ 사업기간 : 2024~2028년"), field="purpose_goal",
    ) == []


# 한 문단에 항목이 줄바꿈 없이 이어진 서식. 구역이 뒤 항목을 삼키면 안 된다.
def test_a_mid_paragraph_label_does_not_swallow_the_next_item() -> None:
    line = "○ 사업목적 : 기술경쟁력 강화 ○ 사업기간 : ’24 ~ ’28 ○ 사업예산 : 2029~2030년"

    assert _contents(("b0", line)) == ["’24 ~ ’28"]
    assert _contents(("b0", line), field="purpose_goal") == ["기술경쟁력 강화"]


# 라벨이 없으면 구역도 없다. 단계 기간과 계획 인용이 여기서 걸러진다.
@pytest.mark.parametrize(
    "text",
    [
        "- 1단계(2026~2027년): 시제품 제작 및 성능검증 중심 지원",
        "제4차 중소기업 기술혁신 촉진계획(2024~2028년)",
        "업력: 업력 3년 이상 10년 이내",
    ],
)
def test_text_without_the_label_has_no_region(text: str) -> None:
    assert _regions(("b0", text)) == []


# 라벨만 찍히고 뒤가 빈 구역은 문서가 비운 것이다. 이어받을 블록도 없으면
# 구역으로 세지 않는다.
def test_an_empty_label_region_is_not_a_region() -> None:
    assert _regions(("b0", "○ 사업기간 :")) == []


# 표는 같은 본문을 부모·셀·문단으로 세 번 싣는다. 가장 좁은 좌표 하나만 남긴다.
def test_nested_table_duplicates_keep_only_the_narrowest_block() -> None:
    text = "○ (사업기간) 2026. 01. 01. ~ 2028. 12. 31."
    pack = CandidatePack(
        pack_id="t", notice_id="t", question="t",
        blocks=[
            SourceBlock(
                block_id=block_id, text=text, relation=SourceRelation.CANDIDATE,
                source_order=order, common_ir_block_id="hwpx:t4",
            )
            for order, block_id in enumerate(
                ["hwpx:t4", "hwpx:t4#r3c1", "hwpx:t4#r3c1p0"]
            )
        ],
    )

    regions = build_field_regions(pack, field_name="program_period")

    assert [r.block_id for r in regions] == ["hwpx:t4#r3c1p0"]


# 복합 제목은 값 구역이 아니다. ``□ 사업기간 및 전체예산`` 의 ``및 전체예산`` 을
# 사업기간 값으로 오인하면 coverage 에 붙였을 때 거짓 gap 이 된다.
@pytest.mark.parametrize(
    "text",
    [
        "□ 사업기간 및 전체예산",
        "□ 사업목적 및 사업필요성",
        "○ 지원대상 및 지원조건",
    ],
)
def test_a_compound_heading_is_not_a_value_region(text: str) -> None:
    assert _regions(("b0", text)) == []
    assert _regions(("b0", text), field="purpose_goal") == []


# 복합 제목은 뒤 블록으로 지배를 넘기지도 않는다. ``□ 사업기간 및 전체예산`` 다음
# 값이 사업기간의 것인지 전체예산의 것인지 원문만으로 가릴 수 없다. 라벨이 다른
# 라벨에 잘려 비었을 때는 이어받지 않는 쪽이 맞다 — 추측으로 남의 값을 가져오는
# 것보다 비워 두고 재검에 맡기는 편이 낫다.
def test_a_compound_heading_does_not_hand_over_to_the_next_block() -> None:
    assert _regions(("b0", "□ 사업기간 및 전체예산"), ("b1", " 2024 ~ 2028 (5년)")) == []


# 반면 단독 제목은 넘긴다. 뒤 값이 누구 것인지 모호하지 않다.
def test_a_single_heading_takes_the_following_value_block() -> None:
    regions = _regions(("b0", "□ 사업기간"), ("b1", " 2024 ~ 2028 (5년)"))

    assert len(regions) == 1
    assert regions[0].block_id == "b1"
    assert regions[0].label_block_id == "b0"


# 구역을 만들지 않는 다른 항목 라벨도 경계로 인식해야 한다. 글머리 기호 없이
# 낱말로만 이어 쓴 서식에서 앞 구역이 뒤 항목을 삼키지 않는다.
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("○ 사업기간 : 2024~2028년 사업예산 : 35,300백만원", "2024~2028년"),
        ("○ 사업기간 : 2024~2028년 지원대상 : 중소기업", "2024~2028년"),
        ("○ 사업기간 : 2024~2028년 수행기관 : 테크노파크", "2024~2028년"),
    ],
)
def test_another_field_label_ends_the_region(text: str, expected: str) -> None:
    assert _contents(("b0", text)) == [expected]


# 같은 문구가 서로 다른 자리에 두 번 나오면 둘 다 남는다. 문구가 같다는 이유로
# 접으면 서로 다른 근거가 하나로 사라진다.
def test_the_same_phrase_at_two_places_is_kept_twice() -> None:
    regions = _regions(
        ("hwpx:t4#r1c1p0", "○ 사업기간 : 2024~2028년"),
        ("hwpx:t4#r9c1p0", "○ 사업기간 : 2024~2028년"),
    )

    assert sorted(r.block_id for r in regions) == [
        "hwpx:t4#r1c1p0", "hwpx:t4#r9c1p0",
    ]


# 접는 것은 계층 중첩뿐이다. 좌표는 언제나 식별에 남는다.
def test_a_region_carries_its_own_occurrence_and_offsets() -> None:
    (region,) = _regions(("hwpx:t4#r3c1p0", "○ (사업기간) 2024~2028년"))

    assert region.block_id == "hwpx:t4#r3c1p0"
    assert region.content_start < region.content_end
    assert region.content_text == "2024~2028년"
    assert region.contains(region.content_start, region.content_end)
    assert not region.contains(0, region.content_end)


# --- 공개 순수 함수 -------------------------------------------------------
# 소비자마다 좌표 원천이 다르다. 벤더는 CandidatePack 블록 텍스트에, 워커는
# Common IR occurrence 텍스트에 같은 규칙을 적용하고 각자 식별자를 만든다.

def test_find_text_regions_returns_offsets_only(text: str = "○ (사업기간) 2024~2028년") -> None:
    (span,) = find_text_regions(text, field_name="program_period")

    assert span.field_name == "program_period"
    assert span.label_text == "○ (사업기간)"
    assert text[span.content_start : span.content_end] == "2024~2028년"
    assert span.label_start < span.content_start


def test_find_text_regions_skips_an_empty_region() -> None:
    assert find_text_regions("○ 사업기간 :", field_name="program_period") == []


def test_find_text_regions_is_unknown_field_safe() -> None:
    assert find_text_regions("○ 사업기간 : 2024~2028년", field_name="총사업비") == []


# 같은 텍스트 안에 같은 문구가 두 번 나오면 두 구간 다 돌려준다. 여기서 접으면
# 서로 다른 자리의 근거가 사라진다.
def test_find_text_regions_keeps_both_occurrences_of_one_phrase() -> None:
    text = "○ 사업기간 : 2024~2028년\n○ 사업기간 : 2024~2028년"

    spans = find_text_regions(text, field_name="program_period")

    assert len(spans) == 2
    assert spans[0].content_start != spans[1].content_start


# 본문에 나온 낱말로는 구역을 끊지 않는다. 라벨 자리에 있을 때만 경계다.
def test_a_boundary_word_in_running_text_does_not_cut_the_region() -> None:
    text = "○ (사업목적) 중소기업의 지원대상을 넓혀 기술경쟁력을 강화한다"

    (span,) = find_text_regions(text, field_name="purpose_goal")

    assert text[span.content_start : span.content_end].strip() == (
        "중소기업의 지원대상을 넓혀 기술경쟁력을 강화한다"
    )


# 구분자를 달고 있으면 글머리 없이 이어 붙여도 라벨이다.
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("사업기간 2024~2028년 사업예산 : 35,300백만원", "2024~2028년"),
        ("사업기간 2024~2028년 지원대상 : 중소기업", "2024~2028년"),
        ("○ 사업기간 : 2024~2028년 수행기관 : 테크노파크", "2024~2028년"),
    ],
)
def test_a_boundary_word_with_a_separator_ends_the_region(
    text: str, expected: str
) -> None:
    (span,) = find_text_regions(text, field_name="program_period")

    assert text[span.content_start : span.content_end].strip() == expected


# 같은 블록 안의 서로 다른 자리는 접지 않는다. 계층 중첩만 접는다.
def test_two_places_in_one_block_are_both_kept() -> None:
    regions = _regions(("b0", "○ 사업기간 : 2024~2028년\n○ 사업기간 : 2024~2028년"))

    assert len(regions) == 2
    assert {r.content_start for r in regions} == {8, 28}


# sibling 지배는 구역을 만들지 않는 다른 항목 라벨에서도 끝난다.
def test_sibling_domination_stops_at_any_form_label() -> None:
    assert _regions(
        ("b0", "□ 사업목적"), ("b1", "□ 지원대상 : 중소기업"), field="purpose_goal",
    ) == []


# --- include_empty / continues_to_sibling ---------------------------------
# 라벨이 아예 없는 것과 라벨은 있는데 비어 있는 것은 다른 사실이다. 다음 블록으로
# 지배가 넘어가는지는 그 구분에서만 나온다.

@pytest.mark.parametrize("text", ["□ 사업목적", "□ 사업목적 ", "□ 사업목적\n"])
def test_a_label_that_runs_out_of_text_may_continue_to_a_sibling(text: str) -> None:
    assert find_text_regions(text, field_name="purpose_goal") == []

    (span,) = find_text_regions(text, field_name="purpose_goal", include_empty=True)

    assert span.content_start >= span.content_end or not text[
        span.content_start : span.content_end
    ].strip()
    assert span.continues_to_sibling is True


# 다음 라벨 때문에 비었으면 넘기지 않는다. 뒤따르는 내용은 남의 값이다.
@pytest.mark.parametrize(
    "text",
    [
        "○ 사업목적 ○ 사업기간 : 2024~2028년",
        "○ 사업목적\n○ 사업기간 : 2024~2028년",
        "○ 사업목적 지원대상 : 중소기업",
    ],
)
def test_a_label_cut_by_the_next_label_never_continues(text: str) -> None:
    (span,) = find_text_regions(text, field_name="purpose_goal", include_empty=True)

    assert span.continues_to_sibling is False


# 내용이 있는 구간은 예전과 같고 이어받지 않는다.
def test_a_filled_region_is_unchanged_and_never_continues() -> None:
    text = "○ 사업목적 : 기술경쟁력 강화"

    (default,) = find_text_regions(text, field_name="purpose_goal")
    (with_empty,) = find_text_regions(
        text, field_name="purpose_goal", include_empty=True
    )

    assert default == with_empty
    assert default.continues_to_sibling is False
    assert text[default.content_start : default.content_end].strip() == "기술경쟁력 강화"


# 접속사는 라벨 **바로 뒤** 에서만 복합 제목을 만든다. 본문 한가운데의 ``성과``
# 마지막 글자나 ``산업·`` 의 가운뎃점이 접속사로 읽히면 구역이 거기서 잘린다.
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("○ 사업목적 : 성과 지원대상을 넓힌다", "성과 지원대상을 넓힌다"),
        ("○ 사업목적 : 산업·지원대상 기업의 경쟁력을 강화한다", "산업·지원대상 기업의 경쟁력을 강화한다"),
        ("○ 사업목적 : 기술과 지원내용을 함께 본다", "기술과 지원내용을 함께 본다"),
    ],
)
def test_a_conjunction_inside_running_text_does_not_cut_the_region(
    text: str, expected: str
) -> None:
    (span,) = find_text_regions(text, field_name="purpose_goal")

    assert text[span.content_start : span.content_end].strip() == expected


# 라벨 바로 뒤의 접속사는 복합 제목이다.
@pytest.mark.parametrize(
    ("text", "field"),
    [
        ("□ 사업기간 및 전체예산", "program_period"),
        ("□ 사업목적 및 사업필요성", "purpose_goal"),
        ("○ 지원내용·지원규모", "purpose_goal"),
    ],
)
def test_a_conjunction_right_after_the_label_makes_a_compound_heading(
    text: str, field: str
) -> None:
    assert find_text_regions(text, field_name=field) == []
