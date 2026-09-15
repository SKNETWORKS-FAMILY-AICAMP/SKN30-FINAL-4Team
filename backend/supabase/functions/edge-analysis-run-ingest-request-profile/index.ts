import { ApiError, corsPreflight, errorResponse, json, parseJson, requireWorkerCallback, serviceClient } from "../_shared/supabase.ts"

Deno.serve(async (request) => {
  if (request.method === "OPTIONS") return corsPreflight()
  if (request.method !== "POST") return json(405, { code: "METHOD_NOT_ALLOWED", message: "POST만 허용됩니다." })
  try {
    requireWorkerCallback(request)
    const body = await parseJson(request)
    const analysisRunId = typeof body.analysis_run_id === "string" ? body.analysis_run_id : ""
    const profile = body.request_profile
    if (!analysisRunId || !profile || typeof profile !== "object" || Array.isArray(profile)) {
      throw new ApiError(400, "INVALID_WORKER_RESULT", "analysis_run_id와 request_profile이 필요합니다.")
    }
    const admin = serviceClient()
    const { data: run, error: runError } = await admin.schema("workspace").from("analysis_run")
      .select("analysis_run_pk,status").eq("analysis_run_pk", analysisRunId).maybeSingle()
    if (runError) throw runError
    if (!run || !["queued", "running"].includes(run.status)) {
      throw new ApiError(409, "ANALYSIS_RUN_NOT_ACCEPTING_RESULT", "결과를 받을 수 없는 분석 작업입니다.")
    }
    const { data: requestProfilePk, error: ingestError } = await admin.schema("workspace")
      .rpc("ingest_request_profile_core", { p_analysis_run_id: analysisRunId, p_profile: profile })
    if (ingestError || !requestProfilePk) throw ingestError ?? new Error("request profile was not materialized")
    const { error: stateError } = await admin.schema("workspace").from("analysis_run")
      .update({ status: "running", error_code: null, error_message: null }).eq("analysis_run_pk", analysisRunId)
    if (stateError) throw stateError
    return json(200, { analysis_run_id: analysisRunId, request_profile_id: requestProfilePk, status: "running" })
  } catch (error) { return errorResponse(error) }
})
