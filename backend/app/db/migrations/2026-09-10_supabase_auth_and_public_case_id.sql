-- Supabase Auth 연결 · 외부 Case 식별자 UUID · Chat 답변 부속 정보 저장.
--
-- schema.sql 은 fresh-install DDL 이라 이미 데이터가 들어 있는 DB 에는 적용할
-- 수 없다. 그쪽을 위한 ALTER 스크립트다. 새로 설치하는 경우에는 schema.sql 에
-- 같은 정의가 들어 있으므로 이 파일을 돌릴 필요가 없다.
--
-- 1. sims.app_user.supabase_user_id
--    로그인은 Supabase Auth 가 맡고 FastAPI 는 그 토큰을 검증한다. 토큰의
--    sub(auth.users.id) 로 내부 사용자를 찾을 자리가 필요하다. 내부 PK(bigint)
--    는 그대로 둔다 — 이미 모든 테이블이 그것을 참조한다.
--
-- 2. sims.inspection_case.analysis_case_id
--    프론트가 내부 PK 를 알 필요가 없게 한다. 외부 API 는 UUID 만 주고받고
--    FastAPI 안에서 bigint 로 바꾼다. 순번 PK 를 URL 에 노출하면 남의 분석 건
--    번호를 훑을 수 있다는 문제도 같이 없어진다.
--
-- 3. sims.chat_message.references / suggested_revision
--    답변의 근거와 수정 제안을 저장한다. 지금은 POST 응답에만 실려 새로고침하면
--    사라진다. evidence_refs 는 references[].evidence_id 로 대체되므로 지운다 —
--    같은 것을 두 모양으로 들고 있으면 어느 쪽이 정답인지 아무도 모른다.

BEGIN;

-- 1 -------------------------------------------------------------------------
ALTER TABLE sims.app_user
    ADD COLUMN IF NOT EXISTS supabase_user_id uuid;

CREATE UNIQUE INDEX IF NOT EXISTS uq_app_user_supabase_user_id
    ON sims.app_user (supabase_user_id)
    WHERE supabase_user_id IS NOT NULL;

-- 2 -------------------------------------------------------------------------
ALTER TABLE sims.inspection_case
    ADD COLUMN IF NOT EXISTS analysis_case_id uuid;

-- 기존 행에 값을 채운 뒤에야 NOT NULL 을 걸 수 있다.
UPDATE sims.inspection_case
   SET analysis_case_id = gen_random_uuid()
 WHERE analysis_case_id IS NULL;

ALTER TABLE sims.inspection_case
    ALTER COLUMN analysis_case_id SET DEFAULT gen_random_uuid(),
    ALTER COLUMN analysis_case_id SET NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_inspection_case_analysis_case_id
    ON sims.inspection_case (analysis_case_id);

-- 소유자 확인과 UUID 조회를 한 번에 한다. 외부 식별자로 들어온 요청은 항상
-- (analysis_case_id, owner_user_id) 로 찾기 때문이다.
CREATE INDEX IF NOT EXISTS ix_inspection_case_public_owner
    ON sims.inspection_case (analysis_case_id, owner_user_id);

-- 3 -------------------------------------------------------------------------
ALTER TABLE sims.chat_message
    ADD COLUMN IF NOT EXISTS "references" jsonb NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS suggested_revision text;

ALTER TABLE sims.chat_message
    DROP CONSTRAINT IF EXISTS ck_chat_message_references_shape,
    DROP CONSTRAINT IF EXISTS ck_chat_message_user_has_no_answer_parts;

ALTER TABLE sims.chat_message
    ADD CONSTRAINT ck_chat_message_references_shape
        CHECK (jsonb_typeof("references") = 'array'),
    -- 근거와 수정 제안은 답변에만 붙는다. 사용자 질문 행에 들어가면 화면이
    -- 사용자가 제안한 것처럼 그린다.
    ADD CONSTRAINT ck_chat_message_user_has_no_answer_parts
        CHECK (
            role <> 'USER'
            OR ("references" = '[]'::jsonb AND suggested_revision IS NULL)
        );

ALTER TABLE sims.chat_message
    DROP COLUMN IF EXISTS evidence_refs;

COMMIT;
