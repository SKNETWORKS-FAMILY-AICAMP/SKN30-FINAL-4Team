-- ============================================================================
-- Migration 104: workspace.analysis_run 결과 지시자 (RUN-04 Realtime 페이로드)
-- Date: 2026-09-08
--
-- 프론트 명세 RUN-04 는 이 행의 UPDATE 를 Realtime 으로 구독하고, 이벤트에서
-- 네 값을 읽는다.
--
--     { "analysis_run_pk", "status", "analysis_case_pk",
--       "error_code", "error_message" }
--
-- Realtime 은 바뀐 **행 자체**를 보낸다. 조인해 주지 않는다. 그래서
-- `analysis_case_pk` 가 이 행에 없으면, 분석이 succeeded 로 끝나도 화면이
-- 어느 결과를 열어야 할지 알 수 없다. 결과를 만들어 놓고 못 찾는 상태다.
--
-- ----------------------------------------------------------------------------
-- analysis_case_pk 에 FK 를 걸지 않는다
-- ----------------------------------------------------------------------------
-- 정방향 링크는 이미 있다: result.analysis_case.source_analysis_run_id 가
-- analysis_run 을 참조하고 UNIQUE 다. 그것이 주인이고 이 컬럼은 Realtime 이
-- 읽을 수 있게 같은 값을 이쪽에 둔 읽기 전용 사본이다. FK 를 걸면 두 테이블이
-- 서로를 참조하는 순환이 되어 삭제 순서가 꼬인다.
--
-- ----------------------------------------------------------------------------
-- error_code / error_message 는 last_error 와 다른 것이다
-- ----------------------------------------------------------------------------
-- last_error(100 번)는 운영용이다. 예외 타입과 원문이 들어간다.
-- 이 두 칸은 **화면에 그대로 보이는 값**이다. 명세: "분석 워커 내부 상세
-- 오류는 운영 로그에 남기며, 화면에는 안전한 사용자용 error_message 만
-- 표시한다."
--
-- 그래서 워커는 이 칸에 str(exception) 을 절대 넣지 않는다. 고정 문구표에서만
-- 고른다 (worker/outcome.py). 두 칸은 항상 같이 살고 같이 죽는다.
-- ============================================================================

BEGIN;

ALTER TABLE workspace.analysis_run
    ADD COLUMN IF NOT EXISTS analysis_case_pk UUID NULL,
    ADD COLUMN IF NOT EXISTS error_code       TEXT NULL,
    ADD COLUMN IF NOT EXISTS error_message    TEXT NULL;

DO $$
BEGIN
    -- 코드만 있고 문구가 없으면 화면이 빈 Alert 를 띄운다. 반대면 운영이
    -- 무슨 실패인지 집계할 수 없다. 짝을 강제한다.
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'ck_workspace_analysis_run_error_pairing'
           AND conrelid = 'workspace.analysis_run'::regclass
    ) THEN
        ALTER TABLE workspace.analysis_run
            ADD CONSTRAINT ck_workspace_analysis_run_error_pairing
            CHECK ((error_code IS NULL) = (error_message IS NULL));
    END IF;
END
$$;

COMMIT;
