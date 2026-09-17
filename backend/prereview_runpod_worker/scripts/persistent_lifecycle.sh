#!/usr/bin/env bash
# Systemd-free lifecycle supervisor for the persistent RunPod Surya service.
# `run` is the one foreground supervisor; the other verbs operate on it.

{ set +x; } 2>/dev/null
set -euo pipefail
umask 077

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

readonly SUPERVISOR_PID_NAME="persistent-supervisor.pid"
readonly API_PID_NAME="persistent-api.pid"
readonly API_LOG_NAME="persistent-api.log"
readonly API_BEARER_FILE="$PREREVIEW_RUNPOD_RUNTIME_ROOT/api-bearer-token"
readonly TAILSCALE_AUTH_FILE="$PREREVIEW_RUNPOD_RUNTIME_ROOT/tailscale-auth-key"
readonly API_HEALTH_URL="http://127.0.0.1:8787/health"
readonly READY_FILE="$PREREVIEW_RUNPOD_RUNTIME_ROOT/persistent-ready"
readonly CONFIG_DIGEST_MARKER="$PREREVIEW_RUNPOD_RUNTIME_ROOT/config.sha256"
readonly ACTION_LOCK_NAME="persistent-lifecycle.lock"
readonly LIFETIME_LOCK_NAME="persistent-supervisor.lock"
readonly READY_TIMEOUT_SECONDS=1260

supervisor_pid_file() { printf '%s/%s\n' "$PREREVIEW_RUNPOD_RUN_DIR" "$SUPERVISOR_PID_NAME"; }
api_pid_file() { printf '%s/%s\n' "$PREREVIEW_RUNPOD_RUN_DIR" "$API_PID_NAME"; }
api_log_file() { printf '%s/%s\n' "$PREREVIEW_RUNPOD_LOG_DIR" "$API_LOG_NAME"; }
lock_file() { printf '%s/%s\n' "$PREREVIEW_RUNPOD_RUNTIME_ROOT" "$ACTION_LOCK_NAME"; }
lifetime_lock_file() { printf '%s/%s\n' "$PREREVIEW_RUNPOD_RUNTIME_ROOT" "$LIFETIME_LOCK_NAME"; }

is_exact_supervisor() {
  require_exact_process_arguments "$1" "$SCRIPT_DIR/persistent_lifecycle.sh" _run_locked
}

is_exact_api() {
  require_exact_process_arguments "$1" -m prereview_runpod_worker.persistent_api.entrypoint
}

is_process_group_leader() {
  local pid="$1"
  python3 - "$pid" <<'PY'
from pathlib import Path
import sys
pid = sys.argv[1]
try:
    tail = Path(f"/proc/{pid}/stat").read_text(encoding="ascii").rsplit(") ", 1)[1].split()
    # tail begins with field 3; process group is field 5, offset 2.
    raise SystemExit(0 if len(tail) > 2 and tail[2] == pid else 1)
except Exception:
    raise SystemExit(1)
PY
}

write_bootstrap_secret() {
  local destination="$1" value="$2" pattern="$3"
  [[ "$value" =~ $pattern ]] || die "bootstrap runtime secret is invalid"
  [[ ! -L "$destination" ]] || die "bootstrap runtime destination is unsafe"
  local temporary
  temporary="$(mktemp "$PREREVIEW_RUNPOD_RUNTIME_ROOT/.bootstrap.XXXXXX")"
  chmod 0600 -- "$temporary"
  printf '%s\n' "$value" > "$temporary"
  mv -f -- "$temporary" "$destination"
  chmod 0600 -- "$destination"
}

read_protected_single_line() {
  local file="$1" pattern="$2" value="" extra="" metadata type owner mode links size fd
  [[ ! -L "$file" && -f "$file" ]] || die "runtime secret file is invalid"
  exec {fd}<"$file" || die "runtime secret file is invalid"
  metadata="$(stat -Lc '%F:%u:%a:%h:%s' -- "/proc/$$/fd/$fd")" || die "runtime secret file is invalid"
  IFS=: read -r type owner mode links size <<< "$metadata"
  [[ "$type" == "regular file" && "$owner" == "$(id -u)" && "$mode" == 600 && "$links" == 1 && "$size" =~ ^[0-9]+$ && size -le 1024 ]] || die "runtime secret file is invalid"
  IFS= read -r value <&"$fd" || true
  if IFS= read -r extra <&"$fd" || [[ ! "$value" =~ $pattern ]]; then exec {fd}<&-; die "runtime secret file is invalid"; fi
  exec {fd}<&-
  printf '%s' "$value"
}

bootstrap_runtime_secrets() {
  # These are the only secret-bearing environment inputs accepted here.  They
  # are immediately materialized into fixed /run files and unset before a
  # service child is launched.  Existing protected files support a restart
  # without re-exporting a value.
  [[ -z "${PREREVIEW_SURYA_API_BEARER_TOKEN:-}" ]] || die \
    "persistent API bearer must be supplied by its runtime file"
  local rejected_name
  for rejected_name in TS_AUTHKEY TAILSCALE_AUTH_KEY TAILSCALE_AUTHKEY; do
    [[ -z "${!rejected_name:-}" ]] || die "Tailscale auth key must be supplied by its runtime file"
  done
  local api_value="${PREREVIEW_RUNPOD_BOOTSTRAP_API_BEARER_TOKEN:-}"
  local auth_value="${PREREVIEW_RUNPOD_BOOTSTRAP_TAILSCALE_AUTH_KEY:-}"
  # Nothing external may run after these local copies until the exports are
  # gone.  Service children can consequently read only the fixed /run files.
  unset PREREVIEW_RUNPOD_BOOTSTRAP_API_BEARER_TOKEN PREREVIEW_RUNPOD_BOOTSTRAP_TAILSCALE_AUTH_KEY
  if [[ -n "$api_value" ]]; then
    [[ "$api_value" =~ ^[A-Za-z0-9_-]{32,512}$ ]] || die "bootstrap runtime secret is invalid"
    if [[ -e "$API_BEARER_FILE" ]]; then [[ "$(read_protected_single_line "$API_BEARER_FILE" '^[A-Za-z0-9_-]{32,512}$')" == "$api_value" ]] || die "existing API bearer differs; refusing journal-key rotation"; else write_bootstrap_secret "$API_BEARER_FILE" "$api_value" '^[A-Za-z0-9_-]{32,512}$'; fi
  fi
  if [[ -n "$auth_value" ]]; then
    [[ "$auth_value" =~ ^tskey-auth-[A-Za-z0-9_-]{16,1000}$ ]] || die "bootstrap runtime secret is invalid"
    # A live userspace state is bound to this ephemeral auth key too; never
    # replace a protected existing key with a different bootstrap value.
    if [[ -e "$TAILSCALE_AUTH_FILE" ]]; then [[ "$(read_protected_single_line "$TAILSCALE_AUTH_FILE" '^tskey-auth-[A-Za-z0-9_-]{16,1000}$')" == "$auth_value" ]] || die "existing Tailscale auth key differs; refusing replacement"; else write_bootstrap_secret "$TAILSCALE_AUTH_FILE" "$auth_value" '^tskey-auth-[A-Za-z0-9_-]{16,1000}$'; fi
  fi
  [[ -f "$API_BEARER_FILE" && ! -L "$API_BEARER_FILE" ]] || die "persistent API bearer runtime file is missing"
  [[ "$(stat -c '%a' -- "$API_BEARER_FILE")" == 600 ]] || die \
    "runtime secret file permissions are unsafe"
  if [[ -e "$TAILSCALE_AUTH_FILE" ]]; then [[ ! -L "$TAILSCALE_AUTH_FILE" && "$(stat -c '%a' -- "$TAILSCALE_AUTH_FILE")" == 600 ]] || die "Tailscale auth key runtime file is unsafe"; fi
}

bootstrap_config() {
  local source="${PREREVIEW_RUNPOD_CONFIG_SOURCE_FILE:-}" digest="${PREREVIEW_RUNPOD_CONFIG_SHA256:-}" actual example_digest metadata size temporary bootstrap_requested=0
  unset PREREVIEW_RUNPOD_CONFIG_SOURCE_FILE PREREVIEW_RUNPOD_CONFIG_SHA256
  if [[ -n "$source" || -n "$digest" ]]; then
    bootstrap_requested=1
    [[ -n "$source" && "$digest" =~ ^[a-f0-9]{64}$ ]] || die "config bootstrap inputs are invalid"
    [[ -f "$source" && ! -L "$source" ]] || die "config source file is invalid"
    metadata="$(stat -c '%F:%u:%h:%s' -- "$source")"; IFS=: read -r _ _ _ size <<< "$metadata"
    [[ "$metadata" =~ ^regular\ file:$(id -u):1:[0-9]+$ && "$size" -le 1048576 ]] || die "config source file is invalid"
    actual="$(sha256sum -- "$source" | awk '{print $1}')"; [[ "$actual" == "$digest" ]] || die "config source digest mismatch"
    example_digest="$(sha256sum -- "$PREREVIEW_RUNPOD_WORKER_HOME/backend/prereview_runpod_worker/config.example.json" | awk '{print $1}')"
    [[ "$actual" != "$example_digest" ]] || die "config example placeholder is not a deployable source"
    if [[ -e "$PREREVIEW_RUNPOD_CONFIG_FILE" ]]; then [[ ! -L "$PREREVIEW_RUNPOD_CONFIG_FILE" && "$(stat -c '%F:%u:%a:%h:%s' -- "$PREREVIEW_RUNPOD_CONFIG_FILE")" =~ ^regular\ file:$(id -u):600:1:[0-9]+$ && "$(sha256sum -- "$PREREVIEW_RUNPOD_CONFIG_FILE" | awk '{print $1}')" == "$digest" ]] || die "existing runtime config differs; refusing replacement"; else temporary="$(mktemp "$PREREVIEW_RUNPOD_RUNTIME_ROOT/.config.XXXXXX")"; chmod 0600 "$temporary"; cp -- "$source" "$temporary"; chmod 0600 "$temporary"; [[ "$(sha256sum -- "$temporary" | awk '{print $1}')" == "$digest" ]] || { rm -f -- "$temporary"; die "config source changed during copy"; }; mv -f "$temporary" "$PREREVIEW_RUNPOD_CONFIG_FILE"; fi
  fi
  [[ -f "$PREREVIEW_RUNPOD_CONFIG_FILE" && ! -L "$PREREVIEW_RUNPOD_CONFIG_FILE" && "$(stat -c '%a' -- "$PREREVIEW_RUNPOD_CONFIG_FILE")" == 600 ]] || die "runtime config file is missing or unsafe"
  actual="$(sha256sum -- "$PREREVIEW_RUNPOD_CONFIG_FILE" | awk '{print $1}')"
  if [[ -e "$CONFIG_DIGEST_MARKER" ]]; then
    [[ ! -L "$CONFIG_DIGEST_MARKER" && "$(stat -c '%F:%u:%a:%h:%s' -- "$CONFIG_DIGEST_MARKER")" =~ ^regular\ file:$(id -u):600:1:65$ && "$(<"$CONFIG_DIGEST_MARKER")" == "$actual" ]] || die "runtime config digest marker is invalid"
  else
    (( bootstrap_requested == 1 )) || die "runtime config digest marker is missing"
    temporary="$(mktemp "$PREREVIEW_RUNPOD_RUNTIME_ROOT/.config-digest.XXXXXX")"; chmod 0600 "$temporary"; printf '%s\n' "$actual" >"$temporary"; mv -f "$temporary" "$CONFIG_DIGEST_MARKER"
  fi
  if ! "$PREREVIEW_RUNPOD_CLIENT_VENV/bin/python" - "$PREREVIEW_RUNPOD_CONFIG_FILE" <<'PY'
import json, sys
try:
    value=json.load(open(sys.argv[1], encoding="utf-8"))
    proxy=value["http"]["proxy_url"]
except Exception: raise SystemExit(1)
if proxy not in {"http://127.0.0.1:1056", "socks5://127.0.0.1:1055", "socks5h://127.0.0.1:1055"}: raise SystemExit(1)
PY
  then
    die "runtime config proxy_url is not the lifecycle listener"
  fi
}

ready_marker_is_valid() { [[ -f "$READY_FILE" && ! -L "$READY_FILE" && "$(stat -c '%a' -- "$READY_FILE")" == 600 && "$(<"$READY_FILE")" == "prereview.persistent-ready/v1" ]]; }
publish_ready_marker() { local temp; temp="$(mktemp "$PREREVIEW_RUNPOD_RUNTIME_ROOT/.ready.XXXXXX")"; chmod 0600 "$temp"; printf '%s\n' prereview.persistent-ready/v1 >"$temp"; mv -f "$temp" "$READY_FILE"; }

api_is_ready() {
  local file pid
  file="$(api_pid_file)"
  pid_is_live "$file" || return 1
  pid="$(<"$file")"
  is_exact_api "$pid" || return 1
  curl --noproxy '*' --fail --silent --show-error --max-time 3 "$API_HEALTH_URL" | python3 -c '
import json, sys
try:
    body=sys.stdin.buffer.read(65_537)
    if len(body) > 65_536: raise ValueError()
    value=json.loads(body)
    if not isinstance(value, dict) or value.get("status") != "ok" or value.get("ready") is not True or value.get("gpu_concurrency") != 1: raise ValueError()
except Exception:
    raise SystemExit(1)
'
}

start_api() {
  require_command curl
  require_command setsid
  local file pid
  file="$(api_pid_file)"
  if pid_is_live "$file"; then
    pid="$(<"$file")"
    is_exact_api "$pid" || die "persistent API PID file command identity is unsafe"
    api_is_ready || die "persistent API is live but not ready"
    return 0
  fi
  [[ ! -e "$file" ]] || die "stale/invalid persistent API PID file; inspect it before removal"
  if curl --noproxy '*' --fail --silent --show-error --max-time 2 "$API_HEALTH_URL" >/dev/null; then
    die "127.0.0.1:8787 is already serving an unknown process; do not reuse it"
  fi
  setsid "$SCRIPT_DIR/start_persistent_api.sh" </dev/null >>"$(api_log_file)" 2>&1 &
  pid="$!"
  write_pid_file "$file" "$pid"
  for _ in $(seq 1 60); do
    api_is_ready && { note "Persistent API ready (pid $pid)."; return 0; }
    kill -0 "$pid" 2>/dev/null || die "persistent API exited; inspect $(api_log_file)"
    sleep 1
  done
  die "persistent API did not become ready within 60 seconds; inspect $(api_log_file)"
}

stop_api() {
  local file pid
  file="$(api_pid_file)"
  if ! pid_is_live "$file"; then
    note "No live managed persistent API process. PID file is left untouched for inspection."
    return 0
  fi
  pid="$(<"$file")"
  if ! is_exact_api "$pid"; then
    note "refusing to signal an unsafe persistent API PID"
    return 1
  fi
  kill -TERM "$pid"
  # The API owns job fencing and its configured graceful shutdown window; do
  # not turn a slow GPU request into data loss with SIGKILL.
  for _ in $(seq 1 300); do
    if ! kill -0 "$pid" 2>/dev/null; then
      remove_pid_record "$file"
      note "Persistent API stopped cleanly."
      return 0
    fi
    sleep 1
  done
  note "persistent API did not stop within 300 seconds; inspect it manually (no SIGKILL issued)"
  return 1
}

status_api() {
  resolve_worker_layout
  require_existing_deployment_tree || { note "RunPod deployment tree is missing or unsafe."; return 1; }
  if ! api_is_ready; then
    note "Persistent API is not ready."
    return 1
  fi
  note "Persistent API ready (pid $(<"$(api_pid_file)"))."
}

with_action_lock() {
  resolve_worker_layout
  ensure_deployment_tree
  require_command flock
  local lock_fd
  [[ ! -L "$(lock_file)" ]] || die "lifecycle action lock path is unsafe"
  exec {lock_fd}>"$(lock_file)"
  flock -w 5 "$lock_fd" || die "another lifecycle action is in progress"
  set +e; "$@"; local result=$?; set -e
  flock -u "$lock_fd"
  exec {lock_fd}>&-
  return "$result"
}


shutdown_children() {
  local result=0
  "$SCRIPT_DIR/tailscale_userspace.sh" serve-off || result=1
  stop_api || result=1
  "$SCRIPT_DIR/stop_surya_vllm.sh" || result=1
  "$SCRIPT_DIR/tailscale_userspace.sh" stop || result=1
  return "$result"
}

run() {
  [[ "$#" -eq 0 ]] || die "usage: $0 run"
  resolve_worker_layout
  ensure_deployment_tree
  require_command python3
  require_command flock
  [[ ! -L "$(lifetime_lock_file)" ]] || die "lifecycle lifetime lock path is unsafe"
  local lifetime_fd
  exec {lifetime_fd}>"$(lifetime_lock_file)"
  flock -n "$lifetime_fd" || die "a lifecycle supervisor already owns the lifetime lock"
  # Materialize and validate all bootstrap inputs before exec. These helpers
  # unset their exported inputs; wrapper and supervisor children see only the
  # fixed /run files, never a bootstrap secret environment variable.
  bootstrap_runtime_secrets
  bootstrap_config
  # The wrapper receives the locked FD, makes it non-inheritable, then spawns
  # _run_locked with close_fds=True while forwarding Pod shutdown signals.
  exec python3 "$SCRIPT_DIR/lifecycle_lock_wrapper.py" --lock-fd "$lifetime_fd" -- \
    "$SCRIPT_DIR/persistent_lifecycle.sh" _run_locked
}

_run_locked() {
  [[ "$#" -eq 0 ]] || die "internal lifecycle supervisor arguments are invalid"
  resolve_worker_layout
  ensure_deployment_tree
  require_command curl
  local supervisor_file api_pid
  supervisor_file="$(supervisor_pid_file)"
  if pid_is_live "$supervisor_file"; then
    die "a managed lifecycle supervisor is already live"
  fi
  [[ ! -e "$supervisor_file" ]] || die "stale/invalid supervisor PID file; inspect it before removal"
  write_pid_file "$supervisor_file" "$$"
  PREREVIEW_LIFECYCLE_CLEANED=0
  PREREVIEW_LIFECYCLE_SUPERVISOR_FILE="$supervisor_file"
  cleanup() {
    (( ${PREREVIEW_LIFECYCLE_CLEANED:-0} == 0 )) || return 0
    PREREVIEW_LIFECYCLE_CLEANED=1
    rm -f -- "$READY_FILE"
    shutdown_children || true
    remove_pid_record "$PREREVIEW_LIFECYCLE_SUPERVISOR_FILE"
  }
  trap 'cleanup; exit 0' HUP INT TERM
  trap 'cleanup' EXIT

  "$SCRIPT_DIR/tailscale_userspace.sh" start
  "$SCRIPT_DIR/start_surya_vllm.sh"
  start_api
  "$SCRIPT_DIR/tailscale_userspace.sh" serve-on
  publish_ready_marker
  api_pid="$(<"$(api_pid_file)")"
  note "Persistent lifecycle supervisor is ready (pid $$)."
  # A foreground supervisor must also notice a dependent process disappearing;
  # otherwise a live API could accept work after vLLM or tailnet access failed.
  while kill -0 "$api_pid" 2>/dev/null; do
    if ! ready_marker_is_valid || ! api_is_ready || ! "$SCRIPT_DIR/tailscale_userspace.sh" status >/dev/null || ! "$SCRIPT_DIR/tailscale_userspace.sh" serve-status >/dev/null || \
      ! "$SCRIPT_DIR/status_surya_vllm.sh" >/dev/null; then
      note "A managed persistent service is no longer ready; shutting down dependent services."
      exit 1
    fi
    sleep 5
  done
  wait "$api_pid" || true
  note "Persistent API exited; shutting down dependent services."
  exit 1
}

start_locked() {
  local file pid
  file="$(supervisor_pid_file)"
  if pid_is_live "$file"; then
    pid="$(<"$file")"
    is_exact_supervisor "$pid" || die "supervisor PID file command identity is unsafe"
    note "Persistent lifecycle supervisor is already running (pid $pid)."
    printf '%s\n' "$pid"
    return 0
  fi
  [[ ! -e "$file" ]] || die "stale/invalid supervisor PID file; inspect it before removal"
  require_command setsid
  setsid "$SCRIPT_DIR/persistent_lifecycle.sh" run </dev/null >>"$(api_log_file)" 2>&1 &
  pid="$!"
  for _ in $(seq 1 10); do
    if pid_is_live "$file"; then
      is_exact_supervisor "$(<"$file")" || die "supervisor command identity is unsafe"
      note "Persistent lifecycle supervisor started (pid $(<"$file"))."
      printf '%s\n' "$(<"$file")"
      return 0
    fi
    kill -0 "$pid" 2>/dev/null || die "persistent lifecycle supervisor exited; inspect $(api_log_file)"
    sleep 1
  done
  die "persistent lifecycle supervisor did not publish a managed PID"
}

wait_for_ready() { local pid="$1"; for _ in $(seq 1 "$READY_TIMEOUT_SECONDS"); do if ready_marker_is_valid && api_is_ready && "$SCRIPT_DIR/tailscale_userspace.sh" status >/dev/null && "$SCRIPT_DIR/status_surya_vllm.sh" >/dev/null && "$SCRIPT_DIR/tailscale_userspace.sh" serve-status >/dev/null; then note "Persistent lifecycle supervisor is fully ready (pid $pid)."; return 0; fi; kill -0 "$pid" 2>/dev/null || die "persistent lifecycle supervisor exited before readiness; inspect $(api_log_file)"; sleep 1; done; die "persistent lifecycle supervisor did not become ready within ${READY_TIMEOUT_SECONDS} seconds"; }

start() { local pid; resolve_worker_layout; pid="$(with_action_lock start_locked)"; [[ "$pid" =~ ^[1-9][0-9]*$ ]] || die "supervisor did not publish a valid PID"; wait_for_ready "$pid"; }

signal_stop_locked() {
  local file pid
  file="$(supervisor_pid_file)"
  if ! pid_is_live "$file"; then
    note "No live managed persistent lifecycle supervisor. PID file is left untouched for inspection."
    return 10
  fi
  pid="$(<"$file")"
  is_exact_supervisor "$pid" || die "refusing to signal an unsafe lifecycle supervisor PID"
  is_process_group_leader "$pid" || die "refusing to signal supervisor that is not its process-group leader"
  # Startup helpers can be foreground descendants of _run_locked for up to a
  # cold vLLM timeout. Signal the validated supervisor group so its trap runs
  # promptly; separately setsid-managed API/vLLM/tailscaled are shut down by
  # that trap in the normal Serve→API→vLLM→tailscaled order.
  kill -TERM -- "-$pid"
  printf '%s\n' "$pid"
}

wait_for_supervisor_stop() {
  local pid="$1" file
  [[ "$pid" =~ ^[1-9][0-9]*$ ]] || return 0
  file="$(supervisor_pid_file)"
  for _ in $(seq 1 315); do
    if ! kill -0 "$pid" 2>/dev/null; then
      # _run_locked has exited, but its Python lock wrapper may need one brief
      # scheduling turn to close the inherited lifetime FD. Do not race a
      # restart against that final release.
      local lifetime_fd
      for _ in $(seq 1 50); do
        exec {lifetime_fd}>"$(lifetime_lock_file)"
        if flock -n "$lifetime_fd"; then
          flock -u "$lifetime_fd"
          exec {lifetime_fd}>&-
          note "Persistent lifecycle supervisor stopped cleanly."
          return 0
        fi
        exec {lifetime_fd}>&-
        sleep 0.1
      done
      die "persistent lifecycle wrapper did not release its lock after supervisor exit"
    fi
    sleep 1
  done
  die "persistent lifecycle supervisor did not stop within 315 seconds; inspect it manually (no SIGKILL issued)"
}

stop() {
  local pid status_code
  set +e
  pid="$(with_action_lock signal_stop_locked)"
  status_code=$?
  set -e
  if (( status_code == 10 )); then
    return 0
  fi
  (( status_code == 0 )) || return "$status_code"
  # The flock protects only identity validation and SIGTERM delivery.  A
  # potentially long graceful GPU/API shutdown never monopolizes the lock.
  wait_for_supervisor_stop "$pid"
}

status() {
  resolve_worker_layout
  require_existing_deployment_tree || { note "RunPod deployment tree is missing or unsafe."; return 1; }
  local file pid result=0
  file="$(supervisor_pid_file)"
  if ! pid_is_live "$file"; then
    note "Persistent lifecycle supervisor is not running."
    result=1
  else
    pid="$(<"$file")"
    if is_exact_supervisor "$pid"; then note "Persistent lifecycle supervisor ready (pid $pid)."; else note "Lifecycle supervisor PID identity is unsafe."; result=1; fi
  fi
  "$SCRIPT_DIR/tailscale_userspace.sh" status || result=1
  ready_marker_is_valid || { note "Persistent lifecycle ready marker is missing or unsafe."; result=1; }
  "$SCRIPT_DIR/tailscale_userspace.sh" serve-status || result=1
  "$SCRIPT_DIR/status_surya_vllm.sh" || result=1
  status_api || result=1
  return "$result"
}

restart() {
  stop
  start
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  case "${1:-}" in
    run) run "${@:2}" ;;
    _run_locked) _run_locked "${@:2}" ;;
    start) start ;;
    stop) stop ;;
    restart) restart ;;
    status) status ;;
    *) die "usage: $0 {run|start|status|stop|restart}" ;;
  esac
fi
