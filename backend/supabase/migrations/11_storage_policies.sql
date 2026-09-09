-- ============================================================================
-- Migration 11: Private Storage Buckets and Browser Upload Policy
-- Date: 2026-09-08
--
-- Browser uploads are limited to request-source/<auth.uid()>/<run-id>/...
-- Edge Functions reserve and validate the run/object key before processing.
-- Existing KB and report buckets remain server-only/private.
-- ============================================================================

BEGIN;

INSERT INTO storage.buckets (id, name, public)
VALUES
    ('existing-kb', 'existing-kb', FALSE),
    ('request-temp', 'request-temp', FALSE),
    ('analysis-reports', 'analysis-reports', FALSE)
ON CONFLICT (id) DO UPDATE SET public = FALSE;

DROP POLICY IF EXISTS request_temp_insert_own_prefix ON storage.objects;
CREATE POLICY request_temp_insert_own_prefix
ON storage.objects
FOR INSERT
TO authenticated
WITH CHECK (
    bucket_id = 'request-temp'
    AND (storage.foldername(name))[1] = 'request-source'
    AND (storage.foldername(name))[2] = (SELECT auth.uid()::text)
);

DROP POLICY IF EXISTS request_temp_delete_own_prefix ON storage.objects;
CREATE POLICY request_temp_delete_own_prefix
ON storage.objects
FOR DELETE
TO authenticated
USING (
    bucket_id = 'request-temp'
    AND (storage.foldername(name))[1] = 'request-source'
    AND (storage.foldername(name))[2] = (SELECT auth.uid()::text)
);

COMMIT;
