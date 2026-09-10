#!/usr/bin/env bash
# self-hosted Supabase의 영속 호스트 경로를 준비한다.
# DB 데이터를 복사하거나 컨테이너를 기동하지 않는다.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPOSITORY_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
CONFIG_FILE="${1:-$SCRIPT_DIR/.env}"
[[ -f "$CONFIG_FILE" ]] || { echo "ERROR: missing config: $CONFIG_FILE" >&2; exit 1; }
command -v realpath >/dev/null 2>&1 || { echo "ERROR: realpath is required" >&2; exit 1; }

set -a
# shellcheck disable=SC1090
. "$CONFIG_FILE"
set +a

: "${SUPABASE_COMPOSE_DIR:?SUPABASE_COMPOSE_DIR is required}"
: "${SUPABASE_DB_DATA_DIR:?SUPABASE_DB_DATA_DIR is required}"
[[ -f "$SUPABASE_COMPOSE_DIR/docker-compose.yml" ]] || { echo "ERROR: Supabase Compose not found" >&2; exit 1; }

COMPOSE_ROOT="$(realpath -m -- "$SUPABASE_COMPOSE_DIR")"
REPOSITORY_ROOT="$(realpath -m -- "$REPOSITORY_ROOT")"
RUNTIME_ROOT="$(realpath -m -- "$REPOSITORY_ROOT/.runtime")"

paths_overlap() {
  local first="$1"
  local second="$2"
  [[ "$first" == "$second" || "$first" == "$second/"* || "$second" == "$first/"* ]]
}

safe_data_path() {
  local variable_name="$1"
  local raw_path="$2"
  local resolved_path

  [[ "$raw_path" = /* && ! -L "$raw_path" ]] || {
    echo "ERROR: $variable_name must be an absolute, non-symlink path" >&2
    return 1
  }
  resolved_path="$(realpath -m -- "$raw_path")"
  [[ "$resolved_path" != "/" && "$(dirname "$resolved_path")" != "/" ]] || {
    echo "ERROR: refusing broad $variable_name path" >&2
    return 1
  }
  if [[ "$resolved_path" == "$REPOSITORY_ROOT" || "$resolved_path" == "$RUNTIME_ROOT" ]]; then
    echo "ERROR: refusing a repository/runtime root as $variable_name" >&2
    return 1
  fi
  if [[ "$resolved_path" == "$REPOSITORY_ROOT/"* && "$resolved_path" != "$RUNTIME_ROOT/"* ]]; then
    echo "ERROR: repository data paths must be below .runtime" >&2
    return 1
  fi
  if [[ "$REPOSITORY_ROOT" == "$resolved_path/"* ]]; then
    echo "ERROR: refusing a repository ancestor as $variable_name" >&2
    return 1
  fi
  if paths_overlap "$resolved_path" "$COMPOSE_ROOT"; then
    echo "ERROR: refusing a path overlapping the Supabase Compose tree" >&2
    return 1
  fi
  printf '%s\n' "$resolved_path"
}

SUPABASE_DB_DATA_DIR="$(safe_data_path SUPABASE_DB_DATA_DIR "$SUPABASE_DB_DATA_DIR")"
if [[ -n "${SUPABASE_STORAGE_DATA_DIR:-}" ]]; then
  SUPABASE_STORAGE_DATA_DIR="$(safe_data_path SUPABASE_STORAGE_DATA_DIR "$SUPABASE_STORAGE_DATA_DIR")"
  if paths_overlap "$SUPABASE_DB_DATA_DIR" "$SUPABASE_STORAGE_DATA_DIR"; then
    echo "ERROR: DB and Storage data directories must not overlap" >&2
    exit 1
  fi
fi

if [[ -e "$SUPABASE_DB_DATA_DIR" ]]; then
  [[ -d "$SUPABASE_DB_DATA_DIR" ]] || { echo "ERROR: DB data path is not a directory" >&2; exit 1; }
  echo "Using existing PostgreSQL directory without changing its mode: $SUPABASE_DB_DATA_DIR"
else
  install -d -m 0700 "$SUPABASE_DB_DATA_DIR"
fi
if [[ -n "${SUPABASE_STORAGE_DATA_DIR:-}" ]]; then
  if [[ -e "$SUPABASE_STORAGE_DATA_DIR" ]]; then
    [[ -d "$SUPABASE_STORAGE_DATA_DIR" ]] || { echo "ERROR: Storage data path is not a directory" >&2; exit 1; }
    echo "Using existing Storage directory without changing its mode: $SUPABASE_STORAGE_DATA_DIR"
  else
    install -d -m 0750 "$SUPABASE_STORAGE_DATA_DIR"
  fi
  echo "Prepared optional Storage directory (not linked automatically): $SUPABASE_STORAGE_DATA_DIR"
fi

LOCAL_DB_PATH="$COMPOSE_ROOT/volumes/db/data"
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
  [[ -d "$(dirname "$LOCAL_DB_PATH")" ]] || install -d "$(dirname "$LOCAL_DB_PATH")"
  ln -s "$SUPABASE_DB_DATA_DIR" "$LOCAL_DB_PATH"
fi

echo "Prepared PostgreSQL data directory: $SUPABASE_DB_DATA_DIR"
echo "Linked Compose DB path: $LOCAL_DB_PATH -> $SUPABASE_DB_DATA_DIR"
echo "Install docker-compose.pgvector.yml, then start with both Compose files."
echo "See: $SCRIPT_DIR/README.md"
