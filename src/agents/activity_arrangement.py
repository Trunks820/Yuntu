"""Source-bound full-day/variant decisions and conservative address containment."""
import re
from src.agents.excursion_time_policy import freeze_time_policy


def source_receipts(place):
    return [dict(row) for row in place.arrangement_evidence if isinstance(row, dict)
            and row.get("source_id") and row.get("evidence") and row.get("whole_target") == place.name]


def full_day_place(place):
    return any(row.get("kind") == "full_day" for row in source_receipts(place))


def nested_relation(parent, child):
    if parent.place_id == child.place_id or not parent.city or parent.city != child.city:
        return False
    if parent.place_type not in {"scenic_area", "park", "attraction"} or not parent.address or not child.address:
        return False
    tokens = re.findall(r"[\u4e00-\u9fffA-Za-z]+(?:路|街|大道|巷)\d+号", re.split(r"省|市|区|县|镇|乡|村", re.sub(r"\s+", "", parent.address))[-1])
    return bool(tokens and parent.name in child.address and any(t in re.sub(r"\s+", "", child.address) for t in tokens))


def confirmed_coverage(parent, child):
    from src.agents.route_feasibility import build_visit_profile
    profile = build_visit_profile(parent)
    return nested_relation(parent, child) and any(
        (row.get("kind") == "full_day" or (row.get("kind") == "whole_visit"
            and profile.basis == "canonical" and profile.visit_minutes == row.get("minutes")))
        and child.name in row.get("covered_names", []) for row in source_receipts(parent))


def resolve_profiles(places, profiles, request, poi_legs, access_legs, time_policy=None, *, settings=None):
    profiles = [p.model_copy(deep=True) for p in profiles]
    by_id = {p.place_id:p for p in profiles}
    violations = []
    relations = {p.place_id: [c.place_id for c in places if nested_relation(p,c)] for p in places}
    # A cycle or ambiguous multiple parents cannot transfer time ownership.
    parents = {p.place_id: [a.place_id for a in places if p.place_id in relations[a.place_id]] for p in places}
    def cyclic(start, current, seen):
        return any(c == start or (c not in seen and cyclic(start,c,seen|{c})) for c in relations[current])
    unsafe = {p.place_id for p in places if cyclic(p.place_id,p.place_id,{p.place_id}) or len(parents[p.place_id]) > 1}
    covered = set()
    for place in places:
        profile = by_id[place.place_id]
        profile.evidence = source_receipts(place)
        profile.nested_place_ids = relations[place.place_id]
        profile.contained_place_ids = [c.place_id for c in places if c.place_id not in unsafe
            and place.place_id not in unsafe and confirmed_coverage(place,c)]
        covered.update(profile.contained_place_ids)
        if profile.nested_place_ids and not profile.contained_place_ids:
            profile.arrangement_warnings.append("containment_coverage_unconfirmed")
        if place.place_id in unsafe or (len(places) > 1 and (not place.city or not place.address)):
            profile.arrangement_warnings.append("containment_unresolved")
        if full_day_place(place):
            profile.arrangement = "full_day"
            profile.basis = "full_day_evidence"
            profile.visit_minutes = 0
            profile.uncertainty_minutes = 0
            profile.singleton_eligible = False
    for pid in covered:
        by_id[pid].visit_minutes = 0
        by_id[pid].uncertainty_minutes = 0
        by_id[pid].singleton_eligible = False
    from src.agents.route_feasibility import day_load_limits
    from src.config import get_settings
    full_days = [p for p in profiles if p.arrangement == "full_day" and p.place_id not in covered]
    window = time_policy or (freeze_time_policy(request) if full_days else None)
    _, limit, breaks = day_load_limits(request, settings or get_settings(), window)
    available = limit - breaks
    nonvisit = sum(l.duration_minutes for l in poi_legs) + sum(l.duration_minutes for l in access_legs)
    if full_days:
        violations.extend(window.violations)
        if window.end_minute-window.start_minute < window.reference_span_minutes or available-nonvisit <= 0:
            violations.append("window_conflict")
        if len(full_days) != 1 or any(p.place_id != full_days[0].place_id and p.place_id not in covered for p in profiles):
            violations.append("full_day_conflict")
        if len(access_legs) != 2:
            violations.append("invalid_access_coverage")
    variant_ids = []
    for place in places:
        profile = by_id[place.place_id]
        variants = [row for row in profile.evidence if row.get("kind") == "play_style"]
        if variants and place.place_id not in covered and profile.arrangement != "full_day":
            profile.play_styles = variants
            profile.visit_minutes = 0
            profile.uncertainty_minutes = 0
            profile.singleton_eligible = False
            variant_ids.append(place.place_id)
    committed = nonvisit + sum(p.visit_minutes+p.uncertainty_minutes for p in profiles if p.place_id not in variant_ids)
    valid_variants = {pid: [row for row in by_id[pid].play_styles if isinstance(row.get("minutes"),int)
        and not isinstance(row["minutes"],bool) and 15 <= row["minutes"]] for pid in variant_ids}
    lower_bounds = {pid: min((row["minutes"] for row in rows),default=float("inf"))
                    for pid,rows in valid_variants.items()}
    for pid in variant_ids:
        profile = by_id[pid]
        remaining = available - committed - sum(value for other,value in lower_bounds.items() if other != pid)
        # Lower bounds establish joint feasibility only; they never select a
        # shortest variant. Every compatible source variant remains eligible.
        usable = [row for row in valid_variants[pid] if row["minutes"] <= remaining]
        if window is not None and window.violations:
            profile.basis = "window_conflict"
            profile.arrangement_warnings.extend(window.violations)
            violations.append("window_conflict")
        elif len(usable) == 1:
            profile.play_style = usable[0]
            profile.visit_minutes = usable[0]["minutes"]
            profile.source = "xhs_play_style"
            profile.basis = "canonical"
            from src.agents.route_feasibility import daytime_activity, valid_coordinate
            point = next(place for place in places if place.place_id == pid)
            profile.singleton_eligible = (profile.visit_minutes >= 240 and daytime_activity(point)
                and valid_coordinate(point.latitude, point.longitude))
        elif usable:
            profile.basis = "unresolved_play_style"
            profile.arrangement_warnings.append("play_style_choice_pending")
            violations.append("unresolved_play_style")
        else:
            profile.basis = "window_conflict"
            violations.append("window_conflict")
    return profiles, list(dict.fromkeys(violations))


def validate_locked_arrangements(profiles, time_policy, poi_legs, access_legs):
    violations = []
    covered = {pid for p in profiles for pid in p.contained_place_ids}
    full = [p for p in profiles if p.arrangement == "full_day" and p.place_id not in covered]
    if full:
        if len(full) != 1 or any(p.place_id != full[0].place_id and p.place_id not in covered for p in profiles):
            violations.append("full_day_conflict")
        if time_policy is None or (time_policy.end_minute-time_policy.start_minute < time_policy.reference_span_minutes
            or time_policy.end_minute-time_policy.start_minute-time_policy.break_minutes
            - sum(l.duration_minutes for l in [*poi_legs,*access_legs]) <= 0):
            violations.append("window_conflict")
        if len(access_legs) != 2:
            violations.append("invalid_access_coverage")
    for p in profiles:
        if p.basis in {"unresolved_play_style", "window_conflict"}:
            violations.append(p.basis)
    return list(dict.fromkeys(violations))
