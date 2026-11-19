import { useEffect, useMemo, useRef, useState } from "react";
import { MapPin, Plane, TrainFront, TriangleAlert, X } from "lucide-react";
import { getScenarioCostStatus, type CostEstimateSummary } from "@/types/cost";
import type { AccommodationInfo, TripPlan, TripResult } from "@/types/trip";
import {
  filterPackingChecklist,
  filterTravelTips,
} from "@/utils/preTripAdvice";
import {
  filterUnscheduledMustInclude,
  formatCostEstimatedDate,
  formatMustIncludeStatusSuffix,
  formatTransportDuration,
  getLatestTransportCheckedTime,
  hasValidAccommodationCoords,
  pickDefaultCostScenarioId,
  resolveBeforeWeatherSection,
  resolveBookCostCategoryRows,
  resolveBookLodgingPresentation,
  resolveTransportFooterNote,
  type BookSidePageId,
} from "@/utils/spreadBook";

type DrawerPageId = Exclude<BookSidePageId, "guide">;

const PAGE_LABELS: Record<DrawerPageId, string> = {
  before: "出发前",
  go: "怎么去 · 住哪",
  cost: "花多少",
  miss: "想去但没排进来的",
  stay: "住处",
};

export interface BookPagesProps {
  page: DrawerPageId;
  result: TripResult;
  plan: TripPlan;
  onClose: () => void;
  onLocateStay: () => void;
  disableEsc?: boolean;
}

function BeforeSection({
  result,
  plan,
}: {
  result: TripResult;
  plan: TripPlan;
}) {
  const weatherSection = resolveBeforeWeatherSection(plan.days, result.weather);
  const packingGroups = useMemo(
    () => filterPackingChecklist(plan.packing_checklist),
    [plan.packing_checklist],
  );
  const travelTips = useMemo(
    () => filterTravelTips(plan.travel_tips),
    [plan.travel_tips],
  );

  return (
    <div className="bk-pg">
      <h2>出发前</h2>

      <div className="bk-ga-block">
        <h3>天气</h3>
        {weatherSection.ok ? (
          <div className="bk-gws">
            {weatherSection.items.map((item) =>
              item.kind === "missing" ? (
                <div className="bk-gw is-none" key={`wx-day-${item.day}`}>
                  <p>
                    <b>{item.title}</b>
                  </p>
                </div>
              ) : (
                <div className="bk-gw" key={`wx-day-${item.day}`}>
                  <span className="bk-gw-i" aria-hidden="true">
                    <item.Icon size={20} strokeWidth={1.7} />
                  </span>
                  <p>
                    <b>{item.title}</b>
                    <span>
                      {item.weatherText} {item.tempRange}
                      {item.windText ? ` · ${item.windText}` : ""}
                    </span>
                  </p>
                  {item.reminders.map((rem, idx) => (
                    <small key={`wx-${item.day}-rem-${idx}`}>{rem}</small>
                  ))}
                </div>
              ),
            )}
          </div>
        ) : (
          <div className="bk-gw is-none" role="status">
            <p>{weatherSection.hint}</p>
          </div>
        )}
      </div>

      {packingGroups.length > 0 && (
        <div className="bk-ga-block">
          <h3>带上这些</h3>
          <div className="bk-gpk">
            {packingGroups.map((group) => (
              <p key={group.category}>
                <b>{group.category}</b>
                <span>{group.items.join("、")}</span>
              </p>
            ))}
          </div>
        </div>
      )}

      {travelTips.length > 0 && (
        <div className="bk-ga-block">
          <h3>小提示</h3>
          <ul className="bk-gtips">
            {travelTips.map((tip, idx) => (
              <li key={`${tip.title}-${idx}`}>
                <b>{tip.title}</b>
                <span>{tip.content}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

function LodgingBlockContent({
  accommodation,
  showMapLink,
  onLocateStay,
}: {
  accommodation: AccommodationInfo;
  showMapLink: boolean;
  onLocateStay: () => void;
}) {
  const presentation = resolveBookLodgingPresentation(accommodation);
  if (!presentation) return null;

  const canLocate = showMapLink && hasValidAccommodationCoords(accommodation);

  if (presentation.kind === "legacy") {
    return (
      <>
        <p className="bk-ga-p">
          <b>{presentation.title}</b>
          {presentation.reason ? (
            <>
              <br />
              <span>{presentation.reason}</span>
            </>
          ) : null}
        </p>
        {canLocate && (
          <button type="button" className="bk-show-stay" onClick={onLocateStay}>
            <MapPin size={14} aria-hidden="true" />
            在地图上看住处
          </button>
        )}
      </>
    );
  }

  if (presentation.kind === "user_lodging") {
    return (
      <>
        <p className="bk-ga-p">
          <b>{presentation.text}</b>
        </p>
        {canLocate && (
          <button type="button" className="bk-show-stay" onClick={onLocateStay}>
            <MapPin size={14} aria-hidden="true" />
            在地图上看住处
          </button>
        )}
      </>
    );
  }

  return (
    <>
      {presentation.unmatchedNotices.map((notice, idx) => (
        <p className="bk-lodging-unmatched" key={`unmatched-${idx}`}>
          {notice.text}
        </p>
      ))}
      <p className="bk-ga-p">
        <b>{presentation.areaLabel}</b>
        {presentation.reason ? (
          <>
            <br />
            <span>{presentation.reason}</span>
          </>
        ) : null}
      </p>
      {presentation.caption && (
        <p className="bk-ga-caption">{presentation.caption}</p>
      )}
      {presentation.tiers.length > 0 && (
        <div className="bk-gtier">
          {presentation.tiers.map((tier, idx) => (
            <div className="bk-gtier-item" key={`${tier.kind}-${idx}`}>
              <b>{tier.label}</b>
              {tier.priceText && (
                <span className="bk-gtier-price">{tier.priceText}</span>
              )}
              {tier.hotelsText && (
                <span className="bk-gtier-hotels">{tier.hotelsText}</span>
              )}
            </div>
          ))}
        </div>
      )}
      {presentation.holidayNotices.map((notice, idx) => (
        <p className="bk-lodging-holiday" key={`holiday-${idx}`}>
          {notice.text}
        </p>
      ))}
      {presentation.showPriceNote && (
        <p className="bk-ga-fine">参考价，实际以预订时为准</p>
      )}
      {canLocate && (
        <button type="button" className="bk-show-stay" onClick={onLocateStay}>
          <MapPin size={14} aria-hidden="true" />
          在地图上看住处
        </button>
      )}
    </>
  );
}

function GoSection({
  plan,
  onLocateStay,
}: {
  plan: TripPlan;
  onLocateStay: () => void;
}) {
  const transport =
    plan.transport && plan.transport.modes && plan.transport.modes.length > 0
      ? plan.transport
      : null;
  const accommodation = plan.accommodation ?? null;

  if (!transport && !accommodation) {
    return (
      <div className="bk-pg">
        <h2>怎么去 · 住哪</h2>
        <p className="bk-ga-empty">这一册没有大交通和住宿信息。</p>
      </div>
    );
  }

  const latestChecked = transport
    ? getLatestTransportCheckedTime(transport)
    : null;

  return (
    <div className="bk-pg">
      <h2>怎么去 · 住哪</h2>

      {transport && (
        <div className="bk-ga-block">
          <h3>怎么去</h3>
          <p className="bk-ga-fine">
            {transport.from_city} → {transport.to_city}
            {transport.query_date ? ` · 出行日期 ${transport.query_date}` : ""}
          </p>

          <div className="bk-tr-modes">
            {transport.modes.map((mode, modeIdx) => {
              const isFlight = mode.mode === "flight";
              const ModeIcon = isFlight ? Plane : TrainFront;
              const modeTitle = isFlight ? "飞机" : "高铁";
              const showStaticBadge =
                mode.price_source === "static_reference" &&
                !mode.price_range.includes("参考");

              return (
                <div className="bk-tr-mode" key={`${mode.mode}-${modeIdx}`}>
                  <div className="bk-tr-mode-h">
                    <span className="bk-tr-mode-name">
                      <ModeIcon size={14} aria-hidden="true" />
                      <b>{modeTitle}</b>
                    </span>
                    <span className="bk-tr-mode-meta">
                      最快 {formatTransportDuration(mode.min_duration_minutes)}
                    </span>
                    <span className="bk-tr-mode-price">{mode.price_range}</span>
                    {showStaticBadge && <span className="bk-tr-ref">参考</span>}
                    {mode.daily_count > 0 && (
                      <span className="bk-tr-mode-meta">
                        每日约 {mode.daily_count} 班
                      </span>
                    )}
                  </div>

                  {mode.availability_status === "sold_out_at_query" && (
                    <p className="bk-tr-soldout" role="status">
                      <TriangleAlert size={13} aria-hidden="true" />
                      <span>查询时段车票已售罄，请以购票平台实时余票为准</span>
                    </p>
                  )}

                  {mode.options && mode.options.length > 0 && (
                    <ul className="bk-gtr">
                      {mode.options.map((opt, optIdx) => {
                        const titleText =
                          opt.type === "flight" && opt.airline
                            ? `${opt.airline} ${opt.no}`
                            : opt.no;
                        return (
                          <li key={`${opt.no}-${optIdx}`}>
                            <div className="bk-gtr-top">
                              <span className="bk-gtr-no">{titleText}</span>
                              <span className="bk-gtr-dur">
                                {formatTransportDuration(opt.duration_minutes)}
                                {opt.price ? (
                                  <>
                                    {" · "}
                                    <em>{opt.price}</em>
                                  </>
                                ) : null}
                              </span>
                            </div>
                            <div className="bk-gtr-route">
                              <b>
                                {opt.departure_time}{" "}
                                <small>{opt.departure_station}</small>
                              </b>
                              <i aria-hidden="true" />
                              <b>
                                {opt.arrival_time}{" "}
                                <small>{opt.arrival_station}</small>
                              </b>
                            </div>
                          </li>
                        );
                      })}
                    </ul>
                  )}
                </div>
              );
            })}
          </div>

          <p className="bk-ga-fine">
            {resolveTransportFooterNote(transport)}
            {latestChecked ? ` · 余票查询于 ${latestChecked}` : ""}
          </p>
        </div>
      )}

      {accommodation && (
        <div className="bk-ga-block">
          <h3>住哪</h3>
          <LodgingBlockContent
            accommodation={accommodation}
            showMapLink
            onLocateStay={onLocateStay}
          />
        </div>
      )}
    </div>
  );
}

function CostSection({
  costEstimate,
  planId,
  peopleCount,
}: {
  costEstimate: CostEstimateSummary | null | undefined;
  planId: string;
  peopleCount: number;
}) {
  const scenarios = useMemo(
    () => costEstimate?.scenarios ?? [],
    [costEstimate],
  );
  const scenarioKey = scenarios.map((s) => s.scenario_id).join(",");

  const [selectedScenarioId, setSelectedScenarioId] = useState<string | null>(
    () => pickDefaultCostScenarioId(scenarios),
  );

  useEffect(() => {
    setSelectedScenarioId(pickDefaultCostScenarioId(scenarios));
  }, [planId, scenarioKey, scenarios]);

  if (!costEstimate || scenarios.length === 0) {
    return (
      <div className="bk-pg">
        <h2>花多少</h2>
        <div className="bk-ga-block">
          <p className="bk-ga-empty">费用暂不可估算</p>
        </div>
      </div>
    );
  }

  const activeScenario =
    scenarios.find((s) => s.scenario_id === selectedScenarioId) ?? scenarios[0];
  const costStatus = getScenarioCostStatus(activeScenario, false);
  const categoryRows = resolveBookCostCategoryRows(activeScenario);
  const noteLabels = [
    ...(costEstimate.assumptions ?? []).map((a) => a.label),
    ...(costEstimate.exclusions ?? []).map((e) => e.label),
  ]
    .map((s) => s.trim())
    .filter(Boolean);
  const estimatedDate = formatCostEstimatedDate(costEstimate.estimated_at);

  return (
    <div className="bk-pg">
      <h2>花多少</h2>
      <div className="bk-ga-block">
        {scenarios.length > 1 && (
          <div
            className="bk-cost-tabs"
            role="radiogroup"
            aria-label="大交通出行场景选择"
          >
            {scenarios.map((sc) => {
              const active = sc.scenario_id === activeScenario.scenario_id;
              return (
                <button
                  key={sc.scenario_id}
                  type="button"
                  role="radio"
                  aria-checked={active}
                  onClick={() => setSelectedScenarioId(sc.scenario_id)}
                >
                  {sc.label}
                </button>
              );
            })}
          </div>
        )}

        <div className="bk-ga-p">
          <span>
            {activeScenario.label} · 全程 · {peopleCount} 人
          </span>
          <div className="bk-gcost-row">
            <b className="bk-gcost">{costStatus.costText}</b>
            {costStatus.costBadge && (
              <span className="bk-cost-badge">{costStatus.costBadge}</span>
            )}
          </div>
        </div>

        <ul className="bk-gled">
          {categoryRows.map((row) => (
            <li key={row.key}>
              <div className="bk-gled-main">
                <b>{row.label}</b>
                <i aria-hidden="true" />
                <em className={row.isUnpriced ? "is-unpriced" : undefined}>
                  {row.amountText}
                </em>
              </div>
              {row.basisLabel && (
                <small className="bk-gled-basis">{row.basisLabel}</small>
              )}
            </li>
          ))}
        </ul>

        {noteLabels.length > 0 && (
          <p className="bk-ga-fine">{noteLabels.join(" · ")}</p>
        )}
        {estimatedDate && <p className="bk-ga-fine">预估于 {estimatedDate}</p>}
        <p className="bk-ga-fine">
          {costEstimate.notice || "费用为规划参考，实际花费以出行行为准"}
        </p>
      </div>
    </div>
  );
}

function MissSection({ result }: { result: TripResult }) {
  const unscheduled = useMemo(
    () => filterUnscheduledMustInclude(result.must_include),
    [result.must_include],
  );

  return (
    <div className="bk-pg">
      <h2>想去但没排进来的</h2>
      {unscheduled.length > 0 ? (
        <ul className="bk-gmiss">
          {unscheduled.map((item, idx) => {
            const suffix = formatMustIncludeStatusSuffix(item.status);
            const key =
              item.place_id != null
                ? `${item.place_id}-${idx}`
                : `${item.name}-${idx}`;
            return (
              <li key={key}>
                <b>
                  {item.name}
                  {suffix}
                </b>
                {item.reason && <span>{item.reason}</span>}
              </li>
            );
          })}
        </ul>
      ) : (
        <p className="bk-ga-empty">想去的地点都排进来了。</p>
      )}
    </div>
  );
}

function StaySection({
  plan,
  onLocateStay,
}: {
  plan: TripPlan;
  onLocateStay: () => void;
}) {
  return (
    <div className="bk-pg">
      <h2>住处</h2>
      <div className="bk-ga-block">
        {plan.accommodation ? (
          <LodgingBlockContent
            accommodation={plan.accommodation}
            showMapLink={false}
            onLocateStay={onLocateStay}
          />
        ) : (
          <p className="bk-ga-empty">这一册没有住宿信息。</p>
        )}
      </div>
    </div>
  );
}

export function BookPages({
  page,
  result,
  plan,
  onClose,
  onLocateStay,
  disableEsc = false,
}: BookPagesProps) {
  const asideRef = useRef<HTMLElement>(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;
  const disableEscRef = useRef(disableEsc);
  disableEscRef.current = disableEsc;

  useEffect(() => {
    if (asideRef.current) {
      asideRef.current.scrollTop = 0;
    }
  }, [page]);

  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !disableEscRef.current) {
        onCloseRef.current();
      }
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, []);

  return (
    <aside
      ref={asideRef}
      className="bk-float bk-side"
      aria-label={PAGE_LABELS[page]}
    >
      <button
        type="button"
        className="bk-side-x"
        aria-label="关上"
        onClick={onClose}
      >
        <X size={16} aria-hidden="true" />
      </button>

      {page === "before" && <BeforeSection result={result} plan={plan} />}
      {page === "go" && <GoSection plan={plan} onLocateStay={onLocateStay} />}
      {page === "cost" && (
        <CostSection
          costEstimate={plan.cost_estimate}
          planId={plan.plan_id}
          peopleCount={result.request.people_count}
        />
      )}
      {page === "miss" && <MissSection result={result} />}
      {page === "stay" && (
        <StaySection plan={plan} onLocateStay={onLocateStay} />
      )}
    </aside>
  );
}
