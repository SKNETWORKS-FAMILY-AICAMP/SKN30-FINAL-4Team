#!/usr/bin/env bash
# Stop only a vLLM server that this package's start script is still tracking.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PIDFILE="$ROOT/.runtime/surya-vllm/vllm.pid"
[[ -f "$PIDFILE" ]] || { echo "status=already_stopped"; exit 0; }
PID="$(<"$PIDFILE")"
matches_server() {
  local pid="$1"
  [[ -r "/proc/$pid/cmdline" ]] || return 1
  tr '\0' '\n' < "/proc/$pid/cmdline" | grep -qx 'vllm.entrypoints.openai.api_server'
}
if ! kill -0 "$PID" 2>/dev/null; then
  rm -f "$PIDFILE"
  echo "status=already_stopped reason=stale_pid"
  exit 0
fi
if ! matches_server "$PID"; then
  echo "Refusing to signal pid=$PID: command is no longer vLLM api_server." >&2
  rm -f "$PIDFILE"
  exit 1
fi
kill -TERM "-$PID" 2>/dev/null || kill -TERM "$PID"
for _ in $(seq 1 20); do
  kill -0 "$PID" 2>/dev/null || break
  sleep 0.5
done
if kill -0 "$PID" 2>/dev/null; then
  echo "status=force_kill pid=$PID"
  kill -KILL "-$PID" 2>/dev/null || kill -KILL "$PID"
else
  echo "status=stopped pid=$PID"
fi
rm -f "$PIDFILE"
