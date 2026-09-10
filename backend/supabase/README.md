# Self-hosted Supabase 운영 안내

이 디렉터리는 PreReview용 self-hosted Supabase의 migration, pgvector Compose override,
Storage 정책과 검증 도구를 관리한다. 실행 중 DB·Storage 객체·Docker volume·실제 `.env`
비밀값은 Git에 넣지 않는다.

## 현재 구조

```text
Frontend ──Cookie──→ FastAPI
                       ├─ Supabase Auth
                       ├─ Postgres + pgvector
                       └─ private Storage
                              ↑
                     same-server polling worker
```

Supabase Studio는 운영자용 도구일 뿐 프론트 업무 API가 아니다. 브라우저는 Auth/Storage/
PostgREST/Realtime/Edge Function을 직접 호출하지 않는다. FastAPI는 공개 경계,
PostgreSQL polling worker는 장시간 분석 경계다.

`functions/`의 Edge Function 소스는 과거 외부 worker dispatch/callback 대안이다. 현재
배포하거나 `ANALYSIS_WORKER_*` 환경변수를 설정하지 않는다. 자세한 경계는
[functions/README.md](functions/README.md)를 참고한다.

## Fresh clone 재현성 상태

공식 bundle 준비는 활성 installer로 재현할 수 있다. 이 스크립트는
`self-hosted/v0.8.0`을 sparse clone하되 예상 commit SHA와 정확히 일치하는지 먼저
검증하고, 새 target에서 공식 key 생성 script를 조용히 실행한 뒤 pgvector override와
resolved commit 기록을 설치한다. tag가 같은 이름으로 이동하면 설치는 fail-closed한다.

```bash
cd /path/to/repository
backend/supabase/install_selfhosted_local.sh
```

기본 target은 Git에서 제외되는 `/path/to/repository/.runtime/supabase-dev`다. 저장소 밖의
새 절대경로를 쓰려면 다음처럼 지정한다.

```bash
backend/supabase/install_selfhosted_local.sh \
  --target /srv/pre-review/supabase-dev
```

사전 조건은 `git`, `openssl`, `realpath`, Docker Engine과 Compose plugin, GitHub 접근이다.
새 auth key 생성에는 Node.js 16 이상 또는 실행 중인 Docker daemon이 필요하다.
스크립트는 repository working tree 안에서는 `.runtime` 하위만 허용하고 symlink·광범위
경로·기존 non-empty target을 거부한다. secret 값은 출력하지 않으며 최종 `.env`는 mode
`600`이다. staging 검증이 끝나기 전에는 target을 채우지 않는다.

이 installer는 **container 기동, migration, DB reset, data seed를 하지 않는다.** 생성된
`.env`의 URL/SMTP 설정을 검토한 뒤 아래 순서로 진행한다.

```bash
cd /path/to/repository/.runtime/supabase-dev
docker compose -f docker-compose.yml -f docker-compose.pgvector.yml up -d
docker compose ps

SUPABASE_DIR="$PWD" \
  /path/to/repository/backend/supabase/apply_migrations.sh

SUPABASE_DIR="$PWD" \
  /path/to/repository/backend/supabase/run_worker_queue_validation.sh
```

`docker compose ps`에서 최소 DB가 healthy가 된 뒤 migration을 적용하고, Auth·Storage까지
healthy인지 확인한 뒤 FastAPI를 연결한다.

여기까지는 빈 application DB다. `apply_migrations.sh`는 schema/bucket/config row만 만들며
Existing KB object/profile/embedding을 seed하지 않는다. 저장소 밖의 고정 100건 ZIP,
checksum manifest, 안전한 batch importer와 후검증 순서는
[Existing KB 100건 bootstrap 가이드](EXISTING_KB_BOOTSTRAP.md)에 정리했다. 새 환경의 전체
FastAPI+worker live E2E 전에는 이 data bootstrap을 별도로 한 번 수행해야 한다.

handover 아래의 과거 `install_supabase.sh`는 현재 installer가 아니다. 현재 pgvector
override·migration 01~24·same-server worker와 묶어 검증된 위 스크립트만 사용한다.

## 데이터 위치

| 대상 | 위치 | 접근 주체 |
|---|---|---|
| PostgreSQL / pgvector | `SUPABASE_DB_DATA_DIR` | FastAPI·same-server worker |
| 파일 기반 Storage 객체 | 실제 Compose의 `volumes/storage` bind 경로 | FastAPI·worker |
| Existing 공고 원본·IR·Profile | private `existing-kb` | trusted importer·worker |
| 요청 원본·Common IR·Request Profile | private `request-temp` | FastAPI·worker |
| PDF 보고서 | private `analysis-reports` | FastAPI·PDF worker(추후) |

벡터는 같은 PostgreSQL의 `vector` extension과 `retrieval` schema를 사용한다. Existing
Profile만 `purpose`, `target`, `support`, `combined` 네 scope로 영속화한다. 요청 Profile은
같은 조립 규칙으로 worker 메모리에서 임베딩·검색한 뒤 폐기한다.

- embedding: OpenAI `text-embedding-3-small`, 1,536차원, cosine
- 입력: `identified`, `partial`, `partially_identified` Fact의 승인된 `value_raw`
- 상한: scope당 8,192 tokens. 초과 시 Fact/줄 경계 chunk와 token-weighted average 사용
- `2,048`은 token 상한이 아니라 API 입력 배열 수에 관한 과거 혼동값이다.

## DB queue

migration 21~24는 Redis/RQ 없이 PostgreSQL을 durable queue와 fenced 결과 저장 경계로 쓴다.

| 영역 | 역할 |
|---|---|
| `workspace.analysis_run` | 브라우저가 FastAPI를 통해 polling하는 lifecycle |
| `workspace.analysis_run_dispatch` | source object, lease, heartbeat, claim 등 private worker state |
| `ops.processing_run` | 실행 시도 감사와 fencing token |
| `result.*` | 90일 read window의 분석 결과; 물리 cleanup은 미구현 |

worker는 `workspace.claim_next_analysis_run()`을 polling한다. `FOR UPDATE SKIP LOCKED`,
30초 heartbeat, 120초 lease, 최대 두 번 시도가 기본이다. 완료는
`workspace.persist_analysis_result_core()`로만 수행한다. 이 함수는 live
`processing_run_pk` fence를 검증한 뒤 결과 materialisation과 run 성공 전이를 한
transaction으로 처리한다. stale worker는 `NULL`을 받아 결과를 바꾸지 못한다.
SIM 후보는 논리 `source_profile_id`뿐 아니라 retrieval에서 실제 읽은
`profile_version_pk`도 완료 payload에 포함한다. 분석 중 KB의 current version이
바뀌어도 DB는 비교한 exact version에 결과·metadata·evidence를 연결한다.

## EC2 최초 배포

환경 파일은 역할이 다르므로 서로 바꾸어 쓰지 않는다.

| 파일 | 역할 |
|---|---|
| `/srv/pre-review/supabase/.env` | 공식 Supabase Compose의 DB/JWT/SMTP 운영 설정 |
| `backend/supabase/.env` | `prepare_selfhosted.sh`가 읽는 호스트 영속 경로 설정 |
| `backend/.env` | FastAPI·same-server worker 런타임 설정 |

세 파일 모두 권한을 `600`으로 제한한다. `prepare_selfhosted.sh`는 두 경로 변수와 선택적
Storage 경로만 source하며, `backend/supabase/.env`의 값은 Docker Compose나 FastAPI에
자동 전달되지 않는다. 이 파일은 dotenv parser가 아니라 shell `source`로 읽으므로 신뢰한
파일만 전달하고 값에 shell command를 넣지 않는다.

1. Docker Engine·Compose plugin을 설치하고, DB/Storage용 영속 볼륨 위치를 정한다.
   EC2 외부 관리가 필요하면 Tailscale은 운영 접근 경로로만 사용한다.
2. 공식 self-hosted Supabase Compose 배포본을 Git 밖 경로(예: `/srv/pre-review/supabase`)에
   설치하고 해당 `.env`에 Supabase 운영 비밀값을 설정한다.
3. 이 저장소의 환경 예시를 복사한다.

```bash
cd /path/to/repository
cp backend/supabase/.env.example backend/supabase/.env
```

```text
SUPABASE_COMPOSE_DIR=/srv/pre-review/supabase
SUPABASE_DB_DATA_DIR=/data/pre-review/postgres
SUPABASE_STORAGE_DATA_DIR=
```

`SUPABASE_DB_DATA_DIR`는 EBS 같은 영속 볼륨의 전용 빈 경로여야 한다. `/`, 저장소,
다른 DB 경로를 지정하지 않는다. 현재 `prepare_selfhosted.sh`가 자동으로 연결하는 것은
DB 경로뿐이다. `SUPABASE_STORAGE_DATA_DIR`는 경로를 준비하지만 공식 Compose의
`volumes/storage`에 자동 연결하지 않는다. 파일 기반 Storage를 별도 EBS에 둘 때는
데이터 생성 전에 EBS를 실제 `volumes/storage` 경로에 mount하거나 검증한 Compose bind
설정을 추가해야 한다. 기존 `volumes/storage`가 비어 있지 않다면 먼저 백업·이관한다.
스크립트는 DB·선택적 Storage 경로가 절대경로이고 symlink가 아닌지 검사하고, `/` 같은
광범위 경로, Compose tree, 저장소의 비-`.runtime` 경로, DB/Storage 간 상호 중첩을
거부한다. 기존 데이터 디렉터리의 mode는 바꾸지 않는다. 이 검사는 Storage를 Compose에
연결해 주지는 않으므로 빈 값이 기본이며 실제 mount 설계를 확정한 뒤에만 전용 경로를
입력한다.
DB 디렉터리는 스크립트를 실행한 사용자 소유로 생성된다. 기동 전에 실제 Supabase DB
컨테이너 UID/GID가 해당 경로를 읽고 쓸 수 있는지 확인하며, 임의의 `chmod 777`로 해결하지
않는다.

4. 안전하게 persistent path를 준비한다.

```bash
backend/supabase/prepare_selfhosted.sh backend/supabase/.env
```

5. pgvector image override를 설치한다.

```bash
cp backend/supabase/docker-compose.pgvector.override.yml.example \
  /srv/pre-review/supabase/docker-compose.pgvector.yml
```

override의 기본 이미지는 digest로 고정되어 있다. 다른 이미지를 쓸 때의
`SUPABASE_POSTGRES_PGVECTOR_IMAGE`는 `backend/supabase/.env`가 아니라 **공식 Supabase
Compose 디렉터리의 `.env`**에 둔다. 기존 DB에는 현재 PostgreSQL major version과 맞는
이미지만 사용한다.

6. override를 포함해 기동하고 migration을 적용한다.

```bash
cd /srv/pre-review/supabase
docker compose -f docker-compose.yml -f docker-compose.pgvector.yml up -d
docker compose ps

SUPABASE_DIR=/srv/pre-review/supabase \
  /path/to/repository/backend/supabase/apply_migrations.sh
```

`apply_migrations.sh`는 별도 migration ledger 없이 `01`~`24` 파일을 매번 전부 순서대로
실행한다. 각 파일은 개별 transaction이므로 중간 실패 시 앞 파일은 이미 commit되어 있다.
DB reset/삭제는 하지 않지만 모든 재실행 조합을 자동 검증하지도 않는다. 최초 적용 또는
명시적 repair 때만 사용하고, 먼저 staging에서 같은 Supabase/image 조합으로 검증한 뒤
`pg_dump -Fc` backup과 적용 Git SHA를 기록한다.

migration 11이 세 private bucket row를 만든다. Dashboard/curl로 같은 bucket을 별도 생성할
필요는 없으며, 적용 뒤 `storage.buckets`의 `public=false`와 Storage API health를 확인한다.

### pgvector 확인

```bash
cd /srv/pre-review/supabase
docker compose -f docker-compose.yml -f docker-compose.pgvector.yml \
  exec -T db psql -U postgres -d postgres \
  -c "SELECT extname, extversion FROM pg_extension WHERE extname = 'vector';"
```

기존 DB의 image 교체 전에는 `pg_dump -Fc` backup을 만든다. 기존과 같은 DB data
volume을 유지하고, PostgreSQL major version이 다른 image에 기존 volume을 붙이지 않는다.

## FastAPI·worker server 설정

FastAPI와 worker의 server-only `.env`는 `backend/.env.example`을 기준으로 한다.

```text
PREREVIEW_OFFLINE_MODE=false
PREREVIEW_AUTH_ALLOWED_ORIGINS=https://frontend.example
SUPABASE_URL=http://host.docker.internal:8000
SUPABASE_ANON_KEY=<server-only>
SUPABASE_SECRET_KEY=<server-only>
DATABASE_URL=<server-only>
OPENAI_API_KEY=<worker-only>
PREREVIEW_WORKER_HEARTBEAT_SECONDS=30
PREREVIEW_WORKER_LEASE_SECONDS=120
PREREVIEW_WORKER_PARSE_TIMEOUT_SECONDS=120
```

FastAPI는 Auth 검증·private upload·owner-scoped 결과 조회에, worker는 DB queue·Storage
I/O·OpenAI 분석에 이를 사용한다. 브라우저, Git, 로그에는 넣지 않는다.

저장소의 `backend/compose.yaml`에서는 API와 worker가 기본으로 함께 기동되고 worker는
포트를 publish하지 않는다. 배포 gate는 `/health/ready`만 사용하지 말고 worker container
상태와 최근 `ops.processing_run`/queue 지연도 확인한다.

`.env` 작성부터 Docker 기동·재생성·로그·WSL/EC2 공개 방식까지의 실제 명령은
[FastAPI·worker 운영 가이드](../fastapi/docs/FASTAPI_WORKER_RUNBOOK.md)를 따른다.

## Existing KB bootstrap 및 embedding

100건 ZIP 검증·안전한 추출·private Storage/`kb.*` 일괄 적재·관계형 후검증·embedding과
최종 4-scope 검증의 전체 순서는
[Existing KB 100건 bootstrap 가이드](EXISTING_KB_BOOTSTRAP.md)를 따른다. 아래는 이미
관계형 적재가 끝난 Profile에 대한 embedding 명령만 요약한 것이다.

dry-run은 DB나 OpenAI API를 호출하지 않는다.

```bash
cd /path/to/repository
backend/.venv/bin/python backend/scripts/embed_existing_profiles.py \
  /srv/pre-review/imports/bizinfo-existing/extracted-100 --dry-run
```

실제 적재는 trusted server 환경변수로 DB URL과 OpenAI key를 제공한다. 값은 명령행에
넣지 않는다. generated `backend/.env`는 컨테이너용이므로 운영 기본은 bootstrap 가이드의
Compose one-shot 명령이다.

```bash
cd /path/to/repository/backend
docker compose run --rm --no-deps \
  -v /srv/pre-review/imports/bizinfo-existing/extracted-100:/data:ro \
  worker python scripts/embed_existing_profiles.py /data
```

이 명령은 Existing Profile을 처음 적재하지 않는다. `kb.profile_version`과 대응
`structured_profile.v0.2.json`이 이미 있어야 하며, 위 `/srv/.../extracted-100`은 저장소에
포함된 경로가 아니다. 먼저 bootstrap 가이드의 manifest 검증과 batch import를 완료한다.

## 검증과 트러블슈팅

```bash
cd /path/to/repository/backend
UV_CACHE_DIR=/tmp/pre_review_uv_cache uv run --extra dev \
  pytest supabase/tests/test_migration_contract.py -q

SUPABASE_DIR=/path/to/supabase-compose \
  ./supabase/run_worker_queue_validation.sh
```

- 재기동 뒤 DB가 비어 있으면 `SUPABASE_DB_DATA_DIR`와 `volumes/db/data` 경로가 바뀌지
  않았는지 확인한다. 새 빈 directory를 붙이면 PostgreSQL은 새 DB를 초기화한다.
- `CREATE EXTENSION vector` 실패 시 pgvector override가 실제 Compose command에 포함됐는지
  확인한다. 실행 중 컨테이너 안에 수동 설치하지 않는다.
- worker가 일을 받지 못하면 worker ID, `claim_next_analysis_run` polling, DB network와
  lease 설정을 확인한다. Edge callback URL/token은 현재 원인이 아니다.
