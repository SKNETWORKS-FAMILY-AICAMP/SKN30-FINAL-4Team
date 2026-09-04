# M69 — 원천 feature 보강 → Stage 1 재검증 → expert 재도전

> 질문: **금액 규모를 결정하는 원천 정보를 더 정확히 구조화하면, Stage 1
> 금액구간 분류 성능을 73.2%에서 충분히 끌어올릴 수 있는가?**

## 0. 같은 조건 / 바뀐 것

```text
dataset  data\processed\design_features_v2.parquet  (1877행)
sha256   eced88f6767e2e2460a05812dd8e9cd39990937e4c1a2a8363e02d114bfdc7f4
target   log10(per_recipient), basis=stated_cap
split    GroupKFold(5), group=program_stem / normalized_title
바뀐 것  입력 feature 만 — 모델 2 전용 원천 feature 층 m2-source-v1
```

## 1. 지시서 3장 후보 11종을 어떻게 처리했는가

| 후보 | 처리 | 이유 |
|---|---|---|
| `support_cap` | **제외 (누수)** | 타깃 그 자체 — per_recipient(basis=stated_cap)이 원문의 기업당 한도다 |
| `support_per_recipient` | **제외 (누수)** | 타깃 그 자체 (컬럼 이름만 다르다) |
| `support_per_project` | **제외 (누수)** | amount_type=per_project 행의 타깃. support_unit 으로만 갈린다 |
| `loan_limit` | **제외 (누수)** | 융자 사업의 타깃. 별도 컬럼이 아니라 같은 자리에서 파싱된다 |
| `amount_min` | **제외 (누수)** | 타깃과 같은 금액 표현의 하한 — 사실상 복사본 |
| `amount_unit_raw` | **제외 (누수)** | 타깃 표현의 단위어. 자릿수를 그대로 읽는다 |
| `support_rate(=support_ratio)` | 이미 M65 feature | 값이 아니라 결측·근거등급·일관성만 보강 |
| `self_burden_rate(=self_burden_ratio)` | 이미 M65 feature | 값이 아니라 결측·근거등급·일관성만 보강 |
| `selected_count(=support_count)` | 이미 M65 feature | 값이 아니라 결측·근거등급·일관성만 보강 |
| `project_duration` | 이미 M65 feature | 값이 아니라 결측·근거등급·일관성만 보강 |
| `support_unit` | 이미 M65 feature | 값이 아니라 결측·근거등급·일관성만 보강 |
| `support_method` | 이미 M65 feature | 값이 아니라 결측·근거등급·일관성만 보강 |
| `total_budget` | **신규 사용** | 타깃 후보를 뺀 나머지 금액 후보에서 추출 |

> 지시서 3장의 후보 11개 중 4개는 이 데이터셋에서 **타깃 그 자체**이고
> 6개는 **이미 M65 에 들어 있다**. 순수 신규는 `total_budget` 하나뿐이라,
> 그 칸만으로는 지시서가 기대한 폭이 나오지 않는다. 그래서 ablation 에
> 지시서에 없던 **G(마스킹 본문 텍스트)** 를 한 칸 더 뒀다 — 현재 모델 2 는
> 제목만 텍스트로 쓰고 사업내용·지원대상 본문은 쓰지 않는다.

## 2. feature 품질 감사 (지시서 5장)

| feature | coverage | 결측률 | 종류 | 이상치 | y 상관 | 타깃과 동일값 |
|---|---:|---:|---|---:|---:|---:|
| `nb_total_budget_log10` | 0.092 | 0.908 | numeric | 1 | 0.581 | 0.064 |
| `nb_has_total_budget` | 1.000 | 0.000 | numeric | 0 | 0.095 | — |
| `nb_n_amounts` | 1.000 | 0.000 | numeric | 71 | 0.162 | — |
| `nb_n_amounts_other` | 1.000 | 0.000 | numeric | 71 | 0.162 | — |
| `nb_budget_per_count_log10` | 0.064 | 0.936 | numeric | 0 | 0.531 | 0.117 |
| `nb_count_log10` | 0.650 | 0.349 | numeric | 0 | -0.093 | 0.000 |
| `nb_has_count` | 1.000 | 0.000 | numeric | 0 | 0.125 | — |
| `nb_count_over_cap` | 1.000 | 0.000 | numeric | 0 | -0.076 | — |
| `nb_has_ratio` | 1.000 | 0.000 | numeric | 0 | 0.333 | — |
| `nb_has_burden` | 1.000 | 0.000 | numeric | 0 | 0.320 | — |
| `nb_rate_sum` | 0.403 | 0.597 | numeric | 3 | 0.103 | — |
| `nb_duration_basis` | 0.472 | 0.528 | categorical | 0 | — | — |
| `nb_has_duration` | 1.000 | 0.000 | numeric | 0 | 0.225 | — |
| `nb_duration_months` | 0.450 | 0.550 | numeric | 21 | 0.117 | — |
| `nb_cadence` | 0.136 | 0.864 | categorical | 0 | — | — |
| `nb_is_periodic` | 1.000 | 0.000 | numeric | 0 | 0.225 | — |
| `nb_unit_basis` | 0.480 | 0.520 | categorical | 0 | — | — |
| `nb_method_hits_n` | 1.000 | 0.000 | numeric | 0 | 0.125 | — |
| `nb_amount_type_source` | 1.000 | 0.000 | categorical | 0 | — | — |
| `nb_evidence_source` | 1.000 | 0.000 | categorical | 0 | — | — |
| `nb_src_len_log10` | 1.000 | 0.000 | numeric | 0 | -0.180 | 0.000 |
| `nb_body_len_log10` | 1.000 | 0.000 | numeric | 0 | -0.145 | 0.000 |
| `nb_scope_facility` | 1.000 | 0.000 | numeric | 0 | -0.015 | — |
| `nb_scope_rnd` | 1.000 | 0.000 | numeric | 0 | 0.065 | — |
| `nb_scope_labor` | 1.000 | 0.000 | numeric | 0 | -0.018 | — |
| `nb_scope_overseas` | 1.000 | 0.000 | numeric | 0 | -0.110 | — |
| `nb_scope_marketing` | 1.000 | 0.000 | numeric | 0 | -0.213 | — |
| `nb_scope_consult` | 1.000 | 0.000 | numeric | 0 | -0.127 | — |
| `nb_scope_cert` | 1.000 | 0.000 | numeric | 0 | -0.099 | — |
| `nb_scope_finance` | 1.000 | 0.000 | numeric | 0 | 0.169 | — |

## 3. 누수 감사 (지시서 4장)

| 점검 | 결과 |
|---|---|
| 타깃 제외 규칙이 지목한 후보 = 실제 타깃 | 1877/1877 (1.000) |
| 마스킹 본문에 숫자가 남은 행 | 0/1877 — 0 이어야 한다 |
| 새 금액 feature 가 타깃과 같은 값인 비율 | `nb_total_budget_log10` 0.064· `nb_budget_per_count_log10` 0.1167· `nb_count_log10` 0.0 |
| 위 비율을 0 으로 요구하지 않는 이유 | 1개사만 뽑는 사업은 총사업비와 기업당 한도가 실제로 같은 숫자다. 우연한 일치는 원문의 사실이고, 복사본이라면 **항상** 일치한다. 그래서 판정 기준은 0 이 아니라 '값이 있는 행의 절반 미만'으로 둔다. 구조적 차단(첫 줄)이 실제 방어선이다. |
| 쓰지 않기로 한 컬럼 | `support_cap` 타깃 그 자체 — per_recipient(basis=stated_cap)이 원문의 기업당 한도다· `support_per_recipient` 타깃 그 자체 (컬럼 이름만 다르다)· `support_per_project` amount_type=per_project 행의 타깃. support_unit 으로만 갈린다· `loan_limit` 융자 사업의 타깃. 별도 컬럼이 아니라 같은 자리에서 파싱된다· `amount_min` 타깃과 같은 금액 표현의 하한 — 사실상 복사본· `amount_unit_raw` 타깃 표현의 단위어. 자릿수를 그대로 읽는다 |
| 이미 M65 에 있던 것 | ['support_rate(=support_ratio)', 'self_burden_rate(=self_burden_ratio)', 'selected_count(=support_count)', 'project_duration', 'support_unit', 'support_method'] |
| 구간 경계 계산 입력 | fold train 의 y 만 (np.percentile(ytr, [33.3, 66.7])) |
| 구조적 차단이 타깃 후보를 정확히 지목 | 통과 |
| 마스킹 뒤 본문에 숫자가 남지 않음 | 통과 |
| 어떤 금액 feature 도 타깃의 복사본이 아님 (일치율 < 0.5) | 통과 |
| **판정** | **PASS** |

## 4. ablation — 단계별 단일 XGB MAE 와 Stage 1 (지시서 9장)

| 단계 | 신규 feature 수 | MAE(log10) | fold σ | 2배 이내 | 3배 이내 | ΔMAE vs A | 95% CI | wilcoxon p | fold승 | S1 acc | macro-F1 | Low recall | Mid recall | High recall | 반대끝 오분류 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| A 기존 M65 | 0 | 0.4117 | 0.0212 | 49.2% | 68.1% | — | — | — | — | 0.7315 | 0.7301 | 0.8216 | 0.6044 | 0.7713 | 0.0293 |
| B +budget/cap | 5 | 0.4083 | 0.0206 | 49.6% | 69.2% | -0.0035 | [-0.0075, +0.0005] | 0.0524 | 5/5 | 0.7363 | 0.7346 | 0.8282 | 0.6060 | 0.7776 | 0.0272 |
| C +selected_count | 8 | 0.4084 | 0.0203 | 48.8% | 69.1% | -0.0033 | [-0.0075, +0.0007] | 0.12 | 5/5 | 0.7357 | 0.7344 | 0.8282 | 0.6076 | 0.7744 | 0.0250 |
| D +rate/burden | 11 | 0.4069 | 0.0197 | 49.4% | 69.0% | -0.0048 | [-0.0092, -0.0009] | 0.0299 | 5/5 | 0.7384 | 0.7370 | 0.8298 | 0.6108 | 0.7776 | 0.0250 |
| E +duration/주기 | 16 | 0.4023 | 0.0224 | 50.6% | 69.7% | -0.0094 | [-0.0143, -0.0047] | 0.00142 | 5/5 | 0.7342 | 0.7333 | 0.8265 | 0.6187 | 0.7603 | 0.0272 |
| F +unit/method/항목 | 30 | 0.4003 | 0.0263 | 49.9% | 69.5% | -0.0114 | [-0.0173, -0.0055] | 0.000437 | 4/5 | 0.7517 | 0.7505 | 0.8380 | 0.6313 | 0.7886 | 0.0250 |
| G +본문 텍스트 | 30 | 0.3719 | 0.0179 | 53.0% | 72.3% | -0.0398 | [-0.0483, -0.0313] | 1.11e-17 | 5/5 | 0.7848 | 0.7847 | 0.8331 | 0.6978 | 0.8249 | 0.0197 |

### fold별 MAE

| 단계 | fold 0 | fold 1 | fold 2 | fold 3 | fold 4 |
|---|---:|---:|---:|---:|---:|
| A 기존 M65 | 0.4201 | 0.3811 | 0.4359 | 0.4288 | 0.3927 |
| B +budget/cap | 0.4198 | 0.3797 | 0.4266 | 0.4281 | 0.3872 |
| C +selected_count | 0.4200 | 0.3782 | 0.4249 | 0.4287 | 0.3904 |
| D +rate/burden | 0.4176 | 0.3780 | 0.4250 | 0.4252 | 0.3886 |
| E +duration/주기 | 0.4179 | 0.3708 | 0.4217 | 0.4218 | 0.3794 |
| F +unit/method/항목 | 0.4263 | 0.3631 | 0.4196 | 0.4186 | 0.3739 |
| G +본문 텍스트 | 0.3947 | 0.3499 | 0.3814 | 0.3819 | 0.3517 |

> baseline(비교군 중앙값) MAE 0.5223. M65 공표치는 MAE 0.4117 / 2배 이내 49.2% 이고,
> 위 표의 A 행이 그 재현입니다. M67 의 Stage 1 은 73.2% 였습니다.

## 5. 실제 구간별 MAE

| 구간 | n | A 기존 M65 | B +budget/cap | C +selected_count | D +rate/burden | E +duration/주기 | F +unit/method/항목 | G +본문 텍스트 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Low | 611 | 0.5036 | 0.4997 | 0.4979 | 0.4986 | 0.4893 | 0.4826 | 0.4576 |
| Mid | 632 | 0.3187 | 0.3182 | 0.3190 | 0.3181 | 0.3128 | 0.3148 | 0.2755 |
| High | 634 | 0.4159 | 0.4099 | 0.4114 | 0.4069 | 0.4078 | 0.4062 | 0.3856 |

## 6. Stage 1 confusion — A 대 G +본문 텍스트

**A 기존 M65** (accuracy 0.7315)

| 실제 \ 예측 | Low | Mid | High |
|---|---:|---:|---:|
| Low | 502 | 89 | 20 |
| Mid | 151 | 382 | 99 |
| High | 35 | 110 | 489 |

**G +본문 텍스트** (accuracy 0.7848)

| 실제 \ 예측 | Low | Mid | High |
|---|---:|---:|---:|
| Low | 509 | 90 | 12 |
| Mid | 112 | 441 | 79 |
| High | 25 | 86 | 523 |

## 6-2. 개선이 어느 코호트에서 나왔는가 (텍스트 층 누수 점검)

| 축 | 값 | n | A MAE | G +본문 텍스트 MAE | Δ |
|---|---|---:|---:|---:|---:|
| cohort | bizinfo | 901 | 0.4710 | 0.4182 | -0.0528 |
| cohort | taxonomy | 976 | 0.3570 | 0.3292 | -0.0278 |
| evidence_source | api_summary | 157 | 0.4416 | 0.4362 | -0.0054 |
| evidence_source | document | 744 | 0.4772 | 0.4144 | -0.0628 |
| evidence_source | scale_text | 976 | 0.3570 | 0.3292 | -0.0278 |

> taxonomy 의 본문은 `purpose`·`content`·`target_text` 라 **금액이 아예
> 없는 텍스트**입니다. 그쪽에서도 개선이 나온다는 것이, 이 이득이
> 마스킹을 뚫고 남은 금액 신호가 아니라 **사업 내용 자체**라는 근거입니다.
> 공고문 전체를 본문으로 쓰는 `document` 행에서 개선이 가장 큰 것은
> 그쪽이 원래 제목 말고는 텍스트가 없던 자리이기 때문입니다.

## 7. 진단용 상한 — 타깃 표현의 단위어를 넣으면 (모델 아님)

```text
Stage 1 accuracy  0.7757   (보강 최고 0.7848 / 기존 0.7315)
macro-F1          0.7749
반대끝 오분류      0.0123
```

> M67 의 oracle 과 같은 성격입니다 — **상한 진단이지 모델이 아닙니다.**
> 단위어(만원/백만원/억원)는 타깃이 적힌 그 문장에서 자릿수를 그대로 읽는
> 값이라 지시서 4장 '사용 금지'에 걸립니다. 이 줄이 말하는 것은 하나입니다 —
> **금액구간을 아는 정보는 원문의 금액 표현 안에만 있고, 사업 설계 정보
> (예산·건수·비율·기간·항목)에는 그만큼 남아 있지 않다.**

## 8. 엄격 split (normalized_title)

| 단계 | MAE(log10) | Stage 1 accuracy |
|---|---:|---:|
| A 기존 M65 | 0.4304 | 0.7032 |
| G +본문 텍스트 | 0.3931 | 0.7565 |

## 9. Stage 1 게이트 (지시서 8장)

```text
기존 Stage 1 accuracy   0.7315
보강 Stage 1 accuracy   0.7848   (G +본문 텍스트)
기준  Case A >= 0.85 / Case B >= 0.80 / Case C >= 0.75
판정  Case C — 개선 폭 부족 — expert 구조 재도전 보류
```

## 10. expert 구조 재도전 — 실행하지 않음

지시서 13장 중단 조건에 걸립니다 — **새 feature 를 넣어도 Stage 1 accuracy 가
0.80 미만**입니다(0.7848). M67 이 이미 측정한 대로 routing 실패 행의 손해가
성공 행의 이득을 넘기 때문에, 이 정확도로 expert 를 다시 켜면 결과는
M67 의 재연(routed 0.4368)입니다.

## 10-2. 서빙 영향 — 자동으로 판정할 수 없는 조건 (지시서 12장 9번)

지시서의 승격 조건 9번은 **serving 복잡도 대비 개선 폭이 충분한가**입니다.
이 칸은 수치로 닫히지 않으므로 사실만 적고 판단은 남깁니다.

```text
현재 계약  m56_m2_canonical.SERVING_FIELDS — 구조화 필드만. 텍스트는 title 하나
새 요구    사업목적·사업내용·지원대상 본문(또는 공고문 원문) 문자열 1개
           B~F 의 구조화 feature 도 이 텍스트에서 뽑는다 — 층 전체가 텍스트를 요구한다
모델 크기  fold 당 TF-IDF+SVD 가 하나 더 (제목용과 같은 규격, 64차원)
얻는 것    MAE 0.4117 -> 0.3719 (9.7%)  ·  엄격 split 0.4304 -> 0.3931
```

> 사업을 **설계하는 시점**의 조회라면 기획안 본문은 이미 손에 있는 입력이므로
> 추가 부담이 크지 않습니다. 반대로 구조화 필드만 폼으로 받는 화면이라면
> 새 입력칸이 하나 늘어납니다. 텍스트 없이 갈 경우의 차선은 **F 단계 (MAE 0.4003)** 인데,
> F 의 구조화 feature 도 원문에서 뽑은 값이라 텍스트 없이는 결국 A 로 돌아갑니다.

## 11. 재현성 / 승격 점검표 (지시서 12장)

| 조건 | 결과 |
|---|---|
| 1. program_stem OOF MAE 가 A 보다 명확히 개선 | 통과 |
| 2. normalized_title 엄격 split 에서도 같은 방향 | 통과 |
| 3. 5개 fold 대부분에서 개선 (4 이상) | 통과 |
| 4. paired 95% CI 가 0 아래 | 통과 |
| 5. Low/High 구간 오차 감소 | 통과 |
| 6. routing 반대 끝 오분류 감소 | 통과 |
| 7. leakage audit PASS | 통과 |
| 8. 재현성 PASS | 통과 |
| 9. 1차 목표 MAE < 0.35 | 미달 |
| 같은 seed 재실행 OOF 일치 | A 기존 M65 True / G +본문 텍스트 True |

## 결론

```text
M65 canonical            MAE 0.4117   Stage1 0.7315
원천 feature 보강 최고    MAE 0.3719   Stage1 0.7848  (G +본문 텍스트)
단위어 상한(모델 아님)    —           Stage1 0.7757

목표  1차 MAE < 0.35 / 최종 < 0.30
판정: 승격 후보 (M65 대체)
```