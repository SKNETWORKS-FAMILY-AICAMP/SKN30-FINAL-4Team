"""Focused shadow-mode tests for Existing Profile composite candidates.

The fixtures are synthetic on purpose.  They exercise source geometry and
exact occurrence provenance only; no Gold notice text, model, DB, or Profile
schema is involved.
"""

from __future__ import annotations

from copy import deepcopy
import random

import pytest

from worker import vendor  # noqa: F401 - installs the vendored contract paths

from semantic_structuring import composite_candidates
from semantic_structuring.common_ir_v1 import project_common_ir_v1
from semantic_structuring.composite_candidates import (
    COMPLETE_PROPOSITION,
    TABLE_AXIS_CONTEXT,
    generate_composite_candidates,
)
from semantic_structuring.models import CandidatePack, ExtractionScope


def _paragraph(block_id: str, text: str, order: int) -> dict:
    occurrence_id = f"{block_id}:occ:0"
    return {
        "block_id": block_id,
        "kind": "paragraph",
        "structure_status": "explicit",
        "text": text,
        "text_occurrence_ids": [occurrence_id],
        "reading_order": order,
        "page": None,
        "section_path": "",
        "occurrences": [{"occurrence_id": occurrence_id, "text": text}],
        "boundary_markers": [],
        "provenance": {},
    }


def _table(block_id: str, rows: list[list[tuple[str, int, int]]], order: int) -> dict:
    """Make an explicit table from ``(text, row_span, col_span)`` cells.

    The caller supplies a rectangular starting-cell layout.  Tests that need
    malformed geometry mutate the result after construction.
    """

    cells: list[dict] = []
    occurrences: list[dict] = []
    occupied: set[tuple[int, int]] = set()
    for row_index, row in enumerate(rows):
        col_index = 0
        for text, row_span, col_span in row:
            while (row_index, col_index) in occupied:
                col_index += 1
            occurrence_id = f"{block_id}:occ:{len(occurrences)}"
            cell_id = f"{block_id}:c:{len(cells)}"
            cells.append({
                "cell_id": cell_id,
                "row_index": row_index,
                "col_index": col_index,
                "row_span": row_span,
                "col_span": col_span,
                "text_occurrence_ids": [occurrence_id],
            })
            occurrences.append({"occurrence_id": occurrence_id, "text": text})
            for covered_row in range(row_index, row_index + row_span):
                for covered_col in range(col_index, col_index + col_span):
                    occupied.add((covered_row, covered_col))
            col_index += col_span
    flattened_text = "\n".join(item["text"] for item in occurrences)
    return {
        "block_id": block_id,
        "kind": "table",
        "structure_status": "explicit",
        "text": flattened_text,
        # Production HWP/HWPX Common IR uses one flattened block occurrence
        # here.  Cell-local occurrences remain in ``occurrences`` and are
        # referenced only from their owning cell.
        "text_occurrence_ids": [f"{block_id}:occ:table"],
        "reading_order": order,
        "page": None,
        "section_path": "",
        "occurrences": [
            {"occurrence_id": f"{block_id}:occ:table", "text": flattened_text},
            *occurrences,
        ],
        "cells": cells,
        "boundary_markers": [],
        "provenance": {},
    }


def _document(*blocks: dict) -> dict:
    return {
        "schema_version": "common_ir_v1",
        "document": {
            "document_id": "hwpx:PBLN-composite-test",
            "source_kind": "hwpx",
            "provenance": {
                "source_sha256": "a" * 64,
                "generator": "synthetic_common_ir_adapter",
                "generator_version": "1.0",
                "parser": "synthetic",
                "parser_version": "1.0",
            },
        },
        "blocks": list(blocks),
        "relations": [],
        "conflicts": [],
    }


def _pack(document: dict, *, allow_duplicate_block_ids: bool = False) -> CandidatePack:
    projection = project_common_ir_v1(document)
    blocks = [
        *[block for block in projection.blocks if block.block_kind != "table"],
        *projection.table_cell_blocks,
    ]
    # An inferred table exposes no trusted cell blocks.  Keep its whole source
    # block only so the test can pass a non-empty, lineaged pack and prove the
    # composite generator itself refuses that geometry.
    if not blocks:
        blocks = projection.blocks
    payload = {
        "pack_id": "PBLN-composite-test-a-pack",
        "notice_id": "PBLN-composite-test",
        "extraction_scope": ExtractionScope.CANDIDATE_PACK,
        "question": "Synthetic routed A scope only",
        "blocks": blocks,
        "generator": "semantic_structuring.common_ir_v1",
        "generator_version": "1",
        "common_ir_document_id": "hwpx:PBLN-composite-test",
    }
    # These two downstream Common-IR defensive tests deliberately supply an
    # impossible document (colliding projected block IDs).  Normal production
    # construction now rejects it at CandidatePack validation; model_construct
    # here preserves the separate generator diagnostic coverage only.
    if allow_duplicate_block_ids:
        return CandidatePack.model_construct(**payload)
    return CandidatePack(**payload)


def _with_primary(generation, occurrence_id: str):
    return next(
        candidate
        for candidate in generation.candidates
        if candidate.kind == TABLE_AXIS_CONTEXT
        and candidate.atoms[0].occurrence_id == occurrence_id
    )


def test_simple_matrix_binds_exact_value_to_one_row_and_column_axis() -> None:
    table = _table(
        "hwpx:t0",
        [
            [("구분", 1, 1), ("지원 내용", 1, 1)],
            [("청년기업", 1, 1), ("컨설팅", 1, 1)],
        ],
        0,
    )
    document = _document(table)
    result = generate_composite_candidates(document, _pack(document))

    candidate = _with_primary(result, "hwpx:t0:occ:3")
    assert [(atom.role, atom.occurrence_id, atom.start_char, atom.end_char) for atom in candidate.atoms] == [
        ("primary_value", "hwpx:t0:occ:3", 0, 3),
        ("row_header", "hwpx:t0:occ:2", 0, 4),
        ("column_header", "hwpx:t0:occ:1", 0, 5),
    ]
    assert candidate.candidate_id.startswith("composite:")


def test_merged_headers_keep_all_explicit_axis_intervals_in_source_order() -> None:
    table = _table(
        "hwpx:t1",
        [
            [("구분", 2, 1), ("지원", 1, 2)],
            [("내용", 1, 1), ("금액", 1, 1)],
            [("A유형", 1, 1), ("컨설팅", 1, 1), ("100만원", 1, 1)],
        ],
        0,
    )
    document = _document(table)
    result = generate_composite_candidates(document, _pack(document))

    candidate = _with_primary(result, "hwpx:t1:occ:5")
    assert [(atom.role, atom.occurrence_id) for atom in candidate.atoms] == [
        ("primary_value", "hwpx:t1:occ:5"),
        ("row_header", "hwpx:t1:occ:4"),
        ("column_header", "hwpx:t1:occ:1"),
        ("column_header", "hwpx:t1:occ:2"),
    ]


def test_inferred_or_ambiguous_table_geometry_fails_closed() -> None:
    inferred = _table(
        "hwpx:t2",
        [[("구분", 1, 1), ("내용", 1, 1)], [("A", 1, 1), ("값", 1, 1)]],
        0,
    )
    inferred["structure_status"] = "inferred"
    inferred_document = _document(inferred)
    inferred_result = generate_composite_candidates(inferred_document, _pack(inferred_document))
    assert not inferred_result.candidates
    assert "TABLE_GEOMETRY_NOT_EXPLICIT" in {item.code for item in inferred_result.diagnostics}

    ambiguous = _table(
        "hwpx:t3",
        [[("구분", 1, 1), ("내용", 1, 1)], [("A", 1, 1), ("값", 1, 1)]],
        0,
    )
    # Explicit geometry that overlaps is still not usable.  This is not an
    # invitation to expand a grid or choose one visual owner heuristically.
    ambiguous["cells"][1]["col_index"] = 0
    ambiguous_document = _document(ambiguous)
    ambiguous_result = generate_composite_candidates(
        ambiguous_document, _pack(ambiguous_document, allow_duplicate_block_ids=True)
    )
    assert not ambiguous_result.candidates
    assert "TABLE_GEOMETRY_AMBIGUOUS" in {item.code for item in ambiguous_result.diagnostics}

    missing_coordinate = _table(
        "hwpx:t3-missing",
        [[("구분", 1, 1), ("내용", 1, 1)], [("A", 1, 1), ("값", 1, 1)]],
        0,
    )
    del missing_coordinate["cells"][0]["row_span"]
    missing_document = _document(missing_coordinate)
    missing_result = generate_composite_candidates(missing_document, _pack(missing_document))
    assert not missing_result.candidates
    assert "TABLE_GEOMETRY_AMBIGUOUS" in {item.code for item in missing_result.diagnostics}


def test_complete_paragraph_is_one_contiguous_exact_occurrence() -> None:
    document = _document(_paragraph("hwpx:p0", "지원 대상은 중소기업이다.", 0))
    result = generate_composite_candidates(document, _pack(document))

    candidate = next(item for item in result.candidates if item.kind == COMPLETE_PROPOSITION)
    assert [(atom.role, atom.occurrence_id, atom.start_char, atom.end_char) for atom in candidate.atoms] == [
        ("complete_proposition", "hwpx:p0:occ:0", 0, len("지원 대상은 중소기업이다.")),
    ]


def test_explicitly_ordered_same_cell_parts_are_not_joined() -> None:
    table = _table("hwpx:t4", [[("지원 대상은", 1, 1)]], 0)
    table["occurrences"].append({"occurrence_id": "hwpx:t4:occ:1", "text": "중소기업이다."})
    table["cells"][0]["text_occurrence_ids"].append("hwpx:t4:occ:1")
    table["text"] = "지원 대상은\n중소기업이다."
    table["occurrences"][0]["text"] = table["text"]
    document = _document(table)
    result = generate_composite_candidates(document, _pack(document))

    candidate = next(item for item in result.candidates if item.kind == COMPLETE_PROPOSITION)
    assert [(atom.role, atom.occurrence_id) for atom in candidate.atoms] == [
        ("complete_proposition_part", "hwpx:t4:occ:0"),
        ("complete_proposition_part", "hwpx:t4:occ:1"),
    ]
    assert all(atom.end_char > atom.start_char for atom in candidate.atoms)


def test_cross_block_fragments_never_become_a_composite_or_mutate_inputs() -> None:
    document = _document(
        _paragraph("hwpx:p1", "지원 대상은", 0),
        _paragraph("hwpx:p2", "중소기업이다.", 1),
    )
    pack = _pack(document)
    original_document = deepcopy(document)
    original_pack = pack.model_dump(mode="json")

    result = generate_composite_candidates(document, pack)

    assert result.candidates == ()
    assert document == original_document
    assert pack.model_dump(mode="json") == original_pack
    assert "PROPOSITION_NOT_COMPLETE" in {item.code for item in result.diagnostics}


def test_same_text_at_different_exact_occurrences_has_distinct_candidates() -> None:
    table = _table(
        "hwpx:t5",
        [
            [("구분", 1, 1), ("내용", 1, 1)],
            [("A", 1, 1), ("동일", 1, 1)],
            [("B", 1, 1), ("동일", 1, 1)],
        ],
        0,
    )
    document = _document(table)
    result = generate_composite_candidates(document, _pack(document))

    matches = [
        candidate
        for candidate in result.candidates
        if candidate.kind == TABLE_AXIS_CONTEXT
        and candidate.atoms[0].occurrence_id in {"hwpx:t5:occ:3", "hwpx:t5:occ:5"}
    ]
    assert len(matches) == 2
    assert matches[0].candidate_id != matches[1].candidate_id
    assert {candidate.atoms[0].occurrence_id for candidate in matches} == {
        "hwpx:t5:occ:3",
        "hwpx:t5:occ:5",
    }


def test_later_data_rows_never_become_axis_context() -> None:
    table = _table(
        "hwpx:t-data-rows",
        [
            [("구분", 1, 1), ("지원액", 1, 1)],
            [("기업 A", 1, 1), ("50만원", 1, 1)],
            [("기업 B", 1, 1), ("70만원", 1, 1)],
            [("기업 C", 1, 1), ("100만원", 1, 1)],
        ],
        0,
    )
    document = _document(table)

    result = generate_composite_candidates(document, _pack(document))

    candidate = _with_primary(result, "hwpx:t-data-rows:occ:7")
    assert [(atom.role, atom.occurrence_id) for atom in candidate.atoms] == [
        ("primary_value", "hwpx:t-data-rows:occ:7"),
        ("row_header", "hwpx:t-data-rows:occ:6"),
        ("column_header", "hwpx:t-data-rows:occ:1"),
    ]
    assert not {
        "hwpx:t-data-rows:occ:3",
        "hwpx:t-data-rows:occ:5",
    }.intersection(atom.occurrence_id for atom in candidate.atoms)


def test_short_fragment_and_date_are_not_complete_propositions() -> None:
    document = _document(
        _paragraph("hwpx:p-fragment", "에 한함.", 0),
        _paragraph("hwpx:p-date", "2026. 4. 21.", 1),
    )

    result = generate_composite_candidates(document, _pack(document))

    assert result.candidates == ()
    assert {
        item.source_block_id
        for item in result.diagnostics
        if item.code == "PROPOSITION_NOT_COMPLETE"
    } == {"hwpx:p-fragment", "hwpx:p-date"}


def test_offsets_use_candidate_pack_text_basis_after_common_ir_trimming() -> None:
    paragraph = _paragraph("hwpx:p-trimmed", "  지원 대상은 중소기업이다.  ", 0)
    document = _document(paragraph)
    pack = _pack(document)

    result = generate_composite_candidates(document, pack)

    candidate = next(item for item in result.candidates if item.kind == COMPLETE_PROPOSITION)
    atom = candidate.atoms[0]
    source = next(item for item in pack.blocks if item.block_id == atom.source_block_id)
    assert source.text == "지원 대상은 중소기업이다."
    assert (atom.start_char, atom.end_char) == (0, len(source.text))


def test_only_native_projector_lineage_is_accepted() -> None:
    document = _document(_paragraph("hwpx:p-lineage", "지원 대상은 중소기업이다.", 0))
    pack = _pack(document).model_copy(update={"generator": "manual_gold_adjudication"})

    result = generate_composite_candidates(document, pack)

    assert result.candidates == ()
    assert [item.code for item in result.diagnostics] == [
        "CANDIDATE_PACK_LINEAGE_MISMATCH"
    ]


def test_invalid_common_ir_identity_fails_closed() -> None:
    document = _document(_paragraph("hwpx:p-identity", "지원 대상은 중소기업이다.", 0))
    pack = _pack(document)
    document["document"]["provenance"]["source_sha256"] = "not-a-sha256"

    result = generate_composite_candidates(document, pack)

    assert result.candidates == ()
    assert [item.code for item in result.diagnostics] == ["COMMON_IR_V1_REQUIRED"]


def test_candidate_and_diagnostic_order_do_not_depend_on_ir_array_order() -> None:
    first = _table(
        "hwpx:t-order-a",
        [[("구분", 1, 1), ("내용", 1, 1)], [("A", 1, 1), ("값", 1, 1)]],
        0,
    )
    second = _table(
        "hwpx:t-order-b",
        [[("구분", 1, 1), ("금액", 1, 1)], [("B", 1, 1), ("100만원", 1, 1)]],
        1,
    )
    second["cells"][0].pop("col_span")
    document = _document(first, second)
    pack = _pack(document)
    baseline = generate_composite_candidates(document, pack)

    shuffled = deepcopy(document)
    random.Random(7).shuffle(shuffled["blocks"])
    for block in shuffled["blocks"]:
        random.Random(block["block_id"]).shuffle(block.get("cells", []))
    reordered = generate_composite_candidates(shuffled, pack)

    assert reordered == baseline


def test_cell_cannot_borrow_an_occurrence_from_another_table() -> None:
    first = _table(
        "hwpx:t-owner-a",
        [[("구분", 1, 1), ("내용", 1, 1)], [("A", 1, 1), ("값", 1, 1)]],
        0,
    )
    second = _table(
        "hwpx:t-owner-b",
        [[("구분", 1, 1), ("내용", 1, 1)], [("B", 1, 1), ("외부값", 1, 1)]],
        1,
    )
    borrowed = second["cells"][-1]["text_occurrence_ids"][0]
    first["cells"][-1]["text_occurrence_ids"] = [borrowed]
    document = _document(first, second)

    result = generate_composite_candidates(document, _pack(document))

    assert not any(
        candidate.atoms[0].common_ir_block_id == "hwpx:t-owner-a"
        for candidate in result.candidates
    )
    assert any(
        item.common_ir_block_id == "hwpx:t-owner-a"
        and item.code == "TABLE_GEOMETRY_AMBIGUOUS"
        for item in result.diagnostics
    )


def test_value_spanning_distinct_row_axes_is_ambiguous() -> None:
    table = _table(
        "hwpx:t-rowspan",
        [
            [("구분", 1, 1), ("지원액", 1, 1)],
            [("기업 A", 1, 1), ("100만원", 2, 1)],
            [("기업 B", 1, 1)],
        ],
        0,
    )
    document = _document(table)

    result = generate_composite_candidates(document, _pack(document))

    assert not any(
        candidate.atoms[0].occurrence_id == "hwpx:t-rowspan:occ:3"
        for candidate in result.candidates
    )
    assert any(
        item.code == "TABLE_AXIS_OWNERSHIP_AMBIGUOUS"
        and item.common_ir_cell_id == "hwpx:t-rowspan:c:3"
        for item in result.diagnostics
    )


def test_tampered_candidate_pack_text_is_not_accepted_as_source() -> None:
    table = _table(
        "hwpx:t-tampered",
        [[("구분", 1, 1), ("지원액", 1, 1)], [("기업 A", 1, 1), ("1억원", 1, 1)]],
        0,
    )
    document = _document(table)
    pack = _pack(document)
    blocks = [
        block.model_copy(update={"text": "2억원"})
        if block.common_ir_occurrence_ids == ("hwpx:t-tampered:occ:3",)
        else block
        for block in pack.blocks
    ]
    tampered = pack.model_copy(update={"blocks": blocks})

    result = generate_composite_candidates(document, tampered)

    assert not any(
        candidate.atoms[0].occurrence_id == "hwpx:t-tampered:occ:3"
        for candidate in result.candidates
    )
    assert any(
        item.code == "A_BLOCK_NOT_EXACT_COMMON_IR_OCCURRENCE"
        for item in result.diagnostics
    )


def test_tampered_paragraph_cannot_bypass_verified_a_occurrences() -> None:
    document = _document(
        _paragraph("hwpx:p-tampered", "지원 대상은 중소기업이다.", 0)
    )
    pack = _pack(document)
    tampered = pack.model_copy(
        update={
            "blocks": [
                block.model_copy(update={"text": "지원 대상은 대기업이다."})
                for block in pack.blocks
            ]
        }
    )

    result = generate_composite_candidates(document, tampered)

    assert result.candidates == ()
    assert [item.code for item in result.diagnostics] == [
        "A_BLOCK_NOT_EXACT_COMMON_IR_OCCURRENCE"
    ]


def test_table_resource_cap_fails_closed_without_partial_candidates(monkeypatch) -> None:
    table = _table(
        "hwpx:t-cap",
        [[("구분", 1, 1), ("내용", 1, 1)], [("A", 1, 1), ("값", 1, 1)]],
        0,
    )
    document = _document(table)
    monkeypatch.setattr(composite_candidates, "MAX_TABLE_CELLS", 3)

    result = generate_composite_candidates(document, _pack(document))

    assert result.candidates == ()
    assert "TABLE_RESOURCE_CAP_EXCEEDED" in {
        item.code for item in result.diagnostics
    }


@pytest.mark.parametrize("invalid_value", [True, 1.0, "1"])
def test_geometry_requires_json_integer_values_not_bool_float_or_string(invalid_value) -> None:
    table = _table(
        "hwpx:t-geometry-type",
        [[("구분", 1, 1), ("내용", 1, 1)], [("A", 1, 1), ("값", 1, 1)]],
        0,
    )
    table["cells"][0]["row_span"] = invalid_value
    document = _document(table)

    result = generate_composite_candidates(document, _pack(document))

    assert result.candidates == ()
    assert "TABLE_GEOMETRY_AMBIGUOUS" in {item.code for item in result.diagnostics}


def test_duplicate_a_occurrence_fails_closed_independent_of_pack_order() -> None:
    table = _table(
        "hwpx:t-duplicate-a",
        [[("구분", 1, 1), ("내용", 1, 1)], [("A", 1, 1), ("값", 1, 1)]],
        0,
    )
    document = _document(table)
    pack = _pack(document)
    original = next(
        block
        for block in pack.blocks
        if block.common_ir_occurrence_ids == ("hwpx:t-duplicate-a:occ:3",)
    )
    duplicate = original.model_copy(update={"block_id": "another-local-locator"})
    first = pack.model_copy(update={"blocks": [duplicate, *pack.blocks]})
    second = pack.model_copy(update={"blocks": [*pack.blocks, duplicate]})

    first_result = generate_composite_candidates(document, first)
    second_result = generate_composite_candidates(document, second)

    assert first_result == second_result
    assert first_result.candidates == ()
    assert [item.code for item in first_result.diagnostics] == [
        "A_BLOCK_OCCURRENCE_DUPLICATED",
        "TABLE_CONTEXT_NOT_ROUTED_A",
    ]


def test_missing_one_header_a_occurrence_rejects_entire_value_candidate() -> None:
    table = _table(
        "hwpx:t-header-not-a",
        [[("구분", 1, 1), ("지원내용", 1, 1)], [("A", 1, 1), ("컨설팅", 1, 1)]],
        0,
    )
    document = _document(table)
    pack = _pack(document)
    missing_column_header = pack.model_copy(update={
        "blocks": [
            block
            for block in pack.blocks
            if block.common_ir_occurrence_ids != ("hwpx:t-header-not-a:occ:1",)
        ]
    })

    result = generate_composite_candidates(document, missing_column_header)

    assert result.candidates == ()
    assert any(
        item.code == "TABLE_CONTEXT_NOT_ROUTED_A"
        and item.common_ir_cell_id == "hwpx:t-header-not-a:c:1"
        for item in result.diagnostics
    )


def test_same_cell_part_missing_from_a_is_rejected_without_type_error() -> None:
    table = _table("hwpx:t-same-cell-missing", [[("지원 대상은", 1, 1)]], 0)
    table["occurrences"].append({"occurrence_id": "hwpx:t-same-cell-missing:occ:1", "text": "중소기업이다."})
    table["cells"][0]["text_occurrence_ids"].append("hwpx:t-same-cell-missing:occ:1")
    table["text"] = "지원 대상은\n중소기업이다."
    table["occurrences"][0]["text"] = table["text"]
    document = _document(table)
    pack = _pack(document)
    missing_part = pack.model_copy(update={
        "blocks": [
            block
            for block in pack.blocks
            if block.common_ir_occurrence_ids != ("hwpx:t-same-cell-missing:occ:1",)
        ]
    })

    result = generate_composite_candidates(document, missing_part)

    assert result.candidates == ()
    assert any(
        item.code == "TABLE_CONTEXT_NOT_ROUTED_A"
        and item.common_ir_cell_id == "hwpx:t-same-cell-missing:c:0"
        for item in result.diagnostics
    )


def test_table_candidate_cannot_leak_into_complete_proposition_path() -> None:
    candidate = _paragraph("hwpx:table-candidate", "지원 대상은 중소기업이다.", 0)
    candidate["kind"] = "table_candidate"
    document = _document(candidate)

    result = generate_composite_candidates(document, _pack(document))

    assert result.candidates == ()
    assert [item.code for item in result.diagnostics] == [
        "TABLE_GEOMETRY_NOT_EXPLICIT"
    ]


def test_same_cell_parts_do_not_cross_an_existing_sentence_boundary() -> None:
    table = _table("hwpx:t-cell-boundary", [[("첫 문장입니다.", 1, 1)]], 0)
    table["occurrences"].append({"occurrence_id": "hwpx:t-cell-boundary:occ:1", "text": "둘째 문장입니다."})
    table["cells"][0]["text_occurrence_ids"].append("hwpx:t-cell-boundary:occ:1")
    table["text"] = "첫 문장입니다.\n둘째 문장입니다."
    table["occurrences"][0]["text"] = table["text"]
    document = _document(table)

    result = generate_composite_candidates(document, _pack(document))

    assert result.candidates == ()
    assert any(
        item.code == "PROPOSITION_PART_BOUNDARY_AMBIGUOUS"
        and item.common_ir_cell_id == "hwpx:t-cell-boundary:c:0"
        for item in result.diagnostics
    )


def test_candidate_id_binds_common_ir_producer_and_exact_source_text_without_leaking_text() -> None:
    baseline_document = _document(_paragraph("hwpx:p-candidate-id", "지원 대상은 중소기업이다.", 0))
    baseline = generate_composite_candidates(baseline_document, _pack(baseline_document))
    baseline_candidate = next(item for item in baseline.candidates if item.kind == COMPLETE_PROPOSITION)

    changed_producer_document = deepcopy(baseline_document)
    changed_producer_document["document"]["provenance"]["generator_version"] = "2.0"
    changed_producer = generate_composite_candidates(
        changed_producer_document, _pack(changed_producer_document)
    )
    changed_producer_candidate = next(
        item for item in changed_producer.candidates if item.kind == COMPLETE_PROPOSITION
    )

    changed_text_document = _document(_paragraph("hwpx:p-candidate-id", "지원 대상은 사회적기업이다.", 0))
    changed_text = generate_composite_candidates(changed_text_document, _pack(changed_text_document))
    changed_text_candidate = next(item for item in changed_text.candidates if item.kind == COMPLETE_PROPOSITION)

    assert baseline_candidate.candidate_id != changed_producer_candidate.candidate_id
    assert baseline_candidate.candidate_id != changed_text_candidate.candidate_id
    assert not hasattr(baseline_candidate, "text")
    assert "중소기업" not in repr(baseline_candidate)


def test_repeated_missing_context_emits_one_diagnostic_per_exact_cell() -> None:
    table = _table(
        "hwpx:t-diagnostic-dedupe",
        [
            [("구분", 1, 1), ("지원내용", 1, 1)],
            [("A", 1, 1), ("컨설팅", 1, 1)],
            [("B", 1, 1), ("교육", 1, 1)],
        ],
        0,
    )
    document = _document(table)
    pack = _pack(document)
    missing_column_header = pack.model_copy(update={
        "blocks": [
            block
            for block in pack.blocks
            if block.common_ir_occurrence_ids != ("hwpx:t-diagnostic-dedupe:occ:1",)
        ]
    })

    result = generate_composite_candidates(document, missing_column_header)

    matching = [
        item
        for item in result.diagnostics
        if item.code == "TABLE_CONTEXT_NOT_ROUTED_A"
        and item.common_ir_cell_id == "hwpx:t-diagnostic-dedupe:c:1"
    ]
    assert len(matching) == 1


def test_document_resource_caps_reject_before_candidate_materialization(monkeypatch) -> None:
    document = _document(_paragraph("hwpx:p-document-cap", "지원 대상은 중소기업이다.", 0))
    monkeypatch.setattr(composite_candidates, "MAX_DOCUMENT_BLOCKS", 0)

    result = generate_composite_candidates(document, _pack(document))

    assert result.candidates == ()
    assert [item.code for item in result.diagnostics] == ["DOCUMENT_BLOCK_CAP_EXCEEDED"]


def test_candidate_upper_bound_cap_rejects_without_partial_result(monkeypatch) -> None:
    table = _table(
        "hwpx:t-candidate-cap",
        [[("구분", 1, 1), ("내용", 1, 1)], [("A", 1, 1), ("값", 1, 1)]],
        0,
    )
    document = _document(table)
    monkeypatch.setattr(composite_candidates, "MAX_COMPOSITE_CANDIDATES", 1)

    result = generate_composite_candidates(document, _pack(document))

    assert result.candidates == ()
    assert [item.code for item in result.diagnostics] == ["COMPOSITE_CANDIDATE_CAP_EXCEEDED"]


def test_duplicate_common_ir_block_id_fails_closed_independent_of_order() -> None:
    first = _paragraph("hwpx:p-duplicate", "지원 대상은 중소기업이다.", 0)
    second = _paragraph("hwpx:p-duplicate", "신청 대상은 창업기업이다.", 1)
    document = _document(first, second)
    pack = _pack(document, allow_duplicate_block_ids=True)

    baseline = generate_composite_candidates(document, pack)
    reversed_document = deepcopy(document)
    reversed_document["blocks"].reverse()
    reversed_result = generate_composite_candidates(reversed_document, pack)

    assert baseline == reversed_result
    assert baseline.candidates == ()
    assert [item.code for item in baseline.diagnostics] == [
        "COMMON_IR_BLOCK_ID_DUPLICATED"
    ]


def test_duplicate_common_ir_occurrence_id_fails_closed_independent_of_order() -> None:
    paragraph = _paragraph("hwpx:p-occurrence-duplicate", "지원 대상은 중소기업이다.", 0)
    duplicate = {
        "occurrence_id": paragraph["occurrences"][0]["occurrence_id"],
        "text": "신청 대상은 창업기업이다.",
    }
    paragraph["occurrences"].append(duplicate)
    document = _document(paragraph)
    pack = _pack(document)

    baseline = generate_composite_candidates(document, pack)
    reversed_document = deepcopy(document)
    reversed_document["blocks"][0]["occurrences"].reverse()
    reversed_result = generate_composite_candidates(reversed_document, pack)

    assert baseline == reversed_result
    assert baseline.candidates == ()
    assert [item.code for item in baseline.diagnostics] == [
        "COMMON_IR_OCCURRENCE_ID_DUPLICATED"
    ]


def test_common_ir_block_kind_cannot_be_relabelled_by_candidate_pack() -> None:
    candidate = _paragraph("hwpx:table-candidate-kind", "지원 대상은 중소기업이다.", 0)
    candidate["kind"] = "table_candidate"
    document = _document(candidate)
    pack = _pack(document)
    relabelled = pack.model_copy(
        update={
            "blocks": [
                block.model_copy(update={"block_kind": "paragraph"})
                for block in pack.blocks
            ]
        }
    )

    result = generate_composite_candidates(document, relabelled)

    assert result.candidates == ()
    assert {item.code for item in result.diagnostics} == {
        "A_BLOCK_KIND_LINEAGE_MISMATCH",
        "TABLE_GEOMETRY_NOT_EXPLICIT",
    }


def test_duplicate_occurrence_within_one_cell_is_ambiguous() -> None:
    table = _table(
        "hwpx:t-cell-occurrence-duplicate",
        [[("구분", 1, 1), ("내용", 1, 1)], [("A", 1, 1), ("값", 1, 1)]],
        0,
    )
    target_occurrence = table["cells"][-1]["text_occurrence_ids"][0]
    table["cells"][-1]["text_occurrence_ids"].append(target_occurrence)
    document = _document(table)

    result = generate_composite_candidates(document, _pack(document))

    assert result.candidates == ()
    assert "TABLE_GEOMETRY_AMBIGUOUS" in {
        item.code for item in result.diagnostics
    }


def test_duplicate_candidate_pack_block_id_rejects_whole_pack() -> None:
    table = _table(
        "hwpx:t-pack-block-duplicate",
        [[("구분", 1, 1), ("내용", 1, 1)], [("A", 1, 1), ("값", 1, 1)]],
        0,
    )
    document = _document(table)
    pack = _pack(document)
    duplicated = pack.model_copy(
        update={
            "blocks": [
                pack.blocks[0],
                pack.blocks[1].model_copy(update={"block_id": pack.blocks[0].block_id}),
                *pack.blocks[2:],
            ]
        }
    )

    result = generate_composite_candidates(document, duplicated)

    assert result.candidates == ()
    assert "A_BLOCK_ID_DUPLICATED" in {item.code for item in result.diagnostics}


@pytest.mark.parametrize("mutation", ["document_scope", "context_relation"])
def test_only_routed_a_candidate_pack_is_accepted(mutation: str) -> None:
    document = _document(
        _paragraph("hwpx:p-a-scope", "지원 대상은 중소기업이다.", 0)
    )
    pack = _pack(document)
    if mutation == "document_scope":
        invalid = pack.model_copy(update={"extraction_scope": "document"})
    else:
        invalid = pack.model_copy(
            update={
                "blocks": [
                    block.model_copy(update={"relation": "preceding_context"})
                    for block in pack.blocks
                ]
            }
        )

    result = generate_composite_candidates(document, invalid)

    assert result.candidates == ()
    assert [item.code for item in result.diagnostics] == [
        "CANDIDATE_PACK_A_SCOPE_REQUIRED"
    ]


def test_candidate_pack_notice_must_match_common_ir_identity() -> None:
    document = _document(
        _paragraph("hwpx:p-notice-lineage", "지원 대상은 중소기업이다.", 0)
    )
    wrong_notice = _pack(document).model_copy(update={"notice_id": "PBLN-other"})

    result = generate_composite_candidates(document, wrong_notice)

    assert result.candidates == ()
    assert [item.code for item in result.diagnostics] == [
        "CANDIDATE_PACK_LINEAGE_MISMATCH"
    ]


def test_missing_table_cell_array_is_a_fatal_document_diagnostic() -> None:
    table = _table(
        "hwpx:t-no-cells",
        [[("구분", 1, 1), ("내용", 1, 1)], [("A", 1, 1), ("값", 1, 1)]],
        0,
    )
    document = _document(table)
    pack = _pack(document)
    table["cells"] = None

    result = generate_composite_candidates(document, pack)

    assert result.candidates == ()
    assert [item.code for item in result.diagnostics] == ["TABLE_CELLS_INVALID"]
    assert "TABLE_CELLS_INVALID" in composite_candidates.FATAL_COMPOSITE_DIAGNOSTIC_CODES
