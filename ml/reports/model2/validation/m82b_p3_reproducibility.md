# M82-B — P3 재현성 3-run

> 질문: **P3 를 Model 2 canonical 후보로 확정해도 되는가 — 세 번의 독립 학습이 같은 숫자를 내는가?**

## run 별 결과

| run | P0 MAE | P3 MAE | Δ | 95% CI | fold승 |
|---|---:|---:|---:|---|---:|
| run1 | 0.356251 | 0.351813 | -0.004400 | [-0.0083, -0.0003] | 5/5 |
| run2 | 0.356251 | 0.351813 | -0.004400 | [-0.0083, -0.0003] | 5/5 |
| run3 | 0.356251 | 0.351813 | -0.004400 | [-0.0083, -0.0003] | 5/5 |

## 점검표

- [x] OOF prediction exact match
- [x] MAE exact match
- [x] CI exact match
- [x] feature dimension exact match
- [x] feature ordering exact match
- [x] M82 공표치 재현 (P0 0.3563 / P3 0.3518)
- [x] fold 분할이 run 간 동일

## 판정

```text
PASS — P3 canonical 후보 확정 가능
```


fold 별 설계행렬 차원 (P0 -> P3): `{'fold0': {'P0': 171, 'P3': 211}, 'fold1': {'P0': 171, 'P3': 211}, 'fold2': {'P0': 171, 'P3': 211}, 'fold3': {'P0': 171, 'P3': 211}, 'fold4': {'P0': 171, 'P3': 211}}`
