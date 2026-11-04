"""Read-only projection of locked daily feasibility for prose and review.

No scheduling, scoring, thresholds, or new feasibility verdicts live here.
"""
import json

from src.agents.schema import RouteDayGroup, RoutePlan


def day_experience_facts(day: RouteDayGroup) -> dict:
    check = day.day_feasibility
    fields = ("visit_minutes", "uncertainty_minutes", "poi_commute_minutes",
              "access_minutes", "capacity_minutes", "load_limit_minutes",
              "load_minutes", "break_minutes", "access_status")
    facts = {"day": day.day, "place_ids": [p.place_id for p in day.places]}
    facts.update({
        name: getattr(check, name, None) if check is not None else None
        for name in fields
    })
    facts["total_commute_minutes"] = (
        check.poi_commute_minutes + check.access_minutes
        if check is not None and check.access_minutes is not None else None)
    facts["commute_duration_sources"] = [leg.source for leg in day.commute_legs]
    facts["access_duration_sources"] = [leg.duration_source for leg in day.access_legs]
    facts["violations"] = list(check.violations) if check is not None else None
    return facts


def route_experience_facts(route: RoutePlan) -> dict:
    return {"days": [day_experience_facts(day) for day in route.day_groups]}


def day_experience_prompt(day: RouteDayGroup) -> str:
    return ("锁定每日体验事实（分钟；access_minutes 含住宿接驳，total_commute_minutes 为全部交通；"
            "null 表示未提供，不能按零处理。时长是规划参考，保留duration_sources来源，estimate不能写成已验证交通。可行不等于轻松，活动主题不能证明全天不赶）："
            + json.dumps(day_experience_facts(day), ensure_ascii=False))


