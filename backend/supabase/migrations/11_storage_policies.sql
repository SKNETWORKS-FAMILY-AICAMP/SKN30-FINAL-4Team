-- ============================================================================
-- Migration 11: Private Storage Buckets and Browser Upload Policy
-- Date: 2026-09-08
--
-- This migration creates private buckets.  Its former own-prefix browser
-- policies are deliberately removed on every replay: migration 14 alone owns
-- the reservation-bound policy, so replaying 11 can never reopen that bypass.
-- ============================================================================

BEGIN;

-- Official Storage tables are owned by this role, rather than the project
-- postgres role selected by apply_migrations.sh. SET LOCAL resets at COMMIT.
SET LOCAL ROLE supabase_storage_admin;

INSERT INTO storage.buckets (id, name, public)
VALUES
    ('existing-kb', 'existing-kb', FALSE),
    ('request-temp', 'request-temp', FALSE),
    ('analysis-reports', 'analysis-reports', FALSE)
ON CONFLICT (id) DO UPDATE SET public = FALSE;

-- Legacy policies accepted any object below a user's prefix and bypassed the
-- exact source reservation required since migration 14.  Do not recreate
-- them here: m14 drops these names defensively and creates the hardened pair.
DROP POLICY IF EXISTS request_temp_insert_own_prefix ON storage.objects;
DROP POLICY IF EXISTS request_temp_delete_own_prefix ON storage.objects;

COMMIT;
