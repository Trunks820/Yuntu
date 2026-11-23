/**
 * 2026-11 第 5 步：电脑端旅行册动效
 *
 * - 长卷卡片（连同两站之间的通勤段）首次出现和换天时依次升起，总长不超过 600ms；普通重绘、收着时、减少动态时不播；
 * - 按钮、页签、卡片、站钉按下微缩，减少动态时不缩。
 *
 * 全部为假数据。
 */
import fs from "node:fs";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render } from "@testing-library/react";
import { DayStrip, type DayStripProps } from "@/pages/book/DayStrip";
import type { TripDay, TripPlace } from "@/types/trip";

function place(id: number, name: string): TripPlace {
  return {
    place_id: id,
    name,
    category: "attraction",
    longitude: 106.55,
    latitude: 29.56,
    role: "anchor_activity",
    optional: false,
    brief: "",
    stay_minutes: 60,
    activity_note: `${name}的说明`,
  };
}

function day(num: number, ids: number[]): TripDay {
  return {
    day: num,
    title: `第 ${num} 天`,
    narrative: "",
    commute_summary: "",
    pace_status: "WITHIN_LIMIT",
    places: ids.map((id) => place(id, `地点 ${id}`)),
    commute_legs: [],
  };
}

const DAYS = [day(1, [11, 12, 13]), day(2, [21, 22])];

function props(overrides: Partial<DayStripProps> = {}): DayStripProps {
  return {
    days: DAYS,
    currentDay: DAYS[0],
    selectedPlaceId: null,
    weather: null,
    placeDetails: {},
    brokenImageUrls: new Set<string>(),
    onImageError: vi.fn(),
    onSelectDay: vi.fn(),
    onSelectPlace: vi.fn(),
    ...overrides,
  };
}

function mockMatchMedia(reduce: boolean) {
  window.matchMedia = vi.fn().mockImplementation((query: string) => ({
    matches: reduce && query === "(prefers-reduced-motion: reduce)",
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  }));
}

describe("电脑端旅行册：长卷卡片依次升起", () => {
  const originalAnimate = HTMLElement.prototype.animate;
  let animate: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    mockMatchMedia(false);
    animate = vi.fn();
    HTMLElement.prototype.animate = animate as never;
  });

  afterEach(() => {
    HTMLElement.prototype.animate = originalAnimate;
  });

  function timings() {
    return animate.mock.calls.map(
      (call) => call[1] as { duration: number; delay: number; fill: string },
    );
  }

  it("首次出现时每张卡片依次升起，间隔均匀，总长不超过 600ms，结束后不留样式", () => {
    render(<DayStrip {...props()} />);

    const t = timings();
    // 3 站 + 中间 2 段通勤
    expect(t).toHaveLength(5);
    expect(t.map((x) => x.delay)).toEqual([0, 50, 100, 150, 200]);
    expect(Math.max(...t.map((x) => x.delay + x.duration))).toBeLessThanOrEqual(
      600,
    );
    expect(t.every((x) => x.fill === "backwards")).toBe(true);
    const [frames] = animate.mock.calls[0] as [Keyframe[]];
    expect(frames[0]).toMatchObject({
      opacity: 0,
      transform: "translateY(14px)",
    });
    expect(frames[1]).toMatchObject({ opacity: 1, transform: "none" });
  });

  it("站点很多时压缩间隔，总长仍不超过 600ms", () => {
    const longDay = day(3, [31, 32, 33, 34, 35, 36, 37, 38]);
    render(<DayStrip {...props({ days: [longDay], currentDay: longDay })} />);

    const t = timings();
    // 8 站 + 中间 7 段通勤
    expect(t).toHaveLength(15);
    expect(Math.max(...t.map((x) => x.delay + x.duration))).toBeLessThanOrEqual(
      600,
    );
  });

  it("换天时重播，只是选中卡片等普通重绘不重播", () => {
    const { rerender } = render(<DayStrip {...props()} />);
    animate.mockClear();

    rerender(<DayStrip {...props({ selectedPlaceId: 12 })} />);
    expect(animate).not.toHaveBeenCalled();

    rerender(<DayStrip {...props({ currentDay: DAYS[1] })} />);
    // 2 站 + 中间 1 段通勤
    expect(animate).toHaveBeenCalledTimes(3);
  });

  it("长卷收着（阅读模式或收起）时不播", () => {
    render(<DayStrip {...props({ collapsed: true })} />);
    render(<DayStrip {...props({ folded: true })} />);
    expect(animate).not.toHaveBeenCalled();
  });

  it("系统开了减少动态时不播", () => {
    mockMatchMedia(true);
    render(<DayStrip {...props()} />);
    expect(animate).not.toHaveBeenCalled();
  });
});

describe("电脑端旅行册：按下微缩", () => {
  const css = fs.readFileSync(
    path.resolve(process.cwd(), "src/pages/book/spread-book.css"),
    "utf-8",
  );

  it("按钮、页签、翻页、收起、关闭、看住处按下缩到 0.96，卡片 0.985，未选中的站钉 0.92", () => {
    for (const sel of [
      ".bk-bar button:active",
      ".bk-dtab:active",
      ".bk-fold:active",
      ".bk-side .bk-side-x:active",
      ".bk-side .bk-rd-pager button:active",
      ".bk-side .bk-pg .bk-show-stay:active",
    ]) {
      expect(css).toContain(`.yuntu-spread .bk-atlas ${sel}`);
    }
    expect(css).toMatch(/\.bk-dc:active \{\s*transform: scale\(0\.985\);/);
    expect(css).toMatch(
      /\.gpin:not\(\.is-on\):active \{\s*transform: scale\(0\.92\);/,
    );
    expect(css).toMatch(/:active \{\s*transform: scale\(0\.96\);/);
  });

  it("减少动态时按下不缩放", () => {
    expect(css).toMatch(
      /@media \(prefers-reduced-motion: reduce\) \{[^@]*\.gpin:not\(\.is-on\):active \{\s*transform: none;/,
    );
  });
});
