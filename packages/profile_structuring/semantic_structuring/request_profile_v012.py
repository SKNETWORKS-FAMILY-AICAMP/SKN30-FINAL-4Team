"""Request Profile v0.1.2 server-side assembly boundary.

This module deliberately has no model client.  It takes a Common IR v1
document, projects it into a CandidatePack, validates an *untrusted* source
selection payload, and materializes the final Request profile from exact
CandidatePack spans.  It is the Request analogue of the Existing v0.2
selection/materialization split, not a second Common IR dialect.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from enum import StrEnum
from hashlib import sha256
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .common_ir_v1 import (
    COMMON_IR_V1_CANDIDATE_PACK_GENERATOR,
    COMMON_IR_V1_CANDIDATE_PACK_GENERATOR_VERSION,
    common_ir_v1_identity,
    project_common_ir_v1,
)
from .models import CandidatePack, ComponentKind, SourceBlock, SourceRelation
from .profile_v02 import ValueSource, find_all_occurrences, materialize_value_source, validate_common_ir_lineage


REQUEST_SCHEMA_VERSION = "pre_review_request_profile/v0.1"
REQUEST_PIPELINE_VERSION = "request_profile_v0.1.2_scaffold"
TEXT_BASIS = "common_ir_v1_candidate_pack"

SHARED_COMPARISON_FIELDS = (
    "purpose_goal", "applicant_eligibility", "support_target",
    "eligibility_conditions", "beneficiary", "exclusions",
    "participation_requirements", "program_period", "support_period",
    "support_activities", "support_methods", "support_items",
    "support_content", "support_scale", "total_budget", "cost_sharing",
)
REQUEST_DELIVERY_FIELDS = ("delivery_relations", "delivery_methods")
REQUEST_CONTEXT_FIELDS = (
    "implementation_plan", "business_need", "legal_basis", "linked_policy",
    "expected_effect", "performance_indicator",
)
ALL_FACT_FIELDS = SHARED_COMPARISON_FIELDS + REQUEST_CONTEXT_FIELDS


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class RequestFactStatus(StrEnum):
    IDENTIFIED = "identified"
    PARTIAL = "partial"
    NOT_FOUND = "not_found"
    MENTIONED_UNRESOLVED = "mentioned_unresolved"
    EXTRACTION_FAILED = "extraction_failed"
    NOT_APPLICABLE = "not_applicable"


class RequestTypeCode(StrEnum):
    DETAIL_PROGRAM_NEW = "detail_program_new"
    SUB_PROGRAM_NEW = "sub_program_new"
    SUB_SUB_PROGRAM_NEW = "sub_sub_program_new"
    PROGRAM_CONTENT_CHANGE = "program_content_change"


REQUEST_TYPE_LABELS = {
    RequestTypeCode.DETAIL_PROGRAM_NEW: "세부사업 신설",
    RequestTypeCode.SUB_PROGRAM_NEW: "내역사업 신설",
    RequestTypeCode.SUB_SUB_PROGRAM_NEW: "내내역사업 신설",
    RequestTypeCode.PROGRAM_CONTENT_CHANGE: "사업내용 변경",
}
CHECKED_GLYPHS = frozenset({"☑", "", "✓", "✔"})
_CELL_ROW = re.compile(r"#r(?P<row>\d+)c\d+p\d+$")
_REQUEST_TYPE_LAYOUT = re.compile(r"[\s:：\-–—|/·]*")
_CELL_COORDINATES = re.compile(r"#r(?P<row>\d+)c(?P<col>\d+)p\d+$")
# A bare cross-reference is not a requested final value.  Keep this deliberately
# narrow: ``동일(50개사)`` and ``변경없음(2027.1.~12.)`` contain independently
# visible values and must remain selectable as such.
_ONLY_UNCHANGED_REFERENCE = re.compile(
    r"^\s*(?:동일|변경\s*없음|기존과\s*동일|"
    r"동일\s*\(\s*변경\s*없음\s*\)|"
    r"변경\s*없음\s*\(\s*동일\s*\))\s*$"
)
_CHANGE_DIFF_MARKERS = ("→", "변경 전", "변경 후", "기존 대비", "시범사업 대비")
_PURPOSE_CHANGE_NARRATIVE = re.compile(
    r"(?:→|변경\s*(?:전|후|여부|내용)|기존\s*대비|시범사업\s*대비|"
    r"기존\s*목적.{0,60}(?:유지|확대)|유지하되)"
)
# A requested purpose can be stated in the same sentence as the intervention
# that will achieve it.  The intervention is not itself the policy outcome.
# Do not trim or rewrite it server-side: reject the larger span and require an
# independently exact outcome span when one is available.
_PURPOSE_INTERVENTION_NARRATIVE = re.compile(
    r"(?:지급\s*구조.{0,120}(?:나누|분할)|멘토링(?:을|를)?\s*.{0,120}구체화)"
)
_PROPOSAL_LABEL_PREFIX = re.compile(
    r"^\s*(?:\d{4}\s*년\s*)?(?:요청안|변경\s*후|변경\s*전|기존\s*사업)\s*(?:\([^)]*\))?\s*[:：]"
)
_COMPONENT_LOCAL_BENEFICIARY_LABEL = re.compile(r"^\s*실제\s*수혜자\s*[:：]")
# These four fields answer a different question from a selection/review rule:
# who may apply, who the programme targets, which positive qualifications
# narrow that set, and who is barred.  A later rank among applicants who may
# still apply has no safe destination in this shared comparison vocabulary.
_SELECTION_REVIEW_PRIORITY_NARRATIVE = re.compile(
    r"(?:신청\s*(?:자체\s*)?(?:는|은)?\s*가능.{0,120}(?:선정|심사).{0,120}(?:우선|후순위)|"
    r"(?:선정|심사).{0,120}(?:우선|후순위)|(?:우선|후순위).{0,120}(?:선정|심사))"
)
_TARGET_SCOPE_FIELDS = frozenset({
    "applicant_eligibility", "support_target", "eligibility_conditions", "exclusions",
})
_WORKFLOW_ONLY_MARKERS = ("→", "선정", "채용", "배치", "출석", "활동 확인")
_SUPPORT_METHOD_MARKERS = ("지원", "지급", "보조", "융자", "보증", "바우처", "교육", "멘토링", "컨설팅", "제공", "정산")
_SERVICE_FORMAT_METHOD = re.compile(r"(?:1\s*:\s*1|그룹|집합|온라인|오프라인|대면|비대면)\s*방식$")
_METHOD_ONLY_SERVICE_TERMS = frozenset({"멘토링"})
_PAYMENT_TRANCHE_LABEL = re.compile(r"^(?:제?\s*\d+\s*단계|\d+\s*차)$")
_STAGE_BOUNDARY_FIELDS = frozenset({
    "applicant_eligibility", "support_target", "eligibility_conditions",
    "beneficiary", "exclusions", "participation_requirements",
})
# The Raw Fact is exactly the date interval. Parenthetical duration, labels,
# and change narration are context, not a program_period value span. An
# explicit official open start (``공고일~2027.12.31``) is permitted: it is a
# named program-boundary event, not an inferred date. Other missing endpoints
# remain ineligible for a program_period Fact.
_DATE_RANGE_SEPARATOR = r"\s*(?:~|∼|–|—|-)\s*"
_DATE_KOREAN_YEAR_MONTH = r"(?:[’'`]\s*)?\d{2,4}\s*년\s*\d{1,2}\s*월"
_DATE_KOREAN_MONTH = r"\d{1,2}\s*월"
_DATE_DOT_YEAR_KOREAN_MONTH = r"(?:[’'`]\s*)?\d{2,4}\s*\.\s*\d{1,2}\s*월"
_DATE_DOT_YMD = r"(?:[’'`]\s*)?\d{2,4}\s*\.\s*\d{1,2}\s*\.\s*\d{1,2}"
_DATE_DOT_YM = r"(?:[’'`]\s*)?\d{2,4}\s*\.\s*\d{1,2}(?:\s*\.)?"
_DATE_DOT_MD = r"\d{1,2}\s*\.\s*\d{1,2}"
_DATE_DOT_MONTH = r"\d{1,2}\s*\."
_PROGRAM_PERIOD_DATE_TOKEN = (
    rf"(?:{_DATE_KOREAN_YEAR_MONTH}|{_DATE_DOT_YEAR_KOREAN_MONTH}|{_DATE_DOT_YMD}|{_DATE_DOT_YM}|"
    rf"{_DATE_KOREAN_MONTH}|{_DATE_DOT_MD}|{_DATE_DOT_MONTH})"
)
_PROGRAM_PERIOD_DATE_RANGE_PATTERN = (
    rf"(?:{_PROGRAM_PERIOD_DATE_TOKEN}{_DATE_RANGE_SEPARATOR}{_PROGRAM_PERIOD_DATE_TOKEN}|"
    rf"공고일{_DATE_RANGE_SEPARATOR}{_PROGRAM_PERIOD_DATE_TOKEN})"
)
_PROGRAM_PERIOD_DATE_RANGE = re.compile(rf"^\s*{_PROGRAM_PERIOD_DATE_RANGE_PATTERN}\s*$")
_PROGRAM_PERIOD_DATE_RANGE_FINDER = re.compile(_PROGRAM_PERIOD_DATE_RANGE_PATTERN)
# A generic month-only range is valid when the source really omits its year.
# It is not valid when the candidate finder has merely started after an
# adjacent dotted year token (``2027. 3월`` -> ``3월``).
_DROPPED_DOTTED_YEAR_PREFIX = re.compile(r"(?:[’'`]\s*)?\d{2,4}\s*\.\s*$")
_MONTH_ONLY_CANDIDATE_PREFIX = re.compile(r"\d{1,2}\s*월")


class ProgramLevel(StrEnum):
    DETAIL_PROGRAM = "detail_program"
    SUB_PROGRAM = "sub_program"
    SUB_SUB_PROGRAM = "sub_sub_program"


class DeliveryActorType(StrEnum):
    CENTRAL_GOVERNMENT = "central_government"
    LOCAL_GOVERNMENT = "local_government"
    PUBLIC_AGENCY = "public_agency"
    FINANCIAL_INSTITUTION = "financial_institution"
    PRIVATE_OPERATOR = "private_operator"
    OTHER = "other"


class DeliveryRole(StrEnum):
    LEAD_AGENCY = "lead_agency"
    OPERATING_AGENCY = "operating_agency"
    DEDICATED_AGENCY = "dedicated_agency"
    PARTICIPATING_PARTNER = "participating_partner"
    DEMAND_PARTNER = "demand_partner"
    COOPERATING_ORGANIZATION = "cooperating_organization"


class DeliveryAction(StrEnum):
    ANNOUNCE = "announce"
    RECRUIT = "recruit"
    RECEIVE = "receive"
    REVIEW = "review"
    EVALUATE = "evaluate"
    SELECT = "select"
    RECOMMEND = "recommend"
    AGREEMENT = "agreement"
    PROVIDE_SUPPORT = "provide_support"
    DISBURSE = "disburse"
    MANAGE = "manage"
    MONITOR = "monitor"
    SETTLE = "settle"
    REPORT = "report"
    FOLLOW_UP = "follow_up"


class DeliveryMethod(StrEnum):
    DIRECT = "direct"
    SUBSIDY = "subsidy"
    CONTRIBUTION = "contribution"
    COMMISSIONED = "commissioned"
    OTHER = "other"


class SourceTextAnchor(StrictModel):
    """Ephemeral model-to-server locator; never persisted in final JSON."""

    source_block_id: str | None = Field(default=None, min_length=1)
    anchor_text: str | None = Field(default=None, min_length=1)
    value_span_candidate_id: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def chooses_one_locator(self) -> "SourceTextAnchor":
        if self.value_span_candidate_id is not None:
            # Structured Output may retain source_block_id as a provenance
            # hint. It is safe only when the server verifies it against the
            # candidate; anchor_text would reintroduce an ambiguous locator.
            if self.anchor_text is not None:
                raise ValueError("value_span_candidate_id must not be combined with anchor_text")
            return self
        if self.source_block_id is None or self.anchor_text is None:
            raise ValueError("legacy value_anchor requires both source_block_id and anchor_text")
        return self


class ValueSpanCandidate(StrictModel):
    """CandidatePack-local deterministic exact span; never a Common IR node."""

    value_span_candidate_id: str = Field(min_length=1)
    source_block_id: str = Field(min_length=1)
    start_char: int = Field(ge=0)
    end_char: int = Field(gt=0)
    value_raw: str = Field(min_length=1)
    candidate_kind: Literal["repeated_exact_span", "program_period_date_range"]


class FactSelection(StrictModel):
    fact_id: str = Field(min_length=1)
    field_name: Literal[
        "purpose_goal", "applicant_eligibility", "support_target",
        "eligibility_conditions", "beneficiary", "exclusions",
        "participation_requirements", "program_period", "support_period",
        "support_activities", "support_methods", "support_items",
        "support_content", "support_scale", "total_budget", "cost_sharing",
        "implementation_plan", "business_need", "legal_basis", "linked_policy",
        "expected_effect", "performance_indicator",
    ]
    value_anchor: SourceTextAnchor
    context_source_block_ids: list[str] = Field(default_factory=list)
    status: Literal["identified", "partial"] = "identified"
    primary_component_id: str | None = None
    program_node_id: str | None = None


class ProgramNodeSelection(StrictModel):
    program_node_id: str = Field(min_length=1)
    level: ProgramLevel
    parent_node_id: str | None = None
    name_anchor: SourceTextAnchor


class SupportComponentSelection(StrictModel):
    support_component_id: str = Field(min_length=1)
    component_kind: ComponentKind
    name_anchor: SourceTextAnchor
    applies_to_anchor: SourceTextAnchor | None = None


class DeliveryActionSelection(StrictModel):
    value_anchor: SourceTextAnchor
    canonical_action: DeliveryAction | None = None


class RelationContainerSelection(StrictModel):
    """Paragraph or explicit Common-IR table-row relationship container."""

    kind: Literal["paragraph", "table_row", "table_column_pair"]
    source_block_id: str | None = None
    anchor_text: str | None = None
    common_ir_block_id: str | None = None
    row_index: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def shape_matches_kind(self) -> "RelationContainerSelection":
        if self.kind == "paragraph":
            if not self.source_block_id or not self.anchor_text:
                raise ValueError("paragraph relation_container requires source_block_id and anchor_text")
            # Some Structured Output clients emit optional properties as null
            # or send the immutable Common IR block as a redundant hint. Keep
            # it ephemeral and validate it server-side; row metadata is never
            # meaningful for a paragraph relation.
            if self.row_index is not None:
                raise ValueError("paragraph relation_container must not carry table-row metadata")
        elif self.kind == "table_row" and (not self.common_ir_block_id or self.row_index is None):
            raise ValueError("table_row relation_container requires common_ir_block_id and row_index")
        elif self.kind == "table_column_pair" and (
            not self.common_ir_block_id or self.row_index is not None or self.source_block_id is not None or self.anchor_text is not None
        ):
            raise ValueError("table_column_pair relation_container requires only common_ir_block_id")
        return self


class DeliveryRelationSelection(StrictModel):
    delivery_relation_id: str = Field(min_length=1)
    actor_anchor: SourceTextAnchor
    role_anchor: SourceTextAnchor | None = None
    actions: list[DeliveryActionSelection] = Field(default_factory=list)
    relation_container: RelationContainerSelection
    canonical_actor_type: DeliveryActorType | None = None
    canonical_role: DeliveryRole | None = None

    @model_validator(mode="after")
    def relation_has_explicit_member(self) -> "DeliveryRelationSelection":
        if self.role_anchor is None and not self.actions:
            raise ValueError("delivery relation requires an explicit role or action")
        if self.relation_container.kind == "table_column_pair" and self.role_anchor is not None and self.actions:
            raise ValueError("table_column_pair must select explicit role or explicit actions, not both")
        return self


class DeliveryMethodSelection(StrictModel):
    fact_id: str = Field(min_length=1)
    method: DeliveryMethod | None = None
    value_anchor: SourceTextAnchor
    context_source_block_ids: list[str] = Field(default_factory=list)
    status: Literal["identified", "partial"] = "identified"


class FieldStateSelection(StrictModel):
    """Explicit source-visible non-value state, never a fake Raw Fact."""

    field_name: str = Field(min_length=1)
    status: Literal["partial", "mentioned_unresolved", "extraction_failed", "not_applicable"]
    fact_ids: list[str] = Field(default_factory=list)
    reason_codes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def registered_field_only(self) -> "FieldStateSelection":
        allowed = set((*ALL_FACT_FIELDS, *REQUEST_DELIVERY_FIELDS, "support_components"))
        if self.field_name not in allowed:
            raise ValueError(f"field_state field is outside Request registry: {self.field_name}")
        if self.status != "partial" and self.fact_ids:
            raise ValueError("only partial field_state may carry selected fact_ids")
        return self


class RequestSourceSelectionV012(StrictModel):
    """The future LLM response contract.  The scaffold consumes JSON only."""

    profile_id: str = Field(min_length=1)
    candidate_pack_id: str = Field(min_length=1)
    program_hierarchy: list[ProgramNodeSelection] = Field(default_factory=list)
    facts: list[FactSelection] = Field(default_factory=list)
    support_components: list[SupportComponentSelection] = Field(default_factory=list)
    delivery_relations: list[DeliveryRelationSelection] = Field(default_factory=list)
    delivery_methods: list[DeliveryMethodSelection] = Field(default_factory=list)
    field_states: list[FieldStateSelection] = Field(default_factory=list)

    @model_validator(mode="after")
    def selection_references_are_well_formed(self) -> "RequestSourceSelectionV012":
        for values, label in (
            ([row.fact_id for row in self.facts], "fact_id"),
            ([row.support_component_id for row in self.support_components], "support_component_id"),
            ([row.delivery_relation_id for row in self.delivery_relations], "delivery_relation_id"),
            ([row.fact_id for row in self.delivery_methods], "delivery_method fact_id"),
            ([row.program_node_id for row in self.program_hierarchy], "program_node_id"),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"duplicate {label}")
        component_ids = {item.support_component_id for item in self.support_components}
        unknown = {item.primary_component_id for item in self.facts if item.primary_component_id} - component_ids
        if unknown:
            raise ValueError(f"facts reference unknown support components: {sorted(unknown)}")
        node_ids = {item.program_node_id for item in self.program_hierarchy}
        unknown_parent = {item.parent_node_id for item in self.program_hierarchy if item.parent_node_id} - node_ids
        if unknown_parent:
            raise ValueError(f"program nodes reference unknown parents: {sorted(unknown_parent)}")
        unknown_nodes = {item.program_node_id for item in self.facts if item.program_node_id} - node_ids
        if unknown_nodes:
            raise ValueError(f"facts reference unknown program nodes: {sorted(unknown_nodes)}")
        method_ids = {row.fact_id for row in self.delivery_methods}
        fact_ids = {row.fact_id for row in self.facts}
        if fact_ids & method_ids:
            raise ValueError(f"duplicate fact_id across facts and delivery methods: {sorted(fact_ids & method_ids)}")
        state_fields = [row.field_name for row in self.field_states]
        if len(state_fields) != len(set(state_fields)):
            raise ValueError("duplicate field_state field_name")
        # ``field_states.fact_ids`` is an optional LLM hint only.  The final
        # profile derives IDs and resolves any value/state conflict from facts
        # actually materialized by the server, so dangling or cross-field
        # hints never become persisted references.
        return self


def build_request_candidate_pack(document: dict[str, Any]) -> CandidatePack:
    """Project every non-wrapper Common IR block/cell into the Request pack.

    Markdown fixtures do not have notice attachments to scope; future Request
    adapters may narrow this pack before selection, but exact-span provenance
    remains identical.
    """

    projection = project_common_ir_v1(document)
    blocks: list[SourceBlock] = []
    seen: set[str] = set()
    # Whole-table text flattens rows/cells and must never become an exact
    # value candidate.  Individual cell paragraphs retain table provenance.
    for block in [
        *[item for item in projection.blocks if item.block_kind != "table"],
        *projection.table_cell_blocks,
    ]:
        if block.block_id not in seen:
            seen.add(block.block_id)
            blocks.append(block.model_copy(update={"relation": SourceRelation.CANDIDATE}))
    return CandidatePack(
        pack_id=f"{projection.common_ir_document_id}-request-v0.1.2",
        notice_id=projection.notice_id,
        extraction_scope="document",
        question="Request Profile v0.1.2 source selection; values must be exact CandidatePack spans.",
        blocks=blocks,
        generator=COMMON_IR_V1_CANDIDATE_PACK_GENERATOR,
        generator_version=COMMON_IR_V1_CANDIDATE_PACK_GENERATOR_VERSION,
        common_ir_document_id=projection.common_ir_document_id,
    )


_SPAN_TOKEN = re.compile(r"\S+")
_LEXICAL_SPAN_TOKEN = re.compile(r"[가-힣A-Za-z0-9][가-힣A-Za-z0-9·&/+\-]*")
_MAX_SPAN_CANDIDATE_TOKENS = 8
_VALUE_SPAN_CANDIDATE_CACHE: dict[str, tuple[ValueSpanCandidate, ...]] = {}


def build_value_span_candidates(pack: CandidatePack) -> list[ValueSpanCandidate]:
    """Generate compact deterministic IDs for restricted exact-span cases.

    Repeated text needs candidate IDs because a model must never select a
    numbered occurrence. ``program_period`` is stricter still: every eligible
    closed or explicit ``공고일``-open date range is made a candidate even when
    unique, so the model cannot include its label, duration parenthesis, or
    change explanation in the final Raw Fact. The ID hash includes the pack
    generator/version and pack ID; it is not a Common IR identifier.
    """

    cache_basis = "\x1e".join((pack.generator or "", pack.generator_version or "", pack.pack_id, *(
        f"{block.block_id}\x1f{block.text}" for block in pack.blocks
    )))
    cache_key = sha256(cache_basis.encode("utf-8")).hexdigest()
    cached = _VALUE_SPAN_CANDIDATE_CACHE.get(cache_key)
    if cached is not None:
        return list(cached)
    candidates: list[ValueSpanCandidate] = []
    seen_spans: set[tuple[str, int, int, str]] = set()

    def add_candidate(
        block: SourceBlock, start: int, end: int, candidate_kind: Literal["repeated_exact_span", "program_period_date_range"],
    ) -> None:
        raw = block.text[start:end]
        if (
            candidate_kind == "program_period_date_range"
            and _MONTH_ONLY_CANDIDATE_PREFIX.match(raw)
            and _DROPPED_DOTTED_YEAR_PREFIX.search(block.text[:start])
        ):
            # Keep genuinely year-less ranges available, but never expose a
            # candidate that silently discards an immediately preceding year.
            return
        key = (block.block_id, start, end, candidate_kind)
        if key in seen_spans:
            return
        seen_spans.add(key)
        identity = "\x1f".join((
            pack.generator or "", pack.generator_version or "", pack.pack_id,
            candidate_kind, block.block_id, str(start), str(end), raw,
        ))
        candidates.append(ValueSpanCandidate(
            value_span_candidate_id=f"vsc:{sha256(identity.encode('utf-8')).hexdigest()[:20]}",
            source_block_id=block.block_id, start_char=start, end_char=end, value_raw=raw,
            candidate_kind=candidate_kind,
        ))

    for block in pack.blocks:
        for match in _PROGRAM_PERIOD_DATE_RANGE_FINDER.finditer(block.text):
            add_candidate(block, match.start(), match.end(), "program_period_date_range")
        tokens = list(_SPAN_TOKEN.finditer(block.text))
        by_text: dict[str, list[tuple[int, int]]] = defaultdict(list)
        for start_index, token in enumerate(tokens):
            for end_index in range(start_index, min(len(tokens), start_index + _MAX_SPAN_CANDIDATE_TOKENS)):
                start, end = token.start(), tokens[end_index].end()
                raw = block.text[start:end]
                by_text[raw].append((start, end))
        for raw, spans in by_text.items():
            if len(spans) < 2:
                continue
            for start, end in spans:
                add_candidate(block, start, end, "repeated_exact_span")
        # Whitespace-token n-grams deliberately retain adjacent punctuation,
        # which is right for sentence spans but misses a repeated inline label
        # such as ``신규채용`` in ``신규채용: ... (신규채용 트랙)``. Add only
        # repeated lexical spans, never every arbitrary substring, so every
        # anchor-bearing selection type can safely choose that exact label.
        lexical_by_text: dict[str, list[tuple[int, int]]] = defaultdict(list)
        for token in _LEXICAL_SPAN_TOKEN.finditer(block.text):
            lexical_by_text[token.group()].append((token.start(), token.end()))
        for spans in lexical_by_text.values():
            if len(spans) >= 2:
                for start, end in spans:
                    add_candidate(block, start, end, "repeated_exact_span")
    _VALUE_SPAN_CANDIDATE_CACHE[cache_key] = tuple(candidates)
    return candidates


def _value_span_candidate_map(pack: CandidatePack) -> dict[str, ValueSpanCandidate]:
    candidates = build_value_span_candidates(pack)
    result = {row.value_span_candidate_id: row for row in candidates}
    if len(result) != len(candidates):
        raise ValueError("duplicate deterministic value_span_candidate_id")
    return result


def candidate_pack_artifact(pack: CandidatePack, document: dict[str, Any]) -> dict[str, Any]:
    identity = common_ir_v1_identity(document)
    return {
        "candidate_pack_id": pack.pack_id,
        "candidate_pack_generator": pack.generator,
        "candidate_pack_generator_version": pack.generator_version,
        "common_ir_document_id": pack.common_ir_document_id,
        "common_ir_source_sha256": identity["source_sha256"],
        "text_basis": TEXT_BASIS,
        "value_span_candidate_generator": "request_candidate_span_v3",
        "value_span_candidate_generator_version": "3",
        "value_span_candidates": [row.model_dump(mode="json") for row in build_value_span_candidates(pack)],
        "blocks": [
            {
                "source_block_id": block.block_id,
                "text": block.text,
                "common_ir_block_id": block.common_ir_block_id,
                **({"common_ir_cell_id": block.common_ir_cell_id} if block.common_ir_cell_id else {}),
                "common_ir_occurrence_ids": list(block.common_ir_occurrence_ids),
            }
            for block in pack.blocks
        ],
    }


def _source_document(document: dict[str, Any]) -> dict[str, Any]:
    info = document["document"]
    provenance = info["provenance"]
    lineage: dict[str, Any] = {
        "document_id": info["document_id"],
        "schema_version": document["schema_version"],
        "source_kind": info["source_kind"],
        "source_sha256": provenance["source_sha256"],
        "source_location": provenance["source_location"],
    }
    if info.get("artifact_role") is not None:
        lineage["artifact_role"] = info["artifact_role"]
    validate_common_ir_lineage(lineage)
    return {"format": info["source_kind"], "common_ir": lineage}


def _resolve_anchor(
    anchor: SourceTextAnchor, blocks: dict[str, SourceBlock], pack: CandidatePack,
    *, required_candidate_kind: Literal["program_period_date_range"] | None = None,
) -> tuple[str, ValueSource]:
    candidates = _value_span_candidate_map(pack)
    if anchor.value_span_candidate_id is not None:
        candidate = candidates.get(anchor.value_span_candidate_id)
        if candidate is None:
            raise ValueError(f"unknown value_span_candidate_id: {anchor.value_span_candidate_id}")
        if required_candidate_kind is not None and candidate.candidate_kind != required_candidate_kind:
            raise ValueError(f"program_period requires a {required_candidate_kind} value_span_candidate_id")
        if anchor.source_block_id is not None and anchor.source_block_id != candidate.source_block_id:
            raise ValueError("value_span_candidate_id source_block_id hint does not match CandidatePack candidate")
        block = blocks.get(candidate.source_block_id)
        if block is None or block.text[candidate.start_char:candidate.end_char] != candidate.value_raw:
            raise ValueError("value_span_candidate_id does not resolve to its CandidatePack exact span")
        return candidate.value_raw, ValueSource(
            source_block_id=candidate.source_block_id,
            start_char=candidate.start_char,
            end_char=candidate.end_char,
        )
    if required_candidate_kind is not None:
        raise ValueError(f"program_period requires a {required_candidate_kind} value_span_candidate_id")
    assert anchor.source_block_id is not None and anchor.anchor_text is not None
    block = blocks.get(anchor.source_block_id)
    if block is None:
        raise ValueError(f"anchor references CandidatePack block outside pack: {anchor.source_block_id}")
    # Legacy anchors remain safe only when their exact text is unique.  A
    # repeated literal needs one server-generated CandidatePack-local ID.
    occurrences = find_all_occurrences(block.text, anchor.anchor_text)
    if len(occurrences) == 1:
        return materialize_value_source(block.block_id, block.text, anchor.anchor_text)
    if len(occurrences) > 1:
        raise ValueError(
            "ambiguous legacy anchor_text requires value_span_candidate_id; do not choose an occurrence index"
        )
    raise ValueError("value anchor must occur exactly once in its source block")


def _evidence(pack: CandidatePack, block: SourceBlock) -> dict[str, Any]:
    if block.common_ir_block_id is None or pack.common_ir_document_id is None:
        raise ValueError("Request assembly requires Common IR provenance on every CandidatePack block")
    evidence: dict[str, Any] = {
        "source_block_id": block.block_id,
        "common_ir_document_id": pack.common_ir_document_id,
        "common_ir_block_id": block.common_ir_block_id,
        "common_ir_occurrence_ids": list(block.common_ir_occurrence_ids),
    }
    if block.common_ir_cell_id:
        evidence["common_ir_cell_id"] = block.common_ir_cell_id
    return evidence


def _materialize_anchor(
    anchor: SourceTextAnchor, blocks: dict[str, SourceBlock], pack: CandidatePack,
    *, required_candidate_kind: Literal["program_period_date_range"] | None = None,
) -> dict[str, Any]:
    raw, source = _resolve_anchor(anchor, blocks, pack, required_candidate_kind=required_candidate_kind)
    return {
        "value_raw": raw,
        "value_source": source.model_dump(mode="json"),
        "evidence": [_evidence(pack, blocks[source.source_block_id])],
    }


def _anchor_block_id(anchor: SourceTextAnchor, pack: CandidatePack) -> str:
    if anchor.value_span_candidate_id is None:
        assert anchor.source_block_id is not None
        return anchor.source_block_id
    candidate = _value_span_candidate_map(pack).get(anchor.value_span_candidate_id)
    if candidate is None:
        raise ValueError(f"unknown value_span_candidate_id: {anchor.value_span_candidate_id}")
    return candidate.source_block_id


def _validate_raw_fact_semantic_policy(field_name: str, value_raw: str) -> None:
    """Keep change-diff narration out of Raw Fact occurrences.

    Request Raw Facts represent values applied to the requested programme, not
    before/after comparison prose. An independently explicit final value can
    still carry an adjacent ``(변경 없음)`` note; a bare "동일" reference cannot.
    """

    if _ONLY_UNCHANGED_REFERENCE.fullmatch(value_raw):
        raise ValueError(
            f"{field_name} is only an unchanged-by-reference marker; emit field_state mentioned_unresolved instead"
        )
    if _PROPOSAL_LABEL_PREFIX.match(value_raw):
        raise ValueError(
            f"{field_name} must exclude a proposal/year/change label prefix and select only the requested content span"
        )
    if field_name in _TARGET_SCOPE_FIELDS and _SELECTION_REVIEW_PRIORITY_NARRATIVE.search(value_raw):
        raise ValueError(
            f"{field_name} must describe application eligibility/target scope or support exclusion, not a selection/review priority"
        )
    if field_name == "purpose_goal" and _PURPOSE_CHANGE_NARRATIVE.search(value_raw):
        raise ValueError(
            "purpose_goal must select the requested policy-purpose span, not a before-after or maintain/expand narration"
        )
    if field_name == "purpose_goal" and _PURPOSE_INTERVENTION_NARRATIVE.search(value_raw):
        raise ValueError(
            "purpose_goal must exclude intervention/change-mechanism prose and select only a separable exact policy-outcome span"
        )
    if field_name in {
        "purpose_goal", "support_content", "delivery_methods",
        "support_component", "support_component_applies_to",
    } and any(marker in value_raw for marker in _CHANGE_DIFF_MARKERS):
        raise ValueError(f"{field_name} must not store before-after comparison narration as a Raw Fact")
    if field_name == "support_scale" and "→" in value_raw:
        raise ValueError("support_scale must select one requested final value, not a before-after comparison span")
    if field_name == "support_items" and value_raw in _METHOD_ONLY_SERVICE_TERMS:
        raise ValueError(
            "support_items must not use a named support service as an item; select it as support_methods instead"
        )
    if field_name == "support_methods":
        if any(marker in value_raw for marker in _WORKFLOW_ONLY_MARKERS):
            raise ValueError(
                "support_methods must select an independently exact providing/payment method, not a selection-to-payment workflow span"
            )
        if not any(marker in value_raw for marker in _SUPPORT_METHOD_MARKERS) and not _SERVICE_FORMAT_METHOD.search(value_raw):
            raise ValueError("support_methods must name a support providing or payment method")
    if field_name == "program_period":
        if not _PROGRAM_PERIOD_DATE_RANGE.fullmatch(value_raw):
            raise ValueError(
                "program_period must be one exact date-range span; exclude duration parentheses, labels, and change narration"
            )


def _validate_stage_support_components(
    components: list[dict[str, Any]], comparison: dict[str, list[dict[str, Any]]],
) -> None:
    """Reject a payment tranche being mistaken for an independently supported stage.

    A stage can be a component only when the source selection also records a
    distinct recipient/eligibility/participation boundary for that stage. A
    different amount or payment condition alone is a property of its parent
    support package, not a new support component.
    """

    facts = [fact for field, rows in comparison.items() if field not in REQUEST_DELIVERY_FIELDS for fact in rows]
    for component in components:
        is_stage = component["component_kind"] == ComponentKind.STAGE_SUPPORT.value
        is_payment_tranche_label = bool(_PAYMENT_TRANCHE_LABEL.fullmatch(component["name_raw"]))
        if not (is_stage or is_payment_tranche_label):
            continue
        component_id = component["support_component_id"]
        has_distinct_boundary = any(
            fact.get("primary_component_id") == component_id
            and fact["field_name"] in _STAGE_BOUNDARY_FIELDS
            for fact in facts
        )
        if not has_distinct_boundary:
            raise ValueError(
                "stage_support requires an explicitly selected, component-scoped recipient/eligibility/participation boundary; "
                "a payment tranche or amount alone is not an independent support component"
            )


def _common_ir_table_cell_map(document: dict[str, Any], table_id: str) -> tuple[dict[str, dict[str, Any]], list[int]]:
    """Return explicit table-cell geometry and non-empty semantic rows."""

    table = next((block for block in document.get("blocks", []) if block.get("block_id") == table_id), None)
    if table is None or table.get("kind") != "table" or table.get("structure_status") != "explicit":
        raise ValueError("table relation container requires an explicit Common IR table")
    occurrence_text = {
        occurrence["occurrence_id"]: occurrence.get("text", "")
        for block in document.get("blocks", []) for occurrence in block.get("occurrences", [])
    }
    cells = {cell["cell_id"]: cell for cell in table.get("cells", [])}
    semantic_rows = sorted({
        cell.get("row_index", 0)
        for cell in cells.values()
        if any(occurrence_text.get(item, "").strip() for item in cell.get("text_occurrence_ids", []))
    })
    return cells, semantic_rows


def _validate_request_profile(profile: dict[str, Any], pack: CandidatePack) -> list[str]:
    """Small deterministic validator for the v0.1.2 invariants in scope now."""

    issues: list[str] = []
    block_texts = {block.block_id: block.text for block in pack.blocks}
    facts: dict[str, dict[str, Any]] = {}
    spans: dict[tuple[str, int, int], list[str]] = defaultdict(list)

    def validate_semantic_span(
        row: dict[str, Any], label: str, *, component_name_id: str | None = None,
    ) -> None:
        """Validate a persisted semantic span, including cross-category reuse."""

        source = row.get("value_source", {})
        text = block_texts.get(source.get("source_block_id"))
        if text is None:
            issues.append(f"{label}: unknown CandidatePack source block")
            return
        start, end = source.get("start_char"), source.get("end_char")
        if not isinstance(start, int) or not isinstance(end, int) or text[start:end] != row.get("value_raw"):
            issues.append(f"{label}: value_raw does not equal exact CandidatePack span")
        key = (source.get("source_block_id"), start, end)
        existing_labels = spans[key]
        # A component label is structural, while a Raw Fact is comparison
        # data. The same literal span may play both roles only if that Fact is
        # explicitly scoped to this very component. Raw Fact↔Raw Fact and all
        # other cross-category reuse remain invalid.
        component_name_reuse = (
            component_name_id is not None
            and len(existing_labels) == 1
            and existing_labels[0] in facts
            and facts[existing_labels[0]].get("primary_component_id") == component_name_id
        )
        if existing_labels and not component_name_reuse:
            issues.append(f"{label}: duplicate exact source span")
        spans[key].append(label)

    for field_name, rows in profile["comparison_profile"].items():
        if field_name in REQUEST_DELIVERY_FIELDS:
            continue
        for fact in rows:
            facts[fact["fact_id"]] = fact
            validate_semantic_span(fact, fact["fact_id"])
            if fact["field_name"] == "program_period" and fact.get("primary_component_id"):
                issues.append(f"{fact['fact_id']}: program_period cannot be component scoped")
    # Request context is made of the same Raw Fact contract as comparison
    # fields; it must therefore participate in provenance, duplicate-span,
    # component and field_state reference validation.
    for rows in profile["request_context"].values():
        for fact in rows:
            facts[fact["fact_id"]] = fact
            validate_semantic_span(fact, fact["fact_id"])
    for fact in profile["comparison_profile"]["delivery_methods"]:
        facts[fact["fact_id"]] = fact
        validate_semantic_span(fact, fact["fact_id"])
    for component in profile["support_components"]:
        validate_semantic_span({
            "value_raw": component["name_raw"], "value_source": component["value_source"],
        }, f"{component['support_component_id']}:name", component_name_id=component["support_component_id"])
        if component.get("applies_to_raw") is not None:
            validate_semantic_span({
                "value_raw": component["applies_to_raw"], "value_source": component["applies_to_source"],
            }, f"{component['support_component_id']}:applies_to")
    for relation in profile["comparison_profile"]["delivery_relations"]:
        validate_semantic_span(relation["actor"], f"{relation['delivery_relation_id']}:actor")
        if relation.get("role") is not None:
            validate_semantic_span(relation["role"], f"{relation['delivery_relation_id']}:role")
        for index, action in enumerate(relation.get("actions", [])):
            validate_semantic_span(action, f"{relation['delivery_relation_id']}:action[{index}]")
    component_ids = {row["support_component_id"] for row in profile["support_components"]}
    for fact in facts.values():
        if fact.get("primary_component_id") and fact["primary_component_id"] not in component_ids:
            issues.append(f"{fact['fact_id']}: unknown primary_component_id")
    relation_ids = {row["delivery_relation_id"] for row in profile["comparison_profile"]["delivery_relations"]}
    for state in profile["field_states"]:
        for fact_id in state.get("fact_ids", []):
            if fact_id not in facts:
                issues.append(f"field_state references unknown fact {fact_id}")
        for relation_id in state.get("relation_ids", []):
            if relation_id not in relation_ids:
                issues.append(f"field_state references unknown relation {relation_id}")
        for component_id in state.get("component_ids", []):
            if component_id not in component_ids:
                issues.append(f"field_state references unknown component {component_id}")
    return issues


def _final_state_reason_codes(
    explicit: FieldStateSelection | None, *, has_server_values: bool,
) -> list[str]:
    """Preserve selection diagnostics without persisting contradictory state."""

    if explicit is None:
        return []
    codes = list(explicit.reason_codes)
    if has_server_values and explicit.status != "partial":
        codes = ["selection_state_conflict", f"selection_status:{explicit.status}", *codes]
    return list(dict.fromkeys(codes))


def _validate_component_local_beneficiary_links(
    beneficiary_facts: list[dict[str, Any]], components: list[dict[str, Any]], pack: CandidatePack,
) -> None:
    """Validate explicit component-local beneficiary rows in both directions.

    This is a validation of model-selected facts, not a producer: the server
    never invents a beneficiary or component. A selected local beneficiary row
    must cite its nearest selected heading. Conversely, each selected explicit
    component heading that actually has a local ``실제 수혜자:`` value row must
    have exactly one selected beneficiary Fact linked to that component. Bare
    references elsewhere in the document are outside this contract.
    """

    order = {block.block_id: index for index, block in enumerate(pack.blocks)}
    by_id = {block.block_id: block for block in pack.blocks}
    component_heading_ids: dict[str, str] = {}
    for component in components:
        heading_block_id = component["value_source"]["source_block_id"]
        heading_block = by_id.get(heading_block_id)
        # Only explicit semantic headings create a local component scope.
        # A component named from a paragraph/table may still be valid, but it
        # must not absorb unrelated beneficiary references by proximity.
        if heading_block is not None and heading_block.block_kind == "heading":
            if heading_block_id in component_heading_ids:
                raise ValueError("multiple support_components cannot use one explicit component heading")
            component_heading_ids[heading_block_id] = component["support_component_id"]

    def nearest_heading(block_id: str) -> SourceBlock | None:
        for prior in reversed(pack.blocks[:order[block_id]]):
            if prior.block_kind == "heading":
                return prior
        return None

    def local_component_id(block_id: str) -> str | None:
        heading = nearest_heading(block_id)
        return component_heading_ids.get(heading.block_id) if heading else None

    # First retain the original direction: an explicitly selected local row
    # cannot float free or point at a different component.
    for fact in beneficiary_facts:
        source_block_id = fact["value_source"]["source_block_id"]
        source_block = by_id[source_block_id]
        if not _COMPONENT_LOCAL_BENEFICIARY_LABEL.match(source_block.text):
            continue
        heading = nearest_heading(source_block_id)
        if heading is None:
            continue
        component_id = local_component_id(source_block_id)
        if component_id is None:
            raise ValueError(
                "component-local beneficiary requires the nearest explicit heading to be selected as a support_component"
            )
        if fact.get("primary_component_id") != component_id:
            raise ValueError(
                "component-local beneficiary must reference its nearest explicit support_component via primary_component_id"
            )

    # Then enforce recall for the explicit selected-component case. A row with
    # no value after the label is not an extractable beneficiary value and is
    # intentionally ignored rather than synthesized by the server.
    for source_block in pack.blocks:
        if not _COMPONENT_LOCAL_BENEFICIARY_LABEL.match(source_block.text):
            continue
        if not _COMPONENT_LOCAL_BENEFICIARY_LABEL.sub("", source_block.text, count=1).strip():
            continue
        component_id = local_component_id(source_block.block_id)
        if component_id is None:
            continue
        linked = [
            fact for fact in beneficiary_facts
            if fact["value_source"]["source_block_id"] == source_block.block_id
            and fact.get("primary_component_id") == component_id
        ]
        if not linked:
            raise ValueError(
                "explicit component-local beneficiary row beneath selected support_component requires one beneficiary Fact"
            )
        if len(linked) != 1:
            raise ValueError(
                "explicit component-local beneficiary row beneath selected support_component requires exactly one beneficiary Fact"
            )


def resolve_request_type_from_candidate_pack(pack: CandidatePack) -> dict[str, Any]:
    """Server-resolve the form checkbox; this is intentionally outside LLM output.

    The resolver considers only CandidatePack blocks that actually contain a
    canonical request-type option.  It then requires exactly one checked glyph
    in that option container and derives the label immediately after the glyph.
    No occurrence index, model-selected anchor, or narrative inference exists.
    """

    option_blocks = [
        block for block in pack.blocks
        if any(label in block.text for label in REQUEST_TYPE_LABELS.values())
        and any(glyph in block.text for glyph in CHECKED_GLYPHS)
    ]
    if len(option_blocks) != 1:
        raise ValueError("request_type option container is missing or ambiguous in CandidatePack")
    block = option_blocks[0]
    checked_positions = [
        index for index, char in enumerate(block.text) if char in CHECKED_GLYPHS
    ]
    if len(checked_positions) != 1:
        raise ValueError("request_type must have exactly one checked option; request_type is unresolved")
    glyph_start = checked_positions[0]
    glyph_end = glyph_start + 1
    layout = _REQUEST_TYPE_LAYOUT.match(block.text, glyph_end)
    assert layout is not None
    label_start = layout.end()
    matching = [
        (code, label) for code, label in REQUEST_TYPE_LABELS.items()
        if block.text.startswith(label, label_start)
    ]
    if len(matching) != 1:
        raise ValueError("request_type checked glyph is not immediately associated with one canonical option label")
    code, label = matching[0]
    label_end = label_start + len(label)
    label_source = ValueSource(
        source_block_id=block.block_id, start_char=label_start, end_char=label_end,
    ).model_dump(mode="json")
    glyph_source = ValueSource(
        source_block_id=block.block_id, start_char=glyph_start, end_char=glyph_end,
    ).model_dump(mode="json")
    evidence = _evidence(pack, block)
    return {
        "selected_code": code.value,
        "value_raw": block.text[label_start:label_end],
        "value_source": label_source,
        "selection_source": {"glyph_raw": block.text[glyph_start:glyph_end], **glyph_source},
        "evidence": [evidence],
    }


def assemble_request_profile_v012(
    document: dict[str, Any], pack: CandidatePack, selection: RequestSourceSelectionV012,
    *, model_id: str = "not_called", prompt_version: str = "request_source_selection_v0.1.2",
) -> dict[str, Any]:
    """Materialize a validated Request profile without an LLM/API call."""

    identity = common_ir_v1_identity(document)
    if pack.common_ir_document_id != identity["document_id"]:
        raise ValueError("CandidatePack and Common IR document identity differ")
    if selection.candidate_pack_id != pack.pack_id:
        raise ValueError("selection candidate_pack_id does not match CandidatePack")
    blocks = {block.block_id: block for block in pack.blocks}
    comparison: dict[str, list[dict[str, Any]]] = {
        **{field: [] for field in SHARED_COMPARISON_FIELDS},
        "delivery_relations": [], "delivery_methods": [],
    }
    request_context: dict[str, list[dict[str, Any]]] = {field: [] for field in REQUEST_CONTEXT_FIELDS}
    for selected in selection.facts:
        try:
            materialized = _materialize_anchor(
                selected.value_anchor, blocks, pack,
                required_candidate_kind="program_period_date_range" if selected.field_name == "program_period" else None,
            )
        except ValueError as error:
            raise ValueError(
                f"fact {selected.fact_id} ({selected.field_name}): {error}"
            ) from error
        contexts = []
        for block_id in selected.context_source_block_ids:
            if block_id not in blocks:
                raise ValueError(f"fact {selected.fact_id} references context outside CandidatePack: {block_id}")
            contexts.append(block_id)
        row = {
            "fact_id": selected.fact_id,
            "field_name": selected.field_name,
            **materialized,
            "context_source_block_ids": contexts,
            "status": selected.status,
            **({"primary_component_id": selected.primary_component_id} if selected.primary_component_id else {}),
            **({"program_node_id": selected.program_node_id} if selected.program_node_id else {}),
        }
        _validate_raw_fact_semantic_policy(selected.field_name, row["value_raw"])
        if selected.field_name in comparison:
            comparison[selected.field_name].append(row)
        else:
            request_context[selected.field_name].append(row)

    components: list[dict[str, Any]] = []
    for selected in selection.support_components:
        name = _materialize_anchor(selected.name_anchor, blocks, pack)
        _validate_raw_fact_semantic_policy("support_component", name["value_raw"])
        row = {
            "support_component_id": selected.support_component_id,
            "component_kind": selected.component_kind.value,
            "name_raw": name["value_raw"],
            "value_source": name["value_source"],
            "evidence": name["evidence"],
        }
        if selected.applies_to_anchor:
            applies = _materialize_anchor(selected.applies_to_anchor, blocks, pack)
            _validate_raw_fact_semantic_policy("support_component_applies_to", applies["value_raw"])
            row["applies_to_raw"] = applies["value_raw"]
            row["applies_to_source"] = applies["value_source"]
        components.append(row)

    _validate_stage_support_components(components, comparison)
    _validate_component_local_beneficiary_links(comparison["beneficiary"], components, pack)

    request_type = resolve_request_type_from_candidate_pack(pack)

    hierarchy: list[dict[str, Any]] = []
    for selected in selection.program_hierarchy:
        node = _materialize_anchor(selected.name_anchor, blocks, pack)
        hierarchy.append({
            "program_node_id": selected.program_node_id,
            "level": selected.level.value,
            "parent_node_id": selected.parent_node_id,
            "name_raw": node["value_raw"], "value_source": node["value_source"], "evidence": node["evidence"],
        })

    for selected in selection.delivery_relations:
        actor = _materialize_anchor(selected.actor_anchor, blocks, pack)
        role = _materialize_anchor(selected.role_anchor, blocks, pack) if selected.role_anchor else None
        actions = []
        for action in selected.actions:
            value = _materialize_anchor(action.value_anchor, blocks, pack)
            actions.append({
                "value_raw": value["value_raw"], "value_source": value["value_source"],
                "evidence": value["evidence"],
                "canonical_action": action.canonical_action.value if action.canonical_action else None,
            })
        relation_blocks = [blocks[_anchor_block_id(selected.actor_anchor, pack)]]
        if selected.role_anchor:
            relation_blocks.append(blocks[_anchor_block_id(selected.role_anchor, pack)])
        relation_blocks.extend(blocks[_anchor_block_id(item.value_anchor, pack)] for item in selected.actions)
        if selected.relation_container.kind == "paragraph":
            assert selected.relation_container.source_block_id is not None
            assert selected.relation_container.anchor_text is not None
            if any(block.block_id != selected.relation_container.source_block_id for block in relation_blocks):
                raise ValueError("paragraph delivery relation members must share its relation container block")
            if (
                selected.relation_container.common_ir_block_id is not None
                and selected.relation_container.common_ir_block_id
                != blocks[selected.relation_container.source_block_id].common_ir_block_id
            ):
                raise ValueError("paragraph relation_container common_ir_block_id does not match source block provenance")
            container = _materialize_anchor(
                SourceTextAnchor(
                    source_block_id=selected.relation_container.source_block_id,
                    anchor_text=selected.relation_container.anchor_text,
                ), blocks, pack,
            )
            relation_container = {
                "container_type": "paragraph",
                **container["evidence"][0],
            }
        elif selected.relation_container.kind == "table_row":
            table_id = selected.relation_container.common_ir_block_id
            row_index = selected.relation_container.row_index
            assert table_id is not None and row_index is not None
            if not relation_blocks or any(
                block.common_ir_block_id != table_id or block.common_ir_cell_id is None
                or (match := _CELL_ROW.search(block.block_id)) is None
                or int(match.group("row")) != row_index
                for block in relation_blocks
            ):
                raise ValueError("table_row delivery relation members must share explicit Common IR table row")
            relation_container = {
                "container_type": "table_row",
                "common_ir_document_id": pack.common_ir_document_id,
                "common_ir_block_id": table_id,
                "row_index": row_index,
            }
        else:
            # An HWP organisation chart can be represented as nested explicit
            # table cells: an actor and its explicit role *or action* appear
            # in one column on successive semantic rows.  This is structural
            # evidence, not heading adjacency and never a document-wide join.
            # In particular, `R&D 과제수행` is an action, not an organisation
            # role merely because it sits under `수행기관`.
            if role is None and not actions:
                raise ValueError("table_column_pair delivery relation requires an explicit role or action")
            if role is not None and actions:
                raise ValueError("table_column_pair must not mix role and action members")
            table_id = selected.relation_container.common_ir_block_id
            assert table_id is not None
            actor_block = blocks[_anchor_block_id(selected.actor_anchor, pack)]
            cells, semantic_rows = _common_ir_table_cell_map(document, table_id)
            actor_cell = cells.get(actor_block.common_ir_cell_id)
            related_blocks = ([blocks[_anchor_block_id(selected.role_anchor, pack)]] if selected.role_anchor else []) + [
                blocks[_anchor_block_id(item.value_anchor, pack)] for item in selected.actions
            ]
            if (
                actor_block.common_ir_block_id != table_id or actor_block.common_ir_cell_id is None
                or actor_cell is None
                or any(item.common_ir_block_id != table_id or item.common_ir_cell_id is None for item in related_blocks)
            ):
                raise ValueError("table_column_pair actor and role/action must be explicit cells in the declared Common IR table")
            related_cells = [cells.get(item.common_ir_cell_id) for item in related_blocks]
            if any(cell is None for cell in related_cells):
                raise ValueError("table_column_pair references Common IR cells outside declared table")
            actor_col = (actor_cell.get("col_index", 0), actor_cell.get("col_span", 1))
            actor_row = actor_cell.get("row_index", 0)
            next_rows = [row for row in semantic_rows if row > actor_row]
            for related_cell in related_cells:
                assert related_cell is not None
                related_col = (related_cell.get("col_index", 0), related_cell.get("col_span", 1))
                related_row = related_cell.get("row_index", 0)
                if actor_col != related_col:
                    raise ValueError("table_column_pair actor and role/action must have identical overlapping column span")
                if not next_rows or next_rows[0] != related_row:
                    raise ValueError("table_column_pair role/action must be the immediate next non-empty semantic row")
            relation_container = {
                "container_type": "table_column_pair",
                "common_ir_document_id": pack.common_ir_document_id,
                "common_ir_block_id": table_id,
                "actor_common_ir_cell_id": actor_block.common_ir_cell_id,
                **({"role_common_ir_cell_id": related_blocks[0].common_ir_cell_id} if role is not None else {}),
                **({"action_common_ir_cell_ids": [item.common_ir_cell_id for item in related_blocks]} if actions else {}),
            }
        comparison["delivery_relations"].append({
            "delivery_relation_id": selected.delivery_relation_id,
            "actor": {
                "value_raw": actor["value_raw"], "value_source": actor["value_source"], "evidence": actor["evidence"],
                "canonical_actor_type": selected.canonical_actor_type.value if selected.canonical_actor_type else None,
            },
            "role": None if role is None else {
                "value_raw": role["value_raw"], "value_source": role["value_source"], "evidence": role["evidence"],
                "canonical_role": selected.canonical_role.value if selected.canonical_role else None,
            },
            "actions": actions,
            "relation_container": relation_container,
        })
    for selected in selection.delivery_methods:
        value = _materialize_anchor(selected.value_anchor, blocks, pack)
        _validate_raw_fact_semantic_policy("delivery_methods", value["value_raw"])
        for block_id in selected.context_source_block_ids:
            if block_id not in blocks:
                raise ValueError(f"delivery method {selected.fact_id} context outside CandidatePack: {block_id}")
        comparison["delivery_methods"].append({
            "fact_id": selected.fact_id,
            "field_name": "delivery_methods",
            "value_raw": value["value_raw"], "value_source": value["value_source"], "evidence": value["evidence"],
            "context_source_block_ids": selected.context_source_block_ids, "status": selected.status,
            "canonical_method": selected.method.value if selected.method else None,
        })

    facts_by_field: dict[str, list[str]] = defaultdict(list)
    for field, rows in comparison.items():
        if field not in REQUEST_DELIVERY_FIELDS:
            facts_by_field[field].extend(row["fact_id"] for row in rows)
    for field, rows in request_context.items():
        facts_by_field[field].extend(row["fact_id"] for row in rows)
    states: list[dict[str, Any]] = []
    explicit_states = {row.field_name: row for row in selection.field_states}
    for field in (*SHARED_COMPARISON_FIELDS, *REQUEST_CONTEXT_FIELDS):
        ids = facts_by_field[field]
        field_rows = comparison.get(field, request_context.get(field, []))
        status = "partial" if any(row["status"] == "partial" for row in field_rows) else "identified"
        explicit = explicit_states.get(field)
        if ids and explicit is not None:
            status = "partial"
        elif explicit and explicit.status == "partial":
            status = "partial"
        reason_codes = _final_state_reason_codes(explicit, has_server_values=bool(ids))
        states.append({
            "field_name": field,
            "status": status if ids else (explicit.status if explicit else "not_found"),
            **({"fact_ids": ids} if ids else {}),
            **({"reason_codes": reason_codes} if reason_codes else {}),
        })
    relation_ids = [item["delivery_relation_id"] for item in comparison["delivery_relations"]]
    relation_explicit = explicit_states.get("delivery_relations")
    relation_status = "identified" if relation_ids else (relation_explicit.status if relation_explicit else "not_found")
    if relation_ids and relation_explicit is not None:
        relation_status = "partial"
    elif relation_explicit and relation_explicit.status == "partial":
        relation_status = "partial"
    relation_codes = _final_state_reason_codes(relation_explicit, has_server_values=bool(relation_ids))
    states.append({"field_name": "delivery_relations", "status": relation_status, **({"relation_ids": relation_ids} if relation_ids else {}), **({"reason_codes": relation_codes} if relation_codes else {})})
    method_ids = [item["fact_id"] for item in comparison["delivery_methods"]]
    method_status = "partial" if any(item["status"] == "partial" for item in comparison["delivery_methods"]) else "identified"
    method_explicit = explicit_states.get("delivery_methods")
    if method_ids and method_explicit is not None:
        method_status = "partial"
    elif method_explicit and method_explicit.status == "partial":
        method_status = "partial"
    method_codes = _final_state_reason_codes(method_explicit, has_server_values=bool(method_ids))
    states.append({"field_name": "delivery_methods", "status": method_status if method_ids else (method_explicit.status if method_explicit else "not_found"), **({"fact_ids": method_ids} if method_ids else {}), **({"reason_codes": method_codes} if method_codes else {})})
    component_ids = [item["support_component_id"] for item in components]
    component_explicit = explicit_states.get("support_components")
    component_status = "identified" if component_ids else (component_explicit.status if component_explicit else "not_found")
    if component_ids and component_explicit is not None:
        component_status = "partial"
    elif component_explicit and component_explicit.status == "partial":
        component_status = "partial"
    component_codes = _final_state_reason_codes(component_explicit, has_server_values=bool(component_ids))
    states.append({"field_name": "support_components", "status": component_status, **({"component_ids": component_ids} if component_ids else {}), **({"reason_codes": component_codes} if component_codes else {})})

    source_doc = _source_document(document)
    profile = {
        "schema_version": REQUEST_SCHEMA_VERSION,
        "profile_id": selection.profile_id,
        "profile_type": "pre_review_request",
        "identity": {"title_raw": None, "source_document_id": identity["document_id"]},
        "request_type": request_type,
        "program_hierarchy": {"nodes": hierarchy},
        "request_context": request_context,
        "comparison_profile": comparison,
        "support_components": components,
        "derived_projections": [],
        "field_states": states,
        "unresolved_relations": [],
        "source_documents": [source_doc],
        "processing_metadata": {
            "pipeline_version": REQUEST_PIPELINE_VERSION,
            "structured_schema_version": REQUEST_SCHEMA_VERSION,
            "input_contract": "common_ir_v1",
            "common_ir_document_id": identity["document_id"],
            "common_ir_source_sha256": identity["source_sha256"],
            "common_ir_schema_version": document["schema_version"],
            "common_ir_generator": document["document"]["provenance"]["generator"],
            "common_ir_generator_version": document["document"]["provenance"]["generator_version"],
            "candidate_pack": {
                "candidate_pack_id": pack.pack_id,
                "candidate_pack_generator": pack.generator,
                "candidate_pack_generator_version": pack.generator_version,
                "common_ir_document_id": identity["document_id"],
                "common_ir_source_sha256": identity["source_sha256"],
                "text_basis": TEXT_BASIS,
            },
            "model_id": model_id,
            "prompt_version": prompt_version,
            "processed_at": datetime.now(timezone.utc).isoformat(),
        },
    }
    issues = _validate_request_profile(profile, pack)
    if issues:
        raise ValueError("Request profile validation failed: " + "; ".join(issues))
    return profile
