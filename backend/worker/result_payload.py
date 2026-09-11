"""Translate worker-owned CPL/FIT/SIM contracts to migration 22/23 JSON.

This module performs no inference.  It only places already-grounded values in
the fenced ``workspace.persist_analysis_result_core`` input shape.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import fields, is_dataclass
from enum import Enum
import math
from typing import Any

from .contracts.cpl_result import CplEvidence, CplResult, cpl_axis_code
from .contracts.fit_result import FitResult, fit_axis_code
from .contracts.ml_result import MlModelId, MlModelResult, MlReferenceResult
from .contracts.sim_result import (
    SimAxis,
    SimAxisResult,
    SimCommonProfile,
    SimComparisonResult,
    sim_display_status,
)


def plain(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: plain(getattr(value, field.name))
            for field in fields(value)
            if field.metadata.get("serialize", True)
        }
    if isinstance(value, Mapping):
        return {str(plain(key)): plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [plain(item) for item in value]
    return value


_CPL_SUMMARY = {
    "confirmed": "값과 원문 근거를 확인했습니다.",
    "needs_confirmation": "일부 값이나 원문 확인이 필요합니다.",
    "no_content": "적용 대상이지만 원문에서 내용을 찾지 못했습니다.",
    "not_applicable": "이 요청에는 적용되지 않는 항목입니다.",
}
_FIT_SUMMARY = {
    "FIT": "두 측면의 연결을 확인했습니다.",
    "NEEDS_REVIEW": "비교는 가능하지만 추가 검토가 필요합니다.",
    "CONFLICT": "원문상 충돌이 확인되었습니다.",
    "INSUFFICIENT": "비교에 필요한 근거가 부족합니다.",
}
_SIM_SUMMARY = {
    "similar": "공통점이 확인되었습니다.",
    "partial": "공통점과 차이점이 함께 확인되었습니다.",
    "different": "비교한 내용에서 차이점이 확인되었습니다.",
    "insufficient": "비교에 필요한 정보가 부족합니다.",
}
_REVIEW_GRADE_STATUS = {
    "FOCUS_REVIEW": "similar",
    "GENERAL_REVIEW": "partial",
    "LOW_PRIORITY": "different",
    "ON_HOLD": "insufficient",
}
_SIM_AXIS_NAME = {
    SimAxis.PURPOSE: "purpose",
    SimAxis.TARGET: "target",
    SimAxis.CONTENT: "support",
    SimAxis.DELIVERY: "delivery",
}

_ML_MESSAGES = {
    "MODEL_ARTIFACT_MISSING": "모델 가중치를 사용할 수 없습니다.",
    "ML_RUNTIME_MISSING": "모델 실행 환경을 사용할 수 없습니다.",
    "INPUT_EVIDENCE_MISSING": "모델 실행에 필요한 원문 근거가 부족합니다.",
    "MODEL_EXECUTION_FAILED": "모델 실행 중 오류가 발생했습니다.",
    "MODEL_INVALID_RESPONSE": "모델 응답을 결과 형식으로 변환하지 못했습니다.",
    "PREDICTION_WITHHELD": "모델이 예측을 보류했습니다.",
}


def _ml_message(result: MlModelResult) -> str | None:
    if result.reference_text:
        return result.reference_text
    if result.reason_code:
        return _ML_MESSAGES.get(result.reason_code, "모델 결과를 제공할 수 없습니다.")
    return None


def _ml_common(result: MlModelResult) -> dict[str, Any]:
    return {
        "status": result.status,
        "message": _ml_message(result),
        "reason_code": result.reason_code,
    }


def _public_ml_payload(ml_result: MlReferenceResult) -> dict[str, Any]:
    """Expose only the fixed ML surface; internal scores never cross this edge."""

    results = {result.model_id: result for result in ml_result.results}
    model_1 = results[MlModelId.MODEL_1_SUPPORT_TYPE]
    model_2 = results[MlModelId.MODEL_2_AMOUNT]
    model_3 = results[MlModelId.MODEL_3_ANOMALY]

    support_type = model_1.internal.get("support_type_pred")
    if model_1.status == "OK" and isinstance(support_type, str):
        support_type = support_type.strip() or None
    else:
        support_type = None

    predicted_amount_won: int | None = None
    if model_2.status == "OK":
        value = model_2.internal.get("pred_won")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            value = float(value)
            if math.isfinite(value) and value > 0:
                predicted_amount_won = int(round(value))

    anomaly_level = model_3.internal.get("level")
    if model_3.status != "OK" or not isinstance(anomaly_level, str):
        anomaly_level = None
    cause_axes = model_3.internal.get("cause_axes")
    if model_3.status != "OK" or not isinstance(cause_axes, list):
        cause_axes = []
    cause_axes = [axis for axis in cause_axes if isinstance(axis, str)]

    return {
        "model_1": {**_ml_common(model_1), "support_type": support_type},
        "model_2": {
            **_ml_common(model_2),
            "predicted_amount_won": predicted_amount_won,
        },
        "model_3": {
            **_ml_common(model_3),
            "anomaly_level": anomaly_level,
            "cause_axes": cause_axes,
        },
    }


def _evidence(
    evidence: Sequence[CplEvidence],
    *,
    axis_type: str,
    side: str,
    field_name: str | None,
    raw_value: str | None,
    source_sha256: str | None = None,
    candidate_source_profile_id: str | None = None,
) -> list[dict[str, Any]]:
    if not isinstance(raw_value, str) or not raw_value:
        return []
    common = {
        "axis_type": axis_type,
        "side": side,
        "field_name": field_name,
        "raw_value": raw_value,
        "source_sha256": source_sha256,
        # Migration 23 resolves this only against candidates of this case.
        "candidate_source_profile_id": candidate_source_profile_id,
    }
    if not evidence:
        return [common]
    return [
        {
            **common,
            "candidate_pack_block_id": row.source_block_id,
            "common_ir_document_id": row.common_ir_document_id,
            "common_ir_block_id": row.common_ir_block_id,
            "common_ir_occurrence_ids": list(row.common_ir_occurrence_ids),
        }
        for row in evidence
    ]


def _axis_json(axis: SimAxisResult) -> dict[str, Any]:
    result = plain(axis)
    status = sim_display_status(axis.status)
    result["status"] = status
    result["summary"] = _SIM_SUMMARY[status]
    return result


def _sim_summary(axes: Sequence[SimAxisResult]) -> str:
    statuses = {sim_display_status(axis.status) for axis in axes}
    if "insufficient" in statuses:
        return "비교에 필요한 정보가 일부 부족합니다."
    if "different" in statuses:
        return "비교한 축에서 차이점이 확인되었습니다."
    if "partial" in statuses:
        return "비교한 축에서 공통점과 차이점이 함께 확인되었습니다."
    if statuses:
        return "주요 비교 축의 공통점이 확인되었습니다."
    return "비교 결과가 없습니다."


def build_result_payload(
    *,
    profile: Mapping[str, Any],
    cpl: CplResult,
    fit: FitResult,
    sim: SimComparisonResult,
    sim_profiles: Mapping[str, SimCommonProfile],
    retrieval_similarities: Mapping[str, float],
    profile_version_ids: Mapping[str, str],
    ml_result: MlReferenceResult | None = None,
) -> dict[str, Any]:
    """Return JSON-native input accepted by the fenced result function."""

    axes: list[dict[str, Any]] = []
    evidences: list[dict[str, Any]] = []
    for item in cpl.items:
        axes.append(
            {
                "axis_type": "CPL",
                "axis_code": cpl_axis_code(item.field_code),
                "status": item.representative_status,
                "summary_text": _CPL_SUMMARY.get(
                    item.representative_status, "추가 확인이 필요합니다."
                ),
                "result_data": plain(item),
            }
        )
        for subfield in item.subfields:
            for fact in subfield.facts:
                evidences.extend(
                    _evidence(
                        fact.evidence,
                        axis_type="CPL",
                        side="REQUEST",
                        field_name=subfield.profile_field,
                        raw_value=fact.value_raw,
                        source_sha256=cpl.common_ir_source_sha256,
                    )
                )

    for relation in fit.relations:
        axes.append(
            {
                "axis_type": "FIT",
                "axis_code": fit_axis_code(relation.relation_id),
                "status": relation.status.value,
                "summary_text": _FIT_SUMMARY.get(
                    relation.status.value, "추가 확인이 필요합니다."
                ),
                "result_data": plain(relation),
            }
        )
        for side_input in (relation.left, relation.right):
            for ref in side_input.facts:
                evidences.extend(
                    _evidence(
                        ref.evidence,
                        axis_type="FIT",
                        side="REQUEST",
                        field_name=ref.field_name,
                        raw_value=ref.value_raw,
                    )
                )

    candidates: list[dict[str, Any]] = []
    for rank, candidate in enumerate(sim.candidates, start=1):
        source_profile_id = candidate.candidate_profile_id
        if not source_profile_id:
            continue
        profile_version_pk = profile_version_ids.get(source_profile_id)
        if not profile_version_pk:
            raise ValueError(
                f"retrieval profile version is missing for {source_profile_id}"
            )
        by_axis = {axis.axis: axis for axis in candidate.axes}
        comparable = [
            _SIM_AXIS_NAME[axis.axis]
            for axis in candidate.axes
            if sim_display_status(axis.status) != "insufficient"
        ]
        candidates.append(
            {
                "source_profile_id": source_profile_id,
                "profile_version_pk": profile_version_pk,
                "rank": rank,
                "similarity_score": retrieval_similarities.get(source_profile_id),
                "priority_score": candidate.internal_ranking.weighted_score,
                "status": _REVIEW_GRADE_STATUS[
                    candidate.internal_ranking.review_grade.value
                ],
                "summary_text": _sim_summary(candidate.axes),
                "comparable_axes": comparable,
                "purpose_result": _axis_json(by_axis[SimAxis.PURPOSE]),
                "target_result": _axis_json(by_axis[SimAxis.TARGET]),
                "support_result": _axis_json(by_axis[SimAxis.CONTENT]),
                "delivery_result": _axis_json(by_axis[SimAxis.DELIVERY]),
            }
        )
        candidate_profile = sim_profiles.get(source_profile_id)
        if candidate_profile is not None:
            for axis in SimAxis:
                for entry in candidate_profile.entries(axis):
                    evidences.extend(
                        _evidence(
                            entry.evidence,
                            axis_type="SIM",
                            side="EXISTING",
                            field_name=entry.source_field,
                            raw_value=entry.value_raw,
                            candidate_source_profile_id=source_profile_id,
                        )
                    )

    request_profile_id = sim.request_profile_id or str(profile.get("profile_id") or "")
    request_common = sim_profiles.get(request_profile_id)
    if request_common is not None:
        for axis in SimAxis:
            for entry in request_common.entries(axis):
                evidences.extend(
                    _evidence(
                        entry.evidence,
                        axis_type="SIM",
                        side="REQUEST",
                        field_name=entry.source_field,
                        raw_value=entry.value_raw,
                        source_sha256=cpl.common_ir_source_sha256,
                    )
                )

    identity = profile.get("identity")
    identity = identity if isinstance(identity, Mapping) else {}
    program_name = identity.get("program_name") or identity.get("title_raw")
    payload = {
        "program_name": program_name if isinstance(program_name, str) else None,
        "axes": axes,
        "candidates": candidates,
        "evidences": evidences,
    }
    if ml_result is not None:
        payload["ml"] = _public_ml_payload(ml_result)
    return payload
