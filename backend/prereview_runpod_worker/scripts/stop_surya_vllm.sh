#!/usr/bin/env bash
# Stop only the exact managed local Surya vLLM command.  It never uses kill -9.

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

resolve_worker_layout
ensure_deployment_tree
readonly PID_FILE="$PREREVIEW_RUNPOD_RUN_DIR/surya-vllm.pid"

if ! pid_is_live "$PID_FILE"; then
  note "No live managed Surya vLLM process. PID file is left untouched for inspection."
  exit 0
fi
pid="$(<"$PID_FILE")"
require_exact_process_arguments "$pid" "serve" "datalab-to/surya-ocr-2" || die \
  "refusing to signal pid $pid: command is not the expected local Surya vLLM service"

kill -TERM "$pid"
for _ in $(seq 1 20); do
  if ! kill -0 "$pid" 2>/dev/null; then
    remove_pid_record "$PID_FILE"
    rm -f -- "$PREREVIEW_SURYA_ATTESTATION_FILE"
    note "Surya vLLM stopped cleanly."
    exit 0
  fi
  sleep 1
done
die "Surya vLLM did not stop within 20 seconds; inspect it manually (no SIGKILL issued)"
