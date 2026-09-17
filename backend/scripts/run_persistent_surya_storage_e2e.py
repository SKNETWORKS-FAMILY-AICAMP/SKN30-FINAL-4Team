#!/usr/bin/env python3
"""Run one accelerator-only persistent-Surya signed-Storage E2E check.

This is an operator tool, not a production worker and not a database commit
path.  It uploads a locally validated render artifact, asks the persistent
Surya API to process it, and reads its deterministic Storage result through
the acceptance coordinator.  Input and output objects are intentionally
retained for inspection; this command never deletes Storage objects or calls
the application database.

The prepared capability-bearing request lives only for this invocation.  The
credential-free reconciliation handle is used when this invocation sees an
uncertain provider outcome or reaches its polling deadline.  The operator
tool does not persist that handle or a lease across its own restart; durable
reattachment remains the production worker's responsibility.

Secrets are accepted only from files or the local Supabase runtime env.  The
command's output is a small safe outcome record and must not be used as a
place to inspect request payloads, signed URLs, or credentials.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import time
from typing import Any
from urllib.parse import urlsplit


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

# Ensure the checked-in Common IR contracts take precedence over any older
# host-installed package before importing a render/worker boundary.
from worker import vendor as _worker_vendor  # noqa: E402,F401

from common_ir_pipeline.pdf_fusion.render_manifest import (  # noqa: E402
    PdfRenderManifest,
    PdfRenderManifestError,
    validate_render_manifest_files,
)
from common_ir_pipeline.pdf_fusion.surya_layout_artifact import (  # noqa: E402
    SuryaLayoutArtifact,
)
from prereview_runpod_worker.surya_layout_worker.settings import (  # noqa: E402
    PREREVIEW_SURYA_WORKER_CONFIG_JSON,
    RunPodSuryaWorkerSettings,
    SuryaWorkerConfigurationError,
    load_worker_settings,
)
from worker.accelerator_coordinator import (  # noqa: E402
    CoordinatorDisposition,
    ExistingPdfSuryaCoordinator,
    ExistingPdfSuryaOutcome,
)
from worker.accelerator_request_factory import (  # noqa: E402
    preflight_surya_layout_request,
    prepare_surya_layout_request,
)
from worker.adapters.persistent_surya_http import PersistentSuryaHttpAdapter  # noqa: E402
from worker.adapters.supabase_accelerator_artifact import (  # noqa: E402
    SupabaseAcceleratorArtifactReader,
)
from worker.adapters.supabase_accelerator_input import (  # noqa: E402
    SupabaseAcceleratorInputUploadError,
    SupabaseAcceleratorInputUploader,
)
from worker.contracts.accelerator import (  # noqa: E402
    AcceleratorResourceCaps,
    AcceleratorStorageScope,
    SuryaLayoutReconciliationHandle,
    SuryaProducerIdentity,
    build_surya_layout_reconciliation_logical_compute_key,
    build_surya_layout_result_object_key,
)
from worker.signed_storage_capabilities import (  # noqa: E402
    SignedStorageCapabilityIssuerError,
    SupabaseSignedStorageCapabilityIssuer,
)


_MAX_CONFIG_BYTES = 64 * 1024
_MAX_SECRET_BYTES = 16 * 1024
_MAX_MANIFEST_BYTES = 8 * 1024 * 1024
_INPUT_PREFIX = "accelerator/input/"
_RESULT_PREFIX = "accelerator/surya-layout/"
_REQUEST_TTL_SECONDS = 1_200
_CAPABILITY_TTL_SECONDS = 1_800
_DISPATCH_SLACK_SECONDS = _CAPABILITY_TTL_SECONDS - _REQUEST_TTL_SECONDS
_RUNPOD_API_CLIENT_SCHEMA = "prereview.persistent-api-client/v1"
_TAILSCALE_DNS_NAME = re.compile(
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*\.ts\.net"
)
_SAFE_OUTCOME_REASONS = frozenset(
    {
        "completed",
        "timed_out",
        "cancelled",
        "content_failed",
        "infra_retryable",
        "fence_lost",
        "configuration_invalid",
        "local_artifact_invalid",
        "storage_configuration_invalid",
        "storage_upload_failed",
        "storage_capability_issue_failed",
        "credential_invalid",
        "operator_error",
    }
)


class PersistentSuryaE2EError(RuntimeError):
    """A fixed, non-secret operator failure category."""


@dataclass(frozen=True, slots=True)
class SafeE2EOutcome:
    """The only result this script is allowed to print."""

    disposition: str
    reason_code: str
    provider_state: str | None = None

    def as_json(self) -> str:
        record: dict[str, str] = {
            "mode": "accelerator_only",
            "operator": "persistent_surya_storage_e2e",
            "disposition": self.disposition,
            "reason_code": self.reason_code,
        }
        if self.provider_state is not None:
            record["provider_state"] = self.provider_state
        return json.dumps(record, sort_keys=True, separators=(",", ":"))


class AlwaysCurrentFence:
    """Operator-only fence: this command owns no database lease."""

    def is_current(self) -> bool:
        return True


class _ReconciliationOnlyAccelerator:
    """Fail closed if deterministic-result preflight ever tries dispatch I/O."""

    def submit(self, request: object) -> object:
        del request
        raise RuntimeError("preflight accelerator dispatch is forbidden")

    def get_status(self, external_job_id: str) -> object:
        del external_job_id
        raise RuntimeError("preflight accelerator polling is forbidden")

    def cancel(self, external_job_id: str) -> object:
        del external_job_id
        raise RuntimeError("preflight accelerator cancellation is forbidden")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Accelerator-only persistent-Surya signed-Storage E2E; it does not "
            "write application database state or delete Storage objects."
        )
    )
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--runpod-config-json", type=Path, required=True)
    parser.add_argument("--runpod-api-client-config", type=Path, required=True)
    parser.add_argument(
        "--runpod-api-base-url",
        help=(
            "Optional compatibility assertion; when supplied it must exactly "
            "match the origin pinned in --runpod-api-client-config."
        ),
    )
    parser.add_argument("--runpod-bearer-token-file", type=Path, required=True)
    parser.add_argument("--storage-bucket", required=True)
    storage = parser.add_mutually_exclusive_group(required=True)
    storage.add_argument("--supabase-runtime-env", type=Path)
    storage.add_argument("--supabase-url")
    parser.add_argument("--supabase-service-role-key-file", type=Path)
    parser.add_argument("--storage-timeout-seconds", type=float, default=30.0)
    parser.add_argument("--http-timeout-seconds", type=float, default=30.0)
    parser.add_argument("--poll-timeout-seconds", type=float)
    parser.add_argument("--poll-interval-seconds", type=float, default=2.0)
    return parser


def _read_regular_file(
    path: Path,
    *,
    max_bytes: int,
    require_private_secret_file: bool = False,
) -> bytes:
    """Read a bounded non-symlink file without exposing its content in errors."""

    try:
        link_info = os.lstat(path)
        if stat.S_ISLNK(link_info.st_mode) or not stat.S_ISREG(link_info.st_mode):
            raise OSError("not a regular file")
        if require_private_secret_file:
            current_uid = getattr(os, "geteuid", lambda: -1)()
            if (
                current_uid < 0
                or link_info.st_uid != current_uid
                or link_info.st_nlink != 1
                or stat.S_IMODE(link_info.st_mode) != 0o600
            ):
                raise OSError("unsafe secret file metadata")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as handle:
            before = os.fstat(handle.fileno())
            if (
                (link_info.st_dev, link_info.st_ino)
                != (before.st_dev, before.st_ino)
                or not stat.S_ISREG(before.st_mode)
                or before.st_size < 1
                or before.st_size > max_bytes
            ):
                raise OSError("unsafe file size")
            if require_private_secret_file:
                current_uid = getattr(os, "geteuid", lambda: -1)()
                if (
                    current_uid < 0
                    or before.st_uid != current_uid
                    or before.st_nlink != 1
                    or stat.S_IMODE(before.st_mode) != 0o600
                ):
                    raise OSError("unsafe secret file metadata")
            content = handle.read(max_bytes + 1)
            after = os.fstat(handle.fileno())
    except (OSError, ValueError):
        raise PersistentSuryaE2EError("operator_error") from None
    if len(content) < 1 or len(content) > max_bytes or (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ):
        raise PersistentSuryaE2EError("operator_error")
    if require_private_secret_file:
        current_uid = getattr(os, "geteuid", lambda: -1)()
        if (
            current_uid < 0
            or after.st_uid != current_uid
            or after.st_nlink != 1
            or stat.S_IMODE(after.st_mode) != 0o600
        ):
            raise PersistentSuryaE2EError("operator_error")
    return content


def _read_text(
    path: Path,
    *,
    max_bytes: int,
    failure: str,
    require_private_secret_file: bool = False,
) -> str:
    try:
        return _read_regular_file(
            path,
            max_bytes=max_bytes,
            require_private_secret_file=require_private_secret_file,
        ).decode("utf-8")
    except (PersistentSuryaE2EError, UnicodeDecodeError):
        raise PersistentSuryaE2EError(failure) from None


def _read_bearer_token(path: Path) -> str:
    token = _read_text(
        path,
        max_bytes=_MAX_SECRET_BYTES,
        failure="credential_invalid",
        require_private_secret_file=True,
    ).strip()
    if not token or len(token) > 4_096 or any(ord(char) < 0x20 or ord(char) == 0x7F for char in token):
        raise PersistentSuryaE2EError("credential_invalid")
    return token


def _strict_json_object(raw: str, *, failure: str) -> Mapping[str, Any]:
    def no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate key")
            value[key] = item
        return value

    try:
        value = json.loads(raw, object_pairs_hook=no_duplicates, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (TypeError, ValueError, json.JSONDecodeError, RecursionError):
        raise PersistentSuryaE2EError(failure) from None
    if not isinstance(value, Mapping):
        raise PersistentSuryaE2EError(failure)
    return value


def _load_render_manifest(artifact_root: Path) -> PdfRenderManifest:
    try:
        root = artifact_root.resolve(strict=True)
        if not root.is_dir() or artifact_root.is_symlink():
            raise OSError("unsafe artifact root")
        raw = _read_text(root / "render_manifest.json", max_bytes=_MAX_MANIFEST_BYTES, failure="local_artifact_invalid")
        manifest = PdfRenderManifest.from_dict(_strict_json_object(raw, failure="local_artifact_invalid"))
        return validate_render_manifest_files(manifest, artifact_root=root)
    except (OSError, PdfRenderManifestError, ValueError, PersistentSuryaE2EError):
        raise PersistentSuryaE2EError("local_artifact_invalid") from None


def _load_runpod_settings(path: Path) -> RunPodSuryaWorkerSettings:
    raw = _read_text(
        path,
        max_bytes=_MAX_CONFIG_BYTES,
        failure="configuration_invalid",
        require_private_secret_file=True,
    )
    # Parse first so duplicate keys cannot be interpreted differently by a
    # later component, then use the deployment's canonical settings loader.
    _strict_json_object(raw, failure="configuration_invalid")
    try:
        return load_worker_settings({PREREVIEW_SURYA_WORKER_CONFIG_JSON: raw})
    except SuryaWorkerConfigurationError:
        raise PersistentSuryaE2EError("configuration_invalid") from None


def _load_runpod_api_origin(
    path: Path,
    *,
    asserted_origin: str | None,
) -> str:
    """Load the exact bearer destination from protected operator state."""

    raw = _read_text(
        path,
        max_bytes=_MAX_CONFIG_BYTES,
        failure="configuration_invalid",
        require_private_secret_file=True,
    )
    payload = _strict_json_object(raw, failure="configuration_invalid")
    if set(payload) != {"schema_version", "origin"}:
        raise PersistentSuryaE2EError("configuration_invalid")
    if payload.get("schema_version") != _RUNPOD_API_CLIENT_SCHEMA:
        raise PersistentSuryaE2EError("configuration_invalid")
    origin = _canonical_runpod_api_origin(payload.get("origin"))
    if asserted_origin is not None and asserted_origin != origin:
        raise PersistentSuryaE2EError("configuration_invalid")
    return origin


def _canonical_runpod_api_origin(value: object) -> str:
    """Accept one canonical Tailscale Serve HTTPS origin, never a public URL."""

    if not isinstance(value, str) or not value:
        raise PersistentSuryaE2EError("configuration_invalid")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise PersistentSuryaE2EError("configuration_invalid") from None
    hostname = parsed.hostname
    if (
        parsed.scheme != "https"
        or not isinstance(hostname, str)
        or _TAILSCALE_DNS_NAME.fullmatch(hostname) is None
        or hostname != hostname.lower()
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or port is not None
    ):
        raise PersistentSuryaE2EError("configuration_invalid")
    canonical = f"https://{hostname}"
    if value != canonical:
        raise PersistentSuryaE2EError("configuration_invalid")
    return canonical


def _load_supabase_settings(args: argparse.Namespace) -> tuple[str, str]:
    if args.supabase_runtime_env is not None:
        if args.supabase_service_role_key_file is not None:
            raise PersistentSuryaE2EError("storage_configuration_invalid")
        try:
            from scripts.local_supabase_env import LocalSupabaseEnvError, load_local_supabase_settings

            values = load_local_supabase_settings(
                args.supabase_runtime_env,
                require_private_secret_file=True,
            )
            url = values["SUPABASE_URL"]
            key = values["SUPABASE_SERVICE_ROLE_KEY"]
        except (ImportError, KeyError, LocalSupabaseEnvError, OSError, ValueError):
            raise PersistentSuryaE2EError("storage_configuration_invalid") from None
        return _validate_internal_supabase_origin(url), key
    if not isinstance(args.supabase_url, str) or args.supabase_service_role_key_file is None:
        raise PersistentSuryaE2EError("storage_configuration_invalid")
    return _validate_internal_supabase_origin(args.supabase_url), _read_text(
        args.supabase_service_role_key_file,
        max_bytes=_MAX_SECRET_BYTES,
        failure="credential_invalid",
        require_private_secret_file=True,
    ).strip()


def _validate_internal_supabase_origin(value: str) -> str:
    """Keep service-role traffic on the canonical local Supabase gateway."""

    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError):
        raise PersistentSuryaE2EError("storage_configuration_invalid") from None
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or port is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise PersistentSuryaE2EError("storage_configuration_invalid")
    canonical = f"http://127.0.0.1:{port}"
    if value != canonical:
        raise PersistentSuryaE2EError("storage_configuration_invalid")
    return canonical


def _required_scope(
    scopes: Sequence[AcceleratorStorageScope],
    *,
    method: str,
    bucket: str,
    prefix: str,
) -> AcceleratorStorageScope:
    matches = [
        scope
        for scope in scopes
        if scope.method == method and scope.bucket == bucket and scope.object_key_prefix == prefix
    ]
    if len(matches) != 1:
        raise PersistentSuryaE2EError("storage_configuration_invalid")
    return matches[0]


def _public_storage_origin(settings: RunPodSuryaWorkerSettings, bucket: str) -> str:
    read_scope = _required_scope(settings.dispatch_policy.allowed_scopes, method="GET", bucket=bucket, prefix=_INPUT_PREFIX)
    write_scope = _required_scope(settings.dispatch_policy.allowed_scopes, method="PUT", bucket=bucket, prefix=_RESULT_PREFIX)
    if read_scope.origin != write_scope.origin:
        raise PersistentSuryaE2EError("storage_configuration_invalid")
    return read_scope.origin


def _resource_caps(
    settings: RunPodSuryaWorkerSettings,
    manifest: PdfRenderManifest,
) -> AcceleratorResourceCaps:
    """Build least-authority caps from this render, below deployment ceilings."""

    policy = settings.dispatch_policy
    manifest_size = len(manifest.canonical_json())
    total_input_bytes = manifest_size + sum(
        page.image_size_bytes for page in manifest.pages
    )
    total_rendered_pixels = sum(
        page.coordinate_manifest.rendered_width_px
        * page.coordinate_manifest.rendered_height_px
        for page in manifest.pages
    )
    # Both limits are deployment invariants.  A lower policy is rejected so
    # every accepted policy retains the full 600-second dispatch slack.
    if policy.max_ttl_seconds < _CAPABILITY_TTL_SECONDS:
        raise PersistentSuryaE2EError("configuration_invalid")
    request_ttl_seconds = _REQUEST_TTL_SECONDS
    if policy.max_execution_timeout_seconds > request_ttl_seconds:
        raise PersistentSuryaE2EError("configuration_invalid")
    return AcceleratorResourceCaps(
        max_page_count=len(manifest.pages),
        max_total_input_bytes=total_input_bytes,
        max_total_rendered_pixels=total_rendered_pixels,
        max_output_bytes=settings.output.max_output_bytes,
        execution_timeout_seconds=policy.max_execution_timeout_seconds,
        ttl_seconds=request_ttl_seconds,
    )


def _capability_ttl_seconds(settings: RunPodSuryaWorkerSettings) -> int:
    """Return the capability lifetime, distinct from the request lifetime."""

    if settings.dispatch_policy.max_ttl_seconds < _CAPABILITY_TTL_SECONDS:
        raise PersistentSuryaE2EError("configuration_invalid")
    return _CAPABILITY_TTL_SECONDS


def _positive_finite(value: object, *, maximum: float, failure: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or not 0 < float(value) <= maximum:
        raise PersistentSuryaE2EError(failure)
    return float(value)


def _safe_coordinator_outcome(outcome: ExistingPdfSuryaOutcome) -> SafeE2EOutcome:
    # Provider reason_code is deliberately not an operator output channel.
    # Collapse every coordinator result into this closed local category set.
    reason = {
        CoordinatorDisposition.SUCCEEDED: "completed",
        CoordinatorDisposition.CANCELLED: "cancelled",
        CoordinatorDisposition.CONTENT_FAILED: "content_failed",
        CoordinatorDisposition.INFRA_RETRYABLE: "infra_retryable",
        CoordinatorDisposition.FENCE_LOST: "fence_lost",
        CoordinatorDisposition.PENDING: "timed_out",
    }.get(outcome.disposition, "operator_error")
    return SafeE2EOutcome(
        disposition=outcome.disposition.value,
        reason_code=reason,
        provider_state=outcome.provider_state.value if outcome.provider_state is not None else None,
    )


def _reconcile_existing_deterministic_result(
    *,
    manifest: PdfRenderManifest,
    producer: SuryaProducerIdentity,
    result_bucket: str,
    max_output_bytes: int,
    artifact_reader: object,
) -> ExistingPdfSuryaOutcome:
    """Accept an immutable prior result before any signed capability exists."""

    logical_compute_key = build_surya_layout_reconciliation_logical_compute_key(
        manifest,
        producer,
    )
    handle = SuryaLayoutReconciliationHandle(
        trusted_render_manifest=manifest,
        producer=producer,
        logical_compute_key=logical_compute_key,
        result_bucket=result_bucket,
        result_object_key=build_surya_layout_result_object_key(
            logical_compute_key
        ),
        max_output_bytes=max_output_bytes,
    )
    coordinator = ExistingPdfSuryaCoordinator(
        accelerator=_ReconciliationOnlyAccelerator(),  # type: ignore[arg-type]
        artifact_reader=artifact_reader,  # type: ignore[arg-type]
        fence=AlwaysCurrentFence(),
    )
    return coordinator.reconcile_result_artifact(
        handle,
        external_job_id=logical_compute_key,
    )


def run_e2e(
    args: argparse.Namespace,
    *,
    storage_factory: Callable[..., object] = SupabaseAcceleratorInputUploader,
    issuer_factory: Callable[..., object] = SupabaseSignedStorageCapabilityIssuer,
    accelerator_factory: Callable[..., object] = PersistentSuryaHttpAdapter,
    artifact_reader_factory: Callable[..., object] = SupabaseAcceleratorArtifactReader,
    coordinator_factory: Callable[..., ExistingPdfSuryaCoordinator] = ExistingPdfSuryaCoordinator,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    accepted_artifact: Callable[[SuryaLayoutArtifact], None] | None = None,
) -> SafeE2EOutcome:
    """Execute the bounded operator flow; injectable ports keep tests offline."""

    manifest = _load_render_manifest(args.artifact_root)
    settings = _load_runpod_settings(args.runpod_config_json)
    caps = _resource_caps(settings, manifest)
    capability_ttl_seconds = _capability_ttl_seconds(settings)
    try:
        preflight_surya_layout_request(
            render_manifest=manifest,
            input_bucket=args.storage_bucket,
            resource_caps=caps,
            read_capability_ttl_seconds=capability_ttl_seconds,
            dispatch_policy=settings.dispatch_policy,
        )
    except ValueError:
        raise PersistentSuryaE2EError("configuration_invalid") from None
    storage_timeout = _positive_finite(args.storage_timeout_seconds, maximum=3_600, failure="storage_configuration_invalid")
    http_timeout = _positive_finite(args.http_timeout_seconds, maximum=3_600, failure="configuration_invalid")
    poll_interval = _positive_finite(args.poll_interval_seconds, maximum=60, failure="configuration_invalid")
    poll_timeout_value = caps.execution_timeout_seconds if args.poll_timeout_seconds is None else args.poll_timeout_seconds
    poll_timeout = _positive_finite(poll_timeout_value, maximum=caps.execution_timeout_seconds, failure="configuration_invalid")
    supabase_url, service_role_key = _load_supabase_settings(args)

    try:
        reader = artifact_reader_factory(
            supabase_url=supabase_url,
            service_role_key=service_role_key,
            timeout_seconds=storage_timeout,
        )
    except (TypeError, ValueError):
        raise PersistentSuryaE2EError("storage_configuration_invalid") from None

    try:
        existing = _reconcile_existing_deterministic_result(
            manifest=manifest,
            producer=settings.producer,
            result_bucket=args.storage_bucket,
            max_output_bytes=caps.max_output_bytes,
            artifact_reader=reader,
        )
    except (TypeError, ValueError):
        raise PersistentSuryaE2EError("local_artifact_invalid") from None
    if existing.disposition is CoordinatorDisposition.SUCCEEDED:
        if existing.artifact is None:
            raise PersistentSuryaE2EError("operator_error")
        if accepted_artifact is not None:
            accepted_artifact(existing.artifact)
        return _safe_coordinator_outcome(existing)
    if not (
        existing.disposition is CoordinatorDisposition.PENDING
        and existing.reason_code == "artifact_not_found"
    ):
        return _safe_coordinator_outcome(existing)

    public_origin = _public_storage_origin(settings, args.storage_bucket)
    runpod_api_origin = _load_runpod_api_origin(
        args.runpod_api_client_config,
        asserted_origin=args.runpod_api_base_url,
    )
    bearer_token = _read_bearer_token(args.runpod_bearer_token_file)
    try:
        storage = storage_factory(
            supabase_url=supabase_url,
            service_role_key=service_role_key,
            allowed_bucket=args.storage_bucket,
            object_key_prefix=_INPUT_PREFIX,
            max_object_bytes=min(
                caps.max_total_input_bytes,
                settings.dispatch_policy.max_capability_bytes,
            ),
            timeout_seconds=storage_timeout,
        )
        issuer = issuer_factory(
            supabase_url=supabase_url,
            service_role_key=service_role_key,
            public_storage_origin=public_origin,
            max_capability_ttl_seconds=capability_ttl_seconds,
            timeout_seconds=storage_timeout,
        )
        accelerator = accelerator_factory(
            base_url=runpod_api_origin,
            bearer_token=bearer_token,
            dispatch_policy=settings.dispatch_policy,
            timeout_seconds=http_timeout,
        )
    except (TypeError, ValueError):
        raise PersistentSuryaE2EError("storage_configuration_invalid") from None
    try:
        prepared = prepare_surya_layout_request(
            render_manifest=manifest,
            artifact_root=args.artifact_root,
            input_bucket=args.storage_bucket,
            uploader=storage,  # type: ignore[arg-type]
            issuer=issuer,  # type: ignore[arg-type]
            producer=settings.producer,
            resource_caps=caps,
            read_capability_ttl_seconds=capability_ttl_seconds,
            dispatch_policy=settings.dispatch_policy,
        )
        coordinator = coordinator_factory(
            accelerator=accelerator,  # type: ignore[arg-type]
            artifact_reader=reader,  # type: ignore[arg-type]
            fence=AlwaysCurrentFence(),
        )
    except SupabaseAcceleratorInputUploadError:
        raise PersistentSuryaE2EError("storage_upload_failed") from None
    except SignedStorageCapabilityIssuerError:
        raise PersistentSuryaE2EError("storage_capability_issue_failed") from None
    except (TypeError, ValueError, PdfRenderManifestError):
        raise PersistentSuryaE2EError("local_artifact_invalid") from None

    deadline = monotonic() + poll_timeout
    prior_job_id: str | None = None
    while True:
        # Keep this exact prepared request in memory for submit/poll.  The
        # separate credential-free handle is safe to persist in the eventual
        # production queue and remains usable after PUT-capability expiry.
        outcome = coordinator.advance(
            prepared.request,
            prepared.trusted_render_manifest,
            prior_external_job_id=prior_job_id,
        )
        if outcome.disposition is not CoordinatorDisposition.PENDING:
            if outcome.disposition is CoordinatorDisposition.INFRA_RETRYABLE:
                # Persistent API job IDs are the logical key.  If the initial
                # submit response was lost before returning an ID, this lets
                # the same invocation poll/reconcile the idempotent job until
                # its deadline rather than submitting a second request.
                prior_job_id = (
                    outcome.external_job_id
                    or prior_job_id
                    or prepared.request.logical_compute_key
                )
                reconciled = coordinator.reconcile_result_artifact(
                    prepared.reconciliation_handle,
                    external_job_id=prior_job_id,
                )
                if reconciled.disposition is not CoordinatorDisposition.PENDING:
                    if reconciled.disposition is not CoordinatorDisposition.INFRA_RETRYABLE:
                        if (
                            accepted_artifact is not None
                            and reconciled.disposition
                            is CoordinatorDisposition.SUCCEEDED
                        ):
                            if reconciled.artifact is None:
                                raise PersistentSuryaE2EError("operator_error")
                            accepted_artifact(reconciled.artifact)
                        return _safe_coordinator_outcome(reconciled)
                    prior_job_id = reconciled.external_job_id or prior_job_id
                # Keep polling/reconciling until the bounded deadline.  The
                # provider's reason remains internal and is never printed.
            else:
                if (
                    accepted_artifact is not None
                    and outcome.disposition is CoordinatorDisposition.SUCCEEDED
                ):
                    if outcome.artifact is None:
                        raise PersistentSuryaE2EError("operator_error")
                    accepted_artifact(outcome.artifact)
                return _safe_coordinator_outcome(outcome)
        else:
            prior_job_id = outcome.external_job_id
        remaining = deadline - monotonic()
        if remaining <= 0:
            if prior_job_id is not None:
                reconciled = coordinator.reconcile_result_artifact(
                    prepared.reconciliation_handle,
                    external_job_id=prior_job_id,
                )
                if reconciled.disposition is not CoordinatorDisposition.PENDING:
                    if (
                        accepted_artifact is not None
                        and reconciled.disposition is CoordinatorDisposition.SUCCEEDED
                    ):
                        if reconciled.artifact is None:
                            raise PersistentSuryaE2EError("operator_error")
                        accepted_artifact(reconciled.artifact)
                    return _safe_coordinator_outcome(reconciled)
            return SafeE2EOutcome(disposition="timed_out", reason_code="timed_out")
        sleep(min(poll_interval, remaining))


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        outcome = run_e2e(args)
    except PersistentSuryaE2EError as error:
        reason = str(error)
        if reason not in _SAFE_OUTCOME_REASONS:
            reason = "operator_error"
        outcome = SafeE2EOutcome(disposition="operator_failed", reason_code=reason)
    except Exception:
        # Adapter and provider exceptions must never become a traceback that
        # might include a URL, capability, or an implementation diagnostic.
        outcome = SafeE2EOutcome(disposition="operator_failed", reason_code="operator_error")
    print(outcome.as_json())
    return 0 if outcome.disposition == CoordinatorDisposition.SUCCEEDED.value else 1


if __name__ == "__main__":  # pragma: no cover - exercised through main()
    raise SystemExit(main())
