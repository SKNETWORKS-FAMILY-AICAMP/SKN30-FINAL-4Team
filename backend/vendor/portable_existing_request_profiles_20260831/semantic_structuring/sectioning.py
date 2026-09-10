"""Structural section candidates for common-IR documents.

This module intentionally only detects *document boundaries*.  It does not
decide whether an attachment is useful: that is a semantic decision made in a
separate section-scope pass.  In particular, an attachment marker must never
mean "discard this content" by itself.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


# These patterns identify a new attached document, not an in-body reference
# such as "세부사항은 붙임 1 참고".  The surrounding brackets/style are a
# structural cue from the source document; semantic scope is deliberately not
# inferred here.
_ATTACHMENT_HEADER = re.compile(
    r"^\s*(?:【\s*(?:붙임|별첨|별지|서식)\s*\d*\s*】|"
    r"\[\s*(?:붙임|별첨|별지|서식)\s*\d*\s*\]|"
    r"■\s*\[\s*서식\s*\d*)",
)


@dataclass(frozen=True)
class SectionCandidate:
    """A contiguous source range with a source-derived boundary."""

    section_id: str
    start_block_index: int
    end_block_index: int
    boundary_kind: str
    title_raw: str | None

    @property
    def source_block_ids(self) -> list[str]:
        return [f"body[{index}]" for index in range(self.start_block_index, self.end_block_index + 1)]


def is_attachment_header(item: dict[str, Any]) -> bool:
    """Return true only for an attachment/form document header block."""

    kind = item.get("kind")
    if kind in {"paragraph", "list_item"}:
        return bool(_ATTACHMENT_HEADER.match(item.get("text", "")))
    if kind != "table":
        return False

    # Some HWP/HWPX adapters preserve a form title only in the top-left table
    # cell (for example, ``■ [서식 1]``).  It is still a source-derived
    # document boundary, not an inference about the table's semantic value.
    first_cell = min(
        item.get("cells", []),
        key=lambda cell: (cell.get("row", 0), cell.get("col", 0)),
        default=None,
    )
    if first_cell is None:
        return False
    first_cell_text = " ".join(block.get("text", "") for block in first_cell.get("blocks", []))
    return bool(_ATTACHMENT_HEADER.match(first_cell_text))


def split_attachment_sections(body: list[dict[str, Any]]) -> list[SectionCandidate]:
    """Split a common-IR body into main notice plus attached-document ranges.

    Empty IR blocks remain part of their surrounding interval so that block
    indices stay canonical.  If no attachment header is found, only
    ``main_notice`` is emitted.
    """

    if not body:
        return []

    starts = [index for index, item in enumerate(body) if is_attachment_header(item)]
    if not starts:
        return [SectionCandidate("main_notice", 0, len(body) - 1, "document_start", None)]

    sections: list[SectionCandidate] = []
    if starts[0] > 0:
        sections.append(SectionCandidate("main_notice", 0, starts[0] - 1, "document_start", None))

    for ordinal, start in enumerate(starts, start=1):
        end = starts[ordinal] - 1 if ordinal < len(starts) else len(body) - 1
        sections.append(
            SectionCandidate(
                section_id=f"attachment_{ordinal}",
                start_block_index=start,
                end_block_index=end,
                boundary_kind="attachment_header",
                title_raw=_section_title(body[start]),
            )
        )
    return sections


def _section_title(item: dict[str, Any]) -> str | None:
    """Return the structural boundary label without flattening the whole table."""

    if item.get("kind") != "table":
        return item.get("text", "").strip() or None
    first_cell = min(
        item.get("cells", []),
        key=lambda cell: (cell.get("row", 0), cell.get("col", 0)),
        default=None,
    )
    if first_cell is None:
        return None
    return " ".join(block.get("text", "") for block in first_cell.get("blocks", [])).strip() or None


def block_section_ids(sections: list[SectionCandidate]) -> dict[str, str]:
    """Map every canonical body block id to exactly one section id."""

    mapping: dict[str, str] = {}
    for section in sections:
        for block_id in section.source_block_ids:
            if block_id in mapping:
                raise ValueError(f"overlapping section boundary for {block_id}")
            mapping[block_id] = section.section_id
    return mapping
