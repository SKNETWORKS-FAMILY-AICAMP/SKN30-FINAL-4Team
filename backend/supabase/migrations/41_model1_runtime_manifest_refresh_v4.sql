-- Register the immutable Model 1 runtime identity after the shared
-- backend/worker/ml_reference.py runtime changed.
--
-- The previously active configuration and its classification rows remain
-- untouched. This configuration stays inactive until the complete current
-- Existing corpus is rerun and the promotion gate verifies every result.
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
