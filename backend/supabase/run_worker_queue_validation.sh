#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SUPABASE_DIR="${SUPABASE_DIR:?Set SUPABASE_DIR to the local Supabase Compose directory}"

[[ -f "$SUPABASE_DIR/docker-compose.yml" ]] || {
  echo "ERROR: docker-compose.yml not found in SUPABASE_DIR: $SUPABASE_DIR" >&2
  exit 1
}

for runtime_test in \
  "$SCRIPT_DIR/tests/analysis_worker_queue_runtime.sql" \
  "$SCRIPT_DIR/tests/v02_runtime.sql"; do
  (cd "$SUPABASE_DIR" && docker compose exec -T db \
    psql -U postgres -d postgres) < "$runtime_test"
done

echo "Analysis worker queue and v0.2 runtime contracts passed (transactions rolled back)."
