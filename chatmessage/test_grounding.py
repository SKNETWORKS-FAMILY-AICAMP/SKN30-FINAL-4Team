"""답변 사후 점검 회귀 검사.

경고는 답변을 막지 않는다. 여기서 보는 것은 "걸려야 할 것이 걸리는가" 다.
"""
from chatmessage.grounding import check_grounding


def context(**models) -> dict:
    return {"report": models}


# 실제 Context 모양 그대로다 — 백분위와 그 출처 정보는 이미 제거돼 있다.
MODEL2 = {
    "model2": {
        "result": {"predicted_amount": 526390144},
        "provenance": {"support_type_compatibility": "known"},
    }
}
MODEL3 = {
    "model3": {
        "result": {"anomaly_level": None, "reference": {"cohort_level": "L0 전체"}}
    }
}


def test_numbers_outside_the_context_are_flagged() -> None:
    assert check_grounding("총 사업비는 999,111,222원입니다.", context(**MODEL2))
    # 콤마를 풀어 쓴 같은 숫자는 새 숫자가 아니다.
    assert not check_grounding("예측액은 526,390,144원입니다.", context(**MODEL2))
    # 두 자리 이하는 순서·개수 표현이라 세지 않는다.
    assert not check_grounding("확인이 필요한 항목은 2개입니다.", context(**MODEL2))


def test_percentile_wording_is_flagged_instead_of_cohort_checking() -> None:
    """백분위 비교군 검사는 없앴다.

    백분위가 Context 에서 제거돼(비노출 정책) 판정할 값이 없다. 대신 백분위를
    입에 올리는 것 자체를 잡는다 — Context 에 없는 값을 말했다는 뜻이다.
    """
    warnings = check_grounding("같은 연구개발 사업 중 75.2 백분위입니다.", context(**MODEL2))
    assert any("내부 진단값" in warning for warning in warnings)


def test_model3_cause_and_level_claims_are_flagged() -> None:
    warnings = check_grounding(
        "이례성은 높은 수준입니다. 지원금액이 가장 큰 원인입니다.", context(**MODEL3)
    )
    assert any("원인을 단정" in warning for warning in warnings)
    assert any("anomaly_level" in warning for warning in warnings)


def test_model3_axis_cause_is_allowed_when_contributions_exist() -> None:
    with_contribution = context(
        model3={"result": {"axis_contribution": {"budget": 0.4}, "anomaly_level": "high"}}
    )
    warnings = check_grounding("지원금액이 가장 큰 원인입니다.", with_contribution)
    assert not any("원인을 단정" in warning for warning in warnings)


def test_clean_answer_produces_no_warnings() -> None:
    assert check_grounding("확인이 필요한 항목이 있습니다.", context(**MODEL2)) == []
