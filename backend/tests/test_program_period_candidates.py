"""program_period 후보 생성이 원문의 연도를 잘라먹지 않는지 고정한다.

``program_period`` 는 서버가 만든 후보만 쓸 수 있다(``_resolve_anchor`` 의
``required_candidate_kind``). 후보가 틀리면 LLM 이 바로잡을 경로가 없고 틀린
값이 ``confirmed`` 로 화면에 올라간다. 그래서 후보 생성 자체를 고정한다.

공문 서식은 날짜를 ``2026. 01. 01. ~ 2028. 12. 31.`` 처럼 일자 뒤에도 마침표를
찍는다. 이 표기에서만 결함이 났으므로 표기별 회귀를 함께 잠근다.
"""

from __future__ import annotations

import pytest

from worker import vendor  # noqa: F401  (vendored 경로를 sys.path 에 넣는다)

from semantic_structuring.models import CandidatePack, SourceBlock, SourceRelation
from semantic_structuring.request_profile_v012 import (
    _DROPPED_DOTTED_YEAR_PREFIX,
    _PROGRAM_PERIOD_DATE_RANGE_FINDER,
    build_value_span_candidates,
)


def _pack(text: str) -> CandidatePack:
    return CandidatePack(
        pack_id="test-pack",
        notice_id="test",
        question="test",
        blocks=[
            SourceBlock(
                block_id="b0", text=text, relation=SourceRelation.CANDIDATE,
            )
        ],
    )


def _period_candidates(text: str) -> list[str]:
    return [
        row.value_raw
        for row in build_value_span_candidates(_pack(text))
        if row.candidate_kind == "program_period_date_range"
    ]


# 실제 결함: 일자 뒤 마침표 때문에 YMD 토큰이 실패하고, YM 대안이 ``01. 01.``
# 을 년-월로 오인해 앞의 ``2026.`` 을 통째로 버렸다.
def test_official_form_date_keeps_its_leading_year() -> None:
    line = "○ (사업기간) 2026. 01. 01. ~ 2028. 12. 31. (3년, 다년도 사업)"

    assert _period_candidates(line) == ["2026. 01. 01. ~ 2028. 12. 31"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # 결함이 났던 표기
        ("2026. 01. 01. ~ 2028. 12. 31.", "2026. 01. 01. ~ 2028. 12. 31"),
        # 회귀: 아래 표기들은 원래 정상이었고 그대로여야 한다
        ("기간: 2025.3.1~2025.12.31", "2025.3.1~2025.12.31"),
        ("2027. 3월 ~ 2028. 2월", "2027. 3월 ~ 2028. 2월"),
        ("3월~12월", "3월~12월"),
        ("공고일 ~ 2027.12.31", "공고일 ~ 2027.12.31"),
        ("2026.1.1 - 2026.12.31", "2026.1.1 - 2026.12.31"),
        ("2026년 1월 ~ 2026년 12월", "2026년 1월 ~ 2026년 12월"),
    ],
)
def test_date_range_forms_are_captured_whole(text: str, expected: str) -> None:
    assert _PROGRAM_PERIOD_DATE_RANGE_FINDER.search(text).group() == expected


# 후보가 연도 바로 뒤에서 시작하면 그 연도를 버린 것이다. 정규식이 다시
# 어긋나도 틀린 값이 조용히 confirmed 로 올라가지 않게 막는 안전망이다.
def test_candidate_starting_right_after_a_dotted_year_is_suppressed() -> None:
    assert _DROPPED_DOTTED_YEAR_PREFIX.search("○ (사업기간) 2026. ")
    assert _DROPPED_DOTTED_YEAR_PREFIX.search("2027. ")


# 인접이 아니면 억제하지 않는다. ``$`` 앵커가 그 경계다.
@pytest.mark.parametrize(
    "prefix", ["○ (사업기간) ", "2026. 상반기 ", "2025년 기준 ", ""],
)
def test_a_year_separated_by_other_text_never_suppresses(prefix: str) -> None:
    assert not _DROPPED_DOTTED_YEAR_PREFIX.search(prefix)


# 다년도 사업은 기간을 연도 범위로만 적기도 한다. 이 문법은 사업기간 구역
# 안에서만 돈다 — 문서 전체에서 찾으면 단계 기간이 같은 목록에 섞이고, 실측에서
# 그것만으로 같은 구역을 쓰는 CPL-03 추출이 3/3 에서 0/3 으로 떨어졌다.
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("○ (사업기간) `24~`28년(5년)", "`24~`28년"),
        ("ㅇ 사업기간 : ’24 ~ ’28(5년)", "’24 ~ ’28"),
        ("○ (사업기간) '21년 ~ '25년(5년)", "'21년 ~ '25년"),
        ("사업기간 2024~2028년", "2024~2028년"),
        ("사업기간(2024~2028년)", "2024~2028년"),
        ("○ 사업 수행 기간 : ’24 ~ ’28", "’24 ~ ’28"),
    ],
)
def test_year_only_range_inside_the_period_region(text: str, expected: str) -> None:
    assert _period_candidates(text) == [expected]


# 구역 밖의 연도 범위는 후보가 아니다. 라벨이 없으면 사업기간 자리가 아니다.
@pytest.mark.parametrize(
    "text",
    [
        "- 1단계(2026~2027년): 시제품 제작 및 성능검증 중심 지원, 연 40개사",
        "제4차 중소기업 기술혁신 촉진계획(2024~2028년)",
        "◦(지원기간) ’24 ~ ’28",
    ],
)
def test_a_year_range_outside_the_period_region_is_not_a_candidate(text: str) -> None:
    assert _period_candidates(text) == []


# 구역은 다음 항목에서 끝난다. 뒤 항목의 연도 범위를 가져오지 않는다.
def test_the_region_does_not_reach_into_the_next_item() -> None:
    line = "○ 사업기간 : ’24 ~ ’28 ○ 관련계획 : 2030~2035년"

    assert _period_candidates(line) == ["’24 ~ ’28"]


# 연도 표시가 없는 범위는 구역 안에서도 받지 않는다. 계획 인용이 똑같이
# 생겼고 Rule 로는 가를 수 없다.
def test_a_year_range_without_a_marker_is_not_a_candidate() -> None:
    assert _period_candidates("○ (사업기간) 2024~2028") == []


# 두 자리 연도를 2000 년대로 펴지 않는다. value_raw 는 원문 그대로다.
def test_two_digit_years_are_never_expanded() -> None:
    assert _period_candidates("ㅇ 사업기간 : ’24 ~ ’28") == ["’24 ~ ’28"]


# 일자·연월 범위는 구역과 무관하게 지금까지처럼 문서 어디서나 찾는다.
def test_dated_ranges_stay_document_wide() -> None:
    assert _period_candidates("기간: 2025.3.1~2025.12.31") == ["2025.3.1~2025.12.31"]


# 연도 아닌 숫자는 구역 안에 있어도 기간이 되지 않는다.
@pytest.mark.parametrize(
    "tail",
    [
        "051-888-0000", "제62조의2, 제9조", "총사업비 6,600백만원",
        "120개사 (연 40개사)", "최대 5,000만원", "목표값 240명",
    ],
)
def test_numbers_that_are_not_years_never_become_a_period(tail: str) -> None:
    assert _period_candidates(f"○ (사업기간) {tail}") == []
