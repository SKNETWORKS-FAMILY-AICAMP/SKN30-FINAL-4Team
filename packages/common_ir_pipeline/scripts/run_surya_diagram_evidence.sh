#!/usr/bin/env bash
# Example: SURYA_INFERENCE_URL=http://127.0.0.1:8000 ./scripts/run_surya_diagram_evidence.sh --pdf /abs/notice.pdf --output /abs/diagram-evidence.json --pages 2
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT/scripts/surya_runtime_env.sh"
test -n "${SURYA_INFERENCE_URL:-}" || { echo "SURYA_INFERENCE_URL is required; this wrapper never autostarts vLLM." >&2; exit 2; }
export SURYA_INFERENCE_AUTOSTART=false
PYTHON="${SURYA_PYTHON:-$ROOT/.venv-surya/bin/python}"
exec "$PYTHON" -m common_ir_pipeline.workers.pdf_surya_diagram_evidence "$@"
