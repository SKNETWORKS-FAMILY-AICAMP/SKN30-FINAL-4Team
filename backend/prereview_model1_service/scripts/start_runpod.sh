#!/usr/bin/env bash
# Start one loopback-only resident Model 1 process.  Tailscale Serve ownership
# remains outside this package so this cannot alter the existing Surya ingress.
{ set +x; } 2>/dev/null
set -euo pipefail
umask 077

readonly worker_root="/workspace/project/prereview-model1/worker"
readonly run_root="/run/prereview-model1"
readonly bearer_file="$run_root/api-bearer-token"
readonly pid_file="$run_root/model1.pid"
readonly log_file="$worker_root/logs/model1.log"
[[ -x "$worker_root/.venv-model1/bin/python" ]] || { printf 'run setup_runpod.sh first\n' >&2; exit 1; }
[[ -f "$bearer_file" && ! -L "$bearer_file" && "$(stat -c '%u:%a:%h' "$bearer_file")" == "$(id -u):600:1" ]] || { printf 'Model 1 bearer file is unsafe\n' >&2; exit 1; }
token="$(<"$bearer_file")"
[[ "$token" =~ ^[A-Za-z0-9_-]{32,512}$ ]] || { printf 'Model 1 bearer token is invalid\n' >&2; exit 1; }
mkdir -p "$run_root" "$worker_root/logs"; chmod 0700 "$run_root"
if [[ -f "$pid_file" ]] && kill -0 "$(<"$pid_file")" 2>/dev/null; then printf 'Model 1 service already running\n' >&2; exit 1; fi
rm -f "$pid_file"
PREREVIEW_MODEL1_API_BEARER_TOKEN="$token" PREREVIEW_MODEL1_DEVICE="${PREREVIEW_MODEL1_DEVICE:-cuda}" PYTHONPATH="$worker_root/backend" \
  setsid "$worker_root/.venv-model1/bin/python" -m uvicorn prereview_model1_service.entrypoint:app \
    --host 127.0.0.1 --port 8791 --workers 1 --no-access-log \
    </dev/null >>"$log_file" 2>&1 &
pid="$!"; printf '%s\n' "$pid" > "$pid_file"; chmod 0600 "$pid_file"
sleep 1
kill -0 "$pid" 2>/dev/null || { rm -f "$pid_file"; printf 'Model 1 service failed; inspect protected log\n' >&2; exit 1; }
printf 'Model 1 service started on 127.0.0.1:8791\n'
