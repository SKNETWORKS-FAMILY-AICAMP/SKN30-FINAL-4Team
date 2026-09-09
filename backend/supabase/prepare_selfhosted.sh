#!/usr/bin/env bash
# self-hosted Supabase의 영속 호스트 경로를 준비한다.
# DB 데이터를 복사하거나 컨테이너를 기동하지 않는다.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="${1:-$SCRIPT_DIR/.env}"
[[ -f "$CONFIG_FILE" ]] || { echo "ERROR: missing config: $CONFIG_FILE" >&2; exit 1; }

set -a
# shellcheck disable=SC1090
. "$CONFIG_FILE"
set +a

: "${SUPABASE_COMPOSE_DIR:?SUPABASE_COMPOSE_DIR is required}"
: "${SUPABASE_DB_DATA_DIR:?SUPABASE_DB_DATA_DIR is required}"
[[ -f "$SUPABASE_COMPOSE_DIR/docker-compose.yml" ]] || { echo "ERROR: Supabase Compose not found" >&2; exit 1; }
[[ "$SUPABASE_DB_DATA_DIR" = /* && "$SUPABASE_DB_DATA_DIR" != "/" ]] || { echo "ERROR: use a safe absolute SUPABASE_DB_DATA_DIR" >&2; exit 1; }

install -d -m 0700 "$SUPABASE_DB_DATA_DIR"
if [[ -n "${SUPABASE_STORAGE_DATA_DIR:-}" ]]; then install -d -m 0750 "$SUPABASE_STORAGE_DATA_DIR"; fi

LOCAL_DB_PATH="$SUPABASE_COMPOSE_DIR/volumes/db/data"
if [[ -L "$LOCAL_DB_PATH" ]]; then
  [[ "$(readlink -f "$LOCAL_DB_PATH")" = "$SUPABASE_DB_DATA_DIR" ]] || {
    echo "ERROR: existing DB symlink points elsewhere: $LOCAL_DB_PATH" >&2; exit 1;
  }
elif [[ -e "$LOCAL_DB_PATH" ]]; then
  [[ -d "$LOCAL_DB_PATH" && -z "$(find "$LOCAL_DB_PATH" -mindepth 1 -maxdepth 1 -print -quit)" ]] || {
    echo "ERROR: refusing to replace non-empty DB path: $LOCAL_DB_PATH" >&2; exit 1;
  }
  rmdir "$LOCAL_DB_PATH"
  ln -s "$SUPABASE_DB_DATA_DIR" "$LOCAL_DB_PATH"
else
  install -d "$(dirname "$LOCAL_DB_PATH")"
  ln -s "$SUPABASE_DB_DATA_DIR" "$LOCAL_DB_PATH"
fi

echo "Prepared PostgreSQL data directory: $SUPABASE_DB_DATA_DIR"
echo "Linked Compose DB path: $LOCAL_DB_PATH -> $SUPABASE_DB_DATA_DIR"
echo "Start with: cd $SUPABASE_COMPOSE_DIR && docker compose up -d"
