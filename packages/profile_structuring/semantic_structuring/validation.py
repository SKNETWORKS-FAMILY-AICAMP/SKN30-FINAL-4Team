"""Cross-object validation that a JSON schema alone cannot express."""

from __future__ import annotations

from dataclasses import dataclass

from .models import CandidatePack, ExtractionScope, FactField, FactScope, QuantityType, SemanticExtraction, TargetFact


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    message: str
    fact_index: int | None = None


def validate_extraction(
    extraction: SemanticExtraction, candidate_pack: CandidatePack | None = None
) -> list[ValidationIssue]:
    """Return safe-to-retry issues; never invent a corrected business fact."""

    issues: list[ValidationIssue] = []
    components = {item.support_component_id: item for item in getattr(extraction, "support_components", [])}
    facts_by_id = {fact.fact_id: fact for fact in extraction.facts}

    if not (extraction.facts or extraction.table_catalog or extraction.unresolved_relations):
        issues.append(ValidationIssue("empty_extraction", "no fact, table, or unresolved relation was returned"))

    if candidate_pack is not None:
        if extraction.notice_id != candidate_pack.notice_id:
            issues.append(ValidationIssue("notice_mismatch", "extraction and candidate pack have different notice IDs"))
        if extraction.candidate_pack_id != candidate_pack.pack_id:
            issues.append(ValidationIssue("candidate_pack_mismatch", "extraction points to a different candidate pack"))

        available_blocks = {block.block_id for block in candidate_pack.blocks}
        referenced_blocks = set()
        for fact in extraction.facts:
            referenced_blocks.update(fact.source_block_ids)
        for component in getattr(extraction, "support_components", []):
            referenced_blocks.update(component.source_block_ids)
        for table in extraction.table_catalog:
            referenced_blocks.update(table.source_block_ids)
        for relation in extraction.unresolved_relations:
            referenced_blocks.update(relation.source_block_ids)
        for block_id in sorted(referenced_blocks - available_blocks):
            issues.append(ValidationIssue("source_outside_candidate_pack", f"source block is not in candidate pack: {block_id}"))

        used_blocks = referenced_blocks
        if candidate_pack.extraction_scope == ExtractionScope.CANDIDATE_PACK:
            unused_candidates = [
                block.block_id
                for block in candidate_pack.blocks
                if block.relation.value == "candidate" and block.block_id not in used_blocks
            ]
            if unused_candidates:
                issues.append(
                    ValidationIssue("unaddressed_candidate", f"candidate blocks were not addressed: {', '.join(unused_candidates)}")
                )

    for index, fact in enumerate(extraction.facts):
        if fact.scope == FactScope.COMPONENT and getattr(fact, "support_component_id", None) not in components:
            issues.append(ValidationIssue("unknown_component", "component fact references an unknown support component", index))

        preference_signal = (fact.semantic_role or "").lower()
        if fact.field_name == FactField.APPLICANT_ELIGIBILITY and "selection_preferences" in preference_signal:
            issues.append(
                ValidationIssue(
                    "preference_in_eligibility",
                    "selection preference belongs to B rule data, not applicant eligibility",
                    index,
                )
            )

        if fact.field_name == FactField.DELIVERY_ROLES and fact.scope != FactScope.NOTICE:
            issues.append(ValidationIssue("delivery_role_not_notice_scoped", "delivery_roles must be notice-scoped, not a support component", index))

        normalized_value = getattr(fact, "normalized_value", None)
        if (
            normalized_value is not None
            and normalized_value.quantity_type == QuantityType.SELECTION_CAPACITY
            and fact.status.value == "identified"
            and fact.scope == FactScope.NOTICE
        ):
            for target_id in fact.applies_to_fact_ids:
                target = facts_by_id.get(target_id)
                if target is None:
                    issues.append(ValidationIssue("unknown_selection_target", f"selection capacity references unknown fact: {target_id}", index))
                elif not isinstance(target, TargetFact) or target.field_name not in {
                    FactField.BENEFICIARY,
                    FactField.APPLICABLE_ENTITY,
                }:
                    issues.append(ValidationIssue("invalid_selection_target", "selection capacity must reference a beneficiary or applicable_entity fact", index))

    for component in components.values():
        if component.component_kind.value == "participation_type":
            same_kind = [item for item in getattr(extraction, "support_components", []) if item.component_kind.value == "participation_type"]
            if len(same_kind) < 2:
                issues.append(ValidationIssue("invalid_participation_type", "participation_type requires two or more explicit alternative types"))

        
    for index, fact in enumerate(extraction.facts):
        preference_signal = (fact.semantic_role or "").lower()
        if any(signal in preference_signal for signal in ("selection_preferences", "preference", "bonus", "가점")):
            issues.append(
                ValidationIssue(
                    "preference_as_a_fact",
                    "selection preference must be stored as B rule data, not as an A comparison fact",
                    index,
                )
            )

    return issues
