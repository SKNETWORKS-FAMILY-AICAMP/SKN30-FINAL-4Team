# 백엔드 통합 변경 명세 v0.2

- 상태: 구현 기준선
- 기준 코드: `origin/backend-rebuild@795d0af`
- 통합 기준: `origin/develop@38cbab3` 포함
- 공개 경계: Browser → FastAPI
- 내부 구성: Supabase Auth·Storage·PostgreSQL·pgvector + 동일 서버 DB queue worker

## 1. 범위와 원칙

1. Browser는 업무 데이터·Storage·worker를 직접 호출하지 않고 FastAPI만 호출한다.
2. FastAPI는 Supabase Auth를 HttpOnly cookie로 감싸고, Storage와 PostgreSQL에는 서버 자격으로 접근한다.
3. worker는 동일 서버의 trusted process이며 기존 DB queue, lease, fenced persistence를 유지한다.
4. frontend 소스는 이번 변경에서 수정하지 않는다. Swagger와 별도 handoff를 제공한다.
5. migration 01~32는 수정하지 않는다. 신규 forward migration만 추가한다.
6. 점수·fact id·diagnostics·원문 내부 좌표는 공개 응답으로 보내지 않는다.
7. 존재하지 않음과 의존 서비스 장애를 구분하고, 장애를 빈 결과로 위장하지 않는다.

## 2. 리뷰 결과의 수용과 보류

### 수용

- close 대상을 `analysis_session_id`로 고정한다.
- 사용자 단위 lifecycle lock과 사용자당 active result session DB 제약을 추가한다.
- worker가 evidence UUID를 선발급하고 DB가 모든 공개 참조를 원자적으로 검증한다.
- raw 결과와 public projection을 별도 컬럼으로 저장한다.
- 채팅 목록 pagination과 생성 답변 polling을 분리한다.
- Auth provider 장애 분류, 409 domain code, offline mode 기본값을 수정한다.
- FastAPI-only 경계에 맞춰 authenticated의 직접 KB/Storage 업무 접근을 철회한다.
- 만료 upload/queued 작업의 상태 정리를 추가한다.

### 이번 범위에서 보류

- evidence 정규화용 M:N 테이블
- processing 작업 취소 API
- 새로고침 후 최근 failed run을 확인·해제하는 UX
- migration ledger 도입
- 90일 경과 데이터와 Storage object의 물리 삭제 scheduler
- request Storage key 체계 전면 변경

API에서는 90일 경과 결과를 즉시 숨긴다. 물리 삭제가 아직 없다는 사실은 운영 문서에 명시하고 production 전 별도 gate로 둔다.

## 3. 인증과 사용자 이름

### 3.1 공개 사용자

`POST /api/v1/auth/sign-in`, `GET /api/v1/auth/me`는 동일한 사용자 구조를 반환한다.

```json
{
  "user": {
    "id": "5aeff4f1-b633-44f2-b3cf-1b8b00dbb546",
    "email": "user@example.com",
    "display_name": "홍길동"
  }
}
```

- HTTP 응답에서는 Supabase `/user`의 `user_metadata.display_name`을 읽는다.
- DB 원천은 `auth.users.raw_user_meta_data.display_name`이다.
- 문자열이 아니거나 trim 후 빈 값이면 email local-part를 사용한다.
- `display_name`은 NFC 정규화, trim 후 1~100자, 제어문자 금지다.
- `POST /api/v1/auth/sign-up`은 기존 credentials와 분리된 request model로 `display_name`을 필수로 받으며 Supabase signup의 `data.display_name`에 기록한다.
- test-user bootstrap은 기존 계정도 metadata를 PATCH한 뒤 `/user` 응답을 재검증한다.
- 기존 `app.user_profile`은 legacy로 유지하되 이번 기능에서 읽거나 쓰거나 삭제하지 않는다.

### 3.2 provider 오류와 cookie

| Supabase Auth 결과 | FastAPI 결과 | cookie 처리 |
|---|---:|---|
| invalid/expired credential | 401 | access/refresh 삭제 |
| rate limit | 429 | 유지 |
| transport 또는 upstream 5xx | 503 | 유지 |
| malformed upstream payload | 502 | 유지 |

`POST /auth/sign-out`은 예외적으로 provider transport/5xx가 발생해도 로컬 access/refresh
Cookie를 삭제한 뒤 `503`을 반환한다. Provider 장애가 일시적인 `refresh`, `GET /auth/me`,
또는 업무 API 요청은 기존 Cookie를 보존해 재시도할 수 있게 한다.

- 위 규칙은 auth route와 공통 `PrincipalDep`에 동일하게 적용한다.
- refresh cookie는 auth 경로로 scope를 좁히고 업무 API에는 전송하지 않는다.
- v0.1에서 `Path=/`로 발급된 동명 refresh cookie의 마이그레이션을 위해 세션 발급·갱신·삭제 응답은 현재 `Domain`/`Secure`/`SameSite` 설정을 그대로 사용해 legacy root-path cookie도 만료시킨다. 신규 credential은 계속 `Path=/api/v1/auth`에만 발급한다.
- 모든 상태 변경 route는 정확한 allow-list 기반 Origin 검사를 유지한다.
- 사용하지 않는 `X-CSRF-Token` CORS 허용은 제거한다.
- cookie 인증 업무 응답에는 공통으로 `Cache-Control: private, no-store`와 cookie 기준 `Vary`를 적용한다.
- production/offline 미설정 시 `PREREVIEW_OFFLINE_MODE` 기본값은 `false`다.
- offline dev header 인증은 명시적으로 켠 로컬 테스트에서만 허용하고 LAN/배포 runbook에서는 금지한다.

## 4. 오류 계약

HTTP status와 domain code를 독립적으로 정한다. 모든 409를 하나의 코드로 바꾸는 전역 매핑을 제거한다.

| 상황 | HTTP | code |
|---|---:|---|
| 인증 없음/만료 | 401 | `UNAUTHORIZED` |
| 타인 소유 또는 단건 없음 | 404 | `NOT_FOUND` |
| 동일 사용자의 진행 run 존재 | 409 | `ANALYSIS_RUN_ACTIVE` |
| 활성 결과 세션을 닫지 않은 신규 upload | 409 | `ACTIVE_RESULT_SESSION` |
| 같은 Idempotency-Key를 다른 입력에 재사용 | 409 | `IDEMPOTENCY_KEY_CONFLICT` |
| 채팅 상태 충돌 | 409 | `CHAT_CONFLICT` |
| 채팅 재시도 소진 | 409 | `CHAT_RETRY_EXHAUSTED` |
| 요청/cursor 검증 실패 | 422 | `VALIDATION_ERROR` |
| DB·Storage·Auth 장애 | 503 | `SERVICE_UNAVAILABLE` |

목록 없음은 200 empty다. legacy 활성 세션 endpoint의 활성 없음은 204이고 unified current는 `idle`이다.

## 5. 사용자 분석 lifecycle

### 5.1 불변식과 잠금

- `result.analysis_session`에 trusted-derived `user_id`를 저장하고 case의 user와 일치하도록 검증한다.
- 같은 사용자에게 `status='active'`인 result session은 최대 1건이다.
- 신규 migration은 기존 중복 active session을 `last_activity_at, analysis_session_pk` 순서로 하나만 남기고 나머지를 `closed/migration_reconciled`로 정리한 뒤 partial unique index를 만든다.
- upload reservation, result persistence, session close는 동일한 user UUID advisory transaction lock을 가장 먼저 획득하고, 같은 lock order로 row 조건을 재검증한다.
- 만료됐지만 status가 active인 session은 lock 안에서 `expired`로 전이시킨다.

### 5.2 upload idempotency 순서

1. 동일 owner, Idempotency-Key, source identity의 정확한 replay인지 먼저 확인한다.
2. 정확한 replay면 active session 여부와 관계없이 기존 run을 반환한다.
3. key namespace는 owner별이다. 같은 owner가 같은 key를 다른 source에
   재사용한 경우에만 `IDEMPOTENCY_KEY_CONFLICT`다. 다른 owner의 우연한 UUID
   충돌은 서로 보이지 않으며 독립 요청으로 처리한다.
4. 새로운 key일 때만 active processing run과 active result session을 검사한다.
5. active processing은 `ANALYSIS_RUN_ACTIVE`, active result는 `ACTIVE_RESULT_SESSION`이다.

### 5.3 세션 종료

```http
POST /api/v1/analysis-sessions/{analysis_session_id}/close
```

- cookie auth, trusted Origin, owner scope다.
- 지정한 동일 소유 session이 active면 `closed_at`과 `reason=new_analysis`를 기록하고 204다.
- 지정한 session이 이미 closed/expired면 204다.
- 타인 소유 또는 존재하지 않으면 404다.
- 예전 close 요청을 재전송해도 새로 만들어진 다른 session은 절대 닫지 않는다.
- 결과와 대화는 분석 완료 시점부터 90일 동안 read-only로 조회한다. close는 보관 만료를 연장하지 않는다.
- 로그아웃과 일반 화면 이동은 close하지 않는다.
- 새 분석 UI는 ready session ID를 close한 뒤 업로드 화면으로 이동한다.
- processing 중 cancel/new-analysis는 이번 범위 밖이다.

### 5.4 현재 상태

```http
GET /api/v1/analysis/current
```

하나의 DB snapshot에서 다음 discriminated union 중 하나를 반환한다.

```json
{"state":"processing","run":{"analysis_run_id":"uuid","status":"queued","original_filename":"request.hwpx","created_at":"...","updated_at":"..."},"session":null}
```

```json
{"state":"ready","run":null,"session":{"analysis_session_id":"uuid","analysis_case_id":"uuid","program_name":"사업명","original_filename":"request.hwpx","session_expires_at":"..."}}
```

```json
{"state":"idle","run":null,"session":null}
```

- 우선순위는 live processing > active/unexpired ready > idle이다.
- `uploading`은 예약 TTL, `queued`는 queue TTL, `running`은 lease/retry 정책을 만족할 때만 processing이다.
- expired uploading, expired queued, cleanup_pending은 processing으로 반환하지 않는다.
- expired uploading/queued를 terminal/cleanup 상태로 옮기는 idempotent reconciliation을 worker polling과 신규 upload 전 수행한다.
- running lease recovery는 기존 2회 fenced retry를 유지한다.
- DB 장애는 idle이 아니라 503이다.
- 가짜 progress percent/stage는 제공하지 않는다.
- 기존 `GET /analysis-runs/{id}`와 `GET /analysis-sessions/active`는 호환용으로 유지한다.
- 최근 failed run 재표시/acknowledgement는 후속이다.

## 6. 분석 이력

```http
GET /api/v1/analysis-history?cursor=<opaque>
```

```json
{"items":[],"next_cursor":null}
```

- page size는 5로 고정한다.
- 정렬 key는 `(completed_at DESC, analysis_case_id DESC)`다.
- 첫 page는 DB `snapshot_at`을 정하고, cursor는 version, snapshot_at, 마지막 sort key를 base64url로 담는다.
- 후속 page는 같은 snapshot을 사용한다. page 사이에 session이 close/expire되거나 새 결과가 끝나도 현재 pagination에 끼워 넣지 않는다.
- 새로 첫 page를 조회하면 변경이 반영된다.
- active/unexpired result session은 제외한다.
- cursor는 HMAC으로 서명하고 endpoint·owner·filter에 바인딩한다. 최대 길이와 exact key/type/version을 엄격히 검사하며 오류는 422다. cursor는 권한 수단이 아니고 모든 query는 owner predicate를 다시 적용한다.
- `next_cursor=null`이 마지막이며 total count는 제공하지 않는다.

## 7. CPL·FIT·SIM 공개 상태

| 구분 | 값 |
|---|---|
| CPL | `confirmed`, `needs_confirmation`, `no_content`, `not_applicable` |
| FIT | `FIT`, `NEEDS_REVIEW`, `CONFLICT`, `INSUFFICIENT`, `NOT_APPLICABLE` |
| SIM | `similar`, `partial`, `different`, `insufficient` |

- Pydantic, worker enum, DB validation, OpenAPI를 동일하게 맞춘다.
- 프론트는 상태를 임의 점수로 환산하지 않는다.
- FIT-4에서 계층 자체가 없으면 `NOT_APPLICABLE`, 계층 비교 대상은 있으나 근거가 부족하면 `INSUFFICIENT`다.
- `comparison_performed=true`는 양쪽의 grounded evidence로 실제 비교 판정까지 수행했다는 의미다. 단순 provider 호출 여부가 아니다.

## 8. 공개 결과와 내부 결과의 분리

### 8.1 CPL/FIT

- 기존 outer `{code,status,summary,detail}`은 유지한다.
- `result.axis_result.result_data`는 내부 감사·chat 용 raw 결과로 유지한다.
- 신규 `public_detail` JSON은 엄격히 검증된 공개 모양만 저장한다.

CPL detail:

```json
{"reason_code":null,"reason":"원문에서 확인했습니다.","values":[{"label":"지원 대상","value":"부산 소재 중소기업","evidence_ids":["uuid"]}],"source_fields":["support_target"],"evidence_ids":["uuid"]}
```

FIT detail:

```json
{"comparison_performed":true,"reason_code":null,"reason":"두 조건이 연결됩니다.","left":{"value_summary":"부산 소재 중소기업","evidence_ids":["uuid"]},"right":{"value_summary":"중소기업 지원기관","evidence_ids":["uuid"]},"evidence_ids":["uuid","uuid"]}
```

### 8.2 evidence 생성과 검증

- worker가 각 snapshot의 UUID를 선발급하고 public detail/axis는 그 UUID만 참조한다.
- UUID는 UUIDv5로 결정적으로 만든다. namespace input에는 analysis run, axis/candidate logical code, role/side, source identity와 원문 좌표를 포함한다. UUID는 권한 수단으로 사용하지 않는다.
- payload evidence에는 UUID, axis/candidate logical code, SIM axis, role, side, source identity를 함께 보낸다.
- materializer는 동일 fenced transaction에서 logical code를 DB PK로 해석한다.
- DB는 duplicate/dangling evidence ID, 다른 case의 axis/candidate 연결, 잘못된 axis-role-side 조합을 거부한다.
- CPL/FIT evidence는 동일 case의 `axis_result_pk`에 연결하고 candidate는 null이어야 한다.
- SIM evidence는 동일 case의 `sim_candidate_pk`와 `sim_axis_code`에 연결한다.
- `evidence_role`은 `VALUE|LEFT|RIGHT`, `side`는 `REQUEST|EXISTING`이다.
- LLM 판단은 실제 선택 fact만 evidence로 만든다. rule 판단은 해당 rule 입력으로 실제 사용한 fact만 사용한다. 비교 미실시는 빈 evidence IDs다.
- PoC에서는 같은 source가 여러 판단에 쓰이면 snapshot을 판단별로 복제한다.
- chat dedupe는 side, source document/profile version, fact 또는 relation/member 좌표를 포함한 key로 수행하고, dedupe·우선순위 정렬 후 상한을 적용한다.

분석 전체 응답의 `evidences`는 CPL/FIT처럼 `sim_candidate_pk IS NULL`인 근거만 반환한다. SIM 근거는 해당 candidate detail에서만 반환하여 후보 간 혼입을 막는다.

브라우저 결과와 chat grounding이 서로 다른 SQL로 결과를 조립하지 않도록 하나의 v2 public projection 함수를 공유한다. Chat은 내부 raw 문맥을 추가로 사용할 수 있지만 답변이 참조할 수 있는 evidence allow-list는 이 public projection에 존재하는 ID로 제한한다.

legacy row에서 정확한 public detail/evidence 연결을 복원할 수 없으면 raw 값을 추정 노출하지 않는다. 빈 evidence IDs와 `LEGACY_PUBLIC_DETAIL_UNAVAILABLE`를 반환한다.

## 9. 유사 공고 공개 계약

### 9.1 분석 결과의 후보 목록

```json
{
  "sim": {
    "status": "completed",
    "reason_code": null,
    "summary": "유사 공고 검색을 완료했습니다.",
    "candidates": [
      {"sim_candidate_id":"uuid","rank":1,"title":"지원사업명","comparison_status":"partial","comparison_summary":"목적은 유사하지만 대상에 차이가 있습니다."}
    ]
  }
}
```

SIM section의 status/reason/summary는 `result.analysis_case`의 전용 컬럼에 저장한다.

### 9.2 후보 상세

```http
GET /api/v1/sim-candidates/{sim_candidate_id}
```

```json
{
  "sim_candidate_id":"uuid",
  "analysis_case_id":"uuid",
  "rank":1,
  "metadata":{
    "title":"지원사업명",
    "support_field":"기술",
    "apply_period":"2026-09-01 ~ 2026-09-30",
    "ministry":"중소벤처기업부",
    "executing_agency":"전담기관",
    "registered_at":"2026-09-01",
    "notice_status":"모집중",
    "source_url":"https://..."
  },
  "comparison":{"status":"partial","summary":"일부 축이 유사합니다.","comparable_axes":["purpose","target","support"]},
  "axes":{
    "purpose":{"code":"SIM-1","status":"similar","summary":"...","reason_code":null,"reason":"...","common_points":[],"differences":[],"request_evidence_ids":[],"existing_evidence_ids":[]},
    "target":{"code":"SIM-2","status":"partial","summary":"...","reason_code":null,"reason":"...","common_points":[],"differences":[],"request_evidence_ids":[],"existing_evidence_ids":[]},
    "support":{"code":"SIM-3","status":"different","summary":"...","reason_code":null,"reason":"...","common_points":[],"differences":[],"request_evidence_ids":[],"existing_evidence_ids":[]},
    "delivery":{"code":"SIM-4","status":"insufficient","summary":"...","reason_code":"CANDIDATE_EVIDENCE_MISSING","reason":"...","common_points":[],"differences":[],"request_evidence_ids":[],"existing_evidence_ids":[]}
  },
  "evidences":[]
}
```

- metadata는 분석 당시의 정확한 Existing profile/source version snapshot이다. 현재 KB 값을 뒤늦게 join해 바꾸지 않는다.
- apply_period와 registered_at은 현재 원천 문자열을 그대로 반환하며 날짜를 추정 변환하지 않는다.
- internal content 축은 public `support`로만 노출한다.
- similarity score, priority score, fact IDs, diagnostics는 비공개다.
- 전체 comparison status는 core `purpose,target,support`만 사용한다: `insufficient > different > partial > similar`. delivery는 별도 표시하되 전체 status를 바꾸지 않는다.
- 기존 raw candidate axis JSON은 내부 컬럼에 유지하고, 신규 public axis 컬럼만 RPC가 읽는다.

## 10. 부분축 retrieval과 장애 결정표

사용 가능한 request embedding 축 집합을 `A`라고 한다.

| 상황 | 처리 | 최종 run |
|---|---|---|
| `A`가 비어 있음 | embedding config/provider/KB/SIM 호출 생략, CPL/FIT/ML 저장, SIM `RETRIEVAL_INPUT_MISSING` | succeeded |
| 1~3축, 후보 있음 | 있는 축만 embed, `|A|`로 평균, top 5 비교, 빠진 축은 `REQUEST_AXIS_MISSING` | succeeded |
| 1~3축, KB optional이며 corpus 0 | SIM `KB_EMPTY`, candidates empty | succeeded |
| 1~3축, KB required이며 match 0 | KB/config/index 오류로 retry | retry 후 failed |
| embedding/DB/config/OpenAI transport·timeout | 기술 장애로 retry | retry 후 failed |
| LLM schema/grounding이 repair 후 특정 축에서만 실패 | 해당 축 `insufficient/LLM_INVALID_RESPONSE`, 다른 결과 유지 | succeeded |

- 기본 운영값은 `PREREVIEW_EXISTING_KB_REQUIRED=true`다.
- 없는 축에 zero vector를 넣지 않는다.
- candidate는 요청한 모든 축을 동일 active configuration과 exact profile version으로 보유해야 한다.
- similarity의 분모는 정확히 `|A|`다.
- retrieval은 CPL/FIT/ML보다 먼저 실패시키지 않도록 0축 분기를 명시적으로 둔다.

## 11. 대화 계약

### 11.1 생성과 단건 polling

`POST /api/v1/analysis-cases/{case_id}/messages`의 202 응답에 assistant message ID가 포함된다. 프론트는 다음 단건 API로 해당 답변만 polling한다.

- 질문 생성에는 `Idempotency-Key: UUID`가 필수다.
- DB는 `(analysis_session_id, Idempotency-Key)`를 unique로 보관하고 질문 본문 hash를 함께 검증한다.
- 동일 key와 동일 본문 재전송은 기존 user/assistant message ID를 반환한다.
- 동일 key와 다른 본문은 `IDEMPOTENCY_KEY_CONFLICT`다.
- retry endpoint도 별도 `Idempotency-Key`를 받아 응답 유실 재전송이 retry count와 OpenAI 호출을 중복시키지 않게 한다.

```http
GET /api/v1/analysis-cases/{case_id}/messages/{message_id}
```

- owner와 90일 retention을 검사한다.
- `generating|completed|failed` 상태를 반환한다.
- 목록 전체를 timestamp delta로 polling하지 않는다.

### 11.2 과거 대화 목록

```http
GET /api/v1/analysis-cases/{case_id}/messages?cursor=<opaque>&limit=50
```

```json
{"items":[],"next_cursor":null}
```

- limit 기본 50, 최대 100이다.
- cursor는 `(sequence_no,message_id)`를 담고 더 오래된 메시지를 가져온다.
- 각 page의 items는 화면 표시를 위해 chronological order로 반환한다.
- `updated_since`는 신규 공개 계약에서 제거한다.
- closed/expired session도 retention 내 owner가 조회할 수 있다.
- frontend는 loading/empty/error를 구분하고 case 변경 때 messages와 cursor를 초기화한다.

### 11.3 close·expiry 경합

- create/retry는 session row를 잠그고 active+unexpired를 검사한다.
- close도 같은 row를 잠근다. 먼저 commit된 동작만 유효하다.
- create가 먼저 commit되어 generating dispatch가 생겼다면 이후 close/expiry와 관계없이 worker가 claim·heartbeat·complete/fail할 수 있다.
- claim은 session active 여부가 아니라 message generating, case retention, dispatch fence를 검사한다.
- retention이 끝난 미완료 메시지는 terminal failure로 정리한다.
- result 재저장은 closed/expired session을 active로 되살리지 않는다.
- result evidence 재생성은 `usage_scope='RESULT'`만 교체하며 conversation evidence/reference를 삭제하지 않는다.

## 12. FastAPI-only 보안 경계

- 신규 migration은 `authenticated`의 직접 `kb.*` SELECT와 legacy `app.user_profile` SELECT를 철회한다.
- request-temp의 authenticated browser INSERT/DELETE 정책과 helper grant를 제거하고 Storage 쓰기는 FastAPI/worker service credential로만 수행한다.
- service role key와 DATABASE_URL은 브라우저, Swagger example, 로그, 오류 응답에 절대 노출하지 않는다.
- owner predicate/RLS/fenced persistence는 유지하고 candidate/evidence UUID만으로 다른 case를 조회할 수 없어야 한다.
- upload 제한은 FastAPI와 앞단 gateway에 동일하게 적용하고, 배포 명령에 concurrency limit을 둔다. streaming upload 전환은 후속이다.
- 사용자별 동시 analysis/chat 생성 상한과 queue backpressure를 두어 직접 HTTP 호출로 작업·OpenAI 비용을 무제한 생성하지 못하게 한다.

구현된 배포 상한은 `PREREVIEW_UPLOAD_MAX_BYTES`(추출한 단일 HWP/HWPX 파일, 기본 50 MiB),
`PREREVIEW_HTTP_MAX_BODY_BYTES`(multipart boundary/header와 모든 part를 포함한 전체 HTTP
body, 기본 51 MiB), Uvicorn `--limit-concurrency`(`PREREVIEW_API_LIMIT_CONCURRENCY`,
기본 32)다. migration 37의 `PREREVIEW_GLOBAL_QUEUE_MAX`(기본 25)는 모든 API replica의
analysis upload와 chat create/retry가 공유하며, 새 작업이 가득 차면 `503`을 반환한다.

## 13. migration 구현 규칙

- migration은 기능별 신규 번호로 나누되 최종 상태가 전체 replay에서도 동일해야 한다.
- 01~32의 public 함수와 migration 26 ML wrapper는 그대로 둔다.
- 현재 fresh 적용 범위는 migration 01~40이며, 01~32가 이미 적용된 DB는 33~40을
  순서대로 upgrade한 뒤 전체 01~40 replay 검증을 수행한다. migration 38은 Model 1
  runtime 코드 manifest가 바뀐 경우 새 inactive configuration을 등록하며, 과거 분류
  row를 재작성하지 않는다. migration 39는 만료된 upload finalisation도 migration 37의
  전역 admission lock 아래 re-admit하고, capacity가 가득 차면 exact source를
  `cleanup_pending`으로 fence해 cleanup key를 반환한다. migration 40은 각 analysis
  실행 시도가 실제 선택한 embedding configuration(또는 검색 축이 없어 선택하지
  않았다는 명시적 null snapshot)을 fenced `ops.processing_run.run_metadata`에 고정한다.
- 신규 worker는 `workspace.persist_analysis_result_core_v2`를 호출한다. v2 함수는 기존 fenced+ML writer를 같은 transaction 안에서 호출한 뒤 public projection/evidence context를 검증·저장하며, 어느 단계든 실패하면 기존 writer의 상태 전이까지 전부 rollback한다.
- 신규 FastAPI는 `api.rpc_get_analysis_result_v2`, `api.rpc_get_sim_candidate_detail_v2`와 v2 history/current 함수를 호출한다. 구 함수는 rollback과 구 pod 보호를 위해 유지한다.
- chat claim도 별도 복사본을 만들지 않고 v2 public projection helper를 사용한다.
- 새 함수·view의 owner, search_path, REVOKE/GRANT를 명시한다.
- 기존 상태/JSON을 검사·정리한 뒤 새 constraint를 추가한다.
- 다음 세 경로를 실제 PostgreSQL에서 검증한다.
  1. 빈 DB에 01~신규 migration 전체 적용
  2. 01~32와 데이터가 있는 DB에 upgrade
  3. 전체 migration 두 번째 replay
- migration ledger는 이번 범위에서 도입하지 않는다.

## 14. 호환성과 release gate

Breaking change:

- analysis history array → `{items,next_cursor}`
- conversation history array → `{items,next_cursor}`
- CPL/FIT opaque detail → typed detail
- candidate top-level/raw axes → metadata/comparison/public axes
- signup request에 display_name 필수

Additive change:

- `/analysis/current`
- session-id close endpoint
- message 단건 polling endpoint
- auth response display_name
- SIM section status/reason/summary
- candidate list comparison fields

현재는 안정화 전 PoC이므로 `/api/v1`을 유지하고 dual v1/v2는 만들지 않는다. 단, backend만 배포하면 현재 frontend와 호환되지 않는다. Swagger와 [BACKEND_FASTAPI_SUPABASE_HANDOFF.md](BACKEND_FASTAPI_SUPABASE_HANDOFF.md)의 frontend cutover handoff를 전달한 뒤 frontend 담당자의 동시 cutover가 끝나기 전에는 통합 완료 또는 production-ready로 표시하지 않는다.

단일 서버 maintenance cutover 순서는 다음으로 고정한다.

1. 신규 upload/chat 유입 중지
2. worker queue drain 후 기존 worker/FastAPI 중지
3. forward migration 적용
4. 신규 FastAPI/worker 기동 및 Swagger smoke test
5. frontend 계약 전환

DB 내부 v2 함수가 구 함수를 보존하므로 코드 rollback 경로는 남긴다. 실행 중 구 pod와 신규 DB projection을 혼용하지 않는다.

## 15. 검증과 완료 조건

### 계약·단위 테스트

- Auth display_name, fallback, 401/429/502/503, refresh cookie 보존
- offline 기본 false 및 dev header 차단
- chat create/retry Idempotency-Key same/different-body replay
- domain별 409 code
- current 3상태와 stale uploading/queued 제외
- exact session close idempotence와 owner 404
- history snapshot cursor, tie, active 제외, close 중 pagination
- CPL/FIT/SIM enum, FIT-4, typed public detail
- public 응답의 fact IDs/diagnostics/score 비노출
- evidence UUID dangling/duplicate/cross-case/context validation
- candidate의 Request/Existing 양쪽 선택 근거
- 0/1/2/3축 retrieval과 KB/provider 결정표
- chat 단건 polling, history cursor, close 직전 accepted job completion
- owner/retention 경계와 candidate 간 evidence 비혼입

### 실제 DB 동시성 테스트

- close 응답 유실 → 새 session 생성 → 예전 close 재전송
- 두 탭 동시 upload
- result completion ↔ 신규 upload
- close ↔ chat create
- expiry 직전 create ↔ worker claim
- worker lease expiry와 두 번째 attempt fence
- 동일 Idempotency-Key same/different source replay

### migration과 E2E

- fresh/upgrade/replay PostgreSQL migration
- Existing KB 100건 bootstrap, active v2 embedding configuration, 각 scope row 검증
- FastAPI login cookie → 실제 HWP/HWPX upload → Storage → DB queue → 실제 worker → Request Profile → embedding/top5 → CPL/FIT/SIM/ML → DB → result/candidate API
- 채팅 create → 단건 polling → completed 및 과거 대화 재조회
- ASGI transport에서 result/chat/analysis dependency 호출이 제한 시간 안에 끝나는지 검증하고, 재현된 sync dependency threadpool timeout을 제거한다.
- Swagger에서 허용된 Origin으로 login/upload/current/close/history/chat 검증
- 합성 fixture와 실제 Hancom 작성 HWP/HWPX 결과를 구분해 기록
- 입력 SHA-256, git commit, worker image/runtime identity, run/case ID, model/configuration version을 E2E 기록에 남긴다.

## 16. 구현·커밋 단위

1. `docs/contracts`: 본 명세, OpenAPI 계약 테스트, frontend handoff
2. `db-lifecycle`: user lock, active session, current/close/history, stale reconciliation, 권한
3. `db-result-retrieval`: public/raw projection, evidence, partial-axis match
4. `worker`: partial retrieval, selected evidence, FIT-4, public payload
5. `fastapi`: auth, errors, lifecycle, result, pagination, chat polling
6. `validation`: migration runtime, concurrency, E2E, runbook

각 단위는 로컬 커밋으로 남기고, 모든 통합 검증 후 커밋 메시지 목록을 사용자에게 확인받은 다음 push한다.
