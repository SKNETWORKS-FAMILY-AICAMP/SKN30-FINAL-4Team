"""Slice 3: Request Profile v0.1.2 → FIT 7관계 결과 계약 (초안 §7.1, §9.0–§9.2.1).

원칙 네 줄.

1. 좌우 근거는 **CPL 결과에서** 만든다. 다른 FIT 관계의 판정 결과를 근거로
   재활용하지 않는다 (초안 §7.1 "실제 relation 근거를 사용한다").
2. 비교가 성립하지 않으면 판정이 아니라 ``INSUFFICIENT`` + reason code 다.
   충돌이 확인되지 않았다는 사실이 "맞다" 의 근거가 되지 않는다.
3. 실패는 관계 단위로 격리한다. 하나가 무너져도 이미 계산된 관계와 그
   근거는 그대로 남는다 (초안 §9.2.1 2항).
4. 없는 reason 은 ``None`` 이지 문자열 ``"null"`` 이 아니다 (초안 §9.5).

점수·확인율·비율·등급 필드는 이 계약에 존재하지 않는다. ``app.schemas.fit``
쪽 옛 계약에는 ``score`` 가 남아 있지만 그것은 구 API 표면이고, 워커 결과가
집계될 수 있는 자리를 새로 만들지 않는다 (cpl_result.py 와 같은 이유).
"""

from dataclasses import dataclass, field
from enum import StrEnum

from ..quantities import QuantitySpan
from .cpl_result import (
    PURPOSE_AXIS_CODES,
    PURPOSE_AXIS_UNRESOLVED,
    CplAxisCode,
    CplEvidence,
    PurposeAxisAssignment,
    PurposeAxisClassification,
)
from .profile_snapshot import (
    LLM_INVALID_RESPONSE,
    LLM_TIMEOUT,
    LLM_UNAVAILABLE,
    StageDiagnostic,
)

__all__ = [
    "COMPARISON_EVIDENCE_MISSING",
    "COMPARISON_VALUE_INVALID",
    "EVIDENCE_REF_UNRESOLVED",
    "FIT_DISPLAY_STATUSES",
    "FIT_NOT_APPLICABLE",
    "FIT_REASON_CODES",
    "HIERARCHY_COMPARISON_NOT_AVAILABLE",
    "LLM_INVALID_RESPONSE",
    "LLM_TIMEOUT",
    "LLM_UNAVAILABLE",
    "NO_CONDITIONS_SPECIFIED",
    "NUMERIC_MISMATCH",
    "PURPOSE_AXIS_CODES",
    "PURPOSE_AXIS_UNRESOLVED",
    "SELF_COMPARISON",
    "SINGLE_SIDED_NO_CONFLICT",
    "CplAxisCode",
    "CplEvidence",
    "FitEvidenceRef",
    "FitRelationId",
    "FitRelationResult",
    "FitResult",
    "FitSide",
    "FitStatus",
    "PurposeAxisAssignment",
    "PurposeAxisClassification",
    "StageDiagnostic",
    "fit_axis_code",
]


class FitRelationId(StrEnum):
    """The stable seven-relation FIT vocabulary from the former API schema."""

    FIT_1 = "FIT-1"
    FIT_2 = "FIT-2"
    FIT_3 = "FIT-3"
    FIT_4 = "FIT-4"
    FIT_5 = "FIT-5"
    FIT_6 = "FIT-6"
    FIT_7 = "FIT-7"


class FitStatus(StrEnum):
    """Worker FIT verdicts.  Values intentionally match the old API contract."""

    FIT = "FIT"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    CONFLICT = "CONFLICT"
    INSUFFICIENT = "INSUFFICIENT"
    NOT_APPLICABLE = "NOT_APPLICABLE"


# 축 어휘 정의는 cpl_result 로 옮겼다. 축을 확정하는 쪽이 어휘도 갖는다.


# ------------------------------------------------------------- 표시 어휘

# ``NOT_APPLICABLE`` means the FIT-4 hierarchy itself does not exist.  It is
# deliberately a real worker enum rather than an output-only alias: a broken
# or evidence-poor hierarchy still has a comparison subject and is therefore
# ``INSUFFICIENT``.
FIT_NOT_APPLICABLE = FitStatus.NOT_APPLICABLE.value

FIT_DISPLAY_STATUSES = frozenset(
    {status.value for status in FitStatus} | {FIT_NOT_APPLICABLE}
)


def fit_axis_code(relation_id: FitRelationId) -> str:
    """Return the stable frontend code (``FIT-1`` through ``FIT-7``)."""

    return relation_id.value


# reason code. 값 자체가 API·로그에 그대로 실려 나가므로 StrEnum 대신
# 문자열 상수로 둔다 (profile_snapshot.py·cpl_result.py 와 같은 이유).
#
# 한쪽 근거가 아예 없거나 상위 단계가 근거를 확정하지 못했다.
COMPARISON_EVIDENCE_MISSING = "COMPARISON_EVIDENCE_MISSING"
# 정량 정규화가 값을 인식하지 못했다. 추측해서 채우지 않고 비교에서 뺀다.
COMPARISON_VALUE_INVALID = "COMPARISON_VALUE_INVALID"
# 근거로 인용된 fact_id 가 프로파일에 없다.
EVIDENCE_REF_UNRESOLVED = "EVIDENCE_REF_UNRESOLVED"
# 예전 FIT-4 정책 게이트의 reason code. 현재 FIT-4는 명시된 parent-child edge를
# 직접 비교하며, 기존 저장 결과와의 호환 때문에 상수만 유지한다.
HIERARCHY_COMPARISON_NOT_AVAILABLE = "HIERARCHY_COMPARISON_NOT_AVAILABLE"
# 문서가 조건을 적지 않았다. 조건 추출 실패와 구분한다 (초안 §7.1 FIT-5).
NO_CONDITIONS_SPECIFIED = "NO_CONDITIONS_SPECIFIED"
# 같은 축에서 좌우 값 집합이 다르다.
NUMERIC_MISMATCH = "NUMERIC_MISMATCH"
# 목적 의미 축 보완이 필요한 축을 만들어내지 못했다.
# 정의는 cpl_result 로 옮겼다. 축을 확정하는 쪽이 사유도 갖는다.
# 좌우가 같은 fact 를 가리킨다. 자기 자신과 비교하지 않는다 (초안 §7.1).
SELF_COMPARISON = "SELF_COMPARISON"
# 한 축이 한쪽에만 있다. 충돌이 없다는 것이 대응했다는 뜻은 아니다.
SINGLE_SIDED_NO_CONFLICT = "SINGLE_SIDED_NO_CONFLICT"

FIT_REASON_CODES = frozenset(
    {
        COMPARISON_EVIDENCE_MISSING,
        COMPARISON_VALUE_INVALID,
        EVIDENCE_REF_UNRESOLVED,
        HIERARCHY_COMPARISON_NOT_AVAILABLE,
        LLM_INVALID_RESPONSE,
        LLM_TIMEOUT,
        LLM_UNAVAILABLE,
        NO_CONDITIONS_SPECIFIED,
        NUMERIC_MISMATCH,
        PURPOSE_AXIS_UNRESOLVED,
        SELF_COMPARISON,
        SINGLE_SIDED_NO_CONFLICT,
    }
)


@dataclass(frozen=True, slots=True)
class FitEvidenceRef:
    """비교 한쪽에 실린 근거 한 건.

    ``fact_id`` 는 프로파일의 것 그대로다. ``delivery_relations`` 처럼 한
    컨테이너 안에서 actor/role/action 이 갈리는 경우만 ``<relation_id>.actor``
    형태의 접미사를 붙여 좌우를 구분한다. 접미사를 떼면 항상 프로파일의
    실제 id 로 되돌아간다.
    """

    fact_id: str
    field_name: str
    value_raw: str | None
    evidence: list[CplEvidence] = field(default_factory=list)
    # CPL 이 원문에서 파생한 정량 맥락. FIT 은 읽기만 한다 — 여기서 Common IR 을
    # 다시 읽으면 CPL->FIT 책임 경계가 흐려진다. 공개 payload 에는 싣지 않는다.
    quantities: tuple[QuantitySpan, ...] = field(
        default=(), repr=False, metadata={"serialize": False}
    )
    primary_component_id: str | None = None


@dataclass(frozen=True, slots=True)
class FitSide:
    """관계 한쪽의 입력. 어떤 필드에서 왔는지를 근거와 함께 남긴다."""

    field_names: list[str] = field(default_factory=list)
    facts: list[FitEvidenceRef] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class FitRelationResult:
    """관계 하나의 결과. 점수 필드는 없다.

    ``used_*_fact_ids`` 는 모델이 판정 근거로 **실제 인용한** id 다. 입력으로
    준 근거 전체(``left`` · ``right``)와 구분해 둔다. 둘을 섞으면 어떤 근거가
    판정을 지탱했는지가 사라진다.
    """

    relation_id: FitRelationId
    status: FitStatus
    reason_code: str | None
    left: FitSide = field(default_factory=FitSide)
    right: FitSide = field(default_factory=FitSide)
    used_left_fact_ids: list[str] = field(default_factory=list)
    used_right_fact_ids: list[str] = field(default_factory=list)
    diagnostics: list[StageDiagnostic] = field(default_factory=list)
    # 의미 비교가 실제 판정에 사용한 사용자용 한 줄 설명이다. 게이트·Rule처럼
    # 모델 설명이 없는 결과는 None으로 두고 공개 경계에서 관계별 문구를 붙인다.
    summary: str | None = None


@dataclass(frozen=True, slots=True)
class FitResult:
    """7관계 + CPL이 확정한 목적 축 기록 + 계보.

    ``purpose_axis``는 CPL 계약을 그대로 재수출한 단일 타입이다. FIT가 별도
    형식으로 복제하거나 변환하지 않아 CPL 결과 객체가 그대로 전달된다.
    점수·확인율 필드는 의도적으로 없다.
    """

    relations: list[FitRelationResult]
    purpose_axis: PurposeAxisClassification
    profile_id: str | None
    common_ir_document_id: str | None
    model_profile: str
    ruleset_version: str
    prompt_version: str
    diagnostics: list[StageDiagnostic] = field(default_factory=list)
