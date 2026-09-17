#!/usr/bin/env bash
# Build a minimal, secret-free RunPod source bundle from an explicit allowlist.

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPOSITORY_ROOT="$(realpath -- "$SCRIPT_DIR/../../..")"

if [[ "$#" -ne 1 ]]; then
  printf 'usage: %s /absolute/output.tar.gz\n' "$0" >&2
  exit 2
fi

OUTPUT="$1"
if [[ "$OUTPUT" != /* ]]; then
  printf 'ERROR: output path must be absolute\n' >&2
  exit 1
fi
if [[ -e "$OUTPUT" || -L "$OUTPUT" ]]; then
  printf 'ERROR: refusing to overwrite existing output: %s\n' "$OUTPUT" >&2
  exit 1
fi
OUTPUT_PARENT="$(dirname -- "$OUTPUT")"
[[ -d "$OUTPUT_PARENT" && ! -L "$OUTPUT_PARENT" ]] || {
  printf 'ERROR: output parent must be an existing non-symlink directory\n' >&2
  exit 1
}
RESOLVED_OUTPUT="$(realpath -m -- "$OUTPUT")"
case "$RESOLVED_OUTPUT" in
  "$REPOSITORY_ROOT"|"$REPOSITORY_ROOT"/*)
    printf 'ERROR: output must be outside the repository tree: %s\n' "$OUTPUT" >&2
    exit 1
    ;;
esac

readonly -a SOURCES=(
  "backend/pyproject.toml"
  "backend/prereview_runpod_worker"
  "backend/worker/__init__.py"
  "backend/worker/vendor.py"
  "backend/worker/contracts/__init__.py"
  "backend/worker/contracts/accelerator.py"
  "backend/vendor/common_ir_pipeline/pyproject.toml"
  "backend/vendor/common_ir_pipeline/src/common_ir_pipeline"
)

for source in "${SOURCES[@]}"; do
  [[ -e "$REPOSITORY_ROOT/$source" ]] || {
    printf 'ERROR: required bundle source is missing: %s\n' "$source" >&2
    exit 1
  }
done

umask 077
(
  cd -- "$REPOSITORY_ROOT"
  tar \
    --sort=name \
    --mtime='UTC 1970-01-01' \
    --owner=0 \
    --group=0 \
    --numeric-owner \
    --exclude='*/__pycache__' \
    --exclude='*.py[co]' \
    -cf - \
    "${SOURCES[@]}"
) | gzip -n > "$OUTPUT"

sha256sum -- "$OUTPUT"
