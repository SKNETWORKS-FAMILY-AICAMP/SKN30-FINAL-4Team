# Worker 연동 구현 현황

마지막 확인일: 2026-09-10
적용 경로: `backend-rebuild`의 FastAPI + same-server polling worker

## 결론

기존 worker의 Common IR·Request Profile·CPL/FIT/SIM 도메인 로직을 보존하고, 입출력
경계는 현재 Supabase 스키마에 맞춘 trusted adapter로 교체했다. 현재 worker는 FastAPI,
Edge Function callback 또는 외부 worker HTTP 서버를 호출하지 않는다.

```text
Browser
  → FastAPI (Supabase Auth Cookie 검증, upload/poll/result read)
  → request-temp + workspace.analysis_run(status=queued)
  → PostgreSQL polling worker
      claim/heartbeat (processing_run_pk fence)
      → HWP/HWPX → Common IR → Request Profile
      → OpenAI embeddings + pgvector Existing retrieval
      → CPL/FIT/SIM → fenced result materialisation
  → FastAPI result read
```

## 현재 구현 경계

| 영역 | 현재 구현 | 기준 파일 |
|---|---|---|
| FastAPI upload | HWP/HWPX MIME·magic·50 MiB 검사, `Idempotency-Key` 기반 `uploading` 예약, private Storage upload, source artifact+`queued` 원자 확정, stale cleanup 재시도 | `app/api/v1/analysis_runs.py`, `app/services/analysis_runs.py` |
| queue | PostgreSQL `FOR UPDATE SKIP LOCKED`, 30초 heartbeat, 120초 lease, 최대 2 attempts | `supabase/migrations/21_analysis_worker_queue.sql`, `worker/postgres_repository.py` |
| worker runtime | 상주 polling, SIGTERM graceful stop, 별도 DB connection heartbeat, stale fence 차단 | `worker/runtime.py`, `worker/main.py` |
| source/profile | private Storage download/hash 확인, Common IR·Request Profile upload, source → Common IR → Profile lineage와 request projection 등록 | `worker/analysis_job.py`, `worker/postgres_analysis_store.py`, `worker/supabase_storage.py` |
| retrieval | OpenAI `text-embedding-3-small` 1,536 dimensions, request 3축 임시 embedding, Existing persistent vector 3축 match | `worker/retrieval_inputs.py`, `worker/postgres_analysis_store.py` |
| result | exact Existing profile version을 포함한 CPL/FIT/SIM/evidence payload를 fenced transaction으로 저장 | `worker/result_payload.py`, `supabase/migrations/23_result_read_retention_and_candidate_evidence.sql` |
| browser read | Cookie 소유권 확인 후 `api` view/RPC를 authenticated role로 조회 | `app/api/v1/results.py`, `app/infrastructure/postgres_results.py` |

`ops.processing_run`은 attempt별 이력·fencing token이고, 공개 상태는
`workspace.analysis_run`, 점유/lease/source 위치는 `workspace.analysis_run_dispatch`다.
worker가 브라우저 access token·Cookie·anon key를 받거나 DB base table을 브라우저에
노출하는 경로는 없다.

## 검증 완료 범위

- backend 회귀: 173 tests
- 합성 HWPX 5건: ZIP/manifest SHA-256/Common IR provenance/본문 보존/request type preflight
  모두 통과
- 실제 1건: Supabase Auth → FastAPI upload → queue → HWPX/Common IR/Request Profile →
  OpenAI embedding/pgvector → CPL/FIT/SIM → fenced result → FastAPI polling/read 성공
- Docker build 및 network 없는 container의 173개 회귀·합성 parser 실행 성공

합성 HWPX는 양식을 흉내 낸 파일로 현재 파서에서 각 2개 텍스트 블록으로 평탄화된다.
실제 Hancom 작성 문서, malformed/timeout 문서는 아직 별도 E2E 범위다.

## 사용하면 안 되는 레거시 경로

- `backend/supabase/functions/`의 Edge Function dispatch/callback 및 signed URL 흐름
- migration 18의 `api.ingest_comparison_result_core`: fencing이 없어 service-role 실행이
  회수되었으며 새 worker에서 사용 금지
- `worker/jobs.py`, `worker/queue.py`, `worker/dispatcher.py`, `worker/persistence.py`의
  옛 `sims.*` queue/result SQL
- mount하지 않은 `app/api/v1/routes.py`의 in-memory `/requests`·`/cases` 예전 계약

## 남은 운영·기능 작업

- password recovery link를 HttpOnly Cookie 세션으로 교환하는 callback/PKCE
- reverse proxy/ASGI의 multipart 전체 body·part 수 제한과 streaming upload
- `request-temp`·90일 만료 결과의 reference-aware cleanup 및 감사. 업로드 요청에 묶인
  stale lazy reaper는 별도 scheduler·cleanup lease로 분리
- worker heartbeat/queue lag를 포함한 readiness
- `ops.model_invocation` 단위 OpenAI 호출 감사
- 일부 purpose/target/support 축이 비었을 때 fail 대신 insufficient 결과로 처리할 정책
- PDF/OCR, PDF 보고서, 채팅: 별도 queue와 공개 API를 정의한 뒤 추가
