"""Render the direction-A travel book as an A5 PDF through Chromium."""

from __future__ import annotations

import hashlib
import json
import os
import re
from base64 import b64encode
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import datetime
from html import escape
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any
from unicodedata import normalize
from uuid import uuid4
from zoneinfo import ZoneInfo

from PIL import Image, ImageOps
from pypdf import PdfReader
from pypdf.generic import StreamObject

from src.cost_estimate.public import CostCategorySummary
from src.export.city_background import CityBackgroundClient
from src.export.city_photo import CityPhotoResolver
from src.export.cost_estimate import (
    CATEGORY_LABELS,
    local_transport_taxi_suffix,
    range_text,
    validate_artifact_cost_estimate,
)
from src.export.html_render import html_document, print_pdf_html
from src.export.public_text import public_text
from src.export.route_map import AmapRouteMaps, RouteMap
from src.export.route_sketch import route_sketch_svg
from src.export.source_contract import validate_export_result_contract
from src.export.trip_art import TripArt, TripArtImage, TripArtImageClient, resolve_trip_art


PDF_MIME_TYPE = "application/pdf"
PDF_HARD_MAX_BYTES = 5 * 1024 * 1024
FONT_ENV_KEYS = ("YUNTU_TRAVEL_PDF_FONT_PATH", "EXPORT_PDF_FONT_PATH", "PDF_FONT_PATH")
_CSS_PATH = Path(__file__).parent / "templates" / "book.css"
_IMAGE_MAX_SIZE = (1200, 900)
_IMAGE_JPEG_QUALITY = 76
_ILLUSTRATION_BUDGET_BYTES = 3_500_000
_MODES = {"driving": "驾车", "walking": "步行", "transit": "公交地铁", "cycling": "骑行", "taxi": "打车"}
_INTERCITY = {"train": "火车", "flight": "飞机"}
_PERIODS = {"morning": "上午", "afternoon": "下午", "evening": "傍晚", "night": "晚上"}
_PRICE_BASIS_LABELS = {
    "sourced": "来源查价",
    "reference": "参考估算",
    "mixed": "来源与参考混合",
    "policy_zero": "按规则计为 ¥0",
}
_INTERNAL_TERMS = re.compile(
    r"(?a:\b(?:market|restaurant|driving|walking|transit|cycling|taxi)\b)|必排|未填写|在节奏限制内",
    re.IGNORECASE,
)
_TERM_LABELS = {**_MODES, "market": "市集", "restaurant": "餐馆"}
_WRITER_MARKUP = re.compile(r"<!--.*?-->|\*\*", re.S)
_PROSE_TOKEN = re.compile(r"[A-Za-z0-9_]+")
_FONT_FILES = ("/FontFile", "/FontFile2", "/FontFile3")
# Some Noto Type 3 subsets extract these CJK radical aliases outside NFKC.
# https://www.unicode.org/Public/UCD/latest/ucd/EquivalentUnifiedIdeograph.txt
_CJK_RADICAL_ALIASES = str.maketrans({
    0x2E92: "巳", 0x2E93: "幺", 0x2EA0: "民", 0x2EC5: "见", 0x2EC6: "角",
    0x2EC9: "贝", 0x2ECB: "车", 0x2ED3: "长", 0x2ED4: "门", 0x2ED8: "青",
    0x2ED9: "韦", 0x2EDA: "页", 0x2EDB: "风", 0x2EDC: "飞", 0x2EDD: "食",
    0x2EE2: "马", 0x2EE3: "骨", 0x2EE4: "鬼", 0x2EE5: "鱼", 0x2EE6: "鸟",
    0x2EE7: "卤", 0x2EE8: "麦", 0x2EE9: "黄", 0x2EEA: "黾", 0x2EEC: "齐",
    0x2EEE: "齿", 0x2EF0: "龙", 0x2EF3: "龟",
})


class PdfRenderError(RuntimeError):
    """Raised when the source or printed PDF fails validation."""

    def __init__(self, code: str, message: str | None = None) -> None:
        super().__init__(message or code)
        self.code = code


@dataclass(frozen=True)
class PdfRenderResult:
    output_path: Path
    page_count: int
    byte_size: int
    sha256: str
    text_length: int
    mime_type: str = PDF_MIME_TYPE
    storage_key: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


def render_pdf_artifact(
    export_source: Any,
    output_path: Path,
    *,
    cover_background_path: Path | None = None,
    ai_background_client: CityBackgroundClient | None = None,
    city_photo_resolver: CityPhotoResolver | None = None,
    storage_key: str | None = None,
    generated_time: datetime | None = None,
    art: TripArt | None = None,
    art_storage_dir: Path | None = None,
) -> PdfRenderResult:
    """Print the complete itinerary and publish it after validating the PDF."""
    payload = _source_payload(export_source)
    result = _validated_result(payload)
    output_path = Path(output_path)
    staging = output_path.with_name(f".{output_path.name}.{uuid4().hex}.tmp")
    # Legacy callers may pass generated_time. The cover uses the travel dates.
    try:
        client = ai_background_client if isinstance(ai_background_client, TripArtImageClient) else None
        resolve_art = art is None and any(
            value is not None for value in (art_storage_dir, client, city_photo_resolver)
        )
        scratch_dir = (
            TemporaryDirectory(prefix="yuntu-pdf-art-")
            if resolve_art and art_storage_dir is None else nullcontext()
        )
        with scratch_dir as scratch:
            if resolve_art:
                art = resolve_trip_art(
                    SimpleNamespace(source=payload), art_storage_dir or Path(scratch),
                    image_client=client, city_photo_resolver=city_photo_resolver,
                )
            html, illustrations, route_maps = _book_html(
                result, art, cover_background_path, amap_maps=AmapRouteMaps(),
            )
            pdf = print_pdf_html(html)
            if len(pdf) > PDF_HARD_MAX_BYTES and any(
                day["map_source"] == "amap" for plan in route_maps for day in plan.values()
            ):
                html, _, _ = _book_html(result, art, cover_background_path)
                pdf = print_pdf_html(html)
                for plan in route_maps:
                    for day in plan.values():
                        if day["map_source"] == "amap":
                            day.update(map_source="sketch", failure_code="pdf_size_limit")
        page_count, text_length = _validate_pdf(pdf, city=result["city"]["name"])
        output_path.parent.mkdir(parents=True, exist_ok=True)
        staging.write_bytes(pdf)
        os.replace(staging, output_path)
    except PdfRenderError:
        raise
    except Exception as exc:
        raise PdfRenderError("PDF_RENDER_FAILED") from exc
    finally:
        staging.unlink(missing_ok=True)

    return PdfRenderResult(
        output_path=output_path,
        page_count=page_count,
        byte_size=len(pdf),
        sha256=hashlib.sha256(pdf).hexdigest(),
        text_length=text_length,
        storage_key=storage_key,
        metadata={
            "renderer": "playwright",
            "renderer_version": "v0.15.4-pdf-book-1",
            "export_version": str(payload.get("export_version") or ""),
            "layout_template": "direction_a",
            "page_format": "A5",
            "cover_image_status": illustrations["cover"],
            "illustrations": illustrations,
            "route_maps": route_maps,
        },
    )


def _source_payload(export_source: Any) -> dict[str, Any]:
    payload = getattr(export_source, "source", export_source)
    if isinstance(payload, dict) and "result" not in payload:
        payload = payload.get("source")
    if not isinstance(payload, dict):
        raise PdfRenderError("INVALID_EXPORT_SOURCE")
    return payload


def _validated_result(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        result = payload["result"]
        validate_export_result_contract(result)
        request = result["request"]
        if not _label(result["city"]["name"]) or int(request["days"]) <= 0:
            raise ValueError("invalid trip cover")
        plans = result["plans"]
        if not isinstance(plans, list) or not plans:
            raise ValueError("missing plans")
        for plan in plans:
            if not _label(plan["title"]) or not _label(plan["summary"]):
                raise ValueError("missing plan header")
            validate_artifact_cost_estimate(plan["cost_estimate"])
            days = plan["days"]
            if not isinstance(days, list) or not days:
                raise ValueError("missing days")
            if len({int(day["day"]) for day in days}) != len(days):
                raise ValueError("duplicate day")
            for day in days:
                _validate_day(day, allow_empty_narrative=result["schema_version"] == "2.3")
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise PdfRenderError("INVALID_EXPORT_SOURCE") from exc
    return result


def _validate_day(day: dict[str, Any], *, allow_empty_narrative: bool) -> None:
    if int(day["day"]) <= 0 or not _label(day["title"]):
        raise ValueError("missing day header")
    narrative = day["narrative"]
    if not isinstance(narrative, str) or (not narrative.strip() and not (allow_empty_narrative and narrative == "")):
        raise ValueError("missing day narrative")
    places = day["places"]
    if not isinstance(places, list) or not places:
        raise ValueError("missing places")
    place_ids = set()
    for place in places:
        place_id = int(place["place_id"])
        if place_id <= 0 or not _label(place["name"]):
            raise ValueError("invalid place")
        place_ids.add(place_id)
    legs = day["commute_legs"]
    if not _label(day["commute_summary"]) or not isinstance(legs, list) or (len(places) > 1 and not legs):
        raise ValueError("missing commute")
    for leg in legs:
        origin, destination = int(leg["from_place_id"]), int(leg["to_place_id"])
        if origin not in place_ids or destination not in place_ids or origin == destination:
            raise ValueError("invalid commute endpoints")
        if not leg["mode"] or int(leg["duration_minutes"]) < 0 or int(leg["distance_meters"]) < 0:
            raise ValueError("invalid commute details")


def _label(value: Any) -> str:
    return public_text(_replace_terms(str(value))) if value is not None else ""


def _replace_terms(value: str) -> str:
    return _INTERNAL_TERMS.sub(lambda match: _TERM_LABELS.get(match[0].lower(), ""), value)


def _prose(value: Any, *, css_class: str = "prose") -> str:
    if not isinstance(value, str):
        return ""
    # Sanitize taxonomy tokens without changing punctuation or line breaks in Writer prose.
    text = _replace_terms(_WRITER_MARKUP.sub("", value))
    text = _PROSE_TOKEN.sub(lambda match: public_text(match[0]), text).strip()
    return f'<p class="{css_class}">{escape(text)}</p>' if text else ""


def _joined(*values: str) -> str:
    return " · ".join(value for value in values if value)


def _figure(art: TripArtImage | None, *, max_bytes: int, cover: bool = False) -> tuple[str, str]:
    paper = '<figure class="art paper-art" aria-hidden="true"></figure>'
    if art is None:
        return paper, "paper"
    try:
        with Image.open(art.path) as original:
            image = ImageOps.exif_transpose(original).convert("RGB")
            image.thumbnail(_IMAGE_MAX_SIZE, Image.Resampling.LANCZOS)
            jpeg = _jpeg_bytes(image, max_bytes)
    except (OSError, ValueError, Image.DecompressionBombError):
        return paper, "paper"
    credit = ""
    if entry := art.photo_entry:
        title = "封面摄影" if cover else "摄影"
        links = []
        for url, label in ((entry.source_page_url, "原图"), (entry.license_url, "许可")):
            if url and url.startswith(("https://", "http://")):
                links.append(f'<a href="{escape(url)}">{label}</a>')
        attribution = _joined(
            escape(f"{title}：{_label(entry.author)}"), escape(_label(entry.provider)),
            escape(_label(entry.license)), *links,
        )
        credit = f"<figcaption>{attribution}。{escape(_label(entry.modification_notice))}</figcaption>"
    data = b64encode(jpeg).decode("ascii")
    return f'<figure class="art"><img src="data:image/jpeg;base64,{data}" alt="">{credit}</figure>', art.status


def _jpeg_bytes(image: Image.Image, max_bytes: int) -> bytes:
    while True:
        output = BytesIO()
        image.save(output, "JPEG", quality=_IMAGE_JPEG_QUALITY, optimize=True)
        if output.tell() <= max_bytes or image.size == (1, 1):
            return output.getvalue()
        size = tuple(max(1, int(side * 0.8)) for side in image.size)
        image = image.resize(size, Image.Resampling.LANCZOS)


def _cover(result: dict[str, Any], cover_art: str, *, summary: str) -> tuple[str, str]:
    request = result["request"]
    title = f'{_label(result["city"]["name"])} {int(request["days"])} 天'
    dates = " 至 ".join(dict.fromkeys(
        text for key in ("start_date", "end_date") if (text := _label(request.get(key)))
    ))
    people = request.get("people_count")
    meta = _joined(dates, f"{people} 人" if people else "")
    return title, f"""
<section class="cover">
  {cover_art}
  <div class="cover-copy">
    <p class="eyebrow">云途旅行册</p>
    <h1>{escape(title)}</h1>
    <p class="cover-meta">{escape(meta)}</p>
    {_prose(summary, css_class="cover-summary")}
  </div>
</section>"""


def _chapter(title: str, body: str, *, label: str = "", intro: str = "") -> str:
    if not body:
        return ""
    kicker = f'<p class="eyebrow">{escape(label)}</p>' if label else ""
    return f'<section class="chapter">{intro}<header>{kicker}<h2>{escape(title)}</h2></header>{body}</section>'


def _stop(number: int, place: dict, next_place: dict | None, leg: dict | None) -> str:
    schedule = place.get("schedule") or {}
    clock = _label(schedule.get("exact_start"))
    if clock:
        arrival = f"约 {clock} 到" if schedule.get("exact_time_source") == "planned" else f"{clock} 到"
    else:
        arrival = _PERIODS.get(schedule.get("period"), "")
    stay = place.get("stay_minutes")
    meta = _joined(arrival, f"停留 {stay} 分钟" if stay is not None else "")
    heading = f'<div class="stop-meta">{escape(meta)}</div>' if meta else ""
    onward = ""
    if next_place is not None and leg is not None:
        mode = _MODES.get(leg["mode"], "")
        duration = f'{mode}{"约 " if leg.get("duration_source") == "estimate" else " "}{leg["duration_minutes"]} 分钟'
        transit = _label(leg.get("transit_summary")) if leg["mode"] == "transit" else ""
        detail = _joined(f'去 {_label(next_place["name"])}', duration.strip(), transit)
        onward = f'<p class="onward">{escape(detail)}</p>'
    activity = _prose(place.get("activity_note")) or _prose(_label(place.get("brief")))
    return f"""<li class="stop">
      <header><span class="stop-no">{number}</span><h3>{escape(_label(place["name"]))}</h3>{heading}</header>
      {activity}{onward}
    </li>"""


def _day_chapter(
    day: dict, day_art: str, *, opening: str | None, amap_maps: AmapRouteMaps | None,
) -> tuple[str, dict[str, str]]:
    places = day["places"]
    legs = {}
    for leg in day["commute_legs"]:
        key = int(leg["from_place_id"]), int(leg["to_place_id"])
        legs.setdefault(key, leg)
    try:
        route_map = amap_maps.fetch(places, legs) if amap_maps else RouteMap(failure_code="sketch_only")
    except Exception:
        # Optional map failures must stay local. Upstream exceptions may contain a key.
        route_map = RouteMap(failure_code="map_failed")
    if route_map.jpeg is not None:
        data = b64encode(route_map.jpeg).decode("ascii")
        route = (
            '<figure class="route">'
            f'<img src="data:image/jpeg;base64,{data}" alt="当天路线地图" '
            'style="display:block;width:100%;height:calc(100% - 5mm);object-fit:contain">'
            '<figcaption>高德地图</figcaption></figure>'
        )
        map_status = {"map_source": "amap"}
    else:
        sketch = route_sketch_svg(places, legs)
        route = f'<figure class="route">{sketch}<figcaption>路线示意</figcaption></figure>' if sketch else ""
        map_status = {"map_source": "sketch", "failure_code": route_map.failure_code or "map_failed"}
    lodging = []
    narrative = []
    for line in day["narrative"].splitlines():
        if line.strip().startswith(("住宿往返：", "住宿往返:")):
            lodging.append(line)
        else:
            narrative.append(line)
    stops = []
    for index, place in enumerate(places):
        next_place = places[index + 1] if index + 1 < len(places) else None
        leg = legs.get((int(place["place_id"]), int(next_place["place_id"]))) if next_place else None
        stops.append(_stop(index + 1, place, next_place, leg))
    body = (
        _prose("\n".join(dict.fromkeys(lodging)), css_class="lodging-trip")
        + f'<div class="day-visuals">{day_art}{route}</div>'
        + _prose("\n".join(narrative))
        + f'<ol class="stops">{"".join(stops)}</ol>'
    )
    chapter = _chapter(
        _label(day["title"]), body, label=f'第 {day["day"]} 天',
        intro=_prose(opening, css_class="prose trip-opening"),
    )
    return chapter, map_status


def _row(title: str, detail: str, *, suffix: str = "") -> str:
    note = f'<span class="row-suffix">{escape(suffix)}</span>' if suffix else ""
    return f'<div class="row"><strong>{escape(title)}</strong><p>{escape(detail)}{note}</p></div>'


def _card(title: str, body: str) -> str:
    return f'<section class="card"><h3>{escape(title)}</h3>{body}</section>' if body else ""


def _before_departure(weather: dict, plan: dict, *, summary: str = "") -> str:
    weather_rows = []
    for day in (weather or {}).get("days") or []:
        low, high = day.get("temp_min_c"), day.get("temp_max_c")
        temps = list(dict.fromkeys(temp for temp in (low, high) if temp is not None))
        temperature = " 至 ".join(map(str, temps)) + " ℃" if temps else ""
        label = _label(day.get("date")) or f'第 {day["day"]} 天'
        conditions = _joined(_label(day.get("weather_text")), temperature, _label(day.get("wind_text")))
        reminders = " ".join(filter(None, map(_label, day.get("reminders") or [])))
        if conditions or reminders:
            weather_rows.append(_row(label, "。".join(filter(None, (conditions, reminders)))))
    packing = []
    for group in plan.get("packing_checklist") or []:
        items = "、".join(filter(None, map(_label, group.get("items") or [])))
        if items:
            packing.append(_row(_label(group.get("category")), items))
    tips = []
    for tip in plan.get("travel_tips") or []:
        if content := _prose(tip.get("content")):
            tips.append(f'<div class="row"><strong>{escape(_label(tip.get("title")))}</strong>{content}</div>')
    summary_card = _card("行程说明", f'<p class="plan-summary">{escape(summary)}</p>') if summary else ""
    return _chapter(
        "出发前",
        summary_card
        + _card("天气", "".join(weather_rows))
        + _card("出行清单", "".join(packing))
        + _card("旅行贴士", "".join(tips)),
    )


def _transport(transport: dict) -> str:
    modes = []
    seen_options = set()
    for mode in transport.get("modes") or []:
        label = _INTERCITY.get(mode.get("mode"))
        if not label:
            continue
        options = []
        for option in mode.get("options") or []:
            identity = tuple(option.get(key) for key in (
                "type", "no", "departure_time", "arrival_time", "departure_station", "arrival_station",
            ))
            if identity in seen_options:
                continue
            seen_options.add(identity)
            times = " → ".join(filter(None, (_label(option.get("departure_time")), _label(option.get("arrival_time")))))
            stations = " → ".join(filter(None, (_label(option.get("departure_station")), _label(option.get("arrival_station")))))
            minutes = option.get("duration_minutes")
            detail = _joined(stations, f"{minutes} 分钟" if minutes else "", _label(option.get("price")))
            options.append(_row(_joined(_label(option.get("no")), times), detail))
        if options:
            modes.append(f'<h4>{label}</h4>{"".join(options)}')
        elif not mode.get("options") and (mode.get("min_duration_minutes") or mode.get("price_range")):
            minutes = mode.get("min_duration_minutes")
            modes.append(_row(label, _joined(f"最快 {minutes} 分钟" if minutes else "", _label(mode.get("price_range")))))
    if not modes:
        return ""
    direction = " → ".join(filter(None, (_label(transport.get("from_city")), _label(transport.get("to_city")))))
    lead = _joined(direction, _label(transport.get("query_date")))
    return _card("城际交通", _prose(lead, css_class="route-line") + "".join(modes))


def _accommodation(stay: dict) -> str:
    display = stay.get("display") or {}
    area = _label(display.get("area_label") or stay.get("name"))
    details = _prose(area, css_class="route-line")
    details += _prose(display.get("caption") or stay.get("reason"))
    for group in display.get("groups") or []:
        hotels = "、".join(filter(None, map(_label, group.get("hotels") or [])))
        low, high = group.get("nightly_low_cny"), group.get("nightly_high_cny")
        price = ""
        if low is not None and high is not None:
            price = f"{low} 元 / 晚" if low == high else f"{low}–{high} 元 / 晚"
        if hotels or price:
            details += _row(_joined(_label(group.get("label")), price), hotels)
    for notice in display.get("notices") or []:
        details += _prose(notice.get("text"), css_class="muted")
    return _card("住宿参考", details)


def _range(value: Any) -> str:
    return range_text(value).replace("¥", "￥")


def _cost_group(categories: list[CostCategorySummary], taxi_suffix: str, text: list[str]) -> str:
    priced = []
    pending = []
    for category in categories:
        label = CATEGORY_LABELS[category.category]
        suffix = taxi_suffix if category.category == "local_transport" else ""
        value = category.range or category.known_range
        if value is None:
            pending.append(_row(label, "待确认"))
            continue

        if category.price_basis == "policy_zero":
            detail = _PRICE_BASIS_LABELS[category.price_basis]
        elif category.price_basis is None:
            detail = f"已知 {_range(value)}"
        else:
            detail = f"{_range(value)}（{_PRICE_BASIS_LABELS[category.price_basis]}）"
        priced.append(_row(label, detail, suffix=suffix))
        text.append(f"{label}：{detail}{suffix}")
        if category.unpriced_item_count:
            pending.append(_row(label, f"另有 {category.unpriced_item_count} 处待确认"))
    return _card("已估费用", "".join(priced)) + _card("待确认费用", "".join(pending))


def _cost_chapter(raw: Any, text: list[str] | None = None) -> str:
    summary = validate_artifact_cost_estimate(raw)
    scenarios = summary.scenarios
    taxi_suffix = local_transport_taxi_suffix(summary)
    local_time = summary.estimated_at.astimezone(ZoneInfo("Asia/Shanghai"))
    timestamp = f"费用参考 · 更新于 {local_time:%Y-%m-%d %H:%M}"
    if text is None:
        text = []
    text.extend(("行程消费预估", timestamp))
    body = _prose(timestamp, css_class="muted")

    # Render unchanged line items once and keep different intercity estimates with their totals.
    shared = [
        category for index, category in enumerate(scenarios[0].categories)
        if (len(scenarios) == 1 or category.category != "intercity_transport")
        and all(scenario.categories[index] == category for scenario in scenarios[1:])
    ] if scenarios else []
    shared_names = {category.category for category in shared}
    for scenario in scenarios:
        total = scenario.total_range_with_known_admission or scenario.total_range
        total_label = "预估" if scenario.total_scope == "full_trip" else "已估"
        total_text = f"{total_label} {_range(total)}" if total is not None else "费用暂不可估算"
        body += f'<div class="total"><h3>{escape(_label(scenario.label))}</h3><strong>{escape(total_text)}</strong></div>'
        body += _cost_group(
            [category for category in scenario.categories if category.category not in shared_names],
            taxi_suffix, text,
        )

    if shared:
        label = "共同费用" if len(scenarios) > 1 else "费用明细"
        text.append(label)
        if len(scenarios) > 1:
            body += f'<h3 class="cost-heading">{label}</h3>'
        body += _cost_group(shared, taxi_suffix, text)
    if summary.exclusions:
        exclusions = "；".join(_label(item.label) for item in summary.exclusions)
        body += _prose(f"未计范围：{exclusions}", css_class="muted")
    text.append(summary.notice)
    return _chapter("花多少", body + _prose(summary.notice, css_class="muted"))


def _styles(font_name: str | None = None) -> str:
    css = _CSS_PATH.read_text(encoding="utf-8")
    if font_name is not None:
        return css + f":root {{--book-font: {json.dumps(font_name)};}}"
    for env_key in FONT_ENV_KEYS:
        if path := os.getenv(env_key):
            font = Path(path).expanduser()
            if font.suffix.lower() in {".ttf", ".ttc", ".otf"} and font.is_file():
                data = b64encode(font.read_bytes()).decode("ascii")
                return css + (
                    f'@font-face {{font-family: "Book Override"; src: url(data:font/otf;base64,{data});}}'
                    ':root {--book-font: "Book Override";}'
                )
    return css


def _add_cost_estimate(story: list, raw: Any, styles: Any, text_parts: list[str]) -> None:
    """Compatibility entry for checks of the cost copy from the former layout."""
    story.append(_cost_chapter(raw, text_parts))


def _book_html(
    result: dict, art: TripArt | None, background: Path | None, *, amap_maps: AmapRouteMaps | None = None,
) -> tuple[str, dict, list[dict[str, dict[str, str]]]]:
    image_limit = _ILLUSTRATION_BUDGET_BYTES // (1 + len(result["plans"][0]["days"]))
    cover_image, cover_status = _figure(art.cover if art else None, max_bytes=image_limit, cover=True)
    if background is not None:
        provided_image, provided_status = _figure(
            TripArtImage(Path(background), "cached"), max_bytes=image_limit, cover=True,
        )
        if provided_status != "paper":
            cover_image, cover_status = provided_image, "provided"
    is_basic = result.get("published_variant") in ("safe", "basic")
    primary_summary = result["plans"][0]["summary"]
    title, cover = _cover(result, cover_image, summary="" if is_basic else primary_summary)
    pages = [cover]
    illustrations = {"cover": cover_status, "days": {}}
    route_maps = []
    for index, plan in enumerate(result["plans"]):
        plan_maps = {}
        if index:
            pages.append(_chapter(_label(plan["title"]), _prose(plan["summary"])))
        # Legacy books can include alternatives for the same travel dates.
        plan_amap_maps = amap_maps if index == 0 else None
        for day_index, day in enumerate(plan["days"]):
            illustration = art.days.get(int(day["day"])) if art and index == 0 else None
            day_art, status = _figure(illustration, max_bytes=image_limit)
            if index == 0:
                illustrations["days"][str(day["day"])] = status
            opening = plan.get("opening") if day_index == 0 else None
            chapter, map_status = _day_chapter(
                day, day_art, opening=opening, amap_maps=plan_amap_maps,
            )
            pages.append(chapter)
            plan_maps[str(day["day"])] = map_status
        route_maps.append(plan_maps)
        departure_summary = primary_summary if is_basic and index == 0 else ""
        pages.append(_before_departure(result.get("weather"), plan, summary=departure_summary))
        pages.append(_chapter(
            "怎么去 · 住哪",
            _transport(plan.get("transport") or {}) + _accommodation(plan.get("accommodation") or {}),
        ))
        pages.append(_cost_chapter(plan["cost_estimate"]))
    return html_document(title, _styles(), "".join(pages)), illustrations, route_maps


def _normalize_pdf_text(text: str) -> str:
    return normalize("NFKC", text).translate(_CJK_RADICAL_ALIASES)


def _validate_pdf(pdf: bytes, *, city: str) -> tuple[int, int]:
    if not pdf.startswith(b"%PDF"):
        raise PdfRenderError("PDF_RENDER_FAILED")
    if len(pdf) > PDF_HARD_MAX_BYTES:
        raise PdfRenderError("PDF_SIZE_EXCEEDED")
    pages = PdfReader(BytesIO(pdf)).pages
    count = len(pages)
    if not count:
        raise PdfRenderError("PDF_PAGE_COUNT_INVALID")
    page_text = [page.extract_text() or "" for page in pages]
    text = "".join(page_text)
    text_length = len("".join(text.split()))
    if not text_length:
        raise PdfRenderError("PDF_TEXT_EMPTY")
    cover = "".join(_normalize_pdf_text(page_text[0]).split())
    city_label = "".join(_normalize_pdf_text(_label(city)).split())
    if "云途旅行册" not in cover or city_label not in cover or not _has_only_embedded_fonts(pages):
        raise PdfRenderError("PDF_FONT_UNAVAILABLE")
    return count, text_length


def _has_font_file(font: Any) -> bool:
    descriptor = font.get("/FontDescriptor")
    if descriptor is None:
        return False
    descriptor = descriptor.get_object()
    return any(isinstance(descriptor[key], StreamObject) for key in _FONT_FILES if key in descriptor)


def _font_is_embedded(font: Any) -> bool:
    subtype = font.get("/Subtype")
    if subtype == "/Type0":
        descendants = font["/DescendantFonts"] if "/DescendantFonts" in font else []
        return bool(descendants) and all(_has_font_file(ref.get_object()) for ref in descendants)
    if subtype == "/Type3":
        char_procs = font.get("/CharProcs")
        return char_procs is not None and bool(char_procs.get_object())
    return subtype in {"/TrueType", "/Type1", "/MMType1"} and _has_font_file(font)


def _has_only_embedded_fonts(pages: Any) -> bool:
    pending = [page.get("/Resources") for page in pages]
    visited = set()
    found_fonts = False
    while pending:
        reference = pending.pop()
        if reference is None:
            continue
        resources = reference.get_object()
        if id(resources) in visited:
            continue
        visited.add(id(resources))
        for ref in resources["/Font"].values() if "/Font" in resources else ():
            font = ref.get_object()
            if not _font_is_embedded(font):
                return False
            found_fonts = True
            pending.append(font.get("/Resources"))
        for key in ("/XObject", "/Pattern"):
            if key in resources:
                pending.extend(ref.get_object().get("/Resources") for ref in resources[key].values())
    return found_fonts
