"""Produce an Existing Profile v0.2 from a Common IR v1 document.

The profile_structuring package owns the artifact contracts, prompts, exact
span materialization, and final validation.  This module only supplies the
three LLM calls through the backend port and composes those existing steps in
memory; it never writes a model-authored value into the profile.
"""

from __future__ import annotations

import ast
from functools import cache
import hashlib
import re
from pathlib import Path
from typing import Any

from app.ports.llm_client import (
    LLMInvalidResponseError,
    LLMTimeoutError,
    LLMUnavailableError,
    LLMClient,
)

from . import vendor
from .contracts.profile_snapshot import (
    CANDIDATE_PACK_EMPTY,
    COMMON_IR_INVALID,
    LLM_INVALID_RESPONSE,
    LLM_TIMEOUT,
    LLM_UNAVAILABLE,
    MATERIALIZATION_FAILED,
    REPAIR_BUDGET_EXHAUSTED,
    StageDiagnostic,
)
from .llm_call import generate
from .profiles import StageError

from semantic_structuring.anchor_occurrence_resolver import SHA256_HEX_PATTERN  # noqa: E402
from semantic_structuring.candidate_assembly import build_routed_a_pack  # noqa: E402
from semantic_structuring.common_ir_v1 import (  # noqa: E402
    _is_main_notice_section,
    _v1_attachment_input,
    apply_common_ir_v1_section_scopes,
    common_ir_v1_identity,
    common_ir_v1_metadata,
    prepare_common_ir_v1,
)
from semantic_structuring.final_profile_assembler import (  # noqa: E402
    assemble_final_profile_v02,
)
from semantic_structuring.notice_preparation import (  # noqa: E402
    SectionScopeDecision,
    SectionScopeDiscovery,
)
from semantic_structuring.request_profile_v012 import candidate_pack_artifact  # noqa: E402
from semantic_structuring.run_block_candidate_discovery_test import (  # noqa: E402
    BlockCandidateDiscovery,
    CandidateRoute,
    ROUTER_CONTRACT_VERSION,
)
from semantic_structuring.source_selection import (  # noqa: E402
    AnchorCorrectionRequest,
    AnchorCorrectionResponse,
    AmbiguousAnchorCorrectionError,
    CorrectionResolverError,
    SourceSelectionExtractionV02,
    apply_finalize_with_fallback_v02,
    build_corrected_anchor_audit,
    build_numeric_candidates,
    classify_empty_repair_response_v02,
    derive_support_scale_measures_v02,
    materialize_components,
    materialize_evidence,
    memoize_anchor_correction_resolver,
    normalize_explicit_condition_variant_relations_v02,
    normalize_explicit_sequential_components_v02,
    normalize_nested_support_scale_anchors_v02,
    preserve_prior_server_validated_facts_v02,
    validate_component_structure_v02,
    validate_scale_measure_candidates_v02,
    validate_selection_quality_v02,
    validate_support_cap_completeness_v02,
)


SECTION_SCOPE_TASK = "announcement_section_scope_v1"
BLOCK_ROUTER_TASK = "announcement_block_router_v03"
SOURCE_SELECTION_TASK = "announcement_source_selection_v02"
ANCHOR_CORRECTION_TASK = "announcement_anchor_correction_v1"


_PROMPT_SHA256 = {
    ("semantic_structuring.common_ir_v1", "classify_common_ir_v1_attachment_scopes"):
        "55e40c4e9297556d3cfea7ce3c26652f386ce3fb5bb834ff34e7e396b4c39396",
    ("semantic_structuring.run_block_candidate_discovery_test", "main"):
        "9ff379ff0a6f2a1f4b152bcbf034b4d9e53ef44fab8aa1331dcab86230a676e9",
    ("semantic_structuring.run_source_selection_test", "main"):
        "dd9c1e030e83e1339d2f626e07e5b10300caa920a3f2bdde86fbaf54bbd7d0c3",
}


def _prompt_literal(node: ast.AST) -> str:
    """Evaluate the team's literal prompt, including its router version f-string."""

    if isinstance(node, ast.Constant):
        if isinstance(node.value, str):
            return node.value
        raise ValueError("prompt literal is not text")
    if isinstance(node, ast.JoinedStr):
        values: list[str] = []
        for item in node.values:
            if isinstance(item, ast.Constant) and isinstance(item.value, str):
                values.append(item.value)
                continue
            if (
                isinstance(item, ast.FormattedValue)
                and isinstance(item.value, ast.Name)
                and item.value.id == "ROUTER_CONTRACT_VERSION"
            ):
                values.append(ROUTER_CONTRACT_VERSION)
                continue
            raise ValueError("prompt contains an unsupported formatted value")
        return "".join(values)
    value = ast.literal_eval(node)
    if not isinstance(value, str):
        raise ValueError("prompt literal is not text")
    return value


def _vendor_prompt(relative_path: str, function_name: str) -> str:
    """Read the prompt expression used by the team's runner verbatim.

    The vendored runners keep their prompt in the call-site, rather than an
    exported function.  Evaluating that literal avoids a second, inevitably
    drifting prompt copy while leaving ``packages/**`` untouched.
    """

    relative = Path(*relative_path.split("."))
    relative = relative.with_suffix(".py")
    source_path = next(
        (base / relative for base in vendor.VENDOR_PATHS if (base / relative).is_file()),
        None,
    )
    if source_path is None:
        raise RuntimeError(f"vendored prompt source is missing: {relative_path}")
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    function = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == function_name
        ),
        None,
    )
    if function is None:
        raise RuntimeError(f"vendored prompt function is missing: {function_name}")
    assignment = next(
        (
            node
            for node in ast.walk(function)
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "instructions" for target in node.targets)
        ),
        None,
    )
    if assignment is None:
        raise RuntimeError(f"vendored prompt assignment is missing: {function_name}")
    value = _prompt_literal(assignment.value)
    expected_sha256 = _PROMPT_SHA256.get((relative_path, function_name))
    if expected_sha256 is None or hashlib.sha256(value.encode("utf-8")).hexdigest() != expected_sha256:
        raise RuntimeError(
            f"vendored prompt changed without a backend contract update: {relative_path}"
        )
    if not value:
        raise RuntimeError(f"vendored prompt is empty: {function_name}")
    return value


@cache
def _scope_instructions() -> str:
    return _vendor_prompt(
        "semantic_structuring.common_ir_v1", "classify_common_ir_v1_attachment_scopes"
    )


@cache
def _router_instructions() -> str:
    return _vendor_prompt(
        "semantic_structuring.run_block_candidate_discovery_test", "main"
    )


@cache
def _source_selection_instructions() -> str:
    return _vendor_prompt(
        "semantic_structuring.run_source_selection_test", "main"
    )
_ANCHOR_CORRECTION_INSTRUCTIONS = (
    "The requested value_anchor is repeated more than once in its source block. "
    "Choose the one candidate_id whose context_before/context_after matches the "
    "field this anchor was selected for. Decide only from anchor_text and context; "
    "no position or occurrence order is given."
)
_REPAIR_INSTRUCTIONS = (
    " This is a repair attempt. The user payload contains previous_selection and "
    "server_validation_errors. Return a complete revised selection. Preserve every valid "
    "component, fact, exact anchor, and relationship from previous_selection. Change only "
    "what is necessary to resolve the listed errors; do not re-extract the notice from scratch, "
    "invent a business answer, or use any Gold/expected output."
)


# The bundle is the cache/reproducibility key for this producer.  The three
# vendored values are checked against their source at call time; the two local
# instructions are hashed here so changing either one creates a new lineage.
_PROMPT_BUNDLE_COMPONENT_HASHES = {
    "section_scope": _PROMPT_SHA256[
        ("semantic_structuring.common_ir_v1", "classify_common_ir_v1_attachment_scopes")
    ],
    "block_router": _PROMPT_SHA256[
        ("semantic_structuring.run_block_candidate_discovery_test", "main")
    ],
    "source_selection": _PROMPT_SHA256[
        ("semantic_structuring.run_source_selection_test", "main")
    ],
    "anchor_correction": hashlib.sha256(
        _ANCHOR_CORRECTION_INSTRUCTIONS.encode("utf-8")
    ).hexdigest(),
    "repair": hashlib.sha256(_REPAIR_INSTRUCTIONS.encode("utf-8")).hexdigest(),
}
PROMPT_BUNDLE_VERSION = (
    "announcement_profile_prompt_bundle/v1:"
    + hashlib.sha256(
        "\n".join(
            f"{name}={_PROMPT_BUNDLE_COMPONENT_HASHES[name]}"
            for name in sorted(_PROMPT_BUNDLE_COMPONENT_HASHES)
        ).encode("utf-8")
    ).hexdigest()
)


def _stage_error(
    *,
    stage: str,
    unit: str | None,
    reason_code: str,
    message: str,
    attempt: int | None = None,
) -> StageError:
    return StageError(
        StageDiagnostic(
            stage=stage,
            unit=unit,
            reason_code=reason_code,
            message=message[:2000],
            attempt=attempt,
            terminated_because=reason_code,
        )
    )


def _generate(
    llm_client: LLMClient,
    *,
    task_name: str,
    instructions: str,
    payload: dict[str, Any],
    response_schema: type,
    model_profile: str,
) -> Any:
    """Call the one backend LLM boundary and preserve its reason class."""

    return generate(
        llm_client,
        task_name=task_name,
        instructions=instructions,
        payload=payload,
        response_schema=response_schema,
        model_profile=model_profile,
    )


def _llm_stage_error(error: Exception, *, stage: str, unit: str, attempt: int | None = None) -> StageError:
    reason_code = {
        LLMTimeoutError: LLM_TIMEOUT,
        LLMUnavailableError: LLM_UNAVAILABLE,
        LLMInvalidResponseError: LLM_INVALID_RESPONSE,
    }.get(type(error), LLM_UNAVAILABLE)
    return _stage_error(
        stage=stage,
        unit=unit,
        reason_code=reason_code,
        message=f"{type(error).__name__}: {error}",
        attempt=attempt,
    )


def _call_llm(
    llm_client: LLMClient,
    *,
    task_name: str,
    instructions: str,
    payload: dict[str, Any],
    response_schema: type,
    model_profile: str,
    stage: str,
    unit: str,
    attempt: int | None = None,
) -> Any:
    try:
        return _generate(
            llm_client,
            task_name=task_name,
            instructions=instructions,
            payload=payload,
            response_schema=response_schema,
            model_profile=model_profile,
        )
    except (LLMTimeoutError, LLMUnavailableError, LLMInvalidResponseError) as error:
        raise _llm_stage_error(error, stage=stage, unit=unit, attempt=attempt) from error


def _prepare_scoped_notice(
    document: dict[str, Any], llm_client: LLMClient, model_profile: str
):
    """Run the optional attachment-scope call, then apply its decision."""

    try:
        unscoped, _projection = prepare_common_ir_v1(document)
    except Exception as error:  # noqa: BLE001 - this document is isolated by caller
        raise _stage_error(
            stage="common_ir_v1_preparation",
            unit=document.get("document", {}).get("document_id"),
            reason_code=COMMON_IR_INVALID,
            message=f"{type(error).__name__}: {error}",
        ) from error

    attachments = [
        section for section in unscoped.sections if not _is_main_notice_section(section.section_id)
    ]
    decisions: list[SectionScopeDecision] = []
    if attachments:
        source_by_id = {
            block.block_id: block
            for block in [*unscoped.fact_candidate_blocks, *unscoped.table_candidate_blocks]
        }
        payload = {
            "notice_id": unscoped.notice_id,
            "attachments": [
                _v1_attachment_input(section, source_by_id) for section in attachments
            ],
        }
        response = _call_llm(
            llm_client,
            task_name=SECTION_SCOPE_TASK,
            instructions=_scope_instructions(),
            payload=payload,
            response_schema=SectionScopeDiscovery,
            model_profile=model_profile,
            stage="section_scope_discovery",
            unit=unscoped.common_ir_document_id,
        )
        expected = {section.section_id for section in attachments}
        actual = [decision.section_id for decision in response.decisions]
        if (
            response.notice_id != unscoped.notice_id
            or set(actual) != expected
            or len(actual) != len(set(actual))
        ):
            raise _stage_error(
                stage="section_scope_discovery",
                unit=unscoped.common_ir_document_id,
                reason_code=LLM_INVALID_RESPONSE,
                message="section scope output did not cover each Common IR attachment exactly once",
            )
        decisions = response.decisions
    try:
        return apply_common_ir_v1_section_scopes(document, decisions)
    except Exception as error:  # noqa: BLE001 - contract failure is per document
        raise _stage_error(
            stage="section_scope_apply",
            unit=unscoped.common_ir_document_id,
            reason_code=COMMON_IR_INVALID,
            message=f"{type(error).__name__}: {error}",
        ) from error


def _route_blocks(prepared, llm_client: LLMClient, model_profile: str):
    try:
        router_pack = prepared.router_pack()
    except Exception as error:  # noqa: BLE001
        raise _stage_error(
            stage="block_candidate_router",
            unit=prepared.common_ir_document_id,
            reason_code=CANDIDATE_PACK_EMPTY,
            message=f"{type(error).__name__}: {error}",
        ) from error
    if not router_pack.blocks:
        raise _stage_error(
            stage="block_candidate_router",
            unit=prepared.common_ir_document_id,
            reason_code=CANDIDATE_PACK_EMPTY,
            message="Common IR router CandidatePack contains no blocks",
        )
    payload = {
        "notice_id": router_pack.notice_id,
        "candidate_pack_id": router_pack.pack_id,
        "source_blocks": [block.model_dump(mode="json") for block in router_pack.blocks],
    }
    response = _call_llm(
        llm_client,
        task_name=BLOCK_ROUTER_TASK,
        instructions=_router_instructions(),
        payload=payload,
        response_schema=BlockCandidateDiscovery,
        model_profile=model_profile,
        stage="block_candidate_router",
        unit=prepared.common_ir_document_id,
    )
    blocks_by_id = {block.block_id: block for block in router_pack.blocks}
    candidate_ids = [candidate.source_block_id for candidate in response.candidates]
    available_ids = set(blocks_by_id)
    unknown_ids = sorted(set(candidate_ids) - available_ids)
    missing_ids = sorted(available_ids - set(candidate_ids))
    duplicate_ids = sorted(
        {block_id for block_id in candidate_ids if candidate_ids.count(block_id) > 1}
    )
    if (
        response.notice_id != router_pack.notice_id
        or response.candidate_pack_id != router_pack.pack_id
        or unknown_ids
        or missing_ids
        or duplicate_ids
    ):
        raise _stage_error(
            stage="block_candidate_router",
            unit=prepared.common_ir_document_id,
            reason_code=LLM_INVALID_RESPONSE,
            message=(
                "router output identifiers invalid: "
                f"unknown={unknown_ids} missing={missing_ids} duplicate={duplicate_ids}"
            ),
        )
    for candidate in response.candidates:
        is_table = blocks_by_id[candidate.source_block_id].block_kind == "table"
        if is_table and candidate.route_tags != [CandidateRoute.TABLE]:
            raise _stage_error(
                stage="block_candidate_router",
                unit=prepared.common_ir_document_id,
                reason_code=LLM_INVALID_RESPONSE,
                message=f"table route requires only table: {candidate.source_block_id}",
            )
        if is_table and candidate.table_disposition is None:
            raise _stage_error(
                stage="block_candidate_router",
                unit=prepared.common_ir_document_id,
                reason_code=LLM_INVALID_RESPONSE,
                message=f"table route requires table_disposition: {candidate.source_block_id}",
            )
        if not is_table and candidate.table_disposition is not None:
            raise _stage_error(
                stage="block_candidate_router",
                unit=prepared.common_ir_document_id,
                reason_code=LLM_INVALID_RESPONSE,
                message=f"non-table route must not have table_disposition: {candidate.source_block_id}",
            )
    try:
        pack, metrics = build_routed_a_pack(
            prepared, [candidate.model_dump(mode="json") for candidate in response.candidates]
        )
    except ValueError as error:
        raise _stage_error(
            stage="block_candidate_router",
            unit=prepared.common_ir_document_id,
            reason_code=LLM_INVALID_RESPONSE,
            message=f"{type(error).__name__}: {error}",
        ) from error
    if pack is None or not pack.blocks:
        raise _stage_error(
            stage="block_candidate_router",
            unit=prepared.common_ir_document_id,
            reason_code=CANDIDATE_PACK_EMPTY,
            message="router produced no A candidate blocks",
        )
    return pack, metrics


def _trusted_source_sha256(document: dict[str, Any], pack) -> str:
    identity = common_ir_v1_identity(document)
    if pack.common_ir_document_id is None:
        raise _stage_error(
            stage="source_selection",
            unit=identity.get("document_id"),
            reason_code=COMMON_IR_INVALID,
            message="Common IR CandidatePack is missing common_ir_document_id",
        )
    if identity["document_id"] != pack.common_ir_document_id:
        raise _stage_error(
            stage="source_selection",
            unit=identity.get("document_id"),
            reason_code=COMMON_IR_INVALID,
            message="Common IR document_id does not match the routed CandidatePack",
        )
    source_sha256 = verified_common_ir_source_sha256(document)
    if not isinstance(source_sha256, str) or not re.fullmatch(SHA256_HEX_PATTERN, source_sha256):
        raise _stage_error(
            stage="source_selection",
            unit=identity.get("document_id"),
            reason_code=COMMON_IR_INVALID,
            message="Common IR source_sha256 is missing or is not lowercase SHA-256",
        )
    return source_sha256


def verified_common_ir_source_sha256(document: dict[str, Any]) -> str:
    """Return only the source hash from a validated Common IR identity."""

    try:
        identity = common_ir_v1_identity(document)
    except (KeyError, TypeError, AttributeError) as error:
        raise ValueError("Common IR document identity is missing") from error
    document_id = identity.get("document_id")
    source_sha256 = identity.get("source_sha256")
    if not isinstance(document_id, str) or not document_id:
        raise ValueError("Common IR document_id is missing")
    if not isinstance(source_sha256, str) or not re.fullmatch(SHA256_HEX_PATTERN, source_sha256):
        raise ValueError("Common IR source_sha256 is missing or is not lowercase SHA-256")
    return source_sha256


def _materialized_evidence_payload(evidence: list[Any]) -> list[dict[str, Any]]:
    return [
        {
            "fact_id": row.fact_id,
            "field_name": row.field_name.value,
            "status": row.status.value,
            "semantic_role": row.semantic_role,
            "subject_role": row.subject_role.value if row.subject_role is not None else None,
            "source_blocks": row.source_blocks,
            "value_source": row.value_source.model_dump(mode="json") if row.value_source else None,
            "context_blocks": row.context_blocks,
            "organization_names": row.organization_names,
            "organization_sources": [source.model_dump(mode="json") for source in row.organization_sources],
            "role_raw": row.role_raw,
            "role_source_block_id": row.role_source_block_id,
            "role_source": row.role_source.model_dump(mode="json") if row.role_source else None,
            "canonical_role": row.canonical_role.value if row.canonical_role is not None else None,
            "primary_component_id": row.primary_component_id,
            "applicability_component_ids": row.applicability_component_ids,
            "modifies_fact_ids": row.modifies_fact_ids,
            "recipient_fact_ids": row.recipient_fact_ids,
            "basis_fact_ids": row.basis_fact_ids,
        }
        for row in evidence
    ]


def _selection_artifact(
    *,
    extraction: SourceSelectionExtractionV02,
    evidence: list[Any],
    components: list[Any],
    measures: list[Any],
    numeric_candidates: list[Any],
    pack,
    pack_metrics: dict[str, Any],
    document: dict[str, Any],
    attempt_count: int,
    correction_call_count: int,
    corrected_anchor_audit: list[dict[str, Any]],
    component_normalizations: list[dict[str, Any]],
    condition_variant_normalizations: list[dict[str, Any]],
    preserved_fact_normalizations: list[dict[str, Any]],
    preservation_fallback: dict[str, Any] | None,
) -> dict[str, Any]:
    identity = common_ir_v1_identity(document)
    return {
        "selection": extraction.model_dump(mode="json"),
        "selection_contract": "v0.2_anchor",
        "materialized_evidence": _materialized_evidence_payload(evidence),
        "materialized_components": [
            {
                "support_component_id": row.support_component_id,
                "name_raw": row.name_raw,
                "name_source_block_id": row.name_source_block_id,
            }
            for row in components
        ],
        "support_facets": [facet.model_dump(mode="json") for facet in extraction.support_facets],
        "support_scale_measures": [measure.model_dump(mode="json") for measure in measures],
        "numeric_candidates": [candidate.model_dump(mode="json") for candidate in numeric_candidates],
        "source_block_texts": {block.block_id: block.text for block in pack.blocks},
        "attempt_count": attempt_count,
        "correction_call_count": correction_call_count,
        "corrected_anchor_audit": corrected_anchor_audit,
        "input_block_count": len(pack.blocks),
        "input_table_metrics": pack_metrics,
        "server_component_normalizations": component_normalizations,
        "server_condition_variant_normalizations": condition_variant_normalizations,
        "server_preserved_fact_normalizations": preserved_fact_normalizations,
        "server_preservation_fallback": preservation_fallback,
        "common_ir_identity": identity,
        "candidate_pack_lineage": {
            "candidate_pack_id": pack.pack_id,
            "candidate_pack_generator": pack.generator,
            "candidate_pack_generator_version": pack.generator_version,
            "common_ir_document_id": pack.common_ir_document_id,
            "common_ir_source_sha256": identity["source_sha256"],
            "text_basis": "common_ir_v1_candidate_pack",
        },
    }


def _select_and_assemble(
    document: dict[str, Any],
    pack,
    pack_metrics: dict[str, Any],
    llm_client: LLMClient,
    model_profile: str,
) -> dict[str, Any]:
    common_ir_source_sha256 = _trusted_source_sha256(document, pack)
    numeric_candidates = build_numeric_candidates(pack)
    base_request = {
        "notice_id": pack.notice_id,
        "candidate_pack_id": pack.pack_id,
        "source_blocks": [block.model_dump(mode="json") for block in pack.blocks],
        "numeric_candidates": [candidate.model_dump(mode="json") for candidate in numeric_candidates],
    }
    correction_audit: dict[str, dict[str, object]] = {}
    correction_call_count = 0

    def correction_once(request: AnchorCorrectionRequest) -> str:
        nonlocal correction_call_count
        correction_call_count += 1
        correction_audit[request.fact_id] = {
            "source_block_id": request.source_block_id,
            "candidate_count": len(request.candidates),
        }
        response = _generate(
            llm_client,
            task_name=ANCHOR_CORRECTION_TASK,
            instructions=_ANCHOR_CORRECTION_INSTRUCTIONS,
            payload=request.model_dump(mode="json"),
            response_schema=AnchorCorrectionResponse,
            model_profile=model_profile,
        )
        return response.candidate_id

    correction_resolver = memoize_anchor_correction_resolver(correction_once)
    last_error: str | None = None
    last_reason = REPAIR_BUDGET_EXHAUSTED
    prior_selection: dict[str, Any] | None = None
    carry_forward: SourceSelectionExtractionV02 | None = None
    final_bundle = None
    selection_call_count = 0
    component_normalizations: list[dict[str, Any]] = []
    condition_variant_normalizations: list[dict[str, Any]] = []
    preserved_fact_normalizations: list[dict[str, Any]] = []
    preservation_fallback: dict[str, Any] | None = None

    def required_support_scale_anchors(validation_error: str | None) -> list[dict[str, str]]:
        """Expose only server-identified missing cap spans to the repair call."""

        marker = "select it as its own atomic support_scale span: "
        if not validation_error or marker not in validation_error:
            return []
        result: list[dict[str, str]] = []
        for item in validation_error.split(marker, 1)[1].split(", "):
            block_id, separator, anchor_text = item.partition(": ")
            if separator and block_id and anchor_text:
                result.append({"source_block_id": block_id, "anchor_text": anchor_text})
        return result

    def finalize(candidate: SourceSelectionExtractionV02):
        validate_selection_quality_v02(candidate)
        candidate, sequential = normalize_explicit_sequential_components_v02(candidate, pack)
        candidate, nested = normalize_nested_support_scale_anchors_v02(candidate)
        sequential.extend(nested)
        validate_component_structure_v02(candidate, pack)
        validate_support_cap_completeness_v02(candidate, pack)
        candidate, variants = normalize_explicit_condition_variant_relations_v02(candidate)
        evidence = materialize_evidence(
            candidate,
            pack,
            common_ir_source_sha256=common_ir_source_sha256,
            resolve_ambiguous_value_anchor=correction_resolver,
        )
        components = materialize_components(candidate, pack)
        resolved_value_sources = {
            row.fact_id: row.value_source
            for row in evidence
            if row.value_source is not None
        }
        measures = derive_support_scale_measures_v02(
            candidate,
            numeric_candidates,
            resolved_value_sources,
            source_block_texts={block.block_id: block.text for block in pack.blocks},
        )
        validate_scale_measure_candidates_v02(
            candidate, measures, numeric_candidates, resolved_value_sources
        )
        return candidate, sequential, variants, evidence, components, measures

    for attempt in range(2):
        is_repair = bool(last_error) and prior_selection is not None
        instructions = _source_selection_instructions() + (_REPAIR_INSTRUCTIONS if is_repair else "")
        request = base_request
        if is_repair:
            required_caps = required_support_scale_anchors(last_error)
            if required_caps:
                instructions += (
                    " The payload's required_support_scale_anchors were deterministically found in "
                    "your own selected evidence. Include each one as a separate support_scale fact using "
                    "exactly its supplied source_block_id and anchor_text."
                )
            request = {
                **base_request,
                "previous_selection": prior_selection,
                "server_validation_errors": [last_error],
                "required_support_scale_anchors": required_caps,
            }
        selection_call_count += 1
        extraction = _call_llm(
            llm_client,
            task_name=SOURCE_SELECTION_TASK,
            instructions=instructions,
            payload=request,
            response_schema=SourceSelectionExtractionV02,
            model_profile=model_profile,
            stage="source_selection",
            unit=pack.common_ir_document_id,
            attempt=attempt + 1,
        )
        prior_selection = extraction.model_dump(mode="json")
        if extraction.notice_id != pack.notice_id or extraction.candidate_pack_id != pack.pack_id:
            last_error = "source selection output belongs to a different candidate pack"
            last_reason = LLM_INVALID_RESPONSE
            continue
        empty_error = classify_empty_repair_response_v02(
            is_repair_attempt=is_repair,
            facts=extraction.facts,
            prior_candidate=carry_forward,
            prior_validation_error=last_error,
        )
        if empty_error is not None:
            raise _stage_error(
                stage="source_selection",
                unit=pack.common_ir_document_id,
                reason_code=LLM_INVALID_RESPONSE,
                message=str(empty_error),
                attempt=attempt + 1,
            ) from empty_error
        if not extraction.facts:
            last_error = (
                "the routed A candidate pack contains substantive source blocks, but no fact was selected"
            )
            last_reason = LLM_INVALID_RESPONSE
            continue
        merged, preserved_changes = preserve_prior_server_validated_facts_v02(
            carry_forward, extraction, pack
        )
        carry_forward = merged
        try:
            final_bundle, preserved_fact_normalizations, preservation_fallback = apply_finalize_with_fallback_v02(
                merged, extraction, preserved_changes, finalize
            )
            component_normalizations = final_bundle[1]
            condition_variant_normalizations = final_bundle[2]
            break
        except (CorrectionResolverError, AmbiguousAnchorCorrectionError) as error:
            cause = error.__cause__
            if isinstance(cause, (LLMTimeoutError, LLMUnavailableError, LLMInvalidResponseError)):
                raise _llm_stage_error(
                    cause,
                    stage="anchor_correction",
                    unit=pack.common_ir_document_id,
                    attempt=attempt + 1,
                ) from error
            raise _stage_error(
                stage="anchor_correction",
                unit=pack.common_ir_document_id,
                reason_code=MATERIALIZATION_FAILED,
                message=f"{type(error).__name__}: {error}",
                attempt=attempt + 1,
            ) from error
        except ValueError as error:
            last_error = str(error)
            last_reason = MATERIALIZATION_FAILED
    else:
        raise _stage_error(
            stage="source_selection",
            unit=pack.common_ir_document_id,
            reason_code=last_reason,
            message=f"source selection failed after one repair: {last_error or 'unknown validation error'}",
            attempt=2,
        )

    if final_bundle is None:  # pragma: no cover - defensive loop invariant
        raise _stage_error(
            stage="source_selection",
            unit=pack.common_ir_document_id,
            reason_code=MATERIALIZATION_FAILED,
            message="source selection produced no finalized bundle",
        )
    extraction, _sequential, _variants, evidence, components, measures = final_bundle
    artifact = _selection_artifact(
        extraction=extraction,
        evidence=evidence,
        components=components,
        measures=measures,
        numeric_candidates=numeric_candidates,
        pack=pack,
        pack_metrics=pack_metrics,
        document=document,
        attempt_count=selection_call_count,
        correction_call_count=correction_call_count,
        corrected_anchor_audit=build_corrected_anchor_audit(evidence, correction_audit),
        component_normalizations=component_normalizations,
        condition_variant_normalizations=condition_variant_normalizations,
        preserved_fact_normalizations=preserved_fact_normalizations,
        preservation_fallback=preservation_fallback,
    )
    derived_projections = [
        facet.model_dump(mode="json") for facet in extraction.support_facets
    ] + [measure.model_dump(mode="json") for measure in measures]
    try:
        profile = assemble_final_profile_v02(
            artifact,
            common_ir_v1_metadata(document),
            derived_projections=derived_projections,
        )
    except Exception as error:  # noqa: BLE001 - final assembly is per document
        raise _stage_error(
            stage="final_profile_assembly",
            unit=pack.common_ir_document_id,
            reason_code=MATERIALIZATION_FAILED,
            message=f"{type(error).__name__}: {error}",
        ) from error
    profile.setdefault("processing_metadata", {})["generation_lineage"] = {
        "model_profile": model_profile,
        "prompt_bundle_version": PROMPT_BUNDLE_VERSION,
        "source_sha256_hex": common_ir_source_sha256,
    }
    # The KB store needs the exact routed pack that produced this profile, not
    # the whole Common IR document under a misleading artifact type.
    profile["processing_metadata"]["candidate_pack_artifact"] = candidate_pack_artifact(
        pack, document
    )
    return profile


def structure_announcement_profile(
    document: dict[str, Any], llm_client: LLMClient, *, model_profile: str
) -> dict[str, Any]:
    """Run the production Common IR v1 → Existing Profile v0.2 chain."""

    prepared = _prepare_scoped_notice(document, llm_client, model_profile)
    pack, metrics = _route_blocks(prepared, llm_client, model_profile)
    return _select_and_assemble(document, pack, metrics, llm_client, model_profile)


__all__ = [
    "ANCHOR_CORRECTION_TASK",
    "BLOCK_ROUTER_TASK",
    "PROMPT_BUNDLE_VERSION",
    "SECTION_SCOPE_TASK",
    "SOURCE_SELECTION_TASK",
    "structure_announcement_profile",
    "verified_common_ir_source_sha256",
]
