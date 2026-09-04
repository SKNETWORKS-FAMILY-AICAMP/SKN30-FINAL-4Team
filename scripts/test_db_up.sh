#!/usr/bin/env bash
# 테스트 전용 PostgreSQL 데이터베이스를 만든다.
#
# E2E 를 돌린 DB 로 테스트를 돌리면 안 된다.
# test_retrieval 은 DB 의 모든 현행 공고를 임베딩하는데, 가짜 벡터 fixture 에는
# 자기가 넣은 공고만 등록돼 있어 남의 행에서 실패한다. 운영 함수를 테스트에
# 맞춰 좁히면 실제 동작을 검증하지 못하므로 DB 를 나눈다.
#
#   bash scripts/test_db_up.sh
#   cd backend && TEST_DATABASE_URL="$(cat ../tmp/test_database_url)" python -m pytest tests
#
# 초기화하려면 --reset 을 붙인다.
set -euo pipefail
export MSYS_NO_PATHCONV=1

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -W 2>/dev/null || pwd)"
NAME=sims-e2e-pg
DB=sims_test
PORT="$(docker inspect -f '{{(index (index .NetworkSettings.Ports "5432/tcp") 0).HostPort}}' "$NAME")"
DSN="postgresql+psycopg://postgres:simstest@127.0.0.1:$PORT/$DB"

if [ "${1:-}" = "--reset" ]; then
  echo "[1/3] 기존 $DB 삭제"
  docker exec -e PGPASSWORD=simstest "$NAME" \
    psql -U postgres -d postgres -c "DROP DATABASE IF EXISTS $DB;" >/dev/null
fi

if ! docker exec -e PGPASSWORD=simstest "$NAME" \
     psql -U postgres -d postgres -tAc \
     "SELECT 1 FROM pg_database WHERE datname='$DB';" | grep -q 1; then
  echo "[2/3] $DB 생성"
  docker exec -e PGPASSWORD=simstest "$NAME" \
    psql -U postgres -d postgres -c "CREATE DATABASE $DB;" >/dev/null
  echo "[3/3] 스키마 적용"
  docker cp "$ROOT/backend/app/db/schema.sql" "$NAME:/tmp/schema.sql"
  docker exec -e PGPASSWORD=simstest "$NAME" \
    psql -v ON_ERROR_STOP=1 -U postgres -d "$DB" -q -f /tmp/schema.sql
else
  echo "[2/3] 기존 $DB 재사용 (초기화하려면 --reset)"
fi

mkdir -p "$ROOT/tmp"
printf '%s' "$DSN" > "$ROOT/tmp/test_database_url"
echo
echo "TEST_DATABASE_URL=$DSN"
echo "tmp/test_database_url 에 저장했다."
