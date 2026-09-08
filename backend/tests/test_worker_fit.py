"""Slice 3: 프로파일 → FIT 7관계 테스트. 네트워크·DB 를 타지 않는다.

LLM 포트는 호출을 기록하는 가짜로 대신한다. 여기서 고정하는 것은 두 가지다.

1. 비교가 성립하지 않는 관계는 **호출 자체가 없고** 정보 부족으로 남는가.
2. 하나가 무너져도 나머지 관계와 그 근거가 그대로 남는가 (초안 §9.2.1).
"""

from dataclasses import fields, is_dataclass
from enum import Enum
import json
from pathlib import Path
import re

import pytest

from app.ports.llm_client import (
    LLMInvalidResponseError,
    LLMTimeoutError,
    LLMUnavailableError,
)
from worker.contracts.fit_result import (
    COMPARISON_EVIDENCE_MISSING,
    FIT_DISPLAY_STATUSES,
    FIT_NOT_APPLICABLE,
    COMPARISON_VALUE_INVALID,
    HIERARCHY_COMPARISON_NOT_AVAILABLE,
    LLM_INVALID_RESPONSE,
    LLM_TIMEOUT,
    LLM_UNAVAILABLE,
    NO_CONDITIONS_SPECIFIED,
    NUMERIC_MISMATCH,
    PURPOSE_AXIS_UNRESOLVED,
    SINGLE_SIDED_NO_CONFLICT,
    FitRelationId,
    FitStatus,
    PurposeAxisCode,
    fit_axis_code,
)
from worker.fit import _quantities, analyze_fit


_PROFILE_PATH = (
    Path(__file__).resolve().parents[2]
    / "packages"
    / "profile_structuring"
    / "examples"
    / "request"
    / "structured_profile_v012.json"
)
_MODEL_PROFILE = "test-profile"
_PURPOSE_TASK = "fit_purpose_axis_classification"
_COMPARISON_TASK = "fit_relation_comparison"


def _fresh_profile() -> dict:
    """테스트가 프로파일을 변형하므로 매번 새로 읽는다."""

    return json.loads(_PROFILE_PATH.read_text(encoding="utf-8"))


@pytest.fixture()
def profile() -> dict:
    return _fresh_profile()


# ------------------------------------------------------------- 가짜 LLM 포트


class FakeLLM:
    """호출을 기록하는 오프라인 포트.

    응답은 dict 로 주고 여기서 ``response_schema`` 로 검증한다. 실제 어댑터와
    같은 자리에서 스키마가 걸리게 하려는 것이다. 예외를 주면 그대로 던진다.
    """

    def __init__(self, *, purpose=None, comparison=None):
        self._scripts = {_PURPOSE_TASK: purpose, _COMPARISON_TASK: comparison}
        self.calls: list[tuple[str, dict]] = []

    async def generate_structured(
        self, *, task_name, messages, response_schema, model_profile
    ):
        payload = json.loads(messages[-1].content)
        self.calls.append((task_name, payload))
        script = self._scripts.get(task_name)
        if callable(script):
            script = script(payload)
        if isinstance(script, BaseException):
            raise script
        if script is None:
            raise LLMUnavailableError(f"no scripted response for {task_name}")
        return response_schema.model_validate(script)

    def payloads(self, task_name: str) -> list[dict]:
        return [payload for name, payload in self.calls if name == task_name]

    def relation_ids_asked(self) -> set[str]:
        return {
            row["relation_id"]
            for payload in self.payloads(_COMPARISON_TASK)
            for row in payload["relations"]
        }


def _direction(quoted: str = "사업화 실행력"):
    """목적 fact 하나를 방향 축으로 분류하는 응답."""

    return {
        "assignments": [
            {
                "fact_id": "fact:purpose",
                "axis_code": PurposeAxisCode.DIRECTION.value,
                "quoted_text": quoted,
            }
        ]
    }


def _agree(payload: dict) -> dict:
    """요청된 모든 관계를 근거를 인용해 FIT 으로 돌려주는 응답."""

    return {
        "relations": [
            {
                "relation_id": row["relation_id"],
                "status": FitStatus.FIT.value,
                "reason_code": None,
                "left_fact_ids": [fact["fact_id"] for fact in row["left"]],
                "right_fact_ids": [fact["fact_id"] for fact in row["right"]],
            }
            for row in payload["relations"]
        ]
    }


def _relation(result, relation_id: FitRelationId):
    return next(row for row in result.relations if row.relation_id is relation_id)


def _to_plain(value):
    """dataclass 트리를 dict/list 로 편다 (test_worker_cpl.py 와 같은 방식)."""

    if is_dataclass(value):
        return {f.name: _to_plain(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, list):
        return [_to_plain(row) for row in value]
    if isinstance(value, dict):
        return {key: _to_plain(row) for key, row in value.items()}
    if isinstance(value, Enum):
        return value.value
    return value


def _fact(fact_id, field_name, value_raw, component=None) -> dict:
    """정량 비교 테스트용 최소 fact. 접지 모양은 프로파일과 같게 둔다."""

    row = {
        "fact_id": fact_id,
        "field_name": field_name,
        "value_raw": value_raw,
        "value_source": {
            "source_block_id": "b1",
            "start_char": 0,
            "end_char": len(value_raw),
            "text_basis": "common_ir_v1_candidate_pack",
        },
        "evidence": [
            {
                "source_block_id": "b1",
                "common_ir_document_id": "doc:1",
                "common_ir_block_id": "b1",
                "common_ir_occurrence_ids": ["occ:b1"],
            }
        ],
        "status": "identified",
    }
    if component:
        row["primary_component_id"] = component
    return row


def _quantity_profile(content: list[dict], scale: list[dict]) -> dict:
    """support_content · support_scale 만 있는 프로파일.

    다른 관계는 근거가 없어 게이트에서 걸린다. FIT-7 만 보기 위한 최소 입력이다.
    """

    return {
        "profile_id": "profile:quantity",
        "comparison_profile": {"support_content": content, "support_scale": scale},
        "field_states": [],
    }


# ------------------------------------------------------------------ 7관계 골격


def test_seven_relations_in_enum_order(profile):
    result = analyze_fit(
        profile,
        FakeLLM(purpose=_direction(), comparison=_agree),
        model_profile=_MODEL_PROFILE,
    )
    assert [row.relation_id for row in result.relations] == list(FitRelationId)


def test_lineage_comes_from_processing_metadata(profile):
    result = analyze_fit(
        profile,
        FakeLLM(purpose=_direction(), comparison=_agree),
        model_profile=_MODEL_PROFILE,
    )
    assert result.profile_id == profile["profile_id"]
    assert (
        result.common_ir_document_id
        == profile["processing_metadata"]["common_ir_document_id"]
    )
    assert result.model_profile == _MODEL_PROFILE


def test_worker_does_not_reach_into_the_old_fit_or_cpl_services():
    """초안 §7.1 의 좌우 근거는 프로파일에서 만든다. 옛 서비스 재해석이 아니다."""

    source = (Path(__file__).resolve().parents[1] / "worker" / "fit.py").read_text(
        encoding="utf-8"
    )
    assert not re.search(r"^\s*from app\.services", source, re.MULTILINE)
    assert not re.search(r"^\s*import app\.services", source, re.MULTILINE)


# --------------------------------------------------------------- FIT-4 게이트


def test_fit4_is_always_insufficient_and_never_calls_the_llm(profile):
    """계층 노드가 있어도 비교를 활성화하지 않는다 (초안 §7.1)."""

    assert profile["program_hierarchy"]["nodes"], "전제가 깨졌다: 계층 노드가 비었다"
    client = FakeLLM(purpose=_direction(), comparison=_agree)
    result = analyze_fit(profile, client, model_profile=_MODEL_PROFILE)

    fit4 = _relation(result, FitRelationId.FIT_4)
    assert fit4.status is FitStatus.INSUFFICIENT
    assert fit4.reason_code == HIERARCHY_COMPARISON_NOT_AVAILABLE
    assert fit4.left.facts == [] and fit4.right.facts == []
    assert FitRelationId.FIT_4.value not in client.relation_ids_asked()


# ------------------------------------------------------------- FIT-7 정량 비교


def test_fit7_on_reference_profile_is_single_sided_not_a_conflict(profile):
    """component 가 갈려 한쪽밖에 없다. 충돌이 아니라 비교 불성립이다.

    support_content 는 mentoring 뿐이고 support_scale 은 education·grant 뿐이라
    양쪽이 다 있는 component 가 없다. 여기서 400만원과 150/250만원을 붙이면
    분할 지급 단계액을 거짓 충돌로 만든다.
    """

    client = FakeLLM(purpose=_direction(), comparison=_agree)
    fit7 = _relation(
        analyze_fit(profile, client, model_profile=_MODEL_PROFILE), FitRelationId.FIT_7
    )
    assert fit7.status is FitStatus.INSUFFICIENT
    assert fit7.status is not FitStatus.NEEDS_REVIEW
    assert fit7.reason_code == SINGLE_SIDED_NO_CONFLICT
    assert FitRelationId.FIT_7.value not in client.relation_ids_asked()


def test_fit7_same_component_same_axis_agreeing_values_are_fit():
    client = FakeLLM()
    result = analyze_fit(
        _quantity_profile(
            [_fact("f:c", "support_content", "팀당 400만원", "component:grant")],
            [_fact("f:s", "support_scale", "400만원", "component:grant")],
        ),
        client,
        model_profile=_MODEL_PROFILE,
    )
    fit7 = _relation(result, FitRelationId.FIT_7)
    assert fit7.status is FitStatus.FIT
    assert fit7.reason_code is None
    assert client.calls == []


def test_fit7_same_axis_differing_values_need_review():
    fit7 = _relation(
        analyze_fit(
            _quantity_profile(
                [_fact("f:c", "support_content", "팀당 400만원", "component:grant")],
                [_fact("f:s", "support_scale", "300만원", "component:grant")],
            ),
            FakeLLM(),
            model_profile=_MODEL_PROFILE,
        ),
        FitRelationId.FIT_7,
    )
    assert fit7.status is FitStatus.NEEDS_REVIEW
    assert fit7.reason_code == NUMERIC_MISMATCH


def test_fit7_amount_against_team_count_is_not_compared():
    """금액과 팀수는 같은 축이 아니다. 축이 한쪽에만 있으면 비교가 성립하지 않는다."""

    fit7 = _relation(
        analyze_fit(
            _quantity_profile(
                [_fact("f:c", "support_content", "팀당 400만원", "component:grant")],
                [_fact("f:s", "support_scale", "4개팀", "component:grant")],
            ),
            FakeLLM(),
            model_profile=_MODEL_PROFILE,
        ),
        FitRelationId.FIT_7,
    )
    assert fit7.status is FitStatus.INSUFFICIENT
    assert fit7.reason_code == SINGLE_SIDED_NO_CONFLICT


def test_fit7_never_compares_across_components():
    """값도 축도 같지만 component 가 다르면 붙이지 않는다."""

    fit7 = _relation(
        analyze_fit(
            _quantity_profile(
                [_fact("f:c", "support_content", "400만원", "component:mentoring")],
                [_fact("f:s", "support_scale", "400만원", "component:grant")],
            ),
            FakeLLM(),
            model_profile=_MODEL_PROFILE,
        ),
        FitRelationId.FIT_7,
    )
    assert fit7.status is FitStatus.INSUFFICIENT
    assert fit7.reason_code == SINGLE_SIDED_NO_CONFLICT


def test_fit7_unnormalisable_values_are_excluded_not_guessed():
    """정량 값을 인식하지 못하면 비교에서 빼고 경고로 남긴다. 추측하지 않는다."""

    fit7 = _relation(
        analyze_fit(
            _quantity_profile(
                [_fact("f:c", "support_content", "전액 지원", "component:grant")],
                [_fact("f:s", "support_scale", "일부 지원", "component:grant")],
            ),
            FakeLLM(),
            model_profile=_MODEL_PROFILE,
        ),
        FitRelationId.FIT_7,
    )
    assert fit7.status is FitStatus.INSUFFICIENT
    assert fit7.reason_code == COMPARISON_VALUE_INVALID
    assert {(row.unit, row.reason_code) for row in fit7.diagnostics} == {
        ("f:c", COMPARISON_VALUE_INVALID),
        ("f:s", COMPARISON_VALUE_INVALID),
    }


def test_fit7_ignores_total_budget_and_cost_sharing(profile):
    """총사업비·직접 지원금을 자동 비교하지 않는다 (초안 §7.1)."""

    fit7 = _relation(
        analyze_fit(
            profile,
            FakeLLM(purpose=_direction(), comparison=_agree),
            model_profile=_MODEL_PROFILE,
        ),
        FitRelationId.FIT_7,
    )
    used = {ref.field_name for ref in (*fit7.left.facts, *fit7.right.facts)}
    assert used == {"support_content", "support_scale"}


# --------------------------------------------------- FIT-7 숫자 시퀀스 소비

# 세 갈래로 나눠 고정한다.
# 1. 지원하는 순수 표현: 매치가 숫자를 전부 소비한다.
# 2. 부분 해석 위험: 숫자 일부만 소비되면 값 전체를 버린다. 여기서 값을
#    만들어내는 것이 이 결함의 본체였다 (10~20개사 → 20).
# 3. 숫자 외 설명이 붙은 표현: **허용** 한다. 규칙은 문자열 전체 소비가
#    아니라 숫자 시퀀스 소비다.


@pytest.mark.parametrize(
    "value_raw, expected",
    [
        ("20개팀", {("COUNT:팀", 20)}),
        ("최종 선정 4개팀", {("COUNT:팀", 4)}),
        ("팀당 400만원", {("AMOUNT_KRW", 4_000_000)}),
        ("150만원", {("AMOUNT_KRW", 1_500_000)}),
        ("250만원", {("AMOUNT_KRW", 2_500_000)}),
        ("월 2회, 총 8회", {("TIMES:월", 2), ("TIMES:TOTAL", 8)}),
        ("21,000천원", {("AMOUNT_KRW", 21_000_000)}),
        ("1.5억원", {("AMOUNT_KRW", 150_000_000)}),
    ],
)
def test_fit7_reads_supported_quantity_expressions(value_raw, expected):
    assert _quantities(value_raw) == expected


@pytest.mark.parametrize(
    "value_raw",
    ["10~20개사", "1억 5000만원", "최대 5천만원", "2027년 중 사업자등록"],
)
def test_fit7_discards_values_whose_numbers_are_only_partly_consumed(value_raw):
    """숫자 문법을 일부만 소비해 다른 값을 만들지 않는다."""

    assert _quantities(value_raw) == set()


def test_fit7_allows_prose_around_a_fully_consumed_number():
    """숫자 외 설명 문구는 무방하다. 문자열 전체를 소비할 필요는 없다."""

    assert _quantities("기업당 400만원 지원") == {("AMOUNT_KRW", 4_000_000)}


@pytest.mark.parametrize(
    "left_raw, right_raw",
    [("10~20개사", "20개사"), ("1억 5000만원", "5000만원")],
)
def test_fit7_partially_read_values_are_never_reported_as_fit(left_raw, right_raw):
    """부분 해석이 만든 값으로 서로 다른 값을 일치라고 판정하지 않는다."""

    fit7 = _relation(
        analyze_fit(
            _quantity_profile(
                [_fact("f:c", "support_content", left_raw, "component:grant")],
                [_fact("f:s", "support_scale", right_raw, "component:grant")],
            ),
            FakeLLM(),
            model_profile=_MODEL_PROFILE,
        ),
        FitRelationId.FIT_7,
    )
    assert fit7.status is not FitStatus.FIT
    assert fit7.status is FitStatus.INSUFFICIENT
    assert fit7.reason_code == COMPARISON_VALUE_INVALID


# ---------------------------------------------------------- 목적 의미 축 보완


def test_purpose_axis_is_classified_once_for_the_whole_document(profile):
    """세 관계가 축을 필요로 해도 문서당 한 번이다 (초안 §9.2)."""

    client = FakeLLM(purpose=_direction(), comparison=_agree)
    result = analyze_fit(profile, client, model_profile=_MODEL_PROFILE)

    assert len(client.payloads(_PURPOSE_TASK)) == 1
    assert result.purpose_axis.attempted is True
    assert [row.axis_code for row in result.purpose_axis.assignments] == [
        PurposeAxisCode.DIRECTION.value
    ]
    for relation_id in (FitRelationId.FIT_2, FitRelationId.FIT_3):
        row = _relation(result, relation_id)
        assert row.status is FitStatus.FIT
        assert [ref.value_raw for ref in row.left.facts] == ["사업화 실행력"]
    assert {FitRelationId.FIT_2.value, FitRelationId.FIT_3.value} <= (
        client.relation_ids_asked()
    )


def test_quoted_text_outside_the_source_value_is_dropped(profile):
    """모델이 원문에 없는 문장을 인용하면 축을 만들지 않는다."""

    client = FakeLLM(purpose=_direction("존재하지 않는 인용문"), comparison=_agree)
    result = analyze_fit(profile, client, model_profile=_MODEL_PROFILE)

    assert result.purpose_axis.assignments == []
    assert result.purpose_axis.dropped == ["fact:purpose"]
    for relation_id in (FitRelationId.FIT_2, FitRelationId.FIT_3):
        row = _relation(result, relation_id)
        assert row.status is FitStatus.INSUFFICIENT
        assert row.reason_code == PURPOSE_AXIS_UNRESOLVED
        assert row.left.facts == []
    assert FitRelationId.FIT_2.value not in client.relation_ids_asked()


def test_purpose_axis_call_failure_does_not_touch_other_relations(profile):
    client = FakeLLM(purpose=LLMTimeoutError("purpose timed out"), comparison=_agree)
    result = analyze_fit(profile, client, model_profile=_MODEL_PROFILE)

    assert result.purpose_axis.reason_code == LLM_TIMEOUT
    assert _relation(result, FitRelationId.FIT_1).reason_code == PURPOSE_AXIS_UNRESOLVED
    assert _relation(result, FitRelationId.FIT_5).status is FitStatus.FIT
    assert _relation(result, FitRelationId.FIT_6).status is FitStatus.FIT


def test_fit1_without_a_target_condition_axis_does_not_fall_back_to_the_whole_purpose(
    profile,
):
    """목적 전체를 대상조건으로 취급하지 않는다 (초안 §7.1 FIT-1)."""

    client = FakeLLM(purpose=_direction(), comparison=_agree)
    result = analyze_fit(profile, client, model_profile=_MODEL_PROFILE)

    fit1 = _relation(result, FitRelationId.FIT_1)
    assert fit1.status is FitStatus.INSUFFICIENT
    assert fit1.reason_code == PURPOSE_AXIS_UNRESOLVED
    assert fit1.left.facts == []
    whole_purpose = profile["comparison_profile"]["purpose_goal"][0]["value_raw"]
    assert whole_purpose not in [ref.value_raw for ref in fit1.left.facts]
    assert FitRelationId.FIT_1.value not in client.relation_ids_asked()
    # 우측(실제 지원 대상)은 지워지지 않는다.
    assert {ref.fact_id for ref in fit1.right.facts} == {
        "fact:target",
        "fact:beneficiary_education",
        "fact:beneficiary_grant",
    }


# -------------------------------------------------- FIT-5 부재와 실패의 구분


def test_fit5_reaches_the_llm_when_one_condition_field_is_identified(profile):
    """참가요건 1건이 있으므로 비교가 성립한다."""

    client = FakeLLM(purpose=_direction(), comparison=_agree)
    result = analyze_fit(profile, client, model_profile=_MODEL_PROFILE)

    fit5 = _relation(result, FitRelationId.FIT_5)
    assert fit5.status is FitStatus.FIT
    assert FitRelationId.FIT_5.value in client.relation_ids_asked()
    assert [ref.fact_id for ref in fit5.right.facts] == ["fact:participation"]


def _set_state(profile: dict, field_name: str, status: str) -> None:
    for row in profile["field_states"]:
        if row["field_name"] == field_name:
            row["status"] = status
            return
    raise AssertionError(f"전제가 깨졌다: field_states 에 {field_name} 이 없다")


def test_fit5_all_conditions_not_found_is_absence_not_failure(profile):
    profile["comparison_profile"]["participation_requirements"] = []
    _set_state(profile, "participation_requirements", "not_found")

    client = FakeLLM(purpose=_direction(), comparison=_agree)
    fit5 = _relation(
        analyze_fit(profile, client, model_profile=_MODEL_PROFILE), FitRelationId.FIT_5
    )
    assert fit5.status is FitStatus.INSUFFICIENT
    assert fit5.reason_code == NO_CONDITIONS_SPECIFIED
    assert FitRelationId.FIT_5.value not in client.relation_ids_asked()


def test_fit5_extraction_failure_is_not_reported_as_absence(profile):
    profile["comparison_profile"]["participation_requirements"] = []
    _set_state(profile, "participation_requirements", "not_found")
    _set_state(profile, "eligibility_conditions", "extraction_failed")

    client = FakeLLM(purpose=_direction(), comparison=_agree)
    fit5 = _relation(
        analyze_fit(profile, client, model_profile=_MODEL_PROFILE), FitRelationId.FIT_5
    )
    assert fit5.status is FitStatus.INSUFFICIENT
    assert fit5.reason_code == COMPARISON_EVIDENCE_MISSING
    assert FitRelationId.FIT_5.value not in client.relation_ids_asked()


# ------------------------------------------------------------- FIT-6 수행체계


def test_fit6_uses_the_actual_delivery_relation_grounding(profile):
    """actions 가 비고 delivery_methods 가 없는 것은 모순이 아니다 (초안 §7.1)."""

    assert profile["comparison_profile"]["delivery_methods"] == []
    client = FakeLLM(purpose=_direction(), comparison=_agree)
    fit6 = _relation(
        analyze_fit(profile, client, model_profile=_MODEL_PROFILE), FitRelationId.FIT_6
    )
    assert fit6.status is not FitStatus.CONFLICT
    assert [ref.fact_id for ref in fit6.left.facts] == ["delivery:center_lead.actor"]
    assert [ref.fact_id for ref in fit6.right.facts] == ["delivery:center_lead.role"]
    assert fit6.left.facts[0].value_raw == "가상 동구청년창업지원센터"
    assert fit6.left.facts[0].evidence, "actor 근거 접지가 비면 안 된다"


# ---------------------------------------------------------------- 응답 격리


def test_a_bogus_evidence_ref_degrades_only_that_relation(profile):
    """한 관계의 접지 오류가 다른 관계 결과를 지우지 않는다 (초안 §9.2.1)."""

    def comparison(payload):
        rows = _agree(payload)
        for row in rows["relations"]:
            if row["relation_id"] == FitRelationId.FIT_5.value:
                row["left_fact_ids"] = ["fact:does-not-exist"]
        return rows

    client = FakeLLM(purpose=_direction(), comparison=comparison)
    result = analyze_fit(profile, client, model_profile=_MODEL_PROFILE)

    fit5 = _relation(result, FitRelationId.FIT_5)
    assert fit5.status is FitStatus.INSUFFICIENT
    assert fit5.reason_code == LLM_INVALID_RESPONSE
    assert fit5.left.facts, "실패한 관계라도 서버가 만든 근거는 남는다"
    for relation_id in (FitRelationId.FIT_2, FitRelationId.FIT_3, FitRelationId.FIT_6):
        assert _relation(result, relation_id).status is FitStatus.FIT


def test_a_missing_relation_in_the_response_degrades_only_that_relation(profile):
    def comparison(payload):
        rows = _agree(payload)
        rows["relations"] = [
            row
            for row in rows["relations"]
            if row["relation_id"] != FitRelationId.FIT_6.value
        ]
        return rows

    result = analyze_fit(
        profile,
        FakeLLM(purpose=_direction(), comparison=comparison),
        model_profile=_MODEL_PROFILE,
    )
    assert _relation(result, FitRelationId.FIT_6).reason_code == LLM_INVALID_RESPONSE
    assert _relation(result, FitRelationId.FIT_5).status is FitStatus.FIT
    assert _relation(result, FitRelationId.FIT_2).status is FitStatus.FIT


def test_an_unparseable_response_lowers_only_the_pending_relations(profile):
    """관계 id 를 하나도 식별할 수 없을 때만 남은 관계 전체가 내려간다."""

    client = FakeLLM(
        purpose=_direction(),
        comparison=LLMInvalidResponseError("top-level JSON is not parseable"),
    )
    result = analyze_fit(profile, client, model_profile=_MODEL_PROFILE)

    for relation_id in (FitRelationId.FIT_2, FitRelationId.FIT_3, FitRelationId.FIT_5, FitRelationId.FIT_6):
        row = _relation(result, relation_id)
        assert row.status is FitStatus.INSUFFICIENT
        assert row.reason_code == LLM_INVALID_RESPONSE

    # LLM 을 타지 않은 두 관계는 그대로다.
    fit4 = _relation(result, FitRelationId.FIT_4)
    assert fit4.reason_code == HIERARCHY_COMPARISON_NOT_AVAILABLE
    fit7 = _relation(result, FitRelationId.FIT_7)
    assert fit7.reason_code == SINGLE_SIDED_NO_CONFLICT


def test_timeout_maps_to_llm_timeout_and_keeps_computed_results(profile):
    client = FakeLLM(purpose=_direction(), comparison=LLMTimeoutError("timed out"))
    result = analyze_fit(profile, client, model_profile=_MODEL_PROFILE)

    assert _relation(result, FitRelationId.FIT_5).reason_code == LLM_TIMEOUT
    assert _relation(result, FitRelationId.FIT_6).reason_code == LLM_TIMEOUT
    # 게이트에서 이미 확정된 관계와 Rule 관계는 타임아웃에 지워지지 않는다.
    assert _relation(result, FitRelationId.FIT_1).reason_code == PURPOSE_AXIS_UNRESOLVED
    assert (
        _relation(result, FitRelationId.FIT_4).reason_code
        == HIERARCHY_COMPARISON_NOT_AVAILABLE
    )
    assert _relation(result, FitRelationId.FIT_7).reason_code == SINGLE_SIDED_NO_CONFLICT


# -------------------------------------------------------- 근거 인용 강제


def _cite(payload, relation_id: FitRelationId, **overrides):
    """한 관계의 인용 목록만 바꾼 응답."""

    rows = _agree(payload)
    for row in rows["relations"]:
        if row["relation_id"] == relation_id.value:
            row.update(overrides)
    return rows


def test_a_verdict_that_cites_no_evidence_is_not_accepted(profile):
    """판정을 내렸는데 근거를 하나도 인용하지 않았다면 판정이 아니다."""

    result = analyze_fit(
        profile,
        FakeLLM(
            purpose=_direction(),
            comparison=lambda payload: _cite(
                payload,
                FitRelationId.FIT_6,
                status=FitStatus.CONFLICT.value,
                left_fact_ids=[],
                right_fact_ids=[],
            ),
        ),
        model_profile=_MODEL_PROFILE,
    )

    fit6 = _relation(result, FitRelationId.FIT_6)
    assert fit6.status is not FitStatus.CONFLICT
    assert fit6.status is FitStatus.INSUFFICIENT
    assert fit6.reason_code == LLM_INVALID_RESPONSE
    assert any(
        row.unit == FitRelationId.FIT_6.value and "근거" in row.message
        for row in result.diagnostics
    )
    # 나머지 관계는 그대로다.
    assert _relation(result, FitRelationId.FIT_5).status is FitStatus.FIT


def test_an_insufficient_verdict_may_cite_nothing(profile):
    """정보 부족은 인용할 근거가 없다는 뜻이므로 강제 대상이 아니다."""

    fit6 = _relation(
        analyze_fit(
            profile,
            FakeLLM(
                purpose=_direction(),
                comparison=lambda payload: _cite(
                    payload,
                    FitRelationId.FIT_6,
                    status=FitStatus.INSUFFICIENT.value,
                    left_fact_ids=[],
                    right_fact_ids=[],
                ),
            ),
            model_profile=_MODEL_PROFILE,
        ),
        FitRelationId.FIT_6,
    )
    assert fit6.status is FitStatus.INSUFFICIENT
    assert fit6.used_left_fact_ids == []


def test_used_fact_ids_are_kept_apart_from_the_input_evidence(profile):
    """모델이 실제로 쓴 id 를 입력 근거 전체와 구분해 보존한다."""

    fit5 = _relation(
        analyze_fit(
            profile,
            FakeLLM(
                purpose=_direction(),
                comparison=lambda payload: _cite(
                    payload,
                    FitRelationId.FIT_5,
                    left_fact_ids=["fact:target"],
                    right_fact_ids=["fact:participation"],
                ),
            ),
            model_profile=_MODEL_PROFILE,
        ),
        FitRelationId.FIT_5,
    )
    assert fit5.status is FitStatus.FIT
    assert fit5.used_left_fact_ids == ["fact:target"]
    assert fit5.used_right_fact_ids == ["fact:participation"]
    assert len(fit5.left.facts) > len(fit5.used_left_fact_ids), (
        "입력 근거 전체와 인용된 id 가 구분되어야 한다"
    )


# ------------------------------------------------------------------ 점수 금지


def test_serialised_result_carries_no_score_like_field(profile):
    """초안 §6.1·§7.2 가 금지한 집계 자리를 계약에 만들지 않는다."""

    result = analyze_fit(
        profile,
        FakeLLM(purpose=_direction(), comparison=_agree),
        model_profile=_MODEL_PROFILE,
    )
    banned = re.compile(r"score|percent|ratio|grade|confidence", re.IGNORECASE)

    def walk(node, path=""):
        if isinstance(node, dict):
            for key, value in node.items():
                assert not banned.search(key), f"{path}.{key} 는 점수형 필드다"
                walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{path}[{index}]")

    walk(_to_plain(result))


def test_out_of_contract_exception_still_preserves_the_rule_relations(profile):
    """포트 계약 밖 예외가 분석 전체를 죽이지 않는지 본다.

    LLMClient 는 세 가지 예외만 약속한다. 어댑터 버그나 매핑 누락으로 그 밖의
    예외가 올라오면, 막지 않을 경우 LLM 을 타지도 않는 FIT-4·FIT-7 의 Rule
    결과까지 함께 사라진다. 초안 §9.4 의 정상 결과 보존 위반이다.
    """

    class OutOfContractLLM:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def generate_structured(self, *, task_name, **_):
            self.calls.append(task_name)
            raise RuntimeError("포트가 약속하지 않은 예외")

    client = OutOfContractLLM()
    result = analyze_fit(profile, client, model_profile="structuring")

    assert len(result.relations) == 7

    # LLM 을 타지 않는 두 관계는 원래 판정을 그대로 유지한다.
    fit4 = _relation(result, FitRelationId.FIT_4)
    assert fit4.reason_code == HIERARCHY_COMPARISON_NOT_AVAILABLE
    fit7 = _relation(result, FitRelationId.FIT_7)
    assert fit7.reason_code == SINGLE_SIDED_NO_CONFLICT

    # LLM 을 타는 관계만 사용 불가로 내려간다. 원인을 안다고 주장하지 않는다.
    for relation_id in (FitRelationId.FIT_5, FitRelationId.FIT_6):
        degraded = _relation(result, relation_id)
        assert degraded.status is FitStatus.INSUFFICIENT
        assert degraded.reason_code == LLM_UNAVAILABLE

    # 목적 축은 세 관계가 필요해도 문서당 한 번만 시도한다.
    assert client.calls.count("fit_purpose_axis_classification") == 1


@pytest.mark.parametrize(
    "value_raw, expected_won",
    [
        ("0.29억원", 29_000_000),
        ("0.57억원", 57_000_000),
        ("2.03억원", 203_000_000),
        ("1.5억원", 150_000_000),
        ("2,900만원", 29_000_000),
    ],
)
def test_decimal_amounts_do_not_lose_a_won(value_raw, expected_won):
    """소수 금액을 float 로 계산하면 1 원씩 깎여 같은 금액이 달라진다."""

    assert _quantities(value_raw) == {("AMOUNT_KRW", expected_won)}


def test_the_same_amount_written_two_ways_compares_equal():
    """0.29억원과 2,900만원은 같은 금액이다. 표기 차이가 불일치가 되면 안 된다."""

    assert _quantities("0.29억원") == _quantities("2,900만원")


def test_sub_won_amounts_are_discarded_rather_than_rounded():
    """원 단위로 떨어지지 않으면 반올림 방향을 추측하지 않고 버린다."""

    # 1.005천원 은 정확히 1,005 원이라 버릴 이유가 없다.
    assert _quantities("1.005천원") == {("AMOUNT_KRW", 1005)}
    # 1.0005천원 은 1,000.5 원이라 원 단위로 떨어지지 않는다.
    assert _quantities("1.0005천원") == set()


@pytest.mark.parametrize(
    "value_raw",
    ["1,2,3만원", "12,34만원", "1,00만원", "1,,000원", "1,2,3개팀"],
)
def test_malformed_comma_grouping_is_rejected(value_raw):
    """쉼표를 그냥 지우면 잘못된 표기가 유효한 숫자가 된다.

    "1,2,3만원" 이 1,230,000 원이 되던 결함이다. 숫자 일부만 소비하는 것과
    같은 계열이라 값 전체를 버린다.
    """

    assert _quantities(value_raw) == set()


@pytest.mark.parametrize(
    "value_raw, expected",
    [
        ("21,000천원", ("AMOUNT_KRW", 21_000_000)),
        ("1,000,000원", ("AMOUNT_KRW", 1_000_000)),
        ("1,500명", ("COUNT:명", 1500)),
    ],
)
def test_well_formed_comma_grouping_survives(value_raw, expected):
    """세 자리 묶음은 정상이므로 계속 비교 대상이다."""

    assert _quantities(value_raw) == {expected}


# ------------------------------------------------------------- 출력 어휘


def test_relation_ids_are_zero_padded_on_output():
    """프론트 계약은 ``FIT-07`` 로 받는다. 내부 id 는 한 자리 그대로다."""

    assert [fit_axis_code(relation) for relation in FitRelationId] == [
        f"FIT-{n:02d}" for n in range(1, 8)
    ]
    assert fit_axis_code(FitRelationId.FIT_7) == "FIT-07"
    assert FitRelationId.FIT_7.value == "FIT-7"


def test_display_status_vocabulary_is_the_five_the_front_end_renders():
    assert FIT_DISPLAY_STATUSES == {
        "FIT",
        "NEEDS_REVIEW",
        "CONFLICT",
        "INSUFFICIENT",
        FIT_NOT_APPLICABLE,
    }
    # 다섯째 값은 출력 경계에만 있다. 옛 API 표면의 enum 은 그대로 넷이다.
    assert FIT_NOT_APPLICABLE not in {status.value for status in FitStatus}


def test_every_relation_status_is_renderable_and_none_is_not_applicable(profile):
    """지금 ``NOT_APPLICABLE`` 을 만들어내는 판정 경로는 없다.

    FIT-4 는 항상 ``INSUFFICIENT / HIERARCHY_COMPARISON_NOT_AVAILABLE`` 인데
    그것은 기준 미확보이지 비교축 미적용이 아니다 (AGENTS.md). 조용히
    ``해당 없음`` 으로 바꾸면 "표본을 아직 못 구했다" 가 "이 문서엔 이 축이
    없다" 로 둔갑한다.
    """

    result = analyze_fit(
        profile,
        FakeLLM(purpose=_direction(), comparison=_agree),
        model_profile=_MODEL_PROFILE,
    )
    statuses = {relation.status.value for relation in result.relations}
    assert statuses <= FIT_DISPLAY_STATUSES
    assert FIT_NOT_APPLICABLE not in statuses
