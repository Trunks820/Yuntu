/**
 * Schema 2.0 Trip Cost Estimate (行程消费预估) 前端严格契约定义
 * 对应服务端 v0.9.4 / Schema 2.0 public contract (yuntu_travel_models.py)
 */

export type CostCompleteness = "complete" | "partial" | "unavailable";

export type CostScope = "full_trip" | "estimated_subset" | "unavailable";

export type CostCategoryKey =
  | "intercity_transport"
  | "accommodation"
  | "local_transport"
  | "admission"
  | "meals";

export type CostCoverage = "priced" | "missing";

export type CostPriceBasis = "sourced" | "reference" | "mixed" | "policy_zero" | null;

export type CostScenarioId = "train_round_trip" | "flight_round_trip" | "without_intercity";

export type CostAssumptionCode =
  | "two_travellers_per_room"
  | "itinerary_days_minus_one_nights"
  | "four_travellers_per_taxi"
  | "adult_full_fare"
  | "two_main_meals_per_day"
  | (string & {});

export type CostExclusionCode = "cycling_cost_not_included" | (string & {});

export interface CostMoneyRange {
  min_cny: number;
  max_cny: number;
}

export function formatMoneyRange(range: CostMoneyRange | null | undefined): string {
  if (!range) return "";
  if (range.min_cny === range.max_cny) {
    return `¥${range.min_cny.toLocaleString()}`;
  }
  return `¥${range.min_cny.toLocaleString()}–¥${range.max_cny.toLocaleString()}`;
}

export interface CostCategorySummary {
  category: CostCategoryKey;
  coverage: CostCoverage;
  range: CostMoneyRange | null;
  price_basis: CostPriceBasis;
  basis_label: string;
  /** 门票部分已知时，已知部分的费用区间（老结果缺省） */
  known_range?: CostMoneyRange | null;
  /** 门票部分已知时，已查到价格的景点数（老结果缺省） */
  priced_item_count?: number | null;
  /** 门票部分已知时，未查到价格的景点数（老结果缺省） */
  unpriced_item_count?: number | null;
}

export interface CostScenarioSummary {
  scenario_id: CostScenarioId;
  intercity_mode: "train" | "flight" | null;
  label: string;
  total_scope: CostScope;
  total_range: CostMoneyRange | null;
  /** 门票部分已知时，含已知门票的总价区间（老结果缺省） */
  total_range_with_known_admission?: CostMoneyRange | null;
  /** 门票部分已知时，未计入总价的待确认门票处数（老结果缺省） */
  unpriced_admission_count?: number | null;
  categories: CostCategorySummary[];
  missing_categories: CostCategoryKey[];
}

export interface CostAssumptionSummary {
  code: CostAssumptionCode;
  label: string;
}

export interface CostExclusionSummary {
  code: CostExclusionCode;
  label: string;
}

export interface CostEstimateSummary {
  snapshot_version: "1";
  completeness: CostCompleteness;
  currency: "CNY";
  estimated_at: string;
  scenarios: CostScenarioSummary[];
  assumptions: CostAssumptionSummary[];
  exclusions: CostExclusionSummary[];
  notice: string;
}

export interface ScenarioCostStatus {
  status: "unselected" | "complete" | "partial" | "unavailable";
  costText: string;
  costBadge: string | null;
  unpricedAdmissionNote?: string | null;
  showAmount: boolean;
}

export function formatMoneyPeak(range?: CostMoneyRange | null): string {
  if (!range) return "";
  return `¥ ${range.max_cny.toLocaleString()}`;
}

/**
 * 场景对外展示的总价区间：有含已知门票的总价时优先用它，否则照旧用 total_range。
 */
export function resolveScenarioTotalRange(
  scenario?: CostScenarioSummary | null,
): CostMoneyRange | null {
  if (!scenario) return null;
  return scenario.total_range_with_known_admission ?? scenario.total_range;
}

/**
 * 场景总价后面的未计入门票说明：仅在 unpriced_admission_count > 0 时返回。
 */
export function formatUnpricedAdmissionNote(
  scenario?: CostScenarioSummary | null,
): string | null {
  const count = scenario?.unpriced_admission_count;
  if (typeof count !== "number" || !Number.isFinite(count) || count <= 0) {
    return null;
  }
  return `另有 ${count} 处门票未计入`;
}

/**
 * 门票部分已知时的待确认文案（仅 admission + missing + 有 known_range + unpriced_item_count > 0）。
 * 老结果或其它类别返回 null，调用方照旧展示“待确认”。
 */
export function formatPartialAdmissionText(
  category?: CostCategorySummary | null,
): string | null {
  if (
    !category ||
    category.category !== "admission" ||
    category.coverage !== "missing" ||
    !category.known_range ||
    !Number.isFinite(category.known_range.min_cny) ||
    !Number.isFinite(category.known_range.max_cny) ||
    category.priced_item_count === 0 ||
    typeof category.unpriced_item_count !== "number" ||
    !Number.isFinite(category.unpriced_item_count) ||
    category.unpriced_item_count <= 0
  ) {
    return null;
  }
  const knownText = formatMoneyRange(category.known_range);
  if (!knownText) return null;
  return `已知 ${knownText}，另有 ${category.unpriced_item_count} 处待确认`;
}

/**
 * 统一场景消费状态计算函数
 * 页面 Hero、TripSpine、CostEstimateCard 统一根据当前 activeScenario.total_scope 映射
 * 顶部总额取 max_cny 峰值单数字展示，符合高颜值单数字与预算上限视觉层级
 */
export function getScenarioCostStatus(
  scenario?: CostScenarioSummary | null,
  isUnselected?: boolean,
): ScenarioCostStatus {
  if (isUnselected) {
    return {
      status: "unselected",
      costText: "选择交通方式查看预估",
      costBadge: null,
      unpricedAdmissionNote: null,
      showAmount: false,
    };
  }

  if (!scenario || scenario.total_scope === "unavailable") {
    return {
      status: "unavailable",
      costText: "费用暂不可估算",
      costBadge: null,
      unpricedAdmissionNote: null,
      showAmount: false,
    };
  }

  const effectiveTotalRange = resolveScenarioTotalRange(scenario);
  const unpricedAdmissionNote = formatUnpricedAdmissionNote(scenario);

  if (scenario.total_scope === "full_trip") {
    return {
      status: "complete",
      costText: `预估 ${formatMoneyPeak(effectiveTotalRange)}`,
      costBadge: unpricedAdmissionNote,
      unpricedAdmissionNote,
      showAmount: true,
    };
  }

  return {
    status: "partial",
    costText: `已估 ${formatMoneyPeak(effectiveTotalRange)}`,
    costBadge: unpricedAdmissionNote ?? "部分费用待确认",
    unpricedAdmissionNote,
    showAmount: true,
  };
}
