"""Deterministically assemble a notice profile from validated source selection.

This module deliberately does not call an LLM.  It turns selected source blocks
into exact fact occurrences, preserving the source text as ``value_raw``.
"""

from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from typing import Any

from .profile_v02 import NUMERIC_CANDIDATE_EXTRACTOR_VERSION, validate_common_ir_lineage, validate_profile_v02
from .source_selection import SourceSelectionExtractionV02


class AssemblyError(ValueError):
    pass


def _source_document(metadata: dict[str, Any]) -> dict[str, Any]:
    common_ir = metadata.get("common_ir")
    if common_ir:
        # Validate the lineage mapping exactly as provided -- no key is
        # defaulted, dropped, or repaired here; an incomplete or malformed
        # mapping must fail rather than be silently normalized.
        validate_common_ir_lineage(common_ir)
        return {
            "document_name": None,
            "format": common_ir.get("source_kind"),
            "source_url": None,
            "notice_detail_url": None,
            "common_ir": common_ir,
        }
    primary = metadata.get("hwpx") or metadata.get("hwp") or {}
    return {
        "document_name": primary.get("name"),
        "format": primary.get("saved_as", "").rsplit(".", 1)[-1] or None,
        "source_url": primary.get("url"),
        "notice_detail_url": metadata.get("detail_url"),
    }


def assemble_final_profile(
    selection_artifact: dict[str, Any],
    metadata: dict[str, Any],
) -> dict[str, Any]:
    """Return the storage-facing final profile from server-materialized evidence.

    A fact is emitted once per cited source block.  This keeps ``value_raw`` an
    exact source value rather than concatenating or rewriting several blocks.
    """

    selection = selection_artifact.get("selection")
    evidence_rows = selection_artifact.get("materialized_evidence")
    if not isinstance(selection, dict) or not isinstance(evidence_rows, list):
        raise AssemblyError("selection artifact requires selection and materialized_evidence")
    notice_id = selection.get("notice_id")
    if not notice_id or notice_id != metadata.get("notice_id"):
        raise AssemblyError("selection notice_id and metadata notice_id must match")
    common_ir = metadata.get("common_ir")
    if common_ir:
        if selection_artifact.get("common_ir_identity") != {
            "document_id": common_ir.get("document_id"), "source_kind": common_ir.get("source_kind"),
            "source_sha256": common_ir.get("source_sha256"),
        }:
            raise AssemblyError("selection artifact and Common IR document identity must match exactly")

    def evidence_reference(source: dict[str, Any], *, include_text: bool = False) -> dict[str, Any]:
        """Preserve CandidatePack and Common IR locators as separate fields."""

        reference = {
            "source_block_id": source.get("source_block_id"),
            "section_id": source.get("section_id"),
            "source_occurrence_ids": source.get("source_occurrence_ids", []),
        }
        if include_text:
            reference["text"] = source.get("text")
        common_ir_document_id = source.get("common_ir_document_id")
        if common_ir:
            if common_ir_document_id != common_ir.get("document_id"):
                raise AssemblyError("materialized evidence Common IR document identity does not match metadata")
            common_ir_block_id = source.get("common_ir_block_id")
            if not common_ir_block_id:
                raise AssemblyError("materialized Common IR evidence requires common_ir_block_id")
            reference.update({
                "common_ir_document_id": common_ir_document_id,
                "common_ir_block_id": common_ir_block_id,
                "common_ir_occurrence_ids": source.get("common_ir_occurrence_ids", []),
            })
            if source.get("common_ir_cell_id") is not None:
                reference["common_ir_cell_id"] = source["common_ir_cell_id"]
        return reference

    components = selection.get("support_components", [])
    materialized_components = {
        item["support_component_id"]: item
        for item in selection_artifact.get("materialized_components", [])
        if item.get("support_component_id")
    }
    component_ids = {item["support_component_id"] for item in components}
    facts_by_scope: dict[str, list[dict[str, Any]]] = defaultdict(list)
    component_facts: dict[str, list[dict[str, Any]]] = defaultdict(list)
    unresolved_relations: list[dict[str, Any]] = []
    # A selected fact can materialize into several exact source occurrences.
    # Relationship ids are selected-fact ids at model time, then deterministically
    # expanded to the persisted occurrence ids here.
    occurrence_ids_by_selected_fact: dict[str, list[str]] = defaultdict(list)
    pending_relations: list[dict[str, Any]] = []

    for row in evidence_rows:
        component_id = row.get("primary_component_id") or row.get("support_component_id")
        if component_id is not None and component_id not in component_ids:
            raise AssemblyError(f"fact {row.get('fact_id')} references unknown component {component_id}")
        source_blocks = row.get("source_blocks") or []
        if not source_blocks:
            raise AssemblyError(f"fact {row.get('fact_id')} has no materialized evidence")
        for index, source in enumerate(source_blocks, start=1):
            block_id = source.get("source_block_id")
            raw_text = source.get("text")
            if not block_id or not raw_text:
                raise AssemblyError(f"fact {row.get('fact_id')} has an invalid source block")
            occurrence = {
                "fact_id": f"{row['fact_id']}#{index}",
                "field_name": row["field_name"],
                "value_raw": raw_text,
                "model_summary": None,
                "status": row["status"],
                "scope": "component" if component_id else "notice",
                "support_component_id": component_id,
                "applicability_component_ids": row.get("applicability_component_ids", []),
                "modifies_fact_ids": [],
                "recipient_fact_ids": [],
                "basis_fact_ids": [],
                "subject_role": row.get("subject_role"),
                "semantic_role": row.get("semantic_role"),
                "evidence": [evidence_reference(source)],
                "context_evidence": [
                    evidence_reference(context, include_text=True)
                    for context in row.get("context_blocks", [])
                ],
            }
            if row["field_name"] == "delivery_roles":
                occurrence["organization_names"] = row.get("organization_names", [])
                occurrence["role_raw"] = row.get("role_raw")
                occurrence["role_source_block_id"] = row.get("role_source_block_id")
                occurrence["canonical_role"] = row.get("canonical_role")
            if component_id:
                component_facts[component_id].append(occurrence)
            else:
                facts_by_scope[row["field_name"]].append(occurrence)
            occurrence_ids_by_selected_fact[row["fact_id"]].append(occurrence["fact_id"])
            pending_relations.append(
                {
                    "occurrence": occurrence,
                    "modifies_fact_ids": row.get("modifies_fact_ids", []),
                    "recipient_fact_ids": row.get("recipient_fact_ids", []),
                    "basis_fact_ids": row.get("basis_fact_ids", []),
                }
            )
            if row["status"] in {"unresolved", "needs_review"}:
                unresolved_relations.append(deepcopy(occurrence))

    for pending in pending_relations:
        occurrence = pending["occurrence"]
        for relation_key in ("modifies_fact_ids", "recipient_fact_ids", "basis_fact_ids"):
            occurrence[relation_key] = [
                occurrence_id
                for selected_fact_id in pending[relation_key]
                for occurrence_id in occurrence_ids_by_selected_fact[selected_fact_id]
            ]

    final_components = []
    for component in components:
        component_id = component["support_component_id"]
        final_components.append({
            "support_component_id": component_id,
            "component_kind": component["component_kind"],
            "name_raw": materialized_components.get(component_id, {}).get("name_raw"),
            "name_status": "identified" if materialized_components.get(component_id, {}).get("name_raw") else "not_extracted",
            "name_source_block_id": materialized_components.get(component_id, {}).get("name_source_block_id"),
            "source_block_ids": component["source_block_ids"],
            "table_block_ids": component.get("table_block_ids", []),
            "facts": component_facts[component_id],
        })

    source_document = _source_document(metadata)
    return {
        "schema_version": "existing_program_profile/v0.1",
        "notice_id": f"bizinfo:{notice_id}",
        "source_profile_id": f"bizinfo:{notice_id}:{source_document['format']}" if common_ir else None,
        "identity": {
            "title_raw": None if common_ir else source_document["document_name"],
            "title_source_block_ids": [],
            "notice_date_raw": None,
            "notice_date_source_block_ids": [],
            "source_url": source_document["notice_detail_url"],
        },
        "comparison_profile": dict(facts_by_scope),
        "support_components": final_components,
        "table_catalog": [],
        "unresolved_relations": unresolved_relations,
        "source_documents": [source_document],
        "assembly": {
            "source_selection_artifact": "server-materialized source selection",
            "value_raw_policy": "exact source block text; no LLM-written summaries",
            "table_catalog_status": "not_generated_in_this_selection_run",
            "component_name_status": "not_extracted_in_this_selection_run",
        },
    }


def assemble_final_profile_v02(
    selection_artifact: dict[str, Any],
    metadata: dict[str, Any],
    *,
    derived_projections: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Assemble the v0.2 storage profile from exact-span materialization only.

    v0.1 artifacts deliberately cannot be silently upgraded: every selected
    fact must have a server-materialized ``value_source``.
    """

    if selection_artifact.get("selection_contract") != "v0.2_anchor":
        raise AssemblyError("v0.2 assembly requires a v0.2_anchor selection artifact")
    try:
        SourceSelectionExtractionV02.model_validate(selection_artifact.get("selection"))
    except ValueError as error:
        raise AssemblyError(f"invalid v0.2 selection artifact: {error}") from error
    block_texts = selection_artifact.get("source_block_texts")
    if not isinstance(block_texts, dict) or not all(isinstance(text, str) for text in block_texts.values()):
        raise AssemblyError("v0.2 selection artifact requires source_block_texts for span validation")
    for row in selection_artifact.get("materialized_evidence", []):
        if row.get("value_source") is None:
            raise AssemblyError(
                f"v0.2 requires an exact value_source for fact {row.get('fact_id')}"
            )
    profile = assemble_final_profile(selection_artifact, metadata)
    value_source_by_fact = {
        row["fact_id"]: row["value_source"]
        for row in selection_artifact["materialized_evidence"]
    }
    evidence_by_fact = {
        row["fact_id"]: row for row in selection_artifact["materialized_evidence"]
    }

    old_to_new_ids: dict[str, str] = {}

    def apply_v02_provenance(occurrence: dict[str, Any]) -> None:
        selected_id = occurrence["fact_id"].rsplit("#", 1)[0]
        if selected_id in old_to_new_ids.values():
            raise AssemblyError(f"v0.2 selected fact materialized more than once: {selected_id}")
        old_to_new_ids[occurrence["fact_id"]] = selected_id
        occurrence["fact_id"] = selected_id
        occurrence["value_source"] = value_source_by_fact[selected_id]
        if occurrence["field_name"] == "delivery_roles":
            row = evidence_by_fact[selected_id]
            names = row.get("organization_names", [])
            sources = row.get("organization_sources", [])
            occurrence["organization_names"] = [
                {"value_raw": name, "value_source": source}
                for name, source in zip(names, sources, strict=True)
            ]
            occurrence["role_source"] = row.get("role_source")

    for rows in profile["comparison_profile"].values():
        for occurrence in rows:
            apply_v02_provenance(occurrence)
    for component in profile["support_components"]:
        for occurrence in component["facts"]:
            apply_v02_provenance(occurrence)
    for rows in profile["comparison_profile"].values():
        for occurrence in rows:
            for relation_key in ("modifies_fact_ids", "recipient_fact_ids", "basis_fact_ids"):
                occurrence[relation_key] = [old_to_new_ids[old] for old in occurrence[relation_key]]
    for component in profile["support_components"]:
        for occurrence in component["facts"]:
            for relation_key in ("modifies_fact_ids", "recipient_fact_ids", "basis_fact_ids"):
                occurrence[relation_key] = [old_to_new_ids[old] for old in occurrence[relation_key]]
    common_ir = metadata.get("common_ir")
    if common_ir and common_ir.get("document_id"):
        # v0.2's source_profile_id is derived from the Common IR document's
        # own identity, not from the v0.1 notice_id/format convention.
        profile["source_profile_id"] = common_ir["document_id"]
    profile["schema_version"] = "existing_program_profile/v0.2"
    profile["derived_projections"] = derived_projections or []
    profile["unresolved_observations"] = []
    profile["processing_metadata"] = {
        "source_selection_artifact": "server-materialized exact-span source selection",
        "value_raw_policy": "value_raw is server-recovered from one value_source span",
        "schema_version": "existing_program_profile/v0.2",
    }
    if common_ir:
        candidate_pack = selection_artifact.get("candidate_pack_lineage")
        required_pack_keys = {
            "candidate_pack_id",
            "candidate_pack_generator",
            "candidate_pack_generator_version",
            "common_ir_document_id",
            "common_ir_source_sha256",
            "text_basis",
        }
        if not isinstance(candidate_pack, dict) or any(not candidate_pack.get(key) for key in required_pack_keys):
            raise AssemblyError("Common IR profile requires complete CandidatePack lineage")
        if (
            candidate_pack["common_ir_document_id"] != common_ir["document_id"]
            or candidate_pack["common_ir_source_sha256"] != common_ir["source_sha256"]
            or candidate_pack["text_basis"] != "common_ir_v1_candidate_pack"
        ):
            raise AssemblyError("CandidatePack lineage does not match Common IR metadata")
        profile["processing_metadata"]["candidate_pack"] = candidate_pack
    if any(projection.get("projection_type") == "support_scale_measures" for projection in profile["derived_projections"]):
        profile["processing_metadata"]["derived_projection_producers"] = {
            "support_scale_measures": {"numeric_candidate_extractor_version": NUMERIC_CANDIDATE_EXTRACTOR_VERSION}
        }
    profile.pop("assembly", None)
    issues = validate_profile_v02(profile, block_texts)
    if issues:
        raise AssemblyError("v0.2 profile validation failed: " + "; ".join(issues))
    return profile
