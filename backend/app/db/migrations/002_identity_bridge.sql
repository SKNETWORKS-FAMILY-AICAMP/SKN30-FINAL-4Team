-- Slice 6: 신원 다리(identity bridge) + 검사 건 ↔ analysis_run 링크.
--
-- schema.sql 에도 같은 정의를 둔다. 이 파일은 이미 만들어진 DB 가 같은 모양을
-- 받아들이게 하는 재실행 가능한 ALTER 다. schema.sql 적용 뒤에 적용한다.
--
-- ----------------------------------------------------------------------------
-- 1. sims.app_user.external_uuid
-- ----------------------------------------------------------------------------
-- 인증을 교체하지 않는다. JWT 도 password_hash 도 sims.app_user 에 그대로
-- 남는다. 팀원 스키마는 사용자를 UUID 로만 가리키는데(auth.users(id) FK)
-- 우리 사용자 PK 는 bigint 라 그대로는 workspace.analysis_run.user_id 를 채울
-- 수 없다. 그래서 사용자마다 안정된 UUID 하나를 더 준다. 그게 전부다.
--
-- 기존 행에도 값이 필요하므로 DEFAULT 를 단 채로 NOT NULL 을 건다. PostgreSQL
-- 11+ 는 DEFAULT 가 있는 컬럼 추가를 테이블 재작성 없이 처리한다.
--
-- ----------------------------------------------------------------------------
-- 2. sims.inspection_case.analysis_run_id
-- ----------------------------------------------------------------------------
-- 큐에서 run 을 집은 워커가 어느 케이스를 분석해야 하는지 되찾는 링크다.
-- FK 는 걸지 않는다. 팀원 스키마가 설치되지 않은 DB 에서도 이 컬럼은 NULL 로
-- 남을 뿐이어야 하고, sims 가 workspace 의 설치 여부에 의존하면 안 된다.
-- UNIQUE 는 "한 run 은 한 케이스의 것" 이라는 불변식이자 워커 조회용 인덱스다.

ALTER TABLE sims.app_user
    ADD COLUMN IF NOT EXISTS external_uuid uuid NOT NULL DEFAULT gen_random_uuid();

ALTER TABLE sims.inspection_case
    ADD COLUMN IF NOT EXISTS analysis_run_id uuid;

-- ADD CONSTRAINT 에는 IF NOT EXISTS 가 없으므로 이름으로 확인하고 건다.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'app_user_external_uuid_key'
           AND conrelid = 'sims.app_user'::regclass
    ) THEN
        ALTER TABLE sims.app_user
            ADD CONSTRAINT app_user_external_uuid_key UNIQUE (external_uuid);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'inspection_case_analysis_run_id_key'
           AND conrelid = 'sims.inspection_case'::regclass
    ) THEN
        ALTER TABLE sims.inspection_case
            ADD CONSTRAINT inspection_case_analysis_run_id_key
            UNIQUE (analysis_run_id);
    END IF;
END
$$;
