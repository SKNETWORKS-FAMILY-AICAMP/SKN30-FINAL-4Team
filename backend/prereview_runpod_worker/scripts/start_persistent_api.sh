#!/usr/bin/env bash
# Foreground launcher for the Tailscale-only persistent-Pod API.

# A caller may invoke this launcher through ``bash -x`` or inherit xtrace via
# SHELLOPTS.  Disable it before any runtime secret can be opened or expanded.
{ set +x; } 2>/dev/null
set -euo pipefail
umask 077

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

readonly PERSISTENT_API_BEARER_FILE="/run/prereview-surya/api-bearer-token"
readonly PERSISTENT_API_BEARER_MAX_BYTES=513
readonly PERSISTENT_API_WORKSPACE_MOUNT="/workspace"
readonly PERSISTENT_API_STATE_DIRECTORY="/workspace/persistent/prereview/surya-jobs"

ensure_persistent_journal_directory() {
  local workspace_mount="${1:-$PERSISTENT_API_WORKSPACE_MOUNT}"
  local state_directory="${2:-$PERSISTENT_API_STATE_DIRECTORY}"
  local expected_state_directory="$workspace_mount/persistent/prereview/surya-jobs"
  local current_path next_path component resolved_path

  if [[ "$workspace_mount" != "$PERSISTENT_API_WORKSPACE_MOUNT" || \
    "$state_directory" != "$PERSISTENT_API_STATE_DIRECTORY" ]]; then
    [[ "${PREREVIEW_RUNPOD_LAUNCHER_TEST_MODE:-}" == "1" ]] || die \
      "persistent API journal path is fixed"
  fi
  [[ "$state_directory" == "$expected_state_directory" ]] || die \
    "persistent API journal path is invalid"
  if [[ -n "${PREREVIEW_SURYA_API_STATE_DIRECTORY:-}" && \
    "$PREREVIEW_SURYA_API_STATE_DIRECTORY" != "$state_directory" ]]; then
    die "persistent API journal path override is invalid"
  fi
  [[ -d "$workspace_mount" && ! -L "$workspace_mount" ]] || die \
    "persistent API requires a non-symlink /workspace mountpoint"
  require_command mountpoint
  mountpoint -q -- "$workspace_mount" || die \
    "persistent API requires /workspace to be a separate mountpoint"
  resolved_path="$(realpath -e -- "$workspace_mount")" || die \
    "persistent API workspace mountpoint is invalid"
  [[ "$resolved_path" == "$workspace_mount" ]] || die \
    "persistent API workspace mountpoint realpath is invalid"

  current_path="$workspace_mount"
  for component in persistent prereview surya-jobs; do
    next_path="$current_path/$component"
    if [[ -e "$next_path" || -L "$next_path" ]]; then
      [[ -d "$next_path" && ! -L "$next_path" ]] || die \
        "persistent API journal path must contain only non-symlink directories"
    else
      mkdir -- "$next_path" || die \
        "persistent API journal directory cannot be created"
    fi
    [[ -d "$next_path" && ! -L "$next_path" ]] || die \
      "persistent API journal path must contain only non-symlink directories"
    resolved_path="$(realpath -e -- "$next_path")" || die \
      "persistent API journal path is invalid"
    [[ "$resolved_path" == "$next_path" ]] || die \
      "persistent API journal path realpath is invalid"
    current_path="$next_path"
  done
  [[ "$current_path" == "$state_directory" ]] || die \
    "persistent API journal path is invalid"

  export PREREVIEW_SURYA_API_STATE_DIRECTORY="$state_directory"
}

read_persistent_api_bearer_file() {
  # Also defend direct/test calls made after a parent re-enabled xtrace.
  { set +x; } 2>/dev/null
  local bearer_file="$1"
  local token_fd metadata file_type file_owner file_mode file_links file_size
  local bearer_token="" extra_line=""

  # The launcher deliberately has no *_TOKEN or *_TOKEN_FILE environment
  # input.  This helper accepts a path only to keep its file checks testable;
  # main() always passes the one fixed /run path above.
  [[ "$bearer_file" == "$PERSISTENT_API_BEARER_FILE" || \
    "${PREREVIEW_RUNPOD_LAUNCHER_TEST_MODE:-}" == "1" ]] || die \
    "persistent API bearer file path is invalid"
  [[ ! -L "$bearer_file" && -f "$bearer_file" ]] || die \
    "persistent API bearer file is invalid"

  # Open before inspecting metadata so a path replacement cannot redirect the
  # later read.  /proc/$$/fd binds stat to exactly the opened inode.
  exec {token_fd}<"$bearer_file" || die "persistent API bearer file is unreadable"
  metadata="$(stat -Lc '%F:%u:%a:%h:%s' -- "/proc/$$/fd/$token_fd")" || {
    exec {token_fd}<&-
    die "persistent API bearer file is invalid"
  }
  IFS=: read -r file_type file_owner file_mode file_links file_size <<< "$metadata"
  if [[ "$file_type" != "regular file" || "$file_owner" != "$(id -u)" || \
    "$file_mode" != "600" || "$file_links" != "1" || \
    ! "$file_size" =~ ^[0-9]+$ || file_size -lt 32 || \
    file_size -gt "$PERSISTENT_API_BEARER_MAX_BYTES" ]]; then
    exec {token_fd}<&-
    die "persistent API bearer file is invalid"
  fi

  if ! IFS= read -r bearer_token <&"$token_fd"; then
    [[ -n "$bearer_token" ]] || {
      exec {token_fd}<&-
      die "persistent API bearer file is invalid"
    }
  fi
  if IFS= read -r extra_line <&"$token_fd"; then
    exec {token_fd}<&-
    die "persistent API bearer file is invalid"
  fi
  exec {token_fd}<&-

  [[ "$bearer_token" =~ ^[A-Za-z0-9_-]{32,512}$ ]] || die \
    "persistent API bearer file is invalid"
  PERSISTENT_API_BEARER_TOKEN="$bearer_token"
}

main() {
  # Do not accept shell-exported credentials as a fallback.  The child needs
  # the token in its environment for settings parsing, but the assignment at
  # exec below does not export it from this launcher shell or put it in history.
  [[ -z "${PREREVIEW_SURYA_API_BEARER_TOKEN:-}" ]] || die \
    "persistent API bearer must be supplied by its runtime file"

  resolve_worker_layout
  ensure_persistent_journal_directory
  ensure_deployment_tree
  read_config_into_environment

  [[ -x "$PREREVIEW_RUNPOD_CLIENT_VENV/bin/python" ]] || die \
    "client venv is missing; run setup_runpod.sh first"
  read_persistent_api_bearer_file "$PERSISTENT_API_BEARER_FILE"

  # The application validates token strength, queue/body/state limits and
  # port.  Never print the environment or invoke the interpreter with the
  # token as a command-line argument.
  PREREVIEW_SURYA_API_BEARER_TOKEN="$PERSISTENT_API_BEARER_TOKEN" \
    exec "$PREREVIEW_RUNPOD_CLIENT_VENV/bin/python" \
      -m prereview_runpod_worker.persistent_api.entrypoint
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
