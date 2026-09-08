-- ============================================================================
-- Migration 103: workspace.analysis_run 업로드 예약 (RUN-01 / RUN-03)
-- Date: 2026-09-08
--
-- 프론트 명세 SCR-004 는 업로드를 세 번에 나눈다.
--
--   RUN-01 edge-analysis-run-create         작업 생성 + object key 발급
--   RUN-02 storage.from(bucket).upload()    브라우저가 Storage 로 직접
--   RUN-03 edge-analysis-run-complete-upload 검증 후 큐 등록
--
-- 즉 run 행은 **파일이 존재하기 전에** 만들어진다. 팀원 DDL(03)에는 그 시점의
-- 상태도, 그 시점에 아는 정보를 담을 자리도 없다. 팀원 파일은 바이트 단위로
-- 보존해야 하므로 100 번과 같은 방식으로 여기서 ALTER 한다.
--
-- ----------------------------------------------------------------------------
-- 1. 'uploading' 상태
-- ----------------------------------------------------------------------------
-- DDL 03 의 CHECK 은 queued 부터 시작한다. 파일이 아직 오지 않은 run 을 queued
-- 로 두면 워커가 즉시 집어 빈 저장소를 읽는다. 프론트도 이 값을 안다 —
-- 명세 RUN-04 표의 "uploading, queued, running → 분석 중 화면 유지".
--
-- 100 번이 만든 클레임 인덱스 술어는 status IN ('queued','running') 이므로
-- uploading 행은 애초에 클레임 대상에 들어가지 않는다. 인덱스는 손대지 않는다.
--
-- ----------------------------------------------------------------------------
-- 2. 업로드 예약 정보 세 칸
-- ----------------------------------------------------------------------------
-- bucket 과 object_key 는 저장하지 않는다. 명세가 정한 경로가
-- `request-source/<user-id>/<run-id>/source.<ext>` 로 (user_id, run_pk,
-- 확장자)의 순수 함수이고, 확장자는 original_filename 에서 나온다. 저장하지
-- 않으면 클라이언트가 위조할 표면이 없고, RUN-03 이 매번 서버 값으로 다시
-- 계산한다.
--
-- workspace.source_artifact 에 넣지 못하는 이유는 그쪽
-- content_sha256 이 NOT NULL 이기 때문이다. 업로드 전에는 알 수 없는 값이다.
-- ============================================================================

BEGIN;

-- 인라인 CHECK 이라 이름이 자동 생성됐다. 이름을 가정하지 말고 이 테이블에서
-- 'cleanup_pending' 를 언급하는 CHECK 을 찾아 지운다.
DO $$
DECLARE
    constraint_name TEXT;
BEGIN
    SELECT conname INTO constraint_name
      FROM pg_constraint
     WHERE conrelid = 'workspace.analysis_run'::regclass
       AND contype = 'c'
       AND pg_get_constraintdef(oid) LIKE '%cleanup_pending%';

    IF constraint_name IS NOT NULL THEN
        EXECUTE format(
            'ALTER TABLE workspace.analysis_run DROP CONSTRAINT %I',
            constraint_name
        );
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'ck_workspace_analysis_run_status'
           AND conrelid = 'workspace.analysis_run'::regclass
    ) THEN
        ALTER TABLE workspace.analysis_run
            ADD CONSTRAINT ck_workspace_analysis_run_status
            CHECK (status IN (
                'uploading','queued','running',
                'succeeded','failed','cancelled','cleanup_pending'
            ));
    END IF;
END
$$;

ALTER TABLE workspace.analysis_run
    ADD COLUMN IF NOT EXISTS original_filename   TEXT   NULL,
    ADD COLUMN IF NOT EXISTS declared_mime_type  TEXT   NULL,
    ADD COLUMN IF NOT EXISTS declared_size_bytes BIGINT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'ck_workspace_analysis_run_declared_size'
           AND conrelid = 'workspace.analysis_run'::regclass
    ) THEN
        ALTER TABLE workspace.analysis_run
            ADD CONSTRAINT ck_workspace_analysis_run_declared_size
            CHECK (declared_size_bytes IS NULL OR declared_size_bytes > 0);
    END IF;
END
$$;

COMMIT;
