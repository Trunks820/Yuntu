"""Read v0.14 area restaurants, kid venues and tea houses from the database.

Package C calls these functions while scheduling. They only run SQL. They do
not call an LLM or Amap. The offline script
``scripts/bootstrap_area_food_v014.py`` is what talks to Amap and inserts rows.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text

from src.agents.data_retrieval import (
    _TOP_REASONS_SELECT_SQL,
    _json_list,
    resolve_top_reasons_for_candidate,
)

AREA_FOOD_BATCH_ID = "area-food-v0.14"
KID_FILL_BATCH_ID = "kid-fill-v0.14"
TEA_FILL_BATCH_ID = "tea-fill-v0.14"
AREA_FOOD_BATCH_IDS = frozenset(
    {AREA_FOOD_BATCH_ID, KID_FILL_BATCH_ID, TEA_FILL_BATCH_ID}
)

# Shared with the offline script's restaurant keep-set so a stored row is one
# the reader can still return. Matched case-insensitively.
LIGHT_FOOD_NAME_PATTERN = (
    "咖啡|烘焙|面包|甜品|蛋糕|三明治|奶茶|茶饮|糖水|"
    "cafe|coffee|bakery|cake|sandwich"
)
KID_NAME_PATTERN = (
    "动物园|乐园|欢乐谷|科技馆|科学馆|海洋|水族|海底世界|"
    "儿童|熊猫|游乐|自然博物馆"
)
# Same words as package B candidate_is_tea_place. A bare 茶 is not a tea house.
TEA_HOUSE_NAME_PHRASES = ("茶馆", "茶室", "茶楼", "茶艺", "茶社", "茶舍")
TEA_NAME_PATTERN = "|".join(TEA_HOUSE_NAME_PHRASES)
# Name or tag matches skip food and stay types. The kid-fill batch does not.
KID_EXCLUDED_PLACE_TYPES = (
    "restaurant",
    "food",
    "snack",
    "cafe",
    "coffee",
    "dessert",
    "bakery",
    "milk_tea",
    "tea",
    "market",
    "hotel",
    "accommodation_area",
    "business_area",
)
_KID_EXCLUDED_PLACE_TYPE_SQL = ", ".join(
    f"'{place_type}'" for place_type in KID_EXCLUDED_PLACE_TYPES
)

_LIGHT_FOOD_NAME = re.compile(LIGHT_FOOD_NAME_PATTERN, re.IGNORECASE)
_EARTH_RADIUS_METERS = 6_371_000.0
_METERS_PER_DEGREE_LAT = 111_320.0


@dataclass(frozen=True)
class AreaCandidate:
    """Fields Package C needs to build a route candidate."""

    place_id: int
    name: str
    place_type: str
    latitude: float
    longitude: float
    district: str | None
    amap_poi_id: str | None
    amap_rating: str | None
    amap_avg_price: str | None
    category_tags: tuple[str, ...]
    top_reasons: tuple[dict, ...] = ()
    warnings: tuple[dict, ...] = ()
    mention_count: int = 0
    positive_count: int = 0
    negative_count: int = 0
    source_count: int = 0
    quality_score: float = 0.0
    recommend_score: float = 0.0


# How many visitor-evidenced restaurants may jump ahead of the Amap-rating
# order. The caller's limit still caps the whole list (route prep asks for 5).
EVIDENCE_RESTAURANT_CAP = 3

# travel_place_summary.canonical_place_id stores travel_canonical_place.place_id.
# That number is also the route place_id. Evidence is read the same way as
# Data Retrieval: non-empty cleaned_evidence, otherwise raw top_reasons.
NEARBY_RESTAURANT_SQL = f"""
SELECT
    c.place_id,
    c.canonical_name AS name,
    c.place_type,
    c.latitude,
    c.longitude,
    c.district,
    c.amap_poi_id,
    c.amap_rating,
    c.amap_avg_price,
    c.category_tags,
    COALESCE(s.mention_count_30d, 0) AS mention_count_30d,
    COALESCE(s.positive_count_30d, 0) AS positive_count_30d,
    COALESCE(s.negative_count_30d, 0) AS negative_count_30d,
    COALESCE(s.source_count, 0) AS source_count,
    COALESCE(s.quality_score, 0) AS quality_score,
    COALESCE(s.recommend_score, 0) AS recommend_score,
    {_TOP_REASONS_SELECT_SQL},
    s.cleaned_evidence AS cleaned_evidence,
    COALESCE(s.warnings, '[]'::jsonb) AS warnings
FROM travel_canonical_place AS c
LEFT JOIN travel_place_summary AS s
  ON s.canonical_place_id = c.place_id
WHERE c.city = :city
  AND c.place_type = 'restaurant'
  AND c.is_active = TRUE
  AND c.latitude IS NOT NULL
  AND c.longitude IS NOT NULL
  AND c.latitude BETWEEN :lat_min AND :lat_max
  AND c.longitude BETWEEN :lng_min AND :lng_max
  AND NOT (c.place_id = ANY(CAST(:exclude_place_ids AS bigint[])))
  AND c.canonical_name !~* :light_food_pattern
"""

KID_PLACE_SQL = f"""
SELECT
    c.place_id,
    c.canonical_name AS name,
    c.place_type,
    c.latitude,
    c.longitude,
    c.district,
    c.amap_poi_id,
    c.amap_rating,
    c.amap_avg_price,
    c.category_tags,
    c.migration_batch_id
FROM travel_canonical_place AS c
WHERE c.city = :city
  AND c.is_active = TRUE
  AND c.latitude IS NOT NULL
  AND c.longitude IS NOT NULL
  AND (
        c.migration_batch_id = '{KID_FILL_BATCH_ID}'
        OR (
            c.place_type NOT IN ({_KID_EXCLUDED_PLACE_TYPE_SQL})
            AND (
                c.category_tags ? '亲子'
                OR c.canonical_name ~ :kid_name_pattern
            )
        )
      )
ORDER BY c.amap_rating DESC NULLS LAST, c.place_id ASC
"""

TEA_HOUSE_SQL = f"""
SELECT
    c.place_id,
    c.canonical_name AS name,
    c.place_type,
    c.latitude,
    c.longitude,
    c.district,
    c.amap_poi_id,
    c.amap_rating,
    c.amap_avg_price,
    c.category_tags
FROM travel_canonical_place AS c
WHERE c.city = :city
  AND c.is_active = TRUE
  AND c.latitude IS NOT NULL
  AND c.longitude IS NOT NULL
  AND (
        c.migration_batch_id = '{TEA_FILL_BATCH_ID}'
        OR c.category_tags ? '茶馆'
        OR c.place_type = 'tea'
        OR c.canonical_name ~ :tea_name_pattern
      )
ORDER BY c.amap_rating DESC NULLS LAST, c.place_id ASC
"""


def haversine_meters(
    latitude_a: float,
    longitude_a: float,
    latitude_b: float,
    longitude_b: float,
) -> float:
    """Great-circle distance in meters. Same earth radius as food resolution."""

    lat_a = math.radians(latitude_a)
    lat_b = math.radians(latitude_b)
    delta_lat = lat_b - lat_a
    delta_lng = math.radians(longitude_b - longitude_a)
    haversine = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat_a) * math.cos(lat_b) * math.sin(delta_lng / 2) ** 2
    )
    return _EARTH_RADIUS_METERS * 2 * math.atan2(
        math.sqrt(haversine),
        math.sqrt(1 - haversine),
    )


def is_light_food_name(name: str) -> bool:
    return bool(_LIGHT_FOOD_NAME.search(name or ""))


def is_tea_house_name(name: str) -> bool:
    """True when the name contains a tea-house word, not merely 茶."""

    return any(phrase in (name or "") for phrase in TEA_HOUSE_NAME_PHRASES)


def include_kid_row(row: Mapping[str, Any]) -> bool:
    """Keep a kid-fill batch row. Other rows skip food and stay types."""

    if str(row.get("migration_batch_id") or "") == KID_FILL_BATCH_ID:
        return True
    place_type = str(row.get("place_type") or "").strip()
    return place_type not in KID_EXCLUDED_PLACE_TYPES


def _require_city(city: str) -> str:
    normalized = str(city or "").strip()
    if not normalized:
        raise ValueError("city is required")
    return normalized


def _category_tags(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return ()
    if not isinstance(value, list):
        return ()
    return tuple(str(item) for item in value if str(item).strip())


def _text_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text_value = str(value).strip()
    return text_value or None


def _whole_number(row: Mapping[str, Any], key: str) -> int:
    value = row.get(key)
    if value is None or value == "":
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _decimal_number(row: Mapping[str, Any], key: str) -> float:
    value = row.get(key)
    if value is None or value == "":
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _dict_items(value: Any) -> tuple[dict, ...]:
    return tuple(
        dict(item)
        for item in _json_list(value)
        if isinstance(item, dict) and item
    )


def candidate_from_row(row: Mapping[str, Any]) -> AreaCandidate:
    reasons = resolve_top_reasons_for_candidate(
        row.get("top_reasons"),
        row.get("cleaned_evidence"),
    )
    return AreaCandidate(
        place_id=int(row["place_id"]),
        name=str(row["name"]),
        place_type=str(row["place_type"]),
        latitude=float(row["latitude"]),
        longitude=float(row["longitude"]),
        district=_text_or_none(row.get("district")),
        amap_poi_id=_text_or_none(row.get("amap_poi_id")),
        amap_rating=_text_or_none(row.get("amap_rating")),
        amap_avg_price=_text_or_none(row.get("amap_avg_price")),
        category_tags=_category_tags(row.get("category_tags")),
        top_reasons=tuple(dict(item) for item in reasons if isinstance(item, dict) and item),
        warnings=_dict_items(row.get("warnings")),
        mention_count=_whole_number(row, "mention_count_30d"),
        positive_count=_whole_number(row, "positive_count_30d"),
        negative_count=_whole_number(row, "negative_count_30d"),
        source_count=_whole_number(row, "source_count"),
        quality_score=_decimal_number(row, "quality_score"),
        recommend_score=_decimal_number(row, "recommend_score"),
    )


def _rating_number(candidate: AreaCandidate) -> float | None:
    if candidate.amap_rating is None:
        return None
    try:
        return float(candidate.amap_rating)
    except ValueError:
        return None


def _rating_sort_prefix(candidate: AreaCandidate) -> tuple[int, float]:
    rating = _rating_number(candidate)
    if rating is None:
        return (1, 0.0)
    return (0, -rating)


def has_visitor_evidence(candidate: AreaCandidate) -> bool:
    """True when resolved summary reasons survived. Mention count alone does not."""

    return any(isinstance(item, dict) and item for item in candidate.top_reasons)


def _evidence_sort_key(candidate: AreaCandidate) -> tuple[int, float, float, int]:
    """Mention count, then quality score, then the retrieval recommend score."""

    return (
        -int(candidate.mention_count or 0),
        -float(candidate.quality_score or 0.0),
        -float(candidate.recommend_score or 0.0),
        int(candidate.place_id),
    )


def rank_nearby_restaurants(
    rows: list[AreaCandidate],
    *,
    latitude: float,
    longitude: float,
    radius_meters: float,
    limit: int,
    exclude_place_ids: Collection[int] | None = None,
) -> list[AreaCandidate]:
    """Keep proper restaurants inside the radius.

    Up to three restaurants with visitor evidence come first, most-mentioned
    and then highest quality score. Remaining slots use Amap rating, then
    distance. A later evidenced restaurant can still fill a slot when its
    rating beats the other leftovers.
    """

    if limit < 1:
        return []
    excluded = {int(place_id) for place_id in (exclude_place_ids or ())}
    inside: list[tuple[float, AreaCandidate]] = []
    for candidate in rows:
        if candidate.place_id in excluded:
            continue
        if candidate.place_type != "restaurant":
            continue
        if is_light_food_name(candidate.name):
            continue
        distance = haversine_meters(
            latitude,
            longitude,
            candidate.latitude,
            candidate.longitude,
        )
        if distance > radius_meters:
            continue
        inside.append((distance, candidate))

    evidenced = [candidate for _distance, candidate in inside if has_visitor_evidence(candidate)]
    evidenced.sort(key=_evidence_sort_key)
    chosen = evidenced[: min(EVIDENCE_RESTAURANT_CAP, limit)]
    chosen_ids = {candidate.place_id for candidate in chosen}
    rest: list[tuple[tuple[int, float, float, int], AreaCandidate]] = []
    for distance, candidate in inside:
        if candidate.place_id in chosen_ids:
            continue
        missing, inverse_rating = _rating_sort_prefix(candidate)
        rest.append(((missing, inverse_rating, distance, candidate.place_id), candidate))
    rest.sort(key=lambda item: item[0])
    return chosen + [candidate for _key, candidate in rest[: limit - len(chosen)]]


def rank_by_rating(rows: list[AreaCandidate]) -> list[AreaCandidate]:
    """Amap rating descending, missing ratings last, then place_id."""

    return sorted(
        rows,
        key=lambda candidate: (
            *_rating_sort_prefix(candidate),
            candidate.place_id,
        ),
    )


def _bounding_box(
    latitude: float,
    longitude: float,
    radius_meters: float,
) -> tuple[float, float, float, float]:
    lat_delta = (radius_meters / _METERS_PER_DEGREE_LAT) * 1.2
    lng_scale = max(math.cos(math.radians(latitude)), 0.2)
    lng_delta = (radius_meters / (_METERS_PER_DEGREE_LAT * lng_scale)) * 1.2
    return (
        latitude - lat_delta,
        latitude + lat_delta,
        longitude - lng_delta,
        longitude + lng_delta,
    )


async def fetch_nearby_restaurants(
    session,
    *,
    city: str,
    latitude: float,
    longitude: float,
    radius_meters: float = 1000,
    limit: int = 5,
    exclude_place_ids: Collection[int] | None = None,
) -> list[AreaCandidate]:
    """Return nearby proper restaurants for one coordinate.

    Includes every active restaurant row with coordinates, whether it came
    from ``area-food-v0.14`` or was already in the city. Light-food names are
    removed. Up to three restaurants with summary evidence come first. The
    rest follow Amap rating, then distance.
    """

    normalized_city = _require_city(city)
    if limit < 1:
        raise ValueError("limit must be positive")
    if radius_meters <= 0:
        raise ValueError("radius_meters must be positive")
    excluded = [int(place_id) for place_id in (exclude_place_ids or ())]
    lat_min, lat_max, lng_min, lng_max = _bounding_box(
        float(latitude),
        float(longitude),
        float(radius_meters),
    )
    result = await session.execute(
        text(NEARBY_RESTAURANT_SQL),
        {
            "city": normalized_city,
            "lat_min": lat_min,
            "lat_max": lat_max,
            "lng_min": lng_min,
            "lng_max": lng_max,
            "exclude_place_ids": excluded,
            "light_food_pattern": LIGHT_FOOD_NAME_PATTERN,
        },
    )
    rows = [candidate_from_row(dict(row)) for row in result.mappings().all()]
    return rank_nearby_restaurants(
        rows,
        latitude=float(latitude),
        longitude=float(longitude),
        radius_meters=float(radius_meters),
        limit=int(limit),
        exclude_place_ids=excluded,
    )


async def fetch_kid_places(session, *, city: str) -> list[AreaCandidate]:
    """Return this batch's kid venues plus existing rows of the same kind."""

    result = await session.execute(
        text(KID_PLACE_SQL),
        {
            "city": _require_city(city),
            "kid_name_pattern": KID_NAME_PATTERN,
        },
    )
    rows = [
        candidate_from_row(dict(row))
        for row in result.mappings().all()
        if include_kid_row(dict(row))
    ]
    return rank_by_rating(rows)


async def fetch_tea_houses(session, *, city: str) -> list[AreaCandidate]:
    """Return this batch's tea houses plus existing rows of the same kind."""

    result = await session.execute(
        text(TEA_HOUSE_SQL),
        {
            "city": _require_city(city),
            "tea_name_pattern": TEA_NAME_PATTERN,
        },
    )
    rows = [candidate_from_row(dict(row)) for row in result.mappings().all()]
    return rank_by_rating(rows)


