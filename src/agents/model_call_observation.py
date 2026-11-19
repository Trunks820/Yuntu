"""Internal-only call facts and reference accounting; never routing policy.

Usage is separate from the legacy zero-filled telemetry fields. None means
the provider did not report the value, including calls cancelled after send.
Prices are operator-maintained snapshots, not live billing or spending limits.
"""
from __future__ import annotations

import json
import math
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any


def _mapping(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    dump = getattr(value, "model_dump", None)
    return dump() if callable(dump) else {}


def _count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value < 0 or value > 9223372036854775807 or not math.isfinite(value) or int(value) != value:
        return None
    return int(value)


def observed_usage(value: Any, wire_api: str = "openai_chat") -> dict[str, int | None]:
    data = _mapping(value)
    if wire_api == "gemini_native":
        input_count = _count(data.get("promptTokenCount"))
        output_count = _count(data.get("candidatesTokenCount"))
        thoughts = _count(data.get("thoughtsTokenCount"))
        if output_count is not None and thoughts is not None:
            output_count += thoughts
        cached = _count(data.get("cachedContentTokenCount"))
        created = None
    elif wire_api == "anthropic_messages":
        input_count = _count(data.get("input_tokens"))
        output_count = _count(data.get("output_tokens"))
        cached = _count(data.get("cache_read_input_tokens"))
        created = _count(data.get("cache_creation_input_tokens"))
        # Anthropic's input_tokens excludes cache read/write input tokens.
        if input_count is not None:
            input_count += (cached or 0) + (created or 0)
    else:
        input_count = _count(data.get("prompt_tokens", data.get("input_tokens")))
        output_count = _count(data.get("completion_tokens", data.get("output_tokens")))
        details = _mapping(data.get("prompt_tokens_details", data.get("input_tokens_details")))
        cached = _count(data.get("prompt_cache_hit_tokens", details.get("cached_tokens")))
        created = None
    return {"input_tokens": input_count, "output_tokens": output_count,
            "cached_input_tokens": cached, "cache_creation_input_tokens": created}


def internal_reason(record: dict) -> str | None:
    status = str(record.get("status") or "").upper()
    termination = str(record.get("termination_reason") or "").lower()
    error = str(record.get("error_code") or record.get("error_type") or "").upper()
    if status == "CANCELLED" or "cancel" in termination or "CANCEL" in error:
        return "CALL_CANCELLED"
    if status == "SUCCESS":
        return None
    if status in {"IN_FLIGHT", "RUNNING", "PENDING"}:
        return None
    http_status = record.get("http_status_code", record.get("http_status"))
    if http_status == 401 or error == "AUTH":
        return "AUTH_FAILED"
    if error in {"QUOTA", "INSUFFICIENT_QUOTA", "INSUFFICIENT_BALANCE"}:
        return "QUOTA_EXHAUSTED"
    if http_status == 403:
        return "PERMISSION_DENIED"
    if http_status == 429 or error in {"RATE_LIMIT", "RATE_LIMITED"}:
        return "RATE_LIMITED"
    if "TIMEOUT" in error or "timeout" in termination or status == "TIMEOUT":
        return "CALL_TIMEOUT"
    if error in {"SCHEMA_INVALID", "JSON_INVALID", "INVALID_OUTPUT", "JSONDECODEERROR", "WIRE_INVALID"}:
        return "INVALID_OUTPUT"
    if error == "TRANSPORT" or any(x in error for x in ("CONNECT", "NETWORK")):
        return "TRANSPORT_FAILED"
    if isinstance(http_status, int) and http_status >= 500:
        return "PROVIDER_UNAVAILABLE"
    return "CALL_FAILED"


def provider_error_code(data: dict) -> str | None:
    """Only retain recognized structured codes; never provider error messages."""
    error = data.get("error")
    if not isinstance(error, dict):
        return None
    code = str(error.get("code") or error.get("type") or "").upper()
    return {"INSUFFICIENT_QUOTA": "QUOTA", "INSUFFICIENT_BALANCE": "QUOTA",
            "QUOTA_EXCEEDED": "QUOTA", "QUOTA_EXHAUSTED": "QUOTA",
            "INVALID_API_KEY": "AUTH", "AUTHENTICATION_ERROR": "AUTH",
            "RATE_LIMIT_EXCEEDED": "RATE_LIMIT", "RATE_LIMIT_ERROR": "RATE_LIMIT"}.get(code)


def enrich_record(record: dict) -> dict:
    """Add only bounded facts. No provider response text or credentials."""
    result = dict(record)
    now = datetime.now(timezone.utc)
    result.setdefault("finished_at", now.isoformat())
    if "started_at" not in result:
        try:
            finish = datetime.fromisoformat(result["finished_at"])
            duration = max(0, min(int(result.get("latency_ms") or 0), 86400000))
            result["started_at"] = (finish - timedelta(milliseconds=duration)).isoformat()
        except (TypeError, ValueError):
            result["started_at"] = None
    result.setdefault("endpoint_label", result.get("relay_endpoint") or result.get("provider") or "unknown")
    result.setdefault("observed_usage", observed_usage(None))
    result["internal_reason_code"] = internal_reason(result)
    return result


def parse_prices(raw: str) -> tuple[list[dict], str]:
    """Prices: JSON array, exact provider/model, optional endpoint_label.

    Required fields: revision, currency, input_per_million, output_per_million.
    Optional cached_input_per_million and cache_creation_per_million apply only
    when the provider actually reports those quantities.
    """
    if not raw.strip():
        return [], "unconfigured"
    try:
        rows = json.loads(raw)
        if not isinstance(rows, list):
            raise ValueError
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError
            for key in ("provider", "model", "revision", "currency"):
                if not isinstance(row.get(key), str) or not row[key].strip():
                    raise ValueError
            if not re.fullmatch(r"[A-Z]{3}", row["currency"]):
                raise ValueError
            for key in ("input_per_million", "output_per_million",
                        "cached_input_per_million", "cache_creation_per_million"):
                if key not in row and key not in {"input_per_million", "output_per_million"}:
                    continue
                number = Decimal(str(row[key]))
                if not number.is_finite() or number < 0:
                    raise ValueError
        return rows, "configured"
    except (ValueError, TypeError, KeyError, InvalidOperation):
        return [], "invalid"


def estimate_cost(record: dict, prices: list[dict]) -> dict:
    unknown = {"amount": None, "currency": None, "price_revision": None,
               "state": "price_missing"}
    matches = [p for p in prices if p["provider"] == record.get("provider")
               and p["model"] == record.get("model")
               and (not p.get("endpoint_label") or p["endpoint_label"] == record.get("endpoint_label"))]
    if not matches:
        return unknown
    matches.sort(key=lambda p: bool(p.get("endpoint_label")), reverse=True)
    price = matches[0]
    result = {**unknown, "currency": price["currency"], "price_revision": price["revision"],
              "state": "usage_missing"}
    usage = record.get("observed_usage") or {}
    inp, out = usage.get("input_tokens"), usage.get("output_tokens")
    if usage.get("complete") is False or _count(inp) is None or _count(out) is None:
        return result
    if sum(_count(usage.get(k)) or 0 for k in ("cached_input_tokens", "cache_creation_input_tokens")) > inp:
        return result
    amount = Decimal(inp) * Decimal(str(price["input_per_million"]))
    amount += Decimal(out) * Decimal(str(price["output_per_million"]))
    for quantity, rate in (("cached_input_tokens", "cached_input_per_million"),
                           ("cache_creation_input_tokens", "cache_creation_per_million")):
        count = usage.get(quantity)
        if rate in price and _count(count) is None:
            return result
        if count:
            if count > inp or rate not in price:
                return result
            amount += Decimal(count) * (Decimal(str(price[rate])) - Decimal(str(price["input_per_million"])))
    return {**result, "amount": str((amount / Decimal(1000000)).quantize(Decimal("0.00000001"))),
            "state": "estimated"}


def summarize_costs(calls: list[dict]) -> dict:
    totals: dict[str, Decimal] = {}
    missing = 0
    for call in calls:
        cost = call.get("estimated_cost") or {}
        if cost.get("state") != "estimated":
            missing += 1
            continue
        currency = cost["currency"]
        totals[currency] = totals.get(currency, Decimal(0)) + Decimal(cost["amount"])
    return {"state": "unavailable" if not calls else "complete" if not missing else "partial" if totals else "unavailable",
            "call_count": len(calls), "unpriced_call_count": missing,
            "known_subtotal_by_currency": {k: str(v) for k, v in sorted(totals.items())}}


def summarize_usage(calls: list[dict]) -> dict:
    return {field: {"reported_total": sum((c.get("observed_usage") or {}).get(field) or 0 for c in calls),
                    "unknown_call_count": sum((c.get("observed_usage") or {}).get(field) is None for c in calls)}
            for field in observed_usage(None)}
