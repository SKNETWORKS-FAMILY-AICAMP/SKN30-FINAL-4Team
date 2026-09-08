-- ============================================================================
-- Migration 100: workspace.analysis_run 큐 컬럼 (임대 + 펜싱 + 멱등 제출)
-- Date: 2026-09-08
--
-- 팀원 DDL(03)의 analysis_run 은 상태 행이다. 클레임 토큰·임대·시도 횟수가
-- 없으므로 워커 두 대가 같은 행을 동시에 집어도 서로를 막지 못하고, 죽은
-- 워커가 잡은 행은 영원히 running 으로 남는다. 초안 §10: "상태 행이 있다는
-- 것만으로 큐가 완성되지는 않는다." 이 마이그레이션은 그 네 가지를 더한다.
-- 팀원 파일은 바이트 단위로 보존해야 하므로 03 을 고치지 않고 여기서 ALTER 한다.
--
-- ----------------------------------------------------------------------------
-- 멱등 제출 키: submission_sha256
-- ----------------------------------------------------------------------------
-- 새 키를 발명하지 않았다. workspace.source_artifact 에는 이미
-- `content_sha256 TEXT NOT NULL CHECK (content_sha256 ~ '^[0-9A-Fa-f]{64}$')`
-- 가 있고, 제출물의 동일성은 이미 그 해시로 정의돼 있다. 다만
-- source_artifact 는 analysis_run 의 자식(FK)이라 run 을 만들기 *전에는*
-- 존재할 수 없다. 그래서 같은 값·같은 CHECK 를 run 루트로 끌어올린다.
-- artifact_type='source' 인 아티팩트의 content_sha256 과 같은 값이 들어간다.
--
-- 유니크 범위는 (user_id, submission_sha256) 이되 살아 있는 상태에만 건다.
-- 더블 서브밋을 막는 것이 목적이지, 한 달 뒤 같은 문서를 다시 분석하는 것을
-- 영구히 막는 것이 목적이 아니다. 종료된 run 은 중복 판정에서 빠진다.
--
-- 호출부는 이 형태로 넣는다 (부분 인덱스이므로 WHERE 절을 함께 적어야
-- 아비터 인덱스가 추론된다):
--
--     INSERT INTO workspace.analysis_run (user_id, status, submission_sha256)
--     VALUES (:user_id, 'queued', :sha256)
--     ON CONFLICT (user_id, submission_sha256)
--         WHERE submission_sha256 IS NOT NULL
--           AND status IN ('queued', 'running')
--     DO NOTHING
--     RETURNING analysis_run_pk;
--
-- 0행이 돌아오면 이미 살아 있는 run 이 있다는 뜻이므로 같은 조건으로 SELECT 해
-- 기존 analysis_run_pk 를 돌려준다.
--
-- ----------------------------------------------------------------------------
-- claimed_by 는 브리프의 네 컬럼에 없는 추가분이다. worker_id 를 claim 파라미터로
-- 받으면서 어디에도 남기지 않으면, 임대가 만료됐을 때 어느 워커가 죽었는지 알 수
-- 없다. 진단용 텍스트 한 칸이면 충분하다.
-- ============================================================================

BEGIN;

ALTER TABLE workspace.analysis_run
    ADD COLUMN IF NOT EXISTS claim_token       UUID NULL,
    ADD COLUMN IF NOT EXISTS lease_expires_at  TIMESTAMPTZ NULL,
    ADD COLUMN IF NOT EXISTS attempt_count     INT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS last_error        TEXT NULL,
    ADD COLUMN IF NOT EXISTS claimed_by        TEXT NULL,
    ADD COLUMN IF NOT EXISTS submission_sha256 TEXT NULL;

-- ADD CONSTRAINT 에는 IF NOT EXISTS 가 없으므로 이름으로 확인하고 건다.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'ck_workspace_analysis_run_attempt_count'
           AND conrelid = 'workspace.analysis_run'::regclass
    ) THEN
        ALTER TABLE workspace.analysis_run
            ADD CONSTRAINT ck_workspace_analysis_run_attempt_count
            CHECK (attempt_count >= 0);
    END IF;

    -- 토큰과 임대는 항상 같이 살고 같이 죽는다. 한쪽만 남으면 펜싱이 새거나
    -- 임대가 영원해진다.
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'ck_workspace_analysis_run_lease_pairing'
           AND conrelid = 'workspace.analysis_run'::regclass
    ) THEN
        ALTER TABLE workspace.analysis_run
            ADD CONSTRAINT ck_workspace_analysis_run_lease_pairing
            CHECK ((claim_token IS NULL) = (lease_expires_at IS NULL));
    END IF;

    -- source_artifact.content_sha256 과 같은 형식 규율을 쓴다.
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'ck_workspace_analysis_run_submission_sha256'
           AND conrelid = 'workspace.analysis_run'::regclass
    ) THEN
        ALTER TABLE workspace.analysis_run
            ADD CONSTRAINT ck_workspace_analysis_run_submission_sha256
            CHECK (submission_sha256 IS NULL
                   OR submission_sha256 ~ '^[0-9A-Fa-f]{64}$');
    END IF;
END
$$;

-- 클레임 쿼리는 queued 이거나 임대가 만료된 running 을 본다. now() 는 불변이
-- 아니라 인덱스 술어에 못 넣으므로, 술어는 살아 있는 상태 두 개로 좁히고
-- lease_expires_at 은 인덱스 컬럼으로 둔다.
-- ponytail: 종료 행이 늘어도 이 인덱스는 커지지 않는다. 살아 있는 행이
-- 수만 개가 되면 그때 상태별로 쪼갠다.
CREATE INDEX IF NOT EXISTS ix_workspace_analysis_run_claimable
    ON workspace.analysis_run (status, lease_expires_at, created_at)
    WHERE status IN ('queued', 'running');

CREATE UNIQUE INDEX IF NOT EXISTS uq_workspace_analysis_run_submission
    ON workspace.analysis_run (user_id, submission_sha256)
    WHERE submission_sha256 IS NOT NULL
      AND status IN ('queued', 'running');

COMMIT;
