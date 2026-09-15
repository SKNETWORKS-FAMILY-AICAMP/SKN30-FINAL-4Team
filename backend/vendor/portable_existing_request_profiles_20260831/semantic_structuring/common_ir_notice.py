"""Prepare Common IR blocks for the existing A/B/C routing contract.

This first bridge preserves Common-IR block and occurrence IDs directly.  It
deliberately treats the supplied document as main-notice content; attachment
section scope will be connected once Common IR carries its section spans.
"""

from __future__ import annotations

from .common_ir_v03 import ProjectionResult, project_common_ir_v03
from .notice_preparation import PreparedNotice


def prepare_common_ir_notice(document: dict) -> tuple[PreparedNotice, ProjectionResult]:
    projection = project_common_ir_v03(document)
    fact_blocks = [block.model_copy(update={"section_id": "main_notice"}) for block in projection.blocks if block.block_kind != "table"]
    table_blocks = [block.model_copy(update={"section_id": "main_notice"}) for block in projection.blocks if block.block_kind == "table"]
    table_cell_blocks = [block.model_copy(update={"section_id": "main_notice"}) for block in projection.table_cell_blocks]
    return (
        PreparedNotice(
            notice_id=projection.notice_id,
            sections=[],
            fact_candidate_blocks=fact_blocks,
            table_candidate_blocks=table_blocks,
            table_cell_candidate_blocks=table_cell_blocks,
            search_only_blocks=[],
            excluded_blocks=[],
            section_scope_applied=False,
        ),
        projection,
    )
