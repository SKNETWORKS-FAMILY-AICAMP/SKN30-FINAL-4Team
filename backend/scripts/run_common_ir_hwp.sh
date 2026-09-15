#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_ENV="$ROOT/vendor/common_ir_pipeline/.venv"

[[ -x "$PYTHON_ENV/bin/common-ir-rhwp" ]] || {
  echo "Common IR uv environment is missing; create it first." >&2
  exit 1
}

# rhwp-python bundles an older FreeType shared object.  Preloading the host
# library provides FT_Palette_Data_Get, required by the current wheel.  The
# override keeps the runner portable to a non-standard RunPod base image.
FREETYPE_LIB="${PREREVIEW_FREETYPE_LIB:-/lib/x86_64-linux-gnu/libfreetype.so.6}"
[[ -r "$FREETYPE_LIB" ]] || {
  echo "FreeType library not found: $FREETYPE_LIB" >&2
  echo "Set PREREVIEW_FREETYPE_LIB to the host libfreetype.so.6 path." >&2
  exit 1
}
export LD_PRELOAD="${LD_PRELOAD:+$LD_PRELOAD:}$FREETYPE_LIB"
exec "$PYTHON_ENV/bin/common-ir-rhwp" "$@"
