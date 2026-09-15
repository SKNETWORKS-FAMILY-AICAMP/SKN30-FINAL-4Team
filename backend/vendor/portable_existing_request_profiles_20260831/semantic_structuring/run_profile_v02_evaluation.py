"""Run deterministic v0.2 regression evaluation; no model call is made."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from semantic_structuring.evaluation_v02 import evaluate_profile_v02


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--selection-artifact", type=Path, required=True)
    parser.add_argument("--gold", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--allow-draft", action="store_true", help="evaluate a Gold fixture before human freeze")
    args = parser.parse_args()

    payload = evaluate_profile_v02(
        json.loads(args.profile.read_text()),
        json.loads(args.selection_artifact.read_text()),
        args.gold,
        allow_draft=args.allow_draft,
    )
    encoded = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded)
    print(encoded)


if __name__ == "__main__":
    main()
