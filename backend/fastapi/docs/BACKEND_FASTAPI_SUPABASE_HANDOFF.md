# 백엔드 핸드오프: FastAPI + self-hosted Supabase

## 확정 구조

```text
Frontend
  → FastAPI (공개 API, Cookie 인증, 업로드·조회)
      ├─ Supabase Auth
      ├─ PostgreSQL + pgvector
      └─ private Storage
             ↑
same-server worker (PostgreSQL polling, parsing, OpenAI, 결과 저장)
```

Supabase는 Auth, PostgreSQL/pgvector, private Storage를 제공한다. FastAPI는 브라우저의
유일한 업무 API이고, worker는 같은 서버의 신뢰된 별도 프로세스다.

현재 사용하지 않는 것: 브라우저 Supabase 직접 호출, Redis/RQ, Edge Function
dispatch/callback, 외부 GPU worker, SSE/Realtime 상태 push. `backend/supabase/functions`
파일은 과거 대안/참고용으로 남아 있지만 현재 배포·호출 경로가 아니다.

## 책임

| 구성요소 | 책임 |
|---|---|
| Frontend | FastAPI REST 호출, Cookie 포함, analysis-run 상태 polling |
| FastAPI | Supabase Auth proxy, Origin 검증, 업로드, 소유권 확인, 결과 read API |
| Supabase | Auth, Postgres/pgvector, private Storage |
| analysis worker | DB queue claim, HWP/HWPX → Common IR → Request Profile, 임베딩/후보 검색, OpenAI 비교, 결과 materialisation |
| PostgreSQL | durable queue, attempt audit, fencing, 결과/lineage/벡터 데이터 |

브라우저에는 Supabase URL/key, service/secret key, DB URL, OpenAI key를 전달하지
않는다. worker에도 브라우저 Cookie나 사용자 access token을 전달하지 않는다.

## 인증 경계

FastAPI는 Supabase Auth로 password grant/refresh/user verification을 수행한다.
access/refresh token은 `pre_review_access`, `pre_review_refresh` HttpOnly Cookie에만
저장하고 JSON 응답으로 반환하지 않는다. 모든 업무 route는 검증된 `sub`만 소유권
판단에 사용한다.

Cookie 상태 변경 요청은 허용 origin 목록으로 검사한다. CORS도 같은 정확한 목록과
`allow_credentials=true`로 설정한다. 운영은 HTTPS + `Secure=true`를 사용한다.

## 현재 FastAPI 경계

| API | 내부 처리 |
|---|---|
| `POST /auth/sign-in` | Supabase 로그인, HttpOnly Cookie 설정 |
| `POST /auth/sign-up` | Supabase 가입 |
| `POST /auth/refresh` | refresh Cookie로 세션 갱신 |
| `POST /auth/sign-out` | Supabase logout, Cookie 삭제 |
| `POST /auth/password-reset`, `update-password`, `GET /auth/me` | Auth 보조 흐름/현재 사용자 검증 |
| `POST /analysis-runs` | `Idempotency-Key` → DB `uploading` 예약 → Storage → artifact+`queued` 원자 확정 |
| `GET /analysis-runs/{id}` | 소유자 상태 polling |
| 결과/후보/세션/이력 GET | `api` views/RPC를 서버 PostgreSQL 연결로 owner-scoped 조회 |

분석 run은 `queued`, `running`, `succeeded`, `failed` 등의 작은 공개 상태만 가진다.
worker의 claim/lease/error detail은 공개 run이 아니라 dispatch·operations 영역에 둔다.

## Worker queue와 결과 저장

### Migration 21: polling/lease

- `workspace.analysis_run_dispatch`에 source 위치, `processing_run_pk`, attempt,
  lease/heartbeat를 저장한다.
- worker는 `workspace.claim_next_analysis_run(worker_id, lease_seconds)`를 호출한다.
  내부적으로 `FOR UPDATE SKIP LOCKED`를 사용한다.
- 기본 heartbeat는 30초, lease는 120초다. 첫 실패/lease 만료는 한 번 재시도하고,
  두 번째 시도 실패는 terminal failed다.
- 모든 시도는 `ops.processing_run` 한 건으로 남는다. 그 UUID가 fencing token이다.

### Migration 22/23: fenced materialisation과 읽기 모델

worker는 `workspace.persist_analysis_result_core(analysis_run_pk,
processing_run_pk, result_json)`만으로 결과를 완료한다. 이 함수는 live lease와 token을
잠근 뒤 다음을 한 transaction에서 처리한다.

1. `result.analysis_case`, axes, SIM candidates, evidence, session materialisation
2. `ops.processing_run=succeeded`
3. dispatch lease 해제와 `analysis_run=succeeded`

오래되었거나 대체된 token은 `NULL`을 반환하며, 결과나 상태를 전혀 바꾸지 않는다.
migration 18의 unfenced Edge callback은 service-role에서 retire되었으므로 새 worker는
사용하면 안 된다.

migration 23은 retention 만료 결과를 `api` 결과 RPC·history view에서 fail-closed로
숨기고, SIM 후보별 evidence snapshot을 후보 상세 RPC에 포함한다. FastAPI는 privileged
DSN으로 연결하더라도 결과 읽기 때 `SET LOCAL ROLE authenticated`와 검증된 사용자 UUID
claim만 설정해 이 `api` read-model/RLS 경계를 따른다.

## Storage와 중간 산출물

`request-temp`에는 요청 원본과 worker 산출물(Common IR, Request Profile)을 private로
보관한다. 각 파일은 `workspace.source_artifact`에 bucket/object key/SHA-256/version을
등록하고 다음 lineage를 남긴다.

```text
source → common_ir → structured_profile
```

Request Profile의 관계형 projection은 trusted worker가
`workspace.ingest_request_profile_core()`로 적재한다. Existing 공고는 `existing-kb`와
`kb.*`; PDF 보고서는 `analysis-reports`와 `result.report_artifact`를 사용한다.

## 환경변수

| 변수 | 사용 주체 |
|---|---|
| `SUPABASE_URL`, `SUPABASE_ANON_KEY` | FastAPI Auth 검증 |
| `SUPABASE_SECRET_KEY` 또는 `SUPABASE_SERVICE_ROLE_KEY` | FastAPI/worker private Storage |
| `DATABASE_URL` | FastAPI repository와 same-server worker |
| `OPENAI_API_KEY`, 모델 설정 | worker |
| `PREREVIEW_WORKER_*` | worker polling/lease 설정 |

실제 값은 서버 `.env` 또는 secret store에만 둔다. 로그, Git, 브라우저에 출력하지
않는다.

## 구현 상태와 다음 범위

현재 request 분석의 worker 경로는 구현되어 있다. `worker.main`은 PostgreSQL polling
runtime, Supabase private Storage, OpenAI LLM/embedding adapter,
HWP/HWPX → Common IR → Request Profile → retrieval/CPL/FIT/SIM handler를 조립한다.
Common IR/Profile artifact·lineage·Request Profile projection과 fenced 결과 저장도 이
경로에 포함된다.

아직 request 분석과 별도로 남은 범위는 채팅/PDF job type·공개 API, worker
readiness의 queue lag/heartbeat 관측, `ops.model_invocation` 호출 감사 기록,
request-temp 및 만료 결과 cleanup이다. 이들은 현재 `analysis_run` worker 계약을
확장하기 전에 별도 계약과 migration으로 정의한다.

공개 API 상세는 [0.FASTAPI_FRONTEND_API_SPEC.md](0.FASTAPI_FRONTEND_API_SPEC.md),
worker payload는 [WORKER_RESULT_PERSISTENCE_CONTRACT.md](WORKER_RESULT_PERSISTENCE_CONTRACT.md)를
따른다.
