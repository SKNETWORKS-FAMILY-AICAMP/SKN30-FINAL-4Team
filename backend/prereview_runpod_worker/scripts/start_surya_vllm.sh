#!/usr/bin/env bash
# Start the pinned Surya vLLM model only on the pod loopback interface.

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

resolve_worker_layout
ensure_deployment_tree
verify_a100_cuda

readonly PID_FILE="$PREREVIEW_RUNPOD_RUN_DIR/surya-vllm.pid"
readonly LOG_FILE="$PREREVIEW_RUNPOD_LOG_DIR/surya-vllm.log"
readonly MODEL_ID="datalab-to/surya-ocr-2"
readonly MODEL_REVISION="3b3d4cdf88d6928b0acdc75181b13206ea67c4a3"
readonly MODEL_SHA256="5755f82a997dd0b111964fa8b31cc2daef7aeb7a706bbd17d73d6a93ef3f723e"
readonly MODEL_FILE="$HUGGINGFACE_HUB_CACHE/models--datalab-to--surya-ocr-2/snapshots/$MODEL_REVISION/model.safetensors"
readonly MODELS_URL="http://127.0.0.1:8000/v1/models"
readonly STARTUP_POLL_ATTEMPTS=600
readonly STARTUP_POLL_INTERVAL_SECONDS=2

[[ -x "$PREREVIEW_RUNPOD_VLLM_VENV/bin/vllm" ]] || die "vLLM venv is not ready; run setup_runpod.sh"
require_command curl
require_command sha256sum
require_command setsid

publish_attestation() {
  local server_pid="$1"
  [[ "$server_pid" =~ ^[1-9][0-9]*$ ]] || die "invalid vLLM PID for attestation"
  [[ -f "$MODEL_FILE" ]] || die \
    "vLLM is ready but the pinned model file is not in the expected immutable snapshot"
  [[ "$(sha256sum -- "$MODEL_FILE" | awk '{print $1}')" == "$MODEL_SHA256" ]] || die \
    "pinned model.safetensors SHA-256 verification failed"

  PREREVIEW_ATTEST_SERVER_PID="$server_pid" \
  PREREVIEW_ATTEST_MODELS_URL="$MODELS_URL" \
  PREREVIEW_ATTEST_MODEL_ID="$MODEL_ID" \
  PREREVIEW_ATTEST_MODEL_REVISION="$MODEL_REVISION" \
  PREREVIEW_ATTEST_MODEL_SHA256="$MODEL_SHA256" \
  PREREVIEW_ATTEST_OUTPUT="$PREREVIEW_SURYA_ATTESTATION_FILE" \
    "$PREREVIEW_RUNPOD_VLLM_VENV/bin/python" - <<'PY'
import json
import os
import pathlib
import tempfile
import urllib.request

models_url = os.environ["PREREVIEW_ATTEST_MODELS_URL"]
model_id = os.environ["PREREVIEW_ATTEST_MODEL_ID"]
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
with opener.open(models_url, timeout=3) as response:
    body = response.read(65_537)
if len(body) > 65_536:
    raise SystemExit("vLLM /v1/models response exceeds the attestation cap")
payload = json.loads(body)
data = payload.get("data") if isinstance(payload, dict) else None
served_ids = {
    item.get("id")
    for item in data
    if isinstance(item, dict) and isinstance(item.get("id"), str)
} if isinstance(data, list) else set()
if served_ids != {model_id}:
    raise SystemExit("vLLM served model identity does not match the pinned model")

attestation = {
    "schema_version": "prereview.surya-vllm-attestation/v1",
    "endpoint_url": "http://127.0.0.1:8000/v1",
    "producer": {
        "model_id": model_id,
        "model_revision": os.environ["PREREVIEW_ATTEST_MODEL_REVISION"],
        "model_weights_sha256": os.environ["PREREVIEW_ATTEST_MODEL_SHA256"],
    },
    "vllm_served_model_id": model_id,
    "server_pid": int(os.environ["PREREVIEW_ATTEST_SERVER_PID"]),
}
output = pathlib.Path(os.environ["PREREVIEW_ATTEST_OUTPUT"])
descriptor, temporary = tempfile.mkstemp(prefix=".surya-vllm.", dir=output.parent)
try:
    os.fchmod(descriptor, 0o600)
    encoded = (json.dumps(attestation, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    with os.fdopen(descriptor, "wb", closefd=True) as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    descriptor = -1
    os.replace(temporary, output)
finally:
    if descriptor >= 0:
        os.close(descriptor)
    try:
        os.unlink(temporary)
    except FileNotFoundError:
        pass
PY
  [[ -f "$PREREVIEW_SURYA_ATTESTATION_FILE" ]] || die "vLLM attestation publication failed"
  [[ "$(stat -c '%a' -- "$PREREVIEW_SURYA_ATTESTATION_FILE")" == "600" ]] || die \
    "vLLM attestation permissions are unsafe"
}

if pid_is_live "$PID_FILE"; then
  pid="$(<"$PID_FILE")"
  require_exact_process_arguments "$pid" "serve" "$MODEL_ID" || die "PID file does not point to the expected Surya vLLM command"
  require_exact_process_arguments "$pid" "--revision" "$MODEL_REVISION" || die \
    "running Surya vLLM process is not bound to the pinned revision"
  curl --noproxy '*' --fail --silent --show-error --max-time 3 "$MODELS_URL" >/dev/null || die "Surya vLLM PID is live but /v1/models is not ready"
  publish_attestation "$pid"
  note "Surya vLLM is already ready (pid $pid)."
  exit 0
fi
if [[ -e "$PID_FILE" ]]; then
  die "stale/invalid PID file at $PID_FILE; inspect it and remove that exact file manually"
fi
if curl --noproxy '*' --fail --silent --show-error --max-time 2 "$MODELS_URL" >/dev/null; then
  die "127.0.0.1:8000 is already serving an unknown process; do not reuse it"
fi

note "Starting Surya vLLM on 127.0.0.1:8000 (model download may take several minutes)."
setsid bash -c '
  export HF_HOME HUGGINGFACE_HUB_CACHE DATALAB_CACHE_DIR VLLM_CACHE_ROOT
  exec "$1" serve "$2" \
    --revision "$3" \
    --served-model-name "$2" \
    --host 127.0.0.1 \
    --port 8000 \
    --dtype bfloat16 \
    --max-model-len 18000 \
    --gpu-memory-utilization 0.85
' prereview-surya-vllm "$PREREVIEW_RUNPOD_VLLM_VENV/bin/vllm" "$MODEL_ID" "$MODEL_REVISION" \
  </dev/null >>"$LOG_FILE" 2>&1 &
pid="$!"
write_pid_file "$PID_FILE" "$pid"

for _ in $(seq 1 "$STARTUP_POLL_ATTEMPTS"); do
  if ! kill -0 "$pid" 2>/dev/null; then
    die "Surya vLLM exited; inspect $LOG_FILE (PID file retained for diagnosis)"
  fi
  if curl --noproxy '*' --fail --silent --show-error --max-time 3 "$MODELS_URL" >/dev/null; then
    break
  fi
  sleep "$STARTUP_POLL_INTERVAL_SECONDS"
done
curl --noproxy '*' --fail --silent --show-error --max-time 3 "$MODELS_URL" >/dev/null || die \
  "Surya vLLM did not become ready within 1200 seconds; inspect $LOG_FILE"

publish_attestation "$pid"
note "Surya vLLM ready (pid $pid); model revision and weights SHA-256 verified."
