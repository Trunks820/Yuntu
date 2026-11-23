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
import { useTripStore } from "@/stores/tripStore";
import type { TripResult, WeatherDay } from "@/types/trip";
import {
  WEATHER_ICON_VISUAL_MAP,
  buildWeatherReminderContent,
  computeSunBubblePosition,
  computeSunDropBounds,
  pickSunDropPlacement,
  resolveBookMapWeatherVisual,
} from "@/utils/spreadBook";

const weatherMapSpy = vi.hoisted(() => ({
  polylines: [] as any[],
  markers: [] as any[],
  customLayers: [] as Array<{
    layer: any;
    dom: HTMLElement;
    opts: any;
    attached: boolean;
    detachedCount: number;
  }>,
  enableMapEvents: false,
  mapListeners: new Map<string, Set<() => void>>(),
  lngLatPoints: new Map<string, { x: number; y: number }>(),
  fitViewCalls: [] as any[],
  panToCalls: [] as any[],
  destroyedCount: 0,
}));

const ioSpy = vi.hoisted(() => ({
  instances: [] as Array<{
    callback: IntersectionObserverCallback;
    observed: Element[];
  }>,
}));

function emitWeatherMapEvent(event: string) {
  const listeners = weatherMapSpy.mapListeners.get(event);
  if (!listeners) return;
  for (const cb of Array.from(listeners)) {
    cb();
  }
}

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
          weatherMapSpy.polylines.push(opts);
        }
        setMap = vi.fn();
      }
      class MockMarker {
        opts: any;
        constructor(opts: any) {
          this.opts = opts;
          weatherMapSpy.markers.push(opts);
        }
        on = vi.fn();
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
      class MockCustomLayer {
        dom: HTMLElement;
        opts: any;
        record: (typeof weatherMapSpy.customLayers)[number];
        constructor(dom: HTMLElement, opts: any) {
          this.dom = dom;
          this.opts = opts;
          this.record = {
            layer: this,
            dom,
            opts,
            attached: false,
            detachedCount: 0,
          };
          weatherMapSpy.customLayers.push(this.record);
        }
        setMap = vi.fn((map: MockMap | null) => {
          if (map?.container) {
            this.record.attached = true;
            const layersHost =
              map.container.querySelector(".amap-layers") ?? map.container;
            const wrap = document.createElement("div");
            wrap.className = "amap-layer amap-e";
            wrap.style.zIndex = String(this.opts?.zIndex ?? 120);
            wrap.appendChild(this.dom);
            layersHost.appendChild(wrap);
          } else {
            this.record.attached = false;
            this.record.detachedCount += 1;
            const wrap = this.dom.parentElement;
            if (wrap?.classList.contains("amap-layer") && wrap.parentNode) {
              wrap.parentNode.removeChild(wrap);
            } else if (this.dom.parentNode) {
              this.dom.parentNode.removeChild(this.dom);
            }
          }
        });
      }
      class MockMap {
        container: HTMLElement;
        constructor(container: HTMLElement) {
          this.container = container;
          const layers = document.createElement("div");
          layers.className = "amap-layers";
          container.appendChild(layers);
        }
        on = vi.fn((event: string, cb: () => void) => {
          if (!weatherMapSpy.enableMapEvents) return;
          let set = weatherMapSpy.mapListeners.get(event);
          if (!set) {
            set = new Set();
            weatherMapSpy.mapListeners.set(event, set);
          }
          set.add(cb);
        });
        off = vi.fn((event: string, cb: () => void) => {
          weatherMapSpy.mapListeners.get(event)?.delete(cb);
        });
        setFitView = vi.fn(
          (
            targets: any[],
            immediately: boolean,
            avoid: number[],
            maxZoom: number,
          ) => {
            weatherMapSpy.fitViewCalls.push({
              targets,
              immediately,
              avoid,
              maxZoom,
            });
          },
        );
        lngLatToContainer = vi.fn((lngLat: [number, number]) => {
          const key = `${lngLat[0]},${lngLat[1]}`;
          const mapped = weatherMapSpy.lngLatPoints.get(key);
          return new MockPixel(mapped?.x ?? 520, mapped?.y ?? 260);
        });
        containerToLngLat = vi.fn((px: any) => [
          104.05 + (px?.x ?? 0) * 0.0001,
          30.65,
        ]);
        panTo = vi.fn((center: any) => {
          weatherMapSpy.panToCalls.push(center);
        });
        setCenter = vi.fn();
        resize = vi.fn();
        destroy = vi.fn(() => {
          weatherMapSpy.destroyedCount += 1;
          weatherMapSpy.mapListeners.clear();
        });
      }
      return {
        Map: MockMap,
        Marker: MockMarker,
        Polyline: MockPolyline,
        CustomLayer: MockCustomLayer,
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
      matches: query.includes("prefers-reduced-motion") ? reduced : false,
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

function makeWeatherResult(overrides?: {
  status?: string;
  days?: WeatherDay[];
}): TripResult {
  const defaultDays: WeatherDay[] = [
    {
      day: 1,
      date: "2026-11-14",
      weather_text: "晴",
      temp_min_c: 14,
      temp_max_c: 25,
      wind_text: "南风 1级",
      icon_code: "sunny",
      reminders: ["紫外线较强，注意防晒"],
    },
    {
      day: 2,
      date: "2026-11-15",
      weather_text: "中雨",
      temp_min_c: 11,
      temp_max_c: 17,
      wind_text: "东北风 3级",
      icon_code: "moderate_rain",
      reminders: ["出门记得带伞"],
    },
    {
      day: 3,
      date: "2026-11-16",
      weather_text: "晴转多云",
      temp_min_c: 26,
      temp_max_c: 41,
      wind_text: "",
      icon_code: "clear",
      reminders: ["午间体感偏热"],
    },
    {
      day: 4,
      date: "2026-11-17",
      weather_text: "微风",
      temp_min_c: 13,
      temp_max_c: 21,
      wind_text: "西风 4级",
      icon_code: "windy",
      reminders: [],
    },
  ];

  return {
    result_id: "92000",
    job_id: "job_7d",
    status: "succeeded",
    city: { name: "成都", adcode: "510100" },
    request: {
      destination: "成都",
      days: 4,
      people_count: 2,
      budget_level: "comfortable",
    },
    must_include: [],
    weather: {
      status: overrides?.status ?? "ok",
      city: "成都",
      days: overrides?.days ?? defaultDays,
    },
    plans: [
      {
        plan_id: "plan_a",
        title: "锦城四日天气样本",
        summary: "测试不同天气图层与太阳交互",
        tags: ["休闲"],
        accommodation: {
          area: "春熙路片区",
          reason: "交通方便",
          longitude: 104.081,
          latitude: 30.658,
        },
        days: [
          {
            day: 1,
            title: "第一天晴游",
            places: [
              {
                place_id: 101,
                name: "人民公园",
                category: "park",
                time_slot: "morning",
                longitude: 104.055,
                latitude: 30.659,
                address: "少城路",
                stay_minutes: 90,
                reason: "喝茶起点",
                optional: false,
              },
              {
                place_id: 102,
                name: "宽窄巷子",
                category: "scenic",
                time_slot: "afternoon",
                longitude: 104.052,
                latitude: 30.664,
                address: "长顺上街",
                stay_minutes: 90,
                reason: "逛老街",
                optional: false,
              },
            ],
            commute_legs: [],
          },
          {
            day: 2,
            title: "第二天听雨",
            places: [
              {
                place_id: 201,
                name: "杜甫草堂",
                category: "museum",
                time_slot: "morning",
                longitude: 104.028,
                latitude: 30.66,
                address: "青华路",
                stay_minutes: 120,
                reason: "雨天逛园",
                optional: false,
              },
              {
                place_id: 202,
                name: "浣花溪公园",
                category: "park",
                time_slot: "afternoon",
                longitude: 104.033,
                latitude: 30.657,
                address: "沧浪路",
                stay_minutes: 60,
                reason: "沿河散步",
                optional: false,
              },
            ],
            commute_legs: [],
          },
          {
            day: 3,
            title: "第三天放晴",
            places: [
              {
                place_id: 301,
                name: "望江楼公园",
                category: "park",
                time_slot: "morning",
                longitude: 104.089,
                latitude: 30.631,
                address: "望江路",
                stay_minutes: 90,
                reason: "竹林漫步",
                optional: false,
              },
            ],
            commute_legs: [],
          },
          {
            day: 4,
            title: "第四天清风",
            places: [
              {
                place_id: 401,
                name: "东郊记忆",
                category: "scenic",
                time_slot: "morning",
                longitude: 104.122,
                latitude: 30.668,
                address: "建设南支路",
                stay_minutes: 90,
                reason: "工业园展",
                optional: false,
              },
            ],
            commute_legs: [],
          },
        ],
      },
    ],
  } as unknown as TripResult;
}

function renderBookWeatherPage(
  initialPath = "/plan/92000/plan_a?job_id=job_7d",
) {
  return render(
    <MemoryRouter initialEntries={[initialPath]}>
      <Routes>
        <Route path="/plan/:resultId/:planId" element={<SpreadBookPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("SpreadBook Step 7d: weather map layer & sunny drop", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    weatherMapSpy.polylines = [];
    weatherMapSpy.markers = [];
    weatherMapSpy.customLayers = [];
    weatherMapSpy.enableMapEvents = false;
    weatherMapSpy.mapListeners.clear();
    weatherMapSpy.lngLatPoints.clear();
    weatherMapSpy.fitViewCalls = [];
    weatherMapSpy.panToCalls = [];
    weatherMapSpy.destroyedCount = 0;

    ioSpy.instances = [];
    class MockIntersectionObserver {
      callback: IntersectionObserverCallback;
      observed: Element[] = [];
      constructor(cb: IntersectionObserverCallback) {
        this.callback = cb;
        ioSpy.instances.push(this);
      }
      observe = vi.fn((el: Element) => {
        this.observed.push(el);
      });
      unobserve = vi.fn();
      disconnect = vi.fn();
      takeRecords = vi.fn(() => []);
      root = null;
      rootMargin = "";
      thresholds = [];
    }
    vi.stubGlobal("IntersectionObserver", MockIntersectionObserver);

    mockMatchMedia(false);
    vi.stubEnv("VITE_AMAP_KEY", "test-amap-key");
    vi.stubEnv("VITE_AMAP_SECURITY", "test-amap-security");
    useTripStore.getState().reset();

    vi.mocked(apiModule.fetchPlaceDetail).mockResolvedValue({
      place_id: 101,
      name: "人民公园",
      images: [],
    } as any);
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    Object.defineProperty(document, "hidden", {
      configurable: true,
      get: () => false,
    });
    useTripStore.getState().reset();
  });

  it("maps exact icon_code values uniformly and returns none for unknown/windy/weather_text mismatches", () => {
    const expectedCases: Array<[string | null | undefined, string, boolean]> = [
      ["sunny", "sun", false],
      ["clear", "sun", false],
      [" CLEAR ", "sun", false],
      ["partly_cloudy", "cloud", false],
      ["cloudy", "cloud", false],
      ["overcast", "cloud", false],
      ["light_rain", "rain", false],
      ["rain", "rain", false],
      ["moderate_rain", "rain", false],
      ["heavy_rain", "rain", true],
      ["thunderstorm", "rain", true],
      ["thunder", "rain", true],
      ["Thunder", "rain", true],
      ["light_snow", "snow", false],
      ["snow", "snow", false],
      ["sleet", "snow", false],
      ["heavy_snow", "snow", true],
      ["fog", "fog", false],
      ["haze", "fog", false],
      ["windy", "none", false],
      ["unknown", "none", false],
      ["", "none", false],
      [null, "none", false],
      [undefined, "none", false],
      ["暴雨", "none", false],
      ["stormy", "none", false],
    ];

    for (const [input, kind, heavy] of expectedCases) {
      expect(resolveBookMapWeatherVisual(input)).toEqual({ kind, heavy });
    }
    expect(WEATHER_ICON_VISUAL_MAP.thunderstorm).toEqual({
      kind: "rain",
      heavy: true,
    });
  });

  it("computes sun drop area within map avoid padding and picks point farthest from day stops & stay", () => {
    const normalBounds = computeSunDropBounds(1440, 848, false, false);
    expect(normalBounds.minX).toBeGreaterThanOrEqual(380);
    expect(normalBounds.maxX).toBeLessThanOrEqual(1300);
    expect(normalBounds.minY).toBeGreaterThanOrEqual(90);
    expect(normalBounds.maxY).toBeLessThanOrEqual(548);

    const sideBounds = computeSunDropBounds(1440, 848, true, false);
    expect(sideBounds.maxX).toBeLessThanOrEqual(1020);

    const readingBounds = computeSunDropBounds(1440, 848, true, true);
    expect(readingBounds.maxX).toBeLessThanOrEqual(880);
    expect(readingBounds.maxY).toBeGreaterThan(normalBounds.maxY);

    // Pins clustered on left-top -> dropped sun should pick candidate pushed far right-bottom inside bounds
    const farRight = pickSunDropPlacement(
      1440,
      848,
      false,
      false,
      [
        { x: 420, y: 150 },
        { x: 460, y: 180 },
      ],
      1,
    );
    expect(farRight.target.x).toBeGreaterThanOrEqual(farRight.bounds.minX);
    expect(farRight.target.x).toBeLessThanOrEqual(farRight.bounds.maxX);
    expect(farRight.target.y).toBeGreaterThanOrEqual(farRight.bounds.minY);
    expect(farRight.target.y).toBeLessThanOrEqual(farRight.bounds.maxY);
    expect(farRight.target.x).toBeGreaterThan(850);

    // Reading mode placement keeps target strictly clear of 500px BookGuide drawer
    const readingDrop = pickSunDropPlacement(
      1440,
      848,
      true,
      true,
      [{ x: 430, y: 200 }],
      3,
    );
    expect(readingDrop.target.x).toBeLessThanOrEqual(842);
    expect(readingDrop.target.x).toBeGreaterThanOrEqual(418);

    // 从舞台上沿之外、落点正上方直落
    expect(readingDrop.start.x).toBe(readingDrop.target.x);
    expect(readingDrop.start.y).toBeLessThan(-38);
  });

  it("builds reminder bubble content cleanly with raw weather/wind, backend reminders, and >=40°C heat alert", () => {
    const full = buildWeatherReminderContent({
      day: 2,
      date: "2026-11-15",
      weather_text: "晴朗少云",
      temp_min_c: 28,
      temp_max_c: 41,
      wind_text: "东南风 2级",
      icon_code: "sunny",
      reminders: ["  带遮阳伞 ", "多喝水"],
    });
    expect(full.headline).toBe("第二天 · 晴朗少云 28–41°C");
    expect(full.windLine).toBe("东南风 2级");
    expect(full.reminders).toEqual([
      "带遮阳伞",
      "多喝水",
      "第2天有 41°C 极端高温，请做好防暑降温",
    ]);

    const minimal = buildWeatherReminderContent({
      day: 1,
      date: "2026-11-14",
      weather_text: "晴",
      temp_min_c: 15,
      temp_max_c: 23,
      wind_text: "",
      icon_code: "sunny",
      reminders: [],
    });
    expect(minimal.headline).toBe("第一天 · 晴 15–23°C");
    expect(minimal.windLine).toBeNull();
    expect(minimal.reminders).toEqual([]);

    const above = computeSunBubblePosition(
      { left: 100, right: 115, top: 640, bottom: 655 },
      { left: 0, top: 52, width: 1440, height: 848 },
    );
    expect(above.placement).toBe("above");

    const below = computeSunBubblePosition(
      { left: 960, right: 975, top: 72, bottom: 87 },
      { left: 0, top: 52, width: 1440, height: 848 },
    );
    expect(below.placement).toBe("below");
  });

  it("mounts weather via official AMap.CustomLayer between polyline and markers, preserves base tint css, and handles all weather kinds cleanly", async () => {
    const multiDay = makeWeatherResult({
      status: "ok",
      days: [
        {
          day: 1,
          date: "2026-11-14",
          weather_text: "晴但写雨测试不猜文案",
          temp_min_c: 14,
          temp_max_c: 24,
          wind_text: "",
          icon_code: "sunny",
          reminders: [],
        },
        {
          day: 2,
          date: "2026-11-15",
          weather_text: "阵雨",
          temp_min_c: 12,
          temp_max_c: 18,
          wind_text: "",
          icon_code: "heavy_rain",
          reminders: [],
        },
        {
          day: 3,
          date: "2026-11-16",
          weather_text: "大雪",
          temp_min_c: -2,
          temp_max_c: 3,
          wind_text: "",
          icon_code: "heavy_snow",
          reminders: [],
        },
        {
          day: 4,
          date: "2026-11-17",
          weather_text: "阴有雾",
          temp_min_c: 9,
          temp_max_c: 14,
          wind_text: "",
          icon_code: "cloudy",
          reminders: [],
        },
      ],
    });
    // Day 5 fog & Day 6 windy -> extend plan
    (multiDay.plans[0].days as any).push(
      { ...multiDay.plans[0].days[0], day: 5, title: "第五天晨雾" },
      { ...multiDay.plans[0].days[0], day: 6, title: "第六天大风" },
    );
    multiDay.weather!.days.push(
      {
        day: 5,
        date: "2026-11-18",
        weather_text: "大雾",
        temp_min_c: 8,
        temp_max_c: 13,
        wind_text: "",
        icon_code: "fog",
        reminders: [],
      },
      {
        day: 6,
        date: "2026-11-19",
        weather_text: "大风",
        temp_min_c: 10,
        temp_max_c: 17,
        wind_text: "北风 5级",
        icon_code: "windy",
        reminders: [],
      },
    );

    useTripStore.getState().setResult("92000", "job_7d", multiDay);
    const { unmount } = renderBookWeatherPage();

    await waitFor(() => {
      expect(weatherMapSpy.markers.length).toBeGreaterThanOrEqual(1);
      expect(
        screen.getByRole("button", { name: /第一天.*收下这颗太阳看天气提醒/ }),
      ).toBeInTheDocument();
    });

    // Day 1 sunny -> no CustomLayer mounted, only sun drop button rendered
    expect(weatherMapSpy.customLayers).toHaveLength(0);

    // Switch Day 2: heavy_rain -> official CustomLayer mounted with 50 < zIndex < 100 & pointerEvents: none
    fireEvent.click(document.querySelector('.bk-dtab[data-day="2"]')!);
    await waitFor(() => {
      expect(weatherMapSpy.customLayers).toHaveLength(1);
    });
    const rainRec = weatherMapSpy.customLayers[0];
    expect(rainRec.attached).toBe(true);
    expect(rainRec.opts.zIndex).toBeGreaterThan(50);
    expect(rainRec.opts.zIndex).toBeLessThan(100);
    expect(rainRec.dom.dataset.wx).toBe("rain");
    expect(rainRec.dom.dataset.heavy).toBe("true");
    expect(rainRec.dom.style.pointerEvents).toBe("none");
    expect(rainRec.dom.querySelector("canvas.bk-wx-canvas")).not.toBeNull();
    // Tab weather badge keeps existing text/icon intact
    expect(
      document.querySelector('.bk-dtab[data-day="2"] .bk-wx-icon'),
    ).not.toBeNull();

    // Switch Day 3: heavy_snow -> rain layer detached, snow CustomLayer attached
    fireEvent.click(document.querySelector('.bk-dtab[data-day="3"]')!);
    await waitFor(() => {
      expect(weatherMapSpy.customLayers).toHaveLength(2);
    });
    expect(rainRec.detachedCount).toBe(1);
    const snowRec = weatherMapSpy.customLayers[1];
    expect(snowRec.dom.dataset.wx).toBe("snow");
    expect(snowRec.dom.dataset.heavy).toBe("true");

    // Switch Day 4: cloudy -> 3 drifting cloud spans
    fireEvent.click(document.querySelector('.bk-dtab[data-day="4"]')!);
    await waitFor(() => {
      expect(weatherMapSpy.customLayers).toHaveLength(3);
    });
    const cloudRec = weatherMapSpy.customLayers[2];
    expect(cloudRec.dom.dataset.wx).toBe("cloud");
    expect(cloudRec.dom.querySelectorAll(".bk-wx-cloud")).toHaveLength(3);

    // Switch Day 5: fog -> 2 band container
    fireEvent.click(document.querySelector('.bk-dtab[data-day="5"]')!);
    await waitFor(() => {
      expect(weatherMapSpy.customLayers).toHaveLength(4);
    });
    expect(weatherMapSpy.customLayers[3].dom.dataset.wx).toBe("fog");
    expect(
      weatherMapSpy.customLayers[3].dom.querySelector(".bk-wx-fog"),
    ).not.toBeNull();

    // Switch Day 6: windy -> nothing drawn on map
    fireEvent.click(document.querySelector('.bk-dtab[data-day="6"]')!);
    await waitFor(() => {
      expect(weatherMapSpy.customLayers[3].detachedCount).toBe(1);
    });
    expect(weatherMapSpy.customLayers).toHaveLength(4);

    // CSS rule preserves base map warm filter & removes filter/opacity on weather layer wrapper
    const css = fs.readFileSync(
      path.resolve(process.cwd(), "src/pages/book/spread-book.css"),
      "utf8",
    );
    expect(css).toContain(
      ".yuntu-spread .bk-atlas .amap-layers > .amap-layer:has(> .bk-wx-layer)",
    );

    unmount();
  });

  it("draws nothing when weather.status !== ok or day missing or map failed", async () => {
    const disabledWeather = makeWeatherResult({ status: "skipped_disabled" });
    useTripStore.getState().setResult("92000", "job_7d", disabledWeather);
    const { unmount } = renderBookWeatherPage();

    await waitFor(() => {
      expect(weatherMapSpy.markers.length).toBeGreaterThanOrEqual(1);
      expect(document.querySelector(".amap-layers")).not.toBeNull();
    });
    expect(weatherMapSpy.customLayers).toHaveLength(0);
    expect(screen.queryByRole("button", { name: /收下这颗太阳/ })).toBeNull();
    unmount();

    // Missing VITE_AMAP_KEY -> fallback paper canvas renders zero weather / sun
    vi.stubEnv("VITE_AMAP_KEY", "");
    useTripStore.getState().setResult("92000", "job_7d", makeWeatherResult());
    renderBookWeatherPage();
    await waitFor(() => {
      expect(screen.getByTestId("bk-map-fallback")).toBeInTheDocument();
    });
    expect(weatherMapSpy.customLayers).toHaveLength(0);
    expect(screen.queryByRole("button", { name: /收下这颗太阳/ })).toBeNull();
  });

  it("syncs weather layer cleanly when BookGuide turns to another day", async () => {
    const sample = makeWeatherResult();
    useTripStore.getState().setResult("92000", "job_7d", sample);
    renderBookWeatherPage();

    await waitFor(() => {
      expect(weatherMapSpy.markers.length).toBeGreaterThanOrEqual(1);
      expect(
        screen.getByRole("button", { name: /第一天.*收下这颗太阳看天气提醒/ }),
      ).toBeInTheDocument();
    });
    expect(weatherMapSpy.customLayers).toHaveLength(0);

    fireEvent.click(screen.getByRole("button", { name: "攻略" }));
    const aside = screen.getByRole("complementary", { name: "全程攻略" });
    fireEvent.click(aside.querySelector('.bk-rtab[data-rday="2"]')!);

    await waitFor(() => {
      expect(weatherMapSpy.customLayers).toHaveLength(1);
    });
    expect(weatherMapSpy.customLayers[0].dom.dataset.wx).toBe("rain");
    expect(weatherMapSpy.customLayers[0].attached).toBe(true);
  });

  it("handles sunny day camera settle, 720ms flight to active tab icon, 7s bubble auto-dismiss, day switch reset, and session collect retention", async () => {
    weatherMapSpy.enableMapEvents = true;
    const sample = makeWeatherResult();
    useTripStore.getState().setResult("92000", "job_7d", sample);

    vi.useFakeTimers({ shouldAdvanceTime: true });
    renderBookWeatherPage();

    // Wait for Day 1 initial synchronous settle -> Day 1 sun button visible
    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: /第一天.*收下这颗太阳看天气提醒/ }),
      ).toBeInTheDocument();
    });

    // 落点一开始就让开右边详情的位置：点开一站的详情，太阳留在原地不挪
    const landedSun = screen.getByRole("button", {
      name: /第一天.*收下这颗太阳看天气提醒/,
    });
    const landedAt = [landedSun.style.left, landedSun.style.top];
    fireEvent.click(document.querySelector(".bk-dc")!);
    await waitFor(() => {
      expect(document.querySelector(".bk-side")).not.toBeNull();
    });
    const sameSun = screen.getByRole("button", {
      name: /第一天.*收下这颗太阳看天气提醒/,
    });
    expect([sameSun.style.left, sameSun.style.top]).toEqual(landedAt);

    // Switch to Day 3 (sunny) without triggering moveend yet: sun MUST wait for settle
    fireEvent.click(document.querySelector('.bk-dtab[data-day="3"]')!);
    expect(screen.queryByRole("button", { name: /收下这颗太阳/ })).toBeNull();

    // Advance 900ms (still below 1600ms settle cap) -> still waiting
    await act(async () => {
      vi.advanceTimersByTime(900);
    });
    expect(screen.queryByRole("button", { name: /收下这颗太阳/ })).toBeNull();

    // Fire map moveend -> Day 3 sun drops cleanly!
    await act(async () => {
      emitWeatherMapEvent("moveend");
    });
    const day3Sun = screen.getByRole("button", {
      name: /第三天.*收下这颗太阳看天气提醒/,
    });
    expect(day3Sun).toBeInTheDocument();

    // Click Day 3 sun -> plays 720ms flight first, then flashes tab icon & opens role="status" bubble with extreme heat
    fireEvent.click(day3Sun);
    expect(day3Sun).toHaveClass("is-collecting");
    expect(screen.queryByRole("status", { name: "当天天气提醒" })).toBeNull();

    await act(async () => {
      vi.advanceTimersByTime(720);
    });

    const day3TabWx = document.querySelector('.bk-dtab[data-day="3"] .bk-wx');
    expect(day3TabWx).toHaveClass("is-got");
    const bubble = screen.getByRole("status", { name: "当天天气提醒" });
    expect(bubble).toHaveTextContent("第三天 · 晴转多云 26–41°C");
    expect(bubble).toHaveTextContent("午间体感偏热");
    expect(bubble).toHaveTextContent("第3天有 41°C 极端高温，请做好防暑降温");

    // After 7000ms auto-dismisses cleanly
    await act(async () => {
      vi.advanceTimersByTime(7000);
    });
    expect(screen.queryByRole("status", { name: "当天天气提醒" })).toBeNull();

    // Switch back to Day 1 (uncollected sunny day!): wait settle -> drops again!
    fireEvent.click(document.querySelector('.bk-dtab[data-day="1"]')!);
    await act(async () => {
      emitWeatherMapEvent("moveend");
    });
    const day1Sun = screen.getByRole("button", {
      name: /第一天.*收下这颗太阳看天气提醒/,
    });
    expect(day1Sun).toBeInTheDocument();

    // Open Day 1 sun and keep bubble open, then switch to Day 3 (collected earlier in session):
    // Day 1 bubble clears cleanly on day switch, and Day 3 does NOT re-drop sun!
    fireEvent.click(day1Sun);
    await act(async () => {
      vi.advanceTimersByTime(720);
    });
    expect(
      screen.getByRole("status", { name: "当天天气提醒" }),
    ).toHaveTextContent("南风 1级");

    fireEvent.click(document.querySelector('.bk-dtab[data-day="3"]')!);
    expect(screen.queryByRole("status", { name: "当天天气提醒" })).toBeNull();
    await act(async () => {
      emitWeatherMapEvent("moveend");
    });
    expect(screen.queryByRole("button", { name: /收下这颗太阳/ })).toBeNull();
  });

  it("targets visible BookGuide .bk-rtab icon in reading mode and flashes .bk-rtab .bk-wx", async () => {
    const sample = makeWeatherResult();
    useTripStore.getState().setResult("92000", "job_7d", sample);

    vi.useFakeTimers({ shouldAdvanceTime: true });
    renderBookWeatherPage();

    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: /第一天.*收下这颗太阳看天气提醒/ }),
      ).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole("button", { name: "攻略" }));
    await act(async () => {
      vi.advanceTimersByTime(1600);
    });
    // Reading mode also nudges obscured pin (panTo) -> waits 650ms nudge settle cap too
    await act(async () => {
      vi.advanceTimersByTime(650);
    });
    const day1Sun = screen.getByRole("button", {
      name: /第一天.*收下这颗太阳看天气提醒/,
    });
    fireEvent.click(day1Sun);
    await act(async () => {
      vi.advanceTimersByTime(720);
    });

    expect(
      document.querySelector('.bk-rtab[data-rday="1"] .bk-wx'),
    ).toHaveClass("is-got");
    expect(
      screen.getByRole("status", { name: "当天天气提醒" }),
    ).toHaveTextContent("第一天 · 晴 14–25°C");
  });

  it("hides sun drop and sun bubble when non-guide reading pages open, restores on close, and keeps showing in guide", async () => {
    const sample = makeWeatherResult();
    useTripStore.getState().setResult("92000", "job_7d", sample);

    vi.useFakeTimers({ shouldAdvanceTime: true });
    renderBookWeatherPage();

    // 晴天且未收集时，太阳正常出现
    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: /第一天.*收下这颗太阳看天气提醒/ }),
      ).toBeInTheDocument();
    });

    // 打开“出发前”（非攻略阅读模式）：长卷收在屏外，太阳不出现（等地图 settle 后仍不出现）
    const beforeBtn = screen.getByRole("button", { name: "出发前" });
    fireEvent.click(beforeBtn);
    expect(screen.queryByRole("button", { name: /收下这颗太阳/ })).toBeNull();
    await act(async () => {
      vi.advanceTimersByTime(1600);
    });
    expect(screen.queryByRole("button", { name: /收下这颗太阳/ })).toBeNull();

    // 关掉“出发前”：地图落定后太阳回来
    fireEvent.click(beforeBtn);
    await act(async () => {
      vi.advanceTimersByTime(1600);
    });
    expect(
      screen.getByRole("button", { name: /第一天.*收下这颗太阳看天气提醒/ }),
    ).toBeInTheDocument();

    // 打开“攻略”：太阳照常出现
    fireEvent.click(screen.getByRole("button", { name: "攻略" }));
    await act(async () => {
      vi.advanceTimersByTime(1600);
    });
    await act(async () => {
      vi.advanceTimersByTime(650);
    });
    expect(
      screen.getByRole("button", { name: /第一天.*收下这颗太阳看天气提醒/ }),
    ).toBeInTheDocument();
  });

  it("pauses RAF when document is hidden, cleans up on unmount, honors prefers-reduced-motion, and tolerates null 2d context", async () => {
    const strokeSpy = vi.fn();
    const fakeCtx = {
      setTransform: vi.fn(),
      clearRect: vi.fn(),
      beginPath: vi.fn(),
      moveTo: vi.fn(),
      lineTo: vi.fn(),
      arc: vi.fn(),
      stroke: strokeSpy,
      fill: vi.fn(),
      strokeStyle: "",
      fillStyle: "",
      lineWidth: 1,
      lineCap: "round",
    } as unknown as CanvasRenderingContext2D;

    const origGetContext = HTMLCanvasElement.prototype.getContext;
    HTMLCanvasElement.prototype.getContext = vi.fn(() => fakeCtx) as any;

    let nextRafId = 1;
    const activeRafs = new Set<number>();
    const rafSpy = vi
      .spyOn(window, "requestAnimationFrame")
      .mockImplementation(() => {
        const id = nextRafId++;
        activeRafs.add(id);
        return id;
      });
    const cancelSpy = vi
      .spyOn(window, "cancelAnimationFrame")
      .mockImplementation((id: number) => {
        activeRafs.delete(id);
      });

    const sample = makeWeatherResult();
    useTripStore.getState().setResult("92000", "job_7d", sample);
    const { unmount } = renderBookWeatherPage();

    await waitFor(() => {
      expect(weatherMapSpy.markers.length).toBeGreaterThanOrEqual(1);
      expect(document.querySelector(".amap-layers")).not.toBeNull();
    });

    // Switch to Day 2 rain -> synchronous draw + RAF started
    fireEvent.click(document.querySelector('.bk-dtab[data-day="2"]')!);
    await waitFor(() => {
      expect(weatherMapSpy.customLayers).toHaveLength(1);
    });
    expect(strokeSpy).toHaveBeenCalledTimes(1);
    expect(rafSpy).toHaveBeenCalledTimes(1);
    expect(activeRafs.size).toBe(1);

    // Tab hidden -> cancels active RAF cleanly
    Object.defineProperty(document, "hidden", {
      configurable: true,
      get: () => true,
    });
    fireEvent(document, new Event("visibilitychange"));
    expect(cancelSpy).toHaveBeenCalledTimes(1);
    expect(activeRafs.size).toBe(0);

    // Tab visible -> resumes RAF
    Object.defineProperty(document, "hidden", {
      configurable: true,
      get: () => false,
    });
    fireEvent(document, new Event("visibilitychange"));
    expect(rafSpy).toHaveBeenCalledTimes(2);
    expect(activeRafs.size).toBe(1);

    // Unmount cleans up active RAF and layer
    unmount();
    expect(activeRafs.size).toBe(0);
    expect(weatherMapSpy.customLayers[0].detachedCount).toBe(1);

    // Now test prefers-reduced-motion: reduce (static frame only, zero RAF, instant sun collect)
    mockMatchMedia(true);
    rafSpy.mockClear();
    strokeSpy.mockClear();
    const { unmount: unmountReduced } = renderBookWeatherPage();

    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: /第一天.*收下这颗太阳看天气提醒/ }),
      ).toBeInTheDocument();
    });

    // Click sun -> immediate reminder card with 0ms wait and no flight
    fireEvent.click(
      screen.getByRole("button", { name: /第一天.*收下这颗太阳看天气提醒/ }),
    );
    expect(
      screen.getByRole("status", { name: "当天天气提醒" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /收下这颗太阳/ })).toBeNull();

    // Switch to Day 2 rain -> static single frame drawn, 0 RAF scheduled
    fireEvent.click(document.querySelector('.bk-dtab[data-day="2"]')!);
    await waitFor(() => {
      expect(weatherMapSpy.customLayers.length).toBeGreaterThanOrEqual(2);
    });
    expect(strokeSpy).toHaveBeenCalledTimes(1);
    expect(rafSpy).not.toHaveBeenCalled();
    unmountReduced();

    // Finally verify null 2d context (default jsdom behavior) never throws
    HTMLCanvasElement.prototype.getContext = origGetContext;
    mockMatchMedia(false);
    renderBookWeatherPage();
    await waitFor(() => {
      expect(weatherMapSpy.markers.length).toBeGreaterThanOrEqual(1);
      expect(
        screen.getByRole("button", { name: /第一天.*收下这颗太阳看天气提醒/ }),
      ).toBeInTheDocument();
    });
    fireEvent.click(document.querySelector('.bk-dtab[data-day="2"]')!);
    await waitFor(() => {
      expect(
        weatherMapSpy.customLayers[weatherMapSpy.customLayers.length - 1]?.dom
          .dataset.wx,
      ).toBe("rain");
    });
  });
});
