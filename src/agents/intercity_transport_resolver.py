"""Resolve direct trains from 12306 and flight fares from Variflight."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal
from zoneinfo import ZoneInfo

import httpx

from src.agents.intercity_transport_static import (
    STATIC_TRANSPORT_VERSION,
    get_static_fallback,
)
from src.agents.schema import (
    FLIGHT_FARE_PENDING,
    TrainOption,
    TransportModeSummary,
    TransportSuggestion,
    TripRequest,
)
from src.agents.variflight import CITY_CODES, FlightLookup, query_flight_prices
from src.config import get_settings
from src.jobs.city_store import resolve_city


logger = logging.getLogger(__name__)

_SECRET_QUERY_RE = re.compile(
    r"([?&](?:key|api_key)=)[^&\s\"']+",
    flags=re.IGNORECASE,
)


def _redact_provider_log_value(value: Any) -> Any:
    if isinstance(value, str):
        return _SECRET_QUERY_RE.sub(r"\1<redacted>", value)
    if isinstance(value, bytes):
        text_value = value.decode("utf-8", errors="replace")
        return _SECRET_QUERY_RE.sub(
            r"\1<redacted>",
            text_value,
        ).encode("utf-8")
    if isinstance(value, httpx.URL):
        return _SECRET_QUERY_RE.sub(r"\1<redacted>", str(value))
    if isinstance(value, tuple):
        return tuple(_redact_provider_log_value(item) for item in value)
    if isinstance(value, list):
        return [_redact_provider_log_value(item) for item in value]
    if isinstance(value, dict):
        return {
            key: (
                "<redacted>"
                if str(key).lower() in {"key", "api_key"}
                else _redact_provider_log_value(item)
            )
            for key, item in value.items()
        }
    return value


class _ProviderSecretFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = _redact_provider_log_value(record.getMessage())
        record.args = ()
        return True


def _harden_provider_http_logging() -> None:
    """Prevent provider query credentials from reaching library HTTP logs."""

    for logger_name in ("httpx", "httpcore"):
        provider_logger = logging.getLogger(logger_name)
        if provider_logger.level == logging.NOTSET or provider_logger.level < logging.WARNING:
            provider_logger.setLevel(logging.WARNING)
        if not any(
            isinstance(item, _ProviderSecretFilter)
            for item in provider_logger.filters
        ):
            provider_logger.addFilter(_ProviderSecretFilter())


_harden_provider_http_logging()

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
TRANSPORT_CACHE_VERSION = "v2"
TRANSPORT_CACHE_PREFIX = f"transport:{TRANSPORT_CACHE_VERSION}"
STATION_MAP_CACHE_KEY = "transport:station-map:v1"
REDIS_OPERATION_TIMEOUT_SECONDS = 0.5

STATION_JS_URL = (
    "https://kyfw.12306.cn/otn/resources/js/framework/station_name.js"
)
LEFT_TICKET_INIT_URL = "https://kyfw.12306.cn/otn/leftTicket/init"
LEFT_TICKET_URL = "https://kyfw.12306.cn/otn/leftTicket/queryZ"
TICKET_PRICE_URL = "https://kyfw.12306.cn/otn/leftTicket/queryTicketPrice"

PROVIDER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
    ),
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": LEFT_TICKET_INIT_URL,
}

SEAT_INDEX = {
    "商务座": 32,
    "一等座": 31,
    "二等座": 30,
    "无座": 26,
    "软卧": 23,
    "硬卧": 28,
    "硬座": 29,
    "动卧": 33,
}

DateWindow = Literal["static_only", "dated", "reference"]
HIGH_SPEED_PREFIXES = ("G", "D", "C")
HIGH_SPEED_ONLY_MAX_MINUTES = 180
ORDINARY_TRAIN_MAX_MINUTES = 720
TRAIN_REFERENCE_DAYS_AHEAD = 14

_redis_client: Any = None
_station_map_memory: tuple[float, dict[str, str]] | None = None
_TRAIN_NUMBER_RE = re.compile(r"[A-Z]?[0-9]+")


class ProviderUnavailable(RuntimeError):
    """A retryable provider transport failure."""


class ProviderResponseError(RuntimeError):
    """A non-retryable provider HTTP response failure."""


@dataclass
class _NoHighSpeedTrains:
    """12306 had no valid G/D/C schedules, with any eligible ordinary trains."""

    ordinary_trains: list[dict[str, Any]] = field(default_factory=list)
    station_map: dict[str, str] = field(default_factory=dict)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _remaining(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())


def _request_timeout(deadline: float, provider_timeout: float) -> float:
    remaining = _remaining(deadline)
    if remaining <= 0:
        raise asyncio.TimeoutError("transport resolver deadline exhausted")
    return min(max(float(provider_timeout), 0.01), remaining)


async def _get(
    client: httpx.AsyncClient,
    url: str,
    *,
    deadline: float,
    provider_timeout: float,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    timeout = _request_timeout(deadline, provider_timeout)
    try:
        response = await asyncio.wait_for(
            client.get(
                url,
                params=params,
                headers=headers,
                timeout=timeout,
            ),
            timeout=timeout,
        )
    except (asyncio.TimeoutError, httpx.TimeoutException, httpx.TransportError) as exc:
        raise ProviderUnavailable(type(exc).__name__) from exc
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise ProviderResponseError(f"http_{response.status_code}") from exc
    return response


def _date_window(
    start_date: str | None,
    *,
    today: date | None = None,
) -> DateWindow:
    """Classify provider availability using the Asia/Shanghai local date."""

    if not start_date or not str(start_date).strip():
        return "static_only"
    try:
        requested = date.fromisoformat(str(start_date).strip())
    except ValueError:
        return "static_only"
    local_today = today or datetime.now(SHANGHAI_TZ).date()
    days_ahead = (requested - local_today).days
    if days_ahead < 0:
        return "static_only"
    if days_ahead <= 15:
        return "dated"
    return "reference"


async def _canonical_city(value: str | None) -> str:
    """Resolve via travel_city/travel_city_alias, falling back to trimmed input."""

    trimmed = str(value or "").strip()
    if not trimmed:
        return ""
    try:
        resolved = await resolve_city(trimmed)
    except Exception as exc:
        logger.info("transport city canonicalization unavailable type=%s", type(exc).__name__)
        return trimmed
    canonical = str(getattr(resolved, "canonical_name", "") or "").strip()
    return canonical or trimmed


async def canonicalize_transport_city(value: str | None) -> str:
    """Expose the resolver's authoritative city identity to source adapters."""

    return await _canonical_city(value)


def _cache_key(from_city: str, to_city: str, query_date: str | None) -> str:
    return f"{TRANSPORT_CACHE_PREFIX}:{from_city}:{to_city}:{query_date or 'no-date'}"


async def _get_redis_client() -> Any:
    global _redis_client
    settings = get_settings()
    redis_url = str(settings.redis_url or "").strip()
    if not redis_url:
        return None
    if _redis_client is not None:
        return _redis_client
    try:
        from redis.asyncio import Redis

        _redis_client = Redis.from_url(
            redis_url,
            decode_responses=True,
            socket_connect_timeout=REDIS_OPERATION_TIMEOUT_SECONDS,
            socket_timeout=REDIS_OPERATION_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        logger.info("transport cache client init failed type=%s", type(exc).__name__)
        return None
    return _redis_client


async def _read_cache(key: str) -> TransportSuggestion | None:
    try:
        client = await _get_redis_client()
        if client is None:
            return None
        raw = await asyncio.wait_for(
            client.get(key),
            timeout=REDIS_OPERATION_TIMEOUT_SECONDS,
        )
        if not raw:
            return None
        return TransportSuggestion.model_validate_json(raw)
    except Exception as exc:
        logger.info("transport cache read missed type=%s", type(exc).__name__)
        return None


async def _write_cache(
    key: str,
    suggestion: TransportSuggestion,
    *,
    ttl_seconds: int,
) -> None:
    try:
        client = await _get_redis_client()
        if client is None:
            return
        payload = suggestion.model_dump(mode="json")
        if suggestion.direct_service_absent:
            payload["direct_service_absent"] = True
        if suggestion.flight_fare_pending:
            payload["flight_fare_pending"] = True
        await asyncio.wait_for(
            client.setex(key, int(ttl_seconds), json.dumps(payload, ensure_ascii=False)),
            timeout=REDIS_OPERATION_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        logger.info("transport cache write skipped type=%s", type(exc).__name__)


async def _read_station_map_cache() -> dict[str, str] | None:
    try:
        client = await _get_redis_client()
        if client is None:
            return None
        raw = await asyncio.wait_for(
            client.get(STATION_MAP_CACHE_KEY),
            timeout=REDIS_OPERATION_TIMEOUT_SECONDS,
        )
        payload = json.loads(raw) if raw else None
        if not isinstance(payload, dict):
            return None
        station_map = {
            str(name): str(code)
            for name, code in payload.items()
            if str(name).strip() and str(code).strip()
        }
        return station_map or None
    except Exception as exc:
        logger.info("transport station cache read missed type=%s", type(exc).__name__)
        return None


async def _write_station_map_cache(station_map: dict[str, str]) -> None:
    settings = get_settings()
    ttl_seconds = max(1, int(settings.transport_station_map_ttl_hours * 3600))
    try:
        client = await _get_redis_client()
        if client is None:
            return
        await asyncio.wait_for(
            client.setex(
                STATION_MAP_CACHE_KEY,
                ttl_seconds,
                json.dumps(station_map, ensure_ascii=False),
            ),
            timeout=REDIS_OPERATION_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        logger.info("transport station cache write skipped type=%s", type(exc).__name__)


async def _warm_12306_session(
    client: httpx.AsyncClient,
    *,
    deadline: float,
    timeout: float,
) -> None:
    await _get(
        client,
        LEFT_TICKET_INIT_URL,
        deadline=deadline,
        provider_timeout=timeout,
        headers=PROVIDER_HEADERS,
    )


def _parse_station_map(text: str) -> dict[str, str]:
    station_map: dict[str, str] = {}
    for segment in str(text or "").split("@"):
        parts = segment.split("|")
        if len(parts) >= 3 and parts[1].strip() and parts[2].strip():
            station_map[parts[1].strip()] = parts[2].strip()
    return station_map


async def _load_station_map(
    client: httpx.AsyncClient,
    *,
    deadline: float,
    timeout: float,
) -> dict[str, str]:
    global _station_map_memory
    ttl_seconds = max(1, int(get_settings().transport_station_map_ttl_hours * 3600))
    now = time.monotonic()
    if _station_map_memory is not None:
        cached_at, station_map = _station_map_memory
        if now - cached_at < ttl_seconds:
            return station_map
    cached = await _read_station_map_cache()
    if cached:
        _station_map_memory = (now, cached)
        return cached
    response = await _get(
        client,
        STATION_JS_URL,
        deadline=deadline,
        provider_timeout=timeout,
        headers=PROVIDER_HEADERS,
    )
    station_map = _parse_station_map(response.text)
    if not station_map:
        raise ProviderUnavailable("station_map_empty")
    _station_map_memory = (now, station_map)
    await _write_station_map_cache(station_map)
    return station_map


def _station_code_for_city(station_map: dict[str, str], city: str) -> str | None:
    exact = station_map.get(city)
    if exact:
        return exact
    candidates = [
        name for name in station_map
        if name.startswith(city) or city in name
    ]
    if not candidates:
        return None
    suffix_priority = ("南", "东", "虹桥", "西", "北")
    candidates.sort(key=lambda name: (
        next(
            (index for index, suffix in enumerate(suffix_priority) if name.endswith(suffix)),
            len(suffix_priority),
        ),
        len(name),
        name,
    ))
    return station_map[candidates[0]]


def _duration_minutes(raw: str) -> int:
    parts = str(raw or "").split(":")
    if len(parts) != 2:
        return 0
    try:
        return int(parts[0]) * 60 + int(parts[1])
    except ValueError:
        return 0


def parse_train_list(
    raw_data: dict[str, Any],
    *,
    include_ordinary: bool = False,
) -> list[dict[str, Any]]:
    """Parse direct 12306 schedules, keeping G/D/C trains by default."""

    data = raw_data.get("data") if isinstance(raw_data, dict) else None
    rows = data.get("result") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return []
    trains: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, str):
            continue
        parts = row.split("|")
        if len(parts) < 34:
            continue
        display_no = parts[3].strip()
        duration = _duration_minutes(parts[10])
        valid_train_number = display_no.startswith(HIGH_SPEED_PREFIXES) or (
            include_ordinary and _TRAIN_NUMBER_RE.fullmatch(display_no)
        )
        if not valid_train_number or duration <= 0:
            continue
        seats = {
            label: (parts[index].strip() if index < len(parts) else "")
            for label, index in SEAT_INDEX.items()
        }
        trains.append({
            "train_no": display_no,
            "train_no_internal": parts[2].strip(),
            "from_station_code": parts[6].strip(),
            "to_station_code": parts[7].strip(),
            "depart_time": parts[8].strip(),
            "arrive_time": parts[9].strip(),
            "duration_min": duration,
            "date": parts[13].strip() if len(parts) > 13 else "",
            "can_web_buy": parts[11].strip() if len(parts) > 11 else "",
            "seats": seats,
            "second_class_raw": seats.get("二等座", ""),
            "from_station_no": parts[16].strip() if len(parts) > 16 else "",
            "to_station_no": parts[17].strip() if len(parts) > 17 else "",
            "seat_types": parts[35].strip() if len(parts) > 35 else "",
        })
    trains.sort(key=lambda item: (item["duration_min"], item["depart_time"], item["train_no"]))
    return trains


def _second_class_state(value: Any) -> Literal["available", "sold_out", "unknown"]:
    normalized = str(value or "").strip()
    if normalized == "有":
        return "available"
    if normalized.isdigit():
        return "available" if int(normalized) > 0 else "sold_out"
    if normalized in {"无", "--", "候补"}:
        return "sold_out"
    return "unknown"


def recommend_trains(
    trains: list[dict[str, Any]],
    *,
    top_n: int = 5,
) -> tuple[list[dict[str, Any]], Literal[
    "available_at_query", "sold_out_at_query", "unknown"
]]:
    """Prefer purchasable second class seats without hiding all-sold schedules."""

    available = [
        train for train in trains
        if _second_class_state(train.get("second_class_raw")) == "available"
    ]
    states = [_second_class_state(train.get("second_class_raw")) for train in trains]
    if available:
        source = available
        status = "available_at_query"
    elif trains and states and all(state == "sold_out" for state in states):
        source = trains
        status = "sold_out_at_query"
    else:
        source = trains
        status = "unknown"
    ranked = sorted(
        source,
        key=lambda train: (
            train.get("duration_min") or math.inf,
            0 if 7 <= int(str(train.get("depart_time") or "12:00")[:2]) <= 21 else 1,
            str(train.get("depart_time") or ""),
            str(train.get("train_no") or ""),
        ),
    )
    return ranked[:top_n], status


def _parse_price_value(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return round(float(value), 2)
    raw_value = str(value).strip()
    explicit_major_unit = (
        "¥" in raw_value
        or "￥" in raw_value
        or "." in raw_value
    )
    raw = raw_value.replace("¥", "").replace("￥", "")
    if not raw or raw == "--":
        return None
    try:
        number = float(raw)
    except ValueError:
        return None
    # Current queryTicketPrice responses use values such as ``¥661.0``.
    # Older digit-only responses encode tenths of a yuan (for example 5530).
    if not explicit_major_unit:
        number /= 10.0
    return round(number, 2)


async def _query_ticket_price(
    client: httpx.AsyncClient,
    train: dict[str, Any],
    query_date: str,
    *,
    deadline: float,
    timeout: float,
) -> dict[str, float]:
    params = {
        "train_no": train.get("train_no_internal", ""),
        "from_station_no": train.get("from_station_no", ""),
        "to_station_no": train.get("to_station_no", ""),
        "seat_types": train.get("seat_types", ""),
        "train_date": query_date,
    }
    try:
        response = await _get(
            client,
            TICKET_PRICE_URL,
            deadline=deadline,
            provider_timeout=timeout,
            params=params,
            headers=PROVIDER_HEADERS,
        )
        payload = response.json()
    except (
        ProviderUnavailable,
        ProviderResponseError,
        asyncio.TimeoutError,
        ValueError,
    ):
        return {}
    price_data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(price_data, dict):
        return {}
    # A9 is business class and A4 is soft sleeper.
    seat_codes = {
        "O": "second_class", "M": "first_class",
        "A1": "hard_seat", "A3": "hard_sleeper",
    }
    result: dict[str, float] = {}
    for code, seat in seat_codes.items():
        price = _parse_price_value(price_data.get(code))
        if price is not None and math.isfinite(price) and price > 0:
            result[seat] = price
    return result


def _number_text(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:.2f}".rstrip("0").rstrip(".")


def _aggregate_price_range(
    prices: list[float],
    *,
    fallback: str,
) -> tuple[str, Literal["realtime", "static_reference"]]:
    if not prices:
        return fallback, "static_reference"
    return (
        f"¥{_number_text(min(prices))}-{_number_text(max(prices))}",
        "realtime",
    )


def _train_option(
    train: dict[str, Any],
    fare: dict[str, float],
    station_names: dict[str, str],
    from_city: str,
    to_city: str,
) -> TrainOption:
    return TrainOption(
        train_no=train["train_no"],
        departure_time=train["depart_time"],
        arrival_time=train["arrive_time"],
        duration_minutes=train["duration_min"],
        departure_station=station_names.get(train["from_station_code"], from_city),
        arrival_station=station_names.get(train["to_station_code"], to_city),
        second_class_price=fare.get("second_class"),
        first_class_price=fare.get("first_class"),
    )


async def _query_ordinary_train_prices(
    trains: list[dict[str, Any]],
    query_date: str,
    *,
    deadline: float,
) -> list[dict[str, float]]:
    timeout = float(get_settings().transport_12306_timeout_seconds)
    parallel_requests = asyncio.Semaphore(5)
    async with httpx.AsyncClient(verify=True, follow_redirects=True) as client:
        await _warm_12306_session(client, deadline=deadline, timeout=timeout)

        async def fetch(train: dict[str, Any]) -> dict[str, float]:
            async with parallel_requests:
                return await _query_ticket_price(
                    client, train, query_date, deadline=deadline, timeout=timeout,
                )

        return await asyncio.gather(*(fetch(train) for train in trains))


def _ordinary_train_result(
    result: _NoHighSpeedTrains,
    fares: list[dict[str, float]],
    from_city: str,
    to_city: str,
) -> dict[str, Any]:
    trains = result.ordinary_trains
    station_names = {code: name for name, code in result.station_map.items()}
    hard_seats = [fare["hard_seat"] for fare in fares if "hard_seat" in fare]
    hard_sleepers = [fare["hard_sleeper"] for fare in fares if "hard_sleeper" in fare]
    price_bounds = []
    if hard_seats:
        lower = min(hard_seats)
        price_bounds = [lower, max(lower, max(hard_sleepers or hard_seats))]
    price_range, price_source = _aggregate_price_range(price_bounds, fallback="以购票平台为准")
    return {
        "train_class": "ordinary",
        "min_duration_minutes": trains[0]["duration_min"],
        "price_range": price_range,
        "price_source": price_source,
        "daily_count": len(trains),
        "availability_checked_at": _utc_now_iso(),
        "top_options": [
            _train_option(train, {}, station_names, from_city, to_city)
            for train in trains[:5]
        ],
    }


async def _query_12306_once(
    from_city: str,
    to_city: str,
    query_date: str,
    *,
    deadline: float,
    timeout: float,
    on_train_duration: Callable[[int], None] | None = None,
) -> dict[str, Any] | _NoHighSpeedTrains | None:
    async with httpx.AsyncClient(verify=True, follow_redirects=True) as client:
        await _warm_12306_session(client, deadline=deadline, timeout=timeout)
        station_map = await _load_station_map(client, deadline=deadline, timeout=timeout)
        from_code = _station_code_for_city(station_map, from_city)
        to_code = _station_code_for_city(station_map, to_city)
        if not from_code or not to_code:
            return None
        response = await _get(
            client,
            LEFT_TICKET_URL,
            deadline=deadline,
            provider_timeout=timeout,
            params={
                "leftTicketDTO.train_date": query_date,
                "leftTicketDTO.from_station": from_code,
                "leftTicketDTO.to_station": to_code,
                "purpose_codes": "ADULT",
            },
            headers=PROVIDER_HEADERS,
        )
        try:
            payload = response.json()
        except ValueError:
            return None
        if not isinstance(payload, dict):
            return None
        if payload.get("httpstatus") not in {None, 200} or payload.get("status") is False:
            return None
        data = payload.get("data")
        rows = data.get("result") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            return None
        schedules = parse_train_list(payload, include_ordinary=True)
        if rows and not schedules:
            return None
        trains = [train for train in schedules if train["train_no"].startswith(HIGH_SPEED_PREFIXES)]
        if not trains:
            logger.info(
                "12306 transport lookup has no G/D/C trains route=%s->%s",
                from_city,
                to_city,
            )
            return _NoHighSpeedTrains(
                ordinary_trains=[
                    train for train in schedules
                    if train["duration_min"] <= ORDINARY_TRAIN_MAX_MINUTES
                ],
                station_map=station_map,
            )
        top, availability = recommend_trains(trains)
        if not top:
            return None
        min_duration = min(train["duration_min"] for train in top)
        if on_train_duration is not None:
            on_train_duration(min_duration)
        prices = await asyncio.gather(*[
            _query_ticket_price(
                client,
                train,
                query_date,
                deadline=deadline,
                timeout=timeout,
            )
            for train in top
        ])

    station_names = {code: name for name, code in station_map.items()}
    options = [
        _train_option(train, fare, station_names, from_city, to_city)
        for train, fare in zip(top, prices)
    ]
    second_prices = [fare["second_class"] for fare in prices if "second_class" in fare]
    static_entry = get_static_fallback(from_city, to_city) or {}
    price_range, price_source = _aggregate_price_range(
        second_prices,
        fallback=str(static_entry.get("train_price") or "以购票平台为准"),
    )
    return {
        "train_class": "high_speed",
        "min_duration_minutes": min_duration,
        "price_range": price_range,
        "price_source": price_source,
        "daily_count": len(trains),
        "availability_status": availability,
        "availability_checked_at": _utc_now_iso(),
        "top_options": options,
    }


async def _query_12306_with_deadline(
    from_city: str,
    to_city: str,
    query_date: str,
    *,
    deadline: float,
    on_train_duration: Callable[[int], None] | None = None,
) -> dict[str, Any] | _NoHighSpeedTrains | None:
    settings = get_settings()
    attempts = max(0, int(settings.transport_12306_retry_count)) + 1
    for attempt in range(attempts):
        if _remaining(deadline) <= 0:
            return None
        try:
            return await _query_12306_once(
                from_city,
                to_city,
                query_date,
                deadline=deadline,
                timeout=float(settings.transport_12306_timeout_seconds),
                on_train_duration=on_train_duration,
            )
        except ProviderResponseError as exc:
            logger.info(
                "12306 transport lookup rejected type=%s",
                type(exc).__name__,
            )
            return None
        except (ProviderUnavailable, asyncio.TimeoutError) as exc:
            logger.info(
                "12306 transport lookup failed attempt=%s type=%s",
                attempt + 1,
                type(exc).__name__,
            )
            if attempt + 1 >= attempts or _remaining(deadline) <= 0.05:
                return None
    return None


async def _query_flights_with_deadline(
    from_city: str,
    to_city: str,
    query_date: str,
    *,
    deadline: float,
) -> FlightLookup:
    settings = get_settings()
    return await query_flight_prices(
        CITY_CODES[from_city],
        CITY_CODES[to_city],
        query_date,
        api_key=settings.variflight_api_key.strip(),
        timeout=_request_timeout(deadline, settings.transport_flight_timeout_seconds),
    )


def _need_flight(
    train: dict[str, Any] | None,
    static_entry: dict[str, Any] | None,
) -> bool:
    if train is not None:
        return int(train.get("min_duration_minutes") or 0) > HIGH_SPEED_ONLY_MAX_MINUTES
    if static_entry is None or static_entry.get("train_hours") is None:
        return True
    try:
        return float(static_entry["train_hours"]) * 60 > HIGH_SPEED_ONLY_MAX_MINUTES
    except (TypeError, ValueError):
        return True


def _static_duration(entry: dict[str, Any], key: str) -> int | None:
    try:
        return max(1, round(float(entry[key]) * 60))
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def _realtime_train_mode(result: dict[str, Any]) -> TransportModeSummary:
    return TransportModeSummary(
        mode="train",
        min_duration_minutes=int(result["min_duration_minutes"]),
        price_range=str(result["price_range"]),
        price_source=result.get("price_source", "static_reference"),
        daily_count=int(result.get("daily_count") or 0),
        data_source="realtime",
        availability_status=result.get("availability_status", "unknown"),
        availability_checked_at=result.get("availability_checked_at"),
        top_options=result.get("top_options") or [],
        train_class=result.get("train_class"),
        reference_date=result.get("reference_date"),
    )


def _assemble(
    from_city: str,
    to_city: str,
    query_date: str | None,
    train: dict[str, Any] | None,
    flight: TransportModeSummary | None,
    static_entry: dict[str, Any] | None,
    *,
    flight_fare_pending: bool = False,
) -> TransportSuggestion | None:
    modes: list[TransportModeSummary] = []
    if train is not None:
        modes.append(_realtime_train_mode(train))
    elif static_entry is not None:
        duration = _static_duration(static_entry, "train_hours")
        if duration is not None:
            modes.append(TransportModeSummary(
                mode="train",
                min_duration_minutes=duration,
                price_range=str(static_entry.get("train_price") or "以购票平台为准"),
                price_source="static_reference",
                daily_count=0,
                data_source="static_fallback",
                train_class="high_speed",
            ))
    if flight is not None:
        modes.append(flight)

    if not modes:
        return None
    sources = {mode.data_source for mode in modes}
    if sources == {"realtime"}:
        source = "realtime"
    elif sources == {"static_fallback"}:
        source = "static_fallback"
    else:
        source = "mixed"
    return TransportSuggestion(
        from_city=from_city,
        to_city=to_city,
        query_date=query_date,
        modes=modes,
        source=source,
        cached_at=_utc_now_iso(),
        flight_fare_pending=flight_fare_pending,
    )


def _assemble_static_only(
    from_city: str,
    to_city: str,
) -> TransportSuggestion | None:
    entry = get_static_fallback(from_city, to_city)
    if entry is None:
        return None
    suggestion = _assemble(from_city, to_city, None, None, None, entry)
    if suggestion is None:
        return None
    return suggestion.model_copy(update={"cached_at": None})


async def _await_until_deadline(task: asyncio.Task, deadline: float) -> Any:
    if task.done():
        try:
            return task.result()
        except (asyncio.CancelledError, Exception):
            return None
    remaining = _remaining(deadline)
    if remaining <= 0:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return None
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=remaining)
    except asyncio.CancelledError:
        if task.cancelled():
            return None
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise
    except asyncio.TimeoutError:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return None
    except Exception:
        await asyncio.gather(task, return_exceptions=True)
        return None


async def _cancel_unfinished(task: asyncio.Task | None) -> None:
    if task is None:
        return
    if not task.done():
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def resolve_transport(trip_request: TripRequest) -> TransportSuggestion | None:
    """Resolve an optional intercity suggestion under one shared budget."""

    settings = get_settings()
    if not bool(settings.intercity_transport_enabled):
        return None
    deadline = time.monotonic() + max(
        0.01,
        float(settings.transport_resolver_total_budget_seconds),
    )
    raw_from_city = str(trip_request.from_city or "").strip()
    raw_to_city = str(trip_request.to_city or "").strip()
    try:
        from_city, to_city = await asyncio.wait_for(
            asyncio.gather(
                _canonical_city(raw_from_city),
                _canonical_city(raw_to_city),
            ),
            timeout=_request_timeout(
                deadline,
                float(settings.transport_resolver_total_budget_seconds),
            ),
        )
    except Exception as exc:
        logger.info(
            "transport city canonicalization fell back type=%s",
            type(exc).__name__,
        )
        from_city, to_city = raw_from_city, raw_to_city
    if not from_city or not to_city or from_city == to_city:
        return None

    today = datetime.now(SHANGHAI_TZ).date()
    window = _date_window(trip_request.start_date, today=today)
    if window == "static_only":
        suggestion = _assemble_static_only(from_city, to_city)
        if suggestion is not None:
            logger.info(
                "transport static fallback version=%s route=%s->%s",
                STATIC_TRANSPORT_VERSION,
                from_city,
                to_city,
            )
        return suggestion

    query_date = str(trip_request.start_date or "").strip()
    reference_date = (
        today + timedelta(days=TRAIN_REFERENCE_DAYS_AHEAD)
        if window == "reference" else None
    )
    train_date = reference_date.isoformat() if reference_date else query_date
    key = _cache_key(from_city, to_city, query_date)
    cached = await _await_until_deadline(
        asyncio.create_task(_read_cache(key)),
        deadline,
    )
    if cached is not None:
        return cached

    static_entry = get_static_fallback(from_city, to_city)
    flight_supported = from_city in CITY_CODES and to_city in CITY_CODES
    flight_task: asyncio.Task[FlightLookup] | None = None
    ordinary_fares_task: asyncio.Task | None = None
    flight_lookup: FlightLookup | None = None
    train_duration: int | None = None

    def start_flight() -> None:
        nonlocal flight_task
        if flight_supported and flight_task is None and _remaining(deadline) > 0:
            flight_task = asyncio.create_task(_query_flights_with_deadline(
                from_city, to_city, query_date, deadline=deadline,
            ))

    def on_train_duration(minutes: int) -> None:
        nonlocal train_duration
        train_duration = minutes
        if minutes > HIGH_SPEED_ONLY_MAX_MINUTES:
            start_flight()

    try:
        train_query = await _await_until_deadline(
            asyncio.create_task(_query_12306_with_deadline(
                from_city,
                to_city,
                train_date,
                deadline=deadline,
                on_train_duration=on_train_duration,
            )),
            deadline,
        )
        no_high_speed = None
        if isinstance(train_query, _NoHighSpeedTrains):
            no_high_speed = train_query
            # A usable schedule response supersedes historical route assumptions.
            static_entry = None
            train = None
        else:
            train = train_query

        need_flight = (
            train_duration > HIGH_SPEED_ONLY_MAX_MINUTES if train_duration is not None
            else _need_flight(train, static_entry)
        )
        if need_flight:
            start_flight()
            if no_high_speed is not None and no_high_speed.ordinary_trains:
                # Collect fares under the same deadline while the flight lookup runs.
                ordinary_fares_task = asyncio.create_task(_query_ordinary_train_prices(
                    no_high_speed.ordinary_trains,
                    train_date,
                    deadline=deadline,
                ))
            if flight_task is not None:
                flight_lookup = await _await_until_deadline(flight_task, deadline)
            if ordinary_fares_task is not None and no_high_speed is not None and (
                flight_lookup is None or flight_lookup.status != "found"
            ):
                fares = await _await_until_deadline(ordinary_fares_task, deadline) or []
                train = _ordinary_train_result(no_high_speed, fares, from_city, to_city)
    finally:
        await _cancel_unfinished(flight_task)
        await _cancel_unfinished(ordinary_fares_task)

    if train is not None and reference_date is not None:
        train = {**train, "reference_date": reference_date, "availability_status": "unknown"}

    flight = flight_lookup.mode if flight_lookup is not None else None
    no_direct_flight = flight_lookup is not None and flight_lookup.status == "no_direct_service"
    flight_lookup_failed = flight_lookup is None or flight_lookup.status == "failed"
    flight_fare_pending = (
        need_flight and flight_supported and flight is None and no_high_speed is None
    )
    if flight_fare_pending and flight_lookup_failed and static_entry is not None:
        duration = _static_duration(static_entry, "flight_hours")
        if duration is not None:
            flight = TransportModeSummary(
                mode="flight",
                min_duration_minutes=duration,
                price_range=FLIGHT_FARE_PENDING,
                price_source="static_reference",
                daily_count=0,
                data_source="static_fallback",
            )
    suggestion = _assemble(
        from_city,
        to_city,
        query_date,
        train,
        flight,
        static_entry,
        flight_fare_pending=flight_fare_pending and flight is None,
    )
    if suggestion is None and no_high_speed is not None and no_direct_flight:
        suggestion = TransportSuggestion(
            from_city=from_city,
            to_city=to_city,
            query_date=query_date,
            source="realtime",
            direct_service_absent=True,
        )
    if (
        suggestion is not None
        and _remaining(deadline) > 0
        and not (need_flight and flight_supported and flight_lookup_failed)
    ):
        await _await_until_deadline(
            asyncio.create_task(_write_cache(
                key,
                suggestion,
                ttl_seconds=int(settings.transport_cache_ttl_seconds),
            )),
            deadline,
        )
    return suggestion


__all__ = [
    "canonicalize_transport_city",
    "parse_train_list",
    "recommend_trains",
    "resolve_transport",
]
