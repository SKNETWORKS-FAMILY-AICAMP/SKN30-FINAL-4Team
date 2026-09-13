"""Build the worker-owned v0.2 raw/public result persistence payload.

``result_data`` and legacy candidate axis fields are audit-only raw data.
Every browser/chat-safe value is separately assembled as ``public_*``.  The
v2 materialiser stores those in distinct columns and never projects raw data.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import fields, is_dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any
from uuid import NAMESPACE_URL, uuid5

from .contracts.cpl_result import CplEvidence, CplFact, CplResult, cpl_display_code
from .contracts.fit_result import FitEvidenceRef, FitResult, FitStatus, fit_axis_code
from .contracts.ml_result import MlModelId, MlModelResult, MlReferenceResult
from .contracts.sim_result import (
    SimAxis,
    SimAxisResult,
    SimCommonEntry,
    SimCommonProfile,
    SimComparisonResult,
    SimStatus,
    sim_display_status,
)

if TYPE_CHECKING:  # avoid a runtime analysis_job -> result_payload cycle
    from .analysis_job import RetrievalDecision


RESULT_CONTRACT_VERSION = "analysis_result/v0.2"
EVIDENCE_NAMESPACE = uuid5(NAMESPACE_URL, "pre-review/result-evidence/v0.2")

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
    "NOT_APPLICABLE": "이 요청에는 계층 비교가 적용되지 않습니다.",
}
_SIM_SUMMARY = {
    "similar": "공통점이 확인되었습니다.",
    "partial": "공통점과 차이점이 함께 확인되었습니다.",
    "different": "비교한 내용에서 차이점이 확인되었습니다.",
    "insufficient": "비교에 필요한 정보가 부족합니다.",
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


def plain(value: Any) -> Any:
    """Convert worker contracts to raw JSON while honouring hidden fields."""

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
    """Retain the established ML surface without exposing model internals."""

    results = {result.model_id: result for result in ml_result.results}
    model_1 = results[MlModelId.MODEL_1_SUPPORT_TYPE]
    model_2 = results[MlModelId.MODEL_2_AMOUNT]
    model_3 = results[MlModelId.MODEL_3_ANOMALY]
    support_type = model_1.internal.get("support_type_pred")
    if model_1.status != "OK" or not isinstance(support_type, str):
        support_type = None
    else:
        support_type = support_type.strip() or None
    predicted_amount_won: int | None = None
    if model_2.status == "OK":
        value = model_2.internal.get("pred_won")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            value = float(value)
            if math.isfinite(value) and value > 0:
                predicted_amount_won = round(value)
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


def _clean_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _source_rows(
    evidence: Sequence[CplEvidence], *, fallback_block: str | None = None
) -> list[dict[str, Any]]:
    """Use only source coordinates already grounded by a pipeline stage."""

    rows = [
        {
            "candidate_pack_block_id": _clean_text(row.source_block_id),
            "common_ir_document_id": _clean_text(row.common_ir_document_id),
            "common_ir_block_id": _clean_text(row.common_ir_block_id),
            "common_ir_occurrence_ids": [
                value for value in row.common_ir_occurrence_ids if _clean_text(value)
            ],
        }
        for row in evidence
    ]
    return rows or [
        {
            "candidate_pack_block_id": _clean_text(fallback_block),
            "common_ir_document_id": None,
            "common_ir_block_id": None,
            "common_ir_occurrence_ids": [],
        }
    ]


def _fact_identity(fact: CplFact) -> str:
    return "|".join(
        str(value or "")
        for value in (
            fact.fact_id,
            fact.relation_id,
            fact.member,
            fact.member_index,
            fact.evidence_ref,
            fact.axis_code,
            fact.axis_quoted_text,
            fact.source_block_id,
            fact.start_char,
            fact.end_char,
        )
    )


def _ref_identity(ref: FitEvidenceRef) -> str:
    return "|".join((ref.fact_id, ref.field_name, ref.primary_component_id or ""))


def _entry_identity(entry: SimCommonEntry) -> str:
    return f"{entry.fact_id}|{entry.source_field}|{entry.common_key}"


class _EvidenceCollector:
    """Allocate deterministic per-decision evidence snapshots.

    The same source may be selected by more than one logical judgement. v0.2
    intentionally snapshots it per judgement, so logical code is in the UUID.
    """

    def __init__(self, analysis_run_id: str) -> None:
        self._analysis_run_id = analysis_run_id
        self.rows: list[dict[str, Any]] = []
        self._seen: set[str] = set()

    def add(
        self,
        *,
        logical_code: str,
        axis_type: str,
        sim_axis: str | None,
        role: str,
        side: str,
        field_name: str | None,
        raw_value: str | None,
        source_sha256: str | None,
        candidate_source_profile_id: str | None,
        source_identity: str,
        source_rows: Iterable[Mapping[str, Any]],
    ) -> list[str]:
        value = _clean_text(raw_value)
        if value is None:
            return []
        output: list[str] = []
        for row in source_rows:
            block = _clean_text(row.get("candidate_pack_block_id"))
            document = _clean_text(row.get("common_ir_document_id"))
            ir_block = _clean_text(row.get("common_ir_block_id"))
            occurrences = [
                str(item)
                for item in row.get("common_ir_occurrence_ids", [])
                if _clean_text(item)
            ]
            coordinates = "|".join(
                (block or "", document or "", ir_block or "", ",".join(occurrences))
            )
            seed = f"{self._analysis_run_id}\x1f{logical_code}\x1f{role}\x1f{side}\x1f{source_identity}\x1f{coordinates}"
            evidence_id = str(uuid5(EVIDENCE_NAMESPACE, seed))
            if evidence_id in self._seen:
                continue
            self._seen.add(evidence_id)
            self.rows.append(
                {
                    "evidence_id": evidence_id,
                    "logical_code": logical_code,
                    "axis_type": axis_type,
                    "sim_axis": sim_axis,
                    "role": role,
                    "side": side,
                    "field_name": _clean_text(field_name),
                    "raw_value": value,
                    "source_sha256": _clean_text(source_sha256),
                    "candidate_source_profile_id": _clean_text(
                        candidate_source_profile_id
                    ),
                    # raw audit context: DB never sends this to browser/chat.
                    "source_identity": source_identity,
                    "candidate_pack_block_id": block,
                    "common_ir_document_id": document,
                    "common_ir_block_id": ir_block,
                    "common_ir_cell_id": _clean_text(row.get("common_ir_cell_id")),
                    "common_ir_occurrence_ids": occurrences,
                }
            )
            output.append(evidence_id)
        return output


def _unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _cpl_detail(
    item: Any, *, collector: _EvidenceCollector, source_sha256: str | None
) -> dict[str, Any]:
    values: list[dict[str, Any]] = []
    source_fields: list[str] = []
    evidence_ids: list[str] = []
    code = cpl_display_code(item.field_code)
    for subfield in item.subfields:
        label = (
            _clean_text(subfield.profile_field_name)
            or _clean_text(subfield.profile_field)
            or "값"
        )
        if subfield.profile_field_name not in source_fields:
            source_fields.append(subfield.profile_field_name)
        for fact in subfield.facts:
            ids = collector.add(
                logical_code=code,
                axis_type="CPL",
                sim_axis=None,
                role="VALUE",
                side="REQUEST",
                field_name=subfield.profile_field,
                raw_value=fact.value_raw,
                source_sha256=source_sha256,
                candidate_source_profile_id=None,
                source_identity=_fact_identity(fact),
                source_rows=_source_rows(
                    fact.evidence, fallback_block=fact.source_block_id
                ),
            )
            if _clean_text(fact.value_raw):
                values.append(
                    {"label": label, "value": fact.value_raw, "evidence_ids": ids}
                )
                evidence_ids.extend(ids)
    return {
        "reason_code": item.status_reason,
        "reason": _CPL_SUMMARY.get(
            item.representative_status, "추가 확인이 필요합니다."
        ),
        "values": values,
        "source_fields": source_fields,
        "evidence_ids": _unique(evidence_ids),
    }


def _selected_refs(
    refs: Sequence[FitEvidenceRef], selected: Sequence[str]
) -> list[FitEvidenceRef]:
    wanted = set(selected)
    return [ref for ref in refs if ref.fact_id in wanted]


def _fit_side(
    refs: Sequence[FitEvidenceRef],
    *,
    collector: _EvidenceCollector,
    logical_code: str,
    role: str,
    selected: Sequence[str],
) -> tuple[dict[str, Any], list[str]]:
    evidence_ids: list[str] = []
    values: list[str] = []
    for ref in _selected_refs(refs, selected):
        ids = collector.add(
            logical_code=logical_code,
            axis_type="FIT",
            sim_axis=None,
            role=role,
            side="REQUEST",
            field_name=ref.field_name,
            raw_value=ref.value_raw,
            source_sha256=None,
            candidate_source_profile_id=None,
            source_identity=_ref_identity(ref),
            source_rows=_source_rows(ref.evidence),
        )
        evidence_ids.extend(ids)
        if _clean_text(ref.value_raw):
            values.append(str(ref.value_raw))
    ids = _unique(evidence_ids)
    return {
        "value_summary": " · ".join(_unique(values)) or None,
        "evidence_ids": ids,
    }, ids


def _fit_detail(relation: Any, *, collector: _EvidenceCollector) -> dict[str, Any]:
    code = fit_axis_code(relation.relation_id)
    performed = relation.status in {
        FitStatus.FIT,
        FitStatus.NEEDS_REVIEW,
        FitStatus.CONFLICT,
    }
    left, left_ids = _fit_side(
        relation.left.facts,
        collector=collector,
        logical_code=code,
        role="LEFT",
        selected=relation.used_left_fact_ids if performed else (),
    )
    right, right_ids = _fit_side(
        relation.right.facts,
        collector=collector,
        logical_code=code,
        role="RIGHT",
        selected=relation.used_right_fact_ids if performed else (),
    )
    return {
        "comparison_performed": performed,
        "reason_code": relation.reason_code,
        "reason": _FIT_SUMMARY.get(relation.status.value, "추가 확인이 필요합니다."),
        "left": left,
        "right": right,
        "evidence_ids": _unique([*left_ids, *right_ids]),
    }


def _sim_axis_raw(axis: SimAxisResult) -> dict[str, Any]:
    result = plain(axis)
    status = sim_display_status(axis.status)
    result["status"] = status
    result["summary"] = _SIM_SUMMARY[status]
    return result


def _entry_by_fact_id(
    profile: SimCommonProfile, axis: SimAxis
) -> dict[str, list[SimCommonEntry]]:
    output: dict[str, list[SimCommonEntry]] = {}
    for entry in profile.entries(axis):
        output.setdefault(entry.fact_id, []).append(entry)
    return output


def _sim_selected(
    entries: Mapping[str, Sequence[SimCommonEntry]],
    fact_ids: Sequence[str],
    *,
    collector: _EvidenceCollector,
    logical_code: str,
    sim_axis: str,
    role: str,
    side: str,
    candidate_source_profile_id: str | None,
    source_sha256: str | None,
) -> list[str]:
    evidence_ids: list[str] = []
    for fact_id in fact_ids:
        for entry in entries.get(fact_id, ()):
            evidence_ids.extend(
                collector.add(
                    logical_code=logical_code,
                    axis_type="SIM",
                    sim_axis=sim_axis,
                    role=role,
                    side=side,
                    field_name=entry.source_field,
                    raw_value=entry.value_raw,
                    source_sha256=source_sha256,
                    candidate_source_profile_id=candidate_source_profile_id,
                    source_identity=_entry_identity(entry),
                    source_rows=_source_rows(entry.evidence),
                )
            )
    return _unique(evidence_ids)


def _public_sim_axis(
    axis: SimAxisResult,
    *,
    request: SimCommonProfile | None,
    candidate: SimCommonProfile | None,
    collector: _EvidenceCollector,
    candidate_source_profile_id: str,
    candidate_logical_code: str,
    request_source_sha256: str | None,
) -> dict[str, Any]:
    name = _SIM_AXIS_NAME[axis.axis]
    # No verdict means no selected evidence (not every available input).
    performed = axis.status is not SimStatus.INSUFFICIENT
    request_ids = _sim_selected(
        _entry_by_fact_id(request, axis.axis) if request else {},
        axis.request_fact_ids if performed else (),
        collector=collector,
        logical_code=f"{candidate_logical_code}:{name}",
        sim_axis=name,
        role="LEFT",
        side="REQUEST",
        candidate_source_profile_id=None,
        source_sha256=request_source_sha256,
    )
    existing_ids = _sim_selected(
        _entry_by_fact_id(candidate, axis.axis) if candidate else {},
        axis.candidate_fact_ids if performed else (),
        collector=collector,
        logical_code=f"{candidate_logical_code}:{name}",
        sim_axis=name,
        role="RIGHT",
        side="EXISTING",
        candidate_source_profile_id=candidate_source_profile_id,
        source_sha256=None,
    )
    status = sim_display_status(axis.status)
    return {
        "code": axis.axis_id,
        "status": status,
        "summary": _SIM_SUMMARY[status],
        "reason_code": axis.reason_code,
        "reason": _SIM_SUMMARY[status],
        "common_points": list(axis.common_points),
        "differences": list(axis.differences),
        "request_evidence_ids": request_ids,
        "existing_evidence_ids": existing_ids,
    }


def _sim_summary(axes: Sequence[SimAxisResult]) -> str:
    # v0.2 candidate status excludes delivery from its priority rule.
    statuses = {
        sim_display_status(axis.status)
        for axis in axes
        if axis.axis in (SimAxis.PURPOSE, SimAxis.TARGET, SimAxis.CONTENT)
    }
    if "insufficient" in statuses:
        return "비교에 필요한 정보가 일부 부족합니다."
    if "different" in statuses:
        return "비교한 축에서 차이점이 확인되었습니다."
    if "partial" in statuses:
        return "비교한 축에서 공통점과 차이점이 함께 확인되었습니다."
    if statuses:
        return "주요 비교 축의 공통점이 확인되었습니다."
    return "비교 결과가 없습니다."


def _sim_candidate_status(axes: Sequence[SimAxisResult]) -> str:
    """Aggregate only core public axes; delivery never changes this status."""

    statuses = {
        sim_display_status(axis.status)
        for axis in axes
        if axis.axis in (SimAxis.PURPOSE, SimAxis.TARGET, SimAxis.CONTENT)
    }
    for status in ("insufficient", "different", "partial", "similar"):
        if status in statuses:
            return status
    return "insufficient"


def _candidate_metadata(
    value: Mapping[str, Any] | None, *, fallback_title: str | None
) -> dict[str, Any]:
    metadata = value if isinstance(value, Mapping) else {}
    source_url = metadata.get("source_url", metadata.get("detail_url"))
    return {
        "title": _clean_text(metadata.get("title")) or _clean_text(fallback_title),
        "support_field": _clean_text(metadata.get("support_field")),
        "apply_period": _clean_text(metadata.get("apply_period")),
        "ministry": _clean_text(metadata.get("ministry")),
        "executing_agency": _clean_text(metadata.get("executing_agency")),
        "registered_at": _clean_text(metadata.get("registered_at")),
        "notice_status": _clean_text(metadata.get("notice_status")),
        "source_url": _clean_text(source_url),
    }


def _fallback_run_id(profile: Mapping[str, Any]) -> str:
    return _clean_text(profile.get("profile_id")) or "unidentified-analysis-run"


def build_result_payload(
    *,
    analysis_run_id: str | None = None,
    profile: Mapping[str, Any],
    cpl: CplResult,
    fit: FitResult,
    sim: SimComparisonResult,
    sim_profiles: Mapping[str, SimCommonProfile],
    retrieval_similarities: Mapping[str, float],
    profile_version_ids: Mapping[str, str],
    ml_result: MlReferenceResult | None = None,
    retrieval: RetrievalDecision | None = None,
    candidate_metadata: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return canonical ``workspace.persist_analysis_result_core_v2`` input.

    This is the only raw/public split point. A public evidence reference can
    only be emitted after its selected UUIDv5 snapshot is in this same payload.
    """

    collector = _EvidenceCollector(analysis_run_id or _fallback_run_id(profile))
    axes: list[dict[str, Any]] = []
    for item in cpl.items:
        axes.append(
            {
                "axis_type": "CPL",
                "axis_code": cpl_display_code(item.field_code),
                "status": item.representative_status,
                "summary_text": _CPL_SUMMARY.get(
                    item.representative_status, "추가 확인이 필요합니다."
                ),
                "result_data": plain(item),
                "public_detail": _cpl_detail(
                    item, collector=collector, source_sha256=cpl.common_ir_source_sha256
                ),
            }
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
                "public_detail": _fit_detail(relation, collector=collector),
            }
        )

    request_profile_id = (
        sim.request_profile_id or _clean_text(profile.get("profile_id")) or ""
    )
    request_common = sim_profiles.get(request_profile_id)
    metadata_by_profile = candidate_metadata or {}
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
        candidate_common = sim_profiles.get(source_profile_id)
        logical_code = f"SIM:{source_profile_id}"
        raw_axes = {axis.axis: _sim_axis_raw(axis) for axis in candidate.axes}
        public_axes = {
            _SIM_AXIS_NAME[axis.axis]: _public_sim_axis(
                axis,
                request=request_common,
                candidate=candidate_common,
                collector=collector,
                candidate_source_profile_id=source_profile_id,
                candidate_logical_code=logical_code,
                request_source_sha256=cpl.common_ir_source_sha256,
            )
            for axis in candidate.axes
        }
        comparable = [
            _SIM_AXIS_NAME[axis.axis]
            for axis in candidate.axes
            if axis.status is not SimStatus.INSUFFICIENT
        ]
        candidates.append(
            {
                "source_profile_id": source_profile_id,
                "profile_version_pk": profile_version_pk,
                "rank": rank,
                # raw-only ranking inputs; public RPCs must never expose them.
                "similarity_score": retrieval_similarities.get(source_profile_id),
                "priority_score": candidate.internal_ranking.weighted_score,
                "status": _sim_candidate_status(candidate.axes),
                "summary_text": _sim_summary(candidate.axes),
                "comparable_axes": comparable,
                "metadata": _candidate_metadata(
                    metadata_by_profile.get(source_profile_id),
                    fallback_title=candidate.title,
                ),
                "purpose_result": raw_axes.get(SimAxis.PURPOSE, {}),
                "target_result": raw_axes.get(SimAxis.TARGET, {}),
                "support_result": raw_axes.get(SimAxis.CONTENT, {}),
                "delivery_result": raw_axes.get(SimAxis.DELIVERY, {}),
                "public_axes": public_axes,
            }
        )

    identity = profile.get("identity")
    identity = identity if isinstance(identity, Mapping) else {}
    program_name = identity.get("program_name") or identity.get("title_raw")
    decision = retrieval
    payload: dict[str, Any] = {
        "contract_version": RESULT_CONTRACT_VERSION,
        "program_name": program_name if isinstance(program_name, str) else None,
        "sim": {
            "status": getattr(decision, "status", "completed"),
            "reason_code": getattr(decision, "reason_code", None),
            "summary": getattr(decision, "summary", "유사 공고 검색을 완료했습니다."),
        },
        "axes": axes,
        "candidates": candidates,
        "evidences": collector.rows,
    }
    # Migration 26's fenced ML writer remains inside v2's transaction.
    if ml_result is not None:
        payload["ml"] = _public_ml_payload(ml_result)
    return payload


__all__ = [
    "EVIDENCE_NAMESPACE",
    "RESULT_CONTRACT_VERSION",
    "build_result_payload",
    "plain",
]
