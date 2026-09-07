#!/usr/bin/env bash
# Explicit lifecycle command for a local Surya 2 vLLM endpoint.  The PDF scan
# worker never invokes this script.  Runtime state is package-local and stop
# validates the tracked process command before signaling it.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT/scripts/surya_runtime_env.sh"
PYTHON="${SURYA_SERVER_PYTHON:-$ROOT/.venv-surya-server/bin/python}"
MODEL="${SURYA_MODEL_CHECKPOINT:-datalab-to/surya-ocr-2}"
HOST="${SURYA_VLLM_HOST:-127.0.0.1}"
PORT="${SURYA_VLLM_PORT:-8000}"
test -x "$PYTHON" || { echo "Missing server Python: $PYTHON. Run scripts/setup_surya_vllm_server.sh first." >&2; exit 1; }
RUNTIME="$ROOT/.runtime/surya-vllm"
mkdir -p "$RUNTIME"
PIDFILE="$RUNTIME/vllm.pid"
META="$RUNTIME/vllm.meta.json"
LOG="$RUNTIME/vllm.log"

matches_server() {
  local pid="$1"
  [[ -r "/proc/$pid/cmdline" ]] || return 1
  tr '\0' '\n' < "/proc/$pid/cmdline" | grep -qx 'vllm.entrypoints.openai.api_server'
}
if [[ -f "$PIDFILE" ]]; then
  old="$(<"$PIDFILE")"
  if kill -0 "$old" 2>/dev/null && matches_server "$old"; then
    echo "Surya vLLM server already running (pid=$old). Use scripts/status_surya_endpoint.sh or scripts/stop_surya_vllm.sh." >&2
    exit 1
  fi
  rm -f "$PIDFILE"
fi

echo "Starting caller-managed Surya vLLM endpoint at http://$HOST:$PORT/v1"
echo "Model downloads, if needed, happen now under HF_HOME=$HF_HOME (not during PDF scans)."
setsid "$PYTHON" -m vllm.entrypoints.openai.api_server \
  --model "$MODEL" --served-model-name "$MODEL" --host "$HOST" --port "$PORT" \
  --dtype "${VLLM_DTYPE:-bfloat16}" \
  --gpu-memory-utilization "${VLLM_GPU_MEMORY_UTILIZATION:-0.85}" \
  --max-model-len "${VLLM_MAX_MODEL_LEN:-18000}" >"$LOG" 2>&1 &
PID=$!
printf '%s\n' "$PID" > "$PIDFILE"
printf '{"pid":%s,"host":"%s","port":%s,"model":"%s"}\n' "$PID" "$HOST" "$PORT" "$MODEL" > "$META"
echo "pid=$PID log=$LOG"
echo "Check readiness: SURYA_INFERENCE_URL=http://$HOST:$PORT scripts/status_surya_endpoint.sh"
