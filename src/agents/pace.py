"""Shared pace detection for route density and commute budgets."""

from __future__ import annotations

from decimal import Decimal, ROUND_CEILING
from typing import Literal

from src.agents.schema import EffectiveCommuteMode, TripRequest
from src.config import Settings

PaceMode = Literal["relaxed", "default", "compact"]

RELAXED_PACE_MARKERS = (
    "不想太累",
    "不要太累",
    "别太累",
    "太累",
    "轻松",
    "悠闲",
    "慢旅行",
    "慢一点",
    "别太赶",
    "不要太赶",
    "不要赶",
    "不赶",
    "慢慢来",
    "慢慢逛",
    "休闲",
)

COMPACT_PACE_MARKERS = (
    "紧凑",
    "充实",
    "特种兵",
    "多玩",
    "多逛",
    "多安排",
    "安排满",
    "行程满",
    "不浪费时间",
)

RELAXED_COMPANION_MARKERS = ("带老人", "带小孩", "带孩子", "带娃")


def detect_pace_mode(request: TripRequest) -> PaceMode:
    """Return the requested itinerary pace.

    Relaxed markers win over compact markers because fatigue avoidance is a
    stronger constraint than filling the day. Compact markers in ``avoid`` are
    an explicit veto, including when the same marker is duplicated into notes.
    """
    relaxed_text = " ".join([*request.preferences, *request.avoid, request.notes])
    if any(marker in relaxed_text for marker in RELAXED_PACE_MARKERS):
        return "relaxed"
    requested_text = " ".join([*request.preferences, request.notes])
    if any(marker in requested_text for marker in RELAXED_COMPANION_MARKERS):
        return "relaxed"

    compact_avoid_text = " ".join(request.avoid)
    compact_vetoed = any(
        marker in compact_avoid_text
        for marker in COMPACT_PACE_MARKERS
    )
    if (
        not compact_vetoed
        and any(marker in requested_text for marker in COMPACT_PACE_MARKERS)
    ):
        return "compact"

    return "default"


def generation_base_mode(
    request: TripRequest,
    settings: Settings,
) -> tuple[EffectiveCommuteMode, str]:
    """Resolve the route-generation mode without mutating the request."""
    if settings.commute_mode_enabled:
        return request.commute_mode, "requested"
    return "driving", "feature_disabled"


def commute_estimate_speed_kmh(
    mode: EffectiveCommuteMode,
    settings: Settings,
) -> float:
    return {
        "driving": settings.commute_estimate_speed_kmh_driving,
        "transit": settings.commute_estimate_speed_kmh_transit,
        "walking": settings.commute_estimate_speed_kmh_walking,
        "cycling": settings.commute_estimate_speed_kmh_cycling,
    }[mode]


def single_leg_max_minutes(mode: EffectiveCommuteMode, settings: Settings) -> int:
    return {
        "driving": settings.route_single_leg_max,
        "transit": settings.route_single_leg_max_transit,
        "walking": settings.route_single_leg_max_walking,
        "cycling": settings.route_single_leg_max_cycling,
    }[mode]


def daily_commute_budget_factor(
    mode: EffectiveCommuteMode,
    settings: Settings,
) -> float:
    return {
        "driving": settings.commute_daily_budget_factor_driving,
        "transit": settings.commute_daily_budget_factor_transit,
        "walking": settings.commute_daily_budget_factor_walking,
        "cycling": settings.commute_daily_budget_factor_cycling,
    }[mode]


def daily_commute_budget_minutes(request: TripRequest, settings: Settings) -> int:
    pace_mode = detect_pace_mode(request)
    if pace_mode == "relaxed":
        pace_budget = settings.route_daily_commute_budget_relaxed
    elif pace_mode == "compact":
        pace_budget = settings.route_daily_commute_budget_compact
    else:
        pace_budget = settings.route_daily_commute_budget_normal
    base_mode, _ = generation_base_mode(request, settings)
    return int(
        (
            Decimal(pace_budget)
            * Decimal(str(daily_commute_budget_factor(base_mode, settings)))
        ).to_integral_value(rounding=ROUND_CEILING)
    )
