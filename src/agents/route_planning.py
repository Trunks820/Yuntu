"""Shared route construction, commute providers and route validation."""

from __future__ import annotations

import asyncio
import contextvars
import itertools
import json
import logging
import math
import re
import time
import weakref
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx
from pydantic import ValidationError
from sqlalchemy import text

from src.agents.pace import (
    RELAXED_PACE_MARKERS,
    commute_estimate_speed_kmh,
    daily_commute_budget_minutes,
    generation_base_mode,
    single_leg_max_minutes,
)
from src.agents.poi_alias import ALIAS_SUFFIXES
from src.agents.preferences import (
    NIGHT_VIEW,
    food_preference_active,
    is_food_partition_place,
    is_nature_core_place as _is_nature_core_place,
    nature_preference_active,
    scoring_dimension_ids,
)
from src.agents.schema import (
    CandidatePlace,
    CommuteLeg,
    EffectiveCommuteMode,
    PoiSelectionResult,
    QualifiedRouteSupplement,
    RetrievalResult,
    RouteDayGroup,
    AccessLeg,
    RoutePlan,
    RouteMembershipLedger,
    SelectedRouteMembership,
    TransitDetailQuality,
    TransitStep,
    TripRequest,
    PlanOutput,
    parse_hhmm,
)
from src.config import get_settings
from src.agents.amap_keys import (
    ROUTE_KEY_BUCKET,
    classify_amap_failure,
    configured_amap_keys,
    get_amap_key_coordinator,
    key_fingerprint,
    redact_amap_key,
)
from src.agents.route_feasibility import (
    POLICY_VERSION,
    DayContext,
    build_visit_profile,
    evaluate_day,
    daytime_activity,
    valid_coordinate,
    access_summary,
)
from src.cost_sources.common import utc_now
from src.cost_sources.local_transport import (
    adapt_amap_route_fare,
    fare_for_locked_leg,
)
from src.cost_sources.models import SourceObservation
from src.pipeline.db import get_session_factory

logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)

AMAP_DRIVING_URL = "https://restapi.amap.com/v3/direction/driving"
AMAP_V5_ROUTE_URLS: dict[EffectiveCommuteMode, str] = {
    "driving": "https://restapi.amap.com/v5/direction/driving",
    "walking": "https://restapi.amap.com/v5/direction/walking",
    "cycling": "https://restapi.amap.com/v5/direction/bicycling",
    "transit": "https://restapi.amap.com/v5/direction/transit/integrated",
}
REDIS_ROUTE_CACHE_TIMEOUT_SECONDS = 0.75
EARTH_RADIUS_KM = 6371.0088
AMAP_ROUTE_RATE_LIMIT_INFOCODES = {
    "10003",  # daily query over limit
    "10004",  # access too frequent
    "10010",  # IP query over limit
    "10014",  # QPS exceeded
    "10029",  # account query over limit on some Amap products
}
AMAP_ROUTE_RATE_LIMIT_MARKERS = (
    "QPS",
    "QUOTA",
    "OVER_LIMIT",
    "TOO_FREQUENT",
    "ACCESS_TOO_FREQUENT",
    "DAILY_QUERY_OVER_LIMIT",
    "USER_DAILY_QUERY_OVER_LIMIT",
    "IP_QUERY_OVER_LIMIT",
    "HAS_EXCEEDED",
    "\u8d85\u9650",
    "\u8d85\u8fc7",
    "\u9891\u7e41",
)


class _ProcessWideAmapRateLimiter:
    """Share Amap start-rate capacity across every job in one event loop."""

    def __init__(self) -> None:
        self._states: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()

    def _state(self) -> tuple[asyncio.Lock, dict[tuple[str, ...], float]]:
        loop = asyncio.get_running_loop()
        state = self._states.get(loop)
        if state is None:
            state = (asyncio.Lock(), {})
            self._states[loop] = state
        return state

    async def wait_for_slot(
        self,
        *,
        scope: tuple[str, ...],
        min_interval: float,
        deadline_monotonic: float | None,
    ) -> float | None:
        lock, next_start_by_scope = self._state()
        async with lock:
            now = time.monotonic()
            scheduled = max(now, next_start_by_scope.get(scope, 0.0))
            if deadline_monotonic is not None and scheduled >= deadline_monotonic:
                return None
            next_start_by_scope[scope] = scheduled + max(0.0, min_interval)
        wait_seconds = max(0.0, scheduled - time.monotonic())
        if wait_seconds > 0:
            await asyncio.sleep(wait_seconds)
        if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
            return None
        return wait_seconds

    def reset_current_loop(self) -> None:
        """Test-only reset without disturbing limiters owned by other loops."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._states.pop(loop, None)


_PROCESS_WIDE_AMAP_RATE_LIMITER = _ProcessWideAmapRateLimiter()
# Task-local so concurrent legs on one provider do not swap keys.
_CURRENT_AMAP_ROUTE_KEY: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_amap_route_key",
    default=None,
)
REMOTE_CENTER_DISTANCE_KM = 35.0
REMOTE_CLUSTER_DISTANCE_KM = 25.0
URBAN_CORE_NEIGHBOR_KM = 15.0
URBAN_CORE_SAMPLE_SIZE = 5
REMOTE_NAME_GROUP_ALIASES = {
    "pingtan": (
        "平潭",
        "龙王头",
        "长江奥",
        "北港村",
        "68海里",
        "海螺塔",
        "最美环岛路",
        "猴研岛",
    ),
}
REMOTE_NAME_GROUP_CONTEXT = {
    "pingtan": "平潭",
}
_NEAR_DUPLICATE_CHAR_MAP = str.maketrans({
    "镋": "镗",
})
_NEAR_DUPLICATE_SUFFIXES = ALIAS_SUFFIXES
_NEAR_DUPLICATE_CONTAINMENT_MIN_LEN = 2
_NEAR_DUPLICATE_CONTAINMENT_MAX_DISTANCE_KM = 1.5

_EVENING_MARKERS = ("夜景", "日落", "夜游", "灯光", "夜晚", "不夜城", "夜市")
_DAYTIME_MARKERS = ("白天", "上午", "下午", "清晨", "早上", "日间")
# "避开白天高温", "白天没啥氛围" say to go after dark (11-18: 大唐不夜城 read as daytime).
_DAYTIME_NEGATED = re.compile(
    r"(?:避开|错开|躲开|不要|别|不建议)[^，。；！？]{0,4}(?:白天|日间|上午|下午)"
    r"|(?:白天|日间)[^，。；！？]{0,3}(?:没|不|无|一般|平淡|太晒|太热)"
)
EVENING_ONLY_CAPACITY_MINUTES = 120
RELAXED_PREFERENCE_MARKERS = RELAXED_PACE_MARKERS
MULTI_DAY_SAME_AREA_CENTER_KM = 2.5
MULTI_DAY_ONE_CLUSTER_RADIUS_KM = 4.0
MULTI_DAY_SPREAD_MIN_CENTER_KM = 6.0
MULTI_DAY_SPREAD_MEDIAN_CENTER_KM = 8.0
MULTI_DAY_SPREAD_GATE_HARD_MIN_CENTER_KM = 5.0
MULTI_DAY_SPREAD_GATE_PASS_MIN_CENTER_KM = 8.0
MULTI_DAY_SCORE_MAX_MIN_DISTANCE_KM = 8.0
MULTI_DAY_SCORE_MAX_MEDIAN_DISTANCE_KM = 12.0
MULTI_DAY_SCORE_MAX_OVERALL_RADIUS_KM = 15.0
MULTI_DAY_SPREAD_GATE_HARD_PENALTY = 25.0
MULTI_DAY_SPREAD_GATE_SOFT_PENALTY = 8.0
MULTI_DAY_SPREAD_GATE_HARD_SCORE_CAP = 35.0
MULTI_DAY_SPREAD_GATE_SOFT_SCORE_CAP = 72.0
MULTI_AREA_ROUTE_LABEL = "multi-area-skeleton"
def is_food_place(place: CandidatePlace) -> bool:
    """Check if a place is food-related."""
    return is_food_partition_place(place)


class RouteProviderError(RuntimeError):
    """Raised when the optional precise route provider cannot return one leg."""


class RouteProviderRateLimitError(RouteProviderError):
    """Raised when the route provider is temporarily rate-limited."""

    def __init__(self, message: str, infocode: str = "") -> None:
        self.infocode = infocode
        super().__init__(message)


class _AmapKeyQuota(RouteProviderRateLimitError):
    """This key is out of quota. The route loop may retry another key."""


class _AmapKeyOverLimit(RouteProviderRateLimitError):
    """This key and mode are cooling down. The route loop may retry another key."""


class RouteProviderBudgetExceededError(RouteProviderRateLimitError):
    """Raised when main-path precise route enrichment exceeds its budget."""


class RoutePlanInvariantError(RuntimeError):
    """Raised with the deterministic day structure that failed Route invariants."""

    def __init__(
        self,
        violations: list[str],
        day_groups: list[RouteDayGroup],
    ) -> None:
        self.violations = tuple(violations)
        self.day_groups = tuple(day_groups)
        super().__init__(
            "route plan invariant violation: " + "; ".join(violations)
        )


class RouteFullDayConflictError(RoutePlanInvariantError):
    """A proved day-count contradiction between source-admitted required places."""

    def __init__(self, full_day_place_ids: list[int], independent_must_include_ids: list[int], days: int) -> None:
        self.full_day_place_ids = tuple(sorted(full_day_place_ids))
        self.independent_must_include_ids = tuple(sorted(independent_must_include_ids))
        self.available_days = days
        # Independent required visits need at least one further ordinary day.
        self.required_days = len(self.full_day_place_ids) + bool(self.independent_must_include_ids)
        self.place_ids = tuple(sorted(set(self.full_day_place_ids) | set(self.independent_must_include_ids)))
        super().__init__(["full_day_conflict"], [])


class RouteMustIncludeConflictError(RuntimeError):
    """Raised when a deterministically resolved must-go cannot be routed."""

    def __init__(self, place_ids: set[int] | list[int], reason: str) -> None:
        self.place_ids = tuple(sorted(place_ids))
        self.reason = reason
        super().__init__(
            "resolved must-go route conflict: "
            f"place_ids={list(self.place_ids)}, reason={reason}"
        )


class SelectedRoutePreciseConflictError(RuntimeError):
    """Precise evidence changed selected membership on a transactional trial."""

    def __init__(
        self,
        *,
        added_place_ids: set[int],
        removed_place_ids: set[int],
        original_day_place_ids: list[list[int]],
        trial_day_place_ids: list[list[int]],
        trial_plan: RoutePlan,
    ) -> None:
        self.added_place_ids = tuple(sorted(added_place_ids))
        self.removed_place_ids = tuple(sorted(removed_place_ids))
        self.original_day_place_ids = tuple(
            tuple(day) for day in original_day_place_ids
        )
        self.trial_day_place_ids = tuple(tuple(day) for day in trial_day_place_ids)
        self.trial_plan = trial_plan
        super().__init__(
            "precise route evidence conflicts with locked selected membership: "
            f"added_place_ids={list(self.added_place_ids)}, "
            f"removed_place_ids={list(self.removed_place_ids)}"
        )


class _MissingTransitCitycodeError(RouteProviderError):
    """Internal estimate fallback when transit routing lacks a citycode."""


@dataclass(frozen=True)
class PreciseRoute:
    distance_meters: int
    duration_minutes: int
    encoded_polyline: str = ""
    transit_steps: tuple[TransitStep, ...] = ()
    transit_detail_quality: TransitDetailQuality = "missing"
    transit_detail_cache_hit: bool = False
    fare_observation: SourceObservation | None = None


AMAP_ROUTE_CACHE_PAYLOAD_VERSION = 4
AMAP_V5_COST_CACHE_PROVIDER = "amap_v5_cost_v4"

# Internal walking-proof decisions for generation_mode=transit. Never public copy.
WALK_PROOF_ADOPTED = "adopted"
WALK_PROOF_OVER_DURATION = "over_duration"
WALK_PROOF_NO_PATH = "no_path"
WALK_PROOF_INVALID = "invalid_route"
WALK_PROOF_PROVIDER_ERROR = "provider_error"
WALK_PROOF_TIMEOUT = "timeout"
WALK_PROOF_BUDGET = "budget_exhausted"
WALK_PROOF_RATE_LIMITED = "rate_limited"
WALK_PROOF_CANCELLED = "cancelled"
# Frozen U4 L8 proof cap. Native walking-mode policy keeps route_single_leg_max_walking.
WALK_PROOF_MAX_DURATION_MINUTES = 28
# Sidecar key prefix inside known_precise_legs. Values are reason strings, never
# CommuteLeg records. Distinct from (from_id, to_id, mode) success facts.
WALK_PROOF_REJECT_SENTINEL = "__walk_proof_reject__"


TRANSIT_DETAIL_GENERIC_TEMPLATE = (
    "公共交通预计 {duration_minutes} 分钟，具体换乘以实时导航为准。"
)


def _nullable_text(value: Any) -> str | None:
    if value is None or not isinstance(value, str):
        return None
    text_value = value.strip()
    return text_value or None


def _nonnegative_number(value: Any) -> int | None:
    """Parse one provider numeric field without accepting prose or negatives."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        if not math.isfinite(value) or value < 0:
            return None
        return int(value)
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw or re.fullmatch(r"\d+(?:\.\d+)?", raw) is None:
        return None
    parsed = float(raw)
    if not math.isfinite(parsed) or parsed < 0:
        return None
    return int(parsed)


def _duration_minutes(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        seconds = float(value)
    elif isinstance(value, str) and re.fullmatch(
        r"\d+(?:\.\d+)?", value.strip()
    ):
        seconds = float(value.strip())
    else:
        return None
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return math.ceil(seconds / 60)


def _stop_name(value: Any) -> str | None:
    if isinstance(value, dict):
        return _nullable_text(value.get("name"))
    return _nullable_text(value)


def _stop_count(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        via_num = value
    elif isinstance(value, float) and value.is_integer():
        via_num = int(value)
    elif isinstance(value, str) and re.fullmatch(r"\d+", value.strip()):
        via_num = int(value.strip())
    else:
        return None
    return via_num + 1 if via_num >= 0 else None


def _ride_kind(provider_type: str | None) -> str | None:
    normalized = (provider_type or "").strip().lower()
    if any(marker in normalized for marker in ("出租", "taxi")):
        return None
    if any(
        marker in normalized
        for marker in (
            "地铁",
            "轨道",
            "轻轨",
            "城轨",
            "有轨电车",
            "metro",
            "subway",
            "light rail",
            "city rail",
            "tram",
        )
    ):
        return "rail"
    if any(
        marker in normalized
        for marker in ("公交", "公共汽车", "巴士", "bus", "brt")
    ):
        return "bus"
    return "other"


def _ride_step(payload: Any) -> tuple[TransitStep | None, bool]:
    if not isinstance(payload, dict):
        return None, bool(payload)
    provider_type = _nullable_text(payload.get("type"))
    kind = _ride_kind(provider_type)
    if kind is None:
        return None, True
    cost = payload.get("cost") if isinstance(payload.get("cost"), dict) else {}
    return TransitStep(
        kind=kind,
        duration_minutes=_duration_minutes(cost.get("duration")),
        distance_meters=_nonnegative_number(payload.get("distance")),
        line_name=_nullable_text(payload.get("name")),
        provider_type=provider_type,
        from_stop=_stop_name(payload.get("departure_stop")),
        to_stop=_stop_name(payload.get("arrival_stop")),
        stop_count=_stop_count(payload.get("via_num")),
    ), False


_TRANSIT_SEGMENT_KEYS = {
    "walking",
    "bus",
    "railway",
    "taxi",
    "entrance",
    "exit",
}


def _select_busline_candidate(buslines: Any) -> Any | None:
    """Pick the same busline candidate used by public transit_steps."""
    if not isinstance(buslines, list):
        return None
    first_partial: Any | None = None
    for busline in buslines:
        step, _ = _ride_step(busline)
        if step is None:
            continue
        if step.line_name and step.from_stop and step.to_stop:
            return busline
        if first_partial is None:
            first_partial = busline
    return first_partial


def normalize_transit_detail(
    transit: Any,
) -> tuple[tuple[TransitStep, ...], bool]:
    """Normalize provider-ordered segments and report unsupported containers."""
    if not isinstance(transit, dict):
        return (), bool(transit)
    segments = transit.get("segments") or []
    if not isinstance(segments, list):
        return (), True
    normalized: list[TransitStep] = []
    unsupported = False
    for segment in segments:
        if not isinstance(segment, dict):
            unsupported = unsupported or bool(segment)
            continue

        walking = segment.get("walking")
        if isinstance(walking, dict) and walking:
            cost = (
                walking.get("cost")
                if isinstance(walking.get("cost"), dict)
                else {}
            )
            normalized.append(TransitStep(
                kind="walking",
                duration_minutes=_duration_minutes(cost.get("duration")),
                distance_meters=_nonnegative_number(walking.get("distance")),
            ))
        elif walking:
            unsupported = True

        bus = segment.get("bus")
        if isinstance(bus, dict):
            buslines = bus.get("buslines") or []
            if isinstance(buslines, list):
                selected_busline = _select_busline_candidate(buslines)
                selected_step: TransitStep | None = None
                if selected_busline is not None:
                    selected_step, _ = _ride_step(selected_busline)
                if selected_step is not None:
                    normalized.append(selected_step)
                elif buslines:
                    unsupported = True
            elif buslines:
                unsupported = True
        elif bus:
            unsupported = True

        railway = segment.get("railway")
        if railway:
            railway_entries = railway if isinstance(railway, list) else [railway]
            for entry in railway_entries:
                step, rejected = _ride_step(entry)
                if (
                    step is None
                    or not step.line_name
                    or not step.from_stop
                    or not step.to_stop
                ):
                    unsupported = True
                    continue
                step.kind = "rail"
                normalized.append(step)
                unsupported = unsupported or rejected

        if segment.get("taxi"):
            unsupported = True
        for key, value in segment.items():
            if key not in _TRANSIT_SEGMENT_KEYS and isinstance(value, (dict, list)) and value:
                unsupported = True
    return tuple(normalized), unsupported


def classify_transit_detail(
    steps: list[TransitStep] | tuple[TransitStep, ...],
    *,
    unsupported: bool = False,
) -> TransitDetailQuality:
    if not steps:
        return "missing"
    if unsupported:
        return "partial"
    rides = [step for step in steps if step.kind != "walking"]
    if not rides:
        return "partial"
    if any(
        not step.line_name or not step.from_stop or not step.to_stop
        for step in rides
    ):
        return "partial"
    if any(
        step.duration_minutes is None and step.distance_meters is None
        for step in steps
        if step.kind == "walking"
    ):
        return "partial"
    return "complete"


def format_transit_summary(
    duration_minutes: int,
    steps: list[TransitStep] | tuple[TransitStep, ...],
    *,
    detail_quality: TransitDetailQuality | None = None,
) -> str:
    quality = detail_quality or classify_transit_detail(steps)
    if quality != "complete":
        return TRANSIT_DETAIL_GENERIC_TEMPLATE.format(
            duration_minutes=duration_minutes
        )
    phrases: list[str] = []
    ride_index = 0
    for step in steps:
        if step.kind == "walking":
            if step.duration_minutes is not None:
                phrases.append(f"步行约 {step.duration_minutes} 分钟")
            elif step.distance_meters is not None:
                phrases.append(f"步行约 {step.distance_meters} 米")
            continue
        ride_index += 1
        prefix = "乘" if ride_index == 1 else "换乘"
        stop_text = f"（{step.from_stop}上车，{step.to_stop}下车"
        if step.stop_count is not None:
            stop_text += f"，{step.stop_count}站"
        phrases.append(f"{prefix}{step.line_name}{stop_text}）")
    return f"公共交通预计 {duration_minutes} 分钟：" + " → ".join(phrases)


@dataclass
class RoutePlanningMetrics:
    route_policy_version: str = "legacy"
    route_v2_trial_count: int = 0
    route_v2_repair_rounds: int = 0
    accommodation_metrics: dict[str, Any] = field(default_factory=dict)
    route_v2_accepted_action: str = ""
    route_v2_stop_reason: str = ""
    route_v2_local_repair_evaluations: int = 0
    route_v2_repair_batches: int = 0
    route_v2_coverage_report: list = field(default_factory=list)
    route_v2_repair_trace: list = field(default_factory=list)
    route_v2_proved_conflicts: dict = field(default_factory=dict)
    access_route_attempt_count: int = 0
    access_route_fact_count: int = 0
    walk_proof_receipts: list[dict[str, Any]] = field(default_factory=list)
    plan_count: int = 0
    day_count: int = 0
    # Final-plan snapshot, recomputed from returned plans at the workflow exit.
    commute_leg_count: int = 0
    amap_cache_hit_count: int = 0
    amap_cache_write_count: int = 0
    amap_cache_redis_hit_count: int = 0
    amap_cache_redis_write_count: int = 0
    amap_cache_postgres_hit_count: int = 0
    amap_cache_postgres_write_count: int = 0
    amap_cache_error_count: int = 0
    amap_cache_invalid_payload_count: int = 0
    amap_cache_coordinate_mismatch_count: int = 0
    amap_cache_redis_configured: int = 0
    amap_cache_redis_available: int = 0
    amap_cache_ttl_seconds: int = 0
    amap_cache_key_version: int = 2
    amap_call_count: int = 0
    amap_success_count: int = 0
    amap_fallback_count: int = 0
    amap_rate_limit_count: int = 0
    amap_wait_ms: int = 0
    amap_budget_call_hard_cap: int = 0
    amap_budget_call_limit: int = 0
    amap_budget_unique_uncached_allocated_leg_count: int = 0
    amap_budget_time_limit_ms: int = 0
    amap_budget_time_effective_limit_ms: int = 0
    amap_budget_call_exceeded_count: int = 0
    amap_budget_time_exceeded_count: int = 0
    amap_budget_exceeded_count: int = 0
    amap_precise_candidate_plan_count: int = 0
    amap_precise_target_plan_count: int = 0
    amap_precise_processed_plan_count: int = 0
    amap_precise_selected_complete_plan_count: int = 0
    amap_precise_skipped_plan_count: int = 0
    amap_provider_error_fallback_count: int = 0
    amap_provider_rate_limit_fallback_count: int = 0
    amap_provider_budget_exhausted_count: int = 0
    amap_precise_recovery_attempt_count: int = 0
    amap_precise_recovery_success_count: int = 0
    amap_precise_recovery_failed_count: int = 0
    amap_precise_recovery_estimate_day_count: int = 0
    amap_precise_membership_conflict_count: int = 0
    amap_precise_membership_conflict_removed_selected_count: int = 0
    amap_precise_membership_conflict_structure_gap_count: int = 0
    amap_precise_membership_soft_accept_count: int = 0
    amap_precise_recovery_reason_counts: dict[str, int] = field(
        default_factory=lambda: {
            "initial_incomplete": 0,
            "precise_budget_rejection": 0,
            "provider_budget_exhausted": 0,
            "recovery_failed": 0,
        }
    )
    amap_missing_citycode_count: int = 0
    accommodation_anchor_applied_day_count: int = 0
    accommodation_anchor_fallback_day_count: int = 0
    by_effective_mode: dict[str, dict[str, int]] = field(
        default_factory=lambda: {
            mode: {
                "leg_count": 0,
                "amap_success_count": 0,
                "estimate_fallback_count": 0,
                "rationalized_count": 0,
            }
            for mode in ("driving", "transit", "walking", "cycling")
        }
    )
    route_quality: dict[str, Any] = field(default_factory=dict)
    selected_used: int = 0
    selected_dropped: int = 0
    qualified_supplemented: int = 0
    selected_drop_reason_counts: dict[str, int] = field(default_factory=dict)
    supplement_reason_counts: dict[str, int] = field(default_factory=dict)

    def record_effective_leg(
        self,
        mode: EffectiveCommuteMode,
        *,
        rationalized: bool,
    ) -> None:
        # Attempt telemetry intentionally includes discarded transactional
        # precise trials; it measures routing work/cost, not final membership.
        self.by_effective_mode[mode]["leg_count"] += 1
        if rationalized:
            self.by_effective_mode[mode]["rationalized_count"] += 1

    def record_effective_success(self, mode: EffectiveCommuteMode) -> None:
        self.by_effective_mode[mode]["amap_success_count"] += 1

    def record_effective_fallback(self, mode: EffectiveCommuteMode) -> None:
        self.by_effective_mode[mode]["estimate_fallback_count"] += 1

    def record_walk_proof(
        self,
        decision: str,
        *,
        from_place_id: int | None = None,
        to_place_id: int | None = None,
        direction: str | None = None,
        place_id: int | None = None,
    ) -> None:
        if not decision:
            return
        row: dict[str, Any] = {"decision": str(decision)}
        if from_place_id is not None:
            row["from_place_id"] = int(from_place_id)
        if to_place_id is not None:
            row["to_place_id"] = int(to_place_id)
        if direction is not None:
            row["direction"] = str(direction)
        if place_id is not None:
            row["place_id"] = int(place_id)
        self.walk_proof_receipts.append(row)

    def record_precise_recovery_reason(self, reason: str, count: int = 1) -> None:
        if reason not in self.amap_precise_recovery_reason_counts:
            return
        self.amap_precise_recovery_reason_counts[reason] += max(0, int(count))

    def record_membership_ledger(self, ledger: RouteMembershipLedger) -> None:
        self.selected_used = sum(item.status == "USED" for item in ledger.selected)
        self.selected_dropped = sum(
            item.status == "DROPPED" for item in ledger.selected
        )
        self.qualified_supplemented = len(ledger.supplemented)
        self.selected_drop_reason_counts = dict(
            sorted({
                reason: sum(item.reason == reason for item in ledger.selected)
                for reason in (
                    "HARD_INELIGIBLE",
                    "TEMPORALLY_UNSCHEDULABLE",
                    "CAPACITY_LIMIT",
                    "ROUTE_FEASIBILITY_LIMIT",
                    "NOT_CHOSEN_FOR_FINAL_ROUTE",
                )
                if any(item.reason == reason for item in ledger.selected)
            }.items())
        )
        self.supplement_reason_counts = dict(
            sorted({
                reason: sum(item.reason == reason for item in ledger.supplemented)
                for reason in ("REPLACE_DROPPED", "FILL_EMPTY_DAY", "FILL_MUST_INCLUDE_DAY", "FILL_MEAL_SLOT")
                if any(item.reason == reason for item in ledger.supplemented)
            }.items())
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "route_policy_version": self.route_policy_version,
            "route_v2_trial_count": self.route_v2_trial_count,
            "route_v2_repair_rounds": self.route_v2_repair_rounds,
            "accommodation": dict(self.accommodation_metrics),
            "route_v2_accepted_action": self.route_v2_accepted_action,
            "route_v2_stop_reason": self.route_v2_stop_reason,
            "route_v2_local_repair_evaluations": self.route_v2_local_repair_evaluations,
            "route_v2_repair_batches": self.route_v2_repair_batches,
            "route_v2_coverage_report": list(self.route_v2_coverage_report),
            "route_v2_repair_trace": list(self.route_v2_repair_trace),
            "route_v2_proved_conflicts": {
                str(place_id): str(reason)
                for place_id, reason in self.route_v2_proved_conflicts.items()
            },
            "access_route_attempt_count": self.access_route_attempt_count,
            "access_route_fact_count": self.access_route_fact_count,
            "walk_proof_receipts": [dict(row) for row in self.walk_proof_receipts],
            "plan_count": self.plan_count,
            "selected_used": self.selected_used,
            "selected_dropped": self.selected_dropped,
            "qualified_supplemented": self.qualified_supplemented,
            "selected_drop_reason_counts": dict(self.selected_drop_reason_counts),
            "supplement_reason_counts": dict(self.supplement_reason_counts),
            "day_count": self.day_count,
            "commute_leg_count": self.commute_leg_count,
            "amap_cache_hit_count": self.amap_cache_hit_count,
            "amap_cache_write_count": self.amap_cache_write_count,
            "amap_cache_redis_hit_count": self.amap_cache_redis_hit_count,
            "amap_cache_redis_write_count": self.amap_cache_redis_write_count,
            "amap_cache_postgres_hit_count": self.amap_cache_postgres_hit_count,
            "amap_cache_postgres_write_count": self.amap_cache_postgres_write_count,
            "amap_cache_error_count": self.amap_cache_error_count,
            "amap_cache_invalid_payload_count": self.amap_cache_invalid_payload_count,
            "amap_cache_coordinate_mismatch_count": (
                self.amap_cache_coordinate_mismatch_count
            ),
            "amap_cache_redis_configured": self.amap_cache_redis_configured,
            "amap_cache_redis_available": self.amap_cache_redis_available,
            "amap_cache_ttl_seconds": self.amap_cache_ttl_seconds,
            "amap_cache_key_version": self.amap_cache_key_version,
            "amap_call_count": self.amap_call_count,
            "amap_success_count": self.amap_success_count,
            "amap_fallback_count": self.amap_fallback_count,
            "amap_rate_limit_count": self.amap_rate_limit_count,
            "amap_wait_ms": self.amap_wait_ms,
            "amap_budget_call_hard_cap": self.amap_budget_call_hard_cap,
            "amap_budget_call_limit": self.amap_budget_call_limit,
            "amap_budget_unique_uncached_allocated_leg_count": (
                self.amap_budget_unique_uncached_allocated_leg_count
            ),
            "amap_budget_time_limit_ms": self.amap_budget_time_limit_ms,
            "amap_budget_time_effective_limit_ms": (
                self.amap_budget_time_effective_limit_ms
            ),
            "amap_budget_call_exceeded_count": self.amap_budget_call_exceeded_count,
            "amap_budget_time_exceeded_count": self.amap_budget_time_exceeded_count,
            "amap_budget_exceeded_count": self.amap_budget_exceeded_count,
            "amap_precise_candidate_plan_count": self.amap_precise_candidate_plan_count,
            "amap_precise_target_plan_count": self.amap_precise_target_plan_count,
            "amap_precise_processed_plan_count": self.amap_precise_processed_plan_count,
            "amap_precise_selected_complete_plan_count": (
                self.amap_precise_selected_complete_plan_count
            ),
            "amap_precise_skipped_plan_count": self.amap_precise_skipped_plan_count,
            "amap_provider_error_fallback_count": (
                self.amap_provider_error_fallback_count
            ),
            "amap_provider_rate_limit_fallback_count": (
                self.amap_provider_rate_limit_fallback_count
            ),
            "amap_provider_budget_exhausted_count": (
                self.amap_provider_budget_exhausted_count
            ),
            "amap_precise_recovery_attempt_count": (
                self.amap_precise_recovery_attempt_count
            ),
            "amap_precise_recovery_success_count": (
                self.amap_precise_recovery_success_count
            ),
            "amap_precise_recovery_failed_count": (
                self.amap_precise_recovery_failed_count
            ),
            "amap_precise_recovery_estimate_day_count": (
                self.amap_precise_recovery_estimate_day_count
            ),
            "amap_precise_membership_conflict_count": (
                self.amap_precise_membership_conflict_count
            ),
            "amap_precise_membership_conflict_removed_selected_count": (
                self.amap_precise_membership_conflict_removed_selected_count
            ),
            "amap_precise_membership_conflict_structure_gap_count": (
                self.amap_precise_membership_conflict_structure_gap_count
            ),
            "amap_precise_membership_soft_accept_count": (
                self.amap_precise_membership_soft_accept_count
            ),
            "amap_precise_recovery_reason_counts": dict(
                self.amap_precise_recovery_reason_counts
            ),
            "amap_missing_citycode_count": self.amap_missing_citycode_count,
            "accommodation_anchor_applied_day_count": (
                self.accommodation_anchor_applied_day_count
            ),
            "accommodation_anchor_fallback_day_count": (
                self.accommodation_anchor_fallback_day_count
            ),
            "by_effective_mode": {
                mode: dict(values)
                for mode, values in self.by_effective_mode.items()
            },
        }

    def route_quality_metrics(self) -> dict[str, Any]:
        return dict(self.route_quality or {})

    def route_quality_stage_metadata(self) -> dict[str, Any]:
        route_quality = self.route_quality_metrics()
        candidates = route_quality.get("route_quality_candidates")
        if not isinstance(candidates, list):
            candidates = []
        selected = next(
            (
                candidate for candidate in candidates
                if candidate.get("label") == route_quality.get("selected_label")
            ),
            {},
        )
        return {
            "route_quality_candidate_count": len(candidates),
            "route_quality_selected_label": route_quality.get("selected_label", ""),
            "route_quality_previous_selected_label": route_quality.get(
                "previous_selected_label",
                "",
            ),
            "route_quality_selected_reason": route_quality.get(
                "selected_reason",
                "",
            ),
            "route_quality_selected_must_include_scheduled": selected.get(
                "must_include_scheduled",
                0,
            ),
            "route_quality_selected_must_include_total": selected.get(
                "must_include_total",
                0,
            ),
            "route_quality_selected_must_include_coverage_ratio": selected.get(
                "must_include_coverage_ratio",
                0.0,
            ),
            "route_quality_selected_requested_preference_dimensions": selected.get(
                "requested_place_type_preference_dimensions",
                [],
            ),
            "route_quality_selected_available_preference_dimensions": selected.get(
                "available_place_type_preference_dimensions",
                [],
            ),
            "route_quality_selected_matched_preference_dimensions": selected.get(
                "matched_place_type_preference_dimensions",
                [],
            ),
            "route_quality_selected_missing_preference_dimensions": selected.get(
                "missing_place_type_preference_dimensions",
                [],
            ),
            "route_quality_selected_preference_coverage_ratio": selected.get(
                "place_type_preference_coverage_ratio",
                0.0,
            ),
            "route_quality_selected_route_shape_class": selected.get(
                "route_shape_class",
                "",
            ),
            "route_quality_selected_multi_day_spread_score": selected.get(
                "multi_day_spread_score",
                0.0,
            ),
            "route_quality_selected_day_center_distance_km_min": selected.get(
                "day_center_distance_km_min",
                0.0,
            ),
            "route_quality_selected_day_center_distance_km_median": selected.get(
                "day_center_distance_km_median",
                0.0,
            ),
            "route_quality_selected_spatial_spread_gate": selected.get(
                "spatial_spread_gate",
                "",
            ),
            "route_quality_selected_anchor_forced_cluster": selected.get(
                "anchor_forced_cluster",
                False,
            ),
        }


class RouteProvider(Protocol):
    async def route(
        self,
        *,
        origin: CandidatePlace,
        destination: CandidatePlace,
        city: str,
        effective_mode: EffectiveCommuteMode,
        citycode: str | None,
    ) -> PreciseRoute:
        ...

    async def close(self) -> None:
        ...


class AmapRouteProvider:
    """One governed Amap router for the legacy v3 and mode-aware v5 APIs."""

    def __init__(
        self,
        *,
        mode_aware: bool | None = None,
        metrics: RoutePlanningMetrics | None = None,
        deadline_monotonic: float | None = None,
        wall_start_monotonic: float | None = None,
        time_budget_ms: int | None = None,
    ) -> None:
        settings = get_settings()
        self.api_generation = (
            "v5"
            if (
                settings.commute_mode_enabled
                if mode_aware is None
                else mode_aware
            )
            else "v3"
        )
        self.cache_provider = (
            AMAP_V5_COST_CACHE_PROVIDER
            if self.api_generation == "v5"
            else "amap"
        )
        self._configured_keys = configured_amap_keys(settings)
        self.api_key = self._configured_keys[0] if self._configured_keys else ""
        self._amap_keys = get_amap_key_coordinator()
        self.timeout = settings.amap_route_timeout
        self.min_interval = 1 / max(settings.amap_route_qps, 0.1)
        self.backoff_seconds = max(0.0, settings.amap_route_backoff_seconds)
        self.cache_ttl_seconds = max(1, settings.amap_route_cache_ttl_hours) * 3600
        self.hard_call_cap = max(0, int(settings.amap_route_call_budget or 0))
        self.call_budget = self.hard_call_cap
        # None keeps the shared backup budget. A positive override is a
        # separate wall clock for one provider instance.
        if time_budget_ms is None:
            self.time_budget_ms = max(0, int(settings.amap_route_time_budget_ms or 0))
        else:
            self.time_budget_ms = max(0, int(time_budget_ms))
        wall_start = (
            float(wall_start_monotonic)
            if wall_start_monotonic is not None
            else time.monotonic()
        )
        self._wall_start_monotonic = wall_start
        configured_deadline = (
            wall_start + (self.time_budget_ms / 1000.0)
            if self.time_budget_ms > 0
            else None
        )
        if deadline_monotonic is not None:
            self._deadline_monotonic = float(deadline_monotonic)
            if configured_deadline is not None:
                self._deadline_monotonic = min(
                    self._deadline_monotonic,
                    configured_deadline,
                )
        elif configured_deadline is not None:
            self._deadline_monotonic = configured_deadline
        else:
            self._deadline_monotonic = None
        effective_time_ms = 0
        if self._deadline_monotonic is not None:
            effective_time_ms = max(
                0,
                round((self._deadline_monotonic - wall_start) * 1000),
            )
        self.redis_url = settings.redis_url.strip()
        self._disabled_until = 0.0
        self._disabled_reason = ""
        self._budget_lock = asyncio.Lock()
        self._client = httpx.AsyncClient()
        self._metrics = metrics
        self._redis_timeout = REDIS_ROUTE_CACHE_TIMEOUT_SECONDS
        self._cache_available = True
        self._redis_available = bool(self.redis_url)
        self._redis_client = None
        self._memory_cache: dict[str, PreciseRoute] = {}
        self._memory_cache_expires_at: dict[str, float] = {}
        self._call_count = 0
        self._allocation_started = False
        self._allocated_call_limit = self.hard_call_cap
        self._allocated_uncached_keys: set[str] = set()
        if self._metrics is not None:
            self._metrics.amap_cache_redis_configured = int(bool(self.redis_url))
            self._metrics.amap_cache_redis_available = int(self._redis_available)
            self._metrics.amap_cache_ttl_seconds = self.cache_ttl_seconds
            self._metrics.amap_budget_call_hard_cap = self.hard_call_cap
            self._metrics.amap_budget_call_limit = self._allocated_call_limit
            self._metrics.amap_budget_unique_uncached_allocated_leg_count = 0
            self._metrics.amap_budget_time_limit_ms = self.time_budget_ms
            self._metrics.amap_budget_time_effective_limit_ms = effective_time_ms

    @classmethod
    def _cache_key(
        cls,
        origin_place_id: int,
        destination_place_id: int,
        *,
        api_generation: str,
        city: str,
        mode: EffectiveCommuteMode,
        origin_longitude: float | None = None,
        origin_latitude: float | None = None,
        destination_longitude: float | None = None,
        destination_latitude: float | None = None,
    ) -> str:
        # Every Amap key shares this cache. Do not put the key or its fingerprint here.
        namespace = "cost_v4" if api_generation == "v5" else "v3"
        return (
            f"amap:route:{namespace}:"
            f"{cls._cache_part(api_generation)}:"
            f"{cls._cache_part(city)}:"
            f"{cls._cache_part(mode)}:"
            f"{cls._endpoint_cache_part(origin_place_id, origin_longitude, origin_latitude)}:"
            f"{cls._endpoint_cache_part(destination_place_id, destination_longitude, destination_latitude)}"
        )

    def _cache_provider_for_mode(self, mode: EffectiveCommuteMode) -> str:
        return self.cache_provider

    def _remember_cache(self, cache_key: str, route: PreciseRoute) -> None:
        self._memory_cache[cache_key] = route
        self._memory_cache_expires_at[cache_key] = (
            time.monotonic() + self.cache_ttl_seconds
        )

    @staticmethod
    def _cache_part(value: str) -> str:
        safe = (value or "").strip().lower()
        return safe.replace(":", "_") or "-"

    @classmethod
    def _endpoint_cache_part(
        cls,
        place_id: int,
        longitude: float | None,
        latitude: float | None,
    ) -> str:
        coord = cls._coord_fingerprint(longitude, latitude)
        if place_id:
            return f"p{int(place_id)}@{coord}"
        return f"coord@{coord}"

    @staticmethod
    def _coord_fingerprint(longitude: float | None, latitude: float | None) -> str:
        if longitude is None or latitude is None:
            return "no_coord"
        return f"{float(longitude):.6f},{float(latitude):.6f}"

    @classmethod
    def _route_cache_key(
        cls,
        *,
        origin: CandidatePlace,
        destination: CandidatePlace,
        city: str,
        api_generation: str,
        mode: EffectiveCommuteMode,
    ) -> str:
        key = cls._cache_key(
            origin.place_id,
            destination.place_id,
            api_generation=api_generation,
            city=city,
            mode=mode,
            origin_longitude=origin.longitude,
            origin_latitude=origin.latitude,
            destination_longitude=destination.longitude,
            destination_latitude=destination.latitude,
        )
        # Coordinate-only lodging endpoints never share Canonical POI cache identity.
        return "access:v1:" + key if origin.place_id == 0 or destination.place_id == 0 else key

    @staticmethod
    def _coordinates_match(
        *,
        cached_lng: float | None,
        cached_lat: float | None,
        current_lng: float | None,
        current_lat: float | None,
    ) -> bool:
        if None in (cached_lng, cached_lat, current_lng, current_lat):
            return False
        return (
            abs(float(cached_lng) - float(current_lng)) <= 0.00001
            and abs(float(cached_lat) - float(current_lat)) <= 0.00001
        )

    async def _get_redis_client(self):
        if not self.redis_url or not self._redis_available:
            return None
        if self._redis_client is not None:
            return self._redis_client
        try:
            from redis.asyncio import Redis

            self._redis_client = Redis.from_url(
                self.redis_url,
                encoding="utf-8",
                decode_responses=True,
                socket_connect_timeout=self._redis_timeout,
                socket_timeout=self._redis_timeout,
                retry_on_timeout=False,
                health_check_interval=30,
            )
        except Exception:
            self._redis_available = False
            if self._metrics is not None:
                self._metrics.amap_cache_error_count += 1
                self._metrics.amap_cache_redis_available = 0
            logger.info("Redis route cache client init failed; using Postgres cache")
            return None
        return self._redis_client

    @staticmethod
    def _route_from_cache_payload(
        raw: str | None,
        *,
        api_generation: str = "v3",
        mode: EffectiveCommuteMode = "driving",
    ) -> PreciseRoute | None:
        if not raw:
            return None
        try:
            payload = json.loads(raw)
            route = PreciseRoute(
                distance_meters=int(payload["distance_meters"]),
                duration_minutes=int(payload["duration_minutes"]),
                encoded_polyline=str(payload.get("encoded_polyline") or ""),
            )
            if api_generation == "v5":
                if (
                    payload.get("provider") != AMAP_V5_COST_CACHE_PROVIDER
                    or payload.get("api_generation") != "v5"
                    or payload.get("mode") != mode
                    or payload.get("payload_version") != AMAP_ROUTE_CACHE_PAYLOAD_VERSION
                ):
                    return None
                route_fact = payload.get("route_fact")
                if (
                    not isinstance(route_fact, dict)
                    or route_fact.get("version") != AMAP_ROUTE_CACHE_PAYLOAD_VERSION
                ):
                    return None
                fare_observation = None
                raw_fare = route_fact.get("fare_observation")
                if isinstance(raw_fare, dict):
                    try:
                        fare_observation = SourceObservation.model_validate(raw_fare)
                    except ValidationError:
                        # A malformed fare never invalidates cached route facts.
                        fare_observation = None
                steps: tuple[TransitStep, ...] = ()
                quality: TransitDetailQuality = "missing"
                if mode == "transit":
                    detail = route_fact.get("transit_detail")
                    if not isinstance(detail, dict) or detail.get("version") != 2:
                        return None
                    raw_steps = detail.get("steps")
                    quality = detail.get("quality")
                    if not isinstance(raw_steps, list) or quality not in {
                        "complete", "partial", "missing"
                    }:
                        return None
                    steps = tuple(TransitStep.model_validate(step) for step in raw_steps)
                    if quality == "complete" and classify_transit_detail(steps) != "complete":
                        return None
                    if quality == "missing" and steps:
                        return None
                    if quality == "partial" and not steps:
                        return None
                route = PreciseRoute(
                    distance_meters=route.distance_meters,
                    duration_minutes=route.duration_minutes,
                    encoded_polyline=str(route_fact.get("encoded_polyline") or ""),
                    transit_steps=steps,
                    transit_detail_quality=quality,
                    transit_detail_cache_hit=mode == "transit",
                    fare_observation=fare_observation,
                )
            return route
        except (KeyError, TypeError, ValueError, json.JSONDecodeError, ValidationError):
            return None

    @staticmethod
    def _v5_route_fact(
        route: PreciseRoute,
        *,
        mode: EffectiveCommuteMode,
    ) -> dict[str, Any]:
        fact: dict[str, Any] = {
            "version": AMAP_ROUTE_CACHE_PAYLOAD_VERSION,
            "encoded_polyline": route.encoded_polyline or "",
            "fare_observation": (
                route.fare_observation.model_dump(mode="json")
                if route.fare_observation is not None
                else None
            ),
        }
        if mode == "transit":
            fact["transit_detail"] = {
                "version": 2,
                "steps": [
                    step.model_dump(mode="json")
                    for step in route.transit_steps
                ],
                "quality": route.transit_detail_quality,
            }
        return fact

    @staticmethod
    def _route_to_cache_payload(
        route: PreciseRoute,
        *,
        provider: str,
        api_generation: str,
        mode: EffectiveCommuteMode,
    ) -> str:
        payload: dict[str, Any] = {
            "distance_meters": route.distance_meters,
            "duration_minutes": route.duration_minutes,
            "encoded_polyline": route.encoded_polyline,
            "provider": provider,
            "api_generation": api_generation,
            "mode": mode,
            "strategy": 0,
            "fetched_at": int(time.time()),
        }
        if api_generation == "v5":
            payload["payload_version"] = AMAP_ROUTE_CACHE_PAYLOAD_VERSION
            payload["route_fact"] = AmapRouteProvider._v5_route_fact(
                route,
                mode=mode,
            )
        return json.dumps(payload, separators=(",", ":"))

    @staticmethod
    def _normalized_polyline_points(raw: Any) -> list[str]:
        if isinstance(raw, dict):
            raw = raw.get("polyline")
        if not isinstance(raw, str):
            return []
        points: list[str] = []
        for token in raw.split(";"):
            pair = token.strip()
            if not pair:
                continue
            parts = pair.split(",")
            if len(parts) != 2:
                continue
            lng_text = parts[0].strip()
            lat_text = parts[1].strip()
            if not lng_text or not lat_text:
                continue
            try:
                longitude = float(lng_text)
                latitude = float(lat_text)
            except ValueError:
                continue
            if not math.isfinite(longitude) or not math.isfinite(latitude):
                continue
            points.append(f"{lng_text},{lat_text}")
        return points

    @staticmethod
    def _step_polyline_points(path: Any) -> list[str]:
        steps = path.get("steps") if isinstance(path, dict) else None
        if not isinstance(steps, list):
            return []
        points: list[str] = []
        for step in steps:
            if not isinstance(step, dict):
                continue
            points.extend(
                AmapRouteProvider._normalized_polyline_points(step.get("polyline"))
            )
        return points

    @staticmethod
    def _flatten_step_polyline(path: dict) -> str:
        points = AmapRouteProvider._step_polyline_points(path)
        if len(points) < 2:
            return ""
        return ";".join(points)

    @staticmethod
    def _flatten_transit_polyline(transit: Any) -> str:
        if not isinstance(transit, dict):
            return ""
        segments = transit.get("segments")
        if not isinstance(segments, list):
            return ""
        points: list[str] = []
        for segment in segments:
            if not isinstance(segment, dict):
                return ""
            for key, value in segment.items():
                if (
                    key not in _TRANSIT_SEGMENT_KEYS
                    and isinstance(value, (dict, list))
                    and value
                ):
                    return ""
            if segment.get("railway") or segment.get("taxi"):
                return ""
            walking = segment.get("walking")
            if isinstance(walking, dict) and walking:
                walking_points = AmapRouteProvider._step_polyline_points(walking)
                if len(walking_points) < 2:
                    return ""
                points.extend(walking_points)
            elif walking:
                return ""
            bus = segment.get("bus")
            if isinstance(bus, dict):
                buslines = bus.get("buslines") or []
                if not isinstance(buslines, list):
                    return ""
                if buslines:
                    selected = _select_busline_candidate(buslines)
                    if not isinstance(selected, dict):
                        return ""
                    bus_points = AmapRouteProvider._normalized_polyline_points(
                        selected.get("polyline")
                    )
                    if len(bus_points) < 2:
                        return ""
                    points.extend(bus_points)
                elif bus:
                    return ""
            elif bus:
                return ""
        if len(points) < 2:
            return ""
        return ";".join(points)

    async def preload_cache_for_plans(
        self,
        plans: list[RoutePlan],
        *,
        city: str,
        generation_mode: EffectiveCommuteMode,
    ) -> None:
        if not self._cache_available:
            return
        route_pairs: dict[str, tuple[CandidatePlace, CandidatePlace, EffectiveCommuteMode]] = {}
        for plan in plans:
            for day_group in plan.day_groups:
                for index in range(max(0, len(day_group.places) - 1)):
                    origin = day_group.places[index]
                    destination = day_group.places[index + 1]
                    for mode in _provider_query_modes(
                        origin,
                        destination,
                        generation_mode=generation_mode,
                    ):
                        route_pairs[self._route_cache_key(
                            origin=origin,
                            destination=destination,
                            city=city,
                            api_generation=self.api_generation,
                            mode=mode,
                        )] = (origin, destination, mode)
        if not route_pairs:
            return
        redis_misses = dict(route_pairs)
        redis_client = await self._get_redis_client()
        if redis_client is not None:
            try:
                keys = list(route_pairs)
                values = await asyncio.wait_for(
                    redis_client.mget(keys),
                    timeout=self._redis_timeout,
                )
                redis_misses = {}
                for cache_key, raw in zip(keys, values):
                    origin, destination, mode = route_pairs[cache_key]
                    route = self._route_from_cache_payload(
                        raw,
                        api_generation=self.api_generation,
                        mode=mode,
                    )
                    if route is None:
                        if raw:
                            if self._metrics is not None:
                                self._metrics.amap_cache_invalid_payload_count += 1
                        redis_misses[cache_key] = (origin, destination, mode)
                        continue
                    self._remember_cache(cache_key, route)
                redis_hit_count = len(route_pairs) - len(redis_misses)
                if self._metrics is not None and redis_hit_count:
                    self._metrics.amap_cache_redis_hit_count += redis_hit_count
            except Exception:
                self._redis_available = False
                redis_misses = dict(route_pairs)
                if self._metrics is not None:
                    self._metrics.amap_cache_error_count += 1
                    self._metrics.amap_cache_redis_available = 0
                logger.info("Redis route cache preload failed; using Postgres cache")
        # Coordinate access is Redis/memory-only: the PG table is keyed by POI IDs.
        redis_misses = {k: v for k, v in redis_misses.items() if v[0].place_id > 0 and v[1].place_id > 0}
        if not redis_misses:
            return
        missed_pairs = [
            (
                origin.place_id,
                destination.place_id,
                mode,
                self._cache_provider_for_mode(mode),
            )
            for origin, destination, mode in redis_misses.values()
        ]
        pair_by_ids = {
            (origin.place_id, destination.place_id, mode): (
                cache_key,
                origin,
                destination,
            )
            for cache_key, (origin, destination, mode) in redis_misses.items()
        }
        try:
            async with get_session_factory()() as session:
                rows = (await session.execute(
                    text("""
                        SELECT provider, origin_place_id, destination_place_id, mode,
                               origin_longitude, origin_latitude,
                               destination_longitude, destination_latitude,
                               distance_meters, duration_minutes,
                               transit_steps_json
                        FROM travel_amap_route_cache
                        WHERE strategy = 0
                          AND updated_time >= NOW() - (:ttl_seconds * INTERVAL '1 second')
                          AND (origin_place_id, destination_place_id, mode, provider) IN (
                              SELECT *
                              FROM unnest(
                                  CAST(:origin_ids AS bigint[]),
                                  CAST(:destination_ids AS bigint[]),
                                  CAST(:modes AS text[]),
                                  CAST(:providers AS text[])
                              )
                          )
                    """),
                    {
                        "origin_ids": [
                            origin_id for origin_id, _, _, _ in missed_pairs
                        ],
                        "destination_ids": [
                            destination_id for _, destination_id, _, _ in missed_pairs
                        ],
                        "modes": [mode for _, _, mode, _ in missed_pairs],
                        "providers": [provider for _, _, _, provider in missed_pairs],
                        "ttl_seconds": self.cache_ttl_seconds,
                    },
                )).all()
        except Exception:
            self._cache_available = False
            logger.warning("Amap route cache preload failed; bypassing cache", exc_info=True)
            return
        loaded: dict[str, PreciseRoute] = {}
        for row in rows:
            pair = (
                int(row.origin_place_id),
                int(row.destination_place_id),
                str(row.mode),
            )
            entry = pair_by_ids.get(pair)
            if entry is None:
                continue
            cache_key, origin, destination = entry
            if not (
                self._coordinates_match(
                    cached_lng=row.origin_longitude,
                    cached_lat=row.origin_latitude,
                    current_lng=origin.longitude,
                    current_lat=origin.latitude,
                )
                and self._coordinates_match(
                    cached_lng=row.destination_longitude,
                    cached_lat=row.destination_latitude,
                    current_lng=destination.longitude,
                    current_lat=destination.latitude,
                )
            ):
                if self._metrics is not None:
                    self._metrics.amap_cache_coordinate_mismatch_count += 1
                continue
            route = PreciseRoute(
                distance_meters=int(row.distance_meters),
                duration_minutes=int(row.duration_minutes),
            )
            if self.api_generation == "v5":
                route_fact = row.transit_steps_json
                if route_fact is None:
                    continue
                route = self._route_from_cache_payload(
                    json.dumps({
                        "distance_meters": route.distance_meters,
                        "duration_minutes": route.duration_minutes,
                        "provider": AMAP_V5_COST_CACHE_PROVIDER,
                        "api_generation": "v5",
                        "mode": str(row.mode),
                        "payload_version": AMAP_ROUTE_CACHE_PAYLOAD_VERSION,
                        "route_fact": route_fact,
                    }),
                    api_generation="v5",
                    mode=str(row.mode),
                )
                if route is None:
                    if self._metrics is not None:
                        self._metrics.amap_cache_invalid_payload_count += 1
                    continue
            loaded[cache_key] = route
        for cache_key, route in loaded.items():
            self._remember_cache(cache_key, route)
        if self._metrics is not None and loaded:
            self._metrics.amap_cache_postgres_hit_count += len(loaded)

    async def close(self) -> None:
        await self._client.aclose()
        if self._redis_client is not None:
            await self._redis_client.aclose()

    def is_backing_off(self) -> bool:
        if time.monotonic() < self._disabled_until:
            return True
        keys = self._active_keys()
        coordinator = getattr(self, "_amap_keys", None)
        if not keys or coordinator is None:
            return False
        return not coordinator.usable_keys(keys, bucket=ROUTE_KEY_BUCKET)

    def _enter_backoff(self, reason: str) -> None:
        self._disabled_reason = reason
        self._disabled_until = time.monotonic() + self.backoff_seconds

    def _active_keys(self) -> list[str]:
        configured = [
            str(key).strip()
            for key in (getattr(self, "_configured_keys", None) or [])
            if str(key).strip()
        ]
        current = str(getattr(self, "api_key", "") or "").strip()
        # Tests assign a fixture key after construction. That override must
        # not rotate in a real key loaded from settings.
        if current and current not in configured:
            return [current]
        if configured:
            return configured
        return [current] if current else []

    def _limiter_fingerprint(self) -> str:
        # The pool sets the context var before reserve. A direct reserve, the
        # shape the existing tests stub, stays on this provider's own key.
        current_key = _CURRENT_AMAP_ROUTE_KEY.get()
        if not current_key:
            current_key = str(getattr(self, "api_key", "") or "").strip()
        if current_key:
            return key_fingerprint(current_key)
        keys = self._active_keys()
        return key_fingerprint(keys[0] if keys else "")

    def _coordinator(self):
        coordinator = getattr(self, "_amap_keys", None)
        if coordinator is None:
            coordinator = get_amap_key_coordinator()
            self._amap_keys = coordinator
        return coordinator

    def _no_usable_key(self, mode: str) -> bool:
        keys = self._active_keys()
        if not keys:
            return False
        return not self._coordinator().usable_keys(
            keys,
            bucket=ROUTE_KEY_BUCKET,
            mode=mode,
        )

    def _redacted_failure_message(self, payload: dict, request_key: str) -> str:
        info = str(
            payload.get("info")
            or payload.get("errmsg")
            or payload.get("message")
            or "Amap route API failed"
        )
        infocode = str(payload.get("infocode") or "")
        safe = redact_amap_key(info, request_key)
        safe = redact_amap_key(safe, str(self.api_key or ""))
        for key in self._active_keys():
            safe = redact_amap_key(safe, key)
        return f"{safe} ({infocode})"

    def _key_limit_error(
        self,
        message: str,
        infocode: str,
        *,
        kind: str,
    ) -> RouteProviderRateLimitError:
        # A direct _fetch_http_route call is outside the retry loop, so it
        # keeps the old provider-wide backoff.
        if _CURRENT_AMAP_ROUTE_KEY.get() is None:
            self._enter_backoff(message)
            return RouteProviderRateLimitError(message, infocode)
        if kind == "quota":
            return _AmapKeyQuota(message, infocode)
        return _AmapKeyOverLimit(message, infocode)

    def _log_key_event(self, api_key: str, mode: str, kind: str, infocode: str) -> None:
        logger.warning(
            "amap route key %s fingerprint=%s mode=%s infocode=%s",
            kind,
            key_fingerprint(api_key),
            mode,
            infocode or "-",
        )

    async def _unreserve_call(self) -> None:
        async with self._budget_lock:
            if self._call_count > 0:
                self._call_count -= 1
            if self._metrics is not None and self._metrics.amap_call_count > 0:
                self._metrics.amap_call_count -= 1

    async def _lookup_cache(
        self,
        *,
        origin: CandidatePlace,
        destination: CandidatePlace,
        city: str,
        mode: EffectiveCommuteMode,
    ) -> PreciseRoute | None:
        cache_key = self._route_cache_key(
            origin=origin,
            destination=destination,
            city=city,
            api_generation=self.api_generation,
            mode=mode,
        )
        if cache_key in self._memory_cache:
            expires_at = self._memory_cache_expires_at.get(cache_key)
            if expires_at is not None and expires_at <= time.monotonic():
                self._memory_cache.pop(cache_key, None)
                self._memory_cache_expires_at.pop(cache_key, None)
            else:
                if self._metrics is not None:
                    self._metrics.amap_cache_hit_count += 1
                route = self._memory_cache[cache_key]
                if self.api_generation == "v5" and mode == "transit":
                    return PreciseRoute(
                        distance_meters=route.distance_meters,
                        duration_minutes=route.duration_minutes,
                        encoded_polyline=route.encoded_polyline,
                        transit_steps=route.transit_steps,
                        transit_detail_quality=route.transit_detail_quality,
                        transit_detail_cache_hit=True,
                        fare_observation=route.fare_observation,
                    )
                return route
        if origin.place_id == 0 or destination.place_id == 0:
            redis_client = await self._get_redis_client()
            if redis_client is not None:
                try:
                    raw = await asyncio.wait_for(redis_client.get(cache_key), timeout=self._redis_timeout)
                    route = self._route_from_cache_payload(raw, api_generation=self.api_generation, mode=mode)
                    if route is not None:
                        self._remember_cache(cache_key, route)
                        if self._metrics is not None:
                            self._metrics.amap_cache_hit_count += 1
                            self._metrics.amap_cache_redis_hit_count += 1
                        return route
                except Exception:
                    self._redis_available = False
            return None
        if not self._cache_available:
            return None
        try:
            async with get_session_factory()() as session:
                row = (await session.execute(
                    text("""
                        UPDATE travel_amap_route_cache
                        SET hit_count = hit_count + 1,
                            last_hit_time = NOW()
                        WHERE provider = :provider
                          AND strategy = 0
                          AND mode = :mode
                          AND origin_place_id = :origin_place_id
                          AND destination_place_id = :destination_place_id
                          AND updated_time >= NOW() - (:ttl_seconds * INTERVAL '1 second')
                        RETURNING origin_longitude, origin_latitude,
                                  destination_longitude, destination_latitude,
                                  distance_meters, duration_minutes,
                                  transit_steps_json
                    """),
                    {
                        "origin_place_id": origin.place_id,
                        "destination_place_id": destination.place_id,
                        "provider": self._cache_provider_for_mode(mode),
                        "mode": mode,
                        "ttl_seconds": self.cache_ttl_seconds,
                    },
                )).one_or_none()
                await session.commit()
        except Exception:
            self._cache_available = False
            logger.warning("Amap route cache unavailable; bypassing cache", exc_info=True)
            return None
        if row is None:
            return None
        if not (
            self._coordinates_match(
                cached_lng=row.origin_longitude,
                cached_lat=row.origin_latitude,
                current_lng=origin.longitude,
                current_lat=origin.latitude,
            )
            and self._coordinates_match(
                cached_lng=row.destination_longitude,
                cached_lat=row.destination_latitude,
                current_lng=destination.longitude,
                current_lat=destination.latitude,
            )
        ):
            if self._metrics is not None:
                self._metrics.amap_cache_coordinate_mismatch_count += 1
            return None
        route = PreciseRoute(
            distance_meters=int(row.distance_meters),
            duration_minutes=int(row.duration_minutes),
        )
        if self.api_generation == "v5":
            if row.transit_steps_json is None:
                return None
            route = self._route_from_cache_payload(
                json.dumps({
                    "distance_meters": route.distance_meters,
                    "duration_minutes": route.duration_minutes,
                    "provider": AMAP_V5_COST_CACHE_PROVIDER,
                    "api_generation": "v5",
                    "mode": mode,
                    "payload_version": AMAP_ROUTE_CACHE_PAYLOAD_VERSION,
                    "route_fact": row.transit_steps_json,
                }),
                api_generation="v5",
                mode=mode,
            )
            if route is None:
                if self._metrics is not None:
                    self._metrics.amap_cache_invalid_payload_count += 1
                return None
        if self._metrics is not None:
            self._metrics.amap_cache_hit_count += 1
            self._metrics.amap_cache_postgres_hit_count += 1
        self._remember_cache(cache_key, route)
        return route

    async def _store_cache(
        self,
        *,
        origin: CandidatePlace,
        destination: CandidatePlace,
        route: PreciseRoute,
        city: str,
        mode: EffectiveCommuteMode,
    ) -> None:
        redis_client = await self._get_redis_client()
        if redis_client is not None:
            try:
                await asyncio.wait_for(
                    redis_client.set(
                        self._route_cache_key(
                            origin=origin,
                            destination=destination,
                            city=city,
                            api_generation=self.api_generation,
                            mode=mode,
                        ),
                        self._route_to_cache_payload(
                            route,
                            provider=self._cache_provider_for_mode(mode),
                            api_generation=self.api_generation,
                            mode=mode,
                        ),
                        ex=self.cache_ttl_seconds,
                    ),
                    timeout=self._redis_timeout,
                )
                if self._metrics is not None:
                    self._metrics.amap_cache_redis_write_count += 1
            except Exception:
                self._redis_available = False
                if self._metrics is not None:
                    self._metrics.amap_cache_error_count += 1
                    self._metrics.amap_cache_redis_available = 0
                logger.info("Redis route cache write failed; using Postgres cache")
        if origin.place_id == 0 or destination.place_id == 0:
            self._remember_cache(self._route_cache_key(origin=origin, destination=destination,
                city=city, api_generation=self.api_generation, mode=mode), route)
            return
        if not self._cache_available:
            return
        try:
            async with get_session_factory()() as session:
                await session.execute(
                    text("""
                        INSERT INTO travel_amap_route_cache (
                            provider, strategy, mode,
                            origin_place_id, destination_place_id,
                            origin_longitude, origin_latitude,
                            destination_longitude, destination_latitude,
                            distance_meters, duration_minutes,
                            transit_steps_json
                        )
                        VALUES (
                            :provider, 0, :mode,
                            :origin_place_id, :destination_place_id,
                            :origin_longitude, :origin_latitude,
                            :destination_longitude, :destination_latitude,
                            :distance_meters, :duration_minutes,
                            CAST(:transit_steps_json AS JSONB)
                        )
                        ON CONFLICT (
                            provider, strategy,
                            origin_place_id, destination_place_id, mode
                        )
                        DO UPDATE SET
                            origin_longitude = EXCLUDED.origin_longitude,
                            origin_latitude = EXCLUDED.origin_latitude,
                            destination_longitude = EXCLUDED.destination_longitude,
                            destination_latitude = EXCLUDED.destination_latitude,
                            distance_meters = EXCLUDED.distance_meters,
                            duration_minutes = EXCLUDED.duration_minutes,
                            transit_steps_json = EXCLUDED.transit_steps_json,
                            updated_time = NOW()
                    """),
                    {
                        "origin_place_id": origin.place_id,
                        "destination_place_id": destination.place_id,
                        "provider": self._cache_provider_for_mode(mode),
                        "mode": mode,
                        "origin_longitude": origin.longitude,
                        "origin_latitude": origin.latitude,
                        "destination_longitude": destination.longitude,
                        "destination_latitude": destination.latitude,
                        "distance_meters": route.distance_meters,
                        "duration_minutes": route.duration_minutes,
                        "transit_steps_json": (
                            json.dumps(
                                self._v5_route_fact(route, mode=mode),
                                separators=(",", ":"),
                            )
                            if self.api_generation == "v5"
                            else None
                        ),
                    },
                )
                await session.commit()
        except Exception:
            self._cache_available = False
            logger.warning("Amap route cache write failed; bypassing cache", exc_info=True)
            return
        if self._metrics is not None:
            self._metrics.amap_cache_write_count += 1
            self._metrics.amap_cache_postgres_write_count += 1
        self._remember_cache(
            self._route_cache_key(
                origin=origin,
                destination=destination,
                city=city,
                api_generation=self.api_generation,
                mode=mode,
            ),
            route,
        )

    def _memory_cache_has(self, cache_key: str) -> bool:
        if cache_key not in self._memory_cache:
            return False
        expires_at = self._memory_cache_expires_at.get(cache_key)
        if expires_at is not None and expires_at <= time.monotonic():
            self._memory_cache.pop(cache_key, None)
            self._memory_cache_expires_at.pop(cache_key, None)
            return False
        return True

    def _plan_route_cache_keys(
        self,
        plan: RoutePlan,
        *,
        city: str,
        generation_mode: EffectiveCommuteMode,
    ) -> set[str]:
        keys: set[str] = set()
        for day_group in plan.day_groups:
            for index in range(max(0, len(day_group.places) - 1)):
                origin = day_group.places[index]
                destination = day_group.places[index + 1]
                for mode in _provider_query_modes(
                    origin,
                    destination,
                    generation_mode=generation_mode,
                ):
                    keys.add(
                        self._route_cache_key(
                            origin=origin,
                            destination=destination,
                            city=city,
                            api_generation=self.api_generation,
                            mode=mode,
                        )
                    )
        return keys

    def allocate_uncached_plan_calls(
        self,
        plan: RoutePlan,
        *,
        city: str,
        generation_mode: EffectiveCommuteMode,
    ) -> int:
        keys = self._plan_route_cache_keys(
            plan,
            city=city,
            generation_mode=generation_mode,
        )
        uncached = {
            key for key in keys
            if not self._memory_cache_has(key)
        }
        new_keys = uncached - self._allocated_uncached_keys
        if not self._allocation_started:
            self._allocation_started = True
            self._allocated_call_limit = 0
        self._allocated_uncached_keys.update(new_keys)
        added = len(new_keys)
        if self.hard_call_cap:
            self._allocated_call_limit = min(
                self.hard_call_cap,
                max(self._call_count, self._allocated_call_limit + added),
            )
        else:
            self._allocated_call_limit = max(
                self._call_count,
                self._allocated_call_limit + added,
            )
        self.call_budget = self._allocated_call_limit
        if self._metrics is not None:
            self._metrics.amap_budget_call_hard_cap = self.hard_call_cap
            self._metrics.amap_budget_call_limit = self._allocated_call_limit
            self._metrics.amap_budget_unique_uncached_allocated_leg_count = len(
                self._allocated_uncached_keys
            )
        return added

    def _effective_call_limit(self) -> int:
        if self._allocation_started:
            return self._allocated_call_limit
        return self.hard_call_cap

    def _raise_call_budget_exceeded(self) -> None:
        limit = self._effective_call_limit()
        if self._metrics is not None:
            self._metrics.amap_budget_call_exceeded_count += 1
            self._metrics.amap_budget_exceeded_count += 1
        raise RouteProviderBudgetExceededError(
            f"Amap route call budget exceeded: {limit}"
        )

    def _raise_time_budget_exceeded(self) -> None:
        if self._metrics is not None:
            limit_ms = self._metrics.amap_budget_time_effective_limit_ms
            self._metrics.amap_budget_time_exceeded_count += 1
            self._metrics.amap_budget_exceeded_count += 1
        elif self._deadline_monotonic is not None:
            limit_ms = max(
                0,
                int(
                    (self._deadline_monotonic - self._wall_start_monotonic) * 1000
                ),
            )
        else:
            limit_ms = self.time_budget_ms
        raise RouteProviderBudgetExceededError(
            f"Amap route time budget exceeded: {limit_ms}ms"
        )

    def _remaining_wall_seconds(self) -> float | None:
        deadline_monotonic = getattr(self, "_deadline_monotonic", None)
        if deadline_monotonic is None:
            return None
        return deadline_monotonic - time.monotonic()

    def _assert_amap_budget_available(self) -> None:
        if self._allocation_started:
            if self._call_count >= self._allocated_call_limit:
                self._raise_call_budget_exceeded()
        elif self.hard_call_cap and self._call_count >= self.hard_call_cap:
            self._raise_call_budget_exceeded()
        if (
            self._deadline_monotonic is not None
            and time.monotonic() >= self._deadline_monotonic
        ):
            self._raise_time_budget_exceeded()

    async def _reserve_amap_call(
        self,
        effective_mode: EffectiveCommuteMode,
        *,
        key_fingerprint: str | None = None,
    ) -> float:
        fingerprint = key_fingerprint or self._limiter_fingerprint()
        # Count the call before the sleep, then wait outside this provider's
        # lock. Two keys can then occupy two limiter scopes at once. A rejected
        # slot rolls the count back so a deadline miss still reports zero calls.
        async with self._budget_lock:
            self._assert_amap_budget_available()
            self._call_count += 1
            if self._metrics is not None:
                self._metrics.amap_call_count += 1
        try:
            wait_seconds = await _PROCESS_WIDE_AMAP_RATE_LIMITER.wait_for_slot(
                scope=(fingerprint, self.api_generation, effective_mode),
                min_interval=self.min_interval,
                deadline_monotonic=self._deadline_monotonic,
            )
        except BaseException:
            await self._unreserve_call()
            raise
        if wait_seconds is None or (
            self._deadline_monotonic is not None
            and time.monotonic() >= self._deadline_monotonic
        ):
            await self._unreserve_call()
            self._raise_time_budget_exceeded()
        if self._metrics is not None:
            self._metrics.amap_wait_ms += int(wait_seconds * 1000)
        return time.monotonic()

    async def _fetch_http_route(
        self,
        *,
        origin: CandidatePlace,
        destination: CandidatePlace,
        city: str,
        effective_mode: EffectiveCommuteMode,
        citycode: str | None,
    ) -> PreciseRoute:
        request_key = _CURRENT_AMAP_ROUTE_KEY.get() or str(self.api_key or "")
        if not request_key:
            raise RouteProviderError("AMAP_API_KEY is not configured")
        if self.api_generation == "v3":
            if effective_mode != "driving":
                raise RouteProviderError("v3 route compatibility only supports driving")
            url = AMAP_DRIVING_URL
            params = {
                "key": request_key,
                "origin": f"{origin.longitude},{origin.latitude}",
                "destination": f"{destination.longitude},{destination.latitude}",
                "strategy": 0,
                "extensions": "base",
                "output": "JSON",
            }
        else:
            if effective_mode == "transit" and not citycode:
                raise RouteProviderError("transit route requires citycode")
            url = AMAP_V5_ROUTE_URLS[effective_mode]
            params = {
                "key": request_key,
                "origin": f"{origin.longitude},{origin.latitude}",
                "destination": f"{destination.longitude},{destination.latitude}",
                "show_fields": "cost,polyline",
            }
            if effective_mode == "transit":
                params.update({"city1": citycode, "city2": citycode})
        request_timeout = float(self.timeout)
        remaining = self._remaining_wall_seconds()
        if remaining is not None:
            if remaining <= 0:
                self._raise_time_budget_exceeded()
            request_timeout = min(request_timeout, remaining)
        try:
            response = await asyncio.wait_for(
                self._client.get(
                    url,
                    params=params,
                    timeout=request_timeout,
                ),
                timeout=request_timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except (asyncio.TimeoutError, httpx.TimeoutException) as exc:
            remaining_after = self._remaining_wall_seconds()
            wall_bound_timeout = remaining is not None and remaining < float(
                self.timeout
            )
            wall_deadline_reached = (
                remaining_after is not None and remaining_after <= 0
            )
            if wall_bound_timeout or wall_deadline_reached:
                try:
                    self._raise_time_budget_exceeded()
                except RouteProviderBudgetExceededError as budget_exc:
                    raise budget_exc from exc
            raise RouteProviderError("Amap route API request failed") from exc
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 429:
                message = "Amap route API HTTP 429 rate limit"
                if self._metrics is not None:
                    self._metrics.amap_rate_limit_count += 1
                raise self._key_limit_error(
                    message,
                    "429",
                    kind="over_limit",
                ) from exc
            raise RouteProviderError(
                f"Amap route API HTTP {exc.response.status_code}"
            ) from exc
        except httpx.HTTPError as exc:
            raise RouteProviderError("Amap route API request failed") from exc
        except ValueError as exc:
            raise RouteProviderError("Amap route API returned invalid JSON") from exc
        if (
            payload.get("status") != "1"
            or str(payload.get("infocode") or "") != "10000"
        ):
            infocode = str(payload.get("infocode") or "").strip()
            message = self._redacted_failure_message(payload, request_key)
            kind = classify_amap_failure(payload)
            if kind is None and _is_amap_route_rate_limit(payload):
                kind = "over_limit"
            if kind in {"quota", "over_limit"}:
                if self._metrics is not None:
                    self._metrics.amap_rate_limit_count += 1
                raise self._key_limit_error(message, infocode, kind=kind)
            raise RouteProviderError(
                message
            )
        route_payload = payload.get("route") or {}
        paths = (
            route_payload.get("transits")
            if effective_mode == "transit" and self.api_generation == "v5"
            else route_payload.get("paths")
        ) or []
        if not paths:
            raise RouteProviderError("Amap route API returned no path")
        path = paths[0]
        try:
            distance = max(0, int(float(path["distance"])))
            if self.api_generation == "v3":
                duration_seconds = path["duration"]
            else:
                cost = path.get("cost") or {}
                duration_seconds = cost.get("duration")
                if duration_seconds in (None, "") and effective_mode == "cycling":
                    duration_seconds = path.get("duration")
                if duration_seconds in (None, ""):
                    raise KeyError("duration")
            duration = max(1, math.ceil(float(duration_seconds) / 60))
        except (KeyError, TypeError, ValueError) as exc:
            raise RouteProviderError("Amap route response is incomplete") from exc
        transit_steps: tuple[TransitStep, ...] = ()
        transit_detail_quality = "missing"
        if effective_mode == "transit" and self.api_generation == "v5":
            transit_steps, unsupported = normalize_transit_detail(path)
            transit_detail_quality = classify_transit_detail(
                transit_steps,
                unsupported=unsupported,
            )
        captured_at = utc_now()
        fare_result = adapt_amap_route_fare(
            mode=effective_mode,
            path=path,
            distance_meters=distance,
            route_identity=(
                f"route:amap:{city}:{origin.place_id}->{destination.place_id}:"
                f"{effective_mode}"
            ),
            captured_at=captured_at,
            observed_at=captured_at,
        )
        if effective_mode in {"driving", "walking", "cycling"}:
            encoded_polyline = self._flatten_step_polyline(path)
        elif effective_mode == "transit":
            encoded_polyline = self._flatten_transit_polyline(path)
        else:
            encoded_polyline = ""
        return PreciseRoute(
            distance_meters=distance,
            duration_minutes=duration,
            encoded_polyline=encoded_polyline,
            transit_steps=transit_steps,
            transit_detail_quality=transit_detail_quality,
            fare_observation=fare_result.selected,
        )

    async def route_access(self, **kwargs) -> PreciseRoute:
        """Coordinate-only lodging transport, sharing this provider's global budget."""
        origin, destination = kwargs["origin"], kwargs["destination"]
        if (origin.place_id == 0) == (destination.place_id == 0):
            raise RouteProviderError("access requires exactly one coordinate-only endpoint")
        if min(origin.place_id, destination.place_id) < 0:
            raise RouteProviderError("access POI endpoint must have a positive canonical ID")
        return await self.route(**kwargs)

    async def _fetch_route_with_key_pool(
        self,
        *,
        origin: CandidatePlace,
        destination: CandidatePlace,
        city: str,
        effective_mode: EffectiveCommuteMode,
        citycode: str | None,
    ) -> PreciseRoute:
        keys = self._active_keys()
        excluded: set[str] = set()
        last_error: RouteProviderRateLimitError | None = None
        while True:
            key = self._coordinator().next_key(
                keys,
                bucket=ROUTE_KEY_BUCKET,
                mode=effective_mode,
                exclude=excluded,
            )
            if key is None:
                if len(keys) == 1 and last_error is not None:
                    self._enter_backoff(str(last_error))
                raise RouteProviderRateLimitError(
                    str(last_error or "Amap route provider is backing off"),
                    getattr(last_error, "infocode", ""),
                )
            token = _CURRENT_AMAP_ROUTE_KEY.set(key)
            try:
                await self._reserve_amap_call(effective_mode)
                try:
                    return await self._fetch_http_route(
                        origin=origin,
                        destination=destination,
                        city=city,
                        effective_mode=effective_mode,
                        citycode=citycode,
                    )
                except _AmapKeyQuota as exc:
                    self._coordinator().mark_quota_exhausted(
                        key,
                        bucket=ROUTE_KEY_BUCKET,
                    )
                    excluded.add(key_fingerprint(key))
                    last_error = exc
                    self._log_key_event(key, effective_mode, "quota", exc.infocode)
                    continue
                except _AmapKeyOverLimit as exc:
                    self._coordinator().mark_over_limit(
                        key,
                        effective_mode,
                        self.backoff_seconds,
                    )
                    excluded.add(key_fingerprint(key))
                    last_error = exc
                    self._log_key_event(
                        key,
                        effective_mode,
                        "over_limit",
                        exc.infocode,
                    )
                    if len(keys) == 1:
                        self._enter_backoff(str(exc))
                        raise
                    continue
            finally:
                _CURRENT_AMAP_ROUTE_KEY.reset(token)

    async def route(
        self,
        *,
        origin: CandidatePlace,
        destination: CandidatePlace,
        city: str,
        effective_mode: EffectiveCommuteMode,
        citycode: str | None,
    ) -> PreciseRoute:
        if self.is_backing_off():
            raise RouteProviderRateLimitError(
                self._disabled_reason or "Amap route provider is backing off"
            )
        if not self._active_keys():
            raise RouteProviderError("AMAP_API_KEY is not configured")
        if self._no_usable_key(effective_mode):
            raise RouteProviderRateLimitError(
                self._disabled_reason or "Amap route provider is backing off"
            )
        if None in (
            origin.longitude,
            origin.latitude,
            destination.longitude,
            destination.latitude,
        ):
            raise RouteProviderError("route endpoint has no coordinates")
        if (
            self.api_generation == "v5"
            and effective_mode == "transit"
            and not citycode
        ):
            raise RouteProviderError("transit route requires citycode")
        cached = await self._lookup_cache(
            origin=origin,
            destination=destination,
            city=city,
            mode=effective_mode,
        )
        if cached is not None:
            return cached
        route = await self._fetch_route_with_key_pool(
            origin=origin,
            destination=destination,
            city=city,
            effective_mode=effective_mode,
            citycode=citycode,
        )
        await self._store_cache(
            origin=origin,
            destination=destination,
            route=route,
            city=city,
            mode=effective_mode,
        )
        if self._metrics is not None:
            self._metrics.amap_success_count += 1
            self._metrics.record_effective_success(effective_mode)
        return route


# Compatibility import for existing callers while the implementation is now
# one shared v3/v5 mode-aware router.
AmapDrivingRouteProvider = AmapRouteProvider


def _is_amap_route_rate_limit(payload: dict) -> bool:
    infocode = str(payload.get("infocode") or "").strip()
    if infocode in AMAP_ROUTE_RATE_LIMIT_INFOCODES:
        return True
    text = " ".join(
        str(payload.get(key) or "")
        for key in ("info", "infocode", "errmsg", "message")
    ).upper()
    return any(marker.upper() in text for marker in AMAP_ROUTE_RATE_LIMIT_MARKERS)


def haversine_km(left: CandidatePlace, right: CandidatePlace) -> float:
    """Return straight-line distance between two stored WGS/GCJ coordinates."""
    if None in (left.longitude, left.latitude, right.longitude, right.latitude):
        return math.inf
    lat1 = math.radians(float(left.latitude))
    lat2 = math.radians(float(right.latitude))
    delta_lat = lat2 - lat1
    delta_lng = math.radians(float(right.longitude) - float(left.longitude))
    value = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lng / 2) ** 2
    )
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(value))


def _time_preferences_active(request: TripRequest | None) -> bool:
    return bool(
        request is not None
        and getattr(get_settings(), "time_preferences_enabled", False)
        and request.has_time_preferences()
    )


def _suppress_evening_markers(request: TripRequest | None = None) -> bool:
    if not _time_preferences_active(request):
        return False
    assert request is not None
    daily_end = request.daily_end
    if daily_end is None:
        return False
    hours, minutes = daily_end.split(":", 1)
    return int(hours) * 60 + int(minutes) < 18 * 60


def _estimate_route_values(
    left: CandidatePlace,
    right: CandidatePlace,
    *,
    effective_mode: EffectiveCommuteMode,
) -> tuple[float, int, int]:
    settings = get_settings()
    straight_km = haversine_km(left, right)
    if not math.isfinite(straight_km):
        raise ValueError("cannot estimate route without coordinates")
    estimated_km = straight_km * settings.route_estimate_road_factor
    duration_minutes = max(
        1,
        math.ceil(
            estimated_km
            * 60
            / commute_estimate_speed_kmh(effective_mode, settings)
        ),
    )
    return estimated_km, round(estimated_km * 1000), duration_minutes


def resolve_leg_effective_mode(
    left: CandidatePlace,
    right: CandidatePlace,
    *,
    generation_mode: EffectiveCommuteMode,
) -> EffectiveCommuteMode:
    settings = get_settings()
    _, road_distance_meters, raw_minutes = _estimate_route_values(
        left,
        right,
        effective_mode=generation_mode,
    )
    if generation_mode == "driving":
        return "driving"
    if generation_mode == "transit":
        # Straight-line proximity only triggers a walking query later. Estimates
        # stay transit so search never treats unproven walking as reachability.
        return "transit"
    if generation_mode == "walking":
        if raw_minutes > single_leg_max_minutes("walking", settings):
            return "transit"
        return "walking"
    if road_distance_meters <= settings.commute_walk_switch_max_meters:
        return "walking"
    if raw_minutes > single_leg_max_minutes("cycling", settings):
        return "transit"
    return "cycling"


def _straight_line_meters(left: CandidatePlace, right: CandidatePlace) -> float:
    distance_km = haversine_km(left, right)
    if not math.isfinite(distance_km):
        return math.inf
    return distance_km * 1000.0


def _transit_walk_proof_triggered(
    left: CandidatePlace,
    right: CandidatePlace,
    *,
    generation_mode: EffectiveCommuteMode,
) -> bool:
    if generation_mode != "transit":
        return False
    settings = get_settings()
    return _straight_line_meters(left, right) <= float(
        settings.commute_transit_walk_proof_max_meters
    )


def _provider_query_modes(
    origin: CandidatePlace,
    destination: CandidatePlace,
    *,
    generation_mode: EffectiveCommuteMode,
) -> list[EffectiveCommuteMode]:
    estimated = resolve_leg_effective_mode(
        origin,
        destination,
        generation_mode=generation_mode,
    )
    modes: list[EffectiveCommuteMode] = [estimated]
    if _transit_walk_proof_triggered(
        origin,
        destination,
        generation_mode=generation_mode,
    ) and "walking" not in modes:
        modes.append("walking")
    return modes


def _valid_precise_route_values(distance_meters: object, duration_minutes: object) -> bool:
    return (
        isinstance(duration_minutes, int)
        and not isinstance(duration_minutes, bool)
        and duration_minutes > 0
        and isinstance(distance_meters, int)
        and not isinstance(distance_meters, bool)
        and distance_meters >= 0
    )


def _walk_proof_duration_limit(settings=None) -> int:
    configured = int((settings or get_settings()).route_single_leg_max_walking)
    return min(WALK_PROOF_MAX_DURATION_MINUTES, configured)


def _walk_proof_duration_ok(duration_minutes: int, settings=None) -> bool:
    return duration_minutes <= _walk_proof_duration_limit(settings)


def _adopted_walk_commute(leg: CommuteLeg) -> bool:
    return (
        leg.mode == "walking"
        and leg.source == "amap"
        and _valid_precise_route_values(leg.distance_meters, leg.duration_minutes)
        and _walk_proof_duration_ok(leg.duration_minutes)
    )


def _adopted_walk_access(leg: AccessLeg) -> bool:
    return (
        leg.mode == "walking"
        and leg.duration_source == "amap"
        and _valid_precise_route_values(leg.distance_meters, leg.duration_minutes)
        and _walk_proof_duration_ok(leg.duration_minutes)
    )


def _walk_proof_error_reason(error: BaseException) -> str:
    if isinstance(error, asyncio.CancelledError):
        return WALK_PROOF_CANCELLED
    if isinstance(error, asyncio.TimeoutError):
        return WALK_PROOF_TIMEOUT
    if isinstance(error, RouteProviderBudgetExceededError):
        return WALK_PROOF_BUDGET
    if isinstance(error, RouteProviderRateLimitError):
        return WALK_PROOF_RATE_LIMITED
    if isinstance(error, RouteProviderError):
        if "no path" in str(error).lower():
            return WALK_PROOF_NO_PATH
        return WALK_PROOF_PROVIDER_ERROR
    return WALK_PROOF_PROVIDER_ERROR


def _walk_reason_of(leg) -> str | None:
    if isinstance(leg, str) and leg:
        return leg
    if leg is None:
        return None
    return (
        getattr(leg, "walk_proof_decision", None)
        or getattr(leg, "walk_proof_receipt", None)
        or None
    )


def _negative_walk_reason(reason: str | None) -> str | None:
    if reason and reason != WALK_PROOF_ADOPTED:
        return reason
    return None


def _walk_proof_reject_key(from_place_id: int, to_place_id: int) -> tuple[str, int, int]:
    return (WALK_PROOF_REJECT_SENTINEL, int(from_place_id), int(to_place_id))


def _cached_walk_reject_reason(
    known_precise_legs: dict | None,
    from_place_id: int,
    to_place_id: int,
) -> str | None:
    if not known_precise_legs:
        return None
    reason = known_precise_legs.get(
        _walk_proof_reject_key(from_place_id, to_place_id)
    )
    return reason if isinstance(reason, str) and reason else None


def _known_success_commute(value) -> bool:
    return (
        isinstance(value, CommuteLeg)
        and value.source == "amap"
        and _valid_precise_route_values(value.distance_meters, value.duration_minutes)
    )


def _known_success_access(value) -> bool:
    return (
        isinstance(value, AccessLeg)
        and value.duration_source == "amap"
        and _valid_precise_route_values(value.distance_meters, value.duration_minutes)
    )


def _stamp_walk_proof(leg, reason: str | None):
    if not reason:
        return leg
    updates = {}
    if getattr(leg, "walk_proof_decision", None) != reason:
        updates["walk_proof_decision"] = reason
    if getattr(leg, "walk_proof_receipt", None) != reason:
        updates["walk_proof_receipt"] = reason
    if not updates:
        return leg
    return leg.model_copy(update=updates)


def _remember_walk_proof_decision(
    known_precise_legs: dict | None,
    estimated: CommuteLeg,
    reason: str,
) -> None:
    if known_precise_legs is None or not reason or reason == WALK_PROOF_ADOPTED:
        return
    known_precise_legs[
        _walk_proof_reject_key(estimated.from_place_id, estimated.to_place_id)
    ] = reason


def _with_walk_decision(leg, reason: str | None):
    if not reason:
        return leg
    return _stamp_walk_proof(leg, reason)


def _estimate_with_cached_walk_reason(
    estimated: CommuteLeg,
    known_precise_legs: dict | None,
) -> CommuteLeg:
    """Stamp a walking-proof reason onto an estimate without reapplying precise facts."""
    if not known_precise_legs:
        return estimated
    reason = _cached_walk_reject_reason(
        known_precise_legs, estimated.from_place_id, estimated.to_place_id,
    )
    if not reason:
        found = known_precise_legs.get(
            (estimated.from_place_id, estimated.to_place_id, estimated.mode)
        )
        reason = (
            _negative_walk_reason(_walk_reason_of(found))
            if _known_success_commute(found)
            else None
        )
    if not reason:
        return estimated
    return _stamp_walk_proof(estimated, reason)


def _transit_walk_proof_applies(
    origin: CandidatePlace | None,
    destination: CandidatePlace | None,
    *,
    generation_mode: EffectiveCommuteMode | None,
    estimated_mode: EffectiveCommuteMode | None = None,
) -> bool:
    if generation_mode is None:
        generation_mode = estimated_mode
    if generation_mode != "transit" or origin is None or destination is None:
        return False
    return _transit_walk_proof_triggered(
        origin,
        destination,
        generation_mode="transit",
    )


def _lookup_known_commute_leg(
    estimated: CommuteLeg,
    known_precise_legs: dict | None,
    *,
    origin: CandidatePlace | None = None,
    destination: CandidatePlace | None = None,
    generation_mode: EffectiveCommuteMode | None = None,
) -> CommuteLeg:
    if not known_precise_legs:
        return estimated
    walking = known_precise_legs.get(
        (estimated.from_place_id, estimated.to_place_id, "walking")
    )
    prove = _transit_walk_proof_applies(
        origin,
        destination,
        generation_mode=generation_mode,
        estimated_mode=estimated.mode,
    )
    if prove and isinstance(walking, CommuteLeg) and _adopted_walk_commute(walking):
        return walking.model_copy(deep=True)
    found = known_precise_legs.get(
        (estimated.from_place_id, estimated.to_place_id, estimated.mode)
    )
    reject = (
        _cached_walk_reject_reason(
            known_precise_legs, estimated.from_place_id, estimated.to_place_id,
        )
        if prove
        else None
    )
    if not _known_success_commute(found):
        if reject:
            return _stamp_walk_proof(estimated, reject)
        return estimated
    copied = found.model_copy(deep=True)
    reason = reject or (
        _negative_walk_reason(_walk_reason_of(copied)) if prove else None
    )
    if reason:
        return _stamp_walk_proof(copied, reason)
    return copied


def _lookup_known_access(
    leg: AccessLeg,
    place: CandidatePlace,
    known_access: dict | None,
    *,
    generation_mode: EffectiveCommuteMode | None = None,
) -> AccessLeg:
    known_access = known_access or {}
    origin, destination = _v2_access_endpoints(leg, place)
    prove = _transit_walk_proof_applies(
        origin,
        destination,
        generation_mode=generation_mode,
        estimated_mode=leg.mode,
    )
    walking_key = _v2_access_key(leg.model_copy(update={"mode": "walking"}), place)
    walking = known_access.get(walking_key)
    if prove and isinstance(walking, AccessLeg) and _adopted_walk_access(walking):
        return walking.model_copy(deep=True)
    found = known_access.get(_v2_access_key(leg, place))
    if not _known_success_access(found):
        return leg
    copied = found.model_copy(deep=True)
    if prove:
        reason = _negative_walk_reason(_walk_reason_of(copied))
        if reason:
            return _stamp_walk_proof(copied, reason)
    return copied


def _precise_commute_leg(
    origin: CandidatePlace,
    destination: CandidatePlace,
    precise: PreciseRoute,
    *,
    mode: EffectiveCommuteMode,
    requested_commute_mode: str,
    walk_proof_decision: str | None = None,
) -> CommuteLeg:
    mode_labels = {
        "driving": "驾车",
        "transit": "公交",
        "walking": "步行",
        "cycling": "骑行",
    }
    transit = mode == "transit"
    walking = mode == "walking"
    leg = CommuteLeg(
        from_place_id=origin.place_id,
        to_place_id=destination.place_id,
        from_name=origin.name,
        to_name=destination.name,
        distance_meters=precise.distance_meters,
        duration_minutes=precise.duration_minutes,
        mode=mode,
        source="amap",
        note=(
            f"{origin.name} → {destination.name}："
            f"{mode_labels[mode]}预计 {precise.duration_minutes} 分钟"
        ),
        encoded_polyline=precise.encoded_polyline or "",
        transit_steps=list(precise.transit_steps) if transit else [],
        transit_detail_quality=(
            precise.transit_detail_quality if transit else "missing"
        ),
        transit_detail_cache_hit=(
            precise.transit_detail_cache_hit if transit else False
        ),
        fare_observation=(
            None
            if walking
            else fare_for_locked_leg(
                precise.fare_observation,
                effective_mode=mode,
                requested_commute_mode=requested_commute_mode,
            )
        ),
        walk_proof_decision=walk_proof_decision,
    )
    return _stamp_walk_proof(leg, walk_proof_decision)


_SHORT_TRANSIT_ESTIMATE_WALK_SPEED = contextvars.ContextVar(
    "short_transit_estimate_walk_speed", default=False,
)


def _duration_mode_for_estimate(
    left: CandidatePlace,
    right: CandidatePlace,
    *,
    effective_mode: EffectiveCommuteMode,
    generation_mode: EffectiveCommuteMode | None = None,
) -> EffectiveCommuteMode:
    # Repair previews only: unproven short transit keeps transit mode, but uses
    # walking speed so a 1-minute transit guess is not treated as reachability.
    if (
        _SHORT_TRANSIT_ESTIMATE_WALK_SPEED.get()
        and generation_mode == "transit"
        and effective_mode == "transit"
        and _transit_walk_proof_triggered(left, right, generation_mode="transit")
    ):
        return "walking"
    return effective_mode


def _estimated_leg(
    left: CandidatePlace,
    right: CandidatePlace,
    *,
    effective_mode: EffectiveCommuteMode,
    generation_mode: EffectiveCommuteMode | None = None,
) -> CommuteLeg:
    _, distance_meters, duration_minutes = _estimate_route_values(
        left,
        right,
        effective_mode=_duration_mode_for_estimate(
            left,
            right,
            effective_mode=effective_mode,
            generation_mode=generation_mode,
        ),
    )
    mode_labels = {
        "driving": "驾车",
        "transit": "公交",
        "walking": "步行",
        "cycling": "骑行",
    }
    return CommuteLeg(
        from_place_id=left.place_id,
        to_place_id=right.place_id,
        from_name=left.name,
        to_name=right.name,
        distance_meters=distance_meters,
        duration_minutes=duration_minutes,
        mode=effective_mode,
        source="estimate",
        note=(
            f"{left.name} → {right.name}："
            f"{mode_labels[effective_mode]}预计 {duration_minutes} 分钟"
        ),
    )


def _legs(
    places: list[CandidatePlace],
    *,
    generation_mode: EffectiveCommuteMode,
) -> list[CommuteLeg]:
    return [
        _estimated_leg(
            places[index],
            places[index + 1],
            effective_mode=resolve_leg_effective_mode(
                places[index],
                places[index + 1],
                generation_mode=generation_mode,
            ),
            generation_mode=generation_mode,
        )
        for index in range(len(places) - 1)
    ]


def _near_duplicate_key(place: CandidatePlace) -> str:
    """Conservative key for obvious same-place extraction variants."""
    value = re.sub(r"\s+", "", place.name or "").strip("（）()【】[]")
    value = value.translate(_NEAR_DUPLICATE_CHAR_MAP)
    for suffix in _NEAR_DUPLICATE_SUFFIXES:
        if value.endswith(suffix) and len(value) > len(suffix) + 1:
            value = value[: -len(suffix)]
            break
    return value


def _place_distance_km(a: CandidatePlace, b: CandidatePlace) -> float | None:
    if not _has_coordinates(a) or not _has_coordinates(b):
        return None
    return _haversine_coords(
        float(a.latitude), float(a.longitude),
        float(b.latitude), float(b.longitude),
    )


def _is_near_duplicate_pair(a: CandidatePlace, b: CandidatePlace) -> bool:
    """Check if two places are near-duplicates via key equality or substring containment + proximity."""
    if get_settings().selector_route_v2_enabled:
        from src.agents.activity_arrangement import nested_relation
        if nested_relation(a,b) or nested_relation(b,a):
            return False
    ka = _near_duplicate_key(a) or f"id:{a.place_id}"
    kb = _near_duplicate_key(b) or f"id:{b.place_id}"
    if ka == kb:
        return True
    short, long = (ka, kb) if len(ka) <= len(kb) else (kb, ka)
    if len(short) < _NEAR_DUPLICATE_CONTAINMENT_MIN_LEN or short not in long:
        return False
    dist = _place_distance_km(a, b)
    if dist is None:
        return False
    return dist <= _NEAR_DUPLICATE_CONTAINMENT_MAX_DISTANCE_KM


def _prefer_duplicate_candidate(
    current: CandidatePlace,
    candidate: CandidatePlace,
) -> CandidatePlace:
    current_score = (
        current.effective_score,
        current.recommend_score,
        current.source_count,
        current.mention_count,
        -current.place_id,
    )
    candidate_score = (
        candidate.effective_score,
        candidate.recommend_score,
        candidate.source_count,
        candidate.mention_count,
        -candidate.place_id,
    )
    return candidate if candidate_score > current_score else current


def _dedupe_near_duplicate_places(
    places: list[CandidatePlace],
    *,
    must_include_ids: set[int] | None = None,
) -> tuple[list[CandidatePlace], list[int]]:
    protected = must_include_ids or set()
    retained: list[CandidatePlace] = []
    dropped: list[int] = []
    for place in places:
        duplicate_of: CandidatePlace | None = None
        for kept in retained:
            if _is_near_duplicate_pair(place, kept):
                duplicate_of = kept
                break
        if duplicate_of is None:
            retained.append(place)
            continue
        if place.place_id in protected:
            preferred, removed = place, duplicate_of
        elif duplicate_of.place_id in protected:
            preferred, removed = duplicate_of, place
        else:
            preferred = _prefer_duplicate_candidate(duplicate_of, place)
            removed = duplicate_of if preferred is place else place
        if removed is duplicate_of:
            retained = [preferred if p is duplicate_of else p for p in retained]
        dropped.append(removed.place_id)
        logger.info(
            "Dropped near-duplicate route POI %s(%s) in favor of %s(%s)",
            removed.name,
            removed.place_id,
            preferred.name,
            preferred.place_id,
        )
    return retained, dropped


def _day_center(day_group: RouteDayGroup) -> tuple[float, float] | None:
    located = [
        place for place in day_group.places
        if place.latitude is not None and place.longitude is not None
    ]
    if not located:
        return None
    return (
        sum(float(place.latitude) for place in located) / len(located),
        sum(float(place.longitude) for place in located) / len(located),
    )


def _nearest_neighbor(
    places: list[CandidatePlace],
    accommodation_coord: tuple[float, float] | None = None,
) -> list[CandidatePlace]:
    if len(places) < 2:
        return list(places)
    remaining = list(places)
    if accommodation_coord:
        top3 = sorted(
            places,
            key=lambda place: (
                place.effective_score,
                place.recommend_score,
                -place.place_id,
            ),
            reverse=True,
        )[:3]
        top3_ids = {place.place_id for place in top3}
        near_and_good = [
            place for place in places
            if place.place_id in top3_ids
            and _anchor_distance_km(place, accommodation_coord) <= 5.0
        ]
        if near_and_good:
            start = min(
                near_and_good,
                key=lambda place: _anchor_distance_km(
                    place,
                    accommodation_coord,
                ),
            )
        else:
            start = top3[0]
    else:
        start = max(
            remaining,
            key=lambda place: (
                place.effective_score,
                place.recommend_score,
                -place.place_id,
            ),
        )
    ordered = [start]
    remaining.remove(start)
    while remaining:
        current = ordered[-1]
        next_place = min(
            remaining,
            key=lambda place: (
                haversine_km(current, place),
                -place.effective_score,
                place.place_id,
            ),
        )
        ordered.append(next_place)
        remaining.remove(next_place)
    return ordered


def _has_coordinates(place: CandidatePlace) -> bool:
    return place.latitude is not None and place.longitude is not None


def _haversine_coords(
    left_latitude: float,
    left_longitude: float,
    right_latitude: float,
    right_longitude: float,
) -> float:
    lat1 = math.radians(float(left_latitude))
    lat2 = math.radians(float(right_latitude))
    delta_lat = lat2 - lat1
    delta_lng = math.radians(float(right_longitude) - float(left_longitude))
    value = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lng / 2) ** 2
    )
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(value))


def _anchor_distance_km(
    place: CandidatePlace,
    coord: tuple[float, float] | None,
) -> float:
    if coord is None or place.latitude is None or place.longitude is None:
        return math.inf
    return _haversine_coords(
        coord[0],
        coord[1],
        place.latitude,
        place.longitude,
    )


def _distance_to_center(
    place: CandidatePlace,
    center: tuple[float, float],
) -> float:
    if not _has_coordinates(place):
        return math.inf
    return _haversine_coords(
        float(place.latitude),
        float(place.longitude),
        center[0],
        center[1],
    )


def _remote_name_group(
    place: CandidatePlace,
    *,
    context_text: str = "",
) -> str | None:
    for group, aliases in REMOTE_NAME_GROUP_ALIASES.items():
        required_context = REMOTE_NAME_GROUP_CONTEXT.get(group)
        context_matches = (
            not required_context
            or required_context in context_text
            or required_context in place.name
        )
        if context_matches and any(alias in place.name for alias in aliases):
            return group
    return None


def _estimate_urban_core_center(
    places: list[CandidatePlace],
) -> tuple[float, float] | None:
    located = [place for place in places if _has_coordinates(place)]
    if len(located) < 3:
        return None

    def neighbor_count(place: CandidatePlace) -> int:
        return sum(
            1 for other in located
            if other.place_id != place.place_id
            and haversine_km(place, other) <= URBAN_CORE_NEIGHBOR_KM
        )

    core_sample = sorted(
        located,
        key=lambda place: (
            -neighbor_count(place),
            -place.effective_score,
            -place.recommend_score,
            place.place_id,
        ),
    )[:URBAN_CORE_SAMPLE_SIZE]
    if not core_sample:
        return None
    return (
        sum(float(place.latitude) for place in core_sample) / len(core_sample),
        sum(float(place.longitude) for place in core_sample) / len(core_sample),
    )


def _remote_clusters(
    remote_places: list[CandidatePlace],
) -> list[list[CandidatePlace]]:
    clusters: list[list[CandidatePlace]] = []
    for place in sorted(
        remote_places,
        key=lambda candidate: (
            -candidate.effective_score,
            -candidate.recommend_score,
            candidate.place_id,
        ),
    ):
        matched_cluster = None
        for cluster in clusters:
            if any(
                haversine_km(place, member) <= REMOTE_CLUSTER_DISTANCE_KM
                for member in cluster
            ):
                matched_cluster = cluster
                break
        if matched_cluster is None:
            clusters.append([place])
        else:
            matched_cluster.append(place)

    return sorted(
        clusters,
        key=lambda cluster: (
            -len(cluster),
            -max(place.effective_score for place in cluster),
            min(place.place_id for place in cluster),
        ),
    )


def _split_remote_day_trips(
    candidates: list[CandidatePlace],
    *,
    days: int,
    remote_context: str = "",
    accommodation_coord: tuple[float, float] | None = None,
) -> tuple[list[CandidatePlace], list[tuple[str | None, list[CandidatePlace]]]]:
    """Split far-away clusters into exclusive day-trip chunks.

    v0.6.4 minimal remote rule:
    - points far from the estimated urban core are not mixed into urban days;
    - isolated remote points are dropped by omission;
    - valid remote clusters take at most one day for <=3-day trips.
    """
    if days < 2:
        return candidates, []

    center = _estimate_urban_core_center(candidates)
    if center is None:
        return candidates, []

    distance_remote_places = [
        place for place in candidates
        if _has_coordinates(place)
        and _distance_to_center(place, center) > REMOTE_CENTER_DISTANCE_KM
    ]
    name_remote_places_by_group: dict[str, list[CandidatePlace]] = defaultdict(list)
    for place in candidates:
        group = _remote_name_group(place, context_text=remote_context)
        if group is not None:
            name_remote_places_by_group[group].append(place)

    name_remote_places = [
        place
        for places in name_remote_places_by_group.values()
        for place in places
    ]
    remote_places_by_id = {
        place.place_id: place
        for place in [*distance_remote_places, *name_remote_places]
    }
    if not remote_places_by_id:
        return candidates, []

    name_remote_ids = {
        place.place_id
        for place in name_remote_places
    }
    name_clusters = [
        places
        for places in name_remote_places_by_group.values()
        if len(places) >= 2
        and any(_is_daytime_activity(place) for place in places)
    ]
    distance_clusters = _remote_clusters([
        place
        for place in distance_remote_places
        if place.place_id not in name_remote_ids
    ])
    clusters = sorted(
        [
            _nearest_neighbor(cluster, accommodation_coord)
            for cluster in [*name_clusters, *distance_clusters]
            if len(cluster) >= 2
            and any(_is_daytime_activity(place) for place in cluster)
        ],
        key=lambda cluster: (
            -len(cluster),
            -max(place.effective_score for place in cluster),
            min(place.place_id for place in cluster),
        ),
    )
    if not clusters:
        remote_ids = set(remote_places_by_id)
        return (
            [
                place for place in candidates
                if place.place_id not in remote_ids
            ],
            [],
        )

    max_remote_days = 1 if days <= 3 else min(2, days - 1)
    selected_clusters = clusters[:max_remote_days]
    selected_remote_ids = {
        place.place_id
        for cluster in selected_clusters
        for place in cluster
    }
    all_remote_ids = set(remote_places_by_id)
    urban_candidates = [
        place for place in candidates
        if place.place_id not in all_remote_ids
    ]
    remote_chunks = [
        (cluster[0].adcode if len({p.adcode for p in cluster}) == 1 else None, cluster)
        for cluster in selected_clusters
        if selected_remote_ids
    ]
    return urban_candidates, remote_chunks


def _within_budget(
    legs: list[CommuteLeg],
    *,
    daily_budget: int,
    single_leg_max: int,
) -> bool:
    return (
        all(leg.duration_minutes <= single_leg_max for leg in legs)
        and sum(leg.duration_minutes for leg in legs) <= daily_budget
    )


def _time_hints(
    places: list[CandidatePlace],
    request: TripRequest | None = None,
) -> list[str]:
    if _suppress_evening_markers(request):
        return []
    hints = []
    for place in places:
        if _is_evening_marked(place):
            hints.append(f"{place.name}适合安排在傍晚或夜间")
    return hints


ROUTE_NIGHT_HINT_PREFIX = "路线计划："


def _route_matches_dimension(place: CandidatePlace, dimension: str) -> bool:
    from src.agents.preferences import candidate_matches_dimension
    return candidate_matches_dimension(place, dimension)


def route_night_visit_intervals(day: RouteDayGroup, request: TripRequest | None) -> dict[int, tuple[int, int]]:
    """Schedule the day's bounded evening stop from the locked route, without provider I/O.

    Unknown windows and incomplete/stale physical inputs do not supply proof.
    Waiting and breaks consume the window; no member, order or load limit changes.
    """
    return _route_night_schedule(day, request)[0]


def route_night_gap(day: RouteDayGroup, request: TripRequest | None) -> str | None:
    """Why a day with an evening-stop candidate got no evening slot; None otherwise."""
    intervals, reason = _route_night_schedule(day, request)
    if intervals or _evening_stop_id(day.places, request) is None:
        return None
    return reason


def _evening_stop_id(places: list[CandidatePlace], request: TripRequest | None) -> int | None:
    """One evening stop per day: its last night-view place that may go after dark.

    Other night-view places stay ordinary visits. Stacking several into one
    evening overran the window and lost the whole day's arrangement (P3 S01).
    """
    if request is None or NIGHT_VIEW not in scoring_dimension_ids(request.preferences):
        return None
    stops = [place.place_id for place in places
             if _route_matches_dimension(place, NIGHT_VIEW) and _scheduled_evening_stop(place, request)]
    return stops[-1] if stops else None


def _route_night_schedule(
    day: RouteDayGroup, request: TripRequest | None,
) -> tuple[dict[int, tuple[int, int]], str | None]:
    if request is None or NIGHT_VIEW not in scoring_dimension_ids(request.preferences):
        return {}, "no_night_preference"
    policy = day.time_policy
    if policy is not None and policy.violations:
        return {}, "time_policy_violation"
    start = policy.start_minute if policy is not None else parse_hhmm(request.daily_start)
    end = policy.end_minute if policy is not None else parse_hhmm(request.daily_end)
    user_start, user_end = parse_hhmm(request.daily_start), parse_hhmm(request.daily_end)
    if user_start is not None:
        start = user_start if start is None else max(start, user_start)
    if user_end is not None:
        end = user_end if end is None else min(end, user_end)
    if start is None or end is None or not 0 <= start < end <= 1440:
        return {}, "window_unknown"
    ids = [place.place_id for place in day.places]
    feasibility = day.day_feasibility
    profiles = day.visit_profiles
    if (not ids or len(ids) != len(set(ids)) or feasibility is None or not feasibility.feasible
            or feasibility.ordered_place_ids != ids
            or [profile.place_id for profile in profiles] != ids
            or any(profile.arrangement != "standard" or profile.visit_minutes <= 0 for profile in profiles)
            or [(leg.from_place_id, leg.to_place_id) for leg in day.commute_legs] != list(zip(ids, ids[1:]))):
        return {}, "receipts_stale"
    if (sum(p.visit_minutes for p in profiles) != feasibility.visit_minutes
            or sum(p.uncertainty_minutes for p in profiles) != feasibility.uncertainty_minutes
            or sum(leg.duration_minutes for leg in day.commute_legs) != feasibility.poi_commute_minutes
            or feasibility.load_minutes != (
                feasibility.visit_minutes + feasibility.uncertainty_minutes
                + feasibility.poi_commute_minutes + (feasibility.access_minutes or 0)
                + feasibility.break_minutes)
            or feasibility.load_minutes > feasibility.load_limit_minutes):
        return {}, "receipts_stale"
    access = {leg.direction: leg for leg in day.access_legs}
    if not day.access_legs and feasibility.access_minutes not in (None, 0):
        return {}, "access_stale"
    if day.access_legs and (
        len(day.access_legs) != 2 or set(access) != {"outbound", "inbound"}
        or access["outbound"].place_id != ids[0] or access["inbound"].place_id != ids[-1]
        or len({(leg.anchor_source, leg.anchor_latitude, leg.anchor_longitude) for leg in day.access_legs}) != 1
        or sum(leg.duration_minutes for leg in day.access_legs) != feasibility.access_minutes
    ):
        return {}, "access_stale"
    breaks = list(policy.break_intervals) + list(policy.system_break_intervals) if policy is not None else []
    breaks += [(parse_hhmm(rest.start), parse_hhmm(rest.end)) for rest in request.rest_windows]
    if any(left is None or right is None or right <= left for left, right in breaks):
        return {}, "breaks_invalid"
    breaks = sorted((left, right) for left, right in breaks if left < end and start < right)

    def reserve(cursor, minutes):
        while cursor + minutes <= end:
            overlap = next((right for left, right in breaks if cursor < right and left < cursor + minutes), None)
            if overlap is None:
                return cursor, cursor + minutes
            cursor = overlap
        return None

    outbound = reserve(start, access["outbound"].duration_minutes if access else 0)
    if outbound is None:
        return {}, "outbound_past_window"
    cursor = outbound[1]
    intervals = {}
    evening_id = _evening_stop_id(day.places, request)
    for index, (place, profile) in enumerate(zip(day.places, profiles)):
        night = place.place_id == evening_id
        if intervals and not night and daytime_activity(place):
            return {}, "daytime_after_evening_stop"
        if night and 5 * 60 <= cursor < 18 * 60:
            cursor = 18 * 60
        interval = reserve(cursor, profile.visit_minutes + profile.uncertainty_minutes)
        if interval is None:
            return {}, "evening_stop_past_window" if night else "visit_past_window"
        if night:
            left, right = interval
            if not (right <= 5 * 60 or 18 * 60 <= left <= right < 1440):
                return {}, "evening_stop_outside_night"
            intervals[place.place_id] = interval
        cursor = interval[1]
        if index < len(day.commute_legs):
            transit = reserve(cursor, day.commute_legs[index].duration_minutes)
            if transit is None:
                return {}, "transit_past_window"
            cursor = transit[1]
    if reserve(cursor, access["inbound"].duration_minutes if access else 0) is None:
        return {}, "inbound_past_window"
    return intervals, None


def route_night_hint(place: CandidatePlace, interval: tuple[int, int]) -> str:
    def clock(minute):
        return f"{minute // 60:02d}:{minute % 60:02d}"
    return f"{ROUTE_NIGHT_HINT_PREFIX}{place.name}夜景{clock(interval[0])}–{clock(interval[1])}"


def _refresh_route_night_hints(day: RouteDayGroup, request: TripRequest) -> None:
    # Rebuild our receipts after membership, profile or actual traffic changes.
    intervals = route_night_visit_intervals(day, request)
    day.time_hints = [hint for hint in day.time_hints if not hint.startswith(ROUTE_NIGHT_HINT_PREFIX)]
    for place in day.places:
        if place.place_id in intervals:
            template = f"{place.name}适合安排在傍晚或夜间"
            day.time_hints = [hint for hint in day.time_hints if hint != template]
            day.time_hints.append(route_night_hint(place, intervals[place.place_id]))


def _is_evening_marked(place: CandidatePlace) -> bool:
    if place.place_type == "museum":
        return False  # closed by evening; a lit facade is not a visit
    evidence = " ".join(
        str(item.get("reason", ""))
        for item in [*place.top_reasons, *place.warnings]
        if isinstance(item, dict)
    )
    return any(
        marker in f"{place.name} {evidence}"
        for marker in _EVENING_MARKERS
    )


def _is_daytime_marked(place: CandidatePlace) -> bool:
    evidence = _DAYTIME_NEGATED.sub("", " ".join(
        str(item.get("reason", ""))
        for item in [*place.top_reasons, *place.warnings]
        if isinstance(item, dict)
    ))
    return any(
        marker in f"{place.name} {evidence}"
        for marker in _DAYTIME_MARKERS
    )


def _scheduled_evening_stop(place: CandidatePlace, request: TripRequest | None) -> bool:
    explicit_night = request is not None and NIGHT_VIEW in scoring_dimension_ids(request.preferences)
    # Keep existing evening-order constraints. Add eligible photo/night stops
    # to that group for an explicit preference; sorting alone is never proof.
    return (_is_evening_marked(place) or (
        explicit_night and _route_matches_dimension(place, NIGHT_VIEW)
    )) and not _is_daytime_marked(place)


def _is_daytime_activity(
    place: CandidatePlace,
    request: TripRequest | None = None,
) -> bool:
    return (
        not is_food_place(place)
        and (
            _is_daytime_marked(place)
            or not _is_evening_marked(place)
        )
    )


def _food_focused(preferences: list[str]) -> bool:
    return food_preference_active(preferences)


def _allows_food_in_core_route_slots(request: TripRequest) -> bool:
    """Food-focused trips can treat restaurants/snacks as route-core stops."""
    return _food_focused(request.preferences)


def _is_core_route_slot_candidate(
    place: CandidatePlace,
    *,
    request: TripRequest,
) -> bool:
    if place.place_type == "hotel":
        return False
    return _allows_food_in_core_route_slots(request) or not is_food_place(place)


def _shared_adcode(places: list[CandidatePlace]) -> str | None:
    adcodes = {place.adcode for place in places}
    return next(iter(adcodes)) if None not in adcodes and len(adcodes) == 1 else None


def _avoid_conflicts_with_place(
    request: TripRequest,
    place: CandidatePlace,
) -> bool:
    for avoid in request.avoid:
        marker = avoid.strip()
        if marker and marker in _route_quality_text(place):
            return True
    return False


def _selection_temporally_unschedulable(
    candidate: CandidatePlace,
    request: TripRequest,
) -> bool:
    return (
        _suppress_evening_markers(request)
        and _is_evening_marked(candidate)
        and not _is_daytime_marked(candidate)
    )


def _close_selection_ledger(
    plan: RoutePlan,
    *,
    selection: PoiSelectionResult,
    candidates_by_id: dict[int, CandidatePlace],
    supplement_reasons: dict[int, str],
    selected_drop_reasons: dict[int, str],
    request: TripRequest,
) -> RouteMembershipLedger:
    selected_ids = [item.place_id for item in selection.selected]
    selected_id_set = set(selected_ids)
    used_ids = {
        place.place_id
        for day_group in plan.day_groups
        for place in day_group.places
    }
    unknown_used_ids = used_ids - selected_id_set - set(supplement_reasons)
    if unknown_used_ids:
        raise RuntimeError(
            "route used places without a closed supplement predicate: "
            f"{sorted(unknown_used_ids)}"
        )
    wrongly_used_dropped_ids = used_ids.intersection(selected_drop_reasons)
    if wrongly_used_dropped_ids:
        raise RuntimeError(
            "route used selected places that deterministic admission dropped: "
            f"{sorted(wrongly_used_dropped_ids)}"
        )
    dispositions: list[SelectedRouteMembership] = []
    for place_id in selected_ids:
        if place_id in used_ids:
            dispositions.append(
                SelectedRouteMembership(place_id=place_id, status="USED")
            )
            continue
        reason = selected_drop_reasons.get(place_id)
        if reason is None:
            candidate = candidates_by_id[place_id]
            raise RuntimeError(
                "admitted selected place disappeared without an allowed predicate: "
                f"{candidate.place_id}"
            )
        dispositions.append(
            SelectedRouteMembership(
                place_id=place_id,
                status="DROPPED",
                reason=reason,
            )
        )
    ledger = RouteMembershipLedger(
        selected_place_ids=selected_ids,
        selected=dispositions,
        supplemented=[
            QualifiedRouteSupplement(place_id=place_id, reason=reason)
            for place_id, reason in supplement_reasons.items()
            if place_id in used_ids
        ],
    )
    plan.membership_ledger = ledger
    plan.dropped_place_ids = sorted(
        item.place_id for item in ledger.selected if item.status == "DROPPED"
    )
    return ledger


def refresh_selection_membership_ledger(
    plan: RoutePlan,
    *,
    selection: PoiSelectionResult,
    qualified_pool: list[CandidatePlace],
    request: TripRequest,
) -> RouteMembershipLedger:
    """Re-close the ledger after deterministic route/budget post-processing."""
    ledger = plan.membership_ledger
    if ledger is None:
        raise RuntimeError("selected route is missing membership ledger")
    return _close_selection_ledger(
        plan,
        selection=selection,
        candidates_by_id={candidate.place_id: candidate for candidate in qualified_pool},
        supplement_reasons={
            item.place_id: item.reason for item in ledger.supplemented
        },
        selected_drop_reasons={
            item.place_id: item.reason
            for item in ledger.selected
            if item.status == "DROPPED" and item.reason is not None
        },
        request=request,
    )


def _route_planning_candidate_pool(
    retrieval: RetrievalResult,
    *,
    must_include_ids: set[int] | None = None,
) -> list[CandidatePlace]:
    selected: dict[int, CandidatePlace] = {}
    for candidate in [
        *retrieval.candidates,
        *getattr(retrieval, "route_planning_candidates", []),
    ]:
        if candidate.place_id not in selected:
            selected[candidate.place_id] = candidate
    pool = list(selected.values())
    deduped, dropped = _dedupe_near_duplicate_places(
        pool, must_include_ids=must_include_ids,
    )
    if dropped:
        logger.info(
            "Pool-level near-duplicate dedup dropped %d places: %s",
            len(dropped),
            dropped,
        )
    return deduped


def valid_must_include_ids(
    request: TripRequest,
    retrieval: RetrievalResult,
) -> set[int]:
    """Return valid same-city route anchors already admitted upstream."""
    if not request.must_include:
        return set()
    return {
        candidate.place_id
        for candidate in retrieval.candidates
        if candidate.must_include
        and candidate.latitude is not None
        and candidate.longitude is not None
    }


def route_plan_place_ids(route_plan: RoutePlan) -> set[int]:
    return {
        place.place_id
        for day_group in route_plan.day_groups
        for place in day_group.places
    }


def route_must_include_metrics(
    request: TripRequest,
    retrieval: RetrievalResult,
    route_plan: RoutePlan,
) -> dict[str, Any]:
    anchor_ids = valid_must_include_ids(request, retrieval)
    scheduled = route_plan_place_ids(route_plan) & anchor_ids
    total = len(anchor_ids)
    ratio = (len(scheduled) / total) if total else 0.0
    return {
        "must_include_total": total,
        "must_include_scheduled": len(scheduled),
        "must_include_coverage_ratio": round(ratio, 4),
    }


def _route_quality_text(place: CandidatePlace) -> str:
    parts: list[str] = [
        place.name,
        place.place_type,
        *(place.category_tags or []),
    ]
    for item in [*place.top_reasons, *place.warnings]:
        if not isinstance(item, dict):
            continue
        for value in item.values():
            if value is not None:
                parts.append(str(value))
    return " ".join(parts)


def _contains_any(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker and marker in text for marker in markers)


def _nature_preference_active(request: TripRequest) -> bool:
    return nature_preference_active(request.preferences, notes=request.notes)


def _relaxed_preference_active(request: TripRequest) -> bool:
    text = " ".join([*request.preferences, *request.avoid, request.notes])
    return _contains_any(text, RELAXED_PREFERENCE_MARKERS)


def _route_place_count(route_plan: RoutePlan) -> int:
    return sum(len(day_group.places) for day_group in route_plan.day_groups)


def _place_type_preference_metrics(
    request: TripRequest,
    retrieval: RetrievalResult,
    route_plan: RoutePlan,
) -> dict[str, Any]:
    dimensions = scoring_dimension_ids(request.preferences)

    pool_by_id: dict[int, CandidatePlace] = {}
    for candidate in [
        *retrieval.candidates,
        *getattr(retrieval, "route_planning_candidates", []),
    ]:
        pool_by_id.setdefault(candidate.place_id, candidate)
    available_dimensions = {
        dimension
        for dimension in dimensions
        if any(
            _route_matches_dimension(candidate, dimension)
            and _has_coordinates(candidate)
            and _is_core_route_slot_candidate(candidate, request=request)
            for candidate in pool_by_id.values()
        )
    }
    matched_dimensions = {
        dimension
        for dimension in available_dimensions
        if any(
            _route_matches_dimension(place, dimension)
            for day_group in route_plan.day_groups
            for place in day_group.places
        )
    }
    missing_dimensions = available_dimensions - matched_dimensions
    unavailable_dimensions = set(dimensions) - available_dimensions
    coverage_ratio = (
        len(matched_dimensions) / len(available_dimensions)
        if available_dimensions
        else 0.0
    )
    return {
        "requested_place_type_preference_dimensions": sorted(dimensions),
        "available_place_type_preference_dimensions": sorted(available_dimensions),
        "matched_place_type_preference_dimensions": sorted(matched_dimensions),
        "missing_place_type_preference_dimensions": sorted(missing_dimensions),
        "unavailable_place_type_preference_dimensions": sorted(
            unavailable_dimensions
        ),
        "place_type_preference_coverage_ratio": round(coverage_ratio, 4),
        "place_type_preference_matched_count": len(matched_dimensions),
        "place_type_preference_available_count": len(available_dimensions),
        "place_type_preference_match": {
            f"place_type:{dimension}": dimension in matched_dimensions
            for dimension in sorted(available_dimensions)
        },
    }


def route_preference_match_metrics(
    request: TripRequest,
    retrieval: RetrievalResult,
    route_plan: RoutePlan,
) -> dict[str, Any]:
    nature_core_day_count = 0
    nature_core_place_count = 0
    for day_group in route_plan.day_groups:
        day_nature_count = sum(
            1 for place in day_group.places
            if _is_nature_core_place(place)
        )
        if day_nature_count:
            nature_core_day_count += 1
            nature_core_place_count += day_nature_count
    nature_required_days = 2 if request.days >= 3 else 1
    nature_active = _nature_preference_active(request)
    nature_match = (
        nature_core_day_count >= nature_required_days
        and nature_core_place_count > 0
    )
    places_per_day = [len(day_group.places) for day_group in route_plan.day_groups]
    relaxed_active = _relaxed_preference_active(request)
    relaxed_match = bool(
        places_per_day
        and max(places_per_day) <= 4
        and (sum(places_per_day) / len(places_per_day)) <= 4.0
    )
    place_type_metrics = _place_type_preference_metrics(
        request,
        retrieval,
        route_plan,
    )
    return {
        "preference_match": {
            "nature": bool(nature_active and nature_match),
            "relaxed": bool(relaxed_active and relaxed_match),
            **place_type_metrics["place_type_preference_match"],
        },
        "nature_core_day_count": nature_core_day_count,
        "nature_core_place_count": nature_core_place_count,
        **{
            key: value
            for key, value in place_type_metrics.items()
            if key != "place_type_preference_match"
        },
    }


def route_spatial_distribution_metrics(route_plan: RoutePlan) -> dict[str, Any]:
    place_type_distribution: dict[str, int] = defaultdict(int)
    adcode_distribution: dict[str, int] = defaultdict(int)
    for day_group in route_plan.day_groups:
        day_adcode = (day_group.adcode or "").strip()
        for place in day_group.places:
            place_type = (place.place_type or "unknown").strip() or "unknown"
            place_type_distribution[place_type] += 1
            adcode = day_adcode or (place.adcode or "").strip()
            if adcode:
                adcode_distribution[adcode] += 1
    place_count = _route_place_count(route_plan)
    max_adcode_count = max(adcode_distribution.values(), default=0)
    concentration = (max_adcode_count / place_count) if place_count else 0.0
    return {
        "place_type_distribution": dict(sorted(place_type_distribution.items())),
        "adcode_distribution": dict(sorted(adcode_distribution.items())),
        "adcode_count": len(adcode_distribution),
        "spatial_concentration_score": round(concentration, 4),
    }


def _round_km(value: float) -> float:
    if not math.isfinite(value):
        return 0.0
    return round(value, 4)


def _median_float(values: list[float]) -> float:
    finite_values = sorted(value for value in values if math.isfinite(value))
    if not finite_values:
        return 0.0
    middle = len(finite_values) // 2
    if len(finite_values) % 2:
        return finite_values[middle]
    return (finite_values[middle - 1] + finite_values[middle]) / 2


def _route_day_centers(route_plan: RoutePlan) -> list[tuple[float, float]]:
    centers: list[tuple[float, float]] = []
    for day_group in route_plan.day_groups:
        center = _day_center(day_group)
        if center is not None:
            centers.append(center)
    return centers


def _center_pair_distances(
    centers: list[tuple[float, float]],
) -> list[float]:
    return [
        _haversine_coords(left[0], left[1], right[0], right[1])
        for left, right in itertools.combinations(centers, 2)
    ]


def _route_located_places(route_plan: RoutePlan) -> list[CandidatePlace]:
    return [
        place
        for day_group in route_plan.day_groups
        for place in day_group.places
        if _has_coordinates(place)
    ]


def _coordinates_center(
    coordinates: list[tuple[float, float]],
) -> tuple[float, float] | None:
    if not coordinates:
        return None
    return (
        sum(latitude for latitude, _ in coordinates) / len(coordinates),
        sum(longitude for _, longitude in coordinates) / len(coordinates),
    )


def _coordinates_radius_km(
    coordinates: list[tuple[float, float]],
) -> float:
    center = _coordinates_center(coordinates)
    if center is None:
        return 0.0
    return max(
        (
            _haversine_coords(latitude, longitude, center[0], center[1])
            for latitude, longitude in coordinates
        ),
        default=0.0,
    )


def _day_radius_km(day_group: RouteDayGroup) -> float:
    center = _day_center(day_group)
    if center is None:
        return 0.0
    return max(
        (
            _distance_to_center(place, center)
            for place in day_group.places
            if _has_coordinates(place)
        ),
        default=0.0,
    )


def _distinct_spatial_day_count(
    centers: list[tuple[float, float]],
) -> int:
    clusters: list[tuple[float, float]] = []
    for center in centers:
        if all(
            _haversine_coords(
                center[0],
                center[1],
                cluster[0],
                cluster[1],
            ) >= MULTI_DAY_SAME_AREA_CENTER_KM
            for cluster in clusters
        ):
            clusters.append(center)
    return len(clusters)


def _valid_anchor_coordinates(
    retrieval: RetrievalResult,
    anchor_ids: set[int],
) -> list[tuple[int, float, float]]:
    seen: set[int] = set()
    coordinates: list[tuple[int, float, float]] = []
    for candidate in retrieval.candidates:
        if (
            candidate.place_id not in anchor_ids
            or candidate.place_id in seen
            or not _has_coordinates(candidate)
        ):
            continue
        seen.add(candidate.place_id)
        coordinates.append((
            candidate.place_id,
            float(candidate.latitude),
            float(candidate.longitude),
        ))
    return coordinates


def _anchor_forced_cluster(
    request: TripRequest,
    retrieval: RetrievalResult,
    route_plan: RoutePlan,
) -> bool:
    if request.days < 3:
        return False
    anchor_ids = valid_must_include_ids(request, retrieval)
    if len(anchor_ids) < 2:
        return False
    if not (route_plan_place_ids(route_plan) & anchor_ids):
        return False
    anchor_coordinates = _valid_anchor_coordinates(retrieval, anchor_ids)
    if len(anchor_coordinates) < 2:
        return False
    distances = _center_pair_distances([
        (latitude, longitude)
        for _, latitude, longitude in anchor_coordinates
    ])
    return bool(distances) and max(distances) < MULTI_DAY_SAME_AREA_CENTER_KM


def _spatial_spread_gate(
    request: TripRequest,
    centers: list[tuple[float, float]],
    *,
    min_distance: float,
) -> str:
    if request.days < 3 or len(centers) < 3:
        return "not_applicable"
    if min_distance < MULTI_DAY_SPREAD_GATE_HARD_MIN_CENTER_KM:
        return "fail_near_centers"
    if min_distance < MULTI_DAY_SPREAD_GATE_PASS_MIN_CENTER_KM:
        return "soft_near_centers"
    return "pass"


def route_multi_day_spread_metrics(
    request: TripRequest,
    retrieval: RetrievalResult,
    route_plan: RoutePlan,
) -> dict[str, Any]:
    centers = _route_day_centers(route_plan)
    center_distances = _center_pair_distances(centers)
    finite_distances = [
        distance for distance in center_distances
        if math.isfinite(distance)
    ]
    min_distance = min(finite_distances, default=0.0)
    median_distance = _median_float(finite_distances)
    same_area_pairs = sum(
        1 for distance in finite_distances
        if distance < MULTI_DAY_SAME_AREA_CENTER_KM
    )
    distinct_days = _distinct_spatial_day_count(centers)
    day_radii = [
        _day_radius_km(day_group)
        for day_group in route_plan.day_groups
        if _day_center(day_group) is not None
    ]
    avg_day_radius = (
        sum(day_radii) / len(day_radii)
        if day_radii
        else 0.0
    )
    overall_radius = _coordinates_radius_km([
        (float(place.latitude), float(place.longitude))
        for place in _route_located_places(route_plan)
        if place.latitude is not None and place.longitude is not None
    ])
    anchor_cluster = _anchor_forced_cluster(request, retrieval, route_plan)
    spread_gate = _spatial_spread_gate(
        request,
        centers,
        min_distance=min_distance,
    )

    route_shape = "moderate"
    if request.days >= 3 and len(centers) >= 3:
        if (
            same_area_pairs >= 2
            and overall_radius <= MULTI_DAY_ONE_CLUSTER_RADIUS_KM
        ):
            route_shape = "one_cluster_split"
        elif (
            spread_gate == "pass"
            and distinct_days >= min(request.days, len(route_plan.day_groups), 3)
            and median_distance >= MULTI_DAY_SPREAD_MEDIAN_CENTER_KM
            and overall_radius >= MULTI_DAY_SPREAD_MIN_CENTER_KM
        ):
            route_shape = "spread"

    spread_score = 0.0
    if request.days >= 3 and centers:
        scored_day_count = max(min(request.days, len(route_plan.day_groups)), 1)
        distinct_component = min(distinct_days / scored_day_count, 1.0) * 30.0
        min_distance_component = (
            min(min_distance, MULTI_DAY_SCORE_MAX_MIN_DISTANCE_KM)
            / MULTI_DAY_SCORE_MAX_MIN_DISTANCE_KM
        ) * 25.0
        median_distance_component = (
            min(median_distance, MULTI_DAY_SCORE_MAX_MEDIAN_DISTANCE_KM)
            / MULTI_DAY_SCORE_MAX_MEDIAN_DISTANCE_KM
        ) * 25.0
        overall_radius_component = (
            min(overall_radius, MULTI_DAY_SCORE_MAX_OVERALL_RADIUS_KM)
            / MULTI_DAY_SCORE_MAX_OVERALL_RADIUS_KM
        ) * 20.0
        day_radius_penalty = min(avg_day_radius, 6.0) / 6.0 * 8.0
        spread_score = (
            distinct_component
            + min_distance_component
            + median_distance_component
            + overall_radius_component
            - day_radius_penalty
        )
        if route_shape == "spread":
            spread_score += 8.0
        if spread_gate == "fail_near_centers" and not anchor_cluster:
            spread_score -= MULTI_DAY_SPREAD_GATE_HARD_PENALTY
            spread_score = min(spread_score, MULTI_DAY_SPREAD_GATE_HARD_SCORE_CAP)
        elif spread_gate == "soft_near_centers" and not anchor_cluster:
            spread_score -= MULTI_DAY_SPREAD_GATE_SOFT_PENALTY
            spread_score = min(spread_score, MULTI_DAY_SPREAD_GATE_SOFT_SCORE_CAP)
        if route_shape == "one_cluster_split":
            if anchor_cluster:
                spread_score = max(spread_score, 45.0)
            else:
                spread_score = min(spread_score, 18.0)
        spread_score = max(0.0, min(spread_score, 100.0))

    return {
        "day_center_distance_km_min": _round_km(min_distance),
        "day_center_distance_km_median": _round_km(median_distance),
        "same_area_day_pair_count": same_area_pairs,
        "distinct_spatial_day_count": distinct_days,
        "avg_day_radius_km": _round_km(avg_day_radius),
        "overall_radius_km": _round_km(overall_radius),
        "route_shape_class": route_shape,
        "multi_day_spread_score": round(spread_score, 4),
        "spatial_spread_gate": spread_gate,
        "anchor_forced_cluster": anchor_cluster,
    }


def _route_commute_minutes(route_plan: RoutePlan) -> int:
    return sum(int(day_group.commute_minutes or 0) for day_group in route_plan.day_groups)


def _route_internal_theme(
    route_plan: RoutePlan,
    candidate_groups_by_label: dict[str, CandidateGroup],
) -> str:
    group = candidate_groups_by_label.get(route_plan.label)
    if group is not None:
        return group.internal_theme
    if route_plan.label == "fallback-unused-candidates":
        return "未占用候选兜底路线"
    if route_plan.label == "fallback-all-candidates":
        return "全量候选兜底路线"
    if route_plan.label == "anchor-aware-all-candidates":
        return "有效必选锚点候选路线"
    if route_plan.label == MULTI_AREA_ROUTE_LABEL:
        return "多片区骨架候选路线"
    return ""


def build_route_quality_candidate_metrics(
    request: TripRequest,
    retrieval: RetrievalResult,
    plans: list[RoutePlan],
) -> list[dict[str, Any]]:
    candidate_groups_by_label = {
        group.label: group
        for group in retrieval.candidate_groups
    }
    candidates: list[dict[str, Any]] = []
    for route_plan in plans:
        metrics = {
            "label": route_plan.label,
            "internal_theme": _route_internal_theme(
                route_plan,
                candidate_groups_by_label,
            ),
            "complete": _is_complete_route_plan(route_plan, request),
            "day_count": len(route_plan.day_groups),
            "place_count": _route_place_count(route_plan),
            "commute_minutes": _route_commute_minutes(route_plan),
        }
        metrics.update(route_must_include_metrics(request, retrieval, route_plan))
        metrics.update(route_preference_match_metrics(request, retrieval, route_plan))
        metrics.update(route_spatial_distribution_metrics(route_plan))
        metrics.update(route_multi_day_spread_metrics(request, retrieval, route_plan))
        candidates.append(metrics)
    return candidates


def _legacy_selected_route_quality_label(
    request: TripRequest,
    plans: list[RoutePlan],
) -> str:
    selected = next(
        (plan for plan in plans if _is_complete_route_plan(plan, request)),
        plans[0] if plans else None,
    )
    return selected.label if selected is not None else ""


def _route_quality_selected_candidate(
    candidates: list[dict[str, Any]],
    selected_label: str,
) -> dict[str, Any] | None:
    if selected_label:
        selected = next(
            (
                candidate for candidate in candidates
                if candidate.get("label") == selected_label
            ),
            None,
        )
        if selected is not None:
            return selected
    return next(
        (candidate for candidate in candidates if candidate.get("complete")),
        candidates[0] if candidates else None,
    )


def build_route_quality_metrics(
    request: TripRequest,
    retrieval: RetrievalResult,
    plans: list[RoutePlan],
    *,
    selected_label: str | None = None,
    previous_selected_label: str | None = None,
    selected_reason: str = "legacy_order",
    rank_components: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    candidates = build_route_quality_candidate_metrics(request, retrieval, plans)
    resolved_selected_label = (
        selected_label
        if selected_label is not None
        else _legacy_selected_route_quality_label(request, plans)
    )
    selected = _route_quality_selected_candidate(candidates, resolved_selected_label)
    selected_label = str(selected.get("label")) if selected else ""
    selected_must_include_total = int(
        selected.get("must_include_total", 0)
        if selected
        else 0
    )
    selected_must_include_scheduled = int(
        selected.get("must_include_scheduled", 0)
        if selected
        else 0
    )
    selected_must_include_ratio = float(
        selected.get("must_include_coverage_ratio", 0.0)
        if selected
        else 0.0
    )
    metrics = {
        "selected_label": selected_label,
        "previous_selected_label": (
            previous_selected_label
            if previous_selected_label is not None
            else selected_label
        ),
        "selected_reason": selected_reason,
        "must_include_total": selected_must_include_total,
        "selected_must_include_scheduled": selected_must_include_scheduled,
        "selected_must_include_coverage_ratio": selected_must_include_ratio,
        "selected_route_shape_class": (
            selected.get("route_shape_class", "")
            if selected
            else ""
        ),
        "selected_multi_day_spread_score": (
            selected.get("multi_day_spread_score", 0.0)
            if selected
            else 0.0
        ),
        "selected_day_center_distance_km_min": (
            selected.get("day_center_distance_km_min", 0.0)
            if selected
            else 0.0
        ),
        "selected_day_center_distance_km_median": (
            selected.get("day_center_distance_km_median", 0.0)
            if selected
            else 0.0
        ),
        "selected_spatial_spread_gate": (
            selected.get("spatial_spread_gate", "")
            if selected
            else ""
        ),
        "route_quality_candidates": candidates,
    }
    if rank_components is not None:
        metrics["rank_components"] = rank_components
    return metrics


async def _load_citycode(city: str, adcodes: set[str]) -> str | None:
    """Resolve one canonical transit citycode for the whole workflow."""
    async with get_session_factory()() as session:
        row = (await session.execute(
            text("""
                SELECT citycode
                FROM travel_amap_district
                WHERE NULLIF(BTRIM(citycode), '') IS NOT NULL
                  AND (
                      REPLACE(name, '市', '') = REPLACE(:city, '市', '')
                      OR adcode = ANY(CAST(:adcodes AS text[]))
                  )
                ORDER BY
                    CASE level
                        WHEN 'city' THEN 0
                        WHEN 'province' THEN 1
                        ELSE 2
                    END,
                    adcode
                LIMIT 1
            """),
            {"city": city, "adcodes": sorted(adcodes)},
        )).one_or_none()
    return str(row.citycode).strip() if row is not None else None


def _is_complete_route_plan(route_plan: RoutePlan, request: TripRequest) -> bool:
    return bool(route_plan.day_groups) and len(route_plan.day_groups) == request.days


async def _preload_precise_route_cache(
    provider: RouteProvider,
    plans: list[RoutePlan],
    *,
    city: str,
    generation_mode: EffectiveCommuteMode,
) -> None:
    preloader = getattr(provider, "preload_cache_for_plans", None)
    if preloader is None:
        return
    maybe = preloader(
        plans,
        city=city,
        generation_mode=generation_mode,
    )
    if asyncio.iscoroutine(maybe):
        await maybe


async def _resolve_precise_legs(
    day_group: RouteDayGroup,
    estimated_legs: list[CommuteLeg],
    *,
    city: str,
    citycode: str | None,
    generation_mode: EffectiveCommuteMode,
    requested_commute_mode: str,
    daily_budget: int,
    single_leg_max: int,
    provider: RouteProvider,
    metrics: RoutePlanningMetrics | None,
    known_precise_legs: dict[tuple[int, int, str], CommuteLeg] | None = None,
    concurrency: int,
    timeout_seconds: float,
    walk_proof: bool = True,
    record_leg_metrics: bool = True,
    whole_day_fallback: bool = True,
) -> list[CommuteLeg]:
    if not estimated_legs:
        return []
    semaphore = asyncio.Semaphore(concurrency)

    def record_final(leg: CommuteLeg, *, fallback: bool = False) -> None:
        if metrics is None or not record_leg_metrics:
            return
        metrics.record_effective_leg(
            leg.mode,
            rationalized=leg.mode != generation_mode,
        )
        if fallback:
            metrics.record_effective_fallback(leg.mode)

    def remember_receipt(leg: CommuteLeg) -> CommuteLeg:
        if metrics is None:
            return leg
        reason = _walk_reason_of(leg)
        if reason:
            metrics.record_walk_proof(
                reason,
                from_place_id=leg.from_place_id,
                to_place_id=leg.to_place_id,
            )
        return leg

    async def fetch(index: int, estimated: CommuteLeg):
        origin = day_group.places[index]
        destination = day_group.places[index + 1]
        precise_key = (origin.place_id, destination.place_id, estimated.mode)
        walking_key = (origin.place_id, destination.place_id, "walking")
        prove = walk_proof and _transit_walk_proof_applies(
            origin,
            destination,
            generation_mode=generation_mode,
            estimated_mode=estimated.mode,
        )
        walk_reason = None
        cached_walk = (
            known_precise_legs.get(walking_key)
            if known_precise_legs is not None
            else None
        )
        if prove and isinstance(cached_walk, CommuteLeg) and _adopted_walk_commute(cached_walk):
            return index, remember_receipt(cached_walk.model_copy(deep=True)), None
        if prove:
            walk_reason = _cached_walk_reject_reason(
                known_precise_legs, origin.place_id, destination.place_id,
            )
            if walk_reason is None and known_precise_legs is not None:
                cached_precise = known_precise_legs.get(precise_key)
                if _known_success_commute(cached_precise):
                    walk_reason = _negative_walk_reason(
                        _walk_reason_of(cached_precise)
                    )
            if walk_reason is None:
                async with semaphore:
                    try:
                        walking_precise = await provider.route(
                            origin=origin,
                            destination=destination,
                            city=city,
                            effective_mode="walking",
                            citycode=citycode,
                        )
                    except RouteProviderBudgetExceededError as exc:
                        reason = _walk_proof_error_reason(exc)
                        _remember_walk_proof_decision(
                            known_precise_legs, estimated, reason,
                        )
                        return index, remember_receipt(
                            _with_walk_decision(estimated, reason)
                        ), exc
                    except RouteProviderRateLimitError as exc:
                        reason = _walk_proof_error_reason(exc)
                        _remember_walk_proof_decision(
                            known_precise_legs, estimated, reason,
                        )
                        return index, remember_receipt(
                            _with_walk_decision(estimated, reason)
                        ), exc
                    except RouteProviderError as exc:
                        walk_reason = _walk_proof_error_reason(exc)
                        _remember_walk_proof_decision(
                            known_precise_legs, estimated, walk_reason,
                        )
                    else:
                        if not _valid_precise_route_values(
                            walking_precise.distance_meters,
                            walking_precise.duration_minutes,
                        ):
                            walk_reason = WALK_PROOF_INVALID
                            _remember_walk_proof_decision(
                                known_precise_legs, estimated, walk_reason,
                            )
                        elif not _walk_proof_duration_ok(
                            walking_precise.duration_minutes,
                        ):
                            walk_reason = WALK_PROOF_OVER_DURATION
                            _remember_walk_proof_decision(
                                known_precise_legs, estimated, walk_reason,
                            )
                        else:
                            resolved = _precise_commute_leg(
                                origin,
                                destination,
                                walking_precise,
                                mode="walking",
                                requested_commute_mode=requested_commute_mode,
                                walk_proof_decision=WALK_PROOF_ADOPTED,
                            )
                            if known_precise_legs is not None:
                                known_precise_legs[walking_key] = (
                                    resolved.model_copy(deep=True)
                                )
                            return index, remember_receipt(resolved), None
        if known_precise_legs is not None:
            cached = known_precise_legs.get(precise_key)
            if _known_success_commute(cached):
                return index, remember_receipt(
                    _with_walk_decision(cached.model_copy(deep=True), walk_reason)
                ), None
        if estimated.mode == "transit" and not citycode:
            if metrics is not None:
                metrics.amap_missing_citycode_count += 1
            return (
                index,
                remember_receipt(_with_walk_decision(estimated, walk_reason)),
                _MissingTransitCitycodeError("transit route requires citycode"),
            )
        async with semaphore:
            try:
                precise = await provider.route(
                    origin=origin,
                    destination=destination,
                    city=city,
                    effective_mode=estimated.mode,
                    citycode=citycode,
                )
            except RouteProviderError as exc:
                return index, remember_receipt(
                    _with_walk_decision(estimated, walk_reason)
                ), exc
        resolved = _precise_commute_leg(
            origin,
            destination,
            precise,
            mode=estimated.mode,
            requested_commute_mode=requested_commute_mode,
            walk_proof_decision=walk_reason,
        )
        if known_precise_legs is not None:
            known_precise_legs[precise_key] = resolved.model_copy(deep=True)
        return index, remember_receipt(resolved), None

    tasks = [
        asyncio.create_task(fetch(index, estimated))
        for index, estimated in enumerate(estimated_legs)
    ]
    try:
        results = await asyncio.wait_for(
            asyncio.gather(*tasks),
            timeout=timeout_seconds,
        )
    except asyncio.TimeoutError:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        stamped = [
            remember_receipt(
                _estimate_with_cached_walk_reason(estimated, known_precise_legs)
            )
            for estimated in estimated_legs
        ]
        if not whole_day_fallback:
            return stamped
        logger.warning(
            "Amap route enrichment timed out, using estimates for day legs"
        )
        if metrics is not None:
            metrics.amap_fallback_count += len(stamped)
            for leg in stamped:
                record_final(leg, fallback=True)
        return stamped

    budget_errors = [
        error
        for _, _, error in results
        if isinstance(error, RouteProviderBudgetExceededError)
    ]
    successful_precise_legs = [
        leg
        for _, leg, error in results
        if error is None and leg.source == "amap"
    ]
    precise_hard_violation = (
        any(
            leg.duration_minutes > single_leg_max
            for leg in successful_precise_legs
        )
        or sum(
            max(0, leg.duration_minutes)
            for leg in successful_precise_legs
        ) > daily_budget
    )
    if budget_errors and not precise_hard_violation and whole_day_fallback:
        logger.warning(
            "Amap route provider budget exhausted; using consistent "
            "estimates for the whole day: %s",
            budget_errors[0],
        )
        stamped = [
            remember_receipt(
                _estimate_with_cached_walk_reason(estimated, known_precise_legs)
            )
            for estimated in estimated_legs
        ]
        if metrics is not None:
            metrics.amap_provider_budget_exhausted_count += 1
            metrics.record_precise_recovery_reason("provider_budget_exhausted")
            metrics.amap_fallback_count += len(stamped)
            for leg in stamped:
                record_final(leg, fallback=True)
        return stamped

    precise_legs: list[CommuteLeg] = []
    budget_exhaustion_recorded = False
    rate_limit_recorded = False
    for index, leg, error in sorted(results, key=lambda item: item[0]):
        if isinstance(error, _MissingTransitCitycodeError):
            if metrics is not None:
                metrics.amap_fallback_count += 1
            record_final(leg, fallback=True)
            precise_legs.append(leg)
            continue
        if isinstance(error, RouteProviderBudgetExceededError):
            if not budget_exhaustion_recorded:
                logger.warning(
                    "Amap route provider budget exhausted after a proven "
                    "precise hard violation; preserving mixed evidence: %s",
                    error,
                )
            if metrics is not None:
                if not budget_exhaustion_recorded:
                    metrics.amap_provider_budget_exhausted_count += 1
                    metrics.record_precise_recovery_reason(
                        "provider_budget_exhausted"
                    )
                metrics.amap_fallback_count += 1
            record_final(leg, fallback=True)
            budget_exhaustion_recorded = True
            precise_legs.append(leg)
            continue
        if isinstance(error, RouteProviderRateLimitError):
            if not rate_limit_recorded:
                logger.warning(
                    "Amap route API rate-limited, using estimates for remaining legs: %s",
                    error,
                )
            if metrics is not None:
                if not rate_limit_recorded:
                    metrics.amap_provider_rate_limit_fallback_count += 1
                metrics.amap_fallback_count += 1
            record_final(leg, fallback=True)
            rate_limit_recorded = True
            precise_legs.append(leg)
            continue
        if isinstance(error, RouteProviderError):
            logger.warning(
                "Amap route API failed, using distance estimate: %s",
                error,
            )
            if metrics is not None:
                metrics.amap_provider_error_fallback_count += 1
                metrics.amap_fallback_count += 1
            record_final(leg, fallback=True)
            precise_legs.append(leg)
            continue
        record_final(leg)
        precise_legs.append(leg)
    return precise_legs


_ROUTE_WORKFLOW_RESERVE_SECONDS = 110.0


def route_workflow_reserve_seconds() -> float:
    """Seconds the route stage leaves for writing and publishing."""
    return _ROUTE_WORKFLOW_RESERVE_SECONDS


def _v2_access_key(leg, place):
    return (leg.direction, leg.anchor_source, leg.anchor_latitude, leg.anchor_longitude,
            place.place_id, place.latitude, place.longitude, leg.mode)


def _v2_access_pair_key(leg, place):
    return (leg.direction, leg.anchor_source, leg.anchor_latitude, leg.anchor_longitude,
            place.place_id, place.latitude, place.longitude)


def _v2_access_endpoints(leg, place):
    anchor = CandidatePlace(place_id=0, name="住宿参考点", place_type="hotel",
                            latitude=leg.anchor_latitude, longitude=leg.anchor_longitude)
    return (anchor, place) if leg.direction == "outbound" else (place, anchor)


def _v2_transport_plan(plan):
    """Provider-only query graph; never used as itinerary membership or persisted."""
    days = list(plan.day_groups)
    for day in plan.day_groups:
        by_id = {p.place_id: p for p in day.places}
        for access in day.access_legs:
            days.append(RouteDayGroup(day=len(days)+1,
                places=list(_v2_access_endpoints(access, by_id[access.place_id]))))
    return RoutePlan(label="provider-transport-queries", day_groups=days)


async def _v2_resolve_access(plan, provider, known_access, attempted, *, city, citycode, deadline,
                             generation_mode=None, walk_proof=True, walk_rejections=None):
    resolver = getattr(provider, "route_access", None)
    if not callable(resolver):
        return
    # Keep rejected proofs across candidate rebuilds, separate from successful
    # provider facts. The caller shares this map only within one route request.
    walk_rejections = {} if walk_rejections is None else walk_rejections
    settings = get_settings()
    semaphore = asyncio.Semaphore(max(1, settings.amap_route_enrichment_concurrency))
    deadline_exhausted = asyncio.Event()
    if generation_mode is None:
        generation_mode = next((leg.mode for day in plan.day_groups for leg in day.access_legs), None)
    call_timeout = max(1, settings.amap_route_timeout)

    def walking_key_for(leg, place):
        return _v2_access_key(leg.model_copy(update={"mode": "walking"}), place)

    def prove_walk(leg, place):
        if not walk_proof:
            return False
        origin, destination = _v2_access_endpoints(leg, place)
        return (
            (generation_mode or leg.mode) == "transit"
            and leg.mode == "transit"
            and _transit_walk_proof_triggered(
                origin, destination, generation_mode="transit",
            )
        )

    def apply_targets(targets, precise):
        for day, index, _, _ in targets:
            day.access_legs[index] = precise.model_copy(deep=True)

    def stamp_targets(targets, reason):
        if not reason:
            return
        for day, index, current, place in targets:
            walk_rejections[walking_key_for(current, place)] = reason
            day.access_legs[index] = _stamp_walk_proof(current, reason)

    pending = {}
    for day in plan.day_groups:
        by_id = {p.place_id: p for p in day.places}
        for index, leg in enumerate(day.access_legs):
            place = by_id[leg.place_id]
            walking_key = walking_key_for(leg, place)
            transit_key = _v2_access_key(leg, place)
            prove = prove_walk(leg, place)
            if prove:
                adopted = known_access.get(walking_key)
                if isinstance(adopted, AccessLeg) and _adopted_walk_access(adopted):
                    day.access_legs[index] = adopted.model_copy(deep=True)
                    continue
            elif _known_success_access(known_access.get(transit_key)):
                day.access_legs[index] = known_access[transit_key].model_copy(deep=True)
                continue
            pending.setdefault(_v2_access_pair_key(leg, place), []).append(
                (day, index, day.access_legs[index], place))

    async def query(origin, destination, mode, key):
        if (deadline_exhausted.is_set() or
                (deadline is not None and time.monotonic() >= deadline)):
            return None, "deadline"
        # Queued work must not consume an attempt or provider budget.
        attempted.add(key)
        remaining = max(.001, deadline - time.monotonic()) if deadline is not None else 60
        try:
            fact = await asyncio.wait_for(
                resolver(
                    origin=origin, destination=destination,
                    city=city, effective_mode=mode, citycode=citycode,
                ),
                timeout=min(remaining, call_timeout),
            )
            if not _valid_precise_route_values(fact.distance_meters, fact.duration_minutes):
                return None, WALK_PROOF_INVALID
            return fact, None
        except asyncio.TimeoutError:
            # A wall-bound timeout may wake just before the monotonic deadline.
            # Stop queued admission while retaining already completed facts.
            if deadline is not None and remaining <= call_timeout:
                deadline_exhausted.set()
            return None, WALK_PROOF_TIMEOUT
        except asyncio.CancelledError:
            raise
        except RouteProviderBudgetExceededError:
            return None, WALK_PROOF_BUDGET
        except RouteProviderRateLimitError:
            return None, WALK_PROOF_RATE_LIMITED
        except RouteProviderError as exc:
            return None, _walk_proof_error_reason(exc)

    async def fetch(pair_key, targets):
        async with semaphore:
            _, _, leg, place = targets[0]
            origin, destination = _v2_access_endpoints(leg, place)
            walking_key = walking_key_for(leg, place)
            transit_key = _v2_access_key(leg, place)
            prove = prove_walk(leg, place)
            walk_reason = walk_rejections.get(walking_key) if prove else None
            if prove:
                cached_walk = known_access.get(walking_key)
                if isinstance(cached_walk, AccessLeg) and _adopted_walk_access(cached_walk):
                    apply_targets(targets, cached_walk)
                    return
                cached_transit = known_access.get(transit_key)
                if _known_success_access(cached_transit):
                    walk_reason = walk_reason or _negative_walk_reason(_walk_reason_of(cached_transit))
                if walk_reason:
                    stamp_targets(targets, walk_reason)
                if walk_reason is None and walking_key not in attempted:
                    try:
                        fact, error = await query(
                            origin, destination, "walking", walking_key,
                        )
                    except asyncio.CancelledError:
                        stamp_targets(targets, WALK_PROOF_CANCELLED)
                        raise
                    if error == "deadline":
                        return
                    if error is None and _walk_proof_duration_ok(fact.duration_minutes):
                        precise = _stamp_walk_proof(leg.model_copy(update={
                            "mode": "walking",
                            "duration_source": "amap",
                            "duration_minutes": fact.duration_minutes,
                            "distance_meters": fact.distance_meters,
                        }), WALK_PROOF_ADOPTED)
                        known_access[walking_key] = precise
                        apply_targets(targets, precise)
                        return
                    walk_reason = (
                        WALK_PROOF_OVER_DURATION
                        if error is None
                        else error
                    )
                    stamp_targets(targets, walk_reason)
                    if walk_reason in {
                        WALK_PROOF_BUDGET,
                        WALK_PROOF_RATE_LIMITED,
                        WALK_PROOF_TIMEOUT,
                    }:
                        return
            if _known_success_access(known_access.get(transit_key)):
                current = known_access[transit_key].model_copy(deep=True)
                if walk_reason:
                    current = _stamp_walk_proof(current, walk_reason)
                    known_access[transit_key] = current
                apply_targets(targets, current)
                return
            if transit_key in attempted:
                if walk_reason:
                    stamp_targets(targets, walk_reason)
                return
            try:
                fact, error = await query(origin, destination, leg.mode, transit_key)
            except asyncio.CancelledError:
                if walk_reason or prove:
                    stamp_targets(targets, walk_reason or WALK_PROOF_CANCELLED)
                raise
            if error is None and fact is not None:
                precise = leg.model_copy(update={
                    "duration_source": "amap",
                    "duration_minutes": fact.duration_minutes,
                    "distance_meters": fact.distance_meters,
                })
                if walk_reason:
                    precise = _stamp_walk_proof(precise, walk_reason)
                known_access[transit_key] = precise
                apply_targets(targets, precise)
                return
            if error == "deadline":
                return
            if walk_reason:
                stamp_targets(targets, walk_reason)

    tasks = [
        asyncio.create_task(fetch(key, targets))
        for key, targets in pending.items()
    ]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        # Drain children before returning or propagating caller cancellation.
        await asyncio.gather(*tasks, return_exceptions=True)


def _v2_access_legs(places, request, accommodation_coord, accommodation_source, known_access=None):
    if not places or accommodation_coord is None or not valid_coordinate(*accommodation_coord):
        return []
    mode, _ = generation_base_mode(request, get_settings())
    result = []
    for direction, place in (("outbound", places[0]), ("inbound", places[-1])):
        # This copy is used for distance estimation only; provider queries use ID 0.
        anchor = place.model_copy(update={"latitude": accommodation_coord[0], "longitude": accommodation_coord[1]})
        left, right = (anchor, place) if direction == "outbound" else (place, anchor)
        effective = resolve_leg_effective_mode(left, right, generation_mode=mode)
        _, distance, minutes = _estimate_route_values(left, right, effective_mode=effective)
        leg = AccessLeg(direction=direction, anchor_source=accommodation_source,
                     anchor_latitude=accommodation_coord[0], anchor_longitude=accommodation_coord[1],
                     place_id=place.place_id, mode=effective, duration_minutes=minutes, distance_meters=distance)
        result.append(_lookup_known_access(
            leg, place, known_access, generation_mode=mode,
        ))
    return result


def _urban_night_time_policy(day: RouteDayGroup, request: TripRequest):
    """Freeze a planned ordinary-day window; never invent a POI opening fact.

    Explicit complete windows retain their existing path. When one is missing,
    shift the existing bounded day capacity to include the existing two-hour
    evening allowance. Physical visits, waiting and the return still must fit.
    """
    if (NIGHT_VIEW not in scoring_dimension_ids(request.preferences)
            or (request.daily_start is not None and request.daily_end is not None)
            or (day.traffic_policy is not None and day.traffic_policy.day_kind == "excursion")):
        return None
    if not any(_route_matches_dimension(p, NIGHT_VIEW) for p in day.places):
        return None
    from src.agents.route_feasibility import capacity_minutes
    from src.agents.pace import detect_pace_mode
    from src.agents.schema import ExcursionTimePolicy
    capacity = capacity_minutes(request, get_settings())
    reference_end = 18 * 60 + EVENING_ONLY_CAPACITY_MINUTES
    reference_start = reference_end - capacity
    user_start, user_end = parse_hhmm(request.daily_start), parse_hhmm(request.daily_end)
    start = max(reference_start, user_start) if user_start is not None else reference_start
    end = min(reference_end, user_end) if user_end is not None else reference_end
    intervals = sorted((max(start, parse_hhmm(rest.start)), min(end, parse_hhmm(rest.end)))
                       for rest in request.rest_windows
                       if max(start, parse_hhmm(rest.start)) < min(end, parse_hhmm(rest.end)))
    merged = []
    for left, right in intervals:
        if merged and left <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(right, merged[-1][1]))
        else:
            merged.append((left, right))
    return ExcursionTimePolicy(
        version="urban-night-time-v1", pace=detect_pace_mode(request), start_minute=start, end_minute=end,
        start_source="user" if user_start is not None and user_start >= reference_start else "system",
        end_source="user" if user_end is not None and user_end <= reference_end else "system",
        reference_span_minutes=capacity, reference_start_minute=reference_start,
        reference_end_minute=reference_end, original_daily_start=request.daily_start,
        original_daily_end=request.daily_end,
        original_rest_windows=[rest.model_dump() for rest in request.rest_windows],
        break_intervals=merged, break_minutes=sum(right-left for left,right in merged),
        violations=["schedule_conflict"] if end <= start else [],
    )


def _v2_finalize_day(day, request, *, locked=False):
    if not locked:
        if day.traffic_policy is not None:
            from src.agents.accommodation_policy import day_policy
            from src.agents.schema import AccommodationPlanningContext
            old = day.traffic_policy
            day.traffic_policy = day_policy(day.places, request, get_settings(),
                AccommodationPlanningContext(state=old.resolution_state, urban_core=old.urban_core), None, day.commute_legs)
            day.traffic_policy.location_precision = old.location_precision
        from src.agents.excursion_time_policy import freeze_time_policy
        day.time_policy = freeze_time_policy(request) if day.traffic_policy and day.traffic_policy.day_kind == "excursion" else None
        from src.agents.activity_arrangement import resolve_profiles, full_day_place
        if any(full_day_place(p) for p in day.places) and day.time_policy is None:
            day.time_policy = freeze_time_policy(request)
        if day.time_policy is None:
            day.time_policy = _urban_night_time_policy(day, request)
        day.visit_profiles, arrangement_violations = resolve_profiles(day.places,
            [build_visit_profile(p) for p in day.places], request, day.commute_legs, day.access_legs, day.time_policy, settings=get_settings())
    day.day_feasibility = evaluate_day(day.places, day.commute_legs, day.access_legs,
        DayContext(request, get_settings(), day.traffic_policy, day.time_policy, tuple(day.visit_profiles)))
    day.day_feasibility.violations = list(dict.fromkeys(day.day_feasibility.violations))
    day.commute_minutes = day.day_feasibility.poi_commute_minutes + (day.day_feasibility.access_minutes or 0)
    day.commute_notes = [leg.note for leg in day.commute_legs]
    summary = access_summary(day)
    if summary:
        day.commute_notes.append(summary)
    _refresh_route_night_hints(day, request)


def _v2_make_plan(groups, request, accommodation_coord, accommodation_source, known, coupling_context=None, anchor=None, known_access=None):
    mode, _ = generation_base_mode(request, get_settings())
    days = []
    for index, places in enumerate(groups, 1):
        legs = _legs(places, generation_mode=mode)
        legs = [
            _lookup_known_commute_leg(
                leg, known,
                origin=places[index],
                destination=places[index + 1],
                generation_mode=mode,
            )
            for index, leg in enumerate(legs)
        ]
        day = RouteDayGroup(day=index, places=[p.model_copy(deep=True) for p in places],
                            area=" / ".join(dict.fromkeys(p.district for p in places if p.district)),
                            adcode=_shared_adcode(places), commute_legs=legs,
                            access_legs=_v2_access_legs(places, request, accommodation_coord, accommodation_source, known_access),
                            time_hints=_time_hints(places, request))
        if coupling_context is not None:
            from src.agents.accommodation_policy import day_policy
            day.traffic_policy = day_policy(places, request, get_settings(), coupling_context, anchor)
        _v2_finalize_day(day, request)
        days.append(day)
    return RoutePlan(label="selected-route-v2", route_policy_version=POLICY_VERSION, day_groups=days,
        accommodation_policy_version=coupling_context.policy_version if coupling_context else None,
        accommodation_anchor=anchor.model_copy(deep=True) if anchor else None,
        accommodation_resolution_state=coupling_context.state if coupling_context else None)


def _selected_place_ids(selection) -> set[int]:
    return {int(item.place_id) for item in selection.selected}


def _coverage_violation_codes(plan_ids, request, coverage) -> list[str]:
    """Report a coverage gap. Omitted coverage keeps the legacy membership check.

    A supplied coverage dict with missing selection, frozen ids, hard ids, or
    place records is incomplete. Unknown input is not treated as covered.
    """
    if coverage is None:
        return []
    if not isinstance(coverage, dict):
        return ["coverage_context_incomplete"]
    selection = coverage.get("selection")
    if selection is None or any(
        key not in coverage or coverage[key] is None
        for key in ("frozen_ids", "hard_ids", "places_by_id")
    ) or not hasattr(selection, "selected"):
        return ["coverage_context_incomplete"]
    if not scoring_dimension_ids(request.preferences):
        return []
    places_by_id = coverage["places_by_id"]
    frozen = {int(place_id) for place_id in coverage["frozen_ids"]}
    selected = _selected_place_ids(selection)
    if any(place_id not in places_by_id for place_id in selected & frozen):
        return ["coverage_context_incomplete"]
    # Ordinary preference is a comparison input, not route infeasibility.
    return []


def _v2_plan_violations(plan, request, must_ids, eligible_ids, *, refresh=True, derive=False, coverage=None):
    ids = [p.place_id for day in plan.day_groups for p in day.places]
    violations = []
    if len(plan.day_groups) != request.days or [d.day for d in plan.day_groups] != list(range(1, request.days + 1)):
        violations.append("incomplete_days")
    if len(ids) != len(set(ids)) or not set(ids).issubset(eligible_ids):
        violations.append("invalid_membership")
    if not must_ids.issubset(ids):
        violations.append("missing_must_include")
    for day in plan.day_groups:
        if plan.accommodation_policy_version:
            if day.traffic_policy is None or day.traffic_policy.resolution_state != plan.accommodation_resolution_state:
                violations.append("accommodation_policy_mismatch")
            anchor = plan.accommodation_anchor
            if anchor is not None:
                expected = (anchor.suggestion.latitude, anchor.suggestion.longitude, anchor.suggestion.source)
                if len(day.access_legs) != 2 or any(
                    (leg.anchor_latitude, leg.anchor_longitude, leg.anchor_source) != expected for leg in day.access_legs
                ):
                    violations.append("locked_accommodation_mismatch")
            elif day.access_legs or plan.accommodation_resolution_state not in {"user_unresolved", "auto_unavailable"}:
                violations.append("missing_locked_accommodation")
        if refresh:
            _v2_finalize_day(day, request, locked=not derive)
        violations.extend(day.day_feasibility.violations)
        evening_seen = False
        for place in day.places:
            # A possible photo activity is not intrinsically evening-only.
            # Preserve the existing hard daytime-order guard; the bounded
            # night interval and L3 establish actual night fulfillment.
            evening_only = _is_evening_marked(place) and not _is_daytime_marked(place)
            if evening_seen and daytime_activity(place):
                violations.append("daytime_after_evening")
            evening_seen = evening_seen or evening_only
        if any(_selection_temporally_unschedulable(p, request) for p in day.places):
            violations.append("temporally_unschedulable")
        if any(p.place_id not in must_ids and _avoid_conflicts_with_place(request, p) for p in day.places):
            violations.append("hard_avoid")
        nested = {(profile.place_id,child) for profile in day.visit_profiles for child in profile.nested_place_ids}
        if any(_is_near_duplicate_pair(a, b) and (a.place_id,b.place_id) not in nested and (b.place_id,a.place_id) not in nested
               for a, b in itertools.combinations(day.places, 2)):
            violations.append("near_duplicate")
    violations.extend(_coverage_violation_codes(ids, request, coverage))
    return violations


async def _v2_verify_traffic(trial, *, request, provider, known, known_access,
                             attempted_access, access_walk_rejections, city, citycode,
                             deadline, mode, metrics, must_ids, eligible_ids, coverage=None):
    """One fact merge and short-walk proof path for initial and acquired routes."""
    settings = get_settings()
    # HTTP/cache owners keep their original caps across every candidate.
    if provider is not None:
        transport = _v2_transport_plan(trial) if callable(getattr(provider, "route_access", None)) else trial
        await _preload_precise_route_cache(provider,[transport],city=city,generation_mode=mode)
        allocate=getattr(provider,"allocate_uncached_plan_calls",None)
        if callable(allocate): allocate(transport,city=city,generation_mode=mode)
        await _v2_resolve_access(trial, provider, known_access, attempted_access,
            city=city, citycode=citycode, deadline=deadline,
            generation_mode=mode, walk_proof=False,
            walk_rejections=access_walk_rejections)
        for day in trial.day_groups:
            if deadline is not None and time.monotonic() >= deadline: break
            if not day.commute_legs: continue
            remaining=max(.001,deadline-time.monotonic()) if deadline is not None else 60
            day.commute_legs = await _resolve_precise_legs(
                day,day.commute_legs,city=city,citycode=citycode,generation_mode=mode,
                requested_commute_mode=request.commute_mode,
                daily_budget=day.traffic_policy.daily_limit_minutes if day.traffic_policy else daily_commute_budget_minutes(request,settings),
                single_leg_max=single_leg_max_minutes(mode,settings),provider=provider,metrics=metrics,
                known_precise_legs=known,concurrency=settings.amap_route_enrichment_concurrency,
                timeout_seconds=min(remaining,max(settings.amap_route_timeout,1)*len(day.commute_legs)),
                walk_proof=False)
    # Existing provider degradation may return estimates for the day: known facts still win.
    for day in trial.day_groups:
        day.commute_legs=[
            _lookup_known_commute_leg(
                leg, known,
                origin=day.places[index],
                destination=day.places[index + 1],
                generation_mode=mode,
            )
            for index, leg in enumerate(day.commute_legs)
        ]
    violations=_v2_plan_violations(trial,request,must_ids,eligible_ids,derive=True,coverage=coverage)
    # A rejected short transit leg can still have a legal walking fact.
    # Ask for that fact before treating the candidate as ineligible.
    if await _v2_prove_traffic_invalid_short_transit(
        trial, request=request, provider=provider, known=known,
        known_access=known_access, attempted_access=attempted_access,
        access_walk_rejections=access_walk_rejections,
        city=city, citycode=citycode, deadline=deadline,
        generation_mode=mode, metrics=metrics,
    ):
        violations=_v2_plan_violations(
            trial, request, must_ids, eligible_ids, derive=True, coverage=coverage,
        )
    return violations


_SHORT_TRANSIT_PROOF_LIMITS = ("single_leg_limit", "daily_commute_limit")


def _v2_day_traffic_limited(day) -> bool:
    violations = getattr(getattr(day, "day_feasibility", None), "violations", ()) or ()
    return any(code in violations for code in _SHORT_TRANSIT_PROOF_LIMITS)


def _v2_unproven_short_commute(day, known) -> bool:
    known = known or {}
    for index, leg in enumerate(day.commute_legs):
        if leg.mode != "transit" or index + 1 >= len(day.places):
            continue
        origin = day.places[index]
        destination = day.places[index + 1]
        if not _transit_walk_proof_applies(
            origin, destination, generation_mode="transit", estimated_mode=leg.mode,
        ):
            continue
        walking = known.get((origin.place_id, destination.place_id, "walking"))
        if isinstance(walking, CommuteLeg) and _adopted_walk_commute(walking):
            continue
        if _cached_walk_reject_reason(known, origin.place_id, destination.place_id):
            continue
        return True
    return False


def _v2_unproven_short_access(day) -> bool:
    by_id = {place.place_id: place for place in day.places}
    for leg in day.access_legs:
        if leg.mode != "transit" or _adopted_walk_access(leg):
            continue
        if _negative_walk_reason(_walk_reason_of(leg)):
            continue
        place = by_id.get(leg.place_id)
        if place is None:
            continue
        origin, destination = _v2_access_endpoints(leg, place)
        if _transit_walk_proof_triggered(origin, destination, generation_mode="transit"):
            return True
    return False


async def _v2_prove_traffic_invalid_short_transit(
    trial, *, request, provider, known, known_access, attempted_access,
    city, citycode, deadline, generation_mode, metrics,
    access_walk_rejections=None,
) -> bool:
    """Proof a short transit leg only after that candidate is already traffic-invalid.

    Feasible trials do not pay for a walking query. The proof stays inside the
    current trial, deadline and provider caps, and walking uses its own cache key.
    """
    if provider is None or generation_mode != "transit":
        return False
    if deadline is not None and time.monotonic() >= deadline:
        return False
    limited = [day for day in trial.day_groups if _v2_day_traffic_limited(day)]
    if not limited:
        return False
    prove_commute = [day for day in limited if _v2_unproven_short_commute(day, known)]
    prove_access = any(_v2_unproven_short_access(day) for day in limited)
    if not prove_commute and not prove_access:
        return False
    settings = get_settings()
    for day in prove_commute:
        if deadline is not None and time.monotonic() >= deadline:
            break
        if not day.commute_legs:
            continue
        remaining = max(.001, deadline - time.monotonic()) if deadline is not None else 60
        daily_budget = (
            day.traffic_policy.daily_limit_minutes
            if day.traffic_policy
            else daily_commute_budget_minutes(request, settings)
        )
        day.commute_legs = await _resolve_precise_legs(
            day, day.commute_legs, city=city, citycode=citycode,
            generation_mode=generation_mode,
            requested_commute_mode=request.commute_mode,
            daily_budget=daily_budget,
            single_leg_max=single_leg_max_minutes(generation_mode, settings),
            provider=provider, metrics=metrics, known_precise_legs=known,
            concurrency=settings.amap_route_enrichment_concurrency,
            timeout_seconds=min(
                remaining, max(settings.amap_route_timeout, 1) * len(day.commute_legs),
            ),
            walk_proof=True, record_leg_metrics=False, whole_day_fallback=False,
        )
    for day in trial.day_groups:
        if not day.commute_legs:
            continue
        day.commute_legs = [
            _lookup_known_commute_leg(
                leg, known,
                origin=day.places[index],
                destination=day.places[index + 1],
                generation_mode=generation_mode,
            )
            for index, leg in enumerate(day.commute_legs)
        ]
    if prove_access and (deadline is None or time.monotonic() < deadline):
        await _v2_resolve_access(
            trial, provider, known_access, attempted_access,
            city=city, citycode=citycode, deadline=deadline,
            generation_mode=generation_mode, walk_proof=True,
            walk_rejections=access_walk_rejections,
        )
    return True


def route_plan_violations(
    plans: list[PlanOutput],
    route_plans: list[RoutePlan],
) -> list[dict]:
    """Return deterministic locked-route membership and ordered-prose violations.

    Locked object is the final ordered ``RouteDayGroup.places`` list (and the
    commute legs derived from it). Declared ``day_place_names`` and **day-body
    narrative** first-hit canonical place-id sequences must ordered-equal that
    list. Day titles are validated separately so a correct title cannot mask a
    reordered body (ADR 0017).

    v0.15.1 article mode (D3): the article may name any place; naming is not
    visiting. The body read here is the backend skeleton (day titles and stop
    labels), so every caller checks the same route lines.
    """
    from src.config import get_settings
    if str(getattr(get_settings(), "writer_prompt_version", "") or "").strip() == "article":
        from src.agents.article_writer import article_skeleton
        plans = [
            plan.model_copy(update={"plan_text": article_skeleton(plan.plan_text, route_plans[index])})
            if index < len(route_plans) else plan
            for index, plan in enumerate(plans)
        ]
    violations = []
    if len(plans) != len(route_plans):
        violations.append({
            "reason": "plan_count_mismatch",
            "expected": len(route_plans),
            "actual": len(plans),
        })
    for index, plan in enumerate(plans):
        if index >= len(route_plans):
            violations.append({
                "plan_index": index + 1,
                "reason": "missing_route_plan",
            })
            continue
        route_plan = route_plans[index]
        expected_names = [
            [place.name for place in day_group.places if place.name]
            for day_group in route_plan.day_groups
        ]
        expected_ids = [
            [_canonical_order_id(place) for place in day_group.places]
            for day_group in route_plan.day_groups
        ]
        declared = plan.day_place_names or []
        if len(declared) != len(expected_names):
            violations.append({
                "plan_index": index + 1,
                "reason": "declared_day_count_mismatch",
                "expected": expected_names,
                "actual": declared,
            })
            continue

        for day_index, (day_expected_names, declared_names) in enumerate(
            zip(expected_names, declared),
            1,
        ):
            if list(declared_names or []) != list(day_expected_names):
                violations.append({
                    "plan_index": index + 1,
                    "day": day_index,
                    "reason": "declared_locked_day_group_violation",
                    "expected": day_expected_names,
                    "actual": list(declared_names or []),
                })

        day_parts = _day_title_and_body_for_order(plan.plan_text or "")
        if not day_parts and expected_names:
            violations.append({
                "plan_index": index + 1,
                "reason": "text_day_count_mismatch",
                "expected": expected_names,
                "actual": [],
            })
            continue
        if day_parts and sorted(day_parts) != list(range(1, len(expected_names) + 1)):
            if len(day_parts) != len(expected_names):
                violations.append({
                    "plan_index": index + 1,
                    "reason": "text_day_count_mismatch",
                    "expected": expected_names,
                    "actual": [
                        extract_ordered_place_names_from_text(
                            day_parts[day_number][0] + "\n" + day_parts[day_number][1],
                            expected_names[day_number - 1],
                        )
                        if day_number - 1 < len(expected_names)
                        else []
                        for day_number in sorted(day_parts)
                    ],
                })

        all_locked_places = [
            place
            for day_group in route_plan.day_groups
            for place in day_group.places
        ]
        for day_index, day_group in enumerate(route_plan.day_groups, 1):
            day_number = day_group.day or day_index
            title_text, body_text = day_parts.get(day_number, ("", ""))
            day_expected_ids = expected_ids[day_index - 1]
            day_expected_names = expected_names[day_index - 1]
            day_expected_id_set = set(day_expected_ids)
            if not title_text and not body_text:
                if day_expected_ids:
                    violations.append({
                        "plan_index": index + 1,
                        "day": day_index,
                        "reason": "text_locked_day_group_violation",
                        "expected": day_expected_names,
                        "actual": [],
                        "expected_place_ids": day_expected_ids,
                        "actual_place_ids": [],
                        "surface": "body",
                    })
                continue

            identity = getattr(plan, "poi_identity_result", None)
            city_name = _route_city_hint(route_plan, plan)

            # Title order is optional when the heading is theme-only, but when
            # the title mentions locked stops its first-hit order must match.
            # Also fail if title names a stop locked to another day.
            if title_text.strip():
                title_all = extract_day_ordered_place_ids(
                    title_text,
                    all_locked_places,
                    route_plan=route_plan,
                    identity_result=identity,
                    city_name=city_name,
                    day=day_number,
                )
                title_ids_all = list(title_all.get("ordered_place_ids") or [])
                title_cross = [
                    place_id
                    for place_id in title_ids_all
                    if place_id not in day_expected_id_set
                ]
                if title_all.get("ambiguous_alias"):
                    violations.append({
                        "plan_index": index + 1,
                        "day": day_index,
                        "reason": "ambiguous_alias",
                        "expected": day_expected_names,
                        "actual": title_all.get("ordered_names") or [],
                        "expected_place_ids": day_expected_ids,
                        "actual_place_ids": title_ids_all,
                        "ambiguous_alias": title_all["ambiguous_alias"],
                        "surface": "title",
                    })
                elif title_cross:
                    violations.append({
                        "plan_index": index + 1,
                        "day": day_index,
                        "reason": "text_locked_day_group_violation",
                        "expected": day_expected_names,
                        "actual": title_all.get("ordered_names") or [],
                        "expected_place_ids": day_expected_ids,
                        "actual_place_ids": title_ids_all,
                        "surface": "title",
                        "cross_day_place_ids": title_cross,
                    })
                else:
                    title_ids = [
                        place_id
                        for place_id in title_ids_all
                        if place_id in day_expected_id_set
                    ]
                    if title_ids and title_ids != day_expected_ids[: len(title_ids)]:
                        violations.append({
                            "plan_index": index + 1,
                            "day": day_index,
                            "reason": "text_locked_day_group_violation",
                            "expected": day_expected_names,
                            "actual": title_all.get("ordered_names") or [],
                            "expected_place_ids": day_expected_ids,
                            "actual_place_ids": title_ids,
                            "surface": "title",
                        })

            # Body narrative is the ADR 0017 day-narrative first-hit sequence.
            # Scan all locked stops so a later-day POI named early is still caught.
            body_all = extract_day_ordered_place_ids(
                body_text,
                all_locked_places,
                route_plan=route_plan,
                identity_result=identity,
                city_name=city_name,
                day=day_number,
            )
            if body_all.get("ambiguous_alias"):
                violations.append({
                    "plan_index": index + 1,
                    "day": day_index,
                    "reason": "ambiguous_alias",
                    "expected": day_expected_names,
                    "actual": body_all.get("ordered_names") or [],
                    "expected_place_ids": day_expected_ids,
                    "actual_place_ids": body_all.get("ordered_place_ids") or [],
                    "ambiguous_alias": body_all["ambiguous_alias"],
                    "surface": "body",
                })
                continue
            body_ids_all = list(body_all.get("ordered_place_ids") or [])
            body_cross = [
                place_id
                for place_id in body_ids_all
                if place_id not in day_expected_id_set
            ]
            actual_ids = [
                place_id
                for place_id in body_ids_all
                if place_id in day_expected_id_set
            ]
            actual_names = [
                name
                for place_id, name in zip(
                    body_ids_all,
                    body_all.get("ordered_names") or [],
                )
                if place_id in day_expected_id_set
            ]
            if body_cross or actual_ids != day_expected_ids:
                payload = {
                    "plan_index": index + 1,
                    "day": day_index,
                    "reason": "text_locked_day_group_violation",
                    "expected": day_expected_names,
                    "actual": body_all.get("ordered_names") or actual_names,
                    "expected_place_ids": day_expected_ids,
                    "actual_place_ids": body_ids_all if body_cross else actual_ids,
                    "surface": "body",
                }
                if body_cross:
                    payload["cross_day_place_ids"] = body_cross
                violations.append(payload)
    return violations


def extract_day_place_names(
    plan_text: str,
    candidate_names: list[str],
) -> list[list[str]]:
    """Extract candidate names from Day sections with longest-name precedence.

    Returns first-hit ordered names per day. Overlapping shorter names lose to
    longer matches; repeated mentions of the same candidate keep only the first
    hit. This helper remains name-based for legacy callers; ordered lock checks
    use :func:`extract_day_ordered_place_ids`.
    """
    heading = re.compile(
        r"(?im)^\s*(?:#{1,6}\s*)?(?:\*\*)?\s*"
        r"(?:Day\s*(\d+)|第\s*(\d+|[一二三四五六七八九十]+)\s*(?:天|日))[^\n]*?(?:\*\*)?\s*$"
    )
    matches = list(heading.finditer(plan_text))
    if not matches:
        return []
    name_patterns = _candidate_name_patterns(candidate_names)
    day_names_by_number: dict[int, list[str]] = {}
    for index, match in enumerate(matches):
        day_number = _parse_day_heading_number(match.group(1) or match.group(2))
        if day_number is None:
            continue
        # Include the heading line because the writer may put the route skeleton
        # directly in it, for example "Day 1｜洪崖洞 → 解放碑".
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(plan_text)
        section = plan_text[start:end]
        current = day_names_by_number.setdefault(day_number, [])
        for name in extract_ordered_place_names_from_text(section, candidate_names):
            if name not in current:
                current.append(name)
    return [
        day_names_by_number[day_number]
        for day_number in sorted(day_names_by_number)
    ]


def extract_ordered_place_names_from_text(
    section_text: str,
    candidate_names: list[str],
) -> list[str]:
    """First-hit ordered candidate names inside one text section."""
    if not section_text or not candidate_names:
        return []
    name_patterns = _candidate_name_patterns(candidate_names)
    claimed_ranges: list[tuple[int, int]] = []
    found: list[tuple[int, str]] = []
    for pattern, name in name_patterns:
        for occurrence in re.finditer(pattern, section_text):
            occurrence_range = (occurrence.start(), occurrence.end())
            if any(
                occurrence_range[0] < claimed_end
                and claimed_start < occurrence_range[1]
                for claimed_start, claimed_end in claimed_ranges
            ):
                continue
            claimed_ranges.append(occurrence_range)
            found.append((occurrence.start(), name))
    found.sort(key=lambda item: (item[0], -len(item[1]), item[1]))
    ordered: list[str] = []
    for _, name in found:
        if name not in ordered:
            ordered.append(name)
    return ordered


def authorized_commute_mask_strings(
    route_plan: RoutePlan,
    *,
    day: int | None = None,
) -> list[str]:
    """Exact backend-authorized commute strings eligible for prose masking.

    Only Composition Blueprint short transitions, deterministic formatter notes,
    and grounded transit summaries are allowed. Broad transit-token regexes are
    intentionally not used so Writer-invented commute lines cannot hide reorders.

    When ``day`` is provided, only that RouteDayGroup's authorized spans are
    returned so a later day's template cannot mask an earlier day's prose.
    """
    masks: list[str] = []
    for day_group in route_plan.day_groups:
        if day is not None and int(day_group.day or 0) != int(day):
            continue
        for note in day_group.commute_notes or []:
            if note:
                masks.append(note)
        for leg in day_group.commute_legs or []:
            if leg.note:
                masks.append(leg.note)
            if leg.mode == "transit":
                masks.append(
                    f"{leg.from_name}→{leg.to_name}，公共交通约 "
                    f"{int(leg.duration_minutes or 0)} 分钟"
                )
                masks.append(
                    format_transit_summary(
                        int(leg.duration_minutes or 0),
                        leg.transit_steps,
                        detail_quality=leg.transit_detail_quality,
                    )
                )
                masks.append(
                    TRANSIT_DETAIL_GENERIC_TEMPLATE.format(
                        duration_minutes=int(leg.duration_minutes or 0)
                    )
                )
    from src.agents.preference_notices import notice_mask_strings
    masks.extend(notice_mask_strings(route_plan, day=day))
    # Longest first so nested authorized phrases mask completely.
    unique = sorted({value for value in masks if value}, key=lambda item: (-len(item), item))
    return unique


def mask_authorized_commute_spans(
    text: str,
    route_plan: RoutePlan,
    *,
    day: int | None = None,
) -> str:
    """Replace authorized exact commute strings with same-length spaces."""
    masked = text or ""
    for value in authorized_commute_mask_strings(route_plan, day=day):
        if value and value in masked:
            masked = masked.replace(value, " " * len(value))
    return masked


def extract_day_ordered_place_ids(
    section_text: str,
    locked_places: list[CandidatePlace],
    *,
    route_plan: RoutePlan | None = None,
    identity_result: Any | None = None,
    city_name: str = "",
    day: int | None = None,
) -> dict[str, Any]:
    """Resolve first-hit locked place ids for one text surface.

    Pipeline:
    text hit → longest-name precedence across route + contextual names →
    exact locked name or canonical_same / alias_same only →
    canonical_place_id (fallback place_id) → first-hit dedupe → ordered comparison.

    Only full locked names and explicit identity aliases may occupy order slots.
    Auto short forms (suffix strip / parenthetical / city prefix) are not route
    aliases for order. ``parent_child`` / contextual-only never occupy slots, even
    when they collide with a generated short form of a locked name.
    """
    del city_name  # reserved for callers; not auto-promoted into route aliases
    if not locked_places:
        return {
            "ordered_place_ids": [],
            "ordered_names": [],
            "ambiguous_alias": None,
        }

    locked_by_order_id = {
        _canonical_order_id(place): place for place in locked_places
    }
    name_to_order_ids: dict[str, set[int]] = {}
    contextual_only: set[str] = set()

    def _register_route(name: str, order_id: int) -> None:
        cleaned = (name or "").strip()
        if not cleaned:
            return
        name_to_order_ids.setdefault(cleaned, set()).add(order_id)

    for place in locked_places:
        _register_route(place.name, _canonical_order_id(place))

    if identity_result is not None:
        locked_names = {place.name for place in locked_places}
        for relation in getattr(identity_result, "relations", []) or []:
            source = getattr(relation, "source_name", "")
            target = getattr(relation, "target_name", "")
            relation_type = getattr(relation, "relation", "")
            if source not in locked_names:
                continue
            source_place = next(
                (place for place in locked_places if place.name == source),
                None,
            )
            if source_place is None:
                continue
            order_id = _canonical_order_id(source_place)
            if relation_type in {"canonical_same", "alias_same"}:
                _register_route(target, order_id)
            elif relation_type == "parent_child" and target:
                contextual_only.add(target.strip())

    # Identity contextual names win over any accidental same-string route alias
    # registration so parent_child never occupies an order slot.
    for name in list(name_to_order_ids):
        if name in contextual_only:
            name_to_order_ids.pop(name, None)

    ambiguous_names = {
        name
        for name, order_ids in name_to_order_ids.items()
        if len(order_ids) > 1
    }

    scan_text = section_text or ""
    if route_plan is not None:
        scan_text = mask_authorized_commute_spans(scan_text, route_plan, day=day)

    route_names = set(name_to_order_ids)
    skip_names = {name for name in contextual_only if name}
    all_names = sorted(
        route_names | skip_names,
        key=lambda name: (-len(name), name),
    )
    claimed_ranges: list[tuple[int, int]] = []
    hits: list[tuple[int, str, str]] = []

    for name in all_names:
        kind = "route" if name in route_names else "contextual"
        for occurrence in re.finditer(re.escape(name), scan_text):
            occurrence_range = (occurrence.start(), occurrence.end())
            if any(
                occurrence_range[0] < claimed_end
                and claimed_start < occurrence_range[1]
                for claimed_start, claimed_end in claimed_ranges
            ):
                continue
            claimed_ranges.append(occurrence_range)
            hits.append((occurrence.start(), kind, name))

    hits.sort(key=lambda item: (item[0], -len(item[2]), item[2]))

    ordered_ids: list[int] = []
    ordered_names: list[str] = []
    for _, kind, name in hits:
        if kind == "contextual":
            continue
        if name in ambiguous_names:
            return {
                "ordered_place_ids": ordered_ids,
                "ordered_names": ordered_names,
                "ambiguous_alias": name,
            }
        order_ids = sorted(name_to_order_ids.get(name) or [])
        if len(order_ids) != 1:
            return {
                "ordered_place_ids": ordered_ids,
                "ordered_names": ordered_names,
                "ambiguous_alias": name,
            }
        order_id = order_ids[0]
        if order_id in ordered_ids:
            continue
        place = locked_by_order_id.get(order_id)
        if place is None:
            continue
        ordered_ids.append(order_id)
        ordered_names.append(place.name)

    return {
        "ordered_place_ids": ordered_ids,
        "ordered_names": ordered_names,
        "ambiguous_alias": None,
    }


def _canonical_order_id(place: CandidatePlace) -> int:
    """Prefer canonical_place_id; fall back to place_id when unset."""
    if place.canonical_place_id is not None:
        return int(place.canonical_place_id)
    return int(place.place_id)


def _day_title_and_body_for_order(plan_text: str) -> dict[int, tuple[str, str]]:
    """Split each Day section into (title_line, body_text).

    Title is the heading line only; body is everything after its first newline.
    Body order is the ADR 0017 narrative first-hit sequence and is never seeded
    from title hits.

    Same-day later headings do **not** overwrite earlier body text: later title
    lines are folded into the body so foreshadowing on a pseudo/duplicate
    heading remains visible to order checks. Prose lines like “第一天围绕…” stay
    in body via the heading classifier.
    """
    heading = re.compile(
        r"(?im)^\s*(?:#{1,6}\s*)?(?:\*\*)?\s*"
        r"(?:Day\s*(\d+)|第\s*(\d+|[一二三四五六七八九十]+)\s*(?:天|日))"
        r"([^\n]*?)(?:\*\*)?\s*$"
    )
    matches = list(heading.finditer(plan_text or ""))
    valid_matches: list[tuple[re.Match[str], int]] = []
    for match in matches:
        day = _parse_day_heading_number(match.group(1) or match.group(2))
        if day is None:
            continue
        if not _is_day_heading_line(match.group(0), match.group(3) or ""):
            continue
        valid_matches.append((match, day))

    sections: dict[int, tuple[str, str]] = {}
    for index, (match, day) in enumerate(valid_matches):
        end = (
            valid_matches[index + 1][0].start()
            if index + 1 < len(valid_matches)
            else len(plan_text or "")
        )
        section = (plan_text or "")[match.start():end]
        newline = section.find("\n")
        if newline < 0:
            title, body = section, ""
        else:
            title, body = section[:newline], section[newline + 1 :]
        if day not in sections:
            sections[day] = (title, body)
            continue
        prev_title, prev_body = sections[day]
        # Keep the first title as the title surface. Fold this later heading
        # line into body so its POI names remain visible to first-hit checks.
        later_chunks = [chunk for chunk in (title, body) if chunk and chunk.strip()]
        later_text = "\n".join(later_chunks)
        if prev_body and later_text:
            merged_body = prev_body.rstrip() + "\n" + later_text.lstrip()
        else:
            merged_body = prev_body or later_text
        sections[day] = (prev_title, merged_body)
    return sections


def _is_day_heading_line(full_line: str, remainder: str) -> bool:
    """Reject prose lines that start with 第N天 / Day N but are not headings.

    Correctness does not depend only on a prose-starter blacklist. Unknown short
    verb themes still parse as headings, but same-day later headings are folded
    into body so their text cannot disappear from order scanning.
    """
    del full_line
    tail = (remainder or "").strip()
    if not tail:
        return True
    if tail[0] in "｜|：:：—–-·/【[（( ":
        return True
    if "→" in tail or "->" in tail or "｜" in tail or "|" in tail:
        return True
    prose_starters = (
        "围绕", "先", "从", "到", "去", "看", "走", "安排", "继续", "今天",
        "把", "以", "在", "用", "可以", "建议", "漫步", "探索", "夜游",
        "闲逛", "逛逛", "打卡", "游玩", "游览", "途经", "路过", "路过",
    )
    if any(tail.startswith(prefix) for prefix in prose_starters):
        return False
    if "。" in tail or "，" in tail or "、" in tail or "；" in tail:
        return False
    # Short theme titles without sentence punctuation.
    if len(tail) <= 24:
        return True
    return False


def _day_sections_for_order(plan_text: str) -> dict[int, str]:
    """Legacy full-section view (title + body) for callers that still need it."""
    return {
        day: (title + ("\n" + body if body else ""))
        for day, (title, body) in _day_title_and_body_for_order(plan_text).items()
    }


def _route_city_hint(route_plan: RoutePlan, plan: PlanOutput) -> str:
    del route_plan
    # City is not stored on RoutePlan; callers may leave empty. Alias city-prefix
    # stripping is best-effort only.
    return ""


def _parse_day_heading_number(value: str | None) -> int | None:
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


def _candidate_name_patterns(candidate_names: list[str]) -> list[tuple[str, str]]:
    """Return safe text patterns mapped back to canonical candidate names."""
    unique_names = sorted(set(candidate_names), key=lambda name: (-len(name), name))
    base_counts: dict[str, int] = {}
    bases: dict[str, str] = {}
    for name in unique_names:
        base = _parenthetical_base_name(name)
        if base:
            bases[name] = base
            base_counts[base] = base_counts.get(base, 0) + 1

    patterns: list[tuple[str, str]] = []
    for name in unique_names:
        escaped = re.escape(name)
        base = bases.get(name)
        if base and base_counts.get(base) == 1:
            escaped = f"{escaped}|{re.escape(base)}"
        patterns.append((escaped, name))
    return patterns


def _parenthetical_base_name(name: str) -> str:
    value = (name or "").strip()
    for left, right in (("（", "）"), ("(", ")")):
        if left not in value or not value.endswith(right):
            continue
        base = value.split(left, 1)[0].strip()
        if len(base) >= 2:
            return base
    return ""
