"""Deterministic v0.6 route planning between retrieval and writing."""

from __future__ import annotations

import asyncio
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

from src.agents.data_retrieval import preference_place_type_dimensions
from src.agents.pace import (
    RELAXED_PACE_MARKERS,
    commute_estimate_speed_kmh,
    daily_commute_budget_minutes,
    generation_base_mode,
    single_leg_max_minutes,
)
from src.agents.poi_alias import ALIAS_SUFFIXES
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
)
from src.config import get_settings
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

    def _state(self) -> tuple[asyncio.Lock, dict[tuple[str, str], float]]:
        loop = asyncio.get_running_loop()
        state = self._states.get(loop)
        if state is None:
            state = (asyncio.Lock(), {})
            self._states[loop] = state
        return state

    async def wait_for_slot(
        self,
        *,
        scope: tuple[str, str],
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
_FOOD_PLACE_TYPES = {"restaurant", "food", "cafe", "snack", "dessert", "market"}
DEFAULT_DAY_CAPACITY_MINUTES = 420
EVENING_ONLY_CAPACITY_MINUTES = 120
DEFAULT_DAY_WEIGHT_BUDGET = 7.0
MIN_TIME_PREFERENCE_WEIGHT_BUDGET = 4.0
NATURE_PREFERENCE_MARKERS = (
    "自然",
    "自然风光",
    "公园",
    "园林",
    "山水",
    "湖",
    "河",
    "海",
    "湿地",
    "森林",
    "徒步",
    "户外",
    "长城",
)
NATURE_CORE_PLACE_TYPES = {
    "park",
    "scenic",
    "nature",
    "trail",
    "mountain",
    "waterfront",
    "garden",
}
NATURE_CORE_TEXT_MARKERS = (
    "公园",
    "园",
    "园林",
    "湖",
    "海",
    "河",
    "湿地",
    "森林",
    "山",
    "长城",
    "颐和园",
    "圆明园",
    "北海",
)
NON_NATURE_PLACE_TYPES = {
    "restaurant",
    "hotel",
    "food",
    "cafe",
    "snack",
    "dessert",
    "business_area",
    "market",
}
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
    return place.place_type in _FOOD_PLACE_TYPES


def top_by_score(places: list[CandidatePlace], n: int) -> list[CandidatePlace]:
    """Return top n places by recommend_score."""
    return sorted(places, key=lambda p: p.recommend_score, reverse=True)[:n]


class RouteProviderError(RuntimeError):
    """Raised when the optional precise route provider cannot return one leg."""


class RouteProviderRateLimitError(RouteProviderError):
    """Raised when the route provider is temporarily rate-limited."""


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
    access_route_attempt_count: int = 0
    access_route_fact_count: int = 0
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
            "access_route_attempt_count": self.access_route_attempt_count,
            "access_route_fact_count": self.access_route_fact_count,
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
        self.api_key = settings.amap_api_key
        self.timeout = settings.amap_route_timeout
        self.min_interval = 1 / max(settings.amap_route_qps, 0.1)
        self.backoff_seconds = max(0.0, settings.amap_route_backoff_seconds)
        self.cache_ttl_seconds = max(1, settings.amap_route_cache_ttl_hours) * 3600
        self.hard_call_cap = max(0, int(settings.amap_route_call_budget or 0))
        self.call_budget = self.hard_call_cap
        self.time_budget_ms = max(0, int(settings.amap_route_time_budget_ms or 0))
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
        route_pairs = {
            self._route_cache_key(
                origin=day_group.places[index],
                destination=day_group.places[index + 1],
                city=city,
                api_generation=self.api_generation,
                mode=resolve_leg_effective_mode(
                    day_group.places[index],
                    day_group.places[index + 1],
                    generation_mode=generation_mode,
                ),
            ): (
                day_group.places[index],
                day_group.places[index + 1],
                resolve_leg_effective_mode(
                    day_group.places[index],
                    day_group.places[index + 1],
                    generation_mode=generation_mode,
                ),
            )
            for plan in plans
            for day_group in plan.day_groups
            for index in range(max(0, len(day_group.places) - 1))
        }
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
        return time.monotonic() < self._disabled_until

    def _enter_backoff(self, reason: str) -> None:
        self._disabled_reason = reason
        self._disabled_until = time.monotonic() + self.backoff_seconds

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
                keys.add(
                    self._route_cache_key(
                        origin=origin,
                        destination=destination,
                        city=city,
                        api_generation=self.api_generation,
                        mode=resolve_leg_effective_mode(
                            origin,
                            destination,
                            generation_mode=generation_mode,
                        ),
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
    ) -> float:
        async with self._budget_lock:
            self._assert_amap_budget_available()
            wait_seconds = await _PROCESS_WIDE_AMAP_RATE_LIMITER.wait_for_slot(
                scope=(self.api_generation, effective_mode),
                min_interval=self.min_interval,
                deadline_monotonic=self._deadline_monotonic,
            )
            if wait_seconds is None:
                self._raise_time_budget_exceeded()
            if self._metrics is not None:
                self._metrics.amap_wait_ms += int(wait_seconds * 1000)
            self._assert_amap_budget_available()
            self._call_count += 1
            if self._metrics is not None:
                self._metrics.amap_call_count += 1
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
        if self.api_generation == "v3":
            if effective_mode != "driving":
                raise RouteProviderError("v3 route compatibility only supports driving")
            url = AMAP_DRIVING_URL
            params = {
                "key": self.api_key,
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
                "key": self.api_key,
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
                self._enter_backoff(message)
                if self._metrics is not None:
                    self._metrics.amap_rate_limit_count += 1
                raise RouteProviderRateLimitError(message) from exc
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
            message = (
                f"{payload.get('info') or 'Amap route API failed'} "
                f"({payload.get('infocode') or ''})"
            )
            if _is_amap_route_rate_limit(payload):
                self._enter_backoff(message)
                if self._metrics is not None:
                    self._metrics.amap_rate_limit_count += 1
                raise RouteProviderRateLimitError(message)
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
        if not self.api_key:
            raise RouteProviderError("AMAP_API_KEY is not configured")
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
        await self._reserve_amap_call(effective_mode)
        try:
            route = await self._fetch_http_route(
                origin=origin,
                destination=destination,
                city=city,
                effective_mode=effective_mode,
                citycode=citycode,
            )
        except RouteProviderError:
            raise
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
        if road_distance_meters <= settings.commute_walk_switch_max_meters:
            return "walking"
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


def _estimated_leg(
    left: CandidatePlace,
    right: CandidatePlace,
    *,
    effective_mode: EffectiveCommuteMode,
) -> CommuteLeg:
    _, distance_meters, duration_minutes = _estimate_route_values(
        left,
        right,
        effective_mode=effective_mode,
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
        if not _is_evening_marked(place) or _is_daytime_marked(place)
    ]
    evening = [
        place for place in places
        if _is_evening_marked(place) and not _is_daytime_marked(place)
    ]
    return [
        *(
            _nearest_neighbor(daytime, accommodation_coord)
            if len(daytime) > 1
            else daytime
        ),
        *(
            _nearest_neighbor(evening, accommodation_coord)
            if len(evening) > 1
            else evening
        ),
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
    return any(
        kw in pref.lower()
        for pref in preferences
        for kw in ["美食", "吃", "food", "火锅", "小吃", "餐厅"]
    )


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
    preference_text = " ".join(request.preferences)
    if _contains_any(preference_text, NATURE_PREFERENCE_MARKERS):
        return True
    # Notes may contain serialized request/city context. Single-character
    # nature markers such as 海/山/湖/河 are too ambiguous there (for example,
    # 上海 must not become a nature preference).
    note_markers = tuple(
        marker for marker in NATURE_PREFERENCE_MARKERS if len(marker) >= 2
    )
    return _contains_any(request.notes, note_markers)


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
        or bool(preference_place_type_dimensions(request.preferences))
    )


def _is_nature_core_place(place: CandidatePlace) -> bool:
    place_type = (place.place_type or "").strip().lower()
    if place_type in NON_NATURE_PLACE_TYPES:
        return False
    if place_type in NATURE_CORE_PLACE_TYPES:
        return True
    if place_type == "attraction":
        return _contains_any(_route_quality_text(place), NATURE_CORE_TEXT_MARKERS)
    return False


def _route_place_count(route_plan: RoutePlan) -> int:
    return sum(len(day_group.places) for day_group in route_plan.day_groups)


def _place_type_preference_metrics(
    request: TripRequest,
    retrieval: RetrievalResult,
    route_plan: RoutePlan,
) -> dict[str, Any]:
    dimensions = preference_place_type_dimensions(request.preferences)
    # Nature already has its stronger semantic matcher. Avoid double-counting a
    # park preference as both nature and a raw place-type dimension.
    if _nature_preference_active(request):
        dimensions.pop("park", None)

    pool_by_id: dict[int, CandidatePlace] = {}
    for candidate in [
        *retrieval.candidates,
        *getattr(retrieval, "route_planning_candidates", []),
    ]:
        pool_by_id.setdefault(candidate.place_id, candidate)
    available_dimensions = {
        dimension
        for dimension, place_types in dimensions.items()
        if any(
            (candidate.place_type or "").strip().lower() in place_types
            and _has_coordinates(candidate)
            and _is_core_route_slot_candidate(candidate, request=request)
            for candidate in pool_by_id.values()
        )
    }
    route_place_types = {
        (place.place_type or "").strip().lower()
        for day_group in route_plan.day_groups
        for place in day_group.places
    }
    matched_dimensions = {
        dimension
        for dimension in available_dimensions
        if route_place_types & dimensions[dimension]
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
    preference_match = candidate.get("preference_match")
    if not isinstance(preference_match, dict):
        return 0
    return sum(1 for matched in preference_match.values() if matched is True)


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
) -> list[CommuteLeg]:
    if not estimated_legs:
        return []
    if metrics is not None:
        for estimated in estimated_legs:
            metrics.record_effective_leg(
                estimated.mode,
                rationalized=estimated.mode != generation_mode,
            )
    semaphore = asyncio.Semaphore(concurrency)

    async def fetch(index: int, estimated: CommuteLeg):
        origin = day_group.places[index]
        destination = day_group.places[index + 1]
        precise_key = (origin.place_id, destination.place_id, estimated.mode)
        if known_precise_legs is not None and precise_key in known_precise_legs:
            return index, known_precise_legs[precise_key].model_copy(deep=True), None
        if estimated.mode == "transit" and not citycode:
            if metrics is not None:
                metrics.amap_missing_citycode_count += 1
            return index, estimated, _MissingTransitCitycodeError(
                "transit route requires citycode"
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
                return index, estimated, exc
        mode_label = {
            "driving": "驾车",
            "transit": "公交",
            "walking": "步行",
            "cycling": "骑行",
        }[estimated.mode]
        resolved = CommuteLeg(
            from_place_id=origin.place_id,
            to_place_id=destination.place_id,
            from_name=origin.name,
            to_name=destination.name,
            distance_meters=precise.distance_meters,
            duration_minutes=precise.duration_minutes,
            mode=estimated.mode,
            source="amap",
            note=(
                f"{origin.name} → {destination.name}："
                f"{mode_label}预计 {precise.duration_minutes} 分钟"
            ),
            encoded_polyline=precise.encoded_polyline or "",
            transit_steps=(
                list(precise.transit_steps)
                if estimated.mode == "transit"
                else []
            ),
            transit_detail_quality=(
                precise.transit_detail_quality
                if estimated.mode == "transit"
                else "missing"
            ),
            transit_detail_cache_hit=(
                precise.transit_detail_cache_hit
                if estimated.mode == "transit"
                else False
            ),
            fare_observation=fare_for_locked_leg(
                precise.fare_observation,
                effective_mode=estimated.mode,
                requested_commute_mode=requested_commute_mode,
            ),
        )
        if known_precise_legs is not None:
            known_precise_legs[precise_key] = resolved.model_copy(deep=True)
        return index, resolved, None

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
        logger.warning(
            "Amap route enrichment timed out, using estimates for day legs"
        )
        if metrics is not None:
            metrics.amap_fallback_count += len(estimated_legs)
            for estimated in estimated_legs:
                metrics.record_effective_fallback(estimated.mode)
        return estimated_legs

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
    if budget_errors and not precise_hard_violation:
        logger.warning(
            "Amap route provider budget exhausted; using consistent "
            "estimates for the whole day: %s",
            budget_errors[0],
        )
        if metrics is not None:
            metrics.amap_provider_budget_exhausted_count += 1
            metrics.record_precise_recovery_reason("provider_budget_exhausted")
            metrics.amap_fallback_count += len(estimated_legs)
            for estimated in estimated_legs:
                metrics.record_effective_fallback(estimated.mode)
        return estimated_legs

    precise_legs: list[CommuteLeg] = []
    budget_exhaustion_recorded = False
    rate_limit_recorded = False
    for index, leg, error in sorted(results, key=lambda item: item[0]):
        if isinstance(error, _MissingTransitCitycodeError):
            if metrics is not None:
                metrics.amap_fallback_count += 1
                metrics.record_effective_fallback(leg.mode)
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
                metrics.record_effective_fallback(leg.mode)
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
                metrics.record_effective_fallback(leg.mode)
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
                metrics.record_effective_fallback(leg.mode)
            precise_legs.append(leg)
            continue
        precise_legs.append(leg)
    return precise_legs


_ROUTE_WORKFLOW_RESERVE_SECONDS = 110.0


# Route v2 owns one request-wide search budget and one final membership commit.
V2_MAX_TRIALS = 24
V2_MAX_PROPOSALS = 8
V2_MAX_REPAIR_ROUNDS = 3
# Changed-day previews have no provider I/O. At most three anchors per round
# plus one improving-parent refresh: (3 + 1) * 3 * 128 = 1536 per request.
V2_MAX_REPAIR_PREVIEWS_PER_BATCH = 128
V2_MAX_REPAIR_PREVIEWS = 1536


def _v2_access_key(leg, place):
    return (leg.direction, leg.anchor_source, leg.anchor_latitude, leg.anchor_longitude,
            place.place_id, place.latitude, place.longitude, leg.mode)


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


async def _v2_resolve_access(plan, provider, known_access, attempted, *, city, citycode, deadline):
    resolver = getattr(provider, "route_access", None)
    if not callable(resolver):
        return
    for day in plan.day_groups:
        by_id = {p.place_id: p for p in day.places}
        for index, leg in enumerate(day.access_legs):
            place = by_id[leg.place_id]
            key = _v2_access_key(leg, place)
            if key in known_access:
                day.access_legs[index] = known_access[key].model_copy(deep=True)
                continue
            if key in attempted or (deadline is not None and time.monotonic() >= deadline):
                continue
            attempted.add(key)
            origin, destination = _v2_access_endpoints(leg, place)
            remaining = max(.001, deadline-time.monotonic()) if deadline is not None else 60
            call_timeout = max(1, get_settings().amap_route_timeout)
            try:
                fact = await asyncio.wait_for(resolver(origin=origin, destination=destination,
                    city=city, effective_mode=leg.mode, citycode=citycode),
                    timeout=min(remaining, call_timeout))
                # Treat malformed provider values as missing, never as a free journey.
                if (not isinstance(fact.duration_minutes, int) or isinstance(fact.duration_minutes, bool)
                        or fact.duration_minutes <= 0 or not isinstance(fact.distance_meters, int)
                        or isinstance(fact.distance_meters, bool) or fact.distance_meters < 0):
                    raise RouteProviderError("invalid access route values")
            except asyncio.TimeoutError:
                # Event-loop timers may wake slightly before monotonic deadline on
                # Windows. A wall-budget timeout must not start another direction.
                if deadline is not None and remaining <= call_timeout:
                    return
                continue
            except RouteProviderError:
                continue
            precise = leg.model_copy(update={"duration_source": "amap",
                "duration_minutes": fact.duration_minutes, "distance_meters": fact.distance_meters})
            known_access[key] = precise
            day.access_legs[index] = precise.model_copy(deep=True)


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
        result.append((known_access or {}).get(_v2_access_key(leg, place), leg).model_copy(deep=True))
    return result


def _v2_finalize_day(day, request):
    day.day_feasibility = evaluate_day(day.places, day.commute_legs, day.access_legs, DayContext(request, get_settings(), day.traffic_policy))
    day.commute_minutes = day.day_feasibility.poi_commute_minutes + (day.day_feasibility.access_minutes or 0)
    day.commute_notes = [leg.note for leg in day.commute_legs]
    summary = access_summary(day)
    if summary:
        day.commute_notes.append(summary)


def _v2_make_plan(groups, request, accommodation_coord, accommodation_source, known, coupling_context=None, anchor=None, known_access=None):
    mode, _ = generation_base_mode(request, get_settings())
    days = []
    for index, places in enumerate(groups, 1):
        legs = _legs(places, generation_mode=mode)
        legs = [known.get((leg.from_place_id, leg.to_place_id, leg.mode), leg).model_copy(deep=True) for leg in legs]
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


def _v2_quality(plan, request, selection, must_ids):
    """Fixed interest dimensions, best ordinary preference per dimension; no count reward."""
    ordinary = [item.place_id for item in selection.selected if item.place_id not in must_ids]
    q = {pid: 1 - i / max(1, len(ordinary)) for i, pid in enumerate(ordinary)}
    dimensions = preference_place_type_dimensions(request.preferences)
    places = [p for day in plan.day_groups for p in day.places]
    coverage = sum(any(p.place_type in types for p in places) for types in dimensions.values())
    if dimensions:
        preference = sum(max((q.get(p.place_id, 0) for p in places if p.place_type in types and p.place_id not in must_ids), default=0)
                         for types in dimensions.values()) / len(dimensions)
    else:
        preference = sum(max((q.get(p.place_id, 0) for p in day.places
                              if p.place_id not in must_ids and _is_multi_area_activity_place(p, request)), default=0)
                         for day in plan.day_groups) / request.days
    types = len({p.place_type for p in places if _is_multi_area_activity_place(p, request)})
    over = sum(max(0, day.day_feasibility.load_minutes - day.day_feasibility.capacity_minutes) for day in plan.day_groups)
    commute = sum(day.commute_minutes for day in plan.day_groups)
    stable = tuple(-p.place_id for day in plan.day_groups for p in day.places)
    if plan.accommodation_policy_version:
        worst_access = max((sum(a.duration_minutes for a in d.access_legs) for d in plan.day_groups),default=0)
        return coverage, preference, types, -over, -commute, -worst_access, stable, -(plan.accommodation_anchor.place_id or 0) if plan.accommodation_anchor else 0
    return coverage, preference, types, -over, -commute, stable


def _v2_plan_violations(plan, request, must_ids, eligible_ids, *, refresh=True):
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
            _v2_finalize_day(day, request)
        violations.extend(day.day_feasibility.violations)
        evening_seen = False
        for place in day.places:
            evening_only = _is_evening_marked(place) and not _is_daytime_marked(place)
            if evening_seen and daytime_activity(place):
                violations.append("daytime_after_evening")
            evening_seen = evening_seen or evening_only
        if any(_selection_temporally_unschedulable(p, request) for p in day.places):
            violations.append("temporally_unschedulable")
        if any(p.place_id not in must_ids and _avoid_conflicts_with_place(request, p) for p in day.places):
            violations.append("hard_avoid")
        if any(_is_near_duplicate_pair(a, b) for a, b in itertools.combinations(day.places, 2)):
            violations.append("near_duplicate")
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
            max(0, feasibility.load_minutes - feasibility.load_limit_minutes) / feasibility.load_limit_minutes,
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


def _v2_seed_groups(request, pool, selection, must_ids, singleton_ids, make_plan, deadline):
    ranked = {item.place_id: i for i, item in enumerate(selection.selected)}
    ordered = sorted(pool, key=lambda p: (p.place_id not in must_ids, ranked.get(p.place_id, 1000), -p.effective_score, p.place_id))
    by_id = {p.place_id: p for p in pool}
    groups = [[by_id[pid]] for pid in singleton_ids]
    fixed = set(range(len(groups)))
    groups.extend([[] for _ in range(request.days - len(groups))])
    used = set(singleton_ids)
    # Put remaining must-go first; extra must-go can share an ordinary day.
    for p in [p for p in ordered if p.place_id in must_ids - used]:
        available = [i for i in range(request.days) if i not in fixed]
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
                if any(_is_near_duplicate_pair(p, x) for x in groups[i]): continue
                trial_groups = [list(g) for g in groups]
                trial_groups[i] = _time_ordered_places([*groups[i], p], request=request)
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
            if not needs_partner and (not trial.day_groups[i].day_feasibility.feasible or
                                      _v2_quality(trial, request, selection, must_ids) <= _v2_quality(current, request, selection, must_ids)):
                break
            groups[i] = ordered_group; used.add(p.place_id)
    return groups


def _v2_repair_proposals(plan, request, pool, must_ids, selection, context=None):
    """Rank bounded changed-day previews; only returned proposals become full trials."""
    context = context or {}
    deadline = context.get("deadline")
    metrics = context.get("metrics")
    groups = [list(d.places) for d in plan.day_groups]
    eligible = {p.place_id for p in pool}
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
        replacements[i] = sorted((p for p in pool if p.place_id not in used), key=lambda p: (
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
            new_unknown = sum((leg.from_place_id, leg.to_place_id, leg.mode) not in known
                              for i in changed for leg in trial.day_groups[i].commute_legs)
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


async def _plan_routes_v2(request, retrieval, selection, pool, provider, metrics,
                          accommodation_coord, accommodation_source, citycode, deadline, route_start, coupling_context=None):
    settings = get_settings()
    if selection.schema_version != "1.1":
        raise RoutePlanInvariantError(["selector_route_version_mismatch"], [])
    must_ids = valid_must_include_ids(request, retrieval) | {p.place_id for p in pool if p.must_include}
    selected_ids = [s.place_id for s in selection.selected]
    all_ids = {p.place_id for p in pool}
    if len(selected_ids) != len(set(selected_ids)) or not set(selected_ids).issubset(all_ids) or not must_ids.issubset(selected_ids):
        raise RoutePlanInvariantError(["invalid_selector_membership"], [])
    pool = [p for p in pool if valid_coordinate(p.latitude,p.longitude) and not _selection_hard_ineligible(p,request)
            and not _selection_temporally_unschedulable(p,request)
            and (p.place_id in must_ids or not _avoid_conflicts_with_place(request,p))]
    eligible = {p.place_id for p in pool}
    if not must_ids.issubset(eligible):
        raise RouteMustIncludeConflictError(must_ids - eligible, "hard_conflict")
    known = {}
    known_access = {}
    attempted_access = set()
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
    own_provider = provider is None and bool(settings.amap_api_key) and settings.amap_route_enabled
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
    option_trials = [0 for _ in options]
    def signature_for(groups, option_index):
        return option_index, tuple(tuple(p.place_id for p in g) for g in groups)
    async def consider(groups, action, option_index=0):
        nonlocal best_valid,best_search,best_search_key,exhausted
        signature = signature_for(groups, option_index)
        if signature in evaluated: return
        if metrics.route_v2_trial_count >= V2_MAX_TRIALS or (deadline is not None and time.monotonic() >= deadline):
            exhausted=True; return
        evaluated.add(signature); metrics.route_v2_trial_count += 1
        option_trials[option_index] += 1
        trial=make_plan(groups, option_index)
        # HTTP/cache owners keep their original caps across every candidate.
        if provider is not None:
            transport = _v2_transport_plan(trial) if callable(getattr(provider, "route_access", None)) else trial
            await _preload_precise_route_cache(provider,[transport],city=retrieval.city,generation_mode=mode)
            allocate=getattr(provider,"allocate_uncached_plan_calls",None)
            if callable(allocate): allocate(transport,city=retrieval.city,generation_mode=mode)
            await _v2_resolve_access(trial, provider, known_access, attempted_access,
                city=retrieval.city, citycode=citycode, deadline=deadline)
            for day in trial.day_groups:
                if deadline is not None and time.monotonic() >= deadline: break
                if not day.commute_legs: continue
                remaining=max(.001,deadline-time.monotonic()) if deadline is not None else 60
                day.commute_legs = await _resolve_precise_legs(
                    day,day.commute_legs,city=retrieval.city,citycode=citycode,generation_mode=mode,
                    requested_commute_mode=request.commute_mode,
                    daily_budget=day.traffic_policy.daily_limit_minutes if day.traffic_policy else daily_commute_budget_minutes(request,settings),
                    single_leg_max=single_leg_max_minutes(mode,settings),provider=provider,metrics=metrics,
                    known_precise_legs=known,concurrency=settings.amap_route_enrichment_concurrency,
                    timeout_seconds=min(remaining,max(settings.amap_route_timeout,1)*len(day.commute_legs)))
        # Existing provider degradation may return estimates for the day: known facts still win.
        for day in trial.day_groups:
            day.commute_legs=[known.get((leg.from_place_id,leg.to_place_id,leg.mode),leg).model_copy(deep=True) for leg in day.commute_legs]
        violations=_v2_plan_violations(trial,request,must_ids,eligible)
        if not violations:
            valid_candidates.append((option_index, trial.model_copy(deep=True)))
            candidate_actions[signature] = action
        if not violations and (best_valid is None or
                _v2_valid_key(trial,request,selection,must_ids) > _v2_valid_key(best_valid,request,selection,must_ids)):
            best_valid=trial.model_copy(deep=True)
            metrics.route_v2_accepted_action=action
        key=_v2_search_key(trial,request,selection,must_ids,violations)
        if option_index not in search_by_option or key > search_by_option[option_index][0]:
            search_by_option[option_index] = (key, trial.model_copy(deep=True))
        if best_search_key is None or key > best_search_key:
            best_search=trial.model_copy(deep=True);best_search_key=key
    try:
        ranks={s.place_id:i for i,s in enumerate(selection.selected)}
        long_ids=[p.place_id for p in sorted(pool,key=lambda p:(p.place_id not in must_ids,ranks.get(p.place_id,1000),p.place_id))
                  if build_visit_profile(p).singleton_eligible and (any(options) if coupling_context else accommodation_coord is not None)]
        configs=[[]]+[long_ids[:n] for n in range(1,min(request.days,len(long_ids))+1)]
        # Round-robin across anchors before spending repair slots on any anchor.
        for config in configs:
            for option_index in range(len(options)):
                if exhausted: break
                builder = lambda groups, i=option_index: make_plan(groups,i)
                groups=_v2_seed_groups(request,pool,selection,must_ids,config,builder,deadline)
                await consider(groups,"initial" if not config else "singleton_configuration",option_index)
            if exhausted: break
        for repair_round in range(V2_MAX_REPAIR_ROUNDS):
            if exhausted or best_search is None: break
            def proposals_for(i):
                return _v2_repair_proposals(search_by_option[i][1],request,pool,must_ids,selection,{
                    "make_plan": lambda groups: make_plan(groups,i), "known": known,
                    "deadline": deadline, "metrics": metrics})
            batches = {i:proposals_for(i) for i in sorted(search_by_option)}
            if not any(batches.values()): break
            metrics.route_v2_repair_rounds=repair_round+1
            previous=metrics.route_v2_trial_count
            refreshed = False
            last_option = -1
            for _ in range(V2_MAX_PROPOSALS):
                # Already evaluated signatures do not consume another trial or hide later actions.
                for i,batch in batches.items():
                    batches[i] = [(a,g) for a,g in batch if signature_for(g,i) not in evaluated]
                available = [i for i,batch in batches.items() if batch]
                if not available: break
                option_index = next((i for i in available if i > last_option), available[0])
                last_option = option_index
                action,groups = batches[option_index].pop(0)
                prior_distance = search_by_option[option_index][0][:3]
                await consider(groups,action,option_index)
                if exhausted: break
                # One extra batch per round, within the same eight full trials: repair
                # newly reduced violations now instead of waiting for the next round.
                if (not refreshed and best_valid is None
                        and search_by_option[option_index][0][:3] > prior_distance):
                    batches[option_index] = proposals_for(option_index)
                    refreshed = True
            if metrics.route_v2_trial_count==previous: break
        exhausted = exhausted or metrics.route_v2_trial_count >= V2_MAX_TRIALS
        # Newly learned precise facts also invalidate an earlier best if they disagree.
        refreshed_valid = []
        for option_index,candidate in valid_candidates:
            refreshed = make_plan([d.places for d in candidate.day_groups], option_index)
            if not _v2_plan_violations(refreshed, request, must_ids, eligible):
                refreshed_valid.append(refreshed)
        best_valid = max(refreshed_valid, key=lambda plan: _v2_valid_key(plan, request, selection, must_ids), default=None)
        if best_valid is None and metrics.route_v2_repair_rounds >= V2_MAX_REPAIR_ROUNDS:
            exhausted = True
        if best_valid is None:
            metrics.route_v2_stop_reason="search_budget_exhausted" if exhausted else "qualified_pool_exhausted"
            raise RoutePlanInvariantError([metrics.route_v2_stop_reason],best_search.day_groups if best_search else [])
        winning_index = next((i for i,a in enumerate(options) if a == best_valid.accommodation_anchor),0)
        metrics.route_v2_accepted_action = candidate_actions[signature_for([d.places for d in best_valid.day_groups],winning_index)]
        if coupling_context:
            from src.agents.accommodation_policy import recommendation_reason
            if best_valid.accommodation_anchor and best_valid.accommodation_anchor.suggestion.source == "auto_recommended":
                best_valid.accommodation_anchor.suggestion.reason = recommendation_reason(best_valid)
            metrics.accommodation_metrics.update(selected_option=winning_index, recommendation_changed=winning_index!=0)
        used=route_plan_place_ids(best_valid)
        by_id={p.place_id:p for p in retrieval.candidates+retrieval.route_planning_candidates}
        _close_selection_ledger(best_valid,selection=selection,candidates_by_id=by_id,
            supplement_reasons={pid:"REPLACE_DROPPED" for pid in used-set(selected_ids)},
            selected_drop_reasons={pid:("HARD_INELIGIBLE" if pid not in eligible else "NOT_CHOSEN_FOR_FINAL_ROUTE")
                                   for pid in selected_ids if pid not in used},request=request)
        metrics.route_v2_stop_reason="best_valid_at_limit" if exhausted else "complete"
        metrics.plan_count=1;metrics.day_count=request.days
        metrics.commute_leg_count=sum(len(day.commute_legs) for day in best_valid.day_groups)
        metrics.record_membership_ledger(best_valid.membership_ledger)
        metrics.route_quality=build_route_quality_metrics(request,retrieval,[best_valid],selected_reason="route_v2_feasibility")
        return [best_valid]
    finally:
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
            accommodation_context if settings.accommodation_route_coupling_enabled else None,
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
        and bool(settings.amap_api_key)
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
    """
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


ROUTE_NIGHT_HINT_PREFIX = "夜游："


def route_night_visit_intervals(day_group, request=None) -> dict[int, tuple[int, int]]:
    """Public routes do not publish clocked night intervals.

    Preference checks treat a missing interval as unscheduled instead of
    inventing a visit window.
    """
    del day_group, request
    return {}


def route_night_hint(place, interval) -> str:
    start, end = interval
    return f"{ROUTE_NIGHT_HINT_PREFIX}{place.name} {int(start)}-{int(end)}"


def route_night_gap(day, request=None):
    """No clocked evening slot exists to miss on the public planner."""
    del day, request
    return None


async def _v2_verify_traffic(
    route,
    *,
    request,
    provider,
    known,
    known_access,
    attempted_access,
    access_walk_rejections,
    city,
    citycode,
    deadline,
    mode,
    metrics,
    must_ids,
    eligible_ids,
    coverage=None,
):
    """Upgrade estimated legs with the existing Amap resolver, then re-check the day.

    Already quoted facts in ``known`` / ``known_access`` win. A missing provider
    leaves the estimates in place.
    """
    del access_walk_rejections, must_ids, eligible_ids, coverage
    settings = get_settings()
    known = {} if known is None else known
    known_access = {} if known_access is None else known_access
    if provider is not None:
        await _v2_resolve_access(
            route,
            provider,
            known_access,
            attempted_access,
            city=city,
            citycode=citycode,
            deadline=deadline,
        )
        for day in route.day_groups:
            if deadline is not None and time.monotonic() >= deadline:
                break
            if not day.commute_legs:
                continue
            remaining = max(0.001, deadline - time.monotonic()) if deadline is not None else 60
            daily_budget = (
                day.traffic_policy.daily_limit_minutes
                if day.traffic_policy is not None
                else daily_commute_budget_minutes(request, settings)
            )
            day.commute_legs = await _resolve_precise_legs(
                day,
                day.commute_legs,
                city=city,
                citycode=citycode,
                generation_mode=mode,
                requested_commute_mode=request.commute_mode,
                daily_budget=daily_budget,
                single_leg_max=single_leg_max_minutes(mode, settings),
                provider=provider,
                metrics=metrics,
                known_precise_legs=known,
                concurrency=settings.amap_route_enrichment_concurrency,
                timeout_seconds=min(
                    remaining,
                    max(settings.amap_route_timeout, 1) * max(1, len(day.commute_legs)),
                ),
            )
    for day in route.day_groups:
        day.commute_legs = [
            known.get((leg.from_place_id, leg.to_place_id, leg.mode), leg).model_copy(deep=True)
            for leg in day.commute_legs
        ]
        _v2_finalize_day(day, request)
