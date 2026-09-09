"""통합 분석 결과 스키마 — 아키텍처 v2.2 및 result_envelope 계약 준수.

CPL, FIT, Model 1·2·3, SIM-DIF 및 챗봇이 공유하는 공통 결과 모델을 정의합니다.
"""
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, ConfigDict, Field


# 공통 Result envelope 상태 4종 (ml/serving/shared/result_envelope.py 호환)
ResultStatus = Literal["success", "failed", "not_available", "insufficient_data"]

# 전체 분석 실행 상태
ExecutionStatus = Literal["PROCESSING", "COMPLETED", "FAILED"]

# 전체 분석 품질 계약 (아키텍처 v2.2)
QualityStatus = Literal["COMPLETE", "PARTIAL"]

# 신뢰 등급
TrustGrade = Literal["trusted", "reference", "hold"]


class ModelInfo(BaseModel):
    name: str
    version: str
    model_type: str


class ModelError(BaseModel):
    code: str
    message: str


class Model1Result(BaseModel):
    support_type: str
    confidence: float
    trust_grade: TrustGrade


class AmountInfo(BaseModel):
    amount: Optional[int] = None
    unit: str = "KRW"


class Model2Reference(BaseModel):
    cohort_level: Optional[str] = None
    cohort_key: Dict[str, Any] = Field(default_factory=dict)
    sample_count: Optional[int] = None
    min_cohort: int = 30


class Model2Result(BaseModel):
    observed_per_recipient: AmountInfo
    predicted_per_recipient: AmountInfo
    cohort_percentile: Optional[float] = None
    level: str
    reference: Model2Reference


class Model3Reference(BaseModel):
    cohort_level: str
    cohort_key: str
    sample_count: int
    min_cohort: int = 20


class Model3Result(BaseModel):
    distance_percentile: float
    anomaly_level: Optional[str] = None
    anomaly_level_status: str = "threshold_undetermined"
    used_axes: List[str] = Field(default_factory=list)
    available_axis_count: int
    reference: Model3Reference


class ModelResultEnvelope(BaseModel):
    analysis_id: str
    model: ModelInfo
    status: ResultStatus
    input: Dict[str, Any] = Field(default_factory=dict)
    result: Optional[Any] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    error: Optional[ModelError] = None


class SimDifResult(BaseModel):
    comparisons: List[Dict[str, Any]] = Field(default_factory=list)
    dif: Optional[ModelResultEnvelope] = None
    retrieval_status: str = "SUCCESS"


class PipelineSummary(BaseModel):
    counts: Dict[str, int] = Field(default_factory=dict)
    by_status: Dict[str, List[str]] = Field(default_factory=dict)
    all_success: bool = False
    partial_failure: bool = False


class FrozenInspectionContext(BaseModel):
    """CPL 13축 점검 및 Reconciler 완료 후 발급되는 불변 컨텍스트 스냅샷."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    context_version: str = Field(
        ...,
        description="불변 컨텍스트 버전 식별자 (예: ctx-101-f8a3c9e2)",
    )
    case_id: int = Field(
        ...,
        description="사전협의 검토 사건 ID",
    )
    title: Optional[str] = Field(
        default=None,
        description="사업명 또는 요청서 제목",
    )
    document_title: Optional[str] = Field(
        default="사전협의 요청서",
        description="문서 제목",
    )
    document_text: str = Field(
        ...,
        description="파서에서 추출된 원본 문서 전문",
    )
    cpl_result: Optional[Any] = Field(
        default=None,
        description="Reconciler를 거쳐 확정된 CplResult 객체",
    )
    cpl_results: Dict[str, Any] = Field(
        default_factory=dict,
        description="Reconciler를 거쳐 확정된 CplResult 직렬화 딕셔너리",
    )
    frozen_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="컨텍스트 동결 시각 (UTC)",
    )
    context_hash: str = Field(
        default="",
        description="문서 및 CPL 결과의 무결성 해시",
    )

    # Duck-typing bridge properties for backward compatibility with legacy tests/inspectors
    @property
    def items(self) -> list[Any]:
        if self.cpl_result is not None:
            return getattr(self.cpl_result, "items", [])
        return self.cpl_results.get("items", [])

    @property
    def ruleset_version(self) -> str:
        if self.cpl_result is not None:
            return getattr(self.cpl_result, "ruleset_version", "")
        return self.cpl_results.get("ruleset_version", "")

    @property
    def confirmed_count(self) -> int:
        if self.cpl_result is not None:
            return getattr(self.cpl_result, "confirmed_count", 0)
        return self.cpl_results.get("confirmed_count", 0)

    @property
    def confirmation_rate(self) -> float:
        if self.cpl_result is not None:
            return getattr(self.cpl_result, "confirmation_rate", 0.0)
        return self.cpl_results.get("confirmation_rate", 0.0)

    @property
    def total_count(self) -> int:
        if self.cpl_result is not None:
            return getattr(self.cpl_result, "total_count", 0)
        return self.cpl_results.get("total_count", 0)


class IntegratedAnalysisResult(BaseModel):
    """최종 Aggregator 및 DB 저장을 위한 통합 분석 결과."""
    analysis_id: str
    case_id: int
    status: ExecutionStatus = "COMPLETED"
    quality: QualityStatus = "COMPLETE"

    # 세부 도메인 결과
    cpl: Optional[Dict[str, Any]] = None
    fit: Optional[Dict[str, Any]] = None
    model_1: Optional[ModelResultEnvelope] = None
    model_2: Optional[ModelResultEnvelope] = None
    model_3: Optional[ModelResultEnvelope] = None
    sim_dif: Optional[SimDifResult] = None

    summary: Optional[Dict[str, Any]] = None
    warnings: List[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    completed_at: Optional[datetime] = None
