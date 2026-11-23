/* eslint-disable @typescript-eslint/no-explicit-any */
import fs from "node:fs";
import path from "node:path";
import { StrictMode } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import AMapLoader from "@amap/amap-jsapi-loader";
import { Sun } from "lucide-react";
import SpreadBookPage from "@/pages/book/SpreadBookPage";
import { router } from "@/router";
import * as apiModule from "@/services/api";
import { ApiRequestError } from "@/services/api";
import { useTripStore } from "@/stores/tripStore";
import type { PlaceDetail, TripPlace, TripResult } from "@/types/trip";
import {
  MISSING_LINK_INFO_TEXT,
  UNSUPPORTED_CONTRACT_TEXT,
} from "@/utils/planLinkError";
import {
  computeBookMapAvoidPadding,
  findCommuteLeg,
  formatBookDayLabel,
  formatDayWeatherBadge,
  formatNextLegText,
  formatStayMinutes,
  formatTransitStepLine,
  getDayWeather,
  hasValidAccommodationCoords,
  isBookReadingPage,
  isValidGeoPoint,
  pickClosestReadingStopId,
  resolveBookCardCopy,
  resolveCommutePopup,
  resolvePlaceTopReasons,
  resolveStopCardNote,
  resolveStopDetailBody,
  resolveStopKind,
  resolveStopScheduleMeta,
  sortPlaceGallery,
} from "@/utils/spreadBook";

const amapBookCalls = vi.hoisted(() => ({
  mapInitOpts: [] as any[],
  polylines: [] as any[],
  markers: [] as Array<{ opts: any; clicks: Array<() => void> }>,
  fitViewCalls: [] as Array<{
    targets: any[];
    immediately: boolean;
    avoid: number[];
    maxZoom: number;
  }>,
  panToCalls: [] as any[],
  lngLatContainerResult: { x: 680, y: 320 },
  destroyedCount: 0,
}));

vi.mock("@amap/amap-jsapi-loader", () => ({
  default: {
    load: vi.fn(async () => {
      class MockPixel {
        x: number;
        y: number;
        constructor(x: number, y: number) {
          this.x = x;
          this.y = y;
        }
      }
      class MockPolyline {
        opts: any;
        constructor(opts: any) {
          this.opts = opts;
          amapBookCalls.polylines.push(opts);
        }
        setMap = vi.fn();
      }
      class MockMarker {
        opts: any;
        clicks: Array<() => void> = [];
        constructor(opts: any) {
          this.opts = opts;
          amapBookCalls.markers.push({ opts, clicks: this.clicks });
        }
        on = vi.fn((event: string, cb: () => void) => {
          if (event === "click") this.clicks.push(cb);
        });
        setzIndex = vi.fn();
        setMap = vi.fn((map: MockMap | null) => {
          if (this.opts?.content instanceof HTMLElement) {
            if (map?.container) {
              map.container.appendChild(this.opts.content);
            } else if (this.opts.content.parentNode) {
              this.opts.content.parentNode.removeChild(this.opts.content);
            }
          }
        });
      }
      class MockMap {
        container: HTMLElement;
        constructor(container: HTMLElement, opts: any) {
          this.container = container;
          amapBookCalls.mapInitOpts.push(opts);
        }
        setFitView = vi.fn(
          (
            targets: any[],
            immediately: boolean,
            avoid: number[],
            maxZoom: number,
          ) => {
            amapBookCalls.fitViewCalls.push({
              targets,
              immediately,
              avoid,
              maxZoom,
            });
          },
        );
        lngLatToContainer = vi.fn(
          () =>
            new MockPixel(
              amapBookCalls.lngLatContainerResult.x,
              amapBookCalls.lngLatContainerResult.y,
            ),
        );
        containerToLngLat = vi.fn((px: any) => [
          106.5 + (px?.x ?? 0) * 0.0001,
          29.5,
        ]);
        panTo = vi.fn((center: any) => {
          amapBookCalls.panToCalls.push(center);
        });
        setCenter = vi.fn();
        resize = vi.fn();
        destroy = vi.fn(() => {
          amapBookCalls.destroyedCount += 1;
        });
      }
      return {
        Map: MockMap,
        Marker: MockMarker,
        Polyline: MockPolyline,
        Pixel: MockPixel,
      };
    }),
  },
}));

vi.mock("@/services/api", async (importOriginal) => {
  const actual = await importOriginal<typeof apiModule>();
  return {
    ...actual,
    fetchResult: vi.fn(),
    fetchPlaceDetail: vi.fn(),
  };
});

function mockMatchMedia(reduced: boolean) {
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    value: vi.fn().mockImplementation((query: string) => ({
      matches: reduced && query.includes("prefers-reduced-motion"),
      media: query,
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })),
  });
}

function makePlaceDetail(id: number, name: string): PlaceDetail {
  return {
    place_id: id,
    name,
    place_type: "attraction",
    district: "渝中区",
    longitude: 106.57 + id * 0.001,
    latitude: 29.55 + id * 0.001,
    summary: `${name}的百科详情介绍。`,
    top_reasons: [`理由一(${name})`, `理由二(${name})`, "多余第三条"],
    warnings: id === 323 ? ["傍晚人流较多，建议错峰上楼"] : [],
    source_count: 12,
    mention_count: 48,
    gallery: [
      {
        asset_id: `a-${id}-2`,
        position: 2,
        alt_text: `${name}次图`,
        desktop: {
          url: `https://img.example/${id}-2-desktop.webp`,
          width: 1200,
          height: 800,
        },
        mobile: {
          url: `https://img.example/${id}-2-mobile.webp`,
          width: 800,
          height: 600,
        },
        thumb: {
          url: `https://img.example/${id}-2-thumb.webp`,
          width: 300,
          height: 225,
        },
      },
      {
        asset_id: `a-${id}-0`,
        position: 0,
        alt_text: `${name}首图`,
        desktop: {
          url: `https://img.example/${id}-0-desktop.webp`,
          width: 1200,
          height: 800,
        },
        mobile: {
          url: `https://img.example/${id}-0-mobile.webp`,
          width: 800,
          height: 600,
        },
        thumb: {
          url: `https://img.example/${id}-0-thumb.webp`,
          width: 300,
          height: 225,
        },
      },
    ],
  };
}

function makeSampleResult(overrides?: Partial<TripResult>): TripResult {
  const base: TripResult = {
    schema_version: "1.5",
    result_id: 91500,
    city: { name: "重庆" },
    request: {
      days: 2,
      people_count: 2,
      preferences: ["citywalk"],
      avoid: [],
    },
    weather: { status: "skipped_disabled", city: "重庆", days: [] },
    plans: [
      {
        plan_id: "plan_a",
        title: "山城慢逛两日",
        summary: "首日动物园，次日渝中半岛串联",
        tags: ["citywalk", "轻松", "historic_district", "museum"],
        pace: {
          level: "RELAXED",
          commute_status: "WITHIN_LIMIT",
          total_commute_minutes: 42,
        },
        accommodation: {
          name: "解放碑商圈",
          latitude: 29.558,
          longitude: 106.577,
          source: "auto_recommended",
        },
        cost_estimate: {
          snapshot_version: "1",
          completeness: "complete",
          currency: "CNY",
          estimated_at: "2026-08-04T00:00:00Z",
          scenarios: [],
          notice: "",
        },
        days: [
          {
            day: 1,
            title: "重庆动物园全日深游",
            narrative: "整日只安排动物园一个主景点，按体力自由推进。",
            commute_summary: "当日集中在园区内，无跨点通勤",
            pace_status: "WITHIN_LIMIT",
            places: [
              {
                place_id: 311,
                name: "重庆动物园",
                category: "attraction",
                longitude: 106.5102,
                latitude: 29.5051,
                role: "anchor_activity",
                optional: false,
                brief: "西南代表性动物园",
                stay_minutes: 90,
                activity_note: "按大熊猫馆到金鱼池顺序慢逛。",
                schedule: {
                  period: "morning",
                  exact_start: "09:30",
                  exact_end: null,
                  exact_time_source: null, // 非受治理来源不得展示准点
                },
              },
            ],
            commute_legs: [],
          },
          {
            day: 2,
            title: "山城巷至千厮门大桥",
            narrative: "从山城巷老街开场，傍晚洪崖洞收束。",
            commute_summary: "公共交通与步行结合，总通勤约 44 分钟",
            pace_status: "WITHIN_LIMIT",
            places: [
              {
                place_id: 321,
                name: "山城巷",
                category: "attraction",
                longitude: 106.5716,
                latitude: 29.5537,
                role: "anchor_activity",
                optional: false,
                brief: "依山而建的老城步道",
                stay_minutes: 45,
                activity_note: "沿崖边栈道自下而上慢走。",
                schedule: {
                  period: "morning",
                  exact_start: null,
                  exact_end: null,
                  exact_time_source: null,
                },
              },
              {
                place_id: 322,
                name: "解放碑",
                category: "business_area",
                longitude: 106.5784,
                latitude: 29.5574,
                role: "secondary_activity",
                optional: false,
                brief: "渝中商圈核心地标",
                activity_note: "顺路解决午餐，不作长停留。",
                schedule: {
                  period: "afternoon",
                  exact_start: null,
                  exact_end: null,
                  exact_time_source: null,
                },
              },
              {
                place_id: 323,
                name: "洪崖洞",
                category: "attraction",
                longitude: 106.5827,
                latitude: 29.5631,
                role: "anchor_activity",
                optional: false,
                brief: "吊脚楼建筑群",
                stay_minutes: 90,
                activity_note: "从顶层平台向下逐层逛。",
                schedule: {
                  period: "evening",
                  exact_start: "19:30",
                  exact_end: null,
                  exact_time_source: "event",
                },
              },
              {
                place_id: 324,
                name: "千厮门大桥",
                category: "photo_spot",
                longitude: 106.5807,
                latitude: 29.5672,
                role: "photo_stop",
                optional: true,
                brief: "回望洪崖洞全景的经典机位",
                // Schema 1.4-style missing schedule & activity_note
              },
            ],
            commute_legs: [
              {
                from_place_id: 321,
                to_place_id: 322,
                mode: "transit",
                duration_source: "amap",
                duration_minutes: 22,
                distance_meters: 2600,
                transit_steps: [
                  {
                    kind: "walking",
                    duration_minutes: 4,
                    distance_meters: 300,
                    line_name: null,
                    provider_type: null,
                    from_stop: null,
                    to_stop: null,
                    stop_count: null,
                  },
                  {
                    kind: "rail",
                    duration_minutes: 6,
                    distance_meters: 1900,
                    line_name: "轨道交通1号线",
                    provider_type: "地铁线路",
                    from_stop: "较场口站",
                    to_stop: "小什字站",
                    stop_count: 2,
                  },
                ],
                transit_summary: "公共交通预计 22 分钟：乘轨道交通1号线",
              },
              {
                from_place_id: 322,
                to_place_id: 323,
                mode: "walking",
                duration_source: "amap",
                duration_minutes: 10,
                distance_meters: 750,
              },
              {
                from_place_id: 323,
                to_place_id: 324,
                mode: "cycling",
                duration_source: "estimate",
                duration_minutes: 12,
                distance_meters: 900,
                transit_steps: [],
                transit_summary: "线路明细暂不可用，请以现场导航为准",
              },
            ],
          },
        ],
      },
    ],
  };
  return { ...base, ...overrides };
}

function RouteEcho() {
  const loc = useLocation();
  return <div data-testid="route-echo">{`${loc.pathname}${loc.search}`}</div>;
}

function renderBookPage(initialUrl: string) {
  return render(
    <div className="yuntu-spread">
      <MemoryRouter initialEntries={[initialUrl]}>
        <RouteEcho />
        <Routes>
          <Route path="/plan/:resultId/:planId" element={<SpreadBookPage />} />
          <Route path="/history" element={<div>history-page</div>} />
          <Route path="/" element={<div>start-page</div>} />
        </Routes>
      </MemoryRouter>
    </div>,
  );
}

describe("SpreadBook pure helpers & code hygiene", () => {
  it("formats day labels, weather gating, stay durations and schedule governance accurately", () => {
    expect(formatBookDayLabel(1)).toBe("第一天");
    expect(formatBookDayLabel(10)).toBe("第十天");
    expect(formatBookDayLabel(11)).toBe("第 11 天");
    expect(formatBookDayLabel(0)).toBe("第 1 天");

    const wxOk = {
      status: "ok",
      city: "重庆",
      days: [
        {
          day: 2,
          date: "2026-11-15",
          weather_text: "晴",
          temp_min_c: 13,
          temp_max_c: 21,
          wind_text: "微风",
          icon_code: "sunny",
          reminders: [],
        },
      ],
    };
    expect(getDayWeather(wxOk, 1)).toBeNull();
    expect(formatDayWeatherBadge(getDayWeather(wxOk, 2))).toEqual({
      Icon: Sun,
      weatherText: "晴",
      tempRange: "13–21°",
    });
    expect(
      getDayWeather({ ...wxOk, status: "skipped_disabled" }, 2),
    ).toBeNull();

    expect(formatStayMinutes(undefined)).toBeNull();
    expect(formatStayMinutes(0)).toBeNull();
    expect(formatStayMinutes(45)).toBe("建议 45 分钟");
    expect(formatStayMinutes(60)).toBe("建议 1 小时");
    expect(formatStayMinutes(90)).toBe("建议 1.5 小时");

    expect(
      resolveStopScheduleMeta({
        place_id: 1,
        name: "A",
        category: "attraction",
        longitude: 106,
        latitude: 29,
        role: "anchor_activity",
        optional: false,
        brief: "",
        schedule: {
          period: "morning",
          exact_start: "09:30",
          exact_end: null,
          exact_time_source: null,
        },
      }),
    ).toEqual({
      period: "morning",
      periodText: "上午",
      exactTimeText: null,
      combinedText: "上午",
    });

    expect(
      resolveStopScheduleMeta({
        place_id: 2,
        name: "B",
        category: "attraction",
        longitude: 106,
        latitude: 29,
        role: "anchor_activity",
        optional: false,
        brief: "",
        schedule: {
          period: "evening",
          exact_start: "19:30",
          exact_end: null,
          exact_time_source: "event",
        },
      }).combinedText,
    ).toBe("傍晚 19:30 起");

    expect(
      resolveStopScheduleMeta({
        place_id: 20,
        name: "B2",
        category: "attraction",
        longitude: 106,
        latitude: 29,
        role: "anchor_activity",
        optional: false,
        brief: "",
        schedule: {
          period: "morning",
          exact_start: "09:45",
          exact_end: null,
          exact_time_source: "planned",
        },
      }),
    ).toEqual({
      period: "morning",
      periodText: "上午",
      exactTimeText: "约 09:45",
      combinedText: "上午 约 09:45",
    });

    expect(
      resolveStopScheduleMeta({
        place_id: 3,
        name: "C",
        category: "attraction",
        longitude: 106,
        latitude: 29,
        role: "anchor_activity",
        optional: false,
        brief: "",
      }),
    ).toEqual({
      period: null,
      periodText: null,
      exactTimeText: null,
      combinedText: null,
    });
  });

  it("resolves card notes, sorted galleries, detail body, tags, commute popups and map bounds padding", () => {
    const placeWithPlaceholder: TripPlace = {
      place_id: 1,
      name: "茶馆",
      category: "cafe",
      longitude: 106.5,
      latitude: 29.5,
      role: "secondary_activity",
      optional: false,
      brief: "写成咖啡/茶歇休息点，不要补充无来源事实",
    };
    expect(resolveStopCardNote(placeWithPlaceholder)).toBe("");
    // brief 是帖子摘句，卡片不放；只放 activity_note
    expect(
      resolveStopCardNote({
        ...placeWithPlaceholder,
        brief: "建议路线：西南联合大学——文林街——翠湖公园",
      }),
    ).toBe("");
    expect(
      resolveStopCardNote({
        ...placeWithPlaceholder,
        activity_note: " 坐下来歇歇脚 ",
      }),
    ).toBe("坐下来歇歇脚");

    expect(resolveStopKind("park")).toBe("park");
    expect(resolveStopKind("restaurant")).toBe("food");
    expect(resolveStopKind("cafe")).toBe("food");
    expect(resolveStopKind("museum")).toBe("museum");
    expect(resolveStopKind("attraction")).toBe("sight");
    expect(resolveStopKind("business_area")).toBe("street");
    expect(resolveStopKind("market")).toBe("market");
    expect(resolveStopKind("photo_spot")).toBe("photo");
    expect(resolveStopKind("hotel")).toBe("other");
    expect(resolveStopKind("")).toBe("other");
    expect(resolveStopKind(undefined)).toBe("other");

    const detail = makePlaceDetail(1, "茶馆");
    expect(resolvePlaceTopReasons(detail)).toEqual([
      "理由一(茶馆)",
      "理由二(茶馆)",
      "多余第三条",
    ]);
    expect(
      resolvePlaceTopReasons({ ...detail, top_reasons: ["  ", " 江景 "] }),
    ).toEqual(["江景"]);
    expect(sortPlaceGallery(detail.gallery).map((g) => g.position)).toEqual([
      0, 2,
    ]);

    const body = resolveStopDetailBody(
      {
        ...placeWithPlaceholder,
        brief: "老牌山城茶馆",
        activity_note: "点一碗盖碗茶看江景",
      },
      detail,
    );
    expect(body).toEqual({
      itineraryText: "老牌山城茶馆。点一碗盖碗茶看江景",
      detailSummary: "茶馆的百科详情介绍。",
    });

    expect(
      resolveBookCardCopy("总结文本，适合用于对比方案ABC", [
        "relaxed",
        "citywalk",
        "historic_district",
      ]),
    ).toEqual({
      summary: "总结文本",
      tags: ["#citywalk"],
    });

    const day2 = makeSampleResult().plans[0].days[1];
    // 到下一站：有 transit_summary 只写它（分钟已在里面），没有才写方式和分钟
    expect(formatNextLegText(findCommuteLeg(day2, 321, 322)!)).toBe(
      "公共交通预计 22 分钟：乘轨道交通1号线",
    );
    expect(formatNextLegText(findCommuteLeg(day2, 322, 323)!)).toBe(
      "步行 · 10 分钟",
    );
    expect(
      formatNextLegText({
        ...findCommuteLeg(day2, 323, 324)!,
        transit_summary: undefined,
      }),
    ).toBe("骑行 · 12 分钟 · 估算");

    const leg1 = findCommuteLeg(day2, 321, 322)!;
    const popup1 = resolveCommutePopup(leg1);
    expect(popup1?.kind).toBe("steps");
    if (popup1?.kind === "steps") {
      expect(formatTransitStepLine(popup1.steps[1])).toBe(
        "轨道交通1号线 · 较场口站上车 → 小什字站 · 2 站 · 6 分钟",
      );
    }
    expect(resolveCommutePopup(findCommuteLeg(day2, 322, 323)!)).toBeNull();
    expect(resolveCommutePopup(findCommuteLeg(day2, 323, 324)!)).toEqual({
      kind: "summary",
      summary: "线路明细暂不可用，请以现场导航为准",
    });

    expect(isValidGeoPoint(0, 0)).toBe(false);
    expect(isValidGeoPoint(106.5, 29.5)).toBe(true);
    expect(hasValidAccommodationCoords(null)).toBe(false);
    expect(isBookReadingPage("guide")).toBe(true);
    expect(isBookReadingPage("before")).toBe(true);
    expect(isBookReadingPage("go")).toBe(true);
    expect(isBookReadingPage("cost")).toBe(true);
    expect(isBookReadingPage("miss")).toBe(false);
    expect(isBookReadingPage("stay")).toBe(false);
    expect(isBookReadingPage(null)).toBe(false);

    // 默认估计高度（215px 展开）与右栏宽度（无栏 140 / 详情 380+60=440 / 阅读 540+60=600）
    expect(computeBookMapAvoidPadding(1440, 848, false)).toEqual([
      90, 275, 380, 140,
    ]);
    expect(computeBookMapAvoidPadding(1440, 848, true)).toEqual([
      90, 275, 380, 440,
    ]);
    expect(computeBookMapAvoidPadding(1440, 848, true, true)).toEqual([
      90, 70, 380, 600,
    ]);
    // 随实际长卷高度、收起贴底与右栏实际宽度变化
    expect(
      computeBookMapAvoidPadding(1440, 848, false, false, {
        stripVisibleHeight: 200,
      }),
    ).toEqual([90, 260, 380, 140]);
    expect(
      computeBookMapAvoidPadding(1440, 848, false, false, {
        stripVisibleHeight: 240,
      }),
    ).toEqual([90, 300, 380, 140]);
    expect(
      computeBookMapAvoidPadding(1440, 848, false, false, {
        stripVisibleHeight: 52,
        stripFolded: true,
      }),
    ).toEqual([90, 96, 380, 140]);
    expect(
      computeBookMapAvoidPadding(1440, 848, true, true, {
        stripVisibleHeight: 215,
        sideWidth: 500,
      }),
    ).toEqual([90, 70, 380, 560]);
    // 四种笔记本视口（扣除 52px 顶栏）均保留足够中心视野且按实际尺寸避让
    for (const [vw, vh] of [
      [1280, 535],
      [1366, 583],
      [1536, 679],
      [1440, 848],
    ]) {
      const readPad = computeBookMapAvoidPadding(vw, vh, true, true);
      expect(readPad).toEqual([90, 70, 380, 600]);
      expect(vw - readPad[2] - readPad[3]).toBeGreaterThanOrEqual(120);
      expect(vh - readPad[0] - readPad[1]).toBeGreaterThanOrEqual(120);

      const stopPad = computeBookMapAvoidPadding(vw, vh, true, false, {
        stripVisibleHeight: 215,
        sideWidth: 380,
      });
      expect(stopPad).toEqual([90, 275, 380, 440]);
      expect(vw - stopPad[2] - stopPad[3]).toBeGreaterThanOrEqual(120);
      expect(vh - stopPad[0] - stopPad[1]).toBeGreaterThanOrEqual(120);
    }
    const tight = computeBookMapAvoidPadding(720, 480, true);
    expect(tight[2] + tight[3]).toBeLessThanOrEqual(600);
    expect(tight[0] + tight[1]).toBeLessThanOrEqual(360);
    const tightRead = computeBookMapAvoidPadding(720, 480, true, true);
    expect(tightRead[2] + tightRead[3]).toBeLessThanOrEqual(600);
    expect(tightRead[0] + tightRead[1]).toBeLessThanOrEqual(360);

    expect(pickClosestReadingStopId([], 240)).toBeNull();
    expect(
      pickClosestReadingStopId(
        [
          { placeId: 101, top: 0, height: 0 },
          { placeId: 102, top: 0, height: 0 },
        ],
        240,
      ),
    ).toBe(101);
    expect(
      pickClosestReadingStopId(
        [
          { placeId: 101, top: 80, height: 110 },
          { placeId: 102, top: 200, height: 120 },
          { placeId: 103, top: 340, height: 110 },
        ],
        240,
      ),
    ).toBe(102);
  });

  it("enforces CSS .yuntu-spread scoping and code hygiene across all Step 7a+7b+7c files", () => {
    const files = [
      "src/utils/spreadBook.ts",
      "src/pages/book/BookBar.tsx",
      "src/pages/book/BookGuide.tsx",
      "src/pages/book/BookMap.tsx",
      "src/pages/book/BookPages.tsx",
      "src/pages/book/DayStrip.tsx",
      "src/pages/book/StopPanel.tsx",
      "src/pages/book/SpreadBookPage.tsx",
      "src/pages/book/spread-book.css",
    ];
    for (const rel of files) {
      const content = fs.readFileSync(path.resolve(process.cwd(), rel), "utf8");
      expect(content).not.toContain("CITY_CENTER");
      expect(content).not.toContain("applyJobResponse");
      expect(content).not.toContain("getBasicExplanation");
      expect(content).not.toContain("kakarot8.com");
      expect(content).not.toContain("L.tileLayer");
      expect(content).not.toContain("方向稿");
      expect(content).not.toContain("104.06");
      expect(content).not.toContain("106.55");
      expect(content).not.toContain("116.40");
      expect(content).not.toContain("121.47");
    }

    const css = fs.readFileSync(
      path.resolve(process.cwd(), "src/pages/book/spread-book.css"),
      "utf8",
    );
    const stripped = css
      .replace(/\/\*[\s\S]*?\*\//g, "")
      .replace(/@keyframes\s+[^{]+\{(?:[^{}]*\{[^}]*\})*[^}]*\}/g, "")
      .replace(/@media\s*\([^)]+\)\s*\{/g, "");
    const withoutRuleBodies = stripped.replace(/\{[^{}]*\}/g, "|RULE|");
    const selectors = withoutRuleBodies
      .split("|RULE|")
      .map((s) => s.replace(/\}\s*/g, "").trim())
      .filter(Boolean);
    const splitTopLevelCommas = (sel: string): string[] => {
      const parts: string[] = [];
      let depth = 0;
      let start = 0;
      for (let i = 0; i < sel.length; i++) {
        const ch = sel[i];
        if (ch === "(" || ch === "[") depth++;
        else if (ch === ")" || ch === "]") depth = Math.max(0, depth - 1);
        else if (ch === "," && depth === 0) {
          parts.push(sel.slice(start, i).trim());
          start = i + 1;
        }
      }
      parts.push(sel.slice(start).trim());
      return parts.filter(Boolean);
    };

    expect(selectors.length).toBeGreaterThan(20);
    for (const sel of selectors) {
      for (const part of splitTopLevelCommas(sel)) {
        expect(part.includes(".yuntu-spread")).toBe(true);
      }
    }
    expect(css).toContain(".yuntu-spread .bk-atlas .amap-layers > .amap-layer");
    expect(css).not.toMatch(/\.amap-layers\s*\{/);
  });

  it("keeps /result/:resultId unchanged while making /plan/:resultId/:planId responsive", () => {
    const rootChildren = router.routes[0]?.children ?? [];
    const resultRoute = rootChildren.find((r) => r.path === "result/:resultId");
    const planRoute = rootChildren.find(
      (r) => r.path === "plan/:resultId/:planId",
    );
    expect(resultRoute).toBeTruthy();
    expect(planRoute).toBeTruthy();

    const routerSrc = fs.readFileSync(
      path.resolve(process.cwd(), "src/router.tsx"),
      "utf8",
    );
    expect(routerSrc).toContain(
      "<Responsive mobile={<PlanDetailPage />} desktop={<SpreadBookPage />} />",
    );
    expect(routerSrc).toMatch(
      /path:\s*"result\/:resultId"[\s\S]*?<ResultPage \/>/,
    );
  });
});

describe("SpreadBookPage open states, interactive strip, StopPanel & map sync", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    amapBookCalls.mapInitOpts = [];
    amapBookCalls.polylines = [];
    amapBookCalls.markers = [];
    amapBookCalls.fitViewCalls = [];
    amapBookCalls.panToCalls = [];
    amapBookCalls.lngLatContainerResult = { x: 680, y: 320 };
    amapBookCalls.destroyedCount = 0;

    useTripStore.getState().reset();
    mockMatchMedia(false);

    vi.stubEnv("VITE_AMAP_KEY", "test-amap-key");
    vi.stubEnv("VITE_AMAP_SECURITY", "test-amap-security");

    vi.mocked(apiModule.fetchPlaceDetail).mockImplementation(async (id) =>
      makePlaceDetail(id, `地点${id}`),
    );
  });

  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it("handles missing job_id, 404, 422 unsupported, 422 invalid, and missing planId without showing a white map", async () => {
    // 1. Missing job_id skips API completely
    const rMissing = renderBookPage("/plan/91500/plan_a");
    expect(screen.getByText(MISSING_LINK_INFO_TEXT)).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "回到历史记录" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "重新规划" }),
    ).toBeInTheDocument();
    expect(apiModule.fetchResult).not.toHaveBeenCalled();
    expect(screen.queryByTestId("bk-amap-container")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "回到历史记录" }));
    expect(screen.getByTestId("route-echo")).toHaveTextContent("/history");
    rMissing.unmount();

    // 2. 404 not found
    vi.mocked(apiModule.fetchResult).mockRejectedValueOnce(
      new ApiRequestError("NOT_FOUND", "nope", 404),
    );
    const r404 = renderBookPage("/plan/91500/plan_a?job_id=job_1");
    await waitFor(() => {
      expect(screen.getByText("攻略不存在")).toBeInTheDocument();
    });
    expect(
      screen.getByRole("button", { name: "重新规划" }),
    ).toBeInTheDocument();
    r404.unmount();

    // 3. 422 unsupported contract -> "重新生成"
    vi.mocked(apiModule.fetchResult).mockRejectedValueOnce(
      new ApiRequestError("RESULT_CONTRACT_UNSUPPORTED", "old", 422),
    );
    const rUnsupported = renderBookPage("/plan/91500/plan_a?job_id=job_2");
    await waitFor(() => {
      expect(screen.getByText(UNSUPPORTED_CONTRACT_TEXT)).toBeInTheDocument();
    });
    expect(
      screen.getByRole("button", { name: "重新生成" }),
    ).toBeInTheDocument();
    rUnsupported.unmount();

    // 4. Plan ID not found in valid result
    vi.mocked(apiModule.fetchResult).mockResolvedValueOnce(makeSampleResult());
    const rNoPlan = renderBookPage("/plan/91500/missing_plan?job_id=job_3");
    await waitFor(() => {
      expect(screen.getByText("方案未找到")).toBeInTheDocument();
    });
    expect(
      screen.getByRole("button", { name: "重新规划" }),
    ).toBeInTheDocument();
    rNoPlan.unmount();
  });

  it("reuses useTripStore cache cleanly without duplicate fetchResult and renders book card & notices", async () => {
    const sample = makeSampleResult({
      delivery: {
        schema_version: "1.0",
        outcome: "BASIC",
        review_state: null,
        reason_code: null,
        quota_state: "RELEASED",
        result_available: true,
      },
      weather: {
        status: "ok",
        city: "重庆",
        days: [
          {
            day: 1,
            date: "2026-11-14",
            weather_text: "小雨",
            temp_min_c: 14,
            temp_max_c: 18,
            wind_text: "北风",
            icon_code: "light_rain",
            reminders: [],
          },
        ],
      },
    });
    useTripStore.getState().setResult("91500", "job_cached", sample);

    renderBookPage("/plan/91500/plan_a?job_id=job_cached");

    expect(apiModule.fetchResult).not.toHaveBeenCalled();
    expect(
      screen.getByRole("heading", { name: "山城慢逛两日" }),
    ).toBeInTheDocument();
    expect(screen.getByText("重庆 · 2 天 · 2 人")).toBeInTheDocument();
    expect(
      screen.getByText(/首日动物园，次日渝中半岛串联/),
    ).toBeInTheDocument();
    expect(screen.getByText("#citywalk #轻松")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /我的行程/ })).toHaveAttribute(
      "href",
      "/history",
    );
    expect(screen.getByText("基础行程已生成")).toBeInTheDocument();
    expect(screen.getByText("本次不扣额度")).toBeInTheDocument();

    // No dates, no Demo markers; 7c 攻略 button appears alongside bar actions
    expect(screen.queryByText(/示例/)).toBeNull();
    expect(screen.getByRole("button", { name: "攻略" })).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "导出 PDF" }),
    ).toBeInTheDocument();

    // Day 1 tab has weather chip; Day 2 does not guess weather
    const day1Tab = screen.getByRole("button", { name: /第一天/ });
    expect(day1Tab).toHaveTextContent("小雨 14–18°");
    const day2Tab = screen.getByRole("button", { name: /第二天/ });
    expect(day2Tab).toHaveTextContent("第二天");
    expect(day2Tab).not.toHaveTextContent("°");

    await waitFor(() => {
      expect(apiModule.fetchPlaceDetail).toHaveBeenCalledWith(311);
      expect(amapBookCalls.markers.length).toBeGreaterThanOrEqual(1);
    });
  });

  it("renders Day 1 & Day 2 stops, sorted photos, commute popups, synchronized StopPanel & per-place cache", async () => {
    const sample = makeSampleResult();
    vi.mocked(apiModule.fetchResult).mockResolvedValue(sample);

    vi.mocked(apiModule.fetchPlaceDetail).mockImplementation(async (id) => {
      if (id === 322) {
        // 解放碑无实拍
        return {
          ...makePlaceDetail(322, "解放碑"),
          gallery: [],
          top_reasons: [],
        };
      }
      return makePlaceDetail(id, id === 311 ? "重庆动物园" : `点${id}`);
    });

    const scrollIntoViewSpy = vi.fn();
    Element.prototype.scrollIntoView = scrollIntoViewSpy;

    render(
      <StrictMode>
        <div className="yuntu-spread">
          <MemoryRouter initialEntries={["/plan/91500/plan_a?job_id=job_live"]}>
            <Routes>
              <Route
                path="/plan/:resultId/:planId"
                element={<SpreadBookPage />}
              />
            </Routes>
          </MemoryRouter>
        </div>
      </StrictMode>,
    );

    // Loading state first (no white map rendered while loading)
    expect(screen.getByText("正在翻开旅行册…")).toBeInTheDocument();
    expect(screen.queryByTestId("bk-amap-container")).toBeNull();

    await waitFor(() => {
      expect(
        screen.getByRole("heading", { name: "山城慢逛两日" }),
      ).toBeInTheDocument();
    });

    // StrictMode check: place 311 fetched only ONCE on Day 1
    await waitFor(() => {
      expect(apiModule.fetchPlaceDetail).toHaveBeenCalledWith(311);
    });
    expect(
      vi
        .mocked(apiModule.fetchPlaceDetail)
        .mock.calls.filter(([id]) => id === 311),
    ).toHaveLength(1);

    // Day 1 card: ungoverned exact_start ("09:30") hidden, stay_minutes 90 -> "建议 1.5 小时", lowest-position thumb used
    const card1 = screen.getByRole("button", { name: /1\s*重庆动物园/ });
    expect(card1).toHaveTextContent("上午 · 建议 1.5 小时");
    expect(card1).not.toHaveTextContent("09:30");
    // 推荐理由是长句，卡片上不放
    expect(card1).not.toHaveTextContent("理由一");
    const thumbImg1 = card1.querySelector("img") as HTMLImageElement;
    expect(thumbImg1).toHaveClass("photo");
    expect(thumbImg1.src).toContain("/311-0-thumb.webp");

    // Map security config + accommodation marker rendered (clickable in 7b)
    await waitFor(() => {
      expect(amapBookCalls.markers.length).toBeGreaterThanOrEqual(2);
    });
    expect((window as any)._AMapSecurityConfig).toEqual({
      securityJsCode: "test-amap-security",
    });
    const stayMarker = amapBookCalls.markers.find((m) =>
      m.opts?.content?.textContent?.includes("住这一带"),
    );
    expect(stayMarker).toBeTruthy();
    expect(stayMarker!.opts.clickable).toBe(true);

    // Switch to Day 2
    fireEvent.click(screen.getByRole("button", { name: /第二天/ }));

    await waitFor(() => {
      expect(
        screen.getByRole("heading", { name: "山城巷至千厮门大桥" }),
      ).toBeInTheDocument();
    });

    // Wait for Day 2 details
    await waitFor(() => {
      expect(apiModule.fetchPlaceDetail).toHaveBeenCalledWith(323);
    });

    // Day 2 header narrative + commute_summary
    expect(
      screen.getByText(/从山城巷老街开场，傍晚洪崖洞收束。/),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/公共交通与步行结合，总通勤约 44 分钟/),
    ).toBeInTheDocument();

    // Governed exact time on 洪崖洞 (19:30 起) & Schema 1.4 千厮门大桥 (可选, no schedule)
    const hongyaCard = screen.getByRole("button", { name: /3\s*洪崖洞/ });
    expect(hongyaCard).toHaveTextContent("傍晚 19:30 起 · 建议 1.5 小时");
    const bridgeCard = screen.getByRole("button", { name: /4\s*千厮门大桥/ });
    expect(bridgeCard).toHaveTextContent("可选");
    expect(bridgeCard).not.toHaveTextContent("上午");

    // 解放碑没有实拍：图位换成“街区”类型线描，不再在线描下重复类型字（类型在上方 .bk-kind）
    const jfbCard = screen.getByRole("button", { name: /2\s*解放碑/ });
    expect(jfbCard).toHaveAttribute("data-kind", "street");
    await waitFor(() => {
      expect(
        jfbCard.querySelector(".bk-ph.is-kind .bk-kind-mark"),
      ).not.toBeNull();
    });
    expect(jfbCard.querySelector(".bk-ph.is-kind")).toHaveTextContent("");
    expect(jfbCard.querySelector(".bk-kind")).toHaveTextContent("街区");
    expect(jfbCard).not.toHaveTextContent("暂无实拍");
    expect(jfbCard).not.toHaveTextContent("渝中商圈核心地标");

    // Map optional pin carries .opt class; sequential dashed polyline drawn
    await waitFor(() => {
      expect(document.querySelector(".gpin.opt")).not.toBeNull();
    });
    const lastPoly =
      amapBookCalls.polylines[amapBookCalls.polylines.length - 1];
    expect(lastPoly.path).toHaveLength(4);
    expect(lastPoly.strokeStyle).toBe("dashed");
    expect(lastPoly.strokeColor).toBe("#173b35");

    // Commute legs: hover step leg shows transit_steps tooltip; cycling estimate shows 估算
    const transitLegBtn = screen.getByRole("button", { name: /22 分钟.*公交/ });
    fireEvent.mouseEnter(transitLegBtn);
    const tooltip = screen.getByRole("tooltip");
    expect(tooltip).toHaveTextContent(
      "轨道交通1号线 · 较场口站上车 → 小什字站 · 2 站 · 6 分钟",
    );
    fireEvent.mouseLeave(transitLegBtn);
    expect(screen.queryByRole("tooltip")).toBeNull();

    const cyclingLegBtn = screen.getByRole("button", {
      name: /12 分钟.*骑行.*估算/,
    });
    fireEvent.focus(cyclingLegBtn);
    expect(screen.getByRole("tooltip")).toHaveTextContent(
      "线路明细暂不可用，请以现场导航为准",
    );
    fireEvent.blur(cyclingLegBtn);

    // Click map pin 3 -> highlights pin AND card, opens right StopPanel
    const pin3 = document.querySelector(
      '.gpin[data-place-id="323"]',
    ) as HTMLButtonElement;
    fireEvent.click(pin3);

    const side = screen.getByRole("complementary", { name: "洪崖洞 详情" });
    expect(side).toBeInTheDocument();
    expect(pin3.classList.contains("is-on")).toBe(true);
    expect(hongyaCard.classList.contains("is-on")).toBe(true);
    expect(scrollIntoViewSpy).toHaveBeenCalled();

    // StopPanel verifies desktop hero, district, top_reasons, body, summary, warnings, extra thumb gallery, next stop
    const heroImg = side.querySelector(".bk-side-hero") as HTMLImageElement;
    expect(heroImg).toHaveClass("photo");
    expect(heroImg.src).toContain("/323-0-desktop.webp");
    expect(side).toHaveTextContent("傍晚 19:30 起 · 建议 1.5 小时 · 渝中区");
    // 正文只放行程句；brief 是帖子摘句，不在右边正文重复
    expect(side).toHaveTextContent("从顶层平台向下逐层逛。");
    expect(side).not.toHaveTextContent("吊脚楼建筑群");
    expect(side).toHaveAttribute("data-kind", "sight");
    // 推荐理由在右边逐条列出（不截成两条）
    expect(
      [...side.querySelectorAll(".bk-reasons li")].map((li) => li.textContent),
    ).toEqual(["理由一(点323)", "理由二(点323)", "多余第三条"]);
    expect(side).toHaveTextContent("点323的百科详情介绍。");
    expect(side).toHaveTextContent("傍晚人流较多，建议错峰上楼");
    const extraGalleryImg = side.querySelector(
      ".bk-gallery img",
    ) as HTMLImageElement;
    expect(extraGalleryImg.src).toContain("/323-2-thumb.webp");
    expect(extraGalleryImg.alt).toBe("点323次图");
    expect(side).toHaveTextContent("到下一站 千厮门大桥");
    expect(side).toHaveTextContent("线路明细暂不可用，请以现场导航为准");
    expect(side.querySelector(".bk-nx")).not.toHaveTextContent("12 分钟");

    // Click last stop card -> last stop omits "到下一站"
    fireEvent.click(bridgeCard);
    const bridgeSide = screen.getByRole("complementary", {
      name: "千厮门大桥 详情",
    });
    expect(bridgeSide).not.toHaveTextContent("到下一站");

    // Close via Esc -> panel disappears and highlights clear
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("complementary")).toBeNull();
    expect(bridgeCard.classList.contains("is-on")).toBe(false);
    expect(document.querySelector(".gpin.is-on")).toBeNull();

    // Re-open via card and close via ×
    fireEvent.click(hongyaCard);
    expect(
      screen.getByRole("complementary", { name: "洪崖洞 详情" }),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "关上" }));
    expect(screen.queryByRole("complementary")).toBeNull();

    // Switch back to Day 1 -> closes drawer, and place 311 is cached (still 1 fetch!)
    fireEvent.click(screen.getByRole("button", { name: /第一天/ }));
    await waitFor(() => {
      expect(
        screen.getByRole("heading", { name: "重庆动物园全日深游" }),
      ).toBeInTheDocument();
    });
    expect(
      vi
        .mocked(apiModule.fetchPlaceDetail)
        .mock.calls.filter(([id]) => id === 311),
    ).toHaveLength(1);
  });

  it("degrades gracefully on place detail failure with manual retry and renders empty-leg dashed divider cleanly", async () => {
    const sample = makeSampleResult();
    // Remove commute leg 322->323 to test missing leg dashed-line-only rendering
    sample.plans[0].days[1].commute_legs =
      sample.plans[0].days[1].commute_legs.filter(
        (l) => !(l.from_place_id === 322 && l.to_place_id === 323),
      );
    useTripStore.getState().setResult("91500", "job_err", sample);

    let failFirst = true;
    vi.mocked(apiModule.fetchPlaceDetail).mockImplementation(async (id) => {
      if (id === 311 && failFirst) {
        throw new Error("detail api 500");
      }
      return makePlaceDetail(id, "重庆动物园");
    });

    renderBookPage("/plan/91500/plan_a?job_id=job_err");

    await waitFor(() => {
      expect(apiModule.fetchPlaceDetail).toHaveBeenCalledWith(311);
    });

    const card1 = screen.getByRole("button", { name: /1\s*重庆动物园/ });
    await waitFor(() => {
      expect(
        card1.querySelector(".bk-ph.is-kind .bk-kind-mark"),
      ).not.toBeNull();
    });
    expect(card1.querySelector(".bk-ph.is-kind")).toHaveTextContent("");
    expect(card1.querySelector(".bk-kind")).toHaveTextContent("景点");

    fireEvent.click(card1);
    const side = screen.getByRole("complementary", { name: "重庆动物园 详情" });
    expect(side).toHaveTextContent("按大熊猫馆到金鱼池顺序慢逛。");
    expect(side).not.toHaveTextContent("西南代表性动物园");
    // 没实拍：顶部是“景点”类型签，不是灰色“暂无实拍”
    expect(side.querySelector(".bk-side-hero.is-kind")).toHaveTextContent(
      "景点",
    );
    expect(side).not.toHaveTextContent("暂无实拍");
    expect(side).toHaveTextContent("详情暂时打不开");

    failFirst = false;
    fireEvent.click(screen.getByRole("button", { name: "再试一次" }));
    await waitFor(() => {
      expect(side).toHaveTextContent("重庆动物园的百科详情介绍。");
    });
    expect(side.querySelector(".bk-side-hero")).toHaveAttribute(
      "src",
      "https://img.example/311-0-desktop.webp",
    );

    // Day 2 missing leg 322->323 shows dashed empty spacer without minutes
    fireEvent.click(screen.getByRole("button", { name: /第二天/ }));
    await waitFor(() => {
      expect(document.querySelector(".bk-leg-empty")).not.toBeNull();
    });
    expect(document.querySelector(".bk-leg-empty")).toHaveTextContent("");
  });

  it("treats PLACE_UNSUPPORTED as no detail instead of a retryable failure", async () => {
    const sample = makeSampleResult();
    useTripStore.getState().setResult("91500", "job_unsup", sample);

    vi.mocked(apiModule.fetchPlaceDetail).mockImplementation(async (id) => {
      if (id === 311) {
        throw new ApiRequestError("PLACE_UNSUPPORTED", "该地点暂不支持。", 422);
      }
      return makePlaceDetail(id, "重庆动物园");
    });

    renderBookPage("/plan/91500/plan_a?job_id=job_unsup");

    const card1 = screen.getByRole("button", { name: /1\s*重庆动物园/ });
    await waitFor(() => {
      expect(
        card1.querySelector(".bk-ph.is-kind .bk-kind-mark"),
      ).not.toBeNull();
    });
    expect(card1.querySelector(".bk-ph.is-kind")).toHaveTextContent("");
    expect(card1.querySelector(".bk-kind")).toHaveTextContent("景点");

    fireEvent.click(card1);
    const side = screen.getByRole("complementary", { name: "重庆动物园 详情" });
    expect(side).toHaveTextContent("按大熊猫馆到金鱼池顺序慢逛。");
    expect(side).not.toHaveTextContent("西南代表性动物园");
    // 没实拍：顶部是“景点”类型签，不是灰色“暂无实拍”
    expect(side.querySelector(".bk-side-hero.is-kind")).toHaveTextContent(
      "景点",
    );
    expect(side).not.toHaveTextContent("暂无实拍");
    expect(side).not.toHaveTextContent("详情暂时打不开");
    expect(screen.queryByRole("button", { name: "再试一次" })).toBeNull();
    expect(
      vi
        .mocked(apiModule.fetchPlaceDetail)
        .mock.calls.filter(([id]) => id === 311),
    ).toHaveLength(1);
  });

  it("supports missing AMap key / SDK failure fallback and prefers-reduced-motion immediate day switch", async () => {
    const sample = makeSampleResult();
    useTripStore.getState().setResult("91500", "job_map", sample);

    // 1. Reduced motion -> setFitView called with immediately = true on day switch
    mockMatchMedia(true);
    const rReduced = renderBookPage("/plan/91500/plan_a?job_id=job_map");
    await waitFor(() => {
      expect(amapBookCalls.fitViewCalls.length).toBeGreaterThanOrEqual(1);
    });
    fireEvent.click(screen.getByRole("button", { name: /第二天/ }));
    await waitFor(() => {
      expect(amapBookCalls.fitViewCalls.length).toBeGreaterThanOrEqual(2);
    });
    const fitCall =
      amapBookCalls.fitViewCalls[amapBookCalls.fitViewCalls.length - 1];
    expect(fitCall.immediately).toBe(true);
    expect(fitCall.avoid).toEqual([90, 275, 380, 140]);
    rReduced.unmount();

    // 2. Missing VITE_AMAP_KEY -> paper fallback without blocking DayStrip or StopPanel
    vi.stubEnv("VITE_AMAP_KEY", "");
    const rNoKey = renderBookPage("/plan/91500/plan_a?job_id=job_map");
    expect(await screen.findByTestId("bk-map-fallback")).toBeInTheDocument();
    const card = screen.getByRole("button", { name: /1\s*重庆动物园/ });
    fireEvent.click(card);
    expect(
      screen.getByRole("complementary", { name: "重庆动物园 详情" }),
    ).toBeInTheDocument();
    rNoKey.unmount();

    // 3. AMapLoader reject -> paper fallback
    vi.stubEnv("VITE_AMAP_KEY", "test-key");
    vi.mocked(AMapLoader.load).mockRejectedValueOnce(new Error("blocked"));
    renderBookPage("/plan/91500/plan_a?job_id=job_map");
    await waitFor(() => {
      expect(screen.getByTestId("bk-map-fallback")).toBeInTheDocument();
    });
  });

  it("renders >10 days tabs with numeric fallback labels after 第十天", async () => {
    const baseDay = makeSampleResult().plans[0].days[0];
    const twelveDays = Array.from({ length: 12 }, (_, idx) => ({
      ...baseDay,
      day: idx + 1,
      title: `第${idx + 1}日主题`,
    }));
    const sample = makeSampleResult({
      request: { days: 12, people_count: 1, preferences: [], avoid: [] },
    });
    sample.plans[0].days = twelveDays;
    useTripStore.getState().setResult("91500", "job_12d", sample);

    renderBookPage("/plan/91500/plan_a?job_id=job_12d");
    expect(screen.getByRole("button", { name: /第十天/ })).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /第 11 天/ }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /第 12 天/ }),
    ).toBeInTheDocument();

    await waitFor(() => {
      expect(apiModule.fetchPlaceDetail).toHaveBeenCalledWith(311);
      expect(amapBookCalls.markers.length).toBeGreaterThanOrEqual(1);
    });
  });

  it("nudges pin clear of StopPanel including .gpl label width and renders gallery safely without asset_id", async () => {
    const sample = makeSampleResult();
    useTripStore.getState().setResult("91500", "job_nudge", sample);
    amapBookCalls.lngLatContainerResult = { x: 980, y: 360 };
    const origInnerWidth = window.innerWidth;
    Object.defineProperty(window, "innerWidth", {
      writable: true,
      configurable: true,
      value: 1440,
    });

    const errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    vi.mocked(apiModule.fetchPlaceDetail).mockImplementation(async (id) => ({
      ...makePlaceDetail(id, `景点${id}`),
      gallery: [
        {
          asset_id: "",
          position: 0,
          alt_text: "主图",
          desktop: {
            url: `https://img.example/${id}-0.webp`,
            width: 1200,
            height: 800,
          },
          mobile: {
            url: `https://img.example/${id}-0.webp`,
            width: 800,
            height: 600,
          },
          thumb: {
            url: `https://img.example/${id}-0.webp`,
            width: 300,
            height: 225,
          },
        },
        {
          asset_id: undefined as any,
          position: 1,
          alt_text: "次图一",
          desktop: {
            url: `https://img.example/${id}-1.webp`,
            width: 1200,
            height: 800,
          },
          mobile: {
            url: `https://img.example/${id}-1.webp`,
            width: 800,
            height: 600,
          },
          thumb: {
            url: `https://img.example/${id}-1.webp`,
            width: 300,
            height: 225,
          },
        },
        {
          asset_id: undefined as any,
          position: 2,
          alt_text: "次图二",
          desktop: {
            url: `https://img.example/${id}-2.webp`,
            width: 1200,
            height: 800,
          },
          mobile: {
            url: `https://img.example/${id}-2.webp`,
            width: 800,
            height: 600,
          },
          thumb: {
            url: `https://img.example/${id}-2.webp`,
            width: 300,
            height: 225,
          },
        },
      ],
    }));

    try {
      renderBookPage("/plan/91500/plan_a?job_id=job_nudge");
      await waitFor(() => {
        expect(amapBookCalls.markers.length).toBeGreaterThanOrEqual(1);
      });

      const card = screen.getByRole("button", { name: /1\s*重庆动物园/ });
      fireEvent.click(card);

      await waitFor(() => {
        expect(
          screen.getByRole("complementary", { name: "重庆动物园 详情" }),
        ).toBeInTheDocument();
        expect(amapBookCalls.panToCalls.length).toBeGreaterThanOrEqual(1);
      });

      await waitFor(() => {
        expect(document.querySelectorAll(".bk-gallery img")).toHaveLength(2);
      });
      const keyWarnings = errorSpy.mock.calls.filter((args) =>
        String(args[0]).includes('unique "key" prop'),
      );
      expect(keyWarnings).toHaveLength(0);
    } finally {
      errorSpy.mockRestore();
      Object.defineProperty(window, "innerWidth", {
        writable: true,
        configurable: true,
        value: origInnerWidth,
      });
    }
  });

  it("folds and unfolds DayStrip without persisting state, and tracks has-stop for StopPanel", async () => {
    const sample = makeSampleResult();
    useTripStore.getState().setResult("91500", "job_fold_toggle", sample);

    const rendered = renderBookPage(
      "/plan/91500/plan_a?job_id=job_fold_toggle",
    );
    await waitFor(() => {
      expect(amapBookCalls.fitViewCalls.length).toBeGreaterThanOrEqual(1);
    });

    const atlas = document.querySelector("main.bk-atlas") as HTMLElement;
    const strip = screen.getByRole("region", { name: "这一天" });
    const foldBtn = screen.getByRole("button", { name: "收起这一天" });

    expect(foldBtn).toHaveAttribute("aria-expanded", "true");
    expect(strip).not.toHaveClass("is-folded");

    // 点击收起 -> is-folded + aria-expanded=false + 卡片区 aria-hidden
    fireEvent.click(foldBtn);
    expect(strip).toHaveClass("is-folded");
    const unfoldBtn = screen.getByRole("button", { name: "展开这一天" });
    expect(unfoldBtn).toHaveAttribute("aria-expanded", "false");
    expect(strip.querySelector(".bk-dcards")).toHaveAttribute(
      "aria-hidden",
      "true",
    );

    // 再次点击展开 -> 恢复展开态
    fireEvent.click(unfoldBtn);
    expect(strip).not.toHaveClass("is-folded");
    expect(screen.getByRole("button", { name: "收起这一天" })).toHaveAttribute(
      "aria-expanded",
      "true",
    );

    // 打开地点详情（StopPanel）：atlas 挂 has-stop（不进 reading），关掉后清除
    fireEvent.click(screen.getByRole("button", { name: /1\s*重庆动物园/ }));
    expect(atlas).toHaveClass("has-stop");
    expect(atlas).not.toHaveClass("is-reading");
    expect(strip).not.toHaveClass("is-collapsed");
    fireEvent.click(screen.getByRole("button", { name: "关上" }));
    expect(atlas).not.toHaveClass("has-stop");

    // 收起状态不持久化：收起后重新打开页面应回到展开态
    fireEvent.click(screen.getByRole("button", { name: "收起这一天" }));
    expect(strip).toHaveClass("is-folded");
    rendered.unmount();

    renderBookPage("/plan/91500/plan_a?job_id=job_fold_toggle");
    expect(screen.getByRole("region", { name: "这一天" })).not.toHaveClass(
      "is-folded",
    );
    expect(screen.getByRole("button", { name: "收起这一天" })).toHaveAttribute(
      "aria-expanded",
      "true",
    );
  });

  it("auto-unfolds DayStrip when clicking any day tab while folded", async () => {
    const sample = makeSampleResult();
    useTripStore.getState().setResult("91500", "job_fold_tab", sample);

    renderBookPage("/plan/91500/plan_a?job_id=job_fold_tab");
    await waitFor(() => {
      expect(amapBookCalls.fitViewCalls.length).toBeGreaterThanOrEqual(1);
    });

    const strip = screen.getByRole("region", { name: "这一天" });
    fireEvent.click(screen.getByRole("button", { name: "收起这一天" }));
    expect(strip).toHaveClass("is-folded");

    // 收起状态下点另一天的页签（第二天）：切到第二天并自动展开
    fireEvent.click(screen.getByRole("button", { name: /第二天/ }));
    expect(strip).not.toHaveClass("is-folded");
    expect(screen.getByRole("button", { name: "收起这一天" })).toHaveAttribute(
      "aria-expanded",
      "true",
    );
    expect(
      screen.getByRole("heading", { name: "山城巷至千厮门大桥" }),
    ).toBeInTheDocument();

    // 再收起，点当前天（第二天）的页签：同样自动展开
    fireEvent.click(screen.getByRole("button", { name: "收起这一天" }));
    expect(strip).toHaveClass("is-folded");
    fireEvent.click(screen.getByRole("button", { name: /第二天/ }));
    expect(strip).not.toHaveClass("is-folded");
    expect(screen.getByRole("button", { name: "收起这一天" })).toHaveAttribute(
      "aria-expanded",
      "true",
    );
  });

  it("preserves DayStrip fold state across opening and closing reading pages", async () => {
    const sample = makeSampleResult();
    useTripStore.getState().setResult("91500", "job_fold_reading", sample);

    renderBookPage("/plan/91500/plan_a?job_id=job_fold_reading");
    await waitFor(() => {
      expect(amapBookCalls.fitViewCalls.length).toBeGreaterThanOrEqual(1);
    });

    const atlas = document.querySelector("main.bk-atlas") as HTMLElement;
    const strip = screen.getByRole("region", { name: "这一天" });

    // 收起状态下打开四页之一（出发前）：长卷整个收下去，关掉后回到原来的收起状态
    fireEvent.click(screen.getByRole("button", { name: "收起这一天" }));
    expect(strip).toHaveClass("is-folded");

    const beforeBtn = screen.getByRole("button", { name: "出发前" });
    fireEvent.click(beforeBtn);
    expect(atlas).toHaveClass("is-reading");
    expect(strip).toHaveClass("is-collapsed");

    fireEvent.click(beforeBtn);
    expect(atlas).not.toHaveClass("is-reading");
    expect(strip).not.toHaveClass("is-collapsed");
    expect(strip).toHaveClass("is-folded");
    expect(screen.getByRole("button", { name: "展开这一天" })).toHaveAttribute(
      "aria-expanded",
      "false",
    );

    // 展开状态下打开“花多少”再关掉：回到原来的展开状态
    fireEvent.click(screen.getByRole("button", { name: "展开这一天" }));
    expect(strip).not.toHaveClass("is-folded");

    const costBtn = screen.getByRole("button", { name: "花多少" });
    fireEvent.click(costBtn);
    expect(atlas).toHaveClass("is-reading");
    fireEvent.click(costBtn);
    expect(atlas).not.toHaveClass("is-reading");
    expect(strip).not.toHaveClass("is-folded");
  });

  it("syncs CSS strip variables and map avoid padding dynamically with measured DayStrip height", async () => {
    const sample = makeSampleResult();
    useTripStore.getState().setResult("91500", "job_fold_avoid", sample);

    const origInnerWidth = window.innerWidth;
    const origInnerHeight = window.innerHeight;
    Object.defineProperty(window, "innerWidth", {
      writable: true,
      configurable: true,
      value: 1440,
    });
    Object.defineProperty(window, "innerHeight", {
      writable: true,
      configurable: true,
      value: 900,
    });

    const offsetHeightGetter = vi
      .spyOn(HTMLElement.prototype, "offsetHeight", "get")
      .mockImplementation(function (this: HTMLElement) {
        if (this.classList.contains("bk-day")) {
          return this.classList.contains("is-folded") ? 180 : 216;
        }
        if (this.classList.contains("bk-dh")) {
          return this.closest(".bk-day")?.classList.contains("is-folded")
            ? 52
            : 88;
        }
        return 0;
      });
    const offsetWidthGetter = vi
      .spyOn(HTMLElement.prototype, "offsetWidth", "get")
      .mockImplementation(function (this: HTMLElement) {
        if (this.classList.contains("bk-side")) {
          return this.closest(".bk-atlas")?.classList.contains("is-reading")
            ? 540
            : 380;
        }
        return 0;
      });

    try {
      renderBookPage("/plan/91500/plan_a?job_id=job_fold_avoid");
      await waitFor(() => {
        expect(amapBookCalls.fitViewCalls.length).toBeGreaterThanOrEqual(1);
      });

      const atlas = document.querySelector("main.bk-atlas") as HTMLElement;
      const strip = screen.getByRole("region", { name: "这一天" });

      // 初始展开态：读实际长卷高 216px -> 避让底部 216 + 16 + 44 = 276，CSS 变量同步
      expect(atlas.style.getPropertyValue("--bk-strip-vis")).toBe("216px");
      expect(atlas.style.getPropertyValue("--bk-strip-clear")).toBe("232px");
      expect(strip.style.getPropertyValue("--bk-fold")).toBe("0px");
      expect(
        amapBookCalls.fitViewCalls[amapBookCalls.fitViewCalls.length - 1].avoid,
      ).toEqual([90, 276, 380, 140]);

      // 点收起：--bk-fold = 180 - 52 + 16 = 144px（贴底），地图避让底部随高度缩成 52 + 44 = 96
      fireEvent.click(screen.getByRole("button", { name: "收起这一天" }));
      expect(strip.style.getPropertyValue("--bk-fold")).toBe("144px");
      expect(atlas.style.getPropertyValue("--bk-strip-vis")).toBe("52px");
      expect(atlas.style.getPropertyValue("--bk-strip-clear")).toBe("52px");
      await waitFor(() => {
        expect(
          amapBookCalls.fitViewCalls[amapBookCalls.fitViewCalls.length - 1]
            .avoid,
        ).toEqual([90, 96, 380, 140]);
      });

      // 打开四页之一（出发前）：长卷高度变量归 0，地图避让切换为阅读模式 [90, 70, 380, 600]
      fireEvent.click(screen.getByRole("button", { name: "出发前" }));
      expect(atlas.style.getPropertyValue("--bk-strip-vis")).toBe("0px");
      expect(atlas.style.getPropertyValue("--bk-strip-clear")).toBe("0px");
      expect(
        amapBookCalls.fitViewCalls[amapBookCalls.fitViewCalls.length - 1].avoid,
      ).toEqual([90, 70, 380, 600]);
    } finally {
      offsetHeightGetter.mockRestore();
      offsetWidthGetter.mockRestore();
      Object.defineProperty(window, "innerWidth", {
        writable: true,
        configurable: true,
        value: origInnerWidth,
      });
      Object.defineProperty(window, "innerHeight", {
        writable: true,
        configurable: true,
        value: origInnerHeight,
      });
    }
  });

  it("scrolls active day tab into view when day tabs overflow horizontally", async () => {
    const sample = makeSampleResult();
    useTripStore.getState().setResult("91500", "job_tab_scroll", sample);

    const offsetWidthGetter = vi
      .spyOn(HTMLElement.prototype, "offsetWidth", "get")
      .mockImplementation(function (this: HTMLElement) {
        return this.classList.contains("bk-dtab") ? 120 : 0;
      });

    try {
      renderBookPage("/plan/91500/plan_a?job_id=job_tab_scroll");
      await waitFor(() => {
        expect(amapBookCalls.fitViewCalls.length).toBeGreaterThanOrEqual(1);
      });

      const strip = screen.getByRole("region", { name: "这一天" });
      const tabsBox = strip.querySelector(".bk-dtabs") as HTMLElement;
      const day2Tab = tabsBox.querySelector(
        '.bk-dtab[data-day="2"]',
      ) as HTMLElement;
      const scrollToSpy = vi.fn();
      Object.defineProperty(tabsBox, "scrollWidth", {
        configurable: true,
        value: 400,
      });
      Object.defineProperty(tabsBox, "clientWidth", {
        configurable: true,
        value: 160,
      });
      Object.defineProperty(tabsBox, "offsetLeft", {
        configurable: true,
        value: 8,
      });
      Object.defineProperty(day2Tab, "offsetLeft", {
        configurable: true,
        value: 208,
      });
      tabsBox.scrollLeft = 0;
      tabsBox.scrollTo = scrollToSpy;

      // 切到第二天 -> 第二天超右边界，自动横向滚进视野
      scrollToSpy.mockClear();
      fireEvent.click(screen.getByRole("button", { name: /第二天/ }));
      expect(scrollToSpy).toHaveBeenCalledWith(
        expect.objectContaining({ left: 168 }),
      );
    } finally {
      offsetWidthGetter.mockRestore();
    }
  });
});
