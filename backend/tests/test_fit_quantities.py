"""FIT-7 정량 Rule 의 안전장치를 고정한다.

이 Rule 은 최초 파이프라인 커밋 이후 바뀐 적이 없고 테스트가 하나도 없었다.
단위·맥락을 넓히기 전에 **지금 지켜지고 있는 것**부터 고정한다. 넓히는 쪽만
테스트 없이 손대면, 값을 더 읽게 되면서 조용히 거짓 불일치가 생긴다.

여기서 고정하지 않는 것이 하나 있다. ``총 120개사`` 와 ``연 40개사`` 가 지금
같은 축으로 묶이는 동작은 **목표 계약이 아니다**. 양립 가능한 수치를 모순으로
만들기 때문이다. 그 동작은 아래 ``xfail`` 로 목표 쪽을 적어 두고, 고쳐지면
테스트가 xpass 로 알려 준다.
"""

from __future__ import annotations

import pytest

from worker.contracts.cpl_result import CplEvidence
from worker.contracts.fit_result import FitEvidenceRef
from worker.fit import _axis_values, _quantities
from worker.fit import _shared_spans
from worker.quantities import QuantitySpan, read_quantities


# --- 읽어야 하는 것 ---------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("6억원", {("AMOUNT_KRW", 600_000_000)}),
        ("연간 6억원", {("AMOUNT_KRW", 600_000_000)}),
        ("5,000만원", {("AMOUNT_KRW", 50_000_000)}),
        ("최대 5,000만원", {("AMOUNT_KRW", 50_000_000)}),
        ("1,500,000,000원", {("AMOUNT_KRW", 1_500_000_000)}),
        ("3천원", {("AMOUNT_KRW", 3_000)}),
        ("120개사", {("COUNT:개사", 120)}),
        ("연 40개사", {("COUNT:개사", 40)}),
        ("15개", {("COUNT:개", 15)}),
        ("240명", {("COUNT:명", 240)}),
        ("90건", {("COUNT:건", 90)}),
        ("총 8회", {("TIMES:TOTAL", 8)}),
        ("월 2회", {("TIMES:월", 2)}),
    ],
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_supported_expressions_are_read(raw: str, expected: set) -> None:
    assert _quantities(raw) == expected


# 설명 문구가 섞여 있어도 숫자가 모두 읽히면 값이 나온다. 문장 전체가 지원
# 문법이어야 한다는 뜻이 아니다.
def test_prose_around_a_number_does_not_block_it() -> None:
    assert _quantities("제조·정보통신 중소기업 120개사에 시제품 제작비를 지원") == {
        ("COUNT:개사", 120)
    }


# --- 버려야 하는 것: 숫자 시퀀스 소비 규칙 ----------------------------------
# 숫자 하나라도 어떤 매치에도 안 걸리면 값 전체를 버린다. 일부만 읽으면 서로
# 다른 값이 일치로 판정된다.


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ("9~15억원", "범위의 뒤쪽만 읽으면 9 가 사라진다"),
        ("과제당 총 9~15억원", "범위 + 수식어"),
        ("1억 5000만원", "복합 단위를 부분값으로 만들지 않는다"),
        ("10~20개사", "개수 범위"),
        ("3차년도 선별지원", "차수는 정량 축이 아니다"),
        ("과제당 총 9억원~15억원, 연간 6억원 / 3차년 선별지원", "읽히는 값이 섞여 있어도 전부 버린다"),
    ],
)
def test_a_number_the_rule_cannot_read_discards_the_whole_value(
    raw: str, reason: str
) -> None:
    assert _quantities(raw) == set(), reason


# --- 현재 미지원 동작 기록 --------------------------------------------------
# 아래는 **금지 계약이 아니라 지금 못 읽는다는 기록**이다. 실문서에서 자주
# 나오는 표현이고 (트레이스 전수에서 백만원 15종·비율 12종), 지원하기로 정하면
# 이 테스트의 기대값을 바꾸는 것이 정상이다. 지원 전에 비교 맥락 계약이
# 필요해서 아직 열지 않았을 뿐이다.


@pytest.mark.parametrize(
    ("raw", "note"),
    [
        ("6,000백만원", "백만 단위. 예산 문서의 표준 표기"),
        ("총사업비 6,600백만원", "같은 단위"),
        ("80%", "비율. 축과 비교 의미를 함께 정해야 한다"),
        ("보조율: 80%, 자부담률: 20%", "성격이 다른 비율 둘"),
    ],
)
def test_currently_unsupported_expressions_are_recorded(raw: str, note: str) -> None:
    assert _quantities(raw) == set(), note


# 쉼표 문법이 틀리면 유효한 숫자를 만들지 않는다.
@pytest.mark.parametrize("raw", ["1,2,3만원", "1,,000원", "12,34개사"])
def test_a_malformed_thousands_group_is_not_repaired(raw: str) -> None:
    assert _quantities(raw) == set()


# 원 단위 정수가 안 되면 반올림 방향을 추측하지 않는다.
def test_a_sub_won_amount_is_discarded_instead_of_rounded() -> None:
    assert _quantities("0.0001천원") == set()


# 십진 고정소수로 계산한다. float 로 곱하면 0.29억원이 28,999,999 가 된다.
def test_a_decimal_amount_keeps_its_exact_value() -> None:
    assert _quantities("0.29억원") == {("AMOUNT_KRW", 29_000_000)}
    assert _quantities("2,900만원") == {("AMOUNT_KRW", 29_000_000)}


def test_an_empty_value_is_not_an_error() -> None:
    assert _quantities(None) == set()
    assert _quantities("") == set()


# --- component 경계 -------------------------------------------------------


def _ref(fact_id: str, raw: str, component: str | None) -> FitEvidenceRef:
    return FitEvidenceRef(
        fact_id=fact_id,
        field_name="comparison_profile.support_scale",
        value_raw=raw,
        evidence=[CplEvidence(None, None, None, [])],
        primary_component_id=component,
        quantities=read_quantities(raw),
    )


# 소속이 다른 값을 한 자리에서 비교하지 않는다. 초안 §7.1 이 총사업비와 직접
# 지원금을 자동 비교하지 말라고 못박은 것과 같은 이유다.
def test_values_from_different_components_never_share_an_axis_bucket() -> None:
    grouped, invalid, withheld = _axis_values([
        _ref("f1", "기업당 한도 5,000만원", "component_1"),
        _ref("f2", "기업당 한도 7,000만원", "component_2"),
    ])

    assert (invalid, withheld) == ([], [])
    assert set(grouped) == {"component_1", "component_2"}
    # 같은 수량(기업당 한도)이지만 소속이 달라 한 집합에 들어가지 않는다.
    assert grouped["component_1"] != grouped["component_2"]


# 소속이 없는 값은 None 그룹에 남고 특정 component 값에 붙지 않는다.
def test_a_value_without_a_component_stays_in_its_own_bucket() -> None:
    grouped, _invalid, _withheld = _axis_values([
        _ref("f1", "기업당 한도 5,000만원", "component_1"),
        _ref("f2", "기업당 한도 6억원", None),
    ])

    assert set(grouped) == {"component_1", None}


def test_an_unreadable_value_is_reported_not_silently_dropped() -> None:
    grouped, invalid, withheld = _axis_values([
        _ref("f1", "기업당 한도 5,000만원", "component_1"),
        _ref("f2", "6,000백만원", "component_1"),
    ])

    # 읽지 못한 값은 진단으로 남는다. 호출부가 그 사실을 사용자에게 알린다.
    assert invalid == ["f2"]
    assert withheld == []
    assert set(grouped) == {"component_1"}


# 값은 읽었는데 비교 맥락을 확정하지 못한 것은 원문 부재와 다르다. 따로 센다.
def test_a_value_without_a_confirmed_context_is_withheld_not_invalid() -> None:
    grouped, invalid, withheld = _axis_values([
        _ref("f1", "5,000만원", "component_1"),
    ])

    assert invalid == []
    assert withheld == ["f1"]
    assert grouped == {}


# --- 같은 수량끼리만 비교 ---------------------------------------------------
# 좌우는 서로 다른 원문 좌표에서 온 근거여야 한다. 같은 근거를 양쪽에 넣으면
# 자기 자신과 비교하는 셈이라 일치가 보장된다.


def _compare(left_raw: str, right_raw: str) -> tuple:
    left, _li, _lw = _axis_values([_ref("left_1", left_raw, "component_1")])
    right, _ri, _rw = _axis_values([_ref("right_1", right_raw, "component_1")])
    return left.get("component_1", {}), right.get("component_1", {})


def test_the_same_quantity_on_both_sides_agrees() -> None:
    left, right = _compare("기업당 지원한도 5,000만원", "기업당 지원한도 5,000만원")

    assert set(left) == set(right) and left == right


def test_the_same_quantity_with_a_different_number_disagrees() -> None:
    left, right = _compare("기업당 지원한도 5,000만원", "기업당 지원한도 7,000만원")

    assert set(left) == set(right)          # 같은 수량을 말한다
    assert left != right                    # 값이 다르다


def test_a_per_company_cap_and_a_package_total_are_different_quantities() -> None:
    left, right = _compare("기업당 지원한도 5,000만원", "총 지원규모 60억원")

    # 축은 같아도 대상 기준이 달라 같은 자리에서 비교되지 않는다.
    assert set(left).isdisjoint(set(right))


def test_counts_are_withheld_until_the_target_set_is_confirmed() -> None:
    left, right = _compare("연 40개사", "120개사")

    # 개수는 기간뿐 아니라 **어느 집합을 세는지**가 수량의 일부다. 신규 선정과
    # 누적 수혜는 단위·기간이 같아도 다른 수량이다. 지금 인식 어휘에 그 구분을
    # 주는 표현이 없으므로 양쪽 모두 보류한다.
    assert left == {} and right == {}


# --- 맥락이 다른 수치는 한 집합에 들어가지 않는다 ------------------------------------------------


def _shares_a_comparison_set(left: str, right: str) -> bool:
    """두 값이 같은 비교 집합(같은 component·같은 축)에 함께 들어가는가."""

    grouped, invalid, _withheld = _axis_values([
        _ref("f1", left, "component_1"), _ref("f2", right, "component_1")
    ])
    if invalid:
        return False
    return any(
        len(values) > 1 for values in grouped.get("component_1", {}).values()
    )


# 기간 기준이 다른 수치다. 총량과 연간은 양립 가능한데 한 집합에 들어가면 값이
# 다르다는 이유로 모순이 된다.
def test_a_total_and_a_yearly_count_do_not_share_a_comparison_set() -> None:
    assert not _shares_a_comparison_set("총 120개사", "연 40개사")


# 대상 기준이 다른 금액이다. 기업당 한도와 묶음 총액은 양립 가능하다.
def test_a_per_company_cap_and_a_package_total_do_not_share_a_comparison_set() -> None:
    assert not _shares_a_comparison_set("기업당 최대 5,000만원", "총 지원규모 60억원")


# --- 기간 경계 -------------------------------------------------------------
# 금액은 기간을 '확정 필요' 로 두지 않는다. 그러면 '기업당 지원한도' 처럼 기간과
# 무관하게 대응하는 비교까지 막힌다. 대신 키에 넣어 한쪽만 명시된 경우와 서로
# 다른 기간을 갈라낸다.


def test_amounts_with_different_periods_are_not_a_mismatch() -> None:
    left, right = _compare(
        "기업당 연간 지원한도 5,000만원", "기업당 전체기간 지원한도 7,000만원"
    )

    # 같은 자리에 놓이면 값이 달라 NUMERIC_MISMATCH 가 된다. 서로 다른 기준이라
    # 그러면 안 된다.
    assert set(left).isdisjoint(set(right))


def test_an_amount_with_a_period_is_not_compared_to_one_without() -> None:
    left, right = _compare("기업당 연간 지원한도 5,000만원", "기업당 지원한도 5,000만원")

    # 같은 기준이라고 말할 근거가 없다. 숫자가 같아도 같은 자리가 아니다.
    assert set(left).isdisjoint(set(right))


def test_unqualified_repetitions_are_withheld_not_compared() -> None:
    grouped, invalid, withheld = _axis_values([
        _ref("f1", "8회", "component_1"), _ref("f2", "10회", "component_1")
    ])

    # 기간 표현이 없는 횟수끼리는 같은 기준이라는 근거가 없다.
    assert invalid == []
    assert withheld == ["f1", "f2"]
    assert grouped == {}


def test_a_qualified_repetition_is_still_comparable() -> None:
    left, right = _compare("월 2회", "월 3회")

    assert set(left) == set(right) and left != right


# --- 자기비교 ---------------------------------------------------------------
# 좌우가 같은 원문 구간을 인용하면 값은 반드시 같다. 그 일치는 문서가 말한 것이
# 아니라 같은 글자를 두 번 센 결과다.


def _placed(fact_id: str, raw: str, block: str, base: int) -> FitEvidenceRef:
    """같은 블록의 주어진 위치에서 읽은 근거."""

    spans = tuple(
        QuantitySpan(row.axis, row.value, row.start + base, row.end + base, row.modifiers)
        for row in read_quantities(raw)
    )
    return FitEvidenceRef(
        fact_id=fact_id,
        field_name="comparison_profile.support_scale",
        value_raw=raw,
        evidence=[CplEvidence(block, None, None, [])],
        primary_component_id="component_1",
        quantities=spans,
    )


def test_the_same_source_span_on_both_sides_is_excluded() -> None:
    left = [_placed("f1", "기업당 지원한도 5,000만원", "blk:1", 100)]
    right = [_placed("f2", "기업당 지원한도 5,000만원", "blk:1", 100)]

    shared = _shared_spans(left, right)
    assert shared                                   # 같은 자리를 알아본다
    grouped, _invalid, withheld = _axis_values(left, shared)
    assert grouped == {} and withheld == ["f1"]


def test_a_different_fact_id_does_not_unlock_the_same_span() -> None:
    left = [_placed("left_only", "기업당 지원한도 5,000만원", "blk:1", 100)]
    right = [_placed("right_only", "기업당 지원한도 5,000만원", "blk:1", 100)]

    # id 만 다르고 원문 좌표가 같다. id 비교로는 못 막는다.
    assert {ref.fact_id for ref in left} & {ref.fact_id for ref in right} == set()
    assert _shared_spans(left, right)


def test_the_same_text_at_a_different_place_is_still_compared() -> None:
    left = [_placed("f1", "기업당 지원한도 5,000만원", "blk:1", 100)]
    right = [_placed("f2", "기업당 지원한도 5,000만원", "blk:1", 400)]

    assert _shared_spans(left, right) == frozenset()
    grouped, _invalid, withheld = _axis_values(left, _shared_spans(left, right))
    assert withheld == [] and grouped != {}


def test_only_the_shared_number_is_excluded_not_the_whole_occurrence() -> None:
    both = "기업당 한도: 최대 5,000만원 (기업당 단가 7,000만원)"
    left = [_placed("f1", both, "blk:1", 0)]
    # 오른쪽은 같은 블록의 같은 자리에서 앞 숫자만 인용했다.
    right = [_placed("f2", "기업당 한도: 최대 5,000만원", "blk:1", 0)]

    shared = _shared_spans(left, right)
    grouped, _invalid, withheld = _axis_values(left, shared)

    # 겹친 숫자만 빠지고 단가 7,000만원은 남는다.
    assert withheld == []
    values = {value for axes in grouped.values() for values in axes.values() for value in values}
    assert values == {70_000_000}


# --- CPL 파생이 실제 실행 팩을 기준으로 한다 --------------------------------


class _Block:
    def __init__(self, block_id: str, text: str) -> None:
        self.block_id = block_id
        self.text = text


class _Pack:
    def __init__(self, *blocks: _Block) -> None:
        self.blocks = list(blocks)


def _profile_with(value: str, block_id: str, start: int, end: int) -> dict:
    return {
        "comparison_profile": {"support_scale": [{
            "fact_id": "fact_1", "value_raw": value, "status": "identified",
            "value_source": {
                "source_block_id": block_id, "start_char": start, "end_char": end,
            },
        }]},
        "field_states": [{"field_name": "support_scale", "status": "identified"}],
    }


def _scale_facts(result):
    return [
        fact
        for item in result.items
        for subfield in item.subfields
        if subfield.profile_field.endswith("support_scale")
        for fact in subfield.facts
    ]


def test_context_comes_from_the_pack_the_run_actually_used() -> None:
    from worker.cpl import _with_quantities, build_cpl_result

    text = "- 기업당 한도: 최대 5,000만원"
    start = text.index("최대")
    profile = _profile_with("최대 5,000만원", "blk:1", start, len(text))
    result = _with_quantities(
        build_cpl_result(profile), _Pack(_Block("blk:1", text))
    )

    (fact,) = _scale_facts(result)
    (span,) = fact.quantities
    # 값에는 '기업당' 이 없다. 실행 팩의 원문에서만 나온다.
    assert span.dimension("scope") == "기업당"
    assert span.dimension("nature") == "한도"
    # 좌표는 팩 블록 기준 절대값이다.
    assert text[span.start : span.end] == "5,000만원"


def test_without_a_pack_the_values_survive_without_context() -> None:
    from worker.cpl import _with_quantities, build_cpl_result

    profile = _profile_with("최대 5,000만원", "blk:1", 0, 10)
    result = _with_quantities(build_cpl_result(profile), None)

    (fact,) = _scale_facts(result)
    assert fact.value_raw == "최대 5,000만원"     # 값은 그대로
    assert fact.quantities == ()                 # 맥락만 없다


def test_coordinates_that_miss_the_block_are_not_guessed() -> None:
    from worker.cpl import _with_quantities, build_cpl_result

    profile = _profile_with("최대 5,000만원", "blk:1", 0, 9)
    result = _with_quantities(
        build_cpl_result(profile), _Pack(_Block("blk:1", "전혀 다른 원문"))
    )

    (fact,) = _scale_facts(result)
    assert fact.quantities == ()
