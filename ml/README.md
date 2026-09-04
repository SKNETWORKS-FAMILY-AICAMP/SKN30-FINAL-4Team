# ml/ — 구조 안내 (data-collection 브랜치)

```text
ml/
├─ data/
│   ├─ raw/          원천 첨부파일 (gitignore · 다운로드 산출물)
│   └─ processed/    수집 표본 2종
├─ pipelines/        수집 코드
├─ reports/          수집 실적·manifest
├─ figures/          수집 진단 그림
└─ tools/            smoke_test.py — 재현 점검
```

> 이 브랜치는 **수집 단계까지**를 담는다. 브랜치 순서는
> `data-collection → feature-engineering → machine-learning → deep-learning`.

## pipelines/ — 실행 순서가 곧 의존 순서다

| 스크립트 | 산출물 |
|---|---|
| `d01_download_api.py` | Open API 공고문 다운로드 → `data/raw/attachments/api/` · `reports/d01_manifest_api.csv` |
| `d02_sample_list.py` | 목록 층화 표본 **5,000건** (56개 층, seed 42) → `list_sample.parquet` |
| `d02b_targeted_sample.py` | 지원성격 부족 유형(설비·교육훈련) 겨냥 보강 **2,856건** → `list_sample_targeted.parquet` |
| `d03_download_list.py` | 표본 공고문 다운로드 → `data/raw/attachments/list/` · `reports/d03_manifest_list.csv` |
| `common.py` | 원천 경로·분류체계·정규화 (라이브러리) |

> `d02b` 의 `target_support_type` 은 **다운로드 우선순위를 정하려는 겨냥일 뿐
> 확정 라벨이 아니다.** 실제 라벨은 다운로드 후 원문 기반으로 재분류한다.

## 두 가지 주의 — 옮기기 전에 읽을 것

**1. 스크립트는 ml/ 바로 아래 한 단계에만 둔다.**
`common.ROOT` 가 `dirname(dirname(__file__))` 로 계산된다. 두 단계 아래로
내리면 `ROOT` 가 `ml/` 이 아니게 되고 데이터 경로가 전부 어긋난다.

**2. 스크립트끼리 평면 이름으로 import 한다.**
`import common as C` 같은 식이다. 역할별 디렉터리로 나눈 뒤에도 이게 동작하도록
각 파일 맨 위에 `pipelines`/`evaluation`/`experiments` 를 `sys.path` 에 얹는
bootstrap 블록이 들어 있다. 새 스크립트도 같은 블록을 쓴다.

## 재현 점검

```bash
python ml/tools/smoke_test.py
```

수집 표본 2종의 건수를 `reports/d02*.json` 에 적힌 값과 대조한다. 이 브랜치에
없는 단계는 `[SKIP]` 으로 지나가므로 같은 파일을 모든 브랜치에서 쓴다.
