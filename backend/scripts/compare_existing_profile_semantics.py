#!/usr/bin/env python3
"""Offline provenance-grade B/G/C semantic gate for Existing Profiles."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
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


def _positive_integer(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an integer") from error
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Compare baseline (B), frozen Gold (G), and candidate (C) Existing "
            "Profile+source-selection+Common-IR archives without network/runtime I/O."
        )
    )
    parser.add_argument("--baseline-zip", required=True, type=Path)
    parser.add_argument("--gold-root", required=True, type=Path)
    candidate_mode = parser.add_mutually_exclusive_group(required=True)
    candidate_mode.add_argument("--candidate-zip", type=Path)
    candidate_mode.add_argument(
        "--calibrate-baseline",
        action="store_true",
        help="Report the reviewed B/G split without treating baseline as a generated candidate.",
    )
    parser.add_argument("--output-dir", required=True, type=Path)
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
        help="Exact candidate corpus size; defaults to the reference count.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
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
                args.candidate_zip,
                expected_reference_count=args.expected_reference_count,
                expected_candidate_count=args.expected_candidate_count,
                notice_ids=args.notice_id,
            )
        report_path = write_semantic_report(report, output_dir=args.output_dir, gold_root=args.gold_root)
    except ExistingProfileSemanticError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    failed = report["counts"]["failed"]
    print(json.dumps({"status": "passed" if failed == 0 else "failed", "report_file": report_path.name, **report["counts"]}, ensure_ascii=False, sort_keys=True))
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
