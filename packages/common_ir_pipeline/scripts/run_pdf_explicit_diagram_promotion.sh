#!/usr/bin/env bash
# Thin wrapper around the conservative literal-arrow promotion CLI.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${COMMON_IR_PYTHON:-$ROOT/.venv/bin/python}"
exec "$PYTHON" -m common_ir_pipeline.workers.promote_pdf_only_explicit_diagram_edges "$@"
