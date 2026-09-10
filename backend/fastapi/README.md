# FastAPI·worker 문서 안내

현재 서비스의 공개 업무 경계는 FastAPI이며, 분석 worker는 같은 서버에서 외부 포트
없이 PostgreSQL queue를 polling한다.

- 실제 환경변수 설정·기동·재기동·로그·장애 확인:
  [FASTAPI_WORKER_RUNBOOK.md](docs/FASTAPI_WORKER_RUNBOOK.md)
- 프론트엔드 API 계약:
  [0.FASTAPI_FRONTEND_API_SPEC.md](docs/0.FASTAPI_FRONTEND_API_SPEC.md)
- worker 결과 저장 계약:
  [WORKER_RESULT_PERSISTENCE_CONTRACT.md](docs/WORKER_RESULT_PERSISTENCE_CONTRACT.md)
- worker 구현 경계·레거시 제외 범위:
  [WORKER_INTEGRATION_ASSESSMENT.md](docs/WORKER_INTEGRATION_ASSESSMENT.md)
- 전체 구현 현황과 미완료 범위:
  [IMPLEMENTATION_STATUS.md](../IMPLEMENTATION_STATUS.md)
- Supabase 인프라 배포·영속 데이터 운영:
  [supabase/README.md](../supabase/README.md)
- Existing KB 100건 검증·적재·embedding:
  [EXISTING_KB_BOOTSTRAP.md](../supabase/EXISTING_KB_BOOTSTRAP.md)

브라우저는 Supabase·PostgreSQL·worker를 직접 호출하지 않는다. 프론트엔드에는 FastAPI
주소만 전달하고, Supabase key·DB URL·OpenAI key는 모두 서버 환경변수로만 관리한다.
