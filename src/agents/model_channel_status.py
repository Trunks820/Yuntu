"""Configuration inventory and passive state projections; never probe a channel."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from src.agents import llm
from src.agents.amap_keys import configured_amap_keys, get_amap_key_coordinator, key_fingerprint


def _base(group: str, name: str, model: str | None, configured: bool, **extra) -> dict:
    return {"group": group, "endpoint_label": name, "model": model,
            "configured": configured, "availability": "unknown", "state_source": "unknown",
            "last_failure_code": None, "last_failure_at": None, "cooldown_until": None,
            "classification": "retained", **extra}


def _state(row: dict | None, source: str) -> dict:
    if row is None:
        return {}
    disabled = row.get("participation") == "DISABLED" or row.get("configured") is False
    circuit = row.get("circuit_state")
    return {"availability": "disabled" if disabled else "temporarily_unavailable" if circuit in {"OPEN", "QUARANTINED"}
            else "limited" if circuit == "HALF_OPEN" else "eligible" if circuit == "CLOSED" and row.get("qualification_state") == "QUALIFIED" else "unknown",
            "state_source": source, **{k: row.get(k) for k in (
                "participation", "circuit_state", "qualification_state", "cooldown_until",
                "manual_retry_not_before", "last_failure_code", "last_failure_at", "updated_at")}}


_REPAIR_STAGES = frozenset({"FRAGMENT_REPAIR", "KEYED_FRAGMENT_REPAIR"})


def _leg_state(leg, writer: dict, roles: dict) -> dict:
    if leg.agent == "export_image":
        return {"state_source": "instance_state_unavailable"}
    if leg.provider == "relay":
        return _state(writer.get(leg.endpoint_id), "persisted_endpoint_state")
    role = "writer" if leg.agent == "repair" else leg.agent
    return _state(roles.get((role, leg.endpoint_id)), "persisted_endpoint_state")


def channel_inventory(settings, writer_rows: list[dict], role_rows: list[dict]) -> list[dict]:
    """One row per leg of src/model_routes.py, then the Amap keys.

    Endpoint rows left in the database by removed routes are history and are
    not listed.
    """
    from src.model_routes import (
        ACCOUNT_LABELS, key_problems, leg_api_key, model_routes, table_problems,
    )
    from src.model_routes import key_fingerprint as model_key_fingerprint

    now = datetime.now(timezone.utc)
    mono = time.monotonic()
    items: list[dict] = []
    writer = {r["endpoint_name"]: r for r in writer_rows}
    roles = {(r["role"], r["endpoint_name"]): r for r in role_rows}
    problems = table_problems() + key_problems(settings)
    for route in model_routes():
        for leg in route.legs:
            key = leg_api_key(settings, leg)
            pending = bool(route.backup_pending and leg.slot == "backup")
            enabled = None
            if leg.agent == "route_planner":
                enabled = getattr(settings, "route_planner_enabled", None)
            elif leg.agent == "writer" and leg.slot == "backup":
                enabled = getattr(settings, "speculative_ds_standby_enabled", None)
            items.append(_base(
                leg.agent, leg.endpoint_id, leg.model, bool(key),
                role="export_image" if leg.agent == "export_image" else
                     "writer" if leg.agent == "repair" else leg.agent,
                provider=leg.provider, slot=leg.slot, title=route.title,
                account=ACCOUNT_LABELS.get(leg.account, leg.account),
                key_env=leg.key_env, key_fingerprint=model_key_fingerprint(key),
                switch_seconds=route.switch_seconds if leg.slot == "backup" else None,
                switch_rule=route.switch_rule, enabled=enabled,
                relay_profile=llm._WRITER_RELAY_PROFILE if leg.provider == "relay" else None,
                classification="planned" if pending else "in_use",
                config_problems=[p for p in problems if leg.key_env in p or f"{leg.agent} " in p],
                **_leg_state(leg, writer, roles)))
    # Review has no leg in the table. Closed is a decision, not a fault.
    review_on = bool(getattr(settings, "llm_review_enabled", True))
    items.append(_base(
        "review", "llm-review", None, True, role="review", provider=None, slot="primary",
        title="审核", enabled=review_on, state_source="config_switch",
        availability="unknown" if review_on else "closed",
        classification="retained" if review_on else "closed",
        switch_rule="" if review_on else "已关闭（LLM_REVIEW_ENABLED=false），只过代码门禁",
        config_problems=[]))
    coordinator = get_amap_key_coordinator()
    # Snapshot without clearing expired entries or advancing the shared counter.
    with coordinator._lock:
        quotas = dict(coordinator._quota_until)
        limits = dict(coordinator._over_limit_until)
    for key in configured_amap_keys(settings):
        fp = key_fingerprint(key)
        for bucket in ("route", "search", "poi_detail"):
            until = quotas.get((fp, bucket))
            items.append(_base("amap", f"amap-{fp}-{bucket}", None, True, bucket=bucket,
                state_source="local_process_bucket_quota", cost_scope="non_model_usage_unpriced",
                availability="temporarily_unavailable" if until and until > now.timestamp() else "unknown",
                cooldown_until=datetime.fromtimestamp(until, timezone.utc).isoformat() if until else None,
                last_failure_code="LEGACY_QUOTA_OR_INVALID_KEY" if until else None))
        for (fingerprint, mode), until in limits.items():
            if fingerprint == fp:
                items.append(_base("amap", f"amap-{fp}-{mode}", None, True, mode=mode,
                    state_source="local_process_mode_cooldown", last_failure_code="RATE_LIMITED",
                    availability="temporarily_unavailable" if until > mono else "unknown",
                    cooldown_until=(now + timedelta(seconds=until - mono)).isoformat()))
    if not configured_amap_keys(settings):
        items.append(_base("amap", "amap", None, False, cost_scope="non_model_usage_unpriced"))
    for item in items:
        if not item["configured"]:
            item["availability"] = "not_configured"
    return items


def call_matches_channel(call: dict, channel: dict) -> bool:
    if channel["group"] == "amap":
        return False
    if channel["group"] == "review":
        return call.get("role") == "review"
    if call.get("provider") != channel.get("provider") or call.get("role") != channel.get("role"):
        return False
    repair = (call.get("stage") in _REPAIR_STAGES
              or call.get("safe_purpose") == "FRAGMENT_REPAIR")
    if channel["group"] == "repair":
        return repair
    if repair:
        return False
    if channel["group"] in {"route_planner", "extract"}:
        return not call.get("relay_profile") and call.get("endpoint_label") == channel.get("provider")
    if channel.get("relay_profile") and call.get("relay_profile") != channel["relay_profile"]:
        return False
    return call.get("endpoint_label") == channel["endpoint_label"]
