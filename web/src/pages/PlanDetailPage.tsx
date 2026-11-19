/**
 * 方案详情（杂志阅读流）
 * 路由：/plan/:resultId/:planId
 */
import { useEffect, useMemo, useState, lazy, Suspense, useRef } from "react";
import { useNavigate, useParams, useSearchParams, Link } from "react-router-dom";
import gsap from "gsap";
import { ScrollTrigger } from "gsap/ScrollTrigger";
import { DetailSkeleton } from "@/components/skeleton/DetailSkeleton";
import { TransportCard } from "@/components/detail/TransportCard";
import { WeatherStrip } from "@/components/detail/WeatherStrip";
import { TripSpine } from "@/components/detail/TripSpine";
import { PlaceDetailModal } from "@/components/detail/PlaceDetailModal";
import { CollapsibleSection } from "@/components/detail/CollapsibleSection";
import { DayCard } from "@/components/detail/DayCard";
import { ShareDialog } from "@/components/share/ShareDialog";
import { useTripStore } from "@/stores/tripStore";
import { fetchResult } from "@/services/api";
import { CostEstimateCard } from "@/components/detail/CostEstimateCard";
import { PreTripAdviceSection } from "@/components/detail/PreTripAdviceSection";
import { filterPackingChecklist, filterTravelTips } from "@/utils/preTripAdvice";
import { MustIncludeNotice } from "@/components/detail/MustIncludeNotice";
import { SafeDeliveryNotice } from "@/components/result/SafeDeliveryNotice";
import { QuotaSettlementNotice } from "@/components/result/QuotaSettlementNotice";
import { getScenarioCostStatus, type CostScenarioSummary } from "@/types/cost";
import { useArtifact } from "@/hooks/useArtifact";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import { MobilePlanView } from "@/pages/mobile/MobilePlanView";
import { saveBlob } from "@/utils/download";
import { showToast } from "@/stores/toastStore";
import { useCityPhotos } from "@/hooks/useCityPhotos";
import { weatherIcon } from "@/constants/weather";
import {
  cleanTags,
  cleanSummary,
} from "@/utils/format";
import { timePreferencesLabel } from "@/utils/schedule";
import {
  classifyResultOpenError,
  missingLinkError,
  type ResultOpenError,
} from "@/utils/planLinkError";
import type { TripDay, TripPlace, TripPlan, TripResult, WeatherDay } from "@/types/trip";
import {
  Compass,
  MapPinned,
  MapPin,
  LoaderCircle,
  FileText,
  Sparkles,
  DraftingCompass,
  CloudSun,
  Map,
  X,
  Link2Off,
  SearchX,
  FileClock,
  TriangleAlert,
} from "lucide-react";

gsap.registerPlugin(ScrollTrigger);

const MapView = lazy(() =>
  import("@/components/detail/MapView").then((m) => ({ default: m.MapView })),
);

const PACE_LABEL: Record<string, string> = {
  RELAXED: "轻松",
  MODERATE: "适中",
  INTENSIVE: "紧凑",
  PACKED: "紧凑",
};

function dayWeatherLabel(
  day: number,
  weatherDays: WeatherDay[] | undefined,
): string | null {
  const w = weatherDays?.find((d) => d.day === day);
  if (!w) return null;
  return `${weatherIcon(w.icon_code)} ${w.temp_max_c}°C`;
}

export default function PlanDetailPage() {
  const { resultId, planId } = useParams<{ resultId: string; planId: string }>();
  const [searchParams] = useSearchParams();
  const jobId = searchParams.get("job_id");
  const navigate = useNavigate();
  const storeResult = useTripStore((s) => s.result);
  const setResult = useTripStore((s) => s.setResult);

  const isMobile = useIsMobileViewport();
  const [fetchedResult, setFetchedResult] = useState<TripResult | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<ResultOpenError | null>(null);

  const [activeTab, setActiveTab] = useState("overview");
  const [showMap, setShowMap] = useState(false);
  const [mapDay, setMapDay] = useState<number | "all">("all");
  const [activePlaceId, setActivePlaceId] = useState<number | null>(null);
  const [detailPlace, setDetailPlace] = useState<TripPlace | null>(null);
  const isShareParam = searchParams.get("share") === "1";
  const [shareOpen, setShareOpen] = useState(isShareParam);
  const [expandedCommutes, setExpandedCommutes] = useState<Set<string>>(new Set());

  // 攻略详情页折叠/展开状态管理
  const [expandedDays, setExpandedDays] = useState<Set<number>>(new Set([1]));
  const [preTripExpanded, setPreTripExpanded] = useState<boolean>(false);
  const [expandedNarratives, setExpandedNarratives] = useState<Set<number>>(new Set());

  const toggleDay = (dayNum: number) => {
    setExpandedDays((prev) => {
      const next = new Set(prev);
      if (next.has(dayNum)) {
        next.delete(dayNum);
      } else {
        next.add(dayNum);
      }
      return next;
    });
  };

  const togglePreTrip = () => {
    setPreTripExpanded((prev) => !prev);
  };

  const toggleNarrative = (dayNum: number) => {
    setExpandedNarratives((prev) => {
      const next = new Set(prev);
      if (next.has(dayNum)) {
        next.delete(dayNum);
      } else {
        next.add(dayNum);
      }
      return next;
    });
  };

  const toggleCommute = (key: string) => {
    setExpandedCommutes((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  useEffect(() => {
    if (isShareParam) {
      setShareOpen(true);
    }
  }, [isShareParam]);

  const [isScrolled, setIsScrolled] = useState(false);
  const leftBrandRef = useRef<HTMLDivElement>(null);
  const rightUserMenuRef = useRef<HTMLDivElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const onScroll = () => {
      setIsScrolled(window.scrollY > 120);
    };
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, []);

  useEffect(() => {
    if (!leftBrandRef.current || !rightUserMenuRef.current) return;
    const isMobile = window.innerWidth < 768;
    if (isMobile) return;

    if (isScrolled) {
      gsap.to(leftBrandRef.current, {
        opacity: 1,
        x: 0,
        scale: 1,
        duration: 0.45,
        ease: "back.out(1.4)",
        overwrite: "auto",
      });
      gsap.to(rightUserMenuRef.current, {
        opacity: 1,
        x: 0,
        scale: 1,
        duration: 0.45,
        ease: "back.out(1.4)",
        overwrite: "auto",
      });
    } else {
      gsap.to(leftBrandRef.current, {
        opacity: 0,
        x: -16,
        scale: 0.9,
        duration: 0.25,
        ease: "power2.in",
        overwrite: "auto",
      });
      gsap.to(rightUserMenuRef.current, {
        opacity: 0,
        x: 16,
        scale: 0.9,
        duration: 0.25,
        ease: "power2.in",
        overwrite: "auto",
      });
    }
  }, [isScrolled]);

  const pdf = useArtifact(resultId, "pdf");

  const matched =
    storeResult &&
    String(storeResult.resultId) === String(resultId) &&
    storeResult.jobId === (jobId ?? "");
  const result = matched ? storeResult.data : fetchedResult;

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
    // 旧书签/损坏链接：缺 resultId 或 job_id 时不发请求，也不把后端 422 原文当人话展示。
    // job_id 是 BFF 的必填查询参数（GET /api/trip/results/{result_record_id}）。
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

  const plan: TripPlan | undefined = result?.plans?.find(
    (p) => p.plan_id === planId,
  );
  const validPackingList = useMemo(
    () => filterPackingChecklist(plan?.packing_checklist),
    [plan?.packing_checklist],
  );
  const validTips = useMemo(
    () => filterTravelTips(plan?.travel_tips),
    [plan?.travel_tips],
  );
  const hasPreTripAdvice = validPackingList.length > 0 || validTips.length > 0;
  const totalPackingItems = useMemo(
    () => validPackingList.reduce((acc, g) => acc + g.items.length, 0),
    [validPackingList],
  );
  const people = result?.request.people_count ?? 1;

  const [activeScenarioId, setActiveScenarioId] = useState<string | null>(null);

  const costEstimate = plan?.cost_estimate;
  const scenarios = useMemo(() => costEstimate?.scenarios ?? [], [costEstimate]);
  const scenarioIds = scenarios.map((s) => s.scenario_id).join(",");

  // 单场景或 without_intercity 场景时自动选定，双大交通场景时默认优先选择高铁场景
  useEffect(() => {
    if (scenarios.length === 1 && scenarios[0]?.scenario_id) {
      setActiveScenarioId(scenarios[0].scenario_id);
    } else if (scenarios.length > 1) {
      // Default to train scenario when both exist
      const trainScenario = scenarios.find((s) => s.scenario_id.includes("train"));
      setActiveScenarioId(trainScenario?.scenario_id ?? scenarios[0].scenario_id);
    }
  }, [plan?.plan_id, scenarioIds, scenarios]);

  const activeScenario: CostScenarioSummary | undefined = scenarios.find(
    (s) => s.scenario_id === activeScenarioId,
  ) ?? (scenarios.length === 1 ? scenarios[0] : undefined);

  // Derive selected transport mode from activeScenarioId
  const selectedTransportMode = activeScenarioId?.includes("train")
    ? "train"
    : activeScenarioId?.includes("flight")
      ? "flight"
      : null;

  // Handle transport mode selection -> update scenario
  const handleTransportModeSelect = (mode: "train" | "flight") => {
    const scenarioId = mode === "train" ? "train_round_trip" : "flight_round_trip";
    // Only switch if that scenario exists
    const exists = scenarios.some((s) => s.scenario_id === scenarioId);
    if (exists) {
      setActiveScenarioId(scenarioId);
    }
  };

  const isDualMode = scenarios.length > 1;
  const isUnselected = isDualMode && !activeScenarioId;
  const heroCostStatus = getScenarioCostStatus(activeScenario, isUnselected);
  const city = result?.city.name ?? "";

  const activeDayNumber = useMemo(() => {
    if (activeTab.startsWith("day-")) {
      const n = Number(activeTab.replace("day-", ""));
      if (!Number.isNaN(n)) return n;
    }
    return typeof mapDay === "number" ? mapDay : 1;
  }, [activeTab, mapDay]);

  // PDF 准备就绪自动下载 / 失败提示
  useEffect(() => {
    if (pdf.phase === "ready" && pdf.blob) {
      saveBlob(pdf.blob, `云途行程路书_${city || ""}_${plan?.title || "方案"}.pdf`);
      showToast("PDF 导出成功，已自动开始下载", "success");
      pdf.reset();
    } else if (pdf.phase === "failed" && pdf.error) {
      showToast(`PDF 导出失败: ${pdf.error.message}`, "error");
      pdf.reset();
    }
  }, [pdf.phase, pdf.blob, pdf.error, city, plan?.title, pdf]);
  const cityCovers = useCityPhotos(city || "");
  const weatherDays =
    result?.weather?.status === "ok" && result.weather.days.length > 0
      ? result.weather.days
      : undefined;
  const timePrefText = result
    ? timePreferencesLabel(result.schema_version, result.time_preferences)
    : null;

  const dayForMap: TripDay | undefined =
    typeof mapDay === "number"
      ? plan?.days?.find((d) => d.day === mapDay)
      : plan?.days?.[0];

  // plan 就绪后，初始化地图日为 "all" 全程总览
  useEffect(() => {
    if (plan?.days && plan.days.length > 0) setMapDay("all");
  }, [plan?.plan_id, plan?.days]);

  // GSAP 微动效（数据就绪后绑定）
  useEffect(() => {
    if (loading || !plan) return;
    const timeout = setTimeout(() => {
      const ctx = gsap.context(() => {
        gsap.utils.toArray<HTMLElement>(".reveal-up").forEach((el) => {
          gsap.fromTo(
            el,
            { y: 40, opacity: 0 },
            {
              y: 0,
              opacity: 1,
              duration: 0.8,
              ease: "power3.out",
              scrollTrigger: {
                trigger: el,
                start: "top 85%",
                toggleActions: "play none none reverse",
              },
            },
          );
        });
        gsap.utils.toArray<HTMLElement>(".parallax-img").forEach((img) => {
          gsap.to(img, {
            y: "15%",
            ease: "none",
            scrollTrigger: {
              trigger: img.parentElement,
              start: "top bottom",
              end: "bottom top",
              scrub: true,
            },
          });
        });
      }, containerRef);
      return () => ctx.revert();
    }, 100);
    return () => clearTimeout(timeout);
  }, [loading, plan]);

  // 折叠/展开变动时刷新 GSAP ScrollTrigger，保证微动效与定位触发位置精准
  useEffect(() => {
    const raf = requestAnimationFrame(() => {
      ScrollTrigger.refresh();
    });
    const timer = setTimeout(() => {
      ScrollTrigger.refresh();
    }, 350);
    return () => {
      cancelAnimationFrame(raf);
      clearTimeout(timer);
    };
  }, [expandedDays, preTripExpanded, expandedNarratives, expandedCommutes]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && showMap) setShowMap(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [showMap]);


  // activeTab 变动时，在移动端横向胶囊导航中自动将高亮 Tab 居中平滑滚入视野
  useEffect(() => {
    if (!activeTab) return;
    const tabEl = document.getElementById(`nav-tab-${activeTab}`);
    if (tabEl && typeof tabEl.scrollIntoView === "function") {
      tabEl.scrollIntoView({
        behavior: "smooth",
        block: "nearest",
        inline: "center",
      });
    }
  }, [activeTab]);

  // 100% 精准 ScrollSpy：结合 getBoundingClientRect 判定当前视野中的 section
  useEffect(() => {
    if (!plan) return;

    const sectionIds = [
      "overview",
      ...(hasPreTripAdvice ? ["pretrip-advice"] : []),
      ...plan.days.map((d) => `day-${d.day}`),
      "cost-estimate",
    ];

    let ticking = false;

    const handleScroll = () => {
      if (ticking) return;
      ticking = true;

      requestAnimationFrame(() => {
        ticking = false;
        let currentId = sectionIds[0];
        const offset = window.innerHeight * 0.4;
        const isBottom = window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 60;
        if (isBottom) {
          currentId = sectionIds[sectionIds.length - 1];
        } else {
          for (const id of sectionIds) {
            const el = document.getElementById(id);
            if (!el) continue;
            const rect = el.getBoundingClientRect();
            if (rect.top <= offset) {
              currentId = id;
            }
          }
        }

        if (currentId) {
          setActiveTab((prev) => (prev !== currentId ? currentId : prev));
          if (currentId.startsWith("day-")) {
            const n = Number(currentId.replace("day-", ""));
            if (!Number.isNaN(n)) {
              setMapDay((prev) => (prev !== n ? n : prev));
            }
          }
        }
      });
    };

    window.addEventListener("scroll", handleScroll, { passive: true });
    handleScroll();

    return () => {
      window.removeEventListener("scroll", handleScroll);
    };
  }, [hasPreTripAdvice, plan]);

  function scrollTo(id: string) {
    setActiveTab(id);

    // 自动展开目标天或行前准备区域
    if (id === "pretrip-advice") {
      setPreTripExpanded(true);
    } else if (id.startsWith("day-")) {
      const n = Number(id.replace("day-", ""));
      if (!Number.isNaN(n)) {
        setMapDay(n);
        setExpandedDays((prev) => {
          if (prev.has(n)) return prev;
          const next = new Set(prev);
          next.add(n);
          return next;
        });
      }
    }

    // 保证展开状态更新后平滑滚动至视口目标
    requestAnimationFrame(() => {
      const el = document.getElementById(id);
      if (el && typeof el.scrollIntoView === "function") {
        el.scrollIntoView({ behavior: "smooth", block: "start" });
      }
    });
  }

  function openPlace(placeId: number) {
    if (!plan) return;
    setActivePlaceId(placeId);
    const found =
      plan.days.flatMap((d) => d.places).find((p) => p.place_id === placeId) ??
      null;
    if (found) {
      setShowMap(false);
      setDetailPlace(found);
    }
  }

  if (loading) return <DetailSkeleton />;

  if (error || !result || !plan) {
    const kind = error?.kind ?? "generic";
    // 缺参数的旧链接用中性 lucide 图标 + 人话；其它分支保持原有单一按钮行为
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
      <div className="empty-state animate-fade-in px-5">
        <span className="empty-state-icon" aria-hidden="true">
          <Icon size={40} strokeWidth={1.5} />
        </span>
        <p className="max-w-xs text-sm font-medium text-primary-700">{message}</p>
        {kind === "invalidlink" ? (
          <div className="mt-2 flex flex-wrap items-center justify-center gap-3">
            <button
              onClick={() => navigate("/history")}
              className="btn-primary"
            >
              回到历史记录
            </button>
            <button onClick={() => navigate("/")} className="btn-secondary">
              重新规划
            </button>
          </div>
        ) : (
          <button onClick={() => navigate("/")} className="btn-primary mt-2">
            {kind === "unsupported" ? "重新生成" : "重新规划"}
          </button>
        )}
      </div>
    );
  }

  if (isMobile) {
    return (
      <>
        <MobilePlanView
          result={result}
          plan={plan}
          jobId={jobId}
          cityCovers={cityCovers}
          weatherDays={weatherDays}
          timePrefText={timePrefText}
          packing={validPackingList}
          tips={validTips}
          transportSlot={
            plan.transport ? (
              <TransportCard
                data={plan.transport}
                selectedMode={selectedTransportMode}
                onSelectMode={handleTransportModeSelect}
              />
            ) : null
          }
          costSlot={
            /* A5：同一张卡片的手机形态，只换颜色和字体，不重排结构 */
            <CostEstimateCard
              variant="mobile"
              costEstimate={plan.cost_estimate}
              activeScenarioId={activeScenarioId}
              onScenarioSelect={setActiveScenarioId}
            />
          }
          pdfLoading={pdf.loading}
          onPdf={() => pdf.start()}
          onShare={() => setShareOpen(true)}
          onBack={() => navigate(searchParams.get("from") === "history" ? "/history" : "/")}
          onOpenPlace={openPlace}
        />
        <PlaceDetailModal
          place={detailPlace}
          isMustInclude={Boolean(
            detailPlace &&
              result.must_include?.some(
                (mi) => mi.status === "scheduled" && mi.place_id != null && mi.place_id === detailPlace.place_id,
              ),
          )}
          onClose={() => setDetailPlace(null)}
        />
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

  return (
    <div
      ref={containerRef}
      className="min-h-screen bg-sand-50 font-body text-gray-800 selection:bg-primary-100"
    >
      {/* 第一层：页顶全局统一导航栏（全宽两端通栏） */}
      <nav className="flex w-full items-center justify-between px-5 py-3.5 sm:px-10 lg:px-14 border-b border-sand-200/80 bg-white/90 backdrop-blur-md sticky top-0 z-30 shadow-2xs">
        <div className="flex items-center space-x-6">
          <Link to="/" className="flex items-center space-x-2">
            <img src="/logo.svg" alt="云途 YunTu" className="h-8 w-8" />
            <span className="text-xl font-black tracking-tight text-gray-900">
              云途 <span className="font-light text-emerald-600 text-sm">YunTu</span>
            </span>
          </Link>

          <div className="hidden sm:flex items-center space-x-2 text-xs font-semibold">
            <Link
              to="/"
              className="inline-flex items-center gap-1.5 text-gray-600 hover:text-gray-900 px-3 py-1.5 rounded-lg hover:bg-sand-100 transition-colors"
            >
              <Compass size={11} className="text-gray-400" />
              <span>行程规划</span>
            </Link>
            <Link
              to="/history"
              className="inline-flex items-center gap-1.5 text-gray-600 hover:text-gray-900 px-3 py-1.5 rounded-lg hover:bg-sand-100 transition-colors"
            >
              <MapPinned size={11} className="text-gray-400" />
              <span>我的行程</span>
            </Link>
          </div>
        </div>

        <Link
          to="/history"
          className="inline-flex items-center gap-1.5 rounded-full border border-sand-200 bg-white px-3 py-1.5 text-xs font-medium text-gray-700 hover:bg-sand-50 transition-colors"
        >
          <MapPinned size={12} className="text-primary-600" />
          <span>我的行程</span>
        </Link>
      </nav>

      {/* Hero */}
      <header
        id="overview"
        className="mx-auto max-w-3xl scroll-mt-24 px-5 pb-8 pt-8 text-center sm:px-8 sm:pt-10 reveal-up"
      >
        <div className="mb-4 inline-flex items-center gap-2 rounded-full border border-primary-200/80 bg-primary-50/90 px-3.5 py-1 text-xs font-semibold tracking-wide text-primary-800 shadow-2xs">
          <MapPin size={11} className="text-primary-600" />
          <span>{city} · {result.request.days ? `${result.request.days} 天` : ""}{people ? ` · ${people} 人` : ""}{timePrefText ? ` · ${timePrefText}` : ""}</span>
        </div>
        <h1 className="font-display mb-4 text-3xl font-extrabold leading-tight tracking-tight text-gray-900 sm:text-5xl">
          {plan.title}
        </h1>
        {cleanSummary(plan.summary) && (
          <p className="mx-auto mb-6 max-w-2xl text-base leading-relaxed text-gray-600 sm:text-lg">
            “{cleanSummary(plan.summary)}”
          </p>
        )}
        <div className="flex flex-wrap justify-center gap-2">
          {cleanTags(plan.tags).map((tag) => (
            <span
              key={tag}
              className="rounded-full border border-primary-200/80 bg-white px-3 py-1 text-xs font-medium text-primary-800 shadow-2xs"
            >
              {tag}
            </span>
          ))}
          {plan.pace?.level && (
            <span className="rounded-full border border-accent-200 bg-accent-50 px-3 py-1 text-xs font-medium text-accent-700 shadow-2xs">
              节奏 {PACE_LABEL[plan.pace.level] ?? plan.pace.level}
            </span>
          )}
          {heroCostStatus.costText && heroCostStatus.costText !== "选择交通方式查看预估" && (
            <a
              href="#cost-estimate"
              onClick={(e) => {
                if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
                e.preventDefault();
                scrollTo("cost-estimate");
              }}
              className="rounded-full border border-emerald-200 bg-emerald-50 px-3 py-1 text-xs font-medium text-emerald-700 transition-colors hover:bg-emerald-100 flex xl:hidden items-center gap-1.5 shadow-2xs"
            >
              <span>
                {activeScenario?.label
                  ? `${activeScenario.label} · ${heroCostStatus.costText}`
                  : heroCostStatus.costText}
              </span>
              {heroCostStatus.costBadge && (
                <span className="rounded bg-amber-100 px-1.5 py-0.2 text-[10px] font-bold text-amber-800">
                  {heroCostStatus.costBadge}
                </span>
              )}
            </a>
          )}
          <span className="rounded-full border border-sand-200 bg-white px-3 py-1 text-xs text-gray-500 shadow-2xs">
            {plan.days.length} 日行程
          </span>
        </div>
      </header>

      {/* 必去地点异常提醒 banner */}
      <MustIncludeNotice items={result.must_include} />

      {/* 第二层：随屏吸顶工具栏（GSAP 物理弹性驱动 back.out(1.4) 缓动滑出） */}
      <div className="sticky top-0 z-40 border-b border-gray-100 bg-sand-50/95 shadow-sm shadow-gray-900/5 backdrop-blur-xl">
        <div className="flex h-14 w-full items-center justify-between gap-3 px-5 sm:px-10 lg:px-14">
          {/* 左侧：桌面端 GSAP 物理弹性滑出 Logo 与全局导航 */}
          <div
            ref={leftBrandRef}
            className="hidden md:flex shrink-0 items-center space-x-6 opacity-0"
            style={{ transform: "translateX(-16px) scale(0.9)" }}
          >
            <Link to="/" className="flex items-center space-x-2">
              <img src="/logo.svg" alt="云途 YunTu" className="h-7 w-7" />
              <span className="text-base font-black tracking-tight text-gray-900">
                云途 <span className="font-light text-emerald-600 text-xs">YunTu</span>
              </span>
            </Link>

            <div className="hidden lg:flex items-center space-x-2 text-xs font-semibold">
              <Link
                to="/"
                className="inline-flex items-center gap-1.5 text-gray-600 hover:text-gray-900 px-2.5 py-1 rounded-lg hover:bg-sand-100 transition-colors"
              >
                <Compass size={11} className="text-gray-400" />
                <span>行程规划</span>
              </Link>
              <Link
                to="/history"
                className="inline-flex items-center gap-1.5 text-gray-600 hover:text-gray-900 px-2.5 py-1 rounded-lg hover:bg-sand-100 transition-colors"
              >
                <MapPinned size={11} className="text-gray-400" />
                <span>我的行程</span>
              </Link>
            </div>
          </div>

          {/* 中间：章节胶囊 Tab 切换（移动端占据左侧主区支持横滑，桌面端居中） */}
          <div className="flex flex-1 min-w-0 items-center justify-start md:justify-center">
            <div className="flex items-center gap-1.5 overflow-x-auto hide-scrollbar py-1 w-full md:w-auto">
              {(
                [
                  ["overview", "概览"],
                  ...(hasPreTripAdvice ? [["pretrip-advice", "行前准备"]] : []),
                  ...plan.days.map((d) => [`day-${d.day}`, `第 ${d.day} 天`]),
                  ["cost-estimate", "出行费用"],
                ] as [string, string][]
              ).map(([id, label]) => (
                <button
                  key={id}
                  id={`nav-tab-${id}`}
                  type="button"
                  onClick={() => scrollTo(id)}
                  className={`shrink-0 rounded-full px-3 py-1 sm:px-3.5 text-[11px] font-bold tracking-wide transition-all duration-250 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary-300 ${
                    activeTab === id
                      ? "bg-gray-900 text-white shadow-md"
                      : "text-gray-600 hover:bg-gray-200/60 hover:text-gray-900"
                  }`}
                >
                  {label}
                </button>
              ))}
            </div>
          </div>

          {/* 右侧：动作按钮 (PDF / 分享) + 桌面端 GSAP 物理弹性滑出 UserMenu */}
          <div className="flex shrink-0 items-center gap-1.5 sm:gap-2">
            <button
              type="button"
              className="flex h-8 items-center justify-center gap-1 rounded-full border border-gray-200 bg-white px-2.5 sm:px-3 text-[11px] font-bold text-gray-700 shadow-2xs transition-all hover:border-gray-300 hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-60"
              title="导出 PDF"
              disabled={pdf.loading}
              onClick={() => pdf.start()}
            >
              {pdf.loading ? (
                <LoaderCircle size={14} className="animate-spin text-red-500" aria-hidden="true" />
              ) : (
                <FileText size={14} className="text-red-500" aria-hidden="true" />
              )}
              <span className="hidden xs:inline sm:inline">{pdf.loading ? "导出中" : "PDF"}</span>
            </button>
            <button
              type="button"
              className="flex h-8 items-center justify-center gap-1.5 rounded-full bg-primary-600 px-2.5 sm:px-3.5 text-[11px] font-bold text-white shadow-md shadow-primary-600/20 transition-all hover:scale-105 hover:bg-primary-700"
              title="分享 AI 长图"
              onClick={() => setShareOpen(true)}
            >
              <Sparkles size={14} className="text-primary-200" aria-hidden="true" />
              <span className="hidden sm:inline">分享 AI 长图</span>
              <span className="sm:hidden">分享</span>
            </button>

            {/* 桌面端向下滚动后，GSAP 物理弹性滑出我的行程入口 */}
            <div
              ref={rightUserMenuRef}
              className="hidden border-l border-gray-200/80 pl-2.5 opacity-0 md:block"
              style={{ transform: "translateX(16px) scale(0.9)" }}
            >
              <Link
                to="/history"
                className="inline-flex h-8 items-center gap-1 rounded-full border border-gray-200 bg-white px-3 text-[11px] font-bold text-gray-700 shadow-2xs transition-colors hover:bg-gray-50"
              >
                <MapPinned size={12} className="text-primary-600" aria-hidden="true" />
                <span>我的行程</span>
              </Link>
            </div>
          </div>
        </div>
      </div>

      <div className="mx-auto max-w-6xl px-4 sm:px-8 py-10 pb-36 sm:py-14 flex flex-col xl:flex-row items-start justify-center gap-8 min-w-0">
        <main className="w-full max-w-3xl space-y-10 shrink-0 min-w-0 order-1 xl:order-2">
          {result.weather && (
            <section className="reveal-up xl:hidden">
              <WeatherStrip data={result.weather} />
            </section>
          )}

          {hasPreTripAdvice && (
            <CollapsibleSection
              id="pretrip-advice"
              ariaLabel="行前准备"
              expanded={preTripExpanded}
              onToggle={togglePreTrip}
              title={
                <div className="flex items-center gap-2.5">
                  <span className="flex h-7 w-7 items-center justify-center rounded-xl bg-primary-100 text-primary-700 text-xs shadow-2xs">
                    <DraftingCompass size={14} aria-hidden="true" />
                  </span>
                  <span className="text-base sm:text-xl font-extrabold text-gray-900 tracking-tight">
                    行前准备
                  </span>
                </div>
              }
              summary={
                totalPackingItems > 0 || validTips.length > 0
                  ? `· ${totalPackingItems > 0 ? `${totalPackingItems}项必备` : ""}${totalPackingItems > 0 && validTips.length > 0 ? " · " : ""}${validTips.length > 0 ? `${validTips.length}条贴士` : ""}`
                  : undefined
              }
            >
              <PreTripAdviceSection
                packingChecklist={validPackingList}
                travelTips={validTips}
                hideHeader
              />
            </CollapsibleSection>
          )}

          {plan.days.map((day, dayIndex) => {
            const weatherLabel = dayWeatherLabel(day.day, weatherDays);
            const isExpanded = expandedDays.has(day.day);

            return (
              <CollapsibleSection
                key={day.day}
                id={`day-${day.day}`}
                ariaLabel={`第 ${day.day} 天 ${day.title}`}
                expanded={isExpanded}
                onToggle={() => toggleDay(day.day)}
                title={
                  <div className="flex items-center gap-2.5">
                    <span className="flex items-center gap-1.5 rounded-full bg-primary-600 px-3 py-1 font-display text-xs font-bold text-white shadow-xs tracking-wider">
                      <span className="inline-block h-1.5 w-1.5 rounded-full bg-emerald-300 animate-pulse" />
                      DAY {String(day.day).padStart(2, "0")}
                    </span>
                    <span className="text-base sm:text-xl font-extrabold text-gray-900 tracking-tight">
                      {day.title}
                    </span>
                  </div>
                }
                summary={`· ${day.places.length}处游览点`}
                headerRight={
                  weatherLabel ? (
                    <span className="hidden xs:inline-flex items-center gap-1.5 rounded-full border border-amber-200/80 bg-amber-50/90 px-2.5 py-0.5 text-xs font-semibold text-amber-800 shadow-2xs">
                      <CloudSun size={12} className="text-amber-500" />
                      <span>{weatherLabel}</span>
                    </span>
                  ) : null
                }
              >
                <DayCard
                  day={day}
                  dayIndex={dayIndex}
                  city={city}
                  cityCover={cityCovers[dayIndex % cityCovers.length]}
                  accommodation={plan.accommodation}
                  mustInclude={result.must_include}
                  activePlaceId={activePlaceId}
                  onPlaceClick={openPlace}
                  onAccommodationLocationClick={() => {
                    setShowMap(true);
                    setActivePlaceId(null);
                  }}
                  expandedCommutes={expandedCommutes}
                  onToggleCommute={toggleCommute}
                  narrativeExpanded={expandedNarratives.has(day.day)}
                  onToggleNarrative={() => toggleNarrative(day.day)}
                />
              </CollapsibleSection>
            );
          })}

        {/* 出行与费用 附录区 */}
        <section
          id="cost-estimate"
          className="scroll-mt-24 border-t border-primary-100/50 pt-8 reveal-up space-y-8"
        >
          <div className="border-b border-gray-100 pb-3">
            <span className="text-xs font-bold uppercase tracking-widest text-primary-600">
              TRAVEL & COST APPENDIX
            </span>
            <h2 className="font-display text-3xl font-bold text-gray-900">
              出行与费用
            </h2>
          </div>

          {/* 大交通推荐卡片（放在费用模块前面） */}
          {plan.transport && (
            <div className="space-y-2">
              <TransportCard
                data={plan.transport}
                selectedMode={selectedTransportMode}
                onSelectMode={handleTransportModeSelect}
              />
            </div>
          )}

          {/* 完整费用模块 */}
          <CostEstimateCard
            costEstimate={plan.cost_estimate}
            activeScenarioId={activeScenarioId}
            onScenarioSelect={setActiveScenarioId}
          />
        </section>
        <div className="space-y-4">
          <SafeDeliveryNotice result={result} jobId={jobId} />
          <QuotaSettlementNotice delivery={result.delivery} />
        </div>
      </main>

        {/* 行程脊柱 (PC ≥1280px) 放置在 DOM 顺序后方，但在视觉上呈现在正文左侧并固定跟随滑动 */}
        <div className="order-2 xl:order-1 shrink-0 xl:sticky xl:top-24">
          <TripSpine
            days={plan.days}
            weather={result.weather}
            costEstimate={plan.cost_estimate}
            activeScenarioId={activeScenarioId}
            activeDay={activeDayNumber}
            packingChecklist={validPackingList}
            travelTips={validTips}
            onDayClick={(dayNum) => scrollTo(`day-${dayNum}`)}
            onCostClick={() => scrollTo("cost-estimate")}
            onAdviceClick={() => scrollTo("pretrip-advice")}
          />
        </div>
      </div>

      {/* 地图 FAB */}
      <div className="fixed bottom-8 right-5 z-50 flex flex-col items-end gap-3 sm:right-8" style={{ paddingBottom: "env(safe-area-inset-bottom, 0px)" }}>
        <button
          type="button"
          onClick={() => {
            setShowMap(true);
            setActivePlaceId(dayForMap?.places[0]?.place_id ?? null);
          }}
          className="flex items-center gap-2.5 rounded-full bg-primary-600 py-3.5 pl-5 pr-6 text-white shadow-xl shadow-primary-600/25 transition-transform hover:scale-[1.03] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary-300 focus-visible:ring-offset-2"
        >
          <Map size={16} className="text-primary-100" aria-hidden="true" />
          <span className="text-xs font-bold tracking-wide">查看地图</span>
        </button>
      </div>

      {/* 地图浮层 */}
      <div
        onClick={() => setShowMap(false)}
        className={`fixed inset-0 z-[100] flex items-center justify-center p-4 transition-all duration-300 sm:p-6 ${
          showMap
            ? "pointer-events-auto bg-gray-900/60 opacity-100 backdrop-blur-sm"
            : "pointer-events-none opacity-0"
        }`}
        aria-hidden={!showMap}
      >
        <div
          onClick={(e) => e.stopPropagation()}
          className={`flex h-[85vh] w-full max-w-5xl flex-col overflow-hidden rounded-[2rem] border border-white/10 bg-gray-800 shadow-2xl transition-transform duration-300 ${
            showMap ? "translate-y-0 scale-100" : "translate-y-4 scale-95"
          }`}
          role="dialog"
          aria-modal="true"
        >
          <div className="z-10 flex items-center justify-between gap-3 border-b border-white/5 bg-gray-900/60 px-6 py-4 backdrop-blur-md">
            <div className="flex min-w-0 items-center gap-4">
              <h3 className="font-display truncate text-lg font-medium text-white">
                {city} 地图
              </h3>
              <div className="hidden flex-wrap gap-1.5 border-l border-white/10 pl-4 sm:flex">
                <button
                  type="button"
                  onClick={() => setMapDay("all")}
                  tabIndex={showMap ? 0 : -1}
                  className={`rounded-full px-3 py-1 text-[11px] font-bold tracking-wider transition-all focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white ${
                    mapDay === "all"
                      ? "bg-primary-500 text-white shadow-sm ring-1 ring-primary-300"
                      : "bg-white/5 text-white/60 hover:bg-white/10 hover:text-white"
                  }`}
                >
                  <Map size={10} className="mr-1 inline-block" />
                  全程总览
                </button>
                {plan.days.map((d) => (
                  <button
                    type="button"
                    key={d.day}
                    onClick={() => setMapDay(d.day)}
                    tabIndex={showMap ? 0 : -1}
                    className={`rounded-full px-3 py-1 text-[11px] font-bold uppercase tracking-widest transition-all focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white ${
                      mapDay === d.day
                        ? "bg-primary-500 text-white"
                        : "bg-white/5 text-white/50 hover:bg-white/10 hover:text-white"
                    }`}
                  >
                    第 {d.day} 天
                  </button>
                ))}
              </div>
            </div>
            <button
              type="button"
              onClick={() => setShowMap(false)}
              tabIndex={showMap ? 0 : -1}
              className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-white/10 text-white/80 transition-all hover:scale-105 hover:bg-white/20 hover:text-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white"
              aria-label="关闭地图"
            >
              <X size={16} aria-hidden="true" />
            </button>
          </div>

          <div className="flex gap-2 overflow-x-auto border-b border-white/5 bg-gray-900/40 px-4 py-3 hide-scrollbar sm:hidden">
            <button
              type="button"
              onClick={() => setMapDay("all")}
              tabIndex={showMap ? 0 : -1}
              className={`shrink-0 rounded-full px-3 py-1.5 text-[10px] font-bold tracking-wider focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white ${
                mapDay === "all"
                  ? "bg-primary-500 text-white shadow-sm ring-1 ring-primary-300"
                  : "bg-white/5 text-white/60"
              }`}
            >
              <Map size={10} className="mr-1 inline-block" />
              全程总览
            </button>
            {plan.days.map((d) => (
              <button
                type="button"
                key={d.day}
                onClick={() => setMapDay(d.day)}
                tabIndex={showMap ? 0 : -1}
                className={`shrink-0 rounded-full px-3 py-1.5 text-[10px] font-bold uppercase tracking-widest focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white ${
                  mapDay === d.day
                    ? "bg-primary-500 text-white"
                    : "bg-white/5 text-white/50"
                }`}
              >
                第 {d.day} 天
              </button>
            ))}
          </div>

          <div className="relative min-h-0 flex-1 bg-gray-800">
            {dayForMap && (
              <Suspense
                fallback={
                  <div className="flex h-full items-center justify-center text-sm uppercase tracking-widest text-white/40">
                    地图加载中…
                  </div>
                }
              >
                <MapView
                  day={dayForMap}
                  days={plan.days}
                  selectedDay={mapDay}
                  accommodation={plan.accommodation}
                  activePlaceId={activePlaceId}
                  onMarkerClick={(id) => openPlace(id)}
                />
              </Suspense>
            )}
          </div>

          <div className="border-t border-white/5 bg-gray-900/60 px-6 py-2 backdrop-blur-md">
            <p className="text-center text-[10px] uppercase tracking-widest text-white/40">
              点击标记查看地点详情
            </p>
          </div>
        </div>
      </div>

      <PlaceDetailModal
        place={detailPlace}
        isMustInclude={Boolean(
          detailPlace &&
            result?.must_include?.some(
              (mi) => mi.status === "scheduled" && mi.place_id != null && mi.place_id === detailPlace.place_id,
            ),
        )}
        onClose={() => setDetailPlace(null)}
      />
      {resultId && (
        <ShareDialog
          open={shareOpen}
          onClose={() => setShareOpen(false)}
          recordId={resultId}
          jobId={jobId ?? undefined}
        />
      )}
    </div>
  );
}
