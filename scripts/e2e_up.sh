#!/usr/bin/env bash
# E2E용 PostgreSQL 기동 + 스키마 적용 + 데모 계정 생성.
# 이미 떠 있으면 재사용한다. 초기화하려면 e2e_down.sh 를 먼저 실행한다.
set -euo pipefail
export MSYS_NO_PATHCONV=1

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -W 2>/dev/null || pwd)"
# 윈도우와 그 외 OS 의 venv 경로가 다르다.
PY="$ROOT/backend/.venv/Scripts/python.exe"
[ -x "$PY" ] || PY="$ROOT/backend/.venv/bin/python"
NAME=sims-e2e-pg
PORT=55433
DSN="postgresql+psycopg://postgres:simstest@127.0.0.1:$PORT/sims"

if ! docker inspect "$NAME" >/dev/null 2>&1; then
  echo "[1/4] PostgreSQL 기동"
  docker run -d --name "$NAME" \
    -e POSTGRES_PASSWORD=simstest -e POSTGRES_DB=sims \
    -p "$PORT:5432" pgvector/pgvector:pg15 >/dev/null
else
  echo "[1/4] 기존 컨테이너 재사용"
  docker start "$NAME" >/dev/null 2>&1 || true
fi

echo "[2/4] 기동 대기"
until docker exec "$NAME" pg_isready -U postgres -d sims >/dev/null 2>&1; do sleep 1; done

echo "[3/4] 스키마 적용"
docker cp "$ROOT/backend/app/db/schema.sql" "$NAME:/tmp/schema.sql"
docker exec -e PGPASSWORD=simstest "$NAME" \
  psql -v ON_ERROR_STOP=1 -U postgres -d sims -q -f /tmp/schema.sql

echo "[4/4] 데모 계정 생성 (회원가입 API 가 없어 직접 넣는다)"
(cd "$ROOT/backend" && DATABASE_URL="$DSN" "$PY" - <<'PYEOF'
import os
from sqlalchemy import create_engine, text
from app.core.security import hash_password

engine = create_engine(os.environ["DATABASE_URL"])
with engine.begin() as c:
    c.execute(
        text("""
            INSERT INTO sims.app_user (login_id, email, password_hash)
            VALUES (:l, :e, :p)
            ON CONFLICT (login_id) DO NOTHING
        """),
        {"l": "demo", "e": "demo@example.com", "p": hash_password("demo-password-1234")},
    )
engine.dispose()
print("  demo / demo-password-1234")
PYEOF
)

echo
echo "완료. DATABASE_URL=$DSN"
