"""v0.15.1: one clock per route day, for the Writer and the rendered plan.

A model route keeps the clock its scheduler computed (RouteDayGroup.timeline).
Selector routes and rows written before v0.15.1 have none; this module then
estimates one from the stored stays and legs with the scheduler's floors.
The estimate never changes the route.
"""
from __future__ import annotations

from datetime import date, timedelta

from src.agents.composition_blueprint import FOOD_TYPES
from src.agents.pace import detect_pace_mode
from src.agents.route_planner.legs import _ESTIMATE_WORD
from src.agents.route_planner.places import _center
from src.agents.route_planner.rules import (
    DINNER,
    LUNCH,
    NIGHT_DINNER_CEILING,
    NIGHT_DINNER_FLOOR,
    PACE_START,
    _fmt,
    _start_day,
    sunset_minutes,
)
from src.agents.schema import (
    DayTimeline,
    DayTimelineItem,
    RouteDayGroup,
    TimelineMove,
    TripRequest,
    parse_hhmm,
)

DEFAULT_STAY_MINUTES = 60
AFTERNOON = 15 * 60


def stored_timeline(group: RouteDayGroup) -> DayTimeline | None:
    """The scheduler's clock, only while it still names the day's places in order."""
    timeline = group.timeline
    if timeline is None or not timeline.items:
        return None
    ids = [item.place_id for item in timeline.items if item.place_id is not None]
    if ids != [place.place_id for place in group.places]:
        return None
    return timeline


def day_timeline(group: RouteDayGroup, request: TripRequest) -> DayTimeline:
    return stored_timeline(group) or estimate_timeline(group, request)


def _dark_minute(group: RouteDayGroup, request: TripRequest) -> int:
    center = _center(list(group.places))
    if center is None:
        return 18 * 60
    day: date = _start_day(request, None) + timedelta(days=max(int(group.day or 1), 1) - 1)
    return sunset_minutes(center[0], center[1], day) + 25


def _guessed_kinds(group: RouteDayGroup) -> dict[int, str]:
    """Old rows have no kinds. First food stop is lunch, last is dinner."""
    foods = [place.place_id for place in group.places
             if place.place_type in FOOD_TYPES and place.place_id not in group.stop_kinds]
    if len(foods) < 2:
        return {}
    return {place_id: "snack" for place_id in foods[1:-1]} | {foods[0]: "lunch", foods[-1]: "dinner"}


def _kind(group: RouteDayGroup, guessed: dict[int, str], place, clock: int) -> str:
    kind = group.stop_kinds.get(place.place_id) or guessed.get(place.place_id)
    if kind:
        return kind
    # A lone food stop is the meal its clock falls in.
    if place.place_type in FOOD_TYPES:
        return "lunch" if clock < AFTERNOON else "dinner"
    return "sight"


def estimate_timeline(group: RouteDayGroup, request: TripRequest) -> DayTimeline:
    """Sequential clock: start by pace, stored stays and legs, meal and dark floors."""
    start = parse_hhmm(request.daily_start) or PACE_START[detect_pace_mode(request)]
    stays = {profile.place_id: profile.visit_minutes for profile in group.visit_profiles}
    legs = {(leg.from_place_id, leg.to_place_id): leg for leg in group.commute_legs}
    access = {leg.direction: leg for leg in group.access_legs}
    has_night = "night" in group.stop_kinds.values()
    dark = _dark_minute(group, request)
    dinner_floor = (
        max(NIGHT_DINNER_FLOOR, min(NIGHT_DINNER_CEILING, dark - 25 - 60)) if has_night else DINNER
    )
    outbound = access.get("outbound")
    depart = (
        TimelineMove(mode=_ESTIMATE_WORD.get(outbound.mode, "步行"), minutes=outbound.duration_minutes)
        if outbound is not None else None
    )
    clock = start + (outbound.duration_minutes if outbound is not None else 0)
    guessed = _guessed_kinds(group)
    items: list[DayTimelineItem] = []
    for place in group.places:
        if items:
            leg = legs.get((items[-1].place_id, place.place_id))
            items[-1].move_to_next = TimelineMove(
                mode=_ESTIMATE_WORD.get(leg.mode, "步行") if leg else "步行",
                minutes=leg.duration_minutes if leg else None,
            )
            clock += leg.duration_minutes if leg else 0
        kind = _kind(group, guessed, place, clock)
        floor = {"lunch": LUNCH, "dinner": dinner_floor, "night": dark}.get(kind, 0)
        wait = max(0, floor - clock)
        clock += wait
        stay = int(stays.get(place.place_id) or DEFAULT_STAY_MINUTES)
        items.append(DayTimelineItem(
            kind=kind, place_id=place.place_id, arrive=_fmt(clock),
            stay_minutes=stay, wait_minutes=wait,
        ))
        clock += stay
    inbound = access.get("inbound")
    if items and inbound is not None:
        items[-1].move_to_next = TimelineMove(
            mode=_ESTIMATE_WORD.get(inbound.mode, "步行"), minutes=inbound.duration_minutes)
        clock += inbound.duration_minutes
    return DayTimeline(
        source="estimated", start=_fmt(start), depart_move=depart, items=items, return_at=_fmt(clock),
    )
