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
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import SpreadBookPage from "@/pages/book/SpreadBookPage";
import * as apiModule from "@/services/api";
import { ApiRequestError } from "@/services/api";
import { useShareImageTaskStore } from "@/stores/shareImageTaskStore";
import { useToastStore } from "@/stores/toastStore";
import { useTripStore } from "@/stores/tripStore";
import type { PlaceDetail, TripResult } from "@/types/trip";
import { pickGuideReadingStopId } from "@/utils/spreadBook";

const guideMapSpy = vi.hoisted(() => ({
  markers: [] as Array<{ opts: any; clicks: Array<() => void> }>,
  fitViewCalls: [] as Array<{
    targets: any[];
    immediately: boolean;
    avoid: number[];
    maxZoom: number;
  }>,
  panToCalls: [] as any[],
  setCenterCalls: [] as Array<{ center: any; immediately?: boolean }>,
  lngLatContainerResult: { x: 680, y: 320 },
  enableMapEvents: false,
  eventListeners: new Map<string, Set<() => void>>(),
}));

const ioSpy = vi.hoisted(() => ({
  instances: [] as Array<{
    callback: IntersectionObserverCallback;
    options: IntersectionObserverInit | undefined;
    observed: Element[];
    disconnected: boolean;
  }>,
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
          guideMapSpy.markers.push({ opts, clicks: this.clicks });
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
        on?: (event: string, cb: () => void) => void;
        off?: (event: string, cb: () => void) => void;
        constructor(container: HTMLElement) {
          this.container = container;
          if (guideMapSpy.enableMapEvents) {
            this.on = (event: string, cb: () => void) => {
              const set =
                guideMapSpy.eventListeners.get(event) ?? new Set<() => void>();
              set.add(cb);
              guideMapSpy.eventListeners.set(event, set);
            };
            this.off = (event: string, cb: () => void) => {
              guideMapSpy.eventListeners.get(event)?.delete(cb);
            };
          }
        }
        setFitView = vi.fn(
          (
            targets: any[],
            immediately: boolean,
            avoid: number[],
            maxZoom: number,
          ) => {
            guideMapSpy.fitViewCalls.push({
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
              guideMapSpy.lngLatContainerResult.x,
              guideMapSpy.lngLatContainerResult.y,
            ),
        );
        containerToLngLat = vi.fn((px: any) => [
          106.5 + (px?.x ?? 0) * 0.0001,
          29.5 + (px?.y ?? 0) * 0.0001,
        ]);
        panTo = vi.fn((center: any) => {
          guideMapSpy.panToCalls.push(center);
        });
        setCenter = vi.fn((center: any, immediately?: boolean) => {
          guideMapSpy.setCenterCalls.push({ center, immediately });
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

class MockIntersectionObserver {
  callback: IntersectionObserverCallback;
  options: IntersectionObserverInit | undefined;
  observed: Element[] = [];
  disconnected = false;

  constructor(
    callback: IntersectionObserverCallback,
    options?: IntersectionObserverInit,
  ) {
    this.callback = callback;
    this.options = options;
    ioSpy.instances.push(this);
  }
  observe = (el: Element) => {
    this.observed.push(el);
  };
  unobserve = (el: Element) => {
    this.observed = this.observed.filter((item) => item !== el);
  };
  disconnect = () => {
    this.disconnected = true;
  };
  takeRecords = () => [];
}

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

function emitMapEvent(event: string) {
  const listeners = Array.from(guideMapSpy.eventListeners.get(event) ?? []);
  for (const cb of listeners) {
    cb();
  }
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
    top_reasons: [`理由(${name})`],
    warnings: [],
    source_count: 10,
    mention_count: 30,
    gallery: [
      {
        asset_id: `img-${id}-2`,
        position: 2,
        alt_text: `${name}次图`,
        desktop: {
          url: `https://img.example/${id}-2-d.webp`,
          width: 1200,
          height: 800,
        },
        mobile: {
          url: `https://img.example/${id}-2-m.webp`,
          width: 800,
          height: 600,
        },
        thumb: {
          url: `https://img.example/${id}-2-t.webp`,
          width: 300,
          height: 225,
        },
      },
      {
        asset_id: `img-${id}-0`,
        position: 0,
        alt_text: `${name}首图`,
        desktop: {
          url: `https://img.example/${id}-0-d.webp`,
          width: 1200,
          height: 800,
        },
        mobile: {
          url: `https://img.example/${id}-0-m.webp`,
          width: 800,
          height: 600,
        },
        thumb: {
          url: `https://img.example/${id}-0-t.webp`,
          width: 300,
          height: 225,
        },
      },
    ],
  };
}

function makeGuideResult(overrides?: Partial<TripResult>): TripResult {
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
          weather_text: "阴",
          temp_min_c: 11,
          temp_max_c: 14,
          wind_text: "微风",
          icon_code: "overcast",
          reminders: [],
        },
        {
          day: 2,
          date: "2026-11-15",
          weather_text: "多云",
          temp_min_c: 12,
          temp_max_c: 16,
          wind_text: "东北风",
          icon_code: "cloudy",
          reminders: [],
        },
      ],
    },
    must_include: [
      { name: "磁器口古镇", status: "not_scheduled", reason: "太远顺路不足" },
    ],
    plans: [
      {
        plan_id: "plan_a",
        title: "山城慢逛两日",
        summary: "动物园与渝中老街夜景串联",
        tags: ["citywalk", "夜景"],
        pace: {
          level: "RELAXED",
          commute_status: "WITHIN_LIMIT",
          total_commute_minutes: 44,
        },
        accommodation: {
          name: "解放碑商圈",
          latitude: 29.558,
          longitude: 106.577,
          source: "auto_recommended",
          reason: "去两边都省时",
        },
        days: [
          {
            day: 1,
            title: "重庆动物园全日深游",
            narrative: "整日只安排动物园一个主景点。",
            commute_summary: "园内步行游览",
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
                activity_note: "按大熊猫馆到金鱼池慢走。",
                schedule: {
                  period: "morning",
                  exact_start: "09:30",
                  exact_end: null,
                  exact_time_source: null, // 非受治理来源不显示 09:30
                },
              },
            ],
            commute_legs: [],
          },
          {
            day: 2,
            title: "山城巷至洪崖洞",
            narrative: "老街开场，傍晚洪崖洞看江景。",
            commute_summary: "公共交通与步行结合，约 44 分钟",
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
                brief: "依山栈道老城",
                stay_minutes: 45,
                activity_note: "自下而上走步道。",
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
                activity_note: "顺路午餐不长停。",
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
                brief: "吊脚楼夜景",
                stay_minutes: 90,
                activity_note: "从顶层平台逐层逛。",
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
                brief: "看洪崖洞全景机位",
              },
              {
                place_id: 325,
                name: "江北嘴江滩",
                category: "photo_spot",
                longitude: 106.589,
                latitude: 29.572,
                role: "photo_stop",
                optional: true,
                brief: "对岸夜色散步点",
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
                transit_summary: "公共交通预计 22 分钟",
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
              // 注意：324 -> 325 故意没有 leg 记录，验证攻略里不渲染 .bk-gl
            ],
          },
        ],
      },
    ],
  };

  return { ...base, ...overrides };
}

function renderBookPage(initialUrl: string) {
  return render(
    <div className="yuntu-spread">
      <MemoryRouter initialEntries={[initialUrl]}>
        <Routes>
          <Route path="/plan/:resultId/:planId" element={<SpreadBookPage />} />
          <Route path="/history" element={<div>history</div>} />
        </Routes>
      </MemoryRouter>
    </div>,
  );
}

const DEFAULT_INNER_WIDTH = window.innerWidth;
const DEFAULT_INNER_HEIGHT = window.innerHeight;

describe("SpreadBook Step 7c: full guide drawer & map scroll-sync", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    ioSpy.instances = [];
    guideMapSpy.markers = [];
    guideMapSpy.fitViewCalls = [];
    guideMapSpy.panToCalls = [];
    guideMapSpy.setCenterCalls = [];
    guideMapSpy.lngLatContainerResult = { x: 680, y: 320 };
    guideMapSpy.enableMapEvents = false;
    guideMapSpy.eventListeners.clear();

    Object.defineProperty(window, "innerWidth", {
      configurable: true,
      writable: true,
      value: 1440,
    });
    Object.defineProperty(window, "innerHeight", {
      configurable: true,
      writable: true,
      value: 900,
    });

    useTripStore.getState().reset();
    useToastStore.getState().hideToast();
    useShareImageTaskStore.getState().clearAllTasks();
    mockMatchMedia(false);

    vi.stubGlobal("IntersectionObserver", MockIntersectionObserver);
    vi.stubEnv("VITE_AMAP_KEY", "test-amap-key");
    vi.stubEnv("VITE_AMAP_SECURITY", "test-amap-security");

    vi.mocked(apiModule.fetchPlaceDetail).mockImplementation(async (id) =>
      makePlaceDetail(id, `地点${id}`),
    );
  });

  afterEach(async () => {
    await act(async () => {});
    Object.defineProperty(window, "innerWidth", {
      configurable: true,
      writable: true,
      value: DEFAULT_INNER_WIDTH,
    });
    Object.defineProperty(window, "innerHeight", {
      configurable: true,
      writable: true,
      value: DEFAULT_INNER_HEIGHT,
    });
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
  });

  it("toggles guide drawer, collapses DayStrip with inert, handles Esc priority & mutual exclusion", async () => {
    useTripStore.getState().setResult("91500", "job_7c_1", makeGuideResult());
    renderBookPage("/plan/91500/plan_a?job_id=job_7c_1");

    await waitFor(() => {
      expect(guideMapSpy.fitViewCalls.length).toBeGreaterThanOrEqual(1);
    });

    const atlas = document.querySelector("main.bk-atlas") as HTMLElement;
    const dayStrip = screen.getByRole("region", { name: "这一天" });
    const guideBtn = screen.getByRole("button", { name: "攻略" });

    // “攻略”是右上一排第一个按钮
    const barButtons = Array.from(
      document.querySelectorAll(".bk-bar button"),
    ).map((b) => b.textContent?.trim());
    expect(barButtons).toEqual([
      "攻略",
      "出发前",
      "怎么去 · 住哪",
      "花多少",
      "导出 PDF",
      "分享长图",
    ]);
    expect(guideBtn).toHaveAttribute("aria-pressed", "false");
    expect(atlas.classList.contains("is-reading")).toBe(false);

    // 点开“攻略”：长卷收下且不可键盘聚焦，地图视野换为读攻略避让边距
    fireEvent.click(guideBtn);
    expect(guideBtn).toHaveAttribute("aria-pressed", "true");
    expect(atlas.classList.contains("is-reading")).toBe(true);
    expect(dayStrip.classList.contains("is-collapsed")).toBe(true);
    expect(dayStrip).toHaveAttribute("aria-hidden", "true");
    expect(dayStrip).toHaveAttribute("inert");
    expect((dayStrip as HTMLElement & { inert: boolean }).inert).toBe(true);
    expect(
      screen.getByRole("complementary", { name: "全程攻略" }),
    ).toBeInTheDocument();

    const lastFitOnOpen =
      guideMapSpy.fitViewCalls[guideMapSpy.fitViewCalls.length - 1];
    expect(lastFitOnOpen.avoid).toEqual([90, 70, 380, 600]);

    // 再点“攻略”关上：恢复长卷与平时视野边距
    fireEvent.click(guideBtn);
    expect(guideBtn).toHaveAttribute("aria-pressed", "false");
    expect(atlas.classList.contains("is-reading")).toBe(false);
    expect(dayStrip.classList.contains("is-collapsed")).toBe(false);
    expect(dayStrip).not.toHaveAttribute("inert");
    expect(
      screen.queryByRole("complementary", { name: "全程攻略" }),
    ).toBeNull();
    const lastFitOnClose =
      guideMapSpy.fitViewCalls[guideMapSpy.fitViewCalls.length - 1];
    expect(lastFitOnClose.avoid).toEqual([90, 275, 380, 140]);

    // 点开后点右上 × 关上
    fireEvent.click(guideBtn);
    fireEvent.click(screen.getByRole("button", { name: "关上" }));
    expect(
      screen.queryByRole("complementary", { name: "全程攻略" }),
    ).toBeNull();
    expect(atlas.classList.contains("is-reading")).toBe(false);

    // 分享弹窗压在攻略上时，第一下 Esc 只关分享，第二下 Esc 才关攻略
    vi.mocked(apiModule.getArtifact).mockResolvedValue({
      ok: true,
      artifact_id: "art-img",
      result_record_id: 91500,
      artifact_type: "share_image",
      status: "ready",
      download_url: "/api/trip/results/91500/artifacts/share_image/download",
      filename: "poster.png",
      mime_type: "image/png",
      byte_size: 1200,
      page_count: null,
      width_px: 1080,
      height_px: 1920,
      expires_time: "",
      metadata: {},
      error: null,
    });
    vi.mocked(apiModule.fetchArtifactBlob).mockResolvedValue(
      new Blob(["png"], { type: "image/png" }),
    );
    fireEvent.click(guideBtn);
    fireEvent.click(screen.getByRole("button", { name: "分享长图" }));
    expect(await screen.findByText("分享行程 AI 长图")).toBeInTheDocument();

    fireEvent.keyDown(document, { key: "Escape" });
    await waitFor(() => {
      expect(screen.queryByText("分享行程 AI 长图")).toBeNull();
    });
    expect(
      screen.getByRole("complementary", { name: "全程攻略" }),
    ).toBeInTheDocument();

    fireEvent.keyDown(document, { key: "Escape" });
    expect(
      screen.queryByRole("complementary", { name: "全程攻略" }),
    ).toBeNull();

    // 互斥：攻略开着时点“出发前”仍保持阅读模式与长卷收下；点“看原因”、地图上的“住”换页并恢复长卷
    fireEvent.click(guideBtn);
    fireEvent.click(screen.getByRole("button", { name: "出发前" }));
    expect(
      screen.getByRole("complementary", { name: "出发前" }),
    ).toBeInTheDocument();
    expect(atlas.classList.contains("is-reading")).toBe(true);
    expect(dayStrip).toHaveAttribute("inert");

    fireEvent.click(guideBtn);
    fireEvent.click(screen.getByRole("button", { name: /看原因/ }));
    expect(
      screen.getByRole("complementary", { name: "想去但没排进来的" }),
    ).toBeInTheDocument();
    expect(atlas.classList.contains("is-reading")).toBe(false);
    expect(dayStrip).not.toHaveAttribute("inert");

    fireEvent.click(guideBtn);
    const stayBtn = document.querySelector("button.gstay") as HTMLButtonElement;
    fireEvent.click(stayBtn);
    expect(
      screen.getByRole("complementary", { name: "住处" }),
    ).toBeInTheDocument();
    expect(atlas.classList.contains("is-reading")).toBe(false);
    expect(dayStrip).not.toHaveAttribute("inert");
  });

  it("renders guide content strictly from backend truth, batches details without duplicates, and handles broken photos", async () => {
    vi.mocked(apiModule.fetchPlaceDetail).mockImplementation(async (id) => {
      if (id === 325) {
        throw new ApiRequestError("NOT_FOUND", "not found", 404);
      }
      return makePlaceDetail(id, `地点${id}`);
    });

    const sample = makeGuideResult();
    useTripStore.getState().setResult("91500", "job_7c_content", sample);
    const r1 = renderBookPage("/plan/91500/plan_a?job_id=job_7c_content");

    // 初始在第一天：只取了第一天的 311
    await waitFor(() => {
      expect(apiModule.fetchPlaceDetail).toHaveBeenCalledTimes(1);
      expect(apiModule.fetchPlaceDetail).toHaveBeenCalledWith(311);
    });

    // 点开攻略：一次性补齐第二天 321..325，绝不重复请求已取过的 311
    fireEvent.click(screen.getByRole("button", { name: "攻略" }));
    await waitFor(() => {
      expect(apiModule.fetchPlaceDetail).toHaveBeenCalledTimes(6);
    });
    const fetchedIds = vi
      .mocked(apiModule.fetchPlaceDetail)
      .mock.calls.map((c) => c[0]);
    expect(fetchedIds).toEqual([311, 321, 322, 323, 324, 325]);

    // 关上再重开攻略：已成功与已失败的都不重取
    fireEvent.click(screen.getByRole("button", { name: "关上" }));
    fireEvent.click(screen.getByRole("button", { name: "攻略" }));
    expect(apiModule.fetchPlaceDetail).toHaveBeenCalledTimes(6);

    const aside = screen.getByRole("complementary", { name: "全程攻略" });
    // 页签顺序与天气
    const rtabs = Array.from(aside.querySelectorAll(".bk-rtab"));
    expect(rtabs.map((t) => t.getAttribute("data-rday"))).toEqual(["1", "2"]);
    expect(rtabs[0]).toHaveTextContent("第一天阴 11–14°");
    expect(rtabs[0]).toHaveAttribute("aria-pressed", "true");
    expect(rtabs[1]).toHaveTextContent("第二天多云 12–16°");

    // 天标题、叙述与受治理时间；通勤摘要与站间通勤行重复，不再显示；一页一天此时只渲染第一天
    expect(aside).toHaveTextContent("重庆动物园全日深游");
    expect(aside).toHaveTextContent("整日只安排动物园一个主景点。");
    expect(aside).not.toHaveTextContent("园内步行游览");
    expect(aside.querySelector('li.bk-gs[data-place-id="323"]')).toBeNull();
    expect(aside.querySelector(".bk-rd-end")).toBeNull();

    const zooStop = aside.querySelector(
      'li.bk-gs[data-place-id="311"]',
    ) as HTMLLIElement;
    expect(zooStop).toHaveTextContent("上午 · 建议 1.5 小时");
    expect(zooStop).not.toHaveTextContent("09:30"); // exact_time_source: null 不写准点
    expect(zooStop).toHaveTextContent(
      "西南代表性动物园。按大熊猫馆到金鱼池慢走。",
    );

    // 按 position 升序选第一张 thumb；图挂或 404 时退回“暂无实拍”
    const zooImg = zooStop.querySelector("img.bk-gs-ph") as HTMLImageElement;
    expect(zooImg.getAttribute("src")).toBe("https://img.example/311-0-t.webp");
    fireEvent.error(zooImg);
    expect(zooStop.querySelector("img.bk-gs-ph")).toBeNull();
    expect(zooStop).toHaveTextContent("暂无实拍");

    // 翻到第二天（最后一天）：验证第二天站点、通勤、缺图回退与页尾提示
    fireEvent.click(aside.querySelector('.bk-rtab[data-rday="2"]')!);
    expect(aside.querySelector('li.bk-gs[data-place-id="311"]')).toBeNull();

    const hongyaStop = aside.querySelector(
      'li.bk-gs[data-place-id="323"]',
    ) as HTMLLIElement;
    expect(hongyaStop).toHaveTextContent("傍晚 19:30 起 · 建议 1.5 小时");

    const qiansimenStop = aside.querySelector(
      'li.bk-gs[data-place-id="324"]',
    ) as HTMLLIElement;
    expect(qiansimenStop).toHaveTextContent("可选");

    // 站间通勤：steps / 纯方式分钟 / 估算+summary / 缺 leg 不渲染
    const legs = Array.from(aside.querySelectorAll("li.bk-gl"));
    expect(legs).toHaveLength(3); // 321->322, 322->323, 323->324; 324->325 无记录不画
    expect(legs[0]).toHaveTextContent("公交 22 分钟");
    expect(legs[0]).toHaveTextContent(
      "轨道交通1号线 · 较场口站上车 → 小什字站 · 2 站 · 6 分钟",
    );
    expect(legs[1]).toHaveTextContent("步行 10 分钟");
    expect(legs[1]).not.toHaveTextContent("估算");
    expect(legs[2]).toHaveTextContent("骑行 12 分钟 · 估算");
    expect(legs[2]).toHaveTextContent("线路明细暂不可用，请以现场导航为准");

    const missingPhotoStop = aside.querySelector(
      'li.bk-gs[data-place-id="325"]',
    ) as HTMLLIElement;
    expect(missingPhotoStop).toHaveTextContent("暂无实拍");

    // 不得出现原型假文案，末尾文案准确（仅最后一天出现）
    expect(aside.textContent).not.toContain("导出 PDF 用的就是这一份");
    expect(aside.textContent).not.toContain("在地图上看");
    expect(aside.textContent).not.toContain("推荐理由");
    expect(aside.querySelector(".bk-rd-end")?.textContent?.trim()).toBe(
      "出发前、怎么去 · 住哪、花多少在右上另外几页。",
    );
    r1.unmount();

    // BASIC / 字段全空回退：无天气、无叙述、无通勤摘要、无笔记时干净省略
    const minimal = makeGuideResult({
      weather: { status: "skipped_disabled", city: "重庆", days: [] },
    });
    minimal.plans[0].days = [
      {
        day: 1,
        title: "首日简版",
        narrative: "",
        commute_summary: "",
        pace_status: "WITHIN_LIMIT",
        places: [
          {
            place_id: 399,
            name: "山城老站",
            category: "attraction",
            longitude: 106.58,
            latitude: 29.56,
            role: "anchor_activity",
            optional: false,
            brief: "写成咖啡/茶歇休息点，不要补充无来源事实",
            activity_note: "",
          },
        ],
        commute_legs: [],
      },
    ];
    vi.mocked(apiModule.fetchPlaceDetail).mockImplementation(async (id) => ({
      ...makePlaceDetail(id, `地点${id}`),
      gallery: [],
    }));
    useTripStore.getState().setResult("91500", "job_7c_min", minimal);
    renderBookPage("/plan/91500/plan_a?job_id=job_7c_min");
    fireEvent.click(screen.getByRole("button", { name: "攻略" }));
    const minAside = screen.getByRole("complementary", { name: "全程攻略" });
    expect(minAside.querySelector(".bk-wx")).toBeNull();
    expect(minAside.querySelector(".bk-ga-nar")).toBeNull();
    expect(minAside.querySelector(".bk-rd-commute-sum")).toBeNull();
    expect(minAside.querySelector(".bk-gs-n")).toBeNull();
    expect(minAside).toHaveTextContent("山城老站");
    // 详情有结果后，没照片也没说明的站收成一行，不留“暂无实拍”灰块
    const bareStop = minAside.querySelector(
      'li.bk-gs[data-place-id="399"]',
    ) as HTMLLIElement;
    await waitFor(() => expect(bareStop).toHaveClass("is-bare"));
    expect(bareStop.querySelector(".bk-gs-ph")).toBeNull();
    expect(bareStop).not.toHaveTextContent("暂无实拍");
  });

  it("opens directly at already-selected StopPanel stop or current Day 2 start", async () => {
    const sample = makeGuideResult();
    useTripStore.getState().setResult("91500", "job_7c_anchor", sample);

    const scrollToSpy = vi.fn();
    Object.defineProperty(HTMLElement.prototype, "scrollTo", {
      configurable: true,
      writable: true,
      value: scrollToSpy,
    });

    const r1 = renderBookPage("/plan/91500/plan_a?job_id=job_7c_anchor");
    // 先点第二天，再点“洪崖洞”打开站点详情
    fireEvent.click(screen.getByRole("button", { name: /第二天/ }));
    fireEvent.click(screen.getByRole("button", { name: /3\s*洪崖洞/ }));
    expect(
      screen.getByRole("complementary", { name: "洪崖洞 详情" }),
    ).toBeInTheDocument();

    scrollToSpy.mockClear();
    fireEvent.click(screen.getByRole("button", { name: "攻略" }));
    const aside = screen.getByRole("complementary", { name: "全程攻略" });
    const stop323 = aside.querySelector('li.bk-gs[data-place-id="323"]');
    expect(stop323?.classList.contains("is-reading")).toBe(true);
    expect(stop323).toHaveAttribute("aria-current", "location");
    expect(scrollToSpy).toHaveBeenCalled();
    r1.unmount();

    // 换在第二天、没选具体哪站时点开攻略 -> 直接呈现第二天首页并高亮第二天第一站
    useTripStore.getState().setResult("91500", "job_7c_day2", sample);
    renderBookPage("/plan/91500/plan_a?job_id=job_7c_day2&day=2");
    fireEvent.click(screen.getByRole("button", { name: "攻略" }));
    const asideDay2 = screen.getByRole("complementary", { name: "全程攻略" });
    expect(asideDay2.querySelector('li.bk-gs[data-place-id="311"]')).toBeNull();
    expect(
      asideDay2.querySelector('li.bk-gs[data-place-id="321"]'),
    ).toHaveAttribute("aria-current", "location");
    expect(asideDay2.querySelector('.bk-rtab[data-rday="2"]')).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(asideDay2.scrollTop).toBe(0);
  });

  it("syncs scroll-spy IO, map pin highlight & panning within a page and across page turns without opening StopPanel", async () => {
    const sample = makeGuideResult();
    useTripStore.getState().setResult("91500", "job_7c_sync", sample);
    guideMapSpy.enableMapEvents = true;

    const scrollToSpy = vi.fn();
    Object.defineProperty(HTMLElement.prototype, "scrollTo", {
      configurable: true,
      writable: true,
      value: scrollToSpy,
    });

    renderBookPage("/plan/91500/plan_a?job_id=job_7c_sync");
    await waitFor(() => {
      expect(guideMapSpy.markers.length).toBeGreaterThanOrEqual(1);
    });

    fireEvent.click(screen.getByRole("button", { name: "攻略" }));
    const aside = screen.getByRole("complementary", { name: "全程攻略" });

    // 第一天页面：IntersectionObserver 只观察第一天的站点
    const day1Io = ioSpy.instances[ioSpy.instances.length - 1];
    expect(day1Io.options?.root).toBe(aside);
    expect(day1Io.options?.rootMargin).toBe("-28% 0px -62% 0px");
    expect(day1Io.observed).toHaveLength(1);

    // 翻到第二天：地图换到第二天，IO 重新观察第二天全部 5 个站点
    fireEvent.click(aside.querySelector('.bk-rtab[data-rday="2"]')!);
    const activeIo = ioSpy.instances[ioSpy.instances.length - 1];
    expect(activeIo.options?.root).toBe(aside);
    expect(activeIo.options?.rootMargin).toBe("-28% 0px -62% 0px");
    expect(activeIo.observed).toHaveLength(5);

    // 在第二天页内模拟滚到“洪崖洞”(323) 且落点被右边攻略挡住（x = 1120），检查等待 moveend 后让位、且不打开 StopPanel
    guideMapSpy.lngLatContainerResult = { x: 1120, y: 320 };
    guideMapSpy.panToCalls = [];
    const el322 = aside.querySelector('li.bk-gs[data-place-id="322"]')!;
    const el323 = aside.querySelector('li.bk-gs[data-place-id="323"]')!;

    await act(async () => {
      activeIo.callback(
        [
          {
            target: el322,
            isIntersecting: true,
            boundingClientRect: {
              top: 40,
              height: 110,
              bottom: 150,
              left: 0,
              right: 500,
              width: 500,
              x: 0,
              y: 40,
              toJSON: () => ({}),
            } as DOMRectReadOnly,
          } as IntersectionObserverEntry,
          {
            target: el323,
            isIntersecting: true,
            boundingClientRect: {
              top: 170,
              height: 120,
              bottom: 290,
              left: 0,
              right: 500,
              width: 500,
              x: 0,
              y: 170,
              toJSON: () => ({}),
            } as DOMRectReadOnly,
          } as IntersectionObserverEntry,
        ],
        activeIo as unknown as IntersectionObserver,
      );
    });

    expect(el323.classList.contains("is-reading")).toBe(true);
    expect(el323).toHaveAttribute("aria-current", "location");
    expect(aside.querySelector('.bk-rtab[data-rday="2"]')).toHaveAttribute(
      "aria-pressed",
      "true",
    );

    // 地图在第二天亮起洪崖洞(第3站)，且未开出 StopPanel
    const hongyaPin = document.querySelector(
      'button.gpin[data-place-id="323"]',
    ) as HTMLButtonElement;
    expect(hongyaPin).not.toBeNull();
    expect(hongyaPin.classList.contains("is-on")).toBe(true);
    expect(screen.queryByRole("complementary", { name: /详情$/ })).toBeNull();

    // moveend 到位前不抢跑 panTo，moveend 触发后让开右边抽屉
    expect(guideMapSpy.panToCalls).toHaveLength(0);
    await act(async () => {
      emitMapEvent("moveend");
    });
    expect(guideMapSpy.panToCalls).toHaveLength(1);

    // 点地图上的第一站“山城巷”(321) -> 攻略滚过去并切换正在读，不打开 StopPanel
    scrollToSpy.mockClear();
    const shanchengPin = document.querySelector(
      'button.gpin[data-place-id="321"]',
    ) as HTMLButtonElement;
    fireEvent.click(shanchengPin);
    expect(
      aside.querySelector('li.bk-gs[data-place-id="321"]'),
    ).toHaveAttribute("aria-current", "location");
    expect(shanchengPin.classList.contains("is-on")).toBe(true);
    expect(scrollToSpy).toHaveBeenCalled();
    expect(screen.queryByRole("complementary", { name: /详情$/ })).toBeNull();

    // 点攻略里的某一站 -> 滚过去并切为正在读
    scrollToSpy.mockClear();
    fireEvent.click(screen.getByRole("button", { name: /4\s*千厮门大桥/ }));
    expect(
      aside.querySelector('li.bk-gs[data-place-id="324"]'),
    ).toHaveAttribute("aria-current", "location");
    expect(scrollToSpy).toHaveBeenCalled();

    // 点攻略顶部“第一天”页签 -> 翻回第一天，地图也回到第一天并亮起重庆动物园
    fireEvent.click(aside.querySelector('.bk-rtab[data-rday="1"]')!);
    expect(aside.querySelector('.bk-rtab[data-rday="1"]')).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(
      aside.querySelector('li.bk-gs[data-place-id="311"]'),
    ).toHaveAttribute("aria-current", "location");
    expect(
      document.querySelector('button.gpin[data-place-id="311"]'),
    ).not.toBeNull();
  });

  it("does not interrupt cross-district setFitView before moveend or 1600ms settle cap", async () => {
    const sample = makeGuideResult();
    useTripStore.getState().setResult("91500", "job_7c_cap", sample);
    guideMapSpy.enableMapEvents = true;
    guideMapSpy.lngLatContainerResult = { x: 1120, y: 320 };

    Object.defineProperty(HTMLElement.prototype, "scrollTo", {
      configurable: true,
      writable: true,
      value: vi.fn(),
    });

    renderBookPage("/plan/91500/plan_a?job_id=job_7c_cap");
    await waitFor(() => {
      expect(guideMapSpy.markers.length).toBeGreaterThanOrEqual(1);
    });

    fireEvent.click(screen.getByRole("button", { name: "攻略" }));
    const aside = screen.getByRole("complementary", { name: "全程攻略" });

    vi.useFakeTimers();
    try {
      guideMapSpy.panToCalls = [];
      // 翻到第二天触发跨区 setFitView
      fireEvent.click(aside.querySelector('.bk-rtab[data-rday="2"]')!);

      await act(async () => {
        vi.advanceTimersByTime(1200);
      });
      expect(guideMapSpy.panToCalls).toHaveLength(0);

      await act(async () => {
        vi.advanceTimersByTime(500);
      });
      expect(guideMapSpy.panToCalls).toHaveLength(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it("honors prefers-reduced-motion for scroll behavior, setFitView & pin nudge, and preserves 7c CSS rules", async () => {
    mockMatchMedia(true);
    const sample = makeGuideResult();
    useTripStore.getState().setResult("91500", "job_7c_rm", sample);
    guideMapSpy.lngLatContainerResult = { x: 1120, y: 320 };

    const scrollToSpy = vi.fn();
    Object.defineProperty(HTMLElement.prototype, "scrollTo", {
      configurable: true,
      writable: true,
      value: scrollToSpy,
    });

    renderBookPage("/plan/91500/plan_a?job_id=job_7c_rm");
    await waitFor(() => {
      expect(guideMapSpy.markers.length).toBeGreaterThanOrEqual(1);
    });

    fireEvent.click(screen.getByRole("button", { name: "攻略" }));
    const aside = screen.getByRole("complementary", { name: "全程攻略" });
    const latestFit =
      guideMapSpy.fitViewCalls[guideMapSpy.fitViewCalls.length - 1];
    expect(latestFit.immediately).toBe(true);
    expect(guideMapSpy.setCenterCalls.some((c) => c.immediately === true)).toBe(
      true,
    );
    expect(guideMapSpy.panToCalls).toHaveLength(0);

    // 切到第二天再点某一站，滚动行为应为 auto（不平滑滚动）
    fireEvent.click(aside.querySelector('.bk-rtab[data-rday="2"]')!);
    scrollToSpy.mockClear();
    fireEvent.click(screen.getByRole("button", { name: /3\s*洪崖洞/ }));
    expect(scrollToSpy).toHaveBeenCalledWith(
      expect.objectContaining({ behavior: "auto" }),
    );

    const css = fs.readFileSync(
      path.resolve(process.cwd(), "src/pages/book/spread-book.css"),
      "utf8",
    );
    expect(css).toContain(".bk-atlas.is-reading .gpin.is-on + .gpl::before");
    expect(css).toContain('content: "正在读 · ";');
    expect(css).toContain("padding: 0 28px 28px;");
    const reducedBlockMatch =
      /@media\s*\(prefers-reduced-motion:\s*reduce\)\s*\{([\s\S]*)\}\s*$/m.exec(
        css,
      );
    expect(reducedBlockMatch).not.toBeNull();
    expect(reducedBlockMatch![1]).toContain(".bk-day");
    expect(reducedBlockMatch![1]).toContain(".bk-gs");
    expect(reducedBlockMatch![1]).toContain(".bk-rd.is-next > *");
    expect(reducedBlockMatch![1]).toContain(".bk-rd.is-prev > *");
  });

  it("攻略一页一天：页尾翻页按钮在首日只出下一天、末日只出上一天与末尾提示", async () => {
    const sample = makeGuideResult();
    useTripStore.getState().setResult("91500", "job_7c_pager", sample);
    renderBookPage("/plan/91500/plan_a?job_id=job_7c_pager");
    await waitFor(() => {
      expect(guideMapSpy.markers.length).toBeGreaterThanOrEqual(1);
    });

    fireEvent.click(screen.getByRole("button", { name: "攻略" }));
    const aside = screen.getByRole("complementary", { name: "全程攻略" });

    // 第一天：有“下一天 / 第二天 · 山城巷至洪崖洞”，无“上一天”，无末尾提示
    const pagerDay1 = aside.querySelector("nav.bk-rd-pager")!;
    expect(pagerDay1).not.toBeNull();
    expect(pagerDay1.querySelector("button.prev")).toBeNull();
    const nextBtn = pagerDay1.querySelector("button.next")!;
    expect(nextBtn).toHaveTextContent("下一天");
    expect(nextBtn).toHaveTextContent("第二天 · 山城巷至洪崖洞");
    expect(aside.querySelector(".bk-rd-end")).toBeNull();

    // 点“下一天”翻到第二天：挂 is-next，地图切到第二天，出现“上一天 · 第一天”和末尾提示
    fireEvent.click(nextBtn);
    expect(aside.querySelector(".bk-rd")?.classList.contains("is-next")).toBe(
      true,
    );
    expect(aside).toHaveTextContent("山城巷至洪崖洞");
    expect(
      document.querySelector('button.gpin[data-place-id="321"]'),
    ).not.toBeNull();
    const pagerDay2 = aside.querySelector("nav.bk-rd-pager")!;
    const prevBtn = pagerDay2.querySelector("button.prev")!;
    expect(prevBtn).toHaveTextContent("上一天");
    expect(prevBtn).toHaveTextContent("第一天");
    expect(pagerDay2.querySelector("button.next")).toBeNull();
    expect(aside.querySelector(".bk-rd-end")?.textContent?.trim()).toBe(
      "出发前、怎么去 · 住哪、花多少在右上另外几页。",
    );

    // 点“上一天”翻回第一天：挂 is-prev，地图切回第一天
    fireEvent.click(prevBtn);
    expect(aside.querySelector(".bk-rd")?.classList.contains("is-prev")).toBe(
      true,
    );
    expect(aside).toHaveTextContent("重庆动物园全日深游");
    expect(
      document.querySelector('button.gpin[data-place-id="311"]'),
    ).not.toBeNull();
  });

  it("攻略滚读纯函数与滚到底部时能把最后一站判为正在读（无需 58vh 底部留白）", async () => {
    // 1. 纯函数验证：顶部选中第一站、接近底部时阅读线顺滑下移、到底直接命中最后一站
    const stops = [
      { placeId: 1, top: 100, height: 180 },
      { placeId: 2, top: 300, height: 180 },
      { placeId: 3, top: 500, height: 180 },
    ];
    expect(
      pickGuideReadingStopId(stops, {
        asideTop: 0,
        asideHeight: 600,
        scrollTop: 0,
        scrollHeight: 820,
      }),
    ).toBe(1);
    expect(
      pickGuideReadingStopId(
        [
          { placeId: 1, top: -120, height: 180 },
          { placeId: 2, top: 80, height: 180 },
          { placeId: 3, top: 280, height: 180 },
        ],
        {
          asideTop: 0,
          asideHeight: 600,
          scrollTop: 220,
          scrollHeight: 820,
        },
      ),
    ).toBe(3);

    // 2. 组件内触发真实 scroll 滚到最底 -> 最后一站 325 高亮
    const sample = makeGuideResult();
    useTripStore.getState().setResult("91500", "job_7c_scroll_bottom", sample);
    renderBookPage("/plan/91500/plan_a?job_id=job_7c_scroll_bottom&day=2");
    await waitFor(() => {
      expect(guideMapSpy.markers.length).toBeGreaterThanOrEqual(1);
    });

    fireEvent.click(screen.getByRole("button", { name: "攻略" }));
    const aside = screen.getByRole("complementary", { name: "全程攻略" });

    Object.defineProperty(aside, "clientHeight", {
      configurable: true,
      value: 500,
    });
    Object.defineProperty(aside, "scrollHeight", {
      configurable: true,
      value: 1100,
    });
    Object.defineProperty(aside, "scrollTop", {
      configurable: true,
      writable: true,
      value: 600,
    });

    fireEvent.scroll(aside);
    expect(
      aside.querySelector('li.bk-gs[data-place-id="325"]'),
    ).toHaveAttribute("aria-current", "location");
    expect(
      document
        .querySelector('button.gpin[data-place-id="325"]')
        ?.classList.contains("is-on"),
    ).toBe(true);
  });
});
