# M83 / M84 — Typed Proximity Relation · Section-aware Proximity

> 질문: **M82 가 연 '수치-맥락' 축을 관계 type(M83)과 문서 섹션(M84)으로 더 정교하게 만들면 M82/P3(0.3518)를 다시 이기는가?**

## 0. 후보

```text
B0  M82/P3 (새 baseline)
R1  B0 + M83 typed relation
R2  B0 + M84 section-aware
R3  B0 + M83 + M84
```

## 1. feature 진단

M83 relation 성립률: `{'support_rate': 0.0554, 'self_burden_rate': 0.4997, 'selected_count': 0.1955, 'duration_month': 0.0597, 'duration_year': 0.1406}`

M84 섹션 검출률: `{'scale': 0.3527, 'content': 0.3479, 'target': 0.3815, 'period': 0.2904, 'condition': 0.6068, 'apply': 0.3815, 'select': 0.276, 'exclude': 0.2302, 'purpose': 0.2195}`

헤더가 있는 문서: **87.8%**

leakage — relation 값 target 일치 **0건** · TF-IDF 숫자 잔존 **0행** · 금액은 거리(위치)만 — 값·자릿수 미사용


## 2. 결과 [program_stem]

| 변형 | MAE | 2x | 3x | Δ vs B0 | 95% CI | fold승 |
|---|---:|---:|---:|---:|---|---:|
| B0 (M82/P3) | 0.3518 | 57.2% | 74.4% | — | — | — |
| R1 | 0.3524 | 56.8% | 74.2% | +0.0005 | [-0.0023, +0.0032] | 2/5 |
| R2 | 0.3534 | 56.8% | 74.3% | +0.0016 | [-0.0012, +0.0045] | 2/5 |
| R3 | 0.3509 | 56.9% | 74.8% | -0.0009 | [-0.0040, +0.0021] | 3/5 |

## 3. 결과 [normalized_title] (strict)

```text
B0  0.3751
R1  0.3765
R2  0.3745
R3  0.3741
```

## 4. 승격 점검표 — 대상 R3

- [x] 1. primary MAE < 0.3518 (M82/P3)
- [x] 1b. 같은 fold B0 보다 낮다
- [x] 2. strict MAE <= 0.3751
- [ ] 3. 4/5 이상 fold 개선
- [ ] 4. paired CI < 0
- [x] 5. leakage audit PASS
- [ ] 6. 실질 기준 ΔMAE <= -0.002
- [x] 7. reproducibility PASS

## 판정

```text
현행 유지 — M82/P3
```
