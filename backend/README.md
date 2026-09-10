# PreReview backend

현재 런타임의 공개 경계는 FastAPI다. 브라우저는 FastAPI만 호출하며, Supabase의
Auth·PostgreSQL/pgvector·private Storage는 서버 내부 인프라로 사용한다.

```text
Frontend (HttpOnly Cookie)
  → FastAPI /api/v1
      ├─ Supabase Auth
      ├─ Postgres / pgvector / private Storage
      ├─ workspace.analysis_run (queued) → analysis worker → CPL/FIT/SIM/ML
      └─ conversation message (generating) → chat worker → grounded answer
```

- Redis/RQ, 브라우저의 Supabase 직접 호출, 현재 런타임의 Edge Function dispatch/callback은 사용하지 않는다.
- 분석 진행 상황은 `GET /api/v1/analysis-runs/{id}` 폴링으로 조회한다. SSE/WebSocket은 현재 구현 범위가 아니다.
- worker는 신뢰된 서버 프로세스이며 PostgreSQL과 private Storage에만 내부 자격증명으로 접근한다. 브라우저 Cookie·사용자 token은 worker에 전달하지 않는다.

## 현재 API

- `POST /api/v1/auth/sign-in`, `sign-up`, `refresh`, `sign-out`, `password-reset`, `update-password`
- `GET /api/v1/auth/me`
- `POST /api/v1/analysis-runs` — HWP/HWPX 요청서 업로드와 queued run 생성
- `GET /api/v1/analysis-runs/{analysis_run_id}` — 작업 상태 폴링
- `GET /api/v1/analysis-cases/{analysis_case_id}`
- `GET /api/v1/sim-candidates/{sim_candidate_id}`
- `GET /api/v1/analysis-sessions/active`
- `GET /api/v1/analysis-history`
- `POST /api/v1/analysis-cases/{analysis_case_id}/messages`
- `GET /api/v1/analysis-cases/{analysis_case_id}/messages`
- `POST /api/v1/analysis-cases/{analysis_case_id}/messages/{assistant_message_id}/retry`

PDF 생성 API는 데이터 모델은 있으나 아직 이 공개 경계에 구현하지 않았다. 메시지
POST/retry는 `202 Accepted`이며 별도 `chat-worker`가 저장된 분석 결과만 근거로 답한다.

## 로컬 실행

```bash
uv sync --frozen --extra dev
uv run uvicorn main:app --reload --host 127.0.0.1 --port 8001
```

환경 파일 없이 실행하면 기본은 offline-safe 모드다. 이 실행은 OpenAPI와 정적 계약
확인용이며 실제 Supabase/worker 분석을 수행하지 않는다. online Compose 모드에는
`.env.example`의 Supabase·PostgreSQL 설정과 허용할 프론트 origin이 필요하다. 실제
비밀값은 커밋하지 않는다.

```bash
# 저장소 루트 .env와 .runtime/supabase-dev/.env가 이미 준비된 로컬 환경
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
운영 가이드의 별도 절차를 따른다.

API와 same-server worker를 Docker Compose로 함께 기동할 때는 분석 `worker`와
`chat-worker`가 기본 service로 포함된다. worker 이미지에 `8000/tcp`가 표시될 수 있지만
호스트 포트로 publish하지 않는다.

```bash
cd backend
# 위 절차로 .env 준비
docker compose up -d --build
docker compose ps
```

환경변수별 의미, 같은 호스트/WSL/EC2 주소 설정, 재기동과 장애 확인은
[FastAPI·worker 운영 가이드](fastapi/docs/FASTAPI_WORKER_RUNBOOK.md)에 정리되어 있다.

`api`만 떠 있고 worker가 없으면 요청은 `queued`에 머문다. 배포 확인 시 `/health/ready`
만 보지 말고 `docker compose ps worker chat-worker`와 최근 `ops.processing_run`도 함께 확인한다.

`/health/live`는 프로세스 생존만, `/health/ready`는 online 의존성 설정 여부를
나타낸다.

## Worker queue

PostgreSQL polling queue와 원자적 결과 저장·legacy 완료 경로 폐기는 migration 21~24가
정의한다.

- `workspace.analysis_run`: 브라우저에 보이는 작은 상태 레코드
- `workspace.analysis_run_dispatch`: source 위치, claim, lease, heartbeat 같은 worker 전용 상태
- `ops.processing_run`: 실행 시도 이력. `processing_run_pk`가 fencing token이다.
- `workspace.claim_next_analysis_run()`: `FOR UPDATE SKIP LOCKED`로 claim
- `workspace.persist_analysis_result_core()`: 유효한 fence를 확인한 뒤 결과와 terminal 상태를 한 transaction으로 반영

기본값은 heartbeat 30초, lease 120초, 전체 최대 두 번의 시도다. 오래된 worker는
새 worker의 결과를 덮어쓸 수 없다.

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

상세 계약은 [FastAPI 문서](fastapi/docs/0.FASTAPI_FRONTEND_API_SPEC.md),
[worker 저장 계약](fastapi/docs/WORKER_RESULT_PERSISTENCE_CONTRACT.md),
[Supabase 운영 안내](supabase/README.md),
[Existing KB bootstrap](supabase/EXISTING_KB_BOOTSTRAP.md)을 따른다.
