"""Slice 4a: 프로파일 → 공통 SIM 비교 프로파일 → 4축 판정 테스트.

네트워크·DB·임베딩을 타지 않는다. LLM 포트는 호출을 기록하는 가짜다.

여기서 고정하는 것은 다섯 가지다.

1. 두 실제 프로파일이 같은 변환표로 옮겨지고, 공고 v0.2 에 없는 전달체계는
   **없는 채로** 남는가 (빈 컨테이너를 자리표시자로 메우지 않는가).
2. 근거가 없는 축은 **호출 자체가 없고** 정보 부족으로 남는가. 게이트에 걸린
   축이 ``DIFFERENT`` 로 새지 않는가 (초안 §7.2).
3. 제외 계약(중복제한·지원규모·미매핑 필드)이 지켜지는가.
4. 접지를 통과하지 못한 LLM 배정이 공통 프로파일에 들어가지 않는가.
5. 점수가 ``internal_ranking`` 밖으로 새지 않는가 (초안 §7.2).
"""

from dataclasses import asdict, fields, replace
import json
from pathlib import Path

import pytest

from app.ports.llm_client import LLMTimeoutError, LLMUnavailableError
from worker.contracts.sim_result import (
    CANDIDATE_EVIDENCE_MISSING,
    DUPLICATE_SPAN_ASSIGNMENT,
    LLM_INVALID_RESPONSE,
    LLM_TIMEOUT,
    LLM_UNAVAILABLE,
    PURPOSE_KEYS,
    REQUEST_EVIDENCE_MISSING,
    CLASSIFICATION_PARTIAL,
    STRUCTURING_COMPLETED,
    STRUCTURING_FAILED,
    STRUCTURING_INCOMPLETE,
    SimAxis,
    SimAxisResult,
    SimReviewGrade,
    SimStatus,
)
from worker.sim import (
    SIM_SCORING_VERSION,
    _AXIS_WEIGHTS,
    _FOCUS_REVIEW_MIN,
    _GENERAL_REVIEW_MIN,
    _STATUS_SCORES,
    compare_candidate,
    compare_candidates,
)
from worker.sim_inputs import build_common_profile


_ROOT = Path(__file__).resolve().parents[2]
_REQUEST_PATH = (
    _ROOT
    / "packages"
    / "profile_structuring"
    / "examples"
    / "request"
    / "structured_profile_v012.json"
)
_EXISTING_PATH = (
    _ROOT
    / "packages"
    / "profile_structuring"
    / "examples"
    / "existing"
    / "structured_profile_v02.json"
)
_SCORING_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "sim_scoring.json"

_MODEL_PROFILE = "test-profile"
_CLASSIFY_TASK = "sim_common_key_classification"
_COMPARISON_TASK = "sim_axis_comparison"

# 이번 슬라이스가 네 컨테이너로 옮기지 않기로 한 필드 (초안 §7.2 / 지시서).
_DEFERRED_FIELDS = (
    "total_budget",
    "cost_sharing",
    "payment_terms",
    "program_period",
    "support_period",
    "support_content",
    "participation_requirements",
)


@pytest.fixture()
def request_profile() -> dict:
    return json.loads(_REQUEST_PATH.read_text(encoding="utf-8"))


@pytest.fixture()
def existing_profile() -> dict:
    return json.loads(_EXISTING_PATH.read_text(encoding="utf-8"))


# ------------------------------------------------------------- 가짜 LLM 포트


class FakeLLM:
    """호출을 기록하는 오프라인 포트 (test_worker_fit.py 와 같은 모양).

    응답은 dict 로 주고 여기서 ``response_schema`` 로 검증한다. 실제 어댑터와
    같은 자리에서 스키마가 걸리게 하려는 것이다. 예외를 주면 그대로 던진다.
    """

    def __init__(self, **scripts):
        self._scripts = {
            _CLASSIFY_TASK: scripts.get("classify"),
            _COMPARISON_TASK: scripts.get("comparison"),
        }
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
            return response_schema.model_validate({})
        return response_schema.model_validate(script)

    def payloads(self, task_name: str) -> list[dict]:
        return [payload for name, payload in self.calls if name == task_name]


def first_key_classifier(payload: dict) -> dict:
    """모든 fact 를 자기 컨테이너의 첫 하위 키에 접지시켜 배정한다."""

    return {
        "assignments": [
            {
                "fact_id": fact["fact_id"],
                "common_key": fact["allowed_common_keys"][0],
                "quoted_text": fact["value_raw"][:6],
            }
            for fact in payload["facts"]
        ]
    }


def verdict_script(status: str, *, reason: str | None = None):
    """요청된 축마다 같은 상태를 돌려주는 스크립트."""

    def build(payload: dict) -> dict:
        return {
            "axes": [
                {
                    "axis": axis["axis"],
                    "status": status,
                    "reason_code": reason,
                    "request_fact_ids": [row["fact_id"] for row in axis["request"][:1]],
                    "candidate_fact_ids": [
                        row["fact_id"] for row in axis["candidate"][:1]
                    ],
                    "common_points": ["같은 대상 표현"],
                    "differences": ["규모가 다르다"],
                }
                for axis in payload["axes"]
            ]
        }

    return build


def _common(profile: dict, *, classify=first_key_classifier):
    llm = FakeLLM(classify=classify)
    return build_common_profile(profile, llm, model_profile=_MODEL_PROFILE), llm


def _fact_ids(container: dict) -> set[str]:
    return {entry.fact_id for rows in container.values() for entry in rows}


# ------------------------------------------------------------------ Rule 변환


def test_both_real_profiles_convert_by_rule(request_profile, existing_profile):
    """실제 두 프로파일이 같은 표로 옮겨지고 원문·근거를 함께 들고 온다."""

    for profile, activity, item, target_group in (
        (request_profile, "창업교육", "창업지원금", "신청자 명의의 사업자 등록이 없는 청년(19~39세)"),
        (existing_profile, None, "인건비", "미취업 청년"),
    ):
        common, _ = _common(profile)
        assert [entry.value_raw for entry in common.content["item"]][0] == item
        assert target_group in [
            entry.value_raw for entry in common.target["target_group"]
        ]
        assert common.content["activity"], "지원활동은 Rule 로 채워져야 한다"
        if activity is not None:
            assert common.content["activity"][0].value_raw == activity
        for entry in common.content["item"] + common.target["target_group"]:
            assert entry.fact_id
            assert entry.evidence, f"{entry.fact_id} 이 접지 없이 들어왔다"


def test_absent_containers_stay_empty_lists(request_profile, existing_profile):
    """없는 값은 빈 목록이다. 자리표시자를 만들지 않는다 (초안 §7.2)."""

    existing, _ = _common(existing_profile)
    assert existing.delivery == {
        "organization": [],
        "method": [],
        "procedure": [],
        "step_role": [],
    }
    request, _ = _common(request_profile)
    # 요청서에는 delivery_relations 만 있고 delivery_methods 는 비어 있다.
    assert [entry.fact_id for entry in request.delivery["organization"]] == [
        "delivery:center_lead.actor"
    ]
    assert request.delivery["method"] == []


def test_request_delivery_entries_keep_the_relation_grounding(request_profile):
    request, _ = _common(request_profile)
    actor = request.delivery["organization"][0]
    assert actor.value_raw == "가상 동구청년창업지원센터"
    assert actor.evidence[0].common_ir_block_id
    assert request.delivery["step_role"][0].value_raw == "총괄"


# ------------------------------------------------------------------ 제외 계약


def test_duplicate_support_conditions_never_reach_the_target_axis(existing_profile):
    """중복제한은 BEN 근거로 분리한다 (AGENTS.md). SIM-2 에 넣지 않는다."""

    common, _ = _common(existing_profile)
    assert [entry.fact_id for entry in common.benefit_overlap_evidence] == [
        "fact:duplicate_restriction"
    ]
    assert "fact:duplicate_restriction" not in _fact_ids(common.target)
    assert common.benefit_overlap_evidence[0].evidence


def test_support_scale_is_preserved_but_not_compared(request_profile, existing_profile):
    """지원규모 정량값은 Evidence 로만 남고 SIM-3 핵심 축에 들어가지 않는다."""

    for profile in (request_profile, existing_profile):
        common, _ = _common(profile)
        scale_ids = {entry.fact_id for entry in common.scale_reference_evidence}
        assert scale_ids, "지원규모 근거는 보존되어야 한다"
        assert not scale_ids & _fact_ids(common.content)
        assert not scale_ids & _fact_ids(common.target)


def test_deferred_fields_are_reported_not_dropped(request_profile, existing_profile):
    """매핑하지 않은 필드는 목록과 진단으로 남는다 (Slice 2 와 같은 계약)."""

    for profile in (request_profile, existing_profile):
        common, _ = _common(profile)
        present = [
            name for name in _DEFERRED_FIELDS if name in profile["comparison_profile"]
        ]
        assert present
        for name in present:
            assert name in common.unmapped_source_fields
        reported = {
            diagnostic.unit
            for diagnostic in common.diagnostics
            if diagnostic.reason_code == "UNMAPPED_SOURCE_FIELD"
        }
        assert set(common.unmapped_source_fields) == reported
        # 컨테이너로 옮긴 필드는 미매핑으로 보고되지 않는다.
        assert "support_items" not in common.unmapped_source_fields


# --------------------------------------------------------------- 접지 검증


def test_quoted_text_outside_the_source_value_is_dropped(request_profile):
    """원문 부분문자열이 아닌 인용문은 공통 프로파일에 들어가지 않는다."""

    def script(payload):
        return {
            "assignments": [
                {
                    "fact_id": fact["fact_id"],
                    "common_key": fact["allowed_common_keys"][0],
                    "quoted_text": "문서에 없는 문장",
                }
                for fact in payload["facts"]
            ]
        }

    common, _ = _common(request_profile, classify=script)
    assert _fact_ids(common.purpose) == set()
    # 목적은 Rule 경로가 없으므로 통째로 비어 있어야 한다.
    assert common.purpose == {key: [] for key in PURPOSE_KEYS}
    assert any(
        diagnostic.reason_code == LLM_INVALID_RESPONSE
        and "인용문" in diagnostic.message
        for diagnostic in common.diagnostics
    )


def test_a_common_key_outside_the_container_is_dropped(request_profile):
    def script(payload):
        return {
            "assignments": [
                {
                    "fact_id": fact["fact_id"],
                    # target 어휘를 purpose fact 에 붙인다.
                    "common_key": "region",
                    "quoted_text": fact["value_raw"][:4],
                }
                for fact in payload["facts"]
                if fact["container"] == "purpose"
            ]
        }

    common, _ = _common(request_profile, classify=script)
    assert common.purpose == {key: [] for key in PURPOSE_KEYS}
    assert "fact:purpose" not in _fact_ids(common.target)


def test_one_span_cannot_be_copied_into_every_sub_key(request_profile):
    """초안 §7.2: 원문 전체를 모든 하위 키에 반복 복제하지 않는다."""

    def copy_everywhere(payload):
        fact = next(row for row in payload["facts"] if row["container"] == "purpose")
        return {
            "assignments": [
                {
                    "fact_id": fact["fact_id"],
                    "common_key": key,
                    "quoted_text": fact["value_raw"],
                }
                for key in PURPOSE_KEYS
            ]
        }

    common, _ = _common(request_profile, classify=copy_everywhere)
    landed = [key for key in PURPOSE_KEYS if common.purpose[key]]
    assert len(landed) == 1, "같은 span 이 세 축에 복제되었다"
    assert sum(len(common.purpose[key]) for key in PURPOSE_KEYS) == 1
    assert (
        len(
            [
                diagnostic
                for diagnostic in common.diagnostics
                if diagnostic.reason_code == DUPLICATE_SPAN_ASSIGNMENT
            ]
        )
        == len(PURPOSE_KEYS) - 1
    )


def test_independently_grounded_spans_may_share_a_fact(request_profile):
    """같은 fact 라도 span 이 다르면 각각 접지된 배정이므로 받는다."""

    def two_spans(payload):
        fact = next(row for row in payload["facts"] if row["container"] == "purpose")
        raw = fact["value_raw"]
        return {
            "assignments": [
                {
                    "fact_id": fact["fact_id"],
                    "common_key": "problem_domain",
                    "quoted_text": raw[:4],
                },
                {
                    "fact_id": fact["fact_id"],
                    "common_key": "direction",
                    "quoted_text": raw[4:],
                },
            ]
        }

    common, _ = _common(request_profile, classify=two_spans)
    assert len(common.purpose["problem_domain"]) == 1
    assert len(common.purpose["direction"]) == 1
    assert not [
        diagnostic
        for diagnostic in common.diagnostics
        if diagnostic.reason_code == DUPLICATE_SPAN_ASSIGNMENT
    ]


def test_classification_is_called_once_per_side(request_profile, existing_profile):
    """네 축이 나눠 쓰지만 분류 호출은 문서당 한 번이다 (초안 §9.2)."""

    _, request_llm = _common(request_profile)
    _, existing_llm = _common(existing_profile)
    assert len(request_llm.payloads(_CLASSIFY_TASK)) == 1
    assert len(existing_llm.payloads(_CLASSIFY_TASK)) == 1
    # 한 payload 안에 목적·신청자격·자격조건이 함께 실린다.
    fields_seen = {
        fact["source_field"] for fact in existing_llm.payloads(_CLASSIFY_TASK)[0]["facts"]
    }
    assert {"purpose_goal", "applicant_eligibility", "eligibility_conditions"} <= fields_seen


# ------------------------------------------------------------------ 게이트


def test_sim4_is_gated_without_any_llm_call(request_profile, existing_profile):
    """공고 v0.2 에는 전달체계가 없다. 낮은 유사도가 아니라 근거 부재다."""

    request, _ = _common(request_profile)
    existing, _ = _common(existing_profile)
    llm = FakeLLM(comparison=verdict_script("SIMILAR"))
    result = compare_candidate(request, existing, llm, model_profile=_MODEL_PROFILE)

    delivery = result.axis(SimAxis.DELIVERY)
    assert delivery.status is SimStatus.INSUFFICIENT
    assert delivery.reason_code == CANDIDATE_EVIDENCE_MISSING
    for payload in llm.payloads(_COMPARISON_TASK):
        assert all(axis["axis"] != "delivery" for axis in payload["axes"])


def test_a_gated_axis_is_never_reported_as_different(request_profile, existing_profile):
    """모델이 굳이 delivery 판정을 돌려줘도 게이트 결과를 덮지 못한다."""

    request, _ = _common(request_profile)
    existing, _ = _common(existing_profile)

    def intrusive(payload):
        rows = verdict_script("SIMILAR")(payload)["axes"]
        rows.append(
            {
                "axis": "delivery",
                "status": "DIFFERENT",
                "reason_code": "NO_MEANING_OVERLAP",
                "request_fact_ids": [],
                "candidate_fact_ids": [],
                "common_points": [],
                "differences": ["전달체계가 다르다"],
            }
        )
        return {"axes": rows}

    result = compare_candidate(
        request, existing, FakeLLM(comparison=intrusive), model_profile=_MODEL_PROFILE
    )
    assert result.axis(SimAxis.DELIVERY).status is SimStatus.INSUFFICIENT
    assert result.axis(SimAxis.DELIVERY).reason_code == CANDIDATE_EVIDENCE_MISSING


def test_an_unresolved_evidence_ref_is_structuring_not_absence(
    request_profile, existing_profile
):
    """원문은 있는데 참조가 해소되지 않으면 부재가 아니라 구조화 미완료다."""

    request, _ = _common(request_profile)
    existing, _ = _common(existing_profile)
    orphan = replace(
        existing.content["item"][0], fact_id="fact:상위_단계가_확정하지_못한_근거"
    )
    broken = replace(existing, content={**existing.content, "item": [orphan]})

    llm = FakeLLM(comparison=verdict_script("SIMILAR"))
    result = compare_candidate(request, broken, llm, model_profile=_MODEL_PROFILE)
    assert result.axis(SimAxis.CONTENT).status is SimStatus.INSUFFICIENT
    assert result.axis(SimAxis.CONTENT).reason_code == STRUCTURING_INCOMPLETE
    for payload in llm.payloads(_COMPARISON_TASK):
        assert all(axis["axis"] != "content" for axis in payload["axes"])


def test_missing_request_side_is_its_own_reason(request_profile, existing_profile):
    """요청서 쪽 부재와 공고 쪽 부재를 다른 코드로 구분한다."""

    request_profile["comparison_profile"]["support_activities"] = []
    request_profile["comparison_profile"]["support_methods"] = []
    request_profile["comparison_profile"]["support_items"] = []
    request, _ = _common(request_profile)
    existing, _ = _common(existing_profile)
    result = compare_candidate(
        request,
        existing,
        FakeLLM(comparison=verdict_script("SIMILAR")),
        model_profile=_MODEL_PROFILE,
    )
    assert result.axis(SimAxis.CONTENT).reason_code == REQUEST_EVIDENCE_MISSING


# ------------------------------------------------------------ 내부 순위 계산


def test_core_three_assessable_renormalises_to_thirds(request_profile, existing_profile):
    """실제 데이터가 타는 경로다. SIM-4 만 빠지면 핵심 3축을 재정규화한다."""

    request, _ = _common(request_profile)
    existing, _ = _common(existing_profile)
    result = compare_candidate(
        request,
        existing,
        FakeLLM(comparison=verdict_script("SIMILAR")),
        model_profile=_MODEL_PROFILE,
    )
    ranking = result.internal_ranking
    assert result.axis(SimAxis.DELIVERY).status is SimStatus.INSUFFICIENT
    assert set(ranking.axis_weights) == {"purpose", "target", "content"}
    assert all(
        weight == pytest.approx(1 / 3) for weight in ranking.axis_weights.values()
    )
    assert sum(ranking.axis_weights.values()) == pytest.approx(1.0)
    assert ranking.weighted_score == pytest.approx(100.0)
    assert ranking.review_grade is not SimReviewGrade.ON_HOLD
    assert ranking.assessable_axis_count == 3
    assert ranking.scoring_version == SIM_SCORING_VERSION


def test_an_insufficient_core_axis_holds_the_whole_ranking(
    request_profile, existing_profile
):
    """핵심 축이 하나라도 비면 점수를 만들지 않는다. 축 결과는 남는다."""

    request, _ = _common(request_profile)
    existing, _ = _common(existing_profile)

    def one_axis_unassessable(payload):
        rows = verdict_script("SIMILAR")(payload)["axes"]
        for row in rows:
            if row["axis"] == "content":
                row["status"] = "INSUFFICIENT"
                row["reason_code"] = None
                row["request_fact_ids"] = []
                row["candidate_fact_ids"] = []
        return {"axes": rows}

    result = compare_candidate(
        request,
        existing,
        FakeLLM(comparison=one_axis_unassessable),
        model_profile=_MODEL_PROFILE,
    )
    ranking = result.internal_ranking
    assert ranking.weighted_score is None
    assert ranking.review_grade is SimReviewGrade.ON_HOLD
    assert ranking.assessable_axis_count == 2
    assert result.axis(SimAxis.PURPOSE).status is SimStatus.SIMILAR
    assert result.axis(SimAxis.TARGET).status is SimStatus.SIMILAR


def test_scoring_constants_match_the_sim_alpha_policy():
    """backend/config/sim_scoring.json 과 어긋나면 여기서 터진다."""

    policy = json.loads(_SCORING_CONFIG_PATH.read_text(encoding="utf-8"))
    assert policy["version"] == SIM_SCORING_VERSION
    assert {axis.value: weight for axis, weight in _AXIS_WEIGHTS.items()} == policy[
        "axis_weights"
    ]
    assert {
        status.value: score for status, score in _STATUS_SCORES.items()
    } == policy["status_scores"]
    assert policy["grades"]["focus_review_min"] == _FOCUS_REVIEW_MIN
    assert policy["grades"]["general_review_min"] == _GENERAL_REVIEW_MIN


# ---------------------------------------------------------------- 실패 격리


def test_a_bogus_candidate_fact_id_degrades_only_that_axis(
    request_profile, existing_profile
):
    request, _ = _common(request_profile)
    existing, _ = _common(existing_profile)

    def bogus(payload):
        rows = verdict_script("SIMILAR")(payload)["axes"]
        for row in rows:
            if row["axis"] == "target":
                row["candidate_fact_ids"] = ["fact:존재하지_않는_근거"]
        return {"axes": rows}

    llm = FakeLLM(comparison=bogus)
    result = compare_candidate(request, existing, llm, model_profile=_MODEL_PROFILE)

    assert result.axis(SimAxis.TARGET).status is SimStatus.INSUFFICIENT
    assert result.axis(SimAxis.TARGET).reason_code == LLM_INVALID_RESPONSE
    assert result.axis(SimAxis.PURPOSE).status is SimStatus.SIMILAR
    assert result.axis(SimAxis.CONTENT).status is SimStatus.SIMILAR
    # 재시도는 문제가 된 축만 다시 싣는다.
    payloads = llm.payloads(_COMPARISON_TASK)
    assert len(payloads) == 2
    assert [axis["axis"] for axis in payloads[1]["axes"]] == ["target"]
    assert payloads[1]["axes"][0]["previous_response_error"]


def test_an_axis_verdict_that_cites_no_evidence_is_not_accepted(
    request_profile, existing_profile
):
    """판정을 내렸는데 한쪽 근거를 인용하지 않았다면 판정이 아니다 (FIT 와 같은 규칙)."""

    request, _ = _common(request_profile)
    existing, _ = _common(existing_profile)

    def uncited(payload):
        rows = verdict_script("DIFFERENT")(payload)["axes"]
        for row in rows:
            if row["axis"] == "target":
                row["request_fact_ids"] = []
                row["candidate_fact_ids"] = []
        return {"axes": rows}

    result = compare_candidate(
        request, existing, FakeLLM(comparison=uncited), model_profile=_MODEL_PROFILE
    )

    target = result.axis(SimAxis.TARGET)
    assert target.status is not SimStatus.DIFFERENT
    assert target.status is SimStatus.INSUFFICIENT
    assert target.reason_code == LLM_INVALID_RESPONSE
    # 인용한 축은 쓴 id 를 그대로 보존한다.
    purpose = result.axis(SimAxis.PURPOSE)
    assert purpose.status is SimStatus.DIFFERENT
    assert purpose.request_fact_ids and purpose.candidate_fact_ids


def test_out_of_contract_exception_preserves_computed_results(
    request_profile, existing_profile
):
    """포트 계약 밖 예외가 Rule 결과와 게이트 결과를 지우지 않는다 (초안 §9.4)."""

    request, _ = _common(request_profile)
    existing, _ = _common(existing_profile)

    def explode(payload):
        raise ValueError("포트 어댑터가 계약 밖 예외를 던졌다")

    result = compare_candidate(
        request, existing, FakeLLM(comparison=explode), model_profile=_MODEL_PROFILE
    )
    assert result.axis(SimAxis.PURPOSE).reason_code == LLM_UNAVAILABLE
    # 게이트로 이미 확정된 축은 통신 실패로 덮이지 않는다.
    assert result.axis(SimAxis.DELIVERY).reason_code == CANDIDATE_EVIDENCE_MISSING
    assert result.internal_ranking.weighted_score is None
    assert result.internal_ranking.review_grade is SimReviewGrade.ON_HOLD
    # Rule 로 만든 공통 프로파일은 그대로 살아 있다.
    assert existing.content["item"]
    assert request.delivery["organization"]
    assert existing.benefit_overlap_evidence


def test_classification_failure_leaves_the_rule_containers_intact(request_profile):
    """분류 호출이 죽어도 Rule 컨테이너와 보존 근거는 남는다."""

    def explode(payload):
        raise ValueError("계약 밖 예외")

    common, _ = _common(request_profile, classify=explode)
    assert common.purpose == {key: [] for key in PURPOSE_KEYS}
    assert common.content["activity"]
    assert common.target["target_group"], "Rule 로 온 대상군은 남아야 한다"
    assert any(
        diagnostic.reason_code == LLM_UNAVAILABLE for diagnostic in common.diagnostics
    )


# ------------------------------------------------------------ 점수 노출 금지


def test_axis_results_have_no_score_field():
    """축 결과는 사용자 표면이다. 점수 필드가 존재하면 안 된다 (초안 §7.2)."""

    names = {field.name for field in fields(SimAxisResult)}
    assert not names & {"score", "weighted_score", "similarity", "percentage"}


def test_scores_live_only_under_internal_ranking(request_profile, existing_profile):
    request, _ = _common(request_profile)
    existing, _ = _common(existing_profile)
    result = compare_candidate(
        request,
        existing,
        FakeLLM(comparison=verdict_script("PARTIAL", reason="PARTIAL_OVERLAP")),
        model_profile=_MODEL_PROFILE,
    )
    banned = ("score", "percent", "ratio", "probability", "grade", "rank")

    def scan(node, path=""):
        if isinstance(node, dict):
            for key, value in node.items():
                assert not any(word in key.lower() for word in banned), (
                    f"{path}.{key} 에 점수형 필드가 있다"
                )
                scan(value, f"{path}.{key}")
        elif isinstance(node, list):
            for row in node:
                scan(row, path)

    for axis_result in result.axes:
        scan(asdict(axis_result), axis_result.axis_id)
    ranking = asdict(result.internal_ranking)
    assert ranking["weighted_score"] is not None
    assert ranking["review_grade"]


def test_comparison_result_carries_the_lineage(request_profile, existing_profile):
    request, _ = _common(request_profile)
    existing, _ = _common(existing_profile)
    result = compare_candidates(
        request,
        [existing],
        FakeLLM(comparison=verdict_script("SIMILAR")),
        model_profile=_MODEL_PROFILE,
    )
    assert result.request_profile_id == "request:PREREVIEW-TEST-2027-03"
    assert [candidate.candidate_profile_id for candidate in result.candidates] == [
        "hwp:PBLN_000000000125016"
    ]
    assert [candidate.candidate_notice_id for candidate in result.candidates] == [
        "bizinfo:PBLN_000000000125016"
    ]
    assert result.scoring_version == SIM_SCORING_VERSION
    assert result.model_profile == _MODEL_PROFILE


# ------------------------------------------- 구조화 실패와 원천 부재의 구분


def test_a_dead_classifier_is_structuring_not_absence(request_profile, existing_profile):
    """분류 LLM 이 죽어서 컨테이너가 빈 것은 "근거가 없다" 가 아니다.

    초안 §7.2 "입력 부족과 응답 결함을 구분한다", §9.5 는 두 문구를 화면에서
    나눠 보이라고 못박았다. 요청서에는 purpose_goal 원문이 그대로 있다.
    """

    assert request_profile["comparison_profile"]["purpose_goal"][0]["value_raw"]
    request, _ = _common(
        request_profile, classify=LLMTimeoutError("분류 호출이 시간을 넘겼다")
    )
    existing, _ = _common(existing_profile)
    assert request.purpose == {key: [] for key in PURPOSE_KEYS}
    assert request.structuring_of(SimAxis.PURPOSE).status == STRUCTURING_FAILED
    assert request.structuring_of(SimAxis.PURPOSE).reason_code == LLM_TIMEOUT
    assert request.structuring_of(SimAxis.PURPOSE).source

    llm = FakeLLM(comparison=verdict_script("SIMILAR"))
    result = compare_candidate(request, existing, llm, model_profile=_MODEL_PROFILE)
    purpose = result.axis(SimAxis.PURPOSE)
    assert purpose.status is SimStatus.INSUFFICIENT
    assert purpose.status is not SimStatus.DIFFERENT
    assert purpose.reason_code == STRUCTURING_INCOMPLETE
    assert purpose.reason_code != REQUEST_EVIDENCE_MISSING
    for payload in llm.payloads(_COMPARISON_TASK):
        assert all(axis["axis"] != "purpose" for axis in payload["axes"])


def test_a_finished_classification_with_no_source_text_is_absence(
    request_profile, existing_profile
):
    """분류가 정상으로 끝났는데 원문이 없어서 비었으면 그건 진짜 부재다."""

    request_profile["comparison_profile"]["purpose_goal"] = []
    request, _ = _common(request_profile)
    existing, _ = _common(existing_profile)
    # 같은 호출로 대상 축은 정상 분류됐다. 목적 축만 분류할 원문이 없었다.
    assert request.structuring_of(SimAxis.TARGET).status == STRUCTURING_COMPLETED
    assert request.structuring_of(SimAxis.PURPOSE).status != STRUCTURING_FAILED

    result = compare_candidate(
        request,
        existing,
        FakeLLM(comparison=verdict_script("SIMILAR")),
        model_profile=_MODEL_PROFILE,
    )
    purpose = result.axis(SimAxis.PURPOSE)
    assert purpose.status is SimStatus.INSUFFICIENT
    assert purpose.reason_code == REQUEST_EVIDENCE_MISSING


def test_candidate_side_structuring_failure_reaches_the_result(
    request_profile, existing_profile
):
    """공고 쪽 구조화 실패도 축과 진단으로 결과까지 와야 한다."""

    request, _ = _common(request_profile)
    existing, _ = _common(
        existing_profile, classify=LLMUnavailableError("공고 분류 포트가 죽었다")
    )
    result = compare_candidate(
        request,
        existing,
        FakeLLM(comparison=verdict_script("SIMILAR")),
        model_profile=_MODEL_PROFILE,
    )
    purpose = result.axis(SimAxis.PURPOSE)
    assert purpose.status is SimStatus.INSUFFICIENT
    assert purpose.reason_code == STRUCTURING_INCOMPLETE

    failures = [
        diagnostic
        for diagnostic in result.diagnostics
        if diagnostic.reason_code == LLM_UNAVAILABLE
    ]
    assert failures, "공고 프로파일 진단이 결과까지 오지 않았다"
    # 어느 비교를 설명하는 진단인지 축 id 로 이어진다.
    assert "SIM-1" in {diagnostic.unit for diagnostic in failures}
    assert any(diagnostic in purpose.diagnostics for diagnostic in failures)


def test_an_absent_delivery_container_stays_absence(request_profile, existing_profile):
    """공고 v0.2 의 SIM-4 는 구조화 실패가 아니라 원천 부재다. 양방향으로 고정한다."""

    request, _ = _common(request_profile)
    existing, _ = _common(existing_profile)
    assert existing.structuring_of(SimAxis.DELIVERY).status == STRUCTURING_COMPLETED

    result = compare_candidate(
        request,
        existing,
        FakeLLM(comparison=verdict_script("SIMILAR")),
        model_profile=_MODEL_PROFILE,
    )
    delivery = result.axis(SimAxis.DELIVERY)
    assert delivery.reason_code == CANDIDATE_EVIDENCE_MISSING
    assert delivery.reason_code != STRUCTURING_INCOMPLETE

    # 분류가 죽어도 Rule 만 타는 SIM-4 의 사유는 바뀌지 않는다.
    dead, _ = _common(existing_profile, classify=LLMUnavailableError("포트가 죽었다"))
    result = compare_candidate(
        request,
        dead,
        FakeLLM(comparison=verdict_script("SIMILAR")),
        model_profile=_MODEL_PROFILE,
    )
    assert result.axis(SimAxis.DELIVERY).reason_code == CANDIDATE_EVIDENCE_MISSING
    assert result.axis(SimAxis.PURPOSE).reason_code == STRUCTURING_INCOMPLETE


# ------------------------------------------------------------ 프로파일 식별자


def test_profile_and_notice_ids_are_separate_fields(request_profile, existing_profile):
    """공고 프로파일은 출처(hwp:...)와 공고 정체(bizinfo:...)를 둘 다 들고 온다."""

    existing, _ = _common(existing_profile)
    assert existing.source_profile_id == "hwp:PBLN_000000000125016"
    assert existing.notice_id == "bizinfo:PBLN_000000000125016"

    request, _ = _common(request_profile)
    assert request.source_profile_id == "request:PREREVIEW-TEST-2027-03"
    assert request.notice_id is None


def test_two_parses_of_one_notice_do_not_collide(request_profile, existing_profile):
    """같은 공고를 hwp 와 pdf 로 읽은 두 프로파일이 한 id 로 뭉개지지 않는다."""

    hwp, _ = _common(existing_profile)
    pdf, _ = _common(
        {**existing_profile, "source_profile_id": "pdf:PBLN_000000000125016"}
    )
    request, _ = _common(request_profile)
    result = compare_candidates(
        request,
        [hwp, pdf],
        FakeLLM(comparison=verdict_script("SIMILAR")),
        model_profile=_MODEL_PROFILE,
    )
    ids = [candidate.candidate_profile_id for candidate in result.candidates]
    assert ids == ["hwp:PBLN_000000000125016", "pdf:PBLN_000000000125016"]
    assert len(set(ids)) == 2
    assert {candidate.candidate_notice_id for candidate in result.candidates} == {
        "bizinfo:PBLN_000000000125016"
    }


def _profile_with_three_purpose_facts() -> dict:
    profile = json.loads(_REQUEST_PATH.read_text(encoding="utf-8"))
    facts = profile["comparison_profile"]["purpose_goal"]
    base = facts[0]
    for index in (2, 3):
        facts.append(
            dict(base, fact_id=f"fact:purpose{index}", value_raw=f"목적 원문 {index} 사업화")
        )
    return profile


class _GroundsOnlyTheFirstFact:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def generate_structured(self, *, task_name, response_schema, **_):
        self.calls.append(task_name)
        return response_schema.model_validate(
            {
                "assignments": [
                    {
                        "fact_id": "fact:purpose",
                        "common_key": "direction",
                        "quoted_text": "사업화",
                    }
                ]
            }
        )


class _GroundsEveryFact:
    """payload 로 받은 fact 를 전부 접지한다.

    고정된 fact_id 목록을 쓰면 다른 프로파일(공고)에는 하나도 맞지 않아
    그쪽이 조용히 실패한다. 그러면 게이트 테스트가 "요청서의 부분 구조화가
    막았다" 가 아니라 "양쪽 다 실패했다" 를 증명하게 된다.
    """

    async def generate_structured(self, *, messages, response_schema, **_):
        payload = json.loads(messages[1].content)
        assignments = []
        for fact in payload["facts"]:
            value_raw = fact.get("value_raw") or ""
            if not value_raw:
                continue
            assignments.append(
                {
                    "fact_id": fact["fact_id"],
                    "common_key": fact["allowed_common_keys"][0],
                    "quoted_text": value_raw,
                }
            )
        return response_schema.model_validate({"assignments": assignments})


def test_partially_grounded_axis_is_not_promoted_to_completed():
    """제출 원문 3건 중 1건만 접지됐는데 축이 완료로 승격되던 결함이다.

    남은 2건의 의미가 빠진 채 비교하면 거짓 유사·비유사가 나온다.
    아무것도 못 건진 경우와 구분해 CLASSIFICATION_PARTIAL 로 남긴다.
    """

    profile = build_common_profile(
        _profile_with_three_purpose_facts(), _GroundsOnlyTheFirstFact(), model_profile="s"
    )
    structuring = profile.structuring_of(SimAxis.PURPOSE)
    assert structuring.status == STRUCTURING_FAILED
    assert structuring.reason_code == CLASSIFICATION_PARTIAL


def test_fully_grounded_axis_is_completed():
    """제출 원문이 모두 접지되면 정상 완료다."""

    profile = build_common_profile(
        _profile_with_three_purpose_facts(), _GroundsEveryFact(), model_profile="s"
    )
    structuring = profile.structuring_of(SimAxis.PURPOSE)
    assert structuring.status == STRUCTURING_COMPLETED
    assert structuring.reason_code is None


def test_partial_axis_is_gated_not_compared():
    """부분 구조화 축은 비교로 넘어가지 않는다."""

    request = build_common_profile(
        _profile_with_three_purpose_facts(), _GroundsOnlyTheFirstFact(), model_profile="s"
    )
    candidate = build_common_profile(
        json.loads(_EXISTING_PATH.read_text(encoding="utf-8")),
        _GroundsEveryFact(),
        model_profile="s",
    )
    # 후보 측은 정상 COMPLETED 여야 한다. 그래야 막힌 원인이 요청서의
    # 부분 구조화 하나로 좁혀진다.
    assert candidate.structuring_of(SimAxis.PURPOSE).status == STRUCTURING_COMPLETED

    llm = _GroundsEveryFact()
    result = compare_candidate(request, candidate, llm, model_profile="s")
    purpose = result.axis(SimAxis.PURPOSE)
    assert purpose.status is SimStatus.INSUFFICIENT
    assert purpose.reason_code == STRUCTURING_INCOMPLETE
