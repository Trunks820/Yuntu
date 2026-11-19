"""Token-protected passive model observations; public APIs keep their contract."""
from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Response

from src.agents.model_call_observation import parse_prices, summarize_costs, summarize_usage
from src.agents.model_channel_status import channel_inventory, call_matches_channel
from src.config import get_settings
from src.internal.auth import verify_internal_token
from src.jobs.model_call_store import event_time, project_calls, read_call_rows, read_channel_rows, read_job

router = APIRouter(prefix="/internal/model-calls", dependencies=[Depends(verify_internal_token)])


@router.get("/status")
async def model_call_status(response: Response, day: date | None = None):
    response.headers["Cache-Control"] = "no-store"
    settings = get_settings()
    prices, price_state = parse_prices(settings.model_call_prices_json)
    chosen_day = day or datetime.now(timezone.utc).date()
    rows = await read_call_rows()
    calls = project_calls(rows, prices, chosen_day)
    all_calls = project_calls(rows, prices)
    writer, roles = await read_channel_rows()
    channels = channel_inventory(settings, writer, roles)
    for channel in channels:
        matching = [c for c in all_calls if call_matches_channel(c, channel)]
        channel["day_cost"] = summarize_costs([c for c in calls if call_matches_channel(c, channel)])
        failures = [c for c in matching if c["internal_reason_code"] not in {None, "CALL_CANCELLED"}
                    and event_time(c.get("finished_at"))]
        if channel.get("state_source") == "instance_state_unavailable":
            # No stored health record (the image leg): judge by the latest call.
            finished = [c for c in matching if event_time(c.get("finished_at"))]
            if finished:
                latest = max(finished, key=lambda c: event_time(c["finished_at"]))
                channel["availability"] = ("temporarily_unavailable" if latest["internal_reason_code"]
                                           not in {None, "CALL_CANCELLED"} else "eligible")
                channel["state_source"] = "latest_call"
        prior_failure = event_time(channel["last_failure_at"])
        if failures and (prior_failure is None or prior_failure < event_time(failures[-1]["finished_at"])):
            channel["last_failure_code"] = failures[-1]["internal_reason_code"]
            channel["last_failure_at"] = failures[-1]["finished_at"]
    return {"as_of": datetime.now(timezone.utc), "day": chosen_day,
            "day_basis": "call_finished_at_utc", "price_config_state": price_state,
            "cost": summarize_costs(calls), "usage": summarize_usage(calls), "channels": channels,
            "unknown_time_call_count": sum(event_time(c.get("finished_at")) is None for c in all_calls),
            "internal_reason_counts": {reason: sum(c["internal_reason_code"] == reason for c in calls)
                for reason in sorted({c["internal_reason_code"] for c in calls if c["internal_reason_code"]})},
            "coverage": {"scope": "persisted_model_call_facts", "legacy_and_inflight": "may_be_incomplete",
                         "amap_cost": "not_in_model_price_table", "prices": "current_manual_reference_not_invoice"}}


@router.get("/jobs/{job_id}")
async def model_calls_for_job(job_id: str, response: Response):
    response.headers["Cache-Control"] = "no-store"
    if len(job_id) > 128:
        raise HTTPException(status_code=404, detail="job not found")
    prices, price_state = parse_prices(get_settings().model_call_prices_json)
    job = await read_job(job_id, prices)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return {**job, "price_config_state": price_state}
