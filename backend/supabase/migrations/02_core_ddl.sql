-- ============================================================================
-- Migration 02: Core DDL Tables
-- Date: 2026-08-31
-- Version: v0.3 (from handover)
--
-- This migration creates the core data model tables across all schemas:
-- 1. app.user_profile
-- 2. ops.* (processing_run, model_invocation, cleanup_event)
-- 3. kb.* (existing knowledge base with version lineage, facts, components, projections)
-- 4. workspace.* (request workspace artifacts and analysis)
-- 5. result.* (analysis results, similarity candidates, conversations, reports)
--
-- Important notes:
-- - Deliberately excludes pgvector embedding table (dimension not fixed yet)
-- - Request embedding is ephemeral by design
-- - Preserves auth.users ownership
-- - No browser direct writes allowed
-- ============================================================================

BEGIN;

-- ============================================================================
-- 0. App / User Profile
-- ============================================================================

CREATE TABLE IF NOT EXISTS app.user_profile (
    user_id         UUID PRIMARY KEY REFERENCES auth.users(id) ON DELETE RESTRICT,
    display_name    TEXT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============================================================================
-- 1. Ops: Processing & Auditing
-- ============================================================================

CREATE TABLE IF NOT EXISTS ops.processing_run (
    processing_run_pk       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_analysis_run_id  UUID NULL,
    parent_run_pk           UUID NULL
        REFERENCES ops.processing_run(processing_run_pk)
        ON DELETE SET NULL,
    run_type                TEXT NOT NULL,
    status                  TEXT NOT NULL
        CHECK (status IN ('queued','running','succeeded','failed','cancelled')),
    pipeline_version        TEXT NULL,
    component_name          TEXT NULL,
    component_version       TEXT NULL,
    schema_version          TEXT NULL,
    started_at              TIMESTAMPTZ NULL,
    finished_at             TIMESTAMPTZ NULL,
    run_metadata            JSONB NULL,
    error_code              TEXT NULL,
    error_message           TEXT NULL,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ops.model_invocation (
    model_invocation_pk UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    processing_run_pk   UUID NOT NULL
        REFERENCES ops.processing_run(processing_run_pk)
        ON DELETE CASCADE,
    model_role           TEXT NULL,
    model_id             TEXT NOT NULL,
    model_version        TEXT NULL,
    prompt_version       TEXT NULL,
    input_hash           TEXT NULL,
    output_hash          TEXT NULL,
    input_tokens         INTEGER NULL CHECK (input_tokens IS NULL OR input_tokens >= 0),
    output_tokens        INTEGER NULL CHECK (output_tokens IS NULL OR output_tokens >= 0),
    latency_ms           INTEGER NULL CHECK (latency_ms IS NULL OR latency_ms >= 0),
    status               TEXT NOT NULL
        CHECK (status IN ('queued','running','succeeded','failed','cancelled')),
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ops.cleanup_event (
    cleanup_event_pk       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_analysis_run_id UUID NULL,
    target_type            TEXT NOT NULL,
    target_identifier_hash TEXT NULL,
    requested_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at           TIMESTAMPTZ NULL,
    status                 TEXT NOT NULL
        CHECK (status IN ('requested','running','succeeded','failed')),
    error_code             TEXT NULL,
    error_message          TEXT NULL,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============================================================================
-- 2. Existing KB: Version Lineage
-- ============================================================================

CREATE TABLE IF NOT EXISTS kb.notice (
    notice_pk       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    notice_id       TEXT NOT NULL UNIQUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS kb.source_profile (
    source_profile_pk UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    notice_pk         UUID NOT NULL
        REFERENCES kb.notice(notice_pk)
        ON DELETE RESTRICT,
    source_profile_id TEXT NOT NULL UNIQUE,
    source_kind       TEXT NOT NULL
        CHECK (source_kind IN ('hwp','hwpx','pdf','markdown_fixture')),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS kb.source_version (
    source_version_pk UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_profile_pk UUID NOT NULL
        REFERENCES kb.source_profile(source_profile_pk)
        ON DELETE RESTRICT,
    source_sha256     TEXT NOT NULL
        CHECK (source_sha256 ~ '^[0-9A-Fa-f]{64}$'),
    source_location   TEXT NULL,
    source_url        TEXT NULL,
    notice_detail_url TEXT NULL,
    first_collected_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    is_current        BOOLEAN NOT NULL DEFAULT TRUE,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source_profile_pk, source_sha256)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_kb_source_version_one_current
ON kb.source_version(source_profile_pk)
WHERE is_current;

CREATE TABLE IF NOT EXISTS kb.artifact (
    artifact_pk        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_version_pk  UUID NOT NULL
        REFERENCES kb.source_version(source_version_pk)
        ON DELETE RESTRICT,
    processing_run_pk  UUID NULL
        REFERENCES ops.processing_run(processing_run_pk)
        ON DELETE SET NULL,
    artifact_type      TEXT NOT NULL
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
    UNIQUE (storage_bucket, storage_object_key),
    UNIQUE (artifact_pk, source_version_pk)
);

CREATE TABLE IF NOT EXISTS kb.artifact_lineage (
    parent_artifact_pk UUID NOT NULL
        REFERENCES kb.artifact(artifact_pk)
        ON DELETE CASCADE,
    child_artifact_pk  UUID NOT NULL
        REFERENCES kb.artifact(artifact_pk)
        ON DELETE CASCADE,
    relation_type      TEXT NOT NULL DEFAULT 'input_to',
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (parent_artifact_pk, child_artifact_pk, relation_type),
    CHECK (parent_artifact_pk <> child_artifact_pk)
);

CREATE TABLE IF NOT EXISTS kb.profile_version (
    profile_version_pk          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_version_pk           UUID NOT NULL
        REFERENCES kb.source_version(source_version_pk)
        ON DELETE RESTRICT,
    schema_version              TEXT NOT NULL,
    profile_sha256              TEXT NOT NULL
        CHECK (profile_sha256 ~ '^[0-9A-Fa-f]{64}$'),
    candidate_pack_artifact_pk  UUID NOT NULL,
    structured_artifact_pk      UUID NOT NULL,
    processing_run_pk           UUID NULL
        REFERENCES ops.processing_run(processing_run_pk)
        ON DELETE SET NULL,
    is_current                  BOOLEAN NOT NULL DEFAULT TRUE,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source_version_pk, profile_sha256),
    UNIQUE (structured_artifact_pk),
    FOREIGN KEY (candidate_pack_artifact_pk, source_version_pk)
        REFERENCES kb.artifact(artifact_pk, source_version_pk)
        ON DELETE RESTRICT,
    FOREIGN KEY (structured_artifact_pk, source_version_pk)
        REFERENCES kb.artifact(artifact_pk, source_version_pk)
        ON DELETE RESTRICT
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_kb_profile_version_one_current
ON kb.profile_version(source_version_pk)
WHERE is_current;

-- ============================================================================
-- 3. Existing KB: Support Component & Raw Fact
-- ============================================================================

CREATE TABLE IF NOT EXISTS kb.support_component (
    component_pk          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    profile_version_pk    UUID NOT NULL
        REFERENCES kb.profile_version(profile_version_pk)
        ON DELETE CASCADE,
    support_component_id  TEXT NOT NULL,
    component_kind        TEXT NOT NULL,
    name_raw              TEXT NULL,
    name_status           TEXT NOT NULL,
    name_source_block_id  TEXT NULL,
    source_block_ids      TEXT[] NOT NULL DEFAULT '{}',
    table_block_ids       TEXT[] NOT NULL DEFAULT '{}',
    ordinal               INTEGER NOT NULL CHECK (ordinal >= 0),
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (profile_version_pk, support_component_id),
    UNIQUE (component_pk, profile_version_pk)
);

CREATE TABLE IF NOT EXISTS kb.fact_occurrence (
    fact_pk               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    profile_version_pk    UUID NOT NULL
        REFERENCES kb.profile_version(profile_version_pk)
        ON DELETE CASCADE,
    fact_id               TEXT NOT NULL,
    fact_scope            TEXT NOT NULL
        CHECK (fact_scope IN ('comparison','existing_specific')),
    field_name            TEXT NOT NULL,
    value_raw             TEXT NOT NULL,
    status                TEXT NOT NULL
        CHECK (status IN ('identified','partial')),
    scope                 TEXT NOT NULL
        CHECK (scope IN ('notice','component')),
    support_component_pk  UUID NULL
        REFERENCES kb.support_component(component_pk)
        ON DELETE RESTRICT,
    subject_role          TEXT NULL,
    semantic_role         TEXT NULL,
    source_block_id       TEXT NOT NULL,
    start_char            INTEGER NOT NULL CHECK (start_char >= 0),
    end_char              INTEGER NOT NULL CHECK (end_char > start_char),
    text_basis            TEXT NOT NULL
        CHECK (text_basis = 'common_ir_v1_candidate_pack'),
    ordinal               INTEGER NOT NULL CHECK (ordinal >= 0),
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (profile_version_pk, fact_id),
    CHECK (
        (fact_scope = 'comparison' AND field_name IN (
            'purpose_goal','applicant_eligibility','support_target',
            'eligibility_conditions','beneficiary','exclusions',
            'participation_requirements','program_period','support_period',
            'support_activities','support_methods','support_items',
            'support_content','support_scale','total_budget','cost_sharing'
        ))
        OR
        (fact_scope = 'existing_specific' AND field_name IN (
            'payment_terms','duplicate_support_conditions','applicable_entity',
            'delivery_roles'
        ))
    ),
    CHECK (
        (scope = 'notice' AND support_component_pk IS NULL)
        OR
        (scope = 'component' AND support_component_pk IS NOT NULL)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_kb_fact_comparison_exact_span
ON kb.fact_occurrence(profile_version_pk, source_block_id, start_char, end_char)
WHERE fact_scope = 'comparison';

CREATE TABLE IF NOT EXISTS kb.fact_evidence (
    evidence_pk              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    fact_pk                  UUID NOT NULL
        REFERENCES kb.fact_occurrence(fact_pk)
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

CREATE TABLE IF NOT EXISTS kb.fact_context (
    context_pk               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    fact_pk                  UUID NOT NULL
        REFERENCES kb.fact_occurrence(fact_pk)
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

CREATE TABLE IF NOT EXISTS kb.fact_relation (
    fact_relation_pk UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_fact_pk   UUID NOT NULL
        REFERENCES kb.fact_occurrence(fact_pk)
        ON DELETE CASCADE,
    target_fact_pk   UUID NOT NULL
        REFERENCES kb.fact_occurrence(fact_pk)
        ON DELETE CASCADE,
    relation_type    TEXT NOT NULL
        CHECK (relation_type IN ('modifies','recipient','basis')),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source_fact_pk, target_fact_pk, relation_type),
    CHECK (source_fact_pk <> target_fact_pk)
);

CREATE TABLE IF NOT EXISTS kb.fact_component_link (
    fact_component_link_pk UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    fact_pk                UUID NOT NULL
        REFERENCES kb.fact_occurrence(fact_pk)
        ON DELETE CASCADE,
    component_pk           UUID NOT NULL
        REFERENCES kb.support_component(component_pk)
        ON DELETE CASCADE,
    link_type              TEXT NOT NULL DEFAULT 'applicability'
        CHECK (link_type = 'applicability'),
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (fact_pk, component_pk, link_type)
);

-- ============================================================================
-- 4. Existing KB: Delivery Role Subtype & Organizations
-- ============================================================================

CREATE TABLE IF NOT EXISTS kb.delivery_role (
    fact_pk                 UUID PRIMARY KEY
        REFERENCES kb.fact_occurrence(fact_pk)
        ON DELETE CASCADE,
    canonical_role          TEXT NULL
        CHECK (
            canonical_role IS NULL
            OR canonical_role IN (
                'announcing_agency','lead_agency','operating_agency',
                'dedicated_agency','participating_partner','demand_partner',
                'cooperating_organization'
            )
        ),
    relation_container_type TEXT NOT NULL,
    relation_container_data JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS kb.delivery_role_organization (
    organization_occurrence_pk UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    fact_pk                    UUID NOT NULL
        REFERENCES kb.delivery_role(fact_pk)
        ON DELETE CASCADE,
    value_raw                  TEXT NOT NULL,
    source_block_id            TEXT NOT NULL,
    start_char                 INTEGER NOT NULL CHECK (start_char >= 0),
    end_char                   INTEGER NOT NULL CHECK (end_char > start_char),
    text_basis                 TEXT NOT NULL
        CHECK (text_basis = 'common_ir_v1_candidate_pack'),
    ordinal                    INTEGER NOT NULL CHECK (ordinal >= 0),
    created_at                 TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (fact_pk, source_block_id, start_char, end_char)
);

-- ============================================================================
-- 5. Existing KB: Target Constraint
-- ============================================================================

CREATE TABLE IF NOT EXISTS kb.target_constraint (
    target_constraint_pk UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    profile_version_pk   UUID NOT NULL
        REFERENCES kb.profile_version(profile_version_pk)
        ON DELETE CASCADE,
    status               TEXT NOT NULL,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (profile_version_pk)
);

CREATE TABLE IF NOT EXISTS kb.target_constraint_source (
    target_constraint_source_pk UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    target_constraint_pk        UUID NOT NULL
        REFERENCES kb.target_constraint(target_constraint_pk)
        ON DELETE CASCADE,
    fact_pk                     UUID NOT NULL
        REFERENCES kb.fact_occurrence(fact_pk)
        ON DELETE CASCADE,
    source_role                 TEXT NOT NULL
        CHECK (source_role IN ('positive','exclusion')),
    ordinal                     INTEGER NOT NULL CHECK (ordinal >= 0),
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (target_constraint_pk, fact_pk)
);

CREATE TABLE IF NOT EXISTS kb.target_constraint_dimension (
    dimension_pk          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    target_constraint_pk  UUID NOT NULL
        REFERENCES kb.target_constraint(target_constraint_pk)
        ON DELETE CASCADE,
    dimension_key         TEXT NOT NULL,
    value_kind            TEXT NOT NULL
        CHECK (value_kind IN ('categorical','numeric')),
    value_text            TEXT NULL,
    comparator            TEXT NULL
        CHECK (
            comparator IS NULL
            OR comparator IN ('eq','lt','lte','gt','gte','range','approx')
        ),
    lower_value           NUMERIC NULL,
    upper_value           NUMERIC NULL,
    unit                  TEXT NULL,
    ordinal               INTEGER NOT NULL CHECK (ordinal >= 0),
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (
        (
            value_kind = 'categorical'
            AND value_text IS NOT NULL
            AND comparator IS NULL
            AND lower_value IS NULL
            AND upper_value IS NULL
        )
        OR
        (
            value_kind = 'numeric'
            AND value_text IS NULL
            AND comparator IS NOT NULL
            AND (
                (comparator IN ('eq','approx') AND lower_value IS NOT NULL AND upper_value IS NOT NULL AND lower_value = upper_value)
                OR (comparator IN ('lt','lte') AND lower_value IS NULL AND upper_value IS NOT NULL)
                OR (comparator IN ('gt','gte') AND lower_value IS NOT NULL AND upper_value IS NULL)
                OR (comparator = 'range' AND lower_value IS NOT NULL AND upper_value IS NOT NULL AND lower_value <= upper_value)
            )
        )
    )
);

-- ============================================================================
-- 6. Existing KB: Support Facets (Activity, Method, Item)
-- ============================================================================

CREATE TABLE IF NOT EXISTS kb.support_facet (
    support_facet_pk  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    profile_version_pk UUID NOT NULL
        REFERENCES kb.profile_version(profile_version_pk)
        ON DELETE CASCADE,
    status            TEXT NOT NULL,
    ordinal           INTEGER NOT NULL CHECK (ordinal >= 0),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (profile_version_pk, ordinal)
);

CREATE TABLE IF NOT EXISTS kb.support_facet_source (
    support_facet_source_pk UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    support_facet_pk        UUID NOT NULL
        REFERENCES kb.support_facet(support_facet_pk)
        ON DELETE CASCADE,
    fact_pk                 UUID NOT NULL
        REFERENCES kb.fact_occurrence(fact_pk)
        ON DELETE CASCADE,
    ordinal                 INTEGER NOT NULL CHECK (ordinal >= 0),
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (support_facet_pk, fact_pk)
);

CREATE TABLE IF NOT EXISTS kb.support_facet_value (
    support_facet_value_pk UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    support_facet_pk       UUID NOT NULL
        REFERENCES kb.support_facet(support_facet_pk)
        ON DELETE CASCADE,
    facet_type             TEXT NOT NULL
        CHECK (facet_type IN ('activity','method','item')),
    value_text             TEXT NOT NULL,
    ordinal                INTEGER NOT NULL CHECK (ordinal >= 0),
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (support_facet_pk, facet_type, value_text)
);

-- ============================================================================
-- 7. Existing KB: Support Scale Projections
-- ============================================================================

CREATE TABLE IF NOT EXISTS kb.support_scale_projection (
    support_scale_projection_pk UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    profile_version_pk          UUID NOT NULL
        REFERENCES kb.profile_version(profile_version_pk)
        ON DELETE CASCADE,
    status                      TEXT NOT NULL,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (profile_version_pk)
);

CREATE TABLE IF NOT EXISTS kb.support_scale_measure (
    measure_pk                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    support_scale_projection_pk  UUID NOT NULL
        REFERENCES kb.support_scale_projection(support_scale_projection_pk)
        ON DELETE CASCADE,
    source_fact_pk               UUID NOT NULL
        REFERENCES kb.fact_occurrence(fact_pk)
        ON DELETE CASCADE,
    source_numeric_candidate_id  TEXT NOT NULL,
    measure_type                 TEXT NOT NULL
        CHECK (measure_type IN ('count','amount','rate')),
    measure_role                 TEXT NOT NULL
        CHECK (measure_role IN (
            'selection_capacity','support_amount','support_limit','support_rate'
        )),
    lower_value                  NUMERIC NULL,
    upper_value                  NUMERIC NULL,
    unit                         TEXT NOT NULL,
    comparator                   TEXT NOT NULL
        CHECK (comparator IN ('eq','lt','lte','gt','gte','range','approx')),
    applies_per                  TEXT NULL,
    calculation_basis            TEXT NULL,
    frequency                    TEXT NULL,
    aggregation_scope            TEXT NULL
        CHECK (aggregation_scope IS NULL OR aggregation_scope IN ('TOTAL','PER_UNIT')),
    ordinal                      INTEGER NOT NULL CHECK (ordinal >= 0),
    created_at                   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (
        (measure_role = 'selection_capacity' AND measure_type = 'count')
        OR (measure_role IN ('support_amount','support_limit') AND measure_type = 'amount')
        OR (measure_role = 'support_rate' AND measure_type = 'rate')
    ),
    CHECK (
        (comparator IN ('eq','approx') AND lower_value IS NOT NULL AND upper_value IS NOT NULL AND lower_value = upper_value)
        OR (comparator IN ('lt','lte') AND lower_value IS NULL AND upper_value IS NOT NULL)
        OR (comparator IN ('gt','gte') AND lower_value IS NOT NULL AND upper_value IS NULL)
        OR (comparator = 'range' AND lower_value IS NOT NULL AND upper_value IS NOT NULL AND lower_value <= upper_value)
    ),
    CHECK (measure_type <> 'rate' OR unit = 'BPS'),
    CHECK (
        aggregation_scope IS NULL
        OR (aggregation_scope = 'PER_UNIT' AND applies_per IS NOT NULL)
        OR (aggregation_scope = 'TOTAL' AND applies_per IS NULL)
    ),
    UNIQUE (
        support_scale_projection_pk,
        source_fact_pk,
        source_numeric_candidate_id,
        measure_type,
        measure_role
    )
);

COMMIT;
