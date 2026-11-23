/* eslint-disable @typescript-eslint/no-explicit-any */
import fs from "node:fs";
import path from "node:path";
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import SpreadBookPage from "@/pages/book/SpreadBookPage";
import {
  COST_ESTIMATE_COMPLETE,
  COST_ESTIMATE_MIXED_POLICY_ZERO,
  COST_ESTIMATE_PARTIAL,
  COST_ESTIMATE_SINGLE_TRAIN,
  COST_ESTIMATE_UNAVAILABLE,
} from "@/fixtures/v0.9.4-fixtures";
import * as apiModule from "@/services/api";
import { ApiRequestError } from "@/services/api";
import { useShareImageTaskStore } from "@/stores/shareImageTaskStore";
import { useToastStore } from "@/stores/toastStore";
import { useTripStore } from "@/stores/tripStore";
import type {
  AccommodationInfo,
  Artifact,
  LodgingDisplay,
  LodgingDisplayGroup,
  PlaceDetail,
  TripDay,
  TripMustInclude,
  TripResult,
  TripTransport,
  TripWeather,
} from "@/types/trip";
import { saveBlob } from "@/utils/download";
import {
  formatCostEstimatedDate,
  formatMustIncludeStatusSuffix,
  formatTransportCheckedTime,
  formatTransportDuration,
  formatWeatherMonthDay,
  filterUnscheduledMustInclude,
  getLatestTransportCheckedTime,
  getWeatherUnavailableHint,
  pickDefaultCostScenarioId,
  resolveBeforeWeatherSection,
  resolveBookCostBreakdownVisuals,
  resolveBookCostCategoryRows,
  resolveBookLodgingPresentation,
  resolveCostPriceBasisLabel,
  resolveTransportFooterNote,
  shouldKeepSidePageOnDaySwitch,
  splitScenarioCostText,
} from "@/utils/spreadBook";

const mapSpy = vi.hoisted(() => ({
  markers: [] as Array<{ opts: any; clicks: Array<() => void> }>,
  panToCalls: [] as any[],
  setCenterCalls: [] as Array<{ center: any; immediately?: boolean }>,
  lngLatContainerResult: { x: 680, y: 320 },
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
        setMap = vi.fn();
      }
      class MockMarker {
        opts: any;
        clicks: Array<() => void> = [];
        constructor(opts: any) {
          this.opts = opts;
          mapSpy.markers.push({ opts, clicks: this.clicks });
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
        constructor(container: HTMLElement) {
          this.container = container;
        }
        setFitView = vi.fn();
        lngLatToContainer = vi.fn(
          () =>
            new MockPixel(
              mapSpy.lngLatContainerResult.x,
              mapSpy.lngLatContainerResult.y,
            ),
        );
        containerToLngLat = vi.fn((px: any) => [
          106.5 + (px?.x ?? 0) * 0.0001,
          29.5 + (px?.y ?? 0) * 0.0001,
        ]);
        panTo = vi.fn((center: any) => {
          mapSpy.panToCalls.push(center);
        });
        setCenter = vi.fn((center: any, immediately?: boolean) => {
          mapSpy.setCenterCalls.push({ center, immediately });
        });
        resize = vi.fn();
        destroy = vi.fn();
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
    getArtifact: vi.fn(),
    createArtifact: vi.fn(),
    fetchArtifactBlob: vi.fn(),
  };
});

vi.mock("@/utils/download", () => ({
  saveBlob: vi.fn(),
}));

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
    summary: `${name}的百科详情。`,
    top_reasons: [`值得看(${name})`],
    warnings: [],
    source_count: 8,
    mention_count: 20,
    gallery: [],
  };
}

const SAMPLE_TIERS: LodgingDisplayGroup[] = [
  {
    kind: "economy",
    label: "经济",
    hotels: ["如家酒店·neo", "庭泊·别院酒店"],
    nightly_low_cny: 90,
    nightly_high_cny: 110,
  },
  {
    kind: "mid",
    label: "中档",
    hotels: ["银河大酒店", "宾顿朗逸酒店"],
    nightly_low_cny: 270,
    nightly_high_cny: 540,
  },
  {
    kind: "high",
    label: "高档",
    hotels: ["重庆国贸格兰大酒店"],
    nightly_low_cny: 690,
    nightly_high_cny: 830,
  },
];

function makeSampleTransport(
  overrides?: Partial<TripTransport>,
): TripTransport {
  return {
    from_city: "上海",
    to_city: "重庆",
    query_date: "2026-11-14",
    source: "mixed",
    modes: [
      {
        mode: "train",
        min_duration_minutes: 620,
        price_range: "¥680–860",
        price_source: "realtime",
        daily_count: 14,
        data_source: "realtime",
        availability_status: "available_at_query",
        availability_checked_at: "2026-11-10T04:15:00Z",
        options: [
          {
            type: "train",
            no: "G1974",
            departure_time: "07:38",
            arrival_time: "17:58",
            duration_minutes: 620,
            price: "¥736",
            departure_station: "上海虹桥",
            arrival_station: "重庆北",
            airline: null,
          },
        ],
      },
      {
        mode: "flight",
        min_duration_minutes: 165,
        price_range: "¥540–1,200",
        price_source: "static_reference",
        daily_count: 22,
        data_source: "static_fallback",
        availability_status: "sold_out_at_query",
        availability_checked_at: "2026-11-10T06:30:00Z",
        options: [
          {
            type: "flight",
            no: "MU5421",
            departure_time: "09:15",
            arrival_time: "12:00",
            duration_minutes: 165,
            price: "¥690",
            departure_station: "浦东T1",
            arrival_station: "江北T3",
            airline: "东方航空",
          },
        ],
      },
    ],
    ...overrides,
  };
}

function makeRichResult(overrides?: Partial<TripResult>): TripResult {
  const days: TripDay[] = [
    {
      day: 1,
      title: "重庆动物园全日深游",
      narrative: "第一天慢慢逛。",
      commute_summary: "园区步行",
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
          schedule: {
            period: "morning",
            exact_start: null,
            exact_end: null,
            exact_time_source: null,
          },
        },
      ],
      commute_legs: [],
    },
    {
      day: 2,
      title: "山城巷与解放碑",
      narrative: "第二天老街慢走。",
      commute_summary: "步行与轨道交通结合",
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
          brief: "老城步道",
        },
        {
          place_id: 322,
          name: "解放碑",
          category: "business_area",
          longitude: 106.5784,
          latitude: 29.5574,
          role: "secondary_activity",
          optional: false,
          brief: "商圈地标",
        },
      ],
      commute_legs: [
        {
          from_place_id: 321,
          to_place_id: 322,
          mode: "walking",
          duration_source: "amap",
          duration_minutes: 10,
          distance_meters: 750,
        },
      ],
    },
  ];

  const base: TripResult = {
    schema_version: "2.3",
    result_id: 91500,
    city: { name: "重庆" },
    request: {
      days: 2,
      people_count: 2,
      preferences: ["citywalk"],
      avoid: [],
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
          reminders: ["带一把折叠伞"],
        },
        {
          day: 2,
          date: "2026-11-15",
          weather_text: "晴",
          temp_min_c: 15,
          temp_max_c: 23,
          wind_text: "",
          icon_code: "sunny",
          reminders: [],
        },
      ],
    },
    must_include: [
      { name: "重庆动物园", status: "scheduled", place_id: 311, reason: null },
      {
        name: "磁器口夜景",
        status: "not_scheduled",
        reason: "时间预算未能容纳",
      },
      {
        name: "武隆天生三桥",
        status: "cross_city",
        reason: "地点超出本次重庆市内范围",
      },
    ],
    plans: [
      {
        plan_id: "plan_a",
        title: "山城慢逛两日",
        summary: "动物园与渝中半岛漫游",
        tags: ["citywalk", "轻松"],
        pace: {
          level: "RELAXED",
          commute_status: "WITHIN_LIMIT",
          total_commute_minutes: 30,
        },
        transport: makeSampleTransport(),
        accommodation: {
          name: "解放碑商圈",
          latitude: 29.558,
          longitude: 106.577,
          source: "auto_recommended",
          reason: "离前两天的景点都近，每天来回约 25 分钟",
          display: {
            layout: "tiers",
            area_label: "解放碑一带",
            caption: "",
            groups: SAMPLE_TIERS,
            round_trip_minutes_low: null,
            round_trip_minutes_high: null,
            notices: [
              {
                kind: "user_lodging_unmatched",
                text: "没找到你填的住处「某客栈」，已改按解放碑一带推荐",
              },
              {
                kind: "holiday",
                text: "假期热门区域房价通常上浮，建议尽早预订",
              },
            ],
          },
        },
        packing_checklist: [
          { category: "衣物", items: ["薄外套", "防滑徒步鞋"] },
          { category: "日用", items: ["折叠伞", "  "] },
          { category: "  ", items: ["会被过滤"] },
        ],
        travel_tips: [
          { title: "山城坡道多", content: "尽量穿抓地好、平底防滑的鞋子" },
          { title: "  ", content: "空标题不展示" },
        ],
        cost_estimate: COST_ESTIMATE_COMPLETE,
        days,
      },
    ],
  };

  return { ...base, ...overrides };
}

function makeReadyArtifact(
  type: "pdf" | "share_image",
  overrides?: Partial<Artifact>,
): Artifact {
  return {
    ok: true,
    artifact_id: `art-${type}`,
    result_record_id: 91500,
    artifact_type: type,
    status: "ready",
    download_url: `/api/trip/results/91500/artifacts/${type}/download`,
    filename: type === "pdf" ? "plan.pdf" : "poster.png",
    mime_type: type === "pdf" ? "application/pdf" : "image/png",
    byte_size: 2048,
    page_count: type === "pdf" ? 4 : null,
    width_px: type === "share_image" ? 1080 : null,
    height_px: type === "share_image" ? 1920 : null,
    expires_time: "",
    metadata: {},
    error: null,
    ...overrides,
  };
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
          <Route path="/login" element={<div>login-page</div>} />
          <Route path="/history" element={<div>history-page</div>} />
        </Routes>
      </MemoryRouter>
    </div>,
  );
}

describe("Step 7b pure helpers", () => {
  it("handles day switch retention and must_include formatting", () => {
    expect(shouldKeepSidePageOnDaySwitch("guide")).toBe(true);
    expect(shouldKeepSidePageOnDaySwitch("before")).toBe(true);
    expect(shouldKeepSidePageOnDaySwitch("go")).toBe(true);
    expect(shouldKeepSidePageOnDaySwitch("cost")).toBe(true);
    expect(shouldKeepSidePageOnDaySwitch("miss")).toBe(true);
    expect(shouldKeepSidePageOnDaySwitch("stay")).toBe(false);
    expect(shouldKeepSidePageOnDaySwitch(null)).toBe(false);

    const items: TripMustInclude[] = [
      { name: "已排", status: "scheduled" },
      { name: "未排", status: "not_scheduled", reason: "太远" },
      { name: "跨城", status: "cross_city", reason: "在外市" },
      { name: "未匹配", status: "recorded_unmatched" },
      { name: "候选", status: "recorded_candidate" },
    ];
    expect(filterUnscheduledMustInclude(items)).toHaveLength(4);
    expect(filterUnscheduledMustInclude(null)).toEqual([]);
    expect(formatMustIncludeStatusSuffix("not_scheduled")).toBe("");
    expect(formatMustIncludeStatusSuffix("cross_city")).toBe(" (不在本城市)");
    expect(formatMustIncludeStatusSuffix("recorded_unmatched")).toBe(
      " (未匹配到对应地点)",
    );
    expect(formatMustIncludeStatusSuffix("recorded_candidate")).toBe(
      " (候选地点备用)",
    );
  });

  it("handles pre-trip weather formatting, missing days, non-ok hints and extreme heat reminder", () => {
    expect(formatWeatherMonthDay("2026-11-14")).toBe("11月14日");
    expect(formatWeatherMonthDay("2026-06-05T08:00:00Z")).toBe("6月5日");
    expect(formatWeatherMonthDay("  ")).toBeNull();
    expect(formatWeatherMonthDay("下周二")).toBe("下周二");

    expect(getWeatherUnavailableHint("skipped_date_out_of_range")).toBe(
      "出发日期较远，临近出行时可查看天气预报",
    );
    expect(getWeatherUnavailableHint("skipped_disabled")).toBe(
      "天气服务暂未开启",
    );
    expect(getWeatherUnavailableHint("failed")).toBe(
      "天气数据获取失败，请稍后再试",
    );
    expect(getWeatherUnavailableHint("other_unknown")).toBe("暂无天气数据");

    const baseDays = makeRichResult().plans[0].days;
    const thirdDay: TripDay = { ...baseDays[0], day: 3, title: "第三天" };
    const weather: TripWeather = {
      status: "ok",
      city: "重庆",
      days: [
        {
          day: 1,
          date: "2026-11-14",
          weather_text: "晴",
          temp_min_c: 20,
          temp_max_c: 41,
          wind_text: "",
          icon_code: "sunny",
          reminders: ["多喝水", "多喝水"],
        },
        // day 2 缺省
      ],
    };

    const section = resolveBeforeWeatherSection(
      [...baseDays, thirdDay],
      weather,
    );
    expect(section.ok).toBe(true);
    if (section.ok) {
      expect(section.items[0]).toMatchObject({
        kind: "forecast",
        title: "第一天 · 11月14日",
        tempRange: "20–41°C",
        windText: null,
        reminders: ["多喝水", "第1天有 41°C 极端高温，请做好防暑降温"],
      });
      expect(section.items[1]).toEqual({
        kind: "missing",
        day: 2,
        title: "第二天 · 暂无预报",
      });
      expect(section.items[2]).toEqual({
        kind: "missing",
        day: 3,
        title: "第三天 · 暂无预报",
      });
    }

    expect(
      resolveBeforeWeatherSection(baseDays, {
        status: "skipped_disabled",
        city: "重庆",
        days: [],
      }),
    ).toEqual({ ok: false, hint: "天气服务暂未开启" });
  });

  it("formats transport durations, checked times, footer notes and lodging display models", () => {
    expect(formatTransportDuration(0)).toBe("0分");
    expect(formatTransportDuration(45)).toBe("45分");
    expect(formatTransportDuration(120)).toBe("2小时");
    expect(formatTransportDuration(165)).toBe("2小时45分");

    expect(formatTransportCheckedTime("invalid")).toBeNull();
    const tr = makeSampleTransport();
    expect(getLatestTransportCheckedTime(tr)).toBe(
      formatTransportCheckedTime("2026-11-10T06:30:00Z"),
    );
    expect(resolveTransportFooterNote(tr)).toBe("实时推荐方案");
    expect(
      resolveTransportFooterNote({ ...tr, source: "static_fallback" }),
    ).toBe("静态估算，班次与价格以购票平台为准");
    expect(resolveTransportFooterNote({ ...tr, query_date: null })).toBe(
      "静态估算，班次与价格以购票平台为准",
    );

    // Lodging variants
    expect(resolveBookLodgingPresentation(null)).toBeNull();
    expect(
      resolveBookLodgingPresentation({
        name: "古城南门",
        latitude: 29.5,
        longitude: 106.5,
        source: "auto_recommended",
        reason: null,
      }),
    ).toEqual({ kind: "legacy", title: "古城南门", reason: null });

    const userLodging: LodgingDisplay = {
      layout: "user_lodging",
      area_label: "解放碑观景酒店",
      groups: [],
      round_trip_minutes_low: 30,
      round_trip_minutes_high: 30,
      notices: [],
    };
    expect(
      resolveBookLodgingPresentation({
        name: "解放碑观景酒店",
        latitude: 29.5,
        longitude: 106.5,
        source: "user_specified",
        display: userLodging,
      }),
    ).toEqual({
      kind: "user_lodging",
      text: "从你住的解放碑观景酒店出发，每天来回约30分钟",
    });

    const partialPriceAcc: AccommodationInfo = {
      name: "解放碑片区",
      latitude: 29.55,
      longitude: 106.57,
      source: "auto_recommended",
      display: {
        layout: "tiers",
        area_label: "解放碑一带",
        groups: [
          {
            kind: "economy",
            label: "经济",
            hotels: ["A酒店", "  ", "B酒店"],
            nightly_low_cny: 120,
            nightly_high_cny: null,
          },
        ],
        notices: [],
      },
    };
    expect(resolveBookLodgingPresentation(partialPriceAcc)).toMatchObject({
      kind: "display",
      tiers: [{ label: "经济", priceText: null, hotelsText: "A酒店、B酒店" }],
      showPriceNote: false,
    });
  });

  it("handles cost scenario defaulting, category ordering, policy_zero, missing and date formatting", () => {
    expect(pickDefaultCostScenarioId([])).toBeNull();
    expect(
      pickDefaultCostScenarioId([
        COST_ESTIMATE_COMPLETE.scenarios[1],
        COST_ESTIMATE_COMPLETE.scenarios[0],
      ]),
    ).toBe("train_round_trip");
    expect(
      pickDefaultCostScenarioId([COST_ESTIMATE_PARTIAL.scenarios[0]]),
    ).toBe("without_intercity");

    expect(resolveCostPriceBasisLabel("sourced")).toBe("来源查价");
    expect(resolveCostPriceBasisLabel("reference")).toBe("参考估算");
    expect(resolveCostPriceBasisLabel("mixed")).toBe("来源与参考混合");
    expect(resolveCostPriceBasisLabel("policy_zero")).toBe("按规则计为 ¥0");
    expect(resolveCostPriceBasisLabel(null)).toBeNull();

    const rows = resolveBookCostCategoryRows(
      COST_ESTIMATE_MIXED_POLICY_ZERO.scenarios[0],
    );
    expect(rows.map((r) => [r.label, r.amountText, r.basisLabel])).toEqual([
      ["大交通", "¥ 300", "来源与参考混合"],
      ["住宿", "¥ 500", "参考估算"],
      ["市内交通", "¥ 0", "按规则计为 ¥0"],
      ["景点门票", "待确认", null],
      ["餐饮", "待确认", null],
    ]);

    expect(formatCostEstimatedDate("invalid")).toBeNull();
    expect(formatCostEstimatedDate("2026-08-04T12:00:00Z")).toBe("2026-08-04");
  });
});

describe("SpreadBook Step 7b UI interactions, drawers, map stay sync & artifacts", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    mapSpy.markers = [];
    mapSpy.panToCalls = [];
    mapSpy.setCenterCalls = [];
    mapSpy.lngLatContainerResult = { x: 680, y: 320 };

    useTripStore.getState().reset();
    useToastStore.getState().hideToast();
    useShareImageTaskStore.getState().clearAllTasks();
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

  it("renders top-right bar, opens/closes via toggle, × and Esc, enforces mutual exclusion & day switch retention", async () => {
    const sample = makeRichResult();
    useTripStore.getState().setResult("91500", "job_7b_1", sample);

    renderBookPage("/plan/91500/plan_a?job_id=job_7b_1");

    await waitFor(() => {
      expect(
        document.querySelector('button.gstay[aria-label="查看住处详情"]'),
      ).not.toBeNull();
    });

    // 右上一排：7c 在最前加入“攻略”，后面是出发前、怎么去 · 住哪、花多少、导出 PDF、分享长图
    const guideBtn = screen.getByRole("button", { name: "攻略" });
    const beforeBtn = screen.getByRole("button", { name: "出发前" });
    const goBtn = screen.getByRole("button", { name: "怎么去 · 住哪" });
    const costBtn = screen.getByRole("button", { name: "花多少" });
    expect(
      screen.getByRole("button", { name: "导出 PDF" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "分享长图" }),
    ).toBeInTheDocument();
    const barButtons = Array.from(
      document.querySelectorAll(".bk-bar button"),
    ).map((b) => b.textContent?.trim());
    expect(barButtons[0]).toBe("攻略");
    expect(guideBtn).toHaveAttribute("aria-pressed", "false");

    const atlas = document.querySelector("main.bk-atlas") as HTMLElement;
    const dayStrip = screen.getByRole("region", { name: "这一天" });

    // 四页都进阅读模式：点开“出发前” -> is-reading + 长卷收下 inert；再点同一个按钮关上恢复
    fireEvent.click(beforeBtn);
    expect(beforeBtn).toHaveAttribute("aria-pressed", "true");
    expect(atlas).toHaveClass("is-reading");
    expect(dayStrip).toHaveClass("is-collapsed");
    expect(dayStrip).toHaveAttribute("aria-hidden", "true");
    expect(dayStrip).toHaveAttribute("inert");
    expect(
      screen.getByRole("complementary", { name: "出发前" }),
    ).toBeInTheDocument();

    fireEvent.click(beforeBtn);
    expect(beforeBtn).toHaveAttribute("aria-pressed", "false");
    expect(atlas).not.toHaveClass("is-reading");
    expect(dayStrip).not.toHaveClass("is-collapsed");
    expect(dayStrip).not.toHaveAttribute("inert");
    expect(screen.queryByRole("complementary")).toBeNull();

    // 再打开，点右上 × 关上；再打开，按 Esc 关上
    fireEvent.click(beforeBtn);
    expect(
      screen.getByRole("complementary", { name: "出发前" }),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "关上" }));
    expect(screen.queryByRole("complementary")).toBeNull();

    fireEvent.click(beforeBtn);
    expect(
      screen.getByRole("complementary", { name: "出发前" }),
    ).toBeInTheDocument();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("complementary")).toBeNull();
    expect(beforeBtn).toHaveAttribute("aria-pressed", "false");

    // 互斥：开着“出发前”（阅读模式）时点地图站钉 -> 换为这一站的详情、退出阅读模式、atlas 挂 has-stop 恢复长卷并高亮站卡
    fireEvent.click(beforeBtn);
    expect(beforeBtn).toHaveAttribute("aria-pressed", "true");
    expect(atlas).toHaveClass("is-reading");
    const zooPin = document.querySelector(
      'button.gpin[data-place-id="311"]',
    ) as HTMLButtonElement;
    fireEvent.click(zooPin);
    expect(beforeBtn).toHaveAttribute("aria-pressed", "false");
    expect(atlas).not.toHaveClass("is-reading");
    expect(atlas).toHaveClass("has-stop");
    expect(dayStrip).not.toHaveAttribute("inert");
    expect(
      screen.getByRole("complementary", { name: "重庆动物园 详情" }),
    ).toBeInTheDocument();
    const stopCard = screen.getByRole("button", { name: /1\s*重庆动物园/ });
    expect(stopCard.classList.contains("is-on")).toBe(true);

    // 互斥：开着一站时点“怎么去 · 住哪”与“花多少” -> 均进阅读模式、长卷收下、站卡与图钉高亮清除
    fireEvent.click(goBtn);
    expect(goBtn).toHaveAttribute("aria-pressed", "true");
    expect(atlas).toHaveClass("is-reading");
    expect(atlas).not.toHaveClass("has-stop");
    expect(dayStrip).toHaveClass("is-collapsed");
    expect(dayStrip).toHaveAttribute("inert");
    expect(stopCard.classList.contains("is-on")).toBe(false);
    expect(document.querySelector(".gpin.is-on")).toBeNull();
    expect(
      screen.getByRole("complementary", { name: "怎么去 · 住哪" }),
    ).toBeInTheDocument();

    fireEvent.click(costBtn);
    expect(costBtn).toHaveAttribute("aria-pressed", "true");
    expect(atlas).toHaveClass("is-reading");
    expect(dayStrip).toHaveClass("is-collapsed");
    expect(dayStrip).toHaveAttribute("inert");
    expect(
      screen.getByRole("complementary", { name: "花多少" }),
    ).toBeInTheDocument();

    // “攻略”也同为阅读模式
    fireEvent.click(guideBtn);
    expect(guideBtn).toHaveAttribute("aria-pressed", "true");
    expect(atlas).toHaveClass("is-reading");
    expect(dayStrip).toHaveClass("is-collapsed");
    expect(dayStrip).toHaveAttribute("inert");
    fireEvent.click(guideBtn);
    expect(atlas).not.toHaveClass("is-reading");
    expect(dayStrip).not.toHaveAttribute("inert");

    // 换天：开着“没排进来”（非阅读模式，长卷可见）时，换天留着不关
    fireEvent.click(screen.getByRole("button", { name: /2 处想去的没排进来/ }));
    expect(atlas).not.toHaveClass("is-reading");
    expect(atlas).not.toHaveClass("has-stop");
    fireEvent.click(screen.getByRole("button", { name: /第二天/ }));
    expect(
      screen.getByRole("complementary", { name: "想去但没排进来的" }),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /第一天/ }));
    expect(
      screen.getByRole("complementary", { name: "想去但没排进来的" }),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "关上" }));

    // 住处切换 + 换天：同日再点一次或按 Esc 可关上；开着“住处”时换天，应该关上并取消 .gstay.is-on
    const stayBtn = document.querySelector("button.gstay") as HTMLButtonElement;
    fireEvent.click(stayBtn);
    expect(stayBtn.classList.contains("is-on")).toBe(true);
    expect(stayBtn).toHaveAttribute("aria-pressed", "true");
    expect(
      screen.getByRole("complementary", { name: "住处" }),
    ).toBeInTheDocument();

    fireEvent.click(stayBtn);
    expect(screen.queryByRole("complementary")).toBeNull();
    expect(stayBtn.classList.contains("is-on")).toBe(false);
    expect(stayBtn).toHaveAttribute("aria-pressed", "false");

    fireEvent.click(stayBtn);
    expect(
      screen.getByRole("complementary", { name: "住处" }),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /第二天/ }));
    expect(screen.queryByRole("complementary")).toBeNull();
    const stayBtnDay2 = document.querySelector(
      "button.gstay",
    ) as HTMLButtonElement;
    expect(stayBtnDay2).toBeInTheDocument();
    expect(stayBtnDay2.classList.contains("is-on")).toBe(false);
    expect(stayBtnDay2).toHaveAttribute("aria-pressed", "false");
    await act(async () => {});
  });

  it("renders before-trip weather/checklist/tips and non-ok/empty fallbacks accurately", () => {
    const rich = makeRichResult();
    rich.weather!.days.push({
      day: 3,
      date: "2026-11-16",
      weather_text: "晴热",
      temp_min_c: 29,
      temp_max_c: 41,
      wind_text: "南风",
      icon_code: "sunny",
      reminders: [],
    });
    rich.plans[0].days.push({
      ...rich.plans[0].days[0],
      day: 3,
      title: "第三天",
    });

    useTripStore.getState().setResult("91500", "job_before", rich);
    const r1 = renderBookPage("/plan/91500/plan_a?job_id=job_before");

    fireEvent.click(screen.getByRole("button", { name: "出发前" }));
    const side = screen.getByRole("complementary", { name: "出发前" });
    expect(side).toHaveTextContent("第一天 · 11月14日");
    expect(side).toHaveTextContent("小雨 14–18°C · 北风");
    expect(side).toHaveTextContent("带一把折叠伞");
    expect(side).toHaveTextContent("第3天有 41°C 极端高温，请做好防暑降温");
    expect(side).toHaveTextContent("带上这些");
    expect(side).toHaveTextContent("薄外套、防滑徒步鞋");
    expect(side).not.toHaveTextContent("会被过滤");
    expect(side).toHaveTextContent("小提示");
    expect(side).toHaveTextContent("山城坡道多");
    expect(side).not.toHaveTextContent("空标题不展示");
    expect(side).not.toHaveTextContent("天气预报暂未生成");
    r1.unmount();

    // weather 非 ok（skipped_date_out_of_range），清单与小贴士均为空时整块略去
    const emptyBefore = makeRichResult({
      weather: {
        status: "skipped_date_out_of_range",
        city: "重庆",
        days: [],
      },
    });
    emptyBefore.plans[0].packing_checklist = [{ category: "空", items: [" "] }];
    emptyBefore.plans[0].travel_tips = [];
    useTripStore.getState().setResult("91500", "job_before_empty", emptyBefore);

    const r2 = renderBookPage("/plan/91500/plan_a?job_id=job_before_empty");
    fireEvent.click(screen.getByRole("button", { name: "出发前" }));
    const side2 = screen.getByRole("complementary", { name: "出发前" });
    expect(side2).toHaveTextContent("出发日期较远，临近出行时可查看天气预报");
    expect(side2).not.toHaveTextContent("带上这些");
    expect(side2).not.toHaveTextContent("小提示");
    expect(side2).not.toHaveTextContent("天气预报暂未生成");
    r2.unmount();

    // status === "ok" 但第二天缺数据时显示“第二天 · 暂无预报”
    const partialWx = makeRichResult({
      weather: {
        status: "ok",
        city: "重庆",
        days: [makeRichResult().weather!.days[0]],
      },
    });
    useTripStore.getState().setResult("91500", "job_before_partial", partialWx);
    renderBookPage("/plan/91500/plan_a?job_id=job_before_partial");
    fireEvent.click(screen.getByRole("button", { name: "出发前" }));
    expect(
      screen.getByRole("complementary", { name: "出发前" }),
    ).toHaveTextContent("第二天 · 暂无预报");
  });

  it("renders go & stay drawers, pans stay away from HUD/sidebars (including reduced-motion), and handles empty/legacy/user lodging", async () => {
    const rich = makeRichResult();
    useTripStore.getState().setResult("91500", "job_go_1", rich);
    // Simulate stay marker obscured underneath the right side drawer
    mapSpy.lngLatContainerResult = { x: 1120, y: 320 };

    const r1 = renderBookPage("/plan/91500/plan_a?job_id=job_go_1");
    await waitFor(() => {
      expect(document.querySelector("button.gstay")).not.toBeNull();
    });

    fireEvent.click(screen.getByRole("button", { name: "怎么去 · 住哪" }));
    const goSide = screen.getByRole("complementary", { name: "怎么去 · 住哪" });

    // 大交通完整检查：起终点、日期、高铁+飞机平铺明细、参考标签仅在 static_reference 且无“参考”时显示、售罄提示、最晚查询时间
    expect(goSide).toHaveTextContent("上海 → 重庆 · 出行日期 2026-11-14");
    expect(goSide).toHaveTextContent("G1974");
    expect(goSide).toHaveTextContent("上海虹桥");
    expect(goSide).toHaveTextContent("重庆北");
    expect(goSide).toHaveTextContent("东方航空 MU5421");
    expect(goSide).toHaveTextContent(
      "查询时段车票已售罄，请以购票平台实时余票为准",
    );
    expect(goSide.querySelectorAll(".bk-tr-ref")).toHaveLength(1);
    const expectedChecked = formatTransportCheckedTime("2026-11-10T06:30:00Z");
    expect(goSide).toHaveTextContent(
      `实时推荐方案 · 余票查询于 ${expectedChecked}`,
    );

    // 住哪完整检查：没找到住处提示在片区前面、三档、节假日提示、参考价小字、在地图上看住处
    const goText = goSide.textContent ?? "";
    const unmatchedIdx = goText.indexOf(
      "没找到你填的住处「某客栈」，已改按解放碑一带推荐",
    );
    const areaIdx = goText.indexOf("解放碑一带");
    expect(unmatchedIdx).toBeGreaterThanOrEqual(0);
    expect(unmatchedIdx).toBeLessThan(areaIdx);
    expect(goSide).toHaveTextContent("经济");
    expect(goSide).toHaveTextContent("约90–110元/晚");
    expect(goSide).toHaveTextContent("如家酒店·neo、庭泊·别院酒店");
    expect(goSide).toHaveTextContent("假期热门区域房价通常上浮，建议尽早预订");
    expect(goSide).toHaveTextContent("参考价，实际以预订时为准");

    // 点“在地图上看住处” -> 切到“住处”，不再放“在地图上看住处”，住亮起且挪进视野
    fireEvent.click(screen.getByRole("button", { name: "在地图上看住处" }));
    const staySide = screen.getByRole("complementary", { name: "住处" });
    expect(screen.queryByRole("button", { name: "在地图上看住处" })).toBeNull();
    expect(staySide).toHaveTextContent("解放碑一带");
    expect(document.querySelector("button.gstay.is-on")).not.toBeNull();
    await waitFor(() => {
      expect(mapSpy.panToCalls.length).toBeGreaterThanOrEqual(1);
    });
    r1.unmount();

    // Reduced-motion：改为 map.setCenter(..., true) 而不是 panTo
    mockMatchMedia(true);
    mapSpy.panToCalls = [];
    mapSpy.setCenterCalls = [];
    useTripStore.getState().setResult("91500", "job_go_rm", rich);
    const rReduced = renderBookPage("/plan/91500/plan_a?job_id=job_go_rm");
    await waitFor(() => {
      expect(document.querySelector("button.gstay")).not.toBeNull();
    });
    fireEvent.click(document.querySelector("button.gstay")!);
    await waitFor(() => {
      expect(mapSpy.setCenterCalls.some((c) => c.immediately === true)).toBe(
        true,
      );
    });
    expect(mapSpy.panToCalls).toHaveLength(0);
    rReduced.unmount();

    // 无大交通 + 无住宿
    mockMatchMedia(false);
    const emptyGo = makeRichResult();
    emptyGo.plans[0].transport = null;
    emptyGo.plans[0].accommodation = null;
    useTripStore.getState().setResult("91500", "job_go_empty", emptyGo);
    const rEmpty = renderBookPage("/plan/91500/plan_a?job_id=job_go_empty");
    fireEvent.click(screen.getByRole("button", { name: "怎么去 · 住哪" }));
    expect(
      screen.getByRole("complementary", { name: "怎么去 · 住哪" }),
    ).toHaveTextContent("这一册没有大交通和住宿信息。");
    rEmpty.unmount();

    // user_lodging + 坐标无效时不渲染“在地图上看住处”
    const userGo = makeRichResult();
    userGo.plans[0].transport = null;
    userGo.plans[0].accommodation = {
      name: "观景酒店",
      latitude: 0,
      longitude: 0,
      source: "user_specified",
      display: {
        layout: "user_lodging",
        area_label: "观景酒店",
        round_trip_minutes_low: 20,
        round_trip_minutes_high: 40,
      },
    };
    useTripStore.getState().setResult("91500", "job_go_user", userGo);
    renderBookPage("/plan/91500/plan_a?job_id=job_go_user");
    fireEvent.click(screen.getByRole("button", { name: "怎么去 · 住哪" }));
    const userSide = screen.getByRole("complementary", {
      name: "怎么去 · 住哪",
    });
    expect(userSide).toHaveTextContent(
      "从你住的观景酒店出发，每天来回约20–40分钟",
    );
    expect(screen.queryByRole("button", { name: "在地图上看住处" })).toBeNull();
  });

  it("renders cost drawer scenarios, switching, single-scenario, partial, unavailable and notice fallback", () => {
    // 1. 多场景默认选高铁、切换机票
    const dual = makeRichResult();
    useTripStore.getState().setResult("91500", "job_cost_1", dual);
    const r1 = renderBookPage("/plan/91500/plan_a?job_id=job_cost_1");
    fireEvent.click(screen.getByRole("button", { name: "花多少" }));
    const side1 = screen.getByRole("complementary", { name: "花多少" });

    expect(
      screen.getByRole("radiogroup", { name: "大交通出行场景选择" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "高铁往返方案" })).toHaveAttribute(
      "aria-checked",
      "true",
    );
    expect(side1).toHaveTextContent("高铁往返方案 · 全程 · 2 人");
    expect(side1).toHaveTextContent("预估 ¥ 1,800");
    expect(side1).toHaveTextContent("大交通");
    expect(side1).toHaveTextContent("¥ 340");
    const howNotes = Array.from(
      side1.querySelectorAll("details.c2-how ul li"),
    ).map((el) => el.textContent);
    expect(howNotes).toEqual(["每间房两位旅客", "骑行费用暂未计入"]);
    const expectedDate = formatCostEstimatedDate(
      COST_ESTIMATE_COMPLETE.estimated_at,
    )!;
    expect(side1.querySelector("details.c2-how p")).toHaveTextContent(
      `预估于 ${expectedDate}`,
    );
    expect(side1).toHaveTextContent(
      "费用为规划参考，实际支付金额请以预订或现场结算为准",
    );

    fireEvent.click(screen.getByRole("radio", { name: "机票往返方案" }));
    expect(side1).toHaveTextContent("机票往返方案 · 全程 · 2 人");
    expect(side1).toHaveTextContent("预估 ¥ 2,400");
    expect(side1).toHaveTextContent("¥ 940");
    r1.unmount();

    // 2. 单场景隐藏切换器 + 缺 notice 回退默认提示
    const single = makeRichResult();
    single.plans[0].cost_estimate = {
      ...COST_ESTIMATE_SINGLE_TRAIN,
      notice: "",
    };
    useTripStore.getState().setResult("91500", "job_cost_2", single);
    const r2 = renderBookPage("/plan/91500/plan_a?job_id=job_cost_2");
    fireEvent.click(screen.getByRole("button", { name: "花多少" }));
    expect(screen.queryByRole("radiogroup")).toBeNull();
    expect(
      screen.getByRole("complementary", { name: "花多少" }),
    ).toHaveTextContent("费用为规划参考，实际花费以出行行为准");
    r2.unmount();

    // 3. 部分估算 + policy_zero + 待确认
    const partial = makeRichResult();
    partial.plans[0].cost_estimate = COST_ESTIMATE_MIXED_POLICY_ZERO;
    useTripStore.getState().setResult("91500", "job_cost_3", partial);
    const r3 = renderBookPage("/plan/91500/plan_a?job_id=job_cost_3");
    fireEvent.click(screen.getByRole("button", { name: "花多少" }));
    const side3 = screen.getByRole("complementary", { name: "花多少" });
    expect(side3).toHaveTextContent("已估 ¥ 800");
    expect(side3).toHaveTextContent("部分费用待确认");
    expect(side3).toHaveTextContent("来源与参考混合");
    expect(side3).toHaveTextContent("按规则计为 ¥0");
    expect(side3).toHaveTextContent("待确认");
    r3.unmount();

    // 4. unavailable 不出现 ¥0
    const unavail = makeRichResult();
    unavail.plans[0].cost_estimate = COST_ESTIMATE_UNAVAILABLE;
    useTripStore.getState().setResult("91500", "job_cost_4", unavail);
    const r4 = renderBookPage("/plan/91500/plan_a?job_id=job_cost_4");
    fireEvent.click(screen.getByRole("button", { name: "花多少" }));
    const side4 = screen.getByRole("complementary", { name: "花多少" });
    expect(side4).toHaveTextContent("费用暂不可估算");
    expect(side4.textContent).not.toContain("¥0");
    r4.unmount();

    // 5. 空 scenarios
    const emptyCost = makeRichResult();
    emptyCost.plans[0].cost_estimate = {
      ...COST_ESTIMATE_PARTIAL,
      scenarios: [],
    };
    useTripStore.getState().setResult("91500", "job_cost_5", emptyCost);
    const r5 = renderBookPage("/plan/91500/plan_a?job_id=job_cost_5");
    fireEvent.click(screen.getByRole("button", { name: "花多少" }));
    expect(
      screen.getByRole("complementary", { name: "花多少" }),
    ).toHaveTextContent("费用暂不可估算");
    r5.unmount();

    // 6. 门票部分已知：门票行展示“已知 ¥120，另有 2 处待确认”，总价含已知门票并写“另有 2 处门票未计入”
    const partialAdmission = makeRichResult();
    partialAdmission.plans[0].cost_estimate = {
      ...COST_ESTIMATE_SINGLE_TRAIN,
      completeness: "partial",
      scenarios: [
        {
          ...COST_ESTIMATE_SINGLE_TRAIN.scenarios[0],
          total_scope: "estimated_subset",
          total_range: { min_cny: 2200, max_cny: 2800 },
          total_range_with_known_admission: { min_cny: 2320, max_cny: 2920 },
          unpriced_admission_count: 2,
          categories: COST_ESTIMATE_SINGLE_TRAIN.scenarios[0].categories.map(
            (c) =>
              c.category === "admission"
                ? {
                    ...c,
                    coverage: "missing",
                    range: null,
                    price_basis: null,
                    known_range: { min_cny: 120, max_cny: 120 },
                    priced_item_count: 1,
                    unpriced_item_count: 2,
                  }
                : c,
          ),
          missing_categories: ["admission"],
        },
      ],
    };
    useTripStore.getState().setResult("91500", "job_cost_6", partialAdmission);
    renderBookPage("/plan/91500/plan_a?job_id=job_cost_6");
    fireEvent.click(screen.getByRole("button", { name: "花多少" }));
    const side6 = screen.getByRole("complementary", { name: "花多少" });
    expect(side6).toHaveTextContent("已估 ¥ 2,920");
    expect(side6).not.toHaveTextContent("¥ 2,800");
    expect(side6).toHaveTextContent("另有 2 处门票未计入");
    expect(side6).not.toHaveTextContent("部分费用待确认");
    expect(side6).toHaveTextContent("已知 ¥120，另有 2 处待确认");
    expect(side6.querySelector("em.is-unpriced")).toHaveTextContent(
      "已知 ¥120，另有 2 处待确认",
    );
  });

  it("renders unscheduled must_include entry row on book card and hides it when none missed", () => {
    const withMiss = makeRichResult({
      must_include: [
        { name: "重庆动物园", status: "scheduled", place_id: 311 },
        {
          name: "洪崖洞夜景",
          status: "not_scheduled",
          reason: "日程已满未能排入",
        },
        {
          name: "武隆天生三桥",
          status: "cross_city",
          reason: "位于武隆区",
        },
        {
          name: "南山神秘茶馆",
          status: "recorded_unmatched",
          reason: "未匹配到对应地点",
        },
        {
          name: "鹅岭备选点",
          status: "recorded_candidate",
          reason: "留作候补",
        },
      ],
    });
    useTripStore.getState().setResult("91500", "job_miss_1", withMiss);
    const r1 = renderBookPage("/plan/91500/plan_a?job_id=job_miss_1");

    const missBtn = screen.getByRole("button", {
      name: /4 处想去的没排进来/,
    });
    expect(missBtn).toHaveTextContent("看原因");
    fireEvent.click(missBtn);
    expect(missBtn).toHaveAttribute("aria-pressed", "true");

    const missSide = screen.getByRole("complementary", {
      name: "想去但没排进来的",
    });
    expect(missSide).not.toHaveTextContent("重庆动物园");
    expect(missSide).toHaveTextContent("洪崖洞夜景");
    expect(missSide).toHaveTextContent("日程已满未能排入");
    expect(missSide).toHaveTextContent("武隆天生三桥 (不在本城市)");
    expect(missSide).toHaveTextContent("南山神秘茶馆 (未匹配到对应地点)");
    expect(missSide).toHaveTextContent("鹅岭备选点 (候选地点备用)");
    r1.unmount();

    const noMiss = makeRichResult({
      must_include: [
        { name: "重庆动物园", status: "scheduled", place_id: 311 },
      ],
    });
    useTripStore.getState().setResult("91500", "job_miss_0", noMiss);
    renderBookPage("/plan/91500/plan_a?job_id=job_miss_0");
    expect(screen.queryByRole("button", { name: /想去的没排进来/ })).toBeNull();
  });

  it("handles PDF export loading/ready/auth-redirect/error and ShareDialog ?share=1 & Esc priority", async () => {
    const rich = makeRichResult();
    useTripStore.getState().setResult("91500", "job_art", rich);

    // 1. PDF 导出：加载态禁用“导出中…” -> ready 自动保存 + success toast -> 恢复“导出 PDF”
    let resolvePdf!: (art: Artifact) => void;
    vi.mocked(apiModule.getArtifact).mockImplementation(
      (_id, type) =>
        new Promise<Artifact>((resolve, reject) => {
          if (type === "pdf") {
            resolvePdf = resolve;
          } else {
            reject(new ApiRequestError("NOT_FOUND", "not created", 404));
          }
        }),
    );
    vi.mocked(apiModule.createArtifact).mockResolvedValue(
      makeReadyArtifact("share_image"),
    );
    const pdfBlob = new Blob(["%PDF-1.4"], { type: "application/pdf" });
    vi.mocked(apiModule.fetchArtifactBlob).mockResolvedValue(pdfBlob);

    const r1 = renderBookPage("/plan/91500/plan_a?job_id=job_art");
    const exportBtn = screen.getByRole("button", { name: "导出 PDF" });
    fireEvent.click(exportBtn);

    await waitFor(() => {
      expect(screen.getByRole("button", { name: "导出中…" })).toBeDisabled();
    });

    await act(async () => {
      resolvePdf(makeReadyArtifact("pdf"));
    });

    await waitFor(() => {
      expect(saveBlob).toHaveBeenCalledWith(
        pdfBlob,
        "云途行程路书_重庆_山城慢逛两日.pdf",
      );
      expect(useToastStore.getState().message).toBe(
        "PDF 导出成功，已自动开始下载",
      );
    });
    expect(screen.getByRole("button", { name: "导出 PDF" })).not.toBeDisabled();

    // 2. 通用失败 -> toast error
    vi.mocked(apiModule.getArtifact).mockRejectedValueOnce(
      new ApiRequestError("SERVER_ERR", "生成服务繁忙", 502),
    );
    fireEvent.click(screen.getByRole("button", { name: "导出 PDF" }));
    await waitFor(() => {
      expect(useToastStore.getState().type).toBe("error");
      expect(useToastStore.getState().message).toBe(
        "PDF 导出失败: 生成服务繁忙",
      );
    });

    // 3. 分享弹窗点击打开，且压在抽屉上时先按 Esc 只关弹窗、不关后面抽屉
    vi.mocked(apiModule.getArtifact).mockResolvedValue(
      makeReadyArtifact("share_image"),
    );
    vi.mocked(apiModule.fetchArtifactBlob).mockResolvedValue(
      new Blob(["png"], { type: "image/png" }),
    );

    fireEvent.click(screen.getByRole("button", { name: "出发前" }));
    expect(
      screen.getByRole("complementary", { name: "出发前" }),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "分享长图" }));
    expect(await screen.findByText("分享行程 AI 长图")).toBeInTheDocument();

    // 第一下 Esc 关 ShareDialog，后面的“出发前”继续留着
    fireEvent.keyDown(document, { key: "Escape" });
    await waitFor(() => {
      expect(screen.queryByText("分享行程 AI 长图")).toBeNull();
    });
    expect(
      screen.getByRole("complementary", { name: "出发前" }),
    ).toBeInTheDocument();

    // 第二下 Esc 再关后面的抽屉
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("complementary")).toBeNull();
    r1.unmount();

    // 4. ?share=1 自动弹出分享窗，以及 401/AUTH_REQUIRED 跳登录
    useTripStore.getState().setResult("91500", "job_share_q", rich);
    renderBookPage("/plan/91500/plan_a?job_id=job_share_q&share=1");
    expect(await screen.findByText("分享行程 AI 长图")).toBeInTheDocument();
    // 关掉 ShareDialog 后测 PDF 401 跳转
    fireEvent.click(screen.getByRole("button", { name: "关闭" }));

    vi.mocked(apiModule.getArtifact).mockRejectedValueOnce(
      new ApiRequestError("AUTH_REQUIRED", "请登录", 401),
    );
    fireEvent.click(screen.getByRole("button", { name: "导出 PDF" }));
    await waitFor(() => {
      expect(useToastStore.getState().message).toBe("PDF 导出失败: 请登录");
    });
  });

  it("includes Step 7b animated selectors in prefers-reduced-motion CSS rule", () => {
    const css = fs.readFileSync(
      path.resolve(process.cwd(), "src/pages/book/spread-book.css"),
      "utf8",
    );
    const reducedBlockMatch =
      /@media\s*\(prefers-reduced-motion:\s*reduce\)\s*\{([\s\S]*)\}\s*$/m.exec(
        css,
      );
    expect(reducedBlockMatch).not.toBeNull();
    const block = reducedBlockMatch![1];
    expect(block).toContain(".bk-bar .bk-bar-icon.is-spin");
    expect(block).toContain(".bk-bar button");
    expect(block).toContain(".gstay");
    expect(block).toContain(".bk-cost-tabs button");
    expect(block).toContain(".bk-fold");
    expect(css).toContain("width: min(540px, calc(100% - 380px));");
    expect(css).toContain(".bk-atlas.has-stop .bk-day");
    expect(css).toContain("right: 420px;");
  });

  it("住哪按结构化字段渲染一档一行三列网格，且全无价时退回两列", () => {
    const rich = makeRichResult();
    useTripStore.getState().setResult("91500", "job_tier_grid", rich);
    const r1 = renderBookPage("/plan/91500/plan_a?job_id=job_tier_grid");

    fireEvent.click(screen.getByRole("button", { name: "怎么去 · 住哪" }));
    const goSide = screen.getByRole("complementary", { name: "怎么去 · 住哪" });
    const rows = Array.from(
      goSide.querySelectorAll(".bk-gtier2 .bk-gtier2-row"),
    );
    expect(rows).toHaveLength(3);
    expect(rows[0].querySelector("b")?.textContent).toBe("经济");
    expect(rows[0].querySelector(".p")?.textContent).toBe("约90–110元/晚");
    expect(rows[0].querySelector(".h")?.textContent).toBe(
      "如家酒店·neo、庭泊·别院酒店",
    );
    expect(rows[0].classList.contains("is-unpriced")).toBe(false);
    r1.unmount();

    const unpriced = makeRichResult();
    unpriced.plans[0].accommodation = {
      ...unpriced.plans[0].accommodation!,
      display: {
        layout: "brands",
        area_label: "解放碑一带",
        groups: [
          {
            kind: "economy",
            label: "经济",
            hotels: ["示例酒店A"],
            nightly_low_cny: null,
            nightly_high_cny: null,
          },
        ],
        notices: [],
      },
    };
    useTripStore.getState().setResult("91500", "job_tier_unpriced", unpriced);
    renderBookPage("/plan/91500/plan_a?job_id=job_tier_unpriced");
    fireEvent.click(screen.getByRole("button", { name: "怎么去 · 住哪" }));
    const row = document.querySelector(".bk-gtier2 .bk-gtier2-row")!;
    expect(row.classList.contains("is-unpriced")).toBe(true);
    expect(row.querySelector(".p")).toBeNull();
    expect(row.querySelector(".h")?.textContent).toBe("示例酒店A");
  });

  it("花多少纯函数拆段与比例条着色顺序正确", () => {
    expect(
      splitScenarioCostText({ costText: "预估 ¥ 1,800", showAmount: true }),
    ).toEqual({ prefix: "预估", main: "¥ 1,800" });
    expect(
      splitScenarioCostText({ costText: "已估 ¥ 3,750", showAmount: true }),
    ).toEqual({ prefix: "已估", main: "¥ 3,750" });
    expect(
      splitScenarioCostText({ costText: "费用暂不可估算", showAmount: false }),
    ).toEqual({ prefix: null, main: "费用暂不可估算" });

    const rows = resolveBookCostCategoryRows(
      COST_ESTIMATE_COMPLETE.scenarios[0],
    );
    const visuals = resolveBookCostBreakdownVisuals(rows);
    expect(visuals.segments.map((s) => s.tone)).toEqual([
      "deep",
      "green",
      "morning",
      "afternoon",
      "evening",
    ]);
    const totalRatio = visuals.segments.reduce((acc, s) => acc + s.ratio, 0);
    expect(totalRatio).toBeCloseTo(1, 5);
  });

  it("花多少：总价拆段、非胶囊灰字徽标、细比例条与门票部分已知空心色块", () => {
    const partialAdmission = makeRichResult();
    partialAdmission.plans[0].cost_estimate = {
      ...COST_ESTIMATE_SINGLE_TRAIN,
      completeness: "partial",
      scenarios: [
        {
          ...COST_ESTIMATE_SINGLE_TRAIN.scenarios[0],
          total_scope: "estimated_subset",
          total_range: { min_cny: 2200, max_cny: 2800 },
          total_range_with_known_admission: { min_cny: 2320, max_cny: 2920 },
          unpriced_admission_count: 2,
          categories: COST_ESTIMATE_SINGLE_TRAIN.scenarios[0].categories.map(
            (c) =>
              c.category === "admission"
                ? {
                    ...c,
                    coverage: "missing",
                    range: null,
                    price_basis: null,
                    known_range: { min_cny: 120, max_cny: 120 },
                    priced_item_count: 1,
                    unpriced_item_count: 2,
                  }
                : c,
          ),
          missing_categories: ["admission"],
        },
      ],
    };
    useTripStore.getState().setResult("91500", "job_cost_v4", partialAdmission);
    renderBookPage("/plan/91500/plan_a?job_id=job_cost_v4");
    fireEvent.click(screen.getByRole("button", { name: "花多少" }));

    const side = screen.getByRole("complementary", { name: "花多少" });
    expect(side.querySelector(".c2-total small")?.textContent).toBe("已估");
    expect(side.querySelector(".c2-total b")?.textContent).toBe("¥ 2,920");
    expect(side.querySelector(".c2-flag")?.textContent).toBe(
      "另有 2 处门票未计入",
    );
    expect(side.querySelector(".bk-cost-badge")).toBeNull();

    // 比例条只算已定价的 4 项（门票未定价不计入）
    const barSegs = Array.from(side.querySelectorAll(".c2-bar i"));
    expect(barSegs).toHaveLength(4);
    expect(barSegs.map((el) => el.className)).toEqual([
      "c2-seg-deep",
      "c2-seg-green",
      "c2-seg-morning",
      "c2-seg-afternoon",
    ]);

    // 未定价门票项色块空心、文案沿用“已知…另有…处待确认”
    const admissionRow = side.querySelector(".c2-rows li.is-unpriced")!;
    expect(admissionRow.querySelector(".dot.is-hollow")).not.toBeNull();
    expect(admissionRow.querySelector("em")?.textContent).toBe(
      "已知 ¥120，另有 2 处待确认",
    );

    // “怎么算的”收进 details，notice 放最下
    expect(side.querySelector("details.c2-how summary")).toHaveTextContent(
      "怎么算的",
    );
    expect(side.querySelector(".c2-note")).not.toBeNull();
  });

  it("花多少：多场景切换同步更新总价主角、场景说明与比例条占比", () => {
    const dual = makeRichResult();
    useTripStore.getState().setResult("91500", "job_cost_dual_v4", dual);
    renderBookPage("/plan/91500/plan_a?job_id=job_cost_dual_v4");
    fireEvent.click(screen.getByRole("button", { name: "花多少" }));

    const side = screen.getByRole("complementary", { name: "花多少" });
    expect(side.querySelector(".c2-scn")?.textContent).toBe(
      "高铁往返方案 · 全程 · 2 人",
    );
    expect(side.querySelector(".c2-total small")?.textContent).toBe("预估");
    expect(side.querySelector(".c2-total b")?.textContent).toBe("¥ 1,800");
    const trainFirstFlex = Number(
      (side.querySelectorAll(".c2-bar i")[0] as HTMLElement).style.flexGrow,
    );

    fireEvent.click(screen.getByRole("radio", { name: "机票往返方案" }));
    expect(side.querySelector(".c2-scn")?.textContent).toBe(
      "机票往返方案 · 全程 · 2 人",
    );
    expect(side.querySelector(".c2-total b")?.textContent).toBe("¥ 2,400");
    const flightFirstFlex = Number(
      (side.querySelectorAll(".c2-bar i")[0] as HTMLElement).style.flexGrow,
    );
    expect(flightFirstFlex).toBeGreaterThan(trainFirstFlex);
  });

  it("第 4 步样式检查：spread-book.css 全册最小字号不低于 12px", () => {
    const css = fs.readFileSync(
      path.resolve(process.cwd(), "src/pages/book/spread-book.css"),
      "utf8",
    );
    const fontSizes = Array.from(css.matchAll(/font-size:\s*(\d+)px/g)).map(
      (m) => Number(m[1]),
    );
    expect(fontSizes.length).toBeGreaterThan(0);
    expect(Math.min(...fontSizes)).toBeGreaterThanOrEqual(12);
  });
});
