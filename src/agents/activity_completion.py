"""Deterministic per-POI Activity completion for v0.8.13 F2."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any

from src.agents import publish_gate
from src.agents.evidence_strength import (
    DeterministicActionContract,
    StructuredEvidencePayload,
)
from src.agents.poi_fragments import replace_fragment_body
from src.agents.route_planning import (
    _is_day_heading_line,
    _parse_day_heading_number,
    mask_authorized_commute_spans,
    route_plan_violations,
)
from src.agents.schema import CandidatePlace, PlanOutput, RoutePlan
from src.agents.writer_repair_sanitizer import DAY_HEADING_RE


_ACTIVITY_SENTENCE_END_RE = re.compile(r"[。！？；;\n]")
_MEAL_ROLES = frozenset({
    "meal_stop",
    "snack_stop",
    "coffee_stop",
    "cafe_stop",
})


def _key_dict(key: tuple[int, int, int]) -> dict[str, int]:
    plan_index, day, place_id = key
    return {
        "plan_index": plan_index,
        "day": day,
        "place_id": place_id,
    }


@dataclass
class ActivityCompletionResult:
    plans: list[PlanOutput]
    before_findings: list[publish_gate.PublishFinding] = field(default_factory=list)
    after_findings: list[publish_gate.PublishFinding] = field(default_factory=list)
    attempted_keys: list[tuple[int, int, int]] = field(default_factory=list)
    applied_keys: list[tuple[int, int, int]] = field(default_factory=list)
    skipped_details: list[dict[str, Any]] = field(default_factory=list)

    def to_metrics(self) -> dict[str, Any]:
        unresolved_keys = sorted({
            (
                int(finding.plan_index),
                int(finding.day),
                int(finding.place_id),
            )
            for finding in self.after_findings
            if (
                finding.plan_index is not None
                and finding.day is not None
                and finding.place_id is not None
            )
        })
        return {
            "activity_local_completion_enabled": True,
            "activity_local_completion_before_missing_count": len(
                self.before_findings
            ),
            "activity_local_completion_attempted_count": len(
                self.attempted_keys
            ),
            "activity_local_completion_applied_count": len(self.applied_keys),
            "activity_local_completion_skipped_count": len(
                self.skipped_details
            ),
            "activity_local_completion_after_missing_count": len(
                self.after_findings
            ),
            "activity_local_completion_applied_keys": [
                _key_dict(key) for key in self.applied_keys
            ],
            "activity_local_completion_skipped_details": list(
                self.skipped_details
            ),
            "activity_local_completion_unresolved_keys": [
                _key_dict(key) for key in unresolved_keys
            ],
        }


def _valid_day_body_spans(plan_text: str) -> dict[int, list[tuple[int, int]]]:
    matches: list[tuple[re.Match[str], int]] = []
    for match in DAY_HEADING_RE.finditer(plan_text or ""):
        day = _parse_day_heading_number(match.group(1) or match.group(2))
        if day is None:
            continue
        if not _is_day_heading_line(match.group(0), match.group(3) or ""):
            continue
        matches.append((match, day))

    spans: dict[int, list[tuple[int, int]]] = {}
    for index, (match, day) in enumerate(matches):
        section_end = (
            matches[index + 1][0].start()
            if index + 1 < len(matches)
            else len(plan_text or "")
        )
        spans.setdefault(day, []).append((match.end(), section_end))
    return spans


def _accepted_mention_names(
    plan: PlanOutput,
    place: CandidatePlace,
) -> list[str]:
    names = [place.name]
    identity_result = plan.poi_identity_result
    for relation in getattr(identity_result, "relations", []) or []:
        if (
            getattr(relation, "source_name", "") == place.name
            and getattr(relation, "relation", "")
            in {"canonical_same", "alias_same"}
        ):
            names.append(str(getattr(relation, "target_name", "") or ""))
    return sorted(
        {name.strip() for name in names if name and name.strip()},
        key=lambda value: (-len(value), value),
    )


def _find_insertion_offset(
    plan: PlanOutput,
    route_plan: RoutePlan,
    *,
    day: int,
    place: CandidatePlace,
) -> tuple[int | None, str]:
    spans = _valid_day_body_spans(plan.plan_text or "").get(day, [])
    if len(spans) != 1:
        return None, (
            "day_section_missing"
            if not spans
            else "day_section_ambiguous"
        )

    body_start, body_end = spans[0]
    body = (plan.plan_text or "")[body_start:body_end]
    masked_body = mask_authorized_commute_spans(
        body,
        route_plan,
        day=day,
    )
    hits: list[tuple[int, int, str]] = []
    for name in _accepted_mention_names(plan, place):
        match = re.search(re.escape(name), masked_body)
        if match is not None:
            hits.append((match.start(), -len(name), name))
    if not hits:
        return None, "locked_place_mention_not_found"

    _, _, selected_name = min(hits)
    mention = re.search(re.escape(selected_name), masked_body)
    if mention is None:
        return None, "locked_place_mention_not_found"
    boundary = _ACTIVITY_SENTENCE_END_RE.search(masked_body, mention.end())
    relative_offset = boundary.end() if boundary is not None else len(body)
    return body_start + relative_offset, ""


def completion_sentence_for_contract(
    contract: DeterministicActionContract,
) -> str:
    """Render one F1-authorized action for both F2 completion and local FR.

    Skips [INTERNAL-prefixed items and falls back to a generic action when
    no publishable action is available.
    """
    from src.agents.evidence_strength import first_publishable_action

    publishable = first_publishable_action(contract.authorized_actions)
    if publishable is None:
        # No publishable action available: use the Safe fallback
        action = "选择感兴趣的部分游览，按体力决定参观范围"
    else:
        action = publishable.strip().rstrip("。")

    if contract.blueprint_role in _MEAL_ROLES:
        return f"到{contract.place_name}后，安排{action}。"
    return f"到{contract.place_name}后，{action}。"


def _insert_sentences(
    plan_text: str,
    insertions: dict[int, list[str]],
) -> str:
    updated = plan_text or ""
    for offset in sorted(insertions, reverse=True):
        sentences = list(dict.fromkeys(insertions[offset]))
        prefix = "" if offset > 0 and updated[offset - 1] == "\n" else "\n"
        suffix = "" if offset < len(updated) and updated[offset] == "\n" else "\n"
        updated = (
            updated[:offset]
            + prefix
            + "\n".join(sentences)
            + suffix
            + updated[offset:]
        )
    return updated


def deterministic_activity_completion(
    plans: list[PlanOutput],
    *,
    route_plans: list[RoutePlan],
    structured_evidence_payload: StructuredEvidencePayload,
) -> ActivityCompletionResult:
    """Close only keyed Activity gaps using pre-authorized fact-free actions."""
    initial_findings = publish_gate.collect_activity_coverage_findings(
        plans,
        route_plans,
    )
    updated_plans = list(plans)
    result = ActivityCompletionResult(
        plans=updated_plans,
        before_findings=initial_findings,
    )
    attempted: set[tuple[int, int, int]] = set()

    # Activity attribution can be revealed one stop at a time when several
    # locked POIs share a clause. Iterate to a fixed point, but handle each
    # stable key at most once. This is a bounded local closure, not LLM Retry.
    while True:
        current_findings = publish_gate.collect_activity_coverage_findings(
            updated_plans,
            route_plans,
        )
        findings_by_plan: dict[int, list[publish_gate.PublishFinding]] = {}
        for finding in current_findings:
            if (
                finding.plan_index is None
                or finding.day is None
                or finding.place_id is None
            ):
                continue
            key = (
                int(finding.plan_index),
                int(finding.day),
                int(finding.place_id),
            )
            if key in attempted:
                continue
            attempted.add(key)
            result.attempted_keys.append(key)
            findings_by_plan.setdefault(key[0], []).append(finding)

        if not findings_by_plan:
            result.after_findings = current_findings
            break

        applied_this_round = 0
        for plan_index in sorted(findings_by_plan):
            zero_index = plan_index - 1
            if (
                zero_index < 0
                or zero_index >= len(updated_plans)
                or zero_index >= len(route_plans)
            ):
                for finding in findings_by_plan[plan_index]:
                    key = (
                        int(finding.plan_index or 0),
                        int(finding.day or 0),
                        int(finding.place_id or 0),
                    )
                    result.skipped_details.append({
                        **_key_dict(key),
                        "reason": "plan_index_out_of_range",
                    })
                continue

            plan = updated_plans[zero_index]
            route_plan = route_plans[zero_index]
            insertions: dict[
                int,
                list[tuple[int, str, tuple[int, int, int]]],
            ] = {}
            skipped_for_plan: list[dict[str, Any]] = []
            locked_order = {
                (day_group.day, place.place_id): order
                for day_group in route_plan.day_groups
                for order, place in enumerate(day_group.places)
            }
            places_by_key = {
                (day_group.day, place.place_id): place
                for day_group in route_plan.day_groups
                for place in day_group.places
            }

            if plan.poi_fragments:
                candidate = plan
                candidate_keys: list[tuple[int, int, int]] = []
                for finding in findings_by_plan[plan_index]:
                    day = int(finding.day or 0)
                    place_id = int(finding.place_id or 0)
                    key = (plan_index, day, place_id)
                    place = places_by_key.get((day, place_id))
                    contract = structured_evidence_payload.action_contract(
                        plan_index=plan_index,
                        day=day,
                        place_id=place_id,
                    )
                    reason = ""
                    if place is None:
                        reason = "locked_place_not_found"
                    elif contract is None:
                        reason = "action_contract_missing"
                    elif contract.blueprint_role == "transfer_context":
                        reason = "transfer_context_not_completed"
                    elif not contract.authorized_actions:
                        reason = "authorized_action_missing"
                    elif contract.place_name != place.name:
                        reason = "action_contract_place_mismatch"
                    if reason:
                        skipped_for_plan.append({
                            **_key_dict(key),
                            "reason": reason,
                        })
                        continue
                    replaced = replace_fragment_body(
                        candidate,
                        plan_index=plan_index,
                        day=day,
                        place_id=place_id,
                        replacement=completion_sentence_for_contract(contract),
                    )
                    if replaced is None:
                        skipped_for_plan.append({
                            **_key_dict(key),
                            "reason": "keyed_fragment_integrity_failed",
                        })
                        continue
                    candidate = replaced
                    candidate_keys.append(key)

                violations = route_plan_violations([candidate], [route_plan])
                if violations:
                    result.skipped_details.extend(skipped_for_plan)
                    result.skipped_details.extend({
                        **_key_dict(key),
                        "reason": "route_order_postcheck_failed",
                    } for key in candidate_keys)
                    continue
                updated_plans[zero_index] = candidate
                result.applied_keys.extend(candidate_keys)
                applied_this_round += len(candidate_keys)
                result.skipped_details.extend(skipped_for_plan)
                continue

            for finding in findings_by_plan[plan_index]:
                day = int(finding.day or 0)
                place_id = int(finding.place_id or 0)
                key = (plan_index, day, place_id)
                place = places_by_key.get((day, place_id))
                contract = structured_evidence_payload.action_contract(
                    plan_index=plan_index,
                    day=day,
                    place_id=place_id,
                )
                reason = ""
                if place is None:
                    reason = "locked_place_not_found"
                elif contract is None:
                    reason = "action_contract_missing"
                elif contract.blueprint_role == "transfer_context":
                    reason = "transfer_context_not_completed"
                elif not contract.authorized_actions:
                    reason = "authorized_action_missing"
                elif contract.place_name != place.name:
                    reason = "action_contract_place_mismatch"
                if reason:
                    skipped_for_plan.append({
                        **_key_dict(key),
                        "reason": reason,
                    })
                    continue

                offset, reason = _find_insertion_offset(
                    plan,
                    route_plan,
                    day=day,
                    place=place,
                )
                if offset is None:
                    skipped_for_plan.append({
                        **_key_dict(key),
                        "reason": reason,
                    })
                    continue
                insertions.setdefault(offset, []).append((
                    locked_order.get((day, place_id), 10_000),
                    completion_sentence_for_contract(contract),
                    key,
                ))

            serializable_insertions: dict[int, list[str]] = {}
            candidate_keys: list[tuple[int, int, int]] = []
            for offset, items in insertions.items():
                ordered = sorted(items, key=lambda item: (item[0], item[2]))
                serializable_insertions[offset] = [item[1] for item in ordered]
                candidate_keys.extend(item[2] for item in ordered)

            candidate = plan.model_copy(update={
                "plan_text": _insert_sentences(
                    plan.plan_text or "",
                    serializable_insertions,
                )
            })
            violations = route_plan_violations([candidate], [route_plan])
            if violations:
                result.skipped_details.extend(skipped_for_plan)
                result.skipped_details.extend({
                    **_key_dict(key),
                    "reason": "route_order_postcheck_failed",
                } for key in candidate_keys)
                continue

            updated_plans[zero_index] = candidate
            result.applied_keys.extend(candidate_keys)
            applied_this_round += len(candidate_keys)
            result.skipped_details.extend(skipped_for_plan)

        if applied_this_round == 0:
            result.after_findings = (
                publish_gate.collect_activity_coverage_findings(
                    updated_plans,
                    route_plans,
                )
            )
            break

    result.plans = updated_plans
    return result
