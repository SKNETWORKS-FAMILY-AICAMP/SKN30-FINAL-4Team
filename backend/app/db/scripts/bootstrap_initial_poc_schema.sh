#!/usr/bin/env bash
set -euo pipefail

# Usage: DATABASE_URL=postgresql://... ./bootstrap_initial_poc_schema.sh
# Applies the archived baseline (01~09) and then PoC migrations (10~13).

if [[ -z "${DATABASE_URL:-}" ]]; then
  echo 'DATABASE_URL is required.' >&2
  exit 1
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
migrations_dir="${script_dir}/../migrations/supabase"

for migration_path in "${migrations_dir}"/*.sql; do
  echo "Applying ${migration_path}..."
  psql "${DATABASE_URL}" -v ON_ERROR_STOP=1 -f "${migration_path}"
done

echo "All migrations applied successfully."
