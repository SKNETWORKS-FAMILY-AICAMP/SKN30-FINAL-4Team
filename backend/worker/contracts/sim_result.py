"""Slice 4a: 요청서·공고 프로파일 → 공통 SIM 비교 프로파일 / 4축 결과 계약
(초안 §7.2, §9.2, AGENTS.md "공통 SIM 비교 프로필 계약").

원칙 다섯 줄.

1. 공통 프로파일의 모든 값은 **출처 Fact 에서** 온다. ``fact_id`` · 원문 인용 ·
   그 Fact 의 ``evidence`` 를 함께 들고 다니며, 없는 값은 빈 목록이다.
   자리표시자를 만들지 않는다 (초안 §7.2).
2. 비교가 성립하지 않으면 판정이 아니라 ``INSUFFICIENT`` + reason code 다.
   "후보의 정보 부족" 과 "낮은 유사도" 를 구분한다 (초안 §7.2). 그래서 게이트에
   걸린 축은 절대 ``DIFFERENT`` 로 보고되지 않는다.
3. 실패는 축 단위로 격리한다. 하나가 무너져도 이미 계산된 축과 Rule 로 만든
   공통 프로파일은 그대로 남는다 (초안 §9.2.1 2항).
4. 매핑하지 않은 원본 필드는 조용히 버리지 않는다. ``unmapped_source_fields``
   와 진단으로 남긴다 (Slice 2 의 ``unmapped_profile_fields`` 와 같은 계약).
5. **점수는 내부 순위용이다.** ``InternalRanking`` 바깥에는 점수 필드가 없고,
   축 결과(``SimAxisResult``)에는 아예 존재하지 않는다. 초안 §7.2: "내부 순위
   계산은 사용자에게 퍼센트·중복 확률로 노출하지 않는다."
"""

from dataclasses import dataclass, field
from typing import Literal

from app.schemas.sim import SimAxis, SimReviewGrade, SimStatus

from .cpl_result import CplEvidence
from .profile_snapshot import (
    LLM_INVALID_RESPONSE,
    LLM_TIMEOUT,
    LLM_UNAVAILABLE,
    StageDiagnostic,
)

__all__ = [
    "SimAxis",
    "SimStatus",
    "SimReviewGrade",
    "CplEvidence",
    "StageDiagnostic",
    "SIM_AXIS_IDS",
    "COMMON_KEYS",
    "PURPOSE_KEYS",
    "TARGET_KEYS",
    "CONTENT_KEYS",
    "DELIVERY_KEYS",
    "ALL_COMMON_KEYS",
    "CANDIDATE_EVIDENCE_MISSING",
    "REQUEST_EVIDENCE_MISSING",
    "STRUCTURING_INCOMPLETE",
    "UNMAPPED_SOURCE_FIELD",
    "DUPLICATE_SPAN_ASSIGNMENT",
    "CLASSIFICATION_UNRESOLVED",
    "PARTIAL_OVERLAP",
    "NO_MEANING_OVERLAP",
    "LLM_INVALID_RESPONSE",
    "LLM_TIMEOUT",
    "LLM_UNAVAILABLE",
    "SIM_REASON_CODES",
    "SIM_VERDICT_REASON_CODES",
    "SimCommonEntry",
    "SimCommonProfile",
    "SimAxisResult",
    "InternalRanking",
    "SimCandidateResult",
    "SimComparisonResult",
]


# 축 id 는 app.schemas.sim 의 선언 순서 그대로다. 여기서 다시 정하지 않는다.
SIM_AXIS_IDS: dict[SimAxis, str] = {
    SimAxis.PURPOSE: "SIM-1",
    SimAxis.TARGET: "SIM-2",
    SimAxis.CONTENT: "SIM-3",
    SimAxis.DELIVERY: "SIM-4",
}

# AGENTS.md "공통 SIM 비교 프로필 계약" 의 하위 키다. 컨테이너마다 닫혀 있고,
# 여기 없는 키는 존재하지 않는 키다. LLM 응답 접지 검사가 이 집합을 본다.
PURPOSE_KEYS: tuple[str, ...] = ("problem_domain", "direction", "specific_objective")
TARGET_KEYS: tuple[str, ...] = (
    "target_group",
    "company_type",
    "industry",
    "region",
    "business_age",
    "revenue",
    "headcount",
    "certification_or_status",
    "other_condition",
    "exclusion",
)
CONTENT_KEYS: tuple[str, ...] = ("activity", "instrument", "item")
DELIVERY_KEYS: tuple[str, ...] = ("organization", "method", "procedure", "step_role")

COMMON_KEYS: dict[SimAxis, tuple[str, ...]] = {
    SimAxis.PURPOSE: PURPOSE_KEYS,
    SimAxis.TARGET: TARGET_KEYS,
    SimAxis.CONTENT: CONTENT_KEYS,
    SimAxis.DELIVERY: DELIVERY_KEYS,
}

ALL_COMMON_KEYS = frozenset(key for keys in COMMON_KEYS.values() for key in keys)

# 하위 키 이름이 컨테이너를 넘어 겹치면 LLM 응답 한 줄만 보고 컨테이너를
# 되찾을 수 없다. import 시점에 고정한다.
assert len(ALL_COMMON_KEYS) == sum(len(keys) for keys in COMMON_KEYS.values()), (
    "공통 하위 키 이름은 네 컨테이너를 통틀어 유일해야 한다"
)


# reason code. 값 자체가 API·로그에 실려 나가므로 StrEnum 대신 문자열 상수다
# (profile_snapshot.py · fit_result.py 와 같은 이유).
#
# 공고 쪽 근거가 없다. v0.2 공고 프로파일의 SIM-4 가 항상 여기로 온다.
CANDIDATE_EVIDENCE_MISSING = "CANDIDATE_EVIDENCE_MISSING"
# 요청서 쪽 근거가 없다.
REQUEST_EVIDENCE_MISSING = "REQUEST_EVIDENCE_MISSING"
# 원문은 있으나 상위 구조화가 끝나지 않아 근거 참조가 해소되지 않는다.
STRUCTURING_INCOMPLETE = "STRUCTURING_INCOMPLETE"
# 네 컨테이너 어디에도 매핑하지 않은 원본 필드. 표시하지 않을 뿐 버리지 않는다.
UNMAPPED_SOURCE_FIELD = "UNMAPPED_SOURCE_FIELD"
# 같은 (fact, 인용문) 을 여러 하위 키에 복제하려 했다. 초안 §7.2 가 금지한다.
DUPLICATE_SPAN_ASSIGNMENT = "DUPLICATE_SPAN_ASSIGNMENT"
# 분류를 호출했지만 접지를 통과한 배정이 하나도 없다.
CLASSIFICATION_UNRESOLVED = "CLASSIFICATION_UNRESOLVED"
# 축 판정 어휘. 모델은 이 두 개 밖의 reason 을 돌려줄 수 없다.
PARTIAL_OVERLAP = "PARTIAL_OVERLAP"
NO_MEANING_OVERLAP = "NO_MEANING_OVERLAP"

# 모델이 축 판정에 붙일 수 있는 reason code (어휘 밖은 None 으로 떨어진다).
SIM_VERDICT_REASON_CODES = frozenset(
    {
        PARTIAL_OVERLAP,
        NO_MEANING_OVERLAP,
        CANDIDATE_EVIDENCE_MISSING,
        REQUEST_EVIDENCE_MISSING,
    }
)

SIM_REASON_CODES = frozenset(
    {
        CANDIDATE_EVIDENCE_MISSING,
        REQUEST_EVIDENCE_MISSING,
        STRUCTURING_INCOMPLETE,
        UNMAPPED_SOURCE_FIELD,
        DUPLICATE_SPAN_ASSIGNMENT,
        CLASSIFICATION_UNRESOLVED,
        PARTIAL_OVERLAP,
        NO_MEANING_OVERLAP,
        LLM_INVALID_RESPONSE,
        LLM_TIMEOUT,
        LLM_UNAVAILABLE,
    }
)


@dataclass(frozen=True, slots=True)
class SimCommonEntry:
    """공통 프로파일 한 칸에 들어간 근거 한 건.

    ``value_raw`` 는 출처 Fact 의 원문이거나 (LLM 변환의 경우) 그 원문 안의
    검증된 인용문이다. 어느 쪽이든 새 값을 만들지 않는다. ``evidence`` 는
    출처 Fact 의 것을 그대로 옮긴다 (초안 §7.2 접지 보존).
    """

    fact_id: str
    source_field: str
    common_key: str
    value_raw: str | None
    evidence: list[CplEvidence] = field(default_factory=list)
    # Rule 변환인지 LLM 분류인지. 재현성 추적에 쓴다.
    origin: Literal["RULE", "LLM"] = "RULE"
    status: str | None = None


@dataclass(frozen=True, slots=True)
class SimCommonProfile:
    """한 문서의 공통 SIM 비교 프로파일.

    네 컨테이너는 항상 모든 하위 키를 갖는다. 값이 없으면 **빈 목록** 이고,
    "미기재" 같은 자리표시자를 넣지 않는다.

    ``benefit_overlap_evidence`` 와 ``scale_reference_evidence`` 는 일부러
    컨테이너 밖에 둔 것이다. AGENTS.md 가 각각 "SIM-2 수혜 가능 집합 비교에
    넣지 않고 BEN 중복수혜·참여제한 근거로 분리한다", "지원규모 정량값은
    Evidence 로 보존하지만 SIM-3 의 핵심 유사성 축에는 넣지 않는다" 로
    못박았다. 보존과 비교 투입은 다른 일이다.
    """

    source_profile_id: str | None
    schema_version: str | None
    common_ir_document_id: str | None
    purpose: dict[str, list[SimCommonEntry]]
    target: dict[str, list[SimCommonEntry]]
    content: dict[str, list[SimCommonEntry]]
    delivery: dict[str, list[SimCommonEntry]]
    benefit_overlap_evidence: list[SimCommonEntry] = field(default_factory=list)
    scale_reference_evidence: list[SimCommonEntry] = field(default_factory=list)
    unmapped_source_fields: list[str] = field(default_factory=list)
    # 출처 프로파일에 실제로 존재하는 id 집합. 게이트 3번이 이것으로 본다.
    fact_id_registry: frozenset[str] = frozenset()
    ruleset_version: str = ""
    prompt_version: str | None = None
    diagnostics: list[StageDiagnostic] = field(default_factory=list)

    def container(self, axis: SimAxis) -> dict[str, list[SimCommonEntry]]:
        return getattr(self, axis.value)

    def entries(self, axis: SimAxis) -> list[SimCommonEntry]:
        return [entry for rows in self.container(axis).values() for entry in rows]


@dataclass(frozen=True, slots=True)
class SimAxisResult:
    """축 하나의 결과.

    점수 필드는 이 계약에 **없다**. 축 결과는 사용자에게 보여질 수 있는
    표면이고, 점수는 ``InternalRanking`` 안에만 산다 (초안 §7.2).
    """

    axis: SimAxis
    axis_id: str
    status: SimStatus
    reason_code: str | None
    request_fact_ids: list[str] = field(default_factory=list)
    candidate_fact_ids: list[str] = field(default_factory=list)
    common_points: list[str] = field(default_factory=list)
    differences: list[str] = field(default_factory=list)
    diagnostics: list[StageDiagnostic] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class InternalRanking:
    """**내부 순위 전용** 집계. 사용자 응답으로 나가면 안 된다 (초안 §7.2).

    핵심 축(SIM-1·2·3) 중 하나라도 정보 부족이면 ``weighted_score`` 는 None
    이고 등급은 ``ON_HOLD`` 다. 부족한 근거를 낮은 점수로 바꿔치기하지 않는다.
    ``axis_weights`` 는 실제로 쓰인(재정규화된) 가중치다.
    """

    weighted_score: float | None
    review_grade: SimReviewGrade
    assessable_axis_count: int
    axis_weights: dict[str, float] = field(default_factory=dict)
    scoring_version: str = ""


@dataclass(frozen=True, slots=True)
class SimCandidateResult:
    """후보 하나의 4축 결과 + 내부 순위."""

    candidate_profile_id: str | None
    axes: list[SimAxisResult]
    internal_ranking: InternalRanking
    diagnostics: list[StageDiagnostic] = field(default_factory=list)

    def axis(self, axis: SimAxis) -> SimAxisResult:
        return next(result for result in self.axes if result.axis is axis)


@dataclass(frozen=True, slots=True)
class SimComparisonResult:
    """요청서 하나에 대한 후보 비교 묶음과 계보."""

    request_profile_id: str | None
    candidates: list[SimCandidateResult]
    model_profile: str
    ruleset_version: str
    prompt_version: str
    scoring_version: str
    diagnostics: list[StageDiagnostic] = field(default_factory=list)
