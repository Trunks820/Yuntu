"""v0.15.1: per-stop card fields for Result 2.3 days.

The web shows one card per place (time or period, name, activity_note,
stay_minutes, the leg to the next place). Until v0.15.1 travel sent none of
these, so the Writer's per-place prose sat inside the day narrative. This
module splits a day's delivered text into the day narrative (opening, wrap,
code notes) and one note per place, and reads each place's clock from the
route's day timeline. Nothing here edits the delivered plan text.
"""
from __future__ import annotations

import re
from typing import Any

from src.agents.composition_blueprint import COFFEE_TYPES, FOOD_TYPES, SNACK_TYPES
from src.agents.schema import DayTimeline, parse_hhmm

_MEAL_PLACE_TYPES = FOOD_TYPES | SNACK_TYPES | COFFEE_TYPES
_MARKUP_RE = re.compile(r"<!--.*?-->|\*\*", flags=re.S)
OPEN_TIME_MAX_CHARS = 30
NOTE_MAX_CHARS = 600


def _line(text: str) -> str:
    return re.sub(r"[ \t　]+", " ", _MARKUP_RE.sub("", text)).strip()


# Fixed lines the basic renderer (safe_plan_renderer) writes between and
# after places. The leg card already says how to get to the next stop.
BASIC_FILLER_LINES = frozenset({
    "随后按既定路线前往下一站。",
    "当天仅安排以上地点，依次完成对应活动；地点之间沿既定路线前往，途中可按自身节奏短暂休息，完成后结束当天行程。",
})


def _dropped(text: str) -> bool:
    return text.startswith("通勤参考") or text in BASIC_FILLER_LINES


def split_day_text(body: str, place_names: dict[int, str]) -> tuple[list[str], dict[int, str]]:
    """Day body lines that are not a place's own paragraph, and each place's paragraph.

    A keyed Writer paragraph starts with "地点名：", a basic-plan sentence with
    "到地点名后，". The label is dropped because the card already shows the name.
    A line between two places (a basic plan's food line) joins the place
    before it; lines before the first and after the last place stay with the
    day. Commute references and the basic plan's fixed filler are dropped.
    """
    labels: list[tuple[str, int, bool]] = []
    for place_id, name in place_names.items():
        if name:
            labels.append((f"{name}：", place_id, False))
            labels.append((f"到{name}后，", place_id, True))
    labels.sort(key=lambda item: -len(item[0]))
    lines = [text for text in (_line(raw) for raw in body.splitlines()) if text]
    matched = [next((label for label in labels if text.startswith(label[0])), None) for text in lines]
    place_rows = [index for index, match in enumerate(matched) if match is not None]
    kept: list[str] = []
    notes: dict[int, str] = {}
    current: int | None = None
    basic = False
    for index, (text, match) in enumerate(zip(lines, matched)):
        if match is not None and match[1] not in notes:
            current, basic = match[1], match[2]
            note = text[len(match[0]):].strip()
            if note:
                notes[current] = note
            continue
        if _dropped(text):
            continue
        # A basic plan has no wrap: its trailing line is the last meal's text.
        if current is not None and (index < place_rows[-1] or basic):
            notes[current] = "\n".join(part for part in (notes.get(current), text) if part)
            continue
        kept.append(text)
    return kept, notes


def practical_line(place: dict[str, Any]) -> str:
    """Amap hours, average spend and rating as one code-written line."""
    parts: list[str] = []
    segments: list[str] = []
    for segment in str(place.get("amap_open_time") or "").split():
        if segment not in segments:
            segments.append(segment)
    hours = " ".join(segments).replace("-", "–")
    if hours and len(hours) <= OPEN_TIME_MAX_CHARS:
        parts.append(f"营业 {hours}")
    if str(place.get("place_type") or "") in _MEAL_PLACE_TYPES:
        try:
            price = float(place.get("amap_avg_price") or 0)
        except (TypeError, ValueError):
            price = 0
        if price > 0:
            parts.append(f"人均约 {round(price)} 元")
    try:
        rating = float(place.get("amap_rating") or 0)
    except (TypeError, ValueError):
        rating = 0
    if 0 < rating <= 5:
        parts.append(f"高德评分 {rating:g}")
    return " · ".join(parts)


def activity_note(note: str, place: dict[str, Any]) -> str | None:
    note = note.strip()
    if len(note) > NOTE_MAX_CHARS:
        note = note[: NOTE_MAX_CHARS - 1].rstrip() + "…"
    facts = practical_line(place)
    text = "\n".join(part for part in (note, facts) if part)
    return text or None


def period_for(minute: int) -> str:
    if minute < 12 * 60:
        return "morning"
    if minute < 17 * 60:
        return "afternoon"
    if minute < 19 * 60:
        return "evening"
    return "night"


def _quarter(minute: int) -> str:
    rounded = min(round(minute / 15) * 15, 23 * 60 + 45)
    return f"{rounded // 60:02d}:{rounded % 60:02d}"


def stop_fields(timeline: DayTimeline | None, *, planned_time: bool) -> dict[int, dict[str, Any]]:
    """schedule and stay_minutes for each place on the timeline, first visit only."""
    fields: dict[int, dict[str, Any]] = {}
    for item in (timeline.items if timeline is not None else []):
        if item.place_id is None or item.place_id in fields:
            continue
        minute = parse_hhmm(item.arrive)
        if minute is None:
            continue
        schedule: dict[str, Any] = {"period": period_for(minute)}
        if planned_time:
            schedule.update(exact_start=_quarter(minute), exact_time_source="planned")
        fields[item.place_id] = {"schedule": schedule, "stay_minutes": item.stay_minutes}
    return fields
