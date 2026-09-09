-- sims.announcement_version 에 Model 1 지원유형 라벨 컬럼 추가.
--
-- schema.sql 은 fresh-install DDL 이라 이미 공고가 들어 있는 DB 에는 적용할 수
-- 없다. 그쪽을 위한 ALTER 스크립트다. 새로 설치하는 경우에는 schema.sql 에
-- 같은 정의가 이미 들어 있으므로 이 파일을 돌릴 필요가 없다.
--
-- 컬럼을 다섯 개로 나눈 이유는 schema.sql 의 주석을 참조. 요약하면 support_type
-- 은 분류 '결과' 이고 실측상 28% 만 신뢰 등급이라, 값만 남기면 하류가 확정된
-- 사실로 읽는다.

BEGIN;

ALTER TABLE sims.announcement_version
    ADD COLUMN IF NOT EXISTS support_type            text,
    ADD COLUMN IF NOT EXISTS support_type_confidence numeric(6,5),
    ADD COLUMN IF NOT EXISTS support_type_grade      text,
    ADD COLUMN IF NOT EXISTS support_type_model_version text,
    ADD COLUMN IF NOT EXISTS support_type_classified_at timestamptz;

ALTER TABLE sims.announcement_version
    DROP CONSTRAINT IF EXISTS ck_announcement_support_type_shape,
    DROP CONSTRAINT IF EXISTS ck_announcement_support_type_grade,
    DROP CONSTRAINT IF EXISTS ck_announcement_support_type_confidence;

ALTER TABLE sims.announcement_version
    ADD CONSTRAINT ck_announcement_support_type_shape
        CHECK (
            (support_type IS NULL
             AND support_type_confidence IS NULL
             AND support_type_grade IS NULL
             AND support_type_model_version IS NULL
             AND support_type_classified_at IS NULL)
            OR
            (support_type IS NOT NULL
             AND support_type_confidence IS NOT NULL
             AND support_type_grade IS NOT NULL
             AND support_type_model_version IS NOT NULL
             AND support_type_classified_at IS NOT NULL)
        ),
    ADD CONSTRAINT ck_announcement_support_type_grade
        CHECK (
            support_type_grade IS NULL
            OR support_type_grade IN ('trusted', 'reference', 'hold')
        ),
    ADD CONSTRAINT ck_announcement_support_type_confidence
        CHECK (
            support_type_confidence IS NULL
            OR (support_type_confidence >= 0 AND support_type_confidence <= 1)
        );

CREATE INDEX IF NOT EXISTS ix_announcement_version_support_type
    ON sims.announcement_version (support_type)
    WHERE is_current AND support_type IS NOT NULL;

COMMIT;
