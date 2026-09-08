-- PoC: Model 1/2/3 결과 전용 테이블.
--
-- axis_result.result_data 를 재사용하지 않는다. axis_result 는 CPL 13개·FIT 7개
-- 축 결과용이고, ML 세 모델은 축이 아니라 별개의 산출물이다. envelope 을 통째로
-- 보존하는 편이 조회도 단순하고 모델을 늘리기도 쉽다.
--
-- Prerequisite: 10~13.

BEGIN;

CREATE TABLE result.model_result (
  model_result_pk   UUID PRIMARY KEY DEFAULT gen_random_uuid(),

  analysis_case_pk  UUID NOT NULL
                      REFERENCES result.analysis_case(analysis_case_pk)
                      ON DELETE CASCADE,

  model_name        TEXT NOT NULL
                      CHECK (model_name IN ('MODEL_1', 'MODEL_2', 'MODEL_3')),

  -- ML Result envelope 의 status 를 그대로 옮긴다. 넷을 구분해야 하는 이유:
  --   failed            그 모델이 실행되다가 죽었다
  --   not_available     선행 의존(Model 1)이 없어 실행 자체를 못 했다
  --   insufficient_data 실행은 됐지만 근거가 모자라 점수를 내지 않았다
  -- Model 1 이 실패해도 Model 2·3 을 failed 로 바꾸지 않는다. 자기 잘못이 아니고,
  -- 사용자가 할 일도 다르다.
  status            TEXT NOT NULL
                      CHECK (status IN ('success', 'failed',
                                        'not_available', 'insufficient_data')),

  result_data       JSONB NULL,   -- envelope.result   (챗봇이 근거로 읽는 자리)
  metadata          JSONB NULL,   -- envelope.metadata + model_info + input_data
  error             JSONB NULL,   -- envelope.error

  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),

  -- 분석 건당 모델별 한 행. 재실행은 upsert 로 덮는다.
  UNIQUE (analysis_case_pk, model_name),

  -- 성공인데 결과가 비어 있거나, 실패인데 결과가 차 있으면 하류가 오해한다.
  CONSTRAINT model_result_status_shape
    CHECK (
      (status = 'success' AND result_data IS NOT NULL AND error IS NULL)
      OR
      (status <> 'success' AND result_data IS NULL)
    )
);

CREATE INDEX ix_result_model_result_case
  ON result.model_result (analysis_case_pk, model_name);

COMMENT ON TABLE result.model_result IS
  'Model 1/2/3 실행 결과. ML Result envelope 을 분해해 보관하며 부분 실패도 그대로 남긴다.';

-- ---------------------------------------------------------------------------
-- 권한: 다른 result.* 테이블과 같은 규칙. 사용자는 자기 분석 건만 읽는다.
-- 쓰기는 service role(워커)만 한다.
-- ---------------------------------------------------------------------------
ALTER TABLE result.model_result ENABLE ROW LEVEL SECURITY;

CREATE POLICY model_result_select_own ON result.model_result
  FOR SELECT TO authenticated
  USING (
    EXISTS (
      SELECT 1 FROM result.analysis_case c
      WHERE c.analysis_case_pk = model_result.analysis_case_pk
        AND c.user_id = auth.uid()
    )
  );

REVOKE ALL ON result.model_result FROM authenticated;
GRANT SELECT ON result.model_result TO authenticated;

COMMIT;
