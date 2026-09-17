# FastAPI·worker 배포 및 운영 가이드

마지막 문서 동기화: 2026-09-17

이 날짜의 통합 checkout에서는 공식 `supabase/postgres:17.6.1.169` 임시 DB의 기존
`01`~`38` fresh 검증 상태에 `39`~`41`을 upgrade하고 전체 `01`~`41` replay를 검증했다.
migration `42`는 통합 전 `backend-rebuild`가 게시한 중간 v4 identity를 비활성 이력으로
보존한다. 통합 runtime은 Model 2/3 재시도를 실행 adapter로 옮겨 Model 1 manifest가
migration 31·32·38의 v3 identity와 다시 일치한다. 실제 DB에는 배포 전에 `42`까지
적용하되 v4 row를 재분류·승격 대상으로 사용하지 않는다.
현재 로컬 DB 적용, 실제 repository SQL, queue/provenance runtime contract와 두 세션
`40001` lock retry도 통과했다. Existing 100건 Model 1 실제 추론 backfill은 100건 모두
`OK`이고 idempotent 재실행은 100건 모두 skip됐다. 활성 결과는 `신뢰` 99건과 raw
`판단보류` 1건이다. 로컬 DB의 v2 임베딩은 100건 × 4 scope(활성 400행)로 준비됐다.
합성 HWPX를 사용한 **host inline** 및 전용 Docker ML
worker **external** OpenAI live E2E에서 Request Profile 구조화(Terra), FIT·SIM·Model
1/2/3 결과 저장과 결과 근거 채팅(Luna)을 모두 완주했다. 전체 backend pytest는
906개를 수집해 `903 passed, 3 skipped`였고, 기본 testpaths 밖의 Supabase 정적 계약
24개와 실제 PostgreSQL runtime 계약도 별도로 통과했다.

이 문서는 self-hosted Supabase가 준비된 뒤 FastAPI와 same-server polling worker를
설정하고 운영하는 방법을 설명한다. Supabase 자체 설치·영속 볼륨·migration 절차는
[Supabase 운영 안내](../../supabase/README.md)를 따른다.

## 1. 실행 구조

```text
Frontend
  │  HTTPS + HttpOnly Cookie
  ▼
Reverse proxy
  │
  ▼
FastAPI (`api`, container 8000 / host 기본 8001)
  ├─ Supabase Auth
  ├─ private Storage
  └─ PostgreSQL에 `queued` 분석 작업 생성
                         │
                         ▼
               polling worker (`worker`, 호스트 publish 없음)
               claim → HWP/HWPX parse → OpenAI → 결과 저장
```

`backend/compose.yaml`은 `api`, analysis `worker`, `chat-worker`, `report-worker`를 실행한다. Supabase는 별도 Compose stack으로
먼저 실행되어 있어야 한다. Redis/RQ, Edge Function dispatch/callback, 외부 worker HTTP
서버는 현재 경로에서 사용하지 않는다.

`worker`만 CPU 전용 `Dockerfile.ml-worker`를 사용한다. 이 이미지는 Model 2/3의 필요한
코드·artifact와 child Python을 포함하고 Model 1은 절대 복사하지 않는다. `api`와
`chat-worker`는 기본 `Dockerfile`로 실행되므로 ML dependency와 Model 1 내용을 가지지
않는다. `report-worker`는 `Dockerfile.report-worker`의 Chromium 전용 이미지로 실행한다. Model 1의 검증된 외부 runtime은 worker에만 read-only bind mount된다.

## 2. 기동 전 확인

다음 조건이 먼저 충족되어야 한다.

- self-hosted Supabase Auth·PostgreSQL/pgvector·Storage가 실행 중이다.
- `backend/supabase/migrations/01`부터 `42`까지 적용되어 있다.
- private bucket `existing-kb`, `request-temp`, `analysis-reports`가 생성되어 있다.
- Existing Profile 100건과 active v2 `retrieval.existing_profile_embedding` 100건 × 4 scope가 준비되어 있다.
- FastAPI·worker 컨테이너에서 Supabase gateway와 PostgreSQL에 접근할 수 있다.
- 서버에서 OpenAI API에 HTTPS로 접근할 수 있다.

새 DB에 Existing Profile과 embedding을 준비하는 절차는
[Existing KB bootstrap 가이드](../../supabase/EXISTING_KB_BOOTSTRAP.md)를 따른다.

### 새 로컬 환경의 준비 순서

처음부터 재현할 때는 다음 순서를 지킨다.

1. self-hosted Supabase를 기동하고 Auth·DB·Storage가 healthy인지 확인한다.
2. migration 01~42를 적용한다.
3. Git 밖의 Model 1 runtime을 준비하고, server-only `backend/.env`를 생성한다.
4. Existing 100건 data pack을 검증·import하고 관계형 KB/Storage를 검증한다.
5. Existing 분류 결과를 쓰는 기능까지 검증할 때는 current Profile 100건에 Model 1을
   dry-run 후 backfill하고 분류 설정을 활성화한다.
6. v2 embedding을 dry-run 후 100 × 4 scope로 backfill·활성화한다.
7. 아래 host-side 스크립트로 로컬 개발용 Auth 사용자를 **명시적으로** 한 번 준비한다.
8. FastAPI `api`, 전용 Docker ML analysis `worker`, `chat-worker`, `report-worker`를 기동한다.
9. Swagger에서 `sign-in` → `me` 또는 live E2E에서 `sign-in` → HWP/HWPX upload → 상태
   poll → 결과/채팅 순서로 확인한다.

`prepare_local_backend_env.py`로 `.env`를 생성한 경우에도 online history를 쓰기 전에
`PREREVIEW_CURSOR_SIGNING_SECRET`을 별도 secret store/`.env`에 설정한다. API 전 인스턴스가
같은 충분히 긴 값을 사용해야 하며, rotation은 발급된 analysis/conversation cursor를 무효화한다.

전체 분석을 안 하고 로그인·`/me`만 확인할 때는 Existing import·Model 1·
embedding을 생략할 수 있다. 현재 분석 E2E에는 Existing import와 v2 embedding이
필수지만 Existing Model 1 분류는 아직 조회 경로에 연결되지 않아 필수 조건이 아니다.
분류 소비 기능까지 포함한 전체 bootstrap에서는 `Existing import → Model 1 분류 →
v2 embedding → Auth/E2E`의 순서를 사용한다.

Supabase 설치·migration 및 로컬 Auth 준비의 반대쪽 안내는
[Supabase 운영 안내](../../supabase/README.md)에 있다.

### 로컬 개발용 Auth 사용자 준비

이 절차는 로컬 Swagger·프론트 수동 연동을 위한 고정 개발 계정을 만드는 용도다. 실제
credential은 Git에서 제외되는 `.runtime/pre-review-dev-auth.env` 한 곳에만 두고 mode를
`600`으로 제한한다.

```bash
cd /path/to/SKN30-FINAL-4Team
mkdir -p .runtime
cp -n backend/supabase/dev-auth.env.example .runtime/pre-review-dev-auth.env
chmod 600 .runtime/pre-review-dev-auth.env

# 파일 안의 빈 값을 로컬 전용 email/password로 채운 뒤 실행한다.
cd backend
PREREVIEW_ENVIRONMENT=development \
PREREVIEW_DEV_AUTH_BOOTSTRAP_ENABLED=true \
uv run python scripts/bootstrap_local_auth_user.py
```

기본 Supabase 위치는 저장소의 `.runtime/supabase-dev`, 기본 credential 파일은
`.runtime/pre-review-dev-auth.env`다. 다른 로컬 경로가 필요할 때만
`--supabase-dir PATH`, `--credentials PATH`를 사용한다. credential 파일에는
`PREREVIEW_DEV_AUTH_EMAIL`, `PREREVIEW_DEV_AUTH_PASSWORD`를 넣고, 선택 역할
`PREREVIEW_DEV_AUTH_ROLE`은 현재 `user`만 허용한다. password는 8자 이상이면서 UTF-8
인코딩 기준 72바이트 이하여야 한다. 스크립트는 생성/재사용 상태만
출력하며 email·password·Supabase key·token은 출력하지 않는다.

fresh installer로 만든 bundle에는 관리 marker `.pre-review-supabase-version`이 자동으로
존재한다. 현재 PC의 과거 수동 설치처럼 marker가 없는 **기존 로컬 bundle만** 다음
override를 추가한다.

```bash
cd /path/to/SKN30-FINAL-4Team/backend
PREREVIEW_ENVIRONMENT=development \
PREREVIEW_DEV_AUTH_BOOTSTRAP_ENABLED=true \
uv run python scripts/bootstrap_local_auth_user.py --allow-unmanaged-local
```

`--allow-unmanaged-local`은 installer marker 확인만 우회한다. loopback Supabase 주소,
`development`/명시적 enable guard, `@example.invalid` 개발 email, credential mode `600`
검사는 우회하지 않으며 LAN·원격 주소를 허용하는 옵션이 아니다.

이 스크립트는 `install_selfhosted_local.sh`, Docker Compose 기동, migration 적용에 자동으로
포함되지 않는다. 계정 생성은 반드시 개발자가 별도로 실행하며 staging·운영에서는 이
스크립트와 개발 credential 파일을 사용하지 않는다.

`scripts/run_local_live_e2e.py`가 매 실행마다 만드는 임의 계정과도 용도가 다르다. 임의
계정은 한 번의 격리된 자동 E2E용이고 DB에 사용자·결과를 남길 수 있다. 위 고정 개발
계정은 사람이 Swagger와 프론트 연결을 반복 확인하기 위한 것이며, E2E가 만든 비밀번호를
공용 개발 로그인으로 재사용하지 않는다.

Supabase와 backend가 같은 호스트의 서로 다른 Compose stack이라면 이 저장소의 기본값처럼
`host.docker.internal`을 사용한다. `compose.yaml`이 Linux의 host gateway mapping을
추가한다. 두 stack을 하나의 명시적 Docker network에 연결한 배포라면 운영자가 정한
내부 DNS 이름으로 교체할 수 있다.

PostgreSQL 포트와 Supabase gateway 포트는 인터넷에 공개하지 않는다. EC2에서는
Tailscale 또는 호스트 내부 network로만 접근시키고, 외부에는 reverse proxy의 443만
노출하는 구성을 권장한다.

## 3. `.env` 만들기

서버에는 역할이 다른 환경 파일이 세 종류 있다.

| 파일 | 역할 |
|---|---|
| Supabase 설치 디렉터리의 `.env` | 공식 Supabase Compose의 DB/JWT/SMTP 설정 |
| `backend/supabase/.env` | 영속 DB 경로 준비 스크립트용이며 런타임 비밀값을 넣지 않음 |
| `backend/.env` | FastAPI·worker에 전달할 Supabase/DB/OpenAI 런타임 설정 |

Compose는 `backend` 디렉터리의 `.env`를 읽는다. 저장소 루트 `.env`에 OpenAI 설정이
있고 공식 Supabase bundle이 `.runtime/supabase-dev`에 설치된 로컬 개발 환경에서는
값을 복사해 터미널에 노출하지 말고 전용 생성기를 사용한다.

```bash
cd /path/to/SKN30-FINAL-4Team

# 1) serving.zip의 allowlist만 Git 밖의 runtime에 새로 배치하고 고정 digest를 확인한다.
# 기본 archive는 $HOME/serving.zip이다. 다른 안전한 로컬 사본일 때만 --archive를 쓴다.
python3 backend/scripts/prepare_model1_runtime.py \
  --destination .runtime/model1-serving/model1

# 2) 입력 비밀 파일도 현재 Linux 사용자만 읽을 수 있게 한다.
chmod 600 .env .runtime/supabase-dev/.env

# 3) 생성기는 위 model1 runtime의 절대 bind 경로와 숫자 UID/GID도 함께 기록한다.
cd backend
uv sync --frozen --extra dev
uv run python scripts/prepare_local_backend_env.py
```

생성기는 다음 작업만 수행한다.

- 저장소 루트 `.env`에서 `OPENAI_*` 설정을 읽는다.
- 공식 Supabase `.env`에서 anon/service-role key와 pooler 접속 정보를 읽는다.
- DB 비밀번호와 DB 이름을 URL encoding해
  `host.docker.internal:5432`용 `DATABASE_URL`을 만든다.
- 컨테이너용 `SUPABASE_URL=http://host.docker.internal:8000`, 로컬 Vite·Swagger CORS,
  HTTP 개발용 Cookie 설정을 함께 기록한다.
- 준비된 `model1` 디렉터리의 절대 bind 경로와 숫자 owner UID/GID를
  `PREREVIEW_MODEL1_SERVING_HOST_DIR`, `PREREVIEW_MODEL1_RUNTIME_UID`,
  `PREREVIEW_MODEL1_RUNTIME_GID`에 기록한다.
- 새 `backend/.env`를 mode `600`으로만 생성한다. 기존 파일이 있으면 실패하며
  덮어쓰지 않는다.

생성기는 비밀값을 출력하지 않고 컨테이너를 시작하거나 네트워크를 호출하지 않는다.
LAN 프론트 origin을 추가하려면 다음처럼 옵션을 반복한다. 기본 localhost origin도
그대로 유지된다.

```bash
uv run python scripts/prepare_local_backend_env.py \
  --frontend-origin http://192.168.0.67:3000
```

기본 위치가 아닌 Supabase bundle을 사용할 때는 `--supabase-env`, 별도 결과를 안전하게
검토할 때는 `--output`을 지정할 수 있다. 어느 경우에도 기존 출력 파일은 덮어쓰지 않는다.
실제 `.env`는 Git에 추가하거나 채팅·이슈·로그에 붙여 넣지 않는다.

입력 파일을 따로 가지고 있지 않거나 운영 HTTPS 값을 직접 구성하는 경우에는
`.env.example`을 복사한 뒤 mode `600`으로 제한하고 편집한다. 다음은 값의 형태만
보여주는 예시다.

```bash
cd /path/to/SKN30-FINAL-4Team/backend
cp .env.example .env
chmod 600 .env
```

```dotenv
# 공개 API
PREREVIEW_OFFLINE_MODE=false
PREREVIEW_API_BIND_ADDRESS=127.0.0.1
PREREVIEW_API_PORT=8001
# FastAPI가 읽는 각 업로드 파일의 최대 바이트(50 MiB 기본).
PREREVIEW_UPLOAD_MAX_BYTES=52428800
# multipart boundary/header를 포함한 HTTP 요청 전체의 최대 바이트(51 MiB 기본).
# 앞단 reverse proxy도 같은 값으로 제한한다.
PREREVIEW_HTTP_MAX_BODY_BYTES=53477376
# Uvicorn이 한 API replica에서 동시에 처리할 최대 연결 수.
PREREVIEW_API_LIMIT_CONCURRENCY=32
# 분석 upload reservation과 chat create/retry가 공유하는 전역 queued backlog 상한.
PREREVIEW_GLOBAL_QUEUE_MAX=25
# analysis/conversation history cursor용 HMAC 비밀값. 충분히 긴 임의값을 쓰고,
# API 재기동/복수 인스턴스 간에 유지한다. 교체하면 기존 cursor는 무효가 된다.
PREREVIEW_CURSOR_SIGNING_SECRET=<long-random-server-secret>

# 브라우저 인증/CORS/CSRF
PREREVIEW_AUTH_ALLOWED_ORIGINS=https://app.example.com
PREREVIEW_AUTH_COOKIE_SECURE=true
PREREVIEW_AUTH_COOKIE_SAMESITE=lax
PREREVIEW_AUTH_COOKIE_DOMAIN=
PREREVIEW_AUTH_REFRESH_COOKIE_MAX_AGE=2592000
PREREVIEW_AUTH_PASSWORD_RESET_CALLBACK_URL=https://app.example.com/api/v1/auth/password-recovery/callback
PREREVIEW_AUTH_PASSWORD_RESET_REDIRECT_TO=https://app.example.com/password-reset/update

# Supabase Auth·private Storage
SUPABASE_URL=http://host.docker.internal:8000
SUPABASE_ANON_KEY=<anon-key>
SUPABASE_SECRET_KEY=<service-role-or-secret-key>
SUPABASE_SERVICE_ROLE_KEY=

# trusted PostgreSQL 연결
DATABASE_URL=postgresql://<user>:<url-encoded-password>@host.docker.internal:<db-port>/postgres
SUPABASE_DB_URL=

# LLM은 OpenAI(기본) 또는 **experimental** OpenAI-compatible vLLM을 선택한다. embedding은
# 별도 provider 경계이며 현재 OpenAI만 지원한다.
PREREVIEW_LLM_PROVIDER=openai
PREREVIEW_EMBEDDING_PROVIDER=openai

# OpenAI embedding은 LLM provider와 무관하게 analysis worker에 항상 필요하다.
OPENAI_API_KEY=<openai-api-key>
# 모든 LLM 단계의 fallback. 단계별 override가 비어 있으면 이 모델을 사용한다.
OPENAI_LLM_MODEL=gpt-5.6-terra
# Request Profile 구조화는 별도 모델을 권장한다.
OPENAI_REQUEST_PROFILE_MODEL=gpt-5.6-terra
# 아래 단계별 override는 필요할 때만 설정한다.
OPENAI_CPL_MODEL=
OPENAI_FIT_MODEL=
OPENAI_SIM_MODEL=
OPENAI_CHAT_MODEL=
OPENAI_EMBEDDING_MODEL=text-embedding-3-small
OPENAI_TIMEOUT_SECONDS=120
OPENAI_MAX_REPAIRS=2

# PREREVIEW_LLM_PROVIDER=vllm일 때의 OpenAI-compatible endpoint.
# HTTPS만 허용한다. local 개발 예외는 localhost/127.0.0.0/8/::1 HTTP뿐이다.
# VLLM_LLM_MODEL은 공통 fallback이고, 단계별 값은 선택 사항이다.
VLLM_BASE_URL=https://<internal-vllm>/v1
VLLM_API_KEY=<internal-token>
VLLM_LLM_MODEL=<served-model-id>
VLLM_REQUEST_PROFILE_MODEL=
VLLM_CPL_MODEL=
VLLM_FIT_MODEL=
VLLM_SIM_MODEL=
VLLM_CHAT_MODEL=
VLLM_TIMEOUT_SECONDS=120
VLLM_MAX_REPAIRS=2
# LLM은 final profile이 아니라 source selection만 내고 final profile은 local materialize한다.
# 16,384는 16.8KB selection fixture보다 충분한 여유를 준다 (1..32768).
VLLM_MAX_OUTPUT_TOKENS=16384
VLLM_MAX_RESPONSE_BYTES=1048576

# PostgreSQL polling worker
PREREVIEW_WORKER_HEARTBEAT_SECONDS=30
PREREVIEW_WORKER_LEASE_SECONDS=120
PREREVIEW_WORKER_IDLE_POLL_SECONDS=1
PREREVIEW_WORKER_TOP_K=5
# true면 Existing KB 또는 retrieval 결과 부재가 fail-closed이고, false면 KB_EMPTY 완료를 허용한다.
PREREVIEW_EXISTING_KB_REQUIRED=true
PREREVIEW_REQUEST_NATIVE_EXACT_CANDIDATE_MODE=off
PREREVIEW_EXISTING_NATIVE_EXACT_CANDIDATE_MODE=off
PREREVIEW_WORKER_STORAGE_TIMEOUT_SECONDS=30
PREREVIEW_WORKER_DATABASE_CONNECT_TIMEOUT_SECONDS=10
PREREVIEW_WORKER_PARSE_TIMEOUT_SECONDS=120
PREREVIEW_FREETYPE_LIB=/usr/lib/x86_64-linux-gnu/libfreetype.so.6

# Docker analysis worker 전용 Model 1 read-only bind identity
PREREVIEW_MODEL1_SERVING_HOST_DIR=/absolute/path/to/.runtime/model1-serving/model1
PREREVIEW_MODEL1_RUNTIME_UID=1000
PREREVIEW_MODEL1_RUNTIME_GID=1000
PREREVIEW_ML_TIMEOUT_SECONDS=180

# PDF report worker (host 직접 실행용 .env.host.local에서만 절대경로 설정)
PREREVIEW_REPORT_CHROMIUM_EXECUTABLE=
PREREVIEW_REPORT_WORKER_HEARTBEAT_SECONDS=30
PREREVIEW_REPORT_WORKER_LEASE_SECONDS=120
PREREVIEW_REPORT_WORKER_IDLE_POLL_SECONDS=1
PREREVIEW_REPORT_RENDER_TIMEOUT_SECONDS=60
PREREVIEW_REPORT_STORAGE_TIMEOUT_SECONDS=30
PREREVIEW_REPORT_DATABASE_CONNECT_TIMEOUT_SECONDS=10
PREREVIEW_REPORT_MAX_BYTES=26214400
PREREVIEW_REPORT_DOWNLOAD_CONCURRENCY=2
PREREVIEW_REPORT_WORKER_MEMORY_LIMIT=1g
```

### 환경 변수와 사용 주체

`O`는 그 process가 값을 읽는다는 뜻이며 필수 여부를 뜻하지 않는다.

| 변수 | API | worker | live 필수 여부 | 설명 |
|---|:---:|:---:|---|---|
| `PREREVIEW_OFFLINE_MODE=false` | O | - | 필수 | 실제 Supabase repository를 활성화 |
| `PREREVIEW_UPLOAD_MAX_BYTES` | O | - | 선택(기본 50 MiB) | multipart에서 추출한 단일 HWP/HWPX 파일의 바이트 상한 |
| `PREREVIEW_HTTP_MAX_BODY_BYTES` | O | - | 선택(기본 51 MiB) | multipart boundary/header와 모든 part를 포함한 HTTP 요청 전체 상한. `PREREVIEW_UPLOAD_MAX_BYTES`와 별개 |
| `PREREVIEW_API_LIMIT_CONCURRENCY` | Compose | - | 선택(기본 32) | Compose가 Uvicorn `--limit-concurrency`로 전달하는 replica별 동시 연결 상한 |
| `PREREVIEW_REPORT_MAX_BYTES` | O | O (report-worker) | 선택(기본·상한 25 MiB) | API 다운로드, worker 렌더 결과, DB/Storage가 공유하는 PDF byte 상한 |
| `PREREVIEW_REPORT_DOWNLOAD_CONCURRENCY` | O | - | 선택(기본 2, 상한 8) | replica별 in-memory PDF 검증·다운로드 동시 처리 수 |
| `PREREVIEW_REPORT_WORKER_HEARTBEAT_SECONDS` | - | O (report-worker) | 선택(기본 30초) | PDF claim heartbeat |
| `PREREVIEW_REPORT_WORKER_LEASE_SECONDS` | - | O (report-worker) | 선택(기본 120초, 30~3600초) | PDF processing fence lease |
| `PREREVIEW_REPORT_WORKER_IDLE_POLL_SECONDS` | - | O (report-worker) | 선택(기본 1초) | 유휴 queue polling 간격 |
| `PREREVIEW_REPORT_RENDER_TIMEOUT_SECONDS` | - | O (report-worker) | 선택(기본 60초) | 작업별 Chromium render timeout |
| `PREREVIEW_REPORT_STORAGE_TIMEOUT_SECONDS` | - | O (report-worker) | 선택(기본 30초) | private Storage 요청 timeout |
| `PREREVIEW_REPORT_DATABASE_CONNECT_TIMEOUT_SECONDS` | - | O (report-worker) | 선택(기본 10초) | PostgreSQL 연결 timeout |
| `PREREVIEW_REPORT_CHROMIUM_EXECUTABLE` | - | O (report-worker) | 호스트 실행 시 필수 | Chromium/Chrome 실행 파일 절대경로. Compose는 `/usr/bin/chromium`을 주입 |
| `PREREVIEW_REPORT_WORKER_MEMORY_LIMIT` | Compose | - | 선택(기본 1 GiB) | Chromium 전용 컨테이너 memory limit |
| `PREREVIEW_GLOBAL_QUEUE_MAX` | O | - | 선택(기본 25) | 모든 API replica의 analysis upload + chat create/retry가 공유하는 PostgreSQL admission cap(1~10000) |
| `PREREVIEW_CURSOR_SIGNING_SECRET` | O | - | online pagination 시 필수 | analysis/conversation history의 signed opaque cursor. 비어 있으면 해당 pagination은 503 |
| `PREREVIEW_AUTH_ALLOWED_ORIGINS` | O | - | 브라우저 사용 시 필수 | 프론트의 정확한 origin 목록, 와일드카드 금지 |
| `SUPABASE_URL` | O | O | 필수 | Auth와 private Storage gateway |
| `SUPABASE_ANON_KEY` | O | - | 필수 | FastAPI가 Supabase Auth를 호출할 때 사용 |
| `SUPABASE_SECRET_KEY` 또는 `SUPABASE_SERVICE_ROLE_KEY` | O | O | 필수 | private Storage용 서버 비밀값 |
| `DATABASE_URL` | O | O | 필수 | FastAPI repository와 worker queue/result 저장 |
| `PREREVIEW_LLM_PROVIDER` | - | O | 선택(기본 `openai`) | `openai` 또는 experimental `vllm`; 선택한 provider 설정 오류는 claim 전 fail-closed |
| `PREREVIEW_EMBEDDING_PROVIDER` | - | analysis worker | 선택(기본 `openai`) | retrieval embedding provider. 현재 `openai`만 허용; LLM 전환과 독립 |
| `OPENAI_API_KEY` | - | analysis/chat worker | embedding 시 필수; OpenAI LLM 사용 시 필수 | analysis worker의 OpenAI embedding key이며, LLM provider가 `openai`이면 구조화·비교·채팅에도 사용 |
| `OPENAI_LLM_MODEL` | - | OpenAI LLM worker | `PREREVIEW_LLM_PROVIDER=openai` 시 필수 | 모든 OpenAI LLM 단계의 fallback 모델. 예제·로컬 준비 스크립트는 기본값을 채움 |
| `OPENAI_REQUEST_PROFILE_MODEL` | - | O | 권장 | Request Profile 구조화 모델. Compose 기본값은 `gpt-5.6-terra`; host 직접 실행에서 비우면 `OPENAI_LLM_MODEL` |
| `OPENAI_CPL_MODEL` | - | O | 선택 | CPL 모델. 비우면 `OPENAI_LLM_MODEL` |
| `OPENAI_FIT_MODEL` | - | O | 선택 | FIT 모델. 비우면 `OPENAI_LLM_MODEL` |
| `OPENAI_SIM_MODEL` | - | O | 선택 | SIM 모델. 비우면 `OPENAI_LLM_MODEL` |
| `OPENAI_CHAT_MODEL` | - | O (chat-worker) | 선택 | 결과 근거 채팅 모델. 비우면 `OPENAI_LLM_MODEL` |
| `OPENAI_EMBEDDING_MODEL` | - | analysis worker | 필수 | OpenAI embedding model; DB active embedding 설정과 일치해야 하며 vLLM LLM 선택과 무관 |
| `OPENAI_TIMEOUT_SECONDS` | - | O | 선택(기본 120초) | 각 OpenAI 호출의 hard timeout. 전체 analysis run 제한이 아님 |
| `OPENAI_MAX_REPAIRS` | - | O | 선택(기본 2회) | Request Profile·FIT·SIM 단계의 제한된 수정 호출 상한 |
| `VLLM_BASE_URL`, `VLLM_API_KEY`, `VLLM_LLM_MODEL` | - | 선택한 worker | `PREREVIEW_LLM_PROVIDER=vllm` 시 필수 | OpenAI-compatible vLLM endpoint, 토큰, 공통 fallback 모델. endpoint는 HTTPS만 허용하며 local `localhost`/127.0.0.0/8/::1만 HTTP 예외 |
| `VLLM_REQUEST_PROFILE_MODEL`, `VLLM_CPL_MODEL`, `VLLM_FIT_MODEL`, `VLLM_SIM_MODEL`, `VLLM_CHAT_MODEL` | - | 선택한 worker | 선택 | 해당 LLM 단계의 vLLM 모델 override. 비우면 `VLLM_LLM_MODEL` |
| `VLLM_TIMEOUT_SECONDS`, `VLLM_MAX_REPAIRS` | - | 선택한 worker | 선택(기본 120/2) | 재시도 없는 단일 vLLM 호출 전체 hard deadline 및 구조화 repair 상한(0~8) |
| `VLLM_MAX_OUTPUT_TOKENS`, `VLLM_MAX_RESPONSE_BYTES` | - | 선택한 worker | 선택(기본 16384/1048576) | `max_tokens` 생성 상한(1~32768). Request Profile은 selection만 LLM이 내고 final profile은 local materialize한다; streaming response body 상한은 1024~4194304 bytes |
| `PREREVIEW_EXISTING_KB_REQUIRED` | - | O | 선택(기본 true) | true면 KB/retrieval 부재를 fail-closed; false면 `KB_EMPTY` 완료 허용. request 0축/zero vector 허용 설정이 아님 |
| `PREREVIEW_REQUEST_NATIVE_EXACT_CANDIDATE_MODE` | - | analysis worker | 선택(기본 `off`) | Request source selection 후보 확장. `off`, `lines`, `lines+continuations`만 허용하며 오타는 작업 claim 전에 기동 실패 |
| `PREREVIEW_EXISTING_NATIVE_EXACT_CANDIDATE_MODE` | - | Existing producer | 선택(기본 `off`) | Existing 재구조화/import producer의 동일 후보 확장 seam. 현재 polling worker는 Existing producer를 호출하지 않음 |
| `PREREVIEW_FREETYPE_LIB` | - | O | 환경별 선택 | `rhwp` parser subprocess에만 주입 |
| `PREREVIEW_MODEL1_SERVING_HOST_DIR` | - | O (Compose) | Docker 분석 시 필수 | 검증된 외부 `model1` 디렉터리의 절대 host 경로. `/opt/prereview/model1`로 read-only mount |
| `PREREVIEW_MODEL1_RUNTIME_UID` / `GID` | - | O (Compose) | Docker 분석 시 필수 | mode 0700 Model 1 runtime의 숫자 owner. non-root 컨테이너 user와 일치해야 함 |
| `PREREVIEW_ML_ROOT` | - | O (host 직접 실행) | host ML 실행 시 필수 | 현재 checkout의 `ml/`; Docker에서는 이미지의 `/app/ml`로 고정 |
| `PREREVIEW_MODEL1_SERVING_DIR` | - | O (host 직접 실행) | host Model 1 실행 시 필수 | 검증된 model 1 serving 디렉터리; Docker에서는 `/opt/prereview/model1`로 고정 |
| `PREREVIEW_ML_PYTHON_EXECUTABLE` | - | O (host 직접 실행) | host ML 실행 시 필수 | 별도 child Python. Docker에서는 image-local `/opt/prereview-ml-venv/bin/python`으로 고정 |
| `PREREVIEW_ML_TIMEOUT_SECONDS` | - | O | 선택(기본 180초) | 각 ML child 호출의 hard timeout |
| `PREREVIEW_STRICT_ML_RUNTIME_PREFLIGHT` | - | O | Docker에서는 필수 | startup 전 Model 1/2/3 artifact·manifest SHA-256을 확인. Compose는 `true`로 고정 |

현재 단일 Compose 템플릿에는 두 LLM provider의 환경변수 슬롯이 함께 있다. 사용하지 않는
provider의 API key는 채우지 않는다. analysis worker에서 vLLM을 선택해도 retrieval
embedding용 `OPENAI_API_KEY`는 필요하지만, chat worker까지 provider별 최소권한 secret으로
완전히 분리하는 Compose profile은 아직 구현되지 않았다. vLLM external release가 NO-GO인
동안 이 항목도 production 전 후속 보안 gate로 유지한다.

호환 alias는 새 배포에서 가급적 사용하지 않는다. DB는 `DATABASE_URL`, anon key는
`SUPABASE_ANON_KEY`를 사용한다. Storage 비밀값은
`SUPABASE_SECRET_KEY`와 `SUPABASE_SERVICE_ROLE_KEY` 중 실제 배포가 제공하는 하나만
설정한다. 둘을 서로 다른 값으로 동시에 설정하면 API와 worker의 선택 우선순위가 달라질
수 있으므로 금지한다.

두 native exact mode는 원문을 요약하거나 재작성하지 않는다. `lines`는 하나의 원문
블록 안에 있는 줄을 정확한 offset과 함께 후보로 노출하고,
`lines+continuations`는 여기에 같은 구역의 인접 원문 블록 2~3개를 lossless span으로
결합한 후보를 추가한다. 활성화된 실행은 parent CandidatePack lineage를 Profile에 남기며,
재개 시 현재 환경변수가 아니라 저장된 lineage로 같은 variant를 재생성한다. 실제 rollout
전에는 Gold100 의미 회귀를 통과해야 하므로 기본값은 계속 `off`로 둔다.

`OPENAI_REQUEST_PROFILE_MODEL=gpt-5.6-terra`는 긴 원문에서 근거 anchor와 컴포넌트
경계를 선택하는 Request Profile 구조화 전용 설정이다. FIT·SIM·채팅용 Luna fallback을
구조화 단계에 암묵적으로 재사용하지 않도록 역할을 분리한다. `OPENAI_TIMEOUT_SECONDS=120`은
각 OpenAI 호출의 hard timeout이며 전체 analysis run 제한 시간이 아니다. 긴 구조화
응답에도 시간을 주되 실패가 무한히 걸리지 않도록 한 값이다.

`OPENAI_MAX_REPAIRS=2`는 Request Profile·FIT·SIM의 bounded repair 상한이다. Request
Profile에서는 최초 호출 뒤 수정 호출을 최대 두 번 허용하므로 최대 세 번 호출한다.
DB queue의 최대 attempt와는 별개다. 최신
acceptance의 analysis worker는 queue attempt 1회였지만 worker 로그에는 Terra 호출
최초 1회와 수정 2회가 관찰됐다. 성공 run 로그는 validation 상세를 노출하지 않았고,
동일 입력의 별도 진단에서 다음 두 서버 검증이 순서대로 확인됐다.

1. `fact f_scale_count (support_scale)`의 모호한 legacy `anchor_text`에
   `value_span_candidate_id`가 필요했다.
2. `stage_support`는 금액 또는 지급 회차만으로 독립 support component가 될 수 없고,
   컴포넌트 범위의 수혜자·자격·참여 조건 경계를 명시적으로 선택해야 했다.

각 수정 호출에는 이전 selection과 해당 서버 검증 오류를 함께 보내며, 근거나 span을
임의로 만드는 무제한 재생성이 아니다. 두 보정 뒤에도 검증을 통과하지 못하면 해당
Request Profile은 fail-closed로 실패한다.

### Model 1 artifact 준비 (`serving.zip`)

최신 `develop`에는 worker adapter와 `ml/pipelines/`, model 1 wrapper·tokenizer·label
mapping이 이미 있다. 기본 입력 위치인 `$HOME/serving.zip`에서 추가로 필요한 것은 Git에
없는 `model1/model/model.safetensors`다. ZIP 전체를 checkout의 `ml/`에 풀거나
압축 안의 Python 코드로 tracked 파일을 덮어쓰지 않는다. 현재 인수한 artifact의
고정 digest는 다음과 같다. 다른 서버에서는 archive를 안전한 로컬 경로로 별도 전달하되
아래 digest가 같은 바이트인지 확인한 뒤에만 명령의 archive 경로를 바꾼다.

| 대상 | SHA-256 |
|---|---|
| `$HOME/serving.zip` | `0fca416dfe6910f2fc00764c94d8418dc67dc42c79569feadd036e0cdc0ede41` |
| `model1/model/model.safetensors` | `8fa1522ced99f69966aed797c94cbd841f9ee9ce7d94c84dbc55adbf28613779` |
| Model 1 runtime manifest | `2903d0e90e71cd121af3185eeab3fefe3e1407175d14476e8d60f611b6861a60` |

다음 스크립트는 SHA-256이 고정된 archive에서 Model 1에 필요한 allowlist 파일만
Git에서 제외된 `.runtime/`에 새로 쓴다. archive와 weight digest, 그리고 DB에 등록된
Model 1 runtime manifest(`2903d0...`)까지 확인하며, 대상이 이미 있으면 실패한다.
따라서 tracked checkout 파일이나 기존 runtime을 덮어쓰지 않는다. 현재 checkout의
wrapper는 archive와 byte-identical하지 않으므로, 등록된 runtime을 재현할 때 archive의
검증된 wrapper를 사용해야 한다.

```bash
cd /absolute/path/to/SKN30-FINAL-4Team
python3 backend/scripts/prepare_model1_runtime.py \
  --destination .runtime/model1-serving/model1
```

기본 archive(`$HOME/serving.zip`) 외의 안전한 로컬 사본을 쓸 때만
`--archive /safe/path/serving.zip`을 추가한다. destination의 마지막 디렉터리 이름은
worker child 계약에 맞춰 반드시 `model1`이어야 한다. 다른 digest의 archive를
받아들이는 옵션은 없다.

ML 의존성은 backend 부모 process와 분리한 child interpreter에 설치한다. 이 경로도
`.runtime/`이므로 Git에 추가되지 않는다. `uv`의 CPU PyTorch backend를 명시하고,
base requirements에 `pyarrow`를 보완한 complete runtime file을 설치한다. base
`requirements.txt` 바이트는 이미 등록된 Model 1 manifest의 일부라 수정하지 않는다.

```bash
cd /absolute/path/to/SKN30-FINAL-4Team
uv venv .runtime/ml-venv
uv pip install --python .runtime/ml-venv/bin/python --torch-backend cpu \
  -r ml/serving/requirements.runtime.txt
```

실제 문서나 비밀값 없이 model 1 child 경계를 검증한다. 성공 시 stdout은
`support_type_pred`, `confidence`, `status`를 포함한 JSON object 하나여야 한다.

```bash
cd /absolute/path/to/SKN30-FINAL-4Team
printf '%s' '{"title":"2026년 중소기업 판로 지원","purpose":"판로 개척","content":"전시회 참가비 지원","target_text":"중소기업"}' | \
  env PREREVIEW_ML_ROOT="$PWD/ml" \
      PREREVIEW_MODEL1_SERVING_DIR="$PWD/.runtime/model1-serving/model1" \
      .runtime/ml-venv/bin/python backend/worker/adapters/ml_child.py --model model1
```

위 host venv는 **host 직접 실행과 Existing Model 1 one-shot backfill 전용**이다. Docker
Compose의 analysis worker에는 host venv나 checkout의 `ml/`을 mount하지 않는다.
`Dockerfile.ml-worker`가 Model 2/3의 코드·등록 artifact와 ML child dependency를
image-local `/opt/prereview-ml-venv`에 설치하며, Model 1만
`PREREVIEW_MODEL1_SERVING_HOST_DIR`에서 `/opt/prereview/model1`으로 read-only mount한다.
`api`와 `chat-worker`에는 ML dependency나 Model 1 mount가 없다.

Compose는 Model 1 host path와 UID/GID를 필수 interpolation으로 두고,
analysis worker를 그 numeric owner로 실행한다. `prepare_local_backend_env.py`가 이 세
값을 자동 기록한다. 단, 생성기는 기존 `backend/.env`를 절대 덮어쓰지 않는다. 이미
`backend/.env`가 있는 설치를 업그레이드할 때는 기존 파일을 mode 600 백업으로 옮긴 뒤
생성기를 실행하거나, 아래 세 값을 기존 파일에 직접 추가해야 한다.

```dotenv
PREREVIEW_MODEL1_SERVING_HOST_DIR=/absolute/path/to/.runtime/model1-serving/model1
PREREVIEW_MODEL1_RUNTIME_UID=<stat -c %u 로 확인한 숫자 UID>
PREREVIEW_MODEL1_RUNTIME_GID=<stat -c %g 로 확인한 숫자 GID>
```

root 소유 runtime이나 group/other 권한이 열린 runtime은 생성기가 거부한다. Model 1을
준비하고 `backend/.env`를 만든 작업은 `sudo`가 아닌 동일한 전용 Linux 사용자로 실행한다.
수동 `.env`로 생성기를 우회하더라도 strict worker는 effective UID나 GID가 0이면 기동을
거부한다.
컨테이너
startup은 Model 1 weight와 runtime manifest, image 안의 Model 2
bundle/cohort/taxonomy 및 Model 3 pool의 SHA-256을 모두 검증한다. 하나라도 다르면
queue를 polling하지 않고 종료한다.
이는 배포 누락을 `unavailable` 결과로 숨기지 않기 위한 fail-closed 정책이다.

ML child에는 DB·Supabase·OpenAI credential 환경변수를 넘기지 않고 Hugging Face/
Transformers network fallback도 강제로 끈다. worker root filesystem은 read-only이고
`/tmp`만 제한된 tmpfs다. 다만 Model 2의 `joblib`은 신뢰된 artifact 전제의 역직렬화
형식이며 child subprocess 자체는 완전한 filesystem sandbox가 아니다. SHA-256/manifest
검증은 artifact 바꿔치기를 탐지하는 무결성 경계일 뿐이므로, 운영에서는 전용 OS 계정,
최소 DB 권한, private Storage와 read-only mount를 함께 사용한다. 이 Docker ML 경로는
rootful Docker가 동작하는 Linux `amd64`와
`/usr/lib/x86_64-linux-gnu/libfreetype.so.6`를 기준으로 한다. host UID가 별도 user
namespace로 다시 매핑되는 rootless Docker는 현재 지원하지 않는다.

### Existing 100건 Model 1 분류 backfill

이 작업은 Request 분석을 실행하는 것이 아니라 현재 Existing Profile을 버전형
KB 분류 결과로 보강하는 one-shot 운영 작업이다. 먼저 migration 31·32·38을 포함한
전체 migration을 적용한다. `apply_migrations.sh`는 01~42를 순서대로 재적용하므로
기존 DB는 운영 가이드의 backup/staging 절차를 먼저 따른다.

```bash
cd /absolute/path/to/SKN30-FINAL-4Team/.runtime/supabase-dev
SUPABASE_DIR="$PWD" \
  /absolute/path/to/SKN30-FINAL-4Team/backend/supabase/apply_migrations.sh
```

migration 31·32·38이 보장하는 현재 runtime 설정의 UUID를 model ID, weight SHA-256, runtime
manifest SHA-256, input assembly 버전, producer 버전 **다섯 값 모두로** 조회한다. manifest는 sorted
compact JSON으로 고정한 다음 logical file→SHA-256 mapping이다: serving의
`inference.py`, label mapping, model config/weight, tokenizer 두 파일, checkout의
`pipelines/model1/dl07_m1_apply.py`, `serving/requirements.txt`와 backend의
`classify_existing_model1.py`, Existing input assembler, ML child/normalizer,
Model 1 contract/reference 파일이다. UUID를 임의로 만들거나 단순히 가장 최근 row를
선택하지 않는다.

```bash
docker compose exec -T db psql -U postgres -d postgres -P pager=off -c "
SELECT classification_config_pk, model_id, artifact_sha256,
       runtime_manifest_sha256, input_assembly_version, producer_version, is_active
FROM retrieval.classification_configuration
WHERE model_id = 'model_1_support_type'
  AND lower(artifact_sha256) =
      '8fa1522ced99f69966aed797c94cbd841f9ee9ce7d94c84dbc55adbf28613779'
  AND lower(runtime_manifest_sha256) =
      '2903d0e90e71cd121af3185eeab3fefe3e1407175d14476e8d60f611b6861a60'
  AND input_assembly_version = 'existing-profile-model1-input-v1'
  AND producer_version = 'pre-review-existing-model1-runtime-v3';
"
```

조회 결과는 정확히 1행이어야 하고 최초 backfill 전 `is_active`는 `false`다. 출력된
UUID만 비밀값이 아닌 임시 shell 변수에 넣는다.

```bash
export PREREVIEW_MODEL1_CONFIG_PK='<classification_config_pk UUID>'
```

Existing import·관계형 검증을 먼저 완료한 뒤, 모델이나 artifact를 열지 않는
읽기 전용 dry-run으로 current Profile 수와 입력 조립 가능 여부를 확인한다. 고정
팩은 `current_profiles: 100`, `predicted: 0`, `promoted: false`여야 한다.

```bash
cd /absolute/path/to/SKN30-FINAL-4Team
backend/.venv/bin/python backend/scripts/classify_existing_model1.py \
  --configuration-id "$PREREVIEW_MODEL1_CONFIG_PK" \
  --supabase-compose-env .runtime/supabase-dev/.env \
  --dry-run
```

실제 backfill은 위에서 검증한 serving directory와 ML child interpreter만 사용한다.
스크립트는 설정에 등록된 weight SHA-256과 실제 weight byte, 그리고 위 fixed runtime
file들의 manifest SHA-256을 모두 먼저 대조하고, 100건을 모두 성공·재검증한 뒤에만
설정을 활성화한다. `PREREVIEW_ML_ROOT`는 현재 checkout의 `ml/`이어야 하며
`PREREVIEW_MODEL1_SERVING_DIR`는 weight를 배치한 디렉터리여야 한다.

```bash
cd /absolute/path/to/SKN30-FINAL-4Team
PREREVIEW_ML_ROOT="$PWD/ml" \
PREREVIEW_MODEL1_SERVING_DIR="$PWD/.runtime/model1-serving/model1" \
PREREVIEW_ML_PYTHON_EXECUTABLE="$PWD/.runtime/ml-venv/bin/python" \
backend/.venv/bin/python backend/scripts/classify_existing_model1.py \
  --configuration-id "$PREREVIEW_MODEL1_CONFIG_PK" \
  --supabase-compose-env .runtime/supabase-dev/.env \
  --timeout-seconds 180
```

최초 완료 JSON은 `status: completed`, `current_profiles: 100`, `predicted: 100`,
`skipped: 0`, `promoted: true`를 예상한다. 같은 current Profile/input으로 재실행하면
이미 검증된 row를 재사용하므로 `predicted: 0`, `skipped: 100`, `promoted: true`가
정상이다. 중간 실패 시 불완전한 설정은 활성화되지 않으며, 원인을 해결한 뒤
같은 명령을 재실행한다.

다음 SQL을 위 Supabase directory의 `docker compose exec -T db psql -U postgres
-d postgres -P pager=off`에 넘겨 검증한다. 먼저 active config와 current corpus의
100/100 `OK` 완전성을 확인한다.

```sql
SELECT classification_config_pk, model_id, artifact_sha256,
       runtime_manifest_sha256, input_assembly_version, producer_version, is_active
FROM retrieval.classification_configuration
ORDER BY is_active DESC, created_at;

WITH current_profiles AS (
    SELECT profile.profile_version_pk
    FROM kb.profile_version AS profile
    JOIN kb.source_version AS source
      ON source.source_version_pk = profile.source_version_pk
    WHERE profile.is_current AND source.is_current
), active_config AS (
    SELECT classification_config_pk
    FROM retrieval.classification_configuration
    WHERE is_active
)
SELECT (SELECT count(*) FROM current_profiles) AS current_profiles,
       (SELECT count(*) FROM active_config) AS active_config_count,
       count(classification.profile_version_pk) AS active_classification_rows,
       count(classification.profile_version_pk) FILTER (
           WHERE classification.execution_status = 'OK'
       ) AS active_ok_rows
FROM active_config AS config
LEFT JOIN retrieval.existing_profile_classification AS classification
  ON classification.classification_config_pk = config.classification_config_pk
 AND classification.profile_version_pk IN (
       SELECT profile_version_pk FROM current_profiles
 );
```

고정 100건 정상 결과는 `current_profiles = 100`, `active_config_count = 1`,
`active_classification_rows = 100`, `active_ok_rows = 100`이다. active config가 없더라도
`current_profiles`는 실제 KB 건수를 유지하며 나머지 세 값이 0으로 보인다. 다음으로 raw
실행 상태와 예측 tier 분포를 확인한다.

```sql
SELECT classification.execution_status,
       classification.prediction_status,
       count(*) AS profile_count
FROM retrieval.existing_profile_classification AS classification
JOIN retrieval.classification_configuration AS config
  ON config.classification_config_pk = classification.classification_config_pk
JOIN kb.profile_version AS profile
  ON profile.profile_version_pk = classification.profile_version_pk
JOIN kb.source_version AS source
  ON source.source_version_pk = profile.source_version_pk
WHERE config.is_active AND profile.is_current AND source.is_current
GROUP BY classification.execution_status, classification.prediction_status
ORDER BY classification.execution_status, classification.prediction_status;
```

마지막으로 서비스 전용 projection의 provenance와 실제 라벨을 확인한다. raw
`support_type_pred`는 감사용으로 남지만 `판단보류`는 이 함수에서 `support_type =
NULL`, `effective_status = UNAVAILABLE`, `effective_reason_code = PREDICTION_WITHHELD`로
변환되어 Model 2·3/검색에 유효 라벨처럼 전달되지 않는다.

```sql
SELECT classification_config_pk, model_id, artifact_sha256,
       runtime_manifest_sha256, input_assembly_version, confidence,
       prediction_status, support_type, effective_status, effective_reason_code
FROM retrieval.get_active_existing_profile_classifications()
ORDER BY effective_status, support_type NULLS LAST, effective_reason_code;
```

Model 1 분류는 Existing current Profile/Fact만 읽으며 embedding을 조회하거나 OpenAI를
호출하지 않는다. 따라서 v2 embedding backfill **전에** 실행해도 되며 fresh
bootstrap에서는 그 순서를 권장한다. 분류 테이블·함수는 service-role 내부 경계이고
FastAPI response model/OpenAPI에 추가되지 않으므로 프론트엔드 API 계약은 바뀌지 않는다.
현재 FastAPI/worker에는 이 projection의 실제 소비자가 아직 없다. 다음 검색·Model 2·3
연결에서는 반드시 `get_active_existing_profile_classifications()`를 통해 읽고 raw
`existing_profile_classification.support_type_pred`를 조회하지 않는다. 지금은 같은
`service_role`이 적재 권한도 가져 DB 권한만으로 이 규칙을 강제하지 못하므로, 소비자 연결
시점에 read 전용 role을 분리하거나 raw table의 SELECT 권한을 축소하는 것을 배포 gate로 둔다.

### 주소 선택

| 실행 형태 | `SUPABASE_URL` 예 | `DATABASE_URL` host 예 |
|---|---|---|
| API·worker를 Docker로, Supabase는 같은 호스트 | `http://host.docker.internal:8000` | `host.docker.internal` |
| API·worker를 호스트 Python으로 직접 실행 | `http://127.0.0.1:8000` | `127.0.0.1` |
| 별도 서버의 내부망/Tailscale Supabase | `http://<내부-DNS-or-IP>:8000` | 내부 DNS 또는 Tailscale IP |

`DATABASE_URL`의 비밀번호에 `@`, `:`, `/`, `#` 같은 문자가 있으면 URL encoding한다.
Supabase Studio의 로그인 계정과 PostgreSQL 접속 계정은 서로 다른 자격증명이다.

### Cookie 설정

로컬/LAN의 HTTP PoC에서는 다음처럼 설정할 수 있다.

```dotenv
# 같은 PC에서만 쓸 때는 127.0.0.1을 유지한다. LAN 공개가 꼭 필요할 때만
# 0.0.0.0으로 바꾸고 OS 방화벽에서 허용 source 대역을 제한한다.
PREREVIEW_API_BIND_ADDRESS=127.0.0.1
PREREVIEW_AUTH_ALLOWED_ORIGINS=http://192.168.0.67:3000
PREREVIEW_AUTH_COOKIE_SECURE=false
PREREVIEW_AUTH_COOKIE_SAMESITE=lax
PREREVIEW_AUTH_COOKIE_DOMAIN=
```

HTTPS 서비스에서는 `PREREVIEW_AUTH_COOKIE_SECURE=true`를 사용한다. 프론트와 API가
서로 다른 site라면 `SameSite=none`과 HTTPS가 함께 필요하다. 어떤 경우에도
`PREREVIEW_AUTH_ALLOWED_ORIGINS`에는 스킴과 포트를 포함한 실제 프론트 origin을 정확히
기록하고 `*`를 사용하지 않는다. 프론트의 모든 인증·업로드·조회 요청에는
`credentials: "include"`가 필요하다. 분석 업로드는 브라우저에서 UUID
`Idempotency-Key`도 보내므로 reverse proxy와 CORS에서 이 헤더를 제거하면 안 된다.

## 4. 최초 빌드와 기동

Supabase가 먼저 정상 기동되고, 3절의 `prepare_model1_runtime.py`와
`prepare_local_backend_env.py`가 모두 성공한 뒤 실행한다. 생성기 이전에 Model 1 runtime을
준비하지 않았다면 Compose는 필요한 host path/UID/GID가 비어 있어 fail-closed한다.

```bash
cd /path/to/SKN30-FINAL-4Team/backend

docker compose config --quiet
docker compose up -d --build
docker compose ps
```

`docker compose config --quiet`은 문법과 필수 interpolation만 검사한다. `--quiet`을 빼면
치환된 비밀값이 터미널에 표시될 수 있으므로 결과를 공유하지 않는다. 처음에는 이미지가
ML CPU dependency를 내려받고 Model 2/3 artifact 검증까지 수행하므로 일반 API 이미지보다
빌드 시간이 길 수 있다. `report-worker` 이미지도 Chromium과 한글 font를 포함하므로
별도 디스크·메모리 여유를 확인한다. 기본 runtime memory limit은 1 GiB, `/tmp` tmpfs는
512 MiB다.

migration 41은 적용 전에 이미 완료된 분석을 PDF queue로 backfill하지 않는다. 배포 이후
새로 완료되거나 실제 재분석 완료로 case 상태가 갱신된 건만 trigger가 enqueue한다. 따라서
배포 직후 과거 완료 건을 일괄 렌더링하는 backlog는 생기지 않는다.

네 서비스 모두 `restart: unless-stopped`이므로 Docker daemon 재시작 뒤 다시 올라온다.
analysis worker, chat worker, report worker는 포트를 publish하지 않는다. API의 기본 호스트 포트는
`8001`이며 `.env`의 `PREREVIEW_API_PORT`로 바꿀 수 있다. 기본 bind 주소는 `127.0.0.1`이다.

## 5. 코드·설정 변경 후 재기동

코드나 dependency/Dockerfile이 바뀌었다면 이미지를 다시 만든다.

```bash
cd /path/to/SKN30-FINAL-4Team/backend
docker compose up -d --build
```

`.env`만 바뀌었다면 단순 `restart`로는 새 환경변수가 반영되지 않는다. 컨테이너를
재생성한다.

```bash
docker compose up -d --force-recreate api worker chat-worker report-worker
```

설정 변경 없이 프로세스만 재시작할 때 사용한다.

```bash
docker compose restart api worker chat-worker report-worker
```

worker가 실행 중인 작업에는 최대 120초 lease가 걸려 있다. 배포 전 새 업로드를 잠시
막고 실행 중 작업이 끝난 뒤 내리는 것이 가장 안전하다. 불가피하게 중단하면 lease 만료
후 다른 worker가 최대 시도 횟수 안에서 다시 점유한다. 강제 종료보다는 충분한 종료
시간을 준다.

```bash
docker compose stop -t 600 api worker chat-worker report-worker
```

`docker compose down -v`나 Supabase stack의 volume 삭제 명령은 사용하지 않는다.
backend 컨테이너를 내리는 것과 Supabase의 DB·Storage 영속 데이터를 삭제하는 것은
서로 다른 작업이다.

## 6. 기동 확인

```bash
docker compose ps
curl -fsS http://127.0.0.1:8001/health/live
curl -fsS http://127.0.0.1:8001/health/ready
docker compose logs --tail=100 api
docker compose logs --tail=100 worker
docker compose logs --tail=100 chat-worker
docker compose logs --tail=100 report-worker
```

- `/health/live`: FastAPI 프로세스가 요청에 응답하는지만 확인한다.
- `/health/ready`: online 환경변수와 repository 조립 여부를 확인한다.
- 현재 `/health/ready`는 실제 DB·Storage 연결이나 worker 생존까지 검사하지 않는다.
- `api`만 정상이고 `worker`가 없으면 업로드된 요청은 계속 `queued`에 남는다.
- `report-worker`가 없으면 새 분석 결과의 `report.status`는 `generating`에 남는다.

Supabase Studio의 SQL Editor에서는 비밀값 없이 다음 상태를 확인할 수 있다.

```sql
SELECT status, count(*)
FROM workspace.analysis_run
GROUP BY status
ORDER BY status;

SELECT processing_run_pk, run_type, status, started_at, finished_at, error_code
FROM ops.processing_run
ORDER BY created_at DESC
LIMIT 20;
```

최종 확인은 테스트 계정으로 로그인해 작은 HWP/HWPX 하나를 업로드한 뒤
`GET /api/v1/analysis-runs/{analysis_run_id}`를 polling하여 `succeeded`와
`analysis_case_id`가 반환되는지 보는 것이다. 이어 결과 조회의
`report.can_download=true`와 소유자 Cookie를 포함한
`GET /api/v1/analysis-cases/{analysis_case_id}/report.pdf`의 `200 application/pdf`를 확인한다.
OpenAI 비용이 발생하므로 배포마다 자동으로 실행하지 않는다.

### Operator용 HWP/HWPX live E2E

로컬 Supabase·migration 01~42·Existing KB/v2 embedding·`backend/.env`가 준비된 개발
환경에서는 다음 스크립트로 Auth → FastAPI 업로드 → Storage/DB queue → analysis
worker → 결과 조회 → cookie 인증·trusted Origin 채팅 POST → chat-worker → 메시지 GET
polling → report-worker 상태 polling → 소유자 PDF 다운로드를 한 번에 검증할 수 있다.
DB에 저장된 Model 1·2·3 status가 모두 `OK`인지도 직접 확인하므로, ML이
`UNAVAILABLE`인 채로 분석만 성공한 경우에는 E2E 성공으로 보지 않는다. 입력은
HWP·HWPX만 받으며 생성된 PDF는 메모리에서 응답 계약을 검증한 뒤 디스크에 저장하지 않고
폐기한다.

```bash
cd /path/to/SKN30-FINAL-4Team/backend
# inline: 이 process가 analysis/chat queue를 직접 claim한다.
# report-worker는 별도로 기동된 상태여야 한다.
uv run --extra dev python scripts/run_local_live_e2e.py --file /safe/local/request.hwp

# external: 실행 중인 Docker API와 worker/chat-worker/report-worker를 사용한다.
# Docker 배포 확인에는 이 모드를 사용한다. HTTP는 loopback API에서만 허용한다.
uv run --extra dev python scripts/run_local_live_e2e.py \
  --worker-mode external \
  --api-base-url http://127.0.0.1:8001 \
  --file /safe/local/request.hwpx

# 기본 위치와 다른 보안 설정 파일을 쓰는 경우만 명시한다.
uv run --extra dev python scripts/run_local_live_e2e.py \
  --worker-mode external \
  --api-base-url http://127.0.0.1:8001 \
  --file /safe/local/request.hwpx \
  --backend-env /safe/local/backend.env \
  --supabase-env /safe/local/supabase.env
```

`external`은 release gate다. 먼저 checkout이 clean한지 확인한 뒤 API와 세 worker 이미지를
다시 build한다. Dockerfile은 operator-supplied build arg를 받지 않고, pristine stage에서 `COPY .`
직후·pip install 전에 backend context 전체(빈 directory와 mode 포함)의 canonical
path·kind·mode·content SHA-256을 계산한다. final runtime image는 이 provenance artifact만 받는다.
`.dockerignore`의 credentials/local Python build output 및 generated identity 파일은 함께 제외되며
runtime environment는 이를 바꿀 수 없다.

```bash
git diff --quiet && git diff --cached --quiet
test -z "$(git ls-files --others --exclude-standard)"
docker compose up -d --build
```

E2E는 `/health/ready`의 `build_id`와 `X-PreReview-Build-Id`가 일치하는지, 그리고 그
값이 이 clean checkout에서 독립 계산한 backend Docker-context digest와 정확히 같은지를
확인한다. Git commit은 execution manifest에 별도로 기록한다. 형식만 유효한 이전 배포나
dirty checkout은 실패한다. 최종 코드/문서를 commit한 뒤 build/E2E 사이에는 tracked 또는
untracked 파일을 바꾸지 않고 trace는 repo 밖에 저장한다. `--api-base-url`은
`--worker-mode external`과 함께만 쓸 수 있으며, deployed API와 inline worker를 섞는 실행은
거부된다.

기본 `--backend-env`는 `backend/.env`, `--supabase-env`는 저장소의
`.runtime/supabase-dev/.env`다. 스크립트는 임의의 confirmed Auth user를 만들고
입력·Common IR·Request Profile·분석 결과와 결과 근거 기반 채팅 한 turn을 검증 후에도
보존한다. 생성한 계정의 email/password, 원문 byte, 질문/답변 본문, 전체 모델 출력은
터미널에 출력하지 않는다. 성공 JSON에는 ID, 상태, DB 감사 이력에서 확인한 실제
worker ID와 시도별 worker ID·attempt 수, 채팅 근거 참조 수, Model 1·2·3의 `OK`
status, PDF 상태·byte 크기처럼 안전한 요약값만 담긴다. PDF 본문과 Storage key,
요청/응답 header 전체는 출력하지 않는다. 다만 구조화·비교·채팅을 위해 테스트 파일에서 추출한
텍스트는 설정된 OpenAI API로 전송된다. 외부 전송이 허용된 합성/비식별 테스트 파일만
사용한다.

기본 `inline` 모드는 같은 process에서 history cursor를 발급하므로 server-only
`PREREVIEW_CURSOR_SIGNING_SECRET`이 shell 또는 `backend/.env`에 반드시 있어야 한다.
shell에 이미 설정된 값이 우선하며 값 자체는 출력하지 않는다. `external` 모드는 배포된
API가 보유한 signing secret을 사용하므로 E2E process가 그 값을 읽지 않는다.

`inline`은 E2E Python process가 자신이 만든 analysis/chat job만 DB queue에서 직접 claim해
host runtime으로 수행한다. PDF는 실제 Chromium/Storage 경계를 통과하도록 별도로
기동한 `report-worker`가 처리하며 두 모드 모두 같은 로그인 Cookie와 case로 상태와
다운로드를 검증한다. `external`은 job을 FastAPI에 업로드한 뒤 이미 기동된 Docker
`worker`, `chat-worker`, `report-worker`가 처리한 상태를 public API로 polling한 뒤,
해당 target의 `ops.processing_run` 감사 이력과 queue attempt 수를 대조한다. `external`에는 배포된
`--api-base-url`이 반드시 필요하므로 ASGI API와 외부 worker를 섞은 실행은 허용하지 않는다.
이 모드는 API·Cookie·Storage·DB queue의 실제 연결과 실행 worker ID를 검증한다. 다만
worker ID 자체에는 image digest가 없으므로, 아래처럼 다른 producer/worker가 없는 전용
검증 창에서 대상 Compose replica만 실행 중이라는 전제가 필요하다.

polling 기본 제한은 분석 1800초, 채팅 600초, PDF 600초다. 문서 크기·후보 수·provider
지연 때문에 더 긴 검증 창이 필요하면 `--analysis-poll-timeout-seconds`와
`--chat-poll-timeout-seconds`, `--report-poll-timeout-seconds`에 양의 초 단위 값을
명시한다. 이 값은 worker나 OpenAI
호출을 중단하지 않고 E2E가 public 상태를 기다리는 시간만 바꾼다.

live E2E는 `OPENAI_REQUEST_PROFILE_MODEL`을 shell 값, 이어서 `backend/.env`
값 순으로 읽고, 둘 다 없으면 `gpt-5.6-terra`를 사용한다. 현재 운영 기본은
`OPENAI_LLM_MODEL`도 `gpt-5.6-terra`이며, 필요한 경우 단계별 override로 분리할 수 있다.

스크립트는 시작 전에 analysis/chat/report queue가 모두 비어 있는지 확인한다. `inline`은 자신이
생성한 target만 claim하며 예상과 다른 run/message가 반환되면 claim transaction을
rollback하고 fail-closed한다. `external`은 queue를 직접 claim하지 않고 public 상태를
polling한 뒤 target의 영구 실행 이력에서 실제 worker ID, 순차 attempt 번호, 이전 실패와
최종 성공을 검증한다. analysis/chat은 현재 계약의 2회 상한, PDF는 migration
41의 3회 상한과 public `retry_count`를 함께 검증한다.

`inline`은 analysis/chat runtime을 직접 실행하므로 재현 검증 중에는 `worker`와
`chat-worker` 컨테이너를 일시 중지하되 `report-worker`는 기동하고, 다른 업로드/채팅을
막아야 한다. 사전검사 뒤
다른 producer/worker가 끼어드는 경쟁을 script가 원격에서 막을 수는 없으므로, 이 작업
차단은 필수다. `external`은 반대로 검증할 Compose의 세 worker만 실행한 채 사용하고,
다른 host worker·복제본·업로드 producer는 잠시 중지해야 한다. 시작 시 queue가 비었는지는
검사하지만 그 뒤의 경쟁을 원격에서 차단하지는 못하기 때문이다. 이 일시 중지는 Supabase
stack이나 영속 volume을 내리는 작업이 아니다.

성공 시 stdout JSON의 `execution_manifest`에는 입력 SHA-256, Git commit, worktree dirty
여부와 tracked diff/untracked content를 내용 노출 없이 식별하는 source-state SHA-256,
analysis/chat/report worker identity(외부 모드의 Docker image ID 포함), 각 실제 container에서
allow-list로 읽은 LLM/embedding 모델·repair 상한과 성공한 실행 시도에 고정된 exact
embedding configuration이 들어간다. 실행 종료 뒤 active 설정이 바뀌어도 E2E 기록은
바뀌지 않는다. `--trace-dir DIR`를 지정하면 다음 재현 자료도
보존한다: `00_upload.json`, `01_common_ir.json`, `02_structured_profile.json`,
`03_cpl.json`~`06_ml.json`, `07_result.json`, `08_run_state.json`,
`09_execution_manifest.json`, `cpl_diagnostics.json`. ML acceptance가 실패해도 이미
저장된 result/run/intermediate artifact를 best-effort trace로 남기며 원래 오류를
가리지 않는다. execution manifest에는 비밀값이나 원문을 넣지 않지만, trace의 Common
IR/Profile/result에는 원문에서 추출된 내용이 포함되므로 비공개 운영 자료로 취급하고
Git에 커밋하지 않는다.

### live E2E endpoint coverage

스크립트가 실제 HTTP로 호출하는 FastAPI 경계는 다음과 같다. `external` 모드에서는
표시된 모든 호출이 배포된 API로 나가고, 기본 `inline` 모드에서는 API 호출을 ASGI로
검증하며 worker만 현재 process에서 직접 실행한다.

| 경로 | 검증 내용 |
|---|---|
| `POST /api/v1/auth/sign-in` | 임의 E2E 사용자 로그인과 Cookie 수신 |
| `POST /api/v1/analysis-runs` | HWP/HWPX multipart 업로드, UUID idempotency, `202 queued` |
| `GET /api/v1/analysis-runs/{id}` | analysis terminal 상태 polling |
| `GET /api/v1/analysis-cases/{id}` | typed public result와 ML message projection |
| `GET /api/v1/analysis/current` | ready → close 뒤 idle snapshot |
| `GET /api/v1/analysis-history` | active 제외 및 close 뒤 historical 포함 |
| `GET /api/v1/sim-candidates/{id}` | 후보 detail, 네 축과 Request/Existing evidence linkage |
| `POST /api/v1/analysis-cases/{id}/messages` | trusted Origin 채팅 create, `202 generating` |
| `GET /api/v1/analysis-cases/{id}/messages/{assistant_id}` | assistant 단건 polling과 completed |
| `GET /api/v1/analysis-cases/{id}/messages` | completed chat history 재조회 |
| `GET /api/v1/analysis-cases/{id}/report/status` | generating polling, ready/download 조합, retry 상태와 private Cookie cache 경계 |
| `GET /api/v1/analysis-cases/{id}/report.pdf` | `200 application/pdf`, 실제 PDF parse·분석 대상명, 고정 파일명, 길이·private/no-store/nosniff |
| `POST /api/v1/analysis-sessions/{id}/close` | 소유 session close와 `204` |

`sign-up`, `refresh`, `sign-out`, `password-reset`, `update-password`, `auth/me`,
실패한 assistant `retry`, active-session compatibility `GET`은 이 live
스크립트의 coverage에 포함되지 않는다. 이 목록은 API 전체 지원 범위가 아니라 E2E 실행
manifest가 증명하는 endpoint coverage다.

최근 분석 경로 실측에서 실제 Hancom HWP는 Common IR 47 blocks(단락 39, 표 8),
relation 1, validation error 0으로 파싱됐고 결과 조회까지 성공했다.
2026-09-14 v0.2 Docker external 전체 경로 실측에서는 run
`08614411-f17d-42f0-93aa-ece5dff0447d`, case
`af973b50-a90e-4b52-ba7b-83a26f9c4b55`가 analysis/chat worker 각각 첫 attempt에
완료됐다. Model 1/2/3 `OK`, CPL 13, FIT 7, SIM 후보 5, evidence 107개,
completed chat과 evidence reference 10개를 확인했다. 후보 상세 근거 연결, active
history 제외, session close 뒤 history 편입과 chat 보존도 같은 실행에서 검증했다.
2026-09-13 합성 HWPX **host inline** 전체 경로 실측에서는 run
`5e51dae9-3c6e-4ed8-b4c6-96185917b08b`, case
`2d02ae97-85f0-4678-a9fe-e006ab389bd1`가 분석·채팅 worker 각각 첫 attempt에
완료됐다. Request Profile은 Terra, FIT·SIM·채팅은 Luna로 실행했고 Model 1·2·3의
DB status는 모두 `OK`였다. 결과는 CPL 13, FIT 7, SIM 후보 1, evidence 77,
검증된 채팅 evidence reference 13개였다. 입력 fixture는
`samples/hwpx/mockup_08_CPL전항목_스마트기술사업화.hwpx`, SHA-256은
`0054617fb553125e2b701d7ff9b37612048d95ab4d19ee3717a89bedd42e7ebb`다.
같은 날 Docker external E2E에서는 run
`f3e3c8c1-9988-4db2-8f6b-bdbed6472399`, case
`c05d9ae0-d279-4839-8a86-102da0be18fd`가 완료됐다. analysis worker
`4d6aae5d4c87:1:540b85e666be`와 chat worker `ea084ec9c993:1:c39815e56162`가 각각
한 번의 DB queue attempt로 처리했다. 실행 image는 analysis worker
`sha256:12adb17d…`, chat worker `sha256:f0669e6e…`였다. Model 1/2/3 `OK`, CPL 13,
FIT 7, SIM 후보 5, evidence 167개, completed chat과 evidence reference 13개를 확인했다.
입력은 위와 같은 `samples/hwpx/mockup_08_CPL전항목_스마트기술사업화.hwpx` 합성
fixture였다.
Request Profile에는 Terra를 사용했고 worker 로그에서 최초 1회와 수정 2회, 총 3회의
호출이 관찰됐다. 성공 run 자체의 안전한 로그는 개별 validation 사유를 남기지 않으므로,
위의 두 구체 검증 사유는 동일 입력의 별도 진단 결과이며 성공 run 로그에서 직접 읽은
값으로 간주하지 않는다. 제공된 `docs/pre_review_request_e2e_5_20260909_v1/generated`
합성 HWPX 5개도 같은 worker image parser에서 각각 Common IR 2 blocks, schema error 0건이었다.
이 5개는 금액이 미정이므로 ML 3축이 모두 필요한 acceptance 입력으로는 쓰지 않았다.
리뷰 뒤 strict worker의 effective UID/GID 0 거부만 추가한 image
`sha256:0847b036…`는 `1000:1000` 정상 기동·ML preflight와 강제 `0:0` 실행의 exit code
2를 확인했다. 이 후속 변경은 위 OpenAI provider/pipeline 경로를 바꾸지 않는다.
산출물 수·run/case ID·입력 SHA-256 같은 검증 기록은
[Backend 구현·검증 기록](../../IMPLEMENTATION_STATUS.md)에서 확인한다.

### Swagger/OpenAPI로 프론트 작업하기

API가 실행되면 다음 문서를 바로 사용할 수 있다.

```text
http://127.0.0.1:8001/docs       Swagger UI
http://127.0.0.1:8001/redoc      ReDoc
http://127.0.0.1:8001/openapi.json
```

Swagger 인증은 Bearer `Authorize` 방식이 아니다. 위에서 명시적으로 준비한 로컬 개발
계정으로 `POST /api/v1/auth/sign-in`을 먼저 실행하면 응답의 `Set-Cookie`를 브라우저가
HttpOnly Cookie로 저장하고 이후 요청에 자동으로 함께 보낸다. Swagger의
`Try it out`으로 상태 변경 API를 시험하려면 Swagger를 연 API origin도 정확한 허용
목록에 추가한다.

```dotenv
PREREVIEW_AUTH_ALLOWED_ORIGINS=https://app.example.com,https://api.example.com
```

Vite 개발 서버는 현재 3000번 포트다. Vite와 로컬 Swagger를 함께 쓸 때는 실제로 브라우저에
입력할 hostname까지 정확히 맞춘다. 예를 들어 Swagger를 `127.0.0.1`로 열면 다음과 같다.

```dotenv
PREREVIEW_AUTH_ALLOWED_ORIGINS=http://localhost:3000,http://127.0.0.1:8001
```

Swagger를 `localhost:8001`로 열 경우에는 두 번째 값을 그 origin으로 바꾼다. `localhost`와
`127.0.0.1`은 Cookie/Origin 관점에서 서로 다른 host이므로 섞어 추정하지 않는다.

프론트 코드는 `/openapi.json`으로 endpoint와 기본 request schema를 확인할 수 있다.
현재 결과·후보·세션·이력 endpoint와 CPL/FIT/SIM detail은 모두 named OpenAPI response
model의 strict typed DTO로 표시된다. raw/internal key는 응답에 추가하지 않는다. 필드 의미와
호출 흐름의 유일한 사람용 계약은
[프론트엔드 API 명세](0.FASTAPI_FRONTEND_API_SPEC.md)다.
`FASTAPI_RESPONSE_CONTRACT.json`은 mock용 비규범 예시로만 사용한다.

OpenAPI에는 `PreReviewAccessCookie`, `PreReviewRefreshCookie` Cookie security
scheme과 각 endpoint의 성공·주요 오류(`ErrorResponse`) schema가 표시된다.
`sign-in`, 비활성화된 `sign-up`, `password-reset`, `password-recovery/callback`,
`password-recovery/verify`는 기존
Cookie 없이 호출할 수 있다. 업무 API, `me`, `update-password`는 access Cookie,
`refresh`는 refresh Cookie를 요구한다. callback GET은 세션을 만들지 않는다. recovery
메일의 일회용 `token_hash`는 URL fragment로 프론트에 도착하며, trusted-Origin verify
POST의 JSON body에서 검증된 뒤에만 세션 Cookie가 설정된다.

이 security scheme은 HttpOnly Cookie라는 전달 방식을 문서화하기 위한 것이다. Swagger의
`Authorize`에 access/refresh token이나 쿠키 값을 직접 입력하지 않는다. 성공한 `sign-in`
응답의 `Set-Cookie`를 같은 browser origin이 저장·자동 전송하도록 시험한다. 먼저 `me`로
세션을 확인하고, `analysis-runs`에 파일과 새 UUID v4 `Idempotency-Key`를 보내고, 반환된
run ID를 `GET /analysis-runs/{analysis_run_id}`로 poll한다. frontend의 실제 HTTP client는
여전히 `credentials: "include"`를 설정해야 한다. `ErrorResponse.errors`는 검증 오류일 때만
나타나는 선택 필드이고, 비밀번호 같은 원 요청 비밀값은 포함하지 않는다.

인증 및 재설정 완료 흐름의 현재 지원 범위도 같은
[프론트엔드 API 명세](0.FASTAPI_FRONTEND_API_SPEC.md)를 따른다. self-hosted Supabase의
recovery 메일 템플릿은 `TokenHash`를 URL fragment로 FastAPI callback에 보내고,
callback은 세션을 만들지 않은 채 프론트 비밀번호 변경 화면으로 이동한다. 프론트가
fragment를 지운 뒤 trusted-Origin verify POST로 세션 Cookie를 교환한다. 과거
`AUTH_API_CONTRACT.md`는 이 명세로
안내하는 호환용 문서일 뿐이다.

현재 `frontend/src`에는 API base URL, Cookie 포함 HTTP client, polling 호출이 연결되어 있지
않다. Swagger/OpenAPI가 보인다는 사실만으로 화면 통합이 완료된 것은 아니며,
`credentials: "include"`, 401 refresh 정책, upload/poll/result 상태 처리를 프론트에 별도로
구현해야 한다.

인터넷에 API를 공개하는 환경에서는 reverse proxy에서 `/docs`, `/redoc`,
`/openapi.json`을 운영자 네트워크로 제한하거나 비활성화할지 결정한다. 이 경로에는
비밀값이 없지만 공개 API 구조를 불필요하게 노출할 수 있다.

## 7. 호스트 Python으로 개발 실행

Docker를 사용하지 않는 개발 실행에서는 API, analysis worker, chat worker, report worker를 서로 다른
터미널에서 실행한다.
`prepare_local_backend_env.py`가 기본으로 만드는 `.env`는 컨테이너용
`host.docker.internal` 주소를 사용하므로 호스트 프로세스에서 그대로 쓰지 않는다.
`.env.example`을 별도 `.env.host.local`로 복사해 `SUPABASE_URL`과 `DATABASE_URL` host를
`127.0.0.1`로 설정한다. Supabase pooler를 쓴다면 사용자명·비밀번호·DB 이름은 URL
encoding한다. FastAPI에는 `--env-file`을 주고 worker에는 같은 파일을 명시적으로
주입한다.

```bash
cd /path/to/SKN30-FINAL-4Team/backend
uv sync --frozen --extra dev
cp .env.example .env.host.local
chmod 600 .env.host.local
# .env.host.local의 server-only 값과 127.0.0.1 주소를 설정

# 터미널 1
uv run uvicorn main:app --host 127.0.0.1 --port 8001 --env-file .env.host.local

# 터미널 2: analysis queue
uv run python -m dotenv -f .env.host.local run -- python -m worker.main

# 터미널 3: conversation queue
uv run python -m dotenv -f .env.host.local run -- python -m worker.chat_main

# 터미널 4: PDF report queue (.env.host.local에 Chromium 절대경로 필요)
uv run python -m dotenv -f .env.host.local run -- python -m worker.report_main
```

네 프로세스 모두 프로젝트의 `backend` 디렉터리에서 실행한다. `.env.host.local`도 비밀
파일이므로 Git에 넣지 않는다. 이 이름은 기본 `.gitignore`의 `.env.*.local` 규칙으로
제외된다.

## 8. WSL과 EC2 공개 방식

현재 WSL PoC를 같은 Wi-Fi에서 사용할 경우 Windows portproxy는 FastAPI용 포트를
별도로 둔다. 이 경우에만 `backend/.env`의
`PREREVIEW_API_BIND_ADDRESS=0.0.0.0`과 `PREREVIEW_AUTH_COOKIE_SECURE=false`를 명시하고,
허용할 LAN 프론트 Origin을 정확히 추가한다.

```text
Windows 18001 → WSL 8001 (FastAPI)
Windows 18000 → WSL 8000 (Supabase Studio, 운영자만)
```

프론트의 API base URL은 `http://<Windows-LAN-IP>:18001/api/v1`이다. Windows 방화벽에는
필요한 source network에 대해서만 TCP 18001 inbound를 허용한다. 일반 사용자는 Studio,
PostgreSQL, worker 포트에 접근하지 않는다.

EC2에서는 Caddy/Nginx/ALB가 `https://api.example.com`을 API 컨테이너의 8000 또는
호스트의 8001로 전달하게 한다. security group에서는 80/443만 공개하고, Supabase
gateway·Studio·PostgreSQL은 Tailscale 또는 내부 network로 제한한다. HTTPS에서는
Cookie Secure를 반드시 활성화한다.

Nginx를 사용하는 경우 [nginx reverse-proxy example](../../deploy/nginx/prereview-api.conf)
을 출발점으로 삼는다. 예제의 `client_max_body_size 51m`은 multipart 전체 요청의
`PREREVIEW_HTTP_MAX_BODY_BYTES=53477376`과 일치하며, FastAPI의 파일 자체 상한
`PREREVIEW_UPLOAD_MAX_BYTES=52428800`도 별도로 적용된다. 실제 도메인·TLS 인증서·upstream
주소와 access log 정책은 배포 환경에 맞춰 바꾼다.

## 9. 자주 발생하는 문제

| 증상 | 우선 확인 |
|---|---|
| `/health/ready`가 503 | `PREREVIEW_OFFLINE_MODE=false`, 허용 Origin, Supabase URL/key, DB URL |
| 로그인·회원가입이 403 | 요청 `Origin`이 allow-list와 정확히 일치하는지 |
| 로그인 응답은 200인데 다음 요청이 401 | 프론트 `credentials: include`, Cookie Secure/SameSite, HTTP/HTTPS 불일치 |
| Auth가 503 | 컨테이너에서 `SUPABASE_URL` 접근 가능 여부와 anon key |
| 업로드가 503 | service-role/secret key, `request-temp`, DB 연결과 migration |
| 요청이 계속 `queued` | analysis worker 컨테이너·로그, DB URL, migration 21~26 및 33~40, queue claim·embedding provenance |
| 질문이 계속 `generating` | chat-worker 컨테이너·로그, DB URL, migration 27 및 33~39, v2 chat queue claim |
| 업로드가 `413` | Nginx `client_max_body_size`/`PREREVIEW_HTTP_MAX_BODY_BYTES`(전체 multipart)와 `PREREVIEW_UPLOAD_MAX_BYTES`(파일 자체)를 각각 확인 |
| 새 업로드/질문이 `503` | 모든 API replica의 `PREREVIEW_GLOBAL_QUEUE_MAX`가 같은지와 active analysis/chat backlog를 확인 |
| API가 과도하게 동시 처리됨 | Compose의 Uvicorn `--limit-concurrency`와 `PREREVIEW_API_LIMIT_CONCURRENCY` 확인 |
| analysis/conversation history가 503 | `PREREVIEW_CURSOR_SIGNING_SECRET`이 API에 비어 있지 않은지, 복수 API 인스턴스가 같은 값을 쓰는지 확인 |
| worker가 바로 종료 | 필수 환경변수 이름 누락; worker는 설정 오류 시 exit code 2 |
| Docker analysis worker가 시작 직후 종료 | `prepare_model1_runtime.py` 실행 여부, `backend/.env`의 Model 1 host path/UID/GID, mount 권한과 startup SHA-256/manifest 오류를 확인. 누락·불일치는 의도된 fail-closed 동작 |
| HWP/HWPX parser가 `FT_Palette_Data_Get` 오류 | 이미지 재빌드와 `PREREVIEW_FREETYPE_LIB` 경로 |
| OpenAI HTTP 200 후 `LLM_INVALID_RESPONSE` | HTTP 성공과 domain 구조 검증 성공은 다름. finish/refusal, JSON root, cross-field validation 단계를 확인하되 raw 응답·원문은 로그에 남기지 않음 |
| `relation_container` cross-field validation 반복 | OpenAI SDK 2.54.0 고정 및 원격 응답 kind별 정규화가 포함된 최신 worker 이미지인지 확인. 필수 container 근거가 없으면 정규화로 값을 만들지 않고 실패하는 것이 정상 |
| 후보가 없거나 embedding 오류 | Existing embedding 적재 여부, active model ID·1,536차원 일치 |
| `.env` 수정 후 값이 그대로임 | `restart` 대신 `up -d --force-recreate` 사용 |

환경을 확인한다고 `docker compose exec api env`, `docker compose exec worker env`, 평문
`docker compose config` 출력을 공유하면 비밀값이 노출될 수 있다. 변수 값 대신
설정 여부와 서비스 연결 성공 여부만 확인한다.

## 10. 현재 서비스 전 제한

다음은 기본 분석 E2E와 별개의 미완료 운영 항목이다.

- multipart part 수 제한과 streaming upload
- `request-temp` 및 만료 결과의 reference-aware cleanup
- worker heartbeat/queue lag를 포함한 readiness
- 실제 Hancom 작성 HWPX 및 malformed/timeout 문서 검증
- PDF 입력·OCR API와 PDF 보고서 재생성 API

구현·검증 기록과 남은 범위는 [IMPLEMENTATION_STATUS.md](../../IMPLEMENTATION_STATUS.md)를
확인한다.
