"""Focused alignment tests for support-cap and numeric-candidate KRW syntax."""

from __future__ import annotations

import pytest

from worker import vendor  # noqa: F401 - install vendored semantic_structuring paths

from semantic_structuring import source_selection
from semantic_structuring.explicit_support_cap_candidates import (
    extract_explicit_support_cap_candidates,
)
from semantic_structuring.models import CandidatePack
from semantic_structuring.source_selection import (
    SourceSelectionExtractionV02,
    SupportScaleFactRepairError,
    build_numeric_candidates,
    enumerate_numeric_candidate_spans,
    finalize_source_selection_v02,
    normalize_numeric_candidate_token,
    typed_repair_requirements_v02,
)


def _pack(text: str) -> CandidatePack:
    return CandidatePack.model_validate({
        "pack_id": "support-cap-numeric-pack",
        "notice_id": "PBLN-support-cap-numeric",
        "question": "support cap numeric normalization",
        "blocks": [{"block_id": "body[0]", "text": text, "relation": "candidate"}],
    })


def _selection(anchor_text: str) -> SourceSelectionExtractionV02:
    return SourceSelectionExtractionV02.model_validate({
        "notice_id": "PBLN-support-cap-numeric",
        "candidate_pack_id": "support-cap-numeric-pack",
        "component_decision": {
            "mode": "none",
            "no_component_reason": "one notice scope",
        },
        "support_components": [],
        "facts": [{
            "fact_id": "scale",
            "field_name": "support_scale",
            "status": "identified",
            "value_anchor": {
                "source_block_id": "body[0]",
                "anchor_text": anchor_text,
            },
        }],
    })


@pytest.mark.parametrize(
    ("source_text", "expected"),
    [
        ("3억 2천만원 500원", ("amount", 320_000_500, "KRW")),
        ("2천5백3십4개사", ("count", 2_534, "개사")),
        ("1,000원", ("amount", 1_000, "KRW")),
        ("10%", ("rate", 1_000, "BPS")),
        ("10개사", ("count", 10, "개사")),
    ],
)
def test_public_numeric_contract_preserves_compound_positional_and_simple_forms(
    source_text: str,
    expected: tuple[str, int, str],
) -> None:
    text = f"지원규모 {source_text} 확정"
    start = text.index(source_text)

    assert normalize_numeric_candidate_token(source_text) == expected
    assert enumerate_numeric_candidate_spans(text) == (
        (start, start + len(source_text), source_text),
    )
    assert [candidate.anchor_text for candidate in build_numeric_candidates(_pack(text))] == [
        source_text
    ]


@pytest.mark.parametrize(
    ("source_amount", "expected_krw"),
    [
        ("5천만원", 50_000_000),
        ("800백만원", 800_000_000),
        ("1억 5000만원", 150_000_000),
        ("5억", 500_000_000),
        ("1,000원", 1_000),
        ("5,000만원", 50_000_000),
        ("2천5백만원", 25_000_000),
        ("2천 5백만원", 25_000_000),
        ("2 천 5 백만 원", 25_000_000),
        ("2천500만원", 25_000_000),
        ("2천5백30만원", 25_300_000),
        ("2천5백3십4만원", 25_340_000),
        ("3억2천5백만원", 325_000_000),
        ("3억 2천 5백만원", 325_000_000),
        ("3 억 2 천 5 백만 원", 325_000_000),
        ("1억5천원", 100_005_000),
        ("1억 5천원", 100_005_000),
        ("1억 5000원", 100_005_000),
        ("1억 5천3백2십4원", 100_005_324),
        ("1 억 5 천 3 백 2 십 4 원", 100_005_324),
        ("3억 2천만원 500원", 320_000_500),
        ("3억2천만원500원", 320_000_500),
        ("3억 2천만 500원", 320_000_500),
        ("3억 2천만원 5백원", 320_000_500),
    ],
)
def test_korean_cap_amount_is_one_whole_candidate_and_normalizes_exactly(
    source_amount: str,
    expected_krw: int,
) -> None:
    text = f"지원금 최대 {source_amount}"
    pack = _pack(text)

    candidates = build_numeric_candidates(pack)
    assert [candidate.anchor_text for candidate in candidates] == [source_amount]
    assert [
        candidate.anchor_text
        for candidate in extract_explicit_support_cap_candidates(pack)
    ] == [f"최대 {source_amount}"]

    finalized = finalize_source_selection_v02(
        _selection(f"최대 {source_amount}"),
        pack,
        candidates,
    )
    measures = finalized[5][0].measures
    assert len(measures) == 1
    assert measures[0].upper_value == expected_krw


@pytest.mark.parametrize(
    "text",
    [
        "지원금 최대 5억원달러",
        "지원금 최대 1억 5000만원 USD",
        "지원금 최대 2천5백만원 USD",
        "지원금 최대 3억 2천5백만원달러",
    ],
)
def test_foreign_currency_suffix_cannot_leave_a_partial_krw_candidate(text: str) -> None:
    assert build_numeric_candidates(_pack(text)) == []


@pytest.mark.parametrize(
    "source_amount",
    [
        "3억 2천5백3십4백만원",
        "3억 2천 5백 3십 4백만원",
    ],
)
def test_unsupported_compound_tail_cannot_leave_an_inner_amount_candidate(
    source_amount: str,
) -> None:
    pack = _pack(f"지원금 최대 {source_amount}")

    assert build_numeric_candidates(pack) == []
    assert extract_explicit_support_cap_candidates(pack) == []


def test_eok_amount_does_not_consume_unrelated_recipient_count_as_won_tail() -> None:
    pack = _pack("지원금 최대 1억 5천명 선정")

    assert [candidate.anchor_text for candidate in build_numeric_candidates(pack)] == [
        "1억",
        "5천명",
    ]
    assert [
        candidate.anchor_text
        for candidate in extract_explicit_support_cap_candidates(pack)
    ] == ["최대 1억"]


@pytest.mark.parametrize(
    "text",
    [
        "지원금 최대 2천,500만원",
        "지원금 최대 3억 2천,500만원",
    ],
)
def test_malformed_unit_comma_prefix_cannot_leave_an_inner_amount_candidate(
    text: str,
) -> None:
    pack = _pack(text)

    assert build_numeric_candidates(pack) == []
    assert extract_explicit_support_cap_candidates(pack) == []


@pytest.mark.parametrize(
    "text",
    [
        "지원금 최대 1, 000원",
        "지원금 최대 1, 5000만원",
    ],
)
def test_whitespace_split_grouping_cannot_leave_a_numeric_suffix(text: str) -> None:
    pack = _pack(text)

    assert build_numeric_candidates(pack) == []
    assert extract_explicit_support_cap_candidates(pack) == []


@pytest.mark.parametrize(
    "text",
    [
        "지원금 최대 1 000원",
        "지원금 최대 1/000원",
        "지원금 최대 1_000원",
        "지원금 최대 12 345만원",
    ],
)
def test_other_split_number_syntax_cannot_leave_a_numeric_suffix(text: str) -> None:
    pack = _pack(text)

    assert build_numeric_candidates(pack) == []
    assert extract_explicit_support_cap_candidates(pack) == []


@pytest.mark.parametrize(
    "text",
    [
        "지원금 최대 1억\n5000만원",
        "지원금 최대 3억\n2천5백만원",
    ],
)
def test_line_broken_compound_amount_fails_closed_instead_of_using_prefix(
    text: str,
) -> None:
    pack = _pack(text)

    assert build_numeric_candidates(pack) == []
    assert extract_explicit_support_cap_candidates(pack) == []
    with pytest.raises(SupportScaleFactRepairError):
        finalize_source_selection_v02(_selection(text), pack, [])


def test_clause_punctuation_before_amount_remains_a_valid_numeric_boundary() -> None:
    pack = _pack("항목, 500만원 지원")

    assert [candidate.anchor_text for candidate in build_numeric_candidates(pack)] == [
        "500만원"
    ]


@pytest.mark.parametrize(
    ("text", "expected_amount"),
    [
        ("사업화 지원 5천만원", "5천만원"),
        ("지원 1억원", "1억원"),
    ],
)
def test_support_word_ending_in_won_is_not_a_money_unit_prefix(
    text: str,
    expected_amount: str,
) -> None:
    assert [candidate.anchor_text for candidate in build_numeric_candidates(_pack(text))] == [
        expected_amount
    ]


def test_strict_grouped_rate_scanner_and_numeric_normalization_stay_aligned() -> None:
    source_rate = "1,000%"
    text = f"지원금 최대 {source_rate}"
    pack = _pack(text)

    assert [candidate.anchor_text for candidate in build_numeric_candidates(pack)] == [
        source_rate
    ]
    assert [
        candidate.anchor_text
        for candidate in extract_explicit_support_cap_candidates(pack)
    ] == [f"최대 {source_rate}"]

    finalized = finalize_source_selection_v02(
        _selection(f"최대 {source_rate}"),
        pack,
        build_numeric_candidates(pack),
    )
    measures = finalized[5][0].measures
    assert len(measures) == 1
    assert measures[0].upper_value == 100_000


@pytest.mark.parametrize(
    ("text", "expected_cap_anchors"),
    [
        ("지원금은 매출 대비 최대 10%", ["최대 10%"]),
        ("지원금은 영업이익의 최대 10%", ["최대 10%"]),
        ("지원금은 매출 대비 약 10%", []),
    ],
)
def test_support_rate_with_financial_basis_remains_a_benefit(
    text: str,
    expected_cap_anchors: list[str],
) -> None:
    pack = _pack(text)
    candidates = build_numeric_candidates(pack)

    assert [candidate.anchor_text for candidate in candidates] == ["10%"]
    assert [
        candidate.anchor_text
        for candidate in extract_explicit_support_cap_candidates(pack)
    ] == expected_cap_anchors

    finalized = finalize_source_selection_v02(_selection(text), pack, candidates)
    measures = finalized[5][0].measures
    assert len(measures) == 1
    assert measures[0].upper_value == 1_000


@pytest.mark.parametrize(
    "text",
    [
        "선정규모 1, 000명 내외",
        "선정규모 1 000명 내외",
        "선정규모 1/000명 내외",
        "선정규모 1_000명 내외",
        "선정규모 2천5백3십4백명 내외",
    ],
)
def test_malformed_count_cannot_leave_a_suffix_numeric_candidate(text: str) -> None:
    pack = _pack(text)

    assert build_numeric_candidates(pack) == []
    assert extract_explicit_support_cap_candidates(pack) == []
    assert finalize_source_selection_v02(_selection(text), pack, [])[5] == []


@pytest.mark.parametrize(
    "text",
    [
        "지원규모 최대 5천명을 선정한다",
        "1팀당 최대 1천명을 지원",
    ],
)
def test_count_with_a_particle_is_never_inventoryed_as_krw_cap(text: str) -> None:
    assert extract_explicit_support_cap_candidates(_pack(text)) == []


def test_historical_cap_selected_by_model_requires_repair() -> None:
    text = "지난해 기업당 최대 5천만원 지원"
    pack = _pack(text)

    with pytest.raises(SupportScaleFactRepairError) as raised:
        finalize_source_selection_v02(
            _selection(text), pack, build_numeric_candidates(pack)
        )

    assert raised.value.repair_payload()[0]["reason"] == (
        "historical_support_cap_context"
    )
    assert typed_repair_requirements_v02(raised.value) == {
        "required_support_scale_anchors": [],
        "required_support_scale_fact_repairs": raised.value.repair_payload(),
        "required_exact_anchor_repairs": [],
        "required_list_item_regions": [],
        "do_not_restore_fact_ids": ["scale"],
    }


def test_historical_financial_eligibility_does_not_own_current_support_amount() -> None:
    text = "전년도 매출액 1억원 대비 10% 이상 증가 시 지원금 5천만원 지급"
    pack = _pack(text)

    finalized = finalize_source_selection_v02(
        _selection(text), pack, build_numeric_candidates(pack)
    )

    measures = finalized[5][0].measures
    assert len(measures) == 1
    assert measures[0].upper_value == 50_000_000


@pytest.mark.parametrize("source_amount", ["1,,000원", "5,00원"])
def test_malformed_grouping_is_rejected_by_scanner_numeric_and_finalizer(
    source_amount: str,
) -> None:
    text = f"지원금 최대 {source_amount}"
    pack = _pack(text)

    assert extract_explicit_support_cap_candidates(pack) == []
    assert build_numeric_candidates(pack) == []
    with pytest.raises(SupportScaleFactRepairError) as raised:
        finalize_source_selection_v02(_selection(text), pack, [])
    assert raised.value.repair_payload()[0]["reason"] == "malformed_numeric_grouping"


@pytest.mark.parametrize(
    ("text", "truncated_anchor"),
    [
        (
            "기업당 최대 5천만원, 총사업비 1억원",
            "기업당 최대 5천만원, 총사업비 1억",
        ),
        (
            "기업당 최대 5천만원 자부담 10%",
            "기업당 최대 5천만원 자부담 10",
        ),
    ],
)
def test_partially_selected_unrelated_numeric_cannot_discharge_cap(
    text: str,
    truncated_anchor: str,
) -> None:
    pack = _pack(text)

    with pytest.raises(SupportScaleFactRepairError) as raised:
        finalize_source_selection_v02(
            _selection(truncated_anchor),
            pack,
            build_numeric_candidates(pack),
        )

    assert raised.value.repair_payload()[0]["reason"] == (
        "support_cap_span_contains_unrelated_numeric"
    )
    assert raised.value.required_support_scale_anchors() == [{
        "source_block_id": "body[0]",
        "anchor_text": "기업당 최대 5천만원",
    }]


def test_broad_cap_with_compound_unrelated_amount_requires_typed_repair_and_cap() -> None:
    text = "총사업비 800백만원 중 지원금 최대 5천만원"
    pack = _pack(text)

    with pytest.raises(SupportScaleFactRepairError) as raised:
        finalize_source_selection_v02(
            _selection(text),
            pack,
            build_numeric_candidates(pack),
        )

    assert raised.value.repair_payload()[0]["reason"] == (
        "support_cap_span_contains_unrelated_numeric"
    )
    assert raised.value.required_support_scale_anchors() == [{
        "source_block_id": "body[0]",
        "anchor_text": "최대 5천만원",
    }]


def test_cap_claim_fails_closed_when_numeric_catalog_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = "지원금 최대 5천만원"
    pack = _pack(text)
    monkeypatch.setattr(source_selection, "build_numeric_candidates", lambda _pack: [])

    with pytest.raises(SupportScaleFactRepairError) as raised:
        finalize_source_selection_v02(_selection(text), pack, [])

    assert raised.value.required_support_scale_anchors() == [{
        "source_block_id": "body[0]",
        "anchor_text": "최대 5천만원",
    }]
