# PreReview EC2 서버 운영 매뉴얼

공용 Linux 계정 `prereview`로 현재 EC2의 Supabase, FastAPI, analysis worker, chat worker,
PDF report worker를
기동·중지·점검하는 절차다.

## 1. 경로와 서비스

```text
저장소:  /home/prereview/workspace/SKN30-FINAL-4Team-develop
Backend: /home/prereview/workspace/SKN30-FINAL-4Team-develop/backend
Supabase: /home/prereview/workspace/SKN30-FINAL-4Team-develop/.runtime/supabase-dev
```

| Compose project | 서비스 | 영속 데이터 |
|---|---|---|
| `backend` | FastAPI, analysis worker, chat worker, PDF report worker | 없음 |
| `supabase` | PostgreSQL, Auth, Storage, gateway, pooler | DB와 Storage |

Backend를 재생성해도 DB는 삭제되지 않는다. Supabase에 `down -v`를 실행하면 안 된다.

## 2. 환경 파일

| 파일 | 용도 | 권한 |
|---|---|---:|
| `backend/.env` | Docker FastAPI와 worker | `600` |
| `.runtime/supabase-dev/.env` | Supabase DB/Auth/SMTP | `600` |
| `.runtime/frontend-fastapi-tunnel.env` | 팀원 PC의 로컬 FastAPI | `600` |

환경 파일을 출력하거나 공유하지 않는다. `docker compose config`는 `--quiet`로 실행한다.

## 3. 평상시 상태 확인

### 3.1 Docker 전체 현황

먼저 서버에서 실행 중인 모든 컨테이너의 이름, 상태, 공개 포트를 확인한다.

```bash
docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
```

실행이 중단됐거나 재시작 중인 컨테이너까지 보려면 다음 명령을 사용한다.

```bash
docker ps -a --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
```

정상 운영 시 Backend에는 다음 네 컨테이너가 모두 `Up`이어야 한다.

| 컨테이너 | 역할 | 기대 프로세스 |
|---|---|---|
| `backend-api-1` | FastAPI | `uvicorn main:app` |
| `backend-worker-1` | 분석 queue worker | `python -m worker.main` |
| `backend-chat-worker-1` | 채팅 queue worker | `python -m worker.chat_main` |
| `backend-report-worker-1` | PDF queue worker | `python -m worker.report_main` |

Backend만 모아서 확인한다.

```bash
docker ps --filter 'name=backend-' \
  --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
docker top backend-api-1
docker top backend-worker-1
docker top backend-chat-worker-1
docker top backend-report-worker-1
```

Supabase의 애플리케이션 필수 컨테이너는 다음과 같다.

| 컨테이너 | 역할 | 정상 기준 |
|---|---|---|
| `supabase-db` | PostgreSQL/pgvector | `Up ... (healthy)` |
| `supabase-pooler` | PostgreSQL pooler | `Up ... (healthy)` |
| `supabase-auth` | 로그인·인증·비밀번호 재설정 | `Up ... (healthy)` |
| `supabase-auth-templates-1` | recovery 메일 template | `Up` |
| `supabase-storage` | private 파일 Storage | `Up ... (healthy)` |
| `supabase-rest` | 내부 PostgREST | `Up ... (healthy)` |
| `supabase-envoy` | Supabase API gateway, host 8000 | `Up ... (healthy)` |

공식 stack의 `realtime`, `meta`, `imgproxy`, `studio`, `edge-functions`도 정상 기동 상태인지
함께 확인한다. 현재 애플리케이션이 직접 사용하지 않더라도 운영 Compose 일부이므로 임의로
삭제하지 않는다.

```bash
docker ps --filter 'name=supabase-' \
  --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
docker ps --filter 'name=realtime-dev.supabase-realtime' \
  --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
```

다음 상태는 정상으로 보지 않는다.

- `Restarting`: 시작 오류가 반복되는 상태
- `Exited`: 프로세스가 종료된 상태
- `unhealthy`: healthcheck 실패
- 필수 컨테이너가 목록에 없음

이 경우 먼저 해당 컨테이너 로그를 확인하고, 원인을 확인한 뒤 이 문서의 Backend 또는
Supabase 기동 절차를 사용한다.

### 3.2 Git, Compose, API와 queue 확인

```bash
cd /home/prereview/workspace/SKN30-FINAL-4Team-develop
git status --short --branch

cd backend
docker compose -p backend config --quiet
docker compose -p backend ps
curl -fsS http://127.0.0.1:8001/health/live
curl -fsS http://127.0.0.1:8001/health/ready
docker compose -p backend logs --tail=100 api
docker compose -p backend logs --tail=100 worker
docker compose -p backend logs --tail=100 chat-worker
docker compose -p backend logs --tail=100 report-worker
```

`ready`는 실제 DB 쿼리와 worker 생존까지 보장하지 않는다. queue도 확인한다.

```bash
docker exec supabase-db psql -U postgres -d postgres -c "
SELECT status, count(*)
FROM workspace.analysis_run
GROUP BY status
ORDER BY status;

SELECT report.status, count(*)
FROM result.report_artifact AS report
WHERE report.report_type = 'pdf'
GROUP BY report.status
ORDER BY report.status;
"
```

`uploading`, `queued`, `running`이 있으면 analysis worker를 바로 내리지 않는다. PDF 배포·재기동
전에는 `ops.processing_run`의 `run_type='report_pdf' AND status='running'`도 확인한다.

## 4. Backend 기동·재생성

```bash
cd /home/prereview/workspace/SKN30-FINAL-4Team-develop/backend
docker compose -p backend config --quiet
docker compose -p backend up -d --build api worker chat-worker report-worker
docker compose -p backend ps
```

이미 최신 이미지가 빌드되어 있으면 다음과 같이 교체한다.

```bash
docker compose -p backend up -d --no-build api worker chat-worker report-worker
```

`.env`만 바뀌었다면 단순 `restart`로 반영되지 않는다.

```bash
docker compose -p backend up -d --force-recreate api worker chat-worker report-worker
```

프로세스만 다시 시작할 때 사용한다.

```bash
docker compose -p backend restart api worker chat-worker report-worker
```

## 5. Backend 중지

```bash
cd /home/prereview/workspace/SKN30-FINAL-4Team-develop/backend
docker compose -p backend stop -t 600 api worker chat-worker report-worker
```

일부만 조작할 때 서비스 이름을 지정한다.

```bash
docker compose -p backend stop -t 600 worker chat-worker report-worker
docker compose -p backend up -d worker chat-worker report-worker
```

Backend에서도 일반적으로 `down`은 필요 없다. `down -v`는 사용하지 않는다.

## 6. Supabase 기동·중지

항상 세 Compose 파일을 함께 사용한다.

```bash
cd /home/prereview/workspace/SKN30-FINAL-4Team-develop/.runtime/supabase-dev

docker compose -p supabase \
  -f docker-compose.yml \
  -f docker-compose.pgvector.yml \
  -f docker-compose.auth-templates.yml config --quiet

docker compose -p supabase \
  -f docker-compose.yml \
  -f docker-compose.pgvector.yml \
  -f docker-compose.auth-templates.yml up -d

docker compose -p supabase \
  -f docker-compose.yml \
  -f docker-compose.pgvector.yml \
  -f docker-compose.auth-templates.yml ps
```

전체 Supabase를 정지하면 로그인, 조회, 업로드, worker 처리가 모두 중단된다.

```bash
docker compose -p supabase \
  -f docker-compose.yml \
  -f docker-compose.pgvector.yml \
  -f docker-compose.auth-templates.yml stop -t 600
```

다시 기동할 때는 위 `up -d`를 사용한다. `down -v`는 절대 사용하지 않는다.

## 7. 코드 업데이트

```bash
cd /home/prereview/workspace/SKN30-FINAL-4Team-develop
git status --short --branch
```

작업 트리가 깨끗할 때만 업데이트한다. 모르는 변경이 있으면 reset하지 말고 담당자에게 묻는다.

```bash
git switch develop
git pull --ff-only origin develop
git rev-parse --short HEAD

cd backend
docker compose -p backend config --quiet
docker compose -p backend up -d --build api worker chat-worker report-worker
```

migration 적용은 평상시 코드 업데이트 명령에 포함하지 않는다. 이 저장소의
`apply_migrations.sh`에는 ledger가 없어 매번 `01`~현재 migration을 전부 재실행하므로,
신규 migration이 있는 배포에서만 `backend/supabase/MIGRATION_MANIFEST.md`의 적용 gate를
따른다. 동일 Supabase/image 조합의 staging 검증, `pg_dump -Fc`와 Storage backup, 활성
queue drain, backend 4개 service 정지, 적용 Git SHA 기록을 마친 뒤 다음을 1회 실행하고
서비스를 다시 기동한다. 각 `cd`는 현재 shell 위치와 무관하게 그대로 실행한다.

```bash
cd /home/prereview/workspace/SKN30-FINAL-4Team-develop/backend
docker compose -p backend stop -t 600 api worker chat-worker report-worker

cd /home/prereview/workspace/SKN30-FINAL-4Team-develop
SUPABASE_DIR="$PWD/.runtime/supabase-dev" \
  backend/supabase/apply_migrations.sh

cd backend
docker compose -p backend up -d --build api worker chat-worker report-worker
```

migration 41은 적용 이전에 이미 완료된 분석을 PDF queue에 넣지 않는다. 적용 이후 새로
완료되거나 실제로 재분석되어 완료 상태가 갱신된 case만 PDF 생성 대상이다. 최초
`report-worker` 이미지는 Chromium·한글 font를 포함하므로 API 이미지보다 크고 빌드에 시간이
더 걸릴 수 있다. 기동 전 서버의 디스크·메모리 여유를 확인한다.

## 8. 장애 대응

### DB 503과 pooler `nxdomain`

```bash
docker ps --format '{{.Names}}\t{{.Status}}' | grep -E 'supabase-(db|pooler)'
docker logs --tail=100 supabase-pooler
```

DB는 healthy인데 `nxdomain`이 반복되면 DB를 내리지 않고 pooler만 재시작한다.

```bash
docker restart supabase-pooler
docker exec backend-api-1 python -c \
  "import os, psycopg; c=psycopg.connect(os.environ['DATABASE_URL'], connect_timeout=5); print(c.execute('select 1').fetchone()[0]); c.close()"
```

### worker 반복 종료 또는 queued 정체

```bash
docker logs --tail=100 backend-worker-1
docker compose -p backend ps worker
docker compose -p backend ps report-worker
```

`Model 1 runtime manifest SHA-256 mismatch`라면 hash를 임의 변경하지 않는다. `serving.zip`,
Git SHA, runtime 경로가 같은 배포 기준인지 확인한다. API의 `ready=200`만 보지 말고 worker와
queue를 함께 확인한다.

## 9. 프론트 팀원의 로컬 FastAPI

EC2 Docker API와 팀원 PC의 FastAPI는 별개다. 최신 tunnel env를 다시 받는다.

```powershell
ssh.exe -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -L 18000:127.0.0.1:8000 -L 15432:127.0.0.1:5432 -L 16543:127.0.0.1:6543 prereview@43.200.108.74
```

위 터미널을 유지한 채, 별도 PowerShell에서 저장소를 최신화하고 환경 파일을 받는다.

```powershell
git switch develop
git pull --ff-only origin develop
scp.exe "prereview@43.200.108.74:/home/prereview/workspace/SKN30-FINAL-4Team-develop/.runtime/frontend-fastapi-tunnel.env" ".\backend\.env.tunnel"
icacls.exe ".\backend\.env.tunnel" /inheritance:r /grant:r "${env:USERNAME}:(R,W)"
Set-Location .\backend
uv run python -c "import asyncio, uvicorn; asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy()); uvicorn.run('main:app', host='127.0.0.1', port=8001, env_file='.env.tunnel')"
```

## 10. 금지 사항

- `docker compose down -v`
- 모르는 dirty 파일을 `git reset --hard`로 제거
- `.env`, token, DB URL, dump를 화면·채팅·Git에 노출
- 활성 분석 작업이 있는데 worker를 강제 종료
- backup 없이 운영 migration 수행

새 환경 구성은
[데이터베이스 신규 환경 구성 가이드](../../backend/supabase/DATABASE_SETUP_GUIDE.md), 상세 계약은
[FastAPI·worker 운영 가이드](../../backend/fastapi/docs/FASTAPI_WORKER_RUNBOOK.md)를 참고한다.
