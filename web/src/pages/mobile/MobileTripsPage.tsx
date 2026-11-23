import React, { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { getHistoryTrips, submitTrip, ApiRequestError } from "@/services/api";
import { useTripStore } from "@/stores/tripStore";
import { useTripTaskStore } from "@/stores/tripTaskStore";
import { useMobileTripStore } from "@/stores/mobileTripStore";
import { useDestinations } from "@/hooks/useDestinations";
import { BUNDLED_FALLBACK_IMAGE, usesBundledCityAssets } from "@/config/cityAssets";
import { GUEST_HISTORY_UPDATED_EVENT } from "@/utils/guestTripHistory";
import { savePendingSubmission, clearPendingSubmission } from "@/utils/pendingSubmission";
import { CITY_PLACEHOLDER, cityPhotoSources } from "@/utils/cityPhotoSources";
import { getNoRouteExplanation, resolveHistoryDisplay } from "@/constants/delivery";
import type { HistoryDisplayStatus } from "@/constants/delivery";
import type { HistoryTripItem } from "@/types/auth";
import type { TripSubmitRequest } from "@/types/tripRequest";
import { CONCEPT_ASSETS } from "@/assets/mobile-concepts/conceptImages";
import { MobileTopBar } from "@/components/mobile/MobileTopBar";
import { Icon } from "@/components/mobile/mobileUi";
import type { MobileIconName } from "@/components/mobile/mobileUi";
import { sceneTags } from "@/components/mobile/mobileFormat";
import "@/styles/mobile.css";

type Filter = "all" | "ready" | "generating" | "retry" | "draft";

const READY: HistoryDisplayStatus[] = ["detailed", "basic", "legacy_success"];

function groupOf(status: HistoryDisplayStatus): Filter {
  if (READY.includes(status)) return "ready";
  if (status === "pending") return "generating";
  return "retry";
}

function dateTime(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return `${d.getMonth() + 1}月${d.getDate()}日 ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

export const MobileTripsPage: React.FC = () => {
  const navigate = useNavigate();
  const setFormData = useTripStore((s) => s.setFormData);
  const clearResult = useTripStore((s) => s.clearResult);
  const draft = useMobileTripStore((s) => s.draft);
  const resumeStep = useMobileTripStore((s) => s.resumeStep);
  const { data: destData } = useDestinations();

  const [items, setItems] = useState<HistoryTripItem[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [retryingTripId, setRetryingTripId] = useState<string | null>(null);
  const [filter, setFilter] = useState<Filter>("all");
  const [query, setQuery] = useState("");

  const loadTrips = useCallback(() => {
    setLoading(true);
    setError(null);
    return getHistoryTrips()
      .then((res) => {
        if (!res.ok) return;
        setItems(res.items || []);
        setNextCursor(res.next_cursor ?? null);
      })
      .catch((err: unknown) => {
        setError(err instanceof ApiRequestError ? err.message : "获取历史行程失败");
      })
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    void loadTrips();
    const onUpdated = () => {
      void loadTrips();
    };
    window.addEventListener(GUEST_HISTORY_UPDATED_EVENT, onUpdated);
    window.addEventListener("storage", onUpdated);
    return () => {
      window.removeEventListener(GUEST_HISTORY_UPDATED_EVENT, onUpdated);
      window.removeEventListener("storage", onUpdated);
    };
  }, [loadTrips]);

  const loadMore = async () => {
    if (!nextCursor || loadingMore) return;
    setLoadingMore(true);
    setActionError(null);
    try {
      const res = await getHistoryTrips({ cursor: nextCursor });
      if (res.ok && res.items) {
        setItems((prev) => {
          const map = new Map(prev.map((i) => [i.trip_id, i]));
          res.items.forEach((i) => map.set(i.trip_id, i));
          return Array.from(map.values());
        });
        setNextCursor(res.next_cursor ?? null);
      }
    } catch (err: unknown) {
      setActionError(err instanceof ApiRequestError ? err.message : "加载更多失败，请重试");
    } finally {
      setLoadingMore(false);
    }
  };

  // 失败 / 未找到路线：用原请求重新提交（与桌面历史页同一流程）
  const retry = async (item: HistoryTripItem) => {
    const input = item.retry_input?.trip_request;
    if (!input) {
      navigate("/");
      return;
    }
    setRetryingTripId(item.trip_id);
    setActionError(null);
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
      navigate(`/planning/${res.job_id}`);
    } catch (err: unknown) {
      setActionError(err instanceof ApiRequestError ? err.message : "重试提交失败，请稍后重试");
      setRetryingTripId(null);
    }
  };

  const openTrip = (item: HistoryTripItem) => {
    const targetId = item.result_record_id || item.trip_id;
    const params = new URLSearchParams({ from: "history" });
    if (item.job_id) params.set("job_id", item.job_id);
    navigate(`/result/${targetId}?${params.toString()}`);
  };

  const hasDraft = Boolean(draft.sceneIds.length || draft.cityConfirmed || draft.rhythm);
  const withStatus = useMemo(
    () => items.map((item) => ({ item, display: resolveHistoryDisplay(item) })),
    [items],
  );
  const count = (f: Filter) =>
    f === "all"
      ? items.length + (hasDraft ? 1 : 0)
      : f === "draft"
        ? Number(hasDraft)
        : withStatus.filter((x) => groupOf(x.display.status) === f).length;

  const q = query.trim();
  const visible = withStatus.filter(
    (x) =>
      (filter === "all" || groupOf(x.display.status) === filter) &&
      (!q || x.item.city.includes(q)),
  );
  const showDraft =
    hasDraft && (filter === "all" || filter === "draft") && (!q || (draft.cityName || "").includes(q));

  const filters: [Filter, string][] = [
    ["all", "全部"],
    ["ready", "已生成"],
    ...(count("generating") ? ([["generating", "生成中"]] as [Filter, string][]) : []),
    ["retry", "待重试"],
    ...(hasDraft ? ([["draft", "草稿"]] as [Filter, string][]) : []),
  ];

  /**
   * E5：同城多篇轮换封面。图源沿用本页现有的 cityPhotoSources（内含 cityPhotos 的
   * getCityAllCovers 静态图库，并优先使用目录里的实景图），不新建图片表。
   * 按记录在列表里是这座城的第几篇来取图（第 n 篇取第 n 张，用完再循环），
   * 相邻的同城卡片因此一定是不同的图；序号按全部记录算，筛选时每张卡的图不变。
   */
  const cityOrder = useMemo(() => {
    const seen = new Map<string, number>();
    const order = new Map<string, number>();
    for (const { item } of withStatus) {
      const n = seen.get(item.city) ?? 0;
      order.set(item.trip_id, n);
      seen.set(item.city, n + 1);
    }
    return order;
  }, [withStatus]);

  const cover = (city: string, nth = 0) => {
    const clean = city.trim().replace(/市$/, "");
    const dest = destData?.destinations.find((d) => d.name === city || d.name === clean);
    const photos = cityPhotoSources(city, dest);
    const first = photos[0];
    const isChongqingFallback =
      usesBundledCityAssets() &&
      clean !== "重庆" &&
      (!first || first === BUNDLED_FALLBACK_IMAGE || first === CITY_PLACEHOLDER);
    if (isChongqingFallback || !first || first === CITY_PLACEHOLDER) {
      const fallbacks = [
        CONCEPT_ASSETS.lake,
        CONCEPT_ASSETS.alley,
        CONCEPT_ASSETS.rooftops,
        CONCEPT_ASSETS.coast,
      ];
      return fallbacks[nth % fallbacks.length];
    }
    return photos[nth % photos.length];
  };

  const tripCard = (item: HistoryTripItem, display: ReturnType<typeof resolveHistoryDisplay>) => {
    const group = groupOf(display.status);
    const noRoute = display.status === "no_route";
    const resultReady = group === "ready" && item.delivery?.result_available !== false;
    const [icon, desc, action, onAction, cls]: [MobileIconName, string, string, () => void, string] =
      group === "ready"
        ? [
            "check",
            display.status === "basic"
              ? "按锁定路线整理的基础行程，可以翻开看看。"
              : "你留下的选择，已经排进每一天。",
            "翻开旅行册",
            () => openTrip(item),
            "",
          ]
        : group === "generating"
          ? ["clock", "可以先看看其他内容，完成后会留在这里。", "查看进度", () => navigate(`/planning/${item.job_id}`), "u-trip-secondary"]
          : [
              "info",
              noRoute
                ? getNoRouteExplanation(item.delivery?.reason_code || null)
                : item.error?.message || "这次没能完成成册，旅行选择还在。",
              retryingTripId === item.trip_id ? "正在提交…" : item.retry_input ? "重新生成" : "重新规划",
              () => void retry(item),
              "u-trip-secondary",
            ];
    const statusCls = group === "ready" ? "ready" : group === "generating" ? "generating" : "failed";
    // 完全正常的卡片（详细攻略、额度已结算）不再每张都标状态和套话，只留封面和底部一行；
    // 基础版、装订中、结算中、失败的照旧显示
    const plain =
      (display.status === "detailed" && item.delivery?.quota_state === "CONSUMED") ||
      display.status === "legacy_success";
    return (
      <article key={item.trip_id} className={`u-trip-card${plain ? " is-plain" : ""}`}>
        <div className="u-trip-cover">
          <img
            src={cover(item.city, cityOrder.get(item.trip_id))}
            alt={`${item.city} · 旅行册封面`}
            loading="lazy"
            draggable={false}
          />
          <div className="u-trip-cover-top">
            <span>YUNTU / TRAVEL BOOK</span>
          </div>
          <div className="u-trip-cover-bottom">
            <div>
              <h3>{item.city}</h3>
              {/* E4：删掉每张卡都重复的那句套话；只有待重试的卡保留它自己的提示语 */}
              {group === "retry" && <p>把心动留好，下次接着写</p>}
            </div>
            {item.days > 0 && (
              <span className="u-days">
                {String(item.days).padStart(2, "0")}
                <small>天 / DAYS</small>
              </span>
            )}
          </div>
        </div>
        <div className="u-trip-body">
          {!plain && (
            <>
              <div className="u-status-row">
                <span className={`u-status ${statusCls}`}>
                  <Icon name={icon} />
                  {display.outcome.label}
                </span>
                {display.quotaNotice && <span className="u-example-label">{display.quotaNotice}</span>}
              </div>
              <p className="u-trip-desc">{desc}</p>
            </>
          )}
          <div className="u-trip-card-bottom">
            {/* E6：与电脑历史页一致，时间一律用创建时间，不再用完成时间 */}
            <span className="u-trip-date">{dateTime(item.created_at)}</span>
            <button
              type="button"
              className={`primary${cls ? ` ${cls}` : ""}`}
              onClick={onAction}
              disabled={(group === "ready" && !resultReady) || retryingTripId === item.trip_id}
            >
              <span>{action}</span>
              <Icon name="arrow" />
            </button>
          </div>
        </div>
      </article>
    );
  };

  const draftCard = (
    <article key="draft" className="u-trip-card">
      <div className="u-trip-cover">
        <img
          src={draft.cityName ? cover(draft.cityName) : CONCEPT_ASSETS.lake}
          alt="旅行草稿封面"
          draggable={false}
        />
        <div className="u-trip-cover-top">
          <span>YUNTU / DRAFT</span>
        </div>
        <div className="u-trip-cover-bottom">
          <div>
            <h3>{draft.cityConfirmed && draft.cityName ? draft.cityName : "目的地未定"}</h3>
            <p>{sceneTags(draft.sceneIds).slice(0, 2).join(" · ") || "这一次，想把日子过慢一点"}</p>
          </div>
          {draft.days ? (
            <span className="u-days">
              {String(draft.days).padStart(2, "0")}
              <small>天 / DAYS</small>
            </span>
          ) : null}
        </div>
      </div>
      <div className="u-trip-body">
        <div className="u-status-row">
          <span className="u-status draft">
            <Icon name="pen" />
            旅行草稿
          </span>
          <span className="u-example-label">这台设备</span>
        </div>
        <p className="u-trip-desc">有些心动已经留下，剩下的慢慢补齐。</p>
        <div className="u-trip-card-bottom">
          <span className="u-trip-date">{dateTime(new Date(draft.updatedAt).toISOString())}</span>
          <button
            type="button"
            className="primary u-trip-secondary"
            onClick={() => {
              resumeStep();
              navigate("/explore");
            }}
          >
            <span>继续规划</span>
            <Icon name="arrow" />
          </button>
        </div>
      </div>
    </article>
  );

  let lastMonth = "";
  const cards = visible.map(({ item, display }) => {
    const d = new Date(item.created_at);
    const month = `${d.getFullYear()} 年 ${d.getMonth() + 1} 月`;
    const heading = month !== lastMonth ? <div className="u-trip-month">{month}</div> : null;
    lastMonth = month;
    return (
      <React.Fragment key={item.trip_id}>
        {heading}
        {tripCard(item, display)}
      </React.Fragment>
    );
  });

  const newTrip = (
    <button type="button" className="u-new-trip" onClick={() => navigate("/")}>
      <span className="u-new-icon">
        <Icon name="plus" />
      </span>
      <span>
        <strong>开启下一段旅程</strong>
        <small>下一本，留给新的心动。</small>
      </span>
      <Icon name="arrow" />
    </button>
  );

  const readyCount = count("ready");

  return (
    <div className="ym">
      <div className="ym-device">
        <section className="screen u-trips" aria-label="我的行程">
          <MobileTopBar account onBack={() => navigate("/")} />
          <div className="scroll">
            <div className="screen-inner u-content">
              <header className="u-heading">
                <div className="eyebrow">YOUR TRAVEL BOOKS</div>
                <h2 className="page-title">我的行程</h2>
                <p className="sub">每一份心动，都收在当前浏览器中。</p>
                <div className="u-heading-photo" aria-hidden="true">
                  <img src={CONCEPT_ASSETS.lake} alt="" draggable={false} />
                  <i className="tape" />
                </div>
              </header>
              <div className="u-trip-stats">
                <strong>{count("all")}</strong>份记录 <span className="dot" />
                <strong>{readyCount}</strong>本可翻阅
              </div>

              <div className="u-trip-tools" style={{ marginTop: 18 }}>
                <label className="u-search" htmlFor="u-trip-search">
                  <Icon name="search" />
                  <input
                    id="u-trip-search"
                    type="search"
                    placeholder="找找某座城的旅行册"
                    autoComplete="off"
                    aria-label="搜索行程城市"
                    value={query}
                    onChange={(e) => setQuery(e.target.value)}
                  />
                  <button
                    type="button"
                    className="u-clear-search"
                    onClick={() => setQuery("")}
                    disabled={!query}
                    aria-label="清空搜索"
                  >
                    <Icon name="close" />
                  </button>
                </label>
                <div className="u-filters" aria-label="按行程状态筛选">
                  {filters.map(([value, text]) => (
                    <button
                      key={value}
                      type="button"
                      className={`chip${filter === value ? " selected" : ""}`}
                      aria-pressed={filter === value}
                      onClick={() => setFilter(value)}
                    >
                      {text} {count(value)}
                    </button>
                  ))}
                </div>
              </div>

              {actionError && (
                <p className="paper-note state-note warn" role="alert">
                  {actionError}
                </p>
              )}

              <div aria-live="polite">
                {loading ? (
                  <p className="u-demo-note">正在翻找你的旅行册…</p>
                ) : error ? (
                  <div className="u-empty">
                    <span className="u-empty-icon">
                      <Icon name="info" />
                    </span>
                    <h3>行程记录暂时没取到</h3>
                    <p>{error}</p>
                    <button type="button" className="secondary" onClick={() => window.location.reload()}>
                      重新加载
                    </button>
                  </div>
                ) : visible.length || showDraft ? (
                  <>
                    {showDraft && draftCard}
                    {cards}
                    {nextCursor && filter === "all" && !q && (
                      <button type="button" className="secondary" onClick={loadMore} disabled={loadingMore}>
                        {loadingMore ? "正在往前翻…" : "再往前翻翻"}
                      </button>
                    )}
                    {newTrip}
                  </>
                ) : (
                  <>
                    <div className="u-empty">
                      <span className="u-empty-icon">
                        <Icon name={q ? "search" : "book"} />
                      </span>
                      <h3>{q ? "没有找到这座城的行程" : "这一页，还留着空白"}</h3>
                      <p>
                        {q ? "换个城市名试试，或者看看全部旅行册。" : "还没有这类行程。下一段旅程，等你开始写。"}
                      </p>
                      {(q || filter !== "all") && (
                        <button
                          type="button"
                          className="secondary"
                          onClick={() => {
                            setQuery("");
                            setFilter("all");
                          }}
                        >
                          看看全部旅行册
                        </button>
                      )}
                    </div>
                    {newTrip}
                  </>
                )}
              </div>
              <p className="u-demo-note">纸页上的故事，随时回来接着写。</p>
            </div>
          </div>
        </section>
      </div>
    </div>
  );
};

export default MobileTripsPage;
