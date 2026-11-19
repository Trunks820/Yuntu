"""Pre-writing commute budget resolution for route plans."""

from __future__ import annotations

from src.agents.composition_blueprint import is_food_place, is_photo_stop
from src.agents.pace import (
    daily_commute_budget_minutes,
    generation_base_mode,
    single_leg_max_minutes,
)
from src.agents.route_planning import _legs, _nearest_neighbor, _within_budget
from src.agents.route_feasibility import (
    DayContext,
    access_status,
    evaluate_day,
    POLICY_VERSION,
)
from src.agents.schema import (
    BudgetDayResult,
    BudgetResult,
    CandidatePlace,
    EffectiveCommuteMode,
    RoutePlan,
    TripRequest,
)
from src.config import get_settings


def daily_budget_minutes(request: TripRequest) -> int:
    settings = get_settings()
    return daily_commute_budget_minutes(request, settings)


def _protection_rank(place: CandidatePlace, all_places: list[CandidatePlace]) -> tuple:
    """Lower rank is more removable."""
    if _detour_outlier(place, all_places):
        category = 0
    elif _is_optional(place):
        category = 1
    elif _is_duplicate_same_area_weak(place, all_places):
        category = 2
    elif is_photo_stop(place):
        category = 3
    elif is_food_place(place) and place.source_count <= 1:
        category = 4
    elif place.place_type in {"snack", "dessert", "cafe", "coffee", "tea"}:
        category = 5
    elif not is_food_place(place):
        category = 6
    elif is_food_place(place):
        category = 7
    else:
        category = 8
    return (
        category,
        place.effective_score,
        place.recommend_score,
        place.place_id,
    )


def _is_optional(place: CandidatePlace) -> bool:
    text = _evidence_text(place)
    return place.place_type in {"optional", "other"} or "可选" in text


def _is_duplicate_same_area_weak(
    place: CandidatePlace,
    all_places: list[CandidatePlace],
) -> bool:
    if not place.adcode:
        return False
    same_area = [candidate for candidate in all_places if candidate.adcode == place.adcode]
    if len(same_area) < 3:
        return False
    stronger = [
        candidate for candidate in same_area
        if candidate.place_id != place.place_id
        and candidate.effective_score >= place.effective_score
    ]
    return len(stronger) >= 2


def _detour_outlier(place: CandidatePlace, all_places: list[CandidatePlace]) -> bool:
    if len(all_places) < 3:
        return False
    with_place = sum(
        leg.duration_minutes
        for leg in _legs(
            _nearest_neighbor(all_places),
            generation_mode="driving",
        )
    )
    without_place = [
        candidate for candidate in all_places
        if candidate.place_id != place.place_id
    ]
    without_minutes = sum(
        leg.duration_minutes
        for leg in _legs(
            _nearest_neighbor(without_place),
            generation_mode="driving",
        )
    )
    return with_place - without_minutes >= max(20, with_place * 0.35)


def _evidence_text(place: CandidatePlace) -> str:
    return " ".join(
        str(item.get("reason", ""))
        for item in [*place.top_reasons, *place.warnings]
        if isinstance(item, dict)
    )


def _minimum_viable(places: list[CandidatePlace], *, food_focused: bool) -> bool:
    activity_count = sum(1 for place in places if not is_food_place(place))
    food_count = sum(1 for place in places if is_food_place(place))
    if food_focused:
        return activity_count >= 1 and food_count >= 1 and len(places) >= 2
    return activity_count >= 1 and len(places) >= 2


def _resolve_day_places(
    places: list[CandidatePlace],
    *,
    budget_minutes: int,
    single_leg_max: int,
    food_focused: bool,
    generation_mode: EffectiveCommuteMode,
    accommodation_coord: tuple[float, float] | None = None,
) -> tuple[list[CandidatePlace], BudgetDayResult]:
    ordered = _nearest_neighbor(places, accommodation_coord)
    legs = _legs(ordered, generation_mode=generation_mode)
    return _resolve_ordered_day(
        ordered,
        legs=legs,
        budget_minutes=budget_minutes,
        single_leg_max=single_leg_max,
        food_focused=food_focused,
        generation_mode=generation_mode,
        accommodation_coord=accommodation_coord,
    )


def _resolve_ordered_day(
    ordered: list[CandidatePlace],
    *,
    legs,
    budget_minutes: int,
    single_leg_max: int,
    food_focused: bool,
    generation_mode: EffectiveCommuteMode,
    accommodation_coord: tuple[float, float] | None = None,
) -> tuple[list[CandidatePlace], BudgetDayResult]:
    commute_minutes = sum(leg.duration_minutes for leg in legs)
    if _within_budget(
        legs,
        daily_budget=budget_minutes,
        single_leg_max=single_leg_max,
    ):
        return ordered, BudgetDayResult(
            day=0,
            status="within_budget",
            budget_minutes=budget_minutes,
            commute_minutes=commute_minutes,
        )

    overage_ratio = (
        (commute_minutes - budget_minutes) / budget_minutes
        if budget_minutes > 0
        else 1.0
    )
    if overage_ratio <= 0.10 and all(
        leg.duration_minutes <= single_leg_max for leg in legs
    ):
        return ordered, BudgetDayResult(
            day=0,
            status="relaxed_exception",
            budget_minutes=budget_minutes,
            commute_minutes=commute_minutes,
            overage_ratio=round(overage_ratio, 3),
            reason="slightly_over_budget",
        )

    if overage_ratio > 0.50:
        return ordered, BudgetDayResult(
            day=0,
            status="infeasible",
            budget_minutes=budget_minutes,
            commute_minutes=commute_minutes,
            overage_ratio=round(overage_ratio, 3),
            reason="severely_over_budget",
        )

    current = ordered
    removed: list[CandidatePlace] = []
    while len(current) > 2:
        candidate = min(current, key=lambda place: _protection_rank(place, current))
        next_places = [
            place for place in current
            if place.place_id != candidate.place_id
        ]
        if not _minimum_viable(next_places, food_focused=food_focused):
            break
        removed.append(candidate)
        current = _nearest_neighbor(next_places, accommodation_coord)
        current_legs = _legs(current, generation_mode=generation_mode)
        current_minutes = sum(leg.duration_minutes for leg in current_legs)
        if _within_budget(
            current_legs,
            daily_budget=budget_minutes,
            single_leg_max=single_leg_max,
        ):
            return current, BudgetDayResult(
                day=0,
                status="pruned",
                budget_minutes=budget_minutes,
                commute_minutes=current_minutes,
                overage_ratio=0.0,
                removed_place_ids=[place.place_id for place in removed],
                removed_place_names=[place.name for place in removed],
                reason="pruned_weak_stops",
            )

    return current, BudgetDayResult(
        day=0,
        status="infeasible",
        budget_minutes=budget_minutes,
        commute_minutes=sum(
            leg.duration_minutes
            for leg in _legs(current, generation_mode=generation_mode)
        ),
        overage_ratio=round(overage_ratio, 3),
        removed_place_ids=[place.place_id for place in removed],
        removed_place_names=[place.name for place in removed],
        reason="unable_to_prune_to_budget",
    )


def _model_route_day_facts_consistent(day) -> bool:
    """Facts only. Old load, leg and day-structure limits are not a veto."""
    fact = day.day_feasibility
    if fact is None:
        return False
    ids = [place.place_id for place in day.places]
    if list(fact.ordered_place_ids) != ids:
        return False
    if [(leg.from_place_id, leg.to_place_id) for leg in day.commute_legs] != list(zip(ids, ids[1:])):
        return False
    poi_minutes = sum(int(leg.duration_minutes) for leg in day.commute_legs)
    if int(fact.poi_commute_minutes) != poi_minutes:
        return False
    access_total = sum(int(leg.duration_minutes) for leg in day.access_legs)
    if day.access_legs:
        access = {leg.direction: leg for leg in day.access_legs}
        anchors = {
            (leg.anchor_source, leg.anchor_latitude, leg.anchor_longitude)
            for leg in day.access_legs
        }
        if (
            len(day.access_legs) != 2
            or set(access) != {"outbound", "inbound"}
            or not ids
            or access["outbound"].place_id != ids[0]
            or access["inbound"].place_id != ids[-1]
            or len(anchors) != 1
            or fact.access_minutes != access_total
        ):
            return False
    elif fact.access_minutes is not None:
        return False
    if fact.access_status != access_status(day.access_legs):
        return False
    return int(day.commute_minutes) == poi_minutes + access_total


def resolve_route_budgets(
    route_plans: list[RoutePlan],
    request: TripRequest,
    *,
    accommodation_coord: tuple[float, float] | None = None,
) -> list[BudgetResult]:
    """Resolve commute budget before Writer and mutate pruned route plans."""
    settings = get_settings()
    route_generation_mode, _ = generation_base_mode(request, settings)
    budget_minutes = daily_budget_minutes(request)
    single_leg_max = single_leg_max_minutes(route_generation_mode, settings)
    food_focused = any(
        marker in " ".join([*request.preferences, request.notes])
        for marker in ("美食", "吃", "小吃", "火锅", "咖啡")
    )
    results: list[BudgetResult] = []
    for route_plan in route_plans:
        if route_plan.planner_source == "model":
            days = []
            for day in route_plan.day_groups:
                consistent = _model_route_day_facts_consistent(day)
                days.append(BudgetDayResult(
                    day=day.day,
                    status="within_budget" if consistent else "infeasible",
                    budget_minutes=(
                        day.traffic_policy.daily_limit_minutes
                        if day.traffic_policy else budget_minutes
                    ),
                    commute_minutes=day.commute_minutes,
                    reason="locked_model_route" if consistent else "model_route_fact_mismatch",
                ))
            results.append(BudgetResult(plan_label=route_plan.label, days=days))
            continue
        if route_plan.route_policy_version == POLICY_VERSION:
            days = []
            for day in route_plan.day_groups:
                check = evaluate_day(day.places, day.commute_legs, day.access_legs, DayContext(request, settings, day.traffic_policy, day.time_policy, tuple(day.visit_profiles)))
                # Read-only verification: stored load and order must match the locked facts.
                valid = check.feasible and check == day.day_feasibility and day.commute_minutes == check.poi_commute_minutes + (check.access_minutes or 0)
                days.append(BudgetDayResult(day=day.day, status="within_budget" if valid else "infeasible",
                    budget_minutes=day.traffic_policy.daily_limit_minutes if day.traffic_policy else budget_minutes, commute_minutes=check.poi_commute_minutes + (check.access_minutes or 0),
                    reason="locked_route_v2" if valid else "route_v2_feasibility_mismatch"))
            results.append(BudgetResult(plan_label=route_plan.label, days=days))
            continue
        day_results: list[BudgetDayResult] = []
        retained_days = []
        for day_group in route_plan.day_groups:
            if not route_plan.optimized or len(day_group.places) < 2:
                day_result = BudgetDayResult(
                    day=day_group.day,
                    status="within_budget",
                    budget_minutes=budget_minutes,
                    commute_minutes=day_group.commute_minutes,
                    reason="not_optimized_or_single_stop",
                )
                retained_days.append(day_group)
                day_results.append(day_result)
                continue

            kept, day_result = _resolve_day_places(
                day_group.places,
                budget_minutes=budget_minutes,
                single_leg_max=single_leg_max,
                food_focused=food_focused,
                generation_mode=route_generation_mode,
                accommodation_coord=accommodation_coord,
            )
            if day_group.commute_legs:
                kept, day_result = _resolve_ordered_day(
                    day_group.places,
                    legs=day_group.commute_legs,
                    budget_minutes=budget_minutes,
                    single_leg_max=single_leg_max,
                    food_focused=food_focused,
                    generation_mode=route_generation_mode,
                    accommodation_coord=accommodation_coord,
                )
            day_result.day = day_group.day
            day_results.append(day_result)
            if day_result.status == "pruned":
                day_group.places = kept
                day_group.commute_legs = _legs(
                    kept,
                    generation_mode=route_generation_mode,
                )
                day_group.commute_minutes = sum(
                    leg.duration_minutes for leg in day_group.commute_legs
                )
                day_group.commute_notes = [leg.note for leg in day_group.commute_legs]
                route_plan.dropped_place_ids.extend(day_result.removed_place_ids)
            retained_days.append(day_group)

        route_plan.day_groups = retained_days
        route_plan.dropped_place_ids = sorted(set(route_plan.dropped_place_ids))
        for index, day_group in enumerate(route_plan.day_groups, 1):
            day_group.day = index
        results.append(BudgetResult(
            plan_label=route_plan.label,
            days=day_results,
        ))
    return results
