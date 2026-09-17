-- Migration 42: preserve the immutable intermediate Model 1 runtime identity
-- published on backend-rebuild before the develop integration.
--
-- The previously active configuration and its classification rows remain
-- untouched. The integrated runtime moved Model 2/3 retry out of the shared
-- reference module and therefore matches the v3 identity registered by
-- migrations 31/32/38 again. This historical v4 row stays inactive and is not
-- a backfill or promotion target for the integrated checkout.
BEGIN;

INSERT INTO retrieval.classification_configuration (
    model_id,
    artifact_sha256,
    runtime_manifest_sha256,
    input_assembly_version,
    producer_version,
    is_active
)
VALUES (
    'model_1_support_type',
    '8fa1522ced99f69966aed797c94cbd841f9ee9ce7d94c84dbc55adbf28613779',
    '85aee02364390b97385987ed6acb64406ca83651128585cb0a53cefb28dc9597',
    'existing-profile-model1-input-v1',
    'pre-review-existing-model1-runtime-v4',
    FALSE
)
ON CONFLICT DO NOTHING;

COMMIT;
