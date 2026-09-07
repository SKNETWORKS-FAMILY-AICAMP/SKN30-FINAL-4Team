#!/usr/bin/env bash
# Explicit, opt-in predownload for the endpoint model.  Never called by the
# scanner.  This downloads only to the package-local HF cache configured by
# surya_runtime_env.sh.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT/scripts/surya_runtime_env.sh"
PYTHON="${SURYA_PYTHON:-$ROOT/.venv-surya/bin/python}"
MODEL="${SURYA_MODEL_CHECKPOINT:-datalab-to/surya-ocr-2}"
test -x "$PYTHON" || { echo "Missing Surya Python: $PYTHON. Run scripts/setup_surya_gpu.sh first." >&2; exit 1; }
echo "Downloading model=$MODEL into HF_HOME=$HF_HOME"
exec "$PYTHON" -m huggingface_hub.cli.hf download "$MODEL"
