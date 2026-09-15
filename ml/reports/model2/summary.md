# Model 2 — 지원규모(기업당 한도) 회귀 + 상대 위치

> 상세 서술은 `../../docs/02_모델_1_2_3_성능_결과서.md` 2장.
> 기각 실험의 전체 기록은 `archive/EXPERIMENTS_REJECTED.md`.

```text
M65  ->  M69  ->  M73  ->  M82/P3   (canonical)
```

**최종 canonical — M82/P3** · Primary OOF MAE **0.3518** (strict 0.3751)
· 2배 이내 57.2% / 3배 이내 74.4% · feature 211열 · 모델 6개(global 1 + 구간 expert 3 + ordinal 이진 2)

## core — 성능을 만든 흐름

| 실험 | 무엇을 바꿨나 | OOF MAE(log10) | 근거 |
|---|---|---:|---|
| **M65** | 초기 XGBoost baseline | 0.4117 | `core/m65_m2_canonical_v2.{json,md}` |
| **M69** | 원천 feature 층 + 마스킹 본문 SVD64 | 0.3719 | `core/m69_m2_source_features.{json,md}` |
| **M73** | ordinal soft routing | 0.3563 | `core/m73_m2_routing_improvement.{json,md}` |
| **M82/P3** | explicit proximity + masked proximity TF-IDF/SVD | **0.3518** | `core/m82_m2_proximity_features.{json,md}` |

M82 대 M73: **-0.0045 · fold승 5/5 · 95% CI [-0.0083, -0.0003]**.

M82 가 중요한 이유 — M67 이후 열었던 축(routing · residual/MoE · semantic embedding
· local cohort · calibration · 곡선회귀 · expert 튜닝 · 이종 모델군 · 앙상블 ·
hierarchical prior · label correction)이 전부 M73 을 넘지 못했는데, **모델 구조가
아니라 입력 표현을 바꿔** 처음으로 유의하게 이겼다. 병목은 XGBoost 가 아니라
**기존 feature 가 수치와 의미의 결합 관계를 표현하지 못한 것**이었다.

P1(명시 수치)과 P2(수치 주변 문맥)는 각각으로는 미달이고 **결합했을 때만** 통과했다.

## validation — 그 결과를 믿을 수 있는가

| 실험 | 확인한 것 | 결과 |
|---|---|---|
| M55 | 제목 텍스트 누수 경로 | 걸치는 계열 0개 |
| M71 | target 품질 감사 | 파싱 오류 4.9% — 교정해도 2/5 fold |
| M74 | 학습곡선 | MAE = c·n^−0.194 (R²=0.9925) — 남은 병목은 **관측 수** |
| M78 | 후처리 / calibration | 보정 여지 약함 |
| M79 | XGB / LGBM / CatBoost / 앙상블 | residual 상관 ≈0.99 — 앙상블 가치 낮음 |
| M81 | 공통 hard-case 라벨 감사 | 고오차 행 오류율 18.6~25.6% (전체 5.3%) — **실재하나 성능을 못 올림** |
| M82-B | 재현성 3-run | OOF·MAE·CI·차원·순서 **exact match** |
| M82-C | serving smoke | 신규 문서 end-to-end **8/8 PASS** |

M81 의 결론이 중요하다 — M79 의 0.99 residual 상관은 **라벨 오류가 주병목이라는
뜻이 아니라**, 네 모델이 같은 feature 정보로 같은 행에서 같은 방향으로 헤매는
것이다. 정보가 없는 것이지 정답이 틀린 것이 아니다.

## archive — 승격되지 않은 것

구세대 canonical(M45 · M56 · M63)과 M67~M85 의 미승격 실험이 있다. 마지막 네 건:

| 실험 | 축 | Primary | 결과 |
|---|---|---:|---|
| M83 | typed proximity relation | 0.3524 | 2/5 fold, CI 0 포함 |
| M84 | section-aware proximity | 0.3534 | 2/5 fold, CI 0 포함 |
| M83+M84 | 결합 | 0.3509 | 3/5 fold, CI 0 포함 |
| M85 | table/layout-aware (T5) | 0.3498 | **5/5 fold** 인데 CI [-0.0047, +0.0006] |

M85 는 승격 기준 9개 중 8개를 통과하고 **CI 하나에서** 걸렸다. 셋이 **같은 형태로**
실패했다는 것이 결론이다 — MAE 는 P3 보다 낮은데 CI 가 0 을 못 넘는다. 남은 신호가
없는 게 아니라 **n=1,877 의 통계적 분해능보다 작다.** M74 의 학습곡선이 같은 이야기다.

> **구조 기반 feature engineering 은 현재 데이터에서 소진.** 다음은 모델이 아니라
> 데이터다 — 신규 표본 · evidence-backed label · 구조 보존된 원문.

M85 Exp0 이 그 지점을 하나 짚었다: **PDF 는 표 구조가 85% 살아 있는데 F05 가 PDF 를
버려 모델링 프레임(1,877행)에 한 행도 없다.** HWP/HWPX 는 추출 텍스트에 표가 0%
남는다. 새 문서를 모으기 전에 이 경로부터 여는 것이 비용 대비 효과가 크다.

## 서빙

```text
serving/model2/
  preprocessing.py   F06 스키마 입력 정규화
  masking.py         [AMOUNT]/# 마스킹 (학습과 같은 SF.mask_text)
  proximity.py       proximity 추출 (학습과 같은 M82 정규식·window)
  feature_builder.py 211열 조립 + 열 순서 대조
  router.py          ordinal soft routing
  predict.py         진입점 (회귀 + percentile)
번들: models/model2_canonical/model2_p3_bundle.joblib (20.8 MB)
```
