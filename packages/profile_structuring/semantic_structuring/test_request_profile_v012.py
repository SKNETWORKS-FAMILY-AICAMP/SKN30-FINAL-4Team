"""Offline contract tests for Request Profile v0.1.2 scaffold."""

from __future__ import annotations

import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

from .request_profile_v012 import (
    RequestSourceSelectionV012,
    assemble_request_profile_v012,
    build_request_candidate_pack,
    build_value_span_candidates,
    candidate_pack_artifact,
    resolve_request_type_from_candidate_pack,
)
from .run_request_profile_v012 import (
    RequestMaterializationError,
    RequestSourceSelectionParseError,
    _failure_selection_artifact,
    _new_remote_run_status,
    _normalize_remote_selection_payload,
    _selection_parse_failure_artifact,
    _status_observer,
    _write_artifact_with_lifecycle,
    request_selection_instructions,
    select_and_materialize_with_repairs,
)
from .models import CandidatePack, SourceBlock, SourceRelation


ROOT = Path("exploratory_study/results/request_profile_test_corpus_common_ir_v1_20260831/common_ir")
CLEAN_ROOT = Path("exploratory_study/results/request_profile_test_corpus_common_ir_v1_presentation_clean_20260831/common_ir")
TABLE_COMMON_IR = Path("exploratory_study/results/common_ir_v1_existing_profile_handoff_20260830/PBLN_000000000125056_hwp/common_ir_v1/PBLN_000000000125056.hwp.json")
REQUEST_HWP_ORG_CHART = Path("exploratory_study/results/request_hwp_structure_probe_20260831/example/common_ir_v1/PREREVIEW-EXAMPLE.hwp.json")


def _document() -> dict:
    return json.loads((ROOT / "PREREVIEW-TEST-2027-01.common_ir_v1.json").read_text(encoding="utf-8"))


def _document_02() -> dict:
    return json.loads((ROOT / "PREREVIEW-TEST-2027-02.common_ir_v1.json").read_text(encoding="utf-8"))


def _clean_document_02() -> dict:
    return json.loads((CLEAN_ROOT / "PREREVIEW-TEST-2027-02.common_ir_v1.json").read_text(encoding="utf-8"))


def _clean_document_03() -> dict:
    return json.loads((CLEAN_ROOT / "PREREVIEW-TEST-2027-03.common_ir_v1.json").read_text(encoding="utf-8"))


def _clean_document_04() -> dict:
    return json.loads((CLEAN_ROOT / "PREREVIEW-TEST-2027-04.common_ir_v1.json").read_text(encoding="utf-8"))


def _anchor(pack, needle: str) -> dict:
    matches = [(block.block_id, block.text) for block in pack.blocks if block.text.count(needle) == 1]
    assert matches, needle
    return {"source_block_id": matches[0][0], "anchor_text": needle}


def _program_period_candidate_anchor(pack, value_raw: str) -> dict:
    matches = [
        candidate for candidate in build_value_span_candidates(pack)
        if candidate.candidate_kind == "program_period_date_range" and candidate.value_raw == value_raw
    ]
    assert len(matches) == 1, value_raw
    return {"value_span_candidate_id": matches[0].value_span_candidate_id}


def _with_server_request_type(pack: CandidatePack) -> CandidatePack:
    """Add the minimal form option container to synthetic/non-Request packs."""

    block = SourceBlock(
        block_id="request:test-checkbox#p0", text="☑ 사업내용 변경",
        relation=SourceRelation.CANDIDATE, common_ir_block_id="request:test-checkbox",
        common_ir_occurrence_ids=("occ:test-checkbox",),
    )
    return pack.model_copy(update={"blocks": [*pack.blocks, block]})


def test_request_candidate_pack_retains_common_ir_lineage() -> None:
    document = _document()
    pack = build_request_candidate_pack(document)
    artifact = candidate_pack_artifact(pack, document)
    assert artifact["common_ir_document_id"] == "request:PREREVIEW-TEST-2027-01"
    assert artifact["text_basis"] == "common_ir_v1_candidate_pack"
    assert artifact["blocks"]


def test_request_02_repeated_anchor_requires_and_resolves_span_candidate_id() -> None:
    """Repeated text cannot select an implicit nth occurrence inside one block."""

    document = _document_02()
    pack = build_request_candidate_pack(document)
    repeated = [
        item for item in build_value_span_candidates(pack)
        if item.source_block_id.endswith(":b4") and item.value_raw == "신설"
    ]
    assert len(repeated) >= 2
    legacy = RequestSourceSelectionV012.model_validate({
        "profile_id": "request-02-ambiguous-legacy", "candidate_pack_id": pack.pack_id,
        "facts": [{
            "fact_id": "content_legacy", "field_name": "support_content",
            "value_anchor": {"source_block_id": repeated[0].source_block_id, "anchor_text": "신설"},
        }],
    })
    try:
        assemble_request_profile_v012(document, pack, legacy)
    except ValueError as error:
        assert "value_span_candidate_id" in str(error)
    else:
        raise AssertionError("ambiguous legacy anchor must fail closed")
    selected = repeated[1]
    candidate_selection = RequestSourceSelectionV012.model_validate({
        "profile_id": "request-02-candidate", "candidate_pack_id": pack.pack_id,
        "facts": [{
            "fact_id": "content_candidate", "field_name": "support_content",
            "value_anchor": {
                "value_span_candidate_id": selected.value_span_candidate_id,
                "source_block_id": selected.source_block_id,
            },
        }],
    })
    profile = assemble_request_profile_v012(document, pack, candidate_selection)
    source = profile["comparison_profile"]["support_content"][0]["value_source"]
    assert source["source_block_id"] == selected.source_block_id
    assert (source["start_char"], source["end_char"]) == (selected.start_char, selected.end_char)
    assert profile["comparison_profile"]["support_content"][0]["value_raw"] == "신설"

    candidate_only = RequestSourceSelectionV012.model_validate({
        "profile_id": "request-02-candidate-only", "candidate_pack_id": pack.pack_id,
        "facts": [{
            "fact_id": "content_candidate_only", "field_name": "support_content",
            "value_anchor": {"value_span_candidate_id": selected.value_span_candidate_id},
        }],
    })
    assert assemble_request_profile_v012(document, pack, candidate_only)["comparison_profile"]["support_content"][0]["value_raw"] == "신설"

    mismatched_hint = RequestSourceSelectionV012.model_validate({
        "profile_id": "request-02-candidate-bad-hint", "candidate_pack_id": pack.pack_id,
        "facts": [{
            "fact_id": "content_bad_hint", "field_name": "support_content",
            "value_anchor": {
                "value_span_candidate_id": selected.value_span_candidate_id,
                "source_block_id": "wrong:block",
            },
        }],
    })
    try:
        assemble_request_profile_v012(document, pack, mismatched_hint)
    except ValueError as error:
        assert "source_block_id hint" in str(error)
    else:
        raise AssertionError("candidate ID with mismatched block hint must fail closed")

    try:
        RequestSourceSelectionV012.model_validate({
            "profile_id": "request-02-candidate-with-anchor", "candidate_pack_id": pack.pack_id,
            "facts": [{
                "fact_id": "content_bad_mix", "field_name": "support_content",
                "value_anchor": {
                    "value_span_candidate_id": selected.value_span_candidate_id,
                    "source_block_id": selected.source_block_id,
                    "anchor_text": "신설",
                },
            }],
        })
    except ValueError as error:
        assert "must not be combined with anchor_text" in str(error)
    else:
        raise AssertionError("candidate ID plus anchor_text must fail selection validation")


def test_remote_candidate_anchor_normalization_keeps_local_contract_strict() -> None:
    """Only a received remote response gets compatibility cleanup in memory."""

    document = _document_02()
    pack = build_request_candidate_pack(document)
    selected = next(
        item for item in build_value_span_candidates(pack)
        if item.source_block_id.endswith(":b4") and item.value_raw == "신설"
    )
    remote_payload = {
        "profile_id": "request-02-remote-normalize", "candidate_pack_id": pack.pack_id,
        "facts": [{
            "fact_id": "content_candidate", "field_name": "support_content",
            "value_anchor": {
                "value_span_candidate_id": selected.value_span_candidate_id,
                "source_block_id": selected.source_block_id,
                # Redundant output from a remote structured-output provider.
                "anchor_text": "신설",
            },
        }],
    }
    normalized = _normalize_remote_selection_payload(remote_payload)
    normalized_anchor = normalized["facts"][0]["value_anchor"]
    assert normalized_anchor == {
        "value_span_candidate_id": selected.value_span_candidate_id,
        "source_block_id": selected.source_block_id,
    }
    remote_selection = RequestSourceSelectionV012.model_validate(normalized)
    profile = assemble_request_profile_v012(document, pack, remote_selection)
    assert profile["comparison_profile"]["support_content"][0]["value_raw"] == "신설"

    # The same data from --selection stays on the strict, fail-closed path.
    try:
        RequestSourceSelectionV012.model_validate(remote_payload)
    except ValueError as error:
        assert "must not be combined with anchor_text" in str(error)
    else:
        raise AssertionError("local selection must not receive remote compatibility cleanup")

    mismatched_hint = deepcopy(normalized)
    mismatched_hint["facts"][0]["value_anchor"]["source_block_id"] = "wrong:block"
    try:
        assemble_request_profile_v012(
            document, pack, RequestSourceSelectionV012.model_validate(mismatched_hint),
        )
    except ValueError as error:
        assert "source_block_id hint" in str(error)
    else:
        raise AssertionError("candidate block hint must remain server-validated")


def test_request_02_clean_program_period_requires_date_range_candidate() -> None:
    """Clean Markdown keeps an exact open official interval without its narration."""

    document = _clean_document_02()
    pack = build_request_candidate_pack(document)
    assert "**" not in "".join(block.text for block in pack.blocks)
    candidates = [
        candidate for candidate in build_value_span_candidates(pack)
        if candidate.candidate_kind == "program_period_date_range"
        and candidate.value_raw == "공고일~2027.12.31"
    ]
    assert len(candidates) == 1
    candidate = candidates[0]

    legacy = RequestSourceSelectionV012.model_validate({
        "profile_id": "request-02-period-legacy", "candidate_pack_id": pack.pack_id,
        "facts": [{
            "fact_id": "period_01", "field_name": "program_period",
            "value_anchor": {"source_block_id": candidate.source_block_id, "anchor_text": candidate.value_raw},
        }],
    })
    try:
        assemble_request_profile_v012(document, pack, legacy)
    except ValueError as error:
        assert "program_period_date_range" in str(error)
    else:
        raise AssertionError("program_period must not use a legacy text anchor")

    selected = RequestSourceSelectionV012.model_validate({
        "profile_id": "request-02-period-candidate", "candidate_pack_id": pack.pack_id,
        "facts": [{
            "fact_id": "period_01", "field_name": "program_period",
            "value_anchor": {"value_span_candidate_id": candidate.value_span_candidate_id},
        }],
    })
    profile = assemble_request_profile_v012(document, pack, selected)
    period = profile["comparison_profile"]["program_period"][0]
    assert period["value_raw"] == "공고일~2027.12.31"
    assert period["value_source"]["source_block_id"] == candidate.source_block_id
    assert (period["value_source"]["start_char"], period["value_source"]["end_char"]) == (
        candidate.start_char, candidate.end_char,
    )

    wrong_candidate = next(
        item for item in build_value_span_candidates(pack)
        if item.candidate_kind == "repeated_exact_span"
    )
    wrong = RequestSourceSelectionV012.model_validate({
        "profile_id": "request-02-period-wrong-kind", "candidate_pack_id": pack.pack_id,
        "facts": [{
            "fact_id": "period_01", "field_name": "program_period",
            "value_anchor": {"value_span_candidate_id": wrong_candidate.value_span_candidate_id},
        }],
    })
    try:
        assemble_request_profile_v012(document, pack, wrong)
    except ValueError as error:
        assert "program_period_date_range" in str(error)
    else:
        raise AssertionError("program_period must reject a non-date candidate")


def test_request_02_repeated_component_name_requires_candidate_id() -> None:
    """Candidate IDs protect component anchors as well as Raw Facts."""

    document = _clean_document_02()
    pack = build_request_candidate_pack(document)
    block = next(block for block in pack.blocks if block.block_id.endswith(":b84"))
    assert block.text.count("신규채용") == 2
    candidates = [
        candidate for candidate in build_value_span_candidates(pack)
        if candidate.source_block_id == block.block_id and candidate.value_raw == "신규채용"
        and candidate.candidate_kind == "repeated_exact_span"
    ]
    assert len(candidates) == 2

    legacy = RequestSourceSelectionV012.model_validate({
        "profile_id": "request-02-component-legacy", "candidate_pack_id": pack.pack_id,
        "support_components": [{
            "support_component_id": "component_new_hire", "component_kind": "support_package",
            "name_anchor": {"source_block_id": block.block_id, "anchor_text": "신규채용"},
        }],
    })
    try:
        assemble_request_profile_v012(document, pack, legacy)
    except ValueError as error:
        assert "value_span_candidate_id" in str(error)
    else:
        raise AssertionError("repeated component name must fail closed without a candidate ID")

    selected_candidate = candidates[0]
    selected = RequestSourceSelectionV012.model_validate({
        "profile_id": "request-02-component-candidate", "candidate_pack_id": pack.pack_id,
        "support_components": [{
            "support_component_id": "component_new_hire", "component_kind": "support_package",
            "name_anchor": {
                "value_span_candidate_id": selected_candidate.value_span_candidate_id,
                "source_block_id": selected_candidate.source_block_id,
            },
        }],
    })
    profile = assemble_request_profile_v012(document, pack, selected)
    component = profile["support_components"][0]
    assert component["name_raw"] == "신규채용"
    assert component["value_source"]["source_block_id"] == block.block_id
    assert (component["value_source"]["start_char"], component["value_source"]["end_char"]) == (
        selected_candidate.start_char, selected_candidate.end_char,
    )


def test_request_02_raw_fact_labels_and_purpose_change_narration_are_rejected() -> None:
    document = _clean_document_02()
    pack = build_request_candidate_pack(document)
    purpose_block = next(block for block in pack.blocks if block.block_id.endswith(":b20"))
    proposal_block = next(block for block in pack.blocks if block.block_id.endswith(":b28"))
    bad_purpose = RequestSourceSelectionV012.model_validate({
        "profile_id": "request-02-purpose-diff", "candidate_pack_id": pack.pack_id,
        "facts": [{
            "fact_id": "purpose_01", "field_name": "purpose_goal",
            "value_anchor": {"source_block_id": purpose_block.block_id, "anchor_text": purpose_block.text},
        }],
    })
    try:
        assemble_request_profile_v012(document, pack, bad_purpose)
    except ValueError as error:
        assert "policy-purpose span" in str(error)
    else:
        raise AssertionError("purpose change narration must not persist as purpose_goal")
    good_purpose_text = "인접 경남권 협력업체까지 지원 범위를 넓히고 안전관리 위반 이력 기업을 지원대상에서 배제하여 제도의 정책적 정합성을 높인다"
    good_purpose = RequestSourceSelectionV012.model_validate({
        "profile_id": "request-02-purpose-exact", "candidate_pack_id": pack.pack_id,
        "facts": [{
            "fact_id": "purpose_01", "field_name": "purpose_goal",
            "value_anchor": {"source_block_id": purpose_block.block_id, "anchor_text": good_purpose_text},
        }],
    })
    assert assemble_request_profile_v012(document, pack, good_purpose)["comparison_profile"]["purpose_goal"][0]["value_raw"] == good_purpose_text

    bad_label = RequestSourceSelectionV012.model_validate({
        "profile_id": "request-02-label-prefix", "candidate_pack_id": pack.pack_id,
        "facts": [{
            "fact_id": "eligibility_01", "field_name": "applicant_eligibility",
            "value_anchor": {"source_block_id": proposal_block.block_id, "anchor_text": proposal_block.text},
        }],
    })
    try:
        assemble_request_profile_v012(document, pack, bad_label)
    except ValueError as error:
        assert "proposal/year/change label prefix" in str(error)
    else:
        raise AssertionError("proposal prefix cannot persist in an applicant_eligibility Raw Fact")
    content = "상시근로자 5인 이상 300인 이하인 울산광역시 및 경상남도(양산시·김해시) 소재 자동차 부품 업종 사업주"
    good_label = RequestSourceSelectionV012.model_validate({
        "profile_id": "request-02-label-content", "candidate_pack_id": pack.pack_id,
        "facts": [{
            "fact_id": "eligibility_01", "field_name": "applicant_eligibility",
            "value_anchor": {"source_block_id": proposal_block.block_id, "anchor_text": content},
        }],
    })
    assert assemble_request_profile_v012(document, pack, good_label)["comparison_profile"]["applicant_eligibility"][0]["value_raw"] == content


def test_request_02_selection_priority_is_not_target_eligibility_or_exclusion() -> None:
    """An applicant who remains eligible but ranks later is not a target-scope Fact."""

    document = _clean_document_02()
    pack = build_request_candidate_pack(document)
    priority = (
        "동일 사업 기준 최근 2년 연속 선정된 기업은 신청 자체는 가능하나 신규 참여기업 대비 심사 시 "
        "후순위로 배정(자격 배제가 아니라 경합 시 우선순위 조정)"
    )
    anchor = _anchor(pack, priority)
    for field_name in ("applicant_eligibility", "support_target", "eligibility_conditions", "exclusions"):
        selection = RequestSourceSelectionV012.model_validate({
            "profile_id": f"request-02-priority-{field_name}", "candidate_pack_id": pack.pack_id,
            "facts": [{
                "fact_id": f"priority_{field_name}", "field_name": field_name,
                "value_anchor": anchor,
            }],
        })
        try:
            assemble_request_profile_v012(document, pack, selection)
        except ValueError as error:
            assert "selection/review priority" in str(error)
        else:
            raise AssertionError(f"selection priority must not materialize as {field_name}")


def test_request_03_purpose_goal_excludes_intervention_and_keeps_exact_outcome() -> None:
    """A purpose Fact is an outcome span, never the mechanism used to achieve it."""

    document = _clean_document_03()
    pack = build_request_candidate_pack(document)
    purpose_block = next(block for block in pack.blocks if block.block_id.endswith(":b8"))
    whole_sentence = (
        "최종 선정팀에게 지급하는 창업지원금의 지급 구조를 단계별로 나누고, 이미 지원 방식에 포함되어 있던 "
        "멘토링을 1:1·정기 방식으로 구체화하여 사업화 실행력을 높임"
    )
    assert whole_sentence in purpose_block.text
    rejected = RequestSourceSelectionV012.model_validate({
        "profile_id": "request-03-purpose-mechanism", "candidate_pack_id": pack.pack_id,
        "facts": [{
            "fact_id": "purpose_mechanism", "field_name": "purpose_goal",
            "value_anchor": {"source_block_id": purpose_block.block_id, "anchor_text": whole_sentence},
        }],
    })
    try:
        assemble_request_profile_v012(document, pack, rejected)
    except ValueError as error:
        assert "intervention/change-mechanism" in str(error)
    else:
        raise AssertionError("purpose_goal must reject an intervention-plus-outcome sentence")

    outcome = "사업화 실행력을 높임"
    accepted = RequestSourceSelectionV012.model_validate({
        "profile_id": "request-03-purpose-outcome", "candidate_pack_id": pack.pack_id,
        "facts": [{
            "fact_id": "purpose_outcome", "field_name": "purpose_goal",
            "value_anchor": {"source_block_id": purpose_block.block_id, "anchor_text": outcome},
        }],
    })
    fact = assemble_request_profile_v012(document, pack, accepted)["comparison_profile"]["purpose_goal"][0]
    assert fact["value_raw"] == outcome
    assert fact["value_source"]["source_block_id"] == purpose_block.block_id
    assert purpose_block.text[fact["value_source"]["start_char"]:fact["value_source"]["end_char"]] == outcome


def test_request_03_grant_component_is_separate_from_item_and_delivery_facets() -> None:
    """A package heading does not replace the separately evidenced grant item."""

    document = _clean_document_03()
    pack = build_request_candidate_pack(document)
    component_block = next(block for block in pack.blocks if block.block_id.endswith(":b41"))
    requested_block = next(block for block in pack.blocks if block.block_id.endswith(":b43"))
    item_block = next(block for block in pack.blocks if block.block_id.endswith(":b46"))
    assert component_block.text == "(2) 창업지원금"
    assert "창업지원금" in item_block.text

    selection = RequestSourceSelectionV012.model_validate({
        "profile_id": "request-03-grant-facets", "candidate_pack_id": pack.pack_id,
        "support_components": [{
            "support_component_id": "startup_grant", "component_kind": "support_package",
            "name_anchor": {"source_block_id": component_block.block_id, "anchor_text": "창업지원금"},
        }],
        "facts": [
            {
                "fact_id": "grant_item", "field_name": "support_items",
                "value_anchor": {"source_block_id": item_block.block_id, "anchor_text": "창업지원금"},
                "primary_component_id": "startup_grant",
            },
            {
                "fact_id": "grant_method", "field_name": "support_methods",
                "value_anchor": {"source_block_id": requested_block.block_id, "anchor_text": "분할 사후 지급"},
                "primary_component_id": "startup_grant",
            },
            {
                "fact_id": "mentoring_method", "field_name": "support_methods",
                "value_anchor": {"source_block_id": requested_block.block_id, "anchor_text": "1:1 방식"},
                "primary_component_id": "startup_grant",
            },
            {
                "fact_id": "mentoring_frequency", "field_name": "support_content",
                "value_anchor": {"source_block_id": requested_block.block_id, "anchor_text": "월 2회, 총 8회"},
                "primary_component_id": "startup_grant",
            },
            {
                "fact_id": "grant_stages", "field_name": "support_content",
                "value_anchor": {
                    "source_block_id": requested_block.block_id,
                    "anchor_text": "1단계(아이디어 검증 완료, 150만원)·2단계(사업화 완료 보고, 250만원)",
                },
                "primary_component_id": "startup_grant",
            },
        ],
    })
    profile = assemble_request_profile_v012(document, pack, selection)
    assert profile["support_components"][0]["name_raw"] == "창업지원금"
    assert profile["comparison_profile"]["support_items"][0]["value_raw"] == "창업지원금"
    assert profile["comparison_profile"]["support_items"][0]["value_source"]["source_block_id"] == item_block.block_id
    assert [row["value_raw"] for row in profile["comparison_profile"]["support_methods"]] == [
        "분할 사후 지급", "1:1 방식",
    ]
    assert [row["value_raw"] for row in profile["comparison_profile"]["support_content"]] == [
        "월 2회, 총 8회",
        "1단계(아이디어 검증 완료, 150만원)·2단계(사업화 완료 보고, 250만원)",
    ]
    assert "support_components identify named packages" in request_selection_instructions()


def test_request_03_rejects_service_as_item_and_payment_tranche_as_component() -> None:
    """A mentoring service is a method; grant instalments are not components alone."""

    document = _clean_document_03()
    pack = build_request_candidate_pack(document)
    requested_block = next(block for block in pack.blocks if block.block_id.endswith(":b43"))

    service_as_item = RequestSourceSelectionV012.model_validate({
        "profile_id": "request-03-mentoring-item", "candidate_pack_id": pack.pack_id,
        "facts": [{
            "fact_id": "mentoring_item", "field_name": "support_items",
            "value_anchor": {"source_block_id": requested_block.block_id, "anchor_text": "멘토링"},
        }],
    })
    try:
        assemble_request_profile_v012(document, pack, service_as_item)
    except ValueError as error:
        assert "named support service" in str(error)
    else:
        raise AssertionError("mentoring must not materialize as support_items")

    service_as_method = RequestSourceSelectionV012.model_validate({
        "profile_id": "request-03-mentoring-method", "candidate_pack_id": pack.pack_id,
        "facts": [{
            "fact_id": "mentoring_method", "field_name": "support_methods",
            "value_anchor": {"source_block_id": requested_block.block_id, "anchor_text": "멘토링"},
        }],
    })
    assert assemble_request_profile_v012(document, pack, service_as_method)["comparison_profile"]["support_methods"][0]["value_raw"] == "멘토링"

    tranche_as_component = RequestSourceSelectionV012.model_validate({
        "profile_id": "request-03-tranche-component", "candidate_pack_id": pack.pack_id,
        "support_components": [{
            "support_component_id": "grant_stage_1", "component_kind": "stage_support",
            "name_anchor": {"source_block_id": requested_block.block_id, "anchor_text": "1단계"},
        }],
        "facts": [{
            "fact_id": "stage_amount", "field_name": "support_scale",
            "value_anchor": {"source_block_id": requested_block.block_id, "anchor_text": "150만원"},
            "primary_component_id": "grant_stage_1",
        }],
    })
    try:
        assemble_request_profile_v012(document, pack, tranche_as_component)
    except ValueError as error:
        assert "payment tranche or amount alone" in str(error)
    else:
        raise AssertionError("payment instalment must not be an independent component")


def test_request_03_mentoring_service_has_its_own_component_links() -> None:
    """A separately formatted mentoring service must not be scoped to a grant."""

    document = _clean_document_03()
    pack = build_request_candidate_pack(document)
    requested_block = next(block for block in pack.blocks if block.block_id.endswith(":b43"))
    selection = RequestSourceSelectionV012.model_validate({
        "profile_id": "request-03-mentoring-component", "candidate_pack_id": pack.pack_id,
        "support_components": [{
            "support_component_id": "mentoring", "component_kind": "support_package",
            "name_anchor": {"source_block_id": requested_block.block_id, "anchor_text": "멘토링"},
        }],
        "facts": [
            {
                "fact_id": "mentoring_method", "field_name": "support_methods",
                "value_anchor": {"source_block_id": requested_block.block_id, "anchor_text": "멘토링"},
                "primary_component_id": "mentoring",
            },
            {
                "fact_id": "mentoring_format", "field_name": "support_methods",
                "value_anchor": {"source_block_id": requested_block.block_id, "anchor_text": "1:1 방식"},
                "primary_component_id": "mentoring",
            },
            {
                "fact_id": "mentoring_cadence", "field_name": "support_content",
                "value_anchor": {"source_block_id": requested_block.block_id, "anchor_text": "월 2회, 총 8회"},
                "primary_component_id": "mentoring",
            },
        ],
    })
    profile = assemble_request_profile_v012(document, pack, selection)
    assert profile["support_components"][0]["name_raw"] == "멘토링"
    assert [(row["value_raw"], row["primary_component_id"]) for row in profile["comparison_profile"]["support_methods"]] == [
        ("멘토링", "mentoring"), ("1:1 방식", "mentoring"),
    ]
    assert profile["comparison_profile"]["support_content"][0]["primary_component_id"] == "mentoring"


def test_request_04_support_activity_does_not_create_support_content_fallback() -> None:
    """A safely classified activity must not be duplicated in the escape hatch."""

    document = _clean_document_04()
    pack = build_request_candidate_pack(document)
    activity = "디지털전환 초기진단(현장 방문 컨설팅) 및 진단결과보고서 발급"
    selection = RequestSourceSelectionV012.model_validate({
        "profile_id": "request-04-activity-only", "candidate_pack_id": pack.pack_id,
        "facts": [{
            "fact_id": "activity_01", "field_name": "support_activities",
            "value_anchor": _anchor(pack, activity),
        }],
    })
    profile = assemble_request_profile_v012(document, pack, selection)
    assert profile["comparison_profile"]["support_activities"][0]["value_raw"] == activity
    assert profile["comparison_profile"]["support_content"] == []
    assert "support_content is a narrow escape hatch" in request_selection_instructions()


def test_request_04_program_period_dotted_year_month_candidates_keep_both_years() -> None:
    """Dotted Korean month endpoints must not silently lose the first year."""

    document = _clean_document_04()
    pack = build_request_candidate_pack(document)
    candidates = [
        candidate for candidate in build_value_span_candidates(pack)
        if candidate.candidate_kind == "program_period_date_range"
    ]
    actual = [candidate for candidate in candidates if candidate.source_block_id.endswith(":b41")]
    assert [candidate.value_raw for candidate in actual] == ["2027. 3월 ~ 2028. 2월"]
    assert "3월 ~ 2028. 2" not in [candidate.value_raw for candidate in candidates]

    selection = RequestSourceSelectionV012.model_validate({
        "profile_id": "request-04-dotted-period", "candidate_pack_id": pack.pack_id,
        "facts": [{
            "fact_id": "period_01", "field_name": "program_period",
            "value_anchor": {"value_span_candidate_id": actual[0].value_span_candidate_id},
        }],
    })
    fact = assemble_request_profile_v012(document, pack, selection)["comparison_profile"]["program_period"][0]
    assert fact["value_raw"] == "2027. 3월 ~ 2028. 2월"
    assert fact["value_source"]["source_block_id"].endswith(":b41")


def test_program_period_yearless_month_range_remains_an_eligible_candidate() -> None:
    """The dotted-year guard must not reject a genuinely year-less range."""

    pack = CandidatePack(
        pack_id="request-yearless-period-pack", notice_id="PREREVIEW-TEST-YEARLESS",
        extraction_scope="document", question="yearless period regression",
        generator="semantic_structuring.common_ir_v1", generator_version="1",
        common_ir_document_id="request:PREREVIEW-TEST-YEARLESS",
        blocks=[SourceBlock(
            block_id="request:yearless#p0", text="지원기간: 3월~5월",
            relation=SourceRelation.CANDIDATE, common_ir_block_id="request:yearless",
            common_ir_occurrence_ids=("occ:yearless",),
        )],
    )
    candidates = [
        candidate.value_raw for candidate in build_value_span_candidates(pack)
        if candidate.candidate_kind == "program_period_date_range"
    ]
    assert candidates == ["3월~5월"]


def test_request_02_component_local_beneficiaries_require_primary_component_links() -> None:
    """The selector, not the server, supplies four exact component-local facts."""

    document = _clean_document_02()
    pack = build_request_candidate_pack(document)
    component_rows = [
        ("employment_incentive", "안전관리 인력 채용장려금 지원", "사업주"),
        ("additional_work", "안전관리 업무 추가 수행 지원", "사업주"),
        ("long_term_retention", "안전관리자 장기근속 지원", "근로자"),
        ("appointment_settlement", "안전관리자 선임 정착 지원", "사업주"),
    ]
    components = []
    facts = []
    for offset, (component_id, heading, beneficiary) in enumerate(component_rows):
        heading_block = next(block for block in pack.blocks if block.text == heading)
        recipient_block = next(
            block for block in pack.blocks
            if block.source_order is not None and heading_block.source_order is not None
            and block.source_order > heading_block.source_order and block.text == f"실제 수혜자: {beneficiary}"
        )
        components.append({
            "support_component_id": component_id, "component_kind": "support_package",
            "name_anchor": {"source_block_id": heading_block.block_id, "anchor_text": heading},
        })
        facts.append({
            "fact_id": f"beneficiary_{offset + 1}", "field_name": "beneficiary",
            "value_anchor": {"source_block_id": recipient_block.block_id, "anchor_text": beneficiary},
            "primary_component_id": component_id,
        })
    selected = RequestSourceSelectionV012.model_validate({
        "profile_id": "request-02-component-beneficiaries", "candidate_pack_id": pack.pack_id,
        "support_components": components, "facts": facts,
    })
    profile = assemble_request_profile_v012(document, pack, selected)
    beneficiaries = profile["comparison_profile"]["beneficiary"]
    assert [(row["primary_component_id"], row["value_raw"]) for row in beneficiaries] == [
        ("employment_incentive", "사업주"),
        ("additional_work", "사업주"),
        ("long_term_retention", "근로자"),
        ("appointment_settlement", "사업주"),
    ]

    missing_link = selected.model_dump(mode="json")
    missing_link["facts"][0].pop("primary_component_id")
    try:
        assemble_request_profile_v012(document, pack, RequestSourceSelectionV012.model_validate(missing_link))
    except ValueError as error:
        assert "component-local beneficiary" in str(error)
    else:
        raise AssertionError("an explicit component-local beneficiary must link to its component")

    missing_fact = selected.model_dump(mode="json")
    missing_fact["facts"].pop(0)
    try:
        assemble_request_profile_v012(document, pack, RequestSourceSelectionV012.model_validate(missing_fact))
    except ValueError as error:
        assert "requires one beneficiary Fact" in str(error)
    else:
        raise AssertionError("selected component with an explicit local beneficiary row must not omit the Fact")

    wrong_link = selected.model_dump(mode="json")
    wrong_link["facts"][0]["primary_component_id"] = "additional_work"
    try:
        assemble_request_profile_v012(document, pack, RequestSourceSelectionV012.model_validate(wrong_link))
    except ValueError as error:
        assert "must reference its nearest explicit support_component" in str(error)
    else:
        raise AssertionError("a component-local beneficiary cannot link to another component")

    duplicate = selected.model_dump(mode="json")
    duplicate_fact = deepcopy(duplicate["facts"][0])
    duplicate_fact["fact_id"] = "beneficiary_duplicate"
    duplicate["facts"].append(duplicate_fact)
    try:
        assemble_request_profile_v012(document, pack, RequestSourceSelectionV012.model_validate(duplicate))
    except ValueError as error:
        assert "requires exactly one beneficiary Fact" in str(error)
    else:
        raise AssertionError("a local beneficiary row cannot create duplicate component facts")

    # A selected heading without a local beneficiary row does not synthesize a
    # beneficiary Fact. The bidirectional guard is limited to explicit rows.
    no_local_component = RequestSourceSelectionV012.model_validate({
        "profile_id": "request-02-component-no-local-beneficiary", "candidate_pack_id": pack.pack_id,
        "support_components": [{
            "support_component_id": "whole_period_section", "component_kind": "support_package",
            "name_anchor": _anchor(pack, "사업 전체기간"),
        }],
    })
    no_local_profile = assemble_request_profile_v012(document, pack, no_local_component)
    assert no_local_profile["comparison_profile"]["beneficiary"] == []


def test_runner_without_selection_writes_template_not_profile(tmp_path: Path) -> None:
    output = tmp_path / "selection-template.json"
    common_ir = ROOT / "PREREVIEW-TEST-2027-01.common_ir_v1.json"
    subprocess.run([
        sys.executable, "-m", "semantic_structuring.run_request_profile_v012",
        "--common-ir", str(common_ir), "--profile-id", "request-template-test", "--output", str(output),
    ], check=True)
    artifact = json.loads(output.read_text(encoding="utf-8"))
    assert artifact["artifact_kind"] == "request_source_selection_template"
    assert artifact["remote_model_called"] is False
    assert "request_type" not in artifact
    # The runner deliberately strips its own artifact-only fields when the
    # template returns as a future model-input/selection document.
    profile_output = tmp_path / "profile.json"
    subprocess.run([
        sys.executable, "-m", "semantic_structuring.run_request_profile_v012",
        "--common-ir", str(common_ir), "--profile-id", "request-template-test",
        "--selection", str(output), "--output", str(profile_output),
    ], check=True)
    assert json.loads(profile_output.read_text(encoding="utf-8"))["profile_type"] == "pre_review_request"
    mismatch = subprocess.run([
        sys.executable, "-m", "semantic_structuring.run_request_profile_v012",
        "--common-ir", str(common_ir), "--profile-id", "different-id",
        "--selection", str(output), "--output", str(tmp_path / "mismatch.json"),
    ], capture_output=True, text=True)
    assert mismatch.returncode != 0


def test_runner_dry_run_writes_no_gold_structured_output_payload(tmp_path: Path) -> None:
    output = tmp_path / "dry-run.json"
    common_ir = ROOT / "PREREVIEW-TEST-2027-01.common_ir_v1.json"
    subprocess.run([
        sys.executable, "-m", "semantic_structuring.run_request_profile_v012",
        "--common-ir", str(common_ir), "--profile-id", "request-dry-run-test",
        "--dry-run", "--output", str(output),
    ], check=True)
    artifact = json.loads(output.read_text(encoding="utf-8"))
    assert artifact["artifact_kind"] == "request_source_selection_dry_run"
    assert artifact["remote_model_called"] is False
    assert artifact["input"]["profile_id"] == "request-dry-run-test"
    assert artifact["input"]["candidate_pack"]["blocks"]
    assert artifact["input"]["read_only_context"]["request_type"] == {
        "selected_code": "program_content_change", "label": "사업내용 변경",
    }
    assert "Gold" in artifact["instructions"]
    assert "never a diagram/layout label" in artifact["instructions"]
    assert "field_states is the one optional exception" in artifact["instructions"]
    assert "gold_change_notes" not in json.dumps(artifact, ensure_ascii=False)


def test_runner_writes_secret_free_failure_artifact_for_local_materialization_error(tmp_path: Path) -> None:
    document = _document()
    pack = build_request_candidate_pack(document)
    selection = {
        "profile_id": "request-local-failure", "candidate_pack_id": pack.pack_id,
        "facts": [{
            "fact_id": "bad_anchor", "field_name": "purpose_goal",
            "value_anchor": {"source_block_id": "not-in-pack", "anchor_text": "nope"},
        }],
    }
    selection_path = tmp_path / "bad-selection.json"
    output = tmp_path / "profile.json"
    selection_path.write_text(json.dumps(selection, ensure_ascii=False), encoding="utf-8")
    result = subprocess.run([
        sys.executable, "-m", "semantic_structuring.run_request_profile_v012",
        "--common-ir", str(ROOT / "PREREVIEW-TEST-2027-01.common_ir_v1.json"),
        "--profile-id", "request-local-failure", "--selection", str(selection_path), "--output", str(output),
    ], capture_output=True, text=True)
    assert result.returncode != 0
    failure = json.loads(output.with_suffix(".failure.json").read_text(encoding="utf-8"))
    assert failure["stage"] == "request_server_materialization"
    assert failure["response_text_stored"] is False


def test_server_guided_remote_repair_retries_once_without_raw_response_storage() -> None:
    document = _document()
    pack = build_request_candidate_pack(document)
    bad = RequestSourceSelectionV012.model_validate({
        "profile_id": "request-repair-test", "candidate_pack_id": pack.pack_id,
        "facts": [{
            "fact_id": "goal_01", "field_name": "purpose_goal",
            "value_anchor": {"source_block_id": pack.blocks[0].block_id, "anchor_text": "not-a-literal-candidate-span"},
        }],
    })
    good = RequestSourceSelectionV012.model_validate({
        "profile_id": "request-repair-test", "candidate_pack_id": pack.pack_id,
        "facts": [{
            "fact_id": "goal_01", "field_name": "purpose_goal",
            "value_anchor": _anchor(pack, "사회연대경제 청년 일경험 사업의 참여기업·참여청년 규모를 확대"),
        }],
    })
    calls = []

    def selector(prior, errors):
        calls.append((prior, errors))
        return (bad, {"attempt": 0}) if prior is None else (good, {"attempt": 1})

    profile, final_selection, usage, diagnostics = select_and_materialize_with_repairs(
        selector, document, pack, "request-repair-test", max_repairs=1, model_id="mock-luna",
    )
    assert profile["comparison_profile"]["purpose_goal"][0]["value_raw"] == good.facts[0].value_anchor.anchor_text
    assert final_selection == good
    assert usage == [{"attempt": 0}, {"attempt": 1}]
    assert len(diagnostics) == 1 and "occur exactly once" in diagnostics[0]["validation_error"]
    assert calls[1][0] == bad
    assert calls[1][1] == [diagnostics[0]["validation_error"]]


def test_remote_materialization_failure_keeps_parsed_selection_without_raw_response() -> None:
    document = _document()
    pack = build_request_candidate_pack(document)
    bad = RequestSourceSelectionV012.model_validate({
        "profile_id": "request-failure-artifact", "candidate_pack_id": pack.pack_id,
        "facts": [{
            "fact_id": "bad_anchor", "field_name": "purpose_goal",
            "value_anchor": {"source_block_id": pack.blocks[0].block_id, "anchor_text": "not-a-literal-span"},
        }],
    })
    try:
        select_and_materialize_with_repairs(
            lambda prior, errors: (bad, {"attempt": 0}),
            document, pack, "request-failure-artifact", max_repairs=0, model_id="mock-luna",
        )
    except RequestMaterializationError as error:
        artifact = _failure_selection_artifact(error, pack, document, profile_id="request-failure-artifact")
    else:
        raise AssertionError("unrepairable parsed selection must raise materialization error")
    assert artifact["artifact_kind"] == "request_source_selection_failure_artifact"
    assert artifact["selection"] == bad.model_dump(mode="json")
    assert artifact["candidate_pack_lineage"]["candidate_pack_id"] == pack.pack_id
    assert artifact["validation_diagnostics"]
    assert artifact["response_text_stored"] is False


def test_remote_parse_failure_preserves_usage_without_raw_response() -> None:
    """A repair response can fail parsing without losing either call's usage."""

    document = _document()
    pack = build_request_candidate_pack(document)
    bad = RequestSourceSelectionV012.model_validate({
        "profile_id": "request-parse-failure-artifact", "candidate_pack_id": pack.pack_id,
        "facts": [{
            "fact_id": "bad_anchor", "field_name": "purpose_goal",
            "value_anchor": {"source_block_id": pack.blocks[0].block_id, "anchor_text": "not-a-literal-span"},
        }],
    })
    parse_error = ValueError("facts.13.value_anchor: redundant locator")

    def selector(prior, errors):
        if prior is None:
            return bad, {"attempt": 0, "input_tokens": 10, "output_tokens": 2, "total_tokens": 12}
        raise RequestSourceSelectionParseError(
            parse_error,
            call_usage={"attempt": 0, "input_tokens": 20, "output_tokens": 3, "total_tokens": 23},
            repair=True,
        )

    try:
        select_and_materialize_with_repairs(
            selector, document, pack, "request-parse-failure-artifact", max_repairs=1, model_id="mock-luna",
        )
    except RequestSourceSelectionParseError as error:
        artifact = _selection_parse_failure_artifact(
            error, pack, document, profile_id="request-parse-failure-artifact",
        )
    else:
        raise AssertionError("parse failure after a repair response must be observable")

    assert artifact["artifact_kind"] == "request_source_selection_parse_failure_artifact"
    assert artifact["retry_count"] == 1
    assert artifact["usage"] == [
        {"attempt": 0, "input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
        {"attempt": 0, "input_tokens": 20, "output_tokens": 3, "total_tokens": 23},
    ]
    assert artifact["response_text_stored"] is False
    assert "selection" not in artifact
    assert artifact["validation_diagnostics"][0]["repair_attempt"] == 2


def test_remote_lifecycle_records_success_failure_and_artifact_write_error(tmp_path: Path) -> None:
    """Offline orchestration remains observable without a remote response body."""

    document = _document()
    pack = build_request_candidate_pack(document)
    good = RequestSourceSelectionV012.model_validate({
        "profile_id": "request-lifecycle-success", "candidate_pack_id": pack.pack_id,
        "facts": [{
            "fact_id": "goal_01", "field_name": "purpose_goal",
            "value_anchor": _anchor(pack, "사회연대경제 청년 일경험 사업의 참여기업·참여청년 규모를 확대"),
        }],
    })
    events: list[dict] = []

    def observe(stage: str, details: dict) -> None:
        events.append({"stage": stage, **details})

    profile, _, _, _ = select_and_materialize_with_repairs(
        lambda prior, errors: (good, {"mock": True}),
        document, pack, "request-lifecycle-success", max_repairs=0, model_id="mock-luna",
        lifecycle=observe,
    )
    assert profile["profile_type"] == "pre_review_request"
    assert [event["stage"] for event in events] == [
        "selection_attempt_started", "selection_attempt_completed",
        "server_materialization_started", "server_materialization_completed",
    ]

    bad = RequestSourceSelectionV012.model_validate({
        "profile_id": "request-lifecycle-failure", "candidate_pack_id": pack.pack_id,
        "facts": [{
            "fact_id": "bad_01", "field_name": "purpose_goal",
            "value_anchor": {"source_block_id": pack.blocks[0].block_id, "anchor_text": "missing literal"},
        }],
    })
    failed_events: list[dict] = []
    try:
        select_and_materialize_with_repairs(
            lambda prior, errors: (bad, {"mock": True}),
            document, pack, "request-lifecycle-failure", max_repairs=0, model_id="mock-luna",
            lifecycle=lambda stage, details: failed_events.append({"stage": stage, **details}),
        )
    except RequestMaterializationError:
        pass
    else:
        raise AssertionError("bad source selection must remain observable as a failure")
    assert failed_events[-1]["stage"] == "server_materialization_failed"
    assert "missing literal" not in json.dumps(failed_events, ensure_ascii=False)

    write_events: list[dict] = []
    def broken_writer(path: Path, payload: dict) -> None:
        raise OSError("disk write unavailable")
    try:
        _write_artifact_with_lifecycle(
            broken_writer, tmp_path / "broken.json", {"safe": True}, artifact_name="profile",
            observer=lambda stage, details: write_events.append({"stage": stage, **details}),
        )
    except OSError:
        pass
    else:
        raise AssertionError("artifact write error must be re-raised")
    assert [event["stage"] for event in write_events] == ["artifact_write_started", "artifact_write_failed"]
    assert write_events[-1]["error_type"] == "OSError"

    status = _new_remote_run_status(
        profile_id="request-lifecycle-status", pack=pack, document=document, model_id="mock-luna",
        profile_path=tmp_path / "profile.json", selection_path=tmp_path / "selection.json",
        failure_path=tmp_path / "profile.failure.json", selection_failure_path=tmp_path / "selection.failure.json",
    )
    observer = _status_observer(status)
    observer("api_response_received", {"api_response_status": "completed", "api_response_id": "resp_safe"})
    assert status["api_key_stored"] is False and status["response_text_stored"] is False
    assert status["events"][0]["api_response_id"] == "resp_safe"


def test_request_profile_materializes_exact_spans_and_checkbox_dual_anchor() -> None:
    document = _document()
    pack = build_request_candidate_pack(document)
    checkbox_block = next(block for block in pack.blocks if "☑ 사업내용 변경" in block.text)
    selection = RequestSourceSelectionV012.model_validate({
        "profile_id": "request-profile-test-01",
        "candidate_pack_id": pack.pack_id,
        "facts": [
            {"fact_id": "goal_01", "field_name": "purpose_goal", "value_anchor": _anchor(pack, "사회연대경제 청년 일경험 사업의 참여기업·참여청년 규모를 확대")},
            {"fact_id": "period_01", "field_name": "program_period", "value_anchor": _program_period_candidate_anchor(pack, "2027년 1월~12월")},
        ],
    })
    profile = assemble_request_profile_v012(document, pack, selection)
    goal = profile["comparison_profile"]["purpose_goal"][0]
    source = goal["value_source"]
    source_text = {block.block_id: block.text for block in pack.blocks}[source["source_block_id"]]
    assert goal["value_raw"] == source_text[source["start_char"]:source["end_char"]]
    assert profile["request_type"]["selection_source"]["glyph_raw"] == "☑"
    assert profile["field_states"][0]["field_name"] == "purpose_goal"


def test_request_context_uses_same_exact_span_and_partial_state_contract() -> None:
    document = _document()
    pack = build_request_candidate_pack(document)
    selection = RequestSourceSelectionV012.model_validate({
        "profile_id": "request-profile-context",
        "candidate_pack_id": pack.pack_id,
        "facts": [{
            "fact_id": "need_01", "field_name": "business_need", "status": "partial",
            "value_anchor": _anchor(pack, "지원기간(5개월)이 짧아 청년의 직무숙련 형성 이전에 사업이 종료되는 한계"),
        }],
    })
    profile = assemble_request_profile_v012(document, pack, selection)
    fact = profile["request_context"]["business_need"][0]
    source = fact["value_source"]
    assert fact["value_raw"] == {b.block_id: b.text for b in pack.blocks}[source["source_block_id"]][source["start_char"]:source["end_char"]]
    state = next(row for row in profile["field_states"] if row["field_name"] == "business_need")
    assert state == {"field_name": "business_need", "status": "partial", "fact_ids": ["need_01"]}


def test_request_01_support_method_and_program_period_are_exact_value_spans() -> None:
    """A mixed workflow is not a support method; payment/date spans are."""

    document = _document()
    pack = build_request_candidate_pack(document)
    workflow = _anchor(pack, "참여기업 선정 → 청년 채용·배치 → 월별 출석·활동 확인 후 사후 정산 지급 (변경 없음)")
    bad_method = RequestSourceSelectionV012.model_validate({
        "profile_id": "request-01-workflow-bad", "candidate_pack_id": pack.pack_id,
        "facts": [{"fact_id": "method_workflow", "field_name": "support_methods", "value_anchor": workflow}],
    })
    try:
        assemble_request_profile_v012(document, pack, bad_method)
    except ValueError as error:
        assert "workflow span" in str(error)
    else:
        raise AssertionError("selection-to-payment workflow cannot become support_methods")

    payment = _anchor(pack, "사후 정산 지급")
    good_method = RequestSourceSelectionV012.model_validate({
        "profile_id": "request-01-payment-good", "candidate_pack_id": pack.pack_id,
        "facts": [{"fact_id": "method_payment", "field_name": "support_methods", "value_anchor": payment}],
    })
    profile = assemble_request_profile_v012(document, pack, good_method)
    assert profile["comparison_profile"]["support_methods"][0]["value_raw"] == "사후 정산 지급"

    full_period = _anchor(pack, "2027년 1월~12월(12개월)")
    bad_period = RequestSourceSelectionV012.model_validate({
        "profile_id": "request-01-period-bad", "candidate_pack_id": pack.pack_id,
        "facts": [{"fact_id": "period_full", "field_name": "program_period", "value_anchor": full_period}],
    })
    try:
        assemble_request_profile_v012(document, pack, bad_period)
    except ValueError as error:
        assert "program_period_date_range" in str(error)
    else:
        raise AssertionError("program_period cannot include duration parentheses")

    date_range = _program_period_candidate_anchor(pack, "2027년 1월~12월")
    good_period = RequestSourceSelectionV012.model_validate({
        "profile_id": "request-01-period-good", "candidate_pack_id": pack.pack_id,
        "facts": [{"fact_id": "period_date", "field_name": "program_period", "value_anchor": date_range}],
    })
    period_profile = assemble_request_profile_v012(document, pack, good_period)
    assert period_profile["comparison_profile"]["program_period"][0]["value_raw"] == "2027년 1월~12월"


def test_request_01_prompt_requires_recall_of_explicit_changed_support_scale_values() -> None:
    """Recall policy is prompt-only: no server schema/validator expansion."""

    document = _document()
    pack = build_request_candidate_pack(document)
    changed_block = next(block for block in pack.blocks if "선정규모 12개사·기업당4명" in block.text)
    assert "20개사·기업당6명" in changed_block.text
    prompt = request_selection_instructions()
    assert "변경 후, 요청안, or 확산사업" in prompt
    assert "must select each as support_scale" in prompt
    assert "동일(변경 없음)" in prompt
    assert "Education 4회" in prompt


def test_duplicate_span_across_request_fields_is_rejected() -> None:
    document = _document()
    pack = build_request_candidate_pack(document)
    anchor = _program_period_candidate_anchor(pack, "2027년 1월~12월")
    selection = RequestSourceSelectionV012.model_validate({
        "profile_id": "request-profile-test-duplicate",
        "candidate_pack_id": pack.pack_id,
        "facts": [
            {"fact_id": "program_01", "field_name": "program_period", "value_anchor": anchor},
            {"fact_id": "support_01", "field_name": "support_period", "value_anchor": anchor},
        ],
    })
    try:
        assemble_request_profile_v012(document, pack, selection)
    except ValueError as error:
        assert "duplicate exact source span" in str(error)
    else:
        raise AssertionError("duplicate source span must be rejected")


def test_duplicate_span_between_context_and_comparison_is_rejected() -> None:
    document = _document()
    pack = build_request_candidate_pack(document)
    anchor = _program_period_candidate_anchor(pack, "2027년 1월~12월")
    selection = RequestSourceSelectionV012.model_validate({
        "profile_id": "request-profile-context-duplicate", "candidate_pack_id": pack.pack_id,
        "facts": [
            {"fact_id": "program_01", "field_name": "program_period", "value_anchor": anchor},
            {"fact_id": "plan_01", "field_name": "implementation_plan", "value_anchor": anchor},
        ],
    })
    try:
        assemble_request_profile_v012(document, pack, selection)
    except ValueError as error:
        assert "duplicate exact source span" in str(error)
    else:
        raise AssertionError("context and comparison cannot duplicate a source span")


def test_duplicate_span_between_comparison_and_delivery_method_is_rejected() -> None:
    document = _document()
    pack = build_request_candidate_pack(document)
    anchor = _program_period_candidate_anchor(pack, "2027년 1월~12월")
    selection = RequestSourceSelectionV012.model_validate({
        "profile_id": "request-profile-method-duplicate", "candidate_pack_id": pack.pack_id,
        "facts": [{"fact_id": "program_01", "field_name": "program_period", "value_anchor": anchor}],
        "delivery_methods": [{"fact_id": "method_01", "value_anchor": anchor}],
    })
    try:
        assemble_request_profile_v012(document, pack, selection)
    except ValueError as error:
        assert "duplicate exact source span" in str(error)
    else:
        raise AssertionError("comparison and delivery method cannot duplicate a source span")


def test_request_type_is_server_resolved_and_rejects_multiple_checked_choices() -> None:
    document = _document()
    pack = build_request_candidate_pack(document)
    checkbox_block = next(block for block in pack.blocks if "☑ 사업내용 변경" in block.text)
    # request_type is deliberately outside the model schema: no Luna repair
    # can change a server-owned checkbox selection.
    try:
        RequestSourceSelectionV012.model_validate({
            "profile_id": "request-type-injected", "candidate_pack_id": pack.pack_id,
            "request_type": {"selected_code": "detail_program_new"},
        })
    except ValueError as error:
        assert "request_type" in str(error)
    else:
        raise AssertionError("LLM selection cannot inject request_type")

    malformed = checkbox_block.model_copy(update={"text": checkbox_block.text.replace("☑ 사업내용 변경", "☑ 잘못된 라벨")})
    malformed_pack = pack.model_copy(update={"blocks": [malformed if b.block_id == malformed.block_id else b for b in pack.blocks]})
    try:
        assemble_request_profile_v012(document, malformed_pack, RequestSourceSelectionV012.model_validate({
            "profile_id": "request-type-label-mismatch", "candidate_pack_id": malformed_pack.pack_id,
        }))
    except ValueError as error:
        assert "immediately associated" in str(error)
    else:
        raise AssertionError("server must reject checked glyph without canonical label")

    unchecked = checkbox_block.model_copy(update={"text": checkbox_block.text.replace("☑", "☐")})
    unchecked_pack = pack.model_copy(update={"blocks": [unchecked if b.block_id == unchecked.block_id else b for b in pack.blocks]})
    try:
        assemble_request_profile_v012(document, unchecked_pack, RequestSourceSelectionV012.model_validate({
            "profile_id": "request-type-unchecked", "candidate_pack_id": unchecked_pack.pack_id,
        }))
    except ValueError as error:
        assert "option container" in str(error) or "exactly one checked" in str(error)
    else:
        raise AssertionError("zero checked request types must fail closed")

    # Reuse the candidate pack but make the request-type container genuinely
    # ambiguous.  No arbitrary single selected code may survive this state.
    changed = checkbox_block.model_copy(update={"text": checkbox_block.text.replace("☐ 내역사업 신설", "☑ 내역사업 신설")})
    ambiguous_pack = pack.model_copy(update={"blocks": [changed if b.block_id == changed.block_id else b for b in pack.blocks]})
    ambiguous = RequestSourceSelectionV012.model_validate({"profile_id": "request-type-ambiguous", "candidate_pack_id": ambiguous_pack.pack_id})
    try:
        assemble_request_profile_v012(document, ambiguous_pack, ambiguous)
    except ValueError as error:
        assert "exactly one checked option" in str(error)
    else:
        raise AssertionError("multiple checked request types must remain unresolved")


def test_request_type_accepts_filled_square_without_rewriting_source_span() -> None:
    document = _document()
    pack = build_request_candidate_pack(document)
    checkbox_block = next(block for block in pack.blocks if "☑ 사업내용 변경" in block.text)
    filled = checkbox_block.model_copy(update={
        "text": checkbox_block.text.replace("☑ 사업내용 변경", "■ 사업내용 변경"),
    })
    filled_pack = pack.model_copy(update={
        "blocks": [filled if block.block_id == filled.block_id else block for block in pack.blocks],
    })

    request_type = resolve_request_type_from_candidate_pack(filled_pack)

    assert request_type["selected_code"] == "program_content_change"
    assert request_type["selection_source"]["glyph_raw"] == "■"
    source = request_type["selection_source"]
    assert filled.text[source["start_char"]:source["end_char"]] == "■"


def test_request_type_sub_program_resolves_label_from_selected_glyph_not_global_substring() -> None:
    document = _document()
    pack = build_request_candidate_pack(document)
    checkbox_block = next(block for block in pack.blocks if "☑ 사업내용 변경" in block.text)
    sub_selected = checkbox_block.model_copy(update={
        "text": checkbox_block.text
        .replace("☐ 내역사업 신설", "☑ 내역사업 신설")
        .replace("☑ 사업내용 변경", "☐ 사업내용 변경"),
    })
    sub_pack = pack.model_copy(update={"blocks": [sub_selected if b.block_id == sub_selected.block_id else b for b in pack.blocks]})
    assert sub_selected.text.count("내역사업 신설") == 2  # includes 내내역사업 신설
    selection = RequestSourceSelectionV012.model_validate({"profile_id": "request-type-sub-program", "candidate_pack_id": sub_pack.pack_id})
    profile = assemble_request_profile_v012(document, sub_pack, selection)
    request_type = profile["request_type"]
    assert request_type["value_raw"] == "내역사업 신설"
    source = request_type["value_source"]
    assert sub_selected.text[source["start_char"]:source["end_char"]] == "내역사업 신설"


def test_candidate_pack_excludes_whole_table_text_but_retains_cell_paragraphs() -> None:
    document = json.loads(TABLE_COMMON_IR.read_text(encoding="utf-8"))
    pack = build_request_candidate_pack(document)
    assert all(block.block_kind != "table" for block in pack.blocks)
    assert any(block.common_ir_cell_id is not None for block in pack.blocks)


def test_delivery_relation_keeps_each_member_provenance() -> None:
    document = _document()
    pack = build_request_candidate_pack(document)
    block = next(block for block in pack.blocks if "전담기관" in block.text and "위탁" in block.text)
    selection = RequestSourceSelectionV012.model_validate({
        "profile_id": "request-profile-test-delivery",
        "candidate_pack_id": pack.pack_id,
        "delivery_relations": [{
            "delivery_relation_id": "delivery_01",
            "actor_anchor": {"source_block_id": block.block_id, "anchor_text": "가상 사회연대경제진흥원"},
            "role_anchor": {"source_block_id": block.block_id, "anchor_text": "전담기관"},
            "actions": [{"value_anchor": {"source_block_id": block.block_id, "anchor_text": "총괄"}, "canonical_action": "manage"}],
            "relation_container": {
                "kind": "paragraph", "source_block_id": block.block_id, "anchor_text": block.text,
                "common_ir_block_id": block.common_ir_block_id,
            },
            "canonical_actor_type": "public_agency",
            "canonical_role": "dedicated_agency",
        }],
    })
    profile = assemble_request_profile_v012(document, pack, selection)
    relation = profile["comparison_profile"]["delivery_relations"][0]
    assert relation["actor"]["evidence"]
    assert relation["role"]["evidence"]
    assert relation["actions"][0]["evidence"]
    assert relation["relation_container"]["common_ir_block_id"] == block.common_ir_block_id


def test_delivery_paragraph_container_rejects_mismatched_common_ir_hint() -> None:
    document = _document()
    pack = build_request_candidate_pack(document)
    block = next(block for block in pack.blocks if "전담기관" in block.text and "위탁" in block.text)
    selection = RequestSourceSelectionV012.model_validate({
        "profile_id": "request-profile-delivery-bad-hint", "candidate_pack_id": pack.pack_id,
        "delivery_relations": [{
            "delivery_relation_id": "delivery_01",
            "actor_anchor": {"source_block_id": block.block_id, "anchor_text": "가상 사회연대경제진흥원"},
            "role_anchor": {"source_block_id": block.block_id, "anchor_text": "전담기관"},
            "relation_container": {
                "kind": "paragraph", "source_block_id": block.block_id, "anchor_text": block.text,
                "common_ir_block_id": "wrong:common-ir-block",
            },
        }],
    })
    try:
        assemble_request_profile_v012(document, pack, selection)
    except ValueError as error:
        assert "common_ir_block_id does not match" in str(error)
    else:
        raise AssertionError("paragraph Common IR hint must match immutable source provenance")


def test_delivery_relation_allows_distinct_cells_in_one_explicit_table_row() -> None:
    document = _document()
    # This isolates the server-side table-row contract: actor and role are
    # different candidate cells, but share explicit Common IR table+row.
    pack = CandidatePack(
        pack_id="request-table-row-pack", notice_id="PREREVIEW-TEST-2027-01",
        extraction_scope="document", question="test table row",
        generator="semantic_structuring.common_ir_v1", generator_version="1",
        common_ir_document_id="request:PREREVIEW-TEST-2027-01",
        blocks=[
            SourceBlock(block_id="table:roles#r2c0p0", text="가상 진흥원", relation=SourceRelation.CANDIDATE,
                        common_ir_block_id="table:roles", common_ir_cell_id="table:roles:c0", common_ir_occurrence_ids=("occ:actor",)),
            SourceBlock(block_id="table:roles#r2c1p0", text="전담기관", relation=SourceRelation.CANDIDATE,
                        common_ir_block_id="table:roles", common_ir_cell_id="table:roles:c1", common_ir_occurrence_ids=("occ:role",)),
        ],
    )
    pack = _with_server_request_type(pack)
    selection = RequestSourceSelectionV012.model_validate({
        "profile_id": "request-table-row", "candidate_pack_id": pack.pack_id,
        "delivery_relations": [{
            "delivery_relation_id": "delivery_table_01",
            "actor_anchor": {"source_block_id": "table:roles#r2c0p0", "anchor_text": "가상 진흥원"},
            "role_anchor": {"source_block_id": "table:roles#r2c1p0", "anchor_text": "전담기관"},
            "relation_container": {"kind": "table_row", "common_ir_block_id": "table:roles", "row_index": 2},
            "canonical_actor_type": "public_agency", "canonical_role": "dedicated_agency",
        }],
    })
    profile = assemble_request_profile_v012(document, pack, selection)
    relation = profile["comparison_profile"]["delivery_relations"][0]
    assert relation["relation_container"] == {
        "container_type": "table_row", "common_ir_document_id": "request:PREREVIEW-TEST-2027-01",
        "common_ir_block_id": "table:roles", "row_index": 2,
    }
    assert profile["comparison_profile"]["delivery_methods"] == []


def test_delivery_relation_allows_explicit_nested_table_column_pair() -> None:
    document = json.loads(TABLE_COMMON_IR.read_text(encoding="utf-8"))
    pack = _with_server_request_type(build_request_candidate_pack(document))
    # t18 col 0 has two immediately successive non-empty rows in the real
    # HWP Common IR. This is structural regression coverage, not a claim that
    # these particular labels form a business delivery relation.
    actor_id = "hwp:t18#r0c0p0"
    role_id = "hwp:t18#r1c0p0"
    actor = next(block for block in pack.blocks if block.block_id == actor_id)
    role = next(block for block in pack.blocks if block.block_id == role_id)
    selection = RequestSourceSelectionV012.model_validate({
        "profile_id": "request-table-column-pair", "candidate_pack_id": pack.pack_id,
        "delivery_relations": [{
            "delivery_relation_id": "delivery_column_01",
            "actor_anchor": {"source_block_id": actor_id, "anchor_text": actor.text},
            "role_anchor": {"source_block_id": role_id, "anchor_text": role.text},
            "relation_container": {"kind": "table_column_pair", "common_ir_block_id": "hwp:t18"},
        }],
    })
    profile = assemble_request_profile_v012(document, pack, selection)
    container = profile["comparison_profile"]["delivery_relations"][0]["relation_container"]
    assert container["container_type"] == "table_column_pair"
    assert container["actor_common_ir_cell_id"] == actor.common_ir_cell_id
    assert container["role_common_ir_cell_id"] == role.common_ir_cell_id
    assert "actor_row_index" not in container
    assert "col_index" not in container


def test_delivery_column_pair_accepts_actual_hwp_actor_action_cells() -> None:
    """Real Request HWP diagram: 수행기관 1 -> R&D 과제수행 is action, not role."""

    document = json.loads(REQUEST_HWP_ORG_CHART.read_text(encoding="utf-8"))
    pack = build_request_candidate_pack(document)
    actor_id = "hwp:t87.c5.b1#r8c0p0"
    action_id = "hwp:t87.c5.b1#r9c0p0"
    actor = next(block for block in pack.blocks if block.block_id == actor_id)
    action = next(block for block in pack.blocks if block.block_id == action_id)
    selection = RequestSourceSelectionV012.model_validate({
        "profile_id": "request-hwp-column-action", "candidate_pack_id": pack.pack_id,
        "delivery_relations": [{
            "delivery_relation_id": "delivery_action_01",
            "actor_anchor": {"source_block_id": actor_id, "anchor_text": actor.text},
            "actions": [{"value_anchor": {"source_block_id": action_id, "anchor_text": action.text}}],
            "relation_container": {"kind": "table_column_pair", "common_ir_block_id": "hwp:t87.c5.b1"},
        }],
    })
    profile = assemble_request_profile_v012(document, pack, selection)
    relation = profile["comparison_profile"]["delivery_relations"][0]
    assert relation["role"] is None
    assert relation["actions"][0]["value_raw"] == "R&D 과제수행"
    assert relation["relation_container"] == {
        "container_type": "table_column_pair",
        "common_ir_document_id": pack.common_ir_document_id,
        "common_ir_block_id": "hwp:t87.c5.b1",
        "actor_common_ir_cell_id": actor.common_ir_cell_id,
        "action_common_ir_cell_ids": [action.common_ir_cell_id],
    }


def test_delivery_column_pair_rejects_role_action_conflict_and_cross_category_span_reuse() -> None:
    document = json.loads(REQUEST_HWP_ORG_CHART.read_text(encoding="utf-8"))
    pack = build_request_candidate_pack(document)
    actor_id = "hwp:t87.c5.b1#r8c0p0"
    action_id = "hwp:t87.c5.b1#r9c0p0"
    actor = next(block for block in pack.blocks if block.block_id == actor_id)
    action = next(block for block in pack.blocks if block.block_id == action_id)
    try:
        RequestSourceSelectionV012.model_validate({
            "profile_id": "request-role-action-conflict", "candidate_pack_id": pack.pack_id,
            "delivery_relations": [{
                "delivery_relation_id": "bad_relation",
                "actor_anchor": {"source_block_id": actor_id, "anchor_text": actor.text},
                "role_anchor": {"source_block_id": action_id, "anchor_text": action.text},
                "actions": [{"value_anchor": {"source_block_id": action_id, "anchor_text": action.text}}],
                "relation_container": {"kind": "table_column_pair", "common_ir_block_id": "hwp:t87.c5.b1"},
            }],
        })
    except ValueError as error:
        assert "not both" in str(error)
    else:
        raise AssertionError("column pair must not classify one member as role and action")

    reuse = RequestSourceSelectionV012.model_validate({
        "profile_id": "request-action-reuse", "candidate_pack_id": pack.pack_id,
        "delivery_relations": [{
            "delivery_relation_id": "delivery_action_01",
            "actor_anchor": {"source_block_id": actor_id, "anchor_text": actor.text},
            "actions": [{"value_anchor": {"source_block_id": action_id, "anchor_text": action.text}}],
            "relation_container": {"kind": "table_column_pair", "common_ir_block_id": "hwp:t87.c5.b1"},
        }],
        "delivery_methods": [{
            "fact_id": "method_duplicate", "value_anchor": {"source_block_id": action_id, "anchor_text": action.text},
        }],
    })
    try:
        assemble_request_profile_v012(document, pack, reuse)
    except ValueError as error:
        assert "duplicate exact source span" in str(error)
    else:
        raise AssertionError("delivery relation action cannot duplicate a delivery method or Fact span")


def test_delivery_column_pair_rejects_invalid_table_geometry() -> None:
    """Geometry remains server-only: bad row/column/table structures fail closed."""

    document = json.loads(REQUEST_HWP_ORG_CHART.read_text(encoding="utf-8"))
    pack = build_request_candidate_pack(document)
    base = {
        "profile_id": "request-hwp-column-invalid", "candidate_pack_id": pack.pack_id,
        "delivery_relations": [{
            "delivery_relation_id": "delivery_action_01",
            "actor_anchor": {"source_block_id": "hwp:t87.c5.b1#r8c0p0", "anchor_text": "수행기관 1"},
            "actions": [{"value_anchor": {"source_block_id": "hwp:t87.c5.b1#r9c0p0", "anchor_text": "R&D 과제수행"}}],
            "relation_container": {"kind": "table_column_pair", "common_ir_block_id": "hwp:t87.c5.b1"},
        }],
    }
    selection = RequestSourceSelectionV012.model_validate(base)

    def nested_table(copy_document: dict) -> dict:
        return next(block for block in copy_document["blocks"] if block["block_id"] == "hwp:t87.c5.b1")

    nonadjacent = deepcopy(document)
    next(cell for cell in nested_table(nonadjacent)["cells"] if cell["cell_id"] == "hwp:t87.c5.b1:c46")["row_index"] = 10
    try:
        assemble_request_profile_v012(nonadjacent, pack, selection)
    except ValueError as error:
        assert "immediate next" in str(error)
    else:
        raise AssertionError("intervening/non-adjacent semantic row must fail")

    col_mismatch = deepcopy(document)
    next(cell for cell in nested_table(col_mismatch)["cells"] if cell["cell_id"] == "hwp:t87.c5.b1:c46")["col_index"] = 1
    try:
        assemble_request_profile_v012(col_mismatch, pack, selection)
    except ValueError as error:
        assert "column span" in str(error)
    else:
        raise AssertionError("column mismatch must fail")

    nonexplicit = deepcopy(document)
    nested_table(nonexplicit)["structure_status"] = "inferred"
    try:
        assemble_request_profile_v012(nonexplicit, pack, selection)
    except ValueError as error:
        assert "explicit Common IR table" in str(error)
    else:
        raise AssertionError("non-explicit table must fail")

    other_table = next(block for block in pack.blocks if block.common_ir_block_id != "hwp:t87.c5.b1" and block.common_ir_cell_id)
    cross_table = deepcopy(base)
    cross_table["delivery_relations"][0]["actions"][0]["value_anchor"] = {
        "source_block_id": other_table.block_id, "anchor_text": other_table.text,
    }
    try:
        assemble_request_profile_v012(document, pack, RequestSourceSelectionV012.model_validate(cross_table))
    except ValueError as error:
        assert "declared Common IR table" in str(error)
    else:
        raise AssertionError("different table relation member must fail")


def test_change_diff_is_not_raw_fact_and_unchanged_reference_becomes_state() -> None:
    document = _document()
    pack = build_request_candidate_pack(document)
    diff = _anchor(pack, "시범사업 대비 지원단가·지원항목 자체를 바꾸지 않고, **선정규모와 지원기간만 확대**")
    selection = RequestSourceSelectionV012.model_validate({
        "profile_id": "request-diff-policy", "candidate_pack_id": pack.pack_id,
        "facts": [{"fact_id": "goal_diff", "field_name": "purpose_goal", "value_anchor": diff}],
    })
    try:
        assemble_request_profile_v012(document, pack, selection)
    except ValueError as error:
        assert "policy-purpose span" in str(error)
    else:
        raise AssertionError("purpose_goal must not preserve a change comparison narration")

    reference_pack = CandidatePack(
        pack_id="request-unchanged-reference-pack", notice_id="PREREVIEW-TEST-2027-01",
        extraction_scope="document", question="unchanged reference test",
        generator="semantic_structuring.common_ir_v1", generator_version="1",
        common_ir_document_id="request:PREREVIEW-TEST-2027-01",
        blocks=[SourceBlock(
            block_id="request:unchanged#p0", text="동일(변경 없음)", relation=SourceRelation.CANDIDATE,
            common_ir_block_id="request:unchanged", common_ir_occurrence_ids=("occ:unchanged",),
        )],
    )
    reference_pack = _with_server_request_type(reference_pack)
    unresolved = RequestSourceSelectionV012.model_validate({
        "profile_id": "request-unchanged-reference", "candidate_pack_id": reference_pack.pack_id,
        "field_states": [{
            "field_name": "support_scale", "status": "mentioned_unresolved",
            "reason_codes": ["unchanged_by_reference"],
        }],
    })
    unresolved_profile = assemble_request_profile_v012(document, reference_pack, unresolved)
    scale_state = next(row for row in unresolved_profile["field_states"] if row["field_name"] == "support_scale")
    assert scale_state == {
        "field_name": "support_scale", "status": "mentioned_unresolved",
        "reason_codes": ["unchanged_by_reference"],
    }
    invalid_reference = RequestSourceSelectionV012.model_validate({
        "profile_id": "request-unchanged-reference-invalid", "candidate_pack_id": reference_pack.pack_id,
        "facts": [{
            "fact_id": "scale_reference", "field_name": "support_scale",
            "value_anchor": {"source_block_id": "request:unchanged#p0", "anchor_text": "동일(변경 없음)"},
        }],
    })
    try:
        assemble_request_profile_v012(document, reference_pack, invalid_reference)
    except ValueError as error:
        assert "unchanged-by-reference" in str(error)
    else:
        raise AssertionError("bare unchanged reference cannot become a Raw Fact")


def test_unchanged_guard_keeps_independently_visible_values_and_applies_to_components_methods() -> None:
    document = _document()
    values_pack = CandidatePack(
        pack_id="request-visible-unchanged-values", notice_id="PREREVIEW-TEST-2027-01",
        extraction_scope="document", question="visible values", generator="test", generator_version="1",
        common_ir_document_id="request:PREREVIEW-TEST-2027-01",
        blocks=[
            SourceBlock(block_id="request:visible#p0", text="동일(50개사)", relation=SourceRelation.CANDIDATE,
                        common_ir_block_id="request:visible", common_ir_occurrence_ids=("occ:visible0",)),
            SourceBlock(block_id="request:visible#p1", text="변경없음(2027.1.~12.)", relation=SourceRelation.CANDIDATE,
                        common_ir_block_id="request:visible", common_ir_occurrence_ids=("occ:visible1",)),
        ],
    )
    values_pack = _with_server_request_type(values_pack)
    selection = RequestSourceSelectionV012.model_validate({
        "profile_id": "request-visible-unchanged", "candidate_pack_id": values_pack.pack_id,
        "facts": [
            {"fact_id": "scale_01", "field_name": "support_scale", "value_anchor": {"source_block_id": "request:visible#p0", "anchor_text": "동일(50개사)"}},
            {"fact_id": "period_01", "field_name": "support_period", "value_anchor": {"source_block_id": "request:visible#p1", "anchor_text": "변경없음(2027.1.~12.)"}},
        ],
    })
    profile = assemble_request_profile_v012(document, values_pack, selection)
    assert profile["comparison_profile"]["support_scale"][0]["value_raw"] == "동일(50개사)"
    assert profile["comparison_profile"]["support_period"][0]["value_raw"] == "변경없음(2027.1.~12.)"

    pack = build_request_candidate_pack(document)
    diff = _anchor(pack, "시범사업 대비 지원단가·지원항목 자체를 바꾸지 않고, **선정규모와 지원기간만 확대**")
    component_selection = RequestSourceSelectionV012.model_validate({
        "profile_id": "request-component-diff", "candidate_pack_id": pack.pack_id,
        "support_components": [{"support_component_id": "component_diff", "component_kind": "support_package", "name_anchor": diff}],
    })
    try:
        assemble_request_profile_v012(document, pack, component_selection)
    except ValueError as error:
        assert "support_component" in str(error)
    else:
        raise AssertionError("component name cannot persist change comparison narration")
    method_selection = RequestSourceSelectionV012.model_validate({
        "profile_id": "request-method-diff", "candidate_pack_id": pack.pack_id,
        "delivery_methods": [{"fact_id": "method_diff", "value_anchor": diff}],
    })
    try:
        assemble_request_profile_v012(document, pack, method_selection)
    except ValueError as error:
        assert "delivery_methods" in str(error)
    else:
        raise AssertionError("delivery method cannot persist change comparison narration")


def test_partial_field_state_keeps_fact_ids_and_reason_codes() -> None:
    document = _document()
    pack = build_request_candidate_pack(document)
    goal = _anchor(pack, "사회연대경제 청년 일경험 사업의 참여기업·참여청년 규모를 확대")
    selection = RequestSourceSelectionV012.model_validate({
        "profile_id": "request-partial-state", "candidate_pack_id": pack.pack_id,
        "facts": [{"fact_id": "goal_01", "field_name": "purpose_goal", "value_anchor": goal}],
        "field_states": [{
            "field_name": "purpose_goal", "status": "partial", "fact_ids": ["goal_01"],
            "reason_codes": ["additional_source_needed"],
        }],
    })
    profile = assemble_request_profile_v012(document, pack, selection)
    state = next(row for row in profile["field_states"] if row["field_name"] == "purpose_goal")
    assert state == {
        "field_name": "purpose_goal", "status": "partial", "fact_ids": ["goal_01"],
        "reason_codes": ["additional_source_needed"],
    }
    failed = RequestSourceSelectionV012.model_validate({
        "profile_id": "request-extraction-failed", "candidate_pack_id": pack.pack_id,
        "field_states": [{"field_name": "legal_basis", "status": "extraction_failed", "reason_codes": ["source_unreadable"]}],
    })
    failed_profile = assemble_request_profile_v012(document, pack, failed)
    assert next(row for row in failed_profile["field_states"] if row["field_name"] == "legal_basis")["status"] == "extraction_failed"


def test_request_02_partial_support_period_allows_no_selection_fact_ids() -> None:
    """Luna may know the field is partial without being able to name an ID yet."""

    document = _document_02()
    pack = build_request_candidate_pack(document)
    selection = RequestSourceSelectionV012.model_validate({
        "profile_id": "request-02-support-period-partial", "candidate_pack_id": pack.pack_id,
        "field_states": [{
            "field_name": "support_period", "status": "partial",
            "reason_codes": ["unchanged_by_reference"],
        }],
    })
    profile = assemble_request_profile_v012(document, pack, selection)
    state = next(row for row in profile["field_states"] if row["field_name"] == "support_period")
    assert state == {
        "field_name": "support_period", "status": "partial",
        "reason_codes": ["unchanged_by_reference"],
    }

    # If a model provides stale/cross-field IDs, server materialization ignores
    # them and derives the real IDs from the selected Raw Facts only.
    exact = _anchor(pack, "최대 6개월")
    with_fact = RequestSourceSelectionV012.model_validate({
        "profile_id": "request-02-support-period-derived", "candidate_pack_id": pack.pack_id,
        "facts": [{
            "fact_id": "support_period_01", "field_name": "support_period", "value_anchor": exact,
        }],
        "field_states": [{
            "field_name": "support_period", "status": "partial",
            "fact_ids": ["wrong_or_stale_fact_id"], "reason_codes": ["source_incomplete"],
        }],
    })
    derived = assemble_request_profile_v012(document, pack, with_fact)
    state = next(row for row in derived["field_states"] if row["field_name"] == "support_period")
    assert state == {
        "field_name": "support_period", "status": "partial", "fact_ids": ["support_period_01"],
        "reason_codes": ["source_incomplete"],
    }


def test_request_02_value_state_conflict_is_server_normalized_to_partial() -> None:
    """A Luna state conflict cannot discard a materialized eligibility value."""

    document = _document_02()
    pack = build_request_candidate_pack(document)
    block = next(block for block in pack.blocks if block.block_id.endswith(":b48"))
    selection = RequestSourceSelectionV012.model_validate({
        "profile_id": "request-02-eligibility-state-conflict", "candidate_pack_id": pack.pack_id,
        "facts": [{
            "fact_id": "eligibility_300", "field_name": "eligibility_conditions",
            "value_anchor": {"source_block_id": block.block_id, "anchor_text": "300인 이하"},
        }],
        "field_states": [{
            "field_name": "eligibility_conditions", "status": "mentioned_unresolved",
            "reason_codes": ["model_context_uncertain"],
        }],
    })
    profile = assemble_request_profile_v012(document, pack, selection)
    state = next(row for row in profile["field_states"] if row["field_name"] == "eligibility_conditions")
    assert state == {
        "field_name": "eligibility_conditions", "status": "partial",
        "fact_ids": ["eligibility_300"],
        "reason_codes": [
            "selection_state_conflict", "selection_status:mentioned_unresolved", "model_context_uncertain",
        ],
    }
