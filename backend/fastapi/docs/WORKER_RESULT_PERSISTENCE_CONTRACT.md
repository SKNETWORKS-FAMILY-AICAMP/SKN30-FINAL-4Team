# 분석 worker 저장 계약

## 적용 범위

현재 worker는 FastAPI endpoint나 Edge Function callback을 호출하지 않는다. 같은 서버의
trusted process가 PostgreSQL polling queue를 claim하고 Supabase private Storage에서
원본을 읽은 뒤 DB function으로 결과를 완료한다.

```text
FastAPI upload
  → request-temp + workspace.analysis_run(status=queued)
  → worker PostgreSQL polling claim
  → source → Common IR → Request Profile → retrieval/CPL/FIT/SIM
  → fenced DB materialisation
  → FastAPI polling/result GET
```

worker에는 `DATABASE_URL`, `SUPABASE_URL`, private Storage 서버 자격증명,
`OPENAI_API_KEY`, `OPENAI_LLM_MODEL`, `OPENAI_EMBEDDING_MODEL` 및
`PREREVIEW_WORKER_*` 제어값만 server-side 환경변수로 제공한다. 브라우저 Cookie,
사용자 access token, anon key, Edge callback token은 worker 실행 계약에 포함하지
않는다. `PREREVIEW_FREETYPE_LIB`은 HWP/HWPX parser **subprocess**에만 주입하는
host library 경로이며 요청 payload가 아니다.

## Queue claim과 fencing

worker는 다음 trusted PostgreSQL function을 사용한다.

| 단계 | Function | 의미 |
|---|---|---|
| claim | `workspace.claim_next_analysis_run(worker_id, lease_seconds)` | `FOR UPDATE SKIP LOCKED`로 queued/stale run 하나를 점유하고 `processing_run_pk`를 받음 |
| heartbeat | `workspace.heartbeat_analysis_run(run_pk, processing_run_pk, lease_seconds)` | live lease 연장 |
| 성공 | `workspace.persist_analysis_result_core(run_pk, processing_run_pk, result_json)` | fence 확인과 결과/terminal 상태를 한 transaction으로 반영 |
| 실패 | `workspace.fail_analysis_run(run_pk, processing_run_pk, error_code, error_message)` | 첫 시도는 재queue, 두 번째는 terminal failed |

기본 heartbeat는 30초, lease는 120초, 최대 시도 횟수는 두 번이다. `processing_run_pk`는
시도별 fencing token이다. heartbeat/complete/fail은 반드시 claim에서 받은 동일 token을
보내야 한다. stale, expired, replacement token의 성공 저장은 `NULL`이며 어떠한 결과 행도
바꾸지 않는다.

`workspace.analysis_run`은 공개 상태만 보관한다. worker 전용 source/lease/attempt state는
`workspace.analysis_run_dispatch`, 실행 이력은 `ops.processing_run`에 있다.

## 중간 산출물

원본은 FastAPI가 생성한다. worker는 다음 파생 산출물을 private `request-temp`에 올리고
`workspace.source_artifact`에 artifact type, bucket, key, SHA-256, MIME, size,
schema version을 등록한다.

```text
source (FastAPI)
  → common_ir (worker)
      → structured_profile / Request Profile (worker)
```

두 transformation edge는 `workspace.artifact_lineage`에 남긴다. Request Profile의 관계형
projection은 `workspace.ingest_request_profile_core()`로 materialise한다. worker가 이 단계
전에 실패하면 `fail_analysis_run`만 호출하고 partial result를 `result.*`에 쓰지 않는다.

## `persist_analysis_result_core` payload

완료 함수는 다음 top-level object를 받는다. JSON은 NaN/Infinity/bytes 같은 비-JSON 값을
포함하면 안 된다.

```json
{
  "program_name": "요청 사업명",
  "axes": [{
    "axis_type": "CPL",
    "axis_code": "CPL-01",
    "status": "confirmed",
    "summary_text": "요약",
    "result_data": {}
  }],
  "candidates": [{
    "source_profile_id": "bizinfo:PBLN_...:hwp",
    "profile_version_pk": "실제로 retrieval/비교에 사용한 kb.profile_version UUID",
    "rank": 1,
    "similarity_score": 0.92,
    "priority_score": 0.87,
    "status": "similar",
    "summary_text": "요약",
    "comparable_axes": ["purpose", "target", "support"],
    "purpose_result": {}, "target_result": {},
    "support_result": {}, "delivery_result": {}
  }],
  "evidences": [{
    "axis_type": "CPL", "side": "REQUEST", "field_name": "purpose_goal",
    "candidate_source_profile_id": "EXISTING 근거일 때 같은 결과 후보의 선택 값",
    "raw_value": "근거 원문", "context_excerpt": "선택 문맥",
    "source_sha256": "64자리 SHA-256 또는 생략",
    "candidate_pack_block_id": "선택 값",
    "common_ir_document_id": "선택 값", "common_ir_block_id": "선택 값",
    "common_ir_cell_id": "선택 값", "common_ir_occurrence_ids": []
  }]
}
```

필수 규칙:

- `axes[*].axis_type`: `CPL`, `FIT`, `BEN`, `DIF` 중 하나이며 `axis_code`/`status`는 비어 있지 않음
- `candidates[*].source_profile_id`: Existing Profile의 논리 ID
- `candidates[*].profile_version_pk`: retrieval/LLM 비교에 실제 사용한 정확한 version UUID.
  DB는 이 UUID와 `source_profile_id`가 같은 lineage인지 확인하며 저장 시점의 current
  version으로 바꾸지 않음
- `candidates[*].rank`: 1 이상
- evidence `side`: `REQUEST` 또는 `EXISTING` (생략 시 `REQUEST`)
- `candidate_source_profile_id`를 보낼 때는 같은 payload의 candidate 중 하나여야 하며,
  candidate 상세 조회용 `sim_candidate_pk`와 Existing Profile version에 fail-closed로 연결됨

성공 함수는 `analysis_case_pk` UUID를 반환하며 아래를 **한 transaction**으로 만든다.

1. `result.analysis_case`와 `result.analysis_session`
2. `result.axis_result`, `result.sim_candidate`, `result.evidence_snapshot`
3. 현재 `ops.processing_run=succeeded`
4. dispatch lease 해제와 `workspace.analysis_run=succeeded`

현재 migration 23은 candidate별 evidence를 `candidate_source_profile_id`로 연결한다.
`result_data` 안의 evidence ID를 자동으로 다시 연결하지는 않는다.
worker serializer는 화면에 필요한 근거 스냅샷을 payload `evidences`로 넘긴다. ID 기반
cross-reference가 필요하면 이 함수/계약을 확장한 migration을 먼저 추가한다.

## 금지·레거시 경계

- migration 18의 `api.ingest_comparison_result_core`는 fenced하지 않으므로 새 worker가 사용하지 않는다.
- 퇴역한 `worker/jobs.py`·`worker/queue.py`·`worker/dispatcher.py`·`worker/persistence.py` 경로는 제거됐다. 현재 운영 진입점은 `worker.main`과 `worker.chat_main`이고, 저장은 이 문서의 `workspace.persist_analysis_result_core` 계약을 기준으로 한다. 옛 `sims.*` SQL은 git 이력에만 남는다.
- Edge Function signed URL/HTTP dispatch/callback 계약은 레거시 참고용이다.
- request 임베딩은 worker 메모리에서 생성·폐기한다. Existing 임베딩만 `retrieval.existing_profile_embedding`에 영속화한다.

## 결과 확인

FastAPI는 Cookie 사용자의 소유권을 확인한 뒤 `api` views/RPC로 결과를 읽는다.

- `GET /api/v1/analysis-runs/{run_id}`: queued/running/succeeded/failed polling
- `GET /api/v1/analysis-cases/{case_id}`: 전체 결과
- `GET /api/v1/sim-candidates/{candidate_id}`: 후보 상세

PDF와 채팅은 현재 이 worker 완료 계약에 넣지 않는다. 별도 job type/queue와 공개 API가
정의된 후 추가한다.
