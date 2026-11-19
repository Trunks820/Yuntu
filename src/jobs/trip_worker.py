"""Background worker for async trip planning jobs."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time

from src.agents.llm import (
    bind_observation_sink,
    chat,
    current_llm_observation_summary,
    llm_call_context,
    merge_llm_observation_summaries,
    pop_job_observation_flush,
    project_persisted_llm_usage_for_stage,
    start_llm_observation,
    stop_llm_observation,
)
from src.agents.publish_gate import (
    PUBLISH_FAILURE_FALLBACK_MESSAGE,
    PublishGateError,
    validate_polished_failure_message,
)
from src.agents.schema import TripRequest
from src.agents.workflow import run_trip_workflow
from src.api.formatter import format_plans_markdown
from src.config import get_settings
from src.jobs.city_batch_store import (
    CityBatchActiveError,
    create_city_crawl_batch,
    get_active_city_batch,
)
from src.jobs.trip_store import (
    TripJobRecord,
    claim_next_pending_trip_job,
    close_running_trip_job_steps,
    ensure_terminal_observation_step,
    enrich_latest_trip_job_step_observation,
    expire_stale_trip_jobs,
    purge_expired_route_replays,
    get_recent_successful_plan_place_ids,
    mark_trip_job_failed,
    mark_trip_job_rejected,
    mark_trip_job_rejected_with_message,
    mark_trip_job_success,
    mark_trip_job_timeout,
    record_trip_job_stage_started,
    record_trip_job_step_event,
    update_trip_job_trip_request,
)
from src.jobs.city_gate import (
    CITY_CLARIFICATION_REQUIRED,
    CITY_PREPARING,
    CityGateDecision,
    CityGateRejected,
    NON_DEMAND_SOURCES,
    gate_async_trip_request,
)
from src.jobs.place_demand import MustIncludeResolution, match_and_record_must_include
from src.jobs.trip_failed_draft import (
    persist_failed_draft_if_eligible,
    project_failed_draft_plans,
)
from src.jobs.generation_delivery import GenerationDelivery
from src.agents.safe_plan_renderer import SafeRenderError
from src.agents.writer_relay_router import WriterRelayError
from src.agents.dispatch_resolver import STRUCTURAL_INCOMPLETE_REASONS

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 1.0
SUCCESS_PERSIST_RETRIES = 3
CITY_CLARIFICATION_FALLBACK_MESSAGE = (
    "我还差一个关键信息：你想去哪个城市？"
    "可以直接发“成都3天美食”或“福州3天想去平潭”，"
    "我再继续帮你规划。"
)


def _terminal_delivery_for_failure(
    policy: str,
    reason_code: str,
) -> GenerationDelivery | None:
    if policy != "v0.12":
        return None
    normalized = re.sub(r"[^A-Z0-9]+", "_", reason_code.upper()).strip("_")
    normalized = normalized[:80] or "WORKFLOW_FAILED"
    return GenerationDelivery(
        outcome="FAILED",
        review_state="NOT_RUN",
        reason_code=normalized,
        selector_source="NOT_RUN",
    )


def _terminal_delivery_for_result(
    policy: str,
    result,
) -> GenerationDelivery | None:
    if policy != "v0.12":
        return None
    metrics = result.quality_metrics if isinstance(result.quality_metrics, dict) else {}
    selector_metrics = metrics.get("poi_selection")
    selector_value = (
        selector_metrics.get("selection_source")
        if isinstance(selector_metrics, dict) else None
    )
    selector_source = (
        "LLM" if selector_value == "LLM"
        else "DETERMINISTIC_FALLBACK" if selector_value else "NOT_RUN"
    )
    if result.result_type in {"NO_CANDIDATES", "NO_USABLE_ROUTE"}:
        return GenerationDelivery(
            outcome="NO_ROUTE",
            review_state="NOT_RUN",
            reason_code=result.result_type,
            selector_source=selector_source,
        )
    if metrics.get("published_variant") == "safe":
        return GenerationDelivery(
            outcome="BASIC",
            review_state=metrics.get("v012_review_state", "ISSUES_UNRESOLVED"),
            reason_code=metrics.get(
                "v012_basic_reason", "REVIEW_ISSUES_UNRESOLVED"
            ),
            selector_source=selector_source,
        )
    review_state = metrics.get("v012_review_state")
    from src.agents.review_availability import (
        has_current_unreviewed_receipt, has_current_pruning_receipt,
    )
    if review_state != "MODEL_PASSED" and not (
        review_state == "CODE_ONLY" and (
            has_current_unreviewed_receipt(result.plans, metrics)
            or has_current_pruning_receipt(result.plans, metrics)
        )
    ):
        raise ValueError("v0.12 detailed result lacks an approved review decision")
    return GenerationDelivery(
        outcome="DETAILED",
        review_state=review_state,
        selector_source=selector_source,
    )

_SAFE_FAILURE_REASON_CODES = frozenset({
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
_LOCAL_RECOVERY_FAILURE_REASONS = frozenset({
    "fragment_registry_invalidated",
    "repair_budget_exceeded",
    "required_fact_fragment_repair_failed",
})


def _elapsed_ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)


def _datetime_delta_ms(end, start) -> int | None:
    if end is None or start is None:
        return None
    try:
        return max(0, int((end - start).total_seconds() * 1000))
    except Exception:
        return None


async def _chat_before_deadline(
    *,
    deadline_monotonic: float | None,
    job_id: str | None,
    request_id: str | None,
    call_reason: str,
    **kwargs,
) -> str:
    if deadline_monotonic is None:
        return await chat(**kwargs)
    remaining = deadline_monotonic - time.monotonic()
    if remaining <= 0:
        raise asyncio.TimeoutError("trip hard deadline exhausted before Writer polish")
    with llm_call_context(
        job_id=job_id,
        request_id=request_id,
        call_reason=call_reason,
        workflow_deadline_monotonic=deadline_monotonic,
    ):
        return await asyncio.wait_for(chat(**kwargs), timeout=remaining)


async def _polish_publish_failure_message(
    error: PublishGateError,
    *,
    deadline_monotonic: float | None = None,
    job_id: str | None = None,
    request_id: str | None = None,
) -> str:
    """Polish user-safe publish failure copy; never expose or repair bad plans."""
    categories = "、".join(error.result.failure_reasons) or "publish_quality_failed"
    system = """你是 YunTu Travel 的失败提示文案助手。
一次旅行攻略生成已经失败，原因是最终文本不适合直接发给用户。请生成一句自然、简短、礼貌的中文说明。

要求：
- 只说明这次结果不够稳，建议用户换个说法再试。
- 不要提内部系统、审核、Publish Gate、Structural Gate、route lock、内部校验。
- 不要编排行程，不要推荐地点，不要修复攻略。
- 输出 JSON：{"message": "..."}"""
    try:
        raw = await _chat_before_deadline(
            deadline_monotonic=deadline_monotonic,
            job_id=job_id,
            request_id=request_id,
            call_reason="publish_failure_polish",
            system=system,
            user=f"failure_category: {categories}",
            role="writer",
            temperature=0.3,
            json_mode=True,
        )
        data = json.loads(raw)
        message = validate_polished_failure_message(
            str(data.get("message", "")).strip()
        )
    except Exception:
        logger.exception("publish failure polish failed")
        return PUBLISH_FAILURE_FALLBACK_MESSAGE
    return message or PUBLISH_FAILURE_FALLBACK_MESSAGE


async def _observe_terminal_polish(
    action,
    *,
    prior_summary: dict,
    stage: str,
    attempt: int,
    publish_retry_round: int,
    job_id: str,
    request_id: str | None,
    fallback_result: str,
) -> tuple[str, dict, dict, bool]:
    """Observe a post-workflow polish call without changing its result policy."""
    token, _ = start_llm_observation()
    cancelled = False
    try:
        with llm_call_context(
            job_id=job_id,
            request_id=request_id,
            stage=stage,
            attempt=attempt,
            publish_retry_round=publish_retry_round,
            owning_stage=stage,
            owning_attempt=attempt,
            owning_publish_retry_round=publish_retry_round,
        ):
            try:
                result = await action()
            except asyncio.CancelledError:
                # Terminal persistence is authoritative. A best-effort copy
                # polish must not turn FAILED/REJECTED into a missing outcome.
                logger.warning(
                    "terminal polish cancelled; using fallback job_id=%s stage=%s",
                    job_id,
                    stage,
                )
                result = fallback_result
                cancelled = True
        polish_summary = current_llm_observation_summary()
    finally:
        stop_llm_observation(token)
    combined = merge_llm_observation_summaries(prior_summary, polish_summary)
    usage = project_persisted_llm_usage_for_stage(
        combined,
        stage=stage,
        attempt=attempt,
        publish_retry_round=publish_retry_round,
    )
    return result, combined, usage, cancelled


async def _enrich_terminal_polish_observation(
    job_id: str,
    *,
    stage: str,
    attempt: int,
    publish_retry_round: int,
    summary: dict,
    usage: dict,
) -> bool:
    """Persist auxiliary polish evidence without changing terminal handling."""
    try:
        return await enrich_latest_trip_job_step_observation(
            job_id,
            stage=stage,
            attempt=attempt,
            publish_retry_round=publish_retry_round,
            metadata={"llm_observation": summary, "llm_usage": usage},
        )
    except Exception:
        logger.warning(
            "terminal polish observation persistence failed job_id=%s stage=%s",
            job_id,
            stage,
            exc_info=True,
        )
        return False


def _city_gate_rejection_metadata(decision: CityGateDecision) -> dict[str, str]:
    metadata: dict[str, str] = {
        "city_gate_status": decision.status,
    }
    if decision.city_status:
        metadata["city_status"] = decision.city_status
    if decision.city_batch_status:
        metadata["city_batch_status"] = decision.city_batch_status
    if decision.city_batch_error_code:
        metadata["city_batch_error_code"] = decision.city_batch_error_code
    return metadata


async def _polish_city_clarification_message(
    user_query: str,
    *,
    deadline_monotonic: float | None = None,
    job_id: str | None = None,
    request_id: str | None = None,
) -> str:
    """Ask the Writer model for clarification copy; fall back deterministically."""
    system = """你是 YunTu Travel 的澄清话术助手。
用户的旅行需求缺少明确目的地城市。请生成一句自然、简短、礼貌的中文澄清文案。

要求：
- 只询问用户想去哪个城市。
- 可以给 1-2 个很短示例，例如“成都3天美食”。
- 不要编排行程，不要推荐地点，不要提系统错误。
- 输出 JSON：{"message": "..."}"""
    try:
        raw = await _chat_before_deadline(
            deadline_monotonic=deadline_monotonic,
            job_id=job_id,
            request_id=request_id,
            call_reason="city_clarification_polish",
            system=system,
            user=f"用户原文：{user_query}",
            role="writer",
            temperature=0.3,
            json_mode=True,
        )
        data = json.loads(raw)
        message = str(data.get("message", "")).strip()
    except Exception:
        logger.exception("city clarification polish failed")
        return CITY_CLARIFICATION_FALLBACK_MESSAGE

    if not message or "城市" not in message:
        return CITY_CLARIFICATION_FALLBACK_MESSAGE
    return message[:160]


def _classify_error(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, WriterRelayError) and exc.code in {
        "WRITER_CAPACITY_BUSY",
        "WRITER_ENDPOINTS_UNAVAILABLE",
    }:
        return exc.code, "服务暂时不可用，请稍后再试"
    if isinstance(exc, PublishGateError):
        return "PUBLISH_GATE_FAILED", PUBLISH_FAILURE_FALLBACK_MESSAGE
    if isinstance(exc, SafeRenderError):
        return "SAFE_RENDER_FAILED", "暂时无法生成可用行程，请稍后再试"
    message = str(exc) or exc.__class__.__name__
    lowered = message.lower()
    if "llm" in lowered or "gemini" in lowered or "openai" in lowered:
        return "LLM_ERROR", "规划失败，请稍后重试"
    if "database" in lowered or "sql" in lowered or "asyncpg" in lowered:
        return "DB_ERROR", "规划失败，请稍后重试"
    return "WORKFLOW_ERROR", "规划失败，请稍后重试"


def _build_failure_observation(
    exc: Exception,
    *,
    stage: str | None,
    error_code: str,
    completed_rewrite_rounds: int = 0,
    failed_draft_available: bool = False,
) -> dict[str, object]:
    resolved_stage = stage or "WORKFLOW"
    category = error_code if error_code in _FAILURE_CATEGORIES else "WORKFLOW_ERROR"
    raw_findings = []
    if isinstance(exc, PublishGateError):
        raw_findings = list(exc.result.blocking_findings)
    reason_facts: dict[str, dict[str, object]] = {}
    for finding in raw_findings:
        internal = str(getattr(finding, "reason", "") or "").strip()
        code = internal if internal in _SAFE_FAILURE_REASON_CODES else "unclassified_failure"
        fact = reason_facts.setdefault(code, {
            "code": code,
            "count": 0,
            "plan_indexes": [],
        })
        fact["count"] = int(fact["count"]) + 1
        plan_index = getattr(finding, "plan_index", None)
        if (
            isinstance(plan_index, int)
            and not isinstance(plan_index, bool)
            and plan_index > 0
            and plan_index not in fact["plan_indexes"]
            and len(fact["plan_indexes"]) < 20
        ):
            fact["plan_indexes"].append(plan_index)

    explicit_indexes = getattr(exc, "trace_truth_reason_plan_indexes", None)
    if isinstance(explicit_indexes, dict):
        for internal, indexes in explicit_indexes.items():
            if not isinstance(indexes, list):
                continue
            internal_code = str(internal or "").strip()
            code = (
                internal_code
                if internal_code in _SAFE_FAILURE_REASON_CODES
                else "unclassified_failure"
            )
            fact = reason_facts.get(code)
            if fact is None:
                continue
            for plan_index in indexes:
                if (
                    isinstance(plan_index, int)
                    and not isinstance(plan_index, bool)
                    and plan_index > 0
                    and plan_index not in fact["plan_indexes"]
                    and len(fact["plan_indexes"]) < 20
                ):
                    fact["plan_indexes"].append(plan_index)

    completed_rounds = 1 if int(completed_rewrite_rounds or 0) > 0 else 0
    attempted = completed_rounds == 1
    raw_codes = {
        str(getattr(finding, "reason", "") or "").strip()
        for finding in raw_findings
        if str(getattr(finding, "reason", "") or "").strip()
    }
    eligible = attempted or (
        isinstance(exc, PublishGateError)
        and bool(raw_codes)
        and raw_codes.issubset(STRUCTURAL_INCOMPLETE_REASONS)
    )
    skip_reason = getattr(exc, "trace_truth_retry_not_attempted_reason", None)
    if skip_reason not in _REWRITE_SKIP_REASONS:
        skip_reason = None
    explicit_eligible = getattr(exc, "trace_truth_retry_eligible", None)
    if isinstance(explicit_eligible, bool) and not attempted:
        eligible = explicit_eligible
    if attempted:
        skip_reason = None
    elif skip_reason is None:
        if getattr(exc, "trace_truth_local_recovery_attempted", None) is True:
            skip_reason = "SAFE_OR_LOCAL_RECOVERY_SELECTED"
        elif eligible:
            skip_reason = "INSUFFICIENT_TIME_BUDGET"
        elif resolved_stage == "FINAL_WRITER" and not failed_draft_available:
            skip_reason = "WRITER_FAILED_BEFORE_DRAFT"
        elif raw_codes.intersection(_LOCAL_RECOVERY_FAILURE_REASONS):
            skip_reason = "SAFE_OR_LOCAL_RECOVERY_SELECTED"
        elif isinstance(exc, PublishGateError):
            skip_reason = "REASON_NOT_ELIGIBLE"
        else:
            skip_reason = "NOT_APPLICABLE"
    return {
        "schema_version": 1,
        "evidence_state": "AVAILABLE",
        "observation_revision": 1,
        "failure_stage": resolved_stage[:120],
        "category": category,
        "reasons": list(reason_facts.values())[:50],
        "automatic_rewrite": {
            "eligible": bool(eligible),
            "attempted": attempted,
            "completed_rounds": completed_rounds,
            "not_attempted_reason": skip_reason,
        },
    }


def _structured_trip_request_from_job(job: TripJobRecord) -> TripRequest | None:
    if job.trip_request_json is None:
        return None
    try:
        return TripRequest(**job.trip_request_json)
    except Exception as exc:
        raise ValueError("invalid trip_request_json on async trip job") from exc


class TripWorker:
    def __init__(self) -> None:
        self._stop = asyncio.Event()
        self._tasks: set[asyncio.Task[None]] = set()

    async def run(self) -> None:
        settings = get_settings()
        logger.info(
            "trip worker started concurrency=%s timeout=%ss",
            settings.trip_worker_concurrency,
            settings.trip_job_timeout_seconds,
        )
        next_stale_sweep = 0.0
        try:
            while not self._stop.is_set():
                try:
                    self._tasks = {task for task in self._tasks if not task.done()}
                    now = time.monotonic()
                    if now >= next_stale_sweep:
                        expired = await expire_stale_trip_jobs(
                            timeout_seconds=settings.trip_job_timeout_seconds,
                        )
                        if expired:
                            logger.warning("trip worker expired stale jobs count=%s", expired)
                        purged_replays = await purge_expired_route_replays()
                        if purged_replays:
                            logger.info(
                                "trip worker purged route replays count=%s",
                                purged_replays,
                            )
                        next_stale_sweep = (
                            now + settings.trip_stale_sweep_interval_seconds
                        )

                    if len(self._tasks) >= settings.trip_worker_concurrency:
                        await asyncio.sleep(POLL_INTERVAL_SECONDS)
                        continue

                    job = await claim_next_pending_trip_job(
                        max_concurrency=settings.trip_worker_concurrency,
                        allow_v012=bool(getattr(
                            settings, "v012_trip_worker_enabled", False
                        )),
                    )
                    if job is None:
                        await asyncio.sleep(POLL_INTERVAL_SECONDS)
                        continue

                    task = asyncio.create_task(self._execute_job(job))
                    self._tasks.add(task)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("trip worker loop failed; retrying")
                    await asyncio.sleep(POLL_INTERVAL_SECONDS)
        finally:
            if self._tasks:
                await asyncio.gather(*self._tasks, return_exceptions=True)
            logger.info("trip worker stopped")

    async def stop(self) -> None:
        self._stop.set()

    async def _execute_job(self, job: TripJobRecord) -> None:
        settings = get_settings()
        job_id = job.job_id
        generation_policy_version = getattr(
            job, "generation_policy_version", "legacy"
        )
        logger.info("trip worker executing job_id=%s", job_id)
        execute_t0 = time.monotonic()
        workflow_deadline_monotonic = (
            execute_t0 + settings.trip_job_timeout_seconds
        )
        runtime_metrics: dict[str, int] = {}
        queue_wait_ms = _datetime_delta_ms(job.started_time, job.created_time)
        if queue_wait_ms is not None:
            runtime_metrics["queue_wait_ms"] = queue_wait_ms
        city_notice_code: str | None = None
        pending_current_stage: str | None = None
        latest_workflow_stage: str | None = None
        latest_workflow_attempt = 1
        latest_workflow_publish_retry_round = 0
        completed_automatic_rewrite_rounds = 0
        pending_success_event: dict | None = None
        structured_trip_request = _structured_trip_request_from_job(job)
        structured_pre_gate_done = False
        must_include_resolution: MustIncludeResolution | None = None
        persisted_trip_request_json: dict | None = None
        failed_draft_snapshot: list[dict] = []

        async def on_writer_output(plans) -> None:
            nonlocal failed_draft_snapshot
            snapshot = project_failed_draft_plans(plans)
            if snapshot:
                # Keep only the latest non-empty Writer projection in memory.
                # Nothing is persisted before a final FAILED/TIMEOUT state.
                failed_draft_snapshot = snapshot

        async def persist_captured_failed_draft() -> None:
            if not failed_draft_snapshot:
                return
            try:
                await persist_failed_draft_if_eligible(
                    job_id,
                    failed_draft_snapshot,
                )
            except Exception:
                # Draft capture is auxiliary and must not rewrite terminal job
                # state or alter Writer/Review/Publish Gate behavior.
                logger.warning(
                    "trip failed-draft persistence failed job_id=%s",
                    job_id,
                    exc_info=True,
                )

        async def on_stage(stage: str) -> None:
            nonlocal pending_current_stage, latest_workflow_stage
            pending_current_stage = stage
            latest_workflow_stage = stage

        async def flush_observation_writes() -> None:
            # P1 stage writes are awaited inline. Kept as a local compatibility
            # hook for the existing terminal flow and tests.
            return None

        async def flush_pending_stage_success() -> None:
            nonlocal pending_success_event
            if pending_success_event is None:
                return
            previous = pending_success_event
            pending_success_event = None
            await record_trip_job_step_event(
                job_id,
                stage=str(previous.get("stage") or ""),
                status="SUCCESS",
                attempt=int(previous.get("attempt") or 1),
                publish_retry_round=int(
                    previous.get("publish_retry_round") or 0
                ),
                latency_ms=previous.get("latency_ms"),
                metadata=previous.get("metadata") or {},
            )

        async def on_stage_event(
            stage: str,
            status: str,
            metadata: dict,
        ) -> None:
            nonlocal pending_current_stage, pending_success_event, latest_workflow_stage
            nonlocal latest_workflow_attempt, latest_workflow_publish_retry_round
            nonlocal completed_automatic_rewrite_rounds
            latest_workflow_stage = stage
            event_metadata = dict(metadata)
            if stage == "PERSISTING" and status in {"SUCCESS", "FAILED"}:
                event_metadata.update(runtime_metrics)
            attempt = int(event_metadata.get("attempt") or 1)
            publish_retry_round = int(
                event_metadata.get("publish_retry_round") or 0
            )
            latest_workflow_attempt = attempt
            latest_workflow_publish_retry_round = publish_retry_round
            if stage == "PUBLISH_RETRY" and status == "SUCCESS":
                completed_automatic_rewrite_rounds = max(
                    completed_automatic_rewrite_rounds,
                    min(1, publish_retry_round),
                )
            if status == "SUCCESS" and stage != "PERSISTING":
                pending_success_event = {
                    "stage": stage,
                    "attempt": attempt,
                    "publish_retry_round": publish_retry_round,
                    "latency_ms": (
                        int(event_metadata["latency_ms"])
                        if event_metadata.get("latency_ms") is not None
                        else None
                    ),
                    "metadata": event_metadata,
                }
                return
            if status == "RUNNING" and pending_current_stage == stage:
                pending_current_stage = None
                previous = pending_success_event
                pending_success_event = None
                await record_trip_job_stage_started(
                    job_id,
                    stage=stage,
                    attempt=attempt,
                    publish_retry_round=publish_retry_round,
                    metadata=event_metadata,
                    previous_success_event=previous,
                )
                return
            if pending_success_event is not None:
                await flush_pending_stage_success()
            if status == "RUNNING":
                await record_trip_job_step_event(
                    job_id,
                    stage=stage,
                    status=status,
                    attempt=attempt,
                    publish_retry_round=publish_retry_round,
                    latency_ms=(
                        int(event_metadata["latency_ms"])
                        if event_metadata.get("latency_ms") is not None
                        else None
                    ),
                    metadata=event_metadata,
                )
                return
            await flush_observation_writes()
            await record_trip_job_step_event(
                job_id,
                stage=stage,
                status=status,
                attempt=attempt,
                publish_retry_round=publish_retry_round,
                latency_ms=(
                    int(event_metadata["latency_ms"])
                    if event_metadata.get("latency_ms") is not None
                    else None
                ),
                metadata=event_metadata,
            )

        async def maybe_create_initial_city_batch(
            decision: CityGateDecision,
            trip_request: TripRequest,
        ) -> None:
            if decision.status != CITY_PREPARING or decision.city_id is None:
                return
            active_batch = await get_active_city_batch()
            if active_batch is not None:
                logger.info(
                    "skip initial city batch job_id=%s city_id=%s active_batch_id=%s",
                    job_id,
                    decision.city_id,
                    active_batch.batch_id,
                )
                return
            try:
                batch = await create_city_crawl_batch(
                    city_id=decision.city_id,
                    trigger_source="demand",
                    reason="first_request",
                    preferences=trip_request.preferences,
                    limit_per_keyword=settings.city_batch_limit_per_keyword,
                )
            except CityBatchActiveError:
                logger.info(
                    "initial city batch race lost job_id=%s city_id=%s",
                    job_id,
                    decision.city_id,
                )
                return
            logger.info(
                "initial city batch created job_id=%s city_id=%s batch_id=%s",
                job_id,
                decision.city_id,
                batch.batch_id,
            )

        async def persist_trip_request_after_city_gate(
            trip_request: TripRequest,
            decision: CityGateDecision,
        ) -> None:
            nonlocal city_notice_code, persisted_trip_request_json
            city_notice_code = decision.city_notice_code
            trip_request_json = trip_request.model_dump()
            if city_notice_code:
                trip_request_json["city_notice_code"] = city_notice_code
            if not decision.allowed:
                trip_request_json.update(_city_gate_rejection_metadata(decision))
            update_trip_request_t0 = time.monotonic()
            await update_trip_job_trip_request(job_id, trip_request_json)
            persisted_trip_request_json = trip_request_json
            runtime_metrics["trip_request_update_latency_ms"] = _elapsed_ms(
                update_trip_request_t0
            )

        async def pre_gate_structured_trip_request(trip_request: TripRequest) -> None:
            nonlocal structured_pre_gate_done, must_include_resolution
            city_gate_t0 = time.monotonic()
            decision = await gate_async_trip_request(
                trip_request,
                source=job.source,
                request_id=job.request_id,
                conversation_id=job.conversation_id,
                raw_query=job.user_query,
            )
            runtime_metrics["city_gate_latency_ms"] = _elapsed_ms(city_gate_t0)
            await persist_trip_request_after_city_gate(trip_request, decision)
            structured_pre_gate_done = True
            if not decision.allowed:
                await maybe_create_initial_city_batch(decision, trip_request)
                raise CityGateRejected(decision)
            resolution = await match_and_record_must_include(
                trip_request,
                source="user_must_include",
                request_id=job.request_id,
                conversation_id=job.conversation_id,
                countable=job.source not in NON_DEMAND_SOURCES,
            )
            if getattr(settings, "must_include_anchor_enabled", False):
                must_include_resolution = resolution

        async def on_trip_request(trip_request: TripRequest) -> None:
            nonlocal city_notice_code, persisted_trip_request_json
            callback_t0 = time.monotonic()
            try:
                if structured_pre_gate_done:
                    trip_request_json = trip_request.model_dump()
                    if city_notice_code:
                        trip_request_json["city_notice_code"] = city_notice_code
                    if trip_request_json != persisted_trip_request_json:
                        update_trip_request_t0 = time.monotonic()
                        await update_trip_job_trip_request(job_id, trip_request_json)
                        persisted_trip_request_json = trip_request_json
                        runtime_metrics["trip_request_update_latency_ms"] = _elapsed_ms(
                            update_trip_request_t0
                        )
                    return

                city_gate_t0 = time.monotonic()
                decision = await gate_async_trip_request(
                    trip_request,
                    source=job.source,
                    request_id=job.request_id,
                    conversation_id=job.conversation_id,
                    raw_query=job.user_query,
                )
                runtime_metrics["city_gate_latency_ms"] = _elapsed_ms(city_gate_t0)
                await persist_trip_request_after_city_gate(trip_request, decision)
                if not decision.allowed:
                    await maybe_create_initial_city_batch(decision, trip_request)
                    raise CityGateRejected(decision)
            finally:
                runtime_metrics["trip_request_callback_latency_ms"] = _elapsed_ms(
                    callback_t0
                )

        try:
            recent_history_t0 = time.monotonic()
            recent_place_id_sets = await get_recent_successful_plan_place_ids(
                source=job.source,
                conversation_id=job.conversation_id,
            )
            runtime_metrics["recent_history_query_latency_ms"] = _elapsed_ms(
                recent_history_t0
            )
            runtime_metrics["pre_workflow_latency_ms"] = _elapsed_ms(execute_t0)
            if (
                structured_trip_request is not None
                and getattr(settings, "place_demand_enabled", False)
            ):
                await pre_gate_structured_trip_request(structured_trip_request)
            remaining_seconds = workflow_deadline_monotonic - time.monotonic()
            if remaining_seconds <= 0:
                raise asyncio.TimeoutError("trip hard deadline exhausted before workflow")
            with llm_call_context(
                job_id=job_id,
                request_id=job.request_id,
                workflow_deadline_monotonic=workflow_deadline_monotonic,
            ):
                bind_observation_sink(job_id)
                result = await asyncio.wait_for(
                    run_trip_workflow(
                        job.user_query,
                        trip_request=structured_trip_request,
                        recent_place_id_sets=recent_place_id_sets,
                        on_stage=on_stage,
                        on_stage_event=on_stage_event,
                        on_trip_request=on_trip_request,
                        on_writer_output=on_writer_output,
                        must_include_resolution=must_include_resolution,
                        workflow_deadline_monotonic=workflow_deadline_monotonic,
                        generation_policy_version=generation_policy_version,
                    ),
                    timeout=remaining_seconds,
                )
            if pending_success_event is not None:
                await flush_observation_writes()
                await flush_pending_stage_success()
        except asyncio.TimeoutError:
            logger.warning("trip worker timeout job_id=%s", job_id)
            await flush_observation_writes()
            await flush_pending_stage_success()
            flushed = pop_job_observation_flush(job_id) or {}
            summary_blob = {
                "llm_call_count_total": int(flushed.get("llm_call_count_total") or 0),
                "llm_error_count_total": int(flushed.get("llm_error_count_total") or 0),
                "termination_reason": "workflow_cancelled",
                "response_received": False,
            }
            detail = json.dumps(summary_blob, ensure_ascii=False)
            observation_meta = {
                "error": "trip worker timeout",
                "llm_observation": flushed or None,
            }
            timeout_error = asyncio.TimeoutError("trip worker timeout")
            observation_meta["failure_observation"] = _build_failure_observation(
                timeout_error,
                stage=latest_workflow_stage,
                error_code="TIMEOUT",
                completed_rewrite_rounds=completed_automatic_rewrite_rounds,
                failed_draft_available=bool(failed_draft_snapshot),
            )
            await mark_trip_job_timeout(
                job_id,
                error_detail=detail,
                step_metadata=observation_meta,
                ensure_terminal_step=True,
                delivery=_terminal_delivery_for_failure(
                    generation_policy_version, "TIMEOUT"
                ),
            )
            await persist_captured_failed_draft()
            return
        except asyncio.CancelledError:
            logger.warning("trip worker cancelled job_id=%s", job_id)
            await flush_observation_writes()
            await flush_pending_stage_success()
            flushed = pop_job_observation_flush(job_id) or {}
            summary_blob = {
                "llm_call_count_total": int(flushed.get("llm_call_count_total") or 0),
                "llm_error_count_total": int(flushed.get("llm_error_count_total") or 0),
                "termination_reason": "workflow_cancelled",
                "response_received": False,
            }
            detail = json.dumps(summary_blob, ensure_ascii=False)
            observation_meta = {
                "error": "trip worker cancelled",
                "llm_observation": flushed or None,
            }
            cancelled_error = RuntimeError("trip worker cancelled")
            observation_meta["failure_observation"] = _build_failure_observation(
                cancelled_error,
                stage=latest_workflow_stage,
                error_code="CANCELLED",
                completed_rewrite_rounds=completed_automatic_rewrite_rounds,
                failed_draft_available=bool(failed_draft_snapshot),
            )
            await mark_trip_job_failed(
                job_id,
                error_code="CANCELLED",
                error_message="规划已取消",
                error_detail=detail,
                step_metadata=observation_meta,
                ensure_terminal_step=True,
                delivery=_terminal_delivery_for_failure(
                    generation_policy_version, "CANCELLED"
                ),
            )
            await persist_captured_failed_draft()
            raise
        except CityGateRejected as exc:
            logger.info(
                "trip worker rejected job_id=%s error_code=%s",
                job_id,
                exc.decision.status,
            )
            # Always drop job-owned sink on non-success exits too.
            flushed = pop_job_observation_flush(job_id) or {}
            await flush_observation_writes()
            await flush_pending_stage_success()
            if exc.decision.status == CITY_CLARIFICATION_REQUIRED:
                owning_stage = latest_workflow_stage or "INTENT_PARSER"
                message, flushed, usage, polish_cancelled = await _observe_terminal_polish(
                    lambda: _polish_city_clarification_message(
                        job.user_query,
                        deadline_monotonic=workflow_deadline_monotonic,
                        job_id=job_id,
                        request_id=job.request_id,
                    ),
                    prior_summary=flushed,
                    stage=owning_stage,
                    attempt=latest_workflow_attempt,
                    publish_retry_round=latest_workflow_publish_retry_round,
                    job_id=job_id,
                    request_id=job.request_id,
                    fallback_result=CITY_CLARIFICATION_FALLBACK_MESSAGE,
                )
                enriched = await _enrich_terminal_polish_observation(
                    job_id,
                    stage=owning_stage,
                    attempt=latest_workflow_attempt,
                    publish_retry_round=latest_workflow_publish_retry_round,
                    summary=flushed,
                    usage=usage,
                )
                await mark_trip_job_rejected_with_message(
                    job_id,
                    error_code=exc.decision.status,
                    error_message=message,
                    step_metadata=(
                        None
                        if enriched
                        else {"llm_observation": flushed, "llm_usage": usage}
                    ),
                    ensure_terminal_evidence=not enriched,
                    delivery=_terminal_delivery_for_failure(
                        generation_policy_version, exc.decision.status
                    ),
                )
                if polish_cancelled:
                    raise asyncio.CancelledError
            else:
                await mark_trip_job_rejected(
                    job_id,
                    error_code=exc.decision.status,
                    delivery=_terminal_delivery_for_failure(
                        generation_policy_version, exc.decision.status
                    ),
                )
            return
        except Exception as exc:
            logger.exception("trip worker failed job_id=%s", job_id)
            await flush_observation_writes()
            await flush_pending_stage_success()
            flushed = pop_job_observation_flush(job_id) or {}
            summary_blob = {
                "error_type": type(exc).__name__,
                "llm_call_count_total": int(
                    flushed.get("llm_call_count_total") or 0
                ),
                "llm_error_count_total": int(
                    flushed.get("llm_error_count_total") or 0
                ),
                "termination_reason": "workflow_failed",
                "response_received": bool(
                    flushed.get("llm_response_received_count")
                ),
            }
            observation_meta = {
                "error": str(exc),
                "llm_observation": flushed or None,
            }
            if isinstance(exc, WriterRelayError):
                observation_meta["writer_relay_capacity"] = {
                    "code": exc.code,
                    "waited_ms": exc.waited_ms,
                    "endpoints": [
                        {
                            "name": item.name,
                            "participation": item.participation,
                            "circuit_state": item.circuit_state,
                            "inflight": item.inflight,
                            "cap": item.cap,
                        }
                        for item in exc.snapshot
                    ],
                }
            error_code, error_message = _classify_error(exc)
            failure_observation = _build_failure_observation(
                exc,
                stage=latest_workflow_stage,
                error_code=error_code,
                completed_rewrite_rounds=completed_automatic_rewrite_rounds,
                failed_draft_available=bool(failed_draft_snapshot),
            )
            summary_blob["failure_observation"] = failure_observation
            detail = json.dumps(summary_blob, ensure_ascii=False)
            observation_meta["failure_observation"] = failure_observation
            polish_cancelled = False
            if isinstance(exc, PublishGateError):
                observation_meta["error"] = error_code
                owning_stage = latest_workflow_stage or "PUBLISH_GATE"
                (
                    error_message,
                    flushed,
                    usage,
                    polish_cancelled,
                ) = await _observe_terminal_polish(
                    lambda: _polish_publish_failure_message(
                        exc,
                        deadline_monotonic=workflow_deadline_monotonic,
                        job_id=job_id,
                        request_id=job.request_id,
                    ),
                    prior_summary=flushed,
                    stage=owning_stage,
                    attempt=latest_workflow_attempt,
                    publish_retry_round=latest_workflow_publish_retry_round,
                    job_id=job_id,
                    request_id=job.request_id,
                    fallback_result=PUBLISH_FAILURE_FALLBACK_MESSAGE,
                )
                observation_meta["llm_observation"] = flushed
                await _enrich_terminal_polish_observation(
                    job_id,
                    stage=owning_stage,
                    attempt=latest_workflow_attempt,
                    publish_retry_round=latest_workflow_publish_retry_round,
                    summary=flushed,
                    usage=usage,
                )
                summary_blob["llm_call_count_total"] = int(
                    flushed.get("llm_call_count_total") or 0
                )
                summary_blob["llm_error_count_total"] = int(
                    flushed.get("llm_error_count_total") or 0
                )
                summary_blob["response_received"] = bool(
                    flushed.get("llm_response_received_count")
                )
                detail = json.dumps(summary_blob, ensure_ascii=False)
            await mark_trip_job_failed(
                job_id,
                error_code=error_code,
                error_message=error_message,
                error_detail=detail,
                step_metadata=observation_meta,
                ensure_terminal_step=True,
                delivery=_terminal_delivery_for_failure(
                    generation_policy_version, error_code
                ),
            )
            await persist_captured_failed_draft()
            if polish_cancelled:
                raise asyncio.CancelledError
            return

        # Success path: clear job sink after workflow returns.
        pop_job_observation_flush(job_id)
        reply_text = format_plans_markdown(result)
        result_type = result.result_type
        plan_count = len(result.plans)

        try:
            delivery = _terminal_delivery_for_result(
                generation_policy_version, result
            )
            await _mark_trip_job_success_with_retry(
                job_id=job_id,
                reply_text=reply_text,
                plan_count=plan_count,
                result_type=result_type,
                result_record_id=result.record_id,
                trip_request_json={
                    **result.trip_request.model_dump(),
                    **({"route_failure_code": result.quality_metrics["accommodation_failure_code"]}
                       if result.result_type == "NO_USABLE_ROUTE" and result.quality_metrics.get("accommodation_failure_code")
                       in {"ACCOMMODATION_UNRESOLVED", "ROUTE_SEARCH_EXHAUSTED"} else {}),
                    **({"city_notice_code": city_notice_code} if city_notice_code else {}),
                },
                delivery=delivery,
                route_diagnostics=result.route_diagnostics,
                route_replay_data=result.route_replay_data,
            )
        except Exception as exc:
            logger.exception("trip worker persistence failed job_id=%s", job_id)
            pop_job_observation_flush(job_id)
            error_code, error_message = _classify_error(exc)
            await mark_trip_job_failed(
                job_id,
                error_code=error_code,
                error_message=error_message,
                error_detail=str(exc),
                delivery=_terminal_delivery_for_failure(
                    generation_policy_version, error_code
                ),
            )
            await persist_captured_failed_draft()
            return
        logger.info(
            "trip worker success job_id=%s result_type=%s plan_count=%s",
            job_id,
            result_type,
            plan_count,
        )


async def _mark_trip_job_success_with_retry(
    *,
    job_id: str,
    reply_text: str,
    plan_count: int,
    result_type: str,
    result_record_id: int | None,
    trip_request_json: dict,
    delivery: GenerationDelivery | None = None,
    route_diagnostics: dict | None = None,
    route_replay_data: dict | None = None,
) -> None:
    last_error: Exception | None = None
    for attempt in range(1, SUCCESS_PERSIST_RETRIES + 1):
        try:
            await mark_trip_job_success(
                job_id,
                reply_text=reply_text,
                plan_count=plan_count,
                result_type=result_type,
                result_record_id=result_record_id,
                trip_request_json=trip_request_json,
                delivery=delivery,
                route_diagnostics=route_diagnostics,
                route_replay_data=route_replay_data,
            )
            return
        except Exception as exc:
            last_error = exc
            if attempt >= SUCCESS_PERSIST_RETRIES:
                break
            logger.warning(
                "trip worker success persistence retry job_id=%s attempt=%s error=%s",
                job_id,
                attempt,
                exc,
            )
            await asyncio.sleep(0.5 * attempt)
    assert last_error is not None
    raise last_error
