# M80 — 금액의 의미(amount_type)와 기관×유형×연도 hierarchical prior

> 질문: **금액의 크기보다 '그 금액이 어떤 의미인가'를 먼저 구분하면 예측이
> 쉬워지는가. 기관·유형·연도별 체급을 prior 로 주면 M73 이 놓친 생성 구조를
> 보완할 수 있는가?**

## 0. 실행 전에 확인한 것 — 지시서 전제 두 가지가 다르다

```text
지시서가 가정한 amount_type   support_cap, support_per_recipient, support_per_project, loan_limit, total_budget
실제 amount_type              per_company, per_project
per_recipient_basis           stated_cap
1A 미실행 사유                amount_type 이 이미 M45.CATS 의 feature 다 — 지시서 1A 의 '이미 들어가 있으면 재실험하지 않는다'
```

M45 의 타깃 정의가 이미 `basis != 'stated_cap'` 을 전부 걸러냈다. total_budget
과 budget÷건수는 '한도'가 아니라 '평균'이라 제외됐고, 융자는 별도 amount_type
이 아니라 `support_type='융자'` 로 갈린다. **'의미가 다른 금액이 섞여 있다'는
문제는 데이터셋 구축 단계에서 이미 해결돼 있다.**

## 1. Experiment 0 — amount_type 진단

| amount_type | n | 커버리지 | median | mean | std | q25 | q75 | IQR | M73 MAE | bias(중앙) | 2배내 | 3배내 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| per_company | 1709 | 91.0% | 7.6990 | 7.6460 | 0.9556 | 7.0000 | 8.3222 | 1.3222 | 0.3478 | +0.0083 | 56.6% | 74.8% |
| per_project | 168 | 8.9% | 7.7782 | 7.7765 | 1.0662 | 7.0000 | 8.6021 | 1.6021 | 0.4425 | +0.0182 | 54.2% | 68.5% |

분포가 실제로 갈리는가 (`per_company` vs `per_project`)

```text
KS 검정        statistic 0.0964, p = 0.1082
분포 겹침 계수  0.7889
중앙값 차이     0.0792 log10 (= 1.20배)
잔차 분포 KS p  0.7669
```

지시서의 진행 기준 네 가지

| 기준 | 충족 |
|---|---|
| median 차이 > 0.15 log10 | 아니오 |
| IQR 구조 차이 > 0.30 | 아니오 |
| MAE 차이 > 0.05 | 예 |
| systematic bias > 0.05 | 아니오 |

**-> Experiment 1 진행.** 분포는 통계적으로 갈리지 않지만(KS p=0.1082), 
한쪽의 MAE 가 뚜렷하게 높다는 기준 하나가 충족된다.

## 2. Experiment 2 전제 — 기관 축 진단

```text
agency 종류        52종 / 결측 31.1%
현재 feature 포함   agency False / agency_grp True
기관별 중앙값 표준편차 0.5363 log10
```

`agency` 는 feature 에 **없다**(들어 있는 것은 4종짜리 agency_grp 뿐). 즉
Experiment 2 는 M73 이 한 번도 보지 못한 정보를 넣는다. 다만 계층을 내려갈수록
셀이 얇아진다.

| 계층 | 셀 수 | 중앙 n | n≥10 셀이 덮는 비중 |
|---|---:|---:|---:|
| support_type | 23 | 20.0 | 0.987 |
| agency x support_type | 257 | 2.0 | 0.372 |
| agency x support_type x year | 348 | 2.0 | 0.313 |

| 기관 (n≥30) | n | median y | M73 MAE |
|---|---:|---:|---:|
| 경상북도 | 41 | 6.653 | 0.3104 |
| 경기도 | 47 | 6.875 | 0.3477 |
| 중소벤처기업부 | 30 | 7.000 | 0.5339 |
| 미기재 | 583 | 7.301 | 0.3773 |
| 농식품부 | 71 | 7.699 | 0.3160 |
| 해수부 | 40 | 7.841 | 0.2828 |
| 환경부 | 49 | 7.929 | 0.3695 |
| 중기부 | 278 | 8.000 | 0.3402 |
| 산업부 | 156 | 8.000 | 0.3042 |
| 문체부 | 123 | 8.000 | 0.3155 |
| 과기부 | 155 | 8.398 | 0.3169 |

## 3. 누수 방어 — target encoding 을 어떻게 만들었는가

prior 는 타깃 통계를 feature 로 만드는 것이라, 방어 없이 만들면 각 행이 자기
정답을 자기 feature 로 돌려받는다. 학습에서는 완벽해 보이고 서빙에서는 무너진다.

```text
test 쪽은 outer train 전체, train 쪽은 inner GroupKFold(3) OOF
k 선택   inner OOF 의 prior 단독 MAE 로만 (대리 기준 — 한계는 보고서 3장)
shrinkage  prior_i = w·group_stat_i + (1-w)·prior_(i-1),  w = n/(n+k)
```

> k 선택의 한계: 엄밀히는 prior 를 먹는 트리모델의 MAE 로 골라야 하지만,
> 그러면 k 마다 전체 파이프라인을 다시 학습해야 한다. prior 자체의 품질이
> 좋을수록 feature 로서도 낫다고 보고 대리 기준을 썼다.

**대조군**: Hctrl/agency_cat — 기관을 타깃 통계 없이 범주형으로만 추가. H2/H3 의 개선이 '기관 정보' 때문인지 'hierarchical prior' 때문인지 가른다

## 4. 결과

| 후보 | 설명 | OOF MAE | Δ vs M73 | 95% CI | wilcoxon p | fold승 | strict MAE | 2배내 | 3배내 |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `M73 raw (soft/ordinal_xgb)` | M73 그대로 | 0.3563 | — | — | — | — | 0.3756 | 56.4% | 74.2% |
| `1E*/hybrid_nested` | Exp1E · M73 × type expert hybrid (alpha nested) | 0.3556 | -0.0007 | [-0.0028, +0.0015] | 0.199 | 3/5 | 0.3734 | 56.4% | 74.0% |
| `1C/resid_ridge` | Exp1C · amount_type+support_type ridge 잔차보정 | 0.3563 | +0.0000 | [-0.0005, +0.0005] | 0.781 | 2/5 | 0.3755 | 56.5% | 74.2% |
| `1C/resid_const` | Exp1C · amount_type 별 상수 잔차보정 | 0.3567 | +0.0004 | [-0.0004, +0.0013] | 0.338 | 1/5 | 0.3758 | 56.6% | 74.1% |
| `H1/stype` | Exp2 H1 · support_type prior | 0.3585 | +0.0023 | [-0.0001, +0.0046] | 0.359 | 1/5 | 0.3785 | 56.6% | 74.4% |
| `1B/type_prior` | Exp1B · amount_type prior feature | 0.3588 | +0.0026 | [-0.0000, +0.0051] | 0.355 | 1/5 | 0.3773 | 56.0% | 74.0% |
| `H3t/temporal` | Exp2 H3 · 미래 정보 미사용 판 (temporal sanity) | 0.3600 | +0.0037 | [+0.0004, +0.0072] | 0.0175 | 1/5 | 0.3796 | 56.5% | 73.7% |
| `Hctrl/agency_cat` | 대조군 · 기관을 범주형으로만 추가 (타깃 통계 없이) | 0.3619 | +0.0056 | [+0.0002, +0.0110] | 0.111 | 0/5 | 0.3840 | 55.8% | 73.1% |
| `H2/agency_stype` | Exp2 H2 · 기관 × 유형 prior | 0.3623 | +0.0060 | [+0.0013, +0.0105] | 0.135 | 1/5 | 0.3818 | 56.0% | 73.1% |
| `H3/agency_stype_year` | Exp2 H3 · 기관 × 유형 × 연도 prior | 0.3647 | +0.0084 | [+0.0035, +0.0133] | 0.00864 | 0/5 | 0.3836 | 56.5% | 72.8% |
| `1D/type_expert` | Exp1D · amount_type 별 전용 회귀 (n>=150) | 0.3729 | +0.0167 | [+0.0092, +0.0246] | 6.23e-05 | 1/5 | 0.3899 | 53.4% | 71.3% |
| `1D/type_expert_n100` |  | 0.3814 | +0.0251 | [+0.0160, +0.0346] | 7.01e-07 | 1/5 | 0.3995 | 52.2% | 69.8% |

### 4-2. sweep — 진단용 (승격 근거 아님)

| 후보 | OOF MAE | Δ | fold승 |
|---|---:|---:|---:|
| `SW1E/hybrid@0.7` | 0.3556 | -0.0007 | 3/5 |
| `SW1E/hybrid@0.8` | 0.3552 | -0.0010 | 4/5 |
| `SW1E/hybrid@0.9` | 0.3554 | -0.0008 | 4/5 |

## 5. prior 가 실제로 어느 계층까지 내려갔는가

`prior_level` 0=global · 1=support_type · 2=기관×유형 · 3=+연도

| 변형 | 계층별 행 비중 |
|---|---|
| 1B/type_prior | {'0': 0.0011, '1': 0.0016, '2': 0.9973} |
| H1/stype | {'0': 0.0011, '1': 0.9989} |
| H2/agency_stype | {'0': 0.0011, '1': 0.0735, '2': 0.9254} |
| H3/agency_stype_year | {'0': 0.0011, '1': 0.0735, '2': 0.0458, '3': 0.8796} |
| H3t/temporal | {'0': 0.0804, '1': 0.4028, '2': 0.5168} |

| 변형 | fold 별 선택 k |
|---|---|
| 1B/type_prior | [10, 10, 10, 10, 10] |
| H1/stype | [10, 10, 10, 10, 10] |
| H2/agency_stype | [10, 10, 10, 10, 10] |
| H3/agency_stype_year | [10, 10, 10, 10, 10] |
| H3t/temporal | [10, 10, 10, 10, 10] |

1D type expert coverage 0.9105 (나머지는 M73 fallback) / 1E alpha [0.7, 0.7, 0.7, 0.9, 0.7]

## 6. amount_type 별 · 비교군별 MAE — 최고 후보 vs M73

| amount_type | n | M73 | `1E*/hybrid_nested` | Δ |
|---|---:|---:|---:|---:|
| per_company | 1709 | 0.3478 | 0.3470 | -0.0008 |
| per_project | 168 | 0.4425 | 0.4425 | +0.0000 |

| 비교군 | n | M73 | `1E*/hybrid_nested` | Δ |
|---|---:|---:|---:|---:|
| bizinfo | 901 | 0.3900 | 0.3920 | +0.0020 |
| taxonomy | 976 | 0.3251 | 0.3220 | -0.0031 |
| api_summary | 157 | 0.4161 | 0.4156 | -0.0005 |
| document | 744 | 0.3845 | 0.3870 | +0.0025 |
| scale_text | 976 | 0.3251 | 0.3220 | -0.0031 |

| 구간 | n | M73 | `1E*/hybrid_nested` |
|---|---:|---:|---:|
| Low | 611 | 0.4111 | 0.4161 |
| Mid | 632 | 0.2909 | 0.2833 |
| High | 634 | 0.3685 | 0.3692 |

### 6-2. fold 별 MAE

| fold | 경계(원) | baseline | M73 | `1E*/hybrid_nested` |
|---|---|---:|---:|---:|
| 0 | 20,000,000 / 120,000,000 | 0.5425 | 0.3695 | 0.3695 |
| 1 | 20,000,000 / 140,000,000 | 0.4965 | 0.3469 | 0.3403 |
| 2 | 20,000,000 / 120,000,000 | 0.5262 | 0.3710 | 0.3687 |
| 3 | 20,000,000 / 120,000,000 | 0.5243 | 0.3747 | 0.3737 |
| 4 | 20,000,000 / 150,000,000 | 0.5218 | 0.3191 | 0.3258 |

## 7. 최종 비교표

| 방법 | OOF MAE | Strict MAE | Within 2x | Fold 승 | 95% CI |
|---|---:|---:|---:|---:|---|
| M73 raw | 0.3563 | 0.3756 | 56.4% | — | — |
| Exp1E · M73 × type expert hybrid (alpha nested) | 0.3556 | 0.3734 | 56.4% | 3/5 | [-0.0028, +0.0015] |
| Exp1C · amount_type+support_type ridge 잔차보정 | 0.3563 | 0.3755 | 56.5% | 2/5 | [-0.0005, +0.0005] |
| Exp1C · amount_type 별 상수 잔차보정 | 0.3567 | 0.3758 | 56.6% | 1/5 | [-0.0004, +0.0013] |
| Exp2 H1 · support_type prior | 0.3585 | 0.3785 | 56.6% | 1/5 | [-0.0001, +0.0046] |
| Exp1B · amount_type prior feature | 0.3588 | 0.3773 | 56.0% | 1/5 | [-0.0000, +0.0051] |
| Exp2 H3 · 미래 정보 미사용 판 (temporal sanity) | 0.3600 | 0.3796 | 56.5% | 1/5 | [+0.0004, +0.0072] |
| 대조군 · 기관을 범주형으로만 추가 (타깃 통계 없이) | 0.3619 | 0.3840 | 55.8% | 0/5 | [+0.0002, +0.0110] |
| Exp2 H2 · 기관 × 유형 prior | 0.3623 | 0.3818 | 56.0% | 1/5 | [+0.0013, +0.0105] |
| Exp2 H3 · 기관 × 유형 × 연도 prior | 0.3647 | 0.3836 | 56.5% | 0/5 | [+0.0035, +0.0133] |
| Exp1D · amount_type 별 전용 회귀 (n>=150) | 0.3729 | 0.3899 | 53.4% | 1/5 | [+0.0092, +0.0246] |
| 1D/type_expert_n100 | 0.3814 | 0.3995 | 52.2% | 1/5 | [+0.0160, +0.0346] |
| oracle 상한 (서빙 불가) | 0.2317 | — | 72.5% | — | — |

## 8. 최종 조합

미실행 — 지시서 '최종 조합 조건'. Exp1 유의 후보 0개 · Exp2 0개 (둘 다 필요)

## 9. 누수 점검 / 재현성

| 점검 | 결과 |
|---|---|
| prior 의 test 쪽 계산 | outer train 전체 통계 — 서빙에서 가능한 계산 |
| prior 의 train 쪽 계산 | inner GroupKFold(3) OOF — 자기 fold 를 뺀 통계만 자기에게 붙는다 (target encoding 누수 방어의 핵심) |
| shrinkage k 선택 | inner OOF 에서 prior 단독 MAE 로만. outer test 미사용 |
| 1C 잔차 / 1E alpha | inner OOF M73 예측과 outer train 정답만 |
| 1D type expert | outer train 의 해당 type 행만으로 학습 |
| 구간 경계 | fold train 의 y 만 (M73 과 동일) |
| temporal 판(H3t) | 연도 Y 행의 prior 는 train 의 year < Y 행만 사용 |
| test y 의 용도 | 최종 metric · oracle 상한 · 구간별 집계뿐 |
| 1A (amount_type feature) | 미실행 — M45.CATS 에 이미 포함 (지시서 1A 단서) |
| prior 가 outer test 를 보지 않았다 | PASS |
| train prior 가 자기 정답을 보지 않았다 (inner OOF) | PASS |
| raw 가 M73 공표치(0.3563)를 재현 | PASS |
| 재현성 PASS | PASS |
| 같은 seed 재실행 OOF 일치 | raw True / 1E*/hybrid_nested True / H3/agency_stype_year True |

## 10. 승격 점검표

대상: `1E*/hybrid_nested` (정직한 후보 중 OOF MAE 최저)

| 조건 | 결과 |
|---|---|
| 1. OOF MAE < 0.3563 | 통과 |
| 1b. 같은 fold raw 보다 낮다 | 통과 |
| 2. strict split 에서도 개선 | 통과 |
| 3. 5개 fold 중 4개 이상 개선 | 미달 |
| 4. paired 95% CI 가 0 아래 | 미달 |
| 5. 특정 amount_type 하나에만 이득이 몰리지 않음 | 통과 |
| 5b. taxonomy·bizinfo 한쪽에만 의존하지 않음 | 미달 |
| 6. fallback 과도하지 않음 | 통과 |
| 7. leakage audit PASS | 통과 |
| 7b. reproducibility PASS | 통과 |
| 8. 실질 개선폭 ΔMAE ≤ -0.003 | 미달 |
| 9. 1차 목표 MAE < 0.35 | 미달 |

## 결론

```text
M73 raw (같은 fold 재현)  MAE = 0.3563
최고 후보                 1E*/hybrid_nested
                          MAE = 0.3556  (Δ -0.0007, 95%CI [-0.0028, +0.0015])
기관 정보 대조군          Hctrl 0.3619 (Δ +0.0056)
amount_type 진단          Experiment 1 진행

판정: 현행 유지 — M73 `soft/ordinal_xgb`
```