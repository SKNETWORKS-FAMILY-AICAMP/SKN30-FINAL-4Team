-- ============================================================================
-- Migration 01: Core Schemas Creation
-- Date: 2026-08-31
-- Purpose: Create all required schemas for self-hosted Supabase
--
-- Schemas created:
--   - app: User profile and application settings
--   - ops: Processing runs, model invocations, cleanup events
--   - kb: Existing knowledge base with version lineage
--   - workspace: Request workspace (temporary during analysis)
--   - result: Analysis results and conversation sessions
-- ============================================================================

BEGIN;

-- Create all schemas if they don't exist
CREATE SCHEMA IF NOT EXISTS app;
CREATE SCHEMA IF NOT EXISTS ops;
CREATE SCHEMA IF NOT EXISTS kb;
CREATE SCHEMA IF NOT EXISTS workspace;
CREATE SCHEMA IF NOT EXISTS result;
CREATE SCHEMA IF NOT EXISTS retrieval;

-- Set search path for this migration
SET search_path TO app, ops, kb, workspace, result, retrieval, public;

COMMIT;
