"""Model JSON, repair prompts and planner model calls."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import re
import time
from functools import partial

from src.agents import llm

from src.config import Settings

from src.agents.route_planner.rules import (
    KINDS,
    JSON_RETRY_HINT,
    REPAIR_TAIL,
    PlannerRules,
)

from src.agents.route_planner.places import coerce_place_id

# RoutePlannerCall is built through the package. Importing planner here would cycle.
import src.agents.route_planner as _route_planner_pkg

logger = logging.getLogger(__name__)

# The backup leg starts only with this much left of both the single call
# (50s) and the whole step (70s). Neither limit is extended for it.
BACKUP_MIN_SECONDS = 20.0

_BAD_KEY_MARKERS = (
    "api key not valid",
    "api_key_invalid",
    "api key expired",
    "valid api key",
    "invalid api key",
    "invalid auth key",
)



def parse_model_plan(text: str) -> tuple[dict | None, str | None]:
    """Return ``(plan, None)`` or ``(None, "json"|"format")``."""
    raw = (text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", raw, re.S)
    if fenced:
        raw = fenced.group(1)
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return None, "json"
    try:
        data = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return None, "json"
    if not isinstance(data, dict) or not isinstance(data.get("days"), list):
        return None, "format"
    for day in data["days"]:
        if not isinstance(day, dict) or not isinstance(day.get("stops"), list):
            return None, "format"
        for stop in day["stops"]:
            if not isinstance(stop, dict) or stop.get("kind") not in KINDS:
                return None, "format"
            stop.pop("_scenic_stops", None)
            if stop.get("kind") == "rest":
                stop["place_id"] = None
            else:
                stop["place_id"] = coerce_place_id(stop.get("place_id"))
            stop["optional"] = stop.get("optional") is True
    return data, None


def merge_days(rules: PlannerRules, previous: dict, revised: dict) -> tuple[dict, str | None]:
    """A repair that returns only some days keeps the other days from the previous draft."""

    def by_day(plan: dict) -> dict[int, dict]:
        found = {}
        for day in plan.get("days") or []:
            try:
                number = int(day.get("day"))
            except (TypeError, ValueError):
                continue
            if day.get("stops") and number not in found:
                found[number] = day
        return found

    old, new = by_day(previous), by_day(revised)
    if len(new) >= rules.days:
        return revised, None
    merged = {key: value for key, value in previous.items() if key != "days"}
    merged.update({key: value for key, value in revised.items() if key != "days"})
    merged["days"] = [copy.deepcopy(new.get(number) or old[number]) for number in range(1, rules.days + 1) if new.get(number) or old.get(number)]
    return merged, f"模型修改只交回第 {sorted(new)} 天，其余沿用上一稿"


def _public_plan(plan: dict, *, include_lodging: bool = True) -> dict:
    days = []
    for day in plan.get("days") or []:
        stops = []
        for stop in day.get("stops") or []:
            for member in stop.get("_scenic_stops", (stop,)):
                row = {
                    "kind": member.get("kind"),
                    "place_id": member.get("place_id"),
                    "stay_minutes": member.get("stay_minutes"),
                }
                if member.get("optional") is True:
                    row["optional"] = True
                stops.append(row)
        days.append({"day": day.get("day"), "stops": stops})
    if not include_lodging:
        # Hotel lodging is chosen in code. The repair prompt must not ask for it.
        return {"days": days}
    lodging = plan.get("lodging") if isinstance(plan.get("lodging"), dict) else {}
    return {
        "lodging": {key: lodging.get(key) for key in ("area", "anchor")},
        "days": days,
    }


def _split_model_result(result) -> tuple[str, dict]:
    if isinstance(result, str):
        return result, {}
    text, usage = result
    return str(text or ""), dict(usage or {})


async def _invoke(
    caller,
    system: str,
    user: str,
    *,
    settings: Settings,
    calls: list[RoutePlannerCall],
    repaired: bool,
    json_retry: bool,
    timeout: float | None = None,
) -> str:
    if timeout is None:
        timeout = float(settings.route_planner_timeout_seconds)
    started = time.perf_counter()
    try:
        result = await asyncio.wait_for(caller(system, user), timeout=timeout)
    except Exception as exc:
        calls.append(
            _route_planner_pkg.RoutePlannerCall(
                index=len(calls) + 1,
                repaired=repaired,
                json_retry=json_retry,
                latency_ms=int((time.perf_counter() - started) * 1000),
                prompt_tokens=0,
                completion_tokens=0,
                provider="",
                model="",
                backup_reason=str(getattr(exc, "route_planner_backup_reason", "") or ""),
            )
        )
        raise
    text, usage = _split_model_result(result)
    latency = usage.get("latency_ms")
    if not isinstance(latency, int):
        latency = int((time.perf_counter() - started) * 1000)
    calls.append(
        _route_planner_pkg.RoutePlannerCall(
            index=len(calls) + 1,
            repaired=repaired,
            json_retry=json_retry,
            latency_ms=int(latency),
            prompt_tokens=int(usage.get("prompt_tokens") or usage.get("token_input") or 0),
            completion_tokens=int(usage.get("completion_tokens") or usage.get("token_output") or 0),
            provider=str(usage.get("provider") or ""),
            model=str(usage.get("model") or ""),
            backup_reason=str(usage.get("backup_reason") or ""),
        )
    )
    return text


def backup_switch_reason(exc: BaseException) -> str | None:
    """Why a failed primary call may go to the backup leg, or None.

    Only a clear failure switches: the endpoint is stopped, the key is
    refused, quota or balance is used up, a 5xx, or no connection. A call
    that was merely slow, or returned bad output, does not.
    """
    import httpx
    import openai

    from src.agents.route_planner.planner import _exception_blob, classify_planner_call_failure

    kind, status = classify_planner_call_failure(exc)
    if kind in {"timeout", "bad_output"}:
        return None
    if getattr(exc, "model_request_not_started", False):
        return "primary_stopped"
    if kind in {"quota", "rate_limited", "server_error"}:
        return kind
    if status in {401, 403}:
        return "auth"
    # Gemini answers a bad or expired key with 400, not 401.
    if status == 400 and any(marker in _exception_blob(exc) for marker in _BAD_KEY_MARKERS):
        return "auth"
    if isinstance(exc, (httpx.ConnectError, openai.APIConnectionError)):
        return "connect"
    return None


async def _call_leg(system: str, user: str, settings: Settings, *, timeout: float,
                    config=None, endpoint_name: str | None = None):
    with llm.llm_call_context(
        call_reason="route_planner",
        safe_purpose="ROUTE_PLANNER",
        max_tokens_request=int(settings.route_planner_max_tokens),
        relay_request_timeout_seconds=timeout,
        reasoning_effort=str(settings.route_planner_reasoning_effort),
        route_planner_timeout_seconds=timeout,
        direct_endpoint_name=endpoint_name,
    ):
        return await llm.chat_with_usage(
            system,
            user,
            role="route_planner",
            temperature=float(settings.route_planner_temperature),
            json_mode=True,
            max_tokens=int(settings.route_planner_max_tokens),
            role_config_override=config,
        )


async def _default_caller(system: str, user: str, settings: Settings, *, timeout: float | None = None):
    """Primary leg, then the backup leg on a clear failure with time left."""
    from src.model_routes import route_leg

    call_limit = float(settings.route_planner_timeout_seconds) if timeout is None else timeout
    call_started = time.perf_counter()
    try:
        return await _call_leg(system, user, settings, timeout=call_limit)
    except Exception as exc:
        reason = backup_switch_reason(exc)
        if reason is None:
            raise
        remaining = call_limit - (time.perf_counter() - call_started)
        if remaining < BACKUP_MIN_SECONDS:
            raise
        try:
            config = llm.route_planner_backup_config(settings)
        except ValueError:
            # No backup key on this server: behave as before the backup existed.
            raise exc from None
        leg = route_leg("route_planner", "backup")
        logger.warning("route_planner switches to backup reason=%s remaining=%.1f", reason, remaining)
        try:
            text, usage = await _call_leg(
                system, user, settings, timeout=remaining,
                config=config, endpoint_name=leg.endpoint_id)
        except Exception as backup_exc:
            backup_exc.route_planner_backup_reason = reason
            raise
        return text, {**dict(usage or {}), "backup_reason": reason}


def _repair_user(prepared: PreparedRoute, previous: dict, adjustments: list[str], issues: list[str]) -> str:
    done = "\n".join(f"- {item}" for item in adjustments) or "- 无"
    left = "\n".join(f"- {item}" for item in issues) or "- 无"
    return (
        f"{prepared.user}\n\n上一稿 JSON：\n{json.dumps(previous, ensure_ascii=False)}\n\n"
        f"代码做了这些调整：\n{done}\n\n还剩这些问题：\n{left}\n\n{REPAIR_TAIL}"
    )


async def _read_plan(
    caller, system, user, *, settings, calls, repaired: bool, deadline_monotonic: float | None = None
) -> tuple[dict | None, str | None]:
    async def invoke(user: str, *, json_retry: bool) -> str:
        timeout = float(settings.route_planner_timeout_seconds)
        if deadline_monotonic is not None:
            timeout = min(timeout, deadline_monotonic - time.monotonic())
            if timeout <= 0:
                raise asyncio.TimeoutError("route planner model deadline exhausted")
        return await _invoke(
            partial(caller, timeout=timeout), system, user, settings=settings,
            calls=calls, repaired=repaired, json_retry=json_retry, timeout=timeout,
        )

    text = await invoke(user, json_retry=False)
    plan, error = parse_model_plan(text)
    if error is None:
        return plan, None
    if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
        return plan, error
    retry_user = user if JSON_RETRY_HINT in user else f"{user}\n{JSON_RETRY_HINT}"
    text = await invoke(retry_user, json_retry=True)
    return parse_model_plan(text)
