"""ReportLab text PDF renderer for v0.8.10.1 export artifacts."""

from __future__ import annotations

import hashlib
import os
import re
import uuid
from io import BytesIO
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    HRFlowable,
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from src.export.city_background import CityBackgroundClient, fallback_city_background_with_metadata
from src.export.city_photo import CityPhotoEntry, CityPhotoResolver
from src.export.cost_estimate import (
    CATEGORY_LABELS,
    range_text,
    validate_artifact_cost_estimate,
)
from src.export.public_text import public_items, public_text
from src.export.source_contract import validate_export_result_contract

FONT_NAME = "YunTuTravelCJK"
FONT_ENV_KEYS = (
    "YUNTU_TRAVEL_PDF_FONT_PATH",
    "EXPORT_PDF_FONT_PATH",
    "PDF_FONT_PATH",
)
PDF_MIME_TYPE = "application/pdf"
PDF_HARD_MAX_BYTES = 5 * 1024 * 1024
COVER_BACKGROUND_MAX_SIZE = (1200, 1697)
COVER_BACKGROUND_JPEG_QUALITY = 82
TEXT_PRIMARY = colors.HexColor("#172033")
TEXT_MUTED = colors.HexColor("#596274")
TEXT_ACCENT = colors.HexColor("#087F73")
TEXT_COST = colors.HexColor("#B65F2A")
TEXT_SUBTLE = colors.HexColor("#737B89")
SECTION_RULE_COLOR = colors.HexColor("#C9D8D4")
PAPER = colors.HexColor("#FBF8F1")
COVER_TEXT = colors.HexColor("#FFFDF8")
COVER_MUTED = colors.HexColor("#E8E3D9")
COVER_SCRIM_RGBA = (10, 20, 31, 42)


class PdfRenderError(RuntimeError):
    """Raised when an export source cannot be rendered as a text PDF."""

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


@dataclass(frozen=True)
class _FontInfo:
    name: str
    source: str


_REGISTERED_FONT: _FontInfo | None = None


def render_pdf_artifact(
    export_source: Any,
    output_path: Path,
    *,
    cover_background_path: Path | None = None,
    ai_background_client: CityBackgroundClient | None = None,
    city_photo_resolver: CityPhotoResolver | None = None,
    storage_key: str | None = None,
    generated_time: datetime | None = None,
) -> PdfRenderResult:
    """Render a complete itinerary text PDF from a P1 export source.

    The renderer is intentionally pure: it only consumes the supplied export
    source, does not call Writer/Review/route planning/frontend code, and writes
    the PDF through a same-directory temporary file before atomically replacing
    the final path.
    """

    payload = _source_payload(export_source)
    result = _validated_result(payload)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    font = _register_chinese_font()
    generated_time = _normalize_time(generated_time)

    background_state = _page_background_state(
        result,
        city_photo_resolver=city_photo_resolver,
        background_path=cover_background_path,
    )
    text_parts: list[str] = []
    story = _build_story(result, generated_time, font.name, text_parts, background_state)

    temp_path = output_path.with_name(
        f".{output_path.name}.{uuid.uuid4().hex}.tmp",
    )
    doc = SimpleDocTemplate(
        str(temp_path),
        pagesize=A4,
        leftMargin=1.55 * cm,
        rightMargin=1.55 * cm,
        topMargin=1.45 * cm,
        bottomMargin=1.35 * cm,
        title=_document_title(result),
        author="yuntu-travel",
        subject="Trip export artifact",
        creator="yuntu-travel ReportLab renderer",
    )
    try:
        doc.build(
            story,
            onFirstPage=lambda canvas, _doc: _draw_cover_page(
                canvas,
                background_state,
                font.name,
            ),
            onLaterPages=lambda canvas, _doc: _draw_body_page(
                canvas,
                background_state,
                result,
                font.name,
            ),
        )
        render_result = _validated_render_result(
            temp_path,
            text_parts=text_parts,
            payload=payload,
            font=font,
            background_state=background_state,
            storage_key=storage_key,
        )
        os.replace(temp_path, output_path)
    except PdfRenderError:
        _unlink_silent(temp_path)
        raise
    except Exception as exc:  # pragma: no cover - exercised through callers.
        _unlink_silent(temp_path)
        raise PdfRenderError("PDF_RENDER_FAILED") from exc

    return PdfRenderResult(
        output_path=output_path,
        page_count=render_result.page_count,
        byte_size=render_result.byte_size,
        sha256=render_result.sha256,
        text_length=render_result.text_length,
        storage_key=storage_key,
        metadata=render_result.metadata,
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
    raise PdfRenderError("INVALID_EXPORT_SOURCE")


def _validated_result(payload: dict[str, Any]) -> dict[str, Any]:
    result = payload.get("result")
    if not isinstance(result, dict):
        raise PdfRenderError("INVALID_EXPORT_SOURCE")
    try:
        validate_export_result_contract(result)
    except (TypeError, ValueError) as exc:
        raise PdfRenderError("INVALID_EXPORT_SOURCE") from exc

    request = result.get("request")
    city = result.get("city")
    plans = result.get("plans")
    if not isinstance(request, dict) or not isinstance(city, dict):
        raise PdfRenderError("INVALID_EXPORT_SOURCE")
    if not public_text(city.get("name")):
        raise PdfRenderError("INVALID_EXPORT_SOURCE")
    if not isinstance(plans, list) or not plans:
        raise PdfRenderError("INVALID_EXPORT_SOURCE")
    if _to_int(request.get("days")) <= 0:
        raise PdfRenderError("INVALID_EXPORT_SOURCE")

    for plan in plans:
        if not isinstance(plan, dict):
            raise PdfRenderError("INVALID_EXPORT_SOURCE")
        if not public_text(plan.get("title")) or not public_text(plan.get("summary")):
            raise PdfRenderError("INVALID_EXPORT_SOURCE")
        try:
            validate_artifact_cost_estimate(plan.get("cost_estimate"))
        except (TypeError, ValueError) as exc:
            raise PdfRenderError("INVALID_EXPORT_SOURCE") from exc
        days = plan.get("days")
        if not isinstance(days, list) or not days:
            raise PdfRenderError("INVALID_EXPORT_SOURCE")
        for day in days:
            _validate_day(day)
    return result


def _validate_day(day: Any) -> None:
    if not isinstance(day, dict):
        raise PdfRenderError("INVALID_EXPORT_SOURCE")
    if _to_int(day.get("day")) <= 0 or not public_text(day.get("title")):
        raise PdfRenderError("INVALID_EXPORT_SOURCE")
    if not public_text(day.get("narrative")):
        raise PdfRenderError("INVALID_EXPORT_SOURCE")

    places = day.get("places")
    if not isinstance(places, list) or not places:
        raise PdfRenderError("INVALID_EXPORT_SOURCE")
    place_ids: set[int] = set()
    for place in places:
        if not isinstance(place, dict):
            raise PdfRenderError("INVALID_EXPORT_SOURCE")
        place_id = _to_int(place.get("place_id"))
        if place_id <= 0 or not public_text(place.get("name")):
            raise PdfRenderError("INVALID_EXPORT_SOURCE")
        place_ids.add(place_id)

    if not public_text(day.get("commute_summary")):
        raise PdfRenderError("INVALID_EXPORT_SOURCE")

    commute_legs = day.get("commute_legs")
    if not isinstance(commute_legs, list):
        raise PdfRenderError("INVALID_EXPORT_SOURCE")
    if len(places) <= 1:
        return
    if not commute_legs:
        raise PdfRenderError("INVALID_EXPORT_SOURCE")
    for leg in commute_legs:
        _validate_commute_leg(leg, place_ids)


def _validate_commute_leg(leg: Any, place_ids: set[int]) -> None:
    if not isinstance(leg, dict):
        raise PdfRenderError("INVALID_EXPORT_SOURCE")
    from_id = _to_int(leg.get("from_place_id"))
    to_id = _to_int(leg.get("to_place_id"))
    if from_id not in place_ids or to_id not in place_ids or from_id == to_id:
        raise PdfRenderError("INVALID_EXPORT_SOURCE")
    if not _text(leg.get("mode")):
        raise PdfRenderError("INVALID_EXPORT_SOURCE")
    if _to_int(leg.get("duration_minutes"), default=-1) < 0:
        raise PdfRenderError("INVALID_EXPORT_SOURCE")
    if _to_int(leg.get("distance_meters"), default=-1) < 0:
        raise PdfRenderError("INVALID_EXPORT_SOURCE")


def _build_story(
    result: dict[str, Any],
    generated_time: datetime,
    font_name: str,
    text_parts: list[str],
    background_state: dict[str, Any],
) -> list[Any]:
    styles = _styles(font_name)
    story: list[Any] = []
    _add_cover(story, result, generated_time, styles, text_parts)
    story.append(PageBreak())
    _add_body(story, result, styles, text_parts)
    _add_photo_attribution(story, background_state.get("entry"), styles, text_parts)
    return story


def _add_cover(
    story: list[Any],
    result: dict[str, Any],
    generated_time: datetime,
    styles: dict[str, ParagraphStyle],
    text_parts: list[str],
) -> None:
    request = _dict(result.get("request"))
    city_name = _city_name(result)
    title = f"{city_name}{_to_int(request.get('days'))}天完整行程"
    story.append(Spacer(1, 4.15 * cm))
    _para(story, styles["cover_title"], title, text_parts)
    _para(story, styles["cover_subtitle"], "云途 · 浅色路线册", text_parts)
    story.append(Spacer(1, 0.7 * cm))

    date_range = _date_range_text(request)
    if date_range:
        _para(story, styles["cover_body"], date_range, text_parts)
    _para(
        story,
        styles["cover_body"],
        f"{_people_text(request.get('people_count'))} · {_list_text(request.get('preferences'), '自在探索')}",
        text_parts,
    )

    summary = _result_summary(result)
    if summary:
        story.append(Spacer(1, 0.35 * cm))
        _para(story, styles["cover_body"], summary, text_parts)


def _add_body(
    story: list[Any],
    result: dict[str, Any],
    styles: dict[str, ParagraphStyle],
    text_parts: list[str],
) -> None:
    _para(story, styles["h1"], "完整行程", text_parts)
    request = _dict(result.get("request"))
    _section_label(story, styles, "行程概览", text_parts)
    _kv(story, styles, "目的地", _city_name(result), text_parts)
    _kv(story, styles, "行程天数", f"{_to_int(request.get('days'))} 天", text_parts)
    _kv(story, styles, "人数", _people_text(request.get("people_count")), text_parts)
    date_range = _date_range_text(request)
    if date_range:
        _kv(story, styles, "日期", date_range, text_parts)
    _kv(story, styles, "偏好", _list_text(request.get("preferences"), "未填写"), text_parts)
    _kv(story, styles, "避开", _list_text(request.get("avoid"), "未填写"), text_parts)
    _kv(story, styles, "备注", _text(request.get("notes"), "未填写"), text_parts)
    _kv(story, styles, "必去", _must_include_text(result.get("must_include")), text_parts)
    _kv(story, styles, "时间偏好", _time_preferences_text(result.get("time_preferences")), text_parts)
    story.append(Spacer(1, 0.35 * cm))
    weather_by_day = _weather_by_day(result.get("weather"))
    for plan_index, plan in enumerate(_list(result.get("plans")), start=1):
        if plan_index > 1:
            story.append(PageBreak())
        _add_plan(story, plan, plan_index, weather_by_day, styles, text_parts)


def _add_plan(
    story: list[Any],
    plan: dict[str, Any],
    plan_index: int,
    weather_by_day: dict[int, dict[str, Any]],
    styles: dict[str, ParagraphStyle],
    text_parts: list[str],
) -> None:
    _para(story, styles["h2"], f"方案 {plan_index}：{_text(plan.get('title'))}", text_parts)
    if _text(plan.get("summary")):
        _para(story, styles["body"], _text(plan.get("summary")), text_parts)
    _kv(story, styles, "标签", _list_text(plan.get("tags"), "无"), text_parts)
    _kv(story, styles, "节奏", _pace_text(plan.get("pace")), text_parts)
    story.append(Spacer(1, 0.18 * cm))

    for day in _list(plan.get("days")):
        _add_day(story, day, weather_by_day, styles, text_parts)
    _add_cost_estimate(story, plan.get("cost_estimate"), styles, text_parts)
    _add_advice(story, plan, styles, text_parts)


def _add_advice(
    story: list[Any],
    plan: dict[str, Any],
    styles: dict[str, ParagraphStyle],
    text_parts: list[str],
) -> None:
    packing = _list(plan.get("packing_checklist"))
    packing_lines = []
    for group in packing:
        category = public_text(group.get("category"))
        items = [public_text(item) for item in group.get("items") or []]
        items = [item for item in items if item]
        if category and items:
            packing_lines.append(f"— {category}：{'、'.join(items)}")
    if packing_lines:
        _add_bullet_section(story, "出行清单", packing_lines, styles, text_parts)

    tips = _list(plan.get("travel_tips"))
    tip_lines = []
    for tip in tips:
        title = public_text(tip.get("title"))
        content = public_text(tip.get("content"))
        if title and content:
            tip_lines.append(f"— {title}：{content}")
    if tip_lines:
        _add_bullet_section(story, "旅行贴士", tip_lines, styles, text_parts)


def _add_bullet_section(
    story: list[Any],
    label: str,
    lines: list[str],
    styles: dict[str, ParagraphStyle],
    text_parts: list[str],
) -> None:
    story.append(Spacer(1, 0.16 * cm))
    text_parts.append(label)
    text_parts.extend(lines)
    label_para = Paragraph(_markup(label), styles["label"])
    bullet_paras = [Paragraph(_markup(line), styles["bullet"]) for line in lines]
    story.append(KeepTogether([label_para, bullet_paras[0]]))
    story.extend(bullet_paras[1:])


def _add_photo_attribution(
    story: list[Any],
    entry: CityPhotoEntry | None,
    styles: dict[str, ParagraphStyle],
    text_parts: list[str],
) -> None:
    if entry is None:
        return
    plain = (
        f"封面摄影：{entry.author}；来源：{entry.provider}；许可：{entry.license}。"
        f"{entry.modification_notice} 原图：{entry.source_page_url}"
    )
    if entry.license_url:
        plain += f"；许可文本：{entry.license_url}"
    text_parts.append(plain)
    source_link = f'<link href="{escape(entry.source_page_url)}" color="#087F73">查看原图</link>'
    license_link = ""
    if entry.license_url:
        license_link = f' · <link href="{escape(entry.license_url)}" color="#087F73">查看许可</link>'
    markup = (
        f'<font color="#087F73">封面摄影</font> · {escape(entry.author)} · '
        f'{escape(entry.provider)} · {escape(entry.license)} · {source_link}{license_link} · '
        f"{escape(entry.modification_notice)}"
    )
    story.append(Paragraph(markup, styles["credit"]))


def _add_cost_estimate(
    story: list[Any],
    raw: Any,
    styles: dict[str, ParagraphStyle],
    text_parts: list[str],
) -> None:
    summary = validate_artifact_cost_estimate(raw)
    story.append(Spacer(1, 0.24 * cm))
    _section_label(story, styles, "行程消费预估", text_parts)

    local_time = summary.estimated_at.astimezone(ZoneInfo("Asia/Shanghai"))
    status_text = f"费用参考 · 更新于 {local_time:%Y-%m-%d %H:%M}"
    text_parts.append(status_text)
    status = Table(
        [[Paragraph(_markup(status_text), styles["cost_meta"])]],
        colWidths=[17.85 * cm],
        hAlign="LEFT",
    )
    status.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#EDF5F2")),
        ("BOX", (0, 0), (-1, -1), 0.35, colors.HexColor("#D7E7E2")),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(status)
    story.append(Spacer(1, 0.12 * cm))

    shared_categories = _shared_cost_categories(summary.scenarios)
    shared_names = {item.category for item in shared_categories}
    scenario_rows: list[list[Paragraph]] = []
    for scenario in summary.scenarios:
        total_text = _scenario_total_text(scenario)
        detail_text = _scenario_cost_detail(scenario, shared_names)
        row_text = f"{scenario.label}：{total_text}；{detail_text}"
        text_parts.append(row_text)
        scenario_rows.append([
            Paragraph(_markup(scenario.label), styles["cost_scenario"]),
            Paragraph(_markup(total_text), styles["cost_amount"]),
            Paragraph(_markup(detail_text), styles["cost_detail"]),
        ])
    scenario_table = Table(
        scenario_rows,
        colWidths=[3.65 * cm, 4.35 * cm, 9.85 * cm],
        hAlign="LEFT",
    )
    scenario_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ROWBACKGROUNDS", (0, 0), (-1, -1), [colors.HexColor("#FBFAF6"), colors.HexColor("#F7F4EC")]),
        ("LINEBELOW", (0, 0), (-1, -2), 0.3, colors.HexColor("#E2DDD2")),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(scenario_table)

    if shared_categories:
        story.append(Spacer(1, 0.16 * cm))
        shared_label = "共同费用" if len(summary.scenarios) > 1 else "费用明细"
        text_parts.append(shared_label)
        story.append(Paragraph(_markup(shared_label), styles["cost_group"]))
        detail_rows: list[list[Paragraph]] = []
        for category in shared_categories:
            label = CATEGORY_LABELS[category.category]
            value = _cost_category_value(category)
            note = category.basis_label
            text_parts.append(f"{label}：{value}（{note}）")
            detail_rows.append([
                Paragraph(_markup(label), styles["cost_category"]),
                Paragraph(_markup(value), styles["cost_value"]),
                Paragraph(_markup(note), styles["cost_detail"]),
            ])
        detail_table = Table(
            detail_rows,
            colWidths=[3.65 * cm, 4.35 * cm, 9.85 * cm],
            hAlign="LEFT",
        )
        detail_table.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LINEBELOW", (0, 0), (-1, -1), 0.3, colors.HexColor("#E2DDD2")),
            ("LEFTPADDING", (0, 0), (-1, -1), 8),
            ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))
        story.append(detail_table)

    if summary.exclusions:
        exclusions = "；".join(item.label for item in summary.exclusions)
        text_parts.append(f"未计范围：{exclusions}")
        story.append(Paragraph(
            f'<font color="#087F73">未计范围</font> · {_markup(exclusions)}',
            styles["cost_note"],
        ))
    text_parts.append(summary.notice)
    story.append(Paragraph(_markup(summary.notice), styles["cost_notice"]))


def _shared_cost_categories(scenarios: Any) -> list[Any]:
    scenarios = list(scenarios)
    if not scenarios:
        return []
    shared: list[Any] = []
    for index, category in enumerate(scenarios[0].categories):
        if len(scenarios) > 1 and category.category == "intercity_transport":
            continue
        if all(other.categories[index] == category for other in scenarios[1:]):
            shared.append(category)
    return shared


def _scenario_total_text(scenario: Any) -> str:
    if scenario.total_range is None:
        return "费用暂不可估算"
    prefix = "已估" if scenario.total_scope == "estimated_subset" else "预估"
    return f"{prefix} {_pdf_range_text(scenario.total_range)}"


def _scenario_cost_detail(scenario: Any, shared_names: set[str]) -> str:
    details: list[str] = []
    for category in scenario.categories:
        if category.category in shared_names:
            continue
        if scenario.scenario_id == "without_intercity" and category.category == "intercity_transport":
            details.append("不含往返大交通")
        else:
            details.append(
                f"{CATEGORY_LABELS[category.category]} {_cost_category_value(category)}"
                f"（{category.basis_label}）"
            )
    missing_count = len(scenario.missing_categories)
    if scenario.scenario_id == "without_intercity" and "intercity_transport" in scenario.missing_categories:
        missing_count -= 1
    if missing_count > 0:
        details.append(f"另有 {missing_count} 项待确认")
    return " · ".join(details) or "费用项已完整估算"


def _cost_category_value(category: Any) -> str:
    if category.range is None:
        return "待确认"
    return _pdf_range_text(category.range)


def _pdf_range_text(value: Any) -> str:
    return range_text(value).replace("¥", "￥")


def _add_day(
    story: list[Any],
    day: dict[str, Any],
    weather_by_day: dict[int, dict[str, Any]],
    styles: dict[str, ParagraphStyle],
    text_parts: list[str],
) -> None:
    day_no = _to_int(day.get("day"))
    title = public_text(f"Day {day_no}｜{_text(day.get('title'))}")
    text_parts.append(title)
    story.append(KeepTogether([
        HRFlowable(width="100%", thickness=0.6, color=SECTION_RULE_COLOR),
        Spacer(1, 0.2 * cm),
        Paragraph(_markup(title), styles["h3"]),
    ]))
    _kv(story, styles, "天气", _weather_text(weather_by_day.get(day_no)), text_parts)
    _kv(story, styles, "当日节奏", _pace_status_text(day.get("pace_status")), text_parts)

    places = _list(day.get("places"))
    _section_label(story, styles, "地点", text_parts)
    place_names = _place_names(places)
    for index, place in enumerate(places, start=1):
        _para(
            story,
            styles["bullet"],
            _place_text(index, place),
            text_parts,
        )

    commute_summary = _text(day.get("commute_summary"))
    if commute_summary:
        _kv(story, styles, "交通概览", commute_summary, text_parts)

    legs = _list(day.get("commute_legs"))
    if legs:
        _section_label(story, styles, "交通明细", text_parts)
        for index, leg in enumerate(legs, start=1):
            _para(
                story,
                styles["bullet"],
                _commute_leg_text(index, leg, place_names),
                text_parts,
            )

    _section_label(story, styles, "当日叙述", text_parts)
    _para(story, styles["body"], _text(day.get("narrative")), text_parts)
    story.append(Spacer(1, 0.28 * cm))


def _styles(font_name: str) -> dict[str, ParagraphStyle]:
    base = {
        "fontName": font_name,
        "wordWrap": "CJK",
        "leading": 15.5,
        "textColor": TEXT_PRIMARY,
        "spaceAfter": 6,
    }

    def style(name: str, **overrides: Any) -> ParagraphStyle:
        params = dict(base)
        params.update(overrides)
        return ParagraphStyle(name, **params)

    return {
        "cover_title": style(
            "YuntuCoverTitle",
            fontSize=27,
            leading=33,
            alignment=TA_CENTER,
            textColor=COVER_TEXT,
            spaceAfter=12,
        ),
        "cover_subtitle": style(
            "YuntuCoverSubtitle",
            fontSize=10,
            leading=14,
            alignment=TA_CENTER,
            textColor=COVER_MUTED,
            spaceAfter=18,
        ),
        "cover_body": style(
            "YuntuCoverBody",
            fontSize=10.5,
            leading=17,
            alignment=TA_CENTER,
            textColor=COVER_TEXT,
            leftIndent=0.45 * cm,
            rightIndent=0.45 * cm,
        ),
        "h1": style(
            "YuntuH1",
            fontSize=19,
            leading=24,
            textColor=TEXT_PRIMARY,
            spaceAfter=12,
        ),
        "h2": style(
            "YuntuH2",
            fontSize=15,
            leading=20,
            textColor=TEXT_PRIMARY,
            spaceBefore=8,
            spaceAfter=8,
        ),
        "h3": style(
            "YuntuH3",
            fontSize=12,
            leading=17,
            textColor=TEXT_PRIMARY,
            spaceAfter=6,
        ),
        "label": style(
            "YuntuLabel",
            fontSize=9,
            leading=13,
            textColor=TEXT_ACCENT,
            spaceBefore=3,
            spaceAfter=2,
        ),
        "body": style(
            "YuntuBody",
            fontSize=9.5,
            leading=15,
        ),
        "body_large": style(
            "YuntuBodyLarge",
            fontSize=10.5,
            leading=16,
            textColor=TEXT_PRIMARY,
        ),
        "bullet": style(
            "YuntuBullet",
            fontSize=9,
            leading=14,
            textColor=TEXT_PRIMARY,
            leftIndent=0.3 * cm,
            firstLineIndent=-0.15 * cm,
            spaceAfter=3,
        ),
        "credit": style(
            "YuntuCredit",
            fontSize=6.8,
            leading=9.5,
            textColor=TEXT_SUBTLE,
            spaceBefore=4,
            spaceAfter=0,
        ),
        "cost_meta": style(
            "YuntuCostMeta",
            fontSize=8.5,
            leading=12,
            textColor=TEXT_ACCENT,
            spaceAfter=0,
        ),
        "cost_scenario": style(
            "YuntuCostScenario",
            fontSize=9,
            leading=13,
            textColor=TEXT_PRIMARY,
            spaceAfter=0,
        ),
        "cost_amount": style(
            "YuntuCostAmount",
            fontSize=10.5,
            leading=14,
            textColor=TEXT_COST,
            spaceAfter=0,
        ),
        "cost_group": style(
            "YuntuCostGroup",
            fontSize=8.2,
            leading=11,
            textColor=TEXT_MUTED,
            leftIndent=0.08 * cm,
            spaceBefore=1,
            spaceAfter=2,
        ),
        "cost_category": style(
            "YuntuCostCategory",
            fontSize=8.5,
            leading=12,
            textColor=TEXT_PRIMARY,
            spaceAfter=0,
        ),
        "cost_value": style(
            "YuntuCostValue",
            fontSize=8.5,
            leading=12,
            textColor=TEXT_PRIMARY,
            spaceAfter=0,
        ),
        "cost_detail": style(
            "YuntuCostDetail",
            fontSize=7.8,
            leading=11,
            textColor=TEXT_MUTED,
            spaceAfter=0,
        ),
        "cost_note": style(
            "YuntuCostNote",
            fontSize=7.8,
            leading=12,
            textColor=TEXT_MUTED,
            leftIndent=0.08 * cm,
            spaceAfter=2,
        ),
        "cost_notice": style(
            "YuntuCostNotice",
            fontSize=7.5,
            leading=11,
            textColor=TEXT_SUBTLE,
            leftIndent=0.08 * cm,
            spaceBefore=2,
            spaceAfter=3,
        ),
    }


def _para(
    story: list[Any],
    style: ParagraphStyle,
    text: str,
    text_parts: list[str],
) -> None:
    clean = public_text(text)
    if not clean:
        return
    text_parts.append(clean)
    story.append(Paragraph(_markup(clean), style))


def _kv(
    story: list[Any],
    styles: dict[str, ParagraphStyle],
    label: str,
    value: str,
    text_parts: list[str],
) -> None:
    clean_value = public_text(value)
    if not clean_value:
        return
    plain = f"{label}：{clean_value}"
    text_parts.append(plain)
    markup = (
        f'<font color="#087F73">{escape(label)}：</font>'
        f'{_markup(clean_value)}'
    )
    story.append(Paragraph(markup, styles["body"]))


def _section_label(
    story: list[Any],
    styles: dict[str, ParagraphStyle],
    label: str,
    text_parts: list[str],
) -> None:
    text_parts.append(label)
    story.append(Paragraph(_markup(label), styles["label"]))


def _markup(text: str) -> str:
    return "<br/>".join(escape(part) for part in _text(text).splitlines())


def _register_chinese_font() -> _FontInfo:
    global _REGISTERED_FONT
    if _REGISTERED_FONT is not None:
        return _REGISTERED_FONT

    for path, source in _font_candidates():
        if path.is_file() and _try_register_font(path):
            _REGISTERED_FONT = _FontInfo(FONT_NAME, source)
            return _REGISTERED_FONT

    try:
        pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
        _REGISTERED_FONT = _FontInfo("STSong-Light", "reportlab-cid-stsong-light")
        return _REGISTERED_FONT
    except Exception as exc:
        raise PdfRenderError("PDF_FONT_UNAVAILABLE") from exc


def _font_candidates() -> list[tuple[Path, str]]:
    # Font strategy for P2:
    # - Operators may pin a font with EXPORT_PDF_FONT_PATH or compatible envs.
    # - Docker uses fonts-noto-cjk installed in Dockerfile.
    # - Windows local runs resolve common Noto/SimHei/Deng/MS YaHei/SimSun paths.
    # No large font file is vendored in this repository for v0.8.10.1 P2.
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
            (
                Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
                "linux:noto-sans-cjk",
            ),
            (
                Path("/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc"),
                "linux:noto-serif-cjk",
            ),
            (
                Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
                "linux:noto-sans-cjk",
            ),
            (
                Path(r"C:\Windows\Fonts\Noto Sans SC (TrueType).otf"),
                "windows:noto-sans-sc",
            ),
            (Path(r"C:\Windows\Fonts\simhei.ttf"), "windows:simhei"),
            (Path(r"C:\Windows\Fonts\Deng.ttf"), "windows:dengxian"),
            (Path(r"C:\Windows\Fonts\msyh.ttc"), "windows:microsoft-yahei"),
            (Path(r"C:\Windows\Fonts\simsun.ttc"), "windows:simsun"),
        ],
    )
    return candidates


def _try_register_font(path: Path) -> bool:
    suffix = path.suffix.lower()
    subfont_indexes = range(0, 6) if suffix == ".ttc" else (None,)
    for index in subfont_indexes:
        try:
            if index is None:
                font = TTFont(FONT_NAME, str(path))
            else:
                font = TTFont(FONT_NAME, str(path), subfontIndex=index)
            pdfmetrics.registerFont(font)
            return True
        except TypeError:
            try:
                pdfmetrics.registerFont(TTFont(FONT_NAME, str(path)))
                return True
            except Exception:
                return False
        except Exception:
            continue
    return False


def _page_background_state(
    result: dict[str, Any],
    *,
    city_photo_resolver: CityPhotoResolver | None,
    background_path: Path | None,
) -> dict[str, Any]:
    from PIL import Image

    metadata: dict[str, Any]
    if background_path is not None:
        try:
            with Image.open(Path(background_path)) as supplied:
                image = supplied.convert("RGB")
            metadata = {
                "background_status": "provided",
                "photo_status": "provided",
                "ai_call_attempted": False,
                "ai_call_count": 0,
            }
            entry = None
        except Exception:
            image, fallback = fallback_city_background_with_metadata(None)
            metadata = {
                **fallback,
                "background_status": "fallback",
                "photo_status": "fallback",
                "photo_error_code": "invalid_provided_image",
                "background_error_code": "invalid_provided_image",
                "ai_call_attempted": False,
                "ai_call_count": 0,
            }
            entry = None
    elif city_photo_resolver is not None:
        resolved = city_photo_resolver.resolve(_city_name(result))
        image = resolved.image
        entry = resolved.entry
        metadata = resolved.metadata
    else:
        image, fallback = fallback_city_background_with_metadata(None)
        entry = None
        metadata = {
            **fallback,
            "background_status": "fallback",
            "photo_status": "fallback",
            "photo_error_code": "photo_resolver_disabled",
            "ai_call_attempted": False,
            "ai_call_count": 0,
        }

    background = _compressed_cover_background(image)
    return {
        **metadata,
        "status": metadata["background_status"],
        "entry": entry,
        "reader": ImageReader(BytesIO(background)),
        "byte_size": len(background),
        "pages_drawn": 0,
    }


def _compressed_cover_background(image) -> bytes:
    from PIL import Image, ImageOps

    fitted = ImageOps.fit(
        image.convert("RGB"),
        COVER_BACKGROUND_MAX_SIZE,
        method=Image.Resampling.LANCZOS,
        centering=(0.5, 0.45),
    )
    composed = Image.alpha_composite(
        fitted.convert("RGBA"),
        Image.new("RGBA", fitted.size, COVER_SCRIM_RGBA),
    )
    output = BytesIO()
    composed.convert("RGB").save(
        output,
        format="JPEG",
        quality=COVER_BACKGROUND_JPEG_QUALITY,
        optimize=True,
    )
    return output.getvalue()


def _draw_cover_page(canvas, background_state: dict[str, Any], font_name: str) -> None:
    reader = background_state["reader"]
    page_width, page_height = A4
    try:
        canvas.drawImage(
            reader,
            0,
            0,
            width=page_width,
            height=page_height,
            mask="auto",
        )
        background_state["pages_drawn"] += 1
    except Exception:
        background_state["status"] = "draw_failed"
    canvas.saveState()
    canvas.setFillColor(colors.Color(0.035, 0.065, 0.10, alpha=0.76))
    canvas.roundRect(1.25 * cm, 8.2 * cm, page_width - 2.5 * cm, 14.2 * cm, 14, fill=1, stroke=0)
    entry = background_state.get("entry")
    if entry is not None:
        canvas.setFillColor(COVER_MUTED)
        canvas.setFont(font_name, 6.5)
        canvas.drawRightString(page_width - 0.8 * cm, 0.65 * cm, entry.short_credit)
    canvas.restoreState()


def _draw_body_page(
    canvas,
    background_state: dict[str, Any],
    result: dict[str, Any],
    font_name: str,
) -> None:
    canvas.saveState()
    canvas.setFillColor(PAPER)
    canvas.rect(0, 0, A4[0], A4[1], fill=1, stroke=0)
    canvas.setStrokeColor(SECTION_RULE_COLOR)
    canvas.setLineWidth(0.7)
    canvas.line(1.45 * cm, A4[1] - 1.0 * cm, A4[0] - 1.45 * cm, A4[1] - 1.0 * cm)
    canvas.restoreState()
    _draw_footer(canvas, result, font_name)


def _draw_footer(canvas, result: dict[str, Any], font_name: str) -> None:
    canvas.saveState()
    canvas.setFont(font_name, 8)
    canvas.setFillColor(TEXT_SUBTLE)
    text = f"{_city_name(result)}完整行程 · {canvas.getPageNumber()}"
    canvas.drawRightString(A4[0] - 1.45 * cm, 0.8 * cm, text)
    canvas.restoreState()


def _pdf_page_count(path: Path) -> int:
    try:
        from pypdf import PdfReader

        return len(PdfReader(str(path)).pages)
    except Exception:
        data = path.read_bytes()
        matches = re.findall(rb"/Type\s*/Page\b", data)
        return max(1, len(matches))


def _validated_render_result(
    temp_path: Path,
    *,
    text_parts: list[str],
    payload: dict[str, Any],
    font: _FontInfo,
    background_state: dict[str, Any],
    storage_key: str | None,
) -> PdfRenderResult:
    if not temp_path.is_file():
        raise PdfRenderError("PDF_RENDER_FAILED")
    data = temp_path.read_bytes()
    byte_size = len(data)
    if byte_size <= 0:
        raise PdfRenderError("PDF_RENDER_FAILED")
    if byte_size > PDF_HARD_MAX_BYTES:
        raise PdfRenderError("PDF_SIZE_EXCEEDED")

    page_count = _pdf_page_count(temp_path)
    if page_count <= 0:
        raise PdfRenderError("PDF_PAGE_COUNT_INVALID")

    text_length = _estimated_text_length(text_parts)
    if text_length <= 0:
        raise PdfRenderError("PDF_TEXT_EMPTY")

    metadata = {
        "renderer": "reportlab",
        "renderer_version": "v0.9.9.9-light-routebook-layout-3",
        "export_version": str(payload.get("export_version") or ""),
        "font_source": font.source,
        "cover_image_status": background_state["status"],
        "background_status": background_state["status"],
        "background_page_count": int(background_state["pages_drawn"]),
        "ai_call_attempted": bool(background_state.get("ai_call_attempted")),
        "ai_call_count": int(background_state.get("ai_call_count", 0)),
    }
    if background_state.get("byte_size") is not None:
        metadata["cover_image_byte_size"] = int(background_state["byte_size"])
        metadata["background_image_byte_size"] = int(background_state["byte_size"])
    if background_state.get("background_error_code"):
        metadata["background_error_code"] = background_state["background_error_code"]
    for key in (
        "photo_status",
        "photo_catalog_revision",
        "photo_object_key",
        "photo_sha256",
        "photo_cache_hit",
        "photo_failure_cache_hit",
        "photo_error_code",
        "photo_author",
        "photo_provider",
        "photo_license",
    ):
        if key in background_state:
            metadata[key] = background_state[key]

    return PdfRenderResult(
        output_path=temp_path,
        page_count=page_count,
        byte_size=byte_size,
        sha256=hashlib.sha256(data).hexdigest(),
        text_length=text_length,
        storage_key=storage_key,
        metadata=metadata,
    )


def _estimated_text_length(text_parts: list[str]) -> int:
    text = re.sub(r"\s+", "", "".join(text_parts))
    return len(text)


def _document_title(result: dict[str, Any]) -> str:
    request = _dict(result.get("request"))
    return f"{_city_name(result)}{_to_int(request.get('days'))}天完整行程"


def _result_summary(result: dict[str, Any]) -> str:
    plans = _list(result.get("plans"))
    if not plans:
        return ""
    return _text(plans[0].get("summary"))


def _city_name(result: dict[str, Any]) -> str:
    city = _dict(result.get("city"))
    return public_text(city.get("name"), "未知城市")


def _people_text(value: Any) -> str:
    count = _to_int(value)
    return f"{count} 人" if count > 0 else "未填写"


def _date_range_text(request: dict[str, Any]) -> str:
    start = _text(request.get("start_date"))
    end = _text(request.get("end_date"))
    if start and end:
        return f"{start} 至 {end}"
    if start:
        return start
    if end:
        return end
    return ""


def _list_text(value: Any, default: str = "无") -> str:
    items = public_items(value, limit=20)
    return "、".join(items) if items else default


def _must_include_text(value: Any) -> str:
    items: list[str] = []
    for item in _list(value):
        name = _text(item.get("name"))
        status = _text(item.get("status"))
        if name and status:
            items.append(f"{name}({status})")
        elif name:
            items.append(name)
    return "、".join(items) if items else "无"


def _time_preferences_text(value: Any) -> str:
    prefs = _dict(value)
    if not prefs:
        return "未填写"
    parts: list[str] = []
    if _text(prefs.get("daily_start")):
        parts.append(f"每日开始 {_text(prefs.get('daily_start'))}")
    if _text(prefs.get("daily_end")):
        parts.append(f"每日结束 {_text(prefs.get('daily_end'))}")
    windows: list[str] = []
    for window in _list(prefs.get("rest_windows")):
        days = _text(window.get("days"), "all")
        start = _text(window.get("start"))
        end = _text(window.get("end"))
        if start and end:
            windows.append(f"{days} {start}-{end}")
    if windows:
        parts.append("休息 " + "；".join(windows))
    return "；".join(parts) if parts else "未填写"


def _pace_text(value: Any) -> str:
    pace = _dict(value)
    if not pace:
        return "无"
    parts: list[str] = []
    if _text(pace.get("level")):
        parts.append(_text(pace.get("level")))
    if _text(pace.get("commute_status")):
        parts.append(f"交通状态 {_text(pace.get('commute_status'))}")
    minutes = _to_int(pace.get("total_commute_minutes"), default=-1)
    if minutes >= 0:
        parts.append(f"总交通约 {minutes} 分钟")
    return "；".join(parts) if parts else "无"


def _weather_by_day(value: Any) -> dict[int, dict[str, Any]]:
    weather = _dict(value)
    result: dict[int, dict[str, Any]] = {}
    for item in _list(weather.get("days")):
        day_no = _to_int(item.get("day"))
        if day_no > 0:
            result[day_no] = item
    return result


def _weather_text(value: dict[str, Any] | None) -> str:
    if not value:
        return "无当日天气摘要"
    parts: list[str] = []
    if _text(value.get("date")):
        parts.append(_text(value.get("date")))
    if _text(value.get("weather_text")):
        parts.append(_text(value.get("weather_text")))
    temp_min = value.get("temp_min_c")
    temp_max = value.get("temp_max_c")
    if temp_min is not None or temp_max is not None:
        parts.append(f"{_text(temp_min, '?')}~{_text(temp_max, '?')}°C")
    if _text(value.get("wind_text")):
        parts.append(_text(value.get("wind_text")))
    reminders = _list_text(value.get("reminders"), "")
    if reminders:
        parts.append("提醒：" + reminders)
    return "；".join(parts) if parts else "无当日天气摘要"


def _pace_status_text(value: Any) -> str:
    text = _text(value)
    if text == "WITHIN_LIMIT":
        return "在节奏限制内"
    if text == "OVER_LIMIT":
        return "节奏偏紧"
    return text or "无"


def _place_names(places: list[dict[str, Any]]) -> dict[int, str]:
    names: dict[int, str] = {}
    for place in places:
        place_id = _to_int(place.get("place_id"))
        if place_id > 0:
            names[place_id] = _text(place.get("name"), f"地点{place_id}")
    return names


def _place_text(index: int, place: dict[str, Any]) -> str:
    name = _text(place.get("name"), "未命名地点")
    category = _text(place.get("category"))
    role = _text(place.get("role"))
    optional = "可选" if bool(place.get("optional")) else "必排"
    # v0.15.1: the Writer's paragraph for this stop when the result has one.
    note = " ".join(str(place.get("activity_note") or "").split())
    brief = _text(note or place.get("brief"))
    attrs = " / ".join(part for part in (category, role, optional) if part)
    if brief:
        return f"{index}. {name}｜{attrs}｜{brief}" if attrs else f"{index}. {name}｜{brief}"
    return f"{index}. {name}｜{attrs}" if attrs else f"{index}. {name}"


def _commute_leg_text(
    index: int,
    leg: dict[str, Any],
    place_names: dict[int, str],
) -> str:
    from_id = _to_int(leg.get("from_place_id"))
    to_id = _to_int(leg.get("to_place_id"))
    from_name = place_names.get(from_id, f"地点{from_id}" if from_id else "起点")
    to_name = place_names.get(to_id, f"地点{to_id}" if to_id else "终点")
    mode = _text(leg.get("mode"), "transit")
    duration = _to_int(leg.get("duration_minutes"), default=-1)
    distance = _to_int(leg.get("distance_meters"), default=-1)
    parts = [f"{index}. {from_name} -> {to_name}", f"方式 {mode}"]
    if duration >= 0:
        parts.append(f"约 {duration} 分钟")
    if distance >= 0:
        parts.append(f"{distance} 米")
    return "；".join(parts)


def _normalize_time(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    text = re.sub(r"\s+", " ", text).strip()
    return text if text else default


def _to_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _unlink_silent(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except Exception:
        pass
