import {
  Fragment,
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import type { LucideIcon } from "lucide-react";
import {
  Bus,
  ChevronDown,
  ChevronUp,
  Footprints,
  Route,
  TrainFront,
} from "lucide-react";
import type {
  PlaceDetail,
  TransitStep,
  TripDay,
  TripWeather,
} from "@/types/trip";
import { commuteModeIcon, commuteModeName } from "@/utils/format";
import {
  findCommuteLeg,
  formatBookDayLabel,
  formatDayWeatherBadge,
  formatStayMinutes,
  formatTransitStepLine,
  getDayWeather,
  isReducedMotionPreferred,
  resolveCommutePopup,
  resolveStopCardNote,
  resolveStopKind,
  resolveStopScheduleMeta,
  sortPlaceGallery,
  STOP_KIND_LABEL,
  type CommutePopupContent,
} from "@/utils/spreadBook";
import { StopKindMark } from "./StopKindMark";

export type PlaceDetailEntry =
  | { status: "loading" }
  | { status: "ready"; data: PlaceDetail }
  // 后端明确说这个地点没有详情：不是故障，也不给重试
  | { status: "unsupported" }
  | { status: "error" };

const STEP_KIND_ICON: Record<TransitStep["kind"], LucideIcon> = {
  walking: Footprints,
  bus: Bus,
  rail: TrainFront,
  other: Route,
};

interface ActiveLegPopup {
  key: string;
  popupId: string;
  content: Exclude<CommutePopupContent, null>;
  left: number;
  bottom: number;
}

export interface DayStripProps {
  days: TripDay[];
  currentDay: TripDay;
  selectedPlaceId: number | null;
  collapsed?: boolean;
  folded?: boolean;
  weather: TripWeather | null | undefined;
  flashingWeatherDay?: number | null;
  placeDetails: Record<number, PlaceDetailEntry | undefined>;
  brokenImageUrls: ReadonlySet<string>;
  onImageError: (url: string) => void;
  onSelectDay: (dayNumber: number) => void;
  onSelectPlace: (placeId: number) => void;
  onToggleFold?: () => void;
  registerCardRef?: (placeId: number, el: HTMLButtonElement | null) => void;
}

export function DayStrip({
  days,
  currentDay,
  selectedPlaceId,
  collapsed = false,
  folded = false,
  weather,
  flashingWeatherDay = null,
  placeDetails,
  brokenImageUrls,
  onImageError,
  onSelectDay,
  onSelectPlace,
  onToggleFold,
  registerCardRef,
}: DayStripProps) {
  const sectionRef = useRef<HTMLElement>(null);
  const tabsRef = useRef<HTMLDivElement>(null);
  const cardsRef = useRef<HTMLDivElement>(null);
  const currentDayNumRef = useRef(currentDay.day);
  currentDayNumRef.current = currentDay.day;
  const hiddenRef = useRef(collapsed || folded);
  hiddenRef.current = collapsed || folded;
  const [activeLegPopup, setActiveLegPopup] = useState<ActiveLegPopup | null>(
    null,
  );

  // 长卷首次出现和换天时，卡片依次从下方升起（单张 320ms，总长不超过 600ms，不过冲）；
  // 点卡片等普通重绘不重播，收着时不播，减少动态时不播
  useLayoutEffect(() => {
    const box = cardsRef.current;
    if (!box || hiddenRef.current || isReducedMotionPreferred()) return;
    const cards = Array.from(box.children) as HTMLElement[];
    const step = cards.length > 1 ? Math.min(50, 240 / (cards.length - 1)) : 0;
    cards.forEach((el, idx) => {
      el.animate?.(
        [
          { opacity: 0, transform: "translateY(14px)" },
          { opacity: 1, transform: "none" },
        ],
        {
          duration: 320,
          delay: Math.round(idx * step),
          easing: "cubic-bezier(0.22, 1, 0.36, 1)",
          fill: "backwards",
        },
      );
    });
  }, [currentDay.day]);

  useEffect(() => {
    setActiveLegPopup(null);
  }, [currentDay.day, collapsed, folded]);

  useEffect(() => {
    const el = sectionRef.current;
    if (!el) return;
    (el as HTMLElement & { inert: boolean }).inert = collapsed;
    if (collapsed) {
      el.setAttribute("inert", "");
    } else {
      el.removeAttribute("inert");
    }
  }, [collapsed]);

  // 页签放不下时（多天或打开详情长卷收窄），把选中的那一天横向滚进视野
  const ensureActiveTabVisible = useCallback((smooth: boolean) => {
    const container = tabsRef.current;
    if (!container) return;
    const activeTab = container.querySelector<HTMLElement>(
      `.bk-dtab[data-day="${currentDayNumRef.current}"]`,
    );
    if (!activeTab || container.scrollWidth <= container.clientWidth) return;
    const tabLeft = activeTab.offsetLeft - container.offsetLeft;
    const tabRight = tabLeft + activeTab.offsetWidth;
    const viewLeft = container.scrollLeft;
    const viewRight = viewLeft + container.clientWidth;
    const behavior: ScrollBehavior =
      smooth && !isReducedMotionPreferred() ? "smooth" : "auto";
    if (tabLeft < viewLeft + 8) {
      const nextLeft = Math.max(0, tabLeft - 8);
      if (typeof container.scrollTo === "function") {
        container.scrollTo({ left: nextLeft, behavior });
      } else {
        container.scrollLeft = nextLeft;
      }
    } else if (tabRight > viewRight - 8) {
      const nextLeft = Math.max(0, tabRight - container.clientWidth + 8);
      if (typeof container.scrollTo === "function") {
        container.scrollTo({ left: nextLeft, behavior });
      } else {
        container.scrollLeft = nextLeft;
      }
    }
  }, []);

  useEffect(() => {
    if (collapsed) return;
    ensureActiveTabVisible(true);
  }, [
    currentDay.day,
    collapsed,
    folded,
    selectedPlaceId,
    ensureActiveTabVisible,
  ]);

  useEffect(() => {
    const container = tabsRef.current;
    if (!container) return;
    const handleResize = () => ensureActiveTabVisible(false);
    window.addEventListener("resize", handleResize);
    if (typeof ResizeObserver === "undefined") {
      return () => {
        window.removeEventListener("resize", handleResize);
      };
    }
    const ro = new ResizeObserver(handleResize);
    ro.observe(container);
    return () => {
      ro.disconnect();
      window.removeEventListener("resize", handleResize);
    };
  }, [ensureActiveTabVisible]);

  const showPopupForTrigger = (
    key: string,
    popupId: string,
    content: Exclude<CommutePopupContent, null>,
    triggerEl: HTMLButtonElement,
  ) => {
    const rect = triggerEl.getBoundingClientRect();
    const hasLayout =
      typeof window !== "undefined" &&
      (rect.width > 0 || rect.height > 0 || rect.top > 0);
    if (!hasLayout) {
      setActiveLegPopup({
        key,
        popupId,
        content,
        left: 20,
        bottom: 292,
      });
      return;
    }
    const popupWidth = 264;
    const centerX = rect.left + rect.width / 2;
    const maxLeft = Math.max(20, window.innerWidth - popupWidth - 20);
    const left = Math.round(
      Math.max(20, Math.min(maxLeft, centerX - popupWidth / 2)),
    );
    const bottom = Math.round(Math.max(16, window.innerHeight - rect.top + 8));
    setActiveLegPopup({
      key,
      popupId,
      content,
      left,
      bottom,
    });
  };

  const hidePopupIfKey = (key: string) => {
    setActiveLegPopup((prev) => (prev?.key === key ? null : prev));
  };

  const summaryTooltip = [currentDay.narrative, currentDay.commute_summary]
    .filter(Boolean)
    .join("　");

  return (
    <>
      <section
        ref={sectionRef}
        className={[
          "bk-float",
          "bk-day",
          collapsed ? "is-collapsed" : "",
          folded ? "is-folded" : "",
        ]
          .filter(Boolean)
          .join(" ")}
        aria-label="这一天"
        aria-hidden={collapsed || undefined}
      >
        <header className="bk-dh">
          <div
            ref={tabsRef}
            className="bk-dtabs"
            role="group"
            aria-label="切换行程天"
          >
            {days.map((d) => {
              const weatherDay = getDayWeather(weather, d.day);
              const badge = formatDayWeatherBadge(weatherDay);
              const active = d.day === currentDay.day;
              return (
                <button
                  key={d.day}
                  type="button"
                  className="bk-dtab"
                  data-day={d.day}
                  aria-pressed={active}
                  onClick={() => onSelectDay(d.day)}
                >
                  <b>{formatBookDayLabel(d.day)}</b>
                  {badge && (
                    <span
                      className={
                        flashingWeatherDay === d.day ? "bk-wx is-got" : "bk-wx"
                      }
                    >
                      <badge.Icon
                        size={15}
                        className="bk-wx-icon"
                        aria-hidden="true"
                      />
                      <span>
                        {badge.weatherText} {badge.tempRange}
                      </span>
                    </span>
                  )}
                </button>
              );
            })}
          </div>

          <div className="bk-dh-t">
            <h2>{currentDay.title}</h2>
            {(currentDay.narrative || currentDay.commute_summary) && (
              <p title={summaryTooltip}>
                {currentDay.narrative}
                {currentDay.commute_summary ? (
                  <small>
                    {currentDay.narrative ? "\u3000" : ""}
                    {currentDay.commute_summary}
                  </small>
                ) : null}
              </p>
            )}
          </div>

          {onToggleFold && (
            <button
              type="button"
              className="bk-fold"
              aria-expanded={!folded}
              aria-label={folded ? "展开这一天" : "收起这一天"}
              onClick={onToggleFold}
            >
              {folded ? (
                <ChevronUp size={18} aria-hidden="true" />
              ) : (
                <ChevronDown size={18} aria-hidden="true" />
              )}
            </button>
          )}
        </header>

        <div
          ref={cardsRef}
          className="bk-dcards"
          aria-hidden={folded || undefined}
          onScroll={() => {
            if (activeLegPopup) setActiveLegPopup(null);
          }}
        >
          {currentDay.places.map((place, idx) => {
            const prevPlace = idx > 0 ? currentDay.places[idx - 1] : null;
            const leg = prevPlace
              ? findCommuteLeg(currentDay, prevPlace.place_id, place.place_id)
              : undefined;
            const legKey = prevPlace
              ? `${currentDay.day}-${prevPlace.place_id}-${place.place_id}-${idx}`
              : "";
            const popupId = `bk-leg-pop-${legKey}`;
            const legPopup = leg ? resolveCommutePopup(leg) : null;
            const ModeIcon = leg ? commuteModeIcon(leg.mode) : null;
            const modeLabel = leg ? commuteModeName(leg.mode) : "";

            const detailEntry = placeDetails[place.place_id];
            const detail =
              detailEntry?.status === "ready" ? detailEntry.data : null;
            const sortedGallery = sortPlaceGallery(detail?.gallery);
            const firstImage = sortedGallery[0] ?? null;
            const thumbUrl = firstImage?.thumb?.url || null;
            const hasPhoto = Boolean(
              thumbUrl && !brokenImageUrls.has(thumbUrl),
            );
            // 详情还在取时先留空白图位，确定没图了才画类型签，免得签和照片来回闪
            const detailSettled =
              detailEntry !== undefined && detailEntry.status !== "loading";
            const kind = resolveStopKind(place.category);
            const kindLabel = STOP_KIND_LABEL[kind];

            const scheduleMeta = resolveStopScheduleMeta(place);
            const stayText = formatStayMinutes(place.stay_minutes);
            const metaParts = [
              scheduleMeta.combinedText,
              stayText,
              place.optional ? "可选" : null,
            ].filter((part): part is string => Boolean(part));

            const cardNote = resolveStopCardNote(place);
            const isSelected =
              selectedPlaceId !== null && place.place_id === selectedPlaceId;

            return (
              <Fragment key={`${currentDay.day}-${place.place_id}-${idx}`}>
                {prevPlace &&
                  (!leg ? (
                    <div className="bk-leg bk-leg-empty" aria-hidden="true" />
                  ) : legPopup ? (
                    <button
                      type="button"
                      className="bk-leg"
                      aria-describedby={
                        activeLegPopup?.key === legKey ? popupId : undefined
                      }
                      onMouseEnter={(e) =>
                        showPopupForTrigger(
                          legKey,
                          popupId,
                          legPopup,
                          e.currentTarget,
                        )
                      }
                      onMouseLeave={() => hidePopupIfKey(legKey)}
                      onFocus={(e) =>
                        showPopupForTrigger(
                          legKey,
                          popupId,
                          legPopup,
                          e.currentTarget,
                        )
                      }
                      onBlur={() => hidePopupIfKey(legKey)}
                    >
                      <span>
                        {ModeIcon && (
                          <ModeIcon
                            size={13}
                            className="bk-leg-icon"
                            aria-hidden="true"
                          />
                        )}
                        {leg.duration_minutes} 分钟
                      </span>
                      <span>
                        {modeLabel}
                        {leg.duration_source === "estimate" ? " · 估算" : ""}
                      </span>
                    </button>
                  ) : (
                    <div className="bk-leg bk-leg-static">
                      <span>
                        {ModeIcon && (
                          <ModeIcon
                            size={13}
                            className="bk-leg-icon"
                            aria-hidden="true"
                          />
                        )}
                        {leg.duration_minutes} 分钟
                      </span>
                      <span>
                        {modeLabel}
                        {leg.duration_source === "estimate" ? " · 估算" : ""}
                      </span>
                    </div>
                  ))}

                <button
                  type="button"
                  className={isSelected ? "bk-dc is-on" : "bk-dc"}
                  data-place-id={place.place_id}
                  data-kind={kind}
                  aria-pressed={isSelected}
                  ref={(el) => registerCardRef?.(place.place_id, el)}
                  onClick={() => onSelectPlace(place.place_id)}
                >
                  {hasPhoto && thumbUrl ? (
                    <span className="bk-ph">
                      <img
                        className="photo"
                        src={thumbUrl}
                        alt={firstImage?.alt_text || ""}
                        loading="lazy"
                        onError={() => onImageError(thumbUrl)}
                      />
                    </span>
                  ) : detailSettled ? (
                    <span className="bk-ph is-kind" aria-hidden="true">
                      <StopKindMark kind={kind} />
                    </span>
                  ) : (
                    <span className="bk-ph" aria-hidden="true" />
                  )}

                  <span className="bk-bd">
                    <span className="bk-k">
                      <span className="bk-kind">{kindLabel}</span>
                      {scheduleMeta.period && (
                        <i
                          className={`sky ${scheduleMeta.period}`}
                          aria-hidden="true"
                        />
                      )}
                      {metaParts.length > 0 && (
                        <span className="bk-k-text">
                          {metaParts.join(" · ")}
                        </span>
                      )}
                    </span>

                    <b>
                      {idx + 1}
                      {"\u3000"}
                      {place.name}
                    </b>

                    {cardNote && <span className="bk-nt">{cardNote}</span>}
                  </span>
                </button>
              </Fragment>
            );
          })}
        </div>
      </section>

      {activeLegPopup && (
        <div
          id={activeLegPopup.popupId}
          role="tooltip"
          className="bk-leg-pop"
          style={{
            left: `${activeLegPopup.left}px`,
            bottom: `${activeLegPopup.bottom}px`,
          }}
        >
          {activeLegPopup.content.kind === "steps" ? (
            <ul className="bk-leg-steps">
              {activeLegPopup.content.steps.map((step, stepIdx) => {
                const StepIcon = STEP_KIND_ICON[step.kind] ?? Route;
                return (
                  <li key={`${activeLegPopup.key}-step-${stepIdx}`}>
                    <StepIcon size={11} aria-hidden="true" />
                    <span>{formatTransitStepLine(step)}</span>
                  </li>
                );
              })}
            </ul>
          ) : (
            <span>{activeLegPopup.content.summary}</span>
          )}
        </div>
      )}
    </>
  );
}
