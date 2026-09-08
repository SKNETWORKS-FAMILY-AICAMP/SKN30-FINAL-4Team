-- ============================================================================
-- Migration 000: auth 스키마 스텁 (팀원 마이그레이션 선행 조건)
--
-- 팀원 패키지(01~09)는 self-hosted Supabase 를 전제로 `auth.users` 와
-- `auth.uid()` 가 이미 있다고 가정한다. 순수 PostgreSQL 에서는 9개 중 8개가
-- `schema "auth" does not exist` 하나 때문에 실패한다. 이 파일은 그 전제만
-- 최소로 채워 로컬·CI 에서 팀원 DDL 이 그대로 적용되게 한다.
--
-- 운영에서는 self-hosted Supabase Auth 가 `auth.users` 와 `auth.uid()` 의
-- 주인이다. 실제 Supabase Auth 를 붙이는 시점에 이 파일은 **병합이 아니라
-- 삭제**한다. 그래서 표면을 `auth.users(id)` 와 `auth.uid()` 둘로만 잡았다.
-- 컬럼을 늘리면 교체가 삭제가 아니라 재작성이 된다.
--
-- 비밀번호 컬럼·인증 로직은 여기 넣지 않는다. 신원(identity) 이관은 별도
-- 단계다.
-- ============================================================================

BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE SCHEMA IF NOT EXISTS auth;

-- Supabase 의 auth.users 는 컬럼이 훨씬 많지만, 팀원 DDL 이 실제로 참조하는
-- 것은 FK 대상인 id 하나뿐이다.
CREATE TABLE IF NOT EXISTS auth.users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid()
);

-- RLS 정책(08)이 호출한다. 순수 PostgreSQL 에는 JWT 클레임이 없으므로
-- 설정되지 않은 경우 NULL 을 돌려준다.
CREATE OR REPLACE FUNCTION auth.uid() RETURNS UUID
LANGUAGE sql STABLE
AS $$ SELECT nullif(current_setting('request.jwt.claim.sub', true), '')::uuid $$;

-- 08_rls_policies.sql 의 GRANT 대상 롤. Supabase 가 만들어 두는 것들이라
-- 없을 때만 만든다.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        CREATE ROLE anon NOLOGIN NOINHERIT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        CREATE ROLE authenticated NOLOGIN NOINHERIT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
        CREATE ROLE service_role NOLOGIN NOINHERIT BYPASSRLS;
    END IF;
END
$$;

COMMIT;
