# Worker PostgreSQL 접근 경계

마지막 감사: 2026-09-10

## 현재 지원 경로

production analysis worker는 Supabase/FastAPI와 같은 서버에서 별도 container로 실행하며
server-only `DATABASE_URL`을 사용한다. 공개 HTTP endpoint, Edge callback, Redis/RQ는 없다.
private Storage 접근에는 별도의 `SUPABASE_URL`과 service-role/secret key가 필요하다.
Supabase HTTP service key와 PostgreSQL DSN/role은 서로 다른 자격증명이다.

현재 worker가 사용하는 DB 범위는 다음과 같다.

- `workspace.claim_next_analysis_run`, `heartbeat_analysis_run`,
  `fail_analysis_run`, `persist_analysis_result_core`
- `workspace.ingest_request_profile_core`
- `workspace.analysis_run_dispatch`, request profile, artifact와 lineage read/write
- `retrieval.embedding_configuration` read와 Existing 후보 match function
- 위 SECURITY INVOKER function 내부에서 접근하는 `workspace`, `result`, `ops`, `kb` 객체

migration 17, 19, 21~24의 명시적 worker function/table grant와 과거 완료 함수 폐기 계약을
적용한다. 활성 함수의 grant 대상은 `service_role`이다.
배포 DSN은 staging runtime validation을 통과한 trusted DB role을 사용하고 PostgreSQL 포트를
인터넷에 공개하지 않는다.

## `worker_dev` 문서의 상태

과거 문서에 있던 `worker_dev`, Windows `portproxy`,
`.runtime/supabase-dev/worker_dev.env` 경로는 이 저장소의 migration이나 provisioning
script로 생성·검증되지 않는다. 따라서 다음 주장은 현재 재현 가능한 계약이 아니다.

- `worker_dev` role이 이미 존재한다는 주장
- 특정 schema의 전체 SELECT/INSERT/UPDATE/DELETE grant가 적용됐다는 주장
- `ops` 접근 없이 현재 queue function을 실행할 수 있다는 주장

특히 queue/result function은 `SECURITY DEFINER`가 아닌 호출자 권한으로 내부 테이블을
변경한다. `BYPASSRLS`와 일부 schema grant만 수동으로 주는 방식은 현재 worker의 최소
권한을 보장하지 않으며 실행 중 permission error를 만들 수 있다.

## 별도 worker DB role이 필요할 때

별도 장비나 최소권한 role을 도입하려면 운영자가 임의 SQL을 실행하지 말고 다음을 먼저
versioned migration과 runtime test로 추가한다.

1. role 생성/회수와 credential rotation 절차
2. worker가 실제 실행하는 function, table, sequence의 최소 grant
3. RLS/BYPASSRLS 및 function `SECURITY INVOKER` 경계 검증
4. DB TLS, firewall/source allow-list와 비공개 port 경로
5. private Storage credential과 bucket/object prefix 권한
6. claim→heartbeat→artifact/profile 저장→fenced 완료/실패 E2E

그 전까지 원격 `worker_dev` 경로는 지원되는 production 또는 통합 테스트 절차로 취급하지
않는다. 접속정보를 Git·명령행·로그에 넣거나 관리자 PostgreSQL 비밀번호를 외부 장비와
공유하지 않는다.
