"""FIT-4 인접 사업계층 비교의 알파 계약을 고정한다."""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel

from worker.contracts.fit_result import (
    COMPARISON_EVIDENCE_MISSING,
    FitRelationId,
    FitStatus,
)
from worker.cpl import build_cpl_result
from worker.fit import analyze_fit
from worker.result_payload import plain


def _node(
    node_id: str,
    level: str,
    name: str,
    parent: str | None = None,
) -> dict[str, Any]:
    return {
        "program_node_id": node_id,
        "level": level,
        "parent_node_id": parent,
        "name_raw": name,
        "value_source": {
            "source_block_id": f"block:{node_id}",
            "start_char": 0,
            "end_char": len(name),
        },
        "evidence": [{
            "source_block_id": f"block:{node_id}",
            "common_ir_document_id": "doc:fit4",
            "common_ir_block_id": f"block:{node_id}",
            "common_ir_occurrence_ids": [f"occ:{node_id}"],
        }],
    }


def _profile(*, parent: str | None = "pn1") -> dict[str, Any]:
    return {
        "profile_id": "request:fit4",
        "processing_metadata": {"common_ir_document_id": "doc:fit4"},
        "program_hierarchy": {"nodes": [
            _node("pn1", "detail_program", "미래자동차산업 전환 지원사업"),
            _node("pn2", "sub_program", "친환경차 부품 양산전환 지원", parent),
        ]},
        "comparison_profile": {
            "support_target": [{
                "fact_id": "fact:child-target",
                "value_raw": "자동차부품 제조 중소기업",
                "program_node_id": "pn2",
                "status": "identified",
            }],
            "total_budget": [{
                "fact_id": "fact:child-budget",
                "value_raw": "예산 전용 근거",
                "program_node_id": "pn2",
                "status": "identified",
            }],
        },
        "request_context": {
            "legal_basis": [{
                "fact_id": "fact:child-legal",
                "value_raw": "법적근거 전용 근거",
                "program_node_id": "pn2",
                "status": "identified",
            }],
        },
        "field_states": [{
            "field_name": "support_target",
            "status": "identified",
        }],
    }


class _HierarchyLLM:
    def __init__(self, status: str = "FIT") -> None:
        self.status = status
        self.payloads: list[dict[str, Any]] = []

    async def generate_structured(
        self,
        *,
        task_name: str,
        messages: list[Any],
        response_schema: type[BaseModel],
        model_profile: str,
    ) -> BaseModel:
        assert task_name == "fit_relation_comparison"
        payload = json.loads(messages[-1].content)
        self.payloads.append(payload)
        assert len(payload["relations"]) == 1
        relation = payload["relations"][0]
        return response_schema.model_validate({
            "relations": [{
                "relation_id": relation["relation_id"],
                "status": self.status,
                "reason_code": None,
                "left_fact_ids": [row["fact_id"] for row in relation["left"]],
                "right_fact_ids": [row["fact_id"] for row in relation["right"]],
            }],
        })


def _fit4(result):
    return next(row for row in result.relations if row.relation_id is FitRelationId.FIT_4)


def test_explicit_adjacent_parent_child_is_compared_with_grounded_names() -> None:
    cpl = build_cpl_result(_profile())
    llm = _HierarchyLLM()

    result = analyze_fit(cpl, llm, model_profile="test")
    fit4 = _fit4(result)

    assert fit4.status is FitStatus.FIT
    assert fit4.reason_code is None
    relation = llm.payloads[0]["relations"][0]
    assert relation["relation_id"] == "FIT-4"
    assert relation["left"][0]["value_raw"] == "미래자동차산업 전환 지원사업"
    assert relation["right"][0]["value_raw"] == "친환경차 부품 양산전환 지원"
    assert any(row["fact_id"] == "fact:child-target" for row in relation["right"])
    assert all(row["fact_id"] not in {"fact:child-budget", "fact:child-legal"}
               for row in relation["right"])
    assert all(row["value_raw"] not in {"예산 전용 근거", "법적근거 전용 근거"}
               for row in relation["right"])


def test_explicit_parent_edge_survives_reversed_levels() -> None:
    profile = _profile()
    # 실제 구조화 출력처럼 level이 뒤집혀도 parent_node_id가 가리키는
    # 명시 관계를 level 순서로 추측해 버리지 않는다.
    profile["program_hierarchy"]["nodes"][0]["level"] = "sub_program"
    profile["program_hierarchy"]["nodes"][1]["level"] = "detail_program"
    cpl = build_cpl_result(profile)
    llm = _HierarchyLLM()

    fit4 = _fit4(analyze_fit(cpl, llm, model_profile="test"))

    assert fit4.status is FitStatus.FIT
    assert llm.payloads[0]["relations"][0]["relation_id"] == "FIT-4"


def test_missing_parent_keeps_fit4_insufficient_without_guessing_from_order() -> None:
    cpl = build_cpl_result(_profile(parent="unknown-parent"))
    llm = _HierarchyLLM()

    result = analyze_fit(cpl, llm, model_profile="test")
    fit4 = _fit4(result)

    assert fit4.status is FitStatus.INSUFFICIENT
    assert fit4.reason_code == COMPARISON_EVIDENCE_MISSING
    assert not llm.payloads


def test_hierarchy_metadata_is_internal_and_never_serialized() -> None:
    cpl = build_cpl_result(_profile())
    nodes = [
        fact
        for item in cpl.items
        for subfield in item.subfields
        if subfield.profile_field == "program_hierarchy.nodes"
        for fact in subfield.facts
    ]
    pn2 = next(fact for fact in nodes if fact.program_node_id == "pn2")

    assert pn2.program_level == "sub_program"
    assert pn2.parent_program_node_id == "pn1"
    encoded = json.dumps(plain(cpl), ensure_ascii=False)
    assert "program_level" not in encoded
    assert "parent_program_node_id" not in encoded


def test_explicit_hierarchy_broadening_can_stay_needs_review() -> None:
    cpl = build_cpl_result(_profile())
    llm = _HierarchyLLM("NEEDS_REVIEW")

    fit4 = _fit4(analyze_fit(cpl, llm, model_profile="test"))

    assert fit4.status is FitStatus.NEEDS_REVIEW
