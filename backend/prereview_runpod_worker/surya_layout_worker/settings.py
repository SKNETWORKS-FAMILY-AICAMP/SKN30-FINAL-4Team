"""Strict cold-start composition for the RunPod Surya layout worker.

The deployment receives one JSON document in
``PREREVIEW_SURYA_WORKER_CONFIG_JSON``.  Keeping all authority and resource
limits in one bounded value makes it possible to audit the worker without
accidentally composing a storage origin from independent environment lists.
The parser deliberately never includes the source JSON, an invalid value, or
an exception diagnostic in its public errors: this configuration can contain
signed-storage topology and must not become a log payload.

This module imports the adapter *classes* only.  Their GPU-only dependencies
(Pillow, Surya and the RunPod SDK) remain lazy, so normal backend tests can
import this module on a CPU-only host.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import json
import math
import os
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from common_ir_pipeline.pdf_fusion.surya_layout_artifact import (
    MAX_ARTIFACT_BYTES,
    MAX_REGIONS_PER_PAGE,
    MAX_REQUESTED_PAGES,
    MAX_TOTAL_REGIONS,
)
from worker.contracts.accelerator import (
    AcceleratorDispatchPolicy,
    AcceleratorStorageScope,
    SuryaProducerIdentity,
)

from .handler import RunPodSuryaExecutionPolicy, RunPodSuryaLayoutHandler
from .http_storage import BoundedHttpsStorageAdapter, validate_loopback_proxy_url
from .surya_inference import (
    SuryaDeploymentAttestation,
    SuryaEndpointAllowlistPolicy,
    SuryaLayoutInferenceAdapter,
    SuryaVllmEndpointSettings,
    load_deployment_attestation,
)

__all__ = [
    "PREREVIEW_SURYA_WORKER_CONFIG_JSON",
    "SURYA_WORKER_CONFIG_SCHEMA_VERSION",
    "RunPodSuryaWorkerComposition",
    "RunPodSuryaWorkerSettings",
    "SuryaWorkerConfigurationError",
    "build_worker_composition",
    "load_worker_settings",
]


PREREVIEW_SURYA_WORKER_CONFIG_JSON = "PREREVIEW_SURYA_WORKER_CONFIG_JSON"
SURYA_WORKER_CONFIG_SCHEMA_VERSION = "prereview.runpod-surya-worker/v1"

# These are parser protections, not operational settings.  Operational caps
# are supplied explicitly in the JSON document below.
_MAX_CONFIG_BYTES = 64 * 1024
_MAX_CONFIG_DEPTH = 12
_MAX_STORAGE_SCOPES = 16
_MAX_ALLOWED_ORIGINS = 16
_MAX_HTTP_BYTES = 1_073_741_824  # 1 GiB
_MAX_TIMEOUT_SECONDS = 3_600.0
_MAX_TTL_SECONDS = 86_400
_MAX_PAGE_COUNT = MAX_REQUESTED_PAGES
_MAX_TOTAL_RENDERED_PIXELS = 2_000_000_000
_MAX_REGIONS_PER_PAGE = MAX_REGIONS_PER_PAGE
_MAX_TERMINAL_OUTPUT_BYTES = 4 * 1024 * 1024


class SuryaWorkerConfigurationError(RuntimeError):
    """A deliberately non-diagnostic deployment configuration failure."""

    def __init__(self) -> None:
        super().__init__("surya_worker_configuration_invalid")


class _StrictSettingsModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class _HttpSettings(_StrictSettingsModel):
    hard_max_bytes: int = Field(gt=0, le=_MAX_HTTP_BYTES)
    connect_timeout_seconds: float = Field(gt=0, le=_MAX_TIMEOUT_SECONDS)
    read_timeout_seconds: float = Field(gt=0, le=_MAX_TIMEOUT_SECONDS)
    write_timeout_seconds: float = Field(gt=0, le=_MAX_TIMEOUT_SECONDS)
    pool_timeout_seconds: float = Field(gt=0, le=_MAX_TIMEOUT_SECONDS)
    proxy_url: str | None = Field(default=None, max_length=512)

    @field_validator(
        "connect_timeout_seconds",
        "read_timeout_seconds",
        "write_timeout_seconds",
        "pool_timeout_seconds",
    )
    @classmethod
    def _finite_timeout(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("timeout must be finite")
        return value

    @field_validator("proxy_url")
    @classmethod
    def _loopback_proxy(cls, value: str | None) -> str | None:
        return validate_loopback_proxy_url(value)


class _OutputSettings(_StrictSettingsModel):
    max_output_bytes: int = Field(gt=0, le=_MAX_HTTP_BYTES)
    max_terminal_output_bytes: int = Field(ge=192, le=_MAX_TERMINAL_OUTPUT_BYTES)
    max_regions_per_page: int = Field(gt=0, le=_MAX_REGIONS_PER_PAGE)


class _EndpointSettings(_StrictSettingsModel):
    url: str = Field(min_length=1, max_length=2_048)
    allowed_origins: list[str] = Field(default_factory=list, max_length=_MAX_ALLOWED_ORIGINS)


class _DispatchSettings(_StrictSettingsModel):
    max_ttl_seconds: int = Field(gt=0, le=_MAX_TTL_SECONDS)
    max_execution_timeout_seconds: int = Field(gt=0, le=int(_MAX_TIMEOUT_SECONDS))
    max_page_count: int = Field(gt=0, le=_MAX_PAGE_COUNT)
    max_total_input_bytes: int = Field(gt=0, le=_MAX_HTTP_BYTES)
    max_total_rendered_pixels: int = Field(gt=0, le=_MAX_TOTAL_RENDERED_PIXELS)
    max_capability_bytes: int = Field(gt=0, le=_MAX_HTTP_BYTES)
    allowed_mime_types: list[str] = Field(min_length=1, max_length=8)


class _RawWorkerSettings(_StrictSettingsModel):
    schema_version: str
    storage_scopes: list[dict[str, Any]] = Field(
        min_length=1,
        max_length=_MAX_STORAGE_SCOPES,
    )
    dispatch_policy: _DispatchSettings
    producer: dict[str, Any]
    http: _HttpSettings
    output: _OutputSettings
    surya_endpoint: _EndpointSettings


@dataclass(frozen=True)
class RunPodSuryaWorkerSettings:
    """Fully validated deployment inputs, with no raw JSON retained."""

    dispatch_policy: AcceleratorDispatchPolicy
    producer: SuryaProducerIdentity
    http: _HttpSettings
    output: _OutputSettings
    endpoint: SuryaVllmEndpointSettings
    endpoint_policy: SuryaEndpointAllowlistPolicy | None


@dataclass(frozen=True)
class RunPodSuryaWorkerComposition:
    """One cold-start-owned set of ports and the safe core handler."""

    handler: RunPodSuryaLayoutHandler
    policy: RunPodSuryaExecutionPolicy


def load_worker_settings(
    environ: Mapping[str, str] | None = None,
) -> RunPodSuryaWorkerSettings:
    """Load the sole deployment document without ever exposing its contents."""

    environment = os.environ if environ is None else environ
    try:
        raw = environment.get(PREREVIEW_SURYA_WORKER_CONFIG_JSON)
    except Exception:
        raise SuryaWorkerConfigurationError() from None
    if not isinstance(raw, str) or not raw or len(raw.encode("utf-8")) > _MAX_CONFIG_BYTES:
        raise SuryaWorkerConfigurationError() from None

    try:
        parsed = _parse_strict_json(raw)
        validated = _RawWorkerSettings.model_validate(parsed)
        if validated.schema_version != SURYA_WORKER_CONFIG_SCHEMA_VERSION:
            raise ValueError("unsupported worker config schema")
        scopes = tuple(
            AcceleratorStorageScope.model_validate(scope)
            for scope in validated.storage_scopes
        )
        _reject_duplicate_scopes(scopes)
        dispatch_policy = AcceleratorDispatchPolicy(
            allowed_scopes=scopes,
            **validated.dispatch_policy.model_dump(mode="python"),
        )
        _validate_dispatch_mime_types(dispatch_policy)
        producer = SuryaProducerIdentity.model_validate(validated.producer)
        _reject_placeholder_identity(producer)
        _validate_cross_field_limits(
            dispatch_policy=dispatch_policy,
            http=validated.http,
            output=validated.output,
        )
        endpoint = SuryaVllmEndpointSettings(
            url=validated.surya_endpoint.url,
            backend="vllm",
            autostart=False,
        )
        endpoint_policy = _endpoint_policy(validated.surya_endpoint.allowed_origins)
    except (TypeError, ValueError, ValidationError, json.JSONDecodeError):
        raise SuryaWorkerConfigurationError() from None
    except Exception:
        # Pydantic/adapter validation has defensively hidden input values, but
        # retain the stronger worker invariant: callers see no config detail.
        raise SuryaWorkerConfigurationError() from None

    return RunPodSuryaWorkerSettings(
        dispatch_policy=dispatch_policy,
        producer=producer,
        http=validated.http,
        output=validated.output,
        endpoint=endpoint,
        endpoint_policy=endpoint_policy,
    )


def build_worker_composition(
    settings: RunPodSuryaWorkerSettings,
    *,
    storage_factory: Callable[..., object] = BoundedHttpsStorageAdapter,
    inference_factory: Callable[..., object] = SuryaLayoutInferenceAdapter,
    handler_factory: Callable[..., RunPodSuryaLayoutHandler] = RunPodSuryaLayoutHandler,
    attestation_loader: Callable[[], SuryaDeploymentAttestation] = load_deployment_attestation,
) -> RunPodSuryaWorkerComposition:
    """Build every deployment port once, before the first RunPod invocation."""

    if not isinstance(settings, RunPodSuryaWorkerSettings):
        raise SuryaWorkerConfigurationError()
    try:
        dispatch = settings.dispatch_policy
        attestation = attestation_loader()
        if not isinstance(attestation, SuryaDeploymentAttestation):
            raise ValueError("invalid deployment attestation")
        attestation.assert_matches(producer=settings.producer, endpoint=settings.endpoint)
        storage = storage_factory(
            hard_max_bytes=settings.http.hard_max_bytes,
            connect_timeout_seconds=settings.http.connect_timeout_seconds,
            read_timeout_seconds=settings.http.read_timeout_seconds,
            write_timeout_seconds=settings.http.write_timeout_seconds,
            pool_timeout_seconds=settings.http.pool_timeout_seconds,
            proxy_url=settings.http.proxy_url,
        )
        inference = inference_factory(
            producer=settings.producer,
            endpoint=settings.endpoint,
            endpoint_policy=settings.endpoint_policy,
            attestation=attestation,
            max_png_bytes=min(
                settings.http.hard_max_bytes,
                dispatch.max_capability_bytes,
                dispatch.max_total_input_bytes,
            ),
            max_image_pixels=dispatch.max_total_rendered_pixels,
            max_regions_per_page=settings.output.max_regions_per_page,
        )
        initialize = getattr(inference, "initialize", None)
        if not callable(initialize):
            raise ValueError("Surya inference adapter has no readiness check")
        initialize()
        policy = RunPodSuryaExecutionPolicy(
            dispatch_policy=dispatch,
            producer=settings.producer,
            max_output_bytes=settings.output.max_output_bytes,
            max_terminal_output_bytes=settings.output.max_terminal_output_bytes,
            max_regions_per_page=settings.output.max_regions_per_page,
        )
        handler = handler_factory(storage=storage, inference=inference, policy=policy)
    except SuryaWorkerConfigurationError:
        raise
    except Exception:
        raise SuryaWorkerConfigurationError() from None
    return RunPodSuryaWorkerComposition(handler=handler, policy=policy)


def _parse_strict_json(raw: str) -> object:
    def reject_constant(_: str) -> object:
        raise ValueError("non-finite JSON number")

    def no_duplicate_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, nested in pairs:
            if key in value:
                raise ValueError("duplicate JSON key")
            value[key] = nested
        return value

    value = json.loads(
        raw,
        object_pairs_hook=no_duplicate_object,
        parse_constant=reject_constant,
    )
    _assert_json_depth(value, depth=0)
    if not isinstance(value, dict):
        raise ValueError("configuration JSON must be an object")
    return value


def _assert_json_depth(value: object, *, depth: int) -> None:
    if depth > _MAX_CONFIG_DEPTH:
        raise ValueError("configuration JSON nesting exceeds limit")
    if isinstance(value, dict):
        for key, nested in value.items():
            if not isinstance(key, str):
                raise ValueError("configuration JSON key is invalid")
            _assert_json_depth(nested, depth=depth + 1)
    elif isinstance(value, list):
        for nested in value:
            _assert_json_depth(nested, depth=depth + 1)
    elif isinstance(value, float) and not math.isfinite(value):
        raise ValueError("configuration JSON number is invalid")


def _reject_duplicate_scopes(scopes: tuple[AcceleratorStorageScope, ...]) -> None:
    canonical = [
        json.dumps(scope.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        for scope in scopes
    ]
    if len(set(canonical)) != len(canonical):
        raise ValueError("duplicate storage scope")


def _validate_dispatch_mime_types(policy: AcceleratorDispatchPolicy) -> None:
    # This worker needs exactly a render-manifest/result JSON surface and PNG
    # pages.  Broader MIME authority belongs in a separate, reviewed worker.
    if set(policy.allowed_mime_types) != {"application/json", "image/png"}:
        raise ValueError("unexpected storage MIME authority")


def _validate_cross_field_limits(
    *,
    dispatch_policy: AcceleratorDispatchPolicy,
    http: _HttpSettings,
    output: _OutputSettings,
) -> None:
    if dispatch_policy.max_ttl_seconds < dispatch_policy.max_execution_timeout_seconds:
        raise ValueError("TTL must cover execution")
    if dispatch_policy.max_capability_bytes > http.hard_max_bytes:
        raise ValueError("HTTP cap must cover capability cap")
    if output.max_output_bytes > dispatch_policy.max_capability_bytes:
        raise ValueError("output cap exceeds capability cap")
    if output.max_output_bytes > MAX_ARTIFACT_BYTES:
        raise ValueError("output cap exceeds portable artifact cap")
    if output.max_terminal_output_bytes > output.max_output_bytes:
        raise ValueError("terminal cap exceeds output cap")
    if dispatch_policy.max_page_count * output.max_regions_per_page > MAX_TOTAL_REGIONS:
        raise ValueError("page/region limits exceed portable artifact cap")


def _reject_placeholder_identity(producer: SuryaProducerIdentity) -> None:
    values = (
        producer.model_revision,
        producer.model_weights_sha256,
        producer.pipeline_revision,
        producer.config_sha256,
        producer.worker_image_digest.removeprefix("sha256:"),
    )
    if any(value and set(value) == {"0"} for value in values):
        raise ValueError("placeholder producer identity is not allowed")


def _endpoint_policy(origins: list[str]) -> SuryaEndpointAllowlistPolicy | None:
    if not origins:
        return None
    return SuryaEndpointAllowlistPolicy(allowed_origins=tuple(origins))
