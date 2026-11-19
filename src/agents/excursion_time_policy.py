"""Frozen excursion windows; pure arithmetic, never mutates the request."""
from src.agents.schema import ExcursionTimePolicy, parse_hhmm
from src.agents.pace import detect_pace_mode


def freeze_time_policy(request):
    pace = detect_pace_mode(request)
    start, end = {"relaxed": (540, 1080), "default": (480, 1140), "compact": (480, 1200)}[pace]
    reference = end - start
    user_start, user_end = parse_hhmm(request.daily_start), parse_hhmm(request.daily_end)
    effective_start = max(start, user_start) if user_start is not None else start
    effective_end = min(end, user_end) if user_end is not None else end
    violations = []
    if effective_end <= effective_start:
        violations.append("schedule_conflict")
    intervals = [(720, 780), (1080, 1140)]
    for rest in request.rest_windows:
        left, right = parse_hhmm(rest.start), parse_hhmm(rest.end)
        if left < effective_start or right > effective_end:
            violations.append("rest_window_conflict")
        intervals.append((left, right))
    clipped = sorted((max(left, effective_start), min(right, effective_end))
                     for left, right in intervals if max(left, effective_start) < min(right, effective_end))
    merged = []
    for left, right in clipped:
        if merged and left <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(right, merged[-1][1]))
        else:
            merged.append((left, right))
    return ExcursionTimePolicy(pace=pace, start_minute=effective_start, end_minute=effective_end,
        start_source="user" if user_start is not None and user_start >= start else "system",
        end_source="user" if user_end is not None and user_end <= end else "system",
        reference_span_minutes=reference, reference_start_minute=start, reference_end_minute=end,
        system_break_intervals=[(720, 780), (1080, 1140)], original_daily_start=request.daily_start,
        original_daily_end=request.daily_end, original_rest_windows=[r.model_dump() for r in request.rest_windows],
        break_intervals=merged, break_minutes=sum(r-l for l,r in merged), violations=list(dict.fromkeys(violations)))
