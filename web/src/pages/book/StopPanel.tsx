import { useEffect, useRef } from "react";
import { X } from "lucide-react";
import type { CommuteLeg, TripPlace } from "@/types/trip";
import {
  formatNextLegText,
  formatStayMinutes,
  resolvePlaceTopReasons,
  resolveStopDetailBody,
  resolveStopKind,
  resolveStopScheduleMeta,
  sortPlaceGallery,
  STOP_KIND_LABEL,
} from "@/utils/spreadBook";
import type { PlaceDetailEntry } from "./DayStrip";
import { StopKindMark } from "./StopKindMark";

export interface StopPanelProps {
  place: TripPlace;
  nextPlace: TripPlace | null;
  nextLeg: CommuteLeg | undefined;
  detailEntry: PlaceDetailEntry | undefined;
  brokenImageUrls: ReadonlySet<string>;
  onImageError: (url: string) => void;
  onRetryDetail: (placeId: number) => void;
  onClose: () => void;
  disableEsc?: boolean;
}

export function StopPanel({
  place,
  nextPlace,
  nextLeg,
  detailEntry,
  brokenImageUrls,
  onImageError,
  onRetryDetail,
  onClose,
  disableEsc = false,
}: StopPanelProps) {
  const asideRef = useRef<HTMLElement>(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;
  const disableEscRef = useRef(disableEsc);
  disableEscRef.current = disableEsc;

  useEffect(() => {
    if (asideRef.current) {
      asideRef.current.scrollTop = 0;
    }
  }, [place.place_id]);

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

  const detail = detailEntry?.status === "ready" ? detailEntry.data : null;
  const sortedGallery = sortPlaceGallery(detail?.gallery);
  const hero = sortedGallery[0] ?? null;
  const heroUrl = hero?.desktop?.url || null;
  const hasHero = Boolean(heroUrl && !brokenImageUrls.has(heroUrl));
  const detailSettled =
    detailEntry !== undefined && detailEntry.status !== "loading";
  const kind = resolveStopKind(place.category);
  const kindLabel = STOP_KIND_LABEL[kind];

  const extraGallery = sortedGallery
    .slice(1)
    .filter(
      (img) => Boolean(img.thumb?.url) && !brokenImageUrls.has(img.thumb.url),
    );

  const scheduleMeta = resolveStopScheduleMeta(place);
  const stayText = formatStayMinutes(place.stay_minutes);
  const districtText = detail?.district?.trim() || null;
  const metaParts = [scheduleMeta.combinedText, stayText, districtText].filter(
    (part): part is string => Boolean(part),
  );

  const topReasons = resolvePlaceTopReasons(detail);
  const { itineraryText, detailSummary } = resolveStopDetailBody(
    place,
    detail,
    { withBrief: false },
  );
  const warnings = (detail?.warnings ?? [])
    .map((w) => w.trim())
    .filter(Boolean);

  const nextLegText = nextLeg ? formatNextLegText(nextLeg) : "";

  return (
    <aside
      ref={asideRef}
      className="bk-float bk-side"
      aria-label={`${place.name} 详情`}
      data-kind={kind}
    >
      <button
        type="button"
        className="bk-side-x"
        aria-label="关上"
        onClick={onClose}
      >
        <X size={16} aria-hidden="true" />
      </button>

      {hasHero && heroUrl ? (
        <img
          className="photo bk-side-hero"
          src={heroUrl}
          alt={hero?.alt_text || place.name}
          onError={() => onImageError(heroUrl)}
        />
      ) : detailSettled ? (
        <div className="bk-side-hero is-kind" aria-hidden="true">
          <StopKindMark kind={kind} />
          <span className="bk-side-hero-k">{kindLabel}</span>
        </div>
      ) : (
        <div className="bk-side-hero" aria-hidden="true" />
      )}

      <div className="bk-pg">
        <p className="bk-k">
          <span className="bk-kind">{kindLabel}</span>
          {scheduleMeta.period && (
            <i className={`sky ${scheduleMeta.period}`} aria-hidden="true" />
          )}
          {metaParts.length > 0 && <span>{metaParts.join(" · ")}</span>}
        </p>

        <h3>{place.name}</h3>

        {itineraryText && <p className="bk-n">{itineraryText}</p>}

        {detailSummary && <p className="bk-n-sub">{detailSummary}</p>}

        {topReasons.length > 0 && (
          <div className="bk-reasons">
            <h4>推荐理由</h4>
            <ul>
              {topReasons.map((reason, idx) => (
                <li key={`${place.place_id}-reason-${idx}`}>{reason}</li>
              ))}
            </ul>
          </div>
        )}

        {detailEntry?.status === "error" && (
          <div className="bk-detail-err" role="status">
            <span>详情暂时打不开</span>
            <button type="button" onClick={() => onRetryDetail(place.place_id)}>
              再试一次
            </button>
          </div>
        )}

        {warnings.length > 0 && (
          <div className="bk-warn" aria-label="注意事项">
            {warnings.map((w, idx) => (
              <p key={`${place.place_id}-warn-${idx}`}>{w}</p>
            ))}
          </div>
        )}

        {extraGallery.length > 0 && (
          <div className="bk-gallery" aria-label="更多实拍">
            {extraGallery.map((img, idx) => (
              <img
                key={
                  img.asset_id ||
                  `${place.place_id}-gallery-${img.position ?? idx}-${idx}`
                }
                className="photo"
                src={img.thumb.url}
                alt={img.alt_text || place.name}
                loading="lazy"
                onError={() => onImageError(img.thumb.url)}
              />
            ))}
          </div>
        )}

        {nextPlace && (
          <p className="bk-nx">
            <b>到下一站 {nextPlace.name}</b>
            {nextLegText}
          </p>
        )}
      </div>
    </aside>
  );
}
