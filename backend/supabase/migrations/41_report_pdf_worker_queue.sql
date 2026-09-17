-- ============================================================================
-- Migration 41: fenced, asynchronous PDF report queue
--
-- A completed live analysis case owns exactly one current ``pdf`` artifact.
-- The analysis materialiser commits the case, the public result projection,
-- and this dispatch row together.  A separate report worker leases the row
-- and may publish storage metadata only with its processing-run fence token.
-- ============================================================================

BEGIN;

-- Migration 34 exposed the latest artifact of any report type as the PDF
-- button state. Preserve its large, audited projection as a base function and
-- narrow only the report member to the latest PDF artifact.
DO $$
BEGIN
    IF to_regprocedure('api.public_analysis_projection_v2_base(uuid,uuid)') IS NULL THEN
        ALTER FUNCTION api.public_analysis_projection_v2(UUID, UUID)
            RENAME TO public_analysis_projection_v2_base;
    END IF;
END;
$$;

CREATE OR REPLACE FUNCTION api.public_analysis_projection_v2(
    p_analysis_case_id UUID,
    p_user_id UUID
)
RETURNS JSONB
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, api, result
AS $$
DECLARE
    v_payload JSONB;
    v_report JSONB;
BEGIN
    v_payload := api.public_analysis_projection_v2_base(
        p_analysis_case_id, p_user_id
    );
    SELECT jsonb_build_object(
               'status', report.status,
               'can_download', report.status = 'ready'
                   AND report.storage_bucket = 'analysis-reports'
                   AND report.storage_object_key IS NOT NULL
                   AND report.content_sha256 IS NOT NULL
                   AND report.expires_at > clock_timestamp(),
               'can_regenerate', false,
               'retry_count', report.retry_count
           )
      INTO v_report
      FROM result.report_artifact AS report
     WHERE report.analysis_case_pk = p_analysis_case_id
       AND report.report_type = 'pdf'
     ORDER BY report.created_at DESC, report.report_artifact_pk DESC
     LIMIT 1;
    RETURN jsonb_set(
        v_payload,
        '{report}',
        COALESCE(v_report, jsonb_build_object(
            'status', 'generating', 'can_download', false,
            'can_regenerate', false, 'retry_count', 0
        )),
        true
    );
END;
$$;

REVOKE ALL ON FUNCTION api.public_analysis_projection_v2_base(UUID, UUID)
FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION api.public_analysis_projection_v2(UUID, UUID)
FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION api.public_analysis_projection_v2(UUID, UUID)
TO service_role;

-- Fail with an actionable diagnostic instead of an opaque index-build error.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
          FROM result.report_artifact
         WHERE report_type = 'pdf'
         GROUP BY analysis_case_pk
        HAVING count(*) > 1
    ) THEN
        RAISE EXCEPTION 'PDF_REPORT_DUPLICATES_REQUIRE_CONSOLIDATION'
            USING ERRCODE = '23505';
    END IF;
END;
$$;

-- One current PDF per case; other report types remain possible. A partial
-- index avoids imposing a new uniqueness rule on historical non-PDF rows.
SET LOCAL ROLE supabase_storage_admin;

UPDATE storage.buckets
   SET public = FALSE, file_size_limit = 26214400
 WHERE id = 'analysis-reports';

SET LOCAL ROLE postgres;

CREATE UNIQUE INDEX IF NOT EXISTS uq_result_report_artifact_one_pdf_per_case
    ON result.report_artifact (analysis_case_pk)
 WHERE report_type = 'pdf';

CREATE TABLE IF NOT EXISTS workspace.report_pdf_dispatch (
    report_artifact_pk UUID PRIMARY KEY
        REFERENCES result.report_artifact(report_artifact_pk)
        ON DELETE CASCADE,
    analysis_case_pk UUID NOT NULL
        REFERENCES result.analysis_case(analysis_case_pk)
        ON DELETE CASCADE,
    source_analysis_run_id UUID NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0
        CHECK (attempt_count BETWEEN 0 AND 3),
    processing_run_pk UUID NULL
        REFERENCES ops.processing_run(processing_run_pk)
        ON DELETE RESTRICT,
    claimed_by TEXT NULL,
    claimed_at TIMESTAMPTZ NULL,
    heartbeat_at TIMESTAMPTZ NULL,
    lease_expires_at TIMESTAMPTZ NULL,
    last_error_code TEXT NULL,
    last_error_message TEXT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (
        (processing_run_pk IS NULL
            AND claimed_by IS NULL
            AND claimed_at IS NULL
            AND heartbeat_at IS NULL
            AND lease_expires_at IS NULL)
        OR
        (processing_run_pk IS NOT NULL
            AND claimed_by IS NOT NULL
            AND claimed_at IS NOT NULL
            AND heartbeat_at IS NOT NULL
            AND lease_expires_at IS NOT NULL)
    ),
    CHECK (lease_expires_at IS NULL OR lease_expires_at > heartbeat_at)
);

CREATE INDEX IF NOT EXISTS ix_workspace_report_pdf_dispatch_poll
    ON workspace.report_pdf_dispatch (created_at, report_artifact_pk)
 WHERE processing_run_pk IS NULL AND attempt_count < 3;

CREATE INDEX IF NOT EXISTS ix_workspace_report_pdf_dispatch_lease
    ON workspace.report_pdf_dispatch (lease_expires_at, report_artifact_pk)
 WHERE processing_run_pk IS NOT NULL;

ALTER TABLE workspace.report_pdf_dispatch ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE workspace.report_pdf_dispatch
FROM PUBLIC, anon, authenticated;

DROP TRIGGER IF EXISTS trg_workspace_report_pdf_dispatch_updated_at
    ON workspace.report_pdf_dispatch;
CREATE TRIGGER trg_workspace_report_pdf_dispatch_updated_at
BEFORE UPDATE ON workspace.report_pdf_dispatch
FOR EACH ROW EXECUTE FUNCTION workspace.set_updated_at();

-- Storage is outside PostgreSQL's transaction boundary. Keep a durable
-- tombstone for every attempt before a worker can upload its private PDF.
-- The key includes the processing-run fence, so an abandoned attempt can
-- never share an object with a later legitimate attempt for the same case.
CREATE TABLE IF NOT EXISTS workspace.report_pdf_object_cleanup (
    report_pdf_object_cleanup_pk UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    report_artifact_pk UUID NOT NULL,
    processing_run_pk UUID NOT NULL,
    storage_bucket TEXT NOT NULL CHECK (storage_bucket = 'analysis-reports'),
    storage_object_key TEXT NOT NULL,
    delete_attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (delete_attempt_count >= 0),
    cleanup_run_pk UUID NULL,
    claimed_by TEXT NULL,
    lease_expires_at TIMESTAMPTZ NULL,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_deleted_at TIMESTAMPTZ NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (storage_bucket, storage_object_key),
    CHECK (
        (cleanup_run_pk IS NULL AND claimed_by IS NULL AND lease_expires_at IS NULL)
        OR
        (cleanup_run_pk IS NOT NULL AND claimed_by IS NOT NULL AND lease_expires_at IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS ix_workspace_report_pdf_object_cleanup_poll
    ON workspace.report_pdf_object_cleanup (next_attempt_at, report_pdf_object_cleanup_pk)
 WHERE cleanup_run_pk IS NULL;

ALTER TABLE workspace.report_pdf_object_cleanup ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE workspace.report_pdf_object_cleanup
FROM PUBLIC, anon, authenticated;

DROP TRIGGER IF EXISTS trg_workspace_report_pdf_object_cleanup_updated_at
    ON workspace.report_pdf_object_cleanup;
CREATE TRIGGER trg_workspace_report_pdf_object_cleanup_updated_at
BEFORE UPDATE ON workspace.report_pdf_object_cleanup
FOR EACH ROW EXECUTE FUNCTION workspace.set_updated_at();

-- The trigger deliberately runs while persist_analysis_result_core_v2 still
-- owns its transaction.  Workers cannot see this queued report until all CPL,
-- FIT, SIM and evidence rows have committed, so a claim never observes a
-- partially materialised projection.
CREATE OR REPLACE FUNCTION workspace.enqueue_pdf_report_for_ready_case()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, workspace, result
AS $$
DECLARE
    v_report_artifact_pk UUID;
    v_previous_processing_run_pk UUID;
    v_previous_storage_bucket TEXT;
    v_previous_storage_object_key TEXT;
BEGIN
    IF NEW.case_status <> 'ready'
       OR NEW.analysis_completed_at IS NULL
       OR COALESCE(NEW.retention_expires_at > clock_timestamp(), FALSE) IS FALSE THEN
        RETURN NEW;
    END IF;
    IF TG_OP = 'UPDATE'
       AND OLD.case_status = 'ready'
       AND OLD.analysis_completed_at IS NOT DISTINCT FROM NEW.analysis_completed_at THEN
        RETURN NEW;
    END IF;

    -- A re-materialised analysis supersedes any older report lease.  Clearing
    -- its dispatch fence without terminalising the old audit run would leave
    -- a misleading forever-running processing_run behind.
    SELECT dispatch.processing_run_pk, report.storage_bucket, report.storage_object_key
      INTO v_previous_processing_run_pk, v_previous_storage_bucket, v_previous_storage_object_key
      FROM workspace.report_pdf_dispatch AS dispatch
      JOIN result.report_artifact AS report
        ON report.report_artifact_pk = dispatch.report_artifact_pk
     WHERE report.analysis_case_pk = NEW.analysis_case_pk
       AND report.report_type = 'pdf'
     FOR UPDATE OF dispatch;
    IF v_previous_processing_run_pk IS NOT NULL THEN
        UPDATE ops.processing_run
           SET status = 'failed', finished_at = clock_timestamp(),
               error_code = 'PDF_REPORT_SUPERSEDED',
               error_message = 'PDF report attempt superseded by a newer analysis result.'
         WHERE processing_run_pk = v_previous_processing_run_pk
           AND status IN ('queued', 'running');
    END IF;
    -- Queue the former ready PDF before replacing its metadata. Each attempt
    -- has a fenced object key, so this cannot target the replacement PDF.
    IF v_previous_storage_bucket = 'analysis-reports'
       AND v_previous_storage_object_key IS NOT NULL THEN
        INSERT INTO workspace.report_pdf_object_cleanup (
            report_artifact_pk, processing_run_pk, storage_bucket,
            storage_object_key
        ) VALUES (
            (SELECT report_artifact_pk FROM result.report_artifact
              WHERE analysis_case_pk = NEW.analysis_case_pk AND report_type = 'pdf'),
            COALESCE(v_previous_processing_run_pk, gen_random_uuid()),
            v_previous_storage_bucket, v_previous_storage_object_key
        ) ON CONFLICT (storage_bucket, storage_object_key) DO NOTHING;
    END IF;


    INSERT INTO result.report_artifact (
        analysis_case_pk, report_type, storage_bucket, storage_object_key,
        content_sha256, mime_type, size_bytes, status, retry_count,
        completed_at, error_code, error_message, expires_at
    ) VALUES (
        NEW.analysis_case_pk, 'pdf', NULL, NULL, NULL, NULL, NULL,
        'generating', 0, NULL, NULL, NULL, NEW.retention_expires_at
    )
    ON CONFLICT (analysis_case_pk) WHERE (report_type = 'pdf') DO UPDATE
       SET storage_bucket = NULL,
           storage_object_key = NULL,
           content_sha256 = NULL,
           mime_type = NULL,
           size_bytes = NULL,
           status = 'generating',
           retry_count = 0,
           completed_at = NULL,
           error_code = NULL,
           error_message = NULL,
           expires_at = EXCLUDED.expires_at
    RETURNING report_artifact_pk INTO v_report_artifact_pk;

    INSERT INTO workspace.report_pdf_dispatch (
        report_artifact_pk, analysis_case_pk, source_analysis_run_id,
        attempt_count, processing_run_pk, claimed_by, claimed_at,
        heartbeat_at, lease_expires_at, last_error_code, last_error_message
    ) VALUES (
        v_report_artifact_pk, NEW.analysis_case_pk, NEW.source_analysis_run_id,
        0, NULL, NULL, NULL, NULL, NULL, NULL, NULL
    )
    ON CONFLICT (report_artifact_pk) DO UPDATE
       SET analysis_case_pk = EXCLUDED.analysis_case_pk,
           source_analysis_run_id = EXCLUDED.source_analysis_run_id,
           attempt_count = 0,
           processing_run_pk = NULL,
           claimed_by = NULL,
           claimed_at = NULL,
           heartbeat_at = NULL,
           lease_expires_at = NULL,
           last_error_code = NULL,
           last_error_message = NULL;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_result_analysis_case_enqueue_pdf_report
    ON result.analysis_case;
CREATE TRIGGER trg_result_analysis_case_enqueue_pdf_report
AFTER INSERT OR UPDATE OF case_status, analysis_completed_at
ON result.analysis_case
FOR EACH ROW EXECUTE FUNCTION workspace.enqueue_pdf_report_for_ready_case();

-- Backfill current, retained completed cases without changing an already
-- ready/failed artifact.  Fresh installations receive the same state as a
-- future analysis completion; production installations do not lose a report
-- which may already have been materialised manually.
INSERT INTO result.report_artifact (
    analysis_case_pk, report_type, storage_bucket, storage_object_key,
    content_sha256, mime_type, size_bytes, status, retry_count,
    completed_at, error_code, error_message, expires_at
)
SELECT analysis_case_pk, 'pdf', NULL, NULL, NULL, NULL, NULL,
       'generating', 0, NULL, NULL, NULL, retention_expires_at
  FROM result.analysis_case
 WHERE case_status = 'ready'
   AND analysis_completed_at IS NOT NULL
   AND retention_expires_at > clock_timestamp()
ON CONFLICT (analysis_case_pk) WHERE (report_type = 'pdf') DO NOTHING;

INSERT INTO workspace.report_pdf_dispatch (
    report_artifact_pk, analysis_case_pk, source_analysis_run_id
)
SELECT report.report_artifact_pk, analysis_case.analysis_case_pk,
       analysis_case.source_analysis_run_id
  FROM result.report_artifact AS report
  JOIN result.analysis_case
    ON analysis_case.analysis_case_pk = report.analysis_case_pk
 WHERE report.report_type = 'pdf'
   AND report.status = 'generating'
   AND analysis_case.case_status = 'ready'
   AND analysis_case.analysis_completed_at IS NOT NULL
   AND analysis_case.retention_expires_at > clock_timestamp()
ON CONFLICT (report_artifact_pk) DO NOTHING;

-- --------------------------------------------------------------------------
-- Worker queue RPCs.  Browser/API roles cannot execute these functions.
-- --------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION workspace.claim_next_pdf_report_v1(
    p_worker_id TEXT,
    p_lease_seconds INTEGER DEFAULT 120
)
RETURNS TABLE (
    report_artifact_id UUID,
    analysis_case_id UUID,
    owner_id UUID,
    source_analysis_run_id UUID,
    result_payload JSONB,
    sim_details JSONB,
    processing_run_pk UUID,
    storage_object_key TEXT,
    attempt_count INTEGER,
    lease_expires_at TIMESTAMPTZ,
    heartbeat_interval_seconds INTEGER
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, api, ops, workspace, result
AS $$
DECLARE
    v_now TIMESTAMPTZ := clock_timestamp();
    v_dispatch workspace.report_pdf_dispatch%ROWTYPE;
    v_owner_id UUID;
    v_processing_run_pk UUID;
    v_storage_object_key TEXT;
    v_result_payload JSONB;
    v_sim_details JSONB;
BEGIN
    IF btrim(COALESCE(p_worker_id, '')) = ''
       OR p_lease_seconds IS NULL OR p_lease_seconds NOT BETWEEN 30 AND 3600 THEN
        RAISE EXCEPTION 'INVALID_PDF_REPORT_WORKER_CLAIM' USING ERRCODE = '22023';
    END IF;

    -- A report never outlives the result it represents.  Mark retention and
    -- exhausted leases terminally so the public projection cannot spin on
    -- "generating" forever.
    UPDATE ops.processing_run AS processing
       SET status = 'failed', finished_at = v_now,
           error_code = 'PDF_REPORT_RETENTION_EXPIRED',
           error_message = 'Report retention expired before completion.'
      FROM workspace.report_pdf_dispatch AS dispatch
      JOIN result.analysis_case AS analysis_case
        ON analysis_case.analysis_case_pk = dispatch.analysis_case_pk
     WHERE processing.processing_run_pk = dispatch.processing_run_pk
       AND analysis_case.retention_expires_at <= v_now
       AND processing.status IN ('queued', 'running');
    UPDATE result.report_artifact AS report
       SET status = 'failed', completed_at = v_now,
           error_code = 'PDF_REPORT_RETENTION_EXPIRED',
           error_message = '보고서 보관 기간이 만료되었습니다.',
           retry_count = GREATEST(dispatch.attempt_count - 1, 0)
      FROM workspace.report_pdf_dispatch AS dispatch
      JOIN result.analysis_case AS analysis_case
        ON analysis_case.analysis_case_pk = dispatch.analysis_case_pk
     WHERE report.report_artifact_pk = dispatch.report_artifact_pk
       AND report.status = 'generating'
       AND analysis_case.retention_expires_at <= v_now;
    UPDATE workspace.report_pdf_dispatch AS dispatch
       SET processing_run_pk = NULL, claimed_by = NULL, claimed_at = NULL,
           heartbeat_at = NULL, lease_expires_at = NULL,
           last_error_code = 'PDF_REPORT_RETENTION_EXPIRED',
           last_error_message = 'Report retention expired before completion.'
      FROM result.analysis_case AS analysis_case
     WHERE analysis_case.analysis_case_pk = dispatch.analysis_case_pk
       AND analysis_case.retention_expires_at <= v_now
       AND dispatch.last_error_code IS DISTINCT FROM 'PDF_REPORT_RETENTION_EXPIRED';

    UPDATE ops.processing_run AS processing
       SET status = 'failed', finished_at = v_now,
           error_code = 'PDF_REPORT_MAX_ATTEMPTS_EXCEEDED',
           error_message = 'PDF report worker lease expired after three attempts.'
      FROM workspace.report_pdf_dispatch AS dispatch
      JOIN result.report_artifact AS report
        ON report.report_artifact_pk = dispatch.report_artifact_pk
     WHERE processing.processing_run_pk = dispatch.processing_run_pk
       AND report.status = 'generating'
       AND dispatch.attempt_count >= 3
       AND (dispatch.lease_expires_at IS NULL OR dispatch.lease_expires_at <= v_now)
       AND processing.status IN ('queued', 'running');
    UPDATE result.report_artifact AS report
       SET status = 'failed', completed_at = v_now,
           error_code = 'PDF_REPORT_MAX_ATTEMPTS_EXCEEDED',
           error_message = 'PDF 보고서 생성 시간이 초과되었습니다.',
           retry_count = GREATEST(dispatch.attempt_count - 1, 0)
      FROM workspace.report_pdf_dispatch AS dispatch
     WHERE report.report_artifact_pk = dispatch.report_artifact_pk
       AND report.status = 'generating'
       AND dispatch.attempt_count >= 3
       AND (dispatch.lease_expires_at IS NULL OR dispatch.lease_expires_at <= v_now);
    UPDATE workspace.report_pdf_dispatch AS dispatch
       SET processing_run_pk = NULL, claimed_by = NULL, claimed_at = NULL,
           heartbeat_at = NULL, lease_expires_at = NULL,
           last_error_code = 'PDF_REPORT_MAX_ATTEMPTS_EXCEEDED',
           last_error_message = 'PDF report worker lease expired after three attempts.'
      FROM result.report_artifact AS report
     WHERE report.report_artifact_pk = dispatch.report_artifact_pk
       AND report.status = 'failed'
       AND dispatch.attempt_count >= 3
       AND (dispatch.lease_expires_at IS NULL OR dispatch.lease_expires_at <= v_now);

    SELECT dispatch.*
      INTO v_dispatch
      FROM workspace.report_pdf_dispatch AS dispatch
      JOIN result.report_artifact AS report
        ON report.report_artifact_pk = dispatch.report_artifact_pk
      JOIN result.analysis_case AS analysis_case
        ON analysis_case.analysis_case_pk = dispatch.analysis_case_pk
     WHERE report.status = 'generating'
       AND analysis_case.case_status = 'ready'
       AND analysis_case.retention_expires_at > v_now
       AND dispatch.attempt_count < 3
       AND (dispatch.processing_run_pk IS NULL OR dispatch.lease_expires_at <= v_now)
     ORDER BY dispatch.created_at, dispatch.report_artifact_pk
     LIMIT 1
     FOR UPDATE OF dispatch, report SKIP LOCKED;
    IF NOT FOUND THEN
        RETURN;
    END IF;

    IF v_dispatch.processing_run_pk IS NOT NULL THEN
        UPDATE ops.processing_run
           SET status = 'failed', finished_at = v_now,
               error_code = 'PDF_REPORT_LEASE_EXPIRED',
               error_message = 'PDF report worker lease expired before completion.'
         WHERE processing_run_pk = v_dispatch.processing_run_pk
           AND status IN ('queued', 'running');
    END IF;

    SELECT user_id INTO v_owner_id
      FROM result.analysis_case
     WHERE analysis_case_pk = v_dispatch.analysis_case_pk;

    INSERT INTO ops.processing_run AS processing (
        source_analysis_run_id, run_type, status, started_at, run_metadata
    ) VALUES (
        NULL, 'report_pdf', 'running', v_now,
        jsonb_build_object(
            'source_analysis_run_id', v_dispatch.source_analysis_run_id,
            'report_artifact_id', v_dispatch.report_artifact_pk,
            'analysis_case_id', v_dispatch.analysis_case_pk,
            'worker_id', btrim(p_worker_id),
            'attempt_no', v_dispatch.attempt_count + 1,
            'lease_seconds', p_lease_seconds
        )
    ) RETURNING processing.processing_run_pk INTO v_processing_run_pk;

    v_storage_object_key := v_owner_id::text || '/' ||
        v_dispatch.analysis_case_pk::text || '/pdf/' ||
        v_processing_run_pk::text || '.pdf';
    INSERT INTO workspace.report_pdf_object_cleanup (
        report_artifact_pk, processing_run_pk, storage_bucket, storage_object_key
    ) VALUES (
        v_dispatch.report_artifact_pk, v_processing_run_pk,
        'analysis-reports', v_storage_object_key
    ) ON CONFLICT (storage_bucket, storage_object_key) DO NOTHING;

    SELECT api.public_analysis_projection_v2(v_dispatch.analysis_case_pk, v_owner_id)
      INTO v_result_payload;
    SELECT COALESCE(jsonb_agg(
               api.rpc_get_sim_candidate_detail_v2(
                   v_owner_id, candidate.sim_candidate_pk
               ) ORDER BY candidate.rank_no, candidate.sim_candidate_pk
           ), '[]'::jsonb)
      INTO v_sim_details
      FROM result.sim_candidate AS candidate
     WHERE candidate.analysis_case_pk = v_dispatch.analysis_case_pk
       AND candidate.public_metadata IS NOT NULL
       AND candidate.public_axes IS NOT NULL;

    UPDATE workspace.report_pdf_dispatch
       SET attempt_count = v_dispatch.attempt_count + 1,
           processing_run_pk = v_processing_run_pk,
           claimed_by = btrim(p_worker_id), claimed_at = v_now,
           heartbeat_at = v_now,
           lease_expires_at = v_now + make_interval(secs => p_lease_seconds),
           last_error_code = NULL, last_error_message = NULL
     WHERE report_artifact_pk = v_dispatch.report_artifact_pk;
    UPDATE result.report_artifact
       SET retry_count = GREATEST(v_dispatch.attempt_count, 0),
           error_code = NULL, error_message = NULL
     WHERE report_artifact_pk = v_dispatch.report_artifact_pk;

    RETURN QUERY SELECT
        v_dispatch.report_artifact_pk, v_dispatch.analysis_case_pk, v_owner_id,
        v_dispatch.source_analysis_run_id, v_result_payload, v_sim_details,
        v_processing_run_pk, v_storage_object_key, v_dispatch.attempt_count + 1,
        v_now + make_interval(secs => p_lease_seconds), 30;
END;
$$;

CREATE OR REPLACE FUNCTION workspace.heartbeat_pdf_report_v1(
    p_report_artifact_id UUID,
    p_processing_run_pk UUID,
    p_lease_seconds INTEGER DEFAULT 120
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, workspace, result
AS $$
DECLARE
    v_now TIMESTAMPTZ := clock_timestamp();
    v_live BOOLEAN := FALSE;
BEGIN
    IF p_lease_seconds IS NULL OR p_lease_seconds NOT BETWEEN 30 AND 3600 THEN
        RAISE EXCEPTION 'LEASE_SECONDS_OUT_OF_RANGE' USING ERRCODE = '22023';
    END IF;
    UPDATE workspace.report_pdf_dispatch AS dispatch
       SET heartbeat_at = v_now,
           lease_expires_at = v_now + make_interval(secs => p_lease_seconds)
      FROM result.report_artifact AS report,
           result.analysis_case AS analysis_case
     WHERE dispatch.report_artifact_pk = p_report_artifact_id
       AND dispatch.processing_run_pk = p_processing_run_pk
       AND report.report_artifact_pk = dispatch.report_artifact_pk
       AND analysis_case.analysis_case_pk = dispatch.analysis_case_pk
       AND report.status = 'generating'
       AND analysis_case.case_status = 'ready'
       AND analysis_case.retention_expires_at > v_now
       AND dispatch.lease_expires_at > v_now
    RETURNING TRUE INTO v_live;
    RETURN COALESCE(v_live, FALSE);
END;
$$;

CREATE OR REPLACE FUNCTION workspace.complete_pdf_report_v1(
    p_report_artifact_id UUID,
    p_processing_run_pk UUID,
    p_storage_bucket TEXT,
    p_storage_object_key TEXT,
    p_content_sha256 TEXT,
    p_mime_type TEXT,
    p_size_bytes BIGINT
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, ops, workspace, result
AS $$
DECLARE
    v_now TIMESTAMPTZ := clock_timestamp();
    v_expires_at TIMESTAMPTZ;
    v_analysis_case_pk UUID;
    v_user_id UUID;
BEGIN
    IF p_storage_bucket <> 'analysis-reports'
       OR btrim(COALESCE(p_storage_object_key, '')) = ''
       OR p_storage_object_key LIKE '/%' OR p_storage_object_key LIKE '%..%'
       OR COALESCE(p_content_sha256, '') !~ '^[0-9a-f]{64}$'
       OR p_mime_type <> 'application/pdf'
       OR p_size_bytes IS NULL OR p_size_bytes <= 0 OR p_size_bytes > 26214400 THEN
        RAISE EXCEPTION 'INVALID_PDF_REPORT_ARTIFACT' USING ERRCODE = '22023';
    END IF;

    SELECT analysis_case.retention_expires_at, analysis_case.analysis_case_pk, analysis_case.user_id
      INTO v_expires_at, v_analysis_case_pk, v_user_id
      FROM workspace.report_pdf_dispatch AS dispatch
      JOIN result.report_artifact AS report
        ON report.report_artifact_pk = dispatch.report_artifact_pk
      JOIN result.analysis_case AS analysis_case
        ON analysis_case.analysis_case_pk = dispatch.analysis_case_pk
     WHERE dispatch.report_artifact_pk = p_report_artifact_id
       AND dispatch.processing_run_pk = p_processing_run_pk
       AND dispatch.lease_expires_at > v_now
       AND report.status = 'generating'
       AND analysis_case.case_status = 'ready'
       AND analysis_case.retention_expires_at > v_now
     FOR UPDATE OF dispatch, report;
    IF NOT FOUND THEN
        RETURN FALSE;
    END IF;
    IF p_storage_object_key <> v_user_id::text || '/' || v_analysis_case_pk::text || '/pdf/' || p_processing_run_pk::text || '.pdf' THEN
        RAISE EXCEPTION 'INVALID_PDF_REPORT_OBJECT_KEY' USING ERRCODE = '22023';
    END IF;

    UPDATE ops.processing_run
       SET status = 'succeeded', finished_at = v_now,
           error_code = NULL, error_message = NULL
     WHERE processing_run_pk = p_processing_run_pk
       AND status = 'running';
    IF NOT FOUND THEN
        RAISE EXCEPTION 'PROCESSING_RUN_NOT_LIVE' USING ERRCODE = 'P0001';
    END IF;

    UPDATE result.report_artifact
       SET storage_bucket = p_storage_bucket,
           storage_object_key = p_storage_object_key,
           content_sha256 = p_content_sha256,
           mime_type = p_mime_type,
           size_bytes = p_size_bytes,
           status = 'ready', completed_at = v_now,
           error_code = NULL, error_message = NULL,
           expires_at = v_expires_at
     WHERE report_artifact_pk = p_report_artifact_id;
    UPDATE workspace.report_pdf_dispatch
       SET processing_run_pk = NULL, claimed_by = NULL, claimed_at = NULL,
           heartbeat_at = NULL, lease_expires_at = NULL,
           last_error_code = NULL, last_error_message = NULL
     WHERE report_artifact_pk = p_report_artifact_id;
    DELETE FROM workspace.report_pdf_object_cleanup
     WHERE report_artifact_pk = p_report_artifact_id
       AND processing_run_pk = p_processing_run_pk
       AND storage_bucket = p_storage_bucket
       AND storage_object_key = p_storage_object_key;
    RETURN TRUE;
END;
$$;

CREATE OR REPLACE FUNCTION workspace.fail_pdf_report_v1(
    p_report_artifact_id UUID,
    p_processing_run_pk UUID,
    p_error_code TEXT,
    p_error_message TEXT,
    p_internal_error_message TEXT DEFAULT NULL
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, ops, workspace, result
AS $$
DECLARE
    v_now TIMESTAMPTZ := clock_timestamp();
    v_attempt_count INTEGER;
BEGIN
    -- The public surface is fixed by this trusted queue.  Exception text is
    -- stored only in the private dispatch row after adapter-side redaction.
    IF p_error_code <> 'PDF_REPORT_FAILED'
       OR p_error_message <> 'PDF 보고서를 생성하지 못했습니다. 잠시 후 다시 시도해 주세요.' THEN
        RAISE EXCEPTION 'INVALID_PDF_REPORT_FAILURE' USING ERRCODE = '22023';
    END IF;

    SELECT dispatch.attempt_count
      INTO v_attempt_count
      FROM workspace.report_pdf_dispatch AS dispatch
      JOIN result.report_artifact AS report
        ON report.report_artifact_pk = dispatch.report_artifact_pk
      JOIN result.analysis_case AS analysis_case
        ON analysis_case.analysis_case_pk = dispatch.analysis_case_pk
     WHERE dispatch.report_artifact_pk = p_report_artifact_id
       AND dispatch.processing_run_pk = p_processing_run_pk
       AND dispatch.lease_expires_at > v_now
       AND report.status = 'generating'
       AND analysis_case.case_status = 'ready'
       AND analysis_case.retention_expires_at > v_now
     FOR UPDATE OF dispatch, report;
    IF NOT FOUND THEN
        RETURN FALSE;
    END IF;

    UPDATE ops.processing_run
       SET status = 'failed', finished_at = v_now,
           error_code = p_error_code, error_message = p_error_message
     WHERE processing_run_pk = p_processing_run_pk
       AND status = 'running';
    IF NOT FOUND THEN
        RAISE EXCEPTION 'PROCESSING_RUN_NOT_LIVE' USING ERRCODE = 'P0001';
    END IF;

    UPDATE result.report_artifact
       SET status = CASE WHEN v_attempt_count >= 3 THEN 'failed' ELSE 'generating' END,
           completed_at = CASE WHEN v_attempt_count >= 3 THEN v_now ELSE NULL END,
           retry_count = GREATEST(v_attempt_count - 1, 0),
           error_code = CASE WHEN v_attempt_count >= 3 THEN p_error_code ELSE NULL END,
           error_message = CASE WHEN v_attempt_count >= 3 THEN p_error_message ELSE NULL END
     WHERE report_artifact_pk = p_report_artifact_id;
    UPDATE workspace.report_pdf_dispatch
       SET processing_run_pk = NULL, claimed_by = NULL, claimed_at = NULL,
           heartbeat_at = NULL, lease_expires_at = NULL,
           last_error_code = p_error_code,
           last_error_message = left(COALESCE(p_internal_error_message, ''), 1000)
     WHERE report_artifact_pk = p_report_artifact_id;
    RETURN TRUE;
END;
$$;

CREATE OR REPLACE FUNCTION workspace.claim_next_pdf_report_cleanup_v1(
    p_worker_id TEXT, p_lease_seconds INTEGER DEFAULT 120
) RETURNS TABLE (
    report_pdf_object_cleanup_id UUID, storage_bucket TEXT,
    storage_object_key TEXT, cleanup_run_pk UUID
) LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, workspace, result
AS $$
DECLARE v_now TIMESTAMPTZ := clock_timestamp();
BEGIN
    IF btrim(COALESCE(p_worker_id, '')) = ''
       OR p_lease_seconds IS NULL OR p_lease_seconds NOT BETWEEN 30 AND 3600 THEN
        RAISE EXCEPTION 'INVALID_PDF_REPORT_CLEANUP_CLAIM' USING ERRCODE = '22023';
    END IF;
    INSERT INTO workspace.report_pdf_object_cleanup (
        report_artifact_pk, processing_run_pk, storage_bucket, storage_object_key
    )
    SELECT report.report_artifact_pk, gen_random_uuid(),
           report.storage_bucket, report.storage_object_key
      FROM result.report_artifact AS report
     WHERE report.report_type = 'pdf' AND report.status = 'ready'
       AND report.expires_at <= v_now
       AND report.storage_bucket = 'analysis-reports'
       AND report.storage_object_key IS NOT NULL
    ON CONFLICT (storage_bucket, storage_object_key) DO NOTHING;

    WITH candidate AS (
        SELECT cleanup.report_pdf_object_cleanup_pk,
               CASE
                   WHEN dispatch.processing_run_pk = cleanup.processing_run_pk
                    AND dispatch.lease_expires_at <= v_now
                   THEN cleanup.processing_run_pk
                   ELSE NULL
               END AS expired_processing_run_pk
          FROM workspace.report_pdf_object_cleanup AS cleanup
          JOIN workspace.report_pdf_dispatch AS dispatch
            ON dispatch.report_artifact_pk = cleanup.report_artifact_pk
          JOIN result.report_artifact AS owned_report
            ON owned_report.report_artifact_pk = cleanup.report_artifact_pk
         WHERE cleanup.next_attempt_at <= v_now
           AND (cleanup.cleanup_run_pk IS NULL OR cleanup.lease_expires_at <= v_now)
           -- Never delete a current, downloadable report. An expired report is
           -- deliberately eligible for physical retention cleanup.
           AND NOT (
               owned_report.storage_bucket = cleanup.storage_bucket
               AND owned_report.storage_object_key = cleanup.storage_object_key
               AND owned_report.status = 'ready'
               AND owned_report.expires_at > v_now
           )
           -- A live owner may still atomically publish this exact key.
           AND (
               dispatch.processing_run_pk IS DISTINCT FROM cleanup.processing_run_pk
               OR dispatch.lease_expires_at IS NULL
               OR dispatch.lease_expires_at <= v_now
           )
         ORDER BY cleanup.next_attempt_at, cleanup.report_pdf_object_cleanup_pk
         LIMIT 1
         FOR UPDATE OF dispatch, owned_report SKIP LOCKED
    ),
    failed_processing AS (
        UPDATE ops.processing_run AS processing
           SET status = 'failed', finished_at = v_now,
               error_code = 'PDF_REPORT_LEASE_EXPIRED',
               error_message = 'PDF report worker lease expired before cleanup.'
          FROM candidate
         WHERE candidate.expired_processing_run_pk IS NOT NULL
           AND processing.processing_run_pk = candidate.expired_processing_run_pk
           AND processing.status IN ('queued', 'running')
        RETURNING processing.processing_run_pk
    ),
    fenced_dispatch AS (
        UPDATE workspace.report_pdf_dispatch AS dispatch
           SET processing_run_pk = NULL, claimed_by = NULL, claimed_at = NULL,
               heartbeat_at = NULL, lease_expires_at = NULL,
               last_error_code = 'PDF_REPORT_LEASE_EXPIRED',
               last_error_message = 'PDF report worker lease expired before cleanup.'
          FROM candidate
         WHERE candidate.expired_processing_run_pk IS NOT NULL
           AND dispatch.processing_run_pk = candidate.expired_processing_run_pk
           AND dispatch.lease_expires_at <= v_now
        RETURNING dispatch.report_artifact_pk
    )
    UPDATE workspace.report_pdf_object_cleanup AS cleanup
       SET cleanup_run_pk = gen_random_uuid(), claimed_by = btrim(p_worker_id),
           lease_expires_at = v_now + make_interval(secs => p_lease_seconds)
      FROM candidate
     WHERE cleanup.report_pdf_object_cleanup_pk = candidate.report_pdf_object_cleanup_pk
    RETURNING cleanup.report_pdf_object_cleanup_pk, cleanup.storage_bucket,
              cleanup.storage_object_key, cleanup.cleanup_run_pk;
END;
$$;

CREATE OR REPLACE FUNCTION workspace.complete_pdf_report_cleanup_v1(
    p_report_pdf_object_cleanup_id UUID, p_cleanup_run_pk UUID
) RETURNS BOOLEAN LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, workspace
AS $$
BEGIN
    UPDATE workspace.report_pdf_object_cleanup
       SET cleanup_run_pk = NULL, claimed_by = NULL, lease_expires_at = NULL,
           delete_attempt_count = delete_attempt_count + 1,
           last_deleted_at = clock_timestamp(),
           -- One delayed recheck covers a stale timed-out PUT which finished
           -- after its lease fence; then keep a terminal audit tombstone.
           next_attempt_at = CASE WHEN delete_attempt_count < 1
               THEN clock_timestamp() + interval '5 minutes'
               ELSE 'infinity'::timestamptz END
     WHERE report_pdf_object_cleanup_pk = p_report_pdf_object_cleanup_id
       AND cleanup_run_pk = p_cleanup_run_pk;
    RETURN FOUND;
END;
$$;

REVOKE ALL ON FUNCTION workspace.enqueue_pdf_report_for_ready_case() FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION workspace.claim_next_pdf_report_v1(TEXT, INTEGER) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION workspace.heartbeat_pdf_report_v1(UUID, UUID, INTEGER) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION workspace.complete_pdf_report_v1(UUID, UUID, TEXT, TEXT, TEXT, TEXT, BIGINT) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION workspace.fail_pdf_report_v1(UUID, UUID, TEXT, TEXT, TEXT) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION workspace.claim_next_pdf_report_cleanup_v1(TEXT, INTEGER) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION workspace.complete_pdf_report_cleanup_v1(UUID, UUID) FROM PUBLIC, anon, authenticated;

GRANT USAGE ON SCHEMA workspace TO service_role;
GRANT EXECUTE ON FUNCTION workspace.claim_next_pdf_report_v1(TEXT, INTEGER),
                         workspace.heartbeat_pdf_report_v1(UUID, UUID, INTEGER),
                         workspace.complete_pdf_report_v1(UUID, UUID, TEXT, TEXT, TEXT, TEXT, BIGINT),
                         workspace.fail_pdf_report_v1(UUID, UUID, TEXT, TEXT, TEXT),
                         workspace.claim_next_pdf_report_cleanup_v1(TEXT, INTEGER),
                         workspace.complete_pdf_report_cleanup_v1(UUID, UUID)
TO service_role;

COMMIT;
