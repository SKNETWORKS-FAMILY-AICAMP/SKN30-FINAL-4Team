# ml/serving/ — 백엔드 전달용 추론 패키지

`ml/models/`·`ml/pipelines/`·`ml/data/`의 canonical 파일은 원본 위치 그대로
둔다. 여기 있는 것은 **백엔드가 추론 한 번을 돌리는 데 필요한 최소 파일**
(artifact 사본 + 얇은 wrapper)이고, 실제 feature 생성·전처리·추론 로직은
전부 원본 `ml/pipelines/` 모듈을 그대로 호출한다 — 재구현하지 않는다.

```text
ml/serving/
├─ model1/
│  ├─ model/            KLUE-BERT weight (dl20_m1_final_export.py 로 학습)
│  ├─ tokenizer/
│  ├─ label_mapping.json
│  ├─ train.parquet             학습에 쓴 원본 데이터 1,404건 (ml/models/model1_canonical/ 사본)
│  ├─ external.parquet          외부 검증 131건 〃
│  └─ inference.py       dl07_m1_apply.clean_text()/tier() 를 감싸는 wrapper
├─ model2/                        ★ M82/P3 세대 (2026-09-04 전환)
│  ├─ predict.py               진입점 — predict()(회귀) · percentile()(비교군 위치)
│  ├─ preprocessing.py         F06 스키마 입력 정규화 + 필수값 검증
│  ├─ masking.py               [AMOUNT]/# 마스킹 (학습과 같은 SF.mask_text)
│  ├─ proximity.py             proximity 추출 (학습과 같은 M82 정규식·window)
│  ├─ feature_builder.py       211열 조립 + 열 이름·순서 대조
│  ├─ router.py                ordinal soft routing (global 1 + expert 3 + 이진 2)
│  ├─ build_bundle.py          번들 재생성 (전체 1,877행 재적합 + 자체검증 6종)
│  ├─ cohort_reference.parquet 비교군 percentile 참조표 (M65 세대와 동일 — 안 바뀜)
│  ├─ inference.py             [구세대 M65] 전환 완료 후 삭제 가능
│  └─ model2_canonical.joblib  [구세대 M65] 〃
│     번들은 ml/models/model2_canonical/model2_p3_bundle.joblib (20.8 MB) 를 참조한다
├─ model3/
│  ├─ design_features_v3.parquet   ml/data/processed/ 사본
│  └─ inference.py                 m3_lab.score_pool() 을 감싸는 wrapper
├─ requirements.txt
└─ README.md   (이 파일)
```

**model1 의 `train.parquet`/`external.parquet` 는 추론에 필요하지 않다** —
weight 안에 이미 학습이 끝나 있다. 재현·감사 목적으로만 같이 둔다(어떤
데이터로 이 weight 가 나왔는지 원본을 남겨야 한다는 요청에 따라 포함).
model2 의 `cohort_reference.parquet` 는 반대로 **추론에 실제로 쓰인다** —
1차 산출물인 percentile 조회가 이 표를 찾아본다(2절 참조).

## 중요: 이 패키지는 완전히 독립적이지 않다

`inference.py` 는 실행 시 `ml/pipelines/`·`ml/evaluation/`·`ml/experiments/`
를 `sys.path` 에 얹고 원본 모듈(`m56_m2_canonical`, `m45_m2_amount`,
`m3_lab`, `m13_m3_anomaly`, `dl07_m1_apply` 등)을 그대로 `import` 한다.
**배포 시 이 저장소의
`ml/pipelines/` 코드가 같이 있어야 한다** — 백엔드가 그 코드를 이해할
필요는 없지만, 파일은 옆에 있어야 한다. 완전히 코드까지 분리하려면 원본
모듈을 재구현해야 하는데, 그것은 지시서가 금지한다("feature engineering
재작성 금지").

또한 `m56_m2_canonical.py`·`m3_lab.py` 가 import 하는 `common.py` 는
import 시점에 `ml/data`·`ml/reports`·`ml/figures`·`ml/models` 디렉터리를
`os.makedirs(exist_ok=True)` 로 만든다 — `ml/` 트리가 존재해야 import 가
깨끗하게 끝난다(디렉터리가 없으면 새로 만들 뿐이라 에러는 나지 않는다).

---

## 주의: 모듈 이름이 겹친다 (model1/predict.py · model2/predict.py)

가이드 구조상 두 모델의 진입점 파일명이 같다. 두 모델을 **한 프로세스에서**
쓰면 `sys.modules` 캐시 때문에 먼저 import 한 쪽이 계속 돌아온다 — 에러 없이
조용히 틀린 모델이 불린다(이 저장소의 API 스모크가 실제로 이 함정에 걸렸다).

한 모델만 쓰면 문제없다. 둘 이상을 쓸 때는 **파일 경로로 고유 이름을 붙여**
불러온다.

```python
import importlib.util, os, sys

def load(sub, modname):
    d = os.path.join("ml/serving", sub)
    sys.path.insert(0, d)                      # 형제 모듈(feature_builder 등)
    spec = importlib.util.spec_from_file_location(
        "%s_%s" % (sub, modname), os.path.join(d, modname + ".py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod

m2 = load("model2", "predict")     # 지원규모
m3 = load("model3", "score")       # 설계 이례성
m1 = load("model1", "predict")     # 지원유형
```

동작 확인은 `python ml/serving/smoke_api.py` — README 에 적힌 호출 경로
그대로 세 모델을 한 프로세스에서 부른다.

---

## 1. 모델 1 — 지원성격 분류 (KLUE-BERT)

**weight 확보 경위** — 저장소·이 컴퓨터 어디에도 KLUE-BERT weight가 없어
처음에는 `BLOCKED: weight missing` 이었다(huggingface 캐시·Downloads까지
확인). 이후 RunPod GPU 박스(RTX 4090)를 붙여, **동일 최종 설정으로**
`ml/pipelines/model1/dl20_m1_final_export.py` 를 실행해 재학습했다. 새 설정을
만들지 않았다 — `klue/bert-base` · lr `5e-5`(`dl12_m1_candidates.py` 내부
CV 가 이미 고른 값, `ml/reports/model1/core/dl12_m1_candidates_dl.json`) · `epochs=8`
`batch=16` `max_len=256` `class_weight=True`(dl12.FIXED 그대로) ·
`seed=42`(내부 CV·저장소 전역 기본 seed 와 동일). 학습 로그·설정·외부
131건 참고 수치는 `ml/reports/model1/core/dl20_m1_final_export.json` 에 있다.

**artifact 위치**: 원본 학습 산출물은 RunPod `/workspace/dl/models/
m1_klue_bert/`(로컬 저장소 밖 — 400MB+ 라 gitignore 대상인 것은 그대로),
서빙용 사본은 `ml/serving/model1/`.

### 입력 필드

`predict(texts, already_cleaned=False)` — `texts` 는 원문 문자열의
list(공고문 전체 텍스트 또는 요약문). 기본으로 `dl07_m1_apply.py` 의
`clean_text()`(안내문 상투구 제거 + 900자 예산)를 그대로 적용한다. 이미
정제된 텍스트라면 `already_cleaned=True`.

### 출력

| 필드 | 의미 |
|---|---|
| `support_type_pred` | 19클래스 중 하나 |
| `confidence` | softmax 최고확률 |
| `status` | `dl07_m1_apply.tier()` 그대로 — 판단보류(<0.20) / 참고용(0.20~0.35) / 신뢰(>=0.35). 이 임계값은 M09 가 정했고 "모델이 바뀌어도 같은 기준"이라 여기서도 그대로 쓴다 |

### 출력 예시 (스모크 테스트 실측)

```python
[{'support_type_pred': '판로', 'confidence': 0.9941691160202026, 'status': '신뢰'}]
```

### 실행 예시

```python
import sys
sys.path.insert(0, "ml/serving/model1")
from inference import predict

texts = ["2026년 중소기업 판로 지원사업 공고. 사업 개요: 국내외 판로 개척을 위한 "
         "전시회 참가비와 온라인 입점 비용을 지원한다."]
print(predict(texts))
```

```bash
python ml/serving/model1/inference.py
```

### dependency

`torch`, `transformers` — `ml/serving/requirements.txt`. GPU 없이도
동작한다(CPU 로 스모크 테스트 완료). 학습(재학습)에는 GPU 가 필요하지만
**추론에는 필요 없다** — `inference.py` 는 `torch.cuda.is_available()` 로
있으면 쓰고 없으면 CPU 로 돈다.

### smoke test

`python ml/serving/model1/inference.py` 를 로컬(CPU, GPU 없음)에서 실행 →
위 출력 예시와 동일하게 정상 반환 확인(2026-09-01). 학습 자체는 RunPod
RTX 4090 에서 8 epoch·64초·loss 2.72→0.01, 외부 131건(참고, 단일
seed=42) accuracy **0.8321** — 같은 seed 로 이미 돌렸던
`dl12_m1_candidates_dl.json` 의 KLUE-BERT seed=42 결과(0.8321)와 정확히
일치해 재현성을 확인했다. dl12 공표치(3seed 평균 0.8422 ± 0.0072)와는
단일 seed 라 자연히 다르다.

## GPU가 필요한가

**학습(재학습)에는 필요하다, 추론에는 필요 없다.**
`dl12_m1_candidates.py`·`dl07_m1_apply.py`·`dl20_m1_final_export.py` 모두
모델과 배치 텐서를 `.cuda()` 로 무조건 옮긴다 — CPU 폴백 코드가 없다.
KLUE-BERT 파인튜닝을 CPU로 돌리면 비현실적으로 오래 걸리고, 코드를
고치지 않는 한(지시서가 금지하는 "모델 로직 재구현"에 해당할 수 있다)
애초에 실행되지 않는다 — 그래서 이번에는 RunPod GPU 박스를 붙여 학습
했다. 반면 `ml/serving/model1/inference.py` 의 **추론**은 CPU 로도
동작한다(위 smoke test). 모델 2·3은 애초에 XGBoost / 거리 기반이라
학습·추론 모두 GPU 가 필요 없다.

---

## 2. 모델 2 — 지원규모 (M82/P3, 2026-09-04 전환)

**세대 전환**: 회귀가 M65 단일 XGBoost → **M82/P3** 로 바뀌었다.

```text
M65 0.4117 -> M69 0.3719 -> M73 0.3563 -> M82/P3 0.3518   (OOF MAE, log10)
```

| | 구세대 (M65) | **현행 (M82/P3)** |
|---|---|---|
| 모듈 | `inference.py` | **`predict.py`** |
| 번들 | `serving/model2/model2_canonical.joblib` | **`models/model2_canonical/model2_p3_bundle.joblib`** |
| 구조 | 단일 XGB + CQR 구간 | global 1 + 구간 expert 3 + ordinal 이진 2 = **6개** |
| feature | 구조화 + 제목 SVD | **211열** (+ 원천층 · 본문 SVD64 · proximity 24 + SVD16) |
| OOF MAE | 0.4117 | **0.3518** (strict 0.3751, 5/5 fold, CI [-0.0083, -0.0003]) |

`inference.py` 와 구세대 joblib 은 **아직 지우지 않았다** — 백엔드 전환이
끝나면 지운다. 새 코드는 `predict.py` 만 부르면 된다.

**artifact 위치**: `ml/models/model2_canonical/` (번들 + manifest). 20.8 MB 라
`serving/` 에 사본을 두지 않고 참조한다 — 이 패키지는 어차피 `ml/pipelines/`
가 옆에 있어야 동작하므로(위 "완전히 독립적이지 않다" 절) 사본이 이득이 없다.

`percentile()` 은 **바뀌지 않았다** — M82 는 회귀만 바꿨고 비교군 사다리
(`m45_m2_amount.build_reference`)는 그대로다. 반환값도 동일하다.

### 입력 필드 — `predict()`

구세대와 달리 **공고문 원문(`evidence_text`)이 필수**다. M82 의 이득이 본문
안의 "수치와 그 주변 의미"(지원비율 70% · 30개사 선정 · 12개월)에서 나오기
때문이다. 없으면 `ValueError` 로 즉시 막는다 — 조용히 나빠지는 것보다 낫다.

| 필드 | 필수 | 비고 |
|---|---|---|
| `title` | **예** | 사업 제목 |
| `evidence_text` | **예** | 공고문 원문(또는 API 요약문). proximity·본문 SVD 입력 |
| `evidence_source` | 권장 | `document` / `scale_text` / `api_summary` |
| `support_type` | 권장 | 모델 1 출력 19클래스 한글. 비면 결측 처리 |
| `support_method` `support_unit` `amount_type` `cohort` `category_large` `agency_type` | 권장 | 도메인표(아래)의 **영문 코드값** |
| `industry` `support_count` `support_ratio` `self_burden_ratio` `project_duration` `year` | 선택 | 없으면 결측 |
| `row_id` | 선택 | 결과 매칭용 |

없는 필드는 결측으로 채워진다(`preprocessing.to_frame` 이 학습 스키마에
맞추고, 범주는 학습 레벨 밖이면 NaN, 정수형에 결측이 있으면 float 로 둔다 —
XGBoost 가 NaN 을 분기로 처리한다).

### 범주형 필드 값 도메인 (원본 데이터의 코드값 — 한글 라벨이 아니다)

| 필드 | 값 |
|---|---|
| `support_method` | `grant`(보조금) `guarantee`(보증) `loan`(융자) `mixed`(혼합) `other` `service`(현물) `voucher`(바우처) |
| `support_unit` | `company`(기업당) `person`(인당) `project`(과제당) `team`(팀당) |
| `amount_type` | `per_company` `per_project` `periodic` `total_budget` `unknown` |
| `agency_type` | `central`(중앙부처) `local`(지자체) `public`(공공기관) |
| `cohort` | `bizinfo` `taxonomy` (출처) |
| `support_type` | 모델 1 출력 19클래스 한글 그대로 (`사업화`·`판로`·`융자` 등) |
| `category_large` | `경영`·`금융`·`기술`·`기타`·`내수`·`소상`·`수출`·`인력`·`창업` |

### 출력 — `predict()`

```json
{"model": "model2_p3", "n": 1, "context_digit_residue": 0,
 "predictions": [{"row_id": "...", "pred_log10": 7.665, "pred_won": 46233528,
   "bucket": "Mid", "bucket_proba": {"Low": 0.075, "Mid": 0.8667, "High": 0.0583},
   "bucket_edges_won": [20000000, 120000000],
   "proximity": {"prox_support_rate": 70.0, "prox_self_burden_rate": 30.0,
                 "prox_selected_count": null, "prox_duration_months": 12.0}}]}
```

| 필드 | 의미 |
|---|---|
| `pred_log10` / `pred_won` | log10(원) 점추정 · 원 단위 환산 |
| `bucket` / `bucket_proba` | 금액 구간(Low/Mid/High)과 그 확률. soft 라우팅의 가중치 자체다 |
| `bucket_edges_won` | 구간 경계(원) — 학습 y 의 P33.3/P66.7 |
| `proximity` | 본문에서 실제로 뽑힌 수치. **예측 근거를 사람이 확인할 수 있게** 같이 준다 |
| `context_digit_residue` | 마스킹 감사 — 0 이 아니면 target 누수 위험. 매 호출 확인한다 |

구세대의 `lo_won`/`hi_won`(CQR 구간)은 **없다** — M73/M82 구조는 구간회귀를
쓰지 않는다. 불확실성이 필요하면 `bucket_proba` 와 `percentile()` 을 쓴다.

### 출력 — `percentile()` (1차 산출물, 변경 없음)

| 필드 | 의미 |
|---|---|
| `status` | `비교가능` / `비교군_부족` / `비교불가`(사유는 `reason`) |
| `level` | 매칭된 비교군 사다리 단계 |
| `n` | 비교군 표본수 |
| `distribution` | p1~p99 분위값(원) |
| `percentile_rank` | 이 금액이 비교군의 몇 % 이하인가 |
| `spread_x` | p90/p10 배율 |
| `statement` | 사람에게 보여줄 문장 |
| `interval_tier` | 이 패키지에서는 계산하지 않음 — 항상 `None` |

### 실행 예시

```python
import sys
sys.path.insert(0, "ml/serving/model2")
from predict import predict, percentile

records = [{
    "row_id": "REQ001",
    "title": "2023년 수출유망상품화 사업",
    "evidence_text": ("□ 지원내용 : 정부지원 비율 70% (자부담 30%)
"
                      "□ 지원규모 : 기업당 최대 2억원
"
                      "□ 사업기간 : 12개월
□ 지원방식 : 보조금"),
    "evidence_source": "document",
    "support_type": "사업화", "support_method": "grant", "support_unit": "company",
    "cohort": "taxonomy", "category_large": "수출", "industry": "제조업",
    "agency_type": "public", "amount_type": "per_company",
    "support_count": 50, "support_ratio": 70, "self_burden_ratio": 30,
    "project_duration": 12, "year": 2023,
}]
print(predict(records))
print(percentile(200_000_000, "사업화", "grant", "company", "taxonomy"))
```

```bash
python ml/serving/model2/predict.py     # 번들 메타(어떤 실험의 어떤 성능인지) 출력
```

### 번들 재생성

```bash
python ml/serving/model2/build_bundle.py
```

전체 1,877행으로 변환기·모델을 다시 적합하고 자체검증 6종(211열 · transform
순서 일치 · 마스킹 잔존 0 · 합성 신규문서 예측 도달 · 예측이 학습 타깃
1~99 분위 안)을 통과해야만 저장한다.

### dependency

`pandas`, `numpy`, `scikit-learn`, `xgboost`, `joblib` — 구세대와 같다.
`ml/serving/requirements.txt`.

### smoke test (API 기준, 2026-09-04)

문서에 적힌 호출 경로 그대로 실행 — `sys.path.insert("ml/serving/model2")` →
`from predict import predict, percentile`.

```text
번들 메타     model2_p3 | M82/P3 | primary MAE 0.3518 | 211열
predict()     pred_log10 7.665 · pred_won 46,233,528 · bucket Mid(0.867)
              proximity 70% / 30% / 12개월  ·  context_digit_residue 0
percentile()  비교가능 · 성격x방식x단위x출처 · n=316 · 62.5% · spread 16.7배
```

`percentile()` 값이 구세대 스모크(2026-09-01)와 **정확히 같다** — 비교군
사다리를 바꾸지 않았다는 것이 실측으로 확인된다.

---

## 3. 모델 3 — 사업 설계 이상패턴 탐지 (비교군 대비 거리 · Freeze)

**artifact 위치**: 원본 `ml/data/processed/design_features_v3.parquet`
(M66 공급보강본, pool 2,626건), 사본 `ml/serving/model3/`. 저장된 학습
weight가 없는 모델이다 — 매 요청마다 이 비교군 pool 전체를 다시 스캔해서
거리를 계산한다(`ml/models/README.md` 참조).

### 입력 필드

값 도메인(`support_method`/`support_unit`/`amount_type`)은 모델 2 절의
도메인표와 같다 — 한글 라벨이 아니라 원본 데이터의 영문 코드값이다.

| 필드 | 필수 | 비고 |
|---|---|---|
| `support_type` | **예 — 없으면 그 행은 채점되지 않는다** | 비교군 사다리 1단계 키. 모델 1 출력 19클래스 한글 그대로 |
| `support_method` | 권장 | 비교군 사다리 1단계 키. `grant`/`guarantee`/`loan`/`mixed`/`other`/`service`/`voucher` |
| `support_unit` | 선택 | `company`/`person`/`project`/`team` |
| `amount_type` | 선택 | `per_company`/`per_project`/`periodic`/`total_budget`/`unknown` |
| `per_recipient` | 수치축(기업당 지원액) | 없으면 그 축만 결측 처리 |
| `support_count` | 수치축(지원건수) | 〃 |
| `project_duration` | 수치축(사업기간) | 〃 |
| `support_ratio` | 수치축(지원비율) | 〃 |
| `row_id` | 선택 (없으면 자동 생성) | 결과 매칭용 |
| `amount_outlier` | 선택 (기본 False) | True 면 `per_recipient` 를 결측 처리 |

수치축 4개 중 **2개 이상**이 채워져야 원래 채점 대상이 되는 규칙
(`MIN_AXES=2`)은 **비교군 pool 쪽에만** 적용된다 — 새로 들어오는 요청
행 자체는 이 필터를 받지 않고 그대로 채점된다.

### 출력

| 컬럼 | 의미 |
|---|---|
| `row_id` | 입력 행 식별자 |
| `score` | 비교군 내부 거리분포에서의 백분위(0~1, 클수록 이례적) |
| `level` | 채점에 실제로 쓰인 비교군 단계 (`L1 support_type x support_method` / `L2 support_type` / `L0 전체`) — 표본이 `MIN_COHORT=20` 미만이면 상위 단계로 물러난다 |
| `cohort_key` | 그 단계에서의 실제 비교군 키 값 |
| `cohort_n` | 비교군 표본수 |
| `top1_axis` | 가장 크게 벗어난 수치축 (설명용) |

**서비스 문구 규율**: "이례적"이지 "잘못됐다"가 아니다. 출력을 사람에게
보여줄 때는 `m13_m3_anomaly.ALLOWED` 목록(`과거 사업 패턴과 차이가 큼` /
`희귀한 설계 조합` / `동일 유형 대비 비전형적` / `확인 필요`)만 쓰고,
`부적절함`·`지원규모 과다` 같은 판정형 표현은 쓰지 않는다.

### 출력 예시 (스모크 테스트 실측)

```text
    row_id  score                        level    cohort_key  cohort_n         top1_axis
0  DEMO001    1.0  L1 support_type x support_method  사업화|grant       748  project_duration
```

### 실행 예시

```python
import sys
sys.path.insert(0, "ml/serving/model3")
from inference import predict

records = [{
    "row_id": "REQ001",
    "support_type": "사업화", "support_method": "grant", "support_unit": "company",
    "amount_type": "per_company", "per_recipient": 500_000_000,
    "support_count": 3, "project_duration": 12, "support_ratio": 70,
}]
print(predict(records))
```

```bash
python ml/serving/model3/inference.py
```

### dependency

`pandas`, `numpy`, `scikit-learn`(m3_lab 이 KMeans·metrics 를 import),
`scipy`(spearmanr) — `ml/serving/requirements.txt`. 첫 호출 시
`design_features_v3.parquet`(약 10MB)을 읽어 메모리에 캐시한다.

### smoke test

`python ml/serving/model3/inference.py` 실행 → 위 출력 예시와 동일하게
1행 DataFrame + 허용 문구 목록 정상 반환 확인(2026-09-01). 처음엔
`support_method`에 한글 값("보조금")을 써서 그 조합이 pool에 없으니
`L1`을 못 찾고 `L2 support_type`로 자동 후퇴하는 것까지만 확인했었다 —
도메인표의 영문 코드값(`grant`)으로 바꾸자 의도한 `L1
support_type x support_method`(비교군 748건)에서 정상 채점됐다.
