"""Pydantic contracts between common IR, LLM semantic extraction, and storage."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    """Reject invented keys so an LLM cannot silently expand the contract."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class FactField(StrEnum):
    PURPOSE_GOAL = "purpose_goal"
    APPLICANT_ELIGIBILITY = "applicant_eligibility"
    SUPPORT_TARGET = "support_target"
    ELIGIBILITY_CONDITIONS = "eligibility_conditions"
    BENEFICIARY = "beneficiary"
    APPLICABLE_ENTITY = "applicable_entity"
    EXCLUSIONS = "exclusions"
    DUPLICATE_SUPPORT_CONDITIONS = "duplicate_support_conditions"
    SUPPORT_METHODS = "support_methods"
    SUPPORT_ACTIVITIES = "support_activities"
    SUPPORT_ITEMS = "support_items"
    SUPPORT_CONTENT = "support_content"
    SUPPORT_SCALE = "support_scale"
    PROGRAM_PERIOD = "program_period"
    SUPPORT_PERIOD = "support_period"
    TOTAL_BUDGET = "total_budget"
    COST_SHARING = "cost_sharing"
    PAYMENT_TERMS = "payment_terms"
    PARTICIPATION_REQUIREMENTS = "participation_requirements"
    DELIVERY_ROLES = "delivery_roles"


class FactStatus(StrEnum):
    IDENTIFIED = "identified"
    PARTIALLY_IDENTIFIED = "partially_identified"
    MENTIONED_NOT_SPECIFIC = "mentioned_not_specific"
    UNRESOLVED = "unresolved"
    NEEDS_REVIEW = "needs_review"


class FactScope(StrEnum):
    NOTICE = "notice"
    COMPONENT = "component"


class ComponentKind(StrEnum):
    SUPPORT_PACKAGE = "support_package"
    PARTICIPATION_TYPE = "participation_type"
    STAGE_SUPPORT = "stage_support"


class TableClass(StrEnum):
    FINANCIAL = "financial"
    RULE = "rule"
    ENTITY_RELATION = "entity_relation"
    CATALOG = "catalog"
    PROCESS = "process"
    FORM_OR_LAYOUT = "form_or_layout"
    UNRESOLVED = "unresolved"


class TablePolicy(StrEnum):
    SEARCH_AND_AGGREGATE = "search_and_aggregate"
    SEARCH_ONLY = "search_only"
    EXCLUDE = "exclude"


class QuantityType(StrEnum):
    SELECTION_CAPACITY = "selection_capacity"
    PER_BENEFICIARY_AMOUNT = "per_beneficiary_amount"
    TOTAL_BUDGET = "total_budget"
    SUPPORT_PERIOD = "support_period"
    RATIO = "ratio"


class MeasureType(StrEnum):
    """Primitive numeric kind after deterministic source parsing."""

    AMOUNT = "amount"
    RATE = "rate"
    COUNT = "count"
    DURATION = "duration"


class MeasureSemanticRole(StrEnum):
    """How a numeric measure functions in a support notice."""

    SUPPORT_LIMIT = "support_limit"
    SELECTION_CAPACITY = "selection_capacity"
    TOTAL_BUDGET = "total_budget"
    COST_SHARE = "cost_share"
    PAYMENT_AMOUNT = "payment_amount"
    SUPPORT_PERIOD = "support_period"
    PROGRAM_PERIOD = "program_period"
    LOAN_LIMIT = "loan_limit"
    GUARANTEE_LIMIT = "guarantee_limit"


class MeasureUnit(StrEnum):
    KRW = "KRW"
    BPS = "BPS"
    PERSON = "PERSON"
    ENTERPRISE = "ENTERPRISE"
    TEAM = "TEAM"
    SITE = "SITE"
    CASE = "CASE"
    ITEM = "ITEM"
    DEVICE = "DEVICE"
    SESSION = "SESSION"
    PROJECT = "PROJECT"
    DAY = "DAY"
    MONTH = "MONTH"
    YEAR = "YEAR"


class NumericComparator(StrEnum):
    EQ = "eq"
    LT = "lt"
    LTE = "lte"
    GT = "gt"
    GTE = "gte"
    RANGE = "range"
    APPROX = "approx"


class MeasureFrequency(StrEnum):
    ONE_TIME = "one_time"
    MONTHLY = "monthly"
    ANNUAL = "annual"
    PERFORMANCE_BASED = "performance_based"
    UNKNOWN = "unknown"


class MoneyBasis(StrEnum):
    SUPPORT_AMOUNT = "support_amount"
    TOTAL_BUDGET = "total_budget"
    PARTICIPANT_COST = "participant_cost"
    ELIGIBLE_COST = "eligible_cost"
    LOAN_PRINCIPAL = "loan_principal"
    GUARANTEE_LIMIT = "guarantee_limit"
    UNKNOWN = "unknown"


class SourceRelation(StrEnum):
    CANDIDATE = "candidate"
    PRECEDING_CONTEXT = "preceding_context"
    FOLLOWING_CONTEXT = "following_context"
    TABLE_HEADER = "table_header"
    COMPETING_CANDIDATE = "competing_candidate"


class ExtractionScope(StrEnum):
    CANDIDATE_PACK = "candidate_pack"
    DOCUMENT = "document"


class SourceBlock(StrictModel):
    """An IR block already selected for one semantic decision."""

    block_id: Annotated[str, Field(min_length=1)]
    text: Annotated[str, Field(min_length=1)]
    relation: SourceRelation
    block_kind: str | None = None
    section_id: str | None = None
    # Internal ordering and immutable Common-IR provenance.  The model may
    # read these but cannot author them; evidence materialization preserves
    # them from the supplied source blocks only.
    source_order: int | None = None
    source_occurrence_ids: list[str] = Field(default_factory=list)
    # CandidatePack-local ``block_id`` is the exact-span locator.  These
    # separate fields point back to the immutable Common IR v1 nodes from
    # which that candidate was projected.  They remain optional so legacy
    # pre-Common-IR fixtures and runners keep their existing contract.
    common_ir_block_id: str | None = Field(default=None, min_length=1, frozen=True)
    common_ir_cell_id: str | None = Field(default=None, min_length=1, frozen=True)
    common_ir_occurrence_ids: tuple[str, ...] = Field(default_factory=tuple, frozen=True)


class CandidatePack(StrictModel):
    """IR input for either a local decision or a document-level evaluation run."""

    pack_id: Annotated[str, Field(min_length=1)]
    notice_id: Annotated[str, Field(min_length=1)]
    extraction_scope: ExtractionScope = ExtractionScope.CANDIDATE_PACK
    question: Annotated[str, Field(min_length=1)]
    blocks: Annotated[list[SourceBlock], Field(min_length=1)]
    # Present as one all-or-none lineage tuple for Common IR v1 projections;
    # omitted for legacy packs.
    generator: str | None = Field(default=None, min_length=1, frozen=True)
    generator_version: str | None = Field(default=None, min_length=1, frozen=True)
    common_ir_document_id: str | None = Field(default=None, min_length=1, frozen=True)

    @model_validator(mode="after")
    def common_ir_lineage_is_complete(self) -> "CandidatePack":
        lineage = (self.generator, self.generator_version, self.common_ir_document_id)
        if any(value is not None for value in lineage) and not all(value is not None for value in lineage):
            raise ValueError(
                "Common IR CandidatePack requires generator, generator_version, "
                "and common_ir_document_id together"
            )
        if self.common_ir_document_id is not None:
            missing = [block.block_id for block in self.blocks if block.common_ir_block_id is None]
            if missing:
                raise ValueError(
                    "Common IR CandidatePack blocks require common_ir_block_id: "
                    f"{missing}"
                )
        return self


class NormalizedQuantity(StrictModel):
    quantity_type: QuantityType
    count: int | None = Field(default=None, ge=0)
    amount_krw: int | None = Field(default=None, ge=0)
    rate: float | None = Field(default=None, ge=0, le=1)
    unit: str | None = None
    bound_type: str | None = None

    @model_validator(mode="after")
    def requires_matching_value(self) -> "NormalizedQuantity":
        if self.quantity_type == QuantityType.SELECTION_CAPACITY and self.count is None:
            raise ValueError("selection_capacity requires count")
        if self.quantity_type in {QuantityType.PER_BENEFICIARY_AMOUNT, QuantityType.TOTAL_BUDGET} and self.amount_krw is None:
            raise ValueError("amount quantity requires amount_krw")
        if self.quantity_type == QuantityType.RATIO and self.rate is None:
            raise ValueError("ratio requires rate")
        if self.quantity_type == QuantityType.SELECTION_CAPACITY and any(
            value is not None for value in (self.amount_krw, self.rate)
        ):
            raise ValueError("selection_capacity cannot also contain amount or rate")
        if self.quantity_type in {QuantityType.PER_BENEFICIARY_AMOUNT, QuantityType.TOTAL_BUDGET} and any(
            value is not None for value in (self.count, self.rate)
        ):
            raise ValueError("amount quantity cannot also contain count or rate")
        if self.quantity_type == QuantityType.RATIO and any(value is not None for value in (self.count, self.amount_krw)):
            raise ValueError("ratio cannot also contain count or amount")
        return self


class NormalizedMeasure(StrictModel):
    """A single comparable measure linked to deterministic IR numeric candidates.

    Values are all integers after normalization: KRW, basis points, counts,
    and day/month/year durations.  This avoids LLM-authored values and binary
    floating-point rates.
    """

    measure_id: str = Field(min_length=1)
    fact_id: str = Field(min_length=1)
    support_component_id: str | None = None
    measure_type: MeasureType
    semantic_role: MeasureSemanticRole
    lower_value: int | None = Field(default=None, ge=0)
    upper_value: int | None = Field(default=None, ge=0)
    unit: MeasureUnit
    comparator: NumericComparator
    applies_per: MeasureUnit | None = None
    frequency: MeasureFrequency | None = None
    money_basis: MoneyBasis | None = None
    source_numeric_candidate_ids: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validates_measure_shape(self) -> "NormalizedMeasure":
        if self.measure_type == MeasureType.AMOUNT and self.unit != MeasureUnit.KRW:
            raise ValueError("amount measures require KRW")
        if self.measure_type == MeasureType.RATE and self.unit != MeasureUnit.BPS:
            raise ValueError("rate measures require BPS")
        if self.measure_type == MeasureType.COUNT and self.unit in {MeasureUnit.KRW, MeasureUnit.BPS, MeasureUnit.DAY, MeasureUnit.MONTH, MeasureUnit.YEAR}:
            raise ValueError("count measures require a count unit")
        if self.measure_type == MeasureType.DURATION and self.unit not in {MeasureUnit.DAY, MeasureUnit.MONTH, MeasureUnit.YEAR}:
            raise ValueError("duration measures require DAY, MONTH, or YEAR")
        if self.measure_type != MeasureType.AMOUNT and self.money_basis is not None:
            raise ValueError("money_basis is only allowed for amount measures")
        if self.comparator in {NumericComparator.EQ, NumericComparator.APPROX}:
            if self.lower_value is None or self.upper_value is None or self.lower_value != self.upper_value:
                raise ValueError("eq and approx measures require equal lower_value and upper_value")
        elif self.comparator in {NumericComparator.LT, NumericComparator.LTE}:
            if self.lower_value is not None or self.upper_value is None:
                raise ValueError("upper-bound measures require only upper_value")
        elif self.comparator in {NumericComparator.GT, NumericComparator.GTE}:
            if self.lower_value is None or self.upper_value is not None:
                raise ValueError("lower-bound measures require only lower_value")
        elif self.comparator == NumericComparator.RANGE:
            if self.lower_value is None or self.upper_value is None or self.lower_value > self.upper_value:
                raise ValueError("range measures require ordered lower_value and upper_value")
        return self


class SupportComponent(StrictModel):
    """A named package, selectable type, or stage; it never owns numeric facts."""

    support_component_id: Annotated[str, Field(min_length=1)]
    component_kind: ComponentKind
    name_raw: Annotated[str, Field(min_length=1)]
    applies_to_raw: str | None = None
    source_block_ids: Annotated[list[str], Field(min_length=1)]


class NoticeIdentity(StrictModel):
    """Notice-level identity; agencies themselves remain delivery-role facts."""

    title_raw: str | None = None
    title_source_block_ids: list[str] = Field(default_factory=list)
    notice_date_raw: str | None = None
    notice_date_source_block_ids: list[str] = Field(default_factory=list)
    source_url: str | None = None


class BaseFact(StrictModel):
    """Common provenance; concrete fact types constrain semantic slots.

    ``value_raw`` is an unmodified source value. Any LLM-produced convenience
    wording belongs in ``model_summary`` and is never evidence.
    """

    fact_id: Annotated[str, Field(min_length=1)]
    kind: str
    value_raw: Annotated[str, Field(min_length=1)]
    model_summary: str | None = None
    source_block_ids: Annotated[list[str], Field(min_length=1)]
    status: FactStatus
    source_heading: str | None = None
    subject_role: str | None = None
    semantic_role: str | None = None


class SupportScaleFact(BaseFact):
    kind: Literal["support_scale"]
    field_name: Literal[FactField.SUPPORT_SCALE, FactField.TOTAL_BUDGET, FactField.PROGRAM_PERIOD, FactField.SUPPORT_PERIOD]
    scope: FactScope
    support_component_id: str | None = None
    applies_to_fact_ids: list[str] = Field(default_factory=list)
    normalized_value: NormalizedQuantity | None = None

    @model_validator(mode="after")
    def component_scope_requires_component_id(self) -> "SupportScaleFact":
        if self.field_name == FactField.PROGRAM_PERIOD and self.scope != FactScope.NOTICE:
            raise ValueError("program_period must be notice-scoped")
        if self.scope == FactScope.COMPONENT and not self.support_component_id:
            raise ValueError("component-scoped fact requires support_component_id")
        if self.scope == FactScope.NOTICE and self.support_component_id is not None:
            raise ValueError("notice-scoped fact cannot have support_component_id")
        if self.status in {FactStatus.MENTIONED_NOT_SPECIFIC, FactStatus.UNRESOLVED, FactStatus.NEEDS_REVIEW} and self.normalized_value:
            raise ValueError("search-only fact status cannot have normalized_value")
        return self


class DeliveryRoleFact(BaseFact):
    kind: Literal["delivery_role"]
    field_name: Literal[FactField.DELIVERY_ROLES] = FactField.DELIVERY_ROLES
    scope: Literal[FactScope.NOTICE] = FactScope.NOTICE
    organization_name: Annotated[str, Field(min_length=1)]
    canonical_role: Annotated[str, Field(min_length=1)]


class PurposeGoalFact(BaseFact):
    kind: Literal["purpose_goal"]
    field_name: Literal[FactField.PURPOSE_GOAL] = FactField.PURPOSE_GOAL
    scope: Literal[FactScope.NOTICE] = FactScope.NOTICE


class TargetFact(BaseFact):
    kind: Literal["target"]
    field_name: Literal[
        FactField.APPLICANT_ELIGIBILITY,
        FactField.SUPPORT_TARGET,
        FactField.ELIGIBILITY_CONDITIONS,
        FactField.BENEFICIARY,
        FactField.APPLICABLE_ENTITY,
    ]
    scope: FactScope = FactScope.NOTICE
    support_component_id: str | None = None

    @model_validator(mode="after")
    def component_scope_requires_component_id(self) -> "TargetFact":
        if self.scope == FactScope.COMPONENT and not self.support_component_id:
            raise ValueError("component-scoped target requires support_component_id")
        return self


class RestrictionFact(BaseFact):
    kind: Literal["restriction"]
    field_name: Literal[FactField.EXCLUSIONS, FactField.DUPLICATE_SUPPORT_CONDITIONS]
    scope: FactScope = FactScope.NOTICE
    support_component_id: str | None = None

    @model_validator(mode="after")
    def component_scope_requires_component_id(self) -> "RestrictionFact":
        if self.scope == FactScope.COMPONENT and not self.support_component_id:
            raise ValueError("component-scoped restriction requires support_component_id")
        return self


class SupportContentFact(BaseFact):
    kind: Literal["support_content"]
    field_name: Literal[
        FactField.SUPPORT_ACTIVITIES,
        FactField.SUPPORT_METHODS,
        FactField.SUPPORT_ITEMS,
        FactField.SUPPORT_CONTENT,
        FactField.PAYMENT_TERMS,
        FactField.PARTICIPATION_REQUIREMENTS,
    ]
    scope: FactScope
    support_component_id: str | None = None

    @model_validator(mode="after")
    def component_scope_requires_component_id(self) -> "SupportContentFact":
        if self.scope == FactScope.COMPONENT and not self.support_component_id:
            raise ValueError("component-scoped support content requires support_component_id")
        return self


class CostSharingFact(BaseFact):
    """Raw cost-sharing evidence; amount/rate conversion is deterministic downstream work."""

    kind: Literal["cost_sharing"]
    field_name: Literal[FactField.COST_SHARING] = FactField.COST_SHARING
    scope: FactScope = FactScope.NOTICE
    support_component_id: str | None = None

    @model_validator(mode="after")
    def component_scope_requires_component_id(self) -> "CostSharingFact":
        if self.scope == FactScope.COMPONENT and not self.support_component_id:
            raise ValueError("component-scoped cost sharing requires support_component_id")
        return self


# OpenAI Structured Outputs accepts ``anyOf`` but not ``oneOf``.  A Pydantic
# discriminated union emits ``oneOf``; the plain union below emits ``anyOf``.
# Every member has a required, mutually exclusive Literal ``kind`` value, so a
# fact can still satisfy only one concrete contract in practice.
StructuredFact = Union[
    SupportScaleFact,
    DeliveryRoleFact,
    PurposeGoalFact,
    TargetFact,
    RestrictionFact,
    SupportContentFact,
    CostSharingFact,
]


class TableCatalogProposal(StrictModel):
    """LLM classification of table meaning; it never chooses product policy."""

    table_ref: Annotated[str, Field(min_length=1)]
    table_class: TableClass
    tags: list[Annotated[str, Field(min_length=1)]] = Field(default_factory=list)
    source_block_ids: Annotated[list[str], Field(min_length=1)]


class TableCatalogEntry(TableCatalogProposal):
    """Server-enriched table record ready for storage and retrieval."""

    policy: TablePolicy

    @model_validator(mode="after")
    def form_layout_is_not_indexable(self) -> "TableCatalogEntry":
        if self.table_class == TableClass.FORM_OR_LAYOUT and self.policy != TablePolicy.EXCLUDE:
            raise ValueError("form_or_layout must use exclude policy")
        if self.table_class == TableClass.UNRESOLVED and self.policy == TablePolicy.SEARCH_AND_AGGREGATE:
            raise ValueError("unresolved table cannot be aggregated")
        return self


class UnresolvedRelation(StrictModel):
    value_raw: Annotated[str, Field(min_length=1)]
    source_block_ids: Annotated[list[str], Field(min_length=1)]
    status: FactStatus = FactStatus.UNRESOLVED

    @model_validator(mode="after")
    def only_safe_unresolved_statuses(self) -> "UnresolvedRelation":
        if self.status not in {FactStatus.UNRESOLVED, FactStatus.NEEDS_REVIEW}:
            raise ValueError("unresolved relation must be unresolved or needs_review")
        return self


class SemanticExtraction(StrictModel):
    """LLM response consumed after a single candidate pack is judged."""

    notice_id: Annotated[str, Field(min_length=1)]
    candidate_pack_id: Annotated[str, Field(min_length=1)]
    identity: NoticeIdentity | None = None
    facts: list[StructuredFact] = Field(default_factory=list)
    support_components: list[SupportComponent] = Field(default_factory=list)
    table_catalog: list[TableCatalogProposal] = Field(default_factory=list)
    unresolved_relations: list[UnresolvedRelation] = Field(default_factory=list)

    @model_validator(mode="after")
    def component_ids_are_unique(self) -> "SemanticExtraction":
        ids = [component.support_component_id for component in self.support_components]
        if len(ids) != len(set(ids)):
            raise ValueError("support_component_id values must be unique")
        fact_ids = [fact.fact_id for fact in self.facts]
        if len(fact_ids) != len(set(fact_ids)):
            raise ValueError("fact_id values must be unique")
        return self


class FlatSemanticExtraction(StrictModel):
    """First-pass document profile: components are intentionally unavailable."""

    notice_id: Annotated[str, Field(min_length=1)]
    candidate_pack_id: Annotated[str, Field(min_length=1)]
    identity: NoticeIdentity | None = None
    facts: list[StructuredFact] = Field(default_factory=list)
    table_catalog: list[TableCatalogProposal] = Field(default_factory=list)
    unresolved_relations: list[UnresolvedRelation] = Field(default_factory=list)

    @model_validator(mode="after")
    def only_notice_scoped_facts(self) -> "FlatSemanticExtraction":
        fact_ids = [fact.fact_id for fact in self.facts]
        if len(fact_ids) != len(set(fact_ids)):
            raise ValueError("fact_id values must be unique")
        for fact in self.facts:
            if fact.scope != FactScope.NOTICE or getattr(fact, "support_component_id", None) is not None:
                raise ValueError("flat extraction permits notice-scoped facts only")
        return self
