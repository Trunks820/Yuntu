"""Deterministic v0.6 route planning between retrieval and writing."""

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
from functools import cmp_to_key
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
    CandidateGroup,
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
    amap_keys_configured,
    classify_amap_failure,
    configured_amap_keys,
    get_amap_key_coordinator,
    key_fingerprint,
    redact_amap_key,
)
from src.agents.route_feasibility import (
    POLICY_VERSION, DWELL_TIME_MINUTES, DayContext, build_visit_profile,
    evaluate_day, day_slot_limit, daytime_activity, valid_coordinate, access_summary,
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

# Place weights for daily budget (not precise minutes, relative weights)
PLACE_WEIGHTS = {
    "attraction": 1.5,
    "museum": 2.0,
    "park": 1.5,
    "scenic_area": 2.5,
    "business_area": 1.0,
    "photo_spot": 0.5,
    "restaurant": 1.0,
    "cafe": 0.75,
    "snack": 0.5,
    "food": 1.0,
    "dessert": 0.5,
    "market": 1.0,
    "street": 0.75,
}

_EVENING_MARKERS = ("夜景", "日落", "夜游", "灯光", "夜晚")
_DAYTIME_MARKERS = ("白天", "上午", "下午", "清晨", "早上", "日间")
DEFAULT_DAY_CAPACITY_MINUTES = 420
EVENING_ONLY_CAPACITY_MINUTES = 120
DEFAULT_DAY_WEIGHT_BUDGET = 7.0
MIN_TIME_PREFERENCE_WEIGHT_BUDGET = 4.0
RELAXED_PREFERENCE_MARKERS = RELAXED_PACE_MARKERS
SPATIAL_SPREAD_PREFERENCE_MARKERS = (
    "分散",
    "不集中",
    "多区域",
    "跨区",
    "地标",
    "城市地标",
)
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
ROUTE_QUALITY_SPREAD_CLEAR_WIN_MIN_DIFF = 20.0
ROUTE_QUALITY_SPREAD_STRONG_WIN_MIN_DIFF = 40.0
ROUTE_QUALITY_FULL_ANCHOR_FLOOR_MIN_TOTAL = 3
MULTI_AREA_ROUTE_LABEL = "multi-area-skeleton"
MULTI_AREA_SEED_MIN_CENTER_KM = 4.0
MULTI_AREA_DENSITY_RADIUS_KM = 15.0
MULTI_AREA_LOCAL_FILL_RADIUS_KM = 8.0
MULTI_AREA_MIN_PLACES_PER_DAY = 3
MULTI_AREA_ACTIVITY_TYPES = {
    "attraction",
    "business_area",
    "museum",
    "park",
    "scenic_area",
}
MULTI_AREA_WEAK_TYPES = {
    "bridge",
    "cafe",
    "intersection",
    "market",
    "other",
    "photo_spot",
    "restaurant",
    "road",
    "shopping",
    "street",
    "transport",
    "viewpoint",
}
MULTI_AREA_WEAK_NAME_MARKERS = (
    "公交",
    "车站",
    "地铁",
    "路口",
    "桥路",
)


def is_food_place(place: CandidatePlace) -> bool:
    """Check if a place is food-related."""
    return is_food_partition_place(place)


def top_by_score(places: list[CandidatePlace], n: int) -> list[CandidatePlace]:
    """Return top n places by recommend_score."""
    return sorted(places, key=lambda p: p.recommend_score, reverse=True)[:n]


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
                for reason in ("REPLACE_DROPPED", "FILL_EMPTY_DAY", "FILL_MUST_INCLUDE_DAY")
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


def _provider_is_backing_off(provider: RouteProvider) -> bool:
    checker = getattr(provider, "is_backing_off", None)
    return bool(callable(checker) and checker())


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


def estimate_dwell_time(place: CandidatePlace) -> int:
    """Estimate dwell time in minutes from canonical profile, then type fallback."""
    if place.typical_visit_minutes is not None:
        return max(15, min(480, int(place.typical_visit_minutes)))
    return DWELL_TIME_MINUTES.get(place.place_type, 60)


def calculate_day_weight(day_places: list[CandidatePlace]) -> float:
    """Calculate total weight for a day's places.

    Uses relative weights (not precise minutes) to prevent
    unreasonable combinations like "6 heavy attractions in one day".
    """
    total = 0.0
    for place in day_places:
        weight = PLACE_WEIGHTS.get(place.place_type, 1.0)
        total += weight
    return total


def _time_preferences_active(request: TripRequest | None) -> bool:
    return bool(
        request is not None
        and getattr(get_settings(), "time_preferences_enabled", False)
        and request.has_time_preferences()
    )


def day_time_capacity_minutes(
    request: TripRequest | None = None,
    *,
    evening_only: bool = False,
) -> int:
    if evening_only:
        return EVENING_ONLY_CAPACITY_MINUTES
    if not _time_preferences_active(request):
        return DEFAULT_DAY_CAPACITY_MINUTES
    assert request is not None
    span = request.daily_span_minutes()
    base = min(
        span if span is not None else DEFAULT_DAY_CAPACITY_MINUTES,
        DEFAULT_DAY_CAPACITY_MINUTES,
    )
    capacity = base - request.rest_window_minutes()
    return max(EVENING_ONLY_CAPACITY_MINUTES, min(DEFAULT_DAY_CAPACITY_MINUTES, capacity))


def _day_weight_budget(request: TripRequest | None = None) -> float:
    if not _time_preferences_active(request):
        return DEFAULT_DAY_WEIGHT_BUDGET
    scaled = DEFAULT_DAY_WEIGHT_BUDGET * (
        day_time_capacity_minutes(request) / DEFAULT_DAY_CAPACITY_MINUTES
    )
    return max(MIN_TIME_PREFERENCE_WEIGHT_BUDGET, scaled)


def _suppress_evening_markers(request: TripRequest | None = None) -> bool:
    if not _time_preferences_active(request):
        return False
    assert request is not None
    daily_end = request.daily_end
    if daily_end is None:
        return False
    hours, minutes = daily_end.split(":", 1)
    return int(hours) * 60 + int(minutes) < 18 * 60


def enforce_day_weight_budget(
    day_places: list[CandidatePlace],
    request: TripRequest | None = None,
) -> list[CandidatePlace]:
    """Enforce daily weight budget to prevent overloaded days.

    v0.6.5: Lightweight dwell weight budget.
    Maximum weight per day: 7.0 (normal), 5.5 (light day).
    If over budget, keep highest-scoring places within limit.
    """
    MAX_WEIGHT_NORMAL = _day_weight_budget(request)    # Normal day
    MAX_WEIGHT_LIGHT = 5.5     # Light day (future use)

    current_weight = calculate_day_weight(day_places)

    if current_weight <= MAX_WEIGHT_NORMAL:
        return day_places

    # Over budget: keep highest-scoring places until within budget, then
    # restore the already optimized route order.
    sorted_places = sorted(day_places, key=lambda p: p.recommend_score, reverse=True)

    selected_ids: set[int] = set()
    total = 0.0
    for place in sorted_places:
        weight = PLACE_WEIGHTS.get(place.place_type, 1.0)
        if total + weight <= MAX_WEIGHT_NORMAL:
            selected_ids.add(place.place_id)
            total += weight

    return _preserve_order(day_places, selected_ids)


def detect_food_intensity(preferences: list[str]) -> str:
    """Detect food intensity from user preferences.

    Returns:
        "strong": Heavy food focus (need multiple meals per day)
        "moderate": Normal food interest
        "light": Minimal food focus
    """
    food_keywords = ["美食", "吃", "food", "火锅", "小吃", "餐厅", "美味"]
    pref_text = " ".join(preferences).lower()

    food_mentions = sum(1 for kw in food_keywords if kw in pref_text)

    if food_mentions >= 2:
        return "strong"
    elif food_mentions >= 1:
        return "moderate"
    else:
        return "light"


def arrange_places_with_meal_slots(
    day_places: list[CandidatePlace],
    food_intensity: str,
) -> list[CandidatePlace]:
    """Lightly arrange food places around meal slots.

    v0.6.6 originally rebuilt a full activity/food timeline, which could undo the
    geographic route order. This version keeps the existing order and only makes
    bounded moves for obvious meal-slot problems:
    - a leading food stop is moved after the first activity;
    - food-focused days try to keep one later food stop after the last activity.
    """
    ordered = list(day_places)
    food_places = [p for p in ordered if is_food_place(p)]
    activity_places = [p for p in ordered if not is_food_place(p)]

    if not food_places or not activity_places:
        return ordered

    def move_after(place_id: int, anchor_id: int) -> None:
        nonlocal ordered
        place = next((p for p in ordered if p.place_id == place_id), None)
        if place is None:
            return
        without_place = [
            p for p in ordered
            if p.place_id != place_id
        ]
        anchor_index = next(
            (
                index for index, candidate in enumerate(without_place)
                if candidate.place_id == anchor_id
            ),
            None,
        )
        if anchor_index is None:
            return
        without_place.insert(anchor_index + 1, place)
        ordered = without_place

    first_activity = activity_places[0]
    leading_food = next(
        (place for place in ordered if is_food_place(place)),
        None,
    )
    if leading_food and ordered[0].place_id == leading_food.place_id:
        move_after(leading_food.place_id, first_activity.place_id)

    if food_intensity in {"strong", "moderate"} and len(food_places) >= 2:
        last_activity = activity_places[-1]
        last_food = food_places[-1]
        last_activity_index = next(
            index for index, place in enumerate(ordered)
            if place.place_id == last_activity.place_id
        )
        has_food_after_last_activity = any(
            is_food_place(place)
            for place in ordered[last_activity_index + 1:]
        )
        if not has_food_after_last_activity:
            move_after(last_food.place_id, last_activity.place_id)

    return ordered


def find_insertion_index(timeline: list[dict], target_time: float) -> int:
    """Find insertion position for a meal at target_time.

    Returns index of first item at or after target_time.
    """
    for i, item in enumerate(timeline):
        if item['time'] >= target_time:
            return i
    return len(timeline)


def validate_day_time_budget(
    day_places: list[CandidatePlace],
    request: TripRequest | None = None,
) -> bool:
    """Validate time budget for a single day.

    Checks if total dwell + commute time fits within day capacity.
    Daytime capacity: 420 minutes (7 hours)
    Evening-only capacity: 120 minutes (2 hours)
    """
    if not day_places:
        return True

    total_dwell = sum(estimate_dwell_time(p) for p in day_places)
    # Commute is estimated by legs, but for now we'll use a simplified check
    # TODO: integrate precise commute calculation from _legs()

    # Check if all non-food places are evening-only
    evening_only = all(
        _is_evening_marked(p) and not _is_daytime_marked(p)
        for p in day_places
        if not is_food_place(p)
    )

    capacity = day_time_capacity_minutes(request, evening_only=evening_only)

    # Allow 20% buffer for flexibility
    return total_dwell <= capacity * 1.2


def enforce_daily_structure(
    day_places: list[CandidatePlace],
    preferences: list[str],
) -> list[CandidatePlace]:
    """Enforce daily structure constraints while preserving route order.

    Food-focused: keep top 3 food places, ensure at least 2 activities
    Normal: keep top 2 food places, prioritize activities

    Fixed v0.6.4: Preserve original route order instead of grouping by type.
    """
    food_places = [p for p in day_places if is_food_place(p)]
    activities = [p for p in day_places if not is_food_place(p)]

    is_food_focused = _food_focused(preferences)

    if is_food_focused:
        # Keep top 3 food places, ensure at least 2 activities
        max_food = 3
        min_activities = 2
    else:
        # Keep top 2 food places, prioritize activities
        max_food = 2
        min_activities = 1

    # Calculate target counts
    target_activities = max(min_activities, len(activities))
    target_food = min(max_food, len(food_places))

    # Select best places by score
    selected_food = top_by_score(food_places, target_food)
    selected_activities = top_by_score(activities, target_activities)

    # Build set of IDs to keep
    kept_ids = {p.place_id for p in selected_food + selected_activities}

    # Return in original route order (critical fix)
    return [p for p in day_places if p.place_id in kept_ids]


def balance_day_types(
    day_places: list[CandidatePlace],
    preferences: list[str],
) -> list[CandidatePlace]:
    """Balance restaurant vs activity ratio within a single day.

    Food-focused: keep top 3 restaurants, ensure at least 2 activities
    Normal: keep top 2 restaurants, prioritize activities

    Deprecated: Use enforce_daily_structure instead.
    """
    return enforce_daily_structure(day_places, preferences)


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


def normalize_route_near_duplicates(
    route_plan: RoutePlan,
    *,
    request: TripRequest | None = None,
    accommodation_coord: tuple[float, float] | None = None,
) -> None:
    """Remove obvious same-place duplicates before budget and writing."""
    changed = False
    dropped_ids: list[int] = []
    settings = get_settings()
    min_places = settings.route_places_per_day_min
    route_generation_mode: EffectiveCommuteMode = (
        generation_base_mode(request, settings)[0]
        if request is not None
        else "driving"
    )
    for day_group in route_plan.day_groups:
        deduped, dropped = _dedupe_near_duplicate_places(day_group.places)
        if not dropped:
            continue
        changed = True
        dropped_ids.extend(dropped)
        day_group.places = _time_ordered_places(
            deduped,
            accommodation_coord=accommodation_coord,
        )
        if route_plan.optimized:
            day_group.commute_legs = _legs(
                day_group.places,
                generation_mode=route_generation_mode,
            )
            day_group.commute_minutes = sum(
                leg.duration_minutes for leg in day_group.commute_legs
            )
            day_group.commute_notes = [leg.note for leg in day_group.commute_legs]
        day_group.time_hints = _time_hints(day_group.places)
    selected: list[tuple[int, CandidatePlace]] = []
    cross_day_dropped: dict[int, set[int]] = defaultdict(set)
    for day_index, day_group in enumerate(route_plan.day_groups):
        for place in day_group.places:
            existing_match: tuple[int, int, CandidatePlace] | None = None
            for sel_idx, (sel_day, sel_place) in enumerate(selected):
                if _is_near_duplicate_pair(place, sel_place):
                    existing_match = (sel_idx, sel_day, sel_place)
                    break
            if existing_match is None:
                selected.append((day_index, place))
                continue
            sel_idx, existing_day_index, existing_place = existing_match
            preferred = _prefer_duplicate_candidate(existing_place, place)
            if preferred is existing_place:
                if (
                    len(day_group.places) - len(cross_day_dropped[day_index])
                    > min_places
                ):
                    cross_day_dropped[day_index].add(place.place_id)
            else:
                if (
                    len(route_plan.day_groups[existing_day_index].places)
                    - len(cross_day_dropped[existing_day_index])
                    > min_places
                ):
                    cross_day_dropped[existing_day_index].add(
                        existing_place.place_id
                    )
                    selected[sel_idx] = (day_index, place)
    for day_index, removed_ids in cross_day_dropped.items():
        if not removed_ids:
            continue
        changed = True
        dropped_ids.extend(sorted(removed_ids))
        day_group = route_plan.day_groups[day_index]
        day_group.places = [
            place for place in day_group.places
            if place.place_id not in removed_ids
        ]
        if route_plan.optimized:
            day_group.commute_legs = _legs(
                day_group.places,
                generation_mode=route_generation_mode,
            )
            day_group.commute_minutes = sum(
                leg.duration_minutes for leg in day_group.commute_legs
            )
            day_group.commute_notes = [leg.note for leg in day_group.commute_legs]
        day_group.time_hints = _time_hints(day_group.places)
    route_plan.day_groups = _order_day_groups_cluster_first(
        route_plan.day_groups
    )
    for index, day_group in enumerate(route_plan.day_groups, 1):
        day_group.day = index
    if changed:
        route_plan.dropped_place_ids = sorted(set([
            *route_plan.dropped_place_ids,
            *dropped_ids,
        ]))


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


def _cluster_key(day_group: RouteDayGroup) -> tuple:
    """Stable grouping key: administrative area, else a coarse geo bucket."""
    if day_group.adcode:
        return ("adcode", day_group.adcode)
    center = _day_center(day_group)
    if center is None:
        return ("unlocated", id(day_group))
    # ~0.1 deg (~11 km) bucket so genuinely nearby unlocated days still merge.
    return ("geo", round(center[0], 1), round(center[1], 1))


def _cluster_day_groups_by_area(
    day_groups: list[RouteDayGroup],
) -> list[list[RouteDayGroup]]:
    """Group days sharing an area into one cluster, first-appearance order."""
    clusters: list[list[RouteDayGroup]] = []
    index_by_key: dict[tuple, int] = {}
    for day_group in day_groups:
        key = _cluster_key(day_group)
        existing = index_by_key.get(key)
        if existing is None:
            index_by_key[key] = len(clusters)
            clusters.append([day_group])
        else:
            clusters[existing].append(day_group)
    return clusters


def _cluster_centroid(
    cluster: list[RouteDayGroup],
) -> tuple[float, float] | None:
    centers = [
        center for center in (_day_center(day) for day in cluster)
        if center is not None
    ]
    if not centers:
        return None
    return (
        sum(center[0] for center in centers) / len(centers),
        sum(center[1] for center in centers) / len(centers),
    )


def _nearest_neighbor_cluster_order(
    centroids: list[tuple[float, float] | None],
) -> tuple[int, ...]:
    """Greedy fallback sweep for pathological all-distinct-area trips."""
    remaining = list(range(len(centroids)))
    order = [remaining.pop(0)]
    while remaining:
        last = centroids[order[-1]]
        if last is None:
            order.append(remaining.pop(0))
            continue

        def distance_from_last(position: int) -> float:
            target = centroids[remaining[position]]
            if target is None:
                return math.inf
            return _haversine_coords(last[0], last[1], target[0], target[1])

        best = min(range(len(remaining)), key=distance_from_last)
        order.append(remaining.pop(best))
    return tuple(order)


def _order_clusters_min_sweep(
    clusters: list[list[RouteDayGroup]],
) -> list[list[RouteDayGroup]]:
    """Order clusters as a single forward sweep, shortest inter-area path."""
    if len(clusters) <= 2:
        return list(clusters)
    centroids = [_cluster_centroid(cluster) for cluster in clusters]

    def path_cost(order: tuple[int, ...]) -> float:
        total = 0.0
        for left, right in zip(order, order[1:]):
            start, end = centroids[left], centroids[right]
            if start is None or end is None:
                continue
            total += _haversine_coords(start[0], start[1], end[0], end[1])
        return total

    indices = tuple(range(len(clusters)))
    if len(clusters) > 8:  # guard against factorial blow-up
        best_order = _nearest_neighbor_cluster_order(centroids)
    else:
        best_order = indices
        best_cost = path_cost(indices)
        for candidate in itertools.permutations(indices):
            cost = path_cost(candidate)
            if cost < best_cost:
                best_cost = cost
                best_order = candidate
    return [clusters[index] for index in best_order]


def _order_day_groups_cluster_first(
    day_groups: list[RouteDayGroup],
) -> list[RouteDayGroup]:
    """Keep same-area days contiguous, order areas as one forward sweep.

    Groups days by administrative area (``adcode``; a coarse geo bucket when
    adcode is missing), then orders the areas to minimise total inter-area
    centre distance. Same-area days can never be split by a day from another
    area, which eliminates the A->B->A geographic sandwich. Single-area trips
    collapse to one cluster and are returned unchanged.
    """
    if len(day_groups) <= 2:
        return list(day_groups)
    clusters = _cluster_day_groups_by_area(day_groups)
    if len(clusters) <= 1:
        return list(day_groups)
    ordered_clusters = _order_clusters_min_sweep(clusters)
    return [day_group for cluster in ordered_clusters for day_group in cluster]


def _preserve_order(
    places: list[CandidatePlace],
    selected_place_ids: set[int],
) -> list[CandidatePlace]:
    return [place for place in places if place.place_id in selected_place_ids]


def _drop_lowest_scoring_place(
    places: list[CandidatePlace],
) -> tuple[list[CandidatePlace], int | None]:
    if not places:
        return [], None
    removed = min(
        places,
        key=lambda place: (
            place.recommend_score,
            place.effective_score,
            -place.place_id,
        ),
    )
    return (
        [
            place for place in places
            if place.place_id != removed.place_id
        ],
        removed.place_id,
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


def _finalize_last_stop(
    ordered: list[CandidatePlace],
    accommodation_coord: tuple[float, float] | None,
) -> list[CandidatePlace]:
    """Move the final activity toward the accommodation when safe."""
    if not accommodation_coord or len(ordered) < 3:
        return ordered

    last = ordered[-1]
    if is_food_place(last):
        return ordered
    dist_last = _anchor_distance_km(last, accommodation_coord)
    if dist_last <= 5.0:
        return ordered

    for index in (-2, -3):
        if len(ordered) + index < 0:
            continue
        candidate = ordered[index]
        if is_food_place(candidate):
            continue
        if _anchor_distance_km(candidate, accommodation_coord) < dist_last:
            ordered[index], ordered[-1] = ordered[-1], ordered[index]
            return ordered

    return ordered


def _record_accommodation_anchor_metrics(
    metrics: RoutePlanningMetrics,
    plans: list[RoutePlan],
    accommodation_coord: tuple[float, float] | None,
) -> None:
    metrics.accommodation_anchor_applied_day_count = 0
    metrics.accommodation_anchor_fallback_day_count = 0
    if accommodation_coord is None:
        return

    for route_plan in plans:
        for day_group in route_plan.day_groups:
            if not day_group.places:
                continue
            top3 = sorted(
                day_group.places,
                key=lambda place: (
                    place.effective_score,
                    place.recommend_score,
                    -place.place_id,
                ),
                reverse=True,
            )[:3]
            top3_ids = {place.place_id for place in top3}
            first = day_group.places[0]
            if (
                first.place_id in top3_ids
                and _anchor_distance_km(first, accommodation_coord) <= 5.0
            ):
                metrics.accommodation_anchor_applied_day_count += 1
            else:
                metrics.accommodation_anchor_fallback_day_count += 1


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


def _split_balanced(
    places: list[CandidatePlace],
    chunk_count: int,
) -> list[list[CandidatePlace]]:
    base, remainder = divmod(len(places), chunk_count)
    chunks = []
    cursor = 0
    for index in range(chunk_count):
        size = base + (1 if index < remainder else 0)
        chunks.append(places[cursor:cursor + size])
        cursor += size
    return chunks


def _split_with_daytime_coverage(
    places: list[CandidatePlace],
    chunk_count: int,
) -> list[list[CandidatePlace]]:
    """Balance a district while spreading daytime activities across days."""
    base, remainder = divmod(len(places), chunk_count)
    target_sizes = [
        base + (1 if index < remainder else 0)
        for index in range(chunk_count)
    ]
    chunks: list[list[CandidatePlace]] = [
        [] for _ in range(chunk_count)
    ]
    daytime = [
        place for place in places
        if _is_daytime_activity(place)
    ]
    seeded = daytime[:chunk_count]
    for index, place in enumerate(seeded):
        chunks[index].append(place)
    seeded_ids = {place.place_id for place in seeded}
    remaining = [
        place for place in places
        if place.place_id not in seeded_ids
    ]
    for place in remaining:
        target_index = min(
            (
                index
                for index, chunk in enumerate(chunks)
                if len(chunk) < target_sizes[index]
            ),
            key=lambda index: (
                len(chunks[index]) / max(target_sizes[index], 1),
                index,
            ),
        )
        chunks[target_index].append(place)
    return chunks


def _district_chunks(
    candidates: list[CandidatePlace],
    *,
    days: int,
    max_places: int,
    accommodation_coord: tuple[float, float] | None = None,
) -> list[tuple[str, list[CandidatePlace]]]:
    clusters: dict[str, list[CandidatePlace]] = defaultdict(list)
    for candidate in candidates:
        if candidate.adcode:
            clusters[candidate.adcode].append(candidate)

    ranked_clusters = sorted(
        clusters.items(),
        key=lambda item: (
            -len(item[1]),
            -max(place.effective_score for place in item[1]),
            item[0],
        ),
    )
    chunks: list[tuple[str, list[CandidatePlace]]] = []
    for adcode, cluster in ranked_clusters:
        if len(cluster) < 2:
            continue
        ordered = _nearest_neighbor(cluster, accommodation_coord)
        chunk_count = max(1, math.ceil(len(ordered) / max_places))
        for chunk in _split_with_daytime_coverage(ordered, chunk_count):
            if len(chunk) >= 2:
                chunks.append((adcode, chunk))

    while len(chunks) < days:
        split_index = next(
            (
                index
                for index, (_, chunk) in sorted(
                    enumerate(chunks),
                    key=lambda item: (-len(item[1][1]), item[0]),
                )
                if len(chunk) >= 4
            ),
            None,
        )
        if split_index is None:
            break
        adcode, chunk = chunks.pop(split_index)
        left, right = _split_balanced(chunk, 2)
        chunks.insert(split_index, (adcode, right))
        chunks.insert(split_index, (adcode, left))

    ranked_chunks = sorted(
        enumerate(chunks),
        key=lambda item: (
            not any(
                _is_daytime_activity(place)
                for place in item[1][1]
            ),
            item[0],
        ),
    )
    return [chunk for _, chunk in ranked_chunks[:days]]


def _fallback_chunks(
    candidates: list[CandidatePlace],
    *,
    days: int,
    max_places: int,
) -> list[tuple[str | None, list[CandidatePlace]]]:
    ranked = sorted(
        candidates,
        key=lambda place: (
            -place.effective_score,
            -place.recommend_score,
            place.place_id,
        ),
    )[: days * max_places]
    day_count = min(days, max(1, len(ranked) // 2))
    if day_count < 1:
        return []
    chunks = [[] for _ in range(day_count)]
    for index, candidate in enumerate(ranked):
        chunks[index % day_count].append(candidate)
    return [(None, chunk) for chunk in chunks if len(chunk) >= 2]


def _route_minutes(
    places: list[CandidatePlace],
    *,
    generation_mode: EffectiveCommuteMode,
) -> int:
    if len(places) < 2:
        return 0
    return sum(
        leg.duration_minutes
        for leg in _legs(
            _nearest_neighbor(places),
            generation_mode=generation_mode,
        )
    )


def _coordinate_chunks(
    candidates: list[CandidatePlace],
    *,
    days: int,
    max_places: int,
    generation_mode: EffectiveCommuteMode,
    food_focused: bool = False,
    accommodation_coord: tuple[float, float] | None = None,
) -> list[tuple[str | None, list[CandidatePlace]]]:
    """Build routeable day chunks from coordinates when district grouping is unavailable."""
    if days < 1:
        return []
    ranked = sorted(
        candidates,
        key=lambda place: (
            -_is_daytime_activity(place),
            -place.effective_score,
            -place.recommend_score,
            place.place_id,
        ),
    )[: days * max_places]
    if len(ranked) < 2:
        return []

    day_count = min(days, max(1, len(ranked) // 2))
    chunks: list[list[CandidatePlace]] = [[] for _ in range(day_count)]
    remaining = list(ranked)

    daytime_seeds = [
        place for place in remaining
        if _is_daytime_activity(place)
    ]
    for index, seed in enumerate(daytime_seeds[:day_count]):
        chunks[index].append(seed)
        remaining.remove(seed)

    for index in range(day_count):
        if chunks[index] or not remaining:
            continue
        seed = remaining.pop(0)
        chunks[index].append(seed)

    if food_focused:
        for chunk in chunks:
            if not chunk or len(chunk) >= max_places:
                continue
            if any(is_food_place(place) for place in chunk):
                continue
            food_candidates = [
                place for place in remaining
                if is_food_place(place)
            ]
            if not food_candidates:
                continue
            anchor = chunk[0]
            food = min(
                food_candidates,
                key=lambda place: (
                    haversine_km(anchor, place),
                    -place.effective_score,
                    -place.recommend_score,
                    place.place_id,
                ),
            )
            chunk.append(food)
            remaining.remove(food)

    for candidate in remaining:
        available = [
            (index, chunk)
            for index, chunk in enumerate(chunks)
            if len(chunk) < max_places
        ]
        if not available:
            break
        undersized = [
            item for item in available
            if len(item[1]) < 2
        ]
        if undersized:
            target_index, _ = min(
                undersized,
                key=lambda item: (len(item[1]), item[0]),
            )
            chunks[target_index].append(candidate)
            continue

        def assignment_cost(item: tuple[int, list[CandidatePlace]]) -> tuple[float, int, int]:
            index, chunk = item
            if not chunk:
                return (0.0, 0, index)
            current_minutes = _route_minutes(
                chunk,
                generation_mode=generation_mode,
            )
            new_minutes = _route_minutes(
                [*chunk, candidate],
                generation_mode=generation_mode,
            )
            return (new_minutes - current_minutes, len(chunk), index)

        target_index, _ = min(available, key=assignment_cost)
        chunks[target_index].append(candidate)

    result = []
    for chunk in chunks:
        if len(chunk) < 2:
            continue
        ordered = _nearest_neighbor(chunk, accommodation_coord)
        adcode = _shared_adcode(ordered)
        result.append((adcode, ordered))
    return result


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


def _trim_to_budget(
    places: list[CandidatePlace],
    *,
    daily_budget: int,
    single_leg_max: int,
    min_places: int,
    generation_mode: EffectiveCommuteMode,
    accommodation_coord: tuple[float, float] | None = None,
) -> tuple[list[CandidatePlace], list[int]]:
    current = _nearest_neighbor(places, accommodation_coord)
    dropped: list[int] = []
    while len(current) >= min_places:
        current_legs = _legs(current, generation_mode=generation_mode)
        if _within_budget(
            current_legs,
            daily_budget=daily_budget,
            single_leg_max=single_leg_max,
        ):
            return current, dropped
        if len(current) == min_places:
            return [], dropped + [place.place_id for place in current]

        current_minutes = sum(leg.duration_minutes for leg in current_legs)
        choices = []
        for index, candidate in enumerate(current):
            reduced = _nearest_neighbor(
                current[:index] + current[index + 1:],
                accommodation_coord,
            )
            reduced_legs = _legs(reduced, generation_mode=generation_mode)
            reduction = current_minutes - sum(
                leg.duration_minutes for leg in reduced_legs
            )
            hard_improvement = (
                max((leg.duration_minutes for leg in current_legs), default=0)
                - max((leg.duration_minutes for leg in reduced_legs), default=0)
            )
            choices.append(
                (
                    hard_improvement,
                    reduction,
                    -candidate.effective_score,
                    -candidate.recommend_score,
                    -candidate.place_id,
                    candidate,
                    reduced,
                )
            )
        _, _, _, _, _, removed, current = max(choices, key=lambda item: item[:5])
        dropped.append(removed.place_id)
    return [], dropped


def _time_hints(
    places: list[CandidatePlace],
    request: TripRequest | None = None,
) -> list[str]:
    if _suppress_evening_markers(request):
        return []
    hints = []
    for place in places:
        evidence = " ".join(
            str(item.get("reason", ""))
            for item in [*place.top_reasons, *place.warnings]
            if isinstance(item, dict)
        )
        if any(marker in f"{place.name} {evidence}" for marker in _EVENING_MARKERS):
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
    evidence = " ".join(
        str(item.get("reason", ""))
        for item in [*place.top_reasons, *place.warnings]
        if isinstance(item, dict)
    )
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


def _is_evening_only_day(
    places: list[CandidatePlace],
    request: TripRequest | None = None,
) -> bool:
    activity_places = [
        place
        for place in places
        if not is_food_place(place)
    ]
    return bool(activity_places) and not any(
        _is_daytime_activity(place, request=request)
        for place in activity_places
    )


def _time_ordered_places(
    places: list[CandidatePlace],
    request: TripRequest | None = None,
    *,
    accommodation_coord: tuple[float, float] | None = None,
) -> list[CandidatePlace]:
    if _suppress_evening_markers(request):
        return (
            _nearest_neighbor(places, accommodation_coord)
            if len(places) > 1
            else list(places)
        )
    daytime = [
        place for place in places
        if not _scheduled_evening_stop(place, request)
    ]
    evening = [
        place for place in places
        if _scheduled_evening_stop(place, request)
    ]
    if len(evening) > 1:
        evening = _nearest_neighbor(evening, accommodation_coord)
    # The day's evening stop closes the day; nothing long runs after it.
    evening_id = _evening_stop_id(evening, request)
    evening = [
        *(place for place in evening if place.place_id != evening_id),
        *(place for place in evening if place.place_id == evening_id),
    ]
    return [
        *(
            _nearest_neighbor(daytime, accommodation_coord)
            if len(daytime) > 1
            else daytime
        ),
        *evening,
    ]


def _rebuild_day_group(
    day_group: RouteDayGroup,
    places: list[CandidatePlace],
    *,
    optimized: bool,
    daily_budget: int,
    single_leg_max: int,
    min_places: int,
    max_places: int,
    district_names: dict[str, str] | None,
    request: TripRequest | None = None,
    accommodation_coord: tuple[float, float] | None = None,
) -> RouteDayGroup | None:
    if not min_places <= len(places) <= max_places:
        return None
    if optimized and day_group.adcode and any(
        place.adcode != day_group.adcode
        for place in places
    ):
        return None
    ordered = _time_ordered_places(
        places,
        request=request,
        accommodation_coord=accommodation_coord,
    )
    settings = get_settings()
    route_generation_mode: EffectiveCommuteMode = (
        generation_base_mode(request, settings)[0]
        if request is not None
        else "driving"
    )
    legs = (
        _legs(ordered, generation_mode=route_generation_mode)
        if optimized
        else []
    )
    if optimized and not _within_budget(
        legs,
        daily_budget=daily_budget,
        single_leg_max=single_leg_max,
    ):
        return None
    return RouteDayGroup(
        day=day_group.day,
        area=(district_names or {}).get(
            day_group.adcode or "",
            day_group.area,
        ),
        adcode=day_group.adcode,
        places=ordered,
        commute_legs=legs,
        commute_minutes=sum(leg.duration_minutes for leg in legs),
        commute_notes=[leg.note for leg in legs],
        time_hints=_time_hints(ordered, request=request),
    )


def _route_day_invariant_violations(
    day_groups: list[RouteDayGroup],
    *,
    optimized: bool,
    min_places: int,
    max_places: int,
    request: TripRequest | None = None,
) -> list[str]:
    violations = []
    seen_place_ids: set[int] = set()
    for day_group in day_groups:
        admitted_single = (
            len(day_group.places) == 1
            and day_group.day_feasibility is not None
            and day_group.day_feasibility.feasible
            and day_group.day_feasibility.singleton_eligible
            and day_group.day_feasibility.ordered_place_ids == [day_group.places[0].place_id]
        )
        if not admitted_single and not min_places <= len(day_group.places) <= max_places:
            violations.append(
                f"day {day_group.day} place count is out of bounds"
            )
        for place in day_group.places:
            if place.place_id in seen_place_ids:
                violations.append(
                    f"place {place.place_id} is assigned to multiple days"
                )
            seen_place_ids.add(place.place_id)
            if (
                optimized
                and day_group.adcode
                and place.adcode != day_group.adcode
            ):
                violations.append(
                    f"place {place.place_id} adcode does not match day "
                    f"{day_group.day}"
                )
        if optimized and _is_evening_only_day(day_group.places, request=request):
            violations.append(
                f"day {day_group.day} has no daytime activity"
            )
        if optimized and not any(
            not is_food_place(place)
            for place in day_group.places
        ):
            violations.append(
                f"day {day_group.day} has no activity"
            )
    return violations


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


def _core_route_slot_candidates(
    candidates: list[CandidatePlace],
    *,
    request: TripRequest,
) -> list[CandidatePlace]:
    return [
        candidate for candidate in candidates
        if _is_core_route_slot_candidate(candidate, request=request)
    ]


def _supplement_food_places(
    day_groups: list[RouteDayGroup],
    candidates: list[CandidatePlace],
    *,
    preferences: list[str],
    optimized: bool,
    daily_budget: int,
    single_leg_max: int,
    min_places: int,
    max_places: int,
    district_names: dict[str, str] | None,
    request: TripRequest | None = None,
    accommodation_coord: tuple[float, float] | None = None,
) -> list[RouteDayGroup]:
    """Add nearby food POIs for food-focused trips without breaking commute budgets."""
    if not optimized or not _food_focused(preferences):
        return day_groups

    assigned_ids = {
        place.place_id
        for day_group in day_groups
        for place in day_group.places
    }
    target_food_total = min(
        len(day_groups),
        sum(1 for place in candidates if is_food_place(place)),
    )
    current_food_total = sum(
        1
        for day_group in day_groups
        for place in day_group.places
        if is_food_place(place)
    )
    if current_food_total >= target_food_total:
        return day_groups

    available_food = sorted(
        (
            place for place in candidates
            if place.place_id not in assigned_ids
            and is_food_place(place)
            and _has_coordinates(place)
        ),
        key=lambda place: (
            -place.effective_score,
            -place.recommend_score,
            place.place_id,
        ),
    )
    retained = list(day_groups)
    while current_food_total < target_food_total and available_food:
        choices = []
        for day_index, day_group in enumerate(retained):
            if len(day_group.places) >= max_places:
                continue
            if sum(1 for place in day_group.places if is_food_place(place)) >= 3:
                continue
            current_minutes = sum(
                leg.duration_minutes
                for leg in _legs(
                    _time_ordered_places(
                        day_group.places,
                        request=request,
                        accommodation_coord=accommodation_coord,
                    ),
                    generation_mode=(
                        generation_base_mode(request, get_settings())[0]
                        if request is not None
                        else "driving"
                    ),
                )
            )
            for food in available_food:
                rebuilt = _rebuild_day_group(
                    day_group,
                    [*day_group.places, food],
                    optimized=True,
                    daily_budget=daily_budget,
                    single_leg_max=single_leg_max,
                    min_places=min_places,
                    max_places=max_places,
                    district_names=district_names,
                    request=request,
                    accommodation_coord=accommodation_coord,
                )
                if rebuilt is None:
                    continue
                added_minutes = rebuilt.commute_minutes - current_minutes
                choices.append((
                    added_minutes,
                    -food.effective_score,
                    -food.recommend_score,
                    day_index,
                    food,
                    rebuilt,
                ))
        if not choices:
            break
        _, _, _, day_index, food, rebuilt = min(choices, key=lambda item: item[:4])
        retained[day_index] = rebuilt
        assigned_ids.add(food.place_id)
        available_food = [
            candidate for candidate in available_food
            if candidate.place_id != food.place_id
        ]
        current_food_total += 1
    return retained


def _shared_adcode(places: list[CandidatePlace]) -> str | None:
    adcodes = {place.adcode for place in places}
    return next(iter(adcodes)) if None not in adcodes and len(adcodes) == 1 else None


def _fill_missing_coordinate_days(
    day_groups: list[RouteDayGroup],
    candidates: list[CandidatePlace],
    *,
    target_days: int,
    optimized: bool,
    daily_budget: int,
    single_leg_max: int,
    min_places: int,
    max_places: int,
    district_names: dict[str, str] | None,
    request: TripRequest | None = None,
    accommodation_coord: tuple[float, float] | None = None,
) -> list[RouteDayGroup]:
    """Recover compact coordinate-routed days from unassigned candidates."""
    if not optimized or len(day_groups) >= target_days:
        return day_groups

    retained = list(day_groups)
    while len(retained) < target_days:
        assigned_ids = {
            place.place_id
            for day_group in retained
            for place in day_group.places
        }
        unused = [
            place for place in candidates
            if place.place_id not in assigned_ids
            and _has_coordinates(place)
            and (
                request is None
                or _is_core_route_slot_candidate(place, request=request)
            )
        ]
        if len(unused) < min_places:
            break

        seeds = sorted(
            (
                place for place in unused
                if _is_daytime_activity(place, request=request)
            ),
            key=lambda place: (
                -place.effective_score,
                -place.recommend_score,
                place.place_id,
            ),
        )
        choices = []
        for seed in seeds:
            neighbors = sorted(
                (
                    place for place in unused
                    if place.place_id != seed.place_id
                ),
                key=lambda place: (
                    haversine_km(seed, place),
                    -place.effective_score,
                    -place.recommend_score,
                    place.place_id,
                ),
            )
            pool = [seed, *neighbors[: max_places - 1]]
            for size in range(min(max_places, len(pool)), min_places - 1, -1):
                places = balance_day_types(
                    pool[:size],
                    request.preferences if request is not None else [],
                )
                places = enforce_day_weight_budget(places, request=request)
                if len(places) < min_places:
                    continue
                if not validate_day_time_budget(places, request=request):
                    continue
                if not any(not is_food_place(place) for place in places):
                    continue
                places = arrange_places_with_meal_slots(
                    places,
                    detect_food_intensity(
                        request.preferences if request is not None else []
                    ),
                )
                adcode = _shared_adcode(places)
                template = RouteDayGroup(
                    day=len(retained) + 1,
                    area=(district_names or {}).get(
                        adcode or "",
                        adcode or "坐标聚类路线",
                    ),
                    adcode=adcode,
                )
                rebuilt = _rebuild_day_group(
                    template,
                    places,
                    optimized=True,
                    daily_budget=daily_budget,
                    single_leg_max=single_leg_max,
                    min_places=min_places,
                    max_places=max_places,
                    district_names=district_names,
                    request=request,
                    accommodation_coord=accommodation_coord,
                )
                if rebuilt is None:
                    continue
                choices.append((
                    -len(rebuilt.places),
                    rebuilt.commute_minutes,
                    -sum(place.effective_score for place in rebuilt.places),
                    seed.place_id,
                    rebuilt,
                ))
        if not choices:
            break
        *_, rebuilt = min(choices, key=lambda item: item[:4])
        retained.append(rebuilt)
    return retained


def _avoid_conflicts_with_place(
    request: TripRequest,
    place: CandidatePlace,
) -> bool:
    for avoid in request.avoid:
        marker = avoid.strip()
        if marker and marker in _route_quality_text(place):
            return True
    return False


def _route_anchor_places(
    request: TripRequest,
    candidates: list[CandidatePlace],
) -> list[CandidatePlace]:
    if not request.must_include:
        return []
    requested_order = {
        item.place_id: index
        for index, item in enumerate(request.must_include)
        if item.place_id is not None
    }
    anchors = [
        candidate for candidate in candidates
        if candidate.must_include
        and _has_coordinates(candidate)
        and not _avoid_conflicts_with_place(request, candidate)
    ]
    return sorted(
        anchors,
        key=lambda place: (
            requested_order.get(place.place_id, len(requested_order)),
            -place.effective_score,
            -place.recommend_score,
            place.place_id,
        ),
    )


def _chunk_anchor_count(
    chunks: list[tuple[str | None, list[CandidatePlace]]],
) -> int:
    return sum(
        1
        for _, chunk in chunks
        for place in chunk
        if place.must_include
    )


def _choose_anchor_aware_chunks(
    normal_chunks: list[tuple[str | None, list[CandidatePlace]]],
    anchor_chunks: list[tuple[str | None, list[CandidatePlace]]],
    *,
    target_days: int,
    required_anchor_ids: set[int] | None = None,
) -> list[tuple[str | None, list[CandidatePlace]]]:
    if not anchor_chunks:
        return [] if required_anchor_ids is not None else normal_chunks
    if required_anchor_ids is not None:
        # Selection membership may bypass later legacy trimming only when its
        # chunks came through the feasibility-checking anchor builder.
        return anchor_chunks
    normal_complete = len(normal_chunks) >= target_days
    anchor_complete = len(anchor_chunks) >= target_days
    if normal_complete and not anchor_complete:
        return normal_chunks
    if anchor_complete and not normal_complete:
        return anchor_chunks
    if (
        anchor_complete == normal_complete
        and _chunk_anchor_count(anchor_chunks) > _chunk_anchor_count(normal_chunks)
    ):
        return anchor_chunks
    return normal_chunks


def _anchor_seed_distance(
    seed: list[CandidatePlace],
    candidate: CandidatePlace,
) -> float:
    return min(haversine_km(place, candidate) for place in seed)


def _anchor_day_group_from_places(
    places: list[CandidatePlace],
    *,
    day: int,
    daily_budget: int,
    single_leg_max: int,
    min_places: int,
    max_places: int,
    district_names: dict[str, str] | None,
    request: TripRequest,
    accommodation_coord: tuple[float, float] | None = None,
) -> RouteDayGroup | None:
    if not min_places <= len(places) <= max_places:
        return None
    if calculate_day_weight(places) > _day_weight_budget(request):
        return None
    if not validate_day_time_budget(places, request=request):
        return None
    adcode = _shared_adcode(places)
    template = RouteDayGroup(
        day=day,
        area=(district_names or {}).get(adcode or "", adcode or "锚点候选路线"),
        adcode=adcode,
    )
    return _rebuild_day_group(
        template,
        places,
        optimized=True,
        daily_budget=daily_budget,
        single_leg_max=single_leg_max,
        min_places=min_places,
        max_places=max_places,
        district_names=district_names,
        request=request,
        accommodation_coord=accommodation_coord,
    )


def _anchor_fill_candidate_key(
    seed: list[CandidatePlace],
    candidate: CandidatePlace,
    request: TripRequest,
) -> tuple:
    seed_adcode = _shared_adcode(seed)
    same_adcode_penalty = (
        0
        if seed_adcode is None or candidate.adcode == seed_adcode
        else 1
    )
    nature_penalty = (
        0
        if not _nature_preference_active(request) or _is_nature_core_place(candidate)
        else 1
    )
    return (
        same_adcode_penalty,
        _anchor_seed_distance(seed, candidate),
        nature_penalty,
        -candidate.effective_score,
        -candidate.recommend_score,
        candidate.place_id,
    )


def _best_anchor_day_addition(
    seed: list[CandidatePlace],
    available: list[CandidatePlace],
    *,
    day: int,
    daily_budget: int,
    single_leg_max: int,
    min_places: int,
    max_places: int,
    district_names: dict[str, str] | None,
    request: TripRequest,
    accommodation_coord: tuple[float, float] | None = None,
) -> tuple[CandidatePlace, RouteDayGroup] | None:
    choices = []
    for candidate in available:
        if candidate.place_id in {place.place_id for place in seed}:
            continue
        places = [*seed, candidate]
        rebuilt = _anchor_day_group_from_places(
            places,
            day=day,
            daily_budget=daily_budget,
            single_leg_max=single_leg_max,
            min_places=min_places,
            max_places=max_places,
            district_names=district_names,
            request=request,
            accommodation_coord=accommodation_coord,
        )
        if rebuilt is None:
            continue
        choices.append((
            _anchor_fill_candidate_key(seed, candidate, request),
            candidate,
            rebuilt,
        ))
    if not choices:
        return None
    _, candidate, rebuilt = min(choices, key=lambda item: item[0])
    return candidate, rebuilt


def _build_anchor_seed_chunks(
    seeds: list[list[CandidatePlace]],
    candidates: list[CandidatePlace],
    *,
    daily_budget: int,
    single_leg_max: int,
    min_places: int,
    max_places: int,
    district_names: dict[str, str] | None,
    request: TripRequest,
    target_places: int | None = None,
    accommodation_coord: tuple[float, float] | None = None,
    must_supplement_pool: list[CandidatePlace] | None = None,
) -> list[tuple[str | None, list[CandidatePlace]]]:
    retained: list[tuple[str | None, list[CandidatePlace]]] = []
    used_ids = {
        place.place_id
        for seed in seeds
        for place in seed
    }
    available = [
        candidate for candidate in candidates
        if candidate.place_id not in used_ids
        and _has_coordinates(candidate)
    ]
    target_places = min(
        max_places,
        target_places
        if target_places is not None
        else _effective_route_places_per_day_capacity(
            request,
            max_places=max_places,
        ),
    )
    for seed in seeds:
        day = len(retained) + 1
        current = list(seed)
        rebuilt: RouteDayGroup | None = None
        while len(current) < min_places:
            addition = _best_anchor_day_addition(
                current,
                available,
                day=day,
                daily_budget=daily_budget,
                single_leg_max=single_leg_max,
                min_places=min_places,
                max_places=max_places,
                district_names=district_names,
                request=request,
                accommodation_coord=accommodation_coord,
            )
            if addition is None and any(place.must_include for place in current):
                # Try qualified partners before a short required seed is lost.
                # Ordinary seeds retain the old admission and filling path.
                addition = _best_anchor_day_addition(
                    current,
                    [
                        place for place in (must_supplement_pool or [])
                        if place.place_id not in used_ids
                    ],
                    day=day,
                    daily_budget=daily_budget,
                    single_leg_max=single_leg_max,
                    min_places=min_places,
                    max_places=max_places,
                    district_names=district_names,
                    request=request,
                    accommodation_coord=accommodation_coord,
                )
            if addition is None:
                break
            candidate, rebuilt = addition
            current = rebuilt.places
            used_ids.add(candidate.place_id)
            available = [
                place for place in available
                if place.place_id != candidate.place_id
            ]
        if len(current) < min_places:
            continue
        rebuilt = _anchor_day_group_from_places(
            current,
            day=day,
            daily_budget=daily_budget,
            single_leg_max=single_leg_max,
            min_places=min_places,
            max_places=max_places,
            district_names=district_names,
            request=request,
            accommodation_coord=accommodation_coord,
        )
        if rebuilt is None:
            continue
        current = rebuilt.places
        while len(current) < target_places:
            addition = _best_anchor_day_addition(
                current,
                available,
                day=day,
                daily_budget=daily_budget,
                single_leg_max=single_leg_max,
                min_places=min_places,
                max_places=max_places,
                district_names=district_names,
                request=request,
                accommodation_coord=accommodation_coord,
            )
            if addition is None:
                break
            candidate, rebuilt = addition
            current = rebuilt.places
            used_ids.add(candidate.place_id)
            available = [
                place for place in available
                if place.place_id != candidate.place_id
            ]
        retained.append((rebuilt.adcode, rebuilt.places))
    return retained


def _anchor_aware_route_chunks(
    candidates: list[CandidatePlace],
    *,
    request: TripRequest,
    days: int,
    max_places: int,
    daily_budget: int,
    single_leg_max: int,
    min_places: int,
    district_names: dict[str, str] | None,
    prefer_district_chunks: bool,
    food_focused: bool,
    accommodation_coord: tuple[float, float] | None = None,
    required_anchor_ids: list[int] | None = None,
    preferred_anchor_ids: list[int] | None = None,
    must_supplement_pool: list[CandidatePlace] | None = None,
) -> list[tuple[str | None, list[CandidatePlace]]]:
    if preferred_anchor_ids is not None:
        candidates_by_id = {candidate.place_id: candidate for candidate in candidates}
        anchors = [
            candidates_by_id[place_id]
            for place_id in preferred_anchor_ids
            if place_id in candidates_by_id
            and _has_coordinates(candidates_by_id[place_id])
        ]
    elif required_anchor_ids is None:
        anchors = _route_anchor_places(request, candidates)
    else:
        candidates_by_id = {candidate.place_id: candidate for candidate in candidates}
        anchors = [
            candidates_by_id[place_id]
            for place_id in required_anchor_ids
            if place_id in candidates_by_id
            and _has_coordinates(candidates_by_id[place_id])
        ]
    if days < 1 or not anchors:
        return []

    seed_max_places = max_places
    if preferred_anchor_ids is not None and len(anchors) >= days * min_places:
        # Ranked selection owns one complete structure. Avoid greedily filling
        # early colocated days to the global maximum when the same admitted
        # set can deterministically cover every requested day.
        seed_max_places = min(
            max_places,
            max(min_places, math.ceil(len(anchors) / days)),
        )

    seeds: list[list[CandidatePlace]] = []
    for anchor in anchors:
        choices = []
        for index, seed in enumerate(seeds):
            if len(seed) >= seed_max_places:
                continue
            places = [*seed, anchor]
            if len(places) >= min_places and _anchor_day_group_from_places(
                places,
                day=index + 1,
                daily_budget=daily_budget,
                single_leg_max=single_leg_max,
                min_places=min_places,
                max_places=max_places,
                district_names=district_names,
                request=request,
                accommodation_coord=accommodation_coord,
            ) is None:
                continue
            seed_adcode = _shared_adcode(seed)
            choices.append((
                0 if seed_adcode and anchor.adcode == seed_adcode else 1,
                _anchor_seed_distance(seed, anchor),
                len(seed),
                index,
            ))
        if choices:
            *_, index = min(choices)
            seeds[index].append(anchor)
            continue
        if len(seeds) < days:
            seeds.append([anchor])

    anchor_chunks = _build_anchor_seed_chunks(
        seeds,
        candidates,
        daily_budget=daily_budget,
        single_leg_max=single_leg_max,
        min_places=min_places,
        max_places=max_places,
        district_names=district_names,
        request=request,
        accommodation_coord=accommodation_coord,
        must_supplement_pool=must_supplement_pool,
    )
    if not anchor_chunks:
        return []

    assigned_ids = {
        place.place_id
        for _, chunk in anchor_chunks
        for place in chunk
    }
    if required_anchor_ids is not None and not set(required_anchor_ids).issubset(
        assigned_ids
    ):
        return anchor_chunks
    remaining = [
        candidate for candidate in candidates
        if candidate.place_id not in assigned_ids
    ]
    remaining_days = max(0, days - len(anchor_chunks))
    if remaining_days:
        filler_max_places = min(
            max_places,
            _effective_route_places_per_day_capacity(
                request,
                max_places=max_places,
            ),
        )
        if prefer_district_chunks:
            filler_chunks = _district_chunks(
                remaining,
                days=remaining_days,
                max_places=filler_max_places,
                accommodation_coord=accommodation_coord,
            )
        else:
            filler_chunks = _coordinate_chunks(
                remaining,
                days=remaining_days,
                max_places=filler_max_places,
                generation_mode=generation_base_mode(
                    request,
                    get_settings(),
                )[0],
                food_focused=food_focused,
                accommodation_coord=accommodation_coord,
            )
        anchor_chunks = [
            *anchor_chunks,
            *filler_chunks[:remaining_days],
        ]
    return anchor_chunks[:days]


SUPPLEMENT_TARGET_RELAXED = 4
SUPPLEMENT_TARGET_NORMAL = 5
SUPPLEMENT_SAME_ADCODE_PREFERENCE_KM = 10.0


def _supplement_chunk_from_pool(
    chunk: list[CandidatePlace],
    supplement_pool: list[CandidatePlace],
    *,
    used_ids: set[int],
    adcode: str | None,
    target_places: int,
    daily_budget: int,
    single_leg_max: int,
    min_places: int,
    request: TripRequest,
    accommodation_coord: tuple[float, float] | None = None,
) -> list[CandidatePlace]:
    """Add activity-type POIs from the wider pool to fill a day chunk."""
    if len(chunk) >= target_places:
        return chunk
    chunk_center = None
    located = [p for p in chunk if _has_coordinates(p)]
    if located:
        chunk_center = (
            sum(float(p.latitude) for p in located) / len(located),
            sum(float(p.longitude) for p in located) / len(located),
        )
    food_focused = _food_focused(request.preferences)
    candidates = []
    for place in supplement_pool:
        if place.place_id in used_ids:
            continue
        if not _is_core_route_slot_candidate(place, request=request):
            continue
        if not _has_coordinates(place):
            continue
        if adcode is not None and place.adcode != adcode:
            continue
        if not _is_daytime_activity(place, request=request):
            continue
        pt = str(place.place_type or "").lower()
        if pt in ("restaurant", "cafe", "street") and not food_focused:
            continue
        if pt in MULTI_AREA_WEAK_TYPES:
            continue
        is_dup = any(_is_near_duplicate_pair(place, existing) for existing in chunk)
        if is_dup:
            continue
        dist = _distance_to_center(place, chunk_center) if chunk_center else math.inf
        if dist > SUPPLEMENT_SAME_ADCODE_PREFERENCE_KM:
            continue
        candidates.append((
            dist,
            -place.effective_score,
            -place.recommend_score,
            place.place_id,
            place,
        ))
    candidates.sort()
    initial_count = len(chunk)
    current = list(chunk)
    for *_, place in candidates:
        if len(current) >= target_places:
            break
        trial = [*current, place]
        trimmed, _ = _trim_to_budget(
            trial,
            daily_budget=daily_budget,
            single_leg_max=single_leg_max,
            min_places=min_places,
            generation_mode=generation_base_mode(
                request,
                get_settings(),
            )[0],
            accommodation_coord=accommodation_coord,
        )
        if len(trimmed) < len(trial):
            continue
        if not validate_day_time_budget(trimmed, request=request):
            continue
        current = trimmed
        used_ids.add(place.place_id)
    if len(current) > initial_count:
        logger.info(
            "Supplement: added %d places to chunk (was %d, now %d, target %d): %s",
            len(current) - initial_count,
            initial_count,
            len(current),
            target_places,
            [p.name for p in current[initial_count:]],
        )
    return current


def _build_candidate_route_plan(
    *,
    request: TripRequest,
    label: str,
    route_candidates: list[CandidatePlace],
    district_names: dict[str, str] | None = None,
    district_data_available: bool = True,
    supplement_pool: list[CandidatePlace] | None = None,
    accommodation_coord: tuple[float, float] | None = None,
    required_anchor_ids: list[int] | None = None,
    preferred_anchor_ids: list[int] | None = None,
    must_supplement_pool: list[CandidatePlace] | None = None,
) -> RoutePlan:
    """Build one deterministic route plan without external route API calls."""
    settings = get_settings()
    selected_route_mode = preferred_anchor_ids is not None
    daily_place_capacity = (
        _effective_route_places_per_day_capacity(
            request,
            max_places=settings.route_places_per_day_max,
        )
        if selected_route_mode
        else settings.route_places_per_day_max
    )
    days = max(1, request.days)
    route_generation_mode, _ = generation_base_mode(request, settings)
    daily_budget = daily_commute_budget_minutes(request, settings)
    single_leg_max = single_leg_max_minutes(route_generation_mode, settings)
    all_candidates = list(route_candidates)
    located_candidates = [
        candidate for candidate in all_candidates
        if _has_coordinates(candidate)
    ]
    candidates = _core_route_slot_candidates(
        located_candidates,
        request=request,
    )
    initial_dropped_ids = {
        candidate.place_id
        for candidate in all_candidates
        if (
            not _has_coordinates(candidate)
            or (
                candidate in located_candidates
                and not _is_core_route_slot_candidate(candidate, request=request)
            )
        )
    }
    has_coordinates = all(
        candidate.latitude is not None
        and candidate.longitude is not None
        for candidate in candidates
    )
    has_districts = all(candidate.adcode for candidate in candidates)
    if selected_route_mode:
        # Selected membership must use one feasibility-checked admission path.
        # Legacy remote pre-splitting can create an over-capacity day before
        # required anchors are considered.
        planning_candidates = candidates
        remote_chunks: list[tuple[str | None, list[CandidatePlace]]] = []
    else:
        planning_candidates, remote_chunks = _split_remote_day_trips(
            candidates,
            days=days,
            remote_context=" ".join([
                request.to_city,
                *request.preferences,
                request.notes,
            ]),
            accommodation_coord=accommodation_coord,
        )
    planning_candidate_ids = {
        candidate.place_id for candidate in planning_candidates
    }
    urban_required_anchor_ids = (
        [
            place_id for place_id in required_anchor_ids
            if place_id in planning_candidate_ids
        ]
        if selected_route_mode
        else None
    )
    urban_preferred_anchor_ids = (
        [
            place_id for place_id in preferred_anchor_ids
            if place_id in planning_candidate_ids
        ]
        if selected_route_mode
        else None
    )
    urban_days = max(0, days - len(remote_chunks))
    route_quality_enabled = bool(
        getattr(settings, "route_quality_selection_enabled", False)
    )
    if has_coordinates and has_districts and district_data_available:
        urban_chunks = _district_chunks(
            planning_candidates,
            days=urban_days,
            max_places=daily_place_capacity,
            accommodation_coord=accommodation_coord,
        )
        if route_quality_enabled or urban_preferred_anchor_ids:
            urban_chunks = _choose_anchor_aware_chunks(
                urban_chunks,
                _anchor_aware_route_chunks(
                    planning_candidates,
                    request=request,
                    days=urban_days,
                    max_places=daily_place_capacity,
                    daily_budget=daily_budget,
                    single_leg_max=single_leg_max,
                    min_places=settings.route_places_per_day_min,
                    district_names=district_names,
                    prefer_district_chunks=True,
                    food_focused=_food_focused(request.preferences),
                    accommodation_coord=accommodation_coord,
                    required_anchor_ids=urban_required_anchor_ids,
                    preferred_anchor_ids=urban_preferred_anchor_ids,
                    must_supplement_pool=must_supplement_pool,
                ),
                target_days=urban_days,
                required_anchor_ids=(
                    set(urban_required_anchor_ids)
                    if urban_required_anchor_ids is not None
                    else None
                ),
            )
        raw_chunks = [*remote_chunks, *urban_chunks]
        optimized = True
        fallback_reason = None
    elif has_coordinates:
        urban_chunks = _coordinate_chunks(
            planning_candidates,
            days=urban_days,
            max_places=daily_place_capacity,
            generation_mode=route_generation_mode,
            food_focused=_food_focused(request.preferences),
            accommodation_coord=accommodation_coord,
        )
        if route_quality_enabled or urban_preferred_anchor_ids:
            urban_chunks = _choose_anchor_aware_chunks(
                urban_chunks,
                _anchor_aware_route_chunks(
                    planning_candidates,
                    request=request,
                    days=urban_days,
                    max_places=daily_place_capacity,
                    daily_budget=daily_budget,
                    single_leg_max=single_leg_max,
                    min_places=settings.route_places_per_day_min,
                    district_names=district_names,
                    prefer_district_chunks=False,
                    food_focused=_food_focused(request.preferences),
                    accommodation_coord=accommodation_coord,
                    required_anchor_ids=urban_required_anchor_ids,
                    preferred_anchor_ids=urban_preferred_anchor_ids,
                    must_supplement_pool=must_supplement_pool,
                ),
                target_days=urban_days,
                required_anchor_ids=(
                    set(urban_required_anchor_ids)
                    if urban_required_anchor_ids is not None
                    else None
                ),
            )
        raw_chunks = [*remote_chunks, *urban_chunks]
        optimized = True
        fallback_reason = None
    else:
        raw_chunks = _fallback_chunks(
            planning_candidates,
            days=urban_days,
            max_places=daily_place_capacity,
        )
        raw_chunks = [*remote_chunks, *raw_chunks]
        optimized = False
        fallback_reason = "missing_location_data"

    day_groups = []
    assigned_chunk_ids = {
        place.place_id
        for _, chunk in raw_chunks
        for place in chunk
    }
    if supplement_pool and optimized:
        supplement_target = _effective_route_places_per_day_capacity(
            request,
            max_places=daily_place_capacity,
        )
        supplemented_chunks = []
        for adcode, chunk in raw_chunks:
            chunk = _supplement_chunk_from_pool(
                chunk,
                supplement_pool,
                used_ids=assigned_chunk_ids,
                adcode=adcode,
                target_places=supplement_target,
                daily_budget=daily_budget,
                single_leg_max=single_leg_max,
                min_places=settings.route_places_per_day_min,
                request=request,
                accommodation_coord=accommodation_coord,
            )
            supplemented_chunks.append((adcode, chunk))
        raw_chunks = supplemented_chunks
    dropped_ids = {
        candidate.place_id
        for candidate in all_candidates
        if all(
            candidate.place_id not in {place.place_id for place in chunk}
            for _, chunk in raw_chunks
        )
    } | initial_dropped_ids
    for adcode, chunk in raw_chunks:
        preserve_selected_membership = selected_route_mode
        if optimized:
            kept, dropped = _trim_to_budget(
                chunk,
                daily_budget=daily_budget,
                single_leg_max=single_leg_max,
                min_places=settings.route_places_per_day_min,
                generation_mode=route_generation_mode,
                accommodation_coord=accommodation_coord,
            )
        else:
            kept = chunk[:daily_place_capacity]
            dropped = [
                place.place_id
                for place in chunk[daily_place_capacity:]
            ]
        dropped_ids.update(dropped)
        if len(kept) < settings.route_places_per_day_min:
            dropped_ids.update(place.place_id for place in kept)
            continue

        if not preserve_selected_membership:
            # Legacy grouped routes retain their historical type/weight/time
            # trimming. Selected chunks were already proven feasible by the
            # anchor builder and must not lose admitted membership here.
            kept = balance_day_types(kept, request.preferences)
            kept = enforce_day_weight_budget(kept, request=request)
            while (
                not validate_day_time_budget(kept, request=request)
                and len(kept) > settings.route_places_per_day_min
            ):
                kept, removed_id = _drop_lowest_scoring_place(kept)
                if removed_id is not None:
                    dropped_ids.add(removed_id)

        if len(kept) < settings.route_places_per_day_min:
            dropped_ids.update(place.place_id for place in kept)
            continue

        # v0.6.6: Arrange places with meal slots
        food_intensity = detect_food_intensity(request.preferences)
        kept = arrange_places_with_meal_slots(kept, food_intensity)
        if not any(not is_food_place(place) for place in kept):
            if not preserve_selected_membership:
                dropped_ids.update(place.place_id for place in kept)
                continue
        kept = _finalize_last_stop(kept, accommodation_coord)

        # v0.9.7 Track B: Re-apply geographic ordering after balance/budget/meal
        # steps that may have disrupted the original _trim_to_budget order.
        if optimized and len(kept) > 1:
            kept = _time_ordered_places(
                kept, request=request, accommodation_coord=accommodation_coord,
            )

        legs = (
            _legs(kept, generation_mode=route_generation_mode)
            if optimized
            else []
        )
        day_groups.append(
            RouteDayGroup(
                day=len(day_groups) + 1,
                area=(district_names or {}).get(
                    adcode or "",
                    adcode or ("坐标聚类路线" if optimized else ""),
                ),
                adcode=adcode,
                places=kept,
                commute_legs=legs,
                commute_minutes=sum(leg.duration_minutes for leg in legs),
                commute_notes=[leg.note for leg in legs],
                time_hints=_time_hints(kept, request=request),
            )
        )

    day_groups = _supplement_food_places(
        day_groups,
        candidates,
        preferences=request.preferences,
        optimized=optimized,
        daily_budget=daily_budget,
        single_leg_max=single_leg_max,
        min_places=settings.route_places_per_day_min,
        max_places=daily_place_capacity,
        district_names=district_names,
        request=request,
        accommodation_coord=accommodation_coord,
    )

    # Keep every optimized day useful without crossing district boundaries.
    if optimized and not selected_route_mode:
        assigned_ids = {
            place.place_id
            for day_group in day_groups
            for place in day_group.places
        }
        available_by_id = {
            candidate.place_id: candidate
            for candidate in candidates
            if candidate.place_id not in assigned_ids
        }
        retained_days = []
        for day_group in day_groups:
            if not _is_evening_only_day(day_group.places, request=request):
                retained_days.append(day_group)
                continue

            rebalanced = None
            same_district_daytime = sorted(
                (
                    place
                    for place in available_by_id.values()
                    if place.adcode == day_group.adcode
                    and _is_daytime_activity(place, request=request)
                ),
                key=lambda place: (
                    -place.effective_score,
                    -place.recommend_score,
                    place.place_id,
                ),
            )
            for place in same_district_daytime:
                rebalanced = _rebuild_day_group(
                    day_group,
                    [*day_group.places, place],
                    optimized=True,
                    daily_budget=daily_budget,
                    single_leg_max=single_leg_max,
                    min_places=settings.route_places_per_day_min,
                    max_places=settings.route_places_per_day_max,
                    district_names=district_names,
                    request=request,
                    accommodation_coord=accommodation_coord,
                )
                if rebalanced is not None:
                    available_by_id.pop(place.place_id, None)
                    assigned_ids.add(place.place_id)
                    break

            if rebalanced is None:
                for donor_index, donor in enumerate(retained_days):
                    if donor.adcode != day_group.adcode:
                        continue
                    donor_daytime = sorted(
                        (
                            place for place in donor.places
                            if _is_daytime_activity(place, request=request)
                        ),
                        key=lambda place: (
                            place.effective_score,
                            place.recommend_score,
                            place.place_id,
                        ),
                    )
                    if len(donor_daytime) < 2:
                        continue
                    current_evening = sorted(
                        (
                            place for place in day_group.places
                            if _is_evening_marked(place)
                        ),
                        key=lambda place: (
                            place.effective_score,
                            place.recommend_score,
                            place.place_id,
                        ),
                    )
                    swap_done = False
                    for incoming in donor_daytime:
                        for outgoing in current_evening:
                            rebuilt_current = _rebuild_day_group(
                                day_group,
                                [
                                    place
                                    for place in day_group.places
                                    if place.place_id != outgoing.place_id
                                ] + [incoming],
                                optimized=True,
                                daily_budget=daily_budget,
                                single_leg_max=single_leg_max,
                                min_places=settings.route_places_per_day_min,
                                max_places=settings.route_places_per_day_max,
                                district_names=district_names,
                                request=request,
                                accommodation_coord=accommodation_coord,
                            )
                            rebuilt_donor = _rebuild_day_group(
                                donor,
                                [
                                    place
                                    for place in donor.places
                                    if place.place_id != incoming.place_id
                                ] + [outgoing],
                                optimized=True,
                                daily_budget=daily_budget,
                                single_leg_max=single_leg_max,
                                min_places=settings.route_places_per_day_min,
                                max_places=settings.route_places_per_day_max,
                                district_names=district_names,
                                request=request,
                                accommodation_coord=accommodation_coord,
                            )
                            if (
                                rebuilt_current is not None
                                and rebuilt_donor is not None
                                and not _is_evening_only_day(
                                    rebuilt_current.places,
                                    request=request,
                                )
                                and not _is_evening_only_day(
                                    rebuilt_donor.places,
                                    request=request,
                                )
                            ):
                                retained_days[donor_index] = rebuilt_donor
                                rebalanced = rebuilt_current
                                swap_done = True
                                break
                        if swap_done:
                            break
                    if swap_done:
                        break

            if rebalanced is None:
                replaceable = [
                    *available_by_id.values(),
                    *day_group.places,
                ]
                replacement_clusters: dict[str, list[CandidatePlace]] = (
                    defaultdict(list)
                )
                for place in replaceable:
                    if place.adcode:
                        replacement_clusters[place.adcode].append(place)
                ranked_replacements = sorted(
                    replacement_clusters.items(),
                    key=lambda item: (
                        -sum(
                            1 for place in item[1]
                            if _is_daytime_activity(place, request=request)
                        ),
                        -len(item[1]),
                        item[0],
                    ),
                )
                for adcode, cluster in ranked_replacements:
                    if (
                        len(cluster) < settings.route_places_per_day_min
                        or not any(
                            _is_daytime_activity(place, request=request)
                            for place in cluster
                        )
                    ):
                        continue
                    ranked = sorted(
                        cluster,
                        key=lambda place: (
                            -place.effective_score,
                            -place.recommend_score,
                            place.place_id,
                        ),
                    )[: settings.route_places_per_day_max]
                    kept, _ = _trim_to_budget(
                        ranked,
                        daily_budget=daily_budget,
                        single_leg_max=single_leg_max,
                        min_places=settings.route_places_per_day_min,
                        generation_mode=route_generation_mode,
                        accommodation_coord=accommodation_coord,
                    )
                    if not kept or not any(
                        _is_daytime_activity(place, request=request)
                        for place in kept
                    ):
                        continue
                    replacement_template = RouteDayGroup(
                        day=day_group.day,
                        area=(district_names or {}).get(adcode, adcode),
                        adcode=adcode,
                    )
                    replacement = _rebuild_day_group(
                        replacement_template,
                        kept,
                        optimized=True,
                        daily_budget=daily_budget,
                        single_leg_max=single_leg_max,
                        min_places=settings.route_places_per_day_min,
                        max_places=settings.route_places_per_day_max,
                        district_names=district_names,
                        request=request,
                        accommodation_coord=accommodation_coord,
                    )
                    if replacement is None:
                        continue
                    for place in day_group.places:
                        assigned_ids.discard(place.place_id)
                        available_by_id[place.place_id] = place
                    for place in replacement.places:
                        available_by_id.pop(place.place_id, None)
                        assigned_ids.add(place.place_id)
                    rebalanced = replacement
                    break

            if rebalanced is not None:
                retained_days.append(rebalanced)
            else:
                for place in day_group.places:
                    assigned_ids.discard(place.place_id)
                    available_by_id[place.place_id] = place
                logger.warning(
                    "Dropping day=%s because no complete daytime route is "
                    "available within one district",
                    day_group.day,
                )
        day_groups = retained_days

    day_groups = _fill_missing_coordinate_days(
        day_groups,
        candidates,
        target_days=days,
        optimized=optimized,
        daily_budget=daily_budget,
        single_leg_max=single_leg_max,
        min_places=settings.route_places_per_day_min,
        max_places=daily_place_capacity,
        district_names=district_names,
        request=request,
        accommodation_coord=accommodation_coord,
    )
    day_groups = _supplement_food_places(
        day_groups,
        candidates,
        preferences=request.preferences,
        optimized=optimized,
        daily_budget=daily_budget,
        single_leg_max=single_leg_max,
        min_places=settings.route_places_per_day_min,
        max_places=daily_place_capacity,
        district_names=district_names,
        request=request,
        accommodation_coord=accommodation_coord,
    )

    day_groups = _order_day_groups_cluster_first(day_groups)
    for index, day_group in enumerate(day_groups, 1):
        day_group.day = index
    violations = _route_day_invariant_violations(
        day_groups,
        optimized=optimized,
        min_places=settings.route_places_per_day_min,
        max_places=daily_place_capacity,
        request=request,
    )
    if violations:
        raise RoutePlanInvariantError(violations, day_groups)
    assigned_ids = {
        place.place_id
        for day_group in day_groups
        for place in day_group.places
    }
    dropped_ids = {
        candidate.place_id
        for candidate in all_candidates
        if candidate.place_id not in assigned_ids
    }
    if optimized and len(day_groups) < days:
        fallback_reason = "insufficient_daytime_coverage"

    return RoutePlan(
        label=label,
        day_groups=day_groups,
        dropped_place_ids=sorted(dropped_ids),
        optimized=optimized,
        fallback_reason=fallback_reason,
    )


def build_group_route_plan(
    *,
    request: TripRequest,
    group: CandidateGroup,
    district_names: dict[str, str] | None = None,
    district_data_available: bool = True,
    supplement_pool: list[CandidatePlace] | None = None,
    accommodation_coord: tuple[float, float] | None = None,
) -> RoutePlan:
    """Historical grouped-route compatibility entrypoint."""
    return _build_candidate_route_plan(
        request=request,
        label=group.label,
        route_candidates=list(group.candidates),
        district_names=district_names,
        district_data_available=district_data_available,
        supplement_pool=supplement_pool,
        accommodation_coord=accommodation_coord,
    )


def _selection_temporally_unschedulable(
    candidate: CandidatePlace,
    request: TripRequest,
) -> bool:
    return (
        _suppress_evening_markers(request)
        and _is_evening_marked(candidate)
        and not _is_daytime_marked(candidate)
    )


def _selection_hard_ineligible(
    candidate: CandidatePlace,
    request: TripRequest,
) -> bool:
    return not _has_coordinates(candidate) or not _is_core_route_slot_candidate(
        candidate,
        request=request,
    )


_SELECTION_STRUCTURE_VIOLATION = re.compile(
    r"^day \d+ has no (?:daytime )?activity$"
)


def _repairable_selection_structure_error(
    error: RoutePlanInvariantError,
) -> bool:
    return bool(error.violations) and all(
        _SELECTION_STRUCTURE_VIOLATION.fullmatch(violation)
        for violation in error.violations
    )


def _selection_structure_gaps(
    day_groups: list[RouteDayGroup] | tuple[RouteDayGroup, ...],
    *,
    request: TripRequest,
) -> list[str | None]:
    gaps: list[str | None] = [
        day_group.adcode
        for day_group in day_groups
        if not any(
            _is_daytime_activity(place, request=request)
            for place in day_group.places
        )
    ]
    gaps.extend([None] * max(0, request.days - len(day_groups)))
    return gaps


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


def _attempt_selected_route_plan(
    *,
    request: TripRequest,
    candidates: list[CandidatePlace],
    must_ids: list[int],
    district_names: dict[str, str] | None,
    district_data_available: bool,
    accommodation_coord: tuple[float, float] | None,
    must_supplement_pool: list[CandidatePlace] | None = None,
) -> tuple[
    RoutePlan | None,
    RoutePlanInvariantError | None,
]:
    try:
        plan = _build_candidate_route_plan(
            request=request,
            label="selection",
            route_candidates=candidates,
            required_anchor_ids=must_ids,
            preferred_anchor_ids=[candidate.place_id for candidate in candidates],
            district_names=district_names,
            district_data_available=district_data_available,
            supplement_pool=None,
            accommodation_coord=accommodation_coord,
            must_supplement_pool=must_supplement_pool,
        )
        day_groups = plan.day_groups
        structure_error = None
    except RoutePlanInvariantError as caught:
        if not _repairable_selection_structure_error(caught):
            raise
        plan = None
        day_groups = caught.day_groups
        structure_error = caught
    used_ids = {
        place.place_id for day_group in day_groups for place in day_group.places
    }
    missing_must_ids = set(must_ids) - used_ids
    if missing_must_ids:
        raise RouteMustIncludeConflictError(
            missing_must_ids,
            "deterministic route feasibility",
        )
    return plan, structure_error


def _selected_route_state(
    plan: RoutePlan | None,
    error: RoutePlanInvariantError | None,
) -> tuple[RouteDayGroup, ...]:
    if error is not None:
        return error.day_groups
    return tuple(plan.day_groups if plan is not None else [])


def _selected_route_used_ids(
    day_groups: list[RouteDayGroup] | tuple[RouteDayGroup, ...],
) -> set[int]:
    return {
        place.place_id for day_group in day_groups for place in day_group.places
    }


def build_selected_route_plan(
    *,
    request: TripRequest,
    selection: PoiSelectionResult,
    qualified_pool: list[CandidatePlace],
    district_names: dict[str, str] | None = None,
    district_data_available: bool = True,
    accommodation_coord: tuple[float, float] | None = None,
    forced_route_drop_ids: set[int] | None = None,
    authorized_supplement_reasons: dict[int, str] | None = None,
    blocked_supplement_ids: set[int] | None = None,
) -> RoutePlan:
    """Build one route from hard musts and soft ranked Selector preferences."""
    candidates_by_id = {candidate.place_id: candidate for candidate in qualified_pool}
    selected_ids = [item.place_id for item in selection.selected]
    missing_ids = [place_id for place_id in selected_ids if place_id not in candidates_by_id]
    if missing_ids:
        raise ValueError(f"selection contains IDs outside qualified pool: {missing_ids}")

    forced_route_drop_ids = set(forced_route_drop_ids or set())
    blocked_supplement_ids = set(blocked_supplement_ids or set())
    selected_id_set = set(selected_ids)
    hard_must_ids = {
        candidate.place_id
        for candidate in qualified_pool
        if request.must_include and candidate.must_include
    }
    missing_selected_must_ids = hard_must_ids - selected_id_set
    if missing_selected_must_ids:
        raise RouteMustIncludeConflictError(
            missing_selected_must_ids,
            "resolved must-go missing from Selector result",
        )

    selected_drop_reasons: dict[int, str] = {}
    eligible_selected: list[CandidatePlace] = []
    for item in selection.selected:
        candidate = candidates_by_id[item.place_id]
        if candidate.place_id in forced_route_drop_ids:
            if candidate.place_id in hard_must_ids:
                raise RouteMustIncludeConflictError(
                    {candidate.place_id},
                    "precise route conflict cannot remove must-go",
                )
            selected_drop_reasons[candidate.place_id] = (
                "ROUTE_FEASIBILITY_LIMIT"
            )
            continue
        if _selection_hard_ineligible(candidate, request):
            if candidate.place_id in hard_must_ids:
                raise RouteMustIncludeConflictError(
                    {candidate.place_id},
                    "hard ineligible",
                )
            selected_drop_reasons[candidate.place_id] = "HARD_INELIGIBLE"
            continue
        if _selection_temporally_unschedulable(candidate, request):
            if candidate.place_id in hard_must_ids:
                raise RouteMustIncludeConflictError(
                    {candidate.place_id},
                    "temporally unschedulable",
                )
            selected_drop_reasons[candidate.place_id] = (
                "TEMPORALLY_UNSCHEDULABLE"
            )
            continue
        eligible_selected.append(candidate.model_copy(update={
            "must_include": candidate.place_id in hard_must_ids,
        }))
    must_ids = [
        candidate.place_id
        for candidate in eligible_selected
        if candidate.place_id in hard_must_ids
    ]
    eligible_selected = [
        *[
            candidate
            for candidate in eligible_selected
            if candidate.place_id in hard_must_ids
        ],
        *[
            candidate
            for candidate in eligible_selected
            if candidate.place_id not in hard_must_ids
        ],
    ]

    qualified_supplements = [
        candidate
        for candidate in qualified_pool
        if candidate.place_id not in selected_id_set
        and candidate.place_id not in blocked_supplement_ids
        and not _selection_hard_ineligible(candidate, request)
        and not _selection_temporally_unschedulable(candidate, request)
        and not _avoid_conflicts_with_place(request, candidate)
    ]
    authorized_supplement_reasons = dict(authorized_supplement_reasons or {})
    invalid_authorized_ids = (
        set(authorized_supplement_reasons)
        - {candidate.place_id for candidate in qualified_supplements}
    )
    if invalid_authorized_ids:
        raise ValueError(
            "authorized route supplements are outside the eligible pool: "
            f"{sorted(invalid_authorized_ids)}"
        )
    accepted_supplements = [
        candidate
        for candidate in qualified_supplements
        if candidate.place_id in authorized_supplement_reasons
    ]
    settings = get_settings()
    trip_capacity = max(0, int(request.days)) * (
        _effective_route_places_per_day_capacity(
            request,
            max_places=settings.route_places_per_day_max,
        )
    )
    if len(must_ids) > trip_capacity:
        raise RouteMustIncludeConflictError(
            set(must_ids),
            "resolved must-go exceeds numeric trip capacity",
        )
    route_candidates = [*eligible_selected, *accepted_supplements]
    plan, structure_error = _attempt_selected_route_plan(
        request=request,
        candidates=route_candidates,
        must_ids=must_ids,
        district_names=district_names,
        district_data_available=district_data_available,
        accommodation_coord=accommodation_coord,
        must_supplement_pool=[
            candidate for candidate in qualified_supplements
            if candidate not in accepted_supplements
            and _is_daytime_activity(candidate, request=request)
        ],
    )
    current_day_groups = _selected_route_state(plan, structure_error)
    pre_fill_used_ids = _selected_route_used_ids(current_day_groups)
    initial_candidate_ids = {candidate.place_id for candidate in route_candidates}
    must_day_supplements = [
        candidate for candidate in qualified_supplements
        if candidate.place_id in pre_fill_used_ids - initial_candidate_ids
    ]
    for candidate in must_day_supplements:
        authorized_supplement_reasons[candidate.place_id] = "FILL_MUST_INCLUDE_DAY"
    accepted_supplements.extend(must_day_supplements)
    route_candidates.extend(must_day_supplements)
    eligible_ordinary = [
        candidate
        for candidate in eligible_selected
        if candidate.place_id not in hard_must_ids
    ]
    used_ordinary_indexes = [
        index
        for index, candidate in enumerate(eligible_ordinary)
        if candidate.place_id in pre_fill_used_ids
    ]
    last_used_ordinary_index = max(used_ordinary_indexes, default=-1)
    pre_fill_capacity_full = len(pre_fill_used_ids) >= trip_capacity
    pre_fill_drop_ids: set[int] = set()
    for index, candidate in enumerate(eligible_ordinary):
        if candidate.place_id in pre_fill_used_ids:
            continue
        selected_drop_reasons[candidate.place_id] = (
            "CAPACITY_LIMIT"
            if pre_fill_capacity_full and index > last_used_ordinary_index
            else "ROUTE_FEASIBILITY_LIMIT"
        )
        pre_fill_drop_ids.add(candidate.place_id)
    if pre_fill_drop_ids:
        route_candidates = [
            candidate
            for candidate in route_candidates
            if candidate.place_id not in pre_fill_drop_ids
        ]
    accepted_supplement_ids = {
        candidate.place_id for candidate in accepted_supplements
    }
    disappeared_supplement_ids = (
        accepted_supplement_ids
        - _selected_route_used_ids(current_day_groups)
    )
    if disappeared_supplement_ids:
        # Supplements are Route-owned rather than user-selected. If a rebuild
        # can no longer place one, retire its stale authorization and let the
        # existing bounded fill loop choose another qualified candidate.
        blocked_supplement_ids.update(disappeared_supplement_ids)
        for place_id in disappeared_supplement_ids:
            authorized_supplement_reasons.pop(place_id, None)
        accepted_supplements = [
            candidate
            for candidate in accepted_supplements
            if candidate.place_id not in disappeared_supplement_ids
        ]
        accepted_supplement_ids.difference_update(disappeared_supplement_ids)
        qualified_supplements = [
            candidate
            for candidate in qualified_supplements
            if candidate.place_id not in disappeared_supplement_ids
        ]

    current_gaps = _selection_structure_gaps(
        current_day_groups,
        request=request,
    )
    remaining_fill_candidates = [
        candidate
        for candidate in qualified_supplements
        if candidate.place_id not in accepted_supplement_ids
        and _is_daytime_activity(candidate, request=request)
    ]
    while current_gaps:
        accepted = False
        target_day = next(
            (
                day_group
                for day_group in current_day_groups
                if not any(
                    _is_daytime_activity(place, request=request)
                    for place in day_group.places
                )
            ),
            None,
        )
        capacity_drop_eligible_ids = (
            _selected_route_used_ids(current_day_groups) & selected_id_set
        )
        ranked_fill_candidates = sorted(
            enumerate(remaining_fill_candidates),
            key=lambda item: (
                0
                if target_day is not None
                and item[1].adcode == target_day.adcode
                else 1,
                item[0],
            ),
        )
        pending_fill_candidates: list[CandidatePlace] = []
        for _, candidate in ranked_fill_candidates:
            pending_fill_candidates.append(candidate)
            trial_candidates = [
                *route_candidates,
                *pending_fill_candidates,
            ]
            proposed_capacity_drop_ids: list[int] = []
            while len(trial_candidates) > trip_capacity:
                trial_candidate_ids = {
                    place.place_id for place in trial_candidates
                }
                proposed_capacity_drop_id = next(
                    (
                        item.place_id
                        for item in reversed(selection.selected)
                        if item.place_id in capacity_drop_eligible_ids
                        and item.place_id in trial_candidate_ids
                        and item.place_id not in hard_must_ids
                        and item.place_id not in selected_drop_reasons
                        and item.place_id not in proposed_capacity_drop_ids
                    ),
                    None,
                )
                if proposed_capacity_drop_id is None:
                    break
                proposed_capacity_drop_ids.append(proposed_capacity_drop_id)
                trial_candidates = [
                    place
                    for place in trial_candidates
                    if place.place_id != proposed_capacity_drop_id
                ]
            if len(trial_candidates) > trip_capacity:
                pending_fill_candidates.pop()
                continue
            if target_day is not None:
                trial_by_id = {
                    place.place_id: place for place in trial_candidates
                }
                ordered_trial: list[CandidatePlace] = []
                ordered_ids: set[int] = set()
                for day_group in current_day_groups:
                    for place in day_group.places:
                        if (
                            place.place_id in trial_by_id
                            and place.place_id not in ordered_ids
                        ):
                            ordered_trial.append(trial_by_id[place.place_id])
                            ordered_ids.add(place.place_id)
                for place in trial_candidates:
                    if (
                        place.place_id != candidate.place_id
                        and place.place_id not in ordered_ids
                    ):
                        ordered_trial.append(place)
                        ordered_ids.add(place.place_id)
                target_ids = [
                    place.place_id
                    for place in target_day.places
                    if place.place_id in ordered_ids
                ]
                insertion_index = (
                    next(
                        index
                        for index, place in enumerate(ordered_trial)
                        if place.place_id == target_ids[-1]
                    )
                    if target_ids
                    else len(ordered_trial)
                )
                ordered_trial.insert(insertion_index, candidate)
                trial_candidates = ordered_trial
            try:
                trial_plan, trial_error = _attempt_selected_route_plan(
                    request=request,
                    candidates=trial_candidates,
                    must_ids=must_ids,
                    district_names=district_names,
                    district_data_available=district_data_available,
                    accommodation_coord=accommodation_coord,
                )
            except RouteMustIncludeConflictError:
                pending_fill_candidates.pop()
                continue
            trial_day_groups = _selected_route_state(trial_plan, trial_error)
            trial_used_ids = _selected_route_used_ids(trial_day_groups)
            trial_gaps = _selection_structure_gaps(
                trial_day_groups,
                request=request,
            )
            pending_fill_ids = {
                place.place_id for place in pending_fill_candidates
            }
            retained_current_ids = (
                _selected_route_used_ids(current_day_groups)
                - set(proposed_capacity_drop_ids)
            )
            if (
                not retained_current_ids.issubset(trial_used_ids)
                or not accepted_supplement_ids.issubset(trial_used_ids)
            ):
                pending_fill_candidates.pop()
                continue
            if len(trial_gaps) < len(current_gaps):
                if not pending_fill_ids.issubset(trial_used_ids):
                    pending_fill_candidates.pop()
                    continue
            else:
                if target_day is not None or len(trial_gaps) > len(current_gaps):
                    pending_fill_candidates.pop()
                elif len(pending_fill_candidates) >= (
                    settings.route_places_per_day_min
                ):
                    # A missing whole day needs a minimum-size feasible seed.
                    # Keep a bounded deterministic sliding window so an
                    # individually unassigned fill can combine with the next
                    # ranked fills without invoking subset/exact-cover search.
                    pending_fill_candidates.pop(0)
                continue
            accepted_supplements.extend(pending_fill_candidates)
            accepted_supplement_ids.update(
                place.place_id for place in pending_fill_candidates
            )
            for proposed_capacity_drop_id in proposed_capacity_drop_ids:
                selected_drop_reasons[proposed_capacity_drop_id] = (
                    "CAPACITY_LIMIT"
                )
            for fill_candidate in pending_fill_candidates:
                authorized_supplement_reasons[fill_candidate.place_id] = (
                    "FILL_EMPTY_DAY"
                )
            route_candidates = trial_candidates
            plan = trial_plan
            structure_error = trial_error
            current_day_groups = trial_day_groups
            current_gaps = trial_gaps
            remaining_fill_candidates = [
                place
                for place in remaining_fill_candidates
                if place.place_id not in accepted_supplement_ids
            ]
            accepted = True
            break
        if not accepted:
            if structure_error is not None:
                raise structure_error
            raise RuntimeError(
                "selected route structure has no feasible qualified daytime fill"
            )

    if plan is None:
        if structure_error is not None:
            raise structure_error
        raise RuntimeError("selected route planning returned no plan")

    used_ids = route_plan_place_ids(plan)
    used_ordinary_indexes = [
        index
        for index, candidate in enumerate(eligible_ordinary)
        if candidate.place_id in used_ids
    ]
    last_used_ordinary_index = max(used_ordinary_indexes, default=-1)
    capacity_full = len(used_ids) >= trip_capacity
    for index, candidate in enumerate(eligible_ordinary):
        if (
            candidate.place_id in used_ids
            or candidate.place_id in selected_drop_reasons
        ):
            continue
        selected_drop_reasons[candidate.place_id] = (
            "CAPACITY_LIMIT"
            if capacity_full and index > last_used_ordinary_index
            else "ROUTE_FEASIBILITY_LIMIT"
        )

    supplement_reasons = {
        candidate.place_id: authorized_supplement_reasons[candidate.place_id]
        for candidate in accepted_supplements
        if candidate.place_id in used_ids
    }

    _close_selection_ledger(
        plan,
        selection=selection,
        candidates_by_id=candidates_by_id,
        supplement_reasons=supplement_reasons,
        selected_drop_reasons=selected_drop_reasons,
        request=request,
    )
    return plan


def _prepend_complete_fallback_route(
    plans: list[RoutePlan],
    *,
    request: TripRequest,
    retrieval: RetrievalResult,
    district_names: dict[str, str] | None,
    district_data_available: bool,
    target_complete_count: int = 1,
    accommodation_coord: tuple[float, float] | None = None,
) -> list[RoutePlan]:
    target_complete_count = max(1, int(target_complete_count or 1))
    complete_count = sum(1 for plan in plans if len(plan.day_groups) == request.days)
    if complete_count >= target_complete_count:
        return plans
    if not retrieval.candidates:
        return plans

    complete_plans = [
        plan for plan in plans
        if len(plan.day_groups) == request.days
    ]
    used_complete_ids = {
        place.place_id
        for plan in complete_plans
        for day_group in plan.day_groups
        for place in day_group.places
    }
    fallback_candidates: list[tuple[str, str, list[CandidatePlace]]] = []
    unused_candidates = [
        candidate
        for candidate in retrieval.candidates
        if candidate.place_id not in used_complete_ids
    ]
    if unused_candidates and used_complete_ids:
        fallback_candidates.append((
            "fallback-unused-candidates",
            "未占用候选兜底路线",
            unused_candidates,
        ))
    fallback_candidates.append((
        "fallback-all-candidates",
        "全量候选兜底路线",
        retrieval.candidates,
    ))

    fallback: RoutePlan | None = None
    for label, theme, candidates in fallback_candidates:
        candidate_fallback = build_group_route_plan(
            request=request,
            group=CandidateGroup(
                label=label,
                internal_theme=theme,
                candidates=candidates,
            ),
            district_names=district_names,
            district_data_available=district_data_available,
            supplement_pool=getattr(retrieval, "route_planning_candidates", None) or [],
            accommodation_coord=accommodation_coord,
        )
        normalize_route_near_duplicates(
            candidate_fallback,
            request=request,
            accommodation_coord=accommodation_coord,
        )
        if len(candidate_fallback.day_groups) == request.days:
            fallback = candidate_fallback
            break
    if fallback is None:
        return plans
    logger.warning(
        "Route Planning semantic groups produced only %d complete %d-day "
        "plan(s); using %s deterministic fallback",
        complete_count,
        request.days,
        fallback.label,
    )
    return [fallback, *plans]


def _prepend_anchor_aware_route_candidate(
    plans: list[RoutePlan],
    *,
    request: TripRequest,
    retrieval: RetrievalResult,
    district_names: dict[str, str] | None,
    district_data_available: bool,
    accommodation_coord: tuple[float, float] | None = None,
) -> list[RoutePlan]:
    if not valid_must_include_ids(request, retrieval):
        return plans
    if not retrieval.candidates:
        return plans
    if any(plan.label == "anchor-aware-all-candidates" for plan in plans):
        return plans

    complete_plans = [
        plan for plan in plans
        if len(plan.day_groups) == request.days
    ]
    current_best_scheduled = max(
        (
            route_must_include_metrics(request, retrieval, plan)[
                "must_include_scheduled"
            ]
            for plan in complete_plans
        ),
        default=0,
    )
    anchor_plan = build_group_route_plan(
        request=request,
        group=CandidateGroup(
            label="anchor-aware-all-candidates",
            internal_theme="有效必选锚点候选路线",
            candidates=retrieval.candidates,
        ),
        district_names=district_names,
        district_data_available=district_data_available,
        supplement_pool=getattr(retrieval, "route_planning_candidates", None) or [],
        accommodation_coord=accommodation_coord,
    )
    normalize_route_near_duplicates(
        anchor_plan,
        request=request,
        accommodation_coord=accommodation_coord,
    )
    if len(anchor_plan.day_groups) != request.days:
        return plans
    anchor_scheduled = route_must_include_metrics(
        request,
        retrieval,
        anchor_plan,
    )["must_include_scheduled"]
    if anchor_scheduled <= current_best_scheduled:
        return plans
    return [anchor_plan, *plans]


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


def _seed_center(seed: list[CandidatePlace]) -> tuple[float, float] | None:
    located = [place for place in seed if _has_coordinates(place)]
    if not located:
        return None
    return (
        sum(float(place.latitude) for place in located) / len(located),
        sum(float(place.longitude) for place in located) / len(located),
    )


def _seed_distance(
    seed: list[CandidatePlace],
    candidate: CandidatePlace,
) -> float:
    center = _seed_center(seed)
    if center is None:
        return math.inf
    return _distance_to_center(candidate, center)


def _multi_area_seed_distance(
    seeds: list[list[CandidatePlace]],
    candidate: CandidatePlace,
) -> float:
    return min(_seed_distance(seed, candidate) for seed in seeds)


def _multi_area_fill_candidate_key(
    seed: list[CandidatePlace],
    candidate: CandidatePlace,
    request: TripRequest,
) -> tuple:
    nature_penalty = (
        0
        if not _nature_preference_active(request) or _is_nature_core_place(candidate)
        else 1
    )
    return (
        _seed_distance(seed, candidate),
        nature_penalty,
        -candidate.effective_score,
        -candidate.recommend_score,
        candidate.place_id,
    )


def _is_multi_area_activity_place(
    place: CandidatePlace,
    request: TripRequest,
) -> bool:
    place_type = str(place.place_type or "").lower()
    if place_type in MULTI_AREA_WEAK_TYPES:
        return False
    text = " ".join([
        str(place.name or ""),
        " ".join(str(tag) for tag in place.category_tags),
    ])
    if any(marker in text for marker in MULTI_AREA_WEAK_NAME_MARKERS):
        return False
    if place_type in MULTI_AREA_ACTIVITY_TYPES:
        return _is_daytime_activity(place, request=request)
    if place.must_include:
        return _is_daytime_activity(place, request=request)
    return False


def _multi_area_same_local_area(
    seed: CandidatePlace,
    candidate: CandidatePlace,
) -> bool:
    if seed.adcode and candidate.adcode:
        return seed.adcode == candidate.adcode
    return haversine_km(seed, candidate) <= MULTI_AREA_LOCAL_FILL_RADIUS_KM


def _best_multi_area_day_addition(
    seed: list[CandidatePlace],
    available: list[CandidatePlace],
    *,
    day: int,
    daily_budget: int,
    single_leg_max: int,
    min_places: int,
    max_places: int,
    district_names: dict[str, str] | None,
    request: TripRequest,
    accommodation_coord: tuple[float, float] | None = None,
) -> tuple[CandidatePlace, RouteDayGroup] | None:
    choices = []
    for candidate in available:
        if candidate.place_id in {place.place_id for place in seed}:
            continue
        if seed and not _multi_area_same_local_area(seed[0], candidate):
            continue
        places = [*seed, candidate]
        incremental_min_places = min(min_places, len(places))
        rebuilt = _anchor_day_group_from_places(
            places,
            day=day,
            daily_budget=daily_budget,
            single_leg_max=single_leg_max,
            min_places=incremental_min_places,
            max_places=max_places,
            district_names=district_names,
            request=request,
            accommodation_coord=accommodation_coord,
        )
        if rebuilt is None:
            continue
        choices.append((
            _multi_area_fill_candidate_key(seed, candidate, request),
            candidate,
            rebuilt,
        ))
    if not choices:
        return None
    _, candidate, rebuilt = min(choices, key=lambda item: item[0])
    return candidate, rebuilt


def _build_multi_area_seed_chunks(
    seeds: list[list[CandidatePlace]],
    candidates: list[CandidatePlace],
    *,
    daily_budget: int,
    single_leg_max: int,
    min_places: int,
    max_places: int,
    district_names: dict[str, str] | None,
    request: TripRequest,
    accommodation_coord: tuple[float, float] | None = None,
) -> list[tuple[str | None, list[CandidatePlace]]]:
    retained: list[tuple[str | None, list[CandidatePlace]]] = []
    used_ids = {
        place.place_id
        for seed in seeds
        for place in seed
    }
    available = [
        candidate for candidate in candidates
        if candidate.place_id not in used_ids
        and _has_coordinates(candidate)
        and _is_multi_area_activity_place(candidate, request)
    ]
    for seed in seeds:
        day = len(retained) + 1
        current = list(seed)
        rebuilt: RouteDayGroup | None = None
        while len(current) < min_places:
            addition = _best_multi_area_day_addition(
                current,
                available,
                day=day,
                daily_budget=daily_budget,
                single_leg_max=single_leg_max,
                min_places=min_places,
                max_places=max_places,
                district_names=district_names,
                request=request,
                accommodation_coord=accommodation_coord,
            )
            if addition is None:
                break
            candidate, rebuilt = addition
            current = rebuilt.places
            used_ids.add(candidate.place_id)
            available = [
                place for place in available
                if place.place_id != candidate.place_id
            ]
        if len(current) < min_places:
            continue
        rebuilt = _anchor_day_group_from_places(
            current,
            day=day,
            daily_budget=daily_budget,
            single_leg_max=single_leg_max,
            min_places=min_places,
            max_places=max_places,
            district_names=district_names,
            request=request,
            accommodation_coord=accommodation_coord,
        )
        if rebuilt is None:
            continue
        retained.append((rebuilt.adcode, rebuilt.places))
    return retained


def _multi_area_seed_can_form_day(
    seed: CandidatePlace,
    candidates: list[CandidatePlace],
    *,
    used_ids: set[int],
    daily_budget: int,
    single_leg_max: int,
    min_places: int,
    max_places: int,
    district_names: dict[str, str] | None,
    request: TripRequest,
    accommodation_coord: tuple[float, float] | None = None,
) -> bool:
    if min_places <= 1:
        return True
    if not _is_multi_area_activity_place(seed, request):
        return False
    available = [
        candidate for candidate in candidates
        if candidate.place_id != seed.place_id
        and candidate.place_id not in used_ids
        and _has_coordinates(candidate)
        and _is_multi_area_activity_place(candidate, request)
        and _multi_area_same_local_area(seed, candidate)
        and haversine_km(seed, candidate) <= MULTI_AREA_DENSITY_RADIUS_KM
    ]
    if len(available) < min_places - 1:
        return False
    current = [seed]
    while len(current) < min_places:
        addition = _best_multi_area_day_addition(
            current,
            available,
            day=1,
            daily_budget=daily_budget,
            single_leg_max=single_leg_max,
            min_places=min_places,
            max_places=max_places,
            district_names=district_names,
            request=request,
            accommodation_coord=accommodation_coord,
        )
        if addition is None:
            return False
        candidate, rebuilt = addition
        current = rebuilt.places
        available = [
            place for place in available
            if place.place_id != candidate.place_id
        ]
    return (
        _anchor_day_group_from_places(
            current,
            day=1,
            daily_budget=daily_budget,
            single_leg_max=single_leg_max,
            min_places=min_places,
            max_places=max_places,
            district_names=district_names,
            request=request,
            accommodation_coord=accommodation_coord,
        )
        is not None
    )


def _multi_area_seed_chunks(
    pool: list[CandidatePlace],
    *,
    request: TripRequest,
    daily_budget: int,
    single_leg_max: int,
    min_places: int,
    max_places: int,
    district_names: dict[str, str] | None,
    accommodation_coord: tuple[float, float] | None = None,
) -> list[tuple[str | None, list[CandidatePlace]]]:
    if request.days < 3:
        return []
    candidates = [
        candidate for candidate in pool
        if _has_coordinates(candidate)
        and not _avoid_conflicts_with_place(request, candidate)
    ]
    if len(candidates) < request.days * min_places:
        return []

    seeds: list[list[CandidatePlace]] = []
    for anchor in _route_anchor_places(request, candidates):
        choices = []
        for index, seed in enumerate(seeds):
            if len(seed) >= max_places:
                continue
            places = [*seed, anchor]
            distance = _seed_distance(seed, anchor)
            if (
                distance > MULTI_DAY_SAME_AREA_CENTER_KM
                and _shared_adcode(seed) != anchor.adcode
            ):
                continue
            if len(places) >= min_places and _anchor_day_group_from_places(
                places,
                day=index + 1,
                daily_budget=daily_budget,
                single_leg_max=single_leg_max,
                min_places=min_places,
                max_places=max_places,
                district_names=district_names,
                request=request,
                accommodation_coord=accommodation_coord,
            ) is None:
                continue
            choices.append((distance, len(seed), index))
        if choices:
            *_, index = min(choices)
            seeds[index].append(anchor)
            continue
        if len(seeds) < request.days:
            seeds.append([anchor])

    used_ids = {
        place.place_id
        for seed in seeds
        for place in seed
    }
    while len(seeds) < request.days:
        available = [
            candidate for candidate in candidates
            if candidate.place_id not in used_ids
            and _is_multi_area_activity_place(candidate, request)
            and _multi_area_seed_can_form_day(
                candidate,
                candidates,
                used_ids=used_ids,
                daily_budget=daily_budget,
                single_leg_max=single_leg_max,
                min_places=min_places,
                max_places=max_places,
                district_names=district_names,
                request=request,
                accommodation_coord=accommodation_coord,
            )
        ]
        if not available:
            break
        if not seeds:
            seed = max(
                available,
                key=lambda candidate: (
                    candidate.effective_score,
                    candidate.recommend_score,
                    candidate.source_count,
                    -candidate.place_id,
                ),
            )
        else:
            choices = [
                (
                    candidate.effective_score,
                    candidate.recommend_score,
                    candidate.source_count,
                    _multi_area_seed_distance(seeds, candidate),
                    -candidate.place_id,
                    candidate,
                )
                for candidate in available
            ]
            choices = [
                choice for choice in choices
                if choice[3] >= MULTI_AREA_SEED_MIN_CENTER_KM
            ]
            if not choices:
                break
            *_, seed = max(choices, key=lambda item: item[:5])
        seed_day = [seed]
        addition = _best_multi_area_day_addition(
            seed_day,
            [
                candidate for candidate in candidates
                if candidate.place_id not in used_ids
                and candidate.place_id != seed.place_id
                and _has_coordinates(candidate)
                and _is_multi_area_activity_place(candidate, request)
            ],
            day=len(seeds) + 1,
            daily_budget=daily_budget,
            single_leg_max=single_leg_max,
            min_places=min_places,
            max_places=max_places,
            district_names=district_names,
            request=request,
            accommodation_coord=accommodation_coord,
        )
        if addition is not None:
            _, rebuilt = addition
            seed_day = rebuilt.places
        seeds.append(seed_day)
        used_ids.update(place.place_id for place in seed_day)

    if len(seeds) < request.days:
        return []
    return _build_multi_area_seed_chunks(
        seeds[: request.days],
        candidates,
        daily_budget=daily_budget,
        single_leg_max=single_leg_max,
        min_places=min_places,
        max_places=seed_max_places,
        district_names=district_names,
        request=request,
        accommodation_coord=accommodation_coord,
    )


def _prepend_multi_area_route_candidate(
    plans: list[RoutePlan],
    *,
    request: TripRequest,
    retrieval: RetrievalResult,
    district_names: dict[str, str] | None,
    district_data_available: bool,
    accommodation_coord: tuple[float, float] | None = None,
) -> list[RoutePlan]:
    if request.days < 3:
        return plans
    if any(plan.label == MULTI_AREA_ROUTE_LABEL for plan in plans):
        return plans
    anchor_ids = valid_must_include_ids(request, retrieval)
    if anchor_ids and any(
        len(plan.day_groups) == request.days
        and route_plan_place_ids(plan) >= anchor_ids
        for plan in plans
    ):
        return plans

    settings = get_settings()
    pool = _route_planning_candidate_pool(
        retrieval, must_include_ids=anchor_ids,
    )
    skeleton_min_places = max(
        settings.route_places_per_day_min,
        MULTI_AREA_MIN_PLACES_PER_DAY,
    )
    chunks = _multi_area_seed_chunks(
        pool,
        request=request,
        daily_budget=daily_commute_budget_minutes(request, settings),
        single_leg_max=single_leg_max_minutes(
            generation_base_mode(request, settings)[0],
            settings,
        ),
        min_places=skeleton_min_places,
        max_places=settings.route_places_per_day_max,
        district_names=district_names,
        accommodation_coord=accommodation_coord,
    )
    if len(chunks) < request.days:
        return plans

    supplement_target = (
        SUPPLEMENT_TARGET_RELAXED
        if _relaxed_preference_active(request)
        else SUPPLEMENT_TARGET_NORMAL
    )
    assigned_skeleton_ids = {
        place.place_id
        for _, chunk in chunks[: request.days]
        for place in chunk
    }
    supplemented_chunks = []
    for adcode, chunk in chunks[: request.days]:
        chunk = _supplement_chunk_from_pool(
            chunk,
            pool,
            used_ids=assigned_skeleton_ids,
            adcode=adcode,
            target_places=supplement_target,
            daily_budget=daily_commute_budget_minutes(request, settings),
            single_leg_max=single_leg_max_minutes(
                generation_base_mode(request, settings)[0],
                settings,
            ),
            min_places=skeleton_min_places,
            request=request,
            accommodation_coord=accommodation_coord,
        )
        supplemented_chunks.append((adcode, chunk))

    day_groups: list[RouteDayGroup] = []
    for day, (_, chunk) in enumerate(supplemented_chunks, 1):
        day_group = _anchor_day_group_from_places(
            chunk,
            day=day,
            daily_budget=daily_commute_budget_minutes(request, settings),
            single_leg_max=single_leg_max_minutes(
                generation_base_mode(request, settings)[0],
                settings,
            ),
            min_places=skeleton_min_places,
            max_places=settings.route_places_per_day_max,
            district_names=district_names,
            request=request,
            accommodation_coord=accommodation_coord,
        )
        if day_group is None:
            return plans
        day_groups.append(day_group)
    day_groups = _order_day_groups_cluster_first(day_groups)
    for index, day_group in enumerate(day_groups, 1):
        day_group.day = index
    violations = _route_day_invariant_violations(
        day_groups,
        optimized=True,
        min_places=skeleton_min_places,
        max_places=settings.route_places_per_day_max,
        request=request,
    )
    if violations:
        return plans
    assigned_ids = {
        place.place_id
        for day_group in day_groups
        for place in day_group.places
    }
    skeleton_plan = RoutePlan(
        label=MULTI_AREA_ROUTE_LABEL,
        day_groups=day_groups,
        dropped_place_ids=sorted(
            candidate.place_id
            for candidate in pool
            if candidate.place_id not in assigned_ids
        ),
        optimized=True,
    )
    normalize_route_near_duplicates(
        skeleton_plan,
        request=request,
        accommodation_coord=accommodation_coord,
    )
    if len(skeleton_plan.day_groups) != request.days:
        return plans
    spread_metrics = route_multi_day_spread_metrics(
        request,
        retrieval,
        skeleton_plan,
    )
    if (
        spread_metrics.get("route_shape_class") == "one_cluster_split"
        or int(spread_metrics.get("distinct_spatial_day_count", 0))
        < min(request.days, 3)
    ):
        return plans
    known_ids = {candidate.place_id for candidate in retrieval.candidates}
    for day_group in skeleton_plan.day_groups:
        for place in day_group.places:
            if place.place_id in known_ids:
                continue
            retrieval.candidates.append(place)
            known_ids.add(place.place_id)
    return [skeleton_plan, *plans]


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


def _effective_route_places_per_day_capacity(
    request: TripRequest,
    *,
    max_places: int,
) -> int:
    """Return the request-aware daily shape shared by admission and Route."""
    shape_limit = (
        SUPPLEMENT_TARGET_RELAXED
        if _relaxed_preference_active(request)
        else SUPPLEMENT_TARGET_NORMAL
    )
    return max(0, min(int(max_places), shape_limit))


def _spatial_spread_preference_active(request: TripRequest) -> bool:
    text = " ".join([*request.preferences, request.notes])
    return _contains_any(text, SPATIAL_SPREAD_PREFERENCE_MARKERS)


def _recognized_route_preference_active(request: TripRequest) -> bool:
    return (
        _nature_preference_active(request)
        or _relaxed_preference_active(request)
        or _spatial_spread_preference_active(request)
        or bool(scoring_dimension_ids(request.preferences))
    )


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


def _route_quality_preference_score(candidate: dict[str, Any]) -> int:
    """Count each matched reward once.

    Legacy ``nature`` and v2 ``place_type:nature`` describe the same request.
    """
    preference_match = candidate.get("preference_match")
    if not isinstance(preference_match, dict):
        return 0
    rewards: set[str] = set()
    for key, matched in preference_match.items():
        if matched is not True:
            continue
        name = str(key)
        if name in {"nature", "place_type:nature"}:
            rewards.add("nature")
        else:
            rewards.add(name)
    return len(rewards)


def _route_quality_place_type_count(candidate: dict[str, Any]) -> int:
    distribution = candidate.get("place_type_distribution")
    if not isinstance(distribution, dict):
        return 0
    return len(distribution)


def build_route_quality_rank_components(
    request: TripRequest,
    retrieval: RetrievalResult,
    plans: list[RoutePlan],
) -> list[dict[str, Any]]:
    candidates = build_route_quality_candidate_metrics(request, retrieval, plans)
    components: list[dict[str, Any]] = []
    for index, candidate in enumerate(candidates):
        components.append({
            "label": candidate.get("label", ""),
            "original_order": index,
            "complete": bool(candidate.get("complete")),
            "must_include_total": int(candidate.get("must_include_total", 0)),
            "must_include_scheduled": int(
                candidate.get("must_include_scheduled", 0)
            ),
            "must_include_coverage_ratio": float(
                candidate.get("must_include_coverage_ratio", 0.0)
            ),
            "multi_day_spread_score": float(
                candidate.get("multi_day_spread_score", 0.0)
            ),
            "route_shape_class": str(candidate.get("route_shape_class", "")),
            "day_center_distance_km_min": float(
                candidate.get("day_center_distance_km_min", 0.0)
            ),
            "day_center_distance_km_median": float(
                candidate.get("day_center_distance_km_median", 0.0)
            ),
            "spatial_spread_gate": str(candidate.get("spatial_spread_gate", "")),
            "anchor_forced_cluster": bool(
                candidate.get("anchor_forced_cluster", False)
            ),
            "preference_coverage": _route_quality_preference_score(candidate),
            "place_type_preference_coverage_ratio": float(
                candidate.get("place_type_preference_coverage_ratio", 0.0)
            ),
            "matched_place_type_preference_dimensions": list(
                candidate.get("matched_place_type_preference_dimensions", [])
            ),
            "missing_place_type_preference_dimensions": list(
                candidate.get("missing_place_type_preference_dimensions", [])
            ),
            "adcode_count": int(candidate.get("adcode_count", 0)),
            "place_type_count": _route_quality_place_type_count(candidate),
            "spatial_concentration_score": float(
                candidate.get("spatial_concentration_score", 0.0)
            ),
            "commute_minutes": int(candidate.get("commute_minutes", 0)),
        })
    return components


def _route_quality_full_anchor_floor(component: dict[str, Any]) -> bool:
    total = int(component.get("must_include_total", 0))
    scheduled = int(component.get("must_include_scheduled", 0))
    return total >= ROUTE_QUALITY_FULL_ANCHOR_FLOOR_MIN_TOTAL and scheduled >= total


def _route_quality_lower_coverage_can_win_by_spread(
    *,
    lower_coverage: dict[str, Any],
    higher_coverage: dict[str, Any],
) -> bool:
    if _route_quality_full_anchor_floor(higher_coverage):
        return False

    spread_advantage = (
        float(lower_coverage.get("multi_day_spread_score", 0.0))
        - float(higher_coverage.get("multi_day_spread_score", 0.0))
    )
    if spread_advantage < ROUTE_QUALITY_SPREAD_CLEAR_WIN_MIN_DIFF:
        return False

    if str(higher_coverage.get("route_shape_class", "")) == "one_cluster_split":
        return True
    return spread_advantage >= ROUTE_QUALITY_SPREAD_STRONG_WIN_MIN_DIFF


def _compare_route_quality_components(
    left: dict[str, Any],
    right: dict[str, Any],
) -> int:
    left_complete = bool(left.get("complete"))
    right_complete = bool(right.get("complete"))
    if left_complete != right_complete:
        return -1 if left_complete else 1

    left_must_include = int(left.get("must_include_scheduled", 0))
    right_must_include = int(right.get("must_include_scheduled", 0))
    must_include_diff = left_must_include - right_must_include
    if abs(must_include_diff) >= 2:
        return -1 if must_include_diff > 0 else 1

    if must_include_diff > 0:
        if _route_quality_lower_coverage_can_win_by_spread(
            lower_coverage=right,
            higher_coverage=left,
        ):
            return 1
        return -1
    if must_include_diff < 0:
        if _route_quality_lower_coverage_can_win_by_spread(
            lower_coverage=left,
            higher_coverage=right,
        ):
            return -1
        return 1

    left_preference = int(left.get("preference_coverage", 0))
    right_preference = int(right.get("preference_coverage", 0))
    if left_preference != right_preference:
        return -1 if left_preference > right_preference else 1

    left_spread = float(left.get("multi_day_spread_score", 0.0))
    right_spread = float(right.get("multi_day_spread_score", 0.0))
    spread_diff = left_spread - right_spread
    if abs(spread_diff) > 0.0001:
        return -1 if spread_diff > 0 else 1

    left_ratio = float(left.get("must_include_coverage_ratio", 0.0))
    right_ratio = float(right.get("must_include_coverage_ratio", 0.0))
    ratio_diff = left_ratio - right_ratio
    if abs(ratio_diff) > 0.0001:
        return -1 if ratio_diff > 0 else 1

    left_commute = int(left.get("commute_minutes", 0))
    right_commute = int(right.get("commute_minutes", 0))
    if left_commute != right_commute:
        return -1 if left_commute < right_commute else 1

    left_order = int(left.get("original_order", 0))
    right_order = int(right.get("original_order", 0))
    if left_order != right_order:
        return -1 if left_order < right_order else 1
    return 0


def rank_route_quality_candidates(
    request: TripRequest,
    retrieval: RetrievalResult,
    plans: list[RoutePlan],
) -> list[RoutePlan]:
    """Return route candidates in P1 route-quality order when intent exists."""
    if not plans:
        return plans
    if not _route_quality_selection_pressure_active(request, retrieval):
        return plans

    components = build_route_quality_rank_components(request, retrieval, plans)
    indexed_plans = list(zip(components, plans, strict=True))
    complete_indexed_plans = [
        item for item in indexed_plans
        if bool(item[0].get("complete"))
    ]
    if not complete_indexed_plans:
        return plans
    incomplete_plans = [
        plan for component, plan in indexed_plans
        if not bool(component.get("complete"))
    ]
    ranked_complete_plans = [
        plan for _, plan in sorted(
            complete_indexed_plans,
            key=cmp_to_key(
                lambda left, right: _compare_route_quality_components(
                    left[0],
                    right[0],
                )
            ),
        )
    ]
    return [*ranked_complete_plans, *incomplete_plans]


def _route_quality_selection_pressure_active(
    request: TripRequest,
    retrieval: RetrievalResult,
) -> bool:
    return (
        request.days >= 3
        or bool(valid_must_include_ids(request, retrieval))
        or _recognized_route_preference_active(request)
    )


async def _load_district_names(adcodes: set[str]) -> dict[str, str]:
    if not adcodes:
        return {}
    async with get_session_factory()() as session:
        rows = (await session.execute(
            text("""
                SELECT adcode, name
                FROM travel_amap_district
                WHERE adcode = ANY(CAST(:adcodes AS text[]))
            """),
            {"adcodes": sorted(adcodes)},
        )).all()
    return {str(row.adcode): str(row.name) for row in rows}


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


async def _enrich_precise_routes(
    route_plan: RoutePlan,
    *,
    request: TripRequest,
    candidates: list[CandidatePlace],
    district_names: dict[str, str] | None,
    city: str,
    citycode: str | None,
    provider: RouteProvider,
    metrics: RoutePlanningMetrics | None = None,
    accommodation_coord: tuple[float, float] | None = None,
    known_precise_legs: dict[tuple[int, int, str], CommuteLeg] | None = None,
) -> None:
    settings = get_settings()
    route_generation_mode, _ = generation_base_mode(request, settings)
    daily_budget = daily_commute_budget_minutes(request, settings)
    single_leg_max = single_leg_max_minutes(route_generation_mode, settings)
    initial_day_count = len(route_plan.day_groups)
    if initial_day_count < request.days and metrics is not None:
        metrics.record_precise_recovery_reason(
            "initial_incomplete",
            request.days - initial_day_count,
        )
    if known_precise_legs is None:
        known_precise_legs = {}
    kept_days = []
    for day_group in route_plan.day_groups:
        day_rejected_for_budget = False
        while len(day_group.places) >= settings.route_places_per_day_min:
            estimated_legs = _legs(
                day_group.places,
                generation_mode=route_generation_mode,
            )
            precise_legs = await _resolve_precise_legs(
                day_group,
                estimated_legs,
                city=city,
                citycode=citycode,
                generation_mode=route_generation_mode,
                requested_commute_mode=request.commute_mode,
                daily_budget=daily_budget,
                single_leg_max=single_leg_max,
                provider=provider,
                metrics=metrics,
                known_precise_legs=known_precise_legs,
                concurrency=max(
                    1,
                    int(getattr(
                        settings,
                        "amap_route_enrichment_concurrency",
                        2,
                    )),
                ),
                timeout_seconds=max(
                    1.0,
                    float(getattr(
                        settings,
                        "amap_route_enrichment_timeout_seconds",
                        120.0,
                    )),
                ),
            )
            if _within_budget(
                precise_legs,
                daily_budget=daily_budget,
                single_leg_max=single_leg_max,
            ):
                if not any(not is_food_place(place) for place in day_group.places):
                    route_plan.dropped_place_ids.extend(
                        place.place_id for place in day_group.places
                    )
                    break
                day_group.commute_legs = precise_legs
                day_group.commute_minutes = sum(
                    leg.duration_minutes for leg in precise_legs
                )
                day_group.commute_notes = [leg.note for leg in precise_legs]
                day_group.time_hints = _time_hints(day_group.places, request=request)
                kept_days.append(day_group)
                break
            if len(day_group.places) == settings.route_places_per_day_min:
                route_plan.dropped_place_ids.extend(
                    place.place_id for place in day_group.places
                )
                day_rejected_for_budget = True
                break

            hard_leg = max(
                precise_legs,
                key=lambda leg: leg.duration_minutes,
                default=None,
            )
            if (
                hard_leg is not None
                and hard_leg.duration_minutes > single_leg_max
            ):
                endpoint_ids = {
                    hard_leg.from_place_id,
                    hard_leg.to_place_id,
                }
                removable = [
                    place
                    for place in day_group.places
                    if place.place_id in endpoint_ids
                ]
            else:
                removable = list(day_group.places)
            removable = [place for place in removable if not place.must_include]
            if not removable:
                # A hard required-to-required conflict cannot be solved by
                # silently removing a user requirement.
                break
            removed = min(
                removable,
                key=lambda place: (
                    place.effective_score,
                    place.recommend_score,
                    place.place_id,
                ),
            )
            route_plan.dropped_place_ids.append(removed.place_id)
            day_group.places = _time_ordered_places(
                [
                    place
                    for place in day_group.places
                    if place.place_id != removed.place_id
                ],
                request=request,
                accommodation_coord=accommodation_coord,
            )
        if day_rejected_for_budget and metrics is not None:
            metrics.record_precise_recovery_reason("precise_budget_rejection")

    missing_days = max(0, request.days - len(kept_days))
    if missing_days:
        kept_days, recovery_dropped_ids = await _recover_precise_route_days(
            kept_days,
            candidates,
            target_days=request.days,
            request=request,
            district_names=district_names,
            city=city,
            citycode=citycode,
            provider=provider,
            metrics=metrics,
            known_precise_legs=known_precise_legs,
            accommodation_coord=accommodation_coord,
        )
        route_plan.dropped_place_ids.extend(recovery_dropped_ids)
    route_plan.day_groups = kept_days
    assigned_ids = {
        place.place_id
        for day_group in route_plan.day_groups
        for place in day_group.places
    }
    route_plan.dropped_place_ids = sorted(
        place_id
        for place_id in set(route_plan.dropped_place_ids)
        if place_id not in assigned_ids
    )
    for index, day_group in enumerate(route_plan.day_groups, 1):
        day_group.day = index


def _precise_recovery_candidate_pool(
    retained_days: list[RouteDayGroup],
    candidates: list[CandidatePlace],
    *,
    request: TripRequest,
    blocked_ids: set[int],
) -> list[CandidatePlace]:
    assigned = [place for day in retained_days for place in day.places]
    assigned_ids = {place.place_id for place in assigned}
    eligible_unused = [
        place
        for place in candidates
        if place.place_id not in assigned_ids
        and place.place_id not in blocked_ids
        and _has_coordinates(place)
        and _is_core_route_slot_candidate(place, request=request)
        and not any(_is_near_duplicate_pair(place, used) for used in assigned)
    ]
    return [*assigned, *eligible_unused]


async def _recover_precise_route_days(
    retained_days: list[RouteDayGroup],
    candidates: list[CandidatePlace],
    *,
    target_days: int,
    request: TripRequest,
    district_names: dict[str, str] | None,
    city: str,
    citycode: str | None,
    provider: RouteProvider,
    metrics: RoutePlanningMetrics | None,
    known_precise_legs: dict[tuple[int, int, str], CommuteLeg],
    accommodation_coord: tuple[float, float] | None,
) -> tuple[list[RouteDayGroup], list[int]]:
    """Run one bounded deterministic post-precise missing-day recovery pass."""
    settings = get_settings()
    route_generation_mode, _ = generation_base_mode(request, settings)
    daily_budget = daily_commute_budget_minutes(request, settings)
    single_leg_max = single_leg_max_minutes(route_generation_mode, settings)
    retained = list(retained_days)
    blocked_ids: set[int] = set()
    dropped_ids: set[int] = set()
    missing_at_start = max(0, target_days - len(retained))
    max_attempts = min(9, missing_at_start + 2)

    for _ in range(max_attempts):
        if len(retained) >= target_days:
            break
        if metrics is not None:
            metrics.amap_precise_recovery_attempt_count += 1
        recovery_pool = _precise_recovery_candidate_pool(
            retained,
            candidates,
            request=request,
            blocked_ids=blocked_ids,
        )
        proposed = _fill_missing_coordinate_days(
            retained,
            recovery_pool,
            target_days=len(retained) + 1,
            optimized=True,
            daily_budget=daily_budget,
            single_leg_max=single_leg_max,
            min_places=settings.route_places_per_day_min,
            max_places=settings.route_places_per_day_max,
            district_names=district_names,
            request=request,
            accommodation_coord=accommodation_coord,
        )
        if len(proposed) == len(retained):
            break
        recovered_day = proposed[-1]
        proposed_ids = {place.place_id for place in recovered_day.places}
        recovered = False
        while len(recovered_day.places) >= settings.route_places_per_day_min:
            estimated_legs = _legs(
                recovered_day.places,
                generation_mode=route_generation_mode,
            )
            precise_legs = await _resolve_precise_legs(
                recovered_day,
                estimated_legs,
                city=city,
                citycode=citycode,
                generation_mode=route_generation_mode,
                requested_commute_mode=request.commute_mode,
                daily_budget=daily_budget,
                single_leg_max=single_leg_max,
                provider=provider,
                metrics=metrics,
                known_precise_legs=known_precise_legs,
                concurrency=max(
                    1,
                    int(getattr(
                        settings,
                        "amap_route_enrichment_concurrency",
                        2,
                    )),
                ),
                timeout_seconds=max(
                    1.0,
                    float(getattr(
                        settings,
                        "amap_route_enrichment_timeout_seconds",
                        120.0,
                    )),
                ),
            )
            if _within_budget(
                precise_legs,
                daily_budget=daily_budget,
                single_leg_max=single_leg_max,
            ) and any(
                not is_food_place(place)
                for place in recovered_day.places
            ) and validate_day_time_budget(
                recovered_day.places,
                request=request,
            ):
                recovered_day.commute_legs = precise_legs
                recovered_day.commute_minutes = sum(
                    leg.duration_minutes for leg in precise_legs
                )
                recovered_day.commute_notes = [leg.note for leg in precise_legs]
                recovered_day.time_hints = _time_hints(
                    recovered_day.places,
                    request=request,
                )
                retained.append(recovered_day)
                recovered = True
                if metrics is not None:
                    metrics.amap_precise_recovery_success_count += 1
                    if any(leg.source == "estimate" for leg in precise_legs):
                        metrics.amap_precise_recovery_estimate_day_count += 1
                break
            if len(recovered_day.places) == settings.route_places_per_day_min:
                break
            hard_leg = max(
                precise_legs,
                key=lambda leg: leg.duration_minutes,
                default=None,
            )
            removable = (
                [
                    place
                    for place in recovered_day.places
                    if place.place_id in {
                        hard_leg.from_place_id,
                        hard_leg.to_place_id,
                    }
                ]
                if hard_leg is not None
                and hard_leg.duration_minutes > single_leg_max
                else list(recovered_day.places)
            )
            removable = [place for place in removable if not place.must_include]
            if not removable:
                # A hard required-to-required conflict cannot be solved by
                # silently removing a user requirement.
                break
            removed = min(
                removable,
                key=lambda place: (
                    place.effective_score,
                    place.recommend_score,
                    place.place_id,
                ),
            )
            dropped_ids.add(removed.place_id)
            recovered_day.places = _time_ordered_places(
                [
                    place
                    for place in recovered_day.places
                    if place.place_id != removed.place_id
                ],
                request=request,
                accommodation_coord=accommodation_coord,
            )
        if recovered:
            continue

        blocked_ids.update(proposed_ids)
        dropped_ids.update(proposed_ids)

    if len(retained) < target_days and metrics is not None:
        metrics.amap_precise_recovery_failed_count += 1
        metrics.record_precise_recovery_reason(
            "recovery_failed",
            target_days - len(retained),
        )
    for index, day_group in enumerate(retained, 1):
        day_group.day = index
    return retained, sorted(dropped_ids)


def _is_complete_route_plan(route_plan: RoutePlan, request: TripRequest) -> bool:
    return bool(route_plan.day_groups) and len(route_plan.day_groups) == request.days


def _limit_to_target_complete_routes(
    plans: list[RoutePlan],
    *,
    request: TripRequest,
    target_plan_count: int | None,
) -> list[RoutePlan]:
    if target_plan_count is None:
        return plans
    target_count = max(1, int(target_plan_count))
    complete_plans = [
        plan for plan in plans if _is_complete_route_plan(plan, request)
    ]
    if not complete_plans:
        return plans
    return complete_plans[:target_count]


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


async def _enrich_precise_route_candidates(
    plans: list[RoutePlan],
    *,
    request: TripRequest,
    city: str,
    citycode: str | None,
    provider: RouteProvider,
    metrics: RoutePlanningMetrics | None = None,
    target_plan_count: int | None = None,
    accommodation_coord: tuple[float, float] | None = None,
    candidates: list[CandidatePlace] | None = None,
    district_names: dict[str, str] | None = None,
    known_precise_legs: dict[tuple[int, int, str], CommuteLeg] | None = None,
) -> list[RoutePlan]:
    if target_plan_count is None:
        target_count = len(plans)
    else:
        target_count = max(1, int(target_plan_count))
    processed: list[RoutePlan] = []
    selected_complete_count = 0
    if metrics is not None:
        metrics.amap_precise_candidate_plan_count = len(plans)
        metrics.amap_precise_target_plan_count = min(target_count, len(plans))

    route_generation_mode, _ = generation_base_mode(request, get_settings())
    for route_plan in plans:
        if target_plan_count is not None and selected_complete_count >= target_count:
            break
        selected_mode = route_plan.membership_ledger is not None
        working_plan = (
            route_plan.model_copy(deep=True) if selected_mode else route_plan
        )
        original_day_place_ids = [
            [place.place_id for place in day_group.places]
            for day_group in route_plan.day_groups
        ]
        original_membership_ids = route_plan_place_ids(route_plan)
        await _preload_precise_route_cache(
            provider,
            [working_plan],
            city=city,
            generation_mode=route_generation_mode,
        )
        allocate = getattr(provider, "allocate_uncached_plan_calls", None)
        if callable(allocate):
            allocate(
                working_plan,
                city=city,
                generation_mode=route_generation_mode,
            )
        if working_plan.optimized:
            route_candidate_ids = route_plan_place_ids(working_plan)
            if not selected_mode:
                route_candidate_ids.update(working_plan.dropped_place_ids)
            scoped_candidates = [
                candidate
                for candidate in (candidates or [])
                if candidate.place_id in route_candidate_ids
            ]
            await _enrich_precise_routes(
                working_plan,
                request=request,
                candidates=scoped_candidates or [
                    place
                    for day_group in route_plan.day_groups
                    for place in day_group.places
                ],
                district_names=district_names,
                city=city,
                citycode=citycode,
                provider=provider,
                metrics=metrics,
                accommodation_coord=accommodation_coord,
                known_precise_legs=known_precise_legs,
            )
        if selected_mode:
            trial_membership_ids = route_plan_place_ids(working_plan)
            if trial_membership_ids != original_membership_ids:
                raise SelectedRoutePreciseConflictError(
                    added_place_ids=(
                        trial_membership_ids - original_membership_ids
                    ),
                    removed_place_ids=(
                        original_membership_ids - trial_membership_ids
                    ),
                    original_day_place_ids=original_day_place_ids,
                    trial_day_place_ids=[
                        [place.place_id for place in day_group.places]
                        for day_group in working_plan.day_groups
                    ],
                    trial_plan=working_plan,
                )
        processed.append(working_plan)
        if _is_complete_route_plan(working_plan, request):
            selected_complete_count += 1

    if metrics is not None:
        metrics.amap_precise_processed_plan_count = len(processed)
        metrics.amap_precise_selected_complete_plan_count = selected_complete_count
        metrics.amap_precise_skipped_plan_count = max(0, len(plans) - len(processed))
    return processed


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


# Route v2 owns one request-wide search budget and one final membership commit.
V2_MAX_PROPOSALS = 8
V2_MAX_REPAIR_ROUNDS = 3
# Changed-day previews have no provider I/O. At most three anchors per round
# plus one improving-parent refresh: (3 + 1) * 3 * 128 = 1536 per request.
V2_MAX_REPAIR_PREVIEWS_PER_BATCH = 128
V2_MAX_REPAIR_PREVIEWS = 1536


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


async def _v2_apply_accepted_walk_proof(
    plan,
    *,
    request,
    provider,
    known,
    known_access,
    attempted_access,
    city,
    citycode,
    deadline,
    generation_mode,
    metrics,
    access_walk_rejections=None,
):
    """Prove short transit legs walkable on the accepted membership only."""
    if provider is None or plan is None:
        return
    settings = get_settings()
    await _preload_precise_route_cache(
        provider, [plan], city=city, generation_mode=generation_mode,
    )
    allocate = getattr(provider, "allocate_uncached_plan_calls", None)
    if callable(allocate):
        allocate(plan, city=city, generation_mode=generation_mode)
    await _v2_resolve_access(
        plan, provider, known_access, attempted_access,
        city=city, citycode=citycode, deadline=deadline,
        generation_mode=generation_mode, walk_proof=True,
        walk_rejections=access_walk_rejections,
    )
    for day in plan.day_groups:
        # A deadline stops further commute queries. Access facts for every day
        # were already applied above, including one-point days with no POI leg.
        can_query = deadline is None or time.monotonic() < deadline
        if day.commute_legs and can_query:
            remaining = (
                max(.001, deadline - time.monotonic())
                if deadline is not None
                else 60
            )
            daily_budget = (
                day.traffic_policy.daily_limit_minutes
                if day.traffic_policy
                else daily_commute_budget_minutes(request, settings)
            )
            day.commute_legs = await _resolve_precise_legs(
                day,
                day.commute_legs,
                city=city,
                citycode=citycode,
                generation_mode=generation_mode,
                requested_commute_mode=request.commute_mode,
                daily_budget=daily_budget,
                single_leg_max=single_leg_max_minutes(generation_mode, settings),
                provider=provider,
                metrics=metrics,
                known_precise_legs=known,
                concurrency=settings.amap_route_enrichment_concurrency,
                timeout_seconds=min(
                    remaining,
                    max(settings.amap_route_timeout, 1) * len(day.commute_legs),
                ),
                walk_proof=True,
                record_leg_metrics=False,
                whole_day_fallback=False,
            )
            day.commute_legs = [
                _lookup_known_commute_leg(
                    leg, known,
                    origin=day.places[index],
                    destination=day.places[index + 1],
                    generation_mode=generation_mode,
                )
                for index, leg in enumerate(day.commute_legs)
            ]
        if day.commute_legs or day.access_legs:
            _v2_finalize_day(day, request)


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


_COVERAGE_COVERED = "COVERED"
_COVERAGE_NO_ELIGIBLE_MATCH = "NO_ELIGIBLE_MATCH"
_COVERAGE_HARD_CONFLICT = "HARD_CONFLICT"
_COVERAGE_SEARCH_NOT_FOUND = "SEARCH_NOT_FOUND"
_COVERAGE_BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
_COVERAGE_TRACE_LIMIT = 24
_COVERAGE_ID_LIMIT = 8
_MEASURED_LIMITS = ("load_limit", "daily_commute_limit", "single_leg_limit")


def _bound_coverage_ids(values) -> list[int]:
    result: list[int] = []
    seen: set[int] = set()
    for raw in values:
        place_id = int(raw)
        if place_id in seen:
            continue
        seen.add(place_id)
        result.append(place_id)
        if len(result) >= _COVERAGE_ID_LIMIT:
            break
    return result


def _selected_place_ids(selection) -> set[int]:
    return {int(item.place_id) for item in selection.selected}


def _frozen_eligible_ids(
    *,
    selector_input,
    request,
    qualified_pool,
    must_include_place_ids=None,
) -> tuple[int, ...]:
    """Use the frozen Selector list, or rebuild it. Never substitute the full pool."""
    if selector_input is not None:
        return tuple(int(place_id) for place_id in selector_input.constraints.eligible_place_ids)
    from src.agents.poi_selection import build_poi_selection_input

    rebuilt = build_poi_selection_input(
        request,
        qualified_pool,
        must_include_place_ids=must_include_place_ids,
    )
    return tuple(int(place_id) for place_id in rebuilt.constraints.eligible_place_ids)


def _coverage_snapshot(
    *,
    plan_place_ids,
    request,
    selection,
    places_by_id,
    frozen_eligible_ids,
    hard_applicable_ids,
    proved_conflicts=None,
    search_exhausted=False,
    plan_feasible=True,
) -> list[dict]:
    """One row per dimension over the frozen credible pool, including replacements."""
    selected = _selected_place_ids(selection)
    frozen = {int(place_id) for place_id in frozen_eligible_ids}
    hard = {int(place_id) for place_id in hard_applicable_ids}
    in_plan = {int(place_id) for place_id in plan_place_ids}
    conflicts = proved_conflicts or {}
    rows = []
    for dimension in scoring_dimension_ids(request.preferences):
        selected_matches: list[int] = []
        applicable: list[int] = []
        for place_id in sorted(frozen):
            place = places_by_id.get(place_id)
            if place is None or not _route_matches_dimension(place, dimension):
                continue
            selected_matches.append(place_id)
            if place_id in hard:
                applicable.append(place_id)
        plan_matches = [place_id for place_id in applicable if place_id in in_plan]
        conflict_rows = []
        for place_id in applicable:
            reason = conflicts.get(place_id)
            if reason is None:
                reason = conflicts.get(str(place_id))
            if reason:
                conflict_rows.append({
                    "place_id": int(place_id),
                    "reason": str(reason)[:80],
                })
        conflicted = {
            int(row["place_id"]) for row in conflict_rows
        }
        if plan_matches and plan_feasible:
            status = _COVERAGE_COVERED
        elif not selected_matches:
            status = _COVERAGE_NO_ELIGIBLE_MATCH
        elif not applicable or set(applicable) <= conflicted:
            status = _COVERAGE_HARD_CONFLICT
        elif search_exhausted:
            status = _COVERAGE_BUDGET_EXHAUSTED
        else:
            status = _COVERAGE_SEARCH_NOT_FOUND
        rows.append({
            "dimension": dimension,
            "evidence_complete": all(pid in places_by_id for pid in frozen) and len(selected_matches) <= _COVERAGE_ID_LIMIT,
            "status": status,
            "selected_match_ids": _bound_coverage_ids([pid for pid in selected_matches if pid in selected]),
            "eligible_match_ids": _bound_coverage_ids(selected_matches),
            "route_place_ids": sorted(in_plan),
            "hard_applicable_match_ids": _bound_coverage_ids(applicable),
            "plan_match_ids": _bound_coverage_ids(plan_matches),
            "conflicts": conflict_rows[:_COVERAGE_ID_LIMIT],
        })
    return rows


def _selected_coverage_count(rows) -> int:
    return sum(1 for row in rows if row.get("status") == _COVERAGE_COVERED)



def _coverage_rows_for_ids(
    place_ids,
    request,
    selection,
    places_by_id,
    frozen_ids,
    hard_ids,
    *,
    proved_conflicts=None,
    search_exhausted=False,
    plan_feasible=True,
) -> list[dict]:
    if not scoring_dimension_ids(request.preferences):
        return []
    return _coverage_snapshot(
        plan_place_ids=place_ids,
        request=request,
        selection=selection,
        places_by_id=places_by_id,
        frozen_eligible_ids=frozen_ids,
        hard_applicable_ids=hard_ids,
        proved_conflicts=proved_conflicts,
        search_exhausted=search_exhausted,
        plan_feasible=plan_feasible,
    )


def _repair_trace_row(
    *,
    action,
    before_ids,
    after_ids,
    before_count,
    after_count,
    decision,
    reason,
    constraint="",
) -> dict:
    before_set = {int(place_id) for place_id in before_ids}
    after_set = {int(place_id) for place_id in after_ids}
    return {
        "action": str(action)[:40],
        "decision": str(decision)[:40],
        "reason": str(reason)[:80],
        "constraint": str(constraint)[:80],
        "before_count": int(before_count),
        "after_count": int(after_count),
        "removed_ids": _bound_coverage_ids(sorted(before_set - after_set)),
        "added_ids": _bound_coverage_ids(sorted(after_set - before_set)),
    }


def _append_repair_trace(metrics, row) -> None:
    if metrics is None or len(metrics.route_v2_repair_trace) >= _COVERAGE_TRACE_LIMIT:
        return
    metrics.route_v2_repair_trace.append(row)


def _measured_limit_improved(before, after) -> str:
    for code in _MEASURED_LIMITS:
        if after.count(code) < before.count(code):
            return code
    return ""


def _context_id_list(context, key, fallback) -> list[int]:
    """An omitted key or None uses fallback. An explicit empty list stays empty."""
    if key not in context or context[key] is None:
        source = fallback
    else:
        source = context[key]
    result: list[int] = []
    seen: set[int] = set()
    for raw in source:
        place_id = int(raw)
        if place_id in seen:
            continue
        seen.add(place_id)
        result.append(place_id)
    return result


_PREFERENCE_COMPARISON = contextvars.ContextVar("preference_comparison", default=None)


def _v2_quality(plan, request, selection, must_ids):
    """Compare overall preference, travel and load inside the existing quality key."""
    from src.agents.pace import detect_pace_mode
    from src.agents.preferences import (preference_reference_minutes,
        preference_contribution_score, route_preference_contributions)
    comparison = _PREFERENCE_COMPARISON.get()
    dimensions, reference = (comparison if comparison is not None else
        (scoring_dimension_ids(request.preferences), preference_reference_minutes(request)))
    places = [p for day in plan.day_groups for p in day.places]
    over = sum(max(0, day.day_feasibility.load_minutes - day.day_feasibility.capacity_minutes)
               for day in plan.day_groups)
    # Finalized commute_minutes already includes accommodation access.
    commute = sum(day.commute_minutes for day in plan.day_groups)
    types = len({p.place_type for p in places if _is_multi_area_activity_place(p, request)})
    stable = tuple(-p.place_id for p in places)
    if not dimensions:
        # Preserve the existing no-interest policy, including accommodation ties.
        ordinary = [item.place_id for item in selection.selected if item.place_id not in must_ids]
        q = {pid: 1 - i / max(1, len(ordinary)) for i, pid in enumerate(ordinary)}
        preference = sum(max((q.get(p.place_id, 0) for p in day.places
            if p.place_id not in must_ids and _is_multi_area_activity_place(p, request)), default=0)
            for day in plan.day_groups) / request.days
        remote = sum(max((p.effective_score for p in day.places
            if p.place_id not in must_ids and not is_food_partition_place(p) and p.source_count > 0),default=0)
            for day in plan.day_groups if day.traffic_policy is not None and day.traffic_policy.day_kind == "excursion")
        if plan.accommodation_policy_version:
            access = max((sum(a.duration_minutes for a in d.access_legs) for d in plan.day_groups),default=0)
            anchor = -(plan.accommodation_anchor.place_id or 0) if plan.accommodation_anchor else 0
            return 0, preference, remote, types, -over, -commute, -access, stable, anchor
        return 0, preference, types, -over, -commute, stable
    contributions = route_preference_contributions(plan, request, dimensions)
    preference = preference_contribution_score(contributions, dimensions, reference,
                                               commute=commute, overload=over)
    pace_load = -over if detect_pace_mode(request) == "relaxed" else 0
    return pace_load, preference, -over, -commute, types, stable



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


def _v2_search_key(plan, request, selection, must_ids, violations):
    """Failure distance precedes preference; feasible-plan quality stays unchanged."""
    settings = get_settings()
    excesses = []
    for day in plan.day_groups:
        feasibility = day.day_feasibility
        policy = day.traffic_policy
        daily_limit = policy.daily_limit_minutes if policy else daily_commute_budget_minutes(request, settings)
        excesses.extend((
            max(0, feasibility.load_minutes - feasibility.load_limit_minutes) / max(1, feasibility.load_limit_minutes),
            max(0, day.commute_minutes - daily_limit) / max(1, daily_limit),
        ))
        for legs, limits in ((day.commute_legs, policy.poi_leg_limits if policy else None),
                             (day.access_legs, policy.access_leg_limits if policy else None)):
            for leg in legs:
                limit = limits[leg.mode] if limits else single_leg_max_minutes(leg.mode, settings)
                excesses.append(max(0, leg.duration_minutes - limit) / max(1, limit))
    return (-len(violations), -max(excesses, default=0), -sum(excesses),
            _v2_quality(plan, request, selection, must_ids))


def _v2_valid_key(plan, request, selection, must_ids):
    # Ordinary estimate-backed delivery remains legal. Once a fully verified
    # POI-commute solution exists, preference cannot replace it with fallback estimates.
    verified = all(leg.source == "amap" for day in plan.day_groups for leg in day.commute_legs)
    access_verified = all(len(day.access_legs) == 2 and all(leg.duration_source == "amap" for leg in day.access_legs)
                          for day in plan.day_groups)
    return verified, access_verified, _v2_quality(plan, request, selection, must_ids)


def _v2_extend_day(places, candidate, request, *, accommodation_coord=None):
    """Construct a day extension; feasibility remains owned by make_plan/evaluate_day."""
    from src.agents.activity_arrangement import confirmed_coverage

    if len(places) >= day_slot_limit(request, get_settings()):
        return None
    if any(candidate.place_id == current.place_id or (
        _is_near_duplicate_pair(candidate, current) and not confirmed_coverage(current, candidate)
    ) for current in places):
        return None
    return _time_ordered_places([*places, candidate], request=request,
                                accommodation_coord=accommodation_coord)


def _v2_seed_groups(request, pool, selection, must_ids, singleton_ids, make_plan, deadline,
                    *, pair_required=False, paired_required=None, coverage_context=None):
    ranked = {item.place_id: i for i, item in enumerate(selection.selected)}
    ordered = sorted(pool, key=lambda p: (p.place_id not in must_ids,
        not any(_route_matches_dimension(p, dim) for dim in scoring_dimension_ids(request.preferences)),
        ranked.get(p.place_id, 1000), -p.effective_score, p.place_id))
    by_id = {p.place_id: p for p in pool}
    from src.agents.activity_arrangement import full_day_place, confirmed_coverage
    groups, used, fixed = [], set(), set()
    for pid in singleton_ids:
        if pid in used:
            continue
        group = [by_id[pid]]
        used.add(pid)
        day = make_plan([group]).day_groups[0]
        is_fixed = full_day_place(group[0]) or day.traffic_policy is None or day.traffic_policy.day_kind != "excursion"
        # Compare required companions before separate seed/empty-day occupancy
        # makes them unavailable to the later unused-partner search.
        if pair_required and pid in must_ids and not is_fixed:
            for p in ordered:
                if deadline is not None and time.monotonic() >= deadline:
                    break
                if len(group) >= day_slot_limit(request, get_settings()):
                    break
                if p.place_id not in must_ids or p.place_id in used or full_day_place(p):
                    continue
                if len(group) == 1 and is_food_partition_place(p):
                    continue
                if p.place_id in singleton_ids:
                    candidate_policy = make_plan([[p]]).day_groups[0].traffic_policy
                    if candidate_policy is None or candidate_policy.day_kind != "excursion":
                        continue
                if any(_is_near_duplicate_pair(p, x) and not confirmed_coverage(x, p) for x in group):
                    continue
                preview_group = _time_ordered_places([*group, p], request=request)
                preview = make_plan([preview_group]).day_groups[0]
                if (preview.traffic_policy is None or preview.traffic_policy.day_kind != "excursion"
                        or not preview.day_feasibility.feasible):
                    continue
                group = preview_group
                used.add(p.place_id)
                if paired_required is not None:
                    paired_required.add(p.place_id)
        if is_fixed:
            fixed.add(len(groups))
        groups.append(group)
    seeded = set(range(len(groups)))
    groups.extend([[] for _ in range(request.days - len(groups))])
    for i in seeded:
        for child in ordered:
            if child.place_id not in used and child.place_id in ranked and confirmed_coverage(groups[i][0],child):
                groups[i].append(child)
                used.add(child.place_id)
    # Put remaining must-go first; extra must-go can share an ordinary day.
    for p in [p for p in ordered if p.place_id in must_ids - used]:
        covered_day = next((i for i in seeded if confirmed_coverage(groups[i][0], p)), None)
        if covered_day is not None:
            groups[covered_day].append(p); used.add(p.place_id); continue
        available = [i for i in range(request.days) if i not in seeded]
        if not available:
            return groups
        empty = next((i for i in available if not groups[i]), None)
        index = empty if empty is not None else min(available, key=lambda i: (
            sum(build_visit_profile(x).visit_minutes for x in groups[i]) + min(haversine_km(p, x) for x in groups[i]) * 3, i))
        groups[index].append(p); used.add(p.place_id)
    for i in range(request.days):
        if not groups[i]:
            p = next((p for p in ordered if p.place_id not in used and daytime_activity(p)), None)
            if p is not None: groups[i].append(p); used.add(p.place_id)
    # Partner selection checks full day load; it never fills ordinary_max blindly.
    for i in range(request.days):
        if i in fixed or not groups[i]: continue
        for _ in range(day_slot_limit(request, get_settings()) - len(groups[i])):
            if deadline is not None and time.monotonic() >= deadline: return groups
            current = make_plan(groups)
            best = None
            for p in ordered:
                if p.place_id in used: continue
                if deadline is not None and time.monotonic() >= deadline: return groups
                extended = _v2_extend_day(groups[i], p, request)
                if extended is None: continue
                if i in seeded:
                    policy = make_plan([groups[i]]).day_groups[0].traffic_policy
                    if policy is not None and policy.day_kind == "excursion":
                        if len(groups[i]) == 1 and is_food_partition_place(p):
                            continue
                        from src.agents.accommodation_policy import remote_anchor
                        if remote_anchor([*groups[i],p], policy.urban_core)[0] is None:
                            continue
                trial_groups = [list(g) for g in groups]
                trial_groups[i] = extended
                # Proposal construction evaluates only the changed day, not another complete trial.
                changed_day = make_plan([trial_groups[i]]).day_groups[0]
                changed_day.day = i + 1
                trial = current.model_copy(deep=True)
                trial.day_groups[i] = changed_day
                feasible = changed_day.day_feasibility.feasible
                key = (feasible, _v2_quality(trial, request, selection, must_ids))
                if best is None or key > best[0]: best = key, p, trial_groups[i], trial
            if best is None: break
            _, p, ordered_group, trial = best
            needs_partner = len(groups[i]) < 2
            if not needs_partner and (
                not trial.day_groups[i].day_feasibility.feasible
                or _v2_quality(trial, request, selection, must_ids)
                   <= _v2_quality(current, request, selection, must_ids)
            ):
                break
            groups[i] = ordered_group; used.add(p.place_id)
    return groups


def _v2_repair_coverage_context(context, pool, plan):
    places_by_id = dict(context.get("places_by_id") or {})
    for place in pool:
        places_by_id.setdefault(place.place_id, place)
    for day in plan.day_groups:
        for place in day.places:
            places_by_id.setdefault(place.place_id, place)
    pool_ids = [place.place_id for place in pool]
    return {
        "places_by_id": places_by_id,
        "frozen_ids": tuple(_context_id_list(context, "frozen_eligible_ids", pool_ids)),
        "hard_ids": tuple(_context_id_list(context, "hard_applicable_ids", pool_ids)),
        "proved": context.get("proved_conflicts"),
    }


def _v2_repair_keeps_selected_coverage(
    plan, updated, request, selection, context, pool, violations, parent_violations, action, metrics,
) -> bool:
    """Trace ordinary preference changes; existing quality comparison owns selection."""
    if not scoring_dimension_ids(request.preferences):
        return True
    coverage = _v2_repair_coverage_context(context, pool, plan)
    parent_ids = [place.place_id for day in plan.day_groups for place in day.places]
    trial_ids = [place.place_id for group in updated for place in group]
    parent_rows = _coverage_rows_for_ids(
        parent_ids, request, selection, coverage["places_by_id"],
        coverage["frozen_ids"], coverage["hard_ids"], proved_conflicts=coverage["proved"],
    )
    trial_rows = _coverage_rows_for_ids(
        trial_ids, request, selection, coverage["places_by_id"],
        coverage["frozen_ids"], coverage["hard_ids"], proved_conflicts=coverage["proved"],
    )
    before_count = _selected_coverage_count(parent_rows)
    after_count = _selected_coverage_count(trial_rows)
    parent_matches = {
        (row["dimension"], tuple(row["plan_match_ids"])) for row in parent_rows
    }
    trial_matches = {
        (row["dimension"], tuple(row["plan_match_ids"])) for row in trial_rows
    }
    lost = {
        row["dimension"]
        for row in parent_rows
        if row["status"] == _COVERAGE_COVERED
    } - {
        row["dimension"]
        for row in trial_rows
        if row["status"] == _COVERAGE_COVERED
    }
    if not lost and parent_matches == trial_matches:
        return True
    _append_repair_trace(metrics, _repair_trace_row(
        action=action, before_ids=parent_ids, after_ids=trial_ids,
        before_count=before_count, after_count=after_count,
        decision="compare", reason="ordinary_preference_tradeoff",
        constraint="preference_quality",
    ))
    return True


def _v2_repair_proposals(plan, request, pool, must_ids, selection, context=None):
    """Rank bounded changed-day previews; only returned proposals become full trials."""
    context = context or {}
    deadline = context.get("deadline")
    metrics = context.get("metrics")
    groups = [list(d.places) for d in plan.day_groups]
    pool_ids = {p.place_id for p in pool}
    membership_ids = set(_context_id_list(context, "membership_ids", pool_ids))
    supplement_ids = set(_context_id_list(context, "supplement_ids", pool_ids))
    eligible = membership_ids
    parent_key = tuple(tuple(p.place_id for p in g) for g in groups)
    seen = {parent_key}
    known = context.get("known", {
        (leg.from_place_id, leg.to_place_id, leg.mode): leg
        for day in plan.day_groups for leg in day.commute_legs if leg.source == "amap"})
    def default_builder(changed):
        # Direct helper callers keep the parent's frozen access/policy context.
        access = next((d.access_legs[0] for d in plan.day_groups if d.access_legs), None)
        coord = (access.anchor_latitude, access.anchor_longitude) if access else None
        access_facts = {_v2_access_key(leg, place): leg for day in plan.day_groups
                        for leg in day.access_legs for place in day.places
                        if place.place_id == leg.place_id and leg.duration_source == "amap"}
        return _v2_make_plan(changed, request, coord,
                            access.anchor_source if access else "auto_recommended", known,
                            known_access=access_facts)
    builder = context.get("make_plan", default_builder)
    parent_violations = _v2_plan_violations(plan, request, must_ids, eligible, refresh=False)
    affected = [i for i, d in enumerate(plan.day_groups) if d.day_feasibility.violations]
    if not affected:
        affected = list(range(len(groups)))
    ordinary = {i: [p for p in groups[i] if p.place_id not in must_ids] for i in affected}
    used = {p.place_id for g in groups for p in g}
    ranks = {s.place_id: i for i, s in enumerate(selection.selected)}
    replacements = {}
    for i in affected:
        replacements[i] = sorted((p for p in pool if p.place_id not in used and p.place_id in supplement_ids), key=lambda p: (
            min((haversine_km(p, x) for x in groups[i] if x.place_id in must_ids),
                default=min((haversine_km(p, x) for x in groups[i]), default=0)),
            ranks.get(p.place_id, 1000), -p.effective_score, p.place_id))[:4]

    def actions(i, kind):
        if kind == "remove_optional":
            for p in ordinary[i]:
                updated = [list(g) for g in groups]
                updated[i] = [x for x in updated[i] if x.place_id != p.place_id]
                yield kind, updated
        elif kind == "reorder":
            if len(groups[i]) >= 2:
                updated = [list(g) for g in groups]
                updated[i] = list(reversed(updated[i]))
                yield kind, updated
        elif kind == "replace":
            # Each replacement visits every ordinary position, not only the tail.
            for replacement in replacements[i]:
                for p in ordinary[i]:
                    updated = [list(g) for g in groups]
                    updated[i] = _time_ordered_places(
                        [replacement if x.place_id == p.place_id else x for x in groups[i]], request=request)
                    yield kind, updated
        elif kind == "fill":
            if len(groups[i]) < 2 or not any(daytime_activity(x) for x in groups[i]):
                for replacement in replacements[i]:
                    updated = [list(g) for g in groups]
                    updated[i] = _time_ordered_places([*groups[i], replacement], request=request)
                    yield kind, updated
        else:
            for p in ordinary[i]:
                for j in range(len(groups)):
                    if j == i:
                        continue
                    if kind == "move":
                        updated = [list(g) for g in groups]
                        updated[i] = [x for x in groups[i] if x.place_id != p.place_id]
                        updated[j] = _time_ordered_places([*groups[j], p], request=request)
                        yield kind, updated
                    else:
                        for other in groups[j]:
                            if other.place_id in must_ids:
                                continue
                            updated = [list(g) for g in groups]
                            updated[i] = _time_ordered_places(
                                [other if x.place_id == p.place_id else x for x in groups[i]], request=request)
                            updated[j] = _time_ordered_places(
                                [p if x.place_id == other.place_id else x for x in groups[j]], request=request)
                            yield kind, updated

    # Interleave both days and action families: a long replacement pool must not
    # consume the preview budget before any cross-day action is considered.
    streams = [iter(actions(i, kind)) for i in affected
               for kind in ("remove_optional", "replace", "reorder", "move", "swap", "fill")]
    ranked = []
    previews = 0
    if metrics is not None:
        metrics.route_v2_repair_batches += 1
    while streams and previews < V2_MAX_REPAIR_PREVIEWS_PER_BATCH:
        remaining = []
        for stream in streams:
            if (deadline is not None and time.monotonic() >= deadline) or (
                metrics is not None and metrics.route_v2_local_repair_evaluations >= V2_MAX_REPAIR_PREVIEWS):
                streams = []
                break
            action, updated = next(stream, (None, None))
            if action is None:
                continue
            remaining.append(stream)
            signature = tuple(tuple(p.place_id for p in g) for g in updated)
            if signature in seen:
                continue
            seen.add(signature)
            if previews >= V2_MAX_REPAIR_PREVIEWS_PER_BATCH:
                break
            previews += 1
            if metrics is not None:
                metrics.route_v2_local_repair_evaluations += 1
            changed = [i for i in range(len(groups)) if signature[i] != parent_key[i]]
            trial = plan.model_copy(update={"day_groups": list(plan.day_groups)})
            for i in changed:
                day = builder([updated[i]]).day_groups[0]
                day.day = i + 1
                if context.get("make_plan") is None:
                    day.traffic_policy = plan.day_groups[i].traffic_policy
                    _v2_finalize_day(day, request)
                trial.day_groups[i] = day
            violations = _v2_plan_violations(trial, request, must_ids, eligible, refresh=False)
            # Do not buy a shorter commute by losing a day, a must-go, or legal membership.
            hard = set(violations) - {"load_limit", "daily_commute_limit", "single_leg_limit"}
            if any(violations.count(v) > parent_violations.count(v) for v in hard):
                continue
            if "missing_must_include" in violations or "invalid_membership" in violations:
                continue
            if not _v2_repair_keeps_selected_coverage(
                plan, updated, request, selection, context, pool, violations, parent_violations, action, metrics,
            ):
                continue
            new_unknown = sum((leg.from_place_id, leg.to_place_id, leg.mode) not in known
                              for i in changed for leg in trial.day_groups[i].commute_legs)
            # Estimate-only previews cannot outrank known precise hard facts
            # by appearing feasible. Within each traffic-evidence tier, the
            # failure-distance prefix precedes preference/interest quality.
            ranked.append(((new_unknown == 0,
                            _v2_search_key(trial, request, selection, must_ids, violations)), action, updated))
        else:
            streams = remaining
            continue
        break
    ranked.sort(key=lambda item: item[0], reverse=True)
    # Four strongest repairs first; preserve opportunities for other action families.
    chosen = ranked[:min(4, V2_MAX_PROPOSALS)]
    for candidate in ranked[len(chosen):]:
        if len(chosen) >= V2_MAX_PROPOSALS:
            break
        if candidate[1] not in {item[1] for item in chosen}:
            chosen.append(candidate)
    for candidate in ranked:
        if len(chosen) >= V2_MAX_PROPOSALS:
            break
        if candidate not in chosen:
            chosen.append(candidate)
    return [(action, updated) for _, action, updated in chosen]


def _required_full_day_conflict(pool, must_ids, days):
    """Prove only required day occupancy, using the admitted coverage relation.

    Optional selected full-day places can be dropped by search. A covered child
    does not require another day; uncertain nesting alone cannot discharge a Must.
    Considering every possible full-day parent keeps this proof conservative.
    """
    from src.agents.activity_arrangement import full_day_place, confirmed_coverage

    full_places = [p for p in pool if full_day_place(p)]
    required = [p for p in full_places if p.place_id in must_ids
                and not any(confirmed_coverage(parent, p) for parent in full_places)]
    required_ids = {anchor.place_id for anchor in required}
    independent = [p.place_id for p in pool if p.place_id in must_ids
                   and p.place_id not in required_ids
                   and not any(confirmed_coverage(anchor, p) for anchor in required)]
    if len(required) > days or (len(required) == days and independent):
        return RouteFullDayConflictError([p.place_id for p in required], independent, days)
    return None


def _common_visit_lower_bound(place, places_by_id):
    """Minutes every legal arrangement still consumes, or None when unknown.

    A short play style, a full-day arrangement, or confirmed inclusion can
    spend less time than the raw visit profile. Any of those, or a variant
    whose minutes are not a verified number, means the raw profile is not a
    universal lower bound. This does not delete evidence or change the
    arrangement owner.
    """
    from src.agents.activity_arrangement import confirmed_coverage, full_day_place, source_receipts

    if full_day_place(place):
        return None
    if any(
        other.place_id != place.place_id and confirmed_coverage(other, place)
        for other in places_by_id.values()
    ):
        return None
    styles = [row for row in source_receipts(place) if row.get("kind") == "play_style"]
    if styles:
        minutes = [
            row["minutes"] for row in styles
            if isinstance(row.get("minutes"), int) and not isinstance(row["minutes"], bool)
            and row["minutes"] >= 15
        ]
        if not minutes:
            return None
        return min(minutes)
    profile = build_visit_profile(place)
    return profile.visit_minutes + profile.uncertainty_minutes


def _load_limit_contexts(place, request, coupling_context):
    """(limit, breaks) for every day window this place can still use.

    None means the day kind or the excursion window cannot be classified, so
    load is not proved. Without a coupling context only the urban window is in
    force. Access minutes are not added: a nearer anchor can make them zero.
    """
    from src.agents.route_feasibility import day_load_limits

    settings = get_settings()
    _capacity, urban_limit, urban_breaks = day_load_limits(request, settings)
    urban = (urban_limit, urban_breaks)
    if coupling_context is None:
        return [urban]
    core = coupling_context.urban_core
    if core is None or place.latitude is None or place.longitude is None:
        return None
    from src.agents.accommodation_policy import distance_km, remote_anchor
    from src.agents.excursion_time_policy import freeze_time_policy

    window = freeze_time_policy(request)
    remote, _sources = remote_anchor([place], core)
    away = distance_km(core, (float(place.latitude), float(place.longitude))) >= 10
    if window.violations and (remote is not None or away):
        return None
    contexts = [urban]
    if remote is not None or away:
        _capacity, limit, breaks = day_load_limits(request, settings, window)
        contexts.append((limit, breaks))
    return contexts


def _v2_prove_singleton_conflicts(plan_ids, request, selection, places_by_id, frozen_ids, hard_ids, make_plan, proved, *, coupling_context=None):
    """Prove only a static conflict or a common lower bound.

    One infeasible ``make_plan([[place]])`` is not proof that every arrangement
    fails. Search that never tried a placement, and budget exhaustion, are not
    proof either. A load proof has to exceed every usable arrangement and every
    legal day window. ``make_plan`` stays in the signature for existing callers
    and is intentionally unused.
    """
    del make_plan
    if proved is None:
        return
    selected = _selected_place_ids(selection)
    frozen = {int(place_id) for place_id in frozen_ids}
    hard = {int(place_id) for place_id in hard_ids}
    occupied = {int(place_id) for place_id in plan_ids}
    request_must_ids = {
        int(item.place_id) for item in request.must_include if item.place_id is not None
    }
    checked = 0
    for dimension in scoring_dimension_ids(request.preferences):
        for place_id in sorted(selected & frozen & hard):
            if checked >= _COVERAGE_TRACE_LIMIT:
                return
            if place_id in occupied or place_id in proved or str(place_id) in proved:
                continue
            place = places_by_id.get(place_id)
            if place is None or not _route_matches_dimension(place, dimension):
                continue
            checked += 1
            if _selection_temporally_unschedulable(place, request):
                proved[place_id] = "temporally_unschedulable"
                continue
            if (
                place_id not in request_must_ids
                and not place.must_include
                and _avoid_conflicts_with_place(request, place)
            ):
                proved[place_id] = "hard_avoid"
                continue
            lower_bound = _common_visit_lower_bound(place, places_by_id)
            contexts = None if lower_bound is None else _load_limit_contexts(place, request, coupling_context)
            if contexts and all(lower_bound + breaks > limit for limit, breaks in contexts):
                proved[place_id] = "load_limit"


def _v2_prefer_selected_plan(left, right, request, selection, must_ids, places_by_id, frozen_ids, hard_ids) -> bool:
    """Preserve verified-traffic tiers and compare the existing overall quality key."""
    if right is None:
        return True
    return _v2_valid_key(left, request, selection, must_ids) > _v2_valid_key(right, request, selection, must_ids)


def _v2_preview_blocked(metrics, deadline) -> bool:
    """The repair preview cap and the search deadline are one shared gate."""
    if deadline is not None and time.monotonic() >= deadline:
        return True
    return metrics is not None and metrics.route_v2_local_repair_evaluations >= V2_MAX_REPAIR_PREVIEWS


def _v2_take_preview(metrics, deadline) -> bool:
    if _v2_preview_blocked(metrics, deadline):
        return False
    if metrics is not None:
        metrics.route_v2_local_repair_evaluations += 1
    return True


def _plan_has_verified_commute(plan) -> bool:
    if plan is None:
        return False
    legs = [leg for day in plan.day_groups for leg in day.commute_legs]
    return bool(legs) and all(leg.source == "amap" for leg in legs)


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


async def _v2_route_acquired_groups(
    request, pool, selection, must_ids, eligible_ids, provider, known, make_plan,
    metrics, deadline, city, citycode, accommodation_coord, places_by_id,
    frozen_ids, hard_ids, proved_conflicts, known_access, attempted_access,
    *, access_walk_rejections=None,
):
    """One covering plan from legs admitted by the existing route owners.

    Cache preload, uncached allocation and ``_resolve_precise_legs`` stay the
    only query path. A miss stays unknown and is not stored as infeasible.
    Stars start at musts and selected scoring matches, then walk outward by
    distance. Every new day and every eligible untried multi-day combination takes one
    preview from the shared 1536 cap and stops at the search deadline. A closed
    gate does not evaluate a combination that has not yet been admitted.
    Nothing here reads a provider fact index.
    """
    if not pool or provider is None or _v2_preview_blocked(metrics, deadline):
        return None
    by_id = {place.place_id: place for place in pool}
    mode, _ = generation_base_mode(request, get_settings())
    dimensions = scoring_dimension_ids(request.preferences)
    selected = _selected_place_ids(selection)
    ranks = {item.place_id: index for index, item in enumerate(selection.selected)}
    ordered = sorted(
        pool,
        key=lambda place: (
            place.place_id not in must_ids,
            ranks.get(place.place_id, 1000),
            -place.effective_score,
            place.place_id,
        ),
    )
    anchors = [
        place for place in ordered
        if place.place_id in must_ids or (
            place.place_id in selected and any(
                _route_matches_dimension(place, dimension) for dimension in dimensions
            )
        )
    ]
    if not anchors:
        anchors = ordered[:8]
    clean = []
    seen_days = set()
    stopped = False
    coverage = {
        "selection": selection,
        "frozen_ids": tuple(frozen_ids),
        "hard_ids": tuple(hard_ids),
        "places_by_id": places_by_id,
        "proved_conflicts": proved_conflicts,
        "search_exhausted": False,
    }
    tried_plans = set()

    def covered_by(ids):
        return {
            dimension for dimension in dimensions
            if any(
                _route_matches_dimension(by_id[place_id], dimension)
                for place_id in ids
            )
        }

    def verified_day(day):
        if day.commute_legs:
            return all(leg.source == "amap" for leg in day.commute_legs)
        return len(day.access_legs) == 2 and all(
            leg.duration_source == "amap" for leg in day.access_legs
        )

    def plan_ok(chosen, covered, *, count_preview):
        key = tuple(sorted(tuple(place.place_id for place in day) for day in chosen))
        if key in tried_plans:
            return False
        ids = [place.place_id for day in chosen for place in day]
        if len(ids) != len(set(ids)) or not set(must_ids).issubset(ids):
            return False
        if not count_preview or not _v2_take_preview(metrics, deadline):
            return False
        tried_plans.add(key)
        plan = make_plan(chosen)
        if _v2_plan_violations(plan, request, must_ids, eligible_ids, coverage=coverage):
            return False
        return all(verified_day(day) for day in plan.day_groups)

    def assemble(count_preview):
        if len(clean) < request.days:
            return None
        ranked = sorted(clean, key=lambda item: (-len(item[2]), item[1], item[0]))[:24]

        def walk(start, used, chosen, covered):
            if len(chosen) == request.days:
                if plan_ok(chosen, covered, count_preview=count_preview):
                    return chosen
                return None
            if not count_preview or _v2_preview_blocked(metrics, deadline):
                return None
            for index in range(start, len(ranked)):
                ids, _commute, day_covered, places = ranked[index]
                if used.intersection(ids):
                    continue
                found = walk(index + 1, used | set(ids), [*chosen, places], covered | day_covered)
                if found is not None:
                    return found
            return None

        return walk(0, set(), [], set())

    async def verify_day(ordered_places):
        nonlocal stopped
        if not _v2_take_preview(metrics, deadline):
            stopped = True
            return None
        draft = make_plan([ordered_places])
        await _v2_verify_traffic(
            draft, request=request, provider=provider, known=known, known_access=known_access,
            attempted_access=attempted_access, access_walk_rejections=access_walk_rejections,
            city=city, citycode=citycode, deadline=deadline, mode=mode, metrics=metrics,
            must_ids=set(), eligible_ids=eligible_ids,
        )
        if deadline is not None and time.monotonic() >= deadline:
            stopped = True
        day = draft.day_groups[0]
        if not day.day_feasibility.feasible or not verified_day(day):
            return None
        return ordered_places, day.commute_minutes

    def partners_for(anchor):
        return sorted(
            (place for place in pool if place.place_id != anchor.place_id),
            key=lambda place: (
                place.place_id not in selected and place.place_id not in must_ids,
                haversine_km(anchor, place), place.place_id,
            ),
        )

    def _extensions(group, partners):
        for partner in partners:
            extended = _v2_extend_day(group, partner, request,
                                      accommodation_coord=accommodation_coord)
            if extended is not None:
                yield extended

    def compact_candidates():
        from src.agents.activity_arrangement import full_day_place
        for anchor in anchors:
            if build_visit_profile(anchor).singleton_eligible or full_day_place(anchor):
                yield [anchor]
            yield from _extensions([anchor], partners_for(anchor))

    def extended_candidates():
        # A separate lazy stream preserves compact-day opportunities while also
        # visiting every size permitted by the common day owner. Both streams
        # consume the same request-wide previews, facts and deadline.
        streams = [iter(_extensions([anchor], partners_for(anchor))) for anchor in anchors]
        expanded = set()
        while streams:
            remaining = []
            for stream in streams:
                group = next(stream, None)
                if group is None:
                    continue
                remaining.append(stream)
                signature = tuple(place.place_id for place in group)
                if signature in expanded:
                    continue
                expanded.add(signature)
                if len(group) > 2:
                    yield group
                if len(group) < day_slot_limit(request, get_settings()):
                    # Use the same selected/distance priority for each extension.
                    remaining.append(iter(_extensions(group, partners_for(group[0]))))
                if _v2_preview_blocked(metrics, deadline):
                    return
            streams = remaining

    streams = [iter(compact_candidates()), iter(extended_candidates())]
    while streams and not stopped and not _v2_preview_blocked(metrics, deadline):
        remaining = []
        for stream in streams:
            if stopped or _v2_preview_blocked(metrics, deadline):
                break
            ordered_places = next(stream, None)
            if ordered_places is None:
                continue
            remaining.append(stream)
            ids = tuple(place.place_id for place in ordered_places)
            if ids in seen_days:
                continue
            seen_days.add(ids)
            verified = await verify_day(ordered_places)
            if verified is None:
                continue
            places, commute = verified
            clean.append((ids, commute, covered_by(ids), places))
            found = assemble(True)
            if found is not None:
                return found
        streams = remaining
    if len(clean) < request.days or _v2_preview_blocked(metrics, deadline):
        return None
    return assemble(True)


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


async def _plan_routes_v2(request, retrieval, selection, pool, provider, metrics,
                          accommodation_coord, accommodation_source, citycode, deadline, route_start, coupling_context=None, selector_input=None):
    settings = get_settings()
    if selection.schema_version != "1.1":
        raise RoutePlanInvariantError(["selector_route_version_mismatch"], [])
    qualified_pool = list(pool)
    must_ids = valid_must_include_ids(request, retrieval) | {p.place_id for p in qualified_pool if p.must_include}
    selected_ids = [s.place_id for s in selection.selected]
    all_ids = {p.place_id for p in qualified_pool}
    if len(selected_ids) != len(set(selected_ids)) or not set(selected_ids).issubset(all_ids) or not must_ids.issubset(selected_ids):
        raise RoutePlanInvariantError(["invalid_selector_membership"], [])
    pool = [p for p in qualified_pool if valid_coordinate(p.latitude,p.longitude) and not _selection_hard_ineligible(p,request)
            and not _selection_temporally_unschedulable(p,request)
            and (p.place_id in must_ids or not _avoid_conflicts_with_place(request,p))]
    hard_ids = {p.place_id for p in pool}
    if not must_ids.issubset(hard_ids):
        raise RouteMustIncludeConflictError(must_ids - hard_ids, "hard_conflict")
    frozen_ids = _frozen_eligible_ids(
        selector_input=selector_input,
        request=request,
        qualified_pool=qualified_pool,
        must_include_place_ids=sorted(must_ids),
    )
    frozen_set = {int(place_id) for place_id in frozen_ids}
    if not must_ids.issubset(frozen_set):
        raise RoutePlanInvariantError(["must_outside_frozen_eligible"], [])
    if not set(selected_ids).issubset(frozen_set):
        raise RoutePlanInvariantError(["selected_outside_frozen_eligible"], [])
    membership_ids = frozen_set & hard_ids
    supplement_ids = set(membership_ids)
    eligible = membership_ids
    pool = [p for p in pool if p.place_id in membership_ids]
    places_by_id = {p.place_id: p for p in qualified_pool}
    coverage_context = {
        "frozen_ids": tuple(frozen_ids),
        "hard_ids": tuple(hard_ids),
        "places_by_id": places_by_id,
    }
    full_day_conflict = _required_full_day_conflict(pool, must_ids, request.days)
    if full_day_conflict is not None:
        if metrics is not None:
            metrics.route_policy_version = POLICY_VERSION
            metrics.route_v2_stop_reason = "full_day_conflict"
        raise full_day_conflict
    known = {}
    known_access = {}
    attempted_access = set()
    access_walk_rejections = {}
    from src.agents.accommodation_policy import choose_options, anchor_coord
    options = choose_options(coupling_context,pool,selection,must_ids) if coupling_context else [None]
    def make_plan(groups, option_index=0):
        anchor = options[option_index]
        coord = anchor_coord(anchor) if anchor else (None if coupling_context else accommodation_coord)
        source = anchor.suggestion.source if anchor else accommodation_source
        return _v2_make_plan(groups,request,coord,source,known,coupling_context,anchor,known_access)
    metrics = metrics if metrics is not None else RoutePlanningMetrics()
    metrics.route_policy_version = POLICY_VERSION
    metrics.route_v2_trial_count = 0
    metrics.route_v2_local_repair_evaluations = 0
    metrics.route_v2_repair_batches = 0
    metrics.route_v2_repair_rounds = 0
    metrics.route_v2_coverage_report = []
    metrics.route_v2_repair_trace = []
    metrics.route_v2_proved_conflicts = {}
    _v2_prove_singleton_conflicts(
        [], request, selection, places_by_id, frozen_ids, hard_ids,
        make_plan, metrics.route_v2_proved_conflicts,
        coupling_context=coupling_context,
    )
    own_provider = provider is None and amap_keys_configured(settings) and settings.amap_route_enabled
    if own_provider:
        provider = AmapRouteProvider(mode_aware=settings.commute_mode_enabled, metrics=metrics,
                                     deadline_monotonic=deadline, wall_start_monotonic=route_start)
    mode,_ = generation_base_mode(request,settings)
    best_valid = None
    best_search = None
    best_search_key = None
    valid_candidates = []
    candidate_actions = {}
    exhausted = False
    evaluated = set()
    search_by_option = {}
    # One extra branch per anchor keeps locally feasible required companions
    # repairable when fewer failing days make a split route rank above them.
    # It is a search alternative, not a new requirement on accepted routes.
    paired_search_by_option = {}
    def preserves_pairs(plan, pairs):
        return all(any(pair.issubset({p.place_id for p in day.places})
                       and day.day_feasibility.feasible for day in plan.day_groups)
                   for pair in pairs)
    def refresh_paired_search(i):
        if i not in paired_search_by_option:
            return None
        pairs, _, parent = paired_search_by_option[i]
        # Unknown legs may acquire their first precise fact during consider.
        # Rank progress against the same current facts as the new trial.
        parent = make_plan([day.places for day in parent.day_groups], i)
        if not preserves_pairs(parent, pairs):
            return None
        violations = _v2_plan_violations(parent,request,must_ids,eligible,refresh=False)
        key = _v2_search_key(parent,request,selection,must_ids,violations)
        paired_search_by_option[i] = (pairs, key, parent)
        return key, parent
    max_trials = settings.route_v2_max_trials
    def search_budget_exhausted():
        return ((max_trials > 0 and metrics.route_v2_trial_count >= max_trials)
                or (deadline is not None and time.monotonic() >= deadline))
    option_trials = [0 for _ in options]
    def signature_for(groups, option_index):
        return option_index, tuple(tuple(p.place_id for p in g) for g in groups)
    async def consider(groups, action, option_index=0, paired_required=None):
        nonlocal best_valid,best_search,best_search_key,exhausted
        signature = signature_for(groups, option_index)
        if signature in evaluated: return
        if search_budget_exhausted():
            exhausted=True; return
        evaluated.add(signature); metrics.route_v2_trial_count += 1
        option_trials[option_index] += 1
        trial=make_plan(groups, option_index)
        coverage={
            "selection": selection, "frozen_ids": tuple(frozen_ids),
            "hard_ids": tuple(hard_ids), "places_by_id": places_by_id,
            "proved_conflicts": metrics.route_v2_proved_conflicts, "search_exhausted": False,
        }
        violations = await _v2_verify_traffic(
            trial, request=request, provider=provider, known=known, known_access=known_access,
            attempted_access=attempted_access, access_walk_rejections=access_walk_rejections,
            city=retrieval.city, citycode=citycode, deadline=deadline, mode=mode,
            metrics=metrics, must_ids=must_ids, eligible_ids=eligible, coverage=coverage,
        )
        if not violations:
            valid_candidates.append((option_index, trial.model_copy(deep=True)))
            candidate_actions[signature] = action
        if not violations and (best_valid is None or
                _v2_valid_key(trial,request,selection,must_ids) > _v2_valid_key(best_valid,request,selection,must_ids)):
            best_valid=trial.model_copy(deep=True)
            metrics.route_v2_accepted_action=action
        key=_v2_search_key(trial,request,selection,must_ids,violations)
        if option_index not in paired_search_by_option and paired_required:
            pairs = tuple(frozenset(p.place_id for p in day.places if p.place_id in must_ids)
                          for day in trial.day_groups
                          if any(p.place_id in paired_required for p in day.places))
            if pairs and preserves_pairs(trial, pairs):
                paired_search_by_option[option_index] = (pairs, key, trial.model_copy(deep=True))
        if refresh_paired_search(option_index) is not None:
            pairs, prior_key, _ = paired_search_by_option[option_index]
            if preserves_pairs(trial, pairs) and key > prior_key:
                paired_search_by_option[option_index] = (pairs, key, trial.model_copy(deep=True))
        if option_index not in search_by_option or key > search_by_option[option_index][0]:
            search_by_option[option_index] = (key, trial.model_copy(deep=True))
        if best_search_key is None or key > best_search_key:
            best_search=trial.model_copy(deep=True);best_search_key=key
    from src.agents.preferences import preference_reference_minutes
    initial_coverage = _coverage_snapshot(plan_place_ids=[], request=request, selection=selection,
        places_by_id=places_by_id, frozen_eligible_ids=frozen_ids, hard_applicable_ids=hard_ids,
        proved_conflicts=metrics.route_v2_proved_conflicts)
    comparison_dimensions = tuple(row["dimension"] for row in initial_coverage
        if not (row["evidence_complete"] and row["status"] in {"NO_ELIGIBLE_MATCH", "HARD_CONFLICT"}))
    comparison_token = _PREFERENCE_COMPARISON.set(
        (comparison_dimensions, preference_reference_minutes(request)))
    estimate_token = (
        _SHORT_TRANSIT_ESTIMATE_WALK_SPEED.set(True)
        if max_trials > 0 and not callable(getattr(provider, "route_access", None))
        else None
    )
    try:
        ranks={s.place_id:i for i,s in enumerate(selection.selected)}
        from src.agents.activity_arrangement import source_receipts
        def singleton_candidate(point):
            if not (any(options) if coupling_context else accommodation_coord is not None):
                return False
            if build_visit_profile(point).singleton_eligible:
                return True
            if not any(row.get("kind") == "play_style" for row in source_receipts(point)):
                return False
            return any(make_plan([[point]], i).day_groups[0].visit_profiles[0].singleton_eligible
                       for i in range(len(options)))
        long_ids=[p.place_id for p in sorted(pool,key=lambda p:(p.place_id not in must_ids,ranks.get(p.place_id,1000),p.place_id))
                  if singleton_candidate(p)]
        from src.agents.activity_arrangement import full_day_place, confirmed_coverage
        from src.agents.accommodation_policy import remote_anchor
        full_ids = [p.place_id for p in pool if full_day_place(p) and p.place_id in set(selected_ids)
                    and not any(full_day_place(parent) and confirmed_coverage(parent,p) for parent in pool)]
        # Only required full-day occupancy is a proved request conflict.
        # Search may drop excess optional full-day choices, keeping Musts first.
        full_ids.sort(key=lambda pid: (pid not in must_ids, ranks.get(pid, 1000), pid))
        remote_ids = [p.place_id for p in pool if p.place_id in must_ids and coupling_context is not None
                      and remote_anchor([p],coupling_context.urban_core)[0] is not None and p.place_id not in full_ids]
        configs = []
        for candidate in ([*full_ids,*remote_ids][:request.days], full_ids,
                          *[list(dict.fromkeys([*full_ids,*long_ids[:n]]))[:request.days]
                            for n in range(1,min(request.days,len(long_ids))+1)]):
            if candidate not in configs:
                configs.append(candidate)
        # Round-robin across anchors before spending repair slots on any anchor.
        for config in configs:
            split_options = []
            for option_index in range(len(options)):
                if exhausted: break
                builder = lambda groups, i=option_index: make_plan(groups,i)
                paired_required = set()
                groups=_v2_seed_groups(request,pool,selection,must_ids,config,builder,deadline,
                                      pair_required=True,paired_required=paired_required,
                                      coverage_context=coverage_context)
                await consider(groups,"initial" if not config else "singleton_configuration",option_index,paired_required)
                if paired_required:
                    split_options.append(option_index)
            # Learned traffic may reject a locally feasible required pair.
            # Keep the original split search, within the same trial/deadline caps,
            # after every anchor has had its first grouped configuration.
            for option_index in split_options:
                if exhausted: break
                if search_budget_exhausted():
                    exhausted=True; break
                builder = lambda groups, i=option_index: make_plan(groups,i)
                groups=_v2_seed_groups(request,pool,selection,must_ids,config,builder,deadline,
                                      coverage_context=coverage_context)
                await consider(groups,"initial" if not config else "singleton_configuration",option_index)
            if exhausted: break
        for repair_round in range(V2_MAX_REPAIR_ROUNDS):
            if exhausted or best_search is None: break
            def branch_search(branch):
                i, paired = branch
                if paired:
                    return refresh_paired_search(i)
                return search_by_option[i]
            def proposals_for(branch):
                search = branch_search(branch)
                if search is None:
                    return []
                i, _ = branch
                return _v2_repair_proposals(search[1],request,pool,must_ids,selection,{
                    "make_plan": lambda groups: make_plan(groups,i), "known": known,
                    "deadline": deadline, "metrics": metrics,
                    "membership_ids": membership_ids, "supplement_ids": supplement_ids,
                    "frozen_eligible_ids": tuple(frozen_ids),
                    "hard_applicable_ids": tuple(hard_ids),
                    "places_by_id": places_by_id,
                    "proved_conflicts": metrics.route_v2_proved_conflicts})
            # Paired and ordinary branches share the existing eight trials per
            # round, global preview cap and deadline. Split routes remain legal.
            branches = [(i, paired) for paired in (True, False)
                        for i in sorted(search_by_option)
                        if not paired or i in paired_search_by_option]
            batches = {branch:proposals_for(branch) for branch in branches}
            if not any(batches.values()): break
            metrics.route_v2_repair_rounds=repair_round+1
            previous=metrics.route_v2_trial_count
            refreshed = False
            last_branch = None
            for _ in range(V2_MAX_PROPOSALS):
                # Already evaluated signatures do not consume another trial or hide later actions.
                for branch,batch in batches.items():
                    batches[branch] = [(a,g) for a,g in batch if signature_for(g,branch[0]) not in evaluated]
                available = [branch for branch,batch in batches.items() if batch]
                if not available: break
                start = branches.index(last_branch) + 1 if last_branch is not None else 0
                branch = next((b for b in branches[start:] if b in available), available[0])
                last_branch = branch
                option_index = branch[0]
                action,groups = batches[branch].pop(0)
                search = branch_search(branch)
                if search is None:
                    batches[branch] = []
                    continue
                prior_distance = search[0][:3]
                await consider(groups,action,option_index)
                if exhausted: break
                # One extra batch per round, within the same eight full trials: repair
                # newly reduced violations now instead of waiting for the next round.
                search = branch_search(branch)
                if (not refreshed and best_valid is None and search is not None
                        and search[0][:3] > prior_distance):
                    batches[branch] = proposals_for(branch)
                    refreshed = True
            if metrics.route_v2_trial_count==previous: break
        if (
            provider is not None
            and not search_budget_exhausted()
            and not _v2_preview_blocked(metrics, deadline)
            and not _plan_has_verified_commute(best_valid)
        ):
            guided = await _v2_route_acquired_groups(
                request, pool, selection, must_ids, eligible, provider, known, make_plan,
                metrics, deadline, retrieval.city, citycode,
                None if coupling_context else accommodation_coord,
                places_by_id, frozen_ids, hard_ids, metrics.route_v2_proved_conflicts,
                known_access, attempted_access,
                access_walk_rejections=access_walk_rejections,
            )
            if guided and not search_budget_exhausted():
                await consider(guided, "route_acquired_seed")
        exhausted = exhausted or search_budget_exhausted()
        if best_valid is None and metrics.route_v2_repair_rounds >= V2_MAX_REPAIR_ROUNDS:
            exhausted = True
        # Re-check the refreshed plan against facts already learned. Report only.
        coverage_check = {
            "selection": selection,
            "frozen_ids": tuple(frozen_ids),
            "hard_ids": tuple(hard_ids),
            "places_by_id": places_by_id,
            "proved_conflicts": metrics.route_v2_proved_conflicts,
            "search_exhausted": exhausted,
        }
        refreshed_valid = []
        for option_index,candidate in valid_candidates:
            refreshed = make_plan([d.places for d in candidate.day_groups], option_index)
            if not _v2_plan_violations(
                refreshed, request, must_ids, eligible, coverage=coverage_check,
            ):
                refreshed_valid.append(refreshed)
        best_valid = None
        for refreshed in refreshed_valid:
            if _v2_prefer_selected_plan(
                refreshed, best_valid, request, selection, must_ids,
                places_by_id, frozen_ids, hard_ids,
            ):
                best_valid = refreshed
        def publish_coverage(plan, *, plan_feasible):
            plan_ids = [] if plan is None else [p.place_id for day in plan.day_groups for p in day.places]
            _v2_prove_singleton_conflicts(
                plan_ids, request, selection, places_by_id, frozen_ids, hard_ids,
                make_plan, metrics.route_v2_proved_conflicts,
                coupling_context=coupling_context,
            )
            metrics.route_v2_coverage_report = _coverage_rows_for_ids(
                plan_ids, request, selection, places_by_id, frozen_ids, hard_ids,
                proved_conflicts=metrics.route_v2_proved_conflicts,
                search_exhausted=exhausted,
                plan_feasible=plan_feasible,
            )[:_COVERAGE_TRACE_LIMIT]
        if best_valid is None:
            metrics.route_v2_stop_reason="search_budget_exhausted" if exhausted else "qualified_pool_exhausted"
            publish_coverage(best_search, plan_feasible=False)
            reasons = [metrics.route_v2_stop_reason]
            raise RoutePlanInvariantError(list(dict.fromkeys(reasons)),best_search.day_groups if best_search else [])
        if provider is not None:
            await _v2_apply_accepted_walk_proof(
                best_valid,
                request=request,
                provider=provider,
                known=known,
                known_access=known_access,
                attempted_access=attempted_access,
                access_walk_rejections=access_walk_rejections,
                city=retrieval.city,
                citycode=citycode,
                deadline=deadline,
                generation_mode=mode,
                metrics=metrics,
            )
        # Search admitted this plan on the facts it had then. Actual walking
        # can be slower than a fast transit leg. Reject that plan before any
        # success coverage, ledger close, or return. Keep the new facts.
        final_violations = list(dict.fromkeys(_v2_plan_violations(
            best_valid, request, must_ids, eligible, coverage=coverage_check,
        )))
        if final_violations:
            metrics.route_v2_stop_reason = "final_facts_infeasible"
            publish_coverage(best_valid, plan_feasible=False)
            raise RoutePlanInvariantError(
                final_violations, list(best_valid.day_groups),
            )
        publish_coverage(best_valid, plan_feasible=True)
        best_valid.preference_coverage = metrics.route_v2_coverage_report
        winning_index = next((i for i,a in enumerate(options) if a == best_valid.accommodation_anchor),0)
        metrics.route_v2_accepted_action = candidate_actions[signature_for([d.places for d in best_valid.day_groups],winning_index)]
        if coupling_context:
            from src.agents.accommodation_policy import recommendation_reason
            if settings.accommodation_route_coupling_enabled and best_valid.accommodation_anchor and best_valid.accommodation_anchor.suggestion.source == "auto_recommended":
                best_valid.accommodation_anchor.suggestion.reason = recommendation_reason(best_valid)
            metrics.accommodation_metrics.update(selected_option=winning_index, recommendation_changed=winning_index!=0)
        used=route_plan_place_ids(best_valid)
        by_id={p.place_id:p for p in retrieval.candidates+retrieval.route_planning_candidates}
        _close_selection_ledger(best_valid,selection=selection,candidates_by_id=by_id,
            supplement_reasons={pid:"REPLACE_DROPPED" for pid in used-set(selected_ids)},
            selected_drop_reasons={pid:("HARD_INELIGIBLE" if pid not in hard_ids else "NOT_CHOSEN_FOR_FINAL_ROUTE")
                                   for pid in selected_ids if pid not in used},request=request)
        metrics.route_v2_stop_reason="best_valid_at_limit" if exhausted else "complete"
        metrics.plan_count=1;metrics.day_count=request.days
        metrics.commute_leg_count=sum(len(day.commute_legs) for day in best_valid.day_groups)
        metrics.record_membership_ledger(best_valid.membership_ledger)
        metrics.route_quality=build_route_quality_metrics(request,retrieval,[best_valid],selected_reason="route_v2_feasibility")
        from src.agents.preferences import route_preference_snapshot
        snapshot = route_preference_snapshot(best_valid, request)
        for row in best_valid.preference_coverage:
            row["snapshot"] = snapshot
        return [best_valid]
    finally:
        _PREFERENCE_COMPARISON.reset(comparison_token)
        if estimate_token is not None:
            _SHORT_TRANSIT_ESTIMATE_WALK_SPEED.reset(estimate_token)
        metrics.access_route_attempt_count = len(attempted_access)
        metrics.access_route_fact_count = len(known_access)
        if coupling_context:
            metrics.accommodation_metrics.update(resolution_state=coupling_context.state,
                candidate_count=len(coupling_context.anchors), evaluated_option_count=sum(n>0 for n in option_trials),
                option_trial_counts=option_trials)
        if own_provider: await provider.close()


async def plan_routes(
    request: TripRequest,
    retrieval: RetrievalResult,
    *,
    provider: RouteProvider | None = None,
    metrics: RoutePlanningMetrics | None = None,
    target_plan_count: int | None = None,
    accommodation_coord: tuple[float, float] | None = None,
    workflow_deadline_monotonic: float | None = None,
    selection: PoiSelectionResult | None = None,
    accommodation_source: str = "auto_recommended",
    accommodation_context=None,
    selector_input=None,
) -> list[RoutePlan]:
    """Plan one selected route, or read the historical grouped compatibility path."""
    route_start = time.monotonic()
    settings = get_settings()
    configured_wall_ms = max(0, int(settings.amap_route_time_budget_ms or 0))
    route_wall_deadline = (
        route_start + (configured_wall_ms / 1000.0)
        if configured_wall_ms
        else None
    )
    effective_deadline = route_wall_deadline
    if workflow_deadline_monotonic is not None:
        reserved = float(workflow_deadline_monotonic) - _ROUTE_WORKFLOW_RESERVE_SECONDS
        if effective_deadline is None:
            effective_deadline = reserved
        else:
            effective_deadline = min(effective_deadline, reserved)
    qualified_pool = _route_planning_candidate_pool(
        retrieval,
        must_include_ids=valid_must_include_ids(request, retrieval),
    )
    adcodes = {
        candidate.adcode
        for candidate in (
            qualified_pool
            if selection is not None
            else [
                candidate
                for group in retrieval.candidate_groups
                for candidate in group.candidates
            ]
        )
        if candidate.adcode
    }
    district_data_available = True
    try:
        district_names = await _load_district_names(adcodes)
    except Exception:
        logger.exception(
            "District data unavailable, using coordinate route fallback"
        )
        district_names = {}
        district_data_available = False
    if adcodes and not district_names:
        logger.warning(
            "District data unavailable, using coordinate route fallback"
        )
        district_data_available = False
    citycode: str | None = None
    if settings.commute_mode_enabled:
        try:
            citycode = await _load_citycode(request.to_city, adcodes)
        except Exception:
            logger.warning(
                "Amap transit citycode unavailable; transit legs will use estimates",
                exc_info=True,
            )

    if settings.selector_route_v2_enabled:
        if selection is None:
            raise RoutePlanInvariantError(["missing_selector_v11"], [])
        return await _plan_routes_v2(
            request, retrieval, selection, qualified_pool, provider, metrics,
            accommodation_coord, accommodation_source, citycode,
            effective_deadline, route_start,
            accommodation_context,
            selector_input=selector_input,
        )
    if selection is not None:
        plans = [
            build_selected_route_plan(
                request=request,
                selection=selection,
                qualified_pool=qualified_pool,
                district_names=district_names,
                district_data_available=district_data_available,
                accommodation_coord=accommodation_coord,
            )
        ]
        target_plan_count = 1
    else:
        wider_pool = getattr(retrieval, "route_planning_candidates", None) or []
        plans = [
            build_group_route_plan(
                request=request,
                group=group,
                district_names=district_names,
                district_data_available=district_data_available,
                supplement_pool=wider_pool,
                accommodation_coord=accommodation_coord,
            )
            for group in retrieval.candidate_groups
        ]
    if selection is None:
        for route_plan in plans:
            normalize_route_near_duplicates(
                route_plan,
                request=request,
                accommodation_coord=accommodation_coord,
            )
    if selection is not None and plans:
        ledger = plans[0].membership_ledger
        if ledger is None:
            raise RuntimeError("selected route is missing membership ledger")
        _close_selection_ledger(
            plans[0],
            selection=selection,
            candidates_by_id={
                candidate.place_id: candidate for candidate in qualified_pool
            },
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
    if selection is None:
        plans = _prepend_complete_fallback_route(
            plans,
            request=request,
            retrieval=retrieval,
            district_names=district_names,
            district_data_available=district_data_available,
            target_complete_count=target_plan_count or 1,
            accommodation_coord=accommodation_coord,
        )
    previous_selected_label = _legacy_selected_route_quality_label(request, plans)
    rank_components: list[dict[str, Any]] | None = None
    route_quality_rank_evaluated = False
    if selection is None and getattr(settings, "route_quality_selection_enabled", False):
        plans = _prepend_anchor_aware_route_candidate(
            plans,
            request=request,
            retrieval=retrieval,
            district_names=district_names,
            district_data_available=district_data_available,
            accommodation_coord=accommodation_coord,
        )
        plans = _prepend_multi_area_route_candidate(
            plans,
            request=request,
            retrieval=retrieval,
            district_names=district_names,
            district_data_available=district_data_available,
            accommodation_coord=accommodation_coord,
        )
        route_quality_rank_evaluated = _route_quality_selection_pressure_active(
            request,
            retrieval,
        )
        if route_quality_rank_evaluated:
            rank_components = build_route_quality_rank_components(
                request,
                retrieval,
                plans,
            )
        ranked_plans = rank_route_quality_candidates(request, retrieval, plans)
        if [plan.label for plan in ranked_plans] != [plan.label for plan in plans]:
            plans = ranked_plans
    if metrics is not None:
        metrics.route_quality = build_route_quality_metrics(
            request,
            retrieval,
            plans,
            previous_selected_label=previous_selected_label,
            selected_reason=(
                "route_quality_rank"
                if route_quality_rank_evaluated
                else "legacy_order"
            ),
            rank_components=rank_components,
        )

    own_provider = (
        provider is None
        and amap_keys_configured(settings)
        and settings.amap_route_enabled
    )
    active_provider = provider
    if own_provider:
        active_provider = AmapRouteProvider(
            mode_aware=settings.commute_mode_enabled,
            metrics=metrics,
            deadline_monotonic=effective_deadline,
            wall_start_monotonic=route_start,
        )
    if active_provider is None:
        plans = _limit_to_target_complete_routes(
            plans,
            request=request,
            target_plan_count=target_plan_count,
        )
        if metrics is not None:
            metrics.plan_count = len(plans)
            metrics.day_count = sum(len(plan.day_groups) for plan in plans)
            metrics.commute_leg_count = sum(
                len(day.commute_legs)
                for plan in plans
                for day in plan.day_groups
            )
            _record_accommodation_anchor_metrics(
                metrics,
                plans,
                accommodation_coord,
            )
            if selection is not None and plans:
                ledger = plans[0].membership_ledger
                if ledger is None:
                    raise RuntimeError("selected route is missing membership ledger")
                metrics.record_membership_ledger(ledger)
        return plans
    try:
        precise_leg_cache: dict[tuple[int, int, str], CommuteLeg] = {}
        forced_route_drop_ids: set[int] = set()
        blocked_supplement_ids: set[int] = set()
        selected_id_set = {
            item.place_id for item in selection.selected
        } if selection is not None else set()
        possible_reselections = sum(
            candidate.place_id not in valid_must_include_ids(request, retrieval)
            for candidate in qualified_pool
            if candidate.place_id in selected_id_set
        ) if selection is not None else 0
        if selection is not None:
            possible_reselections += sum(
                candidate.place_id not in selected_id_set
                for candidate in qualified_pool
            )
        # Selected membership recovery stays fail-closed, but no longer scales
        # its retry count with the full candidate pool.
        max_precise_reselections = min(3, possible_reselections)
        for precise_attempt in range(max_precise_reselections + 1):
            try:
                plans = await _enrich_precise_route_candidates(
                    plans,
                    request=request,
                    city=retrieval.city,
                    citycode=citycode,
                    provider=active_provider,
                    metrics=metrics,
                    target_plan_count=target_plan_count,
                    accommodation_coord=accommodation_coord,
                    candidates=_route_planning_candidate_pool(
                        retrieval,
                        must_include_ids=valid_must_include_ids(
                            request,
                            retrieval,
                        ),
                    ),
                    district_names=district_names,
                    known_precise_legs=precise_leg_cache,
                )
                break
            except SelectedRoutePreciseConflictError as conflict:
                if selection is None:
                    raise
                hard_must_ids = valid_must_include_ids(request, retrieval)
                removed_ids = set(conflict.removed_place_ids)
                ledger = plans[0].membership_ledger
                if ledger is None:
                    raise RuntimeError(
                        "selected route is missing membership ledger"
                    )
                authorized_supplement_reasons = {
                    item.place_id: item.reason
                    for item in ledger.supplemented
                }
                retired_supplement_ids = (
                    removed_ids & set(authorized_supplement_reasons)
                )
                removed_selected_ids = removed_ids & selected_id_set
                removed_hard_must_ids = removed_selected_ids & hard_must_ids
                structure_gaps = _selection_structure_gaps(
                    conflict.trial_plan.day_groups,
                    request=request,
                )
                if metrics is not None:
                    metrics.amap_precise_membership_conflict_count += 1
                    metrics.amap_precise_membership_conflict_removed_selected_count += (
                        len(removed_selected_ids)
                    )
                    metrics.amap_precise_membership_conflict_structure_gap_count += int(
                        bool(structure_gaps)
                    )
                if removed_hard_must_ids:
                    raise
                logger.warning(
                    "Selected precise membership conflict; applying bounded "
                    "Route-owned recovery: removed_selected_ids=%s, "
                    "removed_supplement_ids=%s, added_place_ids=%s, "
                    "provider_deadline_reached=%s",
                    sorted(removed_selected_ids),
                    sorted(retired_supplement_ids),
                    list(conflict.added_place_ids),
                    bool(
                        effective_deadline is not None
                        and time.monotonic() >= effective_deadline
                    ),
                )
                existing_soft_drop_reasons = {
                    item.place_id: item.reason
                    for item in ledger.selected
                    if item.status == "DROPPED" and item.reason is not None
                }
                if removed_selected_ids:
                    if not structure_gaps:
                        # Selector ordinary choices are preferences, not user
                        # must-go facts. Close a bounded precise-route change
                        # directly instead of rebuilding the whole trip around
                        # the same conflict. Structural gaps still fall through
                        # to the bounded qualified-replacement path below.
                        for place_id in retired_supplement_ids:
                            authorized_supplement_reasons.pop(place_id, None)
                        for place_id in conflict.added_place_ids:
                            if place_id not in selected_id_set:
                                authorized_supplement_reasons[place_id] = (
                                    "REPLACE_DROPPED"
                                )
                        selected_drop_reasons = {
                            **existing_soft_drop_reasons,
                            **{
                                place_id: "ROUTE_FEASIBILITY_LIMIT"
                                for place_id in removed_selected_ids
                            },
                        }
                        accepted_plan = conflict.trial_plan
                        _close_selection_ledger(
                            accepted_plan,
                            selection=selection,
                            candidates_by_id={
                                candidate.place_id: candidate
                                for candidate in qualified_pool
                            },
                            supplement_reasons=authorized_supplement_reasons,
                            selected_drop_reasons=selected_drop_reasons,
                            request=request,
                        )
                        plans = [accepted_plan]
                        if metrics is not None:
                            metrics.amap_precise_membership_soft_accept_count += 1
                            metrics.amap_precise_processed_plan_count = 1
                            metrics.amap_precise_selected_complete_plan_count = int(
                                _is_complete_route_plan(accepted_plan, request)
                            )
                        break
                # A complete validated trial needs no new attempt. Only a
                # rebuild consumes this limit, including on the final pass.
                if precise_attempt >= max_precise_reselections:
                    raise
                for place_id in retired_supplement_ids:
                    authorized_supplement_reasons.pop(place_id, None)
                blocked_supplement_ids.update(retired_supplement_ids)
                drop_id = next(
                    (
                        item.place_id
                        for item in reversed(selection.selected)
                        if item.place_id in removed_ids
                        and item.place_id not in hard_must_ids
                        and item.place_id not in forced_route_drop_ids
                    ),
                    None,
                )
                if drop_id is not None:
                    forced_route_drop_ids.add(drop_id)
                if drop_id is None and not retired_supplement_ids:
                    # Added-only drift is diagnostic but cannot drive a safe
                    # membership reduction. A repeated removed ID likewise
                    # provides no progress, so both fail at this bounded seam.
                    raise
                # The Amap provider remains the owner of its original call and
                # wall deadlines. A local membership rebuild is still safe
                # after that deadline: cached legs remain reusable and every
                # uncached call is rejected before HTTP, producing the existing
                # whole-day estimate fallback without extending either budget.
                plans = [
                    build_selected_route_plan(
                        request=request,
                        selection=selection,
                        qualified_pool=qualified_pool,
                        district_names=district_names,
                        district_data_available=district_data_available,
                        accommodation_coord=accommodation_coord,
                        forced_route_drop_ids=forced_route_drop_ids,
                        authorized_supplement_reasons=(
                            authorized_supplement_reasons
                        ),
                        blocked_supplement_ids=blocked_supplement_ids,
                    )
                ]
        plans = _limit_to_target_complete_routes(
            plans,
            request=request,
            target_plan_count=target_plan_count,
        )
    finally:
        if own_provider:
            await active_provider.close()
    if selection is not None and plans:
        ledger = plans[0].membership_ledger
        if ledger is None:
            raise RuntimeError("selected route is missing membership ledger")
        _close_selection_ledger(
            plans[0],
            selection=selection,
            candidates_by_id={
                candidate.place_id: candidate for candidate in qualified_pool
            },
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
    if metrics is not None:
        metrics.plan_count = len(plans)
        metrics.day_count = sum(len(plan.day_groups) for plan in plans)
        metrics.commute_leg_count = sum(
            len(day.commute_legs)
            for plan in plans
            for day in plan.day_groups
        )
        _record_accommodation_anchor_metrics(
            metrics,
            plans,
            accommodation_coord,
        )
        if selection is not None and plans:
            ledger = plans[0].membership_ledger
            if ledger is None:
                raise RuntimeError("selected route is missing membership ledger")
            metrics.record_membership_ledger(ledger)
    return plans


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
