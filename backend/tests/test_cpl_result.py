from __future__ import annotations

import pytest

from worker.contracts.cpl_result import (
    CONFIRMED,
    NEEDS_CONFIRMATION,
    NO_CONTENT,
    NOT_APPLICABLE,
    CplFieldCode,
    aggregate_display,
    display_status,
)

from worker.cpl import build_cpl_result


def test_not_found_remains_no_content_at_subfield_boundary() -> None:
    assert display_status("not_found") == NO_CONTENT


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        ([NO_CONTENT], NO_CONTENT),
        ([NO_CONTENT, NO_CONTENT], NO_CONTENT),
        ([CONFIRMED, NO_CONTENT], NEEDS_CONFIRMATION),
        ([NOT_APPLICABLE, NO_CONTENT], NEEDS_CONFIRMATION),
        ([NO_CONTENT, NEEDS_CONFIRMATION], NEEDS_CONFIRMATION),
        ([CONFIRMED, NOT_APPLICABLE], CONFIRMED),
        ([NOT_APPLICABLE, NOT_APPLICABLE], NOT_APPLICABLE),
        ([CONFIRMED, CONFIRMED], CONFIRMED),
    ],
)
def test_mixed_no_content_does_not_hide_other_subfields(
    statuses: list[str], expected: str
) -> None:
    assert aggregate_display(statuses) == expected


# ---------------------------------------------------------------- 항목 단위

# 규칙이 단위 함수에서만 맞고 13항목 배치에서 어긋나면 화면이 거짓말을 한다.
# 혼합 상태가 실제로 생기는 항목을 프로파일에서 만들어 함께 고정한다.


def _profile(**containers: object) -> dict[str, object]:
    comparison = dict(containers.pop("comparison", {}) or {})
    return {
        "comparison_profile": comparison,
        "request_context": dict(containers.pop("context", {}) or {}),
        "field_states": list(containers.pop("states", []) or []),
        **containers,
    }


def _fact(fact_id: str, value: str) -> dict[str, object]:
    return {"fact_id": fact_id, "value_raw": value, "status": "identified"}


def _item(profile: dict[str, object], code: CplFieldCode):
    return next(
        row for row in build_cpl_result(profile).items if row.field_code is code
    )


# 하위 셋 중 둘의 상태를 모르고 하나만 부재다. 문서에 계획이 없다고 단정할
# 근거가 아니므로 항목 전체를 내용 없음으로 낮추지 않는다.
def test_implementation_plan_with_unknown_subfields_is_not_no_content() -> None:
    item = _item(_profile(), CplFieldCode.IMPLEMENTATION_PLAN)

    assert [row.status for row in item.subfields] == [None, "not_found", None]
    assert item.representative_status == NEEDS_CONFIRMATION


# 지원대상·조건·제외조건이 확인됐는데 비어 있는 하위 필드 때문에 항목 전체가
# "내용 없음" 으로 보이면 안 된다. 실문서 6런에서 실제로 나오는 조합이다.
def test_target_and_conditions_keeps_confirmed_subfields_visible() -> None:
    profile = _profile(
        comparison={
            "support_target": [_fact("f1", "부산광역시 소재 중소기업")],
            "eligibility_conditions": [_fact("f2", "업력 3년 이상 10년 이내")],
            "exclusions": [_fact("f3", "휴·폐업 기업")],
        },
        states=[
            {"field_name": "support_target", "status": "identified"},
            {"field_name": "eligibility_conditions", "status": "identified"},
            {"field_name": "exclusions", "status": "identified"},
            {"field_name": "applicant_eligibility", "status": "not_found"},
            {"field_name": "beneficiary", "status": "not_found"},
            {"field_name": "participation_requirements", "status": "not_found"},
        ],
    )

    item = _item(profile, CplFieldCode.TARGET_AND_CONDITIONS)

    assert sorted({row.status for row in item.subfields}) == ["identified", "not_found"]
    assert item.representative_status == NEEDS_CONFIRMATION


# 반대 방향도 고정한다: 하위가 전부 비면 그때는 내용 없음이 맞다.
def test_all_empty_subfields_still_report_no_content() -> None:
    profile = _profile(
        states=[
            {"field_name": name, "status": "not_found"}
            for name in (
                "applicant_eligibility", "support_target", "beneficiary",
                "eligibility_conditions", "exclusions", "participation_requirements",
            )
        ]
    )

    assert _item(profile, CplFieldCode.TARGET_AND_CONDITIONS).representative_status == NO_CONTENT
