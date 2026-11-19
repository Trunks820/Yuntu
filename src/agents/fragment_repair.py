"""Span-targeted Fragment Repair for v0.8.13 (whitelist only)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

import httpx
import openai

from src.agents.model_json import decode_model_json
from src.agents.activity_completion import completion_sentence_for_contract
from src.agents.evidence_strength import StructuredEvidencePayload
from src.agents.fact_expression_taxonomy import has_hard_fact_marker
from src.agents.final_writer import _merge_locked_place_fields
from src.agents.generation_issues import GenerationIssue
from src.agents.llm import (
    LLMTransportError,
    RoleConfig,
    chat,
    llm_call_context,
    resolve_role_config,
    _call_context,
)
from src.agents.preflight import PreflightFinding, collect_phrase_preflight
from src.agents.poi_fragments import (
    fragment_for_span,
    replace_fragment_range,
    replace_fragment_body,
)
from src.agents.publish_gate import PLACEHOLDER_PHRASES
from src.agents.route_planning import (
    authorized_commute_mask_strings,
    extract_day_ordered_place_ids,
    route_plan_violations,
)
from src.agents.schema import PlanOutput, RoutePlan
from src.agents.text_quality import BANNED_DATABASE_PHRASES

logger = logging.getLogger(__name__)

WHITELIST_CODES = frozenset({
    "database_tone",
    "placeholder_wording",
    "unsupported_fact_expansion",
})
SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？；;\n])")
MAX_TARGETS = 3
MAX_DAY_NARRATIVE_RATIO = 0.20
MAX_DETERMINISTIC_DAY_NARRATIVE_RATIO = 0.25
ANAPHORIC_OWNER_PREFIXES = (
    "这里",
    "这边",
    "该处",
    "此处",
    "园内",
    "馆内",
    "店内",
    "街区内",
    "景区内",
    "现场",
)
ANAPHORIC_LOCAL_FACT_PREFIXES = (
    "登塔",
    "门票",
    "票价",
    "开放时间",
    "营业时间",
    "菜单",
    "菜品",
    "排队",
)
ANAPHORIC_LEAD_INS = (
    "需要注意的是",
    "需要注意",
    "需要提醒的是",
    "需要提醒",
    "值得注意的是",
    "如果考虑",
)


@dataclass
class RepairTarget:
    target_id: str
    plan_index: int
    day_index: int
    surface: str
    start: int
    end: int
    fragment_sha256: str
    issue_codes: list[str]
    fragment_text: str


@dataclass
class FragmentRepairResult:
    plans: list[PlanOutput]
    applied: bool = False
    failure_reason: str = "unknown"
    notes: list[str] = field(default_factory=list)
    target_ids: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.failure_reason not in FRAGMENT_REPAIR_FAILURE_REASONS:
            self.failure_reason = "unknown"


FRAGMENT_REPAIR_FAILURE_REASONS = frozenset({
    "transport_timeout",
    "transport_error",
    "invalid_json",
    "output_contract_invalid",
    "no_matching_fragment",
    "postcheck_failed",
    "budget_denied",
    "unknown",
})


class FragmentRepairCallFailure(RuntimeError):
    def __init__(self, reason: str) -> None:
        stable_reason = (
            reason if reason in FRAGMENT_REPAIR_FAILURE_REASONS else "unknown"
        )
        super().__init__(stable_reason)
        self.reason = stable_reason


def _sha256_text(value: str) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def _day_ranges(text: str) -> list[tuple[int, int, int]]:
    """Return (day_number, start, end) for each day section."""
    pattern = re.compile(
        r"(?im)^\s*(?:#{1,6}\s*)?(?:\*\*)?\s*"
        r"(?:Day\s*(\d+)|第\s*(\d+)\s*天)[^\n]*?(?:\*\*)?\s*$"
    )
    matches = list(pattern.finditer(text or ""))
    ranges: list[tuple[int, int, int]] = []
    for index, match in enumerate(matches):
        day = int(match.group(1) or match.group(2))
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text or "")
        ranges.append((day, match.start(), end))
    return ranges


def _blocked_spans_for_text(
    text: str,
    route_plan: RoutePlan | None,
) -> list[tuple[int, int]]:
    """Spans that are not repairable narrative: headings + authorized commute + POI lines."""
    blocked: list[tuple[int, int]] = []
    if not text:
        return blocked
    heading = re.compile(
        r"(?im)^\s*(?:#{1,6}\s*)?(?:\*\*)?\s*"
        r"(?:Day\s*\d+|第\s*\d+\s*天)[^\n]*?(?:\*\*)?\s*$"
    )
    for match in heading.finditer(text):
        blocked.append((match.start(), match.end()))
    if route_plan is not None:
        for value in authorized_commute_mask_strings(route_plan):
            if not value:
                continue
            cursor = 0
            while True:
                idx = text.find(value, cursor)
                if idx < 0:
                    break
                blocked.append((idx, idx + len(value)))
                cursor = idx + len(value)
    # POI-name lines (same heuristic as preflight surface classifier).
    for match in re.finditer(r"(?m)^[^\n]*$", text):
        line = match.group(0)
        stripped = line.strip()
        if stripped.startswith(("-", "•", "·")) and len(stripped) < 40:
            blocked.append((match.start(), match.end()))
    if not blocked:
        return []
    blocked.sort()
    merged: list[tuple[int, int]] = [blocked[0]]
    for start, end in blocked[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def repairable_narrative_chars(
    text: str,
    *,
    day: int,
    route_plan: RoutePlan | None = None,
) -> int:
    """Count repairable narrative characters for one day.

    Positive count of narrative_sentence-eligible text only. Excludes:
    day_heading, poi_name_line, authorized_commute_span, and other blocked spans.
    """
    day_ranges = {
        day_number: (start, end)
        for day_number, start, end in _day_ranges(text)
    }
    if day not in day_ranges:
        return 0
    day_start, day_end = day_ranges[day]
    blocked = _blocked_spans_for_text(text, route_plan)
    cursor = day_start
    total = 0
    for b_start, b_end in blocked:
        if b_end <= day_start or b_start >= day_end:
            continue
        clip_start = max(b_start, day_start)
        clip_end = min(b_end, day_end)
        if cursor < clip_start:
            # Count only non-empty narrative segments (strip pure whitespace lines).
            segment = text[cursor:clip_start]
            total += _count_narrative_segment_chars(segment)
        cursor = max(cursor, clip_end)
    if cursor < day_end:
        total += _count_narrative_segment_chars(text[cursor:day_end])
    return max(0, total)


def _count_narrative_segment_chars(segment: str) -> int:
    """Count characters in a residual segment that are narrative prose."""
    if not segment:
        return 0
    total = 0
    # Walk lines; skip pure whitespace / empty.
    offset = 0
    while offset < len(segment):
        nl = segment.find("\n", offset)
        if nl < 0:
            line = segment[offset:]
            next_offset = len(segment)
        else:
            line = segment[offset:nl]
            next_offset = nl + 1
        stripped = line.strip()
        if stripped and not (stripped.startswith(("-", "•", "·")) and len(stripped) < 40):
            total += len(line)
            if nl >= 0:
                total += 1  # include newline between narrative lines
        offset = next_offset
    return total


def post_validator_passes(issue_code: str, replacement_text: str) -> bool:
    text = replacement_text or ""
    if issue_code == "database_tone":
        return not any(phrase in text for phrase in BANNED_DATABASE_PHRASES)
    if issue_code == "placeholder_wording":
        return not any(phrase in text for phrase in PLACEHOLDER_PHRASES)
    return False


def _review_action_target(
    plan: PlanOutput,
    route_plan: RoutePlan,
    *,
    plan_index: int,
    day: int,
    start: int,
    end: int,
) -> tuple[int, int, dict[str, int]] | None:
    """Resolve a Review span through the locked-plan ownership map.

    Review text and ``issue.names`` never select the POI. The immutable span is
    expanded to one narrative sentence, then the existing route identity
    resolver maps that frozen sentence to exactly one locked POI. Ambiguous,
    cross-day, or unowned sentences stay outside deterministic FR.
    """
    text = plan.plan_text or ""
    expanded = _expand_to_narrative_sentence(
        text,
        start,
        end,
        blocked_spans=_blocked_spans_for_text(text, route_plan),
    )
    if expanded is None:
        return None
    target_start, target_end = expanded
    day_ranges = [
        (day_start, day_end)
        for day_number, day_start, day_end in _day_ranges(text)
        if day_number == day
    ]
    if (
        len(day_ranges) != 1
        or target_start < day_ranges[0][0]
        or target_end > day_ranges[0][1]
    ):
        return None
    day_groups = [
        day_group
        for day_group in route_plan.day_groups
        if day_group.day == day
    ]
    if len(day_groups) != 1:
        return None
    day_group = day_groups[0]

    def resolve_owned_place_id(
        owner_start: int,
        owner_end: int,
    ) -> tuple[int | None, bool]:
        resolved = extract_day_ordered_place_ids(
            text[owner_start:owner_end],
            day_group.places,
            route_plan=route_plan,
            identity_result=plan.poi_identity_result,
            day=day,
        )
        if resolved.get("ambiguous_alias") is not None:
            return None, False
        ordered_ids = list(resolved.get("ordered_place_ids") or [])
        if len(ordered_ids) == 1:
            return int(ordered_ids[0]), False
        return None, not ordered_ids

    owned_place_id, current_has_no_place = resolve_owned_place_id(
        target_start,
        target_end,
    )
    if owned_place_id is None:
        sentence = text[target_start:target_end].lstrip(
            " \t\r\n，,。！？；;：:"
        )
        for lead_in in ANAPHORIC_LEAD_INS:
            if sentence.startswith(lead_in):
                sentence = sentence[len(lead_in):].lstrip(
                    " \t\r\n，,。！？；;：:"
                )
                break
        if (
            not current_has_no_place
            or not sentence.startswith(
                (*ANAPHORIC_OWNER_PREFIXES, *ANAPHORIC_LOCAL_FACT_PREFIXES)
            )
        ):
            return None
        cursor = target_start
        while cursor > day_ranges[0][0] and text[cursor - 1].isspace():
            cursor -= 1
        if cursor <= day_ranges[0][0]:
            return None
        previous = _expand_to_narrative_sentence(
            text,
            cursor - 1,
            cursor,
            blocked_spans=_blocked_spans_for_text(text, route_plan),
        )
        if (
            previous is None
            or previous[0] < day_ranges[0][0]
            or previous[1] > target_start
        ):
            return None
        owned_place_id, _ = resolve_owned_place_id(*previous)
        if owned_place_id is None:
            return None

    matched_places = [
        place
        for place in day_group.places
        if (
            int(place.canonical_place_id)
            if place.canonical_place_id is not None
            else int(place.place_id)
        ) == owned_place_id
    ]
    if len(matched_places) != 1:
        return None
    return target_start, target_end, {
        "plan_index": plan_index,
        "day": day,
        "place_id": int(matched_places[0].place_id),
    }


def qualify_review_fragment_issues(
    plans: list[PlanOutput],
    issues: list[GenerationIssue],
    *,
    route_plans: list[RoutePlan] | None = None,
) -> list[GenerationIssue]:
    """Attach exact immutable spans and stable action keys to Review snippets.

    Only blocking unsupported-fact issues that resolve to one locked
    ``(plan_index, day, place_id)`` are qualified. Ambiguous, protected,
    cross-surface, or missing snippets remain spanless and therefore fail
    closed for hard content; content issues never consume whole-plan Retry.
    """
    qualified: list[GenerationIssue] = []
    for issue in issues:
        repairable_unsupported_fact = (
            issue.reason == "unsupported_fact_expansion"
            and (
                issue.category in {"BLOCKER", "REPAIR"}
                or (
                    issue.category == "WARN"
                    and issue.publish_action == "RECORD_ONLY"
                )
            )
            and issue.plan_index is not None
        )
        if not repairable_unsupported_fact:
            qualified.append(issue)
            continue
        zero = issue.plan_index - 1
        if zero < 0 or zero >= len(plans):
            qualified.append(issue)
            continue
        plan = plans[zero]
        text = plan.plan_text or ""
        metadata = dict(issue.metadata or {})
        if plan.poi_fragments:
            keyed_fragment = (
                plan.poi_fragment(
                    plan_index=issue.plan_index,
                    day=issue.day,
                    place_id=issue.place_id,
                )
                if issue.day is not None and issue.place_id is not None
                else None
            )
            if keyed_fragment is None:
                hard_fact = (
                    metadata.get("fact_policy") == "verifiable_hard_fact"
                    or has_hard_fact_marker(
                        f"{issue.snippet} {issue.evidence}"
                    )
                )
                metadata.update({
                    "review_anchored": False,
                    "keyed_fragment_contract_error": "key_missing_or_unknown",
                })
                qualified.append(issue.model_copy(update={
                    "category": "BLOCKER" if hard_fact else "WARN",
                    "publish_action": (
                        "FAIL_CLOSED" if hard_fact else "RECORD_ONLY"
                    ),
                    "metadata": metadata,
                }))
                continue
            metadata.update({
                "surface": "poi_fragment",
                "start": keyed_fragment.start,
                "end": keyed_fragment.end,
                "phrase": keyed_fragment.text,
                "fragment_sha256": _sha256_text(keyed_fragment.text),
                "review_anchored": True,
                "review_snippet": issue.snippet,
                "action_contract_key": {
                    "plan_index": keyed_fragment.plan_index,
                    "day": keyed_fragment.day,
                    "place_id": keyed_fragment.place_id,
                },
            })
            qualified.append(issue.model_copy(update={
                "day": keyed_fragment.day,
                "place_id": keyed_fragment.place_id,
                "metadata": metadata,
            }))
            continue

        if not issue.snippet:
            qualified.append(issue)
            continue
        pattern_start = metadata.get("pattern_start")
        pattern_end = metadata.get("pattern_end")
        has_exact_pattern_span = (
            isinstance(pattern_start, int)
            and isinstance(pattern_end, int)
            and 0 <= pattern_start < pattern_end <= len(text)
            and text[pattern_start:pattern_end] == issue.snippet
        )
        if has_exact_pattern_span:
            start = pattern_start
            end = pattern_end
        elif text.count(issue.snippet) == 1:
            start = text.find(issue.snippet)
            end = start + len(issue.snippet)
        else:
            qualified.append(issue)
            continue
        route = (
            route_plans[zero]
            if route_plans is not None and zero < len(route_plans)
            else None
        )
        if route is None:
            qualified.append(issue)
            continue
        day = issue.day
        if day is None:
            for day_number, day_start, day_end in _day_ranges(text):
                if day_start <= start < day_end:
                    day = day_number
                    break
        if day is None:
            qualified.append(issue)
            continue
        target = _review_action_target(
            plans[zero],
            route,
            plan_index=issue.plan_index,
            day=day,
            start=start,
            end=end,
        )
        if target is None:
            qualified.append(issue)
            continue
        target_start, target_end, action_contract_key = target
        fragment = text[target_start:target_end]
        metadata.update({
            "surface": "narrative_sentence",
            "start": target_start,
            "end": target_end,
            "phrase": fragment,
            "fragment_sha256": _sha256_text(fragment),
            "review_anchored": True,
            "review_snippet": issue.snippet,
            "action_contract_key": action_contract_key,
        })
        qualified.append(issue.model_copy(update={
            "day": day,
            "metadata": metadata,
        }))
    return qualified


def deterministic_unsupported_fact_fragment_repair(
    plan: PlanOutput,
    *,
    route_plan: RoutePlan,
    issues: list[GenerationIssue],
    plan_index: int,
    structured_evidence_payload: StructuredEvidencePayload,
) -> FragmentRepairResult:
    """Replace Review-anchored unsupported prose through the F1 action plan."""
    base_text = plan.plan_text or ""
    candidates = [
        issue for issue in issues
        if issue.plan_index == plan_index
        and issue.reason == "unsupported_fact_expansion"
        and (
            issue.category in {"BLOCKER", "REPAIR"}
            or (
                issue.category == "WARN"
                and issue.publish_action == "RECORD_ONLY"
            )
        )
        and (issue.metadata or {}).get("review_anchored") is True
    ]
    # Keyed POI fragments have exact immutable ownership and use a local
    # evidence-contract replacement, so every Review-confirmed key can be
    # closed in one bounded O(n) pass.  The legacy free-text path retains its
    # stricter target cap because it rewrites positional spans.
    if not plan.poi_fragments:
        candidates = candidates[:MAX_TARGETS]
    if not candidates:
        return FragmentRepairResult(plans=[plan], notes=["no review fragment targets"])

    if plan.poi_fragments:
        candidate = plan
        repaired_targets: list[str] = []
        repaired_keys: set[tuple[int, int, int]] = set()
        for issue in candidates:
            metadata = issue.metadata or {}
            key = metadata.get("action_contract_key")
            if not isinstance(key, dict):
                return FragmentRepairResult(
                    plans=[plan],
                    failure_reason="no_matching_fragment",
                )
            try:
                stable_key = (
                    int(key.get("plan_index", 0)),
                    int(key.get("day", 0)),
                    int(key.get("place_id", 0)),
                )
            except (TypeError, ValueError):
                return FragmentRepairResult(
                    plans=[plan],
                    failure_reason="no_matching_fragment",
                )
            if stable_key in repaired_keys:
                continue
            if (
                stable_key[0] != plan_index
                or stable_key[1] <= 0
                or stable_key[2] <= 0
            ):
                return FragmentRepairResult(
                    plans=[plan],
                    failure_reason="no_matching_fragment",
                )
            fragment = candidate.poi_fragment(
                plan_index=stable_key[0],
                day=stable_key[1],
                place_id=stable_key[2],
            )
            locked_place = next(
                (
                    place
                    for day_group in route_plan.day_groups
                    if day_group.day == stable_key[1]
                    for place in day_group.places
                    if int(place.place_id) == stable_key[2]
                ),
                None,
            )
            contract = structured_evidence_payload.action_contract(
                plan_index=stable_key[0],
                day=stable_key[1],
                place_id=stable_key[2],
            )
            if (
                fragment is None
                or locked_place is None
                or contract is None
                or contract.blueprint_role == "transfer_context"
                or not contract.authorized_actions
                or contract.place_name != locked_place.name
                or metadata.get("review_anchored") is not True
                or metadata.get("fragment_sha256") != _sha256_text(fragment.text)
            ):
                return FragmentRepairResult(
                    plans=[plan],
                    failure_reason="no_matching_fragment",
                )
            replaced = replace_fragment_body(
                candidate,
                plan_index=stable_key[0],
                day=stable_key[1],
                place_id=stable_key[2],
                replacement=completion_sentence_for_contract(contract),
            )
            if replaced is None:
                return FragmentRepairResult(
                    plans=[plan],
                    failure_reason="no_matching_fragment",
                )
            candidate = replaced
            repaired_keys.add(stable_key)
            repaired_targets.append(
                f"p{plan_index - 1}:d{stable_key[1]}:poi{stable_key[2]}:keyed"
            )

        violations = route_plan_violations([candidate], [route_plan])
        if violations:
            return FragmentRepairResult(
                plans=[plan],
                failure_reason="postcheck_failed",
                notes=[json.dumps(violations, ensure_ascii=False)],
            )
        return FragmentRepairResult(
            plans=[candidate],
            applied=bool(repaired_targets),
            target_ids=repaired_targets,
            notes=["stable keyed POI fragment replacement"],
        )

    days = {int(issue.day or 0) for issue in candidates}
    if 0 in days:
        return FragmentRepairResult(
            plans=[plan],
            failure_reason="output_contract_invalid",
            notes=[f"days={sorted(days)}"],
        )
    replacements: list[tuple[int, int, str, str]] = []
    used_ranges: list[tuple[int, int]] = []
    covered_by_day: dict[int, int] = {}
    for issue in candidates:
        day = int(issue.day or 0)
        metadata = issue.metadata or {}
        start = int(metadata.get("start", -1))
        end = int(metadata.get("end", -1))
        if start < 0 or end <= start or end > len(base_text):
            return FragmentRepairResult(
                plans=[plan],
                failure_reason="output_contract_invalid",
            )
        fragment = base_text[start:end]
        if _sha256_text(fragment) != metadata.get("fragment_sha256"):
            return FragmentRepairResult(
                plans=[plan],
                failure_reason="no_matching_fragment",
            )
        if any(start < used_end and used_start < end for used_start, used_end in used_ranges):
            continue
        key = metadata.get("action_contract_key")
        if not isinstance(key, dict):
            return FragmentRepairResult(
                plans=[plan],
                failure_reason="no_matching_fragment",
            )
        try:
            key_plan_index = int(key.get("plan_index", 0))
            key_day = int(key.get("day", 0))
            key_place_id = int(key.get("place_id", 0))
        except (TypeError, ValueError):
            return FragmentRepairResult(
                plans=[plan],
                failure_reason="no_matching_fragment",
            )
        if key_plan_index != plan_index or key_day != day or key_place_id <= 0:
            return FragmentRepairResult(
                plans=[plan],
                failure_reason="no_matching_fragment",
            )
        locked_place = next(
            (
                place
                for day_group in route_plan.day_groups
                if day_group.day == day
                for place in day_group.places
                if int(place.place_id) == key_place_id
            ),
            None,
        )
        contract = structured_evidence_payload.action_contract(
            plan_index=key_plan_index,
            day=key_day,
            place_id=key_place_id,
        )
        if (
            locked_place is None
            or contract is None
            or contract.blueprint_role == "transfer_context"
            or not contract.authorized_actions
            or contract.place_name != locked_place.name
        ):
            return FragmentRepairResult(
                plans=[plan],
                failure_reason="no_matching_fragment",
            )
        replacement = completion_sentence_for_contract(contract)
        target_id = (
            f"p{plan_index - 1}:d{day}:poi{key_place_id}:"
            f"review:{_sha256_text(fragment)[:12]}"
        )
        replacements.append((start, end, replacement, target_id))
        used_ranges.append((start, end))
        covered_by_day[day] = (
            covered_by_day.get(day, 0)
            + max(end - start, len(replacement))
        )

    coverage_notes: list[str] = []
    coverage_failed = not replacements
    for day in sorted(covered_by_day):
        narrative_chars = repairable_narrative_chars(
            base_text,
            day=day,
            route_plan=route_plan,
        )
        day_limit = max(
            0,
            int(
                narrative_chars
                * MAX_DETERMINISTIC_DAY_NARRATIVE_RATIO
            ),
        )
        covered = covered_by_day[day]
        coverage_notes.append(
            f"day={day}:covered={covered}:limit={day_limit}:"
            f"narrative={narrative_chars}"
        )
        if day_limit <= 0 or covered > day_limit:
            coverage_failed = True
    if coverage_failed:
        return FragmentRepairResult(
            plans=[plan],
            failure_reason="postcheck_failed",
            notes=coverage_notes,
        )

    text = base_text
    for start, end, replacement, _target_id in sorted(
        replacements,
        key=lambda item: item[0],
        reverse=True,
    ):
        text = text[:start] + replacement + text[end:]
    repaired = _merge_locked_place_fields(
        plan_name=plan.plan_name,
        plan_text=text,
        route_plan=route_plan,
        composition_blueprint=plan.composition_blueprint,
        poi_identity_result=plan.poi_identity_result,
        budget_result=plan.budget_result,
        summary=plan.summary,
        accommodation=plan.accommodation,
        transport=plan.transport,
        code_lodging_section=plan.code_lodging_section,
    )
    if route_plan_violations([repaired], [route_plan]):
        return FragmentRepairResult(
            plans=[plan],
            failure_reason="postcheck_failed",
        )
    return FragmentRepairResult(
        plans=[repaired],
        applied=True,
        target_ids=[item[3] for item in replacements],
        notes=["review-anchored deterministic fragment repair applied"],
    )


def _expand_to_narrative_sentence(
    text: str,
    start: int,
    end: int,
    *,
    blocked_spans: list[tuple[int, int]] | None = None,
) -> tuple[int, int] | None:
    """Expand a phrase hit to its enclosing narrative sentence bounds.

    Returns None when the expanded sentence intersects a blocked surface
    (e.g. authorized commute span), so FR cannot rewrite protected text.
    """
    if not text:
        return start, end
    start = max(0, min(start, len(text)))
    end = max(start, min(end, len(text)))
    left = start
    while left > 0 and text[left - 1] not in "。！？；;\n":
        left -= 1
    right = end
    while right < len(text) and text[right - 1] not in "。！？；;\n":
        if text[right - 1] in "。！？；;":
            break
        right += 1
        if right <= len(text) and right > end and text[right - 1] in "。！？；;\n":
            break
    if right < len(text) and text[right] in "。！？；;":
        right += 1
    if right <= left:
        left, right = start, end
    for b_start, b_end in blocked_spans or []:
        if left < b_end and b_start < right:
            return None
    return left, right


def build_targets_from_preflight(
    plan: PlanOutput,
    *,
    plan_index: int,
    issue_codes: set[str] | None = None,
    route_plan: RoutePlan | None = None,
) -> tuple[str, list[RepairTarget]]:
    """Build FR targets from span-qualified preflight findings on frozen text."""
    base_text = plan.plan_text or ""
    base_hash = _sha256_text(base_text)
    allowed = issue_codes or set(WHITELIST_CODES)
    findings = [
        finding for finding in collect_phrase_preflight(
            [plan],
            route_plans=[route_plan] if route_plan is not None else None,
        )
        if finding.reason in allowed
        and finding.surface == "narrative_sentence"
        and finding.plan_index == 1
    ]
    targets: list[RepairTarget] = []
    day_ranges = _day_ranges(base_text)
    day_budget: dict[int, int] = {}
    for day, start, end in day_ranges:
        day_budget[day] = max(1, int((end - start) * MAX_DAY_NARRATIVE_RATIO))

    blocked: list[tuple[int, int]] = []
    if route_plan is not None:
        for value in authorized_commute_mask_strings(route_plan):
            if not value:
                continue
            cursor = 0
            while True:
                idx = base_text.find(value, cursor)
                if idx < 0:
                    break
                blocked.append((idx, idx + len(value)))
                cursor = idx + len(value)

    used_ranges: list[tuple[int, int]] = []
    locked_day: int | None = None
    for finding in findings:
        if len(targets) >= MAX_TARGETS:
            break
        if finding.reason not in WHITELIST_CODES:
            continue
        if finding.surface != "narrative_sentence":
            continue
        day = finding.day or 1
        if locked_day is None:
            locked_day = day
        elif day != locked_day:
            continue
        expanded = _expand_to_narrative_sentence(
            base_text,
            finding.start,
            finding.end,
            blocked_spans=blocked,
        )
        if expanded is None:
            continue
        start, end = expanded
        if plan.poi_fragments:
            owner = fragment_for_span(
                plan,
                start=finding.start,
                end=finding.end,
            )
            if owner is None:
                continue
            start = max(start, owner.start)
            end = min(end, owner.end)
            # The backend-owned ``地点名：`` prefix may share a sentence with
            # Writer prose. Keep that prefix outside the repair target; the
            # registry selected ownership before this structural clipping.
            owner_prefix = base_text[owner.start:finding.start]
            colon_offset = max(owner_prefix.find("："), owner_prefix.find(":"))
            if colon_offset >= 0:
                start = max(start, owner.start + colon_offset + 1)
        if end <= start or end > len(base_text):
            continue
        if any(start < u_end and u_start < end for u_start, u_end in used_ranges):
            continue
        frag = base_text[start:end]
        frag_hash = _sha256_text(frag)
        target_id = (
            f"p{plan_index - 1}:d{day}:s{len(targets)}:{frag_hash[:12]}"
        )
        targets.append(RepairTarget(
            target_id=target_id,
            plan_index=plan_index,
            day_index=day,
            surface="narrative_sentence",
            start=start,
            end=end,
            fragment_sha256=frag_hash,
            issue_codes=[finding.reason],
            fragment_text=frag,
        ))
        used_ranges.append((start, end))
        if day in day_budget:
            day_budget[day] = max(0, day_budget[day] - len(frag))
    return base_hash, targets


async def call_fragment_repair_llm(
    *,
    plan_text: str,
    targets: list[RepairTarget],
    generator: str | None = None,
) -> dict[str, str]:
    """Ask Writer role for replacement_text only. Returns target_id -> text."""
    if not targets:
        return {}
    payload = {
        "instructions": (
            "Replace only the listed fragments. Return exact target_id set. "
            "No empty replacements. Do not rewrite whole days or invent transit detail."
        ),
        "targets": [
            {
                "target_id": target.target_id,
                "issue_codes": target.issue_codes,
                "fragment_text": target.fragment_text,
            }
            for target in targets
        ],
        "plan_text_excerpt": plan_text[:4000],
    }
    system = (
        "You perform fragment-only travel copy repair. "
        "Output a single JSON object: "
        '{"repairs":[{"target_id":"...","replacement_text":"..."}]}'
    )
    parent_ctx = _call_context.get()
    generation_index = parent_ctx.get("generation_index")
    if generation_index is None:
        generation_index = parent_ctx.get("publish_retry_round") or 0
    role_config_override: RoleConfig | None = None
    if generator == "opus":
        role_config_override = resolve_role_config("writer")
    elif generator == "ds_flash":
        from src.agents.llm import writer_standby_config
        from src.config import get_settings

        role_config_override = writer_standby_config(get_settings(), agent="repair")
    elif generator is not None:
        raise FragmentRepairCallFailure("unsupported_generator")

    with llm_call_context(
        call_reason="fragment_repair",
        role="writer",
        stage="FRAGMENT_REPAIR",
        key_agent="repair",
        generation_index=int(generation_index or 0),
        publish_retry_round=parent_ctx.get("publish_retry_round"),
        job_id=parent_ctx.get("job_id"),
        request_id=parent_ctx.get("request_id"),
    ):
        raw = await chat(
            system=system,
            user=json.dumps(payload, ensure_ascii=False),
            role="writer",
            temperature=0.1,
            json_mode=True,
            role_config_override=role_config_override,
        )
    try:
        data = decode_model_json(raw)
    except json.JSONDecodeError:
        raise FragmentRepairCallFailure("invalid_json")
    repairs = data.get("repairs") if isinstance(data, dict) else None
    if not isinstance(repairs, list):
        raise FragmentRepairCallFailure("output_contract_invalid")
    mapping: dict[str, str] = {}
    for item in repairs:
        if not isinstance(item, dict):
            continue
        target_id = str(item.get("target_id") or "")
        replacement = item.get("replacement_text")
        if not target_id or not isinstance(replacement, str) or not replacement.strip():
            continue
        mapping[target_id] = replacement
    if len(mapping) != len(repairs):
        raise FragmentRepairCallFailure("output_contract_invalid")
    return mapping


def apply_fragment_repairs(
    plan: PlanOutput,
    *,
    route_plan: RoutePlan,
    base_text_sha256: str,
    targets: list[RepairTarget],
    replacements: dict[str, str],
) -> FragmentRepairResult:
    """Atomic high-to-low apply with hash checks and post-validators."""
    base_text = plan.plan_text or ""
    if _sha256_text(base_text) != base_text_sha256:
        return FragmentRepairResult(
            plans=[plan],
            failure_reason="no_matching_fragment",
        )
    expected_ids = {target.target_id for target in targets}
    if set(replacements) != expected_ids:
        return FragmentRepairResult(
            plans=[plan],
            failure_reason="output_contract_invalid",
            notes=["replacement target set mismatch"],
        )

    # Coverage is day-cumulative against repairable narrative only:
    # exclude day headings and authorized commute spans from the denominator.
    # No artificial floor: short narrative days cannot rewrite 100% under a long commute.
    day_covered: dict[int, int] = {}
    for target in targets:
        replacement = replacements[target.target_id]
        for code in target.issue_codes:
            if not post_validator_passes(code, replacement):
                return FragmentRepairResult(
                    plans=[plan],
                    failure_reason="postcheck_failed",
                    notes=[f"{target.target_id}:{code}"],
                )
        original_size = max(1, target.end - target.start)
        replacement_size = len(replacement or "")
        covered = max(original_size, replacement_size)
        day_covered[target.day_index] = day_covered.get(target.day_index, 0) + covered

    for day, covered in day_covered.items():
        narrative_chars = repairable_narrative_chars(
            base_text,
            day=day,
            route_plan=route_plan,
        )
        day_limit = max(0, int(narrative_chars * MAX_DAY_NARRATIVE_RATIO))
        if day_limit <= 0 or covered > day_limit:
            return FragmentRepairResult(
                plans=[plan],
                failure_reason="postcheck_failed",
                notes=[
                    f"day={day}:covered={covered}:limit={day_limit}:"
                    f"narrative={narrative_chars}"
                ],
            )

    # Same-day lock for the applied set.
    days = {target.day_index for target in targets}
    if len(days) > 1:
        return FragmentRepairResult(
            plans=[plan],
            failure_reason="postcheck_failed",
            notes=[f"days={sorted(days)}"],
        )

    # Verify fragment hashes still match
    ordered = sorted(targets, key=lambda item: item.start, reverse=True)
    text = base_text
    keyed_repaired = plan
    for target in ordered:
        current_text = (
            keyed_repaired.plan_text
            if plan.poi_fragments
            else text
        )
        current = current_text[target.start:target.end]
        if _sha256_text(current) != target.fragment_sha256:
            return FragmentRepairResult(
                plans=[plan],
                failure_reason="no_matching_fragment",
                notes=[target.target_id],
            )
        if plan.poi_fragments:
            updated = replace_fragment_range(
                keyed_repaired,
                start=target.start,
                end=target.end,
                replacement=replacements[target.target_id],
            )
            if updated is None:
                return FragmentRepairResult(
                    plans=[plan],
                    failure_reason="postcheck_failed",
                    notes=[target.target_id],
                )
            keyed_repaired = updated
        else:
            text = (
                text[:target.start]
                + replacements[target.target_id]
                + text[target.end:]
            )

    repaired = (
        keyed_repaired
        if plan.poi_fragments
        else _merge_locked_place_fields(
            plan_name=plan.plan_name,
            plan_text=text,
            route_plan=route_plan,
            composition_blueprint=plan.composition_blueprint,
            poi_identity_result=plan.poi_identity_result,
            budget_result=plan.budget_result,
            summary=plan.summary,
            accommodation=plan.accommodation,
            transport=plan.transport,
            code_lodging_section=plan.code_lodging_section,
        )
    )
    if route_plan_violations([repaired], [route_plan]):
        return FragmentRepairResult(
            plans=[plan],
            failure_reason="postcheck_failed",
        )
    # Phrase validators on full text for claimed codes
    remaining = collect_phrase_preflight([repaired])
    claimed = {code for target in targets for code in target.issue_codes}
    if any(item.reason in claimed for item in remaining):
        return FragmentRepairResult(
            plans=[plan],
            failure_reason="postcheck_failed",
            notes=["phrase still present after apply"],
        )
    return FragmentRepairResult(
        plans=[repaired],
        applied=True,
        target_ids=[target.target_id for target in targets],
        notes=["fragment repair applied"],
    )


async def maybe_fragment_repair_plan(
    plan: PlanOutput,
    *,
    route_plan: RoutePlan,
    plan_index: int = 1,
    issue_codes: set[str] | None = None,
    generator: str | None = None,
) -> FragmentRepairResult:
    base_hash, targets = build_targets_from_preflight(
        plan,
        plan_index=plan_index,
        issue_codes=issue_codes,
        route_plan=route_plan,
    )
    if not targets:
        return FragmentRepairResult(plans=[plan], notes=["no fragment targets"])
    try:
        replacements = await call_fragment_repair_llm(
            plan_text=plan.plan_text or "",
            targets=targets,
            generator=generator,
        )
    except FragmentRepairCallFailure as exc:
        return FragmentRepairResult(
            plans=[plan],
            failure_reason=exc.reason,
        )
    except (
        asyncio.TimeoutError,
        TimeoutError,
        httpx.TimeoutException,
        openai.APITimeoutError,
    ):
        return FragmentRepairResult(
            plans=[plan],
            failure_reason="transport_timeout",
        )
    except (
        httpx.TransportError,
        openai.APIConnectionError,
        openai.APIStatusError,
        LLMTransportError,
    ):
        return FragmentRepairResult(
            plans=[plan],
            failure_reason="transport_error",
        )
    return apply_fragment_repairs(
        plan,
        route_plan=route_plan,
        base_text_sha256=base_hash,
        targets=targets,
        replacements=replacements,
    )
