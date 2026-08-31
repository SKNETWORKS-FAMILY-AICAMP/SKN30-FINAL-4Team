# SIMS Pre-review 백엔드 실행 안내

중소기업 지원사업 사전협의 요청서(HWP/HWPX)를 분석해 다음 결과를 만드는 FastAPI 백엔드다.

- CPL: 필수 항목 작성 여부
- FIT: 요청서 내부 내용의 정합성
- SIM: 기존 지원사업과의 유사·중복 가능성
- 결과 JSON, PDF, 결과 기반 채팅

이 문서는 **Windows + VS Code + Command Prompt(cmd)** 기준이다. 아래 명령은 별도 표시가 없으면 저장소 루트에서 실행한다.

```text
SKN30-FINAL-4Team\
├─ backend\
├─ scripts\
├─ .env.example
└─ README.md
```

## 1. 준비물

1. Python 3.12
2. Git for Windows
3. Docker Desktop
4. VS Code
5. OpenAI API 키

OpenAI API 키는 의미 분석, 공고 임베딩, 채팅에 사용되며 API 사용료가 발생할 수 있다. `JWT_SECRET`과 PostgreSQL 계정은 외부에서 발급받지 않는다.

Docker Desktop은 설치 후 실제로 실행해 둔다.

## 2. VS Code에서 CMD 열기

VS Code에서 프로젝트 폴더를 연다.

각자 clone하거나 내려받은 `SKN30-FINAL-4Team` 폴더를 선택한다.

`Terminal → New Terminal`을 누르고 터미널 오른쪽 화살표에서 `Command Prompt`를 선택한다.

현재 위치가 다르면 저장소 루트로 이동한다.

```cmd
cd /d C:\YOUR_PATH\SKN30-FINAL-4Team
```

```cmd
dir
```

`backend`, `scripts`, `.env.example`이 보이면 정상이다.

## 3. 설치 확인

```cmd
python --version
git --version
docker --version
docker ps
```

Python은 `3.12.x`가 권장된다. `docker ps`에서 연결 오류가 나면 Docker Desktop을 실행한 후 다시 시도한다.

## 4. Python 가상환경과 패키지

최초 한 번만 실행한다.

```cmd
python -m venv backend\.venv
backend\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt
```

설치 확인:

```cmd
backend\.venv\Scripts\python.exe -c "import fastapi; print(fastapi.__version__)"
```

FastAPI 버전이 출력되면 정상이다.

## 5. 환경설정

`.env`가 아직 없다면 예제 파일을 복사한다.

```cmd
copy .env.example .env
```

이미 `.env`가 있다면 덮어쓰지 않는다. VS Code에서 연다.

```cmd
code .env
```

### JWT_SECRET

`JWT_SECRET`은 가입해서 받는 키가 아니다. 다음 명령으로 직접 만든다.

```cmd
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

출력된 문자열을 `.env`에 넣는다.

```dotenv
JWT_SECRET=출력된_긴_문자열
```

### OpenAI API 키

`.env`의 다음 줄을:

```dotenv
# OPENAI_API_KEY=replace-me
```

다음처럼 바꾼다.

```dotenv
OPENAI_API_KEY=sk-본인의_API_키
```

최소 설정:

```dotenv
DATABASE_URL=postgresql+psycopg://postgres:simstest@127.0.0.1:55433/sims
DATABASE_CONNECT_TIMEOUT_SECONDS=3
JWT_SECRET=직접_생성한_긴_문자열
JWT_ACCESS_TOKEN_EXPIRE_MINUTES=30
OPENAI_API_KEY=sk-본인의_API_키
```

`.env`에는 비밀키가 들어가므로 커밋하거나 공유하지 않는다.

## 6. PostgreSQL 실행

Docker Desktop이 실행 중인지 확인한다.

```cmd
docker ps
```

DB 준비 스크립트는 Bash 파일이다. CMD에서는 Git for Windows에 포함된 Bash를 호출한다.

```cmd
"%ProgramFiles%\Git\bin\bash.exe" scripts/e2e_up.sh
```

스크립트가 자동으로 수행하는 작업:

1. PostgreSQL 15 + pgvector 컨테이너 실행
2. PostgreSQL 준비 대기
3. `backend/app/db/schema.sql` 적용
4. 데모 로그인 계정 생성

DB 접속 정보:

```text
컨테이너: sims-e2e-pg
주소:      127.0.0.1
포트:      55433
DB:        sims
DB 사용자: postgres
DB 암호:   simstest
```

서비스 로그인 계정은 DB 계정과 다르다.

```text
로그인 ID: demo
비밀번호:  demo-password-1234
```

상태 확인:

```cmd
docker ps
docker exec sims-e2e-pg pg_isready -U postgres -d sims
```

`accepting connections`가 나오면 정상이다.

## 7. 목업 지원사업 공고 입력

```cmd
backend\.venv\Scripts\python.exe scripts\seed_announcements.py
```

테스트용 지원사업 공고 6건을 실제 DB에 넣고 OpenAI API로 임베딩을 생성한다. 공공데이터포털 키 없이 전체 흐름을 확인하기 위한 데이터다.

업로드한 요청서의 CPL과 FIT은 실제 문서를 분석한다. SIM 로직도 실제로 실행되지만 비교 대상은 목업 공고 6건이므로 공식 결과나 전체 실제 공고 기준 결과는 아니다.

## 8. 백엔드 서버 실행

```cmd
cd backend
.venv\Scripts\python.exe -m uvicorn main:app --port 8000
```

정상 로그:

```text
Uvicorn running on http://127.0.0.1:8000
```

브라우저에서 Swagger를 연다.

```text
http://127.0.0.1:8000/docs
```

이 터미널은 서버가 사용하므로 닫지 않는다. 다른 명령은 VS Code에서 CMD 터미널을 하나 더 열어 실행한다.

### 프론트 CORS 설정

기본 허용 프론트 주소는 `http://localhost:3000`과 `http://127.0.0.1:3000`이다. 다른 주소를 쓰면 `.env`에 JSON 배열로 지정하고 백엔드를 재시작한다. 설정값은 기본 목록을 대체한다.

```dotenv
CORS_ALLOWED_ORIGINS=["http://localhost:3000","http://127.0.0.1:3000"]
```

운영에서는 운영 프론트 주소 또는 `[]`(교차 출처 허용 안 함)를 명시한다. 주소에 경로나 후행 `/`을 붙이지 않으며 `*`는 허용하지 않는다. 프론트는 기존 Bearer 토큰을 `Authorization` 헤더로 보내고 `credentials: "include"`는 사용하지 않는다. PDF 파일명용 `Content-Disposition` 헤더도 읽을 수 있다.

CORS 설정은 로그인 화면이나 API 호출 코드를 만드는 기능이 아니다. 상세 범위와 오류 응답 처리는 [CORS 연동 계약](docs/CORS_연동계약_v0.1_260831.md)을 참고한다.

## 9. 전체 E2E 확인

새 CMD 터미널에서 저장소 루트로 이동한다.

```cmd
cd /d C:\YOUR_PATH\SKN30-FINAL-4Team
```

저장소에 포함된 우수사례 목업 HWPX로 전체 흐름을 실행한다.

```cmd
backend\.venv\Scripts\python.exe scripts\e2e_run.py samples\hwpx\사전협의요청서_우수사례.hwpx
```

미흡사례를 실행하려면 파일 경로만 바꾼다.

```cmd
backend\.venv\Scripts\python.exe scripts\e2e_run.py samples\hwpx\사전협의요청서_미흡사례.hwpx
```

목업 파일은 다음 위치에 버전 관리한다.

```text
samples\hwpx\사전협의요청서_우수사례.hwpx
samples\hwpx\사전협의요청서_미흡사례.hwpx
samples\hwpx\mockup_01_우수사례_AI바이오실증.hwpx
samples\hwpx\mockup_02_우수사례_뿌리산업스마트제조.hwpx
samples\hwpx\mockup_03_보통사례_청년로컬크리에이터.hwpx
samples\hwpx\mockup_04_보통사례_친환경그린에너지.hwpx
samples\hwpx\mockup_05_저급사례_AI바우처_모순충돌.hwpx
samples\hwpx\mockup_06_저급사례_해외수출_목적내용불일치.hwpx
```

`scripts\make_mock_hwpx.py`는 샘플을 다시 만들거나 생성 근거를 확인할 때만 사용한다. 일반적인 E2E 실행에는 필요하지 않다.

다른 목업을 실행할 때는 `e2e_run.py` 뒤의 파일 경로만 원하는 샘플로 바꾼다.

```cmd
backend\.venv\Scripts\python.exe scripts\e2e_run.py samples\hwpx\mockup_01_우수사례_AI바이오실증.hwpx
```

E2E 호출 순서:

```text
로그인
→ 요청서 업로드
→ 분석 시작
→ 파싱
→ CPL
→ FIT
→ 유사 공고 검색
→ SIM
→ 결과 JSON
→ PDF 다운로드
→ 채팅
```

7단계가 모두 통과하면 백엔드 핵심 흐름이 정상이다.

## 10. 실제 문서 분석

서버가 켜진 상태에서 Swagger를 사용하거나 프론트를 연결한다.

```text
POST /api/v1/auth/login
POST /api/v1/cases
POST /api/v1/cases/{case_id}/analyze
GET  /api/v1/cases/{case_id}/status
GET  /api/v1/cases/{case_id}/report
GET  /api/v1/cases/{case_id}/report.pdf
POST /api/v1/cases/{case_id}/chat/messages
```

유효한 HWP/HWPX 사전협의 요청서를 넣으면 실제 파싱·CPL·FIT·SIM·리포트·PDF 코드가 실행된다. 다만 현재 환경은 로컬 개발용이고 SIM 비교 공고는 목업 데이터다. 결과는 팀 내부 알파 기준의 사전 검토 결과이며 공식 행정 판정이 아니다.

## 11. 다음 날 다시 실행

가상환경 생성, 패키지 설치, `.env` 작성, 공고 입력은 보통 다시 하지 않아도 된다.

```cmd
docker start sims-e2e-pg
cd backend
.venv\Scripts\python.exe -m uvicorn main:app --port 8000
```

이미 컨테이너가 실행 중이라는 메시지가 나오면 그대로 진행한다.

## 12. 종료와 초기화

서버 종료는 서버 터미널에서 `Ctrl+C`를 누른다.

DB 데이터를 유지하고 컨테이너만 중지:

```cmd
docker stop sims-e2e-pg
```

DB 컨테이너와 내부 데이터를 모두 삭제:

```cmd
"%ProgramFiles%\Git\bin\bash.exe" scripts/e2e_down.sh
```

`e2e_down.sh`를 실행하면 데모 계정, 목업 공고, 업로드 기록, 분석 결과가 사라진다. 다시 시작하려면 6번과 7번을 다시 수행한다.

## 13. 전체 테스트

DB가 실행 중이어야 한다. 저장소 루트의 CMD에서 실행한다.

```cmd
set TEST_DATABASE_URL=postgresql+psycopg://postgres:simstest@127.0.0.1:55433/sims
backend\.venv\Scripts\python.exe -m pytest backend\tests -q
```

목업 공고가 들어간 DB는 테스트 데이터와 충돌할 수 있다. 데이터 삭제에 동의한 경우에만 DB를 초기화한 뒤 전체 테스트한다.

```cmd
"%ProgramFiles%\Git\bin\bash.exe" scripts/e2e_down.sh
"%ProgramFiles%\Git\bin\bash.exe" scripts/e2e_up.sh
set TEST_DATABASE_URL=postgresql+psycopg://postgres:simstest@127.0.0.1:55433/sims
backend\.venv\Scripts\python.exe -m pytest backend\tests -q
```

## 14. 자주 발생하는 문제

### Docker 연결 오류

Docker Desktop을 실행한 뒤 확인한다.

```cmd
docker ps
```

### `bash.exe`를 찾지 못함

```cmd
where git
```

Git이 기본 위치가 아니라면 실제 설치 폴더의 `bin\bash.exe`를 사용한다.

### `OPENAI_API_KEY is required`

`.env`에서 키 앞의 `#`을 제거했는지 확인한다.

```dotenv
OPENAI_API_KEY=sk-본인의_API_키
```

### DB 연결 실패

```cmd
docker ps
docker exec sims-e2e-pg pg_isready -U postgres -d sims
```

```dotenv
DATABASE_URL=postgresql+psycopg://postgres:simstest@127.0.0.1:55433/sims
```

### `No module named ...`

시스템 Python이 아니라 프로젝트 가상환경 Python으로 실행한다.

```cmd
backend\.venv\Scripts\python.exe
```

### 8000 포트가 이미 사용 중임

기존 서버 터미널을 찾아 `Ctrl+C`로 종료한다.

```cmd
netstat -ano | findstr :8000
```

## 15. 현재 실행 범위

실제로 동작하는 것:

- PostgreSQL/pgvector 저장과 검색
- HWP/HWPX 파싱
- CPL/FIT/SIM 로직
- OpenAI API 호출
- 결과 JSON과 PDF 생성
- 분석 결과 기반 채팅

로컬 개발용인 것:

- `postgres / simstest` DB 계정
- `demo / demo-password-1234` 서비스 계정
- 목업 공고 6건
- 로컬 파일 저장소
- 서버 프로세스 내부 백그라운드 작업

운영 배포 전에는 운영 DB, 영속 저장소, 실제 공고 동기화, 사용자·보안 정책, HTTPS, 외부 작업 큐와 모니터링이 별도로 필요하다.

## 이메일 비밀번호 재설정

프론트 연동 계약과 Gmail 설정은 [이메일 비밀번호 재설정 연동계약](docs/SIMS_이메일_비밀번호재설정_연동계약_260831.md)을 참고한다.
`.env.example`의 `SMTP_*`, `PASSWORD_RESET_URL`을 로컬 `.env`에 설정한 뒤 재시작한다. 미설정 시 발송 API는 503을 반환하며 기존 로그인·분석에는 영향을 주지 않는다.
새 비밀번호는 8~128자이며 영문·숫자·특수문자를 각각 포함한다. 기존 비밀번호의 로그인은 이 규칙으로 제한하지 않는다.
