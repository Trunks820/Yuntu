"""Day scheduling, meal windows and idle-time fills."""

from __future__ import annotations

from src.agents.composition_blueprint import is_light_food_place

from src.agents.preferences import candidate_is_tea_place

from src.agents.schema import (
    CandidatePlace,
    TripRequest,
    parse_hhmm,
)

from src.agents.route_planner.rules import (
    LUNCH,
    LATE_LUNCH,
    MEAL_NEAR_KM,
    IDLE_LIMIT_MINUTES,
    PRE_DINNER_REST_MIN_WAIT,
    PRE_DINNER_REST_LEG_CAP,
    SIGHT_EXTEND_CAP,
    PlannerRules,
    _fmt,
    day_has_night,
    dinner_earliest_minute,
    dinner_latest_minute,
    is_pot_meal,
    schedule_start_minute,
)

from src.agents.route_planner.places import (
    hav_km,
    _coords,
    _point_key,
    _closed,
    _rating,
    _as_candidate,
    scenic_groups,
)

from src.agents.route_planner.legs import (
    _chosen_hop,
    resolve_legs,
)



def _stay(stop: dict) -> int:
    try:
        minutes = int(stop.get("stay_minutes") or 60)
    except (TypeError, ValueError):
        minutes = 60
    return min(max(minutes, 20), 240)


def _rest_goes_via_lodge(stop: dict, lodge: CandidatePlace | None) -> bool:
    """Unset flag keeps the old 'any lodging means go back' path."""
    if lodge is None:
        return False
    flagged = stop.get("_rest_via_lodge")
    if flagged is None:
        return True
    return bool(flagged)


def _movement_pairs(stops: list[dict], lodge: CandidatePlace | None) -> list[tuple]:
    pairs = []
    pos = lodge
    for stop in stops:
        if stop.get("kind") == "rest":
            if not _rest_goes_via_lodge(stop, lodge):
                continue
            if pos is not None and _point_key(pos) != _point_key(lodge):
                pairs.append((pos, lodge))
            pos = lodge
            continue
        place = stop.get("_place")
        if place is None or pos is None:
            pos = place or pos
            continue
        pairs.append((pos, place))
        pos = place
    if lodge is not None and pos is not None and _point_key(pos) != _point_key(lodge):
        pairs.append((pos, lodge))
    return pairs


def _lookup(quoted: dict, origin, dest) -> tuple[int | None, str]:
    if origin is None or dest is None:
        return 0, "步行"
    if _point_key(origin) == _point_key(dest):
        return 1, "步行"
    return quoted.get((_point_key(origin), _point_key(dest)), (None, "步行"))


def _immediate_rest_before(stops: list[dict], meal_stop: dict) -> bool:
    try:
        index = stops.index(meal_stop)
    except ValueError:
        return False
    return index > 0 and stops[index - 1].get("kind") == "rest"


def _timeline_prev_place(timeline: list[dict], index: int):
    for item in reversed(timeline[:index]):
        stop = item.get("stop") or {}
        place = stop.get("_place")
        if stop.get("kind") != "rest" and place is not None:
            return item
    return None


# A relaxed lunch that still has a long wait leaves no earlier than this.
LATE_DEPARTURE_CAP = 11 * 60
# Leave about half an hour early, not zero: the meal window stays put.
LUNCH_IDLE_TARGET = 30
# A dinner rest keeps a short gap so the meal is not entered on the dot.
DINNER_IDLE_SLACK = 15
# July and August keep the long midday rest. Do not cut it under two hours.
SUMMER_REST_FLOOR = 120
# Relaxed rests and a late sight are shortened before anything is dropped.
RELAXED_REST_FLOOR = 60
SIGHT_SHRINK_FLOOR = 60


def _extend_prev_sight(
    timeline: list[dict],
    meal_index: int,
    wait: int,
    day_number,
    *,
    day_end: int | None = None,
) -> str | None:
    meal = timeline[meal_index]["stop"]
    meal_label = "午饭" if meal.get("kind") == "lunch" else "晚饭"
    sight_item = None
    for item in reversed(timeline[:meal_index]):
        stop = item.get("stop") or {}
        # Relaxed: a sight before the previous meal cannot shorten this wait.
        if day_end is not None and stop.get("kind") in {"lunch", "dinner"}:
            break
        if stop.get("kind") == "sight" and stop.get("_place") is not None:
            sight_item = item
            break
    if sight_item is None:
        return None
    sight = sight_item["stop"]
    already = sight.get("_extended_minutes")
    already = already if isinstance(already, int) and already > 0 else 0
    current = _stay(sight)
    extra = min(SIGHT_EXTEND_CAP - already, 240 - current, wait)
    # Relaxed urban days pass day_end. The added minutes must not finish after it.
    if day_end is not None:
        room = int(day_end) - (int(sight_item["arrive"]) + current)
        extra = min(extra, max(0, room))
    if extra <= 0:
        return None
    sight["stay_minutes"] = current + extra
    sight["_extended_minutes"] = already + extra
    name = sight["_place"].name
    return f"第{day_number}天「{name}」多逛 {extra} 分钟，正好赶上{meal_label}"


async def _try_insert_dinner_rest(
    day: dict,
    timeline: list[dict],
    meal_index: int,
    *,
    lodge: CandidatePlace,
    rules: PlannerRules,
    estimator,
) -> str | None:
    meal_item = timeline[meal_index]
    meal_stop = meal_item["stop"]
    if _immediate_rest_before(day.get("stops") or [], meal_stop):
        return None
    prev_item = _timeline_prev_place(timeline, meal_index)
    meal_place = meal_stop.get("_place")
    if prev_item is None or meal_place is None:
        return None
    prev_place = prev_item["stop"]["_place"]
    resolved = await resolve_legs(
        estimator,
        [(prev_place, lodge), (lodge, meal_place)],
        rules.commute_mode,
        taxi_over_minutes=int(rules.taxi_over_minutes),
        taxi_ratio=float(rules.taxi_ratio),
    )
    inbound = resolved[0][0] if resolved else None
    outbound = resolved[1][0] if len(resolved) > 1 else None
    if not isinstance(inbound, int) or not isinstance(outbound, int):
        return None
    if inbound > PRE_DINNER_REST_LEG_CAP or outbound > PRE_DINNER_REST_LEG_CAP:
        return None
    prev_end = int(prev_item["arrive"]) + int(prev_item["stay"])
    earliest = dinner_earliest_minute(rules, has_night=day_has_night(day.get("stops")))
    stay = earliest - prev_end - inbound - outbound
    if stay < 20:
        return None
    stay = min(stay, 240)
    rest = {
        "kind": "rest",
        "place_id": None,
        "stay_minutes": stay,
        "_idle_fill": True,
        "_rest_slot": "pre_dinner",
    }
    stops = day.get("stops") or []
    try:
        index = stops.index(meal_stop)
    except ValueError:
        return None
    stops.insert(index, rest)
    day["stops"] = stops
    return f"第{day.get('day')}天晚饭前回住处歇约 {stay} 分钟"


async def _fill_meal_gaps(
    day: dict,
    timeline: list[dict],
    *,
    rules: PlannerRules,
    lodge: CandidatePlace | None,
    estimator,
    day_end: int | None = None,
) -> list[str]:
    """Eat meal waits over 1 hour: rest at lodging, or linger at the previous sight."""
    notes: list[str] = []
    for index, item in enumerate(timeline):
        kind = item["stop"].get("kind")
        if kind not in {"lunch", "dinner"}:
            continue
        wait = item.get("wait")
        if not isinstance(wait, int) or wait <= IDLE_LIMIT_MINUTES:
            continue
        inserted = None
        if (
            kind == "dinner"
            and wait >= PRE_DINNER_REST_MIN_WAIT
            and lodge is not None
        ):
            inserted = await _try_insert_dinner_rest(
                day, timeline, index, lodge=lodge, rules=rules, estimator=estimator
            )
            if inserted:
                notes.append(inserted)
                continue
        extended = _extend_prev_sight(
            timeline, index, wait, day.get("day"), day_end=day_end,
        )
        if extended:
            notes.append(extended)
    return notes


def _shrink_for_day_end(
    day: dict,
    timeline: list[dict],
    late_index: int,
    day_end: int,
    rules: PlannerRules,
) -> list[str]:
    """Make room before the relaxed wrap: shorten rests, then the late sight.

    Only rests after the last meal before the sight count: the sight is not
    pinned to a clock, so it moves up by what they lose. Floors stay.
    """
    item = timeline[late_index]
    over = int(item["arrive"]) + int(item["stay"]) - int(day_end)
    if over <= 0:
        return []
    number = day.get("day")
    notes: list[str] = []
    first = 0
    for index in range(late_index - 1, -1, -1):
        if timeline[index]["stop"].get("kind") in {"lunch", "dinner"}:
            first = index + 1
            break
    floor = SUMMER_REST_FLOOR if rules.start_month in {7, 8} else RELAXED_REST_FLOOR
    for index in range(first, late_index):
        stop = timeline[index]["stop"]
        if stop.get("kind") != "rest" or over <= 0:
            continue
        current = _stay(stop)
        cut = min(over, current - floor)
        if cut <= 0:
            continue
        stop["stay_minutes"] = current - cut
        over -= cut
        notes.append(f"第{number}天歇改为约 {current - cut} 分钟，{_fmt(day_end)} 前收")
    if over > 0:
        sight = item["stop"]
        current = _stay(sight)
        cut = min(over, current - SIGHT_SHRINK_FLOOR)
        if cut > 0:
            sight["stay_minutes"] = current - cut
            notes.append(f"第{number}天「{sight['_place'].name}」少逛 {cut} 分钟，{_fmt(day_end)} 前收")
    return notes


def _absorb_relaxed_waits(
    day: dict,
    timeline: list[dict],
    *,
    start_minute: int,
    can_delay: bool,
) -> list[str]:
    """A relaxed city day does not wait at a restaurant.

    Lunch: leave later, down to about half an hour of wait, no later than
    11:00. Dinner: rest before it, or lengthen the rest already there. Where
    the rest happens is decided on the next pass.
    """
    number = day.get("day")
    stops = day.get("stops") or []
    for item in timeline:
        meal = item["stop"]
        kind = meal.get("kind")
        wait = item.get("wait")
        if kind not in {"lunch", "dinner"} or not isinstance(wait, int) or wait <= IDLE_LIMIT_MINUTES:
            continue
        if kind == "lunch":
            if not can_delay:
                continue
            delay = min(wait - LUNCH_IDLE_TARGET, LATE_DEPARTURE_CAP - start_minute)
            if delay <= 0:
                continue
            day["_start_delay"] = int(day.get("_start_delay") or 0) + delay
            return [f"第{number}天上午时间宽，改为约 {_fmt(start_minute + delay)} 出门"]
        if meal not in stops:
            continue
        absorb = max(20, wait - DINNER_IDLE_SLACK)
        if _immediate_rest_before(stops, meal):
            rest = stops[stops.index(meal) - 1]
            current = _stay(rest)
            longer = min(240, current + absorb)
            if longer <= current:
                continue
            rest["stay_minutes"] = longer
            return [f"第{number}天晚饭前多歇约 {longer - current} 分钟"]
        stay = min(240, absorb)
        stops.insert(stops.index(meal), {
            "kind": "rest",
            "place_id": None,
            "stay_minutes": stay,
            "_idle_fill": True,
            "_rest_slot": "pre_dinner",
        })
        day["stops"] = stops
        return [f"第{number}天晚饭前歇约 {stay} 分钟"]
    return []


def _day_is_excursion(
    day: dict,
    request: TripRequest | None,
    lodge: CandidatePlace | None,
    settings,
) -> bool:
    if request is None or settings is None:
        return False
    from src.agents.route_planner.checks import classify_day, _day_stop_places

    return classify_day(_day_stop_places(day), request, settings, lodge).day_kind == "excursion"


def _adjacent_place(stops: list[dict], index: int, *, forward: bool):
    seq = stops[index + 1 :] if forward else reversed(stops[:index])
    for stop in seq:
        if stop.get("kind") == "rest":
            continue
        place = stop.get("_place")
        if place is not None:
            return place
    return None


async def _assign_rest_via_lodge(
    stops: list[dict],
    lodge: CandidatePlace | None,
    rules: PlannerRules,
    estimator,
) -> None:
    """Mark each rest: back to lodging, or stay put.

    Relaxed only goes back when both legs are within PRE_DINNER_REST_LEG_CAP.
    Other paces keep the old 'lodging exists → go back' path.
    """
    rests = [(index, stop) for index, stop in enumerate(stops) if stop.get("kind") == "rest"]
    if not rests:
        return
    if lodge is None:
        for _index, stop in rests:
            stop["_rest_via_lodge"] = False
        return
    gated = bool(rules.rest_via_lodge_only_when_near)
    if not gated:
        for _index, stop in rests:
            stop["_rest_via_lodge"] = True
        return
    pairs = []
    meta = []
    for index, stop in rests:
        prev = _adjacent_place(stops, index, forward=False)
        nxt = _adjacent_place(stops, index, forward=True)
        meta.append((stop, prev, nxt))
        if prev is not None:
            pairs.append((prev, lodge))
        if nxt is not None:
            pairs.append((lodge, nxt))
    quoted = {}
    if pairs:
        resolved = await resolve_legs(
            estimator,
            pairs,
            rules.commute_mode,
            taxi_over_minutes=int(rules.taxi_over_minutes),
            taxi_ratio=float(rules.taxi_ratio),
        )
        quoted = {
            (_point_key(origin), _point_key(dest)): value
            for (origin, dest), value in zip(pairs, resolved)
        }
    for stop, prev, nxt in meta:
        inbound = 0
        outbound = 0
        if prev is not None:
            inbound, _mode = _lookup(quoted, prev, lodge)
            if inbound is None:
                stop["_rest_via_lodge"] = False
                continue
        if nxt is not None:
            outbound, _mode = _lookup(quoted, lodge, nxt)
            if outbound is None:
                stop["_rest_via_lodge"] = False
                continue
        stop["_rest_via_lodge"] = (
            inbound <= PRE_DINNER_REST_LEG_CAP and outbound <= PRE_DINNER_REST_LEG_CAP
        )


def _countable_snack(stop: dict) -> bool:
    kind = stop.get("kind")
    place = stop.get("_place")
    snack_stop = kind == "snack" and not (place is not None and candidate_is_tea_place(place))
    light_stop = (
        place is not None
        and is_light_food_place(place)
        and kind in {"sight", "snack", "night"}
    )
    return bool(snack_stop or light_stop)


def drop_unrequested_snacks(plan: dict, rules: PlannerRules) -> list[str]:
    """Relaxed days drop snacks unless the user asked for food. Must-go stays."""
    if not rules.skip_unrequested_snacks or rules.food:
        return []
    notes = []
    for day in plan.get("days") or []:
        kept = []
        for stop in day.get("stops") or []:
            if not _countable_snack(stop):
                kept.append(stop)
                continue
            place = stop.get("_place")
            if place is not None and (
                place.must_include or place.place_id in rules.must_ids
            ):
                kept.append(stop)
                continue
            # A tea house can also read as light food. The user asked for it.
            if rules.tea and place is not None and candidate_is_tea_place(place):
                kept.append(stop)
                continue
            name = place.name if place is not None and getattr(place, "name", None) else stop.get("place_id")
            notes.append(f"第{day.get('day')}天去掉小吃「{name}」（轻松不单排小吃）")
        day["stops"] = kept
    return notes


def _insert_relaxed_rest(day: dict) -> str | None:
    stops = day.get("stops") or []
    lunch_at = next((index for index, stop in enumerate(stops) if stop.get("kind") == "lunch"), None)
    if lunch_at is None:
        return None
    nxt = lunch_at + 1
    if nxt < len(stops) and stops[nxt].get("kind") == "rest":
        current = stops[nxt]
        stay = 0
        try:
            stay = int(current.get("stay_minutes") or 0)
        except (TypeError, ValueError):
            stay = 0
        if stay >= 60:
            return None
        current["stay_minutes"] = 60
        current["_relaxed_rest_fill"] = True
        return f"第{day.get('day')}天午饭后加一段歇约 60 分钟"
    rest = {
        "kind": "rest",
        "place_id": None,
        "stay_minutes": 60,
        "_relaxed_rest_fill": True,
    }
    stops.insert(lunch_at + 1, rest)
    day["stops"] = stops
    return f"第{day.get('day')}天午饭后加一段歇约 60 分钟"


async def schedule_day(
    day: dict,
    *,
    rules: PlannerRules,
    lodge: CandidatePlace | None,
    protected: set[int],
    estimator,
    request: TripRequest,
    settings=None,
) -> list[str]:
    """Fill arrival times and drop unprotected sights until the hard rules hold."""
    trims: list[str] = []
    late_dinner = dinner_latest_minute(rules.end_minute)
    timeline: list[dict] = []
    start_minute = schedule_start_minute(
        rules, request, excursion=_day_is_excursion(day, request, lodge, settings)
    )
    end_minute = start_minute
    travel = 0
    missing = False
    if lodge is None and any(stop.get("kind") == "rest" for stop in day.get("stops") or []):
        if not day.get("_inplace_rest_noted"):
            trims.append(f"第{day.get('day')}天回住处歇没有住处，改为原地停留")
            day["_inplace_rest_noted"] = True
    for attempt in range(12):
        movable = [stop for stop in day.get("stops") or [] if stop.get("kind") == "rest" or stop.get("_place")]
        excursion = _day_is_excursion(day, request, lodge, settings)
        start_minute = schedule_start_minute(
            rules, request, excursion=excursion
        )
        # Relaxed city days only: the wrap clock, and a later start set by
        # _absorb_relaxed_waits.
        day_end = None if excursion else rules.relaxed_day_end_minute
        if day_end is not None:
            start_minute += int(day.get("_start_delay") or 0)
        await _assign_rest_via_lodge(movable, lodge, rules, estimator)
        dinner_from = dinner_earliest_minute(rules, has_night=day_has_night(movable))
        pairs = _movement_pairs(movable, lodge)
        resolved = await resolve_legs(
            estimator,
            pairs,
            rules.commute_mode,
            taxi_over_minutes=int(rules.taxi_over_minutes),
            taxi_ratio=float(rules.taxi_ratio),
        )
        quoted = {
            (_point_key(origin), _point_key(dest)): value
            for (origin, dest), value in zip(pairs, resolved)
        }
        groups = scenic_groups(
            [stop["_place"] for stop in movable if stop.get("_place") is not None],
            request,
        )
        timeline = []
        t = start_minute
        pos = lodge
        travel = 0
        missing = False
        depart = None
        for stop in movable:
            if stop.get("kind") == "rest":
                stay = _stay(stop)
                via_lodge = _rest_goes_via_lodge(stop, lodge)
                if via_lodge and pos is not None and _point_key(pos) != _point_key(lodge):
                    minutes, mode = _lookup(quoted, pos, lodge)
                    if minutes is None:
                        missing = True
                    if timeline:
                        timeline[-1]["move"] = (minutes, mode)
                    travel += minutes or 0
                    arrive = t + (minutes or 0)
                    stop["_rest_via_lodge"] = True
                    timeline.append({"stop": stop, "arrive": arrive, "stay": stay, "wait": 0})
                    t = arrive + stay
                    pos = lodge
                else:
                    if via_lodge:
                        stop["_rest_via_lodge"] = True
                        pos = lodge
                    else:
                        stop["_rest_via_lodge"] = False
                    timeline.append({"stop": stop, "arrive": t, "stay": stay, "wait": 0, "move": (0, "步行")})
                    t += stay
                continue
            place = stop["_place"]
            minutes, mode = _lookup(quoted, pos, place)
            if depart is None:
                depart = {"mode": mode, "minutes": minutes}
            if minutes is None:
                missing = True
            natural = t + (minutes or 0)
            travel += minutes or 0
            arrive = natural
            kind = stop.get("kind")
            if kind == "lunch":
                arrive = max(arrive, LUNCH)
            elif kind == "dinner":
                arrive = max(arrive, dinner_from)
            elif kind == "night":
                arrive = max(arrive, rules.dark_minute)
            if timeline:
                timeline[-1]["move"] = (minutes, mode)
            stay = _stay(stop)
            timeline.append({"stop": stop, "arrive": arrive, "stay": stay, "wait": arrive - natural})
            t = arrive + stay
            pos = place
        back_minutes, back_mode = (0, "步行")
        if timeline and lodge is not None and pos is not None and _point_key(pos) != _point_key(lodge):
            back_minutes, back_mode = _lookup(quoted, pos, lodge)
            if back_minutes is None:
                missing = True
            travel += back_minutes or 0
        if timeline:
            timeline[-1]["move"] = (back_minutes, back_mode)
        end_minute = t + (back_minutes or 0)

        def cluster_drop(stop) -> set[int]:
            place = stop.get("_place")
            if place is None:
                return set()
            if stop.get("kind") == "night":
                if place.place_id in protected:
                    return set()
                return {place.place_id}
            members = groups.get(place.place_id, {place.place_id})
            same_day = {
                item["stop"]["_place"].place_id
                for item in timeline
                if item["stop"].get("kind") == "sight" and item["stop"].get("_place") is not None
            }
            chosen = members & same_day
            if not chosen or any(place_id in protected for place_id in chosen):
                return set()
            return chosen

        def removable(before: int):
            for index in range(before - 1, -1, -1):
                stop = timeline[index]["stop"]
                if stop.get("kind") == "sight" and cluster_drop(stop):
                    return stop
            return None

        def optional_victim(before: int | None):
            """An optional sight or night the scenic-group rule still allows.

            Protected stops are refused by ``cluster_drop``. Lunch, dinner and
            rest are never optional victims. An index searches only the stops
            before it, walking backward: a stop after a late meal does not
            make that meal earlier. ``None`` scans the whole day backward.
            """
            ordered = range(len(timeline) - 1, -1, -1) if before is None else range(before - 1, -1, -1)
            for index in ordered:
                stop = timeline[index]["stop"]
                if stop.get("optional") is not True:
                    continue
                if stop.get("kind") not in {"sight", "night"}:
                    continue
                if cluster_drop(stop):
                    return stop
            return None

        def evening_night():
            """Night that is keeping the return late.

            An optional night goes first. Otherwise a night the user did not
            ask for can go. A night the user asked for stays protected.
            """
            nights = [
                item["stop"]
                for item in timeline
                if item["stop"].get("kind") == "night" and cluster_drop(item["stop"])
            ]
            for stop in nights:
                if stop.get("optional") is True:
                    return stop
            if rules.night or not nights:
                return None
            return nights[0]

        victim = None
        reason = ""
        drop_ids: set[int] = set()
        for index, item in enumerate(timeline):
            kind = item["stop"].get("kind")
            if kind == "lunch" and item["arrive"] > LATE_LUNCH:
                victim = optional_victim(index)
                reason = "午饭赶不上饭点，可选点" if victim is not None else ""
                if victim is None:
                    victim, reason = removable(index), "午饭赶不上饭点"
                break
            if kind == "dinner" and item["arrive"] > late_dinner:
                victim = optional_victim(index)
                reason = "晚饭赶不上饭点，可选点" if victim is not None else ""
                if victim is None:
                    victim, reason = removable(index), "晚饭赶不上饭点"
                break
        if victim is None and end_minute > rules.end_minute:
            # Evening first: dropping a sight before dinner does not shorten
            # a night that starts at dark. Then optional sights before dinner.
            dinner_at = next((index for index, item in enumerate(timeline) if item["stop"].get("kind") == "dinner"), len(timeline))
            victim = evening_night()
            if victim is not None:
                reason = "回住处太晚，可选点" if victim.get("optional") is True else "回住处太晚"
            if victim is None:
                victim = optional_victim(dinner_at)
                if victim is not None:
                    reason = "回住处太晚，可选点"
            if victim is None:
                victim, reason = removable(dinner_at), "回住处太晚"
        def limit_drop(kind_name: str, cap: int, label: str):
            chosen = [item["stop"] for item in timeline if item["stop"].get("kind") == kind_name]
            if len(chosen) <= cap:
                return None, ""
            limit_reason = f"超过每天 {cap} 个{label}"
            for stop in reversed(chosen):
                if stop.get("optional") is True and cluster_drop(stop):
                    return stop, f"{limit_reason}，可选点"
            candidates = [stop for stop in chosen if cluster_drop(stop)]
            if not candidates:
                return None, ""
            picked = min(
                candidates,
                key=lambda stop: (stop["_place"].mention_count or 0, stop["_place"].place_id),
            )
            return picked, limit_reason

        if rules.day_sight_limit is not None:
            if victim is None:
                night_cap = rules.night_limit if rules.night_limit is not None else 1
                victim, reason = limit_drop("night", night_cap, "夜景")
            if victim is None:
                victim, reason = limit_drop("sight", rules.day_sight_limit, "点")
        else:
            sights = [item["stop"] for item in timeline if item["stop"].get("kind") in {"sight", "night"}]
            if victim is None and len(sights) > rules.sight_limit:
                limit_reason = f"超过每天 {rules.sight_limit} 个点"
                victim = optional_victim(None)
                reason = f"{limit_reason}，可选点" if victim is not None else ""
                if victim is None:
                    candidates = [stop for stop in sights if stop.get("kind") == "sight" and cluster_drop(stop)]
                    if candidates:
                        victim = min(
                            candidates,
                            key=lambda stop: (stop["_place"].mention_count or 0, stop["_place"].place_id),
                        )
                        reason = limit_reason
        late_index = None
        if victim is None and day_end is not None:
            late_index = next(
                (
                    index
                    for index, item in enumerate(timeline)
                    if item["stop"].get("kind") == "sight"
                    and item["arrive"] + item["stay"] > day_end
                ),
                None,
            )
        if late_index is not None and attempt < 11:
            # Shorten first. A drop only takes the late sight or a later one,
            # and never the day's last daytime sight: the soft check notes it.
            shrunk = _shrink_for_day_end(day, timeline, late_index, day_end, rules)
            if shrunk:
                trims.extend(shrunk)
                continue
            day_sights = {
                item["stop"]["_place"].place_id
                for item in timeline
                if item["stop"].get("kind") == "sight" and item["stop"].get("_place") is not None
            }
            later = [
                item["stop"]
                for item in timeline[late_index:]
                if item["stop"].get("kind") == "sight"
                and cluster_drop(item["stop"])
                and day_sights - cluster_drop(item["stop"])
            ]
            victim = next((stop for stop in reversed(later) if stop.get("optional") is True), None)
            reason = "白天景点收得太晚，可选点" if victim is not None else ""
            if victim is None and later:
                victim, reason = later[0], "白天景点收得太晚"
        if victim is not None:
            drop_ids = {place_id for place_id in cluster_drop(victim) if place_id not in protected}
        if drop_ids and attempt < 11:
            for place_id in sorted(drop_ids):
                name = next(
                    stop["_place"].name
                    for stop in day["stops"]
                    if stop.get("_place") is not None and stop["_place"].place_id == place_id
                )
                suffix = "，同一景区一并删去" if len(drop_ids) > 1 else ""
                trims.append(f"第{day.get('day')}天删去「{name}」（{reason}{suffix}）")
            day["stops"] = [
                stop
                for stop in day["stops"]
                if stop.get("_place") is None or stop["_place"].place_id not in drop_ids
            ]
            continue
        if attempt < 11:
            fills = await _fill_meal_gaps(
                day, timeline, rules=rules, lodge=lodge, estimator=estimator,
                day_end=day_end,
            )
            if fills:
                trims.extend(fills)
                continue
            if day_end is not None:
                absorbed = _absorb_relaxed_waits(
                    day,
                    timeline,
                    start_minute=start_minute,
                    can_delay=parse_hhmm(request.daily_start) is None,
                )
                if absorbed:
                    trims.extend(absorbed)
                    continue
            if rules.require_day_rest and not excursion:
                from src.agents.route_planner.checks import day_has_qualifying_rest

                if not day_has_qualifying_rest(day.get("stops") or [], rules):
                    inserted = _insert_relaxed_rest(day)
                    if inserted:
                        trims.append(inserted)
                        continue
        break
    for item in timeline:
        item["stop"]["time"] = _fmt(item["arrive"])
        item["stop"]["wait_minutes"] = item["wait"]
        minutes, mode = item.get("move") or (0, "步行")
        item["stop"]["move_to_next"] = {"mode": mode, "minutes": minutes}
    day["_return_minute"] = end_minute
    day["_start_minute"] = start_minute
    day["_travel_minutes"] = travel
    day["_route_missing"] = missing
    day["_depart_move"] = depart or {"mode": "步行", "minutes": 0}
    hops = []
    for (origin, dest), choice in zip(pairs, resolved):
        minutes, label = choice
        hops.append(
            _chosen_hop(
                estimator,
                origin,
                dest,
                minutes,
                label,
                source=getattr(choice, "source", "amap"),
            )
        )
    day["_chosen_hops"] = hops
    return trims


def _meal_anchor(stops: list[dict], index: int, lodge: CandidatePlace | None):
    for stop in reversed(stops[:index]):
        if stop.get("_place") is not None and stop.get("kind") not in {"lunch", "dinner", "snack", "rest"}:
            return stop["_place"]
    return lodge


def _next_anchor(stops: list[dict], index: int):
    for stop in stops[index + 1 :]:
        if stop.get("_place") is not None and stop.get("kind") not in {"lunch", "dinner", "snack", "rest"}:
            return stop["_place"]
    return None


async def repick_meals(
    day: dict,
    lodge: CandidatePlace | None,
    *,
    fetch_restaurants,
    session,
    city: str,
    used_ids: set[int],
) -> list[str]:
    """Replace a meal that is far from both neighbors."""
    changes = []
    stops = day.get("stops") or []
    for index, stop in enumerate(stops):
        if stop.get("kind") not in {"lunch", "dinner"}:
            continue
        prev = _meal_anchor(stops, index, lodge)
        nxt = _next_anchor(stops, index)
        current = stop.get("_place")
        if prev is None:
            continue
        if (
            current is not None
            and current.place_type == "restaurant"
            and not is_light_food_place(current)
            and not _closed(current)
            and any(
                neighbor is not None and (distance := hav_km(neighbor, current)) is not None and distance <= MEAL_NEAR_KM
                for neighbor in (prev, nxt)
            )
        ):
            continue
        lat, lng = _coords(prev)
        if lat is None:
            continue
        exclude = set(used_ids)
        if current is not None:
            exclude.add(current.place_id)
        try:
            found = await fetch_restaurants(
                session,
                city=city,
                latitude=lat,
                longitude=lng,
                radius_meters=1500,
                limit=5,
                exclude_place_ids=sorted(exclude),
            )
        except Exception:
            continue
        near = []
        for item in found or []:
            place = _as_candidate(item)
            if place.place_id in exclude or _closed(place) or is_light_food_place(place):
                continue
            distance = hav_km(prev, place)
            if distance is None or distance > MEAL_NEAR_KM:
                continue
            near.append(place)
        if not near:
            continue
        best = min(
            near,
            key=lambda place: (
                0 if place.top_reasons else 1,
                -_rating(place),
                hav_km(prev, place) or 0,
                place.place_id,
            ),
        )
        meal = "午饭" if stop["kind"] == "lunch" else "晚饭"
        old_name = current.name if current is not None else stop.get("place_id")
        changes.append(f"第{day.get('day')}天{meal}由「{old_name}」改为离上一站近的「{best.name}」")
        stop["place_id"] = best.place_id
        stop["_place"] = best
        used_ids.add(best.place_id)
    return changes


def _meal_stop(stops: list[dict], kind: str) -> dict | None:
    return next((stop for stop in stops if stop.get("kind") == kind), None)


def _attached_place(stop: dict | None) -> CandidatePlace | None:
    if stop is None:
        return None
    place = stop.get("_place")
    return place if isinstance(place, CandidatePlace) else None


def _must_meal(place: CandidatePlace, rules: PlannerRules) -> bool:
    return bool(place.must_include) or place.place_id in rules.must_ids


def _visitor_proper_meal(place: CandidatePlace | None, amap_ids: set[int]) -> bool:
    return bool(place and _proper_meal(place) and _visitor_meal(place, amap_ids))


def _pot_swap_choice(
    lunch: dict,
    dinner: dict,
    *,
    rules: PlannerRules,
    amap_ids: set[int],
    meals: list[dict],
) -> dict | None:
    """Which of the two pot meals to replace.

    The meal without visitor evidence goes first. When both have it, or
    neither does, dinner goes first. A must-go shop stays. When the user
    asked for food, the swap must leave at least one visitor meal.
    """
    lunch_place = _attached_place(lunch)
    dinner_place = _attached_place(dinner)
    lunch_visitor = _visitor_proper_meal(lunch_place, amap_ids)
    dinner_visitor = _visitor_proper_meal(dinner_place, amap_ids)
    if lunch_visitor != dinner_visitor:
        ordered = (lunch, dinner) if not lunch_visitor else (dinner, lunch)
    else:
        ordered = (dinner, lunch)
    for stop in ordered:
        place = _attached_place(stop)
        if place is not None and _must_meal(place, rules):
            continue
        if rules.food and _visitor_proper_meal(place, amap_ids):
            others = sum(
                1
                for other in meals
                if other is not stop and _visitor_proper_meal(_attached_place(other), amap_ids)
            )
            if others < 1:
                continue
        return stop
    return None


async def _nearby_non_pot(
    anchor: CandidatePlace,
    *,
    fetch_restaurants,
    session,
    city: str,
    exclude: set[int],
) -> CandidatePlace | None:
    """Same nearby lookup as ``repick_meals``, limited to a non-pot proper meal."""
    lat, lng = _coords(anchor)
    if lat is None:
        return None
    try:
        found = await fetch_restaurants(
            session,
            city=city,
            latitude=lat,
            longitude=lng,
            radius_meters=1500,
            limit=5,
            exclude_place_ids=sorted(exclude),
        )
    except Exception:
        return None
    near = []
    for item in found or []:
        place = _as_candidate(item)
        if place.place_id in exclude or not _proper_meal(place) or is_pot_meal(place):
            continue
        distance = hav_km(anchor, place)
        if distance is None or distance > MEAL_NEAR_KM:
            continue
        near.append(place)
    if not near:
        return None
    return min(
        near,
        key=lambda place: (
            0 if place.top_reasons else 1,
            -_rating(place),
            hav_km(anchor, place) or 0,
            place.place_id,
        ),
    )


async def diversify_same_day_pots(
    plan: dict,
    lodge: CandidatePlace | None,
    *,
    rules: PlannerRules,
    fetch_restaurants,
    session,
    city: str,
    used_ids: set[int],
    amap_ids: set[int] | None = None,
    swapped_days: list[dict] | None = None,
) -> list[str]:
    """Replace one pot meal when the same day's lunch and dinner are both pots.

    A day that cannot be swapped is left for the check. Each swapped day is
    added to ``swapped_days`` so the caller can schedule it again: the legs
    and arrival times were computed for the old shop.
    """
    blocked = set(amap_ids or ())
    changes = []
    days = plan.get("days") or []
    meals = [
        stop
        for day in days
        for stop in day.get("stops") or []
        if stop.get("kind") in {"lunch", "dinner"}
    ]
    for day in days:
        stops = day.get("stops") or []
        lunch = _meal_stop(stops, "lunch")
        dinner = _meal_stop(stops, "dinner")
        if lunch is None or dinner is None:
            continue
        if not is_pot_meal(_attached_place(lunch)) or not is_pot_meal(_attached_place(dinner)):
            continue
        target = _pot_swap_choice(
            lunch, dinner, rules=rules, amap_ids=blocked, meals=meals,
        )
        if target is None:
            continue
        index = stops.index(target)
        anchor = _meal_anchor(stops, index, lodge)
        if anchor is None:
            continue
        current = _attached_place(target)
        exclude = set(used_ids)
        if current is not None:
            exclude.add(current.place_id)
        best = await _nearby_non_pot(
            anchor,
            fetch_restaurants=fetch_restaurants,
            session=session,
            city=city,
            exclude=exclude,
        )
        if best is None:
            continue
        meal = "午饭" if target.get("kind") == "lunch" else "晚饭"
        old_name = current.name if current is not None else target.get("place_id")
        changes.append(f"第{day.get('day')}天{meal}由「{old_name}」改为不同口味的「{best.name}」")
        target["place_id"] = best.place_id
        target["_place"] = best
        used_ids.add(best.place_id)
        if swapped_days is not None:
            swapped_days.append(day)
    return changes


def _visitor_meal(place: CandidatePlace, amap_ids: set[int]) -> bool:
    if place.place_id in amap_ids or is_light_food_place(place) or candidate_is_tea_place(place):
        return False
    if place.place_type not in {"restaurant", "food"}:
        return False
    if (place.mention_count or 0) > 0:
        return True
    return bool(place.top_reasons)


def _proper_meal(place: CandidatePlace | None) -> bool:
    return bool(
        place
        and place.place_type in {"restaurant", "food"}
        and not is_light_food_place(place)
        and not candidate_is_tea_place(place)
        and not _closed(place)
    )


def _idle_fill_counts(plan: dict | None) -> tuple[int, int]:
    rests = 0
    extends = 0
    if not isinstance(plan, dict):
        return 0, 0
    for day in plan.get("days") or []:
        for stop in day.get("stops") or []:
            if stop.get("kind") == "rest" and stop.get("_idle_fill"):
                rests += 1
            extra = stop.get("_extended_minutes")
            if isinstance(extra, int) and extra > 0:
                extends += 1
    return rests, extends
