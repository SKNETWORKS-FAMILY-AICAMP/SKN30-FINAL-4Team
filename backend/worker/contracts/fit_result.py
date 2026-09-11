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

from .cpl_result import CplEvidence, PURPOSE_AXIS_UNRESOLVED
from .profile_snapshot import (
    LLM_INVALID_RESPONSE,
    LLM_TIMEOUT,
    LLM_UNAVAILABLE,
    StageDiagnostic,
)

__all__ = [
    "FitRelationId",
    "FitStatus",
    "CplEvidence",
    "StageDiagnostic",
    "PurposeAxisCode",
    "PURPOSE_AXIS_CODES",
    "FIT_NOT_APPLICABLE",
    "FIT_DISPLAY_STATUSES",
    "fit_axis_code",
    "COMPARISON_EVIDENCE_MISSING",
    "COMPARISON_VALUE_INVALID",
    "EVIDENCE_REF_UNRESOLVED",
    "HIERARCHY_COMPARISON_NOT_AVAILABLE",
    "LLM_INVALID_RESPONSE",
    "LLM_TIMEOUT",
    "LLM_UNAVAILABLE",
    "NO_CONDITIONS_SPECIFIED",
    "NUMERIC_MISMATCH",
    "PURPOSE_AXIS_UNRESOLVED",
    "SELF_COMPARISON",
    "SINGLE_SIDED_NO_CONFLICT",
    "FIT_REASON_CODES",
    "FitEvidenceRef",
    "FitSide",
    "FitRelationResult",
    "PurposeAxisAssignment",
    "PurposeAxisClassification",
    "FitResult",
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


class PurposeAxisCode(StrEnum):
    """목적 의미 축 어휘 (초안 §7.1 FIT-2 "목적 의미 분류").

    CPL 의 ``CplAxisCode`` 를 가져다 쓰지 않는다. 저쪽은 13항목 표시 축이고
    이쪽은 FIT 좌측 입력을 만들기 위한 목적 문장 분류 축이라, 어휘가 우연히
    겹쳐도 같은 계약이 아니다. 한쪽을 고치면 다른 쪽이 조용히 바뀌는 결합을
    만들지 않는다.
    """

    TARGET_CONDITION = "PURPOSE_TARGET_CONDITION"
    DIRECTION = "PURPOSE_DIRECTION"
    PROBLEM_DOMAIN = "PURPOSE_PROBLEM_DOMAIN"
    SPECIFIC_OBJECTIVE = "PURPOSE_SPECIFIC_OBJECTIVE"


PURPOSE_AXIS_CODES = frozenset(code.value for code in PurposeAxisCode)


# ------------------------------------------------------------- 표시 어휘

# 프론트 계약은 FIT 상태를 다섯 개로 받는다. ``app.schemas.fit.FitStatus`` 에는
# 넷뿐이고 그 enum 은 옛 FastAPI 표면이 함께 쓰고 있어 건드리지 않는다.
# 다섯째 값은 출력 경계에서만 존재한다.
#
# 뜻은 "비교축 자체가 이 문서에 적용되지 않음" 이다. 근거를 못 구한
# ``INSUFFICIENT`` 와 다르다. FIT-4 는 계층 비교 기준 표본을 확보하기 전까지
# 항상 ``INSUFFICIENT / HIERARCHY_COMPARISON_NOT_AVAILABLE`` (AGENTS.md) 인데
# 그것은 기준 미확보이지 미적용이 아니므로 여기로 옮기지 않는다. 그래서 지금
# 이 값을 만들어내는 판정 경로는 없다 — 어휘만 열어 둔다.
FIT_NOT_APPLICABLE = "NOT_APPLICABLE"

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
# FIT-4 정책 게이트. 계층 노드가 있다는 이유만으로 열리지 않는다 (초안 §7.1).
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


@dataclass(frozen=True, slots=True)
class PurposeAxisAssignment:
    """목적 fact 하나에 붙은 의미 축. 값·오프셋·근거를 새로 만들지 않는다."""

    fact_id: str
    axis_code: str
    quoted_text: str


@dataclass(frozen=True, slots=True)
class PurposeAxisClassification:
    """목적 의미 축 보완 1회의 기록 (초안 §9.2 "의미 분류 미완료").

    ``attempted`` 는 호출 여부, ``reason_code`` 는 호출이 실패했거나 유효한
    축을 하나도 만들지 못한 사유다. 문서당 한 번이며 같은 입력으로 재시도하지
    않는다 (초안 §9.2 "같은 입력·근거·진단으로 진전이 없으면 종료한다").
    """

    attempted: bool
    assignments: list[PurposeAxisAssignment] = field(default_factory=list)
    reason_code: str | None = None
    dropped: list[str] = field(default_factory=list)
    # 관계 비교와 다른 프롬프트다. 호출하지 않았으면 None 이다.
    prompt_version: str | None = None


@dataclass(frozen=True, slots=True)
class FitResult:
    """7관계 + 목적 축 보완 기록 + 계보. 점수·확인율 필드는 의도적으로 없다."""

    relations: list[FitRelationResult]
    purpose_axis: PurposeAxisClassification
    profile_id: str | None
    common_ir_document_id: str | None
    model_profile: str
    ruleset_version: str
    prompt_version: str
    diagnostics: list[StageDiagnostic] = field(default_factory=list)
