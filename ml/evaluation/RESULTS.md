# evaluation/ — 채택 모델의 최종 성능평가

상세 서술은 `../docs/02_모델_1_2_3_성능_결과서.md`. 여기는 **어느 스크립트가
어느 리포트를 냈고 결론이 무엇인지** 만 적는다. 리포트 실물은 `../reports/`
에 평면으로 있다(`../README.md` 의 '왜 평면인가' 참조).

## 모델 1 — 지원성격 분류 (19클래스) · **Freeze**

| 스크립트 | 리포트 | 결론 |
|---|---|---|
| `m29_m1_external_eval.py` | `m29_m1_external_eval.{json,md}` | 외부 Open API 131건 정확도 **0.8422 ± 0.0072** |
| `m54_m1_class_diagnosis.py` | `m54_m1_class_diagnosis.{json,md}` | 오차가 `사업화`(학습 1,404건 중 438건 = 31%)로 빨려 들어간다. 6종 백본 중 5종 공통 = 백본이 아니라 **다수 클래스 사전확률** |
| `m57_m1_confidence_split.py` | `m57_m1_confidence_split.{json,md}` | 라벨 확신도 '높음' 79건 **0.9494** vs 나머지 0.6538. 격차 CI 가 0 미포함 → 섞인 두 숫자를 분리하고 Freeze |
| `dl16_m1_abstention.py` | `dl/`·`dl16_m1_abstention.json` | 판단보류 커버리지 70% 에서 정확도 **0.9275** |
| `dl15_final_selection.py` | `dl15_final_selection.{json,md}` | 백본 6종 최종 선정 — KLUE-BERT |
| `m14_ml_dl_compare.py` | `m14_ml_dl_compare.{json,md}` | ML/DL 계열 비교. 텍스트 과제에서는 DL 이 이긴다 |

## 모델 2 — 지원규모 상대비교 · **M65 canonical**

| 스크립트 | 리포트 | 결론 |
|---|---|---|
| `m55_m2_leakage_audit.py` | `m55_m2_leakage_audit.{json,md}` | 제목 텍스트가 만드는 누수 경로 점검. `normalize_business_title()` 그룹에서 걸치는 계열 0개 |
| `m63_m2_requal.py` | `m63_m2_requal.{json,md}` | 수정 데이터 재평가. MAE 0.4155 → 0.4117 (Wilcoxon **p=0.113** — 예측력은 유의하게 달라지지 않음) |
| `m65_m2_canonical_v2.py` | `m65_m2_canonical_v2.{json,md}` | **승격**. 점검표 11항목 전부 PASS. MAE **0.4117** / baseline 0.5223 / 개선율 **21.2%**. 승격 근거는 MAE 가 아니라 **비교군 축의 정합성** |

## 모델 3 — 설계 이례성 · **구조 Freeze · 데이터 v3**

정답 대조 정확도가 아니라 **신호 안정성**으로 읽는다(성능결과서 3.3).

| 스크립트 | 리포트 | 결론 |
|---|---|---|
| `m44_m3_final_test.py` | `m44_m3_final_test.{json,md}` | 구조 확정. 정답 대조 ROC-AUC 0.7542 는 **탐색적 참고치**로만 |
| `m64_m3_requal.py` | `m64_m3_requal.{json,md}` | 수정 데이터 재평가. 비교군 구성 개선(얇은 비교군 195 → 118), 상위 목록 재현성은 악화(Top30 0.827 → 0.635) |
| `m66_m3_cohort_supply.py` | `m66_m3_cohort_supply.{json,md}`, `m66_supply_audit.csv` | 공급 보강 pool 2,451 → **2,626**. 지시 지표 5종 운영상태에서 전부 유지 (Spearman **0.967** · Top30 0.738 · attribution **0.970** · fallback **2.32%**) |

## 데이터 품질 (모델이 아니라 입력을 고친 라운드)

| 스크립트 | 리포트 | 결론 |
|---|---|---|
| `m62_data_quality.py` | `m62_data_quality.{json,md}`, `m62_unit_audit.csv` | **진단**. F06 `_pack_bizinfo` 가 근거문 자리에 금액을 뽑지 않은 텍스트를 저장 → `support_method` 834행 오류 |

수정은 M62 가 아니라 **F06 에서** 했다(`pipelines/f06_design_features.py`).
감사와 수정을 분리한 이유는 "고치고 나서 고쳤다고 확인"하는 자기증명을 피하기
위해서다.

## 재현

```bash
python ml/tools/smoke_test.py     # 위 수치 중 핵심 8개를 산출물과 대조
```
