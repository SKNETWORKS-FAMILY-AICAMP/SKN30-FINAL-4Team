-- ============================================================================
-- Migration 04: Workspace Components & Facts
-- Date: 2026-08-31
-- Version: v0.3
-- ============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS workspace.support_component (
    component_pk          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    request_profile_pk    UUID NOT NULL
        REFERENCES workspace.request_profile(request_profile_pk)
        ON DELETE CASCADE,
    support_component_id  TEXT NOT NULL,
    component_kind        TEXT NOT NULL,
    name_raw              TEXT NULL,
    name_source_block_id  TEXT NULL,
    name_start_char       INTEGER NULL CHECK (name_start_char IS NULL OR name_start_char >= 0),
    name_end_char         INTEGER NULL,
    name_text_basis       TEXT NULL,
    evidence              JSONB NOT NULL DEFAULT '[]'::jsonb,
    ordinal               INTEGER NOT NULL CHECK (ordinal >= 0),
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (request_profile_pk, support_component_id)
);

CREATE TABLE IF NOT EXISTS workspace.program_node (
    program_node_pk         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    request_profile_pk      UUID NOT NULL
        REFERENCES workspace.request_profile(request_profile_pk)
        ON DELETE CASCADE,
    program_node_id         TEXT NOT NULL,
    level                   TEXT NOT NULL
        CHECK (level IN ('detail_program','sub_program','sub_sub_program')),
    name_raw                TEXT NOT NULL,
    parent_program_node_pk  UUID NULL
        REFERENCES workspace.program_node(program_node_pk)
        ON DELETE SET NULL
        DEFERRABLE INITIALLY DEFERRED,
    source_block_id         TEXT NOT NULL,
    start_char              INTEGER NOT NULL CHECK (start_char >= 0),
    end_char                INTEGER NOT NULL CHECK (end_char > start_char),
    text_basis              TEXT NOT NULL
        CHECK (text_basis = 'common_ir_v1_candidate_pack'),
    evidence                JSONB NOT NULL DEFAULT '[]'::jsonb,
    ordinal                 INTEGER NOT NULL CHECK (ordinal >= 0),
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (request_profile_pk, program_node_id)
);

CREATE TABLE IF NOT EXISTS workspace.fact_occurrence (
    fact_pk               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    request_profile_pk    UUID NOT NULL
        REFERENCES workspace.request_profile(request_profile_pk)
        ON DELETE CASCADE,
    fact_id               TEXT NOT NULL,
    fact_scope            TEXT NOT NULL
        CHECK (fact_scope IN ('comparison','request_context','request_delivery')),
    field_name            TEXT NOT NULL,
    value_raw             TEXT NOT NULL,
    status                TEXT NOT NULL
        CHECK (status IN ('identified','partial')),
    program_node_pk       UUID NULL
        REFERENCES workspace.program_node(program_node_pk)
        ON DELETE SET NULL,
    support_component_pk  UUID NULL
        REFERENCES workspace.support_component(component_pk)
        ON DELETE SET NULL,
    source_block_id       TEXT NOT NULL,
    start_char            INTEGER NOT NULL CHECK (start_char >= 0),
    end_char              INTEGER NOT NULL CHECK (end_char > start_char),
    text_basis            TEXT NOT NULL
        CHECK (text_basis = 'common_ir_v1_candidate_pack'),
    ordinal               INTEGER NOT NULL CHECK (ordinal >= 0),
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (request_profile_pk, fact_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_workspace_fact_comparison_exact_span
ON workspace.fact_occurrence(request_profile_pk, source_block_id, start_char, end_char)
WHERE fact_scope = 'comparison';

CREATE TABLE IF NOT EXISTS workspace.fact_evidence (
    evidence_pk              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    fact_pk                  UUID NOT NULL
        REFERENCES workspace.fact_occurrence(fact_pk)
        ON DELETE CASCADE,
    source_block_id          TEXT NOT NULL,
    section_id               TEXT NULL,
    common_ir_document_id    TEXT NOT NULL,
    common_ir_block_id       TEXT NOT NULL,
    common_ir_cell_id        TEXT NULL,
    common_ir_occurrence_ids TEXT[] NULL,
    ordinal                  INTEGER NOT NULL CHECK (ordinal >= 0),
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS workspace.fact_context (
    context_pk               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    fact_pk                  UUID NOT NULL
        REFERENCES workspace.fact_occurrence(fact_pk)
        ON DELETE CASCADE,
    source_block_id          TEXT NOT NULL,
    section_id               TEXT NULL,
    context_text             TEXT NULL,
    common_ir_document_id    TEXT NULL,
    common_ir_block_id       TEXT NULL,
    common_ir_cell_id        TEXT NULL,
    common_ir_occurrence_ids TEXT[] NULL,
    ordinal                  INTEGER NOT NULL CHECK (ordinal >= 0),
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMIT;
