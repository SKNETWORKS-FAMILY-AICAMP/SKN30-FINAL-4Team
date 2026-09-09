import { createClient } from "npm:@supabase/supabase-js@2"

export function serviceClient() {
  const url = Deno.env.get("SUPABASE_URL")
  const key = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")
  if (!url || !key) throw new Error("Supabase service configuration is missing")
  return createClient(url, key, { auth: { persistSession: false } })
}

export async function requireUserId(request: Request): Promise<string> {
  const header = request.headers.get("Authorization") ?? ""
  const token = header.startsWith("Bearer ") ? header.slice(7) : ""
  if (!token) throw new ApiError(401, "UNAUTHORIZED", "로그인이 필요합니다.")
  const { data, error } = await serviceClient().auth.getUser(token)
  if (error || !data.user) throw new ApiError(401, "UNAUTHORIZED", "로그인이 필요합니다.")
  return data.user.id
}

/** Additional shared secret for trusted worker callbacks. The caller still
 * presents the public anon JWT so the self-hosted Edge Runtime can route it. */
export function requireWorkerCallback(request: Request): void {
  const expected = Deno.env.get("ANALYSIS_WORKER_CALLBACK_TOKEN")
  const actual = request.headers.get("x-worker-callback-token")
  if (!expected || !actual || actual !== expected) {
    throw new ApiError(401, "WORKER_CALLBACK_UNAUTHORIZED", "worker 콜백 권한이 없습니다.")
  }
}

export class ApiError extends Error {
  constructor(readonly status: number, readonly code: string, message: string) {
    super(message)
  }
}

export function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
    },
  })
}

export function corsPreflight(): Response {
  return new Response("ok", {
    headers: {
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
      "Access-Control-Allow-Methods": "POST, OPTIONS",
    },
  })
}

export async function parseJson(request: Request): Promise<Record<string, unknown>> {
  try {
    const value: unknown = await request.json()
    if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error()
    return value as Record<string, unknown>
  } catch {
    throw new ApiError(400, "INVALID_JSON", "요청 형식이 올바르지 않습니다.")
  }
}

export function errorResponse(error: unknown): Response {
  if (error instanceof ApiError) return json(error.status, { code: error.code, message: error.message })
  console.error(error)
  return json(500, { code: "INTERNAL_ERROR", message: "요청을 처리하지 못했습니다." })
}
