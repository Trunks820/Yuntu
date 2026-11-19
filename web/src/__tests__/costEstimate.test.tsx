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

    // rerender as flight: estimated_subset -> "部分预估" + "2,400" + badge
    rerender(
      <CostEstimateCard
        costEstimate={mixedScenarioEstimate}
        activeScenarioId="flight_round_trip"
      />,
    );

    expect(screen.getByText("部分预估")).toBeInTheDocument();
    expect(screen.getAllByText("2,400").length).toBeGreaterThan(0);
    // C7：范围说明只在摘要头部显示一次，不再重复“部分费用待确认”
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
});
