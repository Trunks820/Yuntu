"""Deterministic last-resort renderer for locked itinerary inputs.

The renderer deliberately accepts only route structure plus the deterministic
Action Contract.  It has no Provider, prompt, evidence, Writer-copy, Review-copy
or network dependency.
"""

from __future__ import annotations

from src.agents.route_feasibility import access_summary

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any, Iterable

from src.agents.composition_blueprint import commute_style
from src.agents.evidence_strength import DeterministicActionContract
from src.agents.food_resolver import FoodAttachmentAuthorization
from src.agents.schema import (
    EffectiveCommuteMode,
    PlanOutput,
    PoiNarrativeFragment,
    RoutePlan,
    TripRequest,
)


class SafeRenderError(RuntimeError):
    """The deterministic fallback could not construct a complete candidate."""


@dataclass(frozen=True, slots=True)
class LockedSafeActionContract:
    """One locked route stop stripped to Safe-render-authorized material."""

    place_id: int
    place_name: str
    blueprint_role: str
    authorized_actions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LockedSafeCommuteTemplate:
    """One route-owned deterministic commute expression."""

    from_place_id: int
    to_place_id: int
    mode: EffectiveCommuteMode
    authorized_text: str


@dataclass(frozen=True, slots=True)
class LockedSafeDay:
    day: int
    places: tuple[LockedSafeActionContract, ...]
    commute_templates: tuple[LockedSafeCommuteTemplate, ...] = ()
    accommodation_access_summary: str = ""
    preference_notices: tuple[str, ...] = ()
    program_hints: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class LockedSafePlan:
    plan_index: int
    days: tuple[LockedSafeDay, ...]
    trip_notices: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class LockedSafeFoodTemplate:
    """One exact deterministic food sentence allowed at the Safe boundary."""

    plan_index: int
    day: int
    anchor_place_id: int
    meal_slot: str
    food_place_id: int
    span_marker: str
    authorized_text: str

    @property
    def key(self) -> tuple[int, int, int, str, int]:
        return (
            self.plan_index,
            self.day,
            self.anchor_place_id,
            self.meal_slot,
            self.food_place_id,
        )


@dataclass(frozen=True, slots=True)
class LockedSafeInput:
    """Frozen Safe boundary; it cannot carry evidence or generated prose."""

    plans: tuple[LockedSafePlan, ...]
    food_templates: tuple[LockedSafeFoodTemplate, ...] = ()


@dataclass(frozen=True)
class SafePlanRenderResult:
    plans: list[PlanOutput]
    latency_ms: int
    input_sha256: str
    output_sha256: str


def _sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _clean_authorized_action(value: str) -> str:
    action = " ".join(str(value or "").split()).strip("。；;，, ")
    if not action:
        raise SafeRenderError("empty_authorized_action")
    if "\n" in action or "\r" in action:
        raise SafeRenderError("invalid_authorized_action")
    return action


def _clean_authorized_food_text(value: str | None) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        raise SafeRenderError("missing_authorized_food_text")
    if "<!--" in text or "-->" in text:
        raise SafeRenderError("invalid_authorized_food_text")
    return text


def _clean_meal_slot(value: str) -> str:
    meal_slot = str(value or "").strip()
    if (
        not meal_slot
        or not meal_slot.replace("_", "").replace("-", "").isalnum()
    ):
        raise SafeRenderError("invalid_food_meal_slot")
    return meal_slot


def _food_span_marker(
    *,
    plan_index: int,
    day: int,
    anchor_place_id: int,
    meal_slot: str,
    food_place_id: int,
) -> str:
    return (
        f"<!-- food:plan{plan_index}_day{day}_anchor{anchor_place_id}"
        f"_slot{meal_slot}_food{food_place_id} -->"
    )


def lock_safe_input(
    *,
    route_plans: list[RoutePlan],
    action_contracts: Iterable[DeterministicActionContract],
    food_authorizations: Iterable[FoodAttachmentAuthorization] = (),
    trip_request: TripRequest | None = None,
) -> LockedSafeInput:
    """Narrow route/action/food contracts at the Safe trust boundary."""
    if not route_plans:
        raise SafeRenderError("locked_inputs_missing")

    action_contracts = tuple(action_contracts)
    contracts = {contract.key: contract for contract in action_contracts}
    if len(contracts) != len(action_contracts):
        raise SafeRenderError("duplicate_action_contract_key")

    seen_keys: set[tuple[int, int, int]] = set()
    locked_plans: list[LockedSafePlan] = []
    from src.agents.preference_notices import build_preference_notices, program_day_hints
    for plan_index, route_plan in enumerate(route_plans, 1):
        notices = (
            build_preference_notices(trip_request, route_plan)
            if trip_request is not None else None
        )
        if not route_plan.day_groups:
            raise SafeRenderError("empty_locked_plan")
        seen_days: set[int] = set()
        locked_days: list[LockedSafeDay] = []
        for day_group in route_plan.day_groups:
            if day_group.day in seen_days:
                raise SafeRenderError("duplicate_locked_day")
            seen_days.add(day_group.day)
            if not day_group.places:
                raise SafeRenderError("empty_locked_day")
            locked_places: list[LockedSafeActionContract] = []
            for place in day_group.places:
                key = (plan_index, day_group.day, place.place_id)
                if key in seen_keys:
                    raise SafeRenderError("duplicate_locked_place_key")
                seen_keys.add(key)
                contract = contracts.get(key)
                if contract is None or contract.place_name != place.name:
                    raise SafeRenderError("action_contract_mismatch")
                actions = sorted({
                    _clean_authorized_action(action)
                    for action in contract.authorized_actions
                    if str(action or "").strip()
                })
                if contract.blueprint_role != "transfer_context" and not actions:
                    raise SafeRenderError("missing_authorized_action")
                locked_places.append(LockedSafeActionContract(
                    place_id=place.place_id,
                    place_name=place.name,
                    blueprint_role=contract.blueprint_role,
                    authorized_actions=tuple(actions),
                ))
            adjacent_places = {
                (left.place_id, right.place_id): (left.name, right.name)
                for left, right in zip(
                    day_group.places,
                    day_group.places[1:],
                    strict=False,
                )
            }
            seen_commute_keys: set[tuple[int, int]] = set()
            locked_commute_templates: list[LockedSafeCommuteTemplate] = []
            for leg in day_group.commute_legs:
                commute_key = (leg.from_place_id, leg.to_place_id)
                expected_names = adjacent_places.get(commute_key)
                if commute_key in seen_commute_keys:
                    raise SafeRenderError("duplicate_commute_template_key")
                seen_commute_keys.add(commute_key)
                if expected_names != (leg.from_name, leg.to_name):
                    raise SafeRenderError("commute_template_route_mismatch")
                style = commute_style(
                    int(leg.duration_minutes or 0),
                    leg.mode,
                )
                if style not in {"long_transfer", "remote_transfer"}:
                    continue
                mode_label = {
                    "driving": "驾车",
                    "transit": "公共交通",
                    "walking": "步行",
                    "cycling": "骑行",
                }[leg.mode]
                duration_minutes = int(leg.duration_minutes or 0)
                authorized_text = (
                    f"{leg.from_name}→{leg.to_name}，公共交通约 "
                    f"{duration_minutes} 分钟"
                    if leg.mode == "transit"
                    else (
                        f"{leg.from_name}→{leg.to_name}，{mode_label}预计 "
                        f"{duration_minutes} 分钟"
                    )
                )
                locked_commute_templates.append(LockedSafeCommuteTemplate(
                    from_place_id=leg.from_place_id,
                    to_place_id=leg.to_place_id,
                    mode=leg.mode,
                    authorized_text=authorized_text,
                ))
            locked_days.append(LockedSafeDay(
                day=day_group.day,
                places=tuple(locked_places),
                commute_templates=tuple(locked_commute_templates),
                accommodation_access_summary=access_summary(day_group) if route_plan.route_policy_version == "selector-route-v2" else "",
                preference_notices=(
                    (notices.day_lines or {}).get(int(day_group.day), ())
                    if notices is not None else ()
                ),
                program_hints=tuple(program_day_hints(day_group)),
            ))
        locked_plans.append(LockedSafePlan(
            plan_index=plan_index,
            days=tuple(locked_days),
            trip_notices=notices.trip_lines if notices is not None else (),
        ))
    route_place_keys = {
        (plan_index, day.day, place.place_id)
        for plan_index, route_plan in enumerate(route_plans)
        for day in route_plan.day_groups
        for place in day.places
    }
    locked_food_templates: dict[
        tuple[int, int, int, str, int],
        LockedSafeFoodTemplate,
    ] = {}
    for authorization in food_authorizations:
        if authorization.evidence_tier != "none":
            continue
        meal_slot = _clean_meal_slot(authorization.meal_slot)
        key = (
            authorization.plan_index,
            authorization.day,
            authorization.anchor_place_id,
            meal_slot,
            authorization.food_place_id,
        )
        if key in locked_food_templates:
            raise SafeRenderError("duplicate_food_template_key")
        if key[:3] not in route_place_keys:
            raise SafeRenderError("food_template_anchor_mismatch")
        locked_food_templates[key] = LockedSafeFoodTemplate(
            plan_index=authorization.plan_index,
            day=authorization.day,
            anchor_place_id=authorization.anchor_place_id,
            meal_slot=meal_slot,
            food_place_id=authorization.food_place_id,
            span_marker=_food_span_marker(
                plan_index=authorization.plan_index,
                day=authorization.day,
                anchor_place_id=authorization.anchor_place_id,
                meal_slot=meal_slot,
                food_place_id=authorization.food_place_id,
            ),
            authorized_text=_clean_authorized_food_text(
                authorization.none_tier_text
            ),
        )
    return LockedSafeInput(
        plans=tuple(locked_plans),
        food_templates=tuple(
            locked_food_templates[key]
            for key in sorted(locked_food_templates)
        ),
    )


def _normalized_contract(locked_input: LockedSafeInput) -> list[dict[str, Any]]:
    return [
        {
            "plan_index": plan.plan_index,
            **({"trip_notices": list(plan.trip_notices)} if plan.trip_notices else {}),
            "days": [
                {
                    "day": day.day,
                    **({"accommodation_access_summary": day.accommodation_access_summary} if day.accommodation_access_summary else {}),
                    **({"preference_notices": list(day.preference_notices)} if day.preference_notices else {}),
                    **({"program_hints": list(day.program_hints)} if day.program_hints else {}),
                    "places": [
                        {
                            "place_id": place.place_id,
                            "place_name": place.place_name,
                            "blueprint_role": place.blueprint_role,
                            "authorized_actions": list(place.authorized_actions),
                        }
                        for place in day.places
                    ],
                    "commute_templates": [
                        {
                            "from_place_id": template.from_place_id,
                            "to_place_id": template.to_place_id,
                            "mode": template.mode,
                            "authorized_text": template.authorized_text,
                        }
                        for template in day.commute_templates
                    ],
                }
                for day in plan.days
            ],
            "food_templates": [
                {
                    "plan_index": template.plan_index,
                    "day": template.day,
                    "anchor_place_id": template.anchor_place_id,
                    "meal_slot": template.meal_slot,
                    "food_place_id": template.food_place_id,
                    "span_marker": template.span_marker,
                    "authorized_text": template.authorized_text,
                }
                for template in locked_input.food_templates
                if template.plan_index == plan.plan_index - 1
            ],
        }
        for plan in locked_input.plans
    ]


def render_safe_plans(
    *,
    locked_input: LockedSafeInput,
) -> SafePlanRenderResult:
    """Render byte-stable plan copy from frozen, authorized inputs only."""
    started = time.monotonic()
    normalized = _normalized_contract(locked_input)
    if not normalized:
        raise SafeRenderError("locked_inputs_missing")
    plans: list[PlanOutput] = []
    for plan_data in normalized:
        plan_index = int(plan_data["plan_index"])
        food_templates_by_anchor: dict[tuple[int, int], list[dict[str, Any]]] = {}
        for template in plan_data["food_templates"]:
            anchor = (
                int(template["day"]),
                int(template["anchor_place_id"]),
            )
            food_templates_by_anchor.setdefault(anchor, []).append(template)
        text_parts: list[str] = []
        fragments: list[PoiNarrativeFragment] = []
        day_place_names: list[list[str]] = []
        used_place_ids: list[int] = []
        used_place_names: list[str] = []
        trip_notices = [
            str(line).strip()
            for line in plan_data.get("trip_notices") or []
            if str(line).strip()
        ]
        if trip_notices:
            text_parts.append("\n".join(trip_notices) + "\n\n")

        for day_data in plan_data["days"]:
            day = int(day_data["day"])
            text_parts.append(f"Day {day}\n")
            if day_data.get("accommodation_access_summary"):
                text_parts.append(str(day_data["accommodation_access_summary"]) + "。\n")
            for line in day_data.get("preference_notices") or []:
                text_parts.append(str(line) + "\n")
            for line in day_data.get("program_hints") or []:
                text_parts.append(str(line) + "\n")
            day_names: list[str] = []
            commute_templates = {
                (
                    int(template["from_place_id"]),
                    int(template["to_place_id"]),
                ): template
                for template in day_data["commute_templates"]
            }
            for place_offset, place_data in enumerate(day_data["places"]):
                place_id = int(place_data["place_id"])
                place_name = str(place_data["place_name"])
                role = str(place_data["blueprint_role"] or "")
                if place_offset:
                    previous_place = day_data["places"][place_offset - 1]
                    commute_template = commute_templates.get((
                        int(previous_place["place_id"]),
                        place_id,
                    ))
                    if commute_template is None:
                        text_parts.append("随后按既定路线前往下一站。\n")
                    else:
                        text_parts.append(
                            f'通勤参考：{commute_template["authorized_text"]}。'
                            "这段路程较远，请预留通勤时间。\n"
                        )
                if role == "transfer_context":
                    sentence = f"到{place_name}后，按既定安排完成转接。"
                else:
                    usable_actions = [
                        str(a) for a in place_data["authorized_actions"]
                        if str(a) and not str(a).startswith("[INTERNAL")
                    ]
                    if usable_actions:
                        action = usable_actions[0]
                    else:
                        action = "选择感兴趣的部分游览，按体力决定参观范围"
                    sentence = f"到{place_name}后，{action}。"
                start = sum(len(part) for part in text_parts)
                text_parts.append(sentence)
                end = start + len(sentence)
                text_parts.append("\n")
                for template in food_templates_by_anchor.get((day, place_id), []):
                    text_parts.append(
                        f'{template["span_marker"]}'
                        f'{template["authorized_text"]}'
                        "<!-- /food -->\n"
                    )
                fragments.append(PoiNarrativeFragment(
                    plan_index=plan_index,
                    day=day,
                    place_id=place_id,
                    text=sentence,
                    start=start,
                    end=end,
                    source="safe_renderer",
                ))
                day_names.append(place_name)
                used_place_ids.append(place_id)
                used_place_names.append(place_name)
            text_parts.append(
                "当天仅安排以上地点，依次完成对应活动；地点之间沿既定路线前往，"
                "途中可按自身节奏短暂休息，完成后结束当天行程。\n"
            )
            day_place_names.append(day_names)

        plan_text = "".join(text_parts).rstrip()
        summary = "按锁定路线顺序整理的基础行程。"
        if trip_notices:
            summary = "\n".join([*trip_notices, summary])
        plans.append(PlanOutput(
            plan_name=f"基础行程 {plan_index}",
            plan_text=plan_text,
            summary=summary,
            used_place_ids=used_place_ids,
            used_place_names=used_place_names,
            day_place_names=day_place_names,
            packing_checklist=None,
            travel_tips=None,
            poi_fragments=fragments,
        ))

    output_payload = [
        plan.model_dump(mode="json", exclude={"poi_fragments"})
        for plan in plans
    ]
    return SafePlanRenderResult(
        plans=plans,
        latency_ms=int((time.monotonic() - started) * 1000),
        input_sha256=_sha256(normalized),
        output_sha256=_sha256(output_payload),
    )
