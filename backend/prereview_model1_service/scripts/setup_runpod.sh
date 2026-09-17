#!/usr/bin/env bash
# Create the Model 1-only venv.  It intentionally does not touch Surya/Gemma.
set -euo pipefail
umask 077

readonly worker_root="/workspace/project/prereview-model1/worker"
readonly venv="$worker_root/.venv-model1"
[[ -d "$worker_root/backend/prereview_model1_service" && ! -L "$worker_root" ]] || { printf 'Model 1 source bundle is missing\n' >&2; exit 1; }
command -v uv >/dev/null 2>&1 || { printf 'uv is required\n' >&2; exit 1; }
nvidia-smi --query-gpu=name --format=csv,noheader | grep -q . || { printf 'CUDA GPU is unavailable\n' >&2; exit 1; }
uv venv --system-site-packages "$venv"
# No torch requirement is supplied: CUDA Torch remains owned by the RunPod image.
uv pip install --python "$venv" -r "$worker_root/backend/prereview_model1_service/requirements-model1.txt"
PYTHONPATH="$worker_root/backend" "$venv/bin/python" - <<'PY'
import torch
assert torch.cuda.is_available(), "Model 1 venv cannot see CUDA"
import numpy
import pandas
from prereview_model1_service.runtime import EXPECTED_WEIGHT_SHA256
assert numpy.__version__ == "2.4.4"
assert pandas.__version__ == "3.0.3"
print(f"Model 1 venv ready; CUDA={torch.cuda.get_device_name(0)}; numpy={numpy.__version__}; pandas={pandas.__version__}; weight={EXPECTED_WEIGHT_SHA256[:12]}")
PY
