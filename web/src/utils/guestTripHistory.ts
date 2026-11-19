import type { HistoryResponse, HistoryTripItem } from "@/types/auth";
import type {
  GenerationPolicyVersion,
  PublicGenerationDelivery,
  TripResult,
} from "@/types/trip";
import type { TripSubmitRequest } from "@/types/tripRequest";

export const GUEST_HISTORY_STORAGE_KEY = "yuntu_guest_trip_history_v1";
export const GUEST_PENDING_META_STORAGE_KEY = "yuntu_guest_trip_meta_v1";
export const GUEST_HISTORY_UPDATED_EVENT = "yuntu:guest-history-updated";

const MAX_HISTORY_ITEMS = 50;
const HISTORY_TTL_MS = 90 * 24 * 60 * 60 * 1000;

interface PendingTripMeta {
  job_id: string;
  city: string;
  days: number;
  created_at: string;
  retry_input?: {
    trip_request: TripSubmitRequest;
  } | null;
}

type TerminalStatus = HistoryTripItem["status"];

function canUseStorage(): boolean {
  return typeof window !== "undefined" && typeof window.localStorage !== "undefined";
}

function dispatchGuestHistoryUpdated(): void {
  if (typeof window === "undefined") return;
  try {
    window.dispatchEvent(new CustomEvent(GUEST_HISTORY_UPDATED_EVENT));
  } catch {
    /* ignore */
  }
}

function readJson<T>(key: string, fallback: T): T {
  if (!canUseStorage()) return fallback;
  try {
    const raw = window.localStorage.getItem(key);
    if (!raw) return fallback;
    return JSON.parse(raw) as T;
  } catch {
    return fallback;
  }
}

function writeJson(key: string, value: unknown): void {
  if (!canUseStorage()) return;
  try {
    window.localStorage.setItem(key, JSON.stringify(value));
  } catch {
    /* ignore quota / privacy mode errors */
  }
}

function toTimestamp(iso: string | null | undefined): number {
  if (!iso) return 0;
  const ts = Date.parse(iso);
  return Number.isFinite(ts) ? ts : 0;
}

function sortHistoryItems(items: HistoryTripItem[]): HistoryTripItem[] {
  return [...items].sort((a, b) => {
    const ta = toTimestamp(a.finished_at ?? a.created_at);
    const tb = toTimestamp(b.finished_at ?? b.created_at);
    return tb - ta;
  });
}

function isTerminalStatus(status: string): status is TerminalStatus {
  return (
    status === "SUCCESS" ||
    status === "FAILED" ||
    status === "TIMEOUT" ||
    status === "REJECTED"
  );
}

function normalizeGenerationPolicy(
  version: string | null | undefined,
): GenerationPolicyVersion | undefined {
  if (version === "legacy" || version === "v0.12") return version;
  return undefined;
}

function readPendingMetaMap(): Record<string, PendingTripMeta> {
  const raw = readJson<unknown>(GUEST_PENDING_META_STORAGE_KEY, {});
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return {};
  return raw as Record<string, PendingTripMeta>;
}

function writePendingMetaMap(map: Record<string, PendingTripMeta>): void {
  const entries = Object.entries(map).slice(-20);
  writeJson(GUEST_PENDING_META_STORAGE_KEY, Object.fromEntries(entries));
}

export function loadGuestHistoryTrips(): HistoryTripItem[] {
  const raw = readJson<unknown>(GUEST_HISTORY_STORAGE_KEY, []);
  if (!Array.isArray(raw)) return [];
  const valid = raw.filter(
    (item): item is HistoryTripItem =>
      Boolean(item) &&
      typeof item === "object" &&
      typeof (item as HistoryTripItem).trip_id === "string" &&
      isTerminalStatus(String((item as HistoryTripItem).status)),
  );
  return sortHistoryItems(valid);
}

function saveGuestHistoryTrips(items: HistoryTripItem[]): void {
  const byId = new Map<string, HistoryTripItem>();
  for (const item of items) {
    byId.set(item.trip_id, item);
  }
  const normalized = sortHistoryItems(Array.from(byId.values())).slice(
    0,
    MAX_HISTORY_ITEMS,
  );
  writeJson(GUEST_HISTORY_STORAGE_KEY, normalized);
  dispatchGuestHistoryUpdated();
}

export function recordGuestSubmittedTrip(
  jobId: string,
  formData: TripSubmitRequest,
): void {
  if (!jobId) return;
  const nowIso = new Date().toISOString();
  const map = readPendingMetaMap();
  map[jobId] = {
    job_id: jobId,
    city: formData.to_city?.trim() || "目的地",
    days: Number(formData.days) > 0 ? Number(formData.days) : 1,
    created_at: nowIso,
    retry_input: { trip_request: { ...formData } },
  };
  writePendingMetaMap(map);
}

export interface GuestJobStatusUpdate {
  status: string;
  result_record_id?: number | string | null;
  generation_policy_version?: string | null;
  delivery?: PublicGenerationDelivery | null;
  error_code?: string | null;
  error_message?: string | null;
  error?: { code: string; message: string; retryable?: boolean } | null;
}

export function recordGuestJobStatus(
  jobId: string,
  job: GuestJobStatusUpdate,
  fallbackMeta?: {
    destination?: string;
    days?: number;
    startedAt?: number;
  },
): void {
  if (!jobId) return;

  const rawStatus =
    job.status === "COMPLETED"
      ? "SUCCESS"
      : job.status;
  if (!isTerminalStatus(rawStatus)) return;

  const pendingMap = readPendingMetaMap();
  const meta = pendingMap[jobId];
  const existingItems = loadGuestHistoryTrips();
  const existing = existingItems.find(
    (item) => item.trip_id === jobId || item.job_id === jobId,
  );

  const createdAt =
    existing?.created_at ??
    meta?.created_at ??
    (fallbackMeta?.startedAt
      ? new Date(fallbackMeta.startedAt).toISOString()
      : new Date().toISOString());
  const createdMs = toTimestamp(createdAt) || Date.now();
  const finishedAt = existing?.finished_at ?? new Date().toISOString();
  const expiresAt =
    existing?.expires_from_history_at ??
    new Date(createdMs + HISTORY_TTL_MS).toISOString();

  const city =
    meta?.city ||
    fallbackMeta?.destination?.trim() ||
    existing?.city ||
    "行程";
  const days =
    meta?.days ??
    fallbackMeta?.days ??
    existing?.days ??
    0;

  const nextError =
    rawStatus === "SUCCESS"
      ? null
      : job.error
        ? {
            code: job.error.code,
            message: job.error.message,
            retryable: job.error.retryable ?? true,
          }
        : {
            code: job.error_code ?? "GENERATION_FAILED",
            message: job.error_message ?? "生成失败，请稍后重试",
            retryable: rawStatus !== "REJECTED",
          };

  const nextItem: HistoryTripItem = {
    trip_id: existing?.trip_id ?? jobId,
    job_id: jobId,
    status: rawStatus,
    city,
    days,
    result_record_id:
      job.result_record_id != null
        ? job.result_record_id
        : (existing?.result_record_id ?? null),
    generation_policy_version:
      normalizeGenerationPolicy(job.generation_policy_version) ??
      existing?.generation_policy_version,
    delivery: job.delivery ?? existing?.delivery ?? null,
    created_at: createdAt,
    finished_at: finishedAt,
    expires_from_history_at: expiresAt,
    retry_input: meta?.retry_input ?? existing?.retry_input ?? null,
    error: nextError,
  };

  const merged = existingItems.filter(
    (item) => item.trip_id !== nextItem.trip_id && item.job_id !== jobId,
  );
  merged.push(nextItem);
  saveGuestHistoryTrips(merged);

  if (meta) {
    delete pendingMap[jobId];
    writePendingMetaMap(pendingMap);
  }
}

export function recordGuestTripResult(
  result: TripResult,
  jobId?: string,
): void {
  if (!result || result.result_id == null) return;

  const normalizedJobId = jobId?.trim() ?? "";
  const existingItems = loadGuestHistoryTrips();
  const existing = existingItems.find(
    (item) =>
      (normalizedJobId &&
        (item.job_id === normalizedJobId || item.trip_id === normalizedJobId)) ||
      (item.result_record_id != null &&
        String(item.result_record_id) === String(result.result_id)),
  );

  const pendingMap = normalizedJobId ? readPendingMetaMap() : {};
  const meta = normalizedJobId ? pendingMap[normalizedJobId] : undefined;

  const nowIso = new Date().toISOString();
  const createdAt = existing?.created_at ?? meta?.created_at ?? nowIso;
  const createdMs = toTimestamp(createdAt) || Date.now();
  const tripId =
    existing?.trip_id ??
    (normalizedJobId || `result-${String(result.result_id)}`);

  const resolvedCity =
    result.city?.name?.trim() || meta?.city || existing?.city || "行程";
  const resolvedDays =
    Number(result.request?.days) > 0
      ? Number(result.request.days)
      : (meta?.days ?? existing?.days ?? 0);

  const nextItem: HistoryTripItem = {
    trip_id: tripId,
    job_id: existing?.job_id || normalizedJobId,
    status: "SUCCESS",
    city: resolvedCity,
    days: resolvedDays,
    result_record_id: result.result_id,
    generation_policy_version:
      result.generation_policy_version ??
      existing?.generation_policy_version,
    delivery: result.delivery ?? existing?.delivery ?? null,
    created_at: createdAt,
    finished_at: existing?.finished_at ?? nowIso,
    expires_from_history_at:
      existing?.expires_from_history_at ??
      new Date(createdMs + HISTORY_TTL_MS).toISOString(),
    retry_input: meta?.retry_input ?? existing?.retry_input ?? null,
    error: null,
  };

  const merged = existingItems.filter((item) => item.trip_id !== tripId);
  merged.push(nextItem);
  saveGuestHistoryTrips(merged);

  if (normalizedJobId && meta) {
    delete pendingMap[normalizedJobId];
    writePendingMetaMap(pendingMap);
  }
}

export function deleteGuestHistoryTrip(tripId: string): void {
  const existing = loadGuestHistoryTrips();
  const filtered = existing.filter(
    (item) => item.trip_id !== tripId && item.job_id !== tripId,
  );
  if (filtered.length !== existing.length) {
    saveGuestHistoryTrips(filtered);
  }
}

export function clearGuestHistoryTrips(): void {
  if (canUseStorage()) {
    try {
      window.localStorage.removeItem(GUEST_HISTORY_STORAGE_KEY);
      window.localStorage.removeItem(GUEST_PENDING_META_STORAGE_KEY);
    } catch {
      /* ignore */
    }
  }
  dispatchGuestHistoryUpdated();
}

export function queryGuestHistoryTrips(params?: {
  cursor?: string;
  limit?: number;
  status?: string;
}): HistoryResponse {
  let items = loadGuestHistoryTrips();
  if (params?.status) {
    const allowed = new Set(
      params.status
        .split(",")
        .map((s) => s.trim().toUpperCase())
        .filter(Boolean),
    );
    if (allowed.size > 0) {
      items = items.filter((item) => allowed.has(item.status));
    }
  }

  const offset = params?.cursor ? Math.max(0, Number.parseInt(params.cursor, 10) || 0) : 0;
  const limit = params?.limit && params.limit > 0 ? params.limit : 20;
  const page = items.slice(offset, offset + limit);
  const nextOffset = offset + page.length;
  const nextCursor = nextOffset < items.length ? String(nextOffset) : null;

  return {
    ok: true,
    items: page,
    next_cursor: nextCursor,
  };
}
