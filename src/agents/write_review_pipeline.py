"""Write -> Review -> Repair -> Publish pipeline."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import unicodedata
from dataclasses import replace
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx
import openai

from src.agents import final_writer, generation_issues, yuntu_review, publish_gate
from src.agents.activity_completion import deterministic_activity_completion
from src.agents.blueprint_integrity import (
    check_plan_blueprint_integrity,
    normalize_declared_locked_stops,
)
from src.agents.dispatch_resolver import (
    DispatchBudgets,
    DispatchDecision,
    SOFT_COPY_RECORD_ONLY_REASONS,
    STRUCTURAL_ORDER_REASONS,
    classify_structural_reasons,
    resolve_dispatch,
)
from src.agents.diversity import normalize_plan_places
from src.agents.evidence_strength import (
    StructuredEvidencePayload,
    build_structured_evidence_payload,
    missing_action_contract_keys,
)
from src.agents.explicit_preference_fulfillment import (
    PreferenceFulfillmentReport,
    evaluate_explicit_preference_fulfillment,
    sync_day_opening_for_locked_arrangement,
    build_body_review_context, body_review_matches, route_preference_decisions,
)
from src.agents.fragment_repair import (
    FragmentRepairResult,
    deterministic_unsupported_fact_fragment_repair,
    maybe_fragment_repair_plan,
    qualify_review_fragment_issues,
)
from src.agents.generation_model_routing import (
    GenerationModelCallError,
    SharedCallBudget,
    project_execution_observation,
)
from src.agents.generation_metrics import GenerationMetrics
from src.agents.keyed_fragment_repair import (
    KEYED_FRAGMENT_REPAIR_MIN_TIMEOUT_SECONDS,
    KEYED_FRAGMENT_REVIEW_TIMEOUT_SECONDS,
    FragmentKey,
    build_keyed_fragment_targets,
    call_keyed_fragment_repair,
    fallback_ratio_exceeded,
    keyed_fragment_repair_timeout_seconds,
    review_keyed_fragment_replacements,
)
from src.agents.llm import LLMTransportError
from src.agents.plan_repair import PlanRepairResult, repair_single_plan
from src.agents.preflight import collect_phrase_preflight, run_predispatch_handlers
from src.agents.poi_fragments import (
    fragment_food_hint, fragment_for_span, normalize_body_food_boundary,
    remap_fragment_registry, replace_fragment_body,
)
from src.agents.repair_policy import (
    RepairBudgetExceeded,
    RepairBudgetTracker,
    RepairPolicy,
)
from src.agents.route_planning import route_plan_violations
from src.agents.pretrip_advice import PreTripAdvicePayload
from src.agents.schema import (
    AccommodationSuggestion,
    PlanOutput,
    RetrievalResult,
    RoutePlan,
    TransportSuggestion,
    TripRequest,
)
from src.agents.speculative_writer import AdoptedGenerator, retry_budget_admitted
from src.agents.safe_plan_renderer import (
    LockedSafeInput,
    SafePlanRenderResult,
    SafeRenderError,
    lock_safe_input,
    render_safe_plans,
)
from src.agents.weather_advisory import WeatherAdvisoryPayload
from src.agents.workflow_observer import (
    StageCallback,
    StageEventCallback,
    _run_observed_step,
)
from src.config import get_settings

logger = logging.getLogger(__name__)

SPECULATIVE_SAFE_TRIGGER = "speculative_no_publishable_draft"
V012_INITIAL_REVIEW_SECONDS = 40.0
V012_REPAIR_RECHECK_SECONDS = 45.0
V012_RECHECK_RESERVE_SECONDS = 15.0
V012_FINALIZATION_RESERVE_SECONDS = 10.0


def _technical_review_failure(exc: Exception) -> bool:
    if isinstance(exc, GenerationModelCallError):
        nontechnical = {"WIRE_INVALID", "SCHEMA_INVALID", "TRUNCATED", "CANCELLED"}
        if exc.code in nontechnical or (exc.code == "UNKNOWN" and exc.status == 200):
            return False
        def invalid_tree(value):
            if isinstance(value, dict):
                return value.get("error_code") in nontechnical or any(invalid_tree(v) for v in value.values())
            return isinstance(value, list) and any(invalid_tree(v) for v in value)
        return not invalid_tree(exc.diagnostic)
    return _review_transport_error_type(exc) is not None
KEYED_SOFT_REPAIR_REASONS = frozenset({
    "unsupported_fact_expansion",
    "activity_content_missing",
    "weak_evidence_data_gap",
    "generic_copy_quality_warn",
})


class ReviewLaunchBudgetUnavailable(asyncio.TimeoutError):
    """A speculative Review was not launched because only reserve remained."""


class DSMandatoryReviewUnavailable(RuntimeError):
    """A DS-authored body cannot ship because full Review was unavailable."""

class _UnanchoredPreReviewHardFact(publish_gate.PublishGateError):
    """A known hard claim has no station owner for deterministic replacement."""


WriterOutputCallback = Callable[[list[PlanOutput]], Awaitable[None]]


class ContentDispatchSignal(RuntimeError):
    """Raised when pipeline needs outer Publish Retry or hard fail after dispatch."""

    def __init__(
        self,
        decision: DispatchDecision,
        *,
        metrics: dict[str, Any],
        plans: list[PlanOutput] | None = None,
        review_notes: str = "",
    ) -> None:
        self.decision = decision
        self.metrics = metrics
        self.plans = plans or []
        self.review_notes = review_notes
        super().__init__(
            f"content dispatch: {decision.action}; reasons={','.join(decision.reasons[:5])}"
        )


def _explicit_env_false(name: str) -> bool:
    value = os.environ.get(name)
    if value is None:
        env_path = Path(".env")
        if env_path.exists():
            for line in env_path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#") or "=" not in stripped:
                    continue
                key, raw_value = stripped.split("=", 1)
                if key.strip().upper() == name:
                    value = raw_value.strip().strip("'\"")
                    break
    return str(value or "").strip().lower() in {"0", "false", "no", "off"}


def _writer_plan_concurrency_enabled(settings: Any) -> bool:
    del settings
    if _explicit_env_false("WRITER_PLAN_CONCURRENCY_ENABLED"):
        return False
    return True


def _review_plan_concurrency_enabled(settings: Any) -> bool:
    del settings
    if _explicit_env_false("REVIEW_PLAN_CONCURRENCY_ENABLED"):
        return False
    return True


def _review_risk_skip_enabled(settings: Any) -> bool:
    return bool(getattr(settings, "review_risk_skip_enabled", False))


def _taxonomy_findings(
    issues: list[generation_issues.GenerationIssue],
    *,
    prefix: str,
) -> list[publish_gate.PublishFinding]:
    findings: list[publish_gate.PublishFinding] = []
    for issue in issues:
        message_parts = [prefix, f"source={issue.source}"]
        if issue.evidence:
            message_parts.append(f"evidence={issue.evidence[:240]}")
        if issue.names:
            message_parts.append("names=" + ",".join(issue.names[:5]))
        findings.append(publish_gate.PublishFinding(
            reason=issue.reason,
            message="; ".join(message_parts),
            plan_index=issue.plan_index,
            day=issue.day,
            snippet=issue.snippet,
        ))
    return findings


def _taxonomy_step_metadata(
    taxonomy: generation_issues.IssueTaxonomyResult,
) -> dict[str, Any]:
    issue_details = []
    for issue in taxonomy.issues[:12]:
        metadata = issue.metadata if isinstance(issue.metadata, dict) else {}
        issue_details.append({
            "source": issue.source,
            "category": issue.category,
            "publish_action": issue.publish_action,
            "reason": issue.reason,
            "plan_index": issue.plan_index,
            "day": issue.day,
            "names": issue.names[:6],
            "snippet": issue.snippet[:180],
            "evidence": issue.evidence[:220],
            "pattern_reason": str(metadata.get("pattern_reason") or ""),
        })
    return {
        "issue_counts": taxonomy.issue_counts,
        "repair_target_plan_indexes": taxonomy.repair_target_plan_indexes,
        "fail_closed_reasons": taxonomy.fail_closed_reasons,
        "data_backlog_reasons": taxonomy.data_backlog_reasons,
        "copy_quality": generation_issues.summarize_copy_quality_issues(
            taxonomy.issues
        ),
        "issue_details": issue_details,
    }


def _metric_value(metrics: GenerationMetrics, key: str, default: Any = None) -> Any:
    if hasattr(metrics, key):
        return getattr(metrics, key)
    return metrics.extra.get(key, default)


def _review_transport_error_type(exc: Exception) -> str | None:
    """Return a transport code only for an explicit transport exception type."""
    if isinstance(exc, ReviewLaunchBudgetUnavailable):
        return "no_residual_budget"
    if isinstance(exc, LLMTransportError):
        return exc.transport_type
    if isinstance(
        exc,
        (TimeoutError, asyncio.TimeoutError, httpx.TimeoutException),
    ):
        return "timeout"
    if isinstance(exc, openai.APITimeoutError):
        return "timeout"
    if isinstance(exc, httpx.HTTPStatusError):
        status_code = exc.response.status_code
        if status_code == 429:
            return "rate_limited"
        if status_code == 524:
            return "http_524"
        if 500 <= status_code < 600:
            return "http_5xx"
        return None
    if isinstance(exc, openai.RateLimitError):
        return "rate_limited"
    if isinstance(exc, openai.APIStatusError):
        status_code = exc.status_code
        if status_code == 524:
            return "http_524"
        if 500 <= status_code < 600:
            return "http_5xx"
        return None
    if isinstance(exc, (httpx.TransportError, openai.APIConnectionError)):
        return "connection_reset"
    return None


def _review_unavailable_type(exc: Exception) -> tuple[str, str] | None:
    """Classify an unusable Review result without retaining its raw output."""
    if isinstance(exc, ReviewLaunchBudgetUnavailable):
        return "budget", "no_residual_budget"
    if isinstance(exc, yuntu_review.ReviewSafetyError):
        invalid_output_type = exc.invalid_output_type
        if invalid_output_type:
            return "invalid_output", invalid_output_type
    transport_type = _review_transport_error_type(exc)
    if transport_type is not None:
        return "transport", transport_type
    return None


def _review_relay_endpoint_since(record_start: int) -> str:
    """Return a sanitized Review relay node name from existing LLM telemetry."""
    from src.agents.llm import current_llm_observation_summary

    records = current_llm_observation_summary().get("llm_call_records") or []
    for record in reversed(records[max(0, record_start):]):
        if not isinstance(record, dict) or record.get("role") != "review":
            continue
        endpoint = str(record.get("relay_endpoint") or "").strip()
        if endpoint and re.fullmatch(r"[A-Za-z0-9._-]+", endpoint):
            return endpoint
    return "unknown"


REVIEW_HIGH_RISK_REASONS = frozenset({
    "fabricated_city",
    "fabricated_place",
    "severe_fact_misleading",
})


def _transport_error_message(exc: Exception) -> str:
    return str(exc).replace("\n", " ")[:500]


def _issue_source_counts(
    issues: list[generation_issues.GenerationIssue],
    source: str,
) -> dict[str, int]:
    counts = {"WARN": 0, "REPAIR": 0, "BLOCKER": 0}
    for issue in issues:
        if issue.source != source:
            continue
        counts[issue.category] = counts.get(issue.category, 0) + 1
    return counts


def _flag_counts(flags: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for flag in flags:
        counts[flag] = counts.get(flag, 0) + 1
    return counts


def _review_shadow_flag_for_issue(
    issue: generation_issues.GenerationIssue,
) -> str | None:
    reason = issue.reason
    metadata = issue.metadata if isinstance(issue.metadata, dict) else {}
    pattern_reason = str(metadata.get("pattern_reason") or "")
    evidence = issue.evidence or ""

    if reason == "route_outside_poi":
        return "unauthorized_poi_names"
    if reason in {
        "cross_day_poi",
        "declared_locked_day_group_violation",
        "text_locked_day_group_violation",
    }:
        return "cross_day_place_movement"
    if reason in {
        "declared_day_count_mismatch",
        "text_day_count_mismatch",
        "declared_locked_day_group_violation",
    }:
        return "day_place_names_mismatch"
    if reason in {
        "missing_route_plan",
        "plan_count_mismatch",
        "blueprint_integrity_violation",
    }:
        return "route_blueprint_mismatch"
    if reason == "database_tone":
        return "database_source_tone"
    if reason == "unsupported_fact_expansion":
        if (
            issue.category == "WARN"
            and issue.publish_action == "RECORD_ONLY"
            and metadata.get("fact_policy") == "soft_prose_shadow"
        ):
            return None
        if pattern_reason:
            if pattern_reason in {
                "booking_claim",
                "booking_detail_claim",
                "history_culture_claim",
                "opening_hours_claim",
                "popularity_best_claim",
                "price_claim",
                "source_voice_claim",
                "ticket_claim",
                "ticket_decision_advice",
                "transport_decision_advice",
            }:
                return "opening_ticket_reservation_crowd_ranking_claim"
            return "hard_fact_without_evidence"
        if "deterministic pattern:" in evidence:
            return "hard_fact_without_evidence"
    if reason == "long_transfer_missing_or_misleading":
        return "missing_long_commute_explanation"
    return None


def _is_review_risk_shadow_only_issue(
    issue: generation_issues.GenerationIssue,
) -> bool:
    metadata = issue.metadata if isinstance(issue.metadata, dict) else {}
    return (
        issue.reason in publish_gate.SHADOW_FINDING_REASONS
        or (
            issue.reason == "unsupported_fact_expansion"
            and issue.category == "WARN"
            and issue.publish_action == "RECORD_ONLY"
            and metadata.get("fact_policy") == "soft_prose_shadow"
        )
    )


# Same Writer-safe fields as the failed-draft table (src/jobs/trip_failed_draft.py).
_FAILED_DRAFT_FIELDS = frozenset({
    "plan_name", "summary", "plan_text", "used_place_names", "day_place_names",
})


def _review_shadow_high_risk_flags(flags: list[str]) -> set[str]:
    high_risk_flags = {
        "unauthorized_poi_names",
        "cross_day_place_movement",
        "used_place_names_mismatch",
        "day_place_names_mismatch",
        "route_blueprint_mismatch",
        "hard_fact_without_evidence",
        "opening_ticket_reservation_crowd_ranking_claim",
    }
    return high_risk_flags.intersection(flags)


def _review_shadow_risk_level(score: int, flags: list[str]) -> str:
    if _review_shadow_high_risk_flags(flags) or score >= 4:
        return "HIGH"
    if flags or score > 0:
        return "MEDIUM"
    return "LOW"


def _review_shadow_risk_level_source(score: int, flags: list[str]) -> str:
    high_flag_matches = bool(_review_shadow_high_risk_flags(flags))
    issue_score_matches = score > 0
    if high_flag_matches and score >= 4:
        return "risk_flags_and_issue_counts"
    if high_flag_matches:
        return "risk_flags"
    if score >= 4:
        return "issue_counts"
    if flags and issue_score_matches:
        return "risk_flags_and_issue_counts"
    if flags:
        return "risk_flags"
    if issue_score_matches:
        return "issue_counts"
    return "no_risk_signals"


def _missing_long_commute_explanation_flags(
    plans: list[PlanOutput],
    composition_blueprints: list | None,
) -> list[str]:
    if not composition_blueprints:
        return []
    flags: list[str] = []
    long_transfer_markers = (
        "路程稍远",
        "路程较远",
        "这段稍远",
        "预留时间",
        "预留约",
        "通勤参考",
    )
    for index, plan in enumerate(plans):
        if index >= len(composition_blueprints):
            continue
        blueprint = composition_blueprints[index]
        for day in getattr(blueprint, "days", []) or []:
            day_text = _shadow_day_section(plan.plan_text or "", getattr(day, "day", None))
            for commute in getattr(day, "commutes", []) or []:
                if not getattr(commute, "must_mention", False):
                    continue
                duration = int(getattr(commute, "duration_minutes", 0) or 0)
                mentions_duration = duration > 0 and str(duration) in day_text
                mentions_long_transfer = any(
                    marker in day_text for marker in long_transfer_markers
                )
                if not mentions_duration and not mentions_long_transfer:
                    flags.append("missing_long_commute_explanation")
    return flags


def _shadow_day_section(plan_text: str, day_number: int | None) -> str:
    if not plan_text or not day_number:
        return plan_text or ""
    heading = re.compile(
        r"(?im)^\s*(?:#{1,6}\s*)?(?:\*\*)?\s*"
        r"(?:Day\s*(\d+)|第\s*(\d+|[一二三四五六七八九十]+)\s*天)[^\n]*?(?:\*\*)?\s*$"
    )
    matches = list(heading.finditer(plan_text))
    for index, match in enumerate(matches):
        parsed = _shadow_parse_day_number(match.group(1) or match.group(2))
        if parsed != day_number:
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(plan_text)
        return plan_text[match.start():end]
    return plan_text


def _shadow_parse_day_number(value: str | None) -> int | None:
    if not value:
        return None
    if value.isdigit():
        return int(value)
    digits = {
        "一": 1,
        "二": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
    }
    if value == "十":
        return 10
    if value.startswith("十") and len(value) == 2:
        tail = digits.get(value[1:])
        return 10 + tail if tail is not None else None
    if value.endswith("十") and len(value) == 2:
        head = digits.get(value[:1])
        return head * 10 if head is not None else None
    if "十" in value and len(value) == 3:
        head = digits.get(value[:1])
        tail = digits.get(value[-1:])
        if head is not None and tail is not None:
            return head * 10 + tail
    return digits.get(value)


def _shadow_crowd_or_ranking_flags(plans: list[PlanOutput]) -> list[str]:
    pattern = re.compile(
        r"排队|人多|人很多|人流量大|拥挤|榜单|排名|排行|热门榜|网红榜"
    )
    for plan in plans:
        if pattern.search(plan.plan_text or ""):
            return ["opening_ticket_reservation_crowd_ranking_claim"]
    return []


def _build_review_shadow_metadata(
    deterministic_issues: list[generation_issues.GenerationIssue],
    *,
    plans: list[PlanOutput],
    route_plans: list[RoutePlan],
    composition_blueprints: list | None = None,
    llm_issues: list[generation_issues.GenerationIssue] | None = None,
) -> dict[str, Any]:
    raw_flags: list[str] = []
    for issue in deterministic_issues:
        if _is_review_risk_shadow_only_issue(issue):
            continue
        flag = _review_shadow_flag_for_issue(issue)
        if flag:
            raw_flags.append(flag)
    if route_plans:
        if not _plans_match_locked_used_names(plans, route_plans):
            raw_flags.append("used_place_names_mismatch")
        if not _plans_match_locked_day_names(plans, route_plans):
            raw_flags.append("day_place_names_mismatch")
    raw_flags.extend(_missing_long_commute_explanation_flags(
        plans,
        composition_blueprints,
    ))
    raw_flags.extend(_shadow_crowd_or_ranking_flags(plans))

    flags = sorted(set(raw_flags))
    flag_counts = _flag_counts(raw_flags)
    score = 0
    for issue in deterministic_issues:
        if _is_review_risk_shadow_only_issue(issue):
            continue
        if issue.category == "BLOCKER":
            score += 3
        elif issue.category == "REPAIR":
            score += 2
        elif issue.category == "WARN":
            score += 1
    level = _review_shadow_risk_level(score, flags)
    level_source = _review_shadow_risk_level_source(score, flags)
    deterministic_counts = _issue_source_counts(deterministic_issues, "deterministic")
    publish_gate_counts = _issue_source_counts(deterministic_issues, "publish_gate")
    shadow_only_issues = [
        issue
        for issue in deterministic_issues
        if _is_review_risk_shadow_only_issue(issue)
    ]
    confirmed_generation_issues: dict[str, Any] = {
        "deterministic_issue_counts": deterministic_counts,
        "publish_gate_issue_counts": publish_gate_counts,
        "total_issue_count": len(deterministic_issues),
    }
    metadata: dict[str, Any] = {
        "review_shadow_enabled": True,
        "risk_flags": flags,
        "risk_flag_counts": flag_counts,
        "deterministic_risk_flags": flags,
        "shadow_risk_level": level,
        "risk_score": score,
        "risk_level": level,
        "risk_level_source": level_source,
        "risk_level_semantics": "review_shadow_risk_not_publish_issue_severity",
        "confirmed_generation_issues": confirmed_generation_issues,
        "review_shadow_deterministic_issue_counts": deterministic_counts,
        "review_shadow_publish_gate_issue_counts": publish_gate_counts,
        "review_shadow_flag_counts": flag_counts,
        "review_shadow_only_issue_count": len(shadow_only_issues),
        "review_shadow_only_reasons": sorted({
            issue.reason for issue in shadow_only_issues
        }),
    }
    if llm_issues is not None:
        llm_counts = _issue_source_counts(llm_issues, "llm_review")
        confirmed_generation_issues["llm_review_issue_counts"] = llm_counts
        confirmed_generation_issues["total_issue_count"] = (
            len(deterministic_issues) + len(llm_issues)
        )
        deterministic_reasons = {
            issue.reason for issue in deterministic_issues if issue.reason
        }
        llm_reasons = {issue.reason for issue in llm_issues if issue.reason}
        metadata.update({
            "review_shadow_llm_issue_counts": llm_counts,
            "review_shadow_mismatch_summary": {
                "deterministic_issue_count": len(deterministic_issues),
                "llm_issue_count": len(llm_issues),
                "deterministic_only_reasons": sorted(
                    deterministic_reasons - llm_reasons
                )[:12],
                "llm_only_reasons": sorted(llm_reasons - deterministic_reasons)[:12],
                "shared_reasons": sorted(deterministic_reasons & llm_reasons)[:12],
            },
        })
    return metadata


def _stable_model_payload(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [_stable_model_payload(item) for item in value]
    if isinstance(value, dict):
        return {key: _stable_model_payload(item) for key, item in value.items()}
    return value


def _stable_json_signature(value: Any) -> str:
    return json.dumps(
        _stable_model_payload(value),
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )


def _route_locked_day_names(route_plan: RoutePlan) -> list[list[str]]:
    return [
        [place.name for place in day_group.places]
        for day_group in route_plan.day_groups
    ]


def _route_locked_used_names(route_plan: RoutePlan) -> list[str]:
    return [
        place.name
        for day_group in route_plan.day_groups
        for place in day_group.places
    ]


def _plans_match_locked_day_names(
    plans: list[PlanOutput],
    route_plans: list[RoutePlan],
) -> bool:
    if len(plans) != len(route_plans):
        return False
    return all(
        plan.day_place_names == _route_locked_day_names(route_plan)
        for plan, route_plan in zip(plans, route_plans)
    )


def _plans_match_locked_used_names(
    plans: list[PlanOutput],
    route_plans: list[RoutePlan],
) -> bool:
    if len(plans) != len(route_plans):
        return False
    return all(
        plan.used_place_names == _route_locked_used_names(route_plan)
        for plan, route_plan in zip(plans, route_plans)
    )


def _taxonomy_without_repaired_issues(
    taxonomy: generation_issues.IssueTaxonomyResult,
    repaired_plan_indexes: list[int],
) -> generation_issues.IssueTaxonomyResult:
    repaired = set(repaired_plan_indexes)
    retained = [
        issue
        for issue in taxonomy.issues
        if not (
            issue.publish_action == "REPAIR_PLAN"
            and issue.plan_index in repaired
        )
    ]
    return generation_issues.resolve_generation_issues(retained)


def _infer_issue_plan_indexes(
    issues: list[generation_issues.GenerationIssue],
    plans: list[PlanOutput],
) -> list[generation_issues.GenerationIssue]:
    """Recover missing plan_index for repairable semantic issues.

    LLM review occasionally returns a precise snippet/name but omits plan_index.
    Without an index the issue remains unresolved yet cannot enter follow-up
    repair. Keep the inference conservative: only assign when exactly one plan
    contains the issue snippet or all named entities.
    """
    # F2 keyed plans require Review to return the backend-owned action key.
    # Never recover ownership from snippet or place-name text once that contract
    # exists; a missing hard key must fail closed and a missing soft key remains
    # record-only.
    if any(plan.poi_fragments for plan in plans):
        return issues

    inferred: list[generation_issues.GenerationIssue] = []
    for issue in issues:
        if issue.plan_index is not None and issue.plan_index >= 1:
            inferred.append(issue)
            continue
        matches: list[int] = []
        snippet = (issue.snippet or "").strip()
        names = [name for name in issue.names if name]
        for index, plan in enumerate(plans, 1):
            haystack = "\n".join([
                plan.plan_name or "",
                plan.plan_text or "",
                " ".join(plan.used_place_names or []),
            ])
            if snippet and snippet in haystack:
                matches.append(index)
                continue
            if names and all(name in haystack for name in names):
                matches.append(index)
        if len(matches) == 1:
            issue = issue.model_copy(update={"plan_index": matches[0]})
        inferred.append(issue)
    return inferred


_BACKEND_OWNED_LLM_REVIEW_REASONS = frozenset({
    "long_transfer_missing_or_misleading",
    "activity_content_missing",
    "food_tier_exceeded",
    "food_none_tier_violation",
    "food_source_attribution",
    *STRUCTURAL_ORDER_REASONS,
})


def _quotes_only_code_lodging_block(
    issue: generation_issues.GenerationIssue,
    plans: list[PlanOutput],
) -> bool:
    """True when the quoted text exists only inside the code-written lodging block."""
    snippet = "".join((issue.snippet or "").split())
    if not snippet:
        return False
    candidates = (
        plans if issue.plan_index is None
        else plans[issue.plan_index - 1:issue.plan_index]
    )
    for plan in candidates:
        span = yuntu_review.code_lodging_block_span(plan)
        if span is None:
            continue
        block = "".join((plan.plan_text or "")[span[0]:span[1]].split())
        # The same words in writer prose remain Review's to judge.
        visible = "".join(yuntu_review._without_code_lodging_section(plan).split())
        if snippet in block and snippet not in visible:
            return True
    return False


def _filter_backend_owned_llm_review_issues(
    issues: list[generation_issues.GenerationIssue],
    plans: list[PlanOutput] | None = None,
) -> tuple[
    list[generation_issues.GenerationIssue],
    list[generation_issues.GenerationIssue],
]:
    """Drop LLM opinions about contracts owned by deterministic backend checks.

    The code-written lodging block (hotel names, tiers, prices) is one of them:
    Review never sees it, so a finding that quotes only that block is dropped.
    """
    retained: list[generation_issues.GenerationIssue] = []
    dropped: list[generation_issues.GenerationIssue] = []
    for issue in issues:
        if issue.source == "llm_review" and (
            issue.reason in _BACKEND_OWNED_LLM_REVIEW_REASONS
            or _quotes_only_code_lodging_block(issue, plans or [])
        ):
            dropped.append(issue)
            continue
        retained.append(issue)
    return retained, dropped


async def run_taxonomy_review(
    plans: list[PlanOutput],
    *,
    trip_request: TripRequest,
    retrieval: RetrievalResult,
    route_plans: list[RoutePlan],
    budget_results: list | None,
    composition_blueprints: list | None,
    review_plan_concurrency_enabled: bool,
    structured_evidence_payload: StructuredEvidencePayload | None = None,
    weather_advisory_payload: WeatherAdvisoryPayload | None = None,
    attachment_auth_map: dict | None = None,
    accommodation: AccommodationSuggestion | None = None,
    review_timeout_seconds: float = yuntu_review.ORDINARY_REVIEW_REQUEST_TIMEOUT_SECONDS,
    generator: str | None = None,
    generation_policy_version: str = "legacy",
    review_call_budget: SharedCallBudget | None = None,
    allow_failure_backup: bool = True,
    previous_review_issues: list[generation_issues.GenerationIssue] | None = None,
) -> tuple[generation_issues.IssueTaxonomyResult, dict[str, Any], dict[str, Any]]:
    """Collect deterministic and semantic generation issues."""
    step_metadata: dict[str, Any] = {}
    review_metrics: dict[str, Any] = {}
    _review_timeout_seconds = review_timeout_seconds
    body_context = None
    if generation_policy_version == "v0.12":
        try:
            body_context = build_body_review_context(trip_request=trip_request,
                route_plans=route_plans or [], plans=plans,
                structured_evidence_payload=structured_evidence_payload,
                previous_issues=previous_review_issues or [])
        except (ValueError, TypeError) as exc:
            raise yuntu_review.ReviewSafetyError(str(exc), invalid_output_type="body_context_invalid") from exc
    deterministic = generation_issues.collect_deterministic_generation_issues(
        plans,
        trip_request=trip_request,
        retrieval=retrieval,
        route_plans=route_plans or [],
        budget_results=budget_results,
        composition_blueprints=composition_blueprints,
        weather_advisory_payload=weather_advisory_payload,
        attachment_auth_map=attachment_auth_map,
        accommodation=accommodation,
        body_semantics_by_review=generation_policy_version == "v0.12",
    )
    shadow_metadata = _build_review_shadow_metadata(
        deterministic,
        plans=plans,
        route_plans=route_plans or [],
        composition_blueprints=composition_blueprints,
    )
    step_metadata.update(shadow_metadata)
    if review_plan_concurrency_enabled and route_plans and len(plans) > 1:
        semantic, review_metrics = (
            await yuntu_review.collect_semantic_generation_issues_by_plan(
                plans,
                retrieval,
                route_plans=route_plans,
                composition_blueprints=composition_blueprints,
                structured_evidence_payload=structured_evidence_payload,
                weather_advisory_payload=weather_advisory_payload,
                attachment_auth_map=attachment_auth_map,
                timeout_seconds=_review_timeout_seconds,
                generator=generator,
                generation_policy_version=generation_policy_version,
                review_call_budget=review_call_budget,
                allow_failure_backup=allow_failure_backup,
            )
        )
        step_metadata.update({
            "parallel_used": True,
            "per_plan_latency_ms": review_metrics.get(
                "review_plan_latencies_ms", []
            ),
            "parallel_wall_latency_ms": review_metrics.get(
                "review_parallel_wall_latency_ms", 0
            ),
            "plan_count": len(plans),
        })
    else:
        step_metadata.update({
            "parallel_used": False,
            "plan_count": len(plans),
        })
        semantic = await yuntu_review.collect_semantic_generation_issues(
            plans,
            retrieval,
            route_plans=route_plans,
            composition_blueprints=composition_blueprints,
            structured_evidence_payload=structured_evidence_payload,
            weather_advisory_payload=weather_advisory_payload,
            attachment_auth_map=attachment_auth_map,
            timeout_seconds=_review_timeout_seconds,
            generator=generator,
            generation_policy_version=generation_policy_version,
            review_call_budget=review_call_budget,
            allow_failure_backup=allow_failure_backup,
            body_review_context=body_context,
        )
    body_receipt = getattr(semantic, "body_review", None)
    semantic, dropped_backend_owned = _filter_backend_owned_llm_review_issues(
        semantic, plans
    )
    dropped_backend_owned_metadata = {
        "review_dropped_backend_owned_count": len(dropped_backend_owned),
        "review_dropped_backend_owned_reasons": sorted({
            issue.reason for issue in dropped_backend_owned
        }),
        "review_dropped_backend_owned_issues": [
            {
                "reason": issue.reason,
                "plan_index": issue.plan_index,
                "day": issue.day,
                "snippet": (issue.snippet or "")[:80],
            }
            for issue in dropped_backend_owned
        ],
    }
    step_metadata.update(dropped_backend_owned_metadata)
    review_metrics.update(dropped_backend_owned_metadata)
    semantic = _infer_issue_plan_indexes(semantic, plans)
    shadow_metadata = _build_review_shadow_metadata(
        deterministic,
        plans=plans,
        route_plans=route_plans or [],
        composition_blueprints=composition_blueprints,
        llm_issues=semantic,
    )
    step_metadata.update(shadow_metadata)
    result = generation_issues.resolve_generation_issues(
        [*deterministic, *semantic]
    )
    result.body_review = body_receipt
    step_metadata.update(_taxonomy_step_metadata(result))
    return result, step_metadata, review_metrics


def assert_single_locked_selected_route(
    route_plans: list[RoutePlan],
    *,
    required: bool = False,
) -> None:
    """Fail before Writer dispatch unless the selected route is uniquely locked."""
    selected_path = any(plan.membership_ledger is not None for plan in route_plans)
    if not required and not selected_path:
        return
    if len(route_plans) != 1:
        raise RuntimeError("Writer requires exactly one selected route plan")
    plan = route_plans[0]
    if plan.membership_ledger is None:
        raise RuntimeError("selected route plan is missing its membership ledger")
    if not plan.day_groups or any(not day.places for day in plan.day_groups):
        raise RuntimeError("selected route plan is not a valid locked itinerary")
    days = [day.day for day in plan.day_groups]
    if days != list(range(1, len(days) + 1)):
        raise RuntimeError("selected route plan days are not consecutively locked")


class WriteReviewPublishPipeline:
    def __init__(
        self,
        *,
        trip_request: TripRequest,
        retrieval: RetrievalResult,
        route_plans: list[RoutePlan],
        poi_identity_results: list | None,
        budget_results: list | None,
        composition_blueprints: list | None,
        accommodation: AccommodationSuggestion | None = None,
        transport: TransportSuggestion | None = None,
        attachment_auth_map: dict | None = None,
        weather_advisory_payload: WeatherAdvisoryPayload | None = None,
        structured_evidence_payload: StructuredEvidencePayload | None = None,
        pretrip_advice_payloads: list[PreTripAdvicePayload] | None = None,
        amap_poi_detail_metrics: dict[str, Any] | None = None,
        on_stage: StageCallback | None = None,
        on_stage_event: StageEventCallback | None = None,
        on_writer_output: WriterOutputCallback | None = None,
        attempt: int = 1,
        publish_retry_round: int = 0,
        repair_policy: RepairPolicy | None = None,
        fragment_repair_call_count: int = 0,
        publish_retry_feedback: list[dict[str, Any]] | None = None,
        workflow_deadline_monotonic: float | None = None,
        residual_reserve_seconds: float = 20.0,
        speculative_initial_generation: bool = True,
        require_selected_route_lock: bool = False,
        generation_policy_version: str = "legacy",
        review_call_budget: SharedCallBudget | None = None,
    ) -> None:
        settings = get_settings()
        self.trip_request = trip_request
        self.retrieval = retrieval
        self.route_plans = route_plans
        self.poi_identity_results = poi_identity_results
        self.budget_results = budget_results
        self.composition_blueprints = composition_blueprints
        self.accommodation = accommodation
        self.transport = transport
        self.attachment_auth_map = dict(attachment_auth_map or {})
        self.weather_advisory_payload = weather_advisory_payload
        self.on_stage = on_stage
        self.on_stage_event = on_stage_event
        self.on_writer_output = on_writer_output
        self.attempt = attempt
        self.publish_retry_round = publish_retry_round
        self.repair_policy = repair_policy or RepairPolicy.from_settings()
        self.metrics = GenerationMetrics(
            writer_original_model=settings.writer_model,
            writer_repair_model=settings.writer_model,
            writer_original_attempt=attempt,
            writer_plan_concurrency_enabled=_writer_plan_concurrency_enabled(
                settings
            ),
        )
        self.metrics.merge_known_metrics({
            "published_variant": "normal",
            "delivery_status": "NORMAL",
            "safe_trigger": None,
            "review_transport_degraded": False,
            "review_transport_error_type": "",
            "review_invalid_output": False,
            "review_invalid_output_type": "",
            "review_reason": "",
            "review_anchor_complete": False,
            "review_missing_anchor_fields": [],
            "fragment_repair_attempted": False,
            "safe_renderer_attempted": False,
            "safe_renderer_latency_ms": 0,
            "safe_renderer_output_sha256": "",
            "safe_renderer_postcheck_passed": False,
            "publish_gate_passed": False,
        })
        self.pretrip_advice_payloads = list(pretrip_advice_payloads or [])
        if structured_evidence_payload is None:
            self.structured_evidence_payload = build_structured_evidence_payload(
                retrieval,
                route_plans=route_plans,
                composition_blueprints=composition_blueprints,
            )
        else:
            self.structured_evidence_payload = structured_evidence_payload
        if amap_poi_detail_metrics:
            self.metrics.merge_known_metrics(amap_poi_detail_metrics)
        self.locked_safe_input: LockedSafeInput | None = None
        self._refresh_locked_safe_input()
        self.metrics.merge_known_metrics({
            "structured_evidence": (
                self.structured_evidence_payload.metrics_summary()
                | self.structured_evidence_payload.metrics
            )
        })
        if self.weather_advisory_payload is not None:
            self.metrics.merge_known_metrics(
                self.weather_advisory_payload.metrics_summary()
            )
        self.plans: list[PlanOutput] = []
        self.repaired_plan_indexes: list[int] = []
        # Shared across Publish Retry rounds via workflow-owned counter.
        self.fragment_repair_call_count = max(0, int(fragment_repair_call_count or 0))
        self.publish_retry_feedback = list(publish_retry_feedback or [])
        self.dispatch_decision: DispatchDecision | None = None
        # When True, outer workflow already spent the Publish Retry budget.
        self.after_publish_retry = bool(publish_retry_round >= 1)
        self.workflow_deadline_monotonic = workflow_deadline_monotonic
        self.residual_reserve_seconds = float(residual_reserve_seconds)
        self.speculative_initial_generation = bool(speculative_initial_generation)
        self._writer_plan_retry_remaining = 1
        self.require_selected_route_lock = bool(require_selected_route_lock)
        self.generation_policy_version = generation_policy_version
        self._body_review = None
        self._body_review_prior = []
        self._next_review_issues = []
        self._approved_original = None
        if generation_policy_version == "v0.12":
            overall_deadline = (
                float(workflow_deadline_monotonic)
                if workflow_deadline_monotonic is not None
                else time.monotonic() + 180.0
            )
            self.review_call_budget = review_call_budget or SharedCallBudget(
                deadline_monotonic=overall_deadline,
                max_calls=5,
                reserve_seconds=V012_FINALIZATION_RESERVE_SECONDS,
            )
        else:
            self.review_call_budget = review_call_budget
        self._v012_repair_phase_deadline: float | None = None
        self._v012_repair_round_active = False
        self._v012_repair_remote_started = False
        self._speculative_adopted_archive_records: list[Any] = []

    def _v012_review_phase_budget(self, stage: str) -> SharedCallBudget | None:
        if self.generation_policy_version != "v0.12":
            return None
        assert self.review_call_budget is not None
        now = time.monotonic()
        if stage == "REVIEW_TAXONOMY_AFTER_REPAIR":
            phase_deadline = self._v012_repair_phase_deadline or (
                now + V012_REPAIR_RECHECK_SECONDS
            )
        else:
            phase_deadline = now + V012_INITIAL_REVIEW_SECONDS
        phase_deadline = min(
            phase_deadline,
            self.review_call_budget.deadline_monotonic
            - self.review_call_budget.reserve_seconds,
        )
        return self.review_call_budget.for_phase(
            deadline_monotonic=phase_deadline,
            reserve_seconds=0.0,
        )

    def _check_publish_gate(
        self,
        plans: list[PlanOutput] | None = None,
    ) -> publish_gate.PublishGateResult:
        return publish_gate.check_publish_gate(
            plans if plans is not None else self.plans,
            trip_request=self.trip_request,
            retrieval=self.retrieval,
            route_plans=self.route_plans,
            attachment_auth_map=self.attachment_auth_map,
            accommodation=self.accommodation,
            body_semantics_by_review=self.generation_policy_version == "v0.12",
        )

    def _residual_seconds(self) -> float | None:
        if self.workflow_deadline_monotonic is None:
            return None
        return float(self.workflow_deadline_monotonic) - time.monotonic()

    def _speculative_round(self) -> bool:
        return bool(_metric_value(self.metrics, "adjudication_reason", ""))

    def _draft_generator(self) -> str:
        value = str(_metric_value(self.metrics, "generator", "opus") or "opus")
        allowed = {item.value for item in AdoptedGenerator}
        if value not in allowed:
            raise ValueError(f"invalid internal Writer generator: {value}")
        return value

    def _ds_review_mandatory(self) -> bool:
        return (
            self._speculative_round()
            and self._draft_generator() == AdoptedGenerator.DS_FLASH.value
        )

    def _review_tail_reserve_seconds(self) -> float:
        return (
            V012_FINALIZATION_RESERVE_SECONDS
            if self.generation_policy_version == "v0.12"
            else self.residual_reserve_seconds
        )

    def _speculative_review_timeout_seconds(self) -> float | None:
        """Return the §8.2 whole-Review cap, or None when launch is forbidden."""
        remaining = self._residual_seconds()
        reserve_seconds = self._review_tail_reserve_seconds()
        # The tail reserve belongs to the whole job. The repair phase already
        # has its own absolute deadline; subtracting the tail again from that
        # phase silently turns a 45s phase into about 25s.
        if remaining is not None and remaining <= reserve_seconds:
            return None
        available = (
            remaining - reserve_seconds
            if remaining is not None
            else yuntu_review.ORDINARY_REVIEW_REQUEST_TIMEOUT_SECONDS
        )
        if (
            self.generation_policy_version == "v0.12"
            and self._v012_repair_phase_deadline is not None
        ):
            phase_remaining = self._v012_repair_phase_deadline - time.monotonic()
            available = min(available, phase_remaining)
        # Quantize downward so float representation cannot allocate a timeout
        # a few picoseconds beyond the workflow budget.
        available = int(available * 1000) / 1000
        if available <= 0:
            return None
        cap = (
            yuntu_review.ORDINARY_REVIEW_REQUEST_TIMEOUT_SECONDS
            if self.generation_policy_version == "v0.12" else 90.0
        )
        return min(cap, available)

    def _residual_admits(self, *, min_seconds: float) -> bool:
        remaining = self._residual_seconds()
        if remaining is None:
            return True
        return remaining >= (min_seconds + self.residual_reserve_seconds)

    def _repair_round_used(self) -> bool:
        return bool(self.fragment_repair_call_count
                    or self.metrics.writer_keyed_fragment_repair_attempted)

    def _reserve_v012_repair_round(self) -> bool:
        if self._repair_round_used():
            return False
        self._v012_repair_round_active = True
        self.fragment_repair_call_count += 1
        self.metrics.fragment_repair_call_count = self.fragment_repair_call_count
        return True

    def _start_v012_repair_phase(self, *, min_writer_seconds: float) -> bool:
        """Admit once after preparing targets; actual calls use the same deadline."""
        now = time.monotonic()
        if self._v012_repair_phase_deadline is None:
            span = min(V012_REPAIR_RECHECK_SECONDS, max(0.0,
                (self.workflow_deadline_monotonic or float("inf"))
                - V012_FINALIZATION_RESERVE_SECONDS - now))
        else:
            span = self._v012_repair_phase_deadline - now
        if span < min_writer_seconds + V012_RECHECK_RESERVE_SECONDS:
            return False
        if self._v012_repair_phase_deadline is None:
            self._v012_repair_phase_deadline = now + span
        return True

    def _v012_repair_writer_timeout(self, cap: float) -> float:
        assert self._v012_repair_phase_deadline is not None
        return min(cap, max(0.0, self._v012_repair_phase_deadline
            - time.monotonic() - V012_RECHECK_RESERVE_SECONDS))

    async def _await_v012_repair(self, call: Awaitable[Any], *, cap: float) -> Any:
        """Cancel at the original absolute cutoff, including setup overhead."""
        assert self._v012_repair_phase_deadline is not None
        deadline = min(time.monotonic() + cap,
                       self._v012_repair_phase_deadline - V012_RECHECK_RESERVE_SECONDS)
        task = asyncio.create_task(call)
        expired = False
        def expire() -> None:
            nonlocal expired
            expired = True
            task.cancel()
        timer = asyncio.get_running_loop().call_at(deadline, expire)
        try:
            return await task
        except asyncio.CancelledError:
            if expired:
                raise asyncio.TimeoutError("repair absolute deadline exhausted")
            raise
        finally:
            timer.cancel()

    def _claim_v012_repair_remote(self) -> bool:
        if (not self._v012_repair_round_active or self._v012_repair_remote_started
                or self.metrics.writer_keyed_fragment_repair_attempted):
            return False
        self._v012_repair_remote_started = True
        return True

    def _dispatch_budgets(
        self,
        *,
        allow_deterministic_fact_followup: bool = False,
    ) -> DispatchBudgets:
        remaining = self._residual_seconds()
        residual_note = ""
        admits_retry = self._residual_admits(min_seconds=25.0)
        admits_fragment = self._residual_admits(min_seconds=8.0)
        if remaining is not None and not admits_retry:
            residual_note = "retry_not_admitted_due_to_remaining_budget"
            self.metrics.residual_denial_reason = residual_note
        return DispatchBudgets(
            publish_retry_remaining=0 if self.after_publish_retry else 1,
            fragment_repair_remaining=(
                1
                if (
                    (self.fragment_repair_call_count <= 0
                     and (self.generation_policy_version != "v0.12"
                          or not self.metrics.writer_keyed_fragment_repair_attempted))
                    or (
                        allow_deterministic_fact_followup
                        and self.generation_policy_version != "v0.12"
                        and self.fragment_repair_call_count < 2
                    )
                )
                else 0
            ),
            residual_admits_retry=admits_retry,
            residual_admits_fragment=admits_fragment,
            after_publish_retry=self.after_publish_retry,
        )

    async def _observe_content_validation(
        self,
        action: Callable[[], Awaitable[Any]],
        *,
        boundary: str,
        metadata: dict[str, Any] | None = None,
    ) -> Any:
        """Persist deterministic post-generation decisions as their own stage."""
        return await _run_observed_step(
            "CONTENT_VALIDATION",
            action,
            on_stage_event=self.on_stage_event,
            set_current_stage=False,
            attempt=self.attempt,
            publish_retry_round=self.publish_retry_round,
            metadata={
                "validation_boundary": boundary,
                **(metadata or {}),
            },
        )

    def _validate_dispatch_decision(
        self,
        decision: DispatchDecision,
        taxonomy_issues: list[generation_issues.GenerationIssue],
    ) -> None:
        self.dispatch_decision = decision
        self.metrics.dispatch_action = decision.action
        self.metrics.dispatch_reasons = list(decision.reasons)
        self.metrics.dispatch_notes = list(decision.notes)
        self.metrics.fragment_repair_call_count = self.fragment_repair_call_count

        if decision.action == "INVARIANT_FAILURE":
            selected = [
                issue for issue in taxonomy_issues
                if generation_issues_issue_reason(issue) in set(decision.reasons)
            ] or taxonomy_issues[:5]
            findings = _taxonomy_findings(
                selected[:20],
                prefix="invariant failure",
            )
            raise publish_gate.PublishGateError(
                publish_gate.PublishGateResult(passed=False, findings=findings)
            )

        if decision.action == "FAIL_CLOSED":
            findings = _taxonomy_findings(
                taxonomy_issues[:20],
                prefix="dispatch fail-closed",
            )
            if not findings:
                findings = [
                    publish_gate.PublishFinding(
                        reason=(
                            decision.reasons[0]
                            if decision.reasons
                            else "fail_closed"
                        ),
                        message="; ".join(decision.notes) or "dispatch fail-closed",
                    )
                ]
            raise publish_gate.PublishGateError(
                publish_gate.PublishGateResult(passed=False, findings=findings)
            )

        if decision.action not in {"PUBLISH_RETRY", "FRAGMENT_REPAIR"}:
            unresolved_blocking = [
                issue for issue in taxonomy_issues
                if getattr(issue, "category", "") in {"BLOCKER", "REPAIR"}
                and getattr(issue, "publish_action", "") == "FAIL_CLOSED"
                and generation_issues_issue_reason(issue)
                not in SOFT_COPY_RECORD_ONLY_REASONS
            ]
            if unresolved_blocking:
                findings = _taxonomy_findings(
                    unresolved_blocking,
                    prefix="blocking residual",
                )
                raise publish_gate.PublishGateError(
                    publish_gate.PublishGateResult(
                        passed=False,
                        findings=findings,
                    )
                )

    def _sentence_pruning_issues(self, semantic=(), *, deterministic_issues=None):
        """Collect code and Review findings without treating a reason as an owner."""
        from src.agents.generation_issues import review_issue_id
        if deterministic_issues is None:
            deterministic = generation_issues.collect_deterministic_generation_issues(
                self.plans, trip_request=self.trip_request, retrieval=self.retrieval,
                route_plans=self.route_plans or [], budget_results=self.budget_results,
                composition_blueprints=self.composition_blueprints,
                weather_advisory_payload=self.weather_advisory_payload,
                attachment_auth_map=self.attachment_auth_map, accommodation=self.accommodation,
                body_semantics_by_review=self.generation_policy_version == "v0.12",
            )
        else:
            deterministic = deterministic_issues
        gate_issues = generation_issues.map_publish_findings_to_issues(
            self._check_publish_gate().findings)
        return list({review_issue_id(issue): issue for issue in [
            *semantic, *deterministic, *gate_issues,
        ]}.values())

    def _record_sentence_pruning(self, result, *, phase, accepted, failure=""):
        records = list(self.metrics.extra.get("sentence_pruning_records", []))
        records.append({
            "phase": phase, "accepted": accepted, "failure": failure,
            "actions": result.actions if accepted else [],
            "attempted_actions": [] if accepted else result.actions,
            "resolved_issue_ids": result.resolved_issue_ids if accepted else [],
            "unresolved_issue_ids": (result.unresolved_issue_ids if accepted else
                                     list(dict.fromkeys([*result.resolved_issue_ids, *result.unresolved_issue_ids]))),
            "unresolved_details": list(getattr(result, "unresolved_details", []) or []),
        })
        self.metrics.merge_known_metrics({
            "sentence_pruning_records": records,
            "sentence_pruning_applied_count": sum(len(row["actions"]) for row in records if row["accepted"]),
            "sentence_pruning_fallback_count": len({
                tuple(action["fragment_key"][name] for name in ("plan_index", "day", "place_id"))
                for row in records if row["accepted"] for action in row["actions"]
                if action.get("replacement_text") and action.get("fragment_key")}),
        })

    def _prune_before_review(self, *, deterministic_issues=None) -> None:
        if self.generation_policy_version != "v0.12":
            return
        from src.agents.sentence_pruning import is_pruning_candidate, prune_problem_sentences
        observed_issues = self._sentence_pruning_issues(deterministic_issues=deterministic_issues)
        preflight_issues = [item.to_issue() for item in collect_phrase_preflight(
            self.plans, route_plans=self.route_plans)]
        # Preserve the existing shared fact/copy repair when a second defect
        # needs it. Deleting just the fact would strand the remaining defect
        # on a different repair path and lose the original joint-work budget.
        if any(issue.publish_action in {"REPAIR_PLAN", "FAIL_CLOSED"}
               and not is_pruning_candidate(issue)
               for issue in [*observed_issues, *preflight_issues]):
            return
        issues = [issue for issue in observed_issues if is_pruning_candidate(issue)]
        if not issues:
            return
        from src.agents.review_availability import pruning_change_matches
        result = prune_problem_sentences(
            self.plans, issues=issues, route_plans=self.route_plans or [],
            structured_evidence_payload=self.structured_evidence_payload, fill_empty=True)
        if (result.unresolved_issue_ids or not result.changed
                or not pruning_change_matches(self.plans, result.plans, result.actions)):
            self._record_sentence_pruning(result, phase="before_review", accepted=False,
                                          failure="problem_sentence_not_uniquely_owned")
            # Keep the original hard-fact check in charge: it can replace a
            # known station locally, or block an unowned claim before Review.
            return
        self.plans = result.plans
        self._record_sentence_pruning(result, phase="before_review", accepted=True)
        # The caller now reruns structural/preflight gates and normal Review on
        # this actual body. No old model receipt is used for the edited text.

    async def _publish_after_sentence_pruning(self, taxonomy):
        from src.agents.generation_issues import review_issue_id
        from src.agents.poi_fragments import fragment_registry_is_intact
        from src.agents.review_availability import (
            code_only_body_fingerprint, pruning_review_closures, has_current_pruning_receipt,
            pruning_change_matches,
        )
        from src.agents.sentence_pruning import is_pruning_candidate, prune_problem_sentences

        active_prior = [issue for issue in self._body_review_prior
                        if self._body_review.issue_resolutions.get(review_issue_id(issue), {}).get("status") == "still_present"]
        issues = self._sentence_pruning_issues([*taxonomy.issues, *active_prior])
        candidates = [issue for issue in issues if is_pruning_candidate(issue)]
        if not candidates:
            return None
        from copy import deepcopy
        original_state = deepcopy((self.plans, self._body_review,
                                   self._body_review_prior, self._next_review_issues))
        original_metrics = deepcopy(self.metrics.__dict__)
        adopted = False
        try:
            original = self.plans
            source_review = self._body_review.model_dump(mode="json")
            source_issues = [issue.model_dump(mode="json") for issue in issues]
            source_hash = code_only_body_fingerprint(original)
            result = prune_problem_sentences(
                original, issues=candidates, route_plans=self.route_plans or [],
                structured_evidence_payload=self.structured_evidence_payload, fill_empty=True)
            failure = "problem_sentence_not_uniquely_owned"
            if (result.changed and not result.unresolved_issue_ids
                    and pruning_change_matches(original, result.plans, result.actions)):
                closures = pruning_review_closures(source_review, source_issues, result.actions, result.plans)
                failure = "prior_findings_remaining"
                if closures is not None:
                    self.plans = result.plans
                    evidence = self._code_delivery_evidence()
                    checks = dict(evidence["gate_checks"])
                    preferences = self._explicit_preference_report()
                    self._record_preference_report(preferences, phase="after_repair")
                    checks.update({
                        "explicit_preference_gate_passed": not (
                            preferences.blocking_route_findings() or preferences.admission_block_findings()),
                        "writer_template_ratio_passed": self._template_ratio_ok(),
                        "pruning_boundaries_passed": True,
                        "fragment_registry_passed": all(fragment_registry_is_intact(p) for p in self.plans),
                        "known_findings_closed": True,
                        "workflow_deadline_remaining": self._residual_seconds() is None or self._residual_seconds() > 0,
                    })
                    failure = "post_pruning_code_gate_failed"
                    if all(checks.values()):
                        try:
                            expected_delivery = [p.model_copy(deep=True) for p in self._plans_without_food_markers()]
                            publish_result = await self._run_publish_gate()
                            plans = self._plans_without_food_markers()
                        except publish_gate.PublishGateError:
                            publish_result = None
                        if publish_result is not None:
                            # Re-evaluate after final deterministic cleanup, and bind
                            # the receipt to the marker-free body actually delivered.
                            checks.update(self._code_delivery_evidence()["gate_checks"])
                            final_preferences = self._explicit_preference_report()
                            checks.update({
                                "explicit_preference_gate_passed": not (
                                    final_preferences.blocking_route_findings() or final_preferences.admission_block_findings()),
                                "writer_template_ratio_passed": self._template_ratio_ok(),
                                "fragment_registry_passed": all(fragment_registry_is_intact(p) for p in plans),
                                # The gate may only remove its existing food markers.
                                # Any other edit would need its own ledger/recheck.
                                "pruning_boundaries_passed": plans == expected_delivery,
                            })
                            checks["final_publish_gate_passed"] = publish_result.passed
                            checks["workflow_deadline_remaining"] = self._residual_seconds() is None or self._residual_seconds() > 0
                            closures = pruning_review_closures(source_review, source_issues, result.actions, plans)
                            checks["known_findings_closed"] = closures is not None
                            receipt = {
                                "version": 1, "cause": "deterministic_sentence_prune",
                                "source_body_sha256": source_hash,
                                "body_sha256": code_only_body_fingerprint(plans),
                                "source_review_receipt": source_review,
                                "source_issues": source_issues, "actions": result.actions,
                                "closures": closures, "code_gates": checks,
                                "prior_findings_remaining": False,
                            }
                            accepted_metrics = {
                                "v012_review_state": "CODE_ONLY", "review_status": "code_rechecked",
                                "review_taxonomy_status": "deterministic_sentence_prune",
                                "published_variant": "normal", "delivery_status": "NORMAL",
                                "safe_trigger": None, "v012_basic_reason": None,
                                "v015_pruning_receipt": receipt,
                            }
                            if has_current_pruning_receipt(plans, accepted_metrics):
                                self._record_sentence_pruning(result, phase="after_review", accepted=True)
                                self.metrics.merge_known_metrics(accepted_metrics)
                                adopted = True
                                return {"kind": "sentence_pruned", "result": (
                                    plans, "", publish_result, self.metrics.to_dict())}
            self.plans = original
            self._record_sentence_pruning(result, phase="after_review", accepted=False, failure=failure)
            return None
        finally:
            if not adopted:
                attempted_records = self.metrics.extra.get("sentence_pruning_records")
                (self.plans, self._body_review, self._body_review_prior,
                 self._next_review_issues) = original_state
                self.metrics.__dict__.clear()
                self.metrics.__dict__.update(original_metrics)
                if attempted_records is not None:
                    self.metrics.extra["sentence_pruning_records"] = attempted_records
                # The original body/receipt/budget return to the existing local
                # repair dispatcher. A rejected optimization is not a veto.


    async def _classify_pre_review_content(self) -> dict[str, Any]:
        if self._speculative_round():
            self._draft_generator()
        if not self.plans:
            if self._speculative_round():
                return {
                    "kind": "safe",
                    "safe_trigger": SPECULATIVE_SAFE_TRIGGER,
                }
            incomplete_issue = generation_issues.GenerationIssue(
                source="deterministic",
                category="BLOCKER",
                publish_action="FAIL_CLOSED",
                reason="structurally_incomplete",
                evidence="writer produced no publishable plans",
            )
            decision = resolve_dispatch(
                structural_incomplete=[incomplete_issue],
                budgets=self._dispatch_budgets(),
            )
            if (
                decision.action != "PUBLISH_RETRY"
                and self._safe_policy_enabled("writer_failure")
            ):
                return {"kind": "safe", "safe_trigger": "writer_failure"}
            self._validate_dispatch_decision(decision, [incomplete_issue])
            return {
                "kind": "dispatch",
                "decision": decision,
                "taxonomy_issues": [incomplete_issue],
            }

        self.metrics.full_writer_generation_count = 1 + int(
            self.publish_retry_round or 0
        )
        self.metrics.publish_retry_count = int(self.publish_retry_round or 0)
        self.metrics.whole_plan_repair_bypassed = True
        self.metrics.content_dispatch_used = True
        self.metrics.fragment_repair_call_count = self.fragment_repair_call_count

        handler = run_predispatch_handlers(self.plans, self.route_plans)
        self.plans = handler.plans
        if self.generation_policy_version == "v0.12":
            # Opening correction, then program sentences, both before the Review
            # snapshot. A later edit inside the publish gate would make that
            # snapshot stale and replace the detailed draft with the basic plan.
            self._sync_day_openings_after_preference_repair()
            self._restore_preference_notices()
        if handler.notes:
            self.metrics.resolver_notes = list(dict.fromkeys(
                [*self.metrics.resolver_notes, *handler.notes]
            ))

        structural_issues = generation_issues.collect_deterministic_generation_issues(
            self.plans,
            trip_request=self.trip_request,
            retrieval=self.retrieval,
            route_plans=self.route_plans or [],
            budget_results=self.budget_results,
            composition_blueprints=self.composition_blueprints,
            weather_advisory_payload=self.weather_advisory_payload,
            attachment_auth_map=self.attachment_auth_map,
            accommodation=self.accommodation,
            body_semantics_by_review=self.generation_policy_version == "v0.12",
        )
        # Reuse the established structural scan. An extra speculative scan
        # must not shift a discovered problem into a phase that ignores it.
        # A changed body alone requires another scan before structural gates.
        before_pruning = self.plans
        self._prune_before_review(
            deterministic_issues=[*structural_issues, *handler.residual_findings])
        if self.plans is not before_pruning:
            structural_issues = generation_issues.collect_deterministic_generation_issues(
                self.plans,
                trip_request=self.trip_request,
                retrieval=self.retrieval,
                route_plans=self.route_plans or [],
                budget_results=self.budget_results,
                composition_blueprints=self.composition_blueprints,
                weather_advisory_payload=self.weather_advisory_payload,
                attachment_auth_map=self.attachment_auth_map,
                accommodation=self.accommodation,
                body_semantics_by_review=self.generation_policy_version == "v0.12",
            )
        structural_issues.extend(handler.residual_findings)
        invariants, incomplete, order_membership = classify_structural_reasons(
            structural_issues
        )
        budgets = self._dispatch_budgets()

        if incomplete or invariants or order_membership:
            structural_blockers = [
                *invariants,
                *incomplete,
                *order_membership,
            ]
            if (
                structural_blockers
                and all(
                    generation_issues_issue_reason(issue) == "route_outside_poi"
                    for issue in structural_blockers
                )
                and self._speculative_round()
                and self._draft_generator() == AdoptedGenerator.DS_FLASH.value
                and self._safe_policy_enabled("writer_failure")
            ):
                # The adopted standby produced a body, so _run_writer itself
                # succeeded. Treat an otherwise-isolated deterministic
                # membership rejection as an unusable Writer draft only when
                # the existing locked-input Writer fallback gate is available.
                self.metrics.resolver_notes = list(dict.fromkeys([
                    *self.metrics.resolver_notes,
                    "standby_route_outside_poi_safe_fallback",
                ]))
                return {"kind": "safe", "safe_trigger": "writer_failure"}
            decision = resolve_dispatch(
                structural_incomplete=incomplete,
                invariant_findings=invariants,
                structural_order_membership=order_membership,
                budgets=budgets,
            )
            self._validate_dispatch_decision(decision, structural_issues)
            return {
                "kind": "dispatch",
                "decision": decision,
                "taxonomy_issues": structural_issues,
            }

        self._run_activity_local_completion()
        pre_review_issues = generation_issues.collect_deterministic_generation_issues(
            self.plans,
            trip_request=self.trip_request,
            retrieval=self.retrieval,
            route_plans=self.route_plans or [],
            budget_results=self.budget_results,
            composition_blueprints=self.composition_blueprints,
            weather_advisory_payload=self.weather_advisory_payload,
            attachment_auth_map=self.attachment_auth_map,
            accommodation=self.accommodation,
            body_semantics_by_review=self.generation_policy_version == "v0.12",
        )
        hard_fact_issues = [
            issue
            for issue in pre_review_issues
            if issue.reason == "unsupported_fact_expansion"
            and issue.category in {"BLOCKER", "REPAIR"}
            and issue.publish_action == "REPAIR_PLAN"
        ]
        qualified_hard_facts = qualify_review_fragment_issues(
            self.plans,
            hard_fact_issues,
            route_plans=self.route_plans,
        )
        anchored_hard_facts = [
            issue
            for issue in qualified_hard_facts
            if (issue.metadata or {}).get("review_anchored") is True
        ]
        unanchored_hard_facts = [
            issue
            for issue in qualified_hard_facts
            if (issue.metadata or {}).get("review_anchored") is not True
        ]
        if unanchored_hard_facts:
            rejected = resolve_dispatch(
                review_issues=unanchored_hard_facts,
                budgets=self._dispatch_budgets(),
            )
            decision = DispatchDecision(
                action="FAIL_CLOSED",
                reasons=rejected.reasons,
                plan_indexes=rejected.plan_indexes,
                notes=[
                    *rejected.notes,
                    "pre-review hard fact has no unique stable action key",
                ],
            )
            try:
                self._validate_dispatch_decision(decision, unanchored_hard_facts)
            except publish_gate.PublishGateError as exc:
                if self.generation_policy_version == "v0.12":
                    raise _UnanchoredPreReviewHardFact(exc.result) from exc
                raise

        if anchored_hard_facts:
            decision = resolve_dispatch(
                review_issues=anchored_hard_facts,
                budgets=self._dispatch_budgets(),
            )
            if decision.action != "FRAGMENT_REPAIR":
                decision = DispatchDecision(
                    action="FAIL_CLOSED",
                    reasons=decision.reasons,
                    plan_indexes=decision.plan_indexes,
                    notes=[
                        *decision.notes,
                        "pre-review hard-fact fragment repair not admitted",
                    ],
                )
                self._validate_dispatch_decision(decision, anchored_hard_facts)
            self.metrics.resolver_notes = list(dict.fromkeys([
                *self.metrics.resolver_notes,
                "pre-review deterministic hard-fact fragment repair",
            ]))
            self.metrics.pre_review_fragment_repair_attempted = True
            return {
                "kind": "pre_review_fragment_repair",
                "decision": decision,
                "taxonomy_issues": anchored_hard_facts,
                "order_membership": order_membership,
            }

        preflight = collect_phrase_preflight(
            self.plans,
            route_plans=self.route_plans,
        )
        return {
            "kind": "review",
            "order_membership": order_membership,
            "preflight_issues": [item.to_issue() for item in preflight],
        }

    async def _classify_after_pre_review_fragment_repair(
        self,
        *,
        order_membership: list[generation_issues.GenerationIssue],
    ) -> dict[str, Any]:
        self._run_activity_local_completion()
        preflight = collect_phrase_preflight(
            self.plans,
            route_plans=self.route_plans,
        )
        return {
            "kind": "review",
            "order_membership": order_membership,
            "preflight_issues": [item.to_issue() for item in preflight],
        }

    async def _classify_post_review_content(
        self,
        *,
        taxonomy: generation_issues.IssueTaxonomyResult,
        order_membership: list[generation_issues.GenerationIssue],
        preflight_issues: list[generation_issues.GenerationIssue],
    ) -> dict[str, Any]:
        conflict_hints = {}
        qualified_review_issues = None
        if self.generation_policy_version == "v0.12":
            self._body_review = taxonomy.body_review
            if not self._body_review_current():
                return {"kind": "safe", "safe_trigger": "review_no_anchor",
                        "reason_code": "REVIEW_UNAVAILABLE"}
            self._next_review_issues = list(taxonomy.issues)
            qualified_review_issues = qualify_review_fragment_issues(
                self.plans, list(taxonomy.issues), route_plans=self.route_plans)
            # The existing qualifier may turn an unanchored hard claim into a
            # blocking action. Eligibility follows that final action while the
            # original model taxonomy/receipt and issue cache remain intact.
            pruned = await self._publish_after_sentence_pruning(
                taxonomy.model_copy(update={"issues": qualified_review_issues}))
            if pruned is not None:
                return pruned
            # v0.13 closeout: only an evidenced contradiction (both passages
            # quoted) can block or open a repair; "unknown" is recorded only.
            states = [row["status"] for key, row in self._body_review.assessments.items()
                      if key.startswith("consistency:") and row["status"] != "supported"]
            if states:
                self.metrics.extra["body_review_consistency_states"] = states
            if "missing" in states:
                conflict_hints = self._body_conflict_repair_hints()
                if not conflict_hints:
                    return {"kind": "safe", "safe_trigger": "fragment_repair_failed",
                            "reason_code": "REVIEW_ISSUES_UNRESOLVED"}
        self._record_pre_repair_metrics(taxonomy)
        review_skipped = bool(self.metrics.initial_review_skipped)
        self.metrics.review_ran = not review_skipped
        self.metrics.review_required_for_complete_body = not review_skipped
        self.metrics.full_candidate_review_skip_disabled = (
            self._ds_review_mandatory()
            or self.publish_retry_round > 0
            or not _review_risk_skip_enabled(get_settings())
        )

        if qualified_review_issues is None:
            qualified_review_issues = qualify_review_fragment_issues(
                self.plans, list(taxonomy.issues), route_plans=self.route_plans)
        high_risk_review_issues = [
            issue
            for issue in qualified_review_issues
            if issue.source == "llm_review"
            and issue.reason in REVIEW_HIGH_RISK_REASONS
        ]
        if (
            high_risk_review_issues
            and self._safe_policy_enabled("review_no_anchor")
        ):
            missing_fields = sorted({
                str(field)
                for issue in high_risk_review_issues
                for field in (
                    (issue.metadata or {}).get("review_missing_anchor_fields") or []
                )
            })
            self.metrics.merge_known_metrics({
                "review_reason": high_risk_review_issues[0].reason,
                "review_anchor_complete": all(
                    (issue.metadata or {}).get("review_anchored") is True
                    for issue in high_risk_review_issues
                ),
                "review_missing_anchor_fields": missing_fields,
            })
            return {"kind": "safe", "safe_trigger": "review_no_anchor"}

        # Unsupported facts already have a deterministic, evidence-owned
        # replacement operator.  Keep them on that path instead of asking a
        # Writer to invent another fragment and a Reviewer to approve it.  The
        # latter adds two remote failure points and can fail closed before the
        # deterministic operator is ever reached.
        keyed_review_issues = qualified_review_issues
        anchored_unsupported_facts = [
            issue
            for issue in qualified_review_issues
            if issue.reason == "unsupported_fact_expansion"
            and (issue.metadata or {}).get("review_anchored") is True
        ]
        if anchored_unsupported_facts:
            decision = resolve_dispatch(
                structural_order_membership=order_membership,
                preflight_findings=preflight_issues,
                review_issues=qualified_review_issues,
                budgets=self._dispatch_budgets(
                    allow_deterministic_fact_followup=True,
                ),
            )
            self._validate_dispatch_decision(decision, qualified_review_issues)
            if decision.action != "RECORD_ONLY":
                return {
                    "kind": "dispatch",
                    "decision": decision,
                    "qualified_review_issues": qualified_review_issues,
                }
            # Recording a soft fact warning does not close the independent
            # explicit-preference gate. Continue with the same issues/budget.
            # Preserve the resolved RECORD_ONLY treatment: these warnings
            # cannot independently create a new remote fragment repair.
            keyed_review_issues = [
                issue for issue in qualified_review_issues
                if issue not in anchored_unsupported_facts
            ]

        preference_report = self._explicit_preference_report()
        self._record_preference_report(preference_report, phase=(
            "after_repair" if self.fragment_repair_call_count > 0 else "before_repair"
        ))
        route_failures = [
            *preference_report.blocking_route_findings(),
            *preference_report.admission_block_findings(),
        ]
        if route_failures:
            return self._preference_fail_closed(preference_report, route_failures)

        keyed_soft_targets, keyed_soft_issue_codes = (
            self._review_keyed_soft_repair_targets(keyed_review_issues)
        )
        for key in conflict_hints:
            keyed_soft_issue_codes.setdefault(key, set()).add("body_review_conflict")
        keyed_soft_targets = list(dict.fromkeys([*keyed_soft_targets, *conflict_hints]))
        preference_codes = preference_report.issue_codes_by_key()
        preference_keys = list(preference_codes)
        if preference_keys:
            self.metrics.explicit_preference_repair_keys = [
                {
                    "plan_index": key[0],
                    "day": key[1],
                    "place_id": key[2],
                }
                for key in preference_keys
            ]
            if (
                self.metrics.explicit_preference_repair_attempted
                or self.metrics.review_keyed_fragment_repair_attempted
                or self.metrics.writer_keyed_fragment_repair_attempted
            ):
                self._record_preference_report(preference_report, phase="after_repair")
                self.metrics.explicit_preference_repair_failure_reason = (
                    "explicit_preference_still_missing"
                )
            for key, codes in preference_codes.items():
                keyed_soft_issue_codes.setdefault(key, set()).update(codes)
            keyed_soft_targets = list(dict.fromkeys(
                [*preference_keys, *keyed_soft_targets]
            ))
        copy_keys: set[FragmentKey] = set()
        if keyed_soft_targets:
            for issue in preflight_issues:
                if issue.reason != "placeholder_wording" or issue.plan_index is None:
                    continue
                zero = issue.plan_index - 1
                metadata = issue.metadata or {}
                if not 0 <= zero < len(self.plans):
                    continue
                start, end = metadata.get("start"), metadata.get("end")
                fragment = (fragment_for_span(self.plans[zero], start=start, end=end)
                            if isinstance(start, int) and isinstance(end, int) else None)
                if fragment is None:
                    if preference_keys:
                        return self._preference_fail_closed(
                            preference_report, preference_report.unresolved_body_findings(),
                            extra_reason="explicit_preference_no_fragment",
                        )
                    decision = resolve_dispatch(
                        structural_order_membership=order_membership,
                        preflight_findings=preflight_issues,
                        review_issues=qualified_review_issues,
                        budgets=self._dispatch_budgets(),
                    )
                    return {"kind": "dispatch", "decision": decision,
                            "qualified_review_issues": qualified_review_issues}
                key = (fragment.plan_index, fragment.day, fragment.place_id)
                copy_keys.add(key)
                keyed_soft_issue_codes.setdefault(key, set()).add(issue.reason)
                keyed_soft_targets = list(dict.fromkeys([*keyed_soft_targets, key]))
        if keyed_soft_targets:
            required_fact_keys = {
                key
                for key in keyed_soft_targets
                if "unsupported_fact_expansion"
                in keyed_soft_issue_codes.get(key, set())
            } | set(preference_keys) | copy_keys | set(conflict_hints)
            self.metrics.review_keyed_fragment_required_count = len(
                required_fact_keys
            )
            if preference_keys:
                self.metrics.explicit_preference_repair_attempted = True
            if (
                self.generation_policy_version == "v0.12"
                and self.fragment_repair_call_count > 0
                and not required_fact_keys
            ):
                # Ordinary RECORD_ONLY warnings may request best-effort repair,
                # but cannot veto a reviewed draft after its only round is used.
                # Continue through Dispatch and the final gate; required facts,
                # preferences and preflight copy repairs retain their old path.
                self.metrics.review_keyed_fragment_repair_failure_policy = (
                    "record_only_original"
                )
            else:
                if self.generation_policy_version == "v0.12" and not required_fact_keys:
                    self._remember_qualified_original(taxonomy)
                return {
                    "kind": "keyed_fragment_repair",
                    "qualified_review_issues": qualified_review_issues,
                    "keyed_soft_targets": keyed_soft_targets,
                    "keyed_soft_issue_codes": keyed_soft_issue_codes,
                    "required_fact_keys": required_fact_keys,
                }

        if self.generation_policy_version != "v0.12":
            self._sync_day_openings_after_preference_repair()
        decision = resolve_dispatch(
            structural_order_membership=order_membership,
            preflight_findings=preflight_issues,
            review_issues=qualified_review_issues,
            budgets=self._dispatch_budgets(),
        )
        self._validate_dispatch_decision(decision, qualified_review_issues)
        return {
            "kind": "dispatch",
            "decision": decision,
            "qualified_review_issues": qualified_review_issues,
        }

    async def _classify_after_keyed_fragment_repair(
        self,
        *,
        repaired: bool,
        required_fact_keys: set[FragmentKey],
        qualified_review_issues: list[generation_issues.GenerationIssue],
        taxonomy: generation_issues.IssueTaxonomyResult,
        order_membership: list[generation_issues.GenerationIssue],
        preflight_issues: list[generation_issues.GenerationIssue],
    ) -> dict[str, Any]:
        after_report = self._explicit_preference_report()
        self._record_preference_report(after_report, phase="after_repair")
        preference_unresolved = [
            *after_report.blocking_route_findings(),
            *after_report.admission_block_findings(),
            *after_report.unresolved_body_findings(),
        ]
        if preference_unresolved:
            self.metrics.explicit_preference_repair_failure_reason = (
                self.metrics.review_keyed_fragment_repair_failure_reason
                or preference_unresolved[0].reason_code
            )
            return self._preference_fail_closed(
                after_report,
                preference_unresolved,
                extra_reason=(
                    self.metrics.review_keyed_fragment_repair_failure_reason
                    or "explicit_preference_still_missing"
                ),
            )
        if not repaired and required_fact_keys:
            self.metrics.review_keyed_fragment_repair_failure_policy = (
                "fail_closed_required_fact"
            )
            decision = DispatchDecision(
                action="FAIL_CLOSED",
                reasons=["required_fact_fragment_repair_failed"],
                notes=[
                    self.metrics.review_keyed_fragment_repair_failure_reason
                    or "required fact fragment remained unresolved"
                ],
                plan_indexes=sorted({key[0] for key in required_fact_keys}),
            )
            self._validate_dispatch_decision(decision, qualified_review_issues)
        if not repaired:
            self.metrics.review_keyed_fragment_repair_failure_policy = (
                "record_only_original"
            )

        decision = resolve_dispatch(
            structural_order_membership=order_membership,
            preflight_findings=preflight_issues,
            review_issues=qualified_review_issues,
            budgets=self._dispatch_budgets(),
        )
        self._validate_dispatch_decision(decision, qualified_review_issues)
        return {
            "kind": "dispatch",
            "decision": decision,
            "qualified_review_issues": qualified_review_issues,
            "taxonomy": taxonomy,
        }

    async def _retry_empty_pre_review_draft(self) -> None:
        """One full Writer retry when the pre-Review draft is empty.

        Admission is Writer + Review + publish reserve, the same predicate as
        the inner Opus retry; a closed Review reserves nothing. A quota already
        spent by that inner retry is not granted again. A successful retry
        falls through into Review.
        """
        if self.plans or self.generation_policy_version != "v0.12":
            return
        if not self._speculative_round():
            return
        if self.metrics.writer_plan_retry_used or self._writer_plan_retry_remaining <= 0:
            self.metrics.merge_known_metrics({
                "writer_plan_retry_denied_reason": "retry_quota_consumed",
            })
            return
        settings = get_settings()
        writer_cost = (
            final_writer.STREAM_ATTEMPT_TIMEOUT_SECONDS
            if getattr(settings, "writer_stream_probe_enabled", False)
            else final_writer.ATTEMPT_TIMEOUT_SECONDS
        )
        if not retry_budget_admitted(
            self._residual_seconds(),
            writer_cost,
            yuntu_review.review_reserve_seconds(
                v012_policy=self.generation_policy_version == "v0.12"),
            self.residual_reserve_seconds,
        ):
            self.metrics.merge_known_metrics({
                "writer_plan_retry_denied_reason": "writer_review_reserve_insufficient",
            })
            return
        self._writer_plan_retry_remaining = 0
        await self._run_writer()
        self.metrics.writer_plan_retry_used = True
        self.metrics.writer_plan_retry_count = max(
            1, int(self.metrics.writer_plan_retry_count or 0)
        )

    async def run(
        self,
    ) -> tuple[list[PlanOutput], str, publish_gate.PublishGateResult, dict[str, Any]]:
        """Write → classify → (optional Review) → Dispatch → FR / gate.

        Whole-plan REPAIR_PLAN execution is bypassed. Only incomplete bodies may
        use outer Publish Retry; locked-order and content failures fail closed.
        Keyed copy whitelist issues use Fragment Repair.
        """
        if self.generation_policy_version == "v0.12":
            # v0.13 closeout (Q11): a preference the route has not arranged is
            # recorded, not a reason to skip the Writer. Arranging it is Route's
            # job (v0.14); a body claiming otherwise is a Review contradiction.
            self.metrics.extra["pre_writer_preference_decisions"] = route_preference_decisions(
                self.trip_request, self.route_plans or [])
        try:
            await self._run_writer()
            await self._retry_empty_pre_review_draft()
        except Exception as exc:
            logger.exception("writer_terminal_failure type=%s", type(exc).__name__)
            if self._safe_policy_enabled("writer_failure"):
                self.metrics.merge_known_metrics({
                    "writer_terminal_failure_type": type(exc).__name__,
                })
                return await self._render_v012_basic_or_legacy(
                    "writer_failure", "WRITER_UNAVAILABLE"
                )
            raise
        try:
            pre_review_state = await self._observe_content_validation(
                self._classify_pre_review_content,
                boundary="pre_review_classification",
                metadata={
                    "plan_count": len(self.plans),
                    "route_plan_count": len(self.route_plans or []),
                },
            )
        except _UnanchoredPreReviewHardFact:
            return await self._render_v012_basic_or_legacy(
                "fragment_repair_failed", "REVIEW_ISSUES_UNRESOLVED"
            )
        except publish_gate.PublishGateError:
            if self.generation_policy_version != "v0.12":
                raise
            return await self._render_v012_basic_or_legacy(
                "writer_failure", "WRITER_UNAVAILABLE"
            )
        if pre_review_state["kind"] == "safe":
            trigger = pre_review_state["safe_trigger"]
            # This branch has no publishable Writer draft, so Review did not run.
            # The same trigger string later means mandatory Review was unavailable.
            reason_code = (
                "WRITER_UNAVAILABLE"
                if trigger in {"writer_failure", SPECULATIVE_SAFE_TRIGGER}
                else "REVIEW_UNAVAILABLE"
            )
            return await self._render_v012_basic_or_legacy(trigger, reason_code)
        if pre_review_state["kind"] == "dispatch":
            return await self._finish_from_dispatch(
                pre_review_state["decision"],
                taxonomy_issues=pre_review_state["taxonomy_issues"],
            )
        if pre_review_state["kind"] == "pre_review_fragment_repair":
            if self.generation_policy_version == "v0.12":
                return await self._run_v012_fragment_repair_and_recheck(
                    pre_review_state["decision"],
                    pre_review_state["taxonomy_issues"],
                )
            await self._run_fragment_repair(
                pre_review_state["decision"],
                pre_review_state["taxonomy_issues"],
            )
            self.metrics.pre_review_fragment_repair_applied = True

            async def classify_after_pre_review_repair() -> dict[str, Any]:
                return await self._classify_after_pre_review_fragment_repair(
                    order_membership=pre_review_state["order_membership"],
                )

            pre_review_state = await self._observe_content_validation(
                classify_after_pre_review_repair,
                boundary="pre_review_classification_after_fragment_repair",
                metadata={"dispatch_action": "FRAGMENT_REPAIR"},
            )

        order_membership = pre_review_state["order_membership"]
        preflight_issues = pre_review_state["preflight_issues"]
        if self._llm_review_closed():
            return await self._finish_with_review_closed()
        try:
            taxonomy = await self._review_taxonomy(
                "REVIEW_TAXONOMY",
                metadata={
                    "pre_review_fragment_repair_attempted": (
                        self.metrics.pre_review_fragment_repair_attempted
                    ),
                    "pre_review_fragment_repair_applied": (
                        self.metrics.pre_review_fragment_repair_applied
                    ),
                    "fragment_repair_call_count": (
                        self.metrics.fragment_repair_call_count
                    ),
                    "fragment_repair_target_ids": list(
                        self.metrics.fragment_repair_target_ids
                    ),
                    "fragment_repair_failure_reason": (
                        self.metrics.fragment_repair_failure_reason
                    ),
                },
            )
        except DSMandatoryReviewUnavailable:
            return await self._render_v012_basic_or_legacy(
                SPECULATIVE_SAFE_TRIGGER, "REVIEW_UNAVAILABLE"
            )
        except GenerationModelCallError as exc:
            if self.generation_policy_version != "v0.12":
                raise
            detailed = await self._publish_technically_unreviewed(exc)
            if detailed is not None:
                return detailed
            return await self._render_v012_basic_or_legacy(
                "review_no_anchor", "REVIEW_UNAVAILABLE"
            )
        except (yuntu_review.ReviewSafetyError, ReviewLaunchBudgetUnavailable, asyncio.TimeoutError) as exc:
            if self.generation_policy_version != "v0.12":
                raise
            detailed = await self._publish_technically_unreviewed(exc)
            if detailed is not None:
                return detailed
            return await self._render_v012_basic_or_legacy(
                "review_no_anchor", "REVIEW_UNAVAILABLE"
            )
        except publish_gate.PublishGateError:
            if self.generation_policy_version != "v0.12":
                raise
            return await self._render_v012_basic_or_legacy(
                "review_no_anchor", "REVIEW_UNAVAILABLE"
            )
        # Review is a remote call and can consume a material share of the hard
        # workflow deadline. Dispatch must decide from the budget that remains
        # *after* Review, never from the pre-Review snapshot above.
        async def classify_post_review() -> dict[str, Any]:
            return await self._classify_post_review_content(
                taxonomy=taxonomy,
                order_membership=order_membership,
                preflight_issues=preflight_issues,
            )

        try:
            post_review_state = await self._observe_content_validation(
                classify_post_review,
                boundary="post_review_qualification_dispatch",
                metadata={"review_issue_count": len(taxonomy.issues)},
            )
        except publish_gate.PublishGateError:
            if self.generation_policy_version != "v0.12":
                raise
            return await self._render_v012_basic_or_legacy(
                "fragment_repair_failed", "REVIEW_ISSUES_UNRESOLVED"
            )
        if post_review_state["kind"] == "sentence_pruned":
            return post_review_state["result"]
        if post_review_state["kind"] == "safe":
            return await self._render_v012_basic_or_legacy(
                post_review_state["safe_trigger"],
                post_review_state.get("reason_code", "REVIEW_ISSUES_UNRESOLVED"),
            )
        if post_review_state["kind"] == "keyed_fragment_repair":
            if self.generation_policy_version == "v0.12":
                if self.fragment_repair_call_count > 0:
                    return await self._render_v012_basic_or_legacy(
                        "fragment_repair_failed",
                        "REVIEW_ISSUES_UNRESOLVED",
                    )
            repaired = await self._run_review_keyed_fragment_repair(
                keys=post_review_state["keyed_soft_targets"],
                issue_codes_by_key=post_review_state["keyed_soft_issue_codes"],
                required_keys=post_review_state["required_fact_keys"],
            )

            if self.generation_policy_version == "v0.12":
                if not repaired:
                    return await self._render_v012_basic_or_legacy(
                        "fragment_repair_failed",
                        "REVIEW_ISSUES_UNRESOLVED",
                    )
                return await self._v012_recheck_repaired_body()

            async def classify_after_keyed_repair() -> dict[str, Any]:
                return await self._classify_after_keyed_fragment_repair(
                    repaired=repaired,
                    required_fact_keys=post_review_state["required_fact_keys"],
                    qualified_review_issues=(
                        post_review_state["qualified_review_issues"]
                    ),
                    taxonomy=taxonomy,
                    order_membership=order_membership,
                    preflight_issues=preflight_issues,
                )

            post_review_state = await self._observe_content_validation(
                classify_after_keyed_repair,
                boundary="post_review_dispatch_after_fragment_repair",
                metadata={"dispatch_action": "FRAGMENT_REPAIR"},
            )
        return await self._finish_from_dispatch(
            post_review_state["decision"],
            taxonomy_issues=post_review_state["qualified_review_issues"],
            taxonomy=taxonomy,
        )

    def _safe_policy_enabled(self, trigger: str) -> bool:
        if self.locked_safe_input is None:
            return False
        if self.generation_policy_version == "v0.12":
            return True
        if trigger == SPECULATIVE_SAFE_TRIGGER:
            return True
        settings = get_settings()
        if trigger == "writer_failure":
            return bool(settings.safe_plan_writer_fallback_enabled)
        return bool(settings.safe_plan_review_fallback_enabled)

    async def _render_v012_basic_or_legacy(
        self,
        trigger: str,
        reason_code: str,
    ) -> tuple[list[PlanOutput], str, publish_gate.PublishGateResult, dict[str, Any]]:
        if self.generation_policy_version == "v0.12" and self._approved_original is not None:
            original = self._approved_original
            self._approved_original = None
            remaining = self._residual_seconds()
            if remaining is None or remaining > 0:
                old_plans, old_receipt, old_prior, old_taxonomy = original
                candidate_plans = self.plans
                self.plans, self._body_review, self._body_review_prior = old_plans, old_receipt, old_prior
                if self._body_review_current() and self._body_review_complete() and self._template_ratio_ok():
                    try:
                        gate = await self._run_publish_gate()
                    except publish_gate.PublishGateError:
                        gate = None
                    if gate is not None and self._body_review_current():
                        self.metrics.extra["optional_polish_failure_reason"] = reason_code
                        self.metrics.merge_known_metrics({"v012_review_state": "MODEL_PASSED",
                            "v012_basic_reason": None, "published_variant": "normal"})
                        self.metrics.review_keyed_fragment_repair_failure_policy = "record_only_original"
                        return self._plans_without_food_markers(), self._format_review_notes(old_taxonomy), gate, self.metrics.to_dict()
                self.plans = candidate_plans
        if self.generation_policy_version == "v0.12":
            self.metrics.merge_known_metrics({
                "v012_review_state": (
                    "NOT_RUN" if reason_code in {"WRITER_UNAVAILABLE", "PREFERENCE_UNVERIFIED"}
                    else "UNAVAILABLE" if reason_code == "REVIEW_UNAVAILABLE"
                    else "ISSUES_UNRESOLVED"
                ),
                "v012_basic_reason": reason_code,
            })
        return await self._render_safe(trigger)

    def _refresh_locked_safe_input(self) -> None:
        try:
            self.locked_safe_input = lock_safe_input(
                route_plans=self.route_plans,
                action_contracts=self.structured_evidence_payload.action_plan,
                food_authorizations=self.attachment_auth_map.values(),
                trip_request=self.trip_request,
            )
        except SafeRenderError:
            self.locked_safe_input = None

    def _restore_preference_notices(self) -> None:
        """Insert program sentences before a Review snapshot is taken.

        Repair that rewrites a day still comes back through pre-review
        classification, which calls this again before the next snapshot.
        The publish gate must not call it: that check runs after the snapshot.
        """
        from src.agents.preference_notices import apply_preference_notices, plan_uses_program_hints

        if not self.plans or not self.route_plans or self.trip_request is None:
            return
        restored: list[PlanOutput] = []
        for index, plan in enumerate(self.plans):
            route = self.route_plans[index] if index < len(self.route_plans) else None
            if route is None:
                restored.append(plan)
                continue
            restored.append(apply_preference_notices(
                plan,
                route,
                self.trip_request,
                program_hints=plan_uses_program_hints(plan),
            ))
        self.plans = restored

    async def _render_safe(
        self,
        trigger: str,
    ) -> tuple[list[PlanOutput], str, publish_gate.PublishGateResult, dict[str, Any]]:
        remaining = self._residual_seconds()
        if remaining is not None and remaining <= 0:
            raise SafeRenderError("workflow_deadline_exhausted")

        if self._speculative_round() and self._speculative_adopted_archive_records:
            records = list(self._speculative_adopted_archive_records)
            self._speculative_adopted_archive_records.clear()
            pending_succeeded = await final_writer.archive_speculative_records(
                records
            )
            previous_succeeded = _metric_value(
                self.metrics,
                "archive_write_succeeded",
                None,
            )
            if pending_succeeded is not None:
                self.metrics.merge_known_metrics({
                    "archive_write_succeeded": (
                        pending_succeeded
                        if previous_succeeded is None
                        else bool(previous_succeeded and pending_succeeded)
                    )
                })

        rendered: SafePlanRenderResult | None = None
        safe_metrics: dict[str, Any] = {
            "published_variant": "safe",
            "delivery_status": "DEGRADED",
            "safe_trigger": trigger,
            "safe_renderer_attempted": True,
            "safe_renderer_postcheck_passed": False,
        }
        if self._speculative_round():
            safe_metrics["generator"] = AdoptedGenerator.SAFE.value
            safe_metrics["writer_prompt_version"] = ""
        self.metrics.merge_known_metrics(safe_metrics)

        async def action() -> SafePlanRenderResult:
            return render_safe_plans(
                locked_input=self.locked_safe_input,
            )

        try:
            rendered = await _run_observed_step(
                "SAFE_PLAN_RENDERER",
                action,
                on_stage_event=self.on_stage_event,
                set_current_stage=False,
                attempt=self.attempt,
                publish_retry_round=self.publish_retry_round,
                metadata={"safe_trigger": trigger},
                finish_metadata=lambda: {
                    "safe_trigger": trigger,
                    "safe_renderer_latency_ms": (
                        rendered.latency_ms if rendered is not None else 0
                    ),
                    "safe_renderer_output_sha256": (
                        rendered.output_sha256 if rendered is not None else ""
                    ),
                },
            )
        except SafeRenderError:
            raise
        except Exception as exc:
            raise SafeRenderError(type(exc).__name__) from exc

        self.plans = self._attach_safe_plan_metadata(rendered.plans)
        self.metrics.merge_known_metrics({
            "safe_renderer_latency_ms": rendered.latency_ms,
            "safe_renderer_input_sha256": rendered.input_sha256,
            "safe_renderer_output_sha256": rendered.output_sha256,
        })
        try:
            publish_result = await self._run_publish_gate()
        except publish_gate.PublishGateError:
            self.metrics.merge_known_metrics({
                "safe_renderer_postcheck_passed": False,
                "publish_gate_passed": False,
            })
            raise
        self.metrics.merge_known_metrics({
            "safe_renderer_postcheck_passed": True,
            "publish_gate_passed": True,
        })
        return self.plans, "", publish_result, self.metrics.to_dict()

    def _attach_safe_plan_metadata(
        self,
        plans: list[PlanOutput],
    ) -> list[PlanOutput]:
        """Project non-rendered structured metadata after Safe rendering."""
        attached: list[PlanOutput] = []
        for plan_index, plan in enumerate(plans):
            attached.append(plan.model_copy(update={
                "composition_blueprint": (
                    self.composition_blueprints[plan_index]
                    if self.composition_blueprints
                    and plan_index < len(self.composition_blueprints)
                    else None
                ),
                "poi_identity_result": (
                    self.poi_identity_results[plan_index]
                    if self.poi_identity_results
                    and plan_index < len(self.poi_identity_results)
                    else None
                ),
                "budget_result": (
                    self.budget_results[plan_index]
                    if self.budget_results
                    and plan_index < len(self.budget_results)
                    else None
                ),
                "accommodation": self.accommodation,
                "transport": self.transport,
            }))
        return attached

    def _llm_review_closed(self) -> bool:
        """W32: the switch governs the v0.12 flow, the one orders run on."""
        return (self.generation_policy_version == "v0.12"
                and not getattr(get_settings(), "llm_review_enabled", True))

    async def _finish_with_review_closed(self, *, stage="REVIEW_TAXONOMY"):
        """W32: no Review call. The code gates alone decide detailed or basic.

        Same gates as W9, but recorded as a closed Review, not an outage.
        A draft that fails them goes to the basic plan as a content
        problem; there is no Review finding to repair or recheck.
        """
        evidence = self._code_delivery_evidence()
        checks = dict(evidence["gate_checks"])
        preferences = self._explicit_preference_report()
        checks["explicit_preference_gate_passed"] = not (
            preferences.blocking_route_findings() or preferences.admission_block_findings())
        checks["writer_template_ratio_passed"] = self._template_ratio_ok()
        publish_result = None
        if all(checks.values()):
            try:
                publish_result = await self._run_publish_gate()
                plans = self._plans_without_food_markers()
            except publish_gate.PublishGateError:
                publish_result = None
            checks["final_publish_gate_passed"] = bool(publish_result and publish_result.passed)
        self.metrics.review_ran = False
        self.metrics.publish_preflight_passed = evidence["publish_preflight"].passed
        self.metrics.blueprint_integrity_passed = not evidence["blueprint_violations"]
        self.metrics.merge_known_metrics({
            "llm_review_enabled": False,
            "review_taxonomy_status": "disabled",
            "review_disabled_stage": stage,
            "review_disabled_failed_checks": sorted(
                name for name, passed in checks.items() if not passed),
            "review_disabled_hard_risk_flags": evidence["hard_risk_flags"],
        })
        if not all(checks.values()):
            # Keep what the gates saw so a basic delivery can be explained
            # later (W15 had three whose only failed check was a risk flag).
            # Internal metrics; the result projection never reads these keys.
            self.metrics.merge_known_metrics({
                "review_disabled_risk_flags": evidence["shadow_metadata"].get("risk_flags") or [],
                "review_disabled_preflight_failure_reasons": evidence["publish_preflight"].failure_reasons,
                "review_disabled_issue_details": _taxonomy_step_metadata(evidence["taxonomy"])["issue_details"],
                "review_disabled_failed_draft": [
                    p.model_dump(mode="json", include=_FAILED_DRAFT_FIELDS) for p in self.plans],
            })
            return await self._render_v012_basic_or_legacy(
                "fragment_repair_failed", "REVIEW_ISSUES_UNRESOLVED"
            )
        # The worker checks this receipt against the exact delivered prose.
        from src.agents.review_availability import code_only_body_fingerprint
        self.metrics.merge_known_metrics({
            "v012_review_state": "CODE_ONLY", "review_status": "unreviewed",
            "published_variant": "normal", "delivery_status": "NORMAL", "safe_trigger": None,
            "v015_unreviewed_receipt": {
                "version": 1, "cause": "review_disabled",
                "body_sha256": code_only_body_fingerprint(plans),
                "code_gates": checks,
                "prior_findings_remaining": False,
                "stage": stage,
            },
        })
        return plans, "", publish_result, self.metrics.to_dict()

    async def _publish_technically_unreviewed(self, exc: Exception, *, stage="REVIEW_TAXONOMY"):
        """W9: publish the current draft only after every code gate passes."""
        if self.generation_policy_version != "v0.12" or not _technical_review_failure(exc):
            return None
        if self._approved_original is not None:
            # Optional polishing already has a fully reviewed fallback. Let the
            # existing path restore and revalidate that exact approved draft.
            return None
        # A previous valid finding is still a finding. Recheck unavailability
        # cannot erase it; require an actual edit removing each offending span.
        prior = list(self._body_review_prior or self._next_review_issues)
        current_text = "\n".join(p.plan_text + "\n" + p.summary for p in self.plans)
        def substantive_text(text):
            return "".join(c for c in unicodedata.normalize("NFKC", text).casefold()
                           if not unicodedata.category(c).startswith(("P", "Z", "C")))
        current_substance = substantive_text(current_text)
        for issue in prior:
            if issue.category in {"BLOCKER", "REPAIR"} or issue.publish_action != "RECORD_ONLY":
                snippet = substantive_text(issue.snippet)
                if not snippet or snippet in current_substance:
                    return None
        if self._body_review is not None:
            for key, row in self._body_review.assessments.items():
                if key.startswith("consistency:") and row["status"] == "missing":
                    quotes = [item.get("quote", "") for item in row.get("evidence", [])]
                    if not quotes or all(substantive_text(quote) and substantive_text(quote) in current_substance for quote in quotes):
                        return None
        _, metadata, _ = self._review_transport_fallback(exc, "transport", type(exc).__name__)
        checks = dict(metadata["review_transport_gate_checks"])
        preferences = self._explicit_preference_report()
        checks["explicit_preference_gate_passed"] = not (
            preferences.blocking_route_findings() or preferences.admission_block_findings())
        checks["writer_template_ratio_passed"] = self._template_ratio_ok()
        if not all(checks.values()):
            return None
        try:
            publish_result = await self._run_publish_gate()
            plans = self._plans_without_food_markers()
        except publish_gate.PublishGateError:
            return None
        # The worker checks this receipt against the exact delivered prose.
        from src.agents.review_availability import code_only_body_fingerprint
        self.metrics.merge_known_metrics({
            "v012_review_state": "CODE_ONLY", "review_status": "unreviewed",
            "published_variant": "normal", "delivery_status": "NORMAL", "safe_trigger": None,
            "review_transport_fail_closed": False,
            "review_transport_fail_open_allowed": True,
            "review_transport_fail_open_reason": "deterministic_gates_passed",
            "review_taxonomy_status": "unreviewed",
            "v015_unreviewed_receipt": {
                "version": 1, "cause": "technical_unavailable",
                "body_sha256": code_only_body_fingerprint(plans),
                "code_gates": {**checks, "final_publish_gate_passed": publish_result.passed},
                "prior_findings_remaining": False,
                "stage": stage,
            },
        })
        return plans, "", publish_result, self.metrics.to_dict()

    async def _v012_recheck_repaired_body(
        self,
    ) -> tuple[list[PlanOutput], str, publish_gate.PublishGateResult, dict[str, Any]]:
        """Hard-check a changed body, then run its own primary/backup review."""
        try:
            pre_review = await self._observe_content_validation(
                self._classify_pre_review_content,
                boundary="v012_post_repair_hard_validation",
                metadata={"fragment_repair_call_count": self.fragment_repair_call_count},
            )
        except publish_gate.PublishGateError:
            return await self._render_v012_basic_or_legacy(
                "fragment_repair_failed", "REVIEW_ISSUES_UNRESOLVED"
            )
        if pre_review.get("kind") != "review":
            return await self._render_v012_basic_or_legacy(
                "fragment_repair_failed", "REVIEW_ISSUES_UNRESOLVED"
            )
        if self._llm_review_closed():
            return await self._finish_with_review_closed(stage="REVIEW_TAXONOMY_AFTER_REPAIR")
        try:
            taxonomy = await self._review_taxonomy(
                "REVIEW_TAXONOMY_AFTER_REPAIR"
            )
        except (GenerationModelCallError, yuntu_review.ReviewSafetyError,
                ReviewLaunchBudgetUnavailable, asyncio.TimeoutError,
                publish_gate.PublishGateError) as exc:
            detailed = await self._publish_technically_unreviewed(exc, stage="REVIEW_TAXONOMY_AFTER_REPAIR")
            if detailed is not None:
                return detailed
            return await self._render_v012_basic_or_legacy(
                "review_no_anchor", "REVIEW_UNAVAILABLE"
            )
        try:
            post_review = await self._observe_content_validation(
                lambda: self._classify_post_review_content(
                    taxonomy=taxonomy,
                    order_membership=pre_review["order_membership"],
                    preflight_issues=pre_review["preflight_issues"],
                ),
                boundary="v012_post_repair_model_recheck",
                metadata={"review_issue_count": len(taxonomy.issues)},
            )
        except publish_gate.PublishGateError:
            return await self._render_v012_basic_or_legacy(
                "fragment_repair_failed", "REVIEW_ISSUES_UNRESOLVED"
            )
        if post_review.get("kind") == "sentence_pruned":
            return post_review["result"]
        if post_review.get("kind") != "dispatch":
            return await self._render_v012_basic_or_legacy(
                post_review.get("safe_trigger", "fragment_repair_failed"),
                post_review.get("reason_code", "REVIEW_ISSUES_UNRESOLVED")
            )
        decision = post_review["decision"]
        if decision.action in {"FRAGMENT_REPAIR", "PUBLISH_RETRY"}:
            return await self._render_v012_basic_or_legacy(
                "fragment_repair_failed", "REVIEW_ISSUES_UNRESOLVED"
            )
        try:
            return await self._finish_from_dispatch(
                decision,
                taxonomy_issues=post_review["qualified_review_issues"],
                taxonomy=taxonomy,
            )
        except publish_gate.PublishGateError:
            return await self._render_v012_basic_or_legacy(
                "fragment_repair_failed", "REVIEW_ISSUES_UNRESOLVED"
            )

    async def _run_v012_fragment_repair_and_recheck(
        self,
        decision: DispatchDecision,
        taxonomy_issues: list[generation_issues.GenerationIssue],
    ) -> tuple[list[PlanOutput], str, publish_gate.PublishGateResult, dict[str, Any]]:
        if not self._reserve_v012_repair_round():
            return await self._render_v012_basic_or_legacy(
                "fragment_repair_failed", "REVIEW_ISSUES_UNRESOLVED"
            )
        self.metrics.merge_known_metrics({"fragment_repair_attempted": True})
        try:
            initial_report = self._explicit_preference_report()
            self._record_preference_report(initial_report, phase="before_repair")
            admission_failures = [*initial_report.blocking_route_findings(),
                                  *initial_report.admission_block_findings()]
            if admission_failures:
                self.metrics.explicit_preference_repair_failure_reason = admission_failures[0].reason_code
                return await self._render_v012_basic_or_legacy(
                    "fragment_repair_failed", "REVIEW_ISSUES_UNRESOLVED"
                )
            # Save this admitted round's work before a deterministic edit makes
            # the Review snapshot stale. Reviewed unknown is never reclassified
            # as a pending writing requirement after that edit.
            preference_codes = initial_report.issue_codes_by_key()
            preference_hints = initial_report.repair_hints_by_key()
            conflict_hints = self._body_conflict_repair_hints()
            if self._body_review is None:
                preference_codes, preference_hints = initial_report.early_writer_requirements(
                    repair_triggered=True,
                )
            body_elements = {
                key: ((preference_hints[key],) if key in preference_hints else ())
                + tuple(conflict_hints.get(key, ()))
                for key in set(preference_hints) | set(conflict_hints)
            }
            codes = set(decision.fragment_issue_codes)
            deterministic = bool(codes & {
                "unsupported_fact_expansion", "food_tier_exceeded", "food_source_attribution",
            })
            if deterministic:
                # Existing zero-model operators prepare the same round. Their
                # cost remains under the absolute job deadline; no second
                # round or remote call is opened for their residuals.
                await self._run_fragment_repair(
                    decision, taxonomy_issues, _within_v012_round=True,
                )
                if self.metrics.pre_review_fragment_repair_attempted:
                    self.metrics.pre_review_fragment_repair_applied = True
            self._run_activity_local_completion()
            report = self._explicit_preference_report()
            if report.blocking_route_findings() or report.admission_block_findings():
                self._record_preference_report(report, phase="before_repair")
                return await self._render_v012_basic_or_legacy(
                    "fragment_repair_failed", "REVIEW_ISSUES_UNRESOLVED"
                )
            admitted_keys = {
                item.key for item in report.findings
                if item.layer == "body" and item.fragment_registered
                and item.arrangement_supported and item.action_authorized
            }
            if set(preference_codes) - admitted_keys:
                return await self._render_v012_basic_or_legacy(
                    "fragment_repair_failed", "REVIEW_ISSUES_UNRESOLVED"
                )
            combined = {key: set(value) for key, value in preference_codes.items()}
            for key in conflict_hints:
                combined.setdefault(key, set()).add("body_review_conflict")
            # Recompute deterministic copy residuals on the edited registry.
            # Copy-only legacy calls keep their original operator below.
            if deterministic or combined:
                copy_codes = {"placeholder_wording", "database_tone"} if deterministic else codes
                for finding in collect_phrase_preflight(self.plans, route_plans=self.route_plans):
                    if finding.reason not in copy_codes:
                        continue
                    plan = self.plans[finding.plan_index - 1]
                    fragment = fragment_for_span(plan, start=finding.start, end=finding.end)
                    if fragment is None:
                        return await self._render_v012_basic_or_legacy(
                            "fragment_repair_failed", "REVIEW_ISSUES_UNRESOLVED"
                        )
                    key = (fragment.plan_index, fragment.day, fragment.place_id)
                    combined.setdefault(key, set()).add(finding.reason)
            if combined:
                self._record_preference_report(report, phase="before_repair")
                if preference_codes:
                    self.metrics.explicit_preference_repair_attempted = True
                    self.metrics.explicit_preference_repair_keys = [
                        {"plan_index": key[0], "day": key[1], "place_id": key[2]}
                        for key in preference_codes
                    ]
                if not await self._run_review_keyed_fragment_repair(
                    keys=list(combined), issue_codes_by_key=combined,
                    required_keys=set(combined),
                    required_body_elements_by_key=body_elements,
                ):
                    return await self._render_v012_basic_or_legacy(
                        "fragment_repair_failed", "REVIEW_ISSUES_UNRESOLVED"
                    )
            else:
                if not self._start_v012_repair_phase(min_writer_seconds=0.0 if deterministic else 30.0):
                    return await self._render_v012_basic_or_legacy(
                        "fragment_repair_failed", "REVIEW_ISSUES_UNRESOLVED"
                    )
                if not deterministic:
                    await self._await_v012_repair(
                        self._run_fragment_repair(decision, taxonomy_issues, _within_v012_round=True),
                        cap=30.0,
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            return await self._render_v012_basic_or_legacy(
                "fragment_repair_failed", "REVIEW_ISSUES_UNRESOLVED"
            )
        return await self._v012_recheck_repaired_body()

    async def _finish_from_dispatch(
        self,
        decision: DispatchDecision,
        *,
        taxonomy_issues: list[generation_issues.GenerationIssue],
        taxonomy: generation_issues.IssueTaxonomyResult | None = None,
    ) -> tuple[list[PlanOutput], str, publish_gate.PublishGateResult, dict[str, Any]]:
        self.dispatch_decision = decision
        self.metrics.dispatch_action = decision.action
        self.metrics.dispatch_reasons = list(decision.reasons)
        self.metrics.dispatch_notes = list(decision.notes)
        self.metrics.fragment_repair_call_count = self.fragment_repair_call_count

        if decision.action == "FAIL_CLOSED":
            if self.generation_policy_version == "v0.12":
                return await self._render_v012_basic_or_legacy(
                    "fragment_repair_failed", "REVIEW_ISSUES_UNRESOLVED"
                )
            findings = _taxonomy_findings(
                taxonomy_issues[:20],
                prefix="dispatch fail-closed",
            )
            raise publish_gate.PublishGateError(
                publish_gate.PublishGateResult(passed=False, findings=findings)
            )

        if decision.action == "PUBLISH_RETRY":
            if self.generation_policy_version == "v0.12":
                return await self._render_v012_basic_or_legacy(
                    "writer_failure", "WRITER_UNAVAILABLE"
                )
            raise ContentDispatchSignal(
                decision,
                metrics=self.metrics.to_dict(),
                plans=self.plans,
                review_notes=self._format_review_notes(
                    taxonomy or generation_issues.resolve_generation_issues(taxonomy_issues)
                ),
            )

        if decision.action == "FRAGMENT_REPAIR":
            if self.generation_policy_version == "v0.12":
                return await self._run_v012_fragment_repair_and_recheck(
                    decision, taxonomy_issues
                )
            self.metrics.merge_known_metrics({"fragment_repair_attempted": True})
            try:
                await self._run_fragment_repair(decision, taxonomy_issues)
            except Exception:
                if self._safe_policy_enabled("fragment_repair_failed"):
                    return await self._render_v012_basic_or_legacy(
                        "fragment_repair_failed", "REVIEW_ISSUES_UNRESOLVED"
                    )
                raise
            # FR owns only a narrow copy fragment and may remove an otherwise
            # valid action sentence. Re-close keyed gaps deterministically;
            # never escalate an Activity residual into another LLM call.
            self._run_activity_local_completion()
            # Legacy policy keeps its historical final-gate-only behavior.
            try:
                publish_result = await self._run_publish_gate()
            except publish_gate.PublishGateError:
                if self._safe_policy_enabled("fragment_repair_failed"):
                    return await self._render_v012_basic_or_legacy(
                        "fragment_repair_failed", "REVIEW_ISSUES_UNRESOLVED"
                    )
                raise
            notes = self._format_review_notes(
                taxonomy or generation_issues.resolve_generation_issues(taxonomy_issues)
            )
            return self.plans, notes, publish_result, self.metrics.to_dict()

        # RECORD_ONLY or clean path → final publish gate and exact reviewed version.
        if self.generation_policy_version == "v0.12":
            if not self._body_review_current():
                return await self._render_v012_basic_or_legacy("review_no_anchor", "REVIEW_UNAVAILABLE")
            if not self._body_review_complete() or not self._template_ratio_ok():
                return await self._render_v012_basic_or_legacy("fragment_repair_failed", "REVIEW_ISSUES_UNRESOLVED")
        publish_result = await self._run_publish_gate()
        if self.generation_policy_version == "v0.12" and not self._body_review_current():
            return await self._render_v012_basic_or_legacy("review_no_anchor", "REVIEW_UNAVAILABLE")
        notes = self._format_review_notes(
            taxonomy or generation_issues.resolve_generation_issues(taxonomy_issues)
        )
        return self._plans_without_food_markers(), notes, publish_result, self.metrics.to_dict()

    async def _run_fragment_repair(
        self,
        decision: DispatchDecision,
        taxonomy_issues: list[generation_issues.GenerationIssue],
        *,
        _within_v012_round: bool = False,
    ) -> None:
        issue_codes = set(
            decision.fragment_issue_codes
            or ["database_tone", "placeholder_wording"]
        )
        deterministic_unsupported_fact = (
            "unsupported_fact_expansion" in issue_codes
        )
        max_repair_passes = (
            1 if self.generation_policy_version == "v0.12"
            else 2 if deterministic_unsupported_fact else 1
        )
        owns_round = (_within_v012_round and self.generation_policy_version == "v0.12"
                      and self._v012_repair_round_active and self.fragment_repair_call_count == 1)
        if (not owns_round and (self.fragment_repair_call_count >= max_repair_passes
                or (self.generation_policy_version == "v0.12"
                    and self.metrics.writer_keyed_fragment_repair_attempted))):
            raise ContentDispatchSignal(
                DispatchDecision(
                    action="FAIL_CLOSED",
                    reasons=decision.reasons,
                    notes=["fragment_repair_call_count exhausted"],
                ),
                metrics=self.metrics.to_dict(),
                plans=self.plans,
            )
        if not self.plans or not self.route_plans:
            return
        # First slice: repair plan 1 (or first decided plan index).
        plan_index = decision.plan_indexes[0] if decision.plan_indexes else 1
        zero = plan_index - 1
        if zero < 0 or zero >= len(self.plans) or zero >= len(self.route_plans):
            zero = 0
            plan_index = 1
        food_repair_codes = issue_codes & {
            "food_tier_exceeded",
            "food_source_attribution",
        }
        if food_repair_codes:
            food_result = generation_issues.repair_food_review_plan(
                self.plans[zero],
                plan_index=plan_index,
                attachment_auth_map=self.attachment_auth_map,
                issues=taxonomy_issues,
            )
            result = FragmentRepairResult(
                plans=food_result.repaired_plans,
                applied=food_result.success,
                failure_reason=(
                    "unknown" if food_result.success else "postcheck_failed"
                ),
                notes=[
                    "deterministic food authorization repair",
                    *food_result.failure_reasons,
                ],
                target_ids=[
                    (
                        f"food:plan{plan_index}:day{issue.day}:"
                        f"anchor{issue.place_id}:{issue.reason}"
                    )
                    for issue in taxonomy_issues
                    if issue.plan_index == plan_index
                    and issue.reason in food_repair_codes
                ],
            )
        elif deterministic_unsupported_fact:
            result = deterministic_unsupported_fact_fragment_repair(
                self.plans[zero],
                route_plan=self.route_plans[zero],
                issues=taxonomy_issues,
                plan_index=plan_index,
                structured_evidence_payload=self.structured_evidence_payload,
            )
        else:
            if owns_round and not self._claim_v012_repair_remote():
                raise RuntimeError("repair_round_exhausted")
            from src.agents.llm import llm_call_context
            generation_index = int(self.publish_retry_round or 0)
            with llm_call_context(
                stage="FRAGMENT_REPAIR",
                generation_index=generation_index,
                publish_retry_round=self.publish_retry_round,
                attempt=self.attempt,
            ):
                repair_kwargs = {
                    "route_plan": self.route_plans[zero],
                    "plan_index": plan_index,
                    "issue_codes": issue_codes,
                }
                if self.generation_policy_version == "v0.12":
                    repair_kwargs["generator"] = self._draft_generator()
                result = await maybe_fragment_repair_plan(
                    self.plans[zero],
                    **repair_kwargs,
                )
        if not owns_round:
            self.fragment_repair_call_count += 1
        self.metrics.fragment_repair_call_count = self.fragment_repair_call_count
        self.metrics.fragment_repair_failure_reason = result.failure_reason
        self.metrics.fragment_repair_target_ids = list(dict.fromkeys([
            *self.metrics.fragment_repair_target_ids,
            *result.target_ids,
        ]))
        if result.notes:
            self.metrics.resolver_notes = list(dict.fromkeys(
                [*self.metrics.resolver_notes, *result.notes]
            ))
        if not result.applied:
            # A failed local content operator proves this fragment cannot be
            # closed deterministically. Whole-plan Writer resampling is not a
            # valid fallback for any content family.
            denial = (
                "unsupported_fact_fragment_repair_failed"
                if deterministic_unsupported_fact
                else "fragment_repair_failed_no_whole_plan_retry"
            )
            raise ContentDispatchSignal(
                DispatchDecision(
                    action="FAIL_CLOSED",
                    reasons=[result.failure_reason or "fragment_repair_failed"],
                    notes=result.notes + [denial],
                    plan_indexes=decision.plan_indexes,
                ),
                metrics=self.metrics.to_dict(),
                plans=self.plans,
            )
        self.plans[zero] = result.plans[0]
        self.repaired_plan_indexes = list(dict.fromkeys(
            [*self.repaired_plan_indexes, plan_index]
        ))

    def _body_context(self, plans=None):
        return build_body_review_context(trip_request=self.trip_request,
            route_plans=self.route_plans or [], plans=self.plans if plans is None else plans,
            structured_evidence_payload=self.structured_evidence_payload,
            previous_issues=self._body_review_prior)

    def _body_review_current(self, plans=None):
        if self._body_review is None:
            return False
        try:
            return body_review_matches(self._body_review, self._body_context(plans))
        except (ValueError, TypeError):
            return False

    def _body_conflict_repair_hints(self):
        """Locate explicit paired evidence, without reinterpreting its meaning.

        Only already registered POI paragraphs are repairable here. An unknown
        result opens no repair; an unlocatable contradiction cannot open a
        guessed repair.
        """
        if not self._body_review_current():
            return {}
        hints = {}
        for requirement_id, row in self._body_review.assessments.items():
            if not requirement_id.startswith("consistency:") or row["status"] != "missing":
                continue
            index = int(requirement_id.split(":")[1])
            plan = self.plans[index - 1]
            keys = set()
            for ref in row["evidence"]:
                if ref["source"] == f"summary:{index}":
                    return {}  # Summary has no registered POI repair owner.
                if ref["source"] != f"plan:{index}":
                    continue  # Locked arrangements remain immutable.
                owners = [f for f in plan.poi_fragments if ref["quote"] in f.text]
                if len(owners) != 1:
                    return {}
                fragment = owners[0]
                keys.add((fragment.plan_index, fragment.day, fragment.place_id))
            if not keys:
                return {}
            instruction = "解决审核定位的正文矛盾；保留原路线、合理妥协和事实授权，不添加外部事实。" + json.dumps(
                {"reason": row["reason"], "evidence": row["evidence"]}, ensure_ascii=False)
            for key in keys:
                hints.setdefault(key, []).append(instruction)
        return hints

    def _body_review_complete(self):
        """A current receipt without an evidenced contradiction, on a qualified route."""
        if not self._body_review_current():
            return False
        if any(row["status"] == "missing" for key, row in self._body_review.assessments.items()
               if key.startswith("consistency:")):
            return False
        report = self._explicit_preference_report()
        return not (report.blocking_route_findings() or report.admission_block_findings())

    def _template_ratio_ok(self):
        """Recheck Writer-owned missing keys, using the original per-plan denominator.

        deterministic_completion is shared by fact correction and is not a
        Writer missing-fragment ledger. Never infer fallback ownership from it.
        """
        writer_keys = [(item["plan_index"], item["day"], item["place_id"])
                       for item in self.metrics.writer_keyed_fragment_fallback_keys]
        for index, plan in enumerate(self.plans, 1):
            fallbacks = [key for key in writer_keys if key[0] == index]
            if fallback_ratio_exceeded(fallbacks, total_fragment_count=len(plan.poi_fragments)):
                return False
        return True

    def _remember_qualified_original(self, taxonomy):
        if (self._body_review_complete() and self._template_ratio_ok()
                and self._check_publish_gate().passed
                and not any(i.category in {"REPAIR", "BLOCKER"} or i.publish_action != "RECORD_ONLY"
                            for i in taxonomy.issues)):
            self._approved_original = ([p.model_copy(deep=True) for p in self.plans],
                self._body_review.model_copy(deep=True), list(self._body_review_prior),
                taxonomy.model_copy(deep=True))

    def _explicit_preference_report(
        self,
        plans: list[PlanOutput] | None = None,
    ) -> PreferenceFulfillmentReport:
        return evaluate_explicit_preference_fulfillment(
            trip_request=self.trip_request,
            route_plans=self.route_plans or [],
            plans=plans if plans is not None else self.plans,
            structured_evidence_payload=self.structured_evidence_payload,
            retrieval=self.retrieval,
            body_review_required=self.generation_policy_version == "v0.12",
        )

    def _record_preference_report(
        self,
        report: PreferenceFulfillmentReport,
        *,
        phase: str,
    ) -> None:
        reasons = [item.reason_code for item in report.findings]
        keys = [
            {"plan_index": item.plan_index, "day": item.day, "place_id": item.place_id}
            for item in report.repairable_body_findings()
        ]
        payload = {
            "explicit_preference_evaluated": True,
            "explicit_preference_dimensions": list(report.dimensions),
            "explicit_preference_findings": [item.to_dict() for item in report.findings][:40],
            "explicit_preference_arrangement_failures": [
                item.to_dict() for item in report.route_gap_findings()
            ][:20],
            "explicit_preference_body_missing_keys": keys[:20],
        }
        if phase == "before_repair":
            payload["explicit_preference_before_reasons"] = reasons[:40]
        else:
            payload["explicit_preference_after_reasons"] = reasons[:40]
        self.metrics.merge_known_metrics(payload)

    def _preference_fail_closed(
        self,
        report: PreferenceFulfillmentReport,
        findings: list,
        *,
        extra_reason: str = "",
    ) -> dict[str, Any]:
        issues = report.to_generation_issues(findings)
        reasons = [item.reason_code for item in findings]
        if extra_reason:
            reasons.append(extra_reason)
        decision = DispatchDecision(
            action="FAIL_CLOSED",
            reasons=list(dict.fromkeys(reasons or ["explicit_preference_unresolved"])),
            notes=[
                extra_reason or "explicit preference fulfillment failed",
                *self.metrics.explicit_preference_before_reasons[:8],
            ],
            plan_indexes=sorted({
                item.plan_index for item in findings if item.plan_index
            }),
        )
        self._validate_dispatch_decision(decision, issues)
        return {
            "kind": "dispatch",
            "decision": decision,
            "qualified_review_issues": issues,
        }

    def _sync_day_openings_after_preference_repair(self) -> None:
        if not self.plans or not self.route_plans:
            return
        report = self._explicit_preference_report()
        night_days = {
            (item.plan_index, item.day)
            for item in report.findings
            if item.dimension == "night_view"
            and item.layer == "arrangement"
            and item.arrangement_supported
        }
        updated: list[PlanOutput] = []
        for index, plan in enumerate(self.plans, 1):
            text = plan.plan_text
            route = self.route_plans[index - 1] if index - 1 < len(self.route_plans) else None
            if route is None:
                updated.append(plan)
                continue
            for day_group in route.day_groups:
                supported = (index, int(day_group.day)) in night_days or any(
                    item.dimension == "night_view"
                    and item.plan_index == index
                    and item.day == int(day_group.day)
                    and item.arrangement_supported
                    for item in report.findings
                )
                text = sync_day_opening_for_locked_arrangement(
                    text,
                    day=int(day_group.day),
                    night_supported=supported,
                    place_names=[place.name for place in day_group.places],
                )
            if text != plan.plan_text:
                remapped = remap_fragment_registry(plan, updated_text=text)
                updated.append(plan if remapped is None else remapped)
            else:
                updated.append(plan)
        self.plans = updated

    def _review_keyed_soft_repair_targets(
        self,
        issues: list[generation_issues.GenerationIssue],
    ) -> tuple[list[FragmentKey], dict[FragmentKey, set[str]]]:
        issue_codes: dict[FragmentKey, set[str]] = {}
        for issue in issues:
            if (
                issue.category != "WARN"
                or issue.publish_action != "RECORD_ONLY"
                or issue.reason not in KEYED_SOFT_REPAIR_REASONS
                or issue.plan_index is None
                or issue.day is None
                or issue.place_id is None
            ):
                continue
            key = (int(issue.plan_index), int(issue.day), int(issue.place_id))
            if not all(part > 0 for part in key):
                continue
            issue_codes.setdefault(key, set()).add(issue.reason)

        selected: list[FragmentKey] = []
        for plan_index, plan in enumerate(self.plans, 1):
            plan_keys = [key for key in issue_codes if key[0] == plan_index]
            required_fact_keys = [
                key
                for key in plan_keys
                if "unsupported_fact_expansion" in issue_codes[key]
            ]
            selected.extend(required_fact_keys)
            if fallback_ratio_exceeded(
                plan_keys,
                total_fragment_count=len(plan.poi_fragments),
            ):
                selected.extend(plan_keys)
        return list(dict.fromkeys(selected)), issue_codes

    async def _run_review_keyed_fragment_repair(
        self,
        *,
        keys: list[FragmentKey],
        issue_codes_by_key: dict[FragmentKey, set[str]],
        required_keys: set[FragmentKey] | None = None,
        required_body_elements_by_key: dict[FragmentKey, tuple[str, ...]] | None = None,
    ) -> bool:
        required_keys = set(required_keys or ())
        self.metrics.review_keyed_fragment_repair_attempted = True
        self.metrics.review_keyed_fragment_repair_target_count = len(keys)
        if (self.metrics.writer_keyed_fragment_repair_attempted
                or (self.generation_policy_version == "v0.12" and (
                    self._v012_repair_remote_started
                    or (self.fragment_repair_call_count > 0 and not self._v012_repair_round_active)))):
            self.metrics.review_keyed_fragment_repair_failure_reason = (
                "repair_round_exhausted"
            )
            self.metrics.review_keyed_fragment_repair_remaining_count = len(keys)
            return False
        remaining = self._residual_seconds()
        if (
            self.generation_policy_version == "v0.12"
            and self._v012_repair_phase_deadline is not None
        ):
            phase_remaining = self._v012_repair_phase_deadline - time.monotonic()
            remaining = (
                phase_remaining if remaining is None
                else min(remaining, phase_remaining)
            )
        desired_timeout = keyed_fragment_repair_timeout_seconds(len(keys))
        repair_timeout = desired_timeout
        if remaining is not None:
            if self.generation_policy_version == "v0.12":
                repair_timeout = min(
                    desired_timeout,
                    max(0.0, remaining - V012_RECHECK_RESERVE_SECONDS),
                )
            else:
                repair_timeout = min(
                    desired_timeout,
                    max(
                        0.0,
                        remaining
                        - KEYED_FRAGMENT_REVIEW_TIMEOUT_SECONDS
                        - self.residual_reserve_seconds,
                    ),
                )
        self.metrics.review_keyed_fragment_repair_timeout_seconds = round(
            repair_timeout,
            3,
        )
        if (self.generation_policy_version != "v0.12"
                and repair_timeout < KEYED_FRAGMENT_REPAIR_MIN_TIMEOUT_SECONDS):
            self.metrics.review_keyed_fragment_repair_failure_reason = "budget_denied"
            self.metrics.review_keyed_fragment_repair_remaining_count = len(keys)
            return False
        targets = build_keyed_fragment_targets(
            keys,
            route_plans=self.route_plans or [],
            structured_evidence_payload=self.structured_evidence_payload,
            plans=self.plans,
            issue_codes_by_key=issue_codes_by_key,
        )
        if len(targets) != len(keys):
            self.metrics.review_keyed_fragment_repair_failure_reason = (
                "target_contract_missing"
            )
            self.metrics.review_keyed_fragment_repair_remaining_count = len(keys)
            return False
        if required_body_elements_by_key is None:
            preference_hints = self._explicit_preference_report().repair_hints_by_key()
            conflict_hints = self._body_conflict_repair_hints()
            body_elements = {
                target.key: ((preference_hints[target.key],) if target.key in preference_hints else ())
                + tuple(conflict_hints.get(target.key, ()))
                for target in targets
            }
        else:
            if (set(required_body_elements_by_key) - set(keys)
                    or any(not isinstance(values, tuple)
                           or any(not isinstance(value, str) or not value.strip() for value in values)
                           for values in required_body_elements_by_key.values())):
                self.metrics.review_keyed_fragment_repair_failure_reason = "target_contract_missing"
                return False
            body_elements = required_body_elements_by_key
        targets = [replace(target, required_body_elements=body_elements.get(target.key, ()))
                   for target in targets]
        if self.generation_policy_version == "v0.12":
            if not self._start_v012_repair_phase(
                min_writer_seconds=KEYED_FRAGMENT_REPAIR_MIN_TIMEOUT_SECONDS,
            ):
                self.metrics.review_keyed_fragment_repair_failure_reason = "budget_denied"
                self.metrics.review_keyed_fragment_repair_remaining_count = len(keys)
                return False
            if not self._v012_repair_round_active and not self._reserve_v012_repair_round():
                self.metrics.review_keyed_fragment_repair_failure_reason = "repair_round_exhausted"
                return False
            if not self._claim_v012_repair_remote():
                self.metrics.review_keyed_fragment_repair_failure_reason = "repair_round_exhausted"
                return False
            # Full-window admission above uses one clock reading. Subsequent
            # bookkeeping is charged to that deadline, never rounded upward
            # and never used to reopen the original minimum-window decision.
            repair_timeout = self._v012_repair_writer_timeout(
                min(desired_timeout, KEYED_FRAGMENT_REPAIR_MIN_TIMEOUT_SECONDS)
            )
            self.metrics.review_keyed_fragment_repair_timeout_seconds = round(repair_timeout, 3)
            if repair_timeout <= 0:
                self.metrics.review_keyed_fragment_repair_failure_reason = "budget_denied"
                return False
        repair_call = call_keyed_fragment_repair(
            generator=self._draft_generator(),
            targets=targets,
            timeout_seconds=repair_timeout,
        )
        try:
            repair = (await self._await_v012_repair(repair_call, cap=repair_timeout)
                      if self.generation_policy_version == "v0.12" else await repair_call)
        except asyncio.TimeoutError:
            self.metrics.review_keyed_fragment_repair_failure_reason = "transport_timeout"
            self.metrics.review_keyed_fragment_repair_remaining_count = len(keys)
            return False
        self.metrics.review_keyed_fragment_repair_latency_ms = repair.latency_ms
        if not repair.replacements:
            self.metrics.review_keyed_fragment_repair_failure_reason = (
                repair.failure_reason or "no_valid_replacements"
            )
            self.metrics.review_keyed_fragment_repair_remaining_count = len(keys)
            return False
        if self.generation_policy_version == "v0.12":
            # The whole repaired draft is rechecked below through the routed
            # v0.12 Review budget.  Do not open this legacy fragment reviewer.
            accepted = dict(repair.replacements)
        else:
            review = await review_keyed_fragment_replacements(
                targets=targets,
                replacements=repair.replacements,
            )
            self.metrics.review_keyed_fragment_repair_review_latency_ms = (
                review.latency_ms
            )
            if (
                review.failure_reason.startswith("transport:")
                and self._residual_admits(
                    min_seconds=KEYED_FRAGMENT_REVIEW_TIMEOUT_SECONDS
                )
            ):
                self.metrics.review_keyed_fragment_repair_review_retry_count = 1
                review = await review_keyed_fragment_replacements(
                    targets=targets,
                    replacements=repair.replacements,
                )
                self.metrics.review_keyed_fragment_repair_review_latency_ms += (
                    review.latency_ms
                )
            if review.failure_reason:
                self.metrics.review_keyed_fragment_repair_failure_reason = (
                    f"review:{review.failure_reason}"
                )
                self.metrics.review_keyed_fragment_repair_remaining_count = len(keys)
                return False

            accepted = {
                key: text
                for key, text in repair.replacements.items()
                if key not in review.rejected_keys
            }
        candidate_plans = list(self.plans)
        route_plans = self.route_plans or []
        applied_keys: set[FragmentKey] = set()
        validation_candidate_names = [
            candidate.name for candidate in self.retrieval.candidates
        ]
        for key, raw_text in accepted.items():
            plan_index, day, place_id = key
            zero = plan_index - 1
            if zero < 0 or zero >= len(candidate_plans) or zero >= len(route_plans):
                continue
            route_plan = route_plans[zero]
            place = next(
                (
                    place
                    for day_group in route_plan.day_groups
                    if int(day_group.day) == day
                    for place in day_group.places
                    if int(place.place_id) == place_id
                ),
                None,
            )
            if place is None:
                continue
            matching_day = next(
                (
                    day_group
                    for day_group in route_plan.day_groups
                    if int(day_group.day) == day
                ),
                None,
            )
            target_offset = next(
                (
                    offset
                    for offset, candidate in enumerate(matching_day.places)
                    if int(candidate.place_id) == place_id
                ),
                0,
            ) if matching_day is not None else 0
            prior_names = {
                prior.name
                for prior in (
                    matching_day.places[:target_offset]
                    if matching_day is not None
                    else []
                )
                if prior.name
            }
            original_fragment = candidate_plans[zero].poi_fragment(
                plan_index=plan_index, day=day, place_id=place_id,
            )
            if original_fragment is None:
                continue
            food_hint = fragment_food_hint(candidate_plans[zero], original_fragment)
            raw_text = normalize_body_food_boundary(
                raw_text, key=key, attachment_text=original_fragment.text,
                food_hint=food_hint, place_name=place.name,
                route_place_names=tuple(
                    p.name for group in route_plan.day_groups for p in group.places
                ),
            )
            if raw_text is None:
                continue
            food_names = (
                [str(food_hint["food_name"])]
                if food_hint and food_hint.get("evidence_tier") in {"full", "structural"}
                and food_hint.get("food_name") else []
            )
            cleaned, _actions, rejection = final_writer._sanitize_keyed_fragment_text(
                raw_text,
                place_name=place.name,
                route_plan=route_plan,
                validation_candidate_names=validation_candidate_names,
                weather_advisory_payload=self.weather_advisory_payload,
                allowed_contextual_names=food_names,
                fragment_key=key,
                same_day_prior_place_names=prior_names,
            )
            if rejection:
                continue
            replacement = f"{place.name}：{cleaned}"
            replaced = replace_fragment_body(
                candidate_plans[zero],
                plan_index=plan_index,
                day=day,
                place_id=place_id,
                replacement=replacement,
                source="fragment_repair",
            )
            if replaced is None:
                continue
            candidate_plans[zero] = replaced
            applied_keys.add(key)

        if route_plan_violations(candidate_plans, route_plans):
            self.metrics.review_keyed_fragment_repair_failure_reason = (
                "postcheck_failed"
            )
            self.metrics.review_keyed_fragment_repair_remaining_count = len(keys)
            return False
        remaining_keys = [key for key in keys if key not in applied_keys]
        self.metrics.review_keyed_fragment_repair_applied_count = len(applied_keys)
        self.metrics.review_keyed_fragment_repair_remaining_count = len(remaining_keys)
        if required_keys.intersection(remaining_keys):
            self.metrics.review_keyed_fragment_repair_failure_reason = (
                "required_fact_repair_incomplete"
            )
            return False
        for plan_index, plan in enumerate(candidate_plans, 1):
            plan_remaining = [key for key in remaining_keys if key[0] == plan_index]
            if fallback_ratio_exceeded(
                plan_remaining,
                total_fragment_count=len(plan.poi_fragments),
            ):
                self.metrics.review_keyed_fragment_repair_failure_reason = (
                    "fallback_ratio_still_exceeded"
                )
                return False
        self.plans = candidate_plans
        self.metrics.review_keyed_fragment_repair_failure_reason = ""
        if self.metrics.explicit_preference_repair_attempted:
            self.metrics.explicit_preference_repair_applied_keys = [
                {
                    "plan_index": key[0],
                    "day": key[1],
                    "place_id": key[2],
                }
                for key in sorted(applied_keys)
            ]
            if self.generation_policy_version != "v0.12":
                self._sync_day_openings_after_preference_repair()
        return True

    def _run_activity_local_completion(self) -> None:
        if self.generation_policy_version == "v0.12":
            return  # Do not turn a lexical miss into an automatic pass sentence.
        if not bool(
            getattr(
                get_settings(),
                "activity_local_completion_enabled",
                True,
            )
        ):
            return

        completion = deterministic_activity_completion(
            self.plans,
            route_plans=self.route_plans,
            structured_evidence_payload=self.structured_evidence_payload,
        )
        self.plans = completion.plans
        current = completion.to_metrics()
        if not self.metrics.activity_local_completion_enabled:
            self.metrics.merge_known_metrics(current)
        else:
            self.metrics.activity_local_completion_before_missing_count += int(
                current["activity_local_completion_before_missing_count"]
            )
            self.metrics.activity_local_completion_attempted_count += int(
                current["activity_local_completion_attempted_count"]
            )
            self.metrics.activity_local_completion_applied_count += int(
                current["activity_local_completion_applied_count"]
            )
            self.metrics.activity_local_completion_after_missing_count = int(
                current["activity_local_completion_after_missing_count"]
            )
            self.metrics.activity_local_completion_applied_keys = list({
                (
                    int(item["plan_index"]),
                    int(item["day"]),
                    int(item["place_id"]),
                ): item
                for item in [
                    *self.metrics.activity_local_completion_applied_keys,
                    *current["activity_local_completion_applied_keys"],
                ]
            }.values())
            self.metrics.activity_local_completion_skipped_details = list({
                (
                    int(item.get("plan_index", 0)),
                    int(item.get("day", 0)),
                    int(item.get("place_id", 0)),
                    str(item.get("reason", "")),
                ): item
                for item in [
                    *self.metrics.activity_local_completion_skipped_details,
                    *current["activity_local_completion_skipped_details"],
                ]
            }.values())
            self.metrics.activity_local_completion_skipped_count = len(
                self.metrics.activity_local_completion_skipped_details
            )
            self.metrics.activity_local_completion_unresolved_keys = list(
                current["activity_local_completion_unresolved_keys"]
            )
        if current["activity_local_completion_skipped_count"]:
            self.metrics.resolver_notes = list(dict.fromkeys([
                *self.metrics.resolver_notes,
                "activity_local_completion_skipped",
            ]))


    async def _run_writer(self) -> None:
        settings = get_settings()
        assert_single_locked_selected_route(
            self.route_plans or [],
            required=self.require_selected_route_lock,
        )
        missing_action_keys = missing_action_contract_keys(
            self.structured_evidence_payload.action_plan,
            self.route_plans or [],
        )
        action_plan_metrics: dict[str, Any] = {
            "writer_action_plan_complete": not missing_action_keys,
            "writer_action_contract_count": len(
                self.structured_evidence_payload.action_plan
            ),
            "writer_action_plan_missing_count": len(missing_action_keys),
            "writer_action_plan_missing_keys": [
                {
                    "plan_index": plan_index,
                    "day": day,
                    "place_id": place_id,
                }
                for plan_index, day, place_id in missing_action_keys
            ],
        }
        self.metrics.merge_known_metrics(action_plan_metrics)
        writer_step_metadata: dict[str, Any] = dict(action_plan_metrics)

        def ensure_publishable(plans: list[PlanOutput]) -> list[PlanOutput]:
            writer_step_metadata["writer_generated_plan_count"] = len(plans)
            if plans:
                writer_step_metadata.setdefault("adopted_generator", "opus")
            writer_step_metadata.setdefault(
                "writer_expected_plan_count",
                len(self.route_plans or []),
            )
            writer_step_metadata["writer_no_publishable_plans"] = not plans
            if not plans and not writer_step_metadata.get("writer_plan_failure_reasons"):
                writer_step_metadata["writer_plan_failure_reasons"] = [
                    "writer_no_publishable_plans"
                ]
            # O3: empty Writer output is structurally incomplete → Publish Retry
            # via Dispatch; do not hard-raise RuntimeError here.
            return plans

        async def run_writer():
            if not action_plan_metrics["writer_action_plan_complete"]:
                writer_step_metadata.update({
                    "writer_expected_plan_count": len(self.route_plans or []),
                    "writer_generated_plan_count": 0,
                    "writer_no_publishable_plans": True,
                    "writer_plan_failure_reasons": [
                        "writer_action_plan_incomplete"
                    ],
                    "writer_plan_failure_details": [
                        {
                            "reason": "writer_action_plan_incomplete",
                            **missing_key,
                        }
                        for missing_key in action_plan_metrics[
                            "writer_action_plan_missing_keys"
                        ]
                    ],
                })
                return ensure_publishable([])
            if self.route_plans:
                use_keyed_writer = _is_original_final_writer_generate()
                if use_keyed_writer:
                    parallel_writer = _writer_plan_concurrency_enabled(settings)
                    writer_step_metadata.update({
                        "parallel_used": parallel_writer,
                        "fallback_used": False,
                        "plan_count": len(self.route_plans),
                    })
                    concurrent_t0 = time.monotonic()
                    concurrent_plans, writer_metrics = (
                        await final_writer.generate_locked_plans_concurrently(
                            self.trip_request,
                            self.retrieval,
                            self.route_plans,
                            poi_identity_results=self.poi_identity_results,
                            budget_results=self.budget_results,
                            composition_blueprints=self.composition_blueprints,
                            structured_evidence_payload=self.structured_evidence_payload,
                            weather_advisory_payload=self.weather_advisory_payload,
                            publish_retry_feedback=self.publish_retry_feedback,
                            attachment_auth_map=self.attachment_auth_map,
                            accommodation=self.accommodation,
                            transport=self.transport,
                            pretrip_advice_payloads=self.pretrip_advice_payloads,
                            parallel=parallel_writer,
                            workflow_deadline_monotonic=(
                                self.workflow_deadline_monotonic
                            ),
                            residual_reserve_seconds=(
                                self.residual_reserve_seconds
                            ),
                            speculative_initial_generation=(
                                self.speculative_initial_generation
                            ),
                            plan_retry_remaining=self._writer_plan_retry_remaining,
                            body_semantics_by_review=self.generation_policy_version == "v0.12",
                            speculative_adopted_archive_sink=(
                                self._speculative_adopted_archive_records
                            ),
                        )
                    )
                    prior_retry_used = bool(self.metrics.writer_plan_retry_used)
                    prior_retry_count = int(self.metrics.writer_plan_retry_count or 0)
                    self.metrics.merge_known_metrics(writer_metrics)
                    if prior_retry_used:
                        self.metrics.writer_plan_retry_used = True
                    self.metrics.writer_plan_retry_count = max(
                        prior_retry_count,
                        int(self.metrics.writer_plan_retry_count or 0),
                    )
                    successful_plan_indexes = writer_metrics.get(
                        "writer_plan_successful_plan_indexes",
                        [],
                    )
                    writer_step_metadata.update({
                        "adopted_generator": (
                            str(writer_metrics.get("generator") or "opus")
                            if concurrent_plans
                            else (
                                str(writer_metrics.get("generator"))
                                if writer_metrics.get("generator") in {
                                    "opus", "ds_flash", "safe"
                                }
                                else None
                            )
                        ),
                        "per_plan_latency_ms": writer_metrics.get(
                            "writer_plan_latencies_ms", []
                        ),
                        "failed_plan_indexes": writer_metrics.get(
                            "writer_plan_failed_plan_indexes", []
                        ),
                        "writer_expected_plan_count": writer_metrics.get(
                            "writer_expected_plan_count",
                            len(self.route_plans or []),
                        ),
                        "writer_generated_plan_count": writer_metrics.get(
                            "writer_generated_plan_count",
                            len(concurrent_plans or []),
                        ),
                        "writer_no_publishable_plans": writer_metrics.get(
                            "writer_no_publishable_plans",
                            concurrent_plans is None or not concurrent_plans,
                        ),
                        "writer_plan_failure_reasons": writer_metrics.get(
                            "writer_plan_failure_reasons", []
                        ),
                        "writer_plan_failure_details": writer_metrics.get(
                            "writer_plan_failure_details", []
                        ),
                        "single_plan_unwrapped_plans_array": writer_metrics.get(
                            "single_plan_unwrapped_plans_array", False
                        ),
                        "single_plan_unwrapped_plan_indexes": writer_metrics.get(
                            "single_plan_unwrapped_plan_indexes", []
                        ),
                        "writer_plan_retry_used": bool(
                            self.metrics.writer_plan_retry_used
                        ),
                        "writer_plan_retry_count": int(
                            self.metrics.writer_plan_retry_count or 0
                        ),
                        "writer_plan_attempt_count": writer_metrics.get(
                            "writer_plan_attempt_count", 0
                        ),
                        "writer_plan_attempt_parse_error_types": writer_metrics.get(
                            "writer_plan_attempt_parse_error_types", []
                        ),
                        "writer_plan_attempt_raw_lengths": writer_metrics.get(
                            "writer_plan_attempt_raw_lengths", []
                        ),
                        "writer_plan_attempt_json_extract_failed": writer_metrics.get(
                            "writer_plan_attempt_json_extract_failed", []
                        ),
                        "writer_plan_retry_latency_ms": writer_metrics.get(
                            "writer_plan_retry_latency_ms", 0
                        ),
                        "writer_output_not_parseable": writer_metrics.get(
                            "writer_output_not_parseable", False
                        ),
                        "parse_error_type": writer_metrics.get(
                            "parse_error_type", ""
                        ),
                        "raw_length": writer_metrics.get("raw_length", 0),
                        "json_extract_failed": writer_metrics.get(
                            "json_extract_failed", False
                        ),
                        "parallel_wall_latency_ms": writer_metrics.get(
                            "writer_plan_parallel_wall_latency_ms",
                            int((time.monotonic() - concurrent_t0) * 1000),
                        ),
                    })
                    for key in (
                        "writer_plan_name_backend_owned",
                        "writer_keyed_fragment_contract_enabled",
                        "writer_keyed_fragment_count",
                        "writer_keyed_fragment_fallback_count",
                        "writer_keyed_fragment_fallback_keys",
                        "writer_keyed_fragment_ignored_count",
                        "writer_keyed_fragment_ignored_keys",
                        "writer_keyed_fragment_invalid_details",
                        "writer_keyed_fragment_repair_attempted",
                        "writer_keyed_fragment_repair_target_count",
                        "writer_keyed_fragment_repair_applied_count",
                        "writer_keyed_fragment_repair_remaining_count",
                        "writer_keyed_fragment_repair_latency_ms",
                        "writer_keyed_fragment_repair_failure_reason",
                        "writer_keyed_preference_repair_target_count",
                        "writer_keyed_preference_repair_applied_count",
                        "writer_keyed_preference_repair_remaining_count",
                    ):
                        writer_step_metadata[key] = writer_metrics.get(key)
                    for key in (
                        "writer_raw_output_sha256",
                        "writer_raw_output_length",
                        "writer_raw_output_prefix",
                        "writer_raw_output_suffix",
                        "parse_error_message",
                        "parse_error_position",
                        "first_json_candidate_prefix",
                        "contains_markdown_fence",
                        "brace_balance",
                        "bracket_balance",
                        "contains_unescaped_control_chars",
                        "contains_trailing_comma_like_pattern",
                        "contains_single_quote_json_like_pattern",
                        "retry_attempts_summary",
                        "adjudication_reason", "opus_adjudication_state", "ds_adjudication_state",
                        "ds_qualified_at_ms", "standby_grace_expires_at_ms", "adjudicated_at_ms",
                    ):
                        if key in writer_metrics:
                            writer_step_metadata[key] = writer_metrics[key]
                    if concurrent_plans is not None:
                        if len(concurrent_plans) != len(self.route_plans):
                            self._retain_route_plan_indexes(
                                [
                                    int(index) - 1
                                    for index in successful_plan_indexes
                                    if int(index) >= 1
                                ]
                            )
                            writer_step_metadata["partial_success_retained"] = True
                        return ensure_publishable(concurrent_plans)
                    writer_step_metadata["fail_fast_no_legacy_fallback"] = True
                    return ensure_publishable([])
                writer_step_metadata.update({
                    "parallel_used": False,
                    "fallback_used": False,
                    "plan_count": len(self.route_plans),
                })
                plans = await final_writer.generate(
                    self.trip_request,
                    self.retrieval,
                    self.route_plans,
                    poi_identity_results=self.poi_identity_results,
                    budget_results=self.budget_results,
                    composition_blueprints=self.composition_blueprints,
                    structured_evidence_payload=self.structured_evidence_payload,
                    weather_advisory_payload=self.weather_advisory_payload,
                    publish_retry_feedback=self.publish_retry_feedback,
                    attachment_auth_map=self.attachment_auth_map,
                    accommodation=self.accommodation,
                    transport=self.transport,
                    pretrip_advice_payloads=self.pretrip_advice_payloads,
                )
                return ensure_publishable(plans)
            plans = await final_writer.generate(
                self.trip_request,
                self.retrieval,
                structured_evidence_payload=self.structured_evidence_payload,
                weather_advisory_payload=self.weather_advisory_payload,
                publish_retry_feedback=self.publish_retry_feedback,
                attachment_auth_map=self.attachment_auth_map,
                accommodation=self.accommodation,
                transport=self.transport,
                pretrip_advice_payloads=self.pretrip_advice_payloads,
            )
            return ensure_publishable(plans)

        writer_t0 = time.monotonic()
        plans = await _run_observed_step(
            "FINAL_WRITER",
            run_writer,
            on_stage=self.on_stage,
            on_stage_event=self.on_stage_event,
            attempt=self.attempt,
            publish_retry_round=self.publish_retry_round,
            metadata={
                "route_plan_count": len(self.route_plans or []),
                "days": self.trip_request.days,
                "publish_retry_feedback_count": len(
                    self.publish_retry_feedback
                ),
                **action_plan_metrics,
            },
            finish_metadata=lambda: writer_step_metadata,
        )
        self.metrics.writer_original_latency_ms = int(
            (time.monotonic() - writer_t0) * 1000
        )
        logger.info("Generated %d plans", len(plans))
        if plans and self.on_writer_output is not None:
            try:
                await self.on_writer_output(plans)
            except Exception:
                # Observability-only capture must never change Writer, Review,
                # Publish Gate, or the delivered plan body.
                logger.warning("failed-draft Writer snapshot callback failed", exc_info=True)
        self.plans = normalize_plan_places(
            plans,
            self.retrieval,
            route_plans=self.route_plans,
        )

    def _retain_route_plan_indexes(self, zero_indexes: list[int]) -> None:
        valid_indexes = [
            index
            for index in zero_indexes
            if 0 <= index < len(self.route_plans)
        ]
        if not valid_indexes:
            return
        self.route_plans = [
            self.route_plans[index]
            for index in valid_indexes
        ]
        if self.poi_identity_results:
            self.poi_identity_results = [
                self.poi_identity_results[index]
                for index in valid_indexes
                if index < len(self.poi_identity_results)
            ]
        if self.budget_results:
            self.budget_results = [
                self.budget_results[index]
                for index in valid_indexes
                if index < len(self.budget_results)
            ]
        if self.composition_blueprints:
            self.composition_blueprints = [
                self.composition_blueprints[index]
                for index in valid_indexes
                if index < len(self.composition_blueprints)
            ]
        self.structured_evidence_payload = build_structured_evidence_payload(
            self.retrieval,
            route_plans=self.route_plans,
            composition_blueprints=self.composition_blueprints,
        )
        self._refresh_locked_safe_input()

    def _record_initial_review_metadata(self, step_metadata: dict[str, Any]) -> None:
        self.metrics.merge_known_metrics({
            key: step_metadata[key]
            for key in (
                "review_shadow_enabled",
                "risk_flags",
                "risk_flag_counts",
                "deterministic_risk_flags",
                "shadow_risk_level",
                "risk_score",
                "risk_level",
                "risk_level_source",
                "risk_level_semantics",
                "confirmed_generation_issues",
                "review_shadow_deterministic_issue_counts",
                "review_shadow_publish_gate_issue_counts",
                "review_shadow_flag_counts",
                "review_shadow_llm_issue_counts",
                "review_shadow_mismatch_summary",
                "review_dropped_backend_owned_count",
                "review_dropped_backend_owned_reasons",
                "review_dropped_backend_owned_issues",
                "initial_review_skipped",
                "initial_review_skip_reason",
                "review_risk_skip_enabled",
                "initial_review_llm_called",
                "initial_review_llm_issue_count",
                "review_transport_degraded",
                "review_transport_error_type",
                "review_transport_error_message",
                "review_invalid_output",
                "review_invalid_output_type",
                "review_transport_fail_open_allowed",
                "review_transport_fail_open_reason",
                "review_transport_fail_closed_reason",
                "deterministic_fallback_gate_passed",
                "publish_gate_passed",
                "publish_preflight_passed",
                "blueprint_integrity_passed",
                "route_plan_unchanged",
                "day_place_names_unchanged",
                "used_place_names_unchanged",
                "no_new_poi_names",
                "review_taxonomy_status",
                "review_timeout_seconds",
                "review_allocated_timeout_seconds",
                "review_request_elapsed_ms",
                "review_timeout_remaining_workflow_seconds",
                "review_relay_endpoint",
                "review_residual_reserve_seconds",
                "review_launch_guard_passed",
            )
            if key in step_metadata
        })
        if "deterministic_risk_flags" in step_metadata:
            self.metrics.review_shadow_deterministic_risk_flags = list(
                step_metadata.get("deterministic_risk_flags") or []
            )

    def _initial_review_skip_decision(self) -> tuple[
        generation_issues.IssueTaxonomyResult,
        dict[str, Any],
    ]:
        deterministic = generation_issues.collect_deterministic_generation_issues(
            self.plans,
            trip_request=self.trip_request,
            retrieval=self.retrieval,
            route_plans=self.route_plans or [],
            budget_results=self.budget_results,
            composition_blueprints=self.composition_blueprints,
            weather_advisory_payload=self.weather_advisory_payload,
            attachment_auth_map=self.attachment_auth_map,
            accommodation=self.accommodation,
            body_semantics_by_review=self.generation_policy_version == "v0.12",
        )
        taxonomy = generation_issues.resolve_generation_issues(deterministic)
        shadow_metadata = _build_review_shadow_metadata(
            deterministic,
            plans=self.plans,
            route_plans=self.route_plans or [],
            composition_blueprints=self.composition_blueprints,
        )
        blueprint_violations = check_plan_blueprint_integrity(
            self.plans,
            self.route_plans,
            self.composition_blueprints,
        )
        publish_preflight = self._check_publish_gate()
        route_plan_unchanged = True
        day_place_names_unchanged = _plans_match_locked_day_names(
            self.plans,
            self.route_plans,
        )
        used_place_names_unchanged = _plans_match_locked_used_names(
            self.plans,
            self.route_plans,
        )
        no_new_poi_names = not any(
            finding.reason == "route_outside_poi"
            for finding in publish_preflight.findings
        )
        risk_level = str(shadow_metadata.get("risk_level") or "UNKNOWN")
        risk_flags = list(shadow_metadata.get("risk_flags") or [])
        deterministic_risk_flags = list(
            shadow_metadata.get("deterministic_risk_flags") or []
        )
        deterministic_issue_gate_passed = (
            not taxonomy.fail_closed_reasons
            and taxonomy.issue_counts.get("BLOCKER", 0) == 0
            and taxonomy.issue_counts.get("REPAIR", 0) == 0
        )
        gate_checks = {
            "risk_level_is_low": risk_level == "LOW",
            "risk_flags_empty": not risk_flags,
            "deterministic_risk_flags_empty": not deterministic_risk_flags,
            "deterministic_issue_gate_passed": deterministic_issue_gate_passed,
            "publish_preflight_passed": publish_preflight.passed,
            "route_plan_present": bool(self.route_plans),
            "route_plan_unchanged": route_plan_unchanged,
            "day_place_names_unchanged": day_place_names_unchanged,
            "used_place_names_unchanged": used_place_names_unchanged,
            "no_new_poi_names": no_new_poi_names,
            "blueprint_integrity_passed": not blueprint_violations,
        }
        if self._ds_review_mandatory():
            gate_checks["speculative_ds_mandatory_review"] = False
        failed_checks = [
            key for key, passed in gate_checks.items() if not passed
        ]
        skipped = not failed_checks
        reason = (
            "low_risk_initial_review_preflight_passed"
            if skipped
            else ",".join(failed_checks)
        )
        metadata: dict[str, Any] = {
            **shadow_metadata,
            "initial_review_skipped": skipped,
            "initial_review_skip_reason": reason,
            "review_risk_skip_enabled": True,
            "initial_review_llm_called": False,
            "initial_review_llm_issue_count": 0,
            "publish_preflight_passed": publish_preflight.passed,
            "publish_preflight_failure_reasons": publish_preflight.failure_reasons,
            "blueprint_integrity_passed": not blueprint_violations,
            "blueprint_integrity_violation_count": len(blueprint_violations),
            "route_plan_unchanged": route_plan_unchanged,
            "day_place_names_unchanged": day_place_names_unchanged,
            "used_place_names_unchanged": used_place_names_unchanged,
            "no_new_poi_names": no_new_poi_names,
            "review_taxonomy_status": (
                "skipped_low_risk" if skipped else "llm_review_required"
            ),
            "initial_review_skip_gate_checks": gate_checks,
            "initial_review_skip_failed_checks": failed_checks,
            "plan_count": len(self.plans),
            "route_plan_count": len(self.route_plans or []),
            **_taxonomy_step_metadata(taxonomy),
        }
        if skipped:
            metadata.update(_build_review_shadow_metadata(
                deterministic,
                plans=self.plans,
                route_plans=self.route_plans or [],
                composition_blueprints=self.composition_blueprints,
                llm_issues=[],
            ))
            metadata.update({
                "initial_review_skipped": True,
                "initial_review_skip_reason": reason,
                "review_risk_skip_enabled": True,
                "initial_review_llm_called": False,
                "initial_review_llm_issue_count": 0,
                "publish_preflight_passed": publish_preflight.passed,
                "publish_preflight_failure_reasons": publish_preflight.failure_reasons,
                "blueprint_integrity_passed": not blueprint_violations,
                "blueprint_integrity_violation_count": len(blueprint_violations),
                "route_plan_unchanged": route_plan_unchanged,
                "day_place_names_unchanged": day_place_names_unchanged,
                "used_place_names_unchanged": used_place_names_unchanged,
                "no_new_poi_names": no_new_poi_names,
                "review_taxonomy_status": "skipped_low_risk",
                "initial_review_skip_gate_checks": gate_checks,
                "initial_review_skip_failed_checks": failed_checks,
                "plan_count": len(self.plans),
                "route_plan_count": len(self.route_plans or []),
                **_taxonomy_step_metadata(taxonomy),
            })
        return taxonomy, metadata

    def _code_delivery_evidence(self) -> dict[str, Any]:
        """Evaluate the existing code gates without inventing a Review outage."""
        deterministic = generation_issues.collect_deterministic_generation_issues(
            self.plans,
            trip_request=self.trip_request,
            retrieval=self.retrieval,
            route_plans=self.route_plans or [],
            budget_results=self.budget_results,
            composition_blueprints=self.composition_blueprints,
            weather_advisory_payload=self.weather_advisory_payload,
            attachment_auth_map=self.attachment_auth_map,
            accommodation=self.accommodation,
            body_semantics_by_review=self.generation_policy_version == "v0.12",
        )
        taxonomy = generation_issues.resolve_generation_issues(deterministic)
        shadow_metadata = _build_review_shadow_metadata(
            deterministic,
            plans=self.plans,
            route_plans=self.route_plans or [],
            composition_blueprints=self.composition_blueprints,
        )
        blueprint_violations = check_plan_blueprint_integrity(
            self.plans,
            self.route_plans,
            self.composition_blueprints,
        )
        publish_preflight = self._check_publish_gate()
        hard_risk_flags = sorted(
            _review_shadow_high_risk_flags(
                list(shadow_metadata.get("deterministic_risk_flags") or [])
            )
        )
        writer_generated_plan_count = int(
            _metric_value(
                self.metrics,
                "writer_generated_plan_count",
                len(self.plans),
            )
            or 0
        )
        writer_output_not_parseable = bool(
            _metric_value(self.metrics, "writer_output_not_parseable", False)
        )
        plan_count = len(self.plans)
        route_plan_count = len(self.route_plans or [])
        route_day_integrity_passed = (
            plan_count == route_plan_count == 1
            and _plans_match_locked_day_names(self.plans, self.route_plans)
            and _plans_match_locked_used_names(self.plans, self.route_plans)
        )
        deterministic_issue_gate_passed = (
            not taxonomy.fail_closed_reasons
            and taxonomy.issue_counts.get("BLOCKER", 0) == 0
            and taxonomy.issue_counts.get("REPAIR", 0) == 0
        )
        gate_checks = {
            "final_writer_succeeded": bool(self.plans),
            "writer_output_not_parseable_false": not writer_output_not_parseable,
            "writer_generated_plan_count_is_one": writer_generated_plan_count == 1,
            "plan_count_is_one": plan_count == 1,
            "route_day_integrity_passed": route_day_integrity_passed,
            "blueprint_integrity_passed": not blueprint_violations,
            "publish_preflight_passed": publish_preflight.passed,
            "deterministic_issue_gate_passed": deterministic_issue_gate_passed,
            # v0.15.1: the high-risk flags are recorded, not a gate. Six of the
            # seven restate checks above; the seventh was a crowd/ranking regex
            # over the whole body, which sent vivid drafts to the basic plan.
        }
        return {
            "taxonomy": taxonomy,
            "shadow_metadata": shadow_metadata,
            "blueprint_violations": blueprint_violations,
            "publish_preflight": publish_preflight,
            "hard_risk_flags": hard_risk_flags,
            "writer_generated_plan_count": writer_generated_plan_count,
            "writer_output_not_parseable": writer_output_not_parseable,
            "plan_count": plan_count,
            "route_plan_count": route_plan_count,
            "gate_checks": gate_checks,
        }

    def _review_transport_fallback(
        self,
        exc: Exception,
        unavailable_kind: str,
        error_type: str,
    ) -> tuple[
        generation_issues.IssueTaxonomyResult,
        dict[str, Any],
        publish_gate.PublishGateError | None,
    ]:
        evidence = self._code_delivery_evidence()
        taxonomy = evidence["taxonomy"]
        shadow_metadata = evidence["shadow_metadata"]
        blueprint_violations = evidence["blueprint_violations"]
        publish_preflight = evidence["publish_preflight"]
        hard_risk_flags = evidence["hard_risk_flags"]
        writer_generated_plan_count = evidence["writer_generated_plan_count"]
        writer_output_not_parseable = evidence["writer_output_not_parseable"]
        plan_count = evidence["plan_count"]
        route_plan_count = evidence["route_plan_count"]
        gate_checks = evidence["gate_checks"]
        failed_checks = [
            key for key, passed in gate_checks.items() if not passed
        ]
        gate_passed = not failed_checks
        ds_mandatory = self._ds_review_mandatory()
        degraded_enabled = bool(
            get_settings().review_unavailable_degraded_enabled
            and not ds_mandatory
        )
        transport_metadata: dict[str, Any] = {
            "review_transport_degraded": unavailable_kind == "transport",
            "review_transport_error_type": (
                error_type if unavailable_kind == "transport" else ""
            ),
            "review_transport_error_message": "",
            "review_invalid_output": unavailable_kind == "invalid_output",
            "review_invalid_output_type": (
                error_type if unavailable_kind == "invalid_output" else ""
            ),
            "review_transport_fail_open_allowed": degraded_enabled and gate_passed,
            "review_transport_fail_open_reason": (
                "deterministic_gates_passed"
                if degraded_enabled and gate_passed
                else ""
            ),
            "review_transport_fail_closed_reason": (
                ",".join(failed_checks) if failed_checks else ""
            ),
            "deterministic_fallback_gate_passed": gate_passed,
            "publish_gate_passed": False,
            "publish_preflight_passed": publish_preflight.passed,
            "publish_preflight_failure_reasons": publish_preflight.failure_reasons,
            "review_taxonomy_status": (
                f"{unavailable_kind}_degraded"
                if degraded_enabled and gate_passed
                else f"{unavailable_kind}_failed_closed"
            ),
            "review_transport_gate_checks": gate_checks,
            "review_transport_hard_risk_flags": hard_risk_flags,
            "blueprint_integrity_passed": not blueprint_violations,
            "blueprint_integrity_violation_count": len(blueprint_violations),
            "route_plan_unchanged": True,
            "day_place_names_unchanged": _plans_match_locked_day_names(
                self.plans,
                self.route_plans,
            ),
            "used_place_names_unchanged": _plans_match_locked_used_names(
                self.plans,
                self.route_plans,
            ),
            "no_new_poi_names": not any(
                finding.reason == "route_outside_poi"
                for finding in publish_preflight.findings
            ),
            "plan_count": plan_count,
            "route_plan_count": route_plan_count,
            "writer_generated_plan_count": writer_generated_plan_count,
            "writer_output_not_parseable": writer_output_not_parseable,
            **shadow_metadata,
            **_taxonomy_step_metadata(taxonomy),
        }
        self.metrics.publish_preflight_passed = publish_preflight.passed
        self.metrics.blueprint_integrity_passed = not blueprint_violations
        self.metrics.route_plan_unchanged = True
        self.metrics.day_place_names_unchanged = bool(
            transport_metadata["day_place_names_unchanged"]
        )
        self.metrics.used_place_names_unchanged = bool(
            transport_metadata["used_place_names_unchanged"]
        )
        self.metrics.no_new_poi_names = bool(
            transport_metadata["no_new_poi_names"]
        )
        self.metrics.review_transport_fail_closed = not (
            degraded_enabled and gate_passed
        )
        self.metrics.review_transport_fail_closed_reason = str(
            transport_metadata["review_transport_fail_closed_reason"] or ""
        )
        self.metrics.merge_known_metrics(transport_metadata)
        if degraded_enabled and gate_passed:
            self.metrics.merge_known_metrics({
                "published_variant": "normal",
                "delivery_status": "DEGRADED",
                "safe_trigger": None,
            })
            error = None
        else:
            error = publish_gate.PublishGateError(
                publish_gate.PublishGateResult(
                    passed=False,
                    findings=[publish_gate.PublishFinding(
                        reason="review_unavailable",
                        message=(
                            "REVIEW_TAXONOMY unavailable; deterministic gates: "
                            + (
                                transport_metadata[
                                    "review_transport_fail_closed_reason"
                                ]
                                or "disabled"
                            )
                        ),
                    )],
                )
            )
        return taxonomy, transport_metadata, error

    async def _review_taxonomy(
        self,
        stage: str,
        metadata: dict[str, Any] | None = None,
    ) -> generation_issues.IssueTaxonomyResult:
        step_metadata: dict[str, Any] = {}
        taxonomy: generation_issues.IssueTaxonomyResult | None = None
        initial_skip_metadata: dict[str, Any] = {}
        v012_call_start = (
            len(self.review_call_budget.calls)
            if self.generation_policy_version == "v0.12"
            and self.review_call_budget is not None
            else 0
        )
        execution_state = "FAILED"
        execution_reason: str | None = "UNKNOWN"
        adopted_sequence: int | None = None
        from src.agents.llm import current_llm_observation_summary, llm_call_context

        def finish_step_metadata() -> dict[str, Any]:
            if (
                self.generation_policy_version == "v0.12"
                and self.review_call_budget is not None
            ):
                calls = self.review_call_budget.calls[v012_call_start:]
                observation = project_execution_observation(
                    calls,
                    role="review",
                    purpose=(
                        "REVIEW" if stage == "REVIEW_TAXONOMY"
                        else "REVIEW_AFTER_REPAIR"
                    ),
                    state=execution_state,
                    reason_code=execution_reason,
                    adopted_call_sequence=adopted_sequence,
                )
                step_metadata["execution_observation"] = observation.model_dump(
                    mode="json"
                )
                sequences = {
                    int(item.get("sequence") or 0) for item in calls
                }
                step_metadata["review_path_diagnostics"] = [
                    item
                    for item in list(self.review_call_budget.path_diagnostics)
                    if int(item.get("sequence") or 0) in sequences
                ]
                step_metadata["review_route_calls"] = list(calls)
            return step_metadata

        async def action():
            nonlocal taxonomy, step_metadata, initial_skip_metadata
            nonlocal execution_state, execution_reason, adopted_sequence
            settings = get_settings()
            skip_feature_enabled = _review_risk_skip_enabled(settings)
            ds_mandatory = self._ds_review_mandatory()
            generation_index = int(self.publish_retry_round or 0)
            with llm_call_context(
                stage=stage,
                review_round="recheck" if stage == "REVIEW_TAXONOMY_AFTER_REPAIR" else "initial",
                generation_index=generation_index,
                publish_retry_round=self.publish_retry_round,
                attempt=self.attempt,
            ):
                if stage == "REVIEW_TAXONOMY":
                    if self.generation_policy_version == "v0.12":
                        # Current production Writer output is free prose.  No
                        # workflow source can yet prove that its complete body
                        # came only from approved templates/structured fields.
                        initial_skip_metadata = {
                            "initial_review_skipped": False,
                            "initial_review_skip_reason": (
                                "v012_free_writer_body_requires_review"
                            ),
                            "review_risk_skip_enabled": False,
                        }
                    elif ds_mandatory:
                        initial_skip_metadata = {
                            "initial_review_skipped": False,
                            "initial_review_skip_reason": (
                                "speculative_ds_mandatory_review"
                            ),
                            "review_risk_skip_enabled": skip_feature_enabled,
                        }
                    elif skip_feature_enabled and self.publish_retry_round == 0:
                        taxonomy, initial_skip_metadata = (
                            self._initial_review_skip_decision()
                        )
                        if initial_skip_metadata["initial_review_skipped"]:
                            step_metadata = initial_skip_metadata
                            self.metrics.review_ran = False
                            self.metrics.review_required_for_complete_body = False
                            self.metrics.full_candidate_review_skip_disabled = False
                            self._record_initial_review_metadata(step_metadata)
                            return taxonomy
                    elif self.publish_retry_round > 0:
                        initial_skip_metadata = {
                            "initial_review_skipped": False,
                            "initial_review_skip_reason": (
                                "publish_retry_review_required"
                            ),
                            "review_risk_skip_enabled": skip_feature_enabled,
                        }
                    else:
                        initial_skip_metadata = {
                            "initial_review_skipped": False,
                            "initial_review_skip_reason": (
                                "review_risk_skip_disabled"
                            ),
                            "review_risk_skip_enabled": False,
                        }
                try:
                    review_budget_metadata: dict[str, Any] = {}
                    _allocated_review_timeout = (
                        self._speculative_review_timeout_seconds()
                    )
                    self._body_review_prior = list(self._next_review_issues)
                    review_call = run_taxonomy_review(
                        self.plans,
                        trip_request=self.trip_request,
                        retrieval=self.retrieval,
                        route_plans=self.route_plans,
                        budget_results=self.budget_results,
                        composition_blueprints=self.composition_blueprints,
                        review_plan_concurrency_enabled=(
                            self.generation_policy_version != "v0.12"
                            and _review_plan_concurrency_enabled(settings)
                            and _is_original_yuntu_review_collect()
                        ),
                        structured_evidence_payload=self.structured_evidence_payload,
                        weather_advisory_payload=self.weather_advisory_payload,
                        attachment_auth_map=self.attachment_auth_map,
                        accommodation=self.accommodation,
                        review_timeout_seconds=(
                            _allocated_review_timeout
                            or yuntu_review.ORDINARY_REVIEW_REQUEST_TIMEOUT_SECONDS
                        ),
                        generator=self.metrics.extra.get("generator"),
                        generation_policy_version=self.generation_policy_version,
                        review_call_budget=self._v012_review_phase_budget(stage),
                        previous_review_issues=self._body_review_prior,
                        allow_failure_backup=True,
                    )
                    if self._speculative_round():
                        remaining_at_launch = self._residual_seconds()
                        if remaining_at_launch is not None:
                            remaining_at_launch = (
                                int(max(0.0, remaining_at_launch) * 1000) / 1000
                            )
                        review_timeout = _allocated_review_timeout
                        if review_timeout is None:
                            review_budget_metadata = {
                                "review_timeout_seconds": None,
                                "review_allocated_timeout_seconds": None,
                                "review_request_elapsed_ms": 0,
                                "review_timeout_remaining_workflow_seconds": (
                                    remaining_at_launch
                                ),
                                "review_residual_reserve_seconds": (
                                    self._review_tail_reserve_seconds()
                                ),
                                "review_launch_guard_passed": False,
                                "review_transport_error_type": None,
                                "review_relay_endpoint": "not_launched",
                            }
                            review_call.close()
                            raise ReviewLaunchBudgetUnavailable(
                                "Review launch denied by residual reserve"
                            )
                        observation = current_llm_observation_summary()
                        record_start = len(
                            observation.get("llm_call_records") or []
                        )
                        review_budget_metadata = {
                            "review_timeout_seconds": review_timeout,
                            "review_allocated_timeout_seconds": review_timeout,
                            "review_request_elapsed_ms": 0,
                            "review_timeout_remaining_workflow_seconds": (
                                remaining_at_launch
                            ),
                            "review_residual_reserve_seconds": (
                                self._review_tail_reserve_seconds()
                            ),
                            "review_launch_guard_passed": True,
                            "review_transport_error_type": None,
                            "review_relay_endpoint": "unknown",
                        }
                        review_started_at = time.monotonic()
                        review_deadline_monotonic = (
                            review_started_at + review_timeout
                        )
                        try:
                            with llm_call_context(
                                review_request_deadline_monotonic=(
                                    review_deadline_monotonic
                                ),
                            ):
                                review_result = await asyncio.wait_for(
                                    review_call,
                                    timeout=review_timeout,
                                )
                        finally:
                            review_budget_metadata[
                                "review_request_elapsed_ms"
                            ] = int(
                                (time.monotonic() - review_started_at) * 1000
                            )
                            review_budget_metadata["review_relay_endpoint"] = (
                                _review_relay_endpoint_since(record_start)
                            )
                    else:
                        review_result = await review_call
                    taxonomy, step_metadata, review_metrics = review_result
                    step_metadata.update(review_budget_metadata)
                except Exception as exc:
                    # Retain the allocated cap/elapsed receipt on recheck
                    # failure as well; it must not disappear with the result.
                    step_metadata.update(review_budget_metadata)
                    if review_budget_metadata:
                        step_metadata["review_transport_error_type"] = (
                            _review_transport_error_type(exc)
                        )
                    if self.generation_policy_version == "v0.12":
                        calls = self.review_call_budget.calls[v012_call_start:]
                        execution_state = "FAILED" if calls else "CALL_REJECTED"
                        execution_reason = (
                            exc.code
                            if isinstance(exc, GenerationModelCallError)
                            else (
                                calls[-1].get("error_code")
                                if calls else "CONFIG_UNAVAILABLE"
                            )
                        )
                        # W9 owns a separate, body-bound deterministic delivery
                        # path. Do not fabricate an empty semantic review here.
                        raise
                    unavailable = _review_unavailable_type(exc)
                    if stage != "REVIEW_TAXONOMY" or unavailable is None:
                        raise
                    unavailable_kind, error_type = unavailable
                    review_launched = not isinstance(
                        exc, ReviewLaunchBudgetUnavailable
                    )
                    taxonomy, step_metadata, fallback_error = (
                        self._review_transport_fallback(
                            exc,
                            unavailable_kind,
                            error_type,
                        )
                    )
                    step_metadata.update(review_budget_metadata)
                    step_metadata.update({
                        "review_launch_guard_passed": review_budget_metadata.get(
                            "review_launch_guard_passed", review_launched
                        ),
                        "review_transport_error_type": (
                            _review_transport_error_type(exc)
                            if review_launched
                            else None
                        ),
                        "initial_review_skipped": False,
                        "initial_review_skip_reason": initial_skip_metadata.get(
                            "initial_review_skip_reason",
                            "review_unavailable",
                        ),
                        "review_risk_skip_enabled": skip_feature_enabled,
                        "initial_review_llm_called": review_launched,
                        "initial_review_llm_issue_count": 0,
                        "review_transport_fail_closed": fallback_error is not None,
                    })
                    self.metrics.review_ran = review_launched
                    self.metrics.review_required_for_complete_body = ds_mandatory
                    self.metrics.full_candidate_review_skip_disabled = ds_mandatory
                    self.metrics.review_transport_fail_closed = fallback_error is not None
                    self._record_initial_review_metadata(step_metadata)
                    if ds_mandatory:
                        self.metrics.merge_known_metrics({
                            "ds_mandatory_review_unavailable": True,
                            "ds_mandatory_review_unavailable_type": error_type,
                        })
                        raise DSMandatoryReviewUnavailable(error_type) from exc
                    if fallback_error is not None:
                        raise fallback_error
                    return taxonomy
                self.metrics.merge_known_metrics(review_metrics)
                if self.generation_policy_version == "v0.12":
                    stage_calls = self.review_call_budget.calls[v012_call_start:]
                    routed_adopted_sequence = (
                        self.review_call_budget.adopted_call_sequence
                    )
                    adopted = next(
                        (
                            call for call in stage_calls
                            if call.get("status") == "SUCCESS"
                            and int(call.get("sequence") or 0)
                            == routed_adopted_sequence
                        ),
                        None,
                    )
                    if adopted is not None:
                        adopted_sequence = int(adopted["sequence"])
                        execution_state = (
                            "FALLBACK_USED"
                            if any(
                                call.get("status") != "SUCCESS"
                                for call in stage_calls
                                if int(call.get("sequence") or 0) < adopted_sequence
                            )
                            else "MODEL_SUCCEEDED"
                        )
                        execution_reason = None
                    else:
                        execution_state = "FAILED"
                        execution_reason = "UNKNOWN"
                    self.metrics.merge_known_metrics({
                        "v012_review_state": "MODEL_PASSED",
                        "v012_review_call_count": (
                            self.review_call_budget.calls_started
                            if self.review_call_budget is not None else 0
                        ),
                        "v012_review_backup_used": (
                            self.review_call_budget.backup_claimed
                            if self.review_call_budget is not None else False
                        ),
                    })
                if stage == "REVIEW_TAXONOMY":
                    llm_counts = step_metadata.get("review_shadow_llm_issue_counts") or {}
                    step_metadata.update({
                        "initial_review_skipped": False,
                        "initial_review_skip_reason": initial_skip_metadata.get(
                            "initial_review_skip_reason",
                            "llm_review_required",
                        ),
                        "review_risk_skip_enabled": skip_feature_enabled,
                        "initial_review_llm_called": True,
                        "initial_review_llm_issue_count": sum(
                            int(value or 0) for value in llm_counts.values()
                        ),
                    })
                    self.metrics.review_ran = True
                    self.metrics.review_required_for_complete_body = True
                    self.metrics.full_candidate_review_skip_disabled = (
                        ds_mandatory
                        or self.publish_retry_round > 0
                        or not skip_feature_enabled
                    )
                    for key in (
                        "publish_preflight_passed",
                        "publish_preflight_failure_reasons",
                        "blueprint_integrity_passed",
                        "blueprint_integrity_violation_count",
                        "route_plan_unchanged",
                        "day_place_names_unchanged",
                        "used_place_names_unchanged",
                        "no_new_poi_names",
                        "initial_review_skip_gate_checks",
                        "initial_review_skip_failed_checks",
                    ):
                        if key in initial_skip_metadata:
                            step_metadata[key] = initial_skip_metadata[key]
                    step_metadata.setdefault("review_transport_degraded", False)
                    step_metadata.setdefault("review_transport_error_type", "")
                    step_metadata.setdefault("review_transport_error_message", "")
                    step_metadata.setdefault("review_invalid_output", False)
                    step_metadata.setdefault("review_invalid_output_type", "")
                    step_metadata.setdefault("review_transport_fail_open_allowed", False)
                    step_metadata.setdefault("review_transport_fail_open_reason", "")
                    step_metadata.setdefault("review_transport_fail_closed_reason", "")
                    step_metadata.setdefault("deterministic_fallback_gate_passed", False)
                    step_metadata.setdefault("review_taxonomy_status", "completed")
                    self._record_initial_review_metadata(step_metadata)
                return taxonomy

        result = await _run_observed_step(
            stage,
            action,
            on_stage=self.on_stage,
            on_stage_event=self.on_stage_event,
            attempt=self.attempt,
            publish_retry_round=self.publish_retry_round,
            metadata={
                "plan_count": len(self.plans),
                "route_plan_count": len(self.route_plans or []),
                **(metadata or {}),
            },
            finish_metadata=finish_step_metadata,
        )
        return result

    async def _repair_plans(
        self,
        taxonomy: generation_issues.IssueTaxonomyResult,
        *,
        followup_round: int,
        target_plan_indexes: list[int],
        budget: RepairBudgetTracker,
    ) -> None:
        candidate_names = [candidate.name for candidate in self.retrieval.candidates]
        repaired = list(self.plans)
        repair_t0 = time.monotonic()

        async def repair_one(plan_index: int) -> PlanRepairResult:
            target_issues = [
                issue
                for issue in taxonomy.issues
                if issue.plan_index == plan_index
                and issue.publish_action == "REPAIR_PLAN"
            ]
            try:
                budget.assert_can_continue()
                timeout_seconds = min(
                    self.repair_policy.per_plan_timeout_seconds,
                    max(1.0, budget.remaining_seconds()),
                )
                return await asyncio.wait_for(
                    repair_single_plan(
                        plan_index=plan_index,
                        plans=self.plans,
                        route_plans=self.route_plans,
                        trip_request=self.trip_request,
                        retrieval_city=self.retrieval.city,
                        issues=target_issues,
                        budget_results=self.budget_results,
                        poi_identity_results=self.poi_identity_results,
                        composition_blueprints=self.composition_blueprints,
                        validation_candidate_names=candidate_names,
                        structured_evidence_payload=self.structured_evidence_payload,
                        weather_advisory_payload=self.weather_advisory_payload,
                    ),
                    timeout=timeout_seconds,
                )
            except asyncio.TimeoutError:
                return PlanRepairResult(
                    plan_index=plan_index,
                    zero_index=plan_index - 1,
                    failure_reason=f"repair_timeout:{plan_index}",
                    failure_detail={
                        "plan_index": plan_index,
                        "reason": "repair_timeout",
                        "timeout_seconds": self.repair_policy.per_plan_timeout_seconds,
                        "issue_reasons": [issue.reason for issue in target_issues],
                        "snippets": [
                            issue.snippet[:160]
                            for issue in target_issues
                            if issue.snippet
                        ],
                    },
                )
            except RepairBudgetExceeded as exc:
                return PlanRepairResult(
                    plan_index=plan_index,
                    zero_index=plan_index - 1,
                    failure_reason=f"repair_budget_exceeded:{plan_index}",
                    failure_detail={
                        "plan_index": plan_index,
                        "reason": "repair_budget_exceeded",
                        "elapsed_seconds": round(exc.elapsed_seconds, 1),
                        "max_wall_seconds": exc.max_wall_seconds,
                        "issue_reasons": [issue.reason for issue in target_issues],
                    },
                )

        parallel_used = (
            get_settings().writer_repair_concurrency_enabled
            and len(target_plan_indexes) > 1
        )

        def apply_repair_results(repair_results: list[PlanRepairResult]) -> None:
            failures: list[str] = []
            failure_details: list[dict[str, Any]] = []
            sanitizer_actions: list[dict[str, Any]] = []
            repair_metrics: dict[str, Any] = {}
            repaired_indexes_round: list[int] = []
            for result in repair_results:
                if isinstance(result.failure_detail, dict):
                    failure_details.append(result.failure_detail)
                sanitizer_actions.extend(result.sanitizer_actions)
                for key, value in result.repair_metrics.items():
                    if key == "writer_repair_sanitizer_action_count":
                        continue
                    repair_metrics[key] = value
                if result.failure_reason:
                    failures.append(result.failure_reason)
                    continue
                if result.repaired_plan is None:
                    failures.append(f"repair_failed:{result.plan_index}")
                    continue
                repaired[result.zero_index] = result.repaired_plan
                repaired_indexes_round.append(result.plan_index)

            wall_latency_ms = int((time.monotonic() - repair_t0) * 1000)
            plan_latencies = [result.latency_ms for result in repair_results]
            repair_modes = [
                str(result.repair_metrics.get("repair_mode") or "")
                for result in repair_results
                if result.repair_metrics.get("repair_mode")
            ]
            writer_llm_repair_called = any(
                bool(result.repair_metrics.get("writer_llm_repair_called"))
                for result in repair_results
            )
            if failures:
                repair_mode = "failed"
            elif not repair_modes:
                repair_mode = "unknown"
            elif len(set(repair_modes)) == 1:
                repair_mode = repair_modes[0]
            else:
                repair_mode = "mixed"
            if followup_round <= 0:
                self.metrics.writer_repair_attempt = 1
                self.metrics.writer_repair_latency_ms = wall_latency_ms
                self.metrics.repair_failure_reasons = failures
                self.metrics.repair_success = not failures
                self.metrics.writer_repair_parallel_used = parallel_used
                self.metrics.writer_repair_plan_latencies_ms = plan_latencies
                self.metrics.writer_repair_parallel_wall_latency_ms = wall_latency_ms
                self.metrics.writer_repair_failure_count = len(failures)
                self.metrics.repair_failure_details = failure_details
                self.metrics.writer_repair_sanitizer_actions = sanitizer_actions
                self.metrics.writer_repair_sanitizer_action_count = len(sanitizer_actions)
                self.metrics.repair_mode = repair_mode
                self.metrics.writer_llm_repair_called = writer_llm_repair_called
                self.metrics.writer_repair_route_outside_names = _detail_values(
                    failure_details,
                    "route_outside_names",
                )
                self.metrics.writer_repair_route_violation_reasons = _detail_values(
                    failure_details,
                    "route_violation_reasons",
                )
                self.metrics.writer_repair_unsupported_fact_patterns = _detail_values(
                    failure_details,
                    "unsupported_fact_patterns",
                )
            else:
                self.metrics.writer_repair_followup_attempt = followup_round
                self.metrics.writer_repair_followup_target_plan_indexes = (
                    target_plan_indexes
                )
                self.metrics.writer_repair_followup_repaired_plan_indexes = (
                    repaired_indexes_round
                )
                self.metrics.writer_repair_followup_failure_reasons = failures
                self.metrics.writer_repair_followup_failure_details = failure_details
                self.metrics.writer_repair_followup_sanitizer_actions = sanitizer_actions
                self.metrics.writer_repair_followup_sanitizer_action_count = len(
                    sanitizer_actions
                )
                self.metrics.writer_repair_followup_plan_latencies_ms = plan_latencies
                self.metrics.writer_repair_followup_latency_ms = wall_latency_ms

            self.metrics.merge_known_metrics(repair_metrics)
            self.plans = normalize_plan_places(
                repaired,
                self.retrieval,
                route_plans=self.route_plans,
            )
            self.repaired_plan_indexes = list(dict.fromkeys([
                *self.repaired_plan_indexes,
                *repaired_indexes_round,
            ]))

        async def action():
            if parallel_used:
                repair_results = await asyncio.gather(*[
                    repair_one(plan_index)
                    for plan_index in target_plan_indexes
                ])
            else:
                repair_results = [
                    await repair_one(plan_index)
                    for plan_index in target_plan_indexes
                ]
            apply_repair_results(repair_results)
            return repair_results

        await _run_observed_step(
            "WRITER_REPAIR",
            action,
            on_stage=self.on_stage,
            on_stage_event=self.on_stage_event,
            attempt=self.attempt,
            publish_retry_round=self.publish_retry_round + followup_round,
            metadata={"repair_target_plan_indexes": target_plan_indexes},
            finish_metadata=lambda: self._repair_stage_metadata(followup_round),
        )

    async def _run_followup_repairs(
        self,
        taxonomy: generation_issues.IssueTaxonomyResult,
        *,
        budget: RepairBudgetTracker,
    ) -> generation_issues.IssueTaxonomyResult:
        followup_round = 1
        current = taxonomy
        while (
            self.repaired_plan_indexes
            and current.repair_target_plan_indexes
            and not current.fail_closed_reasons
            and followup_round <= self.repair_policy.max_followup_rounds
        ):
            try:
                budget.assert_can_continue()
            except RepairBudgetExceeded as exc:
                self._record_repair_budget(budget, exceeded=True)
                self._raise_budget_exceeded(exc)
            targets = self._repair_targets(current.repair_target_plan_indexes)
            await self._repair_plans(
                current,
                followup_round=followup_round,
                target_plan_indexes=targets,
                budget=budget,
            )
            if self.metrics.writer_repair_followup_failure_reasons:
                self._record_repair_budget(budget)
                self._raise_repair_failure(
                    self.metrics.writer_repair_followup_failure_reasons,
                    self.metrics.writer_repair_followup_failure_details,
                    prefix="writer follow-up repair failed",
                )
            self._check_blueprint_integrity()
            current = await self._review_taxonomy(
                "REVIEW_TAXONOMY_AFTER_REPAIR"
            )
            self._record_post_repair_metrics(current)
            self._record_repair_budget(budget)
            followup_round += 1
        return current

    def _repair_targets(self, targets: list[int]) -> list[int]:
        return sorted(set(targets))[: self.repair_policy.max_target_plans_per_round]

    def _repair_stage_metadata(self, followup_round: int) -> dict[str, Any]:
        if followup_round <= 0:
            return {
                "parallel_used": self.metrics.writer_repair_parallel_used,
                "per_plan_latency_ms": self.metrics.writer_repair_plan_latencies_ms,
                "failed_plan_indexes": _failed_plan_indexes(
                    self.metrics.repair_failure_reasons
                ),
                "parallel_wall_latency_ms": (
                    self.metrics.writer_repair_parallel_wall_latency_ms
                ),
                "repair_failure_details": self.metrics.repair_failure_details,
                "writer_repair_route_outside_names": (
                    self.metrics.writer_repair_route_outside_names
                ),
                "writer_repair_route_violation_reasons": (
                    self.metrics.writer_repair_route_violation_reasons
                ),
                "writer_repair_unsupported_fact_patterns": (
                    self.metrics.writer_repair_unsupported_fact_patterns
                ),
                "writer_repair_sanitizer_actions": (
                    self.metrics.writer_repair_sanitizer_actions
                ),
                "writer_repair_sanitizer_action_count": (
                    self.metrics.writer_repair_sanitizer_action_count
                ),
                "repair_mode": self.metrics.repair_mode,
                "writer_llm_repair_called": self.metrics.writer_llm_repair_called,
            }
        return {
            "followup_round": followup_round,
            "per_plan_latency_ms": (
                self.metrics.writer_repair_followup_plan_latencies_ms
            ),
            "failed_plan_indexes": _failed_plan_indexes(
                self.metrics.writer_repair_followup_failure_reasons
            ),
            "repair_failure_details": (
                self.metrics.writer_repair_followup_failure_details
            ),
            "writer_repair_sanitizer_actions": (
                self.metrics.writer_repair_followup_sanitizer_actions
            ),
            "writer_repair_sanitizer_action_count": (
                self.metrics.writer_repair_followup_sanitizer_action_count
            ),
        }

    def _check_blueprint_integrity(self) -> None:
        violations = check_plan_blueprint_integrity(
            self.plans,
            self.route_plans,
            self.composition_blueprints,
        )
        if not violations:
            return
        all_resolved = True
        for plan_idx in sorted({violation.plan_index for violation in violations}):
            plan_violations = [
                violation
                for violation in violations
                if violation.plan_index == plan_idx
            ]
            zero_idx = plan_idx - 1
            if zero_idx < 0 or zero_idx >= len(self.plans):
                all_resolved = False
                continue
            if zero_idx >= len(self.route_plans):
                all_resolved = False
                continue
            if not normalize_declared_locked_stops(
                self.plans[zero_idx],
                self.route_plans[zero_idx],
                plan_violations,
            ):
                all_resolved = False
        if all_resolved:
            self.plans = normalize_plan_places(
                self.plans,
                self.retrieval,
                route_plans=self.route_plans,
            )
            return
        findings = [
            publish_gate.PublishFinding(
                reason="blueprint_integrity_violation",
                message=(
                    f"plan {violation.plan_index} day {violation.day}: "
                    f"reason={violation.reason}, "
                    f"missing={violation.missing_stops}, "
                    f"extra={violation.extra_stops}"
                ),
                plan_index=violation.plan_index,
                day=violation.day,
            )
            for violation in violations
        ]
        raise publish_gate.PublishGateError(
            publish_gate.PublishGateResult(passed=False, findings=findings)
        )

    def _after_repair_review_decision(
        self,
        route_signature_before_repair: str,
    ) -> dict[str, Any]:
        route_plan_unchanged = (
            route_signature_before_repair == _stable_json_signature(self.route_plans)
        )
        day_place_names_unchanged = _plans_match_locked_day_names(
            self.plans,
            self.route_plans,
        )
        used_place_names_unchanged = _plans_match_locked_used_names(
            self.plans,
            self.route_plans,
        )
        blueprint_violations = check_plan_blueprint_integrity(
            self.plans,
            self.route_plans,
            self.composition_blueprints,
        )
        blueprint_integrity_passed = not blueprint_violations
        preflight = self._check_publish_gate()
        no_new_poi_names = not any(
            finding.reason == "route_outside_poi"
            for finding in preflight.findings
        )
        publish_preflight_passed = preflight.passed

        repair_mode = self.metrics.repair_mode or "unknown"
        writer_llm_repair_called = self.metrics.writer_llm_repair_called
        risk_flags: list[str] = []
        if self._ds_review_mandatory():
            risk_flags.append("speculative_ds_mandatory_review")
        if repair_mode not in {"local_sanitizer", "deterministic_normalize"}:
            risk_flags.append("repair_mode_not_deterministic")
        if writer_llm_repair_called:
            risk_flags.append("writer_llm_repair_called")
        if not route_plan_unchanged:
            risk_flags.append("route_plan_changed")
        if not day_place_names_unchanged:
            risk_flags.append("day_place_names_changed")
        if not used_place_names_unchanged:
            risk_flags.append("used_place_names_changed")
        if not no_new_poi_names:
            risk_flags.append("new_poi_names_detected")
        if not blueprint_integrity_passed:
            risk_flags.append("blueprint_integrity_failed")
        if not publish_preflight_passed:
            risk_flags.append("publish_preflight_failed")

        skipped = not risk_flags
        reason = "deterministic_after_repair_preflight_passed" if skipped else (
            ",".join(risk_flags)
        )
        decision = {
            "after_repair_review_skipped": skipped,
            "after_repair_review_skip_reason": reason,
            "repair_mode": repair_mode,
            "writer_llm_repair_called": writer_llm_repair_called,
            "deterministic_risk_flags": risk_flags,
            "publish_preflight_passed": publish_preflight_passed,
            "publish_preflight_failure_reasons": preflight.failure_reasons,
            "blueprint_integrity_passed": blueprint_integrity_passed,
            "blueprint_integrity_violation_count": len(blueprint_violations),
            "route_plan_unchanged": route_plan_unchanged,
            "day_place_names_unchanged": day_place_names_unchanged,
            "used_place_names_unchanged": used_place_names_unchanged,
            "no_new_poi_names": no_new_poi_names,
        }
        self.metrics.after_repair_review_skipped = skipped
        self.metrics.after_repair_review_skip_reason = reason
        self.metrics.deterministic_risk_flags = risk_flags
        self.metrics.publish_preflight_passed = publish_preflight_passed
        self.metrics.blueprint_integrity_passed = blueprint_integrity_passed
        self.metrics.route_plan_unchanged = route_plan_unchanged
        self.metrics.day_place_names_unchanged = day_place_names_unchanged
        self.metrics.used_place_names_unchanged = used_place_names_unchanged
        self.metrics.no_new_poi_names = no_new_poi_names
        return decision

    async def _skip_after_repair_review(
        self,
        taxonomy: generation_issues.IssueTaxonomyResult,
        decision: dict[str, Any],
    ) -> generation_issues.IssueTaxonomyResult:
        if self._ds_review_mandatory():
            return await self._review_taxonomy(
                "REVIEW_TAXONOMY_AFTER_REPAIR"
            )
        fast_path_taxonomy = _taxonomy_without_repaired_issues(
            taxonomy,
            self.repaired_plan_indexes,
        )
        step_metadata = {
            **decision,
            **_taxonomy_step_metadata(fast_path_taxonomy),
        }

        async def action():
            return fast_path_taxonomy

        result = await _run_observed_step(
            "REVIEW_TAXONOMY_AFTER_REPAIR",
            action,
            on_stage_event=self.on_stage_event,
            set_current_stage=False,
            attempt=self.attempt,
            publish_retry_round=self.publish_retry_round,
            metadata=step_metadata,
            finish_metadata=lambda: step_metadata,
        )
        self._record_post_repair_metrics(result)
        return result

    def _plans_without_food_markers(self) -> list[PlanOutput]:
        """Project delivery text without mutating the reviewed ownership registry."""
        projected = []
        for plan in self.plans:
            text = publish_gate.strip_food_span_markers(plan.plan_text)
            remapped = remap_fragment_registry(plan, updated_text=text)
            if remapped is None:
                raise publish_gate.PublishGateError(publish_gate.PublishGateResult(
                    passed=False, findings=[publish_gate.PublishFinding(
                        reason="fragment_registry_invalidated",
                        message="food marker cleanup crossed a stable POI fragment boundary",
                    )]))
            projected.append(remapped)
        return projected

    async def _run_publish_gate(self) -> publish_gate.PublishGateResult:
        publish_result: publish_gate.PublishGateResult | None = None
        findings_before_final_replacement: list[publish_gate.PublishFinding] = []

        async def action():
            nonlocal publish_result, findings_before_final_replacement
            publish_result = self._check_publish_gate()
            transit_targets = {
                finding.plan_index
                for finding in publish_result.findings
                if (
                    finding.reason in publish_gate.TRANSIT_FINDING_REASONS
                    and finding.plan_index is not None
                )
            }
            if transit_targets:
                findings_before_final_replacement = list(publish_result.findings)
                registry_failures: list[int] = []
                updated_plans: list[PlanOutput] = []
                for index, plan in enumerate(self.plans):
                    if (
                        index + 1 not in transit_targets
                        or index >= len(self.route_plans)
                    ):
                        updated_plans.append(plan)
                        continue
                    route = self.route_plans[index]
                    updated_text = final_writer._ensure_locked_route_text(
                        publish_gate.replace_unsafe_transit_copy(
                            plan,
                            route,
                        ).plan_text,
                        route,
                        plan.composition_blueprint,
                    )
                    registry_safe = remap_fragment_registry(
                        plan,
                        updated_text=updated_text,
                    )
                    if registry_safe is None:
                        registry_failures.append(index + 1)
                        updated_plans.append(plan)
                    else:
                        updated_plans.append(registry_safe)
                self.plans = updated_plans
                publish_result = self._check_publish_gate()
                if registry_failures:
                    registry_findings = [
                        publish_gate.PublishFinding(
                            reason="fragment_registry_invalidated",
                            message=(
                                "final transit cleanup crossed a stable "
                                "POI fragment boundary"
                            ),
                            plan_index=plan_index,
                        )
                        for plan_index in registry_failures
                    ]
                    findings_before_final_replacement.extend(registry_findings)
                    publish_result = publish_gate.PublishGateResult(
                        passed=False,
                        findings=[
                            *publish_result.findings,
                            *registry_findings,
                        ],
                    )
            if not publish_result.passed:
                raise publish_gate.PublishGateError(publish_result)
            # Keep the reviewed internal representation (including food ownership)
            # intact. Public prose is an exact marker-only projection returned by
            # the delivery paths; substantive edits still invalidate the receipt.
            try:
                marker_free_plans = self._plans_without_food_markers()
            except publish_gate.PublishGateError as exc:
                publish_result = exc.result  # Preserve the existing failed-stage diagnostics.
                raise
            if not (self.generation_policy_version == "v0.12" and self._body_review_current()):
                self.plans = marker_free_plans
            return publish_result

        result = await _run_observed_step(
            "PUBLISH_GATE",
            action,
            on_stage_event=self.on_stage_event,
            set_current_stage=False,
            attempt=self.attempt,
            publish_retry_round=self.publish_retry_round,
            metadata={
                "plan_count": len(self.plans),
                "route_plan_count": len(self.route_plans or []),
            },
            finish_metadata=lambda: {
                "final_publish_gate_passed": bool(
                    publish_result and publish_result.passed
                ),
                "activity_local_completion_enabled": (
                    self.metrics.activity_local_completion_enabled
                ),
                "activity_local_completion_before_missing_count": (
                    self.metrics.activity_local_completion_before_missing_count
                ),
                "activity_local_completion_attempted_count": (
                    self.metrics.activity_local_completion_attempted_count
                ),
                "activity_local_completion_applied_count": (
                    self.metrics.activity_local_completion_applied_count
                ),
                "activity_local_completion_skipped_count": (
                    self.metrics.activity_local_completion_skipped_count
                ),
                "activity_local_completion_after_missing_count": (
                    self.metrics.activity_local_completion_after_missing_count
                ),
                "activity_local_completion_unresolved_keys": (
                    self.metrics.activity_local_completion_unresolved_keys
                ),
                **(
                    publish_result.to_metrics()
                    if publish_result is not None
                    else {}
                ),
            },
        )
        self.metrics.publish_gate_findings_after_repair = [
            finding.to_dict()
            for finding in (
                findings_before_final_replacement or result.findings
            )
        ]
        self.metrics.final_publish_gate_passed = result.passed
        # These flags describe the final publishable output. Earlier unsafe copy
        # remains available in publish_gate_findings_after_repair.
        self.metrics.route_plan_unchanged = not route_plan_violations(
            self.plans,
            self.route_plans,
        )
        self.metrics.day_place_names_unchanged = _plans_match_locked_day_names(
            self.plans,
            self.route_plans,
        )
        self.metrics.used_place_names_unchanged = _plans_match_locked_used_names(
            self.plans,
            self.route_plans,
        )
        self.metrics.no_new_poi_names = not any(
            finding.reason == "route_outside_poi"
            for finding in result.findings
        )
        self.metrics.merge_known_metrics({"publish_gate_passed": True})
        return result

    def _check_fail_closed(
        self,
        taxonomy: generation_issues.IssueTaxonomyResult,
    ) -> None:
        if taxonomy.fail_closed_reasons:
            findings = _taxonomy_findings(
                [
                    issue
                    for issue in taxonomy.issues
                    if issue.publish_action == "FAIL_CLOSED"
                ],
                prefix="generation taxonomy fail-closed",
            )
            raise publish_gate.PublishGateError(
                publish_gate.PublishGateResult(passed=False, findings=findings)
            )

    def _check_unresolved(
        self,
        taxonomy: generation_issues.IssueTaxonomyResult,
    ) -> None:
        if (
            taxonomy.fail_closed_reasons
            or taxonomy.issue_counts.get("BLOCKER", 0)
            or taxonomy.issue_counts.get("REPAIR", 0)
        ):
            findings = _taxonomy_findings(
                [
                    issue
                    for issue in taxonomy.issues
                    if issue.category in {"BLOCKER", "REPAIR"}
                ],
                prefix="generation taxonomy unresolved issue",
            )
            raise publish_gate.PublishGateError(
                publish_gate.PublishGateResult(passed=False, findings=findings)
            )

    def _record_pre_repair_metrics(
        self,
        taxonomy: generation_issues.IssueTaxonomyResult,
    ) -> None:
        self.metrics.review_issue_counts_before_repair = taxonomy.issue_counts
        self.metrics.copy_quality_before_repair = (
            generation_issues.summarize_copy_quality_issues(taxonomy.issues)
        )
        self.metrics.repair_target_plan_indexes = taxonomy.repair_target_plan_indexes
        self.metrics.data_backlog_reasons = taxonomy.data_backlog_reasons
        self.metrics.resolver_notes = taxonomy.resolver_notes

    def _record_post_repair_metrics(
        self,
        taxonomy: generation_issues.IssueTaxonomyResult,
    ) -> None:
        self.metrics.review_issue_counts_after_repair = taxonomy.issue_counts
        self.metrics.copy_quality_after_repair = (
            generation_issues.summarize_copy_quality_issues(taxonomy.issues)
        )
        self.metrics.data_backlog_reasons = list(dict.fromkeys([
            *self.metrics.data_backlog_reasons,
            *taxonomy.data_backlog_reasons,
        ]))
        self.metrics.resolver_notes = list(dict.fromkeys([
            *self.metrics.resolver_notes,
            *taxonomy.resolver_notes,
        ]))

    def _record_repair_budget(
        self,
        budget: RepairBudgetTracker,
        *,
        exceeded: bool = False,
    ) -> None:
        self.metrics.merge_known_metrics({
            "repair_budget": budget.to_metrics(exceeded=exceeded)
        })

    def _format_review_notes(
        self,
        taxonomy: generation_issues.IssueTaxonomyResult,
    ) -> str:
        return (
            "审核 taxonomy: "
            + ", ".join(
                f"{key}={value}"
                for key, value in taxonomy.issue_counts.items()
            )
        )

    def _raise_budget_exceeded(self, exc: RepairBudgetExceeded) -> None:
        raise publish_gate.PublishGateError(
            publish_gate.PublishGateResult(
                passed=False,
                findings=[publish_gate.PublishFinding(
                    reason="repair_budget_exceeded",
                    message=(
                        f"repair wall time {exc.elapsed_seconds:.0f}s "
                        f"exceeded budget {exc.max_wall_seconds:.0f}s"
                    ),
                )],
            )
        )

    def _raise_repair_failure(
        self,
        reasons: list[str],
        details: list[dict[str, Any]],
        *,
        prefix: str,
    ) -> None:
        logger.warning(
            "%s reasons=%s details=%s",
            prefix,
            reasons,
            json.dumps(details, ensure_ascii=False)[:1200],
        )
        findings = [
            publish_gate.PublishFinding(
                reason=reason,
                message=(
                    f"{prefix}: {reason}; details="
                    + json.dumps(details, ensure_ascii=False)[:800]
                ),
            )
            for reason in reasons
        ]
        raise publish_gate.PublishGateError(
            publish_gate.PublishGateResult(passed=False, findings=findings)
        )


def _detail_values(details: list[dict[str, Any]], key: str) -> list[str]:
    return list(dict.fromkeys(
        value
        for detail in details
        for value in detail.get(key, [])
        if isinstance(value, str) and value
    ))


def _failed_plan_indexes(reasons: list[str]) -> list[int]:
    indexes = []
    for reason in reasons:
        suffix = reason.rsplit(":", 1)[-1]
        if suffix.isdigit():
            indexes.append(int(suffix))
    return indexes


def _is_original_final_writer_generate() -> bool:
    """Keep legacy workflow tests on their patched writer path."""
    return getattr(final_writer.generate, "__module__", "") == "src.agents.final_writer"


def _is_original_yuntu_review_collect() -> bool:
    """Keep legacy workflow tests on their patched review path."""
    return (
        getattr(
            yuntu_review.collect_semantic_generation_issues,
            "__module__",
            "",
        )
        == "src.agents.yuntu_review"
    )


def generation_issues_issue_reason(issue):
    return str(getattr(issue, "reason", "") or "")
