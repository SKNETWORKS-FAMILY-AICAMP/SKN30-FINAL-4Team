from __future__ import annotations

import json
from typing import Any

from worker.cpl import build_cpl_result
from worker.fit import analyze_fit
from worker.result_payload import plain


def _delivery_profile() -> dict[str, Any]:
    return {
        "profile_id": "request:cpl-fit",
        "processing_metadata": {"common_ir_document_id": "ir:cpl-fit"},
        "comparison_profile": {
            "delivery_relations": [
                {
                    "delivery_relation_id": "delivery:1",
                    "actor": {"value_raw": "부산광역시"},
                    "role": {"value_raw": "주관"},
                    "actions": [
                        {"value_raw": "공고"},
                        {"value_raw": "접수"},
                    ],
                }
            ],
            "delivery_methods": [],
        },
        "request_context": {},
        "field_states": [],
    }


class _DeliveryLLM:
    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []

    async def generate_structured(self, **values: Any) -> Any:
        self.payloads.append(json.loads(values["messages"][1].content))
        payload = self.payloads[-1]
        relation = payload["relations"][0]
        return values["response_schema"].model_validate(
            {
                "relations": [
                    {
                        "relation_id": relation["relation_id"],
                        "status": "FIT",
                        "reason_code": None,
                        "left_fact_ids": ["delivery:1.actor"],
                        "right_fact_ids": [
                            "delivery:1.role",
                            "delivery:1.actions[0]",
                            "delivery:1.actions[1]",
                        ],
                    }
                ]
            }
        )


def test_fit_consumes_cpl_delivery_facts_and_keeps_existing_ids() -> None:
    profile = _delivery_profile()
    cpl = build_cpl_result(profile)
    profile["comparison_profile"]["delivery_relations"] = []

    llm = _DeliveryLLM()
    result = analyze_fit(cpl, llm, model_profile="test")

    fit6 = next(row for row in result.relations if row.relation_id.value == "FIT-6")
    assert [ref.fact_id for ref in fit6.left.facts] == ["delivery:1.actor"]
    assert [ref.fact_id for ref in fit6.right.facts] == [
        "delivery:1.role",
        "delivery:1.actions[0]",
        "delivery:1.actions[1]",
    ]
    assert fit6.status.value == "FIT"
    assert llm.payloads[0]["relations"][0]["right"][1]["fact_id"] == (
        "delivery:1.actions[0]"
    )


def test_member_index_is_internal_and_absent_from_public_cpl_json() -> None:
    cpl = build_cpl_result(_delivery_profile())
    delivery = next(
        item for item in cpl.items if item.field_code.value == "DELIVERY_SYSTEM"
    )
    relation_subfield = next(
        subfield
        for subfield in delivery.subfields
        if subfield.profile_field.endswith("delivery_relations")
    )
    assert [(fact.member, fact.member_index) for fact in relation_subfield.facts] == [
        ("actor", None),
        ("role", None),
        ("action", 0),
        ("action", 1),
    ]

    result_data = plain(delivery)
    assert "member_index" not in json.dumps(result_data, ensure_ascii=False)
    assert [
        (fact["fact_id"], fact["member"])
        for fact in result_data["subfields"][0]["facts"]
    ] == [(None, "actor"), (None, "role"), (None, "action"), (None, "action")]
