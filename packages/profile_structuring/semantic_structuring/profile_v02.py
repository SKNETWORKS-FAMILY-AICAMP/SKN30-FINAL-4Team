"""Storage contract and deterministic validation for existing_program_profile/v0.2.

This module intentionally contains no model calls.  A selection model may point
at source text, but only this module turns that selection into persisted text
and provenance.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ValueSource(StrictModel):
    """One server-verified, half-open Unicode-code-point source span."""

    source_block_id: str = Field(min_length=1)
    start_char: int = Field(ge=0)
    end_char: int = Field(gt=0)
    text_basis: Literal["common_ir_v1_candidate_pack"] = "common_ir_v1_candidate_pack"

    @model_validator(mode="after")
    def ordered(self) -> "ValueSource":
        if self.end_char <= self.start_char:
            raise ValueError("value source end_char must be greater than start_char")
        return self


class MeasureType(StrEnum):
    COUNT = "count"
    AMOUNT = "amount"
    RATE = "rate"


class MeasureRole(StrEnum):
    SELECTION_CAPACITY = "selection_capacity"
    SUPPORT_AMOUNT = "support_amount"
    SUPPORT_LIMIT = "support_limit"
    SUPPORT_RATE = "support_rate"


class Comparator(StrEnum):
    EQ = "eq"
    LT = "lt"
    LTE = "lte"
    GT = "gt"
    GTE = "gte"
    RANGE = "range"
    APPROX = "approx"


class AggregationScope(StrEnum):
    TOTAL = "TOTAL"
    PER_UNIT = "PER_UNIT"


class CalculationBasis(StrEnum):
    """Controlled comparison bases; source wording remains in Raw Facts."""

    WAGE = "WAGE"
    TOTAL_PROJECT_COST = "TOTAL_PROJECT_COST"
    ELIGIBLE_COST = "ELIGIBLE_COST"


class SupportScaleMeasure(StrictModel):
    measure_type: MeasureType
    measure_role: MeasureRole
    lower_value: int | None = Field(default=None, ge=0)
    upper_value: int | None = Field(default=None, ge=0)
    unit: str = Field(min_length=1)
    comparator: Comparator
    source_fact_id: str = Field(min_length=1)
    source_numeric_candidate_id: str = Field(min_length=1)
    applies_per: str | None = None
    calculation_basis: CalculationBasis | None = None
    frequency: str | None = None
    aggregation_scope: AggregationScope | None = None


def validate_support_scale_measure_shape(measure: SupportScaleMeasure) -> None:
    """Validate measure semantics after model parsing.

    The selection response deliberately parses the structural schema first.
    Semantic errors are then returned through the runner's repair loop instead
    of aborting before the model can correct them.
    """

    if measure.measure_type == MeasureType.AMOUNT and measure.unit != "KRW":
        raise ValueError("amount measure unit must be KRW")
    if measure.measure_type == MeasureType.RATE and measure.unit != "BPS":
        raise ValueError("rate measure unit must be BPS")
    if measure.measure_type == MeasureType.COUNT and measure.unit in {"KRW", "BPS", "DAY", "MONTH", "YEAR"}:
        raise ValueError("count measure requires a count unit")
    if measure.comparator in {Comparator.EQ, Comparator.APPROX}:
        if measure.lower_value is None or measure.upper_value is None or measure.lower_value != measure.upper_value:
            raise ValueError("eq/approx require equal lower_value and upper_value")
    elif measure.comparator in {Comparator.LT, Comparator.LTE}:
        if measure.lower_value is not None or measure.upper_value is None:
            raise ValueError("upper-bound comparator requires upper_value only")
    elif measure.comparator in {Comparator.GT, Comparator.GTE}:
        if measure.lower_value is None or measure.upper_value is not None:
            raise ValueError("lower-bound comparator requires lower_value only")
    elif measure.comparator == Comparator.RANGE:
        if measure.lower_value is None or measure.upper_value is None or measure.lower_value > measure.upper_value:
            raise ValueError("range requires ordered lower_value and upper_value")
    if measure.aggregation_scope == AggregationScope.PER_UNIT and not measure.applies_per:
        raise ValueError("PER_UNIT measure requires applies_per")
    if measure.aggregation_scope == AggregationScope.TOTAL and measure.applies_per is not None:
        raise ValueError("TOTAL measure must not have applies_per")


class TargetConstraintsProjection(StrictModel):
    projection_type: Literal["target_constraints"] = "target_constraints"
    positive_source_fact_ids: list[str] = Field(min_length=1)
    exclusion_source_fact_ids: list[str] = Field(default_factory=list)
    entity_types: list[str] = Field(default_factory=list)
    regions: list[str] = Field(default_factory=list)
    industries: list[str] = Field(default_factory=list)
    business_age: dict[str, Any] | None = None
    status: str


class SupportFacetsProjection(StrictModel):
    projection_type: Literal["support_facets"] = "support_facets"
    source_fact_ids: list[str] = Field(min_length=1)
    activities: list[str] = Field(default_factory=list)
    methods: list[str] = Field(default_factory=list)
    items: list[str] = Field(default_factory=list)
    status: str


class SupportScaleMeasuresProjection(StrictModel):
    projection_type: Literal["support_scale_measures"] = "support_scale_measures"
    source_fact_ids: list[str] = Field(min_length=1)
    measures: list[SupportScaleMeasure] = Field(min_length=1)
    status: str


# The version label for the deterministic numeric-candidate extractor
# (source_selection.py's regex-based build_numeric_candidates /
# derive_support_scale_measures_v02).  Processing lineage exposes this only
# at processing_metadata.derived_projection_producers.support_scale_measures.
# numeric_candidate_extractor_version, and only when the assembled profile
# actually carries a support_scale_measures projection derived from it; it is
# otherwise absent, not merely null.
NUMERIC_CANDIDATE_EXTRACTOR_VERSION = "numeric_candidate_v1"

_COMMON_IR_LINEAGE_REQUIRED_KEYS = frozenset({
    "document_id", "schema_version", "source_kind", "source_sha256", "source_location",
})
_COMMON_IR_LINEAGE_OPTIONAL_KEYS = frozenset({"artifact_role"})


def validate_common_ir_lineage(lineage: dict[str, Any]) -> None:
    """Enforce the exact Common IR document lineage shape.

    ``source_documents``/metadata lineage carries only the Common IR
    document's own identity and provenance: exactly ``document_id``,
    ``schema_version`` (``common_ir_v1``), ``source_kind``, ``source_sha256``,
    and ``source_location``, each with a nonempty value, plus an optional
    ``artifact_role``.  No business id (e.g. ``notice_id``) and no second
    artifact's id may be merged in here under an alias.  ``artifact_role``
    must be omitted entirely when not provided; a present-but-null value is
    rejected rather than treated as "absent".
    """

    keys = set(lineage)
    missing = _COMMON_IR_LINEAGE_REQUIRED_KEYS - keys
    if missing:
        raise ValueError(f"Common IR lineage is missing required keys: {sorted(missing)}")
    extra = keys - _COMMON_IR_LINEAGE_REQUIRED_KEYS - _COMMON_IR_LINEAGE_OPTIONAL_KEYS
    if extra:
        raise ValueError(f"Common IR lineage must not carry extra keys: {sorted(extra)}")
    empty = sorted(key for key in _COMMON_IR_LINEAGE_REQUIRED_KEYS if not lineage.get(key))
    if empty:
        raise ValueError(f"Common IR lineage requires nonempty values: {empty}")
    if lineage["schema_version"] != "common_ir_v1":
        raise ValueError("Common IR lineage schema_version must be common_ir_v1")
    if "artifact_role" in lineage and lineage["artifact_role"] is None:
        raise ValueError("Common IR lineage artifact_role must be omitted, not null, when absent")


def find_all_occurrences(text: str, anchor_text: str) -> list[int]:
    """Return every real start offset of ``anchor_text`` in ``text``.

    Shared by ``materialize_value_source``, ``source_selection._anchor_resolves``,
    and the CandidatePack Anchor Occurrence Resolver so all three agree on what
    "unique" means.  ``str.count`` undercounts a self-overlapping repeat --
    ``"aaa".count("aa") == 1`` even though "aa" genuinely occurs twice, at
    offsets 0 and 1 -- so counting occurrences by ``str.count``/``str.index``
    can let such a repeat silently bypass ambiguity handling entirely.
    """

    starts: list[int] = []
    cursor = 0
    while True:
        start = text.find(anchor_text, cursor)
        if start < 0:
            return starts
        starts.append(start)
        cursor = start + 1


def materialize_value_source(source_block_id: str, block_text: str, anchor_text: str) -> tuple[str, ValueSource]:
    """Recover an exact v0.2 value only when an anchor is unique in a block."""

    starts = find_all_occurrences(block_text, anchor_text)
    if len(starts) != 1:
        raise ValueError("value anchor must occur exactly once in its source block")
    start = starts[0]
    source = ValueSource(source_block_id=source_block_id, start_char=start, end_char=start + len(anchor_text))
    return block_text[source.start_char : source.end_char], source


def validate_exact_span(value_raw: str, value_source: dict[str, Any], block_texts: dict[str, str]) -> None:
    source = ValueSource.model_validate(value_source)
    text = block_texts.get(source.source_block_id)
    if text is None:
        raise ValueError(f"unknown value source block: {source.source_block_id}")
    if source.end_char > len(text) or value_raw != text[source.start_char : source.end_char]:
        raise ValueError("value_raw does not exactly match value_source")


def validate_profile_v02(profile: dict[str, Any], block_texts: dict[str, str]) -> list[str]:
    """Return contract violations without mutating the assembled profile."""

    issues: list[str] = []
    if profile.get("schema_version") != "existing_program_profile/v0.2":
        issues.append("schema_version must be existing_program_profile/v0.2")
    if not profile.get("source_profile_id"):
        issues.append("source_profile_id is required and must be nonempty")
    source_documents = profile.get("source_documents", [])
    if not source_documents:
        issues.append("source_documents must include Common IR lineage for at least one document")
    for source_document in source_documents:
        common_ir_lineage = source_document.get("common_ir") if isinstance(source_document, dict) else None
        if not common_ir_lineage:
            issues.append("source_documents: each document requires Common IR lineage")
            continue
        try:
            validate_common_ir_lineage(common_ir_lineage)
        except ValueError as error:
            issues.append(f"source_documents: {error}")
    facts: list[dict[str, Any]] = []
    for rows in profile.get("comparison_profile", {}).values():
        facts.extend(rows)
    for component in profile.get("support_components", []):
        facts.extend(component.get("facts", []))
    facts_by_id = {fact["fact_id"]: fact for fact in facts if fact.get("fact_id")}
    ids = set(facts_by_id)
    used_spans: set[tuple[str, int, int]] = set()
    for fact in facts:
        try:
            source = fact.get("value_source")
            if not source:
                raise ValueError("fact has no single value_source")
            validate_exact_span(fact.get("value_raw", ""), source, block_texts)
            key = (source["source_block_id"], source["start_char"], source["end_char"])
            if key in used_spans:
                raise ValueError("the same value span is used by multiple A facts")
            used_spans.add(key)
        except ValueError as error:
            issues.append(f"{fact.get('fact_id')}: {error}")
        if fact.get("field_name") == "delivery_roles":
            organizations = fact.get("organization_names") or []
            if not organizations or not fact.get("role_source"):
                issues.append(f"{fact.get('fact_id')}: delivery role lacks organization/role provenance")
            for organization in organizations:
                try:
                    validate_exact_span(
                        organization.get("value_raw", ""), organization.get("value_source") or {}, block_texts
                    )
                except (AttributeError, ValueError) as error:
                    issues.append(f"{fact.get('fact_id')}: invalid delivery organization provenance: {error}")
            try:
                role_source = fact["role_source"]
                validate_exact_span(fact.get("role_raw", ""), role_source, block_texts)
            except (KeyError, ValueError) as error:
                issues.append(f"{fact.get('fact_id')}: invalid delivery role provenance: {error}")
    for projection in profile.get("derived_projections", []):
        refs = (
            projection.get("source_fact_ids", [])
            + projection.get("positive_source_fact_ids", [])
            + projection.get("exclusion_source_fact_ids", [])
        )
        missing = set(refs) - ids
        if missing:
            issues.append(f"{projection.get('projection_type')}: dangling source fact ids {sorted(missing)}")
        if projection.get("projection_type") == "support_scale_measures":
            try:
                parsed = SupportScaleMeasuresProjection.model_validate(projection)
                for measure in parsed.measures:
                    validate_support_scale_measure_shape(measure)
                    referenced_fact = facts_by_id.get(measure.source_fact_id)
                    if referenced_fact is None:
                        issues.append(f"support_scale_measure: dangling source_fact_id {measure.source_fact_id}")
                    elif referenced_fact.get("field_name") != "support_scale":
                        issues.append(
                            "support_scale_measure: source_fact_id "
                            f"{measure.source_fact_id} must reference a support_scale Raw Fact"
                        )
            except ValueError as error:
                issues.append(f"support_scale_measures: {error}")
    has_scale_measures_projection = any(
        projection.get("projection_type") == "support_scale_measures"
        for projection in profile.get("derived_projections", [])
    )
    producers = (profile.get("processing_metadata") or {}).get("derived_projection_producers") or {}
    numeric_extractor_version = (producers.get("support_scale_measures") or {}).get(
        "numeric_candidate_extractor_version"
    )
    version_path = (
        "processing_metadata.derived_projection_producers.support_scale_measures."
        "numeric_candidate_extractor_version"
    )
    if has_scale_measures_projection and numeric_extractor_version != NUMERIC_CANDIDATE_EXTRACTOR_VERSION:
        issues.append(
            f"{version_path} is required and must be "
            f"{NUMERIC_CANDIDATE_EXTRACTOR_VERSION!r} when a support_scale_measures projection is present"
        )
    if not has_scale_measures_projection and numeric_extractor_version is not None:
        issues.append(f"{version_path} must only be present alongside a support_scale_measures projection")
    return issues
