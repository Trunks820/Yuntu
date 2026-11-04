"""Caller-supplied holidays only. No live calendar feed."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class PublicHoliday:
    name: str
    start: date
    end: date


def overlapping_holiday_name(
    start: date,
    end: date,
    holidays: list[PublicHoliday] | tuple[PublicHoliday, ...] | None = None,
) -> str:
    if not holidays or end < start:
        return ""
    for holiday in holidays:
        if holiday.start <= end and start <= holiday.end and holiday.name:
            return holiday.name
    return ""
