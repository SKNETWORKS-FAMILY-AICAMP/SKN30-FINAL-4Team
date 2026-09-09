-- ============================================================================
-- Migration 06: Result DDL Tables
-- Date: 2026-08-31
-- Version: v0.3
--
-- Analysis results retained independently for 90 days after completion.
-- Includes similarity candidates, evidence snapshots, and conversation sessions.
-- ============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS result.analysis_case (
    analysis_case_pk       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_analysis_run_id UUID NOT NULL,
    user_id                UUID NOT NULL
        REFERENCES auth.users(id)
        ON DELETE RESTRICT,
    case_status            TEXT NOT NULL
        CHECK (case_status IN ('processing','ready','read_only','failed')),
    analysis_completed_at  TIMESTAMPTZ NULL,
    retention_expires_at   TIMESTAMPTZ NULL,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source_analysis_run_id)
);

CREATE TABLE IF NOT EXISTS result.axis_result (
    axis_result_pk       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    analysis_case_pk   UUID NOT NULL
        REFERENCES result.analysis_case(analysis_case_pk)
        ON DELETE CASCADE,
    axis_type            TEXT NOT NULL
        CHECK (axis_type IN ('CPL','FIT','BEN','DIF')),
    axis_code            TEXT NOT NULL,
    status               TEXT NOT NULL,
    summary_text         TEXT NULL,
    result_data          JSONB NULL,
    ordinal              INTEGER NOT NULL CHECK (ordinal >= 0),
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (analysis_case_pk, axis_type, axis_code)
);

CREATE TABLE IF NOT EXISTS result.sim_candidate (
    sim_candidate_pk             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    analysis_case_pk           UUID NOT NULL
        REFERENCES result.analysis_case(analysis_case_pk)
        ON DELETE CASCADE,
    existing_profile_version_pk  UUID NOT NULL
        REFERENCES kb.profile_version(profile_version_pk)
        ON DELETE RESTRICT,
    rank_no                      INTEGER NOT NULL CHECK (rank_no > 0),
    similarity_score             NUMERIC NULL,
    priority_score               NUMERIC NULL,
    status                       TEXT NOT NULL,
    purpose_result               JSONB NULL,
    target_result                JSONB NULL,
    support_result               JSONB NULL,
    delivery_result              JSONB NULL,
    created_at                   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (analysis_case_pk, rank_no),
    UNIQUE (analysis_case_pk, existing_profile_version_pk)
);

CREATE TABLE IF NOT EXISTS result.evidence_snapshot (
    evidence_snapshot_pk         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    analysis_case_pk           UUID NOT NULL
        REFERENCES result.analysis_case(analysis_case_pk)
        ON DELETE CASCADE,
    axis_type                    TEXT NULL,
    axis_result_pk               UUID NULL
        REFERENCES result.axis_result(axis_result_pk)
        ON DELETE SET NULL,
    sim_candidate_pk             UUID NULL
        REFERENCES result.sim_candidate(sim_candidate_pk)
        ON DELETE SET NULL,
    side                         TEXT NOT NULL
        CHECK (side IN ('REQUEST','EXISTING')),
    usage_scope                  TEXT NOT NULL DEFAULT 'RESULT'
        CHECK (usage_scope IN ('RESULT','CONVERSATION')),
    field_name                   TEXT NULL,
    raw_value                    TEXT NOT NULL,
    context_excerpt              TEXT NULL,
    source_sha256                TEXT NULL,
    candidate_pack_block_id      TEXT NULL,
    start_char                   INTEGER NULL,
    end_char                     INTEGER NULL,
    common_ir_document_id        TEXT NULL,
    common_ir_block_id           TEXT NULL,
    common_ir_cell_id            TEXT NULL,
    common_ir_occurrence_ids     TEXT[] NULL,
    existing_profile_version_pk  UUID NULL
        REFERENCES kb.profile_version(profile_version_pk)
        ON DELETE RESTRICT,
    existing_fact_pk             UUID NULL
        REFERENCES kb.fact_occurrence(fact_pk)
        ON DELETE RESTRICT,
    created_at                   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS result.analysis_session (
    analysis_session_pk UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    analysis_case_pk    UUID NOT NULL UNIQUE
        REFERENCES result.analysis_case(analysis_case_pk)
        ON DELETE CASCADE,
    status              TEXT NOT NULL
        CHECK (status IN ('active','expired','closed')),
    started_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_activity_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at          TIMESTAMPTZ NOT NULL,
    closed_at           TIMESTAMPTZ NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS result.conversation_message (
    message_pk           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    analysis_session_pk  UUID NOT NULL
        REFERENCES result.analysis_session(analysis_session_pk)
        ON DELETE CASCADE,
    role                 TEXT NOT NULL
        CHECK (role IN ('user','assistant')),
    sequence_no          INTEGER NOT NULL CHECK (sequence_no > 0),
    content              TEXT NOT NULL,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (analysis_session_pk, sequence_no)
);

CREATE TABLE IF NOT EXISTS result.conversation_reference (
    conversation_reference_pk UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    message_pk                 UUID NOT NULL
        REFERENCES result.conversation_message(message_pk)
        ON DELETE CASCADE,
    axis_result_pk             UUID NULL
        REFERENCES result.axis_result(axis_result_pk)
        ON DELETE CASCADE,
    sim_candidate_pk           UUID NULL
        REFERENCES result.sim_candidate(sim_candidate_pk)
        ON DELETE CASCADE,
    evidence_snapshot_pk       UUID NULL
        REFERENCES result.evidence_snapshot(evidence_snapshot_pk)
        ON DELETE CASCADE,
    existing_fact_pk           UUID NULL
        REFERENCES kb.fact_occurrence(fact_pk)
        ON DELETE RESTRICT,
    reference_role             TEXT NULL,
    created_at                 TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS result.report_artifact (
    report_artifact_pk  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    analysis_case_pk  UUID NOT NULL
        REFERENCES result.analysis_case(analysis_case_pk)
        ON DELETE CASCADE,
    report_type         TEXT NOT NULL,
    storage_bucket      TEXT NOT NULL,
    storage_object_key  TEXT NOT NULL,
    content_sha256      TEXT NOT NULL
        CHECK (content_sha256 ~ '^[0-9A-Fa-f]{64}$'),
    mime_type           TEXT NULL,
    size_bytes          BIGINT NULL CHECK (size_bytes IS NULL OR size_bytes >= 0),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at          TIMESTAMPTZ NOT NULL,
    UNIQUE (storage_bucket, storage_object_key)
);

COMMIT;
