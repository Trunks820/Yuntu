"""Single-call Variflight city-to-city schedule and economy fare lookup."""

from __future__ import annotations

import ast
import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from statistics import median
from typing import Any, Literal
from zoneinfo import ZoneInfo

import httpx

from src.agents.schema import FLIGHT_FARE_PENDING, FlightOption, TransportModeSummary


logger = logging.getLogger(__name__)
VARIFLIGHT_MCP_URL = "https://ai.variflight.com/servers/aviation/mcp"
_SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
CITY_CODES: dict[str, str] = {
    "北京": "BJS", "上海": "SHA", "成都": "CTU", "西安": "SIA", "重庆": "CKG",
    "深圳": "SZX", "广州": "CAN", "杭州": "HGH", "武汉": "WUH", "南京": "NKG",
    "昆明": "KMG", "丽江": "LJG", "大理": "DLU", "三亚": "SYX", "厦门": "XMN",
    "青岛": "TAO", "桂林": "KWL", "长沙": "CSX", "张家界": "DYG", "福州": "FOC",
    "泉州": "JJN", "海口": "HAK", "天津": "TSN", "郑州": "CGO", "合肥": "HFE",
    "济南": "TNA", "南昌": "KHN", "南宁": "NNG", "贵阳": "KWE", "兰州": "LHW",
    "哈尔滨": "HRB", "沈阳": "SHE", "长春": "CGQ", "大连": "DLC", "太原": "TYN",
    "石家庄": "SJW", "宁波": "NGB", "温州": "WNZ", "珠海": "ZUH", "汕头": "SWA",
    "呼和浩特": "HET", "西宁": "XNN", "银川": "INC", "乌鲁木齐": "URC", "拉萨": "LXA",
    "无锡": "WUX", "徐州": "XUZ", "洛阳": "LYA", "宜昌": "YIH", "六盘水": "LPF",
}


@dataclass(frozen=True)
class FlightLookup:
    status: Literal["found", "no_direct_service", "failed"]
    mode: TransportModeSummary | None = None


async def _read_reply(response: httpx.Response) -> dict:
    if response.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "text/event-stream":
        await response.aread()
        return response.json()

    data: list[str] = []
    async for line in response.aiter_lines():
        if line.startswith("data:"):
            data.append(line[5:].removeprefix(" "))
        elif not line and data:
            event = json.loads("\n".join(data))
            if isinstance(event, dict) and event.get("id") == 1:
                return event
            data.clear()
    if data:
        return json.loads("\n".join(data))
    raise ValueError("missing Variflight RPC reply")


def _read_flight_data(payload: Any) -> list | dict:
    if not isinstance(payload, dict) or payload.get("id") != 1 or "error" in payload:
        raise ValueError("invalid Variflight RPC reply")
    result = payload.get("result")
    if not isinstance(result, dict) or result.get("isError"):
        raise ValueError("Variflight tool failed")
    content = result.get("content")
    first = content[0] if isinstance(content, list) and content else None
    message = first.get("text") if isinstance(first, dict) else None
    prefix = "Flight prices: "
    if not isinstance(message, str) or not message.startswith(prefix):
        raise ValueError("missing Variflight price payload")
    body = ast.literal_eval(message.removeprefix(prefix).strip())
    if not isinstance(body, dict) or body.get("code") != 200:
        raise ValueError("Variflight rejected price lookup")
    data = body.get("data")
    if not isinstance(data, (dict, list)):
        raise ValueError("invalid Variflight flight data")
    return data


def _amount(value: Any) -> Decimal | None:
    try:
        amount = Decimal(str(value))
        if amount.is_finite() and amount >= 0:
            return amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except InvalidOperation:
        pass
    return None


def _economy_fare(row: dict) -> Decimal | None:
    cabins = row.get("cabins")
    fuel = _amount(row.get("oilfee"))
    tax = _amount(row.get("tax"))
    if not isinstance(cabins, list) or fuel is None or tax is None:
        return None
    prices = [
        _amount(cabin.get("price")) for cabin in cabins
        if isinstance(cabin, dict) and cabin.get("cabinclass") == "Y"
    ]
    lowest = min((price for price in prices if price is not None and price > 0), default=None)
    return lowest + fuel + tax if lowest is not None else None


def _flight_option(row: dict) -> FlightOption | None:
    flight_no = row.get("flightno")
    departure_airport = row.get("flightdepcode")
    arrival_airport = row.get("flightarrcode")
    if not all(isinstance(value, str) and value.strip() for value in (
        flight_no, departure_airport, arrival_airport,
    )):
        return None
    try:
        departure = datetime.fromtimestamp(int(row["flightdeptimeplandate"]), tz=_SHANGHAI_TZ)
        arrival = datetime.fromtimestamp(int(row["flightarrtimeplandate"]), tz=_SHANGHAI_TZ)
        duration = int((arrival - departure).total_seconds() // 60)
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        return None
    if duration <= 0:
        return None
    return FlightOption(
        flight_no=flight_no,
        airline=str(row.get("flightcompany") or "未知航司"),
        departure_time=departure.strftime("%H:%M"),
        arrival_time=arrival.strftime("%H:%M"),
        duration_minutes=duration,
        departure_airport=departure_airport,
        arrival_airport=arrival_airport,
    )


def _fare_range(fares: list[Decimal]) -> str:
    if not fares:
        return FLIGHT_FARE_PENDING
    fares.sort()
    lower = fares[2] if len(fares) >= 3 else fares[0]
    # Small samples can put the median below the third-lowest fare.
    upper = max(lower, median(fares)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"¥{lower.normalize():f}-{upper.normalize():f}"


def _lookup_result(data: list | dict) -> FlightLookup:
    if isinstance(data, dict):
        if data.get("error_code") == 10 and data.get("error") == "暂无数据":
            return FlightLookup("no_direct_service")
        return FlightLookup("failed")

    options: list[FlightOption] = []
    fares: list[Decimal] = []
    for row in data:
        if not isinstance(row, dict) or row.get("shareflag") not in (None, False, 0, "0", ""):
            continue
        option = _flight_option(row)
        if option is None:
            continue
        options.append(option)
        fare = _economy_fare(row)
        if fare is not None:
            fares.append(fare)
    if not options:
        return FlightLookup("failed")

    options.sort(key=lambda option: (option.duration_minutes, option.departure_time, option.flight_no))
    return FlightLookup("found", TransportModeSummary(
        mode="flight",
        min_duration_minutes=options[0].duration_minutes,
        price_range=_fare_range(fares),
        price_source="realtime",
        daily_count=len(options),
        data_source="realtime",
        availability_checked_at=datetime.now(timezone.utc).isoformat(),
        top_options=options[:5],
    ))


async def query_flight_prices(
    dep_city: str,
    arr_city: str,
    dep_date: str,
    *,
    api_key: str,
    timeout: float,
) -> FlightLookup:
    if not api_key:
        return FlightLookup("failed")

    async def fetch() -> dict:
        async with httpx.AsyncClient() as client:
            async with client.stream(
                "POST",
                VARIFLIGHT_MCP_URL,
                headers={"Accept": "application/json, text/event-stream", "X-API-Key": api_key},
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "getFlightPriceByCities",
                        "arguments": {"dep_city": dep_city, "arr_city": arr_city, "dep_date": dep_date},
                    },
                },
                timeout=timeout,
            ) as response:
                response.raise_for_status()
                return await _read_reply(response)

    try:
        payload = await asyncio.wait_for(fetch(), timeout=timeout)
        return _lookup_result(_read_flight_data(payload))
    except httpx.HTTPStatusError as exc:
        logger.info("Variflight price lookup rejected status=%s", exc.response.status_code)
    except (asyncio.TimeoutError, httpx.RequestError, SyntaxError, ValueError, ArithmeticError) as exc:
        logger.info("Variflight price lookup failed type=%s", type(exc).__name__)
    return FlightLookup("failed")
