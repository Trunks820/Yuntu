"""Constants, sunset and request rules for the v0.14 route planner."""

from __future__ import annotations

import math
import re

from dataclasses import dataclass

from datetime import date

from src.agents.accommodation_policy import excursion_daily_limit_minutes
from src.agents.pace import (
    daily_commute_budget_minutes,
    detect_pace_mode,
)

from src.agents.preferences import (
    CITYWALK,
    NIGHT_VIEW,
    family_arrangement_active,
    food_preference_active,
    normalize_preferences,
)

from src.agents.route_planning import valid_must_include_ids

from src.agents.schema import (
    CandidatePlace,
    RetrievalResult,
    TripRequest,
    parse_hhmm,
)

from src.config import Settings


LUNCH = 11 * 60 + 30
LATE_LUNCH = 13 * 60 + 30
DINNER = 17 * 60
LATE_DINNER = 20 * 60
PACE_START = {"relaxed": 9 * 60, "default": 8 * 60 + 30, "compact": 8 * 60}
DEFAULT_START = PACE_START["default"]
DEFAULT_END = 22 * 60
EXCURSION_START = 7 * 60 + 30
NIGHT_DINNER_FLOOR = 17 * 60 + 30
NIGHT_DINNER_CEILING = 19 * 60 + 30
PACE_SIGHT_HOURS = {"relaxed": 6, "default": 7, "compact": 8}
WALK_CAP_MINUTES = 35
SHORT_WALK_MINUTES = 20
SHORT_DRIVE_WALK_MINUTES = 15
EMIT_ERROR_TEXT_LIMIT = 120
MEAL_NEAR_KM = 1.5
IDLE_LIMIT_MINUTES = 60
PRE_DINNER_REST_MIN_WAIT = 90
PRE_DINNER_REST_LEG_CAP = 30
SIGHT_EXTEND_CAP = 60
CLUSTER_RADIUS_KM = 1.0
SCENIC_NEAR_KM = 0.3
SAME_PLACE_KM = 0.05
# A straight line longer than this cannot be a walk under the 35-minute cap.
FAR_WALK_KM = 2.5
KINDS = {"sight", "lunch", "dinner", "snack", "night", "rest"}
PACE_LIMIT = {"relaxed": 3, "default": 4, "compact": 5}
PACE_LABEL = {"relaxed": "轻松", "default": "适中", "compact": "紧凑"}
OTHER_PREFS = ("自然风光", "文化历史", "拍照")
TEA_MARKERS = ("茶馆", "喝茶", "品茶")
AREA_TYPES = {"business_area", "accommodation_area"}
CLOSED_NAME = re.compile(r"暂停营业|停业|歇业|已关闭")
JSON_RETRY_HINT = "只输出合法 JSON"
REPAIR_TAIL = "请在候选范围内修改后重新输出完整的行程 JSON（格式不变，不用写时间）。只输出合法 JSON。"


@dataclass(frozen=True)
class PlannerRules:
    days: int
    pace_mode: str
    pace_label: str
    sight_limit: int
    start_minute: int
    end_minute: int
    dinner: bool
    dark_minute: int
    commute_mode: str
    commute_budget: int
    family: bool
    night: bool
    food: bool
    tea: bool
    citywalk: bool
    must_ids: tuple[int, ...]
    must_names: tuple[str, ...]
    avoid: tuple[str, ...]
    other_preferences: tuple[str, ...]
    city: str
    urban_commute_cap: int = 150
    taxi_over_minutes: int = 40
    taxi_ratio: float = 0.5
    start_month: int = 0
    excursion_budget: int = 0


def sunset_minutes(latitude: float, longitude: float, day: date) -> int:
    """NOAA sunset in Beijing time, minutes after midnight."""
    g = 2 * math.pi / 365 * (day.timetuple().tm_yday - 1)
    equation = 229.18 * (
        0.000075
        + 0.001868 * math.cos(g)
        - 0.032077 * math.sin(g)
        - 0.014615 * math.cos(2 * g)
        - 0.040849 * math.sin(2 * g)
    )
    declination = (
        0.006918
        - 0.399912 * math.cos(g)
        + 0.070257 * math.sin(g)
        - 0.006758 * math.cos(2 * g)
        + 0.000907 * math.sin(2 * g)
        - 0.002697 * math.cos(3 * g)
        + 0.00148 * math.sin(3 * g)
    )
    phi = math.radians(latitude)
    cosine = math.cos(phi) * math.cos(declination)
    if abs(cosine) < 1e-9:
        return 18 * 60
    argument = math.cos(math.radians(90.833)) / cosine - math.tan(phi) * math.tan(declination)
    argument = max(-1.0, min(1.0, argument))
    hour_angle = math.degrees(math.acos(argument))
    return round(720 - 4 * (longitude - hour_angle) - equation + 480)


def _fmt(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def dinner_latest_minute(end_minute: int) -> int:
    return min(LATE_DINNER, end_minute - 60)


def dinner_earliest_minute(rules: PlannerRules, *, has_night: bool) -> int:
    """Earliest dinner start for one day. Night days follow sunset, clamped."""
    latest = dinner_latest_minute(rules.end_minute)
    if has_night:
        sunset = rules.dark_minute - 25
        earliest = max(NIGHT_DINNER_FLOOR, min(NIGHT_DINNER_CEILING, sunset - 60))
    else:
        earliest = DINNER
    if earliest > latest:
        return latest
    return earliest


def day_has_night(stops) -> bool:
    return any(stop.get("kind") == "night" for stop in (stops or []))


def schedule_start_minute(
    rules: PlannerRules,
    request: TripRequest | None,
    *,
    excursion: bool,
) -> int:
    """Clock used to start one day's timeline. User-filled daily_start wins."""
    if request is not None and parse_hhmm(request.daily_start) is not None:
        return rules.start_minute
    if excursion:
        return min(rules.start_minute, EXCURSION_START)
    return rules.start_minute


def _clock(value) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return parse_hhmm(str(value)) if value is not None else None


def _start_day(request: TripRequest, today: date | None) -> date:
    raw = request.start_date or ""
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return today or date.today()


def _tea_requested(request: TripRequest) -> bool:
    text = " ".join([*(request.preferences or []), request.notes or ""])
    return any(marker in text for marker in TEA_MARKERS)


def _family_requested(request: TripRequest) -> bool:
    if family_arrangement_active(request.preferences, request.notes or ""):
        return True
    return any("亲子" in (item or "") for item in request.preferences or [])

# Imported after the constants so places can import those constants back.
from src.agents.route_planner.places import (
    _coords,
    _center,
)



def build_rules(
    request: TripRequest,
    places: list[CandidatePlace],
    *,
    settings: Settings,
    today: date | None = None,
) -> PlannerRules:
    pace_mode = detect_pace_mode(request)
    start = parse_hhmm(request.daily_start) or PACE_START[pace_mode]
    end = parse_hhmm(request.daily_end) or DEFAULT_END
    start_day = _start_day(request, today)
    center = _center(places)
    if center is None:
        dark = 18 * 60
    else:
        dark = sunset_minutes(center[0], center[1], start_day) + 25
    dimensions = normalize_preferences(request.preferences).scoring_dimensions
    must_ids = tuple(sorted(valid_must_include_ids(request, RetrievalResult(city=request.to_city or "", candidates=places))))
    # valid_must_include_ids reads retrieval.candidates. The pool may already
    # have dropped a duplicate, so also trust flags on the places we were given.
    flagged = tuple(sorted(place.place_id for place in places if place.must_include and _coords(place)[0] is not None))
    chosen = flagged or must_ids
    names = []
    by_id = {place.place_id: place for place in places}
    for item in request.must_include or []:
        place = by_id.get(item.place_id) if item.place_id else None
        names.append(place.name if place is not None else item.name)
    return PlannerRules(
        days=max(int(request.days or 1), 1),
        pace_mode=pace_mode,
        pace_label=PACE_LABEL[pace_mode],
        sight_limit=PACE_LIMIT[pace_mode],
        start_minute=start,
        end_minute=end,
        dinner=end >= 19 * 60,
        dark_minute=dark,
        commute_mode=request.commute_mode,
        commute_budget=daily_commute_budget_minutes(request, settings),
        excursion_budget=excursion_daily_limit_minutes(request, settings),
        urban_commute_cap=int(settings.route_planner_urban_commute_cap_minutes),
        taxi_over_minutes=int(settings.route_planner_taxi_when_transit_over_minutes),
        taxi_ratio=float(settings.route_planner_taxi_ratio),
        family=_family_requested(request),
        night=NIGHT_VIEW in dimensions,
        food=food_preference_active(request.preferences),
        tea=_tea_requested(request),
        citywalk=CITYWALK in dimensions,
        must_ids=chosen,
        must_names=tuple(names),
        avoid=tuple(request.avoid or []),
        other_preferences=tuple(item for item in OTHER_PREFS if item in (request.preferences or [])),
        city=request.to_city or "",
        start_month=start_day.month,
    )


