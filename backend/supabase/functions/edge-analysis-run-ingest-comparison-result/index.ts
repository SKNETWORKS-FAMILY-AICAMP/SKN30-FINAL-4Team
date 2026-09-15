import { ApiError, corsPreflight, errorResponse, json, parseJson, requireWorkerCallback, serviceClient } from "../_shared/supabase.ts"

/** Terminal worker callback. The database function replaces a previous retry
 * result for the same run atomically, then marks the run succeeded. */
Deno.serve(async (request) => {
  if (request.method === "OPTIONS") return corsPreflight()
  if (request.method !== "POST") return json(405, { code: "METHOD_NOT_ALLOWED", message: "POST만 허용됩니다." })
  try {
    requireWorkerCallback(request)
    const body = await parseJson(request)
    const analysisRunId = typeof body.analysis_run_id === "string" ? body.analysis_run_id : ""
    const comparisonResult = body.comparison_result
    if (!analysisRunId || !comparisonResult || typeof comparisonResult !== "object" || Array.isArray(comparisonResult)) {
      throw new ApiError(400, "INVALID_WORKER_RESULT", "analysis_run_id와 comparison_result가 필요합니다.")
    }
    const admin = serviceClient()
    const { data: analysisCaseId, error } = await admin.schema("api")
      .rpc("ingest_comparison_result_core", { p_analysis_run_id: analysisRunId, p_result: comparisonResult })
    if (error || !analysisCaseId) throw error ?? new Error("comparison result was not materialized")
    return json(200, { analysis_run_id: analysisRunId, analysis_case_id: analysisCaseId, status: "succeeded" })
  } catch (error) { return errorResponse(error) }
})
