"""Capability-safe HTTP adapter for the persistent RunPod Surya service.

The trusted worker owns dispatch policy, database fencing, and result
acceptance.  This adapter owns only the small ``submit``/``poll``/``cancel``
transport boundary.  In particular, it sends the capability-bearing request
body only after validating the deployment policy, never follows redirects,
and turns all provider diagnostics into fixed public errors.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
import ipaddress
import json
import math
import re
from typing import Any
from urllib.parse import urlsplit

import httpx

from worker.contracts.accelerator import (
    AcceleratorContractError,
    AcceleratorDispatchPolicy,
    AcceleratorJobStatus,
    SuryaLayoutRequest,
    validate_accelerator_dispatch,
)
from worker.ports.accelerator import AcceleratorJobNotFoundError

__all__ = [
    "PersistentSuryaHttpAdapter",
    "PersistentSuryaHttpError",
    "validate_persistent_surya_base_url",
]


_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_EXTERNAL_JOB_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_PUBLIC_REASON_CODE = re.compile(r"[a-z0-9]+(?:_[a-z0-9]+)*")
_PUBLIC_JOB_KEYS = frozenset(
    {
        "id",
        "state",
        "logical_compute_key",
        "request_digest",
        "created_at",
        "started_at",
        "completed_at",
        "reason_code",
        "output",
    }
)
_JSON_CONTENT_TYPE = "application/json"


class PersistentSuryaHttpError(RuntimeError):
    """A redacted, retryable persistent-worker transport/provider failure."""

    def __init__(self) -> None:
        super().__init__("persistent Surya service unavailable")


def validate_persistent_surya_base_url(
    value: str,
    *,
    allow_loopback_http: bool = False,
) -> str:
    """Return a canonical service origin without accepting ambient authority.

    A bearer token travels on every call, so a remote service is HTTPS-only.
    ``allow_loopback_http`` exists solely for explicit local test/dev fixtures;
    it is false by default and does not permit private-network cleartext.
    """

    if not isinstance(value, str):
        raise ValueError("persistent Surya base URL must be an HTTPS origin")
    candidate = value.strip()
    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except ValueError:
        raise ValueError("persistent Surya base URL must be an HTTPS origin") from None
    if (
        parsed.scheme not in {"https", "http"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or (port is not None and not 0 < port < 65_536)
    ):
        raise ValueError("persistent Surya base URL must be an HTTPS origin")
    if parsed.scheme == "http" and not (
        allow_loopback_http and _is_literal_loopback(parsed.hostname)
    ):
        raise ValueError("persistent Surya base URL must be an HTTPS origin")

    host = parsed.hostname
    if ":" in host:
        host = f"[{host}]"
    authority = host if port is None else f"{host}:{port}"
    return f"{parsed.scheme}://{authority}"


def _is_literal_loopback(hostname: str) -> bool:
    if hostname.lower() == "localhost":
        return True
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return False
    return address.is_loopback


class PersistentSuryaHttpAdapter:
    """Synchronous :class:`AcceleratorPort` adapter for ``/jobs``.

    The service response deliberately uses a durable public-record envelope.
    This adapter accepts only that exact envelope and reconstructs an
    ``AcceleratorJobStatus`` after binding the public record to its terminal
    output where present.  Raw bodies, URLs and bearer tokens are never
    included in raised exception messages.
    """

    def __init__(
        self,
        *,
        base_url: str,
        bearer_token: str,
        dispatch_policy: AcceleratorDispatchPolicy,
        timeout_seconds: float = 30.0,
        max_response_bytes: int = 1_048_576,
        transport: httpx.BaseTransport | None = None,
        now: Callable[[], datetime] | None = None,
        allow_loopback_http: bool = False,
    ) -> None:
        self._base_url = validate_persistent_surya_base_url(
            base_url,
            allow_loopback_http=allow_loopback_http,
        )
        if (
            not isinstance(bearer_token, str)
            or not bearer_token
            or bearer_token != bearer_token.strip()
            or len(bearer_token) > 4_096
            or any(ord(character) < 0x20 or ord(character) == 0x7F for character in bearer_token)
        ):
            raise ValueError("persistent Surya bearer token is invalid")
        if not math.isfinite(float(timeout_seconds)) or timeout_seconds <= 0:
            raise ValueError("persistent Surya timeout must be a finite positive number")
        if (
            isinstance(max_response_bytes, bool)
            or not isinstance(max_response_bytes, int)
            or not 1 <= max_response_bytes <= _MAX_RESPONSE_BYTES
        ):
            raise ValueError("persistent Surya response limit is invalid")

        self._bearer_token = bearer_token
        self._dispatch_policy = dispatch_policy
        self._timeout = httpx.Timeout(float(timeout_seconds))
        self._max_response_bytes = max_response_bytes
        self._transport = transport
        self._now = now or (lambda: datetime.now(UTC))

    def submit(self, request: SuryaLayoutRequest) -> AcceleratorJobStatus:
        """Validate policy immediately before sending one capability payload."""

        validate_accelerator_dispatch(
            request,
            self._dispatch_policy,
            now=self._now(),
        )
        payload = self._request_json(
            "POST",
            "/jobs",
            expected_statuses=frozenset({200, 202}),
            json_body={"input": request.to_wire_payload()},
            headers={"Idempotency-Key": request.logical_compute_key},
        )
        status = self._parse_public_status(payload)
        if (
            status.external_job_id != request.logical_compute_key
            or status.logical_compute_key != request.logical_compute_key
            or status.request_digest != request.request_digest
        ):
            raise AcceleratorContractError("persistent Surya response is not bound to request")
        return status

    def get_status(self, external_job_id: str) -> AcceleratorJobStatus:
        job_id = self._validated_job_id(external_job_id)
        payload = self._request_json(
            "GET",
            f"/jobs/{job_id}",
            expected_statuses=frozenset({200}),
            not_found_is_typed=True,
        )
        status = self._parse_public_status(payload)
        if status.external_job_id != job_id:
            raise AcceleratorContractError("persistent Surya response job binding is invalid")
        return status

    def cancel(self, external_job_id: str) -> AcceleratorJobStatus:
        """Ask the persistent service to cancel a queued job and return state.

        The persistent API contract uses a JSON response for this endpoint so
        the ``AcceleratorPort`` can retain its synchronous status return type.
        A ``204`` delete-style endpoint is intentionally not accepted here.
        """

        job_id = self._validated_job_id(external_job_id)
        payload = self._request_json(
            "POST",
            f"/jobs/{job_id}/cancel",
            expected_statuses=frozenset({200}),
            not_found_is_typed=True,
        )
        status = self._parse_public_status(payload)
        if status.external_job_id != job_id:
            raise AcceleratorContractError("persistent Surya response job binding is invalid")
        return status

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        expected_statuses: frozenset[int],
        json_body: Mapping[str, object] | None = None,
        headers: Mapping[str, str] | None = None,
        not_found_is_typed: bool = False,
    ) -> Mapping[str, Any]:
        request_headers = {
            "Authorization": f"Bearer {self._bearer_token}",
            "Accept": _JSON_CONTENT_TYPE,
        }
        if headers is not None:
            request_headers.update(headers)
        url = f"{self._base_url}{path}"
        try:
            with httpx.Client(
                transport=self._transport,
                timeout=self._timeout,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                with client.stream(
                    method,
                    url,
                    headers=request_headers,
                    json=json_body,
                ) as response:
                    if response.status_code == 404 and not_found_is_typed:
                        raise AcceleratorJobNotFoundError("persistent Surya job not found")
                    if response.status_code not in expected_statuses:
                        raise PersistentSuryaHttpError()
                    if response.headers.get("content-type") != _JSON_CONTENT_TYPE:
                        raise AcceleratorContractError(
                            "persistent Surya response content type is invalid"
                        )
                    body = self._read_bounded_body(response)
        except (AcceleratorContractError, AcceleratorJobNotFoundError, PersistentSuryaHttpError):
            raise
        except httpx.HTTPError:
            raise PersistentSuryaHttpError() from None

        try:
            decoded = json.loads(body, parse_constant=_reject_nonfinite_json)
        except (TypeError, ValueError, RecursionError):
            raise AcceleratorContractError("persistent Surya response is invalid") from None
        if not isinstance(decoded, dict):
            raise AcceleratorContractError("persistent Surya response is invalid")
        return decoded

    def _read_bounded_body(self, response: httpx.Response) -> bytes:
        content_length = response.headers.get("content-length")
        if content_length is not None:
            try:
                declared = int(content_length)
            except ValueError:
                raise AcceleratorContractError(
                    "persistent Surya response content length is invalid"
                ) from None
            if declared < 0 or declared > self._max_response_bytes:
                raise AcceleratorContractError("persistent Surya response is too large")

        received = bytearray()
        for chunk in response.iter_raw():
            if len(received) + len(chunk) > self._max_response_bytes:
                raise AcceleratorContractError("persistent Surya response is too large")
            received.extend(chunk)
        return bytes(received)

    @staticmethod
    def _validated_job_id(value: str) -> str:
        if not isinstance(value, str) or _EXTERNAL_JOB_ID.fullmatch(value) is None:
            raise AcceleratorContractError("persistent Surya job ID is invalid")
        return value

    @staticmethod
    def _parse_public_status(payload: Mapping[str, Any]) -> AcceleratorJobStatus:
        if set(payload) != _PUBLIC_JOB_KEYS:
            raise AcceleratorContractError("persistent Surya response shape is invalid")
        job_id = payload.get("id")
        if not isinstance(job_id, str) or _EXTERNAL_JOB_ID.fullmatch(job_id) is None:
            raise AcceleratorContractError("persistent Surya response job ID is invalid")
        output = payload.get("output")
        outer = {
            "external_job_id": job_id,
            "state": payload.get("state"),
            "logical_compute_key": payload.get("logical_compute_key"),
            "request_digest": payload.get("request_digest"),
            "reason_code": payload.get("reason_code"),
        }
        if output is None:
            # A persistent Pod intentionally cannot retain signed capabilities
            # after a restart or forced shutdown.  Its durable journal then
            # reports this one terminal provider outcome without fabricating a
            # result manifest.  Translate it to a redacted retryable error;
            # every other terminal state remains invalid without ``output``.
            if payload.get("state") == "infra_retryable":
                PersistentSuryaHttpAdapter._validate_unmaterialized_infra_retry(
                    job_id,
                    payload,
                )
                raise PersistentSuryaHttpError()
            outer["result_manifest"] = None
            try:
                return AcceleratorJobStatus.model_validate(outer)
            except Exception:
                raise AcceleratorContractError("persistent Surya response status is invalid") from None
        if not isinstance(output, dict):
            raise AcceleratorContractError("persistent Surya response status is invalid")
        try:
            status = AcceleratorJobStatus.model_validate(output)
        except Exception:
            raise AcceleratorContractError("persistent Surya response status is invalid") from None
        if any(status.model_dump(mode="json").get(key) != value for key, value in outer.items()):
            raise AcceleratorContractError("persistent Surya response status is unbound")
        return status

    @staticmethod
    def _validate_unmaterialized_infra_retry(
        job_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        logical_compute_key = payload.get("logical_compute_key")
        request_digest = payload.get("request_digest")
        reason_code = payload.get("reason_code")
        if (
            _EXTERNAL_JOB_ID.fullmatch(job_id) is None
            or not isinstance(logical_compute_key, str)
            or _SHA256.fullmatch(logical_compute_key) is None
            or not isinstance(request_digest, str)
            or _SHA256.fullmatch(request_digest) is None
            or not isinstance(reason_code, str)
            or _PUBLIC_REASON_CODE.fullmatch(reason_code) is None
        ):
            raise AcceleratorContractError("persistent Surya response status is invalid")


def _reject_nonfinite_json(_: str) -> object:
    raise ValueError("non-finite JSON")
