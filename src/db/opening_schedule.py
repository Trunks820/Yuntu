"""Read dated opening facts for canonical places without invoking a provider."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
import json
import re
from typing import Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.agents.holiday_calendar import PublicHoliday, load_public_holidays


Confidence = Literal["high", "medium", "unknown"]
OpeningStatus = Literal["open", "closed", "unknown"]
TIME_FIELDS = ("opens_at", "closes_at", "last_entry_at")
SCHEDULE_FIELDS = ("closed_weekdays", "holiday_rule", *TIME_FIELDS)
ACCEPTED_CONFIDENCES = frozenset({"high", "medium"})


@dataclass(frozen=True)
class PlaceOpeningSchedule:
    status: OpeningStatus = "unknown"
    confidence: Confidence = "unknown"
    opens_at: str | None = None
    closes_at: str | None = None
    last_entry_at: str | None = None
    opens_at_confidence: Confidence = "unknown"
    closes_at_confidence: Confidence = "unknown"
    last_entry_at_confidence: Confidence = "unknown"


@dataclass(frozen=True)
class SchedulePeriod:
    start_mmdd: str = "01-01"
    end_mmdd: str = "12-31"
    effective_from: date | None = None
    effective_through: date | None = None

    def __post_init__(self):
        for bound in (self.start_mmdd, self.end_mmdd):
            if not isinstance(bound, str) or not re.fullmatch(r"[0-9]{2}-[0-9]{2}", bound):
                raise ValueError("Invalid season boundary")
            date.fromisoformat(f"2000-{bound}")
        if self.effective_from and self.effective_through and self.effective_from > self.effective_through:
            raise ValueError("Invalid effective date range")

    @classmethod
    def from_rule(cls, rule: dict) -> SchedulePeriod:
        return cls(
            rule["season_start_mmdd"], rule["season_end_mmdd"],
            date.fromisoformat(rule["effective_from"]) if rule.get("effective_from") else None,
            date.fromisoformat(rule["effective_through"]) if rule.get("effective_through") else None,
        )

    def as_rule(self) -> dict:
        return {
            "season_start_mmdd": self.start_mmdd, "season_end_mmdd": self.end_mmdd,
            "effective_from": self.effective_from.isoformat() if self.effective_from else None,
            "effective_through": self.effective_through.isoformat() if self.effective_through else None,
        }

    def matches(self, day: date) -> bool:
        if (self.effective_from and day < self.effective_from) or (
            self.effective_through and day > self.effective_through
        ):
            return False
        mmdd = day.isoformat()[5:]
        if self.start_mmdd <= self.end_mmdd:
            return self.start_mmdd <= mmdd <= self.end_mmdd
        return mmdd >= self.start_mmdd or mmdd <= self.end_mmdd


def valid_schedule_value(field: str, value: object) -> bool:
    if field == "closed_weekdays":
        return (isinstance(value, list) and all(type(day) is int and 1 <= day <= 7 for day in value)
                and value == sorted(set(value)))
    if field == "holiday_rule":
        return value in ("open", "closed")
    if field in TIME_FIELDS and isinstance(value, str):
        return bool(re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value)) or (
            field == "closes_at" and value == "24:00"
        )
    return False


def _fields_for_date(rules: list[dict], visit_date: date) -> dict[str, tuple[object, Confidence]]:
    matches: dict[str, list[tuple[object, Confidence]]] = {field: [] for field in SCHEDULE_FIELDS}
    for rule in rules:
        if not isinstance(rule, dict) or not isinstance(rule.get("evidence"), dict):
            continue
        try:
            if not SchedulePeriod.from_rule(rule).matches(visit_date):
                continue
        except (KeyError, TypeError, ValueError):
            continue
        for field in SCHEDULE_FIELDS:
            proof = rule["evidence"].get(field)
            confidence = proof.get("confidence") if isinstance(proof, dict) else None
            value = rule.get(field)
            if isinstance(confidence, str) and confidence in ACCEPTED_CONFIDENCES and valid_schedule_value(field, value):
                matches[field].append((value, confidence))
    return {field: values[0] for field, values in matches.items() if len(values) == 1}


def resolve_place_opening(
    season_rules: list[dict], visit_date: date, *, holidays: Iterable[PublicHoliday] | None = None,
) -> PlaceOpeningSchedule:
    """Resolve an Asia/Shanghai civil date. Holiday hours are not inferred."""
    fields = _fields_for_date(season_rules, visit_date)
    if not fields:
        return PlaceOpeningSchedule()
    year_holidays = [row for row in (load_public_holidays() if holidays is None else holidays)
                     if row.year == visit_date.year]
    if not year_holidays:
        return PlaceOpeningSchedule()
    if any(row.start <= visit_date <= row.end for row in year_holidays):
        if "holiday_rule" not in fields:
            return PlaceOpeningSchedule()
        status, confidence = fields["holiday_rule"]
        return PlaceOpeningSchedule(status, confidence)

    status, confidence = "unknown", "unknown"
    if "closed_weekdays" in fields:
        closed_days, confidence = fields["closed_weekdays"]
        if visit_date.isoweekday() in closed_days:
            return PlaceOpeningSchedule("closed", confidence)
        status = "open"

    opens, opening_confidence = fields.get("opens_at", (None, "unknown"))
    closes, closing_confidence = fields.get("closes_at", (None, "unknown"))
    cutoff, cutoff_confidence = fields.get("last_entry_at", (None, "unknown"))
    if opens is not None and closes is not None and opens >= closes:
        return PlaceOpeningSchedule(status, confidence)
    if cutoff is not None and ((opens is not None and cutoff < opens) or (closes is not None and cutoff > closes)):
        cutoff, cutoff_confidence = None, "unknown"
    return PlaceOpeningSchedule(status, confidence, opens, closes, cutoff,
                                opening_confidence, closing_confidence, cutoff_confidence)


async def load_place_opening_schedules_for_dates(
    session: AsyncSession, place_ids: Iterable[int], visit_dates: Iterable[date],
    *, candidate_city: str | None = None,
) -> dict[date, dict[int, PlaceOpeningSchedule]]:
    """Read the rules once and resolve them for each local visit date."""
    unknown = dict.fromkeys(place_ids, PlaceOpeningSchedule())
    schedules = {day: dict(unknown) for day in visit_dates}
    if not schedules or (not unknown and not candidate_city):
        return schedules
    sql = """
        SELECT place_id, season_rules
        FROM public.travel_canonical_place_opening_schedule
        WHERE place_id = ANY(CAST(:place_ids AS bigint[]))
    """
    params: dict[str, object] = {"place_ids": list(unknown)}
    if candidate_city:
        # The scheduler can add nearby canonical candidates from this city.
        sql += """
            OR place_id IN (SELECT place_id FROM public.travel_canonical_place WHERE city = :candidate_city)
        """
        params["candidate_city"] = candidate_city
    result = await session.execute(text(sql), params)
    holidays = load_public_holidays()
    for place_id, rules in result:
        if isinstance(rules, str):
            rules = json.loads(rules)
        for day, by_id in schedules.items():
            by_id[place_id] = resolve_place_opening(rules, day, holidays=holidays)
    return schedules


async def load_place_opening_schedules(
    session: AsyncSession, place_ids: Iterable[int], visit_date: date,
) -> dict[int, PlaceOpeningSchedule]:
    """Read a date for many canonical places in one query; missing rows are unknown."""
    return (await load_place_opening_schedules_for_dates(session, place_ids, [visit_date]))[visit_date]


async def get_place_opening_schedule(
    session: AsyncSession, place_id: int, visit_date: date,
) -> PlaceOpeningSchedule:
    return (await load_place_opening_schedules(session, [place_id], visit_date))[place_id]
