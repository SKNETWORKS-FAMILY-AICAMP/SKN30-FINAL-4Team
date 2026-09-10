# Pre-review DB 전체 컬럼 설명

2026-09-04 기준 · 대상 `backend/app/db/schema.sql` · **테이블 37개 / 컬럼 379개**
행 수는 팀 Supabase 실측값이다.

찾는 컬럼 이름을 `Ctrl+F` 로 그대로 치면 된다. 컬럼명은 전부 `` `백틱` `` 으로 감싸 두었다.

---

## 읽는 법

- **타입** — `timestamptz` 는 `timestamp with time zone` 이다. 시각은 전부 UTC 로 저장한다.
- **필수** — `O` 면 `NOT NULL`. 기본값이 있으면 설명에 적었다.
- `id` 는 전부 `bigint GENERATED ALWAYS AS IDENTITY` 자동 증가 PK 다. 따로 설명하지 않는다.
- `created_at` 은 전부 `now()` 기본값의 행 생성 시각이다. 다른 뜻이 있을 때만 설명을 달았다.
- **FK** 표기는 `→ 대상테이블` 이다. `CASCADE` 는 부모가 지워지면 같이 지워진다는 뜻이다.

## 테이블 목차

| # | 테이블 | 행 | 한 줄 |
|---|---|---|---|
| 1 | [`app_user`](#1-app_user) | 4 | 로그인 계정 |
| 2 | [`password_change_history`](#2-password_change_history) | 2 | 비밀번호 변경 시각 감사 로그 |
| 3 | [`inspection_case`](#3-inspection_case) | 54 | **분석 1건.** 모든 것이 여기 매달린다 |
| 4 | [`file_asset`](#4-file_asset) | 99 | 저장소 파일 1개의 메타데이터 |
| 5 | [`object_delete_outbox`](#5-object_delete_outbox) | 2,868 | 파일 삭제 대기 큐 |
| 6 | [`uploaded_document`](#6-uploaded_document) | 52 | 검사 건에 붙은 요청서 원본 |
| 7 | [`document_parse_run`](#7-document_parse_run) | 51 | **파싱 시도 1회. 원문 텍스트가 여기 있다** |
| 8 | [`chunking_profile`](#8-chunking_profile) | 0 | 청킹 전략 설정 (미사용) |
| 9 | [`document_chunk_set`](#9-document_chunk_set) | 0 | 청킹 묶음 (미사용) |
| 10 | [`document_chunk`](#10-document_chunk) | 0 | 잘린 조각 (미사용) |
| 11 | [`form_schema`](#11-form_schema) | 392 | 요청서 서식 버전 |
| 12 | [`form_field_definition`](#12-form_field_definition) | 13 | **CPL 13개 항목 정의** |
| 13 | [`request_extraction`](#13-request_extraction) | 51 | 항목 추출 실행 1회 |
| 14 | [`request_field_value`](#14-request_field_value) | 663 | **뽑아낸 항목 값 1개** |
| 15 | [`missing_check_run`](#15-missing_check_run) | 51 | CPL 점검 실행 1회 |
| 16 | [`missing_check_item`](#16-missing_check_item) | 663 | **CPL 판정 1줄. 화면의 13줄** |
| 17 | [`data_source`](#17-data_source) | 3 | 외부 자료 출처 정의 |
| 18 | [`api_sync_run`](#18-api_sync_run) | 1 | 공고 동기화 실행 1회 |
| 19 | [`announcement`](#19-announcement) | 6 | 공고 1건의 불변 식별자 |
| 20 | [`announcement_version`](#20-announcement_version) | 6 | **공고 내용 스냅샷 (40컬럼)** |
| 21 | [`announcement_attachment`](#21-announcement_attachment) | 0 | 공고 첨부파일 |
| 22 | [`announcement_subprogram_period`](#22-announcement_subprogram_period) | 0 | 내역사업별 접수기간 (미사용) |
| 23 | [`archive_import_batch`](#23-archive_import_batch) | 0 | CSV 적재 배치 (미사용) |
| 24 | [`archive_bizinfo_listing`](#24-archive_bizinfo_listing) | 0 | 기업마당 목록 원본 (미사용) |
| 25 | [`archive_central_program`](#25-archive_central_program) | 0 | 중앙부처 사업 원본 (미사용) |
| 26 | [`embedding_model`](#26-embedding_model) | 392 | 임베딩 모델 등록 |
| 27 | [`embedding_profile`](#27-embedding_profile) | 392 | 임베딩 입력 조립 규칙 |
| 28 | [`inspection_embedding`](#28-inspection_embedding) | 47 | **요청서 벡터** |
| 29 | [`announcement_embedding`](#29-announcement_embedding) | 6 | **공고 벡터** |
| 30 | [`chunk_embedding`](#30-chunk_embedding) | 0 | 조각 벡터 (미사용) |
| 31 | [`retrieval_run`](#31-retrieval_run) | 47 | 유사공고 검색 실행 1회 |
| 32 | [`retrieval_candidate`](#32-retrieval_candidate) | 230 | **후보 공고 1건 + SIM 비교 결과** |
| 33 | [`candidate_evidence`](#33-candidate_evidence) | 3,119 | SIM 비교의 원문 근거 |
| 34 | [`inspection_report`](#34-inspection_report) | 47 | **최종 보고서 JSON (불변)** |
| 35 | [`output_artifact`](#35-output_artifact) | 47 | 생성된 PDF 메타 |
| 36 | [`chat_session`](#36-chat_session) | 4 | 검사 건당 대화방 1개 |
| 37 | [`chat_message`](#37-chat_message) | 16 | **질문·답변 본문** |

---

# 1. 인증

## 1. `app_user`

로그인 계정. `login_id` 와 `email` 이 각각 유일하며 **로그인 조회는 `email` 로 한다.**

| 컬럼 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `id` | bigint | O | PK |
| `login_id` | citext | O | 로그인 아이디. 대소문자 구분 안 함(citext). 유일. **로그인에는 안 쓴다** |
| `email` | citext | O | 로그인 식별자. 유일. 공백 불가 |
| `password_hash` | text | O | Argon2 해시. 평문은 어디에도 없다 |
| `password_changed_at` | timestamptz | O | 마지막 비밀번호 변경 시각. 기본 `now()`. **이 시각 이전에 발급된 토큰은 전부 거부된다** — JWT `pwd` 클레임과 비교 |
| `is_active` | boolean | O | 계정 활성 여부. 기본 `true`. 비활성이면 로그인 거부 |
| `last_login_at` | timestamptz | | 마지막 로그인 시각 |
| `created_at` | timestamptz | O | 생성 시각 |
| `updated_at` | timestamptz | O | 수정 시각. 트리거가 자동 갱신 |
| `display_name` | text | | 화면에 보일 이름. **비어 있으면 이메일의 `@` 앞부분을 쓴다** ([auth.py:33](../backend/app/services/auth.py:33)). 회원가입 API 가 없어 계정과 함께 직접 넣는다 |

**제약** — `UNIQUE(email)`, `UNIQUE(login_id)`, `email`·`login_id`·`password_hash` 공백 불가
**쓰는 곳** — [auth.py](../backend/app/services/auth.py), [password_reset.py](../backend/app/services/password_reset.py), [session.py](../backend/app/db/session.py)

## 2. `password_change_history`

성공한 비밀번호 변경 **시각만** 남긴다. 평문도, 옛 해시도, 새 해시도 저장하지 않는다.

| 컬럼 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `id` | bigint | O | PK |
| `user_id` | bigint | O | → `app_user` CASCADE |
| `changed_at` | timestamptz | O | 변경 시각. 기본 `now()` |

**채우는 주체** — 애플리케이션이 아니라 **DB 트리거** `trg_app_user_password_change_history` ([schema.sql:75](../backend/app/db/schema.sql:75)). 계정 최초 생성은 행을 만들지 않는다.
**읽는 곳** — **없다.** 감사 로그인데 소비자가 없다.

---

# 2. 검사 건과 파일

## 3. `inspection_case`

**분석 1건 = 1행.** 업로드부터 완료까지의 상태 기계이며 파일·추출·검색·보고서·대화가 전부 이 행에 매달린다. 사용자가 보는 **분석 이력 1줄**이 이 행 하나다.

| 컬럼 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `id` | bigint | O | PK. API 의 `case_id` |
| `owner_user_id` | bigint | O | → `app_user` CASCADE. **남의 건 조회를 막는 기준.** 안 맞으면 `404` |
| `status` | text | O | 기본 `UPLOADED`. 아래 7개 중 하나 |
| `top_k_used` | smallint | O | 유사공고를 몇 건까지 찾을지. 기본 `5`, 1~100 |
| `failure_code` | text | | 실패 사유 코드. 실측: `ANALYSIS_INTERRUPTED`, `RETRIEVAL_NOT_READY`. **API 응답에는 안 나간다** |
| `failure_message` | text | | 실패 상세 메시지. 내부 로그용 |
| `result_frozen_at` | timestamptz | | 결과를 확정(동결)한 시각. `COMPLETED` 면 반드시 있다 |
| `completed_at` | timestamptz | | 분석 완료 시각. 이력 목록 정렬 기준 |
| `created_at` | timestamptz | O | 업로드 시각. API 의 `started_at` |
| `updated_at` | timestamptz | O | 트리거가 자동 갱신 |

**`status` 값** — `UPLOADED` → `PARSING` → `CHECKING` → `RETRIEVING` → `REPORTING` → `COMPLETED`, 실패 시 `FAILED`
API 는 이걸 3개로 뭉쳐 준다: `COMPLETED`/`FAILED` 외 전부 `IN_PROGRESS`.

**제약** — `COMPLETED` 면 `completed_at` 과 `result_frozen_at` 이 둘 다 있어야 한다. `UNIQUE(id, owner_user_id)` 는 `file_asset` 복합 FK 용
**쓰는 곳** — [case_upload.py:168](../backend/app/services/case_upload.py:168) 생성, 이후 전 서비스가 상태를 옮긴다

## 4. `file_asset`

저장소에 올라간 **파일 1개의 메타데이터.** 파일 내용은 여기 없다.

| 컬럼 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `id` | bigint | O | PK |
| `asset_scope` | text | O | `USER` 또는 `SHARED`. 아래 제약 참고 |
| `owner_user_id` | bigint | | → `app_user` CASCADE. `USER` 면 필수 |
| `inspection_case_id` | bigint | | → `inspection_case` CASCADE. `USER` 면 필수 |
| `storage_key` | text | O | **저장소 경로.** 유일. 예: `users/2832/cases/2121/6f57….hwp` |
| `original_filename` | text | O | 업로드 당시 파일명. **이력 목록의 `title` 이 이 값이다** |
| `detected_mime_type` | text | | 내용으로 판별한 MIME. 확장자와 다르면 업로드가 `422` 로 거부된다 |
| `extension` | text | | 확장자 (`hwp`, `hwpx`, `pdf`) |
| `size_bytes` | bigint | | 바이트 크기. 0 이상 |
| `sha256_hex` | char(64) | | 내용 해시. 저장 후 무결성 검증에 쓴다 |
| `source_url` | text | | 외부에서 받아온 파일이면 원본 URL. 공고 첨부용 |
| `created_at` | timestamptz | O | |

**제약** — `USER` 면 `owner_user_id` 와 `inspection_case_id` 가 **둘 다 있어야 하고**, `SHARED` 면 **둘 다 없어야 한다**. `UNIQUE(storage_key)`
**주의** — 파일 실체는 `backend/storage/` 디스크에 있다. 다른 머신에서 백엔드를 띄우면 행은 있는데 파일이 없다.
**쓰는 곳** — [case_upload.py:194](../backend/app/services/case_upload.py:194) 요청서, [reporting.py:349](../backend/app/services/reporting.py:349) PDF

## 5. `object_delete_outbox`

트랜잭션 삭제 아웃박스 겸 **영구 묘비 대장.** `storage_key` 는 전역 유일하며 재사용되지 않는다.

| 컬럼 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `id` | bigint | O | PK |
| `storage_key` | text | O | 지워야 할 저장소 경로. 유일 |
| `requested_at` | timestamptz | O | 삭제 요청 시각. 기본 `now()` |
| `processed_at` | timestamptz | | 실제로 지운 시각. **`NULL` 이면 아직 안 지웠다** |
| `attempt_count` | integer | O | 삭제 시도 횟수. 기본 `0` |
| `last_error` | text | | 마지막 실패 사유 |

**채우는 주체** — `file_asset` 삭제 시 **DB 트리거** ([schema.sql:200](../backend/app/db/schema.sql:200))
**실측 경고** — 2,868건 전부 `processed_at IS NULL`. **아무도 소비하지 않는다.** 실제 파일은 디스크에 그대로 남아 있다.

## 6. `uploaded_document`

검사 건에 붙은 요청서 원본 1개. **검사 건당 정확히 한 행.**

| 컬럼 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `id` | bigint | O | PK |
| `inspection_case_id` | bigint | O | → `inspection_case` CASCADE. 유일 |
| `file_asset_id` | bigint | O | → `file_asset` CASCADE. 유일 |
| `asset_scope` | text | O | 항상 `USER` 로 고정 |
| `declared_format` | text | O | `HWP` / `HWPX` / `PDF` |
| `uploaded_at` | timestamptz | O | 업로드 시각 |

**주의** — DDL 은 `PDF` 를 허용하지만 **업로드 API 는 HWP·HWPX 만 받는다** (`415`).
**쓰는 곳** — [case_upload.py:236](../backend/app/services/case_upload.py:236)

## 7. `document_parse_run`

파일 1개를 파싱한 **시도 1회.** 재시도는 같은 파일에 `attempt_no` 를 올려 쌓이므로 실패 이력이 남는다.
**원문 텍스트가 실제로 사는 곳이다.**

| 컬럼 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `id` | bigint | O | PK |
| `file_asset_id` | bigint | O | → `file_asset` CASCADE |
| `attempt_no` | integer | O | 시도 번호. 1부터. 같은 파일 안에서 유일 |
| `parser_name` | text | O | 파서 이름. 실측 `rhwp-python` |
| `parser_version` | text | O | 파서 버전. 실측 `0.8.1` |
| `status` | text | O | 기본 `PENDING`. 아래 5개 |
| `used_ocr` | boolean | O | OCR 을 썼는지. 기본 `false` |
| `ocr_confidence` | numeric(5,4) | | OCR 신뢰도 0~1 |
| **`extracted_text`** | text | | **문서 전체 텍스트.** 발췌가 아니다. 실측 4,040자 |
| **`structured_content`** | jsonb | O | **블록 단위 구조.** `{blocks[], warnings, partial}`. 실측 블록 93개. 각 블록에 `block_id`·`source_locator` 가 있고 **근거 인용의 좌표계가 이 `block_id`** 다 |
| `text_sha256_hex` | char(64) | | `extracted_text` 의 해시 |
| `error_code` | text | | 실패 코드 |
| `error_message` | text | | 실패 상세 |
| `started_at` | timestamptz | | 파싱 시작 |
| `finished_at` | timestamptz | | 파싱 종료. 종료 상태면 반드시 있어야 한다 |
| `created_at` | timestamptz | O | |

**`status` 값** — `PENDING` / `PARSING` / `SUCCESS` / `PARTIAL_SUCCESS` / `FAILED`
`PARTIAL_SUCCESS` 는 일부 블록을 못 읽었지만 결과는 쓸 만하다는 뜻이다. 실측 예시가 이 상태다.
**쓰는 곳** — [document_parsing.py:119](../backend/app/services/document_parsing.py:119) 생성, [:194](../backend/app/services/document_parsing.py:194) 결과 갱신

---

# 3. 청킹 (미사용)

세 테이블 모두 **행이 0개이고 애플리케이션 코드가 참조하지 않는다.** 스키마 계약 테스트에만 나온다.
문서를 조각내 조각 단위로 검색하는 설계인데, 현재 SIM 은 공고 메타데이터 요약 임베딩만 쓴다.

## 8. `chunking_profile`

| 컬럼 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `id` | bigint | O | PK |
| `profile_name` | text | O | 전략 이름. `version_no` 와 함께 유일 |
| `version_no` | integer | O | 버전. 1 이상. **전략이 바뀌면 새 버전을 만들어 과거 청크와 안 섞이게 한다** |
| `strategy` | text | O | 자르는 방식 이름 |
| `configuration` | jsonb | O | 전략 파라미터. 기본 `{}` |
| `is_active` | boolean | O | 활성 여부. 기본 `false` |
| `created_at` | timestamptz | O | |

## 9. `document_chunk_set`

파싱 결과 1개를 청킹 프로파일 1개로 자른 묶음. 같은 파싱 결과를 다른 전략으로 여러 번 자를 수 있다.

| 컬럼 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `id` | bigint | O | PK |
| `parse_run_id` | bigint | O | → `document_parse_run` CASCADE |
| `chunking_profile_id` | bigint | O | → `chunking_profile` |
| `created_at` | timestamptz | O | |

**제약** — `UNIQUE(parse_run_id, chunking_profile_id)`

## 10. `document_chunk`

잘린 조각 1개. **근거를 인용하는 최소 단위.**

| 컬럼 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `id` | bigint | O | PK |
| `chunk_set_id` | bigint | O | → `document_chunk_set` CASCADE |
| `chunk_no` | integer | O | 묶음 안 순번. 0부터 |
| `content` | text | O | 조각 본문. 공백만이면 거부 |
| `content_sha256_hex` | char(64) | O | 본문 해시 |
| `page_no` | integer | | 페이지 번호. 1 이상 |
| `section_name` | text | | 소속 절 이름 |
| `source_locator` | jsonb | O | 문서 내 위치. 기본 `{}` |
| `created_at` | timestamptz | O | |

---

# 4. 요청서 항목 추출과 CPL 점검

## 11. `form_schema`

요청서 **서식의 버전 1개.** 서식이 바뀌면 새 버전을 만들고 과거 검사 결과는 그대로 둔다.

| 컬럼 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `id` | bigint | O | PK |
| `schema_name` | text | O | 서식 이름. `version_no` 와 함께 유일. **실제 값은 `sims-cpl` 하나** |
| `version_no` | integer | O | 버전. 1 이상 |
| `source_file_asset_id` | bigint | | → `file_asset` SET NULL. 서식 원본 파일 |
| `effective_from` | date | | 이 서식이 유효해지는 날 |
| `description` | text | | 설명 |
| `is_active` | boolean | O | 활성 여부. 기본 `false` |
| `created_at` | timestamptz | O | |

**실측 경고** — 392행 중 **391행이 `report-<uuid>` 형태의 테스트 잔여물**이다. 실제 서식은 `sims-cpl` 1건뿐이고 여기에만 13개 항목이 붙어 있다.

## 12. `form_field_definition`

서식이 요구하는 **항목 1개의 정의.** CPL 점검이 무엇을 확인해야 하는지가 여기서 정해진다.
**13행 = CPL 13개 항목.** 이 테이블이 항목 목록의 원본이다.

| 컬럼 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `id` | bigint | O | PK |
| `form_schema_id` | bigint | O | → `form_schema` CASCADE |
| `field_code` | text | O | **항목 코드.** `PURPOSE_GOAL` 등 13개. 서식 안에서 유일. API 응답의 `field_code` 가 이 값 |
| `field_label` | text | O | 화면 항목명 (`사업 목적·목표`) |
| `parent_field_code` | text | | 상위 항목 코드. 계층 표현용 |
| `data_type` | text | O | `TEXT`/`DATE`/`NUMBER`/`BOOLEAN`/`CHOICE`/`JSON`. 기본 `TEXT` |
| `required_rule` | jsonb | O | **필수 여부 판정 규칙.** 기본 `{}` |
| `is_sensitive` | boolean | O | 민감 항목 여부. 기본 `false` |
| `display_order` | integer | O | 화면 표시 순서. 0 이상 |
| `created_at` | timestamptz | O | |

**`field_code` 13개** — `REQUEST_TYPE` · `PURPOSE_GOAL` · `IMPLEMENTATION_PLAN` · `BUSINESS_PERIOD` · `NEW_OR_CHANGED_CONTENT` · `BUSINESS_NEED` · `LEGAL_BASIS` · `LINKED_POLICY` · `BUDGET` · `TARGET_AND_CONDITIONS` · `SUPPORT_CONTENT_AND_SCALE` · `DELIVERY_SYSTEM` · `EXPECTED_EFFECTS_AND_PERFORMANCE`

## 13. `request_extraction`

요청서에서 항목 값을 뽑아낸 **실행 1회.** 검사 건당 한 행(`UNIQUE`).

| 컬럼 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `id` | bigint | O | PK |
| `inspection_case_id` | bigint | O | → `inspection_case` CASCADE. **유일** |
| `form_schema_id` | bigint | O | → `form_schema`. 어느 서식으로 뽑았나 |
| `parse_run_id` | bigint | O | → `document_parse_run` CASCADE. 어느 파싱 결과를 썼나 |
| `request_reason` | text | O | 사전협의 요청 유형. 기본 `UNKNOWN`. 아래 5개 |
| `status` | text | O | 기본 `PENDING`. `PENDING`/`RUNNING`/`SUCCESS`/`PARTIAL_SUCCESS`/`FAILED` |
| `extractor_name` | text | O | 추출기 이름. 실측 `cpl-rule-llm` |
| `extractor_version` | text | O | 추출기 버전. 실측 `cpl-alpha-v0.3+cpl-semantic-v0.9` |
| `confidence` | numeric(5,4) | | 전체 신뢰도 0~1. **코드가 안 채운다** |
| `raw_extraction` | jsonb | O | **CPL 결과 전체 스냅샷.** 기본 `{}`. 키: `items`, `warnings`, `confirmed_count`, `total_count`, `confirmation_rate`, `ruleset_version`, `prompt_version`, `model_profile`, `extractor_name`, `extractor_version`, `request_reason` |
| `created_at` | timestamptz | O | |
| `completed_at` | timestamptz | | 추출 완료 시각 |

**`request_reason` 값** — `DETAIL_NEW`(세부사업 신설) / `SUBPROGRAM_NEW`(내역사업 신설) / `SUBSUBPROGRAM_NEW`(내내역사업 신설) / `CONTENT_CHANGE`(사업내용 변경) / `UNKNOWN`
**쓰는 곳** — [cpl/checker.py:106](../backend/app/services/cpl/checker.py:106)

## 14. `request_field_value`

**뽑아낸 항목 값 1개.** 원문 텍스트와 정규화 값, 문서 어디에서 나왔는지를 함께 담아 근거 추적을 가능하게 한다.
**663행 = 51건 × 13항목.**

| 컬럼 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `id` | bigint | O | PK |
| `request_extraction_id` | bigint | O | → `request_extraction` CASCADE |
| `field_definition_id` | bigint | O | → `form_field_definition`. 어느 항목인가 |
| `raw_text` | text | | 원문 그대로. **코드가 항상 `NULL` 을 넣는다** |
| **`normalized_value`** | jsonb | | **실제 값이 여기 전부 들어간다.** `{"occurrences":[{block_id, raw_text, page_no, section_path, source_locator, axis_code, source_role}]}` |
| `confidence` | numeric(5,4) | | 항목 신뢰도 0~1. **코드가 안 채운다** |
| `page_no` | integer | | 페이지 번호. **코드가 항상 `NULL`** |
| `source_bbox` | jsonb | | 좌표 박스. **코드가 안 채운다** |
| `source_locator` | jsonb | O | **근거 계보.** 기본 `{}`. `{"occurrences":[{block_id, page_no, section_path, source_locator, extraction_method, axis_code, source_role}]}` |
| `created_at` | timestamptz | O | |

**제약** — `UNIQUE(request_extraction_id, field_definition_id)` — 항목당 정확히 하나
**주의** — `raw_text`·`page_no`·`confidence`·`source_bbox` **네 컬럼은 DDL 에만 있고 아무도 안 채운다.** 값은 전부 `normalized_value` 와 `source_locator` jsonb 안이다 ([checker.py:166](../backend/app/services/cpl/checker.py:166)).

## 15. `missing_check_run`

CPL 점검 **실행 1회.** 한 검사 건에 여러 실행이 있을 수 있고, 재점검은 새 행을 만들며 기존 결과를 덮어쓰지 않는다.

| 컬럼 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `id` | bigint | O | PK |
| `inspection_case_id` | bigint | O | → `inspection_case` CASCADE |
| `request_extraction_id` | bigint | O | → `request_extraction` CASCADE |
| `ruleset_version` | text | O | **어떤 규칙 버전으로 판정했나.** 재현성의 핵심 |
| `status` | text | O | 기본 `PENDING`. `PENDING`/`RUNNING`/`SUCCESS`/`FAILED` |
| `started_at` | timestamptz | | 점검 시작 |
| `completed_at` | timestamptz | | 점검 종료 |
| `created_at` | timestamptz | O | |

## 16. `missing_check_item`

항목 1개의 **CPL 점검 결과. 화면의 CPL 표 한 줄이 이 행이다.**

| 컬럼 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `id` | bigint | O | PK |
| `missing_check_run_id` | bigint | O | → `missing_check_run` CASCADE |
| `field_definition_id` | bigint | O | → `form_field_definition`. 어느 항목 |
| `evidence_field_value_id` | bigint | | → `request_field_value` SET NULL. **판정 근거가 된 값** |
| `result_status` | text | O | 판정 결과. 아래 5개 |
| `reason_code` | text | | 사유 코드. 실측: `REQUEST_TYPE_AMBIGUOUS`, `LLM_INVALID_RESPONSE` 등. **API 응답에는 안 나간다** |
| `explanation` | text | | 사람이 읽을 설명. **API 응답에는 안 나간다** |
| `created_at` | timestamptz | O | |

**`result_status` 값**

| 값 | 뜻 |
|---|---|
| `PRESENT` | 필요한 내용 확인 |
| `MISSING` | 필요한 내용 누락 |
| `NOT_APPLICABLE` | 원문에 해당 없음이 명시됨 |
| `NEEDS_CONFIRMATION` | 원문은 있으나 확정하기 어려워 확인 필요 |
| `PARSE_FAILED` | 파싱 실패. **API 응답에는 안 담고 DB 에만 보존한다** |

`confirmed_count` 는 `PRESENT` + `NOT_APPLICABLE` 개수다.
**제약** — `UNIQUE(missing_check_run_id, field_definition_id)`

---

# 5. 공고 수집

## 17. `data_source`

외부 자료 **출처 정의.** `is_search_source` 가 참인 출처만 유사공고 검색 대상이 된다.

| 컬럼 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `source_code` | text | O | **PK.** 출처 코드. 예: `BIZINFO_OPEN_API` |
| `source_name` | text | O | 사람이 읽을 출처 이름 |
| `source_type` | text | O | `API` 또는 `FILE` |
| `is_search_source` | boolean | O | 검색 대상 여부. 기본 `false` |
| `description` | text | | 설명 |
| `created_at` | timestamptz | O | |

**주의** — 3건이 `schema.sql:1481` 에서 seed 되지만 **애플리케이션 코드는 이 테이블을 조회하지 않는다.** FK 로만 참조된다.

## 18. `api_sync_run`

출처 1개의 **하루치 동기화 실행.** 출처와 날짜로 유일해 하루에 두 번 돌지 않는다.

| 컬럼 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `id` | bigint | O | PK |
| `source_code` | text | O | → `data_source` |
| `sync_date_kst` | date | O | **한국 시간 기준 동기화 날짜.** `source_code` 와 함께 유일 |
| `status` | text | O | 기본 `PENDING`. `PENDING`/`RUNNING`/`SUCCEEDED`/`FAILED` |
| `attempt_count` | integer | O | 시도 횟수. 기본 `1`, 1 이상 |
| `is_initial_load` | boolean | O | 최초 전량 적재인지. 기본 `false` |
| `started_at` | timestamptz | | 시작 시각 |
| `completed_at` | timestamptz | | 종료 시각 |
| `latest_source_created_at` | timestamptz | | 가져온 것 중 가장 최신 공고의 등록 시각. **다음 증분 동기화의 기준점** |
| `resume_cursor` | text | | 중단된 지점. 이어받기용 |
| `rows_fetched` | integer | O | 가져온 건수. 기본 `0` |
| `rows_inserted` | integer | O | 새 공고로 넣은 건수 |
| `rows_versioned` | integer | O | **내용이 바뀌어 새 버전을 만든 건수** |
| `rows_unchanged` | integer | O | 그대로였던 건수 |
| `error_code` | text | | 실패 코드 |
| `error_message` | text | | 실패 상세 |
| `statistics` | jsonb | O | 부가 통계. 기본 `{}` |
| `created_at` | timestamptz | O | |

**쓰는 곳** — [announcement_sync.py:348](../backend/app/services/retrieval/announcement_sync.py:348)

## 19. `announcement`

공고 1건의 **불변 식별자.** 내용은 여기 없고 `announcement_version` 에 붙는다.

| 컬럼 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `id` | bigint | O | PK |
| `source_code` | text | O | → `data_source` |
| `pblanc_id` | text | O | **출처의 공고 ID.** 전역 유일. 실측 `MOCK-COMM-0002` |
| `first_seen_at` | timestamptz | O | 처음 발견한 시각 |
| `last_seen_at` | timestamptz | O | 마지막으로 본 시각. `first_seen_at` 이상 |
| `created_at` | timestamptz | O | |

## 20. `announcement_version`

Open API 공고의 **SCD Type 2 스냅샷.** 내용이 바뀌면 새 버전을 만든다. 40개 컬럼으로 가장 크다.

### 버전 관리

| 컬럼 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `id` | bigint | O | PK |
| `announcement_id` | bigint | O | → `announcement` |
| `source_sync_run_id` | bigint | | → `api_sync_run` SET NULL. 어느 동기화가 만들었나 |
| `version_no` | integer | O | 버전 번호. 1 이상. 공고 안에서 유일 |
| `content_sha256_hex` | char(64) | O | **내용 해시.** 이게 바뀌면 새 버전을 만든다 |
| `is_current` | boolean | O | 현재 버전인지. 기본 `true` |
| `valid_from` | timestamptz | O | 이 버전이 유효해진 시각. 기본 `now()` |
| `valid_to` | timestamptz | | 유효 종료 시각. **현재 버전이면 `NULL`, 아니면 반드시 값이 있어야 한다** |

### 공고 본문

| 컬럼 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `pblanc_nm` | text | O | **공고 제목.** API 응답의 `title` |
| `pblanc_url` | text | O | **공고 원문 링크.** API 응답의 `source_url` |
| `jrsd_instt_nm` | text | | 소관 기관명 (`중소벤처기업부`) |
| `exc_instt_nm` | text | | 수행 기관명 |
| `bsns_sumry_html` | text | | 사업 요약 HTML 원본 |
| `bsns_sumry_text` | text | O | 사업 요약 평문 |
| `purpose` | text | O | **사업 목적.** SIM `purpose` 축의 재료 |
| `target` | text | O | **지원 대상.** SIM `target` 축의 재료 |
| `content` | text | O | **지원 내용.** SIM `content` 축의 재료 |
| `category_name` | text | | 분류명 |
| `target_name` | text | | 대상 분류명 |
| `hashtags` | text[] | O | 해시태그 배열. 기본 `{}` |
| `view_count` | bigint | | 조회수. 0 이상 |
| `request_method_papers` | text | | 신청 방법·제출 서류 |
| `reference_contact` | text | | 문의처 |
| `receipt_homepage_url` | text | | 접수 홈페이지 |

### 상세 참조 보일러플레이트

| 컬럼 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `detail_ref_fields` | text[] | O | **"자세한 내용은 첨부 참조" 류 문구가 있는 의미 축.** 기본 `{}`. `target` 과 `content` 만 허용. **원본 텍스트는 절대 지우지 않는다** |
| `has_detail_ref` | boolean | | `detail_ref_fields` 가 비지 않았는지. **생성 컬럼이라 직접 못 쓴다** |

### 접수 기간

| 컬럼 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `period_raw_text` | text | O | 출처의 기간 문자열 원본 |
| `period_type` | text | O | 기간 유형. 아래 7개 |
| `period_start_date` | date | | **실제 접수 시작일.** 출처에 명확히 있을 때만. **공고 등록 시각을 몰래 시작일로 바꾸지 않는다** |
| `period_end_date` | date | | 접수 종료일. 시작일 이상 |
| `period_display_text` | text | O | 화면에 그대로 쓸 기간 문구 |

**`period_type` 값** — `FIXED`(확정 기간, 시작·종료일 둘 다 필수) / `UNTIL_EXHAUSTED`(예산 소진 시) / `ALWAYS`(상시) / `UNTIL_FILLED`(모집 완료 시) / `BY_SUBPROGRAM`(내역사업별) / `VARIABLE`(변동) / `UNKNOWN`

### 접수 상태

| 컬럼 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `search_status` | text | O | `OPEN`/`CLOSED`/`UNKNOWN`. 기본 `UNKNOWN`. **`OPEN` 과 `UNKNOWN` 만 검색 대상.** 상태 확인 실패는 `UNKNOWN` 이지 조용한 제외가 아니다. `모집 완료시` 같은 조건은 그 자체로 `CLOSED` 가 아니다 |
| `status_checked_at` | timestamptz | | 상태를 확인한 시각 |
| `status_source` | text | | 상태를 어디서 확인했나 |

### 원본 보존

| 컬럼 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `source_created_at` | timestamptz | O | 출처의 공고 등록 시각 |
| `source_updated_at` | timestamptz | | 출처의 수정 시각 |
| `raw_payload` | jsonb | O | **API 응답 원본 통째로.** 기본 `{}` |
| `created_at` | timestamptz | O | |
| `updated_at` | timestamptz | O | 트리거 자동 갱신 |

## 21. `announcement_attachment`

공고 **첨부파일.** 0행 — 아직 첨부 다운로드를 안 돌린다.

| 컬럼 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `id` | bigint | O | PK |
| `announcement_version_id` | bigint | O | → `announcement_version` CASCADE |
| `attachment_role` | text | O | `PRIMARY`(공고문 본문) 또는 `AUXILIARY`(부속 서류) |
| `ordinal_no` | integer | O | 같은 역할 안 순번. 0 이상 |
| `source_url` | text | O | 첨부 다운로드 URL |
| `original_filename` | text | O | 원본 파일명 |
| `extension` | text | | 확장자 |
| `detected_mime_type` | text | | 판별된 MIME |
| `fetch_status` | text | O | 기본 `NOT_REQUESTED`. `NOT_REQUESTED`/`DOWNLOADING`/`DOWNLOADED`/`FAILED` |
| `file_asset_id` | bigint | | → `file_asset` SET NULL. 받은 파일 |
| `last_fetch_error` | text | | 마지막 실패 사유 |
| `fetched_at` | timestamptz | | 받은 시각 |
| `created_at` | timestamptz | O | |

## 22. `announcement_subprogram_period`

내역사업별 접수 기간. `period_type = 'BY_SUBPROGRAM'` 인 공고를 펼치는 용도. **0행, 코드 참조 없음.**

| 컬럼 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `id` | bigint | O | PK |
| `announcement_version_id` | bigint | O | → `announcement_version` CASCADE |
| `subprogram_name` | text | O | 내역사업 이름 |
| `start_date` | date | | 시작일 |
| `end_date` | date | | 종료일. 시작일 이상 |
| `raw_period_text` | text | O | 기간 원문 |
| `source_page` | integer | | 출처 페이지. 1 이상 |
| `source_locator` | jsonb | O | 문서 내 위치. 기본 `{}` |
| `created_at` | timestamptz | O | |

---

# 6. 아카이브 (전부 미사용)

세 테이블 모두 **0행이고 `schema.sql` 외에는 저장소 어디에도 등장하지 않는다.** CSV 를 통째로 적재하려던 설계다.

## 23. `archive_import_batch`

| 컬럼 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `id` | bigint | O | PK |
| `source_code` | text | O | → `data_source` |
| `source_file_asset_id` | bigint | | → `file_asset` SET NULL. 적재한 CSV |
| `status` | text | O | 기본 `PENDING`. `PENDING`/`RUNNING`/`SUCCEEDED`/`FAILED` |
| `expected_rows` | integer | | 예상 행수 |
| `imported_rows` | integer | O | 적재 성공 행수. 기본 `0` |
| `error_rows` | integer | O | 실패 행수. 기본 `0` |
| `imported_at` | timestamptz | | 적재 시각 |
| `created_at` | timestamptz | O | |

## 24. `archive_bizinfo_listing`

기업마당 목록 CSV **한 줄 그대로.**

| 컬럼 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `id` | bigint | O | PK |
| `import_batch_id` | bigint | O | → `archive_import_batch` CASCADE |
| `source_row_no` | integer | O | CSV 행 번호. 1 이상. 배치 안에서 유일 |
| `list_no` | bigint | | 목록 번호 |
| `category_name` | text | | 분류명 |
| `program_name` | text | O | 사업명 |
| `application_start_date` | date | | 접수 시작일 |
| `application_end_date` | date | | 접수 종료일. 시작일 이상 |
| `jurisdiction_org` | text | | 소관 기관 |
| `executing_org` | text | | 수행 기관 |
| `registered_date` | date | | 등록일 |
| `detail_url` | text | | 상세 링크 |
| `parsed_pblanc_id` | text | | URL 에서 뽑아낸 공고 ID |
| `raw_row` | jsonb | O | CSV 원본 행. 기본 `{}` |
| `created_at` | timestamptz | O | |

## 25. `archive_central_program`

중앙부처 지원사업 CSV **한 줄 그대로.**

| 컬럼 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `id` | bigint | O | PK |
| `import_batch_id` | bigint | O | → `archive_import_batch` CASCADE |
| `source_row_no` | integer | O | CSV 행 번호. 1 이상 |
| `ministry_name` | text | O | 부처명 |
| `category_large` | text | | 대분류 |
| `category_middle` | text | | 중분류 |
| `industry_name` | text | | 업종명 |
| `executing_org` | text | | 수행 기관 |
| `announcement_url` | text | | 공고 링크 |
| `program_type` | text | | 사업 유형 |
| `program_name` | text | O | 사업명 |
| `purpose` | text | | 사업 목적 |
| `content` | text | | 지원 내용 |
| `target` | text | | 지원 대상 |
| `scale_text` | text | | 지원 규모 문구 |
| `description` | text | | 설명 |
| `raw_row` | jsonb | O | CSV 원본 행. 기본 `{}` |
| `created_at` | timestamptz | O | |

---

# 7. 임베딩

## 26. `embedding_model`

임베딩 모델 등록.

| 컬럼 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `id` | bigint | O | PK |
| `provider` | text | O | 제공자 (`openai`) |
| `model_name` | text | O | 모델 이름 (`text-embedding-3-small`) |
| `model_version` | text | O | 모델 버전. 기본 `''` |
| `dimension` | integer | O | **벡터 차원.** 1 이상. 실측 `1536` |
| `distance_metric` | text | O | `COSINE`/`L2`/`INNER_PRODUCT`. 기본 `COSINE` |
| `is_enabled` | boolean | O | 사용 가능 여부. 기본 `true` |
| `created_at` | timestamptz | O | |

**제약** — `UNIQUE(provider, model_name, model_version, dimension)`
**실측 경고** — 392행 대부분이 `provider='test'`, `dimension=2` 인 **테스트 잔여물**이다.

## 27. `embedding_profile`

**임베딩 입력을 어떻게 조립하는가**의 규칙. 규칙이 바뀌면 새 버전을 만들고 기존 벡터는 보존한다.

| 컬럼 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `id` | bigint | O | PK |
| `embedding_model_id` | bigint | O | → `embedding_model` |
| `profile_name` | text | O | 프로파일 이름. `version_no` 와 함께 유일 |
| `version_no` | integer | O | 버전. 1 이상 |
| `profile_kind` | text | O | `SUMMARY`(요약 전체를 한 벡터로) 또는 `CHUNK`(조각별) |
| `field_codes` | text[] | O | **어떤 축을 입력에 넣을지.** 기본 `{}` |
| `input_template` | text | | 입력 텍스트 조립 템플릿 |
| `chunking_profile_id` | bigint | | → `chunking_profile`. `CHUNK` 종류일 때만 |
| `configuration` | jsonb | O | 부가 설정. 기본 `{}` |
| `preprocessing_version` | text | O | **결정적 전처리 규칙 버전.** 기본 `detail-ref-v1`. 이게 바뀌면 새 프로파일 버전과 새 벡터가 필요하다 |
| `is_active` | boolean | O | 활성 여부. 기본 `false`. **실측 392행 중 활성은 1개** |
| `created_at` | timestamptz | O | |

## 28. `inspection_embedding`

**요청서 1건의 검색용 벡터.**

| 컬럼 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `id` | bigint | O | PK |
| `inspection_case_id` | bigint | O | → `inspection_case` CASCADE |
| `embedding_profile_id` | bigint | O | → `embedding_profile` |
| `input_text` | text | O | **임베딩에 실제로 보낸 텍스트.** 공백만이면 거부. 실측 438자 |
| `input_sha256_hex` | char(64) | O | 입력 해시. **같은 입력이면 재계산하지 않는다** |
| `embedding` | vector | O | 벡터. 실측 1536차원 |
| `created_at` | timestamptz | O | |

**제약** — `UNIQUE(inspection_case_id, embedding_profile_id)` — 검사 건 × 프로파일당 하나
**쓰는 곳** — [retrieval.py:275](../backend/app/services/retrieval/retrieval.py:275)

## 29. `announcement_embedding`

**공고 1버전의 검색용 벡터.** 비교 대상 코퍼스.

| 컬럼 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `id` | bigint | O | PK |
| `announcement_version_id` | bigint | O | → `announcement_version` CASCADE |
| `embedding_profile_id` | bigint | O | → `embedding_profile` |
| `input_text` | text | O | **5개 축을 조립한 정확한 입력 텍스트.** 참조 보일러플레이트는 이 파생 입력에서만 뺀다. 한 축이 비면 그 축을 빼고 나머지로 임베딩하며, 보일러플레이트를 되살리지 않는다 |
| `input_sha256_hex` | char(64) | O | 입력 해시 |
| `embedding` | vector | O | 차원 무제한 저장. **트리거가 프로파일에 등록된 차원을 강제한다.** 비교는 같은 프로파일 안에서만 |
| `created_at` | timestamptz | O | |

**제약** — `UNIQUE(announcement_version_id, embedding_profile_id)`

## 30. `chunk_embedding`

조각 벡터. **0행, 코드 참조 없음.**

| 컬럼 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `id` | bigint | O | PK |
| `document_chunk_id` | bigint | O | → `document_chunk` CASCADE |
| `embedding_profile_id` | bigint | O | → `embedding_profile` |
| `input_text` | text | O | 이 조각·프로파일로 보낸 정확한 텍스트 |
| `input_sha256_hex` | char(64) | O | 입력 해시 |
| `embedding` | vector | O | 벡터 |
| `created_at` | timestamptz | O | |

---

# 8. 유사공고 검색과 SIM 비교

## 31. `retrieval_run`

유사공고 검색 **실행 1회.** 그때의 조건을 박제해 재현성을 만든다.

| 컬럼 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `id` | bigint | O | PK |
| `inspection_case_id` | bigint | O | → `inspection_case` CASCADE |
| `inspection_embedding_id` | bigint | O | → `inspection_embedding` CASCADE. 어느 벡터로 찾았나 |
| `source_sync_run_id` | bigint | | → `api_sync_run` SET NULL. 어느 동기화 시점 코퍼스인가 |
| `status` | text | O | 기본 `PENDING`. `PENDING`/`RUNNING`/`SUCCESS`/`FAILED` |
| `top_k_used` | smallint | O | 몇 건까지 뽑았나. 1~100. 실측 `5` |
| `corpus_snapshot_at` | timestamptz | O | **코퍼스 기준 시각.** 이 시점의 공고들과 비교했다 |
| `filter_snapshot` | jsonb | O | **검색 조건 박제.** 기본 `{}`. 실측: `{source_code, search_status:[OPEN,UNKNOWN], is_current:true, embedding_profile_id}` |
| `started_at` | timestamptz | | 시작 시각 |
| `completed_at` | timestamptz | | 종료 시각 |
| `error_code` | text | | 실패 코드. 실측 `RETRIEVAL_NOT_READY` |
| `error_message` | text | | 실패 상세 |
| `created_at` | timestamptz | O | |

## 32. `retrieval_candidate`

**후보 공고 1건 + 그 공고와의 SIM 비교 결과.** 화면의 유사공고 카드 하나가 이 행이다.

| 컬럼 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `id` | bigint | O | PK |
| `retrieval_run_id` | bigint | O | → `retrieval_run` CASCADE |
| `announcement_version_id` | bigint | O | → `announcement_version`. 어느 공고 버전 |
| `rank_no` | smallint | O | **순위.** 1부터. 실행 안에서 유일. **API 응답에는 안 나간다** (배열 순서가 곧 순위) |
| `vector_distance` | double precision | O | 벡터 거리. 작을수록 유사. 실측 `0.5686` |
| `vector_similarity` | double precision | | 유사도 `1 - distance`. -1~1. 실측 `0.4314`. **API 응답에는 안 나간다** |
| `status_verification` | text | O | 접수 상태를 검증했나. 기본 `NEEDS_CONFIRMATION`. `VERIFIED_OPEN`/`VERIFIED_CLOSED`/`NEEDS_CONFIRMATION` |
| `is_presented` | boolean | O | **화면에 보여줄 후보인지.** 기본 `true` |
| `same_jurisdiction_org` | boolean | | 소관 기관이 같은가 |
| `same_executing_org` | boolean | | 수행 기관이 같은가 |
| `period_relation` | text | O | 요청서 사업기간과 공고 접수기간의 관계. 기본 `UNKNOWN`. 아래 5개 |
| `detail_parse_status` | text | O | 첨부 상세 파싱 상태. 기본 `NOT_REQUESTED`. Top-K 후보는 전부 파싱 대상이 될 수 있고, `detail_ref_fields` 는 우선순위일 뿐 유일한 관문이 아니다. 미지원·손상·암호 파일은 `FAILED` 로 남고 요약 결과는 유지된다 |
| `comparison_summary` | text | | **후보 전체 비교 요약 한 문단.** API 응답에 그대로 나간다 |
| `comparison_result` | jsonb | O | **SIM 4축 비교 결과 전체.** 기본 `{}`. 키: `axes`, `rank`, `title`, `source_url`, `comparison_summary`, `semantic_similarity`, `semantic_similarity_display`, `weighted_score`, `review_grade`, `assessable_axis_count`, `warnings`, `ruleset_version`, `prompt_version`, `model_profile`, `scoring_version`, `announcement_id`, `announcement_version_id` |
| `created_at` | timestamptz | O | |

**`detail_parse_status` 값** — `NOT_REQUESTED`/`PARSING`/`SUCCESS`/`PARTIAL_SUCCESS`/`FAILED`
**`period_relation` 값** — `OVERLAP`(겹침)/`NO_OVERLAP`(안 겹침)/`OPEN_ENDED`(종료일 없음)/`BY_SUBPROGRAM`(내역사업별)/`UNKNOWN`
**`comparison_result.axes`** — `purpose`(사업 목적)/`target`(지원 대상)/`content`(지원 내용)/`delivery`(수행 체계). 축별 상태는 `SIMILAR`/`PARTIAL`/`DIFFERENT`/`INSUFFICIENT`
**제약** — `UNIQUE(retrieval_run_id, rank_no)`, `UNIQUE(retrieval_run_id, announcement_version_id)`

## 33. `candidate_evidence`

SIM 비교의 **원문 근거.** 어느 쪽 문서의 어느 대목을 보고 그렇게 판단했는가.

| 컬럼 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `id` | bigint | O | PK |
| `retrieval_candidate_id` | bigint | O | → `retrieval_candidate` CASCADE |
| `evidence_side` | text | O | **어느 쪽 근거인가.** `REQUEST`(요청서) / `ANNOUNCEMENT`(공고) / `COMPARISON`(비교 자체) |
| `field_code` | text | | 어느 축·항목의 근거인가 |
| `document_chunk_id` | bigint | | → `document_chunk` SET NULL. 청킹 미사용이라 항상 `NULL` |
| `page_no` | integer | | 페이지 번호. 1 이상 |
| `source_locator` | jsonb | O | 문서 내 위치. 기본 `{}` |
| `excerpt` | text | | **원문 발췌.** API 응답의 `excerpt` 가 이 값 |
| `explanation` | text | | 왜 이 대목이 근거인지 |
| `created_at` | timestamptz | O | |

**실측** — 3,119건 (`REQUEST` 2,199 / `ANNOUNCEMENT` 920). `COMPARISON` 은 0건.

---

# 9. 보고서와 대화

## 34. `inspection_report`

**최종 보고서 JSON.** 검사 건당 하나이며 **UPDATE 가 트리거로 차단된 불변 행**이다. 고치려면 새 검사 건을 만들어야 한다.

| 컬럼 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `id` | bigint | O | PK |
| `inspection_case_id` | bigint | O | → `inspection_case` CASCADE. **유일** |
| `missing_check_run_id` | bigint | O | → `missing_check_run` CASCADE. 어느 CPL 실행의 결과인가 |
| `retrieval_run_id` | bigint | O | → `retrieval_run` CASCADE. 어느 검색 실행의 결과인가 |
| `report_schema_version` | text | O | 보고서 스키마 버전. 실측 `alpha-report-v0.1` |
| `report_json` | jsonb | O | **보고서 전체.** 아래 키 목록 참고 |
| `finalized_at` | timestamptz | O | 확정 시각 |
| `created_at` | timestamptz | O | |

**`report_json` 최상위 키 (내부 모델)**

| 키 | 뜻 | API 로 나가나 |
|---|---|---|
| `case` | 파일명·완료 시각 | O (`case`) |
| `self_check` | **CPL 결과** | O (`report.cpl`) |
| `structural_consistency` | **FIT 결과** | O (`report.fit`) |
| `similar_candidates` | **SIM 결과** | O (`report.similar_candidates`) |
| `review_issues` | 통합 검토 이슈 | X — 없앴다 |
| `ben_references` | 수혜 참조 | X — 미구현 |
| `differences` | 차이 목록 | X — 미구현 |
| `warnings` | 경고 모음 | X |
| `ui_status` | 화면 상태 힌트 | X |
| `schema_version` | 스키마 버전 | X |

**주의** — DB 의 `report_json` 은 **API 응답 모양이 아니다.** [reporting.py:217](../backend/app/services/reporting.py:217) 의 `_report_response()` 가 깎아서 내보낸다.

## 35. `output_artifact`

생성된 **PDF 의 메타데이터.** 파일은 디스크에 있다.

| 컬럼 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `id` | bigint | O | PK |
| `inspection_report_id` | bigint | O | → `inspection_report` CASCADE. 유일 |
| `file_asset_id` | bigint | O | → `file_asset` CASCADE. 유일. 여기서 `storage_key` 를 얻는다 |
| `output_format` | text | O | **`PDF` 로 고정**. CHECK 가 다른 값을 막는다 |
| `template_version` | text | O | PDF 템플릿 버전. 실측 `alpha-pdf-v0.1` |
| `generated_at` | timestamptz | O | 생성 시각. 기본 `now()` |

## 36. `chat_session`

검사 건당 **대화방 1개.**

| 컬럼 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `id` | bigint | O | PK |
| `inspection_case_id` | bigint | O | → `inspection_case` CASCADE. **유일** |
| `created_at` | timestamptz | O | |

## 37. `chat_message`

**질문·답변 본문 1개.**

| 컬럼 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `id` | bigint | O | PK. API 응답의 `id` |
| `chat_session_id` | bigint | O | → `chat_session` CASCADE |
| `sequence_no` | integer | O | **대화 순번.** 1부터. 세션 안에서 유일. 시간순 정렬의 기준 |
| `role` | text | O | `USER` 또는 `ASSISTANT` |
| `content` | text | O | **본문.** 공백만이면 거부. 질문은 1~4000자 |
| `model_name` | text | | 답변한 모델. 실측 `gpt-4o-mini`. `USER` 행은 `NULL` |
| `model_version` | text | | 모델 버전 |
| `input_tokens` | integer | | 입력 토큰 수. 0 이상. **코드가 안 채운다** |
| `output_tokens` | integer | | 출력 토큰 수. 0 이상. **코드가 안 채운다** |
| `evidence_refs` | jsonb | O | 답변이 참조한 근거. 기본 `[]` |
| `created_at` | timestamptz | O | 작성 시각. API 응답의 `created_at` |

**제약** — `UNIQUE(chat_session_id, sequence_no)`

---

# 부록 A. 상태값 한눈에

| 테이블 | 컬럼 | 값 |
|---|---|---|
| `inspection_case` | `status` | `UPLOADED` `PARSING` `CHECKING` `RETRIEVING` `REPORTING` `COMPLETED` `FAILED` |
| `document_parse_run` | `status` | `PENDING` `PARSING` `SUCCESS` `PARTIAL_SUCCESS` `FAILED` |
| `request_extraction` | `status` | `PENDING` `RUNNING` `SUCCESS` `PARTIAL_SUCCESS` `FAILED` |
| `request_extraction` | `request_reason` | `DETAIL_NEW` `SUBPROGRAM_NEW` `SUBSUBPROGRAM_NEW` `CONTENT_CHANGE` `UNKNOWN` |
| `missing_check_run` | `status` | `PENDING` `RUNNING` `SUCCESS` `FAILED` |
| **`missing_check_item`** | **`result_status`** | **`PRESENT` `MISSING` `NOT_APPLICABLE` `NEEDS_CONFIRMATION` `PARSE_FAILED`** |
| `file_asset` | `asset_scope` | `USER` `SHARED` |
| `uploaded_document` | `declared_format` | `HWP` `HWPX` `PDF` |
| `form_field_definition` | `data_type` | `TEXT` `DATE` `NUMBER` `BOOLEAN` `CHOICE` `JSON` |
| `data_source` | `source_type` | `API` `FILE` |
| `api_sync_run` | `status` | `PENDING` `RUNNING` `SUCCEEDED` `FAILED` |
| `announcement_version` | `period_type` | `FIXED` `UNTIL_EXHAUSTED` `ALWAYS` `UNTIL_FILLED` `BY_SUBPROGRAM` `VARIABLE` `UNKNOWN` |
| `announcement_version` | `search_status` | `OPEN` `CLOSED` `UNKNOWN` |
| `announcement_attachment` | `attachment_role` | `PRIMARY` `AUXILIARY` |
| `announcement_attachment` | `fetch_status` | `NOT_REQUESTED` `DOWNLOADING` `DOWNLOADED` `FAILED` |
| `embedding_model` | `distance_metric` | `COSINE` `L2` `INNER_PRODUCT` |
| `embedding_profile` | `profile_kind` | `SUMMARY` `CHUNK` |
| `retrieval_run` | `status` | `PENDING` `RUNNING` `SUCCESS` `FAILED` |
| `retrieval_candidate` | `status_verification` | `VERIFIED_OPEN` `VERIFIED_CLOSED` `NEEDS_CONFIRMATION` |
| `retrieval_candidate` | `period_relation` | `OVERLAP` `NO_OVERLAP` `OPEN_ENDED` `BY_SUBPROGRAM` `UNKNOWN` |
| `retrieval_candidate` | `detail_parse_status` | `NOT_REQUESTED` `PARSING` `SUCCESS` `PARTIAL_SUCCESS` `FAILED` |
| `candidate_evidence` | `evidence_side` | `REQUEST` `ANNOUNCEMENT` `COMPARISON` |
| `output_artifact` | `output_format` | `PDF` (고정) |
| `chat_message` | `role` | `USER` `ASSISTANT` |

FIT 관계 상태(`FIT`/`NEEDS_REVIEW`/`CONFLICT`/`INSUFFICIENT`)와 SIM 축 상태(`SIMILAR`/`PARTIAL`/`DIFFERENT`/`INSUFFICIENT`)는 **DB 컬럼이 아니라 `report_json`·`comparison_result` jsonb 안**에 있다. CHECK 가 없다.

# 부록 B. DDL 에만 있고 코드가 안 채우는 컬럼

| 테이블 | 컬럼 |
|---|---|
| `request_field_value` | `raw_text` `page_no` `confidence` `source_bbox` |
| `request_extraction` | `confidence` |
| `chat_message` | `input_tokens` `output_tokens` `model_version` |
| `file_asset` | `source_url` (공고 첨부용, 미사용) |
| `api_sync_run` | `resume_cursor` `statistics` |

# 부록 C. 팀 Supabase 실측 현황 (2026-09-04)

```
54 업로드  →  51 파싱  →  47 완료
   3건 파싱 전 사망        4건 검색·보고 단계 사망
```

`inspection_embedding` · `retrieval_run` · `inspection_report` · `output_artifact` 가 **모두 47로 일치**한다. 완료된 건은 빠짐없이 벡터·검색·보고서·PDF 를 갖고 있다.
`missing_check_item` 663 = 51 × 13 으로 정확히 맞는다.

## 손봐야 할 것 넷

1. **`object_delete_outbox` 2,868건이 전부 미처리다.** 소비자가 없어 실제 파일이 디스크에 계속 남는다.
2. **`form_schema` 391행, `embedding_model`·`embedding_profile` 각 392행이 테스트 잔여물이다.** `report-<uuid>` 이름과 `provider='test'`, `dimension=2` 가 팀 Supabase 에 섞여 있다. [README](../backend/README.md) 가 팀 Supabase 를 `TEST_DATABASE_URL` 로 쓰지 말라고 못박아 두었는데 어딘가에서 새어 들어갔다.
3. **`app_user` 4개 중 2개가 테스트 계정이다** — `retrieval-c8d7acc8e0@example.invalid`, `e2e-demo@example.invalid`.
4. **보관 기간 장치가 없다.** 스키마 전체에 `retention_expires_at`·`expires_at` 류 컬럼이 0개다. 한 번 들어간 분석 이력과 대화는 계정을 지우기 전까지 영구다.

## 미사용 테이블 10개

| 테이블 | 상태 |
|---|---|
| `password_change_history`, `object_delete_outbox` | 트리거가 채우고 **아무도 안 읽는다** |
| `chunking_profile`, `document_chunk_set`, `document_chunk`, `chunk_embedding` | 앱 코드 참조 0. 청킹 파이프라인 미착수 |
| `data_source` | seed 3건만 있고 코드가 조회하지 않는다 |
| `announcement_subprogram_period`, `archive_import_batch`, `archive_bizinfo_listing`, `archive_central_program` | **저장소 전체에서 `schema.sql` 외 등장 0** |
