# Pre-review 프론트 API 계약 v1.0

## 1. 로그인

`POST /api/v1/auth/login`

파라미터:

```json
{ "email": "demo@pre-review.com", "password": "사용자 입력값" }
```

리턴:

```json
{ "access_token": "..." }
```

| 코드 | 뜻 | 프론트 처리 |
|---|---|---|
| `200` | 로그인 성공 | 토큰 저장 후 메인 화면 이동 |
| `401` | 이메일·비밀번호·입력값 오류 | 로그인 실패 안내 |

## 2. 세션 연장

`POST /api/v1/auth/refresh`

파라미터: 없음

```json
{ "access_token": "..." }
```

| 코드 | 뜻 | 프론트 처리 |
|---|---|---|
| `200` | 세션 연장 성공 | 기존 토큰 교체 |
| `401` | 토큰 오류 또는 만료 | 토큰 삭제 후 로그인 화면 이동 |

## 3. 로그인 사용자 확인

`GET /api/v1/auth/me`

파라미터: 없음

```json
{ "name": "demo" }
```

| 필드 | 뜻 |
|---|---|
| `name` | 계정 이메일의 `@` 앞부분 |

| 코드 | 뜻 | 프론트 처리 |
|---|---|---|
| `200` | 사용자 확인 성공 | 사이드바 사용자 표시 |
| `401` | 토큰 오류 또는 만료 | 토큰 삭제 후 로그인 화면 이동 |

## 4. 비밀번호 변경

`POST /api/v1/auth/password/change`

파라미터:

```json
{ "current_password": "현재 비밀번호", "new_password": "새 비밀번호" }
```

리턴:

```json
{ "access_token": "..." }
```

| 코드 | 뜻 | 프론트 처리 |
|---|---|---|
| `200` | 비밀번호 변경 성공 | 기존 토큰 교체 |
| `400` | 현재 또는 새 비밀번호 입력 오류 | 해당 입력 안내 |
| `401` | 토큰 오류 또는 만료 | 로그인 화면 이동 |

## 5. 비밀번호 재설정 요청

`POST /api/v1/auth/password/reset/request`

파라미터:

```json
{ "email": "demo@pre-review.com" }
```

리턴:

```json
{ "message": "등록된 이메일이라면 비밀번호 재설정 안내가 발송됩니다." }
```

| 코드 | 뜻 | 프론트 처리 |
|---|---|---|
| `200` | 요청 접수 | 안내 후 로그인 화면 이동 |
| `400` | 이메일 입력 오류 | 이메일 확인 안내 |
| `429` | 메일 요청 횟수 초과 | 잠시 후 재시도 안내 |

## 6. 비밀번호 재설정 확인

`POST /api/v1/auth/password/reset/confirm`

파라미터:

```json
{ "token": "메일 링크의 token 값", "new_password": "새 비밀번호" }
```

리턴:

```json
{ "message": "비밀번호가 재설정되었습니다." }
```

| 코드 | 뜻 | 프론트 처리 |
|---|---|---|
| `200` | 비밀번호 재설정 성공 | 로그인 화면 이동 |
| `400` | 링크 또는 새 비밀번호 오류 | 링크 재요청 또는 입력 안내 |

## 7. 요청서 업로드 및 분석 시작

`POST /api/v1/cases`

파라미터: `multipart/form-data`

| 필드 | 타입 | 뜻 |
|---|---|---|
| `file` | HWP 또는 HWPX | 분석할 요청서 한 개, 최대 50MB |

리턴:

```json
{ "case_id": 2183 }
```

| 코드 | 뜻 | 프론트 처리 |
|---|---|---|
| `200` | 업로드 성공 및 분석 시작 | `case_id`로 상태 조회 |
| `400` | 파일 입력 오류 | 파일 확인 안내 |
| `401` | 토큰 오류 또는 만료 | 로그인 화면 이동 |
| `413` | 파일 용량 초과 | 50MB 이하 파일 안내 |
| `415` | 파일 형식 오류 | HWP/HWPX 파일 안내 |

## 8. 분석 진행 상태

`GET /api/v1/cases/{case_id}/status`

파라미터:

| 위치 | 필드 | 뜻 |
|---|---|---|
| path | `case_id` | 업로드 API에서 받은 분석 건 식별자 |

리턴:

```json
{ "status": "IN_PROGRESS" }
```

| `status` | 뜻 |
|---|---|
| `IN_PROGRESS` | 분석 중 |
| `COMPLETED` | 분석 완료 |
| `FAILED` | 분석 실패 |

| 코드 | 뜻 | 프론트 처리 |
|---|---|---|
| `200` | 상태 조회 성공 | `status`에 따라 분기 |
| `401` | 토큰 오류 또는 만료 | 로그인 화면 이동 |
| `404` | 분석 건 없음 또는 접근 불가 | 업로드 화면 이동 |

## 9. 분석 이력 목록

`GET /api/v1/cases`

파라미터:

| 위치 | 필드 | 필수 | 뜻 |
|---|---|---|---|
| query | `cursor` | 아니요 | 이전 응답의 `next_cursor`. 첫 요청은 생략 |

리턴:

```json
{
  "items": [
    {
      "case_id": 2183,
      "title": "요청서.hwpx",
      "completed_at": "2026-09-03T12:46:06Z"
    }
  ],
  "next_cursor": null
}
```

| 필드 | 뜻 |
|---|---|
| `items[].case_id` | 상세 조회에 사용할 분석 건 식별자 |
| `items[].title` | 요청서 파일명 |
| `items[].completed_at` | 분석 완료 시각 |
| `next_cursor` | 다음 5건 조회값. `null`이면 마지막 |

| 코드 | 뜻 | 프론트 처리 |
|---|---|---|
| `200` | 목록 조회 성공 | 이력 표시 |
| `400` | 잘못된 `cursor` | 첫 목록부터 다시 조회 |
| `401` | 토큰 오류 또는 만료 | 로그인 화면 이동 |

## 10. 분석 상세

`GET /api/v1/cases/{case_id}`

파라미터:

| 위치 | 필드 | 뜻 |
|---|---|---|
| path | `case_id` | 조회할 분석 건 식별자 |

리턴 공통 구조:

```json
{
  "case": { "title": "요청서.hwpx", "completed_at": "2026-09-03T12:46:06Z" },
  "report": { "cpl": {}, "fit": {}, "similar_candidates": [] },
  "chat": { "messages": [], "next_cursor": null }
}
```

### 10.1 CPL: `report.cpl`

```json
{
  "confirmed_count": 8,
  "items": [
    {
      "field_code": "PURPOSE_GOAL",
      "status": "PRESENT",
      "evidence": [{ "excerpt": "원문 근거" }]
    }
  ]
}
```

| `status` | 뜻 |
|---|---|
| `PRESENT` | 필요한 내용 확인 |
| `MISSING` | 필요한 내용 누락 |
| `NOT_APPLICABLE` | 해당 없음이 원문에 명시됨 |
| `NEEDS_CONFIRMATION` | 확인 필요 |

| `field_code` | 화면 항목명 |
|---|---|
| `REQUEST_TYPE` | 사전협의 요청 유형 |
| `PURPOSE_GOAL` | 사업 목적·목표 |
| `IMPLEMENTATION_PLAN` | 추진계획 |
| `BUSINESS_PERIOD` | 사업 기간 |
| `NEW_OR_CHANGED_CONTENT` | 신설·변경 내용 |
| `BUSINESS_NEED` | 사업 필요성 |
| `LEGAL_BASIS` | 법적 근거 |
| `LINKED_POLICY` | 연계 정책·계획 |
| `BUDGET` | 예산 |
| `TARGET_AND_CONDITIONS` | 지원 대상·조건 |
| `SUPPORT_CONTENT_AND_SCALE` | 지원 내용·규모 |
| `DELIVERY_SYSTEM` | 수행 체계 |
| `EXPECTED_EFFECTS_AND_PERFORMANCE` | 기대효과·성과지표 |

### 10.2 FIT: `report.fit`

```json
{
  "module_status": "AVAILABLE",
  "availability": { "assessable_count": 6 },
  "relations": [
    {
      "relation_id": "FIT-1",
      "status": "FIT",
      "summary": "비교 설명",
      "left_evidence": [{ "excerpt": "왼쪽 원문" }],
      "right_evidence": [{ "excerpt": "오른쪽 원문" }]
    }
  ]
}
```

| 값 | 뜻 |
|---|---|
| `AVAILABLE` | FIT 결과 생성 완료 |
| `UNAVAILABLE` | FIT 결과 생성 불가 |
| `FIT` | 연결 관계 확인 |
| `NEEDS_REVIEW` | 추가 검토 필요 |
| `CONFLICT` | 관계 충돌 확인 |
| `INSUFFICIENT` | 비교 정보 부족 |

| `relation_id` | 비교 내용 |
|---|---|
| `FIT-1` | 목적의 대상 조건 ↔ 지원 대상 |
| `FIT-2` | 목적 방향 ↔ 지원 활동·수단 |
| `FIT-3` | 목적 방향 ↔ 기대효과·성과지표 |
| `FIT-4` | 사업 계층 간 비교 |
| `FIT-5` | 대상군 ↔ 지원 조건 |
| `FIT-6` | 수행기관 ↔ 절차·역할 |
| `FIT-7` | 지원 내용 ↔ 지원 규모 정량값 |

### 10.3 SIM: `report.similar_candidates[]`

```json
{
  "title": "유사 공고명",
  "source_url": "https://...",
  "comparison_summary": "전체 비교 요약",
  "axes": {
    "purpose": {
      "status": "SIMILAR",
      "summary": "축 비교 설명",
      "common_points": [],
      "differences": [],
      "request_evidence": [{ "excerpt": "요청서 원문" }],
      "candidate_evidence": [{ "excerpt": "공고 원문" }]
    },
    "target": {},
    "content": {},
    "delivery": {}
  }
}
```

`target`, `content`, `delivery`는 `purpose`와 같은 필드 구조입니다.

| 축 | 뜻 |
|---|---|
| `purpose` | 사업 목적 |
| `target` | 지원 대상 |
| `content` | 지원 내용 |
| `delivery` | 수행 체계 |

| `status` | 뜻 |
|---|---|
| `SIMILAR` | 공통점 확인 |
| `PARTIAL` | 일부 공통점 확인 |
| `DIFFERENT` | 차이 확인 |
| `INSUFFICIENT` | 비교 정보 부족 |

### 10.4 대화: `chat`

```json
{
  "messages": [{ "id": 1, "role": "USER", "content": "질문" }],
  "next_cursor": null
}
```

| `role` | 뜻 |
|---|---|
| `USER` | 사용자 질문 |
| `ASSISTANT` | AI 답변 |

| 코드 | 뜻 | 프론트 처리 |
|---|---|---|
| `200` | 상세 조회 성공 | 분석 결과와 최근 대화 표시 |
| `401` | 토큰 오류 또는 만료 | 로그인 화면 이동 |
| `404` | 분석 건 없음 또는 접근 불가 | 업로드 또는 이력 화면 이동 |
| `409` | 분석 미완료 | 상태 다시 조회 |

## 11. PDF 다운로드

`GET /api/v1/cases/{case_id}/report`

파라미터: path의 `case_id`
리턴: `application/pdf` 바이너리

| 코드 | 뜻 | 프론트 처리 |
|---|---|---|
| `200` | PDF 조회 성공 | 파일 다운로드 |
| `401` | 토큰 오류 또는 만료 | 로그인 화면 이동 |
| `404` | PDF 또는 분석 건 없음 | 파일 없음 안내 |
| `409` | 분석 미완료 | 완료 후 재시도 안내 |
| `503` | 파일 서비스 일시 오류 | 재시도 안내 |

## 12. 이전 대화 조회

`GET /api/v1/cases/{case_id}/messages`

파라미터:

| 위치 | 필드 | 필수 | 뜻 |
|---|---|---|---|
| path | `case_id` | 예 | 분석 건 식별자 |
| query | `cursor` | 아니요 | 이전 응답의 `next_cursor` |

리턴:

```json
{
  "messages": [{ "id": 1, "role": "USER", "content": "질문" }],
  "next_cursor": null
}
```

| 코드 | 뜻 | 프론트 처리 |
|---|---|---|
| `200` | 대화 조회 성공 | 기존 대화 위에 추가 |
| `401` | 토큰 오류 또는 만료 | 로그인 화면 이동 |
| `404` | 분석 건 없음 또는 접근 불가 | 분석 건 없음 안내 |
| `409` | 분석 미완료 | 분석 미완료 안내 |

## 13. AI 질문

`POST /api/v1/cases/{case_id}/messages`

파라미터:

```json
{ "content": "분석 결과에 대한 질문" }
```

리턴:

```json
{
  "user_message": { "id": 2, "role": "USER", "content": "질문" },
  "assistant_message": { "id": 3, "role": "ASSISTANT", "content": "답변" }
}
```

| 코드 | 뜻 | 프론트 처리 |
|---|---|---|
| `200` | AI 답변 성공 | 질문과 답변 표시 |
| `400` | 질문 입력 오류 | 질문 확인 안내 |
| `401` | 토큰 오류 또는 만료 | 로그인 화면 이동 |
| `404` | 분석 건 없음 또는 접근 불가 | 분석 건 없음 안내 |
| `409` | 분석 미완료 | 분석 미완료 안내 |
| `503` | AI 서비스 일시 오류 | 답변 실패 및 재시도 안내 |

`content`는 공백 제외 1~4,000자입니다. `role` 코드는 10.4와 같습니다.
