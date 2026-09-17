"""Bounded HTTPS storage adapter for signed accelerator capabilities.

This module deliberately has no bucket credential, service-role key, cookie
jar, redirect support, or provider-specific retry loop.  Every request is
authorized only by the already validated :class:`SignedStorageCapability`
passed to that call.  Exception messages are fixed public strings so a signed
URL, object key, response body, or upstream diagnostic cannot cross the
worker boundary.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from hashlib import sha256
import logging
import math
import re
from time import monotonic as _system_monotonic
from urllib.parse import urlsplit

import httpx

from worker.contracts.accelerator import (
    AcceleratorContractError,
    SignedStorageCapability,
)

from .handler import (
    StorageContentFailure,
    StorageGetContentFailure,
    StorageInfrastructureFailure,
    StoragePutConflictFailure,
)

__all__ = ["BoundedHttpsStorageAdapter", "validate_loopback_proxy_url"]


_CONTENT_LENGTH = re.compile(r"0|[1-9][0-9]{0,19}")
_GET_SUCCESS = frozenset({200})
_PUT_SUCCESS = frozenset({200, 201, 204})
# These 4xx statuses indicate that the storage service cannot make a reliable
# decision for this attempt.  They are infrastructure outcomes and may be
# retried by the caller, subject to its retry and capability-expiry policy.
_TRANSIENT_STATUS = frozenset({408, 425, 429})
# A create-only collision is intentionally not a content rejection.  It can
# mean that a previous PUT succeeded but its acknowledgement was lost.  The
# trusted owner must reconcile idempotency; this adapter has no read authority
# and therefore never guesses.
_PUT_CONFLICT = frozenset({409, 412})
_SENSITIVE_TRANSPORT_LOGGERS = ("httpx", "httpcore")


class _DeadlineBoundedByteStream(httpx.SyncByteStream):
    """Upload immutable bytes in bounded chunks while checking one deadline.

    ``httpx`` accepts a bytes object directly, but that leaves no application
    boundary between a potentially long sequence of socket writes.  This
    stream adds a deterministic deadline check for every bounded write chunk;
    httpx's write timeout is separately clamped to the remaining budget.
    """

    _CHUNK_BYTES = 64 * 1024

    def __init__(self, content: bytes, *, assert_before_deadline: Callable[[], None]) -> None:
        self._content = content
        self._assert_before_deadline = assert_before_deadline

    def __iter__(self):  # type: ignore[no-untyped-def]
        view = memoryview(self._content)
        for offset in range(0, len(view), self._CHUNK_BYTES):
            self._assert_before_deadline()
            yield view[offset : offset + self._CHUNK_BYTES]
        self._assert_before_deadline()


class BoundedHttpsStorageAdapter:
    """Use one narrowly scoped signed HTTPS capability per storage operation.

    A fresh ``httpx.Client`` is used for every operation.  Apart from keeping
    lifecycle ownership simple in a serverless handler, this prevents an
    upstream ``Set-Cookie`` response from becoming ambient authority on a
    later signed request.  ``trust_env=False`` also disables inherited proxy
    and certificate environment configuration.
    """

    def __init__(
        self,
        *,
        hard_max_bytes: int,
        connect_timeout_seconds: float = 10.0,
        read_timeout_seconds: float = 30.0,
        write_timeout_seconds: float = 30.0,
        pool_timeout_seconds: float = 5.0,
        proxy_url: str | None = None,
        transport: httpx.BaseTransport | None = None,
        now: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        if (
            isinstance(hard_max_bytes, bool)
            or not isinstance(hard_max_bytes, int)
            or hard_max_bytes <= 0
        ):
            raise ValueError("hard_max_bytes must be a positive integer")
        timeout_values = (
            connect_timeout_seconds,
            read_timeout_seconds,
            write_timeout_seconds,
            pool_timeout_seconds,
        )
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or value <= 0
            for value in timeout_values
        ):
            raise ValueError("HTTP timeouts must be finite positive numbers")
        self._hard_max_bytes = hard_max_bytes
        self._timeout = httpx.Timeout(
            connect=float(connect_timeout_seconds),
            read=float(read_timeout_seconds),
            write=float(write_timeout_seconds),
            pool=float(pool_timeout_seconds),
        )
        self._transport = transport
        self._proxy_url = validate_loopback_proxy_url(proxy_url)
        self._now = now or (lambda: datetime.now(UTC))
        self._monotonic = monotonic or _system_monotonic
        # httpx's normal INFO request record contains the complete URL.  A
        # signed capability keeps its authority in the query string, so clamp
        # both transport loggers even when the surrounding RunPod image has a
        # process-wide DEBUG/INFO logging configuration.
        self._suppress_sensitive_transport_logs()

    def get(
        self,
        capability: SignedStorageCapability,
        *,
        max_bytes: int,
        deadline_monotonic: float | None = None,
    ) -> bytes:
        """Read exactly one bounded object and verify MIME and SHA-256."""

        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
            raise StorageGetContentFailure("storage read limit rejected")
        url = self._validated_url(
            capability,
            method="GET",
            access_mode="read_only",
            failure_type=StorageGetContentFailure,
        )
        byte_limit = min(
            max_bytes,
            capability.resource_caps.max_bytes,
            self._hard_max_bytes,
        )

        try:
            self._assert_before_deadline(deadline_monotonic)
            self._suppress_sensitive_transport_logs()
            with self._client(deadline_monotonic) as client:
                with client.stream(
                    "GET",
                    url,
                    headers={"Accept-Encoding": "identity"},
                ) as response:
                    self._check_get_status(response.status_code)
                    self._check_identity_encoding(response)
                    self._check_response_mime(response, capability)
                    declared_length = self._content_length(response)
                    if declared_length is not None and declared_length > byte_limit:
                        raise StorageGetContentFailure("storage object exceeds byte limit")

                    # Do not collect chunks and then concatenate them: that
                    # retains every chunk and allocates a second full-sized
                    # immutable object at ``join`` time.  A bounded bytearray
                    # has one mutable receive buffer; converting it to bytes
                    # at the trust boundary is the only intentional copy.
                    content_buffer = bytearray()
                    actual_length = 0
                    for chunk in response.iter_raw():
                        self._assert_before_deadline(deadline_monotonic)
                        actual_length += len(chunk)
                        if actual_length > byte_limit:
                            raise StorageGetContentFailure(
                                "storage object exceeds byte limit"
                            )
                        content_buffer.extend(chunk)
                    self._assert_before_deadline(deadline_monotonic)
        except StorageGetContentFailure:
            raise
        except httpx.InvalidURL:
            raise StorageGetContentFailure("storage capability rejected") from None
        except httpx.RequestError:
            raise StorageInfrastructureFailure("storage transport unavailable") from None
        except httpx.HTTPError:
            raise StorageInfrastructureFailure("storage response unavailable") from None

        if declared_length is not None and actual_length != declared_length:
            raise StorageGetContentFailure("storage content length mismatch")
        content = bytes(content_buffer)
        expected_sha256 = capability.expected_sha256
        if expected_sha256 is None or sha256(content).hexdigest() != expected_sha256:
            raise StorageGetContentFailure("storage object integrity mismatch")
        return content

    def put_create_only(
        self,
        capability: SignedStorageCapability,
        *,
        content: bytes,
        content_type: str,
        deadline_monotonic: float | None = None,
    ) -> None:
        """Create one immutable object without following redirects or retrying."""

        if not isinstance(content, bytes):
            raise StorageContentFailure("storage upload content rejected")
        url = self._validated_url(
            capability,
            method="PUT",
            access_mode="create_only",
            failure_type=StorageContentFailure,
        )
        if content_type not in capability.resource_caps.allowed_mime_types:
            raise StorageContentFailure("storage upload MIME rejected")
        byte_limit = min(capability.resource_caps.max_bytes, self._hard_max_bytes)
        if len(content) > byte_limit:
            raise StorageContentFailure("storage upload exceeds byte limit")

        try:
            self._assert_before_deadline(deadline_monotonic)
            self._suppress_sensitive_transport_logs()
            with self._client(deadline_monotonic) as client:
                upload_stream = _DeadlineBoundedByteStream(
                    content,
                    assert_before_deadline=lambda: self._assert_before_deadline(
                        deadline_monotonic
                    ),
                )
                # An iterator is intentionally used here.  httpx normalises a
                # SyncByteStream passed as ``content`` into one ByteStream in
                # some releases, which removes the per-chunk deadline check.
                # IteratorByteStream preserves the bounded source iterator.
                request = client.build_request(
                    "PUT",
                    url,
                    headers={
                        "Content-Type": content_type,
                        "Content-Length": str(len(content)),
                        # A create-only signed capability is still paired with
                        # both the standard conditional request guard and the
                        # Supabase Storage no-upsert guard used by this
                        # deployment.  There is no fallback PUT and no
                        # automatic retry.
                        "If-None-Match": "*",
                        "x-upsert": "false",
                    },
                    content=(bytes(chunk) for chunk in upload_stream),
                )
                response = client.send(request, stream=True)
                try:
                    self._assert_before_deadline(deadline_monotonic)
                    status_code = response.status_code
                    if status_code in _PUT_SUCCESS:
                        return
                    if status_code in _TRANSIENT_STATUS:
                        raise StorageInfrastructureFailure(
                            "storage upload unavailable"
                        )
                    if status_code in _PUT_CONFLICT:
                        raise StoragePutConflictFailure(
                            "storage create-only reconciliation required"
                        )
                    if 500 <= status_code <= 599:
                        raise StorageInfrastructureFailure(
                            "storage upload unavailable"
                        )
                    if 300 <= status_code <= 399:
                        raise StorageContentFailure("storage redirect rejected")
                    raise StorageContentFailure("storage upload rejected")
                finally:
                    response.close()
        except (StorageContentFailure, StorageInfrastructureFailure):
            raise
        except httpx.InvalidURL:
            raise StorageContentFailure("storage capability rejected") from None
        except httpx.RequestError:
            raise StorageInfrastructureFailure("storage transport unavailable") from None
        except httpx.HTTPError:
            raise StorageInfrastructureFailure("storage response unavailable") from None

    def _client(self, deadline_monotonic: float | None) -> httpx.Client:
        timeout = self._timeout_for_deadline(deadline_monotonic)
        return httpx.Client(
            transport=self._transport,
            proxy=self._proxy_url,
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
        )

    @staticmethod
    def _suppress_sensitive_transport_logs() -> None:
        """Prevent a mutable process logger setting from exposing signed URLs.

        The service may reconfigure root/transport logging after cold start.
        Apply this immediately before every actual HTTP client creation, not
        just once in ``__init__``.  ``httpx``'s regular request record embeds
        the full URL (including a signed query authority), so INFO is unsafe
        at this boundary.
        """

        for logger_name in _SENSITIVE_TRANSPORT_LOGGERS:
            logging.getLogger(logger_name).setLevel(logging.WARNING)

    def _timeout_for_deadline(self, deadline_monotonic: float | None) -> httpx.Timeout:
        """Clamp each HTTP phase timeout to the remaining invocation budget."""

        remaining = self._remaining_seconds(deadline_monotonic)
        if remaining is None:
            return self._timeout
        # ``httpx`` requires strictly positive timeout values.  The preceding
        # check turns an exhausted absolute deadline into our redacted typed
        # failure instead of accidentally substituting an unbounded timeout.
        return httpx.Timeout(
            connect=min(self._timeout.connect, remaining),
            read=min(self._timeout.read, remaining),
            write=min(self._timeout.write, remaining),
            pool=min(self._timeout.pool, remaining),
        )

    def _remaining_seconds(self, deadline_monotonic: float | None) -> float | None:
        if deadline_monotonic is None:
            return None
        if (
            isinstance(deadline_monotonic, bool)
            or not isinstance(deadline_monotonic, (int, float))
            or not math.isfinite(float(deadline_monotonic))
        ):
            raise StorageInfrastructureFailure("storage deadline unavailable")
        try:
            current = self._monotonic()
        except Exception:
            raise StorageInfrastructureFailure("storage deadline unavailable") from None
        if isinstance(current, bool) or not isinstance(current, (int, float)):
            raise StorageInfrastructureFailure("storage deadline unavailable")
        current_value = float(current)
        if not math.isfinite(current_value):
            raise StorageInfrastructureFailure("storage deadline unavailable")
        return float(deadline_monotonic) - current_value

    def _assert_before_deadline(self, deadline_monotonic: float | None) -> None:
        remaining = self._remaining_seconds(deadline_monotonic)
        if remaining is not None and remaining <= 0:
            raise StorageInfrastructureFailure("storage deadline exceeded")

    def _validated_url(
        self,
        capability: SignedStorageCapability,
        *,
        method: str,
        access_mode: str,
        failure_type: type[StorageContentFailure],
    ) -> str:
        if not isinstance(capability, SignedStorageCapability):
            raise failure_type("storage capability rejected")
        try:
            url = capability.signed_url()
        except (AcceleratorContractError, TypeError, ValueError):
            raise failure_type("storage capability rejected") from None
        if capability.method != method or capability.access_mode != access_mode:
            raise failure_type("storage capability method rejected")
        try:
            current_time = self._now()
            if current_time.tzinfo is None or current_time.utcoffset() is None:
                raise ValueError
        except Exception:
            raise StorageInfrastructureFailure("storage clock unavailable") from None
        if capability.expires_at <= current_time:
            raise failure_type("storage capability expired")
        return url
    @staticmethod
    def _check_get_status(status_code: int) -> None:
        if status_code in _GET_SUCCESS:
            return
        if status_code in _TRANSIENT_STATUS:
            raise StorageInfrastructureFailure("storage download unavailable")
        if 500 <= status_code <= 599:
            raise StorageInfrastructureFailure("storage download unavailable")
        if 300 <= status_code <= 399:
            raise StorageGetContentFailure("storage redirect rejected")
        raise StorageGetContentFailure("storage object rejected")

    @staticmethod
    def _check_identity_encoding(response: httpx.Response) -> None:
        content_encoding = response.headers.get("content-encoding")
        if content_encoding not in {None, "identity"}:
            raise StorageGetContentFailure("storage content encoding rejected")

    @staticmethod
    def _check_response_mime(
        response: httpx.Response,
        capability: SignedStorageCapability,
    ) -> None:
        content_type = response.headers.get("content-type")
        # Deliberately reject parameters (including charset) rather than
        # normalising an upstream value into something the capability did not
        # authorize.
        if content_type not in capability.resource_caps.allowed_mime_types:
            raise StorageGetContentFailure("storage content type rejected")

    @staticmethod
    def _content_length(response: httpx.Response) -> int | None:
        raw_value = response.headers.get("content-length")
        if raw_value is None:
            return None
        if _CONTENT_LENGTH.fullmatch(raw_value) is None:
            raise StorageGetContentFailure("storage content length rejected")
        return int(raw_value)


def validate_loopback_proxy_url(value: str | None) -> str | None:
    """Accept only an unauthenticated numeric-loopback HTTP/SOCKS proxy."""

    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > 512:
        raise ValueError("storage proxy URL rejected")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ValueError("storage proxy URL rejected") from None
    if (
        parsed.scheme not in {"http", "socks5", "socks5h"}
        or parsed.hostname not in {"127.0.0.1", "::1"}
        or port is None
        or not 1 <= port <= 65_535
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError("storage proxy URL rejected")
    host = "[::1]" if parsed.hostname == "::1" else "127.0.0.1"
    return f"{parsed.scheme}://{host}:{port}"
