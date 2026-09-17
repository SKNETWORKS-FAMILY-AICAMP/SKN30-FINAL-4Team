"""Trusted Supabase Storage reader for accepted accelerator output artifacts.

This is deliberately narrower than the worker's general Storage client.  It
is used only after the coordinator has bound a remote result manifest to the
one requested output capability.  The service-role credential therefore never
leaves the trusted worker, and HTTP/provider diagnostics cannot leak through
the coordinator's retry path.
"""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import json
import math
import re
from urllib.parse import quote, urlsplit

import httpx

from common_ir_pipeline.pdf_fusion.surya_layout_artifact import MAX_ARTIFACT_BYTES
from worker.accelerator_coordinator import (
    AcceleratorArtifactReader,
    ArtifactNotFound,
    ArtifactRead,
    ArtifactReadLimitExceeded,
)

__all__ = [
    "SupabaseAcceleratorArtifactReadError",
    "SupabaseAcceleratorArtifactReader",
]


_MAX_OBJECT_KEY_CHARS = 1_024
_SAFE_SEGMENT = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,127})?")
_DECIMAL = re.compile(r"[0-9]+")
_MAX_ERROR_RESPONSE_BYTES = 8 * 1024


class SupabaseAcceleratorArtifactReadError(RuntimeError):
    """A fixed, redacted trusted-artifact retrieval error."""

    def __init__(self) -> None:
        # Do not include provider bodies, object keys, URLs, or credentials.
        super().__init__("accelerator artifact retrieval failed")


@dataclass(frozen=True, slots=True)
class _StorageLocation:
    bucket: str
    object_key: str


class SupabaseAcceleratorArtifactReader(AcceleratorArtifactReader):
    """Read one canonical private Storage object with a hard byte ceiling.

    The configured ``supabase_url`` is a trusted local/gateway origin, not a
    caller-supplied URL.  The reader follows no redirects and ignores ambient
    proxy settings.  It reports the response ``Content-Type`` exactly as
    Storage supplied it; semantic media-type acceptance remains the
    coordinator's responsibility.
    """

    def __init__(
        self,
        *,
        supabase_url: str,
        service_role_key: str,
        timeout_seconds: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._supabase_url = _canonical_supabase_origin(supabase_url)
        if (
            not isinstance(service_role_key, str)
            or not service_role_key
            or service_role_key != service_role_key.strip()
            or len(service_role_key) > 16_384
            or any(
                ord(character) < 0x20 or ord(character) == 0x7F
                for character in service_role_key
            )
        ):
            raise ValueError("Supabase service-role key is invalid")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(float(timeout_seconds))
            or timeout_seconds <= 0
        ):
            raise ValueError("accelerator artifact timeout is invalid")
        self._service_role_key = service_role_key
        self._timeout = httpx.Timeout(float(timeout_seconds))
        self._transport = transport

    def __repr__(self) -> str:
        # Credentials, object paths and responses are intentionally absent.
        return f"SupabaseAcceleratorArtifactReader(supabase_url={self._supabase_url!r})"

    def read(
        self,
        bucket: str,
        object_key: str,
        *,
        max_bytes: int,
    ) -> ArtifactRead:
        """Return the full object or fail; this method never truncates data."""

        location = _validate_storage_location(bucket, object_key)
        if (
            isinstance(max_bytes, bool)
            or not isinstance(max_bytes, int)
            or not 1 <= max_bytes <= MAX_ARTIFACT_BYTES
        ):
            raise ValueError("accelerator artifact byte limit is invalid")

        try:
            with httpx.Client(
                transport=self._transport,
                timeout=self._timeout,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                with client.stream(
                    "GET",
                    _object_url(self._supabase_url, location),
                    headers={
                        "apikey": self._service_role_key,
                        "Authorization": f"Bearer {self._service_role_key}",
                    },
                ) as response:
                    # A redirect could move the service credential to another
                    # authority.  A non-200 response has no safe diagnostic.
                    if response.status_code == 400 and _is_exact_missing_response(response):
                        raise ArtifactNotFound()
                    if response.status_code != 200:
                        raise SupabaseAcceleratorArtifactReadError()
                    content_type = response.headers.get("content-type")
                    if not _is_exact_header_value(content_type):
                        raise SupabaseAcceleratorArtifactReadError()
                    content = _read_bounded(response, max_bytes=max_bytes)
        except (
            ArtifactNotFound,
            ArtifactReadLimitExceeded,
            SupabaseAcceleratorArtifactReadError,
        ):
            raise
        except httpx.HTTPError:
            raise SupabaseAcceleratorArtifactReadError() from None

        return ArtifactRead(content=content, content_type=content_type)


def _canonical_supabase_origin(value: str) -> str:
    if not isinstance(value, str) or not value:
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
    canonical = f"{parsed.scheme}://{host if port is None else f'{host}:{port}'}"
    if value != canonical:
        raise ValueError("Supabase Storage API origin is invalid")
    return canonical


def _is_literal_loopback(hostname: str) -> bool:
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _validate_storage_location(bucket: str, object_key: str) -> _StorageLocation:
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
        or any(
            character.isspace() or ord(character) < 0x20 or ord(character) == 0x7F
            for character in object_key
        )
    ):
        raise ValueError("Storage object key is invalid")
    segments = object_key.split("/")
    if any(_SAFE_SEGMENT.fullmatch(segment) is None for segment in segments):
        raise ValueError("Storage object key is invalid")
    return _StorageLocation(bucket=bucket, object_key=object_key)


def _object_url(origin: str, location: _StorageLocation) -> str:
    return (
        f"{origin}/storage/v1/object/{quote(location.bucket, safe='')}"
        f"/{quote(location.object_key, safe='/')}"
    )


def _is_exact_header_value(value: str | None) -> bool:
    """A missing/malformed Content-Type cannot be truthfully reported."""

    return (
        isinstance(value, str)
        and bool(value.strip())
        and not any(character in value for character in "\r\n\x00")
    )


def _read_bounded(response: httpx.Response, *, max_bytes: int) -> bytes:
    content_length = response.headers.get("content-length")
    if content_length is not None:
        if _DECIMAL.fullmatch(content_length) is None:
            raise SupabaseAcceleratorArtifactReadError()
        try:
            declared_length = int(content_length)
        except ValueError:
            raise SupabaseAcceleratorArtifactReadError() from None
        if declared_length > max_bytes:
            raise ArtifactReadLimitExceeded("accelerator artifact exceeds byte limit")

    received = bytearray()
    for chunk in response.iter_raw():
        if len(received) + len(chunk) > max_bytes:
            raise ArtifactReadLimitExceeded("accelerator artifact exceeds byte limit")
        received.extend(chunk)
    return bytes(received)


def _is_exact_missing_response(response: httpx.Response) -> bool:
    """Recognize only the measured self-hosted Storage missing-object shape."""

    content_type = response.headers.get("content-type", "")
    if content_type.split(";", 1)[0].strip().lower() != "application/json":
        return False
    try:
        raw = _read_bounded(response, max_bytes=_MAX_ERROR_RESPONSE_BYTES)
        payload = json.loads(
            raw,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (ArtifactReadLimitExceeded, TypeError, ValueError, RecursionError):
        return False
    return (
        isinstance(payload, dict)
        and set(payload) == {"statusCode", "error", "message"}
        and payload.get("statusCode") == "404"
        and payload.get("error") == "not_found"
        and isinstance(payload.get("message"), str)
    )


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_json_constant(_: str) -> object:
    raise ValueError("non-finite JSON")
