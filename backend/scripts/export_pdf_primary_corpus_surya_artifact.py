#!/usr/bin/env python3
"""Export one public A4.5 case's strict Surya layout artifact locally.

This operator command is intentionally narrower than the persistent Storage
E2E it invokes: its case must be a tracked public split member, its complete
native capture and canonical render must already bind to that split, and it
can create the local result exactly once.  It accepts no blind-reveal input.
The E2E may safely accept a pre-existing deterministic Storage result; that
cache hit is not a new RunPod execution and this command never claims one.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import stat
import sys
from typing import Any


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from worker import vendor as _worker_vendor  # noqa: E402,F401

from common_ir_pipeline.pdf_fusion.native_capture import (  # noqa: E402
    NativeCaptureError,
    canonical_json_bytes,
    load_native_capture_file,
    validate_native_capture,
)
from common_ir_pipeline.pdf_fusion.primary_corpus_split import (  # noqa: E402
    PrimaryCorpusSplitError,
    PrimaryCorpusSplitFixture,
    load_primary_corpus_split_file,
    validate_split_source_baseline_file,
)
from common_ir_pipeline.pdf_fusion.surya_layout_artifact import (  # noqa: E402
    SuryaLayoutArtifact,
    SuryaLayoutArtifactError,
    SuryaProducerIdentity,
    parse_surya_layout_artifact_bytes,
)
from scripts import run_persistent_surya_storage_e2e as persistent_e2e  # noqa: E402
from scripts.replay_existing_pdf_native import (  # noqa: E402
    ExistingPdfReplayError,
    _validate_common_ir_output,
)
from worker.accelerator_coordinator import CoordinatorDisposition  # noqa: E402
from worker.contracts.accelerator import (  # noqa: E402
    build_surya_layout_reconciliation_logical_compute_key,
)


_NATIVE_DIRECTORY = "native"
_NATIVE_CAPTURE_NAME = "native.json"
_NATIVE_MANIFEST_NAME = "manifest.json"
_COMMON_IR_NAME = "common_ir.json"
_RENDER_DIRECTORY = "render"
_RENDER_MANIFEST_NAME = "render_manifest.json"
_OUTPUT_DIRECTORY = "surya"
_OUTPUT_NAME = "surya_layout_artifact.json"
_SHA256_HEX = frozenset("0123456789abcdef")


class PrimaryCorpusSuryaExportError(RuntimeError):
    """A non-sensitive failure for this local export boundary."""


@dataclass(frozen=True, slots=True)
class _PinnedCaseRoot:
    path: Path
    device: int
    inode: int


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", required=True, type=Path)
    parser.add_argument(
        "--expected-split-sha256",
        required=True,
        help="externally reviewed canonical split SHA-256",
    )
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--case-root", required=True, type=Path)
    parser.add_argument("--source-baseline", required=True, type=Path)
    persistent_e2e.add_e2e_arguments(parser, include_artifact_root=False)
    return parser


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in _SHA256_HEX for character in value)
    )


def _regular_non_symlink(path: Path) -> os.stat_result:
    try:
        info = os.lstat(path)
    except OSError as error:
        raise PrimaryCorpusSuryaExportError("local_artifact_invalid") from error
    current_uid = getattr(os, "geteuid", lambda: -1)()
    if (
        current_uid < 0
        or info.st_uid != current_uid
        or info.st_nlink != 1
        or stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or stat.S_IMODE(info.st_mode) & 0o022
    ):
        raise PrimaryCorpusSuryaExportError("local_artifact_invalid")
    return info


def _case_root(path: Path) -> _PinnedCaseRoot:
    try:
        if path.is_symlink():
            raise OSError("case root must not be a symlink")
        root = path.resolve(strict=True)
        info = os.lstat(root)
        current_uid = getattr(os, "geteuid", lambda: -1)()
        if (
            current_uid < 0
            or info.st_uid != current_uid
            or not stat.S_ISDIR(info.st_mode)
            or stat.S_IMODE(info.st_mode) & 0o022
        ):
            raise OSError("case root must be a directory")
    except OSError as error:
        raise PrimaryCorpusSuryaExportError("local_artifact_invalid") from error
    return _PinnedCaseRoot(root, info.st_dev, info.st_ino)


def _safe_directory(path: Path) -> Path:
    try:
        info = os.lstat(path)
        current_uid = getattr(os, "geteuid", lambda: -1)()
        if (
            current_uid < 0
            or info.st_uid != current_uid
            or stat.S_ISLNK(info.st_mode)
            or not stat.S_ISDIR(info.st_mode)
            or stat.S_IMODE(info.st_mode) & 0o022
        ):
            raise OSError("unsafe case artifact directory")
        return path.resolve(strict=True)
    except OSError as error:
        raise PrimaryCorpusSuryaExportError("local_artifact_invalid") from error


def _public_case(split: PrimaryCorpusSplitFixture, case_id: object) -> Mapping[str, Any]:
    if not isinstance(case_id, str):
        raise PrimaryCorpusSuryaExportError("local_artifact_invalid")
    matches = [case for case in split.payload["cases"] if case["case_id"] == case_id]
    # ``sealed_case`` is deliberately not a selection source.  Keeping the
    # match strictly within ``cases`` also means a reveal cannot be smuggled
    # through this CLI's input surface.
    if len(matches) != 1:
        raise PrimaryCorpusSuryaExportError("local_artifact_invalid")
    return matches[0]


def _read_regular_bytes(path: Path, *, maximum: int = 64 * 1024 * 1024) -> bytes:
    try:
        return persistent_e2e._read_regular_file(path, max_bytes=maximum)
    except persistent_e2e.PersistentSuryaE2EError as error:
        raise PrimaryCorpusSuryaExportError("local_artifact_invalid") from error


def _validate_native_replay_manifest(
    root: Path,
    *,
    notice_id: str,
    source_sha256: str,
    page_count: int,
    native_bytes: bytes,
    native_capture: Mapping[str, Any],
) -> None:
    """Bind native, Common IR, and source files through its replay receipt."""

    manifest_path = root / _NATIVE_MANIFEST_NAME
    common_path = root / _COMMON_IR_NAME
    _regular_non_symlink(manifest_path)
    _regular_non_symlink(common_path)
    try:
        receipt_raw = _read_regular_bytes(manifest_path)
        receipt = persistent_e2e._strict_json_object(
            receipt_raw.decode("utf-8"), failure="local_artifact_invalid"
        )
        if (
            set(receipt) != {"schema_version", "scope", "notice_id", "whole_document", "pipeline", "artifacts", "coverage"}
            or
            receipt.get("schema_version") != "existing_pdf_native_replay/v1"
            or receipt.get("scope") != "existing_kb_offline_only"
            or receipt.get("notice_id") != notice_id
            or receipt.get("whole_document") is not True
            or receipt_raw != canonical_json_bytes(dict(receipt))
            or not isinstance(receipt.get("pipeline"), Mapping)
            or not isinstance(receipt.get("artifacts"), Mapping)
            or not isinstance(receipt.get("coverage"), Mapping)
        ):
            raise ValueError("invalid replay receipt")
        pipeline = receipt["pipeline"]
        if (
            set(pipeline) != {"capture_module", "pdf_inspector_version", "capture_limits", "adapter_module"}
            or pipeline.get("capture_module") != "common_ir_pipeline.workers.pdf_inspector_capture"
            or pipeline.get("pdf_inspector_version") != "1.17.0"
            or pipeline.get("adapter_module") != "common_ir_pipeline.adapters.pdf_native"
            or not isinstance(pipeline.get("capture_limits"), Mapping)
        ):
            raise ValueError("invalid replay pipeline")
        artifacts = receipt["artifacts"]
        expected = {
            "source_pdf": ("source.pdf", source_sha256, _read_regular_bytes(root / "source.pdf")),
            "native_capture": (_NATIVE_CAPTURE_NAME, sha256(native_bytes).hexdigest(), native_bytes),
            "common_ir": (_COMMON_IR_NAME, None, _read_regular_bytes(common_path)),
        }
        if set(artifacts) != set(expected):
            raise ValueError("unexpected replay artifacts")
        for name, (path, digest, raw) in expected.items():
            descriptor = artifacts[name]
            if not isinstance(descriptor, Mapping) or descriptor.get("path") != path:
                raise ValueError("unbound replay artifact")
            expected_keys = {
                "source_pdf": {"path", "sha256", "size_bytes"},
                "native_capture": {"path", "sha256", "size_bytes", "method", "version", "page_count"},
                "common_ir": {"path", "sha256", "size_bytes", "generator", "generator_version", "schema_version", "page_count"},
            }[name]
            if set(descriptor) != expected_keys:
                raise ValueError("invalid replay artifact descriptor")
            expected_digest = sha256(raw).hexdigest() if digest is None else digest
            if descriptor.get("sha256") != expected_digest or descriptor.get("size_bytes") != len(raw):
                raise ValueError("replay artifact hash or size mismatch")
        if (
            artifacts["native_capture"].get("method") != "pdf_inspector"
            or artifacts["native_capture"].get("version") != "1.17.0"
            or artifacts["native_capture"].get("page_count") != page_count
            or artifacts["common_ir"].get("generator") != "common_ir_v1_adapters"
            or artifacts["common_ir"].get("generator_version") != "1.1.0"
            or artifacts["common_ir"].get("schema_version") != "common_ir_v1"
            or artifacts["common_ir"].get("page_count") != page_count
        ):
            raise ValueError("invalid replay artifact lineage")
        coverage = receipt["coverage"]
        if set(coverage) != {
            "common_ir_block_count", "common_ir_pages", "document_page_count",
            "native_text_item_count", "native_text_pages",
        }:
            raise ValueError("invalid replay coverage")
        native_items = native_capture["text_items"]
        native_pages = sorted({item["page"] for item in native_items})
        # Decode Common IR before accepting coverage.  Its page sets describe
        # substantive content, not all physical pages: blank/image-only pages
        # are valid members of a complete native/rendered document.
        common_raw = expected["common_ir"][2]
        common = persistent_e2e._strict_json_object(
            common_raw.decode("utf-8"), failure="local_artifact_invalid"
        )
        if common_raw != canonical_json_bytes(dict(common)):
            raise ValueError("Common IR is not canonical")
        _validate_common_ir_output(
            common,
            notice_id=notice_id,
            source_sha256=source_sha256,
            page_count=page_count,
            pinned_inspector_version="1.17.0",
            allowed_raw_artifact_ids=("native.json",),
        )
        document = common.get("document")
        blocks = common.get("blocks")
        if (
            not isinstance(document, Mapping)
            or document.get("page_count") != page_count
            or document.get("native_text_page_count") != len(native_pages)
            or not isinstance(blocks, list)
            or any(
                not isinstance(block, Mapping)
                or isinstance(block.get("page"), bool)
                or not isinstance(block.get("page"), int)
                or not 1 <= block["page"] <= page_count
                for block in blocks
            )
        ):
            raise ValueError("Common IR page lineage mismatch")
        common_pages = sorted({block["page"] for block in blocks})
        if (
            coverage.get("document_page_count") != page_count
            or coverage.get("native_text_item_count") != len(native_items)
            or coverage.get("native_text_pages") != native_pages
            or coverage.get("common_ir_block_count") != len(blocks)
            or coverage.get("common_ir_pages") != common_pages
        ):
            raise ValueError("replay page lineage mismatch")
    except (
        ExistingPdfReplayError,
        UnicodeDecodeError,
        ValueError,
        TypeError,
        persistent_e2e.PersistentSuryaE2EError,
    ) as error:
        raise PrimaryCorpusSuryaExportError("local_artifact_invalid") from error


def _portable_producer(settings: object) -> SuryaProducerIdentity:
    producer = getattr(settings, "producer", None)
    try:
        values = producer.model_dump(mode="json")
    except (AttributeError, TypeError, ValueError):
        raise PrimaryCorpusSuryaExportError("configuration_invalid") from None
    try:
        return SuryaProducerIdentity.from_dict(values)
    except SuryaLayoutArtifactError:
        raise PrimaryCorpusSuryaExportError("configuration_invalid") from None


def _prepare_case(
    args: argparse.Namespace,
) -> tuple[
    _PinnedCaseRoot,
    Path,
    object,
    SuryaProducerIdentity,
    str,
    tuple[int, ...],
    Path,
]:
    """Perform every corpus/local-output check before the E2E can do I/O."""

    try:
        split = load_primary_corpus_split_file(args.split)
    except PrimaryCorpusSplitError as error:
        raise PrimaryCorpusSuryaExportError("local_artifact_invalid") from error
    if not _is_sha256(args.expected_split_sha256) or split.canonical_sha256 != args.expected_split_sha256:
        raise PrimaryCorpusSuryaExportError("local_artifact_invalid")
    try:
        validate_split_source_baseline_file(split, args.source_baseline)
    except PrimaryCorpusSplitError as error:
        raise PrimaryCorpusSuryaExportError("local_artifact_invalid") from error
    case = _public_case(split, args.case_id)
    pinned_root = _case_root(args.case_root)
    root = pinned_root.path

    native_root = _safe_directory(root / _NATIVE_DIRECTORY)
    render_root = _safe_directory(root / _RENDER_DIRECTORY)
    # Check the named evidence artifacts before parsing either one.  Native
    # and render retain separate source-PDF copies by design, so both are
    # independently rebound below rather than trusting just one copy.
    _regular_non_symlink(native_root / _NATIVE_CAPTURE_NAME)
    _regular_non_symlink(render_root / _RENDER_MANIFEST_NAME)
    try:
        render_manifest = persistent_e2e._load_render_manifest(render_root)
        if render_manifest.source_pdf_relative_path != "source.pdf":
            raise ValueError("render source path is not canonical")
        rendered_root = _safe_directory(render_root / "rendered")
        expected_rendered = {
            f"page-{page:04d}.png" for page in range(1, render_manifest.page_count + 1)
        }
        if {item.name for item in rendered_root.iterdir()} != expected_rendered:
            raise ValueError("rendered pages do not have the canonical exact page set")
        for item in rendered_root.iterdir():
            _regular_non_symlink(item)
        render_raw = _read_regular_bytes(render_root / _RENDER_MANIFEST_NAME)
        if render_raw != render_manifest.canonical_json():
            raise ValueError("render manifest is not canonical")
        _regular_non_symlink(render_root / render_manifest.source_pdf_relative_path)
        native_source_pdf = native_root / "source.pdf"
        _regular_non_symlink(native_source_pdf)
        native_raw = _read_regular_bytes(native_root / _NATIVE_CAPTURE_NAME)
        native_capture = validate_native_capture(
            load_native_capture_file(native_root / _NATIVE_CAPTURE_NAME),
            source_pdf=native_source_pdf,
            expected_notice_id=case["notice_id"],
            expected_source_relative_path="source.pdf",
        )
        if native_raw != canonical_json_bytes(native_capture):
            raise ValueError("native capture is not canonical")
        _validate_native_replay_manifest(
            native_root,
            notice_id=case["notice_id"],
            source_sha256=render_manifest.source_pdf_sha256,
            page_count=render_manifest.page_count,
            native_bytes=native_raw,
            native_capture=native_capture,
        )
    except (
        NativeCaptureError,
        PrimaryCorpusSuryaExportError,
        persistent_e2e.PersistentSuryaE2EError,
        ValueError,
        OSError,
    ) as error:
        raise PrimaryCorpusSuryaExportError("local_artifact_invalid") from error

    expected_source_sha256 = case["source_pdf_sha256"]
    if (
        render_manifest.source_pdf_sha256 != expected_source_sha256
        or native_capture["source_sha256"] != expected_source_sha256
        or native_capture["process_result"]["page_count"] != render_manifest.page_count
    ):
        raise PrimaryCorpusSuryaExportError("local_artifact_invalid")

    try:
        settings = persistent_e2e._load_runpod_settings(args.runpod_config_json)
        producer = _portable_producer(settings)
        # The E2E uses this same contract identity.  Calculating it here lets
        # the callback independently reparse its canonical bytes against the
        # exact public-case inputs, rather than trusting an object callback.
        logical_compute_key = build_surya_layout_reconciliation_logical_compute_key(
            render_manifest, settings.producer
        )
    except (PrimaryCorpusSuryaExportError, persistent_e2e.PersistentSuryaE2EError, TypeError, ValueError) as error:
        raise PrimaryCorpusSuryaExportError("configuration_invalid") from error

    output = _prepare_output_path(root)
    return (
        pinned_root,
        render_root,
        render_manifest,
        producer,
        logical_compute_key,
        tuple(range(1, render_manifest.page_count + 1)),
        output,
    )


def _prepare_output_path(root: Path) -> Path:
    parent = root / _OUTPUT_DIRECTORY
    try:
        target = parent / _OUTPUT_NAME
        try:
            os.lstat(target)
            raise OSError("output already exists")
        except FileNotFoundError:
            pass
        # A missing parent is created only after a single accepted callback;
        # therefore no output directory is left behind for preflight/E2E
        # failures.  An existing parent must already be safe.
        try:
            parent_info = os.lstat(parent)
        except FileNotFoundError:
            return target
        if not _safe_output_parent_info(parent_info):
            raise OSError("unsafe output parent")
        return target
    except OSError as error:
        raise PrimaryCorpusSuryaExportError("local_artifact_invalid") from error


def _safe_output_parent_info(info: os.stat_result) -> bool:
    current_uid = getattr(os, "geteuid", lambda: -1)()
    return (
        current_uid >= 0
        and info.st_uid == current_uid
        and not stat.S_ISLNK(info.st_mode)
        and stat.S_ISDIR(info.st_mode)
        and not stat.S_IMODE(info.st_mode) & 0o022
    )


def _publish_create_only(
    target: Path,
    content: bytes,
    *,
    pinned_root: _PinnedCaseRoot,
) -> None:
    """Create one private result without a replace window or temp residue."""

    temporary_name = f".{target.name}.{os.getpid()}.tmp"
    root_fd: int | None = None
    parent_fd: int | None = None
    temporary_fd: int | None = None
    created_temporary = False
    created_parent = False
    published = False
    try:
        root_fd = os.open(
            pinned_root.path,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        opened_root = os.fstat(root_fd)
        if (opened_root.st_dev, opened_root.st_ino) != (pinned_root.device, pinned_root.inode):
            raise OSError("case root changed")
        try:
            before = os.stat(_OUTPUT_DIRECTORY, dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            os.mkdir(_OUTPUT_DIRECTORY, mode=0o700, dir_fd=root_fd)
            created_parent = True
            before = os.stat(_OUTPUT_DIRECTORY, dir_fd=root_fd, follow_symlinks=False)
        if not _safe_output_parent_info(before):
            raise OSError("unsafe output parent")
        parent_fd = os.open(
            _OUTPUT_DIRECTORY,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=root_fd,
        )
        opened_parent = os.fstat(parent_fd)
        if (before.st_dev, before.st_ino) != (opened_parent.st_dev, opened_parent.st_ino):
            raise OSError("output parent changed")
        if created_parent:
            os.fchmod(parent_fd, 0o700)
        # The target was checked before E2E; check it again through the pinned
        # directory descriptor so an intervening rename/symlink swap cannot
        # turn this into a replace operation.
        try:
            os.stat(_OUTPUT_NAME, dir_fd=parent_fd, follow_symlinks=False)
            raise OSError("output already exists")
        except FileNotFoundError:
            pass
        temporary_fd = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=parent_fd,
        )
        created_temporary = True
        os.fchmod(temporary_fd, 0o600)
        total = 0
        while total < len(content):
            total += os.write(temporary_fd, content[total:])
        os.fsync(temporary_fd)
        os.close(temporary_fd)
        temporary_fd = None
        # linkat is the atomic, create-only commit point: unlike rename it
        # never replaces a pre-existing target.  Nothing after this call may
        # turn a committed final name into a reported failure.
        os.link(
            temporary_name,
            _OUTPUT_NAME,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
            follow_symlinks=False,
        )
        published = True
        # A post-commit unlink/fsync fault can leave the private temporary
        # hard link behind.  It is an availability/cleanup issue, not a
        # reason to contradict the already-published final artifact.
        os.unlink(temporary_name, dir_fd=parent_fd)
        os.fsync(parent_fd)
    except OSError as error:
        if published:
            # The create-only link already committed the canonical bytes.
            # A later directory fsync failure cannot safely be represented as
            # "no output", so retain the committed success.
            return
        try:
            if created_temporary and parent_fd is not None:
                os.unlink(temporary_name, dir_fd=parent_fd)
        except OSError:
            pass
        raise PrimaryCorpusSuryaExportError("local_artifact_invalid") from error
    finally:
        for descriptor in (temporary_fd, parent_fd, root_fd):
            if descriptor is None:
                continue
            try:
                os.close(descriptor)
            except OSError:
                if not published:
                    raise


def run_export(
    args: argparse.Namespace,
    *,
    e2e_runner: Callable[..., persistent_e2e.SafeE2EOutcome] | None = None,
) -> persistent_e2e.SafeE2EOutcome:
    """Run the E2E once and publish only its single strict accepted artifact."""

    pinned_root, render_root, manifest, producer, logical_key, requested_pages, output = _prepare_case(args)
    accepted: list[bytes] = []
    callback_count = 0

    def accept(artifact: SuryaLayoutArtifact) -> None:
        nonlocal callback_count
        callback_count += 1
        if callback_count != 1:
            raise PrimaryCorpusSuryaExportError("local_artifact_invalid")
        if type(artifact) is not SuryaLayoutArtifact:
            raise PrimaryCorpusSuryaExportError("local_artifact_invalid")
        try:
            # Reparse bytes, even though the E2E already accepted the object:
            # this is the independent local export acceptance boundary.
            canonical = artifact.canonical_json()
            checked = parse_surya_layout_artifact_bytes(
                canonical,
                render_manifest=manifest,
                expected_logical_compute_key=logical_key,
                expected_producer=producer,
                expected_requested_pages=requested_pages,
            )
            if checked.canonical_json() != canonical:
                raise SuryaLayoutArtifactError("non-canonical callback artifact")
        except (AttributeError, SuryaLayoutArtifactError, TypeError, ValueError) as error:
            raise PrimaryCorpusSuryaExportError("local_artifact_invalid") from error
        accepted.append(canonical)

    # Do not let the caller select a different E2E artifact root after all
    # public-corpus checks have passed.
    args.artifact_root = render_root
    runner = persistent_e2e.run_e2e if e2e_runner is None else e2e_runner
    outcome = runner(
        args,
        accepted_artifact=accept,
        expected_source_sha256=manifest.source_pdf_sha256,
    )
    if (
        outcome.disposition != CoordinatorDisposition.SUCCEEDED.value
        or callback_count != 1
        or len(accepted) != 1
    ):
        raise PrimaryCorpusSuryaExportError("local_artifact_invalid")
    # The E2E can take minutes.  Re-run the complete local admission check at
    # the publication boundary so a source/native/render/root replacement
    # after dispatch cannot be published under the earlier evidence binding.
    refreshed = _prepare_case(args)
    refreshed_root, refreshed_render, refreshed_manifest, refreshed_producer, refreshed_key, refreshed_pages, refreshed_output = refreshed
    if (
        (refreshed_root.device, refreshed_root.inode) != (pinned_root.device, pinned_root.inode)
        or refreshed_render != render_root
        or refreshed_manifest.manifest_sha256() != manifest.manifest_sha256()
        or refreshed_producer != producer
        or refreshed_key != logical_key
        or refreshed_pages != requested_pages
        or refreshed_output != output
    ):
        raise PrimaryCorpusSuryaExportError("local_artifact_invalid")
    _publish_create_only(output, accepted[0], pinned_root=refreshed_root)
    return outcome


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        run_export(args)
    except Exception:
        # No exception string is an output channel: adapter errors can contain
        # signed URLs, identities, or credentials.  Public case IDs are also
        # omitted, so this output cannot distinguish a blind selection probe.
        print(json.dumps({"status": "failed", "reason_code": "operator_error"}, sort_keys=True))
        return 1
    print(json.dumps({"status": "exported"}, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
