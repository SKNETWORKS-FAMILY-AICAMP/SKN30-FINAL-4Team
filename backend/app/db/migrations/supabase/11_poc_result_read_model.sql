-- PoC: durable result read model. Workspace is disposable after success.

BEGIN;

ALTER TABLE result.analysis_case
  DROP CONSTRAINT IF EXISTS analysis_case_case_status_check,
  ADD CONSTRAINT analysis_case_status_allowed
    CHECK (case_status IN ('processing', 'ready', 'failed')),
  ADD COLUMN original_filename TEXT NOT NULL,
  ADD COLUMN program_name TEXT NULL,
  ADD COLUMN input_profile_snapshot JSONB NOT NULL,
  ADD COLUMN result_schema_version TEXT NOT NULL DEFAULT 'v1';

ALTER TABLE result.axis_result
  ALTER COLUMN summary_text SET NOT NULL,
  ALTER COLUMN result_data SET NOT NULL;

ALTER TABLE result.analysis_session
  DROP CONSTRAINT IF EXISTS analysis_session_status_check,
  ADD COLUMN user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE RESTRICT,
  ADD COLUMN close_reason TEXT NULL,
  ADD CONSTRAINT analysis_session_status_allowed
    CHECK (status IN ('active', 'closed')),
  ADD CONSTRAINT analysis_session_close_shape
    CHECK (
      (status = 'active' AND closed_at IS NULL AND close_reason IS NULL)
      OR
      (status = 'closed' AND closed_at IS NOT NULL
       AND close_reason IN ('new_analysis', 'inactivity'))
    );

CREATE UNIQUE INDEX uq_result_analysis_session_active_user
  ON result.analysis_session (user_id)
  WHERE status = 'active';

CREATE INDEX ix_result_analysis_session_user_status_expiry
  ON result.analysis_session (user_id, status, expires_at);

ALTER TABLE result.sim_candidate
  ADD COLUMN announcement_title TEXT NOT NULL,
  ADD COLUMN issuing_organization TEXT NULL,
  ADD COLUMN source_url TEXT NULL,
  ADD COLUMN notice_status TEXT NULL,
  ADD COLUMN summary_text TEXT NULL,
  ADD COLUMN comparable_axes TEXT[] NOT NULL DEFAULT '{}';

COMMIT;
