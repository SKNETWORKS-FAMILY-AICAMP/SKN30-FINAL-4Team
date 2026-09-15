# M79 — 이종 expert 회귀모델 (XGBoost · LightGBM · CatBoost)

> 질문: **Low/Mid/High expert 가 꼭 XGBoost 여야 하는가. 다른 모델군이
> 특정 금액구간에서 더 정확하거나, 서로 다르게 틀려 ensemble 가치가 있는가?**

## 0. 같은 조건 / 바뀐 것

```text
dataset  data\processed\design_features_v2.parquet  (1877행)
sha256   eced88f6767e2e2460a05812dd8e9cd39990937e4c1a2a8363e02d114bfdc7f4
target   log10(per_recipient), basis=stated_cap
split    GroupKFold(5), group=program_stem / normalized_title
feature  M69 G 단계 (m2-feat-v2 + 원천층 m2-source-v1 + 본문 SVD64)
router   M73 ordinal_xgb 고정
바뀐 것  Low/Mid/High expert 3개의 회귀모델 종류 하나
```

모델군 비교가 공정하려면 튜닝 예산이 같아야 한다.

```text
정렬     세 주 설정의 학습률 0.03 · 트리 800 · 깊이 6 · L1 목적함수를 같은 자리에 맞췄다
xgb      {'objective': 'reg:absoluteerror', 'n_estimators': 800, 'learning_rate': 0.03, 'max_depth': 6}
lgbm     {'objective': 'regression_l1', 'n_estimators': 800, 'learning_rate': 0.03, 'num_leaves': 31, 'max_depth': -1}
cat      {'loss_function': 'MAE', 'iterations': 800, 'learning_rate': 0.03, 'depth': 6}
```

```text
primary  모델군당 주 설정 하나를 데이터 보기 전 고정
nested   expert 조합(1B)·ensemble weight(3B·3C)는 outer train 안 inner GroupKFold(3) OOF soft MAE 로만
sweep    설정 sweep · weight sweep 은 진단용. 여기서 최저값을 골라 승격 근거로 쓰지 않는다
```

## 1. Experiment 0 — M73 canonical 재현

```text
공표 M73        0.3563
이 실험의 재현   0.3563   (차 0.00000)
행 단위 대조     1877행 / 최대 차 0.000000 / 평균 차 0.000000 / 완전일치 True
```

## 2. Experiment 1-A — 동일 모델군 3 expert

| 후보 | OOF MAE | Δ vs M73 | 95% CI | wilcoxon p | fold승 | strict MAE | 2배내 | 3배내 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `1A/xgb (M73)` | 0.3563 | — | — | — | — | 0.3756 | 56.4% | 74.2% |
| `1A/lgbm` | 0.3633 | +0.0070 | [+0.0038, +0.0102] | 2.42e-05 | 0/5 | 0.3805 | 54.4% | 73.6% |
| `1A/cat` | 0.3632 | +0.0069 | [+0.0036, +0.0102] | 0.000163 | 0/5 | 0.3824 | 55.5% | 73.6% |

### 2-2. 설정 sweep — 진단용 (승격 근거 아님)

| 설정 | OOF MAE | Δ vs M73 | fold승 |
|---|---:|---:|---:|
| `lgbm/leaves15` | 0.3640 | +0.0078 | 0/5 |
| `lgbm/depth5` | 0.3643 | +0.0080 | 0/5 |
| `lgbm/lr05` | 0.3638 | +0.0076 | 0/5 |
| `cat/depth4` | 0.3667 | +0.0105 | 0/5 |
| `cat/lr05` | 0.3614 | +0.0052 | 1/5 |

## 3. 진단 1 — expert 별 모델군 (soft 로 섞기 전)

각 구간의 expert 를 **그 구간의 실제 행에서만** 잰 MAE 다. 어느 모델군이
어느 금액대를 잘 맞히는지가 섞이기 전 상태로 보인다.

| 구간 | n | xgb | lgbm | cat | 최저 |
|---|---:|---:|---:|---:|---|
| Low | 611 | 0.2979 | 0.3056 | 0.3101 | xgb |
| Mid | 632 | 0.1778 | 0.1845 | 0.1811 | xgb |
| High | 634 | 0.2215 | 0.2328 | 0.2324 | xgb |

## 4. Experiment 1-B — expert 별 모델군 선택

| 후보 | OOF MAE | Δ vs M73 | 95% CI | fold승 | strict MAE |
|---|---:|---:|---:|---:|---:|
| `1B/hetero_expertwise` | 0.3582 | +0.0019 | [+0.0005, +0.0034] | 0/5 | 0.3775 |
| `1B*/hetero_nested` | 0.3597 | +0.0035 | [+0.0016, +0.0053] | 0/5 | 0.3796 |

fold 별로 무엇을 골랐는가 (Low / Mid / High)

| fold | expertwise | nested (soft MAE 기준) | inner MAE |
|---|---|---|---:|
| 0 | ['xgb', 'cat', 'xgb'] | ['xgb', 'xgb', 'cat'] | 0.3698 |
| 1 | ['cat', 'xgb', 'xgb'] | ['cat', 'xgb', 'xgb'] | 0.3699 |
| 2 | ['xgb', 'xgb', 'xgb'] | ['xgb', 'cat', 'xgb'] | 0.3792 |
| 3 | ['xgb', 'cat', 'xgb'] | ['cat', 'cat', 'xgb'] | 0.3620 |
| 4 | ['xgb', 'xgb', 'xgb'] | ['xgb', 'xgb', 'xgb'] | 0.3761 |

## 5. Experiment 2 — residual 상보성

| 구간 | n | xgb~lgbm | xgb~cat | lgbm~cat |
|---|---:|---:|---:|---:|
| Low | 611 | 0.9743 | 0.9714 | 0.9779 |
| Mid | 632 | 0.9696 | 0.9646 | 0.9753 |
| High | 634 | 0.9473 | 0.9471 | 0.9747 |
| **최종 soft** | — | 0.9898 | 0.9892 | 0.9927 |

> 상관이 1 에 가까우면 세 모델이 **같은 행에서 같은 방향으로** 틀린다는 뜻이고,
> 그러면 평균을 내도 오차가 상쇄되지 않는다. 아래는 그 반대 각도의 확인 —
> 한 모델이 다른 모델보다 더 맞힌 행의 비중이다.

| 비교 | 이긴 행 비중 |
|---|---:|
| xgb beats lgbm | 0.536 |
| xgb beats cat | 0.527 |
| lgbm beats xgb | 0.464 |
| lgbm beats cat | 0.498 |
| cat beats xgb | 0.473 |
| cat beats lgbm | 0.502 |

**진단: ensemble 가치 낮음** (최종 soft residual 상관 최대 0.9927)

## 6. Experiment 3 — expert-level ensemble

| 후보 | OOF MAE | Δ vs M73 | 95% CI | fold승 | strict MAE | 모델 수 |
|---|---:|---:|---:|---:|---:|---:|
| `3A/avg_xgb_cat` | 0.3585 | +0.0023 | [+0.0005, +0.0040] | 0/5 | 0.3775 | 8 |
| `3A/avg_all3` | 0.3597 | +0.0034 | [+0.0014, +0.0054] | 0/5 | 0.3779 | 11 |
| `3B*/pair_nested` | 0.3575 | +0.0012 | [+0.0001, +0.0023] | 0/5 | 0.3767 | 8 |
| `3C*/expert_mix_nested` | 0.3585 | +0.0022 | [+0.0007, +0.0037] | 0/5 | 0.3777 | 7 |

### 6-2. weight sweep — 진단용

| 혼합 | OOF MAE | Δ vs M73 | fold승 |
|---|---:|---:|---:|
| `SWens/xgbcat@0.5` | 0.3585 | +0.0023 | 0/5 |
| `SWens/xgbcat@0.6` | 0.3579 | +0.0016 | 0/5 |
| `SWens/xgbcat@0.7` | 0.3573 | +0.0011 | 0/5 |
| `SWens/xgblgbm@0.5` | 0.3588 | +0.0026 | 1/5 |
| `SWens/xgblgbm@0.6` | 0.3581 | +0.0019 | 1/5 |
| `SWens/xgblgbm@0.7` | 0.3575 | +0.0013 | 1/5 |

fold 별로 무엇을 골랐는가

| fold | 3B 쌍·weight | 3C expert 별 혼합 (Low/Mid/High) |
|---|---|---|
| 0 | ['xgb', 'cat'] @ 0.7 | ['xgb', 'xc70', 'xc50'] |
| 1 | ['xgb', 'cat'] @ 0.7 | ['xc50', 'xgb', 'xgb'] |
| 2 | ['xgb', 'cat'] @ 0.7 | ['xgb', 'xc50', 'xc70'] |
| 3 | ['xgb', 'cat'] @ 0.6 | ['cat', 'cat', 'xgb'] |
| 4 | ['xgb', 'cat'] @ 0.7 | ['all33', 'xgb', 'xgb'] |

## 7. 구간별 · 비교군별 MAE — 최고 후보 vs M73

| 구간 | n | M73 | `3B*/pair_nested` |
|---|---:|---:|---:|
| Low | 611 | 0.4111 | 0.4121 |
| Mid | 632 | 0.2909 | 0.2926 |
| High | 634 | 0.3685 | 0.3695 |

| 비교군 | n | M73 | `3B*/pair_nested` | Δ |
|---|---:|---:|---:|---:|
| bizinfo | 901 | 0.3900 | 0.3911 | +0.0011 |
| taxonomy | 976 | 0.3251 | 0.3264 | +0.0013 |
| api_summary | 157 | 0.4161 | 0.4194 | +0.0033 |
| document | 744 | 0.3845 | 0.3851 | +0.0006 |
| scale_text | 976 | 0.3251 | 0.3264 | +0.0013 |

> 승격조건 5 — 개선이 taxonomy 한쪽에서만 나고 bizinfo 가 악화되면
> 승격하지 않는다.

### 7-2. fold 별 MAE

| fold | 경계(원) | baseline | M73 | `3B*/pair_nested` |
|---|---|---:|---:|---:|
| 0 | 20,000,000 / 120,000,000 | 0.5425 | 0.3695 | 0.3697 |
| 1 | 20,000,000 / 140,000,000 | 0.4965 | 0.3469 | 0.3474 |
| 2 | 20,000,000 / 120,000,000 | 0.5262 | 0.3710 | 0.3713 |
| 3 | 20,000,000 / 120,000,000 | 0.5243 | 0.3747 | 0.3773 |
| 4 | 20,000,000 / 150,000,000 | 0.5218 | 0.3191 | 0.3217 |

## 8. serving 비용

| 모델군 | expert 3개 학습 | 예측 | 모델 크기 |
|---|---:|---:|---:|
| xgb | 23.9초 | 0.079초 | 6.4 MB |
| lgbm | 5.6초 | 0.043초 | 3.9 MB |
| cat | 99.7초 | 0.034초 | 2.8 MB |

라우터(ordinal 이진 2개) 학습 14.8초

| 후보 | serving 모델 수 |
|---|---:|
| 1B*/hetero_nested | 5 |
| 1B/hetero_expertwise | 5 |
| 1A/xgb (M73) | 5 |
| 1A/lgbm | 5 |
| 1A/cat | 5 |
| 3C*/expert_mix_nested | 7 |
| 3A/avg_xgb_cat | 8 |
| 3B*/pair_nested | 8 |
| 3A/avg_all3 | 11 |

> 지시서: **0.001 개선 때문에 모델 10개를 띄우는 구조는 승격하지 않는다.**

## 9. 최종 비교표

| 방법 | OOF MAE | Strict MAE | Within 2x | Fold 승 | 95% CI | 모델 수 |
|---|---:|---:|---:|---:|---|---:|
| M73 XGB Experts | 0.3563 | 0.3756 | 56.4% | — | — | 5 |
| 3B*/pair_nested | 0.3575 | 0.3767 | 56.1% | 0/5 | [+0.0001, +0.0023] | 8 |
| 1B/hetero_expertwise | 0.3582 | 0.3775 | 56.2% | 0/5 | [+0.0005, +0.0034] | 5 |
| 3A/avg_xgb_cat | 0.3585 | 0.3775 | 56.2% | 0/5 | [+0.0005, +0.0040] | 8 |
| 3C*/expert_mix_nested | 0.3585 | 0.3777 | 56.1% | 0/5 | [+0.0007, +0.0037] | 7 |
| 1B*/hetero_nested | 0.3597 | 0.3796 | 56.4% | 0/5 | [+0.0016, +0.0053] | 5 |
| 3A/avg_all3 | 0.3597 | 0.3779 | 55.6% | 0/5 | [+0.0014, +0.0054] | 11 |
| 1A/cat | 0.3632 | 0.3824 | 55.5% | 0/5 | [+0.0036, +0.0102] | 5 |
| 1A/lgbm | 0.3633 | 0.3805 | 54.4% | 0/5 | [+0.0038, +0.0102] | 5 |
| oracle 상한 (서빙 불가) | 0.2317 | — | 72.5% | — | — | — |

## 10. 누수 점검 / 재현성

| 점검 | 결과 |
|---|---|
| 라우터 | M73 ordinal_xgb 고정 — 모든 후보가 같은 확률을 쓴다 |
| 구간 경계 | fold train 의 y 만 (M73 과 동일) |
| 모델군 주 설정 | 데이터 보기 전 고정 (lr 0.03 · 트리 800 · 깊이 6 · L1). sweep 표는 진단용으로 격리 |
| expert 조합 선택(1B) | outer train 안 inner GroupKFold(3) OOF 에서만 |
| ensemble weight 선택(3B·3C) | 같은 inner OOF soft MAE 에서만 |
| test y 의 용도 | 최종 metric · oracle 상한 · 구간별 집계뿐 |
| feature | M69 G 단계 그대로 — 모델군마다 바꾸지 않는다 |
| 조합·weight 가 outer test 를 보지 않았다 | PASS |
| baseline 이 M73 공표치(0.3563)를 재현 | PASS |
| 재현성 PASS | PASS |
| 같은 seed 재실행 OOF 일치 | 1A/xgb (M73) True / 1A/lgbm True / 1A/cat True / 3B*/pair_nested True |

## 11. 승격 점검표

대상: `3B*/pair_nested` (정직한 후보 중 OOF MAE 최저)

| 조건 | 결과 |
|---|---|
| 1. OOF MAE < 0.3563 | 미달 |
| 1b. 같은 fold baseline 보다 낮다 | 미달 |
| 2. strict split 에서도 개선 | 미달 |
| 3. 5개 fold 중 4개 이상 개선 | 미달 |
| 4. paired 95% CI 가 0 아래 | 미달 |
| 5. taxonomy·bizinfo 한쪽에만 의존하지 않음 | 미달 |
| 6. reproducibility PASS | 통과 |
| 7. leakage audit PASS | 통과 |
| 8. 실질 기준 ΔMAE ≤ -0.003 | 미달 |
| 9. serving 복잡도 납득 가능 (모델 8개) | 미달 |
| 10. 1차 목표 MAE < 0.35 | 미달 |

## 결론

```text
M73 XGB experts (재현)   MAE = 0.3563
최고 후보                3B*/pair_nested
                         MAE = 0.3575  (Δ +0.0012, 95%CI [+0.0001, +0.0023])
residual 상관 (최종 soft) 최대 0.9927 -> ensemble 가치 낮음
serving 모델 수          M73 5개 -> 후보 8개

판정: 현행 유지 — M73 `soft/ordinal_xgb`
```