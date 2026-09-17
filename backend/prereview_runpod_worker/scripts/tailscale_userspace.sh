#!/usr/bin/env bash
# Manage the one userspace-networking tailscaled instance for this Pod.
# It never configures Funnel; ingress is restricted to Tailscale Serve.

set -euo pipefail
umask 077

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

readonly PID_FILE_NAME="tailscaled.pid"
readonly STATE_FILE="$PREREVIEW_RUNPOD_RUNTIME_ROOT/tailscaled.state"
readonly VAR_ROOT="$PREREVIEW_RUNPOD_RUNTIME_ROOT/tailscale-var"
readonly LEGACY_STATE_FILE="/run/tailscale/tailscaled.state"
readonly LEGACY_SOCKET_FILE="/run/tailscale/tailscaled.sock"
readonly SOCKET_FILE="$PREREVIEW_RUNPOD_RUNTIME_ROOT/tailscaled.sock"
readonly AUTH_KEY_FILE="$PREREVIEW_RUNPOD_RUNTIME_ROOT/tailscale-auth-key"
readonly LOG_FILE_NAME="tailscaled.log"
readonly SOCKS_LISTENER="127.0.0.1:1055"
readonly HTTP_PROXY_LISTENER="127.0.0.1:1056"

tailscale_bin() { command -v tailscale || die "required command is unavailable: tailscale"; }
tailscaled_bin() { command -v tailscaled || die "required command is unavailable: tailscaled"; }

require_tail_identity() {
  [[ "${PREREVIEW_RUNPOD_TAILSCALE_HOSTNAME:-}" =~ ^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$ ]] || die "Tailscale hostname is invalid"
  [[ "${PREREVIEW_RUNPOD_TAILSCALE_DNS_NAME:-}" =~ ^[a-z0-9]([a-z0-9.-]{0,250}[a-z0-9])?\.ts\.net\.?$ ]] || die "Tailscale DNS name is invalid"
}

node_status_is_expected() {
  require_tail_identity
  "$(tailscale_bin)" --socket="$SOCKET_FILE" status --json | PREREVIEW_TS_HOSTNAME="$PREREVIEW_RUNPOD_TAILSCALE_HOSTNAME" PREREVIEW_TS_DNS_NAME="$PREREVIEW_RUNPOD_TAILSCALE_DNS_NAME" python3 -c '
import json, os, sys
p=json.load(sys.stdin); s=p.get("Self")
if not isinstance(s, dict) or p.get("BackendState") != "Running" or s.get("Online") is not True: raise SystemExit(1)
if s.get("HostName") != os.environ["PREREVIEW_TS_HOSTNAME"]: raise SystemExit(1)
if s.get("DNSName") != os.environ["PREREVIEW_TS_DNS_NAME"]: raise SystemExit(1)
'
}

node_authentication_state() {
  "$(tailscale_bin)" --socket="$SOCKET_FILE" status --json | python3 -c '
import json, sys
p=json.load(sys.stdin); s=p.get("Self")
if p.get("BackendState") == "NeedsLogin" or p.get("NeedsLogin") is True: raise SystemExit(10)
if isinstance(s, dict) and s.get("Online") is True and p.get("BackendState") == "Running": raise SystemExit(0)
raise SystemExit(11)
'
}

serve_status_is_expected() {
  local mode="${1:-on}"
  require_tail_identity
  "$(tailscale_bin)" --socket="$SOCKET_FILE" serve status --json | PREREVIEW_TS_DNS_NAME="$PREREVIEW_RUNPOD_TAILSCALE_DNS_NAME" PREREVIEW_TS_SERVE_MODE="$mode" python3 -c '
import json, os, sys
p=json.load(sys.stdin); web=p.get("Web", {}); tcp=p.get("TCP", {}); funnel=p.get("AllowFunnel", {})
if any(bool(v) for v in (funnel.values() if isinstance(funnel, dict) else [funnel])): raise SystemExit(1)
if not isinstance(web, dict) or not isinstance(tcp, dict): raise SystemExit(1)
if os.environ["PREREVIEW_TS_SERVE_MODE"] == "off":
    raise SystemExit(0 if not web and not tcp else 1)
if tcp != {"443": {"HTTPS": True}} or len(web) != 1: raise SystemExit(1)
host, paths=next(iter(web.items())); dns=os.environ["PREREVIEW_TS_DNS_NAME"].rstrip(".")
if host != dns + ":443" or not isinstance(paths, dict) or set(paths) != {"Handlers"}: raise SystemExit(1)
handlers=paths["Handlers"]
if not isinstance(handlers, dict) or set(handlers) != {"/"}: raise SystemExit(1)
h=handlers["/"]
if not isinstance(h, dict) or set(h) != {"Proxy"} or h["Proxy"] != "http://127.0.0.1:8787": raise SystemExit(1)
'
}

validate_existing_state_file() {
  local metadata type owner mode links size
  [[ -f "$STATE_FILE" && ! -L "$STATE_FILE" ]] || die "tailscaled state file is unsafe"
  metadata="$(stat -c '%F:%u:%a:%h:%s' -- "$STATE_FILE")"; IFS=: read -r type owner mode links size <<< "$metadata"
  [[ "$type" == "regular file" && "$owner" == "$(id -u)" && "$mode" == 600 && "$links" == 1 && "$size" =~ ^[1-9][0-9]*$ && "$size" -le 10485760 ]] || die "tailscaled state file is unsafe"
}

migrate_legacy_state_if_needed() {
  [[ ! -e "$STATE_FILE" ]] || return 0
  [[ -e "$LEGACY_STATE_FILE" ]] || return 0
  [[ ! -S "$LEGACY_SOCKET_FILE" ]] || die "legacy tailscaled socket is live; Serve off and stop the legacy daemon before migration"
  if python3 - "$LEGACY_STATE_FILE" <<'PY'
import os, pathlib, sys
state = sys.argv[1]
for entry in pathlib.Path("/proc").iterdir():
    if not entry.name.isdigit():
        continue
    try:
        argv = (entry / "cmdline").read_bytes().split(b"\0")[:-1]
    except OSError:
        continue
    if not argv or not argv[0].endswith(b"tailscaled"):
        continue
    text = [part.decode("utf-8", "surrogateescape") for part in argv]
    if f"--state={state}" in text or any(
        text[index] == "--state" and index + 1 < len(text) and text[index + 1] == state
        for index in range(len(text))
    ):
        raise SystemExit(0)
raise SystemExit(1)
PY
  then
    die "legacy tailscaled daemon is live; Serve off and stop it before migration"
  fi
  local metadata type owner mode links size temporary
  [[ -f "$LEGACY_STATE_FILE" && ! -L "$LEGACY_STATE_FILE" ]] || die "legacy tailscaled state file is unsafe"
  metadata="$(stat -c '%F:%u:%a:%h:%s' -- "$LEGACY_STATE_FILE")"; IFS=: read -r type owner mode links size <<< "$metadata"
  [[ "$type" == "regular file" && "$owner" == "$(id -u)" && "$mode" == 600 && "$links" == 1 && "$size" =~ ^[1-9][0-9]*$ && "$size" -le 10485760 ]] || die "legacy tailscaled state file is unsafe"
  temporary="$(mktemp "$PREREVIEW_RUNPOD_RUNTIME_ROOT/.tailscaled-state.XXXXXX")"; chmod 0600 "$temporary"; cp -- "$LEGACY_STATE_FILE" "$temporary"; chmod 0600 "$temporary"; mv -f "$temporary" "$STATE_FILE"
  validate_existing_state_file
}

pid_file() { printf '%s/%s\n' "$PREREVIEW_RUNPOD_RUN_DIR" "$PID_FILE_NAME"; }
log_file() { printf '%s/%s\n' "$PREREVIEW_RUNPOD_LOG_DIR" "$LOG_FILE_NAME"; }

ensure_var_root() {
  [[ ! -L "$VAR_ROOT" ]] || die "tailscaled var root is unsafe"
  mkdir -p -- "$VAR_ROOT"
  chmod 0700 -- "$VAR_ROOT"
  [[ -d "$VAR_ROOT" && ! -L "$VAR_ROOT" && "$(stat -c '%u:%a' -- "$VAR_ROOT")" == "$(id -u):700" ]] || die "tailscaled var root is unsafe"
}

require_var_root() {
  [[ -d "$VAR_ROOT" && ! -L "$VAR_ROOT" && "$(stat -c '%u:%a' -- "$VAR_ROOT")" == "$(id -u):700" ]] || die "tailscaled var root is unsafe"
}

validate_auth_key_file() {
  local file="$1" secret="" extra="" metadata type owner mode links size fd
  [[ ! -L "$file" && -f "$file" ]] || die "Tailscale auth key file is invalid"
  exec {fd}<"$file" || die "Tailscale auth key file is invalid"
  metadata="$(stat -Lc '%F:%u:%a:%h:%s' -- "/proc/$$/fd/$fd")" || {
    exec {fd}<&-
    die "Tailscale auth key file is invalid"
  }
  IFS=: read -r type owner mode links size <<< "$metadata"
  if [[ "$type" != "regular file" || "$owner" != "$(id -u)" || "$mode" != "600" || \
    "$links" != "1" || ! "$size" =~ ^[0-9]+$ || size -lt 24 || size -gt 1024 ]]; then
    exec {fd}<&-
    die "Tailscale auth key file is invalid"
  fi
  IFS= read -r secret <&"$fd" || true
  if IFS= read -r extra <&"$fd" || [[ ! "$secret" =~ ^tskey-auth-[A-Za-z0-9_-]{16,1000}$ ]]; then
    exec {fd}<&-
    die "Tailscale auth key file is invalid"
  fi
  exec {fd}<&-
}

is_exact_tailscaled() {
  local pid="$1"
  require_exact_process_arguments "$pid" "--state=$STATE_FILE" "--socket=$SOCKET_FILE" \
    "--statedir=$VAR_ROOT" \
    "--tun=userspace-networking" "--socks5-server=$SOCKS_LISTENER" \
    "--outbound-http-proxy-listen=$HTTP_PROXY_LISTENER"
}

daemon_is_live() {
  local file
  file="$(pid_file)"
  pid_is_live "$file" && is_exact_tailscaled "$(<"$file")"
}

start() {
  resolve_worker_layout
  ensure_deployment_tree
  require_command setsid
  require_command python3
  require_tail_identity
  ensure_var_root
  local file pid needs_auth=1 auth_state=11
  file="$(pid_file)"
  migrate_legacy_state_if_needed
  if [[ -e "$STATE_FILE" ]]; then validate_existing_state_file; fi
  if pid_is_live "$file"; then
    pid="$(<"$file")"
    is_exact_tailscaled "$pid" || die "tailscaled PID file command identity is unsafe"
  else
    [[ ! -e "$file" ]] || die "stale/invalid tailscaled PID file; inspect it before removal"
    [[ ! -e "$SOCKET_FILE" ]] || die "tailscaled control socket already exists without a managed PID"
    setsid "$(tailscaled_bin)" \
      "--state=$STATE_FILE" \
      "--socket=$SOCKET_FILE" \
      "--statedir=$VAR_ROOT" \
      --tun=userspace-networking \
      "--socks5-server=$SOCKS_LISTENER" \
      "--outbound-http-proxy-listen=$HTTP_PROXY_LISTENER" \
      </dev/null >>"$(log_file)" 2>&1 &
    pid="$!"
    write_pid_file "$file" "$pid"
  fi

  for _ in $(seq 1 30); do
    [[ -S "$SOCKET_FILE" ]] && break
    kill -0 "$pid" 2>/dev/null || die "tailscaled exited; inspect $(log_file)"
    sleep 1
  done
  [[ -S "$SOCKET_FILE" ]] || die "tailscaled did not create its control socket"
  for _ in $(seq 1 30); do
    set +e; node_authentication_state >/dev/null 2>&1; auth_state=$?; set -e
    if (( auth_state == 0 )); then needs_auth=0; break; fi
    if (( auth_state == 10 )); then needs_auth=1; break; fi
    sleep 1
  done
  (( auth_state == 0 || auth_state == 10 )) || die "Tailscale state did not settle before authentication"
  if (( needs_auth == 0 )); then
    "$(tailscale_bin)" --socket="$SOCKET_FILE" up --reset --hostname="$PREREVIEW_RUNPOD_TAILSCALE_HOSTNAME" \
      --accept-dns=false --accept-routes=false --netfilter-mode=off --shields-up=false \
      >/dev/null 2>&1 || die "Tailscale startup failed"
  else
    validate_auth_key_file "$AUTH_KEY_FILE"
    # file: makes the CLI read the fixed protected runtime file.  The key value
    # never appears in an argv vector, log, or inherited child environment.
    "$(tailscale_bin)" --socket="$SOCKET_FILE" up --reset --hostname="$PREREVIEW_RUNPOD_TAILSCALE_HOSTNAME" \
      --auth-key="file:$AUTH_KEY_FILE" --accept-dns=false --accept-routes=false --netfilter-mode=off --shields-up=false \
      >/dev/null 2>&1 || die "Tailscale authentication failed"
  fi
  node_status_is_expected >/dev/null 2>&1 || die \
    "Tailscale is not ready"
  # A persisted state may retain a Serve configuration.  It must be completely
  # off before any GPU/API process is spawned; Funnel is rejected by the same
  # strict parser.  Stop this daemon immediately on a bad precondition.
  if serve_status_is_expected off >/dev/null 2>&1; then
    :
  elif serve_status_is_expected on >/dev/null 2>&1; then
    # Exact lifecycle-owned pre-crash Serve is recoverable: remove it before
    # the new API starts, then demand the fully-off precondition again.
    serve_off
    serve_status_is_expected off >/dev/null 2>&1 || { stop || true; die "persisted Tailscale Serve shutdown is unsafe"; }
  else
    stop || true
    die "persisted Tailscale Serve/Funnel configuration is unsafe"
  fi
  note "Userspace Tailscale is ready."
}

status() {
  resolve_worker_layout
  require_existing_deployment_tree || { note "RunPod deployment tree is missing or unsafe."; return 1; }
  require_var_root || { note "tailscaled var root is missing or unsafe."; return 1; }
  local file pid
  file="$(pid_file)"
  if ! pid_is_live "$file"; then
    note "Userspace tailscaled is not running (no live managed PID)."
    return 1
  fi
  pid="$(<"$file")"
  if ! is_exact_tailscaled "$pid"; then
    note "tailscaled PID file exists but command identity is unsafe."
    return 2
  fi
  [[ -S "$SOCKET_FILE" ]] || { note "tailscaled control socket is unavailable."; return 3; }
  node_status_is_expected >/dev/null 2>&1 || {
    note "Tailscale is not ready."
    return 3
  }
  note "Userspace Tailscale ready (pid $pid)."
}

serve_on() {
  resolve_worker_layout
  require_existing_deployment_tree || die "RunPod deployment tree is missing or unsafe"
  daemon_is_live || die "userspace Tailscale is not running with the expected command"
  serve_status_is_expected off >/dev/null 2>&1 || die "Tailscale Serve precondition is unsafe"
  "$(tailscale_bin)" --socket="$SOCKET_FILE" serve --bg --https=443 http://127.0.0.1:8787 \
    >/dev/null 2>&1 || die "Tailscale Serve configuration failed"
  # Do not call `tailscale funnel` and do not accept a public target option.
  serve_status_is_expected on >/dev/null 2>&1 || die \
    "Tailscale Serve is not ready"
}

serve_off() {
  resolve_worker_layout
  local file pid
  file="$(pid_file)"
  if ! pid_is_live "$file"; then
    note "No live managed tailscaled process; Serve is already unavailable."
    return 0
  fi
  pid="$(<"$file")"
  is_exact_tailscaled "$pid" || die "refusing to change Serve for an unsafe tailscaled PID"
  "$(tailscale_bin)" --socket="$SOCKET_FILE" serve --https=443 off >/dev/null 2>&1 || die \
    "Tailscale Serve shutdown failed"
  serve_status_is_expected off >/dev/null 2>&1 || die "Tailscale Serve shutdown status is unsafe"
}

serve_status() {
  resolve_worker_layout
  require_existing_deployment_tree || { note "RunPod deployment tree is missing or unsafe."; return 1; }
  daemon_is_live || { note "userspace Tailscale is not running with the expected command."; return 1; }
  serve_status_is_expected on >/dev/null 2>&1 || { note "Tailscale Serve configuration is unsafe."; return 1; }
  note "Tailscale Serve ready (HTTPS 443 loopback only)."
}

stop() {
  resolve_worker_layout
  local file pid
  file="$(pid_file)"
  if ! pid_is_live "$file"; then
    note "No live managed tailscaled process. PID file is left untouched for inspection."
    return 0
  fi
  pid="$(<"$file")"
  is_exact_tailscaled "$pid" || die "refusing to signal an unsafe tailscaled PID"
  kill -TERM "$pid"
  for _ in $(seq 1 30); do
    if ! kill -0 "$pid" 2>/dev/null; then
      remove_pid_record "$file"
      rm -f -- "$SOCKET_FILE"
      note "Userspace tailscaled stopped cleanly."
      return 0
    fi
    sleep 1
  done
  die "tailscaled did not stop within 30 seconds; inspect it manually (no SIGKILL issued)"
}

dispatch() {
  case "${1:-}" in
    start) start ;;
    status) status ;;
    stop) stop ;;
    serve-on) serve_on ;;
    serve-off) serve_off ;;
    serve-status) serve_status ;;
    *) die "usage: $0 {start|status|stop|serve-on|serve-off|serve-status}" ;;
  esac
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  dispatch "${1:-}"
fi
