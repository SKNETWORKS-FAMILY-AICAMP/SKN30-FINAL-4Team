# SIMS Pre-review

중소기업 지원사업 사전협의 요청서를 받아 **작성 완결성(CPL)** · **내부 정합성(FIT)** ·
**기존 사업과의 유사·중복성(SIM)** 을 검토하고 리포트와 PDF 를 내는 백엔드다.

```
FastAPI · PostgreSQL 15 + pgvector · SQLAlchemy Core · Pydantic v2
OpenAI (gpt-4o-mini · text-embedding-3-small) · reportlab
```

---

## 준비물

```
Python 3.12
Docker Desktop      개발용 PostgreSQL 컨테이너
OpenAI API 키       각자 발급. 공유하지 않는다
```

공공데이터포털 키는 없어도 된다. 목업 공고 6건으로 전체 파이프라인이 돈다.

---

## 구동

### 1. 가상환경

```bash
cd backend
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt     # Windows
# .venv/bin/pip install -r requirements.txt       # macOS · Linux
```

### 2. `.env`

저장소 루트에 만든다.

```bash
cp .env.example .env
```

두 줄만 채우면 된다. `DATABASE_URL` 은 아래 3번이 띄우는 컨테이너에 이미 맞춰져 있다.

```
JWT_SECRET=<아래 명령으로 생성>
OPENAI_API_KEY=<본인 키>
```

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

`.env` 는 `.gitignore` 대상이다. **커밋하거나 공유하지 않는다.**

### 3. DB 기동

```bash
bash scripts/e2e_up.sh
```

컨테이너 기동 · 스키마 적용 · 데모 계정 생성을 한 번에 한다.
계정은 `demo` / `demo-password-1234` 다. 회원가입 API 가 없어 스크립트가 직접 넣는다.

### 4. 목업 공고 넣기

```bash
backend/.venv/Scripts/python.exe scripts/seed_announcements.py
```

실제 동기화 경로를 그대로 쓰고 네트워크 호출만 스텁으로 바꾼다.
임베딩은 실제 OpenAI 로 만든다.

### 5. 서버

```bash
cd backend && .venv/Scripts/python.exe -m uvicorn main:app --port 8000
```

`http://127.0.0.1:8000/docs` 에서 Swagger 로 직접 호출할 수 있다.

### 6. 확인

다른 창에서.

```bash
backend/.venv/Scripts/python.exe scripts/make_mock_hwpx.py
backend/.venv/Scripts/python.exe scripts/e2e_run.py output/mock/사전협의요청서_우수사례.hwpx
```

로그인부터 채팅까지 7단계가 통과하면 정상이다.

---

## 테스트

DB 가 떠 있어야 한다(위 3번).

```bash
cd backend
TEST_DATABASE_URL="postgresql+psycopg://postgres:simstest@127.0.0.1:55433/sims" \
  .venv/Scripts/python.exe -m pytest -q
```

`seed_announcements.py` 를 돌린 DB 에서 전체 회귀를 하면 1건이 실패한다.
목업 공고를 만나 가짜 벡터를 못 찾기 때문이며 회귀가 아니다.
테스트 전에 비운다.

```bash
bash scripts/e2e_down.sh && bash scripts/e2e_up.sh
```

---

## 스크립트

```
scripts/e2e_up.sh               컨테이너 + 스키마 + demo 계정
scripts/e2e_down.sh             컨테이너 제거. 데이터도 사라진다
scripts/seed_announcements.py   목업 공고 6건 동기화·임베딩
scripts/make_mock_hwpx.py       목업 사전협의 요청서 2건 생성
scripts/e2e_run.py              7단계 E2E
```

`e2e_up.sh` 는 `pgvector/pgvector:pg15` 컨테이너를 `127.0.0.1:55433` 에 띄운다.
볼륨을 붙이지 않아 `e2e_down.sh` 를 돌리면 데이터가 함께 사라진다.
매번 깨끗한 상태로 시작하기 위한 것이다.

목업 요청서는 중소벤처기업부 「중소기업지원사업 사전협의 지침」 [서식 1] (24년도)
구조와 안내자료의 우수·미흡 사례를 따랐다. **점수를 맞추려고 목업을 고치지 않는다.**
규칙이 따라가야 하는 쪽이다.

---

## 문서

```
AGENTS.md            개발 지침
CLAUDE_HANDOFF.md    현재 상태와 미결정 사항 (git 미추적)
docs/백엔드_현황_공유.md   구조 · 에이전트 구조 · 알려진 문제
```

CPL · FIT · SIM 판별 로직의 기준은 팀 판별기준 원문이다.
구현기준서의 해당 절은 요약이므로 구현·검토의 근거로 쓰지 않는다.

---

## 알아둘 것

**임베딩 프로필은 첫 실행에 고정된다.** DDL 트리거로 불변이며 바꾸려면
프로필 v2 와 전체 재임베딩이 필요하다.

**`rhwp-python` 은 네이티브 확장이다.** Windows 휠은 확인했고 다른 OS 는
확인하지 못했다. macOS · Linux 에서 설치가 안 되면 알려달라.

**Git Bash 에서 `docker cp` 경로가 깨질 수 있다.** `e2e_up.sh` 는 `pwd -W` 로
처리해 두었다.
