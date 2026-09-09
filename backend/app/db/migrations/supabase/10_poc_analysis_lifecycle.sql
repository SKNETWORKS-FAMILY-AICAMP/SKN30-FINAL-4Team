-- PoC: immediate upload and analysis-run lifecycle.
-- Prerequisite: baseline migrations 01~09 from data_retention_architecture_20260902.zip.

BEGIN;

ALTER TABLE workspace.analysis_run
  DROP CONSTRAINT IF EXISTS analysis_run_status_check,
  ADD CONSTRAINT analysis_run_status_allowed
    CHECK (status IN (
      'uploading', 'queued', 'running', 'succeeded', 'failed', 'cancelled', 'cleanup_pending'
    )),
  ADD COLUMN source_bucket TEXT NOT NULL,
  ADD COLUMN source_object_key TEXT NOT NULL,
  ADD COLUMN original_filename TEXT NOT NULL,
  ADD COLUMN declared_mime_type TEXT NOT NULL,
  ADD COLUMN declared_size_bytes BIGINT NOT NULL CHECK (declared_size_bytes > 0),
  ADD COLUMN idempotency_key UUID NOT NULL DEFAULT gen_random_uuid(),
  ADD COLUMN upload_completed_at TIMESTAMPTZ NULL,
  ADD COLUMN available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  ADD COLUMN heartbeat_at TIMESTAMPTZ NULL,
  ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  ADD COLUMN error_code TEXT NULL,
  ADD COLUMN failed_at TIMESTAMPTZ NULL,
  ADD CONSTRAINT analysis_run_source_object_unique UNIQUE (source_bucket, source_object_key),
  ADD CONSTRAINT analysis_run_user_idempotency_unique UNIQUE (user_id, idempotency_key),
  ADD CONSTRAINT analysis_run_upload_shape
    CHECK (
      (status = 'uploading' AND upload_completed_at IS NULL)
      OR
      (status <> 'uploading')
    );

CREATE UNIQUE INDEX uq_workspace_analysis_run_user_in_flight
  ON workspace.analysis_run (user_id)
  WHERE status IN ('uploading', 'queued', 'running');

CREATE INDEX ix_workspace_analysis_run_worker_claim
  ON workspace.analysis_run (status, available_at, created_at)
  WHERE status = 'queued';

CREATE INDEX ix_workspace_analysis_run_heartbeat
  ON workspace.analysis_run (status, heartbeat_at)
  WHERE status = 'running';

COMMIT;
