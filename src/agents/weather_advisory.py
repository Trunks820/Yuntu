"""Deterministic Weather Advisory Payload for v0.8.0."""

from __future__ import annotations

import re
import time
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from src.agents.schema import RoutePlan, TripRequest
from src.config import get_settings

WEATHER_PROVIDER = "amap"
WEATHER_GRANULARITY = "city_daily"
WEATHER_QUERY_SCOPE = "city"
SUCCESS_TTL_SECONDS = 1800
FAILURE_TTL_SECONDS = 300

STATUS_OK = "ok"
STATUS_DISABLED = "skipped_disabled"
STATUS_NO_TRAVEL_DATE = "skipped_no_travel_date"
STATUS_API_KEY_MISSING = "skipped_api_key_missing"
STATUS_CITY_UNRESOLVED = "skipped_city_unresolved"
STATUS_DATE_OUT_OF_RANGE = "skipped_date_out_of_range"
STATUS_API_ERROR = "skipped_api_error"
STATUS_RATE_LIMITED = "skipped_rate_limited"
STATUS_FORBIDDEN = "skipped_forbidden"
STATUS_TIMEOUT = "skipped_timeout"

WEATHER_STATUSES = {
    STATUS_OK,
    STATUS_DISABLED,
    STATUS_NO_TRAVEL_DATE,
    STATUS_API_KEY_MISSING,
    STATUS_CITY_UNRESOLVED,
    STATUS_DATE_OUT_OF_RANGE,
    STATUS_API_ERROR,
    STATUS_RATE_LIMITED,
    STATUS_FORBIDDEN,
    STATUS_TIMEOUT,
}

REMINDER_TEMPLATES = {
    "rain": "户外步行段注意带伞、防滑。",
    "heat": "午后 citywalk 注意防晒、补水。",
    "cold": "早晚温差较大，注意加衣。",
    "wind": "风力较大时，户外停留注意保暖和安全。",
    "thunderstorm": "如遇雷雨，户外段注意避开临水、高处和空旷区域。",
    "heavy_rain": "如遇强降雨，户外步行段注意缩短停留并确认交通状态。",
}

CITY_ADCODE_MAP = {
    "北京": "110000",
    "北京市": "110000",
    "上海": "310000",
    "上海市": "310000",
    "天津": "120000",
    "天津市": "120000",
    "重庆": "500000",
    "重庆市": "500000",
    "成都": "510100",
    "成都市": "510100",
    "西安": "610100",
    "西安市": "610100",
    "杭州": "330100",
    "杭州市": "330100",
    "南京": "320100",
    "南京市": "320100",
    "苏州": "320500",
    "苏州市": "320500",
    "广州": "440100",
    "广州市": "440100",
    "深圳": "440300",
    "深圳市": "440300",
    "武汉": "420100",
    "武汉市": "420100",
    "长沙": "430100",
    "长沙市": "430100",
    "青岛": "370200",
    "青岛市": "370200",
    "厦门": "350200",
    "厦门市": "350200",
}

DATE_RE = re.compile(r"(?P<year>20\d{2})[-/.年](?P<month>\d{1,2})[-/.月](?P<day>\d{1,2})")
MONTH_DAY_RE = re.compile(r"(?<!\d)(?P<month>\d{1,2})月(?P<day>\d{1,2})日?")

WEATHER_LINE_MARKERS = (
    "天气提醒",
    "天气",
    "下雨",
    "有雨",
    "降雨",
    "雨天",
    "强降雨",
    "雷雨",
    "雷阵雨",
    "阵雨",
    "小雨",
    "暴雨",
    "冰雹",
    "阴天",
    "晴天",
    "多云",
    "降雪",
    "下雪",
    "雪天",
    "雾霾",
    "大雾",
    "能见度",
    "防晒",
    "补水",
    "带伞",
    "防滑",
    "高温",
    "气温",
    "降温",
    "温差",
    "加衣",
    "风力",
    "大风",
    "天气服务",
    "未查到天气",
    "出发前查看天气",
    "天气以实际为准",
    "临近出发",
)
WEATHER_DISCLAIMER_MARKERS = (
    "天气服务暂不可用",
    "未查到天气",
    "建议出发前查看天气",
    "天气以实际为准",
    "临近出发再确认",
    "天气暂不可查",
)
WEATHER_ROUTE_DRIFT_MARKERS = (
    "改去",
    "改成",
    "替换",
    "调整路线",
    "换到",
    "改走",
    "雨天建议",
    "建议改",
)


class WeatherAdvisoryDay(BaseModel):
    day_index: int
    date: str
    conditions: list[str] = Field(default_factory=list)
    authorized_reminders: list[str] = Field(default_factory=list)


class WeatherDisplayDay(BaseModel):
    day: int
    date: str
    weather_text: str = ""
    temp_min_c: int | None = None
    temp_max_c: int | None = None
    wind_text: str = ""
    icon_code: str = "unknown"
    reminders: list[str] = Field(default_factory=list)


class WeatherAdvisoryPayload(BaseModel):
    status: str
    provider: str = WEATHER_PROVIDER
    granularity: str = WEATHER_GRANULARITY
    query_scope: str = WEATHER_QUERY_SCOPE
    city: str = ""
    adcode: str = ""
    days_requested: int = 0
    days_authorized: int = 0
    days_out_of_range: int = 0
    days: list[WeatherAdvisoryDay] = Field(default_factory=list)
    display_days: list[WeatherDisplayDay] = Field(default_factory=list)
    lookup_attempted: bool = False
    cache_hit: bool = False
    cache_negative_hit: bool = False
    cache_ttl_seconds: int = 0
    date_range_mismatch: bool = False

    def is_ok(self) -> bool:
        return self.status == STATUS_OK

    def reminders_for_day(self, day_index: int) -> list[str]:
        for day in self.days:
            if day.day_index == day_index:
                return list(day.authorized_reminders[:2])
        return []

    def display_payload(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "city": self.city,
            "days": [
                day.model_dump(mode="json")
                for day in (self.display_days if self.status == STATUS_OK else [])
            ],
        }

    def metrics_summary(self) -> dict[str, Any]:
        return {
            "weather_status": self.status,
            "weather_provider": self.provider,
            "weather_lookup_attempted": self.lookup_attempted,
            "weather_granularity": self.granularity,
            "weather_query_scope": self.query_scope,
            "weather_days_requested": self.days_requested,
            "weather_days_authorized": self.days_authorized,
            "weather_days_out_of_range": self.days_out_of_range,
            "weather_date_range_mismatch": self.date_range_mismatch,
            "weather_cache_hit": self.cache_hit,
            "weather_cache_negative_hit": self.cache_negative_hit,
            "weather_cache_ttl_seconds": self.cache_ttl_seconds,
        }

    def prompt_text(self) -> str:
        lines = [
            "Weather Advisory Payload（runtime-only，天气授权唯一来源）：",
            f"- status={self.status}",
            f"- provider={self.provider}",
            f"- granularity={self.granularity}",
            f"- query_scope={self.query_scope}",
            "规则：天气展示由 /trip/results.weather 结构化字段承载；"
            "不得解释 raw Amap、conditions、temperature、wind 或天气体感；"
            "v0.8.0.2 起天气只通过 /trip/results.weather 展示；"
            "Writer 不得写任何天气提醒或天气正文。",
        ]
        lines.append("- 正文必须完全不出现天气内容。")
        return "\n".join(lines)


class WeatherCacheEntry(BaseModel):
    payload: WeatherAdvisoryPayload
    expires_at: float
    ttl_seconds: int
    negative: bool = False


_WEATHER_CACHE: dict[str, WeatherCacheEntry] = {}


def clear_weather_cache() -> None:
    _WEATHER_CACHE.clear()


def city_to_adcode(city: str) -> str | None:
    normalized = (city or "").strip()
    if not normalized:
        return None
    return CITY_ADCODE_MAP.get(normalized) or CITY_ADCODE_MAP.get(
        normalized.removesuffix("市")
    )


def weather_cache_key(
    *,
    provider: str,
    adcode: str,
    city: str,
    target_date: str,
    days_requested: int,
) -> str:
    key_city = adcode or city
    return f"{provider}:{key_city}:{target_date}:days={max(1, days_requested)}"


def cache_get(key: str) -> WeatherAdvisoryPayload | None:
    entry = _WEATHER_CACHE.get(key)
    if entry is None:
        return None
    if entry.expires_at <= time.monotonic():
        _WEATHER_CACHE.pop(key, None)
        return None
    payload = entry.payload.model_copy(update={
        "cache_hit": not entry.negative,
        "cache_negative_hit": entry.negative,
        "cache_ttl_seconds": entry.ttl_seconds,
    })
    return payload


def cache_set(key: str, payload: WeatherAdvisoryPayload, *, negative: bool) -> None:
    ttl = FAILURE_TTL_SECONDS if negative else SUCCESS_TTL_SECONDS
    _WEATHER_CACHE[key] = WeatherCacheEntry(
        payload=payload.model_copy(update={
            "cache_hit": False,
            "cache_negative_hit": False,
            "cache_ttl_seconds": ttl,
        }),
        expires_at=time.monotonic() + ttl,
        ttl_seconds=ttl,
        negative=negative,
    )


def disabled_payload() -> WeatherAdvisoryPayload:
    return WeatherAdvisoryPayload(
        status=STATUS_DISABLED,
        cache_ttl_seconds=0,
    )


def skipped_payload(
    status: str,
    *,
    city: str = "",
    adcode: str = "",
    days_requested: int = 0,
    lookup_attempted: bool = False,
    date_range_mismatch: bool = False,
) -> WeatherAdvisoryPayload:
    return WeatherAdvisoryPayload(
        status=status if status in WEATHER_STATUSES else STATUS_API_ERROR,
        city=city,
        adcode=adcode,
        days_requested=days_requested,
        days_authorized=0,
        days_out_of_range=days_requested if status == STATUS_DATE_OUT_OF_RANGE else 0,
        lookup_attempted=lookup_attempted,
        date_range_mismatch=date_range_mismatch,
        cache_ttl_seconds=FAILURE_TTL_SECONDS if lookup_attempted else 0,
    )


async def build_weather_advisory_payload(
    *,
    trip_request: TripRequest,
    user_text: str,
    now: datetime | None = None,
    client: Any | None = None,
) -> WeatherAdvisoryPayload:
    """Build Weather Advisory Payload without affecting the main itinerary."""
    settings = get_settings()
    if not getattr(settings, "weather_enrichment_enabled", False):
        return disabled_payload()

    api_key = str(getattr(settings, "amap_weather_api_key", "") or "").strip()
    if not api_key and client is None:
        return skipped_payload(STATUS_API_KEY_MISSING)

    start = resolve_start_date(
        trip_request=trip_request,
        user_text=user_text,
        now=now,
    )
    mismatch = date_range_mismatch(trip_request, start)
    if start is None:
        return skipped_payload(
            STATUS_NO_TRAVEL_DATE,
            date_range_mismatch=mismatch,
        )

    city = (trip_request.to_city or "").strip()
    adcode = city_to_adcode(city) or ""
    request_dates = requested_dates(start, trip_request.days)
    if not adcode:
        return skipped_payload(
            STATUS_CITY_UNRESOLVED,
            city=city,
            days_requested=len(request_dates),
            date_range_mismatch=mismatch,
        )

    cache_key = weather_cache_key(
        provider=WEATHER_PROVIDER,
        adcode=adcode,
        city=city,
        target_date=start.isoformat(),
        days_requested=len(request_dates),
    )
    cached = cache_get(cache_key)
    if cached is not None:
        if cached.days_requested == len(request_dates):
            return cached
        _WEATHER_CACHE.pop(cache_key, None)

    if client is None:
        from src.agents.amap_weather import AmapWeatherClient

        client = AmapWeatherClient(
            api_key=api_key,
            timeout_seconds=float(getattr(settings, "amap_weather_timeout", 5.0)),
        )

    try:
        forecasts = await client.fetch_daily_forecast(adcode=adcode)
    except Exception as exc:
        status = getattr(exc, "status", STATUS_API_ERROR)
        payload = skipped_payload(
            status,
            city=city,
            adcode=adcode,
            days_requested=len(request_dates),
            lookup_attempted=True,
            date_range_mismatch=mismatch,
        )
        cache_set(cache_key, payload, negative=True)
        return payload

    payload = build_payload_from_daily_forecasts(
        city=city,
        adcode=adcode,
        requested=request_dates,
        forecasts=forecasts,
        date_range_mismatch_value=mismatch,
    )
    cache_set(cache_key, payload, negative=not payload.is_ok())
    return payload


def resolve_start_date(
    *,
    trip_request: TripRequest,
    user_text: str,
    now: datetime | None = None,
) -> date | None:
    if trip_request.start_date:
        parsed = _parse_date_string(trip_request.start_date, now=now)
        if parsed is not None:
            return parsed
    return parse_travel_date_from_text(user_text, now=now)


def date_range_mismatch(trip_request: TripRequest, start: date | None) -> bool:
    if start is None or not trip_request.end_date:
        return False
    end = _parse_date_string(trip_request.end_date)
    if end is None:
        return False
    expected = start + timedelta(days=max(int(trip_request.days or 1), 1) - 1)
    return end != expected


def requested_dates(start: date, days: int) -> list[date]:
    total = max(int(days or 1), 1)
    return [start + timedelta(days=offset) for offset in range(total)]


def parse_travel_date_from_text(
    text: str,
    *,
    now: datetime | None = None,
) -> date | None:
    if not text:
        return None
    ref = now or datetime.now(ZoneInfo("Asia/Shanghai"))
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    if "后天" in text:
        return (ref + timedelta(days=2)).date()
    if "明天" in text:
        return (ref + timedelta(days=1)).date()
    if "今天" in text:
        return ref.date()
    match = DATE_RE.search(text)
    if match:
        return _safe_date(
            int(match.group("year")),
            int(match.group("month")),
            int(match.group("day")),
        )
    match = MONTH_DAY_RE.search(text)
    if match:
        month = int(match.group("month"))
        day = int(match.group("day"))
        parsed = _safe_date(ref.year, month, day)
        if parsed is not None and parsed < ref.date():
            parsed = _safe_date(ref.year + 1, month, day)
        return parsed
    return None


def _parse_date_string(value: str, *, now: datetime | None = None) -> date | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return parse_travel_date_from_text(value, now=now)


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def build_payload_from_daily_forecasts(
    *,
    city: str,
    adcode: str,
    requested: list[date],
    forecasts: list[dict[str, Any]],
    date_range_mismatch_value: bool = False,
) -> WeatherAdvisoryPayload:
    by_date = {
        str(item.get("date") or ""): item
        for item in forecasts
        if item.get("date")
    }
    days: list[WeatherAdvisoryDay] = []
    display_days: list[WeatherDisplayDay] = []
    out_of_range = 0
    available_count = 0
    for day_index, target in enumerate(requested, 1):
        item = by_date.get(target.isoformat())
        if item is None:
            out_of_range += 1
            continue
        available_count += 1
        conditions = classify_conditions(item)
        reminders = [REMINDER_TEMPLATES[name] for name in conditions][:2]
        display_days.append(_display_day_from_forecast(
            day_index=day_index,
            target=target,
            forecast=item,
            conditions=conditions,
            reminders=reminders,
        ))
        if reminders:
            days.append(WeatherAdvisoryDay(
                day_index=day_index,
                date=target.isoformat(),
                conditions=conditions,
                authorized_reminders=reminders,
            ))
    if available_count == 0:
        return WeatherAdvisoryPayload(
            status=STATUS_DATE_OUT_OF_RANGE,
            city=city,
            adcode=adcode,
            days_requested=len(requested),
            days_authorized=0,
            days_out_of_range=len(requested),
            lookup_attempted=True,
            date_range_mismatch=date_range_mismatch_value,
            cache_ttl_seconds=FAILURE_TTL_SECONDS,
        )
    return WeatherAdvisoryPayload(
        status=STATUS_OK,
        city=city,
        adcode=adcode,
        days_requested=len(requested),
        days_authorized=len(days),
        days_out_of_range=out_of_range,
        days=days,
        display_days=display_days,
        lookup_attempted=True,
        date_range_mismatch=date_range_mismatch_value,
        cache_ttl_seconds=SUCCESS_TTL_SECONDS,
    )


def classify_conditions(forecast: dict[str, Any]) -> list[str]:
    text = " ".join(
        str(forecast.get(key) or "")
        for key in (
            "dayweather",
            "nightweather",
            "weather",
            "daypower",
            "nightpower",
            "daywind",
            "nightwind",
        )
    )
    high = _as_int(forecast.get("daytemp") or forecast.get("high"))
    low = _as_int(forecast.get("nighttemp") or forecast.get("low"))
    conditions: list[str] = []
    if "雷" in text:
        conditions.append("thunderstorm")
    if any(marker in text for marker in ("暴雨", "大雨", "强降雨")):
        conditions.append("heavy_rain")
    elif "雨" in text:
        conditions.append("rain")
    if high is not None and high >= 32:
        conditions.append("heat")
    if low is not None and low <= 8:
        conditions.append("cold")
    if _wind_power_high(text):
        conditions.append("wind")
    return _dedupe(conditions)


def _display_day_from_forecast(
    *,
    day_index: int,
    target: date,
    forecast: dict[str, Any],
    conditions: list[str],
    reminders: list[str],
) -> WeatherDisplayDay:
    day_weather = str(
        forecast.get("dayweather") or forecast.get("weather") or ""
    ).strip()
    night_weather = str(forecast.get("nightweather") or "").strip()
    high = _as_int(forecast.get("daytemp") or forecast.get("high"))
    low = _as_int(forecast.get("nighttemp") or forecast.get("low"))
    temp_values = [value for value in (low, high) if value is not None]
    return WeatherDisplayDay(
        day=day_index,
        date=target.isoformat(),
        weather_text=_weather_text(day_weather, night_weather),
        temp_min_c=min(temp_values) if temp_values else None,
        temp_max_c=max(temp_values) if temp_values else None,
        wind_text=_wind_text(
            str(forecast.get("daywind") or "").strip(),
            str(forecast.get("nightwind") or "").strip(),
            str(forecast.get("daypower") or "").strip(),
            str(forecast.get("nightpower") or "").strip(),
        ),
        icon_code=_icon_code(
            " ".join(value for value in (day_weather, night_weather) if value),
            conditions,
        ),
        reminders=list(reminders[:2]),
    )


def _weather_text(day_weather: str, night_weather: str) -> str:
    if day_weather and night_weather and day_weather != night_weather:
        return f"{day_weather}转{night_weather}"
    return day_weather or night_weather


def _wind_text(
    day_wind: str,
    night_wind: str,
    day_power: str,
    night_power: str,
) -> str:
    wind = day_wind or night_wind
    power = day_power or night_power
    if not wind and not power:
        return ""
    wind = _normalize_wind(wind)
    power = _normalize_power(power)
    if wind and power:
        return f"{wind} {power}"
    return wind or power


def _normalize_wind(value: str) -> str:
    text = value.strip()
    if not text or text in {"无", "无风"}:
        return text
    if text.endswith("风"):
        return text
    return text + "风"


def _normalize_power(value: str) -> str:
    text = value.strip()
    if not text:
        return ""
    if text.endswith("级"):
        return text
    match = re.search(r"\d+", text)
    return f"{match.group(0)}级" if match else text


def _icon_code(weather_text: str, conditions: list[str]) -> str:
    if "thunderstorm" in conditions or "雷" in weather_text:
        return "thunderstorm"
    if "heavy_rain" in conditions or any(
        marker in weather_text for marker in ("暴雨", "大雨", "强降雨")
    ):
        return "heavy_rain"
    if "rain" in conditions or "雨" in weather_text:
        return "rain"
    if "雪" in weather_text:
        return "snow"
    if "晴" in weather_text and ("云" in weather_text or "阴" in weather_text):
        return "partly_cloudy"
    if "晴" in weather_text:
        return "sunny"
    if "云" in weather_text:
        return "cloudy"
    if "阴" in weather_text:
        return "overcast"
    return "unknown"


def _as_int(value: Any) -> int | None:
    match = re.search(r"-?\d+", str(value or ""))
    if not match:
        return None
    return int(match.group(0))


def _wind_power_high(text: str) -> bool:
    if "大风" in text or "强风" in text:
        return True
    for value in re.findall(r"(\d+)\s*级", text):
        if int(value) >= 5:
            return True
    return False


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def render_weather_prompt(payload: WeatherAdvisoryPayload | None) -> str:
    if payload is None:
        return ""
    return payload.prompt_text()


def apply_weather_advisory_to_text(
    plan_text: str,
    *,
    route_plan: RoutePlan,
    payload: WeatherAdvisoryPayload | None,
) -> tuple[str, list[dict[str, Any]]]:
    """Remove weather prose; frontend weather display is structured data."""
    cleaned, removed = remove_weather_lines(plan_text)
    result = re.sub(r"\n{3,}", "\n\n", cleaned)
    return result, removed


def remove_weather_lines(plan_text: str) -> tuple[str, list[dict[str, Any]]]:
    lines = (plan_text or "").splitlines()
    kept: list[str] = []
    actions: list[dict[str, Any]] = []
    for line in lines:
        if is_weather_line(line):
            actions.append({
                "action": "remove_weather_line",
                "reason": "unauthorized_or_repositioned_weather",
                "snippet": line.strip()[:180],
            })
            continue
        kept.append(line)
    return "\n".join(kept), actions


def is_weather_line(text: str) -> bool:
    stripped = (text or "").strip()
    if not stripped:
        return False
    if str(getattr(get_settings(), "writer_prompt_version", "") or "").strip() == "article":
        # v0.15.1 article: seasonal advice ("8月防晒") and conditions ("如果遇到阴天")
        # help; only a claim about the trip day's weather can contradict the card.
        return is_weather_forecast_claim(stripped)
    return any(marker in stripped for marker in WEATHER_LINE_MARKERS)


_WEATHER_STATE = re.compile(
    r"晴天|放晴|晴朗|多云|阴天|下雨|有雨|降雨|阵雨|雷雨|小雨|中雨|大雨|暴雨|雨天|"
    r"下雪|降雪|雪天|大雾|雾霾|台风|冰雹|高温预警")
_WEATHER_DAY = re.compile(
    r"当天|今天|明天|后天|这天|那天|这一天|当日|这几天|那几天|"
    r"周[一二三四五六日天]|星期[一二三四五六日天]|\d{1,2}\s*月\s*\d{1,2}\s*日|"
    r"第[一二三四五六七八九十\d]+天|预计|将会|会下|会有")
_WEATHER_CONDITION = re.compile(
    r"如果|若是|万一|遇到|遇上|碰上|的话|以防|备用|备着|防止|可能|容易|常有|多雨|雨季|季节|"
    r"一般|通常|往往|偶尔|时常|有时")


def is_weather_forecast_claim(text: str) -> bool:
    """A sentence that states the weather of a trip day, which only the weather card may do."""
    value = text or ""
    if "天气预报" in value or "天气提醒" in value:
        return True
    return bool(_WEATHER_STATE.search(value) and _WEATHER_DAY.search(value)
                and not _WEATHER_CONDITION.search(value))


def weather_review_issues(
    plans: list[Any],
    *,
    route_plans: list[RoutePlan],
    payload: WeatherAdvisoryPayload | None,
) -> list[Any]:
    from src.agents.generation_issues import GenerationIssue

    issues: list[GenerationIssue] = []
    for plan_index, plan in enumerate(plans, 1):
        route_plan = (
            route_plans[plan_index - 1]
            if plan_index - 1 < len(route_plans)
            else None
        )
        route_names_by_day = _route_names_by_day(route_plan)
        weather_lines = _weather_lines_with_context(plan.plan_text)
        for item in weather_lines:
            line = item["line"]
            day = item["day"]
            if any(marker in line for marker in WEATHER_DISCLAIMER_MARKERS):
                issues.append(_weather_issue(
                    "weather_disclaimer_leak",
                    plan_index=plan_index,
                    day=day,
                    snippet=line,
                    evidence="weather failure or unavailable disclaimer is forbidden",
                ))
                continue
            if any(marker in line for marker in WEATHER_ROUTE_DRIFT_MARKERS):
                issues.append(_weather_issue(
                    "weather_route_drift",
                    plan_index=plan_index,
                    day=day,
                    snippet=line,
                    evidence="weather must not suggest route changes or alternatives",
                ))
                continue
            names = [
                name for name in route_names_by_day.get(day or 0, [])
                if name and name in line
            ]
            if names:
                issues.append(_weather_issue(
                    "weather_poi_bound_claim",
                    plan_index=plan_index,
                    day=day,
                    names=names,
                    snippet=line,
                    evidence="weather reminder must not bind to a specific POI",
                ))
                continue
            issues.append(_weather_issue(
                "weather_unauthorized_claim",
                plan_index=plan_index,
                day=day,
                snippet=line,
                evidence="weather display must use /trip/results.weather; prose is not authorized",
            ))
    return issues


def _weather_issue(
    reason: str,
    *,
    plan_index: int,
    day: int | None,
    snippet: str,
    evidence: str,
    names: list[str] | None = None,
) -> Any:
    from src.agents.generation_issues import GenerationIssue

    return GenerationIssue(
        source="deterministic",
        category="REPAIR",
        publish_action="REPAIR_PLAN",
        reason=reason,
        plan_index=plan_index,
        day=day,
        names=names or [],
        snippet=snippet.strip()[:220],
        evidence=evidence,
    )


def _weather_lines_with_context(plan_text: str) -> list[dict[str, Any]]:
    heading = re.compile(
        r"(?im)^\s*(?:#{1,6}\s*)?(?:\*\*)?\s*"
        r"(?:Day\s*(\d+)|第\s*(\d+)\s*天)[^\n]*?(?:\*\*)?\s*$"
    )
    lines = (plan_text or "").splitlines()
    current_day: int | None = None
    previous_non_empty_was_heading = False
    result: list[dict[str, Any]] = []
    for line in lines:
        match = heading.match(line)
        if match:
            current_day = int(match.group(1) or match.group(2))
            previous_non_empty_was_heading = True
            if is_weather_line(line):
                result.append({
                    "day": current_day,
                    "line": line.strip(),
                    "is_day_notice": False,
                })
            continue
        if not line.strip():
            continue
        if is_weather_line(line):
            is_notice = (
                previous_non_empty_was_heading
                and line.strip().startswith("天气提醒：")
            )
            result.append({
                "day": current_day,
                "line": line.strip(),
                "is_day_notice": is_notice,
            })
        previous_non_empty_was_heading = False
    return result


def _route_names_by_day(route_plan: RoutePlan | None) -> dict[int, list[str]]:
    if route_plan is None:
        return {}
    return {
        day.day: [place.name for place in day.places]
        for day in route_plan.day_groups
    }
