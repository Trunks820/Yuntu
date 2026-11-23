import { useCallback, useEffect, useRef, useState } from "react";
import { ApiRequestError, fetchPlaceDetail } from "@/services/api";
import type { PlaceDetail, TripPlace } from "@/types/trip";

export type PlaceDetailEntry =
  | { status: "loading" }
  | { status: "ready"; data: PlaceDetail }
  // 后端明确说明该地点不支持详情或不存在：不按故障处理，也不再重复请求
  | { status: "unsupported" }
  | { status: "error" };

function toFailedDetailEntry(err: unknown): PlaceDetailEntry {
  return err instanceof ApiRequestError &&
    (err.code === "PLACE_UNSUPPORTED" || err.code === "PLACE_NOT_FOUND")
    ? { status: "unsupported" }
    : { status: "error" };
}

/**
 * 手机端旅行册按天批量拉取各站详情（用于展示各站小实拍）：
 * 规则与 SpreadBookPage 对齐——页面内缓存，同一地点（已成功/加载中/失败）只取一次。
 */
export function usePlaceDetails(
  places: readonly TripPlace[],
): Record<number, PlaceDetailEntry> {
  const [placeDetails, setPlaceDetails] = useState<
    Record<number, PlaceDetailEntry>
  >({});
  const detailCacheRef = useRef<Map<number, PlaceDetail>>(new Map());
  const attemptedPlaceIdsRef = useRef<Set<number>>(new Set());
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const ensureLoaded = useCallback((targetPlaces: readonly TripPlace[]) => {
    const updates: Record<number, PlaceDetailEntry> = {};
    const idsToFetch: number[] = [];
    const seen = new Set<number>();

    for (const place of targetPlaces) {
      const id = place.place_id;
      if (seen.has(id)) continue;
      seen.add(id);

      const cachedDetail = detailCacheRef.current.get(id);
      if (cachedDetail) {
        updates[id] = { status: "ready", data: cachedDetail };
        continue;
      }

      if (attemptedPlaceIdsRef.current.has(id)) {
        continue;
      }
      attemptedPlaceIdsRef.current.add(id);
      updates[id] = { status: "loading" };
      idsToFetch.push(id);
    }

    if (Object.keys(updates).length > 0) {
      setPlaceDetails((prev) => {
        let changed = false;
        const next = { ...prev };
        for (const [key, entry] of Object.entries(updates)) {
          const numId = Number(key);
          if (entry.status === "ready" && prev[numId]?.status === "ready") {
            continue;
          }
          if (!prev[numId] || entry.status === "ready") {
            next[numId] = entry;
            changed = true;
          }
        }
        return changed ? next : prev;
      });
    }

    for (const id of idsToFetch) {
      let request: Promise<PlaceDetail | null | undefined>;
      try {
        request = Promise.resolve(fetchPlaceDetail(id));
      } catch (err: unknown) {
        request = Promise.reject(err);
      }
      request
        .then((detail) => {
          if (!detail) {
            if (!mountedRef.current) return;
            setPlaceDetails((prev) => ({
              ...prev,
              [id]: { status: "unsupported" },
            }));
            return;
          }
          detailCacheRef.current.set(id, detail);
          if (!mountedRef.current) return;
          setPlaceDetails((prev) => ({
            ...prev,
            [id]: { status: "ready", data: detail },
          }));
        })
        .catch((err: unknown) => {
          if (!mountedRef.current) return;
          setPlaceDetails((prev) => ({
            ...prev,
            [id]: toFailedDetailEntry(err),
          }));
        });
    }
  }, []);

  useEffect(() => {
    if (places.length === 0) return;
    ensureLoaded(places);
  }, [places, ensureLoaded]);

  return placeDetails;
}
