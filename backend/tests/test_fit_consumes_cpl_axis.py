"""FIT 이 목적 축을 스스로 분류하지 않고 CPL 것만 소비한다는 것을 고정한다.

축을 만드는 곳이 둘이면 화면과 판정이 갈라진다. CPL 이 "사업목적 확인됨" 인데
FIT 이 같은 문장을 다시 분류하다 실패해 FIT-1·2·3 이 근거 0 개로 멈추는 것이
실문서 12 런에서 11 번 일어났다. 축을 만드는 책임만 CPL 로 옮기고, 축을 쓰는
판정은 FIT 에 그대로 둔다.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel
import pytest

from worker.contracts.cpl_result import CplAxisCode
from worker.contracts.fit_result import FitRelationId, FitStatus
from worker.cpl import analyze_cpl, build_cpl_result
from worker.fit import analyze_fit


_VALUE = "부산 관내 제조 중소기업의 기술경쟁력을 강화하여 매출 성장을 달성"
_TARGET = "부산 관내 제조 중소기업"
_DIRECTION = "매출 성장을 달성"


def _profile() -> dict[str, Any]:
    return {
        "comparison_profile": {
            "purpose_goal": [
                {"fact_id": "fact_1", "value_raw": _VALUE, "status": "identified"}
            ],
            "support_target": [
                {"fact_id": "fact_2", "value_raw": "부산 소재 중소기업", "status": "identified"}
            ],
        },
        "field_states": [
            {"field_name": "purpose_goal", "status": "identified"},
            {"field_name": "support_target", "status": "identified"},
        ],
    }


class _Recorder:
    """호출된 task_name 을 기록한다. 응답은 지정한 것만 돌려준다."""

    def __init__(self, assignments: list[dict[str, str]] | None = None) -> None:
        self.tasks: list[str] = []
        self._assignments = assignments or []

    async def generate_structured(
        self, *, task_name: str, messages: list[Any],
        response_schema: type[BaseModel], model_profile: str,
    ) -> BaseModel:
        self.tasks.append(task_name)
        if "assignments" in response_schema.model_fields:
            return response_schema(assignments=self._assignments)
        raise RuntimeError("비교 호출은 이 테스트 범위 밖이다")


def _rows() -> list[dict[str, str]]:
    return [
        {"fact_id": "fact_1", "axis_code": CplAxisCode.TARGET_CONDITION.value,
         "quoted_text": _TARGET},
        {"fact_id": "fact_1", "axis_code": CplAxisCode.DIRECTION.value,
         "quoted_text": _DIRECTION},
    ]


def _left(fit, relation_id: FitRelationId):
    return next(row for row in fit.relations if row.relation_id is relation_id).left


def test_fit_carries_the_cpl_classification_record_as_is() -> None:
    cpl = analyze_cpl(_profile(), _Recorder(_rows()), model_profile="default")

    fit = analyze_fit(cpl, _Recorder(), model_profile="default")

    assert fit.purpose_axis is cpl.purpose_axis


# FIT 단계에서 축 분류 호출이 일어나면 두 곳이 다시 각자 분류하는 것이다.
def test_fit_never_calls_the_axis_classifier() -> None:
    cpl = analyze_cpl(_profile(), _Recorder(_rows()), model_profile="default")
    fit_llm = _Recorder()

    analyze_fit(cpl, fit_llm, model_profile="default")

    assert "cpl_purpose_axis_classification" not in fit_llm.tasks
    assert "fit_purpose_axis_classification" not in fit_llm.tasks


# CPL 축과 반대되는 응답을 줄 LLM 을 FIT 에 넣어도 CPL 축만 쓰인다.
def test_a_contradicting_model_at_fit_time_changes_nothing() -> None:
    cpl = analyze_cpl(_profile(), _Recorder(_rows()), model_profile="default")
    hostile = _Recorder([
        {"fact_id": "fact_1", "axis_code": CplAxisCode.DIRECTION.value,
         "quoted_text": _TARGET},
    ])

    fit = analyze_fit(cpl, hostile, model_profile="default")

    # 대상조건 축에는 CPL 이 지목한 인용문이 그대로 실린다.
    assert [row.value_raw for row in _left(fit, FitRelationId.FIT_1).facts] == [_TARGET]
    assert [row.value_raw for row in _left(fit, FitRelationId.FIT_2).facts] == [_DIRECTION]


# 축이 없으면 재분류하지 않고 비교를 포기한다.
@pytest.mark.parametrize(
    "cpl_factory",
    [
        lambda: build_cpl_result(_profile()),                       # 축을 붙인 적 없음
        lambda: analyze_cpl(_profile(), _Recorder([]), model_profile="default"),
    ],
    ids=["축 시도 안 함", "축 시도했으나 비었음"],
)
def test_missing_axes_end_as_insufficient_without_reclassifying(cpl_factory) -> None:
    fit_llm = _Recorder()

    fit = analyze_fit(cpl_factory(), fit_llm, model_profile="default")

    for relation_id in (FitRelationId.FIT_1, FitRelationId.FIT_2, FitRelationId.FIT_3):
        row = next(item for item in fit.relations if item.relation_id is relation_id)
        assert row.status is FitStatus.INSUFFICIENT
        assert row.left.facts == []
    assert fit_llm.tasks == []


def test_the_seven_relation_output_shape_is_unchanged() -> None:
    cpl = analyze_cpl(_profile(), _Recorder(_rows()), model_profile="default")

    fit = analyze_fit(cpl, _Recorder(), model_profile="default")

    assert len(fit.relations) == 7
    assert [row.relation_id for row in fit.relations] == list(FitRelationId)
