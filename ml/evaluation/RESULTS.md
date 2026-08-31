# evaluation/ — 채택 모델의 최종 성능평가 (machine-learning 브랜치)

리포트 실물은 `../reports/` 에 평면으로 있다. 서술형 결과서 2종은
`deep-learning` 브랜치의 `ml/docs/` 에 있다.

## 모델 2 — 지원규모 상대비교 · **M65 canonical**

| 스크립트 | 리포트 | 결론 |
|---|---|---|
| `m55_m2_leakage_audit.py` | `m55_m2_leakage_audit.{json,md}` | 제목 텍스트가 만드는 누수 경로 점검. `normalize_business_title()` 그룹에서 걸치는 계열 0개 |
| `m63_m2_requal.py` | `m63_m2_requal.{json,md}` | 수정 데이터 재평가. MAE 0.4155 → 0.4117 (Wilcoxon **p=0.113** — 예측력은 유의하게 달라지지 않음) |
| `m65_m2_canonical_v2.py` | `m65_m2_canonical_v2.{json,md}` | **승격**. 점검표 11항목 전부 PASS. MAE **0.4117** / baseline 0.5223 / 개선율 **21.2%**. 승격 근거는 MAE 가 아니라 **비교군 축의 정합성** |

artifact: `../models/m65_model2_canonical/` (현행), `../models/_archive/m56_model2_canonical/` (직전 세대).

## 모델 3 — 설계 이례성 · **구조 Freeze**

정답 대조 정확도가 아니라 **신호 안정성**으로 읽는다.

| 스크립트 | 리포트 | 결론 |
|---|---|---|
| `m44_m3_final_test.py` | `m44_m3_final_test.{json,md}` | 구조 확정. 정답 대조 ROC-AUC 0.7542 는 **탐색적 참고치**로만 |
| `m64_m3_requal.py` | `m64_m3_requal.{json,md}` | 수정 데이터 재평가. 비교군 구성 개선(얇은 비교군 195 → 118), 상위 목록 재현성은 악화(Top30 0.827 → 0.635). 구조는 유지 |

> 공급 보강(M66 · `design_features_v3`)은 `deep-learning` 브랜치에 있다.

## 모델 1 — 외부 검증

| 스크립트 | 리포트 | 결론 |
|---|---|---|
| `m29_m1_external_eval.py` | `m29_m1_external_eval.{json,md}` | 외부 Open API 131건 정확도 **0.8422 ± 0.0072** (백본 학습은 deep-learning 브랜치) |

## 데이터 품질 (모델이 아니라 입력을 고친 라운드)

| 스크립트 | 리포트 | 결론 |
|---|---|---|
| `m62_data_quality.py` | `m62_data_quality.{json,md}`, `m62_unit_audit.csv` | **진단**. F06 `_pack_bizinfo` 가 근거문 자리에 금액을 뽑지 않은 텍스트를 저장 → `support_method` 834행 오류 |

수정은 M62 가 아니라 **F06 에서** 했다(`pipelines/f06_design_features.py`).
감사와 수정을 분리한 이유는 "고치고 나서 고쳤다고 확인"하는 자기증명을 피하기
위해서다.
