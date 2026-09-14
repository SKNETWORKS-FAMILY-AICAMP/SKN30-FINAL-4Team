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
| Frontend | FastAPI REST 호출, Cookie 포함, `analysis/current`·단건 run/message polling |
| FastAPI | Supabase Auth proxy, Origin 검증, upload lifecycle/소유권 확인, typed public result read API |
| Supabase | Auth, Postgres/pgvector, private Storage |
| analysis worker | DB queue claim, HWP/HWPX → Common IR → Request Profile, 0~3축 embedding/후보 검색, OpenAI 비교, v0.2 결과 materialisation |
| chat worker | public allow-list 결과 근거 기반 별도 queue claim, LLM 답변·reference fenced 저장 |
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

Auth provider의 transport/5xx 같은 transient `503`은 refresh, `GET /auth/me`, 업무 API에서
기존 Cookie를 보존한다. provider가 access/refresh credential을 invalid/expired로 판정한
경우에만 `401`과 함께 Cookie를 삭제한다. `POST /auth/sign-out`은 provider 장애로 `503`이
되어도 로컬 access/refresh Cookie를 항상 삭제한다.

## 현재 FastAPI 경계

| API | 내부 처리 |
|---|---|
| `POST /auth/sign-in` | Supabase 로그인, HttpOnly Cookie 설정 |
| `POST /auth/sign-up` | Supabase 가입 |
| `POST /auth/refresh` | refresh Cookie로 세션 갱신 |
| `POST /auth/sign-out` | Supabase logout, Cookie 삭제 |
| `POST /auth/password-reset`, `update-password`, `GET /auth/me` | Auth 보조 흐름/현재 사용자 검증 |
| `POST /analysis-runs` | UUID `Idempotency-Key` → v2 `uploading` 예약 → Storage → artifact+`queued` 원자 확정 |
| `GET /analysis-runs/{id}`, `GET /analysis/current` | 소유자 단건 run polling 및 single-snapshot `processing`/`ready`/`idle` polling |
| 결과/후보/세션/이력 GET | v2 `api` RPC를 server PostgreSQL 연결로 owner-scoped typed DTO 조회; history는 signed snapshot cursor |
| `POST /analysis-sessions/{id}/close` | supplied session만 owner-scoped/idempotent하게 종료, 성공은 204 |
| `POST /analysis-cases/{id}/messages`, `GET .../messages/{message_id}`, `GET .../messages`, `POST .../messages/{assistant_id}/retry` | UUID `Idempotency-Key`로 assistant turn 생성/재시도, 단건 polling과 signed cursor history; chat worker가 별도 queue에서 답변/reference 완료 |

분석 run은 `queued`, `running`, `succeeded`, `failed` 등의 작은 공개 상태만 가진다.
worker의 claim/lease/error detail은 공개 run이 아니라 dispatch·operations 영역에 둔다.

## 프론트엔드 cutover handoff

**프론트엔드의 legacy API 계약은 아직 v0.2로 cutover되지 않았다.** 이 backend만
배포하면 기존 프론트엔드와 호환되지 않으므로, 아래 변경을 프론트엔드에 반영하고
동시 maintenance cutover와 smoke test를 완료하기 전에는 통합 완료 또는
production-ready로 표시하지 않는다. 기존 legacy 응답 모양을 backend에서 dual
지원하는 계약은 아니다.

프론트엔드가 반드시 반영할 v0.2 변경은 다음과 같다.

- upload와 chat create/retry 요청에 매번 UUID v4 `Idempotency-Key`를 보내고, 같은
  작업 재시도에는 같은 키를 재사용한다. 새 작업에만 새 UUID를 만든다.
- analysis history와 conversation history는 배열이 아니라
  `{items, next_cursor}` envelope이며, `next_cursor`는 opaque cursor로 수정하지
  않고 다음 요청의 `cursor`에 그대로 전달한다.
- chat create/retry의 `assistant_message_id`를 저장한 뒤 단건 message polling
  endpoint를 호출해 `generating`에서 `completed` 또는 `failed`를 확인한다.
- 분석 화면의 초기·복귀 상태는 `GET /analysis/current`의 `processing`, `ready`,
  `idle` 세 상태를 처리한다.
- CPL/FIT/SIM 결과와 SIM candidate detail은 opaque/raw legacy shape가 아닌
  strict typed public DTO를 사용한다. 후보 상세의 typed metadata/comparison,
  축별 CPL/FIT/SIM detail과 evidence 배열을 생성 OpenAPI schema와 사람용 명세에 맞춰
  렌더링한다.
- 회원가입 `display_name`은 1~100자의 필수 필드이며, 성공 auth 응답의
  `display_name`도 표시한다.

프론트 구현의 유일한 사람용 계약은
[프론트엔드 API 명세](0.FASTAPI_FRONTEND_API_SPEC.md)다. exact DTO shape는 배포 대상의
`/openapi.json`에서 확인한다. `FASTAPI_RESPONSE_CONTRACT.json`은 mock용 비규범 예시이며
별도 계약이 아니다.

## Worker queue와 결과 저장

### Migration 21: polling/lease

- `workspace.analysis_run_dispatch`에 source 위치, `processing_run_pk`, attempt,
  lease/heartbeat를 저장한다.
- worker는 `workspace.claim_next_analysis_run(worker_id, lease_seconds)`를 호출한다.
  내부적으로 `FOR UPDATE SKIP LOCKED`를 사용한다.
- 기본 heartbeat는 30초, lease는 120초다. 첫 실패/lease 만료는 한 번 재시도하고,
  두 번째 시도 실패는 terminal failed다.
- 모든 시도는 `ops.processing_run` 한 건으로 남는다. 그 UUID가 fencing token이다.

### Migration 33~40: v0.2 lifecycle, public result, partial retrieval, chat queue, admission, 실행 provenance

worker는 `workspace.persist_analysis_result_core_v2(analysis_run_pk,
processing_run_pk, result_json)`만으로 결과를 완료한다. 이 함수는 live lease와 token을
잠그고 migration 26의 ML writer를 보존한 뒤 다음을 한 transaction에서 처리한다.

1. raw audit와 typed public `result.analysis_case`, CPL/FIT axes, SIM candidates/evidence/session materialisation
2. `ops.processing_run=succeeded`
3. dispatch lease 해제와 `analysis_run=succeeded`

오래되었거나 대체된 token은 `NULL`을 반환하며, 결과나 상태를 전혀 바꾸지 않는다.
migration 18의 unfenced Edge callback은 service-role에서 retire되었으므로 새 worker는
사용하면 안 된다.

migration 33의 upload/current/history/close RPC, migration 34의 result/candidate RPC,
migration 35의 partial-axis retrieval, migration 36의 chat create/read/retry/claim RPC,
migration 37의 analysis/chat 공용 전역 queue admission RPC와 migration 39의 atomic
upload-finalisation RPC는
모두 `service_role` 전용이다. FastAPI는 privileged DSN에서 검증된 사용자 UUID를 명시적
인자로 넘기고 각 RPC의 owner predicate를 통과한 typed public DTO만 반환한다. raw result,
fact ID, ranking score, diagnostics는 API와 chat prompt에 전달하지 않는다.

retrieval은 request에서 실제 생성된 purpose/target/support 0~3축만 보내며 zero vector를
만들지 않는다. 0축은 `RETRIEVAL_INPUT_MISSING`으로 SIM을 생략하고, 1~3축은 가능한 축 수
`|A|` 평균 cosine으로 검색한다. delivery는 aggregate/comparable core 축이 아니다.

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
| `PREREVIEW_CURSOR_SIGNING_SECRET` | FastAPI analysis/conversation history opaque cursor HMAC (online에서는 필수) |
| `PREREVIEW_UPLOAD_MAX_BYTES` | FastAPI가 multipart에서 추출한 단일 HWP/HWPX 파일 상한(기본 50 MiB) |
| `PREREVIEW_HTTP_MAX_BODY_BYTES` | multipart boundary/header와 모든 part를 포함한 HTTP 전체 요청 상한(기본 51 MiB) |
| `PREREVIEW_API_LIMIT_CONCURRENCY` | Compose Uvicorn replica별 `--limit-concurrency` 상한(기본 32) |
| `PREREVIEW_GLOBAL_QUEUE_MAX` | 모든 API replica가 공유하는 analysis upload + chat create/retry admission cap(기본 25) |
| `OPENAI_API_KEY`, 모델 설정 | worker |
| `PREREVIEW_WORKER_*` | worker polling/lease 설정 |
| `PREREVIEW_EXISTING_KB_REQUIRED` | analysis worker의 KB empty fail-closed (`true`) / `KB_EMPTY` 완료 (`false`) 설정 |
| `PREREVIEW_CHAT_WORKER_*` | chat worker polling/lease 설정 |

실제 값은 서버 `.env` 또는 secret store에만 둔다. 로그, Git, 브라우저에 출력하지
않는다.

## 구현 상태와 다음 범위

현재 request 분석의 worker 경로는 구현되어 있다. `worker.main`은 PostgreSQL polling
runtime, Supabase private Storage, OpenAI LLM/embedding adapter,
HWP/HWPX → Common IR → Request Profile → retrieval/CPL/FIT/SIM handler를 조립한다.
Common IR/Profile artifact·lineage·Request Profile projection과 fenced 결과 저장도 이
경로에 포함된다. Model 1/2/3 결과는 analysis result와 함께 저장되며, 공개 결과에는
안전한 projection만 노출된다.

채팅은 request 분석과 분리된 queue/API/worker로 구현되어 있다. migration 36은
assistant message의 v2 idempotency, lease/fencing과 근거 reference 저장을 소유하고,
전역 신규 작업 admission은 migration 37이 소유하며,
`worker.chat_main`이 public allow-list 결과 근거 기반 handler를 조립한다. 2026-09-13 합성 HWPX live E2E에서
run `5e51dae9-3c6e-4ed8-b4c6-96185917b08b`, case
`2d02ae97-85f0-4678-a9fe-e006ab389bd1`가 analysis/chat worker 각각 한 번의 attempt로
완료했다. Request Profile은 Terra, FIT/SIM과 채팅은 Luna였고, ML 1/2/3은 모두 `OK`,
CPL 13/FIT 7/SIM 후보 1/evidence 77/chat reference 13이었다.

이는 합성 HWPX 한 건의 E2E 결과다. 실제 Hancom 작성 HWP/HWPX의 완전 재검증과 Existing
로컬 검증 DB의 Existing Model 1 backfill은 100/100 완료됐고 활성 결과는 `신뢰` 99건,
raw `판단보류` 1건이다. 새 서버에서는 동일 운영 절차를 별도로 실행해야 한다. PDF/OCR 및
PDF 생성은 현재 구현·E2E 완료 범위가 아니다. 추가 운영 범위는 worker readiness의 queue lag/heartbeat 관측,
`ops.model_invocation` 호출 감사 기록, request-temp 및 만료 결과 cleanup이다.

공개 API 상세는 [0.FASTAPI_FRONTEND_API_SPEC.md](0.FASTAPI_FRONTEND_API_SPEC.md),
worker payload는 [WORKER_RESULT_PERSISTENCE_CONTRACT.md](WORKER_RESULT_PERSISTENCE_CONTRACT.md)를
따른다.
