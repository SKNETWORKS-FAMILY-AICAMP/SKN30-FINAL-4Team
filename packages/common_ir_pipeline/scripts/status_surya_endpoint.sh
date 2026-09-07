#!/usr/bin/env bash
# Checks, but never starts/stops, the endpoint chosen by the caller.
set -euo pipefail
URL="${SURYA_INFERENCE_URL:-}"
test -n "$URL" || { echo "status=unconfigured reason=SURYA_INFERENCE_URL_required" >&2; exit 2; }
URL="${URL%/}"
[[ "$URL" == */v1 ]] || URL="$URL/v1"
if command -v curl >/dev/null 2>&1 && curl -fsS --max-time 5 "$URL/models" >/dev/null; then
  echo "status=reachable endpoint=$URL"
else
  echo "status=unreachable endpoint=$URL" >&2
  exit 1
fi
