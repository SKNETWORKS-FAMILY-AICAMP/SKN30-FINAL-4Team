#!/usr/bin/env bash
# Shared, intentionally small safety helpers for the Docker-free RunPod setup.
# This file is sourced by the sibling scripts; it is not an end-user entrypoint.

set -euo pipefail

readonly PREREVIEW_RUNPOD_DEFAULT_HOME="/workspace/project/prereview-surya/worker"
readonly PREREVIEW_RUNPOD_PERSISTENT_CACHE_ROOT="/workspace/persistent/prereview/cache"
readonly PREREVIEW_RUNPOD_RUNTIME_ROOT="/run/prereview-surya"

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

note() {
  printf '%s\n' "$*" >&2
}

require_clean_launcher_environment() {
  local name
  for name in PYTHONHOME PYTHONPATH UV_PROJECT_ENVIRONMENT LD_PRELOAD; do
    if [[ -n "${!name:-}" ]]; then
      die "unsafe inherited ${name}; unset it before running this deployment script"
    fi
  done
}

resolve_worker_layout() {
  require_clean_launcher_environment

  PREREVIEW_RUNPOD_WORKER_HOME="${PREREVIEW_RUNPOD_WORKER_HOME:-$PREREVIEW_RUNPOD_DEFAULT_HOME}"
  if [[ "$PREREVIEW_RUNPOD_WORKER_HOME" != "$PREREVIEW_RUNPOD_DEFAULT_HOME" ]]; then
    die "PREREVIEW_RUNPOD_WORKER_HOME must be ${PREREVIEW_RUNPOD_DEFAULT_HOME}"
  fi
  if [[ "$(realpath -m -- "$PREREVIEW_RUNPOD_WORKER_HOME")" != "$PREREVIEW_RUNPOD_DEFAULT_HOME" ]]; then
    die "worker home resolves outside the approved RunPod path"
  fi

  PREREVIEW_RUNPOD_BACKEND_DIR="$PREREVIEW_RUNPOD_WORKER_HOME/backend"
  PREREVIEW_RUNPOD_CLIENT_VENV="$PREREVIEW_RUNPOD_WORKER_HOME/.venv-surya-client"
  PREREVIEW_RUNPOD_VLLM_VENV="$PREREVIEW_RUNPOD_WORKER_HOME/.venv-surya-vllm"
  PREREVIEW_RUNPOD_RUN_DIR="$PREREVIEW_RUNPOD_RUNTIME_ROOT/pids"
  PREREVIEW_RUNPOD_LOG_DIR="$PREREVIEW_RUNPOD_WORKER_HOME/logs"
  PREREVIEW_RUNPOD_CONFIG_FILE="$PREREVIEW_RUNPOD_RUNTIME_ROOT/config.json"
  PREREVIEW_SURYA_ATTESTATION_DIR="$PREREVIEW_RUNPOD_RUNTIME_ROOT/attestations"
  PREREVIEW_SURYA_ATTESTATION_FILE="$PREREVIEW_SURYA_ATTESTATION_DIR/surya-vllm.json"

  export UV_CACHE_DIR="$PREREVIEW_RUNPOD_PERSISTENT_CACHE_ROOT/uv"
  export HF_HOME="$PREREVIEW_RUNPOD_PERSISTENT_CACHE_ROOT/huggingface"
  export HUGGINGFACE_HUB_CACHE="$HF_HOME/hub"
  export DATALAB_CACHE_DIR="$PREREVIEW_RUNPOD_PERSISTENT_CACHE_ROOT/datalab"
  export VLLM_CACHE_ROOT="$PREREVIEW_RUNPOD_PERSISTENT_CACHE_ROOT/vllm"
}

ensure_deployment_tree() {
  local runtime_path
  for runtime_path in \
    "$PREREVIEW_RUNPOD_RUNTIME_ROOT" \
    "$PREREVIEW_RUNPOD_RUN_DIR" \
    "$PREREVIEW_SURYA_ATTESTATION_DIR"; do
    [[ ! -L "$runtime_path" ]] || die "runtime directory must not be a symlink"
  done
  mkdir -p -- "$PREREVIEW_RUNPOD_WORKER_HOME" \
    "$PREREVIEW_RUNPOD_RUNTIME_ROOT" \
    "$PREREVIEW_RUNPOD_RUN_DIR" \
    "$PREREVIEW_RUNPOD_LOG_DIR" \
    "$UV_CACHE_DIR" \
    "$HUGGINGFACE_HUB_CACHE" \
    "$DATALAB_CACHE_DIR" \
    "$VLLM_CACHE_ROOT" \
    "$PREREVIEW_SURYA_ATTESTATION_DIR"
  chmod 0700 -- \
    "$PREREVIEW_RUNPOD_RUNTIME_ROOT" \
    "$PREREVIEW_RUNPOD_RUN_DIR" \
    "$PREREVIEW_SURYA_ATTESTATION_DIR"
  for runtime_path in \
    "$PREREVIEW_RUNPOD_RUNTIME_ROOT" \
    "$PREREVIEW_RUNPOD_RUN_DIR" \
    "$PREREVIEW_SURYA_ATTESTATION_DIR"; do
    [[ ! -L "$runtime_path" ]] || die "runtime directory must not be a symlink"
    [[ "$(stat -c '%u:%a' -- "$runtime_path")" == "$(id -u):700" ]] || die \
      "runtime directory ownership or permissions are unsafe"
  done
  [[ -f "$PREREVIEW_RUNPOD_BACKEND_DIR/pyproject.toml" ]] || die \
    "backend source is missing at $PREREVIEW_RUNPOD_BACKEND_DIR; copy or clone backend/ there first"
  [[ -f "$PREREVIEW_RUNPOD_BACKEND_DIR/vendor/common_ir_pipeline/pyproject.toml" ]] || die \
    "local common_ir_pipeline source is missing from the approved backend tree"
}

# Unlike ensure_deployment_tree, this helper never creates a directory or
# changes a permission.  Use it from status commands so an observation cannot
# make a broken deployment look healthy by recreating its runtime tree.
require_existing_deployment_tree() {
  local runtime_path
  for runtime_path in \
    "$PREREVIEW_RUNPOD_WORKER_HOME" \
    "$PREREVIEW_RUNPOD_RUNTIME_ROOT" \
    "$PREREVIEW_RUNPOD_RUN_DIR" \
    "$PREREVIEW_SURYA_ATTESTATION_DIR" \
    "$PREREVIEW_RUNPOD_LOG_DIR"; do
    [[ -d "$runtime_path" && ! -L "$runtime_path" ]] || return 1
  done
  [[ -f "$PREREVIEW_RUNPOD_BACKEND_DIR/pyproject.toml" ]] || return 1
  [[ -f "$PREREVIEW_RUNPOD_BACKEND_DIR/vendor/common_ir_pipeline/pyproject.toml" ]] || return 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "required command is unavailable: $1"
}

verify_a100_cuda() {
  require_command nvidia-smi
  require_command python3
  local gpu_names
  gpu_names="$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null)" || die "NVIDIA driver is unavailable"
  [[ "$gpu_names" == *"A100"* ]] || die "this deployment is pinned for an A100 pod; refusing an unverified GPU"

  python3 - <<'PY'
import torch

if not torch.cuda.is_available():
    raise SystemExit("global Python cannot access CUDA")
name = torch.cuda.get_device_name(0)
if "A100" not in name:
    raise SystemExit("global Python GPU is not A100")
print(f"CUDA verified: {name}; torch={torch.__version__}; torchvision check deferred")
PY
}

pid_is_live() {
  local pid_file="$1"
  local start_file="${pid_file}.start_ticks"
  [[ -f "$pid_file" && -f "$start_file" ]] || return 1
  local pid
  pid="$(<"$pid_file")"
  [[ "$pid" =~ ^[1-9][0-9]*$ ]] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  local recorded_start current_start
  recorded_start="$(<"$start_file")"
  [[ "$recorded_start" =~ ^[1-9][0-9]*$ ]] || return 1
  current_start="$(process_start_ticks "$pid")" || return 1
  [[ "$current_start" == "$recorded_start" ]]
}

process_start_ticks() {
  local pid="$1"
  [[ "$pid" =~ ^[1-9][0-9]*$ && -r "/proc/$pid/stat" ]] || return 1
  python3 - "$pid" <<'PY'
from pathlib import Path
import sys

pid = sys.argv[1]
raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
# comm is enclosed in parentheses and may itself contain spaces.  Fields after
# the final ") " begin at proc field 3; starttime is field 22, offset 19.
tail = raw.rsplit(") ", 1)[1].split()
if len(tail) < 20 or not tail[19].isdigit():
    raise SystemExit(1)
print(tail[19])
PY
}

require_exact_process_arguments() {
  local pid="$1"
  shift
  [[ -r "/proc/$pid/cmdline" ]] || return 1
  local -a arguments=()
  mapfile -d '' -t arguments < "/proc/$pid/cmdline"
  local required argument found
  for required in "$@"; do
    found=0
    for argument in "${arguments[@]}"; do
      if [[ "$argument" == "$required" ]]; then
        found=1
        break
      fi
    done
    (( found == 1 )) || return 1
  done
}

write_pid_file() {
  local pid_file="$1"
  local pid="$2"
  local start_ticks
  start_ticks="$(process_start_ticks "$pid")" || die "cannot bind PID record to process start time"
  umask 077
  printf '%s\n' "$pid" > "$pid_file"
  printf '%s\n' "$start_ticks" > "${pid_file}.start_ticks"
}

remove_pid_record() {
  local pid_file="$1"
  rm -f -- "$pid_file" "${pid_file}.start_ticks"
}

read_config_into_environment() {
  [[ -f "$PREREVIEW_RUNPOD_CONFIG_FILE" ]] || die \
    "missing $PREREVIEW_RUNPOD_CONFIG_FILE; copy config.example.json and replace every placeholder"
  [[ ! -L "$PREREVIEW_RUNPOD_CONFIG_FILE" ]] || die "config.json must not be a symlink"
  [[ "$(stat -c '%u' -- "$PREREVIEW_RUNPOD_CONFIG_FILE")" == "$(id -u)" ]] || die \
    "config.json must be owned by the current worker user"
  local config_mode
  config_mode="$(stat -c '%a' -- "$PREREVIEW_RUNPOD_CONFIG_FILE")"
  [[ "$config_mode" =~ ^[0-7]{3,4}$ ]] || die "cannot read config.json permissions"
  # No group/other permission.  The document has topology, not credentials,
  # but retaining a conservative local policy prevents accidental sharing.
  (( (8#$config_mode & 8#077) == 0 )) || die "config.json must not be group/world readable"
  export PREREVIEW_SURYA_WORKER_CONFIG_JSON
  PREREVIEW_SURYA_WORKER_CONFIG_JSON="$(<"$PREREVIEW_RUNPOD_CONFIG_FILE")"
  [[ -n "$PREREVIEW_SURYA_WORKER_CONFIG_JSON" ]] || die "config.json is empty"
}
