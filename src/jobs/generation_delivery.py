"""Privacy-safe route observation types used by route diagnostics."""

from __future__ import annotations

from pydantic import BaseModel, Field


class RouteSearchObservation(BaseModel):
    iterations: int | None = None
    iteration_limit: int | None = None
    elapsed_ms: int | None = None
    time_limit_ms: int | None = None


class RouteViolation(BaseModel):
    day: int | None = None
    code: str
    actual: float | None = None
    limit: float | None = None
    unit: str = "NONE"


class RouteDiagnostics(BaseModel):
    evidence_state: str
    stop_reason: str
    candidate_count: int
    eliminated_counts: dict[str, int] = Field(default_factory=dict)
    search: RouteSearchObservation
    violations: list[RouteViolation] = Field(default_factory=list)
    truncated: bool = False
    replay_available: bool = False
