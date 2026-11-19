"""Deterministic accommodation checks for the publish gate."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from src.agents.evidence_strength import build_structured_evidence_payload
from src.agents.schema import (
    AccommodationSuggestion,
    PlanOutput,
    RetrievalResult,
    RoutePlan,
)

if TYPE_CHECKING:
    from src.agents.publish_gate import PublishFinding


HOTEL_BRANDS = {
    "希尔顿",
    "香格里拉",
    "洲际",
    "万豪",
    "凯悦",
    "君悦",
    "悦榕庄",
    "四季",
    "丽思卡尔顿",
    "索菲特",
    "喜来登",
    "威斯汀",
    "康莱德",
    "瑞吉",
    "柏悦",
    "安缦",
    "艾迪逊",
    "如家",
    "汉庭",
    "7天",
    "锦江之星",
    "全季",
    "亚朵",
    "维也纳",
    "格林豪泰",
    "桔子",
    "布丁",
    "速8",
    "华住",
    "尚客优",
    "城市便捷",
}

ACCOMMODATION_CONTEXT = ("酒店", "入住", "住宿", "下榻", "住在", "房")

# "酒店" stays a context word and is intentionally absent here. A brand's own
# "酒店" must not count as wanting to stay there.
LODGING_INTENT_TERMS = (
    "入住",
    "住宿",
    "下榻",
    "住在",
    "住一晚",
    "住这里",
    "推荐住",
    "房价",
    "房型",
    "客房",
    "订房",
)

# Immediate suffix after the brand. Evidence must contain the whole mention
# ("桔子酒店"), not the brand alone ("桔子"). Longest first.
LODGING_BRAND_SUFFIXES = (
    "国际酒店",
    "商务酒店",
    "连锁酒店",
    "度假酒店",
    "精品酒店",
    "大酒店",
    "酒店",
    "宾馆",
    "旅馆",
    "旅店",
    "旅社",
    "客栈",
    "民宿",
    "度假村",
)

_SENTENCE_RE = re.compile(r"[^。！？\n]+")
_DAY_HEADING_RE = re.compile(r"(?m)^Day\s+")
_PRICE_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        r"\d+元.{0,6}(?:一晚|每晚|/晚)",
        r"每晚.{0,6}\d+.{0,4}元",
        r"人均[^，,。！？\n]{0,10}住宿",
        r"(?:酒店|住宿|客房).{0,6}房价",
    )
)
# A range such as "每晚约 300–450 元" spans too far for the older nightly
# pattern. Only scanned with the code lodging paragraph on, so the gate is
# unchanged while that flag is off. The code paragraph itself is removed
# before the scan; the same words in the writer body still match.
_RANGE_PRICE_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        r"每晚约?\s*\d+\s*[–\-]\s*\d+\s*元",
        r"参考价（每晚）.{0,40}\d+\s*[–\-]\s*\d+\s*元",
    )
)


def _price_patterns() -> tuple[re.Pattern[str], ...]:
    from src.config import get_settings

    if getattr(get_settings(), "lodging_section_v2_enabled", False):
        return _PRICE_PATTERNS + _RANGE_PRICE_PATTERNS
    return _PRICE_PATTERNS


def _name_spans(sentence: str, names: list[str]) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for name in names:
        cleaned = (name or "").strip()
        if not cleaned:
            continue
        spans.extend(
            match.span()
            for match in re.finditer(re.escape(cleaned), sentence)
        )
    return spans


def _span_is_inside(
    span: tuple[int, int],
    containers: list[tuple[int, int]],
) -> bool:
    start, end = span
    return any(left <= start and end <= right for left, right in containers)


def mask_exact_code_lodging_section(plan_text: str, section: str) -> str:
    """Replace one exact code-written lodging block with spaces.

    A shorter or longer copy is left in place so the scanners still see it.
    Newlines stay newlines, which keeps later offsets stable.
    """

    if not section or not plan_text:
        return plan_text
    start = plan_text.find(section)
    if start < 0:
        return plan_text
    masked = "".join("\n" if character == "\n" else " " for character in section)
    return plan_text[:start] + masked + plan_text[start + len(section):]


def _mask_leading_transport_paragraph(plan_text: str, day_start: int) -> str:
    """Hide the transport paragraph and leave a changed lodging block visible."""

    if not plan_text.startswith("【出行建议】"):
        return plan_text
    end = plan_text.find("\n\n")
    if end < 0 or end + 2 > day_start:
        return plan_text
    head = plan_text[: end + 2]
    masked = "".join("\n" if character == "\n" else " " for character in head)
    return masked + plan_text[end + 2 :]


def mask_accommodation_prefix_for_route_scan(
    plan_text: str,
    *,
    accommodation: AccommodationSuggestion | None = None,
    code_lodging_section: str = "",
) -> str:
    """Mask backend-owned prefixes without changing body or fragment offsets."""

    section = code_lodging_section or ""
    if section:
        if section in plan_text:
            plan_text = mask_exact_code_lodging_section(plan_text, section)
        else:
            # The generated paragraph was edited. Do not hide that edit behind
            # the older whole-prefix mask.
            day_heading = _DAY_HEADING_RE.search(plan_text)
            if day_heading is None:
                return plan_text
            return _mask_leading_transport_paragraph(plan_text, day_heading.start())

    arrangement = plan_text.startswith("【住宿安排】")
    if not arrangement and not plan_text.startswith(("【出行建议】", "【住宿建议】")):
        return plan_text
    day_heading = _DAY_HEADING_RE.search(plan_text)
    if day_heading is None:
        return plan_text
    prefix = plan_text[:day_heading.start()]
    if arrangement:
        if accommodation is None or accommodation.source != "user_specified":
            return plan_text
        name = accommodation.name
        if not name or any(character in name for character in "\r\n【】"):
            return plan_text
        # This is the exact deterministic paragraph assembled by FinalWriter.
        # Matching the whole pre-Day prefix prevents a marker or an authorized
        # lodging name from hiding added sightseeing claims or other paragraphs.
        expected_prefixes = (
            f"【住宿安排】以你指定的{name}为每天出发和返回的{basis}，"
            "往返时间按该位置估算。\n\n"
            for basis in ("住宿位置", "住宿区域")
        )
        if prefix not in expected_prefixes:
            return plan_text
    elif not prefix.endswith("\n\n"):
        return plan_text
    masked_prefix = "".join(
        "\n" if character == "\n" else " "
        for character in prefix
    )
    return masked_prefix + plan_text[day_heading.start():]


def _has_lodging_intent(sentence: str) -> bool:
    return any(term in sentence for term in LODGING_INTENT_TERMS)


def _lodging_brand_mention(sentence: str, brand: str, end: int) -> str | None:
    """Brand plus an immediate hotel suffix, such as 桔子酒店. Not the brand alone."""

    rest = sentence[end:]
    for suffix in LODGING_BRAND_SUFFIXES:
        if rest.startswith(suffix):
            return brand + suffix
    return None


def _authorized_evidence_text(
    used_place_names: list[str],
    retrieval: RetrievalResult | None,
    route_plans: list[RoutePlan] | None,
) -> str:
    """Writer-visible evidence for places this plan actually uses.

    Final Writer quotes the classified direct_facts, weak_experience and
    risk_only_warnings from ``build_structured_evidence_payload``. That payload
    is built from route-stop ``top_reasons`` and ``warnings`` (retrieval already
    prefers non-empty cleaned_evidence, including located excerpts, into
    ``top_reasons``). Omitted and not-authorized reasons are not included.
    """

    used_names = {
        name.strip()
        for name in used_place_names
        if name and name.strip()
    }
    if not used_names or (retrieval is None and not route_plans):
        return ""
    payload = build_structured_evidence_payload(
        retrieval if retrieval is not None else RetrievalResult(
            city="",
            candidates=[],
        ),
        route_plans=route_plans,
    )
    texts: list[str] = []
    for place in payload.places:
        if place.name.strip() not in used_names:
            continue
        texts.extend(place.direct_facts)
        texts.extend(place.weak_experience)
        texts.extend(place.risk_only_warnings)
    return "\n".join(texts)


_TRIP_LENGTH_AFTER_RE = re.compile(r"(?:的|行程|之旅|旅程|旅行|游|玩|假期|时间|内|里|下来|走下来|\d)")


def _fabricated_brand(
    plan_text: str,
    used_place_names: list[str],
    authorized_evidence: str = "",
) -> tuple[str, str] | None:
    for sentence_match in _SENTENCE_RE.finditer(plan_text):
        sentence = sentence_match.group(0)
        if not any(context in sentence for context in ACCOMMODATION_CONTEXT):
            continue
        place_spans = _name_spans(sentence, used_place_names)
        lodging_intent = _has_lodging_intent(sentence)
        for brand in sorted(HOTEL_BRANDS, key=len, reverse=True):
            for brand_match in re.finditer(re.escape(brand), sentence):
                if _span_is_inside(brand_match.span(), place_spans):
                    continue
                if brand == "7天" and _TRIP_LENGTH_AFTER_RE.match(sentence, brand_match.end()):
                    # "这趟7天行程" is the trip's length, not the chain (v0.15.1 replay P20).
                    continue
                mention = _lodging_brand_mention(
                    sentence,
                    brand,
                    brand_match.end(),
                )
                if (
                    mention is not None
                    and not lodging_intent
                    and mention in authorized_evidence
                ):
                    continue
                return brand, sentence.strip()
    return None


def check_accommodation_violations(
    plan: PlanOutput,
    accommodation: AccommodationSuggestion | None,
    *,
    retrieval: RetrievalResult | None = None,
    route_plans: list[RoutePlan] | None = None,
) -> list[PublishFinding]:
    """Return fail-closed accommodation findings with conservative matching."""

    from src.agents.publish_gate import PublishFinding

    if accommodation is None:
        return []

    findings: list[PublishFinding] = []
    used_place_names = list(plan.used_place_names or [])
    scanned_text = mask_exact_code_lodging_section(
        plan.plan_text or "",
        getattr(plan, "code_lodging_section", "") or "",
    )

    if accommodation.name in used_place_names:
        findings.append(PublishFinding(
            reason="accommodation_in_route",
            message=(
                f"accommodation '{accommodation.name}' appears in "
                "used_place_names"
            ),
        ))

    for pattern in _price_patterns():
        match = pattern.search(scanned_text)
        if match:
            findings.append(PublishFinding(
                reason="accommodation_price_claim",
                message="plan text contains an accommodation price claim",
                snippet=match.group(0),
            ))
            break

    if accommodation.source == "auto_recommended":
        fabricated = _fabricated_brand(
            scanned_text,
            used_place_names,
            _authorized_evidence_text(
                used_place_names,
                retrieval,
                route_plans,
            ),
        )
        if fabricated is not None:
            brand, sentence = fabricated
            findings.append(PublishFinding(
                reason="accommodation_fabrication",
                message=(
                    "auto-recommended accommodation text names hotel brand "
                    f"'{brand}'"
                ),
                snippet=sentence,
            ))

    return findings
