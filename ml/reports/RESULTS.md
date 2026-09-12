# RESULTS — 모델 1 · 2 · 3 최종 결과

모든 실험을 나열하지 않는다. **성능을 만든 흐름**과 **그 결과를 믿을 수 있게
한 검증**만 둔다.

```text
core        실제 성능/구조를 바꾼 실험
validation  결과를 믿을 수 있는지 검증한 실험
archive     해봤지만 최종 승격되지 않은 실험
```

이 세 칸을 **모델 셋 모두** `experiments/` 와 `reports/` 에서 똑같이 쓴다.
모델별 상세 색인은 각 `summary.md` 에 있다.

| | 색인 | 실험 코드 |
|---|---|---|
| Model 1 | `model1/summary.md` | `experiments/model1/{core,validation,archive}/` |
| Model 2 | `model2/summary.md` | `experiments/model2/{core,validation,archive}/` |
| Model 3 | `model3/summary.md` | `experiments/model3/{core,validation,archive}/` |

기각 실험의 원본 서술은 `model2/archive/EXPERIMENTS_REJECTED.md`.
**공식 모델은 1·2·3 셋뿐이다** — 옛 '모델 4'(설계 이상탐지)는 모델 3 으로
흡수됐고 관련 코드·리포트는 `model3/` 아래로 재배치했다(`../README.md`).

---

## Model 1 — 지원유형 19-class 분류

```text
TF-IDF/SVM  ->  KoELECTRA  ->  KLUE-RoBERTa  ->  KLUE-BERT  ->  final freeze
```

| 항목 | 값 |
|---|---:|
| 학습 | 1,404건 |
| 외부 검증 | 131건 |
| 클래스 | 19 |

서빙: `serving/model1/predict.py` (`inference.py` 구현) · 번들 `models/model1_canonical/`

---

## Model 2 — 지원규모(기업당 한도) 회귀

### 메인 흐름

| 단계 | 무엇을 바꿨나 | OOF MAE(log10) |
|---|---|---:|
| **M65** | 초기 XGBoost baseline | 0.4117 |
| **M69** | 원천 feature 층 + 마스킹 본문 SVD64 | 0.3719 |
| **M73** | ordinal soft routing (구간 expert 3 + 누적 이진 2) | 0.3563 |
| **M82/P3** | explicit proximity + masked proximity TF-IDF/SVD | **0.3518** |

### 최종 canonical — M82/P3

```text
Primary OOF MAE   0.3518   (strict 0.3751)
2배 이내          57.2%     3배 이내 74.4%
vs M73            -0.0045   fold승 5/5   95% CI [-0.0083, -0.0003]
feature           211열
구조              global 1 + 구간 expert 3 + ordinal 이진 2 = 모델 6개
```

M82 가 중요한 이유: M67 이후 열었던 축(routing · residual/MoE · semantic
embedding · local cohort · calibration · 곡선회귀 · expert 튜닝 · 이종 모델군 ·
앙상블 · hierarchical prior · label correction) 이 전부 M73 을 넘지 못했는데,
**모델 구조가 아니라 입력 표현을 바꿔** 처음으로 유의하게 이겼다. 즉 병목은
XGBoost 가 아니라 **기존 feature 가 수치와 의미의 결합 관계를 표현하지 못한
것**에 가까웠다.

P1(명시 수치)과 P2(수치 주변 문맥)는 각각으로는 미달이고 **결합했을 때만**
통과했다 — 정형 수치 정보와 주변 문맥 정보가 상보적이라는 뜻이다.

### 검증 (validation)

| 실험 | 확인한 것 | 결과 |
|---|---|---|
| M71 | target 품질 감사 | 파싱 오류 4.9% — 교정해도 2/5 fold |
| M74 | 학습곡선 | MAE = c·n^−0.194 (R²=0.9925) — 남은 병목은 관측 수 |
| M78 | 후처리/calibration | 보정 여지 약함 |
| M79 | XGB/LGBM/CatBoost/앙상블 | residual 상관 ≈0.99, 앙상블 가치 낮음 |
| M81 | 공통 hard-case 라벨 감사 | 고오차 행 오류율 18.6~25.6% (전체 5.3%) — **실재하나 교정이 성능을 못 올림** |
| M82-B | 재현성 3-run | OOF·MAE·CI·차원·순서 **exact match** |
| M82-C | serving smoke | 신규 문서 end-to-end **8/8 PASS** |

M81 의 결론이 중요하다 — M79 의 0.99 residual 상관은 **라벨 오류가 주병목이
아니라**, 네 모델이 같은 feature 정보로 같은 행에서 같은 방향으로 헤매는
것이다. 정보가 없는 것이지 정답이 틀린 것이 아니다.

### 승격되지 않은 후속 (archive)

| 실험 | 축 | Primary | 결과 |
|---|---|---:|---|
| M83 | typed proximity relation | 0.3524 | 2/5 fold, CI 0 포함 |
| M84 | section-aware proximity | 0.3534 | 2/5 fold, CI 0 포함 |
| M83+M84 | 결합 | 0.3509 | 3/5 fold, CI 0 포함 |
| M85 | table/layout-aware (T5) | 0.3498 | **5/5 fold** 인데 CI [-0.0047, +0.0006] |

M85 는 9개 승격 기준 중 8개를 통과하고 **CI 하나에서** 걸렸다. 방향은
일관적이었고(5/5 fold · 두 split · 두 코호트 · 두 포맷 모두 개선, 특히 표가
있는 행의 개선폭이 없는 행의 2배) 개선폭이 노이즈 바닥 아래였을 뿐이다.

M83·M84·M85 가 **같은 형태로** 실패했다는 것이 결론이다 — MAE 숫자는 P3 보다
낮은데 CI 가 0 을 못 넘는다. 남은 신호가 없는 게 아니라 **n=1,877 의 통계적
분해능보다 작다.** M74 의 학습곡선이 같은 이야기를 한다.

> **구조 기반 feature engineering 은 현재 데이터에서 소진.** 다음은 모델이
> 아니라 데이터다 — 신규 표본 · evidence-backed label · 구조 보존된 원문.

M85 Exp0 이 마지막 항목에 바로 쓰이는 사실을 하나 남겼다: **PDF 는 표 구조가
85% 살아 있는데 F05 가 PDF 를 버려 모델링 프레임(1,877행)에 한 행도 없다.**
HWP/HWPX 는 추출 텍스트에 표가 0% 남는다(원본에는 있다 — M85 가 재파싱으로
복원했다). 원문 품질을 올리려면 새 문서를 모으기 전에 이 경로부터 여는 것이
비용 대비 효과가 크다.

### 서빙

```text
serving/model2/
  preprocessing.py   F06 스키마 입력 정규화
  masking.py         [AMOUNT]/# 마스킹 (학습과 같은 SF.mask_text)
  proximity.py       proximity 추출 (학습과 같은 M82 정규식·window)
  feature_builder.py 211열 조립 + 열 순서 대조
  router.py          ordinal soft routing
  predict.py         진입점 (회귀 + percentile)
  inference.py       [구세대 M65] 백엔드 전환 후 삭제 가능
번들: models/model2_canonical/model2_p3_bundle.joblib (20.8 MB)
```

---

## Model 3 — 유사사업 대비 설계 이례성

```text
Representative Vector -> Distance -> Percentile -> Stability/Attribution -> freeze
```

| 검증 | 값 |
|---|---:|
| Spearman rank stability | 0.967 |
| Attribution Top1 consistency | 0.970 |
| Top30 overlap | 0.738 |
| Synthetic directional consistency | 1.00 |
| v3 pool | 2,626행 (전역 fallback 2.32%) |

서빙: `serving/model3/score.py` (`inference.py` 구현)

---

## 재현

```bash
python ml/tools/smoke_test.py
```

F06 세 판본의 해시 일치, 모델 2 서빙 번들(M82/P3, 211열, 마스킹 감사), 모델 3
스코어링, 모델 1 학습번들을 한 번에 확인한다. 최종 실행: **실패 0 · 건너뜀 0**.
