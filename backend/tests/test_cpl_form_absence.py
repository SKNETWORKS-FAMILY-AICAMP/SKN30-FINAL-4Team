"""양식에 칸이 없어서 빈 것과 추출을 놓쳐서 빈 것을 가른다.

판별기준 §11.3 은 "지원조건이 별도로 없다고 해서 자동으로 오류로 판단하지
않는다", §12.3 은 "일부 정보가 없을 경우 지원내용 부적절로 판단하지 않는다"
고 못 박는다. 요청서(서식 1)에 없는 행이 ``not_found`` 로 남으면 항목 전체가
``needs_confirmation`` 으로 내려가 그 원칙을 어긴다.

**라벨이 없다는 사실만으로는 부족하다.** 값이 있으면 값이 이기고, 라벨이
있는데 빈 것은 추출 누락으로 남긴다.
"""

from __future__ import annotations

from typing import Any

from worker.contracts.cpl_result import (
    CONFIRMED,
    NEEDS_CONFIRMATION,
    NOT_APPLICABLE,
    CplFieldCode,
)
from worker.cpl import FORM_SLOT_ABSENT, analyze_cpl
from worker.cpl_coverage import absent_form_fields


_SLOTS = (
    "comparison_profile.applicant_eligibility",
    "comparison_profile.beneficiary",
    "comparison_profile.participation_requirements",
    "comparison_profile.support_methods",
)


class _Dead:
    async def generate_structured(self, **_: Any) -> Any:
        raise RuntimeError("이 시험은 LLM 을 쓰지 않는다")


def _ir(*lines: str) -> dict[str, Any]:
    return {
        "document": {"document_id": "hwpx:d1"},
        "blocks": [
            {
                "block_id": f"hwpx:b{index}",
                "reading_order": index,
                "occurrences": [{"occurrence_id": f"occ:p{index}", "text": text}],
            }
            for index, text in enumerate(lines)
        ],
    }


# 서식 1 의 지원대상·지원조건 구역. 신청자격·수혜자·참여요건·지원방식 행은 없다.
def _request_form() -> dict[str, Any]:
    return _ir(
        "○ (지원대상) 부산광역시 소재 제조업 및 정보통신업 영위 중소기업",
        "○ (지원조건)",
        "- 지역: 부산광역시 내 본사 또는 주된 사업장 보유",
        "- 제외조건: 휴·폐업 기업",
        "○ (지원내용) 시제품 제작비, 공인 시험인증비를 지원",
        "○ (수행기관) 부산테크노파크(주관)",
    )


def _fact(fact_id: str, value: str) -> dict[str, Any]:
    return {"fact_id": fact_id, "value_raw": value, "status": "identified"}


def _profile(**over: Any) -> dict[str, Any]:
    comparison = {
        "support_target": [_fact("f1", "부산광역시 소재 제조업 및 정보통신업 영위 중소기업")],
        "eligibility_conditions": [_fact("f2", "부산광역시 내 본사 또는 주된 사업장 보유")],
        "exclusions": [_fact("f3", "휴·폐업 기업")],
        "support_activities": [_fact("f4", "시제품 제작")],
        "support_items": [_fact("f5", "시제품 제작비")],
        "support_content": [_fact("f6", "공인 시험인증비")],
        "support_scale": [_fact("f7", "120개사")],
        "cost_sharing": [_fact("f8", "20%")],
    }
    comparison.update(over.pop("comparison", {}) or {})
    states = [
        {"field_name": name, "status": "identified"}
        for name in comparison
        if comparison[name]
    ] + [
        {"field_name": name, "status": "not_found"}
        for name in (
            "applicant_eligibility", "beneficiary",
            "participation_requirements", "support_methods",
        )
    ]
    states = over.pop("states", None) or states
    return {"comparison_profile": comparison, "request_context": {}, "field_states": states}


def _item(result, code: CplFieldCode):
    return next(row for row in result.items if row.field_code is code)


def _run(profile: dict[str, Any], common_ir: dict[str, Any] | None):
    return analyze_cpl(
        profile, _Dead(), model_profile="cpl", common_ir=common_ir
    )


# --- 양식 사실 자체 -----------------------------------------------------------


def test_the_request_form_has_no_row_for_the_four_fields() -> None:
    assert absent_form_fields(_request_form()) == frozenset(_SLOTS)


def test_a_document_that_does_carry_the_label_is_not_absent() -> None:
    document = _ir(
        "○ (신청자격) 업력 7년 미만 창업기업",
        "○ (지원방식) 사후 정산 지급",
    )

    absent = absent_form_fields(document)

    assert "comparison_profile.applicant_eligibility" not in absent
    assert "comparison_profile.support_methods" not in absent
    # 나머지 둘은 여전히 없다 — 한 라벨이 다른 필드를 대신하지 않는다.
    assert "comparison_profile.beneficiary" in absent
    assert "comparison_profile.participation_requirements" in absent


# 라벨만 찍히고 내용이 비어도 "칸은 있다". 구역이 비었다는 것과 행이 없다는 것은
# 다른 사실이라 include_empty 로 묻는다.
def test_an_empty_labelled_region_still_counts_as_a_slot() -> None:
    document = _ir("○ (신청자격)", "○ (지원대상) 중소기업")

    assert "comparison_profile.applicant_eligibility" not in absent_form_fields(document)


# 본문 서술에 낱말이 나왔다고 칸이 생기면 안 된다. 라벨 자리에 있어야 한다.
def test_the_word_inside_a_sentence_is_not_a_label() -> None:
    document = _ir("- 제외조건: 최근 3년 내 동일 지원사업 수혜기업")

    assert "comparison_profile.beneficiary" in absent_form_fields(document)


# --- CPL 항목에 미치는 영향 ---------------------------------------------------


def test_an_absent_slot_becomes_not_applicable_and_stops_dragging_the_item() -> None:
    result = _run(_profile(), _request_form())

    target = _item(result, CplFieldCode.TARGET_AND_CONDITIONS)
    by_name = {row.profile_field: row for row in target.subfields}

    assert by_name["comparison_profile.beneficiary"].status == "not_applicable"
    assert FORM_SLOT_ABSENT in by_name["comparison_profile.beneficiary"].reason_codes
    # 확인 + 해당 없음 = 확인됨
    assert target.representative_status == CONFIRMED
    assert _item(result, CplFieldCode.SUPPORT_CONTENT_AND_SCALE).representative_status == CONFIRMED


def test_without_common_ir_nothing_is_promoted() -> None:
    result = _run(_profile(), None)

    target = _item(result, CplFieldCode.TARGET_AND_CONDITIONS)

    assert target.representative_status == NEEDS_CONFIRMATION
    assert all(row.status != "not_applicable" for row in target.subfields)


# 라벨이 없어도 값이 나왔다면 그 값이 사실이다. 없는 칸을 근거 있는 값 위에
# 두지 않는다.
def test_a_value_without_a_label_beats_the_absent_slot() -> None:
    profile = _profile(
        comparison={"applicant_eligibility": [_fact("f9", "ICT분야 중소·벤처기업(법인)")]},
        states=[
            {"field_name": "support_target", "status": "identified"},
            {"field_name": "eligibility_conditions", "status": "identified"},
            {"field_name": "exclusions", "status": "identified"},
            {"field_name": "applicant_eligibility", "status": "identified"},
            {"field_name": "beneficiary", "status": "not_found"},
            {"field_name": "participation_requirements", "status": "not_found"},
        ],
    )

    target = _item(_run(profile, _request_form()), CplFieldCode.TARGET_AND_CONDITIONS)
    by_name = {row.profile_field: row for row in target.subfields}

    assert by_name["comparison_profile.applicant_eligibility"].status == "identified"
    assert [f.value_raw for f in by_name["comparison_profile.applicant_eligibility"].facts] == [
        "ICT분야 중소·벤처기업(법인)"
    ]


# 라벨이 있는데 빈 것은 추출 누락이다. 해당 없음으로 덮으면 사용자가 고칠
# 기회를 잃는다.
def test_a_present_but_empty_slot_is_not_promoted() -> None:
    document = _ir(
        "○ (지원대상) 부산광역시 소재 제조업 및 정보통신업 영위 중소기업",
        "○ (신청자격)",
    )

    target = _item(_run(_profile(), document), CplFieldCode.TARGET_AND_CONDITIONS)
    by_name = {row.profile_field: row for row in target.subfields}

    assert by_name["comparison_profile.applicant_eligibility"].status == "not_found"
    assert target.representative_status == NEEDS_CONFIRMATION


def test_the_promotion_is_recorded_as_a_diagnostic() -> None:
    result = _run(_profile(), _request_form())

    units = {
        row.unit for row in result.diagnostics if row.reason_code == FORM_SLOT_ABSENT
    }

    assert units == set(_SLOTS)
    assert all(
        "라벨 구역이 없다" in row.message
        for row in result.diagnostics
        if row.reason_code == FORM_SLOT_ABSENT
    )


# 감시 대상이 아닌 필드는 라벨이 없어도 건드리지 않는다. 지원대상·지원조건은
# 라벨이 실제로 있는 행이라, 비었다면 그것은 추출 문제다.
def test_watched_fields_do_not_leak_to_other_paths() -> None:
    result = _run(_profile(), _ir("빈 문서"))

    promoted = {
        row.profile_field
        for item in result.items
        for row in item.subfields
        if row.status == "not_applicable"
    }

    assert promoted <= set(_SLOTS)
