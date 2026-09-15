# M56 — 모델 2 canonical 승격 (STEP 1 · 3 · 4)

> M45 를 지우지 않는다. M45 는 이전 canonical 로 남고 이 스크립트가 새
> entrypoint 가 된다. 바뀐 것은 **feature 와 학습기 둘뿐**이고, 타깃 정의·
> 비교군 사다리·percentile 조회·문구 규율은 M45 것을 그대로 쓴다.

## STEP 1 — 재현성 고정

| 항목 | 값 |
|---|---|
| dataset | `data\processed\design_features.parquet` |
| 생성 스크립트 | `ml/scripts/f06_design_features.py` |
| sha256 | `9f308112fb99e750fcbd4fdcba980a9a3ac698eaa623b99e60e205e11c8043e4` |
| 파일 크기 / 시각 | 965903 bytes / 2026-08-26T11:52:59.969189644 |
| 행 수 | 원본 3963 → 필터 후 **1877** (기대 1877, 일치 True) |
| target | `log10(per_recipient)`, basis = `stated_cap` |
| feature version | `m2-feat-v2` |
| grouping version | `m2-group-v2` |
| seed | 42 |
| 실행 시각 | 2026-08-27T17:11:34.308369 (Python 3.13.12) |

### 타깃 — 무엇을 학습하고 무엇을 제외하는가

| 의미 | 처리 |
|---|---|
| `total_budget` | 제외 — 사업 전체 예산. per_recipient 와 단위가 다르다 |
| `support_cap / support_per_recipient` | **사용** — 원문에 명시된 기업당 한도(stated_cap) |
| `support_per_project` | amount_type=per_project 인 행은 support_unit=project 로 비교군이 갈린다. 타깃 자체는 같은 stated_cap |
| `budget_div_count(총예산÷건수)` | 제외 — '평균'이라 '한도'와 의미가 다르다 |
| `loan_limit` | 별도 컬럼 없음. 융자는 support_method=loan 으로 비교군이 갈린다 |
| `support_rate` | 타깃 아님 — feature(support_ratio) |
| `selected_count` | 타깃 아님 — feature(support_count) |

제외 규칙: support_type 결측 제외 / per_recipient 결측·0 이하 제외 / amount_outlier(상식범위 밖 = 파싱오류) 제외 / per_recipient_basis != 'stated_cap' 제외 / support_unit / cohort 결측 제외

상류 전처리: f05_amount_observations.py — 지원규모 관측 추출 (PDF 확장자 제외: EXCLUDE_EXT={'pdf'}) · e01_extract_text.py — 공고문 원문 텍스트 추출 · amount_parser.py — 금액/기간/기업수 파싱 (M32 패턴 수정 반영)

### feature 목록

```text
[기존 구조화 feature — M45 와 동일]
  범주형  support_type, support_method, support_unit, category_large, industry_grp, agency_grp, amount_type, cohort
  수치형  support_count, support_ratio, project_duration, self_burden_ratio, year

[신규 제목 feature]
  입력    title (amount_masked)
  벡터화  TfidfVectorizer / char_wb / ngram (2, 3) / min_df 3 / max_features 30000 / sublinear_tf True
  차원축소 TruncatedSVD 64 -> title_svd00 ~ title_svd63
  적합    fold train only

[사용 금지]
  evidence_text — 타깃 per_recipient 가 파싱된 바로 그 문장
```

### 모델 파라미터 (M53 실측값. 새 튜닝 없음)

```text
  objective          reg:absoluteerror
  n_estimators       800
  learning_rate      0.03
  max_depth          6
  subsample          0.9
  colsample_bytree   0.8
  min_child_weight   1
  reg_alpha          0.0
  reg_lambda         1.0
  tree_method        hist
  enable_categorical True
  random_state       42
  (구간용) objective  reg:quantileerror  quantile_alpha [0.1, 0.9]
```

### 그룹키

```text
기본   program_stem            재공고(같은 사업의 반복 공고)를 묶는다
엄격   normalize_business_title()  지역·연도·회차·재공고 표현·숫자를 지운
                              제목. 같은 사업 계열까지 묶는다
n_splits 5 / GroupKFold 는 결정적이라 별도 seed 가 없다
```

## STEP 3 — 같은 fold 안에서 M45 vs M56

두 모델을 **같은 split 루프 안에서** 학습했다. fold 를 따로 만들면
'조건이 같았다'를 증명할 수 없다. baseline·metric·타깃·N 모두 공유하고
달라지는 것은 feature 와 학습기뿐이다.

| 지표 | M45 | M56 | 차이 | 판정 |
|---|---:|---:|---:|---|
| MAE(log10) | 0.4681 | 0.4155 | -0.0526 | M56 우세 |
| baseline 대비 개선 | 11.9% | 21.8% | +9.9%p | M56 우세 |
| Within 2x | 43.1% | 48.8% | +5.6%p | M56 우세 |
| Within 3x | 61.6% | 67.6% | +6.0%p | M56 우세 |
| fold 재구성 최저 개선율 | 10.0% | 20.6% | +10.6%p | M56 우세 |
| fold 재구성 통과 | 9/10 | 10/10 | | M56 우세 |
| CQR Coverage | 79.6% (공표) | 80.3% | +0.7%p | 유사/소폭 개선 |
| Median Interval Width | 33.2배 (공표) | 25.1배 | 축소 | M56 우세 |
| 비교군 가능/넓음/어려움 | 12 / 14 / 3 | 16 / 12 / 1 | 개선 | M56 우세 |

> 개선율·안정성은 이번 실행에서 M45 를 **다시 학습해** 얻은 값이다.
> 구간 커버리지·폭·등급 분포는 M45 공표치(성능결과서 2장)와 비교했다.

### 개선 분해 — 입력이 먼저, 학습기가 다음

| 조건 | MAE(log10) | baseline 대비 |
|---|---:|---:|
| M45(LGBM·구조화) | 0.4681 | 11.9% |
| 분해: XGB·구조화만 | 0.4536 | 14.6% |
| 분해: LGBM·구조화+제목 | 0.4384 | 17.5% |
| M56(XGB·구조화+제목) | 0.4155 | 21.8% |

```text
        구조화만        구조화+제목
LGBM    0.4681          0.4384     <- feature 를 바꾼 이득
XGB     0.4536          0.4155     <- 학습기를 바꾼 이득
```

두 축이 겹치지 않고 각각 붙는다. 제목 feature 는 학습기와 무관하게
MAE 를 내리고(LGBM 에서도 XGB 에서도), 학습기 교체 역시 feature 와
무관하게 내린다.

### 엄격 그룹(정규화제목)에서 다시

| 조건 | MAE(log10) | baseline 대비 |
|---|---:|---:|
| M45(LGBM·구조화) | 0.4858 | 11.7% |
| M56(XGB·구조화+제목) | 0.4356 | 20.8% |

## 서비스 inference 동기화

학습만 새 파이프라인이고 서빙이 옛 feature 를 쓰면 승격이 아니다.
전체 데이터로 적합한 산출물(`data\processed\m56_model2_canonical`)을 저장하고, **저장물만으로** 만든
feature 가 학습 때 feature 와 같은지 왕복으로 확인했다.

| 점검 | 결과 |
|---|---|
| n_checked | 5 |
| feature_order_identical | True |
| numeric_identical | True |
| categorical_identical | True |
| prediction_identical | True |
| all_pass | True |

저장물: TF-IDF vectorizer · SVD · 점추정 모델 · 분위 모델 · CQR delta ·
feature order · 범주 목록 · industry 상위 15종. 추론 함수 `serve()` 가
제목 마스킹 → 벡터화 → SVD → 구조화 feature 정렬을 학습과 같은 순서로
수행한다. percentile 조회는 바뀌지 않았으므로 `M45.compare()` 를 그대로 쓴다.

이 저장소에는 별도의 서비스 추론 코드베이스가 없다(demo 는 정적 목업).
따라서 이 모듈이 곧 추론 경로다 — 다른 곳에서 모델 2 를 호출하게 되면
`m2_features` 와 `m56_m2_canonical.serve()` 를 import 해야 하고, feature 를
다시 구현하면 안 된다.

## STEP 4 — 승격 점검표

| 항목 | 결과 |
|---|---|
| M53/M56 실행 재현 가능 (동일 스크립트·seed·해시 기록) | PASS |
| feature pipeline 코드 고정 (m2_features.py) | PASS |
| title normalization 코드 고정 (normalize_business_title) | PASS |
| direct amount leakage 점검 통과 (M55) | PASS |
| normalized-title GroupKFold 에서 개선 유지 | PASS |
| 동일사업·재공고 계열 leakage 방지 | PASS |
| M45 대비 동일 조건 성능 우세 | PASS |
| fold stability 우세 | PASS |
| interval 품질 악화 없음 | PASS |
| 서비스 inference 동기화 (왕복 자체점검) | PASS |
| Product Boundary 위반 없음 (적정/과다/삭감 문구 없음) | PASS |

## 판정

**M53 CANONICAL 승격**

- 동일 조건(같은 dataset·target·N=1877·fold·baseline·metric)에서 MAE 0.4681 → 0.4155,
  개선율 11.9% → 21.8%.
- 제목에 금액·비율 문자열이 0건이고 마스킹 후에도 수치가 동일하다(M55).
- 지역·연도·회차를 지운 계열 그룹으로 다시 갈라도 개선율 20.8% 로 유지된다.
- 제목 단독(B)은 구조화 단독(A)보다 나쁘고 둘을 합쳤을 때만 가장 좋다 —
  제목은 식별자가 아니라 보완 feature 다(M55 2.3).
- 저장물만으로 추론했을 때 학습과 같은 feature·같은 예측이 나온다.

> 모델 2 는 여전히 지원규모 **적정성 판정 모델이 아니다.** 1차 산출물은
> 비교군 percentile 이고, 회귀는 비교군이 얇을 때의 보조 추정이다.
