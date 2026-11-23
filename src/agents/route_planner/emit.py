"""Turn a scheduled plan into a RoutePlan."""

from __future__ import annotations

from src.agents.pace import generation_base_mode

from src.agents.preference_notices import (
    PROGRAM_PRE_DINNER_REST_HINT,
    PROGRAM_REST_HINT,
)

from src.agents.route_planning import (
    RoutePlanningMetrics,
    _v2_make_plan,
    _v2_verify_traffic,
)

from src.agents.schema import (
    AccommodationAnchor,
    CandidatePlace,
    DayTimeline,
    DayTimelineItem,
    RouteDayGroup,
    RoutePlan,
    TimelineMove,
    TripRequest,
)

from src.agents.route_planner.rules import (
    EMIT_ERROR_TEXT_LIMIT,
    PlannerRules,
    _fmt,
)

from src.agents.route_planner.places import (
    _coords,
    coerce_place_id,
    _protected_ids,
)

from src.agents.route_planner.legs import CachingEstimator, _apply_scheduled_legs, prefetch_long_legs

from src.agents.route_planner.scenic import included_scenic_places, merge_scenic_stops, record_scenic_visit

from src.agents.route_planner.schedule import (
    _BACKUP_GAP_REST,
    _movement_pairs,
    _stay,
    diversify_same_day_pots,
    drop_unrequested_snacks,
    schedule_day,
    repick_meals,
)

from src.agents.route_planner.lodging import (
    _resolve_lodging,
    _lodging_anchor,
    _choose_hotel_lodging,
    _estimator_deadline,
    _keep_preset_lodging,
    _lodging_reason_for_route,
    _take_user_lodging,
    hotel_lodging_enabled,
)

# get_settings is read from the package so tests can patch route_planner.get_settings.
import src.agents.route_planner as _route_planner_pkg

INPLACE_LUNCH_REST_HINT = "午饭后在附近找地方坐下歇一会儿"
INPLACE_DINNER_REST_HINT = "晚饭前在附近找地方坐下歇一会儿"



def _attach_places(plan: dict, by_id: dict[int, CandidatePlace], rules: PlannerRules) -> list[str]:
    adjustments = []
    for day in plan.get("days") or []:
        if not rules.dinner:
            dropped = [stop for stop in day.get("stops") or [] if stop.get("kind") in {"dinner", "night"}]
            if dropped:
                day["stops"] = [stop for stop in day["stops"] if stop not in dropped]
                adjustments.append(f"第{day.get('day')}天时间窗 {_fmt(rules.end_minute)} 结束，删去晚饭／夜景 {len(dropped)} 站")
        kept = []
        for stop in day.get("stops") or []:
            stop.pop("_scenic_stops", None)
            if stop.get("kind") == "rest":
                stop["_place"] = None
                kept.append(stop)
                continue
            token = coerce_place_id(stop.get("place_id"))
            place = by_id.get(token) if isinstance(token, int) else None
            if place is None and stop.get("kind") in {"sight", "night", "snack"}:
                adjustments.append(f"第{day.get('day')}天删去候选里没有的编号 {stop.get('place_id')}")
                continue
            stop["place_id"] = token
            stop["_place"] = place
            kept.append(stop)
        day["stops"] = kept
    return adjustments


async def arrange(
    plan: dict,
    *,
    rules: PlannerRules,
    pool: list[CandidatePlace],
    by_id: dict[int, CandidatePlace],
    request: TripRequest,
    estimator,
    fetch_restaurants,
    session,
    settings=None,
    lodging_anchor: AccommodationAnchor | None = None,
    hotel_query=None,
    hotel_cache=None,
    preset_lodging: CandidatePlace | None = None,
    amap_ids: set[int] | None = None,
    search_hotels: bool = True,
    fit_opening_hours: bool = False,
    keep_day_sight: bool = False,
) -> tuple[dict, list[str], CandidatePlace | None, str, str]:
    adjustments = []
    # None, or a settings object with the flag off, keeps the model anchor.
    # A preset is the lodging picked before the prompt. User lodging still wins.
    # Without a preset, hotel lodging stays the package U search.
    if (search_hotels and hotel_lodging_enabled(settings)) or (not search_hotels and preset_lodging is not None):
        taken = _take_user_lodging(request, pool, by_id, lodging_anchor)
        if taken is not None:
            lodge, source, name, lodging_notes = taken
        elif preset_lodging is not None:
            lodge, source, name, lodging_notes = _keep_preset_lodging(
                request, plan, preset_lodging
            )
        else:
            lodge, source, name, lodging_notes = await _choose_hotel_lodging(
                request,
                plan,
                pool,
                by_id,
                lodging_anchor=lodging_anchor,
                settings=settings,
                hotel_query=hotel_query,
                hotel_cache=hotel_cache,
                deadline=_estimator_deadline(estimator),
            )
    else:
        lodge, source, name, lodging_notes = _resolve_lodging(
            request, plan, pool, by_id, lodging_anchor=lodging_anchor
        )
    adjustments.extend(lodging_notes)
    if isinstance(lodge, CandidatePlace) and lodge.place_id and lodge.place_id not in by_id:
        by_id[lodge.place_id] = lodge
    adjustments.extend(_attach_places(plan, by_id, rules))
    adjustments.extend(drop_unrequested_snacks(plan, rules))
    adjustments.extend(await merge_scenic_stops(plan, session))
    days = plan.get("days") or []
    if isinstance(estimator, CachingEstimator):
        # A conditional return to lodging during a rest is decided by the day's scheduler.
        pairs = (
            pair for day in days for pair in _movement_pairs(
                [stop for stop in day.get("stops") or []
                 if stop.get("kind") != "rest" or not rules.rest_via_lodge_only_when_near],
                lodge,
            )
        )
        await prefetch_long_legs(
            estimator, pairs, rules.commute_mode, deadline_monotonic=_estimator_deadline(estimator),
        )
    protected = _protected_ids(list(by_id.values()), rules)

    def day_protected(day: dict) -> set[int]:
        return protected | {
            stop["place_id"]
            for stop in day.get("stops") or []
            if any(place.place_id in protected for place in included_scenic_places(stop))
        }

    used = {
        stop["_place"].place_id
        for day in days
        for stop in day.get("stops") or []
        if stop.get("_place") is not None and stop.get("kind") in {"lunch", "dinner"}
    }
    for day in days:
        adjustments.extend(
            await schedule_day(
                day,
                rules=rules,
                lodge=lodge,
                protected=day_protected(day),
                estimator=estimator,
                request=request,
                settings=settings,
                fit_opening_hours=fit_opening_hours,
                keep_day_sight=keep_day_sight,
            )
        )
        swaps = await repick_meals(
            day,
            lodge,
            fetch_restaurants=fetch_restaurants,
            session=session,
            city=rules.city,
            used_ids=used,
            must_ids=rules.must_ids,
            opening_hours=rules.opening_hours,
        )
        if swaps:
            adjustments.extend(swaps)
            adjustments.extend(
                await schedule_day(
                    day,
                    rules=rules,
                    lodge=lodge,
                    protected=day_protected(day),
                    estimator=estimator,
                    request=request,
                    settings=settings,
                    fit_opening_hours=fit_opening_hours,
                    keep_day_sight=keep_day_sight,
                )
            )
    swapped_days: list[dict] = []
    adjustments.extend(
        await diversify_same_day_pots(
            plan,
            lodge,
            rules=rules,
            fetch_restaurants=fetch_restaurants,
            session=session,
            city=rules.city,
            used_ids=used,
            amap_ids=amap_ids,
            swapped_days=swapped_days,
        )
    )
    # Same as after repick_meals: the legs were for the old shop.
    for day in swapped_days:
        adjustments.extend(
            await schedule_day(
                day,
                rules=rules,
                lodge=lodge,
                protected=day_protected(day),
                estimator=estimator,
                request=request,
                settings=settings,
                fit_opening_hours=fit_opening_hours,
                keep_day_sight=keep_day_sight,
            )
        )
    for place in (stop.get("_place") for day in plan.get("days") or [] for stop in day.get("stops") or []):
        if isinstance(place, CandidatePlace) and place.place_id not in by_id:
            by_id[place.place_id] = place
    return plan, adjustments, lodge, source, name


def _day_places(day: dict) -> list[CandidatePlace]:
    places = []
    for stop in day.get("stops") or []:
        place = stop.get("_place")
        if stop.get("kind") == "rest" or not isinstance(place, CandidatePlace):
            continue
        places.append(place)
    return places


def _move(value) -> TimelineMove:
    value = value if isinstance(value, dict) else {}
    minutes = value.get("minutes")
    return TimelineMove(
        mode=str(value.get("mode") or "步行"),
        minutes=minutes if isinstance(minutes, int) and not isinstance(minutes, bool) else None,
    )


def _scheduled_timeline(day: dict, group: RouteDayGroup) -> DayTimeline | None:
    """The clock ``schedule_day`` left on the stops, read as is.

    None when the day was never scheduled or its stops no longer line up
    with the emitted places; a reader then estimates instead.
    """
    start = day.get("_start_minute")
    end = day.get("_return_minute")
    if not isinstance(start, int) or not isinstance(end, int):
        return None
    items: list[DayTimelineItem] = []
    for stop in day.get("stops") or []:
        kind = stop.get("kind")
        place = stop.get("_place")
        if kind != "rest" and not isinstance(place, CandidatePlace):
            continue
        arrive = stop.get("time")
        if kind not in {"sight", "lunch", "dinner", "snack", "night", "rest"} or not isinstance(arrive, str):
            return None
        items.append(DayTimelineItem(
            kind=kind,
            place_id=None if kind == "rest" else int(place.place_id),
            arrive=arrive,
            stay_minutes=_stay(stop),
            wait_minutes=int(stop.get("wait_minutes") or 0),
            rest_via_lodge=bool(stop.get("_rest_via_lodge")),
            move_to_next=_move(stop.get("move_to_next")),
        ))
    if [item.place_id for item in items if item.place_id is not None] != [
        place.place_id for place in group.places
    ]:
        return None
    return DayTimeline(
        source="scheduler",
        start=_fmt(start),
        depart_move=_move(day.get("_depart_move")),
        items=items,
        return_at=_fmt(end),
    )


def _assigned_stop_kinds(day: dict) -> dict[int, str]:
    """Keep each surviving stop's type. Rest is not a place on the route."""
    kinds: dict[int, str] = {}
    for stop in day.get("stops") or []:
        kind = stop.get("kind")
        place = stop.get("_place")
        if kind not in {"sight", "lunch", "dinner", "snack", "night"}:
            continue
        if not isinstance(place, CandidatePlace) or int(place.place_id or 0) <= 0:
            continue
        kinds[int(place.place_id)] = str(kind)
    return kinds


async def emit_route_plan(
    plan: dict,
    *,
    request: TripRequest,
    lodge: CandidatePlace | None,
    lodging_source: str,
    provider,
    citycode: str | None,
    by_id: dict[int, CandidatePlace],
    known: dict | None = None,
    known_access: dict | None = None,
    location_precision: str | None = None,
    write_lodging_reason: bool = False,
) -> RoutePlan:
    groups = [_day_places(day) for day in plan.get("days") or []]
    coord = _coords(lodge)
    accommodation_coord = coord if coord[0] is not None else None
    source = "user_specified" if lodging_source == "user" else "auto_recommended"
    # Same dict the scheduler filled. _resolve_precise_legs skips a provider
    # call when this key is already a successful amap leg.
    known = {} if known is None else known
    known_access = {} if known_access is None else known_access
    # The anchor carries the lodging already used for the access legs.
    # coupling_context stays None, so day traffic policy is unchanged.
    route = _v2_make_plan(
        groups,
        request,
        accommodation_coord,
        source,
        known,
        coupling_context=None,
        anchor=_lodging_anchor(lodge, lodging_source, location_precision=location_precision),
        known_access=known_access,
    )
    must_ids = set(place.place_id for place in by_id.values() if place.must_include)
    eligible_ids = {place.place_id for place in by_id.values() if place.place_id}
    for group in groups:
        eligible_ids.update(place.place_id for place in group if place.place_id)
    if provider is not None:
        # Same settings object the scheduler freezes before verify. With the
        # commute-mode flag off this is driving, even when the request asked
        # for another mode. Access legs were built with this mode.
        settings = _route_planner_pkg.get_settings()
        mode, _reason = generation_base_mode(request, settings)
        await _v2_verify_traffic(
            route,
            request=request,
            provider=provider,
            known=known,
            known_access=known_access,
            attempted_access=set(),
            access_walk_rejections={},
            city=request.to_city or "",
            citycode=citycode,
            deadline=None,
            mode=mode,
            metrics=RoutePlanningMetrics(),
            must_ids=must_ids,
            eligible_ids=eligible_ids,
            # The scheduler's coverage dict is selector state. This handoff has
            # none. None is the supported omission and skips that check.
            coverage=None,
        )
    _apply_scheduled_legs(route, plan, lodge, request)
    if write_lodging_reason:
        reason = _lodging_reason_for_route(route)
        suggestion = None if route.accommodation_anchor is None else route.accommodation_anchor.suggestion
        if suggestion is not None and suggestion.source != "user_specified" and reason:
            suggestion.reason = reason
    for day, group in zip(plan.get("days") or [], route.day_groups):
        # Read kinds after drops and meal swaps. place_id, not visit order.
        group.stop_kinds = _assigned_stop_kinds(day)
        group.timeline = _scheduled_timeline(day, group)
        stops = day.get("stops") or []
        lunch_rest = False
        dinner_rest = False
        inplace_lunch = False
        inplace_dinner = False
        for index, stop in enumerate(stops):
            if stop.get("kind") != "rest":
                continue
            slot = stop.get("_rest_slot")
            if stop.get("_backup_gap_rest") is _BACKUP_GAP_REST and slot in {"pre_lunch", "pre_night"}:
                where = "回住处休息" if stop.get("_rest_via_lodge") else "在附近找地方休息"
                when = "夜景前" if slot == "pre_night" else "午饭前"
                suffix = "，傍晚再出门" if slot == "pre_night" and stop.get("_rest_via_lodge") else ""
                group.time_hints.append(f"{when}{where}{suffix}")
                continue
            nxt_kind = next(
                (item.get("kind") for item in stops[index + 1 :] if item.get("kind") != "rest"),
                "",
            )
            dinnerish = (
                nxt_kind == "dinner"
                or stop.get("_rest_slot") == "pre_dinner"
                or stop.get("_idle_fill")
            )
            if stop.get("_rest_via_lodge"):
                if dinnerish:
                    dinner_rest = True
                else:
                    lunch_rest = True
            elif dinnerish:
                inplace_dinner = True
            else:
                inplace_lunch = True
        if lunch_rest and PROGRAM_REST_HINT not in group.time_hints:
            group.time_hints.append(PROGRAM_REST_HINT)
        if dinner_rest and PROGRAM_PRE_DINNER_REST_HINT not in group.time_hints:
            group.time_hints.append(PROGRAM_PRE_DINNER_REST_HINT)
        if inplace_lunch and INPLACE_LUNCH_REST_HINT not in group.time_hints:
            group.time_hints.append(INPLACE_LUNCH_REST_HINT)
        if inplace_dinner and INPLACE_DINNER_REST_HINT not in group.time_hints:
            group.time_hints.append(INPLACE_DINNER_REST_HINT)
        for stop in stops:
            record_scenic_visit(group, stop)
            if stop.get("kind") == "night" and stop.get("_place") is not None:
                group.time_hints.append(f"晚饭后去{stop['_place'].name}")
    _seal_model_route(route)
    nearby_names = (
        getattr(lodge, "_nearby_lodging_hotel_names", None)
        if lodge is not None
        else None
    )
    if nearby_names is not None:
        route.nearby_lodging_hotel_names = list(nearby_names)
    return route


def _seal_model_route(route: RoutePlan) -> None:
    """Keep calculated facts and drop the old-rule verdict.

    Package C already accepted the day. Load ceiling, the old single-leg cap
    and the old day shape stay out of ``violations``, so ``feasible`` follows
    the new rules. The numbers on the day stay aligned with its legs.
    """
    route.planner_source = "model"
    for day in route.day_groups:
        fact = day.day_feasibility
        if fact is None:
            continue
        fact.violations = []


def _emit_failure_issue(exc: BaseException) -> str:
    """Type name plus the exception text, clipped so one issue stays readable."""
    detail = " ".join(str(exc).split())
    if len(detail) > EMIT_ERROR_TEXT_LIMIT:
        detail = detail[:EMIT_ERROR_TEXT_LIMIT]
    label = type(exc).__name__
    if not detail:
        return f"交给现有路线格式失败：{label}"
    return f"交给现有路线格式失败：{label}：{detail}"
