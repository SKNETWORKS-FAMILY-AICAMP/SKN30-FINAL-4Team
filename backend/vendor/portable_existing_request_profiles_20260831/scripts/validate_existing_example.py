#!/usr/bin/env python3
"""Validate the shipped Existing v0.2 example against its Common IR text."""

from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from semantic_structuring.profile_v02 import validate_profile_v02


COMMON_IR = ROOT / "examples/existing/common_ir_hwp_v1.json"
PROFILE = ROOT / "examples/existing/structured_profile_v02.json"
SELECTION = ROOT / "examples/existing/source_selection_v02.json"


def main() -> None:
    document = json.loads(COMMON_IR.read_text(encoding="utf-8"))
    profile = json.loads(PROFILE.read_text(encoding="utf-8"))
    selection = json.loads(SELECTION.read_text(encoding="utf-8"))
    # v0.2 profile spans are CandidatePack-local.  The selection artifact keeps
    # the exact CandidatePack text used by server materialization; Common IR is
    # loaded independently below to confirm the upstream input is parseable.
    block_texts = selection.get("source_block_texts") or {}
    if not isinstance(block_texts, dict):
        raise SystemExit("Existing example has no CandidatePack source_block_texts")
    from semantic_structuring.common_ir_v1 import prepare_common_ir_v1

    prepare_common_ir_v1(document)
    issues = validate_profile_v02(profile, block_texts)
    if issues:
        raise SystemExit("Existing example validation failed:\n- " + "\n- ".join(issues))
    print("Existing example exact-span/provenance validation passed")


if __name__ == "__main__":
    main()
