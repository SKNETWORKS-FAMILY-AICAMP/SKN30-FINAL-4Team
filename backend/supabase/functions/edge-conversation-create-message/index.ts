import { ApiError, corsPreflight, errorResponse, json, parseJson, requireUserId, serviceClient } from "../_shared/supabase.ts"
import { conversationWorkerDispatcherFromEnvironment } from "../_shared/worker-http-dispatch.ts"

Deno.serve(async (request) => {
  if (request.method === "OPTIONS") return corsPreflight()
  if (request.method !== "POST") return json(405, { code: "METHOD_NOT_ALLOWED", message: "POST만 허용됩니다." })

  try {
    const userId = await requireUserId(request)
    const body = await parseJson(request)
    const analysisCaseId = typeof body.analysis_case_id === "string" ? body.analysis_case_id : ""
    const content = typeof body.content === "string" ? body.content.trim() : ""
    if (!analysisCaseId) throw new ApiError(400, "INVALID_ANALYSIS_CASE", "analysis_case_id가 필요합니다.")
    if (!content) throw new ApiError(400, "CHAT_CONTENT_REQUIRED", "질문 내용을 입력해 주세요.")

    const admin = serviceClient()
    const { data, error } = await admin.schema("workspace").rpc("prepare_conversation_messages", {
      p_user_id: userId,
      p_analysis_case_id: analysisCaseId,
      p_content: content,
    }).single()
    if (error || !data) {
      if (error?.message.includes("ANALYSIS_SESSION_EXPIRED")) {
        throw new ApiError(409, "ANALYSIS_SESSION_EXPIRED", "분석 세션이 만료되었습니다.")
      }
      if (error?.message.includes("CHAT_CONTENT_REQUIRED")) {
        throw new ApiError(400, "CHAT_CONTENT_REQUIRED", "질문 내용을 입력해 주세요.")
      }
      throw error ?? new Error("conversation reservation was not returned")
    }

    try {
      await conversationWorkerDispatcherFromEnvironment().dispatch({
        schema_version: "conversation_worker_job/v1",
        job_type: "conversation_reply",
        analysis_case_id: analysisCaseId,
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

    return json(201, {
      user_message_id: data.user_message_id,
      assistant_message_id: data.assistant_message_id,
      message_status: "generating",
    })
  } catch (error) {
    return errorResponse(error)
  }
})
