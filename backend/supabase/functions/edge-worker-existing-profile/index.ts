import { ApiError, corsPreflight, errorResponse, json, parseJson, requireWorkerCallback, serviceClient } from "../_shared/supabase.ts"

const PROFILE_URL_TTL_SECONDS = 15 * 60

Deno.serve(async (request) => {
  if (request.method === "OPTIONS") return corsPreflight()
  if (request.method !== "POST") return json(405, { code: "METHOD_NOT_ALLOWED", message: "POST만 허용됩니다." })
  try {
    requireWorkerCallback(request)
    const body = await parseJson(request)
    const sourceProfileId = typeof body.source_profile_id === "string" ? body.source_profile_id.trim() : ""
    if (!sourceProfileId) throw new ApiError(400, "SOURCE_PROFILE_ID_REQUIRED", "source_profile_id가 필요합니다.")
    const admin = serviceClient()
    const { data, error } = await admin.schema("api").rpc("worker_get_existing_profile", { p_source_profile_id: sourceProfileId })
    if (error) throw error
    const profile = data?.[0]
    if (!profile) throw new ApiError(404, "EXISTING_PROFILE_NOT_FOUND", "Existing Profile을 찾을 수 없습니다.")
    const { data: signed, error: signedError } = await admin.storage
      .from(profile.storage_bucket)
      .createSignedUrl(profile.storage_object_key, PROFILE_URL_TTL_SECONDS)
    if (signedError || !signed?.signedUrl) throw signedError ?? new Error("profile signed URL was not created")
    return json(200, {
      profile_version_id: profile.profile_version_id,
      source_profile_id: profile.source_profile_id,
      notice_id: profile.notice_id,
      source_kind: profile.source_kind,
      portal_metadata: profile.portal_metadata,
      schema_version: profile.schema_version,
      structured_profile_download_url: signed.signedUrl,
      expires_in_seconds: PROFILE_URL_TTL_SECONDS,
    })
  } catch (error) { return errorResponse(error) }
})
