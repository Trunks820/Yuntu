import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import { Navigation, X } from "lucide-react";
import type { PlaceGalleryImage, TripPlace } from "@/types/trip";
import type { PlaceDetailEntry } from "@/hooks/usePlaceDetails";
import { isAnchorRole } from "@/constants/places";
import { isReducedMotionPreferred, sortPlaceGallery } from "@/utils/spreadBook";
import { shouldCloseSheetOnRelease } from "@/utils/mobileSwipe";

const EASE = "cubic-bezier(0.22, 1, 0.36, 1)";

export interface MobilePlaceSheetProps {
  place: TripPlace;
  /** 攻略页已按天取过的详情；还在取时为 loading */
  detailEntry: PlaceDetailEntry | undefined;
  /** 与列表同一套中文类型名 */
  kindLabel: string;
  /** 时间、停留等，与列表同口径，按顺序用“ · ”连起来 */
  metaParts: string[];
  nextStop: { name: string; legText: string | null } | null;
  isMustInclude: boolean;
  /** 点开它的那一站：打开时从这里展开，关上时收回这里；从地图打开时为空，改为从底部升起 */
  origin: HTMLElement | null;
  onClose: () => void;
}

function responsiveSrcSet(image: PlaceGalleryImage): string {
  const byWidth = new Map<number, string>();
  [image.thumb, image.mobile, image.desktop].forEach((variant) => {
    if (variant?.url) byWidth.set(variant.width, variant.url);
  });
  return [...byWidth.entries()]
    .sort(([left], [right]) => left - right)
    .map(([width, url]) => `${url} ${width}w`)
    .join(", ");
}

/** 从起点那一站的矩形到弹层自身矩形的反向变换；量不到就返回 null */
function flipFrom(
  origin: HTMLElement | null,
  sheet: HTMLElement,
): string | null {
  if (!origin || !origin.isConnected) return null;
  const r = origin.getBoundingClientRect();
  const s = sheet.getBoundingClientRect();
  if (r.width <= 0 || r.height <= 0 || s.width <= 0 || s.height <= 0)
    return null;
  return `translate(${r.left - s.left}px, ${r.top - s.top}px) scale(${r.width / s.width}, ${r.height / s.height})`;
}

/** 手机旅行册的地点详情：纸色宋体，从点的那一站展开，可按住顶部横杠下拉关闭 */
export function MobilePlaceSheet({
  place,
  detailEntry,
  kindLabel,
  metaParts,
  nextStop,
  isMustInclude,
  origin,
  onClose,
}: MobilePlaceSheetProps) {
  const sheetRef = useRef<HTMLElement>(null);
  const innerRef = useRef<HTMLDivElement>(null);
  const scrimRef = useRef<HTMLDivElement>(null);
  const previousFocusRef = useRef<HTMLElement | null>(null);
  const closingRef = useRef(false);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;
  const originRef = useRef(origin);
  originRef.current = origin;
  const dragRef = useRef<{
    pointerId: number;
    startY: number;
    startAt: number;
    dy: number;
  } | null>(null);
  const [failedAssets, setFailedAssets] = useState<Set<string>>(
    () => new Set(),
  );
  const [loadedUrls, setLoadedUrls] = useState<Set<string>>(() => new Set());

  const detail = detailEntry?.status === "ready" ? detailEntry.data : null;
  const loading = !detailEntry || detailEntry.status === "loading";
  const gallery = sortPlaceGallery(detail?.gallery).filter(
    (img) => !failedAssets.has(img.asset_id),
  );
  // 正文与列表同源：只用 activity_note；没有时用后端详情 summary，不回退生成期的 brief
  const note = place.activity_note?.trim() || "";
  const summary = detail?.summary?.trim() || "";
  const mainText = note || summary;
  const subText = note && summary && summary !== note ? summary : "";
  const reasons = (detail?.top_reasons ?? [])
    .map((r) => r.trim())
    .filter(Boolean);
  const warnings = (detail?.warnings ?? [])
    .map((w) => w.trim())
    .filter(Boolean);
  const sourceCount = detail?.source_count ?? 0;
  const mentionCount = detail?.mention_count ?? 0;
  const hasExtra =
    Boolean(mainText) ||
    reasons.length > 0 ||
    warnings.length > 0 ||
    sourceCount > 0 ||
    mentionCount > 0;
  const hasCoords = place.longitude != null && place.latitude != null;

  // 打开：从那一站展开（或从底部升起），内容在后段淡入
  useLayoutEffect(() => {
    const sheet = sheetRef.current;
    if (
      !sheet ||
      isReducedMotionPreferred() ||
      typeof sheet.animate !== "function"
    )
      return;
    scrimRef.current?.animate?.([{ opacity: 0 }, { opacity: 1 }], {
      duration: 300,
    });
    const from = flipFrom(originRef.current, sheet);
    if (from) {
      sheet.animate(
        [
          { transform: from, borderRadius: "14px" },
          { transform: "none", borderRadius: "22px 22px 0 0" },
        ],
        { duration: 340, easing: EASE },
      );
      innerRef.current?.animate?.(
        [{ opacity: 0 }, { opacity: 0, offset: 0.35 }, { opacity: 1 }],
        { duration: 340 },
      );
    } else {
      sheet.animate(
        [{ transform: "translateY(100%)" }, { transform: "none" }],
        { duration: 320, easing: EASE },
      );
    }
  }, []);

  const requestClose = useCallback(() => {
    if (closingRef.current) return;
    closingRef.current = true;
    const sheet = sheetRef.current;
    if (
      !sheet ||
      isReducedMotionPreferred() ||
      typeof sheet.animate !== "function"
    ) {
      onCloseRef.current();
      return;
    }
    scrimRef.current?.animate?.([{ opacity: 1 }, { opacity: 0 }], {
      duration: 260,
      fill: "forwards",
    });
    const to = flipFrom(originRef.current, sheet);
    let anim: Animation;
    if (to) {
      innerRef.current?.animate?.([{ opacity: 1 }, { opacity: 0 }], {
        duration: 120,
        fill: "forwards",
      });
      anim = sheet.animate(
        [
          { transform: "none", borderRadius: "22px 22px 0 0" },
          { transform: to, borderRadius: "14px" },
        ],
        { duration: 280, easing: EASE, fill: "forwards" },
      );
    } else {
      anim = sheet.animate(
        [{ transform: "none" }, { transform: "translateY(100%)" }],
        {
          duration: 260,
          easing: EASE,
          fill: "forwards",
        },
      );
    }
    anim.finished.then(
      () => onCloseRef.current(),
      () => onCloseRef.current(),
    );
  }, []);

  // 焦点进弹层、Esc 关闭、Tab 只在弹层里循环；关上后焦点回到原处
  useEffect(() => {
    previousFocusRef.current = document.activeElement as HTMLElement | null;
    sheetRef.current?.focus();
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        requestClose();
        return;
      }
      if (e.key !== "Tab") return;
      const sheet = sheetRef.current;
      if (!sheet) return;
      const focusable = Array.from(
        sheet.querySelectorAll<HTMLElement>(
          'button, [href], [tabindex]:not([tabindex="-1"])',
        ),
      );
      if (focusable.length === 0) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("keydown", handleKeyDown);
      previousFocusRef.current?.focus?.();
    };
  }, [requestClose]);

  // 按住顶部横杠下拉：跟手，松手按距离或速度决定关上还是弹回
  const handleGrabDown = (e: React.PointerEvent<HTMLDivElement>) => {
    if (closingRef.current) return;
    dragRef.current = {
      pointerId: e.pointerId,
      startY: e.clientY,
      startAt: performance.now(),
      dy: 0,
    };
    e.currentTarget.setPointerCapture?.(e.pointerId);
    sheetRef.current?.getAnimations?.().forEach((a) => a.cancel());
  };

  const handleGrabMove = (e: React.PointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current;
    const sheet = sheetRef.current;
    if (!drag || drag.pointerId !== e.pointerId || !sheet) return;
    drag.dy = Math.max(0, e.clientY - drag.startY);
    sheet.style.transform = `translateY(${drag.dy}px)`;
    if (scrimRef.current) {
      const h = sheet.offsetHeight || 1;
      scrimRef.current.style.opacity = String(1 - Math.min(1, drag.dy / h));
    }
  };

  const handleGrabEnd = (e: React.PointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current;
    const sheet = sheetRef.current;
    if (!drag || drag.pointerId !== e.pointerId || !sheet) return;
    dragRef.current = null;
    const dy = Math.max(0, e.clientY - drag.startY);
    const close = shouldCloseSheetOnRelease(
      dy,
      performance.now() - drag.startAt,
      sheet.offsetHeight,
    );
    const reduced =
      isReducedMotionPreferred() || typeof sheet.animate !== "function";
    if (close) {
      closingRef.current = true;
      if (reduced) {
        onCloseRef.current();
        return;
      }
      scrimRef.current?.animate?.(
        [{ opacity: scrimRef.current.style.opacity || 1 }, { opacity: 0 }],
        {
          duration: 240,
          fill: "forwards",
        },
      );
      sheet
        .animate(
          [
            { transform: `translateY(${dy}px)` },
            { transform: "translateY(100%)" },
          ],
          {
            duration: 240,
            easing: EASE,
            fill: "forwards",
          },
        )
        .finished.then(
          () => onCloseRef.current(),
          () => onCloseRef.current(),
        );
      return;
    }
    sheet.style.transform = "";
    if (scrimRef.current) scrimRef.current.style.opacity = "";
    if (!reduced && dy > 0) {
      sheet.animate(
        [{ transform: `translateY(${dy}px)` }, { transform: "none" }],
        { duration: 220, easing: EASE },
      );
    }
  };

  const markLoaded = (url: string) =>
    setLoadedUrls((prev) => (prev.has(url) ? prev : new Set(prev).add(url)));

  return (
    <div className="place-overlay">
      <div
        ref={scrimRef}
        className="place-scrim"
        onClick={requestClose}
        aria-hidden="true"
      />
      <section
        ref={sheetRef}
        className="place-sheet"
        role="dialog"
        aria-modal="true"
        aria-label={place.name}
        tabIndex={-1}
      >
        <div ref={innerRef} className="place-sheet-inner">
          <div
            className="place-grab"
            aria-hidden="true"
            onPointerDown={handleGrabDown}
            onPointerMove={handleGrabMove}
            onPointerUp={handleGrabEnd}
            onPointerCancel={handleGrabEnd}
          >
            <i />
          </div>
          <button
            type="button"
            className="place-close"
            aria-label="关闭"
            onClick={requestClose}
          >
            <X size={16} aria-hidden="true" />
          </button>

          <div className="place-scroll">
            {loading ? (
              <div className="place-gallery">
                <div className="place-photo-wait" aria-label="图片加载中" />
              </div>
            ) : gallery.length > 0 ? (
              <div
                className={[
                  "place-gallery",
                  gallery.length > 1 ? "is-multi" : "",
                ]
                  .filter(Boolean)
                  .join(" ")}
                aria-label={`${place.name}实拍`}
              >
                {gallery.map((image, idx) => {
                  const src =
                    image.mobile?.url || image.desktop?.url || image.thumb?.url;
                  if (!src) return null;
                  return (
                    <img
                      key={image.asset_id || `${place.place_id}-${idx}`}
                      src={src}
                      srcSet={responsiveSrcSet(image)}
                      sizes="(max-width: 480px) 88vw, 420px"
                      alt={image.alt_text || place.name}
                      loading={idx === 0 ? "eager" : "lazy"}
                      draggable={false}
                      className={[
                        "is-fading",
                        loadedUrls.has(src) ? "is-loaded" : "",
                      ]
                        .filter(Boolean)
                        .join(" ")}
                      onLoad={() => markLoaded(src)}
                      onError={() =>
                        setFailedAssets((prev) =>
                          new Set(prev).add(image.asset_id),
                        )
                      }
                    />
                  );
                })}
              </div>
            ) : null}

            <p className="place-k">
              <span className="place-kind">{kindLabel}</span>
              {metaParts.join(" · ")}
            </p>
            <h2>{place.name}</h2>
            {(isMustInclude || isAnchorRole(place.role)) && (
              <p className="place-tags">
                {isMustInclude && <span>你的必去</span>}
                {isAnchorRole(place.role) && <span>核心景点</span>}
              </p>
            )}

            {mainText && <p className="place-note">{mainText}</p>}
            {subText && <p className="place-sum">{subText}</p>}

            {reasons.length > 0 && (
              <>
                <h3>推荐理由</h3>
                <ul className="place-reasons">
                  {reasons.map((reason, idx) => (
                    <li key={`${place.place_id}-reason-${idx}`}>{reason}</li>
                  ))}
                </ul>
              </>
            )}

            {warnings.length > 0 && (
              <div className="place-warn">
                {warnings.map((warning, idx) => (
                  <p key={`${place.place_id}-warn-${idx}`}>{warning}</p>
                ))}
              </div>
            )}

            {(sourceCount > 0 || mentionCount > 0) && (
              <p className="place-cred">
                {[
                  sourceCount > 0 ? `综合 ${sourceCount} 个来源` : null,
                  mentionCount > 0 ? `${mentionCount} 次提及` : null,
                ]
                  .filter(Boolean)
                  .join(" · ")}
              </p>
            )}

            {!loading && !hasExtra && (
              <p className="place-empty">
                当前地点暂未收录深度图文百科
                <br />
                已为你规划在该日的行程节奏中，可直接点击下方按钮导航前往
              </p>
            )}

            {nextStop && (
              <p className="place-next">
                <b>到下一站 {nextStop.name}</b>
                {nextStop.legText}
              </p>
            )}
          </div>

          {hasCoords && (
            <div className="place-foot">
              <a
                className="place-route"
                href={`https://uri.amap.com/marker?position=${place.longitude},${place.latitude}&name=${encodeURIComponent(place.name)}`}
                target="_blank"
                rel="noopener noreferrer"
              >
                <Navigation size={16} aria-hidden="true" />
                在高德地图中查看路线
              </a>
            </div>
          )}
        </div>
      </section>
    </div>
  );
}
