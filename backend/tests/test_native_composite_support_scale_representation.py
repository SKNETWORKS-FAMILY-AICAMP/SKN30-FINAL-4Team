"""Representation-invariance regression for native composite scale evidence."""

from __future__ import annotations

import pytest

from worker import vendor  # noqa: F401 - installs the vendored package

from semantic_structuring.models import CandidatePack
from semantic_structuring.source_selection import (
    SourceSelectionExtractionV02,
    build_numeric_candidates,
    finalize_source_selection_v02,
)


@pytest.mark.parametrize(
    "first",
    [
        "매출액 또는 영업이익 10% 이상 감소기업:",
        "지원대상: 매출액 또는 영업이익 10% 이상 감소기업에",
    ],
)
def test_financial_condition_plus_explicit_benefit_survives_native_composition(
    first: str,
) -> None:
    second = "1% 추가 지원"
    composite_text = f"{first} {second}"
    atomic = [
        {
            "block_id": block_id,
            "text": text,
            "relation": "candidate",
            "block_kind": "paragraph",
            "section_id": "main_notice",
            "source_order": order,
            "source_occurrence_ids": [f"occ:{block_id}"],
            "common_ir_block_id": f"ir:{block_id}",
            "common_ir_occurrence_ids": [f"occ:{block_id}"],
        }
        for order, (block_id, text) in enumerate((("condition", first), ("benefit", second)))
    ]
    composite = {
        "block_id": "condition-benefit",
        "text": composite_text,
        "relation": "candidate",
        "block_kind": "native_composite",
        "section_id": "main_notice",
        "source_order": 0,
        "source_occurrence_ids": ["occ:condition", "occ:benefit"],
        "common_ir_block_id": "ir:condition",
        "common_ir_occurrence_ids": ["occ:condition", "occ:benefit"],
        "source_spans": [
            {
                "source_block_id": "condition",
                "exact_text": first,
                "start_char": 0,
                "end_char": len(first),
                "separator_after": " ",
                "source_order": 0,
                "section_id": "main_notice",
                "common_ir_block_id": "ir:condition",
                "common_ir_occurrence_ids": ["occ:condition"],
            },
            {
                "source_block_id": "benefit",
                "exact_text": second,
                "start_char": 0,
                "end_char": len(second),
                "separator_after": "",
                "source_order": 1,
                "section_id": "main_notice",
                "common_ir_block_id": "ir:benefit",
                "common_ir_occurrence_ids": ["occ:benefit"],
            },
        ],
    }
    pack = CandidatePack.model_validate({
        "pack_id": "native-financial-benefit-pack",
        "notice_id": "PBLN-native-financial-benefit",
        "question": "native representation regression",
        "generator": "semantic_structuring.native_exact_transform",
        "generator_version": "native-exact-v2",
        "common_ir_document_id": "hwpx:PBLN-native-financial-benefit",
        "parent_pack_id": "source-pack",
        "parent_generator": "common_ir_v1_candidate_pack",
        "parent_generator_version": "1.1.0",
        "blocks": [*atomic, composite],
    })
    selection = SourceSelectionExtractionV02.model_validate({
        "notice_id": pack.notice_id,
        "candidate_pack_id": pack.pack_id,
        "component_decision": {"mode": "none", "no_component_reason": "one notice scope"},
        "support_components": [],
        "facts": [{
            "fact_id": "conditional-rate",
            "field_name": "support_scale",
            "status": "identified",
            "value_anchor": {
                "source_block_id": "condition-benefit",
                "anchor_text": composite_text,
            },
        }],
    })

    finalized = finalize_source_selection_v02(
        selection, pack, build_numeric_candidates(pack), require_support_cap_completeness=False
    )

    assert [fact.fact_id for fact in finalized[0].facts] == ["conditional-rate"]
    measures = finalized[5][0].measures
    assert len(measures) == 1
    assert measures[0].source_numeric_candidate_id == "condition-benefit#num[1]"
    assert measures[0].measure_type.value == "rate"
    assert measures[0].measure_role.value == "support_rate"
    assert measures[0].comparator.value == "eq"
    assert measures[0].lower_value == 100
    assert measures[0].upper_value == 100
