/* eslint-disable @typescript-eslint/no-explicit-any */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import AMapLoader from "@amap/amap-jsapi-loader";
import { TOTAL_STAGES } from "@/constants/stages";
import { getNoRouteExplanation } from "@/constants/delivery";
import { webErrorMessage } from "@/constants/errors";
import { useDestinations } from "@/hooks/useDestinations";
import { useJobProgress } from "@/hooks/useJobProgress";
import {
  ApiRequestError,
  fetchResult,
  pollJobStatus,
  submitTrip,
} from "@/services/api";
import { showToast } from "@/stores/toastStore";
import {
  CONSECUTIVE_ERROR_LIMIT,
  MAX_POLLING_DURATION_MS,
  useTripTaskStore,
} from "@/stores/tripTaskStore";
import { useTripStore } from "@/stores/tripStore";
import type { JobResponse, PublicGenerationDelivery } from "@/types/trip";
import {
  clearPendingSubmission,
  savePendingSubmission,
} from "@/utils/pendingSubmission";
import { findOriginalRetryInput } from "@/utils/retryInput";
import { isValidTripJobId } from "@/utils/tripJobId";
import {
  CHINA_FALLBACK_CENTER,
  SKY_STAGES,
  buildCompletedRedirectUrl,
  buildPlanningStateCopy,
  computeBindOffsetCenter,
  computeTimeoutMinutes,
  extractPlanningConditionSummary,
  formatElapsedClock,
  normalizePlanningStep,
  resolveMatchedFormData,
  resolvePlanningCityName,
  resolvePlanningCoordinates,
  resolvePlanningZoomLevel,
  resolveStripProgressPct,
  syncPlanningTaskRecord,
  type PlanningCoordinates,
  type SpreadPlanningVisualState,
} from "@/utils/spreadPlanning";
import "./spread-planning.css";

const AMAP_LOAD_TIMEOUT_MS = 25_000;
let inFlightAmapLoad: Promise<any> | null = null;
let inFlightAmapToken: object | null = null;

/**
 * 在 React StrictMode 双挂载或网络异常时共享同一次 SDK 加载，
 * 规避 @amap/amap-jsapi-loader 并发等待队列在 script onerror 时挂死的问题。
 */
function loadAmapSdk(key: string): Promise<any> {
  if (typeof window !== "undefined" && (window as any).AMap) {
    return Promise.resolve((window as any).AMap);
  }
  if (inFlightAmapLoad) {
    return inFlightAmapLoad;
  }

  let timeoutId: number | null = null;
  let settled = false;
  const token = {};
  inFlightAmapToken = token;

  const cleanup = () => {
    if (settled) return;
    settled = true;
    if (timeoutId !== null) {
      window.clearTimeout(timeoutId);
      timeoutId = null;
    }
    if (inFlightAmapToken === token) {
      inFlightAmapToken = null;
      inFlightAmapLoad = null;
    }
  };

  const resetOnFailure = () => {
    if (typeof window !== "undefined" && !(window as any).AMap) {
      (AMapLoader as any).reset?.();
    }
  };

  const loadPromise = new Promise<any>((resolve, reject) => {
    timeoutId = window.setTimeout(() => {
      if (settled) return;
      cleanup();
      resetOnFailure();
      reject(new Error("AMap SDK load timed out"));
    }, AMAP_LOAD_TIMEOUT_MS);

    Promise.resolve(AMapLoader.load({ key, version: "2.0" }))
      .then((AMap) => {
        if (settled) return;
        cleanup();
        if (!AMap) {
          reject(new Error("AMap SDK unavailable"));
          return;
        }
        resolve(AMap);
      })
      .catch((err) => {
        if (settled) return;
        cleanup();
        resetOnFailure();
        reject(err);
      });
  });

  inFlightAmapLoad = loadPromise;
  return loadPromise;
}

function prefersReducedMotion(): boolean {
  if (
    typeof window === "undefined" ||
    typeof window.matchMedia !== "function"
  ) {
    return false;
  }
  return Boolean(
    window.matchMedia("(prefers-reduced-motion: reduce)")?.matches,
  );
}

function ArrowLeftIcon() {
  return (
    <svg className="icon" viewBox="0 0 24 24" aria-hidden="true">
      <path d="M19 12H5M11 6l-6 6 6 6" />
    </svg>
  );
}

function ArrowRightIcon() {
  return (
    <svg className="icon" viewBox="0 0 24 24" aria-hidden="true">
      <path d="M5 12h14M13 6l6 6-6 6" />
    </svg>
  );
}

/**
 * 独立计时叶子组件：每秒只更新“已等 mm:ss”，避免重绘长卷或重置 CSS 过渡。
 */
function ElapsedClock({
  startedAt,
  paused,
  suffix = " · 一般要几分钟",
}: {
  startedAt: number;
  paused: boolean;
  suffix?: string;
}) {
  const [elapsedSec, setElapsedSec] = useState(() =>
    Math.max(0, Math.floor((Date.now() - startedAt) / 1000)),
  );

  useEffect(() => {
    setElapsedSec(Math.max(0, Math.floor((Date.now() - startedAt) / 1000)));
    if (paused) return;
    const id = window.setInterval(() => {
      setElapsedSec(Math.max(0, Math.floor((Date.now() - startedAt) / 1000)));
    }, 1000);
    return () => window.clearInterval(id);
  }, [startedAt, paused]);

  return (
    <span className="bd-clock">
      已等 {formatElapsedClock(elapsedSec)}
      {suffix}
    </span>
  );
}

function InvalidJobView({ onBack }: { onBack: () => void }) {
  return (
    <main className="bd-invalid">
      <section className="bd-invalid-card">
        <h1>没有可查看的生成任务</h1>
        <p>未收到有效任务编号，请返回出发页检查行程状态。</p>
        <button type="button" className="bd-invalid-btn" onClick={onBack}>
          返回出发
        </button>
      </section>
    </main>
  );
}

export default function SpreadPlanningPage() {
  const { jobId } = useParams<{ jobId: string }>();
  const navigate = useNavigate();

  if (!isValidTripJobId(jobId)) {
    return <InvalidJobView onBack={() => navigate("/")} />;
  }

  return <ValidSpreadPlanningPage key={jobId} jobId={jobId} />;
}

function ValidSpreadPlanningPage({ jobId }: { jobId: string }) {
  const navigate = useNavigate();
  const mountTimeRef = useRef<number>(Date.now());
  const unmountedRef = useRef(false);

  const formData = useTripStore((s) => s.formData);
  const currentJobId = useTripStore((s) => s.currentJobId);
  const setJob = useTripStore((s) => s.setJob);
  const setResult = useTripStore((s) => s.setResult);
  const clearResult = useTripStore((s) => s.clearResult);

  const storedTask = useTripTaskStore((s) => s.tasks[jobId]);
  const budget = useTripTaskStore((s) => s.budgets[jobId]);
  const isPaused = Boolean(budget?.isPaused);

  const { data: destinationsData, isError: destinationsError } =
    useDestinations();

  const [knownStep, setKnownStep] = useState<number | null>(null);
  const [delivery, setDelivery] = useState<PublicGenerationDelivery | null>(
    null,
  );
  const [failed, setFailed] = useState(false);
  const [noRoute, setNoRoute] = useState(false);
  const [errorCode, setErrorCode] = useState<string | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [timedOut, setTimedOut] = useState(false);
  const [unknownState, setUnknownState] = useState(false);
  const [completed, setCompleted] = useState(false);
  const [completedTarget, setCompletedTarget] = useState<string | null>(null);
  const [networkUnstable, setNetworkUnstable] = useState(false);
  const [retrying, setRetrying] = useState(false);
  const [rechecking, setRechecking] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  const retrySubmissionInFlight = useRef(false);
  const completedLockRef = useRef(false);
  const autoNavTimerRef = useRef<number | null>(null);

  const startedAt = storedTask?.startedAt ?? mountTimeRef.current;

  const cityName = useMemo(
    () =>
      resolvePlanningCityName({
        taskDestination: storedTask?.destination,
        tripCurrentJobId: currentJobId,
        jobId,
        formCity: formData?.to_city,
      }),
    [storedTask?.destination, currentJobId, jobId, formData?.to_city],
  );

  const matchedFormData = useMemo(
    () => resolveMatchedFormData(formData, currentJobId, jobId),
    [formData, currentJobId, jobId],
  );

  const conditionSummary = useMemo(
    () => extractPlanningConditionSummary(matchedFormData),
    [matchedFormData],
  );

  const coordinates: PlanningCoordinates | null = useMemo(() => {
    if (destinationsError) return null;
    return resolvePlanningCoordinates(cityName, destinationsData?.destinations);
  }, [cityName, destinationsData?.destinations, destinationsError]);

  const hasCoordinates = Boolean(coordinates);
  const authoritativeDelivery = delivery ?? storedTask?.delivery ?? null;

  const visualState: SpreadPlanningVisualState = useMemo(() => {
    if (completed) return "completed";
    if (noRoute) return "no_route";
    if (failed) return "failed";
    if (timedOut || (isPaused && budget?.pauseReason === "timeout")) {
      return "timeout";
    }
    if (
      unknownState ||
      (isPaused && budget?.pauseReason === "consecutive_errors")
    ) {
      return "unknown";
    }
    return "binding";
  }, [
    completed,
    noRoute,
    failed,
    timedOut,
    isPaused,
    budget?.pauseReason,
    unknownState,
  ]);

  const timeoutMinutes = computeTimeoutMinutes(MAX_POLLING_DURATION_MS);

  const stateCopy = useMemo(
    () =>
      buildPlanningStateCopy({
        visualState,
        cityName,
        days: matchedFormData?.days,
        knownStep,
        delivery: authoritativeDelivery,
        errorCode,
        errorMessage,
        timeoutMinutes,
      }),
    [
      visualState,
      cityName,
      matchedFormData?.days,
      knownStep,
      authoritativeDelivery,
      errorCode,
      errorMessage,
      timeoutMinutes,
    ],
  );

  const beginCompletedTransition = useCallback(
    (data: JobResponse) => {
      if (completedLockRef.current || !data.result_record_id) return;
      completedLockRef.current = true;
      setCompleted(true);
      setTimedOut(false);
      setUnknownState(false);
      setNetworkUnstable(false);
      setActionError(null);

      if (data.delivery) {
        setDelivery(data.delivery);
      }

      const nextStep = normalizePlanningStep(data.stage_progress);
      if (nextStep !== null) {
        setKnownStep(nextStep);
      }

      syncPlanningTaskRecord({
        jobId,
        data,
        destination: cityName,
        startedAt,
        terminalStatus: "ready",
        notificationState: "acknowledged",
      });

      const recordId = data.result_record_id;
      const reduce = prefersReducedMotion();
      const prefetch = fetchResult(recordId, jobId)
        .then((res) => {
          setResult(recordId, jobId, res);
          return res;
        })
        .catch(() => null);

      const go = async () => {
        const res = await prefetch;
        if (unmountedRef.current) return;
        const target = buildCompletedRedirectUrl(recordId, jobId, res);
        navigate(target, { replace: true });
      };

      void prefetch.then((res) => {
        if (unmountedRef.current) return;
        setCompletedTarget(buildCompletedRedirectUrl(recordId, jobId, res));
      });

      if (reduce) {
        void go();
      } else {
        autoNavTimerRef.current = window.setTimeout(() => {
          void go();
        }, 2600);
      }
    },
    [jobId, cityName, startedAt, setResult, navigate],
  );

  const onData = useCallback(
    (data: JobResponse): boolean => {
      useTripTaskStore.getState().recordTaskSuccess(jobId);
      setNetworkUnstable(false);

      if (data.delivery) {
        setDelivery(data.delivery);
        syncPlanningTaskRecord({
          jobId,
          data,
          destination: cityName,
          startedAt,
        });
      }

      const nextStep = normalizePlanningStep(data.stage_progress);
      if (nextStep !== null) {
        setKnownStep(nextStep);
        setJob(jobId, data.status, data.stage_progress);
      }

      const isNoRoute = data.delivery?.outcome === "NO_ROUTE";

      if (data.status === "COMPLETED" && data.result_record_id) {
        beginCompletedTransition(data);
        return true;
      }

      if (data.status === "FAILED" || isNoRoute) {
        if (data.error?.code === "GENERATION_STATUS_TIMEOUT") {
          setUnknownState(true);
          setErrorCode(data.error.code);
          setErrorMessage("暂时无法确认任务状态，任务可能仍在继续。");
          return true;
        }

        if (data.delivery) {
          setDelivery(data.delivery);
        }
        syncPlanningTaskRecord({
          jobId,
          data,
          destination: cityName,
          startedAt,
          terminalStatus: isNoRoute ? "no_route" : "failed",
          notificationState: "acknowledged",
          error: isNoRoute
            ? null
            : (data.error ?? {
                code: "GENERATION_FAILED",
                message: "生成失败，请重试",
              }),
        });
        if (isNoRoute) {
          setNoRoute(true);
          setFailed(false);
          setErrorMessage(
            getNoRouteExplanation(data.delivery?.reason_code ?? null),
          );
        } else {
          setFailed(true);
          setNoRoute(false);
          setErrorCode(data.error?.code ?? null);
          setErrorMessage(
            webErrorMessage(data.error?.code, data.error?.message),
          );
        }
        return true;
      }

      return false;
    },
    [jobId, cityName, startedAt, setJob, beginCompletedTransition],
  );

  // 任务归属声明与卸载释放 + 挂载超时核验
  useEffect(() => {
    unmountedRef.current = false;
    useTripTaskStore.getState().claimTaskOwner(jobId, "planning_page");
    const timedOutAtMount = useTripTaskStore.getState().checkTaskTimeout(jobId);
    if (timedOutAtMount) {
      setTimedOut(true);
    }
    return () => {
      unmountedRef.current = true;
      useTripTaskStore.getState().releaseTaskOwner(jobId, "planning_page");
      if (autoNavTimerRef.current !== null) {
        window.clearTimeout(autoNavTimerRef.current);
      }
    };
  }, [jobId]);

  // 停留在装订等待页时已直接看到终态，立即标记已读，避免触发全局 TripTaskNotice 遮挡底部长卷
  useEffect(() => {
    if (storedTask?.notificationState === "unread") {
      useTripTaskStore.getState().acknowledgeTask(jobId);
    }
  }, [jobId, storedTask?.notificationState]);

  const isTerminalVisual = completed || noRoute || failed;

  // 6 分钟观察预算定时器
  useEffect(() => {
    if (isPaused || isTerminalVisual) return;
    const currentBudget = useTripTaskStore.getState().getBudget(jobId);
    const elapsed = Date.now() - currentBudget.startedAt;
    const remaining = MAX_POLLING_DURATION_MS - elapsed;
    if (remaining <= 0) {
      useTripTaskStore.getState().checkTaskTimeout(jobId);
      setTimedOut(true);
      return;
    }
    const timer = window.setTimeout(() => {
      useTripTaskStore.getState().checkTaskTimeout(jobId);
      setTimedOut(true);
    }, remaining);
    return () => window.clearTimeout(timer);
  }, [jobId, isPaused, budget?.startedAt, isTerminalVisual]);

  const onTimeout = useCallback(() => {
    useTripTaskStore.getState().checkTaskTimeout(jobId);
    setTimedOut(true);
  }, [jobId]);

  const onConsecutiveErrors = useCallback(
    (count: number) => {
      if (count > 0) {
        useTripTaskStore.getState().recordTaskError(jobId, count);
      } else {
        useTripTaskStore.getState().recordTaskSuccess(jobId);
      }
      setNetworkUnstable(count >= CONSECUTIVE_ERROR_LIMIT);
    },
    [jobId],
  );

  const deadline = budget?.startedAt
    ? budget.startedAt + MAX_POLLING_DURATION_MS
    : undefined;
  const isBudgetExpired = deadline ? Date.now() >= deadline : false;

  const { stop } = useJobProgress({
    jobId,
    onData,
    onTimeout,
    onConsecutiveErrors,
    consecutiveErrorThreshold: CONSECUTIVE_ERROR_LIMIT,
    initialConsecutiveErrors: budget?.consecutiveErrors || 0,
    deadline,
    timeoutMs: MAX_POLLING_DURATION_MS,
    enabled:
      !completed &&
      !noRoute &&
      !failed &&
      !timedOut &&
      !unknownState &&
      !isPaused &&
      !isBudgetExpired &&
      !rechecking,
  });

  const stopRef = useRef(stop);
  stopRef.current = stop;

  useEffect(
    () => () => {
      stopRef.current();
    },
    [],
  );

  async function handleRecheck() {
    if (retrying || rechecking) return;
    setRetrying(true);
    setRechecking(true);
    setActionError(null);
    const keepUnknownWhileQuerying = !timedOut;
    if (keepUnknownWhileQuerying) {
      setUnknownState(true);
    }

    // 重置连续错误与暂停标志，只执行一次 GET，绝不发起 POST
    const currentBudget = useTripTaskStore.getState().getBudget(jobId);
    useTripTaskStore.setState((s) => ({
      budgets: {
        ...s.budgets,
        [jobId]: {
          ...currentBudget,
          consecutiveErrors: 0,
          isPaused: false,
          pauseReason: null,
          startedAt: Date.now(),
        },
      },
    }));

    try {
      const data = await pollJobStatus(jobId);
      useTripTaskStore.getState().recordTaskSuccess(jobId);
      setNetworkUnstable(false);

      const nextStep = normalizePlanningStep(data.stage_progress);
      if (nextStep !== null) {
        setKnownStep(nextStep);
        setJob(jobId, data.status, data.stage_progress);
      }

      if (data.delivery) {
        setDelivery(data.delivery);
      }

      if (data.status === "COMPLETED" && data.result_record_id) {
        beginCompletedTransition(data);
        return;
      }

      const isNoRoute = data.delivery?.outcome === "NO_ROUTE";
      if (
        (data.status === "FAILED" || isNoRoute) &&
        data.error?.code !== "GENERATION_STATUS_TIMEOUT"
      ) {
        setTimedOut(false);
        setUnknownState(false);
        syncPlanningTaskRecord({
          jobId,
          data,
          destination: cityName,
          startedAt,
          terminalStatus: isNoRoute ? "no_route" : "failed",
          notificationState: "acknowledged",
          error: isNoRoute
            ? null
            : (data.error ?? {
                code: "GENERATION_FAILED",
                message: "生成失败，请重试",
              }),
        });
        if (isNoRoute) {
          setNoRoute(true);
          setFailed(false);
          setErrorMessage(
            getNoRouteExplanation(data.delivery?.reason_code ?? null),
          );
        } else {
          setFailed(true);
          setNoRoute(false);
          setErrorCode(data.error?.code ?? null);
          setErrorMessage(
            webErrorMessage(data.error?.code, data.error?.message),
          );
        }
        return;
      }

      if (
        (data.status === "QUEUED" || data.status === "RUNNING") &&
        data.error?.code !== "GENERATION_STATUS_TIMEOUT"
      ) {
        setTimedOut(false);
        setUnknownState(false);
        setActionError(null);
        return;
      }

      setTimedOut(false);
      setUnknownState(true);
      setErrorMessage("暂时无法确认任务状态，任务可能仍在继续。");
    } catch {
      useTripTaskStore.getState().recordTaskError(jobId);
      setTimedOut(false);
      setUnknownState(true);
      setActionError("网络查询失败，请检查网络设置后重试");
    } finally {
      setRechecking(false);
      setRetrying(false);
    }
  }

  async function handleRetrySame() {
    if (retrying || retrySubmissionInFlight.current) return;
    retrySubmissionInFlight.current = true;
    setRetrying(true);
    setActionError(null);

    try {
      const retryPayload = await findOriginalRetryInput(jobId);

      if (!retryPayload || !retryPayload.to_city || !retryPayload.days) {
        showToast("原请求数据已过期，请重新填写", "error");
        navigate("/");
        return;
      }

      clearResult();
      const pending = savePendingSubmission(retryPayload);
      const res = await submitTrip(retryPayload, pending.request_id);
      syncPlanningTaskRecord({
        jobId: res.job_id,
        requestId: pending.request_id,
        destination: retryPayload.to_city || null,
        startedAt: Date.now(),
        status: "pending",
        notificationState: "none",
      });
      clearPendingSubmission();
      navigate(`/planning/${res.job_id}`, { replace: true });
    } catch (err) {
      if (err instanceof ApiRequestError) {
        if (
          err.status === 400 ||
          err.status === 422 ||
          err.status === 429 ||
          [
            "REQUEST_ID_CONFLICT",
            "CITY_NOT_SUPPORTED",
            "VALIDATION_ERROR",
            "QUOTA_EXHAUSTED",
          ].includes(err.code)
        ) {
          clearPendingSubmission();
        }
      }
      const msg =
        err instanceof ApiRequestError ? err.message : "重试失败，请稍后再试";
      setActionError(msg);
    } finally {
      retrySubmissionInFlight.current = false;
      setRetrying(false);
    }
  }

  // ───────── 高德地图：推近动画 + 纸色兜底 ─────────
  const mapStageRef = useRef<HTMLDivElement>(null);
  const mapContainerRef = useRef<HTMLDivElement>(null);
  const amapRef = useRef<any>(null);
  const mapInstanceRef = useRef<any>(null);
  const markerRef = useRef<any>(null);
  const markerKeyRef = useRef<string>("");
  const ringElRef = useRef<HTMLDivElement | null>(null);
  const prevZoomRef = useRef<number | null>(null);
  const prevStepRef = useRef<number | null>(null);
  const moveEndTimerRef = useRef<number | null>(null);
  const moveEndCleanupRef = useRef<(() => void) | null>(null);
  const [mapFailed, setMapFailed] = useState(false);

  const targetZoom = resolvePlanningZoomLevel(knownStep, hasCoordinates);
  const latestCameraRef = useRef({
    coordinates,
    cityName,
    targetZoom,
    knownStep,
  });
  latestCameraRef.current = {
    coordinates,
    cityName,
    targetZoom,
    knownStep,
  };

  const triggerRingPulse = useCallback(() => {
    const el = ringElRef.current;
    if (!el || prefersReducedMotion()) return;
    el.classList.remove("pulse");
    void el.offsetWidth;
    el.classList.add("pulse");
  }, []);

  const cancelPendingCameraSettlement = useCallback(() => {
    if (moveEndTimerRef.current !== null) {
      window.clearTimeout(moveEndTimerRef.current);
      moveEndTimerRef.current = null;
    }
    if (moveEndCleanupRef.current) {
      moveEndCleanupRef.current();
      moveEndCleanupRef.current = null;
    }
  }, []);

  const applyMapCamera = useCallback(
    (
      map: any,
      nextZoom: number,
      nextCenter: [number, number],
      options: { animate: boolean; pulseOnSettle: boolean },
    ) => {
      cancelPendingCameraSettlement();
      // 高德 JS API 2.0 在 zoomEnable:false 时连程序 setZoomAndCenter 都会被忽略，
      // 因此推近期间临时放开、落定后再锁回（容器已设 pointer-events:none 防用户误触）
      map.setStatus?.({ zoomEnable: true });

      if (!options.animate) {
        map.setZoomAndCenter(nextZoom, nextCenter, true);
        map.setStatus?.({ zoomEnable: false });
        return;
      }

      let settled = false;
      const settle = () => {
        if (settled) return;
        settled = true;
        cancelPendingCameraSettlement();
        map.setStatus?.({ zoomEnable: false });
        if (options.pulseOnSettle) {
          triggerRingPulse();
        }
      };

      if (typeof map.on === "function") {
        const handler = () => settle();
        map.on("moveend", handler);
        moveEndCleanupRef.current = () => {
          map.off?.("moveend", handler);
        };
      } else if (typeof map.once === "function") {
        map.once("moveend", settle);
      }

      moveEndTimerRef.current = window.setTimeout(settle, 1600);
      map.setZoomAndCenter(nextZoom, nextCenter, false, 1400);
    },
    [cancelPendingCameraSettlement, triggerRingPulse],
  );

  const syncCityMarker = useCallback(
    (
      AMap: any,
      map: any,
      nextCoords: PlanningCoordinates | null,
      nextCity: string | null,
    ) => {
      if (!AMap || !map) return;
      const nextKey =
        nextCoords && nextCity
          ? `${nextCity}:${nextCoords.longitude},${nextCoords.latitude}`
          : "";

      if (nextKey && markerRef.current && markerKeyRef.current === nextKey) {
        return;
      }

      if (markerRef.current) {
        markerRef.current.setMap(null);
        markerRef.current = null;
        markerKeyRef.current = "";
        ringElRef.current = null;
      }
      if (!nextCoords || !nextCity) return;

      const wrap = document.createElement("div");
      wrap.className = "bd-ring";
      const ring = document.createElement("b");
      const dot = document.createElement("i");
      const label = document.createElement("span");
      label.textContent = nextCity;
      wrap.append(ring, dot, label);

      const marker = new AMap.Marker({
        position: [nextCoords.longitude, nextCoords.latitude],
        content: wrap,
        offset: new AMap.Pixel(0, 0),
        zIndex: 120,
        clickable: false,
        bubble: true,
      });
      marker.setMap(map);
      markerRef.current = marker;
      markerKeyRef.current = nextKey;
      ringElRef.current = wrap;
    },
    [],
  );

  // 初始化高德 JS API 2.0（严格按 MapView.tsx 方式设置安全密钥）
  useEffect(() => {
    let destroyed = false;
    const key = import.meta.env.VITE_AMAP_KEY;
    if (!key) {
      setMapFailed(true);
      return;
    }

    const security = import.meta.env.VITE_AMAP_SECURITY;
    if (security) {
      (window as any)._AMapSecurityConfig = { securityJsCode: security };
    }

    loadAmapSdk(key)
      .then((AMap) => {
        if (destroyed || !mapContainerRef.current) return;
        const container = mapContainerRef.current;
        const rectWidth = container.clientWidth || window.innerWidth;
        const rectHeight =
          container.clientHeight || Math.max(300, window.innerHeight - 52);
        const cam = latestCameraRef.current;
        const baseTarget = cam.coordinates ?? CHINA_FALLBACK_CENTER;
        const initialCenter = computeBindOffsetCenter(
          baseTarget,
          cam.targetZoom,
          rectWidth,
          rectHeight,
        );

        amapRef.current = AMap;
        const map = new AMap.Map(container, {
          viewMode: "2D",
          zoom: cam.targetZoom,
          center: initialCenter,
          dragEnable: false,
          zoomEnable: false,
          doubleClickZoom: false,
          keyboardEnable: false,
          scrollWheel: false,
          touchZoom: false,
          rotateEnable: false,
          pitchEnable: false,
        });
        mapInstanceRef.current = map;
        prevZoomRef.current = cam.targetZoom;
        prevStepRef.current = cam.knownStep;

        const innerMapsEl = container.querySelector<HTMLElement>(".amap-maps");
        if (innerMapsEl) {
          innerMapsEl.tabIndex = -1;
        }

        syncCityMarker(AMap, map, cam.coordinates, cam.cityName);
      })
      .catch(() => {
        if (!destroyed) {
          setMapFailed(true);
        }
      });

    return () => {
      destroyed = true;
      cancelPendingCameraSettlement();
      if (markerRef.current) {
        markerRef.current.setMap(null);
        markerRef.current = null;
        markerKeyRef.current = "";
        ringElRef.current = null;
      }
      mapInstanceRef.current?.destroy();
      mapInstanceRef.current = null;
      amapRef.current = null;
    };
  }, [cancelPendingCameraSettlement, syncCityMarker]);

  // 城市坐标 / 步数推进响应（两步之间不动，仅步数推近才动画 + 绿圈；减少动态效果时直接切）
  useEffect(() => {
    const AMap = amapRef.current;
    const map = mapInstanceRef.current;
    const container = mapContainerRef.current;
    if (!AMap || !map || !container || mapFailed) return;

    syncCityMarker(AMap, map, coordinates, cityName);

    const width = container.clientWidth || window.innerWidth;
    const height =
      container.clientHeight || Math.max(300, window.innerHeight - 52);
    const center = computeBindOffsetCenter(
      coordinates ?? CHINA_FALLBACK_CENTER,
      targetZoom,
      width,
      height,
    );

    const prevZoom = prevZoomRef.current;
    const prevStep = prevStepRef.current;
    prevZoomRef.current = targetZoom;
    prevStepRef.current = knownStep;

    if (prevZoom === null) {
      applyMapCamera(map, targetZoom, center, {
        animate: false,
        pulseOnSettle: false,
      });
      return;
    }

    const zoomChanged = Math.abs(targetZoom - prevZoom) > 0.01;
    const stepAdvanced =
      knownStep !== null && (prevStep === null || knownStep > prevStep);

    if (!zoomChanged) {
      applyMapCamera(map, targetZoom, center, {
        animate: false,
        pulseOnSettle: false,
      });
      return;
    }

    const reduce = prefersReducedMotion();
    if (reduce || !stepAdvanced) {
      applyMapCamera(map, targetZoom, center, {
        animate: false,
        pulseOnSettle: false,
      });
      return;
    }

    applyMapCamera(map, targetZoom, center, {
      animate: true,
      pulseOnSettle: true,
    });
  }, [
    coordinates,
    cityName,
    targetZoom,
    knownStep,
    mapFailed,
    applyMapCamera,
    syncCityMarker,
  ]);

  // 视口变化保持城市落在卡片和长卷之间的空地正中（不触发动画或绿圈）
  useEffect(() => {
    const stage = mapStageRef.current;
    if (!stage || typeof ResizeObserver === "undefined" || mapFailed) return;
    let lastW = stage.clientWidth;
    let lastH = stage.clientHeight;
    const ro = new ResizeObserver(() => {
      const map = mapInstanceRef.current;
      const w = stage.clientWidth;
      const h = stage.clientHeight;
      if (!map || w <= 0 || h <= 0) return;
      if (Math.abs(w - lastW) <= 2 && Math.abs(h - lastH) <= 2) return;
      lastW = w;
      lastH = h;
      const cam = latestCameraRef.current;
      const center = computeBindOffsetCenter(
        cam.coordinates ?? CHINA_FALLBACK_CENTER,
        cam.targetZoom,
        w,
        h,
      );
      if (typeof map.resize === "function") {
        map.resize();
      }
      applyMapCamera(map, cam.targetZoom, center, {
        animate: false,
        pulseOnSettle: false,
      });
    });
    ro.observe(stage);
    return () => ro.disconnect();
  }, [applyMapCamera, mapFailed]);

  const stripProgressPct = useMemo(
    () => resolveStripProgressPct(knownStep, visualState),
    [knownStep, visualState],
  );
  const stripUnlit = visualState !== "completed" && knownStep === null;
  const showBreathingEdge =
    visualState === "binding" && knownStep !== null && knownStep < TOTAL_STAGES;

  const headlineLines = stateCopy.hudLines;
  const isHudFail = visualState === "no_route" || visualState === "failed";
  const showConsecutiveErrorPauseNote =
    visualState === "unknown" &&
    isPaused &&
    budget?.pauseReason === "consecutive_errors";
  const showWeakNetworkNotice =
    networkUnstable && visualState === "binding" && !isPaused;

  return (
    <main className="bd-atlas" data-mode="binding" data-state={visualState}>
      <div className="bd-stage" ref={mapStageRef}>
        {mapFailed ? (
          <div className="bd-fallback" data-testid="bd-map-fallback">
            <div
              className="bd-fallback-center"
              style={{
                left: "calc((min(340px, 40%) + 100%) / 2)",
                top: "calc((16px + max(56px, 100% - 250px)) / 2)",
              }}
            >
              {cityName && (
                <div className="bd-fallback-pin">
                  <div className="bd-ring">
                    <i />
                    <span>{cityName}</span>
                  </div>
                </div>
              )}
              <span className="bd-fallback-note">
                地图底图暂未加载 · 生成进度正常同步
              </span>
            </div>
          </div>
        ) : (
          <div
            ref={mapContainerRef}
            className="bd-map"
            data-testid="bd-amap-container"
            data-zoom={String(targetZoom)}
          />
        )}
      </div>

      <section
        className={["float", "bd-hud", isHudFail && "is-fail"]
          .filter(Boolean)
          .join(" ")}
        aria-label="这次要排的行程"
      >
        <Link className="hud-back" to="/">
          <ArrowLeftIcon />
          出发
        </Link>
        <h3>
          {headlineLines.map((line, idx) => (
            <span key={`${line}-${idx}`}>
              {idx > 0 && <br />}
              {line}
            </span>
          ))}
        </h3>
        {stateCopy.hudNote && <p className="k">{stateCopy.hudNote}</p>}
        {showConsecutiveErrorPauseNote && (
          <p className="k is-warn" role="status">
            网络连接不稳定（已连续 {CONSECUTIVE_ERROR_LIMIT}{" "}
            次失败），已暂停自动更新
          </p>
        )}
        {showWeakNetworkNotice && (
          <p className="k is-warn" role="status">
            网络连接微弱，正在持续自动同步状态…
          </p>
        )}
        {actionError && (
          <p className="k is-alert" role="alert">
            {actionError}
          </p>
        )}
        {conditionSummary.leadLine && (
          <p className="s">{conditionSummary.leadLine}</p>
        )}
        {conditionSummary.rows.map((row) => (
          <p key={row.label} className="k">
            {row.label}：{row.value}
          </p>
        ))}
      </section>

      <section className="float bd-strip" aria-label="装订进度">
        <header className="bd-sh">
          <div className="bd-sh-meta">
            {visualState === "binding" ? (
              <>
                {knownStep !== null && (
                  <>
                    <b>
                      第 {knownStep} / {TOTAL_STAGES} 步
                    </b>
                    <span className="bd-sep" aria-hidden="true">
                      ·
                    </span>
                  </>
                )}
                <ElapsedClock
                  startedAt={startedAt}
                  paused={false}
                  suffix=" · 一般要几分钟"
                />
              </>
            ) : visualState === "timeout" ? (
              <>
                {stateCopy.stripTitle && (
                  <>
                    <b>{stateCopy.stripTitle}</b>
                    <span className="bd-sep" aria-hidden="true">
                      ·
                    </span>
                  </>
                )}
                <span>{stateCopy.stripSub}</span>
              </>
            ) : (
              <>
                {stateCopy.stripTitle && (
                  <b className={stateCopy.stripFailStyle ? "fail" : undefined}>
                    {stateCopy.stripTitle}
                  </b>
                )}
                {stateCopy.stripTitle && stateCopy.stripSub && (
                  <span className="bd-sep" aria-hidden="true">
                    ·
                  </span>
                )}
                {stateCopy.stripSub && <span>{stateCopy.stripSub}</span>}
              </>
            )}
          </div>

          <span className="acts">
            {visualState === "binding" && (
              <Link className="lk" to="/history">
                先去逛逛，好了叫你
                <ArrowRightIcon />
              </Link>
            )}

            {visualState === "no_route" && (
              <>
                <Link className="lk" to="/history">
                  看看我的行程
                </Link>
                <Link className="btn" to="/">
                  改一改条件
                  <ArrowRightIcon />
                </Link>
              </>
            )}

            {visualState === "failed" && (
              <>
                <button
                  type="button"
                  className="btn"
                  disabled={retrying}
                  onClick={() => void handleRetrySame()}
                >
                  {retrying ? "正在重试…" : "用同样的条件再试"}
                </button>
                <Link className="lk" to="/">
                  改一改条件
                </Link>
              </>
            )}

            {visualState === "timeout" && (
              <>
                <button
                  type="button"
                  className="lk"
                  disabled={retrying || rechecking}
                  onClick={() => void handleRecheck()}
                >
                  {rechecking ? "正在查询…" : "再查一次"}
                </button>
                <Link className="btn" to="/history">
                  回头去我的行程看
                  <ArrowRightIcon />
                </Link>
              </>
            )}

            {visualState === "unknown" && (
              <>
                <Link className="lk" to="/history">
                  先去逛逛，好了叫你
                  <ArrowRightIcon />
                </Link>
                <button
                  type="button"
                  className="btn"
                  disabled={retrying || rechecking}
                  onClick={() => void handleRecheck()}
                >
                  {rechecking ? "正在查询…" : "再查一次"}
                </button>
              </>
            )}

            {visualState === "completed" &&
              (completedTarget ? (
                <button
                  type="button"
                  className="btn"
                  onClick={() => navigate(completedTarget, { replace: true })}
                >
                  翻开旅行册
                  <ArrowRightIcon />
                </button>
              ) : (
                <span>正在准备旅行册…</span>
              ))}
          </span>
        </header>

        <div
          className={["sk", stripUnlit && "is-unlit"].filter(Boolean).join(" ")}
          data-st={visualState}
          style={
            { "--yuntu-bd-sky-p": stripProgressPct } as React.CSSProperties
          }
        >
          <div className="sk-lit" />
          {showBreathingEdge && <div className="sk-edge" aria-hidden="true" />}

          {SKY_STAGES.map((stage) => {
            let roleClass = "t";
            let statusText = "等着";

            if (visualState === "completed") {
              roleClass = "d";
              statusText = "完成";
            } else if (knownStep !== null) {
              if (stage.step < knownStep) {
                roleClass = "d";
                statusText = "完成";
              } else if (stage.step === knownStep) {
                if (visualState === "no_route") {
                  roleClass = "f";
                  statusText = "没排出路线";
                } else if (visualState === "failed") {
                  roleClass = "f";
                  statusText = "未能完成";
                } else if (
                  visualState === "timeout" ||
                  visualState === "unknown"
                ) {
                  roleClass = "n";
                  statusText = "还没报完成";
                } else {
                  roleClass = "n";
                  statusText = "进行中";
                }
              }
            }

            const isNightLit = stage.step === 4 && roleClass !== "t";

            return (
              <div
                key={stage.code}
                className={["sk-lab", roleClass, isNightLit && "nt"]
                  .filter(Boolean)
                  .join(" ")}
                style={{ left: `${stage.positionPct}%` }}
              >
                <small>{stage.period}</small>
                <b>{stage.label}</b>
                <em>{statusText}</em>
              </div>
            );
          })}

          <div className="sk-road">
            <i className="done" />
            <i className="rest" />
            {SKY_STAGES.map((stage) => {
              let dotClass = "";
              if (visualState === "completed") {
                dotClass = stage.step === 4 ? " d nt" : " d";
              } else if (knownStep !== null) {
                if (stage.step < knownStep) {
                  dotClass = " d";
                } else if (stage.step === knownStep) {
                  dotClass =
                    visualState === "no_route" || visualState === "failed"
                      ? " f"
                      : " n";
                }
              }
              return (
                <span
                  key={stage.code}
                  className={["sk-dot", dotClass.trim()]
                    .filter(Boolean)
                    .join(" ")}
                  style={{ left: `${stage.positionPct}%` }}
                />
              );
            })}
          </div>
        </div>

        <span className="sr" aria-live="polite">
          {stateCopy.srAnnouncement}
        </span>
      </section>
    </main>
  );
}
