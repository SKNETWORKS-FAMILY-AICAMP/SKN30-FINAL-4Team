-- ============================================================================
-- Migration 39: atomic upload finalisation under global queue admission
--
-- Migration 37 correctly excludes expired uploading reservations from the
-- admission count.  Finalisation must therefore not convert one of those
-- non-counted rows to queued outside the same admission lock.
-- ============================================================================

BEGIN;

-- This is intentionally a service-role-only command boundary.  It owns the
-- source-artifact insert and queued transition, so FastAPI cannot observe a
-- source row without also holding the global admission decision that made the
-- run visible to a worker.
CREATE OR REPLACE FUNCTION workspace.finalize_analysis_upload_v2(
    p_analysis_run_id UUID,
    p_user_id UUID,
    p_original_filename TEXT,
    p_declared_mime_type TEXT,
    p_declared_size_bytes BIGINT,
    p_source_bucket TEXT,
    p_source_object_key TEXT,
    p_source_content_sha256 TEXT,
    p_source_mime_type TEXT,
    p_source_size_bytes BIGINT,
    p_queued_ttl_seconds INTEGER DEFAULT 3600
)
RETURNS TABLE (
    analysis_run_id UUID,
    status TEXT,
    analysis_case_id UUID,
    error_code TEXT,
    error_message TEXT,
    created_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ,
    outcome TEXT,
    cleanup_objects JSONB
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, workspace
AS $$
DECLARE
    v_run workspace.analysis_run%ROWTYPE;
    v_dispatch workspace.analysis_run_dispatch%ROWTYPE;
    v_now TIMESTAMPTZ;
    v_artifact_matches BOOLEAN;
BEGIN
    IF p_analysis_run_id IS NULL OR p_user_id IS NULL
       OR btrim(COALESCE(p_original_filename, '')) = ''
       OR btrim(COALESCE(p_declared_mime_type, '')) = ''
       OR p_declared_size_bytes IS NULL OR p_declared_size_bytes < 0
       OR btrim(COALESCE(p_source_bucket, '')) = ''
       OR btrim(COALESCE(p_source_object_key, '')) = ''
       OR COALESCE(p_source_content_sha256, '') !~* '^[0-9a-f]{64}$'
       OR btrim(COALESCE(p_source_mime_type, '')) = ''
       OR p_source_size_bytes IS NULL OR p_source_size_bytes < 0
       OR p_source_size_bytes <> p_declared_size_bytes
       OR p_queued_ttl_seconds IS NULL
       OR p_queued_ttl_seconds NOT BETWEEN 60 AND 3600 THEN
        RAISE EXCEPTION 'INVALID_UPLOAD_FINALIZATION' USING ERRCODE = '22023';
    END IF;

    -- Lock this source first.  Reservation replay takes the owner/run lock
    -- before it takes the global lock, so this order avoids an owner/global
    -- lock inversion while still serialising finalise vs exact replay.
    SELECT run.*
      INTO v_run
      FROM workspace.analysis_run AS run
     WHERE run.analysis_run_pk = p_analysis_run_id
       AND run.user_id = p_user_id
     FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ANALYSIS_UPLOAD_NOT_FOUND' USING ERRCODE = 'P0002';
    END IF;

    SELECT dispatch.*
      INTO v_dispatch
      FROM workspace.analysis_run_dispatch AS dispatch
     WHERE dispatch.analysis_run_pk = v_run.analysis_run_pk
     FOR UPDATE;
    IF NOT FOUND
       OR ROW(
           v_run.original_filename,
           v_run.declared_mime_type,
           v_run.declared_size_bytes,
           v_dispatch.source_bucket,
           v_dispatch.source_object_key,
           lower(v_dispatch.source_content_sha256)
       ) IS DISTINCT FROM ROW(
           p_original_filename,
           p_declared_mime_type,
           p_declared_size_bytes,
           p_source_bucket,
           p_source_object_key,
           lower(p_source_content_sha256)
       ) THEN
        RAISE EXCEPTION 'UPLOAD_FINALIZATION_SOURCE_MISMATCH' USING ERRCODE = '22023';
    END IF;

    -- The exact lock key is shared with migration 37's admission helper.
    -- Hold it through source registration and the state update.
    PERFORM pg_advisory_xact_lock(
        hashtextextended('prereview/global-queue-admission/v2', 0)
    );
    v_now := clock_timestamp();

    IF v_run.status = 'queued' THEN
        SELECT EXISTS (
            SELECT 1
              FROM workspace.source_artifact AS artifact
             WHERE artifact.analysis_run_pk = v_run.analysis_run_pk
               AND artifact.artifact_type = 'source'
               AND artifact.storage_bucket = p_source_bucket
               AND artifact.storage_object_key = p_source_object_key
               AND lower(artifact.content_sha256) = lower(p_source_content_sha256)
               AND artifact.mime_type = p_source_mime_type
               AND artifact.size_bytes = p_source_size_bytes
        ) INTO v_artifact_matches;
        IF NOT v_artifact_matches THEN
            RAISE EXCEPTION 'QUEUED_SOURCE_UNVERIFIABLE' USING ERRCODE = 'P0002';
        END IF;

        RETURN QUERY SELECT v_run.analysis_run_pk, v_run.status,
                            v_run.analysis_case_pk, v_run.error_code,
                            v_run.error_message, v_run.created_at,
                            v_run.updated_at, 'queued_replay', '[]'::jsonb;
        RETURN;
    END IF;

    IF v_run.status <> 'uploading' THEN
        RAISE EXCEPTION 'ANALYSIS_UPLOAD_NOT_FINALIZABLE' USING ERRCODE = 'P0002';
    END IF;

    -- A live upload already owns the slot granted at reservation time.  An
    -- expired row was excluded from the count, so it must obtain a fresh slot
    -- while this same transaction lock is held before becoming queued.
    IF v_run.expires_at <= v_now THEN
        BEGIN
            PERFORM workspace.admit_global_queue_work_v2();
        EXCEPTION WHEN SQLSTATE '53000' THEN
            -- The browser has already written this deterministic private
            -- object.  Make cleanup durable and return its exact key rather
            -- than leaving an expired uploading row that no retry can safely
            -- distinguish from a lost finalisation acknowledgement.
            UPDATE workspace.analysis_run
               SET status = 'cleanup_pending',
                   completed_at = v_now,
                   error_code = 'UPLOAD_RESERVATION_EXPIRED',
                   error_message = '파일 업로드 시간이 만료되었습니다. 다시 시도해 주세요.'
             WHERE analysis_run_pk = v_run.analysis_run_pk
            RETURNING * INTO v_run;
            RETURN QUERY SELECT v_run.analysis_run_pk, v_run.status,
                                v_run.analysis_case_pk, v_run.error_code,
                                v_run.error_message, v_run.created_at,
                                v_run.updated_at, 'expired_capacity',
                                jsonb_build_array(jsonb_build_object(
                                    'analysis_run_pk', v_run.analysis_run_pk,
                                    'source_bucket', v_dispatch.source_bucket,
                                    'source_object_key', v_dispatch.source_object_key
                                ));
            RETURN;
        END;
    END IF;

    INSERT INTO workspace.source_artifact (
        analysis_run_pk, artifact_type, artifact_logical_id, storage_bucket,
        storage_object_key, content_sha256, mime_type, size_bytes
    ) VALUES (
        v_run.analysis_run_pk, 'source', p_original_filename, p_source_bucket,
        p_source_object_key, p_source_content_sha256, p_source_mime_type,
        p_source_size_bytes
    );

    UPDATE workspace.analysis_run
       SET status = 'queued',
           expires_at = v_now + make_interval(secs => p_queued_ttl_seconds),
           completed_at = NULL,
           error_code = NULL,
           error_message = NULL
     WHERE analysis_run_pk = v_run.analysis_run_pk
    RETURNING * INTO v_run;

    RETURN QUERY SELECT v_run.analysis_run_pk, v_run.status,
                        v_run.analysis_case_pk, v_run.error_code,
                        v_run.error_message, v_run.created_at,
                        v_run.updated_at,
                        'queued',
                        '[]'::jsonb;
END;
$$;

REVOKE ALL ON FUNCTION workspace.finalize_analysis_upload_v2(
    UUID, UUID, TEXT, TEXT, BIGINT, TEXT, TEXT, TEXT, TEXT, BIGINT, INTEGER
) FROM PUBLIC, anon, authenticated, service_role;

GRANT EXECUTE ON FUNCTION workspace.finalize_analysis_upload_v2(
    UUID, UUID, TEXT, TEXT, BIGINT, TEXT, TEXT, TEXT, TEXT, BIGINT, INTEGER
) TO service_role;

COMMIT;
