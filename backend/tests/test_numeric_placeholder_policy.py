"""Zero-filled recipient-count placeholders must not become support scales."""

from __future__ import annotations

import pytest

from worker import vendor  # noqa: F401 - installs vendored contract paths

from semantic_structuring.models import CandidatePack
from semantic_structuring.source_selection import (
    SourceSelectionExtractionV02,
    SupportScaleFactRepairError,
    build_numeric_candidates,
    contains_numeric_placeholder,
    finalize_source_selection_v02,
    is_numeric_placeholder,
)


def _pack(text: str) -> CandidatePack:
    return CandidatePack.model_validate({
        "pack_id": "numeric-placeholder-pack",
        "notice_id": "PBLN-numeric-placeholder",
        "question": "numeric placeholder policy",
        "blocks": [{
            "block_id": "body[0]",
            "text": text,
            "relation": "candidate",
        }],
    })


def _selection(anchor_text: str) -> SourceSelectionExtractionV02:
    return SourceSelectionExtractionV02.model_validate({
        "notice_id": "PBLN-numeric-placeholder",
        "candidate_pack_id": "numeric-placeholder-pack",
        "component_decision": {
            "mode": "none",
            "no_component_reason": "one support item",
        },
        "support_components": [],
        "facts": [{
            "fact_id": "scale:placeholder",
            "field_name": "support_scale",
            "status": "identified",
            "value_anchor": {
                "source_block_id": "body[0]",
                "anchor_text": anchor_text,
            },
        }],
    })


def test_zero_filled_recipient_placeholders_do_not_become_numeric_candidates() -> None:
    pack = _pack("00개사, 0명, 0개소, 400개사")

    assert [candidate.anchor_text for candidate in build_numeric_candidates(pack)] == [
        "400개사",
    ]


def test_nonzero_selection_capacity_finalizes_as_a_count_measure() -> None:
    text = "선정규모 400개사"
    pack = _pack(text)

    finalized = finalize_source_selection_v02(
        _selection(text),
        pack,
        build_numeric_candidates(pack),
        require_support_cap_completeness=False,
    )

    measures = finalized[5][0].measures
    assert len(measures) == 1
    assert measures[0].measure_type.value == "count"
    assert measures[0].measure_role.value == "selection_capacity"
    assert (measures[0].lower_value, measures[0].upper_value, measures[0].unit) == (
        400,
        400,
        "개사",
    )


@pytest.mark.parametrize(
    "text",
    [
        "선정규모 1, 000명",
        "선정규모 1， 000명",
        "선정규모 1. 000명",
        "선정규모 1． 000명",
        "선정규모 1 000명",
        "선정규모 1_000명",
    ],
)
def test_malformed_count_suffixes_are_not_numeric_placeholders(text: str) -> None:
    pack = _pack(text)

    assert not contains_numeric_placeholder(text)
    assert build_numeric_candidates(pack) == []
    assert finalize_source_selection_v02(
        _selection(text),
        pack,
        [],
        require_support_cap_completeness=False,
    )[5] == []


def test_zero_krw_amount_is_outside_recipient_placeholder_policy() -> None:
    text = "지원금 0원"

    assert not is_numeric_placeholder(text)
    assert not contains_numeric_placeholder(text)


@pytest.mark.parametrize("punctuation", [",", "，", ".", "．"])
@pytest.mark.parametrize("spacing", ["", " ", "  "])
def test_punctuation_before_standalone_zero_count_remains_a_placeholder(
    punctuation: str,
    spacing: str,
) -> None:
    assert contains_numeric_placeholder(f"선정규모{punctuation}{spacing}0명")


@pytest.mark.parametrize("punctuation", [",", "，", ".", "．"])
@pytest.mark.parametrize("spacing", ["", " ", "  "])
def test_malformed_punctuation_count_suffix_is_not_a_placeholder(
    punctuation: str,
    spacing: str,
) -> None:
    assert not contains_numeric_placeholder(f"선정규모 1{punctuation}{spacing}000명")


@pytest.mark.parametrize("spacing", ["", " ", "  "])
def test_malformed_horizontal_space_count_suffix_is_not_a_placeholder(
    spacing: str,
) -> None:
    assert not contains_numeric_placeholder(f"선정규모 1{spacing}000명")


@pytest.mark.parametrize(
    "placeholder",
    ["선정규모 00개사", "모집인원 0명", "지원규모 0개소"],
)
def test_zero_filled_recipient_placeholder_requires_typed_scale_repair(
    placeholder: str,
) -> None:
    pack = _pack(placeholder)

    with pytest.raises(SupportScaleFactRepairError) as raised:
        finalize_source_selection_v02(
            _selection(placeholder),
            pack,
            build_numeric_candidates(pack),
            require_support_cap_completeness=False,
        )

    assert raised.value.repair_payload() == [{
        "fact_id": "scale:placeholder",
        "source_block_id": "body[0]",
        "reason": "numeric_placeholder",
        "numeric_candidate_count": 0,
        "derived_measure_count": 0,
    }]
    assert raised.value.do_not_restore_fact_ids == frozenset({"scale:placeholder"})
