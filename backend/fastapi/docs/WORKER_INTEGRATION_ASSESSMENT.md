# Worker 연동 구현 현황

마지막 확인일: 2026-09-15
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
      → CPL/FIT/SIM + ML 1/2/3 → fenced result materialisation
  → FastAPI result read
  → FastAPI conversation API → PostgreSQL chat queue → result-grounded chat worker
```

## 현재 구현 경계

| 영역 | 현재 구현 | 기준 파일 |
|---|---|---|
| FastAPI upload | HWP/HWPX MIME·magic·50 MiB 검사, `PREREVIEW_HTTP_MAX_BODY_BYTES` 전체 multipart cap, `Idempotency-Key` 기반 `uploading` 예약, private Storage upload, source artifact+`queued` 원자 확정, stale cleanup 재시도 | `app/api/v1/analysis_runs.py`, `app/services/analysis_runs.py`, `app/middleware/request_body_limit.py` |
| queue | PostgreSQL `FOR UPDATE SKIP LOCKED`, 30초 heartbeat, 120초 lease, 최대 2 attempts, migration 37 analysis/chat 공용 admission cap과 full 시 503 | `supabase/migrations/21_analysis_worker_queue.sql`, `supabase/migrations/37_v02_global_queue_admission.sql`, `worker/postgres_repository.py` |
| worker runtime | 상주 polling, SIGTERM graceful stop, 별도 DB connection heartbeat, stale fence 차단 | `worker/runtime.py`, `worker/main.py` |
| source/profile | private Storage download/hash 확인, Common IR·Request Profile upload, source → Common IR → Profile lineage와 request projection 등록 | `worker/analysis_job.py`, `worker/postgres_analysis_store.py`, `worker/supabase_storage.py` |
| retrieval | OpenAI `text-embedding-3-small` 1,536 dimensions, request 3축 임시 embedding, Existing persistent vector 3축 match | `worker/retrieval_inputs.py`, `worker/postgres_analysis_store.py` |
| ML result | Model 1/2/3 adapter 결과를 analysis result와 함께 저장하고, 공개 결과에는 서버가 조립한 안전한 message만 projection | `worker/analysis_job.py`, `worker/result_payload.py`, `supabase/migrations/26_ml_result_contract.sql` |
| result | exact Existing profile version을 포함한 CPL/FIT/SIM/evidence payload를 fenced transaction으로 저장 | `worker/result_payload.py`, `supabase/migrations/23_result_read_retention_and_candidate_evidence.sql` |
| browser read | Cookie 소유권 확인 후 `api` view/RPC를 authenticated role로 조회 | `app/api/v1/results.py`, `app/infrastructure/postgres_results.py` |
| chat | message create/list/retry API, 별도 lease/fencing queue, 결과 근거 기반 LLM answer와 reference 저장 | `app/api/v1/conversations.py`, `worker/chat_main.py`, `worker/postgres_chat_repository.py`, `supabase/migrations/27_chat_worker_queue.sql` |

`ops.processing_run`은 attempt별 이력·fencing token이고, 공개 상태는
`workspace.analysis_run`, 점유/lease/source 위치는 `workspace.analysis_run_dispatch`다.
worker가 브라우저 access token·Cookie·anon key를 받거나 DB base table을 브라우저에
노출하는 경로는 없다.

## Profile·ML 용어 경계

Request/Existing Profile JSON이 업무 용어와 사실·근거의 기준이다. 사용자에게
노출할 지원 금액·한도는 `derived_projections.support_scale_measures`의 검증된
Profile projection에서만 읽는다. 현재 Model 2 학습·serving 코드의 `stated_cap`,
`budget_div_count` 같은 표현은 동결된 모델 내부 용어이며 API·DB 도메인
계약이 아니다. ML subprocess adapter는 이 진단 envelope를 공개 결과로
승격시키지 않는다.

Model 2의 DB 저장 payload는 기존 `status`, `predicted_amount_won`, `message`,
`reason_code` 계약을 유지한다. FastAPI 공개 응답은 기존처럼 `message`만
직렬화하므로 프론트 JSON 필드는 바뀌지 않는다. Profile에 유일하고
검증된 기업·과제·팀당 한도가 있을 때만 그 범위를 문구에 표시하고,
없거나 충돌하면 예측 금액만 중립적인 `지원 단위당`으로 표시한다.
향후 ML을 재학습·교체할 때도 외부 계약은 유지하고 Profile 용어로 입력·라벨·
feature 정의를 재정렬한다.

## 검증 완료 범위

- 전체 backend 회귀 테스트 통과
- 합성 HWPX 5건: ZIP/manifest SHA-256/Common IR provenance/본문 보존/request type preflight
  모두 통과
- 2026-09-13 합성 HWPX live E2E: run `5e51dae9-3c6e-4ed8-b4c6-96185917b08b`, case
  `2d02ae97-85f0-4678-a9fe-e006ab389bd1`에서 Supabase Auth → FastAPI upload → analysis
  queue → HWPX/Common IR/Request Profile(Terra) → ML 1/2/3 모두 `OK` → embedding/pgvector
  → CPL 13/FIT 7/SIM 후보 1/evidence 77 → fenced result/FastAPI read → chat(Luna) 완료와
  reference 13 저장을 확인했다. analysis worker와 chat worker는 각각 한 번의 attempt로
  완료했다.
- Docker build 및 network 없는 container의 전체 회귀·합성 parser 실행 성공
- Request 의미 누락, 지원규모 단위 범위, CPL component 보조 근거, FIT 공개 근거 계약을
  회귀 테스트로 고정했다. 최신 backend 1,083개 테스트가 통과했고, `mockup_08` HWPX는
  최신 이미지의 network 없는 parser subprocess에서도 통과했다.

기존 합성 fixture 5건은 양식을 흉내 낸 파일이며 당시 각 2개 텍스트 블록으로
평탄화됐다. 위 live E2E의 `mockup_08`은 별도의 CPL 전항목 합성 fixture이고 Common IR
6개 block으로 파싱됐다. 이 결과는 합성 HWPX 한 건의 검증이다. 실제 Hancom 작성 HWP/HWPX의 완전 재검증과
malformed/timeout 문서 E2E는 아직 별도 범위다.

## 사용하면 안 되는 레거시 경로

- `backend/supabase/functions/`의 Edge Function dispatch/callback 및 signed URL 흐름
- migration 18의 `api.ingest_comparison_result_core`: fencing이 없어 service-role 실행이
  회수되었으며 새 worker에서 사용 금지
- 퇴역한 `worker/jobs.py`·`worker/queue.py`·`worker/dispatcher.py`·`worker/persistence.py`
  경로는 제거됐다. 현재 운영 진입점은 `worker.main`·`worker.chat_main`이고 큐·저장은
  `workspace.*` fenced RPC 계약을 쓴다. 옛 `sims.*` SQL은 git 이력에만 남는다
- mount하지 않은 `app/api/v1/routes.py`의 in-memory `/requests`·`/cases` 예전 계약

## 남은 운영·기능 작업

- multipart part 수 제한과 streaming upload
- `request-temp`·90일 만료 결과의 reference-aware cleanup 및 감사. 업로드 요청에 묶인
  stale lazy reaper는 별도 scheduler·cleanup lease로 분리
- Request assembler는 검증된 `support_scale` Raw Fact에서 결정적으로
  `support_scale_measures`를 생성한다(`request_profile_v0.1.5`,
  `numeric_candidate_v2`). 각 숫자는 자기 Raw-Fact 내부의 exact locator와
  candidate-local 한도·범위 문법으로 검증된다. 원자적 금액 span 바로 앞의 같은
  source block에 닫힌 문법의 `기업당/과제당/프로젝트당/팀당 ... 한도:` 라벨이
  붙은 경우에만 그 범위를 보존한다. 명시적인 COMPANY/PROJECT/TEAM
  단위 상한만 Model 2의 원문 한도 표시에 사용하고, PERSON/TOTAL·그 외 값은
  Profile provenance에는 보존하되 해당 표시에는 승격하지 않는다
- worker heartbeat/queue lag를 포함한 readiness
- `ops.model_invocation` 단위 OpenAI 호출 감사
- 일부 purpose/target/support 축이 비었을 때 fail 대신 insufficient 결과로 처리할 정책
- PDF/OCR 및 PDF 보고서 생성: 별도 구현·E2E 검증 필요
