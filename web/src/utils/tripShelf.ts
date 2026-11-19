import { getHistoryTrips } from "@/services/api";
import type { ActiveTrip, HistoryTripItem } from "@/types/auth";
import type { Destination } from "@/types/destination";
import type { StageProgress } from "@/types/trip";
import {
  CITY_NAME_TO_FOLDER,
  getCityGalleryItems,
  getCityPhotoCredit,
} from "@/utils/cityPhotos";

export interface TripShelfSummary {
  items: HistoryTripItem[];
  totalCount: number;
  readableCount: number;
  latestReadableTrip: HistoryTripItem | null;
}

export interface TripPhoto {
  url: string;
  credit: string;
}

export const DESTINATIONS_CACHE_KEY = "yuntu:destinations:v1";

const CN_DIGITS = "〇一二三四五六七八九";
const CN_MONTHS = [
  "一",
  "二",
  "三",
  "四",
  "五",
  "六",
  "七",
  "八",
  "九",
  "十",
  "十一",
  "十二",
] as const;

export function cleanTripCityName(city: string): string {
  return city.trim().replace(/市$/, "");
}

export function readCachedDestinations(): Destination[] {
  try {
    const raw = sessionStorage.getItem(DESTINATIONS_CACHE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw) as { destinations?: unknown };
    return Array.isArray(parsed?.destinations)
      ? (parsed.destinations as Destination[])
      : [];
  } catch {
    return [];
  }
}

export function cacheDestinations(data: unknown): void {
  try {
    sessionStorage.setItem(DESTINATIONS_CACHE_KEY, JSON.stringify(data));
  } catch {
    /* ignore */
  }
}

export function buildTripResultHref(
  trip: Pick<HistoryTripItem, "result_record_id" | "job_id">,
): string | null {
  if (trip.result_record_id === null || trip.result_record_id === undefined) {
    return null;
  }
  const recordId = String(trip.result_record_id).trim();
  if (!recordId) return null;
  const jobId = trip.job_id ? String(trip.job_id).trim() : "";
  return `/result/${encodeURIComponent(recordId)}${
    jobId ? `?job_id=${encodeURIComponent(jobId)}` : ""
  }`;
}

export function pickNonGalleryDestinationPhoto(
  city: string,
  destination: Destination | undefined,
): TripPhoto | null {
  if (!destination) return null;
  const cleanCity = cleanTripCityName(city);
  const isUsableUrl = (url: string | undefined): url is string =>
    Boolean(url && url.trim() && !url.includes("city-placeholder.svg"));

  if (isUsableUrl(destination.coverImageUrl)) {
    return {
      url: destination.coverImageUrl,
      credit: getCityPhotoCredit(destination.coverImageUrl, { city: cleanCity }),
    };
  }

  if (isUsableUrl(destination.backgroundImageUrl)) {
    return {
      url: destination.backgroundImageUrl,
      credit: getCityPhotoCredit(destination.backgroundImageUrl, { city: cleanCity }),
    };
  }

  const firstRef = destination.referenceImages?.find((img) =>
    isUsableUrl(img.url),
  );
  if (firstRef) {
    return {
      url: firstRef.url,
      credit: getCityPhotoCredit(firstRef.url, {
        city: cleanCity,
        placeName: firstRef.placeName,
      }),
    };
  }

  return null;
}

/**
 * 为指定城市与轮换序号解析实景图及署名；
 * 16 图库城市从静态图库轮换，其余城市从目的地目录实景取图，绝不拿别的城顶替。
 */
export function resolveCityTripPhoto(
  city: string,
  nth: number,
  destinations: readonly Destination[],
): TripPhoto | null {
  const cleanCity = cleanTripCityName(city);
  if (!cleanCity) return null;

  if (CITY_NAME_TO_FOLDER[cleanCity]) {
    const items = getCityGalleryItems(cleanCity);
    if (items.length === 0) return null;
    const index = ((nth % items.length) + items.length) % items.length;
    const picked = items[index];
    return { url: picked.url, credit: picked.credit };
  }

  const matchedDest = destinations.find(
    (d) => cleanTripCityName(d.name) === cleanCity,
  );
  return pickNonGalleryDestinationPhoto(cleanCity, matchedDest);
}

/**
 * 藏书票（/profile）与照片墙（/history）共用的单册选图函数，
 * 确保同一册行程在两处显示完全相同的照片与署名。
 */
export function resolveTripPhoto(
  trip: Pick<HistoryTripItem, "trip_id" | "city">,
  cityOrder: ReadonlyMap<string, number>,
  destinations: readonly Destination[],
): TripPhoto | null {
  const nth = cityOrder.get(trip.trip_id) ?? 0;
  return resolveCityTripPhoto(trip.city, nth, destinations);
}

export function hasTripStageProgress(
  progress: StageProgress | null | undefined,
): progress is StageProgress {
  return (
    progress != null &&
    typeof progress.step === "number" &&
    Number.isFinite(progress.step) &&
    typeof progress.total === "number" &&
    Number.isFinite(progress.total) &&
    progress.total > 0
  );
}

export function formatChineseMonthHeading(dateInput: Date | string): string {
  const date = typeof dateInput === "string" ? new Date(dateInput) : dateInput;
  if (Number.isNaN(date.getTime())) {
    return "更早之前";
  }
  const yearStr = String(date.getFullYear()).replace(
    /\d/g,
    (digit) => CN_DIGITS[Number(digit)] ?? digit,
  );
  const monthStr = CN_MONTHS[date.getMonth()] ?? String(date.getMonth() + 1);
  return `${yearStr}年 · ${monthStr}月`;
}

export function formatTripMonthDay(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) {
    return "";
  }
  return `${date.getMonth() + 1}月${date.getDate()}日`;
}

/**
 * 书架中“可以翻阅”的一册：已生成成功且具备 result_record_id。
 */
export function isReadableTrip(
  item: Pick<HistoryTripItem, "status" | "result_record_id">,
): boolean {
  if (item.status !== "SUCCESS") return false;
  if (item.result_record_id === null || item.result_record_id === undefined) {
    return false;
  }
  return String(item.result_record_id).trim().length > 0;
}

/**
 * 统一计算书架总册数、可翻阅册数与最近一册可翻阅行程。
 * - `active_trip` 若不在分页列表中，计入总册数。
 * - 按传入列表顺序（GET /me/trips 按创建时间倒序）取第一本可翻阅行程为最近一册。
 * - “我的”（/profile）藏书票与“我的行程”（/history）页头共用同一口径。
 */
export function summarizeTripShelf(
  items: readonly HistoryTripItem[],
  activeTrip?: ActiveTrip | null,
): TripShelfSummary {
  const seenTripIds = new Set<string>();
  const dedupedItems: HistoryTripItem[] = [];

  for (const item of items) {
    if (!item || !item.trip_id || seenTripIds.has(item.trip_id)) {
      continue;
    }
    seenTripIds.add(item.trip_id);
    dedupedItems.push(item);
  }

  const hasActiveOutsideList = Boolean(
    activeTrip?.trip_id && !seenTripIds.has(activeTrip.trip_id),
  );
  const totalCount = dedupedItems.length + (hasActiveOutsideList ? 1 : 0);
  const readableItems = dedupedItems.filter(isReadableTrip);

  return {
    items: dedupedItems,
    totalCount,
    readableCount: readableItems.length,
    latestReadableTrip: readableItems[0] ?? null,
  };
}

/**
 * 计算每册行程在所属城市中的出现序号（从 0 起算），供同城多篇轮换取图。
 */
export function buildTripCityOrder(
  items: readonly Pick<HistoryTripItem, "trip_id" | "city">[],
): Map<string, number> {
  const countsByCity = new Map<string, number>();
  const order = new Map<string, number>();

  for (const item of items) {
    const cleanCity = cleanTripCityName(item.city);
    const nth = countsByCity.get(cleanCity) ?? 0;
    order.set(item.trip_id, nth);
    countsByCity.set(cleanCity, nth + 1);
  }

  return order;
}

/**
 * 沿 GET /api/me/trips 的 next_cursor 翻完所有分页，返回去重后的全部历史条目。
 */
export async function fetchAllHistoryTrips(options?: {
  pageSize?: number;
  status?: string;
  signal?: AbortSignal;
}): Promise<HistoryTripItem[]> {
  const pageSize = Math.min(50, Math.max(1, options?.pageSize ?? 50));
  const status = options?.status;
  const signal = options?.signal;

  const collected: HistoryTripItem[] = [];
  const seenTripIds = new Set<string>();
  const seenCursors = new Set<string>();
  let cursor: string | undefined;

  while (true) {
    if (signal?.aborted) {
      throw new DOMException("Aborted", "AbortError");
    }

    const res = await getHistoryTrips({
      cursor,
      limit: pageSize,
      ...(status ? { status } : {}),
    });

    if (res?.items?.length) {
      for (const item of res.items) {
        if (!item?.trip_id || seenTripIds.has(item.trip_id)) continue;
        seenTripIds.add(item.trip_id);
        collected.push(item);
      }
    }

    const next = res?.next_cursor ?? null;
    if (!next || seenCursors.has(next)) {
      break;
    }
    seenCursors.add(next);
    cursor = next;
  }

  return collected;
}
