#!/usr/bin/env bash
# Prepare the two isolated environments on an A100 RunPod.  Never touches the
# image's global Python, CUDA driver, or system-wide packages.

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

resolve_worker_layout
ensure_deployment_tree
verify_a100_cuda
require_command uv

readonly WORKER_REQUIREMENTS="$PREREVIEW_RUNPOD_BACKEND_DIR/prereview_runpod_worker/requirements-worker.txt"
readonly VLLM_REQUIREMENTS="$PREREVIEW_RUNPOD_BACKEND_DIR/prereview_runpod_worker/requirements-vllm.txt"
[[ -f "$WORKER_REQUIREMENTS" && -f "$VLLM_REQUIREMENTS" ]] || die "deployment requirements files are missing"

note "Creating/reusing client venv: $PREREVIEW_RUNPOD_CLIENT_VENV"
uv venv --system-site-packages "$PREREVIEW_RUNPOD_CLIENT_VENV"

# v1 of this installer registered both projects as editable distributions.
# Remove only those two known metadata records when upgrading an existing
# worker tree; their source files remain in place and are exposed below by the
# reviewed .pth file.  This keeps a failed/partial v1 run from retaining the
# backend's unrelated API/DB dependency contract.
for stale_distribution in pre-review-backend common-ir-pipeline; do
  if "$PREREVIEW_RUNPOD_CLIENT_VENV/bin/python" - "$stale_distribution" <<'PY'
from importlib.metadata import PackageNotFoundError, version
import sys

try:
    version(sys.argv[1])
except PackageNotFoundError:
    raise SystemExit(1)
PY
  then
    uv pip uninstall --python "$PREREVIEW_RUNPOD_CLIENT_VENV" "$stale_distribution"
  fi
done

# This bundle needs only three source packages from the checked-out tree.  Do
# not register the whole backend (or its vendored project) as editable
# distributions: doing so makes their unrelated API/DB dependency metadata
# part of this GPU-only environment.  A fixed .pth file exposes exactly the
# two reviewed source roots while leaving dependency ownership here.
CLIENT_SITE_PACKAGES="$(
  "$PREREVIEW_RUNPOD_CLIENT_VENV/bin/python" - <<'PY'
from pathlib import Path
import sys
import sysconfig

prefix = Path(sys.prefix).resolve()
site_packages = Path(sysconfig.get_path("purelib")).resolve()
try:
    site_packages.relative_to(prefix)
except ValueError as exc:
    raise SystemExit(
        f"client site-packages resolves outside its venv: {site_packages}"
    ) from exc
print(site_packages)
PY
)"
readonly CLIENT_SITE_PACKAGES
readonly CLIENT_SOURCE_PATH_FILE="$CLIENT_SITE_PACKAGES/prereview-runpod-worker-source.pth"
[[ ! -L "$CLIENT_SOURCE_PATH_FILE" ]] || die "client source .pth must not be a symlink"
printf '%s\n%s\n' \
  "$PREREVIEW_RUNPOD_BACKEND_DIR" \
  "$PREREVIEW_RUNPOD_BACKEND_DIR/vendor/common_ir_pipeline/src" \
  > "$CLIENT_SOURCE_PATH_FILE"
chmod 0644 -- "$CLIENT_SOURCE_PATH_FILE"

# The RunPod SDK has a sizeable serverless/API dependency graph but no
# Torch/CUDA authority.  Resolve that graph normally; installing the SDK with
# --no-deps produces a worker that imports successfully only by accident on a
# pre-populated image.  Surya and its Torch-adjacent closure remain isolated in
# the explicit --no-deps requirements below.
uv pip install --python "$PREREVIEW_RUNPOD_CLIENT_VENV" "runpod==1.12.0"
uv pip install --python "$PREREVIEW_RUNPOD_CLIENT_VENV" --no-deps -r "$WORKER_REQUIREMENTS"

note "Creating/reusing isolated vLLM venv: $PREREVIEW_RUNPOD_VLLM_VENV"
uv venv "$PREREVIEW_RUNPOD_VLLM_VENV"
# vLLM owns this Torch 2.11/CUDA 13.0 environment.  Do not use
# --system-site-packages here and do not install backend code into it.
uv pip install --python "$PREREVIEW_RUNPOD_VLLM_VENV" -r "$VLLM_REQUIREMENTS"

# Do not silently discard a resolver metadata warning.  Some vLLM 0.20.1
# installations report an xgrammar metadata conflict after the reviewed
# Transformers 5.16.1 override.  The local adapter smoke is mandatory even
# when this command succeeds; a failure is printed as an operator warning,
# not reclassified as a successful dependency check.
if ! uv pip check --python "$PREREVIEW_RUNPOD_VLLM_VENV"; then
  note "WARNING: vLLM dependency metadata check reports a conflict (often xgrammar)."
  note "Do not hide it or promote this pod until smoke_surya_adapter.sh passes."
fi

# `uv pip check` currently treats the client venv's inherited system Torch and
# torchvision as absent.  Validate every worker pin through Python's combined
# environment metadata instead, then import and exercise the inherited CUDA
# runtime below.  This catches real omissions without a known false failure.
"$PREREVIEW_RUNPOD_CLIENT_VENV/bin/python" - "$WORKER_REQUIREMENTS" <<'PY'
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import re
import sys

requirements_path = Path(sys.argv[1])
expected_versions: dict[str, str] = {"runpod": "1.12.0"}
for line_number, raw_line in enumerate(
    requirements_path.read_text(encoding="utf-8").splitlines(), start=1
):
    line = raw_line.strip()
    if not line or line.startswith("#"):
        continue
    match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([^;\s]+)", line)
    if match is None:
        raise SystemExit(
            f"requirements-worker.txt:{line_number} must use an exact package==version pin"
        )
    expected_versions[match.group(1)] = match.group(2)

for distribution_name, expected in sorted(expected_versions.items()):
    try:
        actual = version(distribution_name)
    except PackageNotFoundError as exc:
        raise SystemExit(f"missing client distribution: {distribution_name}") from exc
    if actual != expected:
        raise SystemExit(
            f"client distribution mismatch: {distribution_name}={actual}; expected {expected}"
        )

import torch
import torchvision
from common_ir_pipeline.pdf_fusion.render_manifest import inspect_png_bytes
from prereview_runpod_worker.surya_layout_worker.handler import RunPodSuryaExecutionPolicy
from worker.contracts.accelerator import SuryaLayoutRequest

assert torch.cuda.is_available(), "client venv cannot see CUDA"
assert "A100" in torch.cuda.get_device_name(0), "client venv GPU is not A100"
assert callable(inspect_png_bytes)
assert RunPodSuryaExecutionPolicy.__name__ == "RunPodSuryaExecutionPolicy"
assert SuryaLayoutRequest.__name__ == "SuryaLayoutRequest"
print(f"client ready: torch={torch.__version__}; torchvision={torchvision.__version__}")
PY

"$PREREVIEW_RUNPOD_VLLM_VENV/bin/python" - <<'PY'
from importlib.metadata import version
import torch

assert torch.cuda.is_available(), "vLLM venv cannot see CUDA"
assert "A100" in torch.cuda.get_device_name(0), "vLLM venv GPU is not A100"
assert version("vllm") == "0.20.1"
assert version("transformers") == "5.16.1"
assert version("huggingface-hub") == "1.31.0"
assert version("tokenizers") == "0.23.1"
print(f"vLLM ready: torch={torch.__version__}; cuda={torch.version.cuda}")
PY

note "Setup complete. On this direct pod, run scripts/start_surya_vllm.sh, then scripts/smoke_surya_adapter.sh."
note "For the persistent Tailscale service, inject its bearer secret and run scripts/start_persistent_api.sh."
note "Run scripts/start_runpod_worker.sh only inside a RunPod Serverless managed worker image."
