# 모델 교차 점검 · 데이터 품질 라운드

> 모델별 결론은 각 `summary.md` 에 있다 —
> `../model1/summary.md` · `../model2/summary.md` · `../model3/summary.md`.
> 전체 흐름 한 장은 `../RESULTS.md`.
>
> 이 폴더(`reports/shared/`)에는 **어느 한 모델에 속하지 않는 것**만 둔다.

## 모델을 가로지르는 점검 (`experiments/shared/`)

| 스크립트 | 리포트 | 무엇을 확인했나 |
|---|---|---|
| `m03_input_alignment.py` | `m03_input_alignment.json` | 학습 입력과 서빙 입력이 같은 텍스트인가 — 모델 1 판정이 한 번 뒤집혔던 원인 |
| `m04_inference_sample.py` | `m04_inference_sample.{json,md}` | 추론 표본 육안 검토 |
| `m05_leakage_check.py` | `m05_leakage_check.json` | 재공고 그룹 누수 차단(`program_stem`) |
| `m06_min_support.py` | `m06_min_support.json` | 클래스별 최소 표본 |
| `m07_manual_eval.py` | `m07_manual_eval.{json,md}` | 사람 대조 |
| `m09_coverage_accuracy.py` | `m09_coverage_accuracy.json` | 커버리지-정확도 trade-off |
| `m10_design_coverage.py` | `m10_design_coverage.{json,md}` | 설계축 결측 구조 — 모델 3 이 Conditional 을 쓰는 근거(4축 중 3축 이상 채워진 행이 22%) |

## ML / DL 계열 비교 (`evaluation/shared/`)

| 스크립트 | 리포트 | 결론 |
|---|---|---|
| `m14_ml_dl_compare.py` | `m14_ml_dl_compare.{json,md}` | 텍스트 과제(모델 1)에서는 DL 이 이긴다. 표형 과제에서는 아니다 |

## 데이터 품질 — 모델이 아니라 입력을 고친 라운드

| 스크립트 | 리포트 | 결론 |
|---|---|---|
| `m62_data_quality.py` | `m62_data_quality.{json,md}` · `m62_unit_audit.csv` | **진단.** F06 `_pack_bizinfo` 가 근거문 자리에 금액을 뽑지 않은 텍스트를 저장 → `support_method` 834행 오류 |

수정은 M62 가 아니라 **F06 에서** 했다(`pipelines/shared/f06_design_features.py`).
감사와 수정을 분리한 이유는 "고치고 나서 고쳤다고 확인"하는 자기증명을 피하기
위해서다. 재평가는 모델 2 는 M65, 모델 3 은 M64·M66 이 맡았다.

## 재현

```bash
python ml/tools/smoke_test.py
```
