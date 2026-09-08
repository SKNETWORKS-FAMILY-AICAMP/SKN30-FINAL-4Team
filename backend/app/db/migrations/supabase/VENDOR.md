# 벤더링한 팀원 Supabase 마이그레이션

초안 v0.2 §4 "팀원 패키지는 전달 버전·해시·출처를 기록해 편입한다"에 따른 기록이다.
편입 시점의 원본을 **바이트 단위 그대로** 복사했고, 아래 해시가 달라지면 로컬 수정이
있었다는 뜻이다.

| 항목 | 값 |
|---|---|
| 편입일 | 2026-09-08 |
| 출처 | `C:/Users/playdata2/Downloads/backend/supabase/migrations` |
| 매니페스트 | `MIGRATION_MANIFEST.md` v0.4 (2026-09-01), sha256 `d0cd989e4563b079e0e4264eaf579b6595958a17ebd858bd0c60bd8cf1ec8249` |
| 파일 수 | 9 (`01_`~`09_`), 1,655 lines |
| 적용 결과 | 57 tables / `app`·`ops`·`kb`·`workspace`·`result` |
| tree sha256 | `ffd5947d1c29cb1e0d702db0c10a9236887fed75bad7a7fd739ed48cd01726d8` |

## 파일별 sha256

| 파일 | bytes | sha256 |
|---|---|---|
| `01_core_schemas.sql` | 980 | `fd0b0a84d6f49af9c424bba6eef26da5e6d433845d4efbc2f298a037d22ae13c` |
| `02_core_ddl.sql` | 23,759 | `c2fc3749f5dfd2854254e1a55968a25e0c2ccb6ae2374ae7ddaa4cb932ad879d` |
| `03_workspace_ddl.sql` | 4,020 | `ba3f2609947c060970d922696d9db33bb86e0ed225d403e12e0a05e9d8ffd2b8` |
| `04_workspace_components.sql` | 5,065 | `9b99fca9c37a16b13bb5643d09a92d78b7d5fae882a821a65dcfb932b9ac3d40` |
| `05_workspace_projections.sql` | 10,730 | `52257783820e7f9b9a0d2450fc25d67d4eb251e245bb0d87ad23871262eddb41` |
| `06_result_ddl.sql` | 7,096 | `1a22f4047ce1debfa8fdbcbd6d8ac7531afb188988c90d4575fd5b79815a716b` |
| `07_indexes.sql` | 7,347 | `74ff099d71b39e5bc918e9889325bd1d08691bb940e94ae6f6dbc190268de749` |
| `08_rls_policies.sql` | 10,062 | `a5aa5a91a2c676041371a8cde482e42549ec45248852e3a6d11ed8ed96825a74` |
| `09_kb_notice_metadata.sql` | 328 | `e6aecce4e9802da81c1f262affc1b01515e1ae3a4383636f88a564b3c792c087` |

## 로컬 수정 금지

**`01_`~`09_` 파일은 고치지 않는다.** 팀원이 새 버전을 주면 통째로 교체하고 여기 해시를
갱신한다. 우리 쪽 변경이 필요하면 별도 번호의 파일을 더한다. 이유는 두 가지다.

- 원본을 고치면 다음 전달본과 3-way merge 를 해야 하고, 그 순간 "누가 무엇을 바꿨는가"를
  잃는다.
- 우리 변경이 별도 파일이면 팀원 쪽 스키마가 정식으로 그 기능을 담게 됐을 때 그 파일만
  지우면 된다.

부득이하게 원본을 고치게 되면 변경 이유·입력 사례·검증을 이 절에 남기고 해시를 갱신한다.

현재 로컬 수정: 없음.

## 우리가 더한 파일 (벤더링 대상 아님)

| 파일 | 목적 |
|---|---|
| `000_auth_stub.sql` | 순수 PostgreSQL 에 `auth.users`/`auth.uid()` 최소 스텁. 실제 Supabase Auth 를 붙이면 **삭제**한다. |
| `100_analysis_run_queue.sql` | `workspace.analysis_run` 에 임대·펜싱·시도 횟수·멱등 제출 키 추가. |

파일명 정렬 순서가 곧 적용 순서다 (`000_` < `01_` … `09_` < `100_`).

## 적용 방법

`psql` 로 순서대로 넣거나, `backend/tests/test_worker_jobs.py::jobs_engine` 이 하는 것처럼
정렬된 파일을 차례로 실행한다. `08_rls_policies.sql` 은 `format('... %I', t)` 를 쓰는데
psycopg 는 파라미터를 함께 넘기면 `%I` 를 플레이스홀더로 오인하므로, 드라이버 커넥션에
파라미터 없이 원문을 넘겨야 한다.

## 해시 재계산

```bash
python backend/app/db/migrations/supabase/verify_vendor_hashes.py
```
