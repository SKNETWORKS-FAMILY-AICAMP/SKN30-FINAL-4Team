"""Shared numeric-projection scope and Request locator regressions."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

from worker import vendor  # noqa: F401 - installs vendored contract paths

from semantic_structuring.models import CandidatePack
from semantic_structuring.profile_v02 import validate_profile_v02
from semantic_structuring.source_selection import (
    SourceSelectionExtractionV02,
    build_numeric_candidates,
    derive_request_support_scale_measures_v012,
    derive_support_scale_measures_v02,
    materialize_evidence,
    parse_support_scale_scope,
)
from semantic_structuring.request_profile_v012 import (
    RequestSourceSelectionV012,
    _validate_request_profile,
    assemble_request_profile_v012,
    build_request_candidate_pack,
)


def test_shared_scope_parser_preserves_existing_scopes_and_adds_project() -> None:
    assert parse_support_scale_scope("기업당 최대 1,000만원")[0] == "COMPANY"
    assert parse_support_scale_scope("과제당 최대 1,000만원")[0] == "PROJECT"
    assert parse_support_scale_scope("프로젝트당 최대 1,000만원")[0] == "PROJECT"
    assert parse_support_scale_scope("팀당 최대 1,000만원")[0] == "TEAM"
    assert parse_support_scale_scope("1인당 최대 1,000만원")[0] == "PERSON"
    assert parse_support_scale_scope("총 지원규모 1억원")[1].value == "TOTAL"
    # The parser can still be narrowed at a downstream comparison boundary,
    # but the Profile producer preserves every literal source scope.
    assert parse_support_scale_scope("1인당 최대 1,000만원", allow_person=False)[0] is None
    assert parse_support_scale_scope("총 지원규모 1억원", allow_total=False)[1] is None


def test_existing_projection_adds_project_scope_without_changing_derivation_rules() -> None:
    pack = CandidatePack.model_validate({
        "pack_id": "project-scope-pack",
        "notice_id": "PBLN-project-scope",
        "question": "project scope",
        "blocks": [{
            "block_id": "body[0]",
            "text": "과제당 최대 1.5억원",
            "relation": "candidate",
        }],
    })
    extraction = SourceSelectionExtractionV02.model_validate({
        "notice_id": "PBLN-project-scope",
        "candidate_pack_id": pack.pack_id,
        "component_decision": {"mode": "none", "no_component_reason": "one support item"},
        "facts": [{
            "fact_id": "scale:project-cap",
            "field_name": "support_scale",
            "status": "identified",
            "value_anchor": {"source_block_id": "body[0]", "anchor_text": "과제당 최대 1.5억원"},
        }],
    })
    evidence = materialize_evidence(extraction, pack)
    sources = {row.fact_id: row.value_source for row in evidence if row.value_source is not None}
    projections = derive_support_scale_measures_v02(
        extraction, build_numeric_candidates(pack), sources
    )

    measure = projections[0].measures[0]
    assert measure.measure_role.value == "support_limit"
    assert measure.upper_value == 150_000_000
    assert measure.applies_per == "PROJECT"
    assert measure.aggregation_scope is not None
    assert measure.aggregation_scope.value == "PER_UNIT"


def test_request_projection_rejects_invalid_raw_fact_locator() -> None:
    pack = CandidatePack.model_validate({
        "pack_id": "request-locator-pack",
        "notice_id": "request-locator",
        "question": "request locator",
        "blocks": [{
            "block_id": "request[0]",
            "text": "기업당 최대 1,000만원",
            "relation": "candidate",
        }],
    })
    projections = derive_request_support_scale_measures_v012(
        [{
            "fact_id": "scale:wrong-location",
            "value_raw": "기업당 최대 1,000만원",
            # The value itself is plausible, but the exact source locator is
            # not. It must not produce a numerical projection.
            "value_source": {
                "source_block_id": "request[0]",
                "start_char": 1,
                "end_char": len("기업당 최대 1,000만원") + 1,
                "text_basis": "common_ir_v1_candidate_pack",
            },
        }],
        build_numeric_candidates(pack),
        source_block_texts={block.block_id: block.text for block in pack.blocks},
    )
    assert projections == []


def test_request_projection_preserves_explicit_person_and_total_scope() -> None:
    pack = CandidatePack.model_validate({
        "pack_id": "request-scope-pack",
        "notice_id": "request-scope",
        "question": "request scope",
        "blocks": [{
            "block_id": "request[0]",
            "text": "인당 최대 100만원, 총 지원규모 1억원",
            "relation": "candidate",
        }],
    })
    text = pack.blocks[0].text
    projections = derive_request_support_scale_measures_v012(
        [{
            "fact_id": "scale:person-and-total",
            "value_raw": text,
            "value_source": {
                "source_block_id": "request[0]",
                "start_char": 0,
                "end_char": len(text),
                "text_basis": "common_ir_v1_candidate_pack",
            },
        }],
        build_numeric_candidates(pack),
        source_block_texts={block.block_id: block.text for block in pack.blocks},
    )
    measures = projections[0].measures
    assert {(measure.applies_per, measure.aggregation_scope.value if measure.aggregation_scope else None) for measure in measures} == {
        ("PERSON", "PER_UNIT"),
        (None, "TOTAL"),
    }


def test_request_projection_recognizes_suffix_limit_marker_in_same_clause() -> None:
    pack = CandidatePack.model_validate({
        "pack_id": "request-suffix-limit-pack",
        "notice_id": "request-suffix-limit",
        "question": "request suffix limit",
        "blocks": [{
            "block_id": "request[0]",
            "text": "과제당 5억원 이내",
            "relation": "candidate",
        }],
    })
    text = pack.blocks[0].text
    projections = derive_request_support_scale_measures_v012(
        [{
            "fact_id": "scale:suffix-limit",
            "value_raw": text,
            "value_source": {
                "source_block_id": "request[0]",
                "start_char": 0,
                "end_char": len(text),
                "text_basis": "common_ir_v1_candidate_pack",
            },
        }],
        build_numeric_candidates(pack),
        source_block_texts={block.block_id: block.text for block in pack.blocks},
    )
    measure = projections[0].measures[0]
    assert (measure.measure_role.value, measure.comparator.value) == ("support_limit", "lte")
    assert (measure.upper_value, measure.applies_per, measure.aggregation_scope.value) == (
        500_000_000,
        "PROJECT",
        "PER_UNIT",
    )


def _request_measures_for_text(text: str) -> list:
    pack = CandidatePack.model_validate({
        "pack_id": f"request-local-{text}",
        "notice_id": "request-local",
        "question": "request local semantics",
        "blocks": [{"block_id": "request[0]", "text": text, "relation": "candidate"}],
    })
    projections = derive_request_support_scale_measures_v012(
        [{
            "fact_id": "scale:local",
            "value_raw": text,
            "value_source": {
                "source_block_id": "request[0]",
                "start_char": 0,
                "end_char": len(text),
                "text_basis": "common_ir_v1_candidate_pack",
            },
        }],
        build_numeric_candidates(pack),
        source_block_texts={block.block_id: block.text for block in pack.blocks},
    )
    return projections[0].measures


def test_request_projection_assigns_markers_to_their_own_numeric_candidate() -> None:
    amount, rate = _request_measures_for_text("기업당 1억원 지원(자부담 10% 이내)")
    assert (amount.measure_role.value, amount.comparator.value, amount.applies_per) == (
        "support_amount", "eq", "COMPANY",
    )
    assert (rate.measure_role.value, rate.comparator.value, rate.applies_per) == (
        "support_rate", "lte", None,
    )

    amount, rate = _request_measures_for_text("총 사업비 1억원 중 최대 70% 지원")
    assert (amount.measure_role.value, amount.comparator.value, amount.aggregation_scope.value) == (
        "support_amount", "eq", "TOTAL",
    )
    assert (rate.measure_role.value, rate.comparator.value, rate.aggregation_scope) == (
        "support_rate", "lte", None,
    )

    amount, rate = _request_measures_for_text("1억원 최대 지원비율 70%")
    assert (amount.measure_role.value, amount.comparator.value) == ("support_amount", "eq")
    assert (rate.measure_role.value, rate.comparator.value) == ("support_rate", "lte")

    count, amount = _request_measures_for_text("10개사 내외 기업당 최대 1억원")
    assert (count.measure_role.value, count.comparator.value, count.applies_per) == (
        "selection_capacity", "approx", None,
    )
    assert (amount.measure_role.value, amount.comparator.value, amount.applies_per) == (
        "support_limit", "lte", "COMPANY",
    )


def test_request_projection_normalizes_decimal_amount_and_rate_exactly() -> None:
    amount, rate = _request_measures_for_text("과제당 1.5억원 이내, 자부담 0.29%")
    assert (amount.upper_value, amount.comparator.value, amount.applies_per) == (
        150_000_000,
        "lte",
        "PROJECT",
    )
    assert (rate.upper_value, rate.comparator.value, rate.unit) == (29, "eq", "BPS")


def test_request_projection_assigns_approx_marker_by_grammar_direction() -> None:
    prefix_amount = _request_measures_for_text("약 1억원")[0]
    suffix_amount = _request_measures_for_text("1억원 내외")[0]
    assert prefix_amount.comparator.value == "approx"
    assert suffix_amount.comparator.value == "approx"


def test_request_projection_ignores_grouping_comma_as_clause_boundary() -> None:
    suffix_limit = _request_measures_for_text("기업당 1,000만원 이내")[0]
    suffix_approx = _request_measures_for_text("1,000만원 내외")[0]
    neutral_limit = _request_measures_for_text("1,000만원 한도")[0]
    assert (suffix_limit.comparator.value, suffix_limit.measure_role.value) == (
        "lte",
        "support_limit",
    )
    assert suffix_approx.comparator.value == "approx"
    assert (neutral_limit.comparator.value, neutral_limit.measure_role.value) == (
        "lte",
        "support_limit",
    )


def test_request_profile_rejects_mutated_normalized_projection() -> None:
    examples = (
        Path(__file__).resolve().parents[1]
        / "vendor"
        / "portable_existing_request_profiles_20260831"
        / "examples"
        / "request"
    )
    document = json.loads((examples / "common_ir_v1.json").read_text(encoding="utf-8"))
    selection = json.loads(
        (examples / "source_selection_v012.json").read_text(encoding="utf-8")
    )["selection"]
    pack = build_request_candidate_pack(document)
    profile = assemble_request_profile_v012(
        document, pack, RequestSourceSelectionV012.model_validate(selection)
    )
    tampered = deepcopy(profile)
    tampered["derived_projections"][0]["measures"][0]["upper_value"] += 1
    issues = _validate_request_profile(tampered, pack)
    assert any("must exactly equal deterministic Raw-Fact re-derivation" in issue for issue in issues)


def test_numeric_candidate_rejects_malformed_multi_dot_amount() -> None:
    pack = CandidatePack.model_validate({
        "pack_id": "malformed-decimal-pack",
        "notice_id": "malformed-decimal",
        "question": "malformed decimal",
        "blocks": [{
            "block_id": "request[0]",
            "text": "기준일은 2026.09.13억원이다",
            "relation": "candidate",
        }],
    })
    assert build_numeric_candidates(pack) == []


def test_numeric_candidate_rejects_partial_full_width_grouped_amount() -> None:
    malformed_values = (
        "1，000만원",
        "1,,500만원",
        "1，，500만원",
        "1,，500만원",
        "1,,000만원",
    )
    for index, value in enumerate(malformed_values):
        pack = CandidatePack.model_validate({
            "pack_id": f"invalid-grouping-pack-{index}",
            "notice_id": "invalid-grouping",
            "question": "unsupported grouping punctuation",
            "blocks": [{
                "block_id": "request[0]",
                "text": f"기업당 {value}",
                "relation": "candidate",
            }],
        })
        assert build_numeric_candidates(pack) == []


def test_numeric_candidate_accepts_no_space_comma_delimited_amounts() -> None:
    pack = CandidatePack.model_validate({
        "pack_id": "comma-delimited-pack",
        "notice_id": "comma-delimited",
        "question": "comma delimited",
        "blocks": [{
            "block_id": "request[0]",
            "text": "총 1억원,기업당 2억원",
            "relation": "candidate",
        }],
    })
    assert [candidate.anchor_text for candidate in build_numeric_candidates(pack)] == [
        "1억원", "2억원"
    ]


def test_existing_profile_validator_accepts_legacy_numeric_candidate_v1() -> None:
    profile = {
        "schema_version": "existing_program_profile/v0.2",
        "source_profile_id": "profile:legacy-v1",
        "source_documents": [{"common_ir": {
            "document_id": "hwp:legacy-v1",
            "schema_version": "common_ir_v1",
            "source_kind": "hwp",
            "source_sha256": "sha256",
            "source_location": "legacy.hwp",
        }}],
        "comparison_profile": {"support_scale": [{
            "fact_id": "scale:legacy",
            "field_name": "support_scale",
            "value_raw": "기업당 최대 100만원",
            "value_source": {
                "source_block_id": "body[0]",
                "start_char": 0,
                "end_char": len("기업당 최대 100만원"),
                "text_basis": "common_ir_v1_candidate_pack",
            },
        }]},
        "support_components": [],
        "derived_projections": [{
            "projection_type": "support_scale_measures",
            "source_fact_ids": ["scale:legacy"],
            "measures": [{
                "measure_type": "amount",
                "measure_role": "support_limit",
                "lower_value": None,
                "upper_value": 1_000_000,
                "unit": "KRW",
                "comparator": "lte",
                "source_fact_id": "scale:legacy",
                "source_numeric_candidate_id": "body[0]#num[0]",
                "applies_per": "COMPANY",
                "calculation_basis": None,
                "frequency": None,
                "aggregation_scope": "PER_UNIT",
            }],
            "status": "identified",
        }],
        "processing_metadata": {"derived_projection_producers": {
            "support_scale_measures": {"numeric_candidate_extractor_version": "numeric_candidate_v1"}
        }},
    }
    assert validate_profile_v02(profile, {"body[0]": "기업당 최대 100만원"}) == []
