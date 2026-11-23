"""Leg estimates, quoted hops and the wording for a move."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterable

from src.agents.route_planning import (
    PreciseRoute,
    RouteProviderError,
    _estimated_leg,
    _v2_access_key,
    _v2_finalize_day,
)

from src.agents.schema import (
    AccessLeg,
    CandidatePlace,
    CommuteLeg,
    RoutePlan,
    TripRequest,
)

from src.agents.route_planner.rules import (
    WALK_CAP_MINUTES,
    SHORT_WALK_MINUTES,
    SHORT_DRIVE_WALK_MINUTES,
    RELAXED_WALK_MINUTES,
    RELAXED_DRIVE_WALK_MINUTES,
    ALWAYS_WALK_KM,
    FAR_WALK_KM,
)

from src.agents.route_planner.places import (
    hav_km,
    _coords,
    _place_id,
    _point_key,
)



def _quoted_route(estimator, origin, dest, mode: str) -> PreciseRoute | None:
    """Precise fact stashed by the estimator that just quoted this pair."""
    for owner in (estimator, getattr(estimator, "inner", None)):
        routes = getattr(owner, "routes", None)
        if not isinstance(routes, dict):
            continue
        found = routes.get((_point_key(origin), _point_key(dest), mode))
        if isinstance(found, PreciseRoute):
            return found
    return None


_HOP_MODE = {"步行": "walking", "公交": "transit", "打车": "driving", "自驾": "driving", "骑行": "cycling"}
_HOP_QUOTE_MODE = {"步行": "walking", "公交": "transit", "打车": "driving", "自驾": "driving", "骑行": "cycling"}
_HOP_WORD = {"步行": "步行", "公交": "公交", "打车": "打车", "自驾": "驾车", "骑行": "骑行"}
_ESTIMATE_MODE = {"步行": "walking", "公交": "transit", "打车": "driving", "自驾": "driving", "骑行": "cycling"}
_ESTIMATE_WORD = {"walking": "步行", "transit": "公交", "driving": "驾车", "cycling": "骑行"}


class LegChoice(tuple):
    """(minutes, label) plus where the minutes came from.

    Equality stays the two-item tuple so existing checks keep working.
    """

    def __new__(cls, minutes, label, source: str = "amap", transit_minutes: int | None = None):
        choice = tuple.__new__(cls, (minutes, label))
        choice.source = "estimate" if source == "estimate" else "amap"
        choice.transit_minutes = transit_minutes
        return choice


def _hop_note(
    origin_name: str, dest_name: str, label: str, minutes: int, transit_minutes: int | None = None,
) -> str:
    if label == "打车" and transit_minutes is not None:
        return f"{origin_name} → {dest_name}：公交约 {transit_minutes} 分钟，打车约 {minutes} 分钟，建议打车"
    word = _HOP_WORD.get(label, label)
    return f"{origin_name} → {dest_name}：{word}约 {int(minutes)} 分钟"


def _estimate_note(origin_name: str, dest_name: str, label: str, minutes: int) -> str:
    mode = _ESTIMATE_MODE.get(label, "walking")
    word = _ESTIMATE_WORD.get(mode, label)
    return f"{origin_name} → {dest_name}：{word}预计 {int(minutes)} 分钟"


def _estimated_commute(origin, dest, label: str) -> CommuteLeg | None:
    """Same distance estimate the old scheduler uses when Amap has no leg."""
    mode = _ESTIMATE_MODE.get(label)
    if mode is None:
        return None
    left, right = _route_place(origin), _route_place(dest)
    if _coords(left)[0] is None or _coords(right)[0] is None:
        return None
    try:
        return _estimated_leg(left, right, effective_mode=mode)
    except ValueError:
        return None


def _fallback_minutes(origin, dest, label: str) -> int | None:
    leg = _estimated_commute(origin, dest, label)
    if leg is None:
        return None
    minutes = int(leg.duration_minutes)
    if label == "打车":
        minutes += 5
    return minutes


def _chosen_hop(
    estimator, origin, dest, minutes, label: str, source: str = "amap", transit_minutes: int | None = None,
) -> dict:
    quote_mode = _HOP_QUOTE_MODE.get(str(label))
    estimated = source == "estimate"
    detail = None if estimated or not quote_mode else _quoted_route(estimator, origin, dest, quote_mode)
    return {
        "from_id": int(_place_id(origin) or 0),
        "to_id": int(_place_id(dest) or 0),
        "from_name": str(getattr(origin, "name", "") or ""),
        "to_name": str(getattr(dest, "name", "") or ""),
        "label": label,
        "minutes": minutes,
        "detail": detail,
        "source": "estimate" if estimated else "amap",
        "transit_minutes": transit_minutes,
    }


def _meters(origin, dest, detail: PreciseRoute | None) -> int:
    if detail is not None:
        return max(0, int(detail.distance_meters))
    distance = hav_km(origin, dest)
    if distance is None:
        return 0
    return max(0, int(round(distance * 1000)))


def _commute_from_hop(origin: CandidatePlace, dest: CandidatePlace, hop: dict) -> CommuteLeg:
    label = str(hop.get("label") or "步行")
    minutes = int(hop["minutes"])
    mode = _HOP_MODE.get(label, "walking")
    detail = hop.get("detail") if isinstance(hop.get("detail"), PreciseRoute) else None
    estimated = hop.get("source") == "estimate"
    steps = []
    quality = "missing"
    cache_hit = False
    polyline = ""
    fare = None
    distance = _meters(origin, dest, None if estimated else detail)
    note = _hop_note(origin.name, dest.name, label, minutes, hop.get("transit_minutes"))
    if estimated:
        estimate = _estimated_commute(origin, dest, label)
        if estimate is not None:
            distance = int(estimate.distance_meters)
        note = _estimate_note(origin.name, dest.name, label, minutes)
        detail = None
    elif detail is not None and mode == "transit":
        steps = list(detail.transit_steps)
        quality = detail.transit_detail_quality
        cache_hit = bool(detail.transit_detail_cache_hit)
    if detail is not None:
        polyline = detail.encoded_polyline or ""
        if mode != "walking":
            fare = detail.fare_observation
    return CommuteLeg(
        from_place_id=origin.place_id,
        to_place_id=dest.place_id,
        from_name=origin.name,
        to_name=dest.name,
        distance_meters=distance,
        duration_minutes=minutes,
        mode=mode,
        source="estimate" if estimated else "amap",
        note=note,
        encoded_polyline=polyline,
        transit_steps=steps,
        transit_detail_quality=quality,
        transit_detail_cache_hit=cache_hit,
        fare_observation=fare,
    )


def _access_from_hop(leg: AccessLeg, hop: dict, place: CandidatePlace) -> AccessLeg:
    label = str(hop.get("label") or "步行")
    minutes = int(hop["minutes"])
    mode = _HOP_MODE.get(label, leg.mode)
    detail = hop.get("detail") if isinstance(hop.get("detail"), PreciseRoute) else None
    anchor = CandidatePlace(
        place_id=0,
        name="住处",
        place_type="hotel",
        latitude=leg.anchor_latitude,
        longitude=leg.anchor_longitude,
    )
    origin, dest = (anchor, place) if leg.direction == "outbound" else (place, anchor)
    estimated = hop.get("source") == "estimate"
    distance = _meters(origin, dest, None if estimated else detail)
    if estimated:
        estimate = _estimated_commute(origin, dest, label)
        if estimate is not None:
            distance = int(estimate.distance_meters)
    return leg.model_copy(update={
        "mode": mode,
        "duration_minutes": minutes,
        "duration_source": "estimate" if estimated else "amap",
        "distance_meters": distance,
        "walk_proof_receipt": None,
    })


def _pair_hops(left_id: int, right_id: int, by_pair: dict, lodge_id: int | None) -> list[dict]:
    """Hops that actually connect two itinerary places. Rest goes via lodging."""
    direct = by_pair.get((left_id, right_id))
    if isinstance((direct or {}).get("minutes"), int):
        return [direct]
    if lodge_id is not None:
        inbound = by_pair.get((left_id, lodge_id))
        outbound = by_pair.get((lodge_id, right_id))
        if isinstance((inbound or {}).get("minutes"), int) and isinstance((outbound or {}).get("minutes"), int):
            return [inbound, outbound]
    return []


def _hop_note_text(hop: dict, lodge: CandidatePlace | None) -> str:
    origin = str(hop.get("from_name") or "")
    dest = str(hop.get("to_name") or "")
    lodge_id = int(_place_id(lodge) or 0) if lodge is not None else 0
    lodge_name = str(getattr(lodge, "name", "") or "") if lodge is not None else ""
    if lodge_id and int(hop.get("from_id") or 0) == lodge_id:
        origin = "住处"
    elif lodge_name and origin == lodge_name:
        origin = "住处"
    if lodge_id and int(hop.get("to_id") or 0) == lodge_id:
        dest = "住处"
    elif lodge_name and dest == lodge_name:
        dest = "住处"
    label = str(hop.get("label") or "步行")
    minutes = int(hop["minutes"])
    if hop.get("source") == "estimate":
        return _estimate_note(origin, dest, label, minutes)
    return _hop_note(origin, dest, label, minutes, hop.get("transit_minutes"))


def _commute_from_via_hops(origin: CandidatePlace, dest: CandidatePlace, hops: list[dict], lodge: CandidatePlace | None) -> CommuteLeg:
    if len(hops) == 1:
        return _commute_from_hop(origin, dest, hops[0])
    minutes = sum(int(hop["minutes"]) for hop in hops)
    main = max(hops, key=lambda hop: int(hop["minutes"]))
    label = str(main.get("label") or "步行")
    mode = _HOP_MODE.get(label, "walking")
    estimated = any(hop.get("source") == "estimate" for hop in hops)
    distance = 0
    for hop in hops:
        detail = hop.get("detail") if isinstance(hop.get("detail"), PreciseRoute) else None
        if detail is not None:
            distance += max(0, int(detail.distance_meters))
    return CommuteLeg(
        from_place_id=origin.place_id,
        to_place_id=dest.place_id,
        from_name=origin.name,
        to_name=dest.name,
        distance_meters=distance,
        duration_minutes=minutes,
        mode=mode,
        source="estimate" if estimated else "amap",
        note="；".join(_hop_note_text(hop, lodge) for hop in hops),
        encoded_polyline="",
        transit_steps=[],
        transit_detail_quality="missing",
        transit_detail_cache_hit=False,
        fare_observation=None,
    )


def _apply_scheduled_legs(route: RoutePlan, plan: dict, lodge: CandidatePlace | None, request: TripRequest) -> None:
    """Replace verifier legs with the hops ``schedule_day`` already chose."""
    # Coordinate-only lodging is a real stop with hop ID 0.
    lodge_id = (_place_id(lodge) or 0) if lodge is not None else None
    for day, group in zip(plan.get("days") or [], route.day_groups):
        hops = [hop for hop in (day.get("_chosen_hops") or []) if isinstance(hop, dict)]
        if not hops:
            continue
        by_pair = {(int(hop["from_id"]), int(hop["to_id"])): hop for hop in hops}
        ids = [place.place_id for place in group.places]
        places = list(group.places)
        changed = False
        access_notes = []
        pairs = list(zip(ids, ids[1:]))
        hop_groups = [_pair_hops(left, right, by_pair, lodge_id) for left, right in pairs]
        if any(hop_groups):
            group.commute_legs = [
                _commute_from_via_hops(places[index], places[index + 1], group_hops, lodge)
                if group_hops else group.commute_legs[index]
                for index, group_hops in enumerate(hop_groups)
            ]
            changed = True
        if lodge is not None and ids and group.access_legs:
            outbound = by_pair.get((lodge_id, ids[0]))
            inbound = by_pair.get((ids[-1], lodge_id))
            updated = []
            for leg in group.access_legs:
                hop = outbound if leg.direction == "outbound" else inbound if leg.direction == "inbound" else None
                place = places[0] if leg.direction == "outbound" else places[-1]
                if hop is None or not isinstance(hop.get("minutes"), int):
                    updated.append(leg)
                    continue
                updated.append(_access_from_hop(leg, hop, place))
                if hop.get("transit_minutes") is not None:
                    access_notes.append(_hop_note_text(hop, lodge))
                changed = True
            group.access_legs = updated
        if changed:
            _v2_finalize_day(group, request, locked=True)
            if any(len(group_hops) > 1 for group_hops in hop_groups):
                notes = []
                for index, group_hops in enumerate(hop_groups):
                    if len(group_hops) > 1:
                        notes.extend(_hop_note_text(hop, lodge) for hop in group_hops)
                    elif group.commute_legs[index].note:
                        notes.append(group.commute_legs[index].note)
                from src.agents.route_feasibility import access_summary
                summary = access_summary(group)
                if summary:
                    notes.append(summary)
                group.commute_notes = notes
            group.commute_notes.extend(access_notes)


class CachingEstimator:
    """Cache leg minutes and share an in-flight query for the same pair."""

    def __init__(self, inner):
        self.inner = inner
        self.cache: dict[tuple, int | None] = {}
        self.facts: list[tuple] = []
        self.routes: dict[tuple, PreciseRoute] = {}
        self._fact_keys: set[tuple] = set()
        self._inflight: dict[tuple, asyncio.Task] = {}

    async def quote(self, origin, dest, mode: str) -> int | None:
        key = (_point_key(origin), _point_key(dest), mode)
        if key in self.cache:
            return self.cache[key]
        task = self._inflight.get(key)
        if task is None:
            task = asyncio.create_task(self._read(origin, dest, mode))
            self._inflight[key] = task
        try:
            value = await task
        finally:
            self._inflight.pop(key, None)
        self.cache[key] = value
        detail = _quoted_route(self.inner, origin, dest, mode)
        if detail is not None:
            self.routes[key] = detail
        if (
            isinstance(value, int)
            and value > 0
            and mode in {"walking", "transit", "driving", "cycling"}
            and key not in self._fact_keys
        ):
            self._fact_keys.add(key)
            self.facts.append((origin, dest, mode, value))
        return value

    async def _read(self, origin, dest, mode: str) -> int | None:
        try:
            value = await self.inner.quote(origin, dest, mode)
        except Exception:
            return None
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None


class AmapLegEstimator:
    """Minutes from ``AmapRouteProvider``, with five extra minutes for a taxi."""

    def __init__(self, provider, *, city: str, citycode: str | None):
        self.provider = provider
        self.city = city
        self.citycode = citycode
        self.routes: dict[tuple, PreciseRoute] = {}

    async def quote(self, origin, dest, mode: str) -> int | None:
        if mode == "taxi":
            driving = await self.quote(origin, dest, "driving")
            return None if driving is None else int(driving) + 5
        try:
            route = await self.provider.route(
                origin=_route_place(origin),
                destination=_route_place(dest),
                city=self.city,
                effective_mode=mode,
                citycode=self.citycode,
            )
        except RouteProviderError:
            return None
        if isinstance(route, PreciseRoute):
            self.routes[(_point_key(origin), _point_key(dest), mode)] = route
        return int(route.duration_minutes)


async def prefetch_long_legs(
    estimator: CachingEstimator,
    pairs: Iterable[tuple],
    commute_mode: str,
    *,
    deadline_monotonic: float | None,
) -> None:
    """Give the longest draft legs first use of the provider's existing budget."""
    long_pairs = {}
    for origin, dest in pairs:
        distance = hav_km(origin, dest)
        if distance is not None and distance > FAR_WALK_KM:
            long_pairs.setdefault((_point_key(origin), _point_key(dest)), (distance, origin, dest))

    ordered = sorted(long_pairs.values(), key=lambda leg: leg[0], reverse=True)
    remaining = None if deadline_monotonic is None else deadline_monotonic - time.monotonic()
    if remaining is not None and remaining <= 0:
        return
    quotes = asyncio.gather(*(estimator.quote(origin, dest, commute_mode) for _, origin, dest in ordered))
    try:
        await asyncio.wait_for(quotes, timeout=remaining)
    except asyncio.TimeoutError:
        # Finished quotes stay cached for daily scheduling.
        pass


class EstimatorBackedProvider:
    """Lets ``_v2_verify_traffic`` reuse a test estimator. No route_access, no database."""

    def __init__(self, estimator):
        self.estimator = estimator

    async def route(self, *, origin, destination, city, effective_mode, citycode):
        del city, citycode
        minutes = await self.estimator.quote(origin, destination, effective_mode)
        if minutes is None:
            raise RouteProviderError("no route")
        distance = hav_km(origin, destination)
        meters = 0 if distance is None else max(0, int(distance * 1000))
        return PreciseRoute(distance_meters=meters, duration_minutes=int(minutes))

    async def close(self) -> None:
        return None


def _route_place(place) -> CandidatePlace:
    if isinstance(place, CandidatePlace):
        return place
    lat, lng = _coords(place)
    return CandidatePlace(
        place_id=int(_place_id(place) or 0),
        name=str(getattr(place, "name", "") or "地点"),
        place_type=str(getattr(place, "place_type", "") or "attraction"),
        latitude=lat,
        longitude=lng,
    )


def _choose_mode(walk: int | None, alternative: int | None, label: str) -> tuple[int | None, str]:
    if walk is not None and walk <= WALK_CAP_MINUTES and (alternative is None or walk <= alternative):
        return walk, "步行"
    return alternative, label


async def resolve_legs(
    estimator,
    pairs: list[tuple],
    commute_mode: str,
    *,
    pace_mode: str = "default",
    taxi_over_minutes: int = 40,
    taxi_ratio: float = 0.5,
) -> list[tuple[int | None, str]]:
    """Start each batch of quotes with the longest straight-line legs.

    Walks within 1 km use the walking quote or the shared distance estimate.
    Beyond 1 km, cycling uses its own mode and relaxed days use their lower
    walking cap. Other days can still walk up to 35 minutes when it is faster.
    Only non-relaxed transit legs that exceed ``taxi_over_minutes`` can switch
    to a taxi, and only with a quote strictly under ``taxi_ratio`` of transit.
    """
    relaxed = pace_mode == "relaxed"
    if commute_mode == "driving":
        alt_mode, alt_label = "driving", "自驾"
        short = RELAXED_DRIVE_WALK_MINUTES if relaxed else SHORT_DRIVE_WALK_MINUTES
    elif commute_mode == "cycling":
        alt_mode, alt_label = "cycling", "骑行"
        short = 0
    else:
        alt_mode, alt_label = "transit", "公交"
        short = RELAXED_WALK_MINUTES if relaxed else SHORT_WALK_MINUTES
    async def quote(index: int, mode: str) -> int | None:
        origin, dest = pairs[index]
        return await estimator.quote(origin, dest, mode)

    results: list[tuple[int | None, str] | None] = [None] * len(pairs)
    distances = [hav_km(origin, dest) for origin, dest in pairs]
    order = sorted(range(len(pairs)), key=lambda index: distances[index] or 0.0, reverse=True)
    first: list[tuple[int, str]] = []
    walking_only: set[int] = set()
    for index in order:
        origin, dest = pairs[index]
        distance = distances[index]
        if distance == 0 and _point_key(origin) == _point_key(dest):
            results[index] = LegChoice(1, "步行", "estimate")
        elif distance is not None and distance <= ALWAYS_WALK_KM:
            first.append((index, "walking"))
            walking_only.add(index)
        elif commute_mode == "cycling" or (distance is not None and distance > FAR_WALK_KM):
            first.append((index, alt_mode))
        else:
            first.append((index, "walking"))

    quotes = await asyncio.gather(*(quote(index, mode) for index, mode in first))
    longer: list[tuple[int, int | None]] = []
    direct: dict[int, int | None] = {}
    for (index, mode), minutes in zip(first, quotes):
        if mode == alt_mode:
            longer.append((index, None))
            direct[index] = minutes
            continue
        walk = int(minutes) if minutes is not None and minutes > 0 else None
        if index in walking_only or (walk is not None and walk <= short):
            results[index] = (walk, "步行")
        else:
            longer.append((index, walk))

    if longer:
        pending = [index for index, _walk in longer if index not in direct]
        alternatives = direct | dict(zip(pending, await asyncio.gather(
            *(quote(index, alt_mode) for index in pending)
        )))
        taxis: list[tuple[int, int | None, int]] = []
        for index, walk in longer:
            alternative = alternatives[index]
            alt_value = None if alternative is None else int(alternative)
            if relaxed or alt_mode == "cycling":
                results[index] = (alt_value, alt_label)
            elif alt_mode == "transit" and alt_value is not None and alt_value > taxi_over_minutes:
                taxis.append((index, walk, alt_value))
            else:
                results[index] = _choose_mode(walk, alt_value, alt_label)
        if taxis:
            taxi_minutes = await asyncio.gather(
                *(quote(index, "taxi") for index, _walk, _transit in taxis)
            )
            for (index, walk, transit), alternative in zip(taxis, taxi_minutes):
                taxi_value = None if alternative is None else int(alternative)
                if taxi_value is None or taxi_value >= transit * taxi_ratio:
                    results[index] = _choose_mode(walk, transit, alt_label)
                else:
                    minutes, label = _choose_mode(walk, taxi_value, "打车")
                    results[index] = LegChoice(minutes, label, transit_minutes=transit if label == "打车" else None)
    chosen: list[tuple[int | None, str]] = []
    for index, item in enumerate(results):
        if item is None:
            item = (None, "步行")
        minutes, label = item
        if minutes is None:
            # A missed Amap quote (budget, no route, request error) keeps the
            # old scheduler's distance estimate and is not a missing road.
            estimated = _fallback_minutes(pairs[index][0], pairs[index][1], label)
            if estimated is not None:
                chosen.append(LegChoice(estimated, label, "estimate"))
                continue
        source = getattr(item, "source", "amap")
        chosen.append(item if isinstance(item, LegChoice) else LegChoice(minutes, label, source))
    return chosen


def quoted_known_legs(estimator) -> dict[tuple[int, int, str], CommuteLeg]:
    """Precise facts already paid for, in the ``_lookup_known_commute_leg`` key shape."""
    known: dict[tuple[int, int, str], CommuteLeg] = {}
    for origin, dest, mode, minutes in getattr(estimator, "facts", ()):
        left, right = _route_place(origin), _route_place(dest)
        if left.place_id <= 0 or right.place_id <= 0 or mode not in {"walking", "transit", "driving", "cycling"}:
            continue
        distance = hav_km(left, right)
        meters = 0 if distance is None else max(0, int(round(distance * 1000)))
        known[(left.place_id, right.place_id, mode)] = CommuteLeg(
            from_place_id=left.place_id,
            to_place_id=right.place_id,
            from_name=left.name,
            to_name=right.name,
            distance_meters=meters,
            duration_minutes=int(minutes),
            mode=mode,
            source="amap",
        )
    return known


def quoted_known_access(estimator, lodge: CandidatePlace | None, lodging_source: str, generation_mode: str) -> dict:
    """Lodging round trips already quoted in the generation mode, keyed for ``_lookup_known_access``."""
    if (
        lodge is None
        or _coords(lodge)[0] is None
        or generation_mode not in {"walking", "transit", "driving", "cycling"}
    ):
        return {}
    source = "user_specified" if lodging_source == "user" else "auto_recommended"
    known = {}
    lodge_id = int(_place_id(lodge) or 0)
    for origin, dest, mode, minutes in getattr(estimator, "facts", ()):
        if mode != generation_mode:
            continue
        left, right = _route_place(origin), _route_place(dest)
        if left.place_id == lodge_id and right.place_id > 0:
            direction, place = "outbound", right
        elif right.place_id == lodge_id and left.place_id > 0:
            direction, place = "inbound", left
        else:
            continue
        distance = hav_km(left, right)
        meters = 0 if distance is None else max(0, int(round(distance * 1000)))
        leg = AccessLeg(
            direction=direction,
            anchor_source=source,
            anchor_latitude=float(lodge.latitude),
            anchor_longitude=float(lodge.longitude),
            place_id=place.place_id,
            mode=generation_mode,
            duration_source="amap",
            duration_minutes=int(minutes),
            distance_meters=meters,
        )
        known[_v2_access_key(leg, place)] = leg
    return known
