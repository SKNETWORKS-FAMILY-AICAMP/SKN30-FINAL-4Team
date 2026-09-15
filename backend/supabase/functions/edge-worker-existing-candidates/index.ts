import { ApiError, corsPreflight, errorResponse, json, parseJson, requireWorkerCallback, serviceClient } from "../_shared/supabase.ts"

/** Worker-only catalog. Its response shape stays stable when the internal
 * selection strategy changes from catalog paging to pgvector Top-K. */
Deno.serve(async (request) => {
  if (request.method === "OPTIONS") return corsPreflight()
  if (request.method !== "POST") return json(405, { code: "METHOD_NOT_ALLOWED", message: "POST만 허용됩니다." })
  try {
    requireWorkerCallback(request)
    const body = await parseJson(request)
    const limit = boundedInteger(body.limit, "limit", 100, 1, 100)
    const offset = boundedInteger(body.offset, "offset", 0, 0, 10_000)
    const admin = serviceClient()
    const { data, error } = await admin.schema("api")
      .rpc("worker_list_existing_candidates", { p_limit: limit, p_offset: offset })
    if (error) throw error
    return json(200, { candidates: data ?? [], next_offset: (data?.length ?? 0) === limit ? offset + limit : null })
  } catch (error) { return errorResponse(error) }
})

function boundedInteger(value: unknown, name: string, fallback: number, min: number, max: number): number {
  if (value === undefined) return fallback
  if (typeof value !== "number" || !Number.isInteger(value) || value < min || value > max) {
    throw new ApiError(400, "INVALID_PAGINATION", `${name} 값이 올바르지 않습니다.`)
  }
  return value
}
