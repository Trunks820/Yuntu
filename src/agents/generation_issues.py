"""Unified generation issue taxonomy for v0.6.22."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, Field

from src.agents.accommodation_review import mask_accommodation_prefix_for_route_scan
from src.agents.blueprint_integrity import check_plan_blueprint_integrity
from src.agents.fact_expression_taxonomy import (
    has_hard_fact_marker,
    has_soft_unauthorized_expression_marker,
    unsupported_fact_pattern_matches,
)
from src.agents.food_review import (
    FoodAttachmentAuthMap,
    FoodAttachmentKey,
    find_food_rule_violations,
    mask_authorized_food_prose,
    repair_food_plan_text,
)
from src.agents.poi_fragments import fragment_for_span, remap_fragment_registry
from src.agents.route_planning import route_plan_violations
from src.agents.schema import (
    AccommodationSuggestion,
    BudgetResult,
    CompositionBlueprint,
    PlanOutput,
    RetrievalResult,
    RoutePlan,
    TripRequest,
)
from src.agents.weather_advisory import (
    WeatherAdvisoryPayload,
    weather_review_issues,
)

IssueSource = Literal["deterministic", "llm_review", "publish_gate"]
IssueCategory = Literal["BLOCKER", "REPAIR", "WARN"]
PublishAction = Literal["FAIL_CLOSED", "REPAIR_PLAN", "RECORD_ONLY"]
IssueTag = Literal["DATA_GAP"]
IssueSideEffect = Literal["DATA_BACKLOG"]

CATEGORY_RANK = {"WARN": 1, "REPAIR": 2, "BLOCKER": 3}
PUBLISH_ACTION_RANK = {"RECORD_ONLY": 1, "REPAIR_PLAN": 2, "FAIL_CLOSED": 3}
NON_REPAIRABLE_REASONS = {
    "accommodation_fabrication",
    "accommodation_price_claim",
    "accommodation_in_route",
    "city_mismatch",
    "plan_count_mismatch",
    "missing_route_plan",
    "budget_infeasible",
    "budget_overrun_without_exception",
    # O1 ordered lock: structural order/membership/ambiguous-alias cannot be
    # fixed by whole-plan Writer repair or Fragment Repair. Design routes them
    # to Publish Retry / fail-closed; until O3 Dispatch lands, keep them out of
    # REPAIR_PLAN so the old repair loop is not entered.
    "declared_day_count_mismatch",
    "text_day_count_mismatch",
    "declared_locked_day_group_violation",
    "text_locked_day_group_violation",
    "ambiguous_alias",
    "day_place_names_mismatch",
    "plan_text_missing_locked_stop",
    "cross_day_poi",
    "route_outside_poi",
    "food_none_tier_violation",
}
COPY_QUALITY_REASONS = {
    "unsupported_fact_expansion",
    "weak_evidence_data_gap",
    "generic_copy_quality_warn",
    "database_tone",
    "rare_character_compatibility",
    "placeholder_wording",
    "duplicate_day_heading",
    "too_short_plan_text",
    "activity_content_missing",
    "food_place_written_as_attraction",
    "unsupported_meal_role",
    "weather_unauthorized_claim",
    "weather_route_drift",
    "weather_poi_bound_claim",
    "weather_disclaimer_leak",
    "weather_format_violation",
    "food_tier_exceeded",
    "food_none_tier_violation",
    "food_source_attribution",
}
MEAL_ROLE_MARKERS = ("午餐", "晚餐", "正餐", "吃饭", "用餐")


class GenerationIssue(BaseModel):
    source: IssueSource
    category: IssueCategory
    publish_action: PublishAction
    reason: str
    plan_index: int | None = None
    day: int | None = None
    place_id: int | None = None
    names: list[str] = Field(default_factory=list)
    snippet: str = ""
    evidence: str = ""
    tags: list[IssueTag] = Field(default_factory=list)
    side_effects: list[IssueSideEffect] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class IssueTaxonomyResult(BaseModel):
    issues: list[GenerationIssue] = Field(default_factory=list)
    repair_target_plan_indexes: list[int] = Field(default_factory=list)
    fail_closed_reasons: list[str] = Field(default_factory=list)
    data_backlog_reasons: list[str] = Field(default_factory=list)
    resolver_notes: list[str] = Field(default_factory=list)
    issue_counts: dict[str, int] = Field(default_factory=dict)


class PlanRepairResult(BaseModel):
    repaired_plans: list[PlanOutput]
    repaired_plan_indexes: list[int]
    success: bool
    failure_reasons: list[str] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _normalized_issue_key(issue: GenerationIssue) -> tuple:
    if issue.place_id is not None:
        return (
            issue.plan_index,
            issue.day,
            issue.place_id,
            issue.reason,
            (),
        )
    names_key = tuple(sorted(_normalize_text(name) for name in issue.names if name))
    snippet_key = _normalize_text(issue.snippet)
    name_or_snippet = names_key or (snippet_key,)
    return (
        issue.plan_index,
        issue.day,
        issue.place_id,
        issue.reason,
        name_or_snippet,
    )


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", "", (value or "").strip()).lower()


def _merge_issue(left: GenerationIssue, right: GenerationIssue) -> GenerationIssue:
    category = max(
        (left.category, right.category),
        key=lambda value: CATEGORY_RANK[value],
    )
    publish_action = max(
        (left.publish_action, right.publish_action),
        key=lambda value: PUBLISH_ACTION_RANK[value],
    )
    if (
        _anchored_repairable_fact_issue(left)
        or _anchored_repairable_fact_issue(right)
    ):
        category = "REPAIR"
        publish_action = "REPAIR_PLAN"
    return left.model_copy(update={
        "category": category,
        "publish_action": publish_action,
        "source": left.source if left.source == "deterministic" else right.source,
        "place_id": left.place_id or right.place_id,
        "names": _dedupe([*left.names, *right.names]),
        "snippet": left.snippet or right.snippet,
        "evidence": left.evidence or right.evidence,
        "tags": _dedupe([*left.tags, *right.tags]),
        "side_effects": _dedupe([*left.side_effects, *right.side_effects]),
        "metadata": {**right.metadata, **left.metadata},
    })


def resolve_generation_issues(
    issues: list[GenerationIssue],
) -> IssueTaxonomyResult:
    merged: dict[tuple, GenerationIssue] = {}
    notes: list[str] = []
    deterministic_blocker_keys = {
        _normalized_issue_key(issue)
        for issue in issues
        if issue.source == "deterministic" and issue.category == "BLOCKER"
    }
    for issue in issues:
        issue = _grade_semantic_issue(issue)
        key = _normalized_issue_key(issue)
        if (
            key in deterministic_blocker_keys
            and issue.source != "deterministic"
            and CATEGORY_RANK[issue.category] < CATEGORY_RANK["BLOCKER"]
        ):
            notes.append(
                f"deterministic BLOCKER preserved for {issue.reason}"
            )
            issue = issue.model_copy(update={
                "category": "BLOCKER",
                "publish_action": max(
                    ("REPAIR_PLAN", issue.publish_action),
                    key=lambda value: PUBLISH_ACTION_RANK[value],
                ),
            })
        merged[key] = (
            _merge_issue(merged[key], issue)
            if key in merged
            else issue
        )

    ordered = sorted(
        merged.values(),
        key=lambda issue: (
            issue.plan_index or 0,
            issue.day or 0,
            -CATEGORY_RANK[issue.category],
            issue.reason,
        ),
    )
    counts = {"BLOCKER": 0, "REPAIR": 0, "WARN": 0, "DATA_GAP": 0}
    repair_targets: list[int] = []
    fail_reasons: list[str] = []
    data_backlog_reasons: list[str] = []
    for issue in ordered:
        counts[issue.category] += 1
        if "DATA_GAP" in issue.tags:
            counts["DATA_GAP"] += 1
        if "DATA_BACKLOG" in issue.side_effects:
            data_backlog_reasons.append(issue.reason)
        if issue.publish_action == "FAIL_CLOSED":
            fail_reasons.append(issue.reason)
        elif (
            issue.publish_action == "REPAIR_PLAN"
            and issue.plan_index is not None
            and issue.plan_index >= 1
        ):
            repair_targets.append(issue.plan_index)
    return IssueTaxonomyResult(
        issues=ordered,
        repair_target_plan_indexes=sorted(set(repair_targets)),
        fail_closed_reasons=_dedupe(fail_reasons),
        data_backlog_reasons=_dedupe(data_backlog_reasons),
        resolver_notes=_dedupe(notes),
        issue_counts=counts,
    )


def summarize_copy_quality_issues(
    issues: list[GenerationIssue],
) -> dict[str, Any]:
    selected = [
        issue for issue in issues
        if issue.reason in COPY_QUALITY_REASONS
    ]
    counts = {"BLOCKER": 0, "REPAIR": 0, "WARN": 0, "DATA_GAP": 0}
    for issue in selected:
        counts[issue.category] += 1
        if "DATA_GAP" in issue.tags:
            counts["DATA_GAP"] += 1
    plan_indexes = sorted({
        issue.plan_index
        for issue in selected
        if issue.plan_index is not None and issue.plan_index >= 1
    })
    soft_unauthorized = [
        issue for issue in selected
        if issue.reason == "unsupported_fact_expansion"
        and issue.publish_action == "RECORD_ONLY"
        and not has_hard_fact_marker(issue.snippet)
        and has_soft_unauthorized_expression_marker(issue.snippet)
    ]
    return {
        "copy_quality_issue_count": len(selected),
        "copy_quality_blocker_count": counts["BLOCKER"],
        "copy_quality_repair_count": counts["REPAIR"],
        "copy_quality_warn_count": counts["WARN"],
        "copy_quality_data_gap_count": counts["DATA_GAP"],
        "soft_unauthorized_expression_count": len(soft_unauthorized),
        "soft_unauthorized_expression_snippets": _dedupe([
            issue.snippet for issue in soft_unauthorized
        ])[:10],
        "copy_quality_reasons": _dedupe([
            issue.reason for issue in selected
        ]),
        "copy_quality_pattern_reasons": _dedupe([
            str(issue.metadata.get("pattern_reason") or "")
            for issue in selected
        ]),
        "copy_quality_plan_indexes": plan_indexes,
        "copy_quality_publish_actions": _dedupe([
            issue.publish_action for issue in selected
        ]),
        "copy_quality_snippets": _dedupe([
            issue.snippet for issue in selected
        ])[:10],
    }


def _anchored_repairable_fact_issue(issue: GenerationIssue) -> bool:
    return (
        issue.reason in {"severe_fact_misleading", "unsupported_fact_expansion"}
        and issue.publish_action == "REPAIR_PLAN"
        and issue.category in {"REPAIR", "BLOCKER"}
        and issue.plan_index is not None
        and (bool(issue.snippet.strip()) or bool(issue.names))
    )


def _grade_semantic_issue(issue: GenerationIssue) -> GenerationIssue:
    """Downgrade soft LLM semantic copy notes to record-only.

    YunTu Review sometimes explains that an unsupported-fact issue is light or
    acceptable, while still returning REPAIR_PLAN. Keep hard, verifiable claims
    repairable, but do not let mild itinerary narration block publishing.
    """
    if issue.source != "llm_review":
        return issue
    if issue.reason != "unsupported_fact_expansion":
        return issue
    fact_policy = (issue.metadata or {}).get("fact_policy")
    if fact_policy == "soft_prose_shadow":
        return issue.model_copy(update={
            "category": "WARN",
            "publish_action": "RECORD_ONLY",
        })
    if (
        fact_policy == "verifiable_hard_fact"
        or has_hard_fact_marker(issue.snippet)
    ):
        updates = {}
        if issue.category == "WARN":
            updates["category"] = "REPAIR"
        if issue.publish_action == "RECORD_ONLY":
            updates["publish_action"] = "REPAIR_PLAN"
        return issue.model_copy(update=updates) if updates else issue

    if issue.publish_action != "REPAIR_PLAN" and issue.category == "WARN":
        if not has_soft_unauthorized_expression_marker(issue.snippet):
            return issue

    # Soft prose and unclassified copy packaging remain observable, but Review
    # cannot promote them into a whole-plan repair/fail-closed path. Only the
    # explicit verifiable-hard-fact taxonomy keeps REPAIR semantics.
    return issue.model_copy(update={
        "category": "WARN",
        "publish_action": "RECORD_ONLY",
    })


def map_publish_findings_to_issues(findings: list[Any]) -> list[GenerationIssue]:
    mapping: dict[str, tuple[IssueCategory, PublishAction]] = {
        "accommodation_fabrication": ("BLOCKER", "FAIL_CLOSED"),
        "accommodation_price_claim": ("BLOCKER", "FAIL_CLOSED"),
        "accommodation_in_route": ("BLOCKER", "FAIL_CLOSED"),
        "route_outside_poi": ("BLOCKER", "FAIL_CLOSED"),
        "cross_day_poi": ("BLOCKER", "FAIL_CLOSED"),
        "city_mismatch": ("BLOCKER", "FAIL_CLOSED"),
        "plan_count_mismatch": ("BLOCKER", "FAIL_CLOSED"),
        "duplicate_day_heading": ("REPAIR", "REPAIR_PLAN"),
        "placeholder_wording": ("REPAIR", "REPAIR_PLAN"),
        "database_tone": ("REPAIR", "REPAIR_PLAN"),
        "rare_character_compatibility": ("REPAIR", "REPAIR_PLAN"),
        "too_short_plan_text": ("REPAIR", "REPAIR_PLAN"),
        "activity_content_missing": ("WARN", "RECORD_ONLY"),
        "food_tier_exceeded": ("REPAIR", "REPAIR_PLAN"),
        "food_none_tier_violation": ("BLOCKER", "FAIL_CLOSED"),
        "food_source_attribution": ("REPAIR", "REPAIR_PLAN"),
    }
    issues: list[GenerationIssue] = []
    for finding in findings:
        reason = getattr(finding, "reason", "")
        category, action = mapping.get(reason, ("WARN", "RECORD_ONLY"))
        issues.append(GenerationIssue(
            source="publish_gate",
            category=category,
            publish_action=action,
            reason=reason,
            plan_index=getattr(finding, "plan_index", None),
            day=getattr(finding, "day", None),
            snippet=getattr(finding, "snippet", ""),
            evidence=getattr(finding, "message", ""),
        ))
    return issues


def _route_violation_to_issue(violation: dict[str, Any]) -> GenerationIssue:
    reason = str(violation.get("reason") or "route_violation")
    action: PublishAction = (
        "FAIL_CLOSED" if reason in NON_REPAIRABLE_REASONS else "REPAIR_PLAN"
    )
    return GenerationIssue(
        source="deterministic",
        category="BLOCKER",
        publish_action=action,
        reason=reason,
        plan_index=violation.get("plan_index"),
        day=violation.get("day"),
        names=_violation_names(violation),
        evidence=str(violation),
        metadata=violation,
    )


def _violation_names(violation: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for key in ("names", "actual", "expected"):
        value = violation.get(key)
        if not isinstance(value, list):
            continue
        names.extend(str(name) for name in value)
    return _dedupe(names)


def _budget_issues(
    budget_results: list[BudgetResult] | None,
) -> list[GenerationIssue]:
    if not budget_results:
        return []
    issues: list[GenerationIssue] = []
    for plan_index, budget_result in enumerate(budget_results, 1):
        for day in budget_result.days:
            if day.status == "infeasible":
                issues.append(GenerationIssue(
                    source="deterministic",
                    category="BLOCKER",
                    publish_action="FAIL_CLOSED",
                    reason="budget_infeasible",
                    plan_index=plan_index,
                    day=day.day,
                    evidence=day.reason,
                    metadata=day.model_dump(mode="json"),
                ))
            elif (
                day.commute_minutes > day.budget_minutes
                and day.status != "relaxed_exception"
            ):
                issues.append(GenerationIssue(
                    source="deterministic",
                    category="BLOCKER",
                    publish_action="FAIL_CLOSED",
                    reason="budget_overrun_without_exception",
                    plan_index=plan_index,
                    day=day.day,
                    evidence=day.reason,
                    metadata=day.model_dump(mode="json"),
                ))
    return issues


def _evidence_text(retrieval: RetrievalResult) -> str:
    chunks: list[str] = []
    for candidate in retrieval.candidates:
        chunks.append(candidate.name)
        for raw in [*candidate.top_reasons, *candidate.warnings]:
            if isinstance(raw, dict):
                chunks.extend(str(value) for value in raw.values() if value)
    return "\n".join(chunks)


def _unsupported_fact_issues(
    plans: list[PlanOutput],
    retrieval: RetrievalResult,
    attachment_auth_map: FoodAttachmentAuthMap | None = None,
) -> list[GenerationIssue]:
    evidence = _evidence_text(retrieval)
    issues: list[GenerationIssue] = []
    for plan_index, plan in enumerate(plans, 1):
        classifier_text = mask_authorized_food_prose(
            plan.plan_text,
            attachment_auth_map,
            plan_index=plan_index - 1,
        )
        if plan.accommodation is not None or plan.transport is not None:
            classifier_text = mask_accommodation_prefix_for_route_scan(
                classifier_text
            )
        for fact_reason, match in unsupported_fact_pattern_matches(classifier_text):
            snippet = match.group(0)
            if snippet and snippet in evidence:
                continue
            hard_fact = has_hard_fact_marker(snippet)
            fragment = fragment_for_span(
                plan,
                start=match.start(),
                end=match.end(),
            )
            action_key = (
                {
                    "plan_index": fragment.plan_index,
                    "day": fragment.day,
                    "place_id": fragment.place_id,
                }
                if fragment is not None
                else None
            )
            issues.append(GenerationIssue(
                source="deterministic",
                category="REPAIR" if hard_fact else "WARN",
                publish_action="REPAIR_PLAN" if hard_fact else "RECORD_ONLY",
                reason="unsupported_fact_expansion",
                plan_index=plan_index,
                day=fragment.day if fragment is not None else None,
                place_id=fragment.place_id if fragment is not None else None,
                snippet=snippet,
                evidence=f"deterministic pattern: {fact_reason}",
                tags=["DATA_GAP"] if hard_fact else [],
                side_effects=["DATA_BACKLOG"] if hard_fact else [],
                metadata={
                    "pattern_reason": fact_reason,
                    "pattern_start": match.start(),
                    "pattern_end": match.end(),
                    **(
                        {
                            "surface": "poi_fragment",
                            "review_anchored": True,
                            "action_contract_key": action_key,
                        }
                        if action_key is not None
                        else {}
                    ),
                    "fact_policy": (
                        "verifiable_hard_fact"
                        if hard_fact
                        else "soft_prose_shadow"
                    ),
                },
            ))
    return issues


def _unsupported_meal_role_issues(
    plans: list[PlanOutput],
    composition_blueprints: list[CompositionBlueprint] | None,
) -> list[GenerationIssue]:
    if not composition_blueprints:
        return []
    issues: list[GenerationIssue] = []
    for plan_index, plan in enumerate(plans, 1):
        blueprint = (
            composition_blueprints[plan_index - 1]
            if plan_index - 1 < len(composition_blueprints)
            else None
        )
        if blueprint is None:
            continue
        text_segments = [
            segment
            for segment in re.split(r"(?<=[。！？；;\n])", plan.plan_text)
            if segment.strip()
        ]
        for day in blueprint.days:
            for stop in day.stops:
                if stop.role not in {"coffee_stop", "snack_stop"}:
                    continue
                for segment in text_segments:
                    if stop.name not in segment:
                        continue
                    if not any(marker in segment for marker in MEAL_ROLE_MARKERS):
                        continue
                    issues.append(GenerationIssue(
                        source="deterministic",
                        category="REPAIR",
                        publish_action="REPAIR_PLAN",
                        reason="unsupported_meal_role",
                        plan_index=plan_index,
                        day=day.day,
                        names=[stop.name],
                        snippet=segment.strip()[:200],
                        evidence=(
                            f"{stop.role} with meal_slot={stop.meal_slot} "
                            "was framed as a full meal"
                        ),
                        tags=["DATA_GAP"],
                        side_effects=["DATA_BACKLOG"],
                    ))
                    break
    return issues


def _food_evidence_tiers(
    composition_blueprints: list[CompositionBlueprint] | None,
) -> dict[FoodAttachmentKey, str]:
    tiers: dict[FoodAttachmentKey, str] = {}
    for plan_index, blueprint in enumerate(composition_blueprints or []):
        for day in blueprint.days:
            for stop in day.stops:
                hint = stop.writing_hint
                if not isinstance(hint, dict) or not hint.get("evidence_tier"):
                    continue
                try:
                    key: FoodAttachmentKey = (
                        plan_index,
                        int(day.day),
                        int(stop.place_id),
                        str(hint.get("meal_slot") or stop.meal_slot or ""),
                        int(hint.get("food_place_id")),
                    )
                except (TypeError, ValueError):
                    continue
                tiers[key] = str(hint["evidence_tier"])
    return tiers


def _food_review_issues(
    plans: list[PlanOutput],
    composition_blueprints: list[CompositionBlueprint] | None,
    attachment_auth_map: FoodAttachmentAuthMap | None,
) -> list[GenerationIssue]:
    if not attachment_auth_map:
        return []
    tiers = _food_evidence_tiers(composition_blueprints)
    issues: list[GenerationIssue] = []
    for zero_index, plan in enumerate(plans):
        for violation in find_food_rule_violations(
            plan.plan_text,
            attachment_auth_map,
            plan_index=zero_index,
            evidence_tier_by_key=tiers,
        ):
            authorization = attachment_auth_map.get(violation.key)
            issues.append(GenerationIssue(
                source="deterministic",
                category=(
                    "BLOCKER"
                    if violation.reason == "food_none_tier_violation"
                    else "REPAIR"
                ),
                publish_action=(
                    "FAIL_CLOSED"
                    if violation.reason == "food_none_tier_violation"
                    else "REPAIR_PLAN"
                ),
                reason=violation.reason,
                plan_index=zero_index + 1,
                day=violation.key[1],
                place_id=violation.key[2],
                names=(
                    [authorization.food_name]
                    if authorization is not None and authorization.food_name
                    else []
                ),
                snippet=violation.snippet,
                evidence=(
                    "none-tier food prose must exactly equal none_tier_text"
                    if violation.reason == "food_none_tier_violation"
                    else "deterministic Food Review rule"
                ),
                metadata={
                    "surface": "narrative_sentence",
                    "start": violation.start,
                    "end": violation.end,
                    "attachment_key": {
                        "plan_index": violation.key[0],
                        "day": violation.key[1],
                        "anchor_place_id": violation.key[2],
                        "meal_slot": violation.key[3],
                        "food_place_id": violation.key[4],
                    },
                    "evidence_tier": tiers.get(violation.key)
                    or (
                        authorization.evidence_tier
                        if authorization is not None
                        else ""
                    ),
                },
            ))
    return issues


def repair_food_review_plan(
    plan: PlanOutput,
    *,
    plan_index: int,
    attachment_auth_map: FoodAttachmentAuthMap | None,
    issues: list[GenerationIssue],
) -> PlanRepairResult:
    """Apply the two repairable food rules to one plan deterministically."""

    repair_reasons = {
        issue.reason
        for issue in issues
        if issue.plan_index == plan_index
        and issue.reason in {
            "food_tier_exceeded",
            "food_source_attribution",
        }
    }
    if not repair_reasons or not attachment_auth_map:
        return PlanRepairResult(
            repaired_plans=[plan],
            repaired_plan_indexes=[],
            success=False,
            failure_reasons=["no_repairable_food_issue"],
        )
    violations = [
        violation
        for violation in find_food_rule_violations(
            plan.plan_text,
            attachment_auth_map,
            plan_index=plan_index - 1,
        )
        if violation.reason in repair_reasons
    ]
    updated_text = repair_food_plan_text(
        plan.plan_text,
        attachment_auth_map,
        violations,
    )
    if updated_text == plan.plan_text:
        return PlanRepairResult(
            repaired_plans=[plan],
            repaired_plan_indexes=[],
            success=False,
            failure_reasons=["food_repair_made_no_change"],
        )
    remapped = remap_fragment_registry(plan, updated_text=updated_text)
    if remapped is None:
        return PlanRepairResult(
            repaired_plans=[plan],
            repaired_plan_indexes=[],
            success=False,
            failure_reasons=["food_repair_fragment_registry_invalidated"],
        )
    return PlanRepairResult(
        repaired_plans=[remapped],
        repaired_plan_indexes=[plan_index],
        success=True,
        metrics={
            "food_repair_reasons": sorted(repair_reasons),
            "food_repair_count": len(violations),
        },
    )


def _blueprint_integrity_issues(
    plans: list[PlanOutput],
    route_plans: list[RoutePlan],
    composition_blueprints: list[CompositionBlueprint] | None,
) -> list[GenerationIssue]:
    issues: list[GenerationIssue] = []
    for violation in check_plan_blueprint_integrity(
        plans,
        route_plans,
        composition_blueprints,
    ):
        # Keep the underlying blueprint reason as the issue reason so structural
        # order/membership findings stay out of REPAIR_PLAN / repair targets.
        reason = violation.reason or "blueprint_integrity_violation"
        action: PublishAction = (
            "FAIL_CLOSED" if reason in NON_REPAIRABLE_REASONS else "REPAIR_PLAN"
        )
        issues.append(GenerationIssue(
            source="deterministic",
            category="BLOCKER",
            publish_action=action,
            reason=reason,
            plan_index=violation.plan_index,
            day=violation.day,
            names=[*violation.missing_stops, *violation.extra_stops],
            evidence=violation.reason,
            metadata={
                "blueprint_reason": violation.reason,
                "missing_stops": violation.missing_stops,
                "extra_stops": violation.extra_stops,
            },
        ))
    return issues


def collect_deterministic_generation_issues(
    plans: list[PlanOutput],
    *,
    trip_request: TripRequest,
    retrieval: RetrievalResult,
    route_plans: list[RoutePlan],
    budget_results: list[BudgetResult] | None = None,
    composition_blueprints: list[CompositionBlueprint] | None = None,
    weather_advisory_payload: WeatherAdvisoryPayload | None = None,
    attachment_auth_map: FoodAttachmentAuthMap | None = None,
    accommodation: AccommodationSuggestion | None = None,
) -> list[GenerationIssue]:
    from src.agents import publish_gate

    issues = [
        _route_violation_to_issue(violation)
        for violation in route_plan_violations(plans, route_plans)
    ]
    issues.extend(_budget_issues(budget_results))
    issues.extend(_unsupported_fact_issues(
        plans,
        retrieval,
        attachment_auth_map,
    ))
    issues.extend(_unsupported_meal_role_issues(plans, composition_blueprints))
    issues.extend(
        _food_review_issues(
            plans,
            composition_blueprints,
            attachment_auth_map,
        )
    )
    issues.extend(
        _blueprint_integrity_issues(plans, route_plans, composition_blueprints)
    )
    issues.extend(
        weather_review_issues(
            plans,
            route_plans=route_plans,
            payload=weather_advisory_payload,
        )
    )
    publish_result = publish_gate.check_publish_gate(
        plans,
        trip_request=trip_request,
        retrieval=retrieval,
        route_plans=route_plans,
        attachment_auth_map=attachment_auth_map,
        accommodation=accommodation,
    )
    issues.extend(map_publish_findings_to_issues(publish_result.findings))
    return issues


def body_review_snapshot(payload: dict) -> str:
    """Stable fingerprint of one review context. Not a secret."""
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def review_issue_id(issue: GenerationIssue) -> str:
    raw = json.dumps(issue.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]
