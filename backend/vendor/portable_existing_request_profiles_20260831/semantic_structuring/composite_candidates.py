"""Deterministic, provenance-preserving composite source candidates.

This is intentionally a *candidate* generator, not a Profile assembler.  It
does not decide a business field and it never makes a new string by joining
source text.  A later, explicitly enabled consumer may use these candidates to
ask a model about a table value with its structural axes, or about a complete
proposition whose source parts are already explicitly ordered.

The input boundary is deliberately narrow:

* Common IR v1 supplies immutable occurrences and explicit table geometry.
* ``CandidatePack`` supplies the already-routed A scope.
* anything inferred, ambiguous, out of the A pack, or spanning Common-IR
  blocks is rejected with a machine-readable diagnostic.

There is no production integration in this module yet.  In particular, it
does not alter Source Selection, final Profile v0.2, DB contracts, or LLM
payloads.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import re
from typing import Any, Literal

from .common_ir_v1 import (
    COMMON_IR_V1_CANDIDATE_PACK_GENERATOR,
    COMMON_IR_V1_CANDIDATE_PACK_GENERATOR_VERSION,
)
from .models import CandidatePack, ExtractionScope, SourceBlock, SourceRelation


# Bump whenever the candidate identity material or fail-closed policy changes.
# These candidates are shadow-only at the moment, but the version is still
# deliberately part of the ID so a later consumer cannot confuse outputs from
# two different safety policies.
COMPOSITE_CANDIDATE_GENERATOR_VERSION = "2"

TABLE_AXIS_CONTEXT: Literal["table_axis_context"] = "table_axis_context"
COMPLETE_PROPOSITION: Literal["complete_proposition"] = "complete_proposition"
CompositeAtomRole = Literal[
    "primary_value",
    "row_header",
    "column_header",
    "complete_proposition",
    "complete_proposition_part",
]

# The generator must not call an unterminated label or a line fragment a
# complete proposition.  This is intentionally conservative; a future policy
# may introduce other sentence-boundary grammars with a version bump.
_SENTENCE_END = re.compile(r"[.!?。！？]\s*$")
_PROPOSITION_INTERNAL_BOUNDARY = re.compile(r"\s")
_PROPOSITION_LEXICAL_CHAR = re.compile(r"[A-Za-z가-힣]")
_SHA256_HEX = re.compile(r"[0-9a-f]{64}")

# These are safety caps on a *shadow-only* candidate enumerator, not an
# invitation to construct a dense grid from spans.  A document exceeding one
# is rejected at that structural unit and remains available as atomic A
# evidence through the existing path.
MAX_TABLE_CELLS = 1_000
MAX_TABLE_OCCURRENCES = 4_000
MAX_PACK_BLOCKS = 10_000
MAX_COMPOSITE_ATOMS = 32
MAX_DOCUMENT_BLOCKS = 10_000
MAX_DOCUMENT_OCCURRENCES = 50_000
MAX_DOCUMENT_TABLES = 1_000
MAX_COMPOSITE_CANDIDATES = 10_000
MAX_DIAGNOSTICS = 1_024
_PROPOSITION_BLOCK_KINDS = frozenset({"paragraph", "heading"})

# A canary must distinguish a safely skipped local candidate from a broken
# document/pack boundary that makes the whole generation result unusable.
FATAL_COMPOSITE_DIAGNOSTIC_CODES = frozenset(
    {
        "COMMON_IR_V1_REQUIRED",
        "COMMON_IR_BLOCKS_INVALID",
        "COMMON_IR_BLOCK_INVALID",
        "COMMON_IR_BLOCK_ID_INVALID",
        "COMMON_IR_BLOCK_ID_DUPLICATED",
        "COMMON_IR_OCCURRENCES_INVALID",
        "COMMON_IR_OCCURRENCE_INVALID",
        "COMMON_IR_OCCURRENCE_ID_DUPLICATED",
        "TABLE_CELLS_INVALID",
        "DOCUMENT_BLOCK_CAP_EXCEEDED",
        "DOCUMENT_OCCURRENCE_CAP_EXCEEDED",
        "DOCUMENT_TABLE_CAP_EXCEEDED",
        "COMPOSITE_CANDIDATE_CAP_EXCEEDED",
        "CANDIDATE_PACK_LINEAGE_MISMATCH",
        "CANDIDATE_PACK_RESOURCE_CAP_EXCEEDED",
        "CANDIDATE_PACK_A_SCOPE_REQUIRED",
        "A_BLOCK_ID_DUPLICATED",
    }
)


@dataclass(frozen=True, slots=True)
class CompositeAtom:
    """One exact Common-IR occurrence interval.

    ``start_char``/``end_char`` are offsets in the immutable CandidatePack
    source block named by ``source_block_id``.  The Common-IR occurrence IDs
    retain the upstream provenance separately.  Consumers must retain the
    atoms individually; they must not write a joined value back as
    ``value_raw``.
    """

    role: CompositeAtomRole
    source_block_id: str
    common_ir_block_id: str
    common_ir_cell_id: str | None
    occurrence_id: str
    start_char: int
    end_char: int


@dataclass(frozen=True, slots=True)
class CompositeCandidate:
    """A source-only relationship between already exact evidence atoms."""

    candidate_id: str
    kind: Literal["table_axis_context", "complete_proposition"]
    atoms: tuple[CompositeAtom, ...]


@dataclass(frozen=True, slots=True)
class CompositeCandidateDiagnostic:
    """Why a potential composition was skipped without guessing."""

    code: str
    common_ir_block_id: str | None = None
    common_ir_cell_id: str | None = None
    source_block_id: str | None = None


@dataclass(frozen=True, slots=True)
class CompositeCandidateGeneration:
    """Pure result of one deterministic generation pass."""

    candidates: tuple[CompositeCandidate, ...]
    diagnostics: tuple[CompositeCandidateDiagnostic, ...]


@dataclass(frozen=True, slots=True)
class _Cell:
    table_id: str
    cell_id: str
    row: int
    row_span: int
    col: int
    col_span: int
    occurrence_ids: tuple[str, ...]

    @property
    def row_end(self) -> int:
        return self.row + self.row_span

    @property
    def col_end(self) -> int:
        return self.col + self.col_span

    def covers_rows_of(self, other: "_Cell") -> bool:
        return self.row <= other.row and self.row_end >= other.row_end

    def covers_cols_of(self, other: "_Cell") -> bool:
        return self.col <= other.col and self.col_end >= other.col_end


@dataclass(frozen=True, slots=True)
class _Occurrence:
    block_id: str
    cell_id: str | None
    occurrence_id: str
    text: str
    expected_source_block_kind: str


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _candidate_id(
    *,
    document_identity: dict[str, object],
    pack: CandidatePack,
    kind: str,
    atoms: tuple[CompositeAtom, ...],
    atom_source_text_hashes: tuple[str, ...],
) -> str:
    """Hash only stable lineage and exact provenance atoms.

    Table atoms have a deterministic role/geometry order before this function
    is called.  Proposition atoms retain their explicit source occurrence
    order, which is meaningful and must not be sorted.
    """

    if len(atoms) != len(atom_source_text_hashes):
        raise ValueError("candidate atom text hashes must align with atoms")
    payload = {
        "generator_version": COMPOSITE_CANDIDATE_GENERATOR_VERSION,
        "common_ir_identity": document_identity,
        "candidate_pack_id": pack.pack_id,
        "candidate_pack_generator": pack.generator,
        "candidate_pack_generator_version": pack.generator_version,
        "kind": kind,
        "atoms": [
            {
                "role": atom.role,
                "source_block_id": atom.source_block_id,
                "common_ir_block_id": atom.common_ir_block_id,
                "common_ir_cell_id": atom.common_ir_cell_id,
                "occurrence_id": atom.occurrence_id,
                "start_char": atom.start_char,
                "end_char": atom.end_char,
                # Exact source text participates in identity without leaking
                # that text into the candidate contract.
                "source_text_sha256": text_hash,
            }
            for atom, text_hash in zip(atoms, atom_source_text_hashes, strict=True)
        ],
    }
    return f"composite:{sha256(_canonical_json(payload)).hexdigest()}"


def _is_complete_single_occurrence(text: str) -> bool:
    """Conservative non-semantic boundary check for an isolated paragraph.

    A bare terminal fragment such as ``중소기업이다.`` may be the second half
    of a split layout paragraph.  It has no explicit Common-IR link to the
    preceding block, so it cannot become a composite on its own.  Requiring
    an internal whitespace boundary keeps ordinary complete Korean prose such
    as ``지원 대상은 중소기업이다.`` while failing closed for that layout split.
    Explicitly ordered parts inside one cell use the separate safe path below.
    """

    return bool(
        _SENTENCE_END.search(text)
        and _PROPOSITION_INTERNAL_BOUNDARY.search(text.strip())
        and len(_PROPOSITION_LEXICAL_CHAR.findall(text)) >= 6
    )


def _document_identity(document: dict[str, Any]) -> dict[str, object] | None:
    if document.get("schema_version") != "common_ir_v1":
        return None
    metadata = document.get("document")
    if not isinstance(metadata, dict):
        return None
    document_id = metadata.get("document_id")
    source_kind = metadata.get("source_kind")
    provenance = metadata.get("provenance")
    source_sha256 = (
        provenance.get("source_sha256") if isinstance(provenance, dict) else None
    )
    if (
        not isinstance(document_id, str)
        or not document_id
        or not isinstance(source_kind, str)
        or not source_kind
        or not isinstance(source_sha256, str)
        or _SHA256_HEX.fullmatch(source_sha256) is None
    ):
        return None
    # ``source_sha256`` identifies original file content.  The provenance
    # tuple identifies the Common-IR producer that interpreted it.  Both are
    # retained even when older fixtures omit producer fields (as ``None``).
    return {
        "schema_version": "common_ir_v1",
        "document_id": document_id,
        "source_kind": source_kind,
        "source_sha256": source_sha256,
        "common_ir_producer": {
            key: provenance.get(key) if isinstance(provenance.get(key), str) else None
            for key in ("generator", "generator_version", "parser", "parser_version")
        },
    }


def _diagnostic_sort_key(item: CompositeCandidateDiagnostic) -> tuple[str, str, str, str]:
    return (
        item.code,
        item.common_ir_block_id or "",
        item.common_ir_cell_id or "",
        item.source_block_id or "",
    )


def _diagnostic(
    diagnostics: list[CompositeCandidateDiagnostic], code: str, *, table_id: str | None = None,
    cell_id: str | None = None, source_block_id: str | None = None,
) -> None:
    item = CompositeCandidateDiagnostic(
        code=code,
        common_ir_block_id=table_id,
        common_ir_cell_id=cell_id,
        source_block_id=source_block_id,
    )
    if item in diagnostics:
        return
    if len(diagnostics) < MAX_DIAGNOSTICS:
        diagnostics.append(item)
        return

    # Do not append unbounded per-cell errors for a hostile or malformed
    # document.  Preserve a deterministic bounded sample plus one aggregate
    # marker, independent of traversal order.
    overflow = CompositeCandidateDiagnostic(code="DIAGNOSTIC_CAP_EXCEEDED")
    retained = set(diagnostics)
    retained.add(item)
    retained.add(overflow)
    sample_limit = max(0, MAX_DIAGNOSTICS - 1)
    sample = sorted(
        (entry for entry in retained if entry != overflow),
        key=_diagnostic_sort_key,
    )[:sample_limit]
    diagnostics[:] = [*sample, overflow]


def _document_within_resource_caps(
    document: dict[str, Any], diagnostics: list[CompositeCandidateDiagnostic]
) -> bool:
    """Reject oversized Common IR before indexing or materializing source text."""

    blocks = document.get("blocks")
    if not isinstance(blocks, list):
        _diagnostic(diagnostics, "COMMON_IR_BLOCKS_INVALID")
        return False
    if len(blocks) > MAX_DOCUMENT_BLOCKS:
        _diagnostic(diagnostics, "DOCUMENT_BLOCK_CAP_EXCEEDED")
        return False

    table_count = 0
    occurrence_count = 0
    candidate_upper_bound = 0
    seen_block_ids: set[str] = set()
    for block in blocks:
        if not isinstance(block, dict):
            _diagnostic(diagnostics, "COMMON_IR_BLOCK_INVALID")
            return False
        block_id = block.get("block_id")
        if not isinstance(block_id, str) or not block_id:
            _diagnostic(diagnostics, "COMMON_IR_BLOCK_ID_INVALID")
            return False
        if block_id in seen_block_ids:
            _diagnostic(diagnostics, "COMMON_IR_BLOCK_ID_DUPLICATED")
            return False
        seen_block_ids.add(block_id)
        raw_occurrences = block.get("occurrences")
        if not isinstance(raw_occurrences, list):
            _diagnostic(diagnostics, "COMMON_IR_OCCURRENCES_INVALID")
            return False
        seen_occurrence_ids: set[str] = set()
        for occurrence in raw_occurrences:
            if (
                not isinstance(occurrence, dict)
                or not isinstance(occurrence.get("occurrence_id"), str)
                or not occurrence["occurrence_id"]
                # Layout/geometry occurrences are valid without text.  If a
                # text member is present, however, it must be an exact string.
                or (
                    "text" in occurrence
                    and not isinstance(occurrence.get("text"), str)
                )
            ):
                _diagnostic(diagnostics, "COMMON_IR_OCCURRENCE_INVALID")
                return False
            occurrence_id = occurrence["occurrence_id"]
            if occurrence_id in seen_occurrence_ids:
                _diagnostic(diagnostics, "COMMON_IR_OCCURRENCE_ID_DUPLICATED")
                return False
            seen_occurrence_ids.add(occurrence_id)
        occurrence_count += len(raw_occurrences)
        if occurrence_count > MAX_DOCUMENT_OCCURRENCES:
            _diagnostic(diagnostics, "DOCUMENT_OCCURRENCE_CAP_EXCEEDED")
            return False
        # Each source occurrence can produce at most one proposition; each
        # cell can produce at most one table candidate.  This is intentionally
        # an upper bound, so we reject before materializing a huge result.
        candidate_upper_bound += len(raw_occurrences)
        if block.get("kind") == "table":
            table_count += 1
            if table_count > MAX_DOCUMENT_TABLES:
                _diagnostic(diagnostics, "DOCUMENT_TABLE_CAP_EXCEEDED")
                return False
            raw_cells = block.get("cells")
            if not isinstance(raw_cells, list):
                _diagnostic(diagnostics, "TABLE_CELLS_INVALID", table_id=block.get("block_id") if isinstance(block.get("block_id"), str) else None)
                return False
            candidate_upper_bound += len(raw_cells)
        if candidate_upper_bound > MAX_COMPOSITE_CANDIDATES:
            _diagnostic(diagnostics, "COMPOSITE_CANDIDATE_CAP_EXCEEDED")
            return False
    return True


def _read_explicit_tables(
    document: dict[str, Any], diagnostics: list[CompositeCandidateDiagnostic]
) -> tuple[dict[str, list[_Cell]], dict[tuple[str, str], _Occurrence]]:
    """Read only explicit, non-overlapping Common-IR table geometry."""

    cells_by_table: dict[str, list[_Cell]] = {}
    occurrences: dict[tuple[str, str], _Occurrence] = {}
    for block in document.get("blocks") or []:
        if not isinstance(block, dict):
            continue
        if block.get("kind") == "table_candidate":
            _diagnostic(
                diagnostics,
                "TABLE_GEOMETRY_NOT_EXPLICIT",
                table_id=(
                    block.get("block_id")
                    if isinstance(block.get("block_id"), str)
                    else None
                ),
            )
            continue
        if block.get("kind") != "table":
            continue
        table_id = block.get("block_id")
        if not isinstance(table_id, str) or not table_id:
            _diagnostic(diagnostics, "TABLE_ID_MISSING")
            continue
        if block.get("structure_status") != "explicit":
            _diagnostic(diagnostics, "TABLE_GEOMETRY_NOT_EXPLICIT", table_id=table_id)
            continue
        occurrence_text = {
            item.get("occurrence_id"): item.get("text")
            for item in block.get("occurrences") or []
            if isinstance(item, dict)
            and isinstance(item.get("occurrence_id"), str)
            and isinstance(item.get("text"), str)
        }
        table_text_occurrence_ids = block.get("text_occurrence_ids")
        if (
            not isinstance(table_text_occurrence_ids, list)
            or any(
                not isinstance(item, str) or item not in occurrence_text
                for item in table_text_occurrence_ids
            )
        ):
            _diagnostic(diagnostics, "TABLE_OCCURRENCE_OWNERSHIP_INVALID", table_id=table_id)
            continue
        raw_cells = block.get("cells")
        if not isinstance(raw_cells, list):
            _diagnostic(diagnostics, "TABLE_GEOMETRY_AMBIGUOUS", table_id=table_id)
            continue
        if len(raw_cells) > MAX_TABLE_CELLS or len(occurrence_text) > MAX_TABLE_OCCURRENCES:
            _diagnostic(diagnostics, "TABLE_RESOURCE_CAP_EXCEEDED", table_id=table_id)
            continue
        table_cells: list[_Cell] = []
        malformed = False
        seen_cell_occurrences: set[str] = set()
        for raw_cell in raw_cells:
            if not isinstance(raw_cell, dict):
                malformed = True
                break
            cell_id = raw_cell.get("cell_id")
            required_geometry = ("row_index", "col_index", "row_span", "col_span")
            if any(key not in raw_cell for key in required_geometry):
                malformed = True
                break
            geometry = tuple(raw_cell.get(key) for key in required_geometry)
            # ``bool`` is an ``int`` subclass and ``int(1.0)`` succeeds.  Both
            # are unacceptable geometry: accepting them would silently alter
            # an upstream structural claim.  Only JSON integer values pass.
            if any(type(value) is not int for value in geometry):
                malformed = True
                break
            row, col, row_span, col_span = geometry
            raw_occurrences = raw_cell.get("text_occurrence_ids") or []
            if (
                not isinstance(cell_id, str)
                or not cell_id
                or row < 0
                or col < 0
                or row_span < 1
                or col_span < 1
                or not isinstance(raw_occurrences, list)
                or any(
                    not isinstance(item, str)
                    or item not in occurrence_text
                    for item in raw_occurrences
                )
                or len(raw_occurrences) != len(set(raw_occurrences))
                or any(item in seen_cell_occurrences for item in raw_occurrences)
            ):
                malformed = True
                break
            seen_cell_occurrences.update(raw_occurrences)
            if any(item.cell_id == cell_id for item in table_cells):
                malformed = True
                break
            cell = _Cell(
                table_id=table_id,
                cell_id=cell_id,
                row=row,
                row_span=row_span,
                col=col,
                col_span=col_span,
                occurrence_ids=tuple(raw_occurrences),
            )
            table_cells.append(cell)
        # Overlapping rectangles make row/column ownership ambiguous even if
        # the upstream adapter called the table explicit.
        for index, left in enumerate(table_cells):
            for right in table_cells[index + 1 :]:
                if left.row == right.row and left.col == right.col:
                    malformed = True
                    break
                overlaps = (
                    left.row < right.row_end
                    and right.row < left.row_end
                    and left.col < right.col_end
                    and right.col < left.col_end
                )
                if overlaps:
                    malformed = True
                    break
            if malformed:
                break
        if malformed:
            _diagnostic(diagnostics, "TABLE_GEOMETRY_AMBIGUOUS", table_id=table_id)
            continue
        for cell in table_cells:
            for occurrence_id in cell.occurrence_ids:
                occurrences[(table_id, occurrence_id)] = _Occurrence(
                    block_id=table_id,
                    cell_id=cell.cell_id,
                    occurrence_id=occurrence_id,
                    text=occurrence_text[occurrence_id],
                    expected_source_block_kind="table_cell",
                )
        cells_by_table[table_id] = table_cells
    return cells_by_table, occurrences


def _read_all_occurrences(document: dict[str, Any]) -> dict[tuple[str, str], _Occurrence]:
    """Index all source occurrences, including non-table paragraph blocks."""

    result: dict[tuple[str, str], _Occurrence] = {}
    for block in document.get("blocks") or []:
        if not isinstance(block, dict):
            continue
        block_id = block.get("block_id")
        block_kind = block.get("kind")
        if not isinstance(block_id, str) or not isinstance(block_kind, str):
            continue
        for item in block.get("occurrences") or []:
            if not isinstance(item, dict):
                continue
            occurrence_id, text = item.get("occurrence_id"), item.get("text")
            if isinstance(occurrence_id, str) and isinstance(text, str):
                result[(block_id, occurrence_id)] = _Occurrence(
                    block_id=block_id,
                    cell_id=None,
                    occurrence_id=occurrence_id,
                    text=text,
                    expected_source_block_kind=block_kind,
                )
    return result


def _a_occurrences(
    pack: CandidatePack,
    all_occurrences: dict[tuple[str, str], _Occurrence],
    diagnostics: list[CompositeCandidateDiagnostic],
) -> dict[tuple[str, str, str | None], SourceBlock]:
    """Return one verified A-pack source block per immutable occurrence."""

    result: dict[tuple[str, str, str | None], SourceBlock] = {}
    blocked_duplicates: set[tuple[str, str, str | None]] = set()
    if len(pack.blocks) > MAX_PACK_BLOCKS:
        _diagnostic(diagnostics, "CANDIDATE_PACK_RESOURCE_CAP_EXCEEDED")
        return {}
    block_ids = [source.block_id for source in pack.blocks]
    if len(block_ids) != len(set(block_ids)):
        _diagnostic(diagnostics, "A_BLOCK_ID_DUPLICATED")
        return {}
    for source in pack.blocks:
        common_block_id = source.common_ir_block_id
        occurrence_ids = tuple(source.common_ir_occurrence_ids)
        if common_block_id is None or len(occurrence_ids) != 1:
            _diagnostic(diagnostics, "A_BLOCK_OCCURRENCE_LINEAGE_AMBIGUOUS", source_block_id=source.block_id)
            continue
        occurrence_id = occurrence_ids[0]
        source_occurrence = all_occurrences.get((common_block_id, occurrence_id))
        if source_occurrence is None or source.text != source_occurrence.text.strip():
            _diagnostic(diagnostics, "A_BLOCK_NOT_EXACT_COMMON_IR_OCCURRENCE", source_block_id=source.block_id)
            continue
        if source.block_kind != source_occurrence.expected_source_block_kind:
            _diagnostic(
                diagnostics,
                "A_BLOCK_KIND_LINEAGE_MISMATCH",
                source_block_id=source.block_id,
            )
            continue
        if source.common_ir_cell_id != source_occurrence.cell_id:
            _diagnostic(diagnostics, "A_BLOCK_CELL_LINEAGE_MISMATCH", source_block_id=source.block_id)
            continue
        key = (common_block_id, occurrence_id, source.common_ir_cell_id)
        if key in blocked_duplicates:
            continue
        if key in result:
            # Neither duplicate is privileged by CandidatePack array order.
            # Remove the first and block the immutable occurrence altogether.
            result.pop(key)
            blocked_duplicates.add(key)
            _diagnostic(diagnostics, "A_BLOCK_OCCURRENCE_DUPLICATED")
            continue
        result[key] = source
    return result


def _atom(role: CompositeAtomRole, source: SourceBlock, occurrence: _Occurrence) -> CompositeAtom:
    return CompositeAtom(
        role=role,
        source_block_id=source.block_id,
        common_ir_block_id=occurrence.block_id,
        common_ir_cell_id=occurrence.cell_id,
        occurrence_id=occurrence.occurrence_id,
        start_char=0,
        # ``project_common_ir_v1`` trims a single occurrence when it creates
        # the CandidatePack source block.  v0.2 value_source coordinates are
        # defined against that exact block text, not the untrimmed occurrence
        # payload, so use the verified source-block length here.
        end_char=len(source.text),
    )


def _atom_source_text_hashes(
    atoms: tuple[CompositeAtom, ...],
    a_blocks: dict[tuple[str, str, str | None], SourceBlock],
) -> tuple[str, ...]:
    """Return text fingerprints aligned to exact atom provenance.

    A CandidatePack block was already checked against the immutable Common IR
    occurrence.  Hashing its text here makes an ID sensitive to source-content
    changes without adding the raw text to the candidate result.
    """

    hashes: list[str] = []
    for atom in atoms:
        source = a_blocks.get(
            (atom.common_ir_block_id, atom.occurrence_id, atom.common_ir_cell_id)
        )
        if source is None:
            raise ValueError("candidate atom has no verified A-pack source")
        hashes.append(sha256(source.text.encode("utf-8")).hexdigest())
    return tuple(hashes)


def _ordered_cell_atoms(
    *, cell: _Cell, role: CompositeAtomRole, a_blocks: dict[tuple[str, str, str | None], SourceBlock],
    occurrences: dict[tuple[str, str], _Occurrence], diagnostics: list[CompositeCandidateDiagnostic],
) -> tuple[CompositeAtom, ...] | None:
    atoms: list[CompositeAtom] = []
    for occurrence_id in cell.occurrence_ids:
        occurrence = occurrences.get((cell.table_id, occurrence_id))
        source = a_blocks.get((cell.table_id, occurrence_id, cell.cell_id))
        if occurrence is None or source is None:
            _diagnostic(
                diagnostics,
                "TABLE_CONTEXT_NOT_ROUTED_A",
                table_id=cell.table_id,
                cell_id=cell.cell_id,
            )
            return None
        if occurrence.text.strip():
            atoms.append(_atom(role, source, occurrence))
    return tuple(atoms)


def _axis_atoms(
    *,
    cells: tuple[_Cell, ...],
    role: CompositeAtomRole,
    a_blocks: dict[tuple[str, str, str | None], SourceBlock],
    occurrences: dict[tuple[str, str], _Occurrence],
    diagnostics: list[CompositeCandidateDiagnostic],
) -> tuple[CompositeAtom, ...] | None:
    """Materialize a complete structural axis or reject the whole axis."""

    atoms: list[CompositeAtom] = []
    for cell in cells:
        cell_atoms = _ordered_cell_atoms(
            cell=cell,
            role=role,
            a_blocks=a_blocks,
            occurrences=occurrences,
            diagnostics=diagnostics,
        )
        if not cell_atoms:
            return None
        atoms.extend(cell_atoms)
    return tuple(atoms)


def _header_bands(
    cells: list[_Cell], diagnostics: list[CompositeCandidateDiagnostic]
) -> tuple[int, int] | None:
    """Return a conservative top/left structural-axis *hypothesis*.

    Common IR v1 has explicit cell rectangles but no semantic ``is_header``
    flag.  In shadow mode only, the unique cell owning the top-left coordinate
    supplies a candidate boundary through its row/column spans.  This is not a
    semantic header assertion and must not be activated as Profile evidence
    without Gold evaluation or an upstream explicit header-region contract.
    We deliberately do *not* walk from a value towards the table edge because
    that would treat earlier data rows/cells as headers for later data rows.
    """

    corners = [
        cell
        for cell in cells
        if cell.row <= 0 < cell.row_end and cell.col <= 0 < cell.col_end
    ]
    if len(corners) != 1 or corners[0].row != 0 or corners[0].col != 0:
        table_id = cells[0].table_id if cells else None
        _diagnostic(diagnostics, "TABLE_HEADER_BANDS_AMBIGUOUS", table_id=table_id)
        return None
    corner = corners[0]
    return corner.row_end, corner.col_end


def _axis_cells(
    *,
    target: _Cell,
    cells: list[_Cell],
    header_rows: int,
    header_cols: int,
    axis: Literal["row", "column"],
    diagnostics: list[CompositeCandidateDiagnostic],
) -> tuple[_Cell, ...] | None:
    """Select cells wholly inside the shadow top/left axis hypothesis."""

    if axis == "row":
        selected = [
            cell
            for cell in cells
            if cell.col >= 0
            and cell.col_end <= header_cols
            and cell.covers_rows_of(target)
        ]
        partial = any(
            cell.col >= 0
            and cell.col_end <= header_cols
            and cell.row < target.row_end
            and target.row < cell.row_end
            and not cell.covers_rows_of(target)
            for cell in cells
        )
        selected.sort(key=lambda cell: (cell.col, cell.row, cell.cell_id))
    else:
        selected = [
            cell
            for cell in cells
            if cell.row >= 0
            and cell.row_end <= header_rows
            and cell.covers_cols_of(target)
        ]
        partial = any(
            cell.row >= 0
            and cell.row_end <= header_rows
            and cell.col < target.col_end
            and target.col < cell.col_end
            and not cell.covers_cols_of(target)
            for cell in cells
        )
        selected.sort(key=lambda cell: (cell.row, cell.col, cell.cell_id))
    if partial or not selected:
        _diagnostic(
            diagnostics,
            "TABLE_AXIS_OWNERSHIP_AMBIGUOUS",
            table_id=target.table_id,
            cell_id=target.cell_id,
        )
        return None
    return tuple(selected)


def _table_axis_candidates(
    *, identity: dict[str, object], pack: CandidatePack, cells_by_table: dict[str, list[_Cell]],
    a_blocks: dict[tuple[str, str, str | None], SourceBlock], table_occurrences: dict[tuple[str, str], _Occurrence],
    diagnostics: list[CompositeCandidateDiagnostic],
) -> list[CompositeCandidate]:
    result: list[CompositeCandidate] = []
    for table_id, cells in sorted(cells_by_table.items()):
        bands = _header_bands(cells, diagnostics)
        if bands is None:
            continue
        header_rows, header_cols = bands
        for target in sorted(cells, key=lambda item: (item.row, item.col, item.cell_id)):
            # Cells inside either structural-axis band are context, not value
            # candidates.  This is what prevents an earlier data value from
            # becoming a pseudo-header for a later row.
            if target.row < header_rows or target.col < header_cols:
                continue
            primary_atoms = _ordered_cell_atoms(
                cell=target, role="primary_value", a_blocks=a_blocks,
                occurrences=table_occurrences, diagnostics=diagnostics,
            )
            if not primary_atoms:
                continue
            row_chain = _axis_cells(
                target=target,
                cells=cells,
                header_rows=header_rows,
                header_cols=header_cols,
                axis="row",
                diagnostics=diagnostics,
            )
            column_chain = _axis_cells(
                target=target,
                cells=cells,
                header_rows=header_rows,
                header_cols=header_cols,
                axis="column",
                diagnostics=diagnostics,
            )
            if row_chain is None or column_chain is None:
                continue
            row_atoms = _axis_atoms(
                cells=row_chain,
                role="row_header",
                a_blocks=a_blocks,
                occurrences=table_occurrences,
                diagnostics=diagnostics,
            )
            column_atoms = _axis_atoms(
                cells=column_chain,
                role="column_header",
                a_blocks=a_blocks,
                occurrences=table_occurrences,
                diagnostics=diagnostics,
            )
            if not row_atoms or not column_atoms:
                continue
            atoms = primary_atoms + row_atoms + column_atoms
            if len(atoms) > MAX_COMPOSITE_ATOMS:
                _diagnostic(
                    diagnostics, "COMPOSITE_ATOM_CAP_EXCEEDED",
                    table_id=target.table_id, cell_id=target.cell_id,
                )
                continue
            result.append(CompositeCandidate(
                candidate_id=_candidate_id(
                    document_identity=identity,
                    pack=pack,
                    kind=TABLE_AXIS_CONTEXT,
                    atoms=atoms,
                    atom_source_text_hashes=_atom_source_text_hashes(atoms, a_blocks),
                ),
                kind=TABLE_AXIS_CONTEXT,
                atoms=atoms,
            ))
    return result


def _complete_proposition_candidates(
    *, identity: dict[str, object], pack: CandidatePack,
    all_occurrences: dict[tuple[str, str], _Occurrence], cells_by_table: dict[str, list[_Cell]],
    a_blocks: dict[tuple[str, str, str | None], SourceBlock], diagnostics: list[CompositeCandidateDiagnostic],
) -> list[CompositeCandidate]:
    result: list[CompositeCandidate] = []
    table_cell_ids = {cell.cell_id for cells in cells_by_table.values() for cell in cells}

    # A paragraph (or other non-table block) has a proposition only if its A
    # block maps exactly to one immutable occurrence and that occurrence ends
    # in an explicit sentence terminator.  Adjacent CandidatePack blocks are
    # never joined.
    # Iterate only the lineage/text-verified A occurrence index.  Walking the
    # raw pack here would let a tampered paragraph or a pack rejected by the
    # resource cap bypass ``_a_occurrences`` even though table candidates use
    # that trusted boundary.
    for source in sorted(a_blocks.values(), key=lambda item: item.block_id):
        if (
            source.common_ir_cell_id is not None
            or source.block_kind not in _PROPOSITION_BLOCK_KINDS
        ):
            continue
        if source.common_ir_block_id is None or len(source.common_ir_occurrence_ids) != 1:
            continue
        occurrence = all_occurrences.get((source.common_ir_block_id, source.common_ir_occurrence_ids[0]))
        if occurrence is None or occurrence.cell_id is not None:
            continue
        if not _is_complete_single_occurrence(occurrence.text):
            _diagnostic(diagnostics, "PROPOSITION_NOT_COMPLETE", source_block_id=source.block_id)
            continue
        atom = _atom("complete_proposition", source, occurrence)
        result.append(CompositeCandidate(
            candidate_id=_candidate_id(
                document_identity=identity,
                pack=pack,
                kind=COMPLETE_PROPOSITION,
                atoms=(atom,),
                atom_source_text_hashes=_atom_source_text_hashes((atom,), a_blocks),
            ),
            kind=COMPLETE_PROPOSITION,
            atoms=(atom,),
        ))

    # Several occurrence paragraphs in one *explicit* cell are the one
    # permitted multi-part proposition form.  Keep parts separately and in
    # Common-IR cell order; no synthesized text is ever emitted.
    for cells in cells_by_table.values():
        for cell in sorted(cells, key=lambda item: (item.row, item.col, item.cell_id)):
            if cell.cell_id not in table_cell_ids or len(cell.occurrence_ids) < 2:
                continue
            atoms = _ordered_cell_atoms(
                cell=cell,
                role="complete_proposition_part",
                a_blocks=a_blocks,
                occurrences=all_occurrences,
                diagnostics=diagnostics,
            )
            if not atoms:
                continue
            if len(atoms) > MAX_COMPOSITE_ATOMS:
                _diagnostic(
                    diagnostics, "COMPOSITE_ATOM_CAP_EXCEEDED",
                    table_id=cell.table_id, cell_id=cell.cell_id,
                )
                continue
            final_occurrence = all_occurrences.get((cell.table_id, cell.occurrence_ids[-1]))
            if final_occurrence is None or not _SENTENCE_END.search(final_occurrence.text):
                _diagnostic(
                    diagnostics, "PROPOSITION_NOT_COMPLETE",
                    table_id=cell.table_id, cell_id=cell.cell_id,
                )
                continue
            # A cell is only a multi-part proposition when all earlier parts
            # are visibly unfinished.  If one has already closed a sentence,
            # joining it to a later occurrence would invent a relationship.
            earlier_occurrences = [
                all_occurrences.get((cell.table_id, occurrence_id))
                for occurrence_id in cell.occurrence_ids[:-1]
            ]
            if any(
                occurrence is None or _SENTENCE_END.search(occurrence.text)
                for occurrence in earlier_occurrences
            ):
                _diagnostic(
                    diagnostics,
                    "PROPOSITION_PART_BOUNDARY_AMBIGUOUS",
                    table_id=cell.table_id,
                    cell_id=cell.cell_id,
                )
                continue
            result.append(CompositeCandidate(
                candidate_id=_candidate_id(
                    document_identity=identity,
                    pack=pack,
                    kind=COMPLETE_PROPOSITION,
                    atoms=atoms,
                    atom_source_text_hashes=_atom_source_text_hashes(atoms, a_blocks),
                ),
                kind=COMPLETE_PROPOSITION,
                atoms=atoms,
            ))
    return result


def generate_composite_candidates(
    common_ir_document: dict[str, Any], a_pack: CandidatePack
) -> CompositeCandidateGeneration:
    """Generate source-only composites for one routed Existing A pack.

    Invalid pack/document lineage returns no candidates rather than attempting
    to infer a source mapping.  This function performs no I/O and is safe for
    shadow-mode evaluation.
    """

    diagnostics: list[CompositeCandidateDiagnostic] = []
    identity = _document_identity(common_ir_document)
    if identity is None:
        _diagnostic(diagnostics, "COMMON_IR_V1_REQUIRED")
        return CompositeCandidateGeneration((), tuple(diagnostics))
    if not _document_within_resource_caps(common_ir_document, diagnostics):
        return CompositeCandidateGeneration((), tuple(sorted(diagnostics, key=_diagnostic_sort_key)))
    document_id = identity["document_id"]
    expected_notice_id = str(document_id).split(":", 1)[-1]
    if (
        a_pack.common_ir_document_id != document_id
        or a_pack.notice_id != expected_notice_id
        or a_pack.generator != COMMON_IR_V1_CANDIDATE_PACK_GENERATOR
        or a_pack.generator_version
        != COMMON_IR_V1_CANDIDATE_PACK_GENERATOR_VERSION
    ):
        _diagnostic(diagnostics, "CANDIDATE_PACK_LINEAGE_MISMATCH")
        return CompositeCandidateGeneration((), tuple(diagnostics))
    if (
        a_pack.extraction_scope != ExtractionScope.CANDIDATE_PACK
        or any(block.relation != SourceRelation.CANDIDATE for block in a_pack.blocks)
    ):
        _diagnostic(diagnostics, "CANDIDATE_PACK_A_SCOPE_REQUIRED")
        return CompositeCandidateGeneration((), tuple(diagnostics))

    cells_by_table, table_occurrences = _read_explicit_tables(
        common_ir_document, diagnostics
    )
    all_occurrences = _read_all_occurrences(common_ir_document)
    # Table occurrence records carry their cell id.  Non-table records do
    # not; use the table version when verifying table-cell A blocks.
    all_occurrences.update(table_occurrences)
    a_blocks = _a_occurrences(a_pack, all_occurrences, diagnostics)

    candidates = _table_axis_candidates(
        identity=identity,
        pack=a_pack,
        cells_by_table=cells_by_table,
        a_blocks=a_blocks,
        table_occurrences=table_occurrences,
        diagnostics=diagnostics,
    )
    candidates.extend(_complete_proposition_candidates(
        identity=identity,
        pack=a_pack,
        all_occurrences=all_occurrences,
        cells_by_table=cells_by_table,
        a_blocks=a_blocks,
        diagnostics=diagnostics,
    ))
    # A malformed table must not affect unrelated explicit tables.  IDs are
    # already deterministic; sorting makes the result stable if dict order
    # changes in a future adapter.
    return CompositeCandidateGeneration(
        candidates=tuple(sorted(candidates, key=lambda item: item.candidate_id)),
        diagnostics=tuple(
            sorted(
                diagnostics,
                key=_diagnostic_sort_key,
            )
        ),
    )


__all__ = [
    "COMPLETE_PROPOSITION",
    "COMPOSITE_CANDIDATE_GENERATOR_VERSION",
    "FATAL_COMPOSITE_DIAGNOSTIC_CODES",
    "TABLE_AXIS_CONTEXT",
    "CompositeAtom",
    "CompositeCandidate",
    "CompositeCandidateDiagnostic",
    "CompositeCandidateGeneration",
    "generate_composite_candidates",
]
