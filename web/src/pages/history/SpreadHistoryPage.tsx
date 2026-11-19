import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import {
  ApiRequestError,
  fetchDestinations,
  pollJobStatus,
  submitTrip,
} from "@/services/api";
import {
  getNoRouteExplanation,
  resolveHistoryDisplay,
} from "@/constants/delivery";
import { STAGE_MAP } from "@/constants/stages";
import { useTripStore } from "@/stores/tripStore";
import { useTripTaskStore } from "@/stores/tripTaskStore";
import type { ActiveTrip, HistoryTripItem } from "@/types/auth";
import type { Destination } from "@/types/destination";
import type { StageProgress } from "@/types/trip";
import type { TripSubmitRequest } from "@/types/tripRequest";
import { CITY_NAME_TO_FOLDER, getCityMobilePhotoUrl } from "@/utils/cityPhotos";
import {
  clearGuestHistoryTrips,
  GUEST_HISTORY_UPDATED_EVENT,
} from "@/utils/guestTripHistory";
import {
  clearPendingSubmission,
  savePendingSubmission,
} from "@/utils/pendingSubmission";
import {
  buildTripCityOrder,
  buildTripResultHref,
  cacheDestinations,
  cleanTripCityName,
  fetchAllHistoryTrips,
  formatChineseMonthHeading,
  formatTripMonthDay,
  hasTripStageProgress,
  isReadableTrip,
  readCachedDestinations,
  resolveCityTripPhoto,
  resolveTripPhoto,
  summarizeTripShelf,
} from "@/utils/tripShelf";
import "./spread-history.css";

/** 与旧桌面/手机历史页一致：基础行程的补充说明 */
const BASIC_TRIP_NOTE = "按锁定路线整理的基础行程，可以翻开看看。";

interface MonthGroup {
  month: string;
  includeActive: boolean;
  items: HistoryTripItem[];
}

interface RetryErrorState {
  tripId: string;
  message: string;
}

function ArrowRightIcon() {
  return (
    <svg className="icon" viewBox="0 0 24 24" aria-hidden="true">
      <path d="M5 12h14M13 6l6 6-6 6" />
    </svg>
  );
}

export default function SpreadHistoryPage() {
  const navigate = useNavigate();

  const tasks = useTripTaskStore((s) => s.tasks);
  const setFormData = useTripStore((s) => s.setFormData);
  const clearResult = useTripStore((s) => s.clearResult);

  const activeTrip = useMemo<ActiveTrip | null>(() => {
    const pending = Object.values(tasks)
      .filter((t) => t.status === "pending")
      .sort((a, b) => b.startedAt - a.startedAt)[0];
    if (!pending) return null;
    return {
      trip_id: pending.jobId,
      job_id: pending.jobId,
      status: "RUNNING",
    };
  }, [tasks]);

  const [shelfItems, setShelfItems] = useState<HistoryTripItem[]>([]);
  const [shelfLoading, setShelfLoading] = useState(true);
  const [shelfError, setShelfError] = useState<string | null>(null);

  const [destinations, setDestinations] = useState<Destination[]>(() =>
    readCachedDestinations(),
  );
  const [destinationsLoaded, setDestinationsLoaded] = useState(false);

  const [brokenPhotoIds, setBrokenPhotoIds] = useState<Record<string, true>>(
    {},
  );
  const [activeStageProgress, setActiveStageProgress] =
    useState<StageProgress | null>(null);

  const [retryingTripId, setRetryingTripId] = useState<string | null>(null);
  const [retryError, setRetryError] = useState<RetryErrorState | null>(null);

  const loadShelf = useCallback(
    async (signal?: AbortSignal) => {
      setShelfLoading(true);
      setShelfError(null);
      try {
        const items = await fetchAllHistoryTrips({ signal });
        if (!signal?.aborted) {
          setShelfItems(items);
          setBrokenPhotoIds({});
        }
      } catch (err: unknown) {
        if (signal?.aborted) return;
        if (err instanceof DOMException && err.name === "AbortError") return;
        setShelfError(
          err instanceof ApiRequestError && err.message
            ? err.message
            : "书架记录暂时打不开，请稍后重试。",
        );
      } finally {
        if (!signal?.aborted) {
          setShelfLoading(false);
        }
      }
    },
    [],
  );

  useEffect(() => {
    const controller = new AbortController();
    void loadShelf(controller.signal);

    const onUpdated = () => {
      void loadShelf();
    };
    window.addEventListener(GUEST_HISTORY_UPDATED_EVENT, onUpdated);
    window.addEventListener("storage", onUpdated);

    return () => {
      controller.abort();
      window.removeEventListener(GUEST_HISTORY_UPDATED_EVENT, onUpdated);
      window.removeEventListener("storage", onUpdated);
    };
  }, [loadShelf]);

  const shelfSummary = useMemo(
    () => summarizeTripShelf(shelfItems, activeTrip),
    [shelfItems, activeTrip],
  );

  const cityOrder = useMemo(
    () => buildTripCityOrder(shelfSummary.items),
    [shelfSummary.items],
  );

  const hasStandaloneActiveTrip = useMemo(
    () =>
      Boolean(
        activeTrip?.trip_id &&
          !shelfSummary.items.some((item) => item.trip_id === activeTrip.trip_id),
      ),
    [activeTrip, shelfSummary.items],
  );

  // 正在装订的行程：与 SpreadTopBar 使用同一个城市来源，拿不到就写“行程”
  const activeTripCity = useMemo(() => {
    if (!activeTrip?.job_id) return "行程";
    return tasks[activeTrip.job_id]?.destination?.trim() || "行程";
  }, [activeTrip, tasks]);

  // 正在装订的行程步数：只取 GET /trip/jobs/:jobId 的 stage_progress
  useEffect(() => {
    const jobId = hasStandaloneActiveTrip ? activeTrip?.job_id : null;
    if (!jobId) {
      setActiveStageProgress(null);
      return;
    }

    let cancelled = false;
    const controller = new AbortController();
    setActiveStageProgress(null);

    pollJobStatus(jobId, { signal: controller.signal })
      .then((res) => {
        if (cancelled) return;
        setActiveStageProgress(
          hasTripStageProgress(res.stage_progress) ? res.stage_progress : null,
        );
      })
      .catch(() => {
        if (!cancelled) {
          setActiveStageProgress(null);
        }
      });

    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [hasStandaloneActiveTrip, activeTrip?.job_id]);

  // 存在非 16 图库城市的可翻阅记录（或已知活动城市）时才按需拉取 /api/destinations
  const neededNonGalleryCities = useMemo(() => {
    const missing = new Set<string>();
    for (const item of shelfSummary.items) {
      if (!isReadableTrip(item)) continue;
      const clean = cleanTripCityName(item.city);
      if (clean && !CITY_NAME_TO_FOLDER[clean]) {
        missing.add(clean);
      }
    }
    if (
      hasStandaloneActiveTrip &&
      activeTripCity !== "行程" &&
      !CITY_NAME_TO_FOLDER[cleanTripCityName(activeTripCity)]
    ) {
      missing.add(cleanTripCityName(activeTripCity));
    }
    return Array.from(missing);
  }, [shelfSummary.items, hasStandaloneActiveTrip, activeTripCity]);

  useEffect(() => {
    if (neededNonGalleryCities.length === 0 || destinationsLoaded) {
      return;
    }
    const cachedSet = new Set(
      destinations.map((dest) => cleanTripCityName(dest.name)),
    );
    const hasUncached = neededNonGalleryCities.some(
      (city) => !cachedSet.has(city),
    );
    if (!hasUncached) {
      setDestinationsLoaded(true);
      return;
    }

    let cancelled = false;
    fetchDestinations()
      .then((res) => {
        if (cancelled) return;
        setDestinations(res.destinations ?? []);
        cacheDestinations(res);
      })
      .catch(() => {
        /* 保持优雅素纸占位，不阻断页面 */
      })
      .finally(() => {
        if (!cancelled) setDestinationsLoaded(true);
      });

    return () => {
      cancelled = true;
    };
  }, [neededNonGalleryCities, destinations, destinationsLoaded]);

  // 按创建月份分组（中文数字，“二〇二六年 · 十一月”）
  const monthGroups = useMemo<MonthGroup[]>(() => {
    const groups: MonthGroup[] = [];
    const currentMonth = hasStandaloneActiveTrip
      ? formatChineseMonthHeading(new Date())
      : null;

    for (const item of shelfSummary.items) {
      const month = formatChineseMonthHeading(item.created_at);
      const existing = groups.find((g) => g.month === month);
      if (existing) {
        existing.items.push(item);
      } else {
        groups.push({ month, includeActive: false, items: [item] });
      }
    }

    if (currentMonth) {
      const existingCurrent = groups.find((g) => g.month === currentMonth);
      if (existingCurrent) {
        existingCurrent.includeActive = true;
        // 正在装订的一册始终排在最前面所在的当前月组
        if (groups[0] !== existingCurrent) {
          const idx = groups.indexOf(existingCurrent);
          groups.splice(idx, 1);
          groups.unshift(existingCurrent);
        }
      } else {
        groups.unshift({
          month: currentMonth,
          includeActive: true,
          items: [],
        });
      }
    }

    return groups;
  }, [shelfSummary.items, hasStandaloneActiveTrip]);

  const handleRetry = async (item: HistoryTripItem) => {
    if (retryingTripId) return;

    const input = item.retry_input?.trip_request;
    if (!input) {
      navigate("/");
      return;
    }

    setRetryingTripId(item.trip_id);
    setRetryError(null);

    const formData: TripSubmitRequest = { ...input };
    setFormData(formData);
    clearResult();

    const pending = savePendingSubmission(formData);

    try {
      const res = await submitTrip(formData, pending.request_id);
      useTripTaskStore.getState().addOrUpdateTask({
        jobId: res.job_id,
        requestId: pending.request_id,
        destination: formData.to_city,
        startedAt: Date.now(),
        status: "pending",
        notificationState: "none",
      });
      clearPendingSubmission();
      navigate(`/planning/${encodeURIComponent(res.job_id)}`);
    } catch (err: unknown) {
      if (err instanceof ApiRequestError) {
        setRetryError({
          tripId: item.trip_id,
          message: err.message,
        });
      } else {
        setRetryError({
          tripId: item.trip_id,
          message: "重试提交失败，请稍后重试",
        });
      }
      setRetryingTripId(null);
    }
  };

  const renderActiveTripCard = () => {
    if (!activeTrip) return null;

    const targetHref = activeTrip.job_id
      ? `/planning/${encodeURIComponent(activeTrip.job_id)}`
      : "/";
    const hasStep = hasTripStageProgress(activeStageProgress);
    const badgeText = hasStep
      ? `装订中 ${activeStageProgress.step}/${activeStageProgress.total}`
      : "装订中";
    const stageLabel =
      hasStep && activeStageProgress.code
        ? STAGE_MAP[activeStageProgress.code]?.label
        : null;
    const activePhotoKey = `__active__:${activeTrip.trip_id}`;
    const activePhoto =
      activeTripCity !== "行程" && !brokenPhotoIds[activePhotoKey]
        ? resolveCityTripPhoto(activeTripCity, 0, destinations)
        : null;

    return (
      <Link
        key={`active-${activeTrip.trip_id}`}
        className="trip-card is-busy"
        to={targetHref}
      >
        <span className={`ph${activePhoto ? "" : " is-busy-blank"}`}>
          {activePhoto ? (
            <img
              className="photo"
              src={getCityMobilePhotoUrl(activePhoto.url)}
              alt={`${activeTripCity}风光`}
              loading="lazy"
              onError={() =>
                setBrokenPhotoIds((prev) => ({
                  ...prev,
                  [activePhotoKey]: true,
                }))
              }
            />
          ) : (
            <em aria-hidden="true">{activeTripCity}</em>
          )}
          <span className="st">{badgeText}</span>
          {stageLabel ? <span className="tc-now">{stageLabel}</span> : null}
        </span>
        <span className="tc-meta">{activeTripCity} · 正在装订</span>
        <b className="tc-title">查看装订进度 →</b>
        {activePhoto?.credit ? (
          <small className="tc-credit">{activePhoto.credit}</small>
        ) : null}
      </Link>
    );
  };

  const renderHistoryCard = (item: HistoryTripItem) => {
    const display = resolveHistoryDisplay(item);
    const cleanCity = cleanTripCityName(item.city) || item.city;
    const itemDays = Number(item.days) > 0 ? Number(item.days) : null;
    const monthDay = formatTripMonthDay(item.created_at);
    const metaParts = [cleanCity];
    if (itemDays !== null) metaParts.push(`${itemDays} 天`);
    if (monthDay) metaParts.push(`${monthDay}创建`);
    const metaText = metaParts.join(" · ");

    const readable =
      isReadableTrip(item) && display.status !== "no_route";
    const resultHref = readable ? buildTripResultHref(item) : null;

    if (readable && resultHref) {
      const photo = !brokenPhotoIds[item.trip_id]
        ? resolveTripPhoto(item, cityOrder, destinations)
        : null;
      const isBasic = display.status === "basic";

      return (
        <Link key={item.trip_id} className="trip-card" to={resultHref}>
          <span className={`ph${photo ? "" : " is-empty"}`}>
            {photo ? (
              <img
                className="photo"
                src={getCityMobilePhotoUrl(photo.url)}
                alt={`${cleanCity}风光`}
                loading="lazy"
                onError={() =>
                  setBrokenPhotoIds((prev) => ({
                    ...prev,
                    [item.trip_id]: true,
                  }))
                }
              />
            ) : (
              <em aria-hidden="true">{cleanCity}</em>
            )}
            <span className={`st${isBasic ? " is-basic" : ""}`}>
              {display.outcome.label}
            </span>
          </span>
          <span className="tc-meta">{metaText}</span>
          <b className="tc-title">翻开这一册 →</b>
          {isBasic ? <span className="tc-note">{BASIC_TRIP_NOTE}</span> : null}
          {photo?.credit ? (
            <small className="tc-credit">{photo.credit}</small>
          ) : null}
        </Link>
      );
    }

    const reasonText =
      display.status === "no_route"
        ? getNoRouteExplanation(item.delivery?.reason_code || null)
        : item.error?.message || "生成失败，请稍后重试";
    const isRetrying = retryingTripId === item.trip_id;
    const cardError =
      retryError?.tripId === item.trip_id ? retryError.message : null;

    return (
      <div key={item.trip_id} className="trip-card is-fail">
        <button
          type="button"
          className="trip-card-hit"
          disabled={retryingTripId !== null}
          onClick={() => void handleRetry(item)}
        >
          <span className="ph">
            <em aria-hidden="true">{cleanCity}</em>
            <span className="why">{reasonText}</span>
          </span>
          <span className="tc-meta">{metaText}</span>
          <b className="tc-title">{isRetrying ? "重新规划中…" : "重新规划 →"}</b>
        </button>
        {cardError ? (
          <p className="tc-err" role="alert">
            {cardError}
          </p>
        ) : null}
      </div>
    );
  };

  const subtitleText = shelfLoading
    ? "正在翻找书架…"
    : shelfError
      ? "书架记录暂时打不开"
      : `${shelfSummary.totalCount} 册 · ${shelfSummary.readableCount} 本可以翻阅 · 保存在当前浏览器中`;

  return (
    <main className="desk">
      <section className="view" id="view-shelf" aria-label="我的行程">
        <div className="shelf">
          <header className="shelf-head">
            <div>
              <h1>我的行程</h1>
              <p>{subtitleText}</p>
            </div>
            <div style={{ display: "flex", alignItems: "center", gap: 16 }}>
              {shelfItems.length > 0 && !activeTrip ? (
                <button
                  type="button"
                  style={{ fontSize: 13, color: "var(--muted)" }}
                  onClick={() => clearGuestHistoryTrips()}
                >
                  清空本地记录
                </button>
              ) : null}
              <Link className="btn-primary" to="/">
                <span>再规划一次</span>
                <ArrowRightIcon />
              </Link>
            </div>
          </header>

          {shelfLoading ? (
            <div className="shelf-state" role="status">
              <h2>正在翻找书架</h2>
              <p>很快就把过往的行程一册一册摆出来。</p>
            </div>
          ) : shelfError ? (
            <div className="shelf-state is-err" role="alert">
              <h2>书架暂时打不开</h2>
              <p>{shelfError}</p>
              <div className="shelf-state-action">
                <button
                  type="button"
                  className="btn-primary"
                  onClick={() => void loadShelf()}
                >
                  再试一次
                </button>
              </div>
            </div>
          ) : shelfSummary.totalCount === 0 ? (
            <div className="shelf-empty">
              <h2>书架还是空的</h2>
              <p>选一座城，填好天数与同行方式，你的第一本路书就会摆在这里。</p>
              <div className="shelf-state-action">
                <Link className="btn-primary" to="/">
                  <span>去出发</span>
                  <ArrowRightIcon />
                </Link>
              </div>
            </div>
          ) : (
            monthGroups.map((group) => (
              <section key={group.month} aria-label={group.month}>
                <h2 className="month">{group.month}</h2>
                <div className="shelf-grid">
                  {group.includeActive ? renderActiveTripCard() : null}
                  {group.items.map((item) => renderHistoryCard(item))}
                </div>
              </section>
            ))
          )}
        </div>
      </section>
    </main>
  );
}
