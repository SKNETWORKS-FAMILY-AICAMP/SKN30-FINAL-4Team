from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from worker import analysis_job
from worker.contracts.fit_result import FitResult, PurposeAxisClassification
from worker.ml_reference import (
    MODEL_3_ALLOWED_LEVELS,
    MODEL_3_LEVEL_SENTENCES,
    MODEL_3_TYPICAL_LEVEL,
    FakeMlModel,
    _estimate_phrase,
    _validate_reference,
)
from worker.contracts.ml_result import MlModelId
from worker.contracts.sim_result import SimCommonProfile, SimComparisonResult


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
        lambda profile, *_args, **_kwargs: SimCommonProfile(
            source_profile_id=profile["profile_id"],
            schema_version=profile["schema_version"],
            common_ir_document_id="ir:ml-test",
            purpose={},
            target={},
            content={},
            delivery={},
        ),
    )
    monkeypatch.setattr(
        analysis_job,
        "compare_candidates",
        lambda request_common, *_args, **_kwargs: SimComparisonResult(
            request_profile_id=request_common.source_profile_id,
            candidates=[],
            model_profile="sim",
            ruleset_version="rules",
            prompt_version="prompt",
            scoring_version="score",
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
                "사전협의안에 적힌 기업당 지원액은 800만원입니다. "
                "조건이 비슷한 과거 사업들은 기업당 약 500만원 수준이었습니다."
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
