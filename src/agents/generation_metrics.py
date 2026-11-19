"""Structured metrics for the write-review-publish pipeline."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class GenerationMetrics:
    writer_original_model: str = ""
    writer_original_attempt: int = 1
    writer_original_latency_ms: int = 0
    writer_plan_concurrency_enabled: bool = False
    writer_plan_concurrency_used: bool = False
    writer_plan_concurrency_fallback_used: bool = False
    writer_plan_legacy_fallback_latency_ms: int = 0
    writer_plan_success_count: int = 0
    writer_plan_failure_count: int = 0
    writer_plan_latencies_ms: list[int] = field(default_factory=list)
    writer_plan_index_alignment_passed: bool = True
    writer_plan_failed_plan_indexes: list[int] = field(default_factory=list)
    single_plan_unwrapped_plans_array: bool = False
    single_plan_unwrapped_plan_indexes: list[int] = field(default_factory=list)
    writer_plan_retry_used: bool = False
    writer_plan_retry_count: int = 0
    writer_plan_attempt_count: int = 0
    writer_plan_attempt_parse_error_types: list[str] = field(default_factory=list)
    writer_plan_attempt_raw_lengths: list[int] = field(default_factory=list)
    writer_plan_attempt_json_extract_failed: list[bool] = field(default_factory=list)
    writer_plan_retry_latency_ms: int = 0
    writer_plan_parallel_wall_latency_ms: int = 0
    writer_plan_name_backend_owned: bool = False
    writer_initial_sanitizer_actions: list[dict[str, Any]] = field(default_factory=list)
    writer_initial_sanitizer_action_count: int = 0
    writer_action_plan_complete: bool = False
    writer_action_contract_count: int = 0
    writer_action_plan_missing_count: int = 0
    writer_action_plan_missing_keys: list[dict[str, int]] = field(default_factory=list)
    writer_keyed_fragment_contract_enabled: bool = False
    writer_keyed_fragment_count: int = 0
    writer_keyed_fragment_fallback_count: int = 0
    writer_keyed_fragment_fallback_keys: list[dict[str, int]] = field(
        default_factory=list
    )
    writer_keyed_fragment_ignored_count: int = 0
    writer_keyed_fragment_ignored_keys: list[dict[str, int]] = field(
        default_factory=list
    )
    writer_keyed_fragment_invalid_details: list[dict[str, Any]] = field(
        default_factory=list
    )
    writer_keyed_fragment_repair_attempted: bool = False
    writer_keyed_fragment_repair_target_count: int = 0
    writer_keyed_fragment_repair_applied_count: int = 0
    writer_keyed_fragment_repair_remaining_count: int = 0
    writer_keyed_fragment_repair_latency_ms: int = 0
    writer_keyed_fragment_repair_failure_reason: str = ""
    writer_keyed_preference_repair_target_count: int = 0
    writer_keyed_preference_repair_applied_count: int = 0
    writer_keyed_preference_repair_remaining_count: int = 0
    review_keyed_fragment_repair_attempted: bool = False
    review_keyed_fragment_repair_target_count: int = 0
    review_keyed_fragment_repair_applied_count: int = 0
    review_keyed_fragment_repair_remaining_count: int = 0
    review_keyed_fragment_repair_timeout_seconds: float = 0.0
    review_keyed_fragment_repair_latency_ms: int = 0
    review_keyed_fragment_repair_review_latency_ms: int = 0
    review_keyed_fragment_repair_review_retry_count: int = 0
    review_keyed_fragment_repair_failure_reason: str = ""
    review_keyed_fragment_repair_failure_policy: str = ""
    review_keyed_fragment_required_count: int = 0
    explicit_preference_evaluated: bool = False
    explicit_preference_dimensions: list[str] = field(default_factory=list)
    explicit_preference_findings: list[dict[str, Any]] = field(default_factory=list)
    explicit_preference_arrangement_failures: list[dict[str, Any]] = field(
        default_factory=list
    )
    explicit_preference_body_missing_keys: list[dict[str, int]] = field(
        default_factory=list
    )
    explicit_preference_repair_attempted: bool = False
    explicit_preference_repair_keys: list[dict[str, int]] = field(default_factory=list)
    explicit_preference_repair_applied_keys: list[dict[str, int]] = field(
        default_factory=list
    )
    explicit_preference_repair_failure_reason: str = ""
    explicit_preference_before_reasons: list[str] = field(default_factory=list)
    explicit_preference_after_reasons: list[str] = field(default_factory=list)
    post_review_ds_rescue_available: bool = False
    post_review_ds_rescue_attempted: bool = False
    post_review_ds_rescue_reason: str = ""
    post_review_ds_rescue_succeeded: bool = False
    post_review_ds_rescue_failure_reason: str = ""
    post_review_ds_rescue_review_latency_ms: int = 0
    post_review_ds_rescue_issue_counts: dict[str, int] = field(default_factory=dict)
    activity_local_completion_enabled: bool = False
    activity_local_completion_before_missing_count: int = 0
    activity_local_completion_attempted_count: int = 0
    activity_local_completion_applied_count: int = 0
    activity_local_completion_skipped_count: int = 0
    activity_local_completion_after_missing_count: int = 0
    activity_local_completion_applied_keys: list[dict[str, int]] = field(
        default_factory=list
    )
    activity_local_completion_skipped_details: list[dict[str, Any]] = field(
        default_factory=list
    )
    activity_local_completion_unresolved_keys: list[dict[str, int]] = field(
        default_factory=list
    )

    writer_repair_model: str = ""
    writer_repair_attempt: int = 0
    writer_repair_latency_ms: int = 0
    repair_target_plan_indexes: list[int] = field(default_factory=list)
    repair_success: bool = False
    repair_failure_reasons: list[str] = field(default_factory=list)
    repair_failure_details: list[dict[str, Any]] = field(default_factory=list)
    writer_repair_parallel_used: bool = False
    writer_repair_plan_latencies_ms: list[int] = field(default_factory=list)
    writer_repair_parallel_wall_latency_ms: int = 0
    writer_repair_failure_count: int = 0
    writer_repair_route_outside_names: list[str] = field(default_factory=list)
    writer_repair_route_violation_reasons: list[str] = field(default_factory=list)
    writer_repair_unsupported_fact_patterns: list[str] = field(default_factory=list)
    writer_repair_sanitizer_actions: list[dict[str, Any]] = field(default_factory=list)
    writer_repair_sanitizer_action_count: int = 0
    repair_mode: str = ""
    writer_llm_repair_called: bool = False
    initial_review_skipped: bool = False
    initial_review_skip_reason: str = ""
    review_risk_skip_enabled: bool = False
    initial_review_llm_called: bool = False
    initial_review_llm_issue_count: int = 0
    after_repair_review_skipped: bool = False
    after_repair_review_skip_reason: str = ""
    deterministic_risk_flags: list[str] = field(default_factory=list)
    review_shadow_enabled: bool = False
    risk_flags: list[str] = field(default_factory=list)
    risk_flag_counts: dict[str, int] = field(default_factory=dict)
    risk_score: int = 0
    shadow_risk_level: str = "LOW"
    risk_level: str = "LOW"
    risk_level_source: str = "no_risk_signals"
    risk_level_semantics: str = ""
    confirmed_generation_issues: dict[str, Any] = field(default_factory=dict)
    review_shadow_deterministic_risk_flags: list[str] = field(default_factory=list)
    review_shadow_deterministic_issue_counts: dict[str, int] = field(default_factory=dict)
    review_shadow_publish_gate_issue_counts: dict[str, int] = field(default_factory=dict)
    review_shadow_flag_counts: dict[str, int] = field(default_factory=dict)
    review_shadow_llm_issue_counts: dict[str, int] = field(default_factory=dict)
    review_shadow_mismatch_summary: dict[str, Any] = field(default_factory=dict)
    review_dropped_backend_owned_count: int = 0
    review_dropped_backend_owned_reasons: list[str] = field(default_factory=list)
    review_dropped_backend_owned_issues: list[dict[str, Any]] = field(
        default_factory=list
    )
    publish_preflight_passed: bool = False
    final_publish_gate_passed: bool = False
    blueprint_integrity_passed: bool = False
    route_plan_unchanged: bool = False
    day_place_names_unchanged: bool = False
    used_place_names_unchanged: bool = False
    no_new_poi_names: bool = False

    # v0.8.13 O3 dispatch / recovery counters
    dispatch_action: str = ""
    dispatch_reasons: list[str] = field(default_factory=list)
    dispatch_notes: list[str] = field(default_factory=list)
    publish_retry_count: int = 0
    fragment_repair_call_count: int = 0
    fragment_repair_target_ids: list[str] = field(default_factory=list)
    fragment_repair_failure_reason: str = ""
    pre_review_fragment_repair_attempted: bool = False
    pre_review_fragment_repair_applied: bool = False
    full_writer_generation_count: int = 1
    residual_denial_reason: str = ""
    content_dispatch_used: bool = False
    whole_plan_repair_bypassed: bool = True

    # v0.8.13 O4 review policy / aggregates
    review_ran: bool = False
    review_required_for_complete_body: bool = False
    full_candidate_review_skip_disabled: bool = False
    review_transport_fail_closed: bool = False
    review_transport_fail_closed_reason: str = ""
    o4_observability_enabled: bool = True

    writer_repair_followup_attempt: int = 0
    writer_repair_followup_target_plan_indexes: list[int] = field(default_factory=list)
    writer_repair_followup_repaired_plan_indexes: list[int] = field(default_factory=list)
    writer_repair_followup_failure_reasons: list[str] = field(default_factory=list)
    writer_repair_followup_failure_details: list[dict[str, Any]] = field(default_factory=list)
    writer_repair_followup_sanitizer_actions: list[dict[str, Any]] = field(default_factory=list)
    writer_repair_followup_sanitizer_action_count: int = 0
    writer_repair_followup_plan_latencies_ms: list[int] = field(default_factory=list)
    writer_repair_followup_latency_ms: int = 0

    resolver_notes: list[str] = field(default_factory=list)
    data_backlog_reasons: list[str] = field(default_factory=list)
    review_plan_concurrency_used: bool = False
    review_plan_latencies_ms: list[int] = field(default_factory=list)
    review_parallel_wall_latency_ms: int = 0
    review_global_issue_count: int = 0
    review_issue_counts_before_repair: dict[str, int] = field(default_factory=dict)
    copy_quality_before_repair: dict[str, Any] = field(default_factory=dict)
    review_issue_counts_after_repair: dict[str, int] = field(default_factory=dict)
    copy_quality_after_repair: dict[str, Any] = field(default_factory=dict)

    publish_gate_findings_after_repair: list[dict[str, Any]] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    def merge_known_metrics(self, metrics_dict: dict[str, Any]) -> None:
        for key, value in metrics_dict.items():
            if hasattr(self, key) and key != "extra":
                setattr(self, key, value)
            else:
                self.extra[key] = value

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        extra = result.pop("extra", {})
        result.update(extra)
        return result
