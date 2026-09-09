#!/usr/bin/env bash
set -euo pipefail

# Apply the versioned backend migrations to a running self-hosted Supabase DB.
# SUPABASE_DIR must point to the Docker Compose directory, never to a remote DB.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SUPABASE_DIR="${SUPABASE_DIR:?Set SUPABASE_DIR to the local Supabase Compose directory}"

[[ -f "$SUPABASE_DIR/docker-compose.yml" ]] || {
  echo "ERROR: docker-compose.yml not found in SUPABASE_DIR: $SUPABASE_DIR" >&2
  exit 1
}

for migration in "$SCRIPT_DIR"/migrations/[0-9][0-9]_*.sql; do
  echo "Applying $(basename "$migration")"
  (cd "$SUPABASE_DIR" && docker compose exec -T db \
    psql -U postgres -d postgres -v ON_ERROR_STOP=1) < "$migration"
done

echo "All backend Supabase migrations applied."
