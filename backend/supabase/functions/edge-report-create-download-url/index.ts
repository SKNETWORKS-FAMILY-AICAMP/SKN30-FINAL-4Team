import { ApiError, corsPreflight, errorResponse, json, parseJson, requireUserId, serviceClient } from "../_shared/supabase.ts"

const SIGNED_URL_TTL_SECONDS = 60

Deno.serve(async (request) => {
  if (request.method === "OPTIONS") return corsPreflight()
  if (request.method !== "POST") return json(405, { code: "METHOD_NOT_ALLOWED", message: "POST만 허용됩니다." })

  try {
    const userId = await requireUserId(request)
    const body = await parseJson(request)
    const analysisCaseId = typeof body.analysis_case_id === "string" ? body.analysis_case_id : ""
    if (!analysisCaseId) throw new ApiError(400, "INVALID_ANALYSIS_CASE", "analysis_case_id가 필요합니다.")

    const admin = serviceClient()
    const { data: analysisCase, error: caseError } = await admin.schema("result").from("analysis_case")
      .select("analysis_case_pk").eq("analysis_case_pk", analysisCaseId).eq("user_id", userId).maybeSingle()
    if (caseError) throw caseError
    if (!analysisCase) throw new ApiError(404, "ANALYSIS_CASE_NOT_FOUND", "분석 결과를 찾을 수 없습니다.")

    const { data: report, error: reportError } = await admin.schema("result").from("report_artifact")
      .select("storage_bucket,storage_object_key,status,expires_at")
      .eq("analysis_case_pk", analysisCaseId).order("created_at", { ascending: false }).limit(1).maybeSingle()
    if (reportError) throw reportError
    if (!report || report.status !== "ready" || !report.storage_bucket || !report.storage_object_key) {
      throw new ApiError(409, "REPORT_NOT_READY", "다운로드할 보고서가 아직 준비되지 않았습니다.")
    }
    if (new Date(report.expires_at).getTime() <= Date.now()) {
      throw new ApiError(410, "REPORT_EXPIRED", "보고서 보관 기간이 만료되었습니다.")
    }

    const { data: signed, error: signedError } = await admin.storage.from(report.storage_bucket)
      .createSignedUrl(report.storage_object_key, SIGNED_URL_TTL_SECONDS)
    if (signedError || !signed?.signedUrl) throw signedError ?? new Error("signed URL was not returned")
    return json(200, { signed_url: signed.signedUrl, expires_in_seconds: SIGNED_URL_TTL_SECONDS })
  } catch (error) {
    return errorResponse(error)
  }
})
