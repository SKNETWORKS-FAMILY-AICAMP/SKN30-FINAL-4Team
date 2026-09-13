"""Focused worker-side contracts for the v0.2 raw/public result boundary."""

from __future__ import annotations

from uuid import UUID

from worker.contracts.cpl_result import (
    CplEvidence,
    CplFact,
    CplFieldCode,
    CplItem,
    CplResult,
    CplSubfield,
)
from worker.contracts.fit_result import (
    FitEvidenceRef,
    FitRelationId,
    FitRelationResult,
    FitResult,
    FitSide,
    FitStatus,
    PurposeAxisClassification,
)
from worker.contracts.sim_result import (
    InternalRanking,
    SimAxis,
    SimAxisResult,
    SimCandidateResult,
    SimCommonEntry,
    SimCommonProfile,
    SimComparisonResult,
    SimReviewGrade,
    SimStatus,
)
from worker.result_payload import RESULT_CONTRACT_VERSION, build_result_payload


def _evidence() -> CplEvidence:
    return CplEvidence(
        source_block_id="pack:1",
        common_ir_document_id="ir:request",
        common_ir_block_id="block:1",
        common_ir_occurrence_ids=["occ:1"],
    )


def _profile(profile_id: str) -> SimCommonProfile:
    empty = {"unused": []}
    entry = SimCommonEntry(
        fact_id=f"fact:{profile_id}",
        source_field="purpose_goal",
        common_key="direction",
        value_raw="지역 기업 성장 지원",
        evidence=[_evidence()],
    )
    return SimCommonProfile(
        source_profile_id=profile_id,
        schema_version="v0.1",
        common_ir_document_id=f"ir:{profile_id}",
        purpose={"direction": [entry]},
        target=dict(empty),
        content=dict(empty),
        delivery=dict(empty),
        fact_id_registry=frozenset({entry.fact_id}),
    )


def _payload() -> dict:
    request_id = "request:run-1"
    existing_id = "existing:notice-1"
    fact = CplFact(
        fact_id="fact:request-purpose",
        value_raw="부산 소재 중소기업",
        status="identified",
        source_block_id="pack:1",
        start_char=3,
        end_char=13,
        text_basis="grounded",
        evidence=[_evidence()],
    )
    cpl = CplResult(
        items=[
            CplItem(
                CplFieldCode.PURPOSE_GOAL,
                "confirmed",
                None,
                [
                    CplSubfield(
                        "comparison_profile.purpose_goal",
                        "purpose_goal",
                        "identified",
                        facts=[fact],
                    )
                ],
            )
        ],
        profile_id=request_id,
        common_ir_document_id="ir:request",
        common_ir_source_sha256="a" * 64,
        candidate_pack_id="pack:request",
        pipeline_version="test",
        structured_schema_version="v0.1",
        model_id="test",
        prompt_version="test",
    )
    left = FitEvidenceRef(
        "fact:left", "purpose_goal", "부산 소재 중소기업", [_evidence()]
    )
    right = FitEvidenceRef(
        "fact:right", "support_target", "중소기업 지원기관", [_evidence()]
    )
    fit = FitResult(
        relations=[
            FitRelationResult(
                FitRelationId.FIT_1,
                FitStatus.FIT,
                None,
                FitSide(facts=[left]),
                FitSide(facts=[right]),
                used_left_fact_ids=["fact:left"],
                used_right_fact_ids=["fact:right"],
            )
        ],
        purpose_axis=PurposeAxisClassification(attempted=False),
        profile_id=request_id,
        common_ir_document_id="ir:request",
        model_profile="test",
        ruleset_version="test",
        prompt_version="test",
    )
    sim_axes = [
        SimAxisResult(
            SimAxis.PURPOSE,
            "SIM-1",
            SimStatus.SIMILAR,
            None,
            request_fact_ids=[f"fact:{request_id}"],
            candidate_fact_ids=[f"fact:{existing_id}"],
        ),
        SimAxisResult(SimAxis.TARGET, "SIM-2", SimStatus.SIMILAR, None),
        SimAxisResult(SimAxis.CONTENT, "SIM-3", SimStatus.PARTIAL, "PARTIAL_OVERLAP"),
        SimAxisResult(
            SimAxis.DELIVERY,
            "SIM-4",
            SimStatus.INSUFFICIENT,
            "CANDIDATE_EVIDENCE_MISSING",
        ),
    ]
    sim = SimComparisonResult(
        request_profile_id=request_id,
        candidates=[
            SimCandidateResult(
                existing_id,
                "notice-1",
                sim_axes,
                InternalRanking(0.99, SimReviewGrade.FOCUS_REVIEW, 3),
                title="후보 공고",
            )
        ],
        model_profile="test",
        ruleset_version="test",
        prompt_version="test",
        scoring_version="test",
    )
    return build_result_payload(
        analysis_run_id="11111111-1111-1111-1111-111111111111",
        profile={"profile_id": request_id, "identity": {"title_raw": "요청 사업"}},
        cpl=cpl,
        fit=fit,
        sim=sim,
        sim_profiles={
            request_id: _profile(request_id),
            existing_id: _profile(existing_id),
        },
        retrieval_similarities={existing_id: 0.99},
        profile_version_ids={existing_id: "22222222-2222-2222-2222-222222222222"},
        candidate_metadata={
            existing_id: {
                "title": "고정된 후보",
                "apply_period": "2026-09-01 ~ 2026-09-30",
            }
        },
    )


def test_v02_payload_is_deterministic_and_links_only_selected_evidence() -> None:
    first = _payload()
    second = _payload()

    assert first == second
    assert first["contract_version"] == RESULT_CONTRACT_VERSION
    assert {UUID(row["evidence_id"]) for row in first["evidences"]}
    cpl_detail = first["axes"][0]["public_detail"]
    fit_detail = first["axes"][1]["public_detail"]
    candidate = first["candidates"][0]
    purpose = candidate["public_axes"]["purpose"]
    assert cpl_detail["evidence_ids"]
    assert fit_detail["comparison_performed"] is True
    assert fit_detail["evidence_ids"]
    assert purpose["request_evidence_ids"] and purpose["existing_evidence_ids"]
    assert candidate["status"] == "partial"  # core axes, not review grade/delivery
    assert candidate["metadata"]["apply_period"] == "2026-09-01 ~ 2026-09-30"
    assert candidate["public_axes"]["delivery"]["request_evidence_ids"] == []


def test_v02_public_details_contain_no_fact_ids_or_diagnostics() -> None:
    payload = _payload()
    details = [axis["public_detail"] for axis in payload["axes"]]
    public_candidate = payload["candidates"][0]["public_axes"]
    rendered = repr({"details": details, "public_candidate": public_candidate})

    assert "fact:" not in rendered
    assert "diagnostic" not in rendered.lower()
    # Raw audit data remains separately available to the fenced materialiser.
    assert "fact:request-purpose" in repr(payload["axes"][0]["result_data"])
