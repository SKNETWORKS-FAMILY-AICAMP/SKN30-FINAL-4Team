"""업무 API 의 JSON 응답을 공통 4필드로 감싼다.

    {"code": ..., "message": ..., "data": ..., "errors": []}

성공 응답은 EnvelopeRoute 가 감싸고, 실패 응답은 main.py 의 예외 핸들러가
같은 모양으로 만든다. HTTP 상태는 상태줄에 있으므로 본문에 넣지 않는다.

PDF 다운로드는 JSONResponse 가 아니므로 그대로 통과하고, 헬스체크는 이
라우트 클래스를 쓰지 않는 라우터에 있어 대상이 아니다.
"""

import json
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute


SUCCESS_CODE = "SUCCESS"
ACCEPTED_CODE = "ACCEPTED"

# 상태코드만으로 정해지는 오류 코드. 의미가 같으면 어느 경로에서나 같은 값이다.
ERROR_CODES = {
    400: "BAD_REQUEST",
    401: "UNAUTHORIZED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    409: "CONFLICT",
    413: "PAYLOAD_TOO_LARGE",
    415: "UNSUPPORTED_MEDIA_TYPE",
    422: "VALIDATION_ERROR",
    429: "TOO_MANY_REQUESTS",
    500: "INTERNAL_ERROR",
    502: "BAD_GATEWAY",
    503: "SERVICE_UNAVAILABLE",
}

SUCCESS_MESSAGES = {
    200: "요청이 처리되었습니다.",
    202: "요청이 접수되었습니다.",
}

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


def envelope(
    code: str,
    message: str,
    data: Any = None,
    errors: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {"code": code, "message": message, "data": data, "errors": errors or []}


def error_body(status_code: int, detail: Any) -> dict[str, Any]:
    """예외의 detail 을 공통 4필드로 옮긴다.

    detail 은 지금까지 문자열·배열·객체 세 형태로 쓰였다. 여기서 하나로 만든다.
    """
    code = ERROR_CODES.get(status_code, "INTERNAL_ERROR")
    message = ERROR_MESSAGES.get(status_code, ERROR_MESSAGES[500])
    errors: list[dict[str, Any]] = []

    if isinstance(detail, dict):
        # 챗의 LLM 실패처럼 원인을 이미 구분해 둔 경우 그 코드를 보존한다.
        code = str(detail.get("code") or code)
        message = str(detail.get("message") or message)
    elif isinstance(detail, list):
        # 422 검증 오류. 입력 원문은 담지 않는다.
        errors = [
            {key: item[key] for key in ("loc", "msg", "type") if key in item}
            for item in detail
            if isinstance(item, dict)
        ]

    return envelope(code, message, None, errors)


def _success_body(status_code: int, payload: Any) -> dict[str, Any]:
    code = ACCEPTED_CODE if status_code == 202 else SUCCESS_CODE
    message = SUCCESS_MESSAGES.get(status_code, SUCCESS_MESSAGES[200])
    return envelope(code, message, payload)


ERROR_SCHEMA_NAME = "ErrorEnvelope"
_ERROR_SCHEMA = {
    "type": "object",
    "title": ERROR_SCHEMA_NAME,
    "required": ["code", "message", "data", "errors"],
    "properties": {
        "code": {"type": "string", "description": "실패 원인 코드"},
        "message": {"type": "string", "description": "사람이 읽는 안내"},
        "data": {"type": "null"},
        "errors": {
            "type": "array",
            "description": "검증 오류의 필드별 정보. 그 외에는 빈 배열",
            "items": {
                "type": "object",
                "properties": {
                    "loc": {"type": "array", "items": {}},
                    "msg": {"type": "string"},
                    "type": {"type": "string"},
                },
            },
        },
    },
}


def _wrap_schema(inner: dict[str, Any], code: str) -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["code", "message", "data", "errors"],
        "properties": {
            "code": {"type": "string", "default": code},
            "message": {"type": "string"},
            "data": inner,
            "errors": {"type": "array", "items": {}, "default": []},
        },
    }


def enveloped_operations(app: Any) -> set[tuple[str, str]]:
    """EnvelopeRoute 로 등록된 (경로, 메서드) 를 모은다."""
    found: set[tuple[str, str]] = set()

    def walk(routes: Any) -> None:
        for route in routes:
            included = getattr(route, "original_router", None)
            if included is not None:
                walk(included.routes)
            elif isinstance(route, EnvelopeRoute):
                for method in route.methods or ():
                    found.add((route.path, method.lower()))

    walk(app.routes)
    return found


def apply_envelope_to_openapi(app: Any) -> None:
    """OpenAPI 문서도 실제 응답과 같은 4필드를 말하게 만든다.

    응답을 라우터 바깥에서 감싸므로 FastAPI 가 생성한 스키마는 감싸기 전
    모양을 가리킨다. 그대로 두면 /docs 를 보고 구현한 클라이언트가 전부
    어긋나므로 생성 결과를 한 번 손본다.
    """
    original = app.openapi

    def openapi() -> dict[str, Any]:
        schema = original()
        if schema.get("_enveloped"):
            return schema
        targets = enveloped_operations(app)
        for path, operations in schema.get("paths", {}).items():
            for method, operation in operations.items():
                if (path, method) not in targets:
                    continue
                for status_code, response in operation.get("responses", {}).items():
                    content = (response.get("content") or {}).get("application/json")
                    if content is None:
                        continue
                    if str(status_code).startswith(("4", "5")):
                        content["schema"] = {
                            "$ref": "#/components/schemas/" + ERROR_SCHEMA_NAME
                        }
                        response["description"] = "공통 4필드 오류 응답"
                    else:
                        code = ACCEPTED_CODE if status_code == "202" else SUCCESS_CODE
                        content["schema"] = _wrap_schema(content["schema"], code)
        schema.setdefault("components", {}).setdefault("schemas", {})[
            ERROR_SCHEMA_NAME
        ] = _ERROR_SCHEMA
        schema["_enveloped"] = True
        app.openapi_schema = schema
        return schema

    app.openapi = openapi


class EnvelopeRoute(APIRoute):
    """성공 JSON 응답을 공통 4필드로 감싸는 라우트."""

    def get_route_handler(self) -> Callable[[Request], Awaitable[Response]]:
        original = super().get_route_handler()

        async def wrapped(request: Request) -> Response:
            response = await original(request)
            return _wrap(response)

        return wrapped


def _is_json(response: Response) -> bool:
    # FastAPI 는 직렬화된 응답을 JSONResponse 가 아닌 Response 로 돌려줄 수 있다.
    # 클래스 대신 Content-Type 으로 판별해야 PDF 같은 바이너리를 안전히 거른다.
    return "application/json" in response.headers.get("content-type", "")


def _wrap(response: Response) -> Response:
    if response.status_code == 204:
        # 본문 없는 성공도 같은 모양을 갖도록 200 으로 올린다.
        payload, status_code = None, 200
    elif _is_json(response):
        status_code = response.status_code
        payload = json.loads(response.body) if response.body else None
    else:
        # PDF 처럼 JSON 이 아닌 응답은 손대지 않는다.
        return response

    if status_code >= 400:
        return response

    # 이미 공통 형식으로 만들어 보낸 응답은 다시 감싸지 않는다.
    if isinstance(payload, dict) and set(payload) == {
        "code",
        "message",
        "data",
        "errors",
    }:
        return response

    headers = {
        key: value
        for key, value in response.headers.items()
        if key.lower() not in ("content-length", "content-type")
    }
    return JSONResponse(
        status_code=status_code,
        content=_success_body(status_code, payload),
        headers=headers,
    )
