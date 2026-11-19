"""Read-only internal observation queries. No admission, reap or synchronization."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timezone

from sqlalchemy import text

from src.agents.model_call_observation import estimate_cost, internal_reason, summarize_costs, summarize_usage
from src.pipeline.db import get_session_factory


# Explicit field projection: never return prompts, errors containing provider text,
# failed drafts or the rest of quality_metrics/step metadata.
CALL_FIELDS = frozenset({
    "observation_id", "observation_call_id", "sequence", "status", "role", "stage",
    "safe_purpose", "provider", "model", "relay_profile", "relay_endpoint", "endpoint_label",
    "latency_ms", "started_at", "finished_at", "attempt", "publish_retry_round",
    "owning_stage", "owning_attempt", "owning_publish_retry_round", "response_received",
    "termination_reason", "error_type", "error_code", "http_status_code", "observed_usage",
})


def event_time(value) -> datetime | None:
    try:
        when = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return when.astimezone(timezone.utc) if when.tzinfo is not None else None
    except (ValueError, TypeError):
        return None


def project_calls(rows: list[dict], prices: list[dict], day: date | None = None) -> list[dict]:
    found: dict[str, dict] = {}
    expanded = []
    for row in rows:
        raw = row.get("call")
        attempts = raw.get("transport_attempts") if isinstance(raw, dict) else None
        if isinstance(attempts, list) and attempts:
            for index, attempt in enumerate(attempts):
                if not isinstance(attempt, dict):
                    continue
                merged = {**raw, **attempt}
                if index == len(attempts) - 1 and raw.get("status") != "success" and attempt.get("status") == "success":
                    # HTTP success does not erase later parse/validation failure.
                    merged.update({key: raw.get(key) for key in ("status", "error_type", "error_code", "termination_reason")})
                if index == len(attempts) - 1 and attempt.get("status") == "cancelled" and internal_reason(raw) == "CALL_TIMEOUT":
                    # wait_for implements transport deadlines by cancelling its
                    # child. The logical result is a timeout, not user cancel.
                    merged.update({key: raw.get(key) for key in ("status", "error_type", "error_code", "termination_reason")})
                expanded.append({**row, "call": merged})
        else:
            expanded.append(row)
    for row in expanded:
        raw = row.get("call")
        if not isinstance(raw, dict):
            continue
        call = {k: v for k, v in raw.items() if k in CALL_FIELDS}
        call["source"] = row.get("source")
        call["job_id"] = row.get("job_id")
        call["artifact_id"] = row.get("artifact_id")
        call["internal_reason_code"] = internal_reason(call)
        call["endpoint_label"] = call.get("endpoint_label") or call.get("relay_endpoint") or "unknown"
        if day is not None:
            when = event_time(call.get("finished_at"))
            if when is None or when.date() != day:
                continue
        # Observation IDs survive copying to plan and terminal steps. Legacy
        # records without IDs are deduplicated within their job/result only.
        identity = str(call.get("observation_id") or hashlib.sha256(
            json.dumps([row.get("owner"), raw], sort_keys=True, default=str).encode()
        ).hexdigest())
        call["estimated_cost"] = estimate_cost(call, prices)
        found[identity] = call
    return sorted(found.values(), key=lambda c: (event_time(c.get("finished_at")) or datetime.min.replace(tzinfo=timezone.utc), str(c.get("observation_id") or "")))


_CALL_SQL = """
SELECT 'plan' AS source, COALESCE(j.job_id, 'result:' || p.id::text) AS owner,
       j.job_id, NULL::text AS artifact_id, c AS call
FROM travel_plan_record p LEFT JOIN travel_trip_job j ON j.result_record_id = p.id
CROSS JOIN LATERAL jsonb_array_elements(CASE WHEN jsonb_typeof(p.quality_metrics->'llm_call_records') = 'array'
    THEN p.quality_metrics->'llm_call_records' ELSE '[]'::jsonb END) c
WHERE (CAST(:job_id AS text) IS NULL OR j.job_id = :job_id)
UNION ALL
SELECT 'step', s.job_id, s.job_id, NULL::text, c
FROM travel_trip_job_step s
CROSS JOIN LATERAL jsonb_array_elements(CASE WHEN jsonb_typeof(s.metadata->'llm_observation'->'llm_call_records') = 'array'
    THEN s.metadata->'llm_observation'->'llm_call_records' ELSE '[]'::jsonb END) c
WHERE (CAST(:job_id AS text) IS NULL OR s.job_id = :job_id)
UNION ALL
SELECT 'export', COALESCE(j.job_id, 'result:' || a.result_record_id::text), j.job_id, a.artifact_id, c
FROM travel_export_artifact a LEFT JOIN travel_trip_job j ON j.result_record_id = a.result_record_id
CROSS JOIN LATERAL jsonb_array_elements(CASE WHEN jsonb_typeof(a.metadata->'model_call_records') = 'array'
    THEN a.metadata->'model_call_records' ELSE '[]'::jsonb END) c
WHERE (CAST(:job_id AS text) IS NULL OR j.job_id = :job_id)
"""


async def read_call_rows(job_id: str | None = None) -> list[dict]:
    async with get_session_factory()() as session:
        result = await session.execute(text(_CALL_SQL), {"job_id": job_id})
        return [dict(row) for row in result.mappings()]


async def read_job(job_id: str, prices: list[dict]) -> dict | None:
    async with get_session_factory()() as session:
        result = await session.execute(text("""
            SELECT j.job_id, j.status, j.created_time, j.finished_time, j.error_code,
                j.delivery_json->>'outcome' AS delivery_outcome,
                j.delivery_json->>'review_state' AS review_state,
                p.quality_metrics->>'safe_trigger' AS safe_trigger,
                p.quality_metrics->>'published_variant' AS published_variant,
                p.quality_metrics->>'delivery_status' AS delivery_status,
                p.quality_metrics->>'dispatch_action' AS dispatch_action,
                p.quality_metrics->'dispatch_reasons' AS dispatch_reasons,
                EXISTS (SELECT 1 FROM travel_trip_failed_draft d WHERE d.job_id = j.job_id) AS failed_draft_available
            FROM travel_trip_job j LEFT JOIN travel_plan_record p ON p.id = j.result_record_id
            WHERE j.job_id = :job_id
        """), {"job_id": job_id})
        row = result.mappings().first()
        if row is None:
            return None
        job = dict(row)
        # Dispatch reasons are machine codes, never snippets or reviewer text.
        reasons = job.get("dispatch_reasons")
        reasons = reasons if isinstance(reasons, list) else []
        job["dispatch_reasons"] = sorted({code for code in reasons
            if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_:.-]{1,160}", code)}) or ["UNKNOWN"]
        result = await session.execute(text("""
            SELECT stage, attempt, publish_retry_round, status, started_time, finished_time, latency_ms
            FROM travel_trip_job_step WHERE job_id = :job_id ORDER BY id
        """), {"job_id": job_id})
        job["steps"] = [dict(step) for step in result.mappings()]
    calls = project_calls(await read_call_rows(job_id), prices)
    return {**job, "calls": calls, "cost": summarize_costs(calls), "usage": summarize_usage(calls),
            "observed_call_failure_codes": sorted({f"{c.get('role') or 'unknown'}:{c['internal_reason_code']}"
                for c in calls if c.get("internal_reason_code")}),
            "observation_state": "recorded" if calls else "unknown",
            "coverage": "persisted_call_facts; in-flight and pre-instrumentation records may be absent"}


async def read_channel_rows() -> tuple[list[dict], list[dict]]:
    async with get_session_factory()() as session:
        writer = await session.execute(text("""
            SELECT s.endpoint_name, s.participation, s.circuit_state, s.qualification_state,
                s.cooldown_until, s.manual_retry_not_before, s.updated_at,
                f.safe_failure_class AS last_failure_code, f.finished_at AS last_failure_at
            FROM travel_relay_endpoint_state s LEFT JOIN LATERAL (
                SELECT safe_failure_class, finished_at FROM travel_writer_dispatch d
                WHERE d.endpoint_name = s.endpoint_name AND d.safe_failure_class IS NOT NULL
                ORDER BY COALESCE(d.finished_at, d.updated_at) DESC LIMIT 1
            ) f ON TRUE
        """))
        roles = await session.execute(text("""
            SELECT role, endpoint_name, provider, configured, participation, circuit_state,
                qualification_state, cooldown_until, manual_retry_not_before,
                last_failure_code, last_failure_at, updated_at
            FROM travel_role_relay_endpoint_state
        """))
        return [dict(r) for r in writer.mappings()], [dict(r) for r in roles.mappings()]


async def persist_export_call(artifact_id: str, call: dict) -> None:
    """Append/replace a single safe fact without overwriting other export metadata."""
    safe = {k: v for k, v in call.items() if k in CALL_FIELDS}
    async with get_session_factory()() as session:
        await session.execute(text("""
            UPDATE travel_export_artifact SET metadata = jsonb_set(metadata, '{model_call_records}',
                COALESCE((SELECT jsonb_agg(item) FROM jsonb_array_elements(
                    COALESCE(metadata->'model_call_records', '[]'::jsonb)) item
                    WHERE item->>'observation_id' <> :observation_id), '[]'::jsonb)
                    || CAST(:call AS jsonb), TRUE)
            WHERE artifact_id = :artifact_id
              AND NOT EXISTS (SELECT 1 FROM jsonb_array_elements(COALESCE(metadata->'model_call_records', '[]'::jsonb)) prior
                  WHERE prior->>'observation_id' = :observation_id AND prior->>'finished_at' IS NOT NULL
                    AND CAST(:finished_at AS text) IS NULL)
        """), {"artifact_id": artifact_id, "observation_id": safe["observation_id"], "finished_at": safe.get("finished_at"), "call": json.dumps([safe])})
        await session.commit()
