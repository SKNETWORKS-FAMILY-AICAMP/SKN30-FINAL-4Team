"""실패 예외를 화면에 그대로 보여도 되는 코드·문구 한 쌍으로 바꾼다.

프론트 명세 RUN-04: "분석 워커 내부 상세 오류는 운영 로그에 남기며, 화면에는
안전한 사용자용 ``error_message`` 만 표시한다."

그래서 규칙이 하나다. **예외에서 나온 문자열은 문구가 되지 못한다.** 문구는
아래 표에서만 고른다. 예외 원문은 ``analysis_run.last_error`` 로 따로 간다
(운영용, 화면에 나가지 않는다).

이유는 구체적이다. 실패 메시지에는 요청서 원문 조각(LLM 응답, 파서 오류의
셀 내용), 저장소 경로, 드라이버 예외의 접속 문자열이 섞여 들어온다. 셋 다
사용자 화면에 있으면 안 되는 것이고, "이번 예외는 안전한가" 를 매번 판단하는
방식은 언젠가 틀린다. 그래서 **화이트리스트가 아니면 기본 문구**다.

코드는 운영 집계를 위해 단계별로 구분하고, 문구는 사용자가 취할 수 있는
행동이 같으면 하나로 합친다 — 파일을 고쳐야 하는가, 기다리면 되는가, 둘 다
아닌가. 세 갈래면 충분하다.
"""

from __future__ import annotations

__all__ = ["DEFAULT_ERROR_CODE", "USER_ERROR_MESSAGES", "user_outcome"]


# 사용자가 파일을 고쳐야 하는 실패.
_CHECK_FILE = "요청서에서 분석에 필요한 내용을 읽지 못했습니다. 파일을 확인해 주세요."
# 기다렸다 다시 하면 되는 실패.
_RETRY_LATER = "분석 서비스를 일시적으로 사용할 수 없습니다. 잠시 후 다시 시도해 주세요."
# 그 외 전부.
_GENERIC = "분석에 실패했습니다. 잠시 후 다시 시도해 주세요."

DEFAULT_ERROR_CODE = "ANALYSIS_FAILED"

# 키는 worker.profiles / worker.analysis 가 진단에 다는 reason code 다.
# 여기 없는 코드는 기본값으로 떨어진다 — 새 코드가 생겨도 원문이 새지 않는다.
USER_ERROR_MESSAGES: dict[str, str] = {
    "PARSE_FAILED": _CHECK_FILE,
    "COMMON_IR_INVALID": _CHECK_FILE,
    "CANDIDATE_PACK_EMPTY": _CHECK_FILE,
    "REQUEST_TYPE_CONTAINER_MISSING": _CHECK_FILE,
    "REQUEST_TYPE_CONTAINER_AMBIGUOUS": _CHECK_FILE,
    "PROFILE_FAILED": _CHECK_FILE,
    "MATERIALIZATION_FAILED": _GENERIC,
    "REPAIR_BUDGET_EXHAUSTED": _GENERIC,
    "REQUEST_TYPE_SELECTION_INVALID": _GENERIC,
    "LLM_INVALID_RESPONSE": _RETRY_LATER,
    "LLM_UNAVAILABLE": _RETRY_LATER,
    DEFAULT_ERROR_CODE: _GENERIC,
}


def user_outcome(error: BaseException) -> tuple[str, str]:
    """``(error_code, error_message)``. 문구는 항상 표에서 나온다."""
    reason = getattr(getattr(error, "diagnostic", None), "reason_code", None)
    if isinstance(reason, str) and reason in USER_ERROR_MESSAGES:
        return reason, USER_ERROR_MESSAGES[reason]
    return DEFAULT_ERROR_CODE, USER_ERROR_MESSAGES[DEFAULT_ERROR_CODE]
