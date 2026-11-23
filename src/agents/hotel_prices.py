"""RollingGo hotel prices for one lodging pin.

Looks up economy, mid, and high hotels, drops prices that do not belong in
the tier, and caches each tier for a day.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from src.agents.accommodation_policy import distance_km
from src.agents.route_feasibility import valid_coordinate

logger = logging.getLogger(__name__)

SOURCE = "rollinggo"
CACHE_PREFIX = "yuntu-travel:hotel-price:v1"
PLACE_TYPE = "景点"
SEARCH_SIZE = 20
ADULT_COUNT = 2
TIER_MIN_COUNT = 2
TIER_LIST_LIMIT = 2
TIER_ECONOMY = "economy"
TIER_MID = "mid"
TIER_HIGH = "high"
TIERS = (TIER_ECONOMY, TIER_MID, TIER_HIGH)
TIER_LABELS = {
    TIER_ECONOMY: "经济",
    TIER_MID: "中档",
    TIER_HIGH: "高档",
}
_AREA_SUFFIXES = ("一带", "商圈", "片区")
_NIGHTLY_RE = re.compile(
    r"约\s*(\d+(?:\.\d+)?)\s*(?:CNY|元|人民币)?\s*/\s*晚",
    re.IGNORECASE,
)

DISABLED_REASON = "未启用"
AUTH_REASON = "RollingGo 令牌失效或缺失，需要重新登录"
TIMEOUT_REASON = "RollingGo 查价超时"
ERROR_REASON = "RollingGo 查价失败"
TIER_TIMEOUT = "超时"
TIER_ERROR = "出错"
TIER_AUTH = "令牌失效或缺失"
INVALID_COORD_REASON = "住处坐标无效"
INVALID_DATE_REASON = "入住日期无效"
INVALID_NIGHTS_REASON = "入住晚数无效"


@dataclass(frozen=True)
class HotelOffer:
    hotel_id: str
    name: str
    star_rating: float
    brand: str
    nightly_price: float
    latitude: float
    longitude: float
    distance_meters: int


@dataclass(frozen=True)
class HotelTier:
    tier: str
    hotels: tuple[HotelOffer, ...]
    nightly_low: int | None
    nightly_high: int | None
    raw_price_count: int
    widened: bool
    failure: str = ""
    queried_at: str = ""
    priced_offers: tuple[HotelOffer, ...] = ()


@dataclass(frozen=True)
class HotelTierResult:
    economy: HotelTier
    mid: HotelTier
    high: HotelTier
    queried_on: date
    checkin_date: date
    nights: int
    source: str
    failure_reason: str


@dataclass(frozen=True)
class _QueryOutcome:
    hotels: tuple[HotelOffer, ...] = ()
    failure: str = ""


class _NullPriceCache:
    async def get(self, key: str) -> None:
        return None

    async def put(self, key: str, payload: dict, ttl: int) -> None:
        return None

    async def aclose(self) -> None:
        return None


class RedisHotelPriceCache:
    """Same Redis client shape as the route-cache client. Failures stay local."""

    def __init__(self, url: str, timeout: float):
        self._url = url
        self._timeout = timeout
        self._client = None
        self._failed = False

    async def _connect(self):
        if self._failed or not self._url:
            return None
        if self._client is not None:
            return self._client
        try:
            from redis.asyncio import Redis

            self._client = Redis.from_url(
                self._url,
                encoding="utf-8",
                decode_responses=True,
                socket_connect_timeout=self._timeout,
                socket_timeout=self._timeout,
                retry_on_timeout=False,
                health_check_interval=30,
            )
        except Exception:
            logger.info("RollingGo price cache client init failed; querying without cache")
            self._failed = True
            self._client = None
            return None
        return self._client

    async def get(self, key: str) -> dict | None:
        try:
            client = await self._connect()
            if client is None:
                return None
            raw = await client.get(key)
        except Exception:
            logger.info("RollingGo price cache read failed; querying without cache")
            return None
        if not raw:
            return None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None

    async def put(self, key: str, payload: dict, ttl: int) -> None:
        try:
            client = await self._connect()
            if client is None:
                return
            await client.set(key, json.dumps(payload, ensure_ascii=False), ex=int(ttl))
        except Exception:
            logger.info("RollingGo price cache write failed; continuing without cache")
            return None

    async def aclose(self) -> None:
        client = self._client
        self._client = None
        if client is None:
            return
        closer = getattr(client, "aclose", None) or getattr(client, "close", None)
        if closer is None:
            return
        try:
            result = closer()
            if hasattr(result, "__await__"):
                await result
        except Exception:
            return None


def _enabled(settings) -> bool:
    return bool(settings is not None and getattr(settings, "rollinggo_enabled", False))


def _empty_tier(tier: str, failure: str = "") -> HotelTier:
    return HotelTier(
        tier=tier,
        hotels=(),
        nightly_low=None,
        nightly_high=None,
        raw_price_count=0,
        widened=False,
        failure=failure,
        queried_at="",
    )


def _result(
    *,
    checkin: date,
    nights: int,
    reason: str,
    economy: HotelTier | None = None,
    mid: HotelTier | None = None,
    high: HotelTier | None = None,
    queried_on: date | None = None,
) -> HotelTierResult:
    return HotelTierResult(
        economy=economy or _empty_tier(TIER_ECONOMY),
        mid=mid or _empty_tier(TIER_MID),
        high=high or _empty_tier(TIER_HIGH),
        queried_on=queried_on or date.today(),
        checkin_date=checkin,
        nights=nights,
        source=SOURCE,
        failure_reason=reason,
    )


def _parse_checkin(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
        except ValueError:
            return None
    return None


def _parse_nights(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < 1:
        return None
    return value


def _parse_origin(latitude, longitude) -> tuple[float, float] | None:
    if isinstance(latitude, bool) or isinstance(longitude, bool):
        return None
    try:
        lat = float(latitude)
        lon = float(longitude)
    except (TypeError, ValueError):
        return None
    if not valid_coordinate(lat, lon):
        return None
    return lat, lon


def _strip_area(area_name: str) -> str:
    name = (area_name or "").strip()
    changed = True
    while changed and name:
        changed = False
        for suffix in _AREA_SUFFIXES:
            if name.endswith(suffix) and len(name) > len(suffix):
                name = name[: -len(suffix)].strip()
                changed = True
                break
    return name


def _place_text(city: str, area_name: str) -> tuple[str, str]:
    """Return the API place and the short name used in originQuery."""
    city_name = (city or "").strip()
    area = _strip_area(area_name)
    if not area:
        return city_name, city_name
    if city_name and area.startswith(city_name):
        return area, area
    place = f"{city_name}{area}" if city_name else area
    return place, area


def _cache_part(value: str) -> str:
    text = (value or "").strip().replace(":", "_")
    return text or "-"


def _area_key_label(area_name: str, latitude: float, longitude: float) -> str:
    area = _strip_area(area_name)
    if area:
        return area
    from src.agents.route_planner.lodging import _snap_to_grid

    snapped_lat, snapped_lon = _snap_to_grid(latitude, longitude)
    return f"{snapped_lat:.6f},{snapped_lon:.6f}"


def _cache_key(
    *,
    city: str,
    area_name: str,
    latitude: float,
    longitude: float,
    checkin: date,
    nights: int,
    tier: str,
    cache_at_origin: bool = False,
) -> str:
    label = _cache_part(_area_key_label(area_name, latitude, longitude))
    if area_name and cache_at_origin:
        label = f"{label}@{_area_key_label('', latitude, longitude)}"
    return (
        f"{CACHE_PREFIX}:{_cache_part(city)}:{label}:"
        f"{checkin.isoformat()}:{int(nights)}:{tier}"
    )


def _star_bounds(settings, tier: str) -> tuple[float, float]:
    if tier == TIER_ECONOMY:
        return float(settings.rollinggo_economy_star_min), float(settings.rollinggo_economy_star_max)
    if tier == TIER_MID:
        return float(settings.rollinggo_mid_star_min), float(settings.rollinggo_mid_star_max)
    return float(settings.rollinggo_high_star_min), float(settings.rollinggo_high_star_max)


def _markup(settings, tier: str) -> float:
    if tier == TIER_ECONOMY:
        return float(settings.rollinggo_economy_markup)
    if tier == TIER_MID:
        return float(settings.rollinggo_mid_markup)
    return float(settings.rollinggo_high_markup)


def _json_number(value: float) -> int | float:
    if float(value).is_integer():
        return int(value)
    return float(value)


def _star_ratings(low: float, high: float) -> list[int | float]:
    return [_json_number(low), _json_number(high)]


def _now_stamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _nightly_yuan(price: Any, nights: int) -> float | None:
    """Per-night yuan. The message's 约X/晚 wins; otherwise the total is split."""
    if not isinstance(price, dict) or price.get("hasPrice") is not True:
        return None
    message = str(price.get("message") or "")
    match = _NIGHTLY_RE.search(message)
    if match:
        try:
            nightly = float(match.group(1))
        except ValueError:
            nightly = 0.0
        if nightly > 0:
            return nightly
    raw = price.get("lowestPrice")
    try:
        total = float(raw)
    except (TypeError, ValueError):
        return None
    if total <= 0 or nights < 1:
        return None
    return total / float(nights)


def _meters(origin: tuple[float, float], latitude: float, longitude: float) -> float:
    return distance_km(origin, (latitude, longitude)) * 1000.0


def _offer_from_row(
    row: Any,
    *,
    origin: tuple[float, float],
    nights: int,
    star_min: float,
    star_max: float,
    radius_meters: int,
) -> HotelOffer | None:
    if not isinstance(row, dict):
        return None
    hotel_id = row.get("hotelId")
    name = str(row.get("name") or "").strip()
    if hotel_id is None or not str(hotel_id).strip() or not name:
        return None
    try:
        star = float(row.get("starRating"))
        latitude = float(row.get("latitude"))
        longitude = float(row.get("longitude"))
    except (TypeError, ValueError):
        return None
    if not (star_min - 1e-9 <= star <= star_max + 1e-9):
        return None
    if not valid_coordinate(latitude, longitude):
        return None
    meters = _meters(origin, latitude, longitude)
    if meters > float(radius_meters) + 1e-6:
        return None
    nightly = _nightly_yuan(row.get("price"), nights)
    if nightly is None:
        return None
    brand = row.get("brand")
    brand_text = "" if brand is None else str(brand).strip()
    return HotelOffer(
        hotel_id=str(hotel_id).strip(),
        name=name,
        star_rating=star,
        brand=brand_text,
        nightly_price=nightly,
        latitude=latitude,
        longitude=longitude,
        distance_meters=int(math.floor(meters + 0.5)),
    )


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[mid])
    return (float(ordered[mid - 1]) + float(ordered[mid])) / 2.0


def _keep_price(
    price: float,
    tier_median: float,
    *,
    low_ratio: float,
    high_ratio: float,
    ceiling: float | None,
) -> bool:
    # "高于 / 低于" are strict. A price sitting on the ratio stays.
    if price < tier_median * low_ratio or price > tier_median * high_ratio:
        return False
    if ceiling is not None and price > ceiling:
        return False
    return True


def _guard(
    candidates: dict[str, list[HotelOffer]],
    cached_prices: dict[str, list[float]],
    settings,
) -> dict[str, list[HotelOffer]]:
    """Drop within-tier outliers, then prices above the next tier's median.

    Medians are taken before either drop, so one expensive hotel does not
    become the bar it is judged against.
    """
    low_ratio = float(settings.rollinggo_price_low_ratio)
    high_ratio = float(settings.rollinggo_price_high_ratio)
    medians: dict[str, float | None] = {}
    for tier in TIERS:
        if tier in cached_prices:
            medians[tier] = _median(cached_prices[tier])
        else:
            medians[tier] = _median([hotel.nightly_price for hotel in candidates.get(tier, [])])
    ceilings = {
        TIER_ECONOMY: medians[TIER_MID],
        TIER_MID: medians[TIER_HIGH],
        TIER_HIGH: None,
    }
    kept: dict[str, list[HotelOffer]] = {}
    for tier in TIERS:
        median = medians[tier]
        if tier in cached_prices or median is None:
            kept[tier] = []
            continue
        kept[tier] = [
            hotel
            for hotel in candidates.get(tier, [])
            if _keep_price(
                hotel.nightly_price,
                median,
                low_ratio=low_ratio,
                high_ratio=high_ratio,
                ceiling=ceilings[tier],
            )
        ]
    return kept


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


def _reference_bounds(prices: list[float], markup: float) -> tuple[int | None, int | None]:
    if not prices:
        return None, None
    ordered = sorted(float(price) for price in prices)
    # Three or fewer use the ends. Four or more use a linear P25 and P75.
    if len(ordered) <= 3:
        low, high = ordered[0], ordered[-1]
    else:
        low, high = _percentile(ordered, 0.25), _percentile(ordered, 0.75)
    rounded_low = _round_to_ten(low * markup)
    rounded_high = _round_to_ten(high * markup)
    if rounded_low > rounded_high:
        rounded_low, rounded_high = rounded_high, rounded_low
    return rounded_low, rounded_high


def _listed(hotels: list[HotelOffer]) -> tuple[HotelOffer, ...]:
    ordered = sorted(hotels, key=lambda hotel: (hotel.distance_meters, hotel.hotel_id))
    return tuple(ordered[:TIER_LIST_LIMIT])


def _tier_from_hotels(
    tier: str,
    hotels: list[HotelOffer],
    *,
    raw_price_count: int,
    widened: bool,
    queried_at: str,
    markup: float,
    failure: str = "",
) -> HotelTier:
    low, high = _reference_bounds([hotel.nightly_price for hotel in hotels], markup)
    return HotelTier(
        tier=tier,
        hotels=_listed(hotels),
        nightly_low=low,
        nightly_high=high,
        raw_price_count=raw_price_count,
        widened=widened,
        failure=failure,
        queried_at=queried_at,
        priced_offers=tuple(hotels),
    )


def _hotel_payload(hotel: HotelOffer) -> dict[str, Any]:
    return {
        "hotel_id": hotel.hotel_id,
        "name": hotel.name,
        "star_rating": hotel.star_rating,
        "brand": hotel.brand,
        "nightly_price": hotel.nightly_price,
        "latitude": hotel.latitude,
        "longitude": hotel.longitude,
        "distance_meters": hotel.distance_meters,
    }


def _hotel_from_payload(row: Any) -> HotelOffer | None:
    if not isinstance(row, dict):
        return None
    try:
        return HotelOffer(
            hotel_id=str(row["hotel_id"]),
            name=str(row["name"]),
            star_rating=float(row["star_rating"]),
            brand=str(row.get("brand") or ""),
            nightly_price=float(row["nightly_price"]),
            latitude=float(row["latitude"]),
            longitude=float(row["longitude"]),
            distance_meters=int(row["distance_meters"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _overall_reason(tiers: list[HotelTier]) -> str:
    reasons = [tier.failure for tier in tiers]
    if not all(reasons):
        return ""
    if any(reason == TIER_AUTH for reason in reasons):
        return AUTH_REASON
    if all(reason == TIER_TIMEOUT for reason in reasons):
        return TIMEOUT_REASON
    return ERROR_REASON


def _load_token(settings) -> str | None:
    direct = str(getattr(settings, "rollinggo_access_token", "") or "").strip()
    if direct:
        return direct
    raw_path = str(getattr(settings, "rollinggo_token_file", "") or "").strip()
    if not raw_path:
        logger.warning(AUTH_REASON)
        return None
    path = Path(raw_path).expanduser()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("token file is not an object")
        token = str(payload.get("access_token") or "").strip()
        expires_in = int(payload.get("expires_in") or 0)
        mtime = path.stat().st_mtime
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        logger.warning(AUTH_REASON)
        return None
    if not token:
        logger.warning(AUTH_REASON)
        return None
    if expires_in > 0:
        warn_seconds = int(settings.rollinggo_token_warn_days) * 24 * 60 * 60
        remaining = (mtime + expires_in) - time.time()
        if remaining < warn_seconds:
            expires_on = datetime.fromtimestamp(mtime + expires_in).date()
            logger.warning(
                "RollingGo 令牌将于 %s 到期，剩余不足 %s 天，需要重新登录",
                expires_on.isoformat(),
                int(settings.rollinggo_token_warn_days),
            )
    return token


def _open_cache(settings):
    url = str(getattr(settings, "redis_url", "") or "").strip()
    if not url:
        return _NullPriceCache()
    from src.agents.route_planning import REDIS_ROUTE_CACHE_TIMEOUT_SECONDS

    return RedisHotelPriceCache(url, REDIS_ROUTE_CACHE_TIMEOUT_SECONDS)


async def _close_cache(cache) -> None:
    closer = getattr(cache, "aclose", None)
    if closer is None:
        return
    try:
        result = closer()
        if hasattr(result, "__await__"):
            await result
    except Exception:
        return None


def _request_body(
    *,
    city: str,
    area_name: str,
    checkin: date,
    nights: int,
    star_min: float,
    star_max: float,
    radius_meters: int,
) -> dict[str, Any]:
    place, short_name = _place_text(city, area_name)
    origin = f"{short_name}附近酒店" if short_name else "附近酒店"
    return {
        "originQuery": origin,
        "place": place,
        "placeType": PLACE_TYPE,
        "size": SEARCH_SIZE,
        "checkInParam": {
            "checkInDate": checkin.isoformat(),
            "stayNights": int(nights),
            "adultCount": ADULT_COUNT,
        },
        "filterOptions": {
            "starRatings": _star_ratings(star_min, star_max),
            "distanceInMeter": int(radius_meters),
        },
    }


def _endpoint(settings) -> str:
    base = str(getattr(settings, "rollinggo_base_url", "") or "").strip().rstrip("/")
    return f"{base}/hotelsearch"


async def _cache_get(cache, key: str) -> dict | None:
    if cache is None:
        return None
    try:
        payload = await cache.get(key)
    except Exception:
        logger.info("RollingGo price cache read failed; querying without cache")
        return None
    return payload if isinstance(payload, dict) else None


async def _cache_put(cache, key: str, payload: dict, ttl: int) -> None:
    if cache is None:
        return
    try:
        await cache.put(key, payload, int(ttl))
    except Exception:
        logger.info("RollingGo price cache write failed; continuing without cache")


def _log_tier_failure(tier: str, failure: str) -> None:
    label = TIER_LABELS[tier]
    if failure == TIER_TIMEOUT:
        logger.warning("RollingGo %s档查价超时", label)
        return
    if failure == TIER_AUTH:
        return
    logger.warning("RollingGo %s档查价失败", label)


async def _query_tier(
    client,
    *,
    settings,
    token: str,
    city: str,
    area_name: str,
    origin: tuple[float, float],
    checkin: date,
    nights: int,
    tier: str,
    radius_meters: int,
) -> _QueryOutcome:
    star_min, star_max = _star_bounds(settings, tier)
    body = _request_body(
        city=city,
        area_name=area_name,
        checkin=checkin,
        nights=nights,
        star_min=star_min,
        star_max=star_max,
        radius_meters=radius_meters,
    )
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    timeout = float(settings.rollinggo_timeout_seconds)
    try:
        response = await asyncio.wait_for(
            client.post(_endpoint(settings), json=body, headers=headers, timeout=timeout),
            timeout=timeout,
        )
    except (asyncio.TimeoutError, TimeoutError, httpx.TimeoutException):
        return _QueryOutcome(failure=TIER_TIMEOUT)
    except Exception:
        return _QueryOutcome(failure=TIER_ERROR)
    status = getattr(response, "status_code", 0)
    if status == 401:
        return _QueryOutcome(failure=TIER_AUTH)
    try:
        payload = response.json()
    except Exception:
        return _QueryOutcome(failure=TIER_ERROR)
    if status >= 400 or not isinstance(payload, dict) or payload.get("success") is not True:
        return _QueryOutcome(failure=TIER_ERROR)
    rows = payload.get("hotelInformationList")
    if not isinstance(rows, list):
        return _QueryOutcome(failure=TIER_ERROR)
    hotels: list[HotelOffer] = []
    seen: set[str] = set()
    for row in rows:
        offer = _offer_from_row(
            row,
            origin=origin,
            nights=nights,
            star_min=star_min,
            star_max=star_max,
            radius_meters=radius_meters,
        )
        if offer is None or offer.hotel_id in seen:
            continue
        seen.add(offer.hotel_id)
        hotels.append(offer)
    return _QueryOutcome(hotels=tuple(hotels))


async def _query_many(client, tiers: list[str], **kwargs) -> dict[str, _QueryOutcome]:
    outcomes = await asyncio.gather(*[_query_tier(client, tier=tier, **kwargs) for tier in tiers])
    return {tier: outcome for tier, outcome in zip(tiers, outcomes)}


def _merge(existing: list[HotelOffer], extra: tuple[HotelOffer, ...]) -> list[HotelOffer]:
    by_id = {hotel.hotel_id: hotel for hotel in existing}
    for hotel in extra:
        old = by_id.get(hotel.hotel_id)
        if old is None or hotel.distance_meters < old.distance_meters:
            by_id[hotel.hotel_id] = hotel
    return list(by_id.values())


def _cached_tier(tier: str, payload: dict, settings) -> HotelTier | None:
    rows = payload.get("hotels")
    if not isinstance(rows, list):
        return None
    hotels: list[HotelOffer] = []
    for row in rows:
        hotel = _hotel_from_payload(row)
        if hotel is None:
            return None
        hotels.append(hotel)
    try:
        raw_price_count = int(payload.get("raw_price_count"))
    except (TypeError, ValueError):
        return None
    return _tier_from_hotels(
        tier,
        hotels,
        raw_price_count=raw_price_count,
        widened=bool(payload.get("widened")),
        queried_at=str(payload.get("queried_at") or ""),
        markup=_markup(settings, tier),
    )


async def _collect(
    *,
    settings,
    client,
    cache,
    token: str,
    city: str,
    area_name: str,
    origin: tuple[float, float],
    checkin: date,
    nights: int,
    cache_at_origin: bool,
) -> HotelTierResult:
    latitude, longitude = origin
    keys = {
        tier: _cache_key(
            city=city,
            area_name=area_name,
            latitude=latitude,
            longitude=longitude,
            checkin=checkin,
            nights=nights,
            tier=tier,
            cache_at_origin=cache_at_origin,
        )
        for tier in TIERS
    }
    cached: dict[str, HotelTier] = {}
    cached_prices: dict[str, list[float]] = {}
    blocked: dict[str, str] = {}
    for tier in TIERS:
        payload = await _cache_get(cache, keys[tier])
        if payload is None:
            continue
        if payload.get("ok") is False:
            reason = str(payload.get("reason") or TIER_ERROR)
            if reason not in {TIER_TIMEOUT, TIER_ERROR, TIER_AUTH}:
                reason = TIER_ERROR
            blocked[tier] = reason
            continue
        if payload.get("ok") is not True:
            continue
        tier_result = _cached_tier(tier, payload, settings)
        if tier_result is None:
            continue
        cached[tier] = tier_result
        cached_prices[tier] = [hotel.nightly_price for hotel in _cached_offers(payload)]

    to_query = [tier for tier in TIERS if tier not in cached and tier not in blocked]
    candidates: dict[str, list[HotelOffer]] = {tier: [] for tier in TIERS}
    failed = dict(blocked)
    queried_at = _now_stamp()
    auth_logged = False
    base_radius = int(settings.rollinggo_radius_meters)
    wide_radius = int(settings.rollinggo_widen_radius_meters)

    local_client = client
    opened_client = False

    async def ensure_client():
        nonlocal local_client, opened_client
        if local_client is None:
            local_client = httpx.AsyncClient(trust_env=False)
            opened_client = True
        return local_client

    async def run_wave(tiers: list[str], radius: int, *, widening: bool) -> dict[str, _QueryOutcome]:
        nonlocal auth_logged
        if not tiers:
            return {}
        outcomes = await _query_many(
            await ensure_client(),
            tiers,
            settings=settings,
            token=token,
            city=city,
            area_name=area_name,
            origin=origin,
            checkin=checkin,
            nights=nights,
            radius_meters=radius,
        )
        if not auth_logged and any(item.failure == TIER_AUTH for item in outcomes.values()):
            logger.warning(AUTH_REASON)
            auth_logged = True
        for tier, outcome in outcomes.items():
            if not outcome.failure or outcome.failure == TIER_AUTH:
                continue
            if widening:
                logger.warning("RollingGo %s档放宽半径未成功", TIER_LABELS[tier])
            else:
                _log_tier_failure(tier, outcome.failure)
        return outcomes

    try:
        if to_query:
            first = await run_wave(to_query, base_radius, widening=False)
            for tier, outcome in first.items():
                if outcome.failure:
                    failed[tier] = outcome.failure
                else:
                    candidates[tier] = list(outcome.hotels)

        kept = _guard(candidates, cached_prices, settings)
        widened: set[str] = set()
        short = [
            tier
            for tier in to_query
            if tier not in failed and len(kept.get(tier, [])) < TIER_MIN_COUNT
        ]
        if short:
            second = await run_wave(short, wide_radius, widening=True)
            for tier, outcome in second.items():
                if outcome.failure:
                    continue
                candidates[tier] = _merge(candidates[tier], outcome.hotels)
                widened.add(tier)
            kept = _guard(candidates, cached_prices, settings)

        cache_ttl = int(settings.rollinggo_cache_ttl_seconds)
        failure_ttl = int(settings.rollinggo_failure_ttl_seconds)
        built: dict[str, HotelTier] = {}
        for tier in TIERS:
            if tier in cached:
                built[tier] = cached[tier]
                continue
            if tier in failed and tier not in widened and not candidates.get(tier):
                # A failed base query has no hotels. A widen that fails keeps the
                # base hotels, so those tiers are not in `failed`.
                built[tier] = _empty_tier(tier, failed[tier])
                if tier in to_query:
                    await _cache_put(
                        cache,
                        keys[tier],
                        {"ok": False, "queried_at": queried_at, "reason": failed[tier]},
                        failure_ttl,
                    )
                continue
            hotels = kept.get(tier, [])
            built[tier] = _tier_from_hotels(
                tier,
                hotels,
                raw_price_count=len(candidates.get(tier, [])),
                widened=tier in widened,
                queried_at=queried_at,
                markup=_markup(settings, tier),
            )
            if tier in to_query:
                await _cache_put(
                    cache,
                    keys[tier],
                    {
                        "ok": True,
                        "queried_at": queried_at,
                        "widened": tier in widened,
                        "raw_price_count": len(candidates.get(tier, [])),
                        "hotels": [_hotel_payload(hotel) for hotel in hotels],
                    },
                    cache_ttl,
                )
        return _result(
            checkin=checkin,
            nights=nights,
            reason=_overall_reason([built[tier] for tier in TIERS]),
            economy=built[TIER_ECONOMY],
            mid=built[TIER_MID],
            high=built[TIER_HIGH],
        )
    finally:
        if opened_client and local_client is not None:
            await local_client.aclose()


def _cached_offers(payload: dict) -> list[HotelOffer]:
    hotels: list[HotelOffer] = []
    for row in payload.get("hotels") or []:
        hotel = _hotel_from_payload(row)
        if hotel is not None:
            hotels.append(hotel)
    return hotels


async def fetch_tier_hotels(
    *,
    city: str,
    area_name: str,
    latitude: float,
    longitude: float,
    checkin_date,
    nights: int,
    settings,
    client=None,
    cache=None,
    cache_at_origin: bool = False,
) -> HotelTierResult:
    """Query three RollingGo tiers for one lodging pin and cache each for a day.

    The switch defaults off. A disabled call returns an empty result and does
    not touch the network. The access token is never written to the result,
    the cache, or the log.
    """
    placeholder = date.today()
    if not _enabled(settings):
        parsed_nights = nights if isinstance(nights, int) and not isinstance(nights, bool) and nights > 0 else 0
        parsed_date = _parse_checkin(checkin_date) or placeholder
        return _result(checkin=parsed_date, nights=parsed_nights, reason=DISABLED_REASON)
    checkin = _parse_checkin(checkin_date)
    if checkin is None:
        return _result(checkin=placeholder, nights=0, reason=INVALID_DATE_REASON)
    parsed_nights = _parse_nights(nights)
    if parsed_nights is None:
        return _result(checkin=checkin, nights=0, reason=INVALID_NIGHTS_REASON)
    origin = _parse_origin(latitude, longitude)
    if origin is None:
        return _result(checkin=checkin, nights=parsed_nights, reason=INVALID_COORD_REASON)
    token = _load_token(settings)
    if not token:
        return _result(checkin=checkin, nights=parsed_nights, reason=AUTH_REASON)
    owns_cache = cache is None
    if owns_cache:
        cache = _open_cache(settings)
    try:
        return await _collect(
            settings=settings,
            client=client,
            cache=cache,
            token=token,
            city=city,
            area_name=area_name,
            origin=origin,
            checkin=checkin,
            nights=parsed_nights,
            cache_at_origin=cache_at_origin,
        )
    finally:
        if owns_cache:
            await _close_cache(cache)
