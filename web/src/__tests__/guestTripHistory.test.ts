import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  clearGuestHistoryTrips,
  deleteGuestHistoryTrip,
  GUEST_HISTORY_UPDATED_EVENT,
  loadGuestHistoryTrips,
  queryGuestHistoryTrips,
  recordGuestJobStatus,
  recordGuestSubmittedTrip,
  recordGuestTripResult,
} from "@/utils/guestTripHistory";
import { exactTimeSourceLabel, governedExactTime } from "@/utils/schedule";
import { getCityGalleryItems } from "@/utils/cityPhotos";
import { SPREAD_START_SCENES } from "@/utils/spreadStartRequest";
import type { TripResult } from "@/types/trip";

describe("guestTripHistory localStorage persistence", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it("tracks submit -> poll completion lifecycle and preserves retry_input", () => {
    const onUpdate = vi.fn();
    window.addEventListener(GUEST_HISTORY_UPDATED_EVENT, onUpdate);

    recordGuestSubmittedTrip("job_cq_1", {
      to_city: "重庆",
      days: 3,
      people_count: 2,
      preferences: ["美食"],
      avoid: [],
      notes: "想看江景",
    });

    // Pending jobs do not enter completed history list yet
    expect(loadGuestHistoryTrips()).toHaveLength(0);

    recordGuestJobStatus("job_cq_1", { status: "RUNNING" });
    expect(loadGuestHistoryTrips()).toHaveLength(0);

    recordGuestJobStatus("job_cq_1", {
      status: "SUCCESS",
      result_record_id: 42,
      generation_policy_version: "v0.12",
    });

    const items = loadGuestHistoryTrips();
    expect(items).toHaveLength(1);
    expect(items[0]).toMatchObject({
      trip_id: "job_cq_1",
      job_id: "job_cq_1",
      status: "SUCCESS",
      city: "重庆",
      days: 3,
      result_record_id: 42,
      generation_policy_version: "v0.12",
    });
    expect(items[0].retry_input?.trip_request.notes).toBe("想看江景");
    expect(onUpdate).toHaveBeenCalledTimes(1);

    window.removeEventListener(GUEST_HISTORY_UPDATED_EVENT, onUpdate);
  });

  it("records direct result views and supports filtering, pagination, delete, and clear", () => {
    recordGuestJobStatus(
      "job_old_fail",
      {
        status: "FAILED",
        error_code: "TIMEOUT",
        error_message: "超时",
      },
      { destination: "重庆", days: 2, startedAt: Date.parse("2026-01-01T00:00:00Z") },
    );

    const sampleResult: TripResult = {
      schema_version: "2.2",
      result_id: 108,
      city: { name: "重庆" },
      request: { days: 4, people_count: 1, preferences: [], avoid: [] },
      plans: [],
    };
    recordGuestTripResult(sampleResult, "job_new_ok");

    const allPage1 = queryGuestHistoryTrips({ limit: 1 });
    expect(allPage1.items).toHaveLength(1);
    expect(allPage1.items[0].trip_id).toBe("job_new_ok");
    expect(allPage1.next_cursor).toBe("1");

    const allPage2 = queryGuestHistoryTrips({ cursor: allPage1.next_cursor ?? undefined, limit: 1 });
    expect(allPage2.items).toHaveLength(1);
    expect(allPage2.items[0].trip_id).toBe("job_old_fail");
    expect(allPage2.next_cursor).toBeNull();

    const failedOnly = queryGuestHistoryTrips({ status: "FAILED" });
    expect(failedOnly.items.map((i) => i.trip_id)).toEqual(["job_old_fail"]);

    deleteGuestHistoryTrip("job_old_fail");
    expect(loadGuestHistoryTrips().map((i) => i.trip_id)).toEqual(["job_new_ok"]);

    clearGuestHistoryTrips();
    expect(loadGuestHistoryTrips()).toHaveLength(0);
  });
});

describe("v0.15.1 planned clock & OSS bundled media fallbacks", () => {
  it("renders planned schedule clock with '约 HH:MM' and '参考' label", () => {
    const res = governedExactTime({
      period: "morning",
      exact_start: "09:30",
      exact_end: "11:00",
      exact_time_source: "planned",
    });
    expect(res).toEqual({ text: "约 09:30", source: "planned" });
    expect(exactTimeSourceLabel("planned")).toBe("参考");
  });

  it("uses bundled Chongqing photo and local scene assets without external CDN 404 paths", () => {
    expect(getCityGalleryItems("重庆")[0]?.url).toBe("/bundled/chongqing.png");
    expect(getCityGalleryItems("杭州")).toEqual([]);
    for (const scene of SPREAD_START_SCENES) {
      expect(scene.photos.length).toBeGreaterThan(0);
      for (const photo of scene.photos) {
        expect(photo.fullUrl.startsWith("/scene-opt/")).toBe(false);
        expect(photo.fullUrl.startsWith("/city-opt/")).toBe(false);
      }
    }
  });
});
