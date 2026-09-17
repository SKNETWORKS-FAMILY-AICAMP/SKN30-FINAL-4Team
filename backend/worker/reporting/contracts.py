"""Strict payload consumed by the offline HTML report template."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.api.v1.results import (
    AnalysisCaseSummary,
    AnalysisCplSection,
    AnalysisFitSection,
    AnalysisSimSection,
    ResultEvidenceReadModel,
    SimCandidateDetailReadModel,
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReportMetadataV1(_StrictModel):
    source_analysis_run_id: UUID | None = None
    source_analysis_status: Literal["succeeded"] = "succeeded"
    generated_from_completed_at: datetime | None = None


class ReportMlMessageV1(_StrictModel):
    message: str | None


class ReportMlReferenceV1(_StrictModel):
    model_1: ReportMlMessageV1
    model_2: ReportMlMessageV1
    model_3: ReportMlMessageV1


class ReportPayloadV1(_StrictModel):
    """Versioned, public-safe data required to render one report."""

    schema_version: Literal["report_payload_v1"] = "report_payload_v1"
    case: AnalysisCaseSummary
    cpl: AnalysisCplSection
    fit: AnalysisFitSection
    sim: AnalysisSimSection
    sim_details: list[SimCandidateDetailReadModel]
    ml: ReportMlReferenceV1
    evidences: list[ResultEvidenceReadModel]
    report_metadata: ReportMetadataV1

    @classmethod
    def from_projection(
        cls,
        *,
        result_payload: object,
        sim_details: object,
        source_analysis_run_id: object = None,
    ) -> "ReportPayloadV1":
        if not isinstance(result_payload, Mapping):
            raise ValueError("analysis result projection must be an object")
        case = AnalysisCaseSummary.model_validate(result_payload.get("case"))
        cpl = AnalysisCplSection.model_validate(result_payload.get("cpl"))
        fit = AnalysisFitSection.model_validate(result_payload.get("fit"))
        sim = AnalysisSimSection.model_validate(result_payload.get("sim"))
        evidences = [
            ResultEvidenceReadModel.model_validate(item)
            for item in list(result_payload.get("evidences") or [])
        ]
        raw_ml = result_payload.get("ml")
        if not isinstance(raw_ml, Mapping):
            raise ValueError("analysis result ML projection must be an object")
        messages: dict[str, ReportMlMessageV1] = {}
        for key in ("model_1", "model_2", "model_3"):
            model = raw_ml.get(key)
            if not isinstance(model, Mapping):
                raise ValueError(f"analysis result {key} projection must be an object")
            messages[key] = ReportMlMessageV1(message=model.get("message"))
        ml = ReportMlReferenceV1.model_validate(messages)
        details = [
            SimCandidateDetailReadModel.model_validate(item)
            for item in list(sim_details or [])
        ]
        candidate_ids = {str(item.sim_candidate_id) for item in sim.candidates}
        detail_ids = {str(item.sim_candidate_id) for item in details}
        if detail_ids != candidate_ids:
            raise ValueError("SIM detail set does not match the analysis result")
        return cls(
            case=case,
            cpl=cpl,
            fit=fit,
            sim=sim,
            sim_details=details,
            ml=ml,
            evidences=evidences,
            report_metadata=ReportMetadataV1(
                source_analysis_run_id=source_analysis_run_id,
                generated_from_completed_at=case.completed_at,
            ),
        )
