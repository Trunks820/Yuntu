"""Fill each fallback day from qualified candidates using the shared scheduler."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from itertools import chain, permutations
from statistics import median

from src.agents.accommodation_policy import remote_anchor
from src.agents.activity_arrangement import full_day_place
from src.agents.composition_blueprint import is_light_food_place
from src.agents.pace import single_leg_max_minutes
from src.agents.preferences import _top_reason_texts, candidate_is_night_place, candidate_is_tea_place
from src.agents.route_feasibility import build_visit_profile, valid_coordinate
from src.agents.route_planning import _close_selection_ledger
from src.agents.schema import (
    AccommodationAnchor,
    CandidatePlace,
    PoiSelectionInput,
    PoiSelectionResult,
    RoutePlan,
    TripRequest,
)
from src.config import Settings

from .checks import (
    _day_is_urban,
    _opening_issues,
    _return_overrun,
    attach_schedule_notice_facts,
    classify_issues,
    collect_issues,
    urban_commute_notes,
)
from .emit import _emit_failure_issue, emit_route_plan
from .lodging import LODGING_NEAR_KM, _lodging_conflicts, _pick_area_lodging, _take_user_lodging, _usable_user_lodging
from .opening_hours import RouteOpeningHours
from .places import _closed, hav_km
from .rules import (
    AREA_TYPES,
    IDLE_LIMIT_MINUTES,
    LATE_LUNCH,
    LATEST_RETURN_MINUTE,
    MEAL_NEAR_KM,
    PlannerRules,
    _clock,
    build_rules,
    day_has_night,
    dinner_latest_minute,
    is_pot_meal,
)
from .scenic import included_scenic_places
from .schedule import (
    MIN_STAY_MINUTES,
    _proper_meal,
    _stay,
    schedule_day,
)


@dataclass
class ScheduledBackupResult:
    plan: RoutePlan | None = None
    issues: list[str] = field(default_factory=list)
    adjustments: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


LUNCH_REST_LIMIT = 120
PREFERRED_MORNING_DEPARTURE = 9 * 60 + 30
EVENING_GAP_MINUTES = 90
SNACK_NAME_MARKERS = ("糯米团", "凉糕", "烧饼", "烤饼", "大饼", "蛋糕", "面包", "星巴克", "酸奶店", "油茶麻花", "酒酿米糕")


class _EstimateOnly:
    async def quote(self, origin, dest, mode):
        return None


def _day_place_ids(day: dict) -> set[int]:
    return {
        place.place_id for stop in day["stops"] if stop.get("_place") is not None
        for place in (stop["_place"], *included_scenic_places(stop))
    }


@dataclass(frozen=True)
class _BackupSchedule:
    rules: PlannerRules
    request: TripRequest
    settings: Settings
    lodge: CandidatePlace | None
    estimator: _EstimateOnly

    async def schedule_day(self, day: dict, *, start_delay: int = 0) -> list[str]:
        return await schedule_day(
            day, rules=self.rules, request=self.request, settings=self.settings, lodge=self.lodge,
            protected=_day_place_ids(day), estimator=self.estimator, fit_opening_hours=True, fill_gaps=False,
            start_delay=start_delay,
        )

    def return_overrun(self, day: dict) -> tuple[int, str] | None:
        urban = _day_is_urban(day, self.rules, self.lodge, self.request, self.settings)
        return _return_overrun(day, self.rules, urban=urban)

    def day_issues(self, day: dict) -> list[str]:
        number = day["day"]
        issues = []
        visits = [stop for stop in day["stops"] if stop.get("_place") is not None]
        if not any(stop["kind"] in {"sight", "night"} for stop in visits):
            issues.append(f"第{number}天没有可安排的游览点")
        if day["_route_missing"]:
            issues.append(f"第{number}天没有查到通往部分站点的路")
        if day["_return_minute"] > LATEST_RETURN_MINUTE and (overrun := self.return_overrun(day)) is not None:
            issues.append(overrun[1])
        if not 0 <= day["_start_minute"] <= day["_return_minute"] < 24 * 60:
            issues.append(f"第{number}天排不进当天的时间窗")
        explicit_end = _clock(self.request.daily_end)
        for stop in visits:
            place = stop["_place"]
            at = _clock(stop.get("time"))
            if at is None or (explicit_end is not None and at + _stay(stop) > explicit_end):
                issues.append(f"第{number}天「{place.name}」排不进指定的时间窗")
            for member in (place, *included_scenic_places(stop)):
                issues.extend(_opening_issues(self.rules, number, stop, member))
        return issues


def _distance(left, right) -> float:
    distance = hav_km(left, right)
    return float("inf") if distance is None else distance


def _sight(place: CandidatePlace) -> bool:
    return (
        place.place_type not in {"accommodation_area", "hotel", "restaurant", "food", "cafe", "snack", "snack_shop", "dessert", "tea"}
        and not is_light_food_place(place)
        and not candidate_is_tea_place(place)
    )


def _backup_snack(place: CandidatePlace) -> bool:
    return (
        is_light_food_place(place) or place.place_type in {"snack", "snack_shop"}
        or any(marker in place.name for marker in SNACK_NAME_MARKERS)
        or place.place_type in {"restaurant", "food"} and any(
            phrase in text for text in [place.name, *_top_reason_texts(place)]
            if len(text) <= 50 and "→" not in text
            for phrase in ("咖啡店", "咖啡馆", "江景咖啡", "下午茶", "甜品店")
        )
    )


def _backup_meal(place: CandidatePlace) -> bool:
    return _proper_meal(place) and not _backup_snack(place)


def _night_candidate(place: CandidatePlace) -> bool:
    """In a multi-stop note, credit night evidence only to the stop it describes."""
    lines = _top_reason_texts(place)
    if all("→" not in line for line in lines):
        return candidate_is_night_place(place)
    local_reasons = [
        {"text": stop} for line in lines for stop in line.split("→")
        if "→" not in line or place.name in stop
    ]
    return candidate_is_night_place(place.model_copy(update={"top_reasons": local_reasons}))


def _named_night(place: CandidatePlace) -> bool:
    return candidate_is_night_place(place.model_copy(update={"top_reasons": []}))


def _night_fits_opening(place: CandidatePlace, rules: PlannerRules, number: int) -> bool:
    limits = rules.opening_hours.for_place(number, place)
    earliest = max(rules.start_minute, rules.dark_minute)
    latest = limits.latest_arrival(MIN_STAY_MINUTES)
    return not limits.closed and (latest is None or earliest <= latest)


def _center_for_backup(places: list[CandidatePlace]) -> tuple[float, float] | None:
    sights = [place for place in places if _sight(place)]
    if not sights:
        return None
    return median(place.latitude for place in sights), median(place.longitude for place in sights)


def _backup_lodging(
    request: TripRequest, selected: list[CandidatePlace], pool: list[CandidatePlace],
    by_id: dict[int, CandidatePlace], anchor: AccommodationAnchor | None, core: tuple[float, float] | None,
) -> tuple[CandidatePlace | None, str, list[str]]:
    taken = _take_user_lodging(request, pool, by_id, anchor)
    if taken is not None:
        lodge, source, _, notes = taken
        return lodge, source, notes
    itinerary = [place for place in selected if place.place_type not in {"accommodation_area", "hotel"}]
    areas = [place for place in pool if place.place_id > 0 and place.place_type in AREA_TYPES]
    proposed = max(areas, key=lambda place: (place.mention_count or 0, place.place_type == "accommodation_area", -place.place_id), default=None)
    if anchor is not None and anchor.suggestion.source == "auto_recommended":
        recommended = by_id.get(anchor.place_id) or anchor.suggestion
        if valid_coordinate(recommended.latitude, recommended.longitude) and _lodging_conflicts(recommended, itinerary):
            proposed = recommended
    if proposed is not None and proposed in areas and not _lodging_conflicts(proposed, itinerary):
        return proposed, "auto", []
    lodge, notes = _pick_area_lodging(areas, itinerary, proposed=proposed, center=core)
    return lodge, "auto" if lodge is not None else "", notes or ["候选里没有可用住区，这次不写住处"]


def _stop(place: CandidatePlace, kind: str) -> dict:
    stay = build_visit_profile(place).visit_minutes if kind == "sight" else 45 if kind == "snack" else 60
    return {"kind": kind, "place_id": place.place_id, "_place": place, "stay_minutes": stay}


def _short_orders(places: list[CandidatePlace]):
    if len(places) <= 5:
        return permutations(places)
    return (places, *([place, *(other for other in places if other is not place)] for place in places[1:]))


async def _fit_required(
    number: int, places: list[CandidatePlace], schedule: _BackupSchedule, *, night_anchor: int | None = None,
) -> dict | None:
    rules = schedule.rules
    limits = {place.place_id: rules.opening_hours.for_place(number, place) for place in places}
    if any(limit.closed for limit in limits.values()):
        return None
    sights = [place for place in places if _sight(place)]
    if not sights:
        return None
    night_only = [place for place in sights if _named_night(place) or place.place_id == night_anchor]
    if len(night_only) > 1 or (night_only and (not rules.dinner or not _night_fits_opening(night_only[0], rules, number))):
        return None
    nights = night_only or [
        place for place in sights if rules.night and rules.dinner and place.place_id in rules.must_ids
        and _night_candidate(place) and _night_fits_opening(place, rules, number)
    ]
    ordered = sorted(places, key=lambda place: (
        place not in sights, limits[place.place_id].latest_arrival(MIN_STAY_MINUTES) or 24 * 60,
        _distance(schedule.lodge, place),
    ))
    for night in nights if night_only else [*nights, None]:
        before_night = [place for place in ordered if place is not night]
        for short, order in chain([(False, before_night)], ((True, order) for order in _short_orders(before_night))):
            if order and order[0] not in sights:
                continue
            meal_kinds = iter(("lunch", "dinner") if rules.dinner else ("lunch",))
            stops = []
            for place in order:
                kind = "sight" if place in sights else next(meal_kinds, "snack") if _backup_meal(place) else "snack"
                stops.append(_stop(place, kind))
            if night is not None:
                stops.append(_stop(night, "night"))
            if short:
                _shorten_stops(stops, 0)
            day = {"day": number, "stops": stops}
            await schedule.schedule_day(day)
            if not schedule.day_issues(day):
                return day
    return None


def _sights(day: dict) -> list[CandidatePlace]:
    return [stop["_place"] for stop in day["stops"] if stop["kind"] == "sight"]


async def _seed_days(
    candidates: list[CandidatePlace], priority: dict[int, int], isolated_ids: set[int],
    night_ids: set[int], schedule: _BackupSchedule,
) -> tuple[list[dict], list[str]]:
    """Place required stops and match distinct feasible anchors to every date."""
    rules = schedule.rules
    numbers = range(1, rules.days + 1)
    by_id = {place.place_id: place for place in candidates}
    required = set(rules.must_ids) & by_id.keys()
    anchors = [place for place in candidates if place.place_id not in required and _sight(place)]
    short_inventory = sum(_sight(place) for place in candidates) < rules.days
    meals = [place for place in candidates if _backup_meal(place)]
    sparse_isolated = {
        place.place_id for place in anchors if place.place_id in isolated_ids
        and sum(_distance(place, meal) <= LODGING_NEAR_KM for meal in meals) < 1 + int(rules.dinner)
    }
    dates = {
        pid: [number for number in numbers if not rules.opening_hours.for_place(number, by_id[pid]).closed]
        for pid in required
    }
    assigned: dict[int, list[int]] = {number: [] for number in numbers}
    cache: dict[tuple[int, tuple[int, ...]], dict | None] = {}
    problems: list[str] = []

    async def fit(number: int, anchor: CandidatePlace | None = None) -> dict | None:
        ids = tuple(sorted(assigned[number] + ([anchor.place_id] if anchor is not None else [])))
        key = number, ids
        if key not in cache:
            night_anchor = anchor.place_id if anchor is not None and anchor.place_id in night_ids else None
            cache[key] = await _fit_required(number, [by_id[pid] for pid in ids], schedule, night_anchor=night_anchor)
        return cache[key]

    async def fill_dates(*, allow_empty: bool) -> list[dict] | None:
        base = {number: day for number in numbers if assigned[number] and (day := await fit(number)) is not None}
        filled = dict(base)
        owners: dict[int, int] = {}

        async def fill(number: int, seen: set[int]) -> bool:
            if number in base and _sights(base[number]):
                return True
            here = [by_id[pid] for pid in assigned[number]]
            elsewhere = [place for other, day in filled.items() if other != number for place in _sights(day)[:1]]
            for place in sorted(anchors, key=lambda place: (
                place.place_id in night_ids or _named_night(place),
                place.place_id in sparse_isolated, priority.get(place.place_id, 2),
                min((_distance(place, other) for other in here), default=0),
                rules.opening_hours.for_place(number, place).latest_arrival(MIN_STAY_MINUTES) or 24 * 60,
                -min((_distance(place, other) for other in elsewhere), default=0),
                _distance(schedule.lodge, place),
            )):
                pid = place.place_id
                if pid in seen or (trial := await fit(number, place)) is None:
                    continue
                seen.add(pid)
                if pid not in owners or await fill(owners[pid], seen):
                    filled[number] = trial
                    owners[pid] = number
                    return True
            return number in base and number not in owners.values()

        for number in numbers:
            if not await fill(number, set()):
                if allow_empty and not assigned[number]:
                    day = {"day": number, "stops": []}
                    await schedule.schedule_day(day)
                    filled[number] = day
                    continue
                problems.append(f"第{number}天没有能在时间窗内游览的地点，或没有查到通往部分站点的路")
                return None
        if not any(day["stops"] for day in filled.values()):
            problems.append("没有能在时间窗内游览的地点，或没有查到通往部分站点的路")
            return None
        return [filled[number] for number in numbers]

    must_order = sorted((pid for pid in required if dates[pid]), key=lambda pid: (len(dates[pid]), pid))

    async def place_required(index: int, *, allow_empty: bool, allow_missed: bool) -> list[dict] | None:
        if index == len(must_order):
            return await fill_dates(allow_empty=allow_empty)
        pid = must_order[index]
        for number in sorted(dates[pid], key=lambda number: len(assigned[number])):
            assigned[number].append(pid)
            if not any(_sight(by_id[item]) and not _named_night(by_id[item]) for item in assigned[number]) or await fit(number):
                found = await place_required(index + 1, allow_empty=allow_empty, allow_missed=allow_missed)
                if found is not None:
                    return found
            assigned[number].pop()
        if allow_missed:
            return await place_required(index + 1, allow_empty=allow_empty, allow_missed=True)
        return None

    days = await place_required(0, allow_empty=short_inventory, allow_missed=False)
    if days is None and must_order:
        days = await place_required(0, allow_empty=short_inventory, allow_missed=True)
    if days is None and not short_inventory:
        days = await place_required(0, allow_empty=True, allow_missed=True)
    if days is not None:
        return days, []
    return [], list(dict.fromkeys(problems))


def _day_clock_key(day: dict, rules: PlannerRules) -> tuple[int, int, int, int]:
    late = 0
    morning = afternoon = False
    waits = []
    lunch = next((stop for stop in day["stops"] if stop["kind"] == "lunch"), None)
    afternoon_start = max(12 * 60, _clock(lunch["time"]) + _stay(lunch)) if lunch is not None else 12 * 60
    for stop in day["stops"]:
        at = _clock(stop["time"])
        kind = stop["kind"]
        if kind == "lunch":
            late += max(0, at - LATE_LUNCH)
        elif kind == "dinner":
            late += max(0, at - dinner_latest_minute(rules.end_minute))
        elif kind == "sight":
            morning |= at < 12 * 60
            afternoon |= afternoon_start <= at < 18 * 60
        waits.append(stop["wait_minutes"])
    return late, 2 - int(morning) - int(afternoon), max(waits, default=0), day["_travel_minutes"]


def _shorten_stops(stops: list[dict], ratio: float) -> None:
    for stop in stops:
        if not full_day_place(stop["_place"]):
            floor = 45 if stop["kind"] in {"lunch", "dinner"} else MIN_STAY_MINUTES
            stop["stay_minutes"] = max(floor, int(_stay(stop) * ratio))


async def _fit_addition(
    day: dict, place: CandidatePlace, kind: str, schedule: _BackupSchedule, *, gap: int | None = None,
) -> dict | None:
    stops = day["stops"]
    night_only = not _sights(day) and day_has_night(stops)
    end = next((index for index, stop in enumerate(stops) if stop["kind"] in {"dinner", "night"}), len(stops))
    if gap is not None:
        positions = _gap_positions(stops, gap, end)
    elif kind == "lunch":
        positions = range(0 if night_only else 1, end + 1)
    elif kind == "dinner":
        positions = [next((index for index, stop in enumerate(stops) if stop["kind"] == "night"), len(stops))]
    elif kind == "night":
        positions = [len(stops)]
    elif kind == "snack":
        positions = [end]
    else:
        positions = range(end + 1)
    previous_lateness = _day_clock_key(day, schedule.rules)[0]
    meal_detour_limit = 2 * single_leg_max_minutes(schedule.rules.commute_mode, schedule.settings)
    for ratio in (1, 0.75, 0.5):
        best = None
        for pos in positions:
            if gap is not None and min(_distance(place, stop["_place"]) for stop in stops[pos - 1:pos + 1]) > LODGING_NEAR_KM:
                continue
            series = [stop.copy() for stop in stops]
            new_stop = _stop(place, kind)
            series.insert(pos, new_stop)
            if series[0]["kind"] not in {"sight", "night"} and (kind == "sight" or not night_only):
                continue
            if ratio != 1:
                _shorten_stops(series if gap is None else [new_stop], ratio)
            trial = {**day, "stops": series}
            await schedule.schedule_day(trial)
            if schedule.day_issues(trial):
                continue
            if kind in {"lunch", "dinner"} and trial["_travel_minutes"] - day["_travel_minutes"] > meal_detour_limit:
                continue
            if gap is not None and (
                series[gap + 1]["time"] > stops[gap]["time"]
                or series[gap + 1]["wait_minutes"] >= stops[gap]["wait_minutes"]
            ):
                continue
            key = _day_clock_key(trial, schedule.rules)
            if key[0] <= previous_lateness and (best is None or key < best[0]):
                best = key, trial
        if best is not None:
            return best[1]
    return None


def _day_target(rules: PlannerRules) -> int:
    return rules.sight_limit - 1


def _gap_positions(stops: list[dict], gap: int, end: int) -> range:
    start = next((index + 1 for index, stop in enumerate(stops[:gap]) if stop["kind"] == "lunch"), 1)
    return range(start, min(gap, end) + 1)


def _gap_limit(kind: str) -> int:
    return IDLE_LIMIT_MINUTES + 1 if kind == "lunch" else EVENING_GAP_MINUTES


async def _fill_days(
    days: list[dict], candidates: list[CandidatePlace], priority: dict[int, int], selected_ids: set[int],
    isolated_ids: set[int], night_ids: set[int], schedule: _BackupSchedule,
) -> dict[int, str]:
    """Grow a usable day one stop at a time; a bad addition leaves it intact."""
    rules = schedule.rules
    used = set().union(*(_day_place_ids(day) for day in days))
    pools = {
        "sight": [place for place in candidates if _sight(place) and not _named_night(place)],
        "meal": [place for place in candidates if _backup_meal(place)],
        "night": [place for place in candidates if place.place_id in night_ids],
        "snack": [place for place in candidates if _backup_snack(place) or rules.tea and candidate_is_tea_place(place)],
    }
    supplements = {pid: "FILL_EMPTY_DAY" for pid in used - selected_ids}

    def ranked(day: dict, kind: str, gap: int | None = None) -> list[CandidatePlace]:
        is_meal = kind in {"lunch", "dinner"}
        stops = day["stops"]
        sights = _sights(day)
        anchors = sights or [stop["_place"] for stop in stops if stop["kind"] == "night"]
        isolated = [place for place in sights if place.place_id in isolated_ids]
        if not anchors or (kind != "sight" and any(stop["kind"] == kind for stop in stops)):
            return []
        if gap is not None:
            end = next((index for index, stop in enumerate(stops) if stop["kind"] == "dinner"), gap)
            anchors = [stops[index]["_place"] for pos in _gap_positions(stops, gap, end) for index in (pos - 1, pos)]
            if not anchors:
                return []
        lunch = next((stop["_place"] for stop in stops if stop["kind"] == "lunch"), None)
        if kind == "night":
            dinner = next((stop["_place"] for stop in stops if stop["kind"] == "dinner"), None)
            if dinner is not None:
                anchors = [dinner]
        choices = []
        nights_filled = all(day_has_night(other["stops"]) for other in days)
        for place in pools["meal" if is_meal else kind]:
            if place.place_id in used or rules.opening_hours.for_place(day["day"], place).closed:
                continue
            if kind == "night" and not _night_fits_opening(place, rules, day["day"]):
                continue
            if kind == "sight" and place.place_id in night_ids and not nights_filled:
                continue
            if kind == "snack" and gap is None and not candidate_is_tea_place(place):
                continue
            distance = min(_distance(place, point) for point in anchors)
            if isolated and min(_distance(place, point) for point in isolated) > LODGING_NEAR_KM:
                continue
            if not isolated and place.place_id in isolated_ids and distance > LODGING_NEAR_KM:
                continue
            tier = MEAL_NEAR_KM if is_meal else LODGING_NEAR_KM
            if gap is not None and (distance > tier or full_day_place(place)):
                continue
            choices.append((distance // tier, priority.get(place.place_id, 2),
                            kind == "dinner" and is_pot_meal(lunch) and is_pot_meal(place), distance, place.place_id, place))
        return [place for *_, place in sorted(choices)]

    def adopt(day: dict, trial: dict, place_id: int, kind: str) -> None:
        day.update(trial)
        used.add(place_id)
        if place_id not in selected_ids:
            supplements[place_id] = "FILL_MEAL_SLOT" if kind in {"lunch", "dinner"} else "FILL_EMPTY_DAY"

    async def add(day: dict, *kinds: str, gap: int | None = None) -> bool:
        best = None
        for kind in kinds:
            for place in ranked(day, kind, gap):
                trial = await _fit_addition(day, place, kind, schedule, gap=gap)
                if trial is None:
                    continue
                if gap is None or (wait := trial["stops"][gap + 1]["wait_minutes"]) < _gap_limit(day["stops"][gap]["kind"]):
                    adopt(day, trial, place.place_id, kind)
                    return True
                if best is None or wait < best[0]:
                    best = wait, trial, place.place_id, kind
        if best is not None:
            adopt(day, *best[1:])
            return True
        return False

    async def reserve_nights() -> None:
        options = {day["day"]: ranked(day, "night") for day in days}
        placed: dict[int, tuple[dict, dict, int]] = {}
        owners: dict[int, dict] = {}
        checked: dict[tuple[int, int], dict | None] = {}

        async def match(day: dict, seen: set[int]) -> bool:
            number = day["day"]
            for place in sorted(options[number], key=lambda place: place.place_id in owners):
                pid = place.place_id
                if pid in seen:
                    continue
                key = number, pid
                if key not in checked:
                    checked[key] = await _fit_addition(day, place, "night", schedule)
                if (trial := checked[key]) is None:
                    continue
                seen.add(pid)
                if pid not in owners or await match(owners[pid], seen):
                    owners[pid] = day
                    placed[number] = day, trial, pid
                    return True
            return False

        for day in days:
            await match(day, set())
        for day, trial, pid in placed.values():
            adopt(day, trial, pid, "night")

    async def fill_sights() -> None:
        for count in range(_day_target(rules)):
            for day in days:
                if len(_sights(day)) <= count:
                    await add(day, "sight")

    meal_kinds = ("lunch", "dinner") if rules.dinner else ("lunch",)
    for kind in meal_kinds:
        for day in days:
            await add(day, kind)
    if night_ids:
        await reserve_nights()
    await fill_sights()
    for kind in meal_kinds:
        for day in days:
            await add(day, kind)
    if night_ids and not all(day_has_night(day["stops"]) for day in days):
        await reserve_nights()
        if all(day_has_night(day["stops"]) for day in days):
            await fill_sights()
    for day in days:
        cap = rules.day_sight_limit or rules.sight_limit
        windows = sorted(day["stops"], key=lambda stop: -stop["wait_minutes"])
        for window in (stop["kind"] for stop in windows if stop["kind"] in (*meal_kinds, "night")):
            while (gap := next((index for index, stop in enumerate(day["stops"])
                               if stop["kind"] == window and stop["wait_minutes"] >= _gap_limit(window)), None)) is not None:
                kinds = ("sight", "snack") if len(_sights(day)) < cap else ("snack",)
                if not await add(day, *kinds, gap=gap):
                    break
        if rules.tea:
            await add(day, "snack")
    return supplements


async def _shorten_late_return(day: dict, schedule: _BackupSchedule) -> list[str]:
    overrun = schedule.return_overrun(day)
    if overrun is None:
        return []
    stop = next(stop for stop in reversed(day["stops"]) if stop.get("_place") is not None)
    place = stop["_place"]
    if full_day_place(place):
        return []
    current = _stay(stop)
    floor = 45 if stop["kind"] in {"lunch", "dinner"} else MIN_STAY_MINUTES
    cut = min(overrun[0], current - floor)
    if cut <= 0:
        return []
    stop["stay_minutes"] = current - cut
    trims = await schedule.schedule_day(day)
    return [f"第{day['day']}天末站「{place.name}」少停留 {cut} 分钟，给回住处留出时间", *trims]


async def _settle_day(day: dict, schedule: _BackupSchedule) -> None:
    """Align a flexible departure and spend only a short afternoon gap resting."""
    rules = schedule.rules
    stops = day["stops"]
    lunch = next((stop for stop in stops if stop["kind"] == "lunch"), None)
    delay = 0
    if _clock(schedule.request.daily_start) is None:
        if not _sights(day):
            delay = stops[0]["wait_minutes"]
        elif lunch is not None and lunch["wait_minutes"]:
            delay = min(lunch["wait_minutes"], PREFERRED_MORNING_DEPARTURE - day["_start_minute"])
        if delay > 0:
            trial = {**day, "stops": [stop.copy() for stop in stops]}
            await schedule.schedule_day(trial, start_delay=delay)
            if not schedule.day_issues(trial) and _day_clock_key(trial, rules)[:2] <= _day_clock_key(day, rules)[:2]:
                day.update(trial)
                stops = day["stops"]
                lunch = next((stop for stop in stops if stop["kind"] == "lunch"), None)
            else:
                delay = 0
    if lunch is None or not rules.require_day_rest:
        return
    gap = max((stop["wait_minutes"] for stop in stops if stop["kind"] in {"dinner", "night"}), default=0)
    stay = min(max(gap, 60), LUNCH_REST_LIMIT)
    for in_place in (False, True):
        series = [stop.copy() for stop in stops]
        series.insert(stops.index(lunch) + 1, {
            "kind": "rest", "_place": None, "stay_minutes": stay, "_rest_in_place": in_place,
        })
        trial = {**day, "stops": series}
        await schedule.schedule_day(trial, start_delay=max(0, delay))
        if not schedule.day_issues(trial) and _day_clock_key(trial, rules)[:2] <= _day_clock_key(day, rules)[:2]:
            day.update(trial)
            return


async def plan_backup(
    request: TripRequest,
    selection: PoiSelectionResult,
    qualified_pool: list[CandidatePlace],
    *,
    settings: Settings,
    selector_input: PoiSelectionInput | None = None,
    lodging_anchor: AccommodationAnchor | None = None,
    urban_core: tuple[float, float] | None = None,
    opening_hours: RouteOpeningHours | None = None,
) -> ScheduledBackupResult:
    """Schedule the Selector's places, using the qualified pool for feasible additions."""
    allowed = {place.place_id: place for place in qualified_pool}
    selected_ids = {item.place_id for item in selection.selected}
    eligible_ids = set(selector_input.constraints.eligible_place_ids) if selector_input is not None else set(allowed)
    if not selected_ids <= allowed.keys() or not selected_ids <= eligible_ids:
        return ScheduledBackupResult(issues=["选点结果包含候选范围外的地点"])
    if len(selected_ids) != len(selection.selected):
        return ScheduledBackupResult(issues=["选点结果包含重复地点"])
    priority = dict.fromkeys(eligible_ids, 1) | dict.fromkeys(selected_ids, 0)
    must_ids = {place.place_id for place in qualified_pool if place.must_include}
    if selector_input is not None:
        must_ids.update(selector_input.constraints.must_include_place_ids)
    pool = [place for place in qualified_pool if valid_coordinate(place.latitude, place.longitude) and not _closed(place)]
    by_id = {place.place_id: place for place in pool}
    selected = [by_id[item.place_id] for item in selection.selected if item.place_id in by_id]
    rules = build_rules(request, selected, settings=settings)
    rules = replace(
        rules, must_ids=tuple(sorted(must_ids.union(rules.must_ids))),
        opening_hours=opening_hours or rules.opening_hours,
        day_sight_limit=rules.day_sight_limit or rules.sight_limit, night_limit=1,
    )
    core = urban_core or _center_for_backup(selected) or _center_for_backup(pool)
    lodge, source, adjustments = _backup_lodging(request, selected, pool, by_id, lodging_anchor, core)
    candidates = [
        place for place in pool if (
            lodge is None or place.place_id in rules.must_ids
            or not (place.place_id == lodge.place_id or source == "auto" and _lodging_conflicts(lodge, [place]))
        )
    ]
    isolated_ids = {
        place.place_id for place in candidates if _sight(place)
        and (full_day_place(place) or remote_anchor([place], core)[0] is not None)
    }
    night_ids = {place.place_id for place in candidates if rules.night and rules.dinner and _sight(place) and _night_candidate(place)}
    schedule = _BackupSchedule(replace(rules, rest_via_lodge_only_when_near=True), request, settings, lodge, _EstimateOnly())
    days, issues = await _seed_days(candidates, priority, isolated_ids, night_ids, schedule)
    if not days:
        return ScheduledBackupResult(issues=issues, adjustments=adjustments)
    supplement_reasons = await _fill_days(days, candidates, priority, selected_ids, isolated_ids, night_ids, schedule)
    arranged = {"days": days}
    for day in days:
        if not day["stops"]:
            continue
        adjustments.extend(await _shorten_late_return(day, schedule))
        await _settle_day(day, schedule)
        for stop in day["stops"]:
            if stop.get("place_id") not in supplement_reasons:
                continue
            kind = {"lunch": "午饭", "dinner": "晚饭", "night": "夜景", "snack": "小吃"}.get(stop["kind"], "游览")
            adjustments.append(f"第{day['day']}天{kind}补入「{stop['_place'].name}」")

    check = {"lodge": lodge, "request": request, "settings": settings}
    notes: list[str] = []
    checked = classify_issues(collect_issues(arranged, rules, allowed, backup_shortages=notes, **check))
    if checked.hard:
        return ScheduledBackupResult(issues=checked.hard, adjustments=adjustments)
    notes.extend(checked.repair + checked.record)
    target = _day_target(rules)
    shortages: dict[int, list[str]] = {}
    for day in days:
        number = day["day"]
        missing = [] if any(stop["kind"] == "lunch" for stop in day["stops"]) else ["缺少午饭，请自行安排"]
        day_count = len(_sights(day))
        if day_count < target:
            missing.insert(0, f"在候选和时间窗内可排白天游览 {day_count} 处（节奏目标 {target} 处）")
        elif _day_clock_key(day, rules)[1]:
            missing.insert(0, "在候选和时间窗内未能同时安排上午和下午游览")
        if rules.night and rules.dinner and not day_has_night(day["stops"]):
            missing.append("在候选和时间窗内未能安排晚饭后的夜景")
        if missing:
            shortages[number] = missing
            notes.extend(f"第{number}天{item}" for item in missing if not item.startswith("缺少"))
    notes.extend(urban_commute_notes(arranged, rules, **check))
    adjustments = list(dict.fromkeys(adjustments))
    resolved_anchor = _usable_user_lodging(lodging_anchor)
    try:
        plan = await emit_route_plan(
            arranged, request=request, lodge=lodge, lodging_source=source, provider=None,
            citycode=None, by_id=by_id, location_precision=resolved_anchor.location_precision if resolved_anchor else None,
        )
    except Exception as exc:
        return ScheduledBackupResult(issues=[_emit_failure_issue(exc)], adjustments=adjustments)
    if any(group.timeline is None or (day["stops"] and not group.timeline.items)
           for day, group in zip(days, plan.day_groups)):
        return ScheduledBackupResult(issues=["兜底排程没有完整时间表"], adjustments=adjustments)
    for group in plan.day_groups:
        group.time_hints.extend(shortages.get(group.day, ()))
        if not group.places:
            all_closed = bool(candidates) and all(
                rules.opening_hours.for_place(group.day, place).closed for place in candidates
            )
            group.empty_day_reason = "all_closed" if all_closed else "insufficient_candidates"

    used = {place.place_id for group in plan.day_groups for place in group.places}
    _close_selection_ledger(
        plan, selection=selection, candidates_by_id=allowed, request=request,
        supplement_reasons={place_id: supplement_reasons[place_id] for place_id in sorted(used - selected_ids)},
        selected_drop_reasons={
            place_id: "ROUTE_FEASIBILITY_LIMIT" if place_id in by_id else "HARD_INELIGIBLE"
            for place_id in sorted(selected_ids - used)
        },
    )
    attach_schedule_notice_facts(plan, arranged, rules, allowed, **check)
    return ScheduledBackupResult(plan=plan, adjustments=adjustments, notes=list(dict.fromkeys(notes)))
