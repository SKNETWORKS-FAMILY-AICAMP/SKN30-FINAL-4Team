# PreReview

HWP/HWPX 사전협의 요청서를 구조화하고, 기존 기업마당 지원사업과 비교하는 서비스다.
브라우저의 공개 업무 API는 FastAPI 하나이며, Supabase는 서버 내부의 인증·PostgreSQL/
pgvector·private Storage 인프라로 사용한다.

```text
Frontend (HttpOnly Cookie)
  → FastAPI /api/v1
      ├─ Supabase Auth
      ├─ PostgreSQL + pgvector / private Storage
      └─ same-server polling worker
          claim → HWP/HWPX → Common IR → OpenAI → result
```

## 먼저 알아둘 범위

현재 구현된 범위는 다음과 같다.

- Supabase Auth를 FastAPI가 감싼 HttpOnly Cookie 로그인/세션 확인
- HWP/HWPX 요청서 업로드, PostgreSQL queue 생성, worker polling
- Common IR·Request Profile 생성, Existing Profile pgvector 검색, CPL/FIT/SIM 결과 조회
- 분석 상태 polling, 결과·후보·활성 세션·이력 조회

아직 공개 API가 없는 범위도 있다.

- 채팅 메시지 생성·재시도와 PDF signed URL/보고서 생성
- 비밀번호 재설정 메일의 token/PKCE를 Cookie 세션으로 교환하는 callback
- SSE/WebSocket 상태 push, PDF/OCR 요청 처리

프론트 화면은 현재 UI 구현이 우선되어 있다. 로그인 상태, 업로드, 결과와 이력은
일부 mock 상태이므로, 아래 API 계약에 맞춘 서비스 계층 연결은 프론트 작업으로 남아
있다. 특히 업로드 UI의 안내와 실제 API의 허용 형식이 다를 수 있으므로 실제 연동은
**HWP/HWPX, 최대 50 MiB** 계약을 따른다.

## 기본 주소와 연결 규칙

| 구성요소 | 기본 주소/포트 | 외부 공개 여부 |
|---|---|---|
| Frontend Vite | `http://localhost:3000` | 개발 PC에서 사용 |
| FastAPI | `http://localhost:8001` | 브라우저 업무 API |
| FastAPI Swagger | `http://localhost:8001/docs` | 개발·운영자 확인용 |
| Supabase gateway | `http://localhost:8000` | 서버 내부 인프라 |
| polling worker | 호스트 publish 없음 | same-server 내부 프로세스 |

프론트 API base URL은 `http://localhost:8001/api/v1`이다. 실제 요청은 Cookie를
포함해야 하며 Bearer token을 직접 저장하거나 보내지 않는다.

```ts
fetch("http://localhost:8001/api/v1/auth/me", {
  credentials: "include",
})
```

로컬 HTTP PoC에서 FastAPI 환경은 최소 다음처럼 맞춘다.

```dotenv
PREREVIEW_OFFLINE_MODE=false
PREREVIEW_AUTH_ALLOWED_ORIGINS=http://localhost:3000,http://localhost:8001
PREREVIEW_AUTH_COOKIE_SECURE=false
PREREVIEW_AUTH_COOKIE_SAMESITE=lax
```

`3000`은 Vite 프론트용이며, `8001`을 함께 넣으면 Swagger UI에서 Auth POST를 직접
시험할 수 있다. 상태 변경 Auth/업로드 요청은 Origin allow-list가 비어 있거나 다르면
403으로 거부된다. 운영에서는 HTTPS와 `PREREVIEW_AUTH_COOKIE_SECURE=true`를 사용한다.

## 실행 방법 선택

### 1. 프론트 화면만 실행

API 없이 화면·스타일을 확인하는 가장 가벼운 방법이다. Node.js 24 계열을 권장한다.

```bash
cd frontend
npm ci
npm run dev
```

Vite는 `http://localhost:3000`에서 실행된다. 이 모드에서는 로그인, 파일 분석, 이력과
결과가 실제 서버 데이터와 연결되지 않는다.

### 2. 팀의 shared backend에 프론트 연결

Supabase와 backend가 이미 팀 서버에서 기동 중이면 프론트 개발자는 서버 담당자에게
FastAPI base URL과 허용된 프론트 origin만 받으면 된다.

1. `frontend`를 위 방법으로 `3000`에서 기동한다.
2. 서버 담당자가 `PREREVIEW_AUTH_ALLOWED_ORIGINS`에 프론트의 정확한 origin을 추가한다.
3. 프론트 API 클라이언트는 `<FastAPI base>/api/v1`을 사용하고 모든 요청에
   `credentials: "include"`를 설정한다.
4. 로그인은 `POST /auth/sign-in`, 업로드는 `POST /analysis-runs`, 상태는
   `GET /analysis-runs/{id}` polling, 성공 결과는 `GET /analysis-cases/{id}` 순서로 연결한다.
   업로드마다 프론트가 UUID v4 `Idempotency-Key`를 만들고 동일 요청 재시도에는 같은 값을 쓴다.

Supabase URL/key, service-role key, PostgreSQL URL, OpenAI key를 프론트에 전달하지
않는다. 현재 frontend에는 API base 환경변수나 실제 API service가 아직 없으므로, 이
단계는 해당 프론트 연동 구현 후에 유효하다.

프론트와 API가 같은 PC의 `localhost`에서 포트만 다르면 위 Cookie 설정을 사용할 수 있다.
서로 다른 PC의 LAN IP를 평문 HTTP로 직접 연결하면 CORS 허용과 별개로 브라우저의
SameSite Cookie 정책에 막힐 수 있다. shared backend 개발에서는 Vite가 `/api`를 backend로
proxy해 브라우저 요청을 같은 origin으로 만들거나, HTTPS와 배포 환경에 맞는 Cookie
정책을 사용한다.

### 3. full-local: Supabase부터 worker까지 실행

이 모드는 Docker, OpenAI API key, self-hosted Supabase와 Existing KB 데이터가 필요하다.
저장소에는 Supabase 본체나 운영 비밀값을 넣지 않으며, 설치 스크립트가 official
self-hosted Supabase **v0.8.0** bundle을 Git 제외 경로에 준비한다. 스크립트는 파일과
비밀값만 생성하고 컨테이너 기동·migration·DB 초기화는 하지 않는다.

1. 저장소 루트에서 빈 Supabase Compose stack을 준비한다. 기본 설치 위치는
   `.runtime/supabase-dev`다.

   ```bash
   backend/supabase/install_selfhosted_local.sh
   ```

   저장소 밖의 경로를 쓰려면 `--target /srv/pre-review/supabase`처럼 새 절대경로를
   지정한다. 자세한 사전 조건과 안전 제한은
   [Supabase 운영 안내](backend/supabase/README.md)를 따른다.

2. PostgreSQL을 별도 영속 경로에 둘 경우 host-path 설정을 준비한다. 기본 Compose
   내부 경로를 그대로 쓸 경우 이 단계는 건너뛴다.

   ```bash
   cp backend/supabase/.env.example backend/supabase/.env
   # SUPABASE_COMPOSE_DIR을 설치 위치로, SUPABASE_DB_DATA_DIR을 전용 영속 경로로 설정
   backend/supabase/prepare_selfhosted.sh backend/supabase/.env
   ```

3. installer가 함께 배치한 pgvector override로 Supabase를 기동하고, 저장소 루트에서
   migration을 적용한다. 아래는 기본 설치 위치 기준이며 외부 target을 골랐다면 해당
   절대경로로 바꾼다.

   ```bash
   cd /path/to/SKN30-FINAL-4Team/.runtime/supabase-dev
   docker compose -f docker-compose.yml -f docker-compose.pgvector.yml up -d
   docker compose ps

   cd /path/to/SKN30-FINAL-4Team
   SUPABASE_DIR="$PWD/.runtime/supabase-dev" \
     backend/supabase/apply_migrations.sh
   ```

   migration은 `existing-kb`, `request-temp`, `analysis-reports` private bucket과 DB schema를
   만든다. migration 적용 전에는 DB volume과 backup 대상을 반드시 확인한다.

4. backend runtime `.env`를 만들고 server-only 값을 채운다. 기본 installer의 Supabase
   `.env`와 저장소 루트 `.env`의 OpenAI 설정이 준비되어 있으면 값을 복붙하지 않고
   안전하게 조립할 수 있다.

   ```bash
   cd /path/to/SKN30-FINAL-4Team
   chmod 600 .env .runtime/supabase-dev/.env

   cd backend
   uv sync --frozen --extra dev
   uv run python scripts/prepare_local_backend_env.py
   ```

   생성기는 기존 `backend/.env`를 덮어쓰지 않고 mode `600`으로 새 파일만 만든다.
   다른 경로·LAN 프론트 origin 옵션은
   [FastAPI·worker 운영 가이드](backend/fastapi/docs/FASTAPI_WORKER_RUNBOOK.md)를 따른다.
   자동 조립을 쓰지 않으면 `.env.example`을 직접 복사하고 `SUPABASE_URL`, anon/service
   key, `DATABASE_URL`, `OPENAI_API_KEY`, Cookie/CORS origin을 실제 환경에 맞게 설정한다.
   값은 Git·채팅·프론트에 넣지 않는다.

5. FastAPI와 worker를 함께 기동한다.

   ```bash
   cd backend
   docker compose config --quiet
   docker compose up -d --build
   docker compose ps
   ```

   `api`는 host `8001`로 publish되며, `worker`는 포트를 열지 않는다. worker가 없으면
   업로드된 분석은 `queued`에 머문다.

### Existing KB 데이터는 별도 전달물이다

빈 Supabase에 migration만 적용하면 스키마와 bucket만 준비된다. 유사도 비교에 필요한
Existing 공고 원본·Common IR·Profile·pgvector embedding, 테스트 Auth user, 분석용 OpenAI
key는 Git에 포함하지 않는다.

full-local 분석 E2E에는 다음 둘 중 하나가 필요하다.

- 민감정보를 제거한 PostgreSQL + Storage backup/restore bundle
- Existing 공고 data pack(예: 100건 metadata·원본·Common IR·Profile·ingestion record)과
  trusted importer 및 embedding 실행 절차

후자의 importer는 서버 전용 `DATABASE_URL`, Supabase service-role key, OpenAI key를
사용한다. Existing KB import와 embedding 적재 상세는
[Existing KB 100건 bootstrap 가이드](backend/supabase/EXISTING_KB_BOOTSTRAP.md)를 따른다.
샘플 HWP/HWPX도 별도로 제공받거나 본인의 테스트 문서를 사용해야 한다.

## 확인 순서

```bash
# backend 컨테이너 상태와 API 생존성
curl -fsS http://127.0.0.1:8001/health/live
curl -fsS http://127.0.0.1:8001/health/ready

# worker 상태는 별도로 확인
cd backend
docker compose ps
docker compose logs --tail=100 worker
```

`/health/ready`는 현재 환경변수와 API repository 조립을 확인하는 endpoint다. worker
생존, queue 지연, Existing KB 적재 여부까지 보장하지 않는다. 실제 E2E는 테스트 계정으로
로그인한 뒤 작은 HWP/HWPX를 업로드하고 `analysis-runs`를 polling해 확인한다. OpenAI 비용이
발생할 수 있다.

## 상세 문서

- [프론트 개발 안내](frontend/README.md)
- [backend 개요](backend/README.md)
- [FastAPI·worker 배포 및 운영 가이드](backend/fastapi/docs/FASTAPI_WORKER_RUNBOOK.md)
- [프론트엔드 FastAPI API 계약](backend/fastapi/docs/0.FASTAPI_FRONTEND_API_SPEC.md)
- [FastAPI 인증·Cookie 계약](backend/AUTH_API_CONTRACT.md)
- [worker 결과 저장 계약](backend/fastapi/docs/WORKER_RESULT_PERSISTENCE_CONTRACT.md)
- [Supabase 설치·migration·Existing embedding 운영](backend/supabase/README.md)
- [Existing KB 100건 검증·적재·embedding](backend/supabase/EXISTING_KB_BOOTSTRAP.md)
- [현재 구현 현황과 제한](backend/IMPLEMENTATION_STATUS.md)

과거 JWT/Bearer·`sims.*` DB·직접 Supabase 호출·Edge Function dispatch/callback 문서는
현재 runtime 계약이 아니다. 현재 경계는 **Frontend → FastAPI → Supabase/worker**다.
