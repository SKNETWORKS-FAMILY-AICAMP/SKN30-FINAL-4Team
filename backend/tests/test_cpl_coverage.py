"""라벨 구역은 있는데 값이 빈 필드를 후보로 표시한다는 것을 고정한다.

실문서에서 관측한 형태다. mockup_08 의 한 실행에서 원문에 ``○ (사업목적) …``
구역이 있고 다른 필드 27 개가 정상인데 ``purpose_goal`` 만 ``not_found`` 였다.
목적 fact 가 통째로 사라지면 네 의미 축이 동시에 없어져 FIT-1·2·3 이 함께
멈추므로, 다른 필드 하나가 빈 것보다 영향이 크다.

이 단계는 후보 표시까지만 한다. 라벨이 있는데 비었다는 사실은 결정적으로 셀 수
있지만, 기술적 추출 실패인지 값을 특정할 수 없는 서술인지 명시적 부재인지는
이 정보만으로 갈리지 않는다.
"""

from __future__ import annotations

from typing import Any

import pytest

from worker.cpl_coverage import detect_coverage_gaps


_PURPOSE = "comparison_profile.purpose_goal"


def _ir(text: str) -> dict[str, Any]:
    return {
        "blocks": [{
            "block_id": "hwpx:t4",
            "occurrences": [{"occurrence_id": "occ:1", "text": text}],
        }]
    }


def _profile(rows: list[dict[str, Any]], status: str) -> dict[str, Any]:
    return {
        "comparison_profile": {"purpose_goal": rows},
        "field_states": [{"field_name": "purpose_goal", "status": status}],
    }


_LABELLED = "○ (사업목적) 부산 관내 제조·정보통신 중소기업의 기술경쟁력을 강화"


def test_a_labelled_region_with_no_value_becomes_a_candidate() -> None:
    gaps = detect_coverage_gaps(_profile([], "not_found"), _ir(_LABELLED))

    assert [gap.profile_field for gap in gaps] == [_PURPOSE]
    assert gaps[0].status == "not_found"
    assert gaps[0].label_block_ids == ("hwpx:t4",)


# 값이 잡혔으면 후보가 아니다. 빈 것만 본다.
def test_an_extracted_field_is_never_a_candidate() -> None:
    profile = _profile(
        [{"fact_id": "fact_1", "value_raw": "부산 관내 중소기업", "status": "identified"}],
        "identified",
    )

    assert detect_coverage_gaps(profile, _ir(_LABELLED)) == []


# 라벨 구역이 없으면 문서에 없는 것이다. 추출 누락으로 의심하지 않는다.
def test_no_label_region_means_no_candidate() -> None:
    other = "○ (사업예산) 총사업비 6,600백만원"

    assert detect_coverage_gaps(_profile([], "not_found"), _ir(other)) == []


# 문서가 "해당 없음" 이라고 말한 것을 추출 누락으로 되돌리지 않는다.
def test_an_explicit_absence_is_not_a_missed_extraction() -> None:
    profile = _profile([], "not_applicable")

    assert detect_coverage_gaps(profile, _ir(_LABELLED)) == []


# 라벨 문법은 닫혀 있되 서식마다 다른 표기를 덮어야 한다. 실문서 10 건에서
# 관측한 세 가지다. 하나라도 놓치면 그 문서는 감지에서 통째로 빠진다.
@pytest.mark.parametrize(
    "label",
    [
        "○ 사업목적 : 경북 도내 바이오 기업의 신제품 개발",
        "○ (사업목적) 부산 관내 제조 중소기업의 기술경쟁력 강화",
        "○ 사업 목적 : 중소기업의 보유기술 상품화",
        "□ 사업목적: 강원도 부품소재 기업 지원",
        "사업목적\n부산 관내 중소기업",
    ],
    ids=["콜론", "괄호", "라벨 안 공백", "다른 글머리", "글머리 없음"],
)
def test_the_label_grammar_covers_the_observed_notations(label: str) -> None:
    assert detect_coverage_gaps(_profile([], "not_found"), _ir(label))


# 표 셀 occurrence 도 본다. 라벨이 셀 안에만 있으면 블록 본문에는 없을 수 있다.
def test_a_label_inside_a_table_cell_is_seen() -> None:
    ir = {
        "blocks": [{
            "block_id": "hwpx:t4",
            "occurrences": [
                {"occurrence_id": "occ:t4", "text": "표 전체 텍스트"},
                {"occurrence_id": "occ:t4:c5", "text": _LABELLED},
            ],
        }]
    }

    assert detect_coverage_gaps(_profile([], "not_found"), ir)
