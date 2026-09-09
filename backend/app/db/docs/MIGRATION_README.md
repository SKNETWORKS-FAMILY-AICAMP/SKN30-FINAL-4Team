# PoC Supabase 보완 Migration

이 디렉터리의 `10~13` migration은 기준 스키마 아카이브
`dist/data_retention_architecture_20260902.zip` 안의 `01~09` migration을 먼저 적용한
Supabase/Postgres DB를 대상으로 한다.

적용 순서:

1. 기준 아카이브의 `01_core_schemas.sql`부터 `09_kb_notice_metadata.sql`
2. 이 디렉터리의 `10_poc_analysis_lifecycle.sql`부터 순서대로

빈 PoC DB를 전제로 하며, 기존 운영 데이터의 backfill migration은 포함하지 않는다.
