import { Fragment, useCallback, useEffect, useRef } from "react";
import { X } from "lucide-react";
import type { TripPlace, TripPlan, TripResult } from "@/types/trip";
import { commuteModeIcon, commuteModeName } from "@/utils/format";
import {
  findCommuteLeg,
  formatBookDayLabel,
  formatDayWeatherBadge,
  formatStayMinutes,
  formatTransitStepLine,
  getDayWeather,
  isReducedMotionPreferred,
  pickClosestReadingStopId,
  resolveCommutePopup,
  resolveStopDetailBody,
  resolveStopScheduleMeta,
  sortPlaceGallery,
} from "@/utils/spreadBook";
import type { PlaceDetailEntry } from "./DayStrip";

export interface BookGuideScrollRequest {
  placeId: number;
  seq: number;
}

export interface BookGuideProps {
  result: TripResult;
  plan: TripPlan;
  initialDayNum: number;
  initialStopId: number | null;
  scrollRequest: BookGuideScrollRequest | null;
  readingPlaceId: number | null;
  readingDayNum: number;
  flashingWeatherDay?: number | null;
  placeDetails: Record<number, PlaceDetailEntry | undefined>;
  brokenImageUrls: ReadonlySet<string>;
  onImageError: (url: string) => void;
  onReadingStopChange: (placeId: number, dayNum: number) => void;
  onClose: () => void;
  disableEsc?: boolean;
}

function getOffsetTopWithin(el: HTMLElement, container: HTMLElement): number {
  const elRect = el.getBoundingClientRect();
  const boxRect = container.getBoundingClientRect();
  if (
    elRect.height > 0 ||
    boxRect.height > 0 ||
    elRect.top !== 0 ||
    boxRect.top !== 0
  ) {
    return elRect.top - boxRect.top + container.scrollTop;
  }
  return el.offsetTop;
}

function scrollAsideTo(aside: HTMLElement, top: number, smooth: boolean) {
  const clamped = Math.max(0, Math.round(top));
  const behavior: ScrollBehavior =
    smooth && !isReducedMotionPreferred() ? "smooth" : "auto";
  if (typeof aside.scrollTo === "function") {
    aside.scrollTo({ top: clamped, behavior });
  } else {
    aside.scrollTop = clamped;
  }
}

export function BookGuide({
  result,
  plan,
  initialDayNum,
  initialStopId,
  scrollRequest,
  readingPlaceId,
  readingDayNum,
  flashingWeatherDay = null,
  placeDetails,
  brokenImageUrls,
  onImageError,
  onReadingStopChange,
  onClose,
  disableEsc = false,
}: BookGuideProps) {
  const asideRef = useRef<HTMLElement>(null);
  const headerRef = useRef<HTMLDivElement>(null);
  const sectionRefs = useRef<Map<number, HTMLElement>>(new Map());
  const stopRefs = useRef<Map<number, HTMLLIElement>>(new Map());

  const readingPlaceIdRef = useRef(readingPlaceId);
  readingPlaceIdRef.current = readingPlaceId;
  const onReadingStopChangeRef = useRef(onReadingStopChange);
  onReadingStopChangeRef.current = onReadingStopChange;
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;
  const disableEscRef = useRef(disableEsc);
  disableEscRef.current = disableEsc;

  // 真实浏览器中，点击或平滑滚动期间暂时跳过途经站点，避免地图连续闪烁；用户手动干预立即解锁
  const suppressIoUntilRef = useRef<number>(0);

  const lockIoDuringSmoothScroll = useCallback((aside: HTMLElement) => {
    const hasRealScroll =
      aside.scrollHeight > aside.clientHeight && aside.clientHeight > 0;
    if (!hasRealScroll || isReducedMotionPreferred()) {
      suppressIoUntilRef.current = 0;
      return;
    }
    const now =
      typeof performance !== "undefined" ? performance.now() : Date.now();
    suppressIoUntilRef.current = now + 600;
  }, []);

  const clearIoLock = useCallback(() => {
    suppressIoUntilRef.current = 0;
  }, []);

  // Esc 关上攻略（弹窗打开时不抢走 Esc）
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

  // 手动滚动与 scrollend 时解除平滑滚动锁
  useEffect(() => {
    const aside = asideRef.current;
    if (!aside) return;

    const handleUserScrollInput = () => clearIoLock();
    const handleScrollEnd = () => clearIoLock();

    aside.addEventListener("wheel", handleUserScrollInput, { passive: true });
    aside.addEventListener("touchstart", handleUserScrollInput, {
      passive: true,
    });
    aside.addEventListener("pointerdown", handleUserScrollInput, {
      passive: true,
    });
    aside.addEventListener("scrollend", handleScrollEnd);

    return () => {
      aside.removeEventListener("wheel", handleUserScrollInput);
      aside.removeEventListener("touchstart", handleUserScrollInput);
      aside.removeEventListener("pointerdown", handleUserScrollInput);
      aside.removeEventListener("scrollend", handleScrollEnd);
    };
  }, [clearIoLock]);

  const scrollToStopElement = useCallback(
    (placeId: number, smooth: boolean) => {
      const aside = asideRef.current;
      const stopEl = stopRefs.current.get(placeId);
      if (!aside || !stopEl) return;

      const asideH = aside.clientHeight || 680;
      const top =
        getOffsetTopWithin(stopEl, aside) - Math.round(asideH * 0.28) + 10;
      if (smooth) {
        lockIoDuringSmoothScroll(aside);
      } else {
        clearIoLock();
      }
      scrollAsideTo(aside, top, smooth);
    },
    [clearIoLock, lockIoDuringSmoothScroll],
  );

  const scrollToDaySection = useCallback(
    (dayNum: number, smooth: boolean) => {
      const aside = asideRef.current;
      const secEl = sectionRefs.current.get(dayNum);
      if (!aside || !secEl) return;

      const headerH = headerRef.current?.getBoundingClientRect().height || 62;
      const top = getOffsetTopWithin(secEl, aside) - headerH - 6;
      if (smooth) {
        lockIoDuringSmoothScroll(aside);
      } else {
        clearIoLock();
      }
      scrollAsideTo(aside, top, smooth);
    },
    [clearIoLock, lockIoDuringSmoothScroll],
  );

  // 初次翻开：若原先正在看某一站，直达那一站；否则从当前这一天的开头读起
  const didInitialScrollRef = useRef(false);
  useEffect(() => {
    if (didInitialScrollRef.current) return;
    didInitialScrollRef.current = true;

    const firstDayNum = plan.days[0]?.day;
    if (initialStopId !== null && stopRefs.current.has(initialStopId)) {
      scrollToStopElement(initialStopId, false);
      return;
    }

    if (
      initialDayNum !== firstDayNum &&
      sectionRefs.current.has(initialDayNum)
    ) {
      scrollToDaySection(initialDayNum, false);
      return;
    }

    if (asideRef.current) {
      asideRef.current.scrollTop = 0;
    }
  }, [
    initialDayNum,
    initialStopId,
    plan.days,
    scrollToDaySection,
    scrollToStopElement,
  ]);

  // 地图点击某一站：攻略滚过去并切换为正在读
  const lastSeqRef = useRef<number | null>(null);
  useEffect(() => {
    if (!scrollRequest || scrollRequest.seq === lastSeqRef.current) return;
    lastSeqRef.current = scrollRequest.seq;
    scrollToStopElement(scrollRequest.placeId, true);
  }, [scrollRequest, scrollToStopElement]);

  // 监听每一站经过“上面约 28%”阅读线：通知地图跟着走
  const planObserverSig = plan.days
    .map((d) => `${d.day}:${d.places.map((p) => p.place_id).join(",")}`)
    .join("|");

  useEffect(() => {
    const aside = asideRef.current;
    if (!aside || typeof IntersectionObserver === "undefined") {
      return;
    }

    const observer = new IntersectionObserver(
      (entries) => {
        const now =
          typeof performance !== "undefined" ? performance.now() : Date.now();
        if (now < suppressIoUntilRef.current) {
          return;
        }

        const hitting = entries.filter((e) => e.isIntersecting);
        if (hitting.length === 0) return;

        const samples = hitting
          .map((e) => {
            const target = e.target as HTMLElement;
            const id = Number(target.dataset.placeId);
            if (!Number.isFinite(id)) return null;
            return {
              placeId: id,
              top: e.boundingClientRect.top,
              height: e.boundingClientRect.height,
            };
          })
          .filter(
            (s): s is { placeId: number; top: number; height: number } =>
              s !== null,
          );

        const asideRect = aside.getBoundingClientRect();
        const targetLineTop =
          asideRect.top +
          (aside.clientHeight || asideRect.height || 680) * 0.28;
        const pickedId = pickClosestReadingStopId(samples, targetLineTop);
        if (pickedId === null || pickedId === readingPlaceIdRef.current) {
          return;
        }

        const matchedEl = stopRefs.current.get(pickedId);
        const dayNum = Number(matchedEl?.dataset.day);
        if (Number.isFinite(dayNum)) {
          onReadingStopChangeRef.current(pickedId, dayNum);
        }
      },
      {
        root: aside,
        rootMargin: "-28% 0px -62% 0px",
      },
    );

    for (const day of plan.days) {
      for (const place of day.places) {
        const el = stopRefs.current.get(place.place_id);
        if (el) {
          observer.observe(el);
        }
      }
    }

    return () => {
      observer.disconnect();
    };
  }, [plan.days, planObserverSig]);

  const handleClickDayTab = (dayNum: number) => {
    scrollToDaySection(dayNum, true);
    const targetDay = plan.days.find((d) => d.day === dayNum);
    const firstPlace: TripPlace | undefined = targetDay?.places[0];
    if (firstPlace) {
      onReadingStopChange(firstPlace.place_id, dayNum);
    }
  };

  const handleClickStop = (placeId: number, dayNum: number) => {
    scrollToStopElement(placeId, true);
    onReadingStopChange(placeId, dayNum);
  };

  const dayTabsContainerRef = useRef<HTMLDivElement>(null);

  // 多日游顶部攻略页签横排超出时，自动把正在读的那一天滚进可见范围
  useEffect(() => {
    const container = dayTabsContainerRef.current;
    if (!container) return;
    const activeTab = container.querySelector<HTMLElement>(
      `.bk-rtab[data-rday="${readingDayNum}"]`,
    );
    if (!activeTab || container.scrollWidth <= container.clientWidth) return;
    const tabLeft = activeTab.offsetLeft;
    const tabRight = tabLeft + activeTab.offsetWidth;
    const viewLeft = container.scrollLeft;
    const viewRight = viewLeft + container.clientWidth;
    const behavior: ScrollBehavior = isReducedMotionPreferred()
      ? "auto"
      : "smooth";
    if (tabLeft < viewLeft + 8) {
      if (typeof container.scrollTo === "function") {
        container.scrollTo({ left: Math.max(0, tabLeft - 8), behavior });
      } else {
        container.scrollLeft = Math.max(0, tabLeft - 8);
      }
    } else if (tabRight > viewRight - 8) {
      const nextLeft = tabRight - container.clientWidth + 8;
      if (typeof container.scrollTo === "function") {
        container.scrollTo({ left: nextLeft, behavior });
      } else {
        container.scrollLeft = nextLeft;
      }
    }
  }, [readingDayNum]);

  return (
    <aside
      ref={asideRef}
      className="bk-float bk-side bk-guide-side"
      aria-label="全程攻略"
    >
      <div ref={headerRef} className="bk-rd-h">
        <div
          ref={dayTabsContainerRef}
          className="bk-rd-tabs"
          role="group"
          aria-label="攻略按天跳转"
        >
          {plan.days.map((d) => {
            const weatherDay = getDayWeather(result.weather, d.day);
            const badge = formatDayWeatherBadge(weatherDay);
            const active = d.day === readingDayNum;
            return (
              <button
                key={d.day}
                type="button"
                className="bk-dtab bk-rtab"
                data-rday={d.day}
                aria-pressed={active}
                onClick={() => handleClickDayTab(d.day)}
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

        <button
          type="button"
          className="bk-side-x"
          aria-label="关上"
          onClick={onClose}
        >
          <X size={16} aria-hidden="true" />
        </button>
      </div>

      <div className="bk-rd">
        {plan.days.map((day) => {
          const weatherDay = getDayWeather(result.weather, day.day);
          const badge = formatDayWeatherBadge(weatherDay);
          const narrativeText = day.narrative?.trim() ?? "";

          return (
            <section
              key={day.day}
              id={`rd-d${day.day}`}
              data-day={day.day}
              className="bk-rd-day"
              ref={(el) => {
                if (el) {
                  sectionRefs.current.set(day.day, el);
                } else {
                  sectionRefs.current.delete(day.day);
                }
              }}
            >
              <header className="bk-ga-dh">
                <h2>
                  <small>{formatBookDayLabel(day.day)}</small>
                  {day.title}
                </h2>
                {badge && (
                  <span className="bk-wx">
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
              </header>

              {narrativeText && <p className="bk-ga-nar">{narrativeText}</p>}

              <ol className="bk-gss">
                {day.places.map((place, idx) => {
                  const prevPlace = idx > 0 ? day.places[idx - 1] : null;
                  const leg = prevPlace
                    ? findCommuteLeg(day, prevPlace.place_id, place.place_id)
                    : undefined;
                  const legPopup = leg ? resolveCommutePopup(leg) : null;
                  const ModeIcon = leg ? commuteModeIcon(leg.mode) : null;
                  const modeLabel = leg ? commuteModeName(leg.mode) : "";

                  const scheduleMeta = resolveStopScheduleMeta(place);
                  const stayText = formatStayMinutes(place.stay_minutes);
                  const metaParts = [
                    scheduleMeta.combinedText,
                    stayText,
                    place.optional ? "可选" : null,
                  ].filter((part): part is string => Boolean(part));

                  const { itineraryText } = resolveStopDetailBody(place, null);

                  const detailEntry = placeDetails[place.place_id];
                  const detail =
                    detailEntry?.status === "ready" ? detailEntry.data : null;
                  const sortedGallery = sortPlaceGallery(detail?.gallery);
                  const firstImage = sortedGallery[0] ?? null;
                  const thumbUrl = firstImage?.thumb?.url || null;
                  const hasPhoto = Boolean(
                    thumbUrl && !brokenImageUrls.has(thumbUrl),
                  );

                  // 详情已有结果、既没照片也没说明的站（多为餐馆）收成一行，不留灰块
                  const isBare =
                    !hasPhoto &&
                    !itineraryText &&
                    (detailEntry?.status === "ready" ||
                      detailEntry?.status === "unsupported" ||
                      detailEntry?.status === "error");

                  const isReading =
                    readingPlaceId !== null &&
                    place.place_id === readingPlaceId;

                  return (
                    <Fragment key={`${day.day}-${place.place_id}-${idx}`}>
                      {leg && (
                        <li className="bk-gl">
                          <span className="bk-gl-head">
                            {ModeIcon && (
                              <ModeIcon
                                size={13}
                                className="bk-gl-icon"
                                aria-hidden="true"
                              />
                            )}
                            <span>
                              {modeLabel} {leg.duration_minutes} 分钟
                              {leg.duration_source === "estimate"
                                ? " · 估算"
                                : ""}
                            </span>
                          </span>
                          {legPopup?.kind === "steps" ? (
                            <div className="bk-gl-detail">
                              <ul className="bk-gl-steps">
                                {legPopup.steps.map((step, stepIdx) => (
                                  <li
                                    key={`${day.day}-${place.place_id}-step-${stepIdx}`}
                                  >
                                    {formatTransitStepLine(step)}
                                  </li>
                                ))}
                              </ul>
                            </div>
                          ) : legPopup?.kind === "summary" ? (
                            <div className="bk-gl-detail">
                              <span>{legPopup.summary}</span>
                            </div>
                          ) : null}
                        </li>
                      )}

                      <li
                        ref={(el) => {
                          if (el) {
                            stopRefs.current.set(place.place_id, el);
                          } else {
                            stopRefs.current.delete(place.place_id);
                          }
                        }}
                        className={[
                          "bk-gs",
                          isReading ? "is-reading" : "",
                          isBare ? "is-bare" : "",
                        ]
                          .filter(Boolean)
                          .join(" ")}
                        data-day={day.day}
                        data-place-id={place.place_id}
                        aria-current={isReading ? "location" : undefined}
                        onClick={(e) => {
                          if ((e.target as Element).closest("button")) return;
                          handleClickStop(place.place_id, day.day);
                        }}
                      >
                        {(scheduleMeta.period || metaParts.length > 0) && (
                          <div className="bk-gs-k">
                            {scheduleMeta.period && (
                              <i
                                className={`sky ${scheduleMeta.period}`}
                                aria-hidden="true"
                              />
                            )}
                            {metaParts.length > 0 && (
                              <span>{metaParts.join(" · ")}</span>
                            )}
                          </div>
                        )}

                        <div className="bk-gs-b">
                          <h4>
                            <button
                              type="button"
                              className="bk-gs-btn"
                              onClick={() =>
                                handleClickStop(place.place_id, day.day)
                              }
                            >
                              <span className="bk-gs-num">{idx + 1}</span>
                              <span>{place.name}</span>
                            </button>
                          </h4>
                          {itineraryText && (
                            <p className="bk-gs-n">{itineraryText}</p>
                          )}
                        </div>

                        {hasPhoto && thumbUrl ? (
                          <img
                            className="photo bk-gs-ph"
                            src={thumbUrl}
                            alt=""
                            loading="lazy"
                            onError={() => onImageError(thumbUrl)}
                          />
                        ) : isBare ? null : (
                          <span className="bk-gs-ph none">暂无实拍</span>
                        )}
                      </li>
                    </Fragment>
                  );
                })}
              </ol>
            </section>
          );
        })}

        <p className="bk-rd-end">
          出发前、怎么去 · 住哪、花多少在右上另外几页。
        </p>
      </div>
    </aside>
  );
}
