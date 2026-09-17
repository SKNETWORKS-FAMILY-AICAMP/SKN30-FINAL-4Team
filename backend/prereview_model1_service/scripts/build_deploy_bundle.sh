#!/usr/bin/env bash
# Build a reproducible, secret-free source bundle for the resident Model 1 API.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPOSITORY_ROOT="$(realpath -- "$SCRIPT_DIR/../../..")"

if [[ "$#" != 1 || "$1" != /* ]]; then
  printf 'usage: %s /absolute/output.tar.gz\n' "$0" >&2
  exit 2
fi
output="$1"
[[ ! -e "$output" && ! -L "$output" ]] || { printf 'refusing to overwrite output\n' >&2; exit 1; }
parent="$(dirname -- "$output")"
[[ -d "$parent" && ! -L "$parent" ]] || { printf 'output parent is unsafe\n' >&2; exit 1; }
case "$(realpath -m -- "$output")" in "$REPOSITORY_ROOT"|"$REPOSITORY_ROOT"/*) printf 'output must be outside checkout\n' >&2; exit 1;; esac

readonly -a sources=(
  backend/prereview_model1_service
  ml/pipelines/model1/dl07_m1_apply.py
)
for source in "${sources[@]}"; do
  [[ -e "$REPOSITORY_ROOT/$source" ]] || { printf 'missing source: %s\n' "$source" >&2; exit 1; }
done
umask 077
(
  cd -- "$REPOSITORY_ROOT"
  tar --sort=name --mtime='UTC 1970-01-01' --owner=0 --group=0 --numeric-owner \
    --exclude='*/__pycache__' --exclude='*.py[co]' -cf - "${sources[@]}"
) | gzip -n > "$output"
sha256sum -- "$output"
