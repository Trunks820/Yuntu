"""Place geometry, candidate shaping and area clusters for the route planner."""

from __future__ import annotations

import math
import re

from dataclasses import (
    dataclass,
    field,
)

from src.agents.activity_arrangement import resolve_profiles

from src.agents.composition_blueprint import is_light_food_place

from src.agents.preferences import (
    candidate_is_family_place,
    candidate_is_night_place,
    candidate_is_tea_place,
)

from src.agents.route_feasibility import build_visit_profile

from src.agents.schema import (
    CandidatePlace,
    TripRequest,
)

from src.agents.route_planner.rules import (
    CLUSTER_RADIUS_KM,
    SCENIC_NEAR_KM,
    AREA_TYPES,
    CLOSED_NAME,
    PlannerRules,
)



@dataclass
class AreaCluster:
    index: int
    seed: CandidatePlace
    members: list[CandidatePlace]
    neighbors: list[tuple[int, float]] = field(default_factory=list)


def hav_km(left, right) -> float | None:
    lat1, lng1 = _coords(left)
    lat2, lng2 = _coords(right)
    if None in (lat1, lng1, lat2, lng2):
        return None
    lat1, lng1, lat2, lng2 = map(math.radians, (lat1, lng1, lat2, lng2))
    hav = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lng2 - lng1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(min(1.0, hav)))


def _coords(place) -> tuple[float | None, float | None]:
    if place is None:
        return None, None
    if isinstance(place, dict):
        lat = place.get("latitude", place.get("lat"))
        lng = place.get("longitude", place.get("lng"))
    else:
        lat = getattr(place, "latitude", None)
        lng = getattr(place, "longitude", None)
    if lat is None or lng is None:
        return None, None
    return float(lat), float(lng)


def _place_id(place) -> int | None:
    if place is None:
        return None
    if isinstance(place, dict):
        value = place.get("place_id")
    else:
        value = getattr(place, "place_id", None)
    return value if isinstance(value, int) and value > 0 else None


def _point_key(place) -> tuple:
    place_id = _place_id(place)
    if place_id is not None:
        return ("id", place_id)
    lat, lng = _coords(place)
    if lat is None:
        return ("obj", id(place))
    return ("xy", round(lat, 5), round(lng, 5))


def coerce_place_id(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    text = str(value).strip()
    if not text or text.lower() == "null":
        return None
    if text.isdigit():
        return int(text)
    return text


def _names_contain(left: str, right: str) -> bool:
    a = re.sub(r"\s+", "", left or "")
    b = re.sub(r"\s+", "", right or "")
    if len(a) < 2 or len(b) < 2:
        return False
    return a in b or b in a


def _closed(place: CandidatePlace | None) -> bool:
    return bool(place and CLOSED_NAME.search(place.name or ""))


def _scenic_eligible(place: CandidatePlace) -> bool:
    if place.place_type in AREA_TYPES | {"restaurant", "food", "cafe", "snack", "tea"}:
        return False
    if is_light_food_place(place) or candidate_is_tea_place(place):
        return False
    return True


def _rating(place: CandidatePlace) -> float:
    raw = getattr(place, "amap_rating", None)
    try:
        if raw is None or str(raw).strip() in {"", "无"}:
            return -1.0
        return float(raw)
    except (TypeError, ValueError):
        return -1.0


def _money(value) -> str:
    text = str(value or "").strip()
    if not text:
        return "无"
    whole, dot, frac = text.partition(".")
    if dot and set(frac) <= {"0"}:
        return whole or "无"
    return text


def _dict_items(value) -> list[dict]:
    if isinstance(value, tuple):
        value = list(value)
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict) and item]


def _as_candidate(item) -> CandidatePlace:
    if isinstance(item, CandidatePlace):
        return item
    reasons = _dict_items(getattr(item, "top_reasons", None))
    place_id = int(item.place_id)
    return CandidatePlace(
        place_id=place_id,
        canonical_place_id=place_id if reasons else None,
        name=str(item.name),
        place_type=str(getattr(item, "place_type", "") or "attraction"),
        latitude=float(item.latitude) if item.latitude is not None else None,
        longitude=float(item.longitude) if item.longitude is not None else None,
        district=getattr(item, "district", None),
        amap_poi_id=getattr(item, "amap_poi_id", None),
        amap_rating=None if getattr(item, "amap_rating", None) is None else str(item.amap_rating),
        amap_avg_price=None if getattr(item, "amap_avg_price", None) is None else str(item.amap_avg_price),
        category_tags=list(getattr(item, "category_tags", ()) or []),
        mention_count=int(getattr(item, "mention_count", 0) or 0),
        positive_count=int(getattr(item, "positive_count", 0) or 0),
        negative_count=int(getattr(item, "negative_count", 0) or 0),
        source_count=int(getattr(item, "source_count", 0) or 0),
        quality_score=float(getattr(item, "quality_score", 0) or 0),
        recommend_score=float(getattr(item, "recommend_score", 0) or 0),
        top_reasons=reasons,
        warnings=_dict_items(getattr(item, "warnings", None)),
    )


# Nearby rows without summary text stay rating-only, the same as before.
_BLANK_NEARBY_EVIDENCE = {
    "canonical_place_id": None,
    "mention_count": 0,
    "positive_count": 0,
    "negative_count": 0,
    "source_count": 0,
    "quality_score": 0.0,
    "recommend_score": 0.0,
    "top_reasons": [],
    "warnings": [],
}


def _center(places: list[CandidatePlace]) -> tuple[float, float] | None:
    located = [place for place in places if _coords(place)[0] is not None]
    if not located:
        return None
    return (
        sum(float(place.latitude) for place in located) / len(located),
        sum(float(place.longitude) for place in located) / len(located),
    )


def cluster_places(places: list[CandidatePlace], radius_km: float = CLUSTER_RADIUS_KM) -> list[AreaCluster]:
    """Greedy 1 km areas. The center is the most-mentioned place, then the smaller id."""
    rest = sorted(places, key=lambda place: (-(place.mention_count or 0), place.place_id))
    groups: list[AreaCluster] = []
    while rest:
        seed = rest[0]
        members = [
            place
            for place in rest
            if place.place_id == seed.place_id
            or ((distance := hav_km(seed, place)) is not None and distance <= radius_km)
        ]
        groups.append(AreaCluster(index=len(groups) + 1, seed=seed, members=members))
        member_ids = {place.place_id for place in members}
        rest = [place for place in rest if place.place_id not in member_ids]
    for cluster in groups:
        ranked = []
        for other in groups:
            if other.index == cluster.index:
                continue
            distance = hav_km(cluster.seed, other.seed)
            if distance is None:
                continue
            ranked.append((distance, other.index))
        ranked.sort()
        cluster.neighbors = [(index, distance) for distance, index in ranked[:3]]
    return groups


def scenic_groups(places: list[CandidatePlace], request: TripRequest) -> dict[int, set[int]]:
    """Same-scenic-area ids. Nested profiles win; otherwise 300 m and containing names."""
    eligible = [place for place in places if _scenic_eligible(place)]
    if not eligible:
        return {}
    parent = {place.place_id: place.place_id for place in eligible}

    def find(place_id: int) -> int:
        while parent[place_id] != place_id:
            parent[place_id] = parent[parent[place_id]]
            place_id = parent[place_id]
        return place_id

    def union(left: int, right: int) -> None:
        if left not in parent or right not in parent:
            return
        left, right = find(left), find(right)
        if left != right:
            parent[right] = left

    try:
        profiles, _violations = resolve_profiles(
            eligible,
            [build_visit_profile(place) for place in eligible],
            request,
            [],
            [],
        )
    except Exception:
        profiles = []
    for profile in profiles:
        for other in [*profile.nested_place_ids, *profile.contained_place_ids]:
            union(profile.place_id, other)
    for index, left in enumerate(eligible):
        for right in eligible[index + 1 :]:
            distance = hav_km(left, right)
            if distance is not None and distance <= SCENIC_NEAR_KM and _names_contain(left.name, right.name):
                union(left.place_id, right.place_id)
    groups: dict[int, set[int]] = {}
    for place in eligible:
        groups.setdefault(find(place.place_id), set()).add(place.place_id)
    return {place_id: groups[find(place_id)] for place_id in parent}


def _protected_ids(places: list[CandidatePlace], rules: PlannerRules) -> set[int]:
    protected = set(rules.must_ids)
    for place in places:
        if rules.family and candidate_is_family_place(place):
            protected.add(place.place_id)
        if rules.night and candidate_is_night_place(place):
            protected.add(place.place_id)
        if rules.tea and candidate_is_tea_place(place):
            protected.add(place.place_id)
    return protected


