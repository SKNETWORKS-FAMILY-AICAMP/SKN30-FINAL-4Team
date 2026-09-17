# PreReview 데이터베이스 신규 환경 구성 가이드

새 Linux/EC2 환경에 self-hosted Supabase와 PreReview 데이터를 구성하는 운영자용
체크리스트다. 비밀값, DB dump, Storage archive, 데이터팩, `serving.zip`은 Git에 넣지 않는다.

## 1. 구성 방식 선택

| 목적 | 방식 | 포함 데이터 |
|---|---|---|
| 새 개발·검증 환경 | migration + Existing KB bootstrap | 스키마, bucket, KB 100건, embedding |
| 현재 서버 전체 복제 | DB dump + Storage archive | 사용자, 이력, 채팅, KB, Storage 객체 |

일반 개발 환경은 첫 번째 방식을 권장한다. 전체 복제본에는 개인정보와 인증정보가 포함될 수
있으므로 승인된 운영자만 취급한다.

## 2. 준비물과 저장소

- Docker Engine 및 Docker Compose plugin
- 최신 `develop`
- 검증된 `serving.zip`
- 새 KB 구성 시 `structured-profiles-100.zip`
- Supabase/DB/OpenAI/SMTP 비밀값
- 전체 복제 시 같은 시점의 PostgreSQL custom dump와 Storage archive

```bash
git clone <repository-url> SKN30-FINAL-4Team
cd SKN30-FINAL-4Team
git switch develop
git pull --ff-only origin develop
git status --short --branch
git rev-parse HEAD
docker version
docker compose version
```

## 3. self-hosted Supabase 설치

```bash
backend/supabase/install_selfhosted_local.sh
install -m 644 \
  backend/supabase/docker-compose.auth-templates.yml \
  .runtime/supabase-dev/docker-compose.auth-templates.yml
```

기본 설치 위치는 `<repository>/.runtime/supabase-dev`다. 설치 스크립트는 컨테이너,
migration, 데이터를 시작하지 않는다. `.runtime/supabase-dev/.env`를 mode `600`으로 유지하고
다음 항목을 환경에 맞게 설정한다.

- `SITE_URL`, `ADDITIONAL_REDIRECT_URLS`
- `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`
- `SMTP_ADMIN_EMAIL`, `SMTP_SENDER_NAME`
- `DISABLE_SIGNUP=true` (설치기가 강제하며 운영에서도 변경하지 않는다)
- `ENABLE_EMAIL_SIGNUP`, `ENABLE_EMAIL_AUTOCONFIRM` (email provider 설정이며 공개 가입 허용과 별개)
- `PREREVIEW_AUTH_TEMPLATE_DIR=<repository>/backend/supabase/templates`

모든 Supabase 명령은 같은 세 Compose 파일을 사용한다.

```bash
chmod 600 .runtime/supabase-dev/.env
cd .runtime/supabase-dev

docker compose -p supabase \
  -f docker-compose.yml \
  -f docker-compose.pgvector.yml \
  -f docker-compose.auth-templates.yml config --quiet

docker compose -p supabase \
  -f docker-compose.yml \
  -f docker-compose.pgvector.yml \
  -f docker-compose.auth-templates.yml up -d

docker compose -p supabase \
  -f docker-compose.yml \
  -f docker-compose.pgvector.yml \
  -f docker-compose.auth-templates.yml ps
```

최소 `db`, `supavisor`, `auth`, `storage`, `rest`, `api-gw`가 정상인지 확인한다.

## 4A. 빈 DB를 migration과 데이터팩으로 구성

```bash
cd <repository>
SUPABASE_DIR="$PWD/.runtime/supabase-dev" \
  backend/supabase/apply_migrations.sh

SUPABASE_DIR="$PWD/.runtime/supabase-dev" \
  backend/supabase/run_worker_queue_validation.sh
```

`apply_migrations.sh`는 migration 01~41을 적용한다. 실패하면 오류 migration과 마지막 성공
지점을 기록하고, 원인을 확인하지 않은 채 전체 명령을 반복하지 않는다.

Existing KB가 필요하면 [Existing KB 100건 bootstrap](EXISTING_KB_BOOTSTRAP.md)의 데이터팩
검증 → 관계형/Storage 적재 → Model 1 분류 → embedding → 4개 scope 검증 순서를 수행한다.
embedding에는 OpenAI 비용이 발생하며 최종 Profile 100건, embedding 400행이 확인돼야 한다.

개발용 로그인 계정이 필요한 경우에만 다음을 수행한다.

```bash
cd <repository>
mkdir -p .runtime
cp -n backend/supabase/dev-auth.env.example .runtime/pre-review-dev-auth.env
chmod 600 .runtime/pre-review-dev-auth.env
# @example.invalid 이메일과 8자 이상의 테스트 비밀번호를 파일 내부에 설정한다.

cd backend
PREREVIEW_ENVIRONMENT=development \
PREREVIEW_DEV_AUTH_BOOTSTRAP_ENABLED=true \
uv run python scripts/bootstrap_local_auth_user.py
```

운영 사용자 생성에는 이 bootstrap 스크립트를 사용하지 않는다.

## 4B. 현재 DB와 Storage를 완전히 복제

DB dump와 Storage archive는 같은 시점의 세트여야 한다. 원본에서 새 업로드를 막고 Backend를
중지한 뒤 백업한다. `down -v`는 사용하지 않는다.

```bash
cd <source-repository>/backend
docker compose -p backend stop -t 600 api worker chat-worker report-worker

install -d -m 700 /secure/backup/pre-review
cd <source-repository>/.runtime/supabase-dev

docker compose -p supabase \
  -f docker-compose.yml \
  -f docker-compose.pgvector.yml \
  -f docker-compose.auth-templates.yml \
  exec -T db pg_dump -U postgres -d postgres -Fc \
  > /secure/backup/pre-review/postgres.dump

tar -C volumes/storage -czf /secure/backup/pre-review/storage.tar.gz .
chmod 600 /secure/backup/pre-review/postgres.dump \
  /secure/backup/pre-review/storage.tar.gz
sha256sum /secure/backup/pre-review/postgres.dump \
  /secure/backup/pre-review/storage.tar.gz
```

원본 Backend가 계속 필요하면 `backend`에서 다시 기동한다.

```bash
docker compose -p backend up -d api worker chat-worker report-worker
```

대상에는 3절의 동일한 Supabase/PostgreSQL 버전을 설치한다. 대상 DB와 Storage가 비어 있고
삭제해도 되는 새 환경인지 확인한다. 복구 중 대상 Backend는 기동하지 않는다.

```bash
cd <target-repository>/.runtime/supabase-dev
docker compose -p supabase \
  -f docker-compose.yml \
  -f docker-compose.pgvector.yml \
  -f docker-compose.auth-templates.yml up -d db

docker compose -p supabase \
  -f docker-compose.yml \
  -f docker-compose.pgvector.yml \
  -f docker-compose.auth-templates.yml \
  exec -T db pg_restore \
  -U postgres -d postgres --clean --if-exists --exit-on-error \
  < /secure/backup/pre-review/postgres.dump
```

Storage archive는 새 환경의 빈 `volumes/storage`에만 푼다.

```bash
test -z "$(find volumes/storage -mindepth 1 -print -quit)"
tar -C volumes/storage -xzf /secure/backup/pre-review/storage.tar.gz

docker compose -p supabase \
  -f docker-compose.yml \
  -f docker-compose.pgvector.yml \
  -f docker-compose.auth-templates.yml up -d
```

전체 복구는 Supabase, PostgreSQL major version, role/owner가 원본과 같을 때만 수행한다.
다른 버전은 staging에서 먼저 검증한다. Storage archive를 빠뜨리면 DB에 객체 정보가 있어도
실제 파일을 읽을 수 없다.

## 5. Model 1과 Backend 준비

승인된 `OPENAI_*` 설정은 저장소 루트의 `.env`에 두고 mode `600`으로 제한한다. Supabase
설정과 키는 `.runtime/supabase-dev/.env`에 둔다. 두 파일의 값을 명령행에 직접 쓰거나
출력하지 않는다. 대상 환경에서 새 Supabase key를 생성했다면 기존 Backend `.env`를 그대로
복사하지 말고 새 key와 DB 접속정보로 다시 생성한다.

```bash
cd <repository>
python3 backend/scripts/prepare_model1_runtime.py \
  --archive /secure/imports/serving.zip \
  --destination .runtime/model1-serving/model1

chmod 600 .runtime/supabase-dev/.env
chmod 600 .env
cd backend
uv sync --frozen --extra dev
uv run python scripts/prepare_local_backend_env.py
chmod 600 .env
docker compose -p backend config --quiet
```

새 JWT/Auth key를 사용하는 대상에서는 복구된 사용자가 다시 로그인해야 하며 기존 세션은
유지되지 않는다.

생성기는 기존 `.env`를 덮어쓰지 않는다. 운영값을 승계할 때도 mode `600` 파일로 안전하게
복사하고 내용을 터미널에 출력하지 않는다.

## 6. Backend 기동과 확인

```bash
cd <repository>/backend
docker compose -p backend up -d --build api worker chat-worker report-worker
docker compose -p backend ps
curl -fsS http://127.0.0.1:8001/health/live
curl -fsS http://127.0.0.1:8001/health/ready
docker compose -p backend logs --tail=100 worker
docker compose -p backend logs --tail=100 chat-worker
docker compose -p backend logs --tail=100 report-worker
```

`ready=200`만으로 DB·Storage·worker를 검증했다고 판단하지 않는다. 테스트 계정으로 로그인해
작은 HWP/HWPX 업로드가 `202`, 분석 완료가 `succeeded`, 결과와 채팅 조회가 성공하는지 본다.
결과의 `report.can_download=true`를 확인한 뒤 소유자 Cookie로
`GET /api/v1/analysis-cases/{analysis_case_id}/report.pdf`가 `200 application/pdf`를
반환하는지도 확인한다. migration 41은 적용 전에 이미 완료된 case를 PDF queue에 넣지
않으므로 이 검증에는 migration 적용 이후 새로 완료한 분석을 사용한다.

## 7. 금지 사항

- `docker compose down -v`
- PostgreSQL major version이 다른 이미지에 기존 data directory 연결
- 비밀 `.env`, dump, Storage archive, `serving.zip`을 Git에 추가
- migration 실패 원인 확인 없이 반복 실행
- non-empty DB/Storage 위에 전체 dump/archive 덮어쓰기

상세 내용은 [Self-hosted Supabase 운영 안내](README.md),
[migration manifest](MIGRATION_MANIFEST.md),
[FastAPI·worker 운영 가이드](../fastapi/docs/FASTAPI_WORKER_RUNBOOK.md)를 참고한다.
