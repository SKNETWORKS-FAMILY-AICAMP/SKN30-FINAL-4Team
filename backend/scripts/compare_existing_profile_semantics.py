#!/usr/bin/env python3
"""Offline provenance-grade B/G/I/C semantic gate for Existing Profiles."""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat
import sys


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from worker.evaluation.existing_profile_semantic_diff import (  # noqa: E402
    DEFAULT_EXPECTED_PROFILE_COUNT,
    ExistingProfileSemanticError,
    calibrate_semantic_profile_corpora,
    compare_semantic_profile_corpora,
    write_semantic_report,
)
from worker.evaluation.pristine_common_ir import (  # noqa: E402
    PRISTINE_HARD6_ARCHIVE_SHA256,
)


# These are the reviewed B/G corpus pins.  They intentionally live beside the
# command that consumes the corpus rather than being inferred from a filename
# or a path.  A later Gold release must update the pins and calibration
# expectations together in a reviewed change.
DEFAULT_BASELINE_ARCHIVE_SHA256 = (
    "6649f1a5aab36f659d688634103950d3b73f8a5903a453aabdbbd9f5bc0f7f0d"
)
DEFAULT_GOLD_FREEZE_MANIFEST_SHA256 = (
    "a2c35fb4c98c92c23ff34faa045a16ec4e8ed1ea4bae5397caab547cb3db6987"
)
DEFAULT_CALIBRATION_PASSED = 94
DEFAULT_CALIBRATION_FAILED = 6
DEFAULT_CALIBRATION_FAILED_NOTICE_IDS = (
    "PBLN_000000000103645",
    "PBLN_000000000112425",
    "PBLN_000000000117175",
    "PBLN_000000000121019",
    "PBLN_000000000121309",
    "PBLN_000000000122023",
)
_SHA256_PATTERN = re.compile(r"[0-9a-fA-F]{64}")

# The profile archive loader has the same 512 MiB physical archive cap.  Keep
# the preflight cap explicit here as well: hashing must not become an
# unbounded read before the loader gets a chance to reject hostile input.
# The reviewed baseline is about 84 MiB, so this does not constrain it.
MAX_PINNED_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_PINNED_FREEZE_MANIFEST_BYTES = 32 * 1024 * 1024


def _positive_integer(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an integer") from error
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def _nonnegative_integer(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an integer") from error
    if parsed < 0:
        raise argparse.ArgumentTypeError("must not be negative")
    return parsed


def _sha256_pin(value: str) -> str:
    if not _SHA256_PATTERN.fullmatch(value):
        raise argparse.ArgumentTypeError("must be a 64-character SHA-256 hexadecimal digest")
    return value.lower()


def _stat_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    """Return the pathname/descriptor identity relevant to a stable digest."""

    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _regular_file_sha256(path: Path, *, label: str, max_bytes: int) -> str:
    """Hash one bounded, stable regular file without following symlinks.

    We retain the pre-open pathname identity, read exactly the initial fstat
    size, and compare both the descriptor and pathname identities afterwards.
    This makes truncation, growth, and replacement input errors rather than a
    potentially unbounded or mixed-snapshot hash.
    """

    descriptor: int | None = None
    try:
        supplied = path.expanduser()
        link_metadata = supplied.lstat()
        if stat.S_ISLNK(link_metadata.st_mode):
            raise ExistingProfileSemanticError(f"{label} must not be a symlink")
        if not stat.S_ISREG(link_metadata.st_mode):
            raise ExistingProfileSemanticError(f"{label} must be a regular file")
        descriptor = os.open(
            supplied,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
        )
    except OSError as error:
        raise ExistingProfileSemanticError(f"{label} is not readable") from error
    try:
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = None
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise ExistingProfileSemanticError(
                    f"{label} must be a regular file"
                )
            if _stat_identity(link_metadata) != _stat_identity(before):
                raise ExistingProfileSemanticError(
                    f"{label} was replaced before its SHA-256 was calculated"
                )
            if before.st_size > max_bytes:
                raise ExistingProfileSemanticError(
                    f"{label} exceeds the {max_bytes}-byte preflight size cap"
                )
            digest = sha256()
            remaining = before.st_size
            while remaining:
                chunk = handle.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise ExistingProfileSemanticError(
                        f"{label} was truncated while its SHA-256 was calculated"
                    )
                digest.update(chunk)
                remaining -= len(chunk)
            if handle.read(1):
                raise ExistingProfileSemanticError(
                    f"{label} grew while its SHA-256 was calculated"
                )
            after = os.fstat(handle.fileno())
            final_path = supplied.lstat()
            if (
                _stat_identity(before) != _stat_identity(after)
                or _stat_identity(before) != _stat_identity(final_path)
            ):
                raise ExistingProfileSemanticError(
                    f"{label} changed while its SHA-256 was calculated"
                )
    except OSError as error:
        raise ExistingProfileSemanticError(f"{label} cannot be read") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return digest.hexdigest()


def _require_sha256_pin(
    *, path: Path, expected: str, label: str, max_bytes: int
) -> None:
    actual = _regular_file_sha256(path, label=label, max_bytes=max_bytes)
    if actual != expected:
        raise ExistingProfileSemanticError(
            f"{label} SHA-256 pin mismatch (expected {expected}, got {actual})"
        )


def _verify_input_pins(args: argparse.Namespace) -> None:
    """Fail before artifact loading/report publication if the trust roots differ."""

    _require_sha256_pin(
        path=args.baseline_zip,
        expected=args.expected_baseline_sha256,
        label="baseline archive",
        max_bytes=MAX_PINNED_ARCHIVE_BYTES,
    )
    _require_sha256_pin(
        path=args.gold_root / "freeze_manifest.json",
        expected=args.expected_gold_freeze_manifest_sha256,
        label="Gold freeze manifest",
        max_bytes=MAX_PINNED_FREEZE_MANIFEST_BYTES,
    )
    if args.candidate_zip is not None:
        if args.input_zip is None or args.expected_input_sha256 is None:
            raise ExistingProfileSemanticError(
                "candidate mode requires --input-zip and --expected-input-sha256"
            )
        if args.expected_input_sha256 != PRISTINE_HARD6_ARCHIVE_SHA256:
            raise ExistingProfileSemanticError(
                "--expected-input-sha256 does not match the checked-in pristine input pin"
            )
        _require_sha256_pin(
            path=args.input_zip,
            expected=args.expected_input_sha256,
            label="pristine input archive",
            max_bytes=MAX_PINNED_ARCHIVE_BYTES,
        )
    if args.expected_candidate_sha256 is not None:
        if args.candidate_zip is None:
            raise ExistingProfileSemanticError(
                "--expected-candidate-sha256 requires --candidate-zip"
            )
        _require_sha256_pin(
            path=args.candidate_zip,
            expected=args.expected_candidate_sha256,
            label="candidate archive",
            max_bytes=MAX_PINNED_ARCHIVE_BYTES,
        )


def _verify_loaded_report_pins(
    args: argparse.Namespace, report: dict[str, object]
) -> None:
    """Bind the parsed snapshot to the preflight pins, closing swap races."""

    expected = (
        ("baseline", "archive_sha256", args.expected_baseline_sha256),
        (
            "gold",
            "freeze_manifest_sha256",
            args.expected_gold_freeze_manifest_sha256,
        ),
    )
    if args.expected_input_sha256 is not None:
        expected += (("input", "archive_sha256", args.expected_input_sha256),)
    if args.expected_candidate_sha256 is not None:
        expected += (
            ("candidate", "archive_sha256", args.expected_candidate_sha256),
        )
    for corpus, field, digest in expected:
        identity = report.get(corpus)
        if not isinstance(identity, dict) or identity.get(field) != digest:
            raise ExistingProfileSemanticError(
                f"loaded {corpus} identity does not match its SHA-256 pin"
            )


def _expected_calibration_failed_ids(args: argparse.Namespace) -> tuple[str, ...]:
    """Resolve reviewed defaults while allowing an explicit all-pass corpus."""

    if args.expected_calibration_failed_notice_id is not None:
        expected_ids = tuple(args.expected_calibration_failed_notice_id)
    elif args.expected_calibration_failed == DEFAULT_CALIBRATION_FAILED:
        expected_ids = DEFAULT_CALIBRATION_FAILED_NOTICE_IDS
    elif args.expected_calibration_failed == 0:
        expected_ids = ()
    else:
        raise ExistingProfileSemanticError(
            "non-default positive --expected-calibration-failed requires matching "
            "--expected-calibration-failed-notice-id values"
        )
    if len(expected_ids) != len(set(expected_ids)):
        raise ExistingProfileSemanticError(
            "expected calibration failed notice ids must not contain duplicates"
        )
    if args.expected_calibration_failed != len(expected_ids):
        raise ExistingProfileSemanticError(
            "--expected-calibration-failed must equal the number of "
            "--expected-calibration-failed-notice-id values"
        )
    return expected_ids


def _calibration_matches_expectations(args: argparse.Namespace, report: dict[str, object]) -> bool:
    """Check the reviewed 94/6 split independently from candidate gate rules."""

    counts = report.get("counts")
    notices = report.get("notices")
    if not isinstance(counts, dict) or not isinstance(notices, list):
        raise ExistingProfileSemanticError("calibration report has an invalid counts/notices contract")
    expected_ids = _expected_calibration_failed_ids(args)
    actual_failed_ids: set[str] = set()
    for item in notices:
        if not isinstance(item, dict):
            raise ExistingProfileSemanticError("calibration report contains an invalid notice record")
        if item.get("semantic_gate_status") == "failed":
            notice_id = item.get("notice_id")
            if not isinstance(notice_id, str):
                raise ExistingProfileSemanticError("calibration failed notice is missing its id")
            actual_failed_ids.add(notice_id)
    return (
        counts.get("passed") == args.expected_calibration_passed
        and counts.get("failed") == args.expected_calibration_failed
        and actual_failed_ids == set(expected_ids)
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Compare historical baseline (B), frozen Gold (G), pinned pristine "
            "input (I), and candidate (C) Existing Profile artifacts without "
            "network/runtime I/O."
        )
    )
    parser.add_argument("--baseline-zip", required=True, type=Path)
    parser.add_argument("--gold-root", required=True, type=Path)
    parser.add_argument(
        "--expected-baseline-sha256",
        type=_sha256_pin,
        default=DEFAULT_BASELINE_ARCHIVE_SHA256,
        help="Pinned SHA-256 for --baseline-zip (reviewed Gold100 default).",
    )
    parser.add_argument(
        "--expected-gold-freeze-manifest-sha256",
        type=_sha256_pin,
        default=DEFAULT_GOLD_FREEZE_MANIFEST_SHA256,
        help="Pinned SHA-256 for --gold-root/freeze_manifest.json (reviewed Gold100 default).",
    )
    candidate_mode = parser.add_mutually_exclusive_group(required=True)
    candidate_mode.add_argument("--candidate-zip", type=Path)
    candidate_mode.add_argument(
        "--calibrate-baseline",
        action="store_true",
        help="Report the reviewed B/G split without treating baseline as a generated candidate.",
    )
    parser.add_argument(
        "--input-zip",
        type=Path,
        default=None,
        help="Pinned pristine hard-6 Common IR ZIP; required in candidate mode.",
    )
    parser.add_argument(
        "--input-manifest",
        type=Path,
        default=None,
        help="Optional deployed copy of the checked-in pristine input manifest.",
    )
    parser.add_argument(
        "--expected-input-sha256",
        type=_sha256_pin,
        default=None,
        help="Explicit pristine input archive pin; required in candidate mode.",
    )
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument(
        "--expected-candidate-sha256",
        type=_sha256_pin,
        default=None,
        help="Optional SHA-256 pin for --candidate-zip; mismatches are input errors.",
    )
    parser.add_argument("--notice-id", action="append", default=None, help="Repeat to limit comparison to selected PBLN ids.")
    parser.add_argument(
        "--expected-reference-count",
        type=_positive_integer,
        default=DEFAULT_EXPECTED_PROFILE_COUNT,
        help="Exact baseline and Gold corpus size (default: 100).",
    )
    parser.add_argument(
        "--expected-candidate-count",
        type=_positive_integer,
        default=None,
        help="Exact candidate corpus size; defaults to the pinned input count (six).",
    )
    parser.add_argument(
        "--expected-calibration-passed",
        type=_nonnegative_integer,
        default=DEFAULT_CALIBRATION_PASSED,
        help="Expected passed count in --calibrate-baseline mode (default: reviewed 94).",
    )
    parser.add_argument(
        "--expected-calibration-failed",
        type=_nonnegative_integer,
        default=DEFAULT_CALIBRATION_FAILED,
        help="Expected failed count in --calibrate-baseline mode (default: reviewed 6).",
    )
    parser.add_argument(
        "--expected-calibration-failed-notice-id",
        action="append",
        default=None,
        help="Repeat expected failed PBLN id in --calibrate-baseline mode (defaults to reviewed six).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    calibration_matches: bool | None = None
    try:
        if args.calibrate_baseline:
            _expected_calibration_failed_ids(args)
            if (
                args.input_zip is not None
                or args.input_manifest is not None
                or args.expected_input_sha256 is not None
            ):
                raise ExistingProfileSemanticError(
                    "pristine input options cannot be used with --calibrate-baseline"
                )
        _verify_input_pins(args)
        if args.calibrate_baseline:
            if args.expected_candidate_count is not None:
                raise ExistingProfileSemanticError(
                    "--expected-candidate-count cannot be used with --calibrate-baseline"
                )
            report = calibrate_semantic_profile_corpora(
                args.baseline_zip,
                args.gold_root,
                expected_reference_count=args.expected_reference_count,
                notice_ids=args.notice_id,
            )
        else:
            report = compare_semantic_profile_corpora(
                args.baseline_zip,
                args.gold_root,
                args.input_zip,
                args.candidate_zip,
                input_manifest=args.input_manifest,
                expected_reference_count=args.expected_reference_count,
                expected_candidate_count=args.expected_candidate_count,
                notice_ids=args.notice_id,
            )
        _verify_loaded_report_pins(args, report)
        if args.calibrate_baseline:
            calibration_matches = _calibration_matches_expectations(args, report)
        report_path = write_semantic_report(report, output_dir=args.output_dir, gold_root=args.gold_root)
    except ExistingProfileSemanticError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1

    if args.calibrate_baseline:
        if calibration_matches:
            print(
                json.dumps(
                    {
                        "status": "calibration_matched",
                        "report_file": report_path.name,
                        **report["counts"],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return 0
        print(
            json.dumps(
                {
                    "status": "calibration_mismatch",
                    "report_file": report_path.name,
                    **report["counts"],
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2

    failed = report["counts"]["failed"]
    print(json.dumps({"status": "passed" if failed == 0 else "failed", "report_file": report_path.name, **report["counts"]}, ensure_ascii=False, sort_keys=True))
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
