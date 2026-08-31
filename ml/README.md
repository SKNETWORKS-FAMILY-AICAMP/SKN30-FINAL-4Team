# ml/ — 구조 안내 (machine-learning 브랜치)

```text
ml/
├─ data/
│   ├─ raw/          원천 첨부파일 (gitignore · E01 재실행에만 필요)
│   ├─ processed/    최종 학습·평가에 쓰는 테이블
│   └─ labels/       사람이 붙인 라벨 9종 (재생성 불가)
├─ models/           채택 모델의 serving artifact  ·  _archive/ 는 직전 세대
├─ pipelines/        수집 → 추출 → feature → 추론 코드 (canonical 산출물을 만드는 것)
├─ evaluation/       채택 모델의 최종 성능평가 코드
├─ experiments/      기각·대체된 실험 코드 (기록 보존)
├─ reports/          모든 측정 리포트 (평면 · 아래 '왜 평면인가' 참조)
├─ figures/          EDA·진단 그림
└─ tools/            smoke_test.py — 파이프라인 재현 점검
```

> 이 브랜치는 **ML 단계까지**를 담는다. 모델 1 의 DL 백본(KLUE-BERT)·학습번들과
> 결과서 2종은 `deep-learning` 브랜치에 있다. 브랜치 순서는
> `data-collection → feature-engineering → machine-learning → deep-learning`.

## 파일이 어디 있는지 정하는 규칙

| 폴더 | 기준 |
|---|---|
| `pipelines/` | 없으면 **canonical 산출물을 다시 만들 수 없는** 코드 |
| `evaluation/` | 채택된 모델의 성능·안정성을 **재는** 코드 |
| `experiments/` | 돌려 봤고 **채택되지 않은** 것 |

## 두 가지 주의 — 옮기기 전에 읽을 것

**1. 스크립트는 ml/ 바로 아래 한 단계에만 둔다.**
`common.ROOT` 가 `dirname(dirname(__file__))` 로 계산된다. 두 단계 아래로
내리면 `ROOT` 가 `ml/` 이 아니게 되고 데이터 경로가 전부 어긋난다.

**2. 스크립트끼리 평면 이름으로 import 한다.**
`import common as C`, `from m13_m4_anomaly import prepare` 같은 식이다.
역할별 디렉터리로 나눈 뒤에도 이게 동작하도록 각 파일 맨 위에 세 디렉터리를
`sys.path` 에 얹는 bootstrap 블록이 들어 있다. 새 스크립트도 같은 블록을 쓴다.

## `reports/` 를 왜 역할별로 나누지 않았는가

스크립트들이 **서로의 리포트를 경로로 직접 읽는다**. 역할별로 쪼개면 그 교차
참조가 전부 끊긴다. 대신 `reports/` 는 평면으로 두고 어느 리포트가 누구 것인지는
`evaluation/RESULTS.md` 와 `experiments/RESULTS.md` 가 색인한다.

## 재현 점검

```bash
python ml/tools/smoke_test.py
```

이 브랜치에 없는 단계(모델 1 학습번들 등)는 `[SKIP]` 으로 지나간다.
