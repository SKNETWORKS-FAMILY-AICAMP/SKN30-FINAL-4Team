# 로컬 프론트 CORS 연동 계약 v0.1

작성일: 2026-08-31. 사용자의 CORS 구현 요청에 따른 독립 작업이다.

## 1. 범위와 기준

백엔드 구현기준서 §14~16의 API·Bearer 인증·보안 계약을 유지하면서 브라우저의 교차 출처 API 호출을 허용한다. API 경로, 요청·응답 JSON, 인증·소유권 검사, DB·migration, 분석 판정은 변경하지 않는다. 프론트 로그인 화면·라우팅·API 호출 구현은 포함하지 않는다.

## 2. 설정

- `CORS_ALLOWED_ORIGINS`는 정확한 HTTP(S) Origin의 JSON 배열이다.
- 미설정 기본값은 `["http://localhost:3000","http://127.0.0.1:3000"]`이다. 현재 로컬 프론트 개발을 위한 값이다.
- 설정값은 기본값을 대체한다. `[]`로 교차 출처 허용을 끌 수 있다. 운영 배포에서는 운영 프론트 Origin 또는 `[]`를 명시한다.
- Origin은 스킴·호스트·선택적 포트만 포함한다. `*`, `null`, 빈 문자열, HTTP(S) 외 스킴, 사용자정보, 경로(후행 `/` 포함), 쿼리·프래그먼트는 설정 오류로 거부한다. 정규식·와일드카드 허용은 제공하지 않는다.
- 실제 요청의 Origin은 설정된 값과 정확히 일치해야 한다. `localhost`와 `127.0.0.1`, 포트가 다른 주소는 서로 다른 Origin이다.
- 앱 생성 시 설정을 확정하며 변경 후 백엔드를 재시작한다. 실제 `.env`의 기존 값이나 비밀정보는 수정하지 않는다.

## 3. HTTP 계약

- 허용 요청 메서드: 현재 API가 사용하는 `GET`, `POST`. 사전 요청 `OPTIONS`는 표준 CORS 미들웨어가 처리한다.
- 허용 요청 헤더: `Authorization`, `Content-Type` 및 표준 CORS safelisted 헤더.
- 인증은 기존 `Authorization: Bearer <token>`을 유지한다. 쿠키 인증을 도입하지 않으며 `allow_credentials=False`로 둔다. 프론트는 `credentials: "include"`를 사용하지 않는다.
- PDF 파일명 읽기를 위해 응답의 `Content-Disposition`을 노출한다.
- 허용된 사전 요청은 성공하고, 허용하지 않은 Origin·메서드·헤더의 사전 요청은 거부한다.
- 허용된 Origin에는 정상 응답과 기존에 처리되는 오류 응답(401, 404, 413, 422 등)에도 CORS 헤더를 제공한다. CORS는 업로드 본문 크기 제한 미들웨어 바깥에 둔다.
- 허용하지 않은 Origin에는 `Access-Control-Allow-Origin`을 제공하지 않는다. CORS는 브라우저의 응답 접근 정책이며 서버 인증·접근 통제가 아니다. 일반 요청 자체의 실행을 차단하거나 인증을 우회하지 않는다.
- Origin 없는 서버 간 요청과 기존 테스트/API 동작은 유지한다.
- 최외곽 서버 오류 처리기가 만드는 미처리 예외의 500 응답에는 CORS 헤더를 보장하지 않는다. 이번 작업은 기존 FastAPI 앱 타입과 오류 처리 구조를 유지한다.

## 4. 구현·검증 경계

설정, 앱 미들웨어 등록, `.env.example`, CORS 테스트만 변경한다. 설치된 FastAPI/Starlette의 `CORSMiddleware`를 사용하며 의존성을 추가하지 않는다. 기존 CPL·분석 파이프라인 미커밋 변경을 보존한다.

테스트는 구현 전 실패 확인 후 통과를 확인한다. 기본·사용자 지정·빈 Origin 설정, 잘못된 설정, 로그인·업로드 사전 요청, Bearer 헤더, 비허용 요청, 정상·오류 응답, Origin 없는 요청을 검증한다. 이후 격리된 실제 PostgreSQL/pgvector DB로 전체 회귀 테스트를 실행한다. 프론트 로그인 화면이 구현되지 않았으므로 이 결과를 실제 브라우저 로그인 E2E 성공으로 표현하지 않는다.

사용자 리뷰·승인 전에는 커밋하지 않는다.

## 5. 구현·검증 결과

- 구현: `backend/app/core/config.py`의 Origin 목록·검증, `backend/main.py`의 표준 CORS 미들웨어 등록, `.env.example` 설정 예시, `backend/tests/test_cors.py` 신규 테스트, `README.md` 설정 안내.
- API·DB·migration 변경 없음. 새 패키지·외부 서비스 의존성 없음. Rule/LLM 판정 변경 및 실제 외부 모델 호출 없음.
- 서브 에이전트가 구현 전 실패를 확인했다(초안 24개 중 21개 실패·3개 통과). 메인 리뷰에서 양쪽 로컬 주소의 사전 요청, 비허용 헤더, `Vary: Origin`, 제어문자 설정 거부를 보완했다.
- 메인 재검증: CORS **28 passed**, 전체 PostgreSQL 회귀 **263 passed**. 각 실행에서 기존 Starlette/httpx deprecation 경고 1건이 남았다. 테스트 삭제·검증 완화 없음.
- 실제 PostgreSQL 15.19 / pgvector 0.8.6의 별도 DB `sims_cors_regression_260831`에 현재 스키마를 적용해 검증했다. 기존 `sims` DB는 변경하지 않았고 검증용 DB만 종료 후 삭제했다. 필요하면 같은 스키마로 재생성할 수 있다.
- `logic_validator.py`, `test_cpl.py`, `analysis_pipeline.py`는 작업 전후 SHA-256이 같다. `config.py`는 기존 내용을 복원하지 않고 CORS 부분만 추가했다. staging·커밋 없음.

실행 명령(작업 디렉터리 `backend`, 테스트 DB 생성·스키마 적용 후):

```powershell
$env:TEST_DATABASE_URL='postgresql+psycopg://postgres:simstest@127.0.0.1:55533/sims_cors_regression_260831'
$env:OPENAI_API_KEY=''
$env:BIZINFO_API_KEY=''
$env:PYTHONUTF8='1'
$env:PYTHONIOENCODING='utf-8'
.\.venv\Scripts\python.exe -m pytest tests/test_cors.py -q -p no:cacheprovider --tb=short
.\.venv\Scripts\python.exe -m pytest tests -q -p no:cacheprovider --tb=short --basetemp=..\tmp\cors_pytest_260831_b
```

남은 범위: 실행 중인 백엔드 프로세스의 재시작은 수행하지 않았다. HTTP 계약은 TestClient로 검증했으며 실제 브라우저 로그인 E2E는 검증하지 않았다. 현재 이슈3 프론트의 로그인 화면·라우팅·API 연결 구현은 별도 작업이다. 미처리 500 응답의 헤더 보장 제한은 §3과 같다. 다음 권장 작업은 사용자 승인 후 프론트 로그인 연결이다.
