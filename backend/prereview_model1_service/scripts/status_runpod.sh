#!/usr/bin/env bash
set -euo pipefail
pid_file="/run/prereview-model1/model1.pid"
[[ -f "$pid_file" ]] || { printf 'stopped\n'; exit 1; }
pid="$(<"$pid_file")"
[[ "$pid" =~ ^[1-9][0-9]*$ ]] && kill -0 "$pid" 2>/dev/null || { printf 'stale pid record\n'; exit 1; }
tr '\0' ' ' < "/proc/$pid/cmdline" | grep -Fq 'prereview_model1_service.entrypoint:app' || { printf 'unsafe pid identity\n'; exit 1; }
printf 'running pid=%s loopback=127.0.0.1:8791\n' "$pid"
