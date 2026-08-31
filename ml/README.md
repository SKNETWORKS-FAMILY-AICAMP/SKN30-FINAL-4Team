# ml/ — 구조 안내 (feature-engineering 브랜치)

```text
ml/
├─ data/
│   ├─ raw/          원천 첨부파일 (gitignore · 추출 재실행에만 필요)
│   └─ processed/    기준 테이블
├─ pipelines/        수집 → 추출 → 기준 테이블 구축 코드
├─ reports/          측정 리포트
└─ tools/            smoke_test.py — 재현 점검
```

> 이 브랜치는 **피처 엔지니어링 단계까지**를 담는다. 모델 학습·평가는
> `machine-learning` / `deep-learning` 브랜치에 있다. 브랜치 순서는
> `data-collection → feature-engineering → machine-learning → deep-learning`.

## pipelines/ — 실행 순서가 곧 의존 순서다

| 스크립트 | 산출물 |
|---|---|
| `d01_download_api.py` | Open API 공고문 → `data/raw/attachments/api/` |
| `d02_sample_list.py` | 목록 층화 표본 → `list_sample.parquet` |
| `d02b_targeted_sample.py` | 지원성격 부족 셀 보강 표본 → `list_sample_targeted.parquet` |
| `d03_download_list.py` | 표본 공고문 다운로드 → `data/raw/attachments/list/` |
| `e02_extract_text_v2.py` | 원문 텍스트 추출 (표 구조 보존) → `reports/e01_documents*.jsonl` |
| `f01_master.py` | `announcement_master.parquet` (97,794건) |
| `f02_detail.py` | `announcement_detail.parquet` (1,570건) |
| `f03_taxonomy.py` | `business_taxonomy.parquet` (1,505건) |
| `f04_merge_documents.py` | `announcement_detail_enriched.parquet` (1,570건) |
| `amount_parser.py` | 지원규모 파서 (라이브러리) |
| `common.py` | 원천 경로·분류체계·정규화 (라이브러리) |

> `e01_documents*.jsonl` 은 gitignore 대상이지만 **임시파일이 아니라 F04 이후
> 전 단계의 입력**이다. 지우면 하류가 재현되지 않는다.

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

기준 테이블 4종의 행수를 `reports/f0*.json` 에 적힌 값과 대조한다. 이 브랜치에
없는 단계(모델 학습 등)는 `[SKIP]` 으로 지나간다.
