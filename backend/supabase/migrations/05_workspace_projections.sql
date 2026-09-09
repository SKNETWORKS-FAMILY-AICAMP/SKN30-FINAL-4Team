-- ============================================================================
-- Migration 05: Workspace Projections & Delivery
-- Date: 2026-08-31
-- Version: v0.3
-- ============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS workspace.target_constraint (
    target_constraint_pk UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    request_profile_pk   UUID NOT NULL
        REFERENCES workspace.request_profile(request_profile_pk)
        ON DELETE CASCADE,
    status               TEXT NOT NULL,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (request_profile_pk)
);

CREATE TABLE IF NOT EXISTS workspace.target_constraint_source (
    target_constraint_source_pk UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    target_constraint_pk        UUID NOT NULL
        REFERENCES workspace.target_constraint(target_constraint_pk)
        ON DELETE CASCADE,
    fact_pk                     UUID NOT NULL
        REFERENCES workspace.fact_occurrence(fact_pk)
        ON DELETE CASCADE,
    source_role                 TEXT NOT NULL
        CHECK (source_role IN ('positive','exclusion')),
    ordinal                     INTEGER NOT NULL CHECK (ordinal >= 0),
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (target_constraint_pk, fact_pk)
);

CREATE TABLE IF NOT EXISTS workspace.target_constraint_dimension (
    dimension_pk          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    target_constraint_pk  UUID NOT NULL
        REFERENCES workspace.target_constraint(target_constraint_pk)
        ON DELETE CASCADE,
    dimension_key         TEXT NOT NULL,
    value_kind            TEXT NOT NULL
        CHECK (value_kind IN ('categorical','numeric')),
    value_text            TEXT NULL,
    comparator            TEXT NULL,
    lower_value           NUMERIC NULL,
    upper_value           NUMERIC NULL,
    unit                  TEXT NULL,
    ordinal               INTEGER NOT NULL CHECK (ordinal >= 0),
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS workspace.support_facet (
    support_facet_pk  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    request_profile_pk UUID NOT NULL
        REFERENCES workspace.request_profile(request_profile_pk)
        ON DELETE CASCADE,
    status            TEXT NOT NULL,
    ordinal           INTEGER NOT NULL CHECK (ordinal >= 0),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (request_profile_pk, ordinal)
);

CREATE TABLE IF NOT EXISTS workspace.support_facet_source (
    support_facet_source_pk UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    support_facet_pk        UUID NOT NULL
        REFERENCES workspace.support_facet(support_facet_pk)
        ON DELETE CASCADE,
    fact_pk                 UUID NOT NULL
        REFERENCES workspace.fact_occurrence(fact_pk)
        ON DELETE CASCADE,
    ordinal                 INTEGER NOT NULL CHECK (ordinal >= 0),
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (support_facet_pk, fact_pk)
);

CREATE TABLE IF NOT EXISTS workspace.support_facet_value (
    support_facet_value_pk UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    support_facet_pk       UUID NOT NULL
        REFERENCES workspace.support_facet(support_facet_pk)
        ON DELETE CASCADE,
    facet_type             TEXT NOT NULL
        CHECK (facet_type IN ('activity','method','item')),
    value_text             TEXT NOT NULL,
    ordinal                INTEGER NOT NULL CHECK (ordinal >= 0),
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (support_facet_pk, facet_type, value_text)
);

CREATE TABLE IF NOT EXISTS workspace.support_scale_projection (
    support_scale_projection_pk UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    request_profile_pk          UUID NOT NULL
        REFERENCES workspace.request_profile(request_profile_pk)
        ON DELETE CASCADE,
    status                      TEXT NOT NULL,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (request_profile_pk)
);

CREATE TABLE IF NOT EXISTS workspace.support_scale_measure (
    measure_pk                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    support_scale_projection_pk  UUID NOT NULL
        REFERENCES workspace.support_scale_projection(support_scale_projection_pk)
        ON DELETE CASCADE,
    source_fact_pk               UUID NOT NULL
        REFERENCES workspace.fact_occurrence(fact_pk)
        ON DELETE CASCADE,
    source_numeric_candidate_id  TEXT NOT NULL,
    measure_type                 TEXT NOT NULL
        CHECK (measure_type IN ('count','amount','rate')),
    measure_role                 TEXT NOT NULL
        CHECK (measure_role IN ('selection_capacity','support_amount','support_limit','support_rate')),
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
    UNIQUE (support_scale_projection_pk, source_fact_pk, source_numeric_candidate_id, measure_type, measure_role)
);

CREATE TABLE IF NOT EXISTS workspace.request_type (
    request_type_pk                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    request_profile_pk              UUID NOT NULL UNIQUE
        REFERENCES workspace.request_profile(request_profile_pk)
        ON DELETE CASCADE,
    selected_code                   TEXT NULL
        CHECK (selected_code IS NULL OR selected_code IN ('detail_program_new','sub_program_new','sub_sub_program_new','program_content_change')),
    value_raw                       TEXT NULL,
    label_source_block_id           TEXT NULL,
    label_start_char                INTEGER NULL,
    label_end_char                  INTEGER NULL,
    label_text_basis                TEXT NULL,
    glyph_raw                       TEXT NULL,
    glyph_source_block_id           TEXT NULL,
    glyph_start_char                INTEGER NULL,
    glyph_end_char                  INTEGER NULL,
    glyph_text_basis                TEXT NULL,
    evidence                        JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at                      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS workspace.delivery_relation (
    delivery_relation_pk       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    request_profile_pk         UUID NOT NULL
        REFERENCES workspace.request_profile(request_profile_pk)
        ON DELETE CASCADE,
    delivery_relation_id       TEXT NOT NULL,
    actor_raw                  TEXT NOT NULL,
    canonical_actor_type       TEXT NULL,
    actor_source_block_id      TEXT NOT NULL,
    actor_start_char           INTEGER NOT NULL CHECK (actor_start_char >= 0),
    actor_end_char             INTEGER NOT NULL CHECK (actor_end_char > actor_start_char),
    actor_text_basis           TEXT NOT NULL
        CHECK (actor_text_basis = 'common_ir_v1_candidate_pack'),
    actor_evidence             JSONB NOT NULL DEFAULT '[]'::jsonb,
    role_raw                   TEXT NULL,
    canonical_role             TEXT NULL,
    role_source_block_id       TEXT NULL,
    role_start_char            INTEGER NULL,
    role_end_char              INTEGER NULL,
    role_text_basis            TEXT NULL,
    role_evidence              JSONB NOT NULL DEFAULT '[]'::jsonb,
    relation_container_type    TEXT NOT NULL,
    relation_container_data    JSONB NOT NULL DEFAULT '{}'::jsonb,
    ordinal                    INTEGER NOT NULL CHECK (ordinal >= 0),
    created_at                 TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (request_profile_pk, delivery_relation_id)
);

CREATE TABLE IF NOT EXISTS workspace.delivery_action (
    delivery_action_pk     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    delivery_relation_pk   UUID NOT NULL
        REFERENCES workspace.delivery_relation(delivery_relation_pk)
        ON DELETE CASCADE,
    action_raw             TEXT NOT NULL,
    canonical_action       TEXT NULL,
    source_block_id        TEXT NOT NULL,
    start_char             INTEGER NOT NULL CHECK (start_char >= 0),
    end_char               INTEGER NOT NULL CHECK (end_char > start_char),
    text_basis             TEXT NOT NULL
        CHECK (text_basis = 'common_ir_v1_candidate_pack'),
    evidence               JSONB NOT NULL DEFAULT '[]'::jsonb,
    ordinal                INTEGER NOT NULL CHECK (ordinal >= 0),
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (delivery_relation_pk, source_block_id, start_char, end_char)
);

CREATE TABLE IF NOT EXISTS workspace.delivery_method (
    fact_pk              UUID PRIMARY KEY
        REFERENCES workspace.fact_occurrence(fact_pk)
        ON DELETE CASCADE,
    canonical_method     TEXT NULL
        CHECK (canonical_method IS NULL OR canonical_method IN ('direct','subsidy','contribution','commissioned','other')),
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS workspace.field_state (
    field_state_pk    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    request_profile_pk UUID NOT NULL
        REFERENCES workspace.request_profile(request_profile_pk)
        ON DELETE CASCADE,
    field_name        TEXT NOT NULL,
    status            TEXT NOT NULL,
    reason_codes      TEXT[] NULL,
    ordinal           INTEGER NOT NULL CHECK (ordinal >= 0),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (request_profile_pk, field_name)
);

CREATE TABLE IF NOT EXISTS workspace.field_state_ref (
    field_state_ref_pk   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    field_state_pk       UUID NOT NULL
        REFERENCES workspace.field_state(field_state_pk)
        ON DELETE CASCADE,
    fact_pk              UUID NULL
        REFERENCES workspace.fact_occurrence(fact_pk)
        ON DELETE CASCADE,
    delivery_relation_pk UUID NULL
        REFERENCES workspace.delivery_relation(delivery_relation_pk)
        ON DELETE CASCADE,
    support_component_pk UUID NULL
        REFERENCES workspace.support_component(component_pk)
        ON DELETE CASCADE,
    ordinal              INTEGER NOT NULL CHECK (ordinal >= 0),
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMIT;
