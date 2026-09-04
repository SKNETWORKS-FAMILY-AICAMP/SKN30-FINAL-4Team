# M72 — 본문 semantic embedding

> 질문: **M69 에서 확인된 본문 텍스트 신호를 TF-IDF/SVD 가 아니라 semantic
> sentence embedding 으로 표현하면, MAE 0.3719 을 의미 있게 낮출 수 있는가?**

## 0. 같은 조건 / 바뀐 것

```text
dataset  data\processed\design_features_v2.parquet  (1877행)
sha256   eced88f6767e2e2460a05812dd8e9cd39990937e4c1a2a8363e02d114bfdc7f4
고정      모델(XGB_POINT) · 구조화 feature(M69 G 단계 전체) · 제목 SVD64 · 마스킹 규칙
바뀐 것  본문을 어떤 벡터로 만드는가 (TF-IDF/SVD vs sentence embedding)
```

## 1. baseline 재현 (지시서 9장 Phase 0)

```text
OOF MAE   0.3719   (기대 0.3719)
Within 2x 53.0%   (기대 53.0%)
Within 3x 72.3%   (기대 72.3%)
M69 저장 OOF 와 행 단위 일치: True
```

## 2. embedding 생성 (지시서 6·7장)

| 인코더 / pooling | 차원 | 생성 시간(초) |
|---|---:|---:|
| ko-sroberta-multitask|head | 768 | 2 |
| ko-sroberta-multitask|chunk_mean | 768 | 7 |
| ko-sroberta-multitask|section | 768 | 4 |
| KoSimCSE-roberta-multitask|head | 768 | 4 |
| KoSimCSE-roberta-multitask|chunk_mean | 768 | 8 |
| KoSimCSE-roberta-multitask|section | 768 | 7 |

> 인코더는 전부 **frozen** 입니다(지시서 12장). `ko-sroberta-multitask` 의 `max_seq_length` 가
> 128 토큰이라 공고문 본문(중앙 2,594자)을 한 번에 넣으면 앞부분만 보고
> 잘립니다. 그래서 덩어리를 L2 정규화한 뒤 평균 — 긴 덩어리가 노름으로 문서 벡터를 지배하지 않게

## 3. 누수 점검 (지시서 2장)

| 점검 | 결과 |
|---|---|
| 임베딩 입력 | M69 마스킹 본문 그대로 — 이 실험은 마스킹을 다시 하지 않는다 |
| 마스킹 본문에 십진 숫자가 남은 행 | 0 / 1877 — 0 이어야 한다 |
| 마스킹 본문에 금액 표현이 남은 행 | 0 / 1877 — 0 이어야 한다 |
| 원문자(①②③) 만 있는 행 | 730 / 1877 — 문단 번호라 자릿수 정보가 없다. 누수 아님 |
| 인코더 | frozen pretrained. y 를 본 적이 없다 (fine-tuning 없음) |
| 차원 축소기 | fold train 에서만 fit (지시서 8장) |
| 타깃을 임베딩 생성에 사용 | 없음 |
| 임베딩 생성 장치 | GPU(RTX 4090) forward. CPU 대비 최대 절대차 1.8e-06 · 코사인 유사도 최소 0.9999998 로 실측 — 값이 아니라 속도만 다르다 |
| XGBoost 장치 | CPU tree_method=hist 그대로. 트리를 GPU 로 옮기면 histogram binning 이 달라져 M65 부터의 비교가 깨진다 |
| **판정** | **PASS** |

## 4. 전체 결과

| 설정 | 규격 | MAE | fold σ | 2배 이내 | 3배 이내 | ΔMAE vs M69 | 95% CI | wilcoxon p | fold승 | taxonomy | bizinfo | 열 수 | 학습(초) |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| M69 baseline | TF-IDF + semantic 없음 | 0.3719 | 0.0179 | 53.0% | 72.3% | — | — | — | — | 0.3292 | 0.4182 | 171 | 75 |
| A1 semantic만 | TF-IDF 없음 + ko-sroberta-multitask/chunk_mean/raw | 0.3754 | 0.0204 | 53.0% | 72.1% | +0.0034 | [-0.0048, +0.0113] | 0.501 | 1/5 | 0.3310 | 0.4234 | 875 | 302 |
| B1 TF-IDF+semantic | TF-IDF + ko-sroberta-multitask/chunk_mean/raw | 0.3697 | 0.0173 | 53.1% | 72.4% | -0.0022 | [-0.0092, +0.0045] | 0.564 | 4/5 | 0.3274 | 0.4155 | 939 | 470 |
| C1 head | TF-IDF + ko-sroberta-multitask/head/raw | 0.3752 | 0.0198 | 53.1% | 71.7% | +0.0032 | [-0.0035, +0.0102] | 0.509 | 2/5 | 0.3314 | 0.4226 | 939 | 316 |
| C2 section | TF-IDF + ko-sroberta-multitask/section/raw | 0.3720 | 0.0150 | 53.1% | 72.0% | +0.0001 | [-0.0067, +0.0068] | 0.863 | 3/5 | 0.3277 | 0.4200 | 939 | 335 |
| D1 SVD128 | TF-IDF + ko-sroberta-multitask/section/128 | 0.3719 | 0.0143 | 53.3% | 71.7% | -0.0001 | [-0.0048, +0.0046] | 0.78 | 1/5 | 0.3285 | 0.4189 | 299 | 113 |
| D2 SVD64 | TF-IDF + ko-sroberta-multitask/section/64 | 0.3711 | 0.0173 | 53.1% | 72.2% | -0.0008 | [-0.0054, +0.0039] | 0.348 | 3/5 | 0.3255 | 0.4205 | 235 | 91 |
| E1 KoSimCSE | TF-IDF + KoSimCSE-roberta-multitask/section/64 | 0.3691 | 0.0179 | 53.1% | 72.8% | -0.0029 | [-0.0082, +0.0024] | 0.319 | 5/5 | 0.3294 | 0.4120 | 235 | 93 |

### fold별 MAE

| 설정 | fold 0 | fold 1 | fold 2 | fold 3 | fold 4 |
|---|---:|---:|---:|---:|---:|
| M69 baseline | 0.3947 | 0.3499 | 0.3814 | 0.3819 | 0.3517 |
| A1 semantic만 | 0.3959 | 0.3567 | 0.3838 | 0.3945 | 0.3459 |
| B1 TF-IDF+semantic | 0.3887 | 0.3522 | 0.3812 | 0.3806 | 0.3457 |
| C1 head | 0.3900 | 0.3520 | 0.3950 | 0.3887 | 0.3502 |
| C2 section | 0.3910 | 0.3592 | 0.3820 | 0.3775 | 0.3504 |
| D1 SVD128 | 0.3809 | 0.3543 | 0.3856 | 0.3839 | 0.3547 |
| D2 SVD64 | 0.3817 | 0.3498 | 0.3850 | 0.3887 | 0.3503 |
| E1 KoSimCSE | 0.3877 | 0.3441 | 0.3812 | 0.3815 | 0.3507 |

## 5. 노이즈 대역 — 승자를 어떻게 읽어야 하는가

```text
baseline MAE              0.3719
후보 7개의 ΔMAE 범위       -0.0028 ~ +0.0035
95% CI 가 0 을 가로지른 후보  7 / 7
0 을 넘지 않은 후보(유의)     0 / 7
```

## 6. phase 진행 (지시서 9장)

| phase | 물려받은 승자 | 후보 | 새 승자 | 교체 |
|---|---|---|---|---|
| Phase 1 — semantic replacement | M69 baseline | A1 semantic만 | M69 baseline | 아니오 |
| Phase 2 — hybrid | M69 baseline | B1 TF-IDF+semantic | B1 TF-IDF+semantic | 예 |
| Phase 3 — pooling | B1 TF-IDF+semantic | C1 head, C2 section | B1 TF-IDF+semantic | 아니오 |
| Phase 4 — embedding 차원 | B1 TF-IDF+semantic | D1 SVD128, D2 SVD64 | B1 TF-IDF+semantic | 아니오 |

## 7. 엄격 split (normalized_title)

| 설정 | strict MAE | taxonomy | bizinfo | fold별 |
|---|---:|---:|---:|---|
| M69 baseline | 0.3931 | 0.3559 | 0.4334 | 0.3724, 0.3940, 0.4114, 0.3853, 0.4026 |
| B1 TF-IDF+semantic | 0.3954 | 0.3573 | 0.4367 | 0.3859, 0.3876, 0.4082, 0.3920, 0.4034 |
| E1 KoSimCSE | 0.3901 | 0.3544 | 0.4287 | 0.3739, 0.3887, 0.4130, 0.3854, 0.3895 |

## 8. 서빙 비용 (지시서 10·11장)

```text
인코더 artifact       443.5 MB   (M69 는 인코더가 필요 없다)
fold 적합 artifact    16.7 MB  (baseline 16.5 MB)
인코딩 지연           307.1 ms/문서 · 280.7 ms/덩어리  (CPU)
```

## 9. 승격 점검표 (지시서 11장) — 대상: E1 KoSimCSE

| 조건 | 결과 |
|---|---|
| 1. OOF MAE 가 0.3719 보다 감소 | 통과 |
| 2. strict split 에서도 같은 방향 | 통과 |
| 3. 5개 fold 중 4개 이상 개선 | 통과 |
| 4. paired CI 가 0 아래 | 미달 |
| 5. taxonomy / bizinfo 양쪽 다 악화되지 않음 | 미달 |
| 6. leakage audit PASS | 통과 |
| 7. reproducibility PASS | 통과 |
| 8. serving latency / artifact 증가가 감당 가능 | 통과 |
| 같은 seed 재실행 OOF 일치 | True |

## 10. 최종 판정 (지시서 14장)

```text
Case B — 통계적으로 일관되면 serving 비용 대비 승격 여부 검토

M69 baseline   MAE 0.3719
M72 최고       MAE 0.3691   (E1 KoSimCSE)

목표  1차 MAE < 0.35 -> 미달 / 최종 < 0.30 -> 미달
판정: 현행 유지 (M69)
```

### fine-tuning 으로 갈 것인가 (지시서 12장)

> frozen embedding 에서 명확한 개선이 있을 때만 검토한다(지시서 12장). 개선이 없으면 fine-tuning 으로 가지 않고 semantic 축을 종료한다.
