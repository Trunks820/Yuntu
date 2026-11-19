"""Backend-rendered share image poster for v0.8.10.1 export artifacts."""

from __future__ import annotations

import hashlib
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageEnhance, ImageFont, ImageOps

from src.export.city_background import (
    CITY_BACKGROUND_ASSET_KEYS,
    FALLBACK_BACKGROUND_PATH,
    CityBackgroundClient,
    fallback_city_background,
    fallback_city_background_with_metadata,
    resolve_city_background,
)
from src.export.image_prompt import (
    SHARE_IMAGE_PROMPT_LAYOUT_VERSION,
    SHARE_IMAGE_PROMPT_VERSION,
    ShareDaySummary,
    ShareImageSummary,
    SharePlaceDetail,
    build_share_image_prompt_payload,
)
from src.export.public_text import public_items, public_text
from src.export.source_contract import validate_export_result_contract


SHARE_IMAGE_LAYOUT_WIDTH = 1440
SHARE_IMAGE_LAYOUT_MAX_HEIGHT = 4800
SHARE_IMAGE_OUTPUT_WIDTH = 1024
SHARE_IMAGE_OUTPUT_MAX_HEIGHT = round(
    SHARE_IMAGE_LAYOUT_MAX_HEIGHT * SHARE_IMAGE_OUTPUT_WIDTH / SHARE_IMAGE_LAYOUT_WIDTH
)
from src.export.cost_estimate import validate_artifact_cost_estimate
# Compatibility aliases describe the final artifact, not the internal fallback canvas.
SHARE_IMAGE_WIDTH = SHARE_IMAGE_OUTPUT_WIDTH
SHARE_IMAGE_MAX_HEIGHT = SHARE_IMAGE_OUTPUT_MAX_HEIGHT
SHARE_IMAGE_TARGET_BYTES = 3 * 1024 * 1024
SHARE_IMAGE_HARD_MAX_BYTES = 10 * 1024 * 1024
SHARE_IMAGE_MIME_TYPE = "image/png"
FONT_ENV_KEYS = (
    "YUNTU_TRAVEL_SHARE_IMAGE_FONT_PATH",
    "EXPORT_IMAGE_FONT_PATH",
    "EXPORT_PDF_FONT_PATH",
    "PDF_FONT_PATH",
)



class ShareImageRenderError(RuntimeError):
    """Raised when a share image cannot be composed or validated."""

    def __init__(self, code: str, message: str | None = None) -> None:
        super().__init__(message or code)
        self.code = code


@dataclass(frozen=True)
class ShareImageRenderResult:
    width_px: int
    height_px: int
    byte_size: int
    sha256: str
    mime_type: str = SHARE_IMAGE_MIME_TYPE
    storage_key: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


AiBackgroundClient = CityBackgroundClient


@dataclass(frozen=True)
class _FontInfo:
    path: Path
    source: str


@dataclass(frozen=True)
class _Fonts:
    title: ImageFont.FreeTypeFont
    subtitle: ImageFont.FreeTypeFont
    section: ImageFont.FreeTypeFont
    day_title: ImageFont.FreeTypeFont
    body: ImageFont.FreeTypeFont
    small: ImageFont.FreeTypeFont
    chip: ImageFont.FreeTypeFont
    micro: ImageFont.FreeTypeFont
    source: str


@dataclass
class _TextBoxTracker:
    boxes: list[tuple[int, int, int, int, str]] = field(default_factory=list)

    def add(self, box: tuple[int, int, int, int], label: str) -> None:
        if box[2] <= box[0] or box[3] <= box[1]:
            return
        self.boxes.append((box[0], box[1], box[2], box[3], label))

    def overlap_count(self) -> int:
        count = 0
        for index, first in enumerate(self.boxes):
            for second in self.boxes[index + 1:]:
                if _boxes_overlap(first[:4], second[:4]):
                    count += 1
        return count


def render_share_image_artifact(
    export_source: Any,
    output_path: Path,
    *,
    ai_background_client: AiBackgroundClient | None = None,
    generated_time: datetime | None = None,
    storage_key: str | None = None,
) -> ShareImageRenderResult:
    """Render a final AI poster or deterministic fallback from export source."""

    summary = build_share_summary(export_source)
    prompt_payload = build_share_image_prompt_payload(summary)
    # The share poster carries no monetary text, so a generated image cannot
    # misstate an amount. Cost figures stay in the PDF and the web result.
    background_image, background_metadata = _background_from_ai_or_fallback(
        prompt_payload.prompt,
        ai_background_client,
        summary.city,
    )
    is_generated = background_metadata["background_status"] == "generated"
    fonts: _Fonts | None = None
    if is_generated:
        poster = _prepare_generated_final_poster(background_image)
        layout_metadata = {
            "layout_text_box_count": 0,
            "layout_text_overlap_count": 0,
            "layout_width_px": background_image.width,
            "layout_height_px": background_image.height,
            "background_composition": "ai_final_poster_passthrough",
            "backend_draw_operations": 0,
        }
    else:
        fonts = _load_fonts()
        poster, layout_metadata = _compose_fallback_poster(
            summary,
            background_image,
            fonts,
            _normalize_time(generated_time),
        )
        poster = _normalize_delivery_poster(poster)
    if (
        poster.width != SHARE_IMAGE_OUTPUT_WIDTH
        or poster.height > SHARE_IMAGE_OUTPUT_MAX_HEIGHT
    ):
        raise ShareImageRenderError("SHARE_IMAGE_DIMENSIONS_INVALID")

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = output_path.with_name(f".{output_path.name}.{uuid.uuid4().hex}.tmp")
    try:
        render_metadata = _write_and_validate_png(poster, temp_path)
        os.replace(temp_path, output_path)
    except ShareImageRenderError:
        _unlink_silent(temp_path)
        raise
    except Exception as exc:  # pragma: no cover - exercised through callers.
        _unlink_silent(temp_path)
        raise ShareImageRenderError("SHARE_IMAGE_RENDER_FAILED") from exc

    metadata: dict[str, Any] = {
        "renderer": "pillow",
        "renderer_version": "j1k-share-image-1k-delivery-v1",
        "layout_width_px": layout_metadata["layout_width_px"],
        "delivery_width_px": poster.width,
        "delivery_height_px": poster.height,
        "layout_template": (
            "ai_full_poster_final_v1"
            if is_generated
            else "deterministic_scrapbook_fallback_v1"
        ),
        "prompt_version": SHARE_IMAGE_PROMPT_VERSION,
        "prompt_layout_version": SHARE_IMAGE_PROMPT_LAYOUT_VERSION,
        "content_source": "backend_export_source",
        "ai_background_scope": "single_final_poster_with_controlled_script",
        "background_role": "final_poster" if is_generated else "fallback_city_source",
        "global_background_placement": "full_canvas",
        "content_render_mode": (
            "ai_full_poster" if is_generated else "deterministic_fallback"
        ),
        "text_rendering": "ai_generated" if is_generated else "backend_pillow",
        "deterministic_overlay": not is_generated,
        "day_block_max_place_tags": 3,
        "core_place_list_rendered": False,
        "background_status": background_metadata["background_status"],
        "day_count": summary.days,
        "compression_mode": summary.compression_mode,
        "core_place_chip_count": len(summary.core_place_chips),
        "visible_script_char_count": prompt_payload.visible_script_char_count,
        "reference_context_char_count": prompt_payload.reference_context_char_count,
        "reference_context_truncated": prompt_payload.reference_context_truncated,
        "font_source": fonts.source if fonts is not None else None,
    }
    metadata.update(background_metadata)
    metadata.update(layout_metadata)
    metadata.update(render_metadata)

    return ShareImageRenderResult(
        width_px=poster.width,
        height_px=poster.height,
        byte_size=render_metadata["byte_size"],
        sha256=render_metadata["sha256"],
        storage_key=_safe_storage_key(storage_key),
        metadata=metadata,
    )


def build_share_summary(export_source: Any) -> ShareImageSummary:
    payload = _source_payload(export_source)
    result = _validated_result(payload)
    request = _dict(result.get("request"))
    plans = _list(result.get("plans"))
    plan = plans[0]
    raw_days = _list(plan.get("days"))
    request_days = _to_int(request.get("days"))
    days = request_days if request_days > 0 else len(raw_days)
    if days <= 0:
        raise ShareImageRenderError("INVALID_EXPORT_SOURCE")

    compression_mode = _compression_mode(days)
    preferences = tuple(_public_items(request.get("preferences"), limit=4))
    avoid = tuple(_public_items(request.get("avoid"), limit=3))
    route_style = _route_style_label(plan)
    day_summaries = tuple(
        _day_summary(day, compression_mode)
        for day in raw_days[: min(days, 7)]
    )
    core_place_chips = tuple(_core_place_chips(raw_days, days))
    people_count = _to_int(request.get("people_count"))
    return ShareImageSummary(
        city=_city_name(result),
        days=days,
        people_count=people_count if people_count > 0 else None,
        preferences=preferences,
        avoid=avoid,
        route_style_label=route_style,
        day_summaries=day_summaries,
        core_place_chips=core_place_chips,
        suitable_for=_suitable_for(people_count, preferences, avoid, route_style),
        compression_mode=compression_mode,
        notes=_user_facing_text(request.get("notes")),
        plan_title=_user_facing_text(plan.get("title")),
        plan_summary=_user_facing_text(plan.get("summary")),
        plan_tags=tuple(_public_items(plan.get("tags"), limit=50)),
        pace_summary=_pace_summary(plan),
        weather_lines=tuple(_weather_lines(result)),
        time_preference_lines=tuple(_time_preference_lines(result)),
    )


def _source_payload(export_source: Any) -> dict[str, Any]:
    if hasattr(export_source, "source"):
        value = getattr(export_source, "source")
        if isinstance(value, dict):
            return value
    if isinstance(export_source, dict):
        if isinstance(export_source.get("result"), dict):
            return export_source
        nested = export_source.get("source")
        if isinstance(nested, dict) and isinstance(nested.get("result"), dict):
            return nested
    raise ShareImageRenderError("INVALID_EXPORT_SOURCE")


def _validated_result(payload: dict[str, Any]) -> dict[str, Any]:
    result = payload.get("result")
    if not isinstance(result, dict):
        raise ShareImageRenderError("INVALID_EXPORT_SOURCE")
    try:
        validate_export_result_contract(result)
    except (TypeError, ValueError) as exc:
        raise ShareImageRenderError("INVALID_EXPORT_SOURCE") from exc
    if not isinstance(result.get("city"), dict):
        raise ShareImageRenderError("INVALID_EXPORT_SOURCE")
    if not _user_facing_text(_dict(result.get("city")).get("name")):
        raise ShareImageRenderError("INVALID_EXPORT_SOURCE")
    if not isinstance(result.get("request"), dict):
        raise ShareImageRenderError("INVALID_EXPORT_SOURCE")
    plans = result.get("plans")
    if not isinstance(plans, list) or not plans:
        raise ShareImageRenderError("INVALID_EXPORT_SOURCE")
    for plan in plans:
        if not isinstance(plan, dict):
            raise ShareImageRenderError("INVALID_EXPORT_SOURCE")
        try:
            validate_artifact_cost_estimate(plan.get("cost_estimate"))
        except (TypeError, ValueError) as exc:
            raise ShareImageRenderError("INVALID_EXPORT_SOURCE") from exc
        days = plan.get("days")
        if not isinstance(days, list) or not days:
            raise ShareImageRenderError("INVALID_EXPORT_SOURCE")
        for day in days:
            if not isinstance(day, dict):
                raise ShareImageRenderError("INVALID_EXPORT_SOURCE")
            if _to_int(day.get("day")) <= 0 or not _user_facing_text(day.get("title")):
                raise ShareImageRenderError("INVALID_EXPORT_SOURCE")
            places = day.get("places")
            if not isinstance(places, list) or not places:
                raise ShareImageRenderError("INVALID_EXPORT_SOURCE")
            if not any(_user_facing_text(_dict(place).get("name")) for place in places):
                raise ShareImageRenderError("INVALID_EXPORT_SOURCE")
    return result


def _day_summary(day: dict[str, Any], compression_mode: str) -> ShareDaySummary:
    day_no = _to_int(day.get("day"))
    places = _list(day.get("places"))
    place_details: list[SharePlaceDetail] = []
    for place in places:
        name = _user_facing_text(place.get("name"))
        if not name:
            continue
        place_details.append(
            SharePlaceDetail(
                name=name,
                category=_user_facing_text(place.get("category")),
                brief=_user_facing_text(place.get("brief")),
            )
        )
    place_names = [place.name for place in place_details]
    if not place_names:
        raise ShareImageRenderError("INVALID_EXPORT_SOURCE")
    title = _user_facing_text(day.get("title"), f"第{day_no}天")
    theme = _theme_text(day, place_names)

    if compression_mode == "rich":
        bullets = _compressed_bullets(day, limit=2, length=34)
        chips = place_names[:3]
    elif compression_mode == "compressed":
        bullets = _compressed_bullets(day, limit=1, length=32)
        chips = place_names[:3]
    else:
        bullets = []
        chips = place_names[:2]

    return ShareDaySummary(
        day=day_no,
        title=title,
        theme=theme,
        bullets=tuple(bullets),
        place_chips=tuple(chips),
        narrative=_user_facing_text(day.get("narrative")),
        commute_summary=_user_facing_text(day.get("commute_summary")),
        places=tuple(place_details),
    )


def _compressed_bullets(day: dict[str, Any], *, limit: int, length: int) -> list[str]:
    candidates: list[str] = []
    narrative = _text(day.get("narrative"))
    candidates.extend(_sentences(narrative))
    commute = _text(day.get("commute_summary"))
    if commute:
        candidates.append(commute)
    for place in _list(day.get("places")):
        brief = _text(place.get("brief"))
        if brief:
            candidates.append(brief)

    bullets: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        compact = _truncate(_user_facing_text(item), length)
        if compact and compact not in seen:
            bullets.append(compact)
            seen.add(compact)
        if len(bullets) >= limit:
            break
    return bullets


def _core_place_chips(days: list[dict[str, Any]], day_count: int) -> list[str]:
    limit = 10 if day_count <= 3 else 12 if day_count <= 5 else 14
    chips: list[str] = []
    seen: set[str] = set()
    for day in days:
        for place in _list(day.get("places")):
            name = _user_facing_text(place.get("name"))
            if name and name not in seen:
                chips.append(name)
                seen.add(name)
            if len(chips) >= limit:
                return chips
    return chips


def _compose_fallback_poster(
    summary: ShareImageSummary,
    background_image: Image.Image,
    fonts: _Fonts,
    generated_time: datetime,
) -> tuple[Image.Image, dict[str, Any]]:
    margin = 76
    content_width = SHARE_IMAGE_LAYOUT_WIDTH - margin * 2
    day_rows: list[tuple[ShareDaySummary, int, int]] = []
    cursor_y = 492
    for day in summary.day_summaries:
        height = _measure_day_card_height(day, content_width, summary.compression_mode, fonts)
        day_rows.append((day, cursor_y, height))
        cursor_y += height + 22
    footer_y = cursor_y + 34
    final_height = min(max(1900, footer_y + 480), SHARE_IMAGE_LAYOUT_MAX_HEIGHT)
    canvas = _prepare_fallback_poster_base(background_image, final_height, day_rows)
    draw = ImageDraw.Draw(canvas, "RGBA")
    tracker = _TextBoxTracker()

    _draw_header(draw, summary, fonts, tracker, margin, content_width)
    _draw_days(draw, summary, fonts, tracker, margin, content_width, day_rows)
    footer_bottom = _draw_footer(
        draw,
        summary,
        generated_time,
        fonts,
        tracker,
        margin,
        footer_y,
        content_width,
    )

    if footer_bottom + 220 > final_height:
        raise ShareImageRenderError("SHARE_IMAGE_DIMENSIONS_INVALID")
    poster = canvas.convert("RGB")
    overlap_count = tracker.overlap_count()
    if overlap_count:
        raise ShareImageRenderError("SHARE_IMAGE_TEXT_OVERLAP")
    return poster, {
        "layout_text_box_count": len(tracker.boxes),
        "layout_text_overlap_count": overlap_count,
        "layout_width_px": SHARE_IMAGE_LAYOUT_WIDTH,
        "layout_height_px": final_height,
        "layout_last_day_bottom_px": (
            day_rows[-1][1] + day_rows[-1][2] if day_rows else 0
        ),
        "layout_footer_bottom_px": footer_bottom,
        "background_composition": "fallback_deterministic_scrapbook",
        "backend_draw_operations": len(tracker.boxes),
    }


def _draw_header(
    draw: ImageDraw.ImageDraw,
    summary: ShareImageSummary,
    fonts: _Fonts,
    tracker: _TextBoxTracker,
    x: int,
    width: int,
) -> None:
    _draw_paper_protection(draw, (x - 16, 42, x + 850, 220), alpha=176)
    draw.rectangle((x, 62, x + 10, 100), fill=(210, 91, 59, 235))
    _draw_text(draw, "城市旅行攻略", (x + 28, 58), fonts.micro, "#6E4633", tracker, "eyebrow")
    _draw_text(draw, f"{summary.city} {summary.days}天", (x, 106), fonts.title, "#172B46", tracker, "hero-title")

    facts: list[str] = []
    if summary.people_count:
        facts.append(f"{summary.people_count}人同行")
    if summary.preferences:
        facts.append("偏好 " + "、".join(summary.preferences[:3]))
    if summary.avoid:
        facts.append("避开 " + "、".join(summary.avoid[:2]))
    facts_text = "  |  ".join(facts) or f"{summary.days}天城市慢游"
    _draw_paper_protection(draw, (x - 8, 238, x + min(width, 1100), 306), alpha=196)
    _draw_wrapped_text(
        draw,
        facts_text,
        x + 14,
        255,
        min(width, 1060),
        fonts.small,
        "#182842",
        tracker,
        "header-facts",
        max_lines=1,
    )
    _draw_paper_protection(draw, (x - 8, 324, x + 660, 390), alpha=160)
    draw.line((x + 12, 353, x + 42, 353), fill=(199, 79, 50, 235), width=8)
    _draw_text(
        draw,
        _fit_text_to_width(summary.route_style_label or "城市慢游路线", fonts.small, 570),
        (x + 58, 337),
        fonts.small,
        "#172B46",
        tracker,
        "route-label",
    )


def _draw_days(
    draw: ImageDraw.ImageDraw,
    summary: ShareImageSummary,
    fonts: _Fonts,
    tracker: _TextBoxTracker,
    x: int,
    width: int,
    day_rows: list[tuple[ShareDaySummary, int, int]],
) -> None:
    _draw_paper_protection(draw, (x - 12, 410, x + 360, 466), alpha=158)
    draw.line((x, 442, x + 26, 442), fill=(210, 91, 59, 240), width=8)
    _draw_text(draw, "每日安排", (x + 42, 420), fonts.section, "#172B46", tracker, "days-heading")

    axis_x = x + 24
    if day_rows:
        first_y = day_rows[0][1] + 30
        last_y = day_rows[-1][1] + day_rows[-1][2] - 22
        draw.line((axis_x, first_y, axis_x, last_y), fill=(136, 91, 60, 185), width=4)
    for day, row_y, height in day_rows:
        _draw_day_card(draw, day, x, row_y, width, height, summary.compression_mode, fonts, tracker)


def _draw_day_card(
    draw: ImageDraw.ImageDraw,
    day: ShareDaySummary,
    x: int,
    y: int,
    width: int,
    height: int,
    mode: str,
    fonts: _Fonts,
    tracker: _TextBoxTracker,
) -> None:
    accent = "#D96542" if day.day % 2 else "#172B46"
    axis_x = x + 24
    text_x = x + 82
    text_width = min(810, width - 430)
    node_y = y + 36
    _draw_paper_protection(draw, (text_x - 16, y - 6, text_x + text_width + 20, y + height + 6), alpha=190)
    draw.line((axis_x + 27, node_y, text_x - 18, node_y), fill=(136, 91, 60, 185), width=3)
    draw.ellipse((axis_x - 27, node_y - 27, axis_x + 27, node_y + 27), fill=(246, 226, 190, 235), outline=accent, width=5)
    _draw_text(
        draw,
        str(day.day),
        (axis_x - 8 if day.day < 10 else axis_x - 15, node_y - 16),
        fonts.micro,
        accent,
        tracker,
        f"day-{day.day}-number",
    )
    draw.rounded_rectangle((text_x, y + 12, text_x + 142, y + 60), radius=8, fill=_hex_to_rgba(accent, 232))
    _draw_text(draw, f"DAY {day.day}", (text_x + 18, y + 20), fonts.micro, "#FFF3DF", tracker, f"day-{day.day}-band")
    title_x = text_x + 162
    _draw_text(
        draw,
        _fit_text_to_width(day.title, fonts.day_title, text_width - 180),
        (title_x, y + 14),
        fonts.day_title,
        "#172B46",
        tracker,
        f"day-{day.day}-title",
    )
    draw.line((text_x, y + 70, text_x + text_width, y + 70), fill=(156, 104, 67, 110), width=2)
    body_x = text_x + 8
    body_y = y + 88
    body_width = text_width - 16
    if mode == "compact":
        body_y = _draw_wrapped_text(
            draw,
            day.theme,
            body_x,
            body_y,
            body_width,
            fonts.small,
            "#304058",
            tracker,
            f"day-{day.day}-theme",
            max_lines=1,
        )
    else:
        for index, bullet in enumerate(day.bullets):
            bullet_y = body_y
            _draw_small_dot(draw, body_x, bullet_y + 14, accent)
            body_y = _draw_wrapped_text(
                draw,
                bullet,
                body_x + 22,
                body_y,
                body_width - 22,
                fonts.small,
                "#304058",
                tracker,
                f"day-{day.day}-bullet-{index}",
                max_lines=1,
            )
            body_y += 10
    body_y += 8
    if day.place_chips:
        _draw_chips(
            draw,
            list(day.place_chips[:3]),
            body_x,
            body_y,
            body_width,
            fonts.micro,
            tracker,
            max_rows=1,
            fill="#F9E6C7",
            text_fill="#7A352A",
            stroke="#D9B58D",
        )
    draw.line((text_x + 8, y + height - 14, text_x + text_width - 8, y + height - 14), fill=(156, 104, 67, 90), width=1)


def _draw_footer(
    draw: ImageDraw.ImageDraw,
    summary: ShareImageSummary,
    generated_time: datetime,
    fonts: _Fonts,
    tracker: _TextBoxTracker,
    x: int,
    y: int,
    width: int,
) -> int:
    _ = generated_time
    footer_width = min(880, width - 360)
    _draw_paper_protection(draw, (x - 14, y - 10, x + footer_width + 26, y + 208), alpha=186)
    draw.line((x + 4, y + 34, x + 36, y + 34), fill=(210, 91, 59, 238), width=8)
    _draw_text(draw, "适合谁", (x + 52, y + 12), fonts.section, "#172B46", tracker, "footer-heading")
    _draw_wrapped_text(
        draw,
        summary.suitable_for,
        x + 4,
        y + 72,
        footer_width,
        fonts.small,
        "#304058",
        tracker,
        "footer-suitable",
        max_lines=1,
    )
    tags = list(summary.preferences[:3])
    if summary.avoid:
        tags.append("避开 " + "、".join(summary.avoid[:1]))
    if tags:
        _draw_chips(
            draw,
            tags[:4],
            x + 4,
            y + 118,
            footer_width,
            fonts.micro,
            tracker,
            max_rows=1,
            fill="#F3D8AD",
            text_fill="#70372D",
            stroke="#C99E73",
        )
    _draw_text(
        draw,
        f"{summary.city} · 把喜欢的城市慢慢走一遍",
        (x + 4, y + 182),
        fonts.micro,
        "#6E5541",
        tracker,
        "footer-signoff",
    )
    return y + 218


def _measure_day_card_height(
    day: ShareDaySummary | None,
    width: int,
    mode: str,
    fonts: _Fonts,
) -> int:
    if day is None:
        return 0
    body_width = min(790, width - 450)
    height = 88
    if mode == "compact":
        height += _wrapped_height(day.theme, fonts.small, body_width, max_lines=1)
        if day.place_chips:
            height += 16 + 48
        return max(186, height + 30)

    for bullet in day.bullets:
        height += _wrapped_height(bullet, fonts.small, body_width - 22, max_lines=1) + 10
    if day.place_chips:
        height += 14 + 48
    minimum = 238 if mode == "rich" else 204
    return max(minimum, height + 28)


def _background_from_ai_or_fallback(
    prompt: str,
    ai_background_client: AiBackgroundClient | None,
    city: str,
) -> tuple[Image.Image, dict[str, Any]]:
    background = resolve_city_background(prompt, ai_background_client)
    if background.metadata.get("background_status") != "fallback":
        return background.image, background.metadata
    fallback_key = CITY_BACKGROUND_ASSET_KEYS.get(city)
    fallback_image, fallback_metadata = fallback_city_background_with_metadata(fallback_key)
    metadata = dict(background.metadata)
    metadata.update(fallback_metadata)
    return fallback_image, metadata


def _fallback_background() -> Image.Image:
    return fallback_city_background()


def _prepare_generated_final_poster(image: Image.Image) -> Image.Image:
    return _normalize_delivery_poster(image)


def _normalize_delivery_poster(image: Image.Image) -> Image.Image:
    source = ImageOps.exif_transpose(image).convert("RGB")
    if source.width <= 0 or source.height <= 0:
        raise ShareImageRenderError("SHARE_IMAGE_DIMENSIONS_INVALID")
    target_height = max(
        1,
        int(round(source.height * SHARE_IMAGE_OUTPUT_WIDTH / source.width)),
    )
    if source.size == (SHARE_IMAGE_OUTPUT_WIDTH, target_height):
        resized = source
    else:
        resized = source.resize(
            (SHARE_IMAGE_OUTPUT_WIDTH, target_height),
            Image.Resampling.LANCZOS,
        )
    if resized.height > SHARE_IMAGE_OUTPUT_MAX_HEIGHT:
        resized = resized.crop(
            (0, 0, SHARE_IMAGE_OUTPUT_WIDTH, SHARE_IMAGE_OUTPUT_MAX_HEIGHT)
        )
    return resized


def _prepare_fallback_poster_base(
    image: Image.Image,
    height: int,
    day_rows: list[tuple[ShareDaySummary, int, int]],
) -> Image.Image:
    canvas = Image.new("RGBA", (SHARE_IMAGE_LAYOUT_WIDTH, height), "#F1DFC0")
    paper_draw = ImageDraw.Draw(canvas, "RGBA")
    for y in range(18, height, 34):
        paper_draw.line(
            (0, y, SHARE_IMAGE_LAYOUT_WIDTH, y),
            fill=(126, 91, 62, 10),
            width=1,
        )

    top_height = 350
    top_photo = _city_photo_crop(
        image,
        SHARE_IMAGE_LAYOUT_WIDTH,
        top_height,
        centering=(0.5, 0.28),
    )
    canvas.alpha_composite(top_photo, dest=(0, 0))
    canvas.alpha_composite(
        Image.new(
            "RGBA",
            (SHARE_IMAGE_LAYOUT_WIDTH, top_height),
            (218, 144, 86, 28),
        ),
        dest=(0, 0),
    )

    vignette_x = 1000
    vignette_width = SHARE_IMAGE_LAYOUT_WIDTH - vignette_x - 62
    for index, (_, row_y, row_height) in enumerate(day_rows):
        vignette_height = max(130, row_height - 18)
        centering_y = min(0.82, 0.20 + index * 0.11)
        vignette = _city_photo_crop(
            image,
            vignette_width,
            vignette_height,
            centering=(0.60, centering_y),
        )
        vignette.alpha_composite(
            Image.new("RGBA", vignette.size, (201, 116, 67, 24)),
        )
        mask = Image.new("L", vignette.size, 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            (0, 0, vignette.width - 1, vignette.height - 1),
            radius=10,
            fill=238,
        )
        vignette.putalpha(mask)
        canvas.alpha_composite(vignette, dest=(vignette_x, row_y + 8))

    bottom_height = 230
    bottom_photo = _city_photo_crop(
        image,
        SHARE_IMAGE_LAYOUT_WIDTH,
        bottom_height,
        centering=(0.5, 0.76),
    )
    canvas.alpha_composite(bottom_photo, dest=(0, height - bottom_height))
    canvas.alpha_composite(
        Image.new(
            "RGBA",
            (SHARE_IMAGE_LAYOUT_WIDTH, bottom_height),
            (20, 42, 67, 24),
        ),
        dest=(0, height - bottom_height),
    )
    return canvas


def _draw_paper_protection(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    *,
    alpha: int,
) -> None:
    draw.rounded_rectangle(
        box,
        radius=10,
        fill=(247, 230, 197, alpha),
        outline=(125, 82, 52, min(74, alpha)),
        width=1,
    )


def _city_photo_crop(
    image: Image.Image,
    width: int,
    height: int,
    *,
    centering: tuple[float, float],
) -> Image.Image:
    photo = ImageOps.fit(
        image.convert("RGB"),
        (width, height),
        method=Image.Resampling.LANCZOS,
        centering=centering,
    )
    photo = ImageEnhance.Contrast(photo).enhance(1.05)
    photo = ImageEnhance.Color(photo).enhance(1.04)
    return photo.convert("RGBA")


def _write_and_validate_png(image: Image.Image, temp_path: Path) -> dict[str, Any]:
    image.save(temp_path, format="PNG", optimize=True, compress_level=9)
    data = temp_path.read_bytes()
    byte_size = len(data)
    if byte_size > SHARE_IMAGE_HARD_MAX_BYTES:
        quantized = image.convert("RGB").quantize(colors=256, method=Image.Quantize.MEDIANCUT)
        quantized.save(temp_path, format="PNG", optimize=True, compress_level=9)
        data = temp_path.read_bytes()
        byte_size = len(data)
    if byte_size <= 0:
        raise ShareImageRenderError("SHARE_IMAGE_EMPTY")
    if byte_size > SHARE_IMAGE_HARD_MAX_BYTES:
        raise ShareImageRenderError("SHARE_IMAGE_SIZE_EXCEEDED")
    with Image.open(temp_path) as saved:
        if (
            saved.width != SHARE_IMAGE_OUTPUT_WIDTH
            or saved.height > SHARE_IMAGE_OUTPUT_MAX_HEIGHT
        ):
            raise ShareImageRenderError("SHARE_IMAGE_DIMENSIONS_INVALID")
    return {
        "byte_size": byte_size,
        "sha256": hashlib.sha256(data).hexdigest(),
        "target_size_ok": byte_size <= SHARE_IMAGE_TARGET_BYTES,
        "hard_size_ok": byte_size < SHARE_IMAGE_HARD_MAX_BYTES,
    }


def _load_fonts() -> _Fonts:
    info = _font_info()

    def load(size: int) -> ImageFont.FreeTypeFont:
        return ImageFont.truetype(str(info.path), size=size)

    try:
        return _Fonts(
            title=load(94),
            subtitle=load(36),
            section=load(38),
            day_title=load(34),
            body=load(30),
            small=load(27),
            chip=load(27),
            micro=load(23),
            source=info.source,
        )
    except Exception as exc:
        raise ShareImageRenderError("SHARE_IMAGE_FONT_UNAVAILABLE") from exc


def _font_info() -> _FontInfo:
    for path, source in _font_candidates():
        if path.is_file():
            return _FontInfo(path=path, source=source)
    raise ShareImageRenderError("SHARE_IMAGE_FONT_UNAVAILABLE")


def _font_candidates() -> list[tuple[Path, str]]:
    candidates: list[tuple[Path, str]] = []
    for env_key in FONT_ENV_KEYS:
        raw = os.getenv(env_key)
        if raw:
            candidates.append((Path(raw).expanduser(), f"env:{env_key}"))

    package_root = Path(__file__).resolve().parent
    fonts_dir = package_root / "fonts"
    if fonts_dir.is_dir():
        for path in sorted(fonts_dir.glob("*")):
            if path.suffix.lower() in {".ttf", ".ttc", ".otf"}:
                candidates.append((path, "repo-font"))

    candidates.extend(
        [
            (Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"), "linux:noto-sans-cjk"),
            (Path("/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc"), "linux:noto-serif-cjk"),
            (Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"), "linux:noto-sans-cjk"),
            (Path(r"C:\Windows\Fonts\Noto Sans SC (TrueType).otf"), "windows:noto-sans-sc"),
            (Path(r"C:\Windows\Fonts\msyh.ttc"), "windows:microsoft-yahei"),
            (Path(r"C:\Windows\Fonts\simhei.ttf"), "windows:simhei"),
            (Path(r"C:\Windows\Fonts\Deng.ttf"), "windows:dengxian"),
            (Path(r"C:\Windows\Fonts\simsun.ttc"), "windows:simsun"),
        ],
    )
    return candidates


def _draw_wrapped_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    x: int,
    y: int,
    max_width: int,
    font: ImageFont.FreeTypeFont,
    fill: str,
    tracker: _TextBoxTracker,
    label: str,
    *,
    max_lines: int,
    line_gap: int = 6,
) -> int:
    lines = _wrap_text(text, font, max_width, max_lines)
    line_height = _line_height(font)
    current_y = y
    for index, line in enumerate(lines):
        _draw_text(draw, line, (x, current_y), font, fill, tracker, f"{label}-{index}")
        current_y += line_height + line_gap
    return current_y - line_gap if lines else y


def _draw_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    xy: tuple[int, int],
    font: ImageFont.FreeTypeFont,
    fill: str,
    tracker: _TextBoxTracker,
    label: str,
) -> None:
    clean = _text(text)
    if not clean:
        return
    draw.text(xy, clean, font=font, fill=fill)
    box = draw.textbbox(xy, clean, font=font)
    tracker.add((int(box[0]), int(box[1]), int(box[2]), int(box[3])), label)


def _draw_chips(
    draw: ImageDraw.ImageDraw,
    chips: list[str] | tuple[str, ...],
    x: int,
    y: int,
    max_width: int,
    font: ImageFont.FreeTypeFont,
    tracker: _TextBoxTracker,
    *,
    max_rows: int,
    fill: str,
    text_fill: str,
    stroke: str,
) -> int:
    chip_height = 48
    gap_x = 12
    gap_y = 12
    current_x = x
    current_y = y
    row = 1
    for index, raw in enumerate(chips):
        text = _fit_text_to_width(_text(raw), font, min(300, max_width - 36))
        if not text:
            continue
        text_width = _text_width(text, font)
        chip_width = min(max_width, text_width + 34)
        if current_x > x and current_x + chip_width > x + max_width:
            row += 1
            if row > max_rows:
                break
            current_x = x
            current_y += chip_height + gap_y
        rect = (current_x, current_y, current_x + chip_width, current_y + chip_height)
        draw.rounded_rectangle(rect, radius=8, fill=fill, outline=stroke, width=1)
        text_y = current_y + (chip_height - _line_height(font)) // 2 - 1
        _draw_text(
            draw,
            text,
            (current_x + 17, text_y),
            font,
            text_fill,
            tracker,
            f"chip-{index}-{text}",
        )
        current_x += chip_width + gap_x
    return current_y + chip_height


def _chips_height(
    chips: list[str] | tuple[str, ...],
    font: ImageFont.FreeTypeFont,
    max_width: int,
    *,
    max_rows: int,
) -> int:
    chip_height = 48
    gap_x = 12
    gap_y = 12
    current_x = 0
    rows = 1
    has_chip = False
    for raw in chips:
        text = _fit_text_to_width(_text(raw), font, min(300, max_width - 36))
        if not text:
            continue
        has_chip = True
        chip_width = min(max_width, _text_width(text, font) + 34)
        if current_x > 0 and current_x + chip_width > max_width:
            rows += 1
            if rows > max_rows:
                rows = max_rows
                break
            current_x = 0
        current_x += chip_width + gap_x
    if not has_chip:
        return 0
    return rows * chip_height + (rows - 1) * gap_y


def _wrap_text(
    text: str,
    font: ImageFont.FreeTypeFont,
    max_width: int,
    max_lines: int,
) -> list[str]:
    clean = _text(text)
    if not clean or max_lines <= 0:
        return []
    lines: list[str] = []
    line = ""
    for char in clean:
        candidate = line + char
        if line and _text_width(candidate, font) > max_width:
            lines.append(line)
            line = char.lstrip()
            if len(lines) == max_lines:
                break
        else:
            line = candidate
    if len(lines) < max_lines and line:
        lines.append(line)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
    consumed = "".join(lines)
    if len(consumed) < len(clean) and lines:
        lines[-1] = _fit_text_to_width(lines[-1] + "...", font, max_width)
    return lines


def _wrapped_height(
    text: str,
    font: ImageFont.FreeTypeFont,
    max_width: int,
    *,
    max_lines: int,
) -> int:
    lines = _wrap_text(text, font, max_width, max_lines)
    if not lines:
        return 0
    return len(lines) * _line_height(font) + max(0, len(lines) - 1) * 6


def _fit_text_to_width(text: str, font: ImageFont.FreeTypeFont, max_width: int) -> str:
    clean = _text(text)
    if _text_width(clean, font) <= max_width:
        return clean
    suffix = "..."
    while clean and _text_width(clean + suffix, font) > max_width:
        clean = clean[:-1]
    return clean + suffix if clean else ""


def _text_width(text: str, font: ImageFont.FreeTypeFont) -> int:
    if not text:
        return 0
    box = font.getbbox(text)
    return int(box[2] - box[0])


def _line_height(font: ImageFont.FreeTypeFont) -> int:
    return int(getattr(font, "size", 28) * 1.28)


def _rounded_rect(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    fill: str,
    alpha: int,
) -> None:
    rgba = _hex_to_rgba(fill, alpha)
    draw.rounded_rectangle(box, radius=8, fill=rgba)


def _draw_small_dot(draw: ImageDraw.ImageDraw, x: int, y: int, fill: str) -> None:
    draw.ellipse((x, y, x + 10, y + 10), fill=_hex_to_rgba(fill, 255))


def _draw_capsule_icon(draw: ImageDraw.ImageDraw, x: int, y: int, fill: str) -> None:
    draw.rounded_rectangle((x, y, x + 24, y + 24), radius=8, outline=_hex_to_rgba(fill, 220), width=2)
    draw.line((x + 7, y + 12, x + 17, y + 12), fill=_hex_to_rgba(fill, 220), width=2)
    draw.line((x + 12, y + 7, x + 12, y + 17), fill=_hex_to_rgba(fill, 220), width=2)


def _hex_to_rgba(value: str, alpha: int) -> tuple[int, int, int, int]:
    raw = value.lstrip("#")
    return (int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16), alpha)


def _compression_mode(days: int) -> str:
    if days <= 3:
        return "rich"
    if days <= 5:
        return "compressed"
    return "compact"


def _pace_summary(plan: dict[str, Any]) -> str:
    pace = _dict(plan.get("pace"))
    parts: list[str] = []
    level = _user_facing_text(pace.get("level"))
    if level:
        parts.append(level)
    commute_status = _user_facing_text(pace.get("commute_status"))
    if commute_status:
        parts.append(commute_status)
    total_minutes = _to_int(pace.get("total_commute_minutes"))
    if total_minutes > 0:
        parts.append(f"总通勤约{total_minutes}分钟")
    return " · ".join(parts)


def _weather_lines(result: dict[str, Any]) -> list[str]:
    weather = _dict(result.get("weather"))
    if not weather or _text(weather.get("status")).lower() not in {"", "ok"}:
        return []
    lines: list[str] = []
    for item in _list(weather.get("days")):
        parts: list[str] = []
        day_no = _to_int(item.get("day"))
        if day_no > 0:
            parts.append(f"第{day_no}天")
        date = _user_facing_text(item.get("date"))
        if date:
            parts.append(date)
        weather_text = _user_facing_text(item.get("weather_text"))
        if weather_text:
            parts.append(weather_text)
        temp_min = item.get("temp_min_c")
        temp_max = item.get("temp_max_c")
        if isinstance(temp_min, (int, float)) and isinstance(temp_max, (int, float)):
            parts.append(f"{temp_min:g}-{temp_max:g}℃")
        wind = _user_facing_text(item.get("wind_text"))
        if wind:
            parts.append(wind)
        reminders = _public_items(item.get("reminders"), limit=10)
        parts.extend(reminders)
        if len(parts) > (1 if day_no > 0 else 0):
            lines.append("｜".join(parts))
    return lines


def _time_preference_lines(result: dict[str, Any]) -> list[str]:
    preferences = _dict(result.get("time_preferences"))
    if not preferences:
        return []
    lines: list[str] = []
    daily_start = _user_facing_text(preferences.get("daily_start"))
    daily_end = _user_facing_text(preferences.get("daily_end"))
    if daily_start or daily_end:
        value = "每日"
        if daily_start and daily_end:
            value += f" {daily_start}-{daily_end}"
        elif daily_start:
            value += f" {daily_start}后开始"
        else:
            value += f" {daily_end}前结束"
        lines.append(value)
    for window in _list(preferences.get("rest_windows")):
        start = _user_facing_text(window.get("start"))
        end = _user_facing_text(window.get("end"))
        if not start and not end:
            continue
        days = _user_facing_text(window.get("days"))
        if days.lower() == "all":
            days = "每天"
        value = f"{days + ' ' if days else ''}休息"
        if start and end:
            value += f" {start}-{end}"
        elif start:
            value += f" {start}后"
        else:
            value += f" {end}前"
        lines.append(value)
    return lines


def _route_style_label(plan: dict[str, Any]) -> str:
    tags = _public_items(plan.get("tags"), limit=3)
    if tags:
        return " · ".join(tags)
    pace = _dict(plan.get("pace"))
    level = _text(pace.get("level"))
    if level == "RELAXED":
        return "轻松节奏"
    if level == "COMPACT":
        return "紧凑节奏"
    if level:
        return _user_facing_text(level, "城市灵感路线")
    return "城市灵感路线"


def _suitable_for(
    people_count: int,
    preferences: tuple[str, ...],
    avoid: tuple[str, ...],
    route_style: str,
) -> str:
    parts: list[str] = []
    if people_count > 0:
        parts.append(f"适合{people_count}人同行")
    if route_style:
        parts.append(route_style)
    if preferences:
        parts.append("偏好 " + "、".join(preferences[:3]))
    if avoid:
        parts.append("尽量避开 " + "、".join(avoid[:2]))
    return " · ".join(parts) if parts else "适合轻松浏览和社交分享的结果摘要"


def _theme_text(day: dict[str, Any], place_names: list[str]) -> str:
    title = _user_facing_text(day.get("title"))
    if place_names:
        return _truncate(f"{title} · {' · '.join(place_names[:2])}", 34)
    return _truncate(title, 34)


def _sentences(text: str) -> list[str]:
    clean = _text(text)
    if not clean:
        return []
    parts = re.split(r"[。！？!?；;]\s*", clean)
    return [_text(part) for part in parts if _text(part)]


def _city_name(result: dict[str, Any]) -> str:
    city = _dict(result.get("city"))
    return _user_facing_text(city.get("name"), "未知城市")


def _string_items(value: Any, *, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    items: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = _text(item)
        if text and text not in seen:
            items.append(text)
            seen.add(text)
        if len(items) >= limit:
            break
    return items


def _public_items(value: Any, *, limit: int) -> list[str]:
    return public_items(value, limit=limit)


def _user_facing_text(value: Any, default: str = "") -> str:
    return public_text(value, default)


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (int, float)):
        return str(value)
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text if text else default


def _truncate(text: str, length: int) -> str:
    clean = _text(text)
    if len(clean) <= length:
        return clean
    return clean[: max(0, length - 3)].rstrip() + "..."


def _to_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _normalize_time(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _boxes_overlap(
    first: tuple[int, int, int, int],
    second: tuple[int, int, int, int],
) -> bool:
    return not (
        first[2] <= second[0]
        or second[2] <= first[0]
        or first[3] <= second[1]
        or second[3] <= first[1]
    )


def _safe_storage_key(value: str | None) -> str | None:
    if not value:
        return None
    if re.match(r"^[A-Za-z]:[\\/]", value):
        return None
    if value.startswith("/") or value.startswith("\\\\"):
        return None
    return value


def _unlink_silent(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except Exception:
        pass
