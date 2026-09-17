"""Least-authority Supabase uploader for persistent-Surya input objects.

This adapter is intentionally separate from the worker's general Storage
client.  It can create objects only in one configured bucket and key prefix,
never follows redirects or ambient proxies, and treats an existing object as
an idempotent retry only after comparing its media type and complete bytes.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import hmac
import ipaddress
import math
import re
import time
from urllib.parse import quote, urlsplit

import httpx

__all__ = [
    "SupabaseAcceleratorInputUploadError",
    "SupabaseAcceleratorInputUploader",
]


_MAX_OBJECT_KEY_CHARS = 1_024
_MAX_UPLOAD_BYTES = 1_073_741_824
_MAX_SERVICE_KEY_CHARS = 16_384
_SAFE_SEGMENT = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,127})?")
_SAFE_DNS_NAME = re.compile(
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*"
)
_DECIMAL = re.compile(r"[0-9]+")
_ALLOWED_CONTENT_TYPES = frozenset({"application/json", "image/png"})
_CONFLICT_STATUSES = frozenset({400, 409})
_AMBIGUOUS_GATEWAY_STATUSES = frozenset({502, 503, 504})
_VERIFY_ATTEMPTS = 3
_VERIFY_RETRY_DELAY_SECONDS = 0.25
_MANIFEST_KEY_SUFFIX = re.compile(
    r"(?P<manifest_sha256>[0-9a-f]{64})/render-manifest\.json"
)
_PAGE_KEY_SUFFIX = re.compile(
    r"[0-9a-f]{64}/page-(?P<page>[0-9]{4,})-(?P<page_sha256>[0-9a-f]{64})\.png"
)


class SupabaseAcceleratorInputUploadError(RuntimeError):
    """A fixed, redacted trusted-input Storage failure."""

    def __init__(self) -> None:
        # Provider bodies, object keys, origins and credentials are omitted.
        super().__init__("accelerator input upload failed")


@dataclass(frozen=True, slots=True)
class _StorageLocation:
    bucket: str
    object_key: str


class SupabaseAcceleratorInputUploader:
    """Create immutable Surya inputs within one preconfigured Storage scope."""

    def __init__(
        self,
        *,
        supabase_url: str,
        service_role_key: str,
        allowed_bucket: str,
        object_key_prefix: str,
        max_object_bytes: int,
        timeout_seconds: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._supabase_url = _canonical_supabase_origin(supabase_url)
        self._service_role_key = _validate_service_role_key(service_role_key)
        self._allowed_bucket = _validate_bucket(allowed_bucket)
        self._object_key_prefix = _validate_object_key_prefix(object_key_prefix)
        if (
            isinstance(max_object_bytes, bool)
            or not isinstance(max_object_bytes, int)
            or not 1 <= max_object_bytes <= _MAX_UPLOAD_BYTES
        ):
            raise ValueError("accelerator input byte limit is invalid")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(float(timeout_seconds))
            or timeout_seconds <= 0
        ):
            raise ValueError("accelerator input timeout is invalid")
        self._max_object_bytes = max_object_bytes
        self._timeout = httpx.Timeout(float(timeout_seconds))
        self._transport = transport

    def __repr__(self) -> str:
        # Do not make credentials or object paths available through diagnostics.
        return (
            "SupabaseAcceleratorInputUploader("
            f"supabase_url={self._supabase_url!r}, "
            f"allowed_bucket={self._allowed_bucket!r})"
        )

    def put_if_absent(
        self,
        *,
        bucket: str,
        object_key: str,
        content: bytes,
        content_type: str,
    ) -> bool:
        """Create one input or verify that its existing value is identical."""

        location = _validate_storage_location(bucket, object_key)
        if (
            location.bucket != self._allowed_bucket
            or not location.object_key.startswith(self._object_key_prefix)
        ):
            raise ValueError("accelerator input location is outside its allowed scope")
        if not isinstance(content, bytes) or not content or len(content) > self._max_object_bytes:
            raise ValueError("accelerator input content is invalid")
        if content_type not in _ALLOWED_CONTENT_TYPES:
            raise ValueError("accelerator input content type is invalid")
        _validate_content_addressed_input(
            object_key=location.object_key,
            object_key_prefix=self._object_key_prefix,
            content=content,
            content_type=content_type,
        )

        url = _object_url(self._supabase_url, location)
        headers = {
            "apikey": self._service_role_key,
            "Authorization": f"Bearer {self._service_role_key}",
            "Content-Type": content_type,
            "x-upsert": "false",
        }
        try:
            with httpx.Client(
                transport=self._transport,
                timeout=self._timeout,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                # Stream the response so an untrusted provider diagnostic is
                # closed without being copied into memory or an exception.
                try:
                    with client.stream(
                        "POST",
                        url,
                        headers=headers,
                        content=content,
                    ) as response:
                        if response.status_code in {200, 201}:
                            return True
                        # A gateway failure after a create-only POST is
                        # ambiguous: the upstream may have committed the object
                        # before the downstream response was lost.  Never replay
                        # that mutation here.  Reconcile the immutable object by
                        # exact GET instead, just as for an explicit conflict.
                        if response.status_code not in (
                            _CONFLICT_STATUSES | _AMBIGUOUS_GATEWAY_STATUSES
                        ):
                            raise SupabaseAcceleratorInputUploadError()
                except (httpx.HTTPError, httpx.StreamError):
                    # A transport failure while awaiting the create response
                    # has the same commit-unknown semantics as a gateway 5xx.
                    # Do not replay the mutation; the bounded exact GET below
                    # is the only safe reconciliation path.
                    pass

                self._verify_existing(
                    client=client,
                    url=url,
                    expected=content,
                    expected_content_type=content_type,
                )
        except SupabaseAcceleratorInputUploadError:
            raise
        except (httpx.HTTPError, httpx.StreamError):
            raise SupabaseAcceleratorInputUploadError() from None
        return False

    def _verify_existing(
        self,
        *,
        client: httpx.Client,
        url: str,
        expected: bytes,
        expected_content_type: str,
    ) -> None:
        headers = {
            "apikey": self._service_role_key,
            "Authorization": f"Bearer {self._service_role_key}",
        }
        for attempt in range(_VERIFY_ATTEMPTS):
            try:
                with client.stream("GET", url, headers=headers) as response:
                    if response.status_code in _AMBIGUOUS_GATEWAY_STATUSES:
                        transient = True
                    else:
                        transient = False
                        if response.status_code != 200:
                            raise SupabaseAcceleratorInputUploadError()
                        if (
                            _normalized_content_type(
                                response.headers.get("content-type")
                            )
                            != expected_content_type
                        ):
                            raise SupabaseAcceleratorInputUploadError()
                        existing = _read_exactly_bounded(
                            response,
                            max_bytes=len(expected),
                        )
                if not transient:
                    if not hmac.compare_digest(existing, expected):
                        raise SupabaseAcceleratorInputUploadError()
                    return
            except (httpx.HTTPError, httpx.StreamError):
                transient = True
            if not transient or attempt + 1 >= _VERIFY_ATTEMPTS:
                break
            time.sleep(_VERIFY_RETRY_DELAY_SECONDS * (attempt + 1))
        raise SupabaseAcceleratorInputUploadError()


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
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or port is not None and not 1 <= port <= 65_535
    ):
        raise ValueError("Supabase Storage API origin is invalid")

    hostname = parsed.hostname
    if "%" in hostname:
        # Scoped IPv6 literals are unnecessary for loopback and introduce an
        # additional zone identifier into URL authority interpretation.
        raise ValueError("Supabase Storage API origin is invalid")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if parsed.scheme == "http" and (address is None or not address.is_loopback):
        raise ValueError("Supabase Storage API origin is invalid")

    if address is not None:
        host = str(address)
        if address.version == 6:
            host = f"[{host}]"
    else:
        if _SAFE_DNS_NAME.fullmatch(hostname) is None or len(hostname) > 253:
            raise ValueError("Supabase Storage API origin is invalid")
        host = hostname
    authority = host if port is None else f"{host}:{port}"
    canonical = f"{parsed.scheme}://{authority}"
    if value != canonical:
        raise ValueError("Supabase Storage API origin is invalid")
    return canonical


def _validate_service_role_key(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > _MAX_SERVICE_KEY_CHARS
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise ValueError("Supabase service-role key is invalid")
    return value


def _validate_bucket(value: str) -> str:
    if not isinstance(value, str) or _SAFE_SEGMENT.fullmatch(value) is None:
        raise ValueError("Storage bucket is invalid")
    return value


def _validate_object_key_prefix(value: str) -> str:
    if not isinstance(value, str) or not value.endswith("/"):
        raise ValueError("Storage object key prefix is invalid")
    prefix_key = value[:-1]
    if not prefix_key or len(value) > _MAX_OBJECT_KEY_CHARS:
        raise ValueError("Storage object key prefix is invalid")
    _validate_object_key(prefix_key)
    return value


def _validate_storage_location(bucket: str, object_key: str) -> _StorageLocation:
    return _StorageLocation(
        bucket=_validate_bucket(bucket),
        object_key=_validate_object_key(object_key),
    )


def _validate_object_key(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > _MAX_OBJECT_KEY_CHARS
        or value.startswith("/")
        or value.endswith("/")
        or "%" in value
        or "\\" in value
        or any(
            character.isspace() or ord(character) < 0x20 or ord(character) == 0x7F
            for character in value
        )
    ):
        raise ValueError("Storage object key is invalid")
    if any(_SAFE_SEGMENT.fullmatch(segment) is None for segment in value.split("/")):
        raise ValueError("Storage object key is invalid")
    return value


def _object_url(origin: str, location: _StorageLocation) -> str:
    return (
        f"{origin}/storage/v1/object/{quote(location.bucket, safe='')}"
        f"/{quote(location.object_key, safe='/')}"
    )


def _validate_content_addressed_input(
    *,
    object_key: str,
    object_key_prefix: str,
    content: bytes,
    content_type: str,
) -> None:
    suffix = object_key[len(object_key_prefix) :]
    if content_type == "application/json":
        match = _MANIFEST_KEY_SUFFIX.fullmatch(suffix)
        expected_sha256 = None if match is None else match.group("manifest_sha256")
    else:
        match = _PAGE_KEY_SUFFIX.fullmatch(suffix)
        if match is None:
            expected_sha256 = None
        else:
            page_text = match.group("page")
            page = int(page_text)
            expected_sha256 = (
                match.group("page_sha256")
                if page > 0 and page_text == f"{page:04d}"
                else None
            )
    if expected_sha256 is None or not hmac.compare_digest(
        sha256(content).hexdigest(),
        expected_sha256,
    ):
        raise ValueError("accelerator input content-address binding is invalid")


def _normalized_content_type(value: str | None) -> str | None:
    if (
        not isinstance(value, str)
        or not value
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        return None
    media_type = value.split(";", 1)[0].strip().lower()
    return media_type if media_type in _ALLOWED_CONTENT_TYPES else None


def _read_exactly_bounded(response: httpx.Response, *, max_bytes: int) -> bytes:
    content_length = response.headers.get("content-length")
    if content_length is not None:
        if _DECIMAL.fullmatch(content_length) is None:
            raise SupabaseAcceleratorInputUploadError()
        try:
            declared_length = int(content_length)
        except ValueError:
            raise SupabaseAcceleratorInputUploadError() from None
        if declared_length > max_bytes:
            raise SupabaseAcceleratorInputUploadError()

    received = bytearray()
    for chunk in response.iter_raw():
        if len(received) + len(chunk) > max_bytes:
            raise SupabaseAcceleratorInputUploadError()
        received.extend(chunk)
    return bytes(received)
