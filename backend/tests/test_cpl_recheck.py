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


# --- 라벨과 내용이 다른 블록에 있는 서식 -----------------------------------
# 값 span 후보 쪽과 달리 재검은 gap 이 먼저 붙어야 돈다. 라벨과 내용이 갈린
# 서식에서 fragment 만 만들어지고 gap 이 안 잡히면, 만들어 둔 fragment 가
# 파이프라인에서 한 번도 쓰이지 않는다.

_SIBLING_LABEL = "□ 사업목적"
_SIBLING_CONTENT = " ㅇ ICT혁신기업의 기술개발을 단계별로 지원"


def _sibling_ir(second: str = _SIBLING_CONTENT) -> dict[str, Any]:
    return {
        "document": {"document_id": "hwp:d2"},
        "blocks": [
            {
                "block_id": "hwp:b0", "reading_order": 0,
                "occurrences": [{"occurrence_id": "occ:p0", "text": _SIBLING_LABEL}],
            },
            {
                "block_id": "hwp:b1", "reading_order": 1,
                "occurrences": [{"occurrence_id": "occ:p1", "text": second}],
            },
        ],
    }


def test_a_sibling_region_reaches_the_recheck() -> None:
    ir = _sibling_ir()
    fragments = build_fragments(ir, profile_field=_PURPOSE)
    assert len(fragments) == 1
    llm = _Llm([{
        "evidence_ref": fragments[0].evidence_ref,
        "raw_text": "ICT혁신기업",
        "axis_code": CplAxisCode.TARGET_CONDITION.value,
    }])

    result = analyze_cpl(_empty_profile(), llm, model_profile="cpl", common_ir=ir)

    _item, subfield = _purpose(result)
    # gap 이 붙어야 재검이 돌고, 돌아야 값이 되돌아온다.
    assert llm.tasks == ["cpl_purpose_recheck"]
    assert RECHECK_RECOVERED in subfield.reason_codes
    assert [fact.value_raw for fact in subfield.facts] == ["ICT혁신기업"]
    # ref 는 내용 occurrence 를 가리킨다.
    assert subfield.facts[0].evidence_ref.startswith("hwp:d2#occ:p1@")


def test_a_label_followed_by_another_form_label_never_rechecks() -> None:
    llm = _Llm([])

    result = analyze_cpl(
        _empty_profile(), llm, model_profile="cpl",
        common_ir=_sibling_ir("□ 지원대상 : 중소기업"),
    )

    _item, subfield = _purpose(result)
    assert llm.tasks == []
    assert EXTRACTION_COVERAGE_GAP not in subfield.reason_codes


# 수행체계 구역은 coverage 만 붙인다. 목적 재검기는 `evidence_ref/raw_text/
# axis_code` 형식이라 actor·role 의 컨테이너와 멤버 좌표를 표현할 수 없다.
# 관계 재검은 별도 계약이며 여기서 끌어오지 않는다.
def test_a_delivery_gap_never_calls_the_purpose_recheck() -> None:
    ir = {
        "document": {"document_id": "hwp:d3"},
        "blocks": [
            {
                "block_id": f"hwp:b{i}", "reading_order": i,
                "occurrences": [{"occurrence_id": f"occ:p{i}", "text": text}],
            }
            for i, text in enumerate(
                ["  ㅇ 사업추진체계", "< 사업추진 체계도 >", "(주무부처)", "정책수립 및 예산 지원"]
            )
        ],
    }
    profile = {
        "comparison_profile": {
            "purpose_goal": [{"value_raw": "기술경쟁력 강화"}], "delivery_relations": [],
        },
        "field_states": [
            {"field_name": "purpose_goal", "status": "identified"},
            {"field_name": "delivery_relations", "status": "not_found"},
        ],
    }
    llm = _Llm([])

    result = analyze_cpl(profile, llm, model_profile="cpl", common_ir=ir)

    delivery = next(
        row for row in result.items
        if row.field_code is CplFieldCode.DELIVERY_SYSTEM
    )
    relations = delivery.subfields[0]
    assert EXTRACTION_COVERAGE_GAP in relations.reason_codes
    assert delivery.representative_status == "needs_confirmation"
    # delivery_methods 를 함께 묶지 않는다.
    methods = next(
        row for row in delivery.subfields
        if row.profile_field_name == "delivery_methods"
    )
    assert EXTRACTION_COVERAGE_GAP not in methods.reason_codes
    assert "cpl_purpose_recheck" not in llm.tasks
