"""Versioned lodging/traffic policy. No I/O, no LLM decisions."""
from __future__ import annotations
import math
from src.agents.schema import DayTrafficPolicy
from src.agents.pace import detect_pace_mode, generation_base_mode, daily_commute_budget_minutes, single_leg_max_minutes

POLICY = "accommodation-route-v1"
MAX_AREAS = 3
# remote_anchor treats a place at least this far from the core as a far suburb.
REMOTE_DISTANCE_KM = 20


def is_remote_distance(kilometers: float) -> bool:
    """True when a point is a far suburb of the reference center.

    The same comparison ``remote_anchor`` uses: closer than
    ``REMOTE_DISTANCE_KM`` stays in town, and a point at exactly that
    distance is remote.
    """
    return float(kilometers) >= REMOTE_DISTANCE_KM

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

def remote_anchor(places, core, poi_legs=()):
    """5A independently remote activity with nearby remote driving companions."""
    if core is None or not places or any(p.latitude is None or p.longitude is None for p in places):
        return None, {}
    from src.agents.route_planning import _estimate_route_values
    for anchor in sorted(places, key=lambda p: (not p.must_include, p.place_id)):
        if anchor.place_type in {"hotel", "restaurant", "food", "cafe", "snack", "dessert", "market", "photo_spot"}:
            continue
        if not is_remote_distance(distance_km(core, (anchor.latitude, anchor.longitude))):
            continue
        accepted, sources = True, {}
        for companion in places:
            if companion.place_id == anchor.place_id:
                continue
            if (distance_km(core, (companion.latitude, companion.longitude)) < 10 or
                distance_km((anchor.latitude, anchor.longitude), (companion.latitude, companion.longitude)) > 15):
                accepted = False; break
            fact = next((leg for leg in poi_legs if leg.mode == "driving" and
                         {leg.from_place_id, leg.to_place_id} == {anchor.place_id, companion.place_id}), None)
            if fact is None:
                _, _, minutes = _estimate_route_values(anchor, companion, effective_mode="driving")
                source = "estimate"
            else:
                minutes, source = fact.duration_minutes, fact.source
            if minutes > 45:
                accepted = False; break
            sources[companion.place_id] = source
        if accepted:
            return anchor, sources
    return None, {}


def excursion_daily_limit_minutes(request, settings):
    """Far-suburb daily commute cap. Same numbers as the excursion branch of day_policy."""
    pace = detect_pace_mode(request)
    mode, _ = generation_base_mode(request, settings)
    if mode == "driving":
        return {"relaxed":300, "default":360, "compact":360}[pace]
    from src.agents.pace import daily_commute_budget_factor
    return math.ceil({"relaxed":180,"default":240,"compact":270}[pace] * daily_commute_budget_factor(mode,settings))


def day_policy(places, request, settings, context, anchor, poi_legs=()):
    core = context.urban_core
    kind, basis = ("unknown", "no_independent_core") if core is None else ("urban", "canonical_accommodation_cluster")
    remote, sources = remote_anchor(places, core, poi_legs)
    if remote is not None:
        kind = "excursion"
    pace = detect_pace_mode(request)
    mode, _ = generation_base_mode(request, settings)
    poi = {m: single_leg_max_minutes(m, settings) for m in ("driving", "transit", "walking", "cycling")}
    access = dict(poi)
    daily = daily_commute_budget_minutes(request, settings)
    if kind == "excursion":
        access.update(driving={"relaxed":150, "default":180, "compact":180}[pace], transit=160, cycling=60)
        daily = excursion_daily_limit_minutes(request, settings)
    return DayTrafficPolicy(day_kind=kind, classification_basis=basis,
        daily_limit_minutes=daily, poi_leg_limits=poi, access_leg_limits=access,
        resolution_state=context.state, location_precision=anchor.location_precision if anchor else "unknown",
        urban_core=core, companion_route_sources=sources)

def recommendation_reason(plan):
    values = [sum(a.duration_minutes for a in d.access_legs) for d in plan.day_groups if len(d.access_legs)==2]
    if len(values)!=len(plan.day_groups) or not values: return "住宿往返时间暂未完整核算"
    return f"按最终行程估算，每天住宿往返合计约 {min(values)}–{max(values)} 分钟"
