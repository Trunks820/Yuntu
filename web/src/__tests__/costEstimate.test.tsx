import { describe, test, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { CostEstimateCard } from "@/components/detail/CostEstimateCard";
import { TripSpine } from "@/components/detail/TripSpine";
import {
  COST_ESTIMATE_COMPLETE,
  COST_ESTIMATE_PARTIAL,
  COST_ESTIMATE_UNAVAILABLE,
  COST_ESTIMATE_SINGLE_TRAIN,
  COST_ESTIMATE_MIXED_POLICY_ZERO,
} from "@/fixtures/v0.9.4-fixtures";
import type { CostEstimateSummary } from "@/types/cost";

describe("v0.9.4 P6 Acceptance - CostEstimateCard Visual & Contract Refactor Tests", () => {
  test("[Req-1] Dual mode (train + flight) initializes unselected with status text and NO monetary amount in DOM", () => {
    const { container } = render(
      <CostEstimateCard
        costEstimate={COST_ESTIMATE_COMPLETE}
        activeScenarioId={null}
      />,
    );

    // Displays status text in main banner
    expect(screen.getByText("选择交通方式查看预估")).toBeInTheDocument();

    // Renders both scenario radio options
    expect(screen.getByText("高铁往返方案")).toBeInTheDocument();
    expect(screen.getByText("机票往返方案")).toBeInTheDocument();

    // Assert NO monetary amounts (no '¥' symbol or price figures) exist anywhere in DOM when unselected
    expect(container.textContent).not.toContain("¥");
  });

  test("[Req-2] Toggle between complete (train full_trip) and partial (flight estimated_subset) scenario updates status copy dynamically", () => {
    const mixedScenarioEstimate: CostEstimateSummary = {
      ...COST_ESTIMATE_COMPLETE,
      scenarios: [
        { ...COST_ESTIMATE_COMPLETE.scenarios[0], total_scope: "full_trip" },
        { ...COST_ESTIMATE_COMPLETE.scenarios[1], total_scope: "estimated_subset" },
      ],
    };

    const { rerender } = render(
      <CostEstimateCard
        costEstimate={mixedScenarioEstimate}
        activeScenarioId="train_round_trip"
      />,
    );

    // train: full_trip -> "完整预估" + "1,800"
    expect(screen.getByText("完整预估")).toBeInTheDocument();
    expect(screen.getAllByText("1,800").length).toBeGreaterThan(0);
    expect(screen.queryByText("部分费用待确认")).not.toBeInTheDocument();

    // rerender as flight: estimated_subset -> "部分预估" + "2,400"（重复的“部分费用待确认”已删）
    rerender(
      <CostEstimateCard
        costEstimate={mixedScenarioEstimate}
        activeScenarioId="flight_round_trip"
      />,
    );

    expect(screen.getByText("部分预估")).toBeInTheDocument();
    expect(screen.getAllByText("2,400").length).toBeGreaterThan(0);
    expect(screen.queryByText("部分费用待确认")).not.toBeInTheDocument();
  });

  test("[Req-3] mixed and policy_zero price basis labels render correctly", () => {
    render(
      <CostEstimateCard
        costEstimate={COST_ESTIMATE_MIXED_POLICY_ZERO}
        activeScenarioId="train_round_trip"
      />,
    );

    expect(screen.getAllByText("来源与参考混合").length).toBeGreaterThan(0);
    expect(screen.getAllByText("按规则计为 ¥0").length).toBeGreaterThan(0);
  });

  test("[Req-4] Sole without_intercity or single scenario automatically triggers onScenarioSelect to activate", () => {
    const handleSelect = vi.fn();
    render(
      <CostEstimateCard
        costEstimate={COST_ESTIMATE_SINGLE_TRAIN}
        activeScenarioId={null}
        onScenarioSelect={handleSelect}
      />,
    );

    expect(handleSelect).toHaveBeenCalledWith("train_round_trip");
  });

  test("[Req-5] Scenario selection buttons support keyboard operation and correct ARIA states", () => {
    const handleSelect = vi.fn();
    render(
      <CostEstimateCard
        costEstimate={COST_ESTIMATE_COMPLETE}
        activeScenarioId="train_round_trip"
        onScenarioSelect={handleSelect}
      />,
    );

    const trainRadio = screen.getByRole("radio", { name: /高铁往返方案/ });
    const flightRadio = screen.getByRole("radio", { name: /机票往返方案/ });

    expect(trainRadio).toHaveAttribute("aria-checked", "true");
    expect(flightRadio).toHaveAttribute("aria-checked", "false");

    fireEvent.click(flightRadio);
    expect(handleSelect).toHaveBeenCalledWith("flight_round_trip");
  });

  test("[Req-6] CostEstimateCard header renders '预算参考' title", () => {
    render(
      <CostEstimateCard
        costEstimate={COST_ESTIMATE_COMPLETE}
        activeScenarioId="train_round_trip"
      />,
    );

    expect(screen.getAllByText("预算参考").length).toBeGreaterThan(0);
  });

  test("[Req-7] Unavailable state renders '费用暂不可估算' and NO ¥0", () => {
    const { container } = render(
      <CostEstimateCard
        costEstimate={COST_ESTIMATE_UNAVAILABLE}
        activeScenarioId="without_intercity"
      />,
    );

    expect(screen.getAllByText("费用暂不可估算").length).toBeGreaterThan(0);
    expect(container.textContent).not.toContain("¥0");
  });

  test("[Req-8] Missing categories render '待确认' and do NOT show ¥0", () => {
    const { container } = render(
      <CostEstimateCard
        costEstimate={COST_ESTIMATE_PARTIAL}
        activeScenarioId="without_intercity"
      />,
    );

    expect(screen.getAllByText("待确认").length).toBeGreaterThan(0);
    expect(container.textContent).not.toContain("¥0");
  });

  test("[Req-9] Forbidden strings do NOT exist in rendered output", () => {
    const { container } = render(
      <CostEstimateCard
        costEstimate={COST_ESTIMATE_COMPLETE}
        activeScenarioId="train_round_trip"
      />,
    );

    const forbiddenPattern = /mockBudget|budgetCap|usedPercent|参考总预算|约占 85%|人均总额|其他/;
    expect(container.textContent).not.toMatch(forbiddenPattern);
  });

  test("[C7-1] A single priced category renders no bar at all, the row itself stays", () => {
    const singlePricedCategoryEstimate: CostEstimateSummary = {
      ...COST_ESTIMATE_SINGLE_TRAIN,
      scenarios: [
        {
          ...COST_ESTIMATE_SINGLE_TRAIN.scenarios[0],
          categories: COST_ESTIMATE_SINGLE_TRAIN.scenarios[0].categories.map((c) =>
            c.category === "accommodation"
              ? c
              : { ...c, coverage: "missing" as const, range: null, price_basis: null },
          ),
        },
      ],
    };

    const { container } = render(
      <CostEstimateCard
        costEstimate={singlePricedCategoryEstimate}
        activeScenarioId="train_round_trip"
      />,
    );

    // 只有一个有金额的支出项：整张卡一条进度条都不画（虚线占位也不画）
    expect(container.querySelectorAll('[data-testid="cost-category-bar"]').length).toBe(0);

    // 这一行本身照常显示；没算出来的行仍保留“待确认”
    expect(screen.getByText("住宿")).toBeInTheDocument();
    expect(screen.getAllByText("¥ 700").length).toBeGreaterThan(0);
    expect(screen.getAllByText("待确认").length).toBe(4);
  });

  test("[C7-1] Several priced categories still get one bar each", () => {
    const { container } = render(
      <CostEstimateCard
        costEstimate={COST_ESTIMATE_COMPLETE}
        activeScenarioId="train_round_trip"
      />,
    );

    expect(container.querySelectorAll('[data-testid="cost-category-bar"]').length).toBe(5);
  });

  test("[C7-2] The partial scope is explained once: '部分预估' stays, the repeated badge is gone", () => {
    render(
      <CostEstimateCard
        costEstimate={COST_ESTIMATE_PARTIAL}
        activeScenarioId="without_intercity"
      />,
    );

    expect(screen.getByText("部分预估")).toBeInTheDocument();
    expect(screen.queryByText("部分费用待确认")).not.toBeInTheDocument();
    // 金额没算出来的行保留“待确认”
    expect(screen.getAllByText("待确认").length).toBe(1);
  });

  test("[C7-3] The intercity row uses travel's own word 大交通 and no longer contradicts the scenario label", () => {
    render(
      <CostEstimateCard
        costEstimate={COST_ESTIMATE_PARTIAL}
        activeScenarioId="without_intercity"
      />,
    );

    // travel 给场景的 label 是“不含大交通方案”，支出行也用同一个词，两条口径一致
    expect(screen.getByText("不含大交通方案")).toBeInTheDocument();
    expect(screen.getByText("大交通")).toBeInTheDocument();
    expect(screen.queryByText("交通")).not.toBeInTheDocument();
    expect(screen.getByText("市内交通")).toBeInTheDocument();
  });

  test("[C7-4] The footer shows travel's notice when present and only falls back when it is empty", () => {
    const travelNotice = "费用为规划参考，实际支付金额请以预订或现场结算为准";
    const { unmount } = render(
      <CostEstimateCard
        costEstimate={COST_ESTIMATE_SINGLE_TRAIN}
        activeScenarioId="train_round_trip"
      />,
    );

    expect(screen.getByText(travelNotice)).toBeInTheDocument();
    expect(screen.queryByText("费用为规划参考，实际花费以出行行为准")).not.toBeInTheDocument();
    unmount();

    render(
      <CostEstimateCard
        costEstimate={{ ...COST_ESTIMATE_SINGLE_TRAIN, notice: "" }}
        activeScenarioId="train_round_trip"
      />,
    );

    expect(screen.getByText("费用为规划参考，实际花费以出行行为准")).toBeInTheDocument();
  });

  test("[C7-5] No invented wording such as 今日 / 每日 / 算力 appears anywhere in the card", () => {
    const { container } = render(
      <CostEstimateCard
        costEstimate={COST_ESTIMATE_PARTIAL}
        activeScenarioId="without_intercity"
      />,
    );

    expect(container.textContent).not.toMatch(/今日|每日|算力/);
  });

  test("[A5] Mobile variant uses the shared tokens instead of Satoshi and bare teal/gray, structure unchanged", () => {
    const { container } = render(
      <CostEstimateCard
        variant="mobile"
        costEstimate={COST_ESTIMATE_COMPLETE}
        activeScenarioId="train_round_trip"
      />,
    );

    // 结构、文案、行数都不变，只换颜色和字体
    expect(screen.getAllByText("预算参考").length).toBeGreaterThan(0);
    expect(screen.getAllByText("1,800").length).toBeGreaterThan(0);
    expect(container.querySelectorAll('[data-testid="cost-category-bar"]').length).toBe(5);

    expect(container.innerHTML).toContain("var(--yt-ink)");
    expect(container.innerHTML).toContain("var(--font-serif)");
    expect(container.innerHTML).not.toContain("font-display");
    expect(container.innerHTML).not.toContain("teal-");
  });

  test("[A5] Desktop variant stays on its own palette and never picks up the mobile tokens", () => {
    const { container } = render(
      <CostEstimateCard
        costEstimate={COST_ESTIMATE_COMPLETE}
        activeScenarioId="train_round_trip"
      />,
    );

    expect(container.innerHTML).not.toContain("var(--yt-");
    expect(container.innerHTML).not.toContain("var(--font-");
    expect(container.innerHTML).toContain("font-display");
    expect(container.innerHTML).toContain("teal-600");
  });
});

describe("v0.9.4 P6 Acceptance - TripSpine Compact Cost Summary Tests", () => {
  const dummyDays = [{ day: 1, title: "D1", places: [], commute_legs: [], commute_summary: "", pace_status: "WITHIN_LIMIT" as const, narrative: "" }];

  test("[Spine-1] Unselected state in spine shows '选择交通方式查看预估'", () => {
    render(
      <TripSpine
        days={dummyDays}
        activeDay={1}
        costEstimate={COST_ESTIMATE_COMPLETE}
        activeScenarioId={null}
      />,
    );

    expect(screen.getByText("选择交通方式查看预估")).toBeInTheDocument();
  });

  test("[Spine-2] Complete active scenario in spine shows '预估 ¥1,200–¥1,800' and scenario label", () => {
    render(
      <TripSpine
        days={dummyDays}
        activeDay={1}
        costEstimate={COST_ESTIMATE_COMPLETE}
        activeScenarioId="train_round_trip"
      />,
    );

    expect(screen.getByText("预估 ¥ 1,800")).toBeInTheDocument();
    expect(screen.getByText("高铁往返方案")).toBeInTheDocument();
  });

  test("[Spine-3] Partial state in spine shows '已估 ¥ 1,460' and '部分费用待确认'", () => {
    render(
      <TripSpine
        days={dummyDays}
        activeDay={1}
        costEstimate={COST_ESTIMATE_PARTIAL}
        activeScenarioId="without_intercity"
      />,
    );

    expect(screen.getByText("已估 ¥ 1,460")).toBeInTheDocument();
    expect(screen.getByText("部分费用待确认")).toBeInTheDocument();
  });

  test("[Spine-4] Unavailable state in spine shows '费用暂不可估算'", () => {
    render(
      <TripSpine
        days={dummyDays}
        activeDay={1}
        costEstimate={COST_ESTIMATE_UNAVAILABLE}
        activeScenarioId="without_intercity"
      />,
    );

    expect(screen.getByText("费用暂不可估算")).toBeInTheDocument();
  });

  test("[Spine-5] Partial admission in spine uses total_range_with_known_admission and shows unpriced admission note", () => {
    const partialAdmissionEstimate: CostEstimateSummary = {
      ...COST_ESTIMATE_PARTIAL,
      scenarios: [
        {
          ...COST_ESTIMATE_PARTIAL.scenarios[0],
          total_range: { min_cny: 2200, max_cny: 2800 },
          total_range_with_known_admission: { min_cny: 2320, max_cny: 2920 },
          unpriced_admission_count: 2,
          categories: COST_ESTIMATE_PARTIAL.scenarios[0].categories.map((c) =>
            c.category === "admission"
              ? {
                  ...c,
                  coverage: "missing" as const,
                  range: null,
                  price_basis: null,
                  known_range: { min_cny: 120, max_cny: 120 },
                  priced_item_count: 1,
                  unpriced_item_count: 2,
                }
              : c,
          ),
          missing_categories: ["intercity_transport", "admission"],
        },
      ],
    };

    render(
      <TripSpine
        days={dummyDays}
        activeDay={1}
        costEstimate={partialAdmissionEstimate}
        activeScenarioId="without_intercity"
      />,
    );

    expect(screen.getByText("已估 ¥ 2,920")).toBeInTheDocument();
    expect(screen.getByText("另有 2 处门票未计入")).toBeInTheDocument();
    expect(screen.queryByText("部分费用待确认")).not.toBeInTheDocument();
  });
});

describe("v0.15.3 F2 - CostEstimateCard Partial Admission Regression", () => {
  test("renders partial admission row and total_range_with_known_admission while keeping C7 single scope label", () => {
    const partialAdmissionEstimate: CostEstimateSummary = {
      ...COST_ESTIMATE_SINGLE_TRAIN,
      completeness: "partial",
      scenarios: [
        {
          ...COST_ESTIMATE_SINGLE_TRAIN.scenarios[0],
          total_scope: "estimated_subset",
          total_range: { min_cny: 2200, max_cny: 2800 },
          total_range_with_known_admission: { min_cny: 2320, max_cny: 2920 },
          unpriced_admission_count: 2,
          categories: COST_ESTIMATE_SINGLE_TRAIN.scenarios[0].categories.map((c) =>
            c.category === "admission"
              ? {
                  ...c,
                  coverage: "missing" as const,
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

    render(
      <CostEstimateCard
        variant="mobile"
        costEstimate={partialAdmissionEstimate}
        activeScenarioId="train_round_trip"
      />,
    );

    expect(screen.getByText("部分预估")).toBeInTheDocument();
    expect(screen.queryByText("部分费用待确认")).not.toBeInTheDocument();
    expect(screen.getByText("2,920")).toBeInTheDocument();
    expect(screen.queryByText("2,800")).not.toBeInTheDocument();
    expect(screen.getByText("另有 2 处门票未计入")).toBeInTheDocument();
    expect(screen.getByText("已知 ¥120，另有 2 处待确认")).toBeInTheDocument();
    expect(screen.queryByText("待确认")).not.toBeInTheDocument();
  });
});
