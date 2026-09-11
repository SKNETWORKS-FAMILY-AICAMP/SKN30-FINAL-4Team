"""라벨이 지배하는 원문 구역 계산을 고정한다.

후보 생성기와 워커의 누락 감지기가 이 한 정의를 공유한다. 여기가 어긋나면
한쪽이 찾는 구역을 다른 쪽이 못 찾는 상태가 조용히 생긴다.
"""

from __future__ import annotations

import pytest

from worker import vendor  # noqa: F401  (vendored 경로를 sys.path 에 넣는다)

from semantic_structuring.field_regions import build_field_regions
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
