# 합성 사전협의 요청서 E2E fixture 5건

이 디렉터리는 다른 개발자가 Request 분석 경로를 재현할 때 사용하는 테스트 자료다.
`generated/`에는 실제 사전협의 요청서의 모양과 항목을 흉내 낸 HWPX 5건과 기대값을
담은 `manifest.json`이 있다. 한글(Hancom)에서 직접 작성한 원본은 아니다.

## 들어 있는 파일

| 파일 | 용도 |
|---|---|
| `generated/01_*.hwpx` ~ `generated/05_*.hwpx` | 업로드·파서·worker E2E 입력 |
| `generated/manifest.json` | 파일명, SHA-256, 공고 ID, 보존되어야 할 본문, 요청 유형 기대값 |
| `backend/scripts/validate_synthetic_hwpx_fixtures.py` | 외부 서비스 없이 5건을 검증하는 회귀 도구 |
| `backend/scripts/run_local_live_e2e.py` | 실제 Supabase와 OpenAI를 쓰는 운영자용 1건 통합 검증 도구 |

fixture의 원본과 `manifest.json`은 테스트 기준선이다. 검증 중 HWPX를 수정하거나
생성 산출물을 `generated/` 안에 저장하지 않는다.

## 검증 범위

이 세트가 확인하는 것은 다음과 같다.

- manifest에 선언된 HWPX가 정확히 5건이고 예상하지 않은 HWPX가 없는지
- HWPX ZIP 필수 구성과 각 파일의 SHA-256이 기준값과 일치하는지
- HWPX → Common IR v1 변환과 Common IR provenance의 원본 SHA-256 계보
- manifest의 사업명·필요성·주요 내용·기대 효과가 Common IR에 누락되지 않는지
- LLM 호출 전 결정 단계에서 `세부사업 신설(detail_program_new)`로 판정되는지
- 파서 실행 전후에 원본 HWPX가 바뀌지 않는지

합성 양식은 현재 `rhwp` 파서에서 문서당 두 텍스트 블록으로 평탄화된다. 이는 이
fixture의 기대값일 뿐, 실제 한글 문서의 표·도형·머리말·쪽 나눔 처리 품질을 대표하지
않는다. PDF/OCR, Request Profile fact 생성, Existing 후보 품질, LLM 판정 품질도
오프라인 검증 범위 밖이다.

## 1. 외부 서비스 없는 5건 검증

최초 한 번 backend Python 환경을 준비한다. 아래 명령만 dependency 설치를 위해
인터넷을 사용할 수 있다.

```bash
cd /path/to/SKN30-FINAL-4Team/backend
uv sync --frozen --extra dev
```

그다음에는 Supabase, PostgreSQL, Storage, OpenAI 없이 실행할 수 있다.

```bash
cd /path/to/SKN30-FINAL-4Team/backend
PREREVIEW_FREETYPE_LIB=/lib/x86_64-linux-gnu/libfreetype.so.6 \
  uv run python scripts/validate_synthetic_hwpx_fixtures.py \
  --fixture-dir ../docs/pre_review_request_e2e_5_20260909_v1/generated
```

정상이면 exit code `0`과 함께 다음 핵심 값이 JSON 한 줄로 출력된다.

```json
{"status":"ok","dataset":"synthetic_prereview_request_e2e_5/v1","fixture_count":5}
```

각 case에는 `source_sha256`, `common_ir_document_id`, `common_ir_block_count`와
`request_type`도 포함된다. 기본 실행은 Common IR을 임시 디렉터리에 만들고 종료할 때
삭제한다. 산출물을 직접 보려면 fixture 밖의 전용 경로를 지정한다.

```bash
cd /path/to/SKN30-FINAL-4Team/backend
PREREVIEW_FREETYPE_LIB=/lib/x86_64-linux-gnu/libfreetype.so.6 \
  uv run python scripts/validate_synthetic_hwpx_fixtures.py \
  --fixture-dir ../docs/pre_review_request_e2e_5_20260909_v1/generated \
  --run-dir /tmp/pre-review-synthetic-hwpx-artifacts
```

Docker 이미지의 parser만 확인할 때는 repository root가 아니라 `backend`에서 실행한다.

```bash
cd /path/to/SKN30-FINAL-4Team/backend
docker compose build api
docker run --rm --network none \
  -e PREREVIEW_FREETYPE_LIB=/usr/lib/x86_64-linux-gnu/libfreetype.so.6 \
  -v "$PWD/../docs/pre_review_request_e2e_5_20260909_v1/generated:/fixtures:ro" \
  backend-api:latest \
  python scripts/validate_synthetic_hwpx_fixtures.py --fixture-dir /fixtures
```

`--network none`과 read-only(`:ro`) mount를 사용하므로 이 검증은 OpenAI를 호출하거나
fixture를 변경할 수 없다.

## 2. 실행 중인 FastAPI의 무비용 smoke

FastAPI 컨테이너를 기동한 뒤 실제 HTTP 포트와 OpenAPI 노출을 먼저 확인한다. 이
단계는 로그인, 파일 업로드, DB 쓰기, OpenAI 호출을 하지 않는다.

```bash
curl -fsS http://127.0.0.1:8001/health/live
curl -fsS http://127.0.0.1:8001/health/ready
curl -fsS http://127.0.0.1:8001/openapi.json \
  | jq -r '.paths | keys[]'
```

`/health/live`는 `{"status":"live"}`, online 설정이 조립된 `/health/ready`는
`{"status":"ready"}`여야 한다. ready는 DB 접속, Existing KB 적재량, worker 생존까지
검사하지 않는다. 기동 방법과 환경변수는
[FastAPI·worker 운영 가이드](../../backend/fastapi/docs/FASTAPI_WORKER_RUNBOOK.md)를
따른다.

## 3. Swagger에서 실제 업로드 E2E

다음 조건이 모두 준비되어야 분석이 `succeeded`까지 간다.

- Supabase migration과 private bucket이 준비되어 있음
- Existing Profile과 1,536차원 `text-embedding-3-small` 검색 벡터가 적재되어 있음
- `backend/.env`에 Supabase·DB·OpenAI 서버 설정이 있음
- FastAPI와 polling worker가 모두 실행 중임
- 로그인 가능한 일반 테스트 계정이 있음
- Swagger origin이 `PREREVIEW_AUTH_ALLOWED_ORIGINS`에 정확히 포함되어 있음

Existing KB 준비 절차는
[Existing KB bootstrap 가이드](../../backend/supabase/EXISTING_KB_BOOTSTRAP.md)를 따른다. 테스트 담당자에게는
일반 사용자 이메일·비밀번호만 전달하며 anon/service-role key나 DB URL은 전달하지 않는다.
현재 로컬 Supabase는 이메일 자동 확인이 꺼져 있으므로 새 `sign-up` 계정은 바로 로그인되지
않을 수 있다. 운영자가 Studio의 Authentication 사용자 화면에서 테스트 계정을 생성·확인한
뒤 자격증명만 안전한 채널로 전달한다. 테스트 비밀번호를 Git이나 README에 고정하지 않는다.

브라우저에서 `http://127.0.0.1:8001/docs`를 열고 다음 순서로 실행한다. 중간에
`localhost`와 `127.0.0.1`을 바꾸지 않는다.

1. `POST /api/v1/auth/sign-in`에 테스트 계정 이메일·비밀번호를 입력한다.
2. 응답 `200`을 확인한다. HttpOnly Cookie는 브라우저가 저장하므로 Swagger의
   `Authorize`에 token을 입력하지 않는다.
3. `POST /api/v1/analysis-runs`의 `file`에 `generated/01_*.hwpx`를 선택한다.
4. 응답 `202`의 `analysis_run_id`와 `status=queued`를 확인한다.
5. `GET /api/v1/analysis-runs/{analysis_run_id}`를 반복 호출한다.
6. `status=succeeded`와 `analysis_case_id`가 나오면
   `GET /api/v1/analysis-cases/{analysis_case_id}`로 CPL/FIT/SIM/evidence를 확인한다.

업로드 이후에는 private Storage 객체, queue, 분석 결과가 남고 worker가 OpenAI를
호출하므로 비용이 발생한다. `409`가 나오면 같은 사용자에게 이전 `queued` 또는
`running` 작업이 남아 있는지 확인한다. `queued`에서 진행되지 않으면 worker 상태를,
후보가 없으면 Existing Profile과 embedding 적재를 먼저 확인한다.

## 4. 운영자용 자동 1건 통합 검증

`run_local_live_e2e.py`는 외부 8001 포트를 호출하는 도구가 아니다. Python 프로세스 안에
FastAPI ASGI 앱을 조립한 뒤 실제 self-hosted Supabase Auth·Storage·PostgreSQL을 쓰고,
worker를 한 번 직접 실행하는 통합 테스트다. 별도 test user를 자동 생성하며 비밀번호,
원문, 전체 모델 출력은 표시하지 않는다.

먼저 `backend/.env`와 `.runtime/supabase-dev/.env`가 준비되어 있어야 한다. background
worker와 claim 경쟁을 피하려면 이 테스트 동안 worker 컨테이너는 중지해 둔다.

```bash
cd /path/to/SKN30-FINAL-4Team/backend
.venv/bin/python scripts/run_local_live_e2e.py \
  --backend-env .env \
  --supabase-env ../.runtime/supabase-dev/.env \
  --file ../docs/pre_review_request_e2e_5_20260909_v1/generated/01_유니콘브릿지_기술금융_사전협의요청서.hwpx
```

이 검증도 OpenAI 비용이 발생하며, Studio 확인을 위해 생성한 test user와 분석 레코드를
삭제하지 않는다. 2026-09-10 기준으로 1번 fixture에서 CPL 13건, FIT 7건, SIM 후보
1건, evidence 54건의 저장·조회까지 확인했다. 이 수치는 모델 변경 뒤 절대적인 golden
count로 사용하지 않고, 계약 누락이나 빈 결과를 발견하는 참고값으로만 사용한다.
