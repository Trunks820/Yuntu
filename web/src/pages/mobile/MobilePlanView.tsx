import React, {
  Suspense,
  lazy,
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import type {
  CommuteLeg,
  PackingChecklistGroup,
  TravelTip,
  TripDay,
  TripPlace,
  TripPlan,
  TripResult,
  WeatherDay,
} from "@/types/trip";
import { exactTimeSourceLabel, governedExactTime, periodLabel } from "@/utils/schedule";
import { cleanSummary, cleanTags } from "@/utils/format";
import { categoryName } from "@/constants/places";
import { collectWeatherReminders, weatherFaIcon } from "@/constants/weather";
import {
  isReducedMotionPreferred,
  resolveBookMapWeatherVisual,
  sortPlaceGallery,
} from "@/utils/spreadBook";
import {
  computeSwipeFollow,
  decideSwipeAxis,
  isSwipeStartInEdgeZone,
  resolveDaySwipe,
} from "@/utils/mobileSwipe";
import { usePlaceDetails } from "@/hooks/usePlaceDetails";
import { MobilePlaceSheet } from "@/components/mobile/MobilePlaceSheet";
import { SafeDeliveryNotice } from "@/components/result/SafeDeliveryNotice";
import { QuotaSettlementNotice } from "@/components/result/QuotaSettlementNotice";
import { MustIncludeNotice } from "@/components/detail/MustIncludeNotice";
import { CONCEPT_ASSETS } from "@/assets/mobile-concepts/conceptImages";
import { Icon } from "@/components/mobile/mobileUi";
import { MobileLodgingRow } from "@/pages/mobile/MobileLodgingRow";
import "@/styles/mobile.css";

const MapView = lazy(() =>
  import("@/components/detail/MapView").then((m) => ({ default: m.MapView })),
);

const PACE_LABEL: Record<string, string> = {
  RELAXED: "节奏轻松",
  MODERATE: "节奏适中",
  INTENSIVE: "节奏紧凑",
  PACKED: "节奏紧凑",
};

const COMMUTE_LABEL: Record<string, string> = {
  driving: "驾车",
  transit: "公共交通",
  walking: "步行",
  cycling: "骑行",
};

function commuteText(leg: CommuteLeg): string {
  const km = leg.distance_meters >= 1000
    ? `${(leg.distance_meters / 1000).toFixed(1)} 公里`
    : `${leg.distance_meters} 米`;
  const head = leg.mode === "transit" && leg.transit_summary
    ? leg.transit_summary
    : COMMUTE_LABEL[leg.mode] || "前往下一站";
  return `${head} · 约 ${leg.duration_minutes} 分钟 · ${km}`;
}

const EMPTY_PLACES: readonly TripPlace[] = [];

type MobileWeatherIconKind = "sun" | "cloud" | "rain" | "other";

/** 与电脑端同一套判定：只有晴、多云、雨用带轻动效的图标；雪、雾、大风、未知或缺省都用静态图标 */
function resolveMobileWeatherKind(
  iconCode: string | null | undefined,
): MobileWeatherIconKind {
  const { kind } = resolveBookMapWeatherVisual(iconCode);
  return kind === "sun" || kind === "cloud" || kind === "rain" ? kind : "other";
}

function MobileDayWeatherIcon({
  iconCode,
  kind,
}: {
  iconCode: string | null | undefined;
  kind: MobileWeatherIconKind;
}) {
  if (kind === "sun") {
    return (
      <svg
        viewBox="0 0 24 24"
        width="22"
        height="22"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-hidden="true"
      >
        <g className="wx-rays">
          <path d="M12 2.5v2.2M12 19.3v2.2M2.5 12h2.2M19.3 12h2.2M5.3 5.3l1.6 1.6M17.1 17.1l1.6 1.6M5.3 18.7l1.6-1.6M17.1 6.9l1.6-1.6" />
        </g>
        <circle cx="12" cy="12" r="4.2" />
      </svg>
    );
  }
  if (kind === "cloud") {
    return (
      <svg
        viewBox="0 0 24 24"
        width="22"
        height="22"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-hidden="true"
      >
        <path
          className="wx-cloud"
          d="M17.5 18.5H8.6a5.6 5.6 0 1 1 5.2-7.7h1.4a3.85 3.85 0 1 1 0 7.7Z"
        />
      </svg>
    );
  }
  if (kind === "rain") {
    return (
      <svg
        viewBox="0 0 24 24"
        width="22"
        height="22"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-hidden="true"
      >
        <path d="M17.5 14.5H8.6a5.6 5.6 0 1 1 5.2-7.7h1.4a3.85 3.85 0 1 1 0 7.7Z" />
        <path className="wx-drop d1" d="M9 17.5l-.8 2.2" />
        <path className="wx-drop d2" d="M13 17.5l-.8 2.2" />
        <path className="wx-drop d3" d="M17 17.5l-.8 2.2" />
      </svg>
    );
  }
  const FallbackIcon = weatherFaIcon(iconCode);
  return <FallbackIcon width={22} height={22} strokeWidth={1.6} aria-hidden="true" />;
}

function splitCommuteSummary(summary: string | null | undefined): string[] {
  if (!summary) return [];
  return summary
    .split("；")
    .map((segment) => segment.trim())
    .filter(Boolean);
}

function stayText(minutes?: number): string | null {
  if (!minutes || minutes <= 0) return null;
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  return h ? `建议停留 ${h} 小时${m ? `${m} 分` : ""}` : `建议停留 ${m} 分钟`;
}

/** 详情弹层里类型名后面那一串：时间与停留，和列表同口径 */
function sheetMetaParts(place: TripPlace, index: number): string[] {
  const exact = governedExactTime(place.schedule);
  const period = periodLabel(place.schedule?.period);
  const time = exact ? exact.text : period || `第 ${index + 1} 站`;
  return [time, stayText(place.stay_minutes)].filter((part): part is string => Boolean(part));
}

function sheetNextStop(day: TripDay, index: number): { name: string; legText: string | null } | null {
  const place = day.places[index];
  const next = day.places[index + 1];
  if (!place || !next) return null;
  const leg = day.commute_legs.find((l) => l.from_place_id === place.place_id);
  return { name: next.name, legText: leg ? commuteText(leg) : null };
}

export interface MobilePlanViewProps {
  result: TripResult;
  plan: TripPlan;
  jobId: string | null;
  cityCovers: string[];
  weatherDays?: WeatherDay[];
  timePrefText: string | null;
  packing: PackingChecklistGroup[];
  tips: TravelTip[];
  /** 现有的大交通与费用组件，直接嵌入 */
  transportSlot: React.ReactNode;
  costSlot: React.ReactNode;
  pdfLoading: boolean;
  onPdf: () => void;
  onShare: () => void;
  onBack: () => void;
}

/** 手机端旅行册：原型 .result 版式，内容全部来自真实生成结果 */
export const MobilePlanView: React.FC<MobilePlanViewProps> = ({
  result,
  plan,
  jobId,
  cityCovers,
  weatherDays,
  timePrefText,
  packing,
  tips,
  transportSlot,
  costSlot,
  pdfLoading,
  onPdf,
  onShare,
  onBack,
}) => {
  const [dayNumber, setDayNumber] = useState(plan.days[0]?.day ?? 1);
  const [mapOpen, setMapOpen] = useState(false);
  // 打开的地点详情；origin 是点开它的那一站，弹层从那里展开、关上时收回去（从地图打开时为空）
  const [sheet, setSheet] = useState<{ placeId: number; origin: HTMLElement | null } | null>(null);
  const [footerAway, setFooterAway] = useState(false);
  const [loadedImageUrls, setLoadedImageUrls] = useState<Set<string>>(() => new Set());
  const [brokenImageUrls, setBrokenImageUrls] = useState<Set<string>>(() => new Set());

  const scrollRef = useRef<HTMLDivElement>(null);
  const tabsRef = useRef<HTMLDivElement>(null);
  const indicatorRef = useRef<HTMLElement>(null);
  const paneRef = useRef<HTMLDivElement>(null);
  const indicatorMountedRef = useRef(false);
  const pendingTransitionRef = useRef<{ day: number; dir: 1 | -1 } | null>(null);
  const swipeRef = useRef<{
    pointerId: number | null;
    startX: number;
    startY: number;
    decided: boolean;
    dragging: boolean;
    lastOffsetX: number;
  }>({
    pointerId: null,
    startX: 0,
    startY: 0,
    decided: false,
    dragging: false,
    lastOffsetX: 0,
  });

  const day: TripDay | undefined = plan.days.find((d) => d.day === dayNumber) ?? plan.days[0];
  const currentDayIndex = day ? plan.days.findIndex((d) => d.day === day.day) : 0;
  const dayPlaces = day?.places ?? EMPTY_PLACES;

  // 详情里的那一站：先在当天找，地图上点到别的天的站点时再到其他天找
  const sheetStop = useMemo(() => {
    if (!sheet) return null;
    const ordered = day ? [day, ...plan.days.filter((d) => d !== day)] : plan.days;
    for (const d of ordered) {
      const index = d.places.findIndex((p) => p.place_id === sheet.placeId);
      if (index >= 0) return { day: d, index, place: d.places[index] };
    }
    return null;
  }, [sheet, day, plan.days]);
  const detailPlaces = useMemo(
    () =>
      sheetStop && !dayPlaces.includes(sheetStop.place)
        ? [...dayPlaces, sheetStop.place]
        : dayPlaces,
    [dayPlaces, sheetStop],
  );
  const placeDetails = usePlaceDetails(detailPlaces);

  const city = result.city.name;
  const cover = cityCovers[0] || CONCEPT_ASSETS.lake;
  const weather = weatherDays?.find((w) => w.day === day?.day);
  const weatherKind = weather ? resolveMobileWeatherKind(weather.icon_code) : null;
  const weatherWind = weather?.wind_text?.trim() || "";
  const weatherReminders = useMemo(
    () => (weather ? collectWeatherReminders([weather]) : []),
    [weather],
  );
  const commuteLines = useMemo(
    () => splitCommuteSummary(day?.commute_summary),
    [day?.commute_summary],
  );
  const summary = cleanSummary(plan.summary);
  const tags = cleanTags(plan.tags);
  // E1：travel 的攻略名形如「{城市}{天数}日｜…」，本身已带城市名；
  // 标题里已有城市时不再单独排一行城市，避免同一座城出现两遍。
  const titleMentionsCity = city.length > 0 && plan.title.includes(city);

  const markImageLoaded = useCallback((url: string) => {
    setLoadedImageUrls((prev) => {
      if (prev.has(url)) return prev;
      const next = new Set(prev);
      next.add(url);
      return next;
    });
  }, []);

  const markImageBroken = useCallback((url: string) => {
    setBrokenImageUrls((prev) => {
      if (prev.has(url)) return prev;
      const next = new Set(prev);
      next.add(url);
      return next;
    });
  }, []);

  const switchDay = useCallback(
    (targetDay: number, dir: 1 | -1) => {
      if (targetDay === day?.day) return;
      pendingTransitionRef.current = { day: targetDay, dir };
      setDayNumber(targetDay);
    },
    [day?.day],
  );

  const syncIndicator = useCallback((animate: boolean) => {
    const tabsEl = tabsRef.current;
    const indEl = indicatorRef.current;
    if (!tabsEl || !indEl) return;
    const selectedEl = tabsEl.querySelector<HTMLElement>(".day-tab.selected");
    if (!selectedEl) return;
    indEl.style.transition =
      animate && !isReducedMotionPreferred() ? "" : "none";
    indEl.style.width = `${selectedEl.offsetWidth}px`;
    indEl.style.height = `${selectedEl.offsetHeight}px`;
    indEl.style.transform = `translate(${selectedEl.offsetLeft}px, ${selectedEl.offsetTop}px)`;
  }, []);

  useLayoutEffect(() => {
    syncIndicator(indicatorMountedRef.current);
    indicatorMountedRef.current = true;

    const tabsEl = tabsRef.current;
    const selectedEl = tabsEl?.querySelector<HTMLElement>(".day-tab.selected");
    if (tabsEl && selectedEl && tabsEl.scrollWidth > tabsEl.clientWidth) {
      const pad = 22;
      if (selectedEl.offsetLeft < tabsEl.scrollLeft + pad) {
        tabsEl.scrollLeft = Math.max(0, selectedEl.offsetLeft - pad);
      } else if (
        selectedEl.offsetLeft + selectedEl.offsetWidth >
        tabsEl.scrollLeft + tabsEl.clientWidth - pad
      ) {
        tabsEl.scrollLeft =
          selectedEl.offsetLeft +
          selectedEl.offsetWidth -
          tabsEl.clientWidth +
          pad;
      }
    }
  }, [day?.day, plan.days.length, syncIndicator]);

  useEffect(() => {
    const handleResize = () => syncIndicator(false);
    window.addEventListener("resize", handleResize);
    const tabsEl = tabsRef.current;
    if (typeof ResizeObserver === "undefined" || !tabsEl) {
      return () => {
        window.removeEventListener("resize", handleResize);
      };
    }
    const observer = new ResizeObserver(handleResize);
    observer.observe(tabsEl);
    return () => {
      observer.disconnect();
      window.removeEventListener("resize", handleResize);
    };
  }, [syncIndicator]);

  // 换天后：如已滚过当天开头则回到当天开头，并播放新一天与各站依次升起的进场动效
  useLayoutEffect(() => {
    const pending = pendingTransitionRef.current;
    if (!pending || pending.day !== day?.day) return;
    pendingTransitionRef.current = null;

    const scroller = scrollRef.current;
    const pane = paneRef.current;
    const tabsEl = tabsRef.current;
    if (scroller && pane && tabsEl) {
      const top =
        pane.getBoundingClientRect().top -
        scroller.getBoundingClientRect().top +
        scroller.scrollTop -
        tabsEl.offsetHeight;
      if (scroller.scrollTop > top) {
        scroller.scrollTop = Math.max(0, top);
      }
    }

    if (
      pane &&
      !isReducedMotionPreferred() &&
      typeof pane.animate === "function"
    ) {
      pane.animate(
        [
          { opacity: 0, transform: `translateX(${pending.dir * 28}px)` },
          { opacity: 1, transform: "none" },
        ],
        { duration: 280, easing: "cubic-bezier(0.22, 1, 0.36, 1)" },
      );
      const stops = pane.querySelectorAll<HTMLElement>(".day-stop");
      const step = stops.length > 1 ? Math.min(60, 240 / (stops.length - 1)) : 0;
      stops.forEach((el, idx) => {
        el.animate?.(
          [
            { opacity: 0, transform: "translateY(12px)" },
            { opacity: 1, transform: "none" },
          ],
          {
            duration: 300,
            delay: 60 + idx * step,
            easing: "cubic-bezier(0.22, 1, 0.36, 1)",
            fill: "backwards",
          },
        );
      });
    }
  }, [day?.day]);

  // 底栏往下读时收起，上滑、停住 0.9s 或滚到底时回来
  useEffect(() => {
    const scroller = scrollRef.current;
    if (!scroller) return;
    let last = scroller.scrollTop;
    let idleTimer: number | null = null;
    const onScroll = () => {
      const y = scroller.scrollTop;
      const atEnd = y + scroller.clientHeight >= scroller.scrollHeight - 40;
      if (y - last > 6 && y > 160 && !atEnd) {
        setFooterAway(true);
      } else if (last - y > 6 || atEnd) {
        setFooterAway(false);
      }
      last = y;
      if (idleTimer !== null) {
        window.clearTimeout(idleTimer);
      }
      idleTimer = window.setTimeout(() => {
        setFooterAway(false);
      }, 900);
    };
    scroller.addEventListener("scroll", onScroll, { passive: true });
    return () => {
      scroller.removeEventListener("scroll", onScroll);
      if (idleTimer !== null) {
        window.clearTimeout(idleTimer);
      }
    };
  }, []);

  const handlePanePointerDown = useCallback(
    (e: React.PointerEvent<HTMLDivElement>) => {
      if (e.pointerType === "mouse" && e.button !== 0) return;
      if (isSwipeStartInEdgeZone(e.clientX, window.innerWidth)) return;
      swipeRef.current = {
        pointerId: e.pointerId,
        startX: e.clientX,
        startY: e.clientY,
        decided: false,
        dragging: false,
        lastOffsetX: 0,
      };
    },
    [],
  );

  const handlePanePointerMove = useCallback(
    (e: React.PointerEvent<HTMLDivElement>) => {
      const state = swipeRef.current;
      if (state.pointerId !== e.pointerId) return;
      const dx = e.clientX - state.startX;
      const dy = e.clientY - state.startY;
      if (!state.decided) {
        const axis = decideSwipeAxis(dx, dy);
        if (axis === "pending") return;
        state.decided = true;
        state.dragging = axis === "horizontal";
        if (state.dragging && paneRef.current) {
          paneRef.current.setPointerCapture?.(e.pointerId);
          paneRef.current.classList.add("is-dragging");
        }
      }
      if (state.dragging && paneRef.current) {
        const { offsetX, opacity } = computeSwipeFollow(
          dx,
          currentDayIndex,
          plan.days.length,
        );
        state.lastOffsetX = offsetX;
        paneRef.current.style.transform = `translateX(${offsetX}px)`;
        paneRef.current.style.opacity = String(opacity);
      }
    },
    [currentDayIndex, plan.days.length],
  );

  const finishPaneSwipe = useCallback(
    (e: React.PointerEvent<HTMLDivElement>, cancelled: boolean) => {
      const state = swipeRef.current;
      if (state.pointerId !== e.pointerId) return;
      state.pointerId = null;
      if (!state.dragging) return;
      state.dragging = false;

      const pane = paneRef.current;
      const lastOffsetX = state.lastOffsetX;
      if (pane) {
        pane.classList.remove("is-dragging");
        pane.style.transform = "";
        pane.style.opacity = "";
      }

      const dx = e.clientX - state.startX;
      if (!cancelled) {
        const resolution = resolveDaySwipe(dx, currentDayIndex, plan.days.length);
        const targetDay = plan.days[resolution.targetIndex];
        if (resolution.shouldSwitch && targetDay && resolution.direction !== 0) {
          switchDay(targetDay.day, resolution.direction);
          return;
        }
      }

      if (
        pane &&
        !isReducedMotionPreferred() &&
        typeof pane.animate === "function"
      ) {
        pane.animate(
          [{ transform: `translateX(${lastOffsetX}px)` }, { transform: "none" }],
          { duration: 220, easing: "cubic-bezier(0.22, 1, 0.36, 1)" },
        );
      }
    },
    [currentDayIndex, plan.days, switchDay],
  );

  return (
    <div className="ym">
      <div className="ym-device">
        <section className="screen result" aria-label={`${city} · 旅行册`}>
          <div className="scroll" ref={scrollRef}>
            <div className="result-hero">
              <img src={cover} alt={`${city} · 旅行册封面`} draggable={false} />
              <div className="topbar">
                <button type="button" className="icon-btn soft" onClick={onBack} aria-label="返回">
                  <Icon name="back" />
                </button>
                <span className="top-caption" style={{ color: "#ffffffc9" }}>
                  云途 · 你的旅行册
                </span>
                <button type="button" className="icon-btn soft" onClick={onShare} aria-label="分享旅行册">
                  <Icon name="spark" />
                </button>
              </div>
              <div className="result-hero-copy">
                <div className="eyebrow">YOUR PERSONAL TRAVEL BOOK</div>
                <h2>
                  {/* 城市与攻略名各占一行；断行交给 .result-hero-* 的 word-break:keep-all */}
                  {!titleMentionsCity && <span className="result-hero-city">{city}。</span>}
                  <span className="result-hero-plan">{plan.title}</span>
                </h2>
                <div className="result-meta">
                  <span>{result.request.days} 天</span>
                  <span className="dot" />
                  <span>{result.request.people_count} 人同行</span>
                  <span className="dot" />
                  <span>{PACE_LABEL[plan.pace.level] || "节奏适中"}</span>
                </div>
              </div>
            </div>

            <div className="result-body">
              {summary && <p className="result-summary">{summary}</p>}
              {(tags.length > 0 || timePrefText) && (
                <div className="tag-row" style={{ marginBottom: 16 }}>
                  {tags.map((t) => (
                    <span key={t} className="chip">
                      {t}
                    </span>
                  ))}
                  {timePrefText && <span className="chip">{timePrefText}</span>}
                </div>
              )}

              <div className="result-notices">
                <SafeDeliveryNotice result={result} jobId={jobId} />
                <MustIncludeNotice items={result.must_include} />
              </div>

              <div className="day-tabs" aria-label="按天查看旅行册" ref={tabsRef}>
                <i className="day-ind" aria-hidden="true" ref={indicatorRef} />
                {plan.days.map((d, idx) => (
                  <button
                    key={d.day}
                    type="button"
                    className={`day-tab${d.day === day?.day ? " selected" : ""}`}
                    aria-pressed={d.day === day?.day}
                    onClick={() => switchDay(d.day, idx > currentDayIndex ? 1 : -1)}
                  >
                    DAY<b>{String(d.day).padStart(2, "0")}</b>
                  </button>
                ))}
              </div>

              {day && (
                <div
                  className="day-pane"
                  ref={paneRef}
                  onPointerDown={handlePanePointerDown}
                  onPointerMove={handlePanePointerMove}
                  onPointerUp={(e) => finishPaneSwipe(e, false)}
                  onPointerCancel={(e) => finishPaneSwipe(e, true)}
                >
                  <div className="day-heading">
                    <h3>{day.title || `第 ${day.day} 天`}</h3>
                    <small>CHAPTER {String(day.day).padStart(2, "0")}</small>
                  </div>
                  {weather && weatherKind && (
                    <>
                      <div className={`day-wx wx-${weatherKind}`}>
                        <MobileDayWeatherIcon
                          iconCode={weather.icon_code}
                          kind={weatherKind}
                        />
                        <span>
                          {weather.weather_text} {weather.temp_min_c}–{weather.temp_max_c}°C
                          {weatherWind ? ` · ${weatherWind}` : ""}
                        </span>
                      </div>
                      {weatherReminders.map((tip, idx) => (
                        <p key={`wx-tip-${idx}`} className="day-wx-tip">
                          {tip}
                        </p>
                      ))}
                    </>
                  )}
                  {day.narrative && <p className="result-summary">{day.narrative}</p>}

                  <div className="day-line">
                    {day.places.map((place, i) => {
                      const exact = governedExactTime(place.schedule);
                      const period = periodLabel(place.schedule?.period);
                      const leg = day.commute_legs.find((l) => l.from_place_id === place.place_id);
                      const stay = stayText(place.stay_minutes);
                      const stopNote = place.activity_note?.trim() || place.brief;
                      const detailEntry = placeDetails[place.place_id];
                      const firstPhoto =
                        detailEntry?.status === "ready"
                          ? sortPlaceGallery(detailEntry.data.gallery)[0]
                          : undefined;
                      const thumbUrl = firstPhoto?.thumb?.url?.trim() || "";
                      const showPhoto = Boolean(thumbUrl) && !brokenImageUrls.has(thumbUrl);
                      return (
                        <article key={`${place.place_id}-${i}`} className="day-stop">
                          <div className="stop-head">
                            <div className="stop-main">
                              <div className="stop-top">
                                <strong>{exact ? exact.text : period || `第 ${i + 1} 站`}</strong>
                                <span>
                                  {exact ? `${exactTimeSourceLabel(exact.source)} · ` : ""}
                                  {categoryName(place.category)}
                                </span>
                              </div>
                              <button
                                type="button"
                                className="stop-title"
                                onClick={(e) =>
                                  setSheet({
                                    placeId: place.place_id,
                                    origin: e.currentTarget.closest<HTMLElement>(".day-stop"),
                                  })
                                }
                              >
                                <h4>{place.name}</h4>
                              </button>
                            </div>
                            {showPhoto && (
                              <img
                                ref={(node) => {
                                  if (node && node.complete && node.naturalWidth > 0) {
                                    markImageLoaded(thumbUrl);
                                  }
                                }}
                                src={thumbUrl}
                                alt=""
                                loading="lazy"
                                draggable={false}
                                className={`stop-ph is-fading${loadedImageUrls.has(thumbUrl) ? " is-loaded" : ""}`}
                                onLoad={() => markImageLoaded(thumbUrl)}
                                onError={() => markImageBroken(thumbUrl)}
                              />
                            )}
                          </div>
                          {stopNote && <p>{stopNote}</p>}
                          <div className="tag-row">
                            {stay && (
                              <span className="reason">
                                <Icon name="clock" />
                                {stay}
                              </span>
                            )}
                            {place.optional && (
                              <span className="reason">
                                <Icon name="spark" />
                                可选，看当天心情
                              </span>
                            )}
                          </div>
                          {leg && i < day.places.length - 1 && (
                            <p className="stop-commute">
                              <Icon name="arrow" />
                              {commuteText(leg)}
                            </p>
                          )}
                        </article>
                      );
                    })}
                  </div>
                  {commuteLines.length > 0 && (
                    <details key={`route-day-${day.day}`} className="day-route">
                      <summary>
                        当天怎么走
                        <i aria-hidden="true" />
                      </summary>
                      <ul>
                        {commuteLines.map((line, idx) => (
                          <li key={`route-${idx}`}>{line}</li>
                        ))}
                      </ul>
                    </details>
                  )}
                  {plan.accommodation && (
                    <MobileLodgingRow accommodation={plan.accommodation} />
                  )}
                </div>
              )}

              {(packing.length > 0 || tips.length > 0) && (
                <>
                  <div className="day-heading" style={{ marginTop: 24 }}>
                    <h3>出发前，记得这些</h3>
                    <small>BEFORE YOU GO</small>
                  </div>
                  {packing.map((g) => (
                    <div key={g.category} className="manifest-row">
                      <Icon name="book" />
                      <span>
                        <b>{g.category}</b>：{g.items.join("、")}
                      </span>
                    </div>
                  ))}
                  {tips.map((t) => (
                    <div key={t.title} className="manifest-row">
                      <Icon name="info" />
                      <span>
                        <b>{t.title}</b>：{t.content}
                      </span>
                    </div>
                  ))}
                </>
              )}

              <div className="day-heading" style={{ marginTop: 24 }}>
                <h3>出行与费用</h3>
                <small>TRAVEL & COST</small>
              </div>
              <div className="result-slot">
                {transportSlot}
                {costSlot}
              </div>

              <QuotaSettlementNotice delivery={result.delivery} />
              <div className="result-disclaimer">
                地点、开放时间与费用以实际情况为准，出发前建议再确认一次。
              </div>
            </div>
          </div>

          <div className={`footer-action${footerAway ? " is-away" : ""}`}>
            <div className="result-footer">
              <button type="button" className="small-secondary" onClick={() => setMapOpen(true)}>
                查看地图
              </button>
              <button type="button" className="primary" onClick={onPdf} disabled={pdfLoading}>
                <span>{pdfLoading ? "正在导出…" : "收好这本旅行册（PDF）"}</span>
                <Icon name="arrow" />
              </button>
            </div>
          </div>
        </section>

        {mapOpen && day && (
          <div className="overlay map-overlay" role="dialog" aria-modal="true" aria-label={`第 ${day.day} 天地图`}>
            <section className="sheet map-sheet">
              <div className="sheet-title">
                <h3>第 {day.day} 天 · 路线</h3>
                <button type="button" className="icon-btn" onClick={() => setMapOpen(false)} aria-label="关闭地图">
                  <Icon name="close" />
                </button>
              </div>
              <div className="map-frame">
                <Suspense fallback={<p className="u-demo-note">地图加载中…</p>}>
                  <MapView
                    day={day}
                    days={plan.days}
                    selectedDay={day.day}
                    accommodation={plan.accommodation}
                    activePlaceId={null}
                    onMarkerClick={(id) => {
                      setMapOpen(false);
                      setSheet({ placeId: id, origin: null });
                    }}
                  />
                </Suspense>
              </div>
            </section>
          </div>
        )}

        {sheetStop && (
          <MobilePlaceSheet
            key={sheetStop.place.place_id}
            place={sheetStop.place}
            detailEntry={placeDetails[sheetStop.place.place_id]}
            kindLabel={categoryName(sheetStop.place.category)}
            metaParts={sheetMetaParts(sheetStop.place, sheetStop.index)}
            nextStop={sheetNextStop(sheetStop.day, sheetStop.index)}
            isMustInclude={Boolean(
              result.must_include?.some(
                (mi) =>
                  mi.status === "scheduled" &&
                  mi.place_id != null &&
                  mi.place_id === sheetStop.place.place_id,
              ),
            )}
            origin={sheet?.origin ?? null}
            onClose={() => setSheet(null)}
          />
        )}
      </div>
    </div>
  );
};
