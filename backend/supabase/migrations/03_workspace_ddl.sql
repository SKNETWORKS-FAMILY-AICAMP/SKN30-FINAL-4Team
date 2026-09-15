-- ============================================================================
-- Migration 03: Workspace DDL Tables
-- Date: 2026-08-31
-- Version: v0.3
--
-- This migration creates the request workspace tables for analysis runs.
-- Workspace is retained during analysis_session, then deleted on session close.
-- ============================================================================

BEGIN;

-- ============================================================================
-- 8. Request Workspace Root / Artifacts / Profile
-- ============================================================================

CREATE TABLE IF NOT EXISTS workspace.analysis_run (
    analysis_run_pk UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID NOT NULL
        REFERENCES auth.users(id)
        ON DELETE RESTRICT,
    status          TEXT NOT NULL
        CHECK (status IN (
            'queued','running','succeeded','failed','cancelled','cleanup_pending'
        )),
    started_at      TIMESTAMPTZ NULL,
    completed_at    TIMESTAMPTZ NULL,
    expires_at      TIMESTAMPTZ NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS workspace.source_artifact (
    artifact_pk         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    analysis_run_pk     UUID NOT NULL
        REFERENCES workspace.analysis_run(analysis_run_pk)
        ON DELETE CASCADE,
    processing_run_pk   UUID NULL
        REFERENCES ops.processing_run(processing_run_pk)
        ON DELETE SET NULL,
    artifact_type       TEXT NOT NULL
        CHECK (artifact_type IN (
            'source','parser_raw','format_ir','common_ir',
            'candidate_pack','structured_profile'
        )),
    artifact_logical_id TEXT NULL,
    storage_bucket      TEXT NOT NULL,
    storage_object_key  TEXT NOT NULL,
    content_sha256      TEXT NOT NULL
        CHECK (content_sha256 ~ '^[0-9A-Fa-f]{64}$'),
    mime_type           TEXT NULL,
    size_bytes          BIGINT NULL CHECK (size_bytes IS NULL OR size_bytes >= 0),
    schema_version      TEXT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (storage_bucket, storage_object_key)
);

CREATE TABLE IF NOT EXISTS workspace.artifact_lineage (
    parent_artifact_pk UUID NOT NULL
        REFERENCES workspace.source_artifact(artifact_pk)
        ON DELETE CASCADE,
    child_artifact_pk  UUID NOT NULL
        REFERENCES workspace.source_artifact(artifact_pk)
        ON DELETE CASCADE,
    relation_type      TEXT NOT NULL DEFAULT 'input_to',
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (parent_artifact_pk, child_artifact_pk, relation_type),
    CHECK (parent_artifact_pk <> child_artifact_pk)
);

CREATE TABLE IF NOT EXISTS workspace.request_profile (
    request_profile_pk               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    analysis_run_pk                  UUID NOT NULL
        REFERENCES workspace.analysis_run(analysis_run_pk)
        ON DELETE CASCADE,
    profile_id                       TEXT NOT NULL,
    schema_version                   TEXT NOT NULL,
    program_name                     TEXT NULL,
    requesting_organization          TEXT NULL,
    source_document_id               TEXT NULL,
    common_ir_document_id            TEXT NULL,
    candidate_pack_id                TEXT NULL,
    candidate_pack_generator         TEXT NULL,
    candidate_pack_generator_version TEXT NULL,
    candidate_pack_artifact_pk       UUID NULL
        REFERENCES workspace.source_artifact(artifact_pk)
        ON DELETE SET NULL,
    structured_artifact_pk           UUID NULL
        REFERENCES workspace.source_artifact(artifact_pk)
        ON DELETE SET NULL,
    processing_run_pk                UUID NULL
        REFERENCES ops.processing_run(processing_run_pk)
        ON DELETE SET NULL,
    created_at                       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (analysis_run_pk, profile_id)
);

COMMIT;
