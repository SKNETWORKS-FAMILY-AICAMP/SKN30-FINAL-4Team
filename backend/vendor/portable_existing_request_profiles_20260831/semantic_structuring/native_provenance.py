"""Lossless serialization helpers for native exact CandidatePack blocks.

The native line/composite transforms are internal CandidatePack augmentations.
Their provenance must survive source selection and final-profile assembly, but
ordinary atomic blocks must retain their historical serialized shape exactly.
This module consequently returns an empty mapping for every atomic block.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .models import (
    CandidatePack,
    NATIVE_EXACT_ATOMIC_BLOCK_KINDS,
    NATIVE_EXACT_COMPOSITE_BLOCK_KINDS,
    NATIVE_EXACT_TRANSFORM_GENERATOR,
    SourceBlock,
    SourceRelation,
)


def native_block_provenance(
    pack: CandidatePack,
    block: SourceBlock,
    *,
    block_index: Mapping[str, SourceBlock] | None = None,
) -> dict[str, Any]:
    """Return only the durable native provenance applicable to ``block``.

    The CandidatePack model already validates this graph at its construction
    boundary.  Recheck the exact slices here nevertheless: persisted evidence
    is a security/audit boundary and must never serialize a stale or manually
    mutated ``model_copy`` graph.
    """

    if block.native_parent_block_id is None and (
        block.native_start_char is not None or block.native_end_char is not None
    ):
        raise ValueError("native line offsets require parent provenance")
    if block.block_kind == "native_line_atom" and block.native_parent_block_id is None:
        raise ValueError("native line atom requires parent provenance")
    if block.block_kind == "native_composite" and not block.source_spans:
        raise ValueError("native composite requires source span provenance")

    if block.native_parent_block_id is not None:
        if block.source_spans:
            raise ValueError("native line atom cannot serialize composite provenance")
        if (
            block.block_kind != "native_line_atom"
            or block.relation != SourceRelation.CANDIDATE
        ):
            raise ValueError("native line atom must use the native candidate contract")
        if (
            not isinstance(block.native_start_char, int)
            or isinstance(block.native_start_char, bool)
            or not isinstance(block.native_end_char, int)
            or isinstance(block.native_end_char, bool)
            or block.native_start_char < 0
        ):
            raise ValueError("native line atom requires exact parent offsets")
        parents = block_index or {
            candidate.block_id: candidate for candidate in pack.blocks
        }
        parent = parents.get(block.native_parent_block_id)
        if parent is None:
            raise ValueError("native line atom parent is not present in CandidatePack")
        if (
            parent.native_parent_block_id is not None
            or parent.source_spans
            or parent.block_kind not in NATIVE_EXACT_ATOMIC_BLOCK_KINDS
            or parent.relation != SourceRelation.CANDIDATE
            or block.native_end_char <= block.native_start_char
            or block.native_end_char > len(parent.text)
            or parent.text[block.native_start_char:block.native_end_char] != block.text
            or block.source_order != parent.source_order
            or block.source_occurrence_ids != parent.source_occurrence_ids
            or block.common_ir_block_id != parent.common_ir_block_id
            or block.common_ir_cell_id != parent.common_ir_cell_id
            or block.common_ir_occurrence_ids != parent.common_ir_occurrence_ids
            or block.section_id != parent.section_id
        ):
            raise ValueError("native line atom does not exactly match its parent span")
        return {
            "native_parent_span": {
                "source_block_id": parent.block_id,
                "start_char": block.native_start_char,
                "end_char": block.native_end_char,
                "exact_text": block.text,
            },
        }

    if block.source_spans:
        if (
            block.block_kind != "native_composite"
            or block.relation != SourceRelation.CANDIDATE
        ):
            raise ValueError("native composite must use the native candidate contract")
        by_id = block_index or {
            candidate.block_id: candidate for candidate in pack.blocks
        }
        spans = block.source_spans
        if len(spans) not in {2, 3}:
            raise ValueError("native composite requires two or three source spans")
        if spans[-1].separator_after:
            raise ValueError("final native composite span cannot have a trailing separator")
        if len({span.source_order for span in spans}) != len(spans) or [
            span.source_order for span in spans
        ] != sorted(span.source_order for span in spans):
            raise ValueError("native composite source spans must be uniquely ordered")
        if len({span.section_id for span in spans}) != 1:
            raise ValueError("native composite source spans must share a section")
        source_kinds: list[str | None] = []
        for span in spans:
            parent = by_id.get(span.source_block_id)
            if parent is None:
                raise ValueError("native composite source block is not present in CandidatePack")
            source_kinds.append(parent.block_kind)
            if (
                parent.source_spans
                or parent.native_parent_block_id is not None
                or parent.relation != SourceRelation.CANDIDATE
                or parent.block_kind not in NATIVE_EXACT_COMPOSITE_BLOCK_KINDS
                or not isinstance(span.start_char, int)
                or isinstance(span.start_char, bool)
                or not isinstance(span.end_char, int)
                or isinstance(span.end_char, bool)
                or span.start_char < 0
                or span.end_char <= span.start_char
                or span.end_char > len(parent.text)
                or parent.text[span.start_char:span.end_char] != span.exact_text
                or parent.source_order != span.source_order
                or parent.section_id != span.section_id
                or parent.common_ir_block_id != span.common_ir_block_id
                or parent.common_ir_cell_id != span.common_ir_cell_id
                or parent.common_ir_occurrence_ids != span.common_ir_occurrence_ids
            ):
                raise ValueError("native composite span does not exactly match its atomic block")
        if "heading" in source_kinds and any(
            kind != "heading" for kind in source_kinds
        ):
            raise ValueError("native composite cannot mix heading and body sources")
        occurrences = tuple(
            occurrence
            for span in spans
            for occurrence in span.common_ir_occurrence_ids
        )
        if (
            block.text != "".join(span.exact_text + span.separator_after for span in spans)
            or block.source_order != spans[0].source_order
            or block.section_id != spans[0].section_id
            or block.common_ir_block_id != spans[0].common_ir_block_id
            or block.common_ir_cell_id is not None
            or tuple(block.common_ir_occurrence_ids) != occurrences
            or tuple(block.source_occurrence_ids) != occurrences
        ):
            raise ValueError("native composite provenance does not exactly match its source spans")
        # Keep every NativeSourceSpan field, including the source order and
        # Common IR locators needed to reconstruct the composition verbatim.
        return {
            "source_spans": [
                span.model_dump(mode="json")
                for span in block.source_spans
            ],
        }
    return {}


def candidate_pack_block_index(pack: CandidatePack) -> dict[str, SourceBlock]:
    """Build one duplicate-safe block index for pack-level serialization."""

    index: dict[str, SourceBlock] = {}
    for block in pack.blocks:
        if block.block_id in index:
            raise ValueError("CandidatePack block ids must be unique for serialization")
        index[block.block_id] = block
    return index


def parent_candidate_pack_lineage(pack: CandidatePack) -> dict[str, str]:
    """Return transformed-pack origin fields using the model's wire names.

    Atomic CandidatePacks intentionally yield ``{}``, preserving legacy
    artifacts byte-for-byte.  CandidatePack's validator enforces the three
    values as all-or-none; retain that check here for callers accepting an
    object created through a non-validating Pydantic ``model_copy`` path.
    """

    values = (
        pack.parent_pack_id,
        pack.parent_generator,
        pack.parent_generator_version,
    )
    if all(value is None for value in values):
        if pack.generator == NATIVE_EXACT_TRANSFORM_GENERATOR or any(
            block.source_spans or block.native_parent_block_id is not None
            for block in pack.blocks
        ):
            raise ValueError("native transformed CandidatePack requires parent lineage")
        return {}
    if any(
        not isinstance(value, str) or not value
        for value in values
    ):
        raise ValueError("transformed CandidatePack requires complete parent lineage")
    if pack.parent_pack_id == pack.pack_id:
        raise ValueError("transformed CandidatePack parent must differ from pack identity")
    if (
        pack.generator == pack.parent_generator
        and pack.generator_version == pack.parent_generator_version
    ):
        raise ValueError("transformed CandidatePack generator must differ from parent")
    return {
        "parent_pack_id": pack.parent_pack_id,
        "parent_generator": pack.parent_generator,
        "parent_generator_version": pack.parent_generator_version,
    }
