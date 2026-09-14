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
| 성공 | `workspace.persist_analysis_result_core_v2(run_pk, processing_run_pk, result_json)` | fence 확인, migration 26 ML writer, raw/public result와 terminal 상태를 한 transaction으로 반영 |
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

## `persist_analysis_result_core_v2` payload

완료 함수는 다음 top-level object를 받는다. JSON은 NaN/Infinity/bytes 같은 비-JSON 값을
포함하면 안 된다.

```json
{
  "contract_version": "analysis_result/v0.2",
  "program_name": "요청 사업명",
  "sim": {"status": "completed", "reason_code": null, "summary": "유사 공고 검색을 완료했습니다."},
  "axes": [{
    "axis_type": "CPL",
    "axis_code": "CPL-01",
    "status": "confirmed",
    "summary_text": "요약",
    "result_data": {"raw_fact_id": "internal only"},
    "public_detail": {
      "reason_code": null,
      "reason": "원문에서 확인했습니다.",
      "values": [{"label": "지원 대상", "value": "부산 소재 중소기업", "evidence_ids": ["uuid"]}],
      "source_fields": ["support_target"],
      "evidence_ids": ["uuid"]
    }
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
    "metadata": {"title": "분석 시점 공고명", "support_field": null, "apply_period": "2026-09-01 ~ 2026-09-30", "ministry": null, "executing_agency": null, "registered_at": null, "notice_status": null, "source_url": null},
    "purpose_result": {"raw_fact_ids": ["internal only"]}, "target_result": {},
    "support_result": {}, "delivery_result": {},
    "public_axes": {
      "purpose": {"code": "SIM-1", "status": "similar", "summary": "공통점이 확인되었습니다.", "reason_code": null, "reason": "공통점이 확인되었습니다.", "common_points": [], "differences": [], "request_evidence_ids": ["uuid"], "existing_evidence_ids": ["uuid"]},
      "target": {}, "support": {}, "delivery": {}
    }
  }],
  "evidences": [{
    "evidence_id": "worker UUIDv5",
    "logical_code": "CPL-01", "axis_type": "CPL", "sim_axis": null,
    "role": "VALUE", "side": "REQUEST", "field_name": "purpose_goal",
    "candidate_source_profile_id": null, "raw_value": "근거 원문",
    "source_identity": "internal source/fact identity",
    "source_sha256": "64자리 SHA-256 또는 생략",
    "candidate_pack_block_id": "선택 값",
    "common_ir_document_id": "선택 값", "common_ir_block_id": "선택 값",
    "common_ir_cell_id": "선택 값", "common_ir_occurrence_ids": []
  }]
}
```

필수 규칙:

- top-level `contract_version`은 정확히 `analysis_result/v0.2`이며 `ml`은 migration 26 호환을 위해 기존 형태로 함께 보낸다.
- `axes[*].axis_type`: `CPL` 또는 `FIT`이며 `axis_code`/`status`는 비어 있지 않음. `result_data`는 raw audit 전용이고 `public_detail`만 Browser/chat projection에 쓴다.
- CPL public detail은 `reason_code`, `reason`, typed `values`, `source_fields`, `evidence_ids`만 가진다. FIT public detail은 `comparison_performed`, 양쪽 요약/evidence IDs만 가진다. FIT-4 계층 자체가 없으면 `NOT_APPLICABLE`; 계층 비교 대상은 있으나 근거가 부족하면 `INSUFFICIENT`다.
- `candidates[*].source_profile_id`: Existing Profile의 논리 ID
- `candidates[*].profile_version_pk`: retrieval/LLM 비교에 실제 사용한 정확한 version UUID.
  DB는 이 UUID와 `source_profile_id`가 같은 lineage인지 확인하며 저장 시점의 current
  version으로 바꾸지 않음
- `candidates[*].rank`: 1 이상
- evidence는 worker가 UUIDv5로 선발급한다. namespace input은 analysis run, logical code, role/side, source identity와 원문 좌표를 포함한다. duplicate/dangling/cross-case/context mismatch는 DB가 거부한다.
- CPL은 `VALUE/REQUEST`, FIT은 `LEFT|RIGHT/REQUEST`, SIM은 `LEFT/REQUEST`와 `RIGHT/EXISTING`만 쓴다. SIM evidence는 candidate와 `sim_axis`에 연결되며 전체 결과 evidence 목록에 섞이지 않는다.
- `similarity_score`, `priority_score`, raw fact IDs, diagnostics, source identity와 내부 좌표는 raw 저장만 하며 public RPC/chat context에서 절대 반환하지 않는다.
- retrieval은 0/1/2/3 available axes를 보낸다. 0축은 `sim.status=skipped`, `RETRIEVAL_INPUT_MISSING`으로 CPL/FIT/ML만 저장한다. 1~3축은 zero vector 없이 `|A|` 평균으로 retrieval하며 optional KB empty는 `KB_EMPTY` completed다.

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
- 퇴역한 `worker/jobs.py`·`worker/queue.py`·`worker/dispatcher.py`·`worker/persistence.py` 경로는 제거됐다. 현재 운영 진입점은 `worker.main`과 `worker.chat_main`이고, 저장은 이 문서의 `workspace.persist_analysis_result_core_v2` 계약을 기준으로 한다. 옛 `sims.*` SQL은 git 이력에만 남는다.
- Edge Function signed URL/HTTP dispatch/callback 계약은 레거시 참고용이다.
- request 임베딩은 worker 메모리에서 생성·폐기한다. Existing 임베딩만 `retrieval.existing_profile_embedding`에 영속화한다.

## 결과 확인과 별도 채팅 계약

migration 37은 FastAPI의 새 analysis upload reservation과 chat create/retry가 동일한
PostgreSQL advisory lock과 `PREREVIEW_GLOBAL_QUEUE_MAX` cap을 사용하도록 한다. cap에
도달한 새 작업은 `503`으로 거부되며, 정확한 idempotency replay는 기존 작업을 반환한다.

FastAPI는 Cookie 사용자의 소유권을 확인한 뒤 `api` views/RPC로 결과를 읽는다.

- `GET /api/v1/analysis-runs/{run_id}`: queued/running/succeeded/failed polling
- `GET /api/v1/analysis-cases/{case_id}`: 전체 결과
- `GET /api/v1/sim-candidates/{candidate_id}`: 후보 상세

채팅은 analysis-result 완료 transaction에 섞지 않는 **별도** job type이다. FastAPI의
`POST /api/v1/analysis-cases/{case_id}/messages`, `GET .../messages`, retry route가
assistant row를 만들거나 조회하고, migration 36의
`workspace.claim_next_conversation_message_v2()`와 v2 lease/fencing transition을 chat
worker가 사용한다. 이 claim은 Browser 결과와 같은 public projection의 evidence
allow-list를 제공하며 raw `result_data`, fact id, diagnostics, ranking score를 chat prompt에
넣지 않는다. 완료 시에는 결과 근거에 연결된 `result.conversation_reference`만 저장한다.
구현 경계는 `app/api/v1/conversations.py`, `worker/chat_main.py`,
`worker/postgres_chat_repository.py`, `worker/chat/handler.py`다.

2026-09-13 합성 HWPX live E2E(run
`5e51dae9-3c6e-4ed8-b4c6-96185917b08b`, case
`2d02ae97-85f0-4678-a9fe-e006ab389bd1`)에서 analysis worker와 chat worker는 각각 한 번의
attempt로 완료했고, 채팅 reference 13개를 확인했다. 같은 run에서 저장된 ML 1/2/3 상태도
모두 `OK`였다. 이는 합성 HWPX 한 건의 범위이며 실제 Hancom 작성 HWP/HWPX의 완전 재검증은
아직 완료되지 않았다.

PDF/OCR과 PDF 생성은 이 계약 및 현재 E2E 완료 범위에 포함하지 않는다.
