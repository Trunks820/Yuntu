"""Bounded, privacy-safe observations of the existing route planner.

This module only projects facts produced by routing.  It never calls a provider,
reruns search, changes a candidate, or assigns a stronger failure reason than
the planner proved.
"""

from __future__ import annotations

import json
from typing import Any, Iterable

from src.agents import route_planning
from src.agents.schema import (
    AccommodationSuggestion,
    PoiSelectionInput,
    RetrievalResult,
    RouteDayGroup,
    RoutePlan,
    TripRequest,
)
from src.jobs.generation_delivery import (
    RouteDiagnostics,
    RouteSearchObservation,
    RouteViolation,
)

SUMMARY_MAX_BYTES = 16 * 1024
REPLAY_MAX_BYTES = 256 * 1024
REPLAY_TTL_DAYS = 7

ELIMINATION_CODES = frozenset({
    "HARD_INELIGIBLE",
    "TEMPORALLY_UNSCHEDULABLE",
    "CAPACITY_LIMIT",
    "ROUTE_FEASIBILITY_LIMIT",
    "NOT_CHOSEN_FOR_FINAL_ROUTE",
})

VIOLATION_CODES = frozenset({
    "FULL_DAY_CONFLICT",
    "INCOMPLETE_DAYS",
    "INVALID_MEMBERSHIP",
    "MISSING_MUST_INCLUDE",
    "ACCOMMODATION_POLICY_MISMATCH",
    "LOCKED_ACCOMMODATION_MISMATCH",
    "MISSING_LOCKED_ACCOMMODATION",
    "DAYTIME_AFTER_EVENING",
    "TEMPORALLY_UNSCHEDULABLE",
    "HARD_AVOID",
    "NEAR_DUPLICATE",
    "INVALID_ACCESS_COVERAGE",
    "ACCESS_ENDPOINT_MISMATCH",
    "ACCESS_ANCHOR_MISMATCH",
    "DAY_STRUCTURE",
    "DUPLICATE_PLACE",
    "NO_DAYTIME_ACTIVITY",
    "INELIGIBLE_PLACE",
    "POI_LEG_MISMATCH",
    "INVALID_LEG",
    "LOAD_LIMIT",
    "DAILY_COMMUTE_LIMIT",
    "SINGLE_LEG_LIMIT",
})


def _json_size(value: Any) -> int:
    # PostgreSQL jsonb::text inserts the ordinary comma/colon spaces too.  The
    # default separators deliberately mirror that larger representation.
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True).encode())


def _candidate_count(retrieval: RetrievalResult) -> int:
    pool = retrieval.route_planning_candidates or retrieval.candidates
    return len({int(place.place_id) for place in pool})


def _safe_eliminations(metrics: route_planning.RoutePlanningMetrics) -> dict[str, int]:
    return {
        code: max(0, int(count))
        for code, count in sorted(metrics.selected_drop_reason_counts.items())
        if code in ELIMINATION_CODES and int(count) > 0
    }


def _violation(
    code: str,
    *,
    day: int | None = None,
    actual: float | None = None,
    limit: float | None = None,
    unit: str = "NONE",
) -> RouteViolation | None:
    normalized = str(code or "").strip().upper()
    if normalized not in VIOLATION_CODES:
        return None
    return RouteViolation(
        day=day,
        code=normalized,
        actual=actual,
        limit=limit,
        unit=unit,
    )


def _day_violations(day_group: RouteDayGroup) -> list[RouteViolation]:
    feasibility = day_group.day_feasibility
    if feasibility is None:
        return []
    result: list[RouteViolation] = []
    for raw_code in feasibility.violations:
        code = str(raw_code).upper()
        kwargs: dict[str, Any] = {"day": int(day_group.day), "unit": "NONE"}
        if code == "LOAD_LIMIT":
            kwargs.update(
                actual=float(feasibility.load_minutes),
                limit=float(feasibility.load_limit_minutes),
                unit="MINUTES",
            )
        elif code == "DAILY_COMMUTE_LIMIT":
            kwargs.update(
                actual=float(
                    feasibility.poi_commute_minutes
                    + (feasibility.access_minutes or 0)
                ),
                limit=(
                    float(day_group.traffic_policy.daily_limit_minutes)
                    if day_group.traffic_policy is not None else None
                ),
                unit="MINUTES",
            )
        elif code == "SINGLE_LEG_LIMIT":
            durations = [
                int(leg.duration_minutes)
                for leg in [*day_group.commute_legs, *day_group.access_legs]
            ]
            kwargs.update(
                actual=float(max(durations)) if durations else None,
                unit="MINUTES",
            )
        projected = _violation(code, **kwargs)
        if projected is not None:
            result.append(projected)
    return result


def _failure_groups(error: BaseException | None) -> list[RouteDayGroup]:
    groups = getattr(error, "day_groups", ()) if error is not None else ()
    return [group for group in groups if isinstance(group, RouteDayGroup)]


def _project_violations(
    plans: Iterable[RoutePlan], error: BaseException | None
) -> tuple[list[RouteViolation], bool]:
    projected: list[RouteViolation] = []
    groups = [group for plan in plans for group in plan.day_groups]
    groups.extend(_failure_groups(error))
    for group in groups:
        projected.extend(_day_violations(group))
    raw_codes = list(getattr(error, "violations", ()) or ())
    if isinstance(error, route_planning.RouteFullDayConflictError):
        projected.append(_violation(
            "FULL_DAY_CONFLICT", actual=float(error.required_days),
            limit=float(error.available_days), unit="COUNT",
        ))
        raw_codes = [code for code in raw_codes if str(code).upper() != "FULL_DAY_CONFLICT"]
    reason = getattr(error, "reason", None)
    if reason:
        raw_codes.append(reason)
    for raw_code in raw_codes:
        item = _violation(str(raw_code))
        if item is not None:
            projected.append(item)
    unique: list[RouteViolation] = []
    seen: set[tuple[Any, ...]] = set()
    for item in projected:
        key = (item.day, item.code, item.actual, item.limit, item.unit)
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique[:20], len(unique) > 20


def _stop_reason(
    *,
    metrics: route_planning.RoutePlanningMetrics,
    candidate_count: int,
    plans: list[RoutePlan],
    error: BaseException | None,
) -> tuple[str, str]:
    if plans and all(plan.day_groups for plan in plans):
        return "SUCCESS", "COMPLETE"
    if candidate_count == 0:
        return "NO_CANDIDATES", "COMPLETE"
    if isinstance(error, route_planning.RouteFullDayConflictError):
        return "CONSTRAINT_CONFLICT", "COMPLETE"
    if metrics.route_v2_stop_reason == "search_budget_exhausted":
        return "SEARCH_BUDGET_EXHAUSTED", "PARTIAL"
    if (
        isinstance(error, route_planning.RouteMustIncludeConflictError)
        and getattr(error, "reason", None) == "hard_conflict"
    ):
        return "CONSTRAINT_CONFLICT", "COMPLETE"
    # qualified_pool_exhausted and a best failed candidate are not proofs that
    # every possible route is mathematically infeasible.
    return "ERROR", "PARTIAL" if error is not None else "UNAVAILABLE"


def _transport_snapshot(plans: Iterable[RoutePlan]) -> list[dict[str, Any]]:
    return [
        {
            "day": int(day.day),
            "from_place_id": int(leg.from_place_id),
            "to_place_id": int(leg.to_place_id),
            "mode": leg.mode,
            "duration_minutes": int(leg.duration_minutes),
            "distance_meters": int(leg.distance_meters),
            "source": str(leg.source or "unknown")[:32],
            "cache_hit": bool(leg.transit_detail_cache_hit),
        }
        for plan in plans
        for day in plan.day_groups
        for leg in day.commute_legs
    ]


def _anchor_snapshot(plans: Iterable[RoutePlan]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for index, plan in enumerate(plans, 1):
        anchor = plan.accommodation_anchor
        if anchor is None:
            continue
        suggestion = anchor.suggestion
        result.append({
            "plan_index": index,
            "place_id": anchor.place_id,
            "latitude": suggestion.latitude,
            "longitude": suggestion.longitude,
            "source": suggestion.source,
            "resolution_state": plan.accommodation_resolution_state,
        })
    return result


def build_route_observation(
    *,
    request: TripRequest,
    selector_input: PoiSelectionInput,
    retrieval: RetrievalResult,
    plans: list[RoutePlan],
    metrics: route_planning.RoutePlanningMetrics,
    elapsed_ms: int | None,
    error: BaseException | None = None,
    accommodation: AccommodationSuggestion | None = None,
) -> tuple[RouteDiagnostics, dict[str, Any]]:
    """Project a summary and bounded replay payload from already-produced facts."""
    count = _candidate_count(retrieval)
    stop_reason, evidence_state = _stop_reason(
        metrics=metrics,
        candidate_count=count,
        plans=plans,
        error=error,
    )
    violations, violations_truncated = _project_violations(plans, error)
    diagnostics = RouteDiagnostics(
        evidence_state=evidence_state,
        stop_reason=stop_reason,
        candidate_count=count,
        eliminated_counts=_safe_eliminations(metrics),
        search=RouteSearchObservation(
            iterations=(
                int(metrics.route_v2_trial_count)
                if metrics.route_policy_version == "selector-route-v2" else None
            ),
            iteration_limit=(
                int(getattr(route_planning.get_settings(), "route_v2_max_trials", 0) or 0) or None
                if metrics.route_policy_version == "selector-route-v2" else None
            ),
            elapsed_ms=max(0, int(elapsed_ms)) if elapsed_ms is not None else None,
            # The Amap enrichment budget is not the route-search time budget.
            time_limit_ms=None,
        ),
        violations=violations,
        truncated=violations_truncated,
        # The persistence transaction changes this to true only when it stores
        # the replay body and expiry together with the terminal job.
        replay_available=False,
    )
    pool = retrieval.route_planning_candidates or retrieval.candidates
    failure_groups = _failure_groups(error)
    observed_plans = plans or (
        [RoutePlan(label="best-failed", day_groups=failure_groups)]
        if failure_groups else []
    )
    replay: dict[str, Any] = {
        "schema_version": "1.0",
        # The privacy-safe snapshot deliberately omits free-form request text,
        # so it is evidence for replay investigation rather than a complete
        # standalone recomputation input.
        "evidence_state": "PARTIAL",
        "algorithm": {
            "route_policy_version": metrics.route_policy_version,
            "iteration_limit": diagnostics.search.iteration_limit,
        },
        "constraints": {
            "days": int(request.days),
            "must_include_place_ids": list(
                selector_input.constraints.must_include_place_ids
            ),
            "eligible_place_ids": list(selector_input.constraints.eligible_place_ids),
            "ordinary_min_count": getattr(
                selector_input.constraints, "ordinary_min_count", None
            ),
            "ordinary_max_count": getattr(
                selector_input.constraints, "ordinary_max_count", None
            ),
            "ordinary_target_count": selector_input.constraints.ordinary_target_count,
            "commute_mode": request.commute_mode,
            "daily_start": request.daily_start,
            "daily_end": request.daily_end,
            "rest_windows": [
                {"days": window.days, "start": window.start, "end": window.end}
                for window in request.rest_windows
            ],
            "avoid_rule_count": len(request.avoid),
        },
        "candidates": [
            {
                "place_id": int(place.place_id),
                "latitude": place.latitude,
                "longitude": place.longitude,
                "visit_minutes": (
                    int(place.typical_visit_minutes)
                    if place.typical_visit_minutes is not None else None
                ),
            }
            for place in pool
        ],
        "selected_routes": [
            {
                "plan_index": plan_index,
                "days": [
                    {
                        "day": int(day.day),
                        "place_ids": [int(place.place_id) for place in day.places],
                    }
                    for day in plan.day_groups
                ],
            }
            for plan_index, plan in enumerate(observed_plans, 1)
        ],
        "transport": _transport_snapshot(observed_plans),
        "accommodation_anchors": (
            _anchor_snapshot(observed_plans)
            or ([{
                "place_id": (
                    request.accommodation.place_id
                    if request.accommodation is not None else None
                ),
                "latitude": accommodation.latitude,
                "longitude": accommodation.longitude,
                "source": accommodation.source,
            }] if accommodation is not None else [])
        ),
        "repairs": {
            "accepted_action": metrics.route_v2_accepted_action or None,
            "rounds": int(metrics.route_v2_repair_rounds),
        },
        "coverage": [
            dict(row)
            for row in list(getattr(metrics, "route_v2_coverage_report", []) or [])[:24]
        ],
        "repair_trace": [
            dict(row)
            for row in list(getattr(metrics, "route_v2_repair_trace", []) or [])[:24]
        ],
    }
    if _json_size(replay) > REPLAY_MAX_BYTES:
        diagnostics.evidence_state = "PARTIAL"
        diagnostics.truncated = True
        replay["evidence_state"] = "PARTIAL"
        while replay["candidates"] and _json_size(replay) > REPLAY_MAX_BYTES:
            replay["candidates"] = replay["candidates"][: len(replay["candidates"]) // 2]
        if _json_size(replay) > REPLAY_MAX_BYTES:
            replay["transport"] = []
        replay["truncated"] = True
    if _json_size(replay) > REPLAY_MAX_BYTES:
        replay = {}
    if _json_size(diagnostics.model_dump(mode="json")) > SUMMARY_MAX_BYTES:
        diagnostics.violations = []
        diagnostics.evidence_state = "PARTIAL"
        diagnostics.truncated = True
    return diagnostics, replay


