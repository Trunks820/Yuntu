"""Read the public-holiday calendar and check overlap for the lodging reminder.

For lodging reminders, years not in the table fall back to 1 January, 1 May
and 1–7 October. Callers requiring verified dates can read the table directly.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_PATH = Path(__file__).resolve().parent / "data" / "public_holidays.json"
_CACHE: dict[tuple[str, int | None, int | None], tuple[PublicHoliday, ...]] = {}


@dataclass(frozen=True)
class PublicHoliday:
    year: int
    name: str
    start: date
    end: date


def fixed_holidays_for_year(year: int) -> tuple[PublicHoliday, ...]:
    return (
        PublicHoliday(year, "元旦", date(year, 1, 1), date(year, 1, 1)),
        PublicHoliday(year, "劳动节", date(year, 5, 1), date(year, 5, 1)),
        PublicHoliday(year, "国庆节", date(year, 10, 1), date(year, 10, 7)),
    )


def load_public_holidays(path: Path | str | None = None) -> tuple[PublicHoliday, ...]:
    target = Path(path) if path is not None else _DEFAULT_PATH
    try:
        stat = target.stat()
        token = (str(target.resolve()), stat.st_mtime_ns, stat.st_size)
    except OSError:
        token = (str(target), None, None)
        _CACHE[token] = ()
        return ()
    cached = _CACHE.get(token)
    if cached is not None:
        return cached
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        logger.warning("public holiday table is unreadable; using fixed dates only")
        rows: tuple[PublicHoliday, ...] = ()
        _CACHE[token] = rows
        return rows
    rows = tuple(_rows_from_payload(payload))
    _CACHE[token] = rows
    return rows


def overlapping_holiday_name(
    start: date,
    end: date,
    holidays: list[PublicHoliday] | tuple[PublicHoliday, ...] | None = None,
) -> str | None:
    """Name of the earliest holiday that overlaps the inclusive trip span."""

    if end < start:
        return None
    table = tuple(load_public_holidays() if holidays is None else holidays)
    present_years = {item.year for item in table}
    chosen: list[PublicHoliday] = []
    for year in range(start.year, end.year + 1):
        if year in present_years:
            chosen.extend(item for item in table if item.year == year)
        else:
            chosen.extend(fixed_holidays_for_year(year))
    overlaps = [
        item
        for item in chosen
        if not (end < item.start or start > item.end)
    ]
    if not overlaps:
        return None
    overlaps.sort(key=lambda item: (item.start, item.name))
    return overlaps[0].name


def _rows_from_payload(payload: object) -> list[PublicHoliday]:
    raw_rows = payload.get("holidays") if isinstance(payload, dict) else None
    if not isinstance(raw_rows, list):
        return []
    rows: list[PublicHoliday] = []
    for raw in raw_rows:
        if not isinstance(raw, dict):
            continue
        try:
            year = int(raw.get("year"))
            name = str(raw.get("name") or "").strip()
            start = date.fromisoformat(str(raw.get("start") or "").strip())
            end = date.fromisoformat(str(raw.get("end") or "").strip())
        except (TypeError, ValueError):
            continue
        if not name or end < start:
            continue
        rows.append(PublicHoliday(year=year, name=name, start=start, end=end))
    return rows
