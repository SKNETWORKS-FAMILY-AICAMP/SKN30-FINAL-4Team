import { ApiError, corsPreflight, errorResponse, json, parseJson, requireUserId, serviceClient } from "../_shared/supabase.ts"

const MAX_BYTES = 50 * 1024 * 1024

Deno.serve(async (request) => {
  if (request.method === "OPTIONS") return corsPreflight()
  if (request.method !== "POST") return json(405, { code: "METHOD_NOT_ALLOWED", message: "POST만 허용됩니다." })
  try {
    const userId = await requireUserId(request)
    const body = await parseJson(request)
    const filename = string(body.original_filename, "original_filename")
    const size = number(body.declared_size_bytes, "declared_size_bytes")
    const extension = extensionOf(filename)
    if (size > MAX_BYTES) throw new ApiError(400, "FILE_TOO_LARGE", "파일 크기는 50MiB 이하여야 합니다.")

    const admin = serviceClient()
    const { data: run, error } = await admin.schema("workspace").from("analysis_run")
      .insert({ user_id: userId, status: "uploading", original_filename: filename,
        declared_mime_type: typeof body.declared_mime_type === "string" ? body.declared_mime_type : null,
        declared_size_bytes: size })
      .select("analysis_run_pk").single()
    if (error?.code === "23505") throw new ApiError(409, "ANALYSIS_ALREADY_IN_PROGRESS", "진행 중인 분석이 있습니다.")
    if (error || !run) throw error ?? new Error("analysis run was not created")

    const objectKey = `request-source/${userId}/${run.analysis_run_pk}/source.${extension}`
    const { error: dispatchError } = await admin.schema("workspace").from("analysis_run_dispatch")
      .insert({ analysis_run_pk: run.analysis_run_pk, source_bucket: "request-temp", source_object_key: objectKey })
    if (dispatchError) throw dispatchError
    return json(201, { analysis_run_id: run.analysis_run_pk, bucket: "request-temp", object_key: objectKey })
  } catch (error) { return errorResponse(error) }
})

function string(value: unknown, name: string): string {
  if (typeof value !== "string" || !value.trim()) throw new ApiError(400, "INVALID_FILE", `${name} 값이 필요합니다.`)
  return value.trim()
}
function number(value: unknown, name: string): number {
  if (typeof value !== "number" || !Number.isInteger(value) || value < 0) throw new ApiError(400, "INVALID_FILE", `${name} 값이 올바르지 않습니다.`)
  return value
}
function extensionOf(filename: string): "hwp" | "hwpx" {
  const extension = filename.split(".").pop()?.toLowerCase()
  if (extension !== "hwp" && extension !== "hwpx") throw new ApiError(400, "UNSUPPORTED_FILE_TYPE", "HWP 또는 HWPX 파일만 업로드할 수 있습니다.")
  return extension
}
