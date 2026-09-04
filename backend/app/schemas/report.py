from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.cpl import CplStatus
from app.schemas.fit import FIT_TOTAL_RELATIONS, FitRelationId, FitStatus
from app.schemas.sim import SimReviewGrade, SimStatus


REPORT_SCHEMA_VERSION = "alpha-report-v0.1"


class ReportEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_ref: str
    source_side: Literal["REQUEST", "ANNOUNCEMENT"]
    source_id: str
    field_code: str | None = None
    axis_code: str | None = None
    source_role: str | None = None
    excerpt: str
    normalized_value: Any | None = None
    block_id: str | None = None
    page_no: int | None = Field(default=None, ge=1)
    section_path: list[str] = Field(default_factory=list)
    source_locator: dict[str, Any] = Field(default_factory=dict)
    extraction_method: Literal["RULE", "LLM", "SOURCE"]
    extraction_version: str


class ReportCase(BaseModel):
    case_id: int
    title: str
    created_at: datetime
    completed_at: datetime


class SelfCheckItem(BaseModel):
    field_code: str
    status: CplStatus
    reason_code: str | None = None
    explanation: str | None = None
    occurrences: list[ReportEvidence] = Field(default_factory=list)


class SelfCheck(BaseModel):
    confirmed_count: int
    total_count: Literal[13] = 13
    confirmation_rate: float
    items: list[SelfCheckItem]
    ruleset_version: str
    prompt_version: str
    model_profile: str
    warnings: list[str] = Field(default_factory=list)


class StructuralScore(BaseModel):
    value: float | None
    numerator: int = Field(ge=0)
    denominator: int = Field(ge=0)
    assessable_count: int = Field(ge=0, le=FIT_TOTAL_RELATIONS)
    total_count: Literal[7] = FIT_TOTAL_RELATIONS
    scoring_version: str


class StructuralRelation(BaseModel):
    relation_id: FitRelationId
    status: FitStatus
    score: int | None = Field(default=None, ge=0, le=100)
    summary: str
    reason_code: str | None = None
    left_evidence: list[ReportEvidence] = Field(default_factory=list)
    right_evidence: list[ReportEvidence] = Field(default_factory=list)
    rule_version: str
    prompt_version: str


class StructuralConsistency(BaseModel):
    module_status: Literal["AVAILABLE", "UNAVAILABLE"]
    score: StructuralScore
    relations: list[StructuralRelation]
    ruleset_version: str
    prompt_version: str
    scoring_version: str
    model_profile: str
    warnings: list[str] = Field(default_factory=list)


ReviewIssueStatus = Literal[
    "MISSING",
    "NEEDS_CONFIRMATION",
    "PARSE_FAILED",
    "NEEDS_REVIEW",
    "CONFLICT",
    "INSUFFICIENT",
    "FOCUS_REVIEW",
    "GENERAL_REVIEW",
]


class ReviewIssue(BaseModel):
    issue_id: str
    source: Literal["CPL", "FIT", "SIM"]
    reference_id: str
    status: ReviewIssueStatus
    summary: str
    reason_code: str | None = None
    evidence: list[ReportEvidence] = Field(default_factory=list)


class ReportSimAxis(BaseModel):
    axis_id: Literal["SIM-1", "SIM-2", "SIM-3", "SIM-4"]
    status: SimStatus
    score: int | None = Field(default=None, ge=0, le=100)
    summary: str
    common_points: list[str] = Field(default_factory=list)
    differences: list[str] = Field(default_factory=list)
    request_evidence: list[ReportEvidence] = Field(default_factory=list)
    candidate_evidence: list[ReportEvidence] = Field(default_factory=list)
    reason_code: str | None = None


class ReportSimAxes(BaseModel):
    purpose: ReportSimAxis
    target: ReportSimAxis
    content: ReportSimAxis
    delivery: ReportSimAxis


class ReportSimCandidate(BaseModel):
    rank: int = Field(ge=1)
    announcement_id: str
    announcement_version_id: int = Field(ge=1)
    title: str
    source_url: str
    semantic_similarity: float = Field(ge=-1, le=1)
    semantic_similarity_display: int = Field(ge=0, le=100)
    weighted_score: float | None = Field(default=None, ge=0, le=100)
    assessable_axis_count: int = Field(ge=0, le=4)
    review_grade: SimReviewGrade
    comparison_summary: str
    axes: ReportSimAxes
    warnings: list[str] = Field(default_factory=list)
    ruleset_version: str
    prompt_version: str
    scoring_version: str
    model_profile: str


class ReportJsonV01(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[REPORT_SCHEMA_VERSION] = REPORT_SCHEMA_VERSION
    case: ReportCase
    ui_status: Literal["COMPLETED"] = "COMPLETED"
    self_check: SelfCheck
    structural_consistency: StructuralConsistency
    review_issues: list[ReviewIssue] = Field(default_factory=list)
    similar_candidates: list[ReportSimCandidate] = Field(default_factory=list)
    ben_references: list[dict[str, Any]] = Field(default_factory=list)
    differences: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def reserved_fields_stay_empty(self) -> "ReportJsonV01":
        if self.ben_references or self.differences:
            raise ValueError("Reserved alpha report fields must stay empty")
        return self


class ReportExcerpt(BaseModel):
    """The only evidence detail the result screen needs to render."""

    excerpt: str


class CplItemDisplay(BaseModel):
    """항목 하나의 점검 결과.

    요청 유형 체크박스를 읽어 보여주던 display 는 없앴다. 문서의 체크 표시를
    정확히 읽지 못해 늘 미선택으로 나왔고, 프론트도 빼기로 했다. 요청 유형도
    다른 12개 항목과 같이 상태와 근거만 준다.
    """

    field_code: str
    status: CplStatus
    evidence: list[ReportExcerpt] = Field(default_factory=list)


class CplDisplay(BaseModel):
    """화면 이름은 요청자료 완전성·기초구조 점검.

    확인율 퍼센트는 담지 않는다. 화면은 13개 중 몇 개인지만 쓴다.
    항목별 상태를 그대로 주고 어떻게 묶어 보여줄지는 프론트가 정한다.
    """

    confirmed_count: int
    items: list[CplItemDisplay]


class FitAvailabilityDisplay(BaseModel):
    assessable_count: int = Field(ge=0, le=FIT_TOTAL_RELATIONS)


class FitRelationDisplay(BaseModel):
    """관계는 코드로만 준다. 사람이 읽을 이름은 프론트가 갖는다."""

    relation_id: FitRelationId
    status: FitStatus
    summary: str
    left_evidence: list[ReportExcerpt] = Field(default_factory=list)
    right_evidence: list[ReportExcerpt] = Field(default_factory=list)


class FitDisplay(BaseModel):
    """화면 이름은 내부 정합성 점검. 점수는 담지 않는다."""

    module_status: Literal["AVAILABLE", "UNAVAILABLE"]
    availability: FitAvailabilityDisplay
    relations: list[FitRelationDisplay]


class ReportSimAxisDisplay(BaseModel):
    status: SimStatus
    summary: str
    common_points: list[str] = Field(default_factory=list)
    differences: list[str] = Field(default_factory=list)
    request_evidence: list[ReportExcerpt] = Field(default_factory=list)
    candidate_evidence: list[ReportExcerpt] = Field(default_factory=list)


class ReportSimAxesDisplay(BaseModel):
    purpose: ReportSimAxisDisplay
    target: ReportSimAxisDisplay
    content: ReportSimAxisDisplay
    delivery: ReportSimAxisDisplay


class ReportSimCandidateDisplay(BaseModel):
    """순위와 유사도 점수는 담지 않는다. 화면이 쓰지 않는다."""

    title: str
    source_url: str
    comparison_summary: str
    axes: ReportSimAxesDisplay


class ReportCaseDisplay(BaseModel):
    """화면 Header 에 쓰는 검사 건 정보."""

    title: str
    completed_at: datetime


class AnalysisReport(BaseModel):
    """Result-screen projection of the immutable, internal report snapshot."""

    cpl: CplDisplay
    fit: FitDisplay
    similar_candidates: list[ReportSimCandidateDisplay] = Field(default_factory=list)


class CaseReport(BaseModel):
    """분석 결과 화면과 과거 이력 상세가 함께 쓰는 조각."""

    case: ReportCaseDisplay
    report: AnalysisReport
