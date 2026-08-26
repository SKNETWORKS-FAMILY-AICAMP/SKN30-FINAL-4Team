from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SimAxis(StrEnum):
    PURPOSE = "purpose"
    TARGET = "target"
    CONTENT = "content"
    DELIVERY = "delivery"


SIM_AXIS_IDS: dict[SimAxis, str] = {
    SimAxis.PURPOSE: "SIM-1",
    SimAxis.TARGET: "SIM-2",
    SimAxis.CONTENT: "SIM-3",
    SimAxis.DELIVERY: "SIM-4",
}


class SimStatus(StrEnum):
    SIMILAR = "SIMILAR"
    PARTIAL = "PARTIAL"
    DIFFERENT = "DIFFERENT"
    INSUFFICIENT = "INSUFFICIENT"


class SimReviewGrade(StrEnum):
    FOCUS_REVIEW = "FOCUS_REVIEW"
    GENERAL_REVIEW = "GENERAL_REVIEW"
    LOW_PRIORITY = "LOW_PRIORITY"
    ON_HOLD = "ON_HOLD"


class SimEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_ref: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    profile_key: str = Field(min_length=1)
    excerpt: str = Field(min_length=1)
    normalized_value: Any | None = None
    page_no: int | None = Field(default=None, ge=1)
    section_path: list[str] = Field(default_factory=list)
    source_locator: dict[str, Any] = Field(default_factory=dict)
    extraction_method: Literal["RULE", "LLM", "SOURCE"]
    extraction_version: str = Field(min_length=1)


class SimAxisResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    axis_id: Literal["SIM-1", "SIM-2", "SIM-3", "SIM-4"]
    status: SimStatus
    score: int | None = Field(default=None, ge=0, le=100)
    summary: str = Field(min_length=1)
    common_points: list[str] = Field(default_factory=list)
    differences: list[str] = Field(default_factory=list)
    request_evidence: list[SimEvidence] = Field(default_factory=list)
    candidate_evidence: list[SimEvidence] = Field(default_factory=list)
    reason_code: str | None = None

    @model_validator(mode="after")
    def require_assessable_evidence(self) -> "SimAxisResult":
        if (self.status == SimStatus.INSUFFICIENT) != (self.score is None):
            raise ValueError(
                "INSUFFICIENT is the only SIM status excluded from scoring"
            )
        if self.status != SimStatus.INSUFFICIENT and (
            not self.request_evidence or not self.candidate_evidence
        ):
            raise ValueError("Assessable SIM axes require evidence on both sides")
        if self.status != SimStatus.SIMILAR and not self.reason_code:
            raise ValueError("Non-SIMILAR SIM axes require a reason code")
        return self


class SimAxes(BaseModel):
    model_config = ConfigDict(extra="forbid")

    purpose: SimAxisResult
    target: SimAxisResult
    content: SimAxisResult
    delivery: SimAxisResult

    @model_validator(mode="after")
    def require_axis_ids(self) -> "SimAxes":
        values = {
            SimAxis.PURPOSE: self.purpose,
            SimAxis.TARGET: self.target,
            SimAxis.CONTENT: self.content,
            SimAxis.DELIVERY: self.delivery,
        }
        if any(result.axis_id != SIM_AXIS_IDS[axis] for axis, result in values.items()):
            raise ValueError("SIM axis IDs do not match their result fields")
        return self


class SimComparisonResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rank: int = Field(ge=1)
    announcement_id: str = Field(min_length=1)
    announcement_version_id: int = Field(ge=1)
    title: str = Field(min_length=1)
    source_url: str = Field(min_length=1)
    semantic_similarity: float = Field(ge=-1, le=1)
    semantic_similarity_display: int = Field(ge=0, le=100)
    axes: SimAxes
    weighted_score: float | None = Field(default=None, ge=0, le=100)
    assessable_axis_count: int = Field(default=0, ge=0, le=4)
    review_grade: SimReviewGrade = SimReviewGrade.ON_HOLD
    comparison_summary: str = Field(min_length=1)
    warnings: list[str] = Field(default_factory=list)
    ruleset_version: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1)
    scoring_version: str = Field(min_length=1)
    model_profile: str = Field(min_length=1)

    @model_validator(mode="after")
    def require_score_summary(self) -> "SimComparisonResult":
        results = (
            self.axes.purpose,
            self.axes.target,
            self.axes.content,
            self.axes.delivery,
        )
        assessable_count = sum(result.score is not None for result in results)
        if self.assessable_axis_count != assessable_count:
            raise ValueError("SIM assessable axis count is inconsistent")
        if assessable_count == 0:
            if (
                self.weighted_score is not None
                or self.review_grade != SimReviewGrade.ON_HOLD
            ):
                raise ValueError("Unassessable SIM comparison must be ON_HOLD")
        elif self.weighted_score is None or self.review_grade == SimReviewGrade.ON_HOLD:
            raise ValueError(
                "Assessable SIM comparison requires score and review grade"
            )
        return self


class SimSemanticAxisResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: SimStatus
    summary: str = Field(min_length=1)
    common_points: list[str]
    differences: list[str]
    request_evidence_refs: list[str]
    candidate_evidence_refs: list[str]
    reason_code: str | None


class SimSemanticAxes(BaseModel):
    model_config = ConfigDict(extra="forbid")

    purpose: SimSemanticAxisResult
    target: SimSemanticAxisResult
    content: SimSemanticAxisResult
    delivery: SimSemanticAxisResult


class SimSemanticResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    axes: SimSemanticAxes
    comparison_summary: str = Field(min_length=1)


class SimGradeThresholds(BaseModel):
    model_config = ConfigDict(extra="forbid")

    focus_review_min: int = Field(ge=0, le=100)
    general_review_min: int = Field(ge=0, le=100)

    @model_validator(mode="after")
    def require_threshold_order(self) -> "SimGradeThresholds":
        if self.general_review_min >= self.focus_review_min:
            raise ValueError("SIM grade thresholds must be strictly ordered")
        return self


class SimRetrievalPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    top_k: int = Field(ge=1, le=100)


class SimScoringPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str = Field(min_length=1)
    axis_weights: dict[SimAxis, float]
    status_scores: dict[SimStatus, int | None]
    grades: SimGradeThresholds
    retrieval: SimRetrievalPolicy

    @model_validator(mode="after")
    def require_complete_policy(self) -> "SimScoringPolicy":
        if set(self.axis_weights) != set(SimAxis):
            raise ValueError("SIM scoring policy must define every axis")
        if set(self.status_scores) != set(SimStatus):
            raise ValueError("SIM scoring policy must define every status")
        if any(weight <= 0 for weight in self.axis_weights.values()):
            raise ValueError("SIM axis weights must be positive")
        if self.status_scores[SimStatus.INSUFFICIENT] is not None:
            raise ValueError("INSUFFICIENT must be excluded from SIM scoring")
        if any(
            self.status_scores[status] is None
            for status in SimStatus
            if status != SimStatus.INSUFFICIENT
        ):
            raise ValueError("Assessable SIM statuses must define scores")
        if any(
            score is not None and not 0 <= score <= 100
            for score in self.status_scores.values()
        ):
            raise ValueError("SIM status scores must be between 0 and 100")
        return self
