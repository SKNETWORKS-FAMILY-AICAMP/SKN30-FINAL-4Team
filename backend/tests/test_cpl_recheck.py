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
        return response_schema(purpose_occurrences=self.rows)


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

    assert llm.tasks == ["cpl_recheck"]
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

    assert dead.tasks == ["cpl_recheck"]  # 같은 입력으로 두 번 부르지 않는다
    assert subfield.reason_codes == [EXTRACTION_COVERAGE_GAP, LLM_UNAVAILABLE]
    assert item.representative_status == "needs_confirmation"


# 값이 이미 있으면 재검 대상이 아니다. 축만 붙이는 1차 분류로 간다.
def test_an_extracted_field_is_never_rechecked() -> None:
    profile = {
        "comparison_profile": {"purpose_goal": [{
            "fact_id": "fact_1", "value_raw": "부산 관내 제조 중소기업",
            "status": "identified",
            "evidence": [{
                "source_block_id": "hwpx:t4",
                "common_ir_document_id": "hwpx:d1",
                "common_ir_block_id": "hwpx:t4",
                "common_ir_occurrence_ids": ["occ:1"],
            }],
        }]},
        "field_states": [{"field_name": "purpose_goal", "status": "identified"}],
    }
    llm = _Llm([])

    analyze_cpl(profile, llm, model_profile="cpl", common_ir=_ir())

    assert llm.tasks == ["cpl_purpose_classification"] or llm.tasks == [
        "cpl_purpose_axis_classification"
    ]
    assert "cpl_recheck" not in llm.tasks


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
    assert llm.tasks == ["cpl_recheck"]
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
    assert "cpl_recheck" not in llm.tasks


# --- 문서당 한 번, 섹션별 실패 격리 ---------------------------------------
# 섹션을 나눠 호출하면 gap 이 둘 다 나온 문서에서 재검이 두 번 돌아
# "문서당 1 회" 가 깨진다. 대신 실패는 섹션끼리 옮지 않는다.

def _both_ir() -> dict[str, Any]:
    """사업목적 구역과 수행체계 표가 모두 있고 값이 둘 다 빈 문서."""

    return {
        "document": {"document_id": "hwp:d4"},
        "blocks": [
            {
                "block_id": "hwp:b0", "reading_order": 0,
                "occurrences": [{"occurrence_id": "occ:p0", "text": _REGION}],
            },
            {
                "block_id": "hwp:b1", "reading_order": 1,
                "occurrences": [{"occurrence_id": "occ:p1", "text": "  ㅇ 사업추진체계"}],
            },
            {
                "block_id": "hwp:t2", "kind": "table", "structure_status": "explicit",
                "reading_order": 2, "text": "표",
                "cells": [
                    {"cell_id": "t2:c0", "row_index": 0, "col_index": 0, "col_span": 1,
                     "row_span": 1, "text_occurrence_ids": ["occ:t2:c0"]},
                    {"cell_id": "t2:c1", "row_index": 1, "col_index": 0, "col_span": 1,
                     "row_span": 1, "text_occurrence_ids": ["occ:t2:c1"]},
                ],
                "occurrences": [
                    {"occurrence_id": "occ:t2:c0", "text": "부산테크노파크"},
                    {"occurrence_id": "occ:t2:c1", "text": "접수·평가"},
                ],
            },
        ],
    }


def _both_profile() -> dict[str, Any]:
    return {
        "comparison_profile": {"purpose_goal": [], "delivery_relations": []},
        "field_states": [
            {"field_name": "purpose_goal", "status": "not_found"},
            {"field_name": "delivery_relations", "status": "not_found"},
        ],
    }


class _Sections:
    """두 섹션을 따로 돌려주는 fake. 요청 payload 도 붙잡는다."""

    def __init__(self, purpose: list[Any], delivery: list[Any]) -> None:
        self.purpose, self.delivery = purpose, delivery
        self.tasks: list[str] = []
        self.payloads: list[dict[str, Any]] = []

    async def generate_structured(
        self, *, task_name: str, messages: list[Any],
        response_schema: type[BaseModel], model_profile: str,
    ) -> BaseModel:
        import json as _json

        self.tasks.append(task_name)
        self.payloads.append(_json.loads(messages[-1].content))
        return response_schema(
            purpose_occurrences=self.purpose, delivery_decisions=self.delivery
        )


def _both_run(llm) -> Any:
    return analyze_cpl(_both_profile(), llm, model_profile="cpl", common_ir=_both_ir())


def _delivery(result):
    item = next(
        row for row in result.items
        if row.field_code is CplFieldCode.DELIVERY_SYSTEM
    )
    return item, item.subfields[0]


def test_both_sections_go_in_one_call() -> None:
    ir = _both_ir()
    ref = build_fragments(ir, profile_field=_PURPOSE)[0].evidence_ref
    from worker.cpl_delivery import build_delivery_pair_candidates

    (pair,), _ = build_delivery_pair_candidates(ir)
    llm = _Sections(
        [{"evidence_ref": ref, "raw_text": "부산 관내 제조 중소기업",
          "axis_code": CplAxisCode.TARGET_CONDITION.value}],
        [{"candidate_id": pair.candidate_id, "accept": True,
          "actor_evidence_refs": list(pair.evidence_refs("actor")),
          "member_evidence_refs": list(pair.evidence_refs("member")),
          "member_kind": "action"}],
    )

    result = _both_run(llm)

    # 문서당 한 번이다.
    assert llm.tasks == ["cpl_recheck"]
    payload = llm.payloads[0]
    assert len(payload["purpose_regions"]) == 1
    assert len(payload["delivery_pairs"]) == 1
    _item, purpose = _purpose(result)
    assert RECHECK_RECOVERED in purpose.reason_codes


# 목적 응답만 잘못되면 목적만 실패한다.
def test_a_broken_purpose_section_does_not_fail_delivery() -> None:
    ir = _both_ir()
    from worker.cpl_delivery import build_delivery_pair_candidates

    (pair,), _ = build_delivery_pair_candidates(ir)
    llm = _Sections(
        [{"evidence_ref": "없는 ref", "raw_text": "x", "axis_code": "NOPE"}],
        [{"candidate_id": pair.candidate_id, "accept": True,
          "actor_evidence_refs": list(pair.evidence_refs("actor")),
          "member_evidence_refs": list(pair.evidence_refs("member")),
          "member_kind": "action"}],
    )

    result = _both_run(llm)

    _item, purpose = _purpose(result)
    _row, delivery = _delivery(result)
    assert RECHECK_NO_VALID_OCCURRENCE in purpose.reason_codes
    assert RECHECK_NO_VALID_OCCURRENCE not in delivery.reason_codes


# delivery 응답만 잘못되면 delivery 만 실패한다.
def test_a_broken_delivery_section_does_not_fail_purpose() -> None:
    ir = _both_ir()
    ref = build_fragments(ir, profile_field=_PURPOSE)[0].evidence_ref
    llm = _Sections(
        [{"evidence_ref": ref, "raw_text": "부산 관내 제조 중소기업",
          "axis_code": CplAxisCode.TARGET_CONDITION.value}],
        [{"candidate_id": "dpc:없는후보", "accept": True, "member_kind": "role"}],
    )

    result = _both_run(llm)

    _item, purpose = _purpose(result)
    _row, delivery = _delivery(result)
    assert RECHECK_RECOVERED in purpose.reason_codes
    assert RECHECK_NO_VALID_OCCURRENCE in delivery.reason_codes


# 최상위 응답을 못 읽으면 보낸 두 섹션이 모두 실패한다.
def test_an_unreadable_response_fails_both_sections() -> None:
    result = analyze_cpl(
        _both_profile(), _Dead(), model_profile="cpl", common_ir=_both_ir()
    )

    _item, purpose = _purpose(result)
    _row, delivery = _delivery(result)
    assert LLM_UNAVAILABLE in purpose.reason_codes
    assert LLM_UNAVAILABLE in delivery.reason_codes


# 보내지 않은 섹션은 판정도 경고도 만들지 않는다.
def test_an_absent_section_makes_no_verdict() -> None:
    llm = _Llm([])

    result = _run(llm)   # 목적 gap 만 있는 문서

    _row, delivery = _delivery(result)
    # 재검이 만든 사유가 하나도 붙지 않는다. 프로필이 원래 달고 온 사유는 그대로다.
    assert not {
        RECHECK_NO_VALID_OCCURRENCE, RECHECK_RECOVERED, EXTRACTION_COVERAGE_GAP,
        LLM_UNAVAILABLE,
    } & set(delivery.reason_codes)


# --- 축 분류와 재검의 독립성 ------------------------------------------------
# 둘은 입력도 응답 스키마도 다른 별개의 일이다. coverage gap 이 있는 문서는
# 의미 호출이 둘이 되지만, 한쪽이 죽어도 다른 쪽 결과가 사라지지 않는다.

class _PerTask:
    """task 별로 성공·실패를 따로 정하는 fake."""

    def __init__(self, fail: set[str], delivery: list[Any] | None = None) -> None:
        self.fail, self.delivery = fail, delivery or []
        self.tasks: list[str] = []

    async def generate_structured(
        self, *, task_name: str, messages: list[Any],
        response_schema: type[BaseModel], model_profile: str,
    ) -> BaseModel:
        self.tasks.append(task_name)
        if task_name in self.fail:
            raise LLMUnavailableError("포트 밖 장애")
        if task_name == "cpl_recheck":
            return response_schema(delivery_decisions=self.delivery)
        return response_schema()


def _delivery_only_ir() -> dict[str, Any]:
    """목적은 1 차에서 나왔고 수행체계 구역만 빈 문서."""

    return {
        "document": {"document_id": "hwp:d5"},
        "blocks": [
            {
                "block_id": "hwp:b0", "reading_order": 0,
                "occurrences": [{
                    "occurrence_id": "occ:p0",
                    "text": "○ (사업목적) 중소기업의 기술경쟁력을 강화",
                }],
            },
            {
                "block_id": "hwp:b1", "reading_order": 1,
                "occurrences": [{"occurrence_id": "occ:p1", "text": "  ㅇ 사업추진체계"}],
            },
            {
                "block_id": "hwp:t2", "kind": "table", "structure_status": "explicit",
                "reading_order": 2, "text": "표",
                "cells": [
                    {"cell_id": "t2:c0", "row_index": 0, "col_index": 0, "col_span": 1,
                     "row_span": 1, "text_occurrence_ids": ["occ:t2:c0"]},
                    {"cell_id": "t2:c1", "row_index": 1, "col_index": 0, "col_span": 1,
                     "row_span": 1, "text_occurrence_ids": ["occ:t2:c1"]},
                ],
                "occurrences": [
                    {"occurrence_id": "occ:t2:c0", "text": "부산테크노파크"},
                    {"occurrence_id": "occ:t2:c1", "text": "접수·평가"},
                ],
            },
        ],
    }


def _delivery_only_profile() -> dict[str, Any]:
    return {
        "comparison_profile": {
            "purpose_goal": [{
                "fact_id": "fact_1", "value_raw": "중소기업의 기술경쟁력을 강화",
                "value_source": {"source_block_id": "hwp:b0", "start_char": 9, "end_char": 23},
                "evidence": [{
                    "source_block_id": "hwp:b0",
                    "common_ir_document_id": "hwp:d5",
                    "common_ir_block_id": "hwp:b0",
                    "common_ir_occurrence_ids": ["occ:p0"],
                }],
            }],
            "delivery_relations": [],
        },
        "field_states": [
            {"field_name": "purpose_goal", "status": "identified"},
            {"field_name": "delivery_relations", "status": "not_found"},
        ],
    }


def _accept_all(ir: dict[str, Any]) -> list[dict[str, Any]]:
    from worker.cpl_delivery import build_delivery_pair_candidates

    candidates, _ = build_delivery_pair_candidates(ir)
    return [{
        "candidate_id": row.candidate_id, "accept": True,
        "actor_evidence_refs": list(row.evidence_refs("actor")),
        "member_evidence_refs": list(row.evidence_refs("member")),
        "member_kind": "action",
    } for row in candidates]


def test_a_failed_axis_call_does_not_stop_the_delivery_recheck() -> None:
    ir = _delivery_only_ir()
    llm = _PerTask({"cpl_purpose_axis_classification"}, _accept_all(ir))

    result = analyze_cpl(
        _delivery_only_profile(), llm, model_profile="cpl", common_ir=ir
    )

    assert llm.tasks == ["cpl_purpose_axis_classification", "cpl_recheck"]
    _item, delivery = _delivery(result)
    assert RECHECK_RECOVERED in delivery.reason_codes
    assert [fact.value_raw for fact in delivery.facts if fact.member == "actor"] == [
        "부산테크노파크"
    ]


def test_a_failed_delivery_recheck_keeps_the_purpose_axes() -> None:
    ir = _delivery_only_ir()
    llm = _PerTask({"cpl_recheck"})

    result = analyze_cpl(
        _delivery_only_profile(), llm, model_profile="cpl", common_ir=ir
    )

    assert llm.tasks == ["cpl_purpose_axis_classification", "cpl_recheck"]
    _row, purpose = _purpose(result)
    # 축 호출은 성공했다. 재검 실패가 목적을 낮추지 않는다.
    assert [fact.value_raw for fact in purpose.facts] == ["중소기업의 기술경쟁력을 강화"]
    assert RECHECK_NO_VALID_OCCURRENCE not in purpose.reason_codes
    _item, delivery = _delivery(result)
    assert LLM_UNAVAILABLE in delivery.reason_codes


def _two_member_ir() -> dict[str, Any]:
    """멤버 칸에 문단이 둘인 문서. 순번이 실제로 갈리는 유일한 형태."""

    ir = _delivery_only_ir()
    table = next(row for row in ir["blocks"] if row["block_id"] == "hwp:t2")
    table["cells"][1]["text_occurrence_ids"] = ["occ:t2:c1", "occ:t2:c1b"]
    table["occurrences"].append({"occurrence_id": "occ:t2:c1b", "text": "사후관리"})
    return ir


def _coordinates(result) -> list[tuple[str | None, str | None, int | None]]:
    _item, delivery = _delivery(result)
    return [
        (fact.relation_id, fact.member, fact.member_index) for fact in delivery.facts
    ]


def test_recovered_member_coordinates_are_positional_and_repeatable() -> None:
    ir = _two_member_ir()
    accepted = _accept_all(ir)

    first = _coordinates(
        analyze_cpl(_delivery_only_profile(), _PerTask(set(), accepted),
                    model_profile="cpl", common_ir=ir)
    )
    second = _coordinates(
        analyze_cpl(_delivery_only_profile(), _PerTask(set(), accepted),
                    model_profile="cpl", common_ir=ir)
    )

    # actor 는 순번이 없고, 같은 관계 안의 멤버만 0·1 로 갈린다.
    assert [(member, index) for _relation, member, index in first] == [
        ("actor", None), ("action", 0), ("action", 1)
    ]
    # 관계 id 는 후보와 actor 좌표에서만 나온다. 다시 돌려도 같아야 저장된
    # 결과와 대조된다.
    assert len({relation for relation, _member, _index in first}) == 1
    assert first == second
