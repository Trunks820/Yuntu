"""Render the direction-A itinerary as a single shareable PNG."""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from datetime import date, datetime
from html import escape
from io import BytesIO
from pathlib import Path
from typing import Any
from uuid import uuid4

from PIL import Image

from src.config import get_settings
from src.export.city_photo import CityPhotoResolver
from src.export.cost_estimate import validate_artifact_cost_estimate
from src.export.html_render import HtmlDimensionsError, html_document, image_data_uri, screenshot_html
from src.export.image_prompt import TRIP_ART_PROMPT_VERSION, is_food_stop, shop_display_name
from src.export.public_text import public_items, public_text
from src.export.source_contract import validate_export_result_contract
from src.export.trip_art import TripArt, TripArtImage, TripArtImageClient, resolve_trip_art


SHARE_IMAGE_OUTPUT_WIDTH = 1024
SHARE_IMAGE_OUTPUT_MAX_HEIGHT = 12_000
SHARE_IMAGE_HARD_MAX_BYTES = 10 * 1024 * 1024
SHARE_IMAGE_MIME_TYPE = "image/png"

_CSS_PATH = Path(__file__).parent / "templates" / "share_long.css"
_PERIODS = {"morning": "上午", "afternoon": "下午", "evening": "晚上"}
_MONEY_RE = re.compile(
    r"[¥￥$＄€£₩₽₹]|人民币|\b(?:CNY|RMB|USD|HKD|EUR|JPY|GBP)\b"
    r"|\d\s*(?:元|块钱|块(?![一-鿿])|港元|美元|日元|欧元|英镑)"
    r"|[零一二两三四五六七八九十百千万亿半]+(?:元|块钱)(?=[左右余上下起，。；！？\s]|$)"
    r"|(?:人均|预算|花费|门票|费用)\s*[约为:：]?\s*\d",
    re.IGNORECASE,
)
_CLAUSE_DELIMITER = re.compile(r"([，。；;！？])")


class ShareImageRenderError(RuntimeError):
    """Raised when a share image cannot be composed or validated."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ShareImageRenderResult:
    width_px: int
    height_px: int
    byte_size: int
    sha256: str
    metadata: dict[str, Any]
    mime_type: str = SHARE_IMAGE_MIME_TYPE
    storage_key: str | None = None


@dataclass(frozen=True)
class ShareDay:
    number: int
    title: str
    periods: dict[str, list[str]]


@dataclass(frozen=True)
class ShareSummary:
    city: str
    dates: str
    people_count: int
    preferences: tuple[str, ...]
    plan_summary: str
    days: tuple[ShareDay, ...]


def render_share_image_artifact(
    export_source: Any,
    output_path: Path,
    *,
    art: TripArt | None = None,
    ai_background_client: TripArtImageClient | None = None,
    city_photo_resolver: CityPhotoResolver | None = None,
    art_storage_dir: Path | None = None,
    generated_time: datetime | None = None,
    storage_key: str | None = None,
) -> ShareImageRenderResult:
    """Compose an itinerary from the export source and its shared illustration set."""
    summary = build_share_summary(export_source)
    output_path = Path(output_path)
    staging = output_path.with_name(f".{output_path.name}.{uuid4().hex}.tmp")
    # Legacy callers may still pass generated_time. The cover uses travel dates.
    try:
        use_art_cache = any(value is not None for value in (
            art_storage_dir, ai_background_client, city_photo_resolver,
        ))
        if art is None and use_art_cache:
            art = resolve_trip_art(
                export_source,
                art_storage_dir if art_storage_dir is not None else Path(get_settings().export_storage_dir),
                image_client=ai_background_client,
                city_photo_resolver=city_photo_resolver,
            )
        png = screenshot_html(
            build_share_long_html(summary, art),
            width=SHARE_IMAGE_OUTPUT_WIDTH,
            max_height=SHARE_IMAGE_OUTPUT_MAX_HEIGHT,
        )
        png, height = _validate_png(png)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        staging.write_bytes(png)
        os.replace(staging, output_path)
    except ShareImageRenderError:
        raise
    except HtmlDimensionsError as exc:
        raise ShareImageRenderError("SHARE_IMAGE_DIMENSIONS_INVALID") from exc
    except Exception as exc:
        raise ShareImageRenderError("SHARE_IMAGE_RENDER_FAILED") from exc
    finally:
        staging.unlink(missing_ok=True)

    art_statuses = {
        "cover": art.cover.status if art else "paper",
        "days": {
            str(day.number): art.days[day.number].status if art and day.number in art.days else "paper"
            for day in summary.days
        },
    }
    return ShareImageRenderResult(
        width_px=SHARE_IMAGE_OUTPUT_WIDTH,
        height_px=height,
        byte_size=len(png),
        sha256=hashlib.sha256(png).hexdigest(),
        storage_key=_safe_storage_key(storage_key),
        metadata={
            "renderer": "playwright",
            "renderer_version": "v0.15.4-share-long-1",
            "layout_template": "direction_a",
            "layout_width_px": SHARE_IMAGE_OUTPUT_WIDTH,
            "delivery_width_px": SHARE_IMAGE_OUTPUT_WIDTH,
            "delivery_height_px": height,
            "content_source": "backend_export_source",
            "text_rendering": "html",
            "prompt_version": TRIP_ART_PROMPT_VERSION,
            "day_count": len(summary.days),
            "illustrations": art_statuses,
        },
    )


def build_share_summary(export_source: Any) -> ShareSummary:
    payload = getattr(export_source, "source", export_source)
    if isinstance(payload, dict) and "result" not in payload:
        payload = payload.get("source")
    try:
        result = payload["result"]
        validate_export_result_contract(result)
        city = public_text(result["city"]["name"].strip())
        request = result["request"]
        plans = result["plans"]
        if not city or not isinstance(request, dict) or not plans:
            raise ValueError("invalid share cover")
        for plan in plans:
            validate_artifact_cost_estimate(plan.get("cost_estimate"))
        plan = plans[0]
        days = tuple(_day(day) for day in plan["days"])
        if not days or len({day.number for day in days}) != len(days):
            raise ValueError("missing or duplicate share days")
        raw_preferences = request.get("preferences") or []
        preferences = public_items(raw_preferences, limit=len(raw_preferences))
        is_basic = result.get("published_variant") in ("safe", "basic")
        return ShareSummary(
            city=city,
            dates=_travel_dates(request),
            people_count=int(request.get("people_count") or 0),
            preferences=tuple(dict.fromkeys(
                label for value in preferences if (label := _without_prices(value))
            )),
            plan_summary="" if is_basic else _without_prices(public_text(plan.get("summary"))),
            days=days,
        )
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise ShareImageRenderError("INVALID_EXPORT_SOURCE") from exc


def _day(day: dict[str, Any]) -> ShareDay:
    number = int(day["day"])
    if number <= 0:
        raise ValueError("invalid share day")
    periods: dict[str, list[str]] = {period: [] for period in _PERIODS}
    current_period = "morning"
    has_schedule = False
    all_names = []
    sights = []
    for place in day["places"]:
        period = (place.get("schedule") or {}).get("period")
        if period == "night":
            period = "evening"
        if period in periods:
            current_period = period
            has_schedule = True
        name = public_text(place.get("name"))
        if not name:
            continue
        if is_food_stop(place):
            name = shop_display_name(name)
        else:
            sights.append(name)
        periods[current_period].append(name)
        all_names.append(name)
    if not all_names:
        raise ValueError("empty share day")
    title = " · ".join(list(dict.fromkeys(sights or all_names))[:3])
    return ShareDay(number, title, periods if has_schedule else {"unscheduled": all_names})


def _without_prices(text: str) -> str:
    clean = text.replace("…", "")
    if not _MONEY_RE.search(clean):
        return clean
    parts = _CLAUSE_DELIMITER.split(clean)
    return "".join(
        clause + delimiter
        for clause, delimiter in zip(parts[::2], [*parts[1::2], ""])
        if not _MONEY_RE.search(clause)
    ).strip()


def _travel_dates(request: dict[str, Any]) -> str:
    parsed = []
    for key in ("start_date", "end_date"):
        try:
            parsed.append(date.fromisoformat(request.get(key) or ""))
        except (ValueError, TypeError):
            parsed.append(None)
    start, end = parsed
    if start and end and start < end:
        date_format = "%m.%d" if start.year == end.year else "%Y.%m.%d"
        return f"{start:%Y.%m.%d} — {end:{date_format}}"
    known = start or end
    return f"{known:%Y.%m.%d}" if known else "日期待确认"


def build_share_long_html(summary: ShareSummary, art: TripArt | None = None) -> str:
    """Format a share cover and every day using the production stylesheet."""
    title = f"{summary.city}{len(summary.days)}天"
    party = f"<span>{summary.people_count} 人同行</span>" if summary.people_count > 0 else ""
    preferences = " · ".join(summary.preferences)
    preference_line = f'<p class="cover-preferences">{escape(preferences)}</p>' if preferences else ""
    intro = f'<p class="cover-intro">{escape(summary.plan_summary)}</p>' if summary.plan_summary else ""
    cover = f"""<header class="cover">
  {_art_html(art.cover if art else None)}
  <div class="cover-copy">
    <p class="cover-label">云途旅行册</p>
    <h1>{escape(title)}</h1>
    <p class="cover-meta"><span>{escape(summary.dates)}</span>{party}</p>
    {preference_line}{intro}
  </div>
</header>"""
    days = "".join(_day_html(day, art.days.get(day.number) if art else None) for day in summary.days)
    body = f'<main id="long" class="share">{cover}<div class="days">{days}</div></main>'
    return html_document(title, _CSS_PATH.read_text(encoding="utf-8"), body)


def _day_html(day: ShareDay, art: TripArtImage | None) -> str:
    periods = []
    for period, stops in day.periods.items():
        if not stops:
            continue
        label = _PERIODS.get(period)
        heading = f"<h3>{label}</h3>" if label else ""
        names = "".join(f"<li>{escape(name)}</li>" for name in stops)
        periods.append(f'<div class="period {period}">{heading}<ul>{names}</ul></div>')
    return f"""<section class="day">
  <header class="day-head">
    {_art_html(art)}
    <div class="day-copy">
      <p class="day-label">第 {day.number} 天</p>
      <h2>{escape(day.title)}</h2>
    </div>
  </header>
  <div class="periods">{''.join(periods)}</div>
</section>"""


def _art_html(art: TripArtImage | None) -> str:
    if art is None:
        return '<figure class="art paper-art" aria-hidden="true"></figure>'
    credit = ""
    if art.photo_entry is not None:
        credit = f"<figcaption>{escape(public_text(art.photo_entry.short_credit))}</figcaption>"
    return f'<figure class="art"><img src="{image_data_uri(art.path)}" alt="">{credit}</figure>'


def _validate_png(png: bytes) -> tuple[bytes, int]:
    with Image.open(BytesIO(png)) as image:
        width, height = image.size
        if image.format != "PNG" or width != SHARE_IMAGE_OUTPUT_WIDTH or not 0 < height <= SHARE_IMAGE_OUTPUT_MAX_HEIGHT:
            raise ShareImageRenderError("SHARE_IMAGE_DIMENSIONS_INVALID")
        image.load()
        if len(png) > SHARE_IMAGE_HARD_MAX_BYTES:
            compact = BytesIO()
            image.convert("RGB").quantize(colors=256).save(compact, format="PNG", optimize=True)
            png = compact.getvalue()
    if len(png) > SHARE_IMAGE_HARD_MAX_BYTES:
        raise ShareImageRenderError("SHARE_IMAGE_TOO_LARGE")
    return png, height


def _safe_storage_key(value: str | None) -> str | None:
    if value and not re.match(r"^(?:[A-Za-z]:[\\/]|/|\\\\)", value):
        return value
    return None
