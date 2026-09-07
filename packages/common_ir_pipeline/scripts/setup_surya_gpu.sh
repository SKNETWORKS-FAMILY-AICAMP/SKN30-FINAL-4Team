#!/usr/bin/env bash
# Create the optional Surya GPU client environment.  This does not start a
# server or download models.  The caller must first install a CUDA-compatible
# PyTorch into this environment when required by its target GPU.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

VENV="${SURYA_VENV:-$ROOT/.venv-surya}"
uv venv "$VENV"
echo "Install a CUDA-compatible PyTorch build into $VENV before the next command if your platform requires it."
uv pip install --python "$VENV/bin/python" -e '.[surya]'
echo "Client setup complete. This script did not start vLLM or download a Surya model."
echo "Verify GPU visibility: $VENV/bin/python -c 'import torch; print(torch.cuda.is_available())'"
