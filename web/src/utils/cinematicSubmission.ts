import type { TripSubmitRequest } from "@/types/tripRequest";
import { generateFingerprint } from "./pendingSubmission";

export const CINEMATIC_SUBMISSION_KEY = "yuntu-cinematic-submission-v1";

// Separate from the old homepage: a pending request is not login consent.
export function saveCinematicSubmission(form: TripSubmitRequest, owner: string) {
  const fingerprint = generateFingerprint(form);
  let existing: { owner?: string; fingerprint?: string; request_id?: string } | null = null;
  try {
    existing = JSON.parse(sessionStorage.getItem(CINEMATIC_SUBMISSION_KEY) || "null");
  } catch { /* A malformed draft cannot supply an idempotency key. */ }
  const request_id = existing?.owner === owner && existing?.fingerprint === fingerprint &&
    typeof existing.request_id === "string" && /^web-[a-f0-9-]+$/.test(existing.request_id)
    ? existing.request_id : `web-${crypto.randomUUID()}`;
  const submission = { owner, fingerprint, request_id, trip_request: form };
  sessionStorage.setItem(CINEMATIC_SUBMISSION_KEY, JSON.stringify(submission));
  return submission;
}

export function clearCinematicSubmission() {
  sessionStorage.removeItem(CINEMATIC_SUBMISSION_KEY);
}
