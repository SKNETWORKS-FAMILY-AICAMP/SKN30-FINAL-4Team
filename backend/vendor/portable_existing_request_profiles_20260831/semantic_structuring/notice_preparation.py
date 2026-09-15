"""Common-IR preparation before semantic fact and table processing.

The format adapter has already produced common IR.  This stage neither parses
facts nor changes source text: it splits attached documents, classifies their
scope, and exposes separate A, B-table, B-search, and C paths.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from openai import OpenAI
from pydantic import BaseModel, ConfigDict

from .models import CandidatePack, SourceBlock, SourceRelation
from .pipeline import CallUsage, _openai_schema, _usage
from .sectioning import SectionCandidate, split_attachment_sections


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AttachmentScope(StrEnum):
    SUBSTANTIVE_ATTACHMENT = "substantive_attachment"
    FORM_TEMPLATE = "form_template"
    OPERATIONAL_GUIDE = "operational_guide"
    MIXED_OR_UNRESOLVED = "mixed_or_unresolved"


class SectionScopeDecision(StrictModel):
    section_id: str
    scope: AttachmentScope


class SectionScopeDiscovery(StrictModel):
    notice_id: str
    decisions: list[SectionScopeDecision]


@dataclass(frozen=True)
class ScopedSection:
    candidate: SectionCandidate
    scope: str


@dataclass(frozen=True)
class PreparedNotice:
    """Canonical source blocks partitioned by product policy.

    * ``fact_candidate_blocks``: the only blocks eligible for A fact
      extraction.
    * ``table_candidate_blocks``: B table catalog input.
    * ``search_only_blocks``: B attachment source retained for retrieval.
    * ``excluded_blocks``: C form/guide source, retained in raw IR only.
    """

    notice_id: str
    sections: list[ScopedSection]
    fact_candidate_blocks: list[SourceBlock]
    table_candidate_blocks: list[SourceBlock]
    table_cell_candidate_blocks: list[SourceBlock]
    search_only_blocks: list[SourceBlock]
    excluded_blocks: list[SourceBlock]
    # False means the adapter has not yet established attachment boundaries;
    # it can be inspected but must not enter the production A pack.
    section_scope_applied: bool = True

    def router_pack(self) -> CandidatePack:
        """Broad router input: main text plus all eligible table blocks."""

        blocks = self.fact_candidate_blocks + self.table_candidate_blocks
        return CandidatePack(
            pack_id=f"{self.notice_id}-router-v0.2",
            notice_id=self.notice_id,
            extraction_scope="document",
            question="Routing only; never sent to the model.",
            blocks=blocks,
        )

    def fact_pack(self) -> CandidatePack:
        return CandidatePack(
            pack_id=f"{self.notice_id}-facts-v0.2",
            notice_id=self.notice_id,
            extraction_scope="document",
            question="Fact extraction only; never sent to the model.",
            blocks=self.fact_candidate_blocks,
        )


def section_scope_payload(prepared: PreparedNotice) -> dict[str, Any]:
    """Stable artifact body for reusing one scope call in downstream stages."""

    return {
        "notice_id": prepared.notice_id,
        "sections": [
            {
                "section_id": section.candidate.section_id,
                "source_block_range": [
                    f"body[{section.candidate.start_block_index}]",
                    f"body[{section.candidate.end_block_index}]",
                ],
                "boundary_kind": section.candidate.boundary_kind,
                "title_raw": section.candidate.title_raw,
                "scope": section.scope,
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


def _table_text(table: dict[str, Any]) -> str:
    cells = []
    for cell in table.get("cells", []):
        text = " ".join(block.get("text", "") for block in cell.get("blocks", [])).strip()
        if text:
            cells.append(f"r{cell['row']}c{cell['col']}: {text}")
    return f"[table {table.get('rows')}x{table.get('cols')}] " + " | ".join(cells)


def _table_cell_blocks(item: dict[str, Any], body_index: int, section_id: str) -> list[SourceBlock]:
    """Expose table-cell paragraphs without flattening them into one claim.

    A table remains a B/table object for cataloguing.  These blocks exist only
    as precise A-fact evidence candidates when the table is in the main notice.
    The id is deterministic from the IR position, so the server can recover the
    exact value cell and any header/context cell later.
    """

    blocks: list[SourceBlock] = []
    for cell in item.get("cells", []):
        for paragraph_index, paragraph in enumerate(cell.get("blocks", [])):
            text = paragraph.get("text", "").strip()
            if not text:
                continue
            blocks.append(
                SourceBlock(
                    block_id=(
                        f"body[{body_index}]#r{cell.get('row', 0)}c{cell.get('col', 0)}"
                        f"p{paragraph_index}"
                    ),
                    text=text,
                    relation="candidate",
                    block_kind="table_cell",
                    section_id=section_id,
                )
            )
    return blocks


def source_blocks_from_document(document: dict[str, Any]) -> list[SourceBlock]:
    blocks: list[SourceBlock] = []
    for index, item in enumerate(document["ir"]["body"]):
        text = item.get("text", "") if item.get("kind") != "table" else _table_text(item)
        if text.strip():
            blocks.append(
                SourceBlock(
                    block_id=f"body[{index}]",
                    text=text,
                    relation=SourceRelation.CANDIDATE,
                    block_kind=item.get("kind"),
                )
            )
    return blocks


def _section_input(section: SectionCandidate, body: list[dict[str, Any]]) -> dict[str, Any]:
    texts = []
    for index in range(section.start_block_index, section.end_block_index + 1):
        item = body[index]
        text = _table_text(item) if item.get("kind") == "table" else item.get("text", "")
        if text.strip():
            texts.append({"source_block_id": f"body[{index}]", "text": text[:800]})
    return {
        "section_id": section.section_id,
        "title_raw": section.title_raw,
        "first_blocks": texts[:5],
        "last_blocks": texts[-2:],
        "block_count": section.end_block_index - section.start_block_index + 1,
    }


def classify_attachment_scopes(client: OpenAI, document: dict[str, Any]) -> tuple[list[SectionScopeDecision], CallUsage]:
    """Classify attachment purpose; exact facts and values stay out of output."""

    body = document["ir"]["body"]
    attachments = [section for section in split_attachment_sections(body) if section.section_id != "main_notice"]
    if not attachments:
        return [], CallUsage(0, 0, 0, 0, 0)

    instructions = (
        "Classify each attached-document section of a Korean public-support notice. Do not extract facts or decide field values. "
        "substantive_attachment: a reference table, rule, specification, or detail needed to understand the announced support; keep as searchable B. "
        "form_template: an application form, pledge, confirmation, consent, or blank fill-in template; classify C. "
        "operational_guide: a manual, procedure guide, sample screen, installation/testing instructions, or submission-operation material; classify C. "
        "mixed_or_unresolved: scope cannot safely be decided from this section preview; retain as searchable B. "
        "When unsure between substantive and form/guide, choose mixed_or_unresolved. Do not infer a section's value from its attachment number. "
        "Return only given section_id values and scope labels."
    )
    request = {"notice_id": document["notice_id"], "attachments": [_section_input(section, body) for section in attachments]}
    response = client.responses.create(
        model=os.environ.get("OPENAI_MODEL", "gpt-5.6-terra"),
        reasoning={"effort": os.environ.get("OPENAI_REASONING_EFFORT", "medium")},
        store=False,
        input=[
            {"role": "system", "content": instructions},
            {"role": "user", "content": json.dumps(request, ensure_ascii=False)},
        ],
        text={
            "format": {
                "type": "json_schema",
                "name": "section_scope_discovery",
                "strict": True,
                "schema": _openai_schema(SectionScopeDiscovery),
            }
        },
    )
    result = SectionScopeDiscovery.model_validate_json(response.output_text)
    expected_ids = {section.section_id for section in attachments}
    actual_ids = [decision.section_id for decision in result.decisions]
    if result.notice_id != document["notice_id"] or set(actual_ids) != expected_ids or len(actual_ids) != len(set(actual_ids)):
        raise ValueError("section scope output did not cover each attachment exactly once")
    return result.decisions, _usage(response, 0)


def prepare_notice(document: dict[str, Any], decisions: list[SectionScopeDecision]) -> PreparedNotice:
    """Apply section scopes without mutating common IR or source wording."""

    body = document["ir"]["body"]
    candidates = split_attachment_sections(body)
    attachments = [section for section in candidates if section.section_id != "main_notice"]
    decision_by_id = {decision.section_id: decision.scope.value for decision in decisions}
    expected_ids = {section.section_id for section in attachments}
    if set(decision_by_id) != expected_ids or len(decision_by_id) != len(decisions):
        raise ValueError("section scope decisions must cover each attachment exactly once")

    scoped = [
        ScopedSection(section, "main_notice" if section.section_id == "main_notice" else decision_by_id[section.section_id])
        for section in candidates
    ]
    scope_by_block: dict[str, str] = {}
    for section in scoped:
        for block_id in section.candidate.source_block_ids:
            scope_by_block[block_id] = section.scope

    fact_blocks: list[SourceBlock] = []
    table_blocks: list[SourceBlock] = []
    table_cell_blocks: list[SourceBlock] = []
    search_only: list[SourceBlock] = []
    excluded: list[SourceBlock] = []
    for block in source_blocks_from_document(document):
        scope = scope_by_block[block.block_id]
        section_id = next(section.candidate.section_id for section in scoped if block.block_id in section.candidate.source_block_ids)
        block = block.model_copy(update={"section_id": section_id})
        if scope == "main_notice":
            if block.block_kind == "table":
                table_blocks.append(block)
                table_cell_blocks.extend(_table_cell_blocks(body[int(block.block_id[5:-1])], int(block.block_id[5:-1]), section_id))
            else:
                fact_blocks.append(block)
        elif scope in {AttachmentScope.SUBSTANTIVE_ATTACHMENT.value, AttachmentScope.MIXED_OR_UNRESOLVED.value}:
            search_only.append(block)
            if block.block_kind == "table":
                table_blocks.append(block)
        else:
            excluded.append(block)

    return PreparedNotice(
        document["notice_id"],
        scoped,
        fact_blocks,
        table_blocks,
        table_cell_blocks,
        search_only,
        excluded,
    )
