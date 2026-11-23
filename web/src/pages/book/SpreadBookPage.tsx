import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  Link,
  useNavigate,
  useParams,
  useSearchParams,
} from "react-router-dom";
import { FileClock, Link2Off, SearchX, TriangleAlert } from "lucide-react";
import { QuotaSettlementNotice } from "@/components/result/QuotaSettlementNotice";
import { SafeDeliveryNotice } from "@/components/result/SafeDeliveryNotice";
import { ShareDialog } from "@/components/share/ShareDialog";
import { useArtifact } from "@/hooks/useArtifact";
import { ApiRequestError, fetchPlaceDetail, fetchResult } from "@/services/api";
import { showToast } from "@/stores/toastStore";
import { useTripStore } from "@/stores/tripStore";
import type {
  PlaceDetail,
  TripPlace,
  TripPlan,
  TripResult,
  WeatherDay,
} from "@/types/trip";
import { saveBlob } from "@/utils/download";
import {
  classifyResultOpenError,
  missingLinkError,
  type ResultOpenError,
} from "@/utils/planLinkError";
import {
  DEFAULT_BOOK_STRIP_FOLDED_HEIGHT,
  DEFAULT_BOOK_STRIP_HEIGHT,
  filterUnscheduledMustInclude,
  findCommuteLeg,
  getDayWeather,
  isBookReadingPage,
  isReducedMotionPreferred,
  resolveBookCardCopy,
  resolveBookMapWeatherVisual,
  shouldKeepSidePageOnDaySwitch,
  type BookBarPageId,
  type BookSidePageId,
} from "@/utils/spreadBook";
import { BookBar } from "./BookBar";
import { BookGuide, type BookGuideScrollRequest } from "./BookGuide";
import { BookMap, type BookMapSettledSnapshot } from "./BookMap";
import { BookPages } from "./BookPages";
import { BookSunBubble, BookSunDrop } from "./BookSun";
import { DayStrip, type PlaceDetailEntry } from "./DayStrip";
import { StopPanel } from "./StopPanel";
import "./spread-book.css";

// 后端明确回“该地点暂不支持 / 不存在”时重试也没用，不按故障提示
function toFailedDetailEntry(err: unknown): PlaceDetailEntry {
  return err instanceof ApiRequestError &&
    (err.code === "PLACE_UNSUPPORTED" || err.code === "PLACE_NOT_FOUND")
    ? { status: "unsupported" }
    : { status: "error" };
}

function ArrowLeftIcon() {
  return (
    <svg className="icon" viewBox="0 0 24 24" aria-hidden="true">
      <path d="M19 12H5M11 6l-6 6 6 6" />
    </svg>
  );
}

export default function SpreadBookPage() {
  const { resultId, planId } = useParams<{
    resultId: string;
    planId: string;
  }>();
  const [searchParams] = useSearchParams();
  const jobId = searchParams.get("job_id");
  const initialDayParam = searchParams.get("day");
  const isShareParam = searchParams.get("share") === "1";

  return (
    <SpreadBookContent
      key={`${resultId ?? ""}:${planId ?? ""}:${jobId ?? ""}`}
      resultId={resultId}
      planId={planId}
      jobId={jobId}
      initialDayParam={initialDayParam}
      isShareParam={isShareParam}
    />
  );
}

interface SpreadBookContentProps {
  resultId: string | undefined;
  planId: string | undefined;
  jobId: string | null;
  initialDayParam: string | null;
  isShareParam: boolean;
}

function SpreadBookContent({
  resultId,
  planId,
  jobId,
  initialDayParam,
  isShareParam,
}: SpreadBookContentProps) {
  const navigate = useNavigate();
  const storeResult = useTripStore((s) => s.result);
  const setResult = useTripStore((s) => s.setResult);

  const [fetchedResult, setFetchedResult] = useState<TripResult | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<ResultOpenError | null>(null);

  const [selectedDayNum, setSelectedDayNum] = useState<number | null>(null);
  const [selectedPlaceId, setSelectedPlaceId] = useState<number | null>(null);
  const [activeSidePage, setActiveSidePage] = useState<BookSidePageId | null>(
    null,
  );
  const [stripFolded, setStripFolded] = useState(false);
  const [layoutMetrics, setLayoutMetrics] = useState({
    stripHeight: 0,
    headerHeight: 0,
    sideWidth: 0,
  });
  const [readingPlaceId, setReadingPlaceId] = useState<number | null>(null);
  const [guideAnchorPlaceId, setGuideAnchorPlaceId] = useState<number | null>(
    null,
  );
  const [guideScrollRequest, setGuideScrollRequest] =
    useState<BookGuideScrollRequest | null>(null);
  const [shareOpen, setShareOpen] = useState(isShareParam);
  const [placeDetails, setPlaceDetails] = useState<
    Record<number, PlaceDetailEntry | undefined>
  >({});
  const [brokenImageUrls, setBrokenImageUrls] = useState<Set<string>>(
    () => new Set(),
  );
  const [collectedSunDays, setCollectedSunDays] = useState<Set<number>>(
    () => new Set(),
  );
  const [collectingSunDay, setCollectingSunDay] = useState<number | null>(null);
  const [flashingWxDay, setFlashingWxDay] = useState<number | null>(null);
  const [sunBubble, setSunBubble] = useState<{
    day: number;
    weatherDay: WeatherDay;
  } | null>(null);
  const [settledCameraSnapshot, setSettledCameraSnapshot] =
    useState<BookMapSettledSnapshot | null>(null);

  const {
    phase: pdfPhase,
    loading: pdfLoading,
    blob: pdfBlob,
    error: pdfError,
    start: startPdf,
    reset: resetPdf,
  } = useArtifact(resultId, "pdf");

  useEffect(() => {
    if (isShareParam) {
      setShareOpen(true);
    }
  }, [isShareParam]);

  const atlasRef = useRef<HTMLElement>(null);
  const mountedRef = useRef(true);
  const stripFoldedRef = useRef(stripFolded);
  stripFoldedRef.current = stripFolded;
  const attemptedPlaceIdsRef = useRef<Set<number>>(new Set());
  const detailCacheRef = useRef<Map<number, PlaceDetail>>(new Map());
  const inFlightRetryRef = useRef<Set<number>>(new Set());
  const cardRefs = useRef<Map<number, HTMLButtonElement>>(new Map());
  const sunCollectTimerRef = useRef<number | null>(null);
  const sunFlashTimerRef = useRef<number | null>(null);
  const sunBubbleTimerRef = useRef<number | null>(null);

  const clearSunTimers = useCallback(() => {
    if (sunCollectTimerRef.current !== null) {
      window.clearTimeout(sunCollectTimerRef.current);
      sunCollectTimerRef.current = null;
    }
    if (sunFlashTimerRef.current !== null) {
      window.clearTimeout(sunFlashTimerRef.current);
      sunFlashTimerRef.current = null;
    }
    if (sunBubbleTimerRef.current !== null) {
      window.clearTimeout(sunBubbleTimerRef.current);
      sunBubbleTimerRef.current = null;
    }
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      clearSunTimers();
    };
  }, [clearSunTimers]);

  const matchedStore =
    storeResult &&
    String(storeResult.resultId) === String(resultId) &&
    storeResult.jobId === (jobId ?? "");
  const result = matchedStore ? storeResult.data : fetchedResult;

  // 打开一册：严格沿用已提交版 PlanDetailPage 的缓存与错误分类逻辑
  useEffect(() => {
    setError(null);
    const cached = useTripStore.getState().result;
    const hit =
      cached &&
      String(cached.resultId) === String(resultId) &&
      cached.jobId === (jobId ?? "");
    if (hit) {
      setFetchedResult(cached.data);
      setLoading(false);
      return;
    }

    setFetchedResult(null);
    if (!resultId || !jobId) {
      setError(missingLinkError());
      setLoading(false);
      return;
    }

    let cancelled = false;
    setLoading(true);
    fetchResult(resultId, jobId)
      .then((data) => {
        if (cancelled) return;
        setFetchedResult(data);
        setResult(resultId, jobId, data);
      })
      .catch((err) => {
        if (cancelled) return;
        setError(classifyResultOpenError(err));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [resultId, jobId, setResult]);

  const plan: TripPlan | undefined = useMemo(
    () => result?.plans?.find((p) => p.plan_id === planId),
    [result?.plans, planId],
  );

  const cityName = result?.city.name ?? "";
  const planTitle = plan?.title ?? "";

  // 导出 PDF：就绪自动下载并提示
  useEffect(() => {
    if (pdfPhase === "ready" && pdfBlob) {
      saveBlob(
        pdfBlob,
        `云途行程路书_${cityName || ""}_${planTitle || "方案"}.pdf`,
      );
      showToast("PDF 导出成功，已自动开始下载", "success");
      resetPdf();
    } else if (pdfPhase === "failed" && pdfError) {
      showToast(`PDF 导出失败: ${pdfError.message}`, "error");
      resetPdf();
    }
  }, [pdfPhase, pdfBlob, pdfError, cityName, planTitle, resetPdf]);

  const currentDay = useMemo(() => {
    const days = plan?.days;
    if (!days || days.length === 0) return null;
    if (selectedDayNum !== null) {
      return days.find((d) => d.day === selectedDayNum) ?? days[0];
    }
    if (initialDayParam) {
      const fromQuery = days.find((d) => String(d.day) === initialDayParam);
      if (fromQuery) return fromQuery;
    }
    return days[0];
  }, [plan?.days, selectedDayNum, initialDayParam]);

  const currentWeatherDay = useMemo(
    () => (currentDay ? getDayWeather(result?.weather, currentDay.day) : null),
    [result?.weather, currentDay],
  );

  const currentWeatherVisual = useMemo(
    () => resolveBookMapWeatherVisual(currentWeatherDay?.icon_code),
    [currentWeatherDay?.icon_code],
  );

  const isGuideOpen = activeSidePage === "guide";
  const isReadingMode = isBookReadingPage(activeSidePage);

  // 测量长卷与右栏的实际尺寸：同步 --bk-fold / --bk-strip-vis / --bk-strip-clear 给样式，并传给地图避让
  useLayoutEffect(() => {
    const atlas = atlasRef.current;
    if (!atlas || !result || !plan) return;

    const syncLayoutMetrics = () => {
      const stripEl = atlas.querySelector<HTMLElement>(".bk-day");
      const headerEl = stripEl?.querySelector<HTMLElement>(".bk-dh") ?? null;
      const sideEl = atlas.querySelector<HTMLElement>(".bk-side");

      const rawStripH = stripEl ? Math.round(stripEl.offsetHeight) : 0;
      const rawHeaderH = headerEl ? Math.round(headerEl.offsetHeight) : 0;
      const rawSideW = sideEl ? Math.round(sideEl.offsetWidth) : 0;

      const foldPx =
        stripFolded && rawStripH > 0 && rawHeaderH > 0
          ? Math.max(0, rawStripH - rawHeaderH + 16)
          : 0;
      if (stripEl) {
        stripEl.style.setProperty("--bk-fold", `${foldPx}px`);
      }

      const visibleStripH = isReadingMode
        ? 0
        : stripFolded
          ? rawHeaderH > 0
            ? rawHeaderH
            : DEFAULT_BOOK_STRIP_FOLDED_HEIGHT
          : rawStripH > 0
            ? rawStripH
            : DEFAULT_BOOK_STRIP_HEIGHT;

      const stripClearPx = isReadingMode
        ? 0
        : stripFolded
          ? visibleStripH
          : visibleStripH + 16;

      atlas.style.setProperty("--bk-strip-vis", `${visibleStripH}px`);
      atlas.style.setProperty("--bk-strip-clear", `${stripClearPx}px`);

      setLayoutMetrics((prev) =>
        prev.stripHeight === rawStripH &&
        prev.headerHeight === rawHeaderH &&
        prev.sideWidth === rawSideW
          ? prev
          : {
              stripHeight: rawStripH,
              headerHeight: rawHeaderH,
              sideWidth: rawSideW,
            },
      );
    };

    syncLayoutMetrics();
    window.addEventListener("resize", syncLayoutMetrics);

    if (typeof ResizeObserver === "undefined") {
      return () => {
        window.removeEventListener("resize", syncLayoutMetrics);
      };
    }

    const ro = new ResizeObserver(syncLayoutMetrics);
    ro.observe(atlas);
    const stripEl = atlas.querySelector<HTMLElement>(".bk-day");
    const headerEl = stripEl?.querySelector<HTMLElement>(".bk-dh");
    const sideEl = atlas.querySelector<HTMLElement>(".bk-side");
    if (stripEl) ro.observe(stripEl);
    if (headerEl) ro.observe(headerEl);
    if (sideEl) ro.observe(sideEl);

    return () => {
      ro.disconnect();
      window.removeEventListener("resize", syncLayoutMetrics);
    };
  }, [
    result,
    plan,
    currentDay?.day,
    isReadingMode,
    stripFolded,
    activeSidePage,
    selectedPlaceId,
  ]);

  useEffect(() => {
    clearSunTimers();
    setCollectingSunDay(null);
    setFlashingWxDay(null);
    setSunBubble(null);
    const activeDayNum = currentDay?.day;
    setSettledCameraSnapshot((prev) =>
      prev && activeDayNum !== undefined && prev.dayNum === activeDayNum
        ? prev
        : null,
    );
  }, [currentDay?.day, clearSunTimers]);

  // 批量读取尚未缓存过的地点详情：页面内缓存，同一地点（已成功/加载中/失败）只取一次
  const ensurePlaceDetailsLoaded = useCallback(
    (places: readonly TripPlace[]) => {
      const updates: Record<number, PlaceDetailEntry> = {};
      const idsToFetch: number[] = [];
      const seen = new Set<number>();

      for (const place of places) {
        const id = place.place_id;
        if (seen.has(id)) continue;
        seen.add(id);

        const cachedDetail = detailCacheRef.current.get(id);
        if (cachedDetail) {
          updates[id] = { status: "ready", data: cachedDetail };
          continue;
        }

        if (attemptedPlaceIdsRef.current.has(id)) {
          continue;
        }
        attemptedPlaceIdsRef.current.add(id);
        updates[id] = { status: "loading" };
        idsToFetch.push(id);
      }

      if (Object.keys(updates).length > 0) {
        setPlaceDetails((prev) => {
          let changed = false;
          const next = { ...prev };
          for (const [key, entry] of Object.entries(updates)) {
            const numId = Number(key);
            if (entry.status === "ready" && prev[numId]?.status === "ready") {
              continue;
            }
            if (!prev[numId] || entry.status === "ready") {
              next[numId] = entry;
              changed = true;
            }
          }
          return changed ? next : prev;
        });
      }

      for (const id of idsToFetch) {
        fetchPlaceDetail(id)
          .then((detail) => {
            detailCacheRef.current.set(id, detail);
            if (!mountedRef.current) return;
            setPlaceDetails((prev) => ({
              ...prev,
              [id]: { status: "ready", data: detail },
            }));
          })
          .catch((err: unknown) => {
            if (!mountedRef.current) return;
            setPlaceDetails((prev) => ({
              ...prev,
              [id]: toFailedDetailEntry(err),
            }));
          });
      }
    },
    [],
  );

  // 换到哪一天，就取那一天各站的详情
  useEffect(() => {
    if (!currentDay) return;
    ensurePlaceDetailsLoaded(currentDay.places);
  }, [currentDay, ensurePlaceDetailsLoaded]);

  // 点开攻略：一次性把整本行程还没拿过详情的站补齐，不重取已有站
  useEffect(() => {
    if (!isGuideOpen || !plan) return;
    ensurePlaceDetailsLoaded(plan.days.flatMap((d) => d.places));
  }, [isGuideOpen, plan, ensurePlaceDetailsLoaded]);

  const handleRetryPlaceDetail = useCallback((placeId: number) => {
    if (inFlightRetryRef.current.has(placeId)) return;
    inFlightRetryRef.current.add(placeId);
    attemptedPlaceIdsRef.current.add(placeId);

    setPlaceDetails((prev) => ({
      ...prev,
      [placeId]: { status: "loading" },
    }));

    fetchPlaceDetail(placeId)
      .then((detail) => {
        detailCacheRef.current.set(placeId, detail);
        if (!mountedRef.current) return;
        setPlaceDetails((prev) => ({
          ...prev,
          [placeId]: { status: "ready", data: detail },
        }));
      })
      .catch((err: unknown) => {
        if (!mountedRef.current) return;
        setPlaceDetails((prev) => ({
          ...prev,
          [placeId]: toFailedDetailEntry(err),
        }));
      })
      .finally(() => {
        inFlightRetryRef.current.delete(placeId);
      });
  }, []);

  const handleImageError = useCallback((url: string) => {
    setBrokenImageUrls((prev) => {
      if (prev.has(url)) return prev;
      const next = new Set(prev);
      next.add(url);
      return next;
    });
  }, []);

  const registerCardRef = useCallback(
    (placeId: number, el: HTMLButtonElement | null) => {
      if (el) {
        cardRefs.current.set(placeId, el);
      } else {
        cardRefs.current.delete(placeId);
      }
    },
    [],
  );

  const handleSelectDay = useCallback((dayNum: number) => {
    setSelectedDayNum(dayNum);
    setSelectedPlaceId(null);
    setStripFolded(false);
    setActiveSidePage((prev) =>
      shouldKeepSidePageOnDaySwitch(prev) ? prev : null,
    );
  }, []);

  const handleToggleStripFold = useCallback(() => {
    setStripFolded((prev) => !prev);
  }, []);

  const handleSelectPlace = useCallback(
    (placeId: number) => {
      if (activeSidePage === "guide") {
        const ownerDay = plan?.days.find((d) =>
          d.places.some((p) => p.place_id === placeId),
        );
        if (ownerDay) {
          setSelectedDayNum((prev) =>
            prev === ownerDay.day ? prev : ownerDay.day,
          );
        }
        setReadingPlaceId(placeId);
        setGuideScrollRequest((prev) => ({
          placeId,
          seq: (prev?.seq ?? 0) + 1,
        }));
        return;
      }

      setActiveSidePage(null);
      setSelectedPlaceId(placeId);
      if (!stripFoldedRef.current) {
        const cardEl = cardRefs.current.get(placeId);
        if (cardEl && typeof cardEl.scrollIntoView === "function") {
          cardEl.scrollIntoView({
            inline: "nearest",
            block: "nearest",
            behavior: isReducedMotionPreferred() ? "auto" : "smooth",
          });
        }
      }
    },
    [activeSidePage, plan],
  );

  const handleToggleBarPage = useCallback(
    (page: BookBarPageId) => {
      if (page === "guide") {
        if (activeSidePage === "guide") {
          setActiveSidePage(null);
          setReadingPlaceId(null);
          setGuideAnchorPlaceId(null);
          setSelectedPlaceId(null);
          return;
        }
        const anchorPlaceId = selectedPlaceId;
        const fallbackPlaceId =
          currentDay?.places[0]?.place_id ??
          plan?.days.find((d) => d.places.length > 0)?.places[0]?.place_id ??
          null;
        setGuideAnchorPlaceId(anchorPlaceId);
        setReadingPlaceId(anchorPlaceId ?? fallbackPlaceId);
        setSelectedPlaceId(null);
        setActiveSidePage("guide");
        return;
      }

      setSelectedPlaceId(null);
      setReadingPlaceId(null);
      setGuideAnchorPlaceId(null);
      setActiveSidePage((prev) => (prev === page ? null : page));
    },
    [activeSidePage, selectedPlaceId, currentDay, plan],
  );

  const handleReadingStopChange = useCallback(
    (placeId: number | null, dayNum: number) => {
      setReadingPlaceId(placeId);
      setSelectedDayNum((prev) => (prev === dayNum ? prev : dayNum));
    },
    [],
  );

  const handleToggleMiss = useCallback(() => {
    setSelectedPlaceId(null);
    setReadingPlaceId(null);
    setGuideAnchorPlaceId(null);
    setActiveSidePage((prev) => (prev === "miss" ? null : "miss"));
  }, []);

  const handleToggleStay = useCallback(() => {
    setSelectedPlaceId(null);
    setReadingPlaceId(null);
    setGuideAnchorPlaceId(null);
    setActiveSidePage((prev) => (prev === "stay" ? null : "stay"));
  }, []);

  const handleLocateStay = useCallback(() => {
    setSelectedPlaceId(null);
    setReadingPlaceId(null);
    setGuideAnchorPlaceId(null);
    setActiveSidePage("stay");
  }, []);

  const handleCloseSide = useCallback(() => {
    setSelectedPlaceId(null);
    setReadingPlaceId(null);
    setGuideAnchorPlaceId(null);
    setActiveSidePage(null);
  }, []);

  const handleCameraSettling = useCallback(() => {
    setSettledCameraSnapshot(null);
  }, []);

  const handleCameraSettled = useCallback(
    (snapshot: BookMapSettledSnapshot) => {
      setSettledCameraSnapshot(snapshot);
    },
    [],
  );

  const handleCollectSun = useCallback(() => {
    if (!currentDay || !currentWeatherDay) return;
    const dayNum = currentDay.day;
    if (collectingSunDay === dayNum) return;

    const targetTabSelector = isGuideOpen
      ? `.bk-rtab[data-rday="${dayNum}"]`
      : `.bk-dtab[data-day="${dayNum}"]`;
    const targetTab =
      atlasRef.current?.querySelector<HTMLElement>(targetTabSelector);
    targetTab?.scrollIntoView?.({
      inline: "nearest",
      block: "nearest",
    });

    const openBubbleWithAutoClose = () => {
      setCollectedSunDays((prev) => {
        if (prev.has(dayNum)) return prev;
        const next = new Set(prev);
        next.add(dayNum);
        return next;
      });
      setSunBubble({ day: dayNum, weatherDay: currentWeatherDay });
      if (sunBubbleTimerRef.current !== null) {
        window.clearTimeout(sunBubbleTimerRef.current);
      }
      sunBubbleTimerRef.current = window.setTimeout(() => {
        sunBubbleTimerRef.current = null;
        if (mountedRef.current) {
          setSunBubble(null);
        }
      }, 7000);
    };

    if (isReducedMotionPreferred()) {
      clearSunTimers();
      setCollectingSunDay(null);
      setFlashingWxDay(null);
      openBubbleWithAutoClose();
      return;
    }

    clearSunTimers();
    setCollectingSunDay(dayNum);
    sunCollectTimerRef.current = window.setTimeout(() => {
      sunCollectTimerRef.current = null;
      if (!mountedRef.current) return;
      setCollectingSunDay(null);
      setFlashingWxDay(dayNum);
      sunFlashTimerRef.current = window.setTimeout(() => {
        sunFlashTimerRef.current = null;
        if (mountedRef.current) {
          setFlashingWxDay(null);
        }
      }, 900);
      openBubbleWithAutoClose();
    }, 720);
  }, [
    collectingSunDay,
    currentDay,
    currentWeatherDay,
    isGuideOpen,
    clearSunTimers,
  ]);

  if (loading && !result) {
    return (
      <main className="bk-atlas" aria-label="旅行册">
        <div className="bk-state" role="status" aria-live="polite">
          <div className="bk-state-card">
            <h1>正在翻开旅行册…</h1>
            <p>正在读取这一趟的行程与地点安排。</p>
          </div>
        </div>
      </main>
    );
  }

  if (error || !result || !plan) {
    const kind = error?.kind ?? "generic";
    const Icon =
      kind === "invalidlink"
        ? Link2Off
        : kind === "notfound"
          ? SearchX
          : kind === "unsupported"
            ? FileClock
            : TriangleAlert;
    const message = error?.message ?? "方案未找到";

    return (
      <main className="bk-atlas" aria-label="旅行册">
        <div className="bk-state">
          <section className="bk-state-card" aria-label="无法打开旅行册">
            <span className="bk-state-icon" aria-hidden="true">
              <Icon size={40} strokeWidth={1.5} />
            </span>
            <p>{message}</p>
            {kind === "invalidlink" ? (
              <div className="bk-state-actions">
                <button
                  type="button"
                  className="bk-btn-primary"
                  onClick={() => navigate("/history")}
                >
                  回到历史记录
                </button>
                <button
                  type="button"
                  className="bk-btn-secondary"
                  onClick={() => navigate("/")}
                >
                  重新规划
                </button>
              </div>
            ) : (
              <div className="bk-state-actions">
                <button
                  type="button"
                  className="bk-btn-primary"
                  onClick={() => navigate("/")}
                >
                  {kind === "unsupported" ? "重新生成" : "重新规划"}
                </button>
              </div>
            )}
          </section>
        </div>
      </main>
    );
  }

  const { summary, tags } = resolveBookCardCopy(plan.summary, plan.tags);
  const unscheduledMustInclude = filterUnscheduledMustInclude(
    result.must_include,
  );

  const selectedIndex =
    currentDay && selectedPlaceId !== null
      ? currentDay.places.findIndex((p) => p.place_id === selectedPlaceId)
      : -1;
  const selectedPlace =
    selectedIndex >= 0 && currentDay ? currentDay.places[selectedIndex] : null;
  const nextPlace =
    selectedPlace && currentDay && selectedIndex + 1 < currentDay.places.length
      ? currentDay.places[selectedIndex + 1]
      : null;
  const nextLeg =
    selectedPlace && nextPlace && currentDay
      ? findCommuteLeg(currentDay, selectedPlace.place_id, nextPlace.place_id)
      : undefined;
  const hasSidePanel = Boolean(selectedPlace || activeSidePage);
  const hasStopPanel = !isReadingMode && Boolean(selectedPlace);
  const measuredStripVisibleHeight = isReadingMode
    ? 0
    : stripFolded
      ? layoutMetrics.headerHeight
      : layoutMetrics.stripHeight;
  const atlasClassName = [
    "bk-atlas",
    isReadingMode ? "is-reading" : "",
    hasStopPanel ? "has-stop" : "",
  ]
    .filter(Boolean)
    .join(" ");
  const mapSelectedPlaceId = isGuideOpen
    ? readingPlaceId
    : (selectedPlace?.place_id ?? null);
  const activeTabIconSelector = isGuideOpen
    ? '.bk-rtab[aria-pressed="true"] .bk-wx-icon'
    : '.bk-dtab[aria-pressed="true"] .bk-wx-icon';
  const showSunDrop = Boolean(
    (!isReadingMode || isGuideOpen) &&
    currentDay &&
    currentWeatherDay &&
    currentWeatherVisual.kind === "sun" &&
    settledCameraSnapshot &&
    settledCameraSnapshot.dayNum === currentDay.day &&
    (!collectedSunDays.has(currentDay.day) ||
      collectingSunDay === currentDay.day),
  );
  const showSunBubble = Boolean(
    (!isReadingMode || isGuideOpen) &&
    currentDay &&
    sunBubble &&
    sunBubble.day === currentDay.day,
  );

  return (
    <>
      <main ref={atlasRef} className={atlasClassName} aria-label="旅行册">
        {currentDay && (
          <BookMap
            day={currentDay}
            accommodation={plan.accommodation}
            weatherDay={currentWeatherDay}
            selectedPlaceId={mapSelectedPlaceId}
            selectedStay={activeSidePage === "stay"}
            hasSidePanel={hasSidePanel}
            isReading={isReadingMode}
            stripVisibleHeight={measuredStripVisibleHeight}
            stripFolded={stripFolded}
            sideWidth={layoutMetrics.sideWidth}
            onSelectPlace={handleSelectPlace}
            onSelectStay={handleToggleStay}
            onCameraSettling={handleCameraSettling}
            onCameraSettled={handleCameraSettled}
          />
        )}

        {showSunDrop &&
          currentDay &&
          currentWeatherDay &&
          settledCameraSnapshot && (
            <BookSunDrop
              key={currentDay.day}
              weatherDay={currentWeatherDay}
              stageWidth={settledCameraSnapshot.stageWidth}
              stageHeight={settledCameraSnapshot.stageHeight}
              isReading={isReadingMode}
              avoidPoints={settledCameraSnapshot.avoidPoints}
              collecting={collectingSunDay === currentDay.day}
              targetIconSelector={activeTabIconSelector}
              onCollect={handleCollectSun}
            />
          )}

        <section className="bk-float bk-card" aria-label="这本旅行册">
          <Link className="bk-back" to="/history">
            <ArrowLeftIcon />
            我的行程
          </Link>
          <h1>{plan.title}</h1>
          <p className="bk-meta">
            {result.city.name} · {result.request.days} 天 ·{" "}
            {result.request.people_count} 人
          </p>
          {(summary || tags.length > 0) && (
            <p className="bk-sum">
              {summary}
              {summary && tags.length > 0 ? " " : ""}
              {tags.length > 0 && (
                <span className="bk-tags">{tags.join(" ")}</span>
              )}
            </p>
          )}
          <div className="bk-notices">
            <SafeDeliveryNotice result={result} jobId={jobId} />
            <QuotaSettlementNotice delivery={result.delivery} />
          </div>
          {unscheduledMustInclude.length > 0 && (
            <button
              type="button"
              className="bk-miss"
              aria-pressed={activeSidePage === "miss"}
              onClick={handleToggleMiss}
            >
              <span>{unscheduledMustInclude.length} 处想去的没排进来</span>
              <span>看原因</span>
            </button>
          )}
        </section>

        <BookBar
          activePage={activeSidePage}
          pdfLoading={pdfLoading}
          onTogglePage={handleToggleBarPage}
          onExportPdf={startPdf}
          onOpenShare={() => setShareOpen(true)}
        />

        {isGuideOpen && currentDay ? (
          <BookGuide
            result={result}
            plan={plan}
            initialDayNum={currentDay.day}
            initialStopId={guideAnchorPlaceId}
            scrollRequest={guideScrollRequest}
            readingPlaceId={readingPlaceId}
            readingDayNum={currentDay.day}
            flashingWeatherDay={flashingWxDay}
            placeDetails={placeDetails}
            brokenImageUrls={brokenImageUrls}
            onImageError={handleImageError}
            onReadingStopChange={handleReadingStopChange}
            onClose={handleCloseSide}
            disableEsc={shareOpen}
          />
        ) : selectedPlace ? (
          <StopPanel
            place={selectedPlace}
            nextPlace={nextPlace}
            nextLeg={nextLeg}
            detailEntry={placeDetails[selectedPlace.place_id]}
            brokenImageUrls={brokenImageUrls}
            onImageError={handleImageError}
            onRetryDetail={handleRetryPlaceDetail}
            onClose={handleCloseSide}
            disableEsc={shareOpen}
          />
        ) : activeSidePage && activeSidePage !== "guide" ? (
          <BookPages
            page={activeSidePage}
            result={result}
            plan={plan}
            onClose={handleCloseSide}
            onLocateStay={handleLocateStay}
            disableEsc={shareOpen}
          />
        ) : null}

        {currentDay && (
          <DayStrip
            days={plan.days}
            currentDay={currentDay}
            selectedPlaceId={selectedPlace?.place_id ?? null}
            collapsed={isReadingMode}
            folded={stripFolded}
            weather={result.weather}
            flashingWeatherDay={flashingWxDay}
            placeDetails={placeDetails}
            brokenImageUrls={brokenImageUrls}
            onImageError={handleImageError}
            onSelectDay={handleSelectDay}
            onSelectPlace={handleSelectPlace}
            onToggleFold={handleToggleStripFold}
            registerCardRef={registerCardRef}
          />
        )}

        {showSunBubble && sunBubble && (
          <BookSunBubble
            weatherDay={sunBubble.weatherDay}
            targetIconSelector={activeTabIconSelector}
          />
        )}
      </main>

      {resultId && (
        <ShareDialog
          open={shareOpen}
          onClose={() => setShareOpen(false)}
          recordId={resultId}
          jobId={jobId ?? undefined}
        />
      )}
    </>
  );
}
