"""Route acceptance checks, day class and stop wording."""

from __future__ import annotations

import re
from typing import NamedTuple

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
    RoutePlan,
    TripRequest,
)

from src.config import Settings

from src.agents.route_planner.rules import (
    LATEST_RETURN_MINUTE,
    return_limit_minute,
    LUNCH,
    LATE_LUNCH,
    ALWAYS_WALK_KM,
    WALK_CAP_MINUTES,
    IDLE_LIMIT_MINUTES,
    PlannerRules,
    _fmt,
    _clock,
    day_has_night,
    dinner_earliest_minute,
    dinner_latest_minute,
    is_pot_meal,
)

from src.agents.route_planner.places import (
    _coords,
    coerce_place_id,
    _closed,
)

from src.agents.route_planner.schedule import (
    _stay,
    _visitor_meal,
    _proper_meal,
)
from src.agents.route_planner.scenic import included_scenic_places


_OPENING_ISSUE_PREFIX = "开放时间冲突："
_DAY_PREFIX = re.compile(r"^第\d+天")
_HARD_DAY_ISSUE = re.compile(
    r"(?:重复安排了编号 \d+$|「.*」名字带停业字样$"
    r"|「.*」.*晚于 \d+:\d{2} (?:停止入场|关门)$"
    r"|餐饮店不当景点：|(?:午饭|晚饭)不是正经吃饭的店$"
    r"|夜景没写到夜景、亮灯或夜游$|夜景是 \d+:\d{2}，天还没黑$"
    r"|没有查到路(?:：|$)|步行 \d+ 分钟，超过 \d+ 分钟："
    r"|没有游览点$|缺少午饭$)"
)
_RETURN_ISSUE = re.compile(r"^(?:没排夜景，)?回到住处约 (\d+):(\d{2})，超过 ")
_REPAIR_PREFIXES = ("用户想看夜景，", "用户喜欢美食，", "用户想去茶馆喝茶，", "亲子出行，")


class RouteIssueTiers(NamedTuple):
    hard: list[str]
    repair: list[str]
    record: list[str]


def classify_issues(issues: list[str]) -> RouteIssueTiers:
    """Group the shared check messages without matching words in place names."""
    tiers = RouteIssueTiers([], [], [])
    for issue in issues:
        day_issue = _DAY_PREFIX.sub("", issue, count=1)
        returned = _RETURN_ISSUE.match(day_issue)
        if (
            issue.startswith(("行程天数不对：", _OPENING_ISSUE_PREFIX))
            or _HARD_DAY_ISSUE.match(day_issue)
            or (returned and int(returned[1]) * 60 + int(returned[2]) > LATEST_RETURN_MINUTE)
        ):
            tiers.hard.append(issue)
        elif issue.startswith(_REPAIR_PREFIXES) or (
            issue.startswith("必去点「") and issue.endswith("」没有排上")
        ):
            tiers.repair.append(issue)
        else:
            tiers.record.append(issue)
    return tiers


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
    """Record city days over the scoring budget up to the separate cap."""
    notes = []
    for day in plan.get("days") or []:
        travel = day.get("_travel_minutes")
        if not isinstance(travel, int):
            continue
        kind, _limit = _commute_class(day, rules, lodge, request, settings)
        if kind == "excursion":
            continue
        if rules.commute_budget < travel <= rules.urban_commute_cap:
            notes.append(f"第{day.get('day')}天路上约 {travel} 分钟，偏长")
    return notes


def missing_preference_notes(rules: PlannerRules) -> list[str]:
    """Asked-for preferences the candidate pool cannot satisfy.

    These are notes, same channel as ``urban_commute_notes``. They do not
    make the plan unqualified.
    """
    notes = []
    if rules.night and not rules.night_candidate:
        notes.append("候选里没有带夜景依据的地点，这次没排夜景")
    if rules.food and not rules.food_candidate:
        notes.append("候选里没有网友提过的正餐，这次没排网友提过的店")
    if rules.tea and not rules.tea_candidate:
        notes.append("候选里没有茶馆，这次没排茶馆")
    if rules.family and not rules.family_candidate:
        notes.append("候选里没有孩子能玩的地方，这次没排亲子点")
    return notes


def pot_pair_note(number, lunch_name: str, dinner_name: str) -> str:
    return f"第{number}天午饭和晚饭都是锅子：{lunch_name}、{dinner_name}"


def _meal_stop(stops: list[dict], kind: str) -> dict | None:
    return next((stop for stop in stops if stop.get("kind") == kind), None)


def _resolved_place(stop: dict | None, by_id: dict[int, CandidatePlace]) -> CandidatePlace | None:
    if stop is None:
        return None
    place = stop.get("_place")
    if isinstance(place, CandidatePlace):
        return place
    token = coerce_place_id(stop.get("place_id"))
    found = by_id.get(token) if isinstance(token, int) else None
    return found if isinstance(found, CandidatePlace) else None


def _place_name(place: CandidatePlace | None, fallback: str) -> str:
    name = getattr(place, "name", None) if place is not None else None
    if isinstance(name, str) and name.strip():
        return name.strip()
    return fallback


def _estimated_leg_count(plan: dict | None) -> int:
    """Fallback estimates on the final arrangement. Same-place walks are not included."""
    if not isinstance(plan, dict):
        return 0
    count = 0
    for day in plan.get("days") or []:
        for hop in day.get("_chosen_hops") or []:
            if (
                isinstance(hop, dict)
                and hop.get("source") == "estimate"
                and hop.get("from_id") != hop.get("to_id")
            ):
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


def _stop_stay_minutes(stop: dict) -> int:
    """Same default and clamp as schedule._stay, so the check sees the scheduled stay."""
    try:
        minutes = int(stop.get("stay_minutes") or 60)
    except (TypeError, ValueError):
        minutes = 60
    return min(max(minutes, 20), 240)


def day_has_qualifying_rest(stops: list[dict], rules: PlannerRules) -> bool:
    """A rest of 60+ minutes, or a tea house of 60+ when the user asked for tea."""
    for stop in stops or []:
        stay = _stop_stay_minutes(stop)
        if stay < 60:
            continue
        if stop.get("kind") == "rest":
            return True
        if not rules.tea:
            continue
        place = stop.get("_place")
        if (
            isinstance(place, CandidatePlace)
            and candidate_is_tea_place(place)
            and stop.get("kind") not in {"lunch", "dinner"}
        ):
            return True
    return False


def _day_is_urban(
    day: dict,
    rules: PlannerRules,
    lodge: CandidatePlace | None,
    request: TripRequest | None,
    settings: Settings | None,
) -> bool:
    kind, _limit = _commute_class(day, rules, lodge, request, settings)
    return kind != "excursion"


def rest_gap_note(number) -> str:
    return f"第{number}天没有一段至少 60 分钟的歇"


def day_end_note(number, name: str, end_at: int, limit: int) -> str:
    return f"第{number}天「{name}」约 {_fmt(end_at)} 结束，超过白天 {_fmt(limit)}"


def _return_overrun(day: dict, rules: PlannerRules, *, urban: bool) -> tuple[int, str] | None:
    pace_limit = return_limit_minute(rules, urban=urban, has_night=day_has_night(day.get("stops")))
    limit = min(pace_limit, LATEST_RETURN_MINUTE)
    end = day.get("_return_minute")
    if not isinstance(end, int) or end <= limit:
        return None
    prefix = "没排夜景，" if pace_limit < rules.end_minute else ""
    return end - limit, f"第{day.get('day')}天{prefix}回到住处约 {_fmt(end)}，超过 {_fmt(limit)}"


def attach_schedule_notice_facts(
    route: RoutePlan,
    plan: dict,
    rules: PlannerRules,
    by_id: dict[int, CandidatePlace],
    *,
    lodge: CandidatePlace | None,
    request: TripRequest,
    settings: Settings,
) -> None:
    """Keep the chosen schedule's facts for the fixed-sentence channel."""
    scheduled: set[int] = set()
    for day, group in zip(plan["days"], route.day_groups):
        scheduled.update(place.place_id for place in group.places)
        scheduled.update(
            place.place_id for stop in day.get("stops") or [] for place in included_scenic_places(stop)
        )
        kind, limit = _commute_class(day, rules, lodge, request, settings)
        travel = day.get("_travel_minutes")
        group.excursion_commute_over_limit = kind == "excursion" and isinstance(travel, int) and travel > limit
        returned = day.get("_return_minute")
        group.return_after_pace_limit = (
            lodge is not None and group.timeline is not None
            and isinstance(returned, int) and returned <= LATEST_RETURN_MINUTE
            and _return_overrun(day, rules, urban=kind != "excursion") is not None
        )
    requested_names = {item.place_id: item.name for item in request.must_include}
    for place_id in rules.must_ids:
        if place_id in scheduled:
            continue
        place = by_id.get(place_id)
        name = place.name if place is not None else requested_names.get(place_id)
        if not name:
            continue
        closed = place is not None and all(
            rules.opening_hours.for_place(number, place).closed for number in range(1, rules.days + 1)
        )
        if name not in route.missed_must_visits or not closed:
            route.missed_must_visits[name] = "closed_all_days" if closed else None


def _walk_exceeds_cap(move: dict) -> bool:
    minutes = move.get("minutes")
    distance = move.get("_straight_km")
    return (
        move.get("mode") == "步行"
        and isinstance(minutes, int)
        and minutes > WALK_CAP_MINUTES
        and (distance is None or distance > ALWAYS_WALK_KM)
    )


def _opening_issues(rules: PlannerRules, number, stop: dict, place: CandidatePlace) -> list[str]:
    limits = rules.opening_hours.for_place(number, place)
    label = f"{_OPENING_ISSUE_PREFIX}第{number}天「{place.name}」"
    if limits.closed:
        return [f"{label}闭馆"]
    if limits.last_entry is None and limits.closing is None:
        return []
    at = _clock(stop.get("time"))
    if at is None:
        return [f"{label}缺少有效到达时间"]
    issues = []
    if limits.last_entry is not None and at > limits.last_entry:
        issues.append(f"{label}{_fmt(at)} 到达，晚于 {_fmt(limits.last_entry)} 停止入场")
    leaves = at + _stay(stop)
    if limits.closing is not None and leaves > limits.closing:
        issues.append(f"{label}{_fmt(leaves)} 离开，晚于 {_fmt(limits.closing)} 关门")
    return issues


def collect_issues(
    plan: dict,
    rules: PlannerRules,
    by_id: dict[int, CandidatePlace],
    amap_ids: set[int] | None = None,
    *,
    lodge: CandidatePlace | None = None,
    request: TripRequest | None = None,
    settings: Settings | None = None,
    backup_shortages: list[str] | None = None,
) -> list[str]:
    """Check the schedule. Backup records unavailable lunches and empty days as shortages."""
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
        day_sights = 0
        nights = 0
        snacks: list[str] = []
        dinner_at = next((index for index, stop in enumerate(stops) if stop.get("kind") == "dinner"), None)
        has_night = day_has_night(stops)
        lodge_name = _lodge_label(lodge)
        named_gap = False
        commute_kind, commute_limit = _commute_class(day, rules, lodge, request, settings)
        urban = commute_kind != "excursion"
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
                elif _walk_exceeds_cap(move):
                    issues.append(f"第{number}天步行 {minutes} 分钟，超过 {WALK_CAP_MINUTES} 分钟：{origin}→{dest}")
                continue
            place = stop.get("_place")
            if place is None:
                token = coerce_place_id(stop.get("place_id"))
                place = by_id.get(token) if isinstance(token, int) else None
            visit_places = (place, *included_scenic_places(stop)) if place is not None else ()
            for point in visit_places:
                scheduled.add(point.place_id)
                if point.place_id in seen:
                    issues.append(f"第{number}天重复安排了编号 {point.place_id}")
                seen[point.place_id] = number
                if _closed(point):
                    issues.append(f"第{number}天「{point.name}」名字带停业字样")
                if rules.opening_hours.by_day:
                    issues.extend(_opening_issues(rules, number, stop, point))
            kind = stop.get("kind")
            if _is_real_sight(stop, place):
                sights += 1
                if kind == "sight":
                    day_sights += 1
                elif kind == "night":
                    nights += 1
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
                if not any(candidate_is_night_place(point) for point in visit_places):
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
            if (
                kind == "sight"
                and urban
                and rules.relaxed_day_end_minute is not None
            ):
                at = _clock(stop.get("time"))
                stay = _stop_stay_minutes(stop)
                if at is not None and stay > 0:
                    end_at = at + stay
                    if end_at > rules.relaxed_day_end_minute:
                        issues.append(day_end_note(
                            number,
                            _stop_label(stop),
                            end_at,
                            rules.relaxed_day_end_minute,
                        ))
            if kind in {"sight", "night"} and any(candidate_is_family_place(point) for point in visit_places):
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
            elif _walk_exceeds_cap(move):
                issues.append(f"第{number}天步行 {minutes} 分钟，超过 {WALK_CAP_MINUTES} 分钟：{origin}→{dest}")
        if dinner_at is not None:
            for stop in stops[dinner_at + 1 :]:
                if stop.get("kind") not in {"night", "rest"}:
                    issues.append(f"第{number}天晚饭后只排夜景")
                    break
        if sights == 0:
            target = backup_shortages if backup_shortages is not None and not stops else issues
            target.append(f"第{number}天没有游览点")
        if len(snacks) > 1:
            issues.append(f"第{number}天有 {len(snacks)} 个小吃或补充能量点（{'、'.join(snacks)}），最多 1 个")
        if rules.day_sight_limit is not None:
            if day_sights > rules.day_sight_limit:
                issues.append(
                    f"第{number}天白天景点 {day_sights} 个，超过{rules.pace_label}节奏的 {rules.day_sight_limit} 个"
                )
            night_cap = rules.night_limit if rules.night_limit is not None else 1
            if nights > night_cap:
                issues.append(
                    f"第{number}天夜景 {nights} 个，超过{rules.pace_label}节奏的 {night_cap} 个"
                )
        elif sights > rules.sight_limit:
            issues.append(f"第{number}天游览点 {sights} 个，超过{rules.pace_label}节奏的 {rules.sight_limit} 个")
        if rules.require_day_rest and urban and not day_has_qualifying_rest(stops, rules):
            issues.append(rest_gap_note(number))
        travel = day.get("_travel_minutes")
        if isinstance(travel, int) and travel > commute_limit:
            detail = _longest_leg_clause(day, lodge)
            suffix = f"；{detail}" if detail else ""
            limit_label = "远郊上限 " if commute_kind == "excursion" else " "
            issues.append(f"第{number}天路上总时间 {travel} 分钟，超过{limit_label}{commute_limit} 分钟{suffix}")
        overrun = _return_overrun(day, rules, urban=urban)
        if overrun is not None:
            issues.append(overrun[1])
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
        elif _walk_exceeds_cap(depart):
            issues.append(
                f"第{number}天步行 {depart_minutes} 分钟，超过 {WALK_CAP_MINUTES} 分钟：{lodge_name}→{first_name}"
            )
        has_lunch = any(stop.get("kind") == "lunch" for stop in stops)
        has_dinner = any(stop.get("kind") == "dinner" for stop in stops)
        if not has_lunch:
            target = backup_shortages if backup_shortages is not None else issues
            target.append(f"第{number}天缺少午饭")
        if rules.dinner and not has_dinner:
            issues.append(f"第{number}天缺少晚饭")
        lunch_place = _resolved_place(_meal_stop(stops, "lunch"), by_id)
        dinner_place = _resolved_place(_meal_stop(stops, "dinner"), by_id)
        if is_pot_meal(lunch_place) and is_pot_meal(dinner_place):
            issues.append(pot_pair_note(
                number,
                _place_name(lunch_place, "午饭"),
                _place_name(dinner_place, "晚饭"),
            ))
    if rules.night and rules.night_candidate and not night_ok:
        issues.append("用户想看夜景，但没有排一个写到夜景的地点")
    if rules.food and rules.food_candidate and not food_ok:
        issues.append("用户喜欢美食，但没有一顿正餐是网友提过的店")
    if rules.tea and rules.tea_candidate and not tea_ok:
        issues.append("用户想去茶馆喝茶，但没有安排茶馆")
    if rules.family and rules.family_candidate and not family_ok:
        issues.append("亲子出行，但没有孩子能玩的地方")
    for place_id in rules.must_ids:
        if place_id not in scheduled:
            place = by_id.get(place_id)
            issues.append(f"必去点「{place.name if place is not None else place_id}」没有排上")
    return list(dict.fromkeys(issues))
