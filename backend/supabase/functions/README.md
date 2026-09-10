# Supabase Edge Functions — 레거시 참고용

이 디렉터리의 Edge Function 소스는 이전의 “Supabase가 외부 GPU worker에 HTTP dispatch하고
callback을 받는” 설계를 보존한 것이다. **현재 선택한 런타임에서는 배포하거나 호출하지
않는다.** 파일을 삭제하지 않은 이유는 과거 계약·마이그레이션의 추적성과 추후 대안 검토를
위해서다.

## 현재 활성 경로

```text
Frontend → FastAPI → Supabase Auth / Postgres / private Storage
                         ↑
                   same-server PostgreSQL polling worker
```

- Frontend는 `/functions/v1/*`를 직접 호출하지 않는다.
- FastAPI는 `POST /api/v1/analysis-runs`에서 원본을 private Storage에 올리고 run을 생성한다.
- worker는 `workspace.claim_next_analysis_run()`을 polling하고 DB/Storage에 내부 연결한다.
- 결과 완료는 callback이 아니라 `workspace.persist_analysis_result_core()`의 fenced transaction이다.
- 진행 상태는 FastAPI `GET /api/v1/analysis-runs/{id}` polling으로 조회한다. Realtime/SSE는 미사용이다.

## 사용하면 안 되는 레거시 계약

아래 환경변수 및 파일의 dispatch/callback 흐름은 현재 production/development 실행 경로가
아니다.

```text
ANALYSIS_WORKER_DISPATCH_URL
ANALYSIS_WORKER_DISPATCH_TOKEN
ANALYSIS_WORKER_INGEST_PROFILE_URL
ANALYSIS_WORKER_CALLBACK_TOKEN
CONVERSATION_WORKER_DISPATCH_URL
CONVERSATION_WORKER_DISPATCH_TOKEN
```

`WORKER_API_CONTRACT.md` 역시 위 레거시 Edge 설계의 참고 문서다. 현 worker 계약은
[../../fastapi/docs/WORKER_RESULT_PERSISTENCE_CONTRACT.md](../../fastapi/docs/WORKER_RESULT_PERSISTENCE_CONTRACT.md)를
따른다.

`deploy_local.sh`와 `../docker-compose.worker.override.yml.example`도 같은 레거시 묶음이다.
파일이 실행 가능하거나 구체적인 URL/토큰 변수를 담고 있다는 사실은 지원되는 배포 절차를
뜻하지 않는다.

## 향후 재도입 시

외부 worker 또는 Supabase 직접 호출을 재도입하려면, 단순히 function을 배포하지 말고 다음을
새로 확정해야 한다.

1. FastAPI와 Edge 중 공개 업무 API의 단일 소유자
2. queue 소유권, retry, lease, fencing의 단일 기준
3. Storage 권한과 worker identity
4. callback 결과가 `processing_run_pk` fence를 포함해 atomic materialisation하는 방식
5. 브라우저 인증·CORS·CSRF 경계

이 결정 전에는 `deploy_local.sh`로 function을 runtime에 복사하거나 functions container를
재기동할 필요가 없다.
