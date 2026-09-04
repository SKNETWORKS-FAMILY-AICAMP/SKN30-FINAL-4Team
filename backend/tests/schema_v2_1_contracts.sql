\set ON_ERROR_STOP on

-- Run against a fresh database after backend/app/db/schema.sql:
-- psql -X -v ON_ERROR_STOP=1 -d <database> -f backend/tests/schema_v2_1_contracts.sql

BEGIN;
SET search_path TO sims, public;

CREATE FUNCTION pg_temp.assert_true(actual boolean, label text)
RETURNS void
LANGUAGE plpgsql
AS $$
BEGIN
    IF actual IS DISTINCT FROM true THEN
        RAISE EXCEPTION 'assertion failed: %', label;
    END IF;
END;
$$;

CREATE FUNCTION pg_temp.expect_error(
    command text,
    expected_state text,
    message_pattern text DEFAULT NULL
)
RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE
    actual_state text;
    actual_message text;
BEGIN
    BEGIN
        EXECUTE command;
    EXCEPTION WHEN OTHERS THEN
        GET STACKED DIAGNOSTICS
            actual_state = RETURNED_SQLSTATE,
            actual_message = MESSAGE_TEXT;

        IF actual_state <> expected_state THEN
            RAISE EXCEPTION 'expected SQLSTATE %, got %: %',
                expected_state, actual_state, actual_message;
        END IF;

        IF message_pattern IS NOT NULL
           AND actual_message NOT LIKE message_pattern THEN
            RAISE EXCEPTION 'unexpected message: %', actual_message;
        END IF;

        RETURN;
    END;

    RAISE EXCEPTION 'expected SQLSTATE %, but command succeeded', expected_state;
END;
$$;

-- Environment and catalog shape.
SELECT pg_temp.assert_true(
    current_setting('server_version_num')::integer >= 150000,
    'PostgreSQL 15 or newer'
);
SELECT pg_temp.assert_true(
    EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'citext'),
    'citext extension installed'
);
SELECT pg_temp.assert_true(
    EXISTS (
        SELECT 1
        FROM pg_extension
        WHERE extname = 'vector'
          AND (
              split_part(extversion, '.', 1)::integer > 0
              OR split_part(extversion, '.', 2)::integer >= 7
          )
    ),
    'pgvector 0.7 or newer'
);
SELECT pg_temp.assert_true(
    (SELECT count(*) = 37
       FROM pg_class c
       JOIN pg_namespace n ON n.oid = c.relnamespace
      WHERE n.nspname = 'sims' AND c.relkind = 'r'),
    '37 tables'
);
SELECT pg_temp.assert_true(
    (SELECT count(*) = 2
       FROM pg_class c
       JOIN pg_namespace n ON n.oid = c.relnamespace
      WHERE n.nspname = 'sims' AND c.relkind = 'v'),
    '2 views'
);
SELECT pg_temp.assert_true(
    (SELECT count(*) = 8
       FROM pg_proc p
       JOIN pg_namespace n ON n.oid = p.pronamespace
      WHERE n.nspname = 'sims'),
    '8 functions'
);
SELECT pg_temp.assert_true(
    (SELECT count(*) = 12
       FROM pg_trigger t
       JOIN pg_class c ON c.oid = t.tgrelid
       JOIN pg_namespace n ON n.oid = c.relnamespace
      WHERE n.nspname = 'sims' AND NOT t.tgisinternal),
    '12 triggers'
);
SELECT pg_temp.assert_true(
    (SELECT count(*) = 56
       FROM pg_class i
       JOIN pg_index x ON x.indexrelid = i.oid
       JOIN pg_class t ON t.oid = x.indrelid
       JOIN pg_namespace n ON n.oid = t.relnamespace
       LEFT JOIN pg_constraint c ON c.conindid = i.oid
      WHERE n.nspname = 'sims' AND c.oid IS NULL),
    '56 explicit indexes'
);
SELECT pg_temp.assert_true(
    (SELECT count(*) = 14
       FROM pg_description d
       JOIN pg_class c ON c.oid = d.objoid
       JOIN pg_namespace n ON n.oid = c.relnamespace
      WHERE n.nspname = 'sims'),
    '14 design comments'
);
SELECT pg_temp.assert_true(
    NOT EXISTS (
        SELECT 1
        FROM pg_constraint c
        JOIN pg_namespace n ON n.oid = c.connamespace
        WHERE n.nspname = 'sims' AND NOT c.convalidated
    ),
    'all constraints validated'
);

-- Confirmed source seed.
SELECT pg_temp.assert_true(
    (SELECT count(*) = 3 FROM data_source),
    '3 data sources'
);
SELECT pg_temp.assert_true(
    EXISTS (
        SELECT 1 FROM data_source
        WHERE source_code = 'BIZINFO_OPEN_API'
          AND source_type = 'API'
          AND is_search_source
    ),
    'Bizinfo Open API is the search source'
);
SELECT pg_temp.assert_true(
    ARRAY(
        SELECT source_code
        FROM data_source
        ORDER BY source_code
    ) = ARRAY[
        'BIZINFO_ARCHIVE',
        'BIZINFO_OPEN_API',
        'CENTRAL_2023_H1'
    ],
    'exact confirmed source codes'
);
SELECT pg_temp.assert_true(
    NOT EXISTS (
        SELECT 1
        FROM data_source
        WHERE source_code IN ('BIZINFO_ARCHIVE', 'CENTRAL_2023_H1')
          AND (source_type <> 'FILE' OR is_search_source)
    ),
    'archive sources are FILE and excluded from search'
);

-- Password history and rollback behavior.
INSERT INTO app_user (login_id, email, password_hash)
VALUES ('password-test', 'password-test@example.com', 'hash-v1')
RETURNING
    id AS password_user_id,
    password_changed_at AS initial_password_changed_at
\gset

SELECT pg_temp.assert_true(
    NOT EXISTS (
        SELECT 1 FROM password_change_history
        WHERE user_id = :'password_user_id'
    ),
    'account creation does not create password history'
);
SELECT pg_temp.assert_true(
    :'initial_password_changed_at'::timestamptz IS NOT NULL,
    'account creation sets password_changed_at'
);
UPDATE app_user SET password_hash = 'hash-v2' WHERE id = :'password_user_id';
SELECT password_changed_at AS changed_password_at
  FROM app_user
 WHERE id = :'password_user_id'
\gset
SELECT pg_temp.assert_true(
    :'changed_password_at'::timestamptz > :'initial_password_changed_at'::timestamptz,
    'password change advances password_changed_at'
);
SELECT pg_temp.assert_true(
    (SELECT count(*) = 1
       FROM password_change_history
      WHERE user_id = :'password_user_id'),
    'password change creates one history row'
);
SELECT pg_temp.assert_true(
    (SELECT u.password_changed_at = h.changed_at
       FROM app_user u
       JOIN password_change_history h ON h.user_id = u.id
      WHERE u.id = :'password_user_id'),
    'password timestamp matches history timestamp'
);
UPDATE app_user SET password_hash = 'hash-v2' WHERE id = :'password_user_id';
SELECT pg_temp.assert_true(
    (SELECT password_changed_at = :'changed_password_at'::timestamptz
       FROM app_user
      WHERE id = :'password_user_id'),
    'same password hash preserves password_changed_at'
);
SELECT pg_temp.assert_true(
    (SELECT count(*) = 1
       FROM password_change_history
      WHERE user_id = :'password_user_id'),
    'same password hash does not create history'
);
SAVEPOINT password_change;
UPDATE app_user SET password_hash = 'hash-v3' WHERE id = :'password_user_id';
ROLLBACK TO SAVEPOINT password_change;
SELECT pg_temp.assert_true(
    (SELECT password_hash = 'hash-v2'
       FROM app_user
      WHERE id = :'password_user_id'),
    'rolled-back password hash is restored'
);
SELECT pg_temp.assert_true(
    (SELECT count(*) = 1
       FROM password_change_history
      WHERE user_id = :'password_user_id'),
    'rolled-back password change leaves no history'
);
DELETE FROM app_user WHERE id = :'password_user_id';
SELECT pg_temp.assert_true(
    NOT EXISTS (
        SELECT 1 FROM password_change_history
        WHERE user_id = :'password_user_id'
    ),
    'password history cascades with account deletion'
);

-- Case and file ownership constraints.
INSERT INTO app_user (login_id, email, password_hash)
VALUES ('owner-a', 'owner-a@example.com', 'hash')
RETURNING id AS owner_a_id
\gset
INSERT INTO app_user (login_id, email, password_hash)
VALUES ('owner-b', 'owner-b@example.com', 'hash')
RETURNING id AS owner_b_id
\gset
INSERT INTO inspection_case (owner_user_id)
VALUES (:'owner_a_id')
RETURNING id AS ownership_case_id
\gset

SELECT pg_temp.expect_error(
    'INSERT INTO sims.inspection_case (owner_user_id) VALUES (-1)',
    '23503'
);
SELECT pg_temp.expect_error(
    format(
        'INSERT INTO sims.inspection_case (owner_user_id, status) VALUES (%s, %L)',
        :'owner_a_id', 'UNKNOWN'
    ),
    '23514'
);
SELECT pg_temp.expect_error(
    format(
        'INSERT INTO sims.inspection_case (owner_user_id, top_k_used) VALUES (%s, 0)',
        :'owner_a_id'
    ),
    '23514'
);
SELECT pg_temp.expect_error(
    format(
        'INSERT INTO sims.inspection_case (owner_user_id, status) VALUES (%s, %L)',
        :'owner_a_id', 'COMPLETED'
    ),
    '23514'
);
SELECT pg_temp.expect_error(
    format(
        'INSERT INTO sims.file_asset '
        '(asset_scope, owner_user_id, inspection_case_id, storage_key, original_filename) '
        'VALUES (%L, %s, %s, %L, %L)',
        'USER', :'owner_b_id', :'ownership_case_id', 'wrong-owner', 'request.hwpx'
    ),
    '23503'
);
SELECT pg_temp.expect_error(
    'INSERT INTO sims.file_asset '
    '(asset_scope, storage_key, original_filename) '
    'VALUES (''USER'', ''missing-owner'', ''request.hwpx'')',
    '23514'
);

INSERT INTO file_asset (
    asset_scope, owner_user_id, inspection_case_id, storage_key, original_filename
) VALUES (
    'USER', :'owner_a_id', :'ownership_case_id', 'deleted-key', 'request.hwpx'
)
RETURNING id AS deleted_asset_id
\gset
DELETE FROM file_asset WHERE id = :'deleted_asset_id';
UPDATE object_delete_outbox
   SET processed_at = now()
 WHERE storage_key = 'deleted-key';
SELECT pg_temp.assert_true(
    (SELECT count(*) = 1
       FROM object_delete_outbox
      WHERE storage_key = 'deleted-key'),
    'file deletion creates a retained tombstone'
);
SELECT pg_temp.expect_error(
    format(
        'INSERT INTO sims.file_asset '
        '(asset_scope, owner_user_id, inspection_case_id, storage_key, original_filename) '
        'VALUES (%L, %s, %s, %L, %L)',
        'USER', :'owner_a_id', :'ownership_case_id', 'deleted-key', 'request.hwpx'
    ),
    'P0001',
    'storage_key % was previously deleted%'
);

INSERT INTO file_asset (asset_scope, storage_key, original_filename)
VALUES ('SHARED', 'shared-document', 'shared.pdf')
RETURNING id AS shared_asset_id
\gset
SELECT pg_temp.expect_error(
    format(
        'INSERT INTO sims.uploaded_document '
        '(inspection_case_id, file_asset_id, asset_scope, declared_format) '
        'VALUES (%s, %s, %L, %L)',
        :'ownership_case_id', :'shared_asset_id', 'USER', 'HWP'
    ),
    '23503'
);

INSERT INTO file_asset (
    asset_scope, owner_user_id, inspection_case_id, storage_key, original_filename
) VALUES (
    'USER', :'owner_a_id', :'ownership_case_id', 'cascade-key', 'request.hwp'
);

-- Current and searchable announcement views.
INSERT INTO announcement (source_code, pblanc_id, first_seen_at, last_seen_at)
VALUES ('BIZINFO_OPEN_API', 'OPEN-1', now(), now())
RETURNING id AS open_announcement_id
\gset
INSERT INTO announcement_version (
    announcement_id, version_no, content_sha256_hex, is_current, valid_to,
    pblanc_nm, pblanc_url, bsns_sumry_text, purpose, target, content,
    detail_ref_fields, source_created_at, period_raw_text, period_type,
    period_display_text, search_status
) VALUES (
    :'open_announcement_id', 1, repeat('a', 64), false, now(),
    'open old', 'https://example.com/open-old', 'summary', 'purpose', 'target', 'content',
    '{}', now(), 'always', 'ALWAYS', 'always', 'OPEN'
)
RETURNING id AS old_open_version_id
\gset
INSERT INTO announcement_version (
    announcement_id, version_no, content_sha256_hex,
    pblanc_nm, pblanc_url, bsns_sumry_text, purpose, target, content,
    detail_ref_fields, source_created_at, period_raw_text, period_type,
    period_display_text, search_status
) VALUES (
    :'open_announcement_id', 2, repeat('b', 64),
    'open', 'https://example.com/open', 'summary', 'purpose', 'target', 'content',
    ARRAY['target'], now(), 'always', 'ALWAYS', 'always', 'OPEN'
)
RETURNING id AS open_version_id
\gset

INSERT INTO announcement (source_code, pblanc_id, first_seen_at, last_seen_at)
VALUES ('BIZINFO_OPEN_API', 'UNKNOWN-1', now(), now())
RETURNING id AS unknown_announcement_id
\gset
INSERT INTO announcement_version (
    announcement_id, version_no, content_sha256_hex,
    pblanc_nm, pblanc_url, bsns_sumry_text, purpose, target, content,
    source_created_at, period_raw_text, period_type, period_display_text, search_status
) VALUES (
    :'unknown_announcement_id', 1, repeat('c', 64),
    'unknown', 'https://example.com/unknown', 'summary', 'purpose', 'target', 'content',
    now(), 'unknown', 'UNKNOWN', 'unknown', 'UNKNOWN'
)
RETURNING id AS unknown_version_id
\gset

INSERT INTO announcement (source_code, pblanc_id, first_seen_at, last_seen_at)
VALUES ('BIZINFO_OPEN_API', 'CLOSED-1', now(), now())
RETURNING id AS closed_announcement_id
\gset
INSERT INTO announcement_version (
    announcement_id, version_no, content_sha256_hex,
    pblanc_nm, pblanc_url, bsns_sumry_text, purpose, target, content,
    source_created_at, period_raw_text, period_type, period_display_text, search_status
) VALUES (
    :'closed_announcement_id', 1, repeat('d', 64),
    'closed', 'https://example.com/closed', 'summary', 'purpose', 'target', 'content',
    now(), 'closed', 'UNKNOWN', 'closed', 'CLOSED'
)
RETURNING id AS closed_version_id
\gset

SELECT pg_temp.assert_true(
    ARRAY(
        SELECT id FROM v_current_announcement ORDER BY id
    ) = ARRAY[
        :'open_version_id', :'unknown_version_id', :'closed_version_id'
    ]::bigint[],
    'current announcement view has the exact current versions'
);
SELECT pg_temp.assert_true(
    ARRAY(
        SELECT id FROM v_searchable_announcement ORDER BY id
    ) = ARRAY[
        :'open_version_id', :'unknown_version_id'
    ]::bigint[],
    'searchable announcement view has current OPEN and UNKNOWN'
);
SELECT pg_temp.assert_true(
    NOT EXISTS (
        SELECT 1 FROM v_current_announcement WHERE id = :'old_open_version_id'
    )
    AND NOT EXISTS (
        SELECT 1 FROM v_searchable_announcement WHERE id = :'closed_version_id'
    ),
    'views exclude old and CLOSED versions'
);
SELECT pg_temp.assert_true(
    (SELECT has_detail_ref FROM announcement_version WHERE id = :'open_version_id'),
    'allowed detail reference derives has_detail_ref'
);
SELECT pg_temp.expect_error(
    format(
        'UPDATE sims.announcement_version '
        'SET detail_ref_fields = ARRAY[%L] WHERE id = %s',
        'purpose', :'open_version_id'
    ),
    '23514'
);
SELECT pg_temp.expect_error(
    format(
        'INSERT INTO sims.announcement_version '
        '(announcement_id, version_no, content_sha256_hex, pblanc_nm, pblanc_url, '
        'bsns_sumry_text, purpose, target, content, source_created_at, period_raw_text, '
        'period_type, period_display_text, search_status) '
        'VALUES (%s, 3, repeat(''e'', 64), %L, %L, %L, %L, %L, %L, now(), %L, %L, %L, %L)',
        :'open_announcement_id', 'duplicate current', 'https://example.com/duplicate',
        'summary', 'purpose', 'target', 'content', 'always', 'ALWAYS', 'always', 'OPEN'
    ),
    '23505'
);

-- Embedding dimension and immutable definitions.
INSERT INTO inspection_case (owner_user_id)
VALUES (:'owner_a_id')
RETURNING id AS report_case_id
\gset
INSERT INTO embedding_model (
    provider, model_name, model_version, dimension, distance_metric
) VALUES ('test', 'three-dim', 'v1', 3, 'COSINE')
RETURNING id AS embedding_model_id
\gset
INSERT INTO embedding_profile (
    embedding_model_id, profile_name, version_no, profile_kind,
    field_codes, preprocessing_version
) VALUES (
    :'embedding_model_id', 'summary-test', 1, 'SUMMARY',
    ARRAY['purpose'], 'v1'
)
RETURNING id AS embedding_profile_id
\gset
INSERT INTO inspection_embedding (
    inspection_case_id, embedding_profile_id, input_text, input_sha256_hex, embedding
) VALUES (
    :'report_case_id', :'embedding_profile_id', 'request text', repeat('f', 64), '[1,2,3]'
)
RETURNING id AS inspection_embedding_id
\gset
SELECT pg_temp.assert_true(
    (SELECT vector_dims(embedding) = 3
       FROM inspection_embedding
      WHERE id = :'inspection_embedding_id'),
    'matching embedding dimension accepted'
);
SELECT pg_temp.expect_error(
    format(
        'INSERT INTO sims.inspection_embedding '
        '(inspection_case_id, embedding_profile_id, input_text, input_sha256_hex, embedding) '
        'VALUES (%s, %s, %L, repeat(''1'', 64), %L)',
        :'ownership_case_id', :'embedding_profile_id', 'wrong dimension', '[1,2]'
    ),
    'P0001',
    'embedding dimension mismatch:%'
);
SELECT pg_temp.expect_error(
    format(
        'INSERT INTO sims.inspection_embedding '
        '(inspection_case_id, embedding_profile_id, input_text, input_sha256_hex, embedding) '
        'VALUES (%s, %s, %L, repeat(''5'', 64), %L)',
        :'ownership_case_id', :'embedding_profile_id', '   ', '[1,2,3]'
    ),
    '23514'
);

INSERT INTO announcement_embedding (
    announcement_version_id, embedding_profile_id,
    input_text, input_sha256_hex, embedding
) VALUES (
    :'open_version_id', :'embedding_profile_id',
    'announcement text', repeat('2', 64), '[1,2,3]'
)
RETURNING id AS announcement_embedding_id
\gset
SELECT pg_temp.expect_error(
    format(
        'UPDATE sims.announcement_embedding SET embedding = %L WHERE id = %s',
        '[1,2]', :'announcement_embedding_id'
    ),
    'P0001',
    'embedding dimension mismatch:%'
);
SELECT pg_temp.expect_error(
    format(
        'UPDATE sims.announcement_embedding SET input_text = %L WHERE id = %s',
        '   ', :'announcement_embedding_id'
    ),
    '23514'
);

INSERT INTO document_parse_run (
    file_asset_id, attempt_no, parser_name, parser_version, status, finished_at
) VALUES (
    :'shared_asset_id', 1, 'contract-test', 'v1', 'SUCCESS', now()
)
RETURNING id AS shared_parse_run_id
\gset
INSERT INTO chunking_profile (
    profile_name, version_no, strategy
) VALUES (
    'contract-test', 1, 'fixed'
)
RETURNING id AS chunking_profile_id
\gset
INSERT INTO document_chunk_set (parse_run_id, chunking_profile_id)
VALUES (:'shared_parse_run_id', :'chunking_profile_id')
RETURNING id AS chunk_set_id
\gset
INSERT INTO document_chunk (
    chunk_set_id, chunk_no, content, content_sha256_hex
) VALUES (
    :'chunk_set_id', 0, 'chunk text', repeat('3', 64)
)
RETURNING id AS document_chunk_id
\gset
INSERT INTO embedding_profile (
    embedding_model_id, profile_name, version_no, profile_kind,
    field_codes, chunking_profile_id, preprocessing_version
) VALUES (
    :'embedding_model_id', 'chunk-test', 1, 'CHUNK',
    '{}', :'chunking_profile_id', 'v1'
)
RETURNING id AS chunk_embedding_profile_id
\gset
INSERT INTO chunk_embedding (
    document_chunk_id, embedding_profile_id,
    input_text, input_sha256_hex, embedding
) VALUES (
    :'document_chunk_id', :'chunk_embedding_profile_id',
    'chunk text', repeat('4', 64), '[1,2,3]'
)
RETURNING id AS chunk_embedding_id
\gset
SELECT pg_temp.expect_error(
    format(
        'UPDATE sims.chunk_embedding SET embedding = %L WHERE id = %s',
        '[1,2]', :'chunk_embedding_id'
    ),
    'P0001',
    'embedding dimension mismatch:%'
);
SELECT pg_temp.expect_error(
    format(
        'UPDATE sims.chunk_embedding SET input_text = %L WHERE id = %s',
        '   ', :'chunk_embedding_id'
    ),
    '23514'
);

SELECT pg_temp.expect_error(
    format(
        'UPDATE sims.embedding_model SET dimension = 4 WHERE id = %s',
        :'embedding_model_id'
    ),
    'P0001',
    'embedding model identity is immutable%'
);
UPDATE embedding_model SET is_enabled = false WHERE id = :'embedding_model_id';
SELECT pg_temp.assert_true(
    (SELECT NOT is_enabled FROM embedding_model WHERE id = :'embedding_model_id'),
    'embedding model enabled flag remains mutable'
);
SELECT pg_temp.expect_error(
    format(
        'UPDATE sims.embedding_profile SET preprocessing_version = %L WHERE id = %s',
        'v2', :'embedding_profile_id'
    ),
    'P0001',
    'embedding profile definition is immutable%'
);
UPDATE embedding_profile SET is_active = true WHERE id = :'embedding_profile_id';
SELECT pg_temp.assert_true(
    (SELECT is_active FROM embedding_profile WHERE id = :'embedding_profile_id'),
    'embedding profile active flag remains mutable'
);

-- Minimal valid report chain and report immutability.
INSERT INTO file_asset (
    asset_scope, owner_user_id, inspection_case_id, storage_key, original_filename
) VALUES (
    'USER', :'owner_a_id', :'report_case_id', 'report-case-file', 'request.hwpx'
)
RETURNING id AS report_file_id
\gset
INSERT INTO uploaded_document (
    inspection_case_id, file_asset_id, declared_format
) VALUES (
    :'report_case_id', :'report_file_id', 'HWPX'
)
RETURNING id AS uploaded_document_id
\gset
INSERT INTO document_parse_run (
    file_asset_id, attempt_no, parser_name, parser_version, status, finished_at
) VALUES (
    :'report_file_id', 1, 'contract-test', 'v1', 'SUCCESS', now()
)
RETURNING id AS parse_run_id
\gset
INSERT INTO form_schema (schema_name, version_no)
VALUES ('contract-test', 1)
RETURNING id AS form_schema_id
\gset
INSERT INTO request_extraction (
    inspection_case_id, form_schema_id, parse_run_id,
    status, extractor_name, extractor_version
) VALUES (
    :'report_case_id', :'form_schema_id', :'parse_run_id',
    'SUCCESS', 'contract-test', 'v1'
)
RETURNING id AS extraction_id
\gset
INSERT INTO missing_check_run (
    inspection_case_id, request_extraction_id, ruleset_version, status
) VALUES (
    :'report_case_id', :'extraction_id', 'contract-test-v1', 'SUCCESS'
)
RETURNING id AS missing_run_id
\gset
INSERT INTO retrieval_run (
    inspection_case_id, inspection_embedding_id, status,
    top_k_used, corpus_snapshot_at
) VALUES (
    :'report_case_id', :'inspection_embedding_id', 'SUCCESS', 5, now()
)
RETURNING id AS retrieval_run_id
\gset
INSERT INTO inspection_report (
    inspection_case_id, missing_check_run_id, retrieval_run_id,
    report_schema_version, report_json, finalized_at
) VALUES (
    :'report_case_id', :'missing_run_id', :'retrieval_run_id',
    'contract-test-v1', '{"ok": true}', now()
)
RETURNING id AS report_id
\gset
SELECT pg_temp.expect_error(
    format(
        'UPDATE sims.inspection_report SET report_json = %L WHERE id = %s',
        '{"ok": false}', :'report_id'
    ),
    'P0001',
    'inspection_report is immutable;%'
);

-- Case deletion cascades user-owned data and enqueues physical deletion.
DELETE FROM inspection_case WHERE id = :'report_case_id';
SELECT pg_temp.assert_true(
    NOT EXISTS (SELECT 1 FROM inspection_report WHERE id = :'report_id'),
    'report cascades with case deletion'
);
SELECT pg_temp.assert_true(
    NOT EXISTS (
        SELECT 1 FROM uploaded_document WHERE id = :'uploaded_document_id'
    )
    AND NOT EXISTS (
        SELECT 1 FROM document_parse_run WHERE id = :'parse_run_id'
    )
    AND NOT EXISTS (
        SELECT 1 FROM request_extraction WHERE id = :'extraction_id'
    )
    AND NOT EXISTS (
        SELECT 1 FROM missing_check_run WHERE id = :'missing_run_id'
    )
    AND NOT EXISTS (
        SELECT 1 FROM inspection_embedding WHERE id = :'inspection_embedding_id'
    )
    AND NOT EXISTS (
        SELECT 1 FROM retrieval_run WHERE id = :'retrieval_run_id'
    ),
    'case deletion removes every case-owned analysis row'
);
SELECT pg_temp.assert_true(
    EXISTS (
        SELECT 1 FROM object_delete_outbox
        WHERE storage_key = 'report-case-file'
    ),
    'case deletion enqueues owned file deletion'
);
SELECT pg_temp.assert_true(
    EXISTS (SELECT 1 FROM announcement WHERE id = :'open_announcement_id'),
    'shared announcements survive case deletion'
);

DELETE FROM inspection_case WHERE id = :'ownership_case_id';
SELECT pg_temp.assert_true(
    EXISTS (
        SELECT 1 FROM object_delete_outbox
        WHERE storage_key = 'cascade-key'
    ),
    'case cascade creates a tombstone for each owned file'
);

ROLLBACK;
