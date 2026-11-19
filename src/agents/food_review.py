"""Deterministic Food Review and food-span helpers for v0.9.0."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from src.agents.food_resolver import FoodAttachmentAuthorization


FoodAttachmentKey = tuple[int, int, int, str, int]
FoodAttachmentAuthMap = dict[
    FoodAttachmentKey,
    FoodAttachmentAuthorization,
]

FOOD_SPAN_RE = re.compile(
    r"<!--\s*food:plan(?P<plan_index>\d+)"
    r"_day(?P<day>\d+)"
    r"_anchor(?P<anchor_place_id>\d+)"
    r"_slot(?P<meal_slot>[^\s]+?)"
    r"_food(?P<food_place_id>-?\d+)\s*-->"
    r"(?P<content>.*?)"
    r"<!--\s*/food\s*-->",
    re.DOTALL,
)
FOOD_OPEN_MARKER_RE = re.compile(
    r"<!--\s*food:plan\d+_day\d+_anchor\d+"
    r"_slot[^\s]+?_food-?\d+\s*-->"
)
FOOD_CLOSE_MARKER_RE = re.compile(r"<!--\s*/food\s*-->")

FOOD_SOURCE_ATTRIBUTION_RE = re.compile(
    r"(?:笔记里提到|博主推荐|据说|亲测|作者推荐|当地推荐|"
    r"当地美食推荐|来源推荐|根据数据)"
)
FOOD_STRUCTURAL_EXPERIENCE_RE = re.compile(
    r"(?:招牌|必点|主打|提供|供应|可以吃到|菜品|口味|食材|"
    r"环境|氛围|新鲜|好吃|美味|推荐|值得|性价比|人气|排队|"
    r"想必|体验)"
)
_NONE_TIER_TRAILING_PUNCTUATION_RE = re.compile(r"[，,。！？!?；;：:]+$")


@dataclass(frozen=True)
class FoodProseSpan:
    """One complete marker-delimited food prose span."""

    key: FoodAttachmentKey
    content: str
    start: int
    content_start: int
    content_end: int
    end: int


@dataclass(frozen=True)
class FoodRuleViolation:
    """Stable deterministic Review finding for one food span."""

    reason: str
    key: FoodAttachmentKey
    start: int
    end: int
    snippet: str


def parse_food_spans(text: str) -> list[FoodProseSpan]:
    spans: list[FoodProseSpan] = []
    for match in FOOD_SPAN_RE.finditer(text or ""):
        key: FoodAttachmentKey = (
            int(match.group("plan_index")),
            int(match.group("day")),
            int(match.group("anchor_place_id")),
            match.group("meal_slot"),
            int(match.group("food_place_id")),
        )
        spans.append(FoodProseSpan(
            key=key,
            content=match.group("content"),
            start=match.start(),
            content_start=match.start("content"),
            content_end=match.end("content"),
            end=match.end(),
        ))
    return spans


def strip_food_span_markers(text: str) -> str:
    """Remove internal markers while preserving the user-facing food prose."""

    cleaned = FOOD_OPEN_MARKER_RE.sub("", text or "")
    return FOOD_CLOSE_MARKER_RE.sub("", cleaned)


def authorized_food_names_for_plan(
    attachment_auth_map: FoodAttachmentAuthMap | None,
    *,
    plan_index: int,
) -> set[str]:
    if not attachment_auth_map:
        return set()
    return {
        authorization.food_name
        for key, authorization in attachment_auth_map.items()
        if key[0] == plan_index and authorization.food_name
    }


def mask_authorized_food_names_in_spans(
    text: str,
    attachment_auth_map: FoodAttachmentAuthMap | None,
    *,
    plan_index: int,
) -> str:
    """Mask only the authorized food name inside its exact authorized span."""

    if not attachment_auth_map:
        return text
    masked = list(text)
    for span in parse_food_spans(text):
        if span.key[0] != plan_index:
            continue
        authorization = attachment_auth_map.get(span.key)
        food_name = authorization.food_name if authorization is not None else None
        if not food_name:
            continue
        for match in re.finditer(re.escape(food_name), span.content):
            start = span.content_start + match.start()
            end = span.content_start + match.end()
            masked[start:end] = " " * (end - start)
    return "".join(masked)


def mask_authorized_food_prose(
    text: str,
    attachment_auth_map: FoodAttachmentAuthMap | None,
    *,
    plan_index: int,
) -> str:
    """Hide authorized food prose from generic fact-expansion classifiers."""

    if not attachment_auth_map:
        return text
    masked = list(text)
    for span in parse_food_spans(text):
        if span.key[0] != plan_index or span.key not in attachment_auth_map:
            continue
        masked[span.content_start:span.content_end] = (
            " " * (span.content_end - span.content_start)
        )
    return "".join(masked)


def _normalize_none_tier_text(text: str) -> str:
    """Ignore harmless Writer formatting without accepting prose expansion."""

    normalized = unicodedata.normalize("NFKC", text or "")
    normalized = re.sub(r"\s+", "", normalized)
    return _NONE_TIER_TRAILING_PUNCTUATION_RE.sub("", normalized)


def find_food_rule_violations(
    plan_text: str,
    attachment_auth_map: FoodAttachmentAuthMap | None,
    *,
    plan_index: int,
    evidence_tier_by_key: dict[FoodAttachmentKey, str] | None = None,
) -> list[FoodRuleViolation]:
    """Apply the three P4 Food Review rules to one zero-indexed plan."""

    if not attachment_auth_map:
        return []
    tiers = evidence_tier_by_key or {}
    spans = [
        span
        for span in parse_food_spans(plan_text)
        if span.key[0] == plan_index and span.key in attachment_auth_map
    ]
    spans_by_key = {span.key: span for span in spans}
    violations: list[FoodRuleViolation] = []

    for key, authorization in attachment_auth_map.items():
        if key[0] != plan_index:
            continue
        tier = tiers.get(key) or authorization.evidence_tier
        span = spans_by_key.get(key)
        if tier == "none":
            expected = authorization.none_tier_text or ""
            actual = span.content if span is not None else ""
            if (
                span is None
                or _normalize_none_tier_text(actual)
                != _normalize_none_tier_text(expected)
            ):
                violations.append(FoodRuleViolation(
                    reason="food_none_tier_violation",
                    key=key,
                    start=span.content_start if span is not None else 0,
                    end=span.content_end if span is not None else max(1, len(plan_text)),
                    snippet=actual,
                ))
            continue
        if span is None:
            continue

        source_match = FOOD_SOURCE_ATTRIBUTION_RE.search(span.content)
        if source_match is not None:
            violations.append(FoodRuleViolation(
                reason="food_source_attribution",
                key=key,
                start=span.content_start + source_match.start(),
                end=span.content_start + source_match.end(),
                snippet=source_match.group(0),
            ))

        if tier == "structural":
            # the restaurant's own name is no review: "雀食府好吃街"
            name = authorization.food_name
            content = span.content.replace(name, "") if name else span.content
            tier_match = FOOD_STRUCTURAL_EXPERIENCE_RE.search(content)
            if tier_match is not None:
                violations.append(FoodRuleViolation(
                    reason="food_tier_exceeded",
                    key=key,
                    start=span.content_start,
                    end=span.content_end,
                    snippet=span.content,
                ))
    return violations


def _structural_food_content(
    authorization: FoodAttachmentAuthorization,
) -> str:
    meal_text = "晚餐可以去" if authorization.meal_slot == "dinner" else "午餐可以去"
    food_name = authorization.food_name or authorization.meal_type or "餐馆"
    parts = [f"{meal_text}附近{food_name}"]
    if authorization.walk_minutes is not None:
        parts.append(f"步行约{authorization.walk_minutes}分钟")
    if authorization.amap_rating:
        parts.append(f"评分{authorization.amap_rating}")
    if authorization.amap_avg_price:
        parts.append(f"人均约{authorization.amap_avg_price}元")
    return "，".join(parts)


def _remove_source_attribution(content: str) -> str:
    cleaned = FOOD_SOURCE_ATTRIBUTION_RE.sub("", content)
    cleaned = re.sub(r"^[，,：:\s]+", "", cleaned)
    cleaned = re.sub(r"[，,]{2,}", "，", cleaned)
    return cleaned


def repair_food_plan_text(
    plan_text: str,
    attachment_auth_map: FoodAttachmentAuthMap | None,
    violations: list[FoodRuleViolation],
) -> str:
    """Repair repairable Food Review findings without touching marker identity."""

    if not attachment_auth_map or not violations:
        return plan_text
    tier_keys = {
        violation.key
        for violation in violations
        if violation.reason == "food_tier_exceeded"
    }
    source_keys = {
        violation.key
        for violation in violations
        if violation.reason == "food_source_attribution"
    }

    def replace(match: re.Match[str]) -> str:
        key: FoodAttachmentKey = (
            int(match.group("plan_index")),
            int(match.group("day")),
            int(match.group("anchor_place_id")),
            match.group("meal_slot"),
            int(match.group("food_place_id")),
        )
        content = match.group("content")
        authorization = attachment_auth_map.get(key)
        if authorization is None:
            return match.group(0)
        if key in tier_keys:
            content = _structural_food_content(authorization)
        elif key in source_keys:
            content = _remove_source_attribution(content)
        relative_start = match.start("content") - match.start()
        relative_end = match.end("content") - match.start()
        whole = match.group(0)
        return whole[:relative_start] + content + whole[relative_end:]

    return FOOD_SPAN_RE.sub(replace, plan_text or "")
