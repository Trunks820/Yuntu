/**
 * 2026-11 手机端阅读性第 1 步
 *
 * - 我的行程：完全正常的卡片（详细攻略、额度已结算）不再标状态和套话；基础版、结算中、失败的照旧；
 * - 首页轮播和“节奏”一步的配图改用 R2 已登记的场景大图，不再用几百像素宽的概念小图。
 *
 * 全部为假数据，不含真实邮箱、用户名或用户 ID。
 */
import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { MobileTripsPage } from "@/pages/mobile/MobileTripsPage";
import { MobileHomePage } from "@/pages/mobile/MobileHomePage";
import { MobileRhythmStep } from "@/components/mobile/MobileRhythmStep";
import * as api from "@/services/api";
import { useMobileTripStore } from "@/stores/mobileTripStore";
import { useTripTaskStore } from "@/stores/tripTaskStore";
import { createInitialMobileDraft } from "@/utils/mobileDraft";
import { SCENE_PHOTOS_BY_ID } from "@/utils/scenePhotos.generated";
import type { HistoryTripItem } from "@/types/auth";
import type { PublicGenerationDelivery } from "@/types/trip";

vi.mock("@/services/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/services/api")>();
  return {
    ...actual,
    getHistoryTrips: vi.fn(),
  };
});

vi.mock("@/hooks/useDestinations", () => ({
  useDestinations: () => ({
    data: undefined,
    isLoading: false,
    isError: false,
    isPlaceholderData: false,
    refetch: vi.fn(),
  }),
}));

vi.mock("@/hooks/useTripSubmission", () => ({
  useTripSubmission: () => ({ isSubmitting: false, executeSubmit: vi.fn() }),
}));

const DETAILED: PublicGenerationDelivery = {
  schema_version: "1.0",
  outcome: "DETAILED",
  review_state: null,
  reason_code: null,
  quota_state: "CONSUMED",
  result_available: true,
};

function trip(
  id: string,
  city: string,
  delivery: PublicGenerationDelivery,
  extra: Partial<HistoryTripItem> = {},
): HistoryTripItem {
  return {
    trip_id: `trip-${id}`,
    job_id: `job-${id}`,
    status: "SUCCESS",
    city,
    days: 2,
    result_record_id: 9000,
    delivery,
    created_at: "2026-11-20T06:50:00Z",
    finished_at: "2026-11-20T06:52:00Z",
    expires_from_history_at: "2027-02-20T06:50:00Z",
    error: null,
    ...extra,
  };
}

const REGISTERED_URLS = new Set(
  Object.values(SCENE_PHOTOS_BY_ID).flatMap((photos) =>
    photos.flatMap((p) => [p.mobileUrl, p.desktopUrl]),
  ),
);

function expectRegisteredScene(img: HTMLElement) {
  expect(REGISTERED_URLS.has(img.getAttribute("src") ?? "")).toBe(true);
  const srcset = img.getAttribute("srcset") ?? "";
  expect(srcset).toMatch(/ 960w, .+ 1920w$/);
  for (const part of srcset.split(", ")) {
    expect(REGISTERED_URLS.has(part.split(" ")[0])).toBe(true);
  }
}

describe("手机端阅读性：我的行程卡片减负", () => {
  beforeEach(() => {
    sessionStorage.clear();
    localStorage.clear();
    vi.clearAllMocks();
    useMobileTripStore.setState({ draft: createInitialMobileDraft() });
  });

  function renderTrips() {
    return render(
      <MemoryRouter initialEntries={["/history"]}>
        <Routes>
          <Route path="/history" element={<MobileTripsPage />} />
        </Routes>
      </MemoryRouter>,
    );
  }

  it("详细攻略且额度已结算的卡片只留封面和底部一行", async () => {
    vi.mocked(api.getHistoryTrips).mockResolvedValue({
      ok: true,
      items: [trip("cq", "重庆", DETAILED)],
      next_cursor: null,
    });
    const { container } = renderTrips();

    await waitFor(() => {
      expect(screen.getByAltText("重庆 · 旅行册封面")).toBeDefined();
    });
    const card = container.querySelector(".u-trip-card");
    expect(card?.classList.contains("is-plain")).toBe(true);
    expect(card?.querySelector(".u-status-row")).toBeNull();
    expect(card?.querySelector(".u-trip-desc")).toBeNull();
    expect(screen.queryByText("详细攻略已生成")).toBeNull();
    expect(screen.queryByText("你留下的选择，已经排进每一天。")).toBeNull();
    expect(screen.getByText("翻开旅行册")).toBeDefined();
  });

  it("基础版、额度结算中、失败的卡片照旧显示状态和说明", async () => {
    vi.mocked(api.getHistoryTrips).mockResolvedValue({
      ok: true,
      items: [
        trip("basic", "杭州", {
          ...DETAILED,
          outcome: "BASIC",
          quota_state: "RELEASED",
        }),
        trip("reserved", "成都", { ...DETAILED, quota_state: "RESERVED" }),
        trip(
          "failed",
          "丽江",
          {
            ...DETAILED,
            outcome: "FAILED",
            quota_state: "RELEASED",
            result_available: false,
          },
          {
            status: "FAILED",
            result_record_id: null,
            error: {
              code: "GENERATION_FAILED",
              message: "这次没能排出路线，额度已退回。",
              retryable: true,
            },
          },
        ),
      ],
      next_cursor: null,
    });
    const { container } = renderTrips();

    await waitFor(() => {
      expect(screen.getByAltText("丽江 · 旅行册封面")).toBeDefined();
    });
    const cards = Array.from(container.querySelectorAll(".u-trip-card"));
    expect(cards).toHaveLength(3);
    for (const card of cards) {
      expect(card.classList.contains("is-plain")).toBe(false);
      expect(card.querySelector(".u-status-row")).not.toBeNull();
      expect(card.querySelector(".u-trip-desc")).not.toBeNull();
    }
    expect(screen.getByText("这次没能排出路线，额度已退回。")).toBeDefined();
  });
});

describe("手机端阅读性：首页与节奏一步换成登记的场景大图", () => {
  beforeEach(() => {
    localStorage.clear();
    sessionStorage.clear();
    useMobileTripStore.setState({
      draft: createInitialMobileDraft(),
      editSnapshot: null,
    });
    useTripTaskStore.setState({ tasks: {}, budgets: {} });
  });

  it("首页三张轮播图都来自场景图清单，并给高清屏 1920 宽的版本", () => {
    const { container } = render(
      <MemoryRouter initialEntries={["/"]}>
        <Routes>
          <Route path="/" element={<MobileHomePage />} />
        </Routes>
      </MemoryRouter>,
    );
    const slides = Array.from(
      container.querySelectorAll<HTMLElement>(".hero-slide"),
    );
    expect(slides).toHaveLength(3);
    slides.forEach(expectRegisteredScene);
    expect(new Set(slides.map((s) => s.getAttribute("src"))).size).toBe(3);
  });

  it("节奏两张卡片的配图都来自场景图清单", () => {
    const { container } = render(<MobileRhythmStep onNext={vi.fn()} />);
    const images = Array.from(
      container.querySelectorAll<HTMLElement>(".rhythm-card img"),
    );
    expect(images).toHaveLength(2);
    images.forEach(expectRegisteredScene);
  });
});
