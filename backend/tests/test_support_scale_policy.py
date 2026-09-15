"""Shared literal support-scale scope policy regressions.

The policy is intentionally lexical.  It preserves only a source-visible
``<known unit>당/별`` marker, and conflicting or unknown units fail closed.
"""

from __future__ import annotations

import pytest

from worker import vendor  # noqa: F401 - installs vendored contract paths

from semantic_structuring.models import CandidatePack
from semantic_structuring.profile_v02 import AggregationScope
from semantic_structuring.source_selection import (
    SourceSelectionExtractionV02,
    build_numeric_candidates,
    derive_request_support_scale_measures_v012,
    derive_support_scale_measures_v02,
    materialize_evidence,
    parse_support_scale_scope,
)
from semantic_structuring.support_scale_policy import explicit_per_unit_scope


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1개社  당 최대 35,000천원", "COMPANY"),
        ("참여기업별 최대 1억원", "COMPANY"),
        ("업체 당 최대 5천만원", "COMPANY"),
        ("회사별 최대 5천만원", "COMPANY"),
        ("개사 당 최대 5천만원", "COMPANY"),
        ("팀별 2백만원", "TEAM"),
        ("개팀 당 2백만원", "TEAM"),
        ("1인 당 월 10만원", "PERSON"),
        ("개인별 월 10만원", "PERSON"),
        ("명별 월 10만원", "PERSON"),
        ("사람 당 월 10만원", "PERSON"),
        ("과제 당 최대 5천만원", "PROJECT"),
        ("개과제별 최대 5천만원", "PROJECT"),
        ("프로젝트 당 최대 5천만원", "PROJECT"),
    ],
)
def test_explicit_per_unit_scope_normalizes_observed_aliases_and_spacing(
    text: str,
    expected: str,
) -> None:
    assert explicit_per_unit_scope(text) == expected
    assert parse_support_scale_scope(text) == (
        expected,
        AggregationScope.PER_UNIT,
    )


@pytest.mark.parametrize(
    "text",
    [
        "센터별 최대 1억원",
        "기업당 또는 팀당 최대 1억원",
        "과제별 또는 1인당 최대 1억원",
        "법인별 최대 1억원",
        "총 1억원",
    ],
)
def test_explicit_per_unit_scope_fails_closed_for_unknown_or_conflicting_units(
    text: str,
) -> None:
    assert explicit_per_unit_scope(text) is None
    if not text.startswith("총"):
        assert parse_support_scale_scope(text) == (None, None)


def test_shared_parser_keeps_total_and_person_filter_contracts() -> None:
    assert parse_support_scale_scope("총 지원규모 1억원") == (
        None,
        AggregationScope.TOTAL,
    )
    assert parse_support_scale_scope(
        "1인별 최대 10만원",
        allow_person=False,
    ) == (None, None)


def test_existing_projection_reuses_alias_policy() -> None:
    pack = CandidatePack.model_validate({
        "pack_id": "existing-alias-scope",
        "notice_id": "PBLN-existing-alias-scope",
        "question": "existing alias scope",
        "blocks": [{
            "block_id": "body[0]",
            "text": "참여기업별 최대 1억원",
            "relation": "candidate",
        }],
    })
    extraction = SourceSelectionExtractionV02.model_validate({
        "notice_id": pack.notice_id,
        "candidate_pack_id": pack.pack_id,
        "component_decision": {
            "mode": "none",
            "no_component_reason": "one support item",
        },
        "support_components": [],
        "facts": [{
            "fact_id": "scale:company-cap",
            "field_name": "support_scale",
            "status": "identified",
            "value_anchor": {
                "source_block_id": "body[0]",
                "anchor_text": "참여기업별 최대 1억원",
            },
        }],
    })
    evidence = materialize_evidence(extraction, pack)
    value_sources = {
        row.fact_id: row.value_source
        for row in evidence
        if row.value_source is not None
    }

    projections = derive_support_scale_measures_v02(
        extraction,
        build_numeric_candidates(pack),
        value_sources,
    )

    assert projections[0].measures[0].applies_per == "COMPANY"
    assert projections[0].measures[0].aggregation_scope == AggregationScope.PER_UNIT


def _request_measure_for_adjacent_label(block_text: str):
    value_raw = "최대 1억원"
    value_start = block_text.index(value_raw)
    pack = CandidatePack.model_validate({
        "pack_id": "request-adjacent-alias-scope",
        "notice_id": "request-adjacent-alias-scope",
        "question": "request adjacent alias scope",
        "blocks": [{
            "block_id": "request[0]",
            "text": block_text,
            "relation": "candidate",
        }],
    })
    projections = derive_request_support_scale_measures_v012(
        [{
            "fact_id": "scale:company-cap",
            "value_raw": value_raw,
            "value_source": {
                "source_block_id": "request[0]",
                "start_char": value_start,
                "end_char": value_start + len(value_raw),
                "text_basis": "common_ir_v1_candidate_pack",
            },
        }],
        build_numeric_candidates(pack),
        source_block_texts={"request[0]": block_text},
    )
    return projections[0].measures[0]


def _request_measure_for_full_span(value_raw: str):
    pack = CandidatePack.model_validate({
        "pack_id": "request-full-span-scope",
        "notice_id": "request-full-span-scope",
        "question": "request full-span scope",
        "blocks": [{
            "block_id": "request[0]",
            "text": value_raw,
            "relation": "candidate",
        }],
    })
    projections = derive_request_support_scale_measures_v012(
        [{
            "fact_id": "scale:full-span",
            "value_raw": value_raw,
            "value_source": {
                "source_block_id": "request[0]",
                "start_char": 0,
                "end_char": len(value_raw),
                "text_basis": "common_ir_v1_candidate_pack",
            },
        }],
        build_numeric_candidates(pack),
        source_block_texts={"request[0]": value_raw},
    )
    return projections[0].measures[0]


@pytest.mark.parametrize(
    ("value_raw", "expected"),
    [
        ("기업 당 최대 1억원", "COMPANY"),
        ("참여기업별 최대 1억원", "COMPANY"),
        ("업체 당 최대 1억원", "COMPANY"),
        ("개인별 최대 1억원", "PERSON"),
    ],
)
def test_request_full_span_reuses_shared_scope_policy(
    value_raw: str,
    expected: str,
) -> None:
    measure = _request_measure_for_full_span(value_raw)
    assert measure.applies_per == expected
    assert measure.aggregation_scope == AggregationScope.PER_UNIT


def test_request_full_span_fails_closed_for_conflicting_scopes() -> None:
    measure = _request_measure_for_full_span("기업당 또는 팀당 최대 1억원")
    assert measure.applies_per is None
    assert measure.aggregation_scope is None


def test_request_adjacent_label_reuses_alias_policy_inside_bounded_grammar() -> None:
    measure = _request_measure_for_adjacent_label(
        "- 참여기업별 지원 한도: 최대 1억원",
    )
    assert measure.applies_per == "COMPANY"
    assert measure.aggregation_scope == AggregationScope.PER_UNIT


def test_request_adjacent_label_fails_closed_for_conflicting_scopes() -> None:
    measure = _request_measure_for_adjacent_label(
        "- 기업당 또는 팀당 한도: 최대 1억원",
    )
    assert measure.applies_per is None
    assert measure.aggregation_scope is None


@pytest.mark.parametrize(
    "block_text",
    [
        "- 올해 참여기업별 지원 한도: 최대 1억원",
        "- 기업당 한도, 참고 금액: 최대 1억원",
    ],
)
def test_request_adjacent_scope_never_escapes_its_closed_label_clause(
    block_text: str,
) -> None:
    measure = _request_measure_for_adjacent_label(block_text)
    assert measure.applies_per is None
    assert measure.aggregation_scope is None
