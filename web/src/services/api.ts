import type { TripFormData } from "@/types/form";
import type { TripSubmitRequest } from "@/types/tripRequest";
import type {
  AsyncSubmitResponse,
  Artifact,
  ArtifactType,
  JobResponse,
  JobStatus,
  PlaceDetail,
  TripResult,
} from "@/types/trip";
import { mapBackendStage, STAGE_MAP, TOTAL_STAGES } from "@/constants/stages";
import { getConversationId } from "@/utils/session";
import {
  mockSubmitTrip,
  mockPollJobStatus,
  mockFetchResult,
} from "./mock";

import { ApiRequestError } from "./errors";
export { ApiRequestError };

const API_BASE = import.meta.env.VITE_API_BASE || "/api";
const USE_MOCK = import.meta.env.VITE_USE_MOCK === "true";

async function request<T>(url: string, options?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${API_BASE}${url}`, {
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      ...options,
    });
  } catch {
    throw new ApiRequestError("NETWORK_ERROR", "网络连接失败，请检查网络", 0);
  }

  let data: unknown;
  try {
    data = await res.json();
  } catch {
    throw new ApiRequestError("BAD_RESPONSE", "服务返回异常", res.status);
  }

  if (!res.ok) {
    const detail = (data as { detail?: unknown }).detail;
    if (detail && typeof detail === "object") {
      const d = detail as { code?: string; message?: string };
      throw new ApiRequestError(
        d.code ?? "HTTP_ERROR",
        d.message ?? "请求失败",
        res.status,
      );
    }
    const flat = data as { error?: { code?: string; message?: string } };
    if (flat.error) {
      throw new ApiRequestError(
        flat.error.code ?? "HTTP_ERROR",
        flat.error.message ?? "请求失败",
        res.status,
      );
    }
    const msg = typeof detail === "string" ? detail : "请求失败";
    throw new ApiRequestError("HTTP_ERROR", msg, res.status);
  }

  return data as T;
}

/* ---------- 原始响应类型 ---------- */

interface RawSubmitResponse {
  ok: boolean;
  job_id: string;
  trip_id?: string;
  status?: string;
  current_stage?: string;
}

interface RawJobStatus {
  ok: boolean;
  job_id: string;
  status: string; // PENDING | RUNNING | SUCCESS | FAILED | TIMEOUT | REJECTED
  current_stage: string | null;
  error_message: string | null;
  error_code: string | null;
  result_record_id: number | null;
  plan_count: number | null;
}

function mapStatus(raw: string): JobStatus {
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

/* ---------- 提交与任务 API ---------- */

export async function submitTrip(
  formData: TripFormData | TripSubmitRequest,
  requestId?: string,
): Promise<AsyncSubmitResponse> {
  if (USE_MOCK) return mockSubmitTrip(formData);

  const raw = await request<RawSubmitResponse>("/trip/async", {
    method: "POST",
    body: JSON.stringify({
      trip_request: formData,
      request_id: requestId || `web-${crypto.randomUUID()}`,
      source: "web",
      conversation_id: getConversationId(),
    }),
  });
  return { ok: raw.ok, job_id: raw.job_id };
}

export async function pollJobStatus(jobId: string): Promise<JobResponse> {
  if (USE_MOCK) return mockPollJobStatus(jobId);

  const raw = await request<RawJobStatus>(`/trip/jobs/${jobId}`);
  const status = mapStatus(raw.status);
  const code = mapBackendStage(raw.current_stage);

  return {
    ok: true,
    job_id: raw.job_id,
    status,
    stage_progress: {
      code,
      step: STAGE_MAP[code].step,
      total: TOTAL_STAGES,
    },
    result_record_id:
      raw.result_record_id != null ? String(raw.result_record_id) : null,
    error:
      status === "FAILED"
        ? {
            code: raw.error_code ?? "GENERATION_FAILED",
            message: raw.error_message ?? "生成失败，请稍后重试",
          }
        : null,
  };
}

export async function fetchResult(
  resultId: string,
  jobId: string,
): Promise<TripResult> {
  if (USE_MOCK) return mockFetchResult(resultId);
  const qs = jobId ? `?job_id=${encodeURIComponent(jobId)}` : "";
  return request<TripResult>(`/trip/results/${resultId}${qs}`);
}

export async function fetchPlaceDetail(placeId: number): Promise<PlaceDetail> {
  return request<PlaceDetail>(`/trip/places/${placeId}`);
}

export interface HotPlace {
  place_id: number;
  name: string;
  place_type: string;
  mention_count: number;
}

export async function fetchHotPlaces(
  city: string,
  limit = 12,
): Promise<HotPlace[]> {
  const raw = await request<{ ok: boolean; places?: HotPlace[] }>(
    `/trip/places?city=${encodeURIComponent(city)}&limit=${limit}`,
  );
  return raw.places ?? [];
}

/* ---------- Artifacts PDF / Share Image ---------- */

export function createArtifact(
  recordId: string,
  type: ArtifactType,
): Promise<Artifact> {
  return request<Artifact>(`/trip/results/${recordId}/artifacts/${type}`, {
    method: "POST",
  });
}

export function getArtifact(
  recordId: string,
  type: ArtifactType,
): Promise<Artifact> {
  return request<Artifact>(`/trip/results/${recordId}/artifacts/${type}`);
}

/**
 * 校验并解析同源产物下载路径，防止重复拼接 API_BASE (/api) 及跨域/外部非法 URL 注入。
 */
export function resolveSameOriginDownloadPath(downloadUrl: string): string {
  const trimmed = (downloadUrl || "").trim();

  // 1. 拒绝外部 URL、Scheme 或 协议相对路径 (http://, https://, //, javascript:)
  if (
    /^(?:https?:)?\/\//i.test(trimmed) ||
    /^[a-zA-Z][a-zA-Z0-9+.-]*:/.test(trimmed)
  ) {
    throw new ApiRequestError(
      "INVALID_DOWNLOAD_URL",
      "非法或越权下载路径",
      400,
    );
  }

  // 2. 必须为绝对/相对根路径（以 / 开头）
  if (!trimmed.startsWith("/")) {
    throw new ApiRequestError("INVALID_DOWNLOAD_URL", "下载路径格式错误", 400);
  }

  // 3. 防重复拼接：若 downloadUrl 已以 API_BASE (如 /api/) 开头或完全等于 API_BASE，直接返回
  if (trimmed === API_BASE || trimmed.startsWith(`${API_BASE}/`)) {
    return trimmed;
  }

  const cleanBase = API_BASE.replace(/\/+$/, "");
  const cleanPath = trimmed.replace(/^\/+/, "");
  return `${cleanBase}/${cleanPath}`;
}

export async function fetchArtifactBlob(
  downloadUrl: string,
  options?: { signal?: AbortSignal },
): Promise<Blob> {
  const targetPath = resolveSameOriginDownloadPath(downloadUrl);

  let res: Response;
  try {
    res = await fetch(targetPath, {
      credentials: "include",
      signal: options?.signal,
    });
  } catch (err: unknown) {
    if (
      err &&
      typeof err === "object" &&
      "name" in err &&
      (err as { name: string }).name === "AbortError"
    ) {
      throw err;
    }
    throw new ApiRequestError("NETWORK_ERROR", "网络连接失败，请检查网络", 0);
  }
  if (!res.ok) {
    throw new ApiRequestError(
      "DOWNLOAD_FAILED",
      "下载失败，请重试",
      res.status,
    );
  }
  return res.blob();
}

/* ---------- Destinations API ---------- */
export { fetchDestinations } from "@/api/destinations";
