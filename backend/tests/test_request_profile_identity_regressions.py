"""Regression coverage for Request Profile identity and period materialization."""

from __future__ import annotations

import json
from pathlib import Path

from worker import vendor  # noqa: F401 - installs vendored contract paths
from worker.contracts.cpl_result import CplResult
from worker.contracts.fit_result import FitResult, PurposeAxisClassification
from worker.contracts.sim_result import SimComparisonResult
from worker.result_payload import build_result_payload

from semantic_structuring.models import CandidatePack, SourceBlock, SourceRelation
from semantic_structuring.request_profile_v012 import (
    RequestSourceSelectionV012,
    assemble_request_profile_v012,
    build_request_candidate_pack,
    build_value_span_candidates,
)


_EXAMPLES = (
    Path(__file__).resolve().parents[1]
    / "vendor"
    / "portable_existing_request_profiles_20260831"
    / "examples"
    / "request"
)


def _example_document() -> dict:
    return json.loads((_EXAMPLES / "common_ir_v1.json").read_text(encoding="utf-8"))


def _example_selection() -> dict:
    return json.loads(
        (_EXAMPLES / "source_selection_v012.json").read_text(encoding="utf-8")
    )["selection"]


def test_marked_year_only_program_period_is_candidate_and_materializes() -> None:
    document = _example_document()
    original = build_request_candidate_pack(document)
    period_block = SourceBlock(
        block_id="request:period#p0",
        text="사업기간: ’24 ~ `28년(5년)",
        relation=SourceRelation.CANDIDATE,
        common_ir_block_id="request:period",
        common_ir_occurrence_ids=("occ:period",),
    )
    pack = original.model_copy(update={"blocks": [*original.blocks, period_block]})
    candidates = [
        candidate
        for candidate in build_value_span_candidates(pack)
        if candidate.candidate_kind == "program_period_date_range"
        and candidate.value_raw == "’24 ~ `28년"
    ]
    assert len(candidates) == 1

    selection = RequestSourceSelectionV012.model_validate(
        {
            "profile_id": "request:marked-year-period",
            "candidate_pack_id": pack.pack_id,
            "facts": [
                {
                    "fact_id": "period_01",
                    "field_name": "program_period",
                    "value_anchor": {
                        "value_span_candidate_id": candidates[0].value_span_candidate_id
                    },
                }
            ],
        }
    )
    profile = assemble_request_profile_v012(document, pack, selection)
    assert profile["comparison_profile"]["program_period"][0]["value_raw"] == "’24 ~ `28년"


def test_title_uses_explicit_business_name_before_detail_program_fallback() -> None:
    document = _example_document()
    original = build_request_candidate_pack(document)
    title_label = SourceBlock(
        block_id="request:title#r1c0p0",
        text="사업명",
        relation=SourceRelation.CANDIDATE,
        block_kind="table_cell",
        common_ir_block_id="request:title",
        common_ir_cell_id="request:title:c0",
        common_ir_occurrence_ids=("occ:title-label",),
    )
    title_value = SourceBlock(
        block_id="request:title#r1c1p0",
        text="ICT지원사업",
        relation=SourceRelation.CANDIDATE,
        block_kind="table_cell",
        common_ir_block_id="request:title",
        common_ir_cell_id="request:title:c1",
        common_ir_occurrence_ids=("occ:title-value",),
    )
    pack = original.model_copy(
        update={"blocks": [*original.blocks, title_label, title_value]}
    )
    raw_selection = _example_selection()
    raw_selection["profile_id"] = "request:explicit-title"
    raw_selection["candidate_pack_id"] = pack.pack_id
    profile = assemble_request_profile_v012(
        document, pack, RequestSourceSelectionV012.model_validate(raw_selection)
    )

    assert profile["identity"]["title_raw"] == "ICT지원사업"
    # The result payload and DB ingest both source program_name from identity.
    payload = build_result_payload(
        profile=profile,
        cpl=CplResult(
            items=[], profile_id=None, common_ir_document_id=None,
            common_ir_source_sha256=None, candidate_pack_id=None,
            pipeline_version=None, structured_schema_version=None,
            model_id=None, prompt_version=None,
        ),
        fit=FitResult(
            relations=[], purpose_axis=PurposeAxisClassification(attempted=False),
            profile_id=None, common_ir_document_id=None, model_profile="test",
            ruleset_version="test", prompt_version="test",
        ),
        sim=SimComparisonResult(
            request_profile_id=None, candidates=[], model_profile="test",
            ruleset_version="test", prompt_version="test", scoring_version="test",
        ),
        sim_profiles={}, retrieval_similarities={}, profile_version_ids={},
    )
    assert payload["program_name"] == "ICT지원사업"
    ingest_sql = (
        Path(__file__).resolve().parents[1]
        / "supabase"
        / "migrations"
        / "17_request_profile_ingest_core.sql"
    ).read_text(encoding="utf-8")
    assert "COALESCE(p_profile#>>'{identity,program_name}', p_profile#>>'{identity,title_raw}')" in ingest_sql


def test_title_falls_back_only_to_a_unique_detail_program_node() -> None:
    document = _example_document()
    pack = build_request_candidate_pack(document)
    raw_selection = _example_selection()
    raw_selection["profile_id"] = "request:detail-title"
    raw_selection["candidate_pack_id"] = pack.pack_id
    profile = assemble_request_profile_v012(
        document, pack, RequestSourceSelectionV012.model_validate(raw_selection)
    )

    assert profile["identity"]["title_raw"] == "동구 청년 창업지원사업(지원내용 개편)"


def test_table_column_pair_validation_honors_actor_row_span() -> None:
    document = _example_document()
    table_id = "request:rowspan-delivery-table"
    document["blocks"].append(
        {
            "block_id": table_id,
            "kind": "table",
            "structure_status": "explicit",
            "reading_order": max(
                block["reading_order"] for block in document["blocks"]
            ) + 1,
            "text": "주무부처 정책수립 및 예산 지원",
            "text_occurrence_ids": [
                "occ:rowspan:actor",
                "occ:rowspan:inside",
                "occ:rowspan:member",
            ],
            "occurrences": [
                {"occurrence_id": "occ:rowspan:actor", "text": "주무부처"},
                {"occurrence_id": "occ:rowspan:inside", "text": "병합 셀 옆의 내용"},
                {
                    "occurrence_id": "occ:rowspan:member",
                    "text": "정책수립 및 예산 지원",
                },
            ],
            "cells": [
                {
                    "cell_id": "request:rowspan:actor",
                    "row_index": 0,
                    "row_span": 2,
                    "col_index": 0,
                    "col_span": 1,
                    "text_occurrence_ids": ["occ:rowspan:actor"],
                },
                {
                    "cell_id": "request:rowspan:inside",
                    "row_index": 1,
                    "row_span": 1,
                    "col_index": 4,
                    "col_span": 1,
                    "text_occurrence_ids": ["occ:rowspan:inside"],
                },
                {
                    "cell_id": "request:rowspan:member",
                    "row_index": 2,
                    "row_span": 1,
                    "col_index": 0,
                    "col_span": 1,
                    "text_occurrence_ids": ["occ:rowspan:member"],
                },
            ],
        }
    )
    pack = build_request_candidate_pack(document)
    by_cell_id = {
        block.common_ir_cell_id: block
        for block in pack.blocks
        if block.common_ir_cell_id is not None
    }
    actor = by_cell_id["request:rowspan:actor"]
    member = by_cell_id["request:rowspan:member"]
    raw_selection = _example_selection()
    raw_selection["profile_id"] = "request:rowspan-delivery"
    raw_selection["candidate_pack_id"] = pack.pack_id
    raw_selection["delivery_relations"].append(
        {
            "delivery_relation_id": "delivery:rowspan",
            "actor_anchor": {
                "source_block_id": actor.block_id,
                "anchor_text": actor.text,
            },
            "role_anchor": {
                "source_block_id": member.block_id,
                "anchor_text": member.text,
            },
            "actions": [],
            "relation_container": {
                "kind": "table_column_pair",
                "common_ir_block_id": table_id,
            },
        }
    )

    profile = assemble_request_profile_v012(
        document,
        pack,
        RequestSourceSelectionV012.model_validate(raw_selection),
    )

    relation = next(
        row
        for row in profile["comparison_profile"]["delivery_relations"]
        if row["delivery_relation_id"] == "delivery:rowspan"
    )
    assert relation["relation_container"] == {
        "container_type": "table_column_pair",
        "common_ir_document_id": document["document"]["document_id"],
        "common_ir_block_id": table_id,
        "actor_common_ir_cell_id": "request:rowspan:actor",
        "role_common_ir_cell_id": "request:rowspan:member",
    }
