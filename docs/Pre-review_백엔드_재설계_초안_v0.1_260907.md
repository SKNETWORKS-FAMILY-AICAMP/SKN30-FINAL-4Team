# Pre-review 백엔드 재설계 초안

2026-09-07 · v0.1 초안 · 작성 근거는 현행 코드 실측과 팀 Supabase 실측이다

FastAPI 백엔드를 걷어내고 **Supabase + 워커** 구조로 옮기는 설계안이다. 확정된 것과 아직 안 정해진 것을 나눠 적었다.

---

## 0. 왜 바꾸나

### 0.1 전제 — 팀에서 정한 것

아래는 이 문서가 **주어진 것으로 받는 결정**이다. 출처는 팀 논의와 `POC_PRODUCT_AND_DATA_DECISIONS.md` 다.

| 결정 | 출처 |
|---|---|
| FastAPI 를 쓰지 않는다 | PoC 문서 1절 |
| 프론트가 Supabase Auth·DB·Storage·Realtime 을 직접 쓴다 | PoC 문서 1절 |
| Python 워커는 HTTP 서버가 아니라 큐에 등록된 작업을 실행하는 프로세스다 | PoC 문서 1절 |
| 57테이블 Supabase 스키마를 유지한다 | PoC 문서 3절 |
| LLM 은 RunPod 에 자체 호스팅한다 | 팀 논의 |
| 큐는 `workspace.analysis_run` 을 그대로 쓴다 | 팀 논의 |

> **[팀 확인 필요]** 이 결정들의 배경(비용·운영·역할 분담 등)은 이 문서가 알지 못한다. 발표 자료나 평가 서술에 쓸 것이라면 팀이 직접 채워야 한다. 아래 0.2 는 그 배경이 아니라, **현행 구조를 실측했을 때 실제로 확인된 문제**다.

### 0.2 현행 구조에서 확인된 문제

전부 팀 Supabase 와 현행 코드 실측이다. 새 구조가 이 문제들을 함께 해결한다.

**① 파일이 한 사람의 노트북에만 있다**

파일 저장 구현이 [`LocalObjectStorage`](../backend/app/infrastructure/local_object_storage.py) 하나뿐이다. `backend/storage/` 는 백엔드를 띄운 그 컴퓨터의 디스크다.

```
DB(팀 Supabase)   file_asset 99행 · output_artifact 47행    ← 모두가 본다
파일(로컬 디스크)  users/{uid}/cases/{cid}/*.hwp             ← 띄운 사람만 있다
```

그래서 다른 팀원이 같은 팀 Supabase 에 붙으면 **이력·결과·대화는 다 보이는데 PDF 다운로드가 `503`** 이다. 재파싱도 불가능하다.

→ 새 구조는 워커가 다른 기계에서 도는 것이 전제라 **Supabase Storage 이전이 필수**가 된다. 이 문제가 함께 사라진다.

**② 서버를 재시작하면 진행 중 분석이 죽는다**

작업 큐가 [`in_process_job_dispatcher.py`](../backend/app/infrastructure/in_process_job_dispatcher.py) **24줄**이고, FastAPI 프로세스 안의 `asyncio` 태스크다. 프로세스가 죽으면 작업도 죽는다.

```
FAILED 7건 중
  ANALYSIS_INTERRUPTED  4건   "The analysis did not survive a server restart"
```

→ 큐가 DB 로 나가면 워커가 죽어도 행이 남는다. 되살리기 SQL 하나로 복구된다.

**③ 인증·API 를 직접 유지하고 있다**

로그인·토큰 발급·비밀번호 재설정·메일 발송을 직접 구현했다. 실측 **약 1,600줄**(`api/` 1,087 + `auth.py` 180 + `password_reset.py` 357)이다.

이 코드가 실제로 문제를 만들고 있다.

- 재설정 방식이 `임시 비밀번호` ↔ `링크+토큰` 으로 두 번 뒤집혔고, 그 과정에서 **API 경로 3개가 문서와 어긋난 채 남아 있다**
- SMTP 계정 오타 하나로 메일이 안 나가는데, 발송을 백그라운드로 던지고 결과를 안 기다려 **화면에는 성공으로 보였다**

→ Supabase Auth 로 옮기면 이 1,600줄이 사라지고, 재설정 흐름도 표준을 따른다.

**④ 스키마가 두 벌이다**

현행 `sims` 37테이블과 팀원이 만든 5스키마 57테이블이 **이름이 하나도 겹치지 않은 채 따로 존재한다.** 둘 다 유지하면 같은 개념을 두 곳에서 관리하게 된다.

| 우리 | 팀원 |
|---|---|
| `inspection_case` | `workspace.analysis_run` + `result.analysis_case` |
| `inspection_report` | `result.axis_result` |
| `retrieval_candidate` | `result.sim_candidate` |
| `chat_message` | `result.conversation_message` |

→ 새 구조는 57테이블 쪽으로 합치는 것을 전제로 한다.

**⑤ 팀원 파서·구조화 결과물을 붙일 자리가 없다**

`common_ir_pipeline`(2,244줄)과 `portable_existing_request_profiles`(14,208줄)가 전달됐다. 현행 FastAPI 구조에서는 이걸 어디에 넣을지가 애매하다.

특히 `ExistingProfile v0.2`(공고 구조화)가 지금 SIM 병목을 직접 겨눈다. 공고 6건의 `target`·`content` 가 전부 비어 있어 **SIM `content` 축이 230건 전부 `INSUFFICIENT`** 다.

→ 워커 구조에서는 "파싱·구조화 단계"가 명시적인 자리를 갖는다.

**⑥ 보관 정책이 없다**

현행 스키마 37테이블 전체에 `retention_expires_at`·`expires_at` 류 컬럼이 **0개**다. 한 번 들어간 분석 이력과 대화가 영구히 남는다.

그리고 `object_delete_outbox` 에 삭제 대기가 **2,868건 쌓여 있는데 처리된 것이 0건**이다. 트리거가 큐에 넣기만 하고 소비자가 없다.

→ 팀원 스키마는 `workspace` 만료와 `result` 90일 보관이 설계에 들어 있다.

### 0.3 바꾸지 않으면

| 문제 | 그대로 두면 |
|---|---|
| ① 파일 | 발표를 한 대의 컴퓨터에서만 할 수 있다 |
| ② 재시작 | 데모 중 서버가 죽으면 분석이 사라진다 |
| ③ 인증 | 문서·코드 불일치를 계속 수동으로 맞춘다 |
| ④ 스키마 | 두 벌을 병행 관리한다 |
| ⑤ 팀원 결과물 | 16,000줄을 못 쓴다 |
| ⑥ 보관 | 계속 쌓인다 (당장 문제는 아님) |

### 0.4 바꾸는 비용

솔직하게 적는다. 3절에 근거가 있다.

| | |
|---|---|
| 다시 쓰는 코드 | 약 3,000줄 (SQL 134곳) |
| 버리는 코드 | 약 2,000줄 |
| **같이 죽는 테스트** | **약 8,000줄** ← 가장 아프다 |
| 살아남는 코드 | 약 7,000줄 (판정 로직 전부) |

**판정 두뇌 4,000줄은 DB 를 안 만져서 그대로 살아남는다.** "처음부터 다시"가 아니라 "머리는 두고 몸통을 갈아끼우는" 작업이다.

다만 죽는 테스트 8,000줄이 지금 CPL·FIT·SIM 판정 결과를 고정해 주는 유일한 장치다. **그래서 8절 0단계(골든셋 확보)가 순서상 가장 급하다.**

---

## 1. 목표 구조

```text
┌──────────┐
│ 프론트    │  React
└────┬─────┘
     │ Supabase JS SDK  (Auth · DB · Storage · Realtime)
┌────▼──────────────────────┐
│ Supabase                  │  DB · Storage · Auth · 큐
└────┬──────────────────────┘
     │ 워커가 큐에서 작업을 집는다 (pull)
┌────▼──────────────────────┐
│ 워커 (Python)              │  GPU 없음
│  ├ HWP 파싱                │
│  ├ CPL / FIT / SIM 판정    │
│  └ 필요할 때만 HTTP 호출   │
└────┬──────────────────────┘
     │ POST /v1/chat/completions
┌────▼──────────────────────┐
│ RunPod (GPU)              │
│  └ vLLM + gemma 12B       │
└───────────────────────────┘
```

**핵심 원칙 셋**

1. **워커는 서버가 아니다.** 요청을 받지 않고 큐에서 가져간다. 공개 주소가 필요 없다.
2. **프론트와 워커는 직접 만나지 않는다.** Supabase가 우편함이다.
3. **LLM은 워커 밖에 있다.** 워커는 HTTP로 물어보기만 한다.

## 2. 구성 요소별 책임

| | 맡는 일 | 안 맡는 일 |
|---|---|---|
| **프론트** | 로그인, 파일 업로드, 상태 구독, 결과 조회 | 분석 요청을 직접 처리 |
| **Supabase** | 인증, 파일 저장, 데이터, **큐** | 분석 실행 |
| **워커** | 파싱, Rule 판정, LLM 호출, 결과 기록 | HTTP 서버 노릇, GPU 연산 |
| **RunPod** | LLM 추론 | 우리 비즈니스 로직 |

---

## 3. 현행 코드를 어떻게 처리하나

앱 코드 **13,068줄** 기준 실측이다.

### 3.1 그대로 옮긴다 — 약 7,000줄

**DB도 HTTP도 안 만지는 순수 함수**라 워커 안으로 파일만 옮기면 된다.

| 파일 | 줄 | `sims.` 참조 |
|---|---|---|
| `services/cpl/logic_validator.py` | 2,481 | **0** |
| `services/fit/fit_engine.py` | 836 | **0** |
| `services/sim/sim_engine.py` | 729 | 6 (저장부만) |
| `parsers/hwp_parser.py` | 334 | 0 |
| `schemas/` | 910 | 0 |
| `infrastructure/openai_*`, `reportlab_pdf_renderer.py` | — | 0 |

**판정 로직이 스키마에 안 묶여 있다.** 입력으로 `ParsedDocument` 객체를 받고 `CplResult` 객체를 돌려준다. 어느 DB에서 왔는지 모른다.

### 3.2 버린다 — 약 2,000줄

| | 줄 | 대체 |
|---|---|---|
| `api/` 라우터 12개 | 1,087 | Supabase 직접 호출 |
| `services/auth.py` + `password_reset.py` | 537 | Supabase Auth |
| `core/security.py` (JWT 발급·검증) | 일부 | Supabase Auth |
| `infrastructure/local_object_storage.py` | 일부 | Supabase Storage |
| `infrastructure/smtp_mail_sender.py` | 일부 | Supabase Auth 메일 |
| `infrastructure/in_process_job_dispatcher.py` | 24 | 큐 dispatcher |

### 3.3 다시 쓴다 — 약 3,000줄

**SQL 134곳, 12개 파일.** 테이블 이름·스키마·PK 타입(`bigint`→`uuid`)이 전부 바뀌어 한 줄도 안 남는다.

```
document_parsing.py   24곳      reporting.py          18곳
retrieval.py          17곳      announcement_sync.py  15곳
cpl/checker.py        12곳      chat.py               11곳
corpus_embedding.py   11곳      analysis_pipeline.py   8곳
…
```

로직은 살고 **저장·조회 부분만** 갈아끼운다.

### 3.4 새로 만든다

| | 규모 |
|---|---|
| 큐 dispatcher (`JobDispatcher` 포트 구현) | 작음 |
| RunPod vLLM 클라이언트 (`LLMClient` 포트 구현) | 150줄쯤 |
| Supabase Storage 어댑터 (`ObjectStorage` 포트 구현) | 작음 |
| 워커 실행 루프 | 작음 |

**포트 세 개가 이미 그 경계에 뚫려 있다.** 인터페이스는 그대로 두고 구현만 바꾼다.

### 3.5 같이 죽는 것

테스트 **10,381줄 중 약 8,000줄**이 DB 스키마에 묶여 있다. 24개 파일 중 12개가 DB 픽스처를 쓴다.

```
sims.app_user          29곳
sims.inspection_case   28곳
sims.uploaded_document  9곳
…
```

**DB 무관 테스트 2,358줄은 살아남는다.** 그게 `logic_validator.py`(2,481줄)와 `fit_engine.py`(836줄)를 계속 지킨다.

---

## 4. 큐 설계

### 4.1 `workspace.analysis_run` 을 그대로 쓴다 — 확정

새 테이블을 만들지 않는다. 이미 큐 모양이다.

```sql
analysis_run_pk UUID PRIMARY KEY
user_id         UUID → auth.users(id)
status          TEXT CHECK IN ('queued','running','succeeded','failed','cancelled','cleanup_pending')
started_at / completed_at / expires_at / created_at / updated_at
```

인덱스도 이미 맞는다.

```sql
ix_workspace_analysis_orphan ON workspace.analysis_run(status, created_at)
```

`WHERE status='queued' ORDER BY created_at` 에 그대로 쓰인다.

### 4.2 워커 루프

**① 집기 — 동시성은 PostgreSQL이 보장한다**

```sql
UPDATE workspace.analysis_run
SET status = 'running', started_at = now()
WHERE analysis_run_pk = (
    SELECT analysis_run_pk FROM workspace.analysis_run
    WHERE status = 'queued'
    ORDER BY created_at
    FOR UPDATE SKIP LOCKED LIMIT 1
)
RETURNING analysis_run_pk, user_id;
```

`FOR UPDATE SKIP LOCKED` 덕분에 워커를 여러 개 띄워도 같은 작업을 두 번 집지 않는다.

**② 처리** — 파싱 → Rule 판정 → 필요 시 RunPod 호출 → 결과 조립

**③ 완료**

```sql
UPDATE workspace.analysis_run
SET status = 'succeeded', completed_at = now(), expires_at = now() + interval '1 day'
WHERE analysis_run_pk = :pk;
```

**④ 죽은 작업 되살리기 (주기 실행)**

```sql
UPDATE workspace.analysis_run
SET status = 'queued', started_at = NULL, attempt_count = attempt_count + 1
WHERE status = 'running'
  AND started_at < now() - interval '10 minutes'
  AND attempt_count < 3;
```

### 4.3 컬럼 두 개 추가 제안

현행에 실패 사유를 담을 자리가 없다. 팀 Supabase 실측에 이런 구분이 있었다.

```
ANALYSIS_INTERRUPTED   4건   서버 재시작으로 분석이 죽음
RETRIEVAL_NOT_READY    3건   유사공고 검색 준비 안 됨
```

`status='failed'` 만으로는 둘을 구분하지 못한다.

```sql
ALTER TABLE workspace.analysis_run
  ADD COLUMN failure_code  TEXT,
  ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0;
```

`attempt_count` 는 ④에서 무한 재시도를 막는다.

---

## 5. LLM 연결

### 5.1 vLLM — 확정

| | |
|---|---|
| 엔진 | **vLLM** |
| 모델 | gemma 12B |
| API | OpenAI 호환 `/v1/chat/completions` |

**vLLM을 고른 이유는 스키마 강제다.** CPL·FIT·SIM·챗봇이 전부 정해진 JSON 스키마를 받아야 돌아간다.

```python
class CplSemanticItem(BaseModel):
    field_code: CplFieldCode                                       # 13개 중 하나
    status: Literal["PRESENT", "MISSING", "NEEDS_CONFIRMATION"]    # 3개 중 하나
    occurrences: list[CplSemanticOccurrence]
```

vLLM의 `response_format` / `guided_json` 이 생성 단계에서 문법에 안 맞는 토큰을 차단하므로 스키마 위반이 구조적으로 불가능하다.

### 5.2 클라이언트 변경점

현행 `openai_llm_client.py`(179줄)에서 바뀌는 건 셋뿐이다.

| | OpenAI | vLLM |
|---|---|---|
| 경로 | `/responses` | `/v1/chat/completions` |
| 메시지 키 | `input` | `messages` |
| 스키마 키 | `text.format` | `response_format` |

`temperature: 0` 은 반드시 유지한다. 기본값(1.0)에서는 같은 문서의 CPL 확인 개수가 **6~9로 흔들렸다**(같은 파일 26회 실행 실측).

### 5.3 필요한 설정

```
RUNPOD_LLM_BASE_URL=https://…/v1
RUNPOD_LLM_MODEL=google/gemma-3-12b-it     # vllm serve 에 넘긴 값과 정확히 같아야 함
RUNPOD_LLM_API_KEY=…                        # Serverless 인 경우
```

`--max-model-len` 은 8192 이상 권장한다. 실측 추출 텍스트가 최대 4,040자이고 프롬프트에 후보 목록이 더 붙는다.

---

## 6. 데이터 흐름

### 6.1 업로드 → 분석

```text
① 프론트 → Supabase Storage       HWP/HWPX 업로드
② 프론트 → analysis_run INSERT     status='queued'
③ 프론트 ← analysis_run_pk         대기 화면 표시
④ 워커   ← 큐에서 집기             status='running'
⑤ 워커                             파싱 → Rule → (RunPod) → 결과
⑥ 워커   → result.* 기록           status='succeeded'
⑦ 프론트 ← Realtime 또는 폴링      결과 화면으로
```

### 6.2 워커 안에서 LLM을 부르는 조건

**13항목 전부를 LLM에 보내지 않는다.** Rule이 확정하지 못한 것만 묻는다.

```python
semantic_fields = {
    item.field_code for item in rule_result.items
    if item.field_code in CPL_SEMANTIC_FIELDS
    and item.status not in {PRESENT, NOT_APPLICABLE}
}
```

| Rule 전용 4개 | LLM 대상 9개 |
|---|---|
| `REQUEST_TYPE` `BUSINESS_PERIOD` `LEGAL_BASIS` `BUDGET` | `PURPOSE_GOAL` `IMPLEMENTATION_PLAN` `NEW_OR_CHANGED_CONTENT` `BUSINESS_NEED` `LINKED_POLICY` `TARGET_AND_CONDITIONS` `SUPPORT_CONTENT_AND_SCALE` `DELIVERY_SYSTEM` `EXPECTED_EFFECTS_AND_PERFORMANCE` |

실측으로 이 구분이 재현성과 정확히 일치했다 — 같은 파일을 반복 실행했을 때 **판정이 한 번도 안 갈린 항목이 Rule 전용 4개**였다.

### 6.3 파일 위치 — 반드시 옮겨야 한다

현행은 백엔드 로컬 디스크다.

```
backend/storage/users/{user_id}/cases/{case_id}/{uuid}.hwp
```

**워커가 다른 기계면 못 읽는다.** Supabase Storage로 옮기는 것이 이 구조의 전제다. `ObjectStorage` 포트가 이미 있어 구현만 갈아끼우면 된다.

덤으로 현행 문제도 함께 해결된다 — 지금은 팀 Supabase에 `file_asset` 행이 99개 있지만 파일은 각자 노트북에만 있어서, 다른 사람이 붙으면 PDF 다운로드가 `503` 이다.

---

## 7. 아직 안 정해진 것

| # | 결정 사항 | 영향 |
|---|---|---|
| 1 | **파서** — 팀원 Common IR 채택 vs 현행 유지 | 근거 단위가 조각→셀로 바뀐다. 중첩 표를 얻는다 |
| 2 | **의미 구조화** — 현행 CPL 13항목 vs RequestProfile 25항목 | 후자면 `logic_validator.py` 2,481줄과 `fit_engine.py` 836줄 재작업 |
| 3 | **임베딩 위치** — OpenAI 유지 vs RunPod | 옮기면 벡터 53개 재생성 (`embedding_model` 이 트리거로 수정 금지) |
| 4 | **챗봇** — 워커 vs Edge Function | 챗봇은 동기(몇 초)라 큐를 거치면 느려진다 |
| 5 | **결과 스키마** — `result.*` 8개 테이블 채택 범위 | `axis_result` 의 `CPL/FIT/BEN/DIF` 가 현행 `report_json` 과 1:1로 맞는다 |
| 6 | **회귀 기준** — 골든셋 확보 시점 | 스키마를 갈기 전에 뽑아야 한다 |

### 7.1 1·2번 판단에 필요한 측정

팀원 파서(`common_ir_pipeline`)를 실제 요청서로 돌려본 결과다.

| | 현행 파서 | Common IR |
|---|---|---|
| 블록 | 93 (문단 86 + 표 7) | 47 (문단 39 + 표 8) |
| 빈 문단 | 포함 | 152개 스킵 |
| **중첩 표** | **못 잡음** (탭 텍스트로 뭉갬) | **별도 블록 + `table_contains` 관계** |
| 스키마 검증 | 없음 | `validation_errors: 0` |
| 셀 텍스트 | — | **완전히 동일** |

핵심 내용은 손실이 없고, 중첩 표(10×14, 셀 52개)를 추가로 얻는다. 대신 근거 지목 단위가 세그먼트(조각)에서 셀로 올라간다.

다만 팀원 파이프라인의 **CandidatePack 단계**가 셀 안을 문단 단위로 다시 쪼개므로, 그 단계까지 함께 가면 이 손실은 사라진다.

### 7.2 3번 이후 결정을 좌우하는 측정

**gemma 12B 로 우리 실제 문서를 돌려 접지 실패 건수를 재야 한다.**

```
같은 HWP  →  gpt-4o-mini   →  LLM_INVALID_RESPONSE 54건, CPL 확인 6~9개
          →  gemma 12B     →  ?
```

- 비슷하면 → 현행 CPL 방식 유지
- 크게 늘면 → CandidatePack 방식(모델이 후보 ID만 고름)으로 전환

프롬프트도 판정 코드도 그대로 두고 `LLMClient` 구현만 바꿔 돌리면 되므로 비교가 깨끗하다.

---

## 8. 작업 순서 제안

다른 결정을 기다리지 않아도 되는 것부터 적었다.

| 단계 | 작업 | 선행 조건 |
|---|---|---|
| **0** | 골든셋 확보 — (입력 HWP, 그때 나온 `report_json`) 쌍 | 없음. **스키마 갈기 전에** |
| **1** | RunPod vLLM 클라이언트 (`LLMClient` 구현) | RunPod 주소·모델명 |
| **2** | gemma vs gpt-4o-mini 접지 실패 비교 | 1 |
| **3** | 워커 골격 — 큐 집기 루프 | 없음 |
| **4** | Supabase Storage 어댑터 (`ObjectStorage` 구현) | 버킷 생성 |
| **5** | 저장 층 재작성 — `sims.*` → `result.*`·`workspace.*` | 결정 5 |
| **6** | 프론트 전환 — Supabase 직접 호출 | 4·5 |

**0번이 가장 급하다.** 스키마를 바꾸면 DB에 묶인 테스트 8,000줄이 죽고, 그게 지금 CPL·FIT·SIM 판정 결과를 고정해 주는 유일한 장치다. 골든셋이 그 자리를 대신한다.

### 8.1 골든셋 형태

```text
samples/regression/
├─ INDEX.md            사람용 목차 · 커버리지
├─ baseline.json       항목별 안정/불안정 측정값
├─ compare.py          새 구현 결과 vs 기대값 비교기
└─ case_0001/
   ├─ input.hwp
   ├─ expected.json    report_json 그대로 (불변 스냅샷)
   └─ meta.json        sha256 · 실행시각 · 파서/룰셋 버전
```

보고서가 있는 47건 중 **입력 파일이 남아 있는 25건**을 뽑는다.

**비교는 세 단계로 한다.** 완전 일치로 하면 현행 코드끼리도 실패하기 때문이다.

| 단계 | 대상 | 실패 시 |
|---|---|---|
| 구조 (엄격) | 13항목 존재, `field_code` 집합, FIT 7관계, 상태값 허용 집합 | 즉시 중단 |
| 결정적 판정 (엄격) | Rule 전용 4항목, FIT 상태, SIM 후보 수 | Rule 로직이 틀어짐 |
| 의미 판정 (느슨) | LLM 9항목 — 허용 전이 안인지만 | 경고, 사람이 확인 |

같은 파일 26회 실행에서 CPL 확인 개수가 **6~9로 흔들린 것**이 이 설계의 근거다.

---

## 9. 위험

| # | 위험 | 근거 | 완화 |
|---|---|---|---|
| 1 | **gemma 12B 의 인용 정확도** | `gpt-4o-mini` 로도 접지 실패 54건. 스키마 강제는 JSON 모양만 보장하고 "원문 그대로 인용"은 못 막는다 | 8단계 2번 측정 → 나쁘면 CandidatePack 방식 |
| 2 | **테스트 8,000줄 소실** | DB 픽스처를 쓰는 파일 12개 | 골든셋으로 판정 품질만 지킨다. 저장 층 테스트는 새 스키마 안정 후 복구 |
| 3 | **워커 재시작 중 작업 유실** | 현행 `ANALYSIS_INTERRUPTED` 4건 | 4.2 ④ 되살리기 + `attempt_count` |
| 4 | **콜드 스타트** | Serverless vLLM 은 유휴 시 종료. 12B 로딩에 수십 초 | 발표 직전 한 번 호출해 깨워둔다 |
| 5 | **SIM 재료 부족** | 공고 6건의 `target`·`content` 가 전부 비어 SIM `content` 축이 230건 전부 `INSUFFICIENT` | 팀원 `ExistingProfile v0.2` 로 공고 구조화 |

---

## 10. 부록 — 참고 문서

| 문서 | 내용 |
|---|---|
| [DB 전체 컬럼 설명](Pre-review_DB_전체컬럼_설명_v1.0_260904.md) | 현행 37테이블 379컬럼 |
| [과거 분석실행 기록](Pre-review_과거_분석실행_기록_v1.0_260904.md) | 54건 전수 실측 |
| [파싱 현행동작과 출력](Pre-review_파싱_현행동작과_출력_v1.0_260904.md) | 파서 동작 · 출력 구조 · 기준선 수치 |
| [판정실패 원인분석](Pre-review_판정실패_원인분석_v1.0_260904.md) | LLM 실패 · CPL 강등 경로 |
| [프론트 API 계약](Pre-review_프론트_API_계약_v1.0_260904.md) | 현행 API (엔드포인트 정리 v1.0 260903 은 경로 3개가 낡음) |
