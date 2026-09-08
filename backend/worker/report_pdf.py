"""Queued analysis -> result-screen projection used by the PDF renderer.

The worker contracts deliberately do not carry display scores.  This adapter
only projects their statuses, summaries, and source excerpts into the same
``CaseReport`` shape used by the result screen; it never exposes internal
ranking values to the renderer.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.schemas.cpl import CplStatus
from app.schemas.fit import FitStatus
from app.schemas.report import (
    AnalysisReport,
    CaseReport,
    CplDisplay,
    CplItemDisplay,
    FitAvailabilityDisplay,
    FitDisplay,
    FitRelationDisplay,
    ReportCaseDisplay,
    ReportExcerpt,
    ReportSimAxesDisplay,
    ReportSimAxisDisplay,
    ReportSimCandidateDisplay,
)
from app.schemas.sim import SimAxis, SimStatus

from worker.persistence import (
    AnalysisResults,
    _fit_summary as _persistence_fit_summary,
    _sim_axis_summary as _persistence_sim_axis_summary,
    _sim_result_summary as _persistence_sim_result_summary,
)


_CPL_STATUS = {
    "confirmed": CplStatus.PRESENT,
    "no_content": CplStatus.MISSING,
    "not_applicable": CplStatus.NOT_APPLICABLE,
    "needs_confirmation": CplStatus.NEEDS_CONFIRMATION,
}

def _excerpts(values: list[str]) -> list[ReportExcerpt]:
    seen: set[str] = set()
    result: list[ReportExcerpt] = []
    for value in values:
        value = value.strip()
        if value and value not in seen:
            seen.add(value)
            result.append(ReportExcerpt(excerpt=value))
    return result


def _cpl_item(item: Any) -> CplItemDisplay:
    excerpts: list[str] = []
    for subfield in item.subfields:
        for fact in subfield.facts:
            value = fact.value_raw or fact.text_basis or fact.selection_glyph_raw
            if value:
                excerpts.append(value)
    return CplItemDisplay(
        field_code=item.field_code.value,
        status=_CPL_STATUS.get(item.representative_status, CplStatus.NEEDS_CONFIRMATION),
        evidence=_excerpts(excerpts),
    )


def _fit_evidence(side: Any) -> list[ReportExcerpt]:
    return _excerpts([fact.value_raw for fact in side.facts if fact.value_raw])


def _fit_display(result: AnalysisResults) -> FitDisplay:
    if result.fit is None:
        return FitDisplay(
            module_status="UNAVAILABLE",
            availability=FitAvailabilityDisplay(assessable_count=0),
            relations=[],
        )
    relations = [
        FitRelationDisplay(
            relation_id=relation.relation_id,
            status=relation.status,
            summary=_persistence_fit_summary(relation),
            left_evidence=_fit_evidence(relation.left),
            right_evidence=_fit_evidence(relation.right),
        )
        for relation in result.fit.relations
    ]
    return FitDisplay(
        module_status="AVAILABLE",
        availability=FitAvailabilityDisplay(
            assessable_count=sum(
                relation.status != FitStatus.INSUFFICIENT
                for relation in result.fit.relations
            )
        ),
        relations=relations,
    )


def _profile_axis_evidence(profile: Any | None, axis: SimAxis) -> list[ReportExcerpt]:
    if profile is None:
        return []
    return _excerpts(
        [entry.value_raw for entry in profile.entries(axis) if entry.value_raw]
    )


def _sim_axis(
    axis: Any | None,
    axis_code: SimAxis,
    request_profile: Any | None,
    candidate_profile: Any | None,
) -> ReportSimAxisDisplay:
    if axis is None:
        return ReportSimAxisDisplay(
            status=SimStatus.INSUFFICIENT,
            summary="비교에 필요한 정보가 부족합니다.",
            request_evidence=_profile_axis_evidence(request_profile, axis_code),
            candidate_evidence=_profile_axis_evidence(candidate_profile, axis_code),
        )
    return ReportSimAxisDisplay(
        status=axis.status,
        summary=_persistence_sim_axis_summary(axis),
        common_points=list(axis.common_points),
        differences=list(axis.differences),
        request_evidence=_profile_axis_evidence(request_profile, axis_code),
        candidate_evidence=_profile_axis_evidence(candidate_profile, axis_code),
    )


def _sim_candidate(
    candidate: Any,
    request_profile: Any | None,
    candidate_profile: Any | None,
) -> ReportSimCandidateDisplay:
    axes = {axis.axis: axis for axis in candidate.axes}
    # The worker keeps source excerpts in the common profiles, separate from
    # the semantic comparison result.  Keep both sides attached to each axis
    # in the same projection the renderer receives.
    display_axes = ReportSimAxesDisplay(
        purpose=_sim_axis(
            axes.get(SimAxis.PURPOSE),
            SimAxis.PURPOSE,
            request_profile,
            candidate_profile,
        ),
        target=_sim_axis(
            axes.get(SimAxis.TARGET),
            SimAxis.TARGET,
            request_profile,
            candidate_profile,
        ),
        content=_sim_axis(
            axes.get(SimAxis.CONTENT),
            SimAxis.CONTENT,
            request_profile,
            candidate_profile,
        ),
        delivery=_sim_axis(
            axes.get(SimAxis.DELIVERY),
            SimAxis.DELIVERY,
            request_profile,
            candidate_profile,
        ),
    )
    title = candidate.title or "공고명 미확인"
    return ReportSimCandidateDisplay(
        title=title,
        source_url="",
        comparison_summary=_persistence_sim_result_summary(candidate.axes),
        axes=display_axes,
    )


def compose_queued_case_report(
    *,
    case_id: int,
    title: str,
    completed_at: datetime,
    results: AnalysisResults,
) -> CaseReport:
    """Build the score-free report projection for a queued result."""

    cpl_items = [_cpl_item(item) for item in (results.cpl.items if results.cpl else [])]
    confirmed_count = sum(
        item.status in {CplStatus.PRESENT, CplStatus.NOT_APPLICABLE}
        for item in cpl_items
    )
    candidates = []
    if results.sim is not None:
        request_profile = results.sim_profiles.get(results.sim.request_profile_id or "")
        candidates = [
            _sim_candidate(
                candidate,
                request_profile,
                results.sim_profiles.get(candidate.candidate_profile_id or ""),
            )
            for candidate in results.sim.candidates
        ]
    return CaseReport(
        case=ReportCaseDisplay(title=title, completed_at=completed_at),
        report=AnalysisReport(
            cpl=CplDisplay(confirmed_count=confirmed_count, items=cpl_items),
            fit=_fit_display(results),
            similar_candidates=candidates,
        ),
    )


__all__ = ["compose_queued_case_report"]
