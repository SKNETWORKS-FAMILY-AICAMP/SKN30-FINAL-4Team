-- ============================================================================
-- Migration 14: Browser Upload Reservation Hardening
-- Date: 2026-09-08
--
-- A browser can upload only the exact Storage object reserved by its own Edge
-- Function-created analysis run. HWP/HWPX MIME values vary by browser, so
-- server-side extension/content validation remains the worker's responsibility.
-- ============================================================================

BEGIN;

UPDATE storage.buckets
   SET public = FALSE,
       file_size_limit = 52428800
 WHERE id = 'request-temp';

CREATE OR REPLACE FUNCTION workspace.can_manage_own_reserved_source(
    p_object_key TEXT,
    p_allowed_statuses TEXT[]
)
RETURNS BOOLEAN
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, workspace
AS $$
    SELECT EXISTS (
        SELECT 1
        FROM workspace.analysis_run ar
        JOIN workspace.analysis_run_dispatch dispatch
          ON dispatch.analysis_run_pk = ar.analysis_run_pk
        WHERE ar.user_id = auth.uid()
          AND dispatch.source_bucket = 'request-temp'
          AND dispatch.source_object_key = p_object_key
          AND ar.status = ANY (p_allowed_statuses)
    );
$$;

REVOKE ALL ON FUNCTION workspace.can_manage_own_reserved_source(TEXT, TEXT[]) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION workspace.can_manage_own_reserved_source(TEXT, TEXT[]) TO authenticated;

DROP POLICY IF EXISTS request_temp_insert_own_prefix ON storage.objects;
DROP POLICY IF EXISTS request_temp_insert_reserved_source ON storage.objects;
CREATE POLICY request_temp_insert_reserved_source
ON storage.objects
FOR INSERT
TO authenticated
WITH CHECK (
    bucket_id = 'request-temp'
    AND (storage.foldername(name))[1] = 'request-source'
    AND (storage.foldername(name))[2] = (SELECT auth.uid()::text)
    AND workspace.can_manage_own_reserved_source(name, ARRAY['uploading'])
);

DROP POLICY IF EXISTS request_temp_delete_own_prefix ON storage.objects;
DROP POLICY IF EXISTS request_temp_delete_unprocessed_source ON storage.objects;
CREATE POLICY request_temp_delete_unprocessed_source
ON storage.objects
FOR DELETE
TO authenticated
USING (
    bucket_id = 'request-temp'
    AND workspace.can_manage_own_reserved_source(
        name, ARRAY['uploading','failed','cancelled']
    )
);

COMMIT;
