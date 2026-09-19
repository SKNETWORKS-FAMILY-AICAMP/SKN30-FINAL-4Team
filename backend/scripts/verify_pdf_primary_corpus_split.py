#!/usr/bin/env python3
"""Verify an immutable A4.5 split, its source baseline, and optional reveal."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from common_ir_pipeline.pdf_fusion.primary_corpus_split import (
    PrimaryCorpusSplitError,
    load_primary_corpus_blind_reveal_file,
    load_primary_corpus_split_file,
    validate_split_source_baseline_file,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", required=True, type=Path)
    parser.add_argument("--source-baseline", required=True, type=Path)
    parser.add_argument(
        "--expected-split-sha256",
        required=True,
        help="externally reviewed canonical split SHA-256",
    )
    parser.add_argument("--reveal", type=Path)
    args = parser.parse_args(argv)
    try:
        split = load_primary_corpus_split_file(args.split)
        if (
            len(args.expected_split_sha256) != 64
            or any(character not in "0123456789abcdef" for character in args.expected_split_sha256)
            or split.canonical_sha256 != args.expected_split_sha256
        ):
            raise PrimaryCorpusSplitError(
                "split canonical digest does not match --expected-split-sha256"
            )
        reveal = (
            load_primary_corpus_blind_reveal_file(args.reveal, split)
            if args.reveal is not None
            else None
        )
        baseline_digest = validate_split_source_baseline_file(
            split, args.source_baseline, reveal=reveal
        )
    except PrimaryCorpusSplitError as error:
        print(
            json.dumps(
                {"status": "invalid", "error": str(error)},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    print(
        json.dumps(
            {
                "status": "verified",
                "split_canonical_sha256": split.canonical_sha256,
                "source_baseline_canonical_sha256": baseline_digest,
                "blind_reveal_verified": reveal is not None,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
