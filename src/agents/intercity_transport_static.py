"""Versioned directional intercity fallback data and flight reference prices.

The table is deliberately directional.  A missing ``(from_city, to_city)`` row
must never borrow the reverse route, because schedules and reference prices are
reviewed per direction.
"""

from __future__ import annotations

from typing import Any

from src.cost_reference.catalog import load_reference_catalog


STATIC_TRANSPORT_VERSION = "2026-08-01"

STATIC_TRANSPORT: dict[tuple[str, str], dict[str, Any]] = {
    ("北京", "上海"): {
        "train_hours": 4.5,
        "train_price": "¥550-660",
        "flight_hours": 2.5,
        "flight_price": "¥800-1500",
    },
    ("上海", "北京"): {
        "train_hours": 4.5,
        "train_price": "¥550-660",
        "flight_hours": 2.5,
        "flight_price": "¥800-1500",
    },
    ("北京", "广州"): {
        "train_hours": 8.0,
        "train_price": "¥860-1400",
        "flight_hours": 3.0,
        "flight_price": "¥900-2000",
    },
    ("北京", "杭州"): {
        "train_hours": 4.5,
        "train_price": "¥540-650",
        "flight_hours": 2.5,
        "flight_price": "¥750-1400",
    },
    ("北京", "西安"): {
        "train_hours": 4.5,
        "train_price": "¥515-620",
        "flight_hours": 2.2,
        "flight_price": "¥700-1300",
    },
    ("北京", "成都"): {
        "train_hours": 7.5,
        "train_price": "¥780-1250",
        "flight_hours": 3.0,
        "flight_price": "¥900-1800",
    },
    ("北京", "重庆"): {
        "train_hours": 7.5,
        "train_price": "¥760-1220",
        "flight_hours": 3.0,
        "flight_price": "¥850-1700",
    },
    ("上海", "杭州"): {
        "train_hours": 0.6,
        "train_price": "¥55-70",
    },
    ("上海", "南京"): {
        "train_hours": 1.2,
        "train_price": "¥120-150",
    },
    ("上海", "苏州"): {
        "train_hours": 0.5,
        "train_price": "¥40-60",
    },
    ("上海", "广州"): {
        "train_hours": 7.0,
        "train_price": "¥800-1300",
        "flight_hours": 2.5,
        "flight_price": "¥850-1600",
    },
    ("上海", "成都"): {
        "train_hours": 11.0,
        "train_price": "¥900-1450",
        "flight_hours": 3.0,
        "flight_price": "¥900-1800",
    },
    ("成都", "重庆"): {
        "train_hours": 1.3,
        "train_price": "¥100-150",
    },
    ("广州", "深圳"): {
        "train_hours": 0.5,
        "train_price": "¥75-100",
    },
    ("杭州", "南京"): {
        "train_hours": 1.2,
        "train_price": "¥100-140",
    },
}


def get_catalog_fallback(from_city: str, to_city: str) -> dict | None:
    """Try to get intercity price from catalog before falling back to static dict."""
    try:
        catalog = load_reference_catalog()
        entries = [
            e for e in catalog.intercity
            if e.from_city == from_city and e.to_city == to_city
        ]
        if not entries:
            return None
        result = {}
        for entry in entries:
            min_yuan = entry.range_fen.min_fen / 100
            max_yuan = entry.range_fen.max_fen / 100
            price_str = f"¥{int(min_yuan)}-{int(max_yuan)}"
            if entry.mode == "train":
                result["train_price"] = price_str
            elif entry.mode == "flight":
                result["flight_price"] = price_str
        return result if result else None
    except Exception:
        return None


def get_static_fallback(from_city: str, to_city: str) -> dict[str, Any] | None:
    static_entry = STATIC_TRANSPORT.get((from_city, to_city)) or {}
    catalog_prices = get_catalog_fallback(from_city, to_city) or {}
    return {**static_entry, **catalog_prices} or None
