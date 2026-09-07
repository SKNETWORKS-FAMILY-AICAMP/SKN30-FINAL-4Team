"""Loss-aware projection and preparation for production Common IR v1.

HWP/HWPX and PDF inputs remain independent.  This module never merges paired
documents or mutates Common IR; it exposes canonical blocks, exact table-cell
occurrences, and structural attachment boundaries for the existing A/B/C
pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from .models import CandidatePack, SourceBlock, SourceRelation
from .notice_preparation import AttachmentScope, CallUsage, SectionScopeDecision


COMMON_IR_V1_CANDIDATE_PACK_GENERATOR = "semantic_structuring.common_ir_v1"
COMMON_IR_V1_CANDIDATE_PACK_GENERATOR_VERSION = "1"

# A [서식] marker can occur before a notice resumes with its next numbered
# top-level heading.  Keep this conservative so ordinary numbered form fields
# do not become main-notice text.
_NOTICE_RESUME_HEADING = re.compile(
    r"^\s*\d{1,2}\s*(?:[.)]|\n)\s*"
    r"(?:사업\s*(?:개요|목적|추진\s*절차)|지원\s*(?:대상|내용|규모)|"
    r"신청\s*방법|선정\s*(?:및\s*)?지원\s*내용|기타\s*유의\s*사항|문의\s*(?:처|사항)|추진\s*(?:절차|일정))"
)


@dataclass(frozen=True)
class CommonIRSection:
    section_id: str
    source_block_ids: list[str]
    boundary_kind: str
    title_raw: str | None
    scope: str | None = None


def _is_main_notice_section(section_id: str) -> bool:
    return section_id == "main_notice" or section_id.startswith("main_notice_resume_")


@dataclass(frozen=True)
class CommonIRV1Projection:
    notice_id: str
    source_kind: str
    common_ir_document_id: str
    blocks: list[SourceBlock]
    table_cell_blocks: list[SourceBlock]
    sections: list[CommonIRSection]
    selected_occurrence_ids: dict[str, list[str]]
    suppressed_wrapper_block_ids: list[str]


class ScanOnlyDocument(ValueError):
    """A PDF explicitly excluded from semantic structuring by source policy."""


@dataclass(frozen=True)
class CommonIRPreparedNotice:
    """Duck-typed PreparedNotice for direct Common IR v1 consumption.

    ``section_scope_applied`` begins false.  The router/A pack must not run
    until a separate scope decision has classified attachment sections.
    """

    notice_id: str
    common_ir_document_id: str
    candidate_pack_generator: str
    candidate_pack_generator_version: str
    sections: list[CommonIRSection]
    fact_candidate_blocks: list[SourceBlock]
    table_candidate_blocks: list[SourceBlock]
    table_cell_candidate_blocks: list[SourceBlock]
    search_only_blocks: list[SourceBlock]
    excluded_blocks: list[SourceBlock]
    section_scope_applied: bool = False

    def router_pack(self) -> CandidatePack:
        # Attachment tables are already assigned to B search retention by the
        # section-scope policy.  Routing them as possible A tables would make
        # an LLM disposition override that policy and later fail because their
        # cells deliberately are not A evidence candidates.
        blocks = self.fact_candidate_blocks + [
            block for block in self.table_candidate_blocks if _is_main_notice_section(block.section_id or "")
        ]
        return CandidatePack(
            pack_id=f"{self.notice_id}-common-ir-v1-router",
            notice_id=self.notice_id,
            extraction_scope="document",
            question="Routing only; never sent to the model.",
            blocks=blocks,
            generator=self.candidate_pack_generator,
            generator_version=self.candidate_pack_generator_version,
            common_ir_document_id=self.common_ir_document_id,
        )


def common_ir_v1_identity(document: dict[str, Any]) -> dict[str, str | None]:
    source = document["document"]
    return {"document_id": source["document_id"], "source_kind": source["source_kind"], "source_sha256": source.get("provenance", {}).get("source_sha256")}


def ensure_common_ir_v1_is_semantically_eligible(document: dict[str, Any]) -> None:
    """Reject only the explicit upstream scan-only decision.

    PDF OCR is not a fallback input. Upstream marks an image-only PDF as
    ``excluded_image_only``; no scope/router/selection/final JSON may be made
    for it. Older Common IR samples without the field remain readable.
    """

    source = document["document"]
    if source.get("source_kind") == "pdf" and source.get("pdf_semantic_eligibility") == "excluded_image_only":
        raise ScanOnlyDocument("PDF is excluded_image_only; semantic structuring is not permitted")


def common_ir_v1_section_scope_payload(prepared: CommonIRPreparedNotice, *, identity: dict[str, str | None] | None = None) -> dict[str, Any]:
    """Persist v1 scopes without pretending IDs are legacy ``body[n]`` IDs."""

    return {
        "notice_id": prepared.notice_id,
        "input_contract": "common_ir_v1",
        "common_ir_identity": identity,
        "sections": [
            {
                "section_id": section.section_id,
                "source_block_ids": section.source_block_ids,
                "boundary_kind": section.boundary_kind,
                "title_raw": section.title_raw,
                **({"scope": section.scope} if section.scope is not None else {}),
            }
            for section in prepared.sections
        ],
        "prepared_counts": {
            "fact_candidate_blocks": len(prepared.fact_candidate_blocks),
            "table_candidate_blocks": len(prepared.table_candidate_blocks),
            "table_cell_candidate_blocks": len(prepared.table_cell_candidate_blocks),
            "search_only_blocks": len(prepared.search_only_blocks),
            "excluded_blocks": len(prepared.excluded_blocks),
        },
    }


def _occurrence_text(document: dict[str, Any]) -> dict[str, str]:
    return {
        occurrence["occurrence_id"]: occurrence.get("text", "")
        for block in document["blocks"]
        for occurrence in block.get("occurrences", [])
    }


def _table_contains_children(document: dict[str, Any]) -> set[str]:
    """Find child table block ids from explicit table_contains relations."""

    return {
        relation["to_id"]
        for relation in document.get("relations", [])
        if relation.get("kind") == "table_contains"
        and relation.get("inferred") is False
        and relation.get("structure_status") == "explicit"
        and relation.get("to_id")
    }


def _suppressible_wrappers(document: dict[str, Any], occurrence_text: dict[str, str]) -> set[str]:
    """Suppress only empty explicit wrappers that have an explicit child table.

    A non-empty wrapper can carry a caption or sibling paragraph and must stay
    visible.  v1's explicit table_contains relation avoids filename/id-pattern
    inference for the nested-table decision.
    """

    children = _table_contains_children(document)
    wrappers: set[str] = set()
    for relation in document.get("relations", []):
        if (relation.get("kind") != "table_contains" or relation.get("to_id") not in children
                or relation.get("inferred") is not False or relation.get("structure_status") != "explicit"):
            continue
        parent_cell_id = relation.get("from_id", "")
        parent_block_id = parent_cell_id.rsplit(":c", 1)[0]
        parent = next((block for block in document["blocks"] if block["block_id"] == parent_block_id), None)
        if parent is None or parent.get("kind") != "table" or parent.get("structure_status") != "explicit":
            continue
        text = parent.get("text", "").strip()
        if not text:
            wrappers.add(parent_block_id)
    return wrappers


def _sections(blocks: list[dict[str, Any]]) -> list[CommonIRSection]:
    ordered = sorted(blocks, key=lambda block: block["reading_order"])
    starts = [index for index, block in enumerate(ordered) if block.get("boundary_markers")]
    if not starts:
        return [CommonIRSection("main_notice", [block["block_id"] for block in ordered], "document_start", None)]
    sections: list[CommonIRSection] = []
    if starts[0] > 0:
        sections.append(CommonIRSection("main_notice", [block["block_id"] for block in ordered[: starts[0]]], "document_start", None))
    for ordinal, start in enumerate(starts, start=1):
        end = starts[ordinal] if ordinal < len(starts) else len(ordered)
        part = ordered[start:end]
        marker = part[0]["boundary_markers"][0]
        sections.append(CommonIRSection(f"attachment_{ordinal}", [block["block_id"] for block in part], marker["marker"], marker["matched_text"]))
    # Do not let a form boundary swallow a later numbered notice body.  The
    # source remains immutable; only the preparation-time section partition is
    # repaired.  One resumed main segment lasts until the next explicit
    # boundary marker, so consecutive [서식 1-2]/[서식 1-4] sections stay C.
    by_id = {block["block_id"]: block for block in ordered}
    repaired: list[CommonIRSection] = []
    resume_ordinal = 0
    for section in sections:
        if _is_main_notice_section(section.section_id):
            repaired.append(section)
            continue
        resume_at = next(
            (
                index for index, block_id in enumerate(section.source_block_ids[1:], start=1)
                if _NOTICE_RESUME_HEADING.match(by_id[block_id].get("text", ""))
            ),
            None,
        )
        if resume_at is None:
            repaired.append(section)
            continue
        repaired.append(CommonIRSection(
            section.section_id, section.source_block_ids[:resume_at], section.boundary_kind, section.title_raw,
        ))
        resume_ordinal += 1
        repaired.append(CommonIRSection(
            f"main_notice_resume_{resume_ordinal}", section.source_block_ids[resume_at:],
            "notice_heading_resume", None,
        ))
    return [section for section in repaired if section.source_block_ids]


def project_common_ir_v1(document: dict[str, Any]) -> CommonIRV1Projection:
    if document.get("schema_version") != "common_ir_v1":
        raise ValueError("expected common_ir_v1")
    ensure_common_ir_v1_is_semantically_eligible(document)
    metadata = document["document"]
    _source_kind, notice_id = metadata["document_id"].split(":", 1)
    occurrence_text = _occurrence_text(document)
    suppressed = _suppressible_wrappers(document, occurrence_text)
    blocks: list[SourceBlock] = []
    cells: list[SourceBlock] = []
    selected: dict[str, list[str]] = {}
    for block in sorted(document["blocks"], key=lambda item: item["reading_order"]):
        if block["block_id"] in suppressed or not block.get("text", "").strip():
            continue
        occurrence_ids = [item for item in block.get("text_occurrence_ids", []) if occurrence_text.get(item, "").strip()]
        source = SourceBlock(
            block_id=block["block_id"], text=block["text"].strip(), relation=SourceRelation.CANDIDATE,
            block_kind=block["kind"], source_order=block["reading_order"], source_occurrence_ids=occurrence_ids,
            common_ir_block_id=block["block_id"],
            common_ir_occurrence_ids=tuple(occurrence_ids),
        )
        blocks.append(source)
        selected[source.block_id] = occurrence_ids
        if block["kind"] != "table" or block.get("structure_status") != "explicit":
            continue
        for cell in sorted(block.get("cells", []), key=lambda item: (item.get("row_index", 0), item.get("col_index", 0), item["cell_id"])):
            for paragraph, occurrence_id in enumerate(cell.get("text_occurrence_ids", [])):
                text = occurrence_text.get(occurrence_id, "").strip()
                if not text:
                    continue
                cell_source = SourceBlock(
                    block_id=f"{block['block_id']}#r{cell.get('row_index', 0)}c{cell.get('col_index', 0)}p{paragraph}",
                    text=text, relation=SourceRelation.CANDIDATE, block_kind="table_cell",
                    source_order=block["reading_order"], source_occurrence_ids=[occurrence_id],
                    common_ir_block_id=block["block_id"],
                    common_ir_cell_id=cell["cell_id"],
                    common_ir_occurrence_ids=(occurrence_id,),
                )
                cells.append(cell_source)
                selected[cell_source.block_id] = [occurrence_id]
    return CommonIRV1Projection(
        notice_id=notice_id, source_kind=metadata["source_kind"],
        common_ir_document_id=metadata["document_id"], blocks=blocks, table_cell_blocks=cells,
        sections=_sections(document["blocks"]), selected_occurrence_ids=selected,
        suppressed_wrapper_block_ids=sorted(suppressed),
    )


def prepare_common_ir_v1(document: dict[str, Any]) -> tuple[CommonIRPreparedNotice, CommonIRV1Projection]:
    projection = project_common_ir_v1(document)
    section_by_block = {block_id: section.section_id for section in projection.sections for block_id in section.source_block_ids}
    def scoped(block: SourceBlock) -> SourceBlock:
        return block.model_copy(update={"section_id": section_by_block.get(block.block_id, "main_notice")})
    return (
        CommonIRPreparedNotice(
            notice_id=projection.notice_id,
            common_ir_document_id=projection.common_ir_document_id,
            candidate_pack_generator=COMMON_IR_V1_CANDIDATE_PACK_GENERATOR,
            candidate_pack_generator_version=COMMON_IR_V1_CANDIDATE_PACK_GENERATOR_VERSION,
            sections=projection.sections,
            fact_candidate_blocks=[scoped(block) for block in projection.blocks if block.block_kind not in {"table", "table_candidate", "diagram_candidate"}],
            table_candidate_blocks=[scoped(block) for block in projection.blocks if block.block_kind == "table"],
            table_cell_candidate_blocks=[scoped(block) for block in projection.table_cell_blocks],
            search_only_blocks=[scoped(block) for block in projection.blocks if block.block_kind in {"table_candidate", "diagram_candidate"}], excluded_blocks=[], section_scope_applied=False,
        ),
        projection,
    )


def _v1_attachment_input(section: CommonIRSection, blocks_by_id: dict[str, SourceBlock]) -> dict[str, Any]:
    visible = [blocks_by_id[block_id] for block_id in section.source_block_ids if block_id in blocks_by_id]
    preview = [
        {"source_block_id": block.block_id, "text": block.text[:800], "block_kind": block.block_kind}
        for block in visible
    ]
    return {
        "section_id": section.section_id,
        "title_raw": section.title_raw,
        "boundary_kind": section.boundary_kind,
        "first_blocks": preview[:5],
        "last_blocks": preview[-2:],
        "block_count": len(section.source_block_ids),
    }


def classify_common_ir_v1_attachment_scopes(client: Any, document: dict[str, Any]) -> tuple[list[SectionScopeDecision], CallUsage]:
    """One scope-only LLM call over v1 attachment boundaries.

    This deliberately mirrors the pre-existing section policy.  The call does
    not see nor decide any business fact; its only output is attachment scope.
    """

    import json
    import os

    from .pipeline import _openai_schema, _usage
    from .notice_preparation import SectionScopeDiscovery

    prepared, _ = prepare_common_ir_v1(document)
    attachments = [section for section in prepared.sections if not _is_main_notice_section(section.section_id)]
    if not attachments:
        return [], CallUsage(0, 0, 0, 0, 0)
    source_by_id = {
        block.block_id: block
        for block in [*prepared.fact_candidate_blocks, *prepared.table_candidate_blocks]
    }
    instructions = (
        "Classify each attached-document section of a Korean public-support notice. Do not extract facts or decide field values. "
        "substantive_attachment: a reference table, rule, specification, or detail needed to understand the announced support; keep as searchable B. "
        "form_template: an application form, pledge, confirmation, consent, or blank fill-in template; classify C. "
        "operational_guide: a manual, procedure guide, sample screen, installation/testing instructions, or submission-operation material; classify C. "
        "mixed_or_unresolved: scope cannot safely be decided from this section preview; retain as searchable B. "
        "When unsure between substantive and form/guide, choose mixed_or_unresolved. Do not infer a section's value from its attachment number. "
        "Return only given section_id values and scope labels."
    )
    request = {
        "notice_id": prepared.notice_id,
        "attachments": [_v1_attachment_input(section, source_by_id) for section in attachments],
    }
    response = client.responses.create(
        model=os.environ.get("OPENAI_MODEL", "gpt-5.6-terra"),
        reasoning={"effort": os.environ.get("OPENAI_REASONING_EFFORT", "medium")},
        store=False,
        input=[
            {"role": "system", "content": instructions},
            {"role": "user", "content": json.dumps(request, ensure_ascii=False)},
        ],
        text={"format": {"type": "json_schema", "name": "section_scope_discovery", "strict": True, "schema": _openai_schema(SectionScopeDiscovery)}},
    )
    result = SectionScopeDiscovery.model_validate_json(response.output_text)
    expected = {section.section_id for section in attachments}
    actual = [decision.section_id for decision in result.decisions]
    if result.notice_id != prepared.notice_id or set(actual) != expected or len(actual) != len(set(actual)):
        raise ValueError("section scope output did not cover each v1 attachment exactly once")
    return result.decisions, _usage(response, 0)


def apply_common_ir_v1_section_scopes(
    document: dict[str, Any], decisions: list[SectionScopeDecision]
) -> CommonIRPreparedNotice:
    """Apply attachment scopes while keeping HWP/HWPX/PDF evidence independent."""

    unscoped, _ = prepare_common_ir_v1(document)
    attachments = [section for section in unscoped.sections if not _is_main_notice_section(section.section_id)]
    scope_by_section = {decision.section_id: decision.scope.value for decision in decisions}
    expected = {section.section_id for section in attachments}
    if set(scope_by_section) != expected or len(scope_by_section) != len(decisions):
        raise ValueError("section scope decisions must cover each Common IR attachment exactly once")
    section_by_block = {block_id: section.section_id for section in unscoped.sections for block_id in section.source_block_ids}
    scope_for_block = {
        block_id: "main_notice" if _is_main_notice_section(section_id) else scope_by_section[section_id]
        for block_id, section_id in section_by_block.items()
    }
    scoped_sections = [
        CommonIRSection(
            section.section_id, section.source_block_ids, section.boundary_kind,
            section.title_raw,
            "main_notice" if _is_main_notice_section(section.section_id) else scope_by_section[section.section_id],
        )
        for section in unscoped.sections
    ]
    # Dataclasses are intentionally immutable; scope stays in block partitions
    # and output artifact, rather than becoming a mutable source property.
    def with_section(block: SourceBlock) -> SourceBlock:
        return block.model_copy(update={"section_id": section_by_block.get(block.block_id, "main_notice")})

    all_blocks = [with_section(block) for block in [*unscoped.fact_candidate_blocks, *unscoped.table_candidate_blocks, *unscoped.search_only_blocks]]
    all_cells = [with_section(block) for block in unscoped.table_cell_candidate_blocks]
    fact, tables, cells, search_only, excluded = [], [], [], [], []
    for block in all_blocks:
        scope = scope_for_block.get(block.block_id, "main_notice")
        if block.block_kind in {"table_candidate", "diagram_candidate"}:
            search_only.append(block)
            continue
        if scope == "main_notice":
            (tables if block.block_kind == "table" else fact).append(block)
        elif scope in {AttachmentScope.SUBSTANTIVE_ATTACHMENT.value, AttachmentScope.MIXED_OR_UNRESOLVED.value}:
            search_only.append(block)
            if block.block_kind == "table":
                tables.append(block)
        else:
            excluded.append(block)
    # Only a main-notice explicit table may enter A as cell evidence.  Attachment
    # tables remain B even when a router sees a monetary value in them.
    cells = [block for block in all_cells if scope_for_block.get(block.block_id.split("#", 1)[0]) == "main_notice"]
    return CommonIRPreparedNotice(
        notice_id=unscoped.notice_id,
        common_ir_document_id=unscoped.common_ir_document_id,
        candidate_pack_generator=unscoped.candidate_pack_generator,
        candidate_pack_generator_version=unscoped.candidate_pack_generator_version,
        sections=scoped_sections,
        fact_candidate_blocks=fact, table_candidate_blocks=tables,
        table_cell_candidate_blocks=cells, search_only_blocks=search_only,
        excluded_blocks=excluded, section_scope_applied=True,
    )


def common_ir_v1_metadata(document: dict[str, Any]) -> dict[str, Any]:
    """Minimal metadata for final assembly; it does not invent catalogue data."""

    from .profile_v02 import validate_common_ir_lineage

    source = document["document"]
    _, notice_id = source["document_id"].split(":", 1)
    common_ir = {
        "document_id": source["document_id"],
        "schema_version": "common_ir_v1",
        "source_kind": source["source_kind"],
        "source_sha256": source.get("provenance", {}).get("source_sha256"),
        "source_location": source.get("provenance", {}).get("source_location"),
    }
    if source.get("artifact_role") is not None:
        common_ir["artifact_role"] = source["artifact_role"]
    validate_common_ir_lineage(common_ir)
    return {
        "notice_id": notice_id,
        "common_ir": common_ir,
    }
