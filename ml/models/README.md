# models/ — 채택 모델의 serving artifact

```text
models/
├─ model1_canonical/                    모델 1 학습·외부검증 데이터 번들
│    ├─ train.parquet               1,404건 · 19클래스
│    └─ external.parquet            131건 (라벨 확신도 포함)
├─ model2_canonical/                모델 2 현행 canonical — M82/P3
│    ├─ model2_p3_bundle.joblib     변환기 3종 + 모델 6개 + 211열 스키마 (20.8 MB)
│    └─ manifest.json               지문·성능·자체검증·feature 목록
└─ _archive/
     ├─ m65_model2_canonical/       모델 2 직전 세대 (M65 단일 XGB, 0.4117)
     └─ m56_model2_canonical/       그 전 세대 (성능결과서 2.5 가 '보존' 으로 명시)
```

## 모델별 무엇이 어디 있는가

| 모델 | 채택 | artifact | 비고 |
|---|---|---|---|
| **1** 지원성격 분류 | KLUE-BERT (+판단보류) · Freeze | **저장소 밖** | 가중치 400MB+ 라 gitignore(`*.safetensors`·`*.bin`). `model1_canonical/` 로 `pipelines/model1/dl12_m1_candidates.py` 를 돌리면 재학습된다 |
| **2** 지원규모 상대비교 | 비교군 percentile + **M82/P3** (ordinal soft routing) | `model2_canonical/` | 저장물만으로 추론된다 — `serving/model2/predict.py` |
| **3** 설계 이례성 | 비교군 대비 거리 · Freeze | **없음 (구조상)** | 저장된 가중치가 없는 모델이다. `pipelines/model3/m3_lab.py` 가 `design_features_v3.parquet` 을 읽어 매번 계산한다 |

## 모델 2 추론 (M82/P3)

```python
import sys
sys.path.insert(0, "ml/serving/model2")
from predict import predict, percentile
out = predict(records)   # records: title · evidence_text 필수 + F06 필드
```

번들 재생성은 `python ml/serving/model2/build_bundle.py`.

추론은 학습 코드 경로를 타지 않는다. 대신 **열 이름과 순서를 번들에 통째로
저장해 매 호출 대조한다**(`feature_builder.assert_schema`) — 여기가 어긋나면
XGBoost 는 에러 없이 조용히 다른 모델이 된다. 마스킹도 서빙에서 매번
확인한다(`context_digit_residue`) — 학습에서만 확인하면 의미가 없다.

세대: M65 0.4117 -> M69 0.3719 -> M73 0.3563 -> **M82/P3 0.3518**.
구세대 M65 는 `_archive/` 와 `serving/model2/inference.py` 에 남아 있고,
백엔드 전환이 끝나면 지운다.

## `_archive/` 를 왜 남기는가

M65 승격의 근거는 *MAE 개선*이 아니라 **비교군 축의 정합성**이었다
(MAE 0.4155 → 0.4117 은 Wilcoxon p=0.113 으로 유의하지 않다). 두 세대를 같이
두어야 "무엇이 실제로 바뀌었는가"를 다시 잴 수 있다. M45 를 남긴 것과 같은
이유다.
