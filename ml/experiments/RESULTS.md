# experiments/ — 돌려 봤고 채택되지 않은 것 (machine-learning 브랜치)

**여기 있는 코드는 전부 기각·대체됐다.** 남긴 이유는 하나다 — 무엇을 이미
재 봤는지 모르면 같은 실험을 다시 하게 된다. 리포트 실물은 `../reports/` 에
평면으로 있다.

산출 parquet 중 아무도 읽지 않는 19종은 제거했다(`design_anomaly{,_tuned,_alerts}`·
`design_clusters{,_tuned,_conditional,_final}`·`inference_sample50`·
`m1_calibrated_oof`·`m37_synthetic`·`m38_vector_direction`·`m43_extremity_pool`·
`coverage_accuracy_sweep`·`m45_cohort_reference`·`m45`/`m53`/`m63_oof_predictions`·
`m46_yoy_{index,pairs}`). 수치는 리포트 json 에 남아 있고 스크립트로 다시 만들 수 있다.

## 모델 1 — 기각된 접근

| 실험 | 결과 | 기각 사유 |
|---|---|---|
| `m24_m1_calibrate` | 커버리지 70.7% 에서 정확도 0.9164 | 보정에 학습데이터 **30% 소모**. 무보정 기준선을 못 넘음 |
| `m25_m1_features` | TF-IDF feature 고도화 | 개선폭이 분산 안에 들어옴 |
| `m03`·`m04`·`m05`·`m06`·`m07`·`m09` | 입력정합·누수·MIN_SUPPORT·사람 대조·커버리지 곡선 | ML 기준선 시절 진단. 결론은 deep-learning 의 M54/M57 로 승계 |
| `m28_m1_external_pool` | 외부셋 41 → 150건 후보 추출 | 확장 자체는 채택(→ M29). 스크립트는 1회성 |

## 모델 2 — 기각된 접근

| 실험 | 결과 | 기각 사유 |
|---|---|---|
| `m11`·`m15`·`m18`·`m21` (설계유형 군집) | grant 내부 6개 유형만 유효 | `service`·`other` 는 **기존 분야 분류를 다시 그린 것**으로 판명 |
| `m19`·`m22`·`m26` (예측구간) | Conformal · Mondrian CQR · 실용성 판정 | 구간이 실무 판단을 좁히지 못함. CQR 자체는 M56 에 흡수 |
| `m46_m2_yoy_lookup` | 전년 대비 조회 | 연도 간 수집 설계가 달라 직접 비교 불가(전처리결과서 5.4) |
| `m52_m2_error_analysis` | 고오차 사례 분석 | 진단. 대응은 M53 → M56/M65 로 승계 |
| `m53_m2_improve` | 개선 측정 (11.9% → 21.8%) | 측정 단계. canonical 승격은 M56/M65 |

## 모델 3/4 — 기각된 접근

모델 4(설계 이상탐지)는 별도 모델이 아니라 **모델 3으로 흡수**됐다.

| 실험 | 결과 | 기각 사유 |
|---|---|---|
| `m16`·`m20`·`m23` (모델 4 튜닝·경고정책·라벨셋) | — | 모델 4 자체가 모델 3 으로 통합 |
| IsolationForest (`m13` 내부) | 합성 이상치 회수율 **8.3%** | 결측 지시자·희귀 범주 빈도에 점수가 지배당함 |
| OneClassSVM (`m30`·`m36`) | 첫 버전 | 실제 라벨 hold-out 에서 무너짐 |
| `m35_m3_supervised` | 지도학습 전환 | 라벨 신뢰도 자체가 문제라 방향을 신호 안정성으로 틀었다 |
| `m39_m3_final`·`m40_m3_vector_grid`·`m42_m3_second_eval` | 구조 탐색·격자 탐색·2차 평가 | M44 로 대체 |
| `m17`·`m31`·`m32`·`m48`·`m49`·`m51` | 튜닝·파서 감사/수정·안정성·stress·attribution | 진단·검증. **M31 이 찾은 파서 버그 4종은 M32 에서 `amount_parser` 로 승격됨** — 감사와 수정을 따로 커밋한 이유는 자기증명을 피하기 위해서다 |

### 설계 희소성 개선 실험 4종 (M58~M61) — **전부 현행 유지**

지시서가 지정한 4종을 다 돌렸고, **필수조건을 깨지 않으면서 현행을 넘는 후보가
없었다.** 이것이 모델 3 모델링을 종료한 근거다.

| 실험 | 시도 | 판정 |
|---|---|---|
| `m58_m3_cohort_refine` | 비교군 축 정교화 | `+지원단위` 는 동질성 개선하나 얇은 비교군 195 → 224. `+기관계열` 은 **ROC 최고(0.8271)에도 안정성 악화로 reject** |
| `m59_m3_prototype` | multi-prototype 대표벡터 | k=2/3 은 재현성 악화. mean 유지 |
| `m60_m3_fallback` | 얇은 비교군 fallback | `MIN_COHORT` 30 이상이면 전역 fallback 79 → 197건 급증. 20 유지 |
| `m61_m3_scaling` | scaling / distance | robust 는 Top30 의 63~67% 교체. standard + Euclidean 유지 |
| `m50_m3_shrinkage` | 얇은 비교군 shrinkage | 순위 안정성 개선 없음 — 미채택 |

## 그 외

| 실험 | 판정 |
|---|---|
| `m10_design_coverage` | 모델 2~4 착수 전 feature coverage 게이트. 통과 후 역할 종료 |

---

## 다시 하지 않기로 한 것

- 모델 3 의 **새 이상탐지 알고리즘 탐색** — 남은 개선 여지가 모델 바깥(얇은
  비교군 표본 수, 파서 품질)에 있다는 것을 M58~M61 로 **측정해서** 확인했다.
- Product Boundary 밖 기능 (정책 타당성 · 예산 적정성 · 법률 적합성 판정).
