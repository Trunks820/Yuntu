"""Trip workflow: Intent Parser -> Data Retrieval -> Final Writer -> persist."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import time
from datetime import date

from sqlalchemy import text

from src.agents import (
    accommodation_resolver,
    data_retrieval,
    final_writer,
    yuntu_review,
    intercity_transport_resolver,
    itinerary_budget,
    llm,
    poi_alias,
    poi_selection,
    route_planner,
    route_planning,
    route_diagnostics,
)
from src.agents.amap_poi_detail import (
    AmapPoiDetailResult,
    AmapPoiDetailTaskHandle,
    amap_poi_detail_client_from_settings,
    await_amap_poi_detail_at_writer_barrier,
    settle_amap_poi_detail_task,
    start_amap_poi_detail_task,
)
from src.agents.lodging_section import (
    LodgingPriceTaskHandle,
    await_lodging_price_at_writer_barrier,
    build_lodging_display,
    lodging_writer_context,
    render_lodging_display_text,
    settle_lodging_price_task,
    start_lodging_price_for_trip,
)
from src.agents.composition_blueprint import (
    build_base_composition_blueprints,
    enrich_blueprints_with_food,
    meal_role_recovery_metrics,
)
from src.agents.evidence_strength import (
    StructuredEvidencePayload,
    build_structured_evidence_payload,
)
from src.agents.generation_model_routing import SharedCallBudget
from src.agents.pretrip_advice import (
    PreTripAdvicePayload,
    build_pretrip_advice_payload,
)
from src.agents.diversity import build_quality_metrics
from src.agents.dispatch_resolver import (
    DispatchDecision,
    STRUCTURAL_INCOMPLETE_REASONS,
)
from src.agents.intent_parser import (
    INTENT_SCHEMA_COERCION_DEFAULT_METADATA,
    parse_intent_with_metadata,
)
from src.agents.pace import (
    COMPACT_PACE_MARKERS,
    RELAXED_PACE_MARKERS,
    detect_pace_mode,
    generation_base_mode,
)
from src.agents.route_notices import (
    ACCOMMODATION_UNRESOLVED_NOTICE,
    ACCOMMODATION_FALLBACK_NOTICE,
    NO_USABLE_ROUTE_NOTICE,
    ROUTE_DEGRADATION_NOTICE,
    TRANSPORT_STATIC_FALLBACK_NOTICE,
)
from src.agents.schema import (
    AccommodationAnchor,
    AccommodationSuggestion,
    CandidatePlace,
    PlanOutput,
    RetrievalResult,
    RoutePlan,
    TransportSuggestion,
    TripRequest,
    WorkflowResult,
    DeliveryMetadata,
)
from src.agents.weather_advisory import build_weather_advisory_payload
from src.agents.workflow_observer import (
    StageCallback,
    StageEventCallback,
    TripRequestCallback,
    _emit_trip_request,
    _run_observed_step,
    start_stage_timing_observation,
    stop_stage_timing_observation,
    summarize_stage_timing_observation,
)
from src.agents.write_review_pipeline import (
    ContentDispatchSignal,
    WriterOutputCallback,
    WriteReviewPublishPipeline,
)
from src.cost_estimate.workflow_integration import (
    IntercityCostPrefetch,
    cancel_intercity_cost_prefetch,
    resolve_workflow_cost_snapshots,
    start_intercity_cost_prefetch,
)
from src.agents import publish_gate
from src.config import get_settings
from src.jobs.place_demand import MustIncludeResolution
from src.pipeline.db import get_session_factory

logger = logging.getLogger(__name__)
WORKFLOW_HARD_TIMEOUT_SECONDS = 180.0
_ROUTE_PLANNER_TRACE_LIMIT = 8
_ROUTE_PLANNER_TRACE_TEXT = 180


def _clip_route_planner_text(value: object) -> str:
    text = " ".join(str(value).split())
    if len(text) > _ROUTE_PLANNER_TRACE_TEXT:
        return text[:_ROUTE_PLANNER_TRACE_TEXT]
    return text


def _route_planner_call_row(call) -> dict:
    return {
        "index": int(getattr(call, "index", 0) or 0),
        "repaired": bool(getattr(call, "repaired", False)),
        "json_retry": bool(getattr(call, "json_retry", False)),
        "latency_ms": int(getattr(call, "latency_ms", 0) or 0),
        "prompt_tokens": int(getattr(call, "prompt_tokens", 0) or 0),
        "completion_tokens": int(getattr(call, "completion_tokens", 0) or 0),
        "provider": str(getattr(call, "provider", "") or ""),
        "model": str(getattr(call, "model", "") or ""),
        "backup_reason": str(getattr(call, "backup_reason", "") or ""),
    }


def _route_planner_stage_trace(*, source: str, reason: str, result) -> dict:
    issues: list[str] = []
    adjustments: list[str] = []
    calls: list[dict] = []
    call_count = 0
    schedule_ms = 0
    elapsed_ms = 0
    amap_calls = 0
    estimated_legs = 0
    lodging_rests = 0
    sight_extends = 0
    if result is not None:
        issues = [
            _clip_route_planner_text(item)
            for item in list(getattr(result, "issues", None) or [])[:_ROUTE_PLANNER_TRACE_LIMIT]
        ]
        adjustments = [
            _clip_route_planner_text(item)
            for item in list(getattr(result, "adjustments", None) or [])[:_ROUTE_PLANNER_TRACE_LIMIT]
        ]
        raw_calls = list(getattr(result, "calls", None) or [])
        call_count = len(raw_calls)
        calls = [
            _route_planner_call_row(call)
            for call in raw_calls[:_ROUTE_PLANNER_TRACE_LIMIT]
        ]
        schedule_ms = int(getattr(result, "schedule_ms", 0) or 0)
        elapsed_ms = int(getattr(result, "elapsed_ms", 0) or 0)
        amap_calls = int(getattr(result, "amap_call_count", 0) or 0)
        estimated_legs = int(getattr(result, "estimated_leg_count", 0) or 0)
        lodging_rests = int(getattr(result, "lodging_rest_count", 0) or 0)
        sight_extends = int(getattr(result, "sight_extend_count", 0) or 0)
        kept_first_draft = bool(getattr(result, "kept_first_draft", False))
        lodging_first = str(getattr(result, "lodging_first", "") or "off")
    else:
        kept_first_draft = False
        lodging_first = "off"
    failure_kind, failure_status = _route_planner_failure_record(result)
    return {
        "route_source": source,
        "route_planner_fallback_reason": reason,
        "route_planner_failure_kind": failure_kind,
        "route_planner_failure_status": failure_status,
        "route_planner_issues": issues,
        "route_planner_adjustments": adjustments,
        "route_planner_call_count": call_count,
        "route_planner_calls": calls,
        "route_planner_schedule_ms": schedule_ms,
        "route_planner_elapsed_ms": elapsed_ms,
        "route_planner_amap_call_count": amap_calls,
        "route_planner_estimated_leg_count": estimated_legs,
        "route_planner_lodging_rest_count": lodging_rests,
        "route_planner_sight_extend_count": sight_extends,
        "route_planner_kept_first_draft": kept_first_draft,
        "route_planner_lodging_first": lodging_first,
    }


def _model_route_budget_ready(plan: RoutePlan, request: TripRequest) -> bool:
    """True when the model route's locked facts survive the pre-adopt check.

    This runs while the backup route is still going. A failed check keeps
    that backup. The same ``resolve_route_budgets`` result is what the later
    v2 gate would otherwise turn into ``NO_USABLE_ROUTE``.
    """
    results = itinerary_budget.resolve_route_budgets([plan], request)
    if len(results) != 1:
        return False
    days = results[0].days
    return (
        len(days) == int(request.days)
        and len(plan.day_groups) == int(request.days)
        and all(day.status == "within_budget" for day in days)
    )


def _route_planner_usable(result, days: int) -> bool:
    plan = getattr(result, "plan", None) if result is not None else None
    groups = getattr(plan, "day_groups", None) if plan is not None else None
    return bool(
        result is not None
        and getattr(result, "qualified", False)
        and plan is not None
        and int(days) > 0
        and len(groups or []) == int(days)
    )


def _log_route_planner_outcome(*, source: str, reason: str, result) -> None:
    """One line when the model route finishes. No prompt and no secret."""
    qualified = False
    call_count = 0
    schedule_ms = 0
    elapsed_ms = 0
    amap_calls = 0
    estimated_legs = 0
    lodging_rests = 0
    sight_extends = 0
    if result is not None:
        qualified = bool(getattr(result, "qualified", False))
        call_count = len(list(getattr(result, "calls", None) or []))
        schedule_ms = int(getattr(result, "schedule_ms", 0) or 0)
        elapsed_ms = int(getattr(result, "elapsed_ms", 0) or 0)
        amap_calls = int(getattr(result, "amap_call_count", 0) or 0)
        estimated_legs = int(getattr(result, "estimated_leg_count", 0) or 0)
        lodging_rests = int(getattr(result, "lodging_rest_count", 0) or 0)
        sight_extends = int(getattr(result, "sight_extend_count", 0) or 0)
    logger.info(
        "route_planner outcome source=%s reason=%s qualified=%s calls=%s "
        "schedule_ms=%s elapsed_ms=%s amap_calls=%s estimated_legs=%s "
        "lodging_rests=%s sight_extends=%s",
        source,
        reason,
        qualified,
        call_count,
        schedule_ms,
        elapsed_ms,
        amap_calls,
        estimated_legs,
        lodging_rests,
        sight_extends,
    )


def _route_planner_failure_record(result) -> tuple[str, int | None]:
    if result is None:
        return "", None
    kind = str(getattr(result, "failure_kind", "") or "").strip()
    raw_status = getattr(result, "failure_status", None)
    if isinstance(raw_status, bool) or not isinstance(raw_status, int):
        return kind, None
    return kind, raw_status


def _route_planner_fallback_reason(result) -> str:
    if result is None:
        return "model_error"
    # Timeout keeps the reason it had before failure kinds existed.
    # Bad output is a model-output problem. Quota, rate limits, server
    # errors and other call failures share model_unavailable. Issue text
    # still classifies results that do not carry a kind.
    kind, _status = _route_planner_failure_record(result)
    if kind == "timeout":
        return "timeout"
    if kind == "bad_output":
        return "model_bad_output"
    if kind:
        return "model_unavailable"
    issues = [str(item) for item in (getattr(result, "issues", None) or [])]
    if any(item.startswith("交给现有路线格式失败") for item in issues):
        return "emit_failed"
    text = " ".join(issues)
    if "TimeoutError" in text or "超时" in text:
        return "timeout"
    return "unqualified"


async def _retrieve_route_task(task: asyncio.Task) -> None:
    """Wait out a child task without adopting its cancellation."""
    await asyncio.wait({task})
    if task.cancelled():
        return
    task.exception()


async def _drain_route_task(task: asyncio.Task | None) -> None:
    """Cancel one route task and retrieve its outcome.

    ``asyncio.wait`` does not turn a cancelled child into a cancelled trip.
    Cancelling this trip still propagates, and a shielded retrieve keeps the
    child from running on after that.
    """
    if task is None:
        return
    if not task.done():
        task.cancel()
    try:
        await _retrieve_route_task(task)
    except asyncio.CancelledError:
        if not task.done():
            task.cancel()
        await asyncio.shield(_retrieve_route_task(task))
        raise


async def _await_route_planner(task: asyncio.Task, deadline_monotonic: float):
    """Return the planner result, or a fallback reason once the deadline hits.

    A result that is already finished is kept. A task still running at the
    deadline is cancelled and is not left in the background.
    """
    remaining = float(deadline_monotonic) - time.monotonic()
    if remaining > 0:
        done, _pending = await asyncio.wait({task}, timeout=remaining)
        if task not in done:
            await _drain_route_task(task)
    else:
        await _drain_route_task(task)
    if task.cancelled():
        return None, "deadline"
    error = task.exception()
    if error is not None:
        if isinstance(error, (TimeoutError, asyncio.TimeoutError)):
            return None, "timeout"
        return None, "model_error"
    return task.result(), ""


def _planner_user_lodging_anchor(
    trip_request: TripRequest,
    accommodation: AccommodationSuggestion | None,
    accommodation_context,
) -> AccommodationAnchor | None:
    """User lodging for the model route. Automatic picks stay with the planner.

    A fixed resolver anchor keeps its own point / area precision. With the
    selector v2 path off, ``resolve_accommodation`` returns a suggestion only;
    a user-supplied lodging is then a point, matching the non-coupling context.
    """
    from src.agents.route_feasibility import valid_coordinate

    if accommodation_context is not None:
        if accommodation_context.state == "fixed" and accommodation_context.anchors:
            candidate = accommodation_context.anchors[0]
            suggestion = candidate.suggestion
            if (
                suggestion is not None
                and suggestion.source == "user_specified"
                and valid_coordinate(suggestion.latitude, suggestion.longitude)
            ):
                return candidate
        return None
    if (
        accommodation is not None
        and accommodation.source == "user_specified"
        and valid_coordinate(accommodation.latitude, accommodation.longitude)
    ):
        return AccommodationAnchor(
            suggestion=accommodation,
            location_precision="point" if trip_request.accommodation else "area",
        )
    return None


async def _run_route_planner(
    trip_request: TripRequest,
    retrieval,
    *,
    settings,
    lodging_anchor: AccommodationAnchor | None = None,
):
    """One model route. Its Amap provider is created inside ``plan_route``."""
    citycode = None
    if settings.commute_mode_enabled:
        try:
            citycode = await route_planning._load_citycode(
                trip_request.to_city or "",
                {
                    place.adcode
                    for place in retrieval.candidates
                    if getattr(place, "adcode", None)
                },
            )
        except Exception:
            logger.warning(
                "Amap transit citycode unavailable; transit legs will use estimates",
                exc_info=True,
            )
            citycode = None

    def provider_factory():
        return route_planning.AmapRouteProvider(
            time_budget_ms=max(
                0,
                int(round(float(settings.route_planner_amap_time_budget_seconds) * 1000)),
            ),
        )

    async with get_session_factory()() as session:
        return await route_planner.plan_route(
            trip_request,
            retrieval,
            session=session,
            provider_factory=provider_factory,
            settings=settings,
            citycode=citycode,
            lodging_anchor=lodging_anchor,
        )


class StructuredTripRequestRequiredError(ValueError):
    """Natural-language workflow entry is disabled for the hosted product."""

    code = "STRUCTURED_TRIP_REQUEST_REQUIRED"

    def __init__(self) -> None:
        super().__init__(
            "trip_request is required; natural-language Intent Parser entry "
            "is temporarily disabled"
        )


def flatten_locked_places_from_routes(
    route_plans: list[RoutePlan],
) -> list[CandidatePlace]:
    places: list[CandidatePlace] = []
    for route_plan in route_plans:
        for day_group in route_plan.day_groups:
            places.extend(list(day_group.places))
    return places


def start_amap_poi_detail_for_locked_routes(
    route_plans: list[RoutePlan],
    *,
    settings=None,
) -> AmapPoiDetailTaskHandle:
    resolved = settings or get_settings()
    client = amap_poi_detail_client_from_settings(resolved)
    return start_amap_poi_detail_task(
        client,
        flatten_locked_places_from_routes(route_plans),
    )


def build_plan_scoped_pretrip_advice_payloads(
    *,
    trip_request: TripRequest,
    route_plans: list[RoutePlan],
    weather_advisory_payload,
    structured_evidence_payload: StructuredEvidencePayload | None,
    amap_facts,
) -> list[PreTripAdvicePayload]:
    return [
        build_pretrip_advice_payload(
            trip_request=trip_request,
            route_plan=route_plan,
            weather_advisory_payload=weather_advisory_payload,
            structured_evidence_payload=structured_evidence_payload,
            amap_facts=amap_facts,
        )
        for route_plan in route_plans
    ]


def content_free_pretrip_advice_metrics(
    amap_result: AmapPoiDetailResult,
    payloads: list[PreTripAdvicePayload],
) -> dict:
    metrics = dict(amap_result.content_free_metrics())
    metrics["pretrip_advice_plan_count"] = len(payloads)
    metrics["pretrip_advice_note_evidence_count"] = sum(
        len(payload.note_tip_evidence) for payload in payloads
    )
    metrics["pretrip_advice_amap_fact_count"] = sum(
        len(payload.amap_tip_facts) for payload in payloads
    )
    return metrics


async def _enrich_after_budget_lock(
    *,
    trip_request: TripRequest,
    retrieval: RetrievalResult,
    route_plans: list[RoutePlan],
    must_include_resolution,
    pre_route_selection_quality: dict,
) -> tuple[
    AmapPoiDetailTaskHandle,
    list,
    list,
    dict,
    dict,
    dict,
    dict,
]:
    amap_handle = start_amap_poi_detail_for_locked_routes(route_plans)
    try:
        poi_identity_results = poi_alias.build_poi_identity_results(
            route_plans,
            retrieval.candidates,
        )
        base_blueprints = build_base_composition_blueprints(
            route_plans,
            trip_request,
        )
        food_enrichment = await enrich_blueprints_with_food(
            base_blueprints,
            route_plans,
            trip_request,
            get_session_factory(),
            get_settings(),
        )
        composition_blueprints = food_enrichment.blueprints
        post_budget_route_quality = _build_post_budget_route_quality(
            trip_request=trip_request,
            retrieval=retrieval,
            route_plans=route_plans,
            pre_route_selection_quality=pre_route_selection_quality,
            must_include_resolution=must_include_resolution,
        )
        return (
            amap_handle,
            poi_identity_results,
            composition_blueprints,
            food_enrichment.attachment_auth_map,
            food_enrichment.meal_attachment_map,
            meal_role_recovery_metrics(route_plans, composition_blueprints),
            post_budget_route_quality,
        )
    except BaseException:
        await settle_amap_poi_detail_task(amap_handle)
        raise


async def await_writer_barrier_and_build_pretrip_advice(
    amap_handle: AmapPoiDetailTaskHandle | None,
    *,
    trip_request: TripRequest,
    retrieval: RetrievalResult,
    route_plans: list[RoutePlan],
    composition_blueprints: list,
    weather_advisory_payload,
) -> tuple[
    AmapPoiDetailResult,
    StructuredEvidencePayload | None,
    list[PreTripAdvicePayload],
    dict,
]:
    if amap_handle is None:
        amap_result = AmapPoiDetailResult()
    else:
        amap_result = await await_amap_poi_detail_at_writer_barrier(amap_handle)
    if not route_plans:
        return amap_result, None, [], content_free_pretrip_advice_metrics(
            amap_result, []
        )
    structured_evidence_payload = build_structured_evidence_payload(
        retrieval,
        route_plans=route_plans,
        composition_blueprints=composition_blueprints,
    )
    payloads = build_plan_scoped_pretrip_advice_payloads(
        trip_request=trip_request,
        route_plans=route_plans,
        weather_advisory_payload=weather_advisory_payload,
        structured_evidence_payload=structured_evidence_payload,
        amap_facts=amap_result.facts,
    )
    return (
        amap_result,
        structured_evidence_payload,
        payloads,
        content_free_pretrip_advice_metrics(amap_result, payloads),
    )


def _date_or_none(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError:
        return None


def _accommodation_fallback_notice_required(
    trip_request: TripRequest,
    accommodation: AccommodationSuggestion | None,
) -> bool:
    request = trip_request.accommodation
    return (
        accommodation is not None
        and accommodation.source == "auto_recommended"
        and request is not None
        and (
            request.place_id is not None
            or bool((request.name or "").strip())
        )
    )


def _append_transport_fallback_notice(
    route_notices: list[str],
    transport: TransportSuggestion | None,
) -> None:
    if transport is not None and transport.source in {"static_fallback", "mixed"}:
        route_notices.append(TRANSPORT_STATIC_FALLBACK_NOTICE)


def _build_publish_retry_feedback(
    *,
    signal: ContentDispatchSignal,
    pipeline: WriteReviewPublishPipeline,
    trip_request: TripRequest,
    retrieval: RetrievalResult,
    route_plans: list[RoutePlan],
) -> list[dict]:
    """Build bounded, lock-owned feedback for the next full generation."""
    findings = publish_gate.check_publish_gate(
        signal.plans,
        trip_request=trip_request,
        retrieval=retrieval,
        route_plans=route_plans,
        attachment_auth_map=pipeline.attachment_auth_map,
        accommodation=pipeline.accommodation,
        body_semantics_by_review=pipeline.generation_policy_version == "v0.12",
    ).findings
    decision_reasons = set(signal.decision.reasons)
    selected = [
        finding
        for finding in findings
        if not decision_reasons or finding.reason in decision_reasons
    ] or findings

    records: list[dict] = []
    for finding in selected[:20]:
        contract = (
            pipeline.structured_evidence_payload.action_contract(
                plan_index=finding.plan_index,
                day=finding.day,
                place_id=finding.place_id,
            )
            if (
                finding.plan_index is not None
                and finding.day is not None
                and finding.place_id is not None
            )
            else None
        )
        locked_places = (
            [{
                "place_id": contract.place_id,
                "name": contract.place_name,
                "authorized_actions": list(contract.authorized_actions),
            }]
            if contract is not None
            else []
        )
        records.append({
            "reason": finding.reason,
            "plan_index": finding.plan_index,
            "day": finding.day,
            "place_id": finding.place_id,
            "locked_places": locked_places,
        })

    if not records:
        records = [
            {
                "reason": reason,
                "plan_index": (
                    signal.decision.plan_indexes[0]
                    if signal.decision.plan_indexes
                    else None
                ),
                "day": None,
                "place_id": None,
                "locked_places": [],
            }
            for reason in signal.decision.reasons[:10]
        ]
    return records


def _publish_retry_is_body_incomplete(decision: DispatchDecision) -> bool:
    """Allow the outer Writer retry only for the frozen body-incomplete family."""
    reasons = set(decision.reasons)
    return (
        decision.action == "PUBLISH_RETRY"
        and bool(reasons)
        and reasons.issubset(STRUCTURAL_INCOMPLETE_REASONS)
    )


def _build_content_dispatch_failure(
    signal: ContentDispatchSignal,
    generation_metrics: dict,
) -> publish_gate.PublishGateError:
    """Preserve only authoritative dispatch facts while converting the signal."""
    known_plan_indexes = list(dict.fromkeys(
        index
        for index in signal.decision.plan_indexes
        if isinstance(index, int)
        and not isinstance(index, bool)
        and index > 0
    ))
    reason_plan_indexes = (
        {signal.decision.reasons[0]: list(known_plan_indexes)}
        if len(signal.decision.reasons) == 1 and known_plan_indexes
        else {}
    )
    findings = [
        publish_gate.PublishFinding(
            reason=reason,
            message="; ".join(signal.decision.notes) or reason,
            plan_index=(
                reason_plan_indexes.get(reason, [None])[0]
                if reason in reason_plan_indexes
                else None
            ),
        )
        for reason in (signal.decision.reasons or ["fail_closed"])
    ]
    error = publish_gate.PublishGateError(
        publish_gate.PublishGateResult(passed=False, findings=findings)
    )
    reason_set = set(signal.decision.reasons)
    retry_eligible = bool(reason_set) and reason_set.issubset(
        STRUCTURAL_INCOMPLETE_REASONS
    )
    local_recovery_attempted = (
        generation_metrics.get("fragment_repair_attempted") is True
        or generation_metrics.get("safe_renderer_attempted") is True
    )
    setattr(error, "trace_truth_retry_eligible", retry_eligible)
    setattr(error, "trace_truth_reason_plan_indexes", reason_plan_indexes)
    setattr(
        error,
        "trace_truth_local_recovery_attempted",
        local_recovery_attempted,
    )
    if retry_eligible and any(
        "remaining_budget" in note for note in signal.decision.notes
    ):
        skip_reason = "INSUFFICIENT_TIME_BUDGET"
    elif local_recovery_attempted:
        skip_reason = "SAFE_OR_LOCAL_RECOVERY_SELECTED"
    else:
        skip_reason = "REASON_NOT_ELIGIBLE"
    setattr(error, "trace_truth_retry_not_attempted_reason", skip_reason)
    return error


def _attach_pipeline_recovery_facts(
    error: Exception,
    metrics: dict,
) -> Exception:
    """Attach explicit recovery-attempt facts without changing the exception."""
    local_recovery_attempted = (
        metrics.get("fragment_repair_attempted") is True
        or metrics.get("safe_renderer_attempted") is True
    )
    if local_recovery_attempted:
        setattr(error, "trace_truth_local_recovery_attempted", True)
        setattr(
            error,
            "trace_truth_retry_not_attempted_reason",
            "SAFE_OR_LOCAL_RECOVERY_SELECTED",
        )
        setattr(error, "trace_truth_retry_eligible", False)
    return error


async def run_trip_workflow(
    user_text: str,
    *,
    trip_request: TripRequest | None = None,
    recent_place_id_sets: list[set[int]] | None = None,
    on_stage: StageCallback | None = None,
    on_stage_event: StageEventCallback | None = None,
    on_trip_request: TripRequestCallback | None = None,
    on_writer_output: WriterOutputCallback | None = None,
    must_include_resolution: MustIncludeResolution | None = None,
    workflow_deadline_monotonic: float | None = None,
    generation_policy_version: str = "legacy",
) -> WorkflowResult:
    if trip_request is None:
        # Keep the legacy Intent Parser implementation for a future product
        # entrypoint, but disable it while Web/BFF /trip/async supplies the
        # authoritative structured TripRequest.
        raise StructuredTripRequestRequiredError()
    local_deadline = time.monotonic() + WORKFLOW_HARD_TIMEOUT_SECONDS
    if workflow_deadline_monotonic is None:
        workflow_deadline_monotonic = local_deadline
    else:
        workflow_deadline_monotonic = min(
            float(workflow_deadline_monotonic), local_deadline
        )
    remaining_seconds = workflow_deadline_monotonic - time.monotonic()
    if remaining_seconds <= 0:
        raise asyncio.TimeoutError("workflow hard deadline exhausted before start")
    token, llm_records = llm.start_llm_observation()
    stage_timing_token = start_stage_timing_observation()
    cost_prefetches: list[IntercityCostPrefetch] = []
    amap_handles: list[AmapPoiDetailTaskHandle] = []
    lodging_price_handles: list[LodgingPriceTaskHandle] = []
    try:
        with llm.llm_call_context(
            workflow_deadline_monotonic=workflow_deadline_monotonic,
        ):
            return await asyncio.wait_for(
                _run_trip_workflow_inner(
                    user_text,
                    trip_request=trip_request,
                    recent_place_id_sets=recent_place_id_sets,
                    on_stage=on_stage,
                    on_stage_event=on_stage_event,
                    on_trip_request=on_trip_request,
                    on_writer_output=on_writer_output,
                    must_include_resolution=must_include_resolution,
                    llm_records=llm_records,
                    workflow_deadline_monotonic=workflow_deadline_monotonic,
                    generation_policy_version=generation_policy_version,
                    cost_prefetches=cost_prefetches,
                    amap_handles=amap_handles,
                    lodging_price_handles=lodging_price_handles,
                ),
                timeout=remaining_seconds,
            )
    except BaseException:
        # O4: flush already-populated LLM records into the job-owned sink before
        # context is cleared so timeout/cancel can persist observability.
        try:
            context = llm._call_context.get() or {}
            job_id = str(context.get("job_id") or "").strip()
            summary = llm.current_llm_observation_summary()
            if job_id and summary.get("llm_call_count_total"):
                llm.set_job_observation_flush(job_id, summary)
        except Exception:
            logger.exception("failed to flush llm observation on workflow failure")
        raise
    finally:
        for handle in amap_handles:
            await settle_amap_poi_detail_task(handle)
        for handle in lodging_price_handles:
            await settle_lodging_price_task(handle)
        for prefetch in cost_prefetches:
            await cancel_intercity_cost_prefetch(prefetch)
        stop_stage_timing_observation(stage_timing_token)
        llm.stop_llm_observation(token)


async def _run_trip_workflow_inner(
    user_text: str,
    *,
    trip_request: TripRequest | None = None,
    recent_place_id_sets: list[set[int]] | None = None,
    on_stage: StageCallback | None = None,
    on_stage_event: StageEventCallback | None = None,
    on_trip_request: TripRequestCallback | None = None,
    on_writer_output: WriterOutputCallback | None = None,
    must_include_resolution: MustIncludeResolution | None = None,
    llm_records: list[dict] | None = None,
    workflow_deadline_monotonic: float | None = None,
    cost_prefetches: list[IntercityCostPrefetch] | None = None,
    amap_handles: list[AmapPoiDetailTaskHandle] | None = None,
    lodging_price_handles: list[LodgingPriceTaskHandle] | None = None,
    generation_policy_version: str = "legacy",
) -> WorkflowResult:
    """Full pipeline: parse -> retrieve -> route -> write/review -> persist.

    The hosted Web/BFF entry always provides *trip_request*, so the Intent
    Parser step is skipped. Its legacy implementation below is intentionally
    retained for a future explicitly authorized natural-language entrypoint.
    Optional callbacks support async worker progress updates without coupling
    workflow to job storage.

    Absolute 180s deadline starts at workflow entry and is never reset by
    Publish Retry / Fragment Repair.
    """
    if workflow_deadline_monotonic is None:
        t0 = time.monotonic()
        workflow_deadline_monotonic = (
            t0 + WORKFLOW_HARD_TIMEOUT_SECONDS
        )
    else:
        t0 = (
            workflow_deadline_monotonic
            - WORKFLOW_HARD_TIMEOUT_SECONDS
        )
    if not _must_include_anchor_active():
        must_include_resolution = None

    if trip_request is None:
        # Unreachable from run_trip_workflow while the structured Web/BFF
        # /trip/async product entry is enforced; retained for future reuse.
        logger.info("[1/5] Intent Parser ...")
        intent_parser_metadata = dict(INTENT_SCHEMA_COERCION_DEFAULT_METADATA)

        async def run_intent_parser() -> TripRequest:
            nonlocal intent_parser_metadata
            parsed, intent_parser_metadata = await parse_intent_with_metadata(
                user_text
            )
            return parsed

        trip_request = await _run_observed_step(
            "INTENT_PARSER",
            run_intent_parser,
            on_stage=on_stage,
            on_stage_event=on_stage_event,
            finish_metadata=lambda: intent_parser_metadata,
        )
    logger.info(
        "TripRequest: %s",
        json.dumps(trip_request.model_dump(), ensure_ascii=False),
    )
    trip_request = _preserve_explicit_pace_text(user_text, trip_request)
    await _emit_trip_request(on_trip_request, trip_request)
    if not trip_request.to_city.strip():
        logger.warning("TripRequest missing destination city; aborting workflow")
        return WorkflowResult(
            trip_request=trip_request,
            plans=[],
            result_type="NO_CANDIDATES",
            review_notes="缺少明确目的地城市，无法生成攻略。",
            record_id=None,
        )

    cost_intercity_prefetch = (
        start_intercity_cost_prefetch(
            trip_request,
            outbound_resolver=intercity_transport_resolver.resolve_transport,
        )
        if cost_prefetches is not None
        else None
    )
    if cost_intercity_prefetch is not None:
        cost_prefetches.append(cost_intercity_prefetch)

    logger.info("[2/5] Data Retrieval ...")
    must_include_place_ids = _workflow_must_include_place_ids(
        trip_request,
        must_include_resolution,
    )
    retrieval = await _run_observed_step(
        "DATA_RETRIEVAL",
        lambda: data_retrieval.query(
            trip_request,
            recent_place_id_sets=recent_place_id_sets,
            must_include_place_ids=must_include_place_ids,
        ),
        on_stage=on_stage,
        on_stage_event=on_stage_event,
    )
    logger.info("Candidates: %d places", len(retrieval.candidates))

    if not retrieval.candidates:
        logger.warning("No candidates found for city=%s, aborting", trip_request.to_city)
        return WorkflowResult(
            trip_request=trip_request,
            plans=[],
            result_type="NO_CANDIDATES",
            review_notes="数据库中没有找到匹配的地点数据，无法生成攻略。",
            record_id=None,
        )

    route_notices: list[str] = []
    settings = get_settings()
    transport: TransportSuggestion | None = None
    accommodation_context = None
    v2_enabled = bool(getattr(settings, "selector_route_v2_enabled", False))
    coupling_enabled = bool(v2_enabled and getattr(settings, "accommodation_route_coupling_enabled", False))
    async with get_session_factory()() as session:
        accommodation_coro = (accommodation_resolver.resolve_accommodation_context(
            trip_request, retrieval, session, coupling_enabled=coupling_enabled) if v2_enabled
            else accommodation_resolver.resolve_accommodation(trip_request, retrieval, session))
        if settings.intercity_transport_enabled:
            shared_outbound = (
                cost_intercity_prefetch.outbound_task
                if cost_intercity_prefetch is not None
                else None
            )
            accommodation, transport = await asyncio.gather(
                accommodation_coro,
                (
                    asyncio.shield(shared_outbound)
                    if shared_outbound is not None
                    else intercity_transport_resolver.resolve_transport(trip_request)
                ),
            )
        else:
            accommodation = await accommodation_coro
    if v2_enabled:
        accommodation_context = accommodation
        accommodation = (accommodation_context.anchors[0].suggestion
                         if accommodation_context.state == "fixed" else None)
    if _accommodation_fallback_notice_required(
        trip_request,
        accommodation,
    ):
        route_notices.append(ACCOMMODATION_FALLBACK_NOTICE)
    _append_transport_fallback_notice(route_notices, transport)
    accommodation_coord = (
        (accommodation.latitude, accommodation.longitude)
        if accommodation is not None
        else None
    )

    # The model route starts before selection so it overlaps the backup
    # selector and route. plan_route creates its own Amap provider later.
    route_planner_trace: dict = {}
    route_source = "fallback"
    route_planner_logged = False
    planner_task: asyncio.Task | None = None
    planner_deadline = time.monotonic()
    if bool(getattr(settings, "route_planner_enabled", False)):
        planner_deadline = min(
            time.monotonic() + float(settings.route_planner_deadline_seconds),
            float(workflow_deadline_monotonic)
            - route_planning._ROUTE_WORKFLOW_RESERVE_SECONDS,
        )
        if planner_deadline <= time.monotonic():
            route_planner_trace.update(
                _route_planner_stage_trace(
                    source="fallback",
                    reason="deadline",
                    result=None,
                )
            )
            _log_route_planner_outcome(
                source="fallback", reason="deadline", result=None
            )
            route_planner_logged = True
        else:
            planner_task = asyncio.create_task(
                _run_route_planner(
                    trip_request,
                    retrieval,
                    settings=settings,
                    lodging_anchor=_planner_user_lodging_anchor(
                        trip_request,
                        accommodation,
                        accommodation_context,
                    ),
                ),
                name="route-planner",
            )

    logger.info("[3/6] POI Selection ...")
    selection_pool = route_planning._route_planning_candidate_pool(
        retrieval,
        must_include_ids=route_planning.valid_must_include_ids(
            trip_request,
            retrieval,
        ),
    )
    selector_input = poi_selection.build_poi_selection_input(
        trip_request,
        selection_pool,
        accommodation=accommodation,
        must_include_place_ids=must_include_place_ids,
    )
    poi_selection_metrics = poi_selection.PoiSelectionMetrics()
    selection_result = None

    async def run_poi_selection():
        nonlocal poi_selection_metrics, selection_result
        execution = await poi_selection.select_pois(
            selector_input,
            workflow_deadline_monotonic=workflow_deadline_monotonic,
            generation_policy_version=generation_policy_version,
        )
        poi_selection_metrics = execution.metrics
        selection_result = execution.result
        return execution.result

    try:
        selection_result = await _run_observed_step(
            "POI_SELECTION",
            run_poi_selection,
            on_stage=on_stage,
            on_stage_event=on_stage_event,
            metadata=poi_selection_metrics.to_stage_metadata(),
            finish_metadata=lambda: poi_selection_metrics.to_stage_metadata(),
        )
    except BaseException:
        if planner_task is not None and not route_planner_logged:
            _log_route_planner_outcome(
                source="cancelled", reason="cancelled", result=None
            )
            route_planner_logged = True
        await _drain_route_task(planner_task)
        raise
    v012_execution_metrics = (
        {
            "generation_policy_version": "v0.12",
            "poi_selection": poi_selection_metrics.to_stage_metadata(),
        }
        if generation_policy_version == "v0.12"
        else {}
    )

    logger.info("[4/6] Route Planning ...")
    route_metrics = route_planning.RoutePlanningMetrics()
    route_started_at = time.monotonic()
    route_diagnostic_summary: dict | None = None
    route_replay_data: dict | None = None

    def observe_route(
        plans: list[RoutePlan], error: BaseException | None = None
    ) -> None:
        nonlocal route_diagnostic_summary, route_replay_data
        if generation_policy_version != "v0.12":
            return
        try:
            summary, replay = route_diagnostics.build_route_observation(
                request=trip_request,
                selector_input=selector_input,
                retrieval=retrieval,
                plans=plans,
                metrics=route_metrics,
                elapsed_ms=max(0, int((time.monotonic() - route_started_at) * 1000)),
                error=error,
                accommodation=accommodation,
            )
            route_diagnostic_summary = summary.model_dump(mode="json")
            route_replay_data = replay or None
        except Exception:
            # Diagnostics are auxiliary.  Their failure cannot rewrite route
            # output, ordering, or the terminal business result.
            logger.warning("route diagnostics projection failed", exc_info=True)
            route_diagnostic_summary = route_diagnostics.RouteDiagnostics(
                evidence_state="UNAVAILABLE",
                stop_reason="ERROR",
                candidate_count=None,
                search=route_diagnostics.RouteSearchObservation(),
                truncated=False,
                replay_available=False,
            ).model_dump(mode="json")
            route_replay_data = None

    async def run_route_planning():
        nonlocal route_source, route_planner_logged

        async def _plan_backup():
            try:
                route_signature = inspect.signature(
                    route_planning.plan_routes
                ).parameters
            except (TypeError, ValueError):
                route_signature = {}
            kwargs = {}
            if "metrics" in route_signature:
                kwargs["metrics"] = route_metrics
            if "selection" in route_signature:
                kwargs["selection"] = selection_result
            elif "target_plan_count" in route_signature:
                # Narrow compatibility for injected historical route doubles. The
                # production planner exposes ``selection`` and never takes this branch.
                kwargs["target_plan_count"] = 1
            if accommodation_context is not None:
                kwargs["accommodation_context"] = accommodation_context
            if "accommodation_source" in route_signature:
                kwargs["accommodation_source"] = accommodation.source if accommodation is not None else "auto_recommended"
            if "accommodation_coord" in route_signature:
                kwargs["accommodation_coord"] = accommodation_coord
            if "workflow_deadline_monotonic" in route_signature:
                kwargs["workflow_deadline_monotonic"] = workflow_deadline_monotonic
            if "selector_input" in route_signature:
                kwargs["selector_input"] = selector_input
            if kwargs:
                return await route_planning.plan_routes(
                    trip_request,
                    retrieval,
                    **kwargs,
                )
            return await route_planning.plan_routes(trip_request, retrieval)

        if planner_task is None:
            return await _plan_backup()

        backup_task = asyncio.create_task(_plan_backup(), name="route-backup")
        try:
            result, reason = await _await_route_planner(
                planner_task,
                planner_deadline,
            )
            if reason == "" and _route_planner_usable(result, trip_request.days):
                if _model_route_budget_ready(result.plan, trip_request):
                    route_source = "model"
                    route_planner_trace.clear()
                    route_planner_trace.update(
                        _route_planner_stage_trace(
                            source="model",
                            reason="",
                            result=result,
                        )
                    )
                    _log_route_planner_outcome(
                        source="model", reason="", result=result
                    )
                    route_planner_logged = True
                    return [result.plan]
                reason = "budget_mismatch"
            if reason == "":
                reason = _route_planner_fallback_reason(result)
            route_source = "fallback"
            route_planner_trace.clear()
            route_planner_trace.update(
                _route_planner_stage_trace(
                    source="fallback",
                    reason=reason,
                    result=result,
                )
            )
            _log_route_planner_outcome(
                source="fallback", reason=reason, result=result
            )
            route_planner_logged = True
            return await backup_task
        finally:
            if planner_task is not None and not route_planner_logged:
                _log_route_planner_outcome(
                    source="cancelled", reason="cancelled", result=None
                )
                route_planner_logged = True
            await _drain_route_task(backup_task)
            await _drain_route_task(planner_task)

    try:
        route_plans = await _run_observed_step(
            "ROUTE_PLANNING",
            run_route_planning,
            on_stage=on_stage,
            on_stage_event=on_stage_event,
            metadata=route_metrics.to_dict(),
            finish_metadata=lambda: (
                route_metrics.to_dict()
                | route_metrics.route_quality_stage_metadata()
                | route_planner_trace
            ),
        )
        usable_route_plans = [
            plan for plan in route_plans
            if plan.day_groups
        ]
        if not usable_route_plans:
            logger.warning(
                "Route Planning produced no usable day groups; refusing "
                "unconstrained generation"
            )
            observe_route(route_plans)
            return WorkflowResult(
                trip_request=trip_request,
                plans=[],
                result_type="NO_USABLE_ROUTE",
                review_notes=NO_USABLE_ROUTE_NOTICE,
                quality_metrics=v012_execution_metrics,
                route_plans=route_plans,
                record_id=None,
                route_diagnostics=route_diagnostic_summary,
                route_replay_data=route_replay_data,
            )
        if len(usable_route_plans) != 1 or len(route_plans) != 1:
            raise RuntimeError("POI selection must produce exactly one usable route")
        route_plans = usable_route_plans
        complete_day_route_plans = [
            plan for plan in route_plans
            if len(plan.day_groups) == trip_request.days
        ]
        if not complete_day_route_plans:
            logger.warning(
                "Route Planning produced no complete %d-day plan; refusing "
                "shortened itinerary publication",
                trip_request.days,
            )
            observe_route(route_plans)
            return WorkflowResult(
                trip_request=trip_request,
                plans=[],
                result_type="NO_USABLE_ROUTE",
                review_notes=NO_USABLE_ROUTE_NOTICE,
                quality_metrics=v012_execution_metrics,
                route_plans=route_plans,
                record_id=None,
                route_diagnostics=route_diagnostic_summary,
                route_replay_data=route_replay_data,
            )
        route_plans = complete_day_route_plans
        observe_route(route_plans)
        route_metrics.plan_count = 1
        if route_source == "model":
            locked = route_plans[0]
            anchor = locked.accommodation_anchor
            if anchor is not None and anchor.suggestion is not None:
                accommodation = anchor.suggestion
                accommodation_coord = (accommodation.latitude, accommodation.longitude)
            if (
                accommodation_context is not None
                and accommodation_context.state == "user_unresolved"
                and getattr(accommodation, "source", None) != "user_specified"
                and ACCOMMODATION_FALLBACK_NOTICE not in route_notices
            ):
                route_notices.append(ACCOMMODATION_FALLBACK_NOTICE)
        elif coupling_enabled:
            locked = route_plans[0]
            accommodation = locked.accommodation_anchor.suggestion if locked.accommodation_anchor else None
            accommodation_coord = (accommodation.latitude, accommodation.longitude) if accommodation else None
    except (
        route_planning.RoutePlanInvariantError,
        route_planning.RouteMustIncludeConflictError,
        route_planning.SelectedRoutePreciseConflictError,
    ) as exc:
        logger.warning("Route Planning found no usable route", exc_info=True)
        observe_route([], exc)
        return WorkflowResult(
            trip_request=trip_request,
            plans=[],
            result_type="NO_USABLE_ROUTE",
            review_notes=(ACCOMMODATION_UNRESOLVED_NOTICE if accommodation_context is not None
                and accommodation_context.state == "user_unresolved" else ("全天活动需要独立一天，与其他必去地点无法在当前天数或时间窗内同时安排；请增加天数或调整时间窗。"
                    if any(code in {"full_day_conflict", "window_conflict"} for code in getattr(exc,"violations",()))
                    or any(code in {"full_day_conflict", "window_conflict"} for day in getattr(exc,"day_groups",())
                           if day.day_feasibility for code in day.day_feasibility.violations)
                    else NO_USABLE_ROUTE_NOTICE)),
            quality_metrics=v012_execution_metrics | (
                {**({"accommodation_failure_code": "ACCOMMODATION_UNRESOLVED"
                    if accommodation_context.state == "user_unresolved" else "ROUTE_SEARCH_EXHAUSTED"}
                    if accommodation_context is not None else {}),
                 "route_planning": {
                    **route_metrics.to_dict(),
                    "failure_codes": list(getattr(exc, "violations", ()))
                    or [getattr(exc, "reason", "route_infeasible")],
                }} if settings.selector_route_v2_enabled else {}
            ),
            route_plans=[],
            record_id=None,
            route_diagnostics=route_diagnostic_summary,
            route_replay_data=route_replay_data,
        )
    except Exception:
        logger.exception("Route Planning failed")
        raise
    finally:
        await _drain_route_task(planner_task)

    if lodging_price_handles is None:
        lodging_price_handles = []
    lodging_price_handle = start_lodging_price_for_trip(
        trip_request=trip_request,
        accommodation=accommodation,
        settings=settings,
    )
    if lodging_price_handle is not None:
        lodging_price_handles.append(lodging_price_handle)

    pre_route_selection_quality = route_metrics.route_quality_metrics()
    post_budget_route_quality: dict[str, Any] = {}
    poi_identity_results = []
    budget_results = []
    composition_blueprints = []
    meal_attachment_map = {}
    meal_recovery_metrics = {}
    attachment_auth_map = {}
    amap_handle: AmapPoiDetailTaskHandle | None = None
    if amap_handles is None:
        amap_handles = []
    if route_plans:
        try:
            budget_signature = inspect.signature(
                itinerary_budget.resolve_route_budgets
            ).parameters
        except (TypeError, ValueError):
            budget_signature = {}
        budget_kwargs = {}
        if "accommodation_coord" in budget_signature:
            budget_kwargs["accommodation_coord"] = accommodation_coord
        budget_results = itinerary_budget.resolve_route_budgets(
            route_plans,
            trip_request,
            **budget_kwargs,
        )
        if any(plan.route_policy_version == "selector-route-v2" for plan in route_plans):
            if len(budget_results) != len(route_plans) or any(
                len(result.days) != trip_request.days or any(day.status == "infeasible" for day in result.days)
                for result in budget_results
            ):
                return WorkflowResult(trip_request=trip_request, plans=[], result_type="NO_USABLE_ROUTE",
                    review_notes=NO_USABLE_ROUTE_NOTICE,
                    quality_metrics=v012_execution_metrics,
                    route_plans=route_plans, record_id=None,
                    route_diagnostics=route_diagnostic_summary,
                    route_replay_data=route_replay_data)
        resolved_pairs = [
            (route_plan, budget_result)
            for route_plan, budget_result in zip(route_plans, budget_results)
            if route_plan.day_groups
        ]
        route_plans = [route_plan for route_plan, _ in resolved_pairs]
        budget_results = [
            budget_result.model_copy(
                update={
                    "days": [
                        day
                        for day in budget_result.days
                        if day.status != "infeasible"
                    ]
                }
            )
            for _, budget_result in resolved_pairs
        ]
        complete_budget_route_pairs = [
            (route_plan, budget_result)
            for route_plan, budget_result in zip(route_plans, budget_results)
            if len(route_plan.day_groups) == trip_request.days
        ]
        if not complete_budget_route_pairs:
            logger.warning(
                "Budget resolution produced no complete %d-day plan; refusing "
                "shortened itinerary publication",
                trip_request.days,
            )
            return WorkflowResult(
                trip_request=trip_request,
                plans=[],
                result_type="NO_USABLE_ROUTE",
                review_notes=NO_USABLE_ROUTE_NOTICE,
                quality_metrics=v012_execution_metrics,
                route_plans=route_plans,
                record_id=None,
                route_diagnostics=route_diagnostic_summary,
                route_replay_data=route_replay_data,
            )
        route_plans = [route_plan for route_plan, _ in complete_budget_route_pairs]
        budget_results = [
            budget_result for _, budget_result in complete_budget_route_pairs
        ]
        if not route_plans:
            return WorkflowResult(
                trip_request=trip_request,
                plans=[],
                result_type="NO_USABLE_ROUTE",
                review_notes=NO_USABLE_ROUTE_NOTICE,
                quality_metrics=v012_execution_metrics,
                route_plans=[],
                record_id=None,
                route_diagnostics=route_diagnostic_summary,
                route_replay_data=route_replay_data,
            )
        if len(route_plans) != 1:
            raise RuntimeError("budget resolution must preserve one locked route")
        if route_plans[0].membership_ledger is not None:
            route_metrics.record_membership_ledger(
                route_planning.refresh_selection_membership_ledger(
                    route_plans[0],
                    selection=selection_result,
                    qualified_pool=selection_pool,
                    request=trip_request,
                )
            )
        (
            amap_handle,
            poi_identity_results,
            composition_blueprints,
            attachment_auth_map,
            meal_attachment_map,
            meal_recovery_metrics,
            post_budget_route_quality,
        ) = await _enrich_after_budget_lock(
            trip_request=trip_request,
            retrieval=retrieval,
            route_plans=route_plans,
            must_include_resolution=must_include_resolution,
            pre_route_selection_quality=pre_route_selection_quality,
        )
        amap_handles.append(amap_handle)

    logger.info("[5/6] Final Writer ...")
    logger.info("[6/6] YunTu Review ...")
    weather_advisory_payload = await build_weather_advisory_payload(
        trip_request=trip_request,
        user_text=user_text,
    )
    (
        _amap_detail_result,
        structured_evidence_payload,
        pretrip_advice_payloads,
        amap_advice_metrics,
    ) = await await_writer_barrier_and_build_pretrip_advice(
        amap_handle,
        trip_request=trip_request,
        retrieval=retrieval,
        route_plans=route_plans,
        composition_blueprints=composition_blueprints,
        weather_advisory_payload=weather_advisory_payload,
    )
    publish_retry_count = 0
    full_writer_generation_count = 1
    fragment_repair_call_count = 0
    generation_metrics: dict = {}
    review_notes = ""
    plans: list[PlanOutput] = []
    publish_result = None
    publish_retry_feedback: list[dict] = []
    review_call_budget = (
        SharedCallBudget(
            deadline_monotonic=workflow_deadline_monotonic,
            max_calls=5,
            reserve_seconds=10.0,
        )
        if generation_policy_version == "v0.12"
        else None
    )

    while True:
        pipeline = WriteReviewPublishPipeline(
            trip_request=trip_request,
            retrieval=retrieval,
            route_plans=route_plans,
            poi_identity_results=poi_identity_results,
            budget_results=budget_results,
            composition_blueprints=composition_blueprints,
            accommodation=accommodation,
            transport=transport,
            attachment_auth_map=attachment_auth_map,
            weather_advisory_payload=weather_advisory_payload,
            structured_evidence_payload=structured_evidence_payload,
            pretrip_advice_payloads=pretrip_advice_payloads,
            amap_poi_detail_metrics=amap_advice_metrics,
            on_stage=on_stage,
            on_stage_event=on_stage_event,
            on_writer_output=on_writer_output,
            attempt=1 + publish_retry_count,
            publish_retry_round=publish_retry_count,
            fragment_repair_call_count=fragment_repair_call_count,
            publish_retry_feedback=publish_retry_feedback,
            workflow_deadline_monotonic=workflow_deadline_monotonic,
            residual_reserve_seconds=20.0,
            speculative_initial_generation=(publish_retry_count == 0),
            require_selected_route_lock=not (
                route_source == "model"
                and all(plan.membership_ledger is None for plan in route_plans)
            ),
            generation_policy_version=generation_policy_version,
            review_call_budget=review_call_budget,
        )
        try:
            with lodging_writer_context(
                enabled=bool(getattr(settings, "lodging_section_v2_enabled", False)),
                handle=lodging_price_handle,
                mid_markup=float(getattr(settings, "rollinggo_mid_markup", 1.05)),
            ):
                plans, review_notes, publish_result, generation_metrics = await pipeline.run()
            fragment_repair_call_count = int(
                getattr(pipeline, "fragment_repair_call_count", fragment_repair_call_count)
                or fragment_repair_call_count
            )
            break
        except ContentDispatchSignal as signal:
            generation_metrics = dict(signal.metrics or {})
            review_notes = signal.review_notes or review_notes
            plans = signal.plans or plans
            fragment_repair_call_count = max(
                fragment_repair_call_count,
                int(generation_metrics.get("fragment_repair_call_count") or 0),
            )
            async def validate_outer_dispatch() -> DispatchDecision:
                decision = signal.decision
                if (
                    decision.action == "PUBLISH_RETRY"
                    and not _publish_retry_is_body_incomplete(decision)
                ):
                    decision = DispatchDecision(
                        action="FAIL_CLOSED",
                        reasons=decision.reasons,
                        plan_indexes=decision.plan_indexes,
                        fragment_issue_codes=decision.fragment_issue_codes,
                        notes=[
                            *decision.notes,
                            "outer retry guard rejected non-incomplete reason",
                        ],
                        skip_review=decision.skip_review,
                    )
                    signal.decision = decision
                if (
                    decision.action == "PUBLISH_RETRY"
                    and publish_retry_count < 1
                ):
                    return decision

                # Fail closed after retry budget or non-retry dispatch.
                raise _build_content_dispatch_failure(
                    signal,
                    generation_metrics,
                ) from signal

            decision = await _run_observed_step(
                "CONTENT_VALIDATION",
                validate_outer_dispatch,
                on_stage_event=on_stage_event,
                set_current_stage=False,
                attempt=1 + publish_retry_count,
                publish_retry_round=publish_retry_count,
                metadata={"validation_boundary": "outer_retry_control"},
            )
            next_publish_retry_round = publish_retry_count + 1

            async def admit_publish_retry() -> list[dict]:
                return _build_publish_retry_feedback(
                    signal=signal,
                    pipeline=pipeline,
                    trip_request=trip_request,
                    retrieval=retrieval,
                    route_plans=route_plans,
                )

            publish_retry_feedback = await _run_observed_step(
                "PUBLISH_RETRY",
                admit_publish_retry,
                on_stage_event=on_stage_event,
                set_current_stage=False,
                attempt=1 + next_publish_retry_round,
                publish_retry_round=next_publish_retry_round,
                metadata={
                    "reason_count": len(decision.reasons),
                    "next_publish_retry_round": next_publish_retry_round,
                },
            )
            publish_retry_count = next_publish_retry_round
            full_writer_generation_count += 1
            logger.info(
                "Dispatch PUBLISH_RETRY on same lock (round=%s reasons=%s)",
                publish_retry_count,
                ",".join(decision.reasons[:5]),
            )
            continue

        except Exception as error:
            pipeline_metrics = pipeline.metrics.to_dict()
            _attach_pipeline_recovery_facts(error, pipeline_metrics)
            raise

    route_plans = pipeline.route_plans
    if coupling_enabled:
        for output in plans:
            output.accommodation = accommodation.model_copy(deep=True) if accommodation else None
    poi_identity_results = pipeline.poi_identity_results or []
    budget_results = pipeline.budget_results or []
    composition_blueprints = pipeline.composition_blueprints or []
    generation_metrics = dict(generation_metrics or {})
    generation_metrics.update(amap_advice_metrics)
    generation_metrics["publish_retry_count"] = publish_retry_count
    generation_metrics["full_writer_generation_count"] = full_writer_generation_count
    generation_metrics["fragment_repair_call_count"] = fragment_repair_call_count
    generation_metrics["content_dispatch_used"] = True
    generation_metrics["whole_plan_repair_bypassed"] = True
    generation_metrics.setdefault("full_candidate_review_skip_disabled", False)
    generation_metrics.setdefault("review_required_for_complete_body", False)
    generation_metrics.setdefault("review_ran", bool(generation_metrics.get("review_ran")))
    generation_metrics["workflow_deadline_seconds"] = 180
    if generation_policy_version == "v0.12":
        generation_metrics["generation_policy_version"] = "v0.12"
    if publish_result is None:
        raise RuntimeError("pipeline completed without publish_result")
    if route_plans:
        post_budget_route_quality = _build_post_budget_route_quality(
            trip_request=trip_request,
            retrieval=retrieval,
            route_plans=route_plans,
            pre_route_selection_quality=pre_route_selection_quality,
            must_include_resolution=must_include_resolution,
        )
    if not _is_complete_plan_result(
        plans,
        route_plans=route_plans,
        expected_days=trip_request.days,
    ):
        logger.warning(
            "Final plan day count does not match request days=%d; refusing "
            "shortened itinerary publication",
            trip_request.days,
        )
        return WorkflowResult(
            trip_request=trip_request,
            plans=[],
            result_type="NO_USABLE_ROUTE",
            review_notes=NO_USABLE_ROUTE_NOTICE,
            quality_metrics=v012_execution_metrics,
            route_plans=route_plans,
            record_id=None,
            route_diagnostics=route_diagnostic_summary,
            route_replay_data=route_replay_data,
        )
    if route_notices:
        review_notes = "\n".join([*route_notices, review_notes]).strip()
    logger.info("Review done. Notes: %s", review_notes[:200])
    must_include_report = _build_must_include_report(
        must_include_resolution,
        route_plans,
        trip_request,
    )
    must_include_metrics = (
        {"must_include_report": must_include_report}
        if must_include_report
        else {}
    )
    route_quality_metrics: dict[str, Any] = {
        "route_quality": post_budget_route_quality or pre_route_selection_quality,
    }
    if pre_route_selection_quality:
        route_quality_metrics["pre_route_selection_quality"] = (
            pre_route_selection_quality
        )
    if post_budget_route_quality:
        route_quality_metrics["post_budget_route_quality"] = (
            post_budget_route_quality
        )

    delivery_metadata = DeliveryMetadata.from_metrics(generation_metrics)
    result = WorkflowResult(
        trip_request=trip_request,
        plans=plans,
        review_notes=review_notes,
        route_plans=route_plans,
        route_diagnostics=route_diagnostic_summary,
        route_replay_data=route_replay_data,
        delivery_metadata=delivery_metadata,
        quality_metrics=build_quality_metrics(
            user_text=user_text,
            plans=plans,
            retrieval=retrieval,
            recent_place_id_sets=recent_place_id_sets,
        ) | {
            **generation_metrics,
            **publish_result.to_metrics(),
            "publish_retry_count": publish_retry_count,
            "route_planning": route_metrics.to_dict() | (
                dict(route_planner_trace) if route_planner_trace else {}
            ),
            **route_quality_metrics,
            "poi_selection": poi_selection_metrics.to_dict(),
            **llm.summarize_llm_call_records(llm_records or []),
            **meal_recovery_metrics,
            **weather_advisory_payload.metrics_summary(),
            **must_include_metrics,
            **_time_preferences_metrics(trip_request),
            **delivery_metadata.model_dump(exclude_none=True),
        },
        weather_display=weather_advisory_payload.display_payload(),
    )
    if lodging_price_handle is not None:
        result.lodging_price_result = await await_lodging_price_at_writer_barrier(
            lodging_price_handle
        )
    attach_lodging_displays(result, trip_request, settings)
    result.cost_estimate_snapshots = list(
        await resolve_workflow_cost_snapshots(
            result,
            meal_attachments=meal_attachment_map,
            intercity_prefetch=cost_intercity_prefetch,
        )
    )

    def persisting_finish_metadata() -> dict[str, int]:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        return summarize_stage_timing_observation(
            workflow_elapsed_ms=elapsed_ms,
        )

    record_id = await _run_observed_step(
        "PERSISTING",
        lambda: _save_plan_record(user_text, result),
        on_stage=on_stage,
        on_stage_event=on_stage_event,
        finish_metadata=persisting_finish_metadata,
    )
    result.record_id = record_id

    elapsed = time.monotonic() - t0
    logger.info("Workflow complete in %.1fs", elapsed)
    return result


def attach_lodging_displays(
    result: WorkflowResult,
    trip_request: TripRequest,
    settings: object,
) -> None:
    """Attach lodging display after the writer barrier, one copy per plan."""

    if not bool(getattr(settings, "lodging_section_v2_enabled", False)):
        return
    try:
        mid_markup = float(getattr(settings, "rollinggo_mid_markup", 1.05))
    except (TypeError, ValueError):
        mid_markup = 1.05
    prices = result.lodging_price_result
    for index, plan in enumerate(result.plans):
        accommodation = plan.accommodation
        if accommodation is None:
            continue
        display = None
        try:
            route_plan = result.route_plans[index]
            display = build_lodging_display(
                accommodation=accommodation,
                route_plan=route_plan,
                trip_request=trip_request,
                prices=prices,
                nearby_hotel_names=route_plan.nearby_lodging_hotel_names,
                mid_markup=mid_markup,
            )
        except Exception:
            logger.warning(
                "lodging display build failed; continuing without display"
            )
            display = None
        plan.accommodation = accommodation.model_copy(update={"display": display})
        if display is None:
            continue
        section = plan.code_lodging_section or ""
        if not section:
            continue
        try:
            rendered = render_lodging_display_text(plan.accommodation, display) + "\n\n"
        except Exception:
            logger.warning("lodging display text render failed during consistency check")
            continue
        if rendered != section:
            logger.warning("lodging display text does not match code lodging section")


def _must_include_anchor_active() -> bool:
    settings = get_settings()
    return bool(
        getattr(settings, "place_demand_enabled", False)
        and getattr(settings, "must_include_anchor_enabled", False)
    )


def _workflow_must_include_place_ids(
    trip_request: TripRequest,
    resolution: MustIncludeResolution | None,
) -> list[int]:
    """One must-go set for Retrieval, Selector and Route.

    Without an active anchor resolution the Selector already treats the
    request's place ids as must-go. Retrieval has to pin the same ids, or the
    route sees them as ordinary picks it may trade away.
    """
    if resolution is not None:
        return list(resolution.matched_place_ids)
    return [
        item.place_id
        for item in trip_request.must_include
        if item.place_id is not None
    ]


def _time_preferences_metrics(trip_request: TripRequest) -> dict:
    if not bool(getattr(get_settings(), "time_preferences_enabled", False)):
        return {}
    payload = trip_request.time_preferences_payload()
    return {"time_preferences": payload} if payload else {}


def _commute_mode_metrics(
    trip_request: TripRequest,
    route_plans: list[RoutePlan],
    route_metrics,
) -> dict:
    """Build persisted commute truth from final published route legs."""
    settings = get_settings()
    effective, effective_reason = generation_base_mode(trip_request, settings)
    legs = [
        leg
        for route_plan in route_plans
        for day_group in route_plan.day_groups
        for leg in day_group.commute_legs
    ]
    degraded_leg_count = sum(
        1 for leg in legs if str(leg.source or "estimate") != "amap"
    )
    rationalized_leg_count = sum(1 for leg in legs if leg.mode != effective)
    if not legs or degraded_leg_count == 0:
        duration_source = "amap"
    elif degraded_leg_count == len(legs):
        duration_source = "estimated"
    else:
        duration_source = "mixed"

    payload: dict = {"commute_mode_request": trip_request.commute_mode}
    transit_legs = [leg for leg in legs if leg.mode == "transit"]
    transit_detail = {
        "complete_count": 0,
        "partial_count": 0,
        "missing_count": 0,
        "cache_hit_count": 0,
    }
    for leg in transit_legs:
        quality = str(leg.transit_detail_quality or "missing")
        if quality not in {"complete", "partial", "missing"}:
            quality = "missing"
        transit_detail[f"{quality}_count"] += 1
        if bool(leg.transit_detail_cache_hit):
            transit_detail["cache_hit_count"] += 1
    payload["transit_detail"] = transit_detail
    if (
        trip_request.commute_mode != "driving"
        or rationalized_leg_count > 0
        or degraded_leg_count > 0
    ):
        payload["commute_mode_report"] = {
            "requested": trip_request.commute_mode,
            "effective": effective,
            "effective_reason": effective_reason,
            "degraded_leg_count": degraded_leg_count,
            "rationalized_leg_count": rationalized_leg_count,
            "duration_source": duration_source,
        }

    diagnostic = (
        route_metrics.to_dict()
        if hasattr(route_metrics, "to_dict")
        else route_metrics
    )
    if isinstance(diagnostic, dict) and isinstance(
        diagnostic.get("by_effective_mode"),
        dict,
    ):
        payload["commute_mode_provider_diagnostics"] = {
            "by_effective_mode": diagnostic["by_effective_mode"],
        }
    return payload


def _route_plan_place_ids(route_plans: list[RoutePlan]) -> set[int]:
    return {
        int(place.place_id)
        for route_plan in route_plans
        for day_group in route_plan.day_groups
        for place in day_group.places
    }


def _matched_must_include_place_ids(
    resolution: MustIncludeResolution | None,
) -> set[int]:
    if resolution is None:
        return set()
    return {
        int(item.place_id)
        for item in resolution.report_items
        if item.match_status == "matched" and item.place_id is not None
    }


def _route_plan_must_include_counts(
    resolution: MustIncludeResolution | None,
    route_plan: RoutePlan,
) -> dict[str, Any] | None:
    matched_place_ids = _matched_must_include_place_ids(resolution)
    if not matched_place_ids:
        return None
    scheduled_place_ids = _route_plan_place_ids([route_plan])
    scheduled = len(matched_place_ids & scheduled_place_ids)
    total = len(matched_place_ids)
    return {
        "must_include_total": total,
        "must_include_scheduled": scheduled,
        "must_include_coverage_ratio": (
            round(scheduled / total, 4)
            if total
            else 0.0
        ),
    }


def _align_route_quality_must_include_counts(
    route_quality: dict[str, Any],
    route_plans: list[RoutePlan],
    resolution: MustIncludeResolution | None,
) -> dict[str, Any]:
    if not route_quality or not _matched_must_include_place_ids(resolution):
        return route_quality
    aligned = dict(route_quality)
    raw_candidates = aligned.get("route_quality_candidates")
    if not isinstance(raw_candidates, list):
        return aligned

    candidates: list[dict[str, Any]] = []
    for index, raw_candidate in enumerate(raw_candidates):
        candidate = (
            dict(raw_candidate)
            if isinstance(raw_candidate, dict)
            else {"label": str(raw_candidate)}
        )
        if index < len(route_plans):
            counts = _route_plan_must_include_counts(
                resolution,
                route_plans[index],
            )
            if counts is not None:
                candidate.update(counts)
        candidates.append(candidate)
    aligned["route_quality_candidates"] = candidates

    selected_label = str(aligned.get("selected_label") or "")
    selected = next(
        (
            candidate for candidate in candidates
            if str(candidate.get("label") or "") == selected_label
        ),
        candidates[0] if candidates else None,
    )
    if selected is not None:
        aligned["must_include_total"] = int(
            selected.get("must_include_total", 0)
        )
        aligned["selected_must_include_scheduled"] = int(
            selected.get("must_include_scheduled", 0)
        )
        aligned["selected_must_include_coverage_ratio"] = float(
            selected.get("must_include_coverage_ratio", 0.0)
        )
    return aligned


def _build_post_budget_route_quality(
    *,
    trip_request: TripRequest,
    retrieval: RetrievalResult,
    route_plans: list[RoutePlan],
    pre_route_selection_quality: dict[str, Any],
    must_include_resolution: MustIncludeResolution | None,
) -> dict[str, Any]:
    if not route_plans:
        return {}
    selected_reason = str(
        pre_route_selection_quality.get("selected_reason") or "legacy_order"
    )
    route_quality = route_planning.build_route_quality_metrics(
        trip_request,
        retrieval,
        route_plans,
        selected_label=route_plans[0].label,
        previous_selected_label=str(
            pre_route_selection_quality.get("selected_label")
            or route_plans[0].label
        ),
        selected_reason=selected_reason,
    )
    return _align_route_quality_must_include_counts(
        route_quality,
        route_plans,
        must_include_resolution,
    )


def _build_must_include_report(
    resolution: MustIncludeResolution | None,
    route_plans: list[RoutePlan],
    trip_request: TripRequest | None = None,
) -> list[dict]:
    if resolution is None or not resolution.report_items:
        return []

    scheduled_place_ids = _route_plan_place_ids(route_plans)
    report = []
    for item in resolution.report_items:
        entry = {"name": item.input_name}
        if item.matched_city:
            entry["matched_city"] = item.matched_city
        if item.avoid_conflict or _must_include_avoid_conflict(item.normalized_name, trip_request):
            entry["avoid_conflict"] = True

        if item.match_status == "matched":
            if item.place_id is not None:
                entry["place_id"] = item.place_id
            if item.place_id is not None and item.place_id in scheduled_place_ids:
                entry["status"] = "scheduled"
                if item.reason:
                    entry["reason"] = item.reason
            else:
                entry["status"] = "not_scheduled"
                entry["reason"] = "同日路线与时长预算未能容纳"
        elif item.match_status == "candidate":
            entry["status"] = "recorded_candidate"
            if item.reason:
                entry["reason"] = item.reason
        elif item.match_status == "unmatched":
            entry["status"] = "recorded_unmatched"
            if item.reason:
                entry["reason"] = item.reason
        elif item.match_status == "cross_city":
            entry["status"] = "cross_city"
            if item.reason:
                entry["reason"] = item.reason
        else:
            continue
        report.append(entry)
    return report


def _must_include_avoid_conflict(
    normalized_name: str,
    trip_request: TripRequest | None,
) -> bool:
    if trip_request is None or not normalized_name:
        return False
    for avoid_item in trip_request.avoid:
        if poi_alias.normalize_place_name(avoid_item) == normalized_name:
            return True
    return False


def _preserve_explicit_pace_text(user_text: str, request: TripRequest) -> TripRequest:
    if not user_text.strip() or detect_pace_mode(request) != "default":
        return request
    markers = (*RELAXED_PACE_MARKERS, *COMPACT_PACE_MARKERS)
    if not any(marker in user_text for marker in markers):
        return request
    notes = " ".join(part for part in (request.notes, user_text) if part).strip()
    return request.model_copy(update={"notes": notes})


def _is_complete_plan_result(
    plans: list[PlanOutput],
    *,
    route_plans: list[RoutePlan],
    expected_days: int,
) -> bool:
    if expected_days <= 0 or not plans:
        return False
    if route_plans and len(plans) != len(route_plans):
        return False
    for index, plan in enumerate(plans):
        if len(plan.day_place_names or []) != expected_days:
            return False
        if route_plans:
            if index >= len(route_plans):
                return False
            route_plan = route_plans[index]
            if len(route_plan.day_groups or []) != expected_days:
                return False
    return True


def _assert_persistable_result(result: WorkflowResult) -> None:
    if not _is_complete_plan_result(
        result.plans,
        route_plans=result.route_plans,
        expected_days=result.trip_request.days,
    ):
        raise ValueError(
            "result day count does not match request.days; refusing persistence"
        )


async def _save_plan_record(user_text: str, result: WorkflowResult) -> int:
    """Insert one row into travel_plan_record and return its id."""
    _assert_persistable_result(result)
    req = result.trip_request
    settings = get_settings()
    weather_display = await _weather_display_for_persistence(user_text, result)
    if isinstance(weather_display, dict) and weather_display:
        display_days = weather_display.get("days") or []
        if len(display_days) > req.days:
            weather_display = {
                **weather_display,
                "days": display_days[: req.days],
            }
    quality_metrics = dict(result.quality_metrics or {})
    quality_metrics.update(
        result.delivery_metadata.model_dump(exclude_none=True)
    )
    quality_metrics.update(_commute_mode_metrics(
        req,
        result.route_plans,
        quality_metrics.get("route_planning"),
    ))
    if isinstance(weather_display, dict) and weather_display:
        display_days = weather_display.get("days") or []
        weather_status = str(weather_display.get("status") or "").strip()
        quality_metrics.update({
            "weather_status": weather_status,
            "weather_days_requested": req.days,
            "weather_days_authorized": len(display_days) if weather_status == "ok" else 0,
            "weather_days_out_of_range": (
                max(req.days - len(display_days), 0)
                if weather_status == "ok"
                else quality_metrics.get("weather_days_out_of_range", 0)
            ),
        })

    snapshots = list(result.cost_estimate_snapshots)
    if not snapshots:
        raise ValueError("cost snapshots must be resolved before persistence")
    if len(snapshots) != len(result.plans):
        raise ValueError("every persisted plan requires exactly one cost snapshot")

    all_place_ids: list[int] = []
    generated_plans: list[dict] = []
    for index, p in enumerate(result.plans):
        snapshot = snapshots[index]
        if (
            snapshot.plan_identity.plan_index != index
            or snapshot.plan_identity.plan_key != p.plan_name
        ):
            raise ValueError("cost snapshot plan identity does not match persisted plan")
        all_place_ids.extend(p.used_place_ids)
        generated_plans.append({
            "plan_name": p.plan_name,
            "plan_text": p.plan_text,
            "summary": p.summary,
            "used_place_names": p.used_place_names,
            "day_place_names": p.day_place_names,
            "accommodation": (
                p.accommodation.model_dump(mode="json")
                if p.accommodation
                else None
            ),
            "transport": (
                p.transport.model_dump(mode="json")
                if p.transport
                else None
            ),
            "cost_estimate_snapshot": snapshot.model_dump(mode="json"),
        })
        # v0.9.9.6: persist only non-empty sanitized advice; empty/None fields
        # are omitted. The PlanOutput fields already carry post-sanitizer public
        # shapes (no evidence_ref / source metadata), so model dumps are clean.
        if p.packing_checklist:
            generated_plans[-1]["packing_checklist"] = [
                group.model_dump(mode="json") for group in p.packing_checklist
            ]
        if p.travel_tips:
            generated_plans[-1]["travel_tips"] = [
                tip.model_dump(mode="json") for tip in p.travel_tips
            ]
        if weather_display:
            generated_plans[-1]["weather_display"] = weather_display
        if p.composition_blueprint is not None:
            generated_plans[-1]["composition_blueprint"] = (
                p.composition_blueprint.model_dump(mode="json")
            )
        if p.poi_identity_result is not None:
            generated_plans[-1]["poi_identity_result"] = (
                p.poi_identity_result.model_dump(mode="json")
            )
        if p.budget_result is not None:
            generated_plans[-1]["budget_result"] = (
                p.budget_result.model_dump(mode="json")
            )
    for index, generated_plan in enumerate(generated_plans):
        if index < len(result.route_plans):
            generated_plan["route_plan"] = result.route_plans[index].model_dump(
                mode="json"
            )

    generated_plan_text = "\n\n---\n\n".join(
        f"## {p.plan_name}\n\n{p.plan_text}" for p in result.plans
    )
    if settings.writer_model == settings.review_model:
        model_label = settings.writer_model
    else:
        model_label = f"w:{settings.writer_model[:20]}|r:{settings.review_model[:20]}"

    params = {
        "user_query": user_text,
        "from_city": req.from_city,
        "to_city": req.to_city,
        "start_date": _date_or_none(req.start_date),
        "end_date": _date_or_none(req.end_date),
        "days": req.days,
        "nights": max(req.days - 1, 1),
        "people_count": req.people_count,
        "preferences": json.dumps(req.preferences, ensure_ascii=False),
        "avoid": json.dumps(req.avoid, ensure_ascii=False),
        "notes": req.notes,
        "used_content_ids": "[]",
        "used_place_ids": json.dumps(sorted(set(all_place_ids))),
        "used_fact_ids": "[]",
        "generated_plan": generated_plan_text,
        "plan_json": json.dumps(generated_plans, ensure_ascii=False),
        "model_name": model_label[:50],
        "quality_feedback": result.review_notes,
        "quality_metrics": json.dumps(quality_metrics, ensure_ascii=False),
        "accommodation_name": (
            result.plans[0].accommodation.name
            if result.plans[0].accommodation
            else None
        ),
        "accommodation_lat": (
            result.plans[0].accommodation.latitude
            if result.plans[0].accommodation
            else None
        ),
        "accommodation_lng": (
            result.plans[0].accommodation.longitude
            if result.plans[0].accommodation
            else None
        ),
        "accommodation_source": (
            result.plans[0].accommodation.source
            if result.plans[0].accommodation
            else None
        ),
    }

    factory = get_session_factory()
    async with factory() as session:
        row = await session.execute(
            text("""
                INSERT INTO travel_plan_record
                    (user_query, from_city, to_city, start_date, end_date,
                     days, nights, people_count,
                     preferences, avoid, notes,
                     used_content_ids, used_place_ids, used_fact_ids,
                     generated_plan, plan_json,
                     model_name, quality_feedback, quality_metrics,
                     accommodation_name, accommodation_lat,
                     accommodation_lng, accommodation_source)
                VALUES
                    (:user_query, :from_city, :to_city, :start_date, :end_date,
                     :days, :nights, :people_count,
                     :preferences, :avoid, :notes,
                     :used_content_ids, :used_place_ids, :used_fact_ids,
                     :generated_plan, :plan_json,
                     :model_name, :quality_feedback, CAST(:quality_metrics AS jsonb),
                     :accommodation_name, :accommodation_lat,
                     :accommodation_lng, :accommodation_source)
                RETURNING id
            """),
            params,
        )
        record_id = int(row.scalar_one())
        await session.commit()
    logger.info("travel_plan_record saved id=%s", record_id)
    return record_id


async def _weather_display_for_persistence(
    user_text: str,
    result: WorkflowResult,
) -> dict:
    """Return sanitized weather display data for plan_json persistence.

    The weather advisory metrics and display payload are built from the same
    deterministic payload. If an older in-flight result has metrics but missed
    the display field, rebuild once before persisting so /trip/results does not
    silently lose successful weather.
    """
    metrics = result.quality_metrics or {}
    weather_status = str(metrics.get("weather_status") or "").strip()
    display = result.weather_display if isinstance(result.weather_display, dict) else {}
    if display:
        display_status = str(display.get("status") or "").strip()
        display_days = display.get("days") or []
        expected_days = int(metrics.get("weather_days_authorized") or 0)
        requested_days = max(1, int(result.trip_request.days or 1))
        if (
            display_status != "ok"
            or expected_days <= 0
            or len(display_days) >= expected_days
        ):
            return display
        if requested_days <= 1 and display_days:
            return display

    if not weather_status:
        return {}

    if weather_status == "ok":
        try:
            payload = await build_weather_advisory_payload(
                trip_request=result.trip_request,
                user_text=user_text,
            )
            display = payload.display_payload()
            if isinstance(display, dict) and display:
                return display
        except Exception as exc:  # pragma: no cover - defensive persistence fallback
            logger.warning("weather display rebuild failed before persistence: %s", exc)

    return {
        "status": weather_status,
        "city": result.trip_request.to_city,
        "days": [],
    }
