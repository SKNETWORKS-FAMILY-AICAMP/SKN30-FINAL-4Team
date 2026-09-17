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
    REQUEST_AXIS_MISSING,
)
from worker.analysis_inputs import CPL_FIELD_SOURCES, field_name_of
from worker.result_payload import (
    RESULT_CONTRACT_VERSION,
    _CPL_FIELD_LABELS,
    build_result_payload,
)
from worker.sim import compare_candidate


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


def _payload(
    *,
    delivery_status: SimStatus = SimStatus.INSUFFICIENT,
    fit_relation: FitRelationResult | None = None,
) -> dict:
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
            fit_relation
            or FitRelationResult(
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
            delivery_status,
            "CANDIDATE_EVIDENCE_MISSING"
            if delivery_status is SimStatus.INSUFFICIENT
            else None,
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
                "source_state": "모집중",
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
    assert cpl_detail["values"][0]["label"] == "사업 목적 · 목표"
    assert set(fit_detail) == {
        "comparison_performed",
        "reason_code",
        "reason",
        "left",
        "right",
        "evidence_ids",
    }
    assert fit_detail["comparison_performed"] is True
    assert fit_detail["evidence_ids"]
    assert fit_detail["reason"] == "사업 목적과 지원대상 관계를 확인했습니다."
    assert purpose["request_evidence_ids"] and purpose["existing_evidence_ids"]
    sim_evidence = [row for row in first["evidences"] if row["axis_type"] == "SIM"]
    assert {row["logical_code"] for row in sim_evidence} == {"SIM-1"}
    assert {row["candidate_source_profile_id"] for row in sim_evidence} == {
        "existing:notice-1"
    }
    assert candidate["status"] == "partial"  # core axes, not review grade/delivery
    assert candidate["metadata"]["apply_period"] == "2026-09-01 ~ 2026-09-30"
    assert candidate["metadata"]["notice_status"] == "모집중"
    assert candidate["public_axes"]["delivery"]["request_evidence_ids"] == []


def test_fit_public_reason_preserves_the_grounded_relation_judgment() -> None:
    judgment = (
        "목적의 기술경쟁력 강화와 기술 컨설팅 지원이 직접 연결됩니다."
    )
    relation = FitRelationResult(
        relation_id=FitRelationId.FIT_2,
        status=FitStatus.FIT,
        reason_code=None,
        left=FitSide(
            facts=[
                FitEvidenceRef(
                    "fact:purpose",
                    "purpose_goal",
                    "기술경쟁력 강화",
                    [_evidence()],
                )
            ]
        ),
        right=FitSide(
            facts=[
                FitEvidenceRef(
                    "fact:support",
                    "support_activities",
                    "기술 컨설팅",
                    [_evidence()],
                )
            ]
        ),
        used_left_fact_ids=["fact:purpose"],
        used_right_fact_ids=["fact:support"],
        summary=judgment,
    )

    fit_axis = _payload(fit_relation=relation)["axes"][1]

    assert fit_axis["summary_text"] == "사업 목적과 지원내용 관계를 확인했습니다."
    assert fit_axis["public_detail"]["reason"] == judgment


def test_fit_insufficient_never_publishes_an_ungrounded_model_summary() -> None:
    sentinel = "근거 없이 공개되면 안 되는 모델 문장"
    relation = FitRelationResult(
        relation_id=FitRelationId.FIT_2,
        status=FitStatus.INSUFFICIENT,
        reason_code="COMPARISON_EVIDENCE_MISSING",
        left=FitSide(
            facts=[
                FitEvidenceRef(
                    "fact:purpose", "purpose_goal", "기술경쟁력 강화", [_evidence()]
                )
            ]
        ),
        right=FitSide(
            facts=[
                FitEvidenceRef(
                    "fact:support", "support_activities", "기술 컨설팅", [_evidence()]
                )
            ]
        ),
        used_left_fact_ids=["fact:purpose"],
        used_right_fact_ids=["fact:support"],
        summary=sentinel,
    )

    fit_axis = _payload(fit_relation=relation)["axes"][1]

    assert fit_axis["summary_text"] == (
        "사업 목적과 지원내용 관계를 판단할 근거가 부족합니다."
    )
    assert fit_axis["public_detail"]["reason"] == fit_axis["summary_text"]
    assert sentinel not in str(fit_axis)
    assert fit_axis["public_detail"]["evidence_ids"] == []


def test_fit2_public_evidence_keeps_each_excerpt_from_one_source_fact() -> None:
    relation = FitRelationResult(
        relation_id=FitRelationId.FIT_2,
        status=FitStatus.FIT,
        reason_code=None,
        left=FitSide(
            facts=[
                FitEvidenceRef(
                    "fact:purpose",
                    "purpose_goal",
                    "기술경쟁력 강화",
                    [_evidence()],
                ),
                FitEvidenceRef(
                    "fact:purpose",
                    "purpose_goal",
                    "매출 성장 달성",
                    [_evidence()],
                ),
                FitEvidenceRef(
                    "fact:unselected",
                    "purpose_goal",
                    "해외 진출",
                    [_evidence()],
                ),
            ]
        ),
        right=FitSide(
            facts=[
                FitEvidenceRef(
                    "fact:support",
                    "support_activities",
                    "기술 컨설팅",
                    [_evidence()],
                )
            ]
        ),
        used_left_fact_ids=["fact:purpose"],
        used_right_fact_ids=["fact:support"],
    )

    payload = _payload(fit_relation=relation)
    fit_detail = payload["axes"][1]["public_detail"]
    evidence_by_id = {row["evidence_id"]: row for row in payload["evidences"]}

    assert len(fit_detail["left"]["evidence_ids"]) == 2
    assert {
        evidence_by_id[evidence_id]["raw_value"]
        for evidence_id in fit_detail["left"]["evidence_ids"]
    } == {"기술경쟁력 강화", "매출 성장 달성"}
    assert fit_detail["left"]["value_summary"] == (
        "기술경쟁력 강화 · 매출 성장 달성"
    )
    assert not any(
        row["logical_code"] == "FIT-2" and row["raw_value"] == "해외 진출"
        for row in payload["evidences"]
    )
    assert set(fit_detail["evidence_ids"]) == {
        *fit_detail["left"]["evidence_ids"],
        *fit_detail["right"]["evidence_ids"],
    }


def test_every_cpl_source_field_has_a_korean_public_label() -> None:
    source_fields = {
        field_name_of(path)
        for paths in CPL_FIELD_SOURCES.values()
        for path in paths
    }

    assert source_fields <= _CPL_FIELD_LABELS.keys()
    assert all(any("가" <= character <= "힣" for character in label)
               for label in _CPL_FIELD_LABELS.values())


def test_v02_candidate_comparable_axes_exclude_delivery() -> None:
    candidate = _payload(delivery_status=SimStatus.SIMILAR)["candidates"][0]

    assert candidate["public_axes"]["delivery"]["status"] == "similar"
    assert candidate["comparable_axes"] == ["purpose", "target", "support"]
    assert candidate["status"] == "partial"


def test_retrieval_missing_axes_are_not_sent_to_sim_and_keep_precise_reason() -> None:
    class MustNotCall:
        async def generate_structured(self, **_values: object) -> object:
            raise AssertionError("missing retrieval axes must not call the LLM")

    compared = compare_candidate(
        _profile("request:partial"),
        _profile("existing:partial"),
        MustNotCall(),
        model_profile="test",
        available_request_axes=frozenset(),
    )

    for axis in (SimAxis.PURPOSE, SimAxis.TARGET, SimAxis.CONTENT):
        result = compared.axis(axis)
        assert result.status is SimStatus.INSUFFICIENT
        assert result.reason_code == REQUEST_AXIS_MISSING


def test_v02_public_details_contain_no_fact_ids_or_diagnostics() -> None:
    payload = _payload()
    details = [axis["public_detail"] for axis in payload["axes"]]
    public_candidate = payload["candidates"][0]["public_axes"]
    rendered = repr({"details": details, "public_candidate": public_candidate})

    assert "fact:" not in rendered
    assert "diagnostic" not in rendered.lower()
    # Raw audit data remains separately available to the fenced materialiser.
    assert "fact:request-purpose" in repr(payload["axes"][0]["result_data"])
