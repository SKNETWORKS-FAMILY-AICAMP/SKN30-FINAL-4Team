from collections.abc import Awaitable, Callable
from typing import Any

from fastapi.responses import JSONResponse

from app.api.envelope import error_body


class RequestBodyTooLarge(Exception):
    pass


class RequestBodyLimitMiddleware:
    def __init__(self, app: Callable[..., Awaitable[None]], max_bytes: int) -> None:
        self._app = app
        self._max_bytes = max_bytes

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        if (
            scope.get("type") != "http"
            or scope.get("method") != "POST"
            or str(scope.get("path", "")).rstrip("/") != "/api/v1/cases"
        ):
            await self._app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        content_length = headers.get(b"content-length")
        if content_length is not None:
            try:
                if int(content_length) > self._max_bytes:
                    await self._reject(scope, receive, send)
                    return
            except ValueError:
                pass

        received_bytes = 0

        async def limited_receive() -> dict[str, Any]:
            nonlocal received_bytes
            message = await receive()
            if message.get("type") == "http.request":
                received_bytes += len(message.get("body", b""))
                if received_bytes > self._max_bytes:
                    raise RequestBodyTooLarge
            return message

        try:
            await self._app(scope, limited_receive, send)
        except RequestBodyTooLarge:
            await self._reject(scope, receive, send)

    @staticmethod
    async def _reject(
        scope: dict[str, Any],
        receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        # 미들웨어는 예외 핸들러를 타지 않으므로 공통 형식을 직접 만든다.
        response = JSONResponse(
            status_code=413,
            content=error_body(413, "Request body exceeds the upload limit"),
        )
        await response(scope, receive, send)
