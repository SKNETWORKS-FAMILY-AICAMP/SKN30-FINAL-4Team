# PreReview backend

운영자가 바로 실행할 수 있는 절차는 다음 문서에서 시작한다.

- [현재 EC2 기동·중지 매뉴얼](../docs/operations/EC2_SERVER_OPERATIONS.md)
- [새 환경 DB 구성·복구 매뉴얼](supabase/DATABASE_SETUP_GUIDE.md)

현재 런타임의 공개 경계는 FastAPI다. 브라우저는 FastAPI만 호출하며, Supabase의
Auth·PostgreSQL/pgvector·private Storage는 서버 내부 인프라로 사용한다.

이 README의 endpoint 목록은 구성 개요일 뿐 프론트 계약 원본이 아니다. 프론트 구현은
[프론트엔드 FastAPI API 명세서](fastapi/docs/0.FASTAPI_FRONTEND_API_SPEC.md)를 유일한
사람용 계약으로 사용하고, exact route·schema·enum·nullability는 배포 API의 `/docs` 또는
`/openapi.json`에서 확인한다.

```text
Frontend (HttpOnly Cookie)
  → FastAPI /api/v1
      ├─ Supabase Auth
      ├─ Postgres / pgvector / private Storage
      ├─ workspace.analysis_run (queued) → analysis worker → CPL/FIT/SIM/ML
      ├─ conversation message (generating) → chat worker → grounded answer
      └─ report artifact (generating) → report worker → private PDF
```

- Redis/RQ, 브라우저의 Supabase 직접 호출, 현재 런타임의 Edge Function dispatch/callback은 사용하지 않는다.
- 분석 화면의 첫 진입은 `GET /api/v1/analysis/current`을 폴링하고, 업로드 응답의 run ID가 있으면
  `GET /api/v1/analysis-runs/{id}`도 사용할 수 있다. SSE/WebSocket은 현재 구현 범위가 아니다.
- worker는 신뢰된 서버 프로세스이며 PostgreSQL과 private Storage에만 내부 자격증명으로 접근한다. 브라우저 Cookie·사용자 token은 worker에 전달하지 않는다.

## 현재 API 개요

- `POST /api/v1/auth/sign-in`, `refresh`, `sign-out`, `password-reset`, `password-recovery/verify`, `update-password`
- `POST /api/v1/auth/sign-up` — 기본 비활성화; GoTrue의 `DISABLE_SIGNUP=true`가 주 방어선
- `GET /api/v1/auth/password-recovery/callback` — 세션을 만들지 않고 fragment를 보존해 프론트로 redirect
- `GET /api/v1/auth/me`
- `POST /api/v1/analysis-runs` — 필수 UUID v4 `Idempotency-Key`, HWP/HWPX 업로드와 queued run 생성
- `GET /api/v1/analysis-runs/{analysis_run_id}` — 작업 상태 폴링
- `GET /api/v1/analysis/current` — 현재 분석의 `processing`/`ready`/`idle` 공개 상태
- `GET /api/v1/analysis-cases/{analysis_case_id}`
- `GET /api/v1/analysis-cases/{analysis_case_id}/report/status` — PDF 생성 상태 폴링
- `GET /api/v1/analysis-cases/{analysis_case_id}/report.pdf` — 준비된 private PDF 보고서 다운로드
- `GET /api/v1/sim-candidates/{sim_candidate_id}`
- `GET /api/v1/analysis-sessions/active`
- `POST /api/v1/analysis-sessions/{analysis_session_id}/close` — 해당 소유자의 특정 session만 idempotent하게 종료
- `GET /api/v1/analysis-history` — page size 5의 signed snapshot cursor 이력
- `POST /api/v1/analysis-cases/{analysis_case_id}/messages` — 필수 UUID v4 `Idempotency-Key`로 assistant turn 생성
- `GET /api/v1/analysis-cases/{analysis_case_id}/messages/{message_id}` — 생성 중/완료 assistant turn polling
- `GET /api/v1/analysis-cases/{analysis_case_id}/messages` — signed cursor 대화 이력
- `POST /api/v1/analysis-cases/{analysis_case_id}/messages/{assistant_message_id}/retry` — 필수 UUID v4 `Idempotency-Key` 재시도

PDF 생성은 별도 `report-worker`가 분석 완료 건을 Chromium으로 렌더링해 private Storage에 저장한다.
보고서 조회 API는 소유권·보존기간·hash를 검증한 뒤에만 반환한다. 메시지 POST/retry는 `202 Accepted`이며 별도 `chat-worker`가 저장된 분석 결과만 근거로 답한다.
PDF migration 적용 전에 이미 완료된 분석은 자동 생성 대상이 아니며, 적용 이후 새로
완료되거나 실제로 재분석되어 완료 상태가 갱신된 case만 PDF queue에 등록된다.
이력 응답의 `next_cursor`는 opaque 값이므로 수정하지 않고 그대로 다음 요청에 전달한다. cursor
서명 비밀값을 교체하면 이미 발급한 cursor는 의도적으로 무효가 된다.

## 로컬 실행

```bash
uv sync --frozen --extra dev
uv run uvicorn main:app --reload --host 127.0.0.1 --port 8001
```

환경 파일 없이 실행하면 기본은 online fail-closed 모드다. 이 실행은 OpenAPI와 정적 계약
확인용이며 실제 Supabase/worker 분석을 수행하지 않는다. online Compose 모드에는
`.env.example`의 Supabase·PostgreSQL 설정과 허용할 프론트 origin이 필요하다. 실제
비밀값은 커밋하지 않는다.

online host 개발은 같은 `.env.host.local`을 네 프로세스에 주입해 API, analysis worker,
chat worker, report worker를 각각 실행한다. chat worker가 없으면 질문은 `generating`에 남고, analysis
worker가 없으면 업로드 run은 `queued`에 남는다.

```bash
# backend/에서, .env.host.local에 server-only 값과 PREREVIEW_CURSOR_SIGNING_SECRET를 설정
uv run uvicorn main:app --host 127.0.0.1 --port 8001 --env-file .env.host.local
uv run python -m dotenv -f .env.host.local run -- python -m worker.main
uv run python -m dotenv -f .env.host.local run -- python -m worker.chat_main
uv run python -m dotenv -f .env.host.local run -- python -m worker.report_main
```

```bash
# 먼저 serving.zip에서 검증된 Model 1 runtime을 준비한다. Docker analysis worker는
# 이 디렉터리만 read-only로 mount하며, host의 Python venv는 mount하지 않는다.
python3 scripts/prepare_model1_runtime.py \
  --destination ../.runtime/model1-serving/model1

# 저장소 루트 .env와 .runtime/supabase-dev/.env가 이미 준비된 로컬 환경.
# 생성기는 위 Model 1 디렉터리의 절대경로와 숫자 UID/GID를 backend/.env에 기록한다.
chmod 600 ../.env ../.runtime/supabase-dev/.env
uv run python scripts/prepare_local_backend_env.py

# 또는 .env.example을 복사하고 운영값을 직접 설정
# cp .env.example .env && chmod 600 .env
```

환경 생성기는 비밀값을 출력하지 않고 mode `600`인 새 `.env`만 만들며 기존 파일은
절대 덮어쓰지 않는다. Docker 컨테이너에서 같은 호스트의 Supabase gateway(8000)와
PostgreSQL pooler(5432)를 사용하도록 주소와 URL encoding까지 처리한다. LAN 프론트
origin 추가 옵션을 포함한 상세 절차는 아래 운영 가이드를 따른다. 이 자동 생성 파일은
Docker Compose용이다. 호스트 Python으로 직접 실행할 때 필요한 `127.0.0.1` 주소 설정은
운영 가이드의 별도 절차를 따른다. online에서 analysis/conversation history를 사용하려면
생성 뒤에도 `PREREVIEW_CURSOR_SIGNING_SECRET`에 충분히 긴 server-only 임의값을 설정한다.

API와 same-server worker를 Docker Compose로 함께 기동할 때는 분석 `worker`, `chat-worker`, `report-worker`가 기본 service로 포함된다. worker 이미지에 `8000/tcp`가 표시될 수 있지만
호스트 포트로 publish하지 않는다.

현재 Compose는 역할을 분리한다. `api`와 `chat-worker`는 가벼운 기본 이미지로 실행하고, `report-worker`는 Chromium 전용 이미지로 실행한다.

analysis `worker`만 `Dockerfile.ml-worker`의 CPU 전용 이미지로 실행한다. Model 2/3 코드와
고정 artifact, ML child Python venv는 그 이미지 안에만 들어간다. Model 1은 Git과 이미지에
넣지 않고, `prepare_model1_runtime.py`가 만든 검증된 runtime만 read-only bind mount한다.

analysis worker는 queue polling 전에 Model 1 weight/runtime manifest와 Model 2/3 artifact의
SHA-256을 모두 확인한다. 하나라도 빠지거나 다르면 `unavailable`로 계속 실행하지 않고
fail-closed로 종료한다. 따라서 `.env` 생성 전에 Model 1 runtime 준비가 필수이며,
`PREREVIEW_MODEL1_SERVING_HOST_DIR`, UID, GID를 빈 채로 Compose를 실행할 수 없다.

배포 API는 operator가 지정한 revision label을 받지 않는다. Dockerfile의 pristine identity
stage가 `COPY .` 직후, pip install보다 먼저 모든 backend build-context 파일·directory의
canonical path·kind·mode·content를 SHA-256으로 계산한다. final runtime stage는 그 identity
artifact만 복사해 `.prereview-build-id`에 둔다. `.dockerignore`의 credentials(`*.pem`,
`*.key`, `secrets/`)와 local Python build outputs, generated identity 파일은 digest와 image
context에서 함께 제외된다. 따라서 오래된 소스를 새 commit ID로 거짓 표기하거나 host build
산출물이 image identity를 오염시킬 수 없고, runtime 환경변수/Compose build arg로 바꾸는 경로도 없다.

```bash
git diff --quiet && git diff --cached --quiet
test -z "$(git ls-files --others --exclude-standard)"
docker compose up -d --build
```

`run_local_live_e2e.py --worker-mode external`은 `/health/ready`의 본문·헤더 ID가
서로 같은지뿐 아니라, 실행한 clean checkout에서 독립 계산한 backend Docker-context
digest와 정확히 같은지도 확인한다. Git commit은 execution manifest에 별도로 남긴다.
따라서 형식은 맞지만 오래된 API나 dirty checkout은 release E2E를 통과할 수 없다.
`--api-base-url`은 `--worker-mode external`에서만 허용된다. E2E trace를 저장할 경우에는
repo 밖의 경로를 사용하고, release build와 E2E 사이에는 tracked/untracked 파일을 바꾸지
않는다.
호스트의 `.runtime/ml-venv`는 Existing Model 1 one-shot backfill 또는 host 직접 개발용일
뿐, 컨테이너에 mount하지 않는다.

```bash
cd backend
# 위 절차로 .env 준비
docker compose up -d --build
docker compose ps
```

Model 2의 `joblib`은 임의 코드 실행 형식이므로, Docker ML worker는 등록된 SHA-256과
일치하는 신뢰된 artifact만 사용한다. 이 hash 검증은 artifact 교체를 막는 무결성 경계이지,
ML child를 완전한 sandbox로 만드는 기능은 아니다. 운영 시에는 worker 전용 OS 계정, 최소
권한 DB credential, private Storage 및 read-only mount를 유지한다. 현재 ML 이미지와
`rhwp` native parser 경로는 rootful Docker가 동작하는 Linux `amd64`를 기준으로
검증되었다. host UID가 별도 user namespace로 매핑되는 rootless Docker는 현재 지원하지
않는다.

환경변수별 의미, 같은 호스트/WSL/EC2 주소 설정, 재기동과 장애 확인은
[FastAPI·worker 운영 가이드](fastapi/docs/FASTAPI_WORKER_RUNBOOK.md)에 정리되어 있다.

`api`만 떠 있고 worker가 없으면 요청은 `queued`에 머문다. 배포 확인 시 `/health/ready`
만 보지 말고 `docker compose ps worker chat-worker report-worker`와 최근
`ops.processing_run`도 함께 확인한다.

`/health/live`는 프로세스 생존만, `/health/ready`는 online 의존성 설정 여부를
나타낸다.

## Worker queue

PostgreSQL polling queue의 기반은 migration 21~26이고, v0.2 lifecycle/public result/chat와
전역 admission 경계와 analysis embedding execution provenance는 migration 33~40이 확정한다. 모두 적용한 DB에서만 현재
FastAPI/worker를 실행한다.

업로드 파일 바이트 상한은 `PREREVIEW_UPLOAD_MAX_BYTES`(기본 50 MiB), multipart boundary와
모든 part를 포함한 HTTP 요청 전체 상한은 `PREREVIEW_HTTP_MAX_BODY_BYTES`(기본 51 MiB)다.
Compose의 Uvicorn replica별 동시 처리 상한은 `PREREVIEW_API_LIMIT_CONCURRENCY`(기본 32)다.
analysis upload reservation과 chat create/retry는 `PREREVIEW_GLOBAL_QUEUE_MAX`(기본 25)의
하나의 PostgreSQL backlog cap을 공유하며, 새 작업이 가득 차면 `503`을 반환한다. 정확한
idempotency replay는 기존 작업을 반환한다.

- `workspace.analysis_run`: 브라우저에 보이는 작은 상태 레코드
- `workspace.analysis_run_dispatch`: source 위치, claim, lease, heartbeat 같은 worker 전용 상태
- `ops.processing_run`: 실행 시도 이력. `processing_run_pk`가 fencing token이다.
- `workspace.claim_next_analysis_run()`: `FOR UPDATE SKIP LOCKED`로 claim
- `workspace.persist_analysis_result_core_v2()`: 유효한 fence를 확인한 뒤 v0.2 raw/public 결과와 terminal 상태를 한 transaction으로 반영

기본값은 heartbeat 30초, lease 120초, 전체 최대 두 번의 시도다. 오래된 worker는
새 worker의 결과를 덮어쓸 수 없다.

v0.2 retrieval은 request에서 실제로 준비된 purpose/target/support 0~3축만 보낸다. 0축은
`RETRIEVAL_INPUT_MISSING`으로 SIM을 건너뛰며 zero vector를 만들지 않는다. 1~3축은 가능한
축 수 `|A|`의 평균으로 검색한다. `PREREVIEW_EXISTING_KB_REQUIRED=true`이면 활성 KB/검색
결과 부재는 재시도/실패이고, `false`이면 분석은 `KB_EMPTY`로 정상 완료될 수 있다.

LLM 실행기는 `PREREVIEW_LLM_PROVIDER=openai|vllm`로 선택하며, 비어 있으면 기존과
동일하게 OpenAI다. 각 제공자는 공통 fallback 모델과 Request Profile/CPL/FIT/SIM/chat
단계별 override를 가진다. retrieval embedding은 별도
`PREREVIEW_EMBEDDING_PROVIDER=openai` 경계로 고정되어 있으므로 LLM만 vLLM으로 바꿔도
Existing vector provenance는 바뀌지 않는다. 알 수 없는 provider나 선택한 provider의
필수 설정이 빠지면 worker는 queue claim 전에 종료한다.

vLLM은 현재 experimental 선택지다. adapter는 OpenAI SDK의 strict JSON Schema 변환을
그대로 쓰고 `temperature=0`, `top_p=1`, `seed=0`과 `max_tokens`를 보낸다. 이는 동일
runtime에서의 재현성을 돕지만 서버·모델 버전과 병렬 실행에 따라 결정성은 best-effort다.
호출 전체 deadline 및 streaming body/output 상한을 적용하며, 실제 vLLM 서버 canary는 아직
실행하지 않았다. vLLM endpoint는 HTTPS만 허용하고, local 개발의 `localhost`/`127.0.0.0/8`/
`::1`에만 HTTP를 허용한다. runtime-bound manifest 계약이 아직 없으므로 external release
E2E에서 vLLM 선택은 명시적으로 NO-GO다. worker heartbeat는 별도 thread에서
lease를 renew하므로 LLM deadline과 lease 총 길이 사이에 startup restriction을 두지 않는다.
Request Profile의 LLM 출력은 최종 profile이 아니라 source selection이고, 최종 profile은
local exact-span materialization으로 만든다. 따라서 `VLLM_MAX_OUTPUT_TOKENS` 기본 16384
(상한 32768)는 16,836-byte selection fixture에 여유를 주되 무제한 출력은 허용하지 않는다.

OpenAI Request Profile 구조화는 Compose 기본 `gpt-5.6-terra`, 호출별 hard timeout
120초, `OPENAI_MAX_REPAIRS=2`를 사용한다. repair 수는 DB queue 재시도 횟수가 아니다.
한 worker attempt 안에서 최초 구조화 호출 뒤 서버 검증 오류를 첨부한 수정 호출을 최대
두 번 더 허용한다(따라서 최대 세 번). 긴 HWP/HWPX의 구조화 응답 시간을 유한하게
보장하면서도, 한 번의 수정만으로 서로 다른 근거·컴포넌트 검증을 모두 해결하지 못한
실측 사례를 수용하기 위한 값이다. CPL·FIT·SIM·채팅은 단계별 override가 없으면
`OPENAI_LLM_MODEL`을 사용하고, 같은 bounded repair 상한은 FIT·SIM에도 전달된다.

parser는 기본 120초 hard deadline을 사용하며 timeout 시 process group 전체를
종료·회수한다. HWPX의 선언된 압축 해제 크기에도 상한을 두고, parser subprocess에는
DB·Storage·OpenAI 비밀값을 전달하지 않는다.

Docker 이미지에는 `rhwp-python`이 요구하는 `libexpat1`과 `libfreetype6`를 함께
설치한다. 또한 `cl100k_base` tokenizer cache를 build 단계에서 검증해 이미지에
포함하므로 retrieval token 계산을 위해 런타임에 외부 파일을 받지 않는다. 이미지
parser smoke는 다음처럼 네트워크 없이 재현할 수 있다.

```bash
docker run --rm --network none \
  -e PREREVIEW_FREETYPE_LIB=/usr/lib/x86_64-linux-gnu/libfreetype.so.6 \
  -v "$PWD/../docs/pre_review_request_e2e_5_20260909_v1/generated:/fixtures:ro" \
  backend-api:latest \
  python scripts/validate_synthetic_hwpx_fixtures.py --fixture-dir /fixtures
```

2026-09-13 수행한 Docker external acceptance에서는 run
`f3e3c8c1-9988-4db2-8f6b-bdbed6472399`, case
`c05d9ae0-d279-4839-8a86-102da0be18fd`로 완료됐다. analysis worker
`4d6aae5d4c87:1:540b85e666be`와 chat worker `ea084ec9c993:1:c39815e56162`가 각각
DB queue attempt 1회로 처리했고, CPL 13, evidence 167, FIT 7, SIM 후보 5,
Model 1/2/3 모두 `OK`, 채팅 `completed`와 reference 13개를 확인했다. 검증 image는
analysis worker `sha256:12adb17d…`, chat worker `sha256:f0669e6e…`였다.
입력은 `samples/hwpx/mockup_08_CPL전항목_스마트기술사업화.hwpx` 합성 fixture였다.

이 성공 run에서는 Request Profile용 Terra 호출이 최초 1회와 수정 2회, 총 3회였음이
worker 로그로 확인됐다. 성공 run의 안전한 로그는 개별 validation 문구를 노출하지 않는다.
동일 입력의 별도 진단에서는 먼저 `f_scale_count`의 모호한 legacy `anchor_text`에
`value_span_candidate_id`가 필요했고, 다음으로 `stage_support`에 금액 회차만이 아닌
컴포넌트 범위의 수혜자·자격·참여 조건 경계가 필요했다. 두 오류를 순서대로 보정한 기록이
있어 수정 한도를 2로 둔다. 이는 자유 재생성이 아니라 이전 selection과 서버 검증 오류를
다음 호출에 전달하는 제한된 보정이며, 분석 worker 자체의 DB queue attempt는 1회였다.

상세 계약은 [FastAPI 문서](fastapi/docs/0.FASTAPI_FRONTEND_API_SPEC.md),
[worker 저장 계약](fastapi/docs/WORKER_RESULT_PERSISTENCE_CONTRACT.md),
[Supabase 운영 안내](supabase/README.md),
[Existing KB bootstrap](supabase/EXISTING_KB_BOOTSTRAP.md)을 따른다.
