#!/usr/bin/env bash
# Install the pinned official self-hosted Supabase Docker bundle into a new
# local target. This prepares files and secrets only: it never starts, resets,
# stops, or removes a database/volume.
set -euo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPOSITORY_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
DEFAULT_TARGET="$REPOSITORY_ROOT/.runtime/supabase-dev"
PINNED_SUPABASE_REF="self-hosted/v0.8.0"
PINNED_SUPABASE_COMMIT="241bb11c0627f2981746d37033f57dbfa81d29b0"
OFFICIAL_REPOSITORY_URL="https://github.com/supabase/supabase.git"

usage() {
  cat <<'USAGE'
usage: install_selfhosted_local.sh [--target /absolute/new/path]

Installs the pinned official Supabase Docker bundle, generates its local
secrets, and adds docker-compose.pgvector.yml. It does not start containers,
apply PreReview migrations, seed data, or reset any volume.
USAGE
}

TARGET_DIR="$DEFAULT_TARGET"
while (($#)); do
  case "$1" in
    --target)
      (($# >= 2)) || { echo "ERROR: --target requires a path" >&2; exit 2; }
      TARGET_DIR="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "ERROR: unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

[[ "$TARGET_DIR" = /* ]] || {
  echo "ERROR: --target must be an absolute path" >&2
  exit 1
}
[[ ! -L "$TARGET_DIR" ]] || {
  echo "ERROR: refusing a symlink target" >&2
  exit 1
}

TARGET_DIR="$(realpath -m -- "$TARGET_DIR")"
RUNTIME_ROOT="$(realpath -m -- "$REPOSITORY_ROOT/.runtime")"
[[ "$TARGET_DIR" != "/" && "$(dirname "$TARGET_DIR")" != "/" ]] || {
  echo "ERROR: refusing a broad filesystem target" >&2
  exit 1
}
[[ "$TARGET_DIR" != "$REPOSITORY_ROOT" && "$TARGET_DIR" != "$RUNTIME_ROOT" ]] || {
  echo "ERROR: refusing a repository or runtime root target" >&2
  exit 1
}
if [[ "$TARGET_DIR" == "$REPOSITORY_ROOT/"* && "$TARGET_DIR" != "$RUNTIME_ROOT/"* ]]; then
  echo "ERROR: repository targets must be below .runtime" >&2
  exit 1
fi

TARGET_EXISTED=false
if [[ -e "$TARGET_DIR" ]]; then
  [[ -d "$TARGET_DIR" ]] || {
    echo "ERROR: target exists and is not a directory" >&2
    exit 1
  }
  [[ -z "$(find "$TARGET_DIR" -mindepth 1 -maxdepth 1 -print -quit)" ]] || {
    echo "ERROR: refusing a non-empty target" >&2
    exit 1
  }
  TARGET_EXISTED=true
fi

for command_name in git openssl docker realpath mktemp; do
  command -v "$command_name" >/dev/null 2>&1 || {
    echo "ERROR: required command is missing: $command_name" >&2
    exit 1
  }
done
docker compose version >/dev/null 2>&1 || {
  echo "ERROR: Docker Compose plugin is unavailable" >&2
  exit 1
}

TARGET_PARENT="$(dirname "$TARGET_DIR")"
if [[ ! -d "$TARGET_PARENT" ]]; then
  install -d -m 0750 "$TARGET_PARENT"
fi

STAGING_DIR="$(mktemp -d "$TARGET_PARENT/.supabase-install.XXXXXX")"
cleanup() {
  if [[ -n "${STAGING_DIR:-}" && "$STAGING_DIR" == "$TARGET_PARENT/.supabase-install."* ]]; then
    rm -rf -- "$STAGING_DIR"
  fi
}
trap cleanup EXIT

echo "Installing pinned Supabase bundle into a staging directory"
echo "  ref: $PINNED_SUPABASE_REF"
echo "  target: $TARGET_DIR"

git clone \
  --depth 1 \
  --filter=blob:none \
  --sparse \
  --branch "$PINNED_SUPABASE_REF" \
  "$OFFICIAL_REPOSITORY_URL" \
  "$STAGING_DIR/source" >/dev/null 2>&1 || {
    echo "ERROR: failed to fetch the pinned official Supabase repository" >&2
    exit 1
  }
RESOLVED_COMMIT="$(git -C "$STAGING_DIR/source" rev-parse HEAD)"
[[ "$RESOLVED_COMMIT" == "$PINNED_SUPABASE_COMMIT" ]] || {
  echo "ERROR: Supabase ref resolved to an unexpected commit" >&2
  exit 1
}
git -C "$STAGING_DIR/source" sparse-checkout set docker >/dev/null 2>&1 || {
  echo "ERROR: failed to select the official Docker bundle" >&2
  exit 1
}

SOURCE_DOCKER="$STAGING_DIR/source/docker"
[[ -f "$SOURCE_DOCKER/docker-compose.yml" && -f "$SOURCE_DOCKER/.env.example" ]] || {
  echo "ERROR: pinned source does not contain the expected Docker bundle" >&2
  exit 1
}
[[ -f "$SOURCE_DOCKER/utils/generate-keys.sh" && -f "$SOURCE_DOCKER/utils/add-new-auth-keys.sh" ]] || {
  echo "ERROR: pinned source does not contain the expected secret generators" >&2
  exit 1
}

cp -a "$SOURCE_DOCKER" "$STAGING_DIR/bundle"
install -m 0600 "$STAGING_DIR/bundle/.env.example" "$STAGING_DIR/bundle/.env"
if ! (
  cd "$STAGING_DIR/bundle"
  sh utils/generate-keys.sh --update-env >/dev/null 2>&1
  sh utils/add-new-auth-keys.sh --update-env >/dev/null 2>&1
); then
  echo "ERROR: official Supabase secret generation failed" >&2
  exit 1
fi
chmod 0600 "$STAGING_DIR/bundle/.env"
# Both official generators use sed backup files while updating the bundle.
# Remove those staging-only copies so generated credentials exist in one file.
rm -f -- \
  "$STAGING_DIR/bundle/.env.old" \
  "$STAGING_DIR/bundle/docker-compose.yml.old"

for required_name in POSTGRES_PASSWORD JWT_SECRET ANON_KEY SERVICE_ROLE_KEY POOLER_TENANT_ID; do
  grep -Eq "^${required_name}=.+$" "$STAGING_DIR/bundle/.env" || {
    echo "ERROR: generated Supabase environment is incomplete: $required_name" >&2
    exit 1
  }
done

install -m 0644 \
  "$SCRIPT_DIR/docker-compose.pgvector.override.yml.example" \
  "$STAGING_DIR/bundle/docker-compose.pgvector.yml"

printf 'ref=%s\ncommit=%s\n' \
  "$PINNED_SUPABASE_REF" "$RESOLVED_COMMIT" \
  > "$STAGING_DIR/bundle/.pre-review-supabase-version"

if ! (
  cd "$STAGING_DIR/bundle"
  docker compose \
    -f docker-compose.yml \
    -f docker-compose.pgvector.yml \
    config --quiet >/dev/null 2>&1
); then
  echo "ERROR: generated Supabase Compose configuration is invalid" >&2
  exit 1
fi

if [[ "$TARGET_EXISTED" == true ]]; then
  rmdir "$TARGET_DIR"
fi
mv -T -- "$STAGING_DIR/bundle" "$TARGET_DIR"

echo "Supabase files prepared successfully."
echo "  target: $TARGET_DIR"
echo "  secrets: $TARGET_DIR/.env (mode 600; values not printed)"
echo "No container was started and no database or volume was reset."
echo "Continue with: $SCRIPT_DIR/README.md"
