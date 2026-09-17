#!/usr/bin/env bash
set -euo pipefail
pid_file="/run/prereview-model1/model1.pid"
[[ -f "$pid_file" ]] || exit 0
pid="$(<"$pid_file")"
if [[ "$pid" =~ ^[1-9][0-9]*$ ]] && kill -0 "$pid" 2>/dev/null; then
  tr '\0' ' ' < "/proc/$pid/cmdline" | grep -Fq 'prereview_model1_service.entrypoint:app' || { printf 'unsafe pid identity\n' >&2; exit 1; }
  kill -TERM "$pid"
fi
rm -f "$pid_file"
