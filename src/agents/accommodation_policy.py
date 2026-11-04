"""Versioned lodging/traffic policy. No I/O, no LLM decisions."""
from __future__ import annotations
import math
from src.agents.schema import DayTrafficPolicy
from src.agents.pace import detect_pace_mode, generation_base_mode, daily_commute_budget_factor, single_leg_max_minutes

POLICY = "accommodation-route-v1"
MAX_AREAS = 3

def distance_km(a, b):
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    return 12742 * math.asin(min(1, math.sqrt(math.sin((lat1-lat2)/2)**2 +
        math.cos(lat1)*math.cos(lat2)*math.sin((lon1-lon2)/2)**2)))

def anchor_coord(anchor):
    return (anchor.suggestion.latitude, anchor.suggestion.longitude)

def urban_core(anchors):
    """Independent canonical accommodation cluster; never center on selected POIs."""
    if len(anchors) < 3:
        return None
    ordered = sorted(anchors, key=lambda a: (
        -sum(distance_km(anchor_coord(a), anchor_coord(b)) <= 10 for b in anchors),
        a.place_id or 0))
    cluster = [a for a in anchors if distance_km(anchor_coord(a), anchor_coord(ordered[0])) <= 10]
    if len(cluster) < 3:
        return None
    best = min(cluster, key=lambda a: (
        sum(distance_km(anchor_coord(a), anchor_coord(b)) for b in cluster), a.place_id or 0))
    return anchor_coord(best)

def choose_options(context, pool, selection, must_ids):
    if context.state != "auto_candidates":
        return context.anchors or [None]
    anchors = context.anchors
    if len(anchors) <= MAX_AREAS:
        return anchors
    by_id = {p.place_id:p for p in pool}
    required = [by_id[pid] for pid in sorted(must_ids) if pid in by_id]
    # Bound contribution per type so many similar short stops cannot dominate.
    preferred = list(required)
    types = set()
    for item in selection.selected:
        p = by_id.get(item.place_id)
        if p is not None and p.place_type not in types and p not in preferred:
            types.add(p.place_type)
            preferred.append(p)
    def cost(a, points):
        return sum(distance_km(anchor_coord(a), (p.latitude,p.longitude)) for p in points)/max(1,len(points))
    chosen = [anchors[0]]
    for points in (required or preferred, preferred):
        if points:
            best = min(anchors, key=lambda a:(cost(a, points), a.place_id or 0))
            if best not in chosen: chosen.append(best)
    for a in sorted(anchors,key=lambda a:(cost(a,preferred),a.place_id or 0)):
        if len(chosen) >= MAX_AREAS: break
        if a not in chosen: chosen.append(a)
    return chosen

def day_policy(places, request, settings, context, anchor):
    core = context.urban_core
    located = [p for p in places if p.latitude is not None and p.longitude is not None]
    primary = [p for p in located if p.place_type not in {
        "hotel","restaurant","food","cafe","snack","dessert","market","photo_spot"}]
    kind, basis = "unknown", "no_independent_core"
    if core is not None:
        kind, basis = "urban", "canonical_accommodation_cluster"
        if primary and len(located)==len(places):
            remote = all(distance_km(core,(p.latitude,p.longitude)) >= 20 for p in primary)
            compact = all(distance_km((a.latitude,a.longitude),(b.latitude,b.longitude)) <= 15
                          for a in located for b in located)
            if remote and compact: kind = "excursion"
    pace = detect_pace_mode(request)
    base = {"relaxed":100,"default":150,"compact":180}
    if kind == "excursion": base = {"relaxed":180,"default":240,"compact":270}
    mode, _ = generation_base_mode(request,settings)
    poi = {m:single_leg_max_minutes(m,settings) for m in ("driving","transit","walking","cycling")}
    access = dict(poi)
    if kind == "excursion": access.update(driving=120,transit=160,cycling=60)
    return DayTrafficPolicy(day_kind=kind,classification_basis=basis,
        daily_limit_minutes=math.ceil(base[pace]*daily_commute_budget_factor(mode,settings)),
        poi_leg_limits=poi,access_leg_limits=access,resolution_state=context.state,
        location_precision=anchor.location_precision if anchor else "unknown")

def is_remote_distance(km: float) -> bool:
    """Same 20 km cutoff the urban/excursion day classifier already uses."""
    try:
        return float(km) >= 20
    except (TypeError, ValueError):
        return False


def excursion_daily_limit_minutes(request, settings) -> int:
    """Excursion-day commute cap. Urban days use the planner's own cap."""
    pace = detect_pace_mode(request)
    base = {"relaxed": 180, "default": 240, "compact": 270}[pace]
    mode, _reason = generation_base_mode(request, settings)
    return math.ceil(base * daily_commute_budget_factor(mode, settings))


def recommendation_reason(plan):
    values = [sum(a.duration_minutes for a in d.access_legs) for d in plan.day_groups if len(d.access_legs)==2]
    if len(values)!=len(plan.day_groups) or not values: return "住宿往返时间暂未完整核算"
    return f"按最终行程估算，每天住宿往返合计约 {min(values)}–{max(values)} 分钟"
