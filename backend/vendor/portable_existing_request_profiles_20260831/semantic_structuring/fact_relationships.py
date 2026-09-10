"""Conditional, source-preserving links between already selected facts.

This is deliberately a second, small decision: extraction selects facts; this
module only decides permitted edges between their ids.  It never receives or
produces rewritten business values.
"""

from __future__ import annotations

from collections import defaultdict
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class RelationType(StrEnum):
    DIRECT_RECIPIENT = "direct_recipient"
    ELIGIBILITY_OR_CALCULATION_BASIS = "eligibility_or_calculation_basis"


class RelationCandidate(StrictModel):
    from_fact_id: str = Field(min_length=1)
    to_fact_id: str = Field(min_length=1)
    permitted_relation_types: list[RelationType] = Field(min_length=1)


class FactRelation(StrictModel):
    from_fact_id: str = Field(min_length=1)
    to_fact_id: str = Field(min_length=1)
    relation_type: RelationType


class FactRelationshipExtraction(StrictModel):
    notice_id: str = Field(min_length=1)
    relations: list[FactRelation] = Field(default_factory=list)


_SUPPORT_FIELDS = {"support_scale", "support_methods", "support_items", "payment_terms"}
_RECIPIENT_FIELDS = {"beneficiary"}
_BASIS_FIELDS = {"applicant_eligibility", "applicable_entity", "participation_requirements"}


def build_relationship_payload(selection_artifact: dict[str, Any]) -> dict[str, Any] | None:
    """Build only meaningful within-component relation candidates.

    Field roles reduce the candidate graph; they do not decide a relationship.
    The model can return no edge when an otherwise compatible pair is not
    explicitly connected by the source/table context.
    """

    selection = selection_artifact.get("selection", {})
    evidence_by_id = {row["fact_id"]: row for row in selection_artifact.get("materialized_evidence", [])}
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for fact in selection.get("facts", []):
        component_id = fact.get("primary_component_id") or fact.get("support_component_id")
        if component_id:
            grouped[component_id].append(fact)

    candidates: list[RelationCandidate] = []
    fact_payload: dict[str, dict[str, Any]] = {}
    for component_id, facts in grouped.items():
        supports = [fact for fact in facts if fact.get("field_name") in _SUPPORT_FIELDS]
        recipients = [fact for fact in facts if fact.get("field_name") in _RECIPIENT_FIELDS]
        bases = [fact for fact in facts if fact.get("field_name") in _BASIS_FIELDS]
        for support in supports:
            for recipient in recipients:
                candidates.append(
                    RelationCandidate(
                        from_fact_id=support["fact_id"],
                        to_fact_id=recipient["fact_id"],
                        permitted_relation_types=[RelationType.DIRECT_RECIPIENT],
                    )
                )
            for basis in bases:
                candidates.append(
                    RelationCandidate(
                        from_fact_id=support["fact_id"],
                        to_fact_id=basis["fact_id"],
                        permitted_relation_types=[RelationType.ELIGIBILITY_OR_CALCULATION_BASIS],
                    )
                )
        for fact in facts:
            row = evidence_by_id.get(fact["fact_id"])
            if row:
                fact_payload[fact["fact_id"]] = {
                    "fact_id": fact["fact_id"],
                    "field_name": fact["field_name"],
                    "semantic_role": fact.get("semantic_role"),
                    "subject_role": fact.get("subject_role"),
                    "value_blocks": row.get("source_blocks", []),
                    "context_blocks": row.get("context_blocks", []),
                    "component_id": component_id,
                }
    if not candidates:
        return None
    return {
        "notice_id": selection.get("notice_id"),
        "facts": list(fact_payload.values()),
        "candidate_relations": [candidate.model_dump(mode="json") for candidate in candidates],
    }


def validate_relationships(
    extraction: FactRelationshipExtraction,
    payload: dict[str, Any],
) -> None:
    if extraction.notice_id != payload["notice_id"]:
        raise ValueError("relationship output belongs to a different notice")
    allowed = {
        (row["from_fact_id"], row["to_fact_id"]): set(row["permitted_relation_types"])
        for row in payload["candidate_relations"]
    }
    seen: set[tuple[str, str, RelationType]] = set()
    for relation in extraction.relations:
        permitted = allowed.get((relation.from_fact_id, relation.to_fact_id))
        if not permitted or relation.relation_type.value not in permitted:
            raise ValueError(f"relationship is outside the server-generated candidate set: {relation}")
        key = (relation.from_fact_id, relation.to_fact_id, relation.relation_type)
        if key in seen:
            raise ValueError(f"duplicate relationship: {relation}")
        seen.add(key)


def apply_relationships(selection_artifact: dict[str, Any], relationships: FactRelationshipExtraction) -> dict[str, Any]:
    """Return a copy with validated relationship ids attached to selected facts."""

    import copy

    result = copy.deepcopy(selection_artifact)
    by_id = {fact["fact_id"]: fact for fact in result["selection"]["facts"]}
    evidence_by_id = {row["fact_id"]: row for row in result.get("materialized_evidence", [])}
    for relation in relationships.relations:
        key = "recipient_fact_ids" if relation.relation_type == RelationType.DIRECT_RECIPIENT else "basis_fact_ids"
        for target in (by_id[relation.from_fact_id], evidence_by_id[relation.from_fact_id]):
            target[key] = list(dict.fromkeys([*target.get(key, []), relation.to_fact_id]))
    return result
