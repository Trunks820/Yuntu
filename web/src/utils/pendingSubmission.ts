import type { TripFormData } from "@/types/form";
import type { TripSubmitRequest } from "@/types/tripRequest";

const PENDING_SUBMISSION_KEY = "yuntu_pending_submission";

export interface PendingSubmission {
  request_id: string;
  trip_request: TripFormData;
  fingerprint: string;
  created_at: number;
}

export function generateFingerprint(formData: TripFormData | TripSubmitRequest): string {
  const normalized = {
    accommodation: formData.accommodation
      ? {
          latitude: formData.accommodation.latitude ?? null,
          longitude: formData.accommodation.longitude ?? null,
          name: formData.accommodation.name ? formData.accommodation.name.trim() : "",
          place_id: formData.accommodation.place_id ?? null,
        }
      : null,
    avoid: formData.avoid ? formData.avoid.map((s) => s.trim()) : [],
    commute_mode: formData.commute_mode ?? "driving",
    daily_end: formData.daily_end ?? "",
    daily_start: formData.daily_start ?? "",
    days: formData.days,
    end_date: formData.end_date,
    from_city: formData.from_city ? formData.from_city.trim() : "",
    must_include: formData.must_include
      ? formData.must_include.map((m) => ({
          name: m.name ? m.name.trim() : "",
          place_id: m.place_id ?? null,
        }))
      : [],
    notes: formData.notes ? formData.notes.trim() : "",
    people_count: formData.people_count,
    preferences: formData.preferences ? formData.preferences.map((s) => s.trim()) : [],
    start_date: formData.start_date,
    to_city: formData.to_city ? formData.to_city.trim() : "",
  };
  return JSON.stringify(normalized);
}

export function getPendingSubmission(): PendingSubmission | null {
  try {
    const raw = sessionStorage.getItem(PENDING_SUBMISSION_KEY);
    if (!raw) return null;
    return JSON.parse(raw) as PendingSubmission;
  } catch {
    sessionStorage.removeItem(PENDING_SUBMISSION_KEY);
    return null;
  }
}

export function savePendingSubmission(formData: TripFormData): PendingSubmission {
  const currentFingerprint = generateFingerprint(formData);
  const existing = getPendingSubmission();

  let requestId: string;
  if (existing && existing.fingerprint === currentFingerprint && existing.request_id) {
    // 内容完全一致：复用已有的 request_id
    requestId = existing.request_id;
  } else {
    // 内容发生变化或无历史暂存：生成新的 request_id
    requestId = `web-${crypto.randomUUID()}`;
  }

  const submission: PendingSubmission = {
    request_id: requestId,
    trip_request: formData,
    fingerprint: currentFingerprint,
    created_at:
      existing && existing.fingerprint === currentFingerprint
        ? existing.created_at
        : Date.now(),
  };

  sessionStorage.setItem(PENDING_SUBMISSION_KEY, JSON.stringify(submission));
  return submission;
}

export function clearPendingSubmission() {
  sessionStorage.removeItem(PENDING_SUBMISSION_KEY);
}
