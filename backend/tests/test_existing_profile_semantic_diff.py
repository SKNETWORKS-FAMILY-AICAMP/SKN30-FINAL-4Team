from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Mapping

import pytest

from scripts import compare_existing_profile_semantics as cli
from scripts import verify_existing_gold100 as verifier
from worker.evaluation import existing_profile_semantic_diff as semantic_diff
from worker.evaluation.existing_profile_semantic_diff import (
    ExistingProfileSemanticError,
    LoadedSemanticArtifacts,
    SemanticArtifact,
    build_semantic_graph,
    calibrate_loaded_semantic_corpora,
    compare_loaded_semantic_corpora as compare_loaded_semantic_corpora_bgic,
    compare_semantic_profiles as compare_semantic_profiles_bgic,
    load_automatic_semantic_artifacts,
    load_reviewed_gold_semantic_artifacts,
    write_semantic_report,
)
from worker.evaluation.pristine_common_ir import LoadedPristineCommonIr


REAL_BASELINE_ENV = "PREREVIEW_EXISTING_GOLD100_BASELINE_ZIP"
REAL_GOLD_ENV = "PREREVIEW_EXISTING_GOLD100_GOLD_ROOT"
REQUIRE_REAL_GOLD100_ENV = "PREREVIEW_REQUIRE_EXISTING_GOLD100"
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
PACK_ID = f"{NOTICE_ID}-a-profile-v0.2"
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
        "candidate_pack_id": PACK_ID,
        "candidate_pack_generator": "semantic_structuring.common_ir_v1",
        "candidate_pack_generator_version": "1",
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
            "candidate_pack_id": PACK_ID,
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


def _pristine(*artifacts: SemanticArtifact) -> LoadedPristineCommonIr:
    documents = {artifact.notice_id: deepcopy(artifact.common_ir) for artifact in artifacts}
    return LoadedPristineCommonIr(
        documents=documents,
        archive_sha256="i" * 64,
        member_sha256={notice_id: "m" * 64 for notice_id in documents},
        manifest={},
    )


def compare_semantic_profiles(
    baseline: SemanticArtifact,
    gold: SemanticArtifact,
    candidate: SemanticArtifact,
    *,
    notice_id: str,
) -> dict[str, object]:
    """Synthetic-test shorthand with an explicit pristine I copied from B."""

    return compare_semantic_profiles_bgic(
        baseline,
        gold,
        deepcopy(baseline.common_ir),
        candidate,
        notice_id=notice_id,
    )


def compare_loaded_semantic_corpora(
    baseline: LoadedSemanticArtifacts,
    gold: LoadedSemanticArtifacts,
    candidate: LoadedSemanticArtifacts,
    *,
    notice_ids: list[str] | None = None,
) -> dict[str, object]:
    """Synthetic-test shorthand with an explicit pristine I copied from B."""

    return compare_loaded_semantic_corpora_bgic(
        baseline,
        gold,
        _pristine(*baseline.artifacts.values()),
        candidate,
        notice_ids=notice_ids,
    )


def _configured_real_corpora_paths() -> tuple[Path, Path] | None:
    """Resolve optional developer fixtures without hiding an explicit bad path."""

    baseline_raw = os.environ.get(REAL_BASELINE_ENV)
    gold_raw = os.environ.get(REAL_GOLD_ENV)
    require_real = os.environ.get(REQUIRE_REAL_GOLD100_ENV, "").strip().lower() in {
        "1", "true", "yes", "on",
    }
    if baseline_raw is None and gold_raw is None:
        if require_real:
            pytest.fail(
                f"{REQUIRE_REAL_GOLD100_ENV}=1 requires {REAL_BASELINE_ENV} and {REAL_GOLD_ENV}"
            )
        return None
    if not baseline_raw or not gold_raw:
        pytest.fail(
            f"set both {REAL_BASELINE_ENV} and {REAL_GOLD_ENV}, or set neither for a developer skip"
        )
    baseline = Path(baseline_raw)
    gold = Path(gold_raw)
    if not baseline.is_file():
        pytest.fail(f"explicitly configured baseline archive is missing: {baseline}")
    if not gold.is_dir():
        pytest.fail(f"explicitly configured Gold root is missing: {gold}")
    return baseline, gold


def _native_composite_artifact(
    *, with_unrouted_middle_block: bool = False
) -> SemanticArtifact:
    artifact = _artifact()
    left_id = "block-left"
    right_id = "block-right"
    left_text = "사업 목적 및"
    right_text = "지원 내용"
    right_order = 2 if with_unrouted_middle_block else 1
    blocks = [
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
            "reading_order": right_order,
            "structure_status": "explicit",
            "text_occurrence_ids": ["occ-right"],
            "occurrences": [{"occurrence_id": "occ-right", "text": right_text}],
        },
    ]
    if with_unrouted_middle_block:
        blocks.insert(1, {
            "block_id": "block-middle",
            "text": "라우팅되지 않은 중간 문단",
            "kind": "paragraph",
            "reading_order": 1,
            "structure_status": "explicit",
            "text_occurrence_ids": ["occ-middle"],
            "occurrences": [{
                "occurrence_id": "occ-middle",
                "text": "라우팅되지 않은 중간 문단",
            }],
        })
    artifact.common_ir["blocks"] = blocks  # type: ignore[index]
    artifact.source_selection["source_block_texts"] = {left_id: left_text, right_id: right_text}  # type: ignore[index]
    _install_regenerated_native_source(
        artifact, include_continuations=True,
        predicate=lambda block: bool(block.source_spans) and block.text == f"{left_text} {right_text}",
    )
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


def _replace_support_scale_numeric_token(
    artifact: SemanticArtifact,
    *,
    anchor_text: str,
    measure_type: str,
    measure_role: str,
    value: int,
    unit: str,
) -> None:
    """Replace the fixture's numeric token while retaining valid provenance."""

    source_text = f"지원 {anchor_text}"
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
    artifact.source_selection["numeric_candidates"][0].update(  # type: ignore[index]
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
        projection["measures"][0].update(
            {
                "measure_type": measure_type,
                "measure_role": measure_role,
                "lower_value": value,
                "upper_value": value,
                "unit": unit,
            }
        )


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
    paths = _configured_real_corpora_paths()
    if paths is None:
        pytest.skip(
            "external baseline/Gold corpora are not configured; set "
            f"{REAL_BASELINE_ENV} and {REAL_GOLD_ENV}"
        )
    baseline, gold = paths
    return (
        load_automatic_semantic_artifacts(baseline),
        load_reviewed_gold_semantic_artifacts(gold),
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


def test_synthetic_candidate_different_from_gold_fails_with_both_delta_directions() -> None:
    baseline = _artifact(value="기존 값", fact_id="baseline-id")
    gold = _artifact(value="교정 값", fact_id="gold-id")
    candidate = deepcopy(baseline)

    report = compare_semantic_profiles(baseline, gold, candidate, notice_id=NOTICE_ID)

    assert report["semantic_gate_status"] == "failed"
    assert report["counts"]["missing_gold"] > 0
    assert report["counts"]["candidate_only"] > 0


def test_delivery_role_rejects_empty_organization_and_missing_role_contract(monkeypatch) -> None:
    artifact = _artifact()
    fact = artifact.profile["comparison_profile"]["support_content"].pop()  # type: ignore[index]
    fact.update(
        {
            "field_name": "delivery_roles",
            "organization_names": [],
            "role_raw": None,
            "role_source_block_id": None,
            "role_source": None,
            "canonical_role": None,
        }
    )
    artifact.profile["comparison_profile"]["delivery_roles"] = [fact]  # type: ignore[index]
    selected = artifact.source_selection["selection"]["facts"][0]  # type: ignore[index]
    selected["field_name"] = "delivery_roles"
    materialized = artifact.source_selection["materialized_evidence"][0]  # type: ignore[index]
    materialized["field_name"] = "delivery_roles"
    monkeypatch.setattr(semantic_diff, "_validate_artifact_triple", lambda *args, **kwargs: None)

    with pytest.raises(ExistingProfileSemanticError, match="delivery_roles requires at least one organization_name"):
        build_semantic_graph(artifact)


def test_delivery_role_null_raw_rejects_non_null_role_provenance(monkeypatch) -> None:
    artifact = _artifact()
    fact = artifact.profile["comparison_profile"]["support_content"].pop()  # type: ignore[index]
    fact.update(
        {
            "field_name": "delivery_roles",
            "organization_names": [],
            "role_raw": None,
            "role_source_block_id": "block-1",
            "role_source": {
                "source_block_id": "block-1",
                "start_char": 0,
                "end_char": len("기존 값"),
                "text_basis": "common_ir_v1_candidate_pack",
            },
            "canonical_role": None,
        }
    )
    artifact.profile["comparison_profile"]["delivery_roles"] = [fact]  # type: ignore[index]
    monkeypatch.setattr(semantic_diff, "_validate_artifact_triple", lambda *args, **kwargs: None)

    with pytest.raises(ExistingProfileSemanticError):
        build_semantic_graph(artifact)


def test_delivery_role_rejects_role_source_anchor_disagreement(monkeypatch) -> None:
    artifact = _artifact()
    fact = artifact.profile["comparison_profile"]["support_content"].pop()  # type: ignore[index]
    fact.update(
        {
            "field_name": "delivery_roles",
            "organization_names": [
                {
                    "value_raw": "기존 값",
                    "value_source": {
                        "source_block_id": "block-1",
                        "start_char": 0,
                        "end_char": len("기존 값"),
                        "text_basis": "common_ir_v1_candidate_pack",
                    },
                }
            ],
            "role_raw": "교정 값",
            "role_source_block_id": "other-block",
            "role_source": {
                "source_block_id": "block-1",
                "start_char": len("기존 값\n"),
                "end_char": len(SOURCE_TEXT),
                "text_basis": "common_ir_v1_candidate_pack",
            },
            "canonical_role": "lead_agency",
        }
    )
    artifact.profile["comparison_profile"]["delivery_roles"] = [fact]  # type: ignore[index]
    monkeypatch.setattr(semantic_diff, "_validate_artifact_triple", lambda *args, **kwargs: None)

    with pytest.raises(ExistingProfileSemanticError, match="role source and anchor must agree"):
        build_semantic_graph(artifact)


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


def _install_regenerated_native_source(
    artifact: SemanticArtifact,
    *,
    include_continuations: bool,
    predicate,
) -> None:
    """Convert one synthetic fact to a real production-A transformed source."""

    source_ids = set(artifact.source_selection["source_block_texts"])  # type: ignore[index]
    base = verifier._existing_a_base_candidate_pack(
        artifact.common_ir, source_ids, label="semantic synthetic native"
    )
    transformed = verifier.augment_pack_with_native_exact_transforms(
        base,
        options=verifier.NativeExactTransformOptions(
            enabled=True, include_line_atoms=True,
            include_continuations=include_continuations,
        ),
    )
    target = next(block for block in transformed.blocks if predicate(block))
    payload = verifier._native_block_payload(target)
    artifact.source_selection["source_block_texts"] = {  # type: ignore[index]
        block.block_id: block.text for block in transformed.blocks
    }
    for lineage in (
        artifact.source_selection["candidate_pack_lineage"],  # type: ignore[index]
        artifact.profile["processing_metadata"]["candidate_pack"],  # type: ignore[index]
    ):
        lineage.update({
            "candidate_pack_id": transformed.pack_id,
            "candidate_pack_generator": transformed.generator,
            "candidate_pack_generator_version": transformed.generator_version,
            "parent_pack_id": transformed.parent_pack_id,
            "parent_generator": transformed.parent_generator,
            "parent_generator_version": transformed.parent_generator_version,
        })
    artifact.source_selection["selection"]["candidate_pack_id"] = transformed.pack_id  # type: ignore[index]
    fact = artifact.profile["comparison_profile"]["support_content"][0]  # type: ignore[index]
    fact["value_source"] = {
        "source_block_id": target.block_id,
        "start_char": 0,
        "end_char": len(target.text),
        "text_basis": "common_ir_v1_candidate_pack",
    }
    fact["value_raw"] = target.text
    fact["evidence"][0].update(payload)  # type: ignore[index]
    selected = artifact.source_selection["selection"]["facts"][0]  # type: ignore[index]
    selected["value_anchor"] = {"source_block_id": target.block_id, "anchor_text": target.text}
    materialized = artifact.source_selection["materialized_evidence"][0]  # type: ignore[index]
    materialized["value_source"] = fact["value_source"]
    materialized["source_blocks"][0].update(payload)
    materialized["source_blocks"][0]["text"] = target.text


def _as_runpod_014_no_parent_native(artifact: SemanticArtifact) -> None:
    """Rewrite current native lineage to the in-place RunPod 0.1.4 shape."""

    source_ids = set(artifact.source_selection["source_block_texts"])  # type: ignore[index]
    base = verifier._existing_a_base_candidate_pack(
        artifact.common_ir, source_ids, label="semantic legacy native"
    )
    for lineage in (
        artifact.source_selection["candidate_pack_lineage"],  # type: ignore[index]
        artifact.profile["processing_metadata"]["candidate_pack"],  # type: ignore[index]
    ):
        lineage.update({
            "candidate_pack_id": base.pack_id,
            "candidate_pack_generator": base.generator,
            "candidate_pack_generator_version": base.generator_version,
        })
        for key in ("parent_pack_id", "parent_generator", "parent_generator_version"):
            lineage.pop(key, None)
    artifact.source_selection["selection"]["candidate_pack_id"] = base.pack_id  # type: ignore[index]


def _native_line_artifacts() -> tuple[SemanticArtifact, SemanticArtifact, SemanticArtifact]:
    baseline = _artifact(value="기존 값", occurrence_id="occ-old")
    gold = deepcopy(baseline)
    candidate = deepcopy(gold)
    _install_regenerated_native_source(
        candidate, include_continuations=False,
        predicate=lambda block: block.native_parent_block_id is not None and block.text == "기존 값",
    )

    return baseline, gold, candidate


def test_candidate_can_use_a_server_regenerated_native_line_atom() -> None:
    baseline, gold, candidate = _native_line_artifacts()
    report = compare_semantic_profiles(baseline, gold, candidate, notice_id=NOTICE_ID)
    assert report["semantic_gate_status"] == "passed"


def test_candidate_graph_accepts_distinct_native_context_spans_with_shared_parent_occurrences() -> None:
    _baseline, _gold, candidate = _native_line_artifacts()
    source_ids = set(candidate.source_selection["source_block_texts"])  # type: ignore[index]
    base = verifier._existing_a_base_candidate_pack(
        candidate.common_ir, source_ids, label="semantic shared native parent"
    )
    transformed = verifier.augment_pack_with_native_exact_transforms(
        base,
        options=verifier.NativeExactTransformOptions(
            enabled=True,
            include_line_atoms=True,
            include_continuations=False,
        ),
    )
    lines = [
        block
        for block in transformed.blocks
        if block.native_parent_block_id is not None
    ]
    assert len(lines) == 2
    assert lines[0].common_ir_occurrence_ids == lines[1].common_ir_occurrence_ids

    selected = candidate.source_selection["selection"]["facts"][0]  # type: ignore[index]
    selected["context_source_block_ids"] = [block.block_id for block in lines]
    materialized = candidate.source_selection["materialized_evidence"][0]  # type: ignore[index]
    materialized["context_blocks"] = [
        {
            **verifier._native_block_payload(block),
            "text": block.text,
            "common_ir_document_id": DOCUMENT_ID,
        }
        for block in lines
    ]
    fact = candidate.profile["comparison_profile"]["support_content"][0]  # type: ignore[index]
    fact["context_evidence"] = [
        {
            **verifier._native_block_payload(block),
            "text": block.text,
            "common_ir_document_id": DOCUMENT_ID,
        }
        for block in lines
    ]

    # Candidate mode keeps each exact context text fingerprint after strict
    # source validation; shared parent occurrence ids are admission metadata.
    build_semantic_graph(
        candidate,
        include_evidence_provenance=False,
        allow_candidate_native_context_overlap=True,
    )
    direct_overlap = deepcopy(fact)
    direct_overlap["evidence"] = deepcopy(fact["context_evidence"])
    direct_overlap["context_evidence"] = []
    resolver = semantic_diff._ProvenanceResolver(
        candidate.source_selection,
        candidate.common_ir,
        label="candidate direct native overlap",
    )
    with pytest.raises(
        ExistingProfileSemanticError,
        match="evidence repeats Common IR occurrence provenance",
    ):
        semantic_diff._fact_base(
            direct_overlap,
            resolver=resolver,
            label="candidate direct native overlap fact",
            include_evidence_provenance=False,
            allow_candidate_native_context_overlap=True,
        )
    # Historical baseline/Gold calibration remains provenance-strict.
    with pytest.raises(
        ExistingProfileSemanticError,
        match="context_evidence repeats Common IR occurrence provenance",
    ):
        build_semantic_graph(candidate, include_evidence_provenance=True)

    # Full B/G/I/C comparison: reviewed references use two ordinary atomic
    # sources with unique occurrences, while C expresses the same two context
    # texts as disjoint native lines inherited from one parent occurrence.
    reference = _artifact()
    reference.common_ir["blocks"] = [  # type: ignore[index]
        {
            "block_id": "block-1",
            "text": "기존 값",
            "kind": "paragraph",
            "reading_order": 0,
            "structure_status": "explicit",
            "text_occurrence_ids": ["occ-old"],
            "occurrences": [{"occurrence_id": "occ-old", "text": "기존 값"}],
        },
        {
            "block_id": "block-2",
            "text": "교정 값",
            "kind": "paragraph",
            "reading_order": 1,
            "structure_status": "explicit",
            "text_occurrence_ids": ["occ-context"],
            "occurrences": [{
                "occurrence_id": "occ-context", "text": "교정 값",
            }],
        },
    ]
    reference.source_selection["source_block_texts"] = {  # type: ignore[index]
        "block-1": "기존 값",
        "block-2": "교정 값",
    }

    def atomic_reference(block_id: str, occurrence_id: str, text: str) -> dict[str, object]:
        return {
            "source_block_id": block_id,
            "text": text,
            "section_id": "main_notice",
            "source_occurrence_ids": [occurrence_id],
            "common_ir_document_id": DOCUMENT_ID,
            "common_ir_block_id": block_id,
            "common_ir_occurrence_ids": [occurrence_id],
        }

    reference_selected = reference.source_selection["selection"]["facts"][0]  # type: ignore[index]
    reference_selected["context_source_block_ids"] = ["block-1", "block-2"]
    reference_materialized = reference.source_selection["materialized_evidence"][0]  # type: ignore[index]
    reference_materialized["source_blocks"] = [
        atomic_reference("block-1", "occ-old", "기존 값")
    ]
    reference_materialized["context_blocks"] = [
        atomic_reference("block-1", "occ-old", "기존 값"),
        atomic_reference("block-2", "occ-context", "교정 값"),
    ]
    reference_fact = reference.profile["comparison_profile"]["support_content"][0]  # type: ignore[index]
    reference_fact["evidence"] = [{
        key: value
        for key, value in atomic_reference(
            "block-1", "occ-old", "기존 값"
        ).items()
        if key != "text"
    }]
    reference_fact["context_evidence"] = [
        atomic_reference("block-1", "occ-old", "기존 값"),
        atomic_reference("block-2", "occ-context", "교정 값"),
    ]

    report = compare_semantic_profiles_bgic(
        reference,
        deepcopy(reference),
        deepcopy(candidate.common_ir),
        candidate,
        notice_id=NOTICE_ID,
    )
    assert report["semantic_gate_status"] == "passed"


def test_candidate_graph_rejects_atomic_occurrence_reuse() -> None:
    artifact = _artifact()
    fact = deepcopy(
        artifact.profile["comparison_profile"]["support_content"][0]  # type: ignore[index]
    )
    repeated = deepcopy(fact["evidence"][0])
    fact["context_evidence"] = [deepcopy(repeated), deepcopy(repeated)]
    resolver = semantic_diff._ProvenanceResolver(
        artifact.source_selection,
        artifact.common_ir,
        label="candidate atomic duplicate",
    )

    with pytest.raises(
        ExistingProfileSemanticError,
        match="context_evidence repeats Common IR occurrence provenance",
    ):
        semantic_diff._fact_base(
            fact,
            resolver=resolver,
            label="candidate atomic duplicate fact",
            include_evidence_provenance=False,
            allow_candidate_native_context_overlap=True,
        )


def test_candidate_graph_rejects_same_native_line_span_repeated() -> None:
    _baseline, _gold, candidate = _native_line_artifacts()
    fact = deepcopy(
        candidate.profile["comparison_profile"]["support_content"][0]  # type: ignore[index]
    )
    repeated = deepcopy(fact["evidence"][0])
    fact["context_evidence"] = [deepcopy(repeated), deepcopy(repeated)]
    resolver = semantic_diff._ProvenanceResolver(
        candidate.source_selection,
        candidate.common_ir,
        label="candidate native duplicate",
    )

    with pytest.raises(
        ExistingProfileSemanticError,
        match="context_evidence repeats Common IR occurrence provenance",
    ):
        semantic_diff._fact_base(
            fact,
            resolver=resolver,
            label="candidate native duplicate fact",
            include_evidence_provenance=False,
            allow_candidate_native_context_overlap=True,
        )


@pytest.mark.parametrize(
    "locators",
    [
        # Two independently validated line locators still collide when their
        # ranges overlap or their regenerated parents differ.
        (
            ("native_parent_span", (("parent", 0, 10),)),
            ("native_parent_span", (("parent", 5, 12),)),
        ),
        (
            ("native_parent_span", (("parent-a", 0, 5),)),
            ("native_parent_span", (("parent-b", 6, 10),)),
        ),
        # Composite source_spans have no reviewed shared-parent relaxation.
        (
            ("source_spans", (("left", 0, 5), ("middle", 0, 4))),
            ("source_spans", (("right", 0, 5),)),
        ),
    ],
)
def test_candidate_occurrence_collision_rejects_non_disjoint_same_parent_lines(
    locators: tuple[tuple[object, ...], tuple[object, ...]],
) -> None:
    rows = [
        {"occurrence_ids": ["shared"], "native_locator": locator}
        for locator in locators
    ]

    assert not semantic_diff._candidate_occurrence_collisions_use_distinct_native_spans(
        rows
    )


def test_candidate_can_use_a_runpod_014_no_parent_native_line_atom() -> None:
    baseline, gold, candidate = _native_line_artifacts()
    _as_runpod_014_no_parent_native(candidate)

    report = compare_semantic_profiles(baseline, gold, candidate, notice_id=NOTICE_ID)

    assert report["semantic_gate_status"] == "passed"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("candidate_pack_generator", "unrelated.generator"),
        ("candidate_pack_generator_version", "999"),
        ("candidate_pack_id", f"{NOTICE_ID}-unrelated-pack"),
    ],
)
def test_runpod_014_no_parent_native_rejects_unrelated_lineage(
    field: str,
    value: str,
) -> None:
    baseline, gold, candidate = _native_line_artifacts()
    _as_runpod_014_no_parent_native(candidate)
    for lineage in (
        candidate.source_selection["candidate_pack_lineage"],  # type: ignore[index]
        candidate.profile["processing_metadata"]["candidate_pack"],  # type: ignore[index]
    ):
        lineage[field] = value
    if field == "candidate_pack_id":
        candidate.source_selection["selection"]["candidate_pack_id"] = value  # type: ignore[index]

    with pytest.raises(
        ExistingProfileSemanticError,
        match="not a reviewed RunPod 0.1.4 CandidatePack",
    ):
        compare_semantic_profiles(baseline, gold, candidate, notice_id=NOTICE_ID)


@pytest.mark.parametrize("mutation", ["missing", "tampered", "invalid_lineage"])
def test_native_line_final_evidence_and_lineage_fail_closed(mutation: str) -> None:
    baseline, gold, candidate = _native_line_artifacts()
    evidence = candidate.profile["comparison_profile"]["support_content"][0]["evidence"][0]  # type: ignore[index]
    materialized = candidate.source_selection["materialized_evidence"][0]["source_blocks"][0]  # type: ignore[index]
    if mutation == "missing":
        evidence.pop("native_parent_span")
    elif mutation == "tampered":
        evidence["native_parent_span"]["exact_text"] = "위조된 값"
        materialized["native_parent_span"]["exact_text"] = "위조된 값"
    else:
        for lineage in (
            candidate.source_selection["candidate_pack_lineage"],  # type: ignore[index]
            candidate.profile["processing_metadata"]["candidate_pack"],  # type: ignore[index]
        ):
            lineage["candidate_pack_generator_version"] = "1:off"

    with pytest.raises(ExistingProfileSemanticError):
        compare_semantic_profiles(baseline, gold, candidate, notice_id=NOTICE_ID)


def test_native_source_projection_normalizes_collision_errors_without_raw_source() -> None:
    artifact = _artifact()
    private_text = "PRIVATE-SOURCE-SENTINEL\n안전"
    first_line_end = private_text.index("\n")
    derived_id = (
        "line:"
        + sha256(f"p0\0{0}\0{first_line_end}".encode()).hexdigest()[:20]
    )
    artifact.common_ir["document"]["document_id"] = "hwpx:PRIVATE-DOCUMENT-ID"  # type: ignore[index]
    artifact.common_ir["document"]["source_kind"] = "hwpx"  # type: ignore[index]
    artifact.common_ir["blocks"] = [  # type: ignore[index]
        {
            "block_id": "p0",
            "text": private_text,
            "kind": "paragraph",
            "reading_order": 0,
            "structure_status": "explicit",
            "text_occurrence_ids": ["occ-private"],
            "occurrences": [
                {"occurrence_id": "occ-private", "text": private_text}
            ],
        },
        {
            # This atomic ID collides with the deterministic first-line ID.
            "block_id": derived_id,
            "text": "충돌 블록",
            "kind": "paragraph",
            "reading_order": 1,
            "structure_status": "explicit",
            "text_occurrence_ids": ["occ-collision"],
            "occurrences": [
                {"occurrence_id": "occ-collision", "text": "충돌 블록"}
            ],
        },
    ]

    with pytest.raises(ExistingProfileSemanticError) as caught:
        semantic_diff._native_source_contracts(
            artifact.common_ir, label="candidate"
        )

    message = str(caught.value)
    assert "exact-native source projection failed" in message
    assert "PRIVATE-SOURCE-SENTINEL" not in message
    assert "PRIVATE-DOCUMENT-ID" not in message
    assert derived_id not in message
    assert "input_value" not in message


def test_gold_companions_must_remain_bound_to_the_loaded_freeze_manifest(
    monkeypatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "gold"
    notice_root = root / "notices" / NOTICE_ID
    notice_root.mkdir(parents=True)
    old_manifest_digest = "a" * 64
    monkeypatch.setattr(
        semantic_diff,
        "load_reviewed_gold",
        lambda *args, **kwargs: SimpleNamespace(
            profiles={NOTICE_ID: {}},
            identity={"freeze_manifest_sha256": old_manifest_digest},
        ),
    )

    def snapshot(path, *, label):
        if path.name == "freeze_manifest.json":
            return SimpleNamespace(value={}, digest="b" * 64)
        return SimpleNamespace(value={})

    monkeypatch.setattr(semantic_diff, "_read_regular_json_snapshot", snapshot)
    monkeypatch.setattr(semantic_diff, "_validate_artifact_triple", lambda *args, **kwargs: None)
    verified = False

    def verify(*args, **kwargs):
        nonlocal verified
        verified = True
        return SimpleNamespace(status="valid")

    monkeypatch.setattr(semantic_diff, "verify_gold_root", verify)

    with pytest.raises(
        ExistingProfileSemanticError,
        match="freeze manifest changed between Profile and companion loading",
    ):
        load_reviewed_gold_semantic_artifacts(root, expected_profile_count=1)

    assert verified is False


def test_gold_directory_swap_before_companion_reads_fails_index_binding(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """A second tree cannot splice unindexed companions onto loaded Profiles."""

    root = tmp_path / "gold"
    evil_root = tmp_path / "evil-gold"
    parked_root = tmp_path / "parked-gold"

    def encoded(document: object) -> bytes:
        return json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    def write_tree(tree: Path, *, selection: Mapping[str, object]) -> tuple[bytes, bytes]:
        notice = tree / "notices" / NOTICE_ID
        notice.mkdir(parents=True)
        selection_bytes = encoded(selection)
        common_ir_bytes = encoded({"common_ir": "trusted"})
        (notice / "source_selection.v0.2.json").write_bytes(selection_bytes)
        (notice / "common_ir_v1.json").write_bytes(common_ir_bytes)
        artifact_index_bytes = encoded(
            {
                "artifacts": [
                    {
                        "notice_id": NOTICE_ID,
                        "artifact": "source_selection.v0.2",
                        "path": f"notices/{NOTICE_ID}/source_selection.v0.2.json",
                        "sha256": sha256(selection_bytes).hexdigest(),
                        "bytes": len(selection_bytes),
                    },
                    {
                        "notice_id": NOTICE_ID,
                        "artifact": "common_ir_v1",
                        "path": f"notices/{NOTICE_ID}/common_ir_v1.json",
                        "sha256": sha256(common_ir_bytes).hexdigest(),
                        "bytes": len(common_ir_bytes),
                    },
                ]
            }
        )
        (tree / "artifact_index.json").write_bytes(artifact_index_bytes)
        manifest_bytes = encoded(
            {"artifact_index_sha256": sha256(artifact_index_bytes).hexdigest()}
        )
        (tree / "freeze_manifest.json").write_bytes(manifest_bytes)
        return artifact_index_bytes, manifest_bytes

    good_index, good_manifest = write_tree(root, selection={"selection": "trusted"})
    evil_index, evil_manifest = write_tree(evil_root, selection={"selection": "evil"})
    # The adversarial tree copies G0's two root pins, but not its selection
    # bytes.  A final whole-tree check alone can be raced around this window.
    (evil_root / "artifact_index.json").write_bytes(good_index)
    (evil_root / "freeze_manifest.json").write_bytes(good_manifest)
    assert evil_index != good_index
    assert evil_manifest != good_manifest

    monkeypatch.setattr(
        semantic_diff,
        "load_reviewed_gold",
        lambda *args, **kwargs: SimpleNamespace(
            profiles={NOTICE_ID: {"profile": "G0"}},
            identity={"freeze_manifest_sha256": sha256(good_manifest).hexdigest()},
        ),
    )
    monkeypatch.setattr(semantic_diff, "_validate_artifact_triple", lambda *args, **kwargs: None)
    real_read = semantic_diff._read_regular_json_snapshot
    swapped = False

    def swap_before_first_companion(path: Path, *, label: str):
        nonlocal swapped
        if label.startswith("Gold source selection") and not swapped:
            root.rename(parked_root)
            evil_root.rename(root)
            swapped = True
        return real_read(path, label=label)

    monkeypatch.setattr(semantic_diff, "_read_regular_json_snapshot", swap_before_first_companion)

    with pytest.raises(
        ExistingProfileSemanticError,
        match="Gold companion snapshot does not match the artifact index",
    ) as caught:
        load_reviewed_gold_semantic_artifacts(root, expected_profile_count=1)

    assert swapped is True
    assert "evil" not in str(caught.value)


def test_candidate_can_use_a_server_regenerated_native_composite() -> None:
    baseline = _native_composite_artifact()
    gold = deepcopy(baseline)
    candidate = deepcopy(gold)

    report = compare_semantic_profiles(baseline, gold, candidate, notice_id=NOTICE_ID)

    assert report["semantic_gate_status"] == "passed"


def test_candidate_can_use_a_runpod_014_no_parent_native_composite() -> None:
    baseline = _native_composite_artifact()
    gold = deepcopy(baseline)
    candidate = deepcopy(gold)
    _as_runpod_014_no_parent_native(candidate)

    report = compare_semantic_profiles(baseline, gold, candidate, notice_id=NOTICE_ID)

    assert report["semantic_gate_status"] == "passed"


def test_routed_native_composite_ignores_unselected_common_ir_middle_block() -> None:
    baseline = _native_composite_artifact(with_unrouted_middle_block=True)
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

    with pytest.raises(ExistingProfileSemanticError, match="regenerated native CandidatePack block"):
        compare_semantic_profiles(baseline, gold, candidate, notice_id=NOTICE_ID)


def test_candidate_composite_extra_span_key_fails_closed() -> None:
    baseline = _native_composite_artifact()
    gold = deepcopy(baseline)
    candidate = deepcopy(gold)
    for source in (
        candidate.source_selection["materialized_evidence"][0]["source_blocks"][0],  # type: ignore[index]
        candidate.profile["comparison_profile"]["support_content"][0]["evidence"][0],  # type: ignore[index]
    ):
        source["source_spans"][0]["forged_key"] = "nope"
    with pytest.raises(ExistingProfileSemanticError):
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
    ("anchor_text", "measure_type", "measure_role", "value", "unit"),
    [
        ("3억 2천만원 500원", "amount", "support_amount", 320_000_500, "KRW"),
        ("2천5백3십4개사", "count", "selection_capacity", 2_534, "개사"),
        ("10억원", "amount", "support_amount", 1_000_000_000, "KRW"),
        ("10%", "rate", "support_rate", 1_000, "BPS"),
        ("10개사", "count", "selection_capacity", 10, "개사"),
    ],
)
def test_semantic_gate_uses_production_numeric_contract_for_new_and_simple_forms(
    anchor_text: str,
    measure_type: str,
    measure_role: str,
    value: int,
    unit: str,
) -> None:
    artifact = _artifact_with_support_scale_measure()
    _replace_support_scale_numeric_token(
        artifact,
        anchor_text=anchor_text,
        measure_type=measure_type,
        measure_role=measure_role,
        value=value,
        unit=unit,
    )

    assert build_semantic_graph(artifact).digest


@pytest.mark.parametrize(
    "source_text",
    [
        "지원금 최대 1 000원",
        "지원금 최대 1억\n5000만원",
        "선정규모 2천5백3십4백명",
    ],
)
def test_semantic_gate_numeric_enumerator_reuses_production_malformed_guards(
    source_text: str,
) -> None:
    assert semantic_diff._server_enumerated_numeric_spans(source_text) == frozenset()


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


def test_candidate_common_ir_must_be_bound_to_pristine_input() -> None:
    baseline = _artifact()
    gold = deepcopy(baseline)
    candidate = deepcopy(gold)
    candidate.common_ir["blocks"][0]["text"] = "조작된 Common IR"  # type: ignore[index]

    with pytest.raises(ExistingProfileSemanticError, match="pinned pristine Common IR"):
        compare_semantic_profiles(baseline, gold, candidate, notice_id=NOTICE_ID)


def test_ordered_common_ir_coordinate_tuple_cannot_be_permuted() -> None:
    baseline = _artifact()
    baseline.common_ir["blocks"][0]["occurrences"] = [  # type: ignore[index]
        {"occurrence_id": "occ-old", "bbox": [0, 0, 10, 20]}
    ]
    gold = deepcopy(baseline)
    candidate = deepcopy(gold)
    candidate.common_ir["blocks"][0]["occurrences"][0]["bbox"] = [0, 0, 20, 10]  # type: ignore[index]

    with pytest.raises(ExistingProfileSemanticError, match="pinned pristine Common IR"):
        compare_semantic_profiles(baseline, gold, candidate, notice_id=NOTICE_ID)


def test_candidate_binds_to_pristine_input_not_historical_baseline() -> None:
    baseline = _artifact(occurrence_id="occ-baseline")
    pristine = _artifact(occurrence_id="occ-input")
    gold = deepcopy(pristine)
    candidate = deepcopy(pristine)

    report = compare_semantic_profiles_bgic(
        baseline,
        gold,
        pristine.common_ir,
        candidate,
        notice_id=NOTICE_ID,
    )

    assert report["semantic_gate_status"] == "passed"
    assert report["graphs"]["input_common_ir_sha256"] == semantic_diff._common_ir_input_digest(
        pristine.common_ir
    )


def test_candidate_matching_baseline_but_not_pristine_input_is_rejected() -> None:
    baseline = _artifact(occurrence_id="occ-baseline")
    gold = deepcopy(baseline)
    candidate = deepcopy(baseline)
    pristine = _artifact(occurrence_id="occ-input")

    with pytest.raises(ExistingProfileSemanticError, match="pinned pristine Common IR"):
        compare_semantic_profiles_bgic(
            baseline,
            gold,
            pristine.common_ir,
            candidate,
            notice_id=NOTICE_ID,
        )


def test_gold_adjudication_occurrence_ids_are_validation_only_for_candidate_gate() -> None:
    baseline = _artifact(occurrence_id="occ-native")
    pristine = deepcopy(baseline)
    candidate = deepcopy(pristine)
    gold = deepcopy(baseline)
    gold_block = gold.common_ir["blocks"][0]  # type: ignore[index]
    gold_block["text_occurrence_ids"].append("occ-adjudicated")  # type: ignore[union-attr]
    gold_block["occurrences"].append(  # type: ignore[union-attr]
        {"occurrence_id": "occ-adjudicated", "text": SOURCE_TEXT}
    )
    gold_fact_evidence = gold.profile["comparison_profile"]["support_content"][0]["evidence"][0]  # type: ignore[index]
    gold_materialized_evidence = gold.source_selection["materialized_evidence"][0]["source_blocks"][0]  # type: ignore[index]
    for evidence in (gold_fact_evidence, gold_materialized_evidence):
        evidence["source_occurrence_ids"] = ["occ-adjudicated"]
        evidence["common_ir_occurrence_ids"] = ["occ-adjudicated"]

    report = compare_semantic_profiles_bgic(
        baseline,
        gold,
        pristine.common_ir,
        candidate,
        notice_id=NOTICE_ID,
    )

    assert report["semantic_gate_status"] == "passed"


def test_candidate_gate_rejects_different_admitted_context_evidence() -> None:
    baseline = _artifact()
    gold = deepcopy(baseline)
    pristine = deepcopy(baseline)
    context_text = "후보가 임의로 추가한 다른 문맥"
    pristine.common_ir["blocks"].append(  # type: ignore[union-attr]
        {
            "block_id": "block-2",
            "text": context_text,
            "kind": "paragraph",
            "reading_order": 1,
            "structure_status": "explicit",
            "text_occurrence_ids": ["occ-context"],
            "occurrences": [
                {"occurrence_id": "occ-context", "text": context_text}
            ],
        }
    )
    candidate = deepcopy(pristine)
    candidate.source_selection["source_block_texts"]["block-2"] = context_text  # type: ignore[index]
    selection_fact = candidate.source_selection["selection"]["facts"][0]  # type: ignore[index]
    selection_fact["context_source_block_ids"] = ["block-2"]
    materialized = candidate.source_selection["materialized_evidence"][0]  # type: ignore[index]
    materialized["context_blocks"] = [
        {
            "source_block_id": "block-2",
            "text": context_text,
            "section_id": "main_notice",
            "source_occurrence_ids": ["occ-context"],
            "common_ir_document_id": DOCUMENT_ID,
            "common_ir_block_id": "block-2",
            "common_ir_occurrence_ids": ["occ-context"],
        }
    ]
    candidate_fact = candidate.profile["comparison_profile"]["support_content"][0]  # type: ignore[index]
    candidate_fact["context_evidence"] = [
        {
            "source_block_id": "block-2",
            "text": context_text,
            "source_occurrence_ids": ["occ-context"],
            "common_ir_document_id": DOCUMENT_ID,
            "common_ir_block_id": "block-2",
            "common_ir_occurrence_ids": ["occ-context"],
            "section_id": "main_notice",
        }
    ]

    report = compare_semantic_profiles_bgic(
        baseline,
        gold,
        pristine.common_ir,
        candidate,
        notice_id=NOTICE_ID,
    )

    assert report["semantic_gate_status"] == "failed"
    assert report["counts"]["candidate_only"] > 0
    assert report["counts"]["missing_gold"] > 0


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
    assert report["input"] == {
        "archive_sha256": "i" * 64,
        "notice_count": 1,
        "role": "pinned_pristine_common_ir_input",
    }
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
    monkeypatch.setattr(cli, "_verify_input_pins", lambda args: None)
    monkeypatch.setattr(cli, "_verify_loaded_report_pins", lambda args, report: None)
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
            "--input-zip", "input.zip",
            "--expected-input-sha256", cli.PRISTINE_HARD6_ARCHIVE_SHA256,
            "--candidate-zip", "candidate.zip",
            "--output-dir", str(tmp_path),
        ]
    )

    assert exit_code == 2
    assert json.loads(capsys.readouterr().out)["status"] == "failed"


def test_cli_candidate_mode_requires_explicit_pristine_input_and_pin(monkeypatch) -> None:
    args = cli.build_parser().parse_args(
        [
            "--baseline-zip", "baseline.zip",
            "--gold-root", "gold",
            "--candidate-zip", "candidate.zip",
            "--output-dir", "output",
        ]
    )
    monkeypatch.setattr(cli, "_require_sha256_pin", lambda **_kwargs: None)

    with pytest.raises(ExistingProfileSemanticError, match="requires --input-zip"):
        cli._verify_input_pins(args)


def test_cli_returns_zero_and_forwards_selection_options_when_gate_passes(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(cli, "_verify_input_pins", lambda args: None)
    monkeypatch.setattr(cli, "_verify_loaded_report_pins", lambda args, report: None)

    def compare(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return {"counts": {"selected": 2, "passed": 2, "failed": 0}}

    monkeypatch.setattr(cli, "compare_semantic_profile_corpora", compare)
    monkeypatch.setattr(
        cli,
        "write_semantic_report",
        lambda *args, **kwargs: tmp_path / "existing-profile-semantic-bgic.v1.json",
    )

    exit_code = cli.main(
        [
            "--baseline-zip", "baseline.zip",
            "--gold-root", "gold",
            "--input-zip", "input.zip",
            "--expected-input-sha256", cli.PRISTINE_HARD6_ARCHIVE_SHA256,
            "--candidate-zip", "candidate.zip",
            "--output-dir", str(tmp_path),
            "--expected-reference-count", "100",
            "--expected-candidate-count", "6",
            "--notice-id", "PBLN_000000000103645",
            "--notice-id", "PBLN_000000000112425",
        ]
    )

    assert exit_code == 0
    assert captured["args"] == (
        Path("baseline.zip"),
        Path("gold"),
        Path("input.zip"),
        Path("candidate.zip"),
    )
    assert captured["kwargs"] == {
        "input_manifest": None,
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
        "report_file": "existing-profile-semantic-bgic.v1.json",
        "selected": 2,
        "status": "passed",
    }


def test_cli_uses_explicit_baseline_calibration_mode(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(cli, "_verify_input_pins", lambda args: None)
    monkeypatch.setattr(cli, "_verify_loaded_report_pins", lambda args, report: None)

    def calibrate(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return {
            "counts": {"selected": 100, "passed": 94, "failed": 6},
            "notices": [
                {
                    "notice_id": notice_id,
                    "semantic_gate_status": "failed" if notice_id in SIX else "passed",
                }
                for notice_id in [
                    *SIX,
                    *[f"PBLN_000000000200{index:03d}" for index in range(94)],
                ]
            ],
        }

    monkeypatch.setattr(cli, "calibrate_semantic_profile_corpora", calibrate)
    monkeypatch.setattr(
        cli,
        "write_semantic_report",
        lambda *args, **kwargs: tmp_path / "existing-profile-semantic-bgic.v1.json",
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

    assert exit_code == 0
    assert captured["kwargs"] == {
        "expected_reference_count": 100,
        "notice_ids": None,
    }
    assert json.loads(capsys.readouterr().out)["status"] == "calibration_matched"


def test_cli_rejects_mismatched_input_pin_before_comparison_or_report(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    baseline = tmp_path / "baseline.zip"
    input_archive = tmp_path / "input.zip"
    candidate = tmp_path / "candidate.zip"
    gold = tmp_path / "gold"
    baseline.write_bytes(b"baseline")
    input_archive.write_bytes(b"input")
    candidate.write_bytes(b"candidate")
    gold.mkdir()
    (gold / "freeze_manifest.json").write_bytes(b"freeze")
    compared = False
    written = False

    def compare(*args, **kwargs):
        nonlocal compared
        compared = True
        return {"counts": {"selected": 1, "passed": 1, "failed": 0}}

    def write(*args, **kwargs):
        nonlocal written
        written = True
        return tmp_path / "report.json"

    monkeypatch.setattr(cli, "compare_semantic_profile_corpora", compare)
    monkeypatch.setattr(cli, "write_semantic_report", write)
    input_sha256 = sha256(input_archive.read_bytes()).hexdigest()
    monkeypatch.setattr(cli, "PRISTINE_HARD6_ARCHIVE_SHA256", input_sha256)
    exit_code = cli.main(
        [
            "--baseline-zip", str(baseline),
            "--gold-root", str(gold),
            "--input-zip", str(input_archive),
            "--expected-input-sha256", input_sha256,
            "--candidate-zip", str(candidate),
            "--output-dir", str(tmp_path),
            "--expected-baseline-sha256", sha256(baseline.read_bytes()).hexdigest(),
            "--expected-gold-freeze-manifest-sha256", sha256((gold / "freeze_manifest.json").read_bytes()).hexdigest(),
            "--expected-candidate-sha256", "0" * 64,
        ]
    )

    assert exit_code == 1
    assert compared is False
    assert written is False
    assert "candidate archive SHA-256 pin mismatch" in capsys.readouterr().err


def test_cli_preflight_rejects_an_oversized_file_before_hashing(tmp_path: Path) -> None:
    oversized = tmp_path / "oversized-freeze-manifest.json"
    oversized.write_bytes(b"x" * 5)

    with pytest.raises(ExistingProfileSemanticError, match="preflight size cap"):
        cli._regular_file_sha256(
            oversized,
            label="Gold freeze manifest",
            max_bytes=4,
        )


def test_cli_preflight_rejects_growth_after_the_captured_size(
    monkeypatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "baseline.zip"
    source.write_bytes(b"baseline")
    real_sha256 = sha256

    class _GrowingDigest:
        def __init__(self) -> None:
            self._delegate = real_sha256()
            self._grew = False

        def update(self, data: bytes) -> None:
            self._delegate.update(data)
            if not self._grew:
                self._grew = True
                with source.open("ab") as handle:
                    handle.write(b"+")

        def hexdigest(self) -> str:
            return self._delegate.hexdigest()

    monkeypatch.setattr(cli, "sha256", _GrowingDigest)

    with pytest.raises(ExistingProfileSemanticError, match="grew"):
        cli._regular_file_sha256(
            source,
            label="baseline archive",
            max_bytes=1024,
        )


def test_cli_rejects_a_loaded_snapshot_that_no_longer_matches_preflight_pin(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    monkeypatch.setattr(cli, "_verify_input_pins", lambda args: None)
    monkeypatch.setattr(
        cli,
        "compare_semantic_profile_corpora",
        lambda *args, **kwargs: {
            "baseline": {"archive_sha256": "f" * 64},
            "gold": {"freeze_manifest_sha256": cli.DEFAULT_GOLD_FREEZE_MANIFEST_SHA256},
            "candidate": {"archive_sha256": "e" * 64},
            "counts": {"selected": 1, "passed": 1, "failed": 0},
            "notices": [],
        },
    )
    written = False

    def write(*args, **kwargs):
        nonlocal written
        written = True
        return tmp_path / "report.json"

    monkeypatch.setattr(cli, "write_semantic_report", write)

    exit_code = cli.main(
        [
            "--baseline-zip", "baseline.zip",
            "--gold-root", "gold",
            "--input-zip", "input.zip",
            "--expected-input-sha256", cli.PRISTINE_HARD6_ARCHIVE_SHA256,
            "--candidate-zip", "candidate.zip",
            "--output-dir", str(tmp_path),
        ]
    )

    assert exit_code == 1
    assert written is False
    assert "loaded baseline identity" in capsys.readouterr().err


def test_cli_rejects_loaded_pristine_input_identity_swap() -> None:
    args = SimpleNamespace(
        expected_baseline_sha256="b" * 64,
        expected_gold_freeze_manifest_sha256="g" * 64,
        expected_input_sha256="i" * 64,
        expected_candidate_sha256=None,
    )

    with pytest.raises(ExistingProfileSemanticError, match="loaded input identity"):
        cli._verify_loaded_report_pins(
            args,
            {
                "baseline": {"archive_sha256": "b" * 64},
                "gold": {"freeze_manifest_sha256": "g" * 64},
                "input": {"archive_sha256": "wrong"},
            },
        )


def test_cli_returns_two_for_calibration_expectation_mismatch(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    monkeypatch.setattr(cli, "_verify_input_pins", lambda args: None)
    monkeypatch.setattr(cli, "_verify_loaded_report_pins", lambda args, report: None)
    monkeypatch.setattr(
        cli,
        "calibrate_semantic_profile_corpora",
        lambda *args, **kwargs: {
            "counts": {"selected": 100, "passed": 95, "failed": 5},
            "notices": [],
        },
    )
    monkeypatch.setattr(
        cli,
        "write_semantic_report",
        lambda *args, **kwargs: tmp_path / "existing-profile-semantic-bgic.v1.json",
    )

    exit_code = cli.main(
        [
            "--baseline-zip", "baseline.zip",
            "--gold-root", "gold",
            "--calibrate-baseline",
            "--output-dir", str(tmp_path),
        ]
    )

    assert exit_code == 2
    assert json.loads(capsys.readouterr().out)["status"] == "calibration_mismatch"


def test_cli_allows_zero_calibration_failures_without_notice_ids(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    monkeypatch.setattr(cli, "_verify_input_pins", lambda args: None)
    monkeypatch.setattr(cli, "_verify_loaded_report_pins", lambda args, report: None)
    monkeypatch.setattr(
        cli,
        "calibrate_semantic_profile_corpora",
        lambda *args, **kwargs: {
            "counts": {"selected": 100, "passed": 100, "failed": 0},
            "notices": [
                {"notice_id": f"PBLN_{index:015d}", "semantic_gate_status": "passed"}
                for index in range(100)
            ],
        },
    )
    monkeypatch.setattr(
        cli,
        "write_semantic_report",
        lambda *args, **kwargs: tmp_path / "existing-profile-semantic-bgic.v1.json",
    )

    exit_code = cli.main(
        [
            "--baseline-zip", "baseline.zip",
            "--gold-root", "gold",
            "--calibrate-baseline",
            "--output-dir", str(tmp_path),
            "--expected-calibration-passed", "100",
            "--expected-calibration-failed", "0",
        ]
    )

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out)["status"] == "calibration_matched"


def test_cli_rejects_nondefault_positive_calibration_failure_count_without_ids(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    monkeypatch.setattr(cli, "_verify_input_pins", lambda args: None)
    called = False

    def calibrate(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("invalid calibration expectations must fail before loading")

    monkeypatch.setattr(cli, "calibrate_semantic_profile_corpora", calibrate)

    exit_code = cli.main(
        [
            "--baseline-zip", "baseline.zip",
            "--gold-root", "gold",
            "--calibrate-baseline",
            "--output-dir", str(tmp_path),
            "--expected-calibration-failed", "1",
        ]
    )

    assert exit_code == 1
    assert called is False
    assert "non-default positive" in capsys.readouterr().err


def test_real_corpus_paths_skip_only_when_both_developer_paths_are_unset(monkeypatch) -> None:
    monkeypatch.delenv(REAL_BASELINE_ENV, raising=False)
    monkeypatch.delenv(REAL_GOLD_ENV, raising=False)
    monkeypatch.delenv(REQUIRE_REAL_GOLD100_ENV, raising=False)
    assert _configured_real_corpora_paths() is None

    monkeypatch.setenv(REAL_BASELINE_ENV, "/definitely/missing/baseline.zip")
    with pytest.raises(pytest.fail.Exception, match="set both"):
        _configured_real_corpora_paths()


def test_real_corpus_paths_fail_for_an_explicit_missing_path(monkeypatch, tmp_path: Path) -> None:
    baseline = tmp_path / "baseline.zip"
    baseline.write_bytes(b"baseline")
    monkeypatch.setenv(REAL_BASELINE_ENV, str(baseline))
    monkeypatch.setenv(REAL_GOLD_ENV, str(tmp_path / "missing-gold"))

    with pytest.raises(pytest.fail.Exception, match="explicitly configured Gold root is missing"):
        _configured_real_corpora_paths()
