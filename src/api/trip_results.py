"""Public result projection API for persisted trip plans."""

from __future__ import annotations

import json
import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import (
    BaseModel,
    Field,
    PrivateAttr,
    ValidationError,
    model_serializer,
    model_validator,
)
from sqlalchemy import text

from src.agents.pretrip_advice import MAX_TIPS, project_public_plan_advice
from src.agents.route_planning import classify_transit_detail, format_transit_summary
from src.agents.route_feasibility import access_status, access_summary
from src.agents.schema import RouteDayGroup
from src.agents.schema import (
    DeliveryStatus,
    PackingChecklistGroup,
    PublishedVariant,
    TransitStep,
    TravelTip,
)
from src.agents.itinerary_lines import strip_itinerary
from src.agents.weather_advisory import is_weather_line
from src.api.public_guard import verify_public_api_client
from src.api.result_stops import activity_note, split_day_text, split_trip_opening, split_trip_tips, stop_fields
from src.config import get_settings
from src.cost_estimate.public import (
    CostEstimateProjectionError,
    CostEstimateSummary,
    project_cost_estimate_summary,
)
from src.pipeline.db import get_session_factory
from src.jobs.generation_delivery import GenerationDelivery, GenerationPolicyVersion, validate_delivery_for_job

router = APIRouter(dependencies=[Depends(verify_public_api_client)])
logger = logging.getLogger(__name__)
READ_RETRIES = 3

PaceLevel = Literal["RELAXED", "MODERATE", "INTENSIVE"]
PaceStatus = Literal["WITHIN_LIMIT", "OVER_LIMIT"]
ResultCommuteMode = Literal["driving", "transit", "walking", "cycling"]
DurationSource = Literal["amap", "estimate"]
MustIncludeStatus = Literal[
    "scheduled",
    "not_scheduled",
    "recorded_candidate",
    "recorded_unmatched",
    "cross_city",
]


class ResultContractUnsupported(Exception):
    """Persisted record cannot satisfy the current public result contract."""


class ResultCity(BaseModel):
    name: str


class ResultRequest(BaseModel):
    days: int
    people_count: int
    preferences: list[str]
    avoid: list[str]


class ResultArtifactRequest(BaseModel):
    """Persisted inputs used only by backend artifact generation."""

    start_date: str | None = None
    end_date: str | None = None
    notes: str = ""


class ResultPace(BaseModel):
    level: PaceLevel
    commute_status: PaceStatus
    total_commute_minutes: int


class ResultSchedule(BaseModel):
    period: Literal["morning", "afternoon", "evening", "night"]
    exact_start: str | None = None
    exact_end: str | None = None
    # "planned" (v0.15.1): the route's own clock rounded to 15 minutes.
    exact_time_source: Literal[
        "reservation", "event", "transport", "verified_venue_rule", "planned"
    ] | None = None


class ResultPlace(BaseModel):
    place_id: int
    name: str
    category: str
    longitude: float | None = None
    latitude: float | None = None
    role: str = "filler"
    optional: bool = False
    brief: str = ""
    # v0.15.1 card fields; the BFF and web already read them. Omitted when
    # empty, so results without them serialize exactly as before.
    stay_minutes: int | None = None
    activity_note: str | None = None
    schedule: ResultSchedule | None = None

    @model_serializer(mode="wrap")
    def _omit_empty_card_fields(self, handler):
        payload = handler(self)
        for key in ("stay_minutes", "activity_note", "schedule"):
            if payload.get(key) is None:
                payload.pop(key, None)
        return payload


class ResultCommuteLeg(BaseModel):
    from_place_id: int
    to_place_id: int
    mode: ResultCommuteMode = "driving"
    duration_source: DurationSource = "estimate"
    duration_minutes: int
    distance_meters: int
    encoded_polyline: str = ""
    transit_steps: list["ResultTransitStep"] | None = None
    transit_summary: str | None = None


class ResultTransitStep(BaseModel):
    kind: Literal["walking", "bus", "rail", "other"]
    duration_minutes: int | None = Field(default=None, ge=0)
    distance_meters: int | None = Field(default=None, ge=0)
    line_name: str | None = None
    provider_type: str | None = None
    from_stop: str | None = None
    to_stop: str | None = None
    stop_count: int | None = Field(default=None, ge=1)


class ResultWeatherDay(BaseModel):
    day: int
    date: str
    weather_text: str = ""
    temp_min_c: int | None = None
    temp_max_c: int | None = None
    wind_text: str = ""
    icon_code: str = "unknown"
    reminders: list[str] = Field(default_factory=list)


class ResultWeather(BaseModel):
    status: str = "skipped_disabled"
    city: str = ""
    days: list[ResultWeatherDay] = Field(default_factory=list)


class ResultMustInclude(BaseModel):
    name: str
    status: MustIncludeStatus
    place_id: int | None = None
    reason: str | None = None
    matched_city: str | None = None
    avoid_conflict: bool | None = None


class ResultRestWindow(BaseModel):
    days: str = "all"
    start: str
    end: str


class ResultTimePreferences(BaseModel):
    daily_start: str | None = None
    daily_end: str | None = None
    rest_windows: list[ResultRestWindow] = Field(default_factory=list)


class ResultDay(BaseModel):
    day: int
    title: str
    places: list[ResultPlace]
    commute_legs: list[ResultCommuteLeg]
    commute_summary: str
    pace_status: PaceStatus
    narrative: str


class ResultLodgingDisplayGroup(BaseModel):
    kind: Literal["economy", "mid", "high", "homestay"]
    label: str
    hotels: list[str]
    nightly_low_cny: int | None = None
    nightly_high_cny: int | None = None

    @model_validator(mode="after")
    def _nightly_prices_together(self) -> "ResultLodgingDisplayGroup":
        if (self.nightly_low_cny is None) != (self.nightly_high_cny is None):
            raise ValueError(
                "nightly_low_cny and nightly_high_cny must both be set or both be None"
            )
        return self


class ResultLodgingDisplayNotice(BaseModel):
    kind: Literal["user_lodging_unmatched", "holiday"]
    text: str


class ResultLodgingDisplay(BaseModel):
    layout: Literal["tiers", "homestay", "brands", "user_lodging", "area_only"]
    area_label: str
    caption: str = ""
    groups: list[ResultLodgingDisplayGroup] = Field(default_factory=list)
    round_trip_minutes_low: int | None = None
    round_trip_minutes_high: int | None = None
    notices: list[ResultLodgingDisplayNotice] = Field(default_factory=list)

    @model_validator(mode="after")
    def _round_trip_together(self) -> "ResultLodgingDisplay":
        if (self.round_trip_minutes_low is None) != (
            self.round_trip_minutes_high is None
        ):
            raise ValueError(
                "round_trip_minutes_low and round_trip_minutes_high must both be set or both be None"
            )
        return self


class ResultAccommodation(BaseModel):
    name: str
    latitude: float
    longitude: float
    source: Literal["user_specified", "auto_recommended"]
    reason: str | None = None
    display: ResultLodgingDisplay | None = None

    @model_serializer(mode="wrap")
    def _omit_empty_display(self, handler):
        payload = handler(self)
        if payload.get("display") is None:
            payload.pop("display", None)
        return payload


class ResultTransportOption(BaseModel):
    type: Literal["train", "flight"]
    no: str
    departure_time: str
    arrival_time: str
    duration_minutes: int
    price: str | None = None
    departure_station: str | None = None
    arrival_station: str | None = None
    airline: str | None = None


class ResultTransportMode(BaseModel):
    mode: Literal["train", "flight"]
    min_duration_minutes: int
    price_range: str
    price_source: Literal["realtime", "static_reference"]
    daily_count: int
    data_source: Literal["realtime", "static_fallback"]
    availability_status: Literal[
        "available_at_query",
        "sold_out_at_query",
        "unknown",
    ] = "unknown"
    availability_checked_at: str | None = None
    options: list[ResultTransportOption]


class ResultTransport(BaseModel):
    from_city: str
    to_city: str
    query_date: str | None = None
    source: Literal["realtime", "mixed", "static_fallback"]
    modes: list[ResultTransportMode]


class ResultPlan(BaseModel):
    plan_id: str
    title: str
    summary: str
    tags: list[str]
    pace: ResultPace
    accommodation: ResultAccommodation | None = None
    transport: ResultTransport | None = None
    days: list[ResultDay]
    cost_estimate: CostEstimateSummary
    packing_checklist: list[PackingChecklistGroup] | None = None
    travel_tips: list[TravelTip] | None = None
    opening: str | None = None

    @model_serializer(mode="wrap")
    def _omit_empty_opening(self, handler):
        payload = handler(self)
        if payload.get("opening") is None:
            payload.pop("opening", None)
        return payload


class TripResultResponse(BaseModel):
    schema_version: Literal["2.1", "2.2", "2.3"]
    published_variant: PublishedVariant
    delivery_status: DeliveryStatus
    generation_policy_version: GenerationPolicyVersion | None = None
    delivery: GenerationDelivery | None = None
    result_id: int
    city: ResultCity
    request: ResultRequest
    weather: ResultWeather
    time_preferences: ResultTimePreferences | None = None
    plans: list[ResultPlan]
    must_include: list[ResultMustInclude] | None = None
    _artifact_request: ResultArtifactRequest = PrivateAttr(
        default_factory=ResultArtifactRequest
    )

    @model_serializer(mode="wrap")
    def serialize_policy_fields(self, handler):
        payload = handler(self)
        if self.schema_version != "2.3":
            payload.pop("generation_policy_version", None)
            payload.pop("delivery", None)
        return payload


def _coerce_json(raw: Any, default: Any) -> Any:
    if raw is None:
        return default
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return default
    return raw


def project_delivery_metadata(
    quality_metrics: dict[str, Any] | None,
) -> tuple[PublishedVariant, DeliveryStatus]:
    """Project historical rows as normal/NORMAL and reject invalid combinations."""
    metrics = quality_metrics if isinstance(quality_metrics, dict) else {}
    variant = metrics.get("published_variant", "normal")
    status = metrics.get("delivery_status", "NORMAL")
    if variant not in {"normal", "safe"} or status not in {"NORMAL", "DEGRADED"}:
        raise ResultContractUnsupported("invalid delivery metadata")
    if variant == "safe" and status != "DEGRADED":
        raise ResultContractUnsupported("safe result must be degraded")
    return variant, status


def _coerce_str_list(raw: Any) -> list[str]:
    value = _coerce_json(raw, [])
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _coerce_plan_json(raw: Any) -> list[dict[str, Any]]:
    value = _coerce_json(raw, [])
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _to_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _date_to_str(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    text_value = str(value).strip()
    return text_value or None


def _short_text(text_value: str, *, limit: int) -> str:
    collapsed = re.sub(r"\s+", " ", text_value).strip()
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1].rstrip() + "…"


def _clean_summary_fragment(value: str) -> str:
    text_value = re.sub(r"\*\*|`|#+|---", " ", value)
    text_value = re.sub(r"\bDay\s*\d+\b|第\s*\d+\s*天", " ", text_value, flags=re.I)
    # "（18:44 左右日落）" goes whole, not as an empty "（ 左右日落）"
    text_value = re.sub(r"[（(][^（）()]*\d{1,2}\s*[:：]\s*\d{2}[^（）()]*[）)]", " ", text_value)
    text_value = re.sub(r"\d{1,2}\s*[:：]\s*\d{2}", " ", text_value)
    text_value = re.sub(r"^\s*[｜|·・、,，:：;；.。\-—>→~～/\\]+\s*", " ", text_value)
    text_value = _strip_weather_reminders(text_value)
    text_value = re.sub(r"\s+", " ", text_value).strip(" ，,、。:：")
    return text_value


def _first_non_empty(values: list[str], *, limit: int = 2) -> list[str]:
    seen: list[str] = []
    for value in values:
        cleaned = _clean_summary_fragment(value)
        if cleaned and cleaned not in seen:
            seen.append(cleaned)
        if len(seen) >= limit:
            break
    return seen


def _plan_summary(plan: dict[str, Any]) -> str:
    # v0.9.0.1: the Writer-generated summary wins when present; the
    # deterministic template below is the fallback for dropped/legacy rows.
    writer_summary = str(plan.get("summary") or "").strip()
    if writer_summary:
        # v0.15.1: the Writer may write up to 150 characters.
        return _short_text(_clean_summary_fragment(writer_summary), limit=160)

    route_plan = plan.get("route_plan") or {}
    blueprint = plan.get("composition_blueprint") or {}
    day_groups = [
        day_group
        for day_group in route_plan.get("day_groups") or []
        if isinstance(day_group, dict)
    ]
    blueprint_days = [
        day
        for day in blueprint.get("days") or []
        if isinstance(day, dict)
    ]
    areas: list[str] = []
    for day_group in day_groups:
        area = _clean_summary_fragment(str(day_group.get("area") or ""))
        if area and not area.isdigit() and area not in areas:
            areas.append(area)
    themes: list[str] = []
    for day in blueprint_days:
        theme = _clean_summary_fragment(
            str(day.get("theme_label") or day.get("theme_code") or "")
        )
        if theme and theme not in themes:
            themes.append(theme)

    parts: list[str] = []
    if day_groups:
        parts.append(f"{len(day_groups)}天")
    if areas:
        parts.append("覆盖" + "、".join(areas[:4]))
    if themes:
        parts.append("、".join(themes[:3]))
    if parts:
        return _short_text(_clean_summary_fragment("，".join(parts) + "。"), limit=90)

    plan_name = _clean_summary_fragment(str(plan.get("plan_name") or ""))
    if plan_name:
        return _short_text(plan_name, limit=90)
    return "围绕核心地点安排的行程方案"


def _assert_result_contract(condition: bool, reason: str) -> None:
    if not condition:
        raise ResultContractUnsupported(reason)


def _project_accommodation(plan: dict[str, Any]) -> ResultAccommodation | None:
    raw = plan.get("accommodation")
    if raw is None:
        return None
    _assert_result_contract(
        isinstance(raw, dict),
        "plan accommodation must be an object",
    )
    _assert_result_contract(
        bool(str(raw.get("name") or "").strip()),
        "plan accommodation name is required",
    )
    payload = dict(raw)
    display_raw = payload.pop("display", None)
    try:
        accommodation = ResultAccommodation.model_validate(payload)
    except ValidationError as exc:
        raise ResultContractUnsupported("plan accommodation is invalid") from exc
    if display_raw is None:
        return accommodation
    try:
        display = ResultLodgingDisplay.model_validate(display_raw)
    except ValidationError:
        logger.warning("dropping invalid lodging display")
        return accommodation
    return accommodation.model_copy(update={"display": display})


def _transport_price(value: Any) -> str | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    rendered = str(int(number)) if number.is_integer() else str(number).rstrip("0").rstrip(".")
    return f"¥{rendered}"


def _project_transport(plan: dict[str, Any]) -> ResultTransport | None:
    raw = plan.get("transport")
    if raw is None:
        return None
    _assert_result_contract(isinstance(raw, dict), "plan transport must be an object")
    raw_modes = raw.get("modes") or []
    _assert_result_contract(isinstance(raw_modes, list), "transport modes must be a list")
    modes: list[ResultTransportMode] = []
    for raw_mode in raw_modes:
        _assert_result_contract(isinstance(raw_mode, dict), "transport mode must be an object")
        mode = str(raw_mode.get("mode") or "")
        _assert_result_contract(mode in {"train", "flight"}, "transport mode is invalid")
        raw_options = raw_mode.get("top_options") or []
        _assert_result_contract(
            isinstance(raw_options, list),
            "transport top_options must be a list",
        )
        options: list[ResultTransportOption] = []
        for raw_option in raw_options:
            _assert_result_contract(
                isinstance(raw_option, dict),
                "transport option must be an object",
            )
            if mode == "train":
                options.append(ResultTransportOption(
                    type="train",
                    no=str(raw_option.get("train_no") or ""),
                    departure_time=str(raw_option.get("departure_time") or ""),
                    arrival_time=str(raw_option.get("arrival_time") or ""),
                    duration_minutes=_to_int(raw_option.get("duration_minutes")),
                    price=_transport_price(raw_option.get("second_class_price")),
                    departure_station=str(
                        raw_option.get("departure_station") or ""
                    ) or None,
                    arrival_station=str(raw_option.get("arrival_station") or "") or None,
                ))
            else:
                options.append(ResultTransportOption(
                    type="flight",
                    no=str(raw_option.get("flight_no") or ""),
                    departure_time=str(raw_option.get("departure_time") or ""),
                    arrival_time=str(raw_option.get("arrival_time") or ""),
                    duration_minutes=_to_int(raw_option.get("duration_minutes")),
                    price=None,
                    departure_station=str(
                        raw_option.get("departure_airport") or ""
                    ) or None,
                    arrival_station=str(raw_option.get("arrival_airport") or "") or None,
                    airline=str(raw_option.get("airline") or "") or None,
                ))
        try:
            modes.append(ResultTransportMode(
                mode=mode,
                min_duration_minutes=_to_int(raw_mode.get("min_duration_minutes")),
                price_range=str(raw_mode.get("price_range") or ""),
                price_source=raw_mode.get("price_source", "static_reference"),
                daily_count=_to_int(raw_mode.get("daily_count")),
                data_source=raw_mode.get("data_source", "static_fallback"),
                availability_status=raw_mode.get("availability_status", "unknown"),
                availability_checked_at=raw_mode.get("availability_checked_at"),
                options=options,
            ))
        except ValidationError as exc:
            raise ResultContractUnsupported("plan transport mode is invalid") from exc
    try:
        return ResultTransport(
            from_city=str(raw.get("from_city") or ""),
            to_city=str(raw.get("to_city") or ""),
            query_date=str(raw.get("query_date") or "") or None,
            source=raw.get("source", "static_fallback"),
            modes=modes,
        )
    except ValidationError as exc:
        raise ResultContractUnsupported("plan transport is invalid") from exc


def _plan_tags(
    *,
    preferences: list[str],
    plan: dict[str, Any],
    pace_level: PaceLevel,
) -> list[str]:
    tags: list[str] = []
    for item in preferences:
        if item not in tags:
            tags.append(item)
    if pace_level == "RELAXED" and "relaxed" not in {tag.lower() for tag in tags}:
        tags.append("relaxed")
    route_plan = plan.get("route_plan") or {}
    for day_group in route_plan.get("day_groups") or []:
        for place in day_group.get("places") or []:
            category = str(place.get("place_type") or "").strip()
            if category and category not in tags:
                tags.append(category)
            if len(tags) >= 5:
                return tags
    return tags[:5]


def _composition_by_place_id(plan: dict[str, Any]) -> dict[int, dict[str, Any]]:
    blueprint = plan.get("composition_blueprint") or {}
    by_id: dict[int, dict[str, Any]] = {}
    for day in blueprint.get("days") or []:
        for stop in day.get("stops") or []:
            place_id = _to_int(stop.get("place_id"))
            if place_id:
                by_id[place_id] = stop
    return by_id


def _composition_day_titles(plan: dict[str, Any]) -> dict[int, str]:
    blueprint = plan.get("composition_blueprint") or {}
    titles: dict[int, str] = {}
    for day in blueprint.get("days") or []:
        day_number = _to_int(day.get("day"))
        title = str(day.get("theme_label") or day.get("theme_code") or "").strip()
        if day_number and title:
            titles[day_number] = title
    return titles


_SHOP_CATEGORIES = {"restaurant", "food", "market"}
_SHOP_ROLES = {"meal_stop", "coffee_stop", "snack_stop", "cafe_stop"}
_SHOP_BRANCH_SUFFIX_RE = re.compile(r"\s*(?:（[^（）]*）|\([^()]*\))\s*$")


def _place_day_title(places: list[ResultPlace], fallback: str) -> str:
    names = [
        place.name for place in places
        if place.category not in _SHOP_CATEGORIES and place.role not in _SHOP_ROLES
    ]
    if not names:
        names = [_SHOP_BRANCH_SUFFIX_RE.sub("", place.name).strip() for place in places]
    unique_names = list(dict.fromkeys(name for name in names if name))
    return " · ".join(unique_names[:3]) or fallback


_DAY_HEADING_RE = re.compile(
    r"(?is)(?:^|\n|\s*---\s*)\s*(?:#{1,6}\s*)?(?:"
    r"\*\*\s*(?:Day\s*(?P<day_b>\d+)|第\s*(?P<cn_b>\d+)\s*天)"
    r"\s*[:：]?\s*(?P<title_b>[^*\n]*?)\s*\*\*"
    r"|(?:Day\s*(?P<day_p>\d+)|第\s*(?P<cn_p>\d+)\s*天)"
    r"\s*[:：]?\s*(?P<title_p>[^\n]*?)\s*(?=\n|$)"
    r")"
)
_URL_RE = re.compile(r"https?://\S+|www\.\S+", flags=re.I)
_INTERNAL_TEXT_RE = re.compile(
    r"\b(?:source|provider|provider_id|crawler_id|recommend_score|quality_score|effective_score)\b"
    r"\s*[:=：]\s*[^,，;；。|｜\s]+",
    flags=re.I,
)
_ALLOWED_COMMUTE_MODES = {"walking", "transit", "driving", "cycling"}


def _normalize_commute_mode(value: Any) -> ResultCommuteMode:
    raw = str(value or "").strip().lower()
    if raw == "taxi":
        return "driving"
    if raw in _ALLOWED_COMMUTE_MODES:
        return raw  # type: ignore[return-value]
    return "driving"


def _persisted_cost_request_mode(
    quality_metrics: dict[str, Any],
) -> Literal["driving", "transit", "cycling"]:
    raw = str(quality_metrics.get("commute_mode_request") or "").strip().lower()
    _assert_result_contract(
        raw in {"driving", "transit", "cycling"},
        "persisted commute_mode_request is required for Schema 2.0",
    )
    return raw  # type: ignore[return-value]


def _heading_day(match: re.Match[str]) -> int:
    return _to_int(
        match.group("day_b")
        or match.group("cn_b")
        or match.group("day_p")
        or match.group("cn_p")
    )


def _heading_title(match: re.Match[str]) -> str:
    return str(match.group("title_b") or match.group("title_p") or "").strip()


def _strip_weather_reminders(text_value: str) -> str:
    without_inline = re.sub(
        r"天气提醒\s*[:：][^。\n]*(?:。)?",
        "",
        text_value,
    )
    kept = [
        line
        for line in without_inline.splitlines()
        if not is_weather_line(line)
    ]
    return "\n".join(kept)


def _clean_day_narrative(title: str, body: str) -> str:
    cleaned_title = _clean_summary_fragment(title)
    if re.search(r"(?:->|→|·|・)", cleaned_title):
        cleaned_title = ""
    parts = [part.strip() for part in (cleaned_title, body) if part.strip()]
    text_value = "\n".join(parts)
    text_value = _strip_weather_reminders(text_value)
    text_value = text_value.replace("---", "\n")
    text_value = re.sub(r"\*\*", "", text_value)
    text_value = re.sub(r"^\s*[｜|·・、,，:：;；.。\-—>→~～/\\]+\s*", "", text_value)
    return _short_text(text_value, limit=1500)


def _clean_public_brief(value: str) -> str:
    text_value = _URL_RE.sub("", value)
    text_value = _INTERNAL_TEXT_RE.sub("", text_value)
    text_value = re.sub(r"\*\*|`|#+|---", " ", text_value)
    text_value = re.sub(r"\s+", " ", text_value).strip(" \t\r\n,，、。;；:：")
    return _short_text(text_value, limit=80) if text_value else ""


def _day_section(plan_text: str, day_number: int, *, total_days: int) -> tuple[str, str] | None:
    """(heading title, body) of one day in the delivered text."""
    if not plan_text.strip():
        return None
    matches = list(_DAY_HEADING_RE.finditer(plan_text))
    if not matches:
        if total_days == 1 and day_number == 1:
            return "", plan_text
        return None

    for index, match in enumerate(matches):
        if _heading_day(match) != day_number:
            continue
        next_match = matches[index + 1] if index + 1 < len(matches) else None
        body = plan_text[match.end(): next_match.start() if next_match else len(plan_text)]
        return _heading_title(match), body
    return None


def _extract_day_narrative(
    plan_text: str,
    day_number: int,
    *,
    total_days: int,
) -> str:
    section = _day_section(plan_text, day_number, total_days=total_days)
    return _clean_day_narrative(*section) if section is not None else ""


def _day_narrative_and_notes(
    section: tuple[str, str] | None,
    *,
    place_names: dict[int, str],
) -> tuple[str, dict[int, str]]:
    """v0.15.1: place paragraphs move to their cards; the day keeps the rest.

    Without any place paragraph (an unexpected text shape) the narrative is
    the whole day as before, so no prose is lost.
    """
    if section is None:
        return "", {}
    title, body = section
    kept, notes = split_day_text(_strip_weather_reminders(body.replace("---", "\n")), place_names)
    if not notes:
        return _clean_day_narrative(title, body), {}
    cleaned_title = _clean_summary_fragment(title)
    if re.search(r"(?:->|→|·|・)", cleaned_title):
        cleaned_title = ""
    lines = [re.sub(r"^\s*[｜|·・、,，:：;；.。\-—>→~～/\\]+\s*", "", line) for line in [cleaned_title, *kept]]
    narrative = "\n".join(line for line in lines if line)
    if len(narrative) > 1500:
        narrative = narrative[:1499].rstrip() + "…"
    return narrative, notes


def _place_brief(place: dict[str, Any], stop: dict[str, Any] | None) -> str:
    # v0.15.1: the first reason that is not a blogger's day route.
    for item in place.get("top_reasons") or []:
        if not isinstance(item, dict):
            break
        reason = strip_itinerary(str(item.get("reason") or "").strip())
        if reason:
            return _clean_public_brief(reason)
    return ""


def _project_place(
    place: dict[str, Any],
    *,
    stop: dict[str, Any] | None,
) -> ResultPlace:
    place_id = _to_int(place.get("place_id"))
    name = str(place.get("name") or "").strip()
    _assert_result_contract(place_id > 0, "place_id is required")
    _assert_result_contract(bool(name), "place name is required")
    category_tags = place.get("category_tags") or []
    category = str(place.get("place_type") or "").strip()
    if not category and category_tags:
        category = str(category_tags[0]).strip()
    return ResultPlace(
        place_id=place_id,
        name=name,
        category=category or "place",
        longitude=_to_float(place.get("longitude")),
        latitude=_to_float(place.get("latitude")),
        role=str((stop or {}).get("role") or "filler").strip() or "filler",
        optional=False,
        brief=_place_brief(place, stop),
    )


def _project_commute_leg(leg: dict[str, Any]) -> ResultCommuteLeg:
    from_place_id = _to_int(leg.get("from_place_id"))
    to_place_id = _to_int(leg.get("to_place_id"))
    duration_minutes = _to_int(leg.get("duration_minutes"))
    distance_meters = _to_int(leg.get("distance_meters"))
    _assert_result_contract(from_place_id > 0, "commute from_place_id is required")
    _assert_result_contract(to_place_id > 0, "commute to_place_id is required")
    _assert_result_contract(duration_minutes >= 0, "commute duration is invalid")
    _assert_result_contract(distance_meters >= 0, "commute distance is invalid")
    mode = _normalize_commute_mode(leg.get("mode"))
    duration_source: DurationSource = (
        "amap"
        if str(leg.get("source") or "").strip() == "amap"
        else "estimate"
    )
    encoded_polyline = str(leg.get("encoded_polyline") or "")
    transit_steps: list[ResultTransitStep] | None = None
    transit_summary: str | None = None
    if mode == "transit":
        raw_steps = leg.get("transit_steps") or []
        normalized_steps: list[TransitStep] = []
        if isinstance(raw_steps, list):
            for raw_step in raw_steps:
                try:
                    normalized_steps.append(TransitStep.model_validate(raw_step))
                except (TypeError, ValueError):
                    normalized_steps = []
                    break
        detail_quality = str(leg.get("transit_detail_quality") or "missing")
        if (
            detail_quality == "complete"
            and normalized_steps
            and classify_transit_detail(normalized_steps) == "complete"
        ):
            transit_steps = [
                ResultTransitStep.model_validate(step.model_dump(mode="json"))
                for step in normalized_steps
            ]
            transit_summary = format_transit_summary(
                duration_minutes,
                normalized_steps,
                detail_quality="complete",
            )
        else:
            transit_steps = []
            transit_summary = format_transit_summary(
                duration_minutes,
                [],
                detail_quality="missing",
            )
    return ResultCommuteLeg(
        from_place_id=from_place_id,
        to_place_id=to_place_id,
        mode=mode,
        duration_source=duration_source,
        duration_minutes=duration_minutes,
        distance_meters=distance_meters,
        encoded_polyline=encoded_polyline,
        transit_steps=transit_steps,
        transit_summary=transit_summary,
    )


def _budget_days(plan: dict[str, Any]) -> dict[int, dict[str, Any]]:
    budget_result = plan.get("budget_result") or {}
    by_day: dict[int, dict[str, Any]] = {}
    for day in budget_result.get("days") or []:
        day_number = _to_int(day.get("day"))
        if day_number:
            by_day[day_number] = day
    return by_day


def _model_route_budget_already_checked(budget_day: dict[str, Any] | None) -> bool:
    """A sealed model day was accepted under the new commute caps.

    The number stored in ``budget_minutes`` is still the old daily budget.
    Comparing them again would mark a delivered day over limit.
    """
    if not isinstance(budget_day, dict):
        return False
    return (
        str(budget_day.get("status") or "").lower() == "within_budget"
        and str(budget_day.get("reason") or "") == "locked_model_route"
    )


def _day_pace_status(day_group: dict[str, Any], budget_day: dict[str, Any] | None) -> PaceStatus:
    if _model_route_budget_already_checked(budget_day):
        return "WITHIN_LIMIT"
    if budget_day:
        status = str(budget_day.get("status") or "").lower()
        if status in {"relaxed_exception", "infeasible"}:
            return "OVER_LIMIT"
        budget_minutes = _to_int(budget_day.get("budget_minutes"))
        commute_minutes = _to_int(budget_day.get("commute_minutes"))
        if budget_minutes and commute_minutes > budget_minutes:
            return "OVER_LIMIT"
    if _to_int(day_group.get("commute_minutes")) > 150:
        return "OVER_LIMIT"
    return "WITHIN_LIMIT"


def _day_clock(day_group: dict[str, Any]):
    """The clock the route planner stored for one day; None for estimates too.

    Cards show only a clock the scheduler computed. Selector routes and older
    rows keep "第 N 站" rather than an estimate presented as the plan.
    """
    from src.agents.route_timeline import stored_timeline

    try:
        return stored_timeline(RouteDayGroup.model_validate(day_group))
    except (ValueError, TypeError, KeyError):
        return None


@dataclass
class _TripLevelText:
    opening: str | None = None
    tips: str | None = None


def _project_days(
    plan: dict[str, Any], *, cards: bool = True, trip_text: _TripLevelText | None = None,
) -> list[ResultDay]:
    route_plan = plan.get("route_plan") or {}
    day_groups = route_plan.get("day_groups") or []
    _assert_result_contract(isinstance(day_groups, list) and bool(day_groups), "day_groups are required")
    plan_text = str(plan.get("plan_text") or "")
    stop_by_place_id = _composition_by_place_id(plan)
    day_titles = _composition_day_titles(plan)
    budget_by_day = _budget_days(plan)
    total_days = len(day_groups)
    planned_time = bool(getattr(get_settings(), "result_planned_time_enabled", False))

    days: list[ResultDay] = []
    for index, day_group in enumerate(day_groups, 1):
        _assert_result_contract(isinstance(day_group, dict), "day_group must be an object")
        day_number = _to_int(day_group.get("day"), index) or index
        raw_places = day_group.get("places") or []
        _assert_result_contract(isinstance(raw_places, list) and bool(raw_places), "day places are required")
        place_dicts = [place for place in raw_places if isinstance(place, dict)]
        if cards:
            place_names = {_to_int(p.get("place_id")): str(p.get("name") or "").strip() for p in place_dicts}
            section = _day_section(plan_text, day_number, total_days=total_days)
            if section is not None and trip_text is not None:
                heading, body = section
                if day_number == 1:
                    body, trip_text.opening = split_trip_opening(body, place_names)
                if index == total_days and place_dicts:
                    last_name = str(place_dicts[-1].get("name") or "").strip()
                    body, trip_text.tips = split_trip_tips(body, last_name)
                section = heading, body
            narrative, notes = _day_narrative_and_notes(section, place_names=place_names)
            clock = stop_fields(_day_clock(day_group), planned_time=planned_time)
        else:
            narrative, notes, clock = _extract_day_narrative(plan_text, day_number, total_days=total_days), {}, {}
        places = []
        for place in place_dicts:
            place_id = _to_int(place.get("place_id"))
            projected = _project_place(place, stop=stop_by_place_id.get(place_id))
            card = clock.get(place_id, {})
            places.append(projected.model_copy(update={
                "stay_minutes": card.get("stay_minutes"),
                "schedule": ResultSchedule(**card["schedule"]) if card.get("schedule") else None,
                "activity_note": activity_note(notes[place_id], place) if place_id in notes else None,
            }))
        _assert_result_contract(bool(places), "projected day places are required")
        raw_commute_legs = day_group.get("commute_legs") or []
        _assert_result_contract(isinstance(raw_commute_legs, list), "commute_legs must be a list")
        _assert_result_contract(
            all(isinstance(leg, dict) for leg in raw_commute_legs),
            "commute_legs contain invalid entries",
        )
        commute_legs = [
            _project_commute_leg(leg)
            for leg in raw_commute_legs
        ]
        _assert_result_contract(
            len(places) <= 1 or bool(commute_legs),
            "multi-place day requires commute_legs",
        )
        commute_minutes = _to_int(day_group.get("commute_minutes"))
        locked_v2 = None
        if route_plan.get("route_policy_version") == "selector-route-v2":
            try:
                locked_v2 = RouteDayGroup.model_validate(day_group)
            except (ValueError, TypeError) as exc:
                raise ResultContractUnsupported("invalid persisted v2 day") from exc
            ids = [p.place_id for p in locked_v2.places]
            _assert_result_contract(
                [(l.from_place_id, l.to_place_id) for l in locked_v2.commute_legs] == list(zip(ids, ids[1:])),
                "v2 POI legs must match the locked order",
            )
            # ``feasible`` is the stored verdict. A model route stores the
            # new-rule verdict (old load, leg and day-structure codes are not
            # in violations). The checks below are fact agreement, for both
            # selector and model routes.
            _assert_result_contract(
                locked_v2.day_feasibility is not None and locked_v2.day_feasibility.feasible
                and locked_v2.day_feasibility.ordered_place_ids == ids,
                "v2 persisted feasibility is required",
            )
            access = {leg.direction: leg for leg in locked_v2.access_legs}
            _assert_result_contract(
                not locked_v2.access_legs or (
                    len(locked_v2.access_legs) == 2 and set(access) == {"outbound", "inbound"}
                    and access["outbound"].place_id == ids[0] and access["inbound"].place_id == ids[-1]
                    and len({(a.anchor_source, a.anchor_latitude, a.anchor_longitude) for a in locked_v2.access_legs}) == 1
                ), "v2 accommodation access must match locked endpoints",
            )
            fact = locked_v2.day_feasibility
            _assert_result_contract(
                fact.access_status == access_status(locked_v2.access_legs)
                and fact.access_minutes == (sum(a.duration_minutes for a in access.values()) if access else None)
                and fact.poi_commute_minutes == sum(l.duration_minutes for l in locked_v2.commute_legs),
                "v2 persisted commute breakdown must agree",
            )
            _assert_result_contract(
                commute_minutes == sum(l.duration_minutes for l in locked_v2.commute_legs) + sum(l.duration_minutes for l in locked_v2.access_legs),
                "v2 commute total must include access",
            )
        commute_notes = [
            str(note).strip()
            for note in day_group.get("commute_notes") or []
            if str(note).strip()
        ]
        if locked_v2 is not None:
            # Read persisted facts independently of the current generation feature flag.
            commute_notes = [leg.note for leg in locked_v2.commute_legs if leg.note]
            summary = access_summary(locked_v2)
            if summary:
                commute_notes.append(summary)
        title = (
            day_titles.get(day_number)
            or str(day_group.get("area") or "").strip()
            or f"Day {day_number}"
        )
        if cards:
            title = _place_day_title(places, title)
        _assert_result_contract(bool(str(title).strip()), "day title is required")
        days.append(ResultDay(
            day=day_number,
            title=title,
            places=places,
            commute_legs=commute_legs,
            commute_summary=(
                "；".join(commute_notes)
                or f"Total commute about {commute_minutes} minutes"
            ),
            pace_status=_day_pace_status(
                day_group,
                budget_by_day.get(day_number),
            ),
            narrative=narrative,
        ))
    return days


def _project_plan(
    *,
    index: int,
    plan: dict[str, Any],
    preferences: list[str],
    expected_days: int,
    people_count: int,
    from_city_present: bool,
    requested_commute_mode: Literal["driving", "transit", "cycling"],
    schema_22_enabled: bool | None = None,
    cards: bool = False,
) -> ResultPlan:
    _assert_result_contract(isinstance(plan, dict), "plan must be an object")
    trip_text = _TripLevelText()
    days = _project_days(plan, cards=cards, trip_text=trip_text)
    _assert_result_contract(bool(days), "plan days are required")
    _assert_result_contract(
        len(days) == expected_days,
        "plan day count must match request.days",
    )
    total_commute = sum(
        sum(leg.duration_minutes for leg in day.commute_legs)
        or _to_int((plan.get("route_plan") or {}).get("commute_minutes"))
        for day in days
    )
    if not total_commute:
        total_commute = sum(
            _to_int(day_group.get("commute_minutes"))
            for day_group in (plan.get("route_plan") or {}).get("day_groups") or []
        )
    if (plan.get("route_plan") or {}).get("route_policy_version") == "selector-route-v2":
        total_commute = sum(_to_int(group.get("commute_minutes"))
                            for group in plan["route_plan"]["day_groups"])
    avg_commute = total_commute / max(len(days), 1)
    if avg_commute <= 90:
        pace_level: PaceLevel = "RELAXED"
    elif avg_commute <= 140:
        pace_level = "MODERATE"
    else:
        pace_level = "INTENSIVE"
    commute_status: PaceStatus = (
        "OVER_LIMIT"
        if any(day.pace_status == "OVER_LIMIT" for day in days)
        else "WITHIN_LIMIT"
    )
    title = str(plan.get("plan_name") or f"Plan {index + 1}").strip()
    _assert_result_contract(bool(title), "plan title is required")
    summary = _plan_summary(plan)
    _assert_result_contract(bool(summary), "plan summary is required")
    try:
        cost_estimate = project_cost_estimate_summary(
            plan.get("cost_estimate_snapshot"),
            plan_index=index,
            plan_key=title,
            people_count=people_count,
            days=expected_days,
            from_city_present=from_city_present,
            requested_commute_mode=requested_commute_mode,
        )
    except CostEstimateProjectionError as exc:
        raise ResultContractUnsupported(str(exc)) from exc
    packing_checklist, travel_tips = project_public_plan_advice(
        plan,
        schema_22_enabled=schema_22_enabled,
    )
    if trip_text.tips:
        overall_tip = TravelTip(title="整体提醒", content=trip_text.tips)
        previous_tips = [tip for tip in travel_tips or [] if tip != overall_tip]
        travel_tips = [*previous_tips[:MAX_TIPS - 1], overall_tip]
    return ResultPlan(
        plan_id=f"plan_{chr(ord('a') + index)}",
        title=title,
        summary=summary,
        tags=_plan_tags(
            preferences=preferences,
            plan=plan,
            pace_level=pace_level,
        ),
        pace=ResultPace(
            level=pace_level,
            commute_status=commute_status,
            total_commute_minutes=total_commute,
        ),
        accommodation=_project_accommodation(plan),
        transport=_project_transport(plan),
        days=days,
        cost_estimate=cost_estimate,
        packing_checklist=packing_checklist,
        travel_tips=travel_tips,
        opening=trip_text.opening,
    )


def _project_weather(
    plans: list[dict[str, Any]],
    *,
    city: str,
    expected_days: int,
    quality_metrics: dict[str, Any] | None = None,
) -> ResultWeather:
    raw: dict[str, Any] = {}
    for plan in plans:
        candidate = plan.get("weather_display")
        if isinstance(candidate, dict):
            raw = candidate
            break
    metrics = quality_metrics if isinstance(quality_metrics, dict) else {}
    status = str(
        raw.get("status")
        or metrics.get("weather_status")
        or "skipped_disabled"
    ).strip()
    weather_city = str(raw.get("city") or city or "").strip()
    days: list[ResultWeatherDay] = []
    if status == "ok":
        for item in raw.get("days") or []:
            if not isinstance(item, dict):
                continue
            day = _to_int(item.get("day"))
            date_value = str(item.get("date") or "").strip()
            if not day or not date_value:
                continue
            days.append(ResultWeatherDay(
                day=day,
                date=date_value,
                weather_text=str(item.get("weather_text") or "").strip(),
                temp_min_c=(
                    _to_int(item.get("temp_min_c"))
                    if item.get("temp_min_c") is not None
                    else None
                ),
                temp_max_c=(
                    _to_int(item.get("temp_max_c"))
                    if item.get("temp_max_c") is not None
                    else None
                ),
                wind_text=str(item.get("wind_text") or "").strip(),
                icon_code=str(item.get("icon_code") or "unknown").strip(),
                reminders=[
                    str(reminder).strip()
                    for reminder in item.get("reminders") or []
                    if str(reminder).strip()
                ][:2],
            ))
        days = days[:expected_days]
        if expected_days > 1 and len(days) <= 1:
            return ResultWeather(
                status="skipped_date_out_of_range",
                city=weather_city,
                days=[],
            )
    return ResultWeather(status=status, city=weather_city, days=days)


def _project_must_include_report(
    quality_metrics: dict[str, Any] | None,
) -> list[ResultMustInclude] | None:
    metrics = quality_metrics if isinstance(quality_metrics, dict) else {}
    raw_report = metrics.get("must_include_report")
    if not isinstance(raw_report, list):
        return None
    projected = []
    for item in raw_report:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        status = str(item.get("status") or "").strip()
        if not name or status not in MustIncludeStatus.__args__:
            continue
        place_id = _to_int(item.get("place_id"))
        public_place_id = (
            place_id
            if place_id > 0 and status in {"scheduled", "not_scheduled"}
            else None
        )
        reason = str(item.get("reason") or "").strip() or None
        matched_city = str(item.get("matched_city") or "").strip() or None
        avoid_conflict = item.get("avoid_conflict")
        projected.append(ResultMustInclude(
            name=name,
            status=status,
            place_id=public_place_id,
            reason=reason,
            matched_city=matched_city,
            avoid_conflict=(
                bool(avoid_conflict)
                if avoid_conflict is not None
                else None
            ),
        ))
    return projected or None


def _project_time_preferences(
    quality_metrics: dict[str, Any] | None,
) -> ResultTimePreferences | None:
    metrics = quality_metrics if isinstance(quality_metrics, dict) else {}
    raw = metrics.get("time_preferences")
    if not isinstance(raw, dict):
        return None

    daily_start = str(raw.get("daily_start") or "").strip() or None
    daily_end = str(raw.get("daily_end") or "").strip() or None
    rest_windows: list[ResultRestWindow] = []
    for item in raw.get("rest_windows") or []:
        if not isinstance(item, dict):
            continue
        start = str(item.get("start") or "").strip()
        end = str(item.get("end") or "").strip()
        if not start or not end:
            continue
        rest_windows.append(ResultRestWindow(
            days=str(item.get("days") or "all").strip() or "all",
            start=start,
            end=end,
        ))

    if daily_start is None and daily_end is None and not rest_windows:
        return None
    return ResultTimePreferences(
        daily_start=daily_start,
        daily_end=daily_end,
        rest_windows=rest_windows,
    )


def project_trip_result(row: Any) -> TripResultResponse:
    preferences = _coerce_str_list(row.preferences)
    avoid = _coerce_str_list(row.avoid)
    plans = _coerce_plan_json(row.plan_json)
    _assert_result_contract(bool(plans), "plan_json must contain at least one plan")
    quality_metrics = row.quality_metrics if isinstance(row.quality_metrics, dict) else {}
    job_policy = getattr(row, "generation_policy_version", None) or "legacy"
    persisted_policy = quality_metrics.get("generation_policy_version")
    delivery: GenerationDelivery | None = None
    if job_policy == "v0.12":
        if persisted_policy != "v0.12":
            raise ResultContractUnsupported("v0.12 result policy metadata is missing")
        if not {"published_variant", "delivery_status"}.issubset(quality_metrics):
            raise ResultContractUnsupported("v0.12 result delivery fields are missing")
    published_variant, delivery_status = project_delivery_metadata(quality_metrics)
    if job_policy == "v0.12":
        try:
            delivery = GenerationDelivery.model_validate(quality_metrics.get("delivery"))
            validate_delivery_for_job(
                delivery,
                status="SUCCESS",
                result_type="PLAN_READY",
                result_record_id=int(row.id),
                published_variant=published_variant,
                delivery_status=delivery_status,
            )
        except (ValidationError, ValueError) as exc:
            raise ResultContractUnsupported("invalid v0.12 delivery metadata") from exc
        if delivery.outcome not in {"DETAILED", "BASIC"}:
            raise ResultContractUnsupported("v0.12 result is not deliverable")
        raw_job_delivery = getattr(row, "job_delivery_json", None)
        if raw_job_delivery is None:
            raise ResultContractUnsupported("v0.12 job delivery metadata is missing")
        try:
            job_delivery = GenerationDelivery.model_validate(raw_job_delivery)
        except ValidationError as exc:
            raise ResultContractUnsupported("invalid v0.12 job delivery") from exc
        if job_delivery != delivery:
            raise ResultContractUnsupported("job and result delivery metadata conflict")
    elif persisted_policy not in {None, "legacy"}:
        raise ResultContractUnsupported("result policy conflicts with its job")
    city = str(row.to_city or "")
    _assert_result_contract(bool(city.strip()), "to_city is required")
    expected_days = _to_int(row.days, 0)
    _assert_result_contract(expected_days > 0, "request days are required")
    people_count = _to_int(row.people_count, 0)
    _assert_result_contract(people_count > 0, "request people_count is required")
    requested_commute_mode = _persisted_cost_request_mode(quality_metrics)
    from_city_present = bool(str(row.from_city or "").strip())
    # One explicit flag decision controls the complete projection: false emits
    # Schema 2.1 and omits advice even if stored; true emits 2.2 and projects
    # valid plan advice. Historical rows without advice stay readable either way.
    schema_22_enabled = bool(get_settings().result_schema_22_enabled)
    projected_plans = [
        _project_plan(
            index=index,
            plan=plan,
            preferences=preferences,
            expected_days=expected_days,
            people_count=people_count,
            from_city_present=from_city_present,
            requested_commute_mode=requested_commute_mode,
            schema_22_enabled=schema_22_enabled,
            # v0.15.1 cards are a Schema 2.3 (v0.12 policy) addition.
            cards=job_policy == "v0.12",
        )
        for index, plan in enumerate(plans)
    ]
    _assert_result_contract(len(projected_plans) == len(plans), "all plans must project")
    result = TripResultResponse(
        schema_version=("2.3" if job_policy == "v0.12" else ("2.2" if schema_22_enabled else "2.1")),
        published_variant=published_variant,
        delivery_status=delivery_status,
        generation_policy_version=("v0.12" if job_policy == "v0.12" else None),
        delivery=delivery,
        result_id=int(row.id),
        city=ResultCity(name=city),
        request=ResultRequest(
            days=_to_int(row.days, 1),
            people_count=people_count,
            preferences=preferences,
            avoid=avoid,
        ),
        weather=_project_weather(
            plans,
            city=city,
            expected_days=expected_days,
            quality_metrics=quality_metrics,
        ),
        time_preferences=_project_time_preferences(quality_metrics),
        plans=projected_plans,
        must_include=_project_must_include_report(quality_metrics),
    )
    result._artifact_request = ResultArtifactRequest(
        start_date=_date_to_str(row.start_date),
        end_date=_date_to_str(row.end_date),
        notes=str(row.notes or ""),
    )
    return result


def artifact_request_fields(result: TripResultResponse) -> dict[str, Any]:
    """Return backend-only persisted fields for PDF/share source construction."""

    return result._artifact_request.model_dump(mode="json", exclude_none=True)


async def get_trip_result(
    result_record_id: int,
    *,
    job_id: str | None = None,
) -> TripResultResponse | None:
    factory = get_session_factory()
    last_error: Exception | None = None
    for attempt in range(1, READ_RETRIES + 1):
        try:
            async with factory() as session:
                row = (await session.execute(
                    text("""
                        SELECT plan.id, plan.user_query, plan.from_city, plan.to_city,
                               plan.start_date, plan.end_date,
                               plan.days, plan.people_count,
                               plan.preferences, plan.avoid, plan.notes,
                               plan.plan_json, plan.quality_metrics,
                               COALESCE((
                                   SELECT job.generation_policy_version
                                   FROM travel_trip_job AS job
                                   WHERE job.result_record_id = plan.id
                                     AND job.status = 'SUCCESS'
                                     AND job.result_type = 'PLAN_READY'
                                     AND (
                                         CAST(:job_id AS VARCHAR) IS NULL
                                         OR job.job_id = CAST(:job_id AS VARCHAR)
                                     )
                                   ORDER BY job.id ASC
                                   LIMIT 1
                               ), 'legacy') AS generation_policy_version,
                               (
                                   SELECT job.delivery_json
                                   FROM travel_trip_job AS job
                                   WHERE job.result_record_id = plan.id
                                     AND job.status = 'SUCCESS'
                                     AND job.result_type = 'PLAN_READY'
                                     AND (
                                         CAST(:job_id AS VARCHAR) IS NULL
                                         OR job.job_id = CAST(:job_id AS VARCHAR)
                                     )
                                   ORDER BY job.id ASC
                                   LIMIT 1
                               ) AS job_delivery_json,
                               EXISTS (
                                   SELECT 1
                                   FROM travel_trip_job AS job
                                   WHERE job.result_record_id = plan.id
                                     AND job.status = 'SUCCESS'
                                     AND job.result_type = 'PLAN_READY'
                                     AND (
                                         CAST(:job_id AS VARCHAR) IS NULL
                                         OR job.job_id = CAST(:job_id AS VARCHAR)
                                     )
                               ) AS has_plan_ready_job
                        FROM travel_plan_record AS plan
                        WHERE plan.id = :result_record_id
                    """),
                    {"result_record_id": result_record_id, "job_id": job_id},
                )).one_or_none()
            if row is None:
                return None
            if not bool(row.has_plan_ready_job):
                raise ResultContractUnsupported("record is not linked to a PLAN_READY success job")
            return project_trip_result(row)
        except ResultContractUnsupported:
            raise
        except Exception as exc:
            last_error = exc
            if attempt >= READ_RETRIES:
                break
            logger.warning(
                "retry get_trip_result result_record_id=%s job_id=%s attempt=%s error=%s",
                result_record_id,
                job_id,
                attempt,
                exc,
            )
            await asyncio.sleep(0.2 * attempt)
    assert last_error is not None
    raise last_error


@router.get(
    "/trip/results/{result_record_id}",
    response_model=TripResultResponse,
    response_model_exclude_none=True,
)
async def trip_result(
    result_record_id: int,
    job_id: str | None = Query(default=None, min_length=1, max_length=64),
):
    try:
        result = await get_trip_result(result_record_id, job_id=job_id)
    except ResultContractUnsupported:
        return JSONResponse(
            status_code=422,
            content={
                "ok": False,
                "error": {
                    "code": "RESULT_CONTRACT_UNSUPPORTED",
                    "message": "该攻略由旧版本生成，暂不支持打开，请重新生成",
                },
            },
        )
    if result is None:
        return JSONResponse(
            status_code=404,
            content={
                "ok": False,
                "error": {
                    "code": "RESULT_NOT_FOUND",
                    "message": "攻略不存在",
                },
            },
        )
    return result
