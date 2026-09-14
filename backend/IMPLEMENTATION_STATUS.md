# Backend rebuild 구현 현황

마지막 갱신: 2026-09-14

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

## 2026-09-14 통합 상태

`origin/develop`의 Model 1/2/3 결과 저장과 비동기 채팅 queue를 이 브랜치의 FastAPI·worker
경계에 통합했다. 현재 migration 번호는 Model 결과 `26`, 채팅 queue `27`, component-name
embedding v2 `28`, v2 활성화 보정 `29`, Existing KB 변경 직렬화/trigger `30`, versioned
Existing Model 1 분류 base `31`, immutable runtime identity·invalidation/promotion hardening
`32`, 현재 코드용 inactive runtime configuration 등록은 `38`이며 v0.2 FastAPI
lifecycle/result/retrieval/chat/admission은 `33`~`37`, atomic upload finalization은
`39`, 실행 시도별 embedding provenance는 `40`이다.

- Existing Model 1 분류는 `retrieval.classification_configuration`과
  `retrieval.existing_profile_classification`에 immutable weight/runtime-manifest/input configuration·input SHA-256·raw label·신뢰도·
  실행 이력을 남긴다. 100건 current corpus 전체가 `OK`일 때만 configuration을 활성화한다.
  raw `판단보류`는 보존하지만 service projection에서는 지원 유형으로 사용하지 않는다.
- `serving.zip`의 model weight는 Git이 아닌 ignored runtime 경로에 배치하고 weight와
  serving/pipeline/backend runtime manifest SHA-256을 검증한 뒤 backfill한다. 정확한 환경 변수·backfill·검증 순서는
  [FastAPI·worker 운영 가이드](fastapi/docs/FASTAPI_WORKER_RUNBOOK.md)에 있다.
- 공식 `supabase/postgres:17.6.1.169` 임시 DB의 migration `01`~`38` fresh 검증 상태에
  `39`~`40` upgrade를 이어 적용하고, 전체 `01`~`40` replay와 현재 로컬 DB 적용을
  검증했다. `01`~`32` fresh apply와 기존 committed migration 31
  상태에서 31·32 upgrade/replay는 통합 전의 역사적 검증 기록으로 보존한다.
  실제 repository SQL과 두 세션 classification/embedding invalidation의 `40001` 전체
  transaction retry도 검증했다. Existing 100건 Model 1 실제 추론 backfill은 100건
  모두 `execution_status=OK`로 완료했고, 재실행은 100건 모두 skip되어 idempotency도
  확인했다. 활성 분류 결과는 `신뢰` 99건, raw `판단보류` 1건이다. 로컬 DB의 v2
  재임베딩은 100건 × 4 scope(400행)를 완료해
  `approved-facts-components-role-aware-v2` 한 개가 활성 상태이고, 이전 설정의 400행은
  비활성 상태로 보존돼 있다. 아래 2026-09-10 결과는 통합 전 번호 체계와 로컬 runtime에
  대한 역사적 검증 기록이다.
- 전체 backend pytest 906개를 수집해 `903 passed, 3 skipped`로 통과했다. 기본
  `testpaths` 밖의 Supabase migration contract wrapper도 별도로 실행해 내부 정적 계약
  24개를 모두 통과했다. Python compile, 응답 계약 JSON, Compose config와
  `git diff --check`도 통과했다. OpenAPI는 breaking v0.2 계약을 명시적으로 표시한다.
- Docker 배포 구조는 analysis `worker`만 CPU 전용 ML 이미지로 분리하고, API와
  `chat-worker`에는 ML runtime을 넣지 않는다. Model 1은 Git/image 밖의 검증된
  read-only bind mount, Model 2/3은 image-local artifact·venv로 구성하며 startup에서
  Model 1/2/3 SHA-256과 manifest를 fail-closed로 확인하도록 구현됐다.
- 2026-09-14 현재 v0.2 코드로 Docker external live E2E를 실제 DB와 OpenAI에 대해
  다시 완주했다.
  - run `08614411-f17d-42f0-93aa-ece5dff0447d`, case
    `af973b50-a90e-4b52-ba7b-83a26f9c4b55`
  - analysis worker `45d192c4cf53:1:2a96831501b5`, chat worker
    `5da52107c5a5:1:816e7882309e`가 각각 한 번의 DB queue attempt로 완료됐다.
  - Request Profile은 Terra, CPL/FIT/SIM/채팅은 Luna, embedding은
    `text-embedding-3-small`을 사용했고 Model 1/2/3은 모두 `OK`였다.
  - CPL 13, FIT 7, SIM 후보 5, evidence 107개와 completed chat/reference 10개를
    확인했다. 후보 상세의 Request/Existing 근거 연결, active session의 history 제외,
    close 뒤 history 편입과 chat history 보존도 함께 통과했다.
  - 입력은 `samples/hwpx/mockup_08_CPL전항목_스마트기술사업화.hwpx`이며 trace는
    `/tmp/prereview-e2e-v02-20260914-fullgreen`에 생성됐다. trace는 임시 운영 산출물로
    Git에 포함하지 않는다.
  - 별도 생성 fixture `01_유니콘브릿지_기술금융_사전협의요청서.hwpx`는 run
    `fb049e4b-3ebe-4b6d-9203-1ab76b91a18c`, case
    `3f703493-79ef-4489-abf1-b5c0b7ed5231`에서 Model 1/2 `OK`, Model 3
    `INPUT_EVIDENCE_MISSING`으로 acceptance 실패했다. 문서에 금액·수량 원문 근거가
    부족한 fixture 특성에 따른 기대 가능한 거부이며, 인프라 실패와 구분된다.
- 2026-09-13 전용 Docker worker external live E2E를 실제 DB와 OpenAI로 완료했다.
  - 최신 run `f3e3c8c1-9988-4db2-8f6b-bdbed6472399`, case
    `c05d9ae0-d279-4839-8a86-102da0be18fd`
  - analysis worker `4d6aae5d4c87:1:540b85e666be`, chat worker
    `ea084ec9c993:1:c39815e56162`가 각각 한 번의 DB queue attempt로 완료됐다.
    실행 image는 analysis worker `sha256:12adb17d…`, chat worker
    `sha256:f0669e6e…`였다.
  - Request Profile은 Terra, FIT/SIM과 결과 근거 기반 채팅은 Luna를 사용했다.
    Model 1/2/3 DB status는 모두 `OK`였다.
  - 결과는 CPL 13, FIT 7, SIM 후보 5, evidence snapshot 167개였다. 채팅은
    `completed`이고 case-scope evidence reference 13개를 저장했다.
    입력은 `samples/hwpx/mockup_08_CPL전항목_스마트기술사업화.hwpx` 합성 fixture였다.
  - Request Profile의 worker 로그는 최초 호출과 수정 호출 2회, 총 3회의 Terra 호출을
    보여준다. 성공 run 로그 자체는 validation 상세를 노출하지 않는다. 동일 입력 별도
    진단에서는 `f_scale_count`의 모호한 legacy anchor를 `value_span_candidate_id`로
    특정하는 보정, 이어 `stage_support`에 컴포넌트 범위의 수혜자·자격·참여 조건 경계를
    선택하는 보정이 순차 확인됐다. 이 때문에 호출별 timeout 120초와 수정 한도 2를
    사용하며, 이는 DB queue attempt 1회와 별개의 내부 구조화 호출 한도다. 같은 bounded
    repair 상한은 FIT·SIM에도 적용된다.
  - 리뷰 후 strict worker의 effective UID/GID 0 거부만 추가해 analysis image를
    `sha256:0847b036…`로 재빌드했다. 이 이미지는 `1000:1000` 기동·ML preflight와
    강제 `0:0` 실행의 exit code 2를 확인했다. OpenAI 전체 E2E는 직전
    `sha256:12adb17d…`에서 수행했으며, 후속 변경은 provider/pipeline 경로를 바꾸지 않는다.
  - 별도 제공된 `docs/pre_review_request_e2e_5_20260909_v1/generated`의 합성 HWPX
    5개도 같은 worker image의 실제 parser에서 각각 Common IR 2 blocks, schema error
    0건으로 확인했다. 이 5개는 금액이 미정이라 ML 3축 acceptance 입력으로 쓰지 않았다.
- 2026-09-13 합성 HWPX host inline live E2E는 실제 DB와 OpenAI를 통해 완료했다.
  - run `5e51dae9-3c6e-4ed8-b4c6-96185917b08b`, case
    `2d02ae97-85f0-4678-a9fe-e006ab389bd1`
  - Request Profile은 Terra, FIT/SIM과 결과 근거 기반 채팅은 Luna를 사용했다.
    analysis worker와 chat worker는 각각 한 번의 attempt로 완료했고, 저장된 ML 1/2/3
    상태는 모두 `OK`였다.
  - CPL 13, FIT 7, SIM 후보 1, evidence 77, chat reference 13을 확인했다. 이 검증은
    `samples/hwpx/mockup_08_CPL전항목_스마트기술사업화.hwpx`
    (SHA-256 `0054617fb553125e2b701d7ff9b37612048d95ab4d19ee3717a89bedd42e7ebb`)
    한 건의 범위이며 실제 Hancom 작성 HWP/HWPX의 완전 재검증을 뜻하지 않는다.

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
- same-server PostgreSQL polling worker 조립, API/chat-worker 기본 이미지와 전용 CPU ML
  worker Docker service 분리
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
- 전체 multipart HTTP body cap(`PREREVIEW_HTTP_MAX_BODY_BYTES`), 파일 cap
  (`PREREVIEW_UPLOAD_MAX_BYTES`), Uvicorn replica concurrency limit, analysis/chat 공용
  queue admission cap과 full 시 `503`
- live E2E 실행 manifest(입력 SHA-256, Git commit, dirty 여부와 source-state SHA-256,
  실제 worker/image identity, worker별 model/embedding configuration)와 성공·ML acceptance
  실패 trace artifact 보존

## 2026-09-10 통합 전 역사적 검증 결과

아래 기록에서 언급하는 migration 26/27/28은 당시 backend-rebuild 단독 브랜치의 번호다.
현재 통합 번호로는 각각 28/29/30에 해당한다. 당시 실제 DB는 Model 결과·채팅 queue·Existing
Model 1 분류 migration을 포함하지 않았다.

- 전체 backend 회귀 테스트 통과
- Request Profile vendor 계약 테스트 통과
- Supabase migration·self-hosted 설치/경로 안전성 계약 테스트 통과
- 합성 HWPX offline parser/preflight 통과
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
  테스트와 합성 HWPX parser 5/5 성공
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

채팅은 migration 27의 별도 queue와 migration 36의 v2 idempotency/claim, migration 37의
공용 admission, owner-scoped API, 결과 근거 제한 handler와 worker로 구현되어 있으며,
위 2026-09-13 합성 HWPX live E2E에서 실제 LLM 완료와 reference 저장을
확인했다. PDF/OCR은 현재 요청 처리 범위에서 제외하며, PDF 생성도 E2E 완료 범위가 아니다.

위 로컬 검증 환경에는 mode `600`인 `backend/.env`와 online API·worker가 준비되어 있었다.
환경 파일과 실행 상태는 Git 산출물이 아니다. 새 checkout/서버에서는 Supabase migration, Existing KB bootstrap과
Supabase/Auth·PostgreSQL·OpenAI server-only 환경 설정을 먼저 준비해야 한다. 환경 파일은
Git에 포함되지 않으므로 [운영 가이드](fastapi/docs/FASTAPI_WORKER_RUNBOOK.md)와
[Existing KB bootstrap 가이드](supabase/EXISTING_KB_BOOTSTRAP.md)를 따라 새로 만든다.

## 남은 작업

- 수정된 Request Profile v0.1.3으로 실제 Hancom HWP와 HWPX live E2E를 완전 재검증해 사업명,
  사업기간, 추진절차, 목적·지원 컴포넌트·delivery relation의 의미 완전성을 재점검
- password recovery link를 HttpOnly session cookie로 교환하는 callback/PKCE 흐름
- multipart part 수 제한과 streaming upload
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

# 실행 중인 Docker API와 worker/chat-worker를 통한 배포 acceptance
.venv/bin/python scripts/run_local_live_e2e.py \
  --worker-mode external \
  --api-base-url http://127.0.0.1:8001 \
  --file /safe/local/request.hwpx
```

external release gate는 clean checkout에서 `docker compose up -d --build`로 다시 build한다.
Dockerfile pristine stage가 `COPY .` 직후·pip install 전에 backend context 전체(빈 directory와
mode 포함)의 deterministic SHA-256을 계산하고 final image에는 artifact만 bake한다. E2E는 health
header/body 일치와 clean checkout의 독립 context digest 일치를 모두 요구한다.
Git commit은 manifest에 별도로 기록한다. runtime 환경변수/build arg로 image ID를 바꾸거나,
`--api-base-url`과 inline worker를 섞는 실행은 허용하지 않는다. 최종 문서/코드를 commit한 뒤
build와 E2E 사이에 tracked/untracked 파일을 바꾸지 않으며 trace는 repo 밖에 저장한다.

기본 `inline` E2E는 시작 전 analysis/chat queue가 비어 있음을 확인한 뒤 자신이 생성한
analysis run과 assistant message만 직접 점유한다. `external`은 배포된 API가 만든 target을
실행 중인 worker가 처리하도록 두고 public API를 polling한 다음, DB 감사 이력에서 실제
worker ID와 순차 attempt를 대조한다. 이때 대상 Compose worker 외 다른 worker/producer가
없는 전용 검증 창이 필요하다. 두 모드 모두 결과의 ML 1/2/3 `OK`, 공개 ML projection,
completed chat 내용과 case-scope evidence reference를 확인한다. 첫 시도가 retryable failure로
복귀하면 두 번째 시도까지 이어가고, 성공·최종 실패·시도 소진 중 하나로 유한하게 종료한다.
자세한 운영 주의사항은 [FastAPI·worker 운영 가이드](fastapi/docs/FASTAPI_WORKER_RUNBOOK.md)를 따른다.

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
OPENAI_LLM_MODEL=gpt-5.6-luna
OPENAI_REQUEST_PROFILE_MODEL=gpt-5.6-terra
PREREVIEW_MODEL1_SERVING_HOST_DIR=/absolute/path/to/.runtime/model1-serving/model1
PREREVIEW_MODEL1_RUNTIME_UID=<numeric-owner-uid>
PREREVIEW_MODEL1_RUNTIME_GID=<numeric-owner-gid>
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
