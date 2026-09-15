# M81 — 공통 고오차 행 데이터 무결성 감사

> 질문: **여러 모델이 공통으로 크게 틀리는 행에 실제 라벨·금액 의미·단위 오류가 존재하는가, 교정하면 M73 0.3563 을 이기는가?**

## 0. 공통 고오차 행 정의

```text
4개 모델(M73 soft/ordinal_xgb, M79 XGB/LGBM/CatBoost) residual 절대값 percentile 이 모두 tier 이상이고 부호가 전부 같음
```

| tier | n(공통) | n(M73 단독) |
|---|---:|---:|
| top10% | 161 | 188 |
| top5%  | 82 | 94 |

## 1/2. 감사

전체 라벨 분포: `{'CORRECT': 0.9473, 'FIXABLE': 0.0234, 'AMBIGUOUS': 0.0144, 'NO_EVIDENCE': 0.0139, 'SEMANTIC_MISMATCH': 0.0011}`


**hard_top10** (n=161) 오류율 0.186 (전체 0.053) FIXABLE 15


**hard_top5** (n=82) 오류율 0.256 (전체 0.053) FIXABLE 9


## 3. 교정 — 확정 FIXABLE 44행


## 6. M81-B — 재학습 (fixed-eval = 라벨 CORRECT 행만, 공정 비교)

| split | V1 MAE | V2 MAE | Δ | 95%% CI | fold승 |
|---|---:|---:|---:|---|---:|
| program_stem | 0.3363 | 0.3373 | +0.0011 | [-0.0032, +0.0051] | 1/5 |
| normalized_title | 0.3558 | 0.3617 | +0.0058 | [+0.0014, +0.0106] | 0/5 |

## 승격 점검표

- [x] 1. 실제 라벨 오류가 유의미하게 발견됨(FIXABLE>=5)
- [x] 2. 교정 후 fixed-eval OOF MAE < 0.3563
- [ ] 3. strict 에서도 개선
- [ ] 4. 최소 4/5 fold 개선
- [ ] 5. CI 가 0 아래
- [x] 6. 수정 근거가 원문에 존재
- [x] 7. score-driven label cleaning 아님
- [x] 8. leakage PASS
- [x] 9. fold 분할이 V1/V2 간 동일(재현 전제)

## 판정

```text
현행 유지 — M73 원본
```
