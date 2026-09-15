"""Regression coverage for Request Profile identity and period materialization."""

from __future__ import annotations

import json
from pathlib import Path

from worker import vendor  # noqa: F401 - installs vendored contract paths
from worker.contracts.cpl_result import CplResult
from worker.contracts.fit_result import FitResult, PurposeAxisClassification
from worker.contracts.ml_result import MlModelId
from worker.contracts.sim_result import SimComparisonResult
from worker.ml_reference import _validate_reference, resolve_authoritative_request_limit
from worker.result_payload import build_result_payload

from semantic_structuring.models import CandidatePack, SourceBlock, SourceRelation
from semantic_structuring.request_profile_v012 import (
    REQUEST_PIPELINE_VERSION,
    RequestCompletenessError,
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


def _selection_with_labeled_program_period(pack: CandidatePack) -> dict:
    """Return the checked-in selection with its now-required labelled period."""

    raw_selection = _example_selection()
    labelled_period = next(
        candidate
        for candidate in build_value_span_candidates(pack)
        if candidate.candidate_kind == "program_period_date_range"
        and candidate.source_block_id.endswith(":b10")
    )
    period_fact = next(
        fact
        for fact in raw_selection["facts"]
        if fact["field_name"] == "program_period"
    )
    period_fact["value_anchor"] = {
        "value_span_candidate_id": labelled_period.value_span_candidate_id,
    }
    return raw_selection


def _append_same_cell_paragraphs(
    pack: CandidatePack, *, prefix: str, texts: list[str]
) -> CandidatePack:
    """Mirror the one-cell/many-paragraph shape emitted by the HWPX parser."""

    source_order = max(
        (block.source_order or 0 for block in pack.blocks), default=0
    ) + 1
    added: list[SourceBlock] = []
    for index, text in enumerate(texts):
        block = SourceBlock(
            block_id=f"{prefix}#r0c0p{index}",
            text=text,
            relation=SourceRelation.CANDIDATE,
            block_kind="table_cell",
            source_order=source_order + index,
            common_ir_block_id=prefix,
            common_ir_cell_id=f"{prefix}:c0",
            common_ir_occurrence_ids=(f"occ:{prefix}:p{index}",),
        )
        block._common_ir_cell_geometry = (0, 1, 0, 1)
        added.append(block)
    return pack.model_copy(update={"blocks": [*pack.blocks, *added]})


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


def test_request_profile_derives_per_team_amount_only_from_selected_raw_fact() -> None:
    """A selected Request amount is projected without inventing a cap role."""

    document = _example_document()
    pack = build_request_candidate_pack(document)
    raw_selection = _example_selection()
    raw_selection["profile_id"] = "request:scale-projection"
    raw_selection["candidate_pack_id"] = pack.pack_id

    profile = assemble_request_profile_v012(
        document, pack, RequestSourceSelectionV012.model_validate(raw_selection)
    )

    projections = profile["derived_projections"]
    assert len(projections) == 1
    measures = projections[0]["measures"]
    team_amount = next(
        measure for measure in measures
        if measure["source_fact_id"] == "fact:grant_total_scale"
    )
    assert team_amount == {
        "measure_type": "amount",
        "measure_role": "support_amount",
        "lower_value": 4_000_000,
        "upper_value": 4_000_000,
        "unit": "KRW",
        "comparator": "eq",
        "source_fact_id": "fact:grant_total_scale",
        "source_numeric_candidate_id": "markdown:PREREVIEW-TEST-2027-03:b43#num[1]",
        "applies_per": "TEAM",
        "calculation_basis": None,
        "frequency": None,
        "aggregation_scope": "PER_UNIT",
    }
    assert all(measure["measure_role"] != "support_limit" for measure in measures)
    assert profile["processing_metadata"]["pipeline_version"] == REQUEST_PIPELINE_VERSION
    assert profile["processing_metadata"]["derived_projection_producers"] == {
        "support_scale_measures": {"numeric_candidate_extractor_version": "numeric_candidate_v2"}
    }


def test_request_profile_derives_only_explicit_limit_and_never_divides_budget() -> None:
    """Per-recipient caps require their own selected lexical evidence."""

    document = _example_document()
    original = build_request_candidate_pack(document)
    cap_block = SourceBlock(
        block_id="request:synthetic-cap#p0",
        text="기업당 최대 1,000만원",
        relation=SourceRelation.CANDIDATE,
        common_ir_block_id="request:synthetic-cap",
    )
    budget_block = SourceBlock(
        block_id="request:synthetic-budget#p0",
        text="총예산 2,000만원, 선정 4개팀",
        relation=SourceRelation.CANDIDATE,
        common_ir_block_id="request:synthetic-budget",
    )
    pack = original.model_copy(update={"blocks": [*original.blocks, cap_block, budget_block]})
    selection = RequestSourceSelectionV012.model_validate({
        "profile_id": "request:explicit-cap",
        "candidate_pack_id": pack.pack_id,
        "facts": [
            {
                "fact_id": "scale:cap",
                "field_name": "support_scale",
                "value_anchor": {
                    "source_block_id": cap_block.block_id,
                    "anchor_text": cap_block.text,
                },
            },
            {
                "fact_id": "scale:budget-and-count",
                "field_name": "support_scale",
                "value_anchor": {
                    "source_block_id": budget_block.block_id,
                    "anchor_text": budget_block.text,
                },
            },
        ],
    })

    profile = assemble_request_profile_v012(document, pack, selection)
    measures = profile["derived_projections"][0]["measures"]
    cap = next(measure for measure in measures if measure["source_fact_id"] == "scale:cap")
    assert cap["measure_role"] == "support_limit"
    assert cap["comparator"] == "lte"
    assert cap["upper_value"] == 10_000_000
    assert cap["applies_per"] == "COMPANY"
    assert cap["aggregation_scope"] == "PER_UNIT"
    # The total and count remain distinct facts; no derived 5,000,000 KRW
    # per-team amount can appear through arithmetic inference.
    assert not any(
        measure["lower_value"] == 5_000_000 or measure["upper_value"] == 5_000_000
        for measure in measures
    )


def test_request_profile_recovers_per_company_scope_from_adjacent_limit_label() -> None:
    """The exact amount span may rely on its bounded, same-block field label."""

    document = _example_document()
    original = build_request_candidate_pack(document)
    cap_block = SourceBlock(
        block_id="request:labelled-company-cap#p0",
        text="- 기업당 한도: 최대 5,000만원 (단가 5,000만원)",
        relation=SourceRelation.CANDIDATE,
        common_ir_block_id="request:labelled-company-cap",
    )
    pack = original.model_copy(update={"blocks": [*original.blocks, cap_block]})
    selection = RequestSourceSelectionV012.model_validate({
        "profile_id": "request:labelled-company-cap",
        "candidate_pack_id": pack.pack_id,
        "facts": [{
            "fact_id": "scale:company-cap",
            "field_name": "support_scale",
            # This is the source-selection shape observed in the live HWPX:
            # the model correctly selects the amount, while the explicit
            # per-company scope sits immediately before it in the same row.
            "value_anchor": {
                "source_block_id": cap_block.block_id,
                "anchor_text": "최대 5,000만원",
            },
        }],
    })

    profile = assemble_request_profile_v012(document, pack, selection)
    (measure,) = profile["derived_projections"][0]["measures"]

    assert measure["measure_role"] == "support_limit"
    assert measure["upper_value"] == 50_000_000
    assert measure["applies_per"] == "COMPANY"
    assert measure["aggregation_scope"] == "PER_UNIT"
    authoritative = resolve_authoritative_request_limit(profile)
    assert authoritative is not None
    assert authoritative.amount_won == 50_000_000
    assert authoritative.applies_per == "COMPANY"
    assert _validate_reference(
        MlModelId.MODEL_2_AMOUNT,
        {"pred_won": 56_000_000},
        authoritative_request_limit=authoritative,
    ) == (
        "사전협의안에 명시된 기업당 지원 한도는 5,000만원입니다. "
        "조건이 비슷한 과거 사업들의 지원 단위당 예측 금액은 약 5,600만원입니다."
    )


def test_request_profile_does_not_guess_between_multiple_adjacent_scopes() -> None:
    document = _example_document()
    original = build_request_candidate_pack(document)
    cap_block = SourceBlock(
        block_id="request:ambiguous-cap#p0",
        text="- 기업당 또는 과제당 한도: 최대 5,000만원",
        relation=SourceRelation.CANDIDATE,
        common_ir_block_id="request:ambiguous-cap",
    )
    pack = original.model_copy(update={"blocks": [*original.blocks, cap_block]})
    selection = RequestSourceSelectionV012.model_validate({
        "profile_id": "request:ambiguous-cap",
        "candidate_pack_id": pack.pack_id,
        "facts": [{
            "fact_id": "scale:ambiguous-cap",
            "field_name": "support_scale",
            "value_anchor": {
                "source_block_id": cap_block.block_id,
                "anchor_text": "최대 5,000만원",
            },
        }],
    })

    profile = assemble_request_profile_v012(document, pack, selection)
    (measure,) = profile["derived_projections"][0]["measures"]

    assert measure["applies_per"] is None
    assert measure["aggregation_scope"] is None
    assert resolve_authoritative_request_limit(profile) is None


def test_annual_plan_rows_are_required_in_addition_to_process_steps() -> None:
    """A workflow must not satisfy an explicit annual/sub-program plan section."""

    document = _example_document()
    pack = _append_same_cell_paragraphs(
        build_request_candidate_pack(document),
        prefix="hwpx:semantic-plan:t0",
        texts=[
            "○ (연차별·내역사업별 추진계획)",
            "- 1단계(2026~2027년): 시제품 제작 및 성능검증 중심 지원, 연 40개사",
            "- 2단계(2028년): 인증·판로 연계 사업화 지원으로 전환, 40개사",
            "- 내역사업 「스마트 기술사업화 지원」: 시제품 제작비, 시험인증비, 판로개척비 3개 항목으로 구성",
            "○ (지원대상) 부산광역시 소재 중소기업",
            "- 수행절차: 공고 → 신청·접수 → 평가 → 선정 → 협약 → 집행",
        ],
    )
    raw_selection = _selection_with_labeled_program_period(pack)
    raw_selection["profile_id"] = "request:annual-plan-completeness"
    raw_selection["candidate_pack_id"] = pack.pack_id
    process = next(block for block in pack.blocks if "수행절차:" in block.text)
    raw_selection["facts"].append({
        "fact_id": "plan:workflow-only",
        "field_name": "implementation_plan",
        "value_anchor": {
            "source_block_id": process.block_id,
            "anchor_text": "공고 → 신청·접수 → 평가 → 선정 → 협약 → 집행",
        },
    })

    try:
        assemble_request_profile_v012(
            document,
            pack,
            RequestSourceSelectionV012.model_validate(raw_selection),
            enforce_completeness=True,
        )
    except RequestCompletenessError as error:
        message = str(error)
        assert "implementation_plan" in message
        assert "hwpx:semantic-plan:t0#r0c0p1" in message
        assert "hwpx:semantic-plan:t0#r0c0p2" in message
        assert "hwpx:semantic-plan:t0#r0c0p3" in message
        assert "2026" not in message
    else:
        raise AssertionError("workflow-only selection must not cover annual plan rows")

    for index in (1, 2, 3):
        row = next(
            block
            for block in pack.blocks
            if block.block_id == f"hwpx:semantic-plan:t0#r0c0p{index}"
        )
        raw_selection["facts"].append({
            "fact_id": f"plan:annual:{index}",
            "field_name": "implementation_plan",
            "value_anchor": {
                "source_block_id": row.block_id,
                "anchor_text": row.text,
            },
        })
    repaired = assemble_request_profile_v012(
        document,
        pack,
        RequestSourceSelectionV012.model_validate(raw_selection),
        enforce_completeness=True,
    )
    assert [
        row["value_source"]["source_block_id"]
        for row in repaired["request_context"]["implementation_plan"][-3:]
    ] == [
        "hwpx:semantic-plan:t0#r0c0p1",
        "hwpx:semantic-plan:t0#r0c0p2",
        "hwpx:semantic-plan:t0#r0c0p3",
    ]


def test_explicit_execution_method_and_actor_roles_are_required() -> None:
    """Action relations elsewhere do not replace explicit method/role spans."""

    document = _example_document()
    pack = _append_same_cell_paragraphs(
        build_request_candidate_pack(document),
        prefix="hwpx:semantic-delivery:t0",
        texts=[
            "○ (수행기관) 부산테크노파크(주관), 부산상공회의소(협력)",
            "- 수행방식: 시 출연기관 위탁(보조)",
            "- 단계별 역할: 공고·선정은 부산광역시, 접수·평가는 부산테크노파크",
        ],
    )
    raw_selection = _selection_with_labeled_program_period(pack)
    raw_selection["profile_id"] = "request:delivery-completeness"
    raw_selection["candidate_pack_id"] = pack.pack_id

    try:
        assemble_request_profile_v012(
            document,
            pack,
            RequestSourceSelectionV012.model_validate(raw_selection),
            enforce_completeness=True,
        )
    except RequestCompletenessError as error:
        message = str(error)
        assert "delivery_methods" in message
        assert "delivery_relation_roles" in message
        assert "hwpx:semantic-delivery:t0#r0c0p0" in message
        assert "hwpx:semantic-delivery:t0#r0c0p1" in message
        assert "부산" not in message
    else:
        raise AssertionError("explicit execution method and actor roles must be selected")

    relation_block = next(
        block
        for block in pack.blocks
        if block.block_id == "hwpx:semantic-delivery:t0#r0c0p0"
    )
    method_block = next(
        block
        for block in pack.blocks
        if block.block_id == "hwpx:semantic-delivery:t0#r0c0p1"
    )
    raw_selection["delivery_relations"].extend([
        {
            "delivery_relation_id": "delivery:busan-techno-park-lead",
            "actor_anchor": {
                "source_block_id": relation_block.block_id,
                "anchor_text": "부산테크노파크",
            },
            "role_anchor": {
                "source_block_id": relation_block.block_id,
                "anchor_text": "주관",
            },
            "relation_container": {
                "kind": "paragraph",
                "source_block_id": relation_block.block_id,
                "anchor_text": relation_block.text,
            },
        },
        {
            "delivery_relation_id": "delivery:busan-chamber-partner",
            "actor_anchor": {
                "source_block_id": relation_block.block_id,
                "anchor_text": "부산상공회의소",
            },
            "role_anchor": {
                "source_block_id": relation_block.block_id,
                "anchor_text": "협력",
            },
            "relation_container": {
                "kind": "paragraph",
                "source_block_id": relation_block.block_id,
                "anchor_text": relation_block.text,
            },
        },
    ])
    raw_selection["delivery_methods"].append({
        "fact_id": "delivery-method:delegated-subsidy",
        "value_anchor": {
            "source_block_id": method_block.block_id,
            "anchor_text": "시 출연기관 위탁(보조)",
        },
    })

    repaired = assemble_request_profile_v012(
        document,
        pack,
        RequestSourceSelectionV012.model_validate(raw_selection),
        enforce_completeness=True,
    )
    assert repaired["comparison_profile"]["delivery_methods"][-1]["value_raw"] == (
        "시 출연기관 위탁(보조)"
    )
    assert [
        relation["role"]["value_raw"]
        for relation in repaired["comparison_profile"]["delivery_relations"][-2:]
    ] == ["주관", "협력"]
