"""Persistence and queries for travel_trip_job (async trip planning)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from src.jobs.projection_outbox import (
    close_all_running_steps,
    emit_trip_projection_commit,
    finish_latest_running_step,
    insert_trip_step,
)
from src.pipeline.db import get_session_factory
from src.jobs.generation_delivery import (
    GenerationDelivery,
    GenerationPolicyVersion,
    RouteDiagnostics,
    RouteSearchObservation,
    pending_delivery,
    validate_delivery_for_job,
)

logger = logging.getLogger(__name__)

TERMINAL_STATUSES = frozenset({"SUCCESS", "FAILED", "TIMEOUT", "REJECTED"})
READ_RETRIES = 3

STAGE_USER_MESSAGES: dict[str, str] = {
    "PENDING": "已收到请求，正在排队规划",
    "INTENT_PARSER": "正在理解你的旅行需求",
    "DATA_RETRIEVAL": "正在检索地点数据",
    "SEMANTIC_GROUPING": "正在整理候选方案",
    "POI_SELECTION": "正在筛选攻略地点",
    "ROUTE_PLANNING": "正在优化每日路线",
    "FINAL_WRITER": "正在生成攻略",
    "HERMES_REVIEW": "正在审核攻略内容",
    "REVIEW_TAXONOMY": "正在归类攻略质量问题",
    "WRITER_REPAIR": "正在修复可修复的攻略问题",
    "REVIEW_TAXONOMY_AFTER_REPAIR": "正在复核修复后的攻略",
    "PUBLISH_RETRY": "正在重新生成一版更稳的攻略",
    "PERSISTING": "正在保存结果",
    "SUCCESS": "攻略已完成",
    "FAILED": "这次规划失败了，可以稍后再试一次",
    "TIMEOUT": "这次规划超时了，可以稍后再试一次",
}

NO_CANDIDATES_USER_MESSAGE = "暂时没有找到足够的真实地点数据"
NO_USABLE_ROUTE_USER_MESSAGE = "现有地点无法组成合规路线，请调整需求后重试"
REJECTED_USER_MESSAGES = {
    "CITY_CLARIFICATION_REQUIRED": (
        "我还差一个关键信息：你想去哪个城市？"
        "可以直接发“成都3天美食”或“福州3天想去平潭”，"
        "我再继续帮你规划。"
    ),
}


async def _lock_conversation(session, *, source: str, conversation_id: str) -> None:
    """Serialize current-recommendation transitions for one conversation."""
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
        {
            "lock_key": json.dumps(
                [source, conversation_id],
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        },
    )


class RequestIdConflictError(Exception):
    """Same request_id with a different message."""


class GenerationPolicyNotEnabledError(Exception):
    """A recognized policy cannot create new work in this release slice."""


@dataclass(frozen=True)
class TripJobRecord:
    job_id: str
    request_id: str
    source: str
    conversation_id: str
    user_query: str
    status: str
    current_stage: str
    result_type: str | None
    trip_request_json: dict | None
    result_record_id: int | None
    reply_text: str | None
    plan_count: int | None
    error_message: str | None
    error_code: str | None
    created_time: datetime
    started_time: datetime | None
    finished_time: datetime | None
    updated_time: datetime
    user_display_name: str | None = None
    generation_policy_version: GenerationPolicyVersion = "legacy"
    delivery: GenerationDelivery | None = None


@dataclass(frozen=True)
class AsyncTripJobCreateResult:
    job_id: str
    status: str
    current_stage: str
    queue_position: int
    message: str
    cached: bool
    generation_policy_version: GenerationPolicyVersion = "legacy"
    delivery: GenerationDelivery | None = None


def _normalize_message(message: str) -> str:
    return message.strip()


def _structured_trip_request_query(trip_request_json: dict) -> str:
    parts: list[str] = []
    for key in ("from_city", "to_city", "start_date", "end_date"):
        value = trip_request_json.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    for key, label in (("days", "days"), ("people_count", "people")):
        value = trip_request_json.get(key)
        if value not in (None, ""):
            parts.append(f"{label}:{value}")
    for key, label in (("preferences", "preferences"), ("avoid", "avoid")):
        value = trip_request_json.get(key)
        if isinstance(value, list) and value:
            parts.append(f"{label}:{','.join(str(item) for item in value)}")
    notes = trip_request_json.get("notes")
    if isinstance(notes, str) and notes.strip():
        parts.append(notes.strip())
    if parts:
        return " | ".join(parts)
    return json.dumps(trip_request_json, ensure_ascii=False, sort_keys=True)


def _effective_user_query(
    *,
    message: str | None,
    trip_request_json: dict | None,
) -> str:
    if trip_request_json is not None:
        return _structured_trip_request_query(trip_request_json)
    return _normalize_message(message or "")


def _normalized_query_hash(message: str) -> str:
    normalized = _normalize_message(message)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _row_to_record(row) -> TripJobRecord:
    trip_request_json = row.trip_request_json
    if isinstance(trip_request_json, str):
        import json

        trip_request_json = json.loads(trip_request_json) if trip_request_json else None

    raw_delivery = getattr(row, "delivery_json", None)
    if isinstance(raw_delivery, str):
        raw_delivery = json.loads(raw_delivery) if raw_delivery else None
    policy = getattr(row, "generation_policy_version", None) or "legacy"
    if policy == "v0.12" and raw_delivery is None:
        raise ValueError("v0.12 job is missing delivery_json")

    return TripJobRecord(
        job_id=row.job_id,
        request_id=row.request_id,
        source=row.source,
        conversation_id=row.conversation_id,
        user_display_name=getattr(row, "user_display_name", None),
        user_query=row.user_query,
        status=row.status,
        current_stage=row.current_stage,
        result_type=row.result_type,
        trip_request_json=trip_request_json,
        result_record_id=row.result_record_id,
        reply_text=row.reply_text,
        plan_count=row.plan_count,
        error_message=row.error_message,
        error_code=row.error_code,
        created_time=row.created_time,
        started_time=row.started_time,
        finished_time=row.finished_time,
        updated_time=row.updated_time,
        generation_policy_version=policy,
        delivery=(
            GenerationDelivery.model_validate(raw_delivery)
            if policy == "v0.12" and raw_delivery is not None
            else None
        ),
    )


def _coerce_place_ids(raw_place_ids) -> set[int]:
    """Normalize JSONB place IDs loaded from a historical plan record."""
    if isinstance(raw_place_ids, str):
        try:
            raw_place_ids = json.loads(raw_place_ids)
        except json.JSONDecodeError:
            return set()
    if not isinstance(raw_place_ids, list):
        return set()

    place_ids = set()
    for place_id in raw_place_ids:
        if isinstance(place_id, bool):
            continue
        try:
            place_ids.add(int(place_id))
        except (TypeError, ValueError):
            continue
    return place_ids


def route_failure_code_for_job(job: TripJobRecord) -> str | None:
    from src.agents.route_notices import COUPLED_ROUTE_FAILURE_NOTICES
    if job.status != "SUCCESS" or job.result_type != "NO_USABLE_ROUTE":
        return None
    code = (job.trip_request_json or {}).get("route_failure_code")
    return code if isinstance(code, str) and code in COUPLED_ROUTE_FAILURE_NOTICES else None


def user_message_for_job(job: TripJobRecord) -> str:
    if job.status == "REJECTED":
        return (
            job.error_message
            or REJECTED_USER_MESSAGES.get(job.error_code or "")
            or job.error_code
            or "CITY_GATE_REJECTED"
        )
    if job.status == "SUCCESS" and job.result_type == "NO_CANDIDATES":
        return NO_CANDIDATES_USER_MESSAGE
    if job.status == "SUCCESS" and job.result_type == "NO_USABLE_ROUTE":
        from src.agents.route_notices import COUPLED_ROUTE_FAILURE_NOTICES
        code = route_failure_code_for_job(job)
        return COUPLED_ROUTE_FAILURE_NOTICES[code] if code else NO_USABLE_ROUTE_USER_MESSAGE
    if job.status == "FAILED" and job.error_message:
        return job.error_message
    if job.status in STAGE_USER_MESSAGES:
        return STAGE_USER_MESSAGES[job.status]
    return STAGE_USER_MESSAGES.get(job.current_stage, "正在处理你的旅行规划")


def _elapsed_ms(start: datetime, end: datetime) -> int:
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)

    return max(0, int((end - start).total_seconds() * 1000))


def compute_queue_wait_ms(job: TripJobRecord) -> int:
    end = job.started_time or job.finished_time or datetime.now(timezone.utc)
    return _elapsed_ms(job.created_time, end)


def compute_run_elapsed_ms(job: TripJobRecord) -> int:
    if job.started_time is None:
        return 0
    end = job.finished_time or datetime.now(timezone.utc)
    return _elapsed_ms(job.started_time, end)


def compute_total_elapsed_ms(job: TripJobRecord) -> int:
    end = job.finished_time or datetime.now(timezone.utc)
    return _elapsed_ms(job.created_time, end)


def compute_elapsed_ms(job: TripJobRecord) -> int:
    if job.started_time is None:
        return compute_total_elapsed_ms(job)
    return compute_run_elapsed_ms(job)


async def get_trip_job_by_id(job_id: str) -> TripJobRecord | None:
    factory = get_session_factory()
    last_error: Exception | None = None
    for attempt in range(1, READ_RETRIES + 1):
        try:
            async with factory() as session:
                result = await session.execute(
                    text("""
                        SELECT job_id, request_id, source, conversation_id,
                               user_display_name, user_query,
                               status, current_stage, result_type, trip_request_json,
                               result_record_id, reply_text, plan_count, error_message, error_code,
                               generation_policy_version, delivery_json,
                               created_time, started_time, finished_time, updated_time
                        FROM travel_trip_job
                        WHERE job_id = :job_id
                    """),
                    {"job_id": job_id},
                )
                row = result.one_or_none()
                if row is None:
                    return None
                return _row_to_record(row)
        except Exception as exc:
            last_error = exc
            if attempt >= READ_RETRIES:
                break
            logger.warning(
                "retry get_trip_job_by_id job_id=%s attempt=%s error=%s",
                job_id,
                attempt,
                exc,
            )
            await asyncio.sleep(0.2 * attempt)
    assert last_error is not None
    raise last_error


async def get_trip_job_by_request_id(request_id: str) -> TripJobRecord | None:
    factory = get_session_factory()
    async with factory() as session:
        result = await session.execute(
            text("""
                SELECT job_id, request_id, source, conversation_id,
                       user_display_name, user_query,
                       status, current_stage, result_type, trip_request_json,
                       result_record_id, reply_text, plan_count, error_message, error_code,
                       generation_policy_version, delivery_json,
                       created_time, started_time, finished_time, updated_time
                FROM travel_trip_job
                WHERE request_id = :request_id
            """),
            {"request_id": request_id},
        )
        row = result.one_or_none()
        if row is None:
            return None
        return _row_to_record(row)


async def get_trip_result_delivery_metadata(
    result_record_id: int | None,
    *,
    job_id: str | None = None,
) -> dict[str, str] | None:
    """Read authoritative delivery fields from the persisted result metrics."""
    if result_record_id is None:
        return None
    factory = get_session_factory()
    async with factory() as session:
        result = await session.execute(
            text("""
                SELECT plan.quality_metrics,
                       job.generation_policy_version,
                       job.delivery_json,
                       job.status,
                       job.result_type,
                       job.result_record_id
                FROM travel_plan_record AS plan
                LEFT JOIN LATERAL (
                    SELECT generation_policy_version, delivery_json, status,
                           result_type, result_record_id
                    FROM travel_trip_job
                    WHERE result_record_id = plan.id
                      AND (
                          CAST(:job_id AS VARCHAR) IS NULL
                          OR job_id = CAST(:job_id AS VARCHAR)
                      )
                    ORDER BY id ASC
                    LIMIT 1
                ) AS job ON TRUE
                WHERE plan.id = :result_record_id
            """),
            {"result_record_id": result_record_id, "job_id": job_id},
        )
        row = result.one_or_none()
    if row is None:
        return None
    metrics = row.quality_metrics if isinstance(row.quality_metrics, dict) else {}
    policy = getattr(row, "generation_policy_version", None) or "legacy"
    if policy == "v0.12":
        if metrics.get("generation_policy_version") != "v0.12":
            return None
        if not {"published_variant", "delivery_status", "delivery"}.issubset(metrics):
            return None
        try:
            job_delivery = GenerationDelivery.model_validate(row.delivery_json)
            result_delivery = GenerationDelivery.model_validate(metrics["delivery"])
            if job_delivery != result_delivery:
                return None
            validate_delivery_for_job(
                job_delivery,
                status=str(row.status),
                result_type=row.result_type,
                result_record_id=row.result_record_id,
                published_variant=metrics["published_variant"],
                delivery_status=metrics["delivery_status"],
            )
        except (ValueError, TypeError):
            return None
    variant = metrics.get("published_variant", "normal")
    status = metrics.get("delivery_status", "NORMAL")
    if variant not in {"normal", "safe"} or status not in {"NORMAL", "DEGRADED"}:
        return None
    if variant == "safe" and status != "DEGRADED":
        return None
    return {
        "published_variant": variant,
        "delivery_status": status,
    }


async def get_recent_successful_plan_place_ids(
    *,
    source: str,
    conversation_id: str,
    limit: int = 3,
) -> list[set[int]]:
    """Load recent reviewed PLAN_READY results for one async conversation."""
    factory = get_session_factory()
    async with factory() as session:
        rows = (await session.execute(
            text("""
                SELECT plan.used_place_ids
                FROM travel_trip_job AS job
                JOIN travel_plan_record AS plan
                  ON plan.id = job.result_record_id
                WHERE job.source = :source
                  AND job.conversation_id = :conversation_id
                  AND job.status = 'SUCCESS'
                  AND job.result_type = 'PLAN_READY'
                ORDER BY job.finished_time DESC NULLS LAST, job.created_time DESC
                LIMIT :limit
            """),
            {
                "source": source,
                "conversation_id": conversation_id,
                "limit": limit,
            },
        )).all()
    return [_coerce_place_ids(row.used_place_ids) for row in rows]


async def count_pending_jobs_before(job_id: str) -> int:
    factory = get_session_factory()
    async with factory() as session:
        result = await session.execute(
            text("""
                SELECT COUNT(*)::int
                FROM travel_trip_job AS ahead
                JOIN travel_trip_job AS current
                  ON current.job_id = :job_id
                WHERE ahead.status = 'PENDING'
                  AND ahead.created_time < current.created_time
            """),
            {"job_id": job_id},
        )
        return int(result.scalar_one())


async def queue_position_for_job(job: TripJobRecord) -> int:
    if job.status != "PENDING":
        return 0
    return await count_pending_jobs_before(job.job_id)


async def create_async_trip_job(
    *,
    message: str | None,
    trip_request_json: dict | None = None,
    request_field_provenance: dict[str, str] | None = None,
    request_user_supplied_json: dict | None = None,
    request_id: str,
    source: str,
    conversation_id: str,
    user_display_name: str | None = None,
    generation_policy_version: GenerationPolicyVersion = "legacy",
    allow_new_v012: bool = False,
) -> AsyncTripJobCreateResult:
    user_query = _effective_user_query(
        message=message,
        trip_request_json=trip_request_json,
    )
    if not user_query:
        raise ValueError("message or trip_request is required")
    if not request_id.strip():
        raise ValueError("request_id is required")
    if not source.strip():
        raise ValueError("source is required")
    if not conversation_id.strip():
        raise ValueError("conversation_id is required")
    if generation_policy_version not in {"legacy", "v0.12"}:
        raise ValueError("unsupported generation_policy_version")
    display_name = (user_display_name or "").strip() or None
    if display_name is not None:
        display_name = display_name[:100]

    existing = await get_trip_job_by_request_id(request_id.strip())
    if existing is not None:
        if (
            existing.user_query == user_query
            and existing.generation_policy_version == generation_policy_version
        ):
            queue_position = await queue_position_for_job(existing)
            return AsyncTripJobCreateResult(
                job_id=existing.job_id,
                status=existing.status,
                current_stage=existing.current_stage,
                queue_position=queue_position,
                message=user_message_for_job(existing),
                cached=True,
                generation_policy_version=existing.generation_policy_version,
                delivery=existing.delivery,
            )
        raise RequestIdConflictError(
            "request_id 已存在，但请求内容不一致",
        )

    if generation_policy_version == "v0.12" and not allow_new_v012:
        raise GenerationPolicyNotEnabledError(
            "generation_policy_version v0.12 is not enabled"
        )

    job_id = uuid.uuid4().hex
    query_hash = _normalized_query_hash(user_query)
    factory = get_session_factory()

    try:
        async with factory() as session:
            await session.execute(
                text("""
                    INSERT INTO travel_trip_job (
                        job_id, request_id, source, conversation_id,
                        user_display_name, user_query,
                        normalized_query, normalized_query_hash,
                        trip_request_json, status, current_stage,
                        projection_version, trace_completeness,
                        guide_result_state, request_field_provenance,
                        request_user_supplied_json, generation_policy_version,
                        delivery_json
                    ) VALUES (
                        :job_id, :request_id, :source, :conversation_id,
                        :user_display_name, :user_query,
                        :normalized_query, :normalized_query_hash,
                        CAST(:trip_request_json AS jsonb), 'PENDING', 'PENDING',
                        1, 'COMPLETE', 'NOT_APPLICABLE',
                        CAST(:request_field_provenance AS jsonb),
                        CAST(:request_user_supplied_json AS jsonb),
                        :generation_policy_version, CAST(:delivery_json AS jsonb)
                    )
                """),
                {
                    "job_id": job_id,
                    "request_id": request_id.strip(),
                    "source": source.strip(),
                    "conversation_id": conversation_id.strip(),
                    "user_display_name": display_name,
                    "user_query": user_query,
                    "normalized_query": user_query,
                    "normalized_query_hash": query_hash,
                    "trip_request_json": (
                        json.dumps(trip_request_json, ensure_ascii=False)
                        if trip_request_json is not None
                        else None
                    ),
                    "request_field_provenance": json.dumps(
                        request_field_provenance or {},
                        ensure_ascii=False,
                    ),
                    "request_user_supplied_json": json.dumps(
                        request_user_supplied_json or {},
                        ensure_ascii=False,
                    ),
                    "generation_policy_version": generation_policy_version,
                    "delivery_json": (
                        pending_delivery().model_dump_json()
                        if generation_policy_version == "v0.12"
                        else None
                    ),
                },
            )
            await emit_trip_projection_commit(
                session,
                job_id=job_id,
                advance_job_version=False,
            )
            await session.commit()
    except IntegrityError:
        logger.info("request_id race on create, re-reading request_id=%s", request_id)
        raced = await get_trip_job_by_request_id(request_id.strip())
        if raced is None:
            raise
        if (
            raced.user_query == user_query
            and raced.generation_policy_version == generation_policy_version
        ):
            queue_position = await queue_position_for_job(raced)
            return AsyncTripJobCreateResult(
                job_id=raced.job_id,
                status=raced.status,
                current_stage=raced.current_stage,
                queue_position=queue_position,
                message=user_message_for_job(raced),
                cached=True,
                generation_policy_version=raced.generation_policy_version,
                delivery=raced.delivery,
            )
        raise RequestIdConflictError(
            "request_id 已存在，但请求内容不一致",
        ) from None

    queue_position = await count_pending_jobs_before(job_id)
    return AsyncTripJobCreateResult(
        job_id=job_id,
        status="PENDING",
        current_stage="PENDING",
        queue_position=queue_position,
        message=STAGE_USER_MESSAGES["PENDING"],
        cached=False,
        generation_policy_version=generation_policy_version,
        delivery=(pending_delivery() if generation_policy_version == "v0.12" else None),
    )


async def count_running_trip_jobs() -> int:
    factory = get_session_factory()
    async with factory() as session:
        result = await session.execute(
            text("SELECT COUNT(*)::int FROM travel_trip_job WHERE status = 'RUNNING'"),
        )
        return int(result.scalar_one())


async def expire_stale_trip_jobs(*, timeout_seconds: int) -> int:
    """Mark worker jobs that exceeded the configured SLA as timed out."""
    factory = get_session_factory()
    async with factory() as session:
        stale_rows = await session.execute(
            text("""
                SELECT job_id, generation_policy_version, delivery_json
                FROM travel_trip_job
                WHERE status IN ('PENDING', 'RUNNING')
                  AND COALESCE(started_time, created_time)
                      < NOW() - make_interval(secs => :timeout_seconds)
                ORDER BY id ASC
                FOR UPDATE
            """),
            {"timeout_seconds": timeout_seconds},
        )
        stale_jobs = list(stale_rows)
        job_ids = [str(row.job_id) for row in stale_jobs]
        for stale_job in stale_jobs:
            job_id = str(stale_job.job_id)
            delivery_json = None
            if str(stale_job.generation_policy_version or "legacy") == "v0.12":
                current_delivery = GenerationDelivery.model_validate(
                    stale_job.delivery_json
                )
                delivery_json = GenerationDelivery(
                    outcome="FAILED",
                    review_state=current_delivery.review_state,
                    reason_code="TIMEOUT",
                    selector_source=current_delivery.selector_source,
                ).model_dump_json()
            await session.execute(
                text("""
                    UPDATE travel_trip_job
                    SET status = 'TIMEOUT',
                        current_stage = 'TIMEOUT',
                        error_code = 'TIMEOUT',
                        error_message = '规划超时，请稍后重试',
                        finished_time = NOW(),
                        delivery_json = COALESCE(
                            CAST(:delivery_json AS jsonb), delivery_json
                        )
                    WHERE job_id = :job_id
                """),
                {"job_id": job_id, "delivery_json": delivery_json},
            )
            step_ids = await close_all_running_steps(
                session,
                job_id=job_id,
                status="TIMEOUT",
                metadata={"error": "trip job exceeded configured timeout"},
            )
            await emit_trip_projection_commit(
                session,
                job_id=job_id,
                changed_step_ids=step_ids,
            )
        await session.commit()
    return len(job_ids)


async def purge_expired_route_replays() -> int:
    """Clear only expired sensitive replay bodies; durable summaries remain."""
    factory = get_session_factory()
    async with factory() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT job_id
                    FROM travel_trip_job
                    WHERE route_replay_data IS NOT NULL
                      AND route_replay_expires_at <= NOW()
                    ORDER BY id
                    FOR UPDATE
                    """
                )
            )
        ).all()
        for row in rows:
            job_id = str(row.job_id)
            await session.execute(
                text(
                    """
                    UPDATE travel_trip_job
                    SET route_replay_data = NULL,
                        route_replay_expires_at = NULL,
                        route_diagnostics = CASE
                            WHEN route_diagnostics IS NULL THEN NULL
                            ELSE jsonb_set(
                                route_diagnostics,
                                '{replay_available}',
                                'false'::jsonb,
                                true
                            )
                        END
                    WHERE job_id = :job_id
                    """
                ),
                {"job_id": job_id},
            )
            await emit_trip_projection_commit(session, job_id=job_id)
        await session.commit()
        return len(rows)


async def claim_next_pending_trip_job(
    *,
    max_concurrency: int,
    allow_v012: bool = False,
) -> TripJobRecord | None:
    """Atomically claim one PENDING job if concurrency allows."""
    factory = get_session_factory()
    async with factory() as session:
        running = await session.execute(
            text("SELECT COUNT(*)::int FROM travel_trip_job WHERE status = 'RUNNING'"),
        )
        if int(running.scalar_one()) >= max_concurrency:
            return None

        pick = await session.execute(
            text("""
                SELECT j.job_id
                FROM travel_trip_job j
                WHERE j.status = 'PENDING'
                  AND (
                    j.generation_policy_version = 'legacy'
                    OR (
                      :allow_v012
                      AND j.generation_policy_version = 'v0.12'
                    )
                  )
                  AND NOT EXISTS (
                    SELECT 1
                    FROM travel_trip_job r
                    WHERE r.status = 'RUNNING'
                      AND r.source = j.source
                      AND r.conversation_id = j.conversation_id
                  )
                ORDER BY j.created_time ASC
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            """),
            {"allow_v012": bool(allow_v012)},
        )
        row = pick.first()
        if row is None:
            return None

        job_id = row.job_id
        update = await session.execute(
            text("""
                UPDATE travel_trip_job
                SET status = 'RUNNING',
                    current_stage = 'PENDING',
                    started_time = COALESCE(started_time, NOW())
                WHERE job_id = :job_id
            """),
            {"job_id": job_id},
        )
        if update.rowcount != 1:
            return None
        await emit_trip_projection_commit(session, job_id=job_id)
        await session.commit()

    return await get_trip_job_by_id(job_id)


async def update_trip_job_stage(job_id: str, stage: str) -> None:
    factory = get_session_factory()
    async with factory() as session:
        result = await session.execute(
            text("""
                UPDATE travel_trip_job
                SET current_stage = :stage
                WHERE job_id = :job_id
                  AND status = 'RUNNING'
            """),
            {"job_id": job_id, "stage": stage},
        )
        if result.rowcount:
            await emit_trip_projection_commit(session, job_id=job_id)
        await session.commit()


async def record_trip_job_stage_started(
    job_id: str,
    *,
    stage: str,
    attempt: int = 1,
    publish_retry_round: int = 0,
    metadata: dict | None = None,
    previous_success_event: dict | None = None,
) -> None:
    """Persist a stage boundary and one indivisible projection commit."""
    factory = get_session_factory()
    async with factory() as session:
        changed_step_ids: list[int] = []
        if previous_success_event:
            previous_id = await finish_latest_running_step(
                session,
                job_id=job_id,
                stage=str(previous_success_event.get("stage") or ""),
                status="SUCCESS",
                attempt=int(previous_success_event.get("attempt") or 1),
                publish_retry_round=int(
                    previous_success_event.get("publish_retry_round") or 0
                ),
                latency_ms=previous_success_event.get("latency_ms"),
                metadata=previous_success_event.get("metadata") or {},
            )
            if previous_id is None:
                previous_id = await insert_trip_step(
                    session,
                    job_id=job_id,
                    stage=str(previous_success_event.get("stage") or ""),
                    status="SUCCESS",
                    attempt=int(previous_success_event.get("attempt") or 1),
                    publish_retry_round=int(
                        previous_success_event.get("publish_retry_round") or 0
                    ),
                    latency_ms=previous_success_event.get("latency_ms"),
                    metadata=previous_success_event.get("metadata") or {},
                    finished=True,
                )
            changed_step_ids.append(previous_id)
        job_update = await session.execute(
            text("""
                UPDATE travel_trip_job
                SET current_stage = :stage
                WHERE job_id = :job_id
                  AND status = 'RUNNING'
            """),
            {"job_id": job_id, "stage": stage},
        )
        if not job_update.rowcount:
            raise RuntimeError("trip job is not RUNNING during stage transition")
        changed_step_ids.append(
            await insert_trip_step(
                session,
                job_id=job_id,
                stage=stage,
                status="RUNNING",
                attempt=attempt,
                publish_retry_round=publish_retry_round,
                metadata=metadata,
            )
        )
        await emit_trip_projection_commit(
            session,
            job_id=job_id,
            changed_step_ids=changed_step_ids,
        )
        await session.commit()


async def record_trip_job_step_event(
    job_id: str,
    *,
    stage: str,
    status: str,
    attempt: int = 1,
    publish_retry_round: int = 0,
    latency_ms: int | None = None,
    metadata: dict | None = None,
) -> None:
    """Persist one workflow step transition and its composite event."""
    factory = get_session_factory()
    async with factory() as session:
        step_id: int | None = None
        if status == "RUNNING":
            step_id = await insert_trip_step(
                session,
                job_id=job_id,
                stage=stage,
                status=status,
                attempt=attempt,
                publish_retry_round=publish_retry_round,
                latency_ms=latency_ms,
                metadata=metadata,
            )
        else:
            step_id = await finish_latest_running_step(
                session,
                job_id=job_id,
                stage=stage,
                status=status,
                attempt=attempt,
                publish_retry_round=publish_retry_round,
                latency_ms=latency_ms,
                metadata=metadata,
            )
            if step_id is None:
                step_id = await insert_trip_step(
                    session,
                    job_id=job_id,
                    stage=stage,
                    status=status,
                    attempt=attempt,
                    publish_retry_round=publish_retry_round,
                    latency_ms=latency_ms,
                    metadata=metadata,
                    finished=True,
                )
        await emit_trip_projection_commit(
            session,
            job_id=job_id,
            changed_step_ids=[step_id],
        )
        await session.commit()


async def enrich_latest_trip_job_step_observation(
    job_id: str,
    *,
    stage: str,
    attempt: int,
    publish_retry_round: int,
    metadata: dict,
) -> bool:
    """Append observation-only metadata to the owning terminal step."""
    factory = get_session_factory()
    async with factory() as session:
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
                        ORDER BY started_time DESC, id DESC
                        LIMIT 1
                        FOR UPDATE
                    )
                    UPDATE travel_trip_job_step step
                    SET metadata = step.metadata || CAST(:metadata AS jsonb),
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
                    "metadata": json.dumps(metadata, ensure_ascii=False),
                },
            )
        ).one_or_none()
        if row is None:
            await session.rollback()
            return False
        await emit_trip_projection_commit(
            session,
            job_id=job_id,
            changed_step_ids=[int(row.id)],
        )
        await session.commit()
        return True


async def close_running_trip_job_steps(
    job_id: str,
    *,
    status: str,
    metadata: dict | None = None,
) -> int:
    """Close RUNNING steps as one projection-relevant source transaction."""
    factory = get_session_factory()
    async with factory() as session:
        step_ids = await close_all_running_steps(
            session,
            job_id=job_id,
            status=status,
            metadata=metadata,
        )
        if step_ids:
            await emit_trip_projection_commit(
                session,
                job_id=job_id,
                changed_step_ids=step_ids,
            )
        await session.commit()
        return len(step_ids)


async def ensure_terminal_observation_step(
    job_id: str,
    *,
    status: str,
    metadata: dict | None = None,
) -> None:
    """Ensure a terminal observation step exists for timeout/cancel paths.

    If no RUNNING step was available to close, insert a finished PERSISTING step
    with event_type=OBSERVATION_FLUSH so full sanitized llm_observation is always
    durable. Stage value stays within the existing DB CHECK allowlist (no schema
    expansion in O4).
    """
    if status not in {"FAILED", "TIMEOUT"}:
        raise ValueError("terminal observation step status must be FAILED or TIMEOUT")
    factory = get_session_factory()
    meta = dict(metadata or {})
    meta.setdefault("event_type", "OBSERVATION_FLUSH")
    async with factory() as session:
        step_id = await insert_trip_step(
            session,
            job_id=job_id,
            stage="PERSISTING",
            status=status,
            latency_ms=0,
            metadata=meta,
            finished=True,
        )
        await emit_trip_projection_commit(
            session,
            job_id=job_id,
            changed_step_ids=[step_id],
        )
        await session.commit()


async def update_trip_job_trip_request(job_id: str, trip_request: dict) -> None:
    factory = get_session_factory()
    async with factory() as session:
        result = await session.execute(
            text("""
                UPDATE travel_trip_job
                SET trip_request_json = CAST(:trip_request_json AS jsonb)
                WHERE job_id = :job_id
                  AND status = 'RUNNING'
            """),
            {
                "job_id": job_id,
                "trip_request_json": json.dumps(trip_request, ensure_ascii=False),
            },
        )
        if result.rowcount:
            await emit_trip_projection_commit(session, job_id=job_id)
        await session.commit()


async def _prepare_terminal_delivery(
    session,
    *,
    job_id: str,
    delivery: GenerationDelivery | dict | None,
    status: str,
    result_type: str | None,
    result_record_id: int | None,
) -> tuple[bool, str | None]:
    """Lock the job and validate/fix v0.12 result metadata in this transaction."""
    job_row = (await session.execute(
        text("""
            SELECT status, generation_policy_version
            FROM travel_trip_job
            WHERE job_id = :job_id
            FOR UPDATE
        """),
        {"job_id": job_id},
    )).mappings().one_or_none()
    if job_row is None:
        return False, None
    row_status = (
        job_row.status if hasattr(job_row, "status") else job_row["status"]
    )
    if str(row_status) != "RUNNING":
        return False, None
    row_policy = (
        job_row.generation_policy_version
        if hasattr(job_row, "generation_policy_version")
        else job_row["generation_policy_version"]
    )
    policy = str(row_policy or "legacy")
    if policy == "legacy":
        if delivery is not None:
            raise ValueError("legacy job cannot persist v0.12 delivery")
        return True, None
    if policy != "v0.12":
        raise ValueError("unsupported persisted generation policy")
    if delivery is None:
        raise ValueError("v0.12 terminal transition requires delivery")
    parsed = (
        delivery
        if isinstance(delivery, GenerationDelivery)
        else GenerationDelivery.model_validate(delivery)
    )
    variant: str | None = None
    delivery_status: str | None = None
    if result_record_id is not None:
        plan_row = (await session.execute(
            text("""
                SELECT quality_metrics
                FROM travel_plan_record
                WHERE id = :result_record_id
                FOR UPDATE
            """),
            {"result_record_id": result_record_id},
        )).mappings().one_or_none()
        if plan_row is None:
            raise ValueError("v0.12 result record does not exist")
        metrics = (
            plan_row["quality_metrics"]
            if isinstance(plan_row["quality_metrics"], dict)
            else {}
        )
        variant = metrics.get("published_variant")
        delivery_status = metrics.get("delivery_status")
    validate_delivery_for_job(
        parsed,
        status=status,
        result_type=result_type,
        result_record_id=result_record_id,
        published_variant=variant,
        delivery_status=delivery_status,
    )
    serialized = parsed.model_dump_json()
    if result_record_id is not None:
        await session.execute(
            text("""
                UPDATE travel_plan_record
                SET quality_metrics = COALESCE(quality_metrics, '{}'::jsonb)
                    || jsonb_build_object(
                        'generation_policy_version', 'v0.12',
                        'delivery', CAST(:delivery_json AS jsonb)
                    )
                WHERE id = :result_record_id
            """),
            {"result_record_id": result_record_id, "delivery_json": serialized},
        )
    return True, serialized


async def mark_trip_job_success(
    job_id: str,
    *,
    reply_text: str,
    plan_count: int,
    result_type: str,
    result_record_id: int | None,
    trip_request_json: dict,
    delivery: GenerationDelivery | dict | None = None,
    route_diagnostics: dict | None = None,
    route_replay_data: dict | None = None,
) -> None:
    factory = get_session_factory()
    async with factory() as session:
        scope = await session.execute(
            text("""
                SELECT source, conversation_id
                FROM travel_trip_job
                WHERE job_id = :job_id
            """),
            {"job_id": job_id},
        )
        row = scope.one_or_none()
        if row is None:
            return
        await _lock_conversation(
            session,
            source=row.source,
            conversation_id=row.conversation_id,
        )
        active, delivery_json = await _prepare_terminal_delivery(
            session,
            job_id=job_id,
            delivery=delivery,
            status="SUCCESS",
            result_type=result_type,
            result_record_id=result_record_id,
        )
        if not active:
            await session.rollback()
            return
        diagnostics_json = None
        replay_json = None
        if route_diagnostics is not None:
            try:
                projected_diagnostics = RouteDiagnostics.model_validate(
                    route_diagnostics
                )
                projected_diagnostics.replay_available = False
                if route_replay_data:
                    candidate_replay = json.dumps(
                        route_replay_data, ensure_ascii=False, sort_keys=True
                    )
                    if len(candidate_replay.encode()) <= 262144:
                        replay_json = candidate_replay
                        projected_diagnostics.replay_available = True
                diagnostics_json = projected_diagnostics.model_dump_json()
                if len(diagnostics_json.encode()) > 16384:
                    diagnostics_json = None
                    replay_json = None
            except Exception as exc:
                logger.warning(
                    "route observation validation failed job_id=%s error_type=%s",
                    job_id, type(exc).__name__,
                )
                diagnostics_json = RouteDiagnostics(
                    evidence_state="UNAVAILABLE",
                    stop_reason="ERROR",
                    candidate_count=None,
                    search=RouteSearchObservation(),
                    truncated=False,
                    replay_available=False,
                ).model_dump_json()
        result = await session.execute(
            text("""
                UPDATE travel_trip_job
                SET status = 'SUCCESS',
                    current_stage = 'SUCCESS',
                    result_type = :result_type,
                    reply_text = :reply_text,
                    plan_count = :plan_count,
                    result_record_id = :result_record_id,
                    trip_request_json = CAST(:trip_request_json AS jsonb),
                    error_code = NULL,
                    error_message = NULL,
                    finished_time = NOW(),
                    delivery_json = COALESCE(CAST(:delivery_json AS jsonb), delivery_json)
                WHERE job_id = :job_id
                  AND status = 'RUNNING'
            """),
            {
                "job_id": job_id,
                "result_type": result_type,
                "reply_text": reply_text,
                "plan_count": plan_count,
                "result_record_id": result_record_id,
                "trip_request_json": json.dumps(trip_request_json, ensure_ascii=False),
                "delivery_json": delivery_json,
            },
        )
        if result.rowcount:
            if diagnostics_json is not None:
                try:
                    async with session.begin_nested():
                        await session.execute(
                            text(
                                """
                                UPDATE travel_trip_job
                                SET route_diagnostics = CAST(:diagnostics AS jsonb),
                                    route_replay_data = CAST(:replay AS jsonb),
                                    route_replay_expires_at = CASE
                                        WHEN :replay IS NOT NULL
                                        THEN created_time + INTERVAL '7 days'
                                        ELSE NULL
                                    END
                                WHERE job_id = :job_id
                                """
                            ),
                            {
                                "job_id": job_id,
                                "diagnostics": diagnostics_json,
                                "replay": replay_json,
                            },
                        )
                except Exception as exc:
                    logger.warning(
                        "route observation persistence failed job_id=%s error_type=%s",
                        job_id, type(exc).__name__,
                    )
                    unavailable_json = RouteDiagnostics(
                        evidence_state="UNAVAILABLE",
                        stop_reason="ERROR",
                        candidate_count=None,
                        search=RouteSearchObservation(),
                        truncated=False,
                        replay_available=False,
                    ).model_dump_json()
                    try:
                        async with session.begin_nested():
                            await session.execute(
                                text(
                                    """
                                    UPDATE travel_trip_job
                                    SET route_diagnostics = CAST(:diagnostics AS jsonb),
                                        route_replay_data = NULL,
                                        route_replay_expires_at = NULL
                                    WHERE job_id = :job_id
                                    """
                                ),
                                {"job_id": job_id, "diagnostics": unavailable_json},
                            )
                    except Exception as marker_exc:
                        logger.warning(
                            "route observation unavailable marker failed job_id=%s error_type=%s",
                            job_id, type(marker_exc).__name__,
                        )
            step_ids = await close_all_running_steps(
                session,
                job_id=job_id,
                status="SUCCESS",
            )
            await emit_trip_projection_commit(
                session,
                job_id=job_id,
                changed_step_ids=step_ids,
            )
        await session.commit()


async def mark_trip_job_failed(
    job_id: str,
    *,
    error_code: str,
    error_message: str,
    error_detail: str,
    step_metadata: dict | None = None,
    ensure_terminal_step: bool = False,
    delivery: GenerationDelivery | dict | None = None,
) -> None:
    factory = get_session_factory()
    async with factory() as session:
        active, delivery_json = await _prepare_terminal_delivery(
            session, job_id=job_id, delivery=delivery, status="FAILED",
            result_type=None, result_record_id=None,
        )
        if not active:
            await session.rollback()
            return
        result = await session.execute(
            text("""
                UPDATE travel_trip_job
                SET status = 'FAILED',
                    current_stage = 'FAILED',
                    error_code = :error_code,
                    error_message = :error_message,
                    error_detail = :error_detail,
                    finished_time = NOW(),
                    delivery_json = COALESCE(CAST(:delivery_json AS jsonb), delivery_json)
                WHERE job_id = :job_id
                  AND status = 'RUNNING'
            """),
            {
                "job_id": job_id,
                "error_code": error_code,
                "error_message": error_message,
                "error_detail": error_detail[:4000],
                "delivery_json": delivery_json,
            },
        )
        if result.rowcount:
            step_ids = await close_all_running_steps(
                session,
                job_id=job_id,
                status="FAILED",
                metadata=step_metadata,
            )
            if ensure_terminal_step and not step_ids:
                terminal_meta = dict(step_metadata or {})
                terminal_meta.setdefault("event_type", "OBSERVATION_FLUSH")
                step_ids.append(
                    await insert_trip_step(
                        session,
                        job_id=job_id,
                        stage="PERSISTING",
                        status="FAILED",
                        latency_ms=0,
                        metadata=terminal_meta,
                        finished=True,
                    )
                )
            await emit_trip_projection_commit(
                session,
                job_id=job_id,
                changed_step_ids=step_ids,
            )
        await session.commit()


async def mark_trip_job_rejected(
    job_id: str,
    *,
    error_code: str,
    delivery: GenerationDelivery | dict | None = None,
) -> None:
    await mark_trip_job_rejected_with_message(
        job_id,
        error_code=error_code,
        error_message=None,
        delivery=delivery,
    )


async def mark_trip_job_rejected_with_message(
    job_id: str,
    *,
    error_code: str,
    error_message: str | None = None,
    step_metadata: dict | None = None,
    ensure_terminal_evidence: bool = False,
    delivery: GenerationDelivery | dict | None = None,
) -> None:
    factory = get_session_factory()
    async with factory() as session:
        active, delivery_json = await _prepare_terminal_delivery(
            session, job_id=job_id, delivery=delivery, status="REJECTED",
            result_type=None, result_record_id=None,
        )
        if not active:
            await session.rollback()
            return
        result = await session.execute(
            text("""
                UPDATE travel_trip_job
                SET status = 'REJECTED',
                    current_stage = 'CITY_GATE_REJECTED',
                    error_code = :error_code,
                    error_message = :error_message,
                    finished_time = NOW(),
                    delivery_json = COALESCE(CAST(:delivery_json AS jsonb), delivery_json)
                WHERE job_id = :job_id
                  AND status = 'RUNNING'
            """),
            {
                "job_id": job_id,
                "error_code": error_code,
                "error_message": error_message,
                "delivery_json": delivery_json,
            },
        )
        if result.rowcount:
            step_ids = await close_all_running_steps(
                session,
                job_id=job_id,
                status="FAILED",
                metadata=step_metadata,
            )
            if ensure_terminal_evidence and not step_ids and step_metadata:
                terminal_meta = dict(step_metadata)
                terminal_meta.setdefault("event_type", "TERMINAL_EVIDENCE")
                step_ids.append(
                    await insert_trip_step(
                        session,
                        job_id=job_id,
                        stage="LLM_OBSERVABILITY",
                        status="FAILED",
                        latency_ms=0,
                        metadata=terminal_meta,
                        finished=True,
                    )
                )
            await emit_trip_projection_commit(
                session,
                job_id=job_id,
                changed_step_ids=step_ids,
            )
        await session.commit()


async def mark_trip_job_timeout(
    job_id: str,
    *,
    error_detail: str | None = None,
    step_metadata: dict | None = None,
    ensure_terminal_step: bool = False,
    delivery: GenerationDelivery | dict | None = None,
) -> None:
    factory = get_session_factory()
    async with factory() as session:
        active, delivery_json = await _prepare_terminal_delivery(
            session, job_id=job_id, delivery=delivery, status="TIMEOUT",
            result_type=None, result_record_id=None,
        )
        if not active:
            await session.rollback()
            return
        result = await session.execute(
            text("""
                UPDATE travel_trip_job
                SET status = 'TIMEOUT',
                    current_stage = 'TIMEOUT',
                    error_code = 'TIMEOUT',
                    error_message = '规划超时，请稍后重试',
                    error_detail = COALESCE(:error_detail, error_detail),
                    finished_time = NOW(),
                    delivery_json = COALESCE(CAST(:delivery_json AS jsonb), delivery_json)
                WHERE job_id = :job_id
                  AND status = 'RUNNING'
            """),
            {
                "job_id": job_id,
                # Keep short non-truncated-JSON summary only. Full observation is
                # stored in step metadata JSONB by the worker.
                "error_detail": (error_detail or "")[:1000] or None,
                "delivery_json": delivery_json,
            },
        )
        if result.rowcount:
            step_ids = await close_all_running_steps(
                session,
                job_id=job_id,
                status="TIMEOUT",
                metadata=step_metadata,
            )
            if ensure_terminal_step and not step_ids:
                terminal_meta = dict(step_metadata or {})
                terminal_meta.setdefault("event_type", "OBSERVATION_FLUSH")
                step_ids.append(
                    await insert_trip_step(
                        session,
                        job_id=job_id,
                        stage="PERSISTING",
                        status="TIMEOUT",
                        latency_ms=0,
                        metadata=terminal_meta,
                        finished=True,
                    )
                )
            await emit_trip_projection_commit(
                session,
                job_id=job_id,
                changed_step_ids=step_ids,
            )
        await session.commit()
