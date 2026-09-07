#!/usr/bin/env bash
# Explicit vLLM endpoint-server setup.  It does not start a server or
# download model weights.  Set SURYA_SERVER_VENV to use another location.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
VENV="${SURYA_SERVER_VENV:-$ROOT/.venv-surya-server}"
uv venv "$VENV"
echo "Install the CUDA/Torch/vLLM build compatible with this host if needed."
uv pip install --python "$VENV/bin/python" -e '.[surya-server]'
echo "Server environment created at $VENV. Start only with scripts/start_surya_vllm.sh."
