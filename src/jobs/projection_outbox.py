"""Atomic, privacy-safe source snapshots for Admin Control Plane v0.2."""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timezone
from typing import Any, Iterable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.agents.llm import project_persisted_llm_usage_for_stage, public_llm_role_purpose
from src.pipeline.db import get_session_factory
from src.jobs.generation_delivery import (
    ExecutionObservation,
    GenerationDelivery,
    RouteDiagnostics,
    validate_delivery_for_job,
)


EVENT_TYPE = "TRIP_PROJECTION_COMMITTED"
EVENT_SCHEMA_VERSION = "1.2"
AGGREGATE_TYPE = "TRIP_JOB"
MAX_CHANGED_STEPS = 500

JOB_PAYLOAD_FIELDS = frozenset(
    {
        "source_id",
        "job_id",
        "source_version",
        "source",
        "city",
        "days",
        "status",
        "current_stage",
        "result_type",
        "result_record_id",
        "generator",
        "guide_result_state",
        "error_code",
        "safe_error",
        "detailed_reason",
        "created_at",
        "started_at",
        "finished_at",
        "retry_count",
        "failed_draft_available",
        "trace_completeness",
        "source_updated_at",
    }
)
STEP_PAYLOAD_FIELDS = frozenset(
    {
        "source_step_id",
        "job_id",
        "source_version",
        "stage",
        "status",
        "attempt",
        "publish_retry_round",
        "started_at",
        "finished_at",
        "duration_ms",
        "source_updated_at",
    }
)
OPTIONAL_JOB_PAYLOAD_FIELDS = frozenset({
    "failure_observation",
    "generation_policy_version",
    "delivery",
    "route_diagnostics",
})
OPTIONAL_STEP_PAYLOAD_FIELDS = frozenset({"llm_usage", "execution_observation"})
FORBIDDEN_PAYLOAD_KEYS = frozenset(
    {
        "email",
        "masked_email",
        "request",
        "request_body",
        "user_query",
        "trip_request_json",
        "reply_text",
        "guide_body",
        "final_guide",
        "failed_draft",
        "artifact_bytes",
        "prompt",
        "model_context",
        "metadata",
        "error_detail",
        "relay_endpoint",
        "endpoint_label",
        "relay_profile",
        "base_url",
        "system",
        "user",
        "response",
        "raw",
        "content",
    }
)

_LLM_USAGE_FIELDS = frozenset({
    "schema_version",
    "observation_revision",
    "state",
    "total_call_count",
    "returned_call_count",
    "truncated",
    "adopted_generator",
    "calls",
})
_LLM_SELECTION_FIELDS = frozenset({
    "selection_source",
    "selection_fallback_reason",
})
_LLM_CALL_FIELDS = frozenset({
    "sequence",
    "provider",
    "model",
    "role",
    "purpose",
    "status",
    "attempt",
    "publish_retry_round",
})
_LLM_ROLES = frozenset({
    "intent", "extract", "grouping", "selector", "writer", "review",
})
_LLM_PURPOSES = frozenset({
    "INTENT_PARSE", "DATA_EXTRACTION", "SEMANTIC_GROUPING", "POI_SELECTOR",
    "WRITER_PRIMARY", "WRITER_STANDBY", "PUBLISH_RETRY_WRITER",
    "REVIEW", "FRAGMENT_REPAIR", "FAILURE_MESSAGE_POLISH", "OTHER_SAFE",
})
_LLM_STATUSES = frozenset({"SUCCESS", "FAILED", "CANCELLED", "TIMEOUT"})
_FAILURE_FIELDS = frozenset({
    "schema_version", "evidence_state", "observation_revision",
    "failure_stage", "category", "reasons", "automatic_rewrite",
})
_REASON_FIELDS = frozenset({"code", "count", "plan_indexes"})
_REWRITE_FIELDS = frozenset({
    "eligible", "attempted", "completed_rounds", "not_attempted_reason",
})
_SAFE_REASON_CODES = frozenset({
    "activity_content_missing", "ambiguous_alias", "blueprint_integrity_violation",
    "blueprint_theme_weak_match", "budget_infeasible",
    "budget_overrun_without_exception", "city_mismatch", "commute_prose_violation",
    "cross_day_poi", "database_tone", "day_place_names_mismatch",
    "declared_day_count_mismatch", "declared_locked_day_group_violation",
    "duplicate_day_heading", "empty_day", "empty_plan_text",
    "food_none_tier_violation", "food_source_attribution", "food_tier_exceeded",
    "fragment_registry_invalidated", "generic_copy_quality_warn",
    "invariant_commute_legs", "invariant_place_merge", "invariant_route_signature",
    "missing_day", "missing_route_plan", "outline_only", "placeholder_wording",
    "plan_count_mismatch", "plan_similarity_warn", "plan_text_missing_locked_stop",
    "rare_character_compatibility", "repair_budget_exceeded",
    "required_fact_fragment_repair_failed", "review_unavailable", "route_outside_poi",
    "structurally_incomplete", "text_day_count_mismatch",
    "text_locked_day_group_violation", "too_short_plan_text",
    "transit_detail_in_prose", "transit_direction_invented",
    "transit_line_not_allowed", "transit_stop_not_allowed", "transit_summary_altered",
    "truncated_skeleton", "unclassified_failure", "unsupported_fact_expansion",
    "unsupported_meal_role", "weak_evidence_data_gap",
})
_FAILURE_CATEGORIES = frozenset({
    "PUBLISH_GATE_FAILED", "WRITER_CAPACITY_BUSY", "WRITER_ENDPOINTS_UNAVAILABLE",
    "SAFE_RENDER_FAILED", "LLM_ERROR", "DB_ERROR", "WORKFLOW_ERROR", "TIMEOUT",
    "CANCELLED",
})
_REWRITE_SKIP_REASONS = frozenset({
    "NOT_APPLICABLE", "REASON_NOT_ELIGIBLE", "RETRY_ALREADY_EXHAUSTED",
    "INSUFFICIENT_TIME_BUDGET", "SAFE_OR_LOCAL_RECOVERY_SELECTED",
    "WRITER_FAILED_BEFORE_DRAFT", "UNKNOWN_FROM_HISTORICAL_EVIDENCE",
})


def _validated_llm_usage(value: Any) -> dict[str, Any]:
    """Return a copy only when the frozen safe observation shape is exact."""
    if not isinstance(value, dict) or set(value) not in {
        _LLM_USAGE_FIELDS,
        _LLM_USAGE_FIELDS | _LLM_SELECTION_FIELDS,
    }:
        raise ValueError("llm_usage shape is invalid")
    if _LLM_SELECTION_FIELDS.issubset(value):
        source = value["selection_source"]
        reason = value["selection_fallback_reason"]
        if source not in {"LLM", "DETERMINISTIC_FALLBACK"}:
            raise ValueError("selection_source is invalid")
        if reason not in {
            None, "NOT_ATTEMPTED", "TIMEOUT", "CALL_FAILED", "INVALID_OUTPUT",
        }:
            raise ValueError("selection_fallback_reason is invalid")
        if (source == "LLM") != (reason is None):
            raise ValueError("selection source and fallback reason conflict")
    if value.get("schema_version") != 1:
        raise ValueError("llm_usage schema version is invalid")
    revision = value.get("observation_revision")
    total = value.get("total_call_count")
    returned = value.get("returned_call_count")
    truncated = value.get("truncated")
    if (
        isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision < 1
        or isinstance(total, bool)
        or not isinstance(total, int)
        or total < 0
        or isinstance(returned, bool)
        or not isinstance(returned, int)
        or not 0 <= returned <= 100
        or not isinstance(truncated, bool)
    ):
        raise ValueError("llm_usage count fields are invalid")
    state = value.get("state")
    if state not in {"OBSERVED", "PARTIAL", "NO_LLM", "UNAVAILABLE"}:
        raise ValueError("llm_usage state is invalid")
    generator = value.get("adopted_generator")
    if generator not in {"opus", "ds_flash", "safe", None}:
        raise ValueError("llm_usage adopted generator is invalid")
    calls = value.get("calls")
    if not isinstance(calls, list) or len(calls) != returned:
        raise ValueError("llm_usage returned count is inconsistent")
    if truncated:
        if total <= 100 or returned != 100:
            raise ValueError("truncated llm_usage counts are inconsistent")
    elif total != returned:
        raise ValueError("untruncated llm_usage counts are inconsistent")
    if state in {"OBSERVED", "PARTIAL"} and total < 1:
        raise ValueError("observed llm_usage requires calls")
    if state in {"NO_LLM", "UNAVAILABLE"} and (
        total != 0 or returned != 0 or truncated or calls
    ):
        raise ValueError("empty llm_usage state cannot contain calls")
    if state == "UNAVAILABLE" and generator is not None:
        raise ValueError("unavailable llm_usage cannot name a generator")
    copied_calls: list[dict[str, Any]] = []
    for call in calls:
        if not isinstance(call, dict) or set(call) != _LLM_CALL_FIELDS:
            raise ValueError("llm call shape is invalid")
        if (
            isinstance(call.get("sequence"), bool)
            or not isinstance(call.get("sequence"), int)
            or call["sequence"] < 0
            or isinstance(call.get("attempt"), bool)
            or not isinstance(call.get("attempt"), int)
            or call["attempt"] < 1
            or isinstance(call.get("publish_retry_round"), bool)
            or not isinstance(call.get("publish_retry_round"), int)
            or call["publish_retry_round"] < 0
        ):
            raise ValueError("llm call numeric fields are invalid")
        copied = dict(call)
        copied["role"], copied["purpose"] = public_llm_role_purpose(
            copied.get("role"), copied.get("purpose")
        )
        provider = copied.get("provider")
        model = copied.get("model")
        if (
            not isinstance(provider, str)
            or not 1 <= len(provider) <= 40
            or not isinstance(model, str)
            or not 1 <= len(model) <= 120
            or copied.get("role") not in _LLM_ROLES
            or copied.get("purpose") not in _LLM_PURPOSES
            or copied.get("status") not in _LLM_STATUSES
        ):
            raise ValueError("llm call safe fields are invalid")
        copied_calls.append(copied)
    return {**value, "calls": copied_calls}


def _validated_failure_observation(value: Any) -> dict[str, Any]:
    """Validate and copy only the frozen safe machine-failure contract."""
    if not isinstance(value, dict) or set(value) != _FAILURE_FIELDS:
        raise ValueError("failure_observation shape is invalid")
    if value.get("schema_version") != 1:
        raise ValueError("failure_observation schema version is invalid")
    revision = value.get("observation_revision")
    stage = value.get("failure_stage")
    if (
        value.get("evidence_state") not in {"AVAILABLE", "PARTIAL", "UNAVAILABLE"}
        or isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision < 1
        or not isinstance(stage, str)
        or not 1 <= len(stage) <= 120
        or value.get("category") not in _FAILURE_CATEGORIES
    ):
        raise ValueError("failure_observation header is invalid")
    reasons = value.get("reasons")
    if not isinstance(reasons, list) or len(reasons) > 50:
        raise ValueError("failure_observation reasons are invalid")
    copied_reasons = []
    for reason in reasons:
        if not isinstance(reason, dict) or set(reason) != _REASON_FIELDS:
            raise ValueError("failure_observation reason shape is invalid")
        count = reason.get("count")
        indexes = reason.get("plan_indexes")
        if (
            reason.get("code") not in _SAFE_REASON_CODES
            or isinstance(count, bool)
            or not isinstance(count, int)
            or count < 1
            or not isinstance(indexes, list)
            or len(indexes) > 20
            or len(indexes) != len(set(indexes))
            or any(
                isinstance(index, bool)
                or not isinstance(index, int)
                or index < 1
                for index in indexes
            )
        ):
            raise ValueError("failure_observation reason value is invalid")
        copied_reasons.append({**reason, "plan_indexes": list(indexes)})
    rewrite = value.get("automatic_rewrite")
    if not isinstance(rewrite, dict) or set(rewrite) != _REWRITE_FIELDS:
        raise ValueError("automatic_rewrite shape is invalid")
    eligible = rewrite.get("eligible")
    attempted = rewrite.get("attempted")
    rounds = rewrite.get("completed_rounds")
    skip = rewrite.get("not_attempted_reason")
    if (
        not isinstance(eligible, bool)
        or not isinstance(attempted, bool)
        or isinstance(rounds, bool)
        or not isinstance(rounds, int)
        or not 0 <= rounds <= 1
    ):
        raise ValueError("automatic_rewrite values are invalid")
    if attempted:
        if not eligible or rounds != 1 or skip is not None:
            raise ValueError("attempted automatic_rewrite is inconsistent")
    elif rounds != 0 or skip not in _REWRITE_SKIP_REASONS:
        raise ValueError("unattempted automatic_rewrite is inconsistent")
    return {
        **value,
        "reasons": copied_reasons,
        "automatic_rewrite": dict(rewrite),
    }

SAFE_ERROR_MESSAGES = {
    "PUBLISH_GATE_FAILED": "攻略未通过发布校验",
    "SAFE_RENDER_FAILED": "基础行程渲染失败",
    "TIMEOUT": "规划超时",
    "LLM_ERROR": "规划服务暂时不可用",
    "DB_ERROR": "规划保存失败",
    "WORKFLOW_ERROR": "规划流程失败",
    "UNKNOWN": "规划失败",
    "CANCELLED": "规划已取消",
    "CITY_PREPARING": "城市数据准备中",
    "CITY_COLLECTION_FAILED": "城市数据采集失败",
    "CITY_DATA_INSUFFICIENT": "城市数据不足",
    "CITY_DISABLED": "城市暂不可用",
    "CITY_CLARIFICATION_REQUIRED": "需要补充目的地城市",
}
DETAILED_REASON_BY_ERROR_CODE = {
    "PUBLISH_GATE_FAILED": "publish_gate_failed",
    "SAFE_RENDER_FAILED": "safe_render_failed",
    "TIMEOUT": "timeout",
    "LLM_ERROR": "llm_error",
    "DB_ERROR": "db_error",
    "WORKFLOW_ERROR": "workflow_error",
    "UNKNOWN": "unknown",
    "CANCELLED": "cancelled",
    "CITY_PREPARING": "city_preparing",
    "CITY_COLLECTION_FAILED": "city_collection_failed",
    "CITY_DATA_INSUFFICIENT": "city_data_insufficient",
    "CITY_DISABLED": "city_disabled",
    "CITY_CLARIFICATION_REQUIRED": "city_clarification_required",
}


async def insert_trip_step(
    session: AsyncSession,
    *,
    job_id: str,
    stage: str,
    status: str,
    attempt: int = 1,
    publish_retry_round: int = 0,
    latency_ms: int | None = None,
    metadata: dict[str, Any] | None = None,
    finished: bool = False,
) -> int:
    """Insert one source step at projection version 1 in the caller transaction."""
    row = (
        await session.execute(
            text(
                """
                INSERT INTO travel_trip_job_step (
                    job_id, stage, attempt, publish_retry_round, status,
                    started_time, finished_time, latency_ms, metadata,
                    projection_version
                ) VALUES (
                    :job_id, :stage, :attempt, :publish_retry_round, :status,
                    NOW(), CASE WHEN :finished THEN NOW() ELSE NULL END,
                    :latency_ms, CAST(:metadata AS jsonb), 1
                )
                RETURNING id
                """
            ),
            {
                "job_id": job_id,
                "stage": stage,
                "attempt": attempt,
                "publish_retry_round": publish_retry_round,
                "status": status,
                "finished": finished,
                "latency_ms": latency_ms,
                "metadata": json.dumps(metadata or {}, ensure_ascii=False),
            },
        )
    ).one()
    return int(row.id)


async def finish_latest_running_step(
    session: AsyncSession,
    *,
    job_id: str,
    stage: str,
    status: str,
    attempt: int = 1,
    publish_retry_round: int = 0,
    latency_ms: int | None = None,
    metadata: dict[str, Any] | None = None,
) -> int | None:
    """Finish the newest matching RUNNING step and advance its version once."""
    row = (
        await session.execute(
            text(
                """
                WITH target AS (
                    SELECT id
                    FROM travel_trip_job_step
                    WHERE job_id = :job_id
                      AND stage = :stage
                      AND attempt = :attempt
                      AND publish_retry_round = :publish_retry_round
                      AND status = 'RUNNING'
                    ORDER BY started_time DESC, id DESC
                    LIMIT 1
                    FOR UPDATE
                )
                UPDATE travel_trip_job_step step
                SET status = :status,
                    finished_time = NOW(),
                    latency_ms = :latency_ms,
                    metadata = step.metadata || CAST(:metadata AS jsonb),
                    projection_version = step.projection_version + 1
                FROM target
                WHERE step.id = target.id
                RETURNING step.id
                """
            ),
            {
                "job_id": job_id,
                "stage": stage,
                "attempt": attempt,
                "publish_retry_round": publish_retry_round,
                "status": status,
                "latency_ms": latency_ms,
                "metadata": json.dumps(metadata or {}, ensure_ascii=False),
            },
        )
    ).one_or_none()
    return int(row.id) if row is not None else None


async def close_all_running_steps(
    session: AsyncSession,
    *,
    job_id: str,
    status: str,
    metadata: dict[str, Any] | None = None,
) -> list[int]:
    """Close every RUNNING step as one part of a terminal source transaction."""
    if status not in {"SUCCESS", "FAILED", "TIMEOUT"}:
        raise ValueError("running trip steps require a terminal step status")
    rows = (
        await session.execute(
            text(
                """
                UPDATE travel_trip_job_step
                SET status = :status,
                    finished_time = NOW(),
                    latency_ms = GREATEST(
                        0,
                        CAST(EXTRACT(EPOCH FROM (NOW() - started_time)) * 1000 AS INT)
                    ),
                    metadata = metadata || CAST(:metadata AS jsonb),
                    projection_version = projection_version + 1
                WHERE job_id = :job_id
                  AND status = 'RUNNING'
                RETURNING id
                """
            ),
            {
                "job_id": job_id,
                "status": status,
                "metadata": json.dumps(metadata or {}, ensure_ascii=False),
            },
        )
    ).all()
    return [int(row.id) for row in rows]


def iso_utc(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def nullable_iso_utc(value: datetime | None) -> str | None:
    return iso_utc(value) if value is not None else None


def compute_guide_result_state(
    *,
    status: str,
    result_type: str | None,
    result_record_id: int | None,
) -> str:
    """Return the frozen Guide Result State truth-table outcome."""
    if status == "SUCCESS":
        if result_type == "PLAN_READY" and result_record_id is not None:
            return "AVAILABLE"
        if (
            result_type in {"NO_CANDIDATES", "NO_USABLE_ROUTE"}
            and result_record_id is None
        ):
            return "LEGAL_NO_GUIDE"
        return "INCONSISTENT"
    if result_type is None and result_record_id is None:
        return "NOT_APPLICABLE"
    return "INCONSISTENT"


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _safe_days(request_data: dict[str, Any]) -> int | None:
    raw = request_data.get("days")
    if isinstance(raw, bool):
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if 1 <= value <= 30 else None


def _duration_ms(
    *,
    started_at: datetime,
    finished_at: datetime | None,
    latency_ms: int | None,
) -> int | None:
    if latency_ms is not None:
        return max(0, int(latency_ms))
    if finished_at is None:
        return None
    start = started_at
    end = finished_at
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    return max(0, int((end - start).total_seconds() * 1000))


def validate_projection_event(event: dict[str, Any]) -> None:
    """Enforce application invariants and the payload privacy allowlist."""
    schema_version = event.get("schema_version")
    if schema_version not in {"1.0", "1.1", "1.2"}:
        raise ValueError("projection schema version is unsupported")
    payload = event.get("payload")
    if not isinstance(payload, dict) or set(payload) != {"job", "changed_steps"}:
        raise ValueError("projection payload shape is invalid")
    job = payload.get("job")
    steps = payload.get("changed_steps")
    if (
        not isinstance(job, dict)
        or not JOB_PAYLOAD_FIELDS.issubset(job)
        or set(job) - JOB_PAYLOAD_FIELDS - OPTIONAL_JOB_PAYLOAD_FIELDS
    ):
        raise ValueError("projection job payload is not allowlisted")
    if "failure_observation" in job:
        _validated_failure_observation(job["failure_observation"])
    v012_fields = {"generation_policy_version", "delivery", "route_diagnostics"}
    if schema_version in {"1.0", "1.1"} and v012_fields.intersection(job):
        raise ValueError("projection 1.0/1.1 cannot contain v0.12 job fields")
    if schema_version == "1.2":
        if job.get("generation_policy_version") != "v0.12" or "delivery" not in job:
            raise ValueError("projection 1.2 v0.12 job requires policy and delivery")
        projected_delivery = GenerationDelivery.model_validate(job["delivery"])
        validate_delivery_for_job(
            projected_delivery,
            status=str(job.get("status") or ""),
            result_type=job.get("result_type"),
            result_record_id=job.get("result_record_id"),
            published_variant=(
                "normal" if projected_delivery.outcome == "DETAILED"
                else "safe" if projected_delivery.outcome == "BASIC"
                else None
            ),
            delivery_status=(
                "NORMAL" if projected_delivery.outcome == "DETAILED"
                else "DEGRADED" if projected_delivery.outcome == "BASIC"
                else None
            ),
        )
        if "route_diagnostics" in job:
            RouteDiagnostics.model_validate(job["route_diagnostics"])
    if not isinstance(steps, list) or len(steps) > MAX_CHANGED_STEPS:
        raise ValueError("projection changed_steps exceeds the contract bound")
    if event.get("aggregate_id") != job.get("job_id"):
        raise ValueError("aggregate_id must equal payload.job.job_id")
    if event.get("aggregate_version") != job.get("source_version"):
        raise ValueError("aggregate_version must equal payload.job.source_version")
    for step in steps:
        if (
            not isinstance(step, dict)
            or not STEP_PAYLOAD_FIELDS.issubset(step)
            or set(step) - STEP_PAYLOAD_FIELDS - OPTIONAL_STEP_PAYLOAD_FIELDS
        ):
            raise ValueError("projection step payload is not allowlisted")
        if step.get("job_id") != job.get("job_id"):
            raise ValueError("projection step belongs to another job")
        if "llm_usage" in step:
            _validated_llm_usage(step["llm_usage"])
        if "execution_observation" in step:
            if schema_version != "1.2":
                raise ValueError("execution_observation requires projection 1.2")
            ExecutionObservation.model_validate(step["execution_observation"])
    safe_error = job.get("safe_error")
    if job.get("error_code") is None:
        if safe_error is not None:
            raise ValueError("safe_error must be null with a null error_code")
    elif not isinstance(safe_error, dict) or safe_error.get("code") != job.get(
        "error_code"
    ):
        raise ValueError("safe_error.code must equal error_code")

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            forbidden = FORBIDDEN_PAYLOAD_KEYS.intersection(value)
            if forbidden:
                raise ValueError(
                    f"projection payload contains forbidden keys: {sorted(forbidden)}"
                )
            for nested in value.values():
                walk(nested)
        elif isinstance(value, list):
            for nested in value:
                walk(nested)

    walk(payload)


def _project_job_snapshot_row(row: Any) -> dict[str, Any]:
    request_data = _json_object(row["trip_request_json"])
    error_code = row["error_code"]
    safe_error = None
    if error_code is not None:
        safe_error = {
            "code": str(error_code),
            "message": SAFE_ERROR_MESSAGES.get(str(error_code), "规划失败"),
        }
    projected = {
        "source_id": int(row["source_id"]),
        "job_id": str(row["job_id"]),
        "source_version": int(row["source_version"]),
        "source": str(row["source"]),
        "city": str(request_data.get("to_city") or "").strip() or None,
        "days": _safe_days(request_data),
        "status": str(row["status"]),
        "current_stage": (
            str(row["current_stage"]) if row["current_stage"] is not None else None
        ),
        "result_type": row["result_type"],
        "result_record_id": (
            int(row["result_record_id"])
            if row["result_record_id"] is not None
            else None
        ),
        "generator": (
            str(row["generator"]) if row["generator"] is not None else None
        ),
        "guide_result_state": str(row["guide_result_state"]),
        "error_code": str(error_code) if error_code is not None else None,
        "safe_error": safe_error,
        "detailed_reason": DETAILED_REASON_BY_ERROR_CODE.get(str(error_code or "")),
        "created_at": iso_utc(row["created_time"]),
        "started_at": nullable_iso_utc(row["started_time"]),
        "finished_at": nullable_iso_utc(row["finished_time"]),
        "retry_count": int(row["retry_count"] or 0),
        "failed_draft_available": bool(row["failed_draft_available"]),
        "trace_completeness": str(row["trace_completeness"]),
        "source_updated_at": iso_utc(row["source_updated_at"]),
    }
    failure_observation = _json_object(row.get("failure_observation"))
    if failure_observation and "evidence_state" in failure_observation:
        projected["failure_observation"] = _validated_failure_observation(
            failure_observation
        )
    policy = row.get("generation_policy_version") or "legacy"
    if policy == "v0.12":
        raw_delivery = _json_object(row.get("delivery_json"))
        delivery = GenerationDelivery.model_validate(raw_delivery)
        validate_delivery_for_job(
            delivery,
            status=str(row["status"]),
            result_type=row["result_type"],
            result_record_id=row["result_record_id"],
            published_variant=row.get("published_variant"),
            delivery_status=row.get("delivery_status"),
        )
        projected["generation_policy_version"] = "v0.12"
        projected["delivery"] = delivery.model_dump(mode="json")
        raw_diagnostics = row.get("route_diagnostics")
        if raw_diagnostics is not None:
            diagnostics = RouteDiagnostics.model_validate(
                _json_object(raw_diagnostics)
            )
            expires_at = row.get("route_replay_expires_at")
            if expires_at is not None:
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=timezone.utc)
                if expires_at <= datetime.now(timezone.utc):
                    diagnostics.replay_available = False
            projected["route_diagnostics"] = diagnostics.model_dump(mode="json")
    return projected


_JOB_SNAPSHOT_SELECT = """
    SELECT
        j.id AS source_id,
        j.job_id,
        j.projection_version AS source_version,
        j.source,
        j.trip_request_json,
        j.status,
        j.current_stage,
        j.result_type,
        j.result_record_id,
        j.generation_policy_version,
        j.delivery_json,
        j.route_diagnostics,
        j.route_replay_expires_at,
        CASE
            WHEN MAX(p.quality_metrics ->> 'generator') IN (
                'opus', 'ds_flash', 'safe'
            ) THEN MAX(p.quality_metrics ->> 'generator')
            WHEN MAX(p.quality_metrics ->> 'published_variant') = 'safe'
                THEN 'safe'
            ELSE NULL
        END AS generator,
        MAX(p.quality_metrics ->> 'published_variant') AS published_variant,
        MAX(p.quality_metrics ->> 'delivery_status') AS delivery_status,
        j.guide_result_state,
        j.error_code,
        j.created_time,
        j.started_time,
        j.finished_time,
        j.trace_completeness,
        j.updated_time AS source_updated_at,
        (
            SELECT observed.metadata -> 'failure_observation'
            FROM travel_trip_job_step observed
            WHERE observed.job_id = j.job_id
              AND observed.metadata ? 'failure_observation'
            ORDER BY observed.id DESC
            LIMIT 1
        ) AS failure_observation,
        COUNT(DISTINCT s.id) FILTER (
            WHERE s.attempt > 1 OR s.publish_retry_round > 0
        )::int AS retry_count,
        EXISTS (
            SELECT 1
            FROM travel_trip_failed_draft d
            WHERE d.job_id = j.job_id
              AND j.status IN ('FAILED', 'TIMEOUT')
              AND COALESCE(j.result_type, '') NOT IN (
                  'NO_CANDIDATES', 'NO_USABLE_ROUTE'
              )
        ) AS failed_draft_available
    FROM travel_trip_job j
    LEFT JOIN travel_trip_job_step s ON s.job_id = j.job_id
    LEFT JOIN travel_plan_record p ON p.id = j.result_record_id
"""


async def _job_snapshot(
    session: AsyncSession,
    job_id: str,
) -> dict[str, Any]:
    row = (
        await session.execute(
            text(
                f"""
                {_JOB_SNAPSHOT_SELECT}
                WHERE j.job_id = :job_id
                GROUP BY j.id
                """
            ),
            {"job_id": job_id},
        )
    ).mappings().one()
    return _project_job_snapshot_row(row)


async def _step_snapshots(
    session: AsyncSession,
    *,
    job_id: str,
    step_ids: list[int],
    include_v012: bool = False,
) -> list[dict[str, Any]]:
    if not step_ids:
        return []
    rows = (
        await session.execute(
            text(
                """
                SELECT id, job_id, projection_version, stage, status, attempt,
                       publish_retry_round, started_time, finished_time,
                       latency_ms, metadata, updated_time
                FROM travel_trip_job_step
                WHERE id = ANY(CAST(:step_ids AS bigint[]))
                ORDER BY id ASC
                """
            ),
            {"step_ids": step_ids},
        )
    ).mappings().all()
    if len(rows) != len(step_ids):
        raise ValueError("changed step snapshot is incomplete")
    snapshots: list[dict[str, Any]] = []
    for row in rows:
        if str(row["job_id"]) != job_id:
            raise ValueError("changed step belongs to another job")
        snapshots.append(_project_step_snapshot_row(row, include_v012=include_v012))
    return snapshots


def _project_step_snapshot_row(row: Any, *, include_v012: bool = False) -> dict[str, Any]:
    projected = {
        "source_step_id": int(row["id"]),
        "job_id": str(row["job_id"]),
        "source_version": int(row["projection_version"]),
        "stage": str(row["stage"]),
        "status": str(row["status"]),
        "attempt": int(row["attempt"]),
        "publish_retry_round": int(row["publish_retry_round"]),
        "started_at": iso_utc(row["started_time"]),
        "finished_at": nullable_iso_utc(row["finished_time"]),
        "duration_ms": _duration_ms(
            started_at=row["started_time"],
            finished_at=row["finished_time"],
            latency_ms=row["latency_ms"],
        ),
        "source_updated_at": iso_utc(row["updated_time"]),
    }
    metadata = _json_object(row.get("metadata"))
    direct_usage = metadata.get("llm_usage")
    if direct_usage is not None:
        usage = _validated_llm_usage(direct_usage)
    elif str(row["status"]) != "RUNNING":
        usage = _validated_llm_usage(
            project_persisted_llm_usage_for_stage(
                metadata.get("llm_observation"),
                stage=str(row["stage"]),
                attempt=int(row["attempt"]),
                publish_retry_round=int(row["publish_retry_round"]),
                adopted_generator=(
                    str(metadata.get("adopted_generator"))
                    if metadata.get("adopted_generator") in {
                        "opus", "ds_flash", "safe"
                    }
                    else None
                ),
            )
        )
    else:
        usage = None
    if usage is not None:
        if str(row["stage"]) == "POI_SELECTION":
            source = metadata.get("selection_source")
            reason = metadata.get("selection_fallback_reason")
            if source is not None:
                usage = _validated_llm_usage({
                    **usage,
                    "selection_source": source,
                    "selection_fallback_reason": reason,
                })
        projected["llm_usage"] = usage
    if include_v012 and metadata.get("execution_observation") is not None:
        projected["execution_observation"] = ExecutionObservation.model_validate(
            metadata["execution_observation"]
        ).model_dump(mode="json")
    return projected


async def get_trip_job_snapshot_page(
    *,
    after_id: int,
    snapshot_max_id: int | None,
    limit: int,
) -> dict[str, Any]:
    """Return a frozen-ID keyset page of safe job source snapshots."""
    factory = get_session_factory()
    async with factory() as session:
        effective_max = snapshot_max_id
        if effective_max is None:
            effective_max = int(
                (
                    await session.execute(
                        text("SELECT COALESCE(MAX(id), 0)::bigint FROM travel_trip_job")
                    )
                ).scalar_one()
            )
        rows = (
            await session.execute(
                text(
                    f"""
                    {_JOB_SNAPSHOT_SELECT}
                    WHERE j.id > :after_id
                      AND j.id <= :snapshot_max_id
                    GROUP BY j.id
                    ORDER BY j.id ASC
                    LIMIT :fetch_limit
                    """
                ),
                {
                    "after_id": after_id,
                    "snapshot_max_id": effective_max,
                    "fetch_limit": limit + 1,
                },
            )
        ).mappings().all()
    has_more = len(rows) > limit
    visible = rows[:limit]
    items = [_project_job_snapshot_row(row) for row in visible]
    return {
        "snapshot_max_id": effective_max,
        "next_after_id": items[-1]["source_id"] if has_more else None,
        "has_more": has_more,
        "items": items,
    }


async def get_trip_step_snapshot_page(
    *,
    after_id: int,
    snapshot_max_id: int | None,
    limit: int,
) -> dict[str, Any]:
    """Return a frozen-ID keyset page of safe step source snapshots."""
    factory = get_session_factory()
    async with factory() as session:
        effective_max = snapshot_max_id
        if effective_max is None:
            effective_max = int(
                (
                    await session.execute(
                        text(
                            "SELECT COALESCE(MAX(id), 0)::bigint "
                            "FROM travel_trip_job_step"
                        )
                    )
                ).scalar_one()
            )
        rows = (
            await session.execute(
                text(
                    """
                    SELECT step.id, step.job_id, step.projection_version,
                           step.stage, step.status, step.attempt,
                           step.publish_retry_round, step.started_time,
                           step.finished_time, step.latency_ms, step.metadata,
                           step.updated_time, job.generation_policy_version
                    FROM travel_trip_job_step AS step
                    JOIN travel_trip_job AS job ON job.job_id = step.job_id
                    WHERE step.id > :after_id
                      AND step.id <= :snapshot_max_id
                    ORDER BY step.id ASC
                    LIMIT :fetch_limit
                    """
                ),
                {
                    "after_id": after_id,
                    "snapshot_max_id": effective_max,
                    "fetch_limit": limit + 1,
                },
            )
        ).mappings().all()
    has_more = len(rows) > limit
    visible = rows[:limit]
    items = [
        _project_step_snapshot_row(
            row,
            include_v012=row.get("generation_policy_version") == "v0.12",
        )
        for row in visible
    ]
    return {
        "snapshot_max_id": effective_max,
        "next_after_id": items[-1]["source_step_id"] if has_more else None,
        "has_more": has_more,
        "items": items,
    }


async def emit_trip_projection_commit(
    session: AsyncSession,
    *,
    job_id: str,
    changed_step_ids: Iterable[int] = (),
    advance_job_version: bool = True,
) -> dict[str, Any]:
    """Create one complete commit event inside the caller's transaction."""
    step_ids = sorted({int(step_id) for step_id in changed_step_ids})
    if len(step_ids) > MAX_CHANGED_STEPS:
        raise ValueError("one source transaction changed more than 500 steps")

    state_row = (
        await session.execute(
            text(
                """
                SELECT status, result_type, result_record_id
                FROM travel_trip_job
                WHERE job_id = :job_id
                FOR UPDATE
                """
            ),
            {"job_id": job_id},
        )
    ).mappings().one()
    guide_result_state = compute_guide_result_state(
        status=str(state_row["status"]),
        result_type=state_row["result_type"],
        result_record_id=state_row["result_record_id"],
    )
    version_sql = ", projection_version = projection_version + 1" if advance_job_version else ""
    await session.execute(
        text(
            f"""
            UPDATE travel_trip_job
            SET guide_result_state = :guide_result_state{version_sql}
            WHERE job_id = :job_id
            """
        ),
        {"job_id": job_id, "guide_result_state": guide_result_state},
    )

    job = await _job_snapshot(session, job_id)
    changed_steps = await _step_snapshots(
        session,
        job_id=job_id,
        step_ids=step_ids,
        include_v012=job.get("generation_policy_version") == "v0.12",
    )
    allocation = (
        await session.execute(
            text(
                """
                UPDATE projection_stream_head
                SET head_sequence = head_sequence + 1,
                    updated_time = NOW()
                WHERE stream_id = 1
                RETURNING head_sequence, updated_time
                """
            )
        )
    ).mappings().one()
    event = {
        "event_id": str(uuid.uuid4()),
        "event_type": EVENT_TYPE,
        "schema_version": (
            EVENT_SCHEMA_VERSION
            if job.get("generation_policy_version") == "v0.12"
            else "1.1"
        ),
        "outbox_sequence": int(allocation["head_sequence"]),
        "aggregate_type": AGGREGATE_TYPE,
        "aggregate_id": job_id,
        "aggregate_version": int(job["source_version"]),
        "occurred_at": iso_utc(allocation["updated_time"]),
        "payload": {"job": job, "changed_steps": changed_steps},
    }
    validate_projection_event(event)
    await session.execute(
        text(
            """
            INSERT INTO trip_projection_outbox (
                outbox_sequence, event_id, event_type, schema_version,
                aggregate_type, aggregate_id, aggregate_version,
                occurred_at, payload
            ) VALUES (
                :outbox_sequence, CAST(:event_id AS uuid), :event_type,
                :schema_version, :aggregate_type, :aggregate_id,
                :aggregate_version, :occurred_at, CAST(:payload AS jsonb)
            )
            """
        ),
        {
            **{key: value for key, value in event.items() if key != "payload"},
            # asyncpg requires a datetime for TIMESTAMPTZ binds; the event dict
            # keeps the ISO string for broker/JSON consumers.
            "occurred_at": allocation["updated_time"],
            "payload": json.dumps(event["payload"], ensure_ascii=False),
        },
    )
    return event


def json_safe(value: Any) -> Any:
    """Small helper for metrics/debug responses without leaking object internals."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, list):
        return [json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    return str(value)
