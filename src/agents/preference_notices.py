"""Fixed sentences for explicit preferences the locked route did not arrange.

The Writer does not author these lines. Detailed plans and the basic
SafePlanRenderer both splice the same strings. A sentence is emitted only from
facts already on the route: preference decisions, family/tea identity, and
program day hints. Unknown causes stay neutral.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from src.agents.poi_fragments import fragment_registry_is_intact, remap_fragment_registry
from src.agents.preferences import (
    FOOD,
    NIGHT_VIEW,
    candidate_is_family_place,
    candidate_is_night_place,
    candidate_is_tea_place,
    candidate_matches_dimension,
    family_arrangement_active,
    food_preference_active,
)
from src.agents.schema import PlanOutput, RoutePlan, TripRequest


# Whole-trip night. The time sentence is the accepted wording for a proved
# window/distance failure. No-match and unknown do not claim that cause.
NIGHT_TRIP_NO_MATCH = "这次没排到适合晚上去的夜景点：现有候选里没有能晚上去的地方。"
NIGHT_TRIP_TIME = (
    "这次没排到适合晚上去的夜景点：候选里能晚上去的地方离住处太远，或放进来会超出当天时间。"
)
NIGHT_TRIP_UNKNOWN = "这次没排到适合晚上去的夜景点：这次没能安排上。"

NIGHT_DAY_RETURN = "这一天晚上没排夜景：晚饭后再去会超过回住处的时间。"
NIGHT_DAY_TIME = "这一天晚上没排夜景：放进来会超出当天时间。"
NIGHT_DAY_UNKNOWN = "这一天晚上没排夜景：这次没能安排上。"

FAMILY_TRIP = "这次没排到孩子能参与的地方：这次没能安排上。"
TEA_TRIP = "这次没排到茶馆：这次没能安排上。"

FOOD_NO_MATCH = "这次没排到网友提过的正餐店：现有候选里没有能对上的店。"
FOOD_TIME = "这次没排到网友提过的正餐店：放进来会超出当天能排的时间。"
FOOD_UNKNOWN = "这次没排到网友提过的正餐店：这次没能安排上。"

ALL_FIXED_SENTENCES: tuple[str, ...] = (
    NIGHT_TRIP_TIME,
    NIGHT_TRIP_NO_MATCH,
    NIGHT_TRIP_UNKNOWN,
    NIGHT_DAY_RETURN,
    NIGHT_DAY_TIME,
    NIGHT_DAY_UNKNOWN,
    FAMILY_TRIP,
    TEA_TRIP,
    FOOD_NO_MATCH,
    FOOD_TIME,
    FOOD_UNKNOWN,
)

# Same strings the route planner already writes into time_hints. Soft hints
# such as "适合安排在傍晚或夜间" stay in the Writer prompt only.
PROGRAM_NIGHT_HINT_PREFIX = "路线计划："
PROGRAM_AFTER_DINNER_PREFIX = "晚饭后去"
PROGRAM_REST_HINT = "午饭后回住处歇脚"
PROGRAM_PRE_DINNER_REST_HINT = "晚饭前回住处歇一会儿"

_TEA_MARKERS = ("茶馆", "喝茶", "品茶")

# Scheduler reasons that mean the evening stop did not fit the locked window.
_NIGHT_TIME_REASONS = frozenset({
    "evening_stop_past_window",
    "inbound_past_window",
    "outbound_past_window",
    "visit_past_window",
    "transit_past_window",
    "evening_stop_outside_night",
    "time_policy_violation",
})
# These two are specifically "after dinner the return would miss the window".
_NIGHT_DAY_RETURN_REASONS = frozenset({
    "evening_stop_past_window",
    "inbound_past_window",
})

PROTECTED_OPENING_PREFIXES: tuple[str, ...] = (
    "这一天晚上没排夜景",
    "这次没排到适合晚上去的夜景点",
    "这次没排到孩子能参与的地方",
    "这次没排到茶馆",
    "这次没排到网友提过的正餐店",
    PROGRAM_NIGHT_HINT_PREFIX,
    PROGRAM_AFTER_DINNER_PREFIX,
    PROGRAM_REST_HINT,
    PROGRAM_PRE_DINNER_REST_HINT,
    "住宿往返",
)

_HEADING_RE = re.compile(
    r"(?im)^\s*(?:#{1,6}\s*)?(?:\*\*)?\s*"
    r"(?:Day\s*(\d+)|第\s*(\d+|[一二三四五六七八九十]+)\s*(?:天|日))"
    r"([^\n]*?)(?:\*\*)?\s*$"
)


@dataclass(frozen=True, slots=True)
class PreferenceNotices:
    """Trip lines belong in the opening. Day lines belong under that day title."""

    trip_lines: tuple[str, ...] = ()
    day_lines: dict[int, tuple[str, ...]] | None = None

    def __post_init__(self) -> None:
        if self.day_lines is None:
            object.__setattr__(self, "day_lines", {})


def program_day_hints(day_group) -> list[str]:
    """Program hints that must show on the basic day, in route order."""
    found: list[str] = []
    for hint in getattr(day_group, "time_hints", None) or []:
        text = str(hint or "").strip()
        if not text or text in found:
            continue
        if (
            text.startswith(PROGRAM_NIGHT_HINT_PREFIX)
            or text.startswith(PROGRAM_AFTER_DINNER_PREFIX)
            or text == PROGRAM_REST_HINT
            or text == PROGRAM_PRE_DINNER_REST_HINT
        ):
            found.append(text)
    return found


def notice_mask_strings(route_plan: RoutePlan, *, day: int | None = None) -> list[str]:
    """Exact strings order checks and repair must not treat as route prose."""
    masks = list(ALL_FIXED_SENTENCES)
    for day_group in route_plan.day_groups:
        if day is not None and int(day_group.day or 0) != int(day):
            continue
        masks.extend(program_day_hints(day_group))
    return masks


def plan_uses_program_hints(plan: PlanOutput) -> bool:
    """Basic plans already own these hints. Detailed copy keeps the Writer prompt."""
    return any(
        getattr(fragment, "source", "") == "safe_renderer"
        for fragment in (plan.poi_fragments or [])
    )


def build_preference_notices(
    request: TripRequest | None,
    route: RoutePlan | None,
) -> PreferenceNotices:
    """Which fixed sentences this one route still owes the reader."""
    if request is None or route is None:
        return PreferenceNotices()
    from src.agents.explicit_preference_fulfillment import route_preference_decisions

    rows = {
        row["dimension"]: row
        for row in route_preference_decisions(request, [route])
    }
    trip: list[str] = []
    by_day: dict[int, list[str]] = {}
    night = rows.get(NIGHT_VIEW)
    if night is not None:
        # A real evening interval means the stop was scheduled, even when the
        # hint line was not copied onto the day yet.
        # Model days can sit over the old load ceiling, so the selector gap
        # is not evidence that the night stop was dropped. The schedule is.
        scheduled_night_days = (
            _model_scheduled_night_days(route)
            if route.planner_source == "model"
            else set()
        )
        arranged = bool(night.get("arrangement_present")) or _night_interval_present(
            route,
            request,
        )
        if scheduled_night_days:
            arranged = True
        if not arranged:
            trip.append(_night_trip_sentence(night))
        for gap in night.get("night_gaps") or []:
            day_number = int(gap.get("day") or 0)
            if day_number <= 0 or day_number in scheduled_night_days:
                continue
            by_day.setdefault(day_number, []).append(
                _night_day_sentence(str(gap.get("reason") or ""))
            )
    places = _route_places(route)
    if family_arrangement_active(request.preferences, notes=request.notes):
        if not any(candidate_is_family_place(place) for place in places):
            trip.append(FAMILY_TRIP)
    if _tea_requested(request):
        if not any(candidate_is_tea_place(place) for place in places):
            trip.append(TEA_TRIP)
    if food_preference_active(request.preferences) and not _food_fulfilled(places):
        trip.append(_food_sentence((rows.get(FOOD) or {}).get("search_status")))
    return PreferenceNotices(
        trip_lines=tuple(dict.fromkeys(trip)),
        day_lines={
            day: tuple(dict.fromkeys(lines))
            for day, lines in by_day.items()
        },
    )


def merge_trip_summary(summary: str, trip_lines: tuple[str, ...]) -> str:
    """Put trip sentences at the front of an already-sanitized opening."""
    current = summary or ""
    missing = [line for line in trip_lines if line and line not in current]
    if not missing:
        return current
    block = "\n".join(missing)
    if not current:
        return block
    return block + "\n" + current


def insert_notice_text(
    text: str,
    route: RoutePlan,
    notices: PreferenceNotices,
    *,
    hint_lines_by_day: dict[int, tuple[str, ...]] | None = None,
) -> str:
    """Splice trip lines before the first day and day lines under that title.

    Day lines sit after the accommodation-access sentence when that line is
    already the first line of the day. Existing copies are left in place.
    """
    hints = hint_lines_by_day or {}
    source = text or ""
    headings = _valid_headings(source)
    if headings:
        preamble = source[:headings[0][0]]
    else:
        preamble = source
    preamble = _ensure_opening_lines(preamble, notices.trip_lines)
    if not headings:
        extra: list[str] = []
        for day_group in route.day_groups:
            day_number = int(day_group.day)
            for line in (
                *(notices.day_lines or {}).get(day_number, ()),
                *hints.get(day_number, ()),
            ):
                if line and line not in preamble and line not in extra:
                    extra.append(line)
        if not extra:
            return preamble
        if preamble and not preamble.endswith("\n"):
            preamble += "\n"
        return preamble + "\n".join(extra) + "\n"

    pieces = [preamble]
    for index, (line_start, day_number) in enumerate(headings):
        end = headings[index + 1][0] if index + 1 < len(headings) else len(source)
        section = source[line_start:end]
        lines = (
            *((notices.day_lines or {}).get(int(day_number), ())),
            *hints.get(int(day_number), ()),
        )
        pieces.append(_insert_day_lines(section, lines))
    return "".join(pieces)


def apply_preference_notices(
    plan: PlanOutput,
    route: RoutePlan | None,
    request: TripRequest | None,
    *,
    program_hints: bool = False,
) -> PlanOutput:
    """Insert missing sentences without giving up fragment ownership.

    When the registry is already intact, an edit that would cross a fragment
    is refused and the plan stays as it was. A registry that was already
    broken still receives the sentences; fragment offsets are rebound when
    every fragment text can still be found in order.
    """
    if route is None or request is None:
        return plan
    notices = build_preference_notices(request, route)
    hints: dict[int, tuple[str, ...]] = {}
    for day_group in route.day_groups:
        found = program_day_hints(day_group)
        if not program_hints:
            found = [text for text in found if text == PROGRAM_PRE_DINNER_REST_HINT]
        if found:
            hints[int(day_group.day)] = tuple(found)
    if not notices.trip_lines and not (notices.day_lines or {}) and not hints:
        return plan
    updated = insert_notice_text(
        plan.plan_text or "",
        route,
        notices,
        hint_lines_by_day=hints,
    )
    summary = merge_trip_summary(plan.summary or "", notices.trip_lines)
    if updated == (plan.plan_text or "") and summary == (plan.summary or ""):
        return plan
    if not plan.poi_fragments:
        return plan.model_copy(update={"plan_text": updated, "summary": summary})
    if fragment_registry_is_intact(plan):
        remapped = remap_fragment_registry(plan, updated_text=updated)
        if remapped is None:
            if summary != (plan.summary or "") and updated == (plan.plan_text or ""):
                return plan.model_copy(update={"summary": summary})
            return plan
        if summary != (remapped.summary or ""):
            return remapped.model_copy(update={"summary": summary})
        return remapped
    rebound = _rebind_fragments(plan, updated)
    payload: dict = {"plan_text": updated, "summary": summary}
    if rebound is not None:
        payload["poi_fragments"] = rebound
    return plan.model_copy(update=payload)


def _route_places(route: RoutePlan):
    return [place for day_group in route.day_groups for place in day_group.places]


def _tea_requested(request: TripRequest) -> bool:
    texts = [*(request.preferences or [])]
    if request.notes:
        texts.append(request.notes)
    return any(
        marker in (text or "")
        for text in texts
        for marker in _TEA_MARKERS
    )


def _model_scheduled_night_days(route: RoutePlan) -> set[int]:
    """Days whose model schedule already placed a night stop."""
    found: set[int] = set()
    for day_group in route.day_groups:
        if _model_day_has_scheduled_night(day_group):
            found.add(int(day_group.day))
    return found


def _stop_kind(day_group, place_id: int) -> str:
    kinds = getattr(day_group, "stop_kinds", None) or {}
    kind = kinds.get(place_id)
    if kind is None:
        kind = kinds.get(str(place_id))
    return str(kind or "")


def _after_dinner_target(hint: str) -> str:
    text = str(hint or "").strip()
    if not text.startswith(PROGRAM_AFTER_DINNER_PREFIX):
        return ""
    return text[len(PROGRAM_AFTER_DINNER_PREFIX):].strip()


def _model_day_has_scheduled_night(day_group) -> bool:
    """A night stop, or an after-dinner hint whose place has night evidence."""
    places = list(getattr(day_group, "places", None) or [])
    for place in places:
        if _stop_kind(day_group, place.place_id) == "night":
            return True
    for hint in getattr(day_group, "time_hints", None) or []:
        target = _after_dinner_target(str(hint or ""))
        if not target:
            continue
        for place in places:
            name = (place.name or "").strip()
            if name and name == target and candidate_is_night_place(place):
                return True
    return False


def _night_interval_present(route: RoutePlan, request: TripRequest) -> bool:
    from src.agents.route_planning import route_night_visit_intervals

    return any(
        bool(route_night_visit_intervals(day_group, request))
        for day_group in route.day_groups
    )


def _night_trip_sentence(row: dict) -> str:
    reasons = {
        str(item.get("reason") or "")
        for item in (row.get("night_gaps") or [])
    }
    status = str(row.get("search_status") or "NOT_ASSESSED")
    if reasons & _NIGHT_TIME_REASONS or status == "HARD_CONFLICT":
        return NIGHT_TRIP_TIME
    if status == "NO_ELIGIBLE_MATCH":
        return NIGHT_TRIP_NO_MATCH
    return NIGHT_TRIP_UNKNOWN


def _night_day_sentence(reason: str) -> str:
    if reason in _NIGHT_DAY_RETURN_REASONS:
        return NIGHT_DAY_RETURN
    if reason in _NIGHT_TIME_REASONS:
        return NIGHT_DAY_TIME
    return NIGHT_DAY_UNKNOWN


def _food_sentence(status: object) -> str:
    if status == "NO_ELIGIBLE_MATCH":
        return FOOD_NO_MATCH
    if status == "HARD_CONFLICT":
        return FOOD_TIME
    return FOOD_UNKNOWN


def _food_fulfilled(places) -> bool:
    """A tea house counts. Light food and an unmentioned restaurant do not.

    The fixed sentence says the route has no visitor-mentioned proper meal.
    Coverage status only chooses the reason clause; it does not invent one.
    """
    from src.agents.composition_blueprint import is_light_food_place

    if any(candidate_is_tea_place(place) for place in places):
        return True
    for place in places:
        if is_light_food_place(place) or candidate_is_tea_place(place):
            continue
        if not candidate_matches_dimension(place, FOOD):
            continue
        kind = (place.place_type or "").strip().lower()
        if kind not in {"restaurant", "food"}:
            continue
        if int(place.mention_count or 0) > 0 or bool(place.top_reasons):
            return True
    return False


def _heading_line_start(match: re.Match[str]) -> int:
    """Point at the heading token. The pattern can consume blank lines before it."""
    raw = match.group(0)
    return match.start() + len(raw) - len(raw.lstrip())


def _valid_headings(text: str):
    from src.agents.route_planning import _is_day_heading_line, _parse_day_heading_number

    found = []
    for match in _HEADING_RE.finditer(text or ""):
        day_number = _parse_day_heading_number(match.group(1) or match.group(2))
        if day_number is None:
            continue
        if not _is_day_heading_line(match.group(0), match.group(3) or ""):
            continue
        found.append((_heading_line_start(match), day_number))
    return found


def _ensure_opening_lines(preamble: str, lines: tuple[str, ...]) -> str:
    missing = [line for line in lines if line and line not in preamble]
    if not missing:
        return preamble
    block = "\n".join(missing) + "\n\n"
    if preamble and not preamble.endswith("\n"):
        preamble += "\n"
    return preamble + block


def _insert_day_lines(section: str, lines: tuple[str, ...]) -> str:
    missing = [line for line in lines if line and line not in section]
    if not missing:
        return section
    newline = section.find("\n")
    if newline < 0:
        heading = section + "\n"
        rest = ""
    else:
        heading = section[:newline + 1]
        rest = section[newline + 1:]
    access = ""
    body = rest
    if rest.startswith("住宿往返"):
        access_end = rest.find("\n")
        if access_end < 0:
            access = rest if rest.endswith("\n") else rest + "\n"
            body = ""
        else:
            access = rest[:access_end + 1]
            body = rest[access_end + 1:]
    block = "".join(line + "\n" for line in missing)
    return heading + access + block + body


def _rebind_fragments(plan: PlanOutput, updated_text: str):
    cursor = 0
    rebound = []
    for fragment in sorted(plan.poi_fragments, key=lambda item: (item.start, item.end)):
        index = updated_text.find(fragment.text, cursor)
        if index < 0:
            return None
        rebound.append(fragment.model_copy(update={
            "start": index,
            "end": index + len(fragment.text),
        }))
        cursor = index + len(fragment.text)
    return rebound


