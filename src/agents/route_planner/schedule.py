"""Day scheduling, meal windows and idle-time fills."""

from __future__ import annotations

from src.agents.composition_blueprint import is_light_food_place

from src.agents.preferences import candidate_is_tea_place

from src.agents.schema import (
    CandidatePlace,
    TripRequest,
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


def _movement_pairs(stops: list[dict], lodge: CandidatePlace | None) -> list[tuple]:
    pairs = []
    pos = lodge
    for stop in stops:
        if stop.get("kind") == "rest":
            if lodge is None:
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


def _extend_prev_sight(timeline: list[dict], meal_index: int, wait: int, day_number) -> str | None:
    meal = timeline[meal_index]["stop"]
    meal_label = "午饭" if meal.get("kind") == "lunch" else "晚饭"
    sight = None
    for item in reversed(timeline[:meal_index]):
        stop = item.get("stop") or {}
        if stop.get("kind") == "sight" and stop.get("_place") is not None:
            sight = stop
            break
    if sight is None:
        return None
    already = sight.get("_extended_minutes")
    already = already if isinstance(already, int) and already > 0 else 0
    current = _stay(sight)
    extra = min(SIGHT_EXTEND_CAP - already, 240 - current, wait)
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
        extended = _extend_prev_sight(timeline, index, wait, day.get("day"))
        if extended:
            notes.append(extended)
    return notes


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
        start_minute = schedule_start_minute(
            rules, request, excursion=_day_is_excursion(day, request, lodge, settings)
        )
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
                via_lodge = lodge is not None
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
                day, timeline, rules=rules, lodge=lodge, estimator=estimator
            )
            if fills:
                trims.extend(fills)
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


