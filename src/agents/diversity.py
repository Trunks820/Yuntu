"""Deterministic bounded-diversity helpers for travel recommendations."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence

from src.agents.preferences import candidate_matches_scoring_preference, order_preference_candidates
from src.agents.schema import (
    CandidateGroup,
    CandidatePlace,
    PlanOutput,
    RetrievalResult,
    RoutePlan,
)

QUALITY_THRESHOLD = 1.5
MIN_PLACES_PER_PLAN = 5
MAX_CANDIDATES_PER_GROUP = 15
IN_RESPONSE_OVERLAP_TARGET = 0.5
CROSS_REQUEST_OVERLAP_TARGET = 0.7
HISTORY_PENALTY_PER_APPEARANCE = 1.0

_RELAXED_PACE_MARKERS = ("轻松", "不想太累", "慢旅行", "悠闲")
_SOLO_TRAVEL_MARKERS = ("一个人", "独自", "独行", "solo")


def _has_non_empty_reason(items: list[dict]) -> bool:
    return any(
        isinstance(item, dict)
        and isinstance(item.get("reason"), str)
        and bool(item["reason"].strip())
        for item in items
    )


def is_specific_place_name(name: str, *, city: str | None = None) -> bool:
    """Reject generic city and short administrative-division names."""
    normalized = name.strip()
    if not normalized or (city and normalized == city.strip()):
        return False
    return not (
        len(normalized) <= 4
        and normalized.endswith(("市", "区", "县"))
    )


def is_qualified_candidate(
    candidate: CandidatePlace,
    *,
    city: str | None = None,
    quality_threshold: float = QUALITY_THRESHOLD,
) -> bool:
    """Return whether a candidate meets the fixed v0.3 admission rules."""
    return (
        candidate.mention_count >= 1
        and candidate.source_count >= 1
        and candidate.quality_score >= quality_threshold
        and is_specific_place_name(candidate.name, city=city)
        and (
            _has_non_empty_reason(candidate.top_reasons)
            or _has_non_empty_reason(candidate.warnings)
        )
    )


def recent_appearance_counts(
    recent_place_id_sets: Iterable[set[int]] | None,
) -> Counter[int]:
    """Count in how many recent successful recommendations each place appeared."""
    counts: Counter[int] = Counter()
    for place_ids in recent_place_id_sets or []:
        counts.update(place_ids)
    return counts


def rank_candidates(
    candidates: Iterable[CandidatePlace],
    *,
    recent_place_id_sets: Iterable[set[int]] | None = None,
    preferred_place_types: set[str] | None = None,
    preferences: Sequence[str] | None = None,
    trip_request=None,
) -> list[CandidatePlace]:
    """Apply deterministic preference boosts and recent-history penalties."""
    counts = recent_appearance_counts(recent_place_id_sets)
    preferred_types = preferred_place_types or set()
    ranked = list(candidates)
    for candidate in ranked:
        if preferences is not None:
            preference_boost = (
                1.0
                if candidate_matches_scoring_preference(candidate, preferences)
                else 0.0
            )
        else:
            preference_boost = 1.0 if candidate.place_type in preferred_types else 0.0
        history_penalty = HISTORY_PENALTY_PER_APPEARANCE * counts[candidate.place_id]
        if candidate.base_priority > 0:
            candidate.effective_score = (
                candidate.base_priority * 0.6
                + candidate.recommend_score * 0.3
                + preference_boost * 1.0
                - history_penalty
            )
        else:
            candidate.effective_score = (
                candidate.recommend_score + preference_boost - history_penalty
            )

    ranked.sort(
        key=lambda candidate: (
            not candidate_matches_scoring_preference(candidate, preferences) if preferences else False,
            -candidate.effective_score,
            -candidate.recommend_score,
            -candidate.mention_count,
            candidate.place_id,
        )
    )
    if preferences:
        from src.agents.schema import TripRequest
        ranked = order_preference_candidates(ranked, trip_request or TripRequest(preferences=list(preferences)))
    return ranked


def overlap_ratio(left: Iterable[int], right: Iterable[int]) -> float | None:
    """Measure overlap relative to the smaller non-empty set."""
    left_ids = set(left)
    right_ids = set(right)
    if not left_ids or not right_ids:
        return None
    return len(left_ids & right_ids) / min(len(left_ids), len(right_ids))


def current_result_overlap_ratio(
    current: Iterable[int],
    previous: Iterable[int],
) -> float | None:
    """Measure how much of the current result repeats the previous result."""
    current_ids = set(current)
    previous_ids = set(previous)
    if not current_ids:
        return None
    return len(current_ids & previous_ids) / len(current_ids)


def build_candidate_groups(
    candidates: Iterable[CandidatePlace],
    *,
    min_places_per_plan: int = MIN_PLACES_PER_PLAN,
    max_candidates_per_group: int = MAX_CANDIDATES_PER_GROUP,
) -> tuple[list[CandidateGroup], float | None, bool]:
    """Split ranked candidates into two deterministic writer input groups."""
    ranked = list(candidates)
    groups: list[list[CandidatePlace]] = [[], []]
    selected = ranked[: max_candidates_per_group * 2]

    for index, candidate in enumerate(selected):
        groups[index % 2].append(candidate)

    for group in groups:
        existing_ids = {candidate.place_id for candidate in group}
        for candidate in ranked:
            if len(group) >= min_places_per_plan:
                break
            if candidate.place_id in existing_ids:
                continue
            group.append(candidate)
            existing_ids.add(candidate.place_id)

    candidate_overlap = overlap_ratio(
        (candidate.place_id for candidate in groups[0]),
        (candidate.place_id for candidate in groups[1]),
    )
    diversity_gap = (
        len(groups[0]) < min_places_per_plan
        or len(groups[1]) < min_places_per_plan
        or candidate_overlap is None
        or candidate_overlap > IN_RESPONSE_OVERLAP_TARGET
    )
    return (
        [
            CandidateGroup(label="A", candidates=groups[0]),
            CandidateGroup(label="B", candidates=groups[1]),
        ],
        candidate_overlap,
        diversity_gap,
    )


def merged_used_place_ids(plans: Iterable[PlanOutput]) -> set[int]:
    """Merge place IDs from every plan in one reviewed recommendation."""
    return {
        place_id
        for plan in plans
        for place_id in plan.used_place_ids
    }


def normalize_plan_places(
    plans: Iterable[PlanOutput],
    retrieval: RetrievalResult,
    route_plans: list[RoutePlan] | None = None,
) -> list[PlanOutput]:
    """Normalize place structure fields for delivery.

    When route_plans are present (v0.8.13+ locked itinerary path), structure is
    always flattened from RouteDayGroup.places in lock order. Never rebuild
    used_place_ids / used_place_names from plan_text, or alias first-hits can
    silently reorder them relative to day_place_names.

    Without route_plans (legacy/debug path), rebuild from delivery text against
    retrieval candidates.
    """
    candidate_by_id = {candidate.place_id: candidate for candidate in retrieval.candidates}
    normalized: list[PlanOutput] = []

    for plan_index, plan in enumerate(plans):
        if route_plans is not None and plan_index < len(route_plans):
            route_plan = route_plans[plan_index]
            day_place_names = [
                [place.name for place in day_group.places if place.name]
                for day_group in route_plan.day_groups
            ]
            used_place_ids = [
                place.place_id
                for day_group in route_plan.day_groups
                for place in day_group.places
            ]
            used_place_names = [
                place.name
                for day_group in route_plan.day_groups
                for place in day_group.places
                if place.name
            ]
            normalized.append(PlanOutput(
                plan_name=plan.plan_name,
                plan_text=plan.plan_text,
                summary=plan.summary,
                used_place_ids=used_place_ids,
                used_place_names=used_place_names,
                day_place_names=day_place_names,
                composition_blueprint=plan.composition_blueprint,
                poi_identity_result=plan.poi_identity_result,
                budget_result=plan.budget_result,
                accommodation=plan.accommodation,
                transport=plan.transport,
                packing_checklist=plan.packing_checklist,
                travel_tips=plan.travel_tips,
                poi_fragments=plan.poi_fragments,
                code_lodging_section=plan.code_lodging_section,
            ))
            continue

        candidates = list(retrieval.candidates)
        candidates.sort(
            key=lambda candidate: (-len(candidate.name), candidate.place_id),
        )
        matched: list[tuple[int, CandidatePlace]] = []
        occupied_spans: list[tuple[int, int]] = []
        for candidate in candidates:
            start = 0
            while True:
                index = plan.plan_text.find(candidate.name, start)
                if index < 0:
                    break
                span = (index, index + len(candidate.name))
                next_char = plan.plan_text[span[1]:span[1] + 1]
                if len(candidate.name) <= 4 and next_char in {"市", "区", "县"}:
                    start = index + 1
                    continue
                if not any(span[0] < used[1] and used[0] < span[1] for used in occupied_spans):
                    matched.append((index, candidate))
                    occupied_spans.append(span)
                    break
                start = index + 1

        matched.sort(key=lambda item: (item[0], item[1].place_id))
        normalized.append(PlanOutput(
            plan_name=plan.plan_name,
            plan_text=plan.plan_text,
            summary=plan.summary,
            used_place_ids=[candidate.place_id for _, candidate in matched],
            used_place_names=[candidate.name for _, candidate in matched],
            day_place_names=plan.day_place_names,
            composition_blueprint=plan.composition_blueprint,
            poi_identity_result=plan.poi_identity_result,
            budget_result=plan.budget_result,
            accommodation=plan.accommodation,
            transport=plan.transport,
            packing_checklist=plan.packing_checklist,
            travel_tips=plan.travel_tips,
            poi_fragments=plan.poi_fragments,
            code_lodging_section=plan.code_lodging_section,
        ))
    return normalized


def candidate_group_violations(
    plans: Iterable[PlanOutput],
    retrieval: RetrievalResult,
) -> list[dict]:
    """Return reviewed plan places that fall outside their assigned A/B group."""
    groups = retrieval.candidate_groups
    violations = []
    for index, plan in enumerate(plans):
        if index >= len(groups):
            violations.append({
                "plan_index": index + 1,
                "reason": "missing_candidate_group",
                "place_ids": plan.used_place_ids,
            })
            continue

        allowed_ids = {candidate.place_id for candidate in groups[index].candidates}
        out_of_group_ids = [
            place_id for place_id in plan.used_place_ids if place_id not in allowed_ids
        ]
        if out_of_group_ids:
            violations.append({
                "plan_index": index + 1,
                "reason": "out_of_group_places",
                "place_ids": out_of_group_ids,
            })
    return violations


def observed_unstructured_preferences(user_text: str) -> list[str]:
    """Identify preferences that v0.3 observes but does not rank automatically."""
    lowered = user_text.lower()
    observed = []
    if any(marker in lowered for marker in _RELAXED_PACE_MARKERS):
        observed.append("relaxed_pace")
    if any(marker in lowered for marker in _SOLO_TRAVEL_MARKERS):
        observed.append("solo_travel")
    return observed


def build_quality_metrics(
    *,
    user_text: str,
    plans: list[PlanOutput],
    retrieval: RetrievalResult,
    recent_place_id_sets: list[set[int]] | None = None,
) -> dict:
    """Build internal metrics after Review has returned delivery-safe plans."""
    plan_overlap = None
    if len(plans) >= 2:
        plan_overlap = overlap_ratio(
            plans[0].used_place_ids,
            plans[1].used_place_ids,
        )

    merged_ids = merged_used_place_ids(plans)
    cross_request_overlap = None
    if recent_place_id_sets:
        cross_request_overlap = current_result_overlap_ratio(
            merged_ids,
            recent_place_id_sets[0],
        )

    preference_gaps = observed_unstructured_preferences(user_text)
    group_violations = candidate_group_violations(plans, retrieval)
    diversity_gap = (
        retrieval.diversity_gap
        or bool(group_violations)
        or plan_overlap is None
        or plan_overlap > IN_RESPONSE_OVERLAP_TARGET
        or (
            cross_request_overlap is not None
            and cross_request_overlap > CROSS_REQUEST_OVERLAP_TARGET
        )
    )
    return {
        "qualified_candidate_count": retrieval.qualified_candidate_count,
        "quality_threshold": QUALITY_THRESHOLD,
        "in_response_candidate_overlap": retrieval.in_response_candidate_overlap,
        "in_response_overlap": plan_overlap,
        "cross_request_overlap": cross_request_overlap,
        "diversity_gap": diversity_gap,
        "candidate_group_violation_count": len(group_violations),
        "candidate_group_violations": group_violations,
        "observed_unstructured_preferences": preference_gaps,
        "preference_evidence_gaps": preference_gaps,
    }
