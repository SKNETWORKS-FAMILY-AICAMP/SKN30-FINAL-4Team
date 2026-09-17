#!/usr/bin/env python3
"""Build and optionally ingest one Existing PDF through the reviewed pipeline.

This operator command composes existing, independently reviewed boundaries.  It
does not implement a second Storage or RunPod client.  Every durable stage is
published without overwrite, and ``--resume`` only accepts artifacts that can
be revalidated against the original invocation.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
import inspect
import io
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import sys
import tempfile
from typing import Any


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

# Put the repository-pinned Common IR package ahead of any stale installed
# distribution before importing its contracts.  Direct CLI execution does not
# inherit the worker entrypoint's vendor bootstrap.
from worker import vendor as _worker_vendor  # noqa: E402,F401

from common_ir_pipeline.pdf_fusion.render_manifest import (  # noqa: E402
    PdfRenderManifest,
    validate_render_manifest_files,
)
from common_ir_pipeline.pdf_fusion.surya_layout_artifact import (  # noqa: E402
    SuryaLayoutArtifact,
    SuryaProducerIdentity as PortableSuryaProducerIdentity,
    validate_surya_layout_artifact,
)
from scripts.existing_kb_pack import validate_notice_directory  # noqa: E402
from scripts.ingest_existing_profile import (  # noqa: E402
    KnowledgeBase,
    SupabaseStorage,
    ingest_record,
    run_transaction,
)
from scripts.local_supabase_env import (  # noqa: E402
    LocalSupabaseEnvError,
    _read_private_secret_file,
    load_local_supabase_settings,
)
from scripts import run_persistent_surya_storage_e2e as surya_e2e  # noqa: E402
from worker import announcement_profiles  # noqa: E402
from worker.adapters.openai_llm_client import OpenAILLMClient  # noqa: E402
from worker.config import MissingConfigError, OpenAIConfig  # noqa: E402
from worker.contracts.accelerator import (  # noqa: E402
    SuryaProducerIdentity,
    build_surya_layout_reconciliation_logical_compute_key,
)


PIPELINE_SCHEMA = "prereview.existing-pdf-one-shot/v1"
OPERATOR_CONTRACT_VERSION = PIPELINE_SCHEMA
OPERATOR_CODE_VERSION = "existing-pdf-one-shot/2026-09-17.1"
PROFILE_MODEL_PROFILE = "existing_profile"
PINNED_OPENAI_MODEL_ID = "gpt-5.6-terra"
COMPOSITE_CANDIDATE_MODE = "shadow"
NATIVE_EXACT_CANDIDATE_MODE = "lines+continuations"
SOURCE_SELECTION_ATTEMPTS = 2
REASONING_EFFORT = "medium"
TASK_COMPLETION_TOKEN_CAPS = {
    announcement_profiles.SECTION_SCOPE_TASK: 4096,
    announcement_profiles.BLOCK_ROUTER_TASK: 16384,
    announcement_profiles.SOURCE_SELECTION_TASK: 32768,
    announcement_profiles.ANCHOR_CORRECTION_TASK: 4096,
}
MAX_METADATA_BYTES = 1024 * 1024
MAX_JSON_BYTES = 256 * 1024 * 1024


class ExistingPdfOneShotError(RuntimeError):
    """A fixed operator-safe pipeline failure."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


class _TaskDispatchLlm:
    """Dispatch only the reviewed Existing tasks to their bounded clients."""

    def __init__(self, delegates: Mapping[str, Any]) -> None:
        if set(delegates) != set(TASK_COMPLETION_TOKEN_CAPS):
            raise ValueError("task dispatcher is incomplete")
        self._delegates = dict(delegates)

    async def generate_structured(self, **kwargs: Any) -> Any:
        task_name = kwargs.get("task_name")
        if not isinstance(task_name, str) or task_name not in self._delegates:
            raise ValueError("structured task is not allowed")
        return await self._delegates[task_name].generate_structured(**kwargs)


@dataclass(frozen=True, slots=True)
class PipelineArtifacts:
    notice_id: str
    output_root: Path
    package_notice_dir: Path
    ingestion_record: Path
    ingested: bool


@dataclass(frozen=True, slots=True)
class PipelinePorts:
    render: Callable[[Path, Path, float], None]
    native_preflight: Callable[[str, Path, Path, float], None]
    load_producer: Callable[[Path], SuryaProducerIdentity]
    dispatch: Callable[[argparse.Namespace, Path], SuryaLayoutArtifact]
    replay: Callable[[str, Path, Path, Path, Path, float], None]
    structure: Callable[[dict[str, Any], OpenAIConfig], tuple[dict[str, Any], dict[str, Any]]]
    validate_package: Callable[[Path], None]
    ingest: Callable[[Path, Mapping[str, str]], Mapping[str, Any]]


def _sha256_path(path: Path) -> str:
    digest = sha256()
    try:
        link = os.lstat(path)
        if stat.S_ISLNK(link.st_mode) or not stat.S_ISREG(link.st_mode):
            raise OSError
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
            after = os.fstat(stream.fileno())
    except OSError:
        raise ExistingPdfOneShotError("input_invalid") from None
    identity = lambda value: (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )
    if before.st_size < 1 or identity(link) != identity(before) or identity(before) != identity(after):
        raise ExistingPdfOneShotError("input_invalid")
    return digest.hexdigest()


def _pdf_header(path: Path) -> bytes:
    """Read only the PDF signature without materializing a large input file."""

    try:
        link = os.lstat(path)
        if stat.S_ISLNK(link.st_mode) or not stat.S_ISREG(link.st_mode):
            raise OSError
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            prefix = stream.read(5)
            after = os.fstat(stream.fileno())
    except OSError:
        raise ExistingPdfOneShotError("input_invalid") from None
    identity = lambda value: (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )
    if (
        before.st_size < 5
        or identity(link) != identity(before)
        or identity(before) != identity(after)
    ):
        raise ExistingPdfOneShotError("input_invalid")
    return prefix


def _stable_regular(path: Path, *, max_bytes: int, private: bool = False) -> bytes:
    try:
        before_link = os.lstat(path)
        if stat.S_ISLNK(before_link.st_mode) or not stat.S_ISREG(before_link.st_mode):
            raise OSError
        if private and (
            before_link.st_uid != os.geteuid()
            or before_link.st_nlink != 1
            or stat.S_IMODE(before_link.st_mode) != 0o600
        ):
            raise OSError
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not 0 < before.st_size <= max_bytes:
                raise OSError
            raw = stream.read(max_bytes + 1)
            after = os.fstat(stream.fileno())
    except OSError:
        raise ExistingPdfOneShotError(
            "secret_file_invalid" if private else "input_invalid"
        ) from None
    identity = lambda value: (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )
    if (
        len(raw) != before.st_size
        or identity(before_link) != identity(before)
        or identity(before) != identity(after)
    ):
        raise ExistingPdfOneShotError(
            "secret_file_invalid" if private else "input_invalid"
        )
    if private and (
        after.st_uid != os.geteuid()
        or after.st_nlink != 1
        or stat.S_IMODE(after.st_mode) != 0o600
    ):
        raise ExistingPdfOneShotError("secret_file_invalid")
    return raw


def _json_object(path: Path, *, max_bytes: int = MAX_JSON_BYTES) -> dict[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    try:
        value = json.loads(
            _stable_regular(path, max_bytes=max_bytes).decode("utf-8"),
            object_pairs_hook=unique,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
        )
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
        raise ExistingPdfOneShotError("artifact_invalid") from None
    if not isinstance(value, dict):
        raise ExistingPdfOneShotError("artifact_invalid")
    return value


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8") + b"\n"
    except (TypeError, ValueError):
        raise ExistingPdfOneShotError("artifact_invalid") from None


def _write_exclusive(path: Path, content: bytes, *, mode: int = 0o600) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor: int | None = None
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            mode,
        )
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), mode)
    except OSError:
        raise ExistingPdfOneShotError("output_conflict") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _copy_exclusive(source: Path, destination: Path) -> None:
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    source_descriptor: int | None = None
    destination_descriptor: int | None = None
    try:
        source_link = os.lstat(source)
        if stat.S_ISLNK(source_link.st_mode) or not stat.S_ISREG(source_link.st_mode):
            raise OSError
        source_descriptor = os.open(
            source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        )
        destination_descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        source_before = os.fstat(source_descriptor)
        copied = 0
        while True:
            chunk = os.read(source_descriptor, 1024 * 1024)
            if not chunk:
                break
            view = memoryview(chunk)
            while view:
                written = os.write(destination_descriptor, view)
                if written < 1:
                    raise OSError
                copied += written
                view = view[written:]
        source_after = os.fstat(source_descriptor)
        os.fsync(destination_descriptor)
        os.fchmod(destination_descriptor, 0o600)
    except OSError:
        if destination_descriptor is not None:
            try:
                os.unlink(destination)
            except OSError:
                pass
        raise ExistingPdfOneShotError("output_conflict") from None
    finally:
        if destination_descriptor is not None:
            os.close(destination_descriptor)
        if source_descriptor is not None:
            os.close(source_descriptor)
    identity = lambda value: (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )
    if (
        identity(source_link) != identity(source_before)
        or identity(source_before) != identity(source_after)
        or copied != source_before.st_size
    ):
        try:
            destination.unlink()
        except OSError:
            pass
        raise ExistingPdfOneShotError("input_invalid")


def _load_backend_openai_config(path: Path) -> OpenAIConfig:
    try:
        from dotenv import dotenv_values

        raw = _read_private_secret_file(path, max_bytes=64 * 1024).decode("utf-8")
        values = {
            str(key): str(value)
            for key, value in dotenv_values(stream=io.StringIO(raw)).items()
            if key is not None and value is not None
        }
        provider = values.get("PREREVIEW_LLM_PROVIDER", "openai").strip().lower()
        if provider != "openai":
            raise ValueError
        if (
            values.get("OPENAI_LOG", "").strip().casefold() == "debug"
            or os.environ.get("OPENAI_LOG", "").strip().casefold() == "debug"
        ):
            raise ValueError
        config = OpenAIConfig.from_env(values)
        model = config.request_profile_model or config.llm_model
        if model != PINNED_OPENAI_MODEL_ID:
            raise ValueError
        return config
    except (
        ImportError,
        UnicodeDecodeError,
        ValueError,
        LocalSupabaseEnvError,
        MissingConfigError,
    ):
        raise ExistingPdfOneShotError("openai_configuration_invalid") from None


def _metadata(path: Path) -> tuple[dict[str, Any], bytes]:
    raw = _stable_regular(path, max_bytes=MAX_METADATA_BYTES)
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = item
        return result
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=unique,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
        )
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
        raise ExistingPdfOneShotError("metadata_invalid") from None
    if not isinstance(value, dict):
        raise ExistingPdfOneShotError("metadata_invalid")
    notice_id = value.get("notice_id")
    if (
        not isinstance(notice_id, str)
        or not notice_id.startswith("PBLN_")
        or not notice_id[5:].isdigit()
        or value.get("pblanc_id") != notice_id
    ):
        raise ExistingPdfOneShotError("metadata_invalid")
    for name in (
        "title",
        "apply_period",
        "ministry",
        "executing_agency",
        "registered_at",
        "detail_url",
    ):
        if not isinstance(value.get(name), str) or not value[name].strip():
            raise ExistingPdfOneShotError("metadata_invalid")
    return value, raw


def _invocation(
    *,
    notice_id: str,
    pdf: Path,
    metadata_raw: bytes,
    runpod_config: Path,
    api_client_config: Path,
    storage_bucket: str,
    model: str,
    surya_producer: SuryaProducerIdentity,
    openai_timeout_seconds: float,
    execution_policy: Mapping[str, float],
    generated_at: str,
) -> dict[str, Any]:
    return {
        "schema_version": PIPELINE_SCHEMA,
        "operator_contract_version": OPERATOR_CONTRACT_VERSION,
        "operator_code_version": OPERATOR_CODE_VERSION,
        "notice_id": notice_id,
        "source_pdf_sha256": _sha256_path(pdf),
        "metadata_sha256": sha256(metadata_raw).hexdigest(),
        "runpod_config_sha256": _sha256_path(runpod_config),
        "runpod_api_client_config_sha256": _sha256_path(api_client_config),
        "storage_bucket": storage_bucket,
        "openai_model": model,
        "surya_producer": surya_producer.model_dump(mode="json"),
        "structuring_policy": {
            "composite_candidate_mode": COMPOSITE_CANDIDATE_MODE,
            "native_exact_candidate_mode": NATIVE_EXACT_CANDIDATE_MODE,
            "source_selection_attempts": SOURCE_SELECTION_ATTEMPTS,
            "reasoning_effort": REASONING_EFFORT,
            "task_completion_token_caps": dict(TASK_COMPLETION_TOKEN_CAPS),
            "timeout_seconds": openai_timeout_seconds,
            "prompt_bundle_version": announcement_profiles.PROMPT_BUNDLE_VERSION,
        },
        "native_preflight_policy": {
            "mode": "native_only_replay",
            "capture_reused_by_fusion": False,
        },
        "execution_policy": dict(execution_policy),
        "generated_at": generated_at,
    }


def _prepare_root(
    output_root: Path,
    invocation: dict[str, Any],
    *,
    resume: bool,
) -> None:
    marker = output_root / "invocation.json"
    if output_root.exists() or output_root.is_symlink():
        try:
            root_info = os.lstat(output_root)
        except OSError:
            raise ExistingPdfOneShotError("output_conflict") from None
        if (
            not resume
            or stat.S_ISLNK(root_info.st_mode)
            or not stat.S_ISDIR(root_info.st_mode)
            or root_info.st_uid != os.geteuid()
            or root_info.st_nlink < 1
            or stat.S_IMODE(root_info.st_mode) != 0o700
        ):
            raise ExistingPdfOneShotError("output_conflict")
        if _json_object(marker, max_bytes=64 * 1024) != invocation:
            raise ExistingPdfOneShotError("resume_mismatch")
        return
    if resume:
        raise ExistingPdfOneShotError("resume_missing")
    parent = output_root.parent
    if parent.is_symlink() or not parent.is_dir():
        raise ExistingPdfOneShotError("output_parent_invalid")
    try:
        output_root.mkdir(mode=0o700)
        os.chmod(output_root, 0o700)
    except OSError:
        raise ExistingPdfOneShotError("output_conflict") from None
    _write_exclusive(marker, _canonical_json(invocation))


def _load_render(root: Path) -> PdfRenderManifest:
    try:
        manifest = PdfRenderManifest.from_dict(_json_object(root / "render_manifest.json"))
        return validate_render_manifest_files(manifest, artifact_root=root)
    except Exception:
        raise ExistingPdfOneShotError("render_invalid") from None


def _load_surya(
    path: Path,
    manifest: PdfRenderManifest,
    producer: SuryaProducerIdentity,
) -> SuryaLayoutArtifact:
    try:
        artifact = SuryaLayoutArtifact.from_dict(_json_object(path))
        portable_producer = PortableSuryaProducerIdentity.from_dict(
            producer.model_dump(mode="json")
        )
        logical_compute_key = build_surya_layout_reconciliation_logical_compute_key(
            manifest,
            producer,
        )
        return validate_surya_layout_artifact(
            artifact,
            render_manifest=manifest,
            expected_logical_compute_key=logical_compute_key,
            expected_producer=portable_producer,
            expected_requested_pages=tuple(range(1, manifest.page_count + 1)),
        )
    except Exception:
        raise ExistingPdfOneShotError("surya_artifact_invalid") from None


def _validate_replay(
    root: Path,
    *,
    notice_id: str,
    source_sha256: str,
    render_manifest_path: Path | None = None,
    surya_artifact_path: Path | None = None,
) -> None:
    if (render_manifest_path is None) != (surya_artifact_path is None):
        raise ExistingPdfOneShotError("replay_invalid")
    fusion = render_manifest_path is not None
    expected_names = {
        "source.pdf",
        "native.json",
        "common_ir.json",
        "manifest.json",
    }
    if fusion:
        expected_names.update(
            {"render_manifest.json", "surya_layout_artifact.json"}
        )
    try:
        if root.is_symlink() or not root.is_dir():
            raise ExistingPdfOneShotError("replay_invalid")
        entries = tuple(root.iterdir())
        actual_names = {path.name for path in entries}
        if actual_names != expected_names or any(
            path.is_symlink() or not path.is_file() for path in entries
        ):
            raise ExistingPdfOneShotError("replay_invalid")
        manifest = _json_object(root / "manifest.json")
        if (
            manifest.get("schema_version") != "existing_pdf_native_replay/v1"
            or manifest.get("notice_id") != notice_id
            or manifest.get("whole_document") is not True
        ):
            raise ExistingPdfOneShotError("replay_invalid")
        artifacts = manifest.get("artifacts")
        if not isinstance(artifacts, dict):
            raise ExistingPdfOneShotError("replay_invalid")
        expected_artifacts = {
            "source_pdf": "source.pdf",
            "native_capture": "native.json",
            "common_ir": "common_ir.json",
        }
        if fusion:
            expected_artifacts.update(
                {
                    "render_manifest": "render_manifest.json",
                    "surya_layout_artifact": "surya_layout_artifact.json",
                }
            )
        if set(artifacts) != set(expected_artifacts):
            raise ExistingPdfOneShotError("replay_invalid")
        for key, name in expected_artifacts.items():
            descriptor = artifacts.get(key)
            candidate = root / name
            if not isinstance(descriptor, dict) or descriptor.get("path") != name:
                raise ExistingPdfOneShotError("replay_invalid")
            if descriptor.get("sha256") != _sha256_path(candidate):
                raise ExistingPdfOneShotError("replay_invalid")
            if descriptor.get("size_bytes") != candidate.stat().st_size:
                raise ExistingPdfOneShotError("replay_invalid")
        if artifacts["source_pdf"].get("sha256") != source_sha256:
            raise ExistingPdfOneShotError("replay_invalid")
        if fusion:
            assert render_manifest_path is not None
            assert surya_artifact_path is not None
            if _sha256_path(root / "render_manifest.json") != _sha256_path(
                render_manifest_path
            ):
                raise ExistingPdfOneShotError("replay_invalid")
            if _sha256_path(root / "surya_layout_artifact.json") != _sha256_path(
                surya_artifact_path
            ):
                raise ExistingPdfOneShotError("replay_invalid")
    except ExistingPdfOneShotError:
        raise
    except (OSError, TypeError, ValueError):
        raise ExistingPdfOneShotError("replay_invalid") from None


def _require_native_preflight_eligible(
    root: Path,
    *,
    expected_page_count: int,
) -> None:
    """Fail before GPU/LLM work unless every PDF page has native text."""

    common_ir = _json_object(root / "common_ir.json")
    document = common_ir.get("document")
    if not isinstance(document, dict):
        raise ExistingPdfOneShotError("native_preflight_ineligible")
    page_count = document.get("page_count")
    native_text_page_count = document.get("native_text_page_count")
    if (
        document.get("pdf_semantic_eligibility") != "eligible_native_text"
        or type(page_count) is not int
        or page_count <= 0
        or type(native_text_page_count) is not int
        or native_text_page_count != page_count
        or page_count != expected_page_count
    ):
        raise ExistingPdfOneShotError("native_preflight_ineligible")


def _default_render(source: Path, output: Path, timeout: float) -> None:
    from scripts.render_existing_pdf_pages import render_existing_pdf_pages

    render_existing_pdf_pages(
        source_pdf=source,
        output_directory=output,
        timeout_seconds=timeout,
    )


def _default_native_preflight(
    notice_id: str,
    source: Path,
    output: Path,
    timeout: float,
) -> None:
    from scripts.replay_existing_pdf_native import replay_existing_pdf

    replay_existing_pdf(
        notice_id=notice_id,
        source_pdf=source,
        output_directory=output,
        timeout_seconds=timeout,
    )


def _default_load_producer(path: Path) -> SuryaProducerIdentity:
    return surya_e2e._load_runpod_settings(path).producer


def _default_dispatch(args: argparse.Namespace, render_root: Path) -> SuryaLayoutArtifact:
    e2e_args = surya_e2e.build_parser().parse_args(
        [
            "--artifact-root",
            str(render_root),
            "--runpod-config-json",
            str(args.runpod_config_json),
            "--runpod-api-client-config",
            str(args.runpod_api_client_config),
            "--runpod-bearer-token-file",
            str(args.runpod_bearer_token_file),
            "--storage-bucket",
            args.storage_bucket,
            "--supabase-runtime-env",
            str(args.supabase_runtime_env),
            "--storage-timeout-seconds",
            str(args.storage_timeout_seconds),
            "--http-timeout-seconds",
            str(args.http_timeout_seconds),
            "--poll-timeout-seconds",
            str(args.poll_timeout_seconds),
            "--poll-interval-seconds",
            str(args.poll_interval_seconds),
        ]
    )
    accepted: list[SuryaLayoutArtifact] = []
    try:
        outcome = surya_e2e.run_e2e(
            e2e_args,
            accepted_artifact=accepted.append,
        )
    except surya_e2e.PersistentSuryaE2EError as error:
        reason = str(error)
        if reason in {
            "storage_upload_failed",
            "storage_capability_issue_failed",
        }:
            raise ExistingPdfOneShotError(reason) from None
        raise ExistingPdfOneShotError("surya_dispatch_failed") from None
    if outcome.disposition != "succeeded" or len(accepted) != 1:
        raise ExistingPdfOneShotError("surya_dispatch_failed")
    return accepted[0]


def _default_replay(
    notice_id: str,
    source: Path,
    layout_artifact: Path,
    render_manifest: Path,
    output: Path,
    timeout: float,
) -> None:
    # The reviewed replay helper owns parser isolation and publication.  Keep
    # this adapter tiny so the pending Surya seam has exactly one integration
    # point and no Common IR construction is duplicated here.
    from scripts.replay_existing_pdf_native import replay_existing_pdf

    parameters = inspect.signature(replay_existing_pdf).parameters
    kwargs: dict[str, Any] = {
        "notice_id": notice_id,
        "source_pdf": source,
        "output_directory": output,
        "timeout_seconds": timeout,
    }
    if "surya_layout_artifact" in parameters:
        kwargs["surya_layout_artifact"] = layout_artifact
        if "render_manifest" not in parameters:
            raise ExistingPdfOneShotError("surya_replay_seam_unavailable")
        kwargs["render_manifest"] = render_manifest
    elif "surya_layout_artifact_path" in parameters:
        kwargs["surya_layout_artifact_path"] = layout_artifact
        if "render_manifest" not in parameters:
            raise ExistingPdfOneShotError("surya_replay_seam_unavailable")
        kwargs["render_manifest"] = render_manifest
    else:
        raise ExistingPdfOneShotError("surya_replay_seam_unavailable")
    replay_existing_pdf(**kwargs)


def _default_structure(
    common_ir: dict[str, Any], config: OpenAIConfig
) -> tuple[dict[str, Any], dict[str, Any]]:
    model = config.request_profile_model or config.llm_model
    clients = {
        task_name: OpenAILLMClient(
            api_key=config.api_key,
            model_profiles={PROFILE_MODEL_PROFILE: model},
            timeout_seconds=config.timeout_seconds,
            max_completion_tokens=token_cap,
            reasoning_effort=REASONING_EFFORT,
        )
        for task_name, token_cap in TASK_COMPLETION_TOKEN_CAPS.items()
    }
    client = _TaskDispatchLlm(
        clients
    )
    artifacts = announcement_profiles.structure_announcement_profile_artifacts(
        common_ir,
        client,
        model_profile=PROFILE_MODEL_PROFILE,
        composite_candidate_mode=COMPOSITE_CANDIDATE_MODE,
        native_exact_candidate_mode=NATIVE_EXACT_CANDIDATE_MODE,
        source_selection_attempts=SOURCE_SELECTION_ATTEMPTS,
    )
    return artifacts.profile, artifacts.source_selection


def _default_ingest(
    record_path: Path, settings: Mapping[str, str]
) -> Mapping[str, Any]:
    storage = SupabaseStorage(
        settings["SUPABASE_URL"], settings["SUPABASE_SERVICE_ROLE_KEY"]
    )
    database = KnowledgeBase(settings["DATABASE_URL"])
    try:
        return run_transaction(
            database, lambda: ingest_record(record_path, storage, database)
        )
    finally:
        database.close()


def default_ports() -> PipelinePorts:
    return PipelinePorts(
        render=_default_render,
        native_preflight=_default_native_preflight,
        load_producer=_default_load_producer,
        dispatch=_default_dispatch,
        replay=_default_replay,
        structure=_default_structure,
        validate_package=validate_notice_directory,
        ingest=_default_ingest,
    )


def _publish_profile(
    directory: Path,
    profile: Mapping[str, Any],
    selection: Mapping[str, Any],
) -> None:
    if directory.exists() or directory.is_symlink():
        raise ExistingPdfOneShotError("output_conflict")
    try:
        staging = Path(tempfile.mkdtemp(prefix=".profile-", dir=directory.parent))
    except OSError:
        raise ExistingPdfOneShotError("profile_publication_failed") from None
    try:
        _write_exclusive(
            staging / "structured_profile.v0.2.json", _canonical_json(profile)
        )
        _write_exclusive(
            staging / "source_selection.json", _canonical_json(selection)
        )
        os.rename(staging, directory)
    except ExistingPdfOneShotError:
        raise
    except OSError:
        raise ExistingPdfOneShotError("profile_publication_failed") from None
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def _profile_artifacts(directory: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    profile_path = directory / "structured_profile.v0.2.json"
    selection_path = directory / "source_selection.json"
    if not profile_path.is_file() or not selection_path.is_file():
        raise ExistingPdfOneShotError("profile_artifact_invalid")
    return _json_object(profile_path), _json_object(selection_path)


def _validated_ingest_result(
    value: Mapping[str, Any], *, notice_id: str
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ExistingPdfOneShotError("ingest_result_invalid")
    status_value = value.get("status")
    profile_version_pk = value.get("profile_version_pk")
    if (
        status_value not in {"ingested", "already_ingested"}
        or value.get("notice_id") != f"bizinfo:{notice_id}"
        or not isinstance(profile_version_pk, str)
        or not profile_version_pk.strip()
    ):
        raise ExistingPdfOneShotError("ingest_result_invalid")
    return dict(value)


def _publish_package(
    *,
    output_root: Path,
    notice_id: str,
    source_pdf: Path,
    metadata: Mapping[str, Any],
    metadata_raw: bytes,
    common_ir_path: Path,
    render_root: Path,
    render_manifest: PdfRenderManifest,
    replay_root: Path,
    profile: Mapping[str, Any],
    selection: Mapping[str, Any],
    generated_at: str,
) -> Path:
    package_root = output_root / "package"
    notice_root = package_root / notice_id
    if package_root.exists() or package_root.is_symlink():
        return notice_root
    try:
        staging = Path(tempfile.mkdtemp(prefix=".package-", dir=output_root))
    except OSError:
        raise ExistingPdfOneShotError("package_publication_failed") from None
    try:
        notice = staging / notice_id
        attachment_name = f"{notice_id}.pdf"
        common_name = f"{notice_id}.pdf.json"
        _write_exclusive(notice / "metadata.json", metadata_raw)
        _copy_exclusive(source_pdf, notice / "attachments" / attachment_name)
        _copy_exclusive(
            common_ir_path, notice / "pipeline" / "common_ir_v1" / common_name
        )
        _write_exclusive(
            notice / "pipeline" / "structured_profile.v0.2.json",
            _canonical_json(profile),
        )
        _write_exclusive(
            notice / "pipeline" / "source_selection.json",
            _canonical_json(selection),
        )
        fusion = notice / "pipeline" / "pdf_fusion"
        replay_artifacts = {
            "native_capture.json": replay_root / "native.json",
            "render_manifest.json": replay_root / "render_manifest.json",
            "surya_layout_artifact.json": replay_root / "surya_layout_artifact.json",
            "replay_manifest.json": replay_root / "manifest.json",
        }
        for name, source in replay_artifacts.items():
            _copy_exclusive(source, fusion / name)
        if _sha256_path(replay_artifacts["render_manifest.json"]) != _sha256_path(
            render_root / "render_manifest.json"
        ):
            raise ExistingPdfOneShotError("render_invalid")
        source_relative = PurePosixPath(render_manifest.source_pdf_relative_path)
        _copy_exclusive(source_pdf, fusion.joinpath(*source_relative.parts))
        for page in render_manifest.pages:
            image_relative = PurePosixPath(page.image_relative_path)
            if not image_relative.parts or image_relative.parts[0] != "rendered":
                raise ExistingPdfOneShotError("render_invalid")
            _copy_exclusive(
                render_root.joinpath(*image_relative.parts),
                fusion.joinpath(*image_relative.parts),
            )
        record = {
            "schema_version": "bizinfo_existing_ingestion_record/v0.1",
            "generated_at": generated_at,
            "portal_metadata": dict(metadata),
            "analysis": {
                "input_path": attachment_name,
                "common_ir_path": f"pipeline/common_ir_v1/{common_name}",
                "pdf_fusion": {
                    "native_capture_path": "pipeline/pdf_fusion/native_capture.json",
                    "render_manifest_path": "pipeline/pdf_fusion/render_manifest.json",
                    "surya_layout_artifact_path": "pipeline/pdf_fusion/surya_layout_artifact.json",
                    "replay_manifest_path": "pipeline/pdf_fusion/replay_manifest.json",
                    "rendered_pages_path": "pipeline/pdf_fusion/rendered",
                },
            },
            "structured_profile": dict(profile),
        }
        _write_exclusive(
            notice / "pipeline" / "ingestion_record.v0.1.json",
            _canonical_json(record),
        )
        os.rename(staging, package_root)
        return notice_root
    except ExistingPdfOneShotError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise ExistingPdfOneShotError("package_publication_failed") from None
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def run_pipeline(
    args: argparse.Namespace,
    *,
    ports: PipelinePorts | None = None,
) -> PipelineArtifacts:
    active_ports = ports or default_ports()
    metadata, metadata_raw = _metadata(args.metadata)
    notice_id = str(metadata["notice_id"])
    if args.pdf.suffix.casefold() != ".pdf" or not args.pdf.is_file():
        raise ExistingPdfOneShotError("input_invalid")
    if _pdf_header(args.pdf) != b"%PDF-":
        raise ExistingPdfOneShotError("input_invalid")
    openai = _load_backend_openai_config(args.backend_env)
    model = openai.request_profile_model or openai.llm_model
    try:
        surya_producer = active_ports.load_producer(args.runpod_config_json)
    except Exception:
        raise ExistingPdfOneShotError("surya_configuration_invalid") from None
    if not isinstance(surya_producer, SuryaProducerIdentity):
        raise ExistingPdfOneShotError("surya_configuration_invalid")
    output_root = args.output_dir.expanduser()
    generated_at = args.generated_at or datetime.now(UTC).isoformat().replace("+00:00", "Z")
    invocation = _invocation(
        notice_id=notice_id,
        pdf=args.pdf,
        metadata_raw=metadata_raw,
        runpod_config=args.runpod_config_json,
        api_client_config=args.runpod_api_client_config,
        storage_bucket=args.storage_bucket,
        model=model,
        surya_producer=surya_producer,
        openai_timeout_seconds=openai.timeout_seconds,
        execution_policy={
            "render_timeout_seconds": args.render_timeout_seconds,
            "replay_timeout_seconds": args.replay_timeout_seconds,
            "storage_timeout_seconds": args.storage_timeout_seconds,
            "http_timeout_seconds": args.http_timeout_seconds,
            "poll_timeout_seconds": args.poll_timeout_seconds,
            "poll_interval_seconds": args.poll_interval_seconds,
        },
        generated_at=generated_at,
    )
    if args.resume and output_root.is_dir():
        prior = _json_object(output_root / "invocation.json", max_bytes=64 * 1024)
        generated_at = str(prior.get("generated_at") or "")
        invocation["generated_at"] = generated_at
    _prepare_root(output_root, invocation, resume=args.resume)

    work = output_root / "work"
    work.mkdir(mode=0o700, exist_ok=True)
    render_root = work / "render"
    if not render_root.exists():
        active_ports.render(args.pdf, render_root, args.render_timeout_seconds)
    render_manifest = _load_render(render_root)

    native_preflight_root = work / "native_preflight"
    if not native_preflight_root.exists():
        active_ports.native_preflight(
            notice_id,
            args.pdf,
            native_preflight_root,
            args.replay_timeout_seconds,
        )
    _validate_replay(
        native_preflight_root,
        notice_id=notice_id,
        source_sha256=invocation["source_pdf_sha256"],
    )
    _require_native_preflight_eligible(
        native_preflight_root,
        expected_page_count=render_manifest.page_count,
    )

    surya_path = work / "surya_layout_artifact.json"
    if not surya_path.exists():
        artifact = active_ports.dispatch(args, render_root)
        _write_exclusive(surya_path, artifact.canonical_json())
    _load_surya(surya_path, render_manifest, surya_producer)

    replay_root = work / "native_replay"
    if not replay_root.exists():
        active_ports.replay(
            notice_id,
            args.pdf,
            surya_path,
            render_root / "render_manifest.json",
            replay_root,
            args.replay_timeout_seconds,
        )
    _validate_replay(
        replay_root,
        notice_id=notice_id,
        source_sha256=invocation["source_pdf_sha256"],
        render_manifest_path=render_root / "render_manifest.json",
        surya_artifact_path=surya_path,
    )
    common_ir_path = replay_root / "common_ir.json"
    common_ir = _json_object(common_ir_path)
    common_document = common_ir.get("document")
    if (
        common_ir.get("schema_version") != "common_ir_v1"
        or not isinstance(common_document, dict)
        or common_document.get("document_id") != f"pdf:{notice_id}"
        or (common_document.get("provenance") or {}).get("source_sha256")
        != invocation["source_pdf_sha256"]
    ):
        raise ExistingPdfOneShotError("common_ir_invalid")

    profile_root = work / "profile"
    if not profile_root.exists():
        profile, selection = active_ports.structure(common_ir, openai)
        _publish_profile(profile_root, profile, selection)
    profile, selection = _profile_artifacts(profile_root)

    package_notice = _publish_package(
        output_root=output_root,
        notice_id=notice_id,
        source_pdf=args.pdf,
        metadata=metadata,
        metadata_raw=metadata_raw,
        common_ir_path=common_ir_path,
        render_root=render_root,
        render_manifest=render_manifest,
        replay_root=replay_root,
        profile=profile,
        selection=selection,
        generated_at=generated_at,
    )
    try:
        active_ports.validate_package(package_notice)
    except Exception:
        raise ExistingPdfOneShotError("package_invalid") from None
    record_path = package_notice / "pipeline" / "ingestion_record.v0.1.json"

    ingested = False
    if not args.no_ingest:
        result_path = output_root / "ingest_result.json"
        prior_exists = result_path.exists() or result_path.is_symlink()
        try:
            supabase = load_local_supabase_settings(
                args.supabase_runtime_env,
                require_private_secret_file=True,
            )
            observed = active_ports.ingest(record_path, supabase)
        except Exception:
            raise ExistingPdfOneShotError("ingest_failed") from None
        result = _validated_ingest_result(observed, notice_id=notice_id)
        if prior_exists:
            prior = _validated_ingest_result(
                _json_object(result_path, max_bytes=1024 * 1024),
                notice_id=notice_id,
            )
            if prior["profile_version_pk"] != result["profile_version_pk"]:
                raise ExistingPdfOneShotError("ingest_result_invalid")
        else:
            _write_exclusive(result_path, _canonical_json(dict(result)))
        ingested = True

    return PipelineArtifacts(
        notice_id=notice_id,
        output_root=output_root,
        package_notice_dir=package_notice,
        ingestion_record=record_path,
        ingested=ingested,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf", type=Path, required=True)
    parser.add_argument("--metadata", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--runpod-config-json", type=Path, required=True)
    parser.add_argument("--runpod-api-client-config", type=Path, required=True)
    parser.add_argument("--runpod-bearer-token-file", type=Path, required=True)
    parser.add_argument("--supabase-runtime-env", type=Path, required=True)
    parser.add_argument("--backend-env", type=Path, required=True)
    parser.add_argument("--storage-bucket", default="request-temp")
    parser.add_argument("--render-timeout-seconds", type=float, default=300.0)
    parser.add_argument("--replay-timeout-seconds", type=float, default=300.0)
    parser.add_argument("--storage-timeout-seconds", type=float, default=30.0)
    parser.add_argument("--http-timeout-seconds", type=float, default=30.0)
    # The reviewed persistent-Surya deployment caps one execution at 180s.
    # A larger local poll budget is rejected by the accelerator boundary, so
    # keep the operator default inside the same explicit contract.
    parser.add_argument("--poll-timeout-seconds", type=float, default=180.0)
    parser.add_argument("--poll-interval-seconds", type=float, default=2.0)
    parser.add_argument("--generated-at", help=argparse.SUPPRESS)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--no-ingest", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = run_pipeline(args)
    except ExistingPdfOneShotError as error:
        print(
            json.dumps(
                {
                    "schema_version": PIPELINE_SCHEMA,
                    "status": "failed",
                    "reason_code": error.reason_code,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 1
    except Exception:
        print(
            json.dumps(
                {
                    "schema_version": PIPELINE_SCHEMA,
                    "status": "failed",
                    "reason_code": "operator_error",
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 1
    print(
        json.dumps(
            {
                "schema_version": PIPELINE_SCHEMA,
                "status": "succeeded",
                "notice_id": result.notice_id,
                "ingested": result.ingested,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
