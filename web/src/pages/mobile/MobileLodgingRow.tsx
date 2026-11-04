import { useState } from "react";
import type {
  AccommodationInfo,
  LodgingDisplay,
  LodgingDisplayGroup,
} from "@/types/trip";
import { Icon } from "@/components/mobile/mobileUi";

function hasNightlyPrice(group: LodgingDisplayGroup): boolean {
  return group.nightly_low_cny != null && group.nightly_high_cny != null;
}

function formatApproxRange(
  low: number | null | undefined,
  high: number | null | undefined,
  unit: string,
): string | null {
  const hasLow = low != null;
  const hasHigh = high != null;
  if (hasLow && hasHigh) {
    return low === high ? `约${low}${unit}` : `约${low}–${high}${unit}`;
  }
  if (hasLow) return `约${low}${unit}`;
  if (hasHigh) return `约${high}${unit}`;
  return null;
}

function formatGroupLine(group: LodgingDisplayGroup): string {
  const hotels = (group.hotels ?? []).join("、");
  const price = hasNightlyPrice(group)
    ? formatApproxRange(group.nightly_low_cny, group.nightly_high_cny, "元/晚")
    : null;
  if (price && hotels) return `${group.label}  ${price} · ${hotels}`;
  if (price) return `${group.label}  ${price}`;
  if (hotels) return `${group.label}  ${hotels}`;
  return group.label;
}

function formatUserLodgingText(display: LodgingDisplay): string {
  const range = formatApproxRange(
    display.round_trip_minutes_low,
    display.round_trip_minutes_high,
    "分钟",
  );
  const head = `从你住的${display.area_label}出发`;
  return range ? `${head}，每天来回${range}` : head;
}

function LegacyLodgingRow({
  accommodation,
}: {
  accommodation: AccommodationInfo;
}) {
  return (
    <div className="manifest-row">
      <Icon name="info" />
      <span>
        <b>住宿参考</b>：{accommodation.name}
        {accommodation.reason ? `（${accommodation.reason}）` : ""}
      </span>
    </div>
  );
}

export function MobileLodgingRow({
  accommodation,
}: {
  accommodation?: AccommodationInfo | null;
}) {
  const [expanded, setExpanded] = useState(false);

  if (!accommodation) return null;

  const display = accommodation.display;
  if (display == null) {
    return <LegacyLodgingRow accommodation={accommodation} />;
  }

  if (display.layout === "user_lodging") {
    return (
      <div className="manifest-row">
        <Icon name="info" />
        <span>
          <b>住宿</b>：{formatUserLodgingText(display)}
          {display.price_status === "待核实" ? (
            <em className="lodging-pending">待核实</em>
          ) : null}
        </span>
      </div>
    );
  }

  const groups = display.groups ?? [];
  const notices = display.notices ?? [];
  const unmatched = notices.filter((n) => n.kind === "user_lodging_unmatched");
  const holiday = notices.filter((n) => n.kind === "holiday");
  const caption = display.caption ?? "";
  const hasGroups = groups.length > 0;
  const detailsVisible = !hasGroups || expanded;
  const hasDetailBody = Boolean(caption) || hasGroups || holiday.length > 0;
  const pricePending = display.price_status === "待核实";
  const showPriceNote =
    detailsVisible && hasGroups && groups.some(hasNightlyPrice) && !pricePending;

  return (
    <>
      {unmatched.map((n, i) => (
        <div className="manifest-row lodging-unmatched" key={`unmatched-${i}`}>
          <Icon name="warn" />
          <span>{n.text}</span>
        </div>
      ))}
      <div className="manifest-row">
        <Icon name="info" />
        <span>
          <b>住宿参考</b>：{display.area_label}
          {pricePending ? <em className="lodging-pending">待核实</em> : null}
          {accommodation.reason ? `（${accommodation.reason}）` : ""}
          {hasGroups && (
            <button
              type="button"
              className="lodging-nearby-toggle"
              aria-expanded={expanded}
              onClick={() => setExpanded((open) => !open)}
            >
              附近酒店 {expanded ? "▴" : "▾"}
            </button>
          )}
        </span>
      </div>
      {detailsVisible && hasDetailBody && (
        <div className="lodging-detail">
          {caption ? <div className="lodging-caption">{caption}</div> : null}
          {hasGroups
            ? groups.map((group, i) => (
                <div className="lodging-group" key={`${group.kind}-${i}`}>
                  {formatGroupLine(group)}
                </div>
              ))
            : null}
          {holiday.map((n, i) => (
            <div className="lodging-holiday" key={`holiday-${i}`}>
              <Icon name="warn" />
              <span>{n.text}</span>
            </div>
          ))}
          {showPriceNote ? (
            <small className="lodging-price-note">
              参考价，实际以预订时为准
            </small>
          ) : null}
        </div>
      )}
    </>
  );
}


