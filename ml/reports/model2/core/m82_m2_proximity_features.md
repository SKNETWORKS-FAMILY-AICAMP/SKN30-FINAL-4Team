# M82 — 수치-문맥 근접(Proximity) feature

> 질문: **전체본문 SVD 가 놓친 수치-의미단어 근접 관계를 명시 feature 로 복원하면 M73 0.3563 을 이기는가?**

## Exp0 — 패턴 커버리지

| 항목 | 값 |
|---|---:|
| prox_support_rate | 3.7% |
| prox_self_burden_rate | 46.4% |
| prox_selected_count | 10.7% |
| prox_duration_months | 12.6% |

window sweep(진단용, 승격 근거 아님): `{'20': {'coverage_support_rate': 0.0341, 'ambiguity_rate': 0.3703}, '30': {'coverage_support_rate': 0.0373, 'ambiguity_rate': 0.3703}, '50': {'coverage_support_rate': 0.0373, 'ambiguity_rate': 0.3703}}`

target(원) leakage 검사: 값 일치 **0건**, context 숫자 잔존 **0행** (모두 0 이어야 통과)


## P0~P3 결과 [program_stem]

| 변형 | MAE | 2x | 3x | Δ vs P0 | 95%% CI | fold승 |
|---|---:|---:|---:|---:|---|---:|
| P0 (M73 baseline) | 0.3563 | 56.4% | 74.2% | — | — | — |
| P1 | 0.3559 | 55.7% | 74.4% | -0.0004 | [-0.0039, +0.0030] | 3/5 |
| P2 | 0.3544 | 57.1% | 74.5% | -0.0019 | [-0.0053, +0.0016] | 3/5 |
| P3 | 0.3518 | 57.2% | 74.4% | -0.0044 | [-0.0083, -0.0003] | 5/5 |

## 승격 점검표 — 대상 P3

- [x] 1. OOF MAE < 0.3563
- [x] 2. strict 에서도 개선
- [x] 3. 최소 4/5 fold 개선
- [x] 4. CI 가 0 아래
- [x] 5. target amount leakage 0건
- [x] 6. parser ambiguity 과도하지 않음(<0.5)
- [x] 7. 실질기준 ΔMAE <= -0.003
- [x] 8. reproducibility

## 판정

```text
승격 후보 (P3)
```
