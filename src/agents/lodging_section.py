"""Code-written lodging paragraph and the price lookup that feeds it.

The lookup starts after the lodging anchor is final and is awaited only when
the writer prefix is assembled. A timeout or error leaves the paragraph on
the no-price wording and lets the trip finish.

Homestay bands use the listed raw nightly prices only (at most two offers
per tier). The band is always a linear P25–P75, including samples of two or
three, then the mid markup, rounded to 10 yuan.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

from src.agents.chain_brands import match_chain_brand
from src.agents.holiday_calendar import PublicHoliday, overlapping_holiday_name
from src.agents.hotel_prices import HotelTierResult, fetch_tier_hotels
from src.agents.schema import (
    AccommodationSuggestion,
    LodgingDisplay,
    LodgingDisplayGroup,
    LodgingDisplayNotice,
    RoutePlan,
    TripRequest,
)

logger = logging.getLogger(__name__)

EN_DASH = "\u2013"
IDEOGRAPHIC_SPACE = "\u3000"
TIER_ORDER = ("economy", "mid", "high")
TIER_LABELS = {"economy": "经济", "mid": "中档", "high": "高档"}
HOMESTAY_LABEL = "民宿客栈"
HOMESTAY_CAPTION = "这一带以民宿客栈为主"
HOMESTAY_CHAIN_LIMIT = 5
LODGING_WRITER_RULE = (
    "- 正文提到住处时只说“住处”或住处名，不要点名任何酒店，也不要写房价\n"
)

_enabled: ContextVar[bool] = ContextVar("lodging_section_v2_enabled", default=False)
_handle: ContextVar[LodgingPriceTaskHandle | None] = ContextVar(
    "lodging_price_handle",
    default=None,
)
_markup: ContextVar[float] = ContextVar("lodging_mid_markup", default=1.05)


@dataclass
class LodgingPriceTaskHandle:
    """One lodging price lookup started before the writer.

    ``consumed`` stays set after a timeout so a late success is not picked up
    by the paragraph or the budget.
    """

    task: asyncio.Task | None
    t0: float
    deadline: float
    timeout_seconds: float
    consumed: bool = False
    result: HotelTierResult | None = None
    monotonic: Callable[[], float] = time.monotonic
    city: str = ""
    area_name: str = ""
    outcome: str = ""


def lodging_section_v2_enabled() -> bool:
    return bool(_enabled.get())


def lodging_mid_markup() -> float:
    return float(_markup.get())


def lodging_prices_for_assembly() -> HotelTierResult | None:
    handle = _handle.get()
    if handle is None or not handle.consumed:
        return None
    return handle.result


@contextmanager
def lodging_writer_context(
    *,
    enabled: bool,
    handle: LodgingPriceTaskHandle | None = None,
    mid_markup: float = 1.05,
):
    enabled_token = _enabled.set(bool(enabled))
    handle_token = _handle.set(handle)
    markup_token = _markup.set(float(mid_markup))
    try:
        yield
    finally:
        _enabled.reset(enabled_token)
        _handle.reset(handle_token)
        _markup.reset(markup_token)


def writer_system_prompt(base: str) -> str:
    """Return ``base`` unchanged unless the lodging paragraph is enabled."""

    if not lodging_section_v2_enabled():
        return base
    needle = "规则：\n"
    if needle in base and LODGING_WRITER_RULE not in base:
        return base.replace(needle, needle + LODGING_WRITER_RULE, 1)
    return base


def resolve_checkin_date(
    trip_request: TripRequest | None,
    settings: Any,
    *,
    today: date | None = None,
) -> date:
    parsed = _parse_date(getattr(trip_request, "start_date", None))
    if parsed is not None:
        return parsed
    base = today or date.today()
    try:
        offset = int(getattr(settings, "lodging_checkin_offset_days", 14))
    except (TypeError, ValueError):
        offset = 14
    if offset < 0:
        offset = 0
    return base + timedelta(days=offset)


def lodging_query_nights(days: int | None) -> int:
    try:
        count = int(days or 0)
    except (TypeError, ValueError):
        count = 0
    return max(count - 1, 1)


def start_lodging_price_task(
    factory: Callable[[], Any],
    *,
    timeout_seconds: float,
    monotonic: Callable[[], float] | None = None,
    city: str = "",
    area_name: str = "",
) -> LodgingPriceTaskHandle:
    """Record the deadline, then start one price task."""

    clock = monotonic or time.monotonic
    timeout = max(float(timeout_seconds), 0.0)
    started = clock()
    task = asyncio.create_task(factory(), name="lodging_price")
    return LodgingPriceTaskHandle(
        task=task,
        t0=started,
        deadline=started + timeout,
        timeout_seconds=timeout,
        monotonic=clock,
        city=str(city or ""),
        area_name=str(area_name or ""),
    )


def start_lodging_price_for_trip(
    *,
    trip_request: TripRequest,
    accommodation: AccommodationSuggestion | None,
    settings: Any,
    fetch: Callable[..., Any] | None = None,
    monotonic: Callable[[], float] | None = None,
    today: date | None = None,
) -> LodgingPriceTaskHandle | None:
    """Start the lookup after the lodging anchor is final.

    A user-stated lodging and a disabled switch do not start a task.
    """

    if not bool(getattr(settings, "lodging_section_v2_enabled", False)):
        return None
    if accommodation is None or accommodation.source == "user_specified":
        return None
    query = fetch or fetch_tier_hotels
    checkin = resolve_checkin_date(trip_request, settings, today=today)
    nights = lodging_query_nights(getattr(trip_request, "days", None))
    try:
        timeout_seconds = float(getattr(settings, "lodging_price_wait_seconds", 5.0))
    except (TypeError, ValueError):
        timeout_seconds = 5.0

    async def _run() -> HotelTierResult:
        return await query(
            city=getattr(trip_request, "to_city", "") or "",
            area_name=accommodation.name,
            latitude=accommodation.latitude,
            longitude=accommodation.longitude,
            checkin_date=checkin,
            nights=nights,
            settings=settings,
        )

    return start_lodging_price_task(
        _run,
        timeout_seconds=timeout_seconds,
        monotonic=monotonic,
        city=getattr(trip_request, "to_city", "") or "",
        area_name=accommodation.name,
    )


async def await_lodging_price_at_writer_barrier(
    handle: LodgingPriceTaskHandle | None = None,
) -> HotelTierResult | None:
    """Wait only the time still left, then cache the outcome."""

    if handle is None:
        handle = _handle.get()
    if handle is None:
        return None
    if handle.consumed:
        return handle.result
    result = await _wait_for_lodging_price(handle)
    handle.consumed = True
    handle.result = result
    _log_lodging_price_consume(handle)
    return result


async def settle_lodging_price_task(handle: LodgingPriceTaskHandle | None) -> None:
    """Cancel a pending lookup and swallow its cancellation."""

    if handle is None or handle.task is None:
        return
    task = handle.task
    if not task.done():
        task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        return
    except Exception:
        return


def build_lodging_display(
    *,
    accommodation: AccommodationSuggestion,
    route_plan: RoutePlan | None = None,
    trip_request: TripRequest | None = None,
    prices: HotelTierResult | None = None,
    nearby_hotel_names: list[str] | None = None,
    holidays: list[PublicHoliday] | tuple[PublicHoliday, ...] | None = None,
    mid_markup: float = 1.05,
) -> LodgingDisplay:
    """Return the structured lodging paragraph used by copy and the result."""

    if accommodation.source == "user_specified":
        span = _round_trip_span(route_plan)
        return LodgingDisplay(
            layout="user_lodging",
            area_label=accommodation.name,
            round_trip_minutes_low=None if span is None else span[0],
            round_trip_minutes_high=None if span is None else span[1],
            price_status="待核实",
        )
    unmatched = _unmatched_lodging_name(accommodation, trip_request)
    area_label = _area_label(accommodation.name)
    notices: list[LodgingDisplayNotice] = []
    if unmatched:
        notices.append(
            LodgingDisplayNotice(
                kind="user_lodging_unmatched",
                text=f"没找到你填的住处「{unmatched}」，按行程推荐住{area_label}",
            )
        )
    caption = ""
    groups: list[LodgingDisplayGroup] = []
    if _prices_found(prices):
        if _homestay_area(nearby_hotel_names) and _priced_homestays(prices):
            layout = "homestay"
            groups, caption = _homestay_group(prices, mid_markup)
            if not groups:
                layout = "area_only"
                caption = ""
        else:
            groups = _priced_tier_groups(prices)
            layout = "tiers" if groups else "area_only"
    else:
        groups = _brand_tier_groups(nearby_hotel_names)
        layout = "brands" if groups else "area_only"
    holiday = _holiday_line(trip_request, holidays)
    if holiday:
        notices.append(LodgingDisplayNotice(kind="holiday", text=holiday))
    return LodgingDisplay(
        layout=layout,
        area_label=area_label,
        caption=caption,
        groups=groups,
        notices=notices,
        price_status=_sourced_price_status(groups),
    )


def render_lodging_display_text(
    accommodation: AccommodationSuggestion,
    display: LodgingDisplay,
) -> str:
    """Render the lodging paragraph from display plus ``accommodation.reason``."""

    if display.layout == "user_lodging":
        phrase = _round_trip_phrase_from_minutes(
            display.round_trip_minutes_low,
            display.round_trip_minutes_high,
        )
        if phrase:
            return f"【住宿】从你住的{display.area_label}出发，{phrase}。"
        return f"【住宿】从你住的{display.area_label}出发。"

    unmatched = next(
        (notice.text for notice in display.notices if notice.kind == "user_lodging_unmatched"),
        "",
    )
    if unmatched:
        head = f"【住宿】{unmatched}"
    else:
        head = f"【住宿】建议住{display.area_label}"
    reason = (accommodation.reason or "").strip()
    if reason.endswith("。"):
        reason = reason[:-1]
    first = f"{head}：{reason}。" if reason else f"{head}。"
    lines = [first]
    if display.layout == "homestay":
        lines.extend(_homestay_text_lines(display))
    elif display.layout in ("tiers", "brands"):
        lines.extend(_tier_text_lines(display.groups))
    for notice in display.notices:
        if notice.kind == "holiday" and notice.text:
            lines.append(notice.text)
    if display.price_status != "sourced" and not any("待核实" in line for line in lines):
        lines.append("房价：待核实")
    return "\n".join(lines)


def render_lodging_section(
    *,
    accommodation: AccommodationSuggestion,
    route_plan: RoutePlan | None = None,
    trip_request: TripRequest | None = None,
    prices: HotelTierResult | None = None,
    nearby_hotel_names: list[str] | None = None,
    holidays: list[PublicHoliday] | tuple[PublicHoliday, ...] | None = None,
    mid_markup: float = 1.05,
) -> str:
    """Return the lodging paragraph without the trailing blank line."""

    display = build_lodging_display(
        accommodation=accommodation,
        route_plan=route_plan,
        trip_request=trip_request,
        prices=prices,
        nearby_hotel_names=nearby_hotel_names,
        holidays=holidays,
        mid_markup=mid_markup,
    )
    return render_lodging_display_text(accommodation, display)


def _round_trip_span(route_plan: RoutePlan | None) -> tuple[int, int] | None:
    if route_plan is None:
        return None
    spans: list[int] = []
    for group in route_plan.day_groups:
        if not group.access_legs:
            continue
        spans.append(sum(int(leg.duration_minutes) for leg in group.access_legs))
    if not spans:
        return None
    return min(spans), max(spans)


def _round_trip_phrase_from_minutes(low: int | None, high: int | None) -> str:
    if low is None or high is None:
        return ""
    if low == high:
        return f"每天来回约{low}分钟"
    return f"每天来回约{low}{EN_DASH}{high}分钟"


def _round_trip_phrase(route_plan: RoutePlan | None) -> str:
    span = _round_trip_span(route_plan)
    if span is None:
        return ""
    return _round_trip_phrase_from_minutes(span[0], span[1])


def _unmatched_lodging_name(
    accommodation: AccommodationSuggestion,
    trip_request: TripRequest | None,
) -> str:
    explicit = (accommodation.user_input_unmatched or "").strip()
    if explicit:
        return explicit
    request = trip_request.accommodation if trip_request is not None else None
    if request is None:
        return ""
    if request.place_id is not None or (request.name or "").strip():
        return (request.name or "").strip() or "未命名"
    return ""


_AREA_NAME_SUFFIXES = ("片区", "地区", "商圈", "沿线")


def _area_label(name: str) -> str:
    cleaned = (name or "").strip()
    if cleaned.endswith("一带"):
        return cleaned
    # "解放碑片区一带" / "观前街地区一带" read badly; the suffix adds nothing.
    for suffix in _AREA_NAME_SUFFIXES:
        if cleaned.endswith(suffix) and len(cleaned) > len(suffix) + 1:
            cleaned = cleaned[: -len(suffix)]
            break
    return f"{cleaned}一带"


def _sourced_price_status(groups: list[LodgingDisplayGroup]) -> str:
    """A nightly band is shown only when both ends came from a sourced lookup."""
    for group in groups:
        if group.nightly_low_cny is not None and group.nightly_high_cny is not None:
            return "sourced"
    return "待核实"


def _prices_found(prices: HotelTierResult | None) -> bool:
    if prices is None:
        return False
    return any(_tier_hotels(prices, tier) for tier in TIER_ORDER)


_HOMESTAY_NAME_WORDS = ("民宿", "客栈", "小院", "院子", "山居", "青旅", "青年旅舍", "旅舍", "精舍")


def _priced_homestays(prices: HotelTierResult) -> bool:
    """Two or more of the priced hotels, or all of them, are named like homestays.

    The homestay wording lists these hotels; it would contradict itself above
    names such as 全季 or voco (seen in Suzhou and Xi'an), so it needs them.
    """

    hotels = [
        hotel
        for tier in (prices.economy, prices.mid, prices.high)
        for hotel in (tier.hotels or ())
    ]
    named = sum(1 for hotel in hotels if any(word in hotel.name for word in _HOMESTAY_NAME_WORDS))
    return named >= 2 or (bool(hotels) and named == len(hotels))


def _homestay_area(nearby_hotel_names: list[str] | None) -> bool:
    """Fewer than five chain names among the searched nearby hotels.

    ``None`` means the nearby search did not run, which stays on the chain
    wording. An empty list means the search ran and found nobody.
    """

    if nearby_hotel_names is None:
        return False
    matched = sum(1 for name in nearby_hotel_names if match_chain_brand(name))
    return matched < HOMESTAY_CHAIN_LIMIT


def _priced_tier_groups(prices: HotelTierResult) -> list[LodgingDisplayGroup]:
    groups: list[LodgingDisplayGroup] = []
    for tier in TIER_ORDER:
        hotels = _tier_hotels(prices, tier)
        names = [hotel.name for hotel in hotels if (hotel.name or "").strip()]
        if not names:
            continue
        low, high = _tier_bounds(prices, tier)
        if len(names) >= 2 and low is not None and high is not None:
            price_low, price_high = low, high
        else:
            price_low, price_high = None, None
        groups.append(
            LodgingDisplayGroup(
                kind=tier,
                label=TIER_LABELS[tier],
                hotels=names,
                nightly_low_cny=price_low,
                nightly_high_cny=price_high,
            )
        )
    return groups


def _homestay_group(
    prices: HotelTierResult,
    mid_markup: float,
) -> tuple[list[LodgingDisplayGroup], str]:
    offers = [
        hotel
        for tier in TIER_ORDER
        for hotel in _tier_hotels(prices, tier)
        if (hotel.name or "").strip()
    ]
    offers.sort(key=lambda hotel: (int(hotel.distance_meters), hotel.name))
    shown = offers[:3]
    names = [hotel.name for hotel in shown]
    bounds = _homestay_bounds(_listed_raw_prices(prices), mid_markup)
    if not names and bounds is None:
        return [], ""
    low, high = (bounds if bounds is not None else (None, None))
    return (
        [
            LodgingDisplayGroup(
                kind="homestay",
                label=HOMESTAY_LABEL,
                hotels=names,
                nightly_low_cny=low,
                nightly_high_cny=high,
            )
        ],
        HOMESTAY_CAPTION if names else "",
    )


def _brand_tier_groups(nearby_hotel_names: list[str] | None) -> list[LodgingDisplayGroup]:
    if not nearby_hotel_names:
        return []
    grouped: dict[str, list[str]] = {tier: [] for tier in TIER_ORDER}
    for name in nearby_hotel_names:
        matched = match_chain_brand(name)
        if matched is None:
            continue
        tier, _brand = matched
        if len(grouped[tier]) >= 2:
            continue
        grouped[tier].append(name)
    return [
        LodgingDisplayGroup(
            kind=tier,
            label=TIER_LABELS[tier],
            hotels=grouped[tier],
        )
        for tier in TIER_ORDER
        if grouped[tier]
    ]


def _tier_text_lines(groups: list[LodgingDisplayGroup]) -> list[str]:
    name_parts: list[str] = []
    price_parts: list[str] = []
    for group in groups:
        names = [name for name in group.hotels if (name or "").strip()]
        if not names:
            continue
        name_parts.append(f"{group.label}：{'、'.join(names)}")
        if group.nightly_low_cny is not None and group.nightly_high_cny is not None:
            price_parts.append(
                f"{group.label}{_yuan_phrase(group.nightly_low_cny, group.nightly_high_cny)}"
            )
    lines: list[str] = []
    if name_parts:
        lines.append(IDEOGRAPHIC_SPACE.join(name_parts))
    if price_parts:
        lines.append("参考价（每晚）：" + "，".join(price_parts))
    return lines


def _homestay_text_lines(display: LodgingDisplay) -> list[str]:
    names: list[str] = []
    low = high = None
    if display.groups:
        group = display.groups[0]
        names = [name for name in group.hotels if (name or "").strip()]
        low = group.nightly_low_cny
        high = group.nightly_high_cny
    lines: list[str] = []
    caption = (display.caption or "").strip()
    if caption and names:
        lines.append(f"{caption}：{'、'.join(names)}")
    elif names:
        lines.append("、".join(names))
    if low is not None and high is not None:
        lines.append("参考价（每晚）：" + _yuan_phrase(low, high))
    return lines


def _holiday_line(
    trip_request: TripRequest | None,
    holidays: list[PublicHoliday] | tuple[PublicHoliday, ...] | None,
) -> str:
    span = _trip_span(trip_request)
    if span is None:
        return ""
    name = overlapping_holiday_name(span[0], span[1], holidays=holidays)
    if not name:
        return ""
    return (
        f"你的行程赶上{name}假期，热门片区假期房价常比平时高，"
        "建议尽早订房、预算多留一些。"
    )


def _trip_span(trip_request: TripRequest | None) -> tuple[date, date] | None:
    if trip_request is None:
        return None
    start = _parse_date(trip_request.start_date)
    end = _parse_date(trip_request.end_date)
    try:
        days = max(int(trip_request.days or 1), 1)
    except (TypeError, ValueError):
        days = 1
    if start is not None and end is not None:
        if end < start:
            return None
        return start, end
    if start is not None:
        return start, start + timedelta(days=days - 1)
    if end is not None:
        return end - timedelta(days=days - 1), end
    return None


def _parse_date(value: object) -> date | None:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _tier_hotels(prices: HotelTierResult, tier: str) -> tuple:
    bucket = getattr(prices, tier, None)
    hotels = getattr(bucket, "hotels", ()) if bucket is not None else ()
    return tuple(hotels or ())


def _tier_bounds(prices: HotelTierResult, tier: str) -> tuple[int | None, int | None]:
    bucket = getattr(prices, tier, None)
    if bucket is None:
        return None, None
    low = getattr(bucket, "nightly_low", None)
    high = getattr(bucket, "nightly_high", None)
    if low is None or high is None:
        return None, None
    return int(low), int(high)


def _listed_raw_prices(prices: HotelTierResult) -> list[float]:
    values: list[float] = []
    for tier in TIER_ORDER:
        for hotel in _tier_hotels(prices, tier):
            try:
                amount = float(hotel.nightly_price)
            except (TypeError, ValueError):
                continue
            if math.isfinite(amount) and amount > 0:
                values.append(amount)
    return values


def _homestay_bounds(prices: list[float], markup: float) -> tuple[int, int] | None:
    # One listed offer would publish that hotel's own price.
    if len(prices) < 2:
        return None
    ordered = sorted(float(price) for price in prices)
    low = _percentile(ordered, 0.25) * float(markup)
    high = _percentile(ordered, 0.75) * float(markup)
    rounded_low = _round_to_ten(low)
    rounded_high = _round_to_ten(high)
    if rounded_low > rounded_high:
        rounded_low, rounded_high = rounded_high, rounded_low
    return rounded_low, rounded_high


def _percentile(sorted_prices: list[float], fraction: float) -> float:
    if len(sorted_prices) == 1:
        return float(sorted_prices[0])
    position = (len(sorted_prices) - 1) * fraction
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return float(sorted_prices[low])
    weight = position - low
    return float(sorted_prices[low]) * (1.0 - weight) + float(sorted_prices[high]) * weight


def _round_to_ten(amount: float) -> int:
    return int(math.floor(float(amount) / 10.0 + 0.5)) * 10


def _yuan_phrase(low: int, high: int) -> str:
    if low == high:
        return f"约 {low} 元"
    return f"约 {low}{EN_DASH}{high} 元"


def _one_line(value: object) -> str:
    return str(value or "").replace("\r", " ").replace("\n", " ")


def _tier_hotel_count(tier: Any) -> int:
    hotels = getattr(tier, "hotels", ()) if tier is not None else ()
    return len(tuple(hotels or ()))


def _classify_price_result(result: HotelTierResult | None) -> str:
    if result is None:
        return "error"
    if _prices_found(result):
        return "ok"
    return "empty"


def _log_lodging_price_consume(handle: LodgingPriceTaskHandle) -> None:
    result = handle.result
    status = handle.outcome or _classify_price_result(result)
    wait_ms = int(max(handle.monotonic() - handle.t0, 0.0) * 1000)
    economy = getattr(result, "economy", None) if result is not None else None
    mid = getattr(result, "mid", None) if result is not None else None
    high = getattr(result, "high", None) if result is not None else None
    failure_reason = getattr(result, "failure_reason", "") if result is not None else ""
    # Production configures no handler for app loggers, so only WARNING and
    # above reach the container log; a lookup that found nothing must show.
    log = logger.info if status == "ok" else logger.warning
    log(
        "lodging price lookup status=%s city=%s area=%s wait_ms=%s "
        "economy_hotels=%s economy_failure=%s mid_hotels=%s mid_failure=%s "
        "high_hotels=%s high_failure=%s failure_reason=%s",
        status,
        _one_line(handle.city),
        _one_line(handle.area_name),
        wait_ms,
        _tier_hotel_count(economy),
        _one_line(getattr(economy, "failure", "")),
        _tier_hotel_count(mid),
        _one_line(getattr(mid, "failure", "")),
        _tier_hotel_count(high),
        _one_line(getattr(high, "failure", "")),
        _one_line(failure_reason),
    )


async def _wait_for_lodging_price(
    handle: LodgingPriceTaskHandle,
) -> HotelTierResult | None:
    task = handle.task
    if task is None:
        handle.outcome = "error"
        return None
    if not task.done():
        remaining = max(handle.deadline - handle.monotonic(), 0.0)
        if remaining <= 0:
            await settle_lodging_price_task(handle)
            handle.outcome = "timeout"
            return None
        try:
            await asyncio.wait_for(task, timeout=remaining)
        except (TimeoutError, asyncio.TimeoutError):
            await settle_lodging_price_task(handle)
            handle.outcome = "timeout"
            return None
        except asyncio.CancelledError:
            await settle_lodging_price_task(handle)
            handle.outcome = "cancelled"
            return None
        except Exception:
            logger.warning("lodging price lookup failed; continuing without prices")
            await settle_lodging_price_task(handle)
            handle.outcome = "error"
            return None
    result = await _task_result(task)
    if isinstance(result, HotelTierResult):
        handle.outcome = _classify_price_result(result)
        return result
    handle.outcome = "cancelled" if task.cancelled() else "error"
    return None


async def _task_result(task: asyncio.Task) -> HotelTierResult | None:
    try:
        result = await task
    except asyncio.CancelledError:
        return None
    except Exception:
        logger.warning("lodging price lookup failed; continuing without prices")
        return None
    if isinstance(result, HotelTierResult):
        return result
    return None


def parse_price_observed_at(value: str, fallback: datetime) -> datetime:
    text = (value or "").strip()
    if text:
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            parsed = None
        if parsed is not None:
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=timezone.utc)
            return parsed
    if fallback.tzinfo is None:
        return fallback.replace(tzinfo=timezone.utc)
    return fallback


