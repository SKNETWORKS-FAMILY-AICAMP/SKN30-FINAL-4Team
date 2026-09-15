#!/usr/bin/env python3
"""Compare an automatic Existing Profile archive to reviewed frozen Gold."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from worker.evaluation.existing_profile_diff import (  # noqa: E402
    DEFAULT_EXPECTED_PROFILE_COUNT,
    ExistingProfileComparisonError,
    assert_expected_comparison,
    compare_existing_profile_corpora,
    write_comparison_report,
)


def _non_negative_integer(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an integer") from error
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return parsed


def _sha256(value: str) -> str:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise argparse.ArgumentTypeError("must be a lowercase SHA-256 hex digest")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Strictly compare unreviewed automatic Existing Profiles to a "
            "human-reviewed frozen Gold corpus without runtime I/O."
        )
    )
    parser.add_argument("--baseline-zip", required=True, type=Path)
    parser.add_argument("--gold-root", required=True, type=Path)
    parser.add_argument(
        "--output-dir",
        required=True,
        type=Path,
        help="Existing directory that will receive exactly one comparison report.",
    )
    parser.add_argument(
        "--expected-profile-count",
        type=_non_negative_integer,
        default=DEFAULT_EXPECTED_PROFILE_COUNT,
        help=(
            "Exact baseline and verified-Gold Profile count "
            f"(default: {DEFAULT_EXPECTED_PROFILE_COUNT})."
        ),
    )
    parser.add_argument("--expected-shared", type=_non_negative_integer)
    parser.add_argument("--expected-unchanged", type=_non_negative_integer)
    parser.add_argument("--expected-changed", type=_non_negative_integer)
    parser.add_argument(
        "--expected-changed-id",
        action="append",
        default=None,
        help="Expected changed PBLN id; repeat once per id.",
    )
    parser.add_argument("--expected-baseline-sha256", type=_sha256)
    parser.add_argument("--expected-gold-freeze-manifest-sha256", type=_sha256)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = compare_existing_profile_corpora(
            args.baseline_zip,
            args.gold_root,
            expected_profile_count=args.expected_profile_count,
        )
        assert_expected_comparison(
            report,
            baseline=args.expected_profile_count,
            gold=args.expected_profile_count,
            shared=(
                args.expected_shared
                if args.expected_shared is not None
                else args.expected_profile_count
            ),
            unchanged=args.expected_unchanged,
            changed=args.expected_changed,
            baseline_only=0,
            gold_only=0,
            changed_notice_ids=args.expected_changed_id,
            baseline_archive_sha256=args.expected_baseline_sha256,
            gold_freeze_manifest_sha256=args.expected_gold_freeze_manifest_sha256,
        )
        report_path = write_comparison_report(
            report,
            output_dir=args.output_dir,
            gold_root=args.gold_root,
        )
    except ExistingProfileComparisonError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1

    counts = report["counts"]
    print(
        json.dumps(
            {
                "status": "compared",
                "report_file": report_path.name,
                "shared": counts["shared"],
                "unchanged": counts["unchanged"],
                "changed": counts["changed"],
                "baseline_only": counts["baseline_only"],
                "gold_only": counts["gold_only"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
