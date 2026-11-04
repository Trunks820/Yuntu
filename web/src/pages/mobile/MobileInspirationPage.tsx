import React, {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { useLocation, useNavigate } from "react-router-dom";
import {
  fetchPoiInspirationCities,
  fetchPoiInspiration,
  type PoiInspirationCity,
  type PoiInspirationPlace,
} from "@/api/poi";
import { ApiRequestError } from "@/services/errors";
import { useMobileTripStore } from "@/stores/mobileTripStore";
import { MobileTopBar } from "@/components/mobile/MobileTopBar";
import { Icon, MobileSheet } from "@/components/mobile/mobileUi";
import { findCachedDestination } from "@/components/mobile/mobileFormat";
import { useDestinations } from "@/hooks/useDestinations";
import { CITY_PLACEHOLDER, cityPhotoSources } from "@/utils/cityPhotoSources";
import {
  loadInspirationSaves,
  saveInspirationSaves,
  type InspirationSave,
} from "@/utils/inspirationSaves";
import {
  MOBILE_MUST_INCLUDE_LIMIT,
  type MobileMustIncludePlace,
} from "@/types/mobile";
import type { Destination } from "@/types/destination";
import type { PlaceGalleryImage } from "@/types/trip";
import "@/styles/mobile.css";

const GUEST_OWNER = "guest";

type View = "swipe" | "saved" | "city";

/**
 * 不用先选城市：灵感地点按城市读，所以每轮取几座城、每座几个地点，交错成一叠；
 * 滑到只剩几张时再取下一轮，所有城市都翻完为止。
 * 收藏过的城市在下一轮排在前面、多取几个，但每轮仍留位置给别的城市
 */
const CITY_BATCH = 4;
const PER_CITY = 5;
const PREFETCH_LEFT = 3;
// 收藏过的城市：每轮最多排进几座、每座多取几个；其余位置留给还没收藏过的城市
const HOT_CITIES = 2;
const HOT_PER_CITY = 8;
const SWIPE_THRESHOLD = 90;
const SWIPE_OUT_MS = 210;
const TOAST_MS = 2400;

interface SwipeUndo {
  index: number;
  city: string;
  placeId: number;
  added: boolean;
}

type CardPlace = PoiInspirationPlace & { city: string };
type PendingCity = { city: string; afterId: number };

function responsiveSrcSet(image: PlaceGalleryImage): string {
  const variants = [image.thumb, image.mobile, image.desktop];
  const byWidth = new Map<number, string>();
  variants.forEach((variant) => byWidth.set(variant.width, variant.url));
  return [...byWidth.entries()]
    .sort(([left], [right]) => left - right)
    .map(([width, url]) => `${url} ${width}w`)
    .join(", ");
}

/** 挑下一轮的城市：收藏越多的城越靠前，其余按原来的顺序补满 */
function planRound(
  queue: PendingCity[],
  saves: InspirationSave[],
): PendingCity[] {
  const counts = new Map<string, number>();
  for (const s of saves) counts.set(s.city, (counts.get(s.city) ?? 0) + 1);
  const hot = queue
    .filter((q) => counts.has(q.city))
    .sort((a, b) => (counts.get(b.city) ?? 0) - (counts.get(a.city) ?? 0))
    .slice(0, HOT_CITIES);
  const rest = queue
    .filter((q) => !hot.includes(q))
    .slice(0, CITY_BATCH - hot.length);
  return [...hot, ...rest];
}

/** 各城的结果轮流取一个，排成一叠 */
function interleave(lists: CardPlace[][]): CardPlace[] {
  const out: CardPlace[] = [];
  const longest = Math.max(0, ...lists.map((l) => l.length));
  for (let i = 0; i < longest; i++) {
    for (const list of lists) if (list[i]) out.push(list[i]);
  }
  return out;
}

function prefersReducedMotion(): boolean {
  return (
    typeof window !== "undefined" &&
    typeof window.matchMedia === "function" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches
  );
}

/** 想去清单城市卡片图：用这座城自己的图，没有就不放图，绝不拿别的城市代替 */
function cityPhoto(
  cityName: string,
  destination?: Destination | null,
): string | null {
  const src = cityPhotoSources(cityName, destination ?? undefined)[0];
  return src === CITY_PLACEHOLDER ? null : src;
}

interface InspirationCardGalleryProps {
  gallery: PlaceGalleryImage[];
  isDraggingRef: React.RefObject<boolean>;
  justDraggedRef: React.RefObject<boolean>;
}

const InspirationCardGallery: React.FC<InspirationCardGalleryProps> = ({
  gallery,
  isDraggingRef,
  justDraggedRef,
}) => {
  const [photoIndex, setPhotoIndex] = useState(0);
  const [imageError, setImageError] = useState(false);

  const hasMultiplePhotos = gallery.length > 1;
  const currentPhoto = gallery[photoIndex] ?? gallery[0];

  const handlePrevPhoto = (e: React.MouseEvent) => {
    e.stopPropagation();
    if (isDraggingRef.current || justDraggedRef.current) return;
    if (photoIndex <= 0) return;
    setPhotoIndex((i) => i - 1);
    setImageError(false);
  };

  const handleNextPhoto = (e: React.MouseEvent) => {
    e.stopPropagation();
    if (isDraggingRef.current || justDraggedRef.current) return;
    if (photoIndex >= gallery.length - 1) return;
    setPhotoIndex((i) => i + 1);
    setImageError(false);
  };

  return (
    <>
      {hasMultiplePhotos && (
        <div className="photo-indicators" aria-hidden="true">
          {gallery.map((img, i) => (
            <span
              key={img.asset_id || i}
              className={`photo-indicator-segment${i === photoIndex ? " active" : ""}`}
            />
          ))}
        </div>
      )}
      {currentPhoto && !imageError ? (
        <img
          key={currentPhoto.asset_id}
          src={currentPhoto.mobile.url}
          srcSet={responsiveSrcSet(currentPhoto)}
          sizes="(max-width: 480px) 100vw, 420px"
          width={currentPhoto.mobile.width}
          height={currentPhoto.mobile.height}
          alt={currentPhoto.alt_text}
          onError={() => setImageError(true)}
          draggable={false}
        />
      ) : (
        <span className="poi-noimage" aria-hidden="true" />
      )}
      {hasMultiplePhotos && (
        <>
          <button
            type="button"
            className="photo-tap-area prev"
            aria-label="上一张照片"
            aria-disabled={photoIndex === 0}
            onClick={handlePrevPhoto}
          />
          <button
            type="button"
            className="photo-tap-area next"
            aria-label="下一张照片"
            aria-disabled={photoIndex === gallery.length - 1}
            onClick={handleNextPhoto}
          />
        </>
      )}
    </>
  );
};

export const MobileInspirationPage: React.FC = () => {
  const navigate = useNavigate();
  const location = useLocation();
  // 从旅行册或出发页回来改地点时，直接打开那座城的清单
  const openCityOnEnter = (location.state as { openCity?: unknown } | null)
    ?.openCity;
  const pendingOpenCity = useRef(
    typeof openCityOnEnter === "string" ? openCityOnEnter : null,
  );
  const userId: string | null = GUEST_OWNER;

  const draft = useMobileTripStore((s) => s.draft);
  const startInspirationTrip = useMobileTripStore(
    (s) => s.startInspirationTrip,
  );
  const setMustInclude = useMobileTripStore((s) => s.setMustInclude);

  const [view, setView] = useState<View>("swipe");

  // 目的地信息：仅用于想去清单的城市缩略图与开始行程时的 destination id；加载失败不阻碍翻卡片
  const { data: destData } = useDestinations();
  const destinations = useMemo(() => destData?.destinations ?? [], [destData]);
  const destinationOf = (cityName: string) =>
    destinations.find((d) => d.name === cityName) ??
    findCachedDestination(null, cityName);

  // cities 接口：灵感发牌城市清单，顺序不变原样使用
  const [citiesError, setCitiesError] = useState(false);
  const [cityList, setCityList] = useState<PoiInspirationCity[] | null>(null);

  // 想去清单：按账号存在这台设备上
  const [saves, setSaves] = useState<InspirationSave[]>([]);
  // 取下一轮时按最新的收藏排城市，不因收藏变化重新开始
  const savesRef = useRef(saves);
  savesRef.current = saves;
  useEffect(() => {
    const loaded = userId ? loadInspirationSaves(userId) : [];
    savesRef.current = loaded;
    setSaves(loaded);
  }, [userId]);
  useEffect(() => {
    const target = pendingOpenCity.current;
    if (!userId || !target) return;
    pendingOpenCity.current = null;
    openCityList(target, loadInspirationSaves(userId));
    // 只在进入页面、读到收藏后执行一次
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [userId]);

  // 混着各城的灵感地点；pending 是还没读完的城市和各自的翻页位置
  const [places, setPlaces] = useState<CardPlace[]>([]);
  const [pending, setPending] = useState<PendingCity[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState(false);
  const [index, setIndex] = useState(0);
  const [undoStack, setUndoStack] = useState<SwipeUndo[]>([]);
  const [leaving, setLeaving] = useState<"like" | "pass" | null>(null);
  const [dragX, setDragX] = useState(0);
  const dragStartPos = useRef<{ x: number; y: number } | null>(null);
  const isDraggingRef = useRef(false);
  const justDraggedRef = useRef(false);
  const justDraggedTimer = useRef<number | undefined>(undefined);
  const abortRef = useRef<AbortController | null>(null);

  // 顺手预加载下一张卡片的首图，选图参数与实际 <img> 保持一致
  useEffect(() => {
    const nextPlace = places[index + 1];
    const nextPhoto = nextPlace?.gallery?.[0];
    if (nextPhoto) {
      const img = new Image();
      img.sizes = "(max-width: 480px) 100vw, 420px";
      img.srcset = responsiveSrcSet(nextPhoto);
      img.src = nextPhoto.mobile.url;
    }
  }, [places, index]);

  // 某座城的清单与这次要带上的地点
  const [listCity, setListCity] = useState<string | null>(null);
  const [selection, setSelection] = useState<number[]>([]);
  const [confirmReplace, setConfirmReplace] = useState(false);

  const [toastMessage, setToastMessage] = useState<string | null>(null);
  const toastTimer = useRef<number | undefined>(undefined);
  const showToast = useCallback((msg: string) => {
    setToastMessage(msg);
    window.clearTimeout(toastTimer.current);
    toastTimer.current = window.setTimeout(
      () => setToastMessage(null),
      TOAST_MS,
    );
  }, []);
  useEffect(
    () => () => {
      window.clearTimeout(toastTimer.current);
      window.clearTimeout(justDraggedTimer.current);
      abortRef.current?.abort();
    },
    [],
  );

  const loadCities = useCallback(async (signal?: AbortSignal) => {
    setCitiesError(false);
    try {
      const res = await fetchPoiInspirationCities(signal);
      setCityList(res.cities);
      return res.cities;
    } catch {
      if (signal?.aborted) return null;
      setCitiesError(true);
      return null;
    }
  }, []);

  const loadRound = useCallback(async (queue: PendingCity[]) => {
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    const batch = planRound(queue, savesRef.current);
    const hotCities = new Set(savesRef.current.map((s) => s.city));
    setLoading(true);
    setLoadError(false);
    const results = await Promise.allSettled(
      batch.map((b) =>
        fetchPoiInspiration(
          b.city,
          b.afterId,
          hotCities.has(b.city) ? HOT_PER_CITY : PER_CITY,
          controller.signal,
        ),
      ),
    );
    if (controller.signal.aborted) {
      // 被页面卸载打断时（没有新一轮接手）解除加载中，回来后可以重新取
      if (abortRef.current === controller) setLoading(false);
      return;
    }
    const lists: CardPlace[][] = [];
    const more: PendingCity[] = [];
    const failed: PendingCity[] = [];

    results.forEach((r, i) => {
      const { city } = batch[i];
      if (r.status === "fulfilled") {
        lists.push(r.value.places.map((p) => ({ ...p, city })));
        if (r.value.next_after_id !== null)
          more.push({ city, afterId: r.value.next_after_id });
      } else {
        const err = r.reason;
        const isCityNotFound =
          (err instanceof ApiRequestError &&
            err.code === "POI_CATALOG_CITY_NOT_FOUND") ||
          (err &&
            typeof err === "object" &&
            "code" in err &&
            (err as { code: string }).code === "POI_CATALOG_CITY_NOT_FOUND");
        if (isCityNotFound) {
          // 城市不存在（POI_CATALOG_CITY_NOT_FOUND）：这座城直接跳过，不再重试
        } else {
          // 网络错误、502、503 及其他404等：失败的城市排到最后重试
          failed.push(batch[i]);
        }
      }
    });

    const nextQueue = [
      ...queue.filter((q) => !batch.includes(q)),
      ...more,
      ...failed,
    ];
    setPending(nextQueue);

    if (failed.length > 0 && lists.length === 0) {
      // 这一轮没有一个成功且有可重试的失败：停下来让用户重试
      setLoadError(true);
    } else if (lists.length > 0) {
      setPlaces((prev) => {
        // 已经在想去清单里的地点不再发牌
        const seen = new Set([
          ...prev.map((p) => `${p.city}:${p.place_id}`),
          ...savesRef.current.map((s) => `${s.city}:${s.placeId}`),
        ]);
        return [
          ...prev,
          ...interleave(lists).filter(
            (p) => !seen.has(`${p.city}:${p.place_id}`),
          ),
        ];
      });
    }
    setLoading(false);
  }, []);

  // 登录后，从 cities 接口取城市清单并开始逛
  useEffect(() => {
    if (!userId) {
      setCityList(null);
      setPending(null);
      setPlaces([]);
      setIndex(0);
      setUndoStack([]);
      setLoadError(false);
      setCitiesError(false);
      return;
    }
    const controller = new AbortController();
    setPlaces([]);
    setIndex(0);
    setUndoStack([]);
    setLoadError(false);
    void (async () => {
      const cities = await loadCities(controller.signal);
      if (controller.signal.aborted || !cities) return;
      setPending(cities.map((c) => ({ city: c.city, afterId: 0 })));
    })();
    return () => controller.abort();
  }, [userId, loadCities]);

  const retryCities = useCallback(() => {
    if (!userId) return;
    setPlaces([]);
    setIndex(0);
    setUndoStack([]);
    setLoadError(false);
    void (async () => {
      const cities = await loadCities();
      if (!cities) return;
      setPending(cities.map((c) => ({ city: c.city, afterId: 0 })));
    })();
  }, [userId, loadCities]);

  // 快翻完时取下一轮
  useEffect(() => {
    if (!userId || !pending || pending.length === 0 || loading || loadError)
      return;
    if (places.length - index <= PREFETCH_LEFT) void loadRound(pending);
  }, [userId, pending, index, places.length, loading, loadError, loadRound]);

  const persist = (next: InspirationSave[]) => {
    setSaves(next);
    if (userId && !saveInspirationSaves(userId, next)) {
      showToast("浏览器未允许保存，收藏仅在本次打开期间保留。");
      return false;
    }
    return true;
  };

  const isSaved = (cityName: string, placeId: number) =>
    saves.some((s) => s.city === cityName && s.placeId === placeId);

  const current = places[index] ?? null;
  const finishedLoading =
    pending !== null && pending.length === 0 && !loading && !loadError;
  const exhausted = !current && finishedLoading && places.length > 0;

  const decide = (like: boolean) => {
    if (!current || leaving) return;
    const added = like && !isSaved(current.city, current.place_id);
    setUndoStack((stack) => [
      ...stack,
      { index, city: current.city, placeId: current.place_id, added },
    ]);
    let stored = true;
    if (added) {
      stored = persist([
        ...saves,
        {
          placeId: current.place_id,
          name: current.name,
          city: current.city,
          district: current.district,
          savedAt: Date.now(),
        },
      ]);
    }
    const name = current.name;
    const finish = () => {
      setLeaving(null);
      setDragX(0);
      setIndex((i) => i + 1);
      if (added && stored) showToast(`已收藏「${name}」`);
    };
    if (prefersReducedMotion()) {
      finish();
      return;
    }
    setLeaving(like ? "like" : "pass");
    window.setTimeout(finish, SWIPE_OUT_MS);
  };

  const undo = () => {
    const last = undoStack[undoStack.length - 1];
    if (!last || leaving) return;
    setUndoStack((stack) => stack.slice(0, -1));
    setIndex(last.index);
    if (last.added) {
      persist(
        saves.filter(
          (s) => !(s.city === last.city && s.placeId === last.placeId),
        ),
      );
    }
  };

  const restart = () => {
    // 再逛一遍时，这一轮收藏的地点也不再出现
    setPlaces((prev) => prev.filter((p) => !isSaved(p.city, p.place_id)));
    setIndex(0);
    setUndoStack([]);
  };

  // 清单按城市分组，保持收藏的先后顺序
  const savedCities = useMemo(() => {
    const groups = new Map<string, InspirationSave[]>();
    for (const save of saves) {
      const list = groups.get(save.city) ?? [];
      list.push(save);
      groups.set(save.city, list);
    }
    return [...groups.entries()];
  }, [saves]);

  const listSaves = listCity ? saves.filter((s) => s.city === listCity) : [];

  function openCityList(cityName: string, from: InspirationSave[] = saves) {
    const saved = new Set(
      from.filter((s) => s.city === cityName).map((s) => s.placeId),
    );
    // 从旅行册回来改地点时，带上次选好的
    const previous =
      draft.entry === "inspiration" && draft.cityName === cityName
        ? draft.mustInclude.map((p) => p.placeId).filter((id) => saved.has(id))
        : [];
    setListCity(cityName);
    setSelection(previous);
    setView("city");
  }

  const togglePick = (placeId: number) => {
    if (selection.includes(placeId)) {
      setSelection((sel) => sel.filter((id) => id !== placeId));
    } else if (selection.length < MOBILE_MUST_INCLUDE_LIMIT) {
      setSelection((sel) => [...sel, placeId]);
    } else {
      showToast(
        `这次先带上最多 ${MOBILE_MUST_INCLUDE_LIMIT} 个，其他的仍在收藏里。`,
      );
    }
  };

  const removeSave = (save: InspirationSave) => {
    persist(
      saves.filter(
        (s) => !(s.city === save.city && s.placeId === save.placeId),
      ),
    );
    setSelection((sel) => sel.filter((id) => id !== save.placeId));
    setUndoStack([]);
    showToast("已取消收藏");
  };

  const hasOtherDraft = Boolean(
    draft.sceneIds.length > 0 ||
    draft.cityName ||
    draft.cityConfirmed ||
    draft.rhythm,
  );
  // 同一座城的灵感草稿接着改，不算替换
  const continuing =
    draft.entry === "inspiration" && draft.cityName === listCity;

  const pickedPlaces = (): MobileMustIncludePlace[] =>
    selection
      .map((id) => listSaves.find((s) => s.placeId === id))
      .filter((s): s is InspirationSave => Boolean(s))
      .map((s) => ({ placeId: s.placeId, name: s.name }));

  const startTrip = () => {
    if (!listCity || selection.length === 0) return;
    startInspirationTrip(
      destinationOf(listCity)?.id ?? null,
      listCity,
      pickedPlaces(),
    );
    setConfirmReplace(false);
    navigate("/explore");
  };

  const handlePlan = () => {
    if (selection.length === 0) return;
    // 接着改同一座城：只换地点，回到原来那一步，其他选择都不动
    if (continuing) {
      setMustInclude(pickedPlaces());
      navigate("/explore");
      return;
    }
    if (hasOtherDraft) {
      setConfirmReplace(true);
      return;
    }
    startTrip();
  };

  const handleBack = () => {
    if (view === "city") setView("saved");
    else if (view === "saved") setView("swipe");
    else navigate("/");
  };

  const topTitle =
    view === "city" && listCity
      ? `${listCity} · 想去清单`
      : view === "saved"
        ? "想去清单"
        : "收集灵感";

  const cardStyle: React.CSSProperties | undefined = leaving
    ? {
        transform: `translateX(${leaving === "like" ? 360 : -360}px) rotate(${leaving === "like" ? 12 : -12}deg)`,
        opacity: 0,
      }
    : dragX
      ? {
          transform: `translateX(${dragX}px) rotate(${dragX / 22}deg)`,
          transition: "none",
        }
      : undefined;

  const renderSwipe = () => {
    if (citiesError && !current) {
      return (
        <div className="inspire-empty" role="alert">
          <h3>城市目录没能加载出来。</h3>
          <p>可能是网络不稳定，再试一次吧。</p>
          <button type="button" className="secondary" onClick={retryCities}>
            重新加载
          </button>
        </div>
      );
    }
    if (loadError && !current) {
      return (
        <div className="inspire-empty" role="alert">
          <h3>地点没能加载出来。</h3>
          <p>可能是网络不稳定，再试一次吧。</p>
          <button
            type="button"
            className="secondary"
            onClick={() => setLoadError(false)}
          >
            重新加载
          </button>
        </div>
      );
    }
    if (cityList && cityList.length === 0 && places.length === 0) {
      return (
        <div className="inspire-empty">
          <h3>暂时还没有可收藏的地点。</h3>
          <p>过些时候再来看看吧。</p>
        </div>
      );
    }
    if (!current && finishedLoading && places.length === 0) {
      return (
        <div className="inspire-empty">
          <h3>暂时还没有可收藏的地点。</h3>
          <p>过些时候再来看看吧。</p>
        </div>
      );
    }
    if (!current && !exhausted) {
      return (
        <p className="inspire-empty" role="status">
          正在翻出各地的地方...
        </p>
      );
    }
    if (exhausted) {
      return (
        <div className="inspire-empty">
          <Icon name="heart" />
          <h3>这一叠，翻完啦。</h3>
          <p>
            喜欢的地方都收好了。
            <br />
            现在不出发，也没关系。
          </p>
          <button type="button" className="secondary" onClick={restart}>
            再逛一遍
          </button>
          <button
            type="button"
            className="text-btn"
            onClick={undo}
            disabled={undoStack.length === 0}
          >
            撤销最后一次滑动
          </button>
        </div>
      );
    }
    const place = current as CardPlace;
    const saved = isSaved(place.city, place.place_id);
    const total =
      pending && pending.length === 0
        ? String(places.length)
        : `${places.length}+`;
    return (
      <>
        <div className="swipe-stack poi-stack">
          <div className="stack-back two" />
          <div className="stack-back" />
          <article
            key={`${place.city}:${place.place_id}`}
            className="swipe-card"
            aria-label={`${place.city} ${place.name}，可以左右滑动`}
            style={cardStyle}
            onPointerDown={(e) => {
              if (leaving) return;
              dragStartPos.current = { x: e.clientX, y: e.clientY };
              isDraggingRef.current = false;
            }}
            onPointerMove={(e) => {
              if (dragStartPos.current === null) return;
              const dx = e.clientX - dragStartPos.current.x;
              if (Math.abs(dx) > 8) {
                isDraggingRef.current = true;
                setDragX(dx);
              }
            }}
            onPointerUp={() => {
              if (dragStartPos.current === null) return;
              const dragged = isDraggingRef.current;
              dragStartPos.current = null;
              if (dragged) {
                justDraggedRef.current = true;
                window.clearTimeout(justDraggedTimer.current);
                justDraggedTimer.current = window.setTimeout(() => {
                  justDraggedRef.current = false;
                  isDraggingRef.current = false;
                }, 100);
                if (Math.abs(dragX) >= SWIPE_THRESHOLD) decide(dragX > 0);
                else setDragX(0);
              } else {
                setDragX(0);
              }
            }}
            onPointerCancel={() => {
              dragStartPos.current = null;
              isDraggingRef.current = false;
              setDragX(0);
            }}
          >
            <InspirationCardGallery
              key={place.place_id}
              gallery={place.gallery ?? []}
              isDraggingRef={isDraggingRef}
              justDraggedRef={justDraggedRef}
            />
            <div
              className="swipe-label"
              style={
                dragX > 40
                  ? { opacity: Math.min(1, dragX / SWIPE_THRESHOLD) }
                  : undefined
              }
            >
              想去
            </div>
            <span className="poi-city">
              <Icon name="pin" />
              {place.city}
              {place.district ? ` · ${place.district}` : ""}
            </span>
            <span className="swipe-number">
              {index + 1} / {total}
            </span>
            <span className="moment-copy">
              <small>A PLACE ON YOUR WISHLIST</small>
              <strong>{place.name}</strong>
              <p>
                {saved
                  ? "已经在你的想去清单里。"
                  : "也许，下一段旅程就从这里开始。"}
              </p>
            </span>
          </article>
        </div>
        <div className="swipe-decision">
          <button
            type="button"
            className="undo"
            aria-label="撤销上一次滑动"
            onClick={undo}
            disabled={undoStack.length === 0 || Boolean(leaving)}
          >
            <Icon name="undo" />
          </button>
          <button
            type="button"
            className="pass"
            aria-label="暂时跳过这个地点"
            onClick={() => decide(false)}
            disabled={Boolean(leaving)}
          >
            <Icon name="close" />
          </button>
          <button
            type="button"
            className="like"
            aria-label="收藏这个地点"
            onClick={() => decide(true)}
            disabled={Boolean(leaving)}
          >
            <Icon name="heart" />
          </button>
        </div>
        <p className="swipe-help">左滑暂时跳过 · 右滑收藏想去</p>
      </>
    );
  };

  const renderContent = () => {
    if (view === "saved") {
      return (
        <>
          <div className="eyebrow">SOMEWHERE, SOMEDAY</div>
          <h2 className="page-title">
            心动可以分散，
            <br />
            这次，先去<em>一座城</em>。
          </h2>
          <p className="sub">
            选一座城，再挑最多 {MOBILE_MUST_INCLUDE_LIMIT} 个地点带进这次旅行。
          </p>
          {savedCities.length ? (
            <>
              <div className="saved-cities">
                {savedCities.map(([cityName, list]) => {
                  const img = cityPhoto(cityName, destinationOf(cityName));
                  return (
                    <button
                      key={cityName}
                      type="button"
                      className="saved-city"
                      onClick={() => openCityList(cityName)}
                    >
                      <span className="poi-image">
                        {img ? (
                          <img
                            src={img}
                            alt={`${cityName}城市照片`}
                            draggable={false}
                          />
                        ) : null}
                      </span>
                      <span>
                        <strong>{cityName}</strong>
                        <small>{list.length} 个想去的地方</small>
                        <p>
                          {list
                            .slice(0, 3)
                            .map((s) => s.name)
                            .join(" · ")}
                        </p>
                      </span>
                      <Icon name="chevron" />
                    </button>
                  );
                })}
              </div>
              <p className="inspire-footnote">
                其他城市的收藏会一直留着。
                <br />
                收藏只保存在这台设备上。
              </p>
            </>
          ) : (
            <div className="inspire-empty">
              <h3>还没有想去的地方。</h3>
              <p>不着急，去逛逛，遇见喜欢的再留下。</p>
            </div>
          )}
        </>
      );
    }
    if (view === "city" && listCity) {
      return (
        <>
          <div className="eyebrow">A FEW PLACES FOR THIS TRIP</div>
          <h2 className="page-title">
            把{listCity}的心动，
            <br />
            带进<em>这一次</em>。
          </h2>
          <div className="saved-pois">
            {listSaves.length ? (
              listSaves.map((save) => {
                const on = selection.includes(save.placeId);
                return (
                  <article key={save.placeId} className="saved-poi">
                    <button
                      type="button"
                      className={`poi-pick${on ? "selected" : ""}`}
                      aria-pressed={on}
                      onClick={() => togglePick(save.placeId)}
                    >
                      <span>
                        <strong>{save.name}</strong>
                        <small>
                          {save.district ? `${save.district} · ` : ""}
                          {on ? "已选入这次旅行" : "留在想去清单里"}
                        </small>
                      </span>
                      <span className="poi-check">
                        {on ? <Icon name="check" /> : <Icon name="plus" />}
                      </span>
                    </button>
                    <button
                      type="button"
                      className="poi-remove"
                      aria-label={`取消收藏${save.name}`}
                      onClick={() => removeSave(save)}
                    >
                      取消收藏
                    </button>
                  </article>
                );
              })
            ) : (
              <p className="inspire-footnote">
                这座城暂时没有收藏，回清单看看其他城市吧。
              </p>
            )}
          </div>
          <p className="inspire-demo">路线、开放时间与可安排性未核验。</p>
        </>
      );
    }
    return (
      <>
        <div className="eyebrow">SAVE A PLACE, KEEP A POSSIBILITY</div>
        <h2 className="page-title">
          世界很大，
          <br />
          先留下<em>想去的地方</em>。
        </h2>
        <p className="sub">不用先选城市，遇见喜欢的就收藏。</p>
        {renderSwipe()}
      </>
    );
  };

  const renderFooter = () => {
    if (view === "city") {
      return (
        <div className="footer-action">
          <button
            type="button"
            className="primary"
            onClick={handlePlan}
            disabled={selection.length === 0}
          >
            <span>
              带上这 {selection.length} 个，去{listCity}
            </span>
            <Icon name="arrow" />
          </button>
          <div className="footer-note">
            {selection.length} / {MOBILE_MUST_INCLUDE_LIMIT} 个候选 ·
            没选的仍会保留
          </div>
        </div>
      );
    }
    if (view === "saved") {
      return (
        <div className="footer-action">
          <button
            type="button"
            className="primary"
            onClick={() => setView("swipe")}
          >
            <span>随处逛逛</span>
            <Icon name="arrow" />
          </button>
        </div>
      );
    }
    if (!userId) return null;
    return (
      <div className="footer-action">
        <button
          type="button"
          className="primary"
          onClick={() => setView("saved")}
        >
          <span>想去清单{saves.length ? ` · ${saves.length}` : ""}</span>
          <Icon name="arrow" />
        </button>
        <div className="footer-note">
          收藏不限 {MOBILE_MUST_INCLUDE_LIMIT} 个，规划时再挑。
        </div>
      </div>
    );
  };

  return (
    <div className="ym">
      <div className="ym-device">
        <section className="screen swipe" aria-label={topTitle}>
          <MobileTopBar
            title={topTitle}
            onBack={handleBack}
            onHome={() => navigate("/")}
          />
          <div className="scroll">
            <div className="screen-inner page-pad">{renderContent()}</div>
          </div>
          {renderFooter()}
        </section>

        {confirmReplace && (
          <MobileSheet
            title="开始新的旅程？"
            onClose={() => setConfirmReplace(false)}
          >
            <p>开始新的旅程会替换上次的草稿。想去清单不受影响。</p>
            <button type="button" className="primary" onClick={startTrip}>
              <span>开始新的旅程</span>
              <Icon name="arrow" />
            </button>
            <button
              type="button"
              className="secondary"
              onClick={() => setConfirmReplace(false)}
            >
              保留上次的草稿
            </button>
          </MobileSheet>
        )}

        {toastMessage && (
          <div className="toast" role="status" aria-live="polite">
            {toastMessage}
          </div>
        )}
      </div>
    </div>
  );
};

export default MobileInspirationPage;


