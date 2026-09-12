# Backend rebuild 구현 현황

마지막 갱신: 2026-09-13

## 현재 선택한 운영 구조

```text
Frontend (HttpOnly Cookie)
  → FastAPI
      → Supabase Auth / PostgreSQL + pgvector / private Storage
      → PostgreSQL polling worker (same server)
```

Supabase는 인증·DB·벡터·Storage 인프라다. 브라우저는 Supabase나 Edge Function을 직접
호출하지 않는다. Redis/RQ, external worker HTTP dispatch/callback, SSE/Realtime은 현재
운영 경로에서 사용하지 않는다.

## 2026-09-13 통합 상태

`origin/develop`의 Model 1/2/3 결과 저장과 비동기 채팅 queue를 이 브랜치의 FastAPI·worker
경계에 통합했다. 현재 migration 번호는 Model 결과 `26`, 채팅 queue `27`, component-name
embedding v2 `28`, v2 활성화 보정 `29`, Existing KB 변경 직렬화/trigger `30`, versioned
Existing Model 1 분류 base `31`, immutable runtime identity·invalidation/promotion hardening
`32`이다.

- Existing Model 1 분류는 `retrieval.classification_configuration`과
  `retrieval.existing_profile_classification`에 immutable weight/runtime-manifest/input configuration·input SHA-256·raw label·신뢰도·
  실행 이력을 남긴다. 100건 current corpus 전체가 `OK`일 때만 configuration을 활성화한다.
  raw `판단보류`는 보존하지만 service projection에서는 지원 유형으로 사용하지 않는다.
- `serving.zip`의 model weight는 Git이 아닌 ignored runtime 경로에 배치하고 weight와
  serving/pipeline/backend runtime manifest SHA-256을 검증한 뒤 backfill한다. 정확한 환경 변수·backfill·검증 순서는
  [FastAPI·worker 운영 가이드](fastapi/docs/FASTAPI_WORKER_RUNBOOK.md)에 있다.
- 공식 `supabase/postgres:17.6.1.169` 임시 DB에서 migration `01`~`32`의 fresh apply와
  전체 replay, 기존 committed migration 31 상태에서 31·32 upgrade/replay를 검증했다.
  실제 repository SQL과 두 세션 classification/embedding invalidation의 `40001` 전체
  transaction retry도 검증했다. 다만 Existing 100건 Model 1 실제 추론 backfill,
  v2 재임베딩, 실제 ML/채팅 OpenAI E2E는 아직 실행하지 않았다. 아래 2026-09-10 결과는
  통합 전 번호 체계와 로컬 runtime에 대한 역사적 검증 기록이다.
- 기본 backend 회귀 테스트 289건과 Supabase/self-hosted/Existing Model 1 중심 계약 테스트
  85건이 통과했다. 생성된 OpenAPI는 `origin/develop`과 byte-canonical SHA-256
  `9461f69719b391ffdbec4d5f4f56011fa31079627f29ffc73a9cacccffe452b5`로 동일하다.
  Python compile, shell syntax, Compose config와 `git diff --check`도 통과했다.

## 완료된 기반

- self-hosted Supabase와 private buckets: `existing-kb`, `request-temp`, `analysis-reports`
- pgvector 기반 Existing Profile 임베딩: `text-embedding-3-small`, 1,536 dimensions,
  `purpose`/`target`/`support`/`combined` 네 scope
- migration 28의 `approved-facts-components-role-aware-v2` 조립 규칙: 승인 Fact와
  `support_components[].name_raw`를 Existing/Request 양쪽에 동일하게 반영하고,
  구 버전 벡터와의 혼용은 fail-closed
- Existing 공고 100건의 Profile·artifact·관계형 KB 적재 및 retrieval 검증
- Existing 100건 data pack의 ZIP/manifest 안전 검증, batch import, 공고별 DB transaction,
  멱등 재실행과 private Storage 실물 검증 절차
- FastAPI Supabase Auth proxy와 HttpOnly access/refresh Cookie 경계
- FastAPI HWP/HWPX 업로드: MIME/내용/50 MiB 검사, 필수 `Idempotency-Key`, DB
  `uploading` 예약 → private Storage → source artifact+`queued` 원자 확정
- commit 결과 read-back, Storage 중복·응답 유실 시 원본 byte/hash 재검증, 불명확한
  upload/finalize 시 원본 보존, `cleanup_pending` 기반 fenced 정확 경로 삭제,
  15분 만료 stale upload의 제한 batch lazy reaper
- FastAPI 상태 polling과 owner-scoped 결과·후보·활성 세션·이력 읽기 endpoint
- Swagger/OpenAPI 15개 경로의 named 성공 응답, 주요 오류 응답과 HttpOnly Cookie
  security scheme
- migration 21: PostgreSQL `FOR UPDATE SKIP LOCKED` polling, 30초 heartbeat,
  120초 lease, 두 번의 attempt, `ops.processing_run` fencing token
- migration 22: fenced atomic result materialisation. stale worker는 결과를 쓰지 못함.
- migration 23: retention 만료 결과를 read model에서 차단하고 SIM 후보별 evidence를 반환
- migration 24: 결과 materialisation 없이 성공 상태만 기록할 수 있던 legacy worker 완료
  함수를 제거
- migration 25: `queued` 전환 전에 동일 run의 source artifact와 dispatch source identity가
  일치하도록 강제하고, active source·dispatch의 핵심 메타데이터 변경을 차단
- same-server polling worker 조립과 CLI/Docker service
- 지원되는 Existing KB writer는 `scripts/ingest_existing_profile.py`(batch는 이를
  subprocess로 호출)뿐이다. 과거 `worker/kb_ingest.py`·`worker/kb_store.py`는 retired
  `app.*` 의존성을 지녀 `.dockerignore`로 runtime image에서 제외되어 현 배포 경로로는
  실행되지 않는다.
- source 다운로드 → HWP/HWPX → Common IR → Request Profile → 3축 임베딩 검색 →
  CPL/FIT/SIM → fenced 결과 저장
- Common IR/Profile을 `request-temp`와 `workspace.source_artifact`에 등록하고
  source → Common IR → Profile lineage 저장
- 비교에 사용한 정확한 Existing `profile_version_pk`를 결과까지 보존
- parser 120초 hard deadline/process-group kill, HWPX declared unpacked-size 상한,
  parser 자식 프로세스의 DB/OpenAI/Storage 비밀값 차단

## 2026-09-10 통합 전 역사적 검증 결과

아래 기록에서 언급하는 migration 26/27/28은 당시 backend-rebuild 단독 브랜치의 번호다.
현재 통합 번호로는 각각 28/29/30에 해당한다. 당시 실제 DB는 Model 결과·채팅 queue·Existing
Model 1 분류 migration을 포함하지 않았다.

- 전체 backend 회귀 테스트: `226 passed`
- Request Profile vendor 계약 테스트: `58 passed`
- Supabase migration·self-hosted 설치/경로 안전성 계약 테스트: `17 passed`
- 합성 HWPX 5건 offline parser/preflight: `5/5 passed`
  - ZIP·manifest SHA-256·Common IR provenance·본문 보존
  - 각 2 Common IR blocks, `detail_program_new` 판정
- 실제 Luna Request Profile 생성: 성공, exact candidate-pack span `6/6`
- migration 01~28 실DB 적용: 성공. 조기 활성화됐던 빈 v2 설정을 보정해 현재는
  v1 400행이 활성이고 v2는 0행·비활성이다.
- queue runtime rollback 계약: 성공
- 합성 HWPX 1건 live E2E: 성공
  - Supabase Auth → FastAPI upload → private Storage/PostgreSQL queue
  - HWPX/Common IR/Request Profile → OpenAI embedding/pgvector → CPL/FIT/SIM
  - FastAPI polling/result read
  - CPL 13, FIT 7, SIM 후보 1, evidence 47
- E2E DB 후검증: source/Common IR/Profile artifact 3개, lineage edge 2개,
  Request Profile 1개, request fact 13개, result axis 20개, processing attempt 1개
- 실제 Hancom 작성 HWP 검증: 성공
  - direct HWP → Common IR: 47 blocks(단락 39, 표 8), relation 1,
    validation error 0
  - 입력 SHA-256:
    `48b1c2909efaca3710642020901071260592fa4c9b763b905ec21c9f905b9228`
  - 최초 run은 동일 run의 두 번 attempt 모두 OpenAI HTTP 200 이후
    Pydantic/domain cross-field validation에서 `LLM_INVALID_RESPONSE`로 종료했다.
    SDK `parse`가 raw 응답을 노출하기 전에 검증해 기존 인메모리 보정 경로가
    실행되지 못한 것이 원인이었다.
  - OpenAI client를 `create` 호출 → raw JSON 인메모리 보존 → local
    `model_validate` 순서로 수정했다. 후속 반복 검증에서 strict JSON Schema로
    표현되지 않는 `delivery_relations[*].relation_container`의 kind별 상호배타
    필드가 다시 확인되어, 원격 응답의 무의미한 필드만 제거하도록 정규화했다.
    필수 근거를 만들거나 span을 변경하지 않으며 서버 materializer 검증은 그대로다.
  - 최종 live E2E run `62f26473-2b94-439a-8c00-917f840dd133`, case
    `723a7eb3-a352-4ca6-ac82-b8a3692910e9`가 첫 worker attempt에 성공
  - CPL 13, FIT 7, SIM 후보 1, evidence 63
  - source/Common IR/structured Profile artifact 각 1개, result axis 20개,
    active analysis session 1개, 종료 후 non-terminal queue 0개
- 위 HWP의 의미 품질 후검증에서 Common IR에는 명시된 `사업기간` 후보 2개와
  `추진절차` 표 1개가 있는데도 최초 Request Profile에는 둘 다 0건이고
  `identity.title_raw`도 `null`인 누락을 확인했다.
  - 연도-only 범위(`’24 ~ ’28`, `` `24~`28년 ``) 후보 규칙과 명시 `사업명` 기반
    title 복원을 추가했다. 해당 문서에서 title은 `ICT지원사업`으로 결정된다.
  - 명시적인 `추진절차`/`사업추진절차` 표와 exact `사업기간` label의 기간 후보를
    성공 전에 검사한다. 누락 시 기존 근거를 보존한 채 한 번 repair하고, 계속
    누락되면 성공 처리하지 않는다.
  - 실제 HWP Common IR에 새 검사를 적용해 누락 조건이 모두 탐지됨을 확인했고,
    관련 회귀 테스트는 통과했다. 수정 버전의 OpenAI live 재처리는 아직 수행 전이다.
- Existing 100건 관계형 KB·Storage live verifier: `valid`
  - Profile 100/100, 기존 v1 embedding 100/100 × 4 scope
  - artifact 500개와 lineage 400개, Profile artifact FK 모두 불일치 0
  - private Storage 실제 객체 500/500의 byte SHA-256·크기 일치
  - 과거 importer가 누락한 delivery role 38행·기관 occurrence 48행 backfill 완료
- migration 20 재실행은 알려진 초기 generic config만 정리하고 active v2/향후 assembly를
  보존하며, active config가 전혀 없을 때만 v1을 bootstrap한다. migration 26은 v2 설정을
  staged inactive로 추가하고 v1 config를 rollback 상태로 유지한다.
  migration 27은 이미 조기 활성화된 **active** v2 환경도 현재 Profile의 네 scope가 완전하지
  않으면 v1으로 복구하고 inactive v2/active future config는 보존한다. worker는 v2 이외의
  config와 후보 0건 모두 fail-closed 하므로 전체
  100건 v2 재임베딩의 hash 검증·원자 전환 전에는 정상 분석 결과를 만들지 않는다.
  검증 backfill도 exact v1/v2 identity가 아닌 active future/foreign config(같은 assembly
  label 재사용 포함)를 발견하면 이를 내리지 않고 명시적으로 실패하며, active config가
  전혀 없을 때만 완전 검증 v2를 활성화한다.
  migration 28은 v2 전환과 Existing current-version 변경을 동일 advisory transaction
  lock으로 직렬화하고, 전환 뒤 current Profile이 생성·변경·삭제되면 v2를 v1으로 demote한다.
  최초 완전 28 설치 또는 함수·네 개 trigger drift 재적용은 27→28 upgrade gap의
  legacy/manual child-row 변경을 안전하게 흡수하려고 active v2를 한 번 demote하며,
  정상 28 재실행은 v2를 유지한다. 전체 Profile byte/hash 재검증 backfill만 v2를
  재활성화한다.
  - v2 비용 없는 dry-run: Profile 100건, 4 scope 400입력, 총 64,973 tokens,
    scope 최대 2,906 tokens로 모두 8,192 상한 안에 있음
- Docker 이미지 build: 성공. 최신 이미지의 `--network none` 컨테이너에서 전체 회귀
  테스트 210건과 합성 HWPX parser 5/5 성공
  (`rhwp-python` native runtime에 `libexpat1`, `libfreetype6` 필요)
- `cl100k_base` tokenizer cache를 이미지에 포함해 retrieval token 계산이 런타임
  인터넷 연결에 의존하지 않음
- 최신 이미지로 API/worker 강제 재생성 후 확인: 두 컨테이너 `Up`, API는
  `0.0.0.0:8001`에 publish, live/ready 각각 200
- 실행 중 OpenAPI 확인: 15 paths/15 operations, access·refresh Cookie security scheme,
  결과 조회 named response schema, `POST /analysis-runs`의 필수 UUID v4
  `Idempotency-Key`, `X-PreReview-Dev-*` 노출 0건

실행 전 DB custom-format backup은
`/tmp/pre_review_before_worker_migrations_20260910.dump`에 생성했다. `/tmp` 파일이므로
장기 보관이 필요하면 별도 영속 위치로 복사해야 한다. Existing delivery-role backfill 직전
백업은 `/tmp/pre_review_before_existing_role_backfill_20260910.dump`이며 mode `600`,
SHA-256은 `01b6c8d98d163e3e494cc99a3e0ea16f76d951d37c348cc0d66bdd02bfb2c241`다.
legacy worker 완료 함수 제거 직전 백업은
`/tmp/pre_review_before_migration_24_20260910.dump`이며 mode `600`, 크기 4,348,849 bytes,
SHA-256은 `a6f54439e3402e201fea3731c732c426ee20ca081bf71a356d941fa994f1afe6`다.
migration 25 최종 적용 직전의 최신 백업은
`/tmp/pre_review_before_migration_25_final_20260910_01.dump`이며 mode `600`,
크기 4,354,891 bytes, SHA-256은
`265d0c3d5a060c2ed717139ed7e6f2459572f71e9efbe50d0997eb11936b48db`다.
DB container의 `pg_restore --list`로 custom-format listing도 확인했다.

PDF/OCR은 현재 요청 처리 범위에서 제외한다. 채팅은 별도 queue/API/worker와
결과 근거 제한 로직까지 구현했으며, 현재 번호의 migration 27 적용 후 실제 LLM E2E를
확인해야 한다.
PDF 생성은 데이터 모델은 있지만 별도 queue/API 구현 전이라 E2E 완료 범위가 아니다.

현재 이 체크아웃에는 mode `600`인 `backend/.env`가 준비되어 있고 API·worker가 online으로
실행 중이다. 새 checkout/서버에서는 Supabase migration, Existing KB bootstrap과
Supabase/Auth·PostgreSQL·OpenAI server-only 환경 설정을 먼저 준비해야 한다. 환경 파일은
Git에 포함되지 않으므로 [운영 가이드](fastapi/docs/FASTAPI_WORKER_RUNBOOK.md)와
[Existing KB bootstrap 가이드](supabase/EXISTING_KB_BOOTSTRAP.md)를 따라 새로 만든다.

## 남은 작업

- Existing Profile 100건을 `approved-facts-components-role-aware-v2`로 재임베딩하고
  scope별 100건(총 400행) 및 live retrieval을 다시 검증
- migration 31·32를 적용한 뒤 Existing current Profile 100건의 Model 1 분류 backfill과
  active-configuration gate를 검증
- 통합된 Model 1/2/3 결과 저장과 채팅 worker를 실제 DB/LLM으로 각각 E2E 검증
- 수정된 Request Profile v0.1.3으로 실제 Hancom HWP live E2E를 재실행해 사업명,
  사업기간, 추진절차, 목적·지원 컴포넌트·delivery relation의 의미 완전성을 재점검
- password recovery link를 HttpOnly session cookie로 교환하는 callback/PKCE 흐름
- reverse proxy/ASGI 경계의 multipart 전체 body·part 수 제한과 streaming upload
- `request-temp` 및 90일 만료 결과의 reference-aware cleanup/감사 작업. 현재 요청 경로의
  stale lazy reaper는 별도 scheduler·cleanup lease로 분리해 업로드 지연을 제거해야 함
- terminal idempotency replay의 HTTP 상태(`200`/`202`) 정규화
- purpose/target/support 중 일부가 없는 요청을 실패 대신 insufficient 결과로 내리는 정책
- worker heartbeat/queue lag를 포함한 배포 readiness
- OpenAI 호출 단위 `ops.model_invocation` 감사 기록 연결
  (현재는 `ops.processing_run`의 시도·성공·실패 이력만 기록)
- 기존 로컬 runtime에 남아 있을 수 있는 legacy Edge Function 제거 및 direct grant 폐기
- 실제 Hancom 작성 HWPX와 malformed/timeout 문서 E2E
- 임의의 새 Existing data pack에 vendor v0.2 의미 검증과 관계형 row fingerprint 대조 확대
- 이미 저장된 과거 non-current Existing Profile을 재입력할 때 fail-closed할지 current로
  원자 전환할지 정책 확정

## 검증 순서

정적 계약 테스트:

```bash
cd backend
UV_CACHE_DIR=/tmp/pre_review_uv_cache uv run --extra dev pytest -q

UV_CACHE_DIR=/tmp/pre_review_uv_cache uv run --extra dev pytest -q \
  supabase/tests/test_migration_contract.py \
  supabase/tests/test_selfhosted_scripts.py
```

Supabase migration 적용과 queue runtime 검증은 실제 DB를 쓰므로 현재 self-hosted runtime을
대상으로만 실행한다. 데이터 초기화 명령은 사용하지 않는다.

```bash
cd /path/to/SKN30-FINAL-4Team
SUPABASE_DIR="$PWD/.runtime/supabase-dev" backend/supabase/apply_migrations.sh
SUPABASE_DIR="$PWD/.runtime/supabase-dev" backend/supabase/run_worker_queue_validation.sh
```

실제 비밀값을 출력·커밋하지 말고, migration 적용 전에는 백업과 Compose volume 경로를
확인한다.

합성 5건 offline 검증과 HWP/HWPX live E2E는 다음 스크립트로 재현한다.
live E2E는 OpenAI 비용이 발생하고 확인용 Auth user/result를 DB에 남긴다.

```bash
PREREVIEW_FREETYPE_LIB=/lib/x86_64-linux-gnu/libfreetype.so.6 \
  backend/.venv/bin/python backend/scripts/validate_synthetic_hwpx_fixtures.py

cd backend
.venv/bin/python scripts/run_local_live_e2e.py

# 지정한 HWP 또는 HWPX로 검증
.venv/bin/python scripts/run_local_live_e2e.py --file /safe/local/request.hwp
```

live E2E 스크립트는 자신이 생성한 동일 run만 점유하고 DB queue 계약과
같은 최대 두 번의 attempt를 수행한다. 첫 시도가 retryable failure로
`queued`에 복귀하면 두 번째 시도까지 이어가고, 성공·최종 실패·시도
소진 중 하나로 유한하게 종료한다. 자세한 운영 주의사항은
[FastAPI·worker 운영 가이드](fastapi/docs/FASTAPI_WORKER_RUNBOOK.md)를 따른다.

## online FastAPI 설정

`backend/.env.example`을 복사해 서버 전용 `.env`에 설정한다.

```text
PREREVIEW_OFFLINE_MODE=false
PREREVIEW_API_BIND_ADDRESS=127.0.0.1
PREREVIEW_AUTH_ALLOWED_ORIGINS=https://frontend.example
PREREVIEW_AUTH_COOKIE_SECURE=true
SUPABASE_URL=http://host.docker.internal:8000
SUPABASE_ANON_KEY=<server-only>
SUPABASE_SECRET_KEY=<server-only>
DATABASE_URL=<server-only>
OPENAI_API_KEY=<worker-only>
```

FastAPI는 결과 조회에 DB URL, 업로드에 service/secret key를 사용한다. worker는
DB URL·Storage server credential·OpenAI key를 사용한다. 어느 값도 브라우저나 Git에
전달하지 않는다.

## 레거시 주의

- `backend/supabase/functions/`와 `WORKER_API_CONTRACT.md`: 과거 Edge dispatch/callback
  참고용이며 새 경로에서는 미호출. 기존 로컬 runtime에는 예전 배포물이 남아 있을 수 있다.
- `backend/app/api/v1/routes.py`: 이전 in-memory `/requests` 계약. 현재 router에 mount하지 않음.
- `backend/worker`의 옛 SQLAlchemy/삭제된 `app.*` 의존 모듈은 현재 `worker.main`에서
  도달하지 않으며 Docker image에서 제외된다. 호환 계층으로 되살리지 말고 별도 정리한다.
- migration 18의 unfenced result callback: migration 22 이후 새 worker에서 사용 금지.
