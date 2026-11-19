"""Deterministic accommodation-anchor resolution for v0.9.1."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text

from src.agents import route_planning
from src.agents.schema import (
    AccommodationSuggestion,
    CandidatePlace,
    RetrievalResult,
    TripRequest,
)
from src.pipeline.poi_resolve import (
    AmapClient,
    AmapDefinitiveNotFound,
    AmapTransientError,
    GeocodeResult,
)

logger = logging.getLogger(__name__)

COORDINATE_ONLY_ACCOMMODATION_NAME = "用户指定住宿位置"
_MATCHABLE_PLACE_TYPES = frozenset({
    "accommodation_area",
    "business_area",
    "hotel",
})
_PLACE_TYPE_PRIORITY = {
    "accommodation_area": 3,
    "business_area": 2,
    "hotel": 1,
}

PLACE_BY_ID_SQL = """
    SELECT
        place_id, canonical_name, city, latitude, longitude, place_type,
        base_priority
    FROM travel_canonical_place
    WHERE place_id = :place_id
      AND city = :city
      AND is_active = TRUE
      AND latitude IS NOT NULL
      AND longitude IS NOT NULL
    LIMIT 1
"""

NAME_CANDIDATES_SQL = """
    SELECT
        place_id, canonical_name, city, latitude, longitude, place_type,
        base_priority
    FROM travel_canonical_place
    WHERE city = :city
      AND is_active = TRUE
      AND latitude IS NOT NULL
      AND longitude IS NOT NULL
      AND place_type IN (
          'accommodation_area',
          'business_area',
          'hotel'
      )
    ORDER BY base_priority DESC, place_id ASC
"""

CANDIDATE_AREAS_SQL = """
    SELECT
        place_id, canonical_name, city, latitude, longitude, place_type,
        base_priority
    FROM travel_canonical_place
    WHERE city = :city
      AND place_type = 'accommodation_area'
      AND contextual_only = TRUE
      AND is_active = TRUE
      AND latitude IS NOT NULL
      AND longitude IS NOT NULL
    ORDER BY base_priority DESC, place_id ASC
"""


@dataclass(frozen=True)
class AccommodationPlace:
    """Small canonical-place projection used by the resolver."""

    place_id: int
    canonical_name: str
    city: str
    latitude: float
    longitude: float
    place_type: str
    base_priority: int = 0


def _place_from_row(row: Any) -> AccommodationPlace:
    mapping = row if isinstance(row, Mapping) else row._mapping
    return AccommodationPlace(
        place_id=int(mapping["place_id"]),
        canonical_name=str(mapping["canonical_name"]),
        city=str(mapping["city"]),
        latitude=float(mapping["latitude"]),
        longitude=float(mapping["longitude"]),
        place_type=str(mapping["place_type"]),
        base_priority=int(mapping.get("base_priority") or 0),
    )


async def _fetch_place_by_id(
    session: Any,
    *,
    place_id: int,
    city: str,
) -> AccommodationPlace | None:
    result = await session.execute(
        text(PLACE_BY_ID_SQL),
        {"place_id": place_id, "city": city},
    )
    row = result.mappings().first()
    return _place_from_row(row) if row is not None else None


async def _fetch_name_candidates(
    session: Any,
    *,
    city: str,
) -> list[AccommodationPlace]:
    result = await session.execute(text(NAME_CANDIDATES_SQL), {"city": city})
    return [_place_from_row(row) for row in result.mappings().all()]


async def _fetch_candidate_areas(
    session: Any,
    *,
    city: str,
) -> list[AccommodationPlace]:
    result = await session.execute(text(CANDIDATE_AREAS_SQL), {"city": city})
    return [_place_from_row(row) for row in result.mappings().all()]


def _compact_name(value: str) -> str:
    return "".join(value.split())


def _name_match_key(value: str) -> str:
    """Normalize only frozen accommodation-area suffixes for fuzzy matching."""

    key = _compact_name(value)
    for suffix in ("附近", "周边"):
        if key.endswith(suffix):
            key = key[:-len(suffix)]
            break
    for suffix in ("(商圈)", "（商圈）", "商圈"):
        if key.endswith(suffix):
            key = key[:-len(suffix)]
            break
    return key


def _ranked_name_match(
    input_name: str,
    candidates: list[AccommodationPlace],
) -> AccommodationPlace | None:
    """Apply the frozen exact-then-containment matching contract."""

    original = input_name.strip()
    compact_input = _compact_name(original)
    if not compact_input:
        return None
    input_match_key = _name_match_key(original)

    eligible = [
        candidate
        for candidate in candidates
        if candidate.place_type in _MATCHABLE_PLACE_TYPES
    ]
    exact = [
        candidate
        for candidate in eligible
        if (
            candidate.canonical_name == original
            or _compact_name(candidate.canonical_name) == compact_input
        )
    ]
    if exact:
        return _pick_name_match(original, exact, log_ambiguity=False)

    contained = [
        candidate
        for candidate in eligible
        if (
            input_match_key
            and (candidate_key := _name_match_key(candidate.canonical_name))
            and (
                input_match_key in candidate_key
                or candidate_key in input_match_key
            )
        )
    ]
    if not contained:
        return None
    return _pick_name_match(original, contained, log_ambiguity=True)


def _pick_name_match(
    input_name: str,
    candidates: list[AccommodationPlace],
    *,
    log_ambiguity: bool,
) -> AccommodationPlace:
    best_type_priority = max(
        _PLACE_TYPE_PRIORITY[candidate.place_type]
        for candidate in candidates
    )
    same_priority = [
        candidate
        for candidate in candidates
        if _PLACE_TYPE_PRIORITY[candidate.place_type] == best_type_priority
    ]
    ranked = sorted(
        same_priority,
        key=lambda candidate: (-candidate.base_priority, candidate.place_id),
    )
    picked = ranked[0]
    if log_ambiguity and len(ranked) > 1:
        logger.info(
            "accommodation name match ambiguous input=%s picked=%s candidates=%s",
            input_name,
            picked.canonical_name,
            [candidate.canonical_name for candidate in ranked],
        )
    return picked


async def _geocode_name(name: str, city: str) -> GeocodeResult | None:
    client = AmapClient()
    try:
        return await client.geocode(city=city, address=name)
    except (AmapDefinitiveNotFound, AmapTransientError) as exc:
        logger.info(
            "accommodation geocode failed city=%s name=%s error=%s",
            city,
            name,
            type(exc).__name__,
        )
        return None
    finally:
        await client.close()


def _suggestion_from_place(
    place: AccommodationPlace,
    *,
    source: str,
    reason: str = "",
    user_input_unmatched: str = "",
) -> AccommodationSuggestion:
    return AccommodationSuggestion(
        name=place.canonical_name,
        latitude=place.latitude,
        longitude=place.longitude,
        source=source,
        reason=reason,
        user_input_unmatched=user_input_unmatched,
    )


def _urban_candidate_pool(
    trip_request: TripRequest,
    retrieval: RetrievalResult,
) -> list[CandidatePlace]:
    pool = (
        retrieval.route_planning_candidates
        or retrieval.candidates
    )
    urban, _ = route_planning._split_remote_day_trips(
        list(pool),
        days=trip_request.days,
        remote_context=" ".join([
            trip_request.to_city,
            *trip_request.preferences,
            trip_request.notes,
        ]),
    )
    return [
        place
        for place in urban
        if place.latitude is not None and place.longitude is not None
    ]


def _proximity_scoring(
    trip_request: TripRequest,
    retrieval: RetrievalResult,
    candidate_areas: list[AccommodationPlace],
) -> AccommodationPlace | None:
    """Pick the area with the shortest mean distance to top urban POIs."""

    if not candidate_areas:
        return None
    pool = _urban_candidate_pool(trip_request, retrieval)
    top_pois = sorted(
        pool,
        key=lambda place: place.effective_score,
        reverse=True,
    )[:20]
    if not top_pois:
        return None

    def mean_distance(area: AccommodationPlace) -> float:
        distances = [
            route_planning._haversine_coords(
                area.latitude,
                area.longitude,
                float(place.latitude),
                float(place.longitude),
            )
            for place in top_pois
        ]
        return sum(distances) / len(distances)

    return min(
        candidate_areas,
        key=lambda area: (
            mean_distance(area),
            -area.base_priority,
            area.place_id,
        ),
    )


async def resolve_accommodation(
    trip_request: TripRequest,
    retrieval: RetrievalResult,
    session: Any,
) -> AccommodationSuggestion | None:
    """Resolve a user anchor or recommend the closest accommodation area."""

    request = trip_request.accommodation
    city = trip_request.to_city.strip()
    unmatched = ""

    if request is not None and request.place_id is not None:
        place = await _fetch_place_by_id(
            session,
            place_id=request.place_id,
            city=city,
        )
        if place is not None:
            return _suggestion_from_place(place, source="user_specified")
        logger.info(
            "accommodation place_id unmatched city=%s place_id=%s",
            city,
            request.place_id,
        )
    elif (
        request is not None
        and request.latitude is not None
        and request.longitude is not None
    ):
        name = (request.name or "").strip() or COORDINATE_ONLY_ACCOMMODATION_NAME
        return AccommodationSuggestion(
            name=name,
            latitude=request.latitude,
            longitude=request.longitude,
            source="user_specified",
        )
    elif request is not None and (request.name or "").strip():
        input_name = str(request.name).strip()
        candidates = await _fetch_name_candidates(session, city=city)
        matched = _ranked_name_match(input_name, candidates)
        if matched is not None:
            return _suggestion_from_place(matched, source="user_specified")

        geocoded = await _geocode_name(input_name, city)
        if geocoded is not None:
            return AccommodationSuggestion(
                name=input_name,
                latitude=geocoded.latitude,
                longitude=geocoded.longitude,
                source="user_specified",
            )
        unmatched = input_name

    candidate_areas = await _fetch_candidate_areas(session, city=city)
    best_area = _proximity_scoring(
        trip_request,
        retrieval,
        candidate_areas,
    )
    if best_area is None:
        return None
    # No reason is claimed: proximity scoring does not verify walking access
    # (area-level recommendation reasons belong to v0.14).
    return _suggestion_from_place(
        best_area,
        source="auto_recommended",
        user_input_unmatched=unmatched,
    )


async def resolve_accommodation_context(trip_request, retrieval, session, *, coupling_enabled=True):
    """Coupled path: failed explicit input never enters automatic recommendation."""
    from src.agents.schema import AccommodationAnchor, AccommodationPlanningContext
    from src.agents.accommodation_policy import urban_core, distance_km
    from src.agents.route_feasibility import valid_coordinate
    city = trip_request.to_city.strip()
    statement = CANDIDATE_AREAS_SQL.replace(
        "AND is_active = TRUE", "AND is_active = TRUE AND trust_level = 'trusted' AND review_status IN ('reviewed', 'auto_accepted')")
    rows = await session.execute(text(statement), {"city": city})
    areas = [_place_from_row(row) for row in rows.fetchall()]
    areas = [a for a in areas if a.city == city and valid_coordinate(a.latitude,a.longitude)]
    seen = set()
    unique_areas = []
    for area in areas:
        coordinate = (area.latitude, area.longitude)
        if coordinate not in seen:
            seen.add(coordinate)
            unique_areas.append(area)
    areas = unique_areas
    anchors = [AccommodationAnchor(suggestion=_suggestion_from_place(a,source="auto_recommended"),
               place_id=a.place_id,location_precision="area") for a in areas]
    core = urban_core(anchors)
    if not coupling_enabled:
        suggestion = await resolve_accommodation(trip_request, retrieval, session)
        locked = [AccommodationAnchor(suggestion=suggestion, location_precision="point" if trip_request.accommodation else "area")] if suggestion else []
        return AccommodationPlanningContext(state="fixed" if locked else "auto_unavailable", anchors=locked, urban_core=core)
    request = trip_request.accommodation
    explicit = request is not None and (bool((request.name or "").strip()) or
        request.place_id is not None or request.latitude is not None or request.longitude is not None)
    if not explicit:
        old = _proximity_scoring(trip_request,retrieval,areas)
        anchors.sort(key=lambda a:(a.place_id != (old.place_id if old else None), a.place_id or 0))
        return AccommodationPlanningContext(state="auto_candidates" if anchors else "auto_unavailable",
                                             anchors=anchors,urban_core=core)
    def unresolved(code):
        return AccommodationPlanningContext(state="user_unresolved",urban_core=core,reason_code=code)
    has_coordinates = request.latitude is not None or request.longitude is not None
    if has_coordinates and not valid_coordinate(request.latitude,request.longitude):
        return unresolved("invalid_coordinates")
    anchor = None
    if request.place_id is not None:
        p = await _fetch_place_by_id(session,place_id=request.place_id,city=city)
        if p is None or p.city != city or p.place_type not in _MATCHABLE_PLACE_TYPES or not valid_coordinate(p.latitude,p.longitude):
            return unresolved("place_id_unresolved")
        if has_coordinates and distance_km((p.latitude,p.longitude),(request.latitude,request.longitude)) > .1:
            return unresolved("conflicting_coordinates")
        if (request.name or "").strip() and _name_match_key(request.name) != _name_match_key(p.canonical_name):
            return unresolved("conflicting_name")
        anchor = AccommodationAnchor(suggestion=_suggestion_from_place(p,source="user_specified"),
                    place_id=p.place_id,location_precision="point" if p.place_type=="hotel" else "area")
    elif has_coordinates:
        anchor = AccommodationAnchor(suggestion=AccommodationSuggestion(
            name=(request.name or "").strip() or COORDINATE_ONLY_ACCOMMODATION_NAME,
            latitude=request.latitude,longitude=request.longitude,source="user_specified"),location_precision="point")
    else:
        name = request.name.strip()
        candidates = await _fetch_name_candidates(session,city=city)
        eligible = [p for p in candidates if p.city == city
                    and p.place_type in _MATCHABLE_PLACE_TYPES
                    and valid_coordinate(p.latitude, p.longitude)]
        # Keep an explicit full name distinct from a suffix-normalized alias.
        exact = [p for p in eligible
                 if _compact_name(p.canonical_name) == _compact_name(name)]
        if not exact:
            exact = [p for p in eligible
                     if _name_match_key(p.canonical_name) == _name_match_key(name)]
        if len(exact)>1:
            return unresolved("ambiguous_name")
        if exact:
            p=exact[0]
            anchor = AccommodationAnchor(suggestion=_suggestion_from_place(p,source="user_specified"),
                place_id=p.place_id,location_precision="point" if p.place_type=="hotel" else "area")
        else:
            geo = await _geocode_name(name,city)
            if geo is None or not valid_coordinate(geo.latitude,geo.longitude):
                return unresolved("name_unresolved")
            if geo.match_count != 1 or (geo.city or geo.province or "").removesuffix("市") != city.removesuffix("市"):
                return unresolved("ambiguous_or_wrong_city")
            # City/district centroids are not successful hotel/address resolution.
            if geo.level not in {"门牌号","兴趣点","道路","道路交叉口","村庄","乡镇","热点商圈"}:
                return unresolved("insufficient_location_precision")
            anchor = AccommodationAnchor(suggestion=AccommodationSuggestion(name=name,latitude=geo.latitude,
                longitude=geo.longitude,source="user_specified"),
                location_precision="point" if geo.level in {"门牌号","兴趣点"} else "area")
    return AccommodationPlanningContext(state="fixed",anchors=[anchor],urban_core=core)
