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


# ------------------------------------------- analyze_cpl 연결 (CPL 계약의 일부)

# 감지 신호는 CPL 계약이라 analyze_cpl 안에 있어야 한다. 오케스트레이터가
# 따로 부르면 운영 경로와 테스트·다른 호출부가 서로 다른 결과를 받는다.


class _NoLlm:
    async def generate_structured(self, **_: Any) -> Any:
        raise RuntimeError("이 테스트는 축 분류를 부르지 않는다")


def _item(result, code: str):
    from worker.contracts.cpl_result import CplFieldCode

    field = next(row for row in CplFieldCode if row.name == code)
    return next(row for row in result.items if row.field_code is field)


def _run(profile: dict[str, Any], ir: dict[str, Any]):
    from worker.cpl import analyze_cpl

    return analyze_cpl(profile, _NoLlm(), model_profile="default", common_ir=ir)


def test_a_coverage_gap_reaches_the_item_without_going_through_the_job() -> None:
    from worker.contracts.cpl_result import EXTRACTION_COVERAGE_GAP

    result = _run(_profile([], "not_found"), _ir(_LABELLED))
    item = _item(result, "PURPOSE_GOAL")

    # 구조화 상태는 그대로 두고 사유만 더한다.
    assert item.subfields[0].status == "not_found"
    assert EXTRACTION_COVERAGE_GAP in item.subfields[0].reason_codes
    # 원문에 구역이 있으면 "내용 없음" 으로 확정할 수 없다.
    assert item.representative_status == "needs_confirmation"
    assert any(
        row.reason_code == EXTRACTION_COVERAGE_GAP for row in result.diagnostics
    )


def test_an_extracted_purpose_carries_no_signal() -> None:
    from worker.contracts.cpl_result import EXTRACTION_COVERAGE_GAP

    profile = _profile(
        [{"fact_id": "fact_1", "value_raw": "부산 관내 중소기업", "status": "identified"}],
        "identified",
    )

    item = _item(_run(profile, _ir(_LABELLED)), "PURPOSE_GOAL")

    assert EXTRACTION_COVERAGE_GAP not in item.subfields[0].reason_codes
    assert item.representative_status == "confirmed"


def test_an_explicit_absence_keeps_its_own_meaning() -> None:
    from worker.contracts.cpl_result import EXTRACTION_COVERAGE_GAP

    item = _item(_run(_profile([], "not_applicable"), _ir(_LABELLED)), "PURPOSE_GOAL")

    assert EXTRACTION_COVERAGE_GAP not in item.subfields[0].reason_codes


# 라벨 구역이 없으면 문서에 정말 없는 것이다. 그때는 내용 없음이 맞다.
def test_a_genuinely_absent_field_stays_no_content() -> None:
    item = _item(
        _run(_profile([], "not_found"), _ir("○ (사업예산) 총사업비 6,600백만원")),
        "PURPOSE_GOAL",
    )

    assert item.representative_status == "no_content"


@pytest.mark.parametrize(
    "label",
    [
        "○ 사업목적 : 경북 도내 바이오 기업",
        "○ (사업목적) 부산 관내 제조 중소기업",
        "○ 사업 목적 : 중소기업의 보유기술 상품화",
    ],
    ids=["콜론", "괄호", "라벨 안 공백"],
)
def test_every_observed_notation_reaches_the_item(label: str) -> None:
    item = _item(_run(_profile([], "not_found"), _ir(label)), "PURPOSE_GOAL")

    assert item.representative_status == "needs_confirmation"


# common_ir 을 받지 못하면 감지를 건너뛸 뿐 값·상태를 바꾸지 않는다.
def test_without_the_source_the_result_is_unchanged() -> None:
    from worker.cpl import analyze_cpl, build_cpl_result

    profile = _profile([], "not_found")

    without = analyze_cpl(profile, _NoLlm(), model_profile="default")
    baseline = build_cpl_result(profile)

    assert [row.representative_status for row in without.items] == [
        row.representative_status for row in baseline.items
    ]


# --------------------------------------------------- 라벨 위치와 구역 내용

# 셀 안 항목을 줄바꿈 없이 이어 쓴 서식이 있다. 줄머리만 보면 그런 문서는
# 통째로 감지에서 빠진다. 실문서 10건은 라벨이 우연히 줄머리에 있어서 통과했다.
def test_a_label_in_the_middle_of_a_paragraph_is_seen() -> None:
    joined = (
        "○ 사업기간 : 2026.1.1~2026.12.31"
        "○ 사업예산 : 금 10억원"
        "○ 사업목적 : 부산 관내 중소기업 지원"
    )

    assert detect_coverage_gaps(_profile([], "not_found"), _ir(joined))


# 라벨만 찍히고 내용이 비어 있으면 문서가 그 구역을 비운 것이다. 문서의 빈칸을
# 우리 결함으로 되돌리지 않는다.
@pytest.mark.parametrize(
    "text",
    [
        "○ 사업목적 :\n○ 사업예산 : 금 10억원",
        "○ 사업목적 : ○ 사업예산 : 금 10억원",
        "○ 사업목적 :",
    ],
    ids=["다음 줄에 다른 항목", "같은 줄에 다음 항목", "끝"],
)
def test_a_labelled_but_empty_region_is_not_a_missed_extraction(text: str) -> None:
    assert detect_coverage_gaps(_profile([], "not_found"), _ir(text)) == []


# 본문 속에 같은 낱말이 나온 것은 라벨이 아니다.
def test_the_word_inside_a_sentence_is_not_a_label() -> None:
    assert detect_coverage_gaps(
        _profile([], "not_found"), _ir("우리 사업목적은 아래와 같다")
    ) == []


# -------------------------------------------------- 재검 입력 (CplFragment)

# 감지 신호만으로는 재검을 못 한다. 구역이 비었다는 사실은 알지만 그 구역의
# 원문을 다시 줄 수 없기 때문이다. fragment 는 그 원문을 가리키는 좌표다.
# CplFact 를 대신하지 않는다 — 저쪽은 검증을 통과해 화면에 나가는 값이고
# 이쪽은 아직 값이 되지 못한 원문이다.


def _fragments(ir: dict[str, Any]):
    from worker.cpl_coverage import build_fragments

    return build_fragments(ir, profile_field=_PURPOSE)


def _doc(text: str, *, occurrence_id: str = "occ:1", document_id: str = "hwpx:d1"):
    return {
        "document": {"document_id": document_id},
        "blocks": [{
            "block_id": "hwpx:t4",
            "occurrences": [{"occurrence_id": occurrence_id, "text": text}],
        }],
    }


# 긴 한 문단에 항목이 이어진 서식에서 목적 구역만 끊어야 한다. 블록 전체를
# 주면 재검 입력이 다시 blob 이라 나눈 의미가 없다.
def test_a_fragment_stops_at_the_next_label() -> None:
    joined = (
        "○ 사업기간 : 2026.1.1~2026.12.31"
        "○ 사업목적 : 부산 관내 중소기업의 기술경쟁력 강화"
        "○ 사업예산 : 금 10억원"
    )

    fragments = _fragments(_doc(joined))

    assert len(fragments) == 1
    assert "사업예산" not in fragments[0].raw_text
    assert "사업기간" not in fragments[0].raw_text
    assert "기술경쟁력 강화" in fragments[0].raw_text


def test_an_empty_labelled_region_produces_no_fragment() -> None:
    assert _fragments(_doc("○ 사업목적 :\n○ 사업예산 : 금 10억원")) == []


def test_the_reference_is_the_same_every_time() -> None:
    document = _doc("○ (사업목적) 부산 관내 제조 중소기업")

    first = [row.evidence_ref for row in _fragments(document)]
    second = [row.evidence_ref for row in _fragments(_doc(
        "○ (사업목적) 부산 관내 제조 중소기업"
    ))]

    assert first == second != []


def test_the_reference_carries_the_document_coordinates() -> None:
    from worker.cpl_coverage import evidence_ref

    fragment = _fragments(_doc("○ (사업목적) 부산 관내 제조 중소기업"))[0]

    assert fragment.evidence_ref == evidence_ref(
        "hwpx:d1", "occ:1", fragment.start_char, fragment.end_char
    )
    assert fragment.common_ir_document_id == "hwpx:d1"
    assert fragment.common_ir_block_id == "hwpx:t4"
    assert fragment.common_ir_occurrence_id == "occ:1"
    # 이 슬라이스는 사업목적만 다룬다. 목적 FIT 관계는 role 을 요구하지 않으므로
    # 전역 role 체계를 함께 만들지 않는다.
    assert fragment.source_role is None


# 프로필의 fact_id 는 프로필 내부 좌표라 다른 프로필에서 같은 값이 다시 나온다.
# 재검 참조가 그것과 같은 공간을 쓰면 서로 다른 것이 같아 보인다.
def test_a_reference_never_looks_like_a_profile_fact_id() -> None:
    fragment = _fragments(_doc("○ (사업목적) 부산 관내 제조 중소기업"))[0]

    assert fragment.evidence_ref not in {"fact_1", "fact_2", "delivery_method_1"}
    assert "#" in fragment.evidence_ref and "@" in fragment.evidence_ref


# 표는 같은 본문을 계층마다 다시 싣는다. 구역 하나를 계층 수만큼 재검에 넣으면
# 같은 원문을 여러 번 묻는다.
def test_a_table_region_is_asked_once_not_once_per_layer() -> None:
    text = "○ (사업목적) 부산 관내 제조 중소기업"
    document = {
        "document": {"document_id": "hwpx:d1"},
        "blocks": [{
            "block_id": "hwpx:t4",
            "occurrences": [
                {"occurrence_id": "occ:rhwp:t4", "text": text},
                {"occurrence_id": "occ:rhwp:t4:c5", "text": text},
                {"occurrence_id": "occ:rhwp:t4:c5:p0", "text": text},
            ],
        }],
    }

    fragments = _fragments(document)

    assert len(fragments) == 1
    # 가장 좁은 occurrence 가 그 자리를 가장 정확히 가리킨다.
    assert fragments[0].common_ir_occurrence_id == "occ:rhwp:t4:c5:p0"


from worker.cpl_coverage import build_fragments  # noqa: E402


# --- 라벨과 내용이 다른 블록에 있는 서식 -----------------------------------
# 새 HWP 는 ``□ 사업목적`` 만 있는 블록 뒤에 내용이 따라온다. 지배가 넘어가되
# ``evidence_ref`` 는 **내용 occurrence** 를 가리킨다 — 그 문자열의 역할은
# "어느 원문 span 인가" 이고, 필드 귀속은 ``profile_field`` 가 따로 담는다.

def _sibling_ir(*paragraphs: str) -> dict[str, object]:
    return {
        "document": {"document_id": "hwp:doc"},
        "blocks": [
            {
                "block_id": f"hwp:b{index}",
                "reading_order": index,
                "occurrences": [
                    {"occurrence_id": f"occ:p{index}", "text": text}
                ],
            }
            for index, text in enumerate(paragraphs)
        ],
    }


def test_a_label_only_block_hands_the_region_to_the_next_block() -> None:
    fragments = build_fragments(
        _sibling_ir("□ 사업목적", " ㅇ ICT혁신기업의 기술개발 지원"),
        profile_field="comparison_profile.purpose_goal",
    )

    assert len(fragments) == 1
    fragment = fragments[0]
    # ref 는 내용 occurrence 다. 라벨 좌표를 문자열에 합치지 않는다.
    assert fragment.evidence_ref == "hwp:doc#occ:p1@0-19"
    assert fragment.common_ir_occurrence_id == "occ:p1"
    assert fragment.label_occurrence_id == "occ:p0"
    assert fragment.label_block_id == "hwp:b0"
    assert fragment.raw_text.strip() == "ㅇ ICT혁신기업의 기술개발 지원"


def test_domination_stops_at_the_next_form_label() -> None:
    assert build_fragments(
        _sibling_ir("□ 사업목적", "□ 지원대상 : 중소기업"),
        profile_field="comparison_profile.purpose_goal",
    ) == []


# 같은 블록 안에서 다음 라벨에 잘려 빈 구역은 넘기지 않는다.
def test_a_region_cut_by_the_next_label_does_not_reach_the_next_block() -> None:
    assert build_fragments(
        _sibling_ir("○ 사업목적 ○ 사업기간 : 2024~2028년", " ㅇ 남의 값"),
        profile_field="comparison_profile.purpose_goal",
    ) == []


# 같은 블록에서 끝나는 기존 서식은 라벨 provenance 가 없다.
def test_a_same_block_region_carries_no_label_provenance() -> None:
    (fragment,) = build_fragments(
        _sibling_ir("○ (사업목적) 기술경쟁력을 강화한다"),
        profile_field="comparison_profile.purpose_goal",
    )

    assert fragment.label_block_id is None
    assert fragment.label_occurrence_id is None
    assert fragment.evidence_ref.startswith("hwp:doc#occ:p0@0-")


# gap 판정은 fragment 와 같은 기준이어야 한다. 따로 세면 라벨과 내용이 갈린
# 서식에서 fragment 는 있는데 gap 이 없어 재검이 target 을 못 찾는다.
def test_a_sibling_region_is_a_gap_candidate() -> None:
    ir = _sibling_ir("□ 사업목적", " ㅇ 실제 목적 내용")
    profile = _profile([], "not_found")

    gaps = detect_coverage_gaps(profile, ir)

    assert len(gaps) == 1
    # 라벨 블록을 가리킨다 — 사람이 원문에서 찾아갈 자리다.
    assert gaps[0].label_block_ids == ("hwp:b0",)
    assert len(build_fragments(ir, profile_field=_PURPOSE)) == 1


def test_a_label_followed_by_another_form_label_is_not_a_gap() -> None:
    ir = _sibling_ir("□ 사업목적", "□ 지원대상 : 중소기업")

    assert detect_coverage_gaps(_profile([], "not_found"), ir) == []
    assert build_fragments(ir, profile_field=_PURPOSE) == []
