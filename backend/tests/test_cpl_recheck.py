"""구역 누락이 확인된 경우에만 한 번 재검한다는 것을 고정한다.

1차 추출이 값을 못 내고 원문에는 구역이 있을 때만 그 구역을 다시 묻는다.
정상 완료한 추출에서 특정 축이 없다는 이유만으로는 부르지 않는다
(AGENTS.md FIT 재검 절과 같은 기준).

``CplSubfield.status`` 는 구조화 1차 결과다. 재검이 값을 되찾아도 덮어쓰지
않는다 — 처음에 놓쳤다는 이력이 사라지면 모델·프롬프트를 바꾼 효과를 나중에
잴 수 없다.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel
import pytest

from worker.contracts.cpl_result import (
    CplAxisCode,
    CplFieldCode,
    EXTRACTION_COVERAGE_GAP,
    RECHECK_NO_VALID_OCCURRENCE,
    RECHECK_RECOVERED,
)
from worker.contracts.profile_snapshot import LLM_UNAVAILABLE
from worker.cpl import analyze_cpl
from worker.cpl_coverage import build_fragments


_PURPOSE = "comparison_profile.purpose_goal"
_REGION = "○ (사업목적) 부산 관내 제조 중소기업의 기술경쟁력을 강화"


def _ir() -> dict[str, Any]:
    return {
        "document": {"document_id": "hwpx:d1"},
        "blocks": [{
            "block_id": "hwpx:t4",
            "occurrences": [{"occurrence_id": "occ:1", "text": _REGION}],
        }],
    }


def _empty_profile() -> dict[str, Any]:
    return {
        "comparison_profile": {"purpose_goal": []},
        "field_states": [{"field_name": "purpose_goal", "status": "not_found"}],
    }


def _ref() -> str:
    return build_fragments(_ir(), profile_field=_PURPOSE)[0].evidence_ref


class _Llm:
    def __init__(self, rows: list[dict[str, str]]) -> None:
        self.rows = rows
        self.tasks: list[str] = []

    async def generate_structured(
        self, *, task_name: str, messages: list[Any],
        response_schema: type[BaseModel], model_profile: str,
    ) -> BaseModel:
        self.tasks.append(task_name)
        return response_schema(occurrences=self.rows)


class _Dead:
    def __init__(self) -> None:
        self.tasks: list[str] = []

    async def generate_structured(self, *, task_name: str, **_: Any) -> BaseModel:
        self.tasks.append(task_name)
        raise RuntimeError("포트 밖 예외")


def _purpose(result):
    item = next(
        row for row in result.items if row.field_code is CplFieldCode.PURPOSE_GOAL
    )
    return item, item.subfields[0]


def _row(text: str, axis: str, ref: str | None = None) -> dict[str, str]:
    return {"evidence_ref": ref or _ref(), "raw_text": text, "axis_code": axis}


def _run(llm) -> Any:
    return analyze_cpl(_empty_profile(), llm, model_profile="cpl", common_ir=_ir())


def test_a_recovered_value_keeps_the_first_pass_status() -> None:
    llm = _Llm([
        _row("부산 관내 제조 중소기업", CplAxisCode.TARGET_CONDITION.value),
        _row("강화", CplAxisCode.DIRECTION.value),
    ])

    item, subfield = _purpose(_run(llm))

    assert llm.tasks == ["cpl_purpose_recheck"]
    # 구조화가 놓쳤다는 사실은 지우지 않는다.
    assert subfield.status == "not_found"
    # 되찾았으므로 후보 표시를 걷고 복구 사유를 남긴다.
    assert subfield.reason_codes == [RECHECK_RECOVERED]
    assert item.representative_status == "confirmed"


def test_a_recovered_fact_is_grounded_without_an_invented_id() -> None:
    llm = _Llm([_row("강화", CplAxisCode.DIRECTION.value)])

    _, subfield = _purpose(_run(llm))
    fact = subfield.facts[0]

    # 서버가 가짜 fact_id 를 만들지 않는다.
    assert fact.fact_id is None
    assert fact.evidence_ref == _ref()
    assert fact.status == "identified"
    assert fact.text_basis == "cpl_purpose_recheck"
    # 좌표는 구역이 아니라 되찾은 값 자체를 가리킨다.
    assert _REGION[fact.start_char : fact.end_char] == "강화"
    assert fact.axis_code == CplAxisCode.DIRECTION.value


# 원래 후보 진단은 사라지지 않는다. 처음에 놓쳤다가 되찾았다는 이력이다.
def test_the_original_gap_survives_as_history() -> None:
    llm = _Llm([_row("강화", CplAxisCode.DIRECTION.value)])

    result = _run(llm)

    codes = [row.reason_code for row in result.diagnostics]
    assert EXTRACTION_COVERAGE_GAP in codes
    assert RECHECK_RECOVERED in codes


@pytest.mark.parametrize(
    "rows",
    [
        [{"evidence_ref": "없는참조", "raw_text": "강화", "axis_code": "PURPOSE_DIRECTION"}],
        [_row("원문에 없는 말", CplAxisCode.DIRECTION.value)],
        [_row("강화", "어휘_밖의_축")],
        [_row("", CplAxisCode.DIRECTION.value)],
        [],
    ],
    ids=["모르는 참조", "구역 밖 인용", "어휘 밖 축", "빈 인용", "빈 응답"],
)
def test_an_unverifiable_answer_leaves_the_gap_open(rows: list[dict[str, str]]) -> None:
    item, subfield = _purpose(_run(_Llm(rows)))

    assert subfield.facts == []
    # 후보 표시를 유지하고 구체 사유를 더한다.
    assert subfield.reason_codes == [
        EXTRACTION_COVERAGE_GAP, RECHECK_NO_VALID_OCCURRENCE
    ]
    assert item.representative_status == "needs_confirmation"


def test_a_transport_failure_is_told_apart_from_an_empty_answer() -> None:
    dead = _Dead()

    item, subfield = _purpose(_run(dead))

    assert dead.tasks == ["cpl_purpose_recheck"]  # 같은 입력으로 두 번 부르지 않는다
    assert subfield.reason_codes == [EXTRACTION_COVERAGE_GAP, LLM_UNAVAILABLE]
    assert item.representative_status == "needs_confirmation"


# 값이 이미 있으면 재검 대상이 아니다. 축만 붙이는 1차 분류로 간다.
def test_an_extracted_field_is_never_rechecked() -> None:
    profile = {
        "comparison_profile": {"purpose_goal": [
            {"fact_id": "fact_1", "value_raw": "부산 관내 제조 중소기업", "status": "identified"}
        ]},
        "field_states": [{"field_name": "purpose_goal", "status": "identified"}],
    }
    llm = _Llm([])

    analyze_cpl(profile, llm, model_profile="cpl", common_ir=_ir())

    assert llm.tasks == ["cpl_purpose_classification"] or llm.tasks == [
        "cpl_purpose_axis_classification"
    ]
    assert "cpl_purpose_recheck" not in llm.tasks


# 문서당 의미 호출은 최대 한 번이다. 재검을 했으면 축 분류를 또 부르지 않는다.
def test_one_semantic_call_per_document() -> None:
    llm = _Llm([_row("강화", CplAxisCode.DIRECTION.value)])

    _run(llm)

    assert len(llm.tasks) == 1
