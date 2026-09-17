#!/usr/bin/env bash
# Read-only health status for the local Surya vLLM service.

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

resolve_worker_layout
require_existing_deployment_tree || die "RunPod deployment tree is missing or unsafe"
readonly PID_FILE="$PREREVIEW_RUNPOD_RUN_DIR/surya-vllm.pid"
readonly MODELS_URL="http://127.0.0.1:8000/v1/models"
require_command curl

if ! pid_is_live "$PID_FILE"; then
  note "Surya vLLM is not running (no live managed PID)."
  exit 1
fi
pid="$(<"$PID_FILE")"
if ! require_exact_process_arguments "$pid" "serve" "datalab-to/surya-ocr-2"; then
  note "Surya vLLM PID file exists but command identity is unsafe."
  exit 2
fi
if ! require_exact_process_arguments "$pid" "--revision" "3b3d4cdf88d6928b0acdc75181b13206ea67c4a3"; then
  note "Surya vLLM process is not bound to the pinned model revision."
  exit 2
fi
if ! curl --noproxy '*' --fail --silent --show-error --max-time 3 "$MODELS_URL" >/dev/null; then
  note "Surya vLLM process is alive but the loopback endpoint is not ready."
  exit 3
fi
if [[ ! -f "$PREREVIEW_SURYA_ATTESTATION_FILE" || -L "$PREREVIEW_SURYA_ATTESTATION_FILE" ]] \
  || [[ "$(stat -c '%a' -- "$PREREVIEW_SURYA_ATTESTATION_FILE")" != "600" ]]; then
  note "Surya vLLM attestation is missing or unsafe."
  exit 4
fi
note "Surya vLLM ready (pid $pid, loopback only)."
