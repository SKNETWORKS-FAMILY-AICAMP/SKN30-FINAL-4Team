# Existing KB 100건 bootstrap 가이드

이 문서는 새 self-hosted Supabase에 기업마당 Existing 공고 100건의 원본·중간 산출물·
구조화 Profile을 private Storage와 `kb.*`에 넣고, pgvector 검색용 embedding까지 만드는
절차를 고정한다.

공고 데이터 ZIP은 약 30 MiB이고 풀면 약 89 MiB이므로 Git에 넣지 않는다. 저장소에는
그 ZIP의 식별자와 SHA-256을 담은
[`fixtures/existing-kb-100.manifest.json`](fixtures/existing-kb-100.manifest.json)만 둔다.
현재 기준 외부 보관 위치는 다음과 같다.

```text
/srv/pre-review/imports/bizinfo-existing/structured-profiles-100.zip
```

경로는 바뀌어도 되지만 ZIP 바이트가 manifest와 같아야 한다. 기존
`ingestion-20260909.ndjson`은 과거 실행 로그이지 bootstrap 입력이 아니다.

## 데이터팩 계약

각 공고에는 정확히 다음 6개 파일이 있어야 한다.

```text
PBLN_<공고번호>/
├── metadata.json
├── attachments/
│   └── <실제 분석 원본>.hwp | .hwpx | .pdf
└── pipeline/
    ├── ingestion_record.v0.1.json
    ├── structured_profile.v0.2.json
    ├── source_selection.json
    └── common_ir_v1/
        └── <공고번호>.<실제 확장자>.json
```

PDF 47건도 포함한다. 이 bootstrap은 이미 생성된 Common IR/Profile을 적재하므로 PDF를
다시 파싱하거나 OCR하지 않는다. 새 PDF를 구조화하는 파이프라인과는 별개다.

`ingestion_record.v0.1.json`의 wrapper 계약은
[`contracts/bizinfo_existing_ingestion_record.v0.1.schema.json`](contracts/bizinfo_existing_ingestion_record.v0.1.schema.json)에
있다. 그 안의 `portal_metadata`와 `structured_profile`은 각각 옆의 `metadata.json`,
`structured_profile.v0.2.json`과 JSON 값이 완전히 같아야 한다. 파일 간 ID·상대경로·
SHA-256·Common IR/candidate-pack 계보·Fact exact span 일치는 schema만으로 표현하지 않고
아래 validator가 검사한다. ZIP과 단건 경로 모두 symlink·특수 파일을 거부하고, 최대
10,000개 entry·전체 512 MiB·파일당 128 MiB까지만 처리한다.

현재 importer는 `target_constraints.business_age`의 자유형 객체를 DB numeric dimension으로
손실 없이 매핑할 계약이 없다. 이 값은 `null`일 때만 허용하고, 빈 객체를 포함한 non-null
값은 조용히 버리지 않고 preflight에서 거부한다.

고정 100건 팩의 기대치는 다음과 같다.

| 항목 | 기대값 |
|---|---:|
| 공고 | 100 |
| 파일 | 600 |
| HWP / HWPX / PDF | 48 / 5 / 47 |
| Fact | 2,409 |
| Existing delivery role / 기관 occurrence | 38 / 48 |
| support facet / scale projection | 4 / 30 |
| target constraint | 0 |
| Profile embedding | 400행(100 × 4 scope) |

## 사전 조건

1. 공식 Supabase bundle과 pgvector override를 포함해 컨테이너가 healthy여야 한다.
2. migration 01~24를 적용해 private `existing-kb` bucket과 `kb`, `retrieval` schema를
   만들어야 한다.
3. `backend/.venv`를 준비한다.
4. `backend/.env`에 서버 전용 `SUPABASE_URL`, service/secret key,
   `SUPABASE_DB_URL` 또는 `DATABASE_URL`을 둔다. embedding 단계에만
   `OPENAI_API_KEY`가 필요하다. 값을 명령행·Git·로그에 적지 않는다.

Supabase 설치·migration과 `.env` 준비는 [README](README.md)와
[FastAPI·worker 운영 가이드](../fastapi/docs/FASTAPI_WORKER_RUNBOOK.md)를 먼저 따른다.

## 1. ZIP 무결성 및 계약 검증

이 단계는 임시 디렉터리만 사용하며 DB·Storage·OpenAI를 호출하지 않는다.

```bash
cd /path/to/repository
backend/.venv/bin/python backend/scripts/existing_kb_pack.py \
  /srv/pre-review/imports/bizinfo-existing/structured-profiles-100.zip \
  --expected-count 100 \
  --manifest backend/supabase/fixtures/existing-kb-100.manifest.json
```

성공 시 `"status": "valid"`와 100건 집계가 JSON 한 줄로 출력된다. validator는 ZIP
경로 탈출·symlink·파일/디렉터리 충돌·과대 member도 거부한다.

새 위치에 풀어야 할 때는 **존재하지 않는 전용 절대경로**를 준다. 기존 디렉터리를
덮어쓰지 않으며, 모든 검증이 끝난 뒤에만 결과 경로로 원자적으로 옮긴다.

```bash
backend/.venv/bin/python backend/scripts/existing_kb_pack.py \
  /srv/pre-review/imports/bizinfo-existing/structured-profiles-100.zip \
  --expected-count 100 \
  --manifest backend/supabase/fixtures/existing-kb-100.manifest.json \
  --extract-to /srv/pre-review/imports/bizinfo-existing/extracted-100-v1
```

이미 풀어 둔 경로는 ZIP manifest 대신 내부 계약을 다시 검사한다.

```bash
backend/.venv/bin/python backend/scripts/existing_kb_pack.py \
  /srv/pre-review/imports/bizinfo-existing/extracted-100 \
  --expected-count 100
```

## 2. Storage 및 `kb.*` 적재

운영 기본 경로는 backend Compose의 `worker` 이미지를 one-shot으로 실행하는 것이다.
`backend/.env`가 컨테이너용 `host.docker.internal` 주소를 사용하므로 별도 URL 변환이
필요 없고, 데이터 경로는 절대경로를 read-only로 mount한다. 먼저 현재 소스를 image에
반영한다.

```bash
cd /path/to/repository/backend
docker compose build worker
```

먼저 `--execute` 없이 실행하면 데이터팩 전체를 다시 검증하고 선택 건수만 보여준다.

```bash
docker compose run --rm --no-deps \
  -v /srv/pre-review/imports/bizinfo-existing/extracted-100:/data:ro \
  worker python scripts/ingest_existing_profiles.py /data \
  --expected-count 100
```

연결 smoke는 첫 1건만 실제 적재한다.

```bash
docker compose run --rm --no-deps \
  -v /srv/pre-review/imports/bizinfo-existing/extracted-100:/data:ro \
  worker python scripts/ingest_existing_profiles.py /data \
  --expected-count 100 --limit 1 --execute
```

확인 후 전체를 적재한다. 앞서 넣은 1건은 같은 source/Profile SHA-256이면 재사용된다.

```bash
docker compose run --rm --no-deps \
  -v /srv/pre-review/imports/bizinfo-existing/extracted-100:/data:ro \
  worker python scripts/ingest_existing_profiles.py /data \
  --expected-count 100 --execute
```

배치 importer는 순차 처리하고 첫 오류에서 멈춘다. 성공한 앞 레코드는 commit되어 있으므로
원인을 고친 뒤 같은 명령을 다시 실행한다. 동일 데이터 재실행은 idempotent하다. 과거
importer가 generic Fact만 넣고 빠뜨린 `kb.delivery_role`과
`kb.delivery_role_organization`도 동일 Profile의 early return 전에 자연키로 보충한다.

공고 1건의 DB 변경은 하나의 transaction이다. 중간 child insert가 실패하면 해당 공고의
current flag를 포함한 DB 변경을 모두 rollback한다. Storage API와 PostgreSQL을 하나의
분산 transaction으로 묶지는 않는다. DB 실패 전에 올라간 객체가 있다면 content-addressed
key라 재실행 시 같은 객체를 안전하게 upsert하지만, 참조 없는 orphan이 남을 수 있다.
임의 삭제하지 말고 `kb.artifact.storage_object_key`에 참조되지 않는지 확인한 뒤 운영 cleanup
절차로 정리한다.

실행 결과는 레코드별 JSON 한 줄과 마지막 summary 한 줄이다. 로그가 필요하면 새 파일로
리다이렉트하되 이 로그를 ingestion input이나 데이터팩 구성요소로 다시 넣지 않는다.

## 3. 관계형 적재 확인

다음 명령은 읽기 전용이다. Profile SHA, 다섯 artifact 각각의 SHA-256·크기·bucket·
content-addressed object key, Profile의 candidate/structured artifact FK, 네 개의 lineage edge,
component, Fact, evidence/context/relation/component link, delivery role/기관,
target constraint, facet/source/value, scale/measure 수를 데이터팩과 대조한다. embedding이
아직 없으면 없어도 성공한다.

```bash
docker compose run --rm --no-deps \
  -v /srv/pre-review/imports/bizinfo-existing/extracted-100:/data:ro \
  worker python scripts/verify_existing_kb.py /data \
  --expected-count 100
```

DB row뿐 아니라 private Storage의 500개 객체 바이트도 실제로 내려받아 크기와 SHA-256을
확인하려면 다음 선택 검증을 한 번 수행한다. service-role key는 환경변수에서만 읽고 URL이나
응답 본문을 오류 결과에 출력하지 않는다.

```bash
docker compose run --rm --no-deps \
  -v /srv/pre-review/imports/bizinfo-existing/extracted-100:/data:ro \
  worker python scripts/verify_existing_kb.py /data \
  --expected-count 100 --verify-storage
```

## 4. pgvector embedding

먼저 비용 없는 dry-run으로 입력 수와 token을 확인한다. 현재 고정 팩은 모든 scope가
8,192-token 상한 안에 있고 총 100 × 4 입력을 만든다.

```bash
docker compose run --rm --no-deps \
  -v /srv/pre-review/imports/bizinfo-existing/extracted-100:/data:ro \
  worker python scripts/embed_existing_profiles.py /data \
  --dry-run
```

실제 OpenAI 호출과 `retrieval.existing_profile_embedding` 변경은 다음 명령에서만 일어난다.
동일 config/scope/input SHA-256은 건너뛴다.

실행기는 각 로컬 Profile을 한 번 읽은 동일 byte snapshot으로 SHA-256과 embedding 입력을
만든다. 그 SHA-256이 현재 `kb.profile_version.profile_sha256` 및 연결된
`structured_profile` artifact의 SHA-256과 정확히 같고, notice/source/schema identity도 DB와
일치해야 한다. 하나라도 다르면 OpenAI 호출과 embedding upsert 전에 전체 실행을 중단한다.

```bash
docker compose run --rm --no-deps \
  -v /srv/pre-review/imports/bizinfo-existing/extracted-100:/data:ro \
  worker python scripts/embed_existing_profiles.py /data
```

마지막으로 네 scope의 입력 SHA-256까지 읽기 전용으로 대조한다.

```bash
docker compose run --rm --no-deps \
  -v /srv/pre-review/imports/bizinfo-existing/extracted-100:/data:ro \
  worker python scripts/verify_existing_kb.py /data \
  --expected-count 100 --require-embeddings
```

호스트의 `backend/.venv`로 직접 실행해야 한다면 mutating/DB 명령마다 다음 옵션을
추가한다.

```text
--supabase-compose-env /path/to/repository/.runtime/supabase-dev/.env
```

이 옵션은 비밀값을 출력하지 않고 공식 Compose `.env`에서 loopback gateway와 pooler
DSN을 메모리에서 조립한다. 옵션 없이 generated `backend/.env`를 호스트 Python이 읽으면
컨테이너 전용 `host.docker.internal`이 WSL에서 resolve되지 않을 수 있다. 비밀번호가 든
DB URL과 service-role key는 CLI 인자를 지원하지 않는다. 환경변수 또는 위 compose-env
옵션으로만 전달해 process list와 shell history에 비밀값이 남지 않게 한다.

## 합성 요청서 HWPX와의 구분

`docs/pre_review_request_e2e_5_20260909_v1/generated`의 HWPX 5건은 Existing seed가 아니라
사용자 요청서 파이프라인 시험 입력이다. 자세한 주의점과 Swagger/live E2E 절차는
[`docs/pre_review_request_e2e_5_20260909_v1/README.md`](../../docs/pre_review_request_e2e_5_20260909_v1/README.md)에
있다. DB·Storage·OpenAI를 건드리지 않는 parser/preflight 검증은 다음과 같다.

```bash
PREREVIEW_FREETYPE_LIB=/lib/x86_64-linux-gnu/libfreetype.so.6 \
  backend/.venv/bin/python backend/scripts/validate_synthetic_hwpx_fixtures.py
```

이 5건은 한글에서 직접 작성한 표준 양식 호환성 증명이 아니라, 양식을 모사한 회귀
fixture다. 실제 live E2E는 별도 명령이며 OpenAI 비용과 Auth/DB/Storage 기록을 남긴다.
