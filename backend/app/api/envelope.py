"""실패 응답의 본문을 만든다.

성공 응답은 라우트가 반환한 값을 그대로 내보낸다. 공통 래퍼를 두지 않는다.
실패 응답은 화면에 표시할 문구 하나만 담는다.

    {"message": "..."}

문구의 주인은 프론트다. 계약의 핵심은 "어떤 상황에 어떤 상태 코드가 나가는가"이며,
여기의 message 는 프론트가 매핑하지 않은 경우의 폴백이자 Swagger 설명용이다.
내부 오류 코드·스택·필드별 검증 세부는 응답에 담지 않는다.
"""

from typing import Any


ERROR_MESSAGES = {
    400: "요청을 처리할 수 없습니다.",
    401: "인증 정보를 확인해 주세요.",
    403: "접근 권한이 없습니다.",
    404: "요청한 대상을 찾을 수 없습니다.",
    405: "허용되지 않은 요청 방식입니다.",
    409: "현재 상태에서는 처리할 수 없습니다.",
    413: "요청 크기가 허용 범위를 넘었습니다.",
    415: "지원하지 않는 파일 형식입니다.",
    422: "입력값을 확인해 주세요.",
    429: "요청이 너무 많습니다. 잠시 후 다시 시도해 주세요.",
    500: "서버에서 요청을 처리하지 못했습니다.",
    502: "외부 서비스가 올바르지 않은 응답을 보냈습니다.",
    503: "서비스를 일시적으로 사용할 수 없습니다.",
}


ERROR_SCHEMA_NAME = "ErrorMessage"
ERROR_SCHEMA = {
    "type": "object",
    "title": ERROR_SCHEMA_NAME,
    "required": ["message"],
    "properties": {
        "message": {"type": "string", "description": "사람이 읽는 안내"},
    },
}


def error_body(status_code: int, detail: Any) -> dict[str, str]:
    """예외의 detail 을 화면 문구 하나로 옮긴다.

    detail 은 문자열·배열·객체 세 형태로 쓰인다. 객체는 이미 사용자에게 보일
    문구를 담고 있으므로 그 문구를 쓰고, 그 외에는 상태 코드별 기본 문구를 쓴다.
    422 검증 오류의 필드별 정보는 입력 원문이 섞일 수 있어 노출하지 않는다.
    """
    message = ERROR_MESSAGES.get(status_code, ERROR_MESSAGES[500])

    if isinstance(detail, dict) and detail.get("message"):
        message = str(detail["message"])

    return {"message": message}
