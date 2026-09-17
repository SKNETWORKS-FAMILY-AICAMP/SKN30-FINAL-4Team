#!/usr/bin/env bash
# Start the RunPod Serverless handler after the local Surya vLLM service is ready.

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

resolve_worker_layout
ensure_deployment_tree
read_config_into_environment

[[ "${PREREVIEW_RUNPOD_SERVERLESS_MANAGED:-}" == "true" ]] || die \
  "this launcher is only for a RunPod Serverless-managed image; an SSH Pod does not register an endpoint"

readonly MODULE="prereview_runpod_worker.surya_layout_worker.entrypoint"
[[ -x "$PREREVIEW_RUNPOD_CLIENT_VENV/bin/python" ]] || die "client venv is not ready; run setup_runpod.sh"

"$SCRIPT_DIR/status_surya_vllm.sh" >/dev/null

# Validate one strict JSON document and cold-start composition before placing
# the process under a RunPod supervisor.  The Python code deliberately emits
# only a fixed status here; config topology or malformed values never echo.
"$PREREVIEW_RUNPOD_CLIENT_VENV/bin/python" - <<'PY'
from prereview_runpod_worker.surya_layout_worker.entrypoint import safe_handler

if not safe_handler.ready:
    raise SystemExit("RunPod Surya worker configuration is invalid or unavailable")
print("RunPod Surya worker cold-start composition verified")
PY

note "Starting the foreground RunPod Serverless handler. It uses no DB or service-role credential."
exec "$PREREVIEW_RUNPOD_CLIENT_VENV/bin/python" -m "$MODULE"
