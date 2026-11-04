"""Route acceptance checks, day class and stop wording."""

from __future__ import annotations

from src.agents.composition_blueprint import is_light_food_place

from src.agents.accommodation_policy import day_policy

from src.agents.preferences import (
    candidate_is_family_place,
    candidate_is_night_place,
    candidate_is_tea_place,
)

from src.agents.schema import (
    AccommodationPlanningContext,
    CandidatePlace,
    TripRequest,
)

from src.config import Settings

from src.agents.route_planner.rules import (
    LUNCH,
    LATE_LUNCH,
    WALK_CAP_MINUTES,
    IDLE_LIMIT_MINUTES,
    PlannerRules,
    _fmt,
    _clock,
    day_has_night,
    dinner_earliest_minute,
    dinner_latest_minute,
)

from src.agents.route_planner.places import (
    _coords,
    coerce_place_id,
    _closed,
)

from src.agents.route_planner.schedule import (
    _visitor_meal,
    _proper_meal,
)



def _day_stop_places(day: dict) -> list[CandidatePlace]:
    places = []
    for stop in day.get("stops") or []:
        place = stop.get("_place")
        if isinstance(place, CandidatePlace) and _coords(place)[0] is not None:
            places.append(place)
    return places


def classify_day(places: list[CandidatePlace], request: TripRequest, settings: Settings, lodge: CandidatePlace | None):
    """Same excursion rule as ``day_policy``. The lodging coordinate stands in for the city core.

    The canonical core is a cluster of accommodation areas loaded from the
    database. This module does not open a session, so a known lodging point is
    the offline center. No lodging leaves ``day_kind`` unknown, which is not
    an excursion.
    """
    core = None
    if lodge is not None and _coords(lodge)[0] is not None:
        core = (float(lodge.latitude), float(lodge.longitude))
    context = AccommodationPlanningContext(
        state="fixed" if core is not None else "auto_unavailable",
        urban_core=core,
    )
    return day_policy(places, request, settings, context, None)


def _commute_class(
    day: dict,
    rules: PlannerRules,
    lodge: CandidatePlace | None,
    request: TripRequest | None,
    settings: Settings | None,
) -> tuple[str, int]:
    preset = day.get("_day_kind")
    if preset == "excursion":
        return "excursion", int(day.get("_daily_limit") or rules.commute_budget)
    if preset in {"urban", "unknown"}:
        return "urban", int(rules.urban_commute_cap)
    if request is None or settings is None:
        return "urban", int(rules.urban_commute_cap)
    policy = classify_day(_day_stop_places(day), request, settings, lodge)
    if policy.day_kind == "excursion":
        return "excursion", int(policy.daily_limit_minutes)
    return "urban", int(rules.urban_commute_cap)


def urban_commute_notes(
    plan: dict,
    rules: PlannerRules,
    *,
    lodge: CandidatePlace | None = None,
    request: TripRequest | None = None,
    settings: Settings | None = None,
) -> list[str]:
    """City days over the scoring budget, including over the cap, are a note.

    The first check still reports a city day over the cap so the model gets one
    repair. After that repair the same sentence is the only record.
    """
    notes = []
    for day in plan.get("days") or []:
        travel = day.get("_travel_minutes")
        if not isinstance(travel, int):
            continue
        kind, _limit = _commute_class(day, rules, lodge, request, settings)
        if kind == "excursion":
            continue
        if travel > rules.commute_budget:
            notes.append(f"第{day.get('day')}天路上约 {travel} 分钟，偏长")
    return notes


def _soften_urban_commute_issues(issues: list[str]) -> list[str]:
    """After the one repair, a city day over the cap is no longer a failure.

    An excursion day over its own limit still says 远郊上限 and stays a failure.
    """
    kept = []
    for issue in issues:
        if "路上总时间" in issue and "远郊上限" not in issue:
            continue
        kept.append(issue)
    return kept


def _estimated_leg_count(plan: dict | None) -> int:
    """Fallback estimates on the final arrangement. Same-place walks are not included."""
    if not isinstance(plan, dict):
        return 0
    count = 0
    for day in plan.get("days") or []:
        for hop in day.get("_chosen_hops") or []:
            if isinstance(hop, dict) and hop.get("source") == "estimate":
                count += 1
    return count


def _stop_label(stop: dict) -> str:
    place = stop.get("_place")
    name = getattr(place, "name", None) if place is not None else None
    if isinstance(name, str) and name.strip():
        return name.strip()
    labels = {"lunch": "午饭", "dinner": "晚饭", "sight": "景点", "night": "夜景", "snack": "小吃", "rest": "歇脚"}
    return labels.get(str(stop.get("kind") or ""), "这一站")


def _lodge_label(lodge: CandidatePlace | None) -> str:
    name = getattr(lodge, "name", None) if lodge is not None else None
    if isinstance(name, str) and name.strip():
        return name.strip()
    return "住处"


def _move_ends(stops: list[dict], index: int, lodge_name: str) -> tuple[str, str]:
    """Name the leg stored on this stop.

    A rest that actually goes back to lodging holds the outbound hop from
    lodging. The previous stop then holds the inbound hop to lodging.
    An in-place rest still holds the hop that skipped it.
    """
    stop = stops[index]
    via_lodge = bool(stop.get("_rest_via_lodge")) if stop.get("kind") == "rest" else False
    if stop.get("kind") == "rest":
        origin = "住处" if via_lodge else lodge_name
        if not via_lodge:
            for prev in reversed(stops[:index]):
                if prev.get("kind") == "rest":
                    continue
                origin = _stop_label(prev)
                break
        dest = "回住处"
        for nxt in stops[index + 1 :]:
            if nxt.get("kind") == "rest":
                continue
            dest = _stop_label(nxt)
            break
        return origin, dest
    origin = _stop_label(stop)
    dest = "回住处"
    for nxt in stops[index + 1 :]:
        if nxt.get("kind") == "rest":
            if nxt.get("_rest_via_lodge"):
                dest = "住处"
                break
            continue
        dest = _stop_label(nxt)
        break
    return origin, dest


def _first_stop_label(stops: list[dict]) -> str:
    for stop in stops:
        if stop.get("kind") == "rest":
            continue
        return _stop_label(stop)
    return "下一站"


def _leg_text(origin: str, dest: str, mode: object, minutes: object) -> str:
    text = f"{origin}→{dest}"
    if isinstance(mode, str) and mode:
        text += f" {mode}"
    if isinstance(minutes, int) and not isinstance(minutes, bool):
        text += f" {minutes} 分钟"
    return text


def _longest_leg_clause(day: dict, lodge: CandidatePlace | None) -> str:
    """Up to two slowest named legs, longest first. Empty when the day has no minutes."""
    stops = list(day.get("stops") or [])
    lodge_name = _lodge_label(lodge)
    found: list[tuple[int, str]] = []
    depart = day.get("_depart_move")
    if isinstance(depart, dict) and isinstance(depart.get("minutes"), int) and not isinstance(depart.get("minutes"), bool):
        found.append((
            int(depart["minutes"]),
            _leg_text(lodge_name, _first_stop_label(stops), depart.get("mode"), depart.get("minutes")),
        ))
    for index, stop in enumerate(stops):
        move = stop.get("move_to_next")
        if not isinstance(move, dict):
            continue
        minutes = move.get("minutes")
        if not isinstance(minutes, int) or isinstance(minutes, bool):
            continue
        origin, dest = _move_ends(stops, index, lodge_name)
        found.append((int(minutes), _leg_text(origin, dest, move.get("mode"), minutes)))
    if not found:
        return ""
    found.sort(key=lambda item: item[0], reverse=True)
    chosen = [text for _minutes, text in found[:2]]
    title = "最长的两段" if len(chosen) > 1 else "最长的一段"
    return title + "：" + "、".join(chosen)


def _is_real_sight(stop: dict, place: CandidatePlace | None) -> bool:
    if stop.get("kind") not in {"sight", "night"} or place is None:
        return False
    if place.place_type in {"restaurant", "food", "cafe", "snack"} or is_light_food_place(place):
        return False
    return True


def collect_issues(
    plan: dict,
    rules: PlannerRules,
    by_id: dict[int, CandidatePlace],
    amap_ids: set[int] | None = None,
    *,
    lodge: CandidatePlace | None = None,
    request: TripRequest | None = None,
    settings: Settings | None = None,
) -> list[str]:
    """Every acceptance check. This does not call a model."""
    issues: list[str] = []
    amap_ids = amap_ids or set()
    days = plan.get("days") or []
    numbers = []
    for day in days:
        try:
            numbers.append(int(day.get("day")))
        except (TypeError, ValueError):
            numbers.append(day.get("day"))
    if sorted(numbers) != list(range(1, rules.days + 1)):
        issues.append(f"行程天数不对：要 {rules.days} 天，实际是第 {numbers} 天")
    seen: dict[int, int] = {}
    night_ok = False
    food_ok = False
    tea_ok = False
    family_ok = False
    scheduled: set[int] = set()
    for day in days:
        number = day.get("day")
        stops = day.get("stops") or []
        sights = 0
        snacks: list[str] = []
        dinner_at = next((index for index, stop in enumerate(stops) if stop.get("kind") == "dinner"), None)
        has_night = day_has_night(stops)
        lodge_name = _lodge_label(lodge)
        named_gap = False
        for index, stop in enumerate(stops):
            if stop.get("kind") == "rest":
                move = stop.get("move_to_next") or {}
                minutes = move.get("minutes")
                origin, dest = _move_ends(stops, index, lodge_name)
                if minutes is None and move:
                    mode = move.get("mode")
                    mode_text = f" {mode}" if isinstance(mode, str) and mode else ""
                    issues.append(f"第{number}天没有查到路：{origin}→{dest}{mode_text}")
                    named_gap = True
                elif move.get("mode") == "步行" and isinstance(minutes, int) and minutes > WALK_CAP_MINUTES:
                    issues.append(f"第{number}天步行 {minutes} 分钟，超过 {WALK_CAP_MINUTES} 分钟：{origin}→{dest}")
                continue
            place = stop.get("_place")
            if place is None:
                token = coerce_place_id(stop.get("place_id"))
                place = by_id.get(token) if isinstance(token, int) else None
            if place is not None:
                scheduled.add(place.place_id)
                if place.place_id in seen:
                    issues.append(f"第{number}天重复安排了编号 {place.place_id}")
                seen[place.place_id] = number
                if _closed(place):
                    issues.append(f"第{number}天「{place.name}」名字带停业字样")
            kind = stop.get("kind")
            if _is_real_sight(stop, place):
                sights += 1
            if kind in {"sight", "night"} and place is not None and (
                place.place_type in {"restaurant", "food", "cafe", "snack"} or is_light_food_place(place)
            ) and not candidate_is_tea_place(place):
                issues.append(f"第{number}天餐饮店不当景点：「{place.name}」")
            snack_stop = kind == "snack" and not (place is not None and candidate_is_tea_place(place))
            light_stop = (
                place is not None
                and is_light_food_place(place)
                and kind in {"sight", "snack", "night"}
            )
            if snack_stop or light_stop:
                snacks.append(place.name if place is not None else str(stop.get("place_id")))
            if kind in {"lunch", "dinner"}:
                label = "午饭" if kind == "lunch" else "晚饭"
                if not _proper_meal(place):
                    issues.append(f"第{number}天{label}不是正经吃饭的店")
                elif _visitor_meal(place, amap_ids):
                    food_ok = True
                at = _clock(stop.get("time"))
                window = (
                    (LUNCH, LATE_LUNCH)
                    if kind == "lunch"
                    else (
                        dinner_earliest_minute(rules, has_night=has_night),
                        dinner_latest_minute(rules.end_minute),
                    )
                )
                if at is not None and not window[0] <= at <= window[1]:
                    issues.append(f"第{number}天{label}是 {_fmt(at)}，不在饭点")
            if kind == "night":
                if place is None or not candidate_is_night_place(place):
                    issues.append(f"第{number}天夜景没写到夜景、亮灯或夜游")
                else:
                    night_ok = True
                if dinner_at is not None and index < dinner_at:
                    issues.append(f"第{number}天夜景排在晚饭之前")
                if index != len(stops) - 1:
                    issues.append(f"第{number}天夜景不是最后一站")
                at = _clock(stop.get("time"))
                if at is not None and at < rules.dark_minute:
                    issues.append(f"第{number}天夜景是 {_fmt(at)}，天还没黑")
            if place is not None and candidate_is_family_place(place) and kind in {"sight", "night"}:
                family_ok = True
            if place is not None and candidate_is_tea_place(place) and kind not in {"lunch", "dinner"}:
                tea_ok = True
            wait = stop.get("wait_minutes")
            if isinstance(wait, int) and wait > IDLE_LIMIT_MINUTES:
                where = place.name if place is not None and getattr(place, "name", None) else _stop_label(stop)
                issues.append(f"第{number}天空等 {wait} 分钟，超过 1 小时：在「{where}」")
            move = stop.get("move_to_next") or {}
            minutes = move.get("minutes")
            origin, dest = _move_ends(stops, index, lodge_name)
            if minutes is None and move:
                mode = move.get("mode")
                mode_text = f" {mode}" if isinstance(mode, str) and mode else ""
                issues.append(f"第{number}天没有查到路：{origin}→{dest}{mode_text}")
                named_gap = True
            elif move.get("mode") == "步行" and isinstance(minutes, int) and minutes > WALK_CAP_MINUTES:
                issues.append(f"第{number}天步行 {minutes} 分钟，超过 {WALK_CAP_MINUTES} 分钟：{origin}→{dest}")
        if dinner_at is not None:
            for stop in stops[dinner_at + 1 :]:
                if stop.get("kind") not in {"night", "rest"}:
                    issues.append(f"第{number}天晚饭后只排夜景")
                    break
        if sights == 0:
            issues.append(f"第{number}天没有游览点")
        if len(snacks) > 1:
            issues.append(f"第{number}天有 {len(snacks)} 个小吃或补充能量点（{'、'.join(snacks)}），最多 1 个")
        if sights > rules.sight_limit:
            issues.append(f"第{number}天游览点 {sights} 个，超过{rules.pace_label}节奏的 {rules.sight_limit} 个")
        travel = day.get("_travel_minutes")
        if isinstance(travel, int):
            kind, limit = _commute_class(day, rules, lodge, request, settings)
            detail = _longest_leg_clause(day, lodge)
            suffix = f"；{detail}" if detail else ""
            if kind == "excursion" and travel > limit:
                issues.append(f"第{number}天路上总时间 {travel} 分钟，超过远郊上限 {limit} 分钟{suffix}")
            elif kind != "excursion" and travel > rules.urban_commute_cap:
                issues.append(f"第{number}天路上总时间 {travel} 分钟，超过 {rules.urban_commute_cap} 分钟{suffix}")
        end = day.get("_return_minute")
        if isinstance(end, int) and end > rules.end_minute:
            issues.append(f"第{number}天回到住处约 {_fmt(end)}，超过 {_fmt(rules.end_minute)}")
        depart = day.get("_depart_move") or {}
        depart_minutes = depart.get("minutes")
        depart_missing = depart_minutes is None and bool(depart)
        if day.get("_route_missing") and not named_gap and not depart_missing:
            issues.append(f"第{number}天没有查到路")
        first_name = _first_stop_label(stops)
        if depart_missing:
            mode = depart.get("mode")
            mode_text = f" {mode}" if isinstance(mode, str) and mode else ""
            issues.append(f"第{number}天没有查到路：{lodge_name}→{first_name}{mode_text}")
        elif depart.get("mode") == "步行" and isinstance(depart_minutes, int) and depart_minutes > WALK_CAP_MINUTES:
            issues.append(
                f"第{number}天步行 {depart_minutes} 分钟，超过 {WALK_CAP_MINUTES} 分钟：{lodge_name}→{first_name}"
            )
        has_lunch = any(stop.get("kind") == "lunch" for stop in stops)
        has_dinner = any(stop.get("kind") == "dinner" for stop in stops)
        if not has_lunch:
            issues.append(f"第{number}天缺少午饭")
        if rules.dinner and not has_dinner:
            issues.append(f"第{number}天缺少晚饭")
    if rules.night and not night_ok:
        issues.append("用户想看夜景，但没有排一个写到夜景的地点")
    if rules.food and not food_ok:
        issues.append("用户喜欢美食，但没有一顿正餐是网友提过的店")
    if rules.tea and not tea_ok:
        issues.append("用户想去茶馆喝茶，但没有安排茶馆")
    if rules.family and not family_ok:
        issues.append("亲子出行，但没有孩子能玩的地方")
    for place_id in rules.must_ids:
        if place_id not in scheduled:
            place = by_id.get(place_id)
            issues.append(f"必去点「{place.name if place is not None else place_id}」没有排上")
    return list(dict.fromkeys(issues))


