"""Opaque, signed pagination cursors shared by history/message list endpoints.

A cursor is never a capability. Every repository call re-applies the owner
predicate itself; the signature here only stops a caller from *constructing*
or *mutating* a cursor to reach another owner's rows, reuse a cursor minted
for a different endpoint or filter scope, or replay one against an
incompatible payload version (v0.2 spec section 6):

    "cursor는 HMAC으로 서명하고 endpoint·owner·filter에 바인딩한다. 최대
    길이와 exact key/type/version을 엄격히 검사하며 오류는 422다."

Both callers key the ``scope`` string in the same shape they will later
enforce in SQL: the analysis-history cursor scope is just ``owner_id`` (no
extra filter); the chat message-history cursor scope is
``f"{owner_id}:{analysis_case_id}"`` so a cursor minted for one case can never
page through another case's messages even for the same owner.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any

from app.api.errors import service_unavailable, validation_error


MAX_CURSOR_LENGTH = 2_000
_SIGNATURE_BYTES = 16  # truncated HMAC-SHA256; anti-tamper, not secrecy


def _mac(secret: str, *, endpoint: str, scope: str, body: bytes) -> bytes:
    key = secret.encode("utf-8")
    message = b"|".join((endpoint.encode("utf-8"), scope.encode("utf-8"), body))
    return hmac.new(key, message, hashlib.sha256).digest()[:_SIGNATURE_BYTES]


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(segment: str) -> bytes:
    if not segment or not segment.isascii():
        raise ValueError("cursor segment is not valid base64url")
    padding = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + padding)


def encode_cursor(
    *,
    secret: str,
    endpoint: str,
    scope: str,
    version: int,
    fields: dict[str, Any],
) -> str:
    if not secret:
        raise service_unavailable("Pagination is not configured")
    body = json.dumps({"v": version, **fields}, separators=(",", ":"), sort_keys=True).encode("utf-8")
    signature = _mac(secret, endpoint=endpoint, scope=scope, body=body)
    return f"{_b64encode(body)}.{_b64encode(signature)}"


def decode_cursor(
    token: str,
    *,
    secret: str,
    endpoint: str,
    scope: str,
    version: int,
    required_keys: frozenset[str],
) -> dict[str, Any]:
    """Validate and return the cursor payload fields (without ``v``).

    Every failure mode — wrong length, wrong signature, wrong endpoint/owner
    binding, wrong version, unexpected/missing keys — raises the same 422
    ``VALIDATION_ERROR``. A cursor is not an authorization mechanism, so a
    forged or stale one must look exactly like "bad input", never like a
    permission boundary an attacker could probe.
    """

    if not secret:
        raise service_unavailable("Pagination is not configured")
    if not token or len(token) > MAX_CURSOR_LENGTH:
        raise validation_error("cursor is invalid")
    body_part, separator, signature_part = token.partition(".")
    if not separator or not signature_part:
        raise validation_error("cursor is invalid")
    try:
        body = _b64decode(body_part)
        signature = _b64decode(signature_part)
    except (ValueError, UnicodeDecodeError) as exc:
        raise validation_error("cursor is invalid") from exc
    expected = _mac(secret, endpoint=endpoint, scope=scope, body=body)
    if len(signature) != len(expected) or not hmac.compare_digest(signature, expected):
        raise validation_error("cursor is invalid")
    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise validation_error("cursor is invalid") from exc
    if not isinstance(payload, dict) or payload.get("v") != version:
        raise validation_error("cursor is invalid")
    fields = {key: value for key, value in payload.items() if key != "v"}
    if set(fields) != set(required_keys):
        raise validation_error("cursor is invalid")
    return fields


__all__ = ["MAX_CURSOR_LENGTH", "decode_cursor", "encode_cursor"]
