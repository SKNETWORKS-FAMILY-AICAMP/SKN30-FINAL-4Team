"""Deterministic line atoms projected from native multi-line source blocks."""

from __future__ import annotations

import hashlib
import re

from .models import (
    CandidatePack,
    NATIVE_EXACT_ATOMIC_BLOCK_KINDS,
    SourceBlock,
    SourceRelation,
)


_LINE = re.compile(r"[^\r\n]+")
# Fail rather than truncate: copied occurrence lists can otherwise turn one
# compact multi-line block into quadratic memory growth.
MAX_NATIVE_LINE_ATOMS = 10_000
MAX_NATIVE_LINE_PROVENANCE_REFERENCES = 100_000
MAX_NATIVE_LINE_TEXT_BYTES = 16 * 1024 * 1024


def build_native_line_atoms(pack: CandidatePack) -> list[SourceBlock]:
    """Expose non-empty native lines with reversible parent offsets.

    No whitespace normalization or text repair is applied.  The returned
    block IDs are a stable function of the parent ID and exact character
    interval, so repeating the transform cannot create a second identity.
    """

    atoms: list[SourceBlock] = []
    provenance_reference_count = 0
    derived_text_bytes = 0
    for parent in pack.blocks:
        if parent.source_spans or parent.native_parent_block_id is not None:
            continue
        if (
            parent.block_kind not in NATIVE_EXACT_ATOMIC_BLOCK_KINDS
            or parent.relation != SourceRelation.CANDIDATE
        ):
            continue
        if "\n" not in parent.text and "\r" not in parent.text:
            continue
        for match in _LINE.finditer(parent.text):
            raw = match.group(0)
            left = len(raw) - len(raw.lstrip())
            right = len(raw.rstrip())
            start, end = match.start() + left, match.start() + right
            if end <= start:
                continue
            if len(atoms) >= MAX_NATIVE_LINE_ATOMS:
                raise ValueError("native line atom limit exceeded")
            text = parent.text[start:end]
            derived_text_bytes += len(text.encode("utf-8"))
            if derived_text_bytes > MAX_NATIVE_LINE_TEXT_BYTES:
                raise ValueError("native line text byte limit exceeded")
            provenance_reference_count += len(parent.source_occurrence_ids)
            provenance_reference_count += len(parent.common_ir_occurrence_ids)
            if provenance_reference_count > MAX_NATIVE_LINE_PROVENANCE_REFERENCES:
                raise ValueError("native line provenance reference limit exceeded")
            identity = f"{parent.block_id}\0{start}\0{end}".encode()
            atoms.append(SourceBlock(
                block_id=f"line:{hashlib.sha256(identity).hexdigest()[:20]}",
                text=text,
                relation="candidate",
                block_kind="native_line_atom",
                section_id=parent.section_id,
                source_order=parent.source_order,
                source_occurrence_ids=list(parent.source_occurrence_ids),
                common_ir_block_id=parent.common_ir_block_id,
                common_ir_cell_id=parent.common_ir_cell_id,
                common_ir_occurrence_ids=parent.common_ir_occurrence_ids,
                native_parent_block_id=parent.block_id,
                native_start_char=start,
                native_end_char=end,
            ))
    return atoms


def augment_pack_with_native_line_atoms(pack: CandidatePack) -> CandidatePack:
    """Return ``pack`` plus deterministic native line atoms, if any."""

    atoms_by_parent: dict[str, list[SourceBlock]] = {}
    for atom in build_native_line_atoms(pack):
        atoms_by_parent.setdefault(atom.native_parent_block_id or "", []).append(atom)
    if not atoms_by_parent:
        return pack
    blocks: list[SourceBlock] = []
    for parent in pack.blocks:
        blocks.append(parent)
        blocks.extend(sorted(
            atoms_by_parent.get(parent.block_id, []),
            key=lambda atom: (atom.native_start_char or 0, atom.block_id),
        ))
    # Validate the graph without using that round-tripped instance as the
    # return value: Common-IR table geometry is an intentional PrivateAttr on
    # existing source blocks and must remain available to later trusted code.
    CandidatePack.model_validate({**pack.model_dump(mode="python"), "blocks": blocks})
    return pack.model_copy(update={"blocks": blocks})
