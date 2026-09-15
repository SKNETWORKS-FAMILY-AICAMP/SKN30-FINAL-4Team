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
  # Official Supabase owns storage.objects/storage.buckets as
  # ``supabase_storage_admin``. ``postgres`` owns this project's schema and
  # SECURITY DEFINER functions but cannot alter Storage policies. Connect as
  # the local admin peer, then select postgres for ordinary project DDL.
  # Migrations 11/14 use transaction-local Storage-owner windows only for
  # bucket/policy operations and switch back before function creation.
  {
    printf 'SET ROLE postgres;\n'
    cat "$migration"
    printf '\nRESET ROLE;\n'
  } | (cd "$SUPABASE_DIR" && docker compose exec -T db \
    psql -U supabase_admin -d postgres -v ON_ERROR_STOP=1)
done

echo "All backend Supabase migrations applied."
