"""Deterministic explicit-preference fulfillment for L3 / U2.

Only user-declared scoring dimensions and family arrangement are checked.
Blueprint-inferred day themes never enter this gate. Validation is LLM-free;
authorized keyed POI fragment repair is a separate, budgeted writer call.
"""

from __future__ import annotations

import re
import json
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

from src.agents.activity_vocabulary import ACTIVITY_ACTION_RE
from src.agents.composition_blueprint import (
    _NIGHT_HINT_CLOCK_RE,
    _NIGHT_HINT_NEGATION_RE,
    _night_activity_bound_to_place,
    _night_hint_clauses,
)
from src.agents.evidence_strength import StructuredEvidencePayload
from src.agents.generation_issues import (GenerationIssue,
    body_review_snapshot, review_issue_id)
from src.agents.poi_fragments import fragment_registry_is_intact
from src.agents.keyed_fragment_repair import FragmentKey
from src.agents.preferences import (
    CITYWALK,
    CULTURE,
    FAMILY,
    FOOD,
    NATURE,
    NIGHT_VIEW,
    PHOTO,
    SCORING_DIMENSIONS,
    SHOPPING,
    candidate_matches_dimension,
    route_preference_compromise,
    route_preference_snapshot,
    family_arrangement_active,
    normalize_preferences,
)
from src.agents.schema import (
    CandidatePlace,
    PlanOutput,
    RetrievalResult,
    RouteDayGroup,
    RoutePlan,
    TripRequest,
    parse_hhmm,
)

LAYER_MEMBER = "member"
LAYER_ARRANGEMENT = "arrangement"
LAYER_BODY = "body"

STATUS_SUPPORTED = "supported"
STATUS_MISSING = "missing"
STATUS_UNSUPPORTED = "unsupported"
STATUS_UNKNOWN = "unknown"
STATUS_UNASSESSED = "unassessed"

REASON_MEMBER_MISSING = "explicit_preference_member_missing"
REASON_ARRANGEMENT_UNSUPPORTED = "explicit_preference_arrangement_unsupported"
REASON_BODY_MISSING = "explicit_preference_body_missing"
REASON_NO_FRAGMENT = "explicit_preference_no_fragment"
REASON_NO_ACTION_AUTHORIZATION = "explicit_preference_no_action_authorization"
REASON_NO_TARGET = "explicit_preference_no_target"
REASON_NEGATION = "explicit_preference_negation_not_fulfilled"
REASON_TITLE_ONLY = "explicit_preference_title_only"
REASON_OTHER_PLACE = "explicit_preference_other_place"
REASON_UNKNOWN = "explicit_preference_unknown"
REASON_UNASSESSED = "explicit_preference_unassessed"
REASON_CLASSIC_DAY = "explicit_preference_classic_day_not_fulfillment"
REASON_WARN_ONLY = "explicit_preference_warn_not_fulfillment"

BODY_MISSING_REASONS = frozenset(
    {
        REASON_BODY_MISSING,
        REASON_NEGATION,
        REASON_TITLE_ONLY,
        REASON_OTHER_PLACE,
    }
)
ROUTE_OWNER_REASONS = frozenset(
    {
        REASON_MEMBER_MISSING,
        REASON_ARRANGEMENT_UNSUPPORTED,
        REASON_NO_TARGET,
    }
)
ADMISSION_BLOCK_REASONS = frozenset(
    {
        REASON_NO_FRAGMENT,
        REASON_NO_ACTION_AUTHORIZATION,
        REASON_NO_TARGET,
    }
)

_NIGHT_BODY_PHRASES = ("夜景", "夜游", "夜间灯光", "晚上看", "晚间看", "夜里看")
_NIGHT_ACTION = re.compile(r"看|走|逛|游|拍|停")
_NIGHT_NEGATION = re.compile(
    r"(?:不是|没有|不安排|不要|不可|不能|不会|不|没|无|非|勿|别).{0,8}"
    r"(?:夜景|夜游|灯光|晚上看)"
    r"|(?:夜景|夜游|灯光).{0,6}(?:不安排|不要|不可|不能|不是|没有|不|没|无|并非)"
)
_DAYTIME_LIGHT = re.compile(r"光线平|白天视角|白天沿")
_FAMILY_BODY_PHRASES = ("亲子一起", "带孩子", "带着孩子", "和孩子", "跟孩子", "与孩子",
                        "一家一起", "小朋友一起", "家庭一起", "带娃一起", "带娃")
_FAMILY_ACTION = ACTIVITY_ACTION_RE
_FAMILY_NEGATION = re.compile(r"(?:不适合|不要|没有).{0,6}(?:亲子|带娃|小孩|儿童)")
_FOOD_BODY_PHRASES = ("用餐", "吃饭", "吃午饭", "吃晚饭", "吃早餐", "吃一顿饭", "吃顿饭",
                      "吃点东西", "吃小吃", "吃甜品", "安排午餐", "安排晚餐", "坐下来吃")
_CITYWALK_BODY_PHRASES = ("沿街走", "街区漫步", "步行逛", "沿街逛", "慢慢走",
                         "沿街散步", "沿街步行", "沿着街道走", "沿街慢慢走")
_PHOTO_BODY_PHRASES = ("取景", "找角度", "拍照", "摄影", "拍摄")
_CULTURE_BODY_PHRASES = ("参观", "看展", "观察建筑外观", "看建筑外观", "观看展陈")
_CULTURE_BODY_CONTEXT = re.compile(r"展品(?:说明)?|展览(?:说明)?|展厅|陈列")
_CULTURE_CONTEXT_ACTION = re.compile(
    r"(?:观看|阅读|看|读)(?:一下|一遍|这份|这些)?(?:展品(?:说明)?|展览(?:说明)?|陈列)"
    r"|阅读|看一看|看看|慢慢看|仔细看|逐一看|读一读|读一遍|慢慢读|仔细读|逐一读|顺着读"
)
_CULTURE_OTHER_READING_OBJECT = re.compile(r"菜单|路牌|导航|攻略|天气|小说|报纸|短信|手机")
_NIGHT_BODY_CONTEXT = re.compile(r"晚上|晚间|夜间|夜里|入夜后|天黑后|夜景|夜色|夜游")
_NIGHT_CONTEXT_ACTION = re.compile(
    r"拍(?!卖|板|手|案|打|马屁)"
    r"(?![^，,。；;]{0,8}(?:指南|服务|机位|偏好|主题))"
    r"[^，,。；;]{0,8}(?:夜景|夜色|江景|灯光|江面)"
    r"|取景|拍照|摄影|拍(?:一|几|两)张"
    r"|(?:看|观看|远眺|观赏)(?:夜景|江景|灯光|江面)|欣赏(?:夜色|夜景)|夜游"
)
_NIGHT_OTHER_TIME = re.compile(r"白天|早上|上午|中午|下午|日间|次日|翌日|明天")
_CONTEXT_DESCRIPTION = re.compile(r"喜欢|偏好|(?:主题|标题)(?:是|为|：|:)|例如|举例|推荐|适合|适宜|或许|可能")
_CONTEXT_ACTION_NOUN = re.compile(r"点|区|机位|指南|建议|偏好|主题|需求|服务|机会|需要|权限")
_NATURE_BODY_PHRASES = ("沿园走", "逛公园", "看山水", "沿步道走", "沿步道步行", "沿步道漫步")
_NATURE_PARK_WALK = re.compile(
    r"(?:在|沿(?:着)?)(?:园(?:里|内|中)|公园(?:里|内|中)|步道)"
    r"(?:(?:慢慢地|慢慢|缓缓|轻松地|轻松|悠闲地|悠闲|随意)){0,2}"
    r"(?:散步|漫步|步行|慢走|走(?:一段|一圈|完)?)"
)
_NATURE_PARK_CONTEXT = re.compile(r"公园|园内|园里|园中")
_NATURE_CONTEXT_WALK = re.compile(r"散步|漫步|慢慢走|慢走|走一走|走走|逛一逛|逛逛|走一段|走一圈")
_NATURE_FOOD_SPAN = re.compile(r"<!--\s*food(?::[^>]+)?\s*-->.*?<!--\s*/food\s*-->", re.DOTALL)
_NATURE_REVERSE_OUTSIDE = re.compile(r"园外|公园外|街区|街上|返程|回程|餐馆|餐厅|午餐|午饭|晚餐|晚饭|用餐|咖啡")
_NATURE_REVERSE_ARRIVAL = re.compile(
    r"(?:去|到|来到|走到|到达|前往|进入).{0,8}(?:公园|园内|园里|园中)"
    r"|入园|下一站|前方|前面|尚未|还未"
)
_NATURE_REVERSE_REFUSAL = re.compile(r"(?:不|没|未|别|不要|没有).{0,5}(?:入园|进入|进园)")
_SHOPPING_BODY_PHRASES = ("逛摊", "挑选商品", "挑选东西", "挑选物品", "挑选纪念品",
                         "沿商铺浏览", "沿商铺逛", "沿摊位浏览", "沿摊位逛", "沿摊位挑选",
                         "买东西", "购物", "商店逛逛", "逛商店", "逛商铺", "逛市场")

_DIMENSION_BODY_PHRASES: dict[str, tuple[str, ...]] = {
    NIGHT_VIEW: _NIGHT_BODY_PHRASES,
    FAMILY: _FAMILY_BODY_PHRASES,
    FOOD: _FOOD_BODY_PHRASES,
    CITYWALK: _CITYWALK_BODY_PHRASES,
    PHOTO: _PHOTO_BODY_PHRASES,
    CULTURE: _CULTURE_BODY_PHRASES,
    NATURE: _NATURE_BODY_PHRASES,
    SHOPPING: _SHOPPING_BODY_PHRASES,
}

_AUTO_NIGHT_TEMPLATE = re.compile(r"适合安排在傍晚或夜间")
_CLOCK_RANGE_RE = re.compile(
    r"(\d{1,2}[:：]\d{2})\s*[-–—~至到]\s*(\d{1,2}[:：]\d{2})"
)
_CLAUSE_SPLIT = re.compile(r"[。！？!?;；\n，,、]")
_UNEXECUTED_CLAUSE = re.compile(r"如果|若是|假如|要是|据说|听说|可以考虑|建议|适合安排")
# Time cues have different roles: an event's completion is a sequence anchor;
# an unattached future cue postpones the activity. A date word alone is no event.
_FUTURE_TIME_CUE = re.compile(r"以后|日后|改天|下次|将来")
_CURRENT_TIME_CUE = re.compile(r"^(?:但|不过|而|随后|接着)?(?:今天|今日|当天|本次|现在|此刻)")
_RETURN_ACTIVITY = re.compile(r"回到|返回|进入")
_WALK_MODIFIER_ACTION = re.compile(r"散步|步行|行走|漫步|慢慢走|慢走|走一段|走走")
_FITNESS_MODIFIER = re.compile(r"适合(?:自己|自身|当前体力|体力)")
_MODIFIER_GOVERNOR = re.compile(
    r"(?:按(?:照)?|沿(?:着)?(?:园内|园里|园中)?|循(?:着)?|以|(?:选|挑|找).{0,6})$"
)
_QUOTED_SPAN = re.compile(r"[「『“\"].*?[」』”\"]")
_BODY_BOUNDARY = "〔省略边界〕"
_NIGHT_START_MINUTE = 18 * 60
_NIGHT_MORNING_END_MINUTE = 5 * 60


@dataclass(frozen=True)
class PreferenceFulfillmentFinding:
    plan_index: int
    day: int
    place_id: int
    place_name: str
    dimension: str
    layer: str
    status: str
    reason_code: str
    fragment_registered: bool = False
    action_authorized: bool = False
    arrangement_supported: bool = False

    @property
    def key(self) -> FragmentKey:
        return (self.plan_index, self.day, self.place_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_index": self.plan_index,
            "day": self.day,
            "place_id": self.place_id,
            "place_name": self.place_name,
            "dimension": self.dimension,
            "layer": self.layer,
            "status": self.status,
            "reason_code": self.reason_code,
            "fragment_registered": self.fragment_registered,
            "action_authorized": self.action_authorized,
            "arrangement_supported": self.arrangement_supported,
        }


@dataclass
class PreferenceFulfillmentReport:
    dimensions: tuple[str, ...] = ()
    findings: list[PreferenceFulfillmentFinding] = field(default_factory=list)
    classic_day_used: bool = False
    warn_only_seen: bool = False
    body_review_required: bool = False
    route_decisions: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "dimensions": list(self.dimensions),
            "route_decisions": self.route_decisions,
            "findings": [item.to_dict() for item in self.findings],
            "classic_day_used": self.classic_day_used,
            "warn_only_seen": self.warn_only_seen,
        }

    def findings_for(self, *, layer: str | None = None, status: str | None = None) -> list[PreferenceFulfillmentFinding]:
        rows = self.findings
        if layer is not None:
            rows = [item for item in rows if item.layer == layer]
        if status is not None:
            rows = [item for item in rows if item.status == status]
        return rows

    def route_gap_findings(self) -> list[PreferenceFulfillmentFinding]:
        return [
            item
            for item in self.findings
            if item.reason_code in ROUTE_OWNER_REASONS
            and item.status in {STATUS_MISSING, STATUS_UNSUPPORTED}
        ]

    def blocking_route_findings(self) -> list[PreferenceFulfillmentFinding]:
        # v0.13 closeout (Q11): under the Review policy a route that misses or
        # has not arranged a preference is recorded only; the Writer still runs.
        # A request without any route keeps blocking.
        if self.body_review_required:
            return [item for item in self.route_gap_findings() if item.reason_code == REASON_NO_TARGET]
        return self.route_gap_findings()

    def admission_block_findings(self) -> list[PreferenceFulfillmentFinding]:
        # Under the Review policy a body-layer admission gap (no registered
        # fragment / no authorized action for the preference) only says the
        # prose cannot voice it; like the wording itself it is recorded only.
        return [
            item
            for item in self.findings
            if item.reason_code in ADMISSION_BLOCK_REASONS
            and item.status in {STATUS_MISSING, STATUS_UNSUPPORTED}
            and not (self.body_review_required and item.layer == LAYER_BODY)
        ]

    def repairable_body_findings(self) -> list[PreferenceFulfillmentFinding]:
        # v0.13 closeout: under the Review policy, whether the prose voices a
        # preference is recorded only. It neither gates delivery nor drives
        # repair; route and admission findings keep their own gates.
        if self.body_review_required:
            return []
        return [
            item
            for item in self.findings
            if item.layer == LAYER_BODY
            and item.reason_code in BODY_MISSING_REASONS | {REASON_UNKNOWN}
            and item.status in {STATUS_MISSING, STATUS_UNKNOWN}
            and item.arrangement_supported
            and item.fragment_registered
            and item.action_authorized
        ]

    def unresolved_body_findings(self) -> list[PreferenceFulfillmentFinding]:
        if self.body_review_required:
            return []
        return [
            item
            for item in self.findings
            if item.layer == LAYER_BODY
            and item.status in {STATUS_MISSING, STATUS_UNKNOWN, STATUS_UNASSESSED}
            and item.reason_code in BODY_MISSING_REASONS | ADMISSION_BLOCK_REASONS | {REASON_UNKNOWN, REASON_UNASSESSED}
        ]

    def issue_codes_by_key(self) -> dict[FragmentKey, set[str]]:
        mapping: dict[FragmentKey, set[str]] = {}
        for item in self.repairable_body_findings():
            mapping.setdefault(item.key, set()).add(item.reason_code)
            mapping[item.key].add(f"explicit_preference:{item.dimension}")
        return mapping

    def repair_hints_by_key(self) -> dict[FragmentKey, str]:
        """Carry admitted, missing dimensions to the existing local writer.

        These are requirements on the already authorized action, never new
        action permissions or facts. Admission failures produce no hint.
        """
        return self._body_hints_by_key(self.repairable_body_findings())

    def _body_hints_by_key(
        self, findings: Iterable[PreferenceFulfillmentFinding],
    ) -> dict[FragmentKey, str]:
        instructions = {
            NIGHT_VIEW: "夜景偏好：按已锁定的晚间安排写观看或拍照行动，明确晚上看夜景或夜游；不得补造亮灯、开放时间、景物或机位事实。",
            CULTURE: "文化偏好：在现有授权动作内写参观、看展或观察建筑外观；不补造历史、展品或建筑构件。",
            NATURE: "自然偏好：在现有授权动作内写逛公园、沿园走、看山水或沿步道的实际行动；只选符合当前地点类型与授权的表达，不补造步道或景物。",
            FAMILY: "亲子偏好：写带孩子一起执行已有授权活动，不补造儿童设施或适龄保证。",
            FOOD: "美食偏好：写已有授权的用餐行动，不补造菜品或价格。",
            CITYWALK: "漫步偏好：写已有授权的沿街走或街区漫步行动，不补造沿途景物。",
            PHOTO: "拍照偏好：写已有授权的取景或找角度行动，不补造机位或光线条件。",
            SHOPPING: "购物偏好：写已有授权的逛摊或挑选行动，不补造商品。",
        }
        by_key: dict[FragmentKey, list[str]] = {}
        for item in findings:
            hint = instructions.get(item.dimension)
            if hint and hint not in by_key.setdefault(item.key, []):
                by_key[item.key].append(hint)
        return {key: " ".join(hints) for key, hints in by_key.items()}

    def early_writer_requirements(
        self, *, repair_triggered: bool = False,
    ) -> tuple[dict[FragmentKey, set[str]], dict[FragmentKey, str]]:
        """Carry original writing requirements only inside an admitted early round.

        Pending requirements are neither proven defects nor repair triggers.
        Under the Review policy no preference requirement joins any repair.
        """
        if (self.body_review_required or not repair_triggered
                or self.blocking_route_findings() or self.admission_block_findings()):
            return {}, {}
        findings = self.repairable_body_findings()
        codes: dict[FragmentKey, set[str]] = {}
        for item in findings:
            codes.setdefault(item.key, set()).update(
                {item.reason_code, f"explicit_preference:{item.dimension}"})
        return codes, self._body_hints_by_key(findings)

    def to_generation_issues(
        self,
        findings: Iterable[PreferenceFulfillmentFinding] | None = None,
    ) -> list[GenerationIssue]:
        issues: list[GenerationIssue] = []
        for item in findings if findings is not None else [
            *self.blocking_route_findings(),
            *self.admission_block_findings(),
            *self.unresolved_body_findings(),
        ]:
            issues.append(
                GenerationIssue(
                    source="deterministic",
                    category="BLOCKER",
                    publish_action="FAIL_CLOSED",
                    reason=item.reason_code,
                    plan_index=item.plan_index,
                    day=item.day,
                    place_id=item.place_id,
                    names=[item.place_name] if item.place_name else [],
                    evidence=item.dimension,
                    metadata={
                        "surface": "explicit_preference",
                        "dimension": item.dimension,
                        "layer": item.layer,
                        "review_anchored": True,
                    },
                )
            )
        return issues


def declared_explicit_dimensions(trip_request: TripRequest) -> tuple[str, ...]:
    normalized = normalize_preferences(trip_request.preferences)
    dimensions = list(sorted(normalized.scoring_dimensions))
    if family_arrangement_active(trip_request.preferences, notes=trip_request.notes):
        dimensions.append(FAMILY)
    return tuple(dimensions)


def _clause_negates_night(clause: str) -> bool:
    return bool(_NIGHT_HINT_NEGATION_RE.search(clause) or _NIGHT_NEGATION.search(clause))


def _is_night_minute(minute: int) -> bool:
    return minute >= _NIGHT_START_MINUTE or minute < _NIGHT_MORNING_END_MINUTE


def _clock_token_to_minute(token: str) -> int | None:
    raw = (token or "").replace("：", ":")
    if ":" not in raw:
        return None
    hours_text, minutes_text = raw.split(":", 1)
    try:
        hours = int(hours_text)
        minutes = int(minutes_text)
    except ValueError:
        return None
    if 0 <= hours <= 23 and 0 <= minutes <= 59:
        return hours * 60 + minutes
    return None


def _hint_clock_minutes(text: str) -> list[int]:
    values: list[int] = []
    for match in _NIGHT_HINT_CLOCK_RE.finditer(text or ""):
        minute = _clock_token_to_minute(match.group(0))
        if minute is not None:
            values.append(minute)
    return values


def _explicit_clock_ranges(text: str) -> list[tuple[int, int, int, int]]:
    ranges: list[tuple[int, int, int, int]] = []
    for match in _CLOCK_RANGE_RE.finditer(text or ""):
        start = _clock_token_to_minute(match.group(1))
        end = _clock_token_to_minute(match.group(2))
        if start is None or end is None:
            continue
        ranges.append((start, end, match.start(), match.end()))
    return ranges


def _locked_schedule_window(
    day_group: RouteDayGroup,
    trip_request: TripRequest | None,
) -> tuple[int | None, int | None]:
    start: int | None = None
    end: int | None = None
    policy = day_group.time_policy
    if policy is not None:
        start = int(policy.start_minute)
        end = int(policy.end_minute)
    if trip_request is not None:
        user_start = parse_hhmm(trip_request.daily_start)
        user_end = parse_hhmm(trip_request.daily_end)
        if user_start is not None:
            start = user_start if start is None else max(start, user_start)
        if user_end is not None:
            end = user_end if end is None else min(end, user_end)
    return start, end


def _break_intervals(
    day_group: RouteDayGroup,
    trip_request: TripRequest | None,
) -> list[tuple[int, int]]:
    intervals: list[tuple[int, int]] = []
    policy = day_group.time_policy
    if policy is not None:
        intervals.extend(list(policy.break_intervals or []))
        intervals.extend(list(policy.system_break_intervals or []))
    if trip_request is not None:
        for window in trip_request.rest_windows or []:
            start = parse_hhmm(window.start)
            end = parse_hhmm(window.end)
            if start is not None and end is not None and end > start:
                intervals.append((start, end))
    return intervals


def _minute_in_window(minute: int, start: int | None, end: int | None) -> bool:
    if start is not None and minute < start:
        return False
    if end is not None and minute > end:
        return False
    return True


def _minute_in_breaks(minute: int, intervals: list[tuple[int, int]]) -> bool:
    return any(start <= minute < end for start, end in intervals)


def _interval_intersects_breaks(
    start: int,
    end: int,
    intervals: list[tuple[int, int]],
) -> bool:
    return any(start < break_end and break_start < end for break_start, break_end in intervals)


def _range_fully_inside_window(
    start: int,
    end: int,
    window_start: int | None,
    window_end: int | None,
    breaks: list[tuple[int, int]],
) -> bool:
    if end < start:
        return False
    if not _is_night_minute(start) or not _is_night_minute(end):
        return False
    if not _minute_in_window(start, window_start, window_end):
        return False
    if not _minute_in_window(end, window_start, window_end):
        return False
    if _interval_intersects_breaks(start, end, breaks):
        return False
    return True


def _clause_has_other_place(clause: str, place_name: str, day_group: RouteDayGroup) -> bool:
    for other in day_group.places:
        other_name = (other.name or "").strip()
        if len(other_name) < 2 or other_name == place_name:
            continue
        if other_name in clause:
            return True
    return False


def _place_night_arrangement(
    place: CandidatePlace,
    day_group: RouteDayGroup,
    trip_request: TripRequest | None = None,
) -> bool:
    """L3 night arrangement needs a bound POI interval inside the locked window."""
    window_start, window_end = _locked_schedule_window(day_group, trip_request)
    breaks = _break_intervals(day_group, trip_request)
    place_name = (place.name or "").strip()
    if len(place_name) < 2:
        return False
    supported = False
    conflict = False
    from src.agents.route_planning import (
        ROUTE_NIGHT_HINT_PREFIX, route_night_hint, route_night_visit_intervals,
    )
    planned = route_night_visit_intervals(day_group, trip_request)
    for hint in day_group.time_hints or []:
        if hint.startswith(ROUTE_NIGHT_HINT_PREFIX):
            interval = planned.get(place.place_id)
            if interval is not None and hint == route_night_hint(place, interval):
                if _range_fully_inside_window(*interval, window_start, window_end, breaks):
                    supported = True
                else:
                    conflict = True
            elif place_name in hint:
                conflict = True
            continue
        if _AUTO_NIGHT_TEMPLATE.search(hint):
            continue
        for clause in _night_hint_clauses(hint):
            if _clause_negates_night(clause):
                continue
            if "建议" in clause or "适合安排" in clause:
                continue
            if not _night_activity_bound_to_place(clause, place_name):
                continue
            if _clause_has_other_place(clause, place_name, day_group):
                conflict = True
                continue
            ranges = _explicit_clock_ranges(clause)
            if ranges:
                leftover = False
                for match in _NIGHT_HINT_CLOCK_RE.finditer(clause):
                    if any(span_start <= match.start() < span_end for _s, _e, span_start, span_end in ranges):
                        continue
                    leftover = True
                    break
                if leftover:
                    conflict = True
                    continue
                if all(
                    _range_fully_inside_window(start, end, window_start, window_end, breaks)
                    for start, end, _span_start, _span_end in ranges
                ):
                    supported = True
                else:
                    conflict = True
                continue
            night_clocks = [
                minute
                for minute in _hint_clock_minutes(clause)
                if _is_night_minute(minute)
            ]
            if not night_clocks:
                continue
            if len(night_clocks) != 1:
                conflict = True
                continue
            minute = night_clocks[0]
            if (
                _minute_in_window(minute, window_start, window_end)
                and not _minute_in_breaks(minute, breaks)
            ):
                supported = True
            else:
                conflict = True
    if conflict:
        return False
    return supported


def night_activity_candidate(place: CandidatePlace) -> bool:
    return candidate_matches_dimension(place, NIGHT_VIEW)


def explicit_candidate_matches_dimension(place: CandidatePlace, dimension: str) -> bool:
    """Compatibility entry; identity is owned by preferences."""
    return candidate_matches_dimension(place, dimension)


def _place_matches_dimension(place: CandidatePlace, dimension: str) -> bool:
    if dimension == FAMILY:
        return True
    if dimension in SCORING_DIMENSIONS:
        return explicit_candidate_matches_dimension(place, dimension)
    return False


def _arrangement_supports(
    place: CandidatePlace,
    dimension: str,
    day_group: RouteDayGroup,
    *,
    family_active: bool,
    trip_request: TripRequest | None = None,
) -> bool:
    del family_active
    if dimension == FAMILY:
        return any(item.place_id == place.place_id for item in day_group.places)
    if dimension == NIGHT_VIEW:
        return _place_night_arrangement(place, day_group, trip_request)
    return _place_matches_dimension(place, dimension)


def route_preference_decisions(trip_request, route_plans, *, plan_index_offset=0):
    """Keep current arrangement gaps separate from evidence of impossibility.

    Shared by pre-Writer admission, body requirements and final qualification.
    A short real activity is present, not a certificate of overall sufficiency.
    """
    from src.agents.route_planning import route_night_gap
    rows = []
    for index, route in enumerate(route_plans, 1 + plan_index_offset):
        for dimension in declared_explicit_dimensions(trip_request):
            matching = [(day, place) for day in route.day_groups for place in day.places
                        if _place_matches_dimension(place, dimension)]
            arranged = [(day, place) for day, place in matching if _arrangement_supports(
                place, dimension, day, family_active=False, trip_request=trip_request)]
            evidence = next((r for r in route.preference_coverage if r.get("dimension") == dimension), {})
            current = evidence.get("snapshot") == route_preference_snapshot(route, trip_request)
            compromise = not arranged and route_preference_compromise(route, dimension, trip_request)
            row = {"plan_index": index, "dimension": dimension,
                "member_present": bool(matching), "arrangement_present": bool(arranged),
                "arranged_place_ids": [place.place_id for _, place in arranged],
                "resolution": "ARRANGED" if arranged else "JUSTIFIED_COMPROMISE" if compromise else "UNVERIFIED",
                "search_status": evidence.get("status", "NOT_ASSESSED") if current else "NOT_ASSESSED",
                "evidence_current": current, "snapshot": route_preference_snapshot(route, trip_request),
                "detailed_route_eligible": bool(arranged) or compromise}
            if dimension == NIGHT_VIEW:
                # Recorded only: which days missed their evening slot, and why.
                row["night_gaps"] = [{"day": int(day.day), "reason": reason}
                    for day in route.day_groups
                    if (reason := route_night_gap(day, trip_request)) is not None]
            rows.append(row)
    return rows


def _fragment_text(plan: PlanOutput | None, key: FragmentKey) -> str:
    if plan is None:
        return ""
    fragment = plan.poi_fragment(plan_index=key[0], day=key[1], place_id=key[2])
    return fragment.text if fragment is not None else ""


def _strip_place_prefix(text: str, place_name: str) -> str:
    body = (text or "").strip()
    name = (place_name or "").strip()
    if name and body.startswith(name):
        body = re.sub(rf"^{re.escape(name)}\s*[：:，,。\-—]*\s*", "", body, count=1)
    return body


def _title_only_hit(body: str, place_name: str, phrases: tuple[str, ...]) -> bool:
    if not place_name:
        return False
    if any(phrase in body for phrase in phrases):
        return False
    return any(phrase in place_name for phrase in phrases)


def _body_clauses(text: str) -> list[str]:
    clauses: list[str] = []
    for part in _CLAUSE_SPLIT.split(text or ""):
        compact = re.sub(r"\s+", "", part)
        if compact:
            clauses.append(compact)
    return clauses


def _clause_has_phrase(clause: str, phrases: tuple[str, ...]) -> bool:
    return any(phrase in clause for phrase in phrases)


def _clause_negates_phrases(clause: str, phrases: tuple[str, ...]) -> bool:
    for phrase in phrases:
        if phrase not in clause:
            continue
        escaped = re.escape(phrase)
        if re.search(
            rf"(?:不是|没有|不安排|不要|不可|不能|不会|不|没|无|非|勿|别|未).{{0,8}}{escaped}",
            clause,
        ):
            return True
        if re.search(
            rf"{escaped}.{{0,6}}(?:不安排|不要|不可|不能|不是|没有)",
            clause,
        ):
            return True
    return False


def _affirmative_activity_matches(clause: str) -> list[re.Match[str]]:
    """Actions, rather than guides/services or negated actions, anchor a scope."""
    return [
        match for match in ACTIVITY_ACTION_RE.finditer(clause)
        if not _CONTEXT_ACTION_NOUN.match(clause[match.end():])
        and not _clause_negates_phrases(clause, (match.group(),))
    ]


def _clause_time_relation(clause: str) -> str:
    """Resolve future cues against their preceding event in the same clause.

    This classifies the temporal relation only. Location, negation, conditions
    and the preference-specific object must still support the activity.
    """
    sequence = False
    for cue in _FUTURE_TIME_CUE.finditer(clause):
        prefix = clause[:cue.start()]
        if cue.group() == "以后" and _affirmative_activity_matches(prefix):
            sequence = True
        else:
            return "deferred"
    if sequence:
        return "sequence"
    if _CURRENT_TIME_CUE.match(clause):
        return "current"
    return "unspecified"


def _clause_unexecuted(clause: str) -> bool:
    return bool(_UNEXECUTED_CLAUSE.search(clause)) or _clause_time_relation(clause) == "deferred"


def _clause_descriptive(clause: str) -> bool:
    """Suitability may qualify an instructed path/pace, not the whole place.

    Bind each modifier to its governing instruction and following walk. Ignore
    only that marker for description classification; do not delete words from
    the evidence or grant an activity. Other description markers still apply.
    """
    instruction_modifiers: set[int] = set()
    for modifier in _FITNESS_MODIFIER.finditer(clause):
        if not _MODIFIER_GOVERNOR.search(clause[:modifier.start()]):
            continue
        action = _WALK_MODIFIER_ACTION.search(clause, modifier.end())
        if action is None or _CONTEXT_ACTION_NOUN.match(clause[action.end():]):
            continue
        # A different assertion between the modifier and walk cannot make the
        # modifier part of that instruction. Existing finite local bindings stay
        # bounded; full-sentence examples and place names never enter this rule.
        between = clause[modifier.end():action.start()]
        if len(between) <= 8 and not _CONTEXT_DESCRIPTION.search(between):
            instruction_modifiers.add(modifier.start())
    return any(match.start() not in instruction_modifiers
               for match in _CONTEXT_DESCRIPTION.finditer(clause))


@dataclass(frozen=True)
class _ExecutionClause:
    text: str
    time_relation: str
    disposition: str


def _execution_clauses(text: str, *, dimension: str,
                       defer_description: bool = False) -> list[_ExecutionClause]:
    """Classify execution scopes without erasing the input or merging boundaries.

    Conditional/advisory and refusal scopes remain active across punctuation
    and ownership changes. Deferred scope ends only at an affirmative explicit
    current instruction. Ending deferral never ends a condition or grants owner
    identity. The owner pass remains responsible for explicit POI re-entry.
    """
    modal_scope = False
    refusal_scope = False
    deferred_scope = False
    clauses: list[_ExecutionClause] = []
    for clause in _body_clauses(text):
        if clause == _BODY_BOUNDARY:
            clauses.append(_ExecutionClause(clause, "unspecified", "boundary"))
            continue
        time_relation = _clause_time_relation(clause)
        modal_scope |= bool(_UNEXECUTED_CLAUSE.search(clause))
        refusal_scope |= dimension == NATURE and bool(_NATURE_REVERSE_REFUSAL.search(clause))
        if time_relation == "deferred":
            deferred_scope = True
        elif time_relation == "current":
            current_actions = _affirmative_activity_matches(clause)
            current_return = any(
                not _clause_negates_phrases(clause, (match.group(),))
                for match in _RETURN_ACTIVITY.finditer(clause)
            )
            if (current_actions or current_return) and not _clause_descriptive(clause):
                deferred_scope = False
        disposition = (
            "modal" if modal_scope else "refused" if refusal_scope
            else "deferred" if deferred_scope
            else "descriptive" if not defer_description and _clause_descriptive(clause)
            else "asserted"
        )
        clauses.append(_ExecutionClause(clause, time_relation, disposition))
    return clauses


def _current_place_body(text: str, place_name: str, other_names: tuple[str, ...]) -> tuple[str, bool]:
    """Resolve location once, before any preference-specific activity checks.

    Source/comparison references do not transfer the subject. Destinations,
    direct activity objects and unqualified named locations do. Every transfer
    emits a boundary, so context cannot be borrowed across a visit elsewhere.
    Longest names win; words inside a name never become activity evidence.
    """
    names = sorted(set(filter(None, (place_name, *other_names))), key=len, reverse=True)
    if not names:
        return text, False
    name_re = re.compile("|".join(map(re.escape, names)))
    # These are grammatical relations, shared by every preference, not prose
    # examples or preference-specific arrival exceptions.
    source_tail = re.compile(r"(?:过来|来到这里|返回这里)(?:之?后|以后)?$")
    relation_tail = re.compile(r"(?:很|比较|更)?(?:近|远|相似|不同)(?:一些)?$")
    transition = re.compile(r"(?:随后|接着|然后|再|后)?(?:回到|返回|走到|来到|到达|前往|进入|去|到|回|在|从)$")
    direct_object = re.compile(r"(?:参观|看展|阅读|观看|观察|拍摄|游览|逛)(?:一下|一番|这座|这家|该|的)*$")
    owner = True
    result: list[str] = []
    saw_other = False
    for clause in _body_clauses(text):
        result.append("，")
        mentions = list(name_re.finditer(clause))
        if not mentions:
            # An explicit destination outside the locked name inventory cannot
            # be assumed to be this POI. Only an explicit current name restores
            # ownership; an implicit "here" cannot silently restore it.
            unresolved = re.search(
                r"(?:前往|到达)(?!这里|本站|园内|园里|园中|公园内|步道)(?=.+)", clause,
            )
            if unresolved:
                if owner:
                    result.append(clause[:unresolved.start()])
                    result.append(f"，{_BODY_BOUNDARY}，")
                owner = False
                saw_other = True
                continue
        if len(mentions) == 1:
            mention = mentions[0]
            prefix, suffix = clause[:mention.start()], clause[mention.end():]
            if ((prefix == "从" and source_tail.fullmatch(suffix))
                    or (re.fullmatch(r"(?:这里|本站)?(?:离|距|相比|比|与)", prefix)
                        and relation_tail.fullmatch(suffix))):
                continue
        cursor = 0
        for mention in mentions:
            prefix = clause[cursor:mention.start()]
            relation = transition.search(prefix) or direct_object.search(prefix)
            split = relation.start() if relation else 0
            new_owner = mention.group() == place_name
            if owner and new_owner:
                result.append(prefix + "本站")
                cursor = mention.end()
                continue
            if owner and prefix[:split]:
                result.append(prefix[:split])
            if new_owner != owner:
                result.append(f"，{_BODY_BOUNDARY}，")
            owner = new_owner
            saw_other |= not owner
            if owner:
                # Mask names without joining neighboring syllables.
                result.append(prefix[split:] + "本站")
            cursor = mention.end()
        if owner:
            result.append(clause[cursor:])
    return "".join(result), saw_other


def _clause_supports_dimension(clause: str, dimension: str, phrases: tuple[str, ...]) -> bool:
    # A service, guide or photo spot describes a noun, not an executed action.
    actionable = tuple(phrase for phrase in phrases if any(
        not _CONTEXT_ACTION_NOUN.match(clause[match.end():])
        for match in re.finditer(re.escape(phrase), clause)
    ))
    if not actionable:
        return False
    if dimension == NIGHT_VIEW:
        # Use the context/action path below for every night expression, so a
        # literal 夜景 cannot bypass a daytime shift or a foreign owner.
        return False
    if dimension == FAMILY:
        return False  # Subject and activity must share the contextual owner.
    return True


def _executed_body(text: str, *, dimension: str, defer_description: bool = False) -> str:
    """Share scope exclusions across literal and contextual recognition.

    Removed spans stay boundaries; deletion must not join two syllables into
    an activity. A conditional governs the following clauses in this POI
    paragraph, including a later return to the current named place.
    """
    boundary = f"，{_BODY_BOUNDARY}，"
    working = _QUOTED_SPAN.sub(boundary, text)
    if dimension != FOOD:
        working = _NATURE_FOOD_SPAN.sub(boundary, working)
    else:
        # A registered food POI may express its own meal in its food span.
        working = re.sub(r"<!--\s*/?food(?::[^>]+)?\s*-->", "，", working)
    return "，".join(
        item.text if item.disposition == "asserted" else _BODY_BOUNDARY
        for item in _execution_clauses(working, dimension=dimension,
                                      defer_description=defer_description)
    )


def _activity_object_relation_supports(
    body: str,
    *,
    object_re: re.Pattern[str],
    action_re: re.Pattern[str],
    unrelated_re: re.Pattern[str] | None = None,
    other_object_re: re.Pattern[str] | None = None,
    outside_re: re.Pattern[str] | None = None,
    refusal_re: re.Pattern[str] | None = None,
) -> bool:
    """Bind an affirmative action and its object in either order at one POI.

    The caller supplies dimension vocabulary, not place names or prose examples.
    Ownership/execution filtering has already emitted explicit boundaries. Keep
    those boundaries and unrelated objects: a pending look at a phone, or a walk
    outside a park, cannot borrow a later exhibit/park context. A fresh object
    and action may establish their own relation after a boundary.
    """
    boundary = f"，{_BODY_BOUNDARY}，"
    working = _NATURE_FOOD_SPAN.sub(boundary, _QUOTED_SPAN.sub(boundary, body))
    object_noun = re.compile(
        rf"(?:{_CONTEXT_ACTION_NOUN.pattern}|地图|照片|图片|视频|介绍)"
    )
    seen_object = pending_action = unrelated_object = blocked = False
    for clause in _body_clauses(working):
        if clause == _BODY_BOUNDARY:
            seen_object = pending_action = unrelated_object = False
            continue
        # The ownership pass masks an explicit current-POI re-entry as 本站.
        # Start a fresh object frame there; it cannot recover a conditional or
        # foreign activity that the execution/owner pass has already removed.
        if re.search(r"(?:回到|返回)本站", clause):
            seen_object = pending_action = unrelated_object = False
        if _clause_unexecuted(clause) or (refusal_re and refusal_re.search(clause)):
            blocked = True
        if blocked or _clause_descriptive(clause):
            seen_object = pending_action = False
            continue
        objects = list(object_re.finditer(clause))
        actions = [m for m in action_re.finditer(clause)
                   if not _CONTEXT_ACTION_NOUN.match(clause[m.end():])]
        activities = actions + [m for m in ACTIVITY_ACTION_RE.finditer(clause)
                                if not _CONTEXT_ACTION_NOUN.match(clause[m.end():])]
        def binds_unrelated(pattern: re.Pattern[str] | None) -> bool:
            if pattern is None:
                return False
            matches = list(pattern.finditer(clause))
            # An incidental instruction such as putting a phone away is not
            # the object of a look/read. A bare object or an actual activity
            # establishes the competing relation instead.
            return bool(matches) and (bool(activities) or any(
                m.span() == (0, len(clause)) for m in matches
            ))
        if ((outside_re and outside_re.search(clause))
                or binds_unrelated(unrelated_re)
                or (not objects and binds_unrelated(other_object_re))):
            seen_object = pending_action = False
            unrelated_object = True
            continue
        if any(object_noun.match(clause[m.end():]) for m in objects):
            seen_object = pending_action = False
            unrelated_object = True
        objects = [m for m in objects if not object_noun.match(clause[m.end():])]
        affirmative_objects = [m for m in objects
                               if not _clause_negates_phrases(clause, (m.group(),))]
        events = [(m.end(), 1, "object", m) for m in objects]
        events.extend((m.end(), 2, "action", m) for m in actions)
        for _, _, kind, match in sorted(events, key=lambda event: event[:2]):
            if _clause_negates_phrases(clause, (match.group(),)):
                seen_object = pending_action = False
                continue
            if kind == "object":
                if pending_action:
                    return True
                seen_object = True
                unrelated_object = False
            elif seen_object:
                return True
            elif not unrelated_object or affirmative_objects:
                pending_action = True
    return False


def _contextual_body_supports(
    body: str,
    *,
    dimension: str,
) -> bool:
    """Use object relations for exhibits, directed time/subject for night/family.

    Object/action order may vary. Night-time and family-subject context retain
    their existing directed meaning and cannot be attached retroactively.
    Membership, action authorization and the locked route remain upstream.
    """
    if dimension == CULTURE:
        return _activity_object_relation_supports(
            body, object_re=_CULTURE_BODY_CONTEXT, action_re=_CULTURE_CONTEXT_ACTION,
            unrelated_re=_CULTURE_OTHER_READING_OBJECT,
            other_object_re=re.compile(
                r"江景|江面|风景|山水|夜景|夜色|灯光|餐馆|餐厅|午餐|午饭|晚餐|用餐|咖啡"
            ),
        )
    elif dimension == NIGHT_VIEW:
        context_re, action_re = _NIGHT_BODY_CONTEXT, _NIGHT_CONTEXT_ACTION
    elif dimension == FAMILY:
        context_re = re.compile("|".join(map(re.escape, _FAMILY_BODY_PHRASES)))
        action_re = _FAMILY_ACTION
    else:
        return False
    # Keep removed spans as clause boundaries; deletion must not manufacture
    # a context word from syllables on opposite sides of a quote/food span.
    working = _NATURE_FOOD_SPAN.sub("，", _QUOTED_SPAN.sub("，", body))
    context_active = False
    implicit_night_context = True
    for clause in _body_clauses(working):
        if clause == _BODY_BOUNDARY:
            context_active = False
            implicit_night_context = False
            continue
        contexts = list(context_re.finditer(clause))
        if dimension == NIGHT_VIEW and not implicit_night_context:
            # A surviving 夜景 object after a removed inline quote cannot
            # reconstruct the broken preceding night-time expression.
            contexts = [m for m in contexts if m.group() not in {"夜景", "夜色", "夜游"}]
        actions = [m for m in action_re.finditer(clause)
                   if not _CONTEXT_ACTION_NOUN.match(clause[m.end():])]
        if dimension == NIGHT_VIEW and _NIGHT_OTHER_TIME.search(clause):
            context_active = False
            continue
        events = []
        events.extend((m.end(), 1, "context", m.group()) for m in contexts)
        events.extend((m.end(), 2, "action", m.group()) for m in actions)
        if dimension == NIGHT_VIEW:
            events.extend((m.end(), 0, "daytime", m.group()) for m in _NIGHT_OTHER_TIME.finditer(clause))
        for _, _, kind, value in sorted(events):
            if kind == "daytime":
                context_active = False
            elif kind == "context":
                context_active = not _clause_negates_phrases(clause, (value,))
                if dimension == NIGHT_VIEW and value not in {"夜景", "夜色", "夜游"}:
                    implicit_night_context = True
            elif context_active:
                if dimension in {CULTURE, FAMILY} and _CULTURE_OTHER_READING_OBJECT.search(clause):
                    continue
                if _clause_negates_phrases(clause, (value,)):
                    continue
                return True
    return False


def _nature_walk_before_context_supports(body: str) -> bool:
    """Use the shared relation, retaining park refusal/outside/arrival fences."""
    return _activity_object_relation_supports(
        body, object_re=_NATURE_PARK_CONTEXT,
        action_re=re.compile(rf"(?:{_NATURE_CONTEXT_WALK.pattern}|步行|行走)"),
        outside_re=re.compile(
            rf"(?:{_NATURE_REVERSE_OUTSIDE.pattern}|{_NATURE_REVERSE_ARRIVAL.pattern})"
        ),
        refusal_re=_NATURE_REVERSE_REFUSAL,
    )


def _nature_body_supports(body: str) -> bool:
    """Recognize the existing park-walk and mountain/water viewing meanings.

    Both use the shared affirmative action/object relation. Expanded scenery
    objects do not have to be adjacent to 看, and walking still needs a park
    object. A bare single-character action grants neither fulfillment nor
    authority. Quotes, attachments, foreign owners and non-execution have
    already been filtered without joining their former boundaries.
    """
    if _nature_walk_before_context_supports(body):
        return True
    return _activity_object_relation_supports(
        body, object_re=re.compile(r"山水"),
        action_re=re.compile(r"观看|观赏|欣赏|看看|看一看|看"),
        unrelated_re=_CULTURE_OTHER_READING_OBJECT,
        outside_re=re.compile(
            rf"(?:{_NATURE_REVERSE_OUTSIDE.pattern}|{_NATURE_REVERSE_ARRIVAL.pattern})"
        ),
        refusal_re=_NATURE_REVERSE_REFUSAL,
    )


def _culture_architecture_body_supports(body: str) -> bool:
    """Use the same object/action relation for authorized exterior observation."""
    return _activity_object_relation_supports(
        body, object_re=re.compile(r"建筑(?:外观|布局)|外观"),
        action_re=re.compile(r"观察|观看|看"),
        unrelated_re=re.compile(
            _CULTURE_OTHER_READING_OBJECT.pattern
            + r"|看(?:情况|时间|预算|安排|进度|起来|上去|来)"
        ),
    )


def _photo_composition_body_supports(body: str) -> bool:
    """Bind existing composition activity to a subsequent shutter action.

    Input has already passed the shared current-place/execution filter. Reuse
    the activity vocabulary rather than adding whole photo phrases. A bare
    composition noun, a generic single-character action, or another POI cannot
    supply this relation. Existing direct photo expressions remain supported.
    """
    composition = False
    for clause in _body_clauses(body):
        if clause == _BODY_BOUNDARY:
            composition = False
            continue
        for match in ACTIVITY_ACTION_RE.finditer(clause):
            action = match.group()
            if action not in {"构图", "取景", "拍"}:
                continue
            if (_CONTEXT_ACTION_NOUN.match(clause[match.end():])
                    or _clause_negates_phrases(clause, (action,))):
                composition = False
                continue
            if action in {"构图", "取景"}:
                composition = True
            elif composition and re.fullmatch(
                r"(?:|下(?:来)?|完|好|(?:一|两|几|多)张)", clause[match.end():]
            ):
                return True
    return False


def current_activity_body(text: str, *, place_name: str,
                          other_place_names: Iterable[str] = (),
                          dimension: str = "activity") -> tuple[str, bool]:
    """One execution/location boundary for coverage, preference and repaired prose."""
    body = _strip_place_prefix(text, place_name) if place_name else text
    working = _executed_body(body, dimension=dimension, defer_description=True)
    working, other = _current_place_body(working, place_name,
        tuple(n for n in other_place_names if n and n != place_name))
    return _executed_body(working, dimension=dimension), other


def has_executed_activity(body: str) -> bool:
    """Recognize actions only after the shared ownership/execution filtering."""
    return any(
        not _clause_negates_phrases(clause, (match.group(),))
        and not _CONTEXT_ACTION_NOUN.match(clause[match.end():])
        for clause in _body_clauses(body)
        for match in ACTIVITY_ACTION_RE.finditer(clause)
    )


def evaluate_body_for_dimension(
    text: str,
    *,
    dimension: str,
    place_name: str,
    other_place_names: Iterable[str] = (),
) -> tuple[str, str]:
    """Return (status, reason_code) for one POI body. Never uses a single character."""
    body = _strip_place_prefix(text, place_name)
    compact = re.sub(r"\s+", "", body)
    working, saw_other = current_activity_body(body, place_name=place_name,
        other_place_names=other_place_names, dimension=dimension)
    phrases = _DIMENSION_BODY_PHRASES.get(dimension, ())
    nature_direct_phrases = phrases
    if dimension == NATURE:
        # Real repair prose may express the same authorized park walk rather
        # than copy one of the four writing-hint literals. Require a park
        # location AND a walking action; bare 散步/园 or scenery is insufficient.
        nature_direct_phrases = tuple(dict.fromkeys((*phrases, *(
            match.group() for match in _NATURE_PARK_WALK.finditer(working)
        ))))
        phrases = tuple(dict.fromkeys((*nature_direct_phrases, *(
            match.group() for match in _NATURE_CONTEXT_WALK.finditer(working)
        ))))
    title_phrases = phrases
    if dimension == FAMILY:
        title_phrases = ("亲子", "带娃", "儿童")
    if not phrases:
        return STATUS_UNKNOWN, REASON_UNKNOWN

    clauses = _body_clauses(working)
    working_compact = re.sub(r"\s+", "", working)
    if not clauses and working_compact:
        clauses = [working_compact]

    supported = False
    saw_negation = False
    saw_unexecuted = False
    for clause in clauses:
        if clause == _BODY_BOUNDARY:
            continue
        if dimension == NIGHT_VIEW and (_NIGHT_NEGATION.search(clause) or _clause_negates_night(clause)):
            if _clause_has_phrase(clause, phrases):
                saw_negation = True
            continue
        if dimension == FAMILY and _FAMILY_NEGATION.search(clause):
            saw_negation = True
            continue
        if _clause_negates_phrases(clause, phrases):
            saw_negation = True
            continue
        if _clause_unexecuted(clause):
            if _clause_has_phrase(clause, phrases):
                saw_unexecuted = True
            continue
        if dimension == NATURE and not _clause_has_phrase(clause, nature_direct_phrases):
            # Generic walks require the shared object relation in both orders.
            # A separate forward latch would treat "公园外" as park context and
            # bypass the relation's location/arrival fence.
            continue
        if _clause_supports_dimension(clause, dimension, phrases):
            supported = True

    if supported or _contextual_body_supports(
        working, dimension=dimension,
    ) or (dimension == NATURE and _nature_body_supports(
        working,
    )) or (dimension == CULTURE and _culture_architecture_body_supports(
        working,
    )) or (dimension == PHOTO and _photo_composition_body_supports(working)):
        return STATUS_SUPPORTED, REASON_BODY_MISSING
    if saw_other:
        return STATUS_MISSING, REASON_OTHER_PLACE
    if saw_negation:
        return STATUS_MISSING, REASON_NEGATION
    if _title_only_hit(compact, place_name, title_phrases):
        return STATUS_MISSING, REASON_TITLE_ONLY
    if dimension == NIGHT_VIEW and _DAYTIME_LIGHT.search(compact):
        return STATUS_MISSING, REASON_BODY_MISSING
    if saw_unexecuted:
        return STATUS_MISSING, REASON_BODY_MISSING
    if dimension in {FOOD, CITYWALK, PHOTO, CULTURE, NATURE, SHOPPING, NIGHT_VIEW, FAMILY}:
        return STATUS_MISSING, REASON_BODY_MISSING
    return STATUS_UNKNOWN, REASON_UNKNOWN


def _contract_authorizes_dimension(contract: Any, dimension: str) -> bool:
    if contract is None:
        return False
    actions = tuple(str(item) for item in (contract.authorized_actions or ()) if item)
    if not actions:
        return False
    blob = " ".join(actions)
    if dimension == NIGHT_VIEW:
        return any(token in blob for token in ("夜景", "夜游", "灯光", "观景", "夜间", "拍照"))
    if dimension == FAMILY:
        return True
    if dimension == FOOD:
        return any(token in blob for token in ("用餐", "午餐", "晚餐", "吃"))
    if dimension == PHOTO:
        return any(token in blob for token in ("拍照", "取景", "打卡"))
    if dimension == CITYWALK:
        return any(token in blob for token in ("步行", "街区", "逛"))
    if dimension == CULTURE:
        return any(token in blob for token in ("参观", "建筑", "展"))
    if dimension == NATURE:
        return any(token in blob for token in ("园", "山水", "步道", "观景"))
    if dimension == SHOPPING:
        return any(token in blob for token in ("摊", "挑选", "商铺"))
    return True


def _iter_locked_stops(
    route_plans: list[RoutePlan],
    *,
    plan_index_offset: int = 0,
) -> Iterable[tuple[int, RouteDayGroup, CandidatePlace]]:
    for plan_index, route_plan in enumerate(route_plans or [], 1 + plan_index_offset):
        for day_group in route_plan.day_groups:
            for place in day_group.places:
                yield plan_index, day_group, place


def evaluate_explicit_preference_fulfillment(
    *,
    trip_request: TripRequest,
    route_plans: list[RoutePlan],
    plans: list[PlanOutput] | None = None,
    structured_evidence_payload: StructuredEvidencePayload | None = None,
    retrieval: RetrievalResult | None = None,
    classic_day_used: bool = False,
    warn_only_seen: bool = False,
    plan_index_offset: int = 0,
    body_review_required: bool = False,
) -> PreferenceFulfillmentReport:
    """Check declared preferences at member, arrangement, and body layers.

    Body-layer statuses always come from the lexical evaluator. Under the
    Review policy they are diagnostics only (see repairable_body_findings).
    """
    del retrieval  # Owner identity stays on the locked route, not the raw pool.
    dimensions = declared_explicit_dimensions(trip_request)
    report = PreferenceFulfillmentReport(
        dimensions=dimensions,
        classic_day_used=classic_day_used,
        warn_only_seen=warn_only_seen,
        body_review_required=body_review_required,
    )
    if classic_day_used:
        report.findings.append(
            PreferenceFulfillmentFinding(
                plan_index=0,
                day=0,
                place_id=0,
                place_name="",
                dimension="",
                layer=LAYER_ARRANGEMENT,
                status=STATUS_UNSUPPORTED,
                reason_code=REASON_CLASSIC_DAY,
            )
        )
    if warn_only_seen:
        report.findings.append(
            PreferenceFulfillmentFinding(
                plan_index=0,
                day=0,
                place_id=0,
                place_name="",
                dimension="",
                layer=LAYER_BODY,
                status=STATUS_UNSUPPORTED,
                reason_code=REASON_WARN_ONLY,
            )
        )
    if not dimensions:
        return report
    if not route_plans:
        for dimension in dimensions:
            report.findings.append(
                PreferenceFulfillmentFinding(
                    plan_index=0,
                    day=0,
                    place_id=0,
                    place_name="",
                    dimension=dimension,
                    layer=LAYER_MEMBER,
                    status=STATUS_MISSING,
                    reason_code=REASON_NO_TARGET,
                )
            )
        return report

    report.route_decisions = route_preference_decisions(
        trip_request, route_plans, plan_index_offset=plan_index_offset)
    family_active = FAMILY in dimensions
    stops = list(_iter_locked_stops(route_plans, plan_index_offset=plan_index_offset))
    other_names_by_plan: dict[int, list[str]] = {}
    for plan_index, day_group, place in stops:
        other_names_by_plan.setdefault(plan_index, []).append(place.name)

    for dimension in dimensions:
        decisions = [r for r in report.route_decisions if r["dimension"] == dimension]
        gaps = [r for r in decisions if not r["detailed_route_eligible"]]
        if body_review_required and gaps:
            for gap in gaps:
                report.findings.append(PreferenceFulfillmentFinding(
                    plan_index=gap["plan_index"], day=0, place_id=0, place_name="",
                    dimension=dimension, layer=LAYER_ARRANGEMENT if gap["member_present"] else LAYER_MEMBER,
                    status=STATUS_UNSUPPORTED if gap["member_present"] else STATUS_MISSING,
                    reason_code=REASON_ARRANGEMENT_UNSUPPORTED if gap["member_present"] else REASON_MEMBER_MISSING))
            continue
        matching = [
            (plan_index, day_group, place)
            for plan_index, day_group, place in stops
            if _place_matches_dimension(place, dimension)
        ]
        if not matching:
            if all(route_preference_compromise(route, dimension, trip_request) for route in route_plans):
                continue
            first_plan, first_day, first_place = stops[0]
            report.findings.append(
                PreferenceFulfillmentFinding(
                    plan_index=first_plan,
                    day=int(first_day.day),
                    place_id=int(first_place.place_id),
                    place_name=first_place.name,
                    dimension=dimension,
                    layer=LAYER_MEMBER,
                    status=STATUS_MISSING,
                    reason_code=REASON_MEMBER_MISSING,
                )
            )
            continue

        arranged = [
            item
            for item in matching
            if _arrangement_supports(
                item[2],
                dimension,
                item[1],
                family_active=family_active,
                trip_request=trip_request,
            )
        ]
        if not arranged:
            plan_index, day_group, place = matching[0]
            report.findings.append(
                PreferenceFulfillmentFinding(
                    plan_index=plan_index,
                    day=int(day_group.day),
                    place_id=int(place.place_id),
                    place_name=place.name,
                    dimension=dimension,
                    layer=LAYER_ARRANGEMENT,
                    status=STATUS_UNSUPPORTED,
                    reason_code=REASON_ARRANGEMENT_UNSUPPORTED,
                )
            )
            continue

        if plans is None:
            continue

        body_findings: list[PreferenceFulfillmentFinding] = []
        for plan_index, day_group, place in arranged:
            key = (plan_index, int(day_group.day), int(place.place_id))
            plan = None
            scoped_index = plan_index - 1 - plan_index_offset
            if 0 <= scoped_index < len(plans):
                plan = plans[scoped_index]
            fragment = (
                plan.poi_fragment(plan_index=key[0], day=key[1], place_id=key[2])
                if plan is not None
                else None
            )
            contract = (
                structured_evidence_payload.action_contract(
                    plan_index=key[0],
                    day=key[1],
                    place_id=key[2],
                )
                if structured_evidence_payload is not None
                else None
            )
            authorized = _contract_authorizes_dimension(contract, dimension)
            registered = fragment is not None
            base = {
                "plan_index": key[0],
                "day": key[1],
                "place_id": key[2],
                "place_name": place.name,
                "dimension": dimension,
                "fragment_registered": registered,
                "action_authorized": authorized,
                "arrangement_supported": True,
            }
            if plan is None or not registered:
                body_findings.append(
                    PreferenceFulfillmentFinding(
                        **base,
                        layer=LAYER_BODY,
                        status=STATUS_MISSING,
                        reason_code=REASON_NO_FRAGMENT,
                    )
                )
                continue
            if not authorized:
                body_findings.append(
                    PreferenceFulfillmentFinding(
                        **base,
                        layer=LAYER_BODY,
                        status=STATUS_MISSING,
                        reason_code=REASON_NO_ACTION_AUTHORIZATION,
                    )
                )
                continue
            status, reason = evaluate_body_for_dimension(
                fragment.text, dimension=dimension, place_name=place.name,
                other_place_names=other_names_by_plan.get(plan_index, ()),
            )
            body_findings.append(
                PreferenceFulfillmentFinding(
                    **base,
                    layer=LAYER_BODY,
                    status=status if status != STATUS_SUPPORTED else STATUS_SUPPORTED,
                    reason_code=reason if status != STATUS_SUPPORTED else "explicit_preference_body_supported",
                )
            )
        # PHOTO is an existential requirement over authorized targets, not a
        # requirement for every matching attraction to acquire photo permission.
        # When none is authorized retain the admission failure; never grant an
        # action from prose or suppress missing registration on an authorized POI.
        if dimension == PHOTO and any(item.action_authorized for item in body_findings):
            body_findings = [item for item in body_findings if item.action_authorized]
        if any(item.status == STATUS_SUPPORTED for item in body_findings):
            report.findings.extend(
                item for item in body_findings if item.status == STATUS_SUPPORTED
            )
            continue
        report.findings.extend(body_findings)
    return report


def sync_day_opening_for_locked_arrangement(
    plan_text: str,
    *,
    day: int,
    night_supported: bool,
    place_names: list[str],
) -> str:
    """Deterministic opening sync. Never LLM-rewrites a whole day."""
    if night_supported or not plan_text:
        return plan_text
    pattern = re.compile(
        rf"(Day\s*{day}：[^\n]*\n)([^\n]{{0,80}}\n)",
        re.I,
    )

    def _strip(match: re.Match[str]) -> str:
        opening = match.group(2)
        if any(name and opening.startswith(name) for name in place_names):
            return match.group(0)
        from src.agents.preference_notices import PROTECTED_OPENING_PREFIXES
        stripped = opening.lstrip()
        if any(stripped.startswith(prefix) for prefix in PROTECTED_OPENING_PREFIXES):
            return match.group(0)
        if not re.search(r"夜景|夜游", opening):
            return match.group(0)
        cleaned = re.sub(r"(?:夜景打卡|夜景|夜游)", "", opening)
        cleaned = re.sub(r"[、，,]{2,}", "、", cleaned).strip(" 、，,\n")
        if not cleaned:
            return match.group(1)
        return match.group(1) + cleaned + "\n"

    return pattern.sub(_strip, plan_text, count=1)



def build_body_review_context(*, trip_request, route_plans, plans,
                              structured_evidence_payload, previous_issues=(),
                              material_provenance=None):
    """Build the mandatory Review questions: one consistency check per plan.

    v0.13 closeout: pace, specificity/repetition and per-POI preference wording
    are not asked (pace -> v0.14, prose quality -> v0.16).
    """
    user_context = trip_request.model_dump(mode="json")
    authorization = asdict(structured_evidence_payload) if structured_evidence_payload else None
    sources = {
        "user": json.dumps(user_context, ensure_ascii=False, separators=(",", ":")),
        "authorization": json.dumps(authorization, ensure_ascii=False, separators=(",", ":")),
    }
    condition_sources = ["user", "authorization"] + [f"route:{i}" for i in range(1, len(plans) + 1)]
    requirements = []
    for index, plan in enumerate(plans, 1):
        if not fragment_registry_is_intact(plan):
            raise ValueError("body review requires an intact fragment registry")
        sources[f"plan:{index}"] = plan.plan_text
        sources[f"summary:{index}"] = plan.summary
        route = route_plans[index - 1] if index <= len(route_plans) else None
        from src.agents.route_experience_facts import route_experience_facts
        sources[f"experience:{index}"] = json.dumps(
            route_experience_facts(route) if route else {"days": []}, ensure_ascii=False)
        # Review does not use map geometry; keep the original RoutePlan intact.
        sources[f"route:{index}"] = route.model_dump_json(exclude={
            "day_groups": {"__all__": {
                "commute_legs": {"__all__": {"encoded_polyline"}},
            }},
        }) if route else ""
        requirements.append({"id": f"consistency:{index}", "kind": "consistency",
            "sources": [f"plan:{index}", f"summary:{index}", f"experience:{index}"] + condition_sources,
            "instruction": "检查全文自相矛盾及正文与锁定安排（地点、日期、顺序、时段）的冲突。节奏、轻松程度和写得是否具体不在本项判断范围。缺失判定必须引用冲突双方；未发现矛盾写 supported 并引用所核对的正文；未能确定写 unknown。"})
    previous = []
    for issue in previous_issues:
        if issue.source != "llm_review":
            continue  # Deterministic defects cannot be retracted by Review.
        indexes = [issue.plan_index] if issue.plan_index else list(range(1, len(plans) + 1))
        previous.append({"id": review_issue_id(issue), "issue": issue.model_dump(mode="json"),
            "sources": [key for i in indexes for key in (f"plan:{i}", f"summary:{i}") if key in sources]})
    context = {"version": "body-review-v4", "requirements": requirements, "sources": sources,
        "condition_sources": condition_sources,
        "plan_fingerprints": [body_review_snapshot({"plan": plan.model_dump(mode="json"),
            "registry": [f.model_dump(mode="json") for f in plan.poi_fragments]}) for plan in plans],
        "user_context": user_context,
        "authorization": authorization,
        "previous_issues": previous}
    supplied = material_provenance or {}
    if not isinstance(supplied, dict) or set(supplied) - sources.keys():
        raise ValueError("material provenance must name current sources")
    # Callers must explicitly identify reconstructed material. Presence of a plan
    # container or a registered POI never establishes historical completeness.
    context["material_provenance"] = {}
    for source in sources:
        label = supplied.get(source, {"origin": "unspecified", "completeness": "unknown"})
        if (not isinstance(label, dict)
                or label.get("origin") not in {"original", "current", "reconstructed", "unspecified"}
                or label.get("completeness") not in {"complete", "excerpt", "unknown"}
                or not isinstance(label.get("note", ""), str)):
            raise ValueError("invalid material provenance")
        context["material_provenance"][source] = dict(label)
    # The namespace changes with ANY review input, not merely the selected quote.
    namespace = body_review_snapshot(context)[:20]
    index = {}
    for source, text in sources.items():
        spans = [(0, len(text))] if text.strip() else []
        if source.startswith("plan:"):
            # Mechanical sentence boundaries only. Never split a condition at a
            # comma, remove a negation, or infer activity status here.
            spans += [(m.start(), m.end()) for m in re.finditer(r"[^\n。！？]+[。！？]?", text)
                      if m.group().strip()]
        for start, end in dict.fromkeys(spans):
            index[f"e:{namespace}:{len(index) + 1}"] = {
                "source": source, "start": start, "end": end}
    context["evidence_index"] = index
    return context


def body_review_matches(receipt, context) -> bool:
    return receipt is not None and receipt.snapshot == body_review_snapshot(context)
