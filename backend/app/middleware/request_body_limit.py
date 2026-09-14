"""Bound the complete HTTP request body before multipart parsing."""

from __future__ import annotations

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class RequestBodyLimitMiddleware:
    """Reject oversized bodies for both Content-Length and streamed requests.

    The upload endpoint also limits the extracted file bytes.  This outer ASGI
    boundary is intentionally separate: multipart headers and extra parts are
    counted too, so Starlette cannot spool an unbounded request before the
    endpoint's file-byte check runs.
    """

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        if scope.get("method") in {"GET", "HEAD", "OPTIONS"}:
            await self._app(scope, receive, send)
            return

        application = scope.get("app")
        state = getattr(application, "state", None)
        maximum = int(getattr(state, "request_body_max_bytes", 0) or 0)
        if maximum <= 0:
            await _error_response(
                503,
                "SERVICE_UNAVAILABLE",
                "Request body limit is not configured correctly",
            )(scope, receive, send)
            return

        declared_length = _content_length(scope)
        if declared_length is not None and declared_length > maximum:
            await _error_response(
                413,
                "FILE_TOO_LARGE",
                f"Request body exceeds the {maximum}-byte limit",
            )(scope, receive, send)
            return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > maximum:
                    # ExceptionMiddleware sits inside user middleware and has
                    # a catch-all Exception handler.  This private sentinel
                    # deliberately bypasses it so this outer boundary, which
                    # owns the byte count, can emit the intended 413.
                    raise _RequestBodyTooLarge
            return message

        try:
            await self._app(scope, limited_receive, send)
        except _RequestBodyTooLarge:
            await _error_response(
                413,
                "FILE_TOO_LARGE",
                f"Request body exceeds the {maximum}-byte limit",
            )(scope, receive, send)


def _content_length(scope: Scope) -> int | None:
    for raw_name, raw_value in scope.get("headers", []):
        if raw_name.lower() != b"content-length":
            continue
        try:
            value = int(raw_value)
        except (TypeError, ValueError):
            return None
        return value if value >= 0 else None
    return None


class _RequestBodyTooLarge(BaseException):
    """Internal control flow that cannot be swallowed by app handlers."""


def _error_response(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"code": code, "message": message},
    )


__all__ = ["RequestBodyLimitMiddleware"]
