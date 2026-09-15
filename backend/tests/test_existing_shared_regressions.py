"""Regression coverage shared by Existing and Request Common-IR preparation.

These cases are deliberately adapter- and model-free.  They lock only source
partitioning: no table is interpreted as a fact and no attachment scope is
decided here.
"""

from __future__ import annotations

import pytest

from worker import vendor  # noqa: F401 - installs vendored contract paths

from semantic_structuring.candidate_assembly import build_routed_a_pack
from semantic_structuring.common_ir_v1 import project_common_ir_v1
from semantic_structuring.models import SourceBlock, SourceRelation
from semantic_structuring.notice_preparation import PreparedNotice, prepare_notice


def _table(rows: list[tuple[str, str]]) -> dict:
    return {
        "kind": "table",
        "rows": len(rows),
        "cols": 2,
        "cells": [
            {"row": row, "col": column, "blocks": [{"text": text}]}
            for row, pair in enumerate(rows)
            for column, text in enumerate(pair)
        ],
    }


def test_routed_prose_keeps_the_single_explicitly_referenced_b_table() -> None:
    """A unique ①-1 reference retains source cells, never a derived fact."""

    prepared = prepare_notice(
        {
            "notice_id": "existing-unique-reference",
            "ir": {
                "body": [
                    {"kind": "paragraph", "text": "세부 지원은 ①-1 항목을 참조"},
                    _table([("구분", "지원 내용"), ("①-1", "시제품 제작")]),
                ]
            },
        },
        [],
    )

    pack, metrics = build_routed_a_pack(
        prepared,
        [
            {
                "source_block_id": "body[0]",
                "route_tags": ["support"],
                "table_disposition": None,
            },
            {
                "source_block_id": "body[1]",
                "route_tags": ["table"],
                "table_disposition": "b_search_only",
            },
        ],
    )

    assert metrics["reference_promoted_a_table_ids"] == ["body[1]"]
    assert metrics["a_table_ids"] == ["body[1]"]
    assert pack is not None
    assert {block.block_id for block in pack.blocks} >= {
        "body[0]",
        "body[1]#r1c0p0",
        "body[1]#r1c1p0",
    }


def test_ambiguous_table_reference_never_promotes_a_b_table() -> None:
    """The same label in multiple tables is unsafe and remains B-only."""

    prepared = prepare_notice(
        {
            "notice_id": "existing-ambiguous-reference",
            "ir": {
                "body": [
                    {"kind": "paragraph", "text": "세부 지원은 ①-1 항목을 참조"},
                    _table([("①-1", "시제품 제작")]),
                    _table([("①-1", "해외 인증")]),
                ]
            },
        },
        [],
    )

    pack, metrics = build_routed_a_pack(
        prepared,
        [
            {
                "source_block_id": "body[0]",
                "route_tags": ["support"],
                "table_disposition": None,
            },
            {
                "source_block_id": "body[1]",
                "route_tags": ["table"],
                "table_disposition": "b_search_only",
            },
            {
                "source_block_id": "body[2]",
                "route_tags": ["table"],
                "table_disposition": "b_search_only",
            },
        ],
    )

    assert metrics["reference_promoted_a_table_ids"] == []
    assert metrics["a_table_ids"] == []
    assert pack is not None
    assert [block.block_id for block in pack.blocks] == ["body[0]"]


def test_date_like_numeric_ranges_do_not_promote_a_b_table() -> None:
    """A year-month range is not a plain package/row reference."""

    prepared = prepare_notice(
        {
            "notice_id": "existing-date-range",
            "ir": {
                "body": [
                    {"kind": "paragraph", "text": "2025-1 지원계획을 공고함"},
                    _table([("사업기간", "2025-1 ~ 2025-12")]),
                ]
            },
        },
        [],
    )
    pack, metrics = build_routed_a_pack(
        prepared,
        [
            {
                "source_block_id": "body[0]",
                "route_tags": ["support"],
                "table_disposition": None,
            },
            {
                "source_block_id": "body[1]",
                "route_tags": ["table"],
                "table_disposition": "b_search_only",
            },
        ],
    )

    assert metrics["reference_promoted_a_table_ids"] == []
    assert metrics["a_table_ids"] == []
    assert pack is not None
    assert [block.block_id for block in pack.blocks] == ["body[0]"]


def test_explicit_plain_row_reference_can_promote_one_b_table() -> None:
    """Short numeric labels require both prose-reference and row-label context."""

    prepared = prepare_notice(
        {
            "notice_id": "existing-plain-reference",
            "ir": {
                "body": [
                    {"kind": "paragraph", "text": "세부 지원은 1-1 항목을 참조"},
                    _table([("1-1", "시제품 제작")]),
                ]
            },
        },
        [],
    )
    pack, metrics = build_routed_a_pack(
        prepared,
        [
            {
                "source_block_id": "body[0]",
                "route_tags": ["support"],
                "table_disposition": None,
            },
            {
                "source_block_id": "body[1]",
                "route_tags": ["table"],
                "table_disposition": "b_search_only",
            },
        ],
    )

    assert metrics["reference_promoted_a_table_ids"] == ["body[1]"]
    assert metrics["a_table_ids"] == ["body[1]"]
    assert pack is not None


def test_missing_legacy_partial_table_route_is_conservatively_retained_as_b() -> None:
    """Old router artifacts may omit a newly exposed structural companion."""

    prepared = PreparedNotice(
        notice_id="existing-legacy-router",
        sections=[],
        fact_candidate_blocks=[
            SourceBlock(
                block_id="body[0]",
                text="지원 내용",
                relation=SourceRelation.CANDIDATE,
                block_kind="paragraph",
            )
        ],
        table_candidate_blocks=[
            SourceBlock(
                block_id="body[1]",
                text="부분 표 구조",
                relation=SourceRelation.CANDIDATE,
                block_kind="table_candidate",
            )
        ],
        table_cell_candidate_blocks=[],
        search_only_blocks=[],
        excluded_blocks=[],
        section_scope_applied=True,
    )

    pack, metrics = build_routed_a_pack(
        prepared,
        [
            {
                "source_block_id": "body[0]",
                "route_tags": ["support"],
                "table_disposition": None,
            }
        ],
    )

    assert metrics["implicit_legacy_b_table_ids"] == ["body[1]"]
    assert metrics["a_table_ids"] == []
    assert pack is not None
    assert [block.block_id for block in pack.blocks] == ["body[0]"]


def _common_ir_block(
    block_id: str,
    reading_order: int,
    text: str,
    *,
    marker: str | None = None,
    kind: str = "paragraph",
) -> dict:
    occurrence_id = f"occ:{block_id}"
    return {
        "block_id": block_id,
        "kind": kind,
        "structure_status": "explicit",
        "text": text,
        "text_occurrence_ids": [occurrence_id],
        "reading_order": reading_order,
        "page": None,
        "section_path": "",
        "occurrences": [{"occurrence_id": occurrence_id, "text": text}],
        "boundary_markers": (
            [{"marker": "서식", "matched_text": marker}] if marker else []
        ),
        "provenance": {},
    }


@pytest.mark.parametrize("source_kind", ["hwp", "hwpx"])
@pytest.mark.parametrize("inline_kind", ["paragraph", "table"])
def test_hwp_family_only_uses_a_leading_marker_as_attachment_boundary(
    source_kind: str, inline_kind: str,
) -> None:
    """Inline [서식] references must not hide later notice content as B/C."""

    document = {
        "schema_version": "common_ir_v1",
        "document": {
            "document_id": f"{source_kind}:existing-inline-marker",
            "source_kind": source_kind,
            "artifact_role": "production",
            "provenance": {"source_sha256": "test-sha"},
        },
        "blocks": [
            _common_ir_block(
                f"{source_kind}:p0",
                0,
                "지원내용은 [서식 1] 작성 예시를 참고",
                marker="[서식 1]",
                kind=inline_kind,
            ),
            _common_ir_block(f"{source_kind}:p1", 1, "사업 지원내용"),
            _common_ir_block(
                f"{source_kind}:p2",
                2,
                "■ [서식 2] 신청서",
                marker="[서식 2]",
            ),
            _common_ir_block(f"{source_kind}:p3", 3, "신청인 성명"),
        ],
        "relations": [],
        "conflicts": [],
    }

    projection = project_common_ir_v1(document)

    assert [
        (section.section_id, section.source_block_ids, section.title_raw)
        for section in projection.sections
    ] == [
        (
            "main_notice",
            [f"{source_kind}:p0", f"{source_kind}:p1"],
            None,
        ),
        (
            "attachment_1",
            [f"{source_kind}:p2", f"{source_kind}:p3"],
            "[서식 2]",
        ),
    ]
