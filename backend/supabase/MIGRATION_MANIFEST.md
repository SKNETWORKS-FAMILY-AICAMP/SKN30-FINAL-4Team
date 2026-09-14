# Supabase migration manifest

마지막 감사: 2026-09-15
현재 버전: v0.2 (migration 41)

이 manifest는 `backend/supabase/migrations`의 순차 SQL과 현재
Frontend → FastAPI → Supabase, same-server PostgreSQL polling worker 구조를 설명한다.
브라우저는 Supabase Auth/Storage/PostgREST/Realtime/Edge Function을 직접 호출하지 않는다.

## 현재 수량

- 순차 migration: 41개 (`01`~`41`)
- SQL 물리 행 수: 11,503 (`wc -l`, 주석/빈 줄 포함)
- 애플리케이션 table: 64개
- data-bearing schema: 6개 (`app`, `ops`, `kb`, `workspace`, `result`, `retrieval`)
- contract schema: 1개 (`api`, table 없이 View/RPC)
- `CREATE [UNIQUE] INDEX` 정의: 88개

`auth`, `storage`, role(`anon`, `authenticated`, `service_role`)은 공식 Supabase stack이 먼저
제공해야 한다. migration 19 전에 pgvector가 포함된 호환 PostgreSQL image가 실행 중이어야
한다.

## Migration 파일

| # | 파일 | 행 | Table | Index | 현재 의미 |
|---:|---|---:|---:|---:|---|
| 01 | `01_core_schemas.sql` | 27 | 0 | 0 | application schema 생성 |
| 02 | `02_core_ddl.sql` | 553 | 26 | 3 | app/ops/Existing KB core |
| 03 | `03_workspace_ddl.sql` | 97 | 4 | 0 | analysis run, source/profile artifact root |
| 04 | `04_workspace_components.sql` | 114 | 5 | 1 | request component/fact projection |
| 05 | `05_workspace_projections.sql` | 233 | 14 | 0 | target/support/delivery/field projection |
| 06 | `06_result_ddl.sql` | 166 | 8 | 0 | result, session, conversation/report table |
| 07 | `07_indexes.sql` | 206 | 0 | 60 | base FK/query index |
| 08 | `08_rls_policies.sql` | 250 | 0 | 0 | RLS와 과거 direct-client read grant |
| 09 | `09_kb_notice_metadata.sql` | 9 | 0 | 0 | Bizinfo portal metadata |
| 10 | `10_api_contract_foundation.sql` | 70 | 0 | 2 | `api` schema와 초기 상태 제약 |
| 11 | `11_storage_policies.sql` | 29 | 0 | 0 | private bucket 생성, legacy broad upload/delete policy 제거 |
| 12 | `12_realtime_analysis_run.sql` | 21 | 0 | 0 | 과거 Realtime publication; 활성 UI는 polling |
| 13 | `13_api_contract_state_hardening.sql` | 151 | 1 | 2 | private dispatch metadata와 상태 hardening |
| 14 | `14_storage_upload_hardening.sql` | 135 | 0 | 0 | 50 MiB/reservation Storage policy hardening |
| 15 | `15_api_views_and_result_rpcs.sql` | 298 | 0 | 0 | owner-scoped read View/RPC |
| 16 | `16_conversation_command_rpcs.sql` | 145 | 0 | 0 | 과거 chat command; chat runtime 미구현 |
| 17 | `17_request_profile_ingest_core.sql` | 193 | 0 | 0 | worker Request Profile materialisation |
| 18 | `18_worker_existing_api_and_result_ingest.sql` | 222 | 0 | 0 | 과거 Edge read/unfenced ingest 계약 |
| 19 | `19_pgvector_existing_profile_retrieval.sql` | 132 | 2 | 2 | Existing Profile embedding/config와 cosine match |
| 20 | `20_embedding_input_policy_and_axis_match.sql` | 173 | 0 | 0 | 8,192-token input policy, three-axis match; replay-safe v1 bootstrap |
| 21 | `21_analysis_worker_queue.sql` | 495 | 0 | 2 | polling claim, lease, heartbeat, two-attempt retry/fence |
| 22 | `22_fenced_analysis_result_ingest.sql` | 194 | 0 | 0 | fenced atomic result ingest, unfenced writer 폐기 |
| 23 | `23_result_read_retention_and_candidate_evidence.sql` | 449 | 0 | 0 | live-retention reads, exact candidate version/evidence |
| 24 | `24_retire_legacy_worker_completion.sql` | 10 | 0 | 0 | 결과 없는 성공 전이를 허용한 과거 완료 함수 제거 |
| 25 | `25_queued_source_invariant.sql` | 669 | 0 | 1 | queued source artifact·dispatch·lease·processing fence 무결성 강제 |
| 26 | `26_ml_result_contract.sql` | 167 | 0 | 0 | fenced result ingest에 Model 1/2/3 public reference를 원자 저장; ledgerless replay-safe wrapper |
| 27 | `27_chat_worker_queue.sql` | 809 | 1 | 2 | result-grounded asynchronous conversation queue/lease/fence |
| 28 | `28_component_name_embedding_assembly.sql` | 40 | 0 | 0 | support component name 포함 v2 임베딩 조립 구성 staged 생성 |
| 29 | `29_repair_component_embedding_activation.sql` | 117 | 0 | 0 | 빈 active v2 보정; inactive v2/active future config 보존 |
| 30 | `30_serialise_existing_kb_embedding_activation.sql` | 308 | 0 | 0 | four-table writer lock·최초 설치/trigger drift one-time revalidation, current/입력 child 변경 시 v1 demote |
| 31 | `31_existing_profile_model1_classification.sql` | 450 | 2 | 4 | Model 1 base schema와 final-runtime replay compatibility |
| 32 | `32_existing_profile_model1_classification_hardening.sql` | 371 | 0 | 1 | immutable runtime identity·retryable KB invalidation·complete promotion·service projection hardening |
| 33 | `33_v02_lifecycle_and_fastapi_boundary.sql` | 1057 | 0 | 4 | owner-scoped upload/current/history/close lifecycle와 stale reconciliation |
| 34 | `34_v02_public_result_projection_and_evidence.sql` | 1266 | 0 | 2 | typed public result/candidate projection과 evidence integrity |
| 35 | `35_v02_partial_axis_retrieval.sql` | 155 | 0 | 0 | 1~3 genuine axis partial retrieval; zero vector 금지 |
| 36 | `36_v02_conversation_idempotency_and_claim.sql` | 793 | 1 | 3 | chat idempotency, cursor history, lease/fenced claim |
| 37 | `37_v02_global_queue_admission.sql` | 552 | 0 | 1 | analysis upload와 chat create/retry 공용 전역 queue cap/backpressure |
| 38 | `38_model1_runtime_manifest_refresh.sql` | 39 | 0 | 0 | 현재 Model 1 runtime manifest를 새 inactive immutable configuration으로 등록; 과거 분류 row 보존 |
| 39 | `39_v02_atomic_upload_finalization.sql` | 201 | 0 | 0 | 만료 upload finalise도 전역 admission lock 아래 re-admit; full이면 cleanup_pending source key 반환 |
| 40 | `40_v02_embedding_execution_provenance.sql` | 110 | 0 | 0 | analysis attempt가 실제 선택한 embedding configuration/zero-axis null snapshot을 fenced processing metadata에 영속 |
| 41 | `41_model1_runtime_manifest_refresh_v4.sql` | 27 | 0 | 0 | 공용 ML runtime 변경을 반영한 Model 1 v4 identity를 inactive로 등록; 기존 활성 설정·분류 row 보존 |

합계는 64 table, 88 index다. SQL 파일이 바뀌면 이 표의 행 수도 함께 갱신하되, 행 수는
스키마 정확성을 대신하는 검증이 아니다.

## Schema와 활성 계약

| Schema | Table | Lifecycle/접근 |
|---|---:|---|
| `app` | 1 | 사용자 profile. RLS own-row read |
| `ops` | 3 | worker attempt/model/cleanup 감사 구조. browser 미노출 |
| `kb` | 22 | versioned Existing KB. 영속 |
| `workspace` | 25 | request 분석과 private analysis/chat queue state |
| `result` | 8 | 분석 결과/session/conversation/report 구조 |
| `retrieval` | 4 | Existing embedding과 versioned Model 1 분류만 영속; request vector는 worker 메모리에서 폐기 |
| `api` | 0 | FastAPI가 transaction-local user claim으로 호출하는 read View/RPC |

활성 분석 lifecycle은 다음과 같다.

```text
FastAPI uploading 예약 → private Storage upload → source artifact + queued 원자 전이
  → workspace.claim_next_analysis_run (FOR UPDATE SKIP LOCKED)
  → heartbeat/lease + processing_run_pk fence
  → artifact/Request Profile + ephemeral request embedding
  → workspace.persist_analysis_result_core_v2
  → result materialisation과 succeeded 전이를 한 transaction으로 commit
```

기본 heartbeat는 30초, lease는 120초, 최대 시도는 2회다. stale/expired/replaced
`processing_run_pk`로 완료하면 `NULL`을 반환하고 결과를 변경하지 않는다. 후보 결과는
논리 `source_profile_id`와 retrieval에 실제 사용한 `profile_version_pk`를 함께 검증한다.

## 남아 있는 legacy DB surface

migration은 append-only 이력이라 다음 객체가 물리적으로 남아 있다. 존재한다고 해서 활성
애플리케이션 경로는 아니다.

- migration 08/13~15의 authenticated direct-read grant/RLS
- migration 11/14의 authenticated direct Storage upload/delete policy
- migration 12의 Realtime publication
- migration 16의 conversation command
- migration 18의 Edge worker read 함수와 unfenced ingest 함수

migration 22가 `api.ingest_comparison_result_core`의 `service_role` 실행 권한을 회수한다.
analysis worker는 migration 21~26의 DB queue/fenced result·ML reference path를, chat worker는
migration 27의 conversation queue/fence path를 사용한다. browser JavaScript에는
Supabase key나 token 응답을 전달하지 않고 access/refresh token은 HttpOnly Cookie에만 둔다.
FastAPI만 공개 업무 API로 사용한다.

## Retention과 cleanup의 정확한 상태

- 성공 결과의 `retention_expires_at`은 현재 SQL에 **90일로 고정**되어 있다.
- migration 23의 history/result/candidate read는 만료 시각 이후 즉시 fail-closed한다.
- `analysis_session`은 30분 expiry를 기록한다.
- workspace/result/Storage object를 물리 삭제하는 scheduler/GC는 아직 구현되지 않았다.
- `ops.cleanup_event`, `cleanup_pending`과 expiry column은 cleanup 메타데이터일 뿐 자동 삭제를
  수행하지 않는다.

따라서 “90일 뒤 자동 삭제”, “실패 run 즉시 정리”, “session 종료 시 workspace 삭제”라고
운영 문서나 개인정보 고지에 주장하면 안 된다. reference-aware cleanup job, 감사 기록,
Storage/DB 삭제 순서와 복구 정책을 구현·검증한 뒤에만 물리 보존 기한을 보장한다.

## 적용 절차

지원되는 적용 경로는 repository script 하나다. `supabase migration up`은 이 저장소에
Supabase CLI migration project/timestamp 형식이 없으므로 사용하지 않는다. 수동 `psql`
loop는 `ON_ERROR_STOP`, Compose 위치와 적용 순서 실수를 만들 수 있으므로 문서화된 경로가
아니다.

사전 조건:

1. 공식 self-hosted Supabase release와 pgvector DB image 조합을 staging에서 고정·검증한다.
2. Auth/Storage가 초기화되고 DB가 healthy인지 확인한다.
3. 실제 DB/Storage volume 경로와 container UID/GID 쓰기 권한을 확인한다.
4. `pg_dump -Fc`와 Storage backup을 만들고 복구 절차를 시험한다.
5. API/worker를 drain하고 적용할 Git SHA를 기록한다.

```bash
SUPABASE_DIR=/srv/pre-review/supabase \
  /path/to/repository/backend/supabase/apply_migrations.sh
```

이 script에는 migration ledger가 없으며 매번 `01`~`41`을 모두 실행한다. 각 파일은 독립
transaction이라 중간 실패 전 파일은 이미 commit된다. reset/delete는 하지 않지만 모든
부분 적용·재실행 상태가 안전하다고 보장하지도 않는다. 실패 시 무작정 재실행하지 말고
적용된 객체와 오류 migration을 확인한 뒤 backup restore 또는 검증된 repair 절차를 따른다.

migration 11이 `existing-kb`, `request-temp`, `analysis-reports`를 private bucket으로 만든다.
별도 curl로 중복 생성하지 않는다.

## 검증

정적 계약 test는 하나의 pytest entry가 내부 20개 schema/RLS/queue/fencing 조건을 검사한다.
SQL을 실제 PostgreSQL에서 실행하거나 container/network/Storage/OpenAI를 검증하지는 않는다.

```bash
cd /path/to/repository/backend
UV_CACHE_DIR=/tmp/pre_review_uv_cache uv run --extra dev \
  pytest supabase/tests/test_migration_contract.py -q
```

queue runtime SQL은 transaction 안에서 claim/heartbeat/retry/fence를 검사하고 rollback한다.

```bash
cd /path/to/repository/backend
SUPABASE_DIR=/path/to/supabase-compose \
  ./supabase/run_worker_queue_validation.sh
```

배포 gate에는 별도로 migration 01~41 fresh apply, 기존 migration `01`~`32` 상태에서의
upgrade/replay, migration 41 Model 1 전체 재분류·승격, 실제 private Storage put/get/delete,
FastAPI Cookie auth/upload/poll/result, HWP/HWPX parser와 OpenAI를 포함한 worker E2E가 필요하다.

## 관련 문서

- [Self-hosted Supabase 운영 안내](README.md)
- [Worker PostgreSQL 접근 경계](WORKER_DB_ACCESS.md)
- [레거시 Edge Functions](functions/README.md)
- [현재 worker 결과 저장 계약](../fastapi/docs/WORKER_RESULT_PERSISTENCE_CONTRACT.md)
- [FastAPI·worker runbook](../fastapi/docs/FASTAPI_WORKER_RUNBOOK.md)
