# 데이터 스키마 및 보관 아키텍처

기준일: 2026-09-10

## 현재 운영 경계

```text
Frontend ──HttpOnly Cookie──> FastAPI
                                ├─ Supabase Auth
                                ├─ PostgreSQL + pgvector
                                └─ private Storage
                                       ↑
                              same-server polling worker
```

브라우저는 Supabase Auth, PostgREST, Storage, Realtime, Edge Function을 직접 호출하지 않는다.
로그인·업로드·조회는 FastAPI만 공개하고, PostgreSQL DSN과 anon/service-role key는 서버에만
둔다. Supabase Studio는 운영자용 관리 화면이지 서비스 API가 아니다.

## 스키마의 기준 파일

- 전체 현황과 적용 순서: `backend/supabase/MIGRATION_MANIFEST.md`
- 순차 migration: `backend/supabase/migrations/01`~`24`
- self-hosted 운영: `backend/supabase/README.md`
- Existing 100건 bootstrap: `backend/supabase/EXISTING_KB_BOOTSTRAP.md`
- worker DB 경계: `backend/supabase/WORKER_DB_ACCESS.md`

현재 application table은 60개이며 `app`, `ops`, `kb`, `workspace`, `result`,
`retrieval` 여섯 data-bearing schema를 사용한다. `api` schema는 owner-scoped View/RPC
계약이며 table을 갖지 않는다. 정확한 table·index 수와 migration별 역할은 manifest를
단일 기준으로 삼는다.

## 데이터 영역

```text
app
└─ user_profile                         Supabase Auth 사용자의 서비스 프로필

ops
├─ processing_run                       worker 실행 시도와 fencing token
├─ model_invocation                     모델 호출 감사 구조
└─ cleanup_event                        물리 정리 감사 구조

kb                                      Existing 공고 영구 지식베이스
└─ notice → source_profile → source_version
                            ├─ artifact → artifact_lineage
                            └─ profile_version
                               ├─ component/fact/evidence/relation
                               ├─ target constraint
                               └─ support/delivery/scale projection

workspace                               사용자 요청 처리 작업공간
└─ analysis_run
   ├─ analysis_run_dispatch             private queue/lease/source 위치
   ├─ source_artifact → artifact_lineage
   └─ request_profile와 구조화 projection

result                                  비교 결과와 읽기 모델
└─ analysis_case
   ├─ axis_result
   ├─ sim_candidate → evidence_snapshot
   ├─ analysis_session/conversation
   └─ report_artifact

retrieval
├─ embedding_configuration
└─ existing_profile_embedding
```

## 파일과 JSON 저장 원칙

파일 자체는 PostgreSQL byte column에 넣지 않고 private Storage에 둔다. DB의 artifact
행에는 bucket, content-addressed object key, SHA-256, MIME, 크기, schema version과
`ops.processing_run` 연결을 기록한다. lineage는 원본 → Common IR → Profile 등 변환
관계를 별도 행으로 보존한다.

### `existing-kb`

기업마당 Existing 공고의 원본 HWP/HWPX/PDF, ingestion record, Common IR,
source-selection/candidate pack, structured Profile을 보관한다. `kb.source_version`과
`kb.profile_version`은 current 버전을 하나만 표시하며 과거 버전은 이력으로 유지한다.

```text
{notice_id}/{source_profile_id}/{source_sha256}/{artifact_type}/{content_sha256}.{ext}
```

100건 데이터팩은 Git에 넣지 않는다. checksum manifest, importer, 검증기로 새 DB에
bootstrap한다.

### `request-temp`

사용자가 FastAPI에 업로드한 HWP/HWPX와 worker가 만든 Common IR, candidate pack,
Request Profile을 보관한다.

```text
{analysis_run_pk}/{artifact_type}/{content_sha256}.{ext}
```

현재 SQL에는 expiry와 `cleanup_pending` 상태가 있으나 Storage/DB 물리 삭제 scheduler는
아직 없다. 이름과 달리 자동 삭제된다고 가정하면 안 된다.

### `analysis-reports`

추후 생성할 PDF 보고서를 private 객체로 보관한다.

```text
{user_id}/{analysis_case_pk}/{report_type}/{content_sha256}.{ext}
```

현재 보고서 생성 API/worker는 미구현이다.

## queue와 결과 원자성

PostgreSQL이 Redis/RQ 없이 durable queue 역할을 한다. worker는
`workspace.claim_next_analysis_run()`으로 claim하고 30초 heartbeat, 120초 lease,
최대 두 번의 시도를 사용한다. `ops.processing_run.processing_run_pk`가 fencing token이다.

완료는 `workspace.persist_analysis_result_core()` 하나로 결과 materialisation과
`succeeded` 전이를 같은 transaction에서 수행한다. migration 24는 결과 없이 상태만
성공으로 바꾸던 과거 `complete_analysis_run` 함수를 제거한다. stale worker의 token은
`NULL`을 받고 결과를 변경하지 못한다.

현재 FastAPI 업로드 경로는 Storage write 뒤 queue row를 만드는 구조다. 다음 작업에서
DB `uploading` 예약 → Storage upload → `queued` finalize, 모호한 commit의 read-back,
stale `uploading` 복구/GC로 전환해야 한다.

## 벡터 검색

pgvector는 같은 PostgreSQL의 `retrieval` schema에 있다.

- provider/model: OpenAI `text-embedding-3-small`
- 차원/거리: 1,536 dimensions, cosine
- 영속 대상: Existing Profile의 `purpose`, `target`, `support`, `combined` 네 scope
- 요청 벡터: worker 메모리에서 생성·검색 후 폐기
- 입력 상한: scope당 8,192 tokens
- 초과 처리: Fact/줄 경계 chunk 후 token-weighted mean

Existing embedding 스크립트는 로컬 Profile의 exact byte SHA-256이 현재 DB Profile 및
연결된 structured artifact SHA와 같고 notice/source/schema identity도 일치할 때만 OpenAI
호출과 upsert를 수행한다.

## 인증과 접근

- Supabase Auth가 사용자 계정과 token을 발급한다.
- FastAPI가 Auth REST를 감싸고 access/refresh token을 HttpOnly Cookie로만 보관한다.
- 브라우저에는 Supabase key, PostgreSQL DSN, worker/OpenAI credential을 전달하지 않는다.
- Cookie 상태 변경은 정확한 Origin allow-list로 CSRF를 차단한다.
- 운영 기본은 API loopback bind와 `Secure=true`이며 LAN HTTP는 명시적 PoC 예외다.
- 세 Storage bucket은 모두 private다.

## 보존과 물리 정리의 현재 상태

| 영역 | 논리 정책 | 물리 삭제 자동화 |
|---|---|---|
| Existing KB | 버전 이력 영구 보존 | 삭제 대상 아님 |
| Workspace | `expires_at`, `cleanup_pending` 존재 | 미구현 |
| Result | 90일 read window, 만료 후 API read 차단 | 미구현 |
| Report | expiry column 존재 | 생성·정리 worker 미구현 |
| 감사 | `ops.cleanup_event` 존재 | 기록 scheduler 미구현 |

`ON DELETE CASCADE`는 DB 하위 행만 삭제하며 Storage 객체는 지우지 않는다. 향후 cleanup은
참조 중인 객체를 보호하면서 Storage 삭제, DB 삭제, 감사 기록과 실패 재시도를 함께
구현해야 한다. 그 전에는 “90일 뒤 자동 삭제”라고 운영 정책에 표시하지 않는다.

DB dump, Storage backup, 실제 `.env`, Docker volume은 Git에 올리지 않는다. migration 적용
전에는 `pg_dump -Fc`와 Storage backup을 만들고 복구 절차를 검증한다.
