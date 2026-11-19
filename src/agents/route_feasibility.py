"""Shared, side-effect-free visit/load policy for Selector and locked Route v2."""
from __future__ import annotations

import math
from dataclasses import dataclass

from src.agents.pace import daily_commute_budget_minutes, detect_pace_mode, single_leg_max_minutes
from src.agents.preferences import FOOD_PARTITION_PLACE_TYPES
from src.agents.schema import DayTrafficPolicy, AccessLeg, CandidatePlace, CommuteLeg, DayFeasibility, TripRequest, VisitProfile, ExcursionTimePolicy
from src.config import Settings

POLICY_VERSION = "selector-route-v2"
DWELL_TIME_MINUTES = {
    "attraction": 75, "museum": 120, "park": 90, "scenic_area": 150,
    "business_area": 60, "market": 45, "street": 45, "photo_spot": 30,
    "restaurant": 60, "cafe": 45, "snack": 30, "food": 60,
    "dessert": 30, "hotel": 0, "other": 75,
}
FOOD_TYPES = set(FOOD_PARTITION_PLACE_TYPES)
EVENING_MARKERS = ("夜景", "日落", "夜游", "灯光", "夜晚")
DAYTIME_MARKERS = ("白天", "上午", "下午", "清晨", "早上", "日间")


def valid_coordinate(latitude: float | None, longitude: float | None) -> bool:
    return (latitude is not None and longitude is not None
            and math.isfinite(latitude) and math.isfinite(longitude)
            and -90 <= latitude <= 90 and -180 <= longitude <= 180)


def daytime_activity(place: CandidatePlace) -> bool:
    text = " ".join([place.name, *(str(item.get("reason", ""))
                    for item in [*place.top_reasons, *place.warnings] if isinstance(item, dict))])
    return place.place_type not in FOOD_TYPES | {"hotel"} and (
        any(marker in text for marker in DAYTIME_MARKERS)
        or not any(marker in text for marker in EVENING_MARKERS))


def build_visit_profile(place: CandidatePlace, policy: str = POLICY_VERSION) -> VisitProfile:
    if policy != POLICY_VERSION:
        raise ValueError("unsupported visit policy")
    value = place.typical_visit_minutes
    valid = (isinstance(value, (int, float)) and not isinstance(value, bool)
             and math.isfinite(value) and value > 0)
    if place.typical_visit_source == "xhs_median" and not place.typical_visit_applicable:
        valid = False
    minutes = max(15, min(480, int(value))) if valid else max(15, DWELL_TIME_MINUTES.get(place.place_type, 60))
    confidence = place.typical_visit_confidence
    if confidence is not None and (not math.isfinite(confidence) or not 0 <= confidence <= 1):
        confidence = None
    reliable = valid and (place.typical_visit_source == "manual" or (
        place.typical_visit_source in {"amap", "xhs_median"}
        and confidence is not None and confidence >= .85))
    return VisitProfile(
        place_id=place.place_id, visit_minutes=minutes,
        basis="canonical" if valid else "type_estimate",
        source=place.typical_visit_source if valid else None,
        confidence=confidence if valid else None,
        uncertainty_minutes=0 if reliable else max(15, math.ceil(minutes * .2)),
        singleton_eligible=bool(reliable and minutes >= 240 and daytime_activity(place)
                                and valid_coordinate(place.latitude, place.longitude)),
    )


def access_status(legs: list[AccessLeg]) -> str:
    if len(legs) != 2 or {leg.direction for leg in legs} != {"outbound", "inbound"}:
        return "unknown"
    count = sum(leg.duration_source == "amap" for leg in legs)
    return "precise" if count == 2 else "mixed" if count else "estimated"


def time_summary(day) -> str:
    """Describe only persisted time facts; never derive historical defaults."""
    policy = getattr(day, "time_policy", None)
    if policy is None:
        return ""
    def clock(value):
        return f"{value // 60:02d}:{value % 60:02d}"
    def intervals(rows):
        return "、".join(f"{clock(left)}–{clock(right)}" for left, right in rows) or "无"
    parts = []
    if policy.reference_start_minute is not None and policy.reference_end_minute is not None:
        parts.append(f"系统参考窗口：{clock(policy.reference_start_minute)}–{clock(policy.reference_end_minute)}")
    labels = {"system": "系统", "user": "用户"}
    source = "系统与用户交集" if policy.original_daily_start or policy.original_daily_end else "系统"
    parts.append(f"有效安排窗口：{clock(policy.start_minute)}–{clock(policy.end_minute)}（{source}；"
                 f"开始来自{labels[policy.start_source]}，结束来自{labels[policy.end_source]}）")
    if policy.system_break_intervals:
        parts.append(f"系统餐段：{intervals(policy.system_break_intervals)}")
    if policy.original_rest_windows:
        parts.append("用户休息：" + "、".join(f"{row['start']}–{row['end']}" for row in policy.original_rest_windows))
    break_source = "系统餐段与用户休息并集" if policy.original_rest_windows and policy.system_break_intervals else (
        "用户休息" if policy.original_rest_windows else "系统餐段" if policy.system_break_intervals else "已锁定餐休")
    parts.append(f"餐休并集：{intervals(policy.break_intervals)}，共{policy.break_minutes}分钟（{break_source}）")
    return "；".join(parts)


def access_summary(day) -> str:
    """User-facing access facts; time-policy diagnostics remain internal."""
    return _access_summary(day)


def _access_summary(day) -> str:
    """One deterministic persisted-fact sentence shared by normal/Safe/Result."""
    if day.day_feasibility is None:
        return ""
    if not day.access_legs:
        policy = getattr(day, "traffic_policy", None)
        if policy and policy.resolution_state == "user_unresolved":
            return "未能确认你填写的住宿位置，本次未计入住宿往返，请核对酒店名称或地址；全天交通覆盖不完整"
        return ""
    legs = {leg.direction: leg for leg in day.access_legs}
    if set(legs) != {"outbound", "inbound"}:
        raise ValueError("incomplete locked accommodation access")
    if any(leg.duration_source == "amap" for leg in legs.values()):
        area = legs["outbound"].anchor_source == "auto_recommended" or (
            getattr(day, "traffic_policy", None) and day.traffic_policy.location_precision == "area")
        origin = "住宿区域参考点" if area else "住宿位置"
        def detail(direction):
            leg = legs[direction]
            suffix = "（距离估算）" if leg.duration_source != "amap" else ""
            return f"约 {leg.duration_minutes} 分钟{suffix}"
        return f"住宿往返：去程{detail('outbound')}，回程{detail('inbound')}；按{origin}计算，实际耗时以出行时为准"
    origin = "按建议住宿区域估算" if legs["outbound"].anchor_source == "auto_recommended" else "按指定住宿位置估算"
    if getattr(day, "traffic_policy", None) and day.traffic_policy.location_precision == "area" and legs["outbound"].anchor_source == "user_specified":
        origin = "按指定住宿区域估算"
    return (f"住宿往返：去程约 {legs['outbound'].duration_minutes} 分钟，"
            f"回程约 {legs['inbound'].duration_minutes} 分钟；{origin}，无接驳地图路线详情")


def capacity_minutes(request: TripRequest, settings: Settings) -> int:
    if not settings.time_preferences_enabled or not request.has_time_preferences():
        return 420
    span = request.daily_span_minutes()
    return max(120, min(420, min(span if span is not None else 420, 420) - request.rest_window_minutes()))


def day_load_limits(request: TripRequest, settings: Settings, time_policy=None) -> tuple[int, int, int]:
    """One capacity/limit/break calculation for style selection and day validation."""
    if time_policy is not None and time_policy.version != "urban-night-time-v1":
        capacity = max(0, time_policy.end_minute - time_policy.start_minute)
        return capacity, capacity, time_policy.break_minutes
    # An urban night clock is an arrangement proof, not a new load allowance.
    # Keep the original ordinary capacity, rest deduction and 1.2 load ceiling.
    capacity = capacity_minutes(request, settings)
    return capacity, math.floor(capacity * 1.2), 0


def day_slot_limit(request: TripRequest, settings: Settings) -> int:
    return max(2, min(4 if detect_pace_mode(request) == "relaxed" else 5, settings.route_places_per_day_max))


@dataclass(frozen=True)
class DayContext:
    request: TripRequest
    settings: Settings
    traffic_policy: DayTrafficPolicy | None = None
    time_policy: ExcursionTimePolicy | None = None
    locked_profiles: tuple[VisitProfile, ...] = ()


def evaluate_day(places: list[CandidatePlace], poi_legs: list[CommuteLeg],
                 access_legs: list[AccessLeg], context: DayContext) -> DayFeasibility:
    profiles = list(context.locked_profiles) if context.locked_profiles else [build_visit_profile(p) for p in places]
    ids = [p.place_id for p in places]
    violations: list[str] = []
    singleton = len(places) == 1 and profiles[0].singleton_eligible
    from src.agents.activity_arrangement import validate_locked_arrangements
    arrangement_violations = validate_locked_arrangements(profiles, context.time_policy, poi_legs, access_legs)
    violations.extend(arrangement_violations)
    full_day = any(p.arrangement == "full_day" for p in profiles)
    access_known = len(access_legs) == 2 and {a.direction for a in access_legs} == {"outbound", "inbound"}
    if context.locked_profiles and [p.place_id for p in profiles] != ids:
        violations.append("locked_profile_mismatch")
    if (access_legs or (context.time_policy is not None and context.traffic_policy is not None and context.traffic_policy.day_kind == "excursion")) and not access_known:
        violations.append("invalid_access_coverage")
    if access_known:
        by_direction = {a.direction: a for a in access_legs}
        if not ids or by_direction["outbound"].place_id != ids[0] or by_direction["inbound"].place_id != ids[-1]:
            violations.append("access_endpoint_mismatch")
        anchors = {(a.anchor_source, a.anchor_latitude, a.anchor_longitude) for a in access_legs}
        if len(anchors) != 1:
            violations.append("access_anchor_mismatch")
    if not (2 <= len(places) <= day_slot_limit(context.request, context.settings) or ((singleton or full_day) and access_known)):
        violations.append("day_structure")
    if (context.traffic_policy is not None and context.traffic_policy.day_kind == "excursion"
        and not singleton and not full_day and sum(p.place_type not in FOOD_TYPES | {"hotel"} for p in places) < 2):
        if "day_structure" not in violations:
            violations.append("day_structure")
    if len(ids) != len(set(ids)):
        violations.append("duplicate_place")
    if not any(daytime_activity(p) for p in places):
        violations.append("no_daytime_activity")
    if any(p.place_type == "hotel" or not valid_coordinate(p.latitude, p.longitude) for p in places):
        violations.append("ineligible_place")
    if [(leg.from_place_id, leg.to_place_id) for leg in poi_legs] != list(zip(ids, ids[1:])):
        violations.append("poi_leg_mismatch")
    if any(leg.duration_minutes < 0 or leg.distance_meters < 0 for leg in poi_legs):
        violations.append("invalid_leg")
    visit = sum(p.visit_minutes for p in profiles)
    uncertainty = sum(p.uncertainty_minutes for p in profiles)
    poi_minutes = sum(leg.duration_minutes for leg in poi_legs)
    access_minutes = sum(leg.duration_minutes for leg in access_legs) if access_known else None
    capacity, limit, breaks = day_load_limits(context.request, context.settings, context.time_policy)
    load = visit + uncertainty + poi_minutes + (access_minutes or 0) + breaks
    if context.time_policy is not None:
        violations.extend(context.time_policy.violations)
    if load > limit:
        violations.append("load_limit")
    policy = context.traffic_policy
    daily_limit = policy.daily_limit_minutes if policy else daily_commute_budget_minutes(context.request, context.settings)
    # The urban target is a later optimization, not this phase's hard gate.
    # Excursion limits and total day load remain authoritative.
    enforce_daily = context.settings.route_urban_commute_limit_enabled or (
        policy is not None and policy.day_kind == "excursion")
    if enforce_daily and poi_minutes + (access_minutes or 0) > daily_limit:
        violations.append("daily_commute_limit")
    if any(leg.duration_minutes > (policy.poi_leg_limits[leg.mode] if policy else single_leg_max_minutes(leg.mode, context.settings)) for leg in poi_legs) or any(
        leg.duration_minutes > (policy.access_leg_limits[leg.mode] if policy else single_leg_max_minutes(leg.mode, context.settings)) for leg in access_legs):
        violations.append("single_leg_limit")
    return DayFeasibility(
        ordered_place_ids=ids, visit_minutes=visit, uncertainty_minutes=uncertainty,
        poi_commute_minutes=poi_minutes, access_minutes=access_minutes,
        capacity_minutes=capacity, load_limit_minutes=limit, load_minutes=load, break_minutes=breaks,
        access_status=access_status(access_legs), violations=violations,
        singleton_eligible=singleton, precise_leg_count=sum(leg.source == "amap" for leg in poi_legs) + sum(leg.duration_source == "amap" for leg in access_legs),
        estimated_leg_count=sum(leg.source != "amap" for leg in poi_legs) + sum(leg.duration_source != "amap" for leg in access_legs),
    )
