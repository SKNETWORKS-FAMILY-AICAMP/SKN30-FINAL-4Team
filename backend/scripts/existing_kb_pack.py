#!/usr/bin/env python3
"""Validate or safely unpack an Existing KB ingestion data pack.

The validator encodes the exact assumptions made by
``ingest_existing_profile.py``.  It performs no network, database, Storage, or
OpenAI calls.  A ZIP is unpacked into a temporary directory for validation;
``--extract-to`` publishes it only after every record has passed validation.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
from tempfile import TemporaryDirectory, mkdtemp
from typing import Any, Iterable
from zipfile import BadZipFile, ZipFile, ZipInfo


PACK_SCHEMA = "prereview_existing_kb_pack/v0.1"
INGESTION_SCHEMA = "bizinfo_existing_ingestion_record/v0.1"
PROFILE_SCHEMA = "existing_program_profile/v0.2"
COMMON_IR_SCHEMA = "common_ir_v1"
ALLOWED_SOURCE_FORMATS = frozenset({"hwp", "hwpx", "pdf"})
ALLOWED_FACT_STATUSES = frozenset({"identified", "partial"})
ALLOWED_FACT_SCOPES = frozenset({"notice", "component"})
COMPARISON_FIELDS = frozenset(
    {
        "purpose_goal",
        "applicant_eligibility",
        "support_target",
        "eligibility_conditions",
        "beneficiary",
        "exclusions",
        "participation_requirements",
        "program_period",
        "support_period",
        "support_activities",
        "support_methods",
        "support_items",
        "support_content",
        "support_scale",
        "total_budget",
        "cost_sharing",
    }
)
EXISTING_SPECIFIC_FIELDS = frozenset(
    {
        "payment_terms",
        "duplicate_support_conditions",
        "applicable_entity",
        "delivery_roles",
    }
)
ALLOWED_PROJECTIONS = frozenset(
    {"support_facets", "support_scale_measures", "target_constraints"}
)
ALLOWED_CANONICAL_ROLES = frozenset(
    {
        "announcing_agency",
        "lead_agency",
        "operating_agency",
        "dedicated_agency",
        "participating_partner",
        "demand_partner",
        "cooperating_organization",
    }
)
MAX_ARCHIVE_MEMBERS = 10_000
MAX_UNCOMPRESSED_BYTES = 512 * 1024 * 1024
MAX_MEMBER_BYTES = 128 * 1024 * 1024
NOTICE_PATTERN = re.compile(r"^PBLN_[0-9]+$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
INGESTION_CONTRACT_PATH = (
    Path(__file__).resolve().parents[1]
    / "supabase"
    / "contracts"
    / "bizinfo_existing_ingestion_record.v0.1.schema.json"
)


class PackValidationError(ValueError):
    """The data pack cannot be consumed safely by the Existing importer."""


@dataclass(frozen=True, slots=True)
class PackSummary:
    schema_version: str
    status: str
    source: str
    notices: int
    files: int
    bytes: int
    source_formats: dict[str, int]
    facts: int
    delivery_roles: int
    projections: dict[str, int]
    relational_rows: dict[str, int]
    notice_ids_sha256: str
    archive_sha256: str | None = None


def _digest_path(path: Path) -> str:
    with path.open("rb") as stream:
        return _digest_stream(stream)


def _digest_stream(stream: Any) -> str:
    digest = sha256()
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(chunk)
    return digest.hexdigest()


def _json_object(path: Path) -> dict[str, Any]:
    def no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise PackValidationError(f"duplicate JSON key {key!r}: {path}")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise PackValidationError(f"non-finite JSON number {value!r}: {path}")

    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=no_duplicate_keys,
            parse_constant=reject_constant,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PackValidationError(f"cannot read JSON {path}: {error}") from error
    if not isinstance(value, dict):
        raise PackValidationError(f"JSON root must be an object: {path}")
    return value


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise PackValidationError(message)


def _validate_ingestion_contract(record: dict[str, Any], path: Path) -> None:
    try:
        from jsonschema import Draft202012Validator, FormatChecker
    except ImportError as error:
        raise PackValidationError(
            "jsonschema is required; run this with the backend environment"
        ) from error
    schema = _json_object(INGESTION_CONTRACT_PATH)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(record), key=lambda item: list(item.path))
    if errors:
        first = errors[0]
        location = ".".join(str(value) for value in first.absolute_path) or "$"
        raise PackValidationError(
            f"ingestion record schema violation at {location}: {first.message}: {path}"
        )


def _source_magic(path: Path, source_format: str) -> None:
    with path.open("rb") as stream:
        prefix = stream.read(8)
    expected = {
        "pdf": b"%PDF-",
        "hwp": bytes.fromhex("d0cf11e0a1b11ae1"),
        "hwpx": b"PK",
    }[source_format]
    _require(prefix.startswith(expected), f"source signature is not {source_format}: {path}")


def _facts(profile: dict[str, Any], notice_id: str) -> list[dict[str, Any]]:
    comparison = profile.get("comparison_profile")
    components = profile.get("support_components")
    _require(isinstance(comparison, dict), f"{notice_id}: comparison_profile must be an object")
    _require(isinstance(components, list), f"{notice_id}: support_components must be an array")
    result: list[dict[str, Any]] = []
    for field_name, values in comparison.items():
        _require(isinstance(values, list), f"{notice_id}: comparison_profile.{field_name} must be an array")
        for fact in values:
            _require(isinstance(fact, dict), f"{notice_id}: comparison fact must be an object")
            _require(fact.get("field_name") == field_name, f"{notice_id}: comparison fact field/container mismatch")
            result.append(fact)
    for component in components:
        _require(isinstance(component, dict), f"{notice_id}: support component must be an object")
        values = component.get("facts", [])
        _require(isinstance(values, list), f"{notice_id}: component facts must be an array")
        for fact in values:
            _require(isinstance(fact, dict), f"{notice_id}: component fact must be an object")
            result.append(fact)
    return result


def _validate_profile(
    profile: dict[str, Any], selection: dict[str, Any], notice_id: str
) -> tuple[int, int, Counter[str], Counter[str]]:
    _require(profile.get("schema_version") == PROFILE_SCHEMA, f"{notice_id}: unsupported Profile schema")
    _require(profile.get("notice_id") == notice_id, f"{notice_id}: Profile notice_id mismatch")

    components = profile.get("support_components")
    assert isinstance(components, list)
    component_ids = [component.get("support_component_id") for component in components]
    _require(all(isinstance(value, str) and value for value in component_ids), f"{notice_id}: invalid support_component_id")
    _require(len(set(component_ids)) == len(component_ids), f"{notice_id}: duplicate support_component_id")

    block_texts = selection.get("source_block_texts")
    _require(isinstance(block_texts, dict), f"{notice_id}: source_selection.source_block_texts must be an object")
    values = _facts(profile, notice_id)
    seen: set[str] = set()
    fact_by_id: dict[str, dict[str, Any]] = {}
    comparison_spans: set[tuple[str, int, int]] = set()
    delivery_roles = 0
    for fact in values:
        fact_id = fact.get("fact_id")
        _require(isinstance(fact_id, str) and fact_id, f"{notice_id}: missing fact_id")
        _require(fact_id not in seen, f"{notice_id}: duplicate fact_id {fact_id}")
        seen.add(fact_id)
        fact_by_id[fact_id] = fact
        _require(
            fact.get("field_name") in COMPARISON_FIELDS | EXISTING_SPECIFIC_FIELDS,
            f"{notice_id}/{fact_id}: unsupported field_name",
        )
        _require(fact.get("status") in ALLOWED_FACT_STATUSES, f"{notice_id}/{fact_id}: unsupported fact status")
        _require(fact.get("scope") in ALLOWED_FACT_SCOPES, f"{notice_id}/{fact_id}: unsupported fact scope")
        component_id = fact.get("support_component_id")
        if fact.get("scope") == "notice":
            _require(component_id is None, f"{notice_id}/{fact_id}: notice fact has a component owner")
        else:
            _require(component_id in component_ids, f"{notice_id}/{fact_id}: component owner is missing")
        source = fact.get("value_source")
        _require(isinstance(source, dict), f"{notice_id}/{fact_id}: value_source must be an object")
        block_id = source.get("source_block_id")
        start, end = source.get("start_char"), source.get("end_char")
        text = block_texts.get(block_id)
        _require(isinstance(text, str), f"{notice_id}/{fact_id}: source block is absent from candidate pack")
        _require(isinstance(start, int) and not isinstance(start, bool) and start >= 0, f"{notice_id}/{fact_id}: invalid start_char")
        _require(isinstance(end, int) and not isinstance(end, bool) and end > start, f"{notice_id}/{fact_id}: invalid end_char")
        _require(source.get("text_basis") == "common_ir_v1_candidate_pack", f"{notice_id}/{fact_id}: unsupported text_basis")
        _require(isinstance(fact.get("value_raw"), str), f"{notice_id}/{fact_id}: value_raw must be a string")
        _require(text[start:end] == fact["value_raw"], f"{notice_id}/{fact_id}: value_raw exact span mismatch")
        if fact.get("field_name") in COMPARISON_FIELDS:
            span = (str(block_id), start, end)
            _require(span not in comparison_spans, f"{notice_id}/{fact_id}: duplicate comparison exact span")
            comparison_spans.add(span)
        evidence = fact.get("evidence", [])
        contexts = fact.get("context_evidence", [])
        _require(isinstance(evidence, list), f"{notice_id}/{fact_id}: evidence must be an array")
        _require(isinstance(contexts, list), f"{notice_id}/{fact_id}: context_evidence must be an array")
        for item in evidence:
            _require(
                isinstance(item, dict)
                and isinstance(item.get("source_block_id"), str)
                and isinstance(item.get("common_ir_document_id"), str)
                and isinstance(item.get("common_ir_block_id"), str),
                f"{notice_id}/{fact_id}: invalid evidence",
            )
        for item in contexts:
            _require(
                isinstance(item, dict)
                and isinstance(item.get("source_block_id"), str),
                f"{notice_id}/{fact_id}: invalid context evidence",
            )
        for relation_key in ("modifies_fact_ids", "recipient_fact_ids", "basis_fact_ids"):
            relation_ids = fact.get(relation_key, [])
            _require(isinstance(relation_ids, list), f"{notice_id}/{fact_id}: {relation_key} must be an array")
        applicability = fact.get("applicability_component_ids", [])
        _require(isinstance(applicability, list), f"{notice_id}/{fact_id}: applicability_component_ids must be an array")
        _require(len(applicability) == len(set(applicability)), f"{notice_id}/{fact_id}: duplicate applicability component")
        _require(all(value in component_ids for value in applicability), f"{notice_id}/{fact_id}: unknown applicability component")
        if fact.get("field_name") == "delivery_roles":
            delivery_roles += 1
            _require(
                fact.get("canonical_role") is None
                or fact.get("canonical_role") in ALLOWED_CANONICAL_ROLES,
                f"{notice_id}/{fact_id}: unsupported canonical delivery role",
            )
            organizations = fact.get("organization_names")
            _require(isinstance(organizations, list) and organizations, f"{notice_id}/{fact_id}: delivery role has no organization")
            for organization in organizations:
                _require(isinstance(organization, dict), f"{notice_id}/{fact_id}: invalid organization occurrence")
                organization_source = organization.get("value_source")
                _require(isinstance(organization_source, dict), f"{notice_id}/{fact_id}: organization value_source missing")
                org_block = block_texts.get(organization_source.get("source_block_id"))
                org_start = organization_source.get("start_char")
                org_end = organization_source.get("end_char")
                _require(isinstance(org_block, str), f"{notice_id}/{fact_id}: organization source block missing")
                _require(
                    isinstance(org_start, int)
                    and not isinstance(org_start, bool)
                    and isinstance(org_end, int)
                    and not isinstance(org_end, bool)
                    and org_end > org_start >= 0,
                    f"{notice_id}/{fact_id}: invalid organization span",
                )
                _require(organization_source.get("text_basis") == "common_ir_v1_candidate_pack", f"{notice_id}/{fact_id}: invalid organization text_basis")
                _require(org_block[org_start:org_end] == organization.get("value_raw"), f"{notice_id}/{fact_id}: organization exact span mismatch")

    for fact in values:
        fact_id = str(fact["fact_id"])
        for relation_key in ("modifies_fact_ids", "recipient_fact_ids", "basis_fact_ids"):
            targets = fact.get(relation_key, [])
            _require(all(target in seen for target in targets), f"{notice_id}/{fact_id}: unknown fact relation target")
            _require(fact_id not in targets, f"{notice_id}/{fact_id}: self fact relation")
            _require(len(targets) == len(set(targets)), f"{notice_id}/{fact_id}: duplicate fact relation")

    projection_counts: Counter[str] = Counter()
    projections = profile.get("derived_projections")
    _require(isinstance(projections, list), f"{notice_id}: derived_projections must be an array")
    for projection in projections:
        _require(isinstance(projection, dict), f"{notice_id}: projection must be an object")
        projection_type = projection.get("projection_type")
        _require(projection_type in ALLOWED_PROJECTIONS, f"{notice_id}: unsupported projection {projection_type!r}")
        _require(isinstance(projection.get("status"), str) and projection["status"], f"{notice_id}: projection status is required")
        projection_counts[str(projection_type)] += 1
        source_keys: Iterable[str]
        if projection_type == "target_constraints":
            source_keys = ("positive_source_fact_ids", "exclusion_source_fact_ids")
        else:
            source_keys = ("source_fact_ids",)
        for key in source_keys:
            source_ids = projection.get(key, [])
            _require(isinstance(source_ids, list) and all(value in seen for value in source_ids), f"{notice_id}: invalid {projection_type}.{key}")
            _require(
                len(source_ids) == len(set(source_ids)),
                f"{notice_id}: duplicate {projection_type}.{key}",
            )
        if projection_type == "target_constraints":
            positive = projection.get("positive_source_fact_ids", [])
            exclusions = projection.get("exclusion_source_fact_ids", [])
            _require(not set(positive) & set(exclusions), f"{notice_id}: target source roles overlap")
            _require(
                all(fact_by_id[fact_id].get("field_name") != "beneficiary" for fact_id in positive + exclusions),
                f"{notice_id}: beneficiary cannot directly source target_constraints",
            )
            for dimension_key in ("entity_types", "regions", "industries"):
                dimension_values = projection.get(dimension_key, [])
                _require(
                    isinstance(dimension_values, list)
                    and all(isinstance(value, str) and value for value in dimension_values),
                    f"{notice_id}: invalid target dimension {dimension_key}",
                )
            _require(
                projection.get("business_age") is None,
                f"{notice_id}: target_constraints.business_age is not supported by this importer",
            )
        if projection_type == "support_scale_measures":
            measures = projection.get("measures", [])
            _require(isinstance(measures, list), f"{notice_id}: support scale measures must be an array")
            _require(all(isinstance(item, dict) and item.get("source_fact_id") in seen for item in measures), f"{notice_id}: support scale source fact is invalid")
    relational_rows: Counter[str] = Counter(
        {
            "support_components": len(components),
            "facts": len(values),
            "fact_evidence": sum(len(fact.get("evidence", [])) for fact in values),
            "fact_context": sum(len(fact.get("context_evidence", [])) for fact in values),
            "fact_relations": sum(
                len(fact.get(key, []))
                for fact in values
                for key in (
                    "modifies_fact_ids",
                    "recipient_fact_ids",
                    "basis_fact_ids",
                )
            ),
            "fact_component_links": sum(
                len(fact.get("applicability_component_ids", [])) for fact in values
            ),
            "delivery_roles": delivery_roles,
            "delivery_role_organizations": sum(
                len(fact.get("organization_names", []))
                for fact in values
                if fact.get("field_name") == "delivery_roles"
            ),
            "support_facets": projection_counts["support_facets"],
            "support_facet_sources": sum(
                len(projection.get("source_fact_ids", []))
                for projection in projections
                if projection["projection_type"] == "support_facets"
            ),
            "support_facet_values": sum(
                sum(
                    len(projection.get(key, []))
                    for key in ("activities", "methods", "items")
                )
                for projection in projections
                if projection["projection_type"] == "support_facets"
            ),
            "support_scale_projections": projection_counts[
                "support_scale_measures"
            ],
            "support_scale_measures": sum(
                len(projection.get("measures", []))
                for projection in projections
                if projection["projection_type"] == "support_scale_measures"
            ),
            "target_constraints": projection_counts["target_constraints"],
            "target_constraint_sources": sum(
                len(projection.get(key, []))
                for projection in projections
                if projection["projection_type"] == "target_constraints"
                for key in (
                    "positive_source_fact_ids",
                    "exclusion_source_fact_ids",
                )
            ),
            "target_constraint_dimensions": sum(
                len(projection.get(key, []))
                for projection in projections
                if projection["projection_type"] == "target_constraints"
                for key in ("entity_types", "regions", "industries")
            ),
        }
    )
    _require(
        projection_counts["target_constraints"] <= 1,
        f"{notice_id}: at most one target_constraints projection is allowed",
    )
    _require(
        projection_counts["support_scale_measures"] <= 1,
        f"{notice_id}: at most one support_scale_measures projection is allowed",
    )
    return len(values), delivery_roles, projection_counts, relational_rows


def _validate_notice(
    directory: Path,
) -> tuple[str, int, int, Counter[str], Counter[str]]:
    notice_id = directory.name
    _require(NOTICE_PATTERN.fullmatch(notice_id) is not None, f"invalid notice directory: {notice_id}")
    _require(not directory.is_symlink(), f"notice directory must not be a symlink: {directory}")
    metadata_path = directory / "metadata.json"
    record_path = directory / "pipeline" / "ingestion_record.v0.1.json"
    profile_path = directory / "pipeline" / "structured_profile.v0.2.json"
    selection_path = directory / "pipeline" / "source_selection.json"
    attachments = sorted((directory / "attachments").glob("*"))
    common_irs = sorted((directory / "pipeline" / "common_ir_v1").glob("*.json"))
    _require(len(attachments) == 1 and attachments[0].is_file(), f"{notice_id}: exactly one source attachment is required")
    _require(len(common_irs) == 1 and common_irs[0].is_file(), f"{notice_id}: exactly one Common IR is required")
    for path in (metadata_path, record_path, profile_path, selection_path):
        _require(path.is_file() and not path.is_symlink(), f"{notice_id}: required file missing: {path.relative_to(directory)}")

    metadata = _json_object(metadata_path)
    record = _json_object(record_path)
    profile = _json_object(profile_path)
    selection = _json_object(selection_path)
    common_ir = _json_object(common_irs[0])
    _validate_ingestion_contract(record, record_path)
    _require(record.get("schema_version") == INGESTION_SCHEMA, f"{notice_id}: unsupported ingestion schema")
    _require(record.get("portal_metadata") == metadata, f"{notice_id}: embedded portal_metadata differs from metadata.json")
    _require(record.get("structured_profile") == profile, f"{notice_id}: embedded Profile differs from structured_profile.v0.2.json")
    _require(metadata.get("notice_id") == notice_id and metadata.get("pblanc_id") == notice_id, f"{notice_id}: metadata identity mismatch")
    for field in ("title", "apply_period", "ministry", "executing_agency", "registered_at", "detail_url"):
        _require(isinstance(metadata.get(field), str) and metadata[field].strip(), f"{notice_id}: metadata.{field} is required")

    documents = profile.get("source_documents")
    _require(isinstance(documents, list) and len(documents) == 1 and isinstance(documents[0], dict), f"{notice_id}: exactly one source document is required")
    document = documents[0]
    source_format = str(document.get("format") or "").lower()
    _require(source_format in ALLOWED_SOURCE_FORMATS, f"{notice_id}: unsupported source format {source_format!r}")
    _require(attachments[0].suffix.lower() == f".{source_format}", f"{notice_id}: attachment extension/format mismatch")
    _source_magic(attachments[0], source_format)
    source_profile_id = f"{source_format}:{notice_id}"
    _require(profile.get("source_profile_id") == source_profile_id, f"{notice_id}: source_profile_id mismatch")

    expected_common_ir_name = f"{notice_id}.{source_format}.json"
    _require(common_irs[0].name == expected_common_ir_name, f"{notice_id}: Common IR filename mismatch")
    analysis = record.get("analysis")
    _require(isinstance(analysis, dict), f"{notice_id}: analysis must be an object")
    _require(analysis.get("input_path") == attachments[0].name, f"{notice_id}: analysis.input_path mismatch")
    _require(analysis.get("common_ir_path") == f"pipeline/common_ir_v1/{expected_common_ir_name}", f"{notice_id}: analysis.common_ir_path must be pack-relative")

    source_hash = _digest_path(attachments[0])
    common_ref = document.get("common_ir")
    _require(isinstance(common_ref, dict), f"{notice_id}: Profile Common IR reference missing")
    document_id = source_profile_id
    _require(common_ref.get("document_id") == document_id, f"{notice_id}: Profile Common IR document_id mismatch")
    _require(common_ref.get("source_sha256") == source_hash, f"{notice_id}: Profile source SHA-256 mismatch")
    _require(common_ref.get("schema_version") == COMMON_IR_SCHEMA, f"{notice_id}: Profile Common IR schema mismatch")
    _require(common_ref.get("source_kind") == source_format, f"{notice_id}: Profile source kind mismatch")
    _require(common_ir.get("schema_version") == COMMON_IR_SCHEMA, f"{notice_id}: Common IR schema mismatch")
    common_document = common_ir.get("document")
    _require(isinstance(common_document, dict), f"{notice_id}: Common IR document missing")
    provenance = common_document.get("provenance")
    _require(isinstance(provenance, dict), f"{notice_id}: Common IR provenance missing")
    _require(common_document.get("document_id") == document_id, f"{notice_id}: Common IR document_id mismatch")
    _require(common_document.get("source_kind") == source_format, f"{notice_id}: Common IR source kind mismatch")
    _require(provenance.get("source_sha256") == source_hash, f"{notice_id}: Common IR source SHA-256 mismatch")

    selection_identity = selection.get("common_ir_identity")
    _require(isinstance(selection_identity, dict), f"{notice_id}: source selection identity missing")
    for key, expected in (("document_id", document_id), ("source_kind", source_format), ("source_sha256", source_hash)):
        _require(selection_identity.get(key) == expected, f"{notice_id}: source selection {key} mismatch")
    profile_pack = (profile.get("processing_metadata") or {}).get("candidate_pack")
    selection_pack = selection.get("candidate_pack_lineage")
    _require(isinstance(profile_pack, dict) and isinstance(selection_pack, dict), f"{notice_id}: candidate-pack lineage missing")
    _require(profile_pack.get("candidate_pack_id") == selection_pack.get("candidate_pack_id"), f"{notice_id}: candidate_pack_id mismatch")
    _require(profile_pack.get("common_ir_document_id") == document_id, f"{notice_id}: Profile candidate-pack Common IR mismatch")
    _require(profile_pack.get("common_ir_source_sha256") == source_hash, f"{notice_id}: Profile candidate-pack SHA-256 mismatch")

    facts, delivery_roles, projections, relational_rows = _validate_profile(
        profile, selection, notice_id
    )
    expected_paths = {
        "metadata.json",
        f"attachments/{attachments[0].name}",
        "pipeline/ingestion_record.v0.1.json",
        "pipeline/source_selection.json",
        "pipeline/structured_profile.v0.2.json",
        f"pipeline/common_ir_v1/{expected_common_ir_name}",
    }
    actual_paths = {
        path.relative_to(directory).as_posix()
        for path in directory.rglob("*")
        if path.is_file()
    }
    _require(actual_paths == expected_paths, f"{notice_id}: unexpected pack files: missing={sorted(expected_paths-actual_paths)!r}, extra={sorted(actual_paths-expected_paths)!r}")
    return source_format, facts, delivery_roles, projections, relational_rows


def validate_notice_directory(directory: Path) -> None:
    """Validate one notice directory against the single-record importer."""

    supplied = directory.expanduser()
    _require(not supplied.is_symlink(), f"notice directory must not be a symlink: {supplied}")
    resolved = supplied.resolve()
    _require(resolved.is_dir(), f"notice directory does not exist: {resolved}")
    _validate_tree_safety(resolved, label="notice directory")
    _validate_notice(resolved)


def _validate_tree_safety(root: Path, *, label: str) -> tuple[list[Path], list[Path]]:
    """Reject links/special files and bound work before parsing any payload."""

    try:
        entries = list(root.rglob("*"))
        _require(
            len(entries) <= MAX_ARCHIVE_MEMBERS,
            f"{label} has too many entries: {len(entries)}",
        )
        for path in entries:
            _require(not path.is_symlink(), f"{label} must not contain symlinks: {path}")
            _require(
                path.is_file() or path.is_dir(),
                f"{label} contains a special filesystem entry: {path}",
            )
        files = [path for path in entries if path.is_file()]
        sizes = [path.stat().st_size for path in files]
    except OSError as error:
        raise PackValidationError(f"cannot inspect {label}: {error}") from error
    _require(
        all(size <= MAX_MEMBER_BYTES for size in sizes),
        f"{label} contains a file exceeding the per-file safety limit",
    )
    _require(
        sum(sizes) <= MAX_UNCOMPRESSED_BYTES,
        f"{label} size exceeds safety limit",
    )
    return entries, files


def validate_directory(root: Path, *, expected_count: int | None = None) -> PackSummary:
    supplied_root = root.expanduser()
    _require(not supplied_root.is_symlink(), f"pack root must not be a symlink: {supplied_root}")
    root = supplied_root.resolve()
    _require(root.is_dir(), f"pack root is not a regular directory: {root}")
    entries, files = _validate_tree_safety(root, label="pack")
    notice_dirs = sorted(path for path in root.iterdir() if path.is_dir())
    other_root_entries = sorted(path.name for path in root.iterdir() if not path.is_dir())
    _require(not other_root_entries, f"pack root contains non-notice entries: {other_root_entries!r}")
    _require(bool(notice_dirs), f"pack contains no notice directories: {root}")
    if expected_count is not None:
        _require(len(notice_dirs) == expected_count, f"expected {expected_count} notices, got {len(notice_dirs)}")

    formats: Counter[str] = Counter()
    projections: Counter[str] = Counter()
    relational_rows: Counter[str] = Counter()
    facts = delivery_roles = 0
    for directory in notice_dirs:
        (
            source_format,
            fact_count,
            role_count,
            projection_counts,
            notice_relational_rows,
        ) = _validate_notice(directory)
        formats[source_format] += 1
        facts += fact_count
        delivery_roles += role_count
        projections.update(projection_counts)
        relational_rows.update(notice_relational_rows)
    notice_digest = sha256(("\n".join(path.name for path in notice_dirs) + "\n").encode()).hexdigest()
    return PackSummary(
        schema_version=PACK_SCHEMA,
        status="valid",
        source=str(root),
        notices=len(notice_dirs),
        files=len(files),
        bytes=sum(path.stat().st_size for path in files),
        source_formats=dict(sorted(formats.items())),
        facts=facts,
        delivery_roles=delivery_roles,
        projections=dict(sorted(projections.items())),
        relational_rows=dict(sorted(relational_rows.items())),
        notice_ids_sha256=notice_digest,
    )


def _safe_member_path(info: ZipInfo) -> PurePosixPath:
    path = PurePosixPath(info.filename)
    _require(not path.is_absolute() and path.parts, f"unsafe absolute ZIP member: {info.filename!r}")
    _require(".." not in path.parts and "." not in path.parts, f"unsafe ZIP member path: {info.filename!r}")
    _require("\\" not in info.filename and "\x00" not in info.filename, f"unsafe ZIP member name: {info.filename!r}")
    mode = info.external_attr >> 16
    _require(not stat.S_ISLNK(mode), f"ZIP symlink is not allowed: {info.filename!r}")
    _require(not mode or stat.S_ISREG(mode) or stat.S_ISDIR(mode), f"special ZIP member is not allowed: {info.filename!r}")
    return path


def _validate_archive_index(archive: ZipFile) -> None:
    infos = archive.infolist()
    _require(len(infos) <= MAX_ARCHIVE_MEMBERS, f"ZIP has too many members: {len(infos)}")
    _require(sum(info.file_size for info in infos) <= MAX_UNCOMPRESSED_BYTES, "ZIP uncompressed size exceeds safety limit")
    seen: set[str] = set()
    file_paths: set[PurePosixPath] = set()
    for info in infos:
        path = _safe_member_path(info)
        _require(info.file_size <= MAX_MEMBER_BYTES, f"ZIP member exceeds safety limit: {info.filename!r}")
        normalized = path.as_posix().rstrip("/")
        _require(normalized not in seen, f"duplicate ZIP member: {normalized!r}")
        seen.add(normalized)
        if not info.is_dir():
            file_paths.add(PurePosixPath(normalized))
    for path in file_paths:
        _require(
            all(parent not in file_paths for parent in path.parents if parent != PurePosixPath(".")),
            f"ZIP file/directory path collision: {path.as_posix()!r}",
        )


def _extract_archive(archive: ZipFile, destination: Path) -> None:
    _validate_archive_index(archive)
    destination.mkdir(parents=True, exist_ok=False)
    for info in archive.infolist():
        relative = _safe_member_path(info)
        output = destination.joinpath(*relative.parts)
        if info.is_dir():
            output.mkdir(parents=True, exist_ok=True)
            continue
        output.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(info) as source, output.open("xb") as target:
            shutil.copyfileobj(source, target, length=1024 * 1024)


def validate_pack(
    path: Path,
    *,
    expected_count: int | None = None,
    manifest_path: Path | None = None,
) -> PackSummary:
    supplied_source = path.expanduser()
    _require(not supplied_source.is_symlink(), f"pack source must not be a symlink: {supplied_source}")
    source = supplied_source.resolve()
    if source.is_dir():
        summary = validate_directory(source, expected_count=expected_count)
        if manifest_path:
            _verify_manifest(summary, manifest_path, source)
        return summary
    _require(source.is_file(), f"pack does not exist: {source}")
    try:
        with source.open("rb") as stream:
            archive_size = os.fstat(stream.fileno()).st_size
            archive_hash = _digest_stream(stream)
            stream.seek(0)
            with ZipFile(stream) as archive, TemporaryDirectory(prefix="existing-kb-validate-") as temporary:
                root = Path(temporary) / "pack"
                _extract_archive(archive, root)
                summary = validate_directory(root, expected_count=expected_count)
    except (BadZipFile, OSError) as error:
        raise PackValidationError(f"invalid ZIP archive: {source}: {error}") from error
    result = PackSummary(
        **{
            **asdict(summary),
            "source": str(source),
            "archive_sha256": archive_hash,
        }
    )
    if manifest_path:
        _verify_manifest(
            result,
            manifest_path,
            source,
            archive_size=archive_size,
        )
    return result


def extract_validated_pack(
    path: Path,
    destination: Path,
    *,
    expected_count: int | None = None,
    manifest_path: Path | None = None,
) -> PackSummary:
    supplied_source = path.expanduser()
    _require(not supplied_source.is_symlink(), f"pack source must not be a symlink: {supplied_source}")
    source = supplied_source.resolve()
    supplied_target = destination.expanduser()
    _require(not supplied_target.is_symlink(), f"extraction target must not be a symlink: {supplied_target}")
    target = supplied_target.resolve(strict=False)
    _require(source.is_file(), f"ZIP pack does not exist: {source}")
    _require(target.is_absolute(), "--extract-to must be an absolute path")
    _require(not target.exists(), f"refusing to overwrite existing extraction target: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    staging_root = Path(mkdtemp(prefix=".existing-kb-stage-", dir=target.parent))
    staged_pack = staging_root / "pack"
    try:
        with source.open("rb") as stream:
            archive_size = os.fstat(stream.fileno()).st_size
            archive_hash = _digest_stream(stream)
            stream.seek(0)
            with ZipFile(stream) as archive:
                _extract_archive(archive, staged_pack)
        summary = validate_directory(staged_pack, expected_count=expected_count)
        published_summary = PackSummary(
            **{
                **asdict(summary),
                "source": str(target),
                "archive_sha256": archive_hash,
            }
        )
        if manifest_path:
            _verify_manifest(
                published_summary,
                manifest_path,
                source,
                archive_size=archive_size,
            )
        os.replace(staged_pack, target)
    except (BadZipFile, OSError) as error:
        raise PackValidationError(f"invalid ZIP archive: {source}: {error}") from error
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)
    return published_summary


def _verify_manifest(
    summary: PackSummary,
    manifest_path: Path,
    pack_path: Path,
    *,
    archive_size: int | None = None,
) -> None:
    manifest = _json_object(manifest_path.resolve())
    _require(manifest.get("schema_version") == "prereview_existing_kb_pack_manifest/v0.1", "unsupported data-pack manifest")
    archive = manifest.get("archive")
    expected = manifest.get("expected")
    _require(isinstance(archive, dict) and isinstance(expected, dict), "invalid data-pack manifest shape")
    if summary.archive_sha256 is not None:
        _require(pack_path.name == archive.get("file_name"), "archive filename differs from manifest")
        _require(summary.archive_sha256 == archive.get("sha256"), "archive SHA-256 differs from manifest")
        _require(
            archive_size is not None,
            "captured archive size is required for manifest verification",
        )
        _require(archive_size == archive.get("size_bytes"), "archive size differs from manifest")
    checks = {
        "notices": summary.notices,
        "files": summary.files,
        "bytes": summary.bytes,
        "source_formats": summary.source_formats,
        "facts": summary.facts,
        "delivery_roles": summary.delivery_roles,
        "projections": summary.projections,
        "relational_rows": summary.relational_rows,
        "notice_ids_sha256": summary.notice_ids_sha256,
    }
    for key, actual in checks.items():
        _require(expected.get(key) == actual, f"manifest mismatch for expected.{key}: expected={expected.get(key)!r}, actual={actual!r}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pack", type=Path, help="ZIP 또는 이미 추출된 공고 루트")
    parser.add_argument("--expected-count", type=int, help="예상 공고 수(100건 팩은 100)")
    parser.add_argument("--manifest", type=Path, help="고정 데이터셋 checksum/count manifest")
    parser.add_argument("--extract-to", type=Path, help="검증 성공 후 ZIP을 이 새 절대경로에 원자적으로 배치")
    args = parser.parse_args()
    if args.expected_count is not None and args.expected_count < 1:
        parser.error("--expected-count must be positive")
    if args.extract_to and not args.pack.is_file():
        parser.error("--extract-to is available only for a ZIP file")
    try:
        summary = (
            extract_validated_pack(
                args.pack,
                args.extract_to,
                expected_count=args.expected_count,
                manifest_path=args.manifest,
            )
            if args.extract_to
            else validate_pack(
                args.pack,
                expected_count=args.expected_count,
                manifest_path=args.manifest,
            )
        )
    except PackValidationError as error:
        print(json.dumps({"status": "invalid", "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(asdict(summary), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
