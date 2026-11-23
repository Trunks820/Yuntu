import { describe, expect, test, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { CostEstimateCard } from "@/components/detail/CostEstimateCard";
import { BookPages } from "@/pages/book/BookPages";
import {
  COST_ESTIMATE_COMPLETE,
  COST_ESTIMATE_PARTIAL,
  COST_ESTIMATE_UNAVAILABLE,
  SAMPLE_RESULT_V094,
} from "@/fixtures/v0.9.4-fixtures";
import {
  formatPartialAdmissionText,
  formatUnpricedAdmissionNote,
  getScenarioCostStatus,
  resolveScenarioTotalRange,
  type CostEstimateSummary,
  type CostScenarioSummary,
} from "@/types/cost";
import type { TripResult } from "@/types/trip";

function makePartialAdmissionScenario(
  overrides?: Partial<CostScenarioSummary>,
): CostScenarioSummary {
  return {
    scenario_id: "train_round_trip",
    intercity_mode: "train",
    label: "普通火车往返",
    total_scope: "estimated_subset",
    total_range: { min_cny: 2200, max_cny: 2800 },
    total_range_with_known_admission: { min_cny: 2320, max_cny: 2920 },
    unpriced_admission_count: 2,
    categories: [
      {
        category: "intercity_transport",
        coverage: "priced",
        range: { min_cny: 400, max_cny: 500 },
        price_basis: "sourced",
        basis_label: "普通火车往返",
      },
      {
        category: "accommodation",
        coverage: "priced",
        range: { min_cny: 1000, max_cny: 1200 },
        price_basis: "reference",
        basis_label: "市区舒适型酒店",
      },
      {
        category: "local_transport",
        coverage: "priced",
        range: { min_cny: 200, max_cny: 300 },
        price_basis: "reference",
        basis_label: "按打车估算",
      },
      {
        category: "admission",
        coverage: "missing",
        range: null,
        price_basis: null,
        basis_label: "部分门票待确认",
        known_range: { min_cny: 120, max_cny: 120 },
        priced_item_count: 1,
        unpriced_item_count: 2,
      },
      {
        category: "meals",
        coverage: "priced",
        range: { min_cny: 600, max_cny: 800 },
        price_basis: "reference",
        basis_label: "不含早餐和小吃",
      },
    ],
    missing_categories: ["admission"],
    ...overrides,
  };
}

function makePartialAdmissionEstimate(
  scenarios: CostScenarioSummary[] = [makePartialAdmissionScenario()],
): CostEstimateSummary {
  return {
    snapshot_version: "1",
    completeness: "partial",
    currency: "CNY",
    estimated_at: "2026-11-21T00:00:00Z",
    scenarios,
    assumptions: [
      { code: "meals_main_only", label: "不含早餐和小吃" },
      { code: "local_taxi", label: "按打车估算" },
    ],
    exclusions: [{ code: "no_intercity", label: "未填出发城市所以不含往返交通" }],
    notice: "没查到直达高铁或直飞航班，票价待确认",
  };
}

function makeResultWithCost(costEstimate: CostEstimateSummary): TripResult {
  return {
    ...SAMPLE_RESULT_V094,
    plans: [
      {
        ...SAMPLE_RESULT_V094.plans[0],
        cost_estimate: costEstimate,
      },
    ],
  };
}

describe("v0.15.3 F2 - Partial admission helpers and edge guards", () => {
  test("formatPartialAdmissionText handles exact, range, and fallback edge cases", () => {
    const admission = makePartialAdmissionScenario().categories[3];

    expect(formatPartialAdmissionText(admission)).toBe(
      "已知 ¥120，另有 2 处待确认",
    );
    expect(
      formatPartialAdmissionText({
        ...admission,
        known_range: { min_cny: 120, max_cny: 180 },
      }),
    ).toBe("已知 ¥120–¥180，另有 2 处待确认");

    // 边界：缺 known_range / unpriced_item_count <= 0 / priced_item_count === 0 / 非 admission / priced
    expect(
      formatPartialAdmissionText({ ...admission, known_range: null }),
    ).toBeNull();
    expect(
      formatPartialAdmissionText({ ...admission, unpriced_item_count: 0 }),
    ).toBeNull();
    expect(
      formatPartialAdmissionText({ ...admission, priced_item_count: 0 }),
    ).toBeNull();
    expect(
      formatPartialAdmissionText({
        ...admission,
        category: "meals",
      }),
    ).toBeNull();
    expect(
      formatPartialAdmissionText({
        ...admission,
        coverage: "priced",
        range: { min_cny: 120, max_cny: 120 },
      }),
    ).toBeNull();
  });

  test("resolveScenarioTotalRange and formatUnpricedAdmissionNote prefer new fields and fallback cleanly", () => {
    const sc = makePartialAdmissionScenario();
    expect(resolveScenarioTotalRange(sc)).toEqual({
      min_cny: 2320,
      max_cny: 2920,
    });
    expect(formatUnpricedAdmissionNote(sc)).toBe("另有 2 处门票未计入");

    const legacySc = COST_ESTIMATE_PARTIAL.scenarios[0];
    expect(resolveScenarioTotalRange(legacySc)).toEqual(legacySc.total_range);
    expect(formatUnpricedAdmissionNote(legacySc)).toBeNull();
    expect(
      formatUnpricedAdmissionNote({ ...sc, unpriced_admission_count: 0 }),
    ).toBeNull();

    // unavailable / unselected 不得输出未计入门票文案
    const unavailStatus = getScenarioCostStatus(
      {
        ...COST_ESTIMATE_UNAVAILABLE.scenarios[0],
        unpriced_admission_count: 2,
      },
      false,
    );
    expect(unavailStatus.unpricedAdmissionNote).toBeNull();
    expect(unavailStatus.costBadge).toBeNull();

    const unselectedStatus = getScenarioCostStatus(sc, true);
    expect(unselectedStatus.unpricedAdmissionNote).toBeNull();
    expect(unselectedStatus.costBadge).toBeNull();
  });
});

describe("v0.15.3 F2 - Mobile CostEstimateCard & PC BookPages acceptance", () => {
  test("F2-AC1 & F2-AC2: CostEstimateCard (mobile & desktop) shows partial admission and total with known admission", () => {
    const estimate = makePartialAdmissionEstimate();

    for (const variant of ["mobile", "desktop"] as const) {
      const { container, unmount } = render(
        <CostEstimateCard
          variant={variant}
          costEstimate={estimate}
          activeScenarioId="train_round_trip"
        />,
      );

      // AC1: 门票行显示“已知 ¥120，另有 2 处待确认”，不再只写“待确认”，且仍保持 missing（虚线空轨）
      expect(screen.getByText("已知 ¥120，另有 2 处待确认")).toBeInTheDocument();
      expect(screen.queryByText("待确认")).not.toBeInTheDocument();
      expect(
        container.querySelectorAll('[data-testid="cost-category-bar"] .border-dashed')
          .length,
      ).toBe(1);

      // AC2: 总价使用 total_range_with_known_admission，后面写“另有 2 处门票未计入”，C7 不重复“部分费用待确认”
      expect(screen.getByText("2,920")).toBeInTheDocument();
      expect(screen.queryByText("2,800")).not.toBeInTheDocument();
      expect(screen.getByText("另有 2 处门票未计入")).toBeInTheDocument();
      expect(screen.getByText("部分预估")).toBeInTheDocument();
      expect(screen.queryByText("部分费用待确认")).not.toBeInTheDocument();

      // 后端透传的场景名和底部说明原样显示
      expect(screen.getByText("普通火车往返")).toBeInTheDocument();
      expect(
        screen.getByText("没查到直达高铁或直飞航班，票价待确认"),
      ).toBeInTheDocument();

      unmount();
    }
  });

  test("F2-AC1 & F2-AC2: PC BookPages cost drawer applies the exact same rules and supports scenario switching", () => {
    const trainSc = makePartialAdmissionScenario();
    const flightSc = makePartialAdmissionScenario({
      scenario_id: "flight_round_trip",
      intercity_mode: "flight",
      label: "直飞往返方案",
      total_range: { min_cny: 3000, max_cny: 3600 },
      total_range_with_known_admission: { min_cny: 3120, max_cny: 3720 },
      unpriced_admission_count: 1,
      categories: trainSc.categories.map((c) =>
        c.category === "admission"
          ? {
              ...c,
              known_range: { min_cny: 120, max_cny: 180 },
              unpriced_item_count: 1,
            }
          : c,
      ),
    });

    const result = makeResultWithCost(
      makePartialAdmissionEstimate([trainSc, flightSc]),
    );

    render(
      <BookPages
        page="cost"
        result={result}
        plan={result.plans[0]}
        onClose={vi.fn()}
        onLocateStay={vi.fn()}
      />,
    );

    const aside = screen.getByRole("complementary", { name: "花多少" });
    expect(aside).toHaveTextContent("已估 ¥ 2,920");
    expect(aside).not.toHaveTextContent("¥ 2,800");
    expect(aside).toHaveTextContent("另有 2 处门票未计入");
    expect(aside).not.toHaveTextContent("部分费用待确认");
    expect(aside).toHaveTextContent("已知 ¥120，另有 2 处待确认");
    // 后端透传文案原样展示
    expect(aside).toHaveTextContent("普通火车往返");
    expect(aside).toHaveTextContent("不含早餐和小吃");
    expect(aside).toHaveTextContent("按打车估算");
    expect(aside).toHaveTextContent("未填出发城市所以不含往返交通");
    expect(aside).toHaveTextContent("没查到直达高铁或直飞航班，票价待确认");

    // 切换场景后同步更新总价、未计入门票提示与门票区间文案
    fireEvent.click(screen.getByRole("radio", { name: "直飞往返方案" }));
    expect(aside).toHaveTextContent("已估 ¥ 3,720");
    expect(aside).toHaveTextContent("另有 1 处门票未计入");
    expect(aside).toHaveTextContent("已知 ¥120–¥180，另有 1 处待确认");
  });

  test("F2-AC3: Legacy results without new fields render identically on both CostEstimateCard and BookPages", () => {
    const { container, unmount } = render(
      <CostEstimateCard
        costEstimate={COST_ESTIMATE_PARTIAL}
        activeScenarioId="without_intercity"
      />,
    );

    expect(screen.getByText("1,460")).toBeInTheDocument();
    expect(screen.getByText("部分预估")).toBeInTheDocument();
    expect(screen.getAllByText("待确认").length).toBe(1);
    expect(container.textContent).not.toContain("另有");
    expect(container.textContent).not.toContain("已知");
    unmount();

    const legacyResult = makeResultWithCost(COST_ESTIMATE_COMPLETE);
    render(
      <BookPages
        page="cost"
        result={legacyResult}
        plan={legacyResult.plans[0]}
        onClose={vi.fn()}
        onLocateStay={vi.fn()}
      />,
    );

    const aside = screen.getByRole("complementary", { name: "花多少" });
    expect(aside).toHaveTextContent("预估 ¥ 1,800");
    expect(aside).not.toHaveTextContent("另有");
    expect(aside).not.toHaveTextContent("已知");
  });

  test("Dual mode unselected still leaks no amount and no unpriced admission count", () => {
    const estimate = makePartialAdmissionEstimate([
      makePartialAdmissionScenario(),
      makePartialAdmissionScenario({
        scenario_id: "flight_round_trip",
        intercity_mode: "flight",
        label: "机票往返方案",
      }),
    ]);

    const { container } = render(
      <CostEstimateCard costEstimate={estimate} activeScenarioId={null} />,
    );

    expect(screen.getByText("选择交通方式查看预估")).toBeInTheDocument();
    expect(container.textContent).not.toContain("¥");
    expect(container.textContent).not.toContain("另有 2 处门票未计入");
  });
});
