# FastAPI·worker 배포 및 운영 가이드

마지막 검증: 2026-09-10

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

`backend/compose.yaml`은 `api`와 `worker`만 실행한다. Supabase는 별도 Compose stack으로
먼저 실행되어 있어야 한다. Redis/RQ, Edge Function dispatch/callback, 외부 worker HTTP
서버는 현재 경로에서 사용하지 않는다.

## 2. 기동 전 확인

다음 조건이 먼저 충족되어야 한다.

- self-hosted Supabase Auth·PostgreSQL/pgvector·Storage가 실행 중이다.
- `backend/supabase/migrations/01`부터 `25`까지 적용되어 있다.
- private bucket `existing-kb`, `request-temp`, `analysis-reports`가 생성되어 있다.
- Existing Profile과 `retrieval.existing_profile_embedding` 데이터가 준비되어 있다.
- FastAPI·worker 컨테이너에서 Supabase gateway와 PostgreSQL에 접근할 수 있다.
- 서버에서 OpenAI API에 HTTPS로 접근할 수 있다.

새 DB에 Existing Profile과 embedding을 준비하는 절차는
[Existing KB bootstrap 가이드](../../supabase/EXISTING_KB_BOOTSTRAP.md)를 따른다.

### 새 로컬 환경의 준비 순서

처음부터 재현할 때는 다음 순서를 지킨다.

1. self-hosted Supabase를 기동하고 Auth·DB·Storage가 healthy인지 확인한다.
2. migration 01~25를 적용한다.
3. 아래 host-side 스크립트로 로컬 개발용 Auth 사용자를 **명시적으로** 한 번 준비한다.
4. 실제 전체 분석이 필요하면 Existing KB와 embedding을 bootstrap한다. 로그인·`/me`만
   확인할 때는 이 단계가 필요하지 않다.
5. `backend/.env`를 준비한다.
6. FastAPI `api`와 polling `worker`를 기동한다.
7. Swagger에서 `sign-in` → `me` → HWP/HWPX upload → 상태 poll 순서로 확인한다.

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
`PREREVIEW_DEV_AUTH_ROLE`은 현재 `user`만 허용한다. password는 12자 이상이면서 UTF-8
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

# 입력 비밀 파일도 현재 Linux 사용자만 읽을 수 있게 한다.
chmod 600 .env .runtime/supabase-dev/.env

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
PREREVIEW_UPLOAD_MAX_BYTES=52428800

# 브라우저 인증/CORS/CSRF
PREREVIEW_AUTH_ALLOWED_ORIGINS=https://app.example.com
PREREVIEW_AUTH_COOKIE_SECURE=true
PREREVIEW_AUTH_COOKIE_SAMESITE=lax
PREREVIEW_AUTH_COOKIE_DOMAIN=
PREREVIEW_AUTH_REFRESH_COOKIE_MAX_AGE=2592000
PREREVIEW_AUTH_PASSWORD_RESET_REDIRECT_TO=https://app.example.com/reset-password

# Supabase Auth·private Storage
SUPABASE_URL=http://host.docker.internal:8000
SUPABASE_ANON_KEY=<anon-key>
SUPABASE_SECRET_KEY=<service-role-or-secret-key>
SUPABASE_SERVICE_ROLE_KEY=

# trusted PostgreSQL 연결
DATABASE_URL=postgresql://<user>:<url-encoded-password>@host.docker.internal:<db-port>/postgres
SUPABASE_DB_URL=

# worker 전용 OpenAI
OPENAI_API_KEY=<openai-api-key>
OPENAI_LLM_MODEL=gpt-5.6-luna
OPENAI_EMBEDDING_MODEL=text-embedding-3-small
OPENAI_TIMEOUT_SECONDS=60
OPENAI_MAX_REPAIRS=1

# PostgreSQL polling worker
PREREVIEW_WORKER_HEARTBEAT_SECONDS=30
PREREVIEW_WORKER_LEASE_SECONDS=120
PREREVIEW_WORKER_IDLE_POLL_SECONDS=1
PREREVIEW_WORKER_TOP_K=5
PREREVIEW_WORKER_STORAGE_TIMEOUT_SECONDS=30
PREREVIEW_WORKER_DATABASE_CONNECT_TIMEOUT_SECONDS=10
PREREVIEW_WORKER_PARSE_TIMEOUT_SECONDS=120
PREREVIEW_FREETYPE_LIB=/usr/lib/x86_64-linux-gnu/libfreetype.so.6
```

### 필수 변수와 사용 주체

| 변수 | API | worker | 설명 |
|---|:---:|:---:|---|
| `PREREVIEW_OFFLINE_MODE=false` | O | - | 실제 Supabase repository를 활성화 |
| `PREREVIEW_AUTH_ALLOWED_ORIGINS` | O | - | 프론트의 정확한 origin 목록, 와일드카드 금지 |
| `SUPABASE_URL` | O | O | Auth와 private Storage gateway |
| `SUPABASE_ANON_KEY` | O | - | FastAPI가 Supabase Auth를 호출할 때 사용 |
| `SUPABASE_SECRET_KEY` 또는 `SUPABASE_SERVICE_ROLE_KEY` | O | O | private Storage용 서버 비밀값 |
| `DATABASE_URL` | O | O | FastAPI repository와 worker queue/result 저장 |
| `OPENAI_API_KEY` | - | O | 구조화·embedding·비교 호출 |
| `OPENAI_LLM_MODEL` | - | O | Request Profile·FIT·SIM 모델 |
| `OPENAI_EMBEDDING_MODEL` | - | O | DB active embedding 설정과 일치해야 함 |
| `PREREVIEW_FREETYPE_LIB` | - | O | `rhwp` parser subprocess에만 주입 |

호환 alias는 새 배포에서 가급적 사용하지 않는다. DB는 `DATABASE_URL`, anon key는
`SUPABASE_ANON_KEY`를 사용한다. Storage 비밀값은
`SUPABASE_SECRET_KEY`와 `SUPABASE_SERVICE_ROLE_KEY` 중 실제 배포가 제공하는 하나만
설정한다. 둘을 서로 다른 값으로 동시에 설정하면 API와 worker의 선택 우선순위가 달라질
수 있으므로 금지한다.

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

Supabase가 먼저 정상 기동된 것을 확인한 뒤 실행한다.

```bash
cd /path/to/SKN30-FINAL-4Team/backend

docker compose config --quiet
docker compose up -d --build
docker compose ps
```

`docker compose config --quiet`은 문법만 검사한다. `--quiet`을 빼면 치환된 비밀값이
터미널에 표시될 수 있으므로 결과를 공유하지 않는다.

두 서비스 모두 `restart: unless-stopped`이므로 Docker daemon 재시작 뒤 다시 올라온다.
worker는 포트를 publish하지 않는다. API의 기본 호스트 포트는 `8001`이며 `.env`의
`PREREVIEW_API_PORT`로 바꿀 수 있다. 기본 bind 주소는 `127.0.0.1`이다.

## 5. 코드·설정 변경 후 재기동

코드나 dependency/Dockerfile이 바뀌었다면 이미지를 다시 만든다.

```bash
cd /path/to/SKN30-FINAL-4Team/backend
docker compose up -d --build
```

`.env`만 바뀌었다면 단순 `restart`로는 새 환경변수가 반영되지 않는다. 컨테이너를
재생성한다.

```bash
docker compose up -d --force-recreate api worker
```

설정 변경 없이 프로세스만 재시작할 때 사용한다.

```bash
docker compose restart api worker
```

worker가 실행 중인 작업에는 최대 120초 lease가 걸려 있다. 배포 전 새 업로드를 잠시
막고 실행 중 작업이 끝난 뒤 내리는 것이 가장 안전하다. 불가피하게 중단하면 lease 만료
후 다른 worker가 최대 시도 횟수 안에서 다시 점유한다. 강제 종료보다는 충분한 종료
시간을 준다.

```bash
docker compose stop -t 600 api worker
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
```

- `/health/live`: FastAPI 프로세스가 요청에 응답하는지만 확인한다.
- `/health/ready`: online 환경변수와 repository 조립 여부를 확인한다.
- 현재 `/health/ready`는 실제 DB·Storage 연결이나 worker 생존까지 검사하지 않는다.
- `api`만 정상이고 `worker`가 없으면 업로드된 요청은 계속 `queued`에 남는다.

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
`analysis_case_id`가 반환되는지 보는 것이다. OpenAI 비용이 발생하므로 배포마다 자동으로
실행하지 않는다.

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
현재 결과·후보·세션·이력 endpoint의 고정 필드는 named OpenAPI response model로 표시된다.
다만 CPL/FIT의 `detail`과 SIM 후보 `axes.*`는 판정 계약에 따라 확장되는 JSON object다.
그 세부 의미와 응답 예시는
[FASTAPI_RESPONSE_CONTRACT.json](FASTAPI_RESPONSE_CONTRACT.json)도 함께 기준으로 삼는다.

OpenAPI에는 `PreReviewAccessCookie`, `PreReviewRefreshCookie` Cookie security scheme과 각
endpoint의 성공·주요 오류(`ErrorResponse`) schema가 표시된다. `sign-in`, `sign-up`,
`password-reset`은 Cookie가 없어도 호출할 수 있으므로 인증 요구가 표시되지 않는다. 반면
업무 API, `me`, `update-password`는 access Cookie, `refresh`는 refresh Cookie를 요구한다.

이 security scheme은 HttpOnly Cookie라는 전달 방식을 문서화하기 위한 것이다. Swagger의
`Authorize`에 access/refresh token이나 쿠키 값을 직접 입력하지 않는다. 성공한 `sign-in`
응답의 `Set-Cookie`를 같은 browser origin이 저장·자동 전송하도록 시험한다. 먼저 `me`로
세션을 확인하고, `analysis-runs`에 파일과 새 UUID v4 `Idempotency-Key`를 보내고, 반환된
run ID를 `GET /analysis-runs/{analysis_run_id}`로 poll한다. frontend의 실제 HTTP client는
여전히 `credentials: "include"`를 설정해야 한다. `ErrorResponse.errors`는 검증 오류일 때만
나타나는 선택 필드이고, 비밀번호 같은 원 요청 비밀값은 포함하지 않는다.

인증 및 재설정 완료 흐름의 현재 지원 범위는
[AUTH_API_CONTRACT.md](../../AUTH_API_CONTRACT.md)도 함께 확인한다. 특히 password-reset은
메일 발송만 제공하고, token/PKCE callback으로 비로그인 비밀번호를 변경하는 endpoint는 아직
없다.

현재 `frontend/src`에는 API base URL, Cookie 포함 HTTP client, polling 호출이 연결되어 있지
않다. Swagger/OpenAPI가 보인다는 사실만으로 화면 통합이 완료된 것은 아니며,
`credentials: "include"`, 401 refresh 정책, upload/poll/result 상태 처리를 프론트에 별도로
구현해야 한다.

인터넷에 API를 공개하는 환경에서는 reverse proxy에서 `/docs`, `/redoc`,
`/openapi.json`을 운영자 네트워크로 제한하거나 비활성화할지 결정한다. 이 경로에는
비밀값이 없지만 공개 API 구조를 불필요하게 노출할 수 있다.

## 7. 호스트 Python으로 개발 실행

Docker를 사용하지 않는 개발 실행에서는 API와 worker를 서로 다른 터미널에서 실행한다.
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

# 터미널 2
uv run python -m dotenv -f .env.host.local run -- python -m worker.main
```

두 프로세스 모두 프로젝트의 `backend` 디렉터리에서 실행한다. `.env.host.local`도 비밀
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

## 9. 자주 발생하는 문제

| 증상 | 우선 확인 |
|---|---|
| `/health/ready`가 503 | `PREREVIEW_OFFLINE_MODE=false`, 허용 Origin, Supabase URL/key, DB URL |
| 로그인·회원가입이 403 | 요청 `Origin`이 allow-list와 정확히 일치하는지 |
| 로그인 응답은 200인데 다음 요청이 401 | 프론트 `credentials: include`, Cookie Secure/SameSite, HTTP/HTTPS 불일치 |
| Auth가 503 | 컨테이너에서 `SUPABASE_URL` 접근 가능 여부와 anon key |
| 업로드가 503 | service-role/secret key, `request-temp`, DB 연결과 migration |
| 요청이 계속 `queued` | worker 컨테이너·로그, DB URL, migration 21~25, queue claim |
| worker가 바로 종료 | 필수 환경변수 이름 누락; worker는 설정 오류 시 exit code 2 |
| HWP/HWPX parser가 `FT_Palette_Data_Get` 오류 | 이미지 재빌드와 `PREREVIEW_FREETYPE_LIB` 경로 |
| 후보가 없거나 embedding 오류 | Existing embedding 적재 여부, active model ID·1,536차원 일치 |
| `.env` 수정 후 값이 그대로임 | `restart` 대신 `up -d --force-recreate` 사용 |

환경을 확인한다고 `docker compose exec api env`, `docker compose exec worker env`, 평문
`docker compose config` 출력을 공유하면 비밀값이 노출될 수 있다. 변수 값 대신
설정 여부와 서비스 연결 성공 여부만 확인한다.

## 10. 현재 서비스 전 제한

다음은 기본 분석 E2E와 별개의 미완료 운영 항목이다.

- password recovery callback/PKCE 완결
- reverse proxy/ASGI 전체 multipart body·part 수 제한과 streaming upload
- `request-temp` 및 만료 결과의 reference-aware cleanup
- worker heartbeat/queue lag를 포함한 readiness
- 실제 Hancom 작성 HWP/HWPX 및 malformed/timeout 문서 검증
- 채팅·PDF 보고서·PDF OCR API

최신 완료·미완료 범위는 [IMPLEMENTATION_STATUS.md](../../IMPLEMENTATION_STATUS.md)를
확인한다.
