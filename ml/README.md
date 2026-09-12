# ml/ — 구조 안내

```text
ml/
├─ data/
│   ├─ raw/          원천 첨부파일 5,000여 건 (gitignore · E01 재실행에만 필요)
│   ├─ processed/    최종 학습·평가에 쓰는 테이블
│   └─ labels/       사람이 붙인 라벨 9종 (재생성 불가)
│
├─ pipelines/        canonical 산출물을 만드는 코드
│   ├─ shared/         수집(d0*) → 추출(e01) → 기준테이블(f0*) → EDA(a01) · common.py
│   ├─ model1/  model2/  model3/
│
├─ experiments/      실험 실행 코드 — 세 모델 모두 같은 세 칸
│   ├─ model1/{core, validation, archive}
│   ├─ model2/{core, validation, archive}
│   ├─ model3/{core, validation, archive}
│   └─ shared/        모델을 가로지르는 점검 (누수·커버리지·정렬)
│
├─ evaluation/       채택 모델의 최종 성능평가 코드 (model1 / model3 / shared)
│
├─ reports/          결과 문서 — experiments 와 같은 세 칸을 쓴다
│   ├─ RESULTS.md      세 모델 전체 흐름 한 장
│   ├─ model1/{summary.md, core, validation, archive}
│   ├─ model2/{summary.md, core, validation, archive}
│   ├─ model3/{summary.md, core, validation, archive}
│   ├─ pipeline/       수집·추출·기준테이블·EDA 산출
│   └─ shared/         모델 교차 비교 (m03~m14 · m62)
│
├─ models/           채택 모델의 serving artifact  ·  _archive/ 는 직전 세대
├─ serving/          백엔드가 그대로 쓰는 추론 패키지 (model1 / model2 / model3)
├─ figures/          EDA·진단 그림
├─ tools/            smoke_test.py — 최종 파이프라인 재현 점검
└─ docs/             전처리 결과서 · 모델 성능 결과서 · 정리 변경목록
```

## 공식 모델은 셋뿐이다

| | 하는 일 | 현행 |
|---|---|---|
| **모델 1** | 지원성격 19종 분류 | KLUE-BERT · 외부 131건 정확도 0.8422 |
| **모델 2** | 지원규모 예측 + 상대 위치 | M82/P3 · OOF MAE 0.3518 |
| **모델 3** | 유사사업 대비 설계 이례성 | 거리 기반 · Spearman 0.967 |

**`model4` 는 없다.** 옛 '모델 4'(설계 이상탐지)는 별도 모델이 아니라 모델 3 으로
흡수됐다. 관련 코드는 역할대로 재배치했다.

```text
m13_m4_anomaly.py   -> pipelines/model3/m13_m3_anomaly.py     (살아있는 전처리)
m16_m4_tuning.py    -> experiments/model3/archive/m16_m3_ocsvm_tuning.py
m20_m4_threshold.py -> experiments/model3/archive/m20_m3_threshold.py
m23_m4_labelset.py  -> experiments/model3/archive/m23_m3_labelset.py
dl10_m4_anomaly.py  -> experiments/model3/archive/dl10_m3_autoencoder.py
```

`m13` 만 archive 가 아닌 이유: `evaluation/model3/m44·m64·m66` 과
`evaluation/shared/m62` 가 지금도 `from m13_m3_anomaly import prepare` 로 쓴다.
나머지 넷은 OneClassSVM·AutoEncoder 1세대 이상탐지로, 현행 거리 기반 구조에
승격되지 않았다(성능결과서 3.1).

## 파일이 어디 있는지 정하는 규칙

| 폴더 | 기준 |
|---|---|
| `pipelines/` | 없으면 **canonical 산출물을 다시 만들 수 없는** 코드 |
| `evaluation/` | 채택된 모델의 성능·안정성을 **재는** 코드 |
| `experiments/` | 돌려 본 실험 코드 |
| `reports/` | 그 결과물 (`.md` / `.json` / 감사 `.csv`) |
| `models/` | 최종 artifact (joblib · parquet 번들) |
| `serving/` | 백엔드가 호출하는 추론 코드 |

`experiments/` 와 `reports/` 아래 세 칸은 **모델 셋 모두 같은 뜻**이다.

```text
core        실제 성능/구조를 바꾼 것
validation  그 결과를 믿을 수 있는지 검증한 것
archive     해봤지만 최종 승격되지 않은 것
```

## 새 스크립트를 추가할 때

**1. 리포트 경로를 직접 쓰지 않는다.** `C.save_report("m90_m2_xxx.json", obj)` 로
쓰면 `common.REPORT_ROUTES` 가 알아서 `reports/model2/...` 로 떨어뜨린다. 새 실험을
추가하면 그 접두어를 `REPORT_ROUTES` 에 한 줄 넣는다 — 안 넣으면 `reports/shared/`
로 가고, 그렇게 루트에 쌓이기 시작하면 정리한 구조가 곧 무너진다.

**2. 스크립트끼리 평면 이름으로 import 한다.** `import common as C`,
`from m13_m3_anomaly import prepare` 같은 식이다. 각 파일 맨 위의 bootstrap 블록이
`pipelines`/`evaluation`/`experiments` 하위를 **재귀로** `sys.path` 에 얹으므로
파일이 몇 단계 아래로 내려가도 동작한다. 새 스크립트에도 같은 블록을 복사한다.

**3. 깊이 제한은 없다.** `common.ROOT` 는 `pipelines/` 와 `data/` 가 함께 있는
디렉터리를 위로 거슬러 찾는다(`_find_root`). 예전 안내에 있던 "ml/ 바로 아래 한
단계에만 둔다"는 제약은 더 이상 유효하지 않다.

## 재현 점검

```bash
python ml/tools/smoke_test.py
```

F06 3변형의 지문 재현 · 모델 2 serving 추론 · 모델 3 스코어링 · 모델 1 번들
정합성을 문서에 적힌 수치와 대조한다. 최종 실행: **22항목 · 실패 0 · 건너뜀 0**.
