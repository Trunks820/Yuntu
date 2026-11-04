import { ApiRequestError } from "@/services/errors";
import { fetchDestinations } from "@/api/destinations";
import type { PlaceGalleryImage } from "@/types/trip";

const API_BASE = import.meta.env.VITE_API_BASE || "/api";

export type SelectablePoi = {
  place_id: number;
  name: string;
  place_type: string;
  district: string | null;
};

export type PoiCatalogResponse = {
  ok: true;
  city: string;
  places: SelectablePoi[];
  next_after_id: number | null;
};

export type PoiSelectionResponse = {
  ok: true;
  city: string;
  items: Array<
    | { place_id: number; status: "available"; place: SelectablePoi }
    | { place_id: number; status: "unavailable" }
  >;
};

async function request<T>(url: string, options?: RequestInit, signal?: AbortSignal): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${API_BASE}${url}`, {
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      signal,
      ...options,
    });
  } catch (err: unknown) {
    if (err && typeof err === 'object' && 'name' in err && (err as {name: string}).name === 'AbortError') {
      throw err;
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

export async function fetchPoiCatalog(
  city: string,
  q = "",
  afterId = 0,
  limit = 20,
  signal?: AbortSignal
): Promise<PoiCatalogResponse> {
  const query = new URLSearchParams();
  query.set("city", city);
  if (q) query.set("q", q);
  query.set("after_id", String(afterId));
  query.set("limit", String(limit));
  
  return request<PoiCatalogResponse>(`/trip/poi-catalog?${query.toString()}`, undefined, signal);
}

export type PoiInspirationCity = { city: string };

export type PoiInspirationPlace = SelectablePoi & {
  city?: string;
  gallery?: PlaceGalleryImage[];
};

export async function fetchPoiInspirationCities(
  signal?: AbortSignal,
): Promise<{ cities: PoiInspirationCity[] }> {
  const dest = await fetchDestinations();
  if (signal?.aborted) {
    throw new DOMException("Aborted", "AbortError");
  }
  const cities = (dest.destinations ?? [])
    .map((item) => item.name?.trim())
    .filter((name): name is string => Boolean(name))
    .map((city) => ({ city }));
  return { cities };
}

export async function fetchPoiInspiration(
  city: string,
  afterId = 0,
  limit = 8,
  signal?: AbortSignal,
): Promise<{ places: PoiInspirationPlace[]; next_after_id: number | null }> {
  const catalog = await fetchPoiCatalog(city, "", afterId, limit, signal);
  return {
    places: catalog.places.map((place) => ({ ...place, city, gallery: [] })),
    next_after_id: catalog.next_after_id,
  };
}

export async function checkPoiSelection(
  city: string,
  placeIds: number[],
  signal?: AbortSignal
): Promise<PoiSelectionResponse> {
  const query = new URLSearchParams();
  query.set("city", city);
  placeIds.forEach(id => query.append("place_ids", String(id)));
  
  return request<PoiSelectionResponse>(`/trip/poi-catalog/selection?${query.toString()}`, undefined, signal);
}
