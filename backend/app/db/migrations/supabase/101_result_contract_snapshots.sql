-- ============================================================================
-- Migration 101: result contract snapshots
-- Date: 2026-09-08
--
-- The result schema keeps the small, immutable values the result screen needs
-- after workspace rows are cleaned up. This migration is additive so it is safe
-- to apply after the original result DDL.
-- ============================================================================

BEGIN;

ALTER TABLE result.analysis_case
    ADD COLUMN IF NOT EXISTS program_name       TEXT NULL,
    ADD COLUMN IF NOT EXISTS original_filename  TEXT NULL,
    ADD COLUMN IF NOT EXISTS report_status      TEXT NULL;

ALTER TABLE result.sim_candidate
    ADD COLUMN IF NOT EXISTS title          TEXT NULL,
    ADD COLUMN IF NOT EXISTS result_summary  TEXT NULL;

ALTER TABLE result.evidence_snapshot
    ADD COLUMN IF NOT EXISTS comparison_side TEXT NULL;

-- Existing result rows predate the separate report lifecycle. Do not mark a
-- ready report as failed merely because this column is new: an explicit failed
-- case stays failed, an existing artifact is ready, and the remaining result
-- rows are waiting for report generation.
UPDATE result.analysis_case AS ac
   SET report_status = CASE
       WHEN ac.case_status = 'failed' THEN 'failed'
       WHEN EXISTS (
           SELECT 1
             FROM result.report_artifact AS ra
            WHERE ra.analysis_case_pk = ac.analysis_case_pk
       ) THEN 'ready'
       ELSE 'generating'
   END
 WHERE ac.report_status IS NULL;

ALTER TABLE result.analysis_case
    ALTER COLUMN report_status SET DEFAULT 'failed',
    ALTER COLUMN report_status SET NOT NULL;

-- ADD CONSTRAINT has no IF NOT EXISTS. Check by relation and name so a second
-- migration run remains a no-op.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM pg_constraint
         WHERE conname = 'ck_result_analysis_case_report_status'
           AND conrelid = 'result.analysis_case'::regclass
    ) THEN
        ALTER TABLE result.analysis_case
            ADD CONSTRAINT ck_result_analysis_case_report_status
            CHECK (report_status IN ('generating', 'ready', 'failed'));
    END IF;

    IF NOT EXISTS (
        SELECT 1
          FROM pg_constraint
         WHERE conname = 'ck_result_evidence_snapshot_comparison_side'
           AND conrelid = 'result.evidence_snapshot'::regclass
    ) THEN
        ALTER TABLE result.evidence_snapshot
            ADD CONSTRAINT ck_result_evidence_snapshot_comparison_side
            CHECK (comparison_side IS NULL OR comparison_side IN ('LEFT', 'RIGHT'));
    END IF;
END
$$;

COMMIT;
