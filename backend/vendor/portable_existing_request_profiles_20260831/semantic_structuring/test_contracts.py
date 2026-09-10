"""Small executable checks for the first semantic-structuring contract."""

import json
import tempfile
from copy import deepcopy
from pathlib import Path

from pydantic import ValidationError

from semantic_structuring.anchor_occurrence_resolver import AnchorOccurrenceRequest, resolve_anchor_occurrences
from semantic_structuring.models import (
    CandidatePack,
    FactField,
    MeasureSemanticRole,
    MeasureType,
    MeasureUnit,
    MoneyBasis,
    NormalizedMeasure,
    NumericComparator,
    SemanticExtraction,
)
from semantic_structuring.pipeline import _model_input, _openai_schema
from semantic_structuring.sectioning import block_section_ids, split_attachment_sections
from semantic_structuring.run_block_candidate_discovery_test import _section_scope_by_block
from semantic_structuring.candidate_assembly import restrict_a_pack_to_table_ids, table_parent_id
from semantic_structuring.notice_preparation import SectionScopeDecision, prepare_notice
from semantic_structuring.candidate_assembly import build_a_candidate_packs, build_combined_a_candidate_pack, build_routed_a_pack
from semantic_structuring.source_selection import (
    AmbiguousAnchorCorrectionError,
    AnchorCorrectionCandidatePrompt,
    AnchorCorrectionRequest,
    CorrectionResolverError,
    DeliveryRoleCanonical,
    EmptyRepairResponseError,
    SourceSelectionExtraction,
    SourceSelectionExtractionV02,
    apply_finalize_with_fallback_v02,
    build_anchor_correction_request,
    build_corrected_anchor_audit,
    build_numeric_candidates,
    classify_empty_repair_response_v02,
    derive_support_scale_measures_v02,
    materialize_components,
    materialize_evidence,
    memoize_anchor_correction_resolver,
    normalize_explicit_condition_variant_relations_v02,
    normalize_explicit_sequential_components_v02,
    preserve_prior_server_validated_facts_v02,
    resolve_value_anchor_with_occurrence_resolver,
    summarize_prior_candidate_v02,
    validate_component_structure_v02,
    validate_scale_measure_candidates_v02,
    validate_selection_quality_v02,
    validate_support_cap_completeness_v02,
)
from semantic_structuring.final_profile_assembler import assemble_final_profile, assemble_final_profile_v02
from semantic_structuring.fact_relationships import (
    FactRelationshipExtraction,
    apply_relationships,
    build_relationship_payload,
    validate_relationships,
)
from semantic_structuring.table_policy import resolve_table_policy
from semantic_structuring.validation import validate_extraction
from semantic_structuring.common_ir_v03 import project_common_ir_v03
from semantic_structuring.common_ir_notice import prepare_common_ir_notice
from semantic_structuring.common_ir_v1 import (
    apply_common_ir_v1_section_scopes,
    common_ir_v1_metadata,
    prepare_common_ir_v1,
)
from semantic_structuring.profile_v02 import (
    NUMERIC_CANDIDATE_EXTRACTOR_VERSION,
    MeasureRole,
    SupportFacetsProjection,
    SupportScaleMeasure,
    find_all_occurrences,
    validate_common_ir_lineage,
    validate_support_scale_measure_shape,
    materialize_value_source,
    validate_profile_v02,
)
from semantic_structuring.run_source_selection_test import trusted_common_ir_source_sha256
from semantic_structuring.evaluation_v02 import evaluate_profile_v02


def minimal_common_ir_lineage_source_documents(document_id: str = "hwp:PBLN-test", source_kind: str = "hwp") -> list[dict]:
    """Minimal valid v0.2 Common IR lineage for fixtures that predate it.

    v0.2 now requires ``source_documents`` to carry Common IR lineage on at
    least one document; production callers get this from
    ``common_ir_v1_metadata``.  Fixtures below construct raw profile dicts
    directly, so they need the same minimal shape rather than a loosened
    validator.
    """

    return [{
        "document_name": None, "format": source_kind, "source_url": None, "notice_detail_url": None,
        "common_ir": {
            "document_id": document_id,
            "schema_version": "common_ir_v1",
            "source_kind": source_kind,
            "source_sha256": "test-sha256",
            "source_location": "test://fixture",
        },
    }]


def stage_fixture() -> dict:
    return {
        "notice_id": "PBLN_000000000125612",
        "candidate_pack_id": "125612-stage-support",
        "support_components": [
            {
                "support_component_id": "stage-education",
                "component_kind": "stage_support",
                "name_raw": "1차 서류심사 합격자 교육 지원",
                "source_block_ids": ["body[44]"],
            }
        ],
        "facts": [
            {
                "fact_id": "fact-stage-capacity",
                "kind": "support_scale",
                "field_name": "support_scale",
                "value_raw": "1차 서류심사: 교육 참여자 20개팀 내외 선정",
                "source_block_ids": ["body[44]"],
                "status": "identified",
                "scope": "component",
                "support_component_id": "stage-education",
                "semantic_role": "selection_capacity",
                "normalized_value": {"quantity_type": "selection_capacity", "count": 20, "unit": "team"},
            }
        ],
    }


def stage_pack() -> CandidatePack:
    return CandidatePack.model_validate(
        {
            "pack_id": "125612-stage-support",
            "notice_id": "PBLN_000000000125612",
            "question": "교육 단계와 최종 지원 단계를 분리한다.",
            "blocks": [
                {
                    "block_id": "body[44]",
                    "text": "1차 서류심사: 교육 참여자 20개팀 내외 선정",
                    "relation": "candidate",
                }
            ],
        }
    )


def test_common_ir_v1_candidate_pack_provenance() -> None:
    """Common IR cell provenance survives projection, materialization, and assembly."""

    document = {
        "schema_version": "common_ir_v1",
        "document": {
            "document_id": "hwpx:PBLN-provenance", "source_kind": "hwpx",
            "artifact_role": "production",
            "provenance": {"source_sha256": "abc123", "source_location": "test://notice.hwpx"},
        },
        "blocks": [{
            "block_id": "hwpx:t0", "kind": "table", "structure_status": "explicit",
            "text": "최대 300만원", "text_occurrence_ids": ["occ:value"],
            "reading_order": 0, "page": None, "section_path": "", "boundary_markers": [],
            "occurrences": [{"occurrence_id": "occ:value", "text": "최대 300만원"}],
            "cells": [{
                "cell_id": "hwpx:t0:c0", "row_index": 0, "col_index": 0,
                "evidence_ids": ["occ:value"], "text_occurrence_ids": ["occ:value"],
            }],
            "provenance": {},
        }],
        "relations": [], "conflicts": [],
    }
    prepared = apply_common_ir_v1_section_scopes(document, [])
    pack, _ = build_routed_a_pack(prepared, [{
        "source_block_id": "hwpx:t0", "route_tags": ["funding_or_condition"],
        "table_disposition": "a_fact_candidate",
    }])
    assert pack is not None
    assert (pack.generator, pack.generator_version, pack.common_ir_document_id) == (
        "semantic_structuring.common_ir_v1", "1", "hwpx:PBLN-provenance",
    )
    cell = pack.blocks[0]
    assert (cell.common_ir_block_id, cell.common_ir_cell_id, cell.common_ir_occurrence_ids) == (
        "hwpx:t0", "hwpx:t0:c0", ("occ:value",),
    )
    selection = SourceSelectionExtractionV02.model_validate({
        "notice_id": "PBLN-provenance", "candidate_pack_id": pack.pack_id,
        "component_decision": {"mode": "none", "no_component_reason": "single fact"},
        "facts": [{
            "fact_id": "scale", "field_name": "support_scale", "status": "identified",
            "value_anchor": {"source_block_id": cell.block_id, "anchor_text": "최대 300만원"},
        }],
    })
    row = materialize_evidence(selection, pack)[0]
    assert row.source_blocks[0]["source_block_id"] == cell.block_id
    assert row.source_blocks[0]["common_ir_block_id"] == "hwpx:t0"
    assert row.source_blocks[0]["common_ir_cell_id"] == "hwpx:t0:c0"
    assert row.source_blocks[0]["common_ir_occurrence_ids"] == ["occ:value"]

    metadata = common_ir_v1_metadata(document)
    profile = assemble_final_profile_v02({
        "selection_contract": "v0.2_anchor",
        "selection": selection.model_dump(mode="json"),
        "source_block_texts": {cell.block_id: cell.text},
        "materialized_evidence": [{
            "fact_id": row.fact_id, "field_name": row.field_name.value,
            "status": row.status.value, "semantic_role": None, "subject_role": None,
            "source_blocks": row.source_blocks,
            "value_source": row.value_source.model_dump(mode="json"),
            "context_blocks": [], "organization_names": [], "organization_sources": [],
            "role_raw": None, "role_source_block_id": None, "role_source": None,
            "canonical_role": None, "primary_component_id": None,
            "applicability_component_ids": [], "modifies_fact_ids": [],
            "recipient_fact_ids": [], "basis_fact_ids": [],
        }],
        "materialized_components": [],
        "common_ir_identity": {
            "document_id": "hwpx:PBLN-provenance", "source_kind": "hwpx",
            "source_sha256": "abc123",
        },
        "candidate_pack_lineage": {
            "candidate_pack_id": pack.pack_id,
            "candidate_pack_generator": pack.generator,
            "candidate_pack_generator_version": pack.generator_version,
            "common_ir_document_id": pack.common_ir_document_id,
            "common_ir_source_sha256": "abc123",
            "text_basis": "common_ir_v1_candidate_pack",
        },
    }, metadata)
    evidence = profile["comparison_profile"]["support_scale"][0]["evidence"][0]
    assert evidence["source_block_id"] == cell.block_id
    assert evidence["common_ir_document_id"] == "hwpx:PBLN-provenance"
    assert evidence["common_ir_block_id"] == "hwpx:t0"
    assert evidence["common_ir_cell_id"] == "hwpx:t0:c0"
    assert evidence["common_ir_occurrence_ids"] == ["occ:value"]
    assert profile["processing_metadata"]["candidate_pack"]["candidate_pack_id"] == pack.pack_id


def main() -> None:
    test_common_ir_v1_candidate_pack_provenance()
    try:
        SupportFacetsProjection.model_validate({
            "projection_type": "support", "source_fact_ids": ["fact_1"], "status": "identified"
        })
    except ValidationError:
        pass
    else:
        raise AssertionError("support facet projection type must be literal support_facets")

    sequential_pack = CandidatePack.model_validate({
        "pack_id": "sequential-pack", "notice_id": "PBLN-sequential", "question": "stage", "blocks": [
            {"block_id": "body[0]", "text": "창업교육(1차 서류심사 합격자)", "relation": "candidate"},
            {"block_id": "body[1]", "text": "최종 선정 시 창업지원금 지원", "relation": "candidate"},
        ],
    })
    package_labeled_stages = SourceSelectionExtractionV02.model_validate({
        "notice_id": "PBLN-sequential", "candidate_pack_id": "sequential-pack",
        "component_decision": {"mode": "packages"},
        "support_components": [
            {"support_component_id": "education", "component_kind": "support_package", "source_block_ids": ["body[0]"], "table_block_ids": []},
            {"support_component_id": "final", "component_kind": "support_package", "source_block_ids": ["body[1]"], "table_block_ids": []},
        ],
        "facts": [],
    })
    try:
        validate_component_structure_v02(package_labeled_stages, sequential_pack)
    except ValueError as error:
        assert "component_kind=stage_support" in str(error)
    else:
        raise AssertionError("explicit sequential components must not be labelled support_package")
    normalized_stages, stage_changes = normalize_explicit_sequential_components_v02(
        package_labeled_stages, sequential_pack
    )
    assert normalized_stages.component_decision.mode.value == "stages"
    assert {component.component_kind.value for component in normalized_stages.support_components} == {"stage_support"}
    assert len(stage_changes) >= 2

    # An explicit support cap ("최대/한도/상한" + amount/rate/count) inside a
    # block the model already selected as evidence must not be silently
    # skipped: the server rejects the selection so a retry can add it.
    cap_pack = CandidatePack.model_validate({
        "pack_id": "cap-pack", "notice_id": "PBLN-cap", "question": "cap", "blocks": [
            {"block_id": "body[0]", "text": "인건비 300만원 지원 (최대 500만원, 6개월간)", "relation": "candidate"},
        ],
    })
    cap_component = {
        "support_component_id": "package-1", "component_kind": "support_package",
        "source_block_ids": ["body[0]"], "table_block_ids": [],
    }
    cap_amount_fact = {
        "fact_id": "fact-amount", "field_name": "support_scale",
        "value_anchor": {"source_block_id": "body[0]", "anchor_text": "300만원"},
        "status": "identified", "primary_component_id": "package-1",
    }
    cap_missing = SourceSelectionExtractionV02.model_validate({
        "notice_id": "PBLN-cap", "candidate_pack_id": "cap-pack",
        "component_decision": {"mode": "packages"},
        "support_components": [cap_component],
        "facts": [cap_amount_fact],
    })
    try:
        validate_support_cap_completeness_v02(cap_missing, cap_pack)
    except ValueError as error:
        assert "최대 500만원" in str(error)
    else:
        raise AssertionError("an explicit unclaimed support cap must be rejected")

    cap_fixed = SourceSelectionExtractionV02.model_validate({
        "notice_id": "PBLN-cap", "candidate_pack_id": "cap-pack",
        "component_decision": {"mode": "packages"},
        "support_components": [cap_component],
        "facts": [cap_amount_fact, {
            "fact_id": "fact-cap", "field_name": "support_scale",
            "value_anchor": {"source_block_id": "body[0]", "anchor_text": "최대 500만원"},
            "status": "identified", "primary_component_id": "package-1",
        }],
    })
    validate_support_cap_completeness_v02(cap_fixed, cap_pack)  # must not raise once selected

    # A support_scale anchor that captures only the bare numeral, without the
    # bound marker, does not preserve deterministic support_limit semantics
    # (comparator=lte); the completeness guard must still reject it.
    cap_bare_numeral = SourceSelectionExtractionV02.model_validate({
        "notice_id": "PBLN-cap", "candidate_pack_id": "cap-pack",
        "component_decision": {"mode": "packages"},
        "support_components": [cap_component],
        "facts": [cap_amount_fact, {
            "fact_id": "fact-cap-bare", "field_name": "support_scale",
            "value_anchor": {"source_block_id": "body[0]", "anchor_text": "500만원"},
            "status": "identified", "primary_component_id": "package-1",
        }],
    })
    try:
        validate_support_cap_completeness_v02(cap_bare_numeral, cap_pack)
    except ValueError as error:
        assert "최대 500만원" in str(error)
    else:
        raise AssertionError("a support_scale anchor missing the cap marker must still be rejected")

    # A duration cap (e.g. 최대 6개월) is support_period, not support_scale;
    # the completeness guard must not demand a support_scale fact for it.
    duration_pack = CandidatePack.model_validate({
        "pack_id": "cap-duration-pack", "notice_id": "PBLN-cap-duration", "question": "cap", "blocks": [
            {"block_id": "body[0]", "text": "최대 6개월간 지원", "relation": "candidate"},
        ],
    })
    duration_extraction = SourceSelectionExtractionV02.model_validate({
        "notice_id": "PBLN-cap-duration", "candidate_pack_id": "cap-duration-pack",
        "component_decision": {"mode": "none", "no_component_reason": "no_component"},
        "support_components": [],
        "facts": [{
            "fact_id": "fact-period", "field_name": "support_period",
            "value_anchor": {"source_block_id": "body[0]", "anchor_text": "최대 6개월간"},
            "status": "identified",
        }],
    })
    validate_support_cap_completeness_v02(duration_extraction, duration_pack)  # must not raise

    # A headcount cap (e.g. 최대 2명) is a personnel/eligibility limit, not a
    # support-scale amount gate; it must never demand a support_scale fact,
    # even though the block is already selected as component evidence.
    headcount_pack = CandidatePack.model_validate({
        "pack_id": "cap-headcount-pack", "notice_id": "PBLN-cap-headcount", "question": "cap", "blocks": [
            {"block_id": "body[0]", "text": "기업당 1명, 상시근로자 5인 이상 최대 2명 지원", "relation": "candidate"},
        ],
    })
    headcount_extraction = SourceSelectionExtractionV02.model_validate({
        "notice_id": "PBLN-cap-headcount", "candidate_pack_id": "cap-headcount-pack",
        "component_decision": {"mode": "packages"},
        "support_components": [{
            "support_component_id": "package-1", "component_kind": "support_package",
            "source_block_ids": ["body[0]"], "table_block_ids": [],
        }],
        "facts": [],
    })
    validate_support_cap_completeness_v02(headcount_extraction, headcount_pack)  # must not raise: not a scale cap

    # Explicit employment-condition variants (e.g. 신규 채용 vs 기존 재직자) that
    # already modify their own disjoint amount/rate facts must also be related
    # to a package support fact (e.g. support_period) that no condition claims
    # but that shares their recipient -- it is common to every variant.
    def variant_fact(fact_id, field_name, recipient_fact_ids=(), modifies_fact_ids=()):
        return {
            "fact_id": fact_id, "field_name": field_name,
            "value_anchor": {"source_block_id": "body[0]", "anchor_text": fact_id},
            "status": "identified", "primary_component_id": "package-1",
            "recipient_fact_ids": list(recipient_fact_ids),
            "modifies_fact_ids": list(modifies_fact_ids),
        }

    variant_extraction = SourceSelectionExtractionV02.model_validate({
        "notice_id": "PBLN-variant", "candidate_pack_id": "variant-pack",
        "component_decision": {"mode": "packages"},
        "support_components": [{
            "support_component_id": "package-1", "component_kind": "support_package",
            "source_block_ids": ["body[0]"], "table_block_ids": [],
        }],
        "facts": [
            variant_fact("fact-beneficiary", "beneficiary"),
            variant_fact("fact-new-amount", "support_scale", recipient_fact_ids=["fact-beneficiary"]),
            variant_fact("fact-old-amount", "support_scale", recipient_fact_ids=["fact-beneficiary"]),
            variant_fact("fact-period", "support_period", recipient_fact_ids=["fact-beneficiary"]),
            variant_fact("fact-cond-new", "eligibility_conditions", modifies_fact_ids=["fact-new-amount"]),
            variant_fact("fact-cond-old", "eligibility_conditions", modifies_fact_ids=["fact-old-amount"]),
        ],
    })
    normalized_variants, variant_changes = normalize_explicit_condition_variant_relations_v02(variant_extraction)
    normalized_by_id = {fact.fact_id: fact for fact in normalized_variants.facts}
    assert set(normalized_by_id["fact-cond-new"].modifies_fact_ids) == {"fact-new-amount", "fact-period"}
    assert set(normalized_by_id["fact-cond-old"].modifies_fact_ids) == {"fact-old-amount", "fact-period"}
    assert len(variant_changes) == 2

    # An unclaimed support_scale sibling (e.g. a package-wide cap) must NOT be
    # inherited by every condition merely because it shares a recipient: the
    # conditions already show support_scale is variant-specific in this
    # package (each claims its own amount), so an unclaimed same-field fact
    # more likely reflects a missed variant-specific link than a fact common
    # to every variant.  A shared support_period stays eligible regardless.
    variant_with_unclaimed_scale = SourceSelectionExtractionV02.model_validate({
        "notice_id": "PBLN-variant-samefield", "candidate_pack_id": "variant-pack",
        "component_decision": {"mode": "packages"},
        "support_components": [{
            "support_component_id": "package-1", "component_kind": "support_package",
            "source_block_ids": ["body[0]"], "table_block_ids": [],
        }],
        "facts": [
            variant_fact("fact-beneficiary", "beneficiary"),
            variant_fact("fact-new-amount", "support_scale", recipient_fact_ids=["fact-beneficiary"]),
            variant_fact("fact-old-amount", "support_scale", recipient_fact_ids=["fact-beneficiary"]),
            variant_fact("fact-period", "support_period", recipient_fact_ids=["fact-beneficiary"]),
            variant_fact("fact-limit", "support_scale", recipient_fact_ids=["fact-beneficiary"]),
            variant_fact("fact-cond-new", "eligibility_conditions", modifies_fact_ids=["fact-new-amount"]),
            variant_fact("fact-cond-old", "eligibility_conditions", modifies_fact_ids=["fact-old-amount"]),
        ],
    })
    normalized_samefield, _ = normalize_explicit_condition_variant_relations_v02(variant_with_unclaimed_scale)
    samefield_by_id = {fact.fact_id: fact for fact in normalized_samefield.facts}
    assert set(samefield_by_id["fact-cond-new"].modifies_fact_ids) == {"fact-new-amount", "fact-period"}
    assert set(samefield_by_id["fact-cond-old"].modifies_fact_ids) == {"fact-old-amount", "fact-period"}
    assert "fact-limit" not in samefield_by_id["fact-cond-new"].modifies_fact_ids
    assert "fact-limit" not in samefield_by_id["fact-cond-old"].modifies_fact_ids

    # A single condition is not a "variant": nothing to relate it against, so
    # the extraction must come back unchanged.
    single_condition = SourceSelectionExtractionV02.model_validate({
        "notice_id": "PBLN-variant-single", "candidate_pack_id": "variant-pack",
        "component_decision": {"mode": "packages"},
        "support_components": [{
            "support_component_id": "package-1", "component_kind": "support_package",
            "source_block_ids": ["body[0]"], "table_block_ids": [],
        }],
        "facts": [
            variant_fact("fact-beneficiary", "beneficiary"),
            variant_fact("fact-amount", "support_scale", recipient_fact_ids=["fact-beneficiary"]),
            variant_fact("fact-period", "support_period", recipient_fact_ids=["fact-beneficiary"]),
            variant_fact("fact-cond", "eligibility_conditions", modifies_fact_ids=["fact-amount"]),
        ],
    })
    _, single_condition_changes = normalize_explicit_condition_variant_relations_v02(single_condition)
    assert single_condition_changes == []

    # Overlapping modifies is ambiguous (not an explicit partition into
    # variants); leave the selection untouched rather than guessing.
    overlapping = SourceSelectionExtractionV02.model_validate({
        "notice_id": "PBLN-variant-overlap", "candidate_pack_id": "variant-pack",
        "component_decision": {"mode": "packages"},
        "support_components": [{
            "support_component_id": "package-1", "component_kind": "support_package",
            "source_block_ids": ["body[0]"], "table_block_ids": [],
        }],
        "facts": [
            variant_fact("fact-beneficiary", "beneficiary"),
            variant_fact("fact-amount", "support_scale", recipient_fact_ids=["fact-beneficiary"]),
            variant_fact("fact-period", "support_period", recipient_fact_ids=["fact-beneficiary"]),
            variant_fact("fact-cond-a", "eligibility_conditions", modifies_fact_ids=["fact-amount"]),
            variant_fact("fact-cond-b", "eligibility_conditions", modifies_fact_ids=["fact-amount"]),
        ],
    })
    _, overlapping_changes = normalize_explicit_condition_variant_relations_v02(overlapping)
    assert overlapping_changes == []

    # A support_scale anchor containing an actual duration expression (e.g.
    # 6개월) must be rejected.  The prior duration_pattern used a
    # double-escaped regex (\\d, \\s*) that could never match a real digit
    # or whitespace, so this guard was effectively dead; it must now fire.
    duration_quality_extraction = SourceSelectionExtractionV02.model_validate({
        "notice_id": "PBLN-duration-quality", "candidate_pack_id": "duration-quality-pack",
        "component_decision": {"mode": "none", "no_component_reason": "no_component"},
        "support_components": [],
        "facts": [{
            "fact_id": "fact-scale-duration", "field_name": "support_scale",
            "value_anchor": {"source_block_id": "body[0]", "anchor_text": "6개월"},
            "status": "identified",
        }],
    })
    try:
        validate_selection_quality_v02(duration_quality_extraction)
    except ValueError as error:
        assert "duration" in str(error)
    else:
        raise AssertionError("a support_scale anchor containing a duration must be rejected")

    # Recipient counts are valid support scales, but an activity frequency is
    # not.  The guard must also catch a complete phrase, not only bare ``2회``.
    activity_frequency_quality_extraction = SourceSelectionExtractionV02.model_validate({
        "notice_id": "PBLN-activity-frequency-quality", "candidate_pack_id": "activity-frequency-quality-pack",
        "component_decision": {"mode": "none", "no_component_reason": "no_component"},
        "support_components": [],
        "facts": [{
            "fact_id": "fact-scale-activity-frequency", "field_name": "support_scale",
            "value_anchor": {"source_block_id": "body[0]", "anchor_text": "직무교육 2회"},
            "status": "identified",
        }],
    })
    try:
        validate_selection_quality_v02(activity_frequency_quality_extraction)
    except ValueError as error:
        assert "activity/session count" in str(error)
    else:
        raise AssertionError("an activity frequency must not be emitted as support_scale")

    # A table often abbreviates the same activity frequency as ``각 1회``.
    # It must follow the same field boundary as a full phrase such as
    # ``직무교육 2회``.
    abbreviated_activity_frequency = SourceSelectionExtractionV02.model_validate({
        "notice_id": "PBLN-abbreviated-activity-frequency", "candidate_pack_id": "activity-frequency-pack",
        "component_decision": {"mode": "none", "no_component_reason": "no_component"},
        "support_components": [],
        "facts": [{
            "fact_id": "fact-scale-abbreviated-activity-frequency", "field_name": "support_scale",
            "value_anchor": {"source_block_id": "body[0]", "anchor_text": "각 1회"},
            "status": "identified",
        }],
    })
    try:
        validate_selection_quality_v02(abbreviated_activity_frequency)
    except ValueError as error:
        assert "activity/session count" in str(error)
    else:
        raise AssertionError("an abbreviated activity frequency must not be emitted as support_scale")

    # A beneficiary can distinguish the financial payee from the policy's
    # direct beneficiary.  This normalized role must not leak onto another
    # FactField.
    financial_recipient = SourceSelectionExtractionV02.model_validate({
        "notice_id": "PBLN-beneficiary-role", "candidate_pack_id": "beneficiary-role-pack",
        "component_decision": {"mode": "none", "no_component_reason": "no_component"},
        "support_components": [],
        "facts": [{
            "fact_id": "fact-financial-recipient", "field_name": "beneficiary",
            "value_anchor": {"source_block_id": "body[0]", "anchor_text": "사업주"},
            "subject_role": "financial_recipient", "status": "identified",
        }],
    })
    assert financial_recipient.facts[0].subject_role == "financial_recipient"
    try:
        SourceSelectionExtractionV02.model_validate({
            "notice_id": "PBLN-beneficiary-role", "candidate_pack_id": "beneficiary-role-pack",
            "component_decision": {"mode": "none", "no_component_reason": "no_component"},
            "support_components": [],
            "facts": [{
                "fact_id": "invalid-role", "field_name": "support_target",
                "value_anchor": {"source_block_id": "body[0]", "anchor_text": "청년"},
                "subject_role": "financial_recipient", "status": "identified",
            }],
        })
    except ValidationError as error:
        assert "subject_role" in str(error)
    else:
        raise AssertionError("subject_role must be rejected outside beneficiary facts")

    # A stochastic one-call repair attempt can fix the flagged problem while
    # silently dropping an unrelated, already-valid fact from the prior
    # attempt.  preserve_prior_server_validated_facts_v02 is the server-side
    # safety net: it must never fabricate a fact, and it must not resurrect a
    # deliberately-superseded or now-dangling one.
    merge_pack = CandidatePack.model_validate({
        "pack_id": "merge-pack", "notice_id": "PBLN-merge", "question": "merge", "blocks": [
            {"block_id": "body[0]", "text": "사업주에게 월 160만원 지원, 최대 6개월간 지원", "relation": "candidate"},
        ],
    })
    merge_component = {
        "support_component_id": "package-1", "component_kind": "support_package",
        "source_block_ids": ["body[0]"], "table_block_ids": [],
    }

    def merge_fact(fact_id, field_name, anchor_text, recipient_fact_ids=()):
        return {
            "fact_id": fact_id, "field_name": field_name,
            "value_anchor": {"source_block_id": "body[0]", "anchor_text": anchor_text},
            "status": "identified", "primary_component_id": "package-1",
            "recipient_fact_ids": list(recipient_fact_ids),
        }

    # (1) A prior attempt's support_period fact ("최대 6개월간") that this
    # repair attempt dropped must be restored, and a relation pointing at a
    # fact `current` already holds verbatim (the beneficiary, re-selected
    # under a new id) must be rewritten to that current id, not dropped.
    preservation_previous = SourceSelectionExtractionV02.model_validate({
        "notice_id": "PBLN-merge", "candidate_pack_id": "merge-pack",
        "component_decision": {"mode": "packages"}, "support_components": [merge_component],
        "facts": [
            merge_fact("prev-beneficiary", "beneficiary", "사업주"),
            merge_fact("prev-amount", "support_scale", "월 160만원", recipient_fact_ids=["prev-beneficiary"]),
            merge_fact("prev-period", "support_period", "최대 6개월간", recipient_fact_ids=["prev-beneficiary"]),
        ],
    })
    preservation_current = SourceSelectionExtractionV02.model_validate({
        "notice_id": "PBLN-merge", "candidate_pack_id": "merge-pack",
        "component_decision": {"mode": "packages"}, "support_components": [merge_component],
        "facts": [
            merge_fact("cur-beneficiary", "beneficiary", "사업주"),
            merge_fact("cur-amount", "support_scale", "월 160만원", recipient_fact_ids=["cur-beneficiary"]),
        ],
    })
    preserved_merge, preserved_changes = preserve_prior_server_validated_facts_v02(
        preservation_previous, preservation_current, merge_pack
    )
    preserved_by_field = {fact.field_name.value: fact for fact in preserved_merge.facts}
    assert len(preserved_merge.facts) == 3
    assert preserved_by_field["support_period"].value_anchor.anchor_text == "최대 6개월간"
    assert preserved_by_field["support_period"].recipient_fact_ids == ["cur-beneficiary"]
    assert [change["field_name"] for change in preserved_changes] == ["support_period"]
    assert preserved_changes[0]["original_fact_id"] == "prev-period"

    # (2) A repair attempt that re-selected the *same slot* (field, scope,
    # block) with a different exact span deliberately superseded the old
    # one; the old span must not be merged back in beside or instead of it.
    conflict_previous = SourceSelectionExtractionV02.model_validate({
        "notice_id": "PBLN-merge", "candidate_pack_id": "merge-pack",
        "component_decision": {"mode": "packages"}, "support_components": [merge_component],
        "facts": [merge_fact("prev-period", "support_period", "최대 6개월간")],
    })
    conflict_current = SourceSelectionExtractionV02.model_validate({
        "notice_id": "PBLN-merge", "candidate_pack_id": "merge-pack",
        "component_decision": {"mode": "packages"}, "support_components": [merge_component],
        "facts": [merge_fact("cur-period", "support_period", "6개월간")],
    })
    conflict_merged, conflict_changes = preserve_prior_server_validated_facts_v02(
        conflict_previous, conflict_current, merge_pack
    )
    assert [fact.fact_id for fact in conflict_merged.facts] == ["cur-period"]
    assert conflict_changes == []

    # (3) A preserved fact's relation to a previous fact that was *not*
    # preserved (here: superseded by a conflicting re-selection) must be
    # dropped, not left dangling or misattributed to an unrelated current fact.
    dangling_pack = CandidatePack.model_validate({
        "pack_id": "dangling-pack", "notice_id": "PBLN-dangling", "question": "merge", "blocks": [
            {"block_id": "body[0]", "text": "신청기업 사업주에게 월 160만원 지원", "relation": "candidate"},
        ],
    })
    dangling_previous = SourceSelectionExtractionV02.model_validate({
        "notice_id": "PBLN-dangling", "candidate_pack_id": "dangling-pack",
        "component_decision": {"mode": "packages"}, "support_components": [merge_component],
        "facts": [
            merge_fact("prev-beneficiary", "beneficiary", "사업주"),
            merge_fact("prev-amount", "support_scale", "월 160만원", recipient_fact_ids=["prev-beneficiary"]),
        ],
    })
    dangling_current = SourceSelectionExtractionV02.model_validate({
        "notice_id": "PBLN-dangling", "candidate_pack_id": "dangling-pack",
        "component_decision": {"mode": "packages"}, "support_components": [merge_component],
        "facts": [merge_fact("cur-beneficiary", "beneficiary", "신청기업")],
    })
    dangling_merged, dangling_changes = preserve_prior_server_validated_facts_v02(
        dangling_previous, dangling_current, dangling_pack
    )
    restored_amount = next(fact for fact in dangling_merged.facts if fact.field_name.value == "support_scale")
    assert restored_amount.recipient_fact_ids == []
    assert len(dangling_changes) == 1

    # (4) On a first attempt there is no prior server-validated selection at
    # all; the merge must be a strict no-op.
    first_attempt_merged, first_attempt_changes = preserve_prior_server_validated_facts_v02(
        None, preservation_current, merge_pack
    )
    assert first_attempt_merged is preservation_current
    assert first_attempt_changes == []

    # (5) Superseding (duplicate prevention) is keyed on the exact
    # (source_block_id, anchor_text) span alone: if `current` reclassified
    # and re-scoped that very span under a different field/component, the
    # old fact must not be duplicated.  Relation *aliasing* is narrower: a
    # modifies_fact_ids reference to that reclassified fact must be dropped,
    # not cross-field-aliased -- a modifies target must still be shaped like
    # what the condition originally modified.
    reclass_pack = CandidatePack.model_validate({
        "pack_id": "reclass-pack", "notice_id": "PBLN-reclass", "question": "merge", "blocks": [
            {"block_id": "body[0]", "text": "사업주에게 최대 500만원 지원, 안전관리자 신규 채용 조건", "relation": "candidate"},
        ],
    })
    reclass_previous = SourceSelectionExtractionV02.model_validate({
        "notice_id": "PBLN-reclass", "candidate_pack_id": "reclass-pack",
        "component_decision": {"mode": "packages"}, "support_components": [merge_component],
        "facts": [
            merge_fact("prev-cap", "support_scale", "최대 500만원"),
            {
                "fact_id": "prev-cond", "field_name": "eligibility_conditions",
                "value_anchor": {"source_block_id": "body[0]", "anchor_text": "안전관리자 신규 채용 조건"},
                "status": "identified", "primary_component_id": "package-1",
                "modifies_fact_ids": ["prev-cap"],
            },
        ],
    })
    reclass_current = SourceSelectionExtractionV02.model_validate({
        "notice_id": "PBLN-reclass", "candidate_pack_id": "reclass-pack",
        "component_decision": {"mode": "packages"}, "support_components": [merge_component],
        "facts": [{
            # Same exact span as prev-cap, but reclassified to a different
            # field_name and re-scoped to notice level (no component).
            "fact_id": "cur-cap", "field_name": "support_content",
            "value_anchor": {"source_block_id": "body[0]", "anchor_text": "최대 500만원"},
            "status": "identified", "primary_component_id": None,
        }],
    })
    reclass_merged, reclass_changes = preserve_prior_server_validated_facts_v02(
        reclass_previous, reclass_current, reclass_pack
    )
    assert len(reclass_merged.facts) == 2  # cur-cap plus the restored condition; prev-cap not duplicated
    assert sum(1 for fact in reclass_merged.facts if fact.value_anchor.anchor_text == "최대 500만원") == 1
    restored_condition = next(fact for fact in reclass_merged.facts if fact.field_name.value == "eligibility_conditions")
    assert restored_condition.modifies_fact_ids == []  # cross-field alias (support_scale -> support_content) dropped
    assert [change["original_fact_id"] for change in reclass_changes] == ["prev-cond"]

    # (5b) The same cross-field-drop rule applies to recipient_fact_ids.
    cross_field_recipient_previous = SourceSelectionExtractionV02.model_validate({
        "notice_id": "PBLN-reclass", "candidate_pack_id": "reclass-pack",
        "component_decision": {"mode": "packages"}, "support_components": [merge_component],
        "facts": [
            merge_fact("prev-cap", "support_scale", "최대 500만원"),
            merge_fact("prev-linked", "support_period", "안전관리자 신규 채용 조건", recipient_fact_ids=["prev-cap"]),
        ],
    })
    cross_field_recipient_merged, _ = preserve_prior_server_validated_facts_v02(
        cross_field_recipient_previous, reclass_current, reclass_pack
    )
    restored_linked = next(fact for fact in cross_field_recipient_merged.facts if fact.fact_id != "cur-cap")
    assert restored_linked.recipient_fact_ids == []  # prev-cap (support_scale) -> cur-cap (support_content): dropped

    # (5c) A same-field alias, by contrast, must remain and be remapped: the
    # beneficiary preserved by test (1) is exactly this case (both
    # `prev-beneficiary` and `cur-beneficiary` are field_name=beneficiary),
    # already asserted there.  Confirm it explicitly here too.
    assert preserved_by_field["support_period"].recipient_fact_ids == ["cur-beneficiary"]

    # (5d) Every preserved-fact audit record must carry enough provenance to
    # explain the restoration without re-deriving it: source_block_id and
    # anchor_text alongside the renamed reason.
    for change in preserved_changes:
        assert change["reason"] == "revalidated_from_prior_attempt"
        assert change["source_block_id"] == "body[0]"
        assert isinstance(change["anchor_text"], str) and change["anchor_text"]

    # (5e) Component identity excludes component_kind: the same exact source
    # evidence reclassified to a different kind under a new id must still be
    # recognized and remapped, not treated as an unresolvable component.
    kind_repair_previous = SourceSelectionExtractionV02.model_validate({
        "notice_id": "PBLN-reclass", "candidate_pack_id": "reclass-pack",
        "component_decision": {"mode": "participation_types"},
        "support_components": [{
            "support_component_id": "old-id", "component_kind": "participation_type",
            "source_block_ids": ["body[0]"], "table_block_ids": [],
        }],
        "facts": [{
            "fact_id": "prev-linked-kind", "field_name": "support_period",
            "value_anchor": {"source_block_id": "body[0]", "anchor_text": "안전관리자 신규 채용 조건"},
            "status": "identified", "primary_component_id": "old-id",
        }],
    })
    kind_repair_current = SourceSelectionExtractionV02.model_validate({
        "notice_id": "PBLN-reclass", "candidate_pack_id": "reclass-pack",
        "component_decision": {"mode": "packages"},
        "support_components": [{
            # Same exact evidence as `old-id`, reclassified to a different
            # component_kind under a new model-authored id -- must still match.
            "support_component_id": "new-id", "component_kind": "support_package",
            "source_block_ids": ["body[0]"], "table_block_ids": [],
        }],
        "facts": [{
            "fact_id": "cur-other", "field_name": "beneficiary",
            "value_anchor": {"source_block_id": "body[0]", "anchor_text": "최대 500만원"},
            "status": "identified", "primary_component_id": "new-id",
        }],
    })
    kind_repair_merged, kind_repair_changes = preserve_prior_server_validated_facts_v02(
        kind_repair_previous, kind_repair_current, reclass_pack
    )
    restored_kind = next(fact for fact in kind_repair_merged.facts if fact.fact_id != "cur-other")
    assert restored_kind.primary_component_id == "new-id"
    assert len(kind_repair_changes) == 1

    # (6) Preservation must never turn an otherwise-valid attempt into a hard
    # failure: if the merged selection fails `finalize`, and merging actually
    # changed something, fall back deterministically to `finalize(current)`
    # and persist an audit record.  If nothing was preserved, or the
    # unmerged attempt also fails, the original failure must still surface.
    fallback_previous = SourceSelectionExtractionV02.model_validate({
        "notice_id": "PBLN-merge", "candidate_pack_id": "merge-pack",
        "component_decision": {"mode": "packages"}, "support_components": [merge_component],
        "facts": [merge_fact("prev-extra", "support_period", "최대 6개월간")],
    })
    fallback_current = SourceSelectionExtractionV02.model_validate({
        "notice_id": "PBLN-merge", "candidate_pack_id": "merge-pack",
        "component_decision": {"mode": "packages"}, "support_components": [merge_component],
        "facts": [merge_fact("cur-amount", "support_scale", "월 160만원")],
    })
    fallback_merged, fallback_preserved = preserve_prior_server_validated_facts_v02(
        fallback_previous, fallback_current, merge_pack
    )
    assert len(fallback_merged.facts) == 2  # the merge itself succeeded structurally

    def _finalize_rejects_multi_fact(candidate):
        if len(candidate.facts) > 1:
            raise ValueError("simulated: merged selection exceeds one fact")
        return list(candidate.facts)

    fallback_result, fallback_used_changes, fallback_record = apply_finalize_with_fallback_v02(
        fallback_merged, fallback_current, fallback_preserved, _finalize_rejects_multi_fact
    )
    assert fallback_result == list(fallback_current.facts)
    assert fallback_used_changes == []
    assert fallback_record is not None
    assert fallback_record["reason"] == "preservation_caused_validation_failure"
    assert fallback_record["dropped_restored_fact_ids"] == [fallback_preserved[0]["restored_fact_id"]]

    def _finalize_always_fails(candidate):
        raise ValueError("always fails regardless of merge")

    try:
        apply_finalize_with_fallback_v02(
            fallback_merged, fallback_current, fallback_preserved, _finalize_always_fails
        )
    except ValueError as error:
        assert "always fails" in str(error)
    else:
        raise AssertionError("when the unmerged attempt also fails, the failure must propagate")

    # (7) A component's identity across attempts is its evidence fingerprint
    # (kind, name/source evidence, source/table block ids), not its
    # model-authored id: reusing an id for genuinely different evidence must
    # not be treated as the same component, and renaming the same evidence
    # to a new id must still be recognized and remapped.
    reuse_pack = CandidatePack.model_validate({
        "pack_id": "reuse-pack", "notice_id": "PBLN-reuse", "question": "merge", "blocks": [
            {"block_id": "body[0]", "text": "패키지A 지원 내용, 최대 300만원", "relation": "candidate"},
            {"block_id": "body[1]", "text": "패키지B 지원 내용, 최대 700만원", "relation": "candidate"},
        ],
    })
    reuse_previous = SourceSelectionExtractionV02.model_validate({
        "notice_id": "PBLN-reuse", "candidate_pack_id": "reuse-pack",
        "component_decision": {"mode": "packages"},
        "support_components": [{
            "support_component_id": "component_1", "component_kind": "support_package",
            "source_block_ids": ["body[0]"], "table_block_ids": [],
        }],
        "facts": [{
            "fact_id": "prev-fact", "field_name": "support_scale",
            "value_anchor": {"source_block_id": "body[0]", "anchor_text": "최대 300만원"},
            "status": "identified", "primary_component_id": "component_1",
        }],
    })
    reuse_current = SourceSelectionExtractionV02.model_validate({
        "notice_id": "PBLN-reuse", "candidate_pack_id": "reuse-pack",
        "component_decision": {"mode": "packages"},
        "support_components": [{
            # Same id as previous, but genuinely different evidence (a
            # different block) -- must not be treated as the same component.
            "support_component_id": "component_1", "component_kind": "support_package",
            "source_block_ids": ["body[1]"], "table_block_ids": [],
        }],
        "facts": [{
            "fact_id": "cur-fact", "field_name": "support_scale",
            "value_anchor": {"source_block_id": "body[1]", "anchor_text": "최대 700만원"},
            "status": "identified", "primary_component_id": "component_1",
        }],
    })
    reuse_merged, reuse_changes = preserve_prior_server_validated_facts_v02(reuse_previous, reuse_current, reuse_pack)
    assert [fact.fact_id for fact in reuse_merged.facts] == ["cur-fact"]
    assert reuse_changes == []

    rename_previous = SourceSelectionExtractionV02.model_validate({
        "notice_id": "PBLN-reuse", "candidate_pack_id": "reuse-pack",
        "component_decision": {"mode": "packages"},
        "support_components": [{
            "support_component_id": "old-id", "component_kind": "support_package",
            "source_block_ids": ["body[0]"], "table_block_ids": [],
        }],
        "facts": [{
            "fact_id": "prev-fact", "field_name": "support_scale",
            "value_anchor": {"source_block_id": "body[0]", "anchor_text": "최대 300만원"},
            "status": "identified", "primary_component_id": "old-id",
        }],
    })
    rename_current = SourceSelectionExtractionV02.model_validate({
        "notice_id": "PBLN-reuse", "candidate_pack_id": "reuse-pack",
        "component_decision": {"mode": "packages"},
        "support_components": [{
            # Same kind/evidence as `old-id` under a new, model-authored id.
            "support_component_id": "new-id", "component_kind": "support_package",
            "source_block_ids": ["body[0]"], "table_block_ids": [],
        }],
        "facts": [{
            "fact_id": "cur-other", "field_name": "beneficiary",
            "value_anchor": {"source_block_id": "body[1]", "anchor_text": "패키지B"},
            "status": "identified", "primary_component_id": "new-id",
        }],
    })
    rename_merged, rename_changes = preserve_prior_server_validated_facts_v02(
        rename_previous, rename_current, reuse_pack
    )
    restored_by_rename = next(fact for fact in rename_merged.facts if fact.fact_id != "cur-other")
    assert restored_by_rename.primary_component_id == "new-id"
    assert len(rename_changes) == 1

    # (8) A previous fact whose anchor no longer resolves to exactly one
    # occurrence in the current pack -- missing, or now ambiguous -- must be
    # dropped, never preserved on a best-effort or approximate span.
    unresolved_pack = CandidatePack.model_validate({
        "pack_id": "unresolved-pack", "notice_id": "PBLN-unresolved", "question": "merge", "blocks": [
            {"block_id": "body[0]", "text": "사업주에게 300만원 지원", "relation": "candidate"},
            {"block_id": "body[1]", "text": "300만원과 또 다른 300만원을 지원", "relation": "candidate"},
        ],
    })
    unresolved_previous = SourceSelectionExtractionV02.model_validate({
        "notice_id": "PBLN-unresolved", "candidate_pack_id": "unresolved-pack",
        "component_decision": {"mode": "none", "no_component_reason": "no_component"},
        "support_components": [],
        "facts": [
            {
                # "최대 300만원" is not a substring of body[0]'s current text at all.
                "fact_id": "prev-missing", "field_name": "support_scale",
                "value_anchor": {"source_block_id": "body[0]", "anchor_text": "최대 300만원"},
                "status": "identified",
            },
            {
                # "300만원" now occurs twice in body[1] -- no longer unique.
                "fact_id": "prev-ambiguous", "field_name": "support_scale",
                "value_anchor": {"source_block_id": "body[1]", "anchor_text": "300만원"},
                "status": "identified",
            },
        ],
    })
    unresolved_current = SourceSelectionExtractionV02.model_validate({
        "notice_id": "PBLN-unresolved", "candidate_pack_id": "unresolved-pack",
        "component_decision": {"mode": "none", "no_component_reason": "no_component"},
        "support_components": [], "facts": [],
    })
    unresolved_merged, unresolved_changes = preserve_prior_server_validated_facts_v02(
        unresolved_previous, unresolved_current, unresolved_pack
    )
    assert unresolved_merged.facts == []
    assert unresolved_changes == []

    # (9) The claimed-field check in normalize_explicit_condition_variant_relations_v02
    # now applies uniformly: an unclaimed support_period sibling must NOT be
    # attached when support_period is itself already a claimed field type.
    period_claimed_extraction = SourceSelectionExtractionV02.model_validate({
        "notice_id": "PBLN-variant-period-claimed", "candidate_pack_id": "variant-pack",
        "component_decision": {"mode": "packages"},
        "support_components": [{
            "support_component_id": "package-1", "component_kind": "support_package",
            "source_block_ids": ["body[0]"], "table_block_ids": [],
        }],
        "facts": [
            variant_fact("fact-beneficiary", "beneficiary"),
            variant_fact("fact-new-amount", "support_scale", recipient_fact_ids=["fact-beneficiary"]),
            variant_fact("fact-old-amount", "support_scale", recipient_fact_ids=["fact-beneficiary"]),
            variant_fact("fact-new-period", "support_period", recipient_fact_ids=["fact-beneficiary"]),
            variant_fact("fact-extra-period", "support_period", recipient_fact_ids=["fact-beneficiary"]),
            variant_fact(
                "fact-cond-new", "eligibility_conditions",
                modifies_fact_ids=["fact-new-amount", "fact-new-period"],
            ),
            variant_fact("fact-cond-old", "eligibility_conditions", modifies_fact_ids=["fact-old-amount"]),
        ],
    })
    normalized_period_claimed, _ = normalize_explicit_condition_variant_relations_v02(period_claimed_extraction)
    period_claimed_by_id = {fact.fact_id: fact for fact in normalized_period_claimed.facts}
    assert "fact-extra-period" not in period_claimed_by_id["fact-cond-new"].modifies_fact_ids
    assert "fact-extra-period" not in period_claimed_by_id["fact-cond-old"].modifies_fact_ids

    # --- Empty repair response policy -------------------------------------
    # A first attempt returning zero facts is not this failure: it is the
    # existing, ordinary validation failure the one-retry loop already
    # handles (classify_empty_repair_response_v02 must be a no-op then).
    assert classify_empty_repair_response_v02(
        is_repair_attempt=False, facts=[], prior_candidate=None, prior_validation_error=None,
    ) is None

    # A repair attempt that DID return facts is not this failure either,
    # regardless of what those facts turn out to validate as.
    assert classify_empty_repair_response_v02(
        is_repair_attempt=True, facts=[object()], prior_candidate=None, prior_validation_error="prior error",
    ) is None

    # A repair attempt (one sent with previous_selection/server_validation_errors)
    # that came back with zero facts must fail immediately and deterministically,
    # carrying an audit-safe summary of the prior candidate -- never claimed to
    # have been server-validated -- and the error that prompted the repair.
    empty_repair_prior_candidate = SourceSelectionExtractionV02.model_validate({
        "notice_id": "PBLN-empty-repair", "candidate_pack_id": "empty-repair-pack",
        "component_decision": {"mode": "none", "no_component_reason": "no_component"},
        "support_components": [],
        "facts": [
            {
                "fact_id": "prior-1", "field_name": "support_scale",
                "value_anchor": {"source_block_id": "body[0]", "anchor_text": "월 160만원"},
                "status": "identified",
            },
            {
                "fact_id": "prior-2", "field_name": "support_period",
                "value_anchor": {"source_block_id": "body[0]", "anchor_text": "6개월간"},
                "status": "identified",
            },
        ],
    })
    empty_repair_error = classify_empty_repair_response_v02(
        is_repair_attempt=True,
        facts=[],
        prior_candidate=empty_repair_prior_candidate,
        prior_validation_error="support_scale anchors must not contain a duration",
    )
    assert isinstance(empty_repair_error, EmptyRepairResponseError)
    assert empty_repair_error.error_classification == "empty_repair_response"
    assert empty_repair_error.prior_validation_error == "support_scale anchors must not contain a duration"
    assert empty_repair_error.prior_candidate_summary == {
        "fact_count": 2,
        "facts": [
            {"field_name": "support_scale", "source_block_id": "body[0]", "anchor_text": "월 160만원"},
            {"field_name": "support_period", "source_block_id": "body[0]", "anchor_text": "6개월간"},
        ],
    }

    # Degenerate case: the first attempt itself returned zero facts (so
    # there was never a carried-forward candidate) and the repair attempt
    # also returned zero facts.  Must still classify and summarize cleanly.
    empty_repair_error_no_prior = classify_empty_repair_response_v02(
        is_repair_attempt=True, facts=[], prior_candidate=None, prior_validation_error="prior error",
    )
    assert isinstance(empty_repair_error_no_prior, EmptyRepairResponseError)
    assert empty_repair_error_no_prior.prior_candidate_summary == {"fact_count": 0, "facts": []}

    # summarize_prior_candidate_v02 alone: audit-safe, no server-validation
    # claim, and a strict empty summary when there is no prior candidate.
    assert summarize_prior_candidate_v02(None) == {"fact_count": 0, "facts": []}
    assert summarize_prior_candidate_v02(empty_repair_prior_candidate)["fact_count"] == 2

    # --- delivery_roles special anchor path (v0.2) --------------------------
    # A valid delivery role must materialize the exact organization name(s)
    # and exact role phrase from their own source blocks, plus the optional
    # canonical_role, never a paraphrase or a guessed offset.
    delivery_valid_pack = CandidatePack.model_validate({
        "pack_id": "delivery-valid-pack", "notice_id": "PBLN-delivery", "question": "delivery", "blocks": [
            {"block_id": "body[0]", "text": "주관기관: 울산광역시 / 운영기관: 한국산업단지공단", "relation": "candidate"},
        ],
    })
    delivery_valid = SourceSelectionExtractionV02.model_validate({
        "notice_id": "PBLN-delivery", "candidate_pack_id": "delivery-valid-pack",
        "component_decision": {"mode": "none", "no_component_reason": "no_component"},
        "support_components": [],
        "facts": [{
            "fact_id": "role-valid", "field_name": "delivery_roles",
            "value_anchor": {"source_block_id": "body[0]", "anchor_text": "주관기관: 울산광역시"},
            "status": "identified",
            "organization_anchors": [{"source_block_id": "body[0]", "anchor_text": "울산광역시"}],
            "role_anchor": {"source_block_id": "body[0]", "anchor_text": "주관기관"},
            "canonical_role": "announcing_agency",
        }],
    })
    delivery_valid_evidence = materialize_evidence(delivery_valid, delivery_valid_pack)
    delivery_row = delivery_valid_evidence[0]
    block_text = delivery_valid_pack.blocks[0].text
    assert delivery_row.organization_names == ["울산광역시"]
    assert delivery_row.role_raw == "주관기관"
    assert delivery_row.canonical_role == DeliveryRoleCanonical.ANNOUNCING_AGENCY
    org_start = block_text.index("울산광역시")
    assert (delivery_row.organization_sources[0].start_char, delivery_row.organization_sources[0].end_char) == (
        org_start, org_start + len("울산광역시")
    )
    role_start = block_text.index("주관기관")
    assert (delivery_row.role_source.start_char, delivery_row.role_source.end_char) == (
        role_start, role_start + len("주관기관")
    )

    def delivery_fact(fact_id, value_text, org_text, role_text, block_id="body[0]"):
        return {
            "fact_id": fact_id, "field_name": "delivery_roles",
            "value_anchor": {"source_block_id": block_id, "anchor_text": value_text},
            "status": "identified",
            "organization_anchors": [{"source_block_id": block_id, "anchor_text": org_text}],
            "role_anchor": {"source_block_id": block_id, "anchor_text": role_text},
        }

    def delivery_extraction(notice_id, pack_id, fact):
        return SourceSelectionExtractionV02.model_validate({
            "notice_id": notice_id, "candidate_pack_id": pack_id,
            "component_decision": {"mode": "none", "no_component_reason": "no_component"},
            "support_components": [], "facts": [fact],
        })

    # Organization anchor missing (0 occurrences) must be rejected, not
    # approximated to the nearest text.
    delivery_org_missing_pack = CandidatePack.model_validate({
        "pack_id": "delivery-org-missing-pack", "notice_id": "PBLN-delivery-org-missing",
        "question": "delivery", "blocks": [
            {"block_id": "body[0]", "text": "주관기관: 울산광역시", "relation": "candidate"},
        ],
    })
    delivery_org_missing = delivery_extraction(
        "PBLN-delivery-org-missing", "delivery-org-missing-pack",
        delivery_fact("role-org-missing", "주관기관: 울산광역시", "존재하지않는기관", "주관기관"),
    )
    try:
        materialize_evidence(delivery_org_missing, delivery_org_missing_pack)
    except ValueError as error:
        assert "organization anchor must occur exactly once" in str(error)
    else:
        raise AssertionError("a missing organization anchor must be rejected")

    # Organization anchor ambiguous (2+ occurrences) must be rejected.
    delivery_org_ambiguous_pack = CandidatePack.model_validate({
        "pack_id": "delivery-org-ambiguous-pack", "notice_id": "PBLN-delivery-org-ambiguous",
        "question": "delivery", "blocks": [
            {"block_id": "body[0]", "text": "울산광역시와 울산광역시 산하기관이 공동 주관", "relation": "candidate"},
        ],
    })
    delivery_org_ambiguous = delivery_extraction(
        "PBLN-delivery-org-ambiguous", "delivery-org-ambiguous-pack",
        delivery_fact(
            "role-org-ambiguous", "울산광역시와 울산광역시 산하기관이 공동 주관", "울산광역시", "주관",
        ),
    )
    try:
        materialize_evidence(delivery_org_ambiguous, delivery_org_ambiguous_pack)
    except ValueError as error:
        assert "organization anchor must occur exactly once" in str(error)
    else:
        raise AssertionError("an ambiguous organization anchor must be rejected")

    # Role anchor missing (0 occurrences) must be rejected.
    delivery_role_missing_pack = CandidatePack.model_validate({
        "pack_id": "delivery-role-missing-pack", "notice_id": "PBLN-delivery-role-missing",
        "question": "delivery", "blocks": [
            {"block_id": "body[0]", "text": "주관기관: 울산광역시", "relation": "candidate"},
        ],
    })
    delivery_role_missing = delivery_extraction(
        "PBLN-delivery-role-missing", "delivery-role-missing-pack",
        delivery_fact("role-role-missing", "주관기관: 울산광역시", "울산광역시", "존재하지않는역할"),
    )
    try:
        materialize_evidence(delivery_role_missing, delivery_role_missing_pack)
    except ValueError as error:
        assert "role anchor must occur exactly once" in str(error)
    else:
        raise AssertionError("a missing role anchor must be rejected")

    # Role anchor ambiguous (2+ occurrences) must be rejected.
    delivery_role_ambiguous_pack = CandidatePack.model_validate({
        "pack_id": "delivery-role-ambiguous-pack", "notice_id": "PBLN-delivery-role-ambiguous",
        "question": "delivery", "blocks": [
            {"block_id": "body[0]", "text": "울산광역시 소관 하에 주관기관과 주관기관이 함께 참여", "relation": "candidate"},
        ],
    })
    delivery_role_ambiguous = delivery_extraction(
        "PBLN-delivery-role-ambiguous", "delivery-role-ambiguous-pack",
        delivery_fact(
            "role-role-ambiguous", "울산광역시 소관 하에 주관기관과 주관기관이 함께 참여", "울산광역시", "주관기관",
        ),
    )
    try:
        materialize_evidence(delivery_role_ambiguous, delivery_role_ambiguous_pack)
    except ValueError as error:
        assert "role anchor must occur exactly once" in str(error)
    else:
        raise AssertionError("an ambiguous role anchor must be rejected")

    # Materialization is all-or-nothing: an ambiguous anchor anywhere in the
    # batch must prevent every row -- including an otherwise-valid, unrelated
    # fact -- from being materialized; no invalid delivery relation, and no
    # partial valid one, is ever returned.
    delivery_mixed_pack = CandidatePack.model_validate({
        "pack_id": "delivery-mixed-pack", "notice_id": "PBLN-delivery-mixed", "question": "delivery", "blocks": [
            {"block_id": "body[0]", "text": "주관기관: 울산광역시", "relation": "candidate"},
            {"block_id": "body[1]", "text": "울산광역시와 울산광역시 산하기관이 공동 주관", "relation": "candidate"},
        ],
    })
    delivery_mixed = SourceSelectionExtractionV02.model_validate({
        "notice_id": "PBLN-delivery-mixed", "candidate_pack_id": "delivery-mixed-pack",
        "component_decision": {"mode": "none", "no_component_reason": "no_component"},
        "support_components": [],
        "facts": [
            delivery_fact("role-ok", "주관기관: 울산광역시", "울산광역시", "주관기관"),
            delivery_fact(
                "role-bad", "울산광역시와 울산광역시 산하기관이 공동 주관", "울산광역시", "주관", block_id="body[1]",
            ),
        ],
    })
    try:
        materialize_evidence(delivery_mixed, delivery_mixed_pack)
    except ValueError as error:
        assert "organization anchor must occur exactly once" in str(error)
    else:
        raise AssertionError("materialize_evidence must reject the whole batch when any delivery anchor is ambiguous")

    # The v0.2 evaluator must match the server-recovered span, rather than a
    # model-generated paraphrase or a block-level approximation.
    with tempfile.TemporaryDirectory() as temp_dir:
        gold_path = Path(temp_dir) / "gold.json"
        gold_path.write_text(json.dumps({
            "gold_schema_version": "0.2-draft",
            "status": "draft_for_human_review",
            "notice_id": "PBLN-evaluator",
            "expected_components": [],
            "expected_facts": [{
                "gold_id": "target-01",
                "field_name": "support_target",
                "scope": "notice",
                "source_anchor": {"source_block_id": "body[0]", "anchor_text": "중소기업"},
            }],
        }))
        # v0.2 requires Common IR lineage on at least one source document;
        # this is the minimal valid lineage shape, not a production sample.
        evaluator_profile = {
            "schema_version": "existing_program_profile/v0.2",
            "notice_id": "bizinfo:PBLN-evaluator",
            "source_profile_id": "hwp:PBLN-evaluator",
            "comparison_profile": {"support_target": [{
                "fact_id": "target-actual", "field_name": "support_target", "scope": "notice",
                "value_raw": "중소기업",
                "value_source": {
                    "source_block_id": "body[0]", "start_char": 6, "end_char": 10,
                    "text_basis": "common_ir_v1_candidate_pack",
                },
            }]},
            "support_components": [],
            "derived_projections": [],
            "source_documents": minimal_common_ir_lineage_source_documents("hwp:PBLN-evaluator"),
        }
        evaluation = evaluate_profile_v02(
            evaluator_profile,
            {"source_block_texts": {"body[0]": "지원대상은 중소기업입니다."}},
            gold_path,
            allow_draft=True,
        )
        assert evaluation["provenance"]["passed"]
        assert evaluation["raw_facts"]["matched"] == 1
        assert evaluation["raw_facts"]["missing"] == []

    raw, source = materialize_value_source("body[42]", "창업지원금 각 300만원 및 멘토링 지원", "각 300만원")
    assert raw == "각 300만원"
    assert source.model_dump() == {
        "source_block_id": "body[42]", "start_char": 6, "end_char": 13,
        "text_basis": "common_ir_v1_candidate_pack",
    }
    SupportScaleMeasure.model_validate({
        "measure_type": "rate", "measure_role": "support_rate", "lower_value": None,
        "upper_value": 8000, "unit": "BPS", "comparator": "lte",
        "source_fact_id": "scale_01", "source_numeric_candidate_id": "body[42]#num[0]",
    })
    malformed_rate = SupportScaleMeasure.model_validate({
        "measure_type": "rate", "measure_role": "support_rate", "lower_value": 8000,
        "upper_value": 8000, "unit": "BPS", "comparator": "lte",
        "source_fact_id": "scale_01", "source_numeric_candidate_id": "body[42]#num[0]",
    })
    try:
        validate_support_scale_measure_shape(malformed_rate)
        raise AssertionError("semantic scale-measure validator must reject lte with lower_value")
    except ValueError as error:
        assert "upper-bound comparator" in str(error)
    v02_profile = {
        "schema_version": "existing_program_profile/v0.2",
        "source_profile_id": "hwp:PBLN-scale",
        "comparison_profile": {"support_scale": [{
            "fact_id": "scale_01", "field_name": "support_scale", "value_raw": raw, "value_source": source.model_dump(),
        }]},
        "support_components": [],
        "derived_projections": [{
            "projection_type": "support_scale_measures", "source_fact_ids": ["scale_01"], "status": "identified",
            "measures": [{"measure_type": "amount", "measure_role": "support_amount", "lower_value": 3000000,
                          "upper_value": 3000000, "unit": "KRW", "comparator": "eq",
                          "source_fact_id": "scale_01", "source_numeric_candidate_id": "body[42]#num[0]"}],
        }],
        "processing_metadata": {
            "derived_projection_producers": {
                "support_scale_measures": {"numeric_candidate_extractor_version": NUMERIC_CANDIDATE_EXTRACTOR_VERSION}
            }
        },
        "source_documents": minimal_common_ir_lineage_source_documents("hwp:PBLN-scale"),
    }
    assert validate_profile_v02(v02_profile, {"body[42]": "창업지원금 각 300만원 및 멘토링 지원"}) == []

    # processing_metadata.derived_projection_producers.support_scale_measures.
    # numeric_candidate_extractor_version must be present, with the exact
    # known value, exactly when a support_scale_measures projection exists --
    # never merely null, and never present without one.
    v02_profile_measures_without_version = deepcopy(v02_profile)
    v02_profile_measures_without_version["processing_metadata"] = {}
    issues_missing_version = validate_profile_v02(
        v02_profile_measures_without_version, {"body[42]": "창업지원금 각 300만원 및 멘토링 지원"}
    )
    assert any("is required and must be" in issue for issue in issues_missing_version)

    v02_profile_measures_with_version = deepcopy(v02_profile_measures_without_version)
    v02_profile_measures_with_version["processing_metadata"] = {
        "derived_projection_producers": {
            "support_scale_measures": {"numeric_candidate_extractor_version": NUMERIC_CANDIDATE_EXTRACTOR_VERSION}
        }
    }
    assert validate_profile_v02(
        v02_profile_measures_with_version, {"body[42]": "창업지원금 각 300만원 및 멘토링 지원"}
    ) == []

    v02_profile_version_without_measures = {
        "schema_version": "existing_program_profile/v0.2",
        "comparison_profile": {}, "support_components": [], "derived_projections": [],
        "processing_metadata": {
            "derived_projection_producers": {
                "support_scale_measures": {"numeric_candidate_extractor_version": NUMERIC_CANDIDATE_EXTRACTOR_VERSION}
            }
        },
    }
    issues_unexpected_version = validate_profile_v02(v02_profile_version_without_measures, {})
    assert any("must only be present" in issue for issue in issues_unexpected_version)

    span_pack = CandidatePack.model_validate({
        "pack_id": "span-pack", "notice_id": "PBLN-span", "question": "span", "blocks": [
            {"block_id": "body[42]", "text": "창업지원금 각 300만원 및 멘토링 지원", "relation": "candidate"}
        ],
    })
    span_selection = SourceSelectionExtractionV02.model_validate({
        "notice_id": "PBLN-span", "candidate_pack_id": "span-pack",
        "component_decision": {"mode": "none", "no_component_reason": "no separate package"},
        "facts": [{"fact_id": "scale", "field_name": "support_scale", "status": "identified",
                   "value_anchor": {"source_block_id": "body[42]", "anchor_text": "각 300만원"}}],
    })
    assert materialize_evidence(span_selection, span_pack)[0].value_source == source

    # CandidatePack Anchor Occurrence Resolver v1, wired through
    # materialize_evidence's optional common_ir_source_sha256 and
    # resolve_ambiguous_value_anchor. Requires a Common IR CandidatePack.
    resolver_pack = CandidatePack.model_validate({
        "pack_id": "resolver-pack", "notice_id": "PBLN-resolver", "question": "resolver",
        "generator": "semantic_structuring.common_ir_v1", "generator_version": "1",
        "common_ir_document_id": "hwpx:PBLN-resolver",
        "blocks": [{
            "block_id": "hwpx:t1#r1c1p0", "text": "지원금 300만원, 추가 지원금 300만원",
            "relation": "candidate", "common_ir_block_id": "hwpx:t1",
        }],
    })
    resolver_block_text = resolver_pack.blocks[0].text
    trusted_sha256 = "c" * 64

    def resolver_fact(fact_id: str, anchor_text: str) -> dict:
        return {
            "fact_id": fact_id, "field_name": "support_content", "status": "identified",
            "value_anchor": {"source_block_id": "hwpx:t1#r1c1p0", "anchor_text": anchor_text},
        }

    def resolver_selection(fact: dict) -> SourceSelectionExtractionV02:
        return SourceSelectionExtractionV02.model_validate({
            "notice_id": "PBLN-resolver", "candidate_pack_id": "resolver-pack",
            "component_decision": {"mode": "none", "no_component_reason": "no separate package"},
            "facts": [fact],
        })

    # Unique anchor: the callback must never run, and no candidate_id is
    # ever generated -- the plain exact-span path is used as before.
    def _never_called(request: AnchorCorrectionRequest) -> str:
        raise AssertionError("correction callback must not run for a unique anchor")

    unique_evidence = materialize_evidence(
        resolver_selection(resolver_fact("unique-anchor", "추가 지원금")),
        resolver_pack,
        common_ir_source_sha256=trusted_sha256,
        resolve_ambiguous_value_anchor=_never_called,
    )[0]
    assert unique_evidence.value_source.start_char == resolver_block_text.index("추가 지원금")
    assert unique_evidence.source_blocks[0]["text"] == "추가 지원금"

    # Ambiguous anchor, second occurrence chosen: the callback receives only
    # candidate_id/anchor_text/context (never offsets), in an order sorted by
    # candidate_id rather than document position (point 4), so the intended
    # candidate_id is learned independently here -- never by list position --
    # and its choice is materialized to that occurrence's own exact span.
    second_start = resolver_block_text.find("지원금", resolver_block_text.find("지원금") + 1)
    independent_resolution = resolve_anchor_occurrences(
        AnchorOccurrenceRequest(
            common_ir_document_id=resolver_pack.common_ir_document_id,
            common_ir_source_sha256=trusted_sha256,
            candidate_pack_id=resolver_pack.pack_id,
            candidate_pack_generator=resolver_pack.generator,
            candidate_pack_generator_version=resolver_pack.generator_version,
            source_block_id="hwpx:t1#r1c1p0",
            anchor_text="지원금",
        ),
        resolver_pack,
        trusted_common_ir_source_sha256=trusted_sha256,
    )
    second_candidate_id = next(
        candidate.candidate_id for candidate in independent_resolution.candidates if candidate.start_char == second_start
    )

    def _choose_second(request: AnchorCorrectionRequest) -> str:
        assert len(request.candidates) == 2
        assert all(candidate.anchor_text == "지원금" for candidate in request.candidates)
        return second_candidate_id

    ambiguous_selection = resolver_selection(resolver_fact("ambiguous-anchor", "지원금"))
    ambiguous_evidence = materialize_evidence(
        ambiguous_selection, resolver_pack,
        common_ir_source_sha256=trusted_sha256, resolve_ambiguous_value_anchor=_choose_second,
    )[0]
    assert ambiguous_evidence.value_source.start_char == second_start
    assert ambiguous_evidence.source_blocks[0]["text"] == "지원금"

    # Without both the SHA-256 and the callback, existing behavior is
    # preserved: a repeated anchor still fails closed with a plain ValueError.
    try:
        materialize_evidence(ambiguous_selection, resolver_pack)
    except ValueError as error:
        assert "must occur exactly once" in str(error)
    else:
        raise AssertionError("an ambiguous anchor without resolver plumbing must still fail closed")

    # A wrong (unknown) corrected candidate_id fails closed -- no
    # first-occurrence fallback -- and the failure carries no candidate_id.
    def _choose_invalid(request: AnchorCorrectionRequest) -> str:
        return "not-a-real-candidate-id"

    try:
        materialize_evidence(
            ambiguous_selection, resolver_pack,
            common_ir_source_sha256=trusted_sha256, resolve_ambiguous_value_anchor=_choose_invalid,
        )
    except AmbiguousAnchorCorrectionError as error:
        assert error.candidate_count == 2
        assert error.fact_id == "ambiguous-anchor"
    else:
        raise AssertionError("an unknown corrected candidate_id must fail closed")

    # --- Accepted Opus review follow-up: 8 focused checks -----------------

    # (1) Shared overlapping-occurrence ambiguity semantics: str.count
    # undercounts a self-overlapping repeat ("아아아아".count("아아아") == 1
    # even though it genuinely occurs at both offset 0 and offset 1); no such
    # repeat may bypass CandidatePack Anchor Occurrence Resolver v1.
    assert find_all_occurrences("아아아아", "아아아") == [0, 1]
    try:
        materialize_value_source("block", "아아아아", "아아아")
    except ValueError as error:
        assert "must occur exactly once" in str(error)
    else:
        raise AssertionError("a self-overlapping repeat must not resolve as unique")

    overlap_pack = CandidatePack.model_validate({
        "pack_id": "overlap-pack", "notice_id": "PBLN-overlap", "question": "overlap",
        "generator": "semantic_structuring.common_ir_v1", "generator_version": "1",
        "common_ir_document_id": "hwpx:PBLN-overlap",
        "blocks": [{
            "block_id": "hwpx:t2#r1c1p0", "text": "아아아아",
            "relation": "candidate", "common_ir_block_id": "hwpx:t2",
        }],
    })
    overlap_selection = SourceSelectionExtractionV02.model_validate({
        "notice_id": "PBLN-overlap", "candidate_pack_id": "overlap-pack",
        "component_decision": {"mode": "none", "no_component_reason": "no separate package"},
        "facts": [{"fact_id": "overlap-anchor", "field_name": "support_content", "status": "identified",
                   "value_anchor": {"source_block_id": "hwpx:t2#r1c1p0", "anchor_text": "아아아"}}],
    })
    # End to end through materialize_evidence: without resolver plumbing this
    # must fail closed exactly like any other repeated anchor.
    try:
        materialize_evidence(overlap_selection, overlap_pack)
    except ValueError as error:
        assert "must occur exactly once" in str(error)
    else:
        raise AssertionError("materialize_evidence must not treat an overlapping repeat as unique")

    # With resolver plumbing, the overlap is exposed as two real candidates
    # (offsets 0 and 1), and the callback's choice materializes to that
    # occurrence's own exact overlapping span.
    overlap_trusted_sha256 = "d" * 64
    overlap_resolution = resolve_anchor_occurrences(
        AnchorOccurrenceRequest(
            common_ir_document_id="hwpx:PBLN-overlap",
            common_ir_source_sha256=overlap_trusted_sha256,
            candidate_pack_id="overlap-pack",
            candidate_pack_generator="semantic_structuring.common_ir_v1",
            candidate_pack_generator_version="1",
            source_block_id="hwpx:t2#r1c1p0",
            anchor_text="아아아",
        ),
        overlap_pack,
        trusted_common_ir_source_sha256=overlap_trusted_sha256,
    )
    assert [candidate.start_char for candidate in overlap_resolution.candidates] == [0, 1]
    overlap_second_candidate_id = overlap_resolution.candidates[1].candidate_id

    def _choose_overlap_second(request: AnchorCorrectionRequest) -> str:
        assert len(request.candidates) == 2
        return overlap_second_candidate_id

    overlap_evidence = materialize_evidence(
        overlap_selection, overlap_pack,
        common_ir_source_sha256=overlap_trusted_sha256,
        resolve_ambiguous_value_anchor=_choose_overlap_second,
    )[0]
    assert overlap_evidence.value_source.start_char == 1
    assert overlap_evidence.value_source.end_char == 4

    # (2) support_scale numeric candidates bind by the fact's resolved
    # value_source span, never by anchor_text containment: two facts anchored
    # to two different occurrences of the identical numeral text must each
    # bind only to their own occurrence's numeric candidate.
    repeated_number_pack = CandidatePack.model_validate({
        "pack_id": "repeated-number-pack", "notice_id": "PBLN-repeated-number", "question": "repeated-number",
        "blocks": [{
            "block_id": "body[1]", "text": "1차 300만원 지원, 2차 300만원 지원",
            "relation": "candidate",
        }],
    })
    repeated_number_selection = SourceSelectionExtractionV02.model_validate({
        "notice_id": "PBLN-repeated-number", "candidate_pack_id": "repeated-number-pack",
        "component_decision": {"mode": "none", "no_component_reason": "no separate package"},
        "facts": [
            {"fact_id": "scale-1", "field_name": "support_scale", "status": "identified",
             "value_anchor": {"source_block_id": "body[1]", "anchor_text": "1차 300만원"}},
            {"fact_id": "scale-2", "field_name": "support_scale", "status": "identified",
             "value_anchor": {"source_block_id": "body[1]", "anchor_text": "2차 300만원"}},
        ],
    })
    repeated_number_evidence = materialize_evidence(repeated_number_selection, repeated_number_pack)
    repeated_number_resolved = {
        row.fact_id: row.value_source for row in repeated_number_evidence if row.value_source is not None
    }
    repeated_number_candidates = build_numeric_candidates(repeated_number_pack)
    assert len(repeated_number_candidates) == 2
    repeated_number_measures = derive_support_scale_measures_v02(
        repeated_number_selection, repeated_number_candidates, repeated_number_resolved
    )
    assert len(repeated_number_measures) == 1
    all_repeated_number_measures = repeated_number_measures[0].measures
    assert len(all_repeated_number_measures) == 2
    measures_by_fact = {measure.source_fact_id: measure for measure in all_repeated_number_measures}
    assert set(measures_by_fact) == {"scale-1", "scale-2"}
    numeric_candidates_by_id = {c.numeric_candidate_id: c for c in repeated_number_candidates}
    for fact_id, measure in measures_by_fact.items():
        bound_candidate = numeric_candidates_by_id[measure.source_numeric_candidate_id]
        resolved = repeated_number_resolved[fact_id]
        assert resolved.start_char <= bound_candidate.start_char and bound_candidate.end_char <= resolved.end_char
    assert measures_by_fact["scale-1"].source_numeric_candidate_id != measures_by_fact["scale-2"].source_numeric_candidate_id

    # A support-limit table header may be represented as the Raw Fact's
    # semantic_role while the selected numeric cell contains only ``18억원``.
    # It must normalize as a limit, not a generic support amount.
    table_limit_selection = SourceSelectionExtractionV02.model_validate({
        "notice_id": "PBLN-table-limit", "candidate_pack_id": "repeated-number-pack",
        "component_decision": {"mode": "none", "no_component_reason": "no separate package"},
        "facts": [{
            "fact_id": "table-limit", "field_name": "support_scale", "status": "identified",
            "semantic_role": "support_limit",
            "value_anchor": {"source_block_id": "body[1]", "anchor_text": "1차 300만원"},
        }],
    })
    table_limit_evidence = materialize_evidence(table_limit_selection, repeated_number_pack)
    table_limit_resolved = {row.fact_id: row.value_source for row in table_limit_evidence if row.value_source is not None}
    table_limit_measures = derive_support_scale_measures_v02(
        table_limit_selection, repeated_number_candidates, table_limit_resolved
    )
    assert table_limit_measures[0].measures[0].measure_role == MeasureRole.SUPPORT_LIMIT

    # The same decision can come from an explicitly linked table-header
    # context even when a model did not emit semantic_role on the value cell.
    table_header_limit_selection = table_limit_selection.model_copy(deep=True)
    table_header_limit_selection.facts[0].semantic_role = None
    table_header_limit_selection.facts[0].context_source_block_ids = ["table-header"]
    table_header_limit_measures = derive_support_scale_measures_v02(
        table_header_limit_selection,
        repeated_number_candidates,
        table_limit_resolved,
        source_block_texts={"table-header": "업체당 지원한도"},
    )
    assert table_header_limit_measures[0].measures[0].measure_role == MeasureRole.SUPPORT_LIMIT
    # The fixed validator agrees with the fixed generator's own binding.
    validate_scale_measure_candidates_v02(
        repeated_number_selection, repeated_number_measures, repeated_number_candidates, repeated_number_resolved
    )
    # A wrong (cross-occurrence) binding -- scale-1's measure pointed at
    # scale-2's numeral -- is rejected by the fixed validator.
    broken_measure = measures_by_fact["scale-1"].model_copy(
        update={"source_numeric_candidate_id": measures_by_fact["scale-2"].source_numeric_candidate_id}
    )
    broken_projection = repeated_number_measures[0].model_copy(update={"measures": [broken_measure]})
    try:
        validate_scale_measure_candidates_v02(
            repeated_number_selection, [broken_projection], repeated_number_candidates, repeated_number_resolved
        )
    except ValueError as error:
        assert "resolved value_source span" in str(error)
    else:
        raise AssertionError("a cross-occurrence numeric candidate binding must be rejected")

    # (3) Correction prompt objects carry no offset field, and their context
    # preserves whitespace exactly rather than stripping it.
    assert set(AnchorCorrectionCandidatePrompt.model_fields) == {
        "candidate_id", "anchor_text", "context_before", "context_after"
    }
    assert set(AnchorCorrectionRequest.model_fields) == {
        "fact_id", "field_name", "source_block_id", "anchor_text", "candidates"
    }
    whitespace_pack = CandidatePack.model_validate({
        "pack_id": "whitespace-pack", "notice_id": "PBLN-whitespace", "question": "whitespace",
        "generator": "semantic_structuring.common_ir_v1", "generator_version": "1",
        "common_ir_document_id": "hwpx:PBLN-whitespace",
        "blocks": [{
            "block_id": "hwpx:t3#r1c1p0", "text": "A  지원금  \n  B  지원금  C",
            "relation": "candidate", "common_ir_block_id": "hwpx:t3",
        }],
    })
    whitespace_trusted_sha256 = "e" * 64
    whitespace_resolution = resolve_anchor_occurrences(
        AnchorOccurrenceRequest(
            common_ir_document_id="hwpx:PBLN-whitespace",
            common_ir_source_sha256=whitespace_trusted_sha256,
            candidate_pack_id="whitespace-pack",
            candidate_pack_generator="semantic_structuring.common_ir_v1",
            candidate_pack_generator_version="1",
            source_block_id="hwpx:t3#r1c1p0",
            anchor_text="지원금",
        ),
        whitespace_pack,
        trusted_common_ir_source_sha256=whitespace_trusted_sha256,
        context_window=4,
    )
    whitespace_request = build_anchor_correction_request("wf", FactField.SUPPORT_CONTENT, whitespace_resolution)
    resolution_by_id = {candidate.candidate_id: candidate for candidate in whitespace_resolution.candidates}
    for prompt_candidate in whitespace_request.candidates:
        resolution_candidate = resolution_by_id[prompt_candidate.candidate_id]
        assert prompt_candidate.context_before == resolution_candidate.context_before
        assert prompt_candidate.context_after == resolution_candidate.context_after
    # At least one candidate's context has meaningful, non-trivial whitespace
    # that a whitespace-stripping model would silently destroy.
    assert any(
        candidate.context_before != candidate.context_before.strip()
        or candidate.context_after != candidate.context_after.strip()
        for candidate in whitespace_request.candidates
    )

    # (4) Candidate prompt order is deterministic by candidate_id, not by
    # document occurrence order.
    order_pack = CandidatePack.model_validate({
        "pack_id": "order-pack", "notice_id": "PBLN-order", "question": "order",
        "generator": "semantic_structuring.common_ir_v1", "generator_version": "1",
        "common_ir_document_id": "hwpx:PBLN-order",
        "blocks": [{
            "block_id": "hwpx:t5#r1c1p0", "text": "지원금 300만원, 추가 지원금 300만원, 또 지원금",
            "relation": "candidate", "common_ir_block_id": "hwpx:t5",
        }],
    })
    order_trusted_sha256 = "f" * 64
    order_resolution = resolve_anchor_occurrences(
        AnchorOccurrenceRequest(
            common_ir_document_id="hwpx:PBLN-order",
            common_ir_source_sha256=order_trusted_sha256,
            candidate_pack_id="order-pack",
            candidate_pack_generator="semantic_structuring.common_ir_v1",
            candidate_pack_generator_version="1",
            source_block_id="hwpx:t5#r1c1p0",
            anchor_text="지원금",
        ),
        order_pack,
        trusted_common_ir_source_sha256=order_trusted_sha256,
    )
    assert len(order_resolution.candidates) == 3
    document_order_ids = [candidate.candidate_id for candidate in order_resolution.candidates]
    order_request = build_anchor_correction_request("of", FactField.SUPPORT_CONTENT, order_resolution)
    prompt_order_ids = [candidate.candidate_id for candidate in order_request.candidates]
    assert prompt_order_ids == sorted(document_order_ids)
    assert prompt_order_ids != document_order_ids  # not merely document occurrence order

    # (5) A correction resolver's own failure (API/network/parse/validation
    # error) fails closed as a classified non-ValueError: it must never be
    # mistaken, by a caller matching on ValueError, for an ordinary
    # selection-contract validation failure that re-enters that model's
    # repair loop, and it must never leak the underlying error's message.
    def _broken_resolver(request: AnchorCorrectionRequest) -> str:
        raise ValueError("leaked-secret-response-fragment")

    broken_resolver_fact = SourceSelectionExtractionV02.model_validate({
        "notice_id": "PBLN-resolver", "candidate_pack_id": "resolver-pack",
        "component_decision": {"mode": "none", "no_component_reason": "no separate package"},
        "facts": [resolver_fact("broken-resolver-fact", "지원금")],
    }).facts[0]
    try:
        resolve_value_anchor_with_occurrence_resolver(
            broken_resolver_fact, resolver_pack,
            common_ir_source_sha256=trusted_sha256, correction_resolver=_broken_resolver,
        )
    except CorrectionResolverError as error:
        assert not isinstance(error, ValueError)
        assert error.error_classification == "correction_resolver_failed"
        assert error.candidate_count == 2
        assert "leaked-secret-response-fragment" not in str(error)
    else:
        raise AssertionError("a broken correction resolver must fail closed as CorrectionResolverError")

    caught_as_value_error = False
    try:
        materialize_evidence(
            ambiguous_selection, resolver_pack,
            common_ir_source_sha256=trusted_sha256, resolve_ambiguous_value_anchor=_broken_resolver,
        )
    except ValueError:
        caught_as_value_error = True
    except CorrectionResolverError as error:
        assert "leaked-secret-response-fragment" not in str(error)
    assert not caught_as_value_error

    # (6) The runner fails fast for a Common IR mismatch/missing/non-SHA
    # source identity, and never silently disables the resolver for a
    # Common IR pack.
    good_document = {
        "document": {
            "document_id": "hwpx:PBLN-resolver", "source_kind": "hwpx",
            "provenance": {"source_sha256": trusted_sha256},
        },
    }
    assert trusted_common_ir_source_sha256(good_document, resolver_pack) == trusted_sha256

    mismatched_document = deepcopy(good_document)
    mismatched_document["document"]["document_id"] = "hwpx:PBLN-different"
    try:
        trusted_common_ir_source_sha256(mismatched_document, resolver_pack)
    except RuntimeError as error:
        assert "does not match" in str(error)
    else:
        raise AssertionError("a Common IR document_id mismatch must fail fast")

    missing_sha_document = deepcopy(good_document)
    missing_sha_document["document"]["provenance"] = {}
    try:
        trusted_common_ir_source_sha256(missing_sha_document, resolver_pack)
    except RuntimeError as error:
        assert "SHA-256" in str(error)
    else:
        raise AssertionError("a missing source_sha256 must fail fast")

    non_hex_sha_document = deepcopy(good_document)
    non_hex_sha_document["document"]["provenance"] = {"source_sha256": "A" * 64}
    try:
        trusted_common_ir_source_sha256(non_hex_sha_document, resolver_pack)
    except RuntimeError as error:
        assert "SHA-256" in str(error)
    else:
        raise AssertionError("a non-lowercase-hex source_sha256 must fail fast")

    try:
        trusted_common_ir_source_sha256(good_document, span_pack)
    except RuntimeError as error:
        assert "common_ir_document_id" in str(error)
    else:
        raise AssertionError("a pack with no common_ir_document_id must fail fast")

    # (7) Memoization: two correction requests for the same deterministic
    # resolution (same source_block_id/anchor_text, whatever fact_id asked)
    # invoke the underlying resolver only once.
    memo_calls: list[str] = []

    def _counting_resolver(request: AnchorCorrectionRequest) -> str:
        memo_calls.append(request.fact_id)
        return sorted(request.candidates, key=lambda candidate: candidate.candidate_id)[0].candidate_id

    memoized_resolver = memoize_anchor_correction_resolver(_counting_resolver)
    memo_request_a = build_anchor_correction_request("fact-a", FactField.SUPPORT_CONTENT, independent_resolution)
    memo_request_b = build_anchor_correction_request("fact-b", FactField.SUPPORT_CONTENT, independent_resolution)
    memo_result_a = memoized_resolver(memo_request_a)
    memo_result_b = memoized_resolver(memo_request_b)
    assert memo_calls == ["fact-a"]  # the second call was a cache hit
    assert memo_result_a == memo_result_b

    # A different resolution (different source_block_id/anchor_text) is not
    # memoized against the first.
    memo_request_c = build_anchor_correction_request("fact-c", FactField.SUPPORT_CONTENT, order_resolution)
    memoized_resolver(memo_request_c)
    assert memo_calls == ["fact-a", "fact-c"]

    # (8) Audit metadata for corrected anchors only: fact_id, source_block_id,
    # candidate_count, and the final value_source -- never a candidate_id --
    # and a stale entry from a discarded attempt is dropped.
    audit_evidence = materialize_evidence(
        ambiguous_selection, resolver_pack,
        common_ir_source_sha256=trusted_sha256, resolve_ambiguous_value_anchor=_choose_second,
    )
    audit_correction_audit = {
        "ambiguous-anchor": {"source_block_id": "hwpx:t1#r1c1p0", "candidate_count": 2},
        "stale-discarded-fact": {"source_block_id": "hwpx:t1#r1c1p0", "candidate_count": 2},
    }
    audit_rows = build_corrected_anchor_audit(audit_evidence, audit_correction_audit)
    assert audit_rows == [{
        "fact_id": "ambiguous-anchor",
        "source_block_id": "hwpx:t1#r1c1p0",
        "candidate_count": 2,
        "value_source": audit_evidence[0].value_source.model_dump(mode="json"),
    }]
    assert all("candidate_id" not in row for row in audit_rows)

    # v0.2 assembly requires Common IR lineage end to end: the selection
    # artifact's common_ir_identity, each materialized source block's
    # common_ir_document_id/common_ir_block_id, and the metadata lineage
    # stamped onto source_documents must all agree.
    span_common_ir_lineage = {
        "document_id": "hwp:PBLN-span", "schema_version": "common_ir_v1", "source_kind": "hwp",
        "source_sha256": "test-sha256", "source_location": "test://fixture",
    }
    span_metadata = {"notice_id": "PBLN-span", "common_ir": span_common_ir_lineage}
    span_common_ir_identity = {
        "document_id": span_common_ir_lineage["document_id"],
        "source_kind": span_common_ir_lineage["source_kind"],
        "source_sha256": span_common_ir_lineage["source_sha256"],
    }
    span_source_block = {
        "source_block_id": "body[42]", "text": "각 300만원", "section_id": None, "source_occurrence_ids": [],
        "common_ir_document_id": span_common_ir_lineage["document_id"], "common_ir_block_id": "body[42]",
    }
    assembled_v02 = assemble_final_profile_v02(
        {
            "selection_contract": "v0.2_anchor",
            "selection": span_selection.model_dump(mode="json"),
            "source_block_texts": {"body[42]": "창업지원금 각 300만원 및 멘토링 지원"},
            "common_ir_identity": span_common_ir_identity,
            "candidate_pack_lineage": {
                "candidate_pack_id": span_selection.candidate_pack_id,
                "candidate_pack_generator": "semantic_structuring.common_ir_v1",
                "candidate_pack_generator_version": "1",
                "common_ir_document_id": span_common_ir_lineage["document_id"],
                "common_ir_source_sha256": span_common_ir_lineage["source_sha256"],
                "text_basis": "common_ir_v1_candidate_pack",
            },
            "materialized_evidence": [{
                "fact_id": "scale", "field_name": "support_scale", "status": "identified",
                "semantic_role": None, "subject_role": None,
                "source_blocks": [span_source_block],
                "value_source": source.model_dump(mode="json"), "context_blocks": [],
                "organization_names": [], "organization_sources": [], "role_raw": None,
                "role_source_block_id": None, "role_source": None, "canonical_role": None,
                "primary_component_id": None, "applicability_component_ids": [],
                "modifies_fact_ids": [], "recipient_fact_ids": [], "basis_fact_ids": [],
            }],
            "materialized_components": [],
        },
        span_metadata,
    )
    assert assembled_v02["schema_version"] == "existing_program_profile/v0.2"
    assert assembled_v02["comparison_profile"]["support_scale"][0]["fact_id"] == "scale"
    assert assembled_v02["comparison_profile"]["support_scale"][0]["value_raw"] == "각 300만원"
    # No support_scale_measures projection was supplied, so the assembler must
    # not stamp a numeric extractor version onto processing lineage.
    assert "derived_projection_producers" not in assembled_v02["processing_metadata"]

    assembled_v02_with_measures = assemble_final_profile_v02(
        {
            "selection_contract": "v0.2_anchor",
            "selection": span_selection.model_dump(mode="json"),
            "source_block_texts": {"body[42]": "창업지원금 각 300만원 및 멘토링 지원"},
            "common_ir_identity": span_common_ir_identity,
            "candidate_pack_lineage": {
                "candidate_pack_id": span_selection.candidate_pack_id,
                "candidate_pack_generator": "semantic_structuring.common_ir_v1",
                "candidate_pack_generator_version": "1",
                "common_ir_document_id": span_common_ir_lineage["document_id"],
                "common_ir_source_sha256": span_common_ir_lineage["source_sha256"],
                "text_basis": "common_ir_v1_candidate_pack",
            },
            "materialized_evidence": [{
                "fact_id": "scale", "field_name": "support_scale", "status": "identified",
                "semantic_role": None, "subject_role": None,
                "source_blocks": [span_source_block],
                "value_source": source.model_dump(mode="json"), "context_blocks": [],
                "organization_names": [], "organization_sources": [], "role_raw": None,
                "role_source_block_id": None, "role_source": None, "canonical_role": None,
                "primary_component_id": None, "applicability_component_ids": [],
                "modifies_fact_ids": [], "recipient_fact_ids": [], "basis_fact_ids": [],
            }],
            "materialized_components": [],
        },
        span_metadata,
        derived_projections=[{
            "projection_type": "support_scale_measures", "source_fact_ids": ["scale"], "status": "identified",
            "measures": [{"measure_type": "amount", "measure_role": "support_amount", "lower_value": 3000000,
                          "upper_value": 3000000, "unit": "KRW", "comparator": "eq",
                          "source_fact_id": "scale", "source_numeric_candidate_id": "body[42]#num[0]"}],
        }],
    )
    # A support_scale_measures projection was supplied this time, so the
    # assembler must stamp the exact known numeric extractor version at its
    # nested derived_projection_producers path.
    assert (
        assembled_v02_with_measures["processing_metadata"]["derived_projection_producers"]
        ["support_scale_measures"]["numeric_candidate_extractor_version"]
        == NUMERIC_CANDIDATE_EXTRACTOR_VERSION
    )
    common_ir_v03 = {
        "schema_version": "common_ir_v0_3",
        "document": {"document_id": "pdf:PBLN-projected", "source_kind": "pdf"},
        "conflicts": [],
        "relations": [],
        "blocks": [
            {
                "block_id": "native-1",
                "kind": "paragraph",
                "structure_status": "explicit",
                "occurrences": [{"occurrence_id": "native-1", "role": "native_text", "text": "지원 대상"}],
                "provenance": {"page": 1, "bbox": [0, 0, 10, 10]},
            },
            {
                "block_id": "ocr-duplicate",
                "kind": "paragraph",
                "structure_status": "explicit",
                "occurrences": [{"occurrence_id": "ocr-duplicate", "role": "ocr_text", "text": "지원 대상"}],
                "provenance": {"page": 1, "bbox": [0, 0, 10, 10]},
            },
            {
                "block_id": "table-1",
                "kind": "table",
                "structure_status": "explicit",
                "occurrences": [{"occurrence_id": "cell-1", "role": "ocr_text", "text": "300만원"}],
                "cells": [{"cell_id": "table-1-cell-1", "row_index": 0, "col_index": 0, "text_occurrence_ids": ["cell-1"]}],
                "provenance": {"page": 1, "bbox": [0, 10, 10, 20]},
            },
        ],
    }
    projected_common_ir = project_common_ir_v03(common_ir_v03)
    assert [block.block_id for block in projected_common_ir.blocks] == ["native-1", "table-1"]
    assert [block.source_order for block in projected_common_ir.blocks] == [0, 2]
    assert projected_common_ir.dropped_duplicate_ocr_block_ids == ["ocr-duplicate"]
    assert projected_common_ir.blocks[1].text == "[table] r0c0: 300만원"
    assert [(block.block_id, block.text) for block in projected_common_ir.table_cell_blocks] == [("table-1#r0c0p0", "300만원")]

    nested_table_common_ir = {
        "schema_version": "common_ir_v0_3",
        "document": {"document_id": "hwpx:PBLN-nested", "source_kind": "hwpx"},
        "blocks": [
            {
                "block_id": "hwpx:t10",
                "kind": "table",
                "structure_status": "explicit",
                "occurrences": [{"occurrence_id": "outer", "role": "rhwp_cell", "text": "TOP 1\t상금 최대5억원"}],
                "cells": [{"cell_id": "hwpx:t10:c0", "row_index": 0, "col_index": 0, "text_occurrence_ids": ["outer"]}],
            },
            {
                "block_id": "hwpx:t10.c0.b1",
                "kind": "table",
                "structure_status": "explicit",
                "occurrences": [
                    {"occurrence_id": "header", "role": "rhwp_cell", "text": "TOP 1"},
                    {"occurrence_id": "amount", "role": "rhwp_cell", "text": "상금 최대5억원"},
                ],
                "cells": [
                    {"cell_id": "header-cell", "row_index": 0, "col_index": 0, "text_occurrence_ids": ["header"]},
                    {"cell_id": "amount-cell", "row_index": 1, "col_index": 0, "text_occurrence_ids": ["amount"]},
                ],
            },
        ],
    }
    nested_projection = project_common_ir_v03(nested_table_common_ir)
    assert [block.block_id for block in nested_projection.blocks] == ["hwpx:t10.c0.b1"]
    assert nested_projection.blocks[0].text == "[table] r0c0: TOP 1 | r1c0: 상금 최대5억원"
    assert nested_projection.suppressed_nested_wrapper_block_ids == ["hwpx:t10"]
    assert [(block.block_id, block.text) for block in nested_projection.table_cell_blocks] == [
        ("hwpx:t10.c0.b1#r0c0p0", "TOP 1"),
        ("hwpx:t10.c0.b1#r1c0p0", "상금 최대5억원"),
    ]
    common_prepared, _ = prepare_common_ir_notice(nested_table_common_ir)
    assert [block.block_id for block in common_prepared.table_candidate_blocks] == ["hwpx:t10.c0.b1"]
    assert [block.block_id for block in common_prepared.table_cell_candidate_blocks] == [
        "hwpx:t10.c0.b1#r0c0p0", "hwpx:t10.c0.b1#r1c0p0"
    ]
    try:
        build_routed_a_pack(common_prepared, [])
    except ValueError as error:
        assert "section scope has not been applied" in str(error)
    else:
        raise AssertionError("unscoped Common IR bridge must not enter A extraction")

    common_ir_v1 = {
        "schema_version": "common_ir_v1",
        "document": {
            "document_id": "hwpx:PBLN-v1", "source_kind": "hwpx", "artifact_role": "production",
            "provenance": {"source_sha256": "abc123", "source_location": "s3://bucket/key"},
        },
        "blocks": [
            {"block_id": "hwpx:p0", "kind": "paragraph", "structure_status": "explicit", "text": "본문", "text_occurrence_ids": ["p0"], "reading_order": 0, "page": None, "section_path": "", "occurrences": [{"occurrence_id": "p0", "text": "본문"}], "boundary_markers": [], "provenance": {}},
            {"block_id": "hwpx:t1", "kind": "table", "structure_status": "explicit", "text": "", "text_occurrence_ids": ["outer"], "reading_order": 1, "page": None, "section_path": "", "occurrences": [{"occurrence_id": "outer", "text": ""}, {"occurrence_id": "value", "text": "상금 최대5억원"}], "cells": [{"cell_id": "hwpx:t1:c0", "evidence_ids": ["outer"], "row_index": 0, "col_index": 0, "text_occurrence_ids": ["outer"]}], "boundary_markers": [], "provenance": {}},
            {"block_id": "hwpx:t1.c0.b1", "kind": "table", "structure_status": "explicit", "text": "상금 최대5억원", "text_occurrence_ids": ["value"], "reading_order": 2, "page": None, "section_path": "", "occurrences": [{"occurrence_id": "value", "text": "상금 최대5억원"}], "cells": [{"cell_id": "hwpx:t1.c0.b1:c0", "evidence_ids": ["value"], "row_index": 0, "col_index": 0, "text_occurrence_ids": ["value"]}], "boundary_markers": [], "provenance": {}},
            {"block_id": "hwpx:p3", "kind": "paragraph", "structure_status": "explicit", "text": "[서식 1] 신청서", "text_occurrence_ids": ["p3"], "reading_order": 3, "page": None, "section_path": "", "occurrences": [{"occurrence_id": "p3", "text": "[서식 1] 신청서"}], "boundary_markers": [{"marker": "서식", "matched_text": "[서식 1]"}], "provenance": {}},
        ],
        "relations": [{"kind": "table_contains", "from_id": "hwpx:t1:c0", "to_id": "hwpx:t1.c0.b1", "inferred": False, "structure_status": "explicit"}],
        "conflicts": [],
    }
    common_v1_prepared, common_v1_projection = prepare_common_ir_v1(common_ir_v1)
    assert common_v1_projection.common_ir_document_id == "hwpx:PBLN-v1"
    assert common_v1_prepared.router_pack().generator == "semantic_structuring.common_ir_v1"
    assert common_v1_prepared.router_pack().generator_version == "1"
    assert common_v1_prepared.router_pack().common_ir_document_id == "hwpx:PBLN-v1"
    assert [block.block_id for block in common_v1_projection.blocks] == ["hwpx:p0", "hwpx:t1.c0.b1", "hwpx:p3"]
    assert [(block.block_id, block.text) for block in common_v1_projection.table_cell_blocks] == [("hwpx:t1.c0.b1#r0c0p0", "상금 최대5억원")]
    assert [section.section_id for section in common_v1_projection.sections] == ["main_notice", "attachment_1"]
    assert common_v1_prepared.section_scope_applied is False
    scoped_common_v1 = apply_common_ir_v1_section_scopes(
        common_ir_v1,
        [SectionScopeDecision.model_validate({"section_id": "attachment_1", "scope": "form_template"})],
    )
    assert scoped_common_v1.section_scope_applied is True
    assert [block.block_id for block in scoped_common_v1.fact_candidate_blocks] == ["hwpx:p0"]
    assert [block.block_id for block in scoped_common_v1.table_candidate_blocks] == ["hwpx:t1.c0.b1"]
    assert [block.block_id for block in scoped_common_v1.table_cell_candidate_blocks] == ["hwpx:t1.c0.b1#r0c0p0"]
    assert [block.block_id for block in scoped_common_v1.excluded_blocks] == ["hwpx:p3"]
    common_v1_a_pack, common_v1_a_metrics = build_routed_a_pack(
        scoped_common_v1,
        [
            {"source_block_id": "hwpx:p0", "route_tags": ["support"]},
            {
                "source_block_id": "hwpx:t1.c0.b1", "route_tags": ["support"],
                "table_disposition": "a_fact_candidate",
            },
        ],
    )
    assert common_v1_a_pack is not None
    assert common_v1_a_pack.generator == "semantic_structuring.common_ir_v1"
    assert common_v1_a_pack.generator_version == "1"
    assert common_v1_a_pack.common_ir_document_id == "hwpx:PBLN-v1"
    common_v1_cell_block = next(
        block for block in common_v1_a_pack.blocks if block.block_kind == "table_cell"
    )
    assert common_v1_cell_block.block_id == "hwpx:t1.c0.b1#r0c0p0"
    assert common_v1_cell_block.common_ir_block_id == "hwpx:t1.c0.b1"
    assert common_v1_cell_block.common_ir_cell_id == "hwpx:t1.c0.b1:c0"
    assert common_v1_cell_block.common_ir_occurrence_ids == ("value",)
    assert common_v1_a_metrics["a_table_cell_total"] == 1

    common_v1_selection = SourceSelectionExtractionV02.model_validate({
        "notice_id": "PBLN-v1", "candidate_pack_id": common_v1_a_pack.pack_id,
        "component_decision": {"mode": "none", "no_component_reason": "single notice fact"},
        "facts": [{
            "fact_id": "common-ir-scale", "field_name": "support_scale", "status": "identified",
            "value_anchor": {
                "source_block_id": common_v1_cell_block.block_id,
                "anchor_text": "상금 최대5억원",
            },
        }],
    })
    common_v1_evidence = materialize_evidence(common_v1_selection, common_v1_a_pack)[0]
    assert common_v1_evidence.source_blocks == [{
        "source_block_id": "hwpx:t1.c0.b1#r0c0p0",
        "text": "상금 최대5억원",
        "section_id": "main_notice",
        "source_occurrence_ids": ["value"],
        "common_ir_document_id": "hwpx:PBLN-v1",
        "common_ir_block_id": "hwpx:t1.c0.b1",
        "common_ir_occurrence_ids": ["value"],
        "common_ir_cell_id": "hwpx:t1.c0.b1:c0",
    }]

    # Common IR metadata lineage exposes exactly document_id, schema_version,
    # source_kind, source_sha256, source_location, and optional artifact_role
    # -- no business id (notice_id) merged in, and schema_version is stamped.
    common_ir_v1_meta = common_ir_v1_metadata(common_ir_v1)
    assert common_ir_v1_meta["notice_id"] == "PBLN-v1"
    assert common_ir_v1_meta["common_ir"] == {
        "document_id": "hwpx:PBLN-v1",
        "schema_version": "common_ir_v1",
        "source_kind": "hwpx",
        "source_sha256": "abc123",
        "source_location": "s3://bucket/key",
        "artifact_role": "production",
    }
    common_v1_profile = assemble_final_profile_v02(
        {
            "selection_contract": "v0.2_anchor",
            "selection": common_v1_selection.model_dump(mode="json"),
            "source_block_texts": {
                block.block_id: block.text for block in common_v1_a_pack.blocks
            },
            "materialized_evidence": [{
                "fact_id": common_v1_evidence.fact_id,
                "field_name": common_v1_evidence.field_name.value,
                "status": common_v1_evidence.status.value,
                "semantic_role": common_v1_evidence.semantic_role,
                "subject_role": common_v1_evidence.subject_role,
                "source_blocks": common_v1_evidence.source_blocks,
                "value_source": common_v1_evidence.value_source.model_dump(mode="json"),
                "context_blocks": common_v1_evidence.context_blocks,
                "organization_names": [], "organization_sources": [],
                "role_raw": None, "role_source_block_id": None, "role_source": None,
                "canonical_role": None, "primary_component_id": None,
                "applicability_component_ids": [], "modifies_fact_ids": [],
                "recipient_fact_ids": [], "basis_fact_ids": [],
            }],
            "materialized_components": [],
            "common_ir_identity": {
                "document_id": "hwpx:PBLN-v1",
                "source_kind": "hwpx",
                "source_sha256": "abc123",
            },
            "candidate_pack_lineage": {
                "candidate_pack_id": common_v1_a_pack.pack_id,
                "candidate_pack_generator": common_v1_a_pack.generator,
                "candidate_pack_generator_version": common_v1_a_pack.generator_version,
                "common_ir_document_id": common_v1_a_pack.common_ir_document_id,
                "common_ir_source_sha256": "abc123",
                "text_basis": "common_ir_v1_candidate_pack",
            },
        },
        common_ir_v1_meta,
    )
    common_v1_profile_evidence = common_v1_profile["comparison_profile"]["support_scale"][0]["evidence"][0]
    assert common_v1_profile_evidence["source_block_id"] == "hwpx:t1.c0.b1#r0c0p0"
    assert common_v1_profile_evidence["common_ir_document_id"] == "hwpx:PBLN-v1"
    assert common_v1_profile_evidence["common_ir_block_id"] == "hwpx:t1.c0.b1"
    assert common_v1_profile_evidence["common_ir_cell_id"] == "hwpx:t1.c0.b1:c0"
    assert common_v1_profile_evidence["common_ir_occurrence_ids"] == ["value"]

    try:
        validate_common_ir_lineage({
            "schema_version": "common_ir_v1", "source_kind": "hwpx",
            "source_sha256": None, "source_location": None,
        })
    except ValueError as error:
        assert "missing required keys" in str(error)
    else:
        raise AssertionError("lineage missing document_id must be rejected")

    try:
        validate_common_ir_lineage({
            "document_id": "hwpx:PBLN-v1", "schema_version": "common_ir_v1", "source_kind": "hwpx",
            "source_sha256": None, "source_location": None, "notice_id": "PBLN-v1",
        })
    except ValueError as error:
        assert "extra keys" in str(error)
    else:
        raise AssertionError("lineage must not merge a business id alias like notice_id")

    try:
        validate_common_ir_lineage({
            "document_id": "hwpx:PBLN-v1", "schema_version": "common_ir_v0_3", "source_kind": "hwpx",
            "source_sha256": "abc123", "source_location": "s3://bucket/key",
        })
    except ValueError as error:
        assert "schema_version must be common_ir_v1" in str(error)
    else:
        raise AssertionError("lineage schema_version must be common_ir_v1")

    validate_common_ir_lineage({
        "document_id": "hwpx:PBLN-v1", "schema_version": "common_ir_v1", "source_kind": "hwpx",
        "source_sha256": "abc123", "source_location": "s3://bucket/key",
    })  # artifact_role may be omitted entirely: it is optional, not required-null

    # The final assembled profile's source_documents lineage must carry the
    # same exact shape end to end -- document_id and source_kind are no
    # longer silently dropped between common_ir_v1_metadata and assembly.
    lineage_profile = assemble_final_profile(
        {
            "selection": {"notice_id": "PBLN-v1", "support_components": []},
            "materialized_evidence": [],
            "common_ir_identity": {
                "document_id": common_ir_v1_meta["common_ir"]["document_id"],
                "source_kind": common_ir_v1_meta["common_ir"]["source_kind"],
                "source_sha256": common_ir_v1_meta["common_ir"]["source_sha256"],
            },
        },
        common_ir_v1_meta,
    )
    assert lineage_profile["source_documents"][0]["common_ir"] == common_ir_v1_meta["common_ir"]

    period_selection = SourceSelectionExtraction.model_validate(
        {
            "notice_id": "PBLN-period", "candidate_pack_id": "period-pack",
            "facts": [
                {"fact_id": "program", "field_name": "program_period", "value_source_block_ids": ["body[0]"], "status": "identified"},
                {"fact_id": "support", "field_name": "support_period", "value_source_block_ids": ["body[1]"], "status": "identified", "primary_component_id": "stage-1"},
            ],
            "support_components": [{"support_component_id": "stage-1", "component_kind": "stage_support", "source_block_ids": ["body[1]"]}],
            "component_decision": {"mode": "stages"},
        }
    )
    assert [fact.field_name for fact in period_selection.facts] == ["program_period", "support_period"]
    try:
        SourceSelectionExtraction.model_validate(
            {"notice_id": "PBLN-period", "candidate_pack_id": "period-pack", "facts": [{"fact_id": "invalid", "field_name": "program_period", "value_source_block_ids": ["body[0]"], "status": "identified", "primary_component_id": "stage-1"}], "support_components": [{"support_component_id": "stage-1", "component_kind": "stage_support", "source_block_ids": ["body[0]"]}], "component_decision": {"mode": "stages"}}
        )
    except ValidationError as error:
        assert "program_period must not be owned" in str(error)
    else:
        raise AssertionError("program_period must not be component-scoped")

    no_child_common_ir = deepcopy(nested_table_common_ir)
    no_child_common_ir["blocks"] = no_child_common_ir["blocks"][:1]
    assert [block.block_id for block in project_common_ir_v03(no_child_common_ir).blocks] == ["hwpx:t10"]

    wrapper_extra_text = deepcopy(nested_table_common_ir)
    wrapper_extra_text["blocks"][0]["occurrences"][0]["text"] = "표 설명\nTOP 1\t상금 최대5억원"
    assert [block.block_id for block in project_common_ir_v03(wrapper_extra_text).blocks] == ["hwpx:t10", "hwpx:t10.c0.b1"]

    empty_child = deepcopy(nested_table_common_ir)
    empty_child["blocks"][1]["occurrences"] = []
    assert [block.block_id for block in project_common_ir_v03(empty_child).blocks] == ["hwpx:t10"]

    NormalizedMeasure.model_validate(
        {
            "measure_id": "m-1",
            "fact_id": "fact-1",
            "measure_type": MeasureType.AMOUNT,
            "semantic_role": MeasureSemanticRole.SUPPORT_LIMIT,
            "upper_value": 3000000,
            "unit": MeasureUnit.KRW,
            "comparator": NumericComparator.LTE,
            "applies_per": MeasureUnit.TEAM,
            "money_basis": MoneyBasis.SUPPORT_AMOUNT,
            "source_numeric_candidate_ids": ["body[16]#num[0]"],
        }
    )
    try:
        NormalizedMeasure.model_validate(
            {
                "measure_id": "m-2",
                "fact_id": "fact-1",
                "measure_type": MeasureType.RATE,
                "semantic_role": MeasureSemanticRole.COST_SHARE,
                "lower_value": 60,
                "upper_value": 60,
                "unit": MeasureUnit.KRW,
                "comparator": NumericComparator.EQ,
                "source_numeric_candidate_ids": ["body[16]#num[1]"],
            }
        )
    except ValidationError as error:
        assert "rate measures require BPS" in str(error)
    else:
        raise AssertionError("rate values must use BPS")

    section_body = [
        {"kind": "paragraph", "text": "본문"},
        {"kind": "paragraph", "text": "【붙임 1】 지원단가표"},
        {"kind": "table", "text": "단가"},
        {"kind": "paragraph", "text": "[서식 1] 신청서"},
        {"kind": "table", "text": "성명"},
    ]
    sections = split_attachment_sections(section_body)
    assert [(item.section_id, item.start_block_index, item.end_block_index) for item in sections] == [
        ("main_notice", 0, 0),
        ("attachment_1", 1, 2),
        ("attachment_2", 3, 4),
    ]
    assert block_section_ids(sections)["body[4]"] == "attachment_2"
    table_header_sections = split_attachment_sections(
        [
            {"kind": "paragraph", "text": "본문"},
            {
                "kind": "table",
                "cells": [
                    {"row": 0, "col": 0, "blocks": [{"text": "■ [서식 1]"}]},
                    {"row": 1, "col": 0, "blocks": [{"text": "참여 신청서"}]},
                ],
            },
            {"kind": "table", "cells": [{"row": 0, "col": 0, "blocks": [{"text": "성명"}]}]},
        ]
    )
    assert [(item.section_id, item.start_block_index, item.end_block_index) for item in table_header_sections] == [
        ("main_notice", 0, 0),
        ("attachment_1", 1, 2),
    ]
    assert table_header_sections[1].title_raw == "■ [서식 1]"
    scope_artifact = Path("/tmp/section_scope_contract.json")
    scope_artifact.write_text(
        json.dumps(
            {
                "notice_id": "PBLN-test",
                "sections": [
                    {"scope": "main_notice", "source_block_range": ["body[0]", "body[1]"]},
                    {"scope": "form_template", "source_block_range": ["body[2]", "body[3]"]},
                    {"scope": "mixed_or_unresolved", "source_block_range": ["body[4]", "body[4]"]},
                ],
            }
        )
    )
    assert _section_scope_by_block(scope_artifact, notice_id="PBLN-test") == {
        "body[0]": "main_notice",
        "body[1]": "main_notice",
        "body[2]": "form_template",
        "body[3]": "form_template",
        "body[4]": "mixed_or_unresolved",
    }
    prepared = prepare_notice(
        {
            "notice_id": "PBLN-prepared",
            "ir": {
                "body": [
                    {"kind": "paragraph", "text": "지원 내용"},
                    {"kind": "table", "rows": 1, "cols": 1, "cells": []},
                    {"kind": "paragraph", "text": "【붙임 1】 신청서"},
                    {"kind": "table", "rows": 1, "cols": 1, "cells": []},
                ]
            },
        },
        [SectionScopeDecision(section_id="attachment_1", scope="form_template")],
    )
    assert [block.block_id for block in prepared.fact_candidate_blocks] == ["body[0]"]
    assert [block.block_id for block in prepared.table_candidate_blocks] == ["body[1]"]
    assert prepared.table_cell_candidate_blocks == []
    assert [block.block_id for block in prepared.excluded_blocks] == ["body[2]", "body[3]"]
    assert all(block.section_id == "main_notice" for block in prepared.router_pack().blocks)
    assert table_parent_id("body[45]#r2c1p0") == "body[45]"
    table_scope_pack = CandidatePack.model_validate({
        "pack_id": "table-scope", "notice_id": "PBLN-table-scope", "question": "table scope",
        "generator": "semantic_structuring.common_ir_v1", "generator_version": "1",
        "common_ir_document_id": "hwpx:PBLN-table-scope",
        "blocks": [
            {"block_id": "hwpx:t1#r0c0p0", "text": "헤더", "relation": "candidate", "common_ir_block_id": "hwpx:t1"},
            {"block_id": "hwpx:t1#r1c0p0", "text": "상품 A", "relation": "candidate", "common_ir_block_id": "hwpx:t1"},
            {"block_id": "hwpx:t2#r0c0p0", "text": "다른 표", "relation": "candidate", "common_ir_block_id": "hwpx:t2"},
            {"block_id": "hwpx:b3", "text": "본문", "relation": "candidate", "common_ir_block_id": "hwpx:b3"},
        ],
    })
    scoped_table_pack = restrict_a_pack_to_table_ids(table_scope_pack, ["hwpx:t1"])
    assert [block.block_id for block in scoped_table_pack.blocks] == ["hwpx:t1#r0c0p0", "hwpx:t1#r1c0p0"]
    assert scoped_table_pack.common_ir_document_id == "hwpx:PBLN-table-scope"
    try:
        restrict_a_pack_to_table_ids(table_scope_pack, ["hwpx:t404"])
    except ValueError as error:
        assert "not present" in str(error)
    else:
        raise AssertionError("unknown table scope must be rejected")
    a_packs = build_a_candidate_packs(
        prepared,
        {"body[0]": ["support"], "body[1]": ["table"]},
    )
    assert [block.block_id for block in a_packs["support"].blocks] == ["body[0]"]
    assert [block.block_id for block in build_combined_a_candidate_pack(a_packs).blocks] == ["body[0]"]

    routed_prepared = prepare_notice(
        {
            "notice_id": "PBLN-routed",
            "ir": {
                "body": [
                    {"kind": "paragraph", "text": "지원 내용"},
                    {
                        "kind": "table",
                        "rows": 2,
                        "cols": 2,
                        "cells": [
                            {"row": 0, "col": 0, "blocks": [{"text": "지원금"}]},
                            {"row": 0, "col": 1, "blocks": [{"text": "300만원"}]},
                            {"row": 1, "col": 0, "blocks": [{"text": "자부담"}]},
                            {"row": 1, "col": 1, "blocks": [{"text": "없음"}]},
                        ],
                    },
                    {"kind": "table", "rows": 1, "cols": 1, "cells": [{"row": 0, "col": 0, "blocks": [{"text": "상세 기준"}]}]},
                    {"kind": "table", "rows": 1, "cols": 1, "cells": [{"row": 0, "col": 0, "blocks": [{"text": "신청서 양식"}]}]},
                ]
            },
        },
        [],
    )
    routed_pack, routed_metrics = build_routed_a_pack(
        routed_prepared,
        [
            {"source_block_id": "body[0]", "route_tags": ["support"], "table_disposition": None},
            {"source_block_id": "body[1]", "route_tags": ["table"], "table_disposition": "a_fact_candidate"},
            {"source_block_id": "body[2]", "route_tags": ["table"], "table_disposition": "b_search_only"},
            {"source_block_id": "body[3]", "route_tags": ["table"], "table_disposition": "c_exclude"},
        ],
    )
    assert [block.block_id for block in routed_pack.blocks] == [
        "body[0]", "body[1]#r0c0p0", "body[1]#r0c1p0", "body[1]#r1c0p0", "body[1]#r1c1p0"
    ]
    assert routed_metrics["a_table_cell_total"] == 4
    try:
        build_routed_a_pack(routed_prepared, [{"source_block_id": "body[0]", "route_tags": ["support"], "table_disposition": None}])
    except ValueError as error:
        assert "coverage mismatch" in str(error)
    else:
        raise AssertionError("incomplete router artifact must fail closed")
    try:
        build_routed_a_pack(
            routed_prepared,
            [
                {"source_block_id": "body[0]", "route_tags": ["support"], "table_disposition": None},
                {"source_block_id": "body[1]", "route_tags": ["table"], "table_disposition": "wrong"},
                {"source_block_id": "body[2]", "route_tags": ["table"], "table_disposition": "b_search_only"},
                {"source_block_id": "body[3]", "route_tags": ["table"], "table_disposition": "c_exclude"},
            ],
        )
    except ValueError as error:
        assert "disposition" in str(error)
    else:
        raise AssertionError("invalid table disposition must fail closed")

    table_only_pack, _ = build_routed_a_pack(
        routed_prepared,
        [
            {"source_block_id": "body[0]", "route_tags": ["not_relevant"], "table_disposition": None},
            {"source_block_id": "body[1]", "route_tags": ["table"], "table_disposition": "a_fact_candidate"},
            {"source_block_id": "body[2]", "route_tags": ["table"], "table_disposition": "b_search_only"},
            {"source_block_id": "body[3]", "route_tags": ["table"], "table_disposition": "c_exclude"},
        ],
    )
    assert [block.block_id for block in table_only_pack.blocks] == [
        "body[1]#r0c0p0", "body[1]#r0c1p0", "body[1]#r1c0p0", "body[1]#r1c1p0"
    ]
    assert all("body[2]" not in block.block_id and "body[3]" not in block.block_id for block in routed_pack.blocks)

    oversized_prepared = prepare_notice(
        {"notice_id": "PBLN-oversized", "ir": {"body": [
            {"kind": "table", "rows": 301, "cols": 1, "cells": [
                {"row": index, "col": 0, "blocks": [{"text": str(index)}]} for index in range(301)
            ]}
        ]}},
        [],
    )
    oversized_pack, oversized_metrics = build_routed_a_pack(
        oversized_prepared,
        [{"source_block_id": "body[0]", "route_tags": ["table"], "table_disposition": "a_fact_candidate"}],
    )
    assert oversized_pack is None
    assert oversized_metrics["downgraded_to_b_table_ids"] == ["body[0]"]

    ordered_prepared = prepare_notice(
        {"notice_id": "PBLN-ordered", "ir": {"body": [
            {"kind": "table", "rows": 11, "cols": 11, "cells": [
                {"row": row, "col": col, "blocks": [{"text": f"{row}-{col}"}]}
                for row in range(11) for col in range(11)
            ]}
        ]}},
        [],
    )
    ordered_pack, _ = build_routed_a_pack(ordered_prepared, [{"source_block_id": "body[0]", "route_tags": ["table"], "table_disposition": "a_fact_candidate"}])
    ordered_ids = [block.block_id for block in ordered_pack.blocks]
    assert ordered_ids.index("body[0]#r0c2p0") < ordered_ids.index("body[0]#r0c10p0")
    assert ordered_ids.index("body[0]#r2c0p0") < ordered_ids.index("body[0]#r10c0p0")
    selection = SourceSelectionExtraction.model_validate(
        {
            "notice_id": "PBLN-prepared",
            "candidate_pack_id": prepared.fact_pack().pack_id,
            "facts": [
                {
                    "fact_id": "purpose-1",
                    "field_name": "purpose_goal",
                    "source_block_ids": ["body[0]"],
                    "status": "identified",
                }
            ],
        }
    )
    evidence = materialize_evidence(selection, prepared.fact_pack())
    assert evidence[0].source_blocks == [{"source_block_id": "body[0]", "text": "지원 내용", "section_id": "main_notice", "source_occurrence_ids": []}]
    assert evidence[0].organization_names == []
    assert materialize_components(selection, prepared.fact_pack()) == []

    cell_selection = SourceSelectionExtraction.model_validate(
        {
            "notice_id": "PBLN-prepared",
            "candidate_pack_id": prepared.fact_pack().pack_id,
            "facts": [
                {
                    "fact_id": "amount-1",
                    "field_name": "support_scale",
                    "value_source_block_ids": ["body[0]"],
                    "context_source_block_ids": [],
                    "status": "identified",
                    "recipient_fact_ids": ["recipient-1"],
                },
                {
                    "fact_id": "recipient-1",
                    "field_name": "beneficiary",
                    "value_source_block_ids": ["body[0]"],
                    "status": "identified",
                },
            ],
        }
    )
    cell_evidence = materialize_evidence(cell_selection, prepared.fact_pack())
    assembled = assemble_final_profile(
        {
            "selection": cell_selection.model_dump(mode="json"),
            "materialized_evidence": [
                {
                    "fact_id": row.fact_id,
                    "field_name": row.field_name,
                    "status": row.status,
                    "semantic_role": row.semantic_role,
                    "subject_role": row.subject_role,
                    "source_blocks": row.source_blocks,
                    "context_blocks": row.context_blocks,
                    "organization_names": row.organization_names,
                    "canonical_role": row.canonical_role,
                    "primary_component_id": row.primary_component_id,
                    "applicability_component_ids": row.applicability_component_ids,
                    "modifies_fact_ids": row.modifies_fact_ids,
                    "recipient_fact_ids": row.recipient_fact_ids,
                    "basis_fact_ids": row.basis_fact_ids,
                }
                for row in cell_evidence
            ],
        },
        {"notice_id": "PBLN-prepared", "hwpx": {}},
    )
    assert assembled["comparison_profile"]["support_scale"][0]["recipient_fact_ids"] == ["recipient-1#1"]

    relation_source = {
        "selection": cell_selection.model_dump(mode="json"),
        "materialized_evidence": [
            {
                "fact_id": row.fact_id,
                "field_name": row.field_name,
                "status": row.status,
                "semantic_role": row.semantic_role,
                "subject_role": row.subject_role,
                "source_blocks": row.source_blocks,
                "context_blocks": row.context_blocks,
                "organization_names": row.organization_names,
                "canonical_role": row.canonical_role,
                "primary_component_id": row.primary_component_id,
                "applicability_component_ids": row.applicability_component_ids,
                "modifies_fact_ids": row.modifies_fact_ids,
                "recipient_fact_ids": row.recipient_fact_ids,
                "basis_fact_ids": row.basis_fact_ids,
            }
            for row in cell_evidence
        ],
    }
    # Notice-scoped facts have no component relationship candidates.
    assert build_relationship_payload(relation_source) is None

    invalid_component_reference = SourceSelectionExtraction.model_validate(
        {
            "notice_id": "PBLN-prepared",
            "candidate_pack_id": prepared.fact_pack().pack_id,
            "component_decision": {"mode": "packages"},
            "support_components": [
                {
                    "support_component_id": "package-1",
                    "component_kind": "support_package",
                    "source_block_ids": ["body[999]"],
                }
            ],
        }
    )
    try:
        materialize_evidence(invalid_component_reference, prepared.fact_pack())
    except ValueError as error:
        assert "support component package-1 references blocks outside the candidate pack" in str(error)
    else:
        raise AssertionError("component source ids outside the candidate pack must be rejected")

    try:
        SourceSelectionExtraction.model_validate(
            {
                "notice_id": "PBLN-prepared",
                "candidate_pack_id": prepared.fact_pack().pack_id,
                "facts": [
                    {
                        "fact_id": "role-1",
                        "field_name": "delivery_roles",
                        "source_block_ids": ["body[0]"],
                        "status": "identified",
                        "canonical_role": "operating_agency",
                        "organization_anchors": [
                            {"source_block_id": "body[1]", "anchor_text": "기관"}
                        ],
                        "role_anchor": {"source_block_id": "body[0]", "anchor_text": "운영"},
                    }
                ],
            }
        )
    except ValidationError as error:
        assert "organization/role anchors must reference one of the fact source_block_ids" in str(error)
    else:
        raise AssertionError("organization spans outside a fact's evidence must be rejected")

    invalid_anchor = SourceSelectionExtraction.model_validate(
        {
            "notice_id": "PBLN-prepared",
            "candidate_pack_id": prepared.fact_pack().pack_id,
            "facts": [
                {
                    "fact_id": "role-2",
                    "field_name": "delivery_roles",
                    "source_block_ids": ["body[0]"],
                    "status": "identified",
                        "canonical_role": "operating_agency",
                        "organization_anchors": [
                            {"source_block_id": "body[0]", "anchor_text": "없는 기관"}
                        ],
                        "role_anchor": {"source_block_id": "body[0]", "anchor_text": "운영"},
                }
            ],
        }
    )
    try:
        materialize_evidence(invalid_anchor, prepared.fact_pack())
    except ValueError as error:
        assert "organization anchor must occur exactly once" in str(error)
    else:
        raise AssertionError("organization anchors absent from the source must be rejected")

    openai_schema = json.dumps(_openai_schema())
    assert '"oneOf"' not in openai_schema
    assert '"anyOf"' in openai_schema

    model_input = _model_input(stage_pack())
    assert '"question"' not in model_input
    assert "교육 단계와 최종 지원 단계를 분리한다." not in model_input

    gold_path = Path("semantic_structuring/golden/PBLN_000000000125002.v0.1.json")
    golden = json.loads(gold_path.read_text())
    output_facts = []
    for fact in golden["a_facts"]:
        output = {key: value for key, value in fact.items() if key not in {"gold_id", "normalization_note"}}
        output["fact_id"] = fact["gold_id"]
        output_facts.append(output)
    table_proposals = [
        {key: value for key, value in table.items() if key != "policy"}
        for table in golden["b_table_catalog"]
    ]
    SemanticExtraction.model_validate(
        {
            "notice_id": golden["notice_id"],
            "candidate_pack_id": "gold-contract-check",
            "facts": output_facts,
            "table_catalog": table_proposals,
        }
    )
    assert resolve_table_policy(
        SemanticExtraction.model_validate(
            {
                "notice_id": golden["notice_id"],
                "candidate_pack_id": "table-policy-check",
                "table_catalog": table_proposals,
            }
        ).table_catalog[0]
    ).policy.value == "search_only"

    valid = SemanticExtraction.model_validate(stage_fixture())
    assert validate_extraction(valid, stage_pack()) == []

    invalid_scope = stage_fixture()
    invalid_scope["facts"][0]["scope"] = "교육 참여자"
    try:
        SemanticExtraction.model_validate(invalid_scope)
    except ValidationError:
        pass
    else:
        raise AssertionError("free-form scope must be rejected")

    outside_evidence = stage_fixture()
    outside_evidence["facts"][0]["source_block_ids"] = ["body[49]"]
    issues = validate_extraction(SemanticExtraction.model_validate(outside_evidence), stage_pack())
    assert "source_outside_candidate_pack" in [issue.code for issue in issues]


if __name__ == "__main__":
    main()
