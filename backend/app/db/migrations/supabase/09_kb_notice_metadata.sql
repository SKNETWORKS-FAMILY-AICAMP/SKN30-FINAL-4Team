BEGIN;

-- Bizinfo list/detail metadata is source-controlled portal data, not LLM
-- output. Keeping it on the notice root makes title/date/agency filters
-- available without altering Common IR exact-span evidence.
ALTER TABLE kb.notice
    ADD COLUMN IF NOT EXISTS portal_metadata JSONB NOT NULL DEFAULT '{}'::jsonb;

COMMIT;
