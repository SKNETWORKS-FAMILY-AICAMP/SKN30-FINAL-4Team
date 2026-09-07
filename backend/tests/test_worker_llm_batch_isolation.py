"""배치 응답 하나가 깨졌을 때 항목별 격리가 실제로 도는지 본다.

다른 워커 테스트는 이미 검증된 pydantic 객체를 돌려주는 가짜 포트를 쓴다.
그러면 어댑터가 배치 **전체** 를 한 번에 검증한다는 사실이 드러나지 않는다.
여기서는 ``VllmLLMClient`` 를 ``httpx.MockTransport`` 로 실제로 태우고, 그
결과를 ``analyze_fit`` · SIM 비교에 그대로 먹인다. 네트워크는 타지 않는다.

고정하는 것은 두 가지다.

1. 행 하나가 스키마를 어겨도 나머지 행의 판정이 살아남는가.
2. 최상위(JSON 아님·엔벨로프 키 부재)는 배치 전체가 내려가는가.
"""

import json
import logging
from pathlib import Path

import httpx
import pytest

from app.ports.llm_client import LLMInvalidResponseError, Message
from worker.adapters.vllm_llm_client import VllmLLMClient
from worker.contracts.fit_result import (
    LLM_INVALID_RESPONSE,
    SINGLE_SIDED_NO_CONFLICT,
    FitRelationId,
    FitStatus,
    PurposeAxisCode,
)
from worker.contracts.sim_result import SimAxis, SimStatus
from worker.fit import _FitComparisonResponse, analyze_fit
from worker.sim import compare_candidate
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

_MODEL_PROFILE = "structuring"
_PURPOSE_TASK = "fit_purpose_axis_classification"
_FIT_TASK = "fit_relation_comparison"
_SIM_CLASSIFY_TASK = "sim_common_key_classification"
_SIM_COMPARE_TASK = "sim_axis_comparison"


@pytest.fixture()
def request_profile() -> dict:
    return json.loads(_REQUEST_PATH.read_text(encoding="utf-8"))


@pytest.fixture()
def existing_profile() -> dict:
    return json.loads(_EXISTING_PATH.read_text(encoding="utf-8"))


# --------------------------------------------------------- 어댑터를 태우는 포트


def _adapter(scripts: dict) -> VllmLLMClient:
    """task_name 별 스크립트를 그대로 본문에 실어 주는 실제 어댑터.

    스크립트가 문자열이면 그 문자열이 content 가 된다 (JSON 아닌 본문 시험용).
    """

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        task = body["response_format"]["json_schema"]["name"]
        payload = json.loads(body["messages"][-1]["content"])
        script = scripts[task]
        answer = script(payload) if callable(script) else script
        content = (
            answer if isinstance(answer, str) else json.dumps(answer, ensure_ascii=False)
        )
        return httpx.Response(
            200, json={"choices": [{"message": {"role": "assistant", "content": content}}]}
        )

    return VllmLLMClient(
        api_key="test-key",
        base_url="https://vllm.invalid/v1",
        model_profiles={_MODEL_PROFILE: "served-gemma"},
        timeout_seconds=5,
        transport=httpx.MockTransport(handler),
    )


def _direction(payload: dict) -> dict:
    """목적 fact 를 전부 방향 축으로 분류한다."""

    return {
        "assignments": [
            {
                "fact_id": fact["fact_id"],
                "axis_code": PurposeAxisCode.DIRECTION.value,
                "quoted_text": (fact["value_raw"] or "")[:6],
            }
            for fact in payload["facts"]
        ]
    }


def _agree(payload: dict) -> dict:
    """요청된 관계를 전부 근거를 인용해 FIT 으로 돌려준다."""

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


def _fit_with(mutate):
    """_agree 응답을 한 번 손본 스크립트."""

    def script(payload: dict) -> dict:
        answer = _agree(payload)
        mutate(answer)
        return answer

    return script


def _relation(result, relation_id: FitRelationId):
    return next(row for row in result.relations if row.relation_id is relation_id)


def _fit_result(fit_script, profile, purpose_script=_direction):
    return analyze_fit(
        profile,
        _adapter({_PURPOSE_TASK: purpose_script, _FIT_TASK: fit_script}),
        model_profile=_MODEL_PROFILE,
    )


# LLM 을 타는 관계 중, 목적 축이 없어도 게이트를 통과하는 관계.
_LLM_RELATIONS = (
    FitRelationId.FIT_2,
    FitRelationId.FIT_3,
    FitRelationId.FIT_5,
    FitRelationId.FIT_6,
)


# ------------------------------------------------------------------ 행 격리


def test_one_relation_with_a_null_status_degrades_alone(request_profile):
    """행 하나의 스키마 위반이 배치 전체를 내리지 않는다."""

    def break_fit5(answer: dict) -> None:
        for row in answer["relations"]:
            if row["relation_id"] == FitRelationId.FIT_5.value:
                row["status"] = None

    result = _fit_result(_fit_with(break_fit5), request_profile)

    broken = _relation(result, FitRelationId.FIT_5)
    assert broken.status is FitStatus.INSUFFICIENT
    assert broken.reason_code == LLM_INVALID_RESPONSE
    for relation_id in (FitRelationId.FIT_2, FitRelationId.FIT_3, FitRelationId.FIT_6):
        assert _relation(result, relation_id).status is FitStatus.FIT


def test_a_row_that_is_not_a_dict_is_dropped_with_a_warning(request_profile):
    def insert_junk(answer: dict) -> None:
        answer["relations"].insert(0, "관계가 아니라 문자열")

    result = _fit_result(_fit_with(insert_junk), request_profile)

    for relation_id in _LLM_RELATIONS:
        assert _relation(result, relation_id).status is FitStatus.FIT
    assert any(
        row.reason_code == LLM_INVALID_RESPONSE for row in result.diagnostics
    ), "식별할 수 없는 행을 버렸다는 진단이 없다"


# --------------------------------------------------------------- 최상위 실패


def test_a_body_that_is_not_json_degrades_the_whole_batch(request_profile):
    result = _fit_result("이것은 JSON 이 아니다", request_profile)

    for relation_id in _LLM_RELATIONS:
        row = _relation(result, relation_id)
        assert row.status is FitStatus.INSUFFICIENT
        assert row.reason_code == LLM_INVALID_RESPONSE
    # LLM 을 타지 않은 Rule 관계는 그대로다.
    assert _relation(result, FitRelationId.FIT_7).reason_code == SINGLE_SIDED_NO_CONFLICT


def test_a_missing_relations_envelope_degrades_the_whole_batch(request_profile):
    """엔벨로프 키 부재는 최상위 오류다. 빈 배치로 조용히 통과하지 않는다."""

    result = _fit_result({}, request_profile)

    for relation_id in _LLM_RELATIONS:
        row = _relation(result, relation_id)
        assert row.status is FitStatus.INSUFFICIENT
        assert row.reason_code == LLM_INVALID_RESPONSE


def test_an_empty_envelope_key_is_a_schema_violation():
    """스키마 자체가 엔벨로프 키를 요구한다."""

    with pytest.raises(Exception):
        _FitComparisonResponse.model_validate({})


# ------------------------------------------------------------ 분류 호출 격리


def test_one_bad_purpose_assignment_keeps_the_valid_ones(request_profile):
    """배정 하나가 깨져도 나머지 배정은 버리지 않는다."""

    def purpose(payload: dict) -> dict:
        answer = _direction(payload)
        answer["assignments"].append(
            {"fact_id": "fact:purpose", "axis_code": None, "quoted_text": "깨진 행"}
        )
        return answer

    result = _fit_result(_agree, request_profile, purpose_script=purpose)

    assert [row.axis_code for row in result.purpose_axis.assignments] == [
        PurposeAxisCode.DIRECTION.value
    ]
    for relation_id in (FitRelationId.FIT_2, FitRelationId.FIT_3):
        assert _relation(result, relation_id).status is FitStatus.FIT


# ------------------------------------------------------------------ SIM 쪽


def _first_key(payload: dict) -> dict:
    return {
        "assignments": [
            {
                "fact_id": fact["fact_id"],
                "common_key": fact["allowed_common_keys"][0],
                "quoted_text": (fact["value_raw"] or "")[:6],
            }
            for fact in payload["facts"]
        ]
    }


def _similar(payload: dict) -> dict:
    return {
        "axes": [
            {
                "axis": axis["axis"],
                "status": SimStatus.SIMILAR.value,
                "reason_code": None,
                "request_fact_ids": [row["fact_id"] for row in axis["request"][:1]],
                "candidate_fact_ids": [row["fact_id"] for row in axis["candidate"][:1]],
                "common_points": ["같은 대상 표현"],
                "differences": [],
            }
            for axis in payload["axes"]
        ]
    }


def _common(profile: dict, classify=_first_key):
    return build_common_profile(
        profile,
        _adapter({_SIM_CLASSIFY_TASK: classify}),
        model_profile=_MODEL_PROFILE,
    )


def _axis(result, axis: SimAxis):
    return next(row for row in result.axes if row.axis is axis)


def test_one_bad_axis_row_degrades_only_that_axis(request_profile, existing_profile):
    def comparison(payload: dict) -> dict:
        answer = _similar(payload)
        for row in answer["axes"]:
            if row["axis"] == SimAxis.TARGET.value:
                row["status"] = None
        return answer

    result = compare_candidate(
        _common(request_profile),
        _common(existing_profile),
        _adapter({_SIM_COMPARE_TASK: comparison}),
        model_profile=_MODEL_PROFILE,
    )

    assert _axis(result, SimAxis.TARGET).status is SimStatus.INSUFFICIENT
    assert _axis(result, SimAxis.TARGET).reason_code == LLM_INVALID_RESPONSE
    for axis in (SimAxis.PURPOSE, SimAxis.CONTENT):
        assert _axis(result, axis).status is SimStatus.SIMILAR


def test_one_bad_common_key_assignment_keeps_the_valid_ones(request_profile):
    """분류 응답 한 줄이 깨져도 나머지 배정은 공통 프로파일에 들어간다."""

    def classify(payload: dict) -> dict:
        answer = _first_key(payload)
        for row in answer["assignments"]:
            if row["fact_id"] == "fact:applicant":
                row["common_key"] = None
        return answer

    common = _common(request_profile, classify=classify)

    assert [entry.fact_id for rows in common.purpose.values() for entry in rows] == [
        "fact:purpose"
    ]


def test_a_sim_body_that_is_not_json_degrades_the_whole_batch(
    request_profile, existing_profile
):
    result = compare_candidate(
        _common(request_profile),
        _common(existing_profile),
        _adapter({_SIM_COMPARE_TASK: "이것은 JSON 이 아니다"}),
        model_profile=_MODEL_PROFILE,
    )

    for axis in SimAxis:
        assert _axis(result, axis).status is SimStatus.INSUFFICIENT


# ------------------------------------------------------------- raw 비노출


_SECRET = "요청서원문-홍길동-901201-1234567"


def _leaky_fit(payload: dict) -> dict:
    answer = _agree(payload)
    for row in answer["relations"]:
        if row["relation_id"] == FitRelationId.FIT_5.value:
            row["status"] = None
            row["reason_code"] = _SECRET
    return answer


def test_raw_payload_never_reaches_the_error_string():
    """부분 회수용 raw 는 메모리 안에만 있다. 문자열로 새면 안 된다."""

    client = _adapter({_FIT_TASK: _leaky_fit})
    payload = {
        "relations": [
            {"relation_id": FitRelationId.FIT_5.value, "left": [], "right": []}
        ]
    }
    with pytest.raises(LLMInvalidResponseError) as caught:
        import asyncio

        asyncio.run(
            client.generate_structured(
                task_name=_FIT_TASK,
                messages=[
                    Message(role="user", content=json.dumps(payload, ensure_ascii=False))
                ],
                response_schema=_FitComparisonResponse,
                model_profile=_MODEL_PROFILE,
            )
        )

    error = caught.value
    assert _SECRET not in str(error)
    assert _SECRET not in repr(error)
    assert _SECRET in json.dumps(error.raw, ensure_ascii=False), (
        "부분 회수에 쓸 raw 는 남아 있어야 한다"
    )


def test_raw_payload_never_reaches_diagnostics_or_logs(request_profile, caplog):
    caplog.set_level(logging.DEBUG)
    result = _fit_result(_leaky_fit, request_profile)

    messages = [row.message for row in result.diagnostics]
    for row in result.relations:
        messages.extend(diagnostic.message for diagnostic in row.diagnostics)
    assert messages, "전제가 깨졌다: 진단이 하나도 없다"
    for message in messages:
        assert _SECRET not in message
    assert _SECRET not in caplog.text
