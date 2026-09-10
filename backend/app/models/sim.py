"""SIM graph state. Final duplicate judgement is intentionally absent."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, TypedDict


class SimAxis(StrEnum):
    PURPOSE = "purpose"
    TARGET = "target"
    CONTENT = "content"
    DELIVERY = "delivery"


SIM_CORE_AXES: tuple[SimAxis, ...] = (SimAxis.PURPOSE, SimAxis.TARGET, SimAxis.CONTENT)
SIM_AXES: tuple[SimAxis, ...] = SIM_CORE_AXES + (SimAxis.DELIVERY,)


class SimAxisStatus(StrEnum):
    SIMILAR = "SIMILAR"
    PARTIAL = "PARTIAL"
    DIFFERENT = "DIFFERENT"
    INSUFFICIENT = "INSUFFICIENT"


class SimReviewGrade(StrEnum):
    FOCUS_REVIEW = "FOCUS_REVIEW"
    GENERAL_REVIEW = "GENERAL_REVIEW"
    LOW_PRIORITY = "LOW_PRIORITY"
    ON_HOLD = "ON_HOLD"


@dataclass(frozen=True, slots=True)
class SimEvidence:
    side: str
    axis: SimAxis
    excerpt: str
    evidence_ref: str


@dataclass(frozen=True, slots=True)
class SimCandidate:
    candidate_id: str
    title: str = ""
    evidence: tuple[SimEvidence, ...] = ()
    exclusive_nonoverlap_axes: tuple[SimAxis, ...] = ()


@dataclass(frozen=True, slots=True)
class SimAxisComparison:
    axis: SimAxis
    status: SimAxisStatus
    reason_code: str | None
    request_evidence_refs: tuple[str, ...]
    candidate_evidence_refs: tuple[str, ...]
    summary: str


@dataclass(frozen=True, slots=True)
class SimComparison:
    candidate_id: str
    title: str
    axes: tuple[SimAxisComparison, ...]
    review_grade: SimReviewGrade
    comparison_summary: str
    assessable_axis_count: int
    pruned: bool = False


@dataclass
class SimGraphState:
    request_evidence: tuple[SimEvidence, ...] = ()
    candidates: tuple[SimCandidate, ...] = ()
    retrieved: tuple[SimCandidate, ...] = ()
    pruned: tuple[SimCandidate, ...] = ()
    comparisons: tuple[SimComparison, ...] = ()
    ranked: tuple[SimComparison, ...] = ()
    warnings: list[str] = field(default_factory=list)
    graph_backend: str = "fallback"
    langgraph_used: bool = False


class SimGraphStateDict(TypedDict, total=False):
    request_evidence: list[dict[str, Any]]
    candidates: list[dict[str, Any]]
    retrieved: list[dict[str, Any]]
    pruned: list[dict[str, Any]]
    comparisons: list[dict[str, Any]]
    ranked: list[dict[str, Any]]
    warnings: list[str]
    graph_backend: str
    langgraph_used: bool
