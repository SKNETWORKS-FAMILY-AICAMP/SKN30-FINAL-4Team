# Model 1 — 지원성격 19종 분류

> 상세 서술은 `../../docs/02_모델_1_2_3_성능_결과서.md` 1장.
> 여기는 **어느 실험이 무엇을 바꿨고 어디에 근거가 있는지** 만 적는다.

```text
TF-IDF/LinearSVM  ->  KoELECTRA  ->  KLUE-RoBERTa  ->  KLUE-BERT  ->  freeze
```

**채택: KLUE-BERT** · 그룹CV macroF1 0.8350 · 외부 131건 정확도 **0.8422 ± 0.0072**
· 학습 1,404건 · 19클래스

## core — 성능을 만든 흐름

| 실험 | 무엇을 했나 | 근거 |
|---|---|---|
| M01 | TF-IDF + LinearSVM ML 기준선 (외부 0.7252) | `core/m01_support_type.json` |
| DL01 | 백본 탐색 — KoELECTRA(lr 3종) · KLUE-RoBERTa(epoch 3종) | `core/dl01_transformer_clf_*.json` (7) |
| DL02 | 승자 백본 하이퍼파라미터 ablation | `core/dl02_ablation_v1.json` |
| DL06 | 19클래스로 재정의하고 재학습 + ablation | `core/dl06_m1_ablation_{full,smoke}.json` |
| DL12 | 재학습 후보 추출 (DL/ML 양쪽) | `core/dl12_m1_candidates_{dl,ml}.json` |
| DL15 | **백본 6종 최종 선정 → KLUE-BERT** | `core/dl15_final_selection.{json,md}` |
| DL20 | 최종 export | `core/dl20_m1_final_export.json` |

## validation — 그 숫자를 믿을 수 있는가

| 실험 | 확인한 것 | 결과 |
|---|---|---|
| DL03 | early stopping vs 고정 epoch (nested split) | `validation/dl03_early_stopping_v1.json` |
| M02 · M08 | 분류기 실전 적용 · 목록 표본(2019~2025) 적용 | `validation/m02_apply.json` · `m08_apply_list_sample.json` |
| M28 | 외부 검증셋 41 → 131건 확장 후보 추출 | `validation/m28_m1_external_pool.json` |
| M29 | **외부 Open API 131건 정확도 0.8422 ± 0.0072** | `validation/m29_m1_external_eval.{json,md}` |
| M54 | 오차가 `사업화`(학습의 31%)로 빨려 든다 — 백본이 아니라 **다수 클래스 사전확률** | `validation/m54_m1_class_diagnosis.{json,md}` |
| M57 | 라벨 확신도 '높음' 79건 0.9494 vs 나머지 0.6538 — **섞인 두 숫자를 분리하고 freeze** | `validation/m57_m1_confidence_split.{json,md}` |
| DL16 | 판단보류 커버리지 70%에서 정확도 **0.9275** | `validation/dl16_m1_abstention.json` |

**판정이 한 번 뒤집힌 이력**을 남겨 둔다. 초기 41건 대조에서 RoBERTa 가 51.2%로
무너져 ML 을 채택했으나, 원인은 모델이 아니라 **DL 만 학습·서빙 텍스트가 어긋난
것**이었다. 입력을 맞추고 정답셋을 131건으로 늘리자 DL 이 다시 앞섰다.

## archive — 해봤지만 승격되지 않은 것

| 실험 | 결과 | 미채택 사유 |
|---|---|---|
| DL04 MC Dropout | 불확실성 분해 | 판단보류가 `predict_proba` 로 이미 해결됨 |
| DL05 KLUE-RoBERTa 재적용 | 외부 0.8143 | KLUE-BERT 가 앞섬 (DL15) |
| M24 Calibrated LinearSVM | 커버리지 70.7%에서 0.9164 | **보정에 학습데이터 30% 소모** — 무보정 기준선을 못 넘음 |
| M25 TF-IDF feature 고도화 | — | ML 세대. DL 채택으로 무의미해짐 |
| M27 margin 기반 판단보류 | — | DL16 / M57 로 대체 |

## 서빙

```text
serving/model1/predict.py    진입점 (inference.py 구현)
번들: models/model1_canonical/{train,external}.parquet
백본 가중치는 저장소 밖 — pipelines/model1/dl12_m1_candidates.py 로 재학습
```
