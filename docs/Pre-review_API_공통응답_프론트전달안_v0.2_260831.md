# Pre-review API 명세 — 공통 JSON 응답

작성일: 2026-08-31 · 문서 버전: v0.2 · 서비스 API 12개 + 운영 API 2개

> **프론트 협의용 새 규격 초안입니다. 아직 서버에 적용되지 않았습니다.**
> 본문의 HTTP 코드, 공통 JSON, 안내 문구는 적용 목표입니다.
> 예시의 계정·토큰·ID·시간·분석 결과는 설명용이며 실제 운영 데이터가 아닙니다.

## 1. 먼저 보는 공통 규칙

### 현재 코드와 적용 목표

**아래 API 표와 JSON 예시는 적용 목표입니다. 현재 서버 응답으로 간주하지 마세요.**
현재 백엔드 코드와의 차이는 다음과 같으며, 실행 서버의 반영 여부는 배포 시 확인합니다.

| 구분 | 현재 백엔드 코드 | 적용 목표 |
|---|---|---|
| 업로드 성공 | 201, case_id·status를 직접 반환 | 200, 공통 5필드의 data에 결과 포함 |
| 비밀번호 변경·로그아웃 성공 | 204, 본문 없음 | 200, 공통 5필드, data는 null |
| 그 외 JSON 성공·분석 접수 | API별 결과 객체를 직접 반환 | 200 또는 202, 공통 5필드로 감싸서 반환 |
| 오류 | 일반 오류는 detail에 문자열·배열·객체. 헬스체크 오류는 별도 객체이며 미처리 500은 텍스트일 수 있음 | 원인별 4xx·5xx 유지, 공통 5필드로 통일 |

### 요청과 응답

- GET은 조회, POST는 등록·처리 요청에 사용합니다. 같은 URL도 메서드가 다르면 별개 API입니다.
- 일반 성공은 **200**, 분석 시작 접수는 **202**로 통일합니다.
- 실패는 원인에 맞는 **4xx·5xx**를 사용합니다. 실패를 200으로 반환하지 않습니다.
- **모든 JSON 응답은 아래 5개 필드를 항상 포함**합니다.
- **PDF 다운로드 성공만** JSON이 아닌 PDF 파일을 반환합니다. PDF 다운로드 실패는 공통 JSON입니다.
- 로그인과 헬스체크 2개를 제외한 모든 API는 Bearer 토큰이 필요합니다.
- 요청 JSON이 필요한 API는 로그인·비밀번호 변경·질문 전송입니다. 업로드는 multipart/form-data입니다.
- 별도 쿼리 파라미터는 없습니다. 날짜·시각은 ISO 8601 문자열입니다.

### 공통 응답 5개 필드

| 필드 | 타입 | 규칙 |
|---|---|---|
| `status_code` | number | 실제 HTTP 상태 코드와 동일 |
| `message` | string | 사람이 읽는 안내. 프론트의 동작 분기 기준으로 쓰지 않음 |
| `code` | string | 성공은 SUCCESS, 접수는 ACCEPTED, 실패는 아래 공통 코드 |
| `data` | object 또는 null | API별 결과. 실패 또는 반환 데이터 없는 성공이면 null |
| `errors` | array | 필드별 입력 오류. 없으면 항상 [] |

성공 예시:

```json
{
  "status_code": 200,
  "message": "문서가 업로드되었습니다.",
  "code": "SUCCESS",
  "data": {
    "case_id": 123,
    "status": "UPLOADED"
  },
  "errors": []
}
```

실패 예시:

```json
{
  "status_code": 415,
  "message": "HWP 또는 HWPX 파일을 업로드해 주세요.",
  "code": "UNSUPPORTED_MEDIA_TYPE",
  "data": null,
  "errors": []
}
```

입력 오류 예시:

```json
{
  "status_code": 422,
  "message": "입력값을 확인해 주세요.",
  "code": "VALIDATION_ERROR",
  "data": null,
  "errors": [
    {
      "loc": [
        "body",
        "content"
      ],
      "msg": "질문을 입력해 주세요.",
      "type": "missing"
    }
  ]
}
```

`errors[]`의 필드는 다음으로 고정합니다.

| 필드 | 타입 | 용도 |
|---|---|---|
| `loc` | (string 또는 number) 배열 | 오류 위치. 예: ["body", "content"], ["path", "case_id"] |
| `msg` | string | 해당 입력 오류 설명 |
| `type` | string | 검증 오류 종류. missing, int_parsing 등 |

`errors`는 배열 자체를 문자열로 출력하지 말고 각 `msg`를 표시합니다.
비밀번호·토큰·사용자 입력 원문·내부 예외 상세는 오류에 다시 담지 않습니다.
문자열·숫자 값의 예시를 고정값으로 취급하지 않습니다.

### 인증 헤더

```http
Authorization: Bearer <access_token>
```

401 응답에서는 `WWW-Authenticate: Bearer` 헤더를 유지합니다.
로그인 API의 401은 로그인 실패 안내로 처리하고, 인증된 화면의 401은 재로그인을 안내합니다.
토큰 보관 방식 등 보안 정책을 이 문서에서 새로 정하지 않습니다.

## 2. 성공·실패 코드표

| HTTP | code | 의미 |
|---|---|---|
| 200 | SUCCESS | 요청 처리 성공 |
| 202 | ACCEPTED | 분석 요청 접수 성공. 완료 여부는 상태 조회로 확인 |
| 400 | BAD_REQUEST | 요청 조건·내용 오류 |
| 401 | UNAUTHORIZED | 로그인 또는 토큰 인증 실패 |
| 404 | NOT_FOUND | 요청한 대상을 찾을 수 없거나 접근 가능한 대상이 아님 |
| 409 | CONFLICT | 현재 상태에서 요청 처리 불가 |
| 413 | PAYLOAD_TOO_LARGE | 파일 또는 업로드 요청 크기 초과 |
| 415 | UNSUPPORTED_MEDIA_TYPE | 지원하지 않거나 유효하지 않은 파일 형식 |
| 422 | VALIDATION_ERROR | 필수 입력·타입·길이 등 검증 실패 |
| 500 | INTERNAL_ERROR | 예상하지 못한 서버 오류 |
| 502 | LLM_INVALID_RESPONSE | 답변 모델의 응답 형식·근거 오류 |
| 503 | SERVICE_UNAVAILABLE | DB·PDF 등 서비스 이용 불가 |
| 503 | LLM_UNAVAILABLE | 답변 모델 이용 불가 |
| 503 | LLM_TIMEOUT | 답변 모델 응답 시간 초과 |

**성공 코드를 API마다 새로 만들지 않습니다.**
로그인·업로드·조회 모두 SUCCESS이고, 어떤 작업인지는 호출한 API와 data로 구분합니다.
실패도 HTTP별 공통 code를 사용하되, 이미 구분 가능한 모델 실패 원인만 보존합니다.

프론트 기본 오류 처리는 세 갈래면 됩니다.

| 구분 | 기본 처리 |
|---|---|
| 401 | 로그인 실패 또는 재로그인 안내 |
| 그 외 4xx | message 표시. errors가 있으면 해당 입력란에도 표시 |
| 5xx | 처리 불가 안내. 재시도는 작업 성격에 따라 사용자에게 제공 |

모든 code마다 분기를 만들 필요는 없습니다. 필요한 화면에서만 추가 분기합니다.
409라고 무조건 재요청하지 않습니다. 분석 시작 불가와 보고서 미준비는 호출 API에 따라 대응합니다.
POST는 네트워크 오류 후 무조건 자동 재전송하지 않습니다. 이미 처리됐을 수 있으므로 현재 상태·이력을 먼저 확인합니다.
네트워크 단절·CORS 차단·프록시 오류처럼 JSON을 받지 못하는 경우는 프론트의 별도 통신 오류로 처리합니다.

각 API 아래에는 정상 사용 경로에서 예상하는 오류만 적습니다.
500은 공통 예외이며, 잘못된 URL·메서드 등 기반 HTTP 오류까지 코드표가 한정하는 것은 아닙니다.
서버가 처리하는 JSON 오류는 공통 형식으로 맞추되, 405의 Allow 같은 HTTP 헤더 의미는 보존합니다.

## 3. API 한눈에 보기

| 번호 | 기능 | 메서드 | URL | 인증 | 성공 |
|---|---|---|---|---|---|
| 1 | 로그인 | POST | `/api/v1/auth/login` | 불필요 | 200 · SUCCESS |
| 2 | 내 정보 조회 | GET | `/api/v1/auth/me` | 필요 | 200 · SUCCESS |
| 3 | 비밀번호 변경 | POST | `/api/v1/auth/change-password` | 필요 | 200 · SUCCESS |
| 4 | 로그아웃 | POST | `/api/v1/auth/logout` | 필요 | 200 · SUCCESS |
| 5 | 문서 업로드·분석 건 생성 | POST | `/api/v1/cases` | 필요 | 200 · SUCCESS |
| 6 | 최근 분석 이력 조회 | GET | `/api/v1/cases` | 필요 | 200 · SUCCESS |
| 7 | 분석 시작 | POST | `/api/v1/cases/{case_id}/analyze` | 필요 | 202 · ACCEPTED |
| 8 | 분석 상태 조회 | GET | `/api/v1/cases/{case_id}/status` | 필요 | 200 · SUCCESS |
| 9 | 분석 결과 조회 | GET | `/api/v1/cases/{case_id}/report` | 필요 | 200 · SUCCESS |
| 10 | PDF 다운로드 | GET | `/api/v1/cases/{case_id}/report.pdf` | 필요 | 200 · PDF |
| 11 | 대화 이력 조회 | GET | `/api/v1/cases/{case_id}/chat/messages` | 필요 | 200 · SUCCESS |
| 12 | 질문 전송 | POST | `/api/v1/cases/{case_id}/chat/messages` | 필요 | 200 · SUCCESS |
| 13 | 서버 생존 확인 | GET | `/health/live` | 불필요 | 200 · SUCCESS |
| 14 | 서버 준비 상태 확인 | GET | `/health/ready` | 불필요 | 200 · SUCCESS |

## 4. API별 요청·응답

아래의 JSON 코드는 주석·생략 기호 없이 복사할 수 있는 예시입니다.
`case_id`는 업로드 결과에서 받은 실제 번호로 바꿔서 호출합니다.
파일 업로드·PDF 본문은 JSON으로 변환하지 않습니다.

### 1. 로그인

**POST `/api/v1/auth/login`** · 인증 불필요

**요청**

login_id는 1~255자, password는 1~128자입니다. 예시 계정·비밀번호는 실제 접속 정보가 아닙니다.

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
  "status_code": 200,
  "message": "로그인되었습니다.",
  "code": "SUCCESS",
  "data": {
    "access_token": "example-token-not-valid",
    "token_type": "bearer"
  },
  "errors": []
}
```

**실패 응답**

| HTTP | code | 발생 조건 |
|---|---|---|
| 401 | UNAUTHORIZED | 아이디·비밀번호 불일치 또는 사용할 수 없는 계정 |
| 422 | VALIDATION_ERROR | 입력 필드 또는 경로 값 검증 실패 |

---

### 2. 내 정보 조회

**GET `/api/v1/auth/me`** · 인증 필요

**요청**

본문·쿼리 파라미터 없음.

**성공 응답 — 200**

```json
{
  "status_code": 200,
  "message": "내 정보를 조회했습니다.",
  "code": "SUCCESS",
  "data": {
    "id": 1,
    "login_id": "demo"
  },
  "errors": []
}
```

**실패 응답**

| HTTP | code | 발생 조건 |
|---|---|---|
| 401 | UNAUTHORIZED | 토큰 누락·만료·유효하지 않음 |

---

### 3. 비밀번호 변경

**POST `/api/v1/auth/change-password`** · 인증 필요

**요청**

current_password는 1~128자, new_password는 12~128자입니다. 현재 비밀번호와 새 비밀번호는 달라야 합니다. 변경 성공 후 기존 토큰은 더 이상 유효하지 않을 수 있으므로 저장된 토큰을 제거하고 다시 로그인합니다. 별도의 토큰 발급은 이 응답에 포함하지 않습니다.

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
  "status_code": 200,
  "message": "비밀번호가 변경되었습니다.",
  "code": "SUCCESS",
  "data": null,
  "errors": []
}
```

**실패 응답**

| HTTP | code | 발생 조건 |
|---|---|---|
| 400 | BAD_REQUEST | 현재 비밀번호 불일치 또는 기존과 같은 새 비밀번호 |
| 401 | UNAUTHORIZED | 토큰 누락·만료·유효하지 않음 |
| 422 | VALIDATION_ERROR | 필수 필드·타입·비밀번호 길이 검증 실패 |

---

### 4. 로그아웃

**POST `/api/v1/auth/logout`** · 인증 필요

**요청**

본문·쿼리 파라미터 없음. 성공 후 프론트가 보관한 토큰을 제거합니다. 현재 서버는 로그아웃 시 토큰 폐기 목록을 기록하지 않으므로 서버 측 즉시 토큰 무효화를 보장하지 않습니다.

**성공 응답 — 200**

```json
{
  "status_code": 200,
  "message": "로그아웃되었습니다.",
  "code": "SUCCESS",
  "data": null,
  "errors": []
}
```

**실패 응답**

| HTTP | code | 발생 조건 |
|---|---|---|
| 401 | UNAUTHORIZED | 토큰 누락·만료·유효하지 않음 |

---

### 5. 문서 업로드·분석 건 생성

**POST `/api/v1/cases`** · 인증 필요

**요청**

HWP 또는 HWPX 파일 한 개를 file 필드로 보냅니다. 파일 최대 크기는 52,428,800바이트(50 MiB, 기존 안내의 50MB)입니다. 확장자뿐 아니라 실제 파일 형식도 검사합니다. 다른 필드명으로 첨부한 추가 파일도 허용하지 않습니다. 브라우저 FormData 사용 시 Content-Type은 브라우저가 boundary와 함께 설정하게 둡니다. 요청은 JSON으로 변경하지 않습니다.

| 필드 | 타입 | 필수 | 값 |
|---|---|---|---|
| file | 파일 | O | HWP/HWPX 파일 한 개 |

**성공 응답 — 200**

```json
{
  "status_code": 200,
  "message": "문서가 업로드되었습니다.",
  "code": "SUCCESS",
  "data": {
    "case_id": 123,
    "status": "UPLOADED"
  },
  "errors": []
}
```

**실패 응답**

| HTTP | code | 발생 조건 |
|---|---|---|
| 400 | BAD_REQUEST | 여러 파일, 빈 파일, 잘못된 파일명 또는 multipart 요청 오류 |
| 401 | UNAUTHORIZED | 토큰 누락·만료·유효하지 않음 |
| 413 | PAYLOAD_TOO_LARGE | 파일 크기 초과 또는 전체 multipart 요청 크기 제한 초과 |
| 415 | UNSUPPORTED_MEDIA_TYPE | 지원하지 않는 형식, 손상된 형식 또는 확장자와 실제 형식 불일치 |
| 422 | VALIDATION_ERROR | file 필드 누락 또는 파일 필드 검증 실패 |

---

### 6. 최근 분석 이력 조회

**GET `/api/v1/cases`** · 인증 필요

**요청**

본문·쿼리 파라미터 없음. 본인 분석 건을 최신순으로 최대 50건 조회합니다. title은 원본 파일명이며 없으면 null입니다. 이력이 없으면 data는 null이 아니라 {"cases": []}입니다.

**성공 응답 — 200**

```json
{
  "status_code": 200,
  "message": "분석 이력을 조회했습니다.",
  "code": "SUCCESS",
  "data": {
    "cases": [
      {
        "case_id": 123,
        "title": "예시_사전협의요청서.hwpx",
        "status": "분석 완료",
        "created_at": "2026-08-31T09:00:00+09:00"
      }
    ]
  },
  "errors": []
}
```

**실패 응답**

| HTTP | code | 발생 조건 |
|---|---|---|
| 401 | UNAUTHORIZED | 토큰 누락·만료·유효하지 않음 |

---

### 7. 분석 시작

**POST `/api/v1/cases/{case_id}/analyze`** · 인증 필요

**요청**

case_id는 정수 경로 파라미터입니다. 본문 없음. 202는 접수 성공이며 분석 완료가 아닙니다. 반환되는 data.status는 PARSING입니다. 성공 후 8번 상태 조회를 시작합니다. job_id는 불투명 문자열이며 별도 job 조회 API는 없습니다. UPLOADED 상태에서만 분석을 시작할 수 있습니다.

**성공 응답 — 202**

```json
{
  "status_code": 202,
  "message": "분석 요청이 접수되었습니다.",
  "code": "ACCEPTED",
  "data": {
    "case_id": 123,
    "job_id": "example-job-123",
    "status": "PARSING"
  },
  "errors": []
}
```

**실패 응답**

| HTTP | code | 발생 조건 |
|---|---|---|
| 401 | UNAUTHORIZED | 토큰 누락·만료·유효하지 않음 |
| 404 | NOT_FOUND | 분석 건이 없거나 본인 소유가 아님 |
| 409 | CONFLICT | 현재 상태에서 분석 시작 불가: 이미 진행·완료·실패한 건 등 |
| 422 | VALIDATION_ERROR | 입력 필드 또는 경로 값 검증 실패 |

---

### 8. 분석 상태 조회

**GET `/api/v1/cases/{case_id}/status`** · 인증 필요

**요청**

case_id는 정수입니다. 본문 없음. status는 ‘분석 중’, ‘분석 완료’, ‘분석 실패’ 중 하나입니다. 분석 실패를 정상 조회해도 HTTP는 200 / code는 SUCCESS입니다. failure_code·failure_message는 분석 실패일 때 확인하며 API 오류 code·message와 구분합니다.

**아직 분석을 시작하지 않은 UPLOADED 건도 ‘분석 중’으로 표시됩니다.**
폴링은 해당 건의 7번 분석 시작 API에서 **202 응답을 받은 뒤에만** 시작합니다.
목록이나 상태 조회의 ‘분석 중’ 값만으로 분석이 시작됐다고 판단하지 않습니다.
새로고침 등으로 시작 성공 여부를 확인할 수 없으면 현재 상태값만으로는 미시작과 진행 중을 구분할 수 없습니다.

**성공 응답 — 200**

```json
{
  "status_code": 200,
  "message": "분석 상태를 조회했습니다.",
  "code": "SUCCESS",
  "data": {
    "case_id": 123,
    "status": "분석 중",
    "failure_code": null,
    "failure_message": null
  },
  "errors": []
}
```

**분석 완료 조회도 HTTP 200입니다.**

```json
{
  "status_code": 200,
  "message": "분석 상태를 조회했습니다.",
  "code": "SUCCESS",
  "data": {
    "case_id": 123,
    "status": "분석 완료",
    "failure_code": null,
    "failure_message": null
  },
  "errors": []
}
```

**분석 실패 조회도 HTTP 200입니다.**

```json
{
  "status_code": 200,
  "message": "분석 상태를 조회했습니다.",
  "code": "SUCCESS",
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

| HTTP | code | 발생 조건 |
|---|---|---|
| 401 | UNAUTHORIZED | 토큰 누락·만료·유효하지 않음 |
| 404 | NOT_FOUND | 분석 건이 없거나 본인 소유가 아님 |
| 422 | VALIDATION_ERROR | 입력 필드 또는 경로 값 검증 실패 |

---

### 9. 분석 결과 조회

**GET `/api/v1/cases/{case_id}/report`** · 인증 필요

**요청**

case_id는 정수입니다. 본문 없음. 분석 결과 전체를 data에 담습니다. PDF가 준비되면 report_download_url에 다운로드 경로가 제공되며, 준비되지 않으면 null입니다.

**성공 응답 — 200**

분석 결과는 크기가 커서 **생략 없는 전체 응답을 별도 파일**로 제공합니다.

- [분석 결과 전체 JSON 예시](Pre-review_API_분석결과_전체예시_v0.2_260831.json)
- [분석 결과 data의 전체 JSON Schema](Pre-review_API_분석결과_데이터스키마_v0.2_260831.json)

| data 내부 필드 | 내용 |
|---|---|
| schema_version | alpha-report-v0.1 |
| case | 분석 건 번호·제목·생성·완료 시각 |
| ui_status | COMPLETED |
| self_check | CPL 확인 수·확인율·13개 항목·근거·버전·경고 |
| structural_consistency | FIT 모듈 상태·점수·7개 관계·근거·버전·경고 |
| review_issues | 검토 쟁점 배열 |
| similar_candidates | 유사사업 후보 배열. 후보별 목적·대상·내용·수행체계 비교 포함 |
| ben_references | 알파에서는 항상 [] |
| differences | 알파에서는 항상 []. 후보별 차이는 axes 내부에서 확인 |
| warnings | 분석 경고 배열 |
| report_download_url | PDF 다운로드 경로. 준비되지 않으면 null |

전체 예시는 **정보가 부족하고 유사 후보가 없는 가상 사례**입니다.
실제 분석 실행 결과나 품질 평가 자료가 아닙니다. 13개 CPL 항목과 7개 FIT 관계를 생략하지 않았습니다.
`example-only` 버전은 예시 표시용이고, 실제 응답의 버전은 실행 기록의 값입니다.
유사 후보·Evidence 등 예시에서 빈 배열인 항목의 전체 필드는 JSON Schema에서 확인할 수 있습니다.

**실패 응답**

| HTTP | code | 발생 조건 |
|---|---|---|
| 401 | UNAUTHORIZED | 토큰 누락·만료·유효하지 않음 |
| 404 | NOT_FOUND | 분석 건이 없거나 본인 소유가 아님 |
| 409 | CONFLICT | 보고서가 아직 준비되지 않았거나 조회 가능한 보고서가 없음 |
| 422 | VALIDATION_ERROR | 입력 필드 또는 경로 값 검증 실패 |

---

### 10. PDF 다운로드

**GET `/api/v1/cases/{case_id}/report.pdf`** · 인증 필요

**요청**

case_id는 정수입니다. 본문 없음. 성공 시 JSON 대신 PDF 바이너리를 반환하는 유일한 예외입니다. 실패 시에는 다른 API와 같은 공통 JSON입니다. 프론트는 HTTP 상태와 Content-Type을 확인한 뒤 PDF는 Blob으로, 오류 JSON은 JSON으로 읽습니다. Bearer 인증이 필요하므로 단순 링크 이동만으로 Authorization 헤더가 자동 추가되지는 않습니다.

**성공 응답 — 200**

```http
HTTP/1.1 200 OK
Content-Type: application/pdf
Content-Disposition: attachment; filename*=UTF-8''report.pdf
```

본문은 실제 PDF 바이너리입니다. 위 파일명은 예시입니다.

허용된 프론트 Origin에서는 서버의 `expose_headers=["Content-Disposition"]` 설정 덕분에 JavaScript로 해당 헤더를 읽어 파일명을 얻을 수 있습니다.
Origin·허용 헤더·Bearer 요청 설정은 [CORS 연동 계약](CORS_연동계약_v0.1_260831.md)을 함께 확인합니다. CORS 사전 요청 OPTIONS는 미들웨어가 처리하며 14개 업무·운영 API와 별개입니다.

**실패 응답**

| HTTP | code | 발생 조건 |
|---|---|---|
| 401 | UNAUTHORIZED | 토큰 누락·만료·유효하지 않음 |
| 404 | NOT_FOUND | 분석 건이 없거나 본인 소유가 아님 |
| 409 | CONFLICT | 분석 또는 PDF가 아직 준비되지 않음 |
| 422 | VALIDATION_ERROR | 입력 필드 또는 경로 값 검증 실패 |
| 503 | SERVICE_UNAVAILABLE | PDF 파일을 사용할 수 없음 |

---

### 11. 대화 이력 조회

**GET `/api/v1/cases/{case_id}/chat/messages`** · 인증 필요

**요청**

case_id는 정수입니다. 본문 없음. 보고서가 준비된 후 이용합니다. 대화가 아직 없으면 chat_session_id는 null, messages는 []입니다. 대화가 있으면 아래 12번의 메시지 객체 형태로 sequence_no 순서대로 반환합니다.

**성공 응답 — 200**

```json
{
  "status_code": 200,
  "message": "대화 이력을 조회했습니다.",
  "code": "SUCCESS",
  "data": {
    "case_id": 123,
    "chat_session_id": null,
    "messages": []
  },
  "errors": []
}
```

**실패 응답**

| HTTP | code | 발생 조건 |
|---|---|---|
| 401 | UNAUTHORIZED | 토큰 누락·만료·유효하지 않음 |
| 404 | NOT_FOUND | 분석 건이 없거나 본인 소유가 아님 |
| 409 | CONFLICT | 조회 가능한 완료 보고서가 없어 대화 이용 불가 |
| 422 | VALIDATION_ERROR | 입력 필드 또는 경로 값 검증 실패 |

---

### 12. 질문 전송

**POST `/api/v1/cases/{case_id}/chat/messages`** · 인증 필요

**요청**

case_id는 정수입니다. content는 1~4000자이며 message라는 필드가 아닙니다. 알 수 없는 추가 필드는 허용하지 않습니다. 공백만 있는 질문은 프론트에서 전송하지 않습니다. 답변은 현재 분석 결과에 근거합니다.

Content-Type: `application/json`

```json
{
  "content": "확인이 필요한 항목을 알려줘"
}
```

**성공 응답 — 200**

```json
{
  "status_code": 200,
  "message": "답변을 생성했습니다.",
  "code": "SUCCESS",
  "data": {
    "case_id": 123,
    "chat_session_id": 10,
    "user_message": {
      "id": 1,
      "sequence_no": 1,
      "role": "USER",
      "content": "확인이 필요한 항목을 알려줘",
      "model_name": null,
      "model_version": null,
      "input_tokens": null,
      "output_tokens": null,
      "evidence_refs": [],
      "created_at": "2026-08-31T09:03:00+09:00"
    },
    "assistant_message": {
      "id": 2,
      "sequence_no": 2,
      "role": "ASSISTANT",
      "content": "자체 점검에서 확인하지 못한 항목을 확인해 주세요. 근거가 부족한 관계는 판단 불가로 표시됩니다.",
      "model_name": "example-only",
      "model_version": null,
      "input_tokens": null,
      "output_tokens": null,
      "evidence_refs": [],
      "created_at": "2026-08-31T09:03:02+09:00"
    }
  },
  "errors": []
}
```

**실패 응답**

| HTTP | code | 발생 조건 |
|---|---|---|
| 401 | UNAUTHORIZED | 토큰 누락·만료·유효하지 않음 |
| 404 | NOT_FOUND | 분석 건이 없거나 본인 소유가 아님 |
| 409 | CONFLICT | 조회 가능한 완료 보고서가 없어 대화 이용 불가 |
| 422 | VALIDATION_ERROR | 입력 필드 또는 경로 값 검증 실패 |
| 502 | LLM_INVALID_RESPONSE | 모델 응답의 형식 또는 근거가 유효하지 않음 |
| 503 | LLM_UNAVAILABLE | 모델을 사용할 수 없음 |
| 503 | LLM_TIMEOUT | 모델 응답 시간 초과 |

---

### 13. 서버 생존 확인

**GET `/health/live`** · 인증 불필요

**요청**

본문·쿼리 파라미터 없음. 서버가 요청에 응답하는지 확인하며 DB·모델·분석 기능 전체를 검사하지 않습니다. 프론트의 필수 호출 절차는 아닙니다. 서버가 꺼졌거나 네트워크가 끊기면 HTTP/JSON 응답 자체를 받지 못할 수 있습니다.

**성공 응답 — 200**

```json
{
  "status_code": 200,
  "message": "서버가 응답 중입니다.",
  "code": "SUCCESS",
  "data": {
    "status": "ok"
  },
  "errors": []
}
```

**실패 응답**

별도 업무 오류는 없습니다. 서버 또는 통신 장애는 공통 오류 처리 대상입니다.

---

### 14. 서버 준비 상태 확인

**GET `/health/ready`** · 인증 불필요

**요청**

본문·쿼리 파라미터 없음. DB 연결과 필수 테이블(sims.app_user)의 존재를 검사합니다. LLM·스토리지·분석 전체의 정상 작동을 보장하지 않습니다. 프론트 필수 기능은 아니며 연결 점검용입니다.

**성공 응답 — 200**

```json
{
  "status_code": 200,
  "message": "서버가 요청을 처리할 준비가 되었습니다.",
  "code": "SUCCESS",
  "data": {
    "status": "ready"
  },
  "errors": []
}
```

**실패 응답**

| HTTP | code | 발생 조건 |
|---|---|---|
| 503 | SERVICE_UNAVAILABLE | DB 연결 실패 또는 필수 테이블이 없어 서버가 준비되지 않음 |

```json
{
  "status_code": 503,
  "message": "서비스를 일시적으로 사용할 수 없습니다.",
  "code": "SERVICE_UNAVAILABLE",
  "data": null,
  "errors": []
}
```

DB 연결·준비 상태 확인 실패 시에도 실패 응답의 data는 null로 통일합니다.

---

## 5. 프론트 호출 순서

1. 로그인 → `data.access_token` 확보
2. 문서 업로드 → `data.case_id` 확보
3. 분석 시작 → 해당 건의 HTTP 202 응답 확인
4. 3단계 성공을 확인한 건에 한해 분석 상태 폴링 시작
   - `data.status = "분석 중"`: 상태 조회 계속
   - `data.status = "분석 완료"`: 상태 조회 종료 후 보고서 조회
   - `data.status = "분석 실패"`: 상태 조회 종료 후 failure_message 표시
5. 분석 결과로 화면 표시
6. report_download_url이 있으면 인증 헤더를 붙여 PDF 다운로드
7. 보고서가 준비된 분석 건에서 질문 전송·대화 이력 조회

폴링 간격·최대 대기시간은 여기서 임의로 고정하지 않습니다.
UPLOADED 건도 ‘분석 중’으로 조회되므로, 분석 이력의 표시값만 보고 폴링을 시작하지 않습니다.
헬스체크는 연결 점검용이며 이 흐름의 선행 필수 호출이 아닙니다.
HTTP 200은 **호출한 API의 성공**입니다. 분석 자체의 성공·실패는 data 안에서 확인합니다.

## 6. 데이터 표시 규칙

| 상황 | 프론트 처리 |
|---|---|
| 상태가 ‘분석 중’ | UPLOADED도 포함하므로 분석 시작 성공을 뜻하지 않음. 해당 건의 7번 API 성공(202) 확인 후에만 폴링 시작 |
| data가 null인 성공 | 비밀번호 변경·로그아웃처럼 결과 데이터가 없는 성공 |
| cases 또는 messages가 [] | 이력 없음. 오류 아님 |
| similar_candidates가 [] | 후보 없음 또는 검색 일부 실패일 수 있으므로 warnings도 확인 |
| 점수가 null | 0점으로 바꾸지 않고 평가 불가로 표시 |
| FIT 관계가 INSUFFICIENT | 정보 부족 또는 비교 불가. HTTP 오류가 아님 |
| SIM review_grade가 ON_HOLD | 판단 보류. 항상 백엔드 결함이라고 간주하지 않음 |
| module_status가 UNAVAILABLE | 해당 분석 모듈 결과를 만들지 못함. 다른 결과와 warnings는 보존 |
| Evidence의 page_no가 null | 임의 페이지 번호를 생성하지 않고 excerpt·section_path 등으로 표시 |
| evidence_ref | 불투명 키. 문자열을 파싱해 의미를 추정하지 않음 |
| semantic_similarity_display | 표시용 유사도 점수. ‘중복 확률’로 표시하지 않음 |
| report_download_url이 null | 다운로드 준비 안 됨. 버튼 비활성화 또는 준비 안내 |
| warnings가 있음 | 전체 결과를 버리지 않고 해당 경고와 가능한 결과를 함께 표시 |

CPL은 13개 항목, FIT은 7개 관계를 유지합니다.
CPL 확인율은 `(PRESENT + NOT_APPLICABLE) / 13 × 100`입니다.
SIM은 목적·대상·지원내용의 핵심 3축 중 하나라도 평가 불가이면 전체 점수가 null이고 ON_HOLD입니다.
FIT-4는 비교 기준이 준비되기 전까지 INSUFFICIENT일 수 있습니다.
일부 분석 모듈의 정보 부족·실패를 API 요청 실패와 혼동하지 않습니다.

분석 이력·상태 조회는 한글 상태값을 사용합니다.
업로드 응답은 UPLOADED, 분석 시작 응답은 PARSING, 보고서 ui_status는 COMPLETED입니다.
이 문서는 응답 바깥 구조를 통일하며 도메인 상태값을 별도로 재설계하지 않습니다.

## 7. 파일 구성과 적용 범위

| 파일 | 용도 |
|---|---|
| 이 문서 | 프론트가 읽는 14개 API 명세 |
| Pre-review_API_공통응답_예시_v0.2_260831.json | API별 요청·성공·실패 예시 모음 |
| Pre-review_API_분석결과_전체예시_v0.2_260831.json | 9번 API의 생략 없는 JSON 응답 |
| Pre-review_API_분석결과_데이터스키마_v0.2_260831.json | 9번 data의 전체 타입·필드 정의 |
| [CORS_연동계약_v0.1_260831.md](CORS_연동계약_v0.1_260831.md) | 허용 Origin·요청 헤더·PDF 파일명 헤더 노출·CORS 제한 |

[14개 API JSON 예시 모음](Pre-review_API_공통응답_예시_v0.2_260831.json)은 **파일 전체가 서버 응답이 아닙니다.**
각 항목의 `success.body`, `errors[].body`가 실제로 제안하는 응답 본문입니다.
PDF 항목에는 바이너리 응답이라는 설명만 있고 가짜 PDF 데이터를 넣지 않습니다.
JSON Schema는 데이터 타입을 설명하며, 항목 개수·점수 의미 등 위 도메인 규칙도 함께 적용합니다.

이번 산출물은 **문서와 설명용 예시**입니다. 서버 코드·DB·OpenAPI는 변경하지 않았습니다.
실제 적용 시에는 응답 모델·오류 핸들러·업로드 크기 제한 응답·OpenAPI·프론트 파싱을 함께 맞춰야 합니다.
공통 응답은 HTTP 전송 계층에만 적용하며, 저장된 report_json·PDF 내용·CPL/FIT/SIM 결과 계약을 바꾸지 않습니다.
비밀번호 변경·로그아웃은 200과 JSON을 반환하도록, 업로드는 200을 반환하도록 적용해야 합니다.

적용 전 확인할 사항:

- 현재 채팅의 공백 전용 입력이 내부 오류로 이어질 수 있으므로 서버에서도 입력 오류로 처리하도록 보완합니다. 문서만으로 수정됐다고 간주하지 않습니다.
- 인증 실패·입력 검증·미들웨어 오류·예상하지 못한 오류까지 같은 JSON인지 확인합니다.
- 프록시·네트워크 등 애플리케이션 바깥 오류는 공통 JSON이 아닐 수 있으므로 프론트가 대비합니다.
- PDF 성공은 파일이고 실패는 JSON이라는 두 경로를 확인합니다.
- 모든 JSON 예시는 적용 목표이며, 서버 배포 전에는 현재 서버 응답과 같다고 가정하지 않습니다.
