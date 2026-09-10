# Pre-review 챗봇 Loader 계약 v1.0

챗봇 코어(`chatmessage/`)와 저장소 사이의 경계 문서다. **백엔드가 Model 1·2·3
저장 구조를 만들 때 이 계약만 맞추면 챗봇 코어는 손대지 않는다.**

```text
DB / Worker 결과
  ↓
Backend Loader / Adapter        ← 백엔드가 만든다
  ↓
chatmessage 공통 입력 dict      ← 이 문서가 정하는 것
  ↓
build_chat_context()
```

`chatmessage` 는 DB 를 모른다. 테이블 이름도, 스키마도, 커넥션도 모른다.
dict 하나를 받을 뿐이다.

---

## 1. Loader 인터페이스

```python
# chatmessage/loader.py
class AnalysisResultLoader(Protocol):
    def load(self) -> dict[str, Any]: ...
```

지금 있는 구현은 둘뿐이다.

| 구현 | 용도 |
|---|---|
| `JsonFileLoader(path)` | fixture 와 실제 분석 결과 JSON. `run_chat.py` 가 쓴다 |
| `DictLoader(payload)` | 이미 dict 로 들고 있을 때. 테스트·호출부 |

DB 를 읽는 Loader 는 **백엔드 쪽에 만든다.** 이 패키지 안에 두면 의존 방향이
거꾸로 선다.

입력에 무엇이 실렸는지 먼저 보려면:

```python
from chatmessage.loader import describe
describe(payload)
# {'cpl': '있음', 'fit': '있음', 'retrieval/sim': '후보 3건',
#  'model1': 'available', 'model2': 'available', 'model3': 'not_available', ...}
```

---

## 2. 입력 dict — 챗봇이 읽는 키

없는 키는 그 섹션이 `not_available` 로 내려앉을 뿐 **오류가 아니다.**

| 키 | 섹션 | 내용 |
|---|---|---|
| `case` | — | `{case_id, title, completed_at}` |
| `self_check` | `cpl` | `{confirmed_count, total_count, items[]}` |
| `structural_consistency` | `fit` | `{module_status, score, relations[]}` |
| `similar_candidates` | `retrieval` `sim` | `[{rank, title, source_url, axes{}}]` |
| `models.model_1` | `model1` | ML envelope |
| `models.model_2` | `model2` | ML envelope |
| `dif` | `model3` | ML envelope |
| `module_summary` | `summary` | 집계 |
| `review_issues` | `summary` | 종합 이슈 목록 |
| `quality`, `warnings` | `summary` | — |

근거는 각 항목 안의 `occurrences` / `left_evidence` / `right_evidence` /
`request_evidence` / `candidate_evidence` 에 담긴다. 각 근거는
`{evidence_ref, source_side, source_id, excerpt, page_no, section_path, …}` 다.
챗봇이 이것을 한 곳(`evidence[]`)에 모으고 각 섹션은 `evidence_ids` 로 가리키게
바꾼다 — 같은 문장이 여러 섹션에 중복돼 프롬프트에 실리지 않게 하려는 것이다.

---

## 3. ML envelope

```json
{
  "status": "available",
  "result": { "...공개 가능한 값만..." },
  "metadata": { "support_type_compatibility": "known" },
  "error": null
}
```

### status

Loader 가 어떤 이름으로 줄지 몰라 입구에서 흡수한다.

| Loader 가 주는 값 | 챗봇이 읽는 값 | 사용자에게 안내되는 뜻 |
|---|---|---|
| `available` `success` `ok` | `success` | 결과 있음 |
| `insufficient_data` `insufficient_evidence` | `insufficient_data` | 실행은 됐지만 근거가 모자람 |
| 그 밖 · 키 없음 | `not_available` | 실행하지 못함 |

**`not_available` 과 `insufficient_data` 를 뭉개지 않는다.** 챗봇이 두 경우를
다른 문구로 안내한다.

### 공개 가능한 값

```text
Model 1  status · support_type (또는 공개용 분류 결과)
Model 2  status · predicted_amount
Model 3  status · 공개용 이례성 결과·설명 (description, anomaly_level 등)
```

### Chat 에 넣지 않는 값

```text
percentile
confidence
probability
raw_score
semantic_similarity(_display)
```

Loader 가 실수로 넣어도 `build_chat_context()` 가 **Context 생성 단계에서
지운다**(키 이름에 위 단어가 들어가면 중첩 구조까지 따라가며 제거). 프롬프트로만
막으면 값은 이미 프롬프트에 실려 있고, 모델이 규칙을 어기는 순간 그대로 나간다.

`rank` 는 점수가 아니라 순서라 공개한다.

---

## 4. ML 저장 구조가 아직 없을 때

**그냥 넣지 않으면 된다.** 빈 envelope 을 만들 필요도 없다.

```python
payload.pop("models", None)
payload.pop("dif", None)
```

챗봇은 세 모델을 `not_available` 로 답하고 나머지(CPL·FIT·유사공고)는 그대로
답한다. `chatmessage/fixtures/sample_cpl_fit_sim.json` 이 그 상태다.

---

## 5. 백엔드에 필요한 것 — 한 문장

> Model 1·2·3 의 **사용자 공개 결과**를 분석 완료 시 영속 저장하고, Chat Loader
> 가 조회할 수 있는 계약을 제공한다. `percentile`·`confidence`·`probability`·
> `raw_score` 는 저장하되 Chat 조회 계약에는 포함하지 않는다.

---

## 6. 저장 구조가 생긴 뒤 남는 챗봇 작업

코어를 다시 뜯는 것이 아니라 연결과 통합 검증만 남는다.

```text
1. 실제 ML Loader 연결
2. 실제 Case Context 확인
3. FastAPI Chat 연결
4. conversation_message 저장 확인
5. references 저장 확인
6. suggested_revision 저장 확인
7. 재조회 / 새로고침 확인
8. 실제 E2E 시연
```

---

## 7. 계약이 지켜지는지 확인하는 법

```bash
# 라우팅·비노출 — LLM 없이 항상 돈다
python -m pytest chatmessage/

# 실제 LLM 으로 질문 세트 전체
python chatmessage/scripts/run_regression.py

# 질문 하나를 눈으로
python chatmessage/scripts/run_chat.py \
  --input-json path/to/report.json \
  --message "확인이 필요한 부분을 알려줘" --show-context
```

새 Loader 를 만들면 그 출력을 `--input-json` 에 넣어 위 세 가지가 그대로
도는지 보면 된다. 챗봇 코어를 고칠 일은 없어야 한다.
