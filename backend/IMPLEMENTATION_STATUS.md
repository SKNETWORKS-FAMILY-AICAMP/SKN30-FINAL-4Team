# Backend rebuild 구현 현황

마지막 갱신: 2026-09-10

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

## 완료된 기반

- self-hosted Supabase와 private buckets: `existing-kb`, `request-temp`, `analysis-reports`
- pgvector 기반 Existing Profile 임베딩: `text-embedding-3-small`, 1,536 dimensions,
  `purpose`/`target`/`support`/`combined` 네 scope
- Existing 공고 100건의 Profile·artifact·관계형 KB 적재 및 retrieval 검증
- Existing 100건 data pack의 ZIP/manifest 안전 검증, batch import, 공고별 DB transaction,
  멱등 재실행과 private Storage 실물 검증 절차
- FastAPI Supabase Auth proxy와 HttpOnly access/refresh Cookie 경계
- FastAPI HWP/HWPX 업로드: MIME/내용/50 MiB 검사, private Storage upload,
  `analysis_run`/dispatch/source artifact 생성, 보상 삭제
- FastAPI 상태 polling과 owner-scoped 결과·후보·활성 세션·이력 읽기 endpoint
- Swagger/OpenAPI 15개 경로의 named 성공 응답, 주요 오류 응답과 HttpOnly Cookie
  security scheme
- migration 21: PostgreSQL `FOR UPDATE SKIP LOCKED` polling, 30초 heartbeat,
  120초 lease, 두 번의 attempt, `ops.processing_run` fencing token
- migration 22: fenced atomic result materialisation. stale worker는 결과를 쓰지 못함.
- migration 23: retention 만료 결과를 read model에서 차단하고 SIM 후보별 evidence를 반환
- migration 24: 결과 materialisation 없이 성공 상태만 기록할 수 있던 legacy worker 완료
  함수를 제거
- same-server polling worker 조립과 CLI/Docker service
- source 다운로드 → HWP/HWPX → Common IR → Request Profile → 3축 임베딩 검색 →
  CPL/FIT/SIM → fenced 결과 저장
- Common IR/Profile을 `request-temp`와 `workspace.source_artifact`에 등록하고
  source → Common IR → Profile lineage 저장
- 비교에 사용한 정확한 Existing `profile_version_pk`를 결과까지 보존
- parser 120초 hard deadline/process-group kill, HWPX declared unpacked-size 상한,
  parser 자식 프로세스의 DB/OpenAI/Storage 비밀값 차단

## 2026-09-10 검증 결과

- 전체 backend 회귀 테스트: `111 passed`
- Supabase migration·self-hosted 설치/경로 안전성 계약 테스트: `16 passed`
- 합성 HWPX 5건 offline parser/preflight: `5/5 passed`
  - ZIP·manifest SHA-256·Common IR provenance·본문 보존
  - 각 2 Common IR blocks, `detail_program_new` 판정
- 실제 Luna Request Profile 생성: 성공, exact candidate-pack span `6/6`
- migration 01~24 실DB 적용: 성공
- queue runtime rollback 계약: 성공
- 실제 1건 E2E: 성공
  - Supabase Auth → FastAPI upload → private Storage/PostgreSQL queue
  - HWPX/Common IR/Request Profile → OpenAI embedding/pgvector → CPL/FIT/SIM
  - FastAPI polling/result read
  - CPL 13, FIT 7, SIM 후보 1, evidence 54
- E2E DB 후검증: source/Common IR/Profile artifact 3개, lineage edge 2개,
  Request Profile 1개, request fact 14개, result axis 20개, processing attempt 1개
- Existing 100건 live verifier: `valid`
  - Profile 100/100, embedding 100/100 × 4 scope
  - artifact 500개와 lineage 400개, Profile artifact FK 모두 불일치 0
  - private Storage 실제 객체 500/500의 byte SHA-256·크기 일치
  - 과거 importer가 누락한 delivery role 38행·기관 occurrence 48행 backfill 완료
- Docker 이미지 build: 성공. 최신 이미지의 `--network none` 컨테이너에서 전체 회귀
  테스트 111건과 합성 HWPX parser 5/5 성공
  (`rhwp-python` native runtime에 `libexpat1`, `libfreetype6` 필요)
- `cl100k_base` tokenizer cache를 이미지에 포함해 retrieval token 계산이 런타임
  인터넷 연결에 의존하지 않음
- 실행 중 API/worker 확인: 두 컨테이너 `Up`, live/ready 200
- 실행 중 OpenAPI 확인: 15 paths/15 operations, access·refresh Cookie security scheme,
  결과 조회 named response schema, `X-PreReview-Dev-*` 노출 0건

실행 전 DB custom-format backup은
`/tmp/pre_review_before_worker_migrations_20260910.dump`에 생성했다. `/tmp` 파일이므로
장기 보관이 필요하면 별도 영속 위치로 복사해야 한다. Existing delivery-role backfill 직전
백업은 `/tmp/pre_review_before_existing_role_backfill_20260910.dump`이며 mode `600`,
SHA-256은 `01b6c8d98d163e3e494cc99a3e0ea16f76d951d37c348cc0d66bdd02bfb2c241`다.
legacy worker 완료 함수 제거 직전의 최신 백업은
`/tmp/pre_review_before_migration_24_20260910.dump`이며 mode `600`, 크기 4,348,849 bytes,
SHA-256은 `a6f54439e3402e201fea3731c732c426ee20ca081bf71a356d941fa994f1afe6`다.

PDF/OCR은 현재 요청 처리 범위에서 제외한다. 채팅은 별도 queue/API/worker와
결과 근거 제한 로직까지 구현했으며, migration 26 적용 후 실제 LLM E2E를 확인해야 한다.
PDF 생성은 데이터 모델은 있지만 별도 queue/API 구현 전이라 E2E 완료 범위가 아니다.

현재 이 체크아웃에는 mode `600`인 `backend/.env`가 준비되어 있고 API·worker가 online으로
실행 중이다. 새 checkout/서버에서는 Supabase migration, Existing KB bootstrap과
Supabase/Auth·PostgreSQL·OpenAI server-only 환경 설정을 먼저 준비해야 한다. 환경 파일은
Git에 포함되지 않으므로 [운영 가이드](fastapi/docs/FASTAPI_WORKER_RUNBOOK.md)와
[Existing KB bootstrap 가이드](supabase/EXISTING_KB_BOOTSTRAP.md)를 따라 새로 만든다.

## 남은 작업

- 요청 업로드를 DB의 `uploading` 예약 → Storage 업로드 → 원자적 `queued` finalize로
  전환하고, 모호한 DB commit 결과의 read-back과 stale `uploading` 복구/GC 구현
- password recovery link를 HttpOnly session cookie로 교환하는 callback/PKCE 흐름
- reverse proxy/ASGI 경계의 multipart 전체 body·part 수 제한과 streaming upload
- `request-temp` 및 90일 만료 결과의 reference-aware cleanup/감사 작업
- purpose/target/support 중 일부가 없는 요청을 실패 대신 insufficient 결과로 내리는 정책
- worker heartbeat/queue lag를 포함한 배포 readiness
- OpenAI 호출 단위 `ops.model_invocation` 감사 기록 연결
  (현재는 `ops.processing_run`의 시도·성공·실패 이력만 기록)
- 기존 로컬 runtime에 남아 있을 수 있는 legacy Edge Function 제거 및 direct grant 폐기
- 실제 Hancom 작성 HWP/HWPX와 malformed/timeout 문서 E2E
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

합성 5건 offline 검증과 실제 1건 live E2E는 다음 스크립트로 재현한다. live E2E는
OpenAI 비용이 발생하고 확인용 Auth user/result를 DB에 남긴다.

```bash
PREREVIEW_FREETYPE_LIB=/lib/x86_64-linux-gnu/libfreetype.so.6 \
  backend/.venv/bin/python backend/scripts/validate_synthetic_hwpx_fixtures.py

cd backend
.venv/bin/python scripts/run_local_live_e2e.py
```

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
