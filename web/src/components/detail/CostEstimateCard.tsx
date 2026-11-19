import { useEffect, useMemo } from "react";
import {
  getScenarioCostStatus,
  type CostEstimateSummary,
  type CostScenarioSummary,
  type CostCategoryKey,
} from "@/types/cost";
import { Plane, Hotel, Bus, Ticket, Utensils, TrainFront, Info, type LucideIcon } from "lucide-react";

interface CostEstimateCardProps {
  costEstimate?: CostEstimateSummary | null;
  activeScenarioId?: string | null;
  onScenarioSelect?: (scenarioId: string) => void;
  /**
   * A5：电脑和手机共用这一张卡，手机分支（PlanDetailPage 的 MobilePlanView）传 "mobile"。
   * 手机形态只换颜色和字体：取切片 3 落在 :root 的全局变量（--yt-* / --font-*），
   * 不再用 Satoshi 粗体和裸 teal-* / gray-*；结构、间距、顺序两端完全一致。
   * 不传就是电脑形态，电脑端视觉不变。
   */
  variant?: "desktop" | "mobile";
}

const ALL_CATEGORIES: CostCategoryKey[] = [
  "intercity_transport",
  "accommodation",
  "local_transport",
  "admission",
  "meals",
];

/**
 * C7：intercity_transport 就是 travel 说的“大交通”。
 * 依据是 travel 自己给的数据：场景 without_intercity 的 label 是“不含大交通方案”，
 * 该场景里唯独 intercity_transport 是 missing，而 local_transport 另有“市内交通”一行。
 * 原来这行只写“交通”，会被读成“含市内交通”，和上面的“不含大交通”相互打架。
 * 这里改用 travel 自己的用词（本组件 aria-label 也一直叫“大交通出行场景选择”），含义不变；
 * 前端不新增对 travel 口径的解释。
 */
const CATEGORY_META: Record<CostCategoryKey, { label: string; defaultIcon: LucideIcon }> = {
  intercity_transport: { label: "大交通", defaultIcon: Plane },
  accommodation: { label: "住宿", defaultIcon: Hotel },
  local_transport: { label: "市内交通", defaultIcon: Bus },
  admission: { label: "景点门票", defaultIcon: Ticket },
  meals: { label: "餐饮", defaultIcon: Utensils },
};

function formatEstimatedAt(isoString?: string): string | null {
  if (!isoString) return null;
  try {
    const d = new Date(isoString);
    if (Number.isNaN(d.getTime())) return null;
    const year = d.getFullYear();
    const month = String(d.getMonth() + 1).padStart(2, "0");
    const day = String(d.getDate()).padStart(2, "0");
    return `${year}-${month}-${day}`;
  } catch {
    return null;
  }
}

export function CostEstimateCard({
  costEstimate,
  activeScenarioId,
  onScenarioSelect,
  variant = "desktop",
}: CostEstimateCardProps) {
  const isMobileVariant = variant === "mobile";
  /** A5：两端共用同一份结构，只在这里按形态选一套颜色/字体类名。 */
  const tone = (desktop: string, mobile: string) => (isMobileVariant ? mobile : desktop);

  const scenarios = useMemo(() => costEstimate?.scenarios ?? [], [costEstimate]);
  const scenarioIds = scenarios.map((s) => s.scenario_id).join(",");

  const isDualMode = scenarios.length > 1;
  const selectedScenario: CostScenarioSummary | undefined = scenarios.find(
    (s) => s.scenario_id === activeScenarioId,
  );

  // 动态金额条比例计算（以当前场景最高 max_cny 为 100% 基准）
  const maxCategoryMaxCny = useMemo(() => {
    if (!selectedScenario) return 1;
    const maxVals = selectedScenario.categories
      .map((c) => (c.coverage === "priced" ? c.range?.max_cny ?? 0 : 0))
      .filter((v) => v > 0);
    return maxVals.length > 0 ? Math.max(...maxVals) : 1;
  }, [selectedScenario]);

  // 只有一个可用场景时自动激活
  useEffect(() => {
    if (scenarios.length === 1 && !activeScenarioId && onScenarioSelect) {
      onScenarioSelect(scenarios[0].scenario_id);
    }
  }, [scenarioIds, activeScenarioId, onScenarioSelect, scenarios]);

  if (!costEstimate || scenarios.length === 0) {
    return (
      <div
        className={`rounded-2xl border p-6 sm:p-8 shadow-soft space-y-4 scroll-mt-24 ${tone(
          "border-primary-100 bg-white",
          "border-[color:var(--yt-line)] bg-[color:var(--yt-card)]",
        )}`}
      >
        <h2
          className={`text-2xl sm:text-3xl tracking-tight ${tone(
            "font-display font-extrabold text-gray-900",
            "font-[family-name:var(--font-serif)] font-medium text-[color:var(--yt-ink)]",
          )}`}
        >
          预算参考
        </h2>
        <div
          className={`flex items-center gap-2 py-4 text-sm ${tone(
            "text-gray-400",
            "text-[color:var(--yt-muted)]",
          )}`}
        >
          <Info
            size={16}
            className={tone("text-gray-300", "text-[color:var(--yt-muted)]")}
            aria-hidden="true"
          />
          <span>费用暂不可估算</span>
        </div>
      </div>
    );
  }

  const isUnselected = isDualMode && !selectedScenario;
  const statusInfo = getScenarioCostStatus(selectedScenario, isUnselected);
  const estimatedAtText = formatEstimatedAt(costEstimate.estimated_at);

  const peakAmount = selectedScenario?.total_range?.max_cny;

  /**
   * C7：进度条只有在同组有多个金额时才说明得了事。
   * 支出项只有一个（或只有一个有金额）时不画图，直接显示这一行。
   * 判断只用组件里已有的数据，与上面画条的条件保持一致，不新增服务端字段。
   */
  const pricedCategoryCount = selectedScenario
    ? selectedScenario.categories.filter(
        (c) => c.coverage === "priced" && c.range && c.price_basis !== "policy_zero",
      ).length
    : 0;
  const showCategoryBars = pricedCategoryCount > 1;

  const scenarioActiveClass = tone(
    "bg-white text-gray-900 shadow-xs",
    "bg-[color:var(--yt-card)] text-[color:var(--yt-ink)] shadow-xs",
  );
  const scenarioIdleClass = tone(
    "text-gray-500 hover:text-gray-800",
    "text-[color:var(--yt-muted)] hover:text-[color:var(--yt-ink)]",
  );

  return (
    <div
      className={`rounded-2xl border p-6 sm:p-8 shadow-soft space-y-6 scroll-mt-24 ${tone(
        "border-primary-100 bg-white",
        "border-[color:var(--yt-line)] bg-[color:var(--yt-card)]",
      )}`}
    >
      {/* 1. 顶栏：标题 + Segmented Control 场景切换器 */}
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <h2
          className={`text-2xl sm:text-3xl tracking-tight ${tone(
            "font-display font-bold text-gray-900",
            "font-[family-name:var(--font-serif)] font-medium text-[color:var(--yt-ink)]",
          )}`}
        >
          预算参考
        </h2>

        {/* 双场景 Segmented Control */}
        {isDualMode && (
          <div
            className={`inline-flex items-center gap-1 rounded-xl p-1 border shrink-0 self-start sm:self-auto ${tone(
              "bg-gray-100/90 border-gray-200/50",
              "bg-[color:var(--yt-paper-soft)] border-[color:var(--yt-line)]",
            )}`}
            role="radiogroup"
            aria-label="大交通出行场景选择"
          >
            {scenarios.map((sc) => {
              const active = sc.scenario_id === activeScenarioId;
              return (
                <button
                  key={sc.scenario_id}
                  type="button"
                  role="radio"
                  aria-checked={active}
                  onClick={() => onScenarioSelect?.(sc.scenario_id)}
                  onKeyDown={(e) => {
                    if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
                      e.preventDefault();
                      const currentIdx = scenarios.findIndex((s) => s.scenario_id === sc.scenario_id);
                      const nextIdx = e.key === "ArrowRight"
                        ? (currentIdx + 1) % scenarios.length
                        : (currentIdx - 1 + scenarios.length) % scenarios.length;
                      onScenarioSelect?.(scenarios[nextIdx].scenario_id);
                    }
                  }}
                  className={`px-3.5 py-1.5 text-xs font-bold rounded-lg transition-all focus-visible:outline-none focus-visible:ring-2 ${tone(
                    "focus-visible:ring-teal-400",
                    "focus-visible:ring-[color:var(--yt-green)]",
                  )} ${active ? scenarioActiveClass : scenarioIdleClass}`}
                >
                  {sc.label}
                </button>
              );
            })}
          </div>
        )}
      </div>

      {/* 2. 主数字区：与交通卡统一美观结构 */}
      <div>
        {/* 行1：场景与预估范围状态 */}
        <div
          className={`text-xs font-normal mb-1 ${tone(
            "text-gray-400",
            "text-[color:var(--yt-muted)]",
          )}`}
        >
          {isUnselected ? (
            <span>选择大交通出行方案以查看预估</span>
          ) : selectedScenario ? (
            <>
              <span>{selectedScenario.label}</span>
              <span className={`mx-1 ${tone("text-gray-300", "text-[color:var(--yt-line)]")}`}>
                ·
              </span>
              {/* C7：范围说明只保留这一处（部分预估 / 完整预估），不再重复“部分费用待确认” */}
              <span
                className={`font-medium ${tone(
                  selectedScenario.total_scope === "full_trip"
                    ? "text-teal-600"
                    : "text-amber-600",
                  selectedScenario.total_scope === "full_trip"
                    ? "text-[color:var(--yt-green)]"
                    : "text-[color:var(--yt-warning)]",
                )}`}
              >
                {selectedScenario.total_scope === "full_trip" ? "完整预估" : "部分预估"}
              </span>
            </>
          ) : (
            <span>缺少有效价格来源</span>
          )}
        </div>

        {/* 行2：精致大数字 (符号小灰字 + 48px 纤细大数字) */}
        <div>
          {statusInfo.status === "unselected" ? (
            <div
              className={`py-2 text-base font-bold ${tone(
                "text-gray-600",
                "text-[color:var(--yt-ink)]",
              )}`}
            >
              选择交通方式查看预估
            </div>
          ) : statusInfo.status === "unavailable" ? (
            <div
              className={`py-2 text-xl font-bold ${tone(
                "text-gray-800",
                "text-[color:var(--yt-ink)]",
              )}`}
            >
              费用暂不可估算
            </div>
          ) : (
            <div className="flex flex-wrap items-baseline gap-1 my-1">
              <span
                className={`text-xl sm:text-2xl font-light mr-1.5 select-none ${tone(
                  "text-gray-400",
                  "text-[color:var(--yt-muted)]",
                )}`}
              >
                ¥
              </span>
              <span
                className={`text-4xl sm:text-5xl tracking-tight tabular-nums whitespace-nowrap ${tone(
                  "font-display font-light text-gray-900",
                  "font-[family-name:var(--font-serif)] font-normal text-[color:var(--yt-ink)]",
                )}`}
              >
                {peakAmount != null ? peakAmount.toLocaleString() : "0"}
              </span>
            </div>
          )}
        </div>

        {/* 行3：预估日期或补充说明 */}
        {estimatedAtText && (
          <div
            className={`text-xs font-normal mt-1 ${tone(
              "text-gray-400",
              "text-[color:var(--yt-muted)]",
            )}`}
          >
            预估于 {estimatedAtText}
          </div>
        )}
      </div>

      {/* 3. 五类横向明细：像素级复刻参考图（直投 Icon -> 标签 -> 细线条 -> 右侧金额） */}
      {selectedScenario && (
        <div
          className={`pt-2 divide-y ${tone(
            "divide-gray-100/80",
            "divide-[color:var(--yt-line)]",
          )}`}
        >
          {ALL_CATEGORIES.map((catKey) => {
            const cat = selectedScenario.categories.find((c) => c.category === catKey);
            const meta = CATEGORY_META[catKey];
            const isTrain = selectedScenario.intercity_mode === "train";
            const CatIcon: LucideIcon =
              catKey === "intercity_transport"
                ? isTrain ? TrainFront : Plane
                : meta.defaultIcon;

            const isPolicyZero = cat?.price_basis === "policy_zero";
            const isPriced = cat?.coverage === "priced" && cat.range && !isPolicyZero;
            const amountVal = isPriced && cat.range ? cat.range.max_cny : null;

            // 4px 细进度条相对比例计算
            const barPercent = isPriced && cat.range
              ? Math.min(100, Math.max(6, Math.round((cat.range.max_cny / maxCategoryMaxCny) * 100)))
              : 0;

            const priceBasisText =
              cat?.price_basis === "sourced"
                ? "来源查价"
                : cat?.price_basis === "reference"
                  ? "参考估算"
                  : cat?.price_basis === "mixed"
                    ? "来源与参考混合"
                    : cat?.price_basis === "policy_zero"
                      ? "按规则计为 ¥0"
                      : null;

            return (
              <div
                key={catKey}
                className="py-3.5 flex items-center justify-between gap-4 sm:gap-8"
              >
                {/* 左侧：直投图标 + 分类 Label */}
                <div className="flex items-center gap-3 shrink-0 w-28 sm:w-32">
                  <CatIcon
                    size={18}
                    className={`shrink-0 ${tone(
                      "text-teal-600",
                      "text-[color:var(--yt-green)]",
                    )}`}
                    aria-hidden="true"
                  />
                  <span
                    className={`text-sm font-medium ${tone(
                      "text-gray-700",
                      "text-[color:var(--yt-ink)]",
                    )}`}
                  >
                    {meta.label}
                  </span>
                </div>

                {/* 中间：4px 细线条进度条（C7：只有一个金额时不画图，这一格整个不渲染） */}
                {showCategoryBars && (
                  <div className="flex-1 mx-2 sm:mx-6" data-testid="cost-category-bar">
                    {isPriced ? (
                      <div
                        className={`h-1 w-full rounded-full overflow-hidden ${tone(
                          "bg-gray-100/80",
                          "bg-[color:var(--yt-line)]",
                        )}`}
                      >
                        <div
                          className={`h-full rounded-full transition-all duration-300 ${tone(
                            "bg-teal-600",
                            "bg-[color:var(--yt-green)]",
                          )}`}
                          style={{ width: `${barPercent}%` }}
                        />
                      </div>
                    ) : (
                      <div
                        className={`h-1 w-full rounded-full border border-dashed ${tone(
                          "border-gray-200 bg-gray-50/50",
                          "border-[color:var(--yt-line)] bg-[color:var(--yt-paper-soft)]",
                        )}`}
                      />
                    )}
                  </div>
                )}

                {/* 右侧：金额 (复刻参考图单行右对齐) */}
                <div className="text-right shrink-0 min-w-[80px]">
                  {isPriced && amountVal != null ? (
                    <div
                      className={`text-sm font-medium tabular-nums ${tone(
                        "text-gray-800",
                        "text-[color:var(--yt-ink)]",
                      )}`}
                    >
                      ¥ {amountVal.toLocaleString()}
                    </div>
                  ) : isPolicyZero ? (
                    <div
                      className={`text-sm font-medium tabular-nums ${tone(
                        "text-gray-800",
                        "text-[color:var(--yt-ink)]",
                      )}`}
                    >
                      ¥ 0
                    </div>
                  ) : (
                    <span
                      className={`inline-block rounded px-2 py-0.5 text-xs font-medium ${tone(
                        "bg-gray-100 text-gray-400",
                        "bg-[color:var(--yt-paper-soft)] text-[color:var(--yt-muted)]",
                      )}`}
                    >
                      待确认
                    </span>
                  )}
                  {priceBasisText && (
                    <div
                      className={`text-[10px] mt-0.5 hidden sm:block ${tone(
                        "text-gray-400",
                        "text-[color:var(--yt-muted)]",
                      )}`}
                    >
                      {priceBasisText}
                    </div>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      )}

      {/* 4. 页脚：极简提示文字 (复刻参考图底部小字)；notice 以 travel 给的为准，不覆盖 */}
      <div
        className={`pt-2 text-xs font-normal border-t ${tone(
          "text-gray-400 border-gray-100/80",
          "text-[color:var(--yt-muted)] border-[color:var(--yt-line)]",
        )}`}
      >
        <p>{costEstimate.notice || "费用为规划参考，实际花费以出行行为准"}</p>
      </div>
    </div>
  );
}
