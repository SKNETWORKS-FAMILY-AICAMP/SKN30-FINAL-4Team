"""프로파일 구조화 산출물의 버전 고정 스냅샷 (초안 §5.1, §9.5).

실패는 값을 지어내서 덮지 않는다. reason code 로 남기고 profile 은 None 이다.
없는 reason code 는 ``None`` 이지 문자열 ``"null"`` 이 아니다 (초안 §9.5).
"""

from dataclasses import dataclass, field
from typing import Any, Literal


# 초안 §9.5 최소 집합. 문자열 상수로 두는 이유는 DB·로그·API 에 그대로 실려
# 나가기 때문이다. ponytail: StrEnum 대신 상수, 값 자체가 계약이다.
PARSE_FAILED = "PARSE_FAILED"
COMMON_IR_INVALID = "COMMON_IR_INVALID"
CANDIDATE_PACK_EMPTY = "CANDIDATE_PACK_EMPTY"
LLM_UNAVAILABLE = "LLM_UNAVAILABLE"
LLM_TIMEOUT = "LLM_TIMEOUT"
LLM_INVALID_RESPONSE = "LLM_INVALID_RESPONSE"
MATERIALIZATION_FAILED = "MATERIALIZATION_FAILED"
# 재료화가 실패했지만 어긋난 묶음만 덜어내고 나머지를 살린 경우.
# 성공(OK)이되 완전하지 않다는 사실이 이 코드로만 남는다.
PARTIAL_MATERIALIZATION = "PARTIAL_MATERIALIZATION"
REPAIR_BUDGET_EXHAUSTED = "REPAIR_BUDGET_EXHAUSTED"
REQUEST_TYPE_CONTAINER_MISSING = "REQUEST_TYPE_CONTAINER_MISSING"
REQUEST_TYPE_CONTAINER_AMBIGUOUS = "REQUEST_TYPE_CONTAINER_AMBIGUOUS"
REQUEST_TYPE_SELECTION_INVALID = "REQUEST_TYPE_SELECTION_INVALID"

REASON_CODES = frozenset(
    {
        PARSE_FAILED,
        COMMON_IR_INVALID,
        CANDIDATE_PACK_EMPTY,
        LLM_UNAVAILABLE,
        LLM_TIMEOUT,
        LLM_INVALID_RESPONSE,
        MATERIALIZATION_FAILED,
        REPAIR_BUDGET_EXHAUSTED,
        REQUEST_TYPE_CONTAINER_MISSING,
        REQUEST_TYPE_CONTAINER_AMBIGUOUS,
        REQUEST_TYPE_SELECTION_INVALID,
    }
)


@dataclass(frozen=True, slots=True)
class StageDiagnostic:
    """어느 단계의 무엇이 왜 실패했는지 한 줄."""

    stage: str
    unit: str | None
    reason_code: str | None
    message: str
    attempt: int | None = None
    terminated_because: str | None = None


@dataclass(frozen=True, slots=True)
class CommonIrArtifact:
    """rhwp → Common IR v1 파싱 한 번의 결과와 계보."""

    run_dir: str
    notice_id: str
    source_kind: str
    source_path: str
    source_sha256: str
    common_ir_path: str
    common_ir_document_id: str
    manifest: dict[str, Any]
    block_count: int
    validation_errors: list[str] = field(default_factory=list)
    document: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ProfileSnapshot:
    """구조화 프로파일 한 건의 상태·계보·비용."""

    profile_id: str
    status: Literal["OK", "FAILED"]
    profile: dict[str, Any] | None
    candidate_pack_id: str | None
    common_ir: CommonIrArtifact | None
    model_id: str
    prompt_version: str
    profile_contract_version: str | None
    selection_attempts: int
    usage: list[dict[str, Any]] = field(default_factory=list)
    diagnostics: list[StageDiagnostic] = field(default_factory=list)
