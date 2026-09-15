from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path

import pytest

from scripts import compare_existing_profile_semantics as cli
from worker.evaluation import existing_profile_semantic_diff as semantic_diff
from worker.evaluation.existing_profile_semantic_diff import (
    ExistingProfileSemanticError,
    LoadedSemanticArtifacts,
    SemanticArtifact,
    build_semantic_graph,
    calibrate_loaded_semantic_corpora,
    compare_loaded_semantic_corpora,
    compare_semantic_profiles,
    load_automatic_semantic_artifacts,
    load_reviewed_gold_semantic_artifacts,
    write_semantic_report,
)


BASELINE = Path("/srv/pre-review/imports/bizinfo-existing/structured-profiles-100.zip")
GOLD = Path(
    "/home/paim/Project/llm-prompting-test/imports/prereview-vectordb-poc/"
    "dataset_prep/frozen_existing_profile_gold_100_20260909_v5"
)
SIX = [
    "PBLN_000000000103645",
    "PBLN_000000000112425",
    "PBLN_000000000117175",
    "PBLN_000000000121019",
    "PBLN_000000000121309",
    "PBLN_000000000122023",
]
NOTICE_ID = "PBLN_000000000000001"
DOCUMENT_ID = f"pdf:{NOTICE_ID}"
SOURCE_SHA = "a" * 64
SOURCE_TEXT = "기존 값\n교정 값"


def _fact(*, fact_id: str, value: str, occurrence_id: str) -> dict[str, object]:
    start = SOURCE_TEXT.index(value)
    return {
        "fact_id": fact_id,
        "field_name": "support_content",
        "value_raw": value,
        "model_summary": None,
        "status": "identified",
        "scope": "notice",
        "support_component_id": None,
        "applicability_component_ids": [],
        "modifies_fact_ids": [],
        "recipient_fact_ids": [],
        "basis_fact_ids": [],
        "subject_role": None,
        "semantic_role": None,
        "value_source": {
            "source_block_id": "block-1",
            "start_char": start,
            "end_char": start + len(value),
            "text_basis": "common_ir_v1_candidate_pack",
        },
        "evidence": [
            {
                "source_block_id": "block-1",
                "source_occurrence_ids": [occurrence_id],
                "common_ir_document_id": DOCUMENT_ID,
                "common_ir_block_id": "block-1",
                "common_ir_occurrence_ids": [occurrence_id],
                "section_id": "main_notice",
            }
        ],
        "context_evidence": [],
    }


def _artifact(
    *,
    value: str = "기존 값",
    fact_id: str = "fact-1",
    occurrence_id: str = "occ-old",
) -> SemanticArtifact:
    fact = _fact(fact_id=fact_id, value=value, occurrence_id=occurrence_id)
    lineage = {
        "candidate_pack_id": "pack-1",
        "common_ir_document_id": DOCUMENT_ID,
        "common_ir_source_sha256": SOURCE_SHA,
        "text_basis": "common_ir_v1_candidate_pack",
    }
    profile = {
        "schema_version": "existing_program_profile/v0.2",
        "notice_id": f"bizinfo:{NOTICE_ID}",
        "source_profile_id": DOCUMENT_ID,
        "source_documents": [
            {
                "document_name": "source.pdf",
                "format": "pdf",
                "source_url": None,
                "notice_detail_url": None,
                "common_ir": {
                    "document_id": DOCUMENT_ID,
                    "schema_version": "common_ir_v1",
                    "source_kind": "pdf",
                    "source_sha256": SOURCE_SHA,
                    "source_location": "/host-a/source.pdf",
                    "artifact_role": "production",
                },
            }
        ],
        "comparison_profile": {"support_content": [fact]},
        "support_components": [],
        "derived_projections": [],
        "identity": {
            "title_raw": None,
            "title_source_block_ids": [],
            "notice_date_raw": None,
            "notice_date_source_block_ids": [],
            "source_url": None,
        },
        "table_catalog": [],
        "unresolved_observations": [],
        "unresolved_relations": [],
        "processing_metadata": {"candidate_pack": lineage},
    }
    selection = {
        "selection_contract": "v0.2_anchor",
        "selection": {
            "notice_id": NOTICE_ID,
            "candidate_pack_id": "pack-1",
            "facts": [
                {
                    "fact_id": fact_id,
                    "field_name": "support_content",
                    "status": "identified",
                    "subject_role": None,
                    "semantic_role": None,
                    "value_anchor": {
                        "source_block_id": "block-1",
                        "anchor_text": value,
                    },
                    "context_source_block_ids": [],
                    "organization_anchors": [],
                    "role_anchor": None,
                    "canonical_role": None,
                    "primary_component_id": None,
                    "applicability_component_ids": [],
                    "modifies_fact_ids": [],
                    "recipient_fact_ids": [],
                    "basis_fact_ids": [],
                }
            ],
            "support_components": [],
            "support_facets": [],
            "support_scale_measures": [],
        },
        "source_block_texts": {"block-1": SOURCE_TEXT},
        "common_ir_identity": {
            "document_id": DOCUMENT_ID,
            "source_kind": "pdf",
            "source_sha256": SOURCE_SHA,
        },
        "candidate_pack_lineage": lineage,
        "materialized_evidence": [
            {
                "fact_id": fact_id,
                "field_name": "support_content",
                "status": "identified",
                "subject_role": None,
                "semantic_role": None,
                "source_blocks": [
                    {
                        "source_block_id": "block-1",
                        "text": value,
                        "section_id": "main_notice",
                        "source_occurrence_ids": [occurrence_id],
                        "common_ir_document_id": DOCUMENT_ID,
                        "common_ir_block_id": "block-1",
                        "common_ir_occurrence_ids": [occurrence_id],
                    }
                ],
                "value_source": fact["value_source"],
                "context_blocks": [],
                "organization_names": [],
                "organization_sources": [],
                "role_raw": None,
                "role_source_block_id": None,
                "role_source": None,
                "canonical_role": None,
                "primary_component_id": None,
                "applicability_component_ids": [],
                "modifies_fact_ids": [],
                "recipient_fact_ids": [],
                "basis_fact_ids": [],
            }
        ],
        "materialized_components": [],
        "numeric_candidates": [],
        "support_facets": [],
        "support_scale_measures": [],
    }
    common_ir = {
        "schema_version": "common_ir_v1",
        "document": {
            "document_id": DOCUMENT_ID,
            "source_kind": "pdf",
            "artifact_role": "production",
            "provenance": {
                "source_sha256": SOURCE_SHA,
                "source_location": "/host-a/source.pdf",
            },
        },
        "blocks": [
            {
                "block_id": "block-1",
                "text": SOURCE_TEXT,
                "kind": "paragraph",
                "reading_order": 0,
                "structure_status": "explicit",
                "text_occurrence_ids": [occurrence_id],
                "occurrences": [
                    {"occurrence_id": occurrence_id, "text": SOURCE_TEXT},
                ],
            }
        ],
    }
    return SemanticArtifact(
        notice_id=NOTICE_ID,
        profile=profile,
        source_selection=selection,
        common_ir=common_ir,
    )


def _loaded(artifact: SemanticArtifact, *, role: str) -> LoadedSemanticArtifacts:
    return LoadedSemanticArtifacts(
        artifacts={artifact.notice_id: artifact},
        identity={"role": role, "profile_count": 1},
    )


def _native_composite_artifact() -> SemanticArtifact:
    artifact = _artifact()
    left_id = "block-left"
    right_id = "block-right"
    left_text = "사업 목적 및"
    right_text = "지원 내용"
    composite_text = f"{left_text} {right_text}"
    composite_digest = sha256(f"{left_id}\0{right_id}".encode()).hexdigest()
    composite_id = f"composite:{composite_digest[:20]}"
    artifact.common_ir["blocks"] = [  # type: ignore[index]
        {
            "block_id": left_id,
            "text": left_text,
            "kind": "paragraph",
            "reading_order": 0,
            "structure_status": "explicit",
            "text_occurrence_ids": ["occ-left"],
            "occurrences": [{"occurrence_id": "occ-left", "text": left_text}],
        },
        {
            "block_id": right_id,
            "text": right_text,
            "kind": "paragraph",
            "reading_order": 1,
            "structure_status": "explicit",
            "text_occurrence_ids": ["occ-right"],
            "occurrences": [{"occurrence_id": "occ-right", "text": right_text}],
        },
    ]
    spans = [
        {
            "source_block_id": left_id,
            "exact_text": left_text,
            "start_char": 0,
            "end_char": len(left_text),
            "separator_after": " ",
            "source_order": 0,
            "section_id": "main_notice",
            "common_ir_block_id": left_id,
            "common_ir_occurrence_ids": ["occ-left"],
            "common_ir_cell_id": None,
        },
        {
            "source_block_id": right_id,
            "exact_text": right_text,
            "start_char": 0,
            "end_char": len(right_text),
            "separator_after": "",
            "source_order": 1,
            "section_id": "main_notice",
            "common_ir_block_id": right_id,
            "common_ir_occurrence_ids": ["occ-right"],
            "common_ir_cell_id": None,
        },
    ]
    fact = artifact.profile["comparison_profile"]["support_content"][0]  # type: ignore[index]
    fact.update(
        {
            "value_raw": composite_text,
            "value_source": {
                "source_block_id": composite_id,
                "start_char": 0,
                "end_char": len(composite_text),
                "text_basis": "common_ir_v1_candidate_pack",
            },
            "evidence": [
                {
                    "source_block_id": composite_id,
                    "section_id": "main_notice",
                    "source_occurrence_ids": ["occ-left", "occ-right"],
                    "common_ir_document_id": DOCUMENT_ID,
                    "common_ir_block_id": left_id,
                    "common_ir_occurrence_ids": ["occ-left", "occ-right"],
                    "source_spans": deepcopy(spans),
                }
            ],
        }
    )
    selected = artifact.source_selection["selection"]["facts"][0]  # type: ignore[index]
    selected["value_anchor"] = {
        "source_block_id": composite_id,
        "anchor_text": composite_text,
    }
    artifact.source_selection["source_block_texts"] = {  # type: ignore[index]
        composite_id: composite_text
    }
    materialized = artifact.source_selection["materialized_evidence"][0]  # type: ignore[index]
    materialized["value_source"] = fact["value_source"]
    materialized["source_blocks"] = [
        {
            **deepcopy(fact["evidence"][0]),
            "text": composite_text,
        }
    ]
    return artifact


def _table_cell_artifact() -> SemanticArtifact:
    artifact = _artifact()
    cell_block_id = "block-1#r1c0p0"
    artifact.common_ir["blocks"] = [  # type: ignore[index]
        {
            "block_id": "block-1",
            "text": "다른 값\n기존 값",
            "kind": "table",
            "reading_order": 0,
            "structure_status": "explicit",
            "text_occurrence_ids": ["occ-other", "occ-old"],
            "occurrences": [
                {"occurrence_id": "occ-other", "text": "다른 값"},
                {"occurrence_id": "occ-old", "text": "기존 값"},
            ],
            "cells": [
                {
                    "cell_id": "cell-other",
                    "row_index": 0,
                    "col_index": 0,
                    "text_occurrence_ids": ["occ-other"],
                },
                {
                    "cell_id": "cell-value",
                    "row_index": 1,
                    "col_index": 0,
                    "text_occurrence_ids": ["occ-old"],
                },
            ],
        }
    ]
    artifact.source_selection["source_block_texts"] = {  # type: ignore[index]
        cell_block_id: "기존 값"
    }
    fact = artifact.profile["comparison_profile"]["support_content"][0]  # type: ignore[index]
    fact["value_source"]["source_block_id"] = cell_block_id
    fact["value_source"]["start_char"] = 0
    fact["value_source"]["end_char"] = len("기존 값")
    fact["evidence"][0].update(
        {
            "source_block_id": cell_block_id,
            "common_ir_cell_id": "cell-value",
        }
    )
    selected = artifact.source_selection["selection"]["facts"][0]  # type: ignore[index]
    selected["value_anchor"]["source_block_id"] = cell_block_id
    materialized = artifact.source_selection["materialized_evidence"][0]  # type: ignore[index]
    materialized["value_source"] = fact["value_source"]
    materialized["source_blocks"][0].update(
        {
            "source_block_id": cell_block_id,
            "common_ir_cell_id": "cell-value",
        }
    )
    return artifact


def _artifact_with_support_scale_measure() -> SemanticArtifact:
    """One self-contained profile whose numeric locator is meaningful."""

    artifact = _artifact()
    source_text = "지원 10개사"
    artifact.source_selection["source_block_texts"]["block-1"] = source_text  # type: ignore[index]
    artifact.common_ir["blocks"][0]["text"] = source_text  # type: ignore[index]
    fact = artifact.profile["comparison_profile"].pop("support_content")[0]  # type: ignore[index]
    fact.update(
        {
            "field_name": "support_scale",
            "value_raw": source_text,
            "value_source": {
                "source_block_id": "block-1",
                "start_char": 0,
                "end_char": len(source_text),
                "text_basis": "common_ir_v1_candidate_pack",
            },
        }
    )
    artifact.profile["comparison_profile"]["support_scale"] = [fact]  # type: ignore[index]
    selected = artifact.source_selection["selection"]["facts"][0]  # type: ignore[index]
    selected["field_name"] = "support_scale"
    selected["value_anchor"]["anchor_text"] = source_text
    materialized = artifact.source_selection["materialized_evidence"][0]  # type: ignore[index]
    materialized["field_name"] = "support_scale"
    materialized["value_source"] = fact["value_source"]
    materialized["source_blocks"][0]["text"] = source_text
    measure = {
        "measure_type": "count",
        "measure_role": "selection_capacity",
        "lower_value": 10,
        "upper_value": 10,
        "unit": "개사",
        "comparator": "eq",
        "source_fact_id": fact["fact_id"],
        "source_numeric_candidate_id": "numeric-10",
        "applies_per": None,
        "calculation_basis": None,
        "frequency": None,
        "aggregation_scope": None,
    }
    projection = {
        "projection_type": "support_scale_measures",
        "source_fact_ids": [fact["fact_id"]],
        "measures": [measure],
        "status": "identified",
    }
    artifact.profile["derived_projections"] = [projection]  # type: ignore[index]
    artifact.source_selection["support_scale_measures"] = [deepcopy(projection)]  # type: ignore[index]
    artifact.source_selection["numeric_candidates"] = [  # type: ignore[index]
        {
            "numeric_candidate_id": "numeric-10",
            "source_block_id": "block-1",
            "anchor_text": "10개사",
            "start_char": 3,
            "end_char": 7,
        }
    ]
    return artifact


def _artifact_with_named_component() -> SemanticArtifact:
    artifact = _artifact()
    component_id = "component-1"
    component_name = "지원 패키지"
    source_blocks = {
        "component-block": component_name,
        "unrelated-block": "완전히 다른 문구",
        "ambiguous-block": f"{component_name} / {component_name}",
    }
    for order, (block_id, text) in enumerate(source_blocks.items(), start=1):
        occurrence_id = f"occ-component-{order}"
        artifact.common_ir["blocks"].append(  # type: ignore[index]
            {
                "block_id": block_id,
                "text": text,
                "kind": "paragraph",
                "reading_order": order,
                "structure_status": "explicit",
                "text_occurrence_ids": [occurrence_id],
                "occurrences": [
                    {"occurrence_id": occurrence_id, "text": text},
                ],
            }
        )
        artifact.source_selection["source_block_texts"][block_id] = text  # type: ignore[index]
    artifact.profile["support_components"] = [  # type: ignore[index]
        {
            "support_component_id": component_id,
            "component_kind": "support_package",
            "name_raw": component_name,
            "name_source_block_id": "component-block",
            "name_status": "identified",
            "source_block_ids": ["component-block"],
            "table_block_ids": [],
            "facts": [],
        }
    ]
    artifact.source_selection["selection"]["support_components"] = [  # type: ignore[index]
        {
            "support_component_id": component_id,
            "component_kind": "support_package",
            "name_anchor": {
                "source_block_id": "component-block",
                "anchor_text": component_name,
            },
            "source_block_ids": ["component-block"],
            "table_block_ids": [],
        }
    ]
    artifact.source_selection["materialized_components"] = [  # type: ignore[index]
        {
            "support_component_id": component_id,
            "name_raw": component_name,
            "name_source_block_id": "component-block",
        }
    ]
    return artifact


def _remap_generated_ids(artifact: SemanticArtifact) -> SemanticArtifact:
    """Bijectively rename every generated fact/component handle and reference."""

    changed = deepcopy(artifact)
    profile = changed.profile
    facts = [
        fact
        for rows in profile["comparison_profile"].values()  # type: ignore[union-attr]
        for fact in rows
    ] + [
        fact
        for component in profile["support_components"]  # type: ignore[index]
        for fact in component["facts"]
    ]
    fact_map = {fact["fact_id"]: f"remapped-fact-{index}" for index, fact in enumerate(facts)}
    component_map = {
        component["support_component_id"]: f"remapped-component-{index}"
        for index, component in enumerate(profile["support_components"])  # type: ignore[index]
    }

    def walk(value):
        if isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, dict):
            for key, item in list(value.items()):
                if key in {"fact_id", "source_fact_id"} and item in fact_map:
                    value[key] = fact_map[item]
                elif key in {
                    "source_fact_ids", "positive_source_fact_ids",
                    "exclusion_source_fact_ids", "modifies_fact_ids",
                    "recipient_fact_ids", "basis_fact_ids",
                } and isinstance(item, list):
                    value[key] = [fact_map.get(reference, reference) for reference in item]
                elif key in {"support_component_id", "primary_component_id"} and item in component_map:
                    value[key] = component_map[item]
                elif key == "applicability_component_ids" and isinstance(item, list):
                    value[key] = [component_map.get(reference, reference) for reference in item]
                else:
                    walk(item)

    walk(profile)
    walk(changed.source_selection)
    return changed


@pytest.fixture(scope="module")
def real_corpora() -> tuple[LoadedSemanticArtifacts, LoadedSemanticArtifacts]:
    if not (BASELINE.is_file() and GOLD.is_dir()):
        pytest.skip("external baseline/Gold corpora are not installed")
    return (
        load_automatic_semantic_artifacts(BASELINE),
        load_reviewed_gold_semantic_artifacts(GOLD),
    )


def test_synthetic_candidate_equal_to_gold_passes_exact_gate() -> None:
    baseline = _artifact()
    gold = _artifact(value="교정 값", fact_id="gold-id")
    candidate = deepcopy(gold)

    report = compare_semantic_profiles(baseline, gold, candidate, notice_id=NOTICE_ID)

    assert report["semantic_gate_status"] == "passed"
    assert report["gate"]["candidate_exactly_equals_gold"] is True
    assert report["counts"]["missing_gold"] == 0
    assert report["counts"]["candidate_only"] == 0
    assert report["counts"]["remaining_baseline_surplus"] == 0


def test_candidate_report_declares_source_universe_scope_and_limitations() -> None:
    artifact = _artifact()
    report = compare_loaded_semantic_corpora(
        _loaded(artifact, role="baseline"),
        _loaded(deepcopy(artifact), role="gold"),
        _loaded(deepcopy(artifact), role="candidate"),
    )

    assert report["evaluation_kind"] == "candidate_gold_gate"
    admission = report["normalization"]["candidate_source_admission"]
    assert admission["scope"] == "deterministic_common_ir_source_universe_not_runpod_router_parity"
    assert admission["transform"].startswith("common-ir-v1-source-universe/v1:")
    assert len(admission["limitations"]) == 2


def test_real_baseline_candidate_reproduces_exact_94_unchanged_and_6_corrected(real_corpora) -> None:
    baseline, gold = real_corpora
    report = calibrate_loaded_semantic_corpora(baseline, gold)

    assert report["evaluation_kind"] == "baseline_gold_calibration"
    assert report["normalization"]["candidate_source_admission"] is None
    assert report["counts"] == {"selected": 100, "passed": 94, "failed": 6}
    assert {
        item["notice_id"]
        for item in report["notices"]
        if item["semantic_gate_status"] == "failed"
    } == set(SIX)
    by_id = {item["notice_id"]: item for item in report["notices"]}
    assert any(
        row["kind"] == "relationship"
        for row in by_id["PBLN_000000000117175"]["differences"]["candidate_only"]
    )
    for notice_id in ("PBLN_000000000112425", "PBLN_000000000121309"):
        assert any(
            row["kind"] == "support_component"
            for row in by_id[notice_id]["differences"]["missing_gold"]
        )


def test_id_remap_and_set_like_order_do_not_change_graph() -> None:
    original = _artifact()
    changed = deepcopy(original)
    for artifact in (original, changed):
        block = artifact.common_ir["blocks"][0]  # type: ignore[index]
        block["text_occurrence_ids"] = ["occ-old", "occ-new"]
        block["occurrences"].append(  # type: ignore[index]
            {"occurrence_id": "occ-new", "text": "교정 값"}
        )
    fact = changed.profile["comparison_profile"]["support_content"][0]  # type: ignore[index]
    fact["fact_id"] = "renamed-fact"  # type: ignore[index]
    fact["evidence"][0]["common_ir_occurrence_ids"] = ["occ-new", "occ-old"]  # type: ignore[index]
    fact["evidence"][0]["source_occurrence_ids"] = ["occ-old", "occ-new"]  # type: ignore[index]
    changed.source_selection["selection"]["facts"][0]["fact_id"] = "renamed-fact"  # type: ignore[index]
    changed.source_selection["materialized_evidence"][0]["fact_id"] = "renamed-fact"  # type: ignore[index]
    changed_source = changed.source_selection["materialized_evidence"][0]["source_blocks"][0]  # type: ignore[index]
    changed_source["common_ir_occurrence_ids"] = ["occ-new", "occ-old"]
    changed_source["source_occurrence_ids"] = ["occ-old", "occ-new"]
    changed.profile["processing_metadata"] = {"candidate_pack": changed.profile["processing_metadata"]["candidate_pack"], "run_id": "ignored"}  # type: ignore[index]

    original_fact = original.profile["comparison_profile"]["support_content"][0]  # type: ignore[index]
    original_fact["evidence"][0]["common_ir_occurrence_ids"] = ["occ-old", "occ-new"]  # type: ignore[index]
    original_fact["evidence"][0]["source_occurrence_ids"] = ["occ-old", "occ-new"]  # type: ignore[index]
    original_source = original.source_selection["materialized_evidence"][0]["source_blocks"][0]  # type: ignore[index]
    original_source["common_ir_occurrence_ids"] = ["occ-old", "occ-new"]
    original_source["source_occurrence_ids"] = ["occ-old", "occ-new"]

    assert build_semantic_graph(original).digest == build_semantic_graph(changed).digest


def test_real_component_relation_projection_ids_are_semantic_handles_only(real_corpora) -> None:
    _baseline, gold = real_corpora
    original = gold.artifacts["PBLN_000000000107712"]
    remapped = _remap_generated_ids(original)

    assert build_semantic_graph(original).digest == build_semantic_graph(remapped).digest


def test_component_name_source_must_remain_in_component_sources(real_corpora) -> None:
    _baseline, gold = real_corpora
    original = gold.artifacts["PBLN_000000000112425"]
    changed = deepcopy(original)
    component = changed.profile["support_components"][0]  # type: ignore[index]
    selected = next(
        item
        for item in changed.source_selection["selection"]["support_components"]  # type: ignore[index]
        if item["support_component_id"] == component["support_component_id"]
    )
    excluded = set(component["source_block_ids"])
    replacement = next(
        source_block_id
        for source_block_id in changed.source_selection["source_block_texts"]  # type: ignore[index]
        if source_block_id not in excluded
    )
    component["source_block_ids"] = [replacement]
    selected["source_block_ids"] = [replacement]

    build_semantic_graph(original)
    with pytest.raises(ExistingProfileSemanticError, match="outside its component sources"):
        build_semantic_graph(changed)


@pytest.mark.parametrize(
    ("name_source_block_id", "source_block_ids", "message"),
    [
        ("unrelated-block", ["unrelated-block"], "occur exactly once"),
        ("ambiguous-block", ["ambiguous-block"], "occur exactly once"),
        ("unrelated-block", ["component-block"], "outside its component sources"),
        ("occ-component-1", ["occ-component-1"], "absent from CandidatePack"),
    ],
)
def test_component_name_anchor_requires_unique_admitted_member_source(
    name_source_block_id: str,
    source_block_ids: list[str],
    message: str,
) -> None:
    artifact = _artifact_with_named_component()
    component = artifact.profile["support_components"][0]  # type: ignore[index]
    component["name_source_block_id"] = name_source_block_id
    component["source_block_ids"] = source_block_ids
    selected = artifact.source_selection["selection"]["support_components"][0]  # type: ignore[index]
    selected["name_anchor"]["source_block_id"] = name_source_block_id
    selected["source_block_ids"] = source_block_ids
    materialized = artifact.source_selection["materialized_components"][0]  # type: ignore[index]
    materialized["name_source_block_id"] = name_source_block_id

    with pytest.raises(ExistingProfileSemanticError, match=message):
        build_semantic_graph(artifact)


def test_nested_projection_set_order_is_not_semantic(real_corpora) -> None:
    _baseline, gold = real_corpora
    original = gold.artifacts["PBLN_000000000117009"]
    changed = deepcopy(original)
    profile_projection = next(
        item
        for item in changed.profile["derived_projections"]  # type: ignore[index]
        if item["projection_type"] == "support_facets"
        and len(item["source_fact_ids"]) > 1
        and len(item["activities"]) > 1
    )
    selected_projection = next(
        item
        for item in changed.source_selection["support_facets"]  # type: ignore[index]
        if item["projection_type"] == "support_facets"
        and set(item["source_fact_ids"]) == set(profile_projection["source_fact_ids"])
    )
    for projection in (profile_projection, selected_projection):
        projection["source_fact_ids"].reverse()
        projection["activities"].reverse()

    assert build_semantic_graph(original).digest == build_semantic_graph(changed).digest


def test_measure_locator_id_is_validation_only_but_value_must_match_source(real_corpora) -> None:
    _baseline, gold = real_corpora
    original = gold.artifacts["PBLN_000000000107712"]
    renamed = deepcopy(original)
    projection = next(
        item
        for item in renamed.profile["derived_projections"]  # type: ignore[index]
        if item["projection_type"] == "support_scale_measures"
    )
    measure = projection["measures"][0]
    old_id = measure["source_numeric_candidate_id"]
    new_id = f"renamed::{old_id}"
    for candidate in renamed.source_selection["numeric_candidates"]:  # type: ignore[index]
        if candidate["numeric_candidate_id"] == old_id:
            candidate["numeric_candidate_id"] = new_id
    for container in (
        renamed.profile["derived_projections"],  # type: ignore[index]
        renamed.source_selection["support_scale_measures"],  # type: ignore[index]
    ):
        for item in container:
            for row in item.get("measures", []):
                if row.get("source_numeric_candidate_id") == old_id:
                    row["source_numeric_candidate_id"] = new_id

    assert build_semantic_graph(original).digest == build_semantic_graph(renamed).digest

    changed_value = deepcopy(original)
    changed_projection = next(
        item
        for item in changed_value.profile["derived_projections"]  # type: ignore[index]
        if item["projection_type"] == "support_scale_measures"
    )
    changed_measure = changed_projection["measures"][0]
    changed_measure["lower_value"] = int(getattr(changed_measure["lower_value"], "lexeme", changed_measure["lower_value"])) + 1
    changed_measure["upper_value"] = int(getattr(changed_measure["upper_value"], "lexeme", changed_measure["upper_value"])) + 1
    selected_measure = changed_value.source_selection["support_scale_measures"][0]["measures"][0]  # type: ignore[index]
    selected_measure["lower_value"] = int(getattr(selected_measure["lower_value"], "lexeme", selected_measure["lower_value"])) + 1
    selected_measure["upper_value"] = int(getattr(selected_measure["upper_value"], "lexeme", selected_measure["upper_value"])) + 1
    with pytest.raises(ExistingProfileSemanticError, match="numeric locator"):
        compare_semantic_profiles(
            original,
            original,
            changed_value,
            notice_id=original.notice_id,
        )


def test_measure_locator_must_belong_to_its_source_fact(real_corpora) -> None:
    _baseline, gold = real_corpora
    artifact = deepcopy(gold.artifacts["PBLN_000000000107712"])
    projection = next(
        item
        for item in artifact.profile["derived_projections"]  # type: ignore[index]
        if item["projection_type"] == "support_scale_measures"
    )
    measure = projection["measures"][0]
    source_fact = next(
        fact
        for fact in artifact.profile["comparison_profile"]["support_scale"]  # type: ignore[index]
        if fact["fact_id"] == measure["source_fact_id"]
    )
    foreign = next(
        candidate
        for candidate in artifact.source_selection["numeric_candidates"]  # type: ignore[index]
        if candidate["source_block_id"]
        != source_fact["value_source"]["source_block_id"]
    )
    measure["source_numeric_candidate_id"] = foreign["numeric_candidate_id"]
    artifact.source_selection["support_scale_measures"][0]["measures"][0]["source_numeric_candidate_id"] = foreign["numeric_candidate_id"]  # type: ignore[index]

    with pytest.raises(ExistingProfileSemanticError, match="outside its source fact span"):
        build_semantic_graph(artifact)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda artifact: artifact.profile.__setitem__("schema_version", "invented/v999"), "schema"),
        (lambda artifact: artifact.profile.__setitem__("notice_id", "bizinfo:PBLN_000000000000999"), "notice"),
    ],
)
def test_invalid_root_identity_is_rejected(mutation, message: str) -> None:
    artifact = _artifact()
    mutation(artifact)

    with pytest.raises(ExistingProfileSemanticError, match=message):
        build_semantic_graph(artifact)


def test_exact_span_and_evidence_block_membership_are_rejected() -> None:
    span_changed = _artifact()
    fact = span_changed.profile["comparison_profile"]["support_content"][0]  # type: ignore[index]
    fact["value_source"]["end_char"] -= 1  # type: ignore[index,operator]
    span_changed.source_selection["materialized_evidence"][0]["value_source"] = fact["value_source"]  # type: ignore[index]
    with pytest.raises(ExistingProfileSemanticError, match="exactly match|materialize"):
        build_semantic_graph(span_changed)

    wrong_block = _artifact()
    wrong_block.common_ir["blocks"].append(  # type: ignore[union-attr]
        {
            "block_id": "block-2",
            "text": "다른 근거",
            "kind": "paragraph",
            "reading_order": 1,
            "structure_status": "explicit",
            "text_occurrence_ids": ["occ-other"],
            "occurrences": [{"occurrence_id": "occ-other", "text": "다른 근거"}],
        }
    )
    evidence = wrong_block.profile["comparison_profile"]["support_content"][0]["evidence"][0]  # type: ignore[index]
    evidence["common_ir_block_id"] = "block-2"  # type: ignore[index]
    with pytest.raises(ExistingProfileSemanticError, match="occurrence"):
        build_semantic_graph(wrong_block)


def test_selection_materialization_must_match_profile_semantics() -> None:
    artifact = _artifact()
    artifact.source_selection["materialized_evidence"][0]["semantic_role"] = "forged-role"  # type: ignore[index]

    with pytest.raises(ExistingProfileSemanticError, match="selection/materialized evidence"):
        build_semantic_graph(artifact)


def test_unresolved_review_reason_is_semantic() -> None:
    original = _artifact()
    original.profile["unresolved_observations"] = [  # type: ignore[index]
        {
            "kind": "normalization",
            "status": "needs_review",
            "value_raw": "검토 대상",
            "reason": "원문 근거가 충돌함",
        }
    ]
    changed = deepcopy(original)
    changed.profile["unresolved_observations"][0]["reason"] = "다른 검토 사유"  # type: ignore[index]

    assert build_semantic_graph(original).digest != build_semantic_graph(changed).digest


def test_source_location_is_not_semantic() -> None:
    baseline = _artifact()
    gold = deepcopy(baseline)
    candidate = deepcopy(gold)
    candidate.common_ir["document"]["provenance"]["source_location"] = "/host-b/source.pdf"  # type: ignore[index]
    candidate.profile["source_documents"][0]["common_ir"]["source_location"] = "/host-b/source.pdf"  # type: ignore[index]

    report = compare_semantic_profiles(baseline, gold, candidate, notice_id=NOTICE_ID)
    assert report["semantic_gate_status"] == "passed"


def test_candidate_source_block_texts_cannot_be_forged_by_offset_relocation() -> None:
    baseline = _artifact()
    gold = deepcopy(baseline)
    candidate = deepcopy(gold)
    prefix = "위조된 CandidatePack: "
    candidate.source_selection["source_block_texts"]["block-1"] = prefix + SOURCE_TEXT  # type: ignore[index]
    fact = candidate.profile["comparison_profile"]["support_content"][0]  # type: ignore[index]
    fact["value_source"]["start_char"] += len(prefix)  # type: ignore[index,operator]
    fact["value_source"]["end_char"] += len(prefix)  # type: ignore[index,operator]
    candidate.source_selection["materialized_evidence"][0]["value_source"] = fact["value_source"]  # type: ignore[index]

    with pytest.raises(ExistingProfileSemanticError):
        compare_semantic_profiles(baseline, gold, candidate, notice_id=NOTICE_ID)


def test_candidate_cannot_reuse_a_baseline_only_legacy_block() -> None:
    baseline = _artifact()
    baseline.source_selection["source_block_texts"]["adj:baseline-only"] = SOURCE_TEXT  # type: ignore[index]
    gold = _artifact()
    candidate = deepcopy(baseline)

    with pytest.raises(ExistingProfileSemanticError, match="not exactly reproducible"):
        compare_semantic_profiles(baseline, gold, candidate, notice_id=NOTICE_ID)


def test_candidate_can_use_a_server_regenerated_native_line_atom() -> None:
    baseline = _artifact(value="기존 값", occurrence_id="occ-old")
    gold = deepcopy(baseline)
    candidate = deepcopy(gold)
    line_id = f"line:{sha256(b'block-1\0' + b'0\0' + str(len('기존 값')).encode()).hexdigest()[:20]}"
    candidate.source_selection["source_block_texts"] = {line_id: "기존 값"}  # type: ignore[index]
    fact = candidate.profile["comparison_profile"]["support_content"][0]  # type: ignore[index]
    fact["value_source"] = {
        "source_block_id": line_id,
        "start_char": 0,
        "end_char": len("기존 값"),
        "text_basis": "common_ir_v1_candidate_pack",
    }
    fact["evidence"][0]["source_block_id"] = line_id  # type: ignore[index]
    selected = candidate.source_selection["selection"]["facts"][0]  # type: ignore[index]
    selected["value_anchor"]["source_block_id"] = line_id
    materialized = candidate.source_selection["materialized_evidence"][0]  # type: ignore[index]
    materialized["value_source"] = fact["value_source"]
    materialized["source_blocks"][0]["source_block_id"] = line_id
    materialized["source_blocks"][0]["native_parent_span"] = {
        "source_block_id": "block-1",
        "start_char": 0,
        "end_char": len("기존 값"),
        "exact_text": "기존 값",
    }

    report = compare_semantic_profiles(baseline, gold, candidate, notice_id=NOTICE_ID)
    assert report["semantic_gate_status"] == "passed"


def test_candidate_can_use_a_server_regenerated_native_composite() -> None:
    baseline = _native_composite_artifact()
    gold = deepcopy(baseline)
    candidate = deepcopy(gold)

    report = compare_semantic_profiles(baseline, gold, candidate, notice_id=NOTICE_ID)

    assert report["semantic_gate_status"] == "passed"


def test_candidate_can_use_a_server_regenerated_pdf_table_occurrence() -> None:
    baseline = _artifact()
    baseline.common_ir["blocks"][0].update(  # type: ignore[index]
        {
            "kind": "table_candidate",
            "occurrences": [
                {
                    "occurrence_id": "occ-old",
                    "text": "기존 값",
                    "provenance": {"method": "pdf_inspector"},
                }
            ],
        }
    )
    gold = deepcopy(baseline)
    candidate = deepcopy(gold)
    native_id = "block-1#native:0000"
    candidate.source_selection["source_block_texts"] = {native_id: "기존 값"}  # type: ignore[index]
    fact = candidate.profile["comparison_profile"]["support_content"][0]  # type: ignore[index]
    fact["value_source"].update(  # type: ignore[index]
        {"source_block_id": native_id, "start_char": 0, "end_char": len("기존 값")}
    )
    fact["evidence"][0]["source_block_id"] = native_id  # type: ignore[index]
    selected = candidate.source_selection["selection"]["facts"][0]  # type: ignore[index]
    selected["value_anchor"]["source_block_id"] = native_id
    materialized = candidate.source_selection["materialized_evidence"][0]  # type: ignore[index]
    materialized["value_source"] = fact["value_source"]
    materialized["source_blocks"][0]["source_block_id"] = native_id

    report = compare_semantic_profiles(baseline, gold, candidate, notice_id=NOTICE_ID)

    assert report["semantic_gate_status"] == "passed"


def test_candidate_composite_span_tampering_fails_closed() -> None:
    baseline = _native_composite_artifact()
    gold = deepcopy(baseline)
    candidate = deepcopy(gold)
    materialized = candidate.source_selection["materialized_evidence"][0]  # type: ignore[index]
    materialized["source_blocks"][0]["source_spans"][0]["separator_after"] = "\n"
    candidate.profile["comparison_profile"]["support_content"][0]["evidence"][0][  # type: ignore[index]
        "source_spans"
    ][0]["separator_after"] = "\n"

    with pytest.raises(ExistingProfileSemanticError, match="source_spans"):
        compare_semantic_profiles(baseline, gold, candidate, notice_id=NOTICE_ID)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("text", "forged evidence text"),
        ("common_ir_cell_id", "forged-cell"),
    ],
)
def test_evidence_unbound_coordinate_or_text_fields_fail_closed(field: str, value: str) -> None:
    artifact = _artifact()
    evidence = artifact.profile["comparison_profile"]["support_content"][0]["evidence"][0]  # type: ignore[index]
    evidence[field] = value

    with pytest.raises(ExistingProfileSemanticError):
        build_semantic_graph(artifact)


def test_evidence_grouping_section_is_validation_only() -> None:
    original = _artifact()
    changed = deepcopy(original)
    evidence = changed.profile["comparison_profile"]["support_content"][0]["evidence"][0]  # type: ignore[index]
    evidence["section_id"] = "another-reviewed-section"
    changed.source_selection["materialized_evidence"][0]["source_blocks"][0]["section_id"] = "another-reviewed-section"  # type: ignore[index]

    assert build_semantic_graph(original).digest == build_semantic_graph(changed).digest


def test_candidate_section_locator_must_match_trusted_source() -> None:
    baseline = _artifact()
    gold = deepcopy(baseline)
    candidate = deepcopy(gold)
    candidate.profile["comparison_profile"]["support_content"][0]["evidence"][0][  # type: ignore[index]
        "section_id"
    ] = "forged-section"
    candidate.source_selection["materialized_evidence"][0]["source_blocks"][0][  # type: ignore[index]
        "section_id"
    ] = "forged-section"

    with pytest.raises(ExistingProfileSemanticError, match="section_id"):
        compare_semantic_profiles(baseline, gold, candidate, notice_id=NOTICE_ID)


def test_evidence_occurrence_is_semantic_provenance() -> None:
    original = _artifact(occurrence_id="occ-old")
    changed = _artifact(occurrence_id="occ-new")

    assert build_semantic_graph(original).digest != build_semantic_graph(changed).digest


def test_legacy_missing_cell_locator_requires_its_native_occurrence() -> None:
    legacy = _table_cell_artifact()
    fact_evidence = legacy.profile["comparison_profile"]["support_content"][0]["evidence"][0]  # type: ignore[index]
    materialized = legacy.source_selection["materialized_evidence"][0]["source_blocks"][0]  # type: ignore[index]
    for evidence in (fact_evidence, materialized):
        evidence.pop("common_ir_cell_id")
        evidence["source_occurrence_ids"] = ["occ-other", "occ-old"]
        evidence["common_ir_occurrence_ids"] = ["occ-other", "occ-old"]

    build_semantic_graph(legacy)

    forged = deepcopy(legacy)
    forged_evidence = forged.profile["comparison_profile"]["support_content"][0]["evidence"][0]  # type: ignore[index]
    forged_materialized = forged.source_selection["materialized_evidence"][0]["source_blocks"][0]  # type: ignore[index]
    for evidence in (forged_evidence, forged_materialized):
        evidence["source_occurrence_ids"] = ["occ-other"]
        evidence["common_ir_occurrence_ids"] = ["occ-other"]

    with pytest.raises(ExistingProfileSemanticError, match="omits its native occurrence"):
        build_semantic_graph(forged)


def test_supplied_table_cell_locator_cannot_be_swapped_or_broadened() -> None:
    swapped = _table_cell_artifact()
    fact_evidence = swapped.profile["comparison_profile"]["support_content"][0]["evidence"][0]  # type: ignore[index]
    materialized = swapped.source_selection["materialized_evidence"][0]["source_blocks"][0]  # type: ignore[index]
    for evidence in (fact_evidence, materialized):
        evidence["common_ir_cell_id"] = "cell-other"
        evidence["source_occurrence_ids"] = ["occ-other"]
        evidence["common_ir_occurrence_ids"] = ["occ-other"]
    with pytest.raises(ExistingProfileSemanticError, match="does not match"):
        build_semantic_graph(swapped)

    broadened = _table_cell_artifact()
    fact_evidence = broadened.profile["comparison_profile"]["support_content"][0]["evidence"][0]  # type: ignore[index]
    materialized = broadened.source_selection["materialized_evidence"][0]["source_blocks"][0]  # type: ignore[index]
    for evidence in (fact_evidence, materialized):
        evidence["source_occurrence_ids"] = ["occ-other", "occ-old"]
        evidence["common_ir_occurrence_ids"] = ["occ-other", "occ-old"]
    with pytest.raises(ExistingProfileSemanticError, match="do not belong"):
        build_semantic_graph(broadened)


def test_gold_only_grouping_block_and_section_normalize_to_raw_occurrence() -> None:
    original = _artifact(value="기존 값", occurrence_id="occ-old")
    regrouped = deepcopy(original)
    regrouped.common_ir["blocks"].append(  # type: ignore[union-attr]
        {
            "block_id": "adj:gold-only",
            "text": "기존 값",
            "kind": "adjudicated_native_span_composition",
            "reading_order": 1,
            "structure_status": "explicit",
            "text_occurrence_ids": ["occ-old"],
            "occurrences": [],
        }
    )
    regrouped.source_selection["source_block_texts"]["adj:gold-only"] = "기존 값"  # type: ignore[index]
    fact = regrouped.profile["comparison_profile"]["support_content"][0]  # type: ignore[index]
    fact["value_source"] = {
        "source_block_id": "adj:gold-only",
        "start_char": 0,
        "end_char": len("기존 값"),
        "text_basis": "common_ir_v1_candidate_pack",
    }
    fact["evidence"][0].update(  # type: ignore[index]
        {
            "source_block_id": "adj:gold-only",
            "common_ir_block_id": "adj:gold-only",
            "section_id": "gold_adjudication.retrieval_discovered",
        }
    )
    selected = regrouped.source_selection["selection"]["facts"][0]  # type: ignore[index]
    selected["value_anchor"]["source_block_id"] = "adj:gold-only"
    materialized = regrouped.source_selection["materialized_evidence"][0]  # type: ignore[index]
    materialized["value_source"] = fact["value_source"]
    materialized["source_blocks"][0].update(
        {
            "source_block_id": "adj:gold-only",
            "common_ir_block_id": "adj:gold-only",
            "section_id": "gold_adjudication.retrieval_discovered",
        }
    )

    assert build_semantic_graph(original).digest == build_semantic_graph(regrouped).digest


def test_non_numeric_measure_locator_is_rejected() -> None:
    artifact = _artifact_with_support_scale_measure()
    numeric = artifact.source_selection["numeric_candidates"][0]  # type: ignore[index]
    numeric.update({"anchor_text": "지", "start_char": 0, "end_char": 1})

    with pytest.raises(ExistingProfileSemanticError):
        build_semantic_graph(artifact)


def test_measure_must_match_its_numeric_locator() -> None:
    artifact = _artifact_with_support_scale_measure()
    numeric = artifact.source_selection["numeric_candidates"][0]  # type: ignore[index]
    numeric.update({"anchor_text": "0", "start_char": 4, "end_char": 5})

    with pytest.raises(ExistingProfileSemanticError):
        build_semantic_graph(artifact)


@pytest.mark.parametrize(
    ("source_text", "anchor_text", "measure_type", "measure_role", "value", "unit"),
    [
        ("지원 10개사 추가 110개사", "10개사", "count", "selection_capacity", 10, "개사"),
        ("지원 10억원 추가 110억원", "10억원", "amount", "support_amount", 1_000_000_000, "KRW"),
        ("지원 10% 추가 110%", "10%", "rate", "support_rate", 1_000, "BPS"),
    ],
)
def test_numeric_locator_cannot_point_into_a_larger_number(
    source_text: str,
    anchor_text: str,
    measure_type: str,
    measure_role: str,
    value: int,
    unit: str,
) -> None:
    artifact = _artifact_with_support_scale_measure()
    fact = artifact.profile["comparison_profile"]["support_scale"][0]  # type: ignore[index]
    value_source = {
        "source_block_id": "block-1",
        "start_char": 0,
        "end_char": len(source_text),
        "text_basis": "common_ir_v1_candidate_pack",
    }
    fact["value_raw"] = source_text
    fact["value_source"] = value_source
    selected = artifact.source_selection["selection"]["facts"][0]  # type: ignore[index]
    selected["value_anchor"]["anchor_text"] = source_text
    materialized = artifact.source_selection["materialized_evidence"][0]  # type: ignore[index]
    materialized["value_source"] = value_source
    materialized["source_blocks"][0]["text"] = source_text
    artifact.source_selection["source_block_texts"]["block-1"] = source_text  # type: ignore[index]
    block = artifact.common_ir["blocks"][0]  # type: ignore[index]
    block["text"] = source_text
    block["occurrences"][0]["text"] = source_text

    start = source_text.index(anchor_text)
    numeric = artifact.source_selection["numeric_candidates"][0]  # type: ignore[index]
    numeric.update(
        {
            "anchor_text": anchor_text,
            "start_char": start,
            "end_char": start + len(anchor_text),
        }
    )
    for projection in (
        artifact.profile["derived_projections"][0],  # type: ignore[index]
        artifact.source_selection["support_scale_measures"][0],  # type: ignore[index]
    ):
        measure = projection["measures"][0]
        measure.update(
            {
                "measure_type": measure_type,
                "measure_role": measure_role,
                "lower_value": value,
                "upper_value": value,
                "unit": unit,
            }
        )

    baseline = artifact
    gold = deepcopy(baseline)
    candidate = deepcopy(baseline)
    forged_start = source_text.rindex(anchor_text)
    candidate.source_selection["numeric_candidates"][0].update(  # type: ignore[index]
        {
            "start_char": forged_start,
            "end_char": forged_start + len(anchor_text),
        }
    )

    with pytest.raises(ExistingProfileSemanticError, match="complete numeric token"):
        compare_semantic_profiles(baseline, gold, candidate, notice_id=NOTICE_ID)


def test_candidate_numeric_spans_are_enumerated_once_per_source_block(monkeypatch) -> None:
    baseline = _artifact_with_support_scale_measure()
    gold = deepcopy(baseline)
    candidate = deepcopy(baseline)
    template = candidate.source_selection["numeric_candidates"][0]  # type: ignore[index]
    candidate.source_selection["numeric_candidates"] = [  # type: ignore[index]
        {**deepcopy(template), "numeric_candidate_id": f"numeric-{index}"}
        for index in range(100)
    ]
    candidate.profile["derived_projections"][0]["measures"][0][  # type: ignore[index]
        "source_numeric_candidate_id"
    ] = "numeric-0"
    candidate.source_selection["support_scale_measures"][0]["measures"][0][  # type: ignore[index]
        "source_numeric_candidate_id"
    ] = "numeric-0"
    calls = 0
    original = semantic_diff._server_enumerated_numeric_spans

    def counted(source_text: str):
        nonlocal calls
        calls += 1
        return original(source_text)

    monkeypatch.setattr(semantic_diff, "_server_enumerated_numeric_spans", counted)

    report = compare_semantic_profiles(baseline, gold, candidate, notice_id=NOTICE_ID)

    assert report["semantic_gate_status"] == "passed"
    assert calls == 1


def test_candidate_common_ir_must_be_bound_to_baseline_input() -> None:
    baseline = _artifact()
    gold = deepcopy(baseline)
    candidate = deepcopy(gold)
    candidate.common_ir["blocks"][0]["text"] = "조작된 Common IR"  # type: ignore[index]

    with pytest.raises(ExistingProfileSemanticError, match="pinned baseline Common IR"):
        compare_semantic_profiles(baseline, gold, candidate, notice_id=NOTICE_ID)


def test_ordered_common_ir_coordinate_tuple_cannot_be_permuted() -> None:
    baseline = _artifact()
    baseline.common_ir["blocks"][0]["occurrences"] = [  # type: ignore[index]
        {"occurrence_id": "occ-old", "bbox": [0, 0, 10, 20]}
    ]
    gold = deepcopy(baseline)
    candidate = deepcopy(gold)
    candidate.common_ir["blocks"][0]["occurrences"][0]["bbox"] = [0, 0, 20, 10]  # type: ignore[index]

    with pytest.raises(ExistingProfileSemanticError, match="pinned baseline Common IR"):
        compare_semantic_profiles(baseline, gold, candidate, notice_id=NOTICE_ID)


def test_duplicate_relation_and_unknown_field_fail_closed() -> None:
    duplicate = _artifact()
    fact = duplicate.profile["comparison_profile"]["support_content"][0]  # type: ignore[index]
    fact["modifies_fact_ids"] = ["fact-1", "fact-1"]  # type: ignore[index]
    duplicate.source_selection["selection"]["facts"][0]["modifies_fact_ids"] = ["fact-1", "fact-1"]  # type: ignore[index]
    duplicate.source_selection["materialized_evidence"][0]["modifies_fact_ids"] = ["fact-1", "fact-1"]  # type: ignore[index]
    with pytest.raises(ExistingProfileSemanticError, match="duplicate"):
        build_semantic_graph(duplicate)

    unknown = _artifact()
    unknown.profile["comparison_profile"]["support_contnet"] = []  # type: ignore[index]
    with pytest.raises(ExistingProfileSemanticError, match="unsupported fields"):
        build_semantic_graph(unknown)


def test_ambiguous_duplicate_semantic_fact_nodes_fail_closed() -> None:
    artifact = _artifact()
    repeated_text = "기존 값 기존 값"
    artifact.source_selection["source_block_texts"]["block-1"] = repeated_text  # type: ignore[index]
    artifact.common_ir["blocks"][0]["text"] = repeated_text  # type: ignore[index]
    first = artifact.profile["comparison_profile"]["support_content"][0]  # type: ignore[index]
    second = deepcopy(first)
    second["fact_id"] = "fact-2"
    second["value_source"] = {
        "source_block_id": "block-1",
        "start_char": 5,
        "end_char": 9,
        "text_basis": "common_ir_v1_candidate_pack",
    }
    artifact.profile["comparison_profile"]["support_content"].append(second)  # type: ignore[index]
    selected_second = deepcopy(artifact.source_selection["selection"]["facts"][0])  # type: ignore[index]
    selected_second["fact_id"] = "fact-2"
    artifact.source_selection["selection"]["facts"].append(selected_second)  # type: ignore[index]
    materialized_second = deepcopy(artifact.source_selection["materialized_evidence"][0])  # type: ignore[index]
    materialized_second["fact_id"] = "fact-2"
    materialized_second["value_source"] = second["value_source"]
    artifact.source_selection["materialized_evidence"].append(materialized_second)  # type: ignore[union-attr]

    with pytest.raises(ExistingProfileSemanticError, match="ambiguous duplicate semantic facts"):
        build_semantic_graph(artifact)


def test_full_corpus_requires_exact_notice_sets() -> None:
    artifact = _artifact()
    baseline = _loaded(artifact, role="baseline")
    gold = _loaded(deepcopy(artifact), role="gold")
    candidate = LoadedSemanticArtifacts(artifacts={}, identity={"role": "candidate", "profile_count": 0})

    with pytest.raises(ExistingProfileSemanticError, match="exact.*notice-id sets"):
        compare_loaded_semantic_corpora(baseline, gold, candidate)


def test_separately_loaded_baseline_shape_is_not_an_implicit_calibration() -> None:
    baseline_artifact = _artifact()
    baseline = _loaded(baseline_artifact, role="baseline")
    gold = _loaded(deepcopy(baseline_artifact), role="gold")
    candidate = _loaded(deepcopy(baseline_artifact), role="candidate")
    candidate.artifacts[NOTICE_ID].source_selection["source_block_texts"][  # type: ignore[index]
        "adj:baseline-only"
    ] = SOURCE_TEXT

    with pytest.raises(ExistingProfileSemanticError, match="not exactly reproducible"):
        compare_loaded_semantic_corpora(baseline, gold, candidate)


def test_report_is_source_free_no_clobber_and_outside_gold(tmp_path: Path) -> None:
    artifact = _artifact()
    baseline = _loaded(artifact, role="baseline")
    gold_artifacts = _loaded(deepcopy(artifact), role="gold")
    candidate = _loaded(deepcopy(artifact), role="candidate")
    baseline.identity.update(  # type: ignore[union-attr]
        {"archive_name": "기밀 공고 원문.zip", "archive_sha256": "b" * 64}
    )
    gold_artifacts.identity.update(  # type: ignore[union-attr]
        {"dataset_version": "기밀 Gold 이름", "freeze_manifest_sha256": "c" * 64}
    )
    candidate.identity.update(  # type: ignore[union-attr]
        {"archive_name": "후보 원문.zip", "archive_sha256": "d" * 64}
    )
    report = compare_loaded_semantic_corpora(
        baseline,
        gold_artifacts,
        candidate,
    )
    encoded = json.dumps(report, ensure_ascii=False)
    assert "기존 값" not in encoded
    assert "기밀 공고 원문.zip" not in encoded
    assert "기밀 Gold 이름" not in encoded
    assert "후보 원문.zip" not in encoded
    assert report["baseline"]["archive_sha256"] == "b" * 64
    assert report["gold"]["freeze_manifest_sha256"] == "c" * 64
    assert report["candidate"]["archive_sha256"] == "d" * 64
    output = tmp_path / "output"
    gold = tmp_path / "gold"
    output.mkdir()
    gold.mkdir()
    written = write_semantic_report(report, output_dir=output, gold_root=gold)
    assert written.is_file()
    with pytest.raises(ExistingProfileSemanticError, match="already exists"):
        write_semantic_report(report, output_dir=output, gold_root=gold)
    with pytest.raises(ExistingProfileSemanticError, match="outside"):
        write_semantic_report(report, output_dir=gold, gold_root=gold)


def test_cli_returns_nonzero_when_semantic_gate_fails(monkeypatch, tmp_path: Path, capsys) -> None:
    monkeypatch.setattr(
        cli,
        "compare_semantic_profile_corpora",
        lambda *args, **kwargs: {"counts": {"selected": 1, "passed": 0, "failed": 1}},
    )
    monkeypatch.setattr(cli, "write_semantic_report", lambda *args, **kwargs: tmp_path / "report.json")

    exit_code = cli.main(
        [
            "--baseline-zip", "baseline.zip",
            "--gold-root", "gold",
            "--candidate-zip", "candidate.zip",
            "--output-dir", str(tmp_path),
        ]
    )

    assert exit_code == 2
    assert json.loads(capsys.readouterr().out)["status"] == "failed"


def test_cli_returns_zero_and_forwards_selection_options_when_gate_passes(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    captured: dict[str, object] = {}

    def compare(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return {"counts": {"selected": 2, "passed": 2, "failed": 0}}

    monkeypatch.setattr(cli, "compare_semantic_profile_corpora", compare)
    monkeypatch.setattr(
        cli,
        "write_semantic_report",
        lambda *args, **kwargs: tmp_path / "existing-profile-semantic-bgc.v1.json",
    )

    exit_code = cli.main(
        [
            "--baseline-zip", "baseline.zip",
            "--gold-root", "gold",
            "--candidate-zip", "candidate.zip",
            "--output-dir", str(tmp_path),
            "--expected-reference-count", "100",
            "--expected-candidate-count", "6",
            "--notice-id", "PBLN_000000000103645",
            "--notice-id", "PBLN_000000000112425",
        ]
    )

    assert exit_code == 0
    assert captured["kwargs"] == {
        "expected_reference_count": 100,
        "expected_candidate_count": 6,
        "notice_ids": [
            "PBLN_000000000103645",
            "PBLN_000000000112425",
        ],
    }
    output = json.loads(capsys.readouterr().out)
    assert output == {
        "failed": 0,
        "passed": 2,
        "report_file": "existing-profile-semantic-bgc.v1.json",
        "selected": 2,
        "status": "passed",
    }


def test_cli_uses_explicit_baseline_calibration_mode(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    captured: dict[str, object] = {}

    def calibrate(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return {"counts": {"selected": 100, "passed": 94, "failed": 6}}

    monkeypatch.setattr(cli, "calibrate_semantic_profile_corpora", calibrate)
    monkeypatch.setattr(
        cli,
        "write_semantic_report",
        lambda *args, **kwargs: tmp_path / "existing-profile-semantic-bgc.v1.json",
    )

    exit_code = cli.main(
        [
            "--baseline-zip", "baseline.zip",
            "--gold-root", "gold",
            "--calibrate-baseline",
            "--output-dir", str(tmp_path),
            "--expected-reference-count", "100",
        ]
    )

    assert exit_code == 2
    assert captured["kwargs"] == {
        "expected_reference_count": 100,
        "notice_ids": None,
    }
    assert json.loads(capsys.readouterr().out)["status"] == "failed"
