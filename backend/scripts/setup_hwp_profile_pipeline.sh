#!/usr/bin/env bash
# Provision only the non-Docker HWP/HWPX profile pipeline environments.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
COMMON_IR_ROOT="$ROOT/backend/vendor/common_ir_pipeline"
SEMANTIC_ROOT="$ROOT/backend/vendor/portable_existing_request_profiles_20260831"
PYTHON_VERSION="${PREREVIEW_PYTHON_VERSION:-3.12}"

command -v uv >/dev/null || {
  echo "uv is required. Install it first: https://docs.astral.sh/uv/" >&2
  exit 1
}
command -v jq >/dev/null || {
  echo "jq is required. On Ubuntu: apt-get update && apt-get install -y jq" >&2
  exit 1
}

[[ -r "${PREREVIEW_FREETYPE_LIB:-/lib/x86_64-linux-gnu/libfreetype.so.6}" ]] || {
  echo "libfreetype.so.6 is required by rhwp-python." >&2
  echo "Install FreeType or set PREREVIEW_FREETYPE_LIB before processing HWP/HWPX." >&2
  exit 1
}

cd "$COMMON_IR_ROOT"
uv venv .venv --python "$PYTHON_VERSION"
uv pip install --python .venv/bin/python -e '.[hwp]'

cd "$SEMANTIC_ROOT"
uv venv .venv --python "$PYTHON_VERSION"
uv sync --active

echo "Setup complete. Copy .env.example to .env, set OPENAI_API_KEY, then run:"
echo "  backend/scripts/run_existing_hwp_pipeline.sh /path/to/PBLN_..."
