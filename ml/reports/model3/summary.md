# Model 3 — 유사사업 대비 설계 이례성

> 상세 서술은 `../../docs/02_모델_1_2_3_성능_결과서.md` 3장.
> **'이례적'이지 '잘못됐다'가 아니다.** 출력 문구를 코드에서 강제한다(ALLOWED/FORBIDDEN).

```text
Representative Vector -> Distance -> Percentile -> Stability/Attribution -> freeze
```

정답 대조 정확도가 아니라 **신호 안정성**으로 읽는다(성능결과서 3.3).

| 검증 | 값 |
|---|---:|
| Spearman rank stability | 0.967 |
| Attribution Top1 consistency | 0.970 |
| Top30 overlap | 0.738 |
| Synthetic directional consistency | 1.00 |
| v3 pool | 2,626행 (전역 fallback 2.32%) |

## core — 현행 구조를 만든 것

| 실험 | 무엇을 했나 | 근거 |
|---|---|---|
| M12 | 비교군(cohort) 정의 | `core/m12_m3_cohort.{json,md}` |
| M33 | 라벨 재정의 | `core/m33_m3_relabel.{json,md}` |
| M38 | 대표벡터 / 차이벡터 방향 | `core/m38_m3_vector_direction.{json,md}` |
| M44 | **구조 확정** (정답 대조 ROC-AUC 0.7542 는 탐색적 참고치로만) | `core/m44_m3_final_test.{json,md}` |
| M51 | 설명축 attribution — **기여도 % 출력 채택** | `core/m51_m3_attribution.{json,md}` |

> **core 가 다섯 건뿐인 것은 누락이 아니다.** 모델 3 은 학습하는 모델이 아니라
> 거리 계산 규칙이라, 구조 자체는 `pipelines/model3/` 이 만든다. `experiments/` 에
> 남은 것은 대부분 그 구조가 우연이 아님을 확인한 검증과, 넘어서지 못한 개선
> 시도다. 그게 이 모델의 실제 이력이다.

## validation — 신호가 안정적인가

| 실험 | 확인한 것 | 결과 |
|---|---|---|
| M30 | 사람 라벨 50건 hold-out | 1세대 OneClassSVM 의 한계를 드러냄 |
| M31 | 파서 오류 감사 | `validation/m31_m3_parser_audit.csv` |
| M34 | 진단 (OCSVM 기준 CLEAN 정의) | 현행 `m44` 가 지금도 import |
| M37 | 합성 이상치 생성기 | 4축 stress 단조성 **100%** |
| M47 | 민감도 — `지원비율` 기여도 편중 | `validation/m47_m3_sensitivity.{json,md}` |
| M48 | 재표집 안정성 · 비교군 | 흔들림의 **위치**를 특정 (n=20~30 얇은 비교군 5~6개) |
| M49 | synthetic stress | 방향성 상식적으로 작동 |
| M64 | 데이터 수정 후 재평가 | 얇은 비교군 195 → 118, Top30 0.827 → 0.635 |
| M66 | 공급 보강 | pool 2,451 → **2,626**, 지표 5종 전부 유지 |

## archive — 승격되지 않은 것

### 1세대 이상탐지 (옛 '모델 4')

모델 4 는 별도 모델이 아니라 모델 3 으로 흡수됐다. 그 1세대 스택이 왜 무너졌는지가
현행 거리 기반 구조의 출발점이다(성능결과서 3.1).

| 실험 | 결과 | 미채택 사유 |
|---|---|---|
| M13 IsolationForest / LOF / OneClassSVM | 회수율 0.083 / 0.633 / 0.783 | IF 는 결측 지시자·희귀 범주 빈도에 점수가 지배당함 |
| M16 OCSVM 튜닝 | recall 개선 | 지표 기준이 섞여 있었음 (M20 이 정정) |
| M20 경고정책 | 경고율 2% 권고 | 구조가 바뀌며 무의미해짐 |
| M23 라벨링 세트 | 층화 50건 | `archive/m23_m3_labelset{,_key}.csv` 로 보존 |
| DL10 AutoEncoder / DL13·DL17 Deep SVDD | 합성 recall 81.7% (ML 상회) | **재학습 시 상위 목록 유지율 29~48%** — 표본에 따라 순위가 뒤집힘 |
| M36 OneClassSVM | — | 현행 거리 기반으로 대체 |

`m13` 의 **코드만은 살아 있다** — `pipelines/model3/m13_m3_anomaly.py` 의 `prepare()`
가 `evaluation/model3/m44·m64·m66` 과 `evaluation/shared/m62` 의 입력 전처리다.

### 옛 지원규모 구간 세대

| 실험 | 미채택 사유 |
|---|---|
| DL09 quantile 회귀 MLP · M17 LightGBM 튜닝 · M19 Conformal · M22 Mondrian CQR | 모델 번호 재편 전 '구간 예측' 세대. 현재 그 역할은 모델 2 가 한다 |
| DL18 텍스트 벡터 결합 | 텍스트 가중치(0.6/0.4)를 데이터로 고를 방법이 없었음 |

### 개선 실험 4종 (M58~M61) — 전부 현행 유지

판정 문턱(Top30 겹침 0.918 / Spearman 0.969)은 M44·M48 이 잰 재표집 변동폭에서
가져와 **결과를 보기 전에 고정**했다.

| 실험 | 판정 | 결정적 근거 |
|---|---|---|
| M50 얇은 비교군 shrinkage | 미채택 | 순위 안정성 개선 없음 |
| M58 비교군 정교화 | 현행 유지 | `+기관계열` 은 **ROC 최고(0.7542→0.8271)인데 reject** — 재표집 Top30 0.796→0.709 |
| M59 multi-prototype | 현행 유지 | 재표집 Top30 0.796→0.729(k=2) / 0.720(k=3) |
| M60 thin cohort fallback | 현행 유지 (20) | MIN_COHORT 30 은 전체 fallback 79→**197건**으로 급증 |
| M61 scaling / distance | 현행 유지 | robust 는 상위 30건의 63% 교체 — `지원비율` 하나가 기여도의 96% |

**이 실험들이 남긴 것.** ① "ROC 가 올라도 reject" 가 M58 에서 실물로 발동했다
(기관계열은 pool 의 37%가 결측 — 정보가 는 게 아니라 *아는 사업/모르는 사업*을
가른 것). ② 안정성과 동질성은 서로 반대로 움직인다. ③ 얇은 비교군은 계산법으로
안 풀린다 — 해법은 모델링이 아니라 **표본 확대**(3.8 에서 실행). ④ scaling 진단이
`지원비율` 최소값 **-320%** 파싱 오류 1건을 찾아냈다.

> **아쉽게 놓친 후보 하나.** M61 의 `Mahalanobis 유사` 는 재표집 안정성이 네 표집비율
> **전부**에서 현행보다 낫고 축 점유율도 0.377→0.332 인데, 상위 30건의 10%가 교체되어
> 사전 고정 문턱(8%)을 2%p 넘겨 reject 됐다. **문턱을 결과 보고 옮기지는 않는다.**
> 기각이 아니라 "라벨이 확보되면 가장 먼저 다시 볼 후보"다.

## Freeze 상태

```text
거리 기반 이례성 점수   유지
비교군 사다리          유지 (지원성격x지원방식 -> 지원성격 -> 전체)
mean 대표벡터          유지 (k=2/3 은 재현성 악화 M59)
standard scaling       유지 (robust 는 Top30 63~67% 교체 M47/M61)
Euclidean 거리         유지 (축가중·Mahalanobis 는 목록 교체 문턱 초과 M61)
MIN_COHORT = 20        유지 (30 이상이면 fallback 79->197 급증 M48/M60)
설명축                 기여도 % 출력 (M51)
입력                   design_features_v3.parquet (M65 근거문 수정 + M66 공급 보강)
```

**모델 3 모델링을 종료한다.** 새 이상탐지 알고리즘 탐색을 다시 시작하지 않는다.

## 서빙

```text
serving/model3/score.py    진입점 (inference.py 구현)
pool: serving/model3/design_features_v3.parquet
```
