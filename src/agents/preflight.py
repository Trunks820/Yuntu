"""Preflight phrase spans and deterministic pre-dispatch handlers for O3."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from src.agents.final_writer import (
    _deduplicate_day_headings,
    _ensure_locked_route_text,
    _merge_locked_place_fields,
)
from src.agents.generation_issues import GenerationIssue
from src.agents.publish_gate import (
    PLACEHOLDER_PHRASES,
    _day_heading_numbers,
    replace_unsafe_transit_copy,
)
from src.agents.poi_fragments import remap_fragment_registry
from src.agents.route_planning import authorized_commute_mask_strings
from src.agents.schema import (
    CompositionBlueprint,
    PlanOutput,
    RoutePlan,
)
from src.agents.text_quality import (
    BANNED_DATABASE_PHRASES,
    find_phrase_spans,
    strip_rare_cjk_characters,
)


@dataclass(frozen=True)
class PreflightFinding:
    reason: str
    plan_index: int
    day: int | None
    surface: str
    start: int
    end: int
    phrase: str
    snippet: str

    def to_issue(self) -> GenerationIssue:
        return GenerationIssue(
            source="publish_gate",
            category="REPAIR",
            publish_action="REPAIR_PLAN",  # observation only under O3
            reason=self.reason,
            plan_index=self.plan_index,
            day=self.day,
            snippet=self.snippet,
            evidence=f"{self.reason}:{self.phrase}",
            metadata={
                "surface": self.surface,
                "start": self.start,
                "end": self.end,
                "phrase": self.phrase,
            },
        )


@dataclass
class HandlerResult:
    plans: list[PlanOutput]
    residual_findings: list[GenerationIssue] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    changed: bool = False


_FULL_MEAL_MARKERS = ("午餐", "晚餐", "正餐", "吃饭", "用餐")


def _normalize_light_food_roles(
    text: str,
    blueprint: CompositionBlueprint | None,
) -> tuple[str, list[str]]:
    """Rewrite coffee/snack full-meal claims to backend-authorized actions."""
    if not text or blueprint is None:
        return text, []
    normalized = text
    notes: list[str] = []
    for day in blueprint.days:
        for stop in day.stops:
            if stop.role not in {"coffee_stop", "snack_stop"} or not stop.name:
                continue
            pattern = re.compile(
                rf"[^。！？；;\n]*{re.escape(stop.name)}[^。！？；;\n]*[。！？；;]?"
            )
            for match in list(pattern.finditer(normalized)):
                segment = match.group(0)
                if not any(marker in segment for marker in _FULL_MEAL_MARKERS):
                    continue
                replacement = (
                    f"到{stop.name}喝杯咖啡或茶歇休息。"
                    if stop.role == "coffee_stop"
                    else f"到{stop.name}作为小吃或甜品补给，短暂停留。"
                )
                normalized = (
                    normalized[:match.start()]
                    + replacement
                    + normalized[match.end():]
                )
                notes.append(f"day{day.day}:{stop.name}:{stop.role}")
                break
    return normalized, notes


def _day_for_offset(text: str, offset: int) -> int | None:
    headings = _day_heading_numbers(text)
    if not headings:
        return None
    current: int | None = None
    for day, start, _end in headings:
        if start <= offset:
            current = day
        else:
            break
    return current


def _surface_for_offset(
    text: str,
    start: int,
    end: int,
    *,
    authorized_commute_spans: list[tuple[int, int]] | None = None,
) -> str:
    headings = _day_heading_numbers(text)
    for _day, h_start, h_end in headings:
        if start < h_end and h_start < end:
            return "day_heading"
    for c_start, c_end in authorized_commute_spans or []:
        if start < c_end and c_start < end:
            return "authorized_commute_span"
    # Default residual prose to narrative_sentence for first-slice FR admission.
    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", start)
    if line_end < 0:
        line_end = len(text)
    line = text[line_start:line_end]
    stripped = line.strip()
    if stripped.startswith(("-", "•", "·")) and len(stripped) < 40:
        return "poi_name_line"
    return "narrative_sentence"


def _authorized_commute_spans_for_plan(
    text: str,
    route_plan: RoutePlan | None,
) -> list[tuple[int, int]]:
    if route_plan is None or not text:
        return []
    spans: list[tuple[int, int]] = []
    for value in authorized_commute_mask_strings(route_plan):
        if not value:
            continue
        start = 0
        while True:
            index = text.find(value, start)
            if index < 0:
                break
            spans.append((index, index + len(value)))
            start = index + len(value)
    return spans


def collect_phrase_preflight(
    plans: list[PlanOutput],
    *,
    route_plans: list[RoutePlan] | None = None,
) -> list[PreflightFinding]:
    """Collect span-qualified database_tone / placeholder_wording findings."""
    findings: list[PreflightFinding] = []
    for plan_index, plan in enumerate(plans, 1):
        text = plan.plan_text or ""
        route = None
        if route_plans is not None and plan_index - 1 < len(route_plans):
            route = route_plans[plan_index - 1]
        commute_spans = _authorized_commute_spans_for_plan(text, route)
        for phrase, start, end in find_phrase_spans(text, BANNED_DATABASE_PHRASES):
            findings.append(PreflightFinding(
                reason="database_tone",
                plan_index=plan_index,
                day=_day_for_offset(text, start),
                surface=_surface_for_offset(
                    text,
                    start,
                    end,
                    authorized_commute_spans=commute_spans,
                ),
                start=start,
                end=end,
                phrase=phrase,
                snippet=text[start:end],
            ))
        for phrase, start, end in find_phrase_spans(text, PLACEHOLDER_PHRASES):
            findings.append(PreflightFinding(
                reason="placeholder_wording",
                plan_index=plan_index,
                day=_day_for_offset(text, start),
                surface=_surface_for_offset(
                    text,
                    start,
                    end,
                    authorized_commute_spans=commute_spans,
                ),
                start=start,
                end=end,
                phrase=phrase,
                snippet=text[start:end],
            ))
    return findings


def run_predispatch_handlers(
    plans: list[PlanOutput],
    route_plans: list[RoutePlan],
) -> HandlerResult:
    """Apply deterministic cleanups without invalidating stable POI ownership."""
    updated: list[PlanOutput] = []
    residuals: list[GenerationIssue] = []
    notes: list[str] = []
    changed = False

    for index, plan in enumerate(plans):
        route = route_plans[index] if index < len(route_plans) else None
        text = plan.plan_text or ""

        text, light_food_notes = _normalize_light_food_roles(
            text,
            plan.composition_blueprint,
        )
        if light_food_notes:
            changed = True
            notes.append(
                f"plan{index + 1}: normalized light-food roles "
                + ",".join(light_food_notes)
            )

        # 1) rare CJK
        cleaned, removed = strip_rare_cjk_characters(text)
        if removed:
            text = cleaned
            changed = True
            notes.append(f"plan{index + 1}: stripped rare cjk x{len(removed)}")
            # Residual if still present
            still, _ = strip_rare_cjk_characters(text)
            if still != text:
                residuals.append(GenerationIssue(
                    source="deterministic",
                    category="REPAIR",
                    publish_action="REPAIR_PLAN",
                    reason="rare_character_compatibility",
                    plan_index=index + 1,
                    snippet="".join(removed[:3]),
                ))

        # 2) duplicate day headings
        deduped = _deduplicate_day_headings(text)
        if deduped != text:
            text = deduped
            changed = True
            notes.append(f"plan{index + 1}: deduplicated day headings")
        # Residual duplicate headings
        seen: set[int] = set()
        for day, _s, _e in _day_heading_numbers(text):
            if day in seen:
                residuals.append(GenerationIssue(
                    source="deterministic",
                    category="REPAIR",
                    publish_action="REPAIR_PLAN",
                    reason="duplicate_day_heading",
                    plan_index=index + 1,
                    day=day,
                ))
                break
            seen.add(day)

        plan_out = plan.model_copy(update={"plan_text": text})
        if route is not None:
            plan_out = replace_unsafe_transit_copy(plan_out, route)
            if plan_out.plan_text != text:
                changed = True
                notes.append(f"plan{index + 1}: replaced unsafe transit copy")
            plan_out = plan_out.model_copy(update={
                "plan_text": _ensure_locked_route_text(
                    plan_out.plan_text,
                    route,
                    plan_out.composition_blueprint,
                )
            })
        registry_safe = remap_fragment_registry(
            plan,
            updated_text=plan_out.plan_text,
        )
        if registry_safe is None:
            residuals.append(GenerationIssue(
                source="deterministic",
                category="BLOCKER",
                publish_action="FAIL_CLOSED",
                reason="fragment_registry_invalidated",
                plan_index=index + 1,
                evidence=(
                    "predispatch whole-text cleanup crossed a stable POI "
                    "fragment boundary"
                ),
                metadata={"contract_family": "STRUCTURE_INVARIANT"},
            ))
            notes.append(
                f"plan{index + 1}: keyed fragment registry remap rejected"
            )
            updated.append(plan)
            continue
        plan_out = registry_safe
        if route is not None:
            plan_out = _merge_locked_place_fields(
                plan_name=plan_out.plan_name,
                plan_text=plan_out.plan_text,
                route_plan=route,
                composition_blueprint=plan_out.composition_blueprint,
                poi_identity_result=plan_out.poi_identity_result,
                budget_result=plan_out.budget_result,
                poi_fragments=plan_out.poi_fragments,
                summary=plan_out.summary,
                accommodation=plan_out.accommodation,
                transport=plan_out.transport,
                packing_checklist=plan_out.packing_checklist,
                travel_tips=plan_out.travel_tips,
                code_lodging_section=plan_out.code_lodging_section,
            )
        updated.append(plan_out)

    return HandlerResult(
        plans=updated,
        residual_findings=residuals,
        notes=notes,
        changed=changed,
    )
