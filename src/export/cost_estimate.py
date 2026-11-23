"""Shared artifact presentation of the public persisted cost projection."""

from __future__ import annotations

from typing import Any

from src.cost_estimate.public import (
    CostEstimateSummary,
    PublicMoneyRangeCny,
)


CATEGORY_LABELS = {
    "intercity_transport": "往返大交通",
    "accommodation": "住宿",
    "local_transport": "市内交通",
    "admission": "门票",
    "meals": "午晚餐",
}
COMPLETENESS_LABELS = {
    "complete": "完整预估",
    "partial": "已估子集",
    "unavailable": "费用暂不可估算",
}


def validate_artifact_cost_estimate(raw: Any) -> CostEstimateSummary:
    return CostEstimateSummary.model_validate(raw)


def range_text(value: PublicMoneyRangeCny) -> str:
    if value.min_cny == value.max_cny:
        return f"¥{value.min_cny}"
    return f"¥{value.min_cny}–¥{value.max_cny}"


def local_transport_taxi_suffix(summary: CostEstimateSummary) -> str:
    if any(
        item.code == "four_travellers_per_taxi" and item.label.startswith("市内交通按打车估算")
        for item in summary.assumptions
    ):
        return "（按打车估算）"
    return ""


def cost_estimate_lines(raw: Any) -> tuple[str, ...]:
    summary = validate_artifact_cost_estimate(raw)
    taxi_suffix = local_transport_taxi_suffix(summary)
    lines = [
        f"估算状态：{COMPLETENESS_LABELS[summary.completeness]}",
        f"估算时间：{summary.estimated_at.isoformat().replace('+00:00', 'Z')}",
    ]
    for scenario in summary.scenarios:
        total = scenario.total_range_with_known_admission or scenario.total_range
        if total is None:
            total_text = "费用暂不可估算"
        elif scenario.total_scope == "full_trip":
            total_text = f"预估 {range_text(total)}"
        else:
            total_text = f"已估 {range_text(total)}｜部分费用待确认"
        if scenario.unpriced_admission_count:
            total_text += f"｜另有 {scenario.unpriced_admission_count} 处门票未计入"
        lines.append(f"{scenario.label}｜{total_text}")
        for category in scenario.categories:
            category_label = CATEGORY_LABELS[category.category]
            suffix = taxi_suffix if category.category == "local_transport" else ""
            if (
                category.category == "admission"
                and category.known_range is not None
                and category.unpriced_item_count
            ):
                lines.append(
                    f"{category_label}：已知 {range_text(category.known_range)}，"
                    f"另有 {category.unpriced_item_count} 处待确认"
                )
            elif category.range is None:
                lines.append(f"{category_label}：待确认（{category.basis_label}）{suffix}")
            else:
                lines.append(
                    f"{category_label}：{range_text(category.range)}（{category.basis_label}）{suffix}"
                )
    if summary.assumptions:
        lines.append("估算假设：" + "；".join(item.label for item in summary.assumptions))
    if summary.exclusions:
        lines.append("未计范围：" + "；".join(item.label for item in summary.exclusions))
    lines.append(summary.notice)
    return tuple(lines)


__all__ = [
    "CATEGORY_LABELS",
    "cost_estimate_lines",
    "local_transport_taxi_suffix",
    "range_text",
    "validate_artifact_cost_estimate",
]
