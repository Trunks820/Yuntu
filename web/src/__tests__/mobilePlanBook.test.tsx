/**
 * 2026-11 第 3 步：手机端攻略页测试
 *
 * 按功能拆分为独立 it：
 * - 左右滑换天纯判定函数；
 * - 去掉城市参考图、天气行（有/无/带提醒）、当天怎么走；
 * - 各站小实拍（有图才出、失败/不支持/无图不报错、同站不重取）；
 * - 滑动页签指示块、横滑换天与边缘/竖滑防误触、底栏下滑收起与恢复；
 * - 第 5 步：地点详情弹层（中文类型、内容、关闭方式、下拉关闭、展开动画）。
 *
 * 全部使用假数据，不含任何真实账号或线上数据。
 */
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { MobilePlanView } from "@/pages/mobile/MobilePlanView";
import * as api from "@/services/api";
import {
  computeSwipeFollow,
  decideSwipeAxis,
  isSwipeStartInEdgeZone,
  resolveDaySwipe,
  shouldCloseSheetOnRelease,
} from "@/utils/mobileSwipe";
import { categoryName } from "@/constants/places";
import type {
  PlaceDetail,
  TripPlan,
  TripResult,
  WeatherDay,
} from "@/types/trip";

vi.mock("@/services/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/services/api")>();
  return {
    ...actual,
    fetchPlaceDetail: vi.fn(),
  };
});

function createTestPlan(overrides: Partial<TripPlan> = {}): TripPlan {
  return {
    plan_id: "plan_mobile_step3",
    title: "重庆3日｜山城步道与江岸漫游",
    summary: "把老城阶梯和江风放进三天里。",
    tags: ["漫步", "美食"],
    pace: {
      level: "MODERATE",
      commute_status: "WITHIN_LIMIT",
      total_commute_minutes: 45,
    },
    accommodation: {
      name: "解放碑片区",
      latitude: 29.557,
      longitude: 106.577,
      source: "auto_recommended",
      reason: "离三天景点都近",
      display: {
        layout: "tiers",
        area_label: "解放碑一带",
        caption: "",
        groups: [
          {
            kind: "economy",
            label: "经济",
            hotels: ["示例酒店A", "示例酒店B"],
            nightly_low_cny: 120,
            nightly_high_cny: 180,
          },
          {
            kind: "mid",
            label: "中档",
            hotels: ["示例酒店C"],
            nightly_low_cny: 320,
            nightly_high_cny: 480,
          },
        ],
        round_trip_minutes_low: null,
        round_trip_minutes_high: null,
        notices: [],
      },
    },
    transport: null,
    days: [
      {
        day: 1,
        title: "嘉陵江岸与老街",
        narrative: "第一天沿着江边慢慢走。",
        pace_status: "WITHIN_LIMIT",
        commute_summary:
          "上午地铁 2 号线直达大礼堂；下午步行串联十八梯与山城步道；晚上江岸回解放碑约 15 分钟",
        places: [
          {
            place_id: 1001,
            name: "三峡博物馆",
            category: "museum",
            brief: "了解山城历史",
            activity_note: "留出两小时看巴渝历史展厅。",
            stay_minutes: 120,
            role: "major_attraction",
          },
          {
            place_id: 1002,
            name: "山城老茶馆",
            category: "restaurant",
            brief: "歇脚喝茶",
            activity_note: "点一杯沱茶歇脚。",
            stay_minutes: 45,
            role: "dining",
          },
          {
            place_id: 1003,
            name: "旧城巷口面馆",
            category: "restaurant",
            brief: "本地小面",
            activity_note: "错峰吃一碗豌杂面。",
            stay_minutes: 40,
            role: "dining",
          },
          {
            place_id: 1004,
            name: "江边观景台",
            category: "photo_spot",
            brief: "看日落",
            activity_note: "傍晚看两岸亮灯。",
            stay_minutes: 50,
            role: "photo",
          },
        ],
        commute_legs: [],
      },
      {
        day: 2,
        title: "南岸索道与老厂区",
        narrative: "第二天过江看老厂房。",
        pace_status: "WITHIN_LIMIT",
        commute_summary: "",
        places: [
          {
            place_id: 2001,
            name: "长江索道",
            category: "attraction",
            brief: "跨江索道",
            activity_note: "早点去减少排队。",
            stay_minutes: 45,
            role: "major_attraction",
          },
        ],
        commute_legs: [],
      },
      {
        day: 3,
        title: "江岸慢收尾",
        narrative: "最后一天轻松收尾。",
        pace_status: "WITHIN_LIMIT",
        commute_summary: "打车往返约 20 分钟",
        places: [
          {
            place_id: 3001,
            name: "南滨路步道",
            category: "park",
            brief: "临江散步",
            activity_note: "慢慢散步看江景。",
            stay_minutes: 60,
            role: "major_attraction",
          },
        ],
        commute_legs: [],
      },
    ],
    cost_estimate: {
      snapshot_version: "1",
      completeness: "complete",
      currency: "CNY",
      estimated_at: "2026-11-20T00:00:00Z",
      scenarios: [],
      assumptions: [],
      exclusions: [],
      notice: "",
    },
    packing_checklist: null,
    travel_tips: null,
    ...overrides,
  };
}

function createTestResult(plan: TripPlan): TripResult {
  return {
    schema_version: "2.3",
    result_id: 91500,
    city: { name: "重庆" },
    request: {
      days: plan.days.length,
      people_count: 2,
      preferences: ["漫步", "美食"],
      avoid: [],
    },
    plans: [plan],
  };
}

function createPlaceDetail(placeId: number, thumbUrl?: string): PlaceDetail {
  return {
    place_id: placeId,
    name: `地点 ${placeId}`,
    category: "attraction",
    address: "重庆渝中区示例路 1 号",
    latitude: 29.56,
    longitude: 106.57,
    summary: "示例地点摘要",
    top_reasons: ["示例理由"],
    gallery: thumbUrl
      ? [
          {
            asset_id: `asset-${placeId}-2`,
            position: 2,
            thumb: { url: `${thumbUrl}?pos=2`, width: 320, height: 320 },
            mobile: { url: `${thumbUrl}?pos=2m`, width: 960, height: 600 },
            desktop: { url: `${thumbUrl}?pos=2d`, width: 1280, height: 800 },
          },
          {
            asset_id: `asset-${placeId}-1`,
            position: 1,
            thumb: { url: thumbUrl, width: 320, height: 320 },
            mobile: { url: `${thumbUrl}?pos=1m`, width: 960, height: 600 },
            desktop: { url: `${thumbUrl}?pos=1d`, width: 1280, height: 800 },
          },
        ]
      : [],
  };
}

const SAMPLE_WEATHER: WeatherDay[] = [
  {
    day: 1,
    date: "2026-11-22",
    weather_text: "晴",
    wind_text: "南风 1级",
    icon_code: "sunny",
    temp_min_c: 10,
    temp_max_c: 18,
    reminders: [],
  },
  {
    day: 2,
    date: "2026-11-23",
    weather_text: "阴转小雨",
    wind_text: "东风 1级",
    icon_code: "light_rain",
    temp_min_c: 14,
    temp_max_c: 17,
    reminders: ["户外步行段注意带伞、防滑。"],
  },
];

function renderMobilePlan(options?: {
  plan?: TripPlan;
  weatherDays?: WeatherDay[];
  cityCovers?: string[];
}) {
  const plan = options?.plan ?? createTestPlan();
  const result = createTestResult(plan);
  return render(
    <MemoryRouter>
      <MobilePlanView
        result={result}
        plan={plan}
        jobId="job_mobile_step3"
        cityCovers={
          options?.cityCovers ?? ["https://cdn.example.test/hero.webp"]
        }
        weatherDays={options?.weatherDays}
        timePrefText={null}
        packing={[]}
        tips={[]}
        transportSlot={null}
        costSlot={null}
        pdfLoading={false}
        onPdf={vi.fn()}
        onShare={vi.fn()}
        onBack={vi.fn()}
      />
    </MemoryRouter>,
  );
}

describe("手机端攻略页：左右滑换天纯函数", () => {
  it("屏幕左右 24px 边缘内不接管横滑，中间区域放行", () => {
    expect(isSwipeStartInEdgeZone(0, 390)).toBe(true);
    expect(isSwipeStartInEdgeZone(23, 390)).toBe(true);
    expect(isSwipeStartInEdgeZone(24, 390)).toBe(false);
    expect(isSwipeStartInEdgeZone(200, 390)).toBe(false);
    expect(isSwipeStartInEdgeZone(366, 390)).toBe(false);
    expect(isSwipeStartInEdgeZone(367, 390)).toBe(true);
    expect(isSwipeStartInEdgeZone(390, 390)).toBe(true);
  });

  it("移动不超过 10px 时维持 pending，横向位移大于纵向 1.3 倍才判 horizontal", () => {
    expect(decideSwipeAxis(6, 6)).toBe("pending");
    expect(decideSwipeAxis(10, 0)).toBe("pending");
    expect(decideSwipeAxis(14, 10)).toBe("horizontal");
    expect(decideSwipeAxis(-20, 12)).toBe("horizontal");
    expect(decideSwipeAxis(12, 10)).toBe("vertical");
    expect(decideSwipeAxis(4, 24)).toBe("vertical");
  });

  it("跟手位移正常按 0.55 倍，首日右拉和末日左拉按 0.18 倍衰减", () => {
    const midLeft = computeSwipeFollow(-100, 1, 3);
    expect(midLeft.atBoundary).toBe(false);
    expect(midLeft.offsetX).toBeCloseTo(-55, 5);
    expect(midLeft.opacity).toBeLessThan(1);

    const firstRight = computeSwipeFollow(100, 0, 3);
    expect(firstRight.atBoundary).toBe(true);
    expect(firstRight.offsetX).toBeCloseTo(18, 5);

    const lastLeft = computeSwipeFollow(-100, 2, 3);
    expect(lastLeft.atBoundary).toBe(true);
    expect(lastLeft.offsetX).toBeCloseTo(-18, 5);
  });

  it("松手横移达到 60px 时切到前后一天，不足 60px 或边界外不切", () => {
    expect(resolveDaySwipe(-60, 0, 3)).toEqual({
      shouldSwitch: true,
      targetIndex: 1,
      direction: 1,
    });
    expect(resolveDaySwipe(60, 2, 3)).toEqual({
      shouldSwitch: true,
      targetIndex: 1,
      direction: -1,
    });
    expect(resolveDaySwipe(-59, 0, 3)).toEqual({
      shouldSwitch: false,
      targetIndex: 0,
      direction: 0,
    });
    expect(resolveDaySwipe(80, 0, 3)).toEqual({
      shouldSwitch: false,
      targetIndex: 0,
      direction: 0,
    });
    expect(resolveDaySwipe(-80, 2, 3)).toEqual({
      shouldSwitch: false,
      targetIndex: 2,
      direction: 0,
    });
  });
});

describe("手机端攻略页：封面、天气行与当天怎么走", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.fetchPlaceDetail).mockImplementation(
      () => new Promise(() => {}),
    );
  });

  it("不再渲染每天开头的城市参考图，保留顶部旅行册封面", () => {
    const { container } = renderMobilePlan();

    expect(container.querySelector(".day-cover")).toBeNull();
    expect(screen.queryByText(/城市参考图/)).toBeNull();
    expect(screen.getByAltText("重庆 · 旅行册封面")).toBeInTheDocument();
  });

  it("当天有天气时在标题下一行显示图标与文案，标题右侧只留 CHAPTER 0N", () => {
    const { container } = renderMobilePlan({ weatherDays: SAMPLE_WEATHER });

    const heading = container.querySelector(".day-heading");
    expect(heading?.querySelector("small")?.textContent).toBe("CHAPTER 01");

    const wx = container.querySelector(".day-wx");
    expect(wx).not.toBeNull();
    expect(wx?.classList.contains("wx-sun")).toBe(true);
    expect(wx?.querySelector("svg .wx-rays")).not.toBeNull();
    expect(wx?.textContent).toBe("晴 10–18°C · 南风 1级");
    expect(container.querySelector(".day-wx-tip")).toBeNull();
  });

  it("当天天气带 reminders 时在天气行下一行原样展示提醒", () => {
    const { container } = renderMobilePlan({ weatherDays: SAMPLE_WEATHER });

    fireEvent.click(screen.getByRole("button", { name: "DAY02" }));

    const wx = container.querySelector(".day-wx");
    expect(wx?.classList.contains("wx-rain")).toBe(true);
    expect(wx?.querySelectorAll("svg .wx-drop")).toHaveLength(3);
    expect(wx?.textContent).toBe("阴转小雨 14–17°C · 东风 1级");

    const tip = container.querySelector(".day-wx-tip");
    expect(tip?.textContent).toBe("户外步行段注意带伞、防滑。");
  });

  it("当天没有天气时整行不出，也不显示提醒", () => {
    const { container } = renderMobilePlan({ weatherDays: SAMPLE_WEATHER });

    fireEvent.click(screen.getByRole("button", { name: "DAY03" }));

    expect(container.querySelector(".day-wx")).toBeNull();
    expect(container.querySelector(".day-wx-tip")).toBeNull();
    expect(container.querySelector(".day-heading small")?.textContent).toBe(
      "CHAPTER 03",
    );
  });

  function renderWithIconCode(iconCode: string) {
    return renderMobilePlan({
      weatherDays: [
        { ...SAMPLE_WEATHER[0], weather_text: "天气", icon_code: iconCode },
      ],
    });
  }

  it.each(["unknown", "", "windy", "light_snow", "fog"])(
    "icon_code 为 %j 时天气行照常显示，但用静态图标、不带动效",
    (iconCode) => {
      const { container } = renderWithIconCode(iconCode);

      const wx = container.querySelector(".day-wx");
      expect(wx).not.toBeNull();
      expect(wx?.classList.contains("wx-other")).toBe(true);
      expect(wx?.textContent).toBe("天气 10–18°C · 南风 1级");
      expect(wx?.querySelector("svg")).not.toBeNull();
      expect(wx?.querySelector(".wx-rays, .wx-cloud, .wx-drop")).toBeNull();
    },
  );

  it.each([
    ["sunny", ".wx-rays"],
    ["partly_cloudy", ".wx-cloud"],
    ["light_rain", ".wx-drop"],
  ])("icon_code 为 %s 时显示对应的动效元素 %s", (iconCode, selector) => {
    const { container } = renderWithIconCode(iconCode);

    expect(container.querySelector(`.day-wx ${selector}`)).not.toBeNull();
  });

  it("有 commute_summary 时收进“当天怎么走”并按分号拆成列表行，不改字", () => {
    const { container } = renderMobilePlan();

    const details = container.querySelector("details.day-route");
    expect(details).not.toBeNull();
    expect(details?.querySelector("summary")?.textContent).toBe("当天怎么走");
    const items = Array.from(details?.querySelectorAll("li") ?? []).map(
      (li) => li.textContent,
    );
    expect(items).toEqual([
      "上午地铁 2 号线直达大礼堂",
      "下午步行串联十八梯与山城步道",
      "晚上江岸回解放碑约 15 分钟",
    ]);
  });

  it("当天没有 commute_summary 时不渲染“当天怎么走”", () => {
    const { container } = renderMobilePlan();

    fireEvent.click(screen.getByRole("button", { name: "DAY02" }));
    expect(container.querySelector("details.day-route")).toBeNull();
  });
});

describe("手机端攻略页：各站小实拍", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("只有返回 gallery 首张 thumb 的站点才渲染 68px 小图，并在加载完成后淡入", async () => {
    vi.mocked(api.fetchPlaceDetail).mockImplementation((placeId) => {
      if (placeId === 1001) {
        return Promise.resolve(
          createPlaceDetail(1001, "https://cdn.example.test/museum-thumb.webp"),
        );
      }
      return Promise.resolve(createPlaceDetail(placeId));
    });

    const { container } = renderMobilePlan();

    await waitFor(() => {
      expect(container.querySelectorAll("img.stop-ph")).toHaveLength(1);
    });

    const photo = container.querySelector<HTMLImageElement>("img.stop-ph");
    expect(photo?.getAttribute("src")).toBe(
      "https://cdn.example.test/museum-thumb.webp",
    );
    expect(photo?.classList.contains("is-fading")).toBe(true);
    expect(photo?.classList.contains("is-loaded")).toBe(false);

    fireEvent.load(photo!);
    expect(photo?.classList.contains("is-loaded")).toBe(true);
  });

  it("无图、unsupported、请求报错或图片加载失败时不放图也不显示报错", async () => {
    vi.mocked(api.fetchPlaceDetail).mockImplementation((placeId) => {
      if (placeId === 1001) {
        return Promise.resolve(
          createPlaceDetail(1001, "https://cdn.example.test/broken-thumb.webp"),
        );
      }
      if (placeId === 1002) {
        return Promise.resolve(createPlaceDetail(1002));
      }
      if (placeId === 1003) {
        const err = new api.ApiRequestError("暂不支持该地点详情");
        err.code = "PLACE_UNSUPPORTED";
        return Promise.reject(err);
      }
      return Promise.reject(new Error("网络异常"));
    });

    const { container } = renderMobilePlan();

    await waitFor(() => {
      expect(container.querySelectorAll("img.stop-ph")).toHaveLength(1);
    });

    const brokenPhoto =
      container.querySelector<HTMLImageElement>("img.stop-ph");
    fireEvent.error(brokenPhoto!);

    expect(container.querySelectorAll("img.stop-ph")).toHaveLength(0);
    expect(container.textContent).not.toContain("暂不支持该地点详情");
    expect(container.textContent).not.toContain("网络异常");
  });

  it("切到其他天再切回第一天时，同一站点只请求一次详情", async () => {
    vi.mocked(api.fetchPlaceDetail).mockImplementation((placeId) =>
      Promise.resolve(
        createPlaceDetail(
          placeId,
          placeId === 1001
            ? "https://cdn.example.test/museum-thumb.webp"
            : undefined,
        ),
      ),
    );

    renderMobilePlan();

    await waitFor(() => {
      expect(api.fetchPlaceDetail).toHaveBeenCalledWith(1001);
    });
    const firstDayCalls = vi
      .mocked(api.fetchPlaceDetail)
      .mock.calls.filter(([id]) => id === 1001).length;
    expect(firstDayCalls).toBe(1);

    fireEvent.click(screen.getByRole("button", { name: "DAY02" }));
    await waitFor(() => {
      expect(api.fetchPlaceDetail).toHaveBeenCalledWith(2001);
    });

    fireEvent.click(screen.getByRole("button", { name: "DAY01" }));
    const afterReturnCalls = vi
      .mocked(api.fetchPlaceDetail)
      .mock.calls.filter(([id]) => id === 1001).length;
    expect(afterReturnCalls).toBe(1);
  });
});

describe("手机端攻略页：滑动页签、横滑换天与底栏自动收起", () => {
  const originalInnerWidth = window.innerWidth;
  const originalMatchMedia = window.matchMedia;

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.fetchPlaceDetail).mockImplementation(
      () => new Promise(() => {}),
    );
    Object.defineProperty(window, "innerWidth", {
      writable: true,
      configurable: true,
      value: 390,
    });
  });

  afterEach(() => {
    vi.useRealTimers();
    Object.defineProperty(window, "innerWidth", {
      writable: true,
      configurable: true,
      value: originalInnerWidth,
    });
    window.matchMedia = originalMatchMedia;
  });

  it("DAY 页签内含滑动指示块，点击后切换选中态", () => {
    const { container } = renderMobilePlan();

    expect(container.querySelector(".day-tabs .day-ind")).not.toBeNull();
    const day2Btn = screen.getByRole("button", { name: "DAY02" });
    fireEvent.click(day2Btn);

    expect(day2Btn).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByText("南岸索道与老厂区")).toBeInTheDocument();
  });

  it("在内容区左滑超过 60px 切到下一天，竖滑或屏幕 24px 边缘起滑不误切", () => {
    const { container } = renderMobilePlan();
    const pane = container.querySelector(".day-pane")!;

    // 边缘 24px 内起滑：忽略
    fireEvent.pointerDown(pane, { pointerId: 1, clientX: 12, clientY: 200 });
    fireEvent.pointerMove(pane, { pointerId: 1, clientX: 140, clientY: 200 });
    fireEvent.pointerUp(pane, { pointerId: 1, clientX: 140, clientY: 200 });
    expect(screen.getByText("嘉陵江岸与老街")).toBeInTheDocument();

    // 竖向滑动：不误切
    fireEvent.pointerDown(pane, { pointerId: 2, clientX: 200, clientY: 200 });
    fireEvent.pointerMove(pane, { pointerId: 2, clientX: 185, clientY: 260 });
    fireEvent.pointerUp(pane, { pointerId: 2, clientX: 110, clientY: 320 });
    expect(screen.getByText("嘉陵江岸与老街")).toBeInTheDocument();

    // 明显左滑超过 60px：切到 DAY02
    fireEvent.pointerDown(pane, { pointerId: 3, clientX: 280, clientY: 220 });
    fireEvent.pointerMove(pane, { pointerId: 3, clientX: 240, clientY: 222 });
    fireEvent.pointerMove(pane, { pointerId: 3, clientX: 190, clientY: 224 });
    fireEvent.pointerUp(pane, { pointerId: 3, clientX: 190, clientY: 224 });
    expect(screen.getByText("南岸索道与老厂区")).toBeInTheDocument();
  });

  it("往下滚动超过阈值时底栏收起，上滑、停住 0.9s 或到底时恢复", () => {
    vi.useFakeTimers();
    const { container } = renderMobilePlan();
    const scroller = container.querySelector<HTMLDivElement>(".scroll")!;
    const footer = container.querySelector<HTMLDivElement>(".footer-action")!;

    Object.defineProperty(scroller, "clientHeight", {
      configurable: true,
      value: 700,
    });
    Object.defineProperty(scroller, "scrollHeight", {
      configurable: true,
      value: 2000,
    });
    Object.defineProperty(scroller, "scrollTop", {
      configurable: true,
      writable: true,
      value: 0,
    });

    // 下滑超过 6px 且 scrollTop > 160、未到底 → 收起
    scroller.scrollTop = 220;
    fireEvent.scroll(scroller);
    expect(footer.classList.contains("is-away")).toBe(true);

    // 停住 0.9s → 恢复
    act(() => {
      vi.advanceTimersByTime(900);
    });
    expect(footer.classList.contains("is-away")).toBe(false);

    // 再次下滑收起 → 上滑超过 6px 立即恢复
    scroller.scrollTop = 320;
    fireEvent.scroll(scroller);
    expect(footer.classList.contains("is-away")).toBe(true);

    scroller.scrollTop = 300;
    fireEvent.scroll(scroller);
    expect(footer.classList.contains("is-away")).toBe(false);

    // 再次下滑收起 → 滚到底部 40px 内恢复
    scroller.scrollTop = 420;
    fireEvent.scroll(scroller);
    expect(footer.classList.contains("is-away")).toBe(true);

    scroller.scrollTop = 1270;
    fireEvent.scroll(scroller);
    expect(footer.classList.contains("is-away")).toBe(false);
  });

  it("减少动态偏好下换天不触发平移动画，页签指示块关闭过渡", () => {
    window.matchMedia = vi.fn().mockImplementation((query: string) => ({
      matches: query === "(prefers-reduced-motion: reduce)",
      media: query,
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    }));

    const { container } = renderMobilePlan();
    const pane = container.querySelector<HTMLDivElement>(".day-pane")!;
    const animateSpy = vi.fn();
    pane.animate = animateSpy;

    fireEvent.click(screen.getByRole("button", { name: "DAY02" }));

    expect(animateSpy).not.toHaveBeenCalled();
    const ind = container.querySelector<HTMLElement>(".day-tabs .day-ind")!;
    expect(ind.style.transition).toBe("none");
  });
});

describe("手机端攻略页：地点详情弹层", () => {
  const notReduced = (query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  });

  beforeEach(() => {
    vi.clearAllMocks();
    window.matchMedia = vi.fn().mockImplementation(notReduced);
    vi.mocked(api.fetchPlaceDetail).mockImplementation((id: number) =>
      Promise.resolve({
        ...createPlaceDetail(id, "https://cdn.example.test/stop.webp"),
        top_reasons: ["作为第一站被推荐"],
        warnings: ["周一闭馆"],
        source_count: 3,
        mention_count: 5,
      } as PlaceDetail),
    );
  });

  function planWithRouteAndCoords(): TripPlan {
    const plan = createTestPlan();
    const [first, ...rest] = plan.days;
    return {
      ...plan,
      days: [
        {
          ...first,
          places: first.places.map((place, idx) => ({
            ...place,
            longitude: 106.55 + idx * 0.01,
            latitude: 29.56,
            role: idx === 0 ? "anchor_activity" : place.role,
          })),
          commute_legs: [
            {
              from_place_id: 1001,
              to_place_id: 1002,
              mode: "walking",
              duration_minutes: 10,
              distance_meters: 800,
            },
          ],
        },
        ...rest,
      ],
    };
  }

  function renderWithSheetData(mustIncludePlaceId?: number) {
    const plan = planWithRouteAndCoords();
    const result: TripResult = {
      ...createTestResult(plan),
      must_include: mustIncludePlaceId
        ? [
            {
              name: "三峡博物馆",
              place_id: mustIncludePlaceId,
              status: "scheduled",
            } as never,
          ]
        : null,
    };
    return render(
      <MemoryRouter>
        <MobilePlanView
          result={result}
          plan={plan}
          jobId="job_mobile_step5"
          cityCovers={["https://cdn.example.test/hero.webp"]}
          timePrefText={null}
          packing={[]}
          tips={[]}
          transportSlot={null}
          costSlot={null}
          pdfLoading={false}
          onPdf={vi.fn()}
          onShare={vi.fn()}
          onBack={vi.fn()}
        />
      </MemoryRouter>,
    );
  }

  async function openStop(name: string) {
    fireEvent.click(screen.getByRole("button", { name }));
    return screen.findByRole("dialog", { name });
  }

  it("点地点名打开详情：类型用列表同款中文名，不再出现英文类型码或图钉表情", async () => {
    renderWithSheetData();
    const dialog = await openStop("三峡博物馆");

    expect(dialog.querySelector(".place-kind")?.textContent).toBe(
      categoryName("museum"),
    );
    expect(dialog.textContent).not.toMatch(/museum|MUSEUM|📍/);
    expect(dialog.querySelector(".place-k")?.textContent).toContain(
      "建议停留 2 小时",
    );
    expect(
      within(dialog).getByText("留出两小时看巴渝历史展厅。"),
    ).toBeInTheDocument();
  });

  it("详情到了以后显示推荐理由、注意事项、来源与提及次数", async () => {
    renderWithSheetData();
    const dialog = await openStop("三峡博物馆");

    expect(
      await within(dialog).findByText("作为第一站被推荐"),
    ).toBeInTheDocument();
    expect(within(dialog).getByText("周一闭馆")).toBeInTheDocument();
    expect(
      within(dialog).getByText("综合 3 个来源 · 5 次提及"),
    ).toBeInTheDocument();
  });

  it("有实拍时按位置排序放图，带多尺寸 srcset，多张时可以横向翻看", async () => {
    renderWithSheetData();
    const dialog = await openStop("三峡博物馆");

    await waitFor(() => {
      expect(dialog.querySelectorAll(".place-gallery img")).toHaveLength(2);
    });
    const first = dialog.querySelector<HTMLImageElement>(".place-gallery img")!;
    expect(first.getAttribute("src")).toBe(
      "https://cdn.example.test/stop.webp?pos=1m",
    );
    expect(first.getAttribute("srcset")).toContain("320w");
    expect(first.getAttribute("srcset")).toContain("1280w");
    expect(
      dialog.querySelector(".place-gallery")?.classList.contains("is-multi"),
    ).toBe(true);
  });

  it("后端不支持该地点详情时不放图、也不留加载占位", async () => {
    vi.mocked(api.fetchPlaceDetail).mockResolvedValue(null as never);
    renderWithSheetData();
    const dialog = await openStop("三峡博物馆");

    await waitFor(() => {
      expect(dialog.querySelector(".place-photo-wait")).toBeNull();
    });
    expect(dialog.querySelector(".place-gallery img")).toBeNull();
  });

  it("显示到下一站和通勤方式；当天最后一站不显示", async () => {
    renderWithSheetData();
    const dialog = await openStop("三峡博物馆");

    const next = dialog.querySelector(".place-next");
    expect(next?.textContent).toContain("到下一站 山城老茶馆");
    expect(next?.textContent).toContain("步行 · 约 10 分钟 · 800 米");

    fireEvent.click(within(dialog).getByRole("button", { name: "关闭" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    const last = await openStop("江边观景台");
    expect(last.querySelector(".place-next")).toBeNull();
  });

  it("必去的站显示“你的必去”，主锚点显示“核心景点”", async () => {
    renderWithSheetData(1001);
    const dialog = await openStop("三峡博物馆");

    expect(within(dialog).getByText("你的必去")).toBeInTheDocument();
    expect(within(dialog).getByText("核心景点")).toBeInTheDocument();
  });

  it("有坐标时底部给高德导航链接", async () => {
    renderWithSheetData();
    const dialog = await openStop("三峡博物馆");

    const link = within(dialog).getByRole("link", {
      name: "在高德地图中查看路线",
    });
    expect(link.getAttribute("href")).toBe(
      `https://uri.amap.com/marker?position=106.55,29.56&name=${encodeURIComponent("三峡博物馆")}`,
    );
    expect(link.getAttribute("target")).toBe("_blank");
  });

  it("关闭按钮、Esc、点遮罩都能关上，关上后焦点回到那一站的地点名", async () => {
    renderWithSheetData();

    let dialog = await openStop("三峡博物馆");
    fireEvent.click(within(dialog).getByRole("button", { name: "关闭" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());

    await openStop("三峡博物馆");
    fireEvent.keyDown(document, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());

    const titleButton = screen.getByRole("button", { name: "三峡博物馆" });
    titleButton.focus();
    dialog = await openStop("三峡博物馆");
    expect(document.activeElement).toBe(dialog);
    fireEvent.click(document.querySelector(".place-scrim")!);
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(document.activeElement).toBe(titleButton);
  });

  it("按住顶部横杠轻拖会弹回，拖远就关上", async () => {
    renderWithSheetData();
    const dialog = await openStop("三峡博物馆");
    const grab = dialog.querySelector<HTMLElement>(".place-grab")!;

    fireEvent.pointerDown(grab, { pointerId: 1, clientY: 100 });
    fireEvent.pointerMove(grab, { pointerId: 1, clientY: 120 });
    fireEvent.pointerUp(grab, { pointerId: 1, clientY: 120 });
    expect(screen.getByRole("dialog")).toBe(dialog);
    expect(dialog.style.transform).toBe("");

    fireEvent.pointerDown(grab, { pointerId: 2, clientY: 100 });
    fireEvent.pointerMove(grab, { pointerId: 2, clientY: 500 });
    fireEvent.pointerUp(grab, { pointerId: 2, clientY: 500 });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  it("下拉关闭的判定：过三分之一高度，或甩得快且超过 30px", () => {
    expect(shouldCloseSheetOnRelease(0, 100, 600)).toBe(false);
    expect(shouldCloseSheetOnRelease(150, 1000, 600)).toBe(false);
    expect(shouldCloseSheetOnRelease(201, 1000, 600)).toBe(true);
    expect(shouldCloseSheetOnRelease(40, 50, 600)).toBe(true);
    expect(shouldCloseSheetOnRelease(25, 10, 600)).toBe(false);
  });

  it("打开时播放展开动画；系统开了减少动态就直接显示", async () => {
    const animate = vi.fn(() => ({
      finished: Promise.resolve(),
      cancel: vi.fn(),
    }));
    const original = HTMLElement.prototype.animate;
    HTMLElement.prototype.animate = animate as never;
    try {
      const view = renderWithSheetData();
      await openStop("三峡博物馆");
      expect(animate).toHaveBeenCalled();
      view.unmount();

      animate.mockClear();
      window.matchMedia = vi.fn().mockImplementation((query: string) => ({
        ...notReduced(query),
        matches: query === "(prefers-reduced-motion: reduce)",
      }));
      renderWithSheetData();
      await openStop("三峡博物馆");
      expect(animate).not.toHaveBeenCalled();
    } finally {
      HTMLElement.prototype.animate = original;
    }
  });
});
