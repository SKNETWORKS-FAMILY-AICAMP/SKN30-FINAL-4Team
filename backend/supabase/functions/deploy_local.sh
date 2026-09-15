#!/usr/bin/env bash
# Copy the versioned Edge Function sources into a local self-hosted Supabase
# runtime.  It deliberately does not delete any target files.
# LEGACY / INACTIVE: the current FastAPI + PostgreSQL polling worker runtime
# does not deploy these functions. See functions/README.md before any use.
set -euo pipefail

if [[ "${PREREVIEW_ENABLE_LEGACY_EDGE_FUNCTIONS:-}" != "I_ACKNOWLEDGE_UNSUPPORTED_EDGE_RUNTIME" ]]; then
  echo "Refusing to deploy inactive Edge Functions." >&2
  echo "The supported runtime is FastAPI + PostgreSQL polling worker; see functions/README.md." >&2
  exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_ROOT="${1:?usage: deploy_local.sh /path/to/supabase-dev/volumes/functions}"

if [[ ! -d "$TARGET_ROOT" ]]; then
  echo "Function volume directory does not exist: $TARGET_ROOT" >&2
  exit 1
fi

for function_name in \
  edge-analysis-run-create \
  edge-analysis-run-complete-upload \
  edge-analysis-run-ingest-request-profile \
  edge-worker-existing-candidates \
  edge-worker-existing-profile \
  edge-analysis-run-ingest-comparison-result \
  edge-conversation-create-message \
  edge-conversation-retry-message \
  edge-report-create-download-url; do
  install -d "$TARGET_ROOT/$function_name"
  install -m 0644 "$SCRIPT_DIR/$function_name/index.ts" "$TARGET_ROOT/$function_name/index.ts"
done

install -d "$TARGET_ROOT/_shared"
for shared_file in supabase.ts worker-dispatch.ts worker-http-dispatch.ts; do
  install -m 0644 "$SCRIPT_DIR/_shared/$shared_file" "$TARGET_ROOT/_shared/$shared_file"
done

echo "Edge Function sources copied to $TARGET_ROOT"
echo "Restart the functions service to clear runtime module caches: docker compose restart functions"
