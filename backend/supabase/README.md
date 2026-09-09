# Self-hosted Supabase 운영 안내

이 디렉터리는 Supabase의 마이그레이션, Edge Function, 테스트, 배포 보조 도구를
관리한다. 실행 중인 DB, Storage 객체, Docker volume, 실제 `.env` 비밀값은 Git에
넣지 않는다.

## 구조

```text
브라우저 → Auth / Storage / api View·RPC / Realtime / Edge Function
                                                │
EC2 self-hosted Supabase ─ PostgreSQL · Storage · Edge Runtime
                                                │ Tailscale
GPU worker → HWP/HWPX → Common IR → Request Profile JSON → Edge 콜백
```

GPU worker에는 PostgreSQL URL, DB 비밀번호, service-role key를 주지 않는다.
signed URL과 worker 전용 콜백 토큰만 사용한다.

## 디렉터리

```text
migrations/  DB 스키마·RLS·View·RPC
functions/   Edge Function 소스와 배포 스크립트
tests/       마이그레이션 계약 테스트
.env.example EC2 경로·worker 연결 설정 예시
```

## 데이터 위치

| 대상 | 위치 |
|---|---|
| PostgreSQL / 향후 pgvector | `SUPABASE_DB_DATA_DIR` |
| 기존 공고 산출물 | `existing-kb` Storage |
| 사용자 요청 산출물 | `request-temp` Storage |
| PDF 보고서 | `analysis-reports` Storage |

벡터 검색은 별도 컨테이너가 아니라 같은 PostgreSQL의 `pgvector` 확장과
`retrieval` 스키마를 사용한다. 모델·차원·검색 단위가 미정이라 아직 적용하지 않는다.

## EC2 최초 배포

1. EC2에 Docker Engine·Compose plugin과 Tailscale을 설치한다. EC2와 GPU worker는
   같은 Tailnet에 연결한다.
2. 공식 self-hosted Supabase Compose 배포본을 `/srv/pre-review/supabase`처럼 Git
   밖의 경로에 설치하고, 해당 디렉터리의 `.env`에 Supabase 운영 비밀값을 설정한다.
3. 이 저장소에서 설정 파일을 만든다.

```bash
cp backend/supabase/.env.example backend/supabase/.env
```

최소 설정값은 다음과 같다.

```bash
SUPABASE_COMPOSE_DIR=/srv/pre-review/supabase
SUPABASE_DB_DATA_DIR=/data/pre-review/postgres
SUPABASE_STORAGE_DATA_DIR=/data/pre-review/storage
SUPABASE_INTERNAL_URL=http://supabase-ec2.<tailnet>.ts.net:8000
```

`SUPABASE_DB_DATA_DIR`는 EBS 데이터 볼륨에 두는 것을 권장한다. `/`, 저장소,
기존 다른 DB 경로를 지정하면 안 된다.

4. DB 영속 경로를 준비한다. 없으면 생성하며, 비어 있지 않은 기존 DB 경로는
   덮어쓰지 않는다.

```bash
backend/supabase/prepare_selfhosted.sh backend/supabase/.env
```

5. Supabase를 기동하고 마이그레이션을 적용한다.

```bash
cd /srv/pre-review/supabase
docker compose up -d
docker compose ps

SUPABASE_DIR=/srv/pre-review/supabase \
  /path/to/repository/backend/supabase/apply_migrations.sh
```

DB와 Storage가 healthy 상태가 된 뒤 마이그레이션을 적용한다. 이 스크립트는 DB를
초기화하거나 데이터를 지우지 않는다.

## worker와 Edge Function 배포

Compose 디렉터리의 `.env`에 worker 연결 정보를 설정한다.

```bash
ANALYSIS_WORKER_DISPATCH_URL=http://gpu-worker.<tailnet>.ts.net:8080/jobs
ANALYSIS_WORKER_DISPATCH_TOKEN=<비밀값>
ANALYSIS_WORKER_INGEST_PROFILE_URL=http://supabase-ec2.<tailnet>.ts.net:8000/functions/v1/edge-analysis-run-ingest-request-profile
ANALYSIS_WORKER_CALLBACK_TOKEN=<별도-비밀값>
```

```bash
cp backend/supabase/docker-compose.worker.override.yml.example \
  /srv/pre-review/supabase/docker-compose.pre-review-worker.yml
cd /srv/pre-review/supabase
docker compose -f docker-compose.yml -f docker-compose.pre-review-worker.yml up -d functions

/path/to/repository/backend/supabase/functions/deploy_local.sh \
  /srv/pre-review/supabase/volumes/functions
docker compose restart functions
```

## Request 처리 흐름

1. Edge Function이 브라우저 업로드 경로를 예약한다.
2. 브라우저가 HWP/HWPX를 `request-temp`에 업로드한다.
3. Edge Function이 GPU worker에 작업을 요청한다.
4. worker가 Common IR과 Request Profile JSON을 생성한다.
5. worker가 Tailscale Edge Function 콜백으로 결과를 보낸다.
6. Edge Function이 `workspace.ingest_request_profile_core` RPC로 적재한다.

## 검증

```bash
cd backend
UV_CACHE_DIR=/tmp/pre_review_uv_cache \
  uv run --extra dev pytest supabase/tests/test_migration_contract.py -q
```

## 트러블슈팅

### 재기동 후 DB가 비어 있음

`SUPABASE_DB_DATA_DIR`와 Compose의 `volumes/db/data` 심볼릭 링크를 확인한다.
경로가 새 빈 디렉터리로 바뀌면 PostgreSQL은 새 DB를 초기화한다.

### Storage 마이그레이션 오류

DB와 Storage 컨테이너가 healthy가 된 뒤 `apply_migrations.sh`를 다시 실행한다.

### worker 콜백이 401임

GPU worker가 Tailnet URL에 연결되는지, `x-worker-callback-token`이
`ANALYSIS_WORKER_CALLBACK_TOKEN`과 같은지 확인한다.

### 프론트에서 api View/RPC가 보이지 않음

PostgREST의 `PGRST_DB_SCHEMAS`에 `api`가 있는지 확인하고 REST 컨테이너를
재기동한다.
