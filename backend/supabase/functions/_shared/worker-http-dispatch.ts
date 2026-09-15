import { type WorkerDispatcher, type WorkerDispatchResult, type WorkerJob } from "./worker-dispatch.ts"

/**
 * Sends only a job-acceptance request. It must not wait for Common IR, LLM,
 * OCR, or report generation; the trusted worker writes those outcomes
 * asynchronously and the browser observes state through Supabase Realtime.
 */
export class HttpWorkerDispatcher implements WorkerDispatcher {
  constructor(
    private readonly dispatchUrl: string,
    private readonly dispatchToken: string,
  ) {}

  async dispatch(job: WorkerJob): Promise<WorkerDispatchResult> {
    const response = await fetch(this.dispatchUrl, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${this.dispatchToken}`,
        "Content-Type": "application/json",
        // Retries of the Edge Function must enqueue the same analysis only once.
        "Idempotency-Key": job.schema_version === "analysis_worker_job/v1"
          ? job.analysis_run_id
          : job.assistant_message_id,
      },
      body: JSON.stringify(job),
      // A GPU job may run for minutes; accepting it must be fast. A timeout
      // leaves the Edge caller free to mark the run retryable instead of
      // holding an invocation open indefinitely.
      signal: AbortSignal.timeout(10_000),
    })

    if (response.status !== 202) {
      throw new Error(`Worker dispatch rejected with HTTP ${response.status}`)
    }

    const payload: unknown = await response.json()
    if (!isAcceptedWorkerResponse(payload)) {
      throw new Error("Worker dispatch response lacks worker_job_id")
    }

    return { accepted: true, worker_job_id: payload.worker_job_id }
  }
}

export function workerDispatcherFromEnvironment(): HttpWorkerDispatcher {
  return workerDispatcherFromEnvironmentPrefix("ANALYSIS_WORKER")
}

export function conversationWorkerDispatcherFromEnvironment(): HttpWorkerDispatcher {
  return workerDispatcherFromEnvironmentPrefix("CONVERSATION_WORKER")
}

function workerDispatcherFromEnvironmentPrefix(prefix: string): HttpWorkerDispatcher {
  const dispatchUrl = Deno.env.get(`${prefix}_DISPATCH_URL`)
  const dispatchToken = Deno.env.get(`${prefix}_DISPATCH_TOKEN`)
  if (!dispatchUrl || !dispatchToken) {
    throw new Error(`${prefix} dispatch environment is not configured`)
  }
  return new HttpWorkerDispatcher(dispatchUrl, dispatchToken)
}

function isAcceptedWorkerResponse(
  value: unknown,
): value is { worker_job_id: string } {
  return typeof value === "object" && value !== null &&
    "worker_job_id" in value &&
    typeof value.worker_job_id === "string" &&
    value.worker_job_id.length > 0
}
