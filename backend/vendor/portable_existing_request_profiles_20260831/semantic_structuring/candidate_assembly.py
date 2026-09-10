"""Build source-preserving A-field candidate packs from broad router output."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import re

from .models import CandidatePack
from .notice_preparation import PreparedNotice


# These are processing routes, not final schema fields.  ``table`` remains on
# the B table path and ``application_operation`` is intentionally out of the A
# comparison profile.
_A_ROUTES = (
    "purpose",
    "target_or_restriction",
    "support",
    "funding_or_condition",
    "delivery",
)
_TABLE_DISPOSITIONS = {"a_fact_candidate", "b_search_only", "c_exclude"}
_CELL_ID = re.compile(r"r(\d+)c(\d+)p(\d+)$")
MAX_A_TABLE_CELL_CANDIDATES = 300


def _common_ir_pack_lineage(prepared) -> dict[str, str]:
    """Return Common IR CandidatePack lineage, or nothing for legacy input."""

    document_id = getattr(prepared, "common_ir_document_id", None)
    if document_id is None:
        return {}
    return {
        "generator": prepared.candidate_pack_generator,
        "generator_version": prepared.candidate_pack_generator_version,
        "common_ir_document_id": document_id,
    }


def table_parent_id(block_id: str) -> str:
    return block_id.split("#", 1)[0]


def restrict_a_pack_to_table_ids(pack: CandidatePack, table_ids: Iterable[str]) -> CandidatePack:
    """Return a deterministic table-only extraction pack.

    This is the bounded path for catalogue-style notices: every selected
    block remains a real Common-IR table-cell candidate, while unrelated
    prose and other tables cannot dilute a component-heavy extraction.  It is
    intentionally a *selection* input only; a later server merge combines
    the resulting table selections with the separately extracted notice
    facts.
    """

    requested = tuple(sorted(set(table_ids)))
    if not requested:
        raise ValueError("table-scoped A pack requires at least one table id")
    requested_set = set(requested)
    blocks = [block for block in pack.blocks if table_parent_id(block.block_id) in requested_set]
    found = {table_parent_id(block.block_id) for block in blocks}
    missing = requested_set - found
    if missing:
        raise ValueError(f"requested table ids are not present in the A candidate pack: {sorted(missing)}")
    table_suffix = "-".join(table_id.replace(":", "-") for table_id in requested)
    return CandidatePack(
        pack_id=f"{pack.pack_id}-table-{table_suffix}",
        notice_id=pack.notice_id,
        extraction_scope="candidate_pack",
        question="Table-scoped A comparison-profile extraction only; never sent to the model.",
        blocks=blocks,
        generator=pack.generator,
        generator_version=pack.generator_version,
        common_ir_document_id=pack.common_ir_document_id,
    )


def restrict_a_pack_to_block_ids(pack: CandidatePack, block_ids: Iterable[str]) -> CandidatePack:
    """Return a deterministic source-block-scoped extraction pack.

    Used only after the router/section stage has identified a coherent notice
    region.  It does not rewrite text or synthesize a value; it merely keeps
    selected original CandidatePack blocks so long notices can be extracted in
    independently validated slices.
    """

    requested = tuple(dict.fromkeys(block_ids))
    if not requested:
        raise ValueError("block-scoped A pack requires at least one block id")
    by_id = {block.block_id: block for block in pack.blocks}
    missing = set(requested) - set(by_id)
    if missing:
        raise ValueError(f"requested block ids are not present in the A candidate pack: {sorted(missing)}")
    return CandidatePack(
        pack_id=f"{pack.pack_id}-block-scope-v1",
        notice_id=pack.notice_id,
        extraction_scope="candidate_pack",
        question="Source-block-scoped A comparison-profile extraction only; never sent to the model.",
        blocks=[by_id[block_id] for block_id in requested],
        generator=pack.generator,
        generator_version=pack.generator_version,
        common_ir_document_id=pack.common_ir_document_id,
    )


def build_routed_a_pack(prepared: PreparedNotice, router_candidates: list[dict]) -> tuple[CandidatePack | None, dict]:
    """Build the only approved A pack: routed prose plus A-classified table cells."""

    if not prepared.section_scope_applied:
        raise ValueError("section scope has not been applied; Common IR bridge is inspection-only")

    by_id: dict[str, dict] = {}
    for item in router_candidates:
        block_id = item["source_block_id"]
        if block_id in by_id:
            raise ValueError(f"router returned duplicate source block id: {block_id}")
        by_id[block_id] = item
    expected = {block.block_id for block in prepared.router_pack().blocks}
    missing = expected - set(by_id)
    unknown = set(by_id) - expected
    if missing or unknown:
        raise ValueError(f"router coverage mismatch: missing={sorted(missing)} unknown={sorted(unknown)}")
    # An adapter may retain attachment tables in B/search-only while omitting
    # them from the A router by policy.  Only tables present in the completed
    # router pack require a table disposition here.
    routable_tables = [block for block in prepared.table_candidate_blocks if block.block_id in expected]
    for block in routable_tables:
        item = by_id[block.block_id]
        disposition = item.get("table_disposition")
        if disposition not in _TABLE_DISPOSITIONS:
            raise ValueError(f"router table disposition invalid or missing: {block.block_id}")
    routes = {block_id: item["route_tags"] for block_id, item in by_id.items()}
    requested_table_a_ids = {block_id for block_id, item in by_id.items() if item.get("table_disposition") == "a_fact_candidate"}
    cell_parent_ids = {table_parent_id(block.block_id) for block in prepared.table_cell_candidate_blocks}
    cell_counts = {parent: sum(1 for block in prepared.table_cell_candidate_blocks if table_parent_id(block.block_id) == parent) for parent in requested_table_a_ids & cell_parent_ids}
    # Cell-less or oversized tables remain B rather than aborting the whole
    # notice.  This is a deterministic product-policy override, not a second
    # LLM judgement; the router artifact still records its original decision.
    table_a_ids: set[str] = set()
    downgraded_a_tables = sorted(requested_table_a_ids - cell_parent_ids)
    used_cells = 0
    for table_id in sorted(requested_table_a_ids & cell_parent_ids):
        if used_cells + cell_counts[table_id] > MAX_A_TABLE_CELL_CANDIDATES:
            downgraded_a_tables.append(table_id)
            continue
        table_a_ids.add(table_id)
        used_cells += cell_counts[table_id]
    table_cells = [block for block in prepared.table_cell_candidate_blocks if table_parent_id(block.block_id) in table_a_ids]
    prose_packs = build_a_candidate_packs(prepared, routes)
    # A notice can expose its only comparison fact in a concise table.  Keep
    # that valid case without reintroducing the flattened table block.
    if not prose_packs and table_cells:
        prose_packs = {
            "table_fact": CandidatePack(
                pack_id=f"{prepared.notice_id}-table-fact-v0.2",
                notice_id=prepared.notice_id,
                extraction_scope="candidate_pack",
                question="A table-cell extraction only; never sent to the model.",
                # CandidatePack requires a non-empty source list.  The first
                # cell is deduplicated when the full table-cell set is added.
                blocks=[table_cells[0]],
                **_common_ir_pack_lineage(prepared),
            )
        }
    pack = build_combined_a_candidate_pack(
        prose_packs,
        table_cell_blocks=table_cells,
        include_whole_table_blocks=False,
    )
    return pack, {"a_table_ids": sorted(table_a_ids), "a_table_cell_counts": {key: cell_counts[key] for key in table_a_ids}, "a_table_cell_total": len(table_cells), "downgraded_to_b_table_ids": downgraded_a_tables}


def build_a_candidate_packs(
    prepared: PreparedNotice,
    route_tags_by_block: Mapping[str, Iterable[str]],
) -> dict[str, CandidatePack]:
    """Partition routed main-notice blocks for later field extraction.

    This function has no LLM call and does not create values, quotes, or
    summaries.  Only source blocks eligible for A facts can enter these packs;
    B attachment prose, C forms, application-operation blocks, and tables are
    excluded by construction.
    """

    available = {block.block_id: block for block in prepared.fact_candidate_blocks}
    unknown = set(route_tags_by_block) - set(available) - {block.block_id for block in prepared.table_candidate_blocks}
    if unknown:
        raise ValueError(f"router returned unknown source blocks: {sorted(unknown)}")

    result: dict[str, CandidatePack] = {}
    for route in _A_ROUTES:
        blocks = [
            block
            for block in prepared.fact_candidate_blocks
            if route in set(route_tags_by_block.get(block.block_id, ()))
        ]
        if not blocks:
            continue
        result[route] = CandidatePack(
            pack_id=f"{prepared.notice_id}-{route}-v0.2",
            notice_id=prepared.notice_id,
            extraction_scope="candidate_pack",
            question=f"{route} route only; never sent to the model.",
            blocks=blocks,
            **_common_ir_pack_lineage(prepared),
        )
    return result


def build_combined_a_candidate_pack(
    packs: Mapping[str, CandidatePack], *, component_table_blocks: Iterable = (), table_cell_blocks: Iterable = (), include_whole_table_blocks: bool = False
) -> CandidatePack | None:
    """Make one deduplicated A-extraction input, not one model call per route.

    Broad route packs are retained for diagnostics and future specialist
    extractors.  The normal first-pass extractor receives this union once, so
    a block carrying multiple route tags is never charged or interpreted
    multiple times.
    """

    if not packs:
        return None
    exemplar = next(iter(packs.values()))
    lineages = {
        (pack.generator, pack.generator_version, pack.common_ir_document_id)
        for pack in packs.values()
    }
    if len(lineages) != 1:
        raise ValueError("candidate packs with different Common IR lineage cannot be combined")
    by_id = {block.block_id: block for pack in packs.values() for block in pack.blocks}
    # Values use cell-paragraph candidates so a fact is never the flattened
    # entire table.  Batch extraction omits whole-table blocks as well: sending
    # both representations duplicates large table text and explodes token cost.
    if include_whole_table_blocks:
        by_id.update({block.block_id: block for block in component_table_blocks})
    by_id.update({block.block_id: block for block in table_cell_blocks})

    def order(block):
        body, _, cell = block.block_id.partition("#")
        base_order = block.source_order
        if base_order is None:
            try:
                base_order = int(body.removeprefix("body[").removesuffix("]"))
            except ValueError:
                base_order = 0
        if not cell:
            return (base_order, 0, 0, 0, 0)
        match = _CELL_ID.fullmatch(cell)
        if match:
            row, col, paragraph = (int(value) for value in match.groups())
            return (base_order, 1, row, col, paragraph)
        return (base_order, 2, 0, 0, 0)

    blocks = sorted(by_id.values(), key=order)
    return CandidatePack(
        pack_id=f"{exemplar.notice_id}-a-profile-v0.2",
        notice_id=exemplar.notice_id,
        extraction_scope="candidate_pack",
        question="A comparison-profile extraction only; never sent to the model.",
        blocks=blocks,
        **(
            {
                "generator": exemplar.generator,
                "generator_version": exemplar.generator_version,
                "common_ir_document_id": exemplar.common_ir_document_id,
            }
            if exemplar.common_ir_document_id is not None
            else {}
        ),
    )
