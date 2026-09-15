# 레거시 GPU worker ↔ Supabase Edge API 계약 (비활성)

> 이 문서는 과거 외부 GPU worker HTTP dispatch/callback 설계의 기록이다. 현재
> production/development 경로에는 적용하지 않으며, 아래 Function·환경변수·인증 헤더를
> 배포하거나 설정하지 않는다. 현재 same-server polling worker 계약은
> [../../fastapi/docs/WORKER_RESULT_PERSISTENCE_CONTRACT.md](../../fastapi/docs/WORKER_RESULT_PERSISTENCE_CONTRACT.md)를
> 따른다.

과거 설계에서 worker는 PostgreSQL URL·DB 계정·Supabase service-role key를 받지 않았다. Edge가 전달한
signed URL로 파일을 읽고, worker 전용 Edge Function으로 Existing 정보를 조회·결과를 전송한다.

## 공통 인증

모든 worker API 요청에 아래 헤더를 보낸다.

```http
apikey: <Supabase anon key>
Authorization: Bearer <Supabase anon key>
x-worker-callback-token: <job.callback.callback_token>
Content-Type: application/json
```

`callback_token`은 dispatch token과 별도의 비밀값이다. URL·anon key·token은 작업 요청에서
확인하거나 worker의 비밀 설정으로 주입하며, 소스 코드나 로그에 저장하지 않는다.

## 1. 작업 접수

Edge가 worker의 `ANALYSIS_WORKER_DISPATCH_URL`로 보낸다.

```json
{
  "schema_version": "analysis_worker_job/v1",
  "analysis_run_id": "uuid",
  "user_id": "uuid",
  "source": {
    "bucket": "request-temp",
    "object_key": "request-source/.../source.hwp",
    "download_url": "짧은_유효기간의_signed_URL",
    "original_filename": "request.hwp",
    "content_type": "application/x-hwp",
    "size_bytes": 1234
  },
  "callback": {
    "ingest_request_profile_url": ".../edge-analysis-run-ingest-request-profile",
    "ingest_comparison_result_url": ".../edge-analysis-run-ingest-comparison-result",
    "existing_candidates_url": ".../edge-worker-existing-candidates",
    "existing_profile_url": ".../edge-worker-existing-profile",
    "callback_token": "비밀값"
  }
}
```

worker는 10초 안에 아래처럼 응답한다.

```json
{ "worker_job_id": "worker-unique-job-id" }
```

HTTP 상태는 `202`여야 한다.

## 2. Existing 후보 목록

```http
POST <existing_candidates_url>
```

요청은 선택적으로 pagination을 받는다.

```json
{ "limit": 100, "offset": 0 }
```

응답의 `portal_metadata`에는 공고명·지원분야·신청기간·기관 등 후보 선별용 메타데이터가 있다.

```json
{
  "candidates": [{
    "profile_version_id": "uuid",
    "source_profile_id": "bizinfo:PBLN_...:hwp",
    "notice_id": "PBLN_...",
    "source_kind": "hwp",
    "portal_metadata": {}
  }],
  "next_offset": null
}
```

## 3. Existing Profile 상세

```http
POST <existing_profile_url>

{ "source_profile_id": "bizinfo:PBLN_...:hwp" }
```

응답의 `structured_profile_download_url`은 Existing Profile JSON의 짧은 유효기간 signed URL이다.

```json
{
  "profile_version_id": "uuid",
  "source_profile_id": "bizinfo:PBLN_...:hwp",
  "notice_id": "PBLN_...",
  "portal_metadata": {},
  "schema_version": "existing_program_profile/v0.2",
  "structured_profile_download_url": "signed URL",
  "expires_in_seconds": 900
}
```

## 4. Request Profile callback

구조화가 끝나면 아래를 전송한다.

```http
POST <ingest_request_profile_url>

{
  "analysis_run_id": "uuid",
  "request_profile": { "schema_version": "pre_review_request_profile/v0.1" }
}
```

성공하면 run은 `running` 상태가 된다.

## 5. 비교 결과 callback

비교가 끝나면 아래를 전송한다. callback이 `result.*`에 기록하고 run을 `succeeded`로 바꾼다.

```http
POST <ingest_comparison_result_url>

{
  "analysis_run_id": "uuid",
  "comparison_result": {
    "program_name": "요청 사업명",
    "axes": [{
      "axis_type": "CPL",
      "axis_code": "purpose",
      "status": "matched",
      "summary_text": "요약",
      "result_data": {}
    }],
    "candidates": [{
      "source_profile_id": "bizinfo:PBLN_...:hwp",
      "rank": 1,
      "similarity_score": 0.92,
      "priority_score": 0.87,
      "status": "recommended",
      "summary_text": "추천 사유",
      "comparable_axes": ["purpose", "target"],
      "purpose_result": {},
      "target_result": {},
      "support_result": {},
      "delivery_result": {}
    }],
    "evidences": [{
      "side": "REQUEST",
      "field_name": "purpose",
      "raw_value": "근거 원문",
      "context_excerpt": "선택 사항"
    }]
  }
}
```

`source_profile_id`는 후보 목록에서 받은 값을 그대로 사용하는 계약이었다. 현재 worker는
이 callback을 쓰지 않고 DB queue를 claim하며, fenced DB function을 통해 결과와
`analysis_run` 상태를 한 transaction으로 변경한다.
