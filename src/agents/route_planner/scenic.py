"""Confirmed Amap containment for the model-route scheduler."""

import logging
from collections import Counter, defaultdict

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.agents.composition_blueprint import is_light_food_place
from src.agents.preferences import candidate_is_night_place, candidate_is_tea_place
from src.agents.route_feasibility import build_visit_profile, valid_coordinate
from src.agents.route_planner.places import _closed, hav_km
from src.agents.route_planner.schedule import _stay
from src.agents.schema import CandidatePlace, RouteDayGroup

logger = logging.getLogger(__name__)

AMAP_PART_OF_PREFIX = "amap:part_of:"
SCENIC_MERGE_RADIUS_KM = 1.5
PART_OF_EXCLUDED_TYPES = frozenset({
    "restaurant", "food", "cafe", "snack", "hotel", "accommodation_area",
    "tea", "snack_shop", "dessert", "bakery", "milk_tea",
})

_READ_PART_OF = text("""
    SELECT child.place_id AS child_id, parent.place_id AS parent_id
    FROM travel_place_alias AS relation
    JOIN travel_canonical_place AS parent ON parent.place_id = relation.canonical_place_id
    JOIN travel_canonical_place AS child
      ON child.amap_poi_id = split_part(relation.normalized_alias, ':', 3)
     AND child.city = parent.city
    WHERE child.place_id = ANY(CAST(:place_ids AS bigint[]))
      AND relation.relation_type = 'part_of' AND relation.status = 'confirmed'
      AND relation.normalized_alias = :prefix || child.amap_poi_id || ':' || parent.amap_poi_id
      AND child.is_active AND parent.is_active AND child.place_id <> parent.place_id
      AND child.trust_level <> 'rejected' AND parent.trust_level <> 'rejected'
      AND child.review_status <> 'rejected' AND parent.review_status <> 'rejected'
""")


def _canonical_id(place: CandidatePlace) -> int:
    return place.canonical_place_id or place.place_id


def _scenic_stop(stop: dict) -> bool:
    place = stop.get("_place")
    kind = stop.get("kind")
    return (
        kind in {"sight", "night"}
        and isinstance(place, CandidatePlace)
        and place.place_type not in PART_OF_EXCLUDED_TYPES
        and not (is_light_food_place(place) or candidate_is_tea_place(place) or _closed(place))
        and (kind != "night" or candidate_is_night_place(place))
    )


def within_scenic_merge_radius(child: CandidatePlace, parent: CandidatePlace) -> bool:
    if not all(valid_coordinate(place.latitude, place.longitude) for place in (child, parent)):
        return False
    distance = hav_km(child, parent)
    return distance is not None and distance <= SCENIC_MERGE_RADIUS_KM


async def load_scenic_parents(session, place_ids: set[int]) -> dict[int, int]:
    if session is None or not place_ids:
        return {}
    params = {"place_ids": sorted(place_ids), "prefix": AMAP_PART_OF_PREFIX}
    try:
        if isinstance(session, AsyncSession):
            async with session.begin_nested():
                result = await session.execute(_READ_PART_OF, params)
        else:
            result = await session.execute(_READ_PART_OF, params)
        parents = defaultdict(set)
        for child_id, parent_id in result.all():
            parents[child_id].add(parent_id)
        return {child_id: next(iter(found)) for child_id, found in parents.items() if len(found) == 1}
    except Exception as exc:
        logger.warning("scenic_part_of_lookup_failed kind=%s", type(exc).__name__)
        return {}


def _selected_root(place_id: int, parents: dict[int, int], indexes: dict[int, int]) -> int:
    ancestor = place_id
    seen = set()
    while parents.get(ancestor) in indexes:
        if ancestor in seen:
            return place_id
        seen.add(ancestor)
        ancestor = parents[ancestor]
    return ancestor


def _merge_day(day: dict, parents: dict[int, int]) -> list[str]:
    stops = day.get("stops") or []
    counts = Counter(_canonical_id(stop["_place"]) for stop in stops if stop.get("_place") is not None)
    indexes = {
        _canonical_id(stop["_place"]): index
        for index, stop in enumerate(stops)
        if _scenic_stop(stop) and counts[_canonical_id(stop["_place"])] == 1
    }
    places = {place_id: stops[index]["_place"] for place_id, index in indexes.items()}
    nearby_parents = {
        child: parent for child, parent in parents.items()
        if child in places and parent in places and within_scenic_merge_radius(places[child], places[parent])
    }
    groups = defaultdict(list)
    for place_id, index in indexes.items():
        root = _selected_root(place_id, nearby_parents, indexes)
        if root != place_id and not within_scenic_merge_radius(places[place_id], places[root]):
            root = place_id
        groups[root].append(index)

    replacements = {}
    removed = set()
    notes = []
    for root, member_indexes in groups.items():
        if len(member_indexes) < 2:
            continue
        root_index = indexes[root]
        original = tuple(stops[index] for index in member_indexes)
        night_index = max((index for index in member_indexes if stops[index]["kind"] == "night"), default=None)
        anchor = root_index if night_index is None else night_index
        merged = {
            **stops[root_index],
            "kind": stops[anchor]["kind"],
            "stay_minutes": _stay({"stay_minutes": sum(_stay(stop) for stop in original)}),
            "optional": all(stop.get("optional") is True for stop in original),
            "_scenic_stops": original,
        }
        replacements[anchor] = merged
        removed.update(member_indexes)
        notes.append(f"第{day.get('day')}天{scenic_visit_note(merged)}")
    if replacements:
        day["stops"] = [
            replacements.get(index, stop) for index, stop in enumerate(stops)
            if index not in removed or index in replacements
        ]
    return notes


async def merge_scenic_stops(plan: dict, session) -> list[str]:
    days = plan.get("days") or []
    place_ids = set()
    for day in days:
        eligible = {_canonical_id(stop["_place"]) for stop in day.get("stops") or [] if _scenic_stop(stop)}
        if len(eligible) > 1:
            place_ids.update(eligible)
    parents = await load_scenic_parents(session, place_ids)
    return [note for day in days for note in _merge_day(day, parents)] if parents else []


def included_scenic_places(stop: dict) -> tuple[CandidatePlace, ...]:
    return tuple(member["_place"] for member in stop.get("_scenic_stops", ())
                 if member["place_id"] != stop["place_id"])


def scenic_visit_note(stop: dict) -> str:
    names = "、".join(place.name for place in included_scenic_places(stop))
    return f"「{stop['_place'].name}」约{_stay(stop)}分钟，含{names}（同站游览，不另排交通）"


def record_scenic_visit(group: RouteDayGroup, stop: dict) -> None:
    members = included_scenic_places(stop)
    if not members:
        return
    parent = stop["_place"]
    group.time_hints.append(scenic_visit_note(stop))
    profile = next((item for item in group.visit_profiles if item.place_id == parent.place_id), None)
    if profile is None:
        profile = build_visit_profile(parent)
        group.visit_profiles.append(profile)
    member_ids = [place.place_id for place in members]
    profile.nested_place_ids = list(dict.fromkeys([*profile.nested_place_ids, *member_ids]))
    profile.contained_place_ids = list(dict.fromkeys([*profile.contained_place_ids, *member_ids]))
