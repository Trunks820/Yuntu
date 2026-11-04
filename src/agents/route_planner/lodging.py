"""Lodging choice and the anchor handed to route emission.

With hotel lodging off, the model still names an area and the fallback below
is unchanged. With it on, code picks one hotel cluster after the model returns.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from dataclasses import dataclass

import httpx

from src.agents.schema import (
    AccommodationAnchor,
    AccommodationSuggestion,
    CandidatePlace,
    TripRequest,
)

from src.agents.accommodation_policy import distance_km, is_remote_distance

from src.agents.preferences import is_food_partition_place

from src.agents.amap_keys import (
    SEARCH_KEY_BUCKET,
    classify_amap_failure,
    configured_amap_keys,
    get_amap_key_coordinator,
    key_fingerprint,
)

from src.agents.route_planner.rules import AREA_TYPES

from src.agents.route_feasibility import valid_coordinate

from src.agents.route_planner.places import (
    _coords,
    _place_id,
    coerce_place_id,
    _names_contain,
)

logger = logging.getLogger(__name__)

# ~500 m cells. Unnamed hotels share a cell; the cache key snaps the same way.
HOTEL_GRID_METERS = 500
METERS_PER_DEGREE = 111_320
# Straight-line "near" for the lodging sentence. The search radius is separate.
LODGING_NEAR_KM = 3.0
HOTEL_PAGE_SIZE = 25
HOTEL_TYPES = "100000"
AROUND_URL = "https://restapi.amap.com/v5/place/around"
PLACE_AROUND_MODE = "place_around"
HOTEL_CACHE_PREFIX = "yuntu-travel:hotel-lodging:v1"

NOT_ENOUGH_REASON = "高德没查到附近酒店，住处改用商圈"
ERROR_REASON = "高德查询出错，住处改用商圈"
TIMEOUT_REASON = "高德查询超时，住处改用商圈"
# Result marker for route_planner_lodging_first. ``preselected`` is only the
# state before the route center is compared; results use the four below.
LODGING_FIRST_OFF = "off"
LODGING_FIRST_UNAVAILABLE = "unavailable"
LODGING_FIRST_PRESELECTED = "preselected"
LODGING_FIRST_KEPT = "kept"
LODGING_FIRST_RESELECTED = "reselected"
LODGING_FIRST_DRIFT_NOTE = "路线和预选住处离得远，按路线重选住处"
# Not used to name an unnamed hotel cell; a meal stop is not a place to stay near.
_MEAL_PLACE_TYPES = frozenset({"restaurant", "food", "cafe", "snack", "dessert"})


def _itinerary_places(plan: dict, by_id: dict[int, CandidatePlace]) -> list[CandidatePlace]:
    """Places the model put on any day. Rest stops are not places."""
    found: list[CandidatePlace] = []
    seen: set[int] = set()
    for day in plan.get("days") or []:
        for stop in day.get("stops") or []:
            if stop.get("kind") == "rest":
                continue
            place = stop.get("_place")
            if not isinstance(place, CandidatePlace):
                token = coerce_place_id(stop.get("place_id"))
                place = by_id.get(token) if isinstance(token, int) else None
            if not isinstance(place, CandidatePlace) or place.place_id in seen:
                continue
            seen.add(place.place_id)
            found.append(place)
    return found


def _lodging_conflicts(area: CandidatePlace, itinerary: list[CandidatePlace]) -> bool:
    """True when the anchor is a stop, or the two names contain each other."""
    for place in itinerary:
        if area.place_id == place.place_id or _names_contain(area.name, place.name):
            return True
    return False


def _usable_user_lodging(anchor: AccommodationAnchor | None) -> AccommodationAnchor | None:
    """A resolver anchor the planner must keep. Anything else is not one."""
    suggestion = None if anchor is None else anchor.suggestion
    if suggestion is None or suggestion.source != "user_specified":
        return None
    if not valid_coordinate(suggestion.latitude, suggestion.longitude):
        return None
    return anchor


def _user_lodging_name(anchor: AccommodationAnchor) -> str:
    return (anchor.suggestion.name or "").strip() or "用户指定住宿"


def _place_from_user_lodging(anchor: AccommodationAnchor) -> CandidatePlace:
    """Point used for access legs. Precision is applied later, not from this type."""
    place_id = anchor.place_id if isinstance(anchor.place_id, int) and anchor.place_id > 0 else 0
    return CandidatePlace(
        place_id=place_id,
        name=_user_lodging_name(anchor),
        place_type="hotel",
        latitude=float(anchor.suggestion.latitude),
        longitude=float(anchor.suggestion.longitude),
    )


def _accommodation_attempted(request: TripRequest) -> bool:
    accommodation = request.accommodation
    if accommodation is None:
        return False
    return bool(
        accommodation.place_id
        or accommodation.latitude is not None
        or (accommodation.name or "").strip()
    )


def _take_user_lodging(
    request: TripRequest,
    pool: list[CandidatePlace],
    by_id: dict[int, CandidatePlace],
    lodging_anchor: AccommodationAnchor | None = None,
):
    """Committed user lodging, or None when this trip still needs a choice.

    The checks match the historical prefix of ``_resolve_lodging``. An
    unresolved name is not a result; the caller writes that note.
    """
    passed = _usable_user_lodging(lodging_anchor)
    if passed is not None:
        # The resolver already placed this lodging. A same-named stop does not move it.
        place = _place_from_user_lodging(passed)
        return place, "user", place.name, []
    accommodation = request.accommodation
    if accommodation is None or not _accommodation_attempted(request):
        return None
    if (
        accommodation.place_id
        and accommodation.place_id in by_id
        and _coords(by_id[accommodation.place_id])[0] is not None
    ):
        place = by_id[accommodation.place_id]
        return place, "user", place.name, []
    if accommodation.latitude is not None and accommodation.longitude is not None:
        name = (accommodation.name or "").strip() or "用户指定住宿"
        place = CandidatePlace(
            place_id=int(accommodation.place_id or 0),
            name=name,
            place_type="hotel",
            latitude=float(accommodation.latitude),
            longitude=float(accommodation.longitude),
        )
        return place, "user", name, []
    wanted = (accommodation.name or "").strip()
    for place in pool:
        if wanted and (place.name == wanted or _names_contain(wanted, place.name)) and _coords(place)[0] is not None:
            return place, "user", place.name, []
    return None


def _resolve_lodging(
    request: TripRequest,
    plan: dict,
    pool: list[CandidatePlace],
    by_id: dict[int, CandidatePlace],
    *,
    lodging_anchor: AccommodationAnchor | None = None,
    ignore_model_anchor: bool = False,
):
    taken = _take_user_lodging(request, pool, by_id, lodging_anchor)
    if taken is not None:
        return taken
    adjustments = []
    accommodation = request.accommodation
    if accommodation is not None and _accommodation_attempted(request):
        wanted = (accommodation.name or "").strip()
        adjustments.append(f"用户住宿「{wanted or '未命名'}」没有坐标，改用商圈")
    itinerary = _itinerary_places(plan, by_id)
    lodging = plan.get("lodging") if isinstance(plan.get("lodging"), dict) else {}
    anchor = coerce_place_id(lodging.get("anchor"))
    rejected: CandidatePlace | None = None
    if (
        not ignore_model_anchor
        and isinstance(anchor, int)
        and anchor in by_id
        and by_id[anchor].place_type in AREA_TYPES
        and _coords(by_id[anchor])[0] is not None
    ):
        place = by_id[anchor]
        if not _lodging_conflicts(place, itinerary):
            return place, "model", place.name, adjustments
        rejected = place
    areas = [place for place in pool if place.place_type in AREA_TYPES and _coords(place)[0] is not None]
    safe = [place for place in areas if not _lodging_conflicts(place, itinerary)]

    def _remember(place: CandidatePlace, note: str) -> tuple:
        adjustments.append(note)
        plan["lodging"] = {**lodging, "anchor": place.place_id, "area": place.name}
        return place, "auto", place.name, adjustments

    if safe:
        place = max(safe, key=lambda item: (item.mention_count or 0, -item.place_id))
        if rejected is not None:
            return _remember(place, f"住处「{rejected.name}」与行程地点重名或互相包含，改为「{place.name}」")
        top = max(areas, key=lambda item: (item.mention_count or 0, -item.place_id))
        if top.place_id != place.place_id:
            return _remember(place, f"住处「{top.name}」与行程地点重名或互相包含，改为「{place.name}」")
        return _remember(place, f"住宿改为候选里提及最多的商圈「{place.name}」，记为自动推荐")
    blocked = rejected or (max(areas, key=lambda item: (item.mention_count or 0, -item.place_id)) if areas else None)
    if blocked is not None:
        plan["lodging"] = {**lodging, "anchor": None, "area": ""}
        adjustments.append(f"住处「{blocked.name}」与行程地点重名或互相包含，这次不写住处")
    return None, "", "", adjustments


def _lodging_anchor(
    lodge,
    lodging_source: str,
    *,
    location_precision: str | None = None,
) -> AccommodationAnchor | None:
    """Lodging already used for access legs. This does not change the schedule.

    ``location_precision`` keeps a resolver result. A lodging this module
    picked itself still uses the hotel / area rule.
    """
    latitude, longitude = _coords(lodge)
    if lodge is None or latitude is None or longitude is None:
        return None
    source = "user_specified" if lodging_source == "user" else "auto_recommended"
    name = str(getattr(lodge, "name", "") or "").strip() or "建议住宿区域"
    if location_precision not in {"point", "area", "unknown"}:
        place_type = getattr(lodge, "place_type", "")
        location_precision = "point" if place_type == "hotel" else "area"
    return AccommodationAnchor(
        suggestion=AccommodationSuggestion(
            name=name,
            latitude=latitude,
            longitude=longitude,
            source=source,
        ),
        place_id=_place_id(lodge),
        location_precision=location_precision,
    )


def hotel_lodging_enabled(settings) -> bool:
    """True only when this call was given settings and the flag is on.

    ``settings is None`` stays off. Callers must not read ``get_settings()``
    just to decide the flag, or a developer ``.env`` would change the tests.
    """
    return bool(
        settings is not None
        and getattr(settings, "route_planner_hotel_lodging_enabled", False)
    )


def lodging_first_enabled(settings) -> bool:
    """Hotel lodging on, and the lodging-first trial on.

    Either flag off, or ``settings is None``, leaves the package U path.
    """
    return bool(
        hotel_lodging_enabled(settings)
        and getattr(settings, "route_planner_lodging_first_enabled", False)
    )


def _estimator_deadline(estimator) -> float | None:
    """Wall-clock deadline already started for this route's Amap budget."""
    current = estimator
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        provider = getattr(current, "provider", None)
        if provider is not None:
            return getattr(provider, "_deadline_monotonic", None)
        current = getattr(current, "inner", None)
    return None


@dataclass(frozen=True)
class HotelHit:
    name: str
    latitude: float
    longitude: float
    business_area: str = ""
    typecode: str = ""


class _HotelSearchError(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _mean_point(points: list[tuple[float, float]]) -> tuple[float, float] | None:
    if not points:
        return None
    return (
        sum(point[0] for point in points) / len(points),
        sum(point[1] for point in points) / len(points),
    )


def _median_point(points: list[tuple[float, float]]) -> tuple[float, float] | None:
    if not points:
        return None

    def _axis(values: list[float]) -> float:
        ordered = sorted(values)
        mid = len(ordered) // 2
        if len(ordered) % 2:
            return ordered[mid]
        return (ordered[mid - 1] + ordered[mid]) / 2.0

    return (
        _axis([point[0] for point in points]),
        _axis([point[1] for point in points]),
    )


def _day_points(plan: dict, by_id: dict[int, CandidatePlace]) -> list[list[tuple[float, float]]]:
    """Coordinate points on each day, before places are attached."""
    days: list[list[tuple[float, float]]] = []
    for day in plan.get("days") or []:
        points: list[tuple[float, float]] = []
        for stop in day.get("stops") or []:
            if stop.get("kind") == "rest":
                continue
            place = stop.get("_place")
            if not isinstance(place, CandidatePlace):
                token = coerce_place_id(stop.get("place_id"))
                place = by_id.get(token) if isinstance(token, int) else None
            if not isinstance(place, CandidatePlace):
                continue
            latitude, longitude = _coords(place)
            if latitude is None or longitude is None:
                continue
            points.append((float(latitude), float(longitude)))
        days.append(points)
    return days


def _center_from_day_points(
    day_points: list[list[tuple[float, float]]],
) -> tuple[tuple[float, float] | None, list[bool]]:
    """Search center, and which days are far suburbs of the day-center median.

    A day whose own center is at least ``REMOTE_DISTANCE_KM`` from the median
    of the day centers is left out. The center is then the mean of the
    remaining days' points. When every day with coordinates is remote, the
    center falls back to the mean of all of those points and the days stay
    remote. A day with no coordinates is not remote and adds no points.
    """
    centers: list[tuple[float, float]] = []
    indexed: list[int] = []
    for index, points in enumerate(day_points):
        center = _mean_point(points)
        if center is None:
            continue
        indexed.append(index)
        centers.append(center)
    remote = [False] * len(day_points)
    if not centers:
        return None, remote
    median = _median_point(centers)
    assert median is not None
    kept: list[tuple[float, float]] = []
    for index, center in zip(indexed, centers):
        if is_remote_distance(distance_km(median, center)):
            remote[index] = True
            continue
        kept.extend(day_points[index])
    if kept:
        return _mean_point(kept), remote
    everything = [point for points in day_points for point in points]
    return _mean_point(everything), remote


def _search_center(
    plan: dict,
    by_id: dict[int, CandidatePlace],
) -> tuple[tuple[float, float] | None, list[bool]]:
    return _center_from_day_points(_day_points(plan, by_id))


def _grid_steps(latitude: float) -> tuple[float, float]:
    lat_step = HOTEL_GRID_METERS / METERS_PER_DEGREE
    cosine = math.cos(math.radians(latitude))
    if abs(cosine) < 1e-6:
        cosine = 1e-6 if cosine >= 0 else -1e-6
    lon_step = HOTEL_GRID_METERS / (METERS_PER_DEGREE * cosine)
    return lat_step, lon_step


def _snap_axis(value: float, step: float) -> float:
    scaled = value / step
    if scaled >= 0:
        snapped = math.floor(scaled + 0.5)
    else:
        snapped = math.ceil(scaled - 0.5)
    return snapped * step


def _snap_to_grid(latitude: float, longitude: float) -> tuple[float, float]:
    """Snap to a ~500 m cell.

    Longitude step depends on latitude. It is taken from the snapped latitude,
    so every point in one latitude band shares one longitude grid. A step taken
    from the raw latitude would move the longitude line between two nearby points.
    """
    lat_step, _ignored = _grid_steps(latitude)
    snapped_lat = _snap_axis(latitude, lat_step)
    _lat_step, lon_step = _grid_steps(snapped_lat)
    return (snapped_lat, _snap_axis(longitude, lon_step))


def _hotel_cache_key(latitude: float, longitude: float, radius: int, pages: int) -> str:
    snapped_lat, snapped_lon = _snap_to_grid(latitude, longitude)
    return (
        f"{HOTEL_CACHE_PREFIX}:{snapped_lat:.6f}:{snapped_lon:.6f}:"
        f"{int(radius)}:{int(pages)}"
    )


def _itinerary_names(plan: dict, by_id: dict[int, CandidatePlace]) -> set[str]:
    return {
        place.name.strip()
        for place in _itinerary_places(plan, by_id)
        if (place.name or "").strip()
    }


def _grid_area_name(
    point: tuple[float, float],
    itinerary_places: list[CandidatePlace],
) -> str | None:
    """``<nearest itinerary stop>一带`` for a cell with no business-area name.

    A hotel's own name is never the area: "建议住在汉庭酒店附近" reads as a
    hotel recommendation and fails the accommodation brand gate. Meal stops
    are skipped. No usable stop means no name, and the caller falls back.
    """
    best: tuple[float, str] | None = None
    for place in itinerary_places:
        if place.place_type in _MEAL_PLACE_TYPES:
            continue
        latitude, longitude = _coords(place)
        name = (place.name or "").strip()
        if latitude is None or longitude is None or not name:
            continue
        distance = distance_km(point, (float(latitude), float(longitude)))
        if best is None or distance < best[0]:
            best = (distance, name)
    return None if best is None else f"{best[1]}一带"


def _pick_hotel_area(
    hotels: list[HotelHit],
    center: tuple[float, float],
    itinerary_names: set[str],
    itinerary_places: list[CandidatePlace] = (),
) -> tuple[str, float, float] | None:
    """Winning area name and the hotel coordinate closest to that group's center.

    Named hotels group by ``business_area``. Unnamed hotels group by the 500 m
    cell and are not also filed under a name. The largest group wins. A tie
    goes to the group whose centroid is closer to the search center, then to
    the name. The lodging point is the hotel in that group closest to the
    group's own centroid. A grid group is named after the nearest itinerary
    stop (``_grid_area_name``); without one there is no pick. An exact
    itinerary-name match gains the suffix ``商圈``.
    """
    usable = [
        hotel for hotel in hotels
        if math.isfinite(hotel.latitude) and math.isfinite(hotel.longitude)
    ]
    if not usable:
        return None
    groups: dict[str, list[HotelHit]] = {}
    for hotel in usable:
        area = (hotel.business_area or "").strip()
        if area:
            key = "area:" + area
        else:
            latitude, longitude = _snap_to_grid(hotel.latitude, hotel.longitude)
            key = f"grid:{latitude:.6f}:{longitude:.6f}"
        groups.setdefault(key, []).append(hotel)

    def _group_sort(item: tuple[str, list[HotelHit]]) -> tuple:
        key, members = item
        centroid = _mean_point([(hotel.latitude, hotel.longitude) for hotel in members])
        distance = distance_km(center, centroid) if centroid is not None else 1e9
        label = key[5:] if key.startswith("area:") else key
        return (-len(members), distance, label)

    key, members = min(groups.items(), key=_group_sort)
    centroid = _mean_point([(hotel.latitude, hotel.longitude) for hotel in members])
    assert centroid is not None

    def _hotel_sort(hotel: HotelHit) -> tuple:
        here = (hotel.latitude, hotel.longitude)
        return (
            distance_km(centroid, here),
            distance_km(center, here),
            hotel.name or "",
        )

    chosen = min(members, key=_hotel_sort)
    if key.startswith("area:"):
        name = key[5:]
    else:
        name = _grid_area_name((chosen.latitude, chosen.longitude), list(itinerary_places))
        if name is None:
            return None
    blocked = {item.strip() for item in itinerary_names if item and item.strip()}
    if name in blocked:
        name = f"{name}商圈"
    return name, float(chosen.latitude), float(chosen.longitude)


def _day_label(numbers: list[int]) -> str:
    return "第" + "、".join(str(number) for number in numbers) + "天"


def _minute_span(values: list[int]) -> str:
    low = min(values)
    high = max(values)
    if low == high:
        return f"{low}分钟"
    return f"{low}–{high}分钟"


def _format_lodging_reason(
    *,
    lodging: tuple[float, float],
    day_points: list[list[tuple[float, float]]],
    day_numbers: list[int],
    round_trip_minutes: list[int],
) -> str:
    """One sentence from day centers and each day's lodging round trip.

    Remote uses the same median-of-day-centers rule as the search center.
    Near means that day's center is within ``LODGING_NEAR_KM`` of the lodging
    and the day is not remote. A day that is neither is left out of both
    clauses. Minutes are the min and max of the days in that clause.
    """
    centers: list[tuple[float, float]] = []
    usable: list[int] = []
    for index, points in enumerate(day_points):
        center = _mean_point(points)
        if center is None:
            continue
        centers.append(center)
        usable.append(index)
    if not usable:
        return ""
    median = _median_point(centers)
    assert median is not None
    near_days: list[int] = []
    near_minutes: list[int] = []
    far_days: list[int] = []
    far_minutes: list[int] = []
    middle_minutes: list[int] = []
    for index, center in zip(usable, centers):
        number = day_numbers[index]
        minutes = int(round_trip_minutes[index])
        if is_remote_distance(distance_km(median, center)):
            far_days.append(number)
            far_minutes.append(minutes)
            continue
        if distance_km(lodging, center) <= LODGING_NEAR_KM:
            near_days.append(number)
            near_minutes.append(minutes)
            continue
        middle_minutes.append(minutes)
    parts: list[str] = []
    if near_days:
        parts.append(
            f"离{_day_label(near_days)}的点都近，每天来回约{_minute_span(near_minutes)}"
        )
    if far_days:
        parts.append(f"{_day_label(far_days)}去远郊，来回约{_minute_span(far_minutes)}")
    if parts:
        return "；".join(parts)
    if middle_minutes:
        return f"每天来回约{_minute_span(middle_minutes)}"
    return ""


def _lodging_reason_for_route(route) -> str:
    anchor = getattr(route, "accommodation_anchor", None)
    suggestion = None if anchor is None else anchor.suggestion
    if suggestion is None:
        return ""
    lodging = (float(suggestion.latitude), float(suggestion.longitude))
    day_points: list[list[tuple[float, float]]] = []
    numbers: list[int] = []
    minutes: list[int] = []
    for group in route.day_groups:
        points: list[tuple[float, float]] = []
        for place in group.places:
            latitude, longitude = _coords(place)
            if latitude is None or longitude is None:
                continue
            points.append((float(latitude), float(longitude)))
        day_points.append(points)
        numbers.append(int(group.day))
        minutes.append(sum(int(leg.duration_minutes) for leg in group.access_legs))
    return _format_lodging_reason(
        lodging=lodging,
        day_points=day_points,
        day_numbers=numbers,
        round_trip_minutes=minutes,
    )


def _hotel_area_place(name: str, latitude: float, longitude: float) -> CandidatePlace:
    # place_id 0 keeps this off the candidate map. accommodation_area makes
    # the delivered precision "area" rather than a hotel point.
    return CandidatePlace(
        place_id=0,
        name=name,
        place_type="accommodation_area",
        latitude=float(latitude),
        longitude=float(longitude),
    )


class _NullHotelCache:
    async def get(self, key: str):
        return None

    async def put(self, key: str, hotels: list[HotelHit], ttl: int) -> None:
        return None

    async def aclose(self) -> None:
        return None


def _hotels_to_json(hotels: list[HotelHit]) -> str:
    payload = [
        {
            "name": hotel.name,
            "latitude": hotel.latitude,
            "longitude": hotel.longitude,
            "business_area": hotel.business_area,
            "typecode": hotel.typecode,
        }
        for hotel in hotels
    ]
    return json.dumps(payload, ensure_ascii=False)


def _hotels_from_json(raw: str) -> list[HotelHit] | None:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, list):
        return None
    hotels: list[HotelHit] = []
    for row in payload:
        if not isinstance(row, dict):
            return None
        try:
            hotels.append(HotelHit(
                name=str(row.get("name") or ""),
                latitude=float(row["latitude"]),
                longitude=float(row["longitude"]),
                business_area=str(row.get("business_area") or ""),
                typecode=str(row.get("typecode") or ""),
            ))
        except (TypeError, ValueError, KeyError):
            return None
    return hotels


class RedisHotelCache:
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
            )
        except Exception:
            logger.info("hotel lodging redis client init failed; querying without cache")
            self._failed = True
            self._client = None
            return None
        return self._client

    async def get(self, key: str) -> list[HotelHit] | None:
        try:
            client = await self._connect()
            if client is None:
                return None
            raw = await client.get(key)
        except Exception:
            return None
        if not raw:
            return None
        return _hotels_from_json(raw)

    async def put(self, key: str, hotels: list[HotelHit], ttl: int) -> None:
        try:
            client = await self._connect()
            if client is None:
                return
            await client.set(key, _hotels_to_json(hotels), ex=int(ttl))
        except Exception:
            return

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
            return


def _open_hotel_cache(settings):
    url = str(getattr(settings, "redis_url", "") or "").strip()
    if not url:
        return _NullHotelCache(), False
    from src.agents.route_planning import REDIS_ROUTE_CACHE_TIMEOUT_SECONDS

    return RedisHotelCache(url, REDIS_ROUTE_CACHE_TIMEOUT_SECONDS), True


def _poi_to_hotel(poi: object) -> HotelHit | None:
    if not isinstance(poi, dict):
        return None
    parts = str(poi.get("location") or "").split(",")
    if len(parts) != 2:
        return None
    try:
        longitude = float(parts[0])
        latitude = float(parts[1])
    except ValueError:
        return None
    business = poi.get("business") if isinstance(poi.get("business"), dict) else {}
    raw_area = business.get("business_area") if isinstance(business, dict) else ""
    if isinstance(raw_area, list):
        raw_area = next((str(item).strip() for item in raw_area if str(item).strip()), "")
    area = str(raw_area or "").strip()
    if area in {"[]", "null", "None"}:
        area = ""
    return HotelHit(
        name=str(poi.get("name") or "").strip(),
        latitude=latitude,
        longitude=longitude,
        business_area=area,
        typecode=str(poi.get("typecode") or poi.get("type") or ""),
    )


def _within_radius(
    origin: tuple[float, float],
    hotel: HotelHit,
    radius_meters: int,
) -> bool:
    meters = distance_km(origin, (hotel.latitude, hotel.longitude)) * 1000.0
    return meters <= float(radius_meters) + 1e-6


def _poi_identity(poi: dict, hotel: HotelHit) -> str:
    poi_id = str(poi.get("id") or "").strip()
    if poi_id:
        return "id:" + poi_id
    return f"name:{hotel.name}:{hotel.latitude:.5f}:{hotel.longitude:.5f}"


def _call_timeout(settings, deadline) -> float:
    configured = float(settings.route_planner_hotel_lodging_timeout_seconds)
    if deadline is None:
        return configured
    remaining = float(deadline) - time.monotonic()
    if remaining <= 0:
        raise _HotelSearchError(TIMEOUT_REASON)
    return min(configured, remaining)


async def _fetch_hotel_page(
    client: httpx.AsyncClient,
    *,
    latitude: float,
    longitude: float,
    page: int,
    radius: int,
    settings,
    deadline,
    keys: list[str],
    excluded: set[str],
) -> dict:
    coordinator = get_amap_key_coordinator()
    last_reason = ERROR_REASON
    for _attempt in range(max(1, len(keys) + 1)):
        key = coordinator.next_key(
            keys,
            bucket=SEARCH_KEY_BUCKET,
            mode=PLACE_AROUND_MODE,
            exclude=excluded,
        )
        if key is None:
            raise _HotelSearchError(last_reason)
        per_call = _call_timeout(settings, deadline)
        params = {
            "key": key,
            "location": f"{longitude:.6f},{latitude:.6f}",
            "types": HOTEL_TYPES,
            "radius": str(int(radius)),
            "sortrule": "distance",
            "show_fields": "business",
            "page_size": str(HOTEL_PAGE_SIZE),
            "page_num": str(page),
        }
        try:
            response = await asyncio.wait_for(
                client.get(AROUND_URL, params=params, timeout=per_call),
                timeout=per_call,
            )
        except (TimeoutError, asyncio.TimeoutError, httpx.TimeoutException):
            raise _HotelSearchError(TIMEOUT_REASON) from None
        except httpx.HTTPError:
            raise _HotelSearchError(ERROR_REASON) from None
        if response.status_code == 429:
            coordinator.mark_over_limit(
                key,
                PLACE_AROUND_MODE,
                float(getattr(settings, "amap_route_backoff_seconds", 60.0)),
            )
            excluded.add(key_fingerprint(key))
            last_reason = ERROR_REASON
            logger.warning(
                "amap hotel search over_limit fingerprint=%s http=429",
                key_fingerprint(key),
            )
            continue
        if response.status_code >= 400:
            raise _HotelSearchError(ERROR_REASON)
        try:
            payload = response.json()
        except Exception:
            raise _HotelSearchError(ERROR_REASON) from None
        if not isinstance(payload, dict):
            raise _HotelSearchError(ERROR_REASON)
        if str(payload.get("status")) != "1":
            kind = classify_amap_failure(payload)
            if kind == "quota":
                coordinator.mark_quota_exhausted(key, bucket=SEARCH_KEY_BUCKET)
                excluded.add(key_fingerprint(key))
                last_reason = ERROR_REASON
                logger.warning(
                    "amap hotel search quota fingerprint=%s infocode=%s",
                    key_fingerprint(key),
                    str(payload.get("infocode") or ""),
                )
                continue
            if kind == "over_limit":
                coordinator.mark_over_limit(
                    key,
                    PLACE_AROUND_MODE,
                    float(getattr(settings, "amap_route_backoff_seconds", 60.0)),
                )
                excluded.add(key_fingerprint(key))
                last_reason = ERROR_REASON
                logger.warning(
                    "amap hotel search over_limit fingerprint=%s infocode=%s",
                    key_fingerprint(key),
                    str(payload.get("infocode") or ""),
                )
                continue
            raise _HotelSearchError(ERROR_REASON)
        return payload
    raise _HotelSearchError(last_reason)


async def _fetch_hotels_around(
    latitude: float,
    longitude: float,
    *,
    settings,
    deadline=None,
    client: httpx.AsyncClient | None = None,
) -> list[HotelHit]:
    keys = configured_amap_keys(settings)
    if not keys:
        logger.warning("amap hotel search skipped: no key configured")
        raise _HotelSearchError(ERROR_REASON)
    radius = int(settings.route_planner_hotel_lodging_radius_meters)
    pages = int(settings.route_planner_hotel_lodging_pages)
    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient()
    hotels: list[HotelHit] = []
    seen: set[str] = set()
    excluded: set[str] = set()
    origin = (float(latitude), float(longitude))
    try:
        for page in range(1, pages + 1):
            payload = await _fetch_hotel_page(
                client,
                latitude=latitude,
                longitude=longitude,
                page=page,
                radius=radius,
                settings=settings,
                deadline=deadline,
                keys=keys,
                excluded=excluded,
            )
            pois = payload.get("pois") or []
            if not isinstance(pois, list):
                raise _HotelSearchError(ERROR_REASON)
            for poi in pois:
                if not isinstance(poi, dict):
                    continue
                hotel = _poi_to_hotel(poi)
                if hotel is None or not _within_radius(origin, hotel, radius):
                    continue
                identity = _poi_identity(poi, hotel)
                if identity in seen:
                    continue
                seen.add(identity)
                hotels.append(hotel)
            if len(pois) < HOTEL_PAGE_SIZE:
                break
    finally:
        if owns_client:
            await client.aclose()
    return hotels


async def _query_hotels(
    latitude: float,
    longitude: float,
    *,
    settings,
    deadline,
    hotel_query,
    client,
) -> list[HotelHit]:
    if hotel_query is None:
        return await _fetch_hotels_around(
            latitude,
            longitude,
            settings=settings,
            deadline=deadline,
            client=client,
        )
    try:
        rows = await hotel_query(latitude, longitude)
    except _HotelSearchError:
        raise
    except (TimeoutError, asyncio.TimeoutError):
        raise _HotelSearchError(TIMEOUT_REASON) from None
    except Exception:
        raise _HotelSearchError(ERROR_REASON) from None
    hotels: list[HotelHit] = []
    for row in rows or []:
        if not isinstance(row, HotelHit):
            raise _HotelSearchError(ERROR_REASON)
        hotels.append(row)
    return hotels


async def _load_hotels(
    latitude: float,
    longitude: float,
    *,
    settings,
    deadline,
    hotel_query,
    hotel_cache,
    client=None,
) -> list[HotelHit]:
    radius = int(settings.route_planner_hotel_lodging_radius_meters)
    pages = int(settings.route_planner_hotel_lodging_pages)
    ttl = int(settings.route_planner_hotel_lodging_cache_ttl_seconds)
    key = _hotel_cache_key(latitude, longitude, radius, pages)
    if hotel_cache is None:
        hotel_cache, owned = _open_hotel_cache(settings)
    else:
        owned = False
    try:
        try:
            cached = await hotel_cache.get(key)
        except Exception:
            cached = None
        if cached is not None:
            return list(cached)
        if deadline is not None and time.monotonic() >= float(deadline):
            raise _HotelSearchError(TIMEOUT_REASON)
        hotels = await _query_hotels(
            latitude,
            longitude,
            settings=settings,
            deadline=deadline,
            hotel_query=hotel_query,
            client=client,
        )
        try:
            await hotel_cache.put(key, hotels, ttl)
        except Exception:
            pass
        return hotels
    finally:
        if owned:
            await hotel_cache.aclose()


async def _choose_hotel_lodging(
    request: TripRequest,
    plan: dict,
    pool: list[CandidatePlace],
    by_id: dict[int, CandidatePlace],
    *,
    lodging_anchor: AccommodationAnchor | None = None,
    settings,
    hotel_query=None,
    hotel_cache=None,
    deadline=None,
    client=None,
):
    """Pick one hotel cluster, or the most-mentioned area when search cannot.

    A committed user lodging returns before any cache or hotel query. The
    model's ``lodging.anchor`` is not used on this path, including the
    area fallback.
    """
    taken = _take_user_lodging(request, pool, by_id, lodging_anchor)
    if taken is not None:
        return taken

    def _fallback(reason: str):
        place, source, name, notes = _resolve_lodging(
            request,
            plan,
            pool,
            by_id,
            lodging_anchor=lodging_anchor,
            ignore_model_anchor=True,
        )
        return place, source, name, [reason, *notes]

    center, _remote = _search_center(plan, by_id)
    if center is None:
        return _fallback(NOT_ENOUGH_REASON)
    try:
        hotels = await _load_hotels(
            center[0],
            center[1],
            settings=settings,
            deadline=deadline,
            hotel_query=hotel_query,
            hotel_cache=hotel_cache,
            client=client,
        )
    except _HotelSearchError as exc:
        return _fallback(exc.reason)
    def _attach_nearby_names(place: CandidatePlace | None) -> None:
        # Handoff for the lodging paragraph. The area chosen above is unchanged.
        if place is None:
            return
        names = [
            str(hotel.name).strip()
            for hotel in list(hotels)[:50]
            if str(getattr(hotel, "name", "") or "").strip()
        ]
        object.__setattr__(place, "_nearby_lodging_hotel_names", names)

    minimum = int(getattr(settings, "route_planner_hotel_lodging_min_hotels", 5))
    if len(hotels) < minimum:
        place, source, name, notes = _fallback(NOT_ENOUGH_REASON)
        _attach_nearby_names(place)
        return place, source, name, notes
    picked = _pick_hotel_area(
        hotels,
        center,
        _itinerary_names(plan, by_id),
        _itinerary_places(plan, by_id),
    )
    if picked is None:
        place, source, name, notes = _fallback(NOT_ENOUGH_REASON)
        _attach_nearby_names(place)
        return place, source, name, notes
    name, latitude, longitude = picked
    place = _hotel_area_place(name, latitude, longitude)
    _attach_nearby_names(place)
    plan["lodging"] = {"area": name, "anchor": None}
    notes: list[str] = []
    accommodation = request.accommodation
    if accommodation is not None and _accommodation_attempted(request):
        label = (accommodation.name or "").strip() or "未命名"
        notes.append(f"用户住宿「{label}」没有坐标，已按附近酒店选住处")
    return place, "auto", name, notes


def _candidate_score_key(place: CandidatePlace) -> tuple:
    """Same numeric order ``diversity.rank_candidates`` sorts on.

    The score is ``effective_score``. Ties follow ``recommend_score``,
    ``mention_count``, then the smaller place id. The preference flag in
    front of that ranking key is not a score, so it is not used here.
    """
    return (
        -float(place.effective_score or 0.0),
        -float(place.recommend_score or 0.0),
        -int(place.mention_count or 0),
        int(place.place_id),
    )


def _lodging_first_center(
    places: list[CandidatePlace],
    *,
    must_ids: set[int],
    limit: int,
) -> tuple[tuple[float, float] | None, list[CandidatePlace]]:
    """Mean of must-go places plus the top non-food places, after dropping suburbs.

    The median is the same per-axis median as the route search center. A place
    at least ``REMOTE_DISTANCE_KM`` from that median is left out via
    ``is_remote_distance``. When nothing remains, there is no center.
    """
    located: list[CandidatePlace] = []
    for place in places:
        latitude, longitude = _coords(place)
        if latitude is None or longitude is None:
            continue
        located.append(place)
    chosen: dict[int, CandidatePlace] = {}
    for place in located:
        if place.place_id in must_ids:
            chosen[place.place_id] = place
    ranked = sorted(
        (place for place in located if not is_food_partition_place(place)),
        key=_candidate_score_key,
    )
    for place in ranked[: max(0, int(limit))]:
        chosen.setdefault(place.place_id, place)
    if not chosen:
        return None, []
    median = _median_point([
        (float(_coords(place)[0]), float(_coords(place)[1]))
        for place in chosen.values()
    ])
    assert median is not None
    kept: list[CandidatePlace] = []
    for place in chosen.values():
        point = (float(_coords(place)[0]), float(_coords(place)[1]))
        if is_remote_distance(distance_km(median, point)):
            continue
        kept.append(place)
    if not kept:
        return None, []
    center = _mean_point([
        (float(_coords(place)[0]), float(_coords(place)[1]))
        for place in kept
    ])
    return center, kept


def _unresolved_accommodation_note(request: TripRequest) -> list[str]:
    accommodation = request.accommodation
    if accommodation is None or not _accommodation_attempted(request):
        return []
    label = (accommodation.name or "").strip() or "未命名"
    return [f"用户住宿「{label}」没有坐标，已按附近酒店选住处"]


def _keep_preset_lodging(
    request: TripRequest,
    plan: dict,
    preset: CandidatePlace,
):
    """Use the lodging picked before the prompt. Source stays ``auto``."""
    name = (preset.name or "").strip() or "建议住宿区域"
    plan["lodging"] = {"area": name, "anchor": None}
    return preset, "auto", name, _unresolved_accommodation_note(request)


def _preset_for_plan(
    plan: dict,
    by_id: dict[int, CandidatePlace],
    preset: CandidatePlace,
    *,
    drift_km: float,
) -> tuple[CandidatePlace | None, str]:
    """Keep the preset, or drop it when the route center is farther than ``drift_km``.

    ``>`` matches "超过". A route with no center cannot be called drifted.
    """
    center, _remote = _search_center(plan, by_id)
    latitude, longitude = _coords(preset)
    if (
        center is not None
        and latitude is not None
        and longitude is not None
        and distance_km(center, (float(latitude), float(longitude))) > float(drift_km)
    ):
        return None, LODGING_FIRST_RESELECTED
    return preset, LODGING_FIRST_KEPT


async def _preselect_lodging(
    places: list[CandidatePlace],
    *,
    must_ids: set[int],
    settings,
    hotel_query=None,
    hotel_cache=None,
    deadline=None,
    client=None,
) -> CandidatePlace | None:
    """One hotel cluster around the candidate center, or None when search cannot.

    The route clock has not started. ``deadline`` stays None unless the caller
    already has one, so the hotel timeout is the configured per-call limit.
    """
    limit = int(getattr(settings, "route_planner_lodging_first_top_count", 12))
    center, anchors = _lodging_first_center(places, must_ids=must_ids, limit=limit)
    if center is None:
        return None
    try:
        hotels = await _load_hotels(
            center[0],
            center[1],
            settings=settings,
            deadline=deadline,
            hotel_query=hotel_query,
            hotel_cache=hotel_cache,
            client=client,
        )
    except _HotelSearchError:
        return None
    minimum = int(getattr(settings, "route_planner_hotel_lodging_min_hotels", 5))
    if len(hotels) < minimum:
        return None
    picked = _pick_hotel_area(
        hotels,
        center,
        {
            place.name.strip()
            for place in anchors
            if (place.name or "").strip()
        },
        anchors,
    )
    if picked is None:
        return None
    name, latitude, longitude = picked
    if not str(name or "").strip():
        return None
    place = _hotel_area_place(name, latitude, longitude)
    # Same handoff as _choose_hotel_lodging: the lodging paragraph reads these
    # names to tell a homestay-heavy area. A kept preset carries them through.
    object.__setattr__(
        place,
        "_nearby_lodging_hotel_names",
        [str(hotel.name).strip() for hotel in list(hotels)[:50] if str(getattr(hotel, "name", "") or "").strip()],
    )
    return place

