import type { JobResponse, StageProgress } from "@/types/trip";
import { mapBackendStage, STAGE_MAP, TOTAL_STAGES } from "@/constants/stages";

const API_BASE = import.meta.env.VITE_API_BASE || "/api";

import type { PublicGenerationDelivery } from "@/types/trip";

interface RawStreamEvent {
  status?: string;
  stage_progress?: { code: string; step: number; total: number };
  result_record_id?: string | number | null;
  error?: { code: string; message: string } | null;
  current_stage?: string | null;
  delivery?: PublicGenerationDelivery | null;
  generation_policy_version?: string | null;
}

interface StreamHandlers {
  onData: (data: JobResponse) => void;
  onError: (err: Event) => void;
  onInterrupted?: () => void;
}

function mapStatus(raw: string): JobResponse["status"] {
  switch (raw) {
    case "PENDING":
      return "QUEUED";
    case "RUNNING":
      return "RUNNING";
    case "SUCCESS":
      return "COMPLETED";
    case "FAILED":
    case "TIMEOUT":
    case "REJECTED":
      return "FAILED";
    default:
      return "RUNNING";
  }
}

function toJobResponse(
  jobId: string,
  raw: RawStreamEvent,
  eventType: string,
): JobResponse {
  const status =
    eventType === "complete"
      ? ("COMPLETED" as const)
      : eventType === "failed"
        ? ("FAILED" as const)
        : raw.status
          ? mapStatus(raw.status)
          : ("RUNNING" as const);

  let stageProgress: StageProgress | null = null;
  if (raw.stage_progress) {
    stageProgress = raw.stage_progress as StageProgress;
  } else if (raw.current_stage) {
    const code = mapBackendStage(raw.current_stage);
    stageProgress = { code, step: STAGE_MAP[code].step, total: TOTAL_STAGES };
  }

  const resultId =
    raw.result_record_id != null ? String(raw.result_record_id) : null;

  return {
    ok: true,
    job_id: jobId,
    status,
    stage_progress: stageProgress,
    result_record_id: resultId,
    error:
      status === "FAILED"
        ? (raw.error ?? { code: "GENERATION_FAILED", message: "生成失败" })
        : null,
    delivery: raw.delivery ?? null,
    generation_policy_version: raw.generation_policy_version ?? null,
  };
}

export function subscribeJobStream(
  jobId: string,
  handlers: StreamHandlers,
): () => void {
  const url = `${API_BASE}/trip/jobs/${encodeURIComponent(jobId)}/stream`;
  const es = new EventSource(url);

  function handle(eventType: string, e: MessageEvent) {
    let raw: RawStreamEvent = {};
    if (e.data) {
      try {
        raw = JSON.parse(e.data);
      } catch {
        handlers.onError(new Event("parse-error"));
        return;
      }
    }
    handlers.onData(toJobResponse(jobId, raw, eventType));
    if (eventType === "complete" || eventType === "failed") {
      es.close();
    }
  }

  es.addEventListener("progress", (e) => handle("progress", e as MessageEvent));
  es.addEventListener("complete", (e) => handle("complete", e as MessageEvent));
  es.addEventListener("failed", (e) => handle("failed", e as MessageEvent));
  es.addEventListener("interrupted", () => {
    es.close();
    handlers.onInterrupted?.();
  });
  es.onerror = (e) => handlers.onError(e);

  return () => es.close();
}
