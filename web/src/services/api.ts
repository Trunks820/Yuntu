import type { TripFormData } from "@/types/form";
import type { TripSubmitRequest } from "@/types/tripRequest";
import type {
  AsyncSubmitResponse,
  Artifact,
  ArtifactType,
  JobResponse,
  JobStatus,
  PlaceDetail,
  PublicGenerationDelivery,
  StageProgress,
  TripResult,
} from "@/types/trip";
import type { HistoryResponse } from "@/types/auth";

import { mapBackendStage, STAGE_MAP, TOTAL_STAGES } from "@/constants/stages";
import { getConversationId } from "@/utils/session";
import {
  queryGuestHistoryTrips,
  recordGuestJobStatus,
  recordGuestSubmittedTrip,
  recordGuestTripResult,
} from "@/utils/guestTripHistory";
import {
  mockSubmitTrip,
  mockPollJobStatus,
  mockFetchResult,
} from "./mock";

import { ApiRequestError } from "./errors";
import { isValidTripJobId } from "@/utils/tripJobId";
export { ApiRequestError };

const API_BASE = import.meta.env.VITE_API_BASE || "/api";
const USE_MOCK = import.meta.env.VITE_USE_MOCK === "true";

/**
 * 底层 HTTP 请求（开源免登录直连模式）
 */
async function request<T>(url: string, options?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${API_BASE}${url}`, {
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      ...options,
    });
  } catch (err: unknown) {
    if (
      (typeof DOMException !== "undefined" &&
        err instanceof DOMException &&
        err.name === "AbortError") ||
      (err instanceof Error && err.name === "AbortError")
    ) {
      throw new ApiRequestError("TIMEOUT", "请求超时，请检查网络后重试", 504);
    }
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

/* ---------- 本地行程记录 API（开源版浏览器 localStorage） ---------- */

export async function getHistoryTrips(params?: {
  cursor?: string;
  limit?: number;
  status?: string;
}): Promise<HistoryResponse> {
  return queryGuestHistoryTrips(params);
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
  stage_progress?: StageProgress | null;
  error_message: string | null;
  error_code: string | null;
  result_record_id: number | null;
  plan_count: number | null;
  /** v0.12: 生成策略版本 */
  generation_policy_version?: string | null;
  /** v0.12: BFF 公共交付投影 */
  delivery?: PublicGenerationDelivery | null;
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
  formData: TripSubmitRequest,
  requestId?: string,
): Promise<AsyncSubmitResponse> {
  if (USE_MOCK) {
    const mockData: TripFormData = {
      to_city: formData.to_city,
      start_date: formData.start_date || "",
      end_date: formData.end_date || "",
      days: formData.days,
      people_count: formData.people_count,
      preferences: formData.preferences || [],
      avoid: formData.avoid || [],
      notes: formData.notes || "",
      budget: formData.budget,
      must_include: formData.must_include,
      commute_mode: formData.commute_mode,
      daily_start: formData.daily_start,
      daily_end: formData.daily_end,
      from_city: formData.from_city,
      accommodation: formData.accommodation,
    };
    const res = await mockSubmitTrip(mockData);
    if (res.job_id) {
      recordGuestSubmittedTrip(res.job_id, formData);
    }
    return res;
  }

  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), 30000);

  try {
    const raw = await request<RawSubmitResponse>("/trip/async", {
      method: "POST",
      signal: controller.signal,
      body: JSON.stringify({
        trip_request: formData,
        source: "web",
        conversation_id: getConversationId(),
        request_id: requestId || `web-${crypto.randomUUID()}`,
      }),
    });
    if (!isValidTripJobId(raw.job_id)) {
      throw new ApiRequestError("BAD_RESPONSE", "未收到有效任务编号，请稍后重试", 502);
    }
    recordGuestSubmittedTrip(raw.job_id, formData);
    return { ok: raw.ok, job_id: raw.job_id };
  } catch (err: unknown) {
    if (
      (err instanceof ApiRequestError && err.code === "TIMEOUT") ||
      (typeof DOMException !== "undefined" &&
        err instanceof DOMException &&
        err.name === "AbortError") ||
      (err instanceof Error && err.name === "AbortError")
    ) {
      throw new ApiRequestError("TIMEOUT", "提交超时，提交结果待确认", 504);
    }
    throw err;
  } finally {
    clearTimeout(timeoutId);
  }
}


export async function pollJobStatus(
  jobId: string,
  options?: { signal?: AbortSignal },
): Promise<JobResponse> {
  if (USE_MOCK) {
    const mockJob = await mockPollJobStatus(jobId);
    recordGuestJobStatus(jobId, mockJob);
    return mockJob;
  }

  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), 30000);

  if (options?.signal) {
    if (options.signal.aborted) {
      controller.abort();
    } else {
      options.signal.addEventListener("abort", () => controller.abort(), {
        once: true,
      });
    }
  }

  try {
    const raw = await request<RawJobStatus>(`/trip/jobs/${jobId}`, {
      signal: controller.signal,
    });
    recordGuestJobStatus(jobId, raw);
    const status = mapStatus(raw.status);
    let stageProgress: StageProgress | null = null;
    if (
      raw.stage_progress &&
      typeof raw.stage_progress.step === "number" &&
      typeof raw.stage_progress.total === "number"
    ) {
      stageProgress = raw.stage_progress;
    } else if (raw.current_stage) {
      const code = mapBackendStage(raw.current_stage);
      stageProgress = {
        code,
        step: STAGE_MAP[code].step,
        total: TOTAL_STAGES,
      };
    }

    return {
      ok: true,
      job_id: raw.job_id,
      status,
      stage_progress: stageProgress,
      result_record_id:
        raw.result_record_id != null ? String(raw.result_record_id) : null,
      error:
        status === "FAILED"
          ? {
              code: raw.error_code ?? "GENERATION_FAILED",
              message: raw.error_message ?? "生成失败，请稍后重试",
            }
          : null,
      delivery: raw.delivery ?? null,
      generation_policy_version: raw.generation_policy_version ?? null,
    };
  } finally {
    clearTimeout(timeoutId);
  }
}


export async function fetchResult(
  resultId: string,
  jobId: string,
): Promise<TripResult> {
  if (USE_MOCK) {
    const mockResult = await mockFetchResult(resultId);
    recordGuestTripResult(mockResult, jobId);
    return mockResult;
  }
  const qs = jobId ? `?job_id=${encodeURIComponent(jobId)}` : "";
  const result = await request<TripResult>(`/trip/results/${resultId}${qs}`);
  recordGuestTripResult(result, jobId);
  return result;
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

/**
 * Fetch destinations from BFF API
 * Returns list of available destinations with coordinates and metadata
 */
export { fetchDestinations } from "@/api/destinations";
