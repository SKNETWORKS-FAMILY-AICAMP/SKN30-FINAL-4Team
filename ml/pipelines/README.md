# pipelines/ — canonical 산출물을 만드는 코드

```text
pipelines/
├─ shared/   수집(d0*) → 추출(e01) → 기준테이블·feature(f0*) → EDA(a01)
│            + common.py · amount_parser.py (라이브러리)
├─ model1/   지원성격 분류 학습·적용·export
├─ model2/   지원규모 회귀 feature·학습·추론
└─ model3/   설계 이례성 벡터·비교군·스코어링
```

실행 순서가 곧 의존 순서다. 앞 단계 산출물이 없으면 뒤 단계가 돌지 않는다.
`1~3절이 shared/`, `4절이 모델별 폴더`다. 스크립트끼리는 평면 이름으로 import
하므로(`import common as C`) 폴더가 나뉜 것은 호출부에 영향이 없다.

## 1. 수집 (d)

| 스크립트 | 산출물 |
|---|---|
| `d01_download_api.py` | Open API 1,570건 공고문 → `data/raw/attachments/api/` |
| `d02_sample_list.py` | 목록 층화 표본 5,000건 → `list_sample.parquet` |
| `d03_targeted_sample.py` | 지원성격 부족 셀 보강 표본 → `list_sample_targeted.parquet` |
| `d04_download_documents.py` | 표본 공고문 다운로드 → `data/raw/attachments/list/` |

## 2. 추출 (e)

| 스크립트 | 산출물 |
|---|---|
| `e01_extract_text.py` | pdf-inspector(Rust) + rhwp 로 원문 추출 → `reports/e01_documents*.jsonl` |

> `e01_documents*.jsonl` **106MB 는 임시파일이 아니라 하류의 입력**이다.
> F04·F05·F06·M29 가 읽는다. gitignore 대상이라 저장소 비용은 없다.
> 이것만 있으면 `data/raw/` 없이도 F04 아래 전 구간이 재현된다.

## 3. 기준 테이블 · feature (f)

| 스크립트 | 산출물 |
|---|---|
| `f01_master.py` | `announcement_master.parquet` (97,794건) |
| `f02_detail.py` | `announcement_detail.parquet` (Open API 1,570건) |
| `f03_taxonomy.py` | `business_taxonomy.parquet` (중앙부처 1,505건) |
| `f04_merge_documents.py` | `announcement_detail_enriched.parquet` |
| `f05_amount_observations.py` | `support_amount_observations.parquet` |
| `f06_design_features.py` | `design_features{,_v2,_v3}.parquet` |
| `amount_parser.py` | 금액 파서 (라이브러리) |
| `common.py` | 경로·분류체계·정규화 (라이브러리) |
| `a01_eda_plots.py` | 전처리 검증 EDA → `figures/` |

**F06 3변형** — 셋 다 bit 단위로 재현된다(`tools/smoke_test.py` 가 매번 확인).

```bash
cd ml/pipelines/shared
python f06_design_features.py --legacy   # v1  design_features.parquet      9f308112fb99e750…
python f06_design_features.py            # v2  design_features_v2.parquet   eced88f6767e2e24…
python f06_design_features.py --supply   # v3  design_features_v3.parquet   79649c095b177583…
```

- **v1** 은 M56 세대의 지문으로 얼려 둔 대조본이다. 덮어쓰지 않는다.
- **v2** 는 근거문 결함을 F06 에서 고친 것 — **모델 2 canonical 입력**.
- **v3** 은 v2 위에 공급 보강(S1·S2·S3)을 얹은 것 — **모델 3 canonical 입력**.

## 4. 모델별 추론·학습 경로

**모델 1 (지원성격 분류 · KLUE-BERT)**

| 스크립트 | 역할 |
|---|---|
| `m01_support_type.py` | TF-IDF+LinearSVM 기준선 · `MIN_SUPPORT`·`coarsen`·`tfidf` 제공 (13개 모듈이 import) |
| `m02_apply.py` | ML 기준선 적용 → `announcement_detail_with_support_type_v2.parquet` (F06 입력) |
| `m08_apply_list_sample.py` | 목록 표본에 적용 → `list_sample_support_type.parquet` (F06 입력) |
| `dl11_m1_export.py` | 학습·외부검증 번들 → `models/model1_canonical/` |
| `dl12_m1_candidates.py` | 백본 6종 학습·선정 (원격 GPU) |
| `dl07_m1_apply.py` | KLUE-BERT 적용 → `openapi_support_type_roberta_v2.parquet` |

**모델 2 (지원규모 상대비교 · 비교군 percentile + XGBoost)**

| 스크립트 | 역할 |
|---|---|
| `m2_features.py` | feature/그룹 규격 · 데이터셋 지문 (`m2-feat-v2`) |
| `m45_m2_amount.py` | 1세대(LGBM·구조화) · `prepare`/`make_xy`/`compare` 제공 |
| `m56_m2_canonical.py` | canonical 학습 + **`serve()` 추론 진입점** |

**모델 3 (설계 이례성 · 비교군 대비 거리)**

저장된 가중치가 없다. 비교군 대비 거리를 매번 계산하는 구조다.

| 스크립트 | 역할 |
|---|---|
| `m3_lab.py` | 스코어링·안정성 검증 라이브러리 (`score_pool`·`load_pool`·`cohort_profile`) |
| `m13_m3_anomaly.py` | `prepare`·`encode`·축 정의 (25개 모듈이 import) |
| `m12_m3_cohort.py` | `cohort_reference.parquet` |
| `m38_m3_vector_direction.py` | 벡터 구성 · `MIN_COHORT` |
| `m47_m3_sensitivity.py` | `build_vectors_v` — v1/v2/v3 어느 데이터로도 벡터를 만든다 |
| `m33`·`m34`·`m36`·`m37`·`m41`·`m43` | 라벨셋 구성·진단 (위 모듈들이 import) |

> `m33`·`m36`·`m37`·`m41`·`m43` 은 그 자체로는 탐색이었지만 **채택 경로가
> import 하고 있어** pipelines 에 둔다. 의존 방향(상류→하류)을 지키기 위해서다.

## 알려진 예외 1건

`dl11_m1_export.py`(pipelines) 가 `m29_m1_external_eval.py`(evaluation) 의
`build_labelset`·`model_inputs` 를 import 한다. 외부 검증셋 구성 코드가 평가
스크립트 안에 있어서 생긴 역방향 의존이다. bootstrap 이 경로를 얹어 주므로
동작에는 문제가 없다. 분리하려면 M29 에서 labelset 구성부를 떼어내야 한다.
