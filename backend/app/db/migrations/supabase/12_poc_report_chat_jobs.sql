-- PoC: report lifecycle, durable chat turn state, and Postgres worker queue.

BEGIN;

CREATE TABLE result.report_generation (
  report_generation_pk UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  analysis_case_pk UUID NOT NULL REFERENCES result.analysis_case(analysis_case_pk) ON DELETE CASCADE,
  report_type TEXT NOT NULL CHECK (report_type = 'final_pdf'),
  status TEXT NOT NULL CHECK (status IN ('generating', 'ready', 'retryable_failed', 'failed')),
  retry_count INTEGER NOT NULL DEFAULT 0 CHECK (retry_count BETWEEN 0 AND 3),
  error_code TEXT NULL,
  requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  completed_at TIMESTAMPTZ NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (analysis_case_pk, report_type)
);

ALTER TABLE result.conversation_message
  ALTER COLUMN content DROP NOT NULL,
  ADD COLUMN reply_to_message_pk UUID NULL REFERENCES result.conversation_message(message_pk) ON DELETE SET NULL,
  ADD COLUMN message_status TEXT NOT NULL DEFAULT 'completed'
    CHECK (message_status IN ('generating', 'completed', 'failed')),
  ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
  ADD COLUMN error_code TEXT NULL,
  ADD COLUMN updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  ADD CONSTRAINT conversation_message_role_shape
    CHECK (
      (role = 'user' AND message_status = 'completed' AND content IS NOT NULL AND reply_to_message_pk IS NULL)
      OR
      (role = 'assistant' AND reply_to_message_pk IS NOT NULL)
    );

ALTER TABLE ops.processing_run
  DROP CONSTRAINT IF EXISTS processing_run_status_check,
  ADD CONSTRAINT processing_run_status_allowed
    CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
  ADD COLUMN analysis_case_pk UUID NULL REFERENCES result.analysis_case(analysis_case_pk) ON DELETE CASCADE,
  ADD COLUMN assistant_message_pk UUID NULL REFERENCES result.conversation_message(message_pk) ON DELETE CASCADE,
  ADD COLUMN report_generation_pk UUID NULL REFERENCES result.report_generation(report_generation_pk) ON DELETE CASCADE,
  ADD COLUMN available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  ADD COLUMN heartbeat_at TIMESTAMPTZ NULL,
  ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0);

CREATE UNIQUE INDEX uq_ops_processing_assistant_message
  ON ops.processing_run (assistant_message_pk)
  WHERE assistant_message_pk IS NOT NULL;

CREATE UNIQUE INDEX uq_ops_processing_report_generation
  ON ops.processing_run (report_generation_pk)
  WHERE report_generation_pk IS NOT NULL AND status IN ('queued', 'running');

CREATE INDEX ix_ops_processing_worker_claim
  ON ops.processing_run (run_type, status, available_at, created_at)
  WHERE status = 'queued';

CREATE INDEX ix_ops_processing_heartbeat
  ON ops.processing_run (status, heartbeat_at)
  WHERE status = 'running';

CREATE INDEX ix_result_report_generation_case
  ON result.report_generation (analysis_case_pk, status);

CREATE INDEX ix_result_conversation_message_reply
  ON result.conversation_message (reply_to_message_pk)
  WHERE reply_to_message_pk IS NOT NULL;

COMMIT;
