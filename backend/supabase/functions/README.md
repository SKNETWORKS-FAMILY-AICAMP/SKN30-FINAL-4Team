# Supabase Edge Functions

프론트엔드가 직접 호출하는 쓰기 작업과 권한이 필요한 작업을 둔다.

## 책임 경계

- Edge Function: Supabase JWT 확인, 입력 검증, 작업 상태 전이, Storage signed URL 발급, 워커 작업 요청
- GPU worker: HWP/HWPX 파싱, Common IR, 구조화, 비교, 보고서 생성
- PostgreSQL `api` schema: 프론트엔드용 읽기 View와 RPC

Edge Function은 HWP/HWPX 처리나 LLM 호출을 직접 수행하지 않는다. 실행 시간과 GPU 의존성을 피하기 위해, 업로드 완료 후 `WorkerDispatch` 계약으로 외부 워커에 작업을 요청한다.

분석은 비동기다. Edge Function은 worker가 작업을 **접수**했다는 `202` 응답과 `worker_job_id`만 받고 즉시 프론트에 `queued` 상태를 돌려준다. worker가 실제 처리를 마치면 서비스 권한으로 Storage/DB의 `workspace`·`result` 데이터를 기록하고 `analysis_run` 상태를 변경한다. 프론트는 Realtime으로 그 변경을 수신한다.

## Worker dispatch seam

`_shared/worker-dispatch.ts`에는 아직 전송 방식을 결정하지 않은 공통 작업 계약만 있다.

향후 하나의 구현체를 선택한다.

1. RunPod endpoint에 HTTPS `POST`
2. 별도 queue/worker 서비스에 enqueue
3. 사내 GPU worker의 HTTPS endpoint에 `POST`

어떤 방식을 선택해도 Edge Function에는 `WorkerDispatchJob`을 넘기고 `WorkerDispatchResult`를 받는 코드만 남긴다. worker URL, 토큰, service-role key는 Git이나 브라우저에 두지 않고 Edge Function secret으로 설정한다.

현재는 `worker-http-dispatch.ts`가 다음 환경변수를 읽는 HTTP 어댑터만 제공한다. Function이 아직 이 어댑터를 호출하지는 않는다.

```text
ANALYSIS_WORKER_DISPATCH_URL=https://worker.example/jobs
ANALYSIS_WORKER_DISPATCH_TOKEN=<worker-shared-secret>
```

worker endpoint의 최소 계약은 다음이다.

```text
POST ANALYSIS_WORKER_DISPATCH_URL
Authorization: Bearer <ANALYSIS_WORKER_DISPATCH_TOKEN>
Content-Type: application/json

body: AnalysisWorkerJob
response: 202 { "worker_job_id": "..." }
```

`analysis_run_id` is also sent as the HTTP `Idempotency-Key`. The worker must
return the same `worker_job_id` for a duplicate key and must respond within
10 seconds. It is a trusted server-side component: its Supabase credentials
must never be usable by the browser and should be limited to the workspace,
result, ops, and Storage operations it needs.

## 예정 Function

```text
edge-analysis-run-create
edge-analysis-run-complete-upload
edge-analysis-run-ingest-request-profile
edge-conversation-create-message
edge-conversation-retry-message
edge-report-create-download-url
```

각 Function은 해당 화면/API 계약과 함께 추가한다. 미구현 Function을 배포하지 않는다.

## Local self-hosted deployment

The self-hosted stack mounts `volumes/functions` into the Edge Runtime; it
does not automatically read this repository directory. Copy the versioned
sources without deleting unrelated runtime functions, then restart only the
functions container:

```bash
backend/supabase/functions/deploy_local.sh \
  .runtime/supabase-dev/volumes/functions
cd .runtime/supabase-dev
docker compose restart functions
```

Before invoking analysis or chat functions, set the corresponding worker
dispatch URL/token as Edge Runtime secrets/environment variables. Analysis
uses `ANALYSIS_WORKER_DISPATCH_URL` and `_TOKEN`; conversation uses
`CONVERSATION_WORKER_DISPATCH_URL` and `_TOKEN`.
