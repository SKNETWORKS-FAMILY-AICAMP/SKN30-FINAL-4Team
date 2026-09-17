"""Trusted-side issuer for narrowly scoped Supabase Storage capabilities.

Only the EC2/laptop worker process may construct this object because it owns
the Supabase service-role key.  The remote PDF accelerator receives the
resulting :class:`~worker.contracts.accelerator.SignedStorageCapability`, never
the service-role key or a general Storage API endpoint.

The issuer deliberately has a small surface: a verified immutable input gets
a signed ``GET`` capability, and a deterministic, absent result key gets a
create-only signed ``PUT`` capability.  It is not a general Storage client.
"""

from __future__ import annotations

from base64 import urlsafe_b64decode
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
import ipaddress
import json
import math
import re
import time
from typing import Any, Literal
from urllib.parse import parse_qsl, quote, urlsplit, urlunsplit

import httpx

from worker.contracts.accelerator import (
    SignedStorageCapability,
    StorageResourceCaps,
    build_surya_layout_result_object_key as _contract_result_object_key,
)

__all__ = [
    "SignedStorageCapabilityIssuerError",
    "SupabaseSignedStorageCapabilityIssuer",
    "build_surya_layout_result_object_key",
]


_MAX_RESPONSE_BYTES = 64 * 1024
_MAX_SIGNED_URL_CHARS = 8_192
_MAX_OBJECT_KEY_CHARS = 1_024
_SAFE_SEGMENT = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,127})?")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_TRANSIENT_GATEWAY_STATUSES = frozenset({502, 503, 504})
_REQUEST_ATTEMPTS = 3
_REQUEST_RETRY_DELAY_SECONDS = 0.25


@dataclass(frozen=True, slots=True)
class _IssuerHttpResponse:
    status_code: int
    content_type: str | None
    body: bytes


class SignedStorageCapabilityIssuerError(RuntimeError):
    """A redacted trusted-storage issuance failure.

    The message is intentionally fixed: provider response bodies can contain
    signed URLs and must never cross this boundary as diagnostics.
    """

    def __init__(self) -> None:
        super().__init__("signed storage capability issuance failed")


def build_surya_layout_result_object_key(logical_compute_key: str) -> str:
    """Return the one deterministic immutable output key for a computation.

    The caller must still prove the key is absent immediately before it signs
    the upload.  A retry of the same immutable compute identity therefore
    cannot silently choose a new lineage location.
    """

    return _contract_result_object_key(logical_compute_key)


class SupabaseSignedStorageCapabilityIssuer:
    """Issue exact signed Storage capabilities from a trusted worker only.

    ``supabase_url`` is the internal gateway origin used with the service-role
    key.  ``public_storage_origin`` is a separately configured canonical
    HTTPS/Tailscale origin that the RunPod worker can reach.  Rewriting the
    authority is intentional; the Storage path and signed query are otherwise
    preserved verbatim after strict binding checks.
    """

    def __init__(
        self,
        *,
        supabase_url: str,
        service_role_key: str,
        public_storage_origin: str,
        max_capability_ttl_seconds: int,
        timeout_seconds: float = 30.0,
        max_response_bytes: int = _MAX_RESPONSE_BYTES,
        transport: httpx.BaseTransport | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._supabase_url = _canonical_api_origin(supabase_url)
        self._public_origin = _canonical_public_storage_origin(public_storage_origin)
        if (
            not isinstance(service_role_key, str)
            or not service_role_key
            or service_role_key != service_role_key.strip()
            or len(service_role_key) > 16_384
            or any(ord(char) < 0x20 or ord(char) == 0x7F for char in service_role_key)
        ):
            raise ValueError("Supabase service-role key is invalid")
        if (
            isinstance(max_capability_ttl_seconds, bool)
            or not isinstance(max_capability_ttl_seconds, int)
            or not 1 <= max_capability_ttl_seconds <= 86_400
        ):
            raise ValueError("capability TTL ceiling is invalid")
        if not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool) or not math.isfinite(float(timeout_seconds)) or timeout_seconds <= 0:
            raise ValueError("storage signing timeout is invalid")
        if (
            isinstance(max_response_bytes, bool)
            or not isinstance(max_response_bytes, int)
            or not 1 <= max_response_bytes <= _MAX_RESPONSE_BYTES
        ):
            raise ValueError("storage signing response limit is invalid")

        self._service_role_key = service_role_key
        self._max_capability_ttl_seconds = max_capability_ttl_seconds
        self._timeout = httpx.Timeout(float(timeout_seconds))
        self._max_response_bytes = max_response_bytes
        self._transport = transport
        self._now = now or (lambda: datetime.now(UTC))

    def __repr__(self) -> str:
        # In particular, do not include the service-role key.
        return (
            "SupabaseSignedStorageCapabilityIssuer("
            f"supabase_url={self._supabase_url!r}, "
            f"public_storage_origin={self._public_origin!r})"
        )

    def issue_read(
        self,
        *,
        bucket: str,
        object_key: str,
        expected_sha256: str,
        size_bytes: int,
        mime_type: str,
        ttl_seconds: int,
    ) -> SignedStorageCapability:
        """Create an exact read-only capability for one verified object."""

        bucket, object_key = _validate_storage_location(bucket, object_key)
        if not isinstance(expected_sha256, str) or _SHA256.fullmatch(expected_sha256) is None:
            raise ValueError("expected SHA-256 is invalid")
        _validate_positive_int(size_bytes, "artifact byte size")
        _validate_positive_int(ttl_seconds, "requested capability TTL")
        if ttl_seconds > self._max_capability_ttl_seconds:
            raise ValueError("requested capability TTL exceeds configured ceiling")
        resource_caps = StorageResourceCaps(
            max_bytes=size_bytes,
            allowed_mime_types=(mime_type,),
        )

        relative_url, _ = self._post_for_url(
            f"/storage/v1/object/sign/{_quoted_location(bucket, object_key)}",
            body={"expiresIn": ttl_seconds},
            response_key="signedURL",
        )
        url, path = self._rewrite_and_bind_url(
            relative_url,
            bucket=bucket,
            object_key=object_key,
            operation="read",
        )
        expires_at = self._signed_token_expiry(url)
        self._assert_expiry_within_ceiling(expires_at)
        return SignedStorageCapability(
            method="GET",
            access_mode="read_only",
            url=url,
            storage_host=urlsplit(self._public_origin).hostname or "",
            bucket=bucket,
            path=path,
            object_key=object_key,
            expected_sha256=expected_sha256,
            expires_at=expires_at,
            resource_caps=resource_caps,
        )

    def issue_create_only_upload(
        self,
        *,
        bucket: str,
        object_key: str,
        max_bytes: int,
        mime_type: str,
    ) -> SignedStorageCapability:
        """Sign one immutable result upload after proving its key is absent."""

        bucket, object_key = _validate_storage_location(bucket, object_key)
        _validate_positive_int(max_bytes, "result byte cap")
        resource_caps = StorageResourceCaps(
            max_bytes=max_bytes,
            allowed_mime_types=(mime_type,),
        )
        self._require_absent(bucket=bucket, object_key=object_key)
        relative_url, response_token = self._post_for_url(
            f"/storage/v1/object/upload/sign/{_quoted_location(bucket, object_key)}",
            body={"upsert": False},
            response_key="url",
            require_response_token=True,
        )
        url, path = self._rewrite_and_bind_url(
            relative_url,
            bucket=bucket,
            object_key=object_key,
            operation="upload",
        )
        if response_token != _query_token(url):
            raise SignedStorageCapabilityIssuerError()
        self._assert_create_only_upload_claims(
            response_token,
            bucket=bucket,
            object_key=object_key,
        )
        # Report the provider JWT exp rather than inventing a lifetime from a
        # request timeout. The self-hosted deployment configures this to 1,800
        # seconds, and the dispatch ceiling below fails closed on drift.
        expires_at = self._signed_token_expiry(url)
        self._assert_expiry_within_ceiling(expires_at)
        return SignedStorageCapability(
            method="PUT",
            access_mode="create_only",
            url=url,
            storage_host=urlsplit(self._public_origin).hostname or "",
            bucket=bucket,
            path=path,
            object_key=object_key,
            expected_sha256=None,
            expires_at=expires_at,
            resource_caps=resource_caps,
        )

    def issue_surya_layout_result_upload(
        self,
        *,
        bucket: str,
        logical_compute_key: str,
        max_bytes: int,
        mime_type: Literal["application/json"] = "application/json",
    ) -> SignedStorageCapability:
        """Issue the canonical absent Surya result object capability."""

        return self.issue_create_only_upload(
            bucket=bucket,
            object_key=build_surya_layout_result_object_key(logical_compute_key),
            max_bytes=max_bytes,
            mime_type=mime_type,
        )

    def _require_absent(self, *, bucket: str, object_key: str) -> None:
        response = self._request(
            "GET",
            f"/storage/v1/object/info/{_quoted_location(bucket, object_key)}",
        )
        if response.status_code == 200:
            # A deterministic output may never overwrite an older result.
            raise SignedStorageCapabilityIssuerError()
        if response.status_code != 400:
            raise SignedStorageCapabilityIssuerError()
        payload = self._read_json(response)
        if (
            set(payload) == {"statusCode", "error", "message"}
            and payload.get("statusCode") == "404"
            and payload.get("error") == "not_found"
            and isinstance(payload.get("message"), str)
        ):
            return
        raise SignedStorageCapabilityIssuerError()

    def _post_for_url(
        self,
        path: str,
        *,
        body: Mapping[str, object],
        response_key: str,
        require_response_token: bool = False,
    ) -> tuple[str, str | None]:
        response = self._request("POST", path, json_body=body)
        if response.status_code not in {200, 201}:
            raise SignedStorageCapabilityIssuerError()
        payload = self._read_json(response)
        expected_keys = {response_key, "token"} if require_response_token else {response_key}
        if set(payload) != expected_keys or not isinstance(payload.get(response_key), str):
            raise SignedStorageCapabilityIssuerError()
        url = payload[response_key]
        if not require_response_token:
            return url, None
        token = payload.get("token")
        if not isinstance(token, str) or not token:
            raise SignedStorageCapabilityIssuerError()
        return url, token

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Mapping[str, object] | None = None,
    ) -> _IssuerHttpResponse:
        for attempt in range(_REQUEST_ATTEMPTS):
            try:
                with httpx.Client(
                    transport=self._transport,
                    timeout=self._timeout,
                    follow_redirects=False,
                    trust_env=False,
                ) as client:
                    with client.stream(
                        method,
                        f"{self._supabase_url}{path}",
                        headers={
                            "apikey": self._service_role_key,
                            "Authorization": f"Bearer {self._service_role_key}",
                            "Accept": "application/json",
                        },
                        json=json_body,
                    ) as response:
                        if 300 <= response.status_code <= 399:
                            raise SignedStorageCapabilityIssuerError()
                        if response.status_code not in _TRANSIENT_GATEWAY_STATUSES:
                            return _IssuerHttpResponse(
                                status_code=response.status_code,
                                content_type=response.headers.get("content-type"),
                                body=self._read_bounded_response_body(response),
                            )
            except SignedStorageCapabilityIssuerError:
                raise
            except httpx.HTTPError:
                pass
            if attempt + 1 < _REQUEST_ATTEMPTS:
                time.sleep(_REQUEST_RETRY_DELAY_SECONDS * (attempt + 1))
        raise SignedStorageCapabilityIssuerError()

    def _read_json(self, response: _IssuerHttpResponse) -> Mapping[str, Any]:
        try:
            if not _is_json_media_type(response.content_type):
                raise SignedStorageCapabilityIssuerError()
            decoded = json.loads(
                response.body,
                object_pairs_hook=_unique_json_object,
                parse_constant=_reject_nonfinite_json,
            )
            if not isinstance(decoded, dict):
                raise SignedStorageCapabilityIssuerError()
            return decoded
        except (TypeError, ValueError, UnicodeDecodeError):
            raise SignedStorageCapabilityIssuerError() from None

    def _read_bounded_response_body(self, response: httpx.Response) -> bytes:
        content_length = response.headers.get("content-length")
        if content_length is not None:
            try:
                declared = int(content_length)
            except ValueError:
                raise SignedStorageCapabilityIssuerError() from None
            if declared < 0 or declared > self._max_response_bytes:
                raise SignedStorageCapabilityIssuerError()
        received = bytearray()
        for chunk in response.iter_raw():
            if len(received) + len(chunk) > self._max_response_bytes:
                raise SignedStorageCapabilityIssuerError()
            received.extend(chunk)
        return bytes(received)

    def _rewrite_and_bind_url(
        self,
        returned_url: str,
        *,
        bucket: str,
        object_key: str,
        operation: Literal["read", "upload"],
    ) -> tuple[str, str]:
        if (
            not isinstance(returned_url, str)
            or not returned_url
            or len(returned_url) > _MAX_SIGNED_URL_CHARS
            or not returned_url.isascii()
            or any(char.isspace() or ord(char) < 0x20 or ord(char) == 0x7F for char in returned_url)
            or "\\" in returned_url
        ):
            raise SignedStorageCapabilityIssuerError()
        try:
            parsed = urlsplit(returned_url)
        except ValueError:
            raise SignedStorageCapabilityIssuerError() from None
        if parsed.fragment or not parsed.query or parsed.username or parsed.password:
            raise SignedStorageCapabilityIssuerError()
        if parsed.scheme:
            if _origin_from_split(parsed) != self._supabase_url:
                raise SignedStorageCapabilityIssuerError()
        elif parsed.netloc:
            raise SignedStorageCapabilityIssuerError()

        # Storage's HTTP API commonly returns a service-relative
        # ``/object/...`` path.  The public gateway route is always
        # ``/storage/v1``; the provider's suffix and query are left untouched.
        if parsed.path.startswith("/object/"):
            canonical_path = f"/storage/v1{parsed.path}"
        else:
            canonical_path = parsed.path
        endpoint = "sign" if operation == "read" else "upload/sign"
        expected_path = (
            f"/storage/v1/object/{endpoint}/"
            f"{bucket}/{object_key}"
        )
        if canonical_path != expected_path:
            raise SignedStorageCapabilityIssuerError()
        url = urlunsplit(("https", urlsplit(self._public_origin).netloc, canonical_path, parsed.query, ""))
        return url, canonical_path

    def _signed_token_expiry(self, url: str) -> datetime:
        try:
            payload = _decode_jwt_payload(_query_token(url))
            exp = payload.get("exp")
            if isinstance(exp, bool) or not isinstance(exp, int) or exp <= 0:
                raise ValueError
            expires_at = datetime.fromtimestamp(exp, UTC)
        except (TypeError, ValueError, OverflowError, UnicodeDecodeError):
            raise SignedStorageCapabilityIssuerError() from None
        current = _aware_now(self._now)
        if expires_at <= current:
            raise SignedStorageCapabilityIssuerError()
        return expires_at

    def _assert_create_only_upload_claims(
        self,
        token: str,
        *,
        bucket: str,
        object_key: str,
    ) -> None:
        try:
            payload = _decode_jwt_payload(token)
        except (TypeError, ValueError, UnicodeDecodeError):
            raise SignedStorageCapabilityIssuerError() from None
        if (
            set(payload) != {"url", "upsert", "iat", "exp"}
            or payload.get("url") != f"{bucket}/{object_key}"
            or payload.get("upsert") is not False
            or isinstance(payload.get("iat"), bool)
            or not isinstance(payload.get("iat"), int)
        ):
            raise SignedStorageCapabilityIssuerError()

    def _assert_expiry_within_ceiling(self, expires_at: datetime) -> None:
        current = _aware_now(self._now)
        if (expires_at - current).total_seconds() > self._max_capability_ttl_seconds:
            raise SignedStorageCapabilityIssuerError()


def _canonical_api_origin(value: str) -> str:
    """Validate the trusted internal Supabase gateway origin.

    Service-role credentials may use cleartext only on a literal loopback
    address. Every non-loopback deployment must terminate HTTPS before this
    boundary.
    """

    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("Supabase Storage API origin is invalid")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ValueError("Supabase Storage API origin is invalid") from None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or port is not None and not 1 <= port <= 65_535
    ):
        raise ValueError("Supabase Storage API origin is invalid")
    if parsed.scheme == "http" and not _is_literal_loopback(parsed.hostname):
        raise ValueError("Supabase Storage API origin is invalid")
    host = parsed.hostname if ":" not in parsed.hostname else f"[{parsed.hostname}]"
    authority = host if port is None else f"{host}:{port}"
    canonical = f"{parsed.scheme}://{authority}"
    if value.rstrip("/") != canonical:
        raise ValueError("Supabase Storage API origin is invalid")
    return canonical


def _is_literal_loopback(hostname: str) -> bool:
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _decode_jwt_payload(token: str) -> Mapping[str, Any]:
    pieces = token.split(".")
    if len(pieces) != 3 or any(not piece for piece in pieces):
        raise ValueError("invalid JWT")
    padded = pieces[1] + "=" * (-len(pieces[1]) % 4)
    payload = json.loads(
        urlsafe_b64decode(padded.encode("ascii")),
        object_pairs_hook=_unique_json_object,
        parse_constant=_reject_nonfinite_json,
    )
    if not isinstance(payload, dict):
        raise ValueError("invalid JWT payload")
    return payload


def _canonical_public_storage_origin(value: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("public Storage origin is invalid")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ValueError("public Storage origin is invalid") from None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path
        or parsed.query
        or parsed.fragment
        or port not in {None, 443}
    ):
        raise ValueError("public Storage origin is invalid")
    canonical = f"https://{parsed.hostname}"
    if value != canonical:
        raise ValueError("public Storage origin is invalid")
    return canonical


def _origin_from_split(parsed: Any) -> str:
    try:
        port = parsed.port
    except ValueError:
        return ""
    if not parsed.hostname or parsed.username or parsed.password or port is not None and not 1 <= port <= 65_535:
        return ""
    host = parsed.hostname if ":" not in parsed.hostname else f"[{parsed.hostname}]"
    return f"{parsed.scheme}://{host if port is None else f'{host}:{port}'}"


def _validate_storage_location(bucket: str, object_key: str) -> tuple[str, str]:
    if not isinstance(bucket, str) or _SAFE_SEGMENT.fullmatch(bucket) is None:
        raise ValueError("Storage bucket is invalid")
    if (
        not isinstance(object_key, str)
        or not object_key
        or len(object_key) > _MAX_OBJECT_KEY_CHARS
        or object_key.startswith("/")
        or object_key.endswith("/")
        or "%" in object_key
        or "\\" in object_key
        or any(char.isspace() or ord(char) < 0x20 or ord(char) == 0x7F for char in object_key)
    ):
        raise ValueError("Storage object key is invalid")
    segments = object_key.split("/")
    if any(_SAFE_SEGMENT.fullmatch(segment) is None for segment in segments):
        raise ValueError("Storage object key is invalid")
    return bucket, object_key


def _quoted_location(bucket: str, object_key: str) -> str:
    return f"{quote(bucket, safe='')}/{quote(object_key, safe='/')}"


def _validate_positive_int(value: object, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


def _aware_now(now: Callable[[], datetime]) -> datetime:
    try:
        value = now()
    except Exception:
        raise SignedStorageCapabilityIssuerError() from None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise SignedStorageCapabilityIssuerError()
    return value


def _is_json_media_type(value: str | None) -> bool:
    """Accept JSON plus ordinary parameters (e.g. ``charset=utf-8``)."""

    if not isinstance(value, str) or not value or any(
        char in value for char in "\r\n\x00"
    ):
        return False
    media_type, _, parameters = value.partition(";")
    if media_type.strip().lower() != "application/json":
        return False
    # We need not interpret response charset parameters, but malformed bare
    # delimiters should not turn an arbitrary response into trusted JSON.
    return not parameters or all(
        "=" in parameter and parameter.strip()
        for parameter in parameters.split(";")
    )


def _query_token(url: str) -> str:
    try:
        query = parse_qsl(urlsplit(url).query, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        raise SignedStorageCapabilityIssuerError() from None
    tokens = [value for key, value in query if key == "token"]
    if len(tokens) != 1 or not tokens[0]:
        raise SignedStorageCapabilityIssuerError()
    return tokens[0]


def _reject_nonfinite_json(_: str) -> object:
    raise ValueError("non-finite JSON")


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value
