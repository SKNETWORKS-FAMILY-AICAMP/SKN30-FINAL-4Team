-- Register the current immutable Model 1 runtime identity.
--
-- The Model 1 manifest covers the serving archive as well as the checked-out
-- worker adapter/reference code.  Those backend bytes changed after the
-- original v2 registration.  A configuration identity is deliberately
-- immutable and existing classifications retain their foreign key to the
-- configuration that produced them, so this migration never updates or
-- deletes the old configuration or any classification row.
--
-- Fresh installs receive this identity from the replay-safe m31/m32 seed.
-- Databases that applied m31/m32 before this correction receive the same
-- inactive configuration here and must backfill it before promotion.  An
-- already active historical configuration remains active until the normal
-- complete-corpus promotion gate atomically switches to this one.

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
    '2903d0e90e71cd121af3185eeab3fefe3e1407175d14476e8d60f611b6861a60',
    'existing-profile-model1-input-v1',
    'pre-review-existing-model1-runtime-v3',
    FALSE
)
ON CONFLICT DO NOTHING;

COMMENT ON TABLE retrieval.classification_configuration IS
    'Immutable Model 1 weight/runtime/input/producer identity; runtime refreshes register a new inactive configuration and require a complete successful current Existing corpus before promotion.';

COMMIT;
