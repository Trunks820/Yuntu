import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import {
  COMPANION_BOUNDARIES,
  type MobileTripDraft,
} from "@/types/mobile";
import {
  buildMobileTripRequest,
  clampCompanionCounts,
  clearMobileDraft,
  computeEndDate,
  createInitialMobileDraft,
  formatCompanionNote,
  getActiveBatchScenes,
  handleAccountSwitchForMobileDraft,
  isDateInPast,
  isValidCalendarDate,
  loadMobileDraft,
  resumeMobileDraftStep,
  sanitizeMobileDraft,
  saveMobileDraft,
} from "@/utils/mobileDraft";

describe("mobileDraft utilities", () => {
  beforeEach(() => {
    sessionStorage.clear();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  describe("createInitialMobileDraft", () => {
    it("creates a valid default draft adhering to phase-0 schema", () => {
      const draft = createInitialMobileDraft("user-1");
      expect(draft.version).toBe(1);
      expect(draft.revision).toBe(1);
      expect(draft.step).toBe("moments");
      expect(draft.entry).toBe("quiz");
      expect(draft.sceneIds).toEqual([]);
      expect(draft.momentBatch).toBe(0);
      expect(draft.rhythm).toBeNull();
      expect(draft.cityId).toBeNull();
      expect(draft.cityName).toBeNull();
      expect(draft.cityConfirmed).toBe(false);
      expect(draft.days).toBe(3);
      expect(draft.dateMode).toBe("undecided");
      expect(draft.startDate).toBeNull();
      expect(draft.companion).toBe("solo");
      expect(draft.adults).toBe(1);
      expect(draft.children).toBe(0);
      expect(draft.avoidIds).toEqual([]);
      expect(draft.rawNotes).toBe("");
      expect(draft.ownerUserId).toBe("user-1");
    });
  });

  describe("Companion boundaries and clamping (AC-07)", () => {
    it("enforces solo boundary: 1 adult, 0 children", () => {
      const clamped = clampCompanionCounts("solo", 5, 3);
      expect(clamped).toEqual({ adults: 1, children: 0 });
    });

    it("enforces partner boundary: 2 adults, 0-3 children, max 5", () => {
      expect(clampCompanionCounts("partner", 1, 0)).toEqual({
        adults: 2,
        children: 0,
      });
      expect(clampCompanionCounts("partner", 3, 4)).toEqual({
        adults: 2,
        children: 3,
      });
    });

    it("enforces friends boundary: 2-6 adults, 0 children", () => {
      expect(clampCompanionCounts("friends", 1, 2)).toEqual({
        adults: 2,
        children: 0,
      });
      expect(clampCompanionCounts("friends", 4, 1)).toEqual({
        adults: 4,
        children: 0,
      });
      expect(clampCompanionCounts("friends", 8, 0)).toEqual({
        adults: 6,
        children: 0,
      });
    });

    it("enforces parents boundary: adult>=2, children 0-3, total<=6", () => {
      const bounds = COMPANION_BOUNDARIES.parents;
      expect(bounds.defaultAdults).toBe(3);
      expect(bounds.defaultChildren).toBe(0);

      // adults 5, children 2 -> total 7 > 6 -> children clamped to 1
      expect(clampCompanionCounts("parents", 5, 2)).toEqual({
        adults: 5,
        children: 1,
      });
      // adults 1 -> clamped to 2
      expect(clampCompanionCounts("parents", 1, 0)).toEqual({
        adults: 2,
        children: 0,
      });
    });

    it("enforces kids boundary: adult>=1, children 1-3, total<=6", () => {
      const bounds = COMPANION_BOUNDARIES.kids;
      expect(bounds.defaultAdults).toBe(2);
      expect(bounds.defaultChildren).toBe(1);

      // adults 0 -> clamped to 1; children 0 -> clamped to 1
      expect(clampCompanionCounts("kids", 0, 0)).toEqual({
        adults: 1,
        children: 1,
      });
      // adults 5, children 3 -> total 8 > 6 -> children clamped to 1
      expect(clampCompanionCounts("kids", 5, 3)).toEqual({
        adults: 5,
        children: 1,
      });
    });

    it("formats companion note with exact wording", () => {
      expect(formatCompanionNote("solo", 1, 0)).toBe(
        "【同行情况】独自一人，1位成人、0位儿童。",
      );
      expect(formatCompanionNote("partner", 2, 1)).toBe(
        "【同行情况】情侣/双人，2位成人、1位儿童。",
      );
      expect(formatCompanionNote("kids", 2, 2)).toBe(
        "【同行情况】带孩子/亲子，2位成人、2位儿童。",
      );
    });
  });

  describe("Date computation and validation (AC-06)", () => {
    it("computes end date correctly without UTC offset bugs", () => {
      // 1 day: same date
      expect(computeEndDate("2026-09-15", 1)).toBe("2026-09-15");
      // 3 days: start_date + 2
      expect(computeEndDate("2026-09-15", 3)).toBe("2026-09-17");
      // cross-month
      expect(computeEndDate("2026-09-30", 3)).toBe("2026-10-02");
      // cross-year
      expect(computeEndDate("2026-12-30", 4)).toBe("2027-01-02");
    });

    it("identifies past dates based on local midnight", () => {
      vi.useFakeTimers({ toFake: ["Date"] });
      vi.setSystemTime(new Date(2026, 8, 20, 15, 30)); // 2026-09-20 15:30

      expect(isDateInPast("2026-09-19")).toBe(true);
      expect(isDateInPast("2026-09-20")).toBe(false);
      expect(isDateInPast("2026-09-21")).toBe(false);
    });

    it("validates calendar date round-trip consistency strictly (rejecting 2027-02-31, etc.)", () => {
      // Leap year
      expect(isValidCalendarDate("2024-02-29")).toBe(true);
      // Non-leap year
      expect(isValidCalendarDate("2026-02-29")).toBe(false);
      // Impossible day
      expect(isValidCalendarDate("2027-02-31")).toBe(false);
      expect(isValidCalendarDate("2026-04-31")).toBe(false); // April has 30 days
      expect(isValidCalendarDate("2026-09-31")).toBe(false); // September has 30 days
      expect(isValidCalendarDate("2026-11-31")).toBe(false); // November has 30 days
      // Valid dates
      expect(isValidCalendarDate("2026-10-01")).toBe(true);
      expect(isValidCalendarDate("2026-12-31")).toBe(true);
      // Malformed strings
      expect(isValidCalendarDate("not-a-date")).toBe(false);
      expect(isValidCalendarDate("")).toBe(false);
      expect(isValidCalendarDate(null)).toBe(false);
    });
  });

  describe("Scene batch rotation (AC-03)", () => {
    it("returns 4 scenes across 4 categories per batch", () => {
      const batch0 = getActiveBatchScenes(0, []);
      expect(batch0).toHaveLength(4);
      expect(batch0.map((s) => s.category)).toEqual([
        "street",
        "nature",
        "food",
        "night",
      ]);
    });

    it("fixes selected scene positions when rotating batch", () => {
      // street_1 is selected
      const batch1WithSelected = getActiveBatchScenes(1, ["street_1"]);
      expect(batch1WithSelected[0].id).toBe("street_1"); // retained
      expect(batch1WithSelected[1].id).toBe("nature_2"); // rotated to batch 1
      expect(batch1WithSelected[2].id).toBe("food_2"); // rotated to batch 1
      expect(batch1WithSelected[3].id).toBe("night_2"); // rotated to batch 1
    });
  });

  describe("buildMobileTripRequest pure builder (AC-03..AC-08)", () => {
    it("omits start_date and end_date entirely when dateMode is undecided", () => {
      const draft: MobileTripDraft = {
        ...createInitialMobileDraft("user-1"),
        cityName: "成都",
        days: 4,
        dateMode: "undecided",
        startDate: null,
      };

      const req = buildMobileTripRequest(draft);
      expect(req.to_city).toBe("成都");
      expect(req.days).toBe(4);
      expect("start_date" in req).toBe(false);
      expect("end_date" in req).toBe(false);
      expect(req.start_date).toBeUndefined();
      expect(req.end_date).toBeUndefined();
    });

    it("includes start_date and computed end_date when dateMode is exact", () => {
      const draft: MobileTripDraft = {
        ...createInitialMobileDraft("user-1"),
        cityName: "成都",
        days: 3,
        dateMode: "exact",
        startDate: "2026-10-01",
      };

      const req = buildMobileTripRequest(draft);
      expect(req.start_date).toBe("2026-10-01");
      expect(req.end_date).toBe("2026-10-03");
    });

    it("deduplicates scene interest categories (AC-03)", () => {
      // Pick 2 street scenes and 1 nature scene
      const draft: MobileTripDraft = {
        ...createInitialMobileDraft("user-1"),
        cityName: "杭州",
        sceneIds: ["street_1", "street_2", "nature_1"],
      };

      const req = buildMobileTripRequest(draft);
      // Both street scenes map to "citywalk", so citywalk appears only once
      expect(req.preferences).toEqual(["citywalk", "自然风光"]);
    });

    it("appends slow and full rhythm keywords correctly (AC-04)", () => {
      const slowDraft: MobileTripDraft = {
        ...createInitialMobileDraft("user-1"),
        cityName: "杭州",
        sceneIds: ["street_1"],
        rhythm: "slow",
      };
      expect(buildMobileTripRequest(slowDraft).preferences).toEqual([
        "citywalk",
        "慢一点",
      ]);

      const fullDraft: MobileTripDraft = {
        ...createInitialMobileDraft("user-1"),
        cityName: "杭州",
        sceneIds: ["street_1"],
        rhythm: "full",
      };
      expect(buildMobileTripRequest(fullDraft).preferences).toEqual([
        "citywalk",
        "充实一点",
      ]);
    });

    it("maps avoid options to exact strings without false approximations (AC-04)", () => {
      const draft: MobileTripDraft = {
        ...createInitialMobileDraft("user-1"),
        cityName: "杭州",
        avoidIds: ["no_early", "less_climb", "less_queue", "no_rush"],
      };

      const req = buildMobileTripRequest(draft);
      expect(req.avoid).toEqual([
        "不想早起",
        "尽量少爬坡",
        "尽量少排队",
        "不想赶行程",
      ]);
    });

    it("preserves rawNotes and appends companion note block exactly once (AC-07, AC-08)", () => {
      const draft: MobileTripDraft = {
        ...createInitialMobileDraft("user-1"),
        cityName: "西安",
        companion: "partner",
        adults: 2,
        children: 1,
        rawNotes: "希望多逛博物馆",
      };

      const req = buildMobileTripRequest(draft);
      expect(req.people_count).toBe(3);
      expect(req.notes).toBe(
        "希望多逛博物馆\n【同行情况】情侣/双人，2位成人、1位儿童。",
      );
      // Ensure the rawNotes on draft itself is unchanged
      expect(draft.rawNotes).toBe("希望多逛博物馆");
    });

    it("never includes unprovided prototype fields (budget, commute_mode, etc.)", () => {
      const draft = createInitialMobileDraft("user-1");
      const req = buildMobileTripRequest(draft);

      expect(req.budget).toBeUndefined();
      expect(req.commute_mode).toBeUndefined();
      expect(req.must_include).toBeUndefined();
      expect(req.daily_start).toBeUndefined();
      expect(req.daily_end).toBeUndefined();
      expect(req.from_city).toBeUndefined();
      expect("from_city" in req).toBe(false);
      expect(req.accommodation).toBeUndefined();
    });

    it("attaches trimmed and length-capped from_city when provided, omits when blank", () => {
      const trimmedReq = buildMobileTripRequest({
        ...createInitialMobileDraft("user-1"),
        cityName: "成都",
        fromCity: "  深圳  ",
      });
      expect(trimmedReq.from_city).toBe("深圳");

      const cappedReq = buildMobileTripRequest({
        ...createInitialMobileDraft("user-1"),
        cityName: "成都",
        fromCity: "城".repeat(80),
      });
      expect(cappedReq.from_city).toBe("城".repeat(60));

      const blankReq = buildMobileTripRequest({
        ...createInitialMobileDraft("user-1"),
        cityName: "成都",
        fromCity: "   ",
      });
      expect("from_city" in blankReq).toBe(false);
    });
  });

  describe("resumeMobileDraftStep (P0-01)", () => {
    it("identifies first incomplete required step in order", () => {
      const d = createInitialMobileDraft();

      // 1. No scenes -> moments
      expect(resumeMobileDraftStep(d)).toBe("moments");

      // 2. Has scenes, no rhythm -> rhythm
      d.sceneIds = ["street_1"];
      expect(resumeMobileDraftStep(d)).toBe("rhythm");

      // 3. Has rhythm, unconfirmed city -> profile
      d.rhythm = "slow";
      expect(resumeMobileDraftStep(d)).toBe("profile");

      // 4. Confirmed city, invalid days -> departure
      d.cityName = "成都";
      d.cityConfirmed = true;
      d.days = null;
      expect(resumeMobileDraftStep(d)).toBe("departure");

      // 5. Valid days, exact mode with expired date -> departure
      vi.useFakeTimers({ toFake: ["Date"] });
      vi.setSystemTime(new Date(2026, 8, 20));
      d.days = 3;
      d.dateMode = "exact";
      d.startDate = "2026-09-18"; // past
      expect(resumeMobileDraftStep(d)).toBe("departure");

      // 6. Valid future date, valid companions -> review
      d.startDate = "2026-09-25";
      expect(resumeMobileDraftStep(d)).toBe("review");
    });
  });

  describe("Storage and sanitization", () => {
    it("serializes and deserializes draft safely", () => {
      const draft = createInitialMobileDraft("user-1");
      draft.cityName = "重庆";
      draft.cityConfirmed = true;
      draft.fromCity = "深圳";
      draft.fromCitySource = "geo";
      draft.sceneIds = ["street_1", "food_1"];

      saveMobileDraft(draft);
      const loaded = loadMobileDraft();
      expect(loaded).not.toBeNull();
      expect(loaded?.cityName).toBe("重庆");
      expect(loaded?.fromCity).toBe("深圳");
      expect(loaded?.fromCitySource).toBe("geo");
      expect(loaded?.sceneIds).toEqual(["street_1", "food_1"]);

      clearMobileDraft();
      expect(loadMobileDraft()).toBeNull();
    });

    it("sanitizer rejects non-version-1 or malformed structures", () => {
      expect(sanitizeMobileDraft(null)).toBeNull();
      expect(sanitizeMobileDraft({ version: 2 })).toBeNull();
      expect(sanitizeMobileDraft("not-json")).toBeNull();

      const sanitized = sanitizeMobileDraft({
        version: 1,
        sceneIds: ["invalid_id", "street_1"],
        days: 99, // out of range
        rawNotes: "a".repeat(400),
        fromCity: "北".repeat(80),
        fromCityTouched: true,
        fromCitySource: "geo",
      });

      expect(sanitized).not.toBeNull();
      expect(sanitized?.sceneIds).toEqual(["street_1"]);
      expect(sanitized?.days).toBeNull();
      expect(sanitized?.rawNotes.length).toBe(300);
      expect(sanitized?.fromCity.length).toBe(60);
      expect(sanitized?.fromCityTouched).toBe(true);
      expect(sanitized?.fromCitySource).toBeNull();
    });

    it("sanitizer enforces finite integers for days and companions (rejecting 2.5)", () => {
      const sanitized = sanitizeMobileDraft({
        version: 1,
        days: 2.5, // non-integer
        adults: 2.5,
        children: 1.8,
      });

      expect(sanitized).not.toBeNull();
      expect(sanitized?.days).toBeNull(); // 2.5 must be rejected to null
      expect(sanitized?.adults).toBe(1); // clamped to valid integer for solo default
      expect(sanitized?.children).toBe(0);
    });

    it("sanitizer enforces calendar date round-trip consistency (rejecting 2027-02-31)", () => {
      const sanitized = sanitizeMobileDraft({
        version: 1,
        startDate: "2027-02-31",
      });

      expect(sanitized).not.toBeNull();
      expect(sanitized?.startDate).toBeNull();
    });

    it("sanitizer rolls back step to earliest required incomplete step when input is invalid", () => {
      // Step says 'review', but days is 2.5 (invalid) -> must roll back to 'departure'
      const sanitizedDays = sanitizeMobileDraft({
        version: 1,
        step: "review",
        sceneIds: ["street_1"],
        rhythm: "slow",
        cityName: "成都",
        cityConfirmed: true,
        days: 2.5,
        dateMode: "undecided",
      });
      expect(sanitizedDays?.step).toBe("departure");

      // Step says 'review', but startDate is impossible 2027-02-31 in exact mode -> 'departure'
      const sanitizedDate = sanitizeMobileDraft({
        version: 1,
        step: "review",
        sceneIds: ["street_1"],
        rhythm: "slow",
        cityName: "成都",
        cityConfirmed: true,
        days: 3,
        dateMode: "exact",
        startDate: "2027-02-31",
      });
      expect(sanitizedDate?.step).toBe("departure");

      // Step says 'review', but sceneIds is empty -> 'moments'
      const sanitizedScenes = sanitizeMobileDraft({
        version: 1,
        step: "review",
        sceneIds: [],
        rhythm: "slow",
        cityName: "成都",
        cityConfirmed: true,
        days: 3,
      });
      expect(sanitizedScenes?.step).toBe("moments");

      // Step says 'companions', but city is not confirmed -> 'profile'
      const sanitizedCity = sanitizeMobileDraft({
        version: 1,
        step: "companions",
        sceneIds: ["street_1"],
        rhythm: "slow",
        cityName: null,
        cityConfirmed: false,
      });
      expect(sanitizedCity?.step).toBe("profile");
    });
  });

  describe("Account switch and anonymous claim (AC-09)", () => {
    it("allows newly authenticated user to claim an anonymous draft", () => {
      const anonDraft = createInitialMobileDraft(null);
      anonDraft.rawNotes = "我的心愿单";

      const claimed = handleAccountSwitchForMobileDraft(anonDraft, "user-abc");
      expect(claimed?.ownerUserId).toBe("user-abc");
      expect(claimed?.rawNotes).toBe("我的心愿单");
    });

    it("clears sensitive notes and companions when switching accounts", () => {
      const userADraft = createInitialMobileDraft("user-A");
      userADraft.rawNotes = "私密备忘信息";
      userADraft.companion = "kids";
      userADraft.adults = 2;
      userADraft.children = 2;

      const switched = handleAccountSwitchForMobileDraft(userADraft, "user-B");
      expect(switched?.ownerUserId).toBe("user-B");
      expect(switched?.rawNotes).toBe("");
      expect(switched?.companion).toBe("solo");
      expect(switched?.adults).toBe(1);
      expect(switched?.children).toBe(0);
    });

    it("clears sensitive notes upon explicit logout", () => {
      const userADraft = createInitialMobileDraft("user-A");
      userADraft.rawNotes = "账号A独有备注";

      const loggedOut = handleAccountSwitchForMobileDraft(userADraft, null);
      expect(loggedOut?.ownerUserId).toBeNull();
      expect(loggedOut?.rawNotes).toBe("");
    });
  });
});
