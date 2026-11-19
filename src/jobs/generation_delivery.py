"""Strict v0.12 generation policy and delivery contracts.

The models in this module are transport/persistence contracts.  They do not
decide quota or generation behavior.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


GenerationPolicyVersion = Literal["legacy", "v0.12"]
DeliveryOutcome = Literal["PENDING", "DETAILED", "BASIC", "NO_ROUTE", "FAILED"]
ReviewState = Literal[
    "NOT_RUN",
    "CODE_ONLY",
    "MODEL_PASSED",
    "UNAVAILABLE",
    "ISSUES_UNRESOLVED",
]
SelectorSource = Literal["LLM", "DETERMINISTIC_FALLBACK", "NOT_RUN"]

EXECUTION_REASON_CODES = frozenset({
    "AUTH", "MODEL_PERMISSION", "RATE_LIMIT", "TRANSPORT", "TIMEOUT",
    "HTTP_5XX", "WIRE_INVALID", "SCHEMA_INVALID", "TRUNCATED",
    "CANCELLED", "UNKNOWN", "NO_CANDIDATES", "DEADLINE_EXHAUSTED",
    "CONFIG_UNAVAILABLE", "CALL_LIMIT_EXHAUSTED", "DETERMINISTIC_FALLBACK",
    "CODE_ONLY", "LOW_RISK",
})
ROUTE_ELIMINATION_CODES = frozenset({
    "HARD_INELIGIBLE", "TEMPORALLY_UNSCHEDULABLE", "CAPACITY_LIMIT",
    "ROUTE_FEASIBILITY_LIMIT", "NOT_CHOSEN_FOR_FINAL_ROUTE",
})
ROUTE_VIOLATION_CODES = frozenset({
    "FULL_DAY_CONFLICT", "INCOMPLETE_DAYS", "INVALID_MEMBERSHIP", "MISSING_MUST_INCLUDE",
    "ACCOMMODATION_POLICY_MISMATCH", "LOCKED_ACCOMMODATION_MISMATCH",
    "MISSING_LOCKED_ACCOMMODATION", "DAYTIME_AFTER_EVENING",
    "TEMPORALLY_UNSCHEDULABLE", "HARD_AVOID", "NEAR_DUPLICATE",
    "INVALID_ACCESS_COVERAGE", "ACCESS_ENDPOINT_MISMATCH",
    "ACCESS_ANCHOR_MISMATCH", "DAY_STRUCTURE", "DUPLICATE_PLACE",
    "NO_DAYTIME_ACTIVITY", "INELIGIBLE_PLACE", "POI_LEG_MISMATCH",
    "INVALID_LEG", "LOAD_LIMIT", "DAILY_COMMUTE_LIMIT", "SINGLE_LEG_LIMIT",
})

BASIC_REASON_CODES = frozenset(
    {
        "REVIEW_UNAVAILABLE",
        "REVIEW_ISSUES_UNRESOLVED",
        "WRITER_UNAVAILABLE",
        "PREFERENCE_UNVERIFIED",
    }
)
NO_ROUTE_REASON_CODES = frozenset(
    {
        "NO_CANDIDATES",
        "SEARCH_BUDGET_EXHAUSTED",
        "CONSTRAINT_CONFLICT",
        "ROUTE_DATA_UNAVAILABLE",
        "NO_USABLE_ROUTE",
    }
)


class StrictContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GenerationDelivery(StrictContractModel):
    schema_version: Literal["1.0"] = "1.0"
    outcome: DeliveryOutcome
    review_state: ReviewState
    reason_code: str | None = Field(default=None, max_length=80, pattern=r"^[A-Z0-9_]+$")
    selector_source: SelectorSource

    @model_validator(mode="after")
    def validate_outcome(self) -> "GenerationDelivery":
        if self.outcome == "PENDING":
            if self.review_state != "NOT_RUN" or self.reason_code is not None:
                raise ValueError("PENDING delivery cannot claim a review decision or reason")
        elif self.outcome == "DETAILED":
            if self.review_state not in {"CODE_ONLY", "MODEL_PASSED"}:
                raise ValueError("DETAILED delivery requires a passed review decision")
            if self.reason_code is not None:
                raise ValueError("DETAILED delivery cannot carry a reason_code")
        elif self.outcome == "BASIC":
            if self.reason_code not in BASIC_REASON_CODES:
                raise ValueError("BASIC delivery requires an approved reason_code")
            expected_review = {
                "REVIEW_UNAVAILABLE": "UNAVAILABLE",
                "REVIEW_ISSUES_UNRESOLVED": "ISSUES_UNRESOLVED",
                "WRITER_UNAVAILABLE": "NOT_RUN",
                "PREFERENCE_UNVERIFIED": "NOT_RUN",
            }[self.reason_code]
            if self.review_state != expected_review:
                raise ValueError("BASIC review_state contradicts its reason_code")
        elif self.outcome == "NO_ROUTE":
            if self.reason_code not in NO_ROUTE_REASON_CODES:
                raise ValueError("NO_ROUTE delivery requires an approved reason_code")
            if self.review_state != "NOT_RUN":
                raise ValueError("NO_ROUTE delivery cannot claim review completion")
        elif self.reason_code is None:
            raise ValueError("FAILED delivery requires a safe reason_code")
        return self


class ExecutionCall(StrictContractModel):
    sequence: int = Field(ge=1)
    provider: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=128)
    role: str = Field(min_length=1, max_length=32)
    purpose: str = Field(min_length=1, max_length=64)
    endpoint_id: str | None = Field(default=None, max_length=64)
    status: Literal["SUCCESS", "FAILED", "TIMEOUT", "CANCELLED", "REJECTED"]
    duration_ms: int | None = Field(default=None, ge=0)
    error_code: str | None = Field(default=None, max_length=80)
    failure_scope: Literal["MODEL", "PROVIDER", "ACCOUNT", "UNKNOWN"] | None = None


class ExecutionObservation(StrictContractModel):
    schema_version: Literal["1.0"] = "1.0"
    state: Literal[
        "NOT_ATTEMPTED",
        "DETERMINISTIC",
        "CALL_REJECTED",
        "MODEL_SUCCEEDED",
        "FALLBACK_USED",
        "FAILED",
        "SKIPPED_LOW_RISK",
    ]
    reason_code: str | None = Field(default=None, max_length=80)
    adopted_call_sequence: int | None = Field(default=None, ge=1)
    calls: list[ExecutionCall] = Field(default_factory=list, max_length=20)
    truncated: bool = False
    total_call_count: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_counts(self) -> "ExecutionObservation":
        if self.reason_code is not None and self.reason_code not in EXECUTION_REASON_CODES:
            raise ValueError("execution reason_code is not allowlisted")
        if self.total_call_count < len(self.calls):
            raise ValueError("total_call_count cannot be smaller than calls")
        if self.truncated != (self.total_call_count > len(self.calls)):
            raise ValueError("truncated must describe omitted calls")
        sequences = [call.sequence for call in self.calls]
        if len(sequences) != len(set(sequences)):
            raise ValueError("call sequences must be unique")
        if self.adopted_call_sequence is not None:
            adopted = next(
                (call for call in self.calls if call.sequence == self.adopted_call_sequence),
                None,
            )
            if adopted is None or adopted.status != "SUCCESS":
                raise ValueError("adopted_call_sequence must reference a returned success")
        return self


class RouteSearchObservation(StrictContractModel):
    iterations: int | None = Field(default=None, ge=0)
    iteration_limit: int | None = Field(default=None, ge=0)
    elapsed_ms: int | None = Field(default=None, ge=0)
    time_limit_ms: int | None = Field(default=None, ge=0)


class RouteViolation(StrictContractModel):
    day: int | None = Field(default=None, ge=1)
    code: str = Field(min_length=1, max_length=80)
    actual: float | None = None
    limit: float | None = None
    unit: Literal["MINUTES", "COUNT", "METERS", "NONE"]

    @model_validator(mode="after")
    def validate_code(self) -> "RouteViolation":
        if self.code not in ROUTE_VIOLATION_CODES:
            raise ValueError("route violation code is not allowlisted")
        return self


class RouteDiagnostics(StrictContractModel):
    schema_version: Literal["1.0"] = "1.0"
    evidence_state: Literal["COMPLETE", "PARTIAL", "UNAVAILABLE"]
    stop_reason: Literal[
        "SUCCESS",
        "NO_CANDIDATES",
        "SEARCH_BUDGET_EXHAUSTED",
        "CONSTRAINT_CONFLICT",
        "DATA_UNAVAILABLE",
        "ERROR",
    ]
    candidate_count: int | None = Field(default=None, ge=0)
    eliminated_counts: dict[str, int] = Field(default_factory=dict)
    search: RouteSearchObservation
    violations: list[RouteViolation] = Field(default_factory=list, max_length=20)
    truncated: bool
    replay_available: bool

    @model_validator(mode="after")
    def validate_eliminated_counts(self) -> "RouteDiagnostics":
        if any(
            key not in ROUTE_ELIMINATION_CODES or value < 0
            for key, value in self.eliminated_counts.items()
        ):
            raise ValueError("eliminated_counts must contain bounded non-negative counts")
        return self


def validate_delivery_for_job(
    delivery: GenerationDelivery,
    *,
    status: str,
    result_type: str | None,
    result_record_id: int | None,
    published_variant: str | None = None,
    delivery_status: str | None = None,
) -> None:
    """Validate the authoritative delivery object against persisted job facts."""
    outcome = delivery.outcome
    if outcome == "PENDING":
        valid = status in {"PENDING", "RUNNING"} and result_type is None and result_record_id is None
    elif outcome == "DETAILED":
        valid = (
            status == "SUCCESS"
            and result_type == "PLAN_READY"
            and result_record_id is not None
            and published_variant == "normal"
            and delivery_status == "NORMAL"
        )
    elif outcome == "BASIC":
        valid = (
            status == "SUCCESS"
            and result_type == "PLAN_READY"
            and result_record_id is not None
            and published_variant == "safe"
            and delivery_status == "DEGRADED"
        )
    elif outcome == "NO_ROUTE":
        valid = (
            status == "SUCCESS"
            and result_type in {"NO_CANDIDATES", "NO_USABLE_ROUTE"}
            and result_record_id is None
            and published_variant is None
            and delivery_status is None
        )
    else:
        valid = (
            status in {"FAILED", "TIMEOUT", "REJECTED"}
            and result_type is None
            and result_record_id is None
            and published_variant is None
            and delivery_status is None
        )
    if not valid:
        raise ValueError("delivery contradicts job/result facts")


def pending_delivery() -> GenerationDelivery:
    return GenerationDelivery(
        outcome="PENDING",
        review_state="NOT_RUN",
        selector_source="NOT_RUN",
    )
