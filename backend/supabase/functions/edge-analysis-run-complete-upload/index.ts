import { errorResponse, ApiError, corsPreflight, json, parseJson, requireUserId, serviceClient } from "../_shared/supabase.ts"
import { workerDispatcherFromEnvironment } from "../_shared/worker-http-dispatch.ts"

Deno.serve(async (request) => {
  if (request.method === "OPTIONS") return corsPreflight()
  if (request.method !== "POST") return json(405, { code: "METHOD_NOT_ALLOWED", message: "POST만 허용됩니다." })
  try {
    const userId = await requireUserId(request)
    const body = await parseJson(request)
    const runId = typeof body.analysis_run_id === "string" ? body.analysis_run_id : ""
    if (!runId) throw new ApiError(400, "INVALID_ANALYSIS_RUN", "analysis_run_id가 필요합니다.")
    const admin = serviceClient()
    const { data: run, error: runError } = await admin.schema("workspace").from("analysis_run")
      .select("analysis_run_pk,user_id,status,original_filename,declared_mime_type,declared_size_bytes")
      .eq("analysis_run_pk", runId).maybeSingle()
    if (runError) throw runError
    if (!run || run.user_id !== userId) throw new ApiError(404, "ANALYSIS_RUN_NOT_FOUND", "분석 작업을 찾을 수 없습니다.")
    if (run.status !== "uploading") throw new ApiError(409, "ANALYSIS_RUN_NOT_UPLOADABLE", "업로드를 완료할 수 없는 상태입니다.")
    const { data: dispatch, error: dispatchError } = await admin.schema("workspace").from("analysis_run_dispatch")
      .select("source_bucket,source_object_key").eq("analysis_run_pk", runId).single()
    if (dispatchError || !dispatch) throw dispatchError ?? new Error("dispatch reservation missing")
    const slash = dispatch.source_object_key.lastIndexOf("/")
    const { data: objects, error: objectError } = await admin.storage.from(dispatch.source_bucket)
      .list(dispatch.source_object_key.slice(0, slash), { search: dispatch.source_object_key.slice(slash + 1) })
    if (objectError || !objects?.some((item) => item.name === dispatch.source_object_key.slice(slash + 1))) {
      throw new ApiError(409, "SOURCE_OBJECT_NOT_FOUND", "업로드된 원본 파일을 찾을 수 없습니다.")
    }
    try {
      const callbackUrl = Deno.env.get("ANALYSIS_WORKER_INGEST_PROFILE_URL")
      const callbackToken = Deno.env.get("ANALYSIS_WORKER_CALLBACK_TOKEN")
      if (!callbackUrl || !callbackToken) throw new Error("worker callback configuration is missing")
      const functionsBase = callbackUrl.replace(/\/edge-analysis-run-ingest-request-profile\/?$/, "")
      if (functionsBase === callbackUrl) throw new Error("worker ingest profile URL is not a function endpoint")
      const { data: signedSource, error: signedSourceError } = await admin.storage
        .from(dispatch.source_bucket)
        .createSignedUrl(dispatch.source_object_key, 60 * 60)
      if (signedSourceError || !signedSource?.signedUrl) {
        throw signedSourceError ?? new Error("source signed URL was not created")
      }
      const accepted = await workerDispatcherFromEnvironment().dispatch({ schema_version: "analysis_worker_job/v1", analysis_run_id: runId, user_id: userId,
        source: { bucket: dispatch.source_bucket, object_key: dispatch.source_object_key, download_url: signedSource.signedUrl, original_filename: run.original_filename ?? "source", content_type: run.declared_mime_type, size_bytes: run.declared_size_bytes ?? 0 },
        callback: {
          ingest_request_profile_url: callbackUrl,
          ingest_comparison_result_url: `${functionsBase}/edge-analysis-run-ingest-comparison-result`,
          existing_candidates_url: `${functionsBase}/edge-worker-existing-candidates`,
          existing_profile_url: `${functionsBase}/edge-worker-existing-profile`,
          callback_token: callbackToken,
        }, requested_at: new Date().toISOString() })
      await admin.schema("workspace").from("analysis_run_dispatch").update({ worker_job_id: accepted.worker_job_id, dispatched_at: new Date().toISOString() }).eq("analysis_run_pk", runId)
      await admin.schema("workspace").from("analysis_run").update({ status: "queued" }).eq("analysis_run_pk", runId)
      return json(202, { analysis_run_id: runId, status: "queued" })
    } catch (error) {
      await admin.schema("workspace").from("analysis_run").update({ status: "failed", error_code: "WORKER_UNAVAILABLE", error_message: "분석 작업을 시작하지 못했습니다. 다시 시도해 주세요." }).eq("analysis_run_pk", runId)
      console.error(error)
      throw new ApiError(503, "WORKER_UNAVAILABLE", "분석 작업을 시작하지 못했습니다. 다시 시도해 주세요.")
    }
  } catch (error) { return errorResponse(error) }
})
