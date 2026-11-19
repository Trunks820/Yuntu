import { describe, test, expect, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, act } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { MustIncludeNotice } from "@/components/detail/MustIncludeNotice";
import { PlaceDetailModal } from "@/components/detail/PlaceDetailModal";
import PlanDetailPage from "@/pages/PlanDetailPage";
import type { TripMustInclude, TripResult } from "@/types/trip";
import { useTripStore } from "@/stores/tripStore";

const { mockPageResult } = vi.hoisted(() => ({
  mockPageResult: {
    schema_version: "2.0",
    result_id: 801,
    city: { name: "重庆" },
    request: { days: 3, people_count: 2, preferences: ["美食"], avoid: [] },
    must_include: [
      { name: "解放碑", status: "scheduled", place_id: 1, reason: null },
      { name: "九寨沟", status: "cross_city", place_id: null, reason: "不在目的地城市范围内" },
    ],
    plans: [
      {
        plan_id: "plan_v094_1",
        title: "重庆经典 3 日游",
        summary: "经典地标与老街漫步方案",
        tags: ["经典", "美食"],
        pace: { level: "MODERATE", commute_status: "WITHIN_LIMIT", total_commute_minutes: 40 },
        cost_estimate: {
          snapshot_version: "1",
          completeness: "complete",
          currency: "CNY",
          estimated_at: "2026-08-04T00:00:00Z",
          scenarios: [
            {
              scenario_id: "train_round_trip",
              intercity_mode: "train",
              label: "高铁往返方案",
              total_scope: "full_trip",
              total_range: { min_cny: 1200, max_cny: 1800 },
              categories: [],
              missing_categories: [],
            },
          ],
          assumptions: [],
          exclusions: [],
          notice: "费用为规划参考",
        },
        days: [
          {
            day: 1,
            title: "解放碑与洪崖洞",
            commute_summary: "总通勤约 20 分钟",
            pace_status: "WITHIN_LIMIT",
            narrative: "第一天打卡解放碑与洪崖洞。",
            places: [
              { place_id: 1, name: "解放碑", category: "landmark", longitude: 106.5784, latitude: 29.5574, role: "anchor", optional: false, brief: "核心地标" },
            ],
            commute_legs: [],
          },
        ],
      },
    ],
  },
}));

vi.mock("@/services/api", () => ({
  fetchResult: vi.fn().mockImplementation(() => Promise.resolve(mockPageResult)),
  fetchPlaceDetail: vi.fn().mockImplementation(() => Promise.resolve(null)),
  ApiRequestError: class ApiRequestError extends Error {
    status?: number;
    code?: string;
  },
}));

describe("v0.9.4 P6 Acceptance - MustInclude Notice & Inline Badge Tests", () => {
  test("[Notice-1] Unscheduled, cross-city, candidate, or unmatched items render notice banner below Hero", () => {
    const items: TripMustInclude[] = [
      { name: "解放碑", status: "scheduled", place_id: 1, reason: null },
      { name: "九寨沟", status: "cross_city", place_id: null, reason: "不在目的地城市范围内" },
      { name: "磁器口", status: "not_scheduled", place_id: 3, reason: "时间预算未能容纳" },
      { name: "南山一棵树", status: "recorded_unmatched", place_id: null, reason: "未匹配到地点" },
    ];

    render(<MustIncludeNotice items={items} />);

    expect(screen.getByRole("region", { name: "必去地点调整说明" })).toBeInTheDocument();
    expect(screen.getByText("九寨沟")).toBeInTheDocument();
    expect(screen.getByText("磁器口")).toBeInTheDocument();
    expect(screen.getByText("南山一棵树")).toBeInTheDocument();
    expect(screen.queryByText("解放碑")).not.toBeInTheDocument();
  });

  test("[Notice-2] All items scheduled renders NOTHING (banner hidden)", () => {
    const items: TripMustInclude[] = [
      { name: "解放碑", status: "scheduled", place_id: 1, reason: null },
      { name: "洪崖洞", status: "scheduled", place_id: 2, reason: null },
    ];

    const { container } = render(<MustIncludeNotice items={items} />);
    expect(container.firstChild).toBeNull();
  });

  test("[Req-6] Place card renders '你的必去' badge strictly by place_id match, and NOT by name", async () => {
    act(() => {
      useTripStore.getState().setResult("801", "", mockPageResult as unknown as TripResult);
    });

    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    await act(async () => {
      render(
        <QueryClientProvider client={queryClient}>
          <MemoryRouter initialEntries={["/plan/801/plan_v094_1"]}>
            <Routes>
              <Route path="/plan/:resultId/:planId" element={<PlanDetailPage />} />
            </Routes>
          </MemoryRouter>
        </QueryClientProvider>,
      );
    });

    expect(screen.getByText("重庆经典 3 日游")).toBeInTheDocument();

    // Place_id = 1 (解放碑) has status === "scheduled" -> renders "你的必去"
    expect(screen.getByText("你的必去")).toBeInTheDocument();

    // Verify only ONE #cost-estimate anchor exists on page
    const costEstimateAnchors = document.querySelectorAll("#cost-estimate");
    expect(costEstimateAnchors.length).toBe(1);

    // Verify Sticky Navigation tabs contain ONLY '概览', 'D1', and '出行费用'
    expect(screen.getByText("出行费用")).toBeInTheDocument();
    expect(screen.queryByText("预算")).not.toBeInTheDocument();
    expect(screen.queryByText("必去")).not.toBeInTheDocument();
    expect(screen.queryByText("BUDGET")).not.toBeInTheDocument();
    expect(screen.queryByText("MUST-GO")).not.toBeInTheDocument();
  });

  test("[Modal-1] PlaceDetailModal renders '你的必去' badge strictly when isMustInclude is true", async () => {
    const place = { place_id: 1, name: "解放碑", category: "landmark", longitude: 106.57, latitude: 29.55, role: "anchor", optional: false, brief: "" };

    let rerenderFn: ReturnType<typeof render>["rerender"];
    await act(async () => {
      const res = render(<PlaceDetailModal place={place} isMustInclude={true} onClose={() => {}} />);
      rerenderFn = res.rerender;
    });
    expect(screen.getByText("你的必去")).toBeInTheDocument();

    await act(async () => {
      rerenderFn(<PlaceDetailModal place={place} isMustInclude={false} onClose={() => {}} />);
    });
    expect(screen.queryByText("你的必去")).not.toBeInTheDocument();
  });
});
