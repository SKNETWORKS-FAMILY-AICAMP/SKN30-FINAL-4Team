# 백엔드 작업 인계 — 2026-09-08

브랜치 `이동욱`. 마지막 커밋 `564555d`. **아래 작업은 전부 미커밋 상태다.**

기준: `docs/Pre-review_백엔드_코드베이스_재설계_초안_v0.2_260907.md`
어휘 우선순위: **프론트 명세 4종 > AGENTS.md > 재설계 초안**

---

## 1. 이번에 한 것

### 1-1. Edge 업로드 진입점 (RUN-01 / RUN-03)

프론트 명세 SCR-004의 3단계 업로드를 기존 분석 큐에 연결했다.

| 파일 | 내용 |
| --- | --- |
| `app/db/migrations/supabase/103_analysis_run_upload.sql` | `uploading` 상태 + 업로드 예약 3칸 |
| `app/services/analysis_run_upload.py` | RUN-01/RUN-03 판정 로직 |
| `app/api/v1/analysis_runs.py` | `POST /functions/v1/edge-analysis-run-create`, `.../edge-analysis-run-complete-upload` |
| `tests/test_analysis_run_upload.py` | 16건 |
| `app/services/case_upload.py` | `record_uploaded_document` / `validate_declared_upload` 추출 (멀티파트 경로와 공유) |
| `worker/dispatcher.py` | `QueueJobDispatcher.dispatch(run_id)` 추출 (삽입 없이 실행만) |

**설계 요점**

- object key는 `request-source/<user-uuid>/<run-pk>/source.<ext>` — **저장하지 않고 서버가 매번 재계산**한다. 클라이언트가 bucket/key를 보내도 무시된다.
- RUN-03은 저장소에서 **바이트를 다시 읽어** 매직바이트·크기·확장자 일치를 검사한다. 확장자를 믿지 않는다.
- 케이스 + 원본 2행 + `uploading→queued`가 **한 트랜잭션**이다. 중간 상태가 생길 자리가 없다.
- 멱등성은 `UPDATE ... WHERE status='uploading'`의 rowcount 하나다. 동시 요청 중 진 쪽은 자기 쓰기를 통째로 롤백한다.
- 두 번째 큐를 만들지 않았다. `worker/jobs.claim_next`가 바로 그 run_pk를 집는 것으로 확인.

### 1-2. 마이그레이션 러너

| 파일 | 내용 |
| --- | --- |
| `app/db/scripts/apply_migrations.py` | autocommit + 순서 고정, `--dry-run` / `--on-supabase` / `--with-schema` |
| `tests/test_apply_migrations.py` | 4건 |
| `app/db/migrations/supabase/000_auth_stub.sql` | `CREATE OR REPLACE FUNCTION auth.uid()` → **없을 때만 생성**으로 변경 |

**중요 — 두 개의 함정을 실제로 밟았다.**

1. **자체 COMMIT이 없는 파일이 조용히 롤백된다.** 팀원 파일(01~09)은 자기 안에 `BEGIN; ... COMMIT;`이 있어 스스로 커밋하지만 우리 `001`/`002`는 없다. 커밋하지 않는 커넥션으로 돌리면 **오류 한 줄 없이** 사라진다. `sims_test`는 `schema.sql`이 이미 그 컬럼을 갖고 있어 `002`가 no-op이라 기존 테스트로는 영원히 안 잡힌다. → 러너는 반드시 `isolation_level="AUTOCOMMIT"`.
2. **`000_auth_stub.sql`이 진짜 Supabase를 망가뜨릴 뻔했다.** `CREATE OR REPLACE FUNCTION auth.uid()`가 Supabase 자신의 `auth.uid()`를 덮어쓴다. 우리 스텁은 구버전 PostgREST의 `request.jwt.claim.sub`만 읽는데 지금 PostgREST는 `request.jwt.claims`(JSON)를 설정하므로, 덮어쓰면 `auth.uid()`가 항상 NULL → **그 프로젝트 RLS 전체가 모든 요청을 거부**한다. 지금은 "없을 때만 생성"이라 안전하다.

### 1-3. RUN-04 Realtime 결과 지시자

| 파일 | 내용 |
| --- | --- |
| `app/db/migrations/supabase/104_analysis_run_outcome.sql` | `analysis_case_pk`, `error_code`, `error_message` + 짝 CHECK |
| `worker/outcome.py` | 예외 → 안전한 코드·문구 고정표 |
| `worker/jobs.py` | `complete()`가 case_pk 기록·오류 소거, `fail()`이 안전한 쌍 기록 |
| `tests/test_worker_outcome.py` | 8건 |

**설계 요점**

- Realtime은 바뀐 **행 자체**를 보낸다. 조인해 주지 않는다. `analysis_case_pk`가 이 행에 없으면 분석이 끝나도 화면이 결과를 못 찾는다.
- 오류 칸이 두 벌이다: `last_error`(운영용, 예외 원문) / `error_code`+`error_message`(화면용, **고정표에서만**).
- 규칙 하나: **예외에서 나온 문자열은 문구가 되지 못한다.** 화이트리스트에 없는 reason code는 기본 문구로 떨어진다.
- `analysis_case_pk`는 결과 행과 **같은 트랜잭션**에서 쓴다. 임대를 뺏기면 함께 롤백된다.
- FK는 걸지 않았다 — `result.analysis_case.source_analysis_run_id`가 이미 UNIQUE 정방향 링크라 순환이 된다.

### 1-4. 보고서 다운로드 (`edge-report-create-download-url`)

| 파일 | 내용 |
| --- | --- |
| `app/api/v1/report_download.py` | 발급 POST + 수령 GET |
| `app/core/security.py` | 다운로드 토큰 발급·해독 |
| `app/services/reporting.py` | `authorize_report_download`, `open_report_artifact` |
| `app/ports/object_storage.py` | `storage_key(bucket, key)` — 업로드와 공유 |
| `app/core/config.py` | `report_download_ttl_seconds` (기본 60) |
| `tests/test_report_download.py` | 13건 |

**설계 요점**

- 화면은 `window.location.href = signed_url`로 이동한다. **그 이동에는 `Authorization` 헤더가 붙지 않는다.** 그래서 URL 자체가 자격증명이어야 한다. 기존 `GET /api/v1/cases/{id}/report`는 Bearer를 요구하므로 이 용도로 못 쓴다 — 중복이 아니다.
- 소유권은 **발급 시점에만** 본다. 토큰에 사용자를 담지 않는 이유이자 수명이 60초인 이유다. 상태·만료는 수령 때 다시 본다.
- **다운로드 조건을 `api.rpc_get_analysis_result`의 `report.can_download`와 글자 그대로 같게 썼다.** 화면은 그 값으로 버튼을 켠 뒤 여기로 온다. 어긋나면 켜진 버튼이 409를 받는다. 테스트가 RPC를 직접 호출해 두 값이 함께 움직이는지 본다.
- 토큰 경계는 **함수 수준**에서 고정했다. 엔드포인트만 보면 지금은 `sub`가 `"1"`이라 UUID 파싱에서 우연히 걸리는데, Supabase Auth로 옮기면 `sub`가 UUID가 되어 그 우연이 사라진다.

### 1-5. DB 상태

로컬 도커 `sims-e2e-pg`(= `pgvector/pgvector:pg15`, **Supabase 아님**)의 `sims` DB에 마이그레이션 17개 적용 완료.
공고 1660건 / 임베딩 1541건 / 검사건 45건 그대로. `external_uuid` 백필됨.

`.env`의 `DATABASE_URL`은 **Supabase Cloud**를 가리킨다(공고 6건). **적용하지 않았다.**

---

## 2. 검증 상태

- 전체 회귀 **698 passed, 12 skipped, 0 failed** (실 PostgreSQL, DB 초기화 없이 연속 2회)
- 팀원 벤더 파일 9개 해시 OK (`app/db/migrations/supabase/verify_vendor_hashes.py`)
- 역검증(사보타주) 8건 전부 잡힘

```bash
export TEST_DATABASE_URL="postgresql+psycopg://postgres:simstest@127.0.0.1:55533/sims_test"
cd backend && ./.venv/Scripts/python.exe -m pytest tests -q
```

---

## 3. LLM 실측 결과 — **여기가 지금 최대 문제**

RunPod vLLM에 SSH 터널로 붙어 실측했다.
`gemma-12b` = `google/gemma-4-12B-it-qat-w4a16-ct`, 컨텍스트 32768, vLLM 0.28.1.

```bash
ssh -N -T -i ~/.ssh/id_ed25519 -L 127.0.0.1:8010:127.0.0.1:8002 \
  root@69.30.85.152 -p 22109 -o ServerAliveInterval=30 -o ExitOnForwardFailure=yes
```

로컬 8000은 우리 백엔드 uvicorn이 점유 중이라 **8010**을 썼다.

### 3-1. 해소된 위험

- 실제 `RequestSourceSelectionV012` 스키마(**8,157자 / `$defs` 15 / 깊이 7**)가 우리 `VllmLLMClient`를 통해 **2.9초**에 통과. guided decoding 컴파일 문제 없음.
- vLLM 0.28은 `/v1/responses`도 서빙한다(200). 예전 기록의 "지원 안 함"은 이 빌드에선 틀렸다. 워커는 `/chat/completions` 어댑터를 쓰므로 영향 없음.

### 3-2. 실제 요청서 10건 결과

샘플: `.codex-merge-develop/samples/hwpx/`

| 결과 | 건수 | 대상 |
| --- | --- | --- |
| `request_type` 옵션 컨테이너 없음 → LLM 도달 못 함 | 4 | `mockup_07`, `mockup_08`, `사전협의요청서_미흡사례`, `사전협의요청서_우수사례` |
| 선택 단계 도달 | 6 | `mockup_01`~`mockup_06` |
| 실제 LLM 호출한 것 | 4 | **전부 실패** |

```
mockup_01   20.3s  legacy value_anchor requires both source_block_id and anchor_text
mockup_04   17.5s  value_span_candidate_id must not be combined with anchor_text
mockup_05   15.3s  legacy value_anchor requires both source_block_id and anchor_text
mockup_03  455.2s  JSON 파싱 자체 실패 (컨텍스트 소진 추정)
```

### 3-3. 근본 원인

**guided decoding은 JSON Schema만 강제한다.** `RequestSourceSelectionV012`의 앵커 제약은 Pydantic `model_validator`에 있어 JSON Schema로 내려가지 않는다. 그래서 모델이 *스키마는 만족하지만 검증은 실패하는* 출력을 낸다.

**그리고 그 실패에 보완 루프가 닿지 않는다.**

- 패키지 `select_and_materialize_with_repairs`(`packages/profile_structuring/semantic_structuring/run_request_profile_v012.py:502`)는 **materialization** 실패(`ValueError`)만 재시도한다.
- selector 실패(`RequestSourceSelectionParseError`)는 그대로 re-raise.
- 우리 어댑터는 스키마 검증 실패를 `LLMInvalidResponseError`로 바꿔 던지므로 루프 밖으로 나간다.

→ **앵커 오류는 재시도 0회.** 초안의 "제한 보완 루프"가 명세만 있고 이 이음매에 연결돼 있지 않다.

### 3-4. 부수 발견

- **`cpl_llm_timeout_seconds` 기본 30초.** 실측 15~455초. 현 설정이면 대부분 타임아웃한다.
- **어댑터가 `finish_reason`을 보지 않는다.** 절단된 응답이 "잘못된 응답"과 구분되지 않는다.

### 3-5. 조사 중 내가 낸 오진 2건 (같은 실수 반복 방지용)

두 번 다 **probe 스크립트 결함**이었다. 실제 코드에는 그 결함이 없다.

1. "출력 예산 부족" — 내 probe가 넣은 `max_tokens: 8192` 때문. 어댑터는 `max_tokens`를 보내지 않고, 그 경우 vLLM이 잔여 컨텍스트를 다 쓴다(14,833토큰, `finish_reason=stop`).
2. "모델이 레지스트리 밖 `field_name`을 만든다" — 내 probe가 9,015자 시스템 프롬프트를 빠뜨린 탓. 진짜 프롬프트로는 안 난다.

**교훈: 반드시 `make_vllm_selector` 경로로 재현해라. 직접 조립한 요청 본문으로 판단하지 마라.**

---

## 4. 백엔드 조립까지 남은 것

### 우선순위 1 — 지금 이것 때문에 요청서가 한 건도 안 통과한다

**(가) 선택 단계 보완 루프**
`worker/profiles.py`의 `make_vllm_selector`에 붙인다. **패키지 수정 불필요** — `LLMInvalidResponseError.raw`가 이미 검증 실패한 원본을 들고 있으므로, 검증 오류를 프롬프트로 되먹여 재호출하면 된다. 초안의 보완 루프를 올바른 이음매에 다는 것이다.
함께: `cpl_llm_timeout_seconds` 상향, 어댑터의 `finish_reason` 확인(절단을 별도 사유로 구분).

**(나) `request_type` 없는 요청서 4건**
팀원 패키지가 `raise ValueError("request_type option container is missing or ambiguous in CandidatePack")`로 거부한다. 목업 서식 문제인지 파서 문제인지 팀원 확인 필요. 참고로 이 `ValueError`는 `StageError`가 아니라 **raw 예외로 새어 나간다** — 진단이 남지 않는다.

### 우선순위 2 — 우리 코드, vLLM 불필요

**(다) PDF를 큐 경로에 연결** *(가장 큰 덩어리)*
지금 큐 경로(`worker/analysis.py::analyse_case`)는 PDF를 만들지 않는다. `finalize_report`는 레거시 `analysis_pipeline.py`에서만 불리고 레거시 `sims` 실행 기록(`missing_check_run_id`, `retrieval_run_id`)에 묶여 있어 그대로 못 쓴다.
→ `compose_report` + 렌더러는 재사용하고 저장만 `result.report_artifact` + `report_status='ready'`로 새로 잇는다. 디스패처의 펜싱 트랜잭션 안에서.
**1-4의 다운로드 엔드포인트는 이게 끝나야 실제로 동작한다.**

**(라) api 표면 4개**
프론트 명세가 요구하는데 없는 것:
`v_active_analysis_session`, `v_my_analysis_history`, `rpc_touch_active_analysis_session`, `rpc_close_active_analysis_session`

### 우선순위 3 — 외부 의존

**(마) SIM 후보 0건**
`sims.announcement_profile`이 `OK=0, FAILED=7`이다. `search_candidates`가 `ap.status='OK'`로 조인하므로 후보가 0이고 SIM 칸이 빈다. 공고 ExistingProfile 3단계 LLM 체인이 미배선. vLLM 필요.

**(바) 임베딩 모델 결정**
`OPENAI_BASE_URL` **하나**가 LLM과 임베딩 클라이언트 양쪽에 들어간다. vLLM으로 돌리면 임베딩도 같이 옮겨간다. 기존 말뭉치 1541건은 OpenAI `text-embedding-3-small` **1536차원**이고 `embed_texts`가 모델명·차원을 검증하므로, 임베딩 모델을 바꾸면 **1541건 재적재**가 따라온다.
→ 가장 싼 길: LLM만 vLLM, 임베딩은 OpenAI 유지. 그러려면 base_url을 LLM/임베딩으로 쪼개는 작은 설정 분리가 필요하다.

### 범위 밖 — 팀원 담당

채팅(`edge-conversation-create-message`, `edge-conversation-retry-message`, `v_conversation_messages`). 우리는 경계만 유지하고 **추측해서 구현하지 않는다.**

---

## 5. 자체 호스팅 Supabase

팀원이 로컬 호스팅 Supabase에 자기 스키마를 이미 올려 뒀다. **우리 스키마를 대신 올리는 게 아니라 그 위에 우리 추가분만 얹는다.**

```bash
uv run python -m app.db.scripts.apply_migrations \
  --on-supabase --with-schema --database-url "<self-hosted URL>"
```

`--on-supabase`는 팀원 `01~09`와 `000_auth_stub`을 건너뛴다. 특히 `08_rls_policies.sql`은 `DROP POLICY` 후 `CREATE`라 다시 돌리면 팀원이 고친 정책이 우리 버전으로 되돌아간다.

**예행연습 완료** — 팀원 상황을 재현한 DB(01~09 선적용 + 진짜 `auth.uid()` + 3컬럼 `auth.users`)에 1.26초에 적용했고, `auth.uid()` 원본 보존·`auth.users` 무손상·`vector` 자동 설치를 확인했다. 그 DB에서 RUN-01→Storage→RUN-03→claim 전 구간도 돌렸다.

**pgvector는 `schema.sql` 10행이 `CREATE EXTENSION IF NOT EXISTS vector`를 스스로 한다.** 우리 롤에 확장 생성 권한이 있으면 자동이고, 없으면 팀원이 한 번만 실행하면 된다.

**왜 필요한지**: 팀원 `02_core_ddl.sql:14`에 `Deliberately excludes pgvector embedding table (dimension not fixed yet)`. 팀원 스키마가 임베딩 테이블을 일부러 뺐고 그 자리를 우리 `sims.announcement_embedding`이 채운다.

**두 스키마는 같은 DB여야 한다.** 우리 코드가 한 쿼리에서 조인한다:
- `workspace.analysis_run` ⋈ `sims.inspection_case`
- `sims.announcement_embedding` ⋈ `kb.source_profile/source_version/profile_version`
- `sims.app_user` → `auth.users` + `app.user_profile`

**미검증**: `ensure_identity`가 `auth.users`에 직접 INSERT한다. GoTrue를 거치지 않으므로 그 행은 FK 앵커일 뿐 로그인 가능한 계정이 아니다. 진짜 Supabase에서 그 INSERT가 허용되는지 확인 안 됐다.

---

## 6. 로컬에서 검증 불가능한 범위

로컬은 순수 PostgreSQL이라 아래는 전부 미검증이다. **Fake로 통과한 것을 실제 연동 완료로 보고하지 마라.**

- Supabase Storage 실제 업로드 (RUN-02는 우리 코드가 아니다 — 브라우저가 직접 올린다)
- Edge Functions (Deno 런타임). 지금은 FastAPI가 같은 경로·같은 응답 모양으로 서빙한다
- Realtime 구독 (RUN-04). 확인한 것은 "행에 값이 올바르게 들어간다"까지다
- PostgREST 경유 호출 — `supabase.rpc()`, `.from('v_...')`
- RLS + 실제 JWT (`auth.uid()`가 스텁)

---

## 7. 작업 규칙 (사용자 지시)

- **임의로 commit·push·pull·merge·rebase·reset·restore·checkout·stash 금지.** 승인받고 한다
- `packages/**` 수정 금지. 팀원 벤더 파일도 바이트 보존 (`verify_vendor_hashes.py`로 확인)
- 프론트 파일 수정 금지
- 기존 사용자 변경·미추적 파일 보존
- 새 의존성은 꼭 필요할 때만
- 기존 FastAPI 흐름을 복제해 두 번째 시스템을 만들지 않는다
- 통합 테스트는 공유 `sims`/`sims_test`에 DDL을 실행하지 말고 fixture 전용 DB를 만든다
- 리팩토링·품질보다 **백엔드 조립을 먼저** 끝낸다
