import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
  type SyntheticEvent,
} from "react";
import { checkPoiSelection, fetchPoiCatalog, type SelectablePoi } from "@/api/poi";
import { useDestinations } from "@/hooks/useDestinations";
import { useTripSubmission } from "@/hooks/useTripSubmission";
import { ApiRequestError } from "@/services/api";
import { showToast } from "@/stores/toastStore";
import { useTripStore } from "@/stores/tripStore";
import {
  COMPANION_BOUNDARIES,
  MOBILE_AVOID_OPTIONS,
  MOBILE_MUST_INCLUDE_LIMIT,
  type MobileCompanionRelation,
} from "@/types/mobile";
import {
  clampCompanionCounts,
  computeEndDate,
  isValidCalendarDate,
} from "@/utils/mobileDraft";
import { clearCinematicSubmission } from "@/utils/cinematicSubmission";
import {
  GEO_GUESS_HINT_TEXT,
  saveLastFromCity,
  useFromCityPrefill,
  type FromCityPrefillSource,
} from "@/utils/fromCityPrefill";
import { clearPendingSubmission } from "@/utils/pendingSubmission";
import {
  SPREAD_START_ACCOMMODATION_MAX,
  SPREAD_START_AVOID_ICONS,
  SPREAD_START_COMMUTES,
  SPREAD_START_COMPANIONS,
  SPREAD_START_DAY_OPTIONS,
  SPREAD_START_FROM_CITY_MAX,
  SPREAD_START_NOTES_MAX,
  SPREAD_START_RHYTHMS,
  SPREAD_START_STEPS,
  buildSpreadStartRequest,
  clearSpreadStartSubmitIntent,
  computeTargetCalendarOffset,
  createInitialSpreadStartDraft,
  formatCnMonthDay,
  formatTicketDateParts,
  furthestAllowedStep,
  getCityTicketCode,
  getCompanionById,
  getLocalTodayIso,
  getRhythmById,
  getSceneById,
  getScenePhotoOption,
  getSpreadStartBatchScenes,
  isSpreadStepValid,
  loadSpreadStartDraft,
  resolveSpreadStartCityPhoto,
  saveSpreadStartDraft,
  type SpreadRhythmId,
  type SpreadStartDraft,
} from "@/utils/spreadStartRequest";
import { cleanTripCityName } from "@/utils/tripShelf";
import "./spread-start.css";

interface SubmitErrorBanner {
  message: string;
  isCityNotSupported: boolean;
}

interface HeroImageTarget {
  fullUrl: string;
  smallUrl: string;
  label: string;
  credit: string;
}

interface HeroLayer {
  id: number;
  src: string;
  isOut: boolean;
}

const WEEK_CHARS = ["日", "一", "二", "三", "四", "五", "六"] as const;
const PEOPLE_COLORS = [
  "#37715d",
  "#b78269",
  "#557e99",
  "#c3a04b",
  "#91769d",
  "#8d995f",
] as const;
const PEOPLE_COLOR_OFFSET: Record<MobileCompanionRelation, number> = {
  solo: 0,
  partner: 0,
  friends: 2,
  parents: 4,
  kids: 0,
};

function pad2(n: number): string {
  return String(n).padStart(2, "0");
}

function oneLineTitle(title: string): string {
  return title.replace(/\s*\n\s*/g, "");
}

function preloadImageUrl(src: string): Promise<boolean> {
  return new Promise((resolve) => {
    const img = new Image();
    try {
      if ("fetchPriority" in img) {
        (img as HTMLImageElement & { fetchPriority: string }).fetchPriority =
          "high";
      }
    } catch {
      // ignore in mocked environments
    }
    let settled = false;
    const done = (ok: boolean) => {
      if (settled) return;
      settled = true;
      resolve(ok);
    };
    img.onload = () => {
      if (typeof img.decode === "function") {
        img.decode().then(
          () => done(true),
          () => done(img.complete && img.naturalWidth > 0),
        );
      } else {
        done(true);
      }
    };
    img.onerror = () => done(false);
    img.src = src;
    if (img.complete && img.naturalWidth > 0) {
      if (typeof img.decode === "function") {
        img.decode().then(
          () => done(true),
          () => done(true),
        );
      } else {
        done(true);
      }
    }
  });
}

function BrandCloudIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d="M7.2 17.5h9.6a3.9 3.9 0 0 0 .5-7.8 5.4 5.4 0 0 0-10.3 1.2 3.3 3.3 0 0 0 .2 6.6z" />
      <circle cx="9.6" cy="14.2" r="1.1" />
      <path d="M10.7 14.2h3.6" />
    </svg>
  );
}

function TicketRouteIcon() {
  return (
    <svg className="route" viewBox="0 0 100 36" aria-hidden="true">
      <path d="M2 22c21-28 58-25 92-9" strokeDasharray="3 4" />
      <path d="m77 10 17 3-11 11M94 13 75 22" />
    </svg>
  );
}

function ArrowRightIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.8}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M5 12h14M13 6l6 6-6 6" />
    </svg>
  );
}

function ArrowLeftIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.8}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M19 12H5M11 6l-6 6 6 6" />
    </svg>
  );
}

function CheckIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={2.2}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M5 12.5l4.5 4.5L19 7.5" />
    </svg>
  );
}

function ShuffleIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.8}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M4 12a8 8 0 0 1 14-5.3M20 4v4h-4M20 12a8 8 0 0 1-14 5.3M4 20v-4h4" />
    </svg>
  );
}

function SmallCardIcon({
  name,
  className = "ico",
}: {
  name:
    | "car"
    | "bus"
    | "bike"
    | "alarm"
    | "mountain"
    | "users"
    | "hourglass"
    | "person"
    | "heart"
    | "people"
    | "family";
  className?: string;
}) {
  switch (name) {
    case "car":
      return (
        <svg className={className} viewBox="0 0 24 24" aria-hidden="true">
          <path d="M19 17h2c.6 0 1-.4 1-1v-3c0-.9-.7-1.7-1.5-1.9C18.7 10.6 16 10 16 10s-1.3-1.4-2.2-2.3c-.5-.4-1.1-.7-1.8-.7H5c-.6 0-1.1.4-1.4.9l-1.4 2.9A3.7 3.7 0 0 0 2 12v4c0 .6.4 1 1 1h2" />
          <circle cx="7" cy="17" r="2" />
          <path d="M9 17h6" />
          <circle cx="17" cy="17" r="2" />
        </svg>
      );
    case "bus":
      return (
        <svg className={className} viewBox="0 0 24 24" aria-hidden="true">
          <path d="M8 6v6M15 6v6M2 12h19.6" />
          <path d="M18 18h3s.5-1.7.8-2.8c.1-.4.2-.8.2-1.2 0-.4-.1-.8-.2-1.2l-1.4-5C20.1 6.8 19.1 6 18 6H4a2 2 0 0 0-2 2v10h3" />
          <circle cx="7" cy="18" r="2" />
          <path d="M9 18h5" />
          <circle cx="16" cy="18" r="2" />
        </svg>
      );
    case "bike":
      return (
        <svg className={className} viewBox="0 0 24 24" aria-hidden="true">
          <circle cx="18.5" cy="17.5" r="3.5" />
          <circle cx="5.5" cy="17.5" r="3.5" />
          <circle cx="15" cy="5" r="1" />
          <path d="M12 17.5V14l-3-3 4-3 2 3h2" />
        </svg>
      );
    case "alarm":
      return (
        <svg className={className} viewBox="0 0 24 24" aria-hidden="true">
          <circle cx="12" cy="13" r="8" />
          <path d="M12 9v4l2 2M5 3 2 6M22 6l-3-3" />
        </svg>
      );
    case "mountain":
      return (
        <svg className={className} viewBox="0 0 24 24" aria-hidden="true">
          <path d="m8 3 4 8 5-5 5 15H2L8 3z" />
        </svg>
      );
    case "users":
      return (
        <svg className={className} viewBox="0 0 24 24" aria-hidden="true">
          <path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2" />
          <circle cx="9" cy="7" r="4" />
          <path d="M22 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75" />
        </svg>
      );
    case "hourglass":
      return (
        <svg className={className} viewBox="0 0 24 24" aria-hidden="true">
          <path d="M5 22h14M5 2h14M17 22v-4.2a2 2 0 0 0-.6-1.4L12 12l-4.4 4.4a2 2 0 0 0-.6 1.4V22M7 2v4.2a2 2 0 0 0 .6 1.4L12 12l4.4-4.4a2 2 0 0 0 .6-1.4V2" />
        </svg>
      );
    case "person":
      return (
        <svg
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth={1.6}
          strokeLinecap="round"
          strokeLinejoin="round"
          aria-hidden="true"
        >
          <circle cx="12" cy="7" r="3.5" />
          <path d="M5 21v-2a7 7 0 0 1 14 0v2" />
        </svg>
      );
    case "heart":
      return (
        <svg
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth={1.6}
          strokeLinecap="round"
          strokeLinejoin="round"
          aria-hidden="true"
        >
          <path d="M20.7 5.8c-2-2-5.2-1.7-7.2.3L12 7.6l-1.5-1.5c-2-2-5.2-2.3-7.2-.3-2.1 2.1-1.8 5.4.3 7.5L12 21l8.4-7.7c2.1-2.1 2.4-5.4.3-7.5Z" />
        </svg>
      );
    case "people":
      return (
        <svg
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth={1.6}
          strokeLinecap="round"
          strokeLinejoin="round"
          aria-hidden="true"
        >
          <circle cx="9" cy="7" r="3" />
          <path d="M2 20v-2a7 7 0 0 1 14 0v2M16 4a3 3 0 0 1 0 6M18 13a5 5 0 0 1 4 5v2" />
        </svg>
      );
    case "family":
      return (
        <svg
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth={1.6}
          strokeLinecap="round"
          strokeLinejoin="round"
          aria-hidden="true"
        >
          <circle cx="7" cy="6" r="2.5" />
          <circle cx="17" cy="6" r="2.5" />
          <circle cx="12" cy="14" r="2" />
          <path d="M2 18v-3a5 5 0 0 1 6-5M22 18v-3a5 5 0 0 0-6-5M8 22v-2a4 4 0 0 1 8 0v2" />
        </svg>
      );
  }
}

export default function SpreadStartPage() {
  const [draft, setDraft] = useState<SpreadStartDraft>(() => {
    const loaded = loadSpreadStartDraft();
    return loaded ?? createInitialSpreadStartDraft("guest");
  });

  const [preview, setPreview] = useState<
    { sceneId: string } | { cityName: string } | null
  >(null);
  const [cityRegion, setCityRegion] = useState("全部");
  const [calOffset, setCalOffset] = useState(() =>
    draft.step === 3
      ? computeTargetCalendarOffset(draft.startDate, draft.dateMode)
      : 0,
  );
  const [peopleOpen, setPeopleOpen] = useState(false);
  const [moreOpen, setMoreOpen] = useState(false);

  // 地点目录搜索状态（第 4 步折叠区）
  const [poiQueryInput, setPoiQueryInput] = useState("");
  const [poiResults, setPoiResults] = useState<SelectablePoi[]>([]);
  const [poiNextAfterId, setPoiNextAfterId] = useState<number | null>(null);
  const [poiLoading, setPoiLoading] = useState(false);
  const [poiError, setPoiError] = useState<string | null>(null);
  const [poiSearchedCity, setPoiSearchedCity] = useState<string | null>(null);

  // 后端提交报错
  const [submitError, setSubmitError] = useState<SubmitErrorBanner | null>(null);
  const [brokenGridUrls, setBrokenGridUrls] = useState<Record<string, true>>({});
  const [reducedMotion, setReducedMotion] = useState(() =>
    typeof window !== "undefined" && typeof window.matchMedia === "function"
      ? Boolean(window.matchMedia("(prefers-reduced-motion: reduce)")?.matches)
      : false,
  );

  // 左页相框渐变图层状态
  const [heroLayers, setHeroLayers] = useState<HeroLayer[]>([]);
  const [heroLoadingLabel, setHeroLoadingLabel] = useState<string | null>(null);
  const heroCommittedFullRef = useRef<string>("");
  const heroActiveTargetRef = useRef<string>("");
  const heroLayerIdRef = useRef(0);
  const heroCleanupTimersRef = useRef<number[]>([]);

  // 切步焦点管理
  const sideBodyRef = useRef<HTMLDivElement>(null);
  const headingRef = useRef<HTMLHeadingElement>(null);
  const prevRenderedStepRef = useRef<number>(draft.step);
  const draftRef = useRef(draft);
  draftRef.current = draft;

  const handleApplyFromCityPrefill = useCallback(
    (city: string, source: FromCityPrefillSource) => {
      setDraft((prev) => {
        if (prev.fromCityTouched || prev.fromCity.trim().length > 0) {
          return prev;
        }
        return {
          ...prev,
          fromCity: city,
          fromCitySource: source,
          updatedAt: Date.now(),
        };
      });
    },
    [],
  );

  const {
    showGeoHint: showFromCityGeoHint,
    markUserEdited: markFromCityEdited,
  } = useFromCityPrefill({
    value: draft.fromCity,
    onApplyPrefill: handleApplyFromCityPrefill,
    authStatus: "authenticated",
    bootstrapped: true,
    userId: "guest",
    maxLength: SPREAD_START_FROM_CITY_MAX,
    initialTouched: Boolean(draft.fromCityTouched),
    initialSource: draft.fromCitySource ?? null,
  });

  const handleChangeFromCity = (nextRaw: string) => {
    setSubmitError(null);
    markFromCityEdited();
    const value = nextRaw.slice(0, SPREAD_START_FROM_CITY_MAX);
    setDraft((prev) => ({
      ...prev,
      revision: prev.revision + 1,
      fromCity: value,
      fromCityTouched: true,
      fromCitySource: null,
      updatedAt: Date.now(),
    }));
  };

  const {
    data: destinationsData,
    isError: isDestinationsError,
    isLoading: isDestinationsLoading,
    isFetching: isDestinationsFetching,
    isPlaceholderData: isDestinationsPlaceholderData,
    refetch: refetchDestinations,
  } = useDestinations();

  const activeDestinations = useMemo(() => {
    if (isDestinationsError || isDestinationsPlaceholderData) return [];
    const list = destinationsData?.destinations ?? [];
    return list.filter((d) => d.isActive !== false);
  }, [destinationsData, isDestinationsError, isDestinationsPlaceholderData]);

  // 监测 prefers-reduced-motion
  useEffect(() => {
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") {
      return;
    }
    const mq = window.matchMedia("(prefers-reduced-motion: reduce)");
    if (!mq || typeof mq.addEventListener !== "function") return;
    const onChange = () => setReducedMotion(mq.matches);
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, []);

  // 实时落盘草稿到 sessionStorage
  useEffect(() => {
    saveSpreadStartDraft(draft);
  }, [draft]);

  // 换步时清空预览、切第 4 步对齐已选月份、滚回顶部并将焦点交给问题标题
  useEffect(() => {
    if (prevRenderedStepRef.current !== draft.step) {
      prevRenderedStepRef.current = draft.step;
      setPreview(null);
      if (draft.step === 3) {
        setCalOffset(
          computeTargetCalendarOffset(draft.startDate, draft.dateMode),
        );
      }
      if (sideBodyRef.current) {
        sideBodyRef.current.scrollTop = 0;
      }
      headingRef.current?.focus({ preventScroll: true });
    }
  }, [draft.step, draft.startDate, draft.dateMode]);

  // 加载/搜索城市地点目录（开源免登录直连，展开折叠区后按需调用）
  const loadPoiCatalogPage = useCallback(
    async (
      city: string,
      query: string,
      afterId: number | undefined,
      append: boolean,
    ) => {
      if (!city) {
        return;
      }
      setPoiLoading(true);
      setPoiError(null);
      try {
        const res = await fetchPoiCatalog(city, query, afterId, 20);
        setPoiSearchedCity(city);
        setPoiNextAfterId(res.next_after_id);
        setPoiResults((prev) => (append ? [...prev, ...res.places] : res.places));
      } catch (err: unknown) {
        setPoiError(
          err instanceof ApiRequestError && err.message
            ? err.message
            : "地点目录暂时读不到，请稍后再试。",
        );
      } finally {
        setPoiLoading(false);
      }
    },
    [],
  );

  useEffect(() => {
    if (
      draft.step !== 3 ||
      !moreOpen ||
      !draft.cityName
    ) {
      return;
    }
    const cleanCity = cleanTripCityName(draft.cityName);
    if (!cleanCity || poiSearchedCity === cleanCity) {
      return;
    }
    void loadPoiCatalogPage(cleanCity, "", undefined, false);
  }, [draft.step, moreOpen, draft.cityName, poiSearchedCity, loadPoiCatalogPage]);

  // 计算左框内容
  const batchScenes = useMemo(
    () => getSpreadStartBatchScenes(draft.batch, draft.moments),
    [draft.batch, draft.moments],
  );

  const heroTarget = useMemo<{
    scene: ReturnType<typeof getSceneById>;
    cityDestName: string | null;
    image: HeroImageTarget | null;
    creditText: string;
    isTicket: boolean;
  }>(() => {
    let targetScene = undefined as ReturnType<typeof getSceneById>;
    let targetCity: string | null = null;

    if (preview && "sceneId" in preview) {
      targetScene = getSceneById(preview.sceneId);
    } else if (preview && "cityName" in preview) {
      targetCity = preview.cityName;
    } else if (draft.step >= 2 && draft.cityName) {
      targetCity = draft.cityName;
    } else {
      const lastPicked = getSceneById(draft.moments[draft.moments.length - 1]);
      targetScene = lastPicked || batchScenes[0];
    }

    if (targetCity) {
      const cleanCity = cleanTripCityName(targetCity);
      const cityPhoto = resolveSpreadStartCityPhoto(cleanCity, activeDestinations);
      const creditText = cityPhoto?.credit
        ? `${cleanCity} · ${cityPhoto.credit}`
        : "";
      return {
        scene: undefined,
        cityDestName: cleanCity,
        image: cityPhoto
          ? {
              fullUrl: cityPhoto.fullUrl,
              smallUrl: cityPhoto.smallUrl,
              label: cleanCity,
              credit: creditText,
            }
          : null,
        creditText,
        isTicket: draft.step === 3 && Boolean(draft.cityName),
      };
    }

    if (targetScene) {
      const photo = getScenePhotoOption(targetScene, draft);
      return {
        scene: targetScene,
        cityDestName: null,
        image: {
          fullUrl: photo.fullUrl,
          smallUrl: photo.smallUrl,
          label: oneLineTitle(targetScene.title),
          credit: photo.credit,
        },
        creditText: photo.credit,
        isTicket: false,
      };
    }

    return {
      scene: undefined,
      cityDestName: null,
      image: null,
      creditText: "",
      isTicket: false,
    };
  }, [preview, draft, batchScenes, activeDestinations]);

  // 左页大图切换效果：先淡出旧图并显示衬线文字占位，待小图/大图解码后淡入（JSDOM 或减动效直切）
  useEffect(() => {
    const clearTimers = () => {
      for (const id of heroCleanupTimersRef.current) {
        window.clearTimeout(id);
      }
      heroCleanupTimersRef.current = [];
    };

    const nextFull = heroTarget.image?.fullUrl ?? "";
    const nextSmall = heroTarget.image?.smallUrl ?? "";
    const nextLabel =
      heroTarget.image?.label ?? heroTarget.cityDestName ?? "";

    if (!nextFull) {
      clearTimers();
      heroCommittedFullRef.current = "";
      heroActiveTargetRef.current = "";
      setHeroLayers([]);
      setHeroLoadingLabel(null);
      return;
    }

    if (nextFull === heroCommittedFullRef.current) {
      heroActiveTargetRef.current = nextFull;
      return;
    }

    // 目标变更或上次尚未完成大图落位：清空已落位标记，避免 StrictMode 重挂与快速来回悬停跳过加载
    heroCommittedFullRef.current = "";
    heroActiveTargetRef.current = nextFull;
    clearTimers();

    const supportsDecode =
      typeof HTMLImageElement !== "undefined" &&
      typeof HTMLImageElement.prototype.decode === "function";

    if (reducedMotion || !supportsDecode) {
      const id = ++heroLayerIdRef.current;
      heroCommittedFullRef.current = nextFull;
      setHeroLayers([{ id, src: nextFull, isOut: false }]);
      setHeroLoadingLabel(null);
      return;
    }

    let cancelled = false;
    setHeroLayers((prev) => prev.map((layer) => ({ ...layer, isOut: true })));
    setHeroLoadingLabel(nextLabel);

    const fadeOutOnlyTimer = window.setTimeout(() => {
      if (!cancelled && heroActiveTargetRef.current === nextFull) {
        setHeroLayers((prev) => prev.filter((layer) => !layer.isOut));
      }
    }, 500);
    heroCleanupTimersRef.current.push(fadeOutOnlyTimer);

    const mountLayer = (src: string, isFull: boolean) => {
      if (cancelled || heroActiveTargetRef.current !== nextFull) return;
      clearTimers();
      const newId = ++heroLayerIdRef.current;
      setHeroLayers((prev) => [
        // 同目标由小图升到大图时保留小图作底，等大图淡入结束再统一清掉旧层
        ...prev.map((layer) =>
          !isFull ? layer : layer.isOut ? layer : { ...layer, isOut: false },
        ),
        { id: newId, src, isOut: false },
      ]);
      setHeroLoadingLabel(null);
      if (isFull) {
        heroCommittedFullRef.current = nextFull;
      }
      const timerId = window.setTimeout(() => {
        if (!cancelled && heroActiveTargetRef.current === nextFull) {
          setHeroLayers((prev) => prev.filter((layer) => layer.id >= newId));
        }
      }, 500);
      heroCleanupTimersRef.current.push(timerId);
    };

    const runLoad = async () => {
      if (nextSmall && nextSmall !== nextFull) {
        const smallOk = await preloadImageUrl(nextSmall);
        if (cancelled || heroActiveTargetRef.current !== nextFull) return;
        if (smallOk) {
          mountLayer(nextSmall, false);
        }
      }
      const fullOk = await preloadImageUrl(nextFull);
      if (cancelled || heroActiveTargetRef.current !== nextFull) return;
      if (fullOk) {
        mountLayer(nextFull, true);
      }
    };

    void runLoad();

    return () => {
      cancelled = true;
      clearTimers();
    };
  }, [
    heroTarget.image?.fullUrl,
    heroTarget.image?.smallUrl,
    heroTarget.image?.label,
    heroTarget.cityDestName,
    reducedMotion,
  ]);

  // 提交协调器（统一提交锁、幂等、409 及成功跳 /planning/:jobId）
  const { isSubmitting, executeSubmit } = useTripSubmission({
    source: "cinematic",
    onRequestPrepare: async () => {
      const currentDraft = draftRef.current;
      const cleanCity = cleanTripCityName(currentDraft.cityName || "");

      if (currentDraft.mustInclude.length > 0 && cleanCity) {
        try {
          const selection = await checkPoiSelection(
            cleanCity,
            currentDraft.mustInclude.map((item) => item.placeId),
          );
          const nextPlaces: Array<{ placeId: number; name: string }> = [];
          let hasChanged = false;

          for (const selected of currentDraft.mustInclude) {
            const matched = selection.items.find(
              (item) => item.place_id === selected.placeId,
            );
            if (!matched || matched.status !== "available") {
              hasChanged = true;
              continue;
            }
            if (matched.place.name !== selected.name) {
              hasChanged = true;
            }
            nextPlaces.push({
              placeId: selected.placeId,
              name: matched.place.name,
            });
          }

          if (hasChanged) {
            setDraft((prev) => ({
              ...prev,
              revision: prev.revision + 1,
              mustInclude: nextPlaces,
              updatedAt: Date.now(),
            }));
            setMoreOpen(true);
            setSubmitError({
              message:
                "想顺路去的地点目录已按最新状态更新，请核对后再点提交。",
              isCityNotSupported: false,
            });
            return null;
          }
        } catch (err: unknown) {
          setMoreOpen(true);
          setSubmitError({
            message:
              err instanceof ApiRequestError && err.message
                ? err.message
                : "暂时没能核对想顺路去的地点，请稍后再试。",
            isCityNotSupported: false,
          });
          return null;
        }
      }

      setSubmitError(null);
      return buildSpreadStartRequest(currentDraft);
    },
    onSuccess: () => {
      saveLastFromCity(
        useTripStore.getState().formData?.from_city,
        SPREAD_START_FROM_CITY_MAX,
      );
      setSubmitError(null);
      clearSpreadStartSubmitIntent();
      clearPendingSubmission();
    },
    onError: (err: unknown) => {
      if (err instanceof ApiRequestError) {
        const isIndeterminate =
          err.code === "TIMEOUT" || err.code === "NETWORK_ERROR";
        if (!isIndeterminate) {
          clearCinematicSubmission();
        }
        setSubmitError({
          message: err.message,
          isCityNotSupported:
            err.status === 422 || err.code === "CITY_NOT_SUPPORTED",
        });
        return;
      }

      const fallbackMsg =
        err instanceof Error && err.message
          ? err.message
          : "暂时未能提交这次行程，请稍后重试。";
      setSubmitError({
        message: fallbackMsg,
        isCityNotSupported: false,
      });
    },
  });

  // ── 草稿交互更新函数 ─────────────────────────────────────────────────────
  const handleToggleMoment = (sceneId: string) => {
    setSubmitError(null);
    const current = draftRef.current;
    const exists = current.moments.includes(sceneId);
    if (!exists && current.moments.length >= 4) {
      showToast("最多挑 4 张画面，先取消一张再选", "error");
      return;
    }
    setDraft((prev) => {
      const stillExists = prev.moments.includes(sceneId);
      if (!stillExists && prev.moments.length >= 4) {
        return prev;
      }
      const nextMoments = stillExists
        ? prev.moments.filter((id) => id !== sceneId)
        : [...prev.moments, sceneId];
      const nextPicked = { ...prev.pickedPhotoIndex };
      if (stillExists) {
        delete nextPicked[sceneId];
      } else {
        nextPicked[sceneId] = Math.floor(prev.shuffleRound / 2);
      }
      return {
        ...prev,
        revision: prev.revision + 1,
        moments: nextMoments,
        pickedPhotoIndex: nextPicked,
        updatedAt: Date.now(),
      };
    });
  };

  const handleReshuffle = () => {
    setDraft((prev) => {
      const nextRound = prev.shuffleRound + 1;
      return {
        ...prev,
        shuffleRound: nextRound,
        batch: (nextRound % 2) as 0 | 1,
        updatedAt: Date.now(),
      };
    });
  };

  const handleSelectRhythm = (rhythmId: SpreadRhythmId) => {
    setSubmitError(null);
    setDraft((prev) => ({
      ...prev,
      revision: prev.revision + 1,
      rhythm: rhythmId,
      updatedAt: Date.now(),
    }));
  };

  const handleToggleCommute = (commuteId: "driving" | "transit" | "cycling") => {
    setSubmitError(null);
    setDraft((prev) => ({
      ...prev,
      revision: prev.revision + 1,
      commute: prev.commute === commuteId ? null : commuteId,
      updatedAt: Date.now(),
    }));
  };

  const handleToggleAvoid = (avoidId: (typeof MOBILE_AVOID_OPTIONS)[number]["id"]) => {
    setSubmitError(null);
    setDraft((prev) => {
      const exists = prev.avoidIds.includes(avoidId);
      return {
        ...prev,
        revision: prev.revision + 1,
        avoidIds: exists
          ? prev.avoidIds.filter((id) => id !== avoidId)
          : [...prev.avoidIds, avoidId],
        updatedAt: Date.now(),
      };
    });
  };

  const handleSelectCity = (cityName: string) => {
    const cleanCity = cleanTripCityName(cityName);
    setSubmitError(null);
    setDraft((prev) => {
      const changed = prev.cityName !== cleanCity;
      return {
        ...prev,
        revision: prev.revision + 1,
        cityName: cleanCity,
        mustInclude: changed ? [] : prev.mustInclude,
        accommodation: changed ? "" : prev.accommodation,
        updatedAt: Date.now(),
      };
    });
    setPoiQueryInput("");
    setPoiResults([]);
    setPoiNextAfterId(null);
    setPoiError(null);
    setPoiSearchedCity(null);
  };

  const handleSelectDays = (days: number) => {
    setSubmitError(null);
    setDraft((prev) => ({
      ...prev,
      revision: prev.revision + 1,
      days,
      updatedAt: Date.now(),
    }));
  };

  const handleSelectDate = (iso: string) => {
    setSubmitError(null);
    setDraft((prev) => ({
      ...prev,
      revision: prev.revision + 1,
      dateMode: "exact",
      startDate: iso,
      updatedAt: Date.now(),
    }));
  };

  const handleClearDate = () => {
    setSubmitError(null);
    setDraft((prev) => ({
      ...prev,
      revision: prev.revision + 1,
      dateMode: "undecided",
      startDate: null,
      updatedAt: Date.now(),
    }));
  };

  const handleSelectCompanion = (compId: MobileCompanionRelation) => {
    const bounds = COMPANION_BOUNDARIES[compId];
    setSubmitError(null);
    setPeopleOpen(false);
    setDraft((prev) => ({
      ...prev,
      revision: prev.revision + 1,
      companion: compId,
      adults: bounds.defaultAdults,
      children: bounds.defaultChildren,
      updatedAt: Date.now(),
    }));
  };

  const handleAdjustPeople = (key: "adults" | "children", delta: number) => {
    setSubmitError(null);
    setDraft((prev) => {
      if (!prev.companion) return prev;
      const bounds = COMPANION_BOUNDARIES[prev.companion];
      const nextAdults = key === "adults" ? prev.adults + delta : prev.adults;
      const nextChildren =
        key === "children" ? prev.children + delta : prev.children;
      if (
        nextAdults < bounds.minAdults ||
        nextAdults > bounds.maxAdults ||
        nextChildren < bounds.minChildren ||
        nextChildren > bounds.maxChildren ||
        (delta > 0 && nextAdults + nextChildren > bounds.maxTotal)
      ) {
        return prev;
      }
      const clamped = clampCompanionCounts(
        prev.companion,
        nextAdults,
        nextChildren,
      );
      return {
        ...prev,
        revision: prev.revision + 1,
        adults: clamped.adults,
        children: clamped.children,
        updatedAt: Date.now(),
      };
    });
  };

  const handleToggleMustInclude = (poi: { placeId: number; name: string }) => {
    setSubmitError(null);
    const current = draftRef.current;
    const exists = current.mustInclude.some((p) => p.placeId === poi.placeId);
    if (!exists && current.mustInclude.length >= MOBILE_MUST_INCLUDE_LIMIT) {
      showToast(
        `想顺路去的地点最多挑 ${MOBILE_MUST_INCLUDE_LIMIT} 处`,
        "error",
      );
      return;
    }
    setDraft((prev) => {
      const stillExists = prev.mustInclude.some(
        (p) => p.placeId === poi.placeId,
      );
      if (!stillExists && prev.mustInclude.length >= MOBILE_MUST_INCLUDE_LIMIT) {
        return prev;
      }
      return {
        ...prev,
        revision: prev.revision + 1,
        mustInclude: stillExists
          ? prev.mustInclude.filter((p) => p.placeId !== poi.placeId)
          : [...prev.mustInclude, poi],
        updatedAt: Date.now(),
      };
    });
  };

  const handleGoStep = (targetStep: 0 | 1 | 2 | 3) => {
    const maxAllowed = furthestAllowedStep(draft);
    if (targetStep > maxAllowed) return;
    setSubmitError(null);
    setDraft((prev) => ({
      ...prev,
      step: targetStep,
      updatedAt: Date.now(),
    }));
  };

  // ── 预览（悬停 / 聚焦）─────────────────────────────────────────────────────
  const handleScenePreviewEnter = (sceneId: string) => {
    setPreview((prev) =>
      prev && "sceneId" in prev && prev.sceneId === sceneId ? prev : { sceneId },
    );
  };

  const handleCityPreviewEnter = (cityName: string) => {
    setPreview((prev) =>
      prev && "cityName" in prev && prev.cityName === cityName
        ? prev
        : { cityName },
    );
  };

  const handleGridLeave = (e: SyntheticEvent<HTMLElement>) => {
    const related = (e as unknown as { relatedTarget?: EventTarget | null })
      .relatedTarget;
    if (
      related instanceof Node &&
      e.currentTarget.contains(related)
    ) {
      return;
    }
    setPreview(null);
  };

  // ── 第 3 步地区列表 & 第 4 步拦截状态 ──────────────────────────────────────
  const regions = useMemo(() => {
    const uniqueRegions: string[] = [];
    for (const dest of activeDestinations) {
      const r = dest.region?.trim();
      if (r && !uniqueRegions.includes(r)) {
        uniqueRegions.push(r);
      }
    }
    return ["全部", ...uniqueRegions];
  }, [activeDestinations]);

  const effectiveRegion = regions.includes(cityRegion) ? cityRegion : "全部";

  const filteredCities = useMemo(() => {
    if (effectiveRegion === "全部") return activeDestinations;
    return activeDestinations.filter(
      (d) => d.region?.trim() === effectiveRegion,
    );
  }, [activeDestinations, effectiveRegion]);

  const selectedDestination = useMemo(() => {
    if (!draft.cityName) return undefined;
    return activeDestinations.find(
      (d) => cleanTripCityName(d.name) === cleanTripCityName(draft.cityName || ""),
    );
  }, [activeDestinations, draft.cityName]);

  const handlePrimaryAction = () => {
    if (draft.step < 3) {
      if (!isSpreadStepValid(draft, draft.step)) return;
      setSubmitError(null);
      setDraft((prev) => ({
        ...prev,
        step: Math.min(3, prev.step + 1) as 0 | 1 | 2 | 3,
        updatedAt: Date.now(),
      }));
      return;
    }

    if (![0, 1, 2, 3].every((idx) => isSpreadStepValid(draft, idx))) {
      return;
    }

    setSubmitError(null);
    void executeSubmit();
  };

  // ── 渲染左页下部说明或出发票 ───────────────────────────────────────────────
  const renderHeroCap = () => {
    if (heroTarget.isTicket && draft.cityName) {
      const cleanCity = cleanTripCityName(draft.cityName);
      const cityCode = getCityTicketCode(cleanCity, selectedDestination);
      const compOpt = getCompanionById(draft.companion);
      const exact =
        draft.dateMode === "exact" &&
        draft.startDate &&
        isValidCalendarDate(draft.startDate);
      const startParts = exact && draft.startDate
        ? formatTicketDateParts(draft.startDate)
        : null;
      const endParts =
        exact && draft.startDate && draft.days
          ? formatTicketDateParts(computeEndDate(draft.startDate, draft.days))
          : null;
      const totalPeople = draft.adults + draft.children;

      const peopleColor = (index: number, compId: MobileCompanionRelation) =>
        PEOPLE_COLORS[(index + PEOPLE_COLOR_OFFSET[compId]) % PEOPLE_COLORS.length];

      const adultFigures =
        compOpt && draft.adults > 0
          ? Array.from({ length: draft.adults }, (_, i) => ({
              key: `a-${i}`,
              child: false,
              coat: peopleColor(i, compOpt.id),
            }))
          : [];
      const childFigures =
        compOpt && draft.children > 0
          ? Array.from({ length: draft.children }, (_, i) => ({
              key: `c-${i}`,
              child: true,
              coat: peopleColor(draft.adults + i, compOpt.id),
            }))
          : [];
      const mid = Math.ceil(adultFigures.length / 2);
      const orderedFigures = [
        ...adultFigures.slice(0, mid),
        ...childFigures,
        ...adultFigures.slice(mid),
      ];

      return (
        <article className="ticket" aria-label="出发票">
          <div className="t-head">
            <span className="t-brand">
              <span className="brand-mark" aria-hidden="true">
                <BrandCloudIcon />
              </span>
              云途
            </span>
            <small>NOT A BOOKING · 旅行草稿</small>
          </div>
          <div className="t-main">
            <div>
              {draft.fromCity.trim() ? (
                <p className="t-code" style={{ marginTop: 0, marginBottom: 2 }}>
                  {`从 ${draft.fromCity.trim()} 出发`}
                </p>
              ) : null}
              <p className="t-city">{cleanCity}</p>
              {cityCode ? <p className="t-code">{cityCode}</p> : null}
            </div>
            <TicketRouteIcon />
            <div className="t-days">
              <strong>{draft.days ? pad2(draft.days) : "—"}</strong>
              <small>天</small>
              <p className="t-code">YOUR DAYS</p>
            </div>
          </div>
          <div className="t-perf" />
          <div className="t-bottom">
            <div>
              <span className="t-tiny">出发日期（选填）</span>
              <p className="t-date">
                {!exact || !startParts ? (
                  "日子还没定"
                ) : endParts ? (
                  <>
                    {startParts.md}
                    <small>{startParts.week}</small> — {endParts.md}
                    <small>{endParts.week}</small>
                  </>
                ) : (
                  <>
                    {startParts.md}
                    <small>{startParts.week}</small> 出发
                  </>
                )}
              </p>
            </div>
            <div className="t-people">
              {compOpt ? (
                <>
                  <span className="mini" aria-hidden="true">
                    {orderedFigures.map((fig) => (
                      <span
                        key={fig.key}
                        className={`person${fig.child ? " child" : ""}`}
                        style={{ ["--coat" as string]: fig.coat }}
                      >
                        <i className="head" />
                        <i className="body" />
                      </span>
                    ))}
                  </span>
                  {`${compOpt.line} · ${totalPeople} 人`}
                </>
              ) : (
                "和谁去，还没定"
              )}
            </div>
          </div>
        </article>
      );
    }

    if (draft.step === 0 && heroTarget.scene) {
      const pickedIdx = draft.moments.indexOf(heroTarget.scene.id);
      return (
        <>
          <div className="cap-main">
            <p className="cap-title">{oneLineTitle(heroTarget.scene.title)}</p>
          </div>
          <div className="cap-side">
            <span className="cap-meta">
              {heroTarget.scene.tag} ·{" "}
              {pickedIdx >= 0 ? `已挑第 ${pickedIdx + 1} 张` : "还没挑"}
            </span>
            {heroTarget.creditText ? (
              <span className="hero-credit">{heroTarget.creditText}</span>
            ) : null}
          </div>
        </>
      );
    }

    const maxAllowed = furthestAllowedStep(draft);
    const compOpt = getCompanionById(draft.companion);
    const rhythmOpt = getRhythmById(draft.rhythm);
    const avoidLabels = draft.avoidIds
      .map((id) => MOBILE_AVOID_OPTIONS.find((item) => item.id === id)?.label)
      .filter(Boolean)
      .join("、");
    const mustLabels = draft.mustInclude.map((item) => item.name).join("、");

    let dateLine = "";
    if (draft.days) {
      dateLine =
        draft.dateMode === "exact" &&
        draft.startDate &&
        isValidCalendarDate(draft.startDate)
          ? `${formatCnMonthDay(draft.startDate)}至${formatCnMonthDay(computeEndDate(draft.startDate, draft.days))}`
          : "还没定";
    }

    const notesPairs = [
      ["日子", dateLine],
      ["避开", avoidLabels],
      ["必去", mustLabels],
      ["住在", draft.accommodation.trim()],
      ["出发", draft.fromCity.trim()],
      ["备注", draft.rawNotes.trim()],
    ].filter((pair): pair is [string, string] => Boolean(pair[1]));

    const pickedScenes = draft.moments
      .map((id) => getSceneById(id))
      .filter((s): s is NonNullable<typeof s> => Boolean(s));

    const renderSlot = (
      value: string,
      hint: string,
      targetStep: 1 | 2 | 3,
    ) => (
      <button
        type="button"
        className={`slot${value ? " is-filled" : ""}`}
        aria-label={`${hint}：${value || "还没选"}`}
        disabled={targetStep > maxAllowed}
        onClick={() => handleGoStep(targetStep)}
      >
        {value ? value : <span className="hint">{hint}</span>}
      </button>
    );

    return (
      <>
        <div className="cap-main">
          <p className="draft-sentence">
            想去{renderSlot(draft.cityName || "", "哪座城", 2)}，待
            {renderSlot(draft.days ? `${draft.days} 天` : "", "几天", 3)}，
            {renderSlot(compOpt ? compOpt.line : "", "和谁", 3)}，
            {renderSlot(rhythmOpt ? rhythmOpt.label : "", "什么节奏", 1)}地走。
          </p>
          {notesPairs.length > 0 ? (
            <p className="draft-notes">
              {notesPairs.map(([k, v]) => (
                <span key={k}>
                  <b>{k}</b>
                  {v}
                </span>
              ))}
            </p>
          ) : null}
        </div>
        <div className="cap-side">
          {pickedScenes.length > 0 ? (
            <button
              type="button"
              className="cap-picks"
              aria-label={`你挑的 ${pickedScenes.length} 张画面，回第一步修改`}
              onClick={() => handleGoStep(0)}
            >
              {pickedScenes.map((scene) => {
                const photo = getScenePhotoOption(scene, draft);
                if (brokenGridUrls[photo.smallUrl]) return null;
                return (
                  <img
                    key={scene.id}
                    className="photo"
                    src={photo.smallUrl}
                    alt=""
                    width={64}
                    height={40}
                    onError={() =>
                      setBrokenGridUrls((prev) => ({
                        ...prev,
                        [photo.smallUrl]: true,
                      }))
                    }
                  />
                );
              })}
            </button>
          ) : null}
          {heroTarget.creditText ? (
            <span className="hero-credit">{heroTarget.creditText}</span>
          ) : null}
        </div>
      </>
    );
  };

  // ── 渲染第 4 步日历与同行 ──────────────────────────────────────────────────
  const renderCalendar = () => {
    const now = new Date();
    const base = new Date(now.getFullYear(), now.getMonth() + calOffset, 1);
    const y = base.getFullYear();
    const m = base.getMonth();
    const today = getLocalTodayIso(now);
    const start =
      draft.dateMode === "exact" &&
      draft.startDate &&
      isValidCalendarDate(draft.startDate)
        ? draft.startDate
        : "";
    const end = start ? computeEndDate(start, draft.days || 1) : "";
    const daysInMonth = new Date(y, m + 1, 0).getDate();
    const leadingBlanks = base.getDay();

    return (
      <div className="cal" role="group" aria-labelledby="dateLabel">
        <div className="cal-head">
          <b>{`${y} 年 ${m + 1} 月`}</b>
          <span>
            <button
              type="button"
              className="cal-nav"
              aria-label="上个月"
              disabled={calOffset <= 0}
              onClick={() => setCalOffset((v) => Math.max(0, v - 1))}
            >
              ‹
            </button>
            <button
              type="button"
              className="cal-nav"
              aria-label="下个月"
              disabled={calOffset >= 11}
              onClick={() => setCalOffset((v) => Math.min(11, v + 1))}
            >
              ›
            </button>
          </span>
        </div>
        <div className="cal-grid">
          {WEEK_CHARS.map((w) => (
            <span key={w} className="wd" aria-hidden="true">
              {w}
            </span>
          ))}
          {Array.from({ length: leadingBlanks }, (_, idx) => (
            <span key={`blank-${idx}`} />
          ))}
          {Array.from({ length: daysInMonth }, (_, idx) => {
            const d = idx + 1;
            const iso = `${y}-${pad2(m + 1)}-${pad2(d)}`;
            const cls =
              iso === start
                ? end > start
                  ? "s"
                  : "s solo"
                : start && iso === end
                  ? "e"
                  : start && iso > start && iso < end
                    ? "in"
                    : "";
            return (
              <button
                key={iso}
                type="button"
                className={`d${cls ? ` ${cls}` : ""}`}
                aria-pressed={iso === start}
                aria-label={`${m + 1}月${d}日`}
                disabled={iso < today}
                onClick={() => handleSelectDate(iso)}
              >
                <span>{d}</span>
              </button>
            );
          })}
        </div>
      </div>
    );
  };

  const renderPeopleSection = () => {
    if (!draft.companion) return null;
    const compOpt = getCompanionById(draft.companion);
    const bounds = COMPANION_BOUNDARIES[draft.companion];
    if (!compOpt || !bounds) return null;

    const total = draft.adults + draft.children;
    const adjustable = compOpt.id !== "solo";
    const note =
      total >= 6
        ? "这版先支持最多 6 人同行。"
        : draft.children === 3
          ? "儿童最多 3 位，总人数不超过 6 位。"
          : compOpt.id === "partner"
            ? "两位成人，最多可带 3 位儿童。"
            : compOpt.id === "friends"
              ? "朋友同行，最多 6 位成人。"
              : "总人数最多 6 位，儿童最多 3 位。";

    return (
      <>
        <div className="counts">
          <span>
            {`成人 ${draft.adults} 位${bounds.maxChildren > 0 ? ` · 儿童 ${draft.children} 位` : ""}`}
          </span>
          {adjustable ? (
            <button
              type="button"
              className="text-btn"
              aria-expanded={peopleOpen}
              onClick={() => setPeopleOpen((v) => !v)}
            >
              {peopleOpen ? "收好人数" : "调整人数"}
            </button>
          ) : null}
        </div>
        {adjustable && peopleOpen ? (
          <>
            <div className="steppers">
              {bounds.minAdults !== bounds.maxAdults ? (
                <div className="stepper">
                  <span>成人</span>
                  <button
                    type="button"
                    aria-label="减少成人"
                    disabled={draft.adults <= bounds.minAdults}
                    onClick={() => handleAdjustPeople("adults", -1)}
                  >
                    −
                  </button>
                  <b>{draft.adults}</b>
                  <button
                    type="button"
                    aria-label="增加成人"
                    disabled={
                      draft.adults >= bounds.maxAdults ||
                      total >= bounds.maxTotal
                    }
                    onClick={() => handleAdjustPeople("adults", 1)}
                  >
                    +
                  </button>
                </div>
              ) : (
                <span className="stepper">{`成人 ${draft.adults} 位`}</span>
              )}
              {bounds.maxChildren > 0 ? (
                <div className="stepper">
                  <span>儿童</span>
                  <button
                    type="button"
                    aria-label="减少儿童"
                    disabled={draft.children <= bounds.minChildren}
                    onClick={() => handleAdjustPeople("children", -1)}
                  >
                    −
                  </button>
                  <b>{draft.children}</b>
                  <button
                    type="button"
                    aria-label="增加儿童"
                    disabled={
                      draft.children >= bounds.maxChildren ||
                      total >= bounds.maxTotal
                    }
                    onClick={() => handleAdjustPeople("children", 1)}
                  >
                    +
                  </button>
                </div>
              ) : null}
            </div>
            <p className="field-help">{note}</p>
          </>
        ) : null}
      </>
    );
  };

  // ── 渲染第 4 步底部小字与按钮 ──────────────────────────────────────────────
  const renderStep4FooterNote = () => {
    if (submitError) {
      return (
        <span className="foot-note is-err" role="status">
          {submitError.message}
          {submitError.isCityNotSupported ? (
            <button
              type="button"
              className="foot-action"
              onClick={() => handleGoStep(2)}
            >
              换一座城
            </button>
          ) : null}
        </span>
      );
    }

    return (
      <span className="foot-note" role="status">
        开源免登录 · 生成的行程将保存在当前浏览器中
      </span>
    );
  };

  // ── 右栏标题与主体渲染 ─────────────────────────────────────────────────────
  const maxStepAllowed = furthestAllowedStep(draft);
  const fullMomentsPicked = draft.moments.length >= 4;
  const cleanSelectedCity = draft.cityName
    ? cleanTripCityName(draft.cityName)
    : "";

  const handlePoiSearch = () => {
    if (!cleanSelectedCity) return;
    void loadPoiCatalogPage(
      cleanSelectedCity,
      poiQueryInput.trim(),
      undefined,
      false,
    );
  };

  const handlePoiKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter") {
      e.preventDefault();
      handlePoiSearch();
    }
  };

  const primaryCtaLabel =
    draft.step === 0
      ? "就选这几张"
      : draft.step === 1
        ? "就这个步调"
        : draft.step === 2
          ? cleanSelectedCity
            ? `就去${cleanSelectedCity}`
            : "先选一座城"
          : isSubmitting
            ? "正在提交…"
            : "生成攻略";

  const primaryDisabled =
    draft.step < 3
      ? !isSpreadStepValid(draft, draft.step)
      : isSubmitting || !isSpreadStepValid(draft, 3);

  const extraFilledCount = [
    draft.mustInclude.length,
    draft.accommodation.trim(),
    draft.rawNotes.trim(),
  ].filter(Boolean).length;

  return (
    <main className="desk">
      <section className="view" id="view-start" data-view="start">
        <div className="gallery">
          <figure
            className={`hero${heroTarget.isTicket ? " is-ticket" : ""}`}
            id="startHero"
          >
            <div
              className={`hero-frame${
                !heroTarget.image
                  ? " is-blank"
                  : heroLoadingLabel
                    ? " is-loading"
                    : ""
              }`}
              id="heroFrame"
              aria-label={
                !heroTarget.image && heroTarget.cityDestName
                  ? heroTarget.cityDestName
                  : undefined
              }
              data-label={
                heroTarget.image
                  ? heroLoadingLabel ?? undefined
                  : heroTarget.cityDestName ?? undefined
              }
            >
              {heroLayers.map((layer) => (
                <img
                  key={layer.id}
                  className={`photo${layer.isOut ? " is-out" : ""}`}
                  src={layer.src}
                  alt={
                    heroTarget.image?.label
                      ? `${heroTarget.image.label}风景`
                      : "旅行画面"
                  }
                />
              ))}
              {heroTarget.isTicket && heroTarget.creditText ? (
                <span className="frame-credit">{heroTarget.creditText}</span>
              ) : null}
            </div>
            <figcaption className="hero-cap" id="heroCap">
              {renderHeroCap()}
            </figcaption>
          </figure>

          <section className="side" id="startSide" aria-label="出发">
            <header className="side-head">
              <nav className="steps" aria-label="出发步骤">
                {SPREAD_START_STEPS.map((label, idx) => (
                  <button
                    key={label}
                    type="button"
                    className="step"
                    aria-current={draft.step === idx ? "step" : undefined}
                    disabled={idx > maxStepAllowed}
                    onClick={() => handleGoStep(idx as 0 | 1 | 2 | 3)}
                  >
                    <b>{idx + 1}</b>
                    {label}
                  </button>
                ))}
              </nav>
            </header>

            <div className="side-body ask" key={draft.step} ref={sideBodyRef}>
              {draft.step === 0 ? (
                <>
                  <div className="ask-head">
                    <div>
                      <h2 ref={headingRef} tabIndex={-1}>
                        先挑几张，让你<em>想出发</em>的画面。
                      </h2>
                      <p className="sub">
                        {fullMomentsPicked
                          ? "已经选满 4 张；想换的话，先取消一张。"
                          : "选 1 到 4 张，凭第一感觉。"}
                      </p>
                    </div>
                    <button
                      type="button"
                      className="text-btn"
                      onClick={handleReshuffle}
                    >
                      <ShuffleIcon />
                      换一批
                    </button>
                  </div>
                  <div
                    className="tiles"
                    onMouseLeave={handleGridLeave}
                    onBlur={(e) => handleGridLeave(e)}
                  >
                    {batchScenes.map((scene) => {
                      const pickedIdx = draft.moments.indexOf(scene.id);
                      const picked = pickedIdx >= 0;
                      const photo = getScenePhotoOption(scene, draft);
                      const isBroken = Boolean(brokenGridUrls[photo.smallUrl]);
                      return (
                        <button
                          key={scene.id}
                          type="button"
                          className="tile"
                          aria-pressed={picked}
                          aria-disabled={
                            fullMomentsPicked && !picked ? "true" : undefined
                          }
                          onClick={() => handleToggleMoment(scene.id)}
                          onMouseEnter={() =>
                            handleScenePreviewEnter(scene.id)
                          }
                          onFocus={() => handleScenePreviewEnter(scene.id)}
                        >
                          <span
                            className={`tile-ph${isBroken ? " is-blank" : ""}`}
                          >
                            {!isBroken ? (
                              <img
                                className="photo"
                                src={photo.smallUrl}
                                alt=""
                                width={640}
                                height={360}
                                onError={() =>
                                  setBrokenGridUrls((prev) => ({
                                    ...prev,
                                    [photo.smallUrl]: true,
                                  }))
                                }
                              />
                            ) : (
                              <em aria-hidden="true">{scene.short}</em>
                            )}
                            <span className="no" aria-hidden="true">
                              {picked ? pickedIdx + 1 : ""}
                            </span>
                          </span>
                          <span className="tile-lb">{scene.short}</span>
                        </button>
                      );
                    })}
                  </div>
                </>
              ) : null}

              {draft.step === 1 ? (
                <>
                  <div className="ask-head">
                    <div>
                      <h2 ref={headingRef} tabIndex={-1}>
                        这一趟，想按什么<em>步调</em>走？
                      </h2>
                      <p className="sub">
                        每天排多满、在城里怎么走，都会照着来排。
                      </p>
                    </div>
                  </div>
                  <div className="field">
                    <p className="field-label" id="paceLabel">
                      节奏
                    </p>
                    <div
                      className="paces"
                      role="group"
                      aria-labelledby="paceLabel"
                    >
                      {SPREAD_START_RHYTHMS.map((r) => (
                        <button
                          key={r.id}
                          type="button"
                          className="pace"
                          aria-pressed={draft.rhythm === r.id}
                          onClick={() => handleSelectRhythm(r.id)}
                        >
                          <span className="pace-art">
                            <img
                              src={r.imageUrl}
                              alt=""
                              width={840}
                              height={460}
                            />
                            <span className="mark" aria-hidden="true">
                              <CheckIcon />
                            </span>
                          </span>
                          <span className="pace-name">{r.label}</span>
                          <span className="pace-desc">{r.desc}</span>
                        </button>
                      ))}
                    </div>
                  </div>
                  <div className="field">
                    <p className="field-label kicker" id="commuteLabel">
                      在城里怎么走<span>默认打车</span>
                    </p>
                    <div
                      className="minis c3"
                      role="group"
                      aria-labelledby="commuteLabel"
                    >
                      {SPREAD_START_COMMUTES.map((c) => (
                        <button
                          key={c.id}
                          type="button"
                          className="mini-card"
                          aria-pressed={draft.commute === c.id}
                          onClick={() => handleToggleCommute(c.id)}
                        >
                          <SmallCardIcon name={c.icon} />
                          <b>{c.label}</b>
                          <small>{c.desc}</small>
                          <span className="mark" aria-hidden="true">
                            <CheckIcon />
                          </span>
                        </button>
                      ))}
                    </div>
                  </div>
                  <div className="field">
                    <p className="field-label kicker" id="avoidLabel">
                      还想避开什么<span>选填，可多选</span>
                    </p>
                    <div
                      className="minis c4"
                      role="group"
                      aria-labelledby="avoidLabel"
                    >
                      {MOBILE_AVOID_OPTIONS.map((av) => (
                        <button
                          key={av.id}
                          type="button"
                          className="mini-card"
                          aria-pressed={draft.avoidIds.includes(av.id)}
                          onClick={() => handleToggleAvoid(av.id)}
                        >
                          <SmallCardIcon name={SPREAD_START_AVOID_ICONS[av.id]} />
                          <b>{av.label}</b>
                          <span className="mark" aria-hidden="true">
                            <CheckIcon />
                          </span>
                        </button>
                      ))}
                    </div>
                  </div>
                </>
              ) : null}

              {draft.step === 2 ? (
                <>
                  <div className="ask-head">
                    <div>
                      <h2 ref={headingRef} tabIndex={-1}>
                        把这样的日子，放在<em>哪座城</em>？
                      </h2>
                      <p className="sub">
                        选一座城。鼠标停在城市上，左边先看看。
                      </p>
                    </div>
                  </div>

                  {isDestinationsError && !isDestinationsFetching ? (
                    <div className="city-catalog-state is-err" role="status">
                      <p>城市目录暂时读不到</p>
                      <button
                        type="button"
                        className="btn-primary"
                        onClick={() => void refetchDestinations()}
                      >
                        再试一次
                      </button>
                    </div>
                  ) : isDestinationsLoading ||
                    isDestinationsPlaceholderData ||
                    (isDestinationsFetching &&
                      activeDestinations.length === 0) ? (
                    <div className="city-catalog-state" role="status">
                      <p>正在读取城市目录…</p>
                    </div>
                  ) : activeDestinations.length === 0 ? (
                    <div className="city-catalog-state is-err" role="status">
                      <p>城市目录暂时读不到</p>
                      <button
                        type="button"
                        className="btn-primary"
                        onClick={() => void refetchDestinations()}
                      >
                        再试一次
                      </button>
                    </div>
                  ) : (
                    <>
                      <div
                        className="region-tabs"
                        role="group"
                        aria-label="按地区看"
                      >
                        {regions.map((region) => (
                          <button
                            key={region}
                            type="button"
                            className="region-tab"
                            aria-pressed={region === effectiveRegion}
                            onClick={() => setCityRegion(region)}
                          >
                            {region}
                          </button>
                        ))}
                      </div>
                      <div
                        className="city-grid"
                        role="group"
                        aria-label="城市"
                        onMouseLeave={handleGridLeave}
                        onBlur={(e) => handleGridLeave(e)}
                      >
                        {filteredCities.map((dest) => {
                          const cleanCity = cleanTripCityName(dest.name);
                          const cityPhoto = resolveSpreadStartCityPhoto(
                            cleanCity,
                            activeDestinations,
                          );
                          const isBroken = cityPhoto
                            ? Boolean(brokenGridUrls[cityPhoto.smallUrl])
                            : true;
                          const tagsLine = (dest.tags ?? [])
                            .slice(0, 2)
                            .join(" · ") || dest.tagline || "";

                          return (
                            <button
                              key={dest.id || cleanCity}
                              type="button"
                              className="city-tile"
                              aria-pressed={cleanCity === cleanSelectedCity}
                              onClick={() => handleSelectCity(cleanCity)}
                              onMouseEnter={() =>
                                handleCityPreviewEnter(cleanCity)
                              }
                              onFocus={() => handleCityPreviewEnter(cleanCity)}
                            >
                              <span
                                className={`tile-ph${
                                  cityPhoto && !isBroken ? "" : " is-blank"
                                }`}
                              >
                                {cityPhoto && !isBroken ? (
                                  <img
                                    className="photo"
                                    src={cityPhoto.smallUrl}
                                    alt=""
                                    loading="lazy"
                                    width={480}
                                    height={300}
                                    onError={() =>
                                      setBrokenGridUrls((prev) => ({
                                        ...prev,
                                        [cityPhoto.smallUrl]: true,
                                      }))
                                    }
                                  />
                                ) : (
                                  <em aria-hidden="true">{cleanCity}</em>
                                )}
                                <span className="mark" aria-hidden="true">
                                  <CheckIcon />
                                </span>
                              </span>
                              <span className="city-lb">
                                <b>{cleanCity}</b>
                                {tagsLine ? <span>{tagsLine}</span> : null}
                              </span>
                            </button>
                          );
                        })}
                      </div>
                    </>
                  )}
                </>
              ) : null}

              {draft.step === 3 ? (
                <>
                  <div className="ask-head">
                    <div>
                      <h2 ref={headingRef} tabIndex={-1}>
                        最后，留一张<em>出发票</em>。
                      </h2>
                      <p className="sub">
                        先选玩几天和同行的人，出发日期可以晚点决定。
                      </p>
                    </div>
                  </div>
                  <div className="field">
                    <p className="field-label kicker" id="daysLabel">
                      给自己留几天？<span>1–7 天</span>
                    </p>
                    <div
                      className="rail"
                      role="group"
                      aria-labelledby="daysLabel"
                    >
                      {SPREAD_START_DAY_OPTIONS.map((n) => (
                        <button
                          key={n}
                          type="button"
                          className="rail-day"
                          aria-pressed={draft.days === n}
                          aria-label={`${n} 天`}
                          onClick={() => handleSelectDays(n)}
                        >
                          <b>{pad2(n)}</b>
                          <small>天</small>
                        </button>
                      ))}
                    </div>
                  </div>
                  <div className="split">
                    <div className="split-col">
                      <div className="field">
                        <label
                          className="field-label kicker"
                          htmlFor="fromCityInput"
                        >
                          从哪出发<span>选填</span>
                        </label>
                        <div className="from-city-row">
                          <input
                            type="text"
                            id="fromCityInput"
                            className="input"
                            name="from_city"
                            autoComplete="address-level2"
                            aria-label="从哪出发"
                            aria-describedby={
                              showFromCityGeoHint ? "fromCityHint" : undefined
                            }
                            maxLength={SPREAD_START_FROM_CITY_MAX}
                            placeholder="如：上海"
                            value={draft.fromCity}
                            onChange={(e) => handleChangeFromCity(e.target.value)}
                          />
                          {draft.fromCity ? (
                            <button
                              type="button"
                              className="text-btn"
                              aria-label="清空出发城市"
                              onClick={() => handleChangeFromCity("")}
                            >
                              清空
                            </button>
                          ) : null}
                        </div>
                        {showFromCityGeoHint ? (
                          <p
                            className="field-help is-guess"
                            id="fromCityHint"
                            role="status"
                          >
                            {GEO_GUESS_HINT_TEXT}
                          </p>
                        ) : null}
                      </div>
                      <div className="field">
                        <p className="field-label kicker" id="dateLabel">
                          哪天出发<span>选填</span>
                        </p>
                        {renderCalendar()}
                        {draft.dateMode === "exact" && draft.startDate ? (
                          <button
                            type="button"
                            className="text-btn cal-clear"
                            onClick={handleClearDate}
                          >
                            改成暂不确定日期
                          </button>
                        ) : (
                          <p className="cal-note">不选也能先排，日子以后再补。</p>
                        )}
                      </div>
                    </div>
                    <div className="field">
                      <p className="field-label kicker" id="compLabel">
                        和谁一起写这本？
                      </p>
                      <div
                        className="comps"
                        role="group"
                        aria-labelledby="compLabel"
                      >
                        {SPREAD_START_COMPANIONS.map((item) => (
                          <button
                            key={item.id}
                            type="button"
                            className={`comp${item.wide ? " wide" : ""}`}
                            aria-pressed={draft.companion === item.id}
                            onClick={() => handleSelectCompanion(item.id)}
                          >
                            <SmallCardIcon name={item.icon} className="" />
                            <span>
                              <b>{item.title}</b>
                              <small>{item.sub}</small>
                            </span>
                            <span className="mark" aria-hidden="true">
                              <CheckIcon />
                            </span>
                          </button>
                        ))}
                      </div>
                      {renderPeopleSection()}
                    </div>
                  </div>

                  <details
                    className="more"
                    open={moreOpen}
                    onToggle={(e) => setMoreOpen(e.currentTarget.open)}
                  >
                    <summary>
                      <b>还有要交代的</b>
                      <span>
                        {extraFilledCount > 0
                          ? `已填 ${extraFilledCount} 项`
                          : "必去地点、住哪一带、备注，都可以不填"}
                      </span>
                    </summary>
                    <div className="more-body">
                      <div className="field">
                        <p className="field-label" id="mustLabel">
                          想顺路去<span>{`最多 ${MOBILE_MUST_INCLUDE_LIMIT} 处`}</span>
                        </p>
                        <>
                          <div className="poi-search-row">
                              <input
                                type="text"
                                id="poiSearchInput"
                                className="input"
                                placeholder={
                                  cleanSelectedCity
                                    ? `搜 ${cleanSelectedCity} 想去的地点…`
                                    : "先选城市后搜索地点…"
                                }
                                aria-label="搜索想顺路去的地点"
                                value={poiQueryInput}
                                onChange={(e) =>
                                  setPoiQueryInput(e.target.value)
                                }
                                onKeyDown={handlePoiKeyDown}
                              />
                              <button
                                type="button"
                                className="poi-search-btn"
                                disabled={poiLoading || !cleanSelectedCity}
                                onClick={handlePoiSearch}
                              >
                                {poiLoading ? "查找中…" : "搜索"}
                              </button>
                            </div>
                            {draft.mustInclude.length > 0 ? (
                              <div
                                className="chips"
                                role="group"
                                aria-label="已选想顺路去的地点"
                              >
                                {draft.mustInclude.map((item) => (
                                  <button
                                    key={item.placeId}
                                    type="button"
                                    className="chip"
                                    aria-pressed="true"
                                    onClick={() =>
                                      handleToggleMustInclude(item)
                                    }
                                  >
                                    {`${item.name} ×`}
                                  </button>
                                ))}
                              </div>
                            ) : null}
                            {poiError ? (
                              <p className="field-help is-err" role="status">
                                {poiError}
                              </p>
                            ) : poiLoading && poiResults.length === 0 ? (
                              <p className="field-help">正在读取地点目录…</p>
                            ) : poiResults.length > 0 ? (
                              <>
                                <div
                                  className="poi-catalog-list"
                                  role="group"
                                  aria-labelledby="mustLabel"
                                >
                                  {poiResults.map((poi) => {
                                    const isSelected = draft.mustInclude.some(
                                      (p) => p.placeId === poi.place_id,
                                    );
                                    const full =
                                      draft.mustInclude.length >=
                                      MOBILE_MUST_INCLUDE_LIMIT;
                                    const meta = [poi.district, poi.place_type]
                                      .filter(Boolean)
                                      .join(" · ");
                                    return (
                                      <button
                                        key={poi.place_id}
                                        type="button"
                                        className="chip"
                                        aria-pressed={isSelected}
                                        disabled={!isSelected && full}
                                        onClick={() =>
                                          handleToggleMustInclude({
                                            placeId: poi.place_id,
                                            name: poi.name,
                                          })
                                        }
                                      >
                                        {poi.name}
                                        {meta ? (
                                          <span className="poi-meta">
                                            {meta}
                                          </span>
                                        ) : null}
                                      </button>
                                    );
                                  })}
                                </div>
                                {poiNextAfterId !== null && cleanSelectedCity ? (
                                  <button
                                    type="button"
                                    className="text-btn poi-load-more"
                                    disabled={poiLoading}
                                    onClick={() =>
                                      void loadPoiCatalogPage(
                                        cleanSelectedCity,
                                        poiQueryInput.trim(),
                                        poiNextAfterId,
                                        true,
                                      )
                                    }
                                  >
                                    {poiLoading ? "加载中…" : "再看一批地点"}
                                  </button>
                                ) : null}
                              </>
                            ) : poiSearchedCity ? (
                              <p className="field-help">
                                地点目录里暂时没有匹配的地点
                              </p>
                            ) : null}
                        </>
                      </div>

                      <div className="field">
                        <label
                          className="field-label"
                          htmlFor="accommodationInput"
                        >
                          住在哪一带
                        </label>
                        <input
                          type="text"
                          id="accommodationInput"
                          className="input"
                          name="accommodation"
                          autoComplete="off"
                          maxLength={SPREAD_START_ACCOMMODATION_MAX}
                          placeholder="如：解放碑附近或具体酒店（选填）…"
                          value={draft.accommodation}
                          onChange={(e) => {
                            setSubmitError(null);
                            const value = e.target.value.slice(
                              0,
                              SPREAD_START_ACCOMMODATION_MAX,
                            );
                            setDraft((prev) => ({
                              ...prev,
                              revision: prev.revision + 1,
                              accommodation: value,
                              updatedAt: Date.now(),
                            }));
                          }}
                        />
                      </div>

                      <div className="field">
                        <label
                          className="field-label"
                          htmlFor="draftNoteInput"
                        >
                          还有什么要交代
                        </label>
                        <input
                          type="text"
                          id="draftNoteInput"
                          className="input"
                          name="note"
                          autoComplete="off"
                          maxLength={SPREAD_START_NOTES_MAX}
                          placeholder="如：第一天下午两点才到…"
                          value={draft.rawNotes}
                          onChange={(e) => {
                            setSubmitError(null);
                            const value = e.target.value.slice(
                              0,
                              SPREAD_START_NOTES_MAX,
                            );
                            setDraft((prev) => ({
                              ...prev,
                              revision: prev.revision + 1,
                              rawNotes: value,
                              updatedAt: Date.now(),
                            }));
                          }}
                        />
                      </div>
                    </div>
                  </details>
                </>
              ) : null}
            </div>

            <footer className="side-foot">
              {draft.step > 0 ? (
                <button
                  type="button"
                  className="turn"
                  onClick={() =>
                    handleGoStep((draft.step - 1) as 0 | 1 | 2 | 3)
                  }
                >
                  <ArrowLeftIcon />
                  上一步
                </button>
              ) : (
                <span aria-hidden="true" />
              )}
              <div className="foot-end">
                {draft.step === 0 ? (
                  <span className="foot-note">{`已挑 ${draft.moments.length} / 4`}</span>
                ) : null}
                {draft.step === 3 ? renderStep4FooterNote() : null}
                <button
                  type="button"
                  className="btn-primary"
                  disabled={primaryDisabled}
                  onClick={handlePrimaryAction}
                >
                  {primaryCtaLabel}
                  <ArrowRightIcon />
                </button>
              </div>
            </footer>
          </section>
        </div>
      </section>
    </main>
  );
}
