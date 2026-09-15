/**
 * Stable boundary between a Supabase Edge Function and the heavy analysis
 * worker.  It is deliberately transport-agnostic: RunPod HTTP, a queue, and
 * an internal GPU service can all implement WorkerDispatcher later.
 */

export type AnalysisWorkerJob = {
  schema_version: "analysis_worker_job/v1"
  analysis_run_id: string
  user_id: string
  source: {
    bucket: string
    object_key: string
    /** Short-lived URL minted by Edge; the worker never receives Storage credentials. */
    download_url: string
    original_filename: string
    content_type: string | null
    size_bytes: number
  }
  callback: {
    ingest_request_profile_url: string
    ingest_comparison_result_url: string
    existing_candidates_url: string
    existing_profile_url: string
    callback_token: string
  }
  requested_at: string
}

export type WorkerDispatchResult = {
  accepted: true
  worker_job_id: string
}

export type ConversationWorkerJob = {
  schema_version: "conversation_worker_job/v1"
  job_type: "conversation_reply"
  analysis_case_id: string
  analysis_session_id: string
  user_id: string
  user_message_id: string
  assistant_message_id: string
  requested_at: string
}

export type WorkerJob = AnalysisWorkerJob | ConversationWorkerJob

export interface WorkerDispatcher {
  dispatch(job: WorkerJob): Promise<WorkerDispatchResult>
}

/**
 * Safe default until an actual worker transport is selected.  Callers must
 * mark the analysis run as failed or retryable; they must never pretend that
 * a job was queued when no worker received it.
 */
export class WorkerDispatchNotConfiguredError extends Error {
  constructor() {
    super("Analysis worker dispatch is not configured")
    this.name = "WorkerDispatchNotConfiguredError"
  }
}

export const unconfiguredWorkerDispatcher: WorkerDispatcher = {
  async dispatch(): Promise<WorkerDispatchResult> {
    throw new WorkerDispatchNotConfiguredError()
  },
}
