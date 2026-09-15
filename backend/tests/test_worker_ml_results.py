from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from worker import analysis_job
from worker.contracts.fit_result import FitResult, PurposeAxisClassification
from worker.contracts.ml_result import (
    MODEL_EXECUTION_FAILED,
    MlModelId,
    MlModelResult,
    MlReferenceResult,
)
from worker.ml_reference import (
    MODEL_3_ALLOWED_LEVELS,
    MODEL_3_LEVEL_SENTENCES,
    MODEL_3_TYPICAL_LEVEL,
    FakeMlModel,
    MlModelInput,
    _estimate_phrase,
    _run_one,
    _validate_reference,
    build_ml_inputs,
    resolve_authoritative_request_limit,
)
from worker.contracts.sim_result import SimCommonProfile, SimComparisonResult
from worker.result_payload import _public_ml_payload


class _NoCallLLM:
    pass


def _fact(field: str, value: str, block_id: str) -> dict[str, Any]:
    return {
        "fact_id": f"fact:{field}",
        "field_name": field,
        "value_raw": value,
        "status": "identified",
        "value_source": {
            "source_block_id": block_id,
            "start_char": 0,
            "end_char": len(value),
        },
        "evidence": [
            {
                "source_block_id": block_id,
                "common_ir_block_id": block_id,
                "common_ir_occurrence_ids": [],
            }
        ],
    }


def _request_limit_projection(
    amount_won: int,
    *,
    applies_per: str = "COMPANY",
    comparator: str = "lte",
    source_fact_id: str = "fact:support_scale",
) -> dict[str, Any]:
    lower_value = amount_won if comparator == "eq" else None
    return {
        "projection_type": "support_scale_measures",
        "status": "identified",
        "source_fact_ids": [source_fact_id],
        "measures": [
            {
                "source_fact_id": source_fact_id,
                "measure_type": "amount",
                "measure_role": "support_limit",
                "lower_value": lower_value,
                "upper_value": amount_won,
                "unit": "KRW",
                "comparator": comparator,
                "applies_per": applies_per,
                "aggregation_scope": "PER_UNIT",
            }
        ],
    }


def _profile() -> tuple[dict[str, Any], dict[str, Any]]:
    fields = {
        "purpose_goal": _fact("purpose_goal", "지역 기업의 성장 지원", "b-purpose"),
        "support_target": _fact("support_target", "지역 중소기업", "b-target"),
        "support_activities": _fact(
            "support_activities", "기술 컨설팅", "b-content"
        ),
        "support_scale": _fact("support_scale", "기업당 5백만원", "b-scale"),
        "total_budget": _fact("total_budget", "총 1억원", "b-budget"),
    }
    profile = {
        "schema_version": "pre_review_request_profile/v0.1",
        "profile_id": "request:ml-test",
        "identity": {"title_raw": "ML 연결 테스트 사업"},
        "processing_metadata": {
            "common_ir_document_id": "ir:ml-test",
            "common_ir_source_sha256": "a" * 64,
        },
        "comparison_profile": {
            "purpose_goal": [fields["purpose_goal"]],
            "support_target": [fields["support_target"]],
            "support_activities": [fields["support_activities"]],
            "support_scale": [fields["support_scale"]],
            "total_budget": [fields["total_budget"]],
        },
        "request_context": {},
        "support_components": [],
        "derived_projections": [_request_limit_projection(7_000_000)],
        "field_states": [
            {"field_name": name, "status": "identified"} for name in fields
        ],
    }
    common_ir = {
        "schema_version": "common_ir_v1",
        "document": {
            "document_id": "ir:ml-test",
            "provenance": {"source_sha256": "a" * 64},
        },
        "blocks": [
            {"block_id": block_id, "text": text}
            for block_id, text in (
                ("b-purpose", "지역 기업의 성장 지원"),
                ("b-target", "지역 중소기업"),
                ("b-content", "기술 컨설팅"),
                ("b-scale", "기업당 5백만원"),
                ("b-budget", "총 1억원"),
            )
        ],
    }
    return profile, common_ir


def test_core_engine_runs_all_ml_models_and_exposes_only_public_fields(
    monkeypatch,
) -> None:
    profile, common_ir = _profile()
    monkeypatch.setattr(
        analysis_job,
        "analyze_fit",
        lambda *_args, **_kwargs: FitResult(
            relations=[],
            purpose_axis=PurposeAxisClassification(attempted=False),
            profile_id=profile["profile_id"],
            common_ir_document_id="ir:ml-test",
            model_profile="fit",
            ruleset_version="rules",
            prompt_version="prompt",
        ),
    )
    monkeypatch.setattr(
        analysis_job,
        "build_common_profile",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("SIM must not be structured without retrieved candidates")
        ),
    )
    monkeypatch.setattr(
        analysis_job,
        "compare_candidates",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("SIM must not compare without retrieved candidates")
        ),
    )

    model_1 = FakeMlModel(
        MlModelId.MODEL_1_SUPPORT_TYPE,
        {"support_type_pred": "융자", "confidence": 0.91, "status": "신뢰"},
    )
    model_2 = FakeMlModel(
        MlModelId.MODEL_2_AMOUNT,
        {
            "pred_won": 5_000_000,
            # A model or legacy adapter cannot decide the observed request
            # amount; the structured Profile measure above is authoritative.
            "stated_per_recipient_won": 8_000_000,
            "percentile_rank": 0.5,
        },
    )
    model_3 = FakeMlModel(
        MlModelId.MODEL_3_ANOMALY,
        {"level": "확인 필요", "cause_axes": ["지원비율"], "score": 0.2},
    )
    engine = analysis_job.CoreAnalysisEngine(
        _NoCallLLM(),
        cpl_model_profile="cpl",
            fit_model_profile="fit",
        sim_model_profile="sim",
        ml_models={
            MlModelId.MODEL_1_SUPPORT_TYPE: model_1,
            MlModelId.MODEL_2_AMOUNT: model_2,
            MlModelId.MODEL_3_ANOMALY: model_3,
        },
    )

    payload = engine.build_payload(profile=profile, common_ir=common_ir, candidates=[])

    assert payload["ml"] == {
        "model_1": {
            "status": "OK",
            "support_type": "융자",
            "message": "과거 비슷한 사업들과 견주면 이 사업은 '융자' 성격에 가깝습니다.",
            "reason_code": None,
        },
        "model_2": {
            "status": "OK",
            "predicted_amount_won": 5_000_000,
            "message": (
                "사전협의안에 명시된 기업당 지원 한도는 700만원입니다. "
                "조건이 비슷한 과거 사업들의 지원 단위당 예측 금액은 약 500만원입니다."
            ),
            "reason_code": None,
        },
        "model_3": {
            "status": "OK",
            "anomaly_level": "확인 필요",
            "cause_axes": ["지원비율"],
            "message": (
                "과거 비슷한 사업들과 다소 차이가 있어 한 번 확인해 볼 만합니다. "
                "가장 크게 차이 나는 항목은 지원비율입니다."
            ),
            "reason_code": None,
        },
    }
    rendered = json.dumps(payload, ensure_ascii=False)
    assert all(
        forbidden not in rendered
        for forbidden in ("confidence", "percentile", "score", "internal")
    )
    assert model_1.calls[0]["title"] == "ML 연결 테스트 사업"
    assert model_2.calls[0]["support_type"] == "융자"
    assert model_3.calls[0]["support_type_status"] == "신뢰"
    assert "authoritative_request_limit" not in model_2.calls[0]


def test_profile_title_uses_structured_detail_program_when_identity_is_empty() -> None:
    profile = {
        "identity": {"title_raw": None},
        "program_hierarchy": {
            "nodes": [
                {"level": "parent_program", "name_raw": "상위사업"},
                {"level": "detail_program", "name_raw": "세부 지원사업"},
            ]
        },
    }

    assert analysis_job._profile_title(profile) == "세부 지원사업"


def test_prediction_phrase_drops_false_precision_and_covers_every_level() -> None:
    """예측 금액은 유효숫자 두 자리, 표시 어휘는 전부 문장이 있어야 한다.

    ``8,520,390원`` 이 문장에 그대로 나가면 추정치가 확정 금액으로 읽힌다.
    표시 어휘에 문장이 없으면 그 level 은 사용자에게 내보낼 수 없다.
    """

    assert _estimate_phrase(8_520_390) == "850만원"
    assert _estimate_phrase(1_234_567_890) == "12억원"
    for rejected in (0, -1, float("nan"), True, None):
        assert _estimate_phrase(rejected) is None

    assert set(MODEL_3_ALLOWED_LEVELS) <= set(MODEL_3_LEVEL_SENTENCES)
    assert MODEL_3_TYPICAL_LEVEL in MODEL_3_LEVEL_SENTENCES
    # 통상 범위 안이면 축을 말하지 않는다 — 정상 사례를 이례 사례로 읽게 된다.
    typical = _validate_reference(
        MlModelId.MODEL_3_ANOMALY,
        {"level": MODEL_3_TYPICAL_LEVEL, "cause_axes": ["지원비율"]},
    )
    assert typical == MODEL_3_LEVEL_SENTENCES[MODEL_3_TYPICAL_LEVEL]


def test_model2_message_does_not_claim_an_unprovided_stated_amount() -> None:
    assert _validate_reference(
        MlModelId.MODEL_2_AMOUNT,
        {"pred_won": 5_000_000, "stated_per_recipient_won": 8_000_000},
    ) == "조건이 비슷한 과거 사업들의 지원 단위당 예측 금액은 약 500만원입니다."


def test_model2_display_uses_profile_limit_metadata_and_preserves_scope() -> None:
    profile, _ = _profile()
    profile["derived_projections"] = [_request_limit_projection(9_000_000, applies_per="PROJECT")]

    inputs = build_ml_inputs(profile, title="ML 연결 테스트 사업")

    assert inputs[MlModelId.MODEL_2_AMOUNT].metadata == {
        "authoritative_request_limit": {"amount_won": 9_000_000, "applies_per": "PROJECT"}
    }
    assert "authoritative_request_limit" not in inputs[MlModelId.MODEL_2_AMOUNT].payload
    limit = resolve_authoritative_request_limit(profile)
    assert _validate_reference(
        MlModelId.MODEL_2_AMOUNT,
        {"pred_won": 5_000_000, "stated_per_recipient_won": 80_000_000},
        authoritative_request_limit=limit,
    ) == (
        "사전협의안에 명시된 과제당 지원 한도는 900만원입니다. "
        "조건이 비슷한 과거 사업들의 지원 단위당 예측 금액은 약 500만원입니다."
    )


def test_model2_conflicting_or_non_limit_profile_measures_are_withheld() -> None:
    profile, _ = _profile()
    profile["derived_projections"] = [
        _request_limit_projection(5_000_000),
        _request_limit_projection(7_000_000),
    ]

    assert resolve_authoritative_request_limit(profile) is None
    assert _validate_reference(
        MlModelId.MODEL_2_AMOUNT,
        {"pred_won": 5_000_000, "stated_per_recipient_won": 80_000_000},
        authoritative_request_limit=resolve_authoritative_request_limit(profile),
    ) == "조건이 비슷한 과거 사업들의 지원 단위당 예측 금액은 약 500만원입니다."

    non_limit = _request_limit_projection(5_000_000)
    non_limit["measures"][0]["measure_role"] = "support_amount"
    assert resolve_authoritative_request_limit(
        {"derived_projections": [non_limit]}
    ) is None


def test_model2_profile_limit_preserves_company_project_and_team_scope() -> None:
    for applies_per, label in (
        ("COMPANY", "기업당"),
        ("PROJECT", "과제당"),
        ("TEAM", "팀당"),
    ):
        profile, _ = _profile()
        profile["derived_projections"] = [
            _request_limit_projection(5_000_000, applies_per=applies_per)
        ]
        limit = resolve_authoritative_request_limit(profile)

        assert _validate_reference(
            MlModelId.MODEL_2_AMOUNT,
            {"pred_won": 4_000_000},
            authoritative_request_limit=limit,
        ) == (
            f"사전협의안에 명시된 {label} 지원 한도는 500만원입니다. "
            "조건이 비슷한 과거 사업들의 지원 단위당 예측 금액은 약 400만원입니다."
        )


class _SequencedMlModel:
    artifact_version = "test-model-3"

    def __init__(self, failures: int, output: dict[str, Any]) -> None:
        self.failures = failures
        self.output = output
        self.calls: list[dict[str, Any]] = []

    def predict(self, inputs: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(dict(inputs))
        if len(self.calls) <= self.failures:
            raise RuntimeError("temporary model 3 failure")
        return dict(self.output)


def _model_3_input() -> MlModelInput:
    return MlModelInput(
        model_id=MlModelId.MODEL_3_ANOMALY,
        payload={"evidence_text": "사업기간 1년, 지원비율 50%"},
        sources=["common_ir:evidence_text"],
    )


def test_model3_execution_failure_is_retried_once_and_can_recover() -> None:
    model = _SequencedMlModel(
        failures=1,
        output={"level": "확인 필요", "cause_axes": ["지원비율"]},
    )
    diagnostics = []

    result = _run_one(
        MlModelId.MODEL_3_ANOMALY,
        model,
        _model_3_input(),
        diagnostics,
    )

    assert len(model.calls) == 2
    assert result.status == "OK"
    assert result.reference_text is not None
    assert diagnostics == []


def test_model3_exhausted_failure_keeps_internal_reason_but_hides_message() -> None:
    model = _SequencedMlModel(
        failures=2,
        output={"level": "확인 필요", "cause_axes": ["지원비율"]},
    )
    diagnostics = []

    model_3 = _run_one(
        MlModelId.MODEL_3_ANOMALY,
        model,
        _model_3_input(),
        diagnostics,
    )
    payload = _public_ml_payload(
        MlReferenceResult(
            results=[
                MlModelResult(
                    model_id=MlModelId.MODEL_1_SUPPORT_TYPE,
                    status="OK",
                    reason_code=None,
                    reference_text="지원유형 참고 분류",
                ),
                MlModelResult(
                    model_id=MlModelId.MODEL_2_AMOUNT,
                    status="OK",
                    reason_code=None,
                    reference_text="예측 지원액 참고",
                ),
                model_3,
            ]
        )
    )

    assert len(model.calls) == 2
    assert model_3.status == "FAILED"
    assert model_3.reason_code == MODEL_EXECUTION_FAILED
    assert len(diagnostics) == 1
    assert "attempts=2" in diagnostics[0].message
    assert payload["model_1"]["message"] == "지원유형 참고 분류"
    assert payload["model_2"]["message"] == "예측 지원액 참고"
    assert payload["model_3"] == {
        "status": "FAILED",
        "message": None,
        "reason_code": MODEL_EXECUTION_FAILED,
        "anomaly_level": None,
        "cause_axes": [],
    }

def _payload_with_model_1(model_1: MlModelResult) -> dict[str, Any]:
    return _public_ml_payload(
        MlReferenceResult(
            results=[
                model_1,
                MlModelResult(
                    model_id=MlModelId.MODEL_2_AMOUNT,
                    status="OK",
                    reason_code=None,
                    reference_text="예측 지원액 참고",
                ),
                MlModelResult(
                    model_id=MlModelId.MODEL_3_ANOMALY,
                    status="OK",
                    reason_code=None,
                    reference_text="설계 이례성 참고",
                ),
            ]
        )
    )


def test_model1_failure_is_not_retried_and_public_message_is_null() -> None:
    model = _SequencedMlModel(failures=2, output={})
    diagnostics = []

    model_1 = _run_one(
        MlModelId.MODEL_1_SUPPORT_TYPE,
        model,
        MlModelInput(
            model_id=MlModelId.MODEL_1_SUPPORT_TYPE,
            payload={"title": "테스트 사업", "evidence_text": "지원 사업 원문"},
            sources=["common_ir:evidence_text"],
        ),
        diagnostics,
    )
    payload = _payload_with_model_1(model_1)

    assert len(model.calls) == 1
    assert model_1.status == "FAILED"
    assert model_1.reason_code == MODEL_EXECUTION_FAILED
    assert len(diagnostics) == 1
    assert payload["model_1"]["message"] is None
    assert payload["model_2"]["message"] == "예측 지원액 참고"
    assert payload["model_3"]["message"] == "설계 이례성 참고"


def test_model1_unavailable_reason_is_internal_and_public_message_is_null() -> None:
    model_1 = MlModelResult(
        model_id=MlModelId.MODEL_1_SUPPORT_TYPE,
        status="UNAVAILABLE",
        reason_code="PREDICTION_WITHHELD",
        reference_text=None,
    )

    payload = _payload_with_model_1(model_1)

    assert payload["model_1"]["status"] == "UNAVAILABLE"
    assert payload["model_1"]["reason_code"] == "PREDICTION_WITHHELD"
    assert payload["model_1"]["message"] is None

def _model_2_input() -> MlModelInput:
    return MlModelInput(
        model_id=MlModelId.MODEL_2_AMOUNT,
        payload={"evidence_text": "기업당 500만원을 지원한다."},
        sources=["common_ir:evidence_text"],
    )


def _payload_with_model_2(model_2: MlModelResult) -> dict[str, Any]:
    return _public_ml_payload(
        MlReferenceResult(
            results=[
                MlModelResult(
                    model_id=MlModelId.MODEL_1_SUPPORT_TYPE,
                    status="OK",
                    reason_code=None,
                    reference_text="지원유형 참고 분류",
                ),
                model_2,
                MlModelResult(
                    model_id=MlModelId.MODEL_3_ANOMALY,
                    status="OK",
                    reason_code=None,
                    reference_text="설계 이례성 참고",
                ),
            ]
        )
    )


def test_model2_execution_failure_is_retried_once_and_can_recover() -> None:
    model = _SequencedMlModel(
        failures=1,
        output={"pred_won": 5_000_000},
    )
    diagnostics = []

    model_2 = _run_one(
        MlModelId.MODEL_2_AMOUNT,
        model,
        _model_2_input(),
        diagnostics,
    )

    assert len(model.calls) == 2
    assert model_2.status == "OK"
    assert model_2.reference_text is not None
    assert diagnostics == []


def test_model2_exhausted_failure_keeps_internal_reason_but_hides_message() -> None:
    model = _SequencedMlModel(failures=2, output={})
    diagnostics = []

    model_2 = _run_one(
        MlModelId.MODEL_2_AMOUNT,
        model,
        _model_2_input(),
        diagnostics,
    )
    payload = _payload_with_model_2(model_2)

    assert len(model.calls) == 2
    assert model_2.status == "FAILED"
    assert model_2.reason_code == MODEL_EXECUTION_FAILED
    assert len(diagnostics) == 1
    assert "attempts=2" in diagnostics[0].message
    assert payload["model_1"]["message"] == "지원유형 참고 분류"
    assert payload["model_2"]["message"] is None
    assert payload["model_3"]["message"] == "설계 이례성 참고"
