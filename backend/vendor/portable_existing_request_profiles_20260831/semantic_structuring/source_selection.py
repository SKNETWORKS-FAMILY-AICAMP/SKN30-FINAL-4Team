"""A-profile source selection with server-side evidence materialization."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_core import PydanticCustomError

from .anchor_occurrence_resolver import (
    AnchorOccurrenceRequest,
    AnchorOccurrenceResolution,
    materialize_selected_anchor_candidate,
    resolve_anchor_occurrences,
)
from .explicit_support_cap_candidates import (
    KOREAN_KRW_MONEY_SOURCE,
    explicit_support_cap_spans_by_block,
    extract_explicit_support_cap_candidates,
    is_historical_support_cap_context,
)
from .explicit_list_completeness import (
    ExplicitListCompletenessError,
    explicit_list_repair_has_invalid_overlaps_v02,
    validate_explicit_list_completeness_v02,
)
from .models import CandidatePack, ComponentKind, FactField
from .native_provenance import (
    native_block_provenance,
    project_value_source_to_atomic_ranges,
    stable_unique_occurrence_ids,
)
from .profile_v02 import (
    AggregationScope,
    CalculationBasis,
    Comparator,
    MeasureRole,
    MeasureType,
    SupportFacetsProjection,
    SupportScaleMeasure,
    SupportScaleMeasuresProjection,
    ValueSource,
    find_all_occurrences,
    materialize_value_source,
    validate_support_scale_measure_shape,
)
from .support_scale_policy import (
    explicit_per_unit_scope,
    per_unit_scope_occurrences,
)


# These are public, compact failure-stage identifiers.  They are deliberately
# stable because the worker retains only the string error in a retry artifact.
FINALIZE_STAGE_CODES = frozenset({
    "selection_quality_validation",
    "sequential_component_normalization",
    "semantic_duplicate_normalization",
    "nested_support_scale_anchor_normalization",
    "component_structure_validation",
    "condition_variant_relation_normalization",
    "evidence_materialization",
    "value_source_uniqueness_validation",
    "explicit_list_completeness_validation",
    "repair_issue_aggregation",
    "component_materialization",
    "support_scale_semantics_validation",
    "support_cap_completeness_validation",
    "support_scale_measure_derivation",
    "scale_measure_validation",
})

COMPONENT_NAME_ANCHOR_SOURCE_ERROR = (
    "component_name_anchor_must_reference_component_source_block"
)


class DuplicateResolvedValueSourceSpanError(ValueError):
    """Two facts attempted to own one already-materialized source span."""

    def __init__(self, conflicts: list[dict[str, str]]) -> None:
        self.conflicts = [
            {"fact_id": conflict["fact_id"], "field_name": conflict["field_name"]}
            for conflict in conflicts
        ]
        labels = ", ".join(
            f"{item['fact_id']} ({item['field_name']})" for item in self.conflicts
        )
        super().__init__(f"duplicate resolved ValueSource span across facts: {labels}")


@contextmanager
def annotate_finalize_stage(code: str):
    """Add an allowlisted finalization stage without hiding the root error."""

    if code not in FINALIZE_STAGE_CODES:
        raise ValueError("unsupported finalize stage code")
    try:
        yield
    except ValueError as error:
        if not hasattr(error, "finalize_stage"):
            error.finalize_stage = code
        raise


def finalize_source_selection_v02(
    candidate: "SourceSelectionExtractionV02",
    pack: CandidatePack,
    numeric_candidates: list["NumericCandidate"],
    *,
    common_ir_source_sha256: str | None = None,
    resolve_ambiguous_value_anchor: "AnchorCorrectionResolver | None" = None,
    normalize_sequential_components=None,
    require_support_cap_completeness: bool = True,
    value_source_overrides: Mapping[str, ValueSource | Mapping[str, object]] | None = None,
    typed_repair_error: ValueError | None = None,
):
    """Run the single Existing Profile v0.2 finalization boundary.

    Model selection, deterministic repair, and the worker must all reach this
    function rather than carrying subtly different local finalize sequences.
    Request-profile construction has its own v0.1.5 path and is intentionally
    not invoked here.
    """

    normalize_sequential_components = (
        normalize_sequential_components or normalize_explicit_sequential_components_v02
    )
    selection_quality_repair_error: SupportScaleFactRepairError | None = None
    try:
        with annotate_finalize_stage("selection_quality_validation"):
            validate_selection_quality_v02(candidate, pack=pack)
    except SupportScaleFactRepairError as error:
        # This typed defect is safe to carry through the remaining
        # deterministic audits. Doing so lets the worker repair it together
        # with list/cap omissions inside its one bounded retry.
        selection_quality_repair_error = error
    with annotate_finalize_stage("sequential_component_normalization"):
        candidate, sequential_normalizations = normalize_sequential_components(candidate, pack)
    with annotate_finalize_stage("semantic_duplicate_normalization"):
        candidate, semantic_normalizations = normalize_semantic_duplicate_facts_v02(candidate, pack)
    sequential_normalizations.extend(semantic_normalizations)
    with annotate_finalize_stage("nested_support_scale_anchor_normalization"):
        candidate, nested_normalizations = normalize_nested_support_scale_anchors_v02(candidate)
    sequential_normalizations.extend(nested_normalizations)
    with annotate_finalize_stage("component_structure_validation"):
        validate_component_structure_v02(candidate, pack)
    with annotate_finalize_stage("condition_variant_relation_normalization"):
        candidate, variant_normalizations = normalize_explicit_condition_variant_relations_v02(candidate)
    try:
        with annotate_finalize_stage("evidence_materialization"):
            evidence = materialize_evidence(
                candidate,
                pack,
                common_ir_source_sha256=common_ir_source_sha256,
                resolve_ambiguous_value_anchor=resolve_ambiguous_value_anchor,
                value_source_overrides=value_source_overrides,
            )
    except ExactAnchorMaterializationRepairError as exact_anchor_error:
        # Selection-quality validation runs before evidence materialization and
        # may already have found an independent, typed support-scale defect.
        # Preserve both findings in the single bounded repair request rather
        # than making the model spend its only retry on the absent anchor and
        # discover the scale defect one attempt too late.  Do not prune facts
        # or continue downstream list/cap audits here: doing so would create an
        # audit-only selection with different relation/component semantics.
        if selection_quality_repair_error is None:
            raise
        with annotate_finalize_stage("repair_issue_aggregation"):
            raise SourceSelectionRepairIssuesError([
                exact_anchor_error,
                selection_quality_repair_error,
            ]) from exact_anchor_error
    with annotate_finalize_stage("value_source_uniqueness_validation"):
        validate_materialized_value_source_uniqueness_v02(evidence)
    validate_materialized_typed_repair_replacements_v02(
        pack, evidence, typed_repair_error
    )
    list_error: ExplicitListCompletenessError | None = None
    try:
        with annotate_finalize_stage("explicit_list_completeness_validation"):
            validate_explicit_list_completeness_v02(pack, evidence)
    except ExplicitListCompletenessError as error:
        list_error = error
    with annotate_finalize_stage("component_materialization"):
        components = materialize_components(candidate, pack)
    with annotate_finalize_stage("support_scale_semantics_validation"):
        scale_repair_error = validate_materialized_support_scale_semantics_v02(
            candidate, pack, evidence, raise_error=False,
        )
    cap_error: SupportCapCompletenessError | None = None
    support_cap_check_state = "complete" if require_support_cap_completeness else "not_checked"
    if require_support_cap_completeness:
        try:
            with annotate_finalize_stage("support_cap_completeness_validation"):
                validate_support_cap_completeness_v02(
                    candidate, pack, materialized_evidence=evidence,
                )
        except SupportCapCompletenessError as error:
            cap_error = error
    scale_errors = [
        error
        for error in (selection_quality_repair_error, scale_repair_error)
        if error is not None
    ]
    if not scale_errors:
        combined_scale_error = None
    elif len(scale_errors) == 1:
        combined_scale_error = scale_errors[0]
    else:
        combined_payload = {
            (
                item["fact_id"],
                item["source_block_id"],
                item["reason"],
            ): item
            for error in scale_errors
            for item in error.repair_payload()
        }
        combined_scale_error = SupportScaleFactRepairError(
            [combined_payload[key] for key in sorted(combined_payload)]
        )
    if combined_scale_error is not None:
        if cap_error is not None:
            combined_scale_error = combined_scale_error.with_missing_support_cap_anchors(
                cap_error.required_support_scale_anchors(),
                additional_do_not_restore_fact_ids=cap_error.do_not_restore_fact_ids,
            )
        else:
            combined_scale_error = combined_scale_error.with_support_cap_check_state(
                support_cap_check_state
            )
    scale_or_cap_error: ValueError | None = combined_scale_error or cap_error
    if list_error is not None and scale_or_cap_error is not None:
        with annotate_finalize_stage("repair_issue_aggregation"):
            raise SourceSelectionRepairIssuesError(
                [list_error, scale_or_cap_error]
            )
    if list_error is not None:
        raise list_error
    if combined_scale_error is not None:
        with annotate_finalize_stage("support_scale_semantics_validation"):
            raise combined_scale_error
    if cap_error is not None:
        with annotate_finalize_stage("support_cap_completeness_validation"):
            raise cap_error
    resolved_value_sources = {
        row.fact_id: row.value_source
        for row in evidence
        if row.value_source is not None
    }
    with annotate_finalize_stage("support_scale_measure_derivation"):
        measures = derive_support_scale_measures_v02(
            candidate,
            numeric_candidates,
            resolved_value_sources,
            source_block_texts={block.block_id: block.text for block in pack.blocks},
        )
    with annotate_finalize_stage("scale_measure_validation"):
        validate_scale_measure_candidates_v02(
            candidate,
            measures,
            numeric_candidates,
            resolved_value_sources,
            support_cap_check_state=support_cap_check_state,
        )
    return (
        candidate,
        sequential_normalizations,
        variant_normalizations,
        evidence,
        components,
        measures,
    )


def validate_materialized_value_source_uniqueness_v02(
    evidence: list["MaterializedEvidence"],
) -> None:
    """Enforce exact `(block, start, end)` Raw Fact ownership once resolved."""

    by_span: dict[tuple[str, int, int], list[dict[str, str]]] = {}
    for row in evidence:
        source = row.value_source
        if source is None:
            continue
        by_span.setdefault(
            (source.source_block_id, source.start_char, source.end_char), []
        ).append({"fact_id": row.fact_id, "field_name": row.field_name.value})
    conflicts = [item for rows in by_span.values() if len(rows) > 1 for item in rows]
    if conflicts:
        raise DuplicateResolvedValueSourceSpanError(conflicts)


class SelectionStatus(StrEnum):
    IDENTIFIED = "identified"
    PARTIALLY_IDENTIFIED = "partially_identified"


class ComponentDecisionMode(StrEnum):
    NONE = "none"
    PACKAGES = "packages"
    PARTICIPATION_TYPES = "participation_types"
    STAGES = "stages"
    MIXED = "mixed"


class DeliveryRoleCanonical(StrEnum):
    ANNOUNCING_AGENCY = "announcing_agency"
    LEAD_AGENCY = "lead_agency"
    OPERATING_AGENCY = "operating_agency"
    DEDICATED_AGENCY = "dedicated_agency"
    PARTICIPATING_PARTNER = "participating_partner"
    DEMAND_PARTNER = "demand_partner"
    COOPERATING_ORGANIZATION = "cooperating_organization"


class BeneficiarySubjectRole(StrEnum):
    """The only normalized roles permitted on a beneficiary Raw Fact."""

    FINANCIAL_RECIPIENT = "financial_recipient"
    POLICY_BENEFICIARY = "policy_beneficiary"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class SourceTextAnchor(StrictModel):
    """An exact source phrase selected for server-side location recovery.

    This is an ephemeral locator, not a persisted business value.  The server
    finds it in canonical source text and stores only the recovered text.
    """

    source_block_id: str = Field(min_length=1)
    anchor_text: str = Field(min_length=1)


class AnchorCorrectionCandidatePrompt(StrictModel):
    """One candidate as shown to the correction model: identity and context only.

    Deliberately excludes ``start_char``/``end_char`` and any occurrence
    ordinal.  The correction model must disambiguate the intended value from
    ``context_before``/``context_after`` alone, never from position.

    ``StrictModel`` strips whitespace by default; that would erase leading
    /trailing whitespace that is itself part of the exact context two
    otherwise-identical candidates are disambiguated by, so it is disabled
    here.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)

    candidate_id: str = Field(min_length=1)
    anchor_text: str = Field(min_length=1)
    context_before: str
    context_after: str


class AnchorCorrectionRequest(StrictModel):
    """CandidatePack Anchor Occurrence Resolver v1 correction-call payload."""

    fact_id: str = Field(min_length=1)
    field_name: FactField
    source_block_id: str = Field(min_length=1)
    anchor_text: str = Field(min_length=1)
    primary_component_id: str | None = None
    applicability_component_ids: tuple[str, ...] = ()
    context_source_block_ids: tuple[str, ...] = ()
    modifies_fact_ids: tuple[str, ...] = ()
    recipient_fact_ids: tuple[str, ...] = ()
    basis_fact_ids: tuple[str, ...] = ()
    candidates: list[AnchorCorrectionCandidatePrompt] = Field(min_length=2)


class AnchorCorrectionResponse(StrictModel):
    """The correction model's structured-output contract: one candidate_id."""

    candidate_id: str = Field(min_length=1)


# Injected by the caller (e.g. run_source_selection_test.py); this module
# makes no model/API calls itself.  It takes one ambiguous-anchor correction
# request and must return exactly one candidate_id chosen by the same LLM.
AnchorCorrectionResolver = Callable[[AnchorCorrectionRequest], str]


class AmbiguousAnchorCorrectionError(RuntimeError):
    """A repeated value_anchor could not be safely resolved to one span.

    Raised when a correction call returns a candidate_id that is not part of
    the resolution it was given, or when no correction resolver is available
    at all.  There is deliberately no first-occurrence fallback: an unknown
    or wrong candidate fails closed.  Diagnostics are limited to the
    candidate block id, the anchor text, and the candidate count -- never the
    model's full output -- so a failure artifact stays safe to persist.
    """

    error_classification = "ambiguous_anchor_unresolved"

    def __init__(self, *, fact_id: str, source_block_id: str, anchor_text: str, candidate_count: int, reason: str):
        super().__init__(
            f"fact {fact_id}: value_anchor is ambiguous in block {source_block_id} "
            f"({candidate_count} candidates) and correction did not select a valid candidate_id: {reason}"
        )
        self.fact_id = fact_id
        self.source_block_id = source_block_id
        self.anchor_text = anchor_text
        self.candidate_count = candidate_count
        self.reason = reason


class CorrectionResolverError(RuntimeError):
    """The injected correction resolver itself failed, not a wrong choice.

    Distinct from ``AmbiguousAnchorCorrectionError``: that class means the
    correction call *ran* and returned an unknown or wrong ``candidate_id``.
    This class means the call could not be completed at all -- an API/network
    failure, an incomplete response, a structured-output parse/validation
    failure, or any other exception the injected resolver raised.  That
    failure belongs to the correction call, not to the first selection LLM's
    one-call contract: it is a ``RuntimeError``, not a ``ValueError``, so it
    can never be mistaken for an ordinary contract-validation failure and
    silently re-enter that model's repair loop.  Diagnostics are limited to
    the candidate block id, the anchor text, the candidate count, and the
    failing exception's type name -- never its message or the correction
    call's raw response -- so a failure artifact stays safe to persist.
    """

    error_classification = "correction_resolver_failed"

    def __init__(self, *, fact_id: str, source_block_id: str, anchor_text: str, candidate_count: int, error_type: str):
        super().__init__(
            f"fact {fact_id}: correction resolver failed in block {source_block_id} "
            f"({candidate_count} candidates): {error_type}"
        )
        self.fact_id = fact_id
        self.source_block_id = source_block_id
        self.anchor_text = anchor_text
        self.candidate_count = candidate_count
        self.error_type = error_type


def build_anchor_correction_request(
    fact_id: str,
    field_name: FactField,
    resolution: AnchorOccurrenceResolution,
    *,
    primary_component_id: str | None = None,
    applicability_component_ids: tuple[str, ...] = (),
    context_source_block_ids: tuple[str, ...] = (),
    modifies_fact_ids: tuple[str, ...] = (),
    recipient_fact_ids: tuple[str, ...] = (),
    basis_fact_ids: tuple[str, ...] = (),
) -> AnchorCorrectionRequest:
    """Turn an ambiguous resolution into the compact, offset-free correction call.

    ``resolution.candidates`` is in document occurrence order (ascending
    ``start_char``).  The prompt list is instead sorted by ``candidate_id`` --
    a content-derived hash independent of position -- so list order itself
    never leaks which candidate came first in the source text.
    """

    if resolution.status != "ambiguous":
        raise ValueError("anchor correction request requires an ambiguous resolution")
    return AnchorCorrectionRequest(
        fact_id=fact_id,
        field_name=field_name,
        source_block_id=resolution.request.source_block_id,
        anchor_text=resolution.request.anchor_text,
        primary_component_id=primary_component_id,
        applicability_component_ids=applicability_component_ids,
        context_source_block_ids=context_source_block_ids,
        modifies_fact_ids=modifies_fact_ids,
        recipient_fact_ids=recipient_fact_ids,
        basis_fact_ids=basis_fact_ids,
        candidates=sorted(
            (
                AnchorCorrectionCandidatePrompt(
                    candidate_id=candidate.candidate_id,
                    anchor_text=candidate.anchor_text,
                    context_before=candidate.context_before,
                    context_after=candidate.context_after,
                )
                for candidate in resolution.candidates
            ),
            key=lambda prompt: prompt.candidate_id,
        ),
    )


def _local_source_unit(text: str, start: int, end: int) -> tuple[int, int]:
    """Return a conservative newline/semicolon-bounded ownership unit."""

    starts = [text.rfind(delimiter, 0, start) + 1 for delimiter in ("\n", ";", "；")]
    unit_start = max(starts)
    ends = [
        position
        for delimiter in ("\n", ";", "；")
        if (position := text.find(delimiter, end)) >= 0
    ]
    return unit_start, min(ends, default=len(text))


def _objectively_bound_candidate_ids(
    resolution: AnchorOccurrenceResolution,
    pack: CandidatePack,
    semantic_binding_sources: tuple[ValueSource, ...],
) -> frozenset[str]:
    """Find occurrence candidates bound to an independently exact semantic anchor.

    Candidate context alone is a model hint, not proof of component ownership.
    A correction is locally verifiable only when a unique component-name or
    related-fact anchor shares the candidate's conservative source unit.  If
    no such anchor exists, or several occurrences share those units, callers
    must fail closed instead of accepting a merely distinct position.
    """

    if resolution.status != "ambiguous":
        return frozenset()
    block = next(
        (
            item
            for item in pack.blocks
            if item.block_id == resolution.request.source_block_id
        ),
        None,
    )
    if block is None:
        return frozenset()
    binding_units = {
        _local_source_unit(block.text, source.start_char, source.end_char)
        for source in semantic_binding_sources
        if source.source_block_id == block.block_id
    }
    return frozenset(
        candidate.candidate_id
        for candidate in resolution.candidates
        if _local_source_unit(
            block.text, candidate.start_char, candidate.end_char
        ) in binding_units
    )


def resolve_value_anchor_with_occurrence_resolver(
    fact: "SourceSelectedFact | SourceSelectedFactV02",
    pack: CandidatePack,
    *,
    common_ir_source_sha256: str,
    correction_resolver: AnchorCorrectionResolver,
    semantic_binding_sources: tuple[ValueSource, ...] = (),
    require_objective_semantic_binding: bool = False,
) -> tuple[str, "ValueSource"]:
    """Resolve one fact's value_anchor through Anchor Occurrence Resolver v1.

    Called only after the plain exact-span path (``materialize_value_source``)
    has already failed for this fact -- a unique anchor never reaches here.
    A genuinely unique anchor (e.g. the plain path failed for an unrelated
    reason) still resolves with no candidate IDs generated.  A repeated
    anchor is resolved by asking the same LLM to choose exactly one
    server-issued candidate_id from compact, offset-free context; an unknown
    or wrong choice fails closed via ``AmbiguousAnchorCorrectionError`` --
    there is no first-occurrence fallback.
    """

    anchor = fact.value_anchor
    request = AnchorOccurrenceRequest(
        common_ir_document_id=pack.common_ir_document_id,
        common_ir_source_sha256=common_ir_source_sha256,
        candidate_pack_id=pack.pack_id,
        candidate_pack_generator=pack.generator,
        candidate_pack_generator_version=pack.generator_version,
        source_block_id=anchor.source_block_id,
        anchor_text=anchor.anchor_text,
    )
    resolution = resolve_anchor_occurrences(
        request, pack, trusted_common_ir_source_sha256=common_ir_source_sha256
    )
    if resolution.status == "unique":
        source = resolution.value_source
        assert source is not None
        block_text = {block.block_id: block.text for block in pack.blocks}[source.source_block_id]
        return block_text[source.start_char : source.end_char], source

    correction_request = build_anchor_correction_request(
        fact.fact_id,
        fact.field_name,
        resolution,
        primary_component_id=fact.primary_component_id,
        applicability_component_ids=tuple(fact.applicability_component_ids),
        context_source_block_ids=tuple(fact.context_source_block_ids),
        modifies_fact_ids=tuple(fact.modifies_fact_ids),
        recipient_fact_ids=tuple(fact.recipient_fact_ids),
        basis_fact_ids=tuple(fact.basis_fact_ids),
    )
    try:
        selected_candidate_id = correction_resolver(correction_request)
    except Exception as error:
        # Any failure of the injected resolver itself (API/network error,
        # incomplete response, structured-output parse/validation failure,
        # ...) -- as opposed to a resolver that ran and returned an unknown
        # candidate_id, below.  Never re-raised as-is: a bare exception here
        # could be a ValueError (e.g. pydantic's ValidationError) and would
        # then be indistinguishable from an ordinary contract-validation
        # failure to a caller matching on ValueError.
        raise CorrectionResolverError(
            fact_id=fact.fact_id,
            source_block_id=anchor.source_block_id,
            anchor_text=anchor.anchor_text,
            candidate_count=len(resolution.candidates),
            error_type=type(error).__name__,
        ) from error
    try:
        materialized = materialize_selected_anchor_candidate(
            resolution, selected_candidate_id
        )
    except ValueError as error:
        raise AmbiguousAnchorCorrectionError(
            fact_id=fact.fact_id,
            source_block_id=anchor.source_block_id,
            anchor_text=anchor.anchor_text,
            candidate_count=len(resolution.candidates),
            reason=str(error),
        ) from error
    if require_objective_semantic_binding:
        objectively_bound_ids = _objectively_bound_candidate_ids(
            resolution, pack, semantic_binding_sources
        )
        if objectively_bound_ids != frozenset({selected_candidate_id}):
            raise AmbiguousAnchorCorrectionError(
                fact_id=fact.fact_id,
                source_block_id=anchor.source_block_id,
                anchor_text=anchor.anchor_text,
                candidate_count=len(resolution.candidates),
                reason="selected occurrence is not uniquely bound to local semantic evidence",
            )
    return materialized


def memoize_anchor_correction_resolver(resolver: AnchorCorrectionResolver) -> AnchorCorrectionResolver:
    """Cache one correction choice per fact-specific deterministic resolution.

    The same fact can reach finalization more than once because
    ``apply_finalize_with_fallback_v02`` can run
    ``materialize_evidence`` twice for what is otherwise the very same
    ambiguous anchor -- once for its merged attempt, once for its unmerged
    fallback -- and a repeat call for an unchanged resolution must reuse the
    first server-issued choice rather than asking the correction model again.
    Distinct facts are *not* interchangeable, even if their literal anchor is
    identical: two support-cap occurrences must be able to select two distinct
    server candidates and the uniqueness guard then verifies that they did.
    """

    cache: dict[tuple[object, ...], str] = {}

    def resolve(request: AnchorCorrectionRequest) -> str:
        key = (
            request.fact_id,
            request.field_name,
            request.source_block_id,
            request.anchor_text,
            request.primary_component_id,
            request.applicability_component_ids,
            request.context_source_block_ids,
            request.modifies_fact_ids,
            request.recipient_fact_ids,
            request.basis_fact_ids,
        )
        if key not in cache:
            cache[key] = resolver(request)
        return cache[key]

    return resolve


def build_corrected_anchor_audit(
    evidence: list["MaterializedEvidence"],
    correction_audit: dict[str, dict[str, object]],
) -> list[dict[str, object]]:
    """Safe, candidate_id-free audit rows for facts that needed correction.

    ``correction_audit`` maps fact_id to the ``{source_block_id,
    candidate_count}`` recorded when that fact's ambiguous anchor was sent
    for correction; it may carry stale entries from a discarded attempt (for
    example a merged attempt that ``apply_finalize_with_fallback_v02``
    ultimately fell back away from).  Only fact_ids that also survive into
    the final materialized ``evidence`` are reported, each paired with that
    final, resolver-verified ``value_source`` -- never a ``candidate_id``.
    """

    evidence_by_fact_id = {row.fact_id: row for row in evidence}
    audit: list[dict[str, object]] = []
    for fact_id, info in correction_audit.items():
        row = evidence_by_fact_id.get(fact_id)
        if row is None or row.value_source is None:
            continue
        audit.append({
            "fact_id": fact_id,
            "source_block_id": info["source_block_id"],
            "candidate_count": info["candidate_count"],
            "value_source": row.value_source.model_dump(mode="json"),
        })
    return sorted(audit, key=lambda entry: entry["fact_id"])


class NumericCandidate(StrictModel):
    """Server-enumerated numeric span available for one scale measure.

    ``start_char``/``end_char`` are this candidate's own exact character
    offsets in its source block's canonical text.  A support_scale fact must
    bind only to a numeric candidate whose span falls inside that fact's own
    *resolved* value_source span -- never merely one whose ``anchor_text``
    happens to match, which cannot distinguish two occurrences of the same
    number elsewhere in the same block.
    """

    numeric_candidate_id: str = Field(min_length=1)
    source_block_id: str = Field(min_length=1)
    anchor_text: str = Field(min_length=1)
    start_char: int = Field(ge=0)
    end_char: int = Field(gt=0)


_GROUPED_DECIMAL_NUMBER = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
_GROUPED_INTEGER_NUMBER = r"(?:\d{1,3}(?:,\d{3})+|\d+)"
_POSITIONAL_COUNT_NUMBER = (
    rf"(?:{_GROUPED_INTEGER_NUMBER}[ \t]*만"
    rf"(?:[ \t]*{_GROUPED_INTEGER_NUMBER}[ \t]*천)?"
    rf"(?:[ \t]*{_GROUPED_INTEGER_NUMBER}[ \t]*백)?"
    rf"(?:[ \t]*{_GROUPED_INTEGER_NUMBER}[ \t]*십)?"
    rf"(?:[ \t]*{_GROUPED_INTEGER_NUMBER})?"
    rf"|{_GROUPED_INTEGER_NUMBER}[ \t]*천"
    rf"(?:[ \t]*{_GROUPED_INTEGER_NUMBER}[ \t]*백)?"
    rf"(?:[ \t]*{_GROUPED_INTEGER_NUMBER}[ \t]*십)?"
    rf"(?:[ \t]*{_GROUPED_INTEGER_NUMBER})?"
    rf"|{_GROUPED_INTEGER_NUMBER}[ \t]*백"
    rf"(?:[ \t]*{_GROUPED_INTEGER_NUMBER}[ \t]*십)?"
    rf"(?:[ \t]*{_GROUPED_INTEGER_NUMBER})?"
    rf"|{_GROUPED_INTEGER_NUMBER}[ \t]*십"
    rf"(?:[ \t]*{_GROUPED_INTEGER_NUMBER})?"
    rf"|{_GROUPED_INTEGER_NUMBER})"
)
_COUNT_SOURCE = rf"(?P<number>{_POSITIONAL_COUNT_NUMBER})[ \t]*(?P<unit>개사|개팀|개[ \t]*과제|명|팀|사)"
_NUMERIC_TOKEN_PREFIX = r"(?<![\d.])(?<!\d,)(?<!\d，)(?<![,，][,，])"
_NUMERIC_TOKEN_SUFFIX = (
    r"(?!\d|[,，]\d|\.\d)"
    # Do not let the regex backtrack from a malformed/foreign-currency large
    # amount and emit a valid-looking prefix such as ``5억`` from
    # ``5억원달러`` or ``1억`` from ``1억 5000만원 USD``.
    r"(?![ \t]*(?:원|만[ \t]*원))"
    rf"(?![ \t]*{_GROUPED_DECIMAL_NUMBER}[ \t]*(?:천|백)?[ \t]*만(?:[ \t]*원)?)"
)
_NUMERIC_CANDIDATE_PATTERN = re.compile(
    rf"{_NUMERIC_TOKEN_PREFIX}(?:{_COUNT_SOURCE}|"
    rf"{KOREAN_KRW_MONEY_SOURCE}|"
    rf"{_GROUPED_DECIMAL_NUMBER}[ \t]*%){_NUMERIC_TOKEN_SUFFIX}"
)

_AMOUNT_PATTERN = re.compile(KOREAN_KRW_MONEY_SOURCE)
_RATE_PATTERN = re.compile(
    rf"(?P<number>{_GROUPED_DECIMAL_NUMBER})[ \t]*%"
)
_COUNT_PATTERN = re.compile(_COUNT_SOURCE)
# A zero-filled recipient count in a notice template is not an announced
# selection capacity.  Keep this deliberately narrow: legitimate monetary
# zeroes and non-recipient numeric values are outside this policy, while a
# source-visible ``400개사`` remains an ordinary numeric candidate.
_NUMERIC_PLACEHOLDER_PATTERN = re.compile(
    r"^\s*0+\s*(?:개사|개소|명)\s*$"
)
_NUMERIC_PLACEHOLDER_OCCURRENCE_PATTERN = re.compile(
    r"0+[ \t]*(?:개사|개소|명)(?!\d)"
)
_MALFORMED_COUNT_SUFFIX_PREFIX = re.compile(
    r"(?:\d[ \t]*|\d[,，._/／．]+[ \t]*)$"
)
_GROUPED_NUMBER_WITH_COMMA = re.compile(
    r"(?<![\d,，])(?P<number>\d[\d,，]*[,，][\d,，]*(?:\.\d+)?)"
    r"(?![\d,，])(?=[ \t]*(?:억|천|백|만|원))"
)
_PRECEDING_MALFORMED_MONEY_FRAGMENT = re.compile(
    r"(?:"
    r"[0-9,，.]+(?:[ \t]*[억천백십만원][ \t]*[0-9,，.]*)*"
    r"[억천백십만원][ \t]*"
    r"|[0-9,，.]+[ \t]*[천백십][ \t]*[,，]+[ \t]*"
    r"|[0-9][0-9,，.]*[,，][ \t]+"
    r"|[0-9][0-9,，.]*[ \t]+"
    r"|[0-9][0-9,，.]*[/_][ \t]*"
    r")$"
)
_PRECEDING_MALFORMED_COUNT_FRAGMENT = re.compile(
    r"(?:"
    r"[0-9][0-9,，.]*[,，][ \t]+"
    r"|[0-9][0-9,，.]*[ \t]+"
    r"|[0-9][0-9,，.]*[/_][ \t]*"
    r"|[0-9][0-9,，.]*(?:[ \t]*(?:만|천|백|십)[ \t]*[0-9,，.]*)*"
    r"[ \t]*(?:만|천|백|십)[ \t]*"
    r")$"
)
_FOLLOWING_LINE_MONEY_AMOUNT = re.compile(
    rf"^\r?\n[ \t]*(?:{KOREAN_KRW_MONEY_SOURCE})"
)


def is_numeric_placeholder(text: str) -> bool:
    """Recognize explicit zero-filled recipient-count placeholders only."""

    return bool(_NUMERIC_PLACEHOLDER_PATTERN.fullmatch(text))


def contains_numeric_placeholder(text: str) -> bool:
    """Return whether a selected span contains a standalone zero count.

    A ``0명`` can be a template placeholder after ordinary prose punctuation,
    but the zero suffix in malformed larger counts (``1,  000명``) is not.
    Inspecting each occurrence's full preceding text avoids a fixed-width
    lookbehind that would otherwise confuse those two cases.
    """

    return any(
        not _MALFORMED_COUNT_SUFFIX_PREFIX.search(text[:match.start()])
        for match in _NUMERIC_PLACEHOLDER_OCCURRENCE_PATTERN.finditer(text)
    )


def _has_line_broken_compound_money(text: str) -> bool:
    """Detect an ambiguous large-unit amount split at a physical line."""

    return any(
        _FOLLOWING_LINE_MONEY_AMOUNT.match(text[match.end():])
        for match in _AMOUNT_PATTERN.finditer(text)
    )


def _decimal_to_int(value: Decimal) -> int | None:
    """Return an exact integer representation, or fail closed."""

    if value != value.to_integral_value():
        return None
    return int(value)


def _amount_krw(match: re.Match[str]) -> int | None:
    """Normalize a Korean won amount without binary floating-point arithmetic."""

    try:
        token = re.sub(r"[ \t]", "", match.group(0))
        if token.endswith("원"):
            token = token[:-1]
        has_large_unit = "억" in token or "만" in token

        value = Decimal(0)
        if "억" in token:
            eok_number, token = token.split("억", 1)
            value = Decimal(eok_number.replace(",", "")) * 100_000_000

        def positional_coefficient(text: str) -> Decimal | None:
            if not text:
                return None
            cursor = 0
            coefficient = Decimal(0)
            unit_factors = {"천": 1_000, "백": 100, "십": 10, None: 1}
            for term in re.finditer(
                rf"(?P<number>{_GROUPED_DECIMAL_NUMBER})(?P<unit>천|백|십)?",
                text,
            ):
                if term.start() != cursor:
                    return None
                coefficient += Decimal(
                    term.group("number").replace(",", "")
                ) * unit_factors[term.group("unit")]
                cursor = term.end()
            return coefficient if cursor == len(text) else None

        if "만" in token:
            man_text, token = token.split("만", 1)
            man_coefficient = positional_coefficient(man_text)
            if man_coefficient is None:
                return None
            value += man_coefficient * 10_000
            # ``3억 2천만원 500원`` may retain the first explicit 원 as a
            # delimiter before its smaller won remainder.
            if token.startswith("원"):
                token = token[1:]

        if token:
            won_coefficient = positional_coefficient(token)
            if won_coefficient is None:
                return None
            value += won_coefficient
        elif not has_large_unit:
            # A bare ``N억`` legitimately has no remainder. Any other empty
            # token would violate the shared amount grammar and fails closed.
            if "억" not in match.group(0):
                return None
    except InvalidOperation:
        return None
    return _decimal_to_int(value)


def _rate_bps(match: re.Match[str]) -> int | None:
    """Normalize a percent expression to exact basis points."""

    try:
        value = Decimal(match.group("number").replace(",", "")) * 100
    except InvalidOperation:
        return None
    return _decimal_to_int(value)


def _count_value(match: re.Match[str]) -> int | None:
    """Normalize a source-visible Korean positional recipient count."""

    token = re.sub(r"[ \t,]", "", match.group("number"))
    if token.isdigit():
        return int(token)
    cursor = 0
    value = 0
    last_factor = 100_000
    factors = {"만": 10_000, "천": 1_000, "백": 100, "십": 10, None: 1}
    for term in re.finditer(r"(?P<number>\d+)(?P<unit>만|천|백|십)?", token):
        if term.start() != cursor:
            return None
        factor = factors[term.group("unit")]
        if factor >= last_factor:
            return None
        value += int(term.group("number")) * factor
        last_factor = factor
        cursor = term.end()
    return value if cursor == len(token) else None


def normalize_numeric_candidate_token(
    anchor_text: str,
) -> tuple[str, int, str] | None:
    """Normalize one complete production numeric token, or fail closed.

    This is the public counterpart to the numeric-candidate scanner.  Keeping
    amount, rate, and Korean positional-count normalization behind the same
    helper lets offline validators prove the exact contract used by profile
    generation instead of maintaining a weaker copy of its grammar.
    """

    amount = _AMOUNT_PATTERN.fullmatch(anchor_text)
    if amount is not None:
        value = _amount_krw(amount)
        return ("amount", value, "KRW") if value is not None else None

    rate = _RATE_PATTERN.fullmatch(anchor_text)
    if rate is not None:
        value = _rate_bps(rate)
        return ("rate", value, "BPS") if value is not None else None

    count = _COUNT_PATTERN.fullmatch(anchor_text)
    if count is not None:
        value = _count_value(count)
        return (
            ("count", value, count.group("unit"))
            if value is not None
            else None
        )

    return None


def enumerate_numeric_candidate_spans(
    source_text: str,
) -> tuple[tuple[int, int, str], ...]:
    """Return production-valid ``(start, end, text)`` numeric spans.

    Besides the shared complete-token grammar, this applies the contextual
    malformed-prefix and line-broken-suffix guards used by profile generation.
    Each surviving token is guaranteed to normalize successfully through
    :func:`normalize_numeric_candidate_token`.
    """

    spans: list[tuple[int, int, str]] = []
    for match in _NUMERIC_CANDIDATE_PATTERN.finditer(source_text):
        anchor_text = match.group(0)
        if is_numeric_placeholder(anchor_text):
            continue
        normalized = normalize_numeric_candidate_token(anchor_text)
        if normalized is None:
            continue
        measure_type = normalized[0]
        # A malformed compound can contain a valid-looking inner amount or
        # count.  Such a suffix is not an independent candidate.
        if (
            measure_type == "amount"
            and _PRECEDING_MALFORMED_MONEY_FRAGMENT.search(
                source_text[:match.start()]
            )
        ):
            continue
        if (
            measure_type == "count"
            and _PRECEDING_MALFORMED_COUNT_FRAGMENT.search(
                source_text[:match.start()]
            )
        ):
            continue
        if (
            measure_type == "amount"
            and _FOLLOWING_LINE_MONEY_AMOUNT.match(source_text[match.end():])
        ):
            continue
        spans.append((match.start(), match.end(), anchor_text))
    return tuple(spans)

# A package/type/stage's own support facts: the fields a support component can
# own and that an explicit employment-condition variant can modify.  Shared by
# the package-completeness check and the variant-relation normalization below
# so both use one definition of "this component's support facts".
_PACKAGE_SUPPORT_FIELDS = {
    FactField.SUPPORT_ACTIVITIES,
    FactField.SUPPORT_METHODS,
    FactField.SUPPORT_ITEMS,
    FactField.SUPPORT_CONTENT,
    FactField.SUPPORT_SCALE,
    FactField.SUPPORT_PERIOD,
    FactField.PAYMENT_TERMS,
    FactField.COST_SHARING,
}

def build_numeric_candidates(pack: CandidatePack) -> list[NumericCandidate]:
    """Enumerate exact numeric expressions; semantics remain model-selected."""

    candidates: list[NumericCandidate] = []
    for block in pack.blocks:
        for index, (start_char, end_char, anchor_text) in enumerate(
            enumerate_numeric_candidate_spans(block.text)
        ):
            candidates.append(NumericCandidate(
                numeric_candidate_id=f"{block.block_id}#num[{index}]",
                source_block_id=block.block_id,
                anchor_text=anchor_text,
                start_char=start_char,
                end_char=end_char,
            ))
    return candidates


def build_explicit_support_cap_anchors(pack: CandidatePack) -> list[dict[str, str]]:
    """Expose exact cap spans as untrusted selection/repair hints.

    They are not Raw Facts and do not bypass materialization.  Position is
    deliberately retained only in the private candidate catalog; the model
    receives the existing public locator shape and must still select one fact
    per exact occurrence through the finalizer.
    """

    return [
        {"source_block_id": item.source_block_id, "anchor_text": item.anchor_text}
        for item in sorted(
            extract_explicit_support_cap_candidates(pack),
            key=lambda item: (item.source_block_id, item.start_char, item.end_char),
        )
    ]


def _numeric_candidate_within_value_source(candidate: NumericCandidate, resolved: ValueSource) -> bool:
    """A candidate binds to a fact only when fully inside its resolved span.

    Position, not text, is the identity of "this fact's own numeral": two
    occurrences of the same amount elsewhere in a block must never both bind
    to one fact merely because their text happens to match.
    """

    return (
        candidate.source_block_id == resolved.source_block_id
        and candidate.start_char >= resolved.start_char
        and candidate.end_char <= resolved.end_char
    )


def _numeric_candidate_is_per_unit_scope_label(
    candidate: NumericCandidate,
    text: str,
    *,
    text_start_char: int = 0,
) -> bool:
    """Keep a scope cardinality out of selection-capacity projections.

    In ``1명당 최대 5천만원`` the literal ``1명`` belongs to the PERSON
    scope marker.  It is not a promise to select one person.  The same closed
    vocabulary that derives ``applies_per`` owns this decision for both
    Existing and Request profiles.
    """

    return any(
        text_start_char + occurrence.start <= candidate.start_char
        and candidate.end_char <= text_start_char + occurrence.end
        for occurrence in per_unit_scope_occurrences(text)
    )


_LOCAL_NUMERIC_CONTEXT_BOUNDARY = re.compile(r"[,，;；:：\n。]|\.(?!\d)")
_LOCAL_LIMIT_PREFIX = re.compile(r"(?:최대|한도|상한)\s*$")
_LOCAL_MINIMUM_PREFIX = re.compile(r"(?:최소|하한)\s*$")
_LOCAL_APPROX_PREFIX = re.compile(r"약\s*$")
_LOCAL_LIMIT_SUFFIX = re.compile(r"^\s*(?:이내|이하|한도|상한)")
_LOCAL_STRICT_LIMIT_SUFFIX = re.compile(r"^\s*미만")
_LOCAL_MINIMUM_SUFFIX = re.compile(r"^\s*(?:이상|하한)")
_LOCAL_STRICT_MINIMUM_SUFFIX = re.compile(r"^\s*초과")
_LOCAL_APPROX_SUFFIX = re.compile(r"^\s*(?:내외|정도)")


def _numeric_candidate_local_context_v02(
    value_raw: str,
    candidates: list[NumericCandidate],
    index: int,
    *,
    value_start_char: int,
) -> tuple[str, str]:
    """Return the candidate's nearest non-numeric, delimiter-bounded context.

    A selected Raw Fact may faithfully retain both an eligibility threshold
    and the resulting benefit.  Context for one numeral therefore stops at a
    delimiter *or the neighbouring numeral*, whichever comes first.  This
    prevents ``10% 이상 감소기업: 1% 추가 지원`` from lending ``이상`` to the
    actual 1% benefit, and prevents the 10% eligibility threshold from being
    materialised as a support rate.
    """

    candidate = candidates[index]
    relative_start = candidate.start_char - value_start_char
    relative_end = candidate.end_char - value_start_char
    previous_end = (
        candidates[index - 1].end_char - value_start_char
        if index > 0 else 0
    )
    next_start = (
        candidates[index + 1].start_char - value_start_char
        if index + 1 < len(candidates) else len(value_raw)
    )
    boundaries = list(_LOCAL_NUMERIC_CONTEXT_BOUNDARY.finditer(value_raw))
    preceding_boundary = max(
        (match.end() for match in boundaries if match.end() <= relative_start),
        default=0,
    )
    following_boundary = min(
        (match.start() for match in boundaries if match.start() >= relative_end),
        default=len(value_raw),
    )
    context_start = max(previous_end, preceding_boundary)
    context_end = min(next_start, following_boundary)
    return (
        value_raw[context_start:relative_start],
        value_raw[relative_end:context_end],
    )


def _local_numeric_comparator_v02(prefix: str, suffix: str) -> Comparator:
    """Bind a literal comparator only to its adjacent numeric candidate."""

    if _LOCAL_STRICT_LIMIT_SUFFIX.match(suffix):
        return Comparator.LT
    if _LOCAL_LIMIT_SUFFIX.match(suffix):
        return Comparator.LTE
    if _LOCAL_STRICT_MINIMUM_SUFFIX.match(suffix):
        return Comparator.GT
    if _LOCAL_MINIMUM_SUFFIX.match(suffix):
        return Comparator.GTE
    if _LOCAL_APPROX_SUFFIX.match(suffix):
        return Comparator.APPROX
    if _LOCAL_LIMIT_PREFIX.search(prefix):
        return Comparator.LTE
    if _LOCAL_MINIMUM_PREFIX.search(prefix):
        return Comparator.GTE
    if _LOCAL_APPROX_PREFIX.search(prefix):
        return Comparator.APPROX
    return Comparator.EQ


_DIRECT_POST_NUMERIC_BENEFIT = re.compile(
    r"^\s*(?:(?:을|를|로|까지)\s*)?"
    r"(?:추가\s*지원|"
    r"지원(?!\s*(?:대상|자격|사업|분야|요건|조건|실적|이력))|"
    r"지급|보조|융자|보증)"
)
_SUPPORT_BASIS_PREFIX = re.compile(
    r"(?:지원(?:금|비|율|액)|보조(?:금)?|융자|보증).*?"
    r"(?:연\s*매출|매출(?:액)?|영업\s*이익)\s*(?:의|대비)\s*"
    r"(?:최대|한도|상한|약)?\s*$"
)


def _is_financial_eligibility_numeric_v02(
    value_raw: str,
    candidate: NumericCandidate,
    *,
    value_start_char: int,
    local_suffix: str,
) -> bool:
    """Separate applicant thresholds from a benefit in one faithful fact."""

    relative_start = candidate.start_char - value_start_char
    relative_end = candidate.end_char - value_start_char
    prefix = value_raw[:relative_start]
    suffix = value_raw[relative_end:]
    if _DIRECT_POST_NUMERIC_BENEFIT.match(suffix):
        return False

    benefit_owners = list(_SUPPORT_SCALE_BENEFIT_SIGNAL.finditer(prefix))
    eligibility_roles = list(_SUPPORT_SCALE_ELIGIBILITY_ROLE.finditer(prefix))
    latest_benefit_end = max(
        (match.end() for match in benefit_owners),
        default=-1,
    )
    latest_role_end = max(
        (match.end() for match in eligibility_roles),
        default=-1,
    )
    if (
        _SUPPORT_BASIS_PREFIX.search(prefix)
        and latest_benefit_end > latest_role_end
    ):
        return False

    eligibility = list(_SUPPORT_SCALE_ELIGIBILITY_METRIC.finditer(prefix))
    if not eligibility and _SUPPORT_SCALE_ELIGIBILITY_METRIC.search(local_suffix):
        return True
    if not eligibility:
        return False
    latest_eligibility_end = eligibility[-1].end()
    return latest_benefit_end <= latest_eligibility_end


def derive_support_scale_measures_v02(
    extraction: "SourceSelectionExtractionV02",
    candidates: list[NumericCandidate],
    resolved_value_sources: dict[str, ValueSource],
    *,
    source_block_texts: dict[str, str] | None = None,
) -> list[SupportScaleMeasuresProjection]:
    """Derive unambiguous numeric measures from verified Raw Fact spans.

    Numeric representation (KRW/BPS/count, bound operators, explicit monthly
    cadence) is deterministic once the semantic model has selected a
    ``support_scale`` span.  Keeping it server-side prevents a second
    interpretation task and preserves exact candidate provenance.  Ambiguous
    text simply yields no measure; Raw Facts are never altered.

    ``resolved_value_sources`` maps fact_id to that fact's final, server-
    verified ``ValueSource`` (from ``materialize_evidence``, so it reflects
    any CandidatePack Anchor Occurrence Resolver correction).  A numeric
    candidate is only bound to a fact when the candidate's own span falls
    inside that resolved span -- never merely because its text is contained
    in the fact's ``anchor_text`` -- so a repeated number elsewhere in the
    same block can never bind to the wrong fact.
    """

    measures: list[SupportScaleMeasure] = []
    candidates_by_block: dict[str, list[NumericCandidate]] = {}
    for candidate in candidates:
        candidates_by_block.setdefault(candidate.source_block_id, []).append(candidate)

    for fact in extraction.facts:
        if fact.field_name != FactField.SUPPORT_SCALE or fact.value_anchor is None:
            continue
        resolved = resolved_value_sources.get(fact.fact_id)
        if resolved is None:
            continue
        source_text = (
            source_block_texts.get(resolved.source_block_id)
            if source_block_texts else None
        )
        text = (
            source_text[resolved.start_char:resolved.end_char]
            if isinstance(source_text, str)
            else fact.value_anchor.anchor_text
        )
        # Table cells often contain only ``18억원`` while their row/column
        # header explicitly says ``업체당 지원한도``.  A support cap is then a
        # structural fact, not a guess from the numeric cell.  The runner
        # supplies candidate-pack text for the selected context blocks.
        context_text = "\n".join(
            source_block_texts.get(block_id, "")
            for block_id in fact.context_source_block_ids
        ) if source_block_texts else ""
        scope_text = source_text if isinstance(source_text, str) else text
        scope_text_start = 0 if isinstance(source_text, str) else resolved.start_char
        has_limit_context = bool(re.search(r"(?:지원|융자|보증)?\s*한도|상한", context_text))
        fact_candidates = sorted(
            (
                candidate
                for candidate in candidates_by_block.get(resolved.source_block_id, [])
                if _numeric_candidate_within_value_source(candidate, resolved)
            ),
            key=lambda candidate: (candidate.start_char, candidate.end_char),
        )
        for index, candidate in enumerate(fact_candidates):
            local_prefix, local_suffix = _numeric_candidate_local_context_v02(
                text,
                fact_candidates,
                index,
                value_start_char=resolved.start_char,
            )
            if _is_financial_eligibility_numeric_v02(
                text,
                candidate,
                value_start_char=resolved.start_char,
                local_suffix=local_suffix,
            ):
                continue
            amount = _AMOUNT_PATTERN.fullmatch(candidate.anchor_text)
            rate = _RATE_PATTERN.fullmatch(candidate.anchor_text)
            count = _COUNT_PATTERN.fullmatch(candidate.anchor_text)
            if count and _numeric_candidate_is_per_unit_scope_label(
                candidate,
                scope_text,
                text_start_char=scope_text_start,
            ):
                continue
            comparator = _local_numeric_comparator_v02(local_prefix, local_suffix)
            lower_value: int | None
            upper_value: int | None
            if comparator in {Comparator.LTE, Comparator.LT}:
                lower_value, upper_value = None, 0
            elif comparator in {Comparator.GTE, Comparator.GT}:
                lower_value, upper_value = 0, None
            else:
                lower_value = upper_value = 0

            if amount:
                value = _amount_krw(amount)
                if value is None:
                    continue
                measure_type = MeasureType.AMOUNT
                # A table header such as ``업체당 지원한도`` can establish a
                # limit even when the numeric cell itself is just ``18억원``.
                # The selected Raw Fact preserves that header-derived
                # semantic role; do not discard it merely because the value
                # span has no lexical marker like ``최대``.
                role = (
                    MeasureRole.SUPPORT_LIMIT
                    if (
                        comparator == Comparator.LTE
                        or fact.semantic_role == "support_limit"
                        or has_limit_context
                    )
                    else MeasureRole.SUPPORT_AMOUNT
                )
                unit = "KRW"
            elif rate:
                value = _rate_bps(rate)
                if value is None:
                    continue
                measure_type, role, unit = MeasureType.RATE, MeasureRole.SUPPORT_RATE, "BPS"
            elif count:
                value = _count_value(count)
                if value is None:
                    continue
                measure_type, role, unit = MeasureType.COUNT, MeasureRole.SELECTION_CAPACITY, count.group("unit")
            else:
                continue

            if comparator in {Comparator.LTE, Comparator.LT}:
                upper_value = value
            elif comparator in {Comparator.GTE, Comparator.GT}:
                lower_value = value
            else:
                lower_value = upper_value = value

            applies_per, aggregation_scope = parse_support_scale_scope(text)

            measures.append(SupportScaleMeasure(
                measure_type=measure_type,
                measure_role=role,
                lower_value=lower_value,
                upper_value=upper_value,
                unit=unit,
                comparator=comparator,
                source_fact_id=fact.fact_id,
                source_numeric_candidate_id=candidate.numeric_candidate_id,
                applies_per=applies_per,
                calculation_basis=(CalculationBasis.WAGE if re.search(r"(?:급여|임금)", text) else None),
                frequency=("MONTHLY" if re.search(r"(?:매월|월\s*\d)", text) else None),
                aggregation_scope=aggregation_scope,
            ))

    if not measures:
        return []
    return [SupportScaleMeasuresProjection(
        source_fact_ids=sorted({measure.source_fact_id for measure in measures}),
        measures=measures,
        status=SelectionStatus.IDENTIFIED,
    )]


_REQUEST_MARKER_PATTERN = re.compile(
    r"(?:총\s*|전체\s*)|최대|이내|한도|상한|내외|약|정도"
)
_REQUEST_LIMIT_PREFIX_MARKERS = frozenset({"최대"})
_REQUEST_LIMIT_SUFFIX_MARKERS = frozenset({"이내"})
_REQUEST_LIMIT_NEUTRAL_MARKERS = frozenset({"한도", "상한"})
_REQUEST_APPROX_PREFIX_MARKERS = frozenset({"약"})
_REQUEST_APPROX_SUFFIX_MARKERS = frozenset({"내외", "정도"})
_REQUEST_APPROX_MARKERS = (
    _REQUEST_APPROX_PREFIX_MARKERS | _REQUEST_APPROX_SUFFIX_MARKERS
)
# A period followed by a digit belongs to a decimal/date token; every other
# period ends a source clause, including a dotted date's final ``.``.
_REQUEST_CLAUSE_BOUNDARY = re.compile(r"[,，;；\n]|\.(?!\d)|。")
_REQUEST_ADJACENT_SCOPE_LABEL = re.compile(
    r"\s*(?:[-*○◦□■●▪‣ㅇ·]\s*)?"
    r"(?P<scope>[0-9A-Za-z가-힣社\s]+?(?:당|별))\s*"
    r"(?:(?:지원|보조|융자|보증)\s*)?"
    r"(?:한도|상한|지원금|지원액|금액)?\s*[:：]?\s*"
)


def parse_support_scale_scope(
    text: str,
    *,
    allow_person: bool = True,
    allow_total: bool = True,
) -> tuple[str | None, AggregationScope | None]:
    """Parse only literal, source-visible support-scale scope wording.

    Existing and Request profiles share the same literal scope grammar.  The
    normalized scope remains provenance-preserving metadata, not an inferred
    recipient: downstream matching may choose to ignore PERSON or TOTAL when
    it needs a comparable per-recipient limit.
    """

    per_unit_scope = explicit_per_unit_scope(text)
    if per_unit_scope is not None:
        if per_unit_scope == "PERSON" and not allow_person:
            return None, None
        return per_unit_scope, AggregationScope.PER_UNIT
    if allow_total and re.search(r"(?:총\s*|전체\s*)", text):
        return None, AggregationScope.TOTAL
    return None, None


def _request_marker_assignment(
    marker_text: str,
    marker_start: int,
    marker_end: int,
    candidates: list[NumericCandidate],
    *,
    value_raw: str,
    value_start_char: int,
) -> int | None:
    """Assign one literal modifier to its nearest exact numeral.

    Source facts may faithfully contain multiple quantities.  We therefore do
    not let a clause-level ``이내`` or ``최대`` leak to every number in that
    Raw Fact. Prefix terms (``기업당``, ``최대``) can bind only to a following
    number in the same delimiter-bounded clause; suffix terms (``이내``,
    ``내외``) only to a preceding one. Neutral terms use nearest-number
    resolution. This is a deterministic syntax rule, never a semantic guess
    from unselected context.
    """

    marker_text = marker_text.strip()
    prefix_marker = marker_text in (
        _REQUEST_LIMIT_PREFIX_MARKERS | _REQUEST_APPROX_PREFIX_MARKERS
    ) or explicit_per_unit_scope(marker_text) is not None or marker_text.startswith(
        ("총", "전체")
    )
    suffix_marker = marker_text in (
        _REQUEST_LIMIT_SUFFIX_MARKERS | _REQUEST_APPROX_SUFFIX_MARKERS
    )
    if prefix_marker:
        tie_direction = "following"
    elif suffix_marker:
        tie_direction = "preceding"
    else:  # ``한도``: nearest wins; a following numeral wins only on a tie.
        tie_direction = "following"

    candidate_spans = [
        (
            candidate.start_char - value_start_char,
            candidate.end_char - value_start_char,
        )
        for candidate in candidates
    ]
    clause_boundaries = [
        boundary.start()
        for boundary in _REQUEST_CLAUSE_BOUNDARY.finditer(value_raw)
        if not any(start <= boundary.start() < end for start, end in candidate_spans)
    ]
    marker_clause = sum(position < marker_start for position in clause_boundaries)
    ranked: list[tuple[int, int, int]] = []
    for index, candidate in enumerate(candidates):
        start = candidate.start_char - value_start_char
        end = candidate.end_char - value_start_char
        candidate_clause = sum(position < start for position in clause_boundaries)
        if candidate_clause != marker_clause:
            continue
        # Prefix and suffix modifiers are grammar, not merely proximity hints:
        # ``1억원 최대 지원비율 70%`` must bind 최대 to 70%, while
        # ``10% 이내`` must bind 이내 to 10%.  Only neutral terms (한도/상한)
        # use nearest-candidate resolution in either direction.
        if prefix_marker and start < marker_end:
            continue
        if suffix_marker and end > marker_start:
            continue
        if end <= marker_start:
            distance, direction = marker_start - end, "preceding"
        elif start >= marker_end:
            distance, direction = start - marker_end, "following"
        else:
            distance, direction = 0, "overlap"
        direction_penalty = 0 if direction == tie_direction or direction == "overlap" else 1
        ranked.append((distance, direction_penalty, index))
    if not ranked:
        return None
    return min(ranked)[2]


def _request_candidate_numeric_semantics(
    value_raw: str,
    candidates: list[NumericCandidate],
    *,
    value_start_char: int,
) -> dict[str, tuple[Comparator, str | None, AggregationScope | None]]:
    """Return candidate-local comparator and literal scope metadata."""

    markers_by_candidate: dict[int, list[str]] = {index: [] for index in range(len(candidates))}
    markers = [
        (marker.group(0), marker.start(), marker.end())
        for marker in _REQUEST_MARKER_PATTERN.finditer(value_raw)
    ]
    markers.extend(
        (occurrence.text, occurrence.start, occurrence.end)
        for occurrence in per_unit_scope_occurrences(value_raw)
    )
    for marker_text, marker_start, marker_end in sorted(
        markers, key=lambda row: (row[1], row[2], row[0])
    ):
        index = _request_marker_assignment(
            marker_text,
            marker_start,
            marker_end,
            candidates,
            value_raw=value_raw,
            value_start_char=value_start_char,
        )
        if index is not None:
            markers_by_candidate[index].append(marker_text.strip())

    semantics: dict[str, tuple[Comparator, str | None, AggregationScope | None]] = {}
    for index, candidate in enumerate(candidates):
        markers = markers_by_candidate[index]
        if any(marker in _REQUEST_LIMIT_PREFIX_MARKERS | _REQUEST_LIMIT_SUFFIX_MARKERS | _REQUEST_LIMIT_NEUTRAL_MARKERS for marker in markers):
            comparator = Comparator.LTE
        elif any(marker in _REQUEST_APPROX_MARKERS for marker in markers):
            comparator = Comparator.APPROX
        else:
            comparator = Comparator.EQ
        scopes = {
            parse_support_scale_scope(marker)
            for marker in markers
            if parse_support_scale_scope(marker) != (None, None)
        }
        applies_per, aggregation_scope = next(iter(scopes)) if len(scopes) == 1 else (None, None)
        semantics[candidate.numeric_candidate_id] = (comparator, applies_per, aggregation_scope)
    return semantics


def _adjacent_request_scope_label(
    block_text: str,
    *,
    value_start_char: int,
) -> tuple[str | None, AggregationScope | None]:
    """Read one explicit scope label immediately before a selected value.

    Request selectors intentionally keep ``value_raw`` atomic, so a source row
    such as ``기업당 한도: 최대 5,000만원`` materializes only the amount
    phrase.  The per-company scope is nevertheless explicit provenance in the
    same block.  Accept it only when the delimiter-bounded prefix consists
    entirely of one closed scope label plus known bridge words.  Arbitrary
    earlier prose, multiple scopes, and cross-block context all fail closed.
    """

    prefix = block_text[:value_start_char]
    boundaries = list(_REQUEST_CLAUSE_BOUNDARY.finditer(prefix))
    clause = prefix[boundaries[-1].end() :] if boundaries else prefix
    match = _REQUEST_ADJACENT_SCOPE_LABEL.fullmatch(clause)
    if match is None:
        return None, None
    scope = explicit_per_unit_scope(
        match.group("scope"),
        require_full_text=True,
    )
    if scope is None:
        return None, None
    return scope, AggregationScope.PER_UNIT


def derive_request_support_scale_measures_v012(
    support_scale_facts: list[dict[str, object]],
    candidates: list[NumericCandidate],
    *,
    source_block_texts: dict[str, str],
) -> list[SupportScaleMeasuresProjection]:
    """Derive Request numeric projections from already materialized Raw Facts.

    The Request contract has no trusted semantic-role signal.  A
    ``support_limit`` is therefore emitted only where the numeric span's own
    exact Raw-Fact clause says ``최대``, ``이내``, ``한도``, or ``상한``.  In
    particular, this function never derives an amount by dividing a total
    budget by a selection count, and never reads an unselected context block.
    It may preserve one literal per-unit scope from a closed, delimiter-bounded
    label immediately before an otherwise atomic selected value in the same
    verified source block.

    Each candidate must be an exact numeric span inside a validated
    ``value_source``.  Invalid or ambiguous caller data produces no projection
    for that fact; final profile validation repeats these provenance checks and
    fails closed before a profile can be emitted.
    """

    candidates_by_block: dict[str, list[NumericCandidate]] = {}
    for candidate in candidates:
        block_text = source_block_texts.get(candidate.source_block_id)
        if (
            block_text is None
            or candidate.end_char > len(block_text)
            or block_text[candidate.start_char:candidate.end_char] != candidate.anchor_text
        ):
            continue
        candidates_by_block.setdefault(candidate.source_block_id, []).append(candidate)

    measures: list[SupportScaleMeasure] = []
    for fact in support_scale_facts:
        fact_id = fact.get("fact_id")
        value_raw = fact.get("value_raw")
        raw_source = fact.get("value_source")
        if not isinstance(fact_id, str) or not fact_id or not isinstance(value_raw, str):
            continue
        try:
            resolved = ValueSource.model_validate(raw_source)
        except ValueError:
            continue
        block_text = source_block_texts.get(resolved.source_block_id)
        if (
            block_text is None
            or resolved.end_char > len(block_text)
            or block_text[resolved.start_char:resolved.end_char] != value_raw
        ):
            continue

        fact_candidates = [
            candidate
            for candidate in candidates_by_block.get(resolved.source_block_id, [])
            if _numeric_candidate_within_value_source(candidate, resolved)
        ]
        candidate_semantics = _request_candidate_numeric_semantics(
            value_raw,
            fact_candidates,
            value_start_char=resolved.start_char,
        )
        adjacent_scope = (
            _adjacent_request_scope_label(
                block_text,
                value_start_char=resolved.start_char,
            )
            if len(fact_candidates) == 1
            else (None, None)
        )
        for candidate in fact_candidates:
            comparator, applies_per, aggregation_scope = candidate_semantics[
                candidate.numeric_candidate_id
            ]
            if (applies_per, aggregation_scope) == (None, None):
                applies_per, aggregation_scope = adjacent_scope
            amount = _AMOUNT_PATTERN.fullmatch(candidate.anchor_text)
            rate = _RATE_PATTERN.fullmatch(candidate.anchor_text)
            count = _COUNT_PATTERN.fullmatch(candidate.anchor_text)
            if count and _numeric_candidate_is_per_unit_scope_label(
                candidate,
                block_text,
            ):
                continue
            if amount:
                value = _amount_krw(amount)
                if value is None:
                    continue
                measure_type = MeasureType.AMOUNT
                measure_role = (
                    MeasureRole.SUPPORT_LIMIT
                    if comparator == Comparator.LTE
                    else MeasureRole.SUPPORT_AMOUNT
                )
                unit = "KRW"
            elif rate:
                value = _rate_bps(rate)
                if value is None:
                    continue
                measure_type = MeasureType.RATE
                measure_role = MeasureRole.SUPPORT_RATE
                unit = "BPS"
            elif count:
                value = _count_value(count)
                if value is None:
                    continue
                measure_type = MeasureType.COUNT
                measure_role = MeasureRole.SELECTION_CAPACITY
                unit = count.group("unit")
            else:
                continue

            measures.append(SupportScaleMeasure(
                measure_type=measure_type,
                measure_role=measure_role,
                lower_value=None if comparator == Comparator.LTE else value,
                upper_value=value,
                unit=unit,
                comparator=comparator,
                source_fact_id=fact_id,
                source_numeric_candidate_id=candidate.numeric_candidate_id,
                applies_per=applies_per,
                aggregation_scope=aggregation_scope,
            ))

    if not measures:
        return []
    measures.sort(key=lambda measure: (
        measure.source_fact_id,
        measure.source_numeric_candidate_id,
    ))
    return [SupportScaleMeasuresProjection(
        source_fact_ids=sorted({measure.source_fact_id for measure in measures}),
        measures=measures,
        status=SelectionStatus.IDENTIFIED,
    )]


class SourceSelectedFact(StrictModel):
    """A semantic field claim that never contains LLM-written source text."""

    fact_id: str = Field(min_length=1)
    field_name: FactField
    # ``source_block_ids`` is the legacy v0.2/v0.3 spelling.  New selections
    # separate exact persisted value cells from supporting headers/context.
    source_block_ids: list[str] = Field(default_factory=list)
    value_source_block_ids: list[str] = Field(default_factory=list)
    # v0.2 locator.  It is never persisted: the server resolves it to
    # value_raw + ValueSource before final assembly.
    value_anchor: SourceTextAnchor | None = None
    context_source_block_ids: list[str] = Field(default_factory=list)
    status: SelectionStatus
    semantic_role: str | None = None
    subject_role: str | None = None
    organization_anchors: list[SourceTextAnchor] = Field(default_factory=list)
    role_anchor: SourceTextAnchor | None = None
    canonical_role: DeliveryRoleCanonical | None = None
    # ``support_component_id`` is retained only to read v0.2/v0.3 artifacts.
    # New selections use one primary owner plus optional applicability context.
    support_component_id: str | None = None
    primary_component_id: str | None = None
    applicability_component_ids: list[str] = Field(default_factory=list)
    modifies_fact_ids: list[str] = Field(default_factory=list)
    recipient_fact_ids: list[str] = Field(default_factory=list)
    basis_fact_ids: list[str] = Field(default_factory=list)

    @property
    def resolved_value_source_block_ids(self) -> list[str]:
        if self.value_anchor is not None:
            return [self.value_anchor.source_block_id]
        return self.value_source_block_ids or self.source_block_ids

    @model_validator(mode="after")
    def requires_exact_value_source(self) -> "SourceSelectedFact":
        if not self.resolved_value_source_block_ids:
            raise ValueError("a fact requires value_source_block_ids (or legacy source_block_ids)")
        if self.value_anchor is not None and (self.source_block_ids or self.value_source_block_ids):
            raise ValueError("value_anchor must not be mixed with block-level value sources")
        return self

    @model_validator(mode="after")
    def delivery_role_requires_named_organization_anchors(self) -> "SourceSelectedFact":
        if self.field_name == FactField.DELIVERY_ROLES:
            if not self.organization_anchors or self.role_anchor is None:
                raise ValueError("delivery_roles requires organization_anchors and role_anchor")
            anchor_block_ids = {anchor.source_block_id for anchor in self.organization_anchors}
            source_block_ids = set(self.resolved_value_source_block_ids) | set(self.context_source_block_ids)
            if not (anchor_block_ids | {self.role_anchor.source_block_id}) <= source_block_ids:
                raise ValueError(
                    "delivery_roles organization/role anchors must reference one of the fact source_block_ids"
                )
        elif self.organization_anchors or self.role_anchor or self.canonical_role:
            raise ValueError("organization anchors, role_anchor, and canonical_role are only allowed for delivery_roles")
        return self

    @model_validator(mode="after")
    def program_period_is_notice_scoped(self) -> "SourceSelectedFact":
        if self.field_name == FactField.PROGRAM_PERIOD and (self.primary_component_id or self.support_component_id):
            raise ValueError("program_period must not be owned by a support component")
        return self


class SourceSelectedFactV02(StrictModel):
    """v0.2 write-only selection shape; legacy block values are impossible."""

    fact_id: str = Field(min_length=1)
    field_name: FactField
    value_anchor: SourceTextAnchor
    context_source_block_ids: list[str] = Field(default_factory=list)
    status: SelectionStatus
    semantic_role: str | None = None
    subject_role: BeneficiarySubjectRole | None = None
    organization_anchors: list[SourceTextAnchor] = Field(default_factory=list)
    role_anchor: SourceTextAnchor | None = None
    canonical_role: DeliveryRoleCanonical | None = None
    primary_component_id: str | None = None
    applicability_component_ids: list[str] = Field(default_factory=list)
    modifies_fact_ids: list[str] = Field(default_factory=list)
    recipient_fact_ids: list[str] = Field(default_factory=list)
    basis_fact_ids: list[str] = Field(default_factory=list)

    @property
    def source_block_ids(self) -> list[str]:
        return []

    @property
    def value_source_block_ids(self) -> list[str]:
        return []

    @property
    def support_component_id(self) -> None:
        return None

    @property
    def resolved_value_source_block_ids(self) -> list[str]:
        return [self.value_anchor.source_block_id]

    @model_validator(mode="after")
    def validates_v02_fact(self) -> "SourceSelectedFactV02":
        if self.field_name == FactField.DELIVERY_ROLES:
            if not self.organization_anchors or self.role_anchor is None:
                raise ValueError("delivery_roles requires organization_anchors and role_anchor")
            source_ids = set(self.resolved_value_source_block_ids) | set(self.context_source_block_ids)
            anchor_ids = {anchor.source_block_id for anchor in self.organization_anchors} | {self.role_anchor.source_block_id}
            if not anchor_ids <= source_ids:
                raise ValueError("delivery role anchors must be within value/context source blocks")
        elif self.organization_anchors or self.role_anchor or self.canonical_role:
            raise ValueError("organization anchors, role_anchor, and canonical_role are only allowed for delivery_roles")
        if self.subject_role is not None and self.field_name != FactField.BENEFICIARY:
            raise ValueError("subject_role is only allowed for beneficiary facts")
        if self.field_name == FactField.PROGRAM_PERIOD and self.primary_component_id:
            raise ValueError("program_period must not be owned by a support component")
        return self


_LAYOUT_OR_ENUMERATION_PREFIX = re.compile(
    r"^\s*(?:(?:[-·◦□○●▪•※])|(?:[①-⑳])|(?:\d+[.)]))\s*"
)
_STANDALONE_LAYOUT_OR_REFERENCE = re.compile(
    r"^\s*(?:(?:[-·◦□○●▪•※])|(?:[①-⑳](?:-\d+)?)|(?:\d+[.)]))\s*$"
)
_PAYMENT_OR_SETTLEMENT = re.compile(r"(?:지급|입금|정산|지불|송금|납부|결제|계좌)")
_SUPPORT_SEMANTIC_FIELDS = frozenset({
    FactField.SUPPORT_ACTIVITIES,
    FactField.SUPPORT_METHODS,
    FactField.SUPPORT_ITEMS,
    FactField.SUPPORT_CONTENT,
})


def _semantic_value_key(value: str) -> str:
    """Normalize visual list decoration, not semantic source content."""

    value = _LAYOUT_OR_ENUMERATION_PREFIX.sub("", value)
    value = unicodedata.normalize("NFKC", value)
    return re.sub(r"[\s*]+", "", value).casefold()


def normalize_semantic_duplicate_facts_v02(
    extraction: "SourceSelectionExtractionV02",
    pack: CandidatePack,
) -> tuple["SourceSelectionExtractionV02", list[dict[str, str]]]:
    """Remove layout-only support duplicates in the same explicit scope.

    This is intentionally narrower than text deduplication: equal-looking
    content in different support components or applicability scopes can be a
    real distinct benefit and remains untouched.  Existing CandidatePack v1
    has no rich composite-span rank; retain the most specific/non-bullet and
    longest literal source anchor deterministically.
    """

    pack_blocks = {block.block_id: block for block in pack.blocks}
    grouped: dict[tuple[object, ...], list[SourceSelectedFactV02]] = {}
    for fact in extraction.facts:
        if fact.field_name not in _SUPPORT_SEMANTIC_FIELDS:
            continue
        block = pack_blocks.get(fact.value_anchor.source_block_id)
        if block is None:
            continue
        occurrence_ids = tuple(
            block.common_ir_occurrence_ids or tuple(block.source_occurrence_ids)
        )
        # Without immutable shared-occurrence provenance, equal text can be
        # two legitimate facts at different places.  Leave it untouched and
        # let ordinary exact-span validation handle it.
        if not occurrence_ids:
            continue
        semantic_key = _semantic_value_key(fact.value_anchor.anchor_text)
        if not semantic_key:
            continue
        grouped.setdefault(
            (
                fact.field_name,
                pack.common_ir_document_id,
                occurrence_ids,
                fact.primary_component_id,
                tuple(sorted(fact.applicability_component_ids)),
                fact.status,
                fact.semantic_role,
                tuple(sorted(fact.modifies_fact_ids)),
                tuple(sorted(fact.recipient_fact_ids)),
                tuple(sorted(fact.basis_fact_ids)),
                semantic_key,
            ),
            [],
        ).append(fact)

    replacement: dict[str, str] = {}
    changes: list[dict[str, str]] = []
    for facts in grouped.values():
        if len(facts) < 2:
            continue
        # Equal literal anchors may denote repeated real occurrences.  This
        # normalization is only for two differently decorated projections of
        # the *same immutable occurrence* (for example a wrapper bullet plus
        # an atomic item), never for positional deduplication.
        if len({fact.value_anchor.anchor_text for fact in facts}) < 2:
            continue

        def rank(fact: SourceSelectedFactV02) -> tuple[int, int, int, str]:
            raw = fact.value_anchor.anchor_text
            meaningful_enumeration = int(bool(re.match(r"^\s*(?:[①-⑳]|\d+[.)])", raw)))
            layout_bullet = int(bool(re.match(r"^\s*[-·◦□○●▪•※]", raw)))
            specific_field = int(fact.field_name != FactField.SUPPORT_CONTENT)
            return (meaningful_enumeration - layout_bullet, len(raw), specific_field, fact.fact_id)

        retained = max(facts, key=rank)
        for fact in facts:
            if fact.fact_id == retained.fact_id:
                continue
            replacement[fact.fact_id] = retained.fact_id
            changes.append({
                "kind": "layout_equivalent_support_fact_removed",
                "removed_fact_id": fact.fact_id,
                "retained_fact_id": retained.fact_id,
            })
    if not replacement:
        return extraction, []

    payload = extraction.model_dump(mode="json")
    payload["facts"] = [
        row for row in payload["facts"] if row["fact_id"] not in replacement
    ]
    for row in payload["facts"]:
        for relation_key in ("modifies_fact_ids", "recipient_fact_ids", "basis_fact_ids"):
            row[relation_key] = list(dict.fromkeys(
                replacement.get(value, value) for value in row[relation_key]
            ))
    for projection in (
        *payload.get("support_facets", []),
        *payload.get("support_scale_measures", []),
    ):
        if "source_fact_ids" in projection:
            projection["source_fact_ids"] = list(dict.fromkeys(
                replacement.get(value, value) for value in projection["source_fact_ids"]
            ))
    return SourceSelectionExtractionV02.model_validate(payload), changes


def validate_selection_quality_v02(
    extraction: "SourceSelectionExtractionV02",
    *,
    pack: CandidatePack | None = None,
) -> None:
    """Reject contract-invalid composite numeric anchors before assembly.

    This is deliberately a narrow provenance guard, not a semantic classifier:
    an explicitly duration-bearing anchor cannot be a ``support_scale`` value
    under the v0.2 field contract.  The LLM must select the atomic amount,
    rate, count, or limit span instead.  Keeping the check here gives a
    one-retry runner a concrete, non-answer-bearing correction signal.
    """

    standalone_facts = [
        fact.fact_id for fact in extraction.facts
        if _STANDALONE_LAYOUT_OR_REFERENCE.fullmatch(fact.value_anchor.anchor_text)
    ]
    if standalone_facts:
        raise ValueError(
            "standalone layout markers or reference labels cannot become active facts: "
            f"{sorted(standalone_facts)}"
        )

    # A duration must be an atomic one-to-three digit quantity.  Without the
    # digit boundaries, the tail of a calendar year (for example ``2025년``
    # in a programme title) is incorrectly read as a five-year duration.
    duration_pattern = re.compile(
        r"(?<!\d)(?:\d{1,3}|[일이삼사오육칠팔구십]+)(?!\d)"
        r"\s*(?:개월|개월간|년|년간|주|주간|일간)"
    )
    invalid = [
        fact
        for fact in extraction.facts
        if fact.field_name == FactField.SUPPORT_SCALE
        and duration_pattern.search(fact.value_anchor.anchor_text)
    ]
    scale_repair_records: dict[str, dict[str, object]] = {
        fact.fact_id: {
            "fact_id": fact.fact_id,
            "source_block_id": fact.value_anchor.source_block_id,
            "reason": "duration_bearing_anchor",
            "numeric_candidate_count": 0,
            "derived_measure_count": 0,
        }
        for fact in invalid
    }

    # ``00개사`` and similar zero-filled recipient counts are template
    # placeholders, not a confirmed recruitment/selection/support scale.
    # Treat them as a typed repair defect so the existing one-retry flow
    # removes the active fact and prevents it from being restored, while the
    # original source text remains available through the candidate pack.
    for fact in extraction.facts:
        if (
            fact.field_name == FactField.SUPPORT_SCALE
            and contains_numeric_placeholder(fact.value_anchor.anchor_text)
        ):
            scale_repair_records.setdefault(fact.fact_id, {
                "fact_id": fact.fact_id,
                "source_block_id": fact.value_anchor.source_block_id,
                "reason": "numeric_placeholder",
                "numeric_candidate_count": 0,
                "derived_measure_count": 0,
            })

    # A support scale count denotes how many recipients/projects are selected
    # or supported.  A count of classes, mentoring sessions, or other
    # activities is not a scale, even when the model anchors the whole phrase
    # (rather than bare ``N회``).  This is a field-contract guard, not a
    # document-specific keyword exception.
    activity_count_pattern = re.compile(
        r"(?:교육|훈련|멘토링|컨설팅|상담|워크숍|세미나|설명회|프로그램|강의|실습|행사)"
        r"[^\n]{0,24}?\d+\s*(?:회|회차)"
    )
    activity_count = [
        fact
        for fact in extraction.facts
        if fact.field_name == FactField.SUPPORT_SCALE
        and (
            # ``각 1회`` / ``총 2회`` are still activity/service frequencies.
            # The earlier bare-number pattern missed these common table-cell
            # forms, allowing them to become support-scale facts.
            re.fullmatch(r"\s*(?:(?:각|총)\s*)?\d+\s*(?:회|회차)\s*", fact.value_anchor.anchor_text)
            or activity_count_pattern.search(fact.value_anchor.anchor_text)
        )
    ]
    for fact in activity_count:
        scale_repair_records.setdefault(fact.fact_id, {
            "fact_id": fact.fact_id,
            "source_block_id": fact.value_anchor.source_block_id,
            "reason": "activity_count_not_support_scale",
            "numeric_candidate_count": 0,
            "derived_measure_count": 0,
        })

    # A beneficiary must be an actual person, organization, or enterprise.
    # Calculation units and payment channels can describe a benefit but cannot
    # themselves receive it.
    non_entity_beneficiary = re.compile(r"(?:\d+\s*인당|\d+\s*명당|기업당|팀당|과제당|계좌|입금)")
    invalid_beneficiaries = [
        fact.fact_id
        for fact in extraction.facts
        if fact.field_name == FactField.BENEFICIARY
        and fact.subject_role is not None
        and non_entity_beneficiary.search(fact.value_anchor.anchor_text)
    ]
    if invalid_beneficiaries:
        raise ValueError(
            "beneficiary anchors must name an actual recipient, not a calculation unit or payment channel: "
            f"{', '.join(invalid_beneficiaries)}"
        )

    generic_exclusion_result = re.compile(r"^\s*(?:지원\s*)?(?:대상에서\s*)?(?:제외됨|제외|불가|제한됨)\s*[.。]?$")
    invalid_exclusions = [
        fact.fact_id for fact in extraction.facts
        if fact.field_name == FactField.EXCLUSIONS
        and generic_exclusion_result.fullmatch(fact.value_anchor.anchor_text)
    ]
    if invalid_exclusions:
        raise ValueError(
            "exclusions must anchor the restricted subject or condition, not a generic exclusion outcome: "
            f"{', '.join(invalid_exclusions)}"
        )

    # Do not compare anchors by literal text here.  The same text may occur
    # at two legitimate source positions.  The canonical finalizer checks
    # ownership only after each fact has an exact materialized ValueSource.

    # A notice can have participation types and, independently, multiple
    # beneficiary-and-benefit packages.  If the model has already found two
    # direct beneficiaries and attached support facts to each, leaving every
    # one of those facts notice-scoped loses the relationship needed by the
    # final profile.  This is a structural completeness rule: it does not
    # decide who the beneficiaries are or what any package should contain.
    beneficiary_ids = {
        fact.fact_id for fact in extraction.facts
        if fact.field_name == FactField.BENEFICIARY
    }
    linked_beneficiaries = {
        recipient_id
        for fact in extraction.facts
        if fact.field_name in _PACKAGE_SUPPORT_FIELDS
        for recipient_id in fact.recipient_fact_ids
        if recipient_id in beneficiary_ids
    }
    has_package = any(
        component.component_kind == ComponentKind.SUPPORT_PACKAGE
        for component in extraction.support_components
    )
    has_stage = any(
        component.component_kind == ComponentKind.STAGE_SUPPORT
        for component in extraction.support_components
    )
    if len(linked_beneficiaries) >= 2 and not has_package and not has_stage:
        raise ValueError(
            "multiple direct beneficiaries have separately linked support facts, but no support_package was returned; "
            "create the explicit beneficiary-and-benefit support packages and assign their facts with primary_component_id"
        )

    # Typed support-scale defects are deferred until every non-repairable
    # selection-quality guard above has completed. The canonical finalizer can
    # then carry this safe defect through the independent list/cap audits and
    # expose all requirements in one bounded repair call.
    if scale_repair_records:
        raise SupportScaleFactRepairError(list(scale_repair_records.values()))


class SupportCapCompletenessError(ValueError):
    """A safe missing-cap failure with in-memory-only repair anchors."""

    error_classification = "missing_explicit_support_cap"
    field_name = FactField.SUPPORT_SCALE

    def __init__(
        self,
        missing_cap_anchors: list[tuple[str, str]],
        *,
        do_not_restore_fact_ids: set[str] | frozenset[str] = frozenset(),
    ) -> None:
        self._missing_cap_anchors = tuple(missing_cap_anchors)
        self._do_not_restore_fact_ids = frozenset(do_not_restore_fact_ids)
        source_block_ids = sorted({block_id for block_id, _ in self._missing_cap_anchors})
        super().__init__(
            "support cap completeness failed: "
            f"field_name={self.field_name.value} "
            f"classification={self.error_classification} "
            f"source_block_ids={source_block_ids} "
            f"missing_cap_count={len(self._missing_cap_anchors)}"
        )

    def required_support_scale_anchors(self) -> list[dict[str, str]]:
        return [
            {"source_block_id": block_id, "anchor_text": anchor_text}
            for block_id, anchor_text in self._missing_cap_anchors
        ]

    @property
    def do_not_restore_fact_ids(self) -> frozenset[str]:
        return self._do_not_restore_fact_ids


@dataclass(frozen=True, slots=True)
class _SupportScaleFactRepairRecord:
    fact_id: str
    source_block_id: str
    reason: str
    numeric_candidate_count: int
    derived_measure_count: int

    def repair_payload(self) -> dict[str, object]:
        return {
            "fact_id": self.fact_id,
            "source_block_id": self.source_block_id,
            "reason": self.reason,
            "numeric_candidate_count": self.numeric_candidate_count,
            "derived_measure_count": self.derived_measure_count,
        }


class SupportScaleFactRepairError(ValueError):
    """Typed, source-text-free repair signal for a false scale Raw Fact."""

    error_classification = "support_scale_fact_requires_repair"
    _ALLOWED_REASONS = frozenset({
        "no_supported_measure_derived",
        "applicant_financial_eligibility_threshold",
        "duration_bearing_anchor",
        "cross_atomic_support_cap_span",
        "support_cap_span_contains_unrelated_numeric",
        "malformed_numeric_grouping",
        "ambiguous_line_broken_amount",
        "activity_count_not_support_scale",
        "historical_support_cap_context",
        "numeric_placeholder",
    })
    _CAP_CHECK_STATES = frozenset({"not_checked", "complete", "missing"})

    def __init__(
        self,
        facts: list[dict[str, object]],
        *,
        missing_cap_anchors: list[dict[str, str]] | None = None,
        support_cap_check_state: str = "not_checked",
        additional_do_not_restore_fact_ids: set[str] | frozenset[str] = frozenset(),
    ) -> None:
        safe_facts: list[_SupportScaleFactRepairRecord] = []
        for item in facts:
            fact_id = item.get("fact_id")
            source_block_id = item.get("source_block_id")
            reason = item.get("reason")
            numeric_candidate_count = item.get("numeric_candidate_count")
            derived_measure_count = item.get("derived_measure_count")
            if (
                not isinstance(fact_id, str) or not fact_id
                or not isinstance(source_block_id, str) or not source_block_id
                or reason not in self._ALLOWED_REASONS
                or not isinstance(numeric_candidate_count, int) or isinstance(numeric_candidate_count, bool)
                or numeric_candidate_count < 0
                or not isinstance(derived_measure_count, int) or isinstance(derived_measure_count, bool)
                or derived_measure_count < 0
            ):
                raise ValueError("invalid support-scale repair classification")
            safe_facts.append(_SupportScaleFactRepairRecord(
                fact_id=fact_id,
                source_block_id=source_block_id,
                reason=reason,
                numeric_candidate_count=numeric_candidate_count,
                derived_measure_count=derived_measure_count,
            ))
        if not safe_facts:
            raise ValueError("support-scale repair classification requires a fact")
        if support_cap_check_state not in self._CAP_CHECK_STATES:
            raise ValueError("invalid support-cap check state")
        raw_missing_cap_anchors = missing_cap_anchors or []
        if support_cap_check_state == "missing" and not raw_missing_cap_anchors:
            raise ValueError("missing support-cap state requires anchors")
        if support_cap_check_state != "missing" and raw_missing_cap_anchors:
            raise ValueError("support-cap anchors require missing state")
        safe_missing: list[tuple[str, str]] = []
        for anchor in raw_missing_cap_anchors:
            source_block_id = anchor.get("source_block_id")
            anchor_text = anchor.get("anchor_text")
            if not isinstance(source_block_id, str) or not source_block_id or not isinstance(anchor_text, str) or not anchor_text:
                raise ValueError("invalid missing support-cap anchor")
            safe_missing.append((source_block_id, anchor_text))
        self._facts = tuple(sorted(
            safe_facts, key=lambda item: (item.fact_id, item.source_block_id, item.reason)
        ))
        self._missing_cap_anchors = tuple(safe_missing)
        self._support_cap_check_state = support_cap_check_state
        self._do_not_restore_fact_ids = frozenset(additional_do_not_restore_fact_ids) | {
            item.fact_id for item in self._facts
        }
        super().__init__(
            "support_scale fact repair required: "
            f"classification={self.error_classification} fact_count={len(self._facts)}"
        )

    @property
    def do_not_restore_fact_ids(self) -> frozenset[str]:
        return self._do_not_restore_fact_ids

    @property
    def support_cap_check_state(self) -> str:
        return self._support_cap_check_state

    def required_support_scale_anchors(self) -> list[dict[str, str]]:
        return [
            {"source_block_id": block_id, "anchor_text": anchor_text}
            for block_id, anchor_text in self._missing_cap_anchors
        ]

    def with_missing_support_cap_anchors(
        self,
        missing_cap_anchors: list[dict[str, str]],
        *,
        additional_do_not_restore_fact_ids: set[str] | frozenset[str] = frozenset(),
    ) -> "SupportScaleFactRepairError":
        return type(self)(
            self.repair_payload(),
            missing_cap_anchors=missing_cap_anchors,
            support_cap_check_state="missing",
            additional_do_not_restore_fact_ids=(
                self.do_not_restore_fact_ids | frozenset(additional_do_not_restore_fact_ids)
            ),
        )

    def with_support_cap_check_state(self, state: str) -> "SupportScaleFactRepairError":
        return type(self)(
            self.repair_payload(),
            support_cap_check_state=state,
            additional_do_not_restore_fact_ids=self.do_not_restore_fact_ids,
        )

    def repair_payload(self) -> list[dict[str, object]]:
        return [item.repair_payload() for item in self._facts]


@dataclass(frozen=True, slots=True)
class _ExactAnchorMaterializationRepairRecord:
    fact_id: str
    field_name: str
    source_block_id: str
    reason: str

    def repair_payload(self) -> dict[str, str]:
        return {
            "fact_id": self.fact_id,
            "field_name": self.field_name,
            "source_block_id": self.source_block_id,
            "reason": self.reason,
        }


class ExactAnchorMaterializationRepairError(ValueError):
    """Typed retry signal for value anchors absent from canonical block text.

    The repair records deliberately identify only the rejected fact, field,
    source block, and a closed reason code.  In particular, neither the
    model-authored anchor nor any canonical source text is retained in the
    exception or its repair payload.
    """

    error_classification = "exact_anchor_materialization_requires_repair"
    _REASON = "anchor_not_exact_substring"

    def __init__(self, repairs: list[dict[str, object]]) -> None:
        safe_repairs: dict[
            tuple[str, str, str, str], _ExactAnchorMaterializationRepairRecord
        ] = {}
        for item in repairs:
            fact_id = item.get("fact_id")
            raw_field_name = item.get("field_name")
            source_block_id = item.get("source_block_id")
            reason = item.get("reason")
            if isinstance(raw_field_name, FactField):
                field_name = raw_field_name.value
            elif isinstance(raw_field_name, str):
                try:
                    field_name = FactField(raw_field_name).value
                except ValueError as error:
                    raise ValueError("invalid exact-anchor repair classification") from error
            else:
                field_name = None
            if (
                not isinstance(fact_id, str)
                or not fact_id
                or field_name is None
                or not isinstance(source_block_id, str)
                or not source_block_id
                or reason != self._REASON
            ):
                raise ValueError("invalid exact-anchor repair classification")
            record = _ExactAnchorMaterializationRepairRecord(
                fact_id=fact_id,
                field_name=field_name,
                source_block_id=source_block_id,
                reason=self._REASON,
            )
            safe_repairs[
                (record.fact_id, record.field_name, record.source_block_id, record.reason)
            ] = record
        if not safe_repairs:
            raise ValueError("exact-anchor repair classification requires a fact")
        self._repairs = tuple(safe_repairs[key] for key in sorted(safe_repairs))
        self._do_not_restore_fact_ids = frozenset(
            item.fact_id for item in self._repairs
        )
        super().__init__(
            "exact value-anchor repair required: "
            f"classification={self.error_classification} "
            f"fact_count={len(self._repairs)}"
        )

    @property
    def do_not_restore_fact_ids(self) -> frozenset[str]:
        return self._do_not_restore_fact_ids

    def required_exact_anchor_repairs(self) -> list[dict[str, str]]:
        return [item.repair_payload() for item in self._repairs]


class SourceSelectionRepairIssuesError(ValueError):
    """Aggregate independent typed defects into the one bounded repair call."""

    error_classification = "multiple_source_selection_repairs"

    def __init__(self, issues: list[ValueError]) -> None:
        supported = (
            ExactAnchorMaterializationRepairError,
            ExplicitListCompletenessError,
            SupportCapCompletenessError,
            SupportScaleFactRepairError,
        )
        if len(issues) < 2 or any(not isinstance(issue, supported) for issue in issues):
            raise ValueError("combined source-selection repair requires typed issues")
        self._issues = tuple(issues)
        classifications = sorted(
            str(getattr(issue, "error_classification")) for issue in self._issues
        )
        super().__init__(
            "source-selection repair issues detected: "
            f"classification={self.error_classification} "
            f"issue_count={len(self._issues)} issue_classifications={classifications}"
        )

    @property
    def do_not_restore_fact_ids(self) -> frozenset[str]:
        blocked: set[str] = set()
        for issue in self._issues:
            if isinstance(issue, (
                ExactAnchorMaterializationRepairError,
                ExplicitListCompletenessError,
                SupportCapCompletenessError,
                SupportScaleFactRepairError,
            )):
                blocked.update(issue.do_not_restore_fact_ids)
        return frozenset(blocked)

    def required_support_scale_anchors(self) -> list[dict[str, str]]:
        anchors: dict[tuple[str, str], dict[str, str]] = {}
        for issue in self._issues:
            if isinstance(issue, (SupportCapCompletenessError, SupportScaleFactRepairError)):
                for anchor in issue.required_support_scale_anchors():
                    anchors[(anchor["source_block_id"], anchor["anchor_text"])] = anchor
        return [anchors[key] for key in sorted(anchors)]

    def required_support_scale_fact_repairs(self) -> list[dict[str, object]]:
        repairs: dict[tuple[str, str, str], dict[str, object]] = {}
        for issue in self._issues:
            if isinstance(issue, SupportScaleFactRepairError):
                for repair in issue.repair_payload():
                    key = (
                        str(repair["fact_id"]),
                        str(repair["source_block_id"]),
                        str(repair["reason"]),
                    )
                    repairs[key] = repair
        return [repairs[key] for key in sorted(repairs)]

    def required_exact_anchor_repairs(self) -> list[dict[str, str]]:
        repairs: dict[tuple[str, str, str, str], dict[str, str]] = {}
        for issue in self._issues:
            if isinstance(issue, ExactAnchorMaterializationRepairError):
                for repair in issue.required_exact_anchor_repairs():
                    key = (
                        repair["fact_id"],
                        repair["field_name"],
                        repair["source_block_id"],
                        repair["reason"],
                    )
                    repairs[key] = repair
        return [repairs[key] for key in sorted(repairs)]

    @property
    def required_list_item_regions(self) -> list[dict[str, object]]:
        regions: list[dict[str, object]] = []
        for issue in self._issues:
            if isinstance(issue, ExplicitListCompletenessError):
                regions.extend(issue.required_list_item_regions)
        return regions


def do_not_restore_fact_ids_for_typed_repair_v02(
    error: ValueError | None,
) -> frozenset[str]:
    """Return preservation exclusions only for known, typed repair errors.

    Do not inspect arbitrary ``ValueError`` attributes: retry preservation is
    an audit boundary and an unrelated validation error must not gain a new
    preservation policy merely by carrying a similarly named attribute.
    """

    if isinstance(error, (
        ExactAnchorMaterializationRepairError,
        SupportCapCompletenessError,
        SupportScaleFactRepairError,
        ExplicitListCompletenessError,
        SourceSelectionRepairIssuesError,
    )):
        return error.do_not_restore_fact_ids
    return frozenset()


def required_explicit_list_item_regions_for_typed_repair_v02(
    error: ValueError | None,
) -> list[dict[str, object]]:
    """Expose exact list text/coordinates only in the typed in-memory repair."""

    if isinstance(error, (ExplicitListCompletenessError, SourceSelectionRepairIssuesError)):
        return error.required_list_item_regions
    return []


def required_support_scale_anchors_for_typed_repair_v02(
    error: ValueError | None,
) -> list[dict[str, str]]:
    """Expose support-cap locators only for reviewed typed repair errors."""

    if isinstance(error, (
        SupportCapCompletenessError,
        SupportScaleFactRepairError,
        SourceSelectionRepairIssuesError,
    )):
        return error.required_support_scale_anchors()
    return []


def required_support_scale_fact_repairs_for_typed_repair_v02(
    error: ValueError | None,
) -> list[dict[str, object]]:
    """Expose source-text-free invalid scale records for one repair call."""

    if isinstance(error, SupportScaleFactRepairError):
        return error.repair_payload()
    if isinstance(error, SourceSelectionRepairIssuesError):
        return error.required_support_scale_fact_repairs()
    return []


def required_exact_anchor_repairs_for_typed_repair_v02(
    error: ValueError | None,
) -> list[dict[str, str]]:
    """Expose source-text-free absent-anchor records for one repair call."""

    if isinstance(error, ExactAnchorMaterializationRepairError):
        return error.required_exact_anchor_repairs()
    if isinstance(error, SourceSelectionRepairIssuesError):
        return error.required_exact_anchor_repairs()
    return []


def typed_repair_requirements_v02(
    error: ValueError | None,
) -> dict[str, object]:
    """Build the one canonical worker/CLI repair payload fragment."""

    return {
        "required_support_scale_anchors": (
            required_support_scale_anchors_for_typed_repair_v02(error)
        ),
        "required_support_scale_fact_repairs": (
            required_support_scale_fact_repairs_for_typed_repair_v02(error)
        ),
        "required_exact_anchor_repairs": (
            required_exact_anchor_repairs_for_typed_repair_v02(error)
        ),
        "required_list_item_regions": (
            required_explicit_list_item_regions_for_typed_repair_v02(error)
        ),
        "do_not_restore_fact_ids": sorted(
            do_not_restore_fact_ids_for_typed_repair_v02(error)
        ),
    }


def validate_typed_repair_replacements_v02(
    previous: "SourceSelectionExtractionV02 | None",
    current: "SourceSelectionExtractionV02",
    error: ValueError | None,
) -> None:
    """Reject an unchanged fact which a typed repair explicitly invalidated.

    ``do_not_restore_fact_ids`` prevents the preservation layer from bringing
    a rejected prior fact back after the model omitted it.  It cannot by
    itself stop a repair response from repeating that same bad fact (possibly
    under a new id) and merely adding another fact beside it.  Compare the
    source-visible fact fingerprint here and re-raise the original typed error
    when that happens.  A genuine repair remains allowed to reuse the id after
    changing either the field or exact anchor; normal finalization then checks
    the changed fact in full.
    """

    if previous is None:
        return
    blocked_ids = do_not_restore_fact_ids_for_typed_repair_v02(error)
    if not blocked_ids:
        return
    blocked_fingerprints = {
        (
            fact.field_name,
            fact.value_anchor.source_block_id,
            fact.value_anchor.anchor_text,
        )
        for fact in previous.facts
        if fact.fact_id in blocked_ids
    }
    if not blocked_fingerprints:
        return
    if any(
        (
            fact.field_name,
            fact.value_anchor.source_block_id,
            fact.value_anchor.anchor_text,
        ) in blocked_fingerprints
        for fact in current.facts
    ):
        if error is None:  # pragma: no cover - blocked ids imply a typed error
            raise ValueError("typed repair repeated a rejected prior fact")
        raise error


def validate_materialized_typed_repair_replacements_v02(
    pack: CandidatePack,
    materialized_evidence: list["MaterializedEvidence"],
    error: ValueError | None,
) -> None:
    """Recheck typed list repairs using canonical original coordinates."""

    required_regions = required_explicit_list_item_regions_for_typed_repair_v02(
        error
    )
    if not required_regions:
        return
    if explicit_list_repair_has_invalid_overlaps_v02(
        pack, materialized_evidence, required_regions
    ):
        if error is None:  # pragma: no cover - regions imply a typed error
            raise ValueError("typed list repair retained an invalid fact")
        raise error


_SUPPORT_SCALE_ELIGIBILITY_METRIC = re.compile(
    r"(?:연\s*매출|매출(?:액)?|영업\s*이익|자산(?:액)?|자본(?:금)?|"
    r"부채(?:액|비율)?|투자(?:금|액)|출자|재무|고용\s*(?:인원|인력)|"
    r"종업원\s*수|상시\s*근로자\s*수|근로자\s*수)"
)
_SUPPORT_SCALE_ELIGIBILITY_ROLE = re.compile(
    r"(?:신청\s*(?:자격|대상)|지원\s*(?:자격|대상)|모집\s*대상|참가\s*자격)"
)
_SUPPORT_SCALE_THRESHOLD_COMPARATOR = re.compile(
    r"(?:이하|이상|미만|초과|보유)"
)
_SUPPORT_SCALE_BENEFIT_SIGNAL = re.compile(
    r"(?:추가\s*지원|지원(?:금|비|율|한도|금액|규모)|"
    r"지원(?!\s*(?:대상|자격|사업|분야|요건|조건))|지급|보조(?:금)?|융자|보증)"
)


def _bounded_source_segment(text: str, start: int, end: int) -> str:
    """Return the literal line/semicolon unit that owns one source span."""

    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", end)
    if line_end < 0:
        line_end = len(text)
    segment_start = max(
        line_start,
        text.rfind(";", line_start, start) + 1,
        text.rfind("；", line_start, start) + 1,
    )
    stops = [position for position in (text.find(";", end, line_end), text.find("；", end, line_end), line_end) if position >= 0]
    return text[segment_start:min(stops)]


def _projection_has_only_whitespace_gaps_v02(
    pack: CandidatePack,
    source: ValueSource,
    projected: list[tuple[str, int, int]],
    fully_projected: bool,
) -> bool:
    """Allow a native composite's inserted whitespace, never synthetic text."""

    if fully_projected:
        return True
    block = next(
        (item for item in pack.blocks if item.block_id == source.source_block_id),
        None,
    )
    if block is None or not block.source_spans:
        return False
    gap_text: list[str] = []
    cursor = 0
    for span in block.source_spans:
        cursor += len(span.exact_text)
        separator_end = cursor + len(span.separator_after)
        overlap_start = max(source.start_char, cursor)
        overlap_end = min(source.end_char, separator_end)
        if overlap_end > overlap_start:
            gap_text.append(block.text[overlap_start:overlap_end])
        cursor = separator_end
    covered_length = sum(end - start for _, start, end in projected)
    gaps = "".join(gap_text)
    return (
        covered_length + len(gaps) == source.end_char - source.start_char
        and bool(gaps)
        and gaps.isspace()
    )


def _valid_support_cap_claims_v02(
    pack: CandidatePack,
    source: ValueSource,
    projected: list[tuple[str, int, int]],
    fully_projected: bool,
    confirmed_caps: Mapping[str, set[tuple[int, int]]],
    numeric_candidates: list[NumericCandidate],
) -> set[tuple[str, int, int]]:
    """Return cap claims whose evidence contains no unrelated numeral."""

    if not projected or not _projection_has_only_whitespace_gaps_v02(
        pack, source, projected, fully_projected
    ):
        return set()
    def claims_in_coordinate_space(
        ranges: list[tuple[str, int, int]],
    ) -> set[tuple[str, int, int]]:
        selected_numeric_spans = [
            (candidate.source_block_id, candidate.start_char, candidate.end_char)
            for candidate in numeric_candidates
            if any(
                block_id == candidate.source_block_id
                and selected_start < candidate.end_char
                and candidate.start_char < selected_end
                for block_id, selected_start, selected_end in ranges
            )
        ]
        contained_caps: set[tuple[str, int, int]] = set()
        claims: set[tuple[str, int, int]] = set()
        for block_id, selected_start, selected_end in ranges:
            for cap_start, cap_end in confirmed_caps.get(block_id, set()):
                if not (
                    selected_start <= cap_start
                    and cap_end <= selected_end
                ):
                    continue
                contained_caps.add((block_id, cap_start, cap_end))
                if selected_numeric_spans and all(
                    numeric_block_id == block_id
                    and cap_start <= numeric_start
                    and numeric_end <= cap_end
                    for numeric_block_id, numeric_start, numeric_end in selected_numeric_spans
                ):
                    claims.add((block_id, cap_start, cap_end))
        # A partially valid broad selection is still broad. If any contained
        # cap has another selected numeral outside its own bounds, none of
        # this fact's claims may discharge completeness.
        return claims if claims == contained_caps else set()

    # Native composites normally compare in projected atomic coordinates.
    # A cap whose marker and amount straddle an immutable composite boundary
    # exists only in the composite coordinate space, so retain that exact
    # coordinate as an additional (not replacement) completeness identity.
    direct = [(source.source_block_id, source.start_char, source.end_char)]
    return claims_in_coordinate_space(projected) | claims_in_coordinate_space(direct)


def _projected_selection_has_financial_threshold_v02(
    projected: list[tuple[str, int, int]],
    selected_text: str,
    numeric_candidates: list[NumericCandidate],
) -> bool:
    """Recognize a financial eligibility threshold across native spans."""

    # Native composition can join an eligibility predicate to its explicit
    # benefit (for example ``매출액 ... 감소기업: 1% 추가 지원``).  Treating
    # that complete selected value as eligibility-only makes its validity
    # depend on whether the same immutable text happened to remain atomic.
    # A broad/cross-atomic cap remains subject to the independent cap checks
    # below; this guard only declines the eligibility-only classification.
    if _SUPPORT_SCALE_BENEFIT_SIGNAL.search(selected_text):
        return False
    if not _SUPPORT_SCALE_ELIGIBILITY_METRIC.search(selected_text):
        return False
    if not (
        _SUPPORT_SCALE_ELIGIBILITY_ROLE.search(selected_text)
        or _SUPPORT_SCALE_THRESHOLD_COMPARATOR.search(selected_text)
    ):
        return False
    return any(
        block_id == candidate.source_block_id
        and selected_start <= candidate.start_char
        and candidate.end_char <= selected_end
        for block_id, selected_start, selected_end in projected
        for candidate in numeric_candidates
    )


def validate_materialized_support_scale_semantics_v02(
    extraction: "SourceSelectionExtractionV02",
    pack: CandidatePack,
    evidence: list["MaterializedEvidence"],
    *,
    raise_error: bool = True,
) -> SupportScaleFactRepairError | None:
    """Fail closed when applicant financial eligibility becomes support scale."""

    del extraction
    block_texts = {block.block_id: block.text for block in pack.blocks}
    confirmed_caps = explicit_support_cap_spans_by_block(pack)
    numeric_candidates = build_numeric_candidates(pack)
    invalid: dict[str, str] = {}
    evidence_by_fact_id = {row.fact_id: row for row in evidence}
    for row in evidence:
        if row.field_name != FactField.SUPPORT_SCALE or row.value_source is None:
            continue
        source = row.value_source
        text = block_texts.get(source.source_block_id)
        if isinstance(text, str):
            segment = _bounded_source_segment(text, source.start_char, source.end_char)
            selected_text = text[source.start_char:source.end_char]
            projected, fully_projected = project_value_source_to_atomic_ranges(
                pack, source
            )
            valid_cap_claims = _valid_support_cap_claims_v02(
                pack,
                source,
                projected,
                fully_projected,
                confirmed_caps,
                numeric_candidates,
            )
            owns_confirmed_cap = bool(valid_cap_claims)
            overlapping_cap_keys = {
                (projected_block_id, cap_start, cap_end)
                for projected_block_id, projected_start, projected_end in projected
                for cap_start, cap_end in confirmed_caps.get(projected_block_id, set())
                if projected_start < cap_end and projected_end > cap_start
            }
            overlapping_cap_keys.update({
                (source.source_block_id, cap_start, cap_end)
                for cap_start, cap_end in confirmed_caps.get(
                    source.source_block_id, set()
                )
                if source.start_char < cap_end and source.end_char > cap_start
            })
            overlaps_confirmed_cap = bool(overlapping_cap_keys)
            is_single_atomic_selection = fully_projected and len(projected) == 1
            malformed_grouping = any(
                re.fullmatch(_GROUPED_DECIMAL_NUMBER, match.group("number")) is None
                for match in _GROUPED_NUMBER_WITH_COMMA.finditer(selected_text)
            )
            if _has_line_broken_compound_money(selected_text):
                invalid[row.fact_id] = "ambiguous_line_broken_amount"
            elif is_historical_support_cap_context(segment):
                invalid[row.fact_id] = "historical_support_cap_context"
            elif malformed_grouping:
                invalid[row.fact_id] = "malformed_numeric_grouping"
            elif (
                not is_single_atomic_selection
                and _projected_selection_has_financial_threshold_v02(
                    projected, selected_text, numeric_candidates
                )
            ):
                invalid[row.fact_id] = "applicant_financial_eligibility_threshold"
            elif (
                overlaps_confirmed_cap
                and not is_single_atomic_selection
                and overlapping_cap_keys != valid_cap_claims
            ):
                invalid[row.fact_id] = "cross_atomic_support_cap_span"
            elif overlaps_confirmed_cap and not owns_confirmed_cap:
                invalid[row.fact_id] = "support_cap_span_contains_unrelated_numeric"
            elif (
                _SUPPORT_SCALE_ELIGIBILITY_METRIC.search(segment)
                and not _SUPPORT_SCALE_BENEFIT_SIGNAL.search(selected_text)
                and not owns_confirmed_cap
            ):
                invalid[row.fact_id] = "applicant_financial_eligibility_threshold"
    if not invalid:
        return None
    error = SupportScaleFactRepairError([
        {
            "fact_id": fact_id,
            "source_block_id": evidence_by_fact_id[fact_id].value_source.source_block_id,
            "reason": invalid[fact_id],
            "numeric_candidate_count": 0,
            "derived_measure_count": 0,
        }
        for fact_id in sorted(invalid)
    ])
    if raise_error:
        raise error
    return error


def validate_support_cap_completeness_v02(
    extraction: "SourceSelectionExtractionV02",
    pack: CandidatePack,
    *,
    materialized_evidence: list["MaterializedEvidence"] | None = None,
) -> None:
    """Require one exactly materialized scale fact per cap occurrence.

    Literal text is not an identifier: two equal caps at different offsets
    need two independently materialized facts.  Conversely, one oversized
    fact cannot discharge more than one cap occurrence.
    """

    if materialized_evidence is None:
        materialized_evidence = materialize_evidence(extraction, pack)
    captured_ranges: list[
        tuple[
            str,
            list[tuple[str, int, int]],
            tuple[str, int, int],
            set[tuple[str, int, int]],
        ]
    ] = []
    confirmed_caps = explicit_support_cap_spans_by_block(pack)
    numeric_candidates = build_numeric_candidates(pack)
    for row in materialized_evidence:
        if row.field_name == FactField.SUPPORT_SCALE and row.value_source is not None:
            projected, fully_projected = project_value_source_to_atomic_ranges(
                pack, row.value_source
            )
            captured_ranges.append((
                row.fact_id,
                projected,
                (
                    row.value_source.source_block_id,
                    row.value_source.start_char,
                    row.value_source.end_char,
                ),
                _valid_support_cap_claims_v02(
                    pack,
                    row.value_source,
                    projected,
                    fully_projected,
                    confirmed_caps,
                    numeric_candidates,
                ),
            ))

    # ``pack`` is the trusted, routed A-candidate scope.  Completeness must
    # cover every scanner-confirmed native cap in that whole scope, not just
    # blocks the model happened to cite.  Otherwise an omitted A block can
    # hide its cap simply by receiving no fact/context/component reference.
    # B/search-only blocks are absent from an A pack by construction; the
    # scanner itself continues to reject unsafe PDF table/layout/OCR blocks.
    candidates = extract_explicit_support_cap_candidates(pack)
    claims_by_fact: dict[str, list[tuple[str, int, int]]] = {}
    claims_by_cap: dict[tuple[str, int, int], list[str]] = {}
    invalid_claimant_fact_ids: set[str] = set()
    for candidate in candidates:
        cap_key = (candidate.source_block_id, candidate.start_char, candidate.end_char)
        for fact_id, projected, direct, valid_cap_claims in captured_ranges:
            overlaps = any(
                block_id == candidate.source_block_id
                and projected_start <= candidate.start_char
                and candidate.end_char <= projected_end
                for block_id, projected_start, projected_end in projected
            )
            direct_block_id, direct_start, direct_end = direct
            overlaps = overlaps or (
                direct_block_id == candidate.source_block_id
                and direct_start <= candidate.start_char
                and candidate.end_char <= direct_end
            )
            if cap_key in valid_cap_claims:
                claims_by_fact.setdefault(fact_id, []).append(cap_key)
                claims_by_cap.setdefault(cap_key, []).append(fact_id)
            elif overlaps:
                invalid_claimant_fact_ids.add(fact_id)
    missing: list[tuple[str, str]] = []
    for candidate in candidates:
        cap_key = (candidate.source_block_id, candidate.start_char, candidate.end_char)
        claimants = claims_by_cap.get(cap_key, [])
        if len(claimants) != 1 or len(claims_by_fact.get(claimants[0], [])) != 1:
            missing.append((candidate.source_block_id, candidate.anchor_text))
            invalid_claimant_fact_ids.update(claimants)
    if missing:
        raise SupportCapCompletenessError(
            missing,
            do_not_restore_fact_ids=invalid_claimant_fact_ids,
        )


def normalize_nested_support_scale_anchors_v02(
    extraction: "SourceSelectionExtractionV02",
) -> tuple["SourceSelectionExtractionV02", list[dict[str, str]]]:
    """Remove only a strictly-contained scale anchor in the same package/block.

    ``150만원`` carries no information once the same component selected
    ``최대 150만원`` from the same source block.  This is provenance cleanup,
    not cross-block semantic deduplication: different blocks or components
    remain untouched.
    """
    removed: set[str] = set()
    changes: list[dict[str, str]] = []
    scales = [fact for fact in extraction.facts if fact.field_name == FactField.SUPPORT_SCALE]
    for short in scales:
        for long in scales:
            if short.fact_id == long.fact_id:
                continue
            if (
                short.primary_component_id == long.primary_component_id
                and short.value_anchor.source_block_id == long.value_anchor.source_block_id
                and len(short.value_anchor.anchor_text) < len(long.value_anchor.anchor_text)
                and short.value_anchor.anchor_text in long.value_anchor.anchor_text
            ):
                removed.add(short.fact_id)
                changes.append({
                    "kind": "nested_support_scale_anchor_removed",
                    "removed_fact_id": short.fact_id,
                    "retained_fact_id": long.fact_id,
                })
                break
    if not removed:
        return extraction, []
    payload = extraction.model_dump(mode="json")
    payload["facts"] = [fact for fact in payload["facts"] if fact["fact_id"] not in removed]
    return SourceSelectionExtractionV02.model_validate(payload), changes


_EARLY_STAGE_MARKER = re.compile(r"(?:\d+\s*차|심사\s*합격|선발\s*후|수료\s*후)")
_FINAL_STAGE_MARKER = re.compile(r"(?:최종\s*(?:선정|지원)?|최종\s*선발)")


def _explicit_sequential_component_ids(
    extraction: "SourceSelectionExtractionV02",
    pack: CandidatePack,
) -> tuple[list[str], list[str]]:
    """Return selected components with explicit earlier/final stage evidence."""

    block_texts = {block.block_id: block.text for block in pack.blocks}
    early_components: list[str] = []
    final_components: list[str] = []
    for component in extraction.support_components:
        component_text = "\n".join(
            block_texts.get(block_id, "")
            for block_id in [*component.source_block_ids, *component.table_block_ids]
        )
        if _EARLY_STAGE_MARKER.search(component_text):
            early_components.append(component.support_component_id)
        if _FINAL_STAGE_MARKER.search(component_text):
            final_components.append(component.support_component_id)
    return early_components, final_components


def normalize_explicit_sequential_components_v02(
    extraction: "SourceSelectionExtractionV02",
    pack: CandidatePack,
) -> tuple["SourceSelectionExtractionV02", list[dict[str, str]]]:
    """Deterministically turn explicitly sequential components into stages.

    This is a server normalization, not a semantic guess: it applies only when
    separate selected components contain both an explicit earlier-stage marker
    and an explicit final-stage marker.  Ambiguous structures are unchanged.
    """

    early_components, final_components = _explicit_sequential_component_ids(extraction, pack)
    sequential_ids = set(early_components) | set(final_components)
    if not early_components or not final_components or len(sequential_ids) < 2:
        return extraction, []
    block_texts = {block.block_id: block.text for block in pack.blocks}
    changed = []
    components = []
    for component in extraction.support_components:
        update: dict[str, object] = {}
        if component.support_component_id in sequential_ids and component.component_kind != ComponentKind.STAGE_SUPPORT:
            update["component_kind"] = ComponentKind.STAGE_SUPPORT
            changed.append({
                "support_component_id": component.support_component_id,
                "from_kind": component.component_kind.value,
                "to_kind": ComponentKind.STAGE_SUPPORT.value,
                "reason": "explicit_earlier_and_final_stage_markers",
            })
        # A final stage must be named after its stage label rather than only
        # the grant delivered in that stage, when that label is explicit and
        # uniquely locatable in the selected component evidence.
        if component.support_component_id in final_components:
            final_matches = [
                (block_id, match.group(0))
                for block_id in [*component.source_block_ids, *component.table_block_ids]
                for match in _FINAL_STAGE_MARKER.finditer(block_texts.get(block_id, ""))
            ]
            if len(final_matches) == 1:
                block_id, anchor_text = final_matches[0]
                current_anchor = component.name_anchor
                if current_anchor is None or (
                    current_anchor.source_block_id != block_id or current_anchor.anchor_text != anchor_text
                ):
                    update["name_anchor"] = SourceTextAnchor(
                        source_block_id=block_id, anchor_text=anchor_text
                    )
                    changed.append({
                        "support_component_id": component.support_component_id,
                        "from_kind": current_anchor.anchor_text if current_anchor else "",
                        "to_kind": anchor_text,
                        "reason": "explicit_final_stage_label",
                    })
        components.append(component.model_copy(update=update) if update else component)
    if not changed:
        return extraction, []
    mode = (
        ComponentDecisionMode.STAGES
        if len(sequential_ids) == len(components)
        else ComponentDecisionMode.MIXED
    )
    normalized = extraction.model_copy(update={
        "component_decision": ComponentDecision(mode=mode),
        "support_components": components,
    })
    return normalized, changed


def normalize_explicit_condition_variant_relations_v02(
    extraction: "SourceSelectionExtractionV02",
) -> tuple["SourceSelectionExtractionV02", list[dict[str, str]]]:
    """Attach a package's shared support facts to every explicit condition variant.

    This is a deterministic structural completion, not a semantic guess: it
    only fires on a support component that already carries at least two
    ``eligibility_conditions`` facts, each of which explicitly modifies its
    own, mutually disjoint set of that component's support facts (e.g. 신규
    채용 vs 기존 재직자 rows already linked to their own amount/rate by
    ``modifies_fact_ids``).  That disjoint partition is the model's own
    evidence that these conditions are alternative employment-condition
    variants of one package.  Any other package support fact of that same
    component that shares a recipient with the facts already claimed by a
    condition, but that no condition yet claims, applies regardless of which
    variant was selected -- so it is added to every sibling condition's
    ``modifies_fact_ids``.  A component with only one such condition, or with
    overlapping (ambiguous) modifies sets, is left untouched.

    An unclaimed fact is only attached when its field type is *not* already
    one of the field types the conditions explicitly modify.  Otherwise the
    field type is demonstrably variant-specific in this package -- e.g.
    every condition already claims its own support_scale -- and an
    unclaimed same-field sibling more likely reflects a missed
    variant-specific rate/amount link than a fact genuinely common to every
    variant; silently attaching it would fabricate a false shared relation.
    This applies uniformly to every field, including support_period: no
    field is exempted from the claimed-field check.
    """

    facts_by_id = {fact.fact_id: fact for fact in extraction.facts}
    facts_by_component: dict[str, list[SourceSelectedFactV02]] = {}
    for fact in extraction.facts:
        if fact.primary_component_id:
            facts_by_component.setdefault(fact.primary_component_id, []).append(fact)

    additions: dict[str, set[str]] = {}
    changed: list[dict[str, str]] = []
    for component_id, component_facts in facts_by_component.items():
        conditions = [
            fact
            for fact in component_facts
            if fact.field_name == FactField.ELIGIBILITY_CONDITIONS and fact.modifies_fact_ids
        ]
        if len(conditions) < 2:
            continue
        modified_sets = [set(condition.modifies_fact_ids) for condition in conditions]
        if any(a & b for i, a in enumerate(modified_sets) for b in modified_sets[i + 1 :]):
            continue  # overlapping modifies is ambiguous, not an explicit partition
        claimed = set().union(*modified_sets)
        claimed_field_names = {facts_by_id[fact_id].field_name for fact_id in claimed}
        recipients = {
            recipient_id
            for fact_id in claimed
            for recipient_id in facts_by_id[fact_id].recipient_fact_ids
        }
        if not recipients:
            continue
        shared = [
            fact
            for fact in component_facts
            if fact.field_name in _PACKAGE_SUPPORT_FIELDS
            and fact.fact_id not in claimed
            and set(fact.recipient_fact_ids) & recipients
            and fact.field_name not in claimed_field_names
        ]
        if not shared:
            continue
        for condition in conditions:
            new_ids = {fact.fact_id for fact in shared} - set(condition.modifies_fact_ids)
            if new_ids:
                additions.setdefault(condition.fact_id, set()).update(new_ids)
                changed.append({
                    "support_component_id": component_id,
                    "condition_fact_id": condition.fact_id,
                    "added_modifies_fact_ids": ", ".join(sorted(new_ids)),
                    "reason": "explicit_condition_variant_shared_support_fact",
                })

    if not additions:
        return extraction, []
    facts = [
        fact.model_copy(update={
            "modifies_fact_ids": [*fact.modifies_fact_ids, *sorted(additions[fact.fact_id])]
        }) if fact.fact_id in additions else fact
        for fact in extraction.facts
    ]
    normalized = extraction.model_copy(update={"facts": facts})
    return normalized, changed


def _anchor_resolves(pack_blocks: dict[str, str], source_block_id: str, anchor_text: str) -> bool:
    text = pack_blocks.get(source_block_id)
    # ``str.count`` undercounts a self-overlapping repeat (see
    # profile_v02.find_all_occurrences); a preserved anchor must not be
    # treated as still-unique on that undercount alone.
    return text is not None and len(find_all_occurrences(text, anchor_text)) == 1


def _component_evidence_fingerprint(component: "SourceSelectedComponent") -> tuple:
    """A component's identity across independent runs, not its model-authored id.

    ``support_component_id`` is assigned per-response and is not stable
    across attempts/runs.  Two components are "the same" when their exact
    source evidence matches: the name anchor (if any), and the block ids
    that make up the component's own evidence.  ``component_kind`` is
    deliberately excluded: it is a repairable classification (for example
    ``normalize_explicit_sequential_components_v02`` can relabel a
    support_package to a stage_support from the very same evidence), not
    part of the component's identity.
    """

    name = (
        (component.name_anchor.source_block_id, component.name_anchor.anchor_text)
        if component.name_anchor is not None else None
    )
    return (
        name,
        tuple(sorted(component.source_block_ids)),
        tuple(sorted(component.table_block_ids)),
    )


def preserve_prior_server_validated_facts_v02(
    previous: "SourceSelectionExtractionV02 | None",
    current: "SourceSelectionExtractionV02",
    pack: CandidatePack,
    *,
    do_not_restore_fact_ids: set[str] | frozenset[str] | None = None,
) -> tuple["SourceSelectionExtractionV02", list[dict[str, str]]]:
    """Carry forward a prior attempt's facts a repair attempt silently dropped.

    A one-call model response is stochastic: fixing the one problem a
    validator flagged does not guarantee every other, already-correct fact
    survives the next attempt.  This is a narrow, mechanical safety net, not
    a semantic merge: on the first attempt there is nothing to preserve
    (``previous`` is ``None``), so it is a no-op.

    A previous fact is superseded -- never re-added -- whenever ``current``
    already holds a fact anchored to the exact same (source_block_id,
    anchor_text) span, *regardless of that fact's field_name or component
    scope*: the newer attempt claimed that exact evidence, however it
    reclassified or re-scoped it, so nothing new needs to be added for it.
    Duplicate prevention is intentionally field-agnostic.  Relation
    *aliasing* is not: a reference to a superseded previous fact is only
    rewritten to the current fact's id when the current fact's field_name is
    unchanged from the previous one's -- e.g. a recipient must still be a
    recipient-shaped fact, not whatever the span was reclassified into.  A
    cross-field alias is dropped rather than silently retargeted, for
    modifies_fact_ids, recipient_fact_ids, and basis_fact_ids alike.

    Short of superseded, a fact from ``previous`` is only carried into
    ``current`` when all hold:

    - its (field_name, component, source_block_id) "slot" is not already
      occupied by a *different* exact anchor in ``current`` -- that slot was
      explicitly re-decided by the newer attempt, so the older span must not
      be resurrected beside or instead of it;
    - every component it is scoped to (primary and applicability) has an
      equivalent component in ``current`` -- matched by a stable evidence
      fingerprint (name anchor, source/table block ids; ``component_kind``
      is excluded since it is a repairable classification, not identity),
      not by the model-authored component id, which is not stable across
      attempts -- and its component references are remapped to that current
      id;
    - its value_anchor (and, for delivery_roles, every organization_anchor
      and the role_anchor) still resolves to exactly one occurrence in the
      *current* candidate pack -- re-verified here, not merely assumed from
      the earlier attempt.

    A preserved fact is re-issued under a fresh fact_id (the two attempts'
    id spaces are independent and may coincidentally collide).  A
    modifies/recipient/basis reference from a preserved fact is rewritten to
    the *current* fact's id when it pointed at another preserved fact
    (always safe -- same fact, same field, just a fresh id) or at a
    same-field superseded fact (above), and dropped -- rather than left
    dangling or cross-field-aliased -- when it pointed at a previous fact
    that was not preserved (superseded by a conflicting re-selection or a
    field change, or unresolvable).  The merged extraction is re-run through
    ``SourceSelectionExtractionV02`` validation so every contract invariant
    -- unique ids, known component/fact references -- holds over the
    combined set exactly as it would for a single model response.
    """

    if previous is None or not previous.facts:
        return current, []

    blocked_fact_ids = set() if do_not_restore_fact_ids is None else set(do_not_restore_fact_ids)
    if any(not isinstance(fact_id, str) or not fact_id for fact_id in blocked_fact_ids):
        raise ValueError("do_not_restore_fact_ids must contain non-empty fact IDs")

    pack_blocks = {block.block_id: block.text for block in pack.blocks}

    # (source_block_id, anchor_text) -> (current fact_id, current field_name)
    # already holding that exact span, regardless of field/component: the
    # strongest identity, used field-agnostically for duplicate prevention.
    current_anchor_index: dict[tuple[str, str], tuple[str, FactField]] = {}
    # (field_name, current primary_component_id, source_block_id) -> anchor
    # texts already present in `current` for that slot.
    current_slots: dict[tuple[str, str | None, str], set[str]] = {}
    for fact in current.facts:
        anchor_key = (fact.value_anchor.source_block_id, fact.value_anchor.anchor_text)
        current_anchor_index.setdefault(anchor_key, (fact.fact_id, fact.field_name))
        slot = (fact.field_name, fact.primary_component_id, fact.value_anchor.source_block_id)
        current_slots.setdefault(slot, set()).add(fact.value_anchor.anchor_text)

    previous_components_by_id = {
        component.support_component_id: component for component in previous.support_components
    }
    current_component_id_by_fingerprint: dict[tuple, str] = {}
    for component in current.support_components:
        current_component_id_by_fingerprint.setdefault(
            _component_evidence_fingerprint(component), component.support_component_id
        )

    def _resolve_component_remap(fact: "SourceSelectedFactV02") -> dict[str, str] | None:
        """Map every previous component id `fact` scopes to, to its current
        equivalent by evidence fingerprint.  ``None`` means unresolvable."""

        referenced_ids = {fact.primary_component_id, *fact.applicability_component_ids} - {None}
        remap: dict[str, str] = {}
        for prev_component_id in referenced_ids:
            prev_component = previous_components_by_id.get(prev_component_id)
            if prev_component is None:
                return None
            current_component_id = current_component_id_by_fingerprint.get(
                _component_evidence_fingerprint(prev_component)
            )
            if current_component_id is None:
                return None
            remap[prev_component_id] = current_component_id
        return remap

    def _anchors_resolve(fact: "SourceSelectedFactV02") -> bool:
        if not _anchor_resolves(pack_blocks, fact.value_anchor.source_block_id, fact.value_anchor.anchor_text):
            return False
        for block_id in fact.context_source_block_ids:
            if block_id not in pack_blocks:
                return False
        if fact.field_name == FactField.DELIVERY_ROLES:
            for anchor in [*fact.organization_anchors, *([fact.role_anchor] if fact.role_anchor else [])]:
                if not _anchor_resolves(pack_blocks, anchor.source_block_id, anchor.anchor_text):
                    return False
        return True

    kept: list[tuple["SourceSelectedFactV02", dict[str, str]]] = []
    # A previous fact_id resolves to a current fact_id when `current` already
    # holds its exact evidence *and* classified it under the same field_name;
    # this is not "preservation" (nothing new is added) but keeps a kept
    # fact's relation to it from being dropped.  Duplicate prevention above
    # (the `continue`) fires regardless of field; this alias map is strictly
    # narrower, so a cross-field reference is dropped, not retargeted.
    id_remap: dict[str, str] = {}
    for fact in previous.facts:
        if fact.fact_id in blocked_fact_ids:
            continue  # a typed repair intentionally omitted/reclassified it
        anchor_key = (fact.value_anchor.source_block_id, fact.value_anchor.anchor_text)
        superseding = current_anchor_index.get(anchor_key)
        if superseding is not None:
            superseding_id, superseding_field = superseding
            if fact.field_name == superseding_field:
                id_remap[fact.fact_id] = superseding_id
            continue  # current already claims this exact span, however classified/scoped
        component_remap = _resolve_component_remap(fact)
        if component_remap is None:
            continue  # a component this fact is scoped to has no current equivalent
        remapped_primary = component_remap.get(fact.primary_component_id) if fact.primary_component_id else None
        slot = (fact.field_name, remapped_primary, fact.value_anchor.source_block_id)
        if current_slots.get(slot) and fact.value_anchor.anchor_text not in current_slots[slot]:
            continue  # this slot was explicitly re-decided with a different span
        if not _anchors_resolve(fact):
            continue
        kept.append((fact, component_remap))

    if not kept:
        return current, []

    existing_ids = {fact.fact_id for fact in current.facts}
    for fact, _ in kept:
        candidate_id, suffix = f"restored__{fact.fact_id}", 0
        while candidate_id in existing_ids or candidate_id in id_remap.values():
            suffix += 1
            candidate_id = f"restored__{fact.fact_id}__{suffix}"
        id_remap[fact.fact_id] = candidate_id

    def _remap(ids: list[str]) -> list[str]:
        return [id_remap[old] for old in ids if old in id_remap]

    changes: list[dict[str, str]] = []
    restored_facts = []
    for fact, component_remap in kept:
        update: dict[str, object] = {
            "fact_id": id_remap[fact.fact_id],
            "modifies_fact_ids": _remap(fact.modifies_fact_ids),
            "recipient_fact_ids": _remap(fact.recipient_fact_ids),
            "basis_fact_ids": _remap(fact.basis_fact_ids),
        }
        if fact.primary_component_id is not None:
            update["primary_component_id"] = component_remap[fact.primary_component_id]
        if fact.applicability_component_ids:
            update["applicability_component_ids"] = [
                component_remap[component_id] for component_id in fact.applicability_component_ids
            ]
        restored_facts.append(fact.model_copy(update=update))
        changes.append({
            "restored_fact_id": id_remap[fact.fact_id],
            "original_fact_id": fact.fact_id,
            "field_name": fact.field_name.value,
            "source_block_id": fact.value_anchor.source_block_id,
            "anchor_text": fact.value_anchor.anchor_text,
            "reason": "revalidated_from_prior_attempt",
        })

    merged_payload = current.model_dump(mode="json")
    merged_payload["facts"] = [*merged_payload["facts"], *[fact.model_dump(mode="json") for fact in restored_facts]]
    merged = current.__class__.model_validate(merged_payload)
    return merged, changes


def apply_finalize_with_fallback_v02(
    merged: "SourceSelectionExtractionV02",
    current: "SourceSelectionExtractionV02",
    preserved_changes: list[dict[str, str]],
    finalize,
):
    """Run ``finalize`` on the merged selection; never let preservation break it.

    ``finalize`` is the validate/normalize/materialize sequence a selection
    must pass.  If it accepts ``merged``, that result is used as-is.  If it
    rejects ``merged`` -- restoring a prior fact can, in principle, trip an
    unrelated structural check (for example the package-completeness or
    stage-contradiction guard) -- and merging actually changed something,
    this falls back to ``finalize(current)`` (the unmerged, otherwise-valid
    attempt) and returns an audit record explaining why.  If nothing was
    preserved, or the unmerged attempt is itself invalid, the original
    failure propagates exactly as it would without this wrapper.
    """

    try:
        return finalize(merged), preserved_changes, None
    except ValueError as merge_error:
        if not preserved_changes:
            raise
        result = finalize(current)
        fallback_record = {
            "reason": "preservation_caused_validation_failure",
            "validation_error": str(merge_error),
            "dropped_restored_fact_ids": sorted(change["restored_fact_id"] for change in preserved_changes),
        }
        return result, [], fallback_record


class EmptyRepairResponseError(RuntimeError):
    """A repair attempt (not the first attempt) returned zero facts.

    This is a distinct, deterministic failure: a repair attempt is expected
    to revise a prior candidate, not discard it, so an empty response gives
    the server nothing safe to merge, preserve, or assemble a profile from.
    Retrying with the same one-retry loop would silently repeat the same
    stochastic gap, so this fails immediately instead of continuing the
    loop or issuing an unplanned extra model call.
    """

    error_classification = "empty_repair_response"

    def __init__(self, prior_candidate_summary: dict, prior_validation_error: str | None):
        super().__init__(
            "a repair attempt returned zero facts; refusing to merge, preserve, or assemble a profile"
        )
        self.prior_candidate_summary = prior_candidate_summary
        self.prior_validation_error = prior_validation_error


def summarize_prior_candidate_v02(extraction: "SourceSelectionExtractionV02 | None") -> dict:
    """An audit-safe summary of a prior, already-rejected candidate selection.

    This never claims the prior facts were server-validated: it reports
    only what the rejected candidate itself stated -- field_name and the
    exact (source_block_id, anchor_text) it selected -- with no
    materialization, offset resolution, or other server confirmation
    implied.  Safe to persist in a failure artifact.
    """

    if extraction is None:
        return {"fact_count": 0, "facts": []}
    return {
        "fact_count": len(extraction.facts),
        "facts": [
            {
                "field_name": fact.field_name.value,
                "source_block_id": fact.value_anchor.source_block_id,
                "anchor_text": fact.value_anchor.anchor_text,
            }
            for fact in extraction.facts
        ],
    }


def classify_empty_repair_response_v02(
    *,
    is_repair_attempt: bool,
    facts: list,
    prior_candidate: "SourceSelectionExtractionV02 | None",
    prior_validation_error: str | None,
) -> EmptyRepairResponseError | None:
    """Detect the empty-repair-response failure without merging or asserting validity.

    Returns the (unraised) error only when this attempt was itself a repair
    -- one sent with ``previous_selection``/``server_validation_errors`` --
    and it came back with zero facts.  A first attempt returning zero facts
    is not this failure: it returns ``None`` so the existing one-retry loop
    handles it exactly as before.
    """

    if not is_repair_attempt or facts:
        return None
    return EmptyRepairResponseError(
        summarize_prior_candidate_v02(prior_candidate), prior_validation_error
    )


def validate_component_structure_v02(
    extraction: "SourceSelectionExtractionV02",
    pack: CandidatePack,
) -> None:
    """Reject a package label when selected evidence explicitly shows stages.

    This is intentionally a narrow contradiction check, not a general-purpose
    document classifier.  It fires only when *different selected components*
    themselves cite an explicit earlier-stage marker and an explicit final-stage
    marker.  Ambiguous education or grant text by itself is left untouched.
    """

    early_components, final_components = _explicit_sequential_component_ids(extraction, pack)
    sequential_components = set(early_components) | set(final_components)
    if (
        early_components
        and final_components
        and len(sequential_components) >= 2
        and any(
            component.component_kind != ComponentKind.STAGE_SUPPORT
            for component in extraction.support_components
            if component.support_component_id in sequential_components
        )
    ):
        raise ValueError(
            "selected component evidence contains explicit earlier-stage and final-stage markers "
            f"(earlier={sorted(early_components)}, final={sorted(final_components)}); "
            "those components must use component_kind=stage_support and component_decision=stages"
        )


class SourceSelectedComponent(StrictModel):
    """A package or stage identified only by canonical source references."""

    support_component_id: str = Field(min_length=1)
    component_kind: ComponentKind
    source_block_ids: list[str] = Field(min_length=1)
    table_block_ids: list[str] = Field(default_factory=list)
    name_anchor: SourceTextAnchor | None = None

    @model_validator(mode="after")
    def component_name_anchor_is_within_component_source(self) -> "SourceSelectedComponent":
        if self.name_anchor and self.name_anchor.source_block_id not in set(self.source_block_ids) | set(self.table_block_ids):
            raise PydanticCustomError(
                COMPONENT_NAME_ANCHOR_SOURCE_ERROR,
                "component name_anchor must reference a component source block",
            )
        return self


class ComponentDecision(StrictModel):
    """Mandatory one-call declaration of the notice's component structure."""

    mode: ComponentDecisionMode
    # Only used when the model declares no component.  It is a controlled
    # decision label, not a free-text explanation or business value.
    no_component_reason: str | None = None

    @model_validator(mode="after")
    def none_reason_matches_mode(self) -> "ComponentDecision":
        if self.mode == ComponentDecisionMode.NONE and not self.no_component_reason:
            raise ValueError("component_decision none requires no_component_reason")
        if self.mode != ComponentDecisionMode.NONE and self.no_component_reason is not None:
            raise ValueError("no_component_reason is only allowed for component_decision none")
        return self


class SourceSelectionExtraction(StrictModel):
    notice_id: str = Field(min_length=1)
    candidate_pack_id: str = Field(min_length=1)
    # Default permits historical artifact reads.  The OpenAI schema generator
    # marks every property required, so new one-call responses must declare it.
    component_decision: ComponentDecision = Field(
        default_factory=lambda: ComponentDecision(
            mode=ComponentDecisionMode.NONE,
            no_component_reason="legacy_artifact_without_component_decision",
        )
    )
    support_components: list[SourceSelectedComponent] = Field(default_factory=list)
    facts: list[SourceSelectedFact] = Field(default_factory=list)

    @model_validator(mode="after")
    def fact_ids_are_unique(self) -> "SourceSelectionExtraction":
        fact_ids = [fact.fact_id for fact in self.facts]
        if len(fact_ids) != len(set(fact_ids)):
            raise ValueError("fact_id values must be unique")
        component_ids = [component.support_component_id for component in self.support_components]
        if len(component_ids) != len(set(component_ids)):
            raise ValueError("support_component_id values must be unique")
        if self.component_decision.mode == ComponentDecisionMode.NONE and component_ids:
            raise ValueError("component_decision none cannot include support components")
        if self.component_decision.mode != ComponentDecisionMode.NONE and not component_ids:
            raise ValueError("non-none component_decision requires support components")
        kinds = {component.component_kind for component in self.support_components}
        expected_kind = {
            ComponentDecisionMode.PACKAGES: ComponentKind.SUPPORT_PACKAGE,
            ComponentDecisionMode.PARTICIPATION_TYPES: ComponentKind.PARTICIPATION_TYPE,
            ComponentDecisionMode.STAGES: ComponentKind.STAGE_SUPPORT,
        }.get(self.component_decision.mode)
        if expected_kind is not None and kinds != {expected_kind}:
            raise ValueError("component_decision mode does not match support component kinds")
        primary_ids = {
            fact.primary_component_id or fact.support_component_id
            for fact in self.facts
            if fact.primary_component_id or fact.support_component_id
        }
        contextual_ids = {component_id for fact in self.facts for component_id in fact.applicability_component_ids}
        unknown_component_ids = (primary_ids | contextual_ids) - set(component_ids)
        if unknown_component_ids:
            raise ValueError(f"facts reference unknown support components: {sorted(unknown_component_ids)}")
        referenced_fact_ids = {
            fact_id
            for fact in self.facts
            for fact_id in [*fact.modifies_fact_ids, *fact.recipient_fact_ids, *fact.basis_fact_ids]
        }
        unknown_fact_ids = referenced_fact_ids - set(fact_ids)
        if unknown_fact_ids:
            raise ValueError(f"facts reference unknown facts: {sorted(unknown_fact_ids)}")
        return self


class SourceSelectionExtractionV02(StrictModel):
    """OpenAI Structured Output contract used only by new v0.2 runs."""

    notice_id: str = Field(min_length=1)
    candidate_pack_id: str = Field(min_length=1)
    component_decision: ComponentDecision
    support_components: list[SourceSelectedComponent] = Field(default_factory=list)
    facts: list[SourceSelectedFactV02] = Field(default_factory=list)
    support_facets: list[SupportFacetsProjection] = Field(default_factory=list)
    support_scale_measures: list[SupportScaleMeasuresProjection] = Field(default_factory=list)

    @model_validator(mode="after")
    def validates_references(self) -> "SourceSelectionExtractionV02":
        fact_ids = [fact.fact_id for fact in self.facts]
        if len(fact_ids) != len(set(fact_ids)):
            raise ValueError("fact_id values must be unique")
        component_ids = {component.support_component_id for component in self.support_components}
        references = {component_id for fact in self.facts for component_id in [fact.primary_component_id, *fact.applicability_component_ids] if component_id}
        if references - component_ids:
            raise ValueError(f"facts reference unknown support components: {sorted(references - component_ids)}")
        fact_refs = {fact_id for fact in self.facts for fact_id in [*fact.modifies_fact_ids, *fact.recipient_fact_ids, *fact.basis_fact_ids]}
        if fact_refs - set(fact_ids):
            raise ValueError(f"facts reference unknown facts: {sorted(fact_refs - set(fact_ids))}")
        facet_refs = {fact_id for facet in self.support_facets for fact_id in facet.source_fact_ids}
        if facet_refs - set(fact_ids):
            raise ValueError(f"support facets reference unknown facts: {sorted(facet_refs - set(fact_ids))}")
        scale_refs = {
            fact_id for projection in self.support_scale_measures for fact_id in projection.source_fact_ids
        }
        measure_refs = {
            measure.source_fact_id
            for projection in self.support_scale_measures
            for measure in projection.measures
        }
        if (scale_refs | measure_refs) - set(fact_ids):
            raise ValueError(
                "support scale measures reference unknown facts: "
                f"{sorted((scale_refs | measure_refs) - set(fact_ids))}"
            )
        return self


def validate_scale_measure_candidates_v02(
    extraction: SourceSelectionExtractionV02,
    measures_projections: list[SupportScaleMeasuresProjection],
    candidates: list[NumericCandidate],
    resolved_value_sources: dict[str, ValueSource],
    *,
    support_cap_check_state: str = "not_checked",
) -> None:
    """Ensure each derived measure binds to its fact's own resolved span.

    ``measures_projections`` is the server-derived output of
    ``derive_support_scale_measures_v02`` -- the model's own
    ``support_scale_measures`` is always an empty placeholder by contract, so
    validating it would check nothing.  Binding is by exact character-span
    containment inside ``resolved_value_sources[fact_id]`` -- never by
    ``anchor_text`` containment, which cannot distinguish two occurrences of
    the same number elsewhere in the same block.
    """

    candidate_by_id = {candidate.numeric_candidate_id: candidate for candidate in candidates}
    facts = {fact.fact_id: fact for fact in extraction.facts}
    measured_fact_ids: set[str] = set()
    for projection in measures_projections:
        for measure in projection.measures:
            validate_support_scale_measure_shape(measure)
            fact = facts.get(measure.source_fact_id)
            candidate = candidate_by_id.get(measure.source_numeric_candidate_id)
            if fact is None or fact.field_name != FactField.SUPPORT_SCALE:
                raise ValueError(
                    f"support scale measure {measure.source_numeric_candidate_id} must reference a support_scale fact"
                )
            if candidate is None:
                raise ValueError(f"unknown source_numeric_candidate_id: {measure.source_numeric_candidate_id}")
            resolved = resolved_value_sources.get(fact.fact_id)
            if resolved is None or not _numeric_candidate_within_value_source(candidate, resolved):
                raise ValueError(
                    "numeric candidate must fall within its source support_scale fact's resolved value_source span"
                )
            measured_fact_ids.add(measure.source_fact_id)

    # A selected scale fact that contains an enumerated numeric expression is
    # not merely a prose label: it is the precise input this projection exists
    # to normalize.  Requiring coverage here lets the repair turn correct a
    # lazy empty projection without a separate model call.
    required_fact_ids = {
        fact.fact_id
        for fact in extraction.facts
        if fact.field_name == FactField.SUPPORT_SCALE
        and fact.value_anchor is not None
        and (resolved := resolved_value_sources.get(fact.fact_id)) is not None
        and any(_numeric_candidate_within_value_source(candidate, resolved) for candidate in candidates)
    }
    missing_fact_ids = required_fact_ids - measured_fact_ids
    if missing_fact_ids:
        candidates_by_fact_id = {
            fact_id: sum(
                1
                for candidate in candidates
                if _numeric_candidate_within_value_source(
                    candidate, resolved_value_sources[fact_id]
                )
            )
            for fact_id in missing_fact_ids
        }
        raise SupportScaleFactRepairError([
            {
                "fact_id": fact_id,
                "source_block_id": resolved_value_sources[fact_id].source_block_id,
                "reason": "no_supported_measure_derived",
                "numeric_candidate_count": candidates_by_fact_id[fact_id],
                "derived_measure_count": 0,
            }
            for fact_id in sorted(missing_fact_ids)
        ], support_cap_check_state=support_cap_check_state)


@dataclass(frozen=True)
class MaterializedEvidence:
    """Exact server-derived source text, never authored by the model."""

    fact_id: str
    field_name: FactField
    status: SelectionStatus
    semantic_role: str | None
    subject_role: str | None
    source_blocks: list[dict[str, object]]
    value_source: ValueSource | None
    context_blocks: list[dict[str, object]]
    organization_names: list[str]
    organization_sources: list[ValueSource]
    role_raw: str | None
    role_source_block_id: str | None
    role_source: ValueSource | None
    canonical_role: DeliveryRoleCanonical | None
    primary_component_id: str | None
    applicability_component_ids: list[str]
    modifies_fact_ids: list[str]
    recipient_fact_ids: list[str]
    basis_fact_ids: list[str]


@dataclass(frozen=True)
class MaterializedComponent:
    support_component_id: str
    name_raw: str | None
    name_source_block_id: str | None


def _materialized_source_block(
    pack: CandidatePack,
    block,
    *,
    text: str,
) -> dict[str, object]:
    """Keep CandidatePack span identity separate from Common IR provenance."""

    materialized: dict[str, object] = {
        "source_block_id": block.block_id,
        "text": text,
        "section_id": block.section_id,
        "source_occurrence_ids": stable_unique_occurrence_ids(
            block.source_occurrence_ids
        ),
    }
    if pack.common_ir_document_id is not None:
        # CandidatePack's model validator guarantees common_ir_block_id for
        # every block in a Common IR pack.  The explicit check keeps this
        # helper fail-closed if it is ever called with an unvalidated object.
        if block.common_ir_block_id is None:
            raise ValueError(f"Common IR source block provenance missing: {block.block_id}")
        materialized.update({
            "common_ir_document_id": pack.common_ir_document_id,
            "common_ir_block_id": block.common_ir_block_id,
            "common_ir_occurrence_ids": stable_unique_occurrence_ids(
                block.common_ir_occurrence_ids
            ),
        })
        if block.common_ir_cell_id is not None:
            materialized["common_ir_cell_id"] = block.common_ir_cell_id
    # Native exact candidates remain exact CandidatePack source blocks, but
    # their durable parent/slice graph is needed to recover the original
    # Common IR text after this materialization boundary.
    materialized.update(native_block_provenance(pack, block))
    return materialized


def _project_unique_semantic_anchor_to_value_block(
    *,
    anchor_text: str,
    anchor_block,
    value_block,
) -> ValueSource | None:
    """Project one exact semantic anchor onto the selected value block.

    Same-block anchors are directly positional.  A sibling Common IR
    projection may also bind when it shares immutable occurrence provenance
    and its literal anchor occurs exactly once in the value block.  No
    cross-block text match is trusted without that provenance intersection.
    """

    if not anchor_text or len(find_all_occurrences(anchor_block.text, anchor_text)) != 1:
        return None
    if anchor_block.block_id != value_block.block_id:
        anchor_occurrences = set(anchor_block.common_ir_occurrence_ids)
        value_occurrences = set(value_block.common_ir_occurrence_ids)
        if not anchor_occurrences or not anchor_occurrences.intersection(value_occurrences):
            return None
    value_starts = find_all_occurrences(value_block.text, anchor_text)
    if len(value_starts) != 1:
        return None
    start = value_starts[0]
    return ValueSource(
        source_block_id=value_block.block_id,
        start_char=start,
        end_char=start + len(anchor_text),
    )


def _semantic_binding_sources_for_fact(
    fact,
    extraction: SourceSelectionExtraction,
    by_id: Mapping[str, object],
    *,
    value_block,
) -> tuple[ValueSource, ...]:
    """Recover only server-verifiable anchors that identify a fact's locality."""

    sources: list[ValueSource] = []

    def add_anchor(anchor: SourceTextAnchor | None) -> None:
        if anchor is None:
            return
        anchor_block = by_id.get(anchor.source_block_id)
        if anchor_block is None:
            return
        source = _project_unique_semantic_anchor_to_value_block(
            anchor_text=anchor.anchor_text,
            anchor_block=anchor_block,
            value_block=value_block,
        )
        if source is not None:
            sources.append(source)

    components = {
        component.support_component_id: component
        for component in extraction.support_components
    }
    component_ids = [
        component_id
        for component_id in (
            fact.primary_component_id,
            fact.support_component_id,
            *fact.applicability_component_ids,
        )
        if component_id
    ]
    for component_id in component_ids:
        component = components.get(component_id)
        if component is None:
            continue
        add_anchor(component.name_anchor)
        for block_id in (*component.source_block_ids, *component.table_block_ids):
            source_block = by_id.get(block_id)
            if source_block is None:
                continue
            projected = _project_unique_semantic_anchor_to_value_block(
                anchor_text=source_block.text,
                anchor_block=source_block,
                value_block=value_block,
            )
            if projected is not None:
                sources.append(projected)

    for block_id in fact.context_source_block_ids:
        context_block = by_id.get(block_id)
        if context_block is None:
            continue
        projected = _project_unique_semantic_anchor_to_value_block(
            anchor_text=context_block.text,
            anchor_block=context_block,
            value_block=value_block,
        )
        if projected is not None:
            sources.append(projected)

    facts = {item.fact_id: item for item in extraction.facts}
    for fact_id in (
        *fact.modifies_fact_ids,
        *fact.recipient_fact_ids,
        *fact.basis_fact_ids,
    ):
        related = facts.get(fact_id)
        if related is not None:
            add_anchor(related.value_anchor)
    for anchor in (*fact.organization_anchors, fact.role_anchor):
        add_anchor(anchor)

    unique_sources = {
        (source.source_block_id, source.start_char, source.end_char): source
        for source in sources
    }
    return tuple(unique_sources.values())


def _fact_occurrence_binding_signature(
    fact,
    extraction: SourceSelectionExtraction,
) -> tuple[object, ...]:
    """Return the semantic bindings that make repeated facts non-interchangeable."""

    incoming_relations = tuple(sorted(
        (other.fact_id, relation_key)
        for other in extraction.facts
        for relation_key in (
            "modifies_fact_ids",
            "recipient_fact_ids",
            "basis_fact_ids",
        )
        if fact.fact_id in getattr(other, relation_key)
    ))
    projection_membership = tuple(
        (projection.projection_type, index)
        for index, projection in enumerate(
            (
                *getattr(extraction, "support_facets", ()),
                *getattr(extraction, "support_scale_measures", ()),
            )
        )
        if fact.fact_id in projection.source_fact_ids
    )
    return (
        fact.field_name,
        fact.status,
        fact.semantic_role,
        fact.subject_role,
        fact.primary_component_id,
        fact.support_component_id,
        tuple(sorted(fact.applicability_component_ids)),
        tuple(sorted(fact.context_source_block_ids)),
        tuple(sorted(fact.modifies_fact_ids)),
        tuple(sorted(fact.recipient_fact_ids)),
        tuple(sorted(fact.basis_fact_ids)),
        tuple(
            sorted(
                (anchor.source_block_id, anchor.anchor_text)
                for anchor in fact.organization_anchors
            )
        ),
        (
            fact.role_anchor.source_block_id,
            fact.role_anchor.anchor_text,
        )
        if fact.role_anchor is not None
        else None,
        fact.canonical_role,
        incoming_relations,
        projection_membership,
    )


def materialize_evidence(
    extraction: SourceSelectionExtraction,
    pack: CandidatePack,
    *,
    common_ir_source_sha256: str | None = None,
    resolve_ambiguous_value_anchor: AnchorCorrectionResolver | None = None,
    value_source_overrides: Mapping[str, ValueSource | Mapping[str, object]] | None = None,
) -> list[MaterializedEvidence]:
    """Resolve selected ids against canonical pack text and reject unknown ids.

    By default a ``value_anchor`` that is not unique in its block fails
    closed exactly as before (``materialize_value_source`` raises).  Passing
    ``common_ir_source_sha256`` and ``resolve_ambiguous_value_anchor`` opts a
    caller into CandidatePack Anchor Occurrence Resolver v1: only once the
    plain path has failed is the resolver consulted, and only a genuinely
    ambiguous (repeated) anchor reaches the correction call -- a unique
    anchor always stays on the current one-call path with no candidate IDs
    ever generated.
    """

    by_id = {block.block_id: block for block in pack.blocks}
    for component in extraction.support_components:
        referenced = set(component.source_block_ids) | set(component.table_block_ids)
        unknown = referenced - set(by_id)
        if unknown:
            raise ValueError(
                "support component "
                f"{component.support_component_id} references blocks outside the candidate pack: "
                f"{sorted(unknown)}"
            )
    # Check every model-authored value anchor before materializing any row.
    # This turns all zero-occurrence failures into one bounded retry payload,
    # rather than leaking the first rejected anchor through a generic
    # ``ValueError`` or returning a partially materialized list.  Missing
    # block ids remain ordinary reference-contract errors below; only an
    # anchor absent from a known canonical block is repairable here.
    exact_anchor_repairs = [
        {
            "fact_id": fact.fact_id,
            "field_name": fact.field_name.value,
            "source_block_id": fact.value_anchor.source_block_id,
            "reason": "anchor_not_exact_substring",
        }
        for fact in extraction.facts
        if fact.value_anchor is not None
        and (
            block := by_id.get(fact.value_anchor.source_block_id)
        ) is not None
        and not find_all_occurrences(block.text, fact.value_anchor.anchor_text)
    ]
    if exact_anchor_repairs:
        raise ExactAnchorMaterializationRepairError(exact_anchor_repairs)
    repeated_groups: dict[tuple[str, str], list[object]] = {}
    for fact in extraction.facts:
        if fact.value_anchor is not None:
            repeated_groups.setdefault(
                (
                    fact.value_anchor.source_block_id,
                    fact.value_anchor.anchor_text,
                ),
                [],
            ).append(fact)
    objective_binding_fact_ids = {
        fact.fact_id
        for facts in repeated_groups.values()
        if len(facts) > 1
        and len({
            _fact_occurrence_binding_signature(fact, extraction)
            for fact in facts
        }) > 1
        for fact in facts
    }

    rows: list[MaterializedEvidence] = []
    for fact in extraction.facts:
        if fact.source_block_ids and fact.value_source_block_ids:
            raise ValueError(f"fact {fact.fact_id} must not mix legacy source_block_ids with value_source_block_ids")
        unknown = (
            set(fact.resolved_value_source_block_ids) | set(fact.context_source_block_ids)
        ) - set(by_id)
        if unknown:
            raise ValueError(f"fact {fact.fact_id} references blocks outside the candidate pack: {sorted(unknown)}")
        value_source: ValueSource | None = None
        value_blocks: list[dict[str, object]]
        if fact.value_anchor is not None:
            value_block = by_id[fact.value_anchor.source_block_id]
            override = (value_source_overrides or {}).get(fact.fact_id)
            if override is not None:
                # An occurrence sidecar is allowed only after every coordinate
                # is rechecked against this immutable CandidatePack.  It is
                # therefore an exact-position disambiguation, never a fuzzy
                # occurrence preference or model-authored value.
                value_source = (
                    override
                    if isinstance(override, ValueSource)
                    else ValueSource.model_validate(override)
                )
                if (
                    value_source.source_block_id != value_block.block_id
                    or value_source.start_char < 0
                    or value_source.end_char <= value_source.start_char
                    or value_source.end_char > len(value_block.text)
                    or value_block.text[value_source.start_char:value_source.end_char]
                    != fact.value_anchor.anchor_text
                ):
                    raise ValueError(
                        "value_source override must match the selected exact anchor in its source block"
                    )
                value_raw = fact.value_anchor.anchor_text
            else:
                try:
                    value_raw, value_source = materialize_value_source(
                        value_block.block_id, value_block.text, fact.value_anchor.anchor_text
                    )
                except ValueError:
                    if common_ir_source_sha256 is None or resolve_ambiguous_value_anchor is None:
                        raise
                    value_raw, value_source = resolve_value_anchor_with_occurrence_resolver(
                        fact,
                        pack,
                        common_ir_source_sha256=common_ir_source_sha256,
                        correction_resolver=resolve_ambiguous_value_anchor,
                        semantic_binding_sources=_semantic_binding_sources_for_fact(
                            fact,
                            extraction,
                            by_id,
                            value_block=value_block,
                        ),
                        require_objective_semantic_binding=(
                            fact.fact_id in objective_binding_fact_ids
                        ),
                    )
            value_blocks = [
                _materialized_source_block(pack, value_block, text=value_raw)
            ]
        else:
            value_blocks = [
                _materialized_source_block(pack, by_id[block_id], text=by_id[block_id].text)
                for block_id in fact.resolved_value_source_block_ids
            ]
        organization_names: list[str] = []
        organization_sources: list[ValueSource] = []
        for anchor in fact.organization_anchors:
            block = by_id.get(anchor.source_block_id)
            if block is None:
                raise ValueError(f"fact {fact.fact_id} organization anchor references an unknown block")
            if block.text.count(anchor.anchor_text) != 1:
                raise ValueError(
                    f"fact {fact.fact_id} organization anchor must occur exactly once in its source block"
                )
            start_char = block.text.index(anchor.anchor_text)
            name = block.text[start_char : start_char + len(anchor.anchor_text)]
            organization_names.append(name)
            organization_sources.append(ValueSource(
                source_block_id=block.block_id,
                start_char=start_char,
                end_char=start_char + len(anchor.anchor_text),
            ))
        role_raw = None
        role_source_block_id = None
        role_source: ValueSource | None = None
        if fact.role_anchor is not None:
            role_block = by_id.get(fact.role_anchor.source_block_id)
            if role_block is None or role_block.text.count(fact.role_anchor.anchor_text) != 1:
                raise ValueError(f"fact {fact.fact_id} role anchor must occur exactly once in its source block")
            start_char = role_block.text.index(fact.role_anchor.anchor_text)
            role_raw = role_block.text[start_char : start_char + len(fact.role_anchor.anchor_text)]
            role_source_block_id = role_block.block_id
            role_source = ValueSource(
                source_block_id=role_block.block_id,
                start_char=start_char,
                end_char=start_char + len(fact.role_anchor.anchor_text),
            )
        rows.append(
            MaterializedEvidence(
                fact_id=fact.fact_id,
                field_name=fact.field_name,
                status=fact.status,
                semantic_role=fact.semantic_role,
                subject_role=fact.subject_role,
                source_blocks=value_blocks,
                value_source=value_source,
                context_blocks=[
                    _materialized_source_block(pack, by_id[block_id], text=by_id[block_id].text)
                    for block_id in fact.context_source_block_ids
                ],
                organization_names=organization_names,
                organization_sources=organization_sources,
                role_raw=role_raw,
                role_source_block_id=role_source_block_id,
                role_source=role_source,
                canonical_role=fact.canonical_role,
            primary_component_id=fact.primary_component_id or fact.support_component_id,
            applicability_component_ids=fact.applicability_component_ids,
            modifies_fact_ids=fact.modifies_fact_ids,
            recipient_fact_ids=fact.recipient_fact_ids,
            basis_fact_ids=fact.basis_fact_ids,
            )
        )
    return rows


def materialize_components(
    extraction: SourceSelectionExtraction,
    pack: CandidatePack,
) -> list[MaterializedComponent]:
    """Recover component names from exact source anchors, never model wording."""

    by_id = {block.block_id: block for block in pack.blocks}
    result: list[MaterializedComponent] = []
    for component in extraction.support_components:
        anchor = component.name_anchor
        if anchor is None:
            result.append(MaterializedComponent(component.support_component_id, None, None))
            continue
        block = by_id.get(anchor.source_block_id)
        if block is None or block.text.count(anchor.anchor_text) != 1:
            raise ValueError(
                f"support component {component.support_component_id} name_anchor must occur exactly once in its source block"
            )
        result.append(
            MaterializedComponent(component.support_component_id, anchor.anchor_text, anchor.source_block_id)
        )
    return result
