# Pre-review API 명세 — 현행

작성일: 2026-08-31 · 문서 버전: v1.0 · 서비스 API 14개 + 운영 API 2개

> **현재 백엔드의 실제 응답입니다.** 같은 폴더의 제안 규격 문서와 내용이 다릅니다.
> 토큰·ID·시각 등 본문 값은 예시이며 고정값이 아닙니다.

## 1. 먼저 보는 공통 규칙

### 응답 형식

업무 API 는 **성공·실패 모두 같은 4필드**로 응답합니다.

```json
{ "code": "SUCCESS", "message": "요청이 처리되었습니다.", "data": {}, "errors": [] }
```

| 필드 | 규칙 |
|---|---|
| `code` | 성공은 SUCCESS, 분석 접수는 ACCEPTED, 실패는 원인 코드 |
| `message` | 사람이 읽는 안내. 문자열로 분기하지 않는다 |
| `data` | 실제 결과. 결과 데이터가 없는 성공은 null |
| `errors` | 검증 오류의 필드별 정보. 그 외에는 빈 배열 |

HTTP 상태는 상태줄에 있으므로 본문에 넣지 않는다. response.status 로 읽는다.

예외는 둘뿐입니다.

| 경우 | 형식 |
|---|---|
| PDF 다운로드 성공 | application/pdf 바이너리. 실패는 공통 4필드 |
| 헬스체크 2개 | {"status": ...}. 모니터링용 별도 계약이라 통일 대상이 아니다 |

### 인증

```http
Authorization: Bearer <access_token>
```

인증이 필요 없는 API는 다음 5개뿐입니다.

- `POST /api/v1/auth/login`
- `POST /api/v1/auth/password-reset/request`
- `POST /api/v1/auth/password-reset/confirm`
- `GET /health/live`
- `GET /health/ready`

비밀번호가 바뀌면 그 이전에 발급된 토큰은 거부됩니다. 재로그인이 필요합니다.

### 오류 기본 처리

| 구분 | 프론트 행동 |
|---|---|
| 401 | 로그인 실패 또는 재로그인 안내 |
| 429 | Retry-After 만큼 기다린 뒤 재시도 안내 |
| 그 외 4xx | message 표시. 422 면 errors 배열을 해당 입력란에도 표시 |
| 5xx | 처리 불가 안내. 재시도 여부는 작업 성격에 따라 결정 |

## 2. API 한눈에 보기

| 번호 | 기능 | 메서드 | URL | 인증 | 성공 |
|---|---|---|---|---|---|
| 1 | 로그인 | POST | `/api/v1/auth/login` | 불필요 | 200 |
| 2 | 내 정보 조회 | GET | `/api/v1/auth/me` | 필요 | 200 |
| 3 | 비밀번호 변경 | POST | `/api/v1/auth/change-password` | 필요 | 200 |
| 4 | 로그아웃 | POST | `/api/v1/auth/logout` | 필요 | 200 |
| 5 | 비밀번호 재설정 요청 | POST | `/api/v1/auth/password-reset/request` | 불필요 | 200 |
| 6 | 비밀번호 재설정 확인 | POST | `/api/v1/auth/password-reset/confirm` | 불필요 | 200 |
| 7 | 문서 업로드·분석 건 생성 | POST | `/api/v1/cases` | 필요 | 200 |
| 8 | 최근 분석 이력 조회 | GET | `/api/v1/cases` | 필요 | 200 |
| 9 | 분석 시작 | POST | `/api/v1/cases/{case_id}/analyze` | 필요 | 202 |
| 10 | 분석 상태 조회 | GET | `/api/v1/cases/{case_id}/status` | 필요 | 200 |
| 11 | 분석 결과 조회 | GET | `/api/v1/cases/{case_id}/report` | 필요 | 200 |
| 12 | PDF 다운로드 | GET | `/api/v1/cases/{case_id}/report.pdf` | 필요 | 200 |
| 13 | 대화 이력 조회 | GET | `/api/v1/cases/{case_id}/chat/messages` | 필요 | 200 |
| 14 | 질문 전송 | POST | `/api/v1/cases/{case_id}/chat/messages` | 필요 | 200 |
| 15 | 서버 생존 확인 | GET | `/health/live` | 불필요 | 200 |
| 16 | 서버 준비 상태 확인 | GET | `/health/ready` | 불필요 | 200 |

## 3. API별 요청·응답

### 1. 로그인

**POST `/api/v1/auth/login`** · 인증 불필요

**요청**

login_id: 1~255자, password: 1~128자. 쿼리 파라미터 없음.

Content-Type: `application/json`

```json
{
  "login_id": "demo",
  "password": "example-password"
}
```

**성공 응답 — 200**

```json
{
  "code": "SUCCESS",
  "message": "요청이 처리되었습니다.",
  "data": {
    "access_token": "example-token-not-valid",
    "token_type": "bearer"
  },
  "errors": []
}
```

**실패 응답**

| HTTP | 응답 본문 | 발생 조건 |
|---|---|---|
| 401 | `{"code": "UNAUTHORIZED", "message": "인증 정보를 확인해 주세요.", "data": null, "errors": []}` | 아이디·비밀번호 불일치 또는 비활성 계정 |
| 422 | `{"code": "VALIDATION_ERROR", "message": "입력값을 확인해 주세요.", "data": null, "errors": [{"loc": ["b…` | 필수 필드 누락 또는 길이 위반 |

실패 응답에 붙는 헤더: `WWW-Authenticate`

**주의**

- 성공 응답의 access_token 을 이후 인증 API 의 Bearer 토큰으로 쓴다.
- 이 API 의 401 은 재로그인이 아니라 로그인 실패 안내로 처리한다.
- 401 응답에 WWW-Authenticate: Bearer 헤더가 붙는다.

---

### 2. 내 정보 조회

**GET `/api/v1/auth/me`** · 인증 필요

**요청**

본문 없음. 쿼리 파라미터 없음.

**성공 응답 — 200**

```json
{
  "code": "SUCCESS",
  "message": "요청이 처리되었습니다.",
  "data": {
    "id": 1,
    "login_id": "demo"
  },
  "errors": []
}
```

**실패 응답**

| HTTP | 응답 본문 | 발생 조건 |
|---|---|---|
| 401 | `{"code": "UNAUTHORIZED", "message": "인증 정보를 확인해 주세요.", "data": null, "errors": []}` | 토큰 누락·만료·유효하지 않음 |

실패 응답에 붙는 헤더: `WWW-Authenticate`

**주의**

- 보관 중인 토큰의 유효성 확인에도 쓸 수 있다.

---

### 3. 비밀번호 변경

**POST `/api/v1/auth/change-password`** · 인증 필요

**요청**

current_password: 1~128자, new_password: 8~128자, new_password 는 영문·숫자·특수문자를 각각 1자 이상 포함해야 함, 현재 비밀번호와 새 비밀번호는 서로 달라야 함. 쿼리 파라미터 없음.

Content-Type: `application/json`

```json
{
  "current_password": "example-old-password",
  "new_password": "example-new-password"
}
```

**성공 응답 — 200**

```json
{
  "code": "SUCCESS",
  "message": "요청이 처리되었습니다.",
  "data": null,
  "errors": []
}
```

**실패 응답**

| HTTP | 응답 본문 | 발생 조건 |
|---|---|---|
| 400 | `{"code": "BAD_REQUEST", "message": "요청을 처리할 수 없습니다.", "data": null, "errors": []}` | 현재 비밀번호 불일치 또는 기존과 동일한 새 비밀번호 |
| 401 | `{"code": "UNAUTHORIZED", "message": "인증 정보를 확인해 주세요.", "data": null, "errors": []}` | 토큰 누락·만료·유효하지 않음 |
| 422 | `{"code": "VALIDATION_ERROR", "message": "입력값을 확인해 주세요.", "data": null, "errors": [{"loc": ["b…` | 필수 필드 누락 또는 비밀번호 길이 위반 |

실패 응답에 붙는 헤더: `WWW-Authenticate`

**주의**

- 결과 데이터가 없는 성공이므로 data 는 null 이다.
- 변경 성공 후 기존 토큰은 서버가 거부한다. 저장된 토큰을 지우고 다시 로그인해야 한다.
- 400 은 형식 오류가 아니라 업무 규칙 거절이며 상세 사유를 구분해 알려주지 않는다.

---

### 4. 로그아웃

**POST `/api/v1/auth/logout`** · 인증 필요

**요청**

본문 없음. 쿼리 파라미터 없음.

**성공 응답 — 200**

```json
{
  "code": "SUCCESS",
  "message": "요청이 처리되었습니다.",
  "data": null,
  "errors": []
}
```

**실패 응답**

| HTTP | 응답 본문 | 발생 조건 |
|---|---|---|
| 401 | `{"code": "UNAUTHORIZED", "message": "인증 정보를 확인해 주세요.", "data": null, "errors": []}` | 토큰 누락·만료·유효하지 않음 |

실패 응답에 붙는 헤더: `WWW-Authenticate`

**주의**

- 결과 데이터가 없는 성공이므로 data 는 null 이다.
- 서버는 토큰 폐기 목록을 두지 않으므로 즉시 무효화를 보장하지 않는다. 프론트가 저장한 토큰을 지운다.

---

### 5. 비밀번호 재설정 요청

**POST `/api/v1/auth/password-reset/request`** · 인증 불필요

**요청**

email: 3~254자, 이메일 형식. 쿼리 파라미터 없음.

Content-Type: `application/json`

```json
{
  "email": "user@example.com"
}
```

**성공 응답 — 200**

```json
{
  "code": "SUCCESS",
  "message": "등록된 이메일이라면 비밀번호 재설정 안내가 발송됩니다.",
  "data": null,
  "errors": []
}
```

응답 헤더:

```http
Cache-Control: no-store
```

**실패 응답**

| HTTP | 응답 본문 | 발생 조건 |
|---|---|---|
| 422 | `{"code": "VALIDATION_ERROR", "message": "입력값을 확인해 주세요.", "data": null, "errors": [{"loc": ["b…` | 이메일 형식 오류 또는 필드 누락 |
| 429 | `{"code": "TOO_MANY_REQUESTS", "message": "요청이 너무 많습니다. 잠시 후 다시 시도해 주세요.", "data": null, "erro…` | 요청 제한 초과 (IP 또는 이메일 기준) |
| 503 | `{"message": "비밀번호 재설정 메일 서비스를 사용할 수 없습니다.", "code": "SERVICE_UNAVAILABLE", "data": null, "err…` | 메일 발송 설정이 없어 기능이 비활성 |

실패 응답에 붙는 헤더: `Cache-Control`, `Retry-After`

**주의**

- 계정 존재 여부와 무관하게 같은 200 메시지를 돌려준다. 이메일 존재 확인 용도로 쓸 수 없다.
- 메일은 백그라운드로 발송되므로 200 이 곧 발송 성공을 뜻하지 않는다.
- SMTP 5개 설정과 PASSWORD_RESET_URL 이 모두 있어야 동작한다. 하나라도 없으면 503 이다.
- 429 응답에는 Retry-After 헤더가 붙는다.
- 모든 응답에 Cache-Control: no-store 가 붙는다.

---

### 6. 비밀번호 재설정 확인

**POST `/api/v1/auth/password-reset/confirm`** · 인증 불필요

**요청**

token: 1~4096자, new_password: 8~128자, new_password 는 영문·숫자·특수문자를 각각 1자 이상 포함해야 함. 쿼리 파라미터 없음.

Content-Type: `application/json`

```json
{
  "token": "<메일 링크의 토큰>",
  "new_password": "example-new-password"
}
```

**성공 응답 — 200**

```json
{
  "code": "SUCCESS",
  "message": "비밀번호가 재설정되었습니다.",
  "data": null,
  "errors": []
}
```

응답 헤더:

```http
Cache-Control: no-store
```

**실패 응답**

| HTTP | 응답 본문 | 발생 조건 |
|---|---|---|
| 400 | `{"message": "비밀번호 재설정 링크가 유효하지 않거나 새 비밀번호가 기존 비밀번호와 같습니다.", "code": "BAD_REQUEST", "data": nu…` | 토큰이 유효하지 않거나 만료, 또는 기존과 같은 비밀번호 |
| 429 | `{"code": "TOO_MANY_REQUESTS", "message": "요청이 너무 많습니다. 잠시 후 다시 시도해 주세요.", "data": null, "erro…` | 요청 제한 초과 (IP 기준) |
| 422 | `{"code": "VALIDATION_ERROR", "message": "입력값을 확인해 주세요.", "data": null, "errors": [{"loc": ["b…` | 필드 누락 또는 비밀번호 정책 위반 |

실패 응답에 붙는 헤더: `Cache-Control`, `Retry-After`

**주의**

- 400 은 토큰 문제와 비밀번호 재사용을 구분하지 않는다. 메시지 하나로 합쳐 응답한다.
- 성공하면 그 계정의 기존 접근 토큰도 함께 무효화된다.
- new_password 제약은 비밀번호 변경(3번)과 같다. 길이 8~128자에 같은 정책 검증을 쓴다.

---

### 7. 문서 업로드·분석 건 생성

**POST `/api/v1/cases`** · 인증 필요

**요청**

파일 정확히 1개, HWP 또는 HWPX, 52,428,800바이트(50 MiB) 이하, 확장자와 실제 파일 형식을 모두 검사. 쿼리 파라미터 없음.

Content-Type: `multipart/form-data`

| 필드 | 타입 | 필수 | 값 |
|---|---|---|---|
| file | file | O | HWP/HWPX 파일 1개 |

**성공 응답 — 200**

```json
{
  "code": "SUCCESS",
  "message": "요청이 처리되었습니다.",
  "data": {
    "case_id": 123,
    "status": "UPLOADED"
  },
  "errors": []
}
```

**실패 응답**

| HTTP | 응답 본문 | 발생 조건 |
|---|---|---|
| 400 | `{"code": "BAD_REQUEST", "message": "요청을 처리할 수 없습니다.", "data": null, "errors": []}` | 파일이 2개 이상이거나 multipart 요청 오류 |
| 401 | `{"code": "UNAUTHORIZED", "message": "인증 정보를 확인해 주세요.", "data": null, "errors": []}` | 토큰 누락·만료·유효하지 않음 |
| 413 | `{"code": "PAYLOAD_TOO_LARGE", "message": "요청 크기가 허용 범위를 넘었습니다.", "data": null, "errors": []}` | 파일 또는 multipart 요청 전체 크기 초과 |
| 415 | `{"code": "UNSUPPORTED_MEDIA_TYPE", "message": "지원하지 않는 파일 형식입니다.", "data": null, "errors": []}` | HWP·HWPX 가 아니거나 확장자와 실제 형식 불일치 |
| 422 | `{"code": "VALIDATION_ERROR", "message": "입력값을 확인해 주세요.", "data": null, "errors": [{"loc": ["b…` | file 필드 누락 |

실패 응답에 붙는 헤더: `WWW-Authenticate`

**주의**

- 성공은 200 이다. 예전에는 201 이었다.
- file 이외의 필드명으로 붙인 추가 파일도 허용하지 않는다.
- FormData 를 쓸 때 Content-Type 은 브라우저가 boundary 와 함께 정하게 둔다.
- 성공 응답의 case_id 를 이후 API 의 경로 파라미터로 쓴다.

---

### 8. 최근 분석 이력 조회

**GET `/api/v1/cases`** · 인증 필요

**요청**

본문 없음. 쿼리 파라미터 없음.

**성공 응답 — 200**

```json
{
  "code": "SUCCESS",
  "message": "요청이 처리되었습니다.",
  "data": {
    "cases": [
      {
        "case_id": 2,
        "title": "예시_사전협의요청서.hwpx",
        "status": "분석 중",
        "created_at": "2026-08-31T07:13:35.137450Z"
      },
      {
        "case_id": 1,
        "title": "예시_사전협의요청서.hwpx",
        "status": "분석 완료",
        "created_at": "2026-08-31T06:11:28.191921Z"
      }
    ]
  },
  "errors": []
}
```

**실패 응답**

| HTTP | 응답 본문 | 발생 조건 |
|---|---|---|
| 401 | `{"code": "UNAUTHORIZED", "message": "인증 정보를 확인해 주세요.", "data": null, "errors": []}` | 토큰 누락·만료·유효하지 않음 |

실패 응답에 붙는 헤더: `WWW-Authenticate`

**주의**

- 본인 분석 건만 최신순으로 최대 50건 반환한다.
- title 은 원본 파일명이며 없으면 null 이다. 사업명 추출은 아직 없다.
- 이력이 없어도 cases 는 빈 배열이며 null 이 아니다.
- status 는 10번과 같은 3개 값이다. 업로드만 하고 시작하지 않은 건도 '분석 중' 으로 보인다.

---

### 9. 분석 시작

**POST `/api/v1/cases/{case_id}/analyze`** · 인증 필요

**요청**

경로 파라미터 `case_id`는 정수입니다. 본문 없음. 쿼리 파라미터 없음.

**성공 응답 — 202**

```json
{
  "code": "ACCEPTED",
  "message": "요청이 접수되었습니다.",
  "data": {
    "case_id": 123,
    "job_id": "example-job-id",
    "status": "PARSING"
  },
  "errors": []
}
```

**실패 응답**

| HTTP | 응답 본문 | 발생 조건 |
|---|---|---|
| 401 | `{"code": "UNAUTHORIZED", "message": "인증 정보를 확인해 주세요.", "data": null, "errors": []}` | 토큰 누락·만료·유효하지 않음 |
| 404 | `{"code": "NOT_FOUND", "message": "요청한 대상을 찾을 수 없습니다.", "data": null, "errors": []}` | 분석 건이 없거나 본인 소유가 아님 |
| 409 | `{"code": "CONFLICT", "message": "현재 상태에서는 처리할 수 없습니다.", "data": null, "errors": []}` | UPLOADED 가 아닌 상태에서 시작 요청 |
| 422 | `{"code": "VALIDATION_ERROR", "message": "입력값을 확인해 주세요.", "data": null, "errors": [{"loc": ["p…` | case_id 가 정수가 아님 |

실패 응답에 붙는 헤더: `WWW-Authenticate`

**주의**

- 202 는 접수 성공이며 분석 완료가 아니다. 이어서 10번으로 상태를 확인한다.
- 내부 상태가 UPLOADED 일 때만 시작할 수 있다.
- 이미 실패한 건도 409 다. 재분석 API 는 없으므로 7번으로 다시 업로드해야 한다.
- 409 의 message 는 고정 문구다. 내부 상태를 알려주지 않으므로 파싱해서 분기하지 않는다.
- 시작 여부 확인용으로 호출하지 않는다. 202 면 실제로 분석이 시작된다.

---

### 10. 분석 상태 조회

**GET `/api/v1/cases/{case_id}/status`** · 인증 필요

**요청**

경로 파라미터 `case_id`는 정수입니다. 본문 없음. 쿼리 파라미터 없음.

**성공 응답 — 200**

```json
{
  "code": "SUCCESS",
  "message": "요청이 처리되었습니다.",
  "data": {
    "case_id": 1,
    "status": "분석 완료",
    "failure_code": null,
    "failure_message": null
  },
  "errors": []
}
```

**분석 실패 조회 — 200**

```json
{
  "code": "SUCCESS",
  "message": "요청이 처리되었습니다.",
  "data": {
    "case_id": 123,
    "status": "분석 실패",
    "failure_code": "DOCUMENT_PARSE_FAILED",
    "failure_message": "The uploaded document could not be parsed"
  },
  "errors": []
}
```

**실패 응답**

| HTTP | 응답 본문 | 발생 조건 |
|---|---|---|
| 401 | `{"code": "UNAUTHORIZED", "message": "인증 정보를 확인해 주세요.", "data": null, "errors": []}` | 토큰 누락·만료·유효하지 않음 |
| 404 | `{"code": "NOT_FOUND", "message": "요청한 대상을 찾을 수 없습니다.", "data": null, "errors": []}` | 분석 건이 없거나 본인 소유가 아님 |
| 422 | `{"code": "VALIDATION_ERROR", "message": "입력값을 확인해 주세요.", "data": null, "errors": [{"loc": ["p…` | case_id 가 정수가 아님 |

실패 응답에 붙는 헤더: `WWW-Authenticate`

**주의**

- status 는 '분석 중', '분석 완료', '분석 실패' 3개 중 하나다.
- 내부 상태 대응 — 분석 중: UPLOADED, PARSING, CHECKING, RETRIEVING, REPORTING / 분석 완료: COMPLETED / 분석 실패: FAILED.
- 업로드만 하고 아직 시작하지 않은 건도 '분석 중' 으로 나온다. 이 값만으로 시작 여부를 알 수 없다.
- 분석 실패를 정상 조회해도 HTTP 는 200 이다.
- failure_code·failure_message 는 분석 자체의 실패이며 API 오류와 다르다.

---

### 11. 분석 결과 조회

**GET `/api/v1/cases/{case_id}/report`** · 인증 필요

**요청**

경로 파라미터 `case_id`는 정수입니다. 본문 없음. 쿼리 파라미터 없음.

**성공 응답 — 200**

```json
{
  "code": "SUCCESS",
  "message": "요청이 처리되었습니다.",
  "data": "<분석 결과 전체. 생략 없는 예시는 Pre-review_API_분석결과_전체예시_v0.2_260831.json 참조>",
  "errors": []
}
```

**실패 응답**

| HTTP | 응답 본문 | 발생 조건 |
|---|---|---|
| 401 | `{"code": "UNAUTHORIZED", "message": "인증 정보를 확인해 주세요.", "data": null, "errors": []}` | 토큰 누락·만료·유효하지 않음 |
| 404 | `{"code": "NOT_FOUND", "message": "요청한 대상을 찾을 수 없습니다.", "data": null, "errors": []}` | 분석 건이 없거나 본인 소유가 아님 |
| 409 | `{"code": "CONFLICT", "message": "현재 상태에서는 처리할 수 없습니다.", "data": null, "errors": []}` | 보고서가 아직 준비되지 않음 |
| 422 | `{"code": "VALIDATION_ERROR", "message": "입력값을 확인해 주세요.", "data": null, "errors": [{"loc": ["p…` | case_id 가 정수가 아님 |

실패 응답에 붙는 헤더: `WWW-Authenticate`

**주의**

- 분석 결과 전체가 data 안에 들어간다. 다른 API 와 같은 4필드다.
- 404 와 409 의 경계 — 분석 건이 없거나 남의 것이면 404, 건은 있는데 보고서가 아직이면 409 다. 12번도 같은 규칙이다.
- self_check 는 항상 13개 항목, structural_consistency 는 항상 7개 관계다.
- report_download_url 은 PDF 가 준비되기 전에는 null 이다.
- 필드 타입 정의는 Pre-review_API_분석결과_데이터스키마_v0.2_260831.json 을 참조한다.

---

### 12. PDF 다운로드

**GET `/api/v1/cases/{case_id}/report.pdf`** · 인증 필요

**요청**

경로 파라미터 `case_id`는 정수입니다. 본문 없음. 쿼리 파라미터 없음.

**성공 응답 — 200**

<PDF 바이너리>

응답 헤더:

```http
Content-Disposition: attachment; filename*=UTF-8''Pre-review_1.pdf
```

**실패 응답**

| HTTP | 응답 본문 | 발생 조건 |
|---|---|---|
| 401 | `{"code": "UNAUTHORIZED", "message": "인증 정보를 확인해 주세요.", "data": null, "errors": []}` | 토큰 누락·만료·유효하지 않음 |
| 404 | `{"code": "NOT_FOUND", "message": "요청한 대상을 찾을 수 없습니다.", "data": null, "errors": []}` | 분석 건이 없거나 본인 소유가 아님 |
| 409 | `{"code": "CONFLICT", "message": "현재 상태에서는 처리할 수 없습니다.", "data": null, "errors": []}` | 분석 또는 PDF 가 아직 준비되지 않음 |
| 422 | `{"code": "VALIDATION_ERROR", "message": "입력값을 확인해 주세요.", "data": null, "errors": [{"loc": ["p…` | case_id 가 정수가 아님 |
| 503 | `{"code": "SERVICE_UNAVAILABLE", "message": "서비스를 일시적으로 사용할 수 없습니다.", "data": null, "errors": []}` | 보고서 기록은 있으나 PDF 파일을 읽을 수 없음 |

실패 응답에 붙는 헤더: `WWW-Authenticate`

**주의**

- **성공만 PDF 바이너리이고 실패는 JSON 이다.** HTTP 상태와 Content-Type 을 먼저 확인한다.
- 파일명은 Content-Disposition 에 Pre-review_<case_id>.pdf 형태로 온다.
- Content-Disposition 은 서버의 expose_headers 설정 덕분에 스크립트로 읽을 수 있다.
- Bearer 인증이 필요하므로 단순 링크 이동으로는 헤더가 붙지 않는다.

---

### 13. 대화 이력 조회

**GET `/api/v1/cases/{case_id}/chat/messages`** · 인증 필요

**요청**

경로 파라미터 `case_id`는 정수입니다. 본문 없음. 쿼리 파라미터 없음.

**성공 응답 — 200**

```json
{
  "code": "SUCCESS",
  "message": "요청이 처리되었습니다.",
  "data": {
    "case_id": 1,
    "chat_session_id": null,
    "messages": []
  },
  "errors": []
}
```

**실패 응답**

| HTTP | 응답 본문 | 발생 조건 |
|---|---|---|
| 401 | `{"code": "UNAUTHORIZED", "message": "인증 정보를 확인해 주세요.", "data": null, "errors": []}` | 토큰 누락·만료·유효하지 않음 |
| 404 | `{"code": "NOT_FOUND", "message": "요청한 대상을 찾을 수 없습니다.", "data": null, "errors": []}` | 분석 건이 없거나 본인 소유가 아님 |
| 409 | `{"code": "CONFLICT", "message": "현재 상태에서는 처리할 수 없습니다.", "data": null, "errors": []}` | 완료된 보고서가 없어 대화를 쓸 수 없음 |
| 422 | `{"code": "VALIDATION_ERROR", "message": "입력값을 확인해 주세요.", "data": null, "errors": [{"loc": ["p…` | case_id 가 정수가 아님 |

실패 응답에 붙는 헤더: `WWW-Authenticate`

**주의**

- 보고서가 준비된 뒤에만 쓸 수 있다.
- 대화가 아직 없으면 chat_session_id 는 null 이고 messages 는 빈 배열이다.
- messages 는 sequence_no 오름차순이다.

---

### 14. 질문 전송

**POST `/api/v1/cases/{case_id}/chat/messages`** · 인증 필요

**요청**

경로 파라미터 `case_id`는 정수입니다. content: 1~4000자, 정의되지 않은 추가 필드 금지. 쿼리 파라미터 없음.

Content-Type: `application/json`

```json
{
  "content": "확인이 필요한 항목을 알려줘"
}
```

**성공 응답 — 200**

```json
{
  "code": "SUCCESS",
  "message": "요청이 처리되었습니다.",
  "data": {
    "case_id": 123,
    "chat_session_id": 10,
    "user_message": "<메시지 객체>",
    "assistant_message": "<메시지 객체>"
  },
  "errors": []
}
```

**실패 응답**

| HTTP | 응답 본문 | 발생 조건 |
|---|---|---|
| 401 | `{"code": "UNAUTHORIZED", "message": "인증 정보를 확인해 주세요.", "data": null, "errors": []}` | 토큰 누락·만료·유효하지 않음 |
| 404 | `{"code": "NOT_FOUND", "message": "요청한 대상을 찾을 수 없습니다.", "data": null, "errors": []}` | 분석 건이 없거나 본인 소유가 아님 |
| 409 | `{"code": "CONFLICT", "message": "현재 상태에서는 처리할 수 없습니다.", "data": null, "errors": []}` | 완료된 보고서가 없어 대화를 쓸 수 없음 |
| 422 | `{"code": "VALIDATION_ERROR", "message": "입력값을 확인해 주세요.", "data": null, "errors": [{"loc": ["b…` | 필드 누락, 길이 위반, 또는 알 수 없는 추가 필드 |
| 502 | `{"code": "LLM_INVALID_RESPONSE", "message": "The chat model returned an invalid response", "d…` | 모델 응답의 형식 또는 근거가 유효하지 않음 |
| 503 | `{"code": "LLM_UNAVAILABLE", "message": "The chat model is unavailable", "data": null, "errors…` | 모델을 쓸 수 없거나 응답 시간 초과 |

실패 응답에 붙는 헤더: `WWW-Authenticate`

**주의**

- 필드명은 content 이며 message 가 아니다.
- 모델 실패는 최상위 code 로 구분한다 — 502 는 LLM_INVALID_RESPONSE, 503 은 LLM_UNAVAILABLE 또는 LLM_TIMEOUT 이다.
- 공백만 있는 질문은 프론트에서 막는다. 현재 서버는 이를 입력 오류로 처리하지 않는다.
- 답변 생성에 시간이 걸린다. 요청 타임아웃을 넉넉히 잡는다.

---

### 15. 서버 생존 확인

**GET `/health/live`** · 인증 불필요

**요청**

본문 없음. 쿼리 파라미터 없음.

**성공 응답 — 200**

```json
{
  "status": "ok"
}
```

**실패 응답**

별도 업무 오류가 없습니다. 서버·통신 장애는 공통 오류 처리 대상입니다.

**주의**

- 서버가 응답하는지만 본다. DB·모델·분석 기능을 검사하지 않는다.
- 프론트의 필수 호출 절차가 아니다.

---

### 16. 서버 준비 상태 확인

**GET `/health/ready`** · 인증 불필요

**요청**

본문 없음. 쿼리 파라미터 없음.

**성공 응답 — 200**

```json
{
  "status": "ready"
}
```

**실패 응답**

| HTTP | 응답 본문 | 발생 조건 |
|---|---|---|
| 503 | `{"status": "unavailable"}` | DB 연결 실패 또는 필수 테이블(sims.app_user) 없음 |

**주의**

- DB 연결과 필수 테이블 존재만 검사한다. LLM·스토리지·분석 전체를 보장하지 않는다.
- **응답이 공통 4필드가 아니라 status 필드 하나다.** 모니터링용 별도 계약이다.

---

## 4. 프론트 호출 순서

1. 로그인 → access_token 확보

2. 문서 업로드(200) → case_id 확보

3. 분석 시작(202)

4. 상태 조회를 반복. '분석 완료' 면 결과 조회, '분석 실패' 면 failure_message 표시 후 재업로드 안내

5. 분석 결과 조회 → 화면 표시

6. report_download_url 이 있으면 인증 헤더를 붙여 PDF 다운로드

7. 보고서가 준비된 건에서 질문 전송·대화 이력 조회

## 5. 분석 상태값

| 표시 status | 서버 내부 상태 | 주의 |
|---|---|---|
| 분석 중 | UPLOADED, PARSING, CHECKING, RETRIEVING, REPORTING | 아직 시작하지 않은 건도 여기 포함된다 |
| 분석 완료 | COMPLETED | — |
| 분석 실패 | FAILED | 재분석 API 가 없어 새로 업로드해야 한다 |

상태는 3개뿐이고 **업로드만 하고 시작하지 않은 건도 '분석 중'** 입니다.
이 값만으로 분석 시작 여부를 알 수 없습니다.

기계 판독용 명세는 [Pre-review_API_명세_현행_v1.0_260831.json](Pre-review_API_명세_현행_v1.0_260831.json)에 있습니다.
