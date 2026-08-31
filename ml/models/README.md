# models/ — 채택 모델의 serving artifact

```text
models/
├─ m1_dl_bundle/                    모델 1 학습·외부검증 데이터 번들
│    ├─ train.parquet               1,404건 · 19클래스
│    └─ external.parquet            131건 (라벨 확신도 포함)
├─ m65_model2_canonical/            모델 2 현행 canonical
│    ├─ model2_canonical.joblib     vectorizer · svd · point · quantile · meta
│    └─ manifest.json               지문·파이프라인·누수점검·승격 점검표
└─ _archive/
     └─ m56_model2_canonical/       모델 2 직전 세대 (성능결과서 2.5 가 '보존' 으로 명시)
```

## 모델별 무엇이 어디 있는가

| 모델 | 채택 | artifact | 비고 |
|---|---|---|---|
| **1** 지원성격 분류 | KLUE-BERT (+판단보류) · Freeze | **저장소 밖** | 가중치 400MB+ 라 gitignore(`*.safetensors`·`*.bin`). `m1_dl_bundle/` 로 `pipelines/dl12_m1_candidates.py` 를 돌리면 재학습된다 |
| **2** 지원규모 상대비교 | 비교군 percentile + XGBoost(구조화+제목) | `m65_model2_canonical/` | 저장물만으로 추론된다 — `m56_m2_canonical.serve(bundle, records)` |
| **3** 설계 이례성 | 비교군 대비 거리 · Freeze | **없음 (구조상)** | 저장된 가중치가 없는 모델이다. `pipelines/m3_lab.py` 가 `design_features_v3.parquet` 을 읽어 매번 계산한다 |

## 모델 2 추론

```python
import joblib, m56_m2_canonical as M56
bundle = joblib.load("ml/models/m65_model2_canonical/model2_canonical.joblib")
out = M56.serve(bundle, records)   # records = M56.SERVING_FIELDS 를 가진 dict 목록
```

`serve()` 는 학습 코드 경로를 타지 않는다. 컬럼 순서·범주 목록·CQR 보정폭을
전부 `meta` 에서 강제로 맞춘다 — 여기가 어긋나면 서빙이 조용히 다른 모델이
된다. `manifest.json` 의 `step_c.roundtrip` 이 그 일치를 기록한 것이다.

## `_archive/` 를 왜 남기는가

M65 승격의 근거는 *MAE 개선*이 아니라 **비교군 축의 정합성**이었다
(MAE 0.4155 → 0.4117 은 Wilcoxon p=0.113 으로 유의하지 않다). 두 세대를 같이
두어야 "무엇이 실제로 바뀌었는가"를 다시 잴 수 있다. M45 를 남긴 것과 같은
이유다.
