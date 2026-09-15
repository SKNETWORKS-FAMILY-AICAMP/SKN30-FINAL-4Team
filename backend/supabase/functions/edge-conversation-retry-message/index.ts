import { ApiError, corsPreflight, errorResponse, json, parseJson, requireUserId, serviceClient } from "../_shared/supabase.ts"
import { conversationWorkerDispatcherFromEnvironment } from "../_shared/worker-http-dispatch.ts"

Deno.serve(async (request) => {
  if (request.method === "OPTIONS") return corsPreflight()
  if (request.method !== "POST") return json(405, { code: "METHOD_NOT_ALLOWED", message: "POST만 허용됩니다." })

  try {
    const userId = await requireUserId(request)
    const body = await parseJson(request)
    const assistantMessageId = typeof body.assistant_message_id === "string" ? body.assistant_message_id : ""
    if (!assistantMessageId) throw new ApiError(400, "INVALID_ASSISTANT_MESSAGE", "assistant_message_id가 필요합니다.")

    const admin = serviceClient()
    const { data, error } = await admin.schema("workspace").rpc("retry_conversation_message", {
      p_user_id: userId,
      p_assistant_message_id: assistantMessageId,
    }).single()
    if (error || !data) {
      const message = error?.message ?? ""
      if (message.includes("CHAT_RETRY_COOLDOWN")) {
        return json(429, { code: "CHAT_RETRY_COOLDOWN", message: "5초 후 다시 시도해 주세요.", retry_after_seconds: 5 })
      }
      if (message.includes("CHAT_RETRY_EXHAUSTED")) {
        throw new ApiError(409, "CHAT_RETRY_EXHAUSTED", "재시도 횟수를 모두 사용했습니다.")
      }
      if (message.includes("ANALYSIS_SESSION_EXPIRED")) {
        throw new ApiError(409, "ANALYSIS_SESSION_EXPIRED", "분석 세션이 만료되었습니다.")
      }
      if (message.includes("CHAT_MESSAGE_NOT_RETRYABLE")) {
        throw new ApiError(409, "CHAT_MESSAGE_NOT_RETRYABLE", "재시도할 수 없는 답변입니다.")
      }
      throw error ?? new Error("conversation retry reservation was not returned")
    }

    try {
      await conversationWorkerDispatcherFromEnvironment().dispatch({
        schema_version: "conversation_worker_job/v1",
        job_type: "conversation_reply",
        analysis_case_id: data.analysis_case_id,
        analysis_session_id: data.analysis_session_id,
        user_id: userId,
        user_message_id: data.user_message_id,
        assistant_message_id: data.assistant_message_id,
        requested_at: new Date().toISOString(),
      })
    } catch (dispatchError) {
      await admin.schema("result").from("conversation_message")
        .update({ status: "failed", error_code: "WORKER_UNAVAILABLE", error_message: "답변 생성에 실패했습니다. 다시 시도해 주세요." })
        .eq("message_pk", data.assistant_message_id)
      console.error(dispatchError)
      throw new ApiError(503, "WORKER_UNAVAILABLE", "답변 생성에 실패했습니다. 다시 시도해 주세요.")
    }

    return json(200, {
      assistant_message_id: data.assistant_message_id,
      message_status: "generating",
      retry_count: data.retry_count,
    })
  } catch (error) {
    return errorResponse(error)
  }
})
