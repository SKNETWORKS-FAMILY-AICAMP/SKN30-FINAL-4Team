#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/examples/request/request_selection_dry_run.json"

cd "$ROOT"
uv run python -m semantic_structuring.run_request_profile_v012 \
  --common-ir examples/request/common_ir_v1.json \
  --profile-id request:PREREVIEW-TEST-2027-03 \
  --dry-run \
  --output "$OUT"

echo "Wrote: $OUT"
