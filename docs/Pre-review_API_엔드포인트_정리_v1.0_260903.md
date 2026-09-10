# Pre-review API 엔드포인트 정리

2026-09-03 기준 · **구현된 그대로** 적었다. 실행 중인 서버의 OpenAPI 에서 뽑았다.

전체 12개다. 스웨거는 `/docs` 에서 볼 수 있다.

## 0. 공통 규칙

**공통 응답 래퍼가 없다.** 예전 `{code, message, data, errors}` 4필드는 없앴다. 성공하면 그 API 의 본문이 바로 오고, 실패하면 아래 하나만 온다.

```json
{ "message": "요청을 처리하지 못했습니다." }
```

**`message` 로 분기하지 않는다.** 화면 문구는 프론트가 갖고, 계약의 핵심은 **상태 코드**다. 서버 `message` 는 폴백·로그용이다.

**인증은 헤더로 한다.** 로그인과 비밀번호 재설정 요청, 헬스체크를 뺀 전부.

```
Authorization: Bearer <access_token>
```

**시각은 전부 ISO 8601 UTC** 다. 예: `2026-09-03T05:12:16.223182Z`

**비밀번호가 바뀌면 그 전에 발급된 토큰은 전부 무효다.** 재로그인이 필요하다.

### 공통 오류 코드

| 코드 | 언제 | 화면 |
|---|---|---|
| `401` | 토큰이 없거나 만료됐거나 잘못됨 | 로그인 화면으로 |
| `404` | 그 분석 건이 없거나 남의 것 | 이력으로 |
| `422` | 입력 형식 오류 | 그 자리에서 다시 입력 |

`404` 는 남의 건을 조회할 때도 나온다. 존재 여부를 알려주지 않기 위해서다.

---

# 1. 인증

## AUTH-01. 로그인

```
POST /api/v1/auth/login          인증 불필요
```

**요청**

```json
{ "email": "sunny10@pre-review.com", "password": "demo1234" }
```

| 필드 | 타입 | 뜻 |
|---|---|---|
| `email` | string | 로그인 식별자. **아이디가 아니라 이메일이다** |
| `password` | string | 비밀번호 |

**응답 `200`**

```json
{ "access_token": "eyJhbGciOiJIUzI1NiIs..." }
```

`token_type` 은 없다. 항상 Bearer 다.

| 코드 | 언제 |
|---|---|
| `200` | 성공 |
| `401` | 이메일 또는 비밀번호 불일치. **계정 존재 여부는 알려주지 않는다** |
| `422` | 이메일 형식이 아니거나 입력이 비었다 |

**시도 횟수 제한은 없다.** 몇 번 틀려도 잠기지 않는다.

## AUTH-02. 세션 연장

```
POST /api/v1/auth/refresh        인증 필요
```

요청 본문 없음. 응답은 로그인과 같은 `{ "access_token": "..." }` 다.

| 코드 | 언제 |
|---|---|
| `200` | 새 토큰 발급 |
| `401` | 이미 만료됐거나 잘못된 토큰. 재로그인 필요 |

**세션 수명은 1시간이다.** 만료 전에만 연장된다. 요청마다 자동으로 늘어나는 슬라이딩 방식이 **아니다.**

연장 시점, 남은 시간 계산, 연장 확인 UI 는 전부 프론트가 처리한다. 토큰의 `exp` 를 직접 읽으면 된다.

> 현재 `.env` 의 만료 시간을 **1년으로 늘려 둔 상태**다. 프론트 작업 편의를 위한 임시 조치이며, 연장 구현이 끝나면 1시간으로 되돌린다.

## AUTH-03. 로그인 사용자 확인

```
GET /api/v1/auth/me              인증 필요
```

**응답 `200`**

```json
{ "name": "sunny10" }
```

| 필드 | 뜻 |
|---|---|
| `name` | 이메일의 `@` 앞부분. 서버가 잘라서 보낸다 |

전체 이메일 주소는 응답에 담지 않는다. `app_user` 에 이름 컬럼을 따로 두지 않기로 했다.

## AUTH-04. 비밀번호 변경 (로그인 상태)

```
POST /api/v1/auth/change-password    인증 필요
```

**요청**

```json
{ "current_password": "demo1234", "new_password": "New-password1!" }
```

**응답 `200`** — `{ "access_token": "..." }`

**변경에 성공하면 새 토큰을 바로 준다.** 기존 토큰은 전부 무효가 되므로, 이 토큰으로 갈아끼우면 재로그인 없이 이어서 쓸 수 있다.

| 코드 | 언제 | 화면 |
|---|---|---|
| `200` | 변경됨 | 새 토큰으로 교체 |
| `400` | **현재 비밀번호가 틀림** | 그 자리에서 다시 입력 |
| `422` | 규칙 위반이거나 **기존과 같은 비밀번호** | 그 자리에서 다시 입력 |
| `401` | 토큰 문제 | 로그인 화면으로 |

**비밀번호 규칙** — 8~128자, ASCII 영문자·숫자·특수문자 각각 1개 이상. 특수문자는 `!` 부터 `~` 사이의 기호이며 공백이나 한글은 특수문자로 세지 않는다. 확인란 일치는 화면에서만 검사하고 서버는 새 비밀번호 하나만 받는다.

## AUTH-05. 비밀번호 재설정 요청

```
POST /api/v1/auth/password-reset/request     인증 불필요
```

**요청** — `{ "email": "..." }`

**임시 비밀번호를 만들어 메일로 보낸다.** 사용자는 메일에 적힌 값으로 평소처럼 로그인한다. **프론트가 만들 화면은 없다.** 재설정 링크도, 확인 API 도 없다.

**응답** — `{ "message": "등록된 이메일이라면 임시 비밀번호가 발송됩니다." }`

| 코드 | 언제 | 화면 |
|---|---|---|
| `200` | 정상 / **미가입 이메일** / **비활성 계정** / **메일 미설정** | 안내 문구 하나, 로그인 화면 유지 |
| `422` | 이메일 형식이 아님 | 화면 유지 |
| `429` | 요청 제한 초과 | 화면 유지. `Retry-After` 헤더(초) |

**화면 처리는 한 갈래다.** 어느 경우든 `등록된 주소라면 메일이 갑니다` 같은 한 가지 안내만 띄우면 된다. 가입 여부를 구분해 알려주지 않는 것은 이메일을 넣어보며 가입 여부를 알아내는 것을 막기 위해서다.

발송에 성공하면 **기존 비밀번호와 기존 접근 토큰이 모두 무효가 된다.** 임시 비밀번호는 만료되지 않으므로 로그인 후 AUTH-04 로 변경하도록 안내하는 편이 좋다.

요청 제한은 **IP당·이메일당 15분에 5회**이며 초과 시 `Retry-After: 900` 이 온다.

> **로그아웃 API 는 없다.** 서버가 토큰을 폐기하지 않기로 해 아무 일도 하지 않는 껍데기였다. 프론트가 저장 토큰을 지우면 된다. 그 토큰은 만료까지는 유효하다.

---

# 2. 분석

## CASE-01. 요청서 업로드 및 분석 시작

```
POST /api/v1/cases               인증 필요
Content-Type: multipart/form-data
```

**요청** — 폼 필드 `file` 에 파일 **정확히 하나**

**검증을 통과하면 곧바로 분석이 시작된다.** 분석을 따로 시작하는 API 는 없다.

**응답 `202`**

```json
{ "case_id": 2183, "started_at": "2026-09-03T05:33:23.967192Z" }
```

| 필드 | 뜻 |
|---|---|
| `case_id` | 이후 모든 조회에 쓰는 식별자 |
| `started_at` | 분석 시작 시각 |

| 코드 | 언제 |
|---|---|
| `202` | 접수. 분석 시작됨 |
| `400` | 파일이 없거나 둘 이상 |
| `413` | 50MB 초과 |
| `415` | HWP·HWPX 가 아님 |
| `422` | 파일이 비었거나 확장자와 실제 내용이 다름 |

**지원 형식은 HWP 와 HWPX 뿐, 최대 50MB.**

`case_id` 를 브라우저에 저장해 두면 새로고침해도 CASE-02 로 진행 상태를 다시 조회해 대기 화면을 복구할 수 있다.

## CASE-02. 분석 진행 상태 (롱폴링)

```
GET /api/v1/cases/{case_id}/status       인증 필요
```

**응답 `200`**

```json
{ "case_id": 2183, "status": "IN_PROGRESS" }
```

| `status` | 뜻 | 화면 |
|---|---|---|
| `IN_PROGRESS` | 진행 중 | 대기 화면 유지, **다시 호출** |
| `COMPLETED` | 완료 | CASE-04 로 결과 조회 |
| `FAILED` | 실패 | 업로드 화면에서 실패 안내 |

**롱폴링이다.** 상태가 바뀔 때까지 응답을 붙잡고 있다가 바뀌는 즉시 돌려준다. **최대 25초** 기다리며 그 안에 변화가 없으면 현재 상태로 응답한다. 그때 다시 호출하면 된다.

짧은 주기로 반복 호출할 필요가 없다. 응답을 받으면 바로 다시 부르는 방식이면 된다.

**실패 사유는 담지 않는다.** 화면은 실패 하나로만 다룬다.

| 코드 | 언제 |
|---|---|
| `200` | 현재 상태 |
| `404` | 분석 건이 없거나 남의 것 |

## CASE-03. 분석 이력 목록

```
GET /api/v1/cases?limit=5&cursor=...     인증 필요
```

| 쿼리 | 기본 | 뜻 |
|---|---|---|
| `limit` | `5` | 한 번에 받을 개수. 1~50 |
| `cursor` | 없음 | 이전 응답의 `next_cursor` 를 그대로. 첫 쪽은 비운다 |

**응답 `200`**

```json
{
  "items": [
    { "case_id": 2121, "title": "사전협의서_예시.hwp", "completed_at": "2026-09-03T05:20:31.402Z" }
  ],
  "next_cursor": "eyJjIjoiMjAy..."
}
```

| 필드 | 뜻 |
|---|---|
| `items[].case_id` | 분석 건 식별자 |
| `items[].title` | **업로드한 파일명.** 요청서에서 사업명을 뽑지 않는다. `null` 일 수 있다 |
| `items[].completed_at` | 분석 완료 시각 |
| `next_cursor` | 다음 쪽 커서. **`null` 이면 마지막 쪽** |

**완료된 분석만 담는다.** 그래서 상태 필드가 없다. 진행 중인 건은 업로드한 브라우저가 `case_id` 로 복구하고, 실패는 업로드 화면에서만 알린다.

완료 시각 내림차순이다. 더보기를 누를 때마다 `next_cursor` 를 넣어 다음 5건을 받는다.

| 코드 | 언제 |
|---|---|
| `400` | `cursor` 가 서버가 만든 값이 아님 |

## CASE-04. 분석 상세 조회

```
GET /api/v1/cases/{case_id}              인증 필요
```

**결과 화면에 필요한 것을 한 번에 준다.** 보고서와 대화를 따로 부르지 않아도 된다.

**응답 `200`**

```json
{
  "case":   { "case_id": 2121, "title": "사전협의서_예시.hwp", "completed_at": "..." },
  "report": { "cpl": {}, "fit": {}, "similar_candidates": [] },
  "chat":   { "messages": [], "next_cursor": null }
}
```

| 필드 | 뜻 |
|---|---|
| `case` | 화면 Header 용. 파일명과 완료 시각 |
| `report` | 분석 결과 3종. **아래 3장에서 자세히** |
| `chat` | **최근 20개** 대화. 더 이전 것은 CHAT-01 로 |

| 코드 | 언제 | 화면 |
|---|---|---|
| `200` | 완료된 건 | 결과 표시 |
| `404` | 없거나 남의 것 | 이력으로 |
| `409` | **아직 분석이 안 끝남** | 대기 화면으로 |

PDF 링크는 담지 않는다. 완료된 건은 PDF 가 항상 있고 경로가 고정이다.

## CASE-05. 보고서 PDF 다운로드

```
GET /api/v1/cases/{case_id}/report       인증 필요
```

**응답 `200`** — `application/pdf` 바이너리. `Content-Disposition` 헤더에 파일명이 있다.

| 코드 | 언제 |
|---|---|
| `200` | PDF |
| `404` | 없거나 남의 것 |
| `409` | 아직 분석이 안 끝나 PDF 가 없음 |
| `503` | 저장소에서 PDF 를 읽지 못함 |

PDF 내용은 **화면과 동일하다.** 같은 응답 모델을 그린다.

---

# 3. 분석 결과: `report`

CASE-04 의 `report` 안이다.

## 3.1 CPL — 요청자료 완전성·기초구조 점검

요청서 필수 항목 13개의 점검 결과다.

```json
{
  "confirmed_count": 8,
  "total_count": 13,
  "items": []
}
```

| 필드 | 뜻 |
|---|---|
| `confirmed_count` | `PRESENT` + `NOT_APPLICABLE` 인 항목 수 |
| `total_count` | 항상 `13` |
| `items` | 항목별 결과 |

**확인율 퍼센트는 담지 않는다.** 화면은 13개 중 몇 개인지만 쓴다.

### `items[]`

```json
{
  "field_code": "TARGET_AND_CONDITIONS",
  "status": "NEEDS_CONFIRMATION",
  "evidence": [{ "excerpt": "전략적 제휴완료 또는 예정인 ICT중소・벤처기업(법인)" }]
}
```

| 필드 | 뜻 |
|---|---|
| `field_code` | 항목 코드. 한글 항목명은 프론트가 매핑 |
| `status` | 점검 상태 |
| `evidence` | 원문 근거. **`MISSING`·`NEEDS_CONFIRMATION` 항목에만 담긴다** |

`evidence` 는 분석용 세부 조각을 합쳐 **최대 1개**로 준다. 그 안에 여러 문단이 `\n\n` 으로 이어져 있을 수 있으므로 `white-space: pre-wrap` 으로 그리면 된다.

> `display` (요청 유형 체크박스)는 **없앴다.** 문서의 체크 표시를 정확히 읽지 못해 늘 미선택으로 나왔다. `REQUEST_TYPE` 도 다른 12개와 같이 상태와 근거만 준다.

### CPL 상태

| 값 | 뜻 |
|---|---|
| `PRESENT` | 필요한 내용 확인 |
| `MISSING` | 필요한 내용 누락 |
| `NOT_APPLICABLE` | 원문에 해당 없음이 명시됨 |
| `NEEDS_CONFIRMATION` | 원문은 있으나 확정하기 어려워 확인 필요 |

`PARSE_FAILED` 는 응답에 담지 않는다. 내부 보고서에만 보존한다.

### CPL 항목 코드

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

## 3.2 FIT — 내부 정합성 점검

요청서 내부 항목 간 논리적 연결 관계 7개의 점검 결과다.

```json
{
  "module_status": "AVAILABLE",
  "availability": { "assessable_count": 3, "total_count": 7 },
  "relations": []
}
```

| 필드 | 뜻 |
|---|---|
| `module_status` | `AVAILABLE` 또는 `UNAVAILABLE`. 결과 생성 여부 |
| `availability.assessable_count` | 실제 비교 결과를 낼 수 있었던 관계 수 |
| `availability.total_count` | 항상 `7` |
| `relations` | 관계별 결과 |

**점수는 담지 않는다.**

### `relations[]`

```json
{
  "relation_id": "FIT-1",
  "status": "NEEDS_REVIEW",
  "summary": "목적과 대상 범위의 연결이 명확하지 않습니다.",
  "left_evidence":  [{ "excerpt": "왼쪽 원문 근거" }],
  "right_evidence": [{ "excerpt": "오른쪽 원문 근거" }]
}
```

| 필드 | 뜻 |
|---|---|
| `relation_id` | 관계 코드 |
| `status` | 관계 점검 상태 |
| `summary` | 관계 설명 |
| `left_evidence` | 비교 왼쪽 항목의 원문 근거 |
| `right_evidence` | 비교 오른쪽 항목의 원문 근거 |

각 배열도 합쳐서 최대 1개다.

### FIT 관계 코드

| 코드 | 비교 내용 |
|---|---|
| `FIT-1` | 목적의 대상 조건 ↔ 지원 대상 |
| `FIT-2` | 목적 방향 ↔ 지원 활동·수단 |
| `FIT-3` | 목적 방향 ↔ 기대효과·성과지표 |
| `FIT-4` | 사업 계층 간 비교 |
| `FIT-5` | 대상군 ↔ 지원 조건 |
| `FIT-6` | 수행기관 ↔ 절차·역할 |
| `FIT-7` | 지원 내용 ↔ 지원 규모 정량값 |

### FIT 상태

| 값 | 뜻 |
|---|---|
| `FIT` | 연결 관계 확인 |
| `NEEDS_REVIEW` | 근거는 있으나 추가 검토 필요 |
| `CONFLICT` | 관계 충돌 확인 |
| `INSUFFICIENT` | 비교 정보 부족 |

## 3.3 SIM — 유사 공고 비교

```json
{
  "title": "2026년 ICT 미래시장 선점 R&D 지원사업 공고",
  "source_url": "https://www.bizinfo.go.kr/...",
  "comparison_summary": "후보 전체 비교 요약",
  "axes": {}
}
```

| 필드 | 뜻 |
|---|---|
| `title` | 후보 공고 제목 |
| `source_url` | **공고 원문 링크** |
| `comparison_summary` | 후보 전체 비교 요약 |
| `axes` | 4개 축 비교 결과 |

**순위와 유사도 점수는 담지 않는다.** 배열 순서가 곧 표시 순서다.

최대 5건이며, **빈 배열일 수 있다.** 접수 중인 공고 중 비교 대상이 없으면 그렇다. 오류가 아니다.

### `axes`

| 키 | 화면 이름 |
|---|---|
| `purpose` | 사업 목적 |
| `target` | 지원 대상 |
| `content` | 지원 내용 |
| `delivery` | 수행 체계 |

### 축별 구조

```json
{
  "status": "PARTIAL",
  "summary": "일부 공통점이 확인되었습니다.",
  "common_points": ["공통점"],
  "differences": ["차이점"],
  "request_evidence":   [{ "excerpt": "현재 요청서 원문" }],
  "candidate_evidence": [{ "excerpt": "후보 공고 원문" }]
}
```

| 필드 | 뜻 |
|---|---|
| `status` | 축별 비교 상태 |
| `summary` | 축별 비교 설명 |
| `common_points` | 확인된 공통점 |
| `differences` | 확인된 차이점 |
| `request_evidence` | 현재 요청서 원문 근거 |
| `candidate_evidence` | 후보 공고 원문 근거 |

### SIM 상태

| 값 | 뜻 |
|---|---|
| `SIMILAR` | 공통점 확인 |
| `PARTIAL` | 일부 공통점 확인 |
| `DIFFERENT` | 차이 확인 |
| `INSUFFICIENT` | 비교 정보 부족 |

> **통합 검토 이슈(`review_issues`)는 없앴다.** CPL·FIT·SIM 각 결과에 이미 상태와 근거가 들어 있어 같은 내용을 두 번 주는 것이었다.

---

# 4. AI 질의응답

## CHAT-01. 이전 대화 불러오기

```
GET /api/v1/cases/{case_id}/messages?cursor=...      인증 필요
```

| 쿼리 | 뜻 |
|---|---|
| `cursor` | 이전 응답의 `next_cursor` 를 그대로. 첫 호출은 비운다 |

**응답 `200`**

```json
{
  "messages": [
    { "id": 91, "role": "USER", "content": "이 사업의 지원 대상은?", "created_at": "..." },
    { "id": 92, "role": "ASSISTANT", "content": "요청서 기준으로...", "created_at": "..." }
  ],
  "next_cursor": "37"
}
```

| 필드 | 뜻 |
|---|---|
| `id` | 메시지 식별자 |
| `role` | `USER` 또는 `ASSISTANT` |
| `content` | 본문 |
| `created_at` | 작성 시각 |
| `next_cursor` | 더 이전 대화의 커서. **`null` 이면 처음까지 다 불러온 것** |

**한 번에 20개**다. `messages` 는 **시간순(오래된 것부터)** 이라 그대로 그리면 된다. 위로 스크롤할 때 `next_cursor` 를 넣어 더 이전 20개를 받는다. **이력 목록(CASE-03)과 방향이 반대다.**

CASE-04 의 `chat` 이 이미 최근 20개를 담고 있으므로, 처음 화면을 그릴 때는 이 API 를 부를 필요가 없다.

## CHAT-02. AI 에게 질문

```
POST /api/v1/cases/{case_id}/messages    인증 필요
```

**요청** — `{ "content": "이 사업의 지원 대상은?" }` (1~4000자)

**응답 `200`**

```json
{
  "user_message":      { "id": 93, "role": "USER", "content": "...", "created_at": "..." },
  "assistant_message": { "id": 94, "role": "ASSISTANT", "content": "...", "created_at": "..." }
}
```

**질문과 답을 함께 돌려준다.** 화면은 이 둘을 그대로 목록 끝에 붙이면 된다.

| 코드 | 언제 | 화면 |
|---|---|---|
| `200` | 답변 | 목록에 추가 |
| `404` | 없거나 남의 것 | 이력으로 |
| `409` | 아직 분석이 안 끝남 | 대기 화면으로 |
| `502` | AI 가 형식을 지키지 않음 | 재시도 안내 |
| `503` | AI 서비스 불가 또는 시간 초과 | 재시도 안내 |

---

# 5. 운영

| 메서드 | 경로 | 인증 | 용도 |
|---|---|---|---|
| GET | `/health/live` | 불필요 | 프로세스 살아 있는지. DB 를 보지 않는다 |
| GET | `/health/ready` | 불필요 | DB 까지 연결되는지 |

프론트가 쓸 일은 없다.

---

# 6. 전체 목록

| # | 기능 | 메서드 | 경로 | 인증 | 성공 |
|---|---|---|---|---|---|
| 1 | 로그인 | POST | `/api/v1/auth/login` | X | 200 |
| 2 | 세션 연장 | POST | `/api/v1/auth/refresh` | O | 200 |
| 3 | 로그인 사용자 확인 | GET | `/api/v1/auth/me` | O | 200 |
| 4 | 비밀번호 변경 | POST | `/api/v1/auth/change-password` | O | 200 |
| 5 | 비밀번호 재설정 요청 | POST | `/api/v1/auth/password-reset/request` | X | 200 |
| 6 | 업로드·분석 시작 | POST | `/api/v1/cases` | O | **202** |
| 7 | 진행 상태 (롱폴링) | GET | `/api/v1/cases/{case_id}/status` | O | 200 |
| 8 | 분석 이력 목록 | GET | `/api/v1/cases` | O | 200 |
| 9 | 분석 상세 | GET | `/api/v1/cases/{case_id}` | O | 200 |
| 10 | 보고서 PDF | GET | `/api/v1/cases/{case_id}/report` | O | 200 |
| 11 | 이전 대화 | GET | `/api/v1/cases/{case_id}/messages` | O | 200 |
| 12 | AI 질문 | POST | `/api/v1/cases/{case_id}/messages` | O | 200 |

# 7. 없앤 것

예전 문서나 초안에 있었다면 **지금은 없다.**

| 없앤 것 | 대신 |
|---|---|
| 공통 응답 래퍼 `{code, message, data, errors}` | 본문을 바로 준다 |
| `token_type` | 항상 Bearer |
| `login_id` 요청 필드 | `email` |
| `POST /auth/logout` | 프론트가 토큰 삭제 |
| `POST /cases/{id}/analyze` | 업로드하면 바로 시작 |
| `POST /auth/password-reset/confirm` | 임시 비밀번호를 메일로 |
| `GET /cases/{id}/report.pdf` | `/cases/{id}/report` |
| `/cases/{id}/chat/messages` | `/cases/{id}/messages` |
| CPL `confirmation_rate` | `confirmed_count` / `total_count` |
| CPL `display` (체크박스) | 상태와 근거만 |
| FIT 점수 | `availability` 만 |
| SIM `rank`, `relevance_score` | 배열 순서 |
| `review_issues` | 각 모듈 결과에 이미 있음 |
| 이력의 `status` | 완료 건만 담으므로 불필요 |

# 8. 아직 안 된 것

- **SIM 재료를 공고 원문 파싱으로 전환** — 팀원 작업 대기. 지금은 공고 메타데이터 기반이다.
- **중복수혜 문구 탐색** — 공고문에 `중복 수혜 불가` 류 문구가 있으면 알려주는 기능. 미착수.
- 공고 코퍼스가 **6건**뿐이다. 대량 동기화 전이라 SIM 후보가 비거나 적게 나올 수 있다.
