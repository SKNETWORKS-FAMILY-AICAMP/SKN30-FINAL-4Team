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
│  └─ inference.py       dl07_m1_apply.clean_text()/tier() 를 감싸는 wrapper
├─ model2/
│  ├─ model2_canonical.joblib   ml/models/m65_model2_canonical/ 사본
│  ├─ manifest.json             〃
│  └─ inference.py              m56_m2_canonical.serve() 를 감싸는 wrapper
├─ model3/
│  ├─ design_features_v3.parquet   ml/data/processed/ 사본
│  └─ inference.py                 m3_lab.score_pool() 을 감싸는 wrapper
├─ requirements.txt
└─ README.md   (이 파일)
```

## 중요: 이 패키지는 완전히 독립적이지 않다

`inference.py` 는 실행 시 `ml/pipelines/`·`ml/evaluation/`·`ml/experiments/`
를 `sys.path` 에 얹고 원본 모듈(`m56_m2_canonical`, `m3_lab`,
`m13_m4_anomaly` 등)을 그대로 `import` 한다. **배포 시 이 저장소의
`ml/pipelines/` 코드가 같이 있어야 한다** — 백엔드가 그 코드를 이해할
필요는 없지만, 파일은 옆에 있어야 한다. 완전히 코드까지 분리하려면 원본
모듈을 재구현해야 하는데, 그것은 지시서가 금지한다("feature engineering
재작성 금지").

또한 `m56_m2_canonical.py`·`m3_lab.py` 가 import 하는 `common.py` 는
import 시점에 `ml/data`·`ml/reports`·`ml/figures`·`ml/models` 디렉터리를
`os.makedirs(exist_ok=True)` 로 만든다 — `ml/` 트리가 존재해야 import 가
깨끗하게 끝난다(디렉터리가 없으면 새로 만들 뿐이라 에러는 나지 않는다).

---

## 1. 모델 1 — 지원성격 분류 (KLUE-BERT)

**weight 확보 경위** — 저장소·이 컴퓨터 어디에도 KLUE-BERT weight가 없어
처음에는 `BLOCKED: weight missing` 이었다(huggingface 캐시·Downloads까지
확인). 이후 RunPod GPU 박스(RTX 4090)를 붙여, **동일 최종 설정으로**
`ml/pipelines/dl20_m1_final_export.py` 를 실행해 재학습했다. 새 설정을
만들지 않았다 — `klue/bert-base` · lr `5e-5`(`dl12_m1_candidates.py` 내부
CV 가 이미 고른 값, `ml/reports/dl12_m1_candidates_dl.json`) · `epochs=8`
`batch=16` `max_len=256` `class_weight=True`(dl12.FIXED 그대로) ·
`seed=42`(내부 CV·저장소 전역 기본 seed 와 동일). 학습 로그·설정·외부
131건 참고 수치는 `ml/reports/dl20_m1_final_export.json` 에 있다.

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

## 2. 모델 2 — 지원규모 상대비교 (비교군 percentile + XGBoost)

**artifact 위치**: 원본 `ml/models/m65_model2_canonical/` (M65 canonical),
사본 `ml/serving/model2/`

### 입력 필드 (`m56_m2_canonical.SERVING_FIELDS`)

| 필드 | 비고 |
|---|---|
| `title` | 사업 제목 (TF-IDF+SVD 로 벡터화됨) |
| `support_type` | 지원성격 (모델 1 출력) |
| `support_method` | 지원방식 (보조금/융자 등) |
| `support_unit` | 지원단위 (기업당/과제당 등) |
| `cohort` | 비교군 구분 |
| `category_large` | 대분류 |
| `industry` | 업종 |
| `agency_type` | 기관계열 |
| `amount_type` | 금액 의미 (per_company/per_project 등) |
| `support_count` | 지원건수 |
| `support_ratio` | 지원비율 |
| `self_burden_ratio` | 자기부담비율 |
| `project_duration` | 사업기간 |
| `year` | 연도 |

없는 필드는 `NaN`/`"미기재"`로 자동 대체된다(`build_serving_frame`).

### 출력

| 컬럼 | 의미 |
|---|---|
| `pred_log10` / `lo_log10` / `hi_log10` | log10(원) 점추정 · 구간(CQR 보정) |
| `pred_won` / `lo_won` / `hi_won` | 원 단위 환산값 |

**주의**: 이것은 회귀 참고값이다. 모델 2 의 1차 산출물인 "비교군
percentile 위치"(`M45.compare`, 예: "상위 38%")는 전체 비교군 참조
테이블(`design_features` 계열 전체)이 있어야 계산되며, 이 최소 패키지
에는 포함하지 않았다 — 필요하면 `ml/pipelines/m45_m2_amount.py` 의
`build_reference()`/`compare()` 를 같은 방식으로 감싸야 한다(추가 데이터
필요, 원본 코드 재사용 원칙은 동일).

### 출력 예시 (스모크 테스트 실측)

```text
   pred_log10  lo_log10  hi_log10    pred_won      lo_won       hi_won
0    7.767004  7.055977   8.57406  58479612.0  11375667.0  375025184.0
```

### 실행 예시

```python
import sys
sys.path.insert(0, "ml/serving/model2")
from inference import predict

records = [{
    "title": "2023년 수출유망상품화 사업",
    "support_type": "판로", "support_method": "보조금", "support_unit": "기업당",
    "cohort": "중소기업", "category_large": "수출", "industry": "제조업",
    "agency_type": "공공기관", "amount_type": "per_company",
    "support_count": 50, "support_ratio": 70, "self_burden_ratio": 30,
    "project_duration": 12, "year": 2023,
}]
print(predict(records))
```

```bash
python ml/serving/model2/inference.py
```

### dependency

`pandas`, `numpy`, `scikit-learn`(TfidfVectorizer·TruncatedSVD 언피클용),
`xgboost`, `joblib` — `ml/serving/requirements.txt`.

### smoke test

`python ml/serving/model2/inference.py` 실행 → 위 출력 예시와 동일하게
1행 DataFrame 정상 반환 확인(2026-09-01).

---

## 3. 모델 3 — 사업 설계 이상패턴 탐지 (비교군 대비 거리 · Freeze)

**artifact 위치**: 원본 `ml/data/processed/design_features_v3.parquet`
(M66 공급보강본, pool 2,626건), 사본 `ml/serving/model3/`. 저장된 학습
weight가 없는 모델이다 — 매 요청마다 이 비교군 pool 전체를 다시 스캔해서
거리를 계산한다(`ml/models/README.md` 참조).

### 입력 필드

| 필드 | 필수 | 비고 |
|---|---|---|
| `support_type` | **예 — 없으면 그 행은 채점되지 않는다** | 비교군 사다리 1단계 키 |
| `support_method` | 권장 | 비교군 사다리 1단계 키 |
| `support_unit` | 선택 | |
| `amount_type` | 선택 | |
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
보여줄 때는 `m13_m4_anomaly.ALLOWED` 목록(`과거 사업 패턴과 차이가 큼` /
`희귀한 설계 조합` / `동일 유형 대비 비전형적` / `확인 필요`)만 쓰고,
`부적절함`·`지원규모 과다` 같은 판정형 표현은 쓰지 않는다.

### 출력 예시 (스모크 테스트 실측)

```text
    row_id  score            level cohort_key  cohort_n         top1_axis
0  DEMO001    1.0  L2 support_type         판로       255  project_duration
```

### 실행 예시

```python
import sys
sys.path.insert(0, "ml/serving/model3")
from inference import predict

records = [{
    "row_id": "REQ001",
    "support_type": "판로", "support_method": "보조금", "support_unit": "기업당",
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
1행 DataFrame + 허용 문구 목록 정상 반환 확인(2026-09-01).
