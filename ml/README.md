# ml/ — 구조 안내

```text
ml/
├─ data/
│   ├─ raw/          원천 첨부파일 5,000여 건 (gitignore · E01 재실행에만 필요)
│   ├─ processed/    최종 학습·평가에 쓰는 테이블
│   └─ labels/       사람이 붙인 라벨 9종 (재생성 불가)
├─ models/           채택 모델의 serving artifact  ·  _archive/ 는 직전 세대
├─ pipelines/        수집 → 추출 → feature → 추론 코드 (canonical 산출물을 만드는 것)
├─ evaluation/       채택 모델의 최종 성능평가 코드
├─ experiments/      기각·대체된 실험 코드 (기록 보존)
├─ reports/          모든 측정 리포트 (평면 · 아래 '왜 평면인가' 참조)
├─ figures/          EDA·진단 그림
├─ tools/            smoke_test.py — 최종 파이프라인 재현 점검
└─ docs/             전처리 결과서 · 모델 성능 결과서
```

## 파일이 어디 있는지 정하는 규칙

| 폴더 | 기준 |
|---|---|
| `pipelines/` | 없으면 **canonical 산출물을 다시 만들 수 없는** 코드 |
| `evaluation/` | 채택된 모델의 성능·안정성을 **재는** 코드 |
| `experiments/` | 돌려 봤고 **채택되지 않은** 것. 기각 사유가 `experiments/RESULTS.md` 에 있다 |

## 두 가지 주의 — 옮기기 전에 읽을 것

**1. 스크립트는 ml/ 바로 아래 한 단계에만 둔다.**
`common.ROOT` 가 `dirname(dirname(__file__))` 로 계산된다. 두 단계 아래로
내리면 `ROOT` 가 `ml/` 이 아니게 되고 데이터 경로가 전부 어긋난다.

**2. 스크립트끼리 평면 이름으로 import 한다.**
`import common as C`, `from m13_m4_anomaly import prepare` 같은 식이다.
역할별 디렉터리로 나눈 뒤에도 이게 동작하도록 각 파일 맨 위에 세 디렉터리를
`sys.path` 에 얹는 bootstrap 블록이 들어 있다(85개 파일). 새 스크립트를
추가할 때도 같은 블록을 복사한다.

## `reports/` 를 왜 역할별로 나누지 않았는가

스크립트들이 **서로의 리포트를 경로로 직접 읽는다** — `dl13` 이 `m30` 의
json 을, `dl15` 가 12개 리포트를 모아 읽는 식으로 **107개 호출부**가 걸려
있다. 역할별로 쪼개면 이 교차 참조가 전부 끊긴다. 대신 `reports/` 는 평면으로
두고, 어느 리포트가 누구 것이고 채택/기각이 어떻게 됐는지는
`evaluation/RESULTS.md` 와 `experiments/RESULTS.md` 가 색인한다.

## 재현 점검

```bash
python ml/tools/smoke_test.py
```

F06 3변형의 지문 재현 · 모델 2 serving 추론 · 모델 3 스코어링 · 모델 1 번들
정합성을 문서에 적힌 수치와 대조한다.
