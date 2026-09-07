"""A-profile source selection with server-side evidence materialization."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .anchor_occurrence_resolver import (
    AnchorOccurrenceRequest,
    AnchorOccurrenceResolution,
    materialize_selected_anchor_candidate,
    resolve_anchor_occurrences,
)
from .models import CandidatePack, ComponentKind, FactField
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
    fact_id: str, field_name: FactField, resolution: AnchorOccurrenceResolution
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


def resolve_value_anchor_with_occurrence_resolver(
    fact: "SourceSelectedFact | SourceSelectedFactV02",
    pack: CandidatePack,
    *,
    common_ir_source_sha256: str,
    correction_resolver: AnchorCorrectionResolver,
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

    correction_request = build_anchor_correction_request(fact.fact_id, fact.field_name, resolution)
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
        return materialize_selected_anchor_candidate(resolution, selected_candidate_id)
    except ValueError as error:
        raise AmbiguousAnchorCorrectionError(
            fact_id=fact.fact_id,
            source_block_id=anchor.source_block_id,
            anchor_text=anchor.anchor_text,
            candidate_count=len(resolution.candidates),
            reason=str(error),
        ) from error


def memoize_anchor_correction_resolver(resolver: AnchorCorrectionResolver) -> AnchorCorrectionResolver:
    """Cache one correction choice per deterministic resolution.

    Two correction requests are the same deterministic resolution exactly
    when they share ``(source_block_id, anchor_text)``: the resolver's own
    candidate set (ids, text, context) depends only on those two values plus
    the fixed CandidatePack/Common IR lineage already validated upstream, and
    is otherwise identical regardless of which fact_id asked for it.  This
    matters because ``apply_finalize_with_fallback_v02`` can run
    ``materialize_evidence`` twice for what is otherwise the very same
    ambiguous anchor -- once for its merged attempt, once for its unmerged
    fallback -- and a repeat call for an unchanged resolution must reuse the
    first server-issued choice rather than asking the correction model again.
    """

    cache: dict[tuple[str, str], str] = {}

    def resolve(request: AnchorCorrectionRequest) -> str:
        key = (request.source_block_id, request.anchor_text)
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


_NUMERIC_CANDIDATE_PATTERN = re.compile(
    r"(?:\d[\d,]*\s*(?:천|만|억)?\s*원|\d+(?:\.\d+)?\s*%|\d+\s*(?:개사|개팀|개 과제|개과제|명|팀|사))"
)

_AMOUNT_PATTERN = re.compile(r"(?P<number>\d[\d,]*)\s*(?P<suffix>천|만|억)?\s*원")
_RATE_PATTERN = re.compile(r"(?P<number>\d+(?:\.\d+)?)\s*%")
_COUNT_PATTERN = re.compile(r"(?P<number>\d+)\s*(?P<unit>개사|개팀|개 과제|개과제|명|팀|사)")

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

# An explicit support-scale cap: a bound marker directly followed by the
# amount or rate it bounds.  A count/headcount (e.g. "최대 2명") is a
# personnel/eligibility limit, not a support-scale amount gate, and a
# duration (개월/년/...) is support_period, not support_scale -- neither
# belongs in this pattern.
_AMOUNT_OR_RATE_CANDIDATE_PATTERN = re.compile(
    r"(?:\d[\d,]*\s*(?:천|만|억)?\s*원|\d+(?:\.\d+)?\s*%)"
)
_EXPLICIT_CAP_PATTERN = re.compile(
    rf"(?:최대|한도|상한)\s*[:：]?\s*{_AMOUNT_OR_RATE_CANDIDATE_PATTERN.pattern}"
)


def build_numeric_candidates(pack: CandidatePack) -> list[NumericCandidate]:
    """Enumerate exact numeric expressions; semantics remain model-selected."""

    candidates: list[NumericCandidate] = []
    for block in pack.blocks:
        for index, match in enumerate(_NUMERIC_CANDIDATE_PATTERN.finditer(block.text)):
            candidates.append(NumericCandidate(
                numeric_candidate_id=f"{block.block_id}#num[{index}]",
                source_block_id=block.block_id,
                anchor_text=match.group(0),
                start_char=match.start(),
                end_char=match.end(),
            ))
    return candidates


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
        text = fact.value_anchor.anchor_text
        # Table cells often contain only ``18억원`` while their row/column
        # header explicitly says ``업체당 지원한도``.  A support cap is then a
        # structural fact, not a guess from the numeric cell.  The runner
        # supplies candidate-pack text for the selected context blocks.
        context_text = "\n".join(
            source_block_texts.get(block_id, "")
            for block_id in fact.context_source_block_ids
        ) if source_block_texts else ""
        has_limit_context = bool(re.search(r"(?:지원|융자|보증)?\s*한도|상한", context_text))
        for candidate in candidates_by_block.get(resolved.source_block_id, []):
            if not _numeric_candidate_within_value_source(candidate, resolved):
                continue
            amount = _AMOUNT_PATTERN.fullmatch(candidate.anchor_text)
            rate = _RATE_PATTERN.fullmatch(candidate.anchor_text)
            count = _COUNT_PATTERN.fullmatch(candidate.anchor_text)
            comparator = Comparator.EQ
            lower_value: int | None
            upper_value: int | None
            if re.search(r"(?:최대|이내|한도|상한)", text):
                comparator, lower_value, upper_value = Comparator.LTE, None, 0
            elif re.search(r"(?:최소|이상|하한)", text):
                comparator, lower_value, upper_value = Comparator.GTE, 0, None
            elif re.search(r"(?:내외|약|정도)", text):
                comparator, lower_value, upper_value = Comparator.APPROX, 0, 0
            else:
                lower_value = upper_value = 0

            if amount:
                factor = {None: 1, "천": 1_000, "만": 10_000, "억": 100_000_000}[amount.group("suffix")]
                value = int(amount.group("number").replace(",", "")) * factor
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
                value = int(float(rate.group("number")) * 100)
                measure_type, role, unit = MeasureType.RATE, MeasureRole.SUPPORT_RATE, "BPS"
            elif count:
                value = int(count.group("number"))
                measure_type, role, unit = MeasureType.COUNT, MeasureRole.SELECTION_CAPACITY, count.group("unit")
            else:
                continue

            if comparator in {Comparator.LTE, Comparator.LT}:
                upper_value = value
            elif comparator in {Comparator.GTE, Comparator.GT}:
                lower_value = value
            else:
                lower_value = upper_value = value

            applies_per = None
            aggregation_scope = None
            if "기업당" in text:
                applies_per, aggregation_scope = "COMPANY", AggregationScope.PER_UNIT
            elif "팀당" in text:
                applies_per, aggregation_scope = "TEAM", AggregationScope.PER_UNIT
            elif "인당" in text or "1인당" in text:
                applies_per, aggregation_scope = "PERSON", AggregationScope.PER_UNIT
            elif re.search(r"(?:총\s*|전체\s*)", text):
                aggregation_scope = AggregationScope.TOTAL

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


def validate_selection_quality_v02(extraction: "SourceSelectionExtractionV02") -> None:
    """Reject contract-invalid composite numeric anchors before assembly.

    This is deliberately a narrow provenance guard, not a semantic classifier:
    an explicitly duration-bearing anchor cannot be a ``support_scale`` value
    under the v0.2 field contract.  The LLM must select the atomic amount,
    rate, count, or limit span instead.  Keeping the check here gives a
    one-retry runner a concrete, non-answer-bearing correction signal.
    """

    duration_pattern = re.compile(r"(?:\d|[일이삼사오육칠팔구십])\s*(?:개월|개월간|년|년간|주|주간|일간)")
    invalid = [
        fact.fact_id
        for fact in extraction.facts
        if fact.field_name == FactField.SUPPORT_SCALE
        and duration_pattern.search(fact.value_anchor.anchor_text)
    ]
    if invalid:
        raise ValueError(
            "support_scale anchors must not contain a duration; select separate atomic spans "
            f"for these fact ids: {', '.join(invalid)}"
        )

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
        fact.fact_id
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
    if activity_count:
        raise ValueError(
            "support_scale is limited to beneficiary count, amount, rate, or limit; "
            "an activity/session count such as N회 must not be emitted as support_scale: "
            f"{', '.join(activity_count)}"
        )

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

    generic_exclusion_result = re.compile(r"^\s*(?:지원\s*)?(?:대상에서\s*)?(?:제외됨|제외|불가|제한됨)\s*[.。]?$" )
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

    # One exact raw span has one Raw Fact identity.  If it has several
    # possible semantic facets, retain it in one field and defer additional
    # interpretation to a derived projection; never duplicate it across A
    # fields and leave discovery of the clash to final assembly.
    span_claims: dict[tuple[str, str], list[str]] = {}
    for fact in extraction.facts:
        key = (fact.value_anchor.source_block_id, fact.value_anchor.anchor_text)
        span_claims.setdefault(key, []).append(f"{fact.fact_id} ({fact.field_name.value})")
    duplicate_claims = [", ".join(claims) for claims in span_claims.values() if len(claims) > 1]
    if duplicate_claims:
        raise ValueError(
            "one exact value anchor must not be claimed by multiple Raw Facts; choose one field and preserve "
            f"other meaning only as a derived facet: {'; '.join(duplicate_claims)}"
        )

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


def validate_support_cap_completeness_v02(
    extraction: "SourceSelectionExtractionV02",
    pack: CandidatePack,
) -> None:
    """Reject a selection that leaves an explicit support-scale cap unselected.

    This is a narrow completeness guard, not a semantic classifier: it fires
    only when a block the model has *already* selected as evidence -- a
    component's source/table block, or some fact's value or context block --
    contains an explicit "최대/한도/상한 + amount or rate" cap expression that
    no selected ``support_scale`` fact anchors *in full, including its bound
    marker*.  A cap embedded in a block the model never referenced is left
    untouched; this never scans undiscovered text, and it never invents the
    missing fact itself -- it only forces a one-retry runner to select it.
    A headcount cap (e.g. 최대 2명) is a personnel/eligibility limit, not a
    support-scale amount gate, and is intentionally not checked here.
    """

    block_texts = {block.block_id: block.text for block in pack.blocks}
    evidence_block_ids: set[str] = set()
    for component in extraction.support_components:
        evidence_block_ids.update(component.source_block_ids)
        evidence_block_ids.update(component.table_block_ids)
    for fact in extraction.facts:
        if fact.value_anchor is not None:
            evidence_block_ids.add(fact.value_anchor.source_block_id)
        evidence_block_ids.update(fact.context_source_block_ids)

    captured_spans: dict[str, list[str]] = {}
    for fact in extraction.facts:
        if fact.field_name == FactField.SUPPORT_SCALE and fact.value_anchor is not None:
            captured_spans.setdefault(fact.value_anchor.source_block_id, []).append(
                fact.value_anchor.anchor_text
            )

    missing: list[str] = []
    for block_id in sorted(evidence_block_ids):
        text = block_texts.get(block_id)
        if text is None:
            continue
        for match in _EXPLICIT_CAP_PATTERN.finditer(text):
            # The selected anchor must contain the whole bound expression --
            # marker and amount/rate together -- not merely the bare numeral,
            # so a selected support_scale fact preserves deterministic
            # support_limit semantics (comparator=lte) rather than looking
            # like a plain support_amount/support_rate value.
            cap_text = match.group(0).strip()
            if any(cap_text in span for span in captured_spans.get(block_id, [])):
                continue
            missing.append(f"{block_id}: {cap_text}")
    if missing:
        raise ValueError(
            "an explicit support cap (최대/한도/상한 + an amount or rate) appears in already-selected evidence "
            "but no support_scale fact anchors the full bound expression (marker and amount/rate together); "
            f"select it as its own atomic support_scale span: {', '.join(sorted(set(missing)))}"
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
            raise ValueError("component name_anchor must reference a component source block")
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
        raise ValueError(
            "every selected numeric support_scale fact must have at least one "
            "support_scale_measure; missing source_fact_id values: "
            f"{sorted(missing_fact_ids)}"
        )


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
        "source_occurrence_ids": block.source_occurrence_ids,
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
            "common_ir_occurrence_ids": list(block.common_ir_occurrence_ids),
        })
        if block.common_ir_cell_id is not None:
            materialized["common_ir_cell_id"] = block.common_ir_cell_id
    return materialized


def materialize_evidence(
    extraction: SourceSelectionExtraction,
    pack: CandidatePack,
    *,
    common_ir_source_sha256: str | None = None,
    resolve_ambiguous_value_anchor: AnchorCorrectionResolver | None = None,
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
