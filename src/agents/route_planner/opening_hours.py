"""Trusted, dated visit limits shared by the model route and its backup."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from src.agents.schema import CandidatePlace, TripRequest, parse_hhmm
from src.db.opening_schedule import ACCEPTED_CONFIDENCES, load_place_opening_schedules_for_dates

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class VisitLimits:
    closed: bool = False
    last_entry: int | None = None
    closing: int | None = None

    def latest_arrival(self, stay: int) -> int | None:
        if self.closing is None:
            return self.last_entry
        latest = self.closing - stay
        return latest if self.last_entry is None else min(latest, self.last_entry)


_NO_LIMITS = VisitLimits()


@dataclass(frozen=True)
class OpeningDay:
    date: date
    limits: dict[int, VisitLimits]


@dataclass(frozen=True)
class RouteOpeningHours:
    by_day: dict[int, OpeningDay] = field(default_factory=dict)

    def for_place(self, day_number: int | str, place: CandidatePlace) -> VisitLimits:
        if not self.by_day:
            return _NO_LIMITS
        try:
            day = self.by_day.get(int(day_number))
        except (TypeError, ValueError, OverflowError):
            return _NO_LIMITS
        return day.limits.get(_canonical_id(place), _NO_LIMITS) if day else _NO_LIMITS


def _canonical_id(place: CandidatePlace) -> int:
    # Nearby restaurant rows also use the canonical PK as their route ID.
    return place.canonical_place_id or place.place_id


async def load_route_opening_hours(
    session: AsyncSession | None, request: TripRequest, places: Iterable[CandidatePlace],
) -> RouteOpeningHours:
    if session is None or not request.start_date:
        return RouteOpeningHours()
    try:
        start = date.fromisoformat(request.start_date.strip()[:10])
        dates = [start + timedelta(days=index) for index in range(max(1, request.days))]
    except (ValueError, OverflowError):
        return RouteOpeningHours()
    place_ids = sorted({_canonical_id(place) for place in places if _canonical_id(place) > 0})
    city = request.to_city.strip() or None
    try:
        if isinstance(session, AsyncSession) and session.in_transaction():
            # Standalone planning may reuse a session that already read candidates.
            async with session.begin_nested():
                schedules = await load_place_opening_schedules_for_dates(session, place_ids, dates, candidate_city=city)
        else:
            schedules = await load_place_opening_schedules_for_dates(session, place_ids, dates, candidate_city=city)
    except (SQLAlchemyError, OSError) as exc:
        logger.warning("opening_schedule_lookup_failed kind=%s", type(exc).__name__)
        return RouteOpeningHours()

    days: dict[int, OpeningDay] = {}
    for number, day in enumerate(dates, 1):
        by_id: dict[int, VisitLimits] = {}
        for place_id, schedule in schedules[day].items():
            if schedule.status == "closed" and schedule.confidence in ACCEPTED_CONFIDENCES:
                by_id[place_id] = VisitLimits(closed=True)
                continue
            last_entry = None
            closing = None
            if schedule.last_entry_at_confidence in ACCEPTED_CONFIDENCES:
                last_entry = parse_hhmm(schedule.last_entry_at)
            if schedule.closes_at_confidence in ACCEPTED_CONFIDENCES:
                closing = 24 * 60 if schedule.closes_at == "24:00" else parse_hhmm(schedule.closes_at)
            if last_entry is not None or closing is not None:
                by_id[place_id] = VisitLimits(last_entry=last_entry, closing=closing)
        if by_id:
            days[number] = OpeningDay(day, by_id)
    return RouteOpeningHours(days)
