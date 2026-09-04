from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.cpl import (
    CplAxisCode,
    CplFieldCode,
    CplOccurrence,
    CplSourceRole,
)


class FitRelationId(StrEnum):
    FIT_1 = "FIT-1"
    FIT_2 = "FIT-2"
    FIT_3 = "FIT-3"
    FIT_4 = "FIT-4"
    FIT_5 = "FIT-5"
    FIT_6 = "FIT-6"
    FIT_7 = "FIT-7"


FIT_RELATIONS: tuple[FitRelationId, ...] = tuple(FitRelationId)
FIT_TOTAL_RELATIONS = len(FIT_RELATIONS)


class FitStatus(StrEnum):
    FIT = "FIT"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    CONFLICT = "CONFLICT"
    INSUFFICIENT = "INSUFFICIENT"


class FitInputFeedbackReason(StrEnum):
    """Reasons FIT can send back to CPL before final relation analysis."""

    REQUIRED_AXIS_MISSING = "REQUIRED_AXIS_MISSING"
    SOURCE_ROLE_MISSING = "SOURCE_ROLE_MISSING"
    EVIDENCE_TOO_BROAD = "EVIDENCE_TOO_BROAD"
    POSSIBLE_MISCLASSIFICATION = "POSSIBLE_MISCLASSIFICATION"
    CONFLICTING_OCCURRENCES = "CONFLICTING_OCCURRENCES"
    NORMALIZATION_INCOMPLETE = "NORMALIZATION_INCOMPLETE"


class FitInputFeedback(BaseModel):
    """Internal, typed request from FIT to the CPL analyzer.

    This is not an API result.  FIT may describe what it cannot consume, but it
    cannot add evidence or change a CPL status.  The orchestrator uses this
    contract only to select a bounded CPL recheck.
    """

    model_config = ConfigDict(extra="forbid")

    relation_id: FitRelationId
    side: Literal["left", "right"]
    field_code: CplFieldCode
    reason_code: FitInputFeedbackReason
    required_axis_codes: list[CplAxisCode] = Field(default_factory=list)
    required_source_roles: list[CplSourceRole | None] = Field(default_factory=list)


class FitRelationResult(BaseModel):
    relation_id: FitRelationId
    status: FitStatus
    score: int | None = Field(default=None, ge=0, le=100)
    summary: str = Field(min_length=1)
    left_evidence: list[CplOccurrence] = Field(default_factory=list)
    right_evidence: list[CplOccurrence] = Field(default_factory=list)
    reason_code: str | None = None
    rule_version: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1)


class FitScoreSummary(BaseModel):
    value: float | None
    numerator: int = Field(ge=0)
    denominator: int = Field(ge=0)
    assessable_count: int = Field(ge=0, le=FIT_TOTAL_RELATIONS)
    total_count: Literal[7] = FIT_TOTAL_RELATIONS
    scoring_version: str = Field(min_length=1)


class FitResult(BaseModel):
    relations: list[FitRelationResult]
    score: FitScoreSummary
    warnings: list[str] = Field(default_factory=list)
    ruleset_version: str
    prompt_version: str
    scoring_version: str
    model_profile: str

    @model_validator(mode="after")
    def require_exact_relations(self) -> "FitResult":
        ids = [relation.relation_id for relation in self.relations]
        if len(ids) != FIT_TOTAL_RELATIONS or set(ids) != set(FIT_RELATIONS):
            raise ValueError("FIT result must contain each of the 7 relations exactly once")
        if len(ids) != len(set(ids)):
            raise ValueError("FIT result contains duplicate relations")
        assessable_count = sum(
            relation.status != FitStatus.INSUFFICIENT for relation in self.relations
        )
        if any(
            (relation.status == FitStatus.INSUFFICIENT) != (relation.score is None)
            for relation in self.relations
        ):
            raise ValueError("FIT relation status and score are inconsistent")
        if self.score.assessable_count != assessable_count:
            raise ValueError("FIT assessable relation count is inconsistent")
        if self.score.scoring_version != self.scoring_version:
            raise ValueError("FIT scoring versions are inconsistent")
        if any(
            relation.rule_version != self.ruleset_version
            or relation.prompt_version != self.prompt_version
            for relation in self.relations
        ):
            raise ValueError("FIT relation versions are inconsistent")
        return self


class FitSemanticRelation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    relation_id: FitRelationId
    status: FitStatus
    summary: str = Field(min_length=1)
    left_evidence_refs: list[str]
    right_evidence_refs: list[str]
    reason_code: str | None


class FitSemanticResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    relations: list[FitSemanticRelation]


class FitScoringPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str = Field(min_length=1)
    status_scores: dict[FitStatus, int | None]
    weights: dict[FitRelationId, int]

    @model_validator(mode="after")
    def require_complete_policy(self) -> "FitScoringPolicy":
        if set(self.status_scores) != set(FitStatus):
            raise ValueError("FIT scoring policy must define every status")
        if set(self.weights) != set(FIT_RELATIONS):
            raise ValueError("FIT scoring policy must define every relation")
        if self.status_scores[FitStatus.INSUFFICIENT] is not None:
            raise ValueError("INSUFFICIENT must be excluded from FIT scoring")
        if any(
            score is not None and not 0 <= score <= 100
            for score in self.status_scores.values()
        ):
            raise ValueError("FIT status scores must be between 0 and 100")
        if any(weight <= 0 for weight in self.weights.values()):
            raise ValueError("FIT relation weights must be positive")
        return self
