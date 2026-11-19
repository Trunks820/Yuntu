"""Deterministic publish-quality gate for user-facing itineraries."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from src.agents.activity_vocabulary import _ACTIVITY_ACTION_MARKERS, _MEAL_ACTIVITY_MARKERS
from src.agents.composition_blueprint import commute_style
from src.agents.accommodation_review import (
    check_accommodation_violations,
    mask_accommodation_prefix_for_route_scan,
)
from src.agents.fact_expression_taxonomy import (
    untrusted_transport_claim_matches,
)
from src.agents.food_review import (
    FoodAttachmentAuthMap,
    authorized_food_names_for_plan,
    find_food_rule_violations,
    mask_authorized_food_names_in_spans,
    strip_food_span_markers,
)
from src.agents.poi_alias import build_route_name_policy, mask_plan_place_references
from src.agents.route_planning import (
    _is_day_heading_line,
    _parse_day_heading_number,
    extract_day_ordered_place_ids,
    format_transit_summary,
    mask_authorized_commute_spans,
)
from src.agents.schema import (
    AccommodationSuggestion,
    PlanOutput,
    RetrievalResult,
    RoutePlan,
    TripRequest,
)
from src.agents.text_quality import (
    BANNED_DATABASE_PHRASES,
    PLACEHOLDER_PHRASES,
    extract_claimed_names,
    find_phrase_spans,
    rare_cjk_characters,
)


INTERNAL_TERMS = (
    "Publish Gate",
    "Structural Gate",
    "route lock",
    "内部校验",
    "[INTERNAL",
    "写作指引，勿输出",
)

PUBLISH_FAILURE_FALLBACK_MESSAGE = (
    "这次攻略生成时质量校验没有通过，我不想把可能有错误路线或残缺内容的攻略发给你。"
    "你可以换个说法再试一次，比如补充城市、天数、偏好或想去的区域。"
)

TRANSIT_FINDING_REASONS = {
    "transit_detail_in_prose",
    "transit_summary_altered",
    "transit_line_not_allowed",
    "transit_stop_not_allowed",
    "transit_direction_invented",
}

# Forward-only recovery: keep these findings fully observable without turning
# soft prose defects into whole-plan Retry/fail-closed. Activity remains shadow
# until F3 acceptance; database tone is cleaned deterministically when exact
# phrases are known and any residual stays report-only.
SHADOW_FINDING_REASONS = frozenset({
    "activity_content_missing",
    "database_tone",
})

_TRANSIT_MARKERS = ("公共交通", "公交", "轨道", "地铁", "换乘", "上车", "下车")
_TRANSIT_LINE_RE = re.compile(
    r"(?:乘坐|搭乘|换乘|乘|坐|公交|轨道交通|地铁)\s*"
    r"([^，。；：:\s→（）]{1,24}(?:号线|路|线))"
)
_TRANSIT_STOP_RE = re.compile(r"([^，。；：:\s→（）]{1,24})(?:上车|下车)")
_TRANSIT_CLAUSE_SPLIT_RE = re.compile(r"([，,。！？!?；;])")
_TRANSIT_LEAD_IN_ONLY_RE = re.compile(
    r"^(?:需要注意的是|需要注意|值得注意的是|不过|但|另外|此外|"
    r"同时|所以|因此|然后|随后|接着|之后)$"
)
_TRANSIT_SIGNAL_RE = re.compile(
    r"(?:随后|然后|再|接着|之后)?"
    r"(?:乘坐|搭乘|换乘|乘|坐)?"
    r"(?:公共交通|公交|轨道交通|轨道|地铁|换乘|上车|下车)"
)
_UNTRUSTED_TRANSPORT_SIGNAL_RE = re.compile(
    r"建议(?:打车|坐车|乘车|乘坐|步行|地铁|公交|自驾|开车)|"
    r"打车前往|坐车前往|乘车前往|"
    r"步行几分钟(?:就能|可)到|"
    r"打车或坐车|打车或乘车"
)
_GENERIC_ACTIVITY_PHRASES = (
    "按自己的节奏在这里看看走走",
    "按自己的节奏看看走走",
    "看看走走",
    "拍照停留",
    "停留拍照",
    "短暂停留",
    "周边走走",
    "走走看看",
    "简单看看",
    "逛一逛",
    "转一圈",
    "记录一下这个位置的样子",
    "记录一下位置和街景",
    "再继续后面的安排",
    "继续后面的安排",
    "本日按锁定顺序到",
    "本日按锁定路线依次游览",
    "游览或步行参观",
    "简短游览或步行经过",
    "作为当天主活动",
)
_MEAL_ROLES = {"meal_stop", "snack_stop", "coffee_stop", "cafe_stop"}


def _is_transit_bearing_line(line: str) -> bool:
    return (
        any(marker in line for marker in _TRANSIT_MARKERS)
        or _TRANSIT_LINE_RE.search(line) is not None
        or _TRANSIT_STOP_RE.search(line) is not None
        or _UNTRUSTED_TRANSPORT_SIGNAL_RE.search(line) is not None
        or bool(untrusted_transport_claim_matches(line))
    )


def _canonical_transit_by_day(route_plan: RoutePlan) -> dict[int, list[str]]:
    result: dict[int, list[str]] = {}
    for day_group in route_plan.day_groups:
        summaries = [
            format_transit_summary(
                int(leg.duration_minutes or 0),
                leg.transit_steps,
                detail_quality=leg.transit_detail_quality,
            )
            for leg in day_group.commute_legs
            if leg.mode == "transit"
        ]
        if summaries:
            result[day_group.day] = summaries
    return result


def _transit_transitions_by_day(route_plan: RoutePlan) -> dict[int, list[str]]:
    result: dict[int, list[str]] = {}
    for day_group in route_plan.day_groups:
        transitions = [
            f"{leg.from_name}→{leg.to_name}，公共交通约 "
            f"{int(leg.duration_minutes or 0)} 分钟"
            for leg in day_group.commute_legs
            if leg.mode == "transit"
        ]
        if transitions:
            result[day_group.day] = transitions
    return result


def _required_transit_transitions_by_day(
    route_plan: RoutePlan,
) -> dict[int, list[str]]:
    """Return only long/remote transit copy that prose must mention."""
    result: dict[int, list[str]] = {}
    for day_group in route_plan.day_groups:
        transitions = [
            f"{leg.from_name}→{leg.to_name}，公共交通约 "
            f"{int(leg.duration_minutes or 0)} 分钟"
            for leg in day_group.commute_legs
            if (
                leg.mode == "transit"
                and commute_style(
                    int(leg.duration_minutes or 0),
                    leg.mode,
                ) in {"long_transfer", "remote_transfer"}
            )
        ]
        if transitions:
            result[day_group.day] = transitions
    return result


def _matching_commute_leg(text: str, commute_legs: list[Any]) -> Any | None:
    matches = [
        leg
        for leg in commute_legs
        if leg.to_name and leg.to_name in text
    ]
    if not matches:
        return None
    return max(matches, key=lambda leg: len(leg.to_name))


def _sanitize_transit_clause(clause: str, commute_legs: list[Any]) -> str:
    """Remove only the transport claim while retaining nearby activity copy."""
    if not _is_transit_bearing_line(clause):
        return clause

    signal_matches = [
        match
        for pattern in (
            _TRANSIT_SIGNAL_RE,
            _UNTRUSTED_TRANSPORT_SIGNAL_RE,
        )
        if (match := pattern.search(clause)) is not None
    ]
    signal_matches.extend(untrusted_transport_claim_matches(clause))
    signal_start = min(
        (match.start() for match in signal_matches),
        default=len(clause),
    )
    for marker in _TRANSIT_MARKERS:
        marker_start = clause.find(marker)
        if marker_start >= 0:
            signal_start = min(signal_start, marker_start)
    for pattern in (_TRANSIT_LINE_RE, _TRANSIT_STOP_RE):
        pattern_match = pattern.search(clause)
        if pattern_match is not None:
            signal_start = min(signal_start, pattern_match.start())

    prefix = clause[:signal_start].strip()
    if (
        prefix.startswith(("通勤参考", "通勤：", "通勤:"))
        or "→" in prefix
    ):
        prefix = ""
    if not any(marker in prefix for marker in _ACTIVITY_ACTION_MARKERS):
        prefix = ""

    leg = _matching_commute_leg(clause, commute_legs)
    neutral_transition = ""
    if leg is not None:
        destination_start = clause.rfind(leg.to_name)
        suffix = clause[destination_start + len(leg.to_name):].strip()
        if _is_transit_bearing_line(suffix):
            suffix = ""
        neutral_transition = f"再到{leg.to_name}{suffix}"

    return "，".join(
        part for part in (prefix, neutral_transition) if part
    )


def _sanitize_transit_line(line: str, commute_legs: list[Any]) -> str:
    """Clause-level cleanup so a mixed activity paragraph is not discarded."""
    if not _is_transit_bearing_line(line):
        return line
    if _article_prompt() and not line.lstrip().startswith(("通勤参考：", "通勤参考:")):
        # v0.15.1 article: its transit sentences were checked against the legs when it was
        # split; cutting from the first transit word would also cut the "站名：" label.
        return line
    if line.lstrip().startswith(("通勤参考：", "通勤参考:")):
        return ""

    normalized = line
    for leg in commute_legs:
        arrow = re.compile(
            rf"(?:通勤参考[:：]\s*)?"
            rf"{re.escape(leg.from_name)}\s*→\s*{re.escape(leg.to_name)}"
        )
        normalized = arrow.sub(f"再到{leg.to_name}", normalized)
    normalized = re.sub(r"^\s*通勤参考[:：]\s*", "", normalized)

    tokens = _TRANSIT_CLAUSE_SPLIT_RE.split(normalized)
    kept: list[str] = []
    for index in range(0, len(tokens), 2):
        clause = tokens[index]
        delimiter = tokens[index + 1] if index + 1 < len(tokens) else ""
        cleaned = _sanitize_transit_clause(clause, commute_legs).strip()
        if _TRANSIT_LEAD_IN_ONLY_RE.fullmatch(
            cleaned.strip(" \t\r\n，,。！？!?；;：:")
        ):
            continue
        if cleaned:
            kept.append(cleaned + delimiter)

    result = "".join(kept).strip()
    result = re.sub(r"([，,；;]){2,}", "，", result)
    result = result.rstrip("，,；; ")
    if (
        result
        and line.rstrip().endswith(("。", "！", "？", "!", "?"))
        and not result.endswith(("。", "！", "？", "!", "?"))
    ):
        result += line.rstrip()[-1]
    return result


def _mask_grounded_transit_summaries(text: str, route_plan: RoutePlan) -> str:
    """Hide only exact canonical transit copy from generic POI-name scanning."""
    masked = text
    for summaries in _canonical_transit_by_day(route_plan).values():
        for summary in summaries:
            masked = masked.replace(summary, " " * len(summary))
    return masked


def _mask_locked_place_names(text: str, day_group: Any) -> str:
    """Exclude canonical POI names from transport-claim signal detection."""
    masked = text
    place_names = sorted(
        {
            str(place.name or "").strip()
            for place in day_group.places
            if str(place.name or "").strip()
        },
        key=len,
        reverse=True,
    )
    for place_name in place_names:
        masked = masked.replace(place_name, " " * len(place_name))
    return masked


def _validate_transit_grounding(
    plans: list[PlanOutput],
    route_plans: list[RoutePlan],
) -> list[PublishFinding]:
    findings: list[PublishFinding] = []
    for plan_index, plan in enumerate(plans, 1):
        if plan_index > len(route_plans):
            continue
        route_plan = route_plans[plan_index - 1]
        detailed_by_day = _canonical_transit_by_day(route_plan)
        transitions_by_day = _transit_transitions_by_day(route_plan)
        sections = _day_sections(plan.plan_text)
        for day_group in route_plan.day_groups:
            day = day_group.day
            detailed_summaries = detailed_by_day.get(day, [])
            transitions = transitions_by_day.get(day, [])
            section = sections.get(day, "")
            body_lines = section.splitlines()[1:]
            if not section or not any(
                _is_transit_bearing_line(
                    _mask_locked_place_names(line, day_group)
                )
                for line in body_lines
            ):
                continue
            for line in body_lines:
                scan_line = _mask_locked_place_names(line, day_group)
                if not _is_transit_bearing_line(scan_line):
                    continue
                if any(summary in line for summary in detailed_summaries):
                    findings.append(PublishFinding(
                        reason="transit_detail_in_prose",
                        message="detailed public-transit line/stop summary belongs only in structured result fields",
                        plan_index=plan_index,
                        day=day,
                        snippet=line[:120],
                    ))
                matched = [value for value in transitions if value in line]
                remainder = line
                for value in matched:
                    remainder = remainder.replace(value, "")
                scan_remainder = _mask_locked_place_names(
                    remainder,
                    day_group,
                )
                if not matched or _is_transit_bearing_line(scan_remainder):
                    findings.append(PublishFinding(
                        reason="transit_summary_altered",
                        message="every prose public-transit claim must contain one exact short deterministic transition",
                        plan_index=plan_index,
                        day=day,
                        snippet=line[:120],
                    ))
                for match in _TRANSIT_LINE_RE.finditer(scan_remainder):
                    findings.append(PublishFinding(
                        reason="transit_line_not_allowed",
                        message=f"public-transit line detail is not allowed in prose: {match.group(1)}",
                        plan_index=plan_index,
                        day=day,
                        snippet=line[:120],
                    ))
                for match in _TRANSIT_STOP_RE.finditer(scan_remainder):
                    findings.append(PublishFinding(
                        reason="transit_stop_not_allowed",
                        message=f"public-transit stop detail is not allowed in prose: {match.group(1)}",
                        plan_index=plan_index,
                        day=day,
                        snippet=line[:120],
                    ))
                if re.search(r"(?:开往|驶向|朝向|方向)", scan_remainder):
                    findings.append(PublishFinding(
                        reason="transit_direction_invented",
                        message="public-transit direction/headsign was not provided",
                        plan_index=plan_index,
                        day=day,
                        snippet=line[:120],
                    ))
    return findings


def replace_unsafe_transit_copy(
    plan: PlanOutput,
    route_plan: RoutePlan,
) -> PlanOutput:
    """Sanitize transit clauses and inject only required long-transfer copy."""
    transitions_by_day = _required_transit_transitions_by_day(route_plan)
    text = plan.plan_text or ""
    valid_matches = _valid_day_heading_matches(text)
    if not valid_matches:
        transitions = [
            value for values in transitions_by_day.values() for value in values
        ]
        commute_legs = [
            leg
            for day_group in route_plan.day_groups
            for leg in day_group.commute_legs
        ]
        kept = [
            cleaned
            for line in text.splitlines()
            if (cleaned := _sanitize_transit_line(line, commute_legs))
        ]
        kept.extend(
            f"通勤参考：{value}。这段路程较远，请预留通勤时间。"
            for value in transitions
        )
        return plan.model_copy(update={"plan_text": "\n".join(kept)})
    parts: list[str] = [text[:valid_matches[0][0].start()]]
    for index, (match, day) in enumerate(valid_matches):
        end = (
            valid_matches[index + 1][0].start()
            if index + 1 < len(valid_matches)
            else len(text)
        )
        section = text[match.start():end]
        transitions = transitions_by_day.get(day, [])
        day_group = next(
            (
                group
                for group in route_plan.day_groups
                if group.day == day
            ),
            None,
        )
        commute_legs = list(day_group.commute_legs) if day_group else []
        section_lines = section.splitlines()
        lines = section_lines[:1] + [
            cleaned
            for line in section_lines[1:]
            if (cleaned := _sanitize_transit_line(line, commute_legs))
        ]
        lines.extend(
            f"通勤参考：{value}。这段路程较远，请预留通勤时间。"
            for value in transitions
            if value not in "\n".join(lines)
        )
        section = "\n".join(lines) + ("\n" if section.endswith("\n") else "")
        parts.append(section)
    return plan.model_copy(update={"plan_text": "".join(parts)})


@dataclass(frozen=True)
class PublishFinding:
    reason: str
    message: str
    plan_index: int | None = None
    day: int | None = None
    snippet: str = ""
    place_id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "message": self.message,
            "plan_index": self.plan_index,
            "day": self.day,
            "place_id": self.place_id,
            "snippet": self.snippet,
        }


@dataclass(frozen=True)
class PublishGateResult:
    passed: bool
    findings: list[PublishFinding] = field(default_factory=list)

    @property
    def blocking_findings(self) -> list[PublishFinding]:
        return [
            finding
            for finding in self.findings
            if finding.reason not in SHADOW_FINDING_REASONS
        ]

    @property
    def shadow_findings(self) -> list[PublishFinding]:
        return [
            finding
            for finding in self.findings
            if finding.reason in SHADOW_FINDING_REASONS
        ]

    @property
    def failure_reasons(self) -> list[str]:
        return list(dict.fromkeys(
            finding.reason for finding in self.blocking_findings
        ))

    @property
    def shadow_reasons(self) -> list[str]:
        return list(dict.fromkeys(
            finding.reason for finding in self.shadow_findings
        ))

    def to_metrics(self) -> dict[str, Any]:
        return {
            "publish_gate_passed": self.passed,
            "publish_failure_reasons": self.failure_reasons,
            "publish_findings": [finding.to_dict() for finding in self.findings],
            "publish_blocking_findings": [
                finding.to_dict() for finding in self.blocking_findings
            ],
            "publish_shadow_reasons": self.shadow_reasons,
            "publish_shadow_findings": [
                finding.to_dict() for finding in self.shadow_findings
            ],
            "activity_content_shadow_enabled": (
                "activity_content_missing" in SHADOW_FINDING_REASONS
            ),
            "database_tone_shadow_enabled": (
                "database_tone" in SHADOW_FINDING_REASONS
            ),
            "duplicate_day_heading_count": sum(
                1 for finding in self.findings
                if finding.reason == "duplicate_day_heading"
            ),
            "placeholder_count": sum(
                1 for finding in self.findings
                if finding.reason == "placeholder_wording"
            ),
            "database_tone_count": sum(
                1 for finding in self.findings
                if finding.reason == "database_tone"
            ),
            "rare_character_compatibility_count": sum(
                1 for finding in self.findings
                if finding.reason == "rare_character_compatibility"
            ),
            "activity_content_missing_count": sum(
                1 for finding in self.findings
                if finding.reason == "activity_content_missing"
            ),
        }


class PublishGateError(RuntimeError):
    """Raised when final travel advice is structurally safe but unpublishable."""

    def __init__(self, result: PublishGateResult):
        self.result = result
        reasons = ", ".join(result.failure_reasons) or "unknown"
        details = "; ".join(
            finding.message[:240]
            for finding in result.findings[:3]
            if finding.message
        )
        suffix = f"; details={details}" if details else ""
        super().__init__(f"Publish Gate failed: {reasons}{suffix}")


def _short_snippet(text: str, start: int, end: int, *, limit: int = 120) -> str:
    left = max(0, start - 40)
    right = min(len(text), end + 40)
    snippet = re.sub(r"\s+", " ", text[left:right]).strip()
    return snippet[:limit]


def _day_heading_numbers(text: str) -> list[tuple[int, int, int]]:
    return [
        (day, match.start(), match.end())
        for match, day in _valid_day_heading_matches(text)
    ]


def _valid_day_heading_matches(
    text: str,
) -> list[tuple[re.Match[str], int]]:
    pattern = re.compile(
        r"(?im)^\s*(?:#{1,6}\s*)?(?:\*\*)?\s*"
        r"(?:Day\s*(\d+)|第\s*(\d+|[一二三四五六七八九十]+)\s*(?:天|日))"
        r"([^\n]*?)(?:\*\*)?\s*$"
    )
    headings: list[tuple[re.Match[str], int]] = []
    for match in pattern.finditer(text):
        day = _parse_day_heading_number(match.group(1) or match.group(2))
        if day is None or not _is_day_heading_line(
            match.group(0),
            match.group(3) or "",
        ):
            continue
        headings.append((match, day))
    return headings


def _validate_plan_text(plan: PlanOutput, plan_index: int) -> list[PublishFinding]:
    findings: list[PublishFinding] = []
    text = plan.plan_text or ""

    seen_days: set[int] = set()
    for day, start, end in _day_heading_numbers(text):
        if day in seen_days:
            findings.append(PublishFinding(
                reason="duplicate_day_heading",
                message=f"duplicate Day {day} heading",
                plan_index=plan_index,
                day=day,
                snippet=_short_snippet(text, start, end),
            ))
        seen_days.add(day)

    for phrase in PLACEHOLDER_PHRASES:
        for match in re.finditer(re.escape(phrase), text):
            findings.append(PublishFinding(
                reason="placeholder_wording",
                message=f"placeholder wording found: {phrase}",
                plan_index=plan_index,
                snippet=_short_snippet(text, match.start(), match.end()),
            ))

    for phrase in BANNED_DATABASE_PHRASES:
        for match in re.finditer(re.escape(phrase), text):
            findings.append(PublishFinding(
                reason="database_tone",
                message=f"database/source wording found: {phrase}",
                plan_index=plan_index,
                snippet=_short_snippet(text, match.start(), match.end()),
            ))

    rare_char_surfaces = [
        ("plan_text", text),
        ("plan_name", plan.plan_name or ""),
        ("used_place_names", " ".join(plan.used_place_names or [])),
        (
            "day_place_names",
            " ".join(name for day_names in plan.day_place_names for name in day_names),
        ),
    ]
    for surface, value in rare_char_surfaces:
        for char in rare_cjk_characters(value):
            start = value.find(char)
            findings.append(PublishFinding(
                reason="rare_character_compatibility",
                message=(
                    f"rare CJK character in {surface} may not render safely: "
                    f"U+{ord(char):04X}"
                ),
                plan_index=plan_index,
                snippet=_short_snippet(value, start, start + len(char))
                if start >= 0 else char,
            ))

    if len(text.strip()) < 80:
        findings.append(PublishFinding(
            reason="too_short_plan_text",
            message="plan text is too short to publish",
            plan_index=plan_index,
            snippet=text.strip()[:120],
        ))

    return findings


def _validate_city(
    trip_request: TripRequest,
    retrieval: RetrievalResult,
) -> list[PublishFinding]:
    request_city = trip_request.to_city.strip()
    retrieval_city = retrieval.city.strip()
    if request_city and retrieval_city and request_city != retrieval_city:
        return [PublishFinding(
            reason="city_mismatch",
            message=(
                f"trip request city {request_city} differs from "
                f"retrieval city {retrieval_city}"
            ),
        )]
    return []


def _day_sections(plan_text: str) -> dict[int, str]:
    matches = _valid_day_heading_matches(plan_text)
    sections: dict[int, str] = {}
    for index, (match, day) in enumerate(matches):
        end = (
            matches[index + 1][0].start()
            if index + 1 < len(matches)
            else len(plan_text)
        )
        sections[day] = plan_text[match.start():end]
    return sections


def _day_bodies(plan_text: str) -> dict[int, str]:
    matches = _valid_day_heading_matches(plan_text)
    bodies: dict[int, str] = {}
    for index, (match, day) in enumerate(matches):
        end = (
            matches[index + 1][0].start()
            if index + 1 < len(matches)
            else len(plan_text)
        )
        bodies[day] = plan_text[match.end():end]
    return bodies


def _activity_role_by_place_id(plan: PlanOutput) -> dict[int, str]:
    blueprint = plan.composition_blueprint
    if blueprint is None:
        return {}
    return {
        stop.place_id: stop.role
        for day in blueprint.days
        for stop in day.stops
    }


def _activity_clauses(text: str) -> list[str]:
    return [
        clause.strip()
        for clause in re.split(r"[，,。！？!?；;\n]+", text or "")
        if clause.strip()
    ]


def _meaningful_activity_text(
    text: str,
    *,
    role: str,
    place_type: str,
    locked_names: list[str],
    place_name: str = "",
) -> bool:
    from src.agents.explicit_preference_fulfillment import current_activity_body, has_executed_activity

    meal = role in _MEAL_ROLES or place_type in {"restaurant", "cafe", "snack"}
    cleaned, _ = current_activity_body(text or "", place_name=place_name,
        other_place_names=[name for name in locked_names if name != place_name],
        dimension="food" if meal else "activity")
    if not has_executed_activity(cleaned):
        return False
    for name in sorted(locked_names, key=len, reverse=True):
        if name:
            cleaned = cleaned.replace(name, "")
    for phrase in _GENERIC_ACTIVITY_PHRASES:
        cleaned = cleaned.replace(phrase, "")
    cleaned = re.sub(r"作为.{0,16}(?:节点|收尾|停留点)", "", cleaned)
    cleaned = re.sub(
        r"(?:驾车|车程|公共交通|公交|地铁|骑行|转场|通勤参考)"
        r"[^。；\n]{0,40}?\d+\s*分钟",
        "",
        cleaned,
    )
    compact = re.sub(r"\s+", "", cleaned)
    cjk_length = len(re.findall(r"[\u3400-\u9fff]", compact))

    is_meal = role in _MEAL_ROLES or place_type in {
        "restaurant",
        "cafe",
        "snack",
    }
    if is_meal:
        return (
            any(marker in compact for marker in _MEAL_ACTIVITY_MARKERS)
            and cjk_length >= 3
        )
    return (
        has_executed_activity(compact)
        and cjk_length >= 6
    )


def _validate_activity_coverage(
    plans: list[PlanOutput],
    route_plans: list[RoutePlan],
    *, body_semantics_by_review: bool = False,
) -> list[PublishFinding]:
    """Require useful per-stop actions, not headings or commute-only mentions."""
    findings: list[PublishFinding] = []
    for plan_index, plan in enumerate(plans, 1):
        if plan_index > len(route_plans):
            continue
        route_plan = route_plans[plan_index - 1]
        bodies = _day_bodies(plan.plan_text or "")
        role_by_place_id = _activity_role_by_place_id(plan)

        locked_names = [place.name for day in route_plan.day_groups for place in day.places if place.name]
        for day_group in route_plan.day_groups:
            if plan.poi_fragments:
                for place in day_group.places:
                    role = role_by_place_id.get(place.place_id, "")
                    if role == "transfer_context":
                        continue
                    fragment = plan.poi_fragment(
                        plan_index=plan_index,
                        day=day_group.day,
                        place_id=place.place_id,
                    )
                    if fragment is not None and (bool(fragment.text.strip()) if body_semantics_by_review else
                            _meaningful_activity_text(fragment.text, role=role, place_type=place.place_type,
                                locked_names=locked_names, place_name=place.name)):
                        continue
                    findings.append(PublishFinding(
                        reason="activity_content_missing",
                        message=(
                            "keyed POI fragment lacks a concrete activity: "
                            f"{place.name}"
                        ),
                        plan_index=plan_index,
                        day=day_group.day,
                        place_id=place.place_id,
                        snippet=(
                            fragment.text[:120]
                            if fragment is not None
                            else place.name
                        ),
                    ))
                continue

            body = bodies.get(day_group.day, "")
            masked_body = mask_authorized_commute_spans(
                body,
                route_plan,
                day=day_group.day,
            )
            clauses = _activity_clauses(masked_body)
            clause_ids = [
                list(
                    extract_day_ordered_place_ids(
                        clause,
                        day_group.places,
                        route_plan=route_plan,
                        identity_result=plan.poi_identity_result,
                        day=day_group.day,
                    ).get("ordered_place_ids")
                    or []
                )
                for clause in clauses
            ]
            for place in day_group.places:
                role = role_by_place_id.get(place.place_id, "")
                if role == "transfer_context":
                    continue
                target_id = (
                    place.canonical_place_id
                    if place.canonical_place_id is not None
                    else place.place_id
                )
                windows: list[str] = []
                for clause_index, (clause, ids) in enumerate(
                    zip(clauses, clause_ids)
                ):
                    if target_id not in ids:
                        continue
                    exact_start = clause.find(place.name)
                    local = clause
                    is_last_named_place = target_id == ids[-1]
                    if exact_start >= 0:
                        next_starts = [
                            clause.find(other.name)
                            for other in day_group.places
                            if (
                                other.name
                                and other.name != place.name
                                and clause.find(other.name) > exact_start
                            )
                        ]
                        exact_end = min(next_starts) if next_starts else len(clause)
                        between = (
                            clause[exact_start + len(place.name):exact_end]
                            if next_starts
                            else ""
                        )
                        collective = between.strip() in {"和", "与", "、", "及"}
                        local = clause[
                            max(0, exact_start - 8):
                            (len(clause) if collective else exact_end)
                        ]
                        is_last_named_place = not next_starts

                    context = [local]
                    if is_last_named_place:
                        for offset in (1, 2):
                            next_index = clause_index + offset
                            if next_index >= len(clauses) or clause_ids[next_index]:
                                break
                            context.append(clauses[next_index])
                    windows.append("，".join(context))

                if any(
                    _meaningful_activity_text(
                        window,
                        role=role,
                        place_type=place.place_type,
                        locked_names=locked_names,
                        place_name=place.name,
                    )
                    for window in windows
                ):
                    continue
                findings.append(PublishFinding(
                    reason="activity_content_missing",
                    message=(
                        f"locked POI lacks a concrete activity beyond route/"
                        f"commute wording: {place.name}"
                    ),
                    plan_index=plan_index,
                    day=day_group.day,
                    place_id=place.place_id,
                    snippet=(windows[0] if windows else place.name)[:120],
                ))
    return findings


def collect_activity_coverage_findings(
    plans: list[PlanOutput],
    route_plans: list[RoutePlan],
    *, body_semantics_by_review: bool = False,
) -> list[PublishFinding]:
    """Expose stable-key Activity findings to deterministic local completion."""
    return _validate_activity_coverage(plans, route_plans, body_semantics_by_review=body_semantics_by_review)


def _day_heading_text(plan_text: str) -> str:
    return "\n".join(
        match.group(0)
        for match, _day in _valid_day_heading_matches(plan_text or "")
    )


def _article_prompt() -> bool:
    from src.config import get_settings
    return str(getattr(get_settings(), "writer_prompt_version", "") or "").strip() == "article"


def _article_skeleton_plans(plans: list[PlanOutput], route_plans: list[RoutePlan]) -> list[PlanOutput]:
    from src.agents.article_writer import article_skeleton
    return [
        plan.model_copy(update={"plan_text": article_skeleton(plan.plan_text, route_plans[index])})
        if index < len(route_plans) else plan
        for index, plan in enumerate(plans)
    ]


def _validate_route_membership(
    plans: list[PlanOutput],
    retrieval: RetrievalResult,
    route_plans: list[RoutePlan],
    attachment_auth_map: FoodAttachmentAuthMap | None = None,
    accommodation: AccommodationSuggestion | None = None,
) -> list[PublishFinding]:
    findings: list[PublishFinding] = []
    candidate_names = [candidate.name for candidate in retrieval.candidates]
    article = _article_prompt()
    for index, plan in enumerate(plans):
        if index >= len(route_plans):
            continue
        route_plan = route_plans[index]
        if article:
            # v0.15.1 D3: places named in the article are not visits; the
            # backend's titles and stop labels carry the route.
            from src.agents.article_writer import article_skeleton
            plan = plan.model_copy(update={"plan_text": article_skeleton(plan.plan_text, route_plan)})
        reference_text = mask_plan_place_references(
            plan, route_plan=route_plan, plan_index=index + 1,
            candidate_names=candidate_names,
        )
        grounded_text = _mask_grounded_transit_summaries(
            reference_text,
            route_plan,
        )
        if accommodation is not None or plan.transport is not None:
            grounded_text = mask_accommodation_prefix_for_route_scan(
                grounded_text,
                accommodation=accommodation,
                code_lodging_section=getattr(plan, "code_lodging_section", "") or "",
            )
        locked_by_day = {
            day_group.day: {place.name for place in day_group.places}
            for day_group in route_plan.day_groups
        }
        locked_places = [
            place
            for day_group in route_plan.day_groups
            for place in day_group.places
        ]
        locked_names = {place.name for place in locked_places}
        policy = build_route_name_policy(
            locked_places=locked_places,
            candidate_names=candidate_names,
            identity_result=plan.poi_identity_result,
            city_name=retrieval.city,
        )
        route_names = policy.route_names
        food_names = authorized_food_names_for_plan(
            attachment_auth_map,
            plan_index=index,
        )
        extraction_names = [*candidate_names, *route_names, *food_names]
        membership_text = mask_authorized_food_names_in_spans(
            grounded_text,
            attachment_auth_map,
            plan_index=index,
        )
        text_names = set(extract_claimed_names(membership_text, extraction_names))
        # An attachment may resolve to a restaurant that is also a locked route
        # stop. Its normal Day-heading/body occurrences remain route-authorized;
        # only route-external attachment names need the food-span exemption.
        forbidden_text_names = (
            policy.forbidden_text_names
            | (food_names - route_names)
        )

        for name in sorted(text_names & forbidden_text_names):
            start = grounded_text.find(name)
            findings.append(PublishFinding(
                reason="route_outside_poi",
                message=f"route-outside POI found in plan text: {name}",
                plan_index=index + 1,
                snippet=_short_snippet(plan.plan_text, start, start + len(name))
                if start >= 0 else "",
            ))

        plan_name_names = set(extract_claimed_names(
            plan.plan_name,
            extraction_names,
        ))
        for name in sorted(plan_name_names - route_names):
            start = plan.plan_name.find(name)
            findings.append(PublishFinding(
                reason="route_outside_poi",
                message=f"route-outside POI found in plan_name: {name}",
                plan_index=index + 1,
                snippet=_short_snippet(plan.plan_name, start, start + len(name))
                if start >= 0 else name,
            ))

        heading_names = set(extract_claimed_names(
            _day_heading_text(grounded_text),
            extraction_names,
        ))
        for name in sorted(heading_names - route_names):
            start = grounded_text.find(name)
            findings.append(PublishFinding(
                reason="route_outside_poi",
                message=f"route-outside POI found in Day heading: {name}",
                plan_index=index + 1,
                snippet=_short_snippet(plan.plan_text, start, start + len(name))
                if start >= 0 else name,
            ))

        for name in sorted(set(plan.used_place_names) - route_names):
            findings.append(PublishFinding(
                reason="route_outside_poi",
                message=f"route-outside POI found in used_place_names: {name}",
                plan_index=index + 1,
                snippet=name,
            ))

        day_place_names = {
            name
            for day_names in plan.day_place_names
            for name in day_names
        }
        for name in sorted(day_place_names - route_names):
            findings.append(PublishFinding(
                reason="route_outside_poi",
                message=f"route-outside POI found in day_place_names: {name}",
                plan_index=index + 1,
                snippet=name,
            ))

        sections = _day_sections(grounded_text)
        for day, section in sections.items():
            allowed = locked_by_day.get(day, set())
            if not allowed:
                continue
            # Today's stops, including planner meal stops, are often absent from
            # retrieval.candidates. The longest-name scan can only hide a shorter
            # other-day POI when the current stop name itself is in this list.
            section_names = set(extract_claimed_names(
                section,
                [*candidate_names, *(name for name in allowed if name)],
            ))
            for name in sorted(section_names & locked_names):
                if name in allowed:
                    continue
                start = section.find(name)
                if start < 0:
                    continue
                absolute_start = grounded_text.find(section) + start
                findings.append(PublishFinding(
                    reason="cross_day_poi",
                    message=f"POI appears in wrong day section: {name}",
                    plan_index=index + 1,
                    day=day,
                    snippet=_short_snippet(
                        plan.plan_text,
                        absolute_start,
                        absolute_start + len(name),
                    ),
                ))
    return findings


def _validate_food_authorization(
    plans: list[PlanOutput],
    attachment_auth_map: FoodAttachmentAuthMap | None,
) -> list[PublishFinding]:
    if not attachment_auth_map:
        return []
    findings: list[PublishFinding] = []
    for zero_index, plan in enumerate(plans):
        for violation in find_food_rule_violations(
            plan.plan_text,
            attachment_auth_map,
            plan_index=zero_index,
        ):
            findings.append(PublishFinding(
                reason=violation.reason,
                message=(
                    "none-tier food prose differs from authorized none_tier_text"
                    if violation.reason == "food_none_tier_violation"
                    else "food prose exceeds deterministic tier/source authorization"
                ),
                plan_index=zero_index + 1,
                day=violation.key[1],
                place_id=violation.key[2],
                snippet=violation.snippet[:200],
            ))
    return findings


def strip_food_markers_from_plans(
    plans: list[PlanOutput],
) -> list[PlanOutput]:
    """Remove internal food markers after the final Gate has passed."""

    return [
        plan.model_copy(update={
            "plan_text": strip_food_span_markers(plan.plan_text),
        })
        for plan in plans
    ]


def _validate_arrival_durations(plans, route_plans):
    from src.agents.arrival_copy import normalize_arrival_copy
    findings = []
    for index, (plan, route) in enumerate(zip(plans, route_plans), 1):
        if route.route_policy_version != "selector-route-v2":
            continue
        bodies = _day_bodies(plan.plan_text or "")
        for day in route.day_groups:
            for leg in day.commute_legs:
                # Read final rendered text, not possibly stale fragment offsets.
                for match in re.finditer(rf"(?m)^{re.escape(leg.to_name)}[:：]([^\n]*)", bodies.get(day.day, "")):
                    body = match.group(1)
                    if normalize_arrival_copy(body, leg) != body:
                        findings.append(PublishFinding(reason="arrival_duration_ungrounded",
                            message="arrival-time copy must use the locked incoming route",
                            plan_index=index, day=day.day, place_id=leg.to_place_id))
    return findings


def check_publish_gate(
    plans: list[PlanOutput],
    *,
    trip_request: TripRequest,
    retrieval: RetrievalResult,
    route_plans: list[RoutePlan] | None = None,
    attachment_auth_map: FoodAttachmentAuthMap | None = None,
    accommodation: AccommodationSuggestion | None = None,
    body_semantics_by_review: bool = False,
) -> PublishGateResult:
    """Return deterministic publish-quality findings for final plans."""
    findings: list[PublishFinding] = []
    findings.extend(_validate_city(trip_request, retrieval))
    expected_count = len(route_plans or [])
    if expected_count and len(plans) != expected_count:
        findings.append(PublishFinding(
            reason="plan_count_mismatch",
            message=f"expected {expected_count} plans, got {len(plans)}",
        ))
    if route_plans:
        findings.extend(_validate_route_membership(
            plans,
            retrieval,
            route_plans,
            attachment_auth_map=attachment_auth_map,
            accommodation=accommodation,
        ))
        # v0.15.1 article: its transit sentences were checked against the legs when it
        # was split (article_writer); the backend's own lines are what is checked here.
        findings.extend(_validate_transit_grounding(
            _article_skeleton_plans(plans, route_plans) if _article_prompt() else plans,
            route_plans,
        ))
        findings.extend(_validate_arrival_durations(plans, route_plans))
        findings.extend(collect_activity_coverage_findings(plans, route_plans, body_semantics_by_review=body_semantics_by_review))
    findings.extend(_validate_food_authorization(plans, attachment_auth_map))
    for index, plan in enumerate(plans, 1):
        findings.extend(_validate_plan_text(plan, index))
        plan_routes = (
            [route_plans[index - 1]]
            if route_plans and index - 1 < len(route_plans)
            else None
        )
        findings.extend(check_accommodation_violations(
            plan,
            accommodation,
            retrieval=retrieval,
            route_plans=plan_routes,
        ))
    blocking_findings = [
        finding
        for finding in findings
        if finding.reason not in SHADOW_FINDING_REASONS
    ]
    return PublishGateResult(passed=not blocking_findings, findings=findings)


def validate_polished_failure_message(message: str) -> str | None:
    """Return safe polished copy, or None when deterministic fallback is required."""
    cleaned = re.sub(r"\s+", " ", (message or "")).strip()
    if not cleaned:
        return None
    if any(term in cleaned for term in INTERNAL_TERMS):
        return None
    if len(cleaned) > 240:
        cleaned = cleaned[:240].rstrip()
    return cleaned
