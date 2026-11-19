"""Normalize and aggregate XHS visit-duration facts for canonical POIs."""

from __future__ import annotations

import math
import hashlib
import re
from dataclasses import dataclass, field, replace
from statistics import median
from typing import Any

MIN_VISIT_MINUTES = 15
MAX_VISIT_MINUTES = 480
HIGH_CONFIDENCE_THRESHOLD = 0.85

_CHINESE_DIGITS = {
    "零": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}

_EXPLICIT_DURATION_HINT = re.compile(
    r"(游玩|逛|停留|玩|拍照|打卡|路过|建议|预留|安排|耗时|时长|够|足够|大概|左右)"
)
_MINUTE_PATTERN = re.compile(r"(\d+(?:\.\d+)?)\s*(?:分钟|分|min|mins|minutes)", re.I)
_HOUR_PATTERN = re.compile(r"(\d+(?:\.\d+)?)\s*(?:小时|h|hr|hrs|hour|hours)", re.I)
_RANGE_HOUR_PATTERN = re.compile(
    r"(\d+(?:\.\d+)?|[一二两三四五六七八九十])\s*(?:-|~|到|至)\s*"
    r"(\d+(?:\.\d+)?|[一二两三四五六七八九十])\s*(?:小时|h|hr|hrs)",
    re.I,
)
_RANGE_MINUTE_PATTERN = re.compile(
    r"(\d+(?:\.\d+)?|[一二两三四五六七八九十])\s*(?:-|~|到|至)\s*"
    r"(\d+(?:\.\d+)?|[一二两三四五六七八九十])\s*(?:分钟|分|min|mins)",
    re.I,
)
_CHINESE_HOUR_PATTERN = re.compile(r"([一二两三四五六七八九十])\s*(?:个)?小时")
_CHINESE_MINUTE_PATTERN = re.compile(r"([一二两三四五六七八九十])\s*(?:十)?\s*(?:分钟|分)")

_MAJOR_PLACE_NAME_MARKERS = (
    "景区",
    "风景区",
    "山",
    "峡",
    "谷",
    "湖",
    "古镇",
    "森林",
    "乐园",
    "度假区",
    "动物园",
    "植物园",
)
_SUPPORTED_PLACE_TYPES = {
    "attraction",
    "business_area",
    "market",
    "museum",
    "park",
    "photo_spot",
    "restaurant",
}


@dataclass(frozen=True)
class NormalizedVisitDuration:
    minutes: int
    raw: str
    strategy: str


@dataclass(frozen=True)
class VisitDurationFact:
    minutes: int
    raw: str
    evidence: str
    confidence: float
    source_id: str | None = None
    note_fingerprint: str | None = None
    source_text: str | None = None
    source_title: str = ""
    place_name: str = ""


@dataclass(frozen=True)
class VisitDurationAggregate:
    minutes: int
    confidence: float
    fact_count: int
    source: str = "xhs_median"


REASON_ACCEPTED = "ACCEPTED"
REASON_SOURCE_MISSING = "SOURCE_MISSING"
REASON_QUOTE_NOT_VERBATIM = "QUOTE_NOT_VERBATIM"
REASON_OTHER_OBJECT = "OTHER_OBJECT"
REASON_TRAFFIC_OR_QUEUE = "TRAFFIC_OR_QUEUE"
REASON_NEGATION = "NEGATION"
REASON_PARTIAL_OR_GRANULARITY = "PARTIAL_OR_GRANULARITY"
REASON_CONFIDENCE_TOO_LOW = "CONFIDENCE_TOO_LOW"
REASON_INDEPENDENT_SOURCE_INSUFFICIENT = "INDEPENDENT_SOURCE_INSUFFICIENT"
REASON_AGGREGATE_REJECT = "AGGREGATE_REJECT"
REASON_NOTE_AMBIGUOUS = "NOTE_AMBIGUOUS"
REASON_DUPLICATE_NOTE = "DUPLICATE_NOTE"
REASON_DURATION_PARSE_FAILED = "DURATION_PARSE_FAILED"
REASON_HEADING_SCOPE_FAIL = "HEADING_SCOPE_FAIL"
REASON_MULTI_DURATION = "MULTI_DURATION"
REASON_NON_VISIT_CONTEXT = "NON_VISIT_CONTEXT"
REASON_OBJECT_NOT_BOUND = "OBJECT_NOT_BOUND"
REASON_EMPTY_EVIDENCE = "EMPTY_EVIDENCE"

_NEGATION_MARKERS = re.compile(
    r"不用|不需要|不必|无需|没必要|并非|不是|没有|未能|没逛完|没玩完|不够|不能|不看|只看|只在|不建议|不推荐"
)
_TRAFFIC_QUEUE_MARKERS = re.compile(
    r"排队|等候|等了|车程|船程|交通|通勤|水上巴士|地铁|高铁|大巴|公交|开车|自驾|坐船|乘船|"
    r"导航|返程|往返|单程|上山路|下山路|到达|抵达|每个|每次|一趟|班次"
)
_PARTIAL_MARKERS = re.compile(
    r"拍照|打卡|路过|联票|半天|全天|一天|一整天|一上午|一下午|其中|一部分|局部|登顶|山顶|上山|下山|门口"
)


@dataclass(frozen=True)
class VisitDurationRejection:
    stage: str
    reason_code: str
    source_id: str | None = None
    canonical_place_id: int | None = None
    source_name: str = ""
    canonical_name: str = ""
    minutes: int | None = None
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "reason_code": self.reason_code,
            "source_id": self.source_id,
            "canonical_place_id": self.canonical_place_id,
            "source_name": self.source_name,
            "canonical_name": self.canonical_name,
            "minutes": self.minutes,
            "summary": self.summary,
        }


@dataclass
class VisitDurationObservation:
    candidate_count: int = 0
    accepted_count: int = 0
    bind_alias_attempts: int = 0
    parse_count: int = 0
    rejected: list[VisitDurationRejection] = field(default_factory=list)
    source_canonical_map: list[dict[str, Any]] = field(default_factory=list)

    def to_public_dict(self) -> dict[str, Any]:
        reasons = [item.reason_code for item in self.rejected]
        return {
            "candidate_count": self.candidate_count,
            "accepted_count": self.accepted_count,
            "bind_alias_attempts": self.bind_alias_attempts,
            "parse_count": self.parse_count,
            "reject_reasons": reasons,
            "reject_reason_counts": {
                code: reasons.count(code) for code in dict.fromkeys(reasons)
            },
            "source_canonical_map": list(self.source_canonical_map[:20]),
            "stage_counts": {
                "candidate": self.candidate_count,
                "parse": self.parse_count,
                "bind_alias_attempts": self.bind_alias_attempts,
                "bind_reject": sum(1 for item in self.rejected if item.stage == "bind"),
                "aggregate_reject": sum(1 for item in self.rejected if item.stage == "aggregate"),
                "accepted": self.accepted_count,
            },
        }


def bounded_duration_summary(evidence: str, *, max_len: int = 80) -> str:
    text = evidence or ""
    has_duration = bool(
        _MINUTE_PATTERN.search(text)
        or _HOUR_PATTERN.search(text)
        or _CHINESE_HOUR_PATTERN.search(text)
        or _CHINESE_MINUTE_PATTERN.search(text)
    )
    return f"<text chars={len(text)} duration_like={has_duration}>"[:max_len]


def _non_visit_reason(text: str) -> str | None:
    if not _NON_VISIT.search(text):
        return None
    if _NEGATION_MARKERS.search(text):
        return REASON_NEGATION
    if _TRAFFIC_QUEUE_MARKERS.search(text):
        return REASON_TRAFFIC_OR_QUEUE
    if _PARTIAL_MARKERS.search(text):
        return REASON_PARTIAL_OR_GRANULARITY
    return REASON_NON_VISIT_CONTEXT


def _to_number(value: str) -> float | None:
    value = value.strip()
    try:
        return float(value)
    except ValueError:
        pass
    if value in _CHINESE_DIGITS:
        return float(_CHINESE_DIGITS[value])
    if value.startswith("十") and len(value) > 1 and value[1] in _CHINESE_DIGITS:
        return float(10 + _CHINESE_DIGITS[value[1]])
    if value.endswith("十") and len(value) > 1 and value[0] in _CHINESE_DIGITS:
        return float(_CHINESE_DIGITS[value[0]] * 10)
    return None


def _clamp_minutes(minutes: float) -> int:
    if not math.isfinite(minutes):
        return 0
    return max(MIN_VISIT_MINUTES, min(MAX_VISIT_MINUTES, int(round(minutes))))


def _is_major_place(*, place_name: str = "", place_type: str | None = None) -> bool:
    if place_type in {"park"} and any(marker in place_name for marker in ("森林", "湿地", "植物园", "动物园")):
        return True
    return any(marker in place_name for marker in _MAJOR_PLACE_NAME_MARKERS)


def has_visit_duration_marker_support(
    *, place_name: str = "", place_type: str | None = None
) -> bool:
    """Return whether one strong fact is enough to write canonical duration."""
    if place_type in _SUPPORTED_PLACE_TYPES:
        return True
    return any(marker in place_name for marker in _MAJOR_PLACE_NAME_MARKERS)


def normalize_visit_duration(
    value: Any,
    *,
    place_name: str = "",
    place_type: str | None = None,
    require_duration_hint: bool = False,
) -> NormalizedVisitDuration | None:
    """Normalize a raw visit-duration expression to minutes.

    The function only returns evidence-derived durations. It never fabricates a
    place-type rule default; callers should keep runtime defaults out of
    canonical persistence.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return NormalizedVisitDuration(
            minutes=_clamp_minutes(float(value)),
            raw=str(value),
            strategy="numeric_minutes",
        )
    if isinstance(value, dict):
        raw = (
            value.get("raw")
            or value.get("text")
            or value.get("value")
            or value.get("duration")
            or value.get("visit_duration")
        )
        # Cached/model-provided numbers never supply source semantics.
        value = raw
    if not isinstance(value, str):
        return None

    raw = value.strip()
    if not raw:
        return None
    compact = re.sub(r"\s+", "", raw)
    if require_duration_hint and not _EXPLICIT_DURATION_HINT.search(compact):
        return None

    if any(token in compact for token in ("一个半小时", "一小时半", "1个半小时", "1小时半")):
        return NormalizedVisitDuration(minutes=90, raw=raw, strategy="one_and_half_hour")

    match = _RANGE_HOUR_PATTERN.search(compact)
    if match:
        left = _to_number(match.group(1))
        right = _to_number(match.group(2))
        if left is not None and right is not None:
            return NormalizedVisitDuration(
                minutes=_clamp_minutes((left + right) / 2 * 60),
                raw=raw,
                strategy="hour_range_median",
            )

    match = _RANGE_MINUTE_PATTERN.search(compact)
    if match:
        left = _to_number(match.group(1))
        right = _to_number(match.group(2))
        if left is not None and right is not None:
            return NormalizedVisitDuration(
                minutes=_clamp_minutes((left + right) / 2),
                raw=raw,
                strategy="minute_range_median",
            )

    match = _HOUR_PATTERN.search(compact)
    if match:
        return NormalizedVisitDuration(
            minutes=_clamp_minutes(float(match.group(1)) * 60),
            raw=raw,
            strategy="hour",
        )
    match = _CHINESE_HOUR_PATTERN.search(compact)
    if match:
        number = _to_number(match.group(1))
        if number is not None:
            return NormalizedVisitDuration(
                minutes=_clamp_minutes(number * 60),
                raw=raw,
                strategy="chinese_hour",
            )

    match = _MINUTE_PATTERN.search(compact)
    if match:
        return NormalizedVisitDuration(
            minutes=_clamp_minutes(float(match.group(1))),
            raw=raw,
            strategy="minute",
        )
    match = _CHINESE_MINUTE_PATTERN.search(compact)
    if match:
        number = _to_number(match.group(1))
        if number is not None:
            suffix_ten = "十" in match.group(0)
            return NormalizedVisitDuration(
                minutes=_clamp_minutes(
                    number * 10
                    if suffix_ten and match.group(1) != "十"
                    else number
                ),
                raw=raw,
                strategy="chinese_minute",
            )

    if any(token in compact for token in ("半小时", "半个小时")):
        return NormalizedVisitDuration(minutes=30, raw=raw, strategy="half_hour")
    return None


_NON_VISIT = re.compile(
    r"不用|不需要|不必|无需|没必要|并非|不是|没有|未能|没逛完|没玩完|不够|不能|不看|只看|只在|不建议|不推荐|"
    r"排队|等候|等了|车程|船程|交通|通勤|水上巴士|地铁|高铁|大巴|公交|开车|自驾|坐船|乘船|"
    r"导航|返程|往返|单程|上山路|下山路|到达|抵达|每个|每次|一趟|班次|"
    r"吃饭|午饭|晚饭|休息|营业|开放时间|停运|检票|拍照|打卡|路过|联票|"
    r"半天|全天|一天|一整天|一上午|一下午|其中|一部分|局部|登顶|山顶|上山|下山|门口"
)
_WHOLE_VISIT = re.compile(
    r"游玩|游览|参观|逛完|玩完|走完|全程|(?:建议|预留|留).{0,12}(?:慢慢看|逛|玩)|"
    r"(?:逛|玩|停留).{0,8}(?:小时|分钟|h\b|min\b)|(?:小时|分钟|h\b|min\b).{0,8}(?:逛|玩|看完)"
)

# Only grammatical visit/time modifiers may connect the object and duration.
# Arbitrary intervening text would let a neighbour or one child become the
# subject of the predicate while merely mentioning the target elsewhere.
_VISIT_MODIFIER = (
    r"(?:建议|预留|安排|留|约需|需要|大概|大约|约|也能|可以|能|慢慢看|慢慢|"
    r"完整|全部|总共|全程|共|时间|时长|游玩|游览|参观|逛完|玩完|走完|看完|"
    r"逛|玩|停留|徒步|步行|穿越|耗时|为|是|需|用|了|够|足够|左右|也|就|花|"
    r"刚刚好|刚好|正好|[:：])*"
)
_DURATION_EXPRESSION = (
    r"(?:一个半小时|一小时半|1个半小时|1小时半|半个?小时|"
    r"(?:\d+(?:\.\d+)?|[一二两三四五六七八九十]+)"
    r"(?:(?:-|~|到|至)(?:\d+(?:\.\d+)?|[一二两三四五六七八九十]+))?"
    r"(?:个?小时|分钟|分|hours?|hrs?|h|minutes?|mins?))"
)
# Adjacent labeled visit lines under an exact POI section heading.
_DURATION_LABEL = r"(?:游玩时间|游玩时长|建议时长|时长)"
_NUMBERED_POI_HEADING = re.compile(r"^(?P<index>\d+)[.、]\s*(?P<identity>.+)$")
_HEADING_RANGE_OR_CHAIN = re.compile(r"附近|从.+到.+|→|->|[+＋]|联游|串联")


def original_source_title(raw_json: Any) -> str:
    """Read only the provider envelope stored on travel_raw_item.

    Neither a caller's reconstructed title nor travel_content.title
    (generated by Extract) is source authority.
    """
    import json

    if isinstance(raw_json, str):
        try:
            raw_json = json.loads(raw_json)
        except json.JSONDecodeError:
            return ""
    if not isinstance(raw_json, dict):
        return ""
    note_card = raw_json.get("note_card")
    candidates = [raw_json]
    if isinstance(note_card, dict):
        candidates.insert(0, note_card)
    for container in candidates:
        for key in ("title", "display_title", "note_title"):
            val = container.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return ""


def _bound_complete_visit(context: str, name: str, *, heading: bool) -> bool:
    """Match the whole object to the visit predicate in its own clause."""
    # Labels and ordinary first-person introductions do not introduce another
    # object. No arbitrary words (including child/nearby names) are skipped.
    intro = r"(?:(?:今天|我们|我|建议|可以|在|去|去了|给|为)|[^,，:：]+[:：])*"
    visit = _VISIT_MODIFIER + _DURATION_EXPRESSION + _VISIT_MODIFIER
    clauses = re.split(r"[,，;；]", context)
    comfort = r"(?:不累|很舒服|很轻松|刚刚好|刚好|正好|很合适)+"
    for index, clause in enumerate(clauses):
        clause = clause.strip("。！!？? ")
        named = re.fullmatch(intro + re.escape(name) + visit, clause, re.I)
        verb_object = re.fullmatch(
            r"(?:建议)?(?:游玩|游览|参观|逛完|玩完|走完)" + re.escape(name) + visit,
            clause, re.I,
        )
        reversed_object = re.fullmatch(visit + re.escape(name), clause, re.I)
        labeled = re.sub(
            r"^[▪▫•·*#\ufe0f]*" + _DURATION_LABEL + r"[:：]",
            "",
            clause,
        )
        unnamed = (heading and index == 0 and re.fullmatch(visit, labeled, re.I)
                   and all(re.fullmatch(comfort, tail, re.I) for tail in clauses[1:]))
        # Heading scope only applies to a genuinely unnamed visit clause; a
        # named different object cannot be erased by the heading.
        if any((named, verb_object, reversed_object, unnamed)) and _WHOLE_VISIT.search(clause):
            return True
    return False


def _contained_complete_route(context: str, name: str) -> bool:
    """The duration must finish the chain inside the named container."""
    match = re.fullmatch(
        re.escape(name) + r"(?:[（(](?P<parent>[^（）()]+)[）)]|"
        r"(?:园内|馆内|经典路线|游览路线|徒步路线)[:：](?P<marked>.+))",
        context,
    )
    if not match:
        return False
    body = match.group('parent') or match.group('marked')
    # Split at the explicit whole-route duration predicate. The left-hand
    # chain cannot borrow a duration from a named neighbour on the right.
    completion = re.search(r"全程|完整|全部|逛完|走完|玩完|游玩|游览|参观", body)
    if not completion:
        return False
    chain = body[:completion.start()].rstrip(',，;；')
    duration = body[completion.start():]
    return bool(re.search(r"→|->|、|/|[+＋]", chain)) and bool(re.fullmatch(
        _VISIT_MODIFIER + _DURATION_EXPRESSION + _VISIT_MODIFIER, duration, re.I,
    )) and not re.search(r"[,，;；]", chain)


def _single_object_guide_scope(source_text: str, evidence: str, name: str) -> bool:
    """Bind one whole-object photo/visit guide, validating every original line.

    Exact topic and invitation identify the object; they never erase a later
    unknown scope. Only complete typed descriptions, observations, transport
    metadata and ancillary advice are admitted. This intentionally does not
    attempt to authorize arbitrary prose or infer subjects from punctuation.
    """
    lines = [line.strip() for line in source_text.splitlines() if line.strip()]
    label = lambda line: re.sub(r"^[^\u4e00-\u9fffA-Za-z0-9]+", "", line).strip(' ：:')
    matches = lambda pattern, line: bool(re.fullmatch(pattern, line))
    invitation = (r"(?:就|推荐|欢迎|可以)?来" + re.escape(name)
                  + r"(?:逛逛|游玩|参观|躲一躲)(?:吧|呀)?[！!。]?")
    topic = r"(?:#[\u4e00-\u9fffA-Za-z0-9]+(?:\[话题\])?#\s*)+"
    if not re.search(r"#" + re.escape(name) + r"(?:\[话题\])?#", source_text):
        return False
    if not any(matches(invitation, line) for line in lines):
        return False
    if re.search(r"DAY\s*\d|第[一二三四五六七八九十\d]+天|行程(?:推荐|安排)|"
                 r"联游|串联|路线|→|->", source_text, re.I):
        return False

    guide = r"(?:游玩(?:小)?攻略|游玩指南|游览攻略|参观攻略)"
    guides = [i for i, line in enumerate(lines) if re.search(guide, line)]
    if len(guides) != 1:
        return False
    index = guides[0]
    if not matches(guide, label(lines[index])) or index + 1 >= len(lines):
        return False
    duration_line = lines[index + 1]
    if (evidence not in duration_line
            or not re.match(r"(?:游玩时长|建议时长|时长)[:：]", label(duration_line))):
        return False
    duration_clause = re.sub(r"^(?:游玩时长|建议时长|时长)[:：]", "", label(duration_line))
    if not matches(_VISIT_MODIFIER + _DURATION_EXPRESSION + _VISIT_MODIFIER
                   + r"(?:[，,](?:不累|很舒服|很轻松|刚刚好|刚好|正好|很合适)+)*[。！!]?",
                   re.sub(r"\s+", "", duration_clause)):
        return False
    photo_heading = r"(?:保姆级|推荐|精选|最佳)?(?:出片|拍照|摄影)机位(?:[（(](?:新手必冲|推荐|参考)[）)])?"
    photos = [i for i, line in enumerate(lines[:index]) if matches(photo_heading, label(line))]
    if len(photos) != 1 or photos[0] + 1 == index:
        return False
    photo_index = photos[0]

    # These are grammatical environmental/physical predicates, not POI names.
    # No arbitrary prefix, suffix or punctuation can introduce another subject.
    # All opaque noun fields share one semantic guard, including topic
    # prefixes. Conservative rejection is preferable to interpreting an
    # unknown noun as an instruction, duration, itinerary or partial visit.
    nominal_semantics = (
        r"[玩逛游览参观看去来走到进出访停留登爬乘坐拍打卡待步逗驻跑]"
        r"|[只仅独单限局部不没无非未勿别否]"
        r"|建议|推荐|专门|其中|前往|行程|路线|联游|串联|体验|穿过|经过"
        r"|选择|目的|重点|对象|替代|取消|需要|安排"
        r"|[零〇一二两三四五六七八九十百千万半壹贰叁肆伍陆柒捌玖拾佰仟\d]"
        r"|[时分秒刻钟天日夜早晚午周月年晨季点]|时间|整|全|礼拜|星期"
        r"|DAY|min|hour|only|partial"
    )
    nominal = r"(?:(?!" + nominal_semantics + r")[\u4e00-\u9fff])"
    safe_nominal = lambda text: bool(re.fullmatch(nominal + r"{1,16}", text, re.I))

    def safe_topic(text: str) -> bool:
        # Complete tourism themes have no visit predicate or numeric claim.
        # Their remaining geographic noun uses the very same slot guard.
        theme = re.fullmatch(r"(?P<region>[\u4e00-\u9fff]*?)(?:旅游|小众打卡地|周末游|"
                             r"周边游|散心好去处)", text)
        if theme:
            return not theme['region'] or safe_nominal(theme['region'])
        if text in ('治愈系风景', '周末逃离城市计划'):
            return True
        return safe_nominal(text)
    descriptions = [
        r"当我认为(?:国内|这里的|这些)景区的审美(?:已经)?(?:完蛋了|很糟糕)的时候[….]+",
        r"藏在" + nominal + r"{1,12}山脚下、" + nominal + r"{1,12}江边的(?:百年)?古村",
        r"青山环绕、古树成荫、老屋错落",
        r"满眼(?:都)?是(?:治愈的)?(?:绿|绿色)[🍃🌿]?",
        r"不同于(?:商业化|热闹|喧闹)的(?:古镇|街区)",
        r"这里没有(?:喧闹的)?叫卖[，,]人(?:少|不多)(?:又|而且)?(?:安静|清静)",
        r"(?:青石板|石板)?小路蜿蜒在(?:山林|林间|古街)(?:之间|里|中)?",
        r"(?:百年)?(?:" + nominal + r"{1,5}树|树木)遮(?:天蔽日|荫|阴)",
        r"(?:阳光|光线|日光)(?:透过|穿过)枝叶(?:缝隙)?(?:洒下来|洒落|照下来|照落)",
        r"光影(?:斑驳|柔和)[，,]氛围感(?:直接)?(?:拉满|很好)",
        r"(?:老旧的)?(?:吊脚楼|老屋|建筑)依山而建",
        r"(?:青苔|绿植)爬满(?:石阶|台阶)(?:与|和)(?:院墙|墙面)",
        r"(?:新旧|新老)?建筑(?:温柔)?相融",
        r"走在(?:巷子|小巷|林间|古街)里[，,]空气(?:都)?是(?:清新的|清新)",
        r"(?:山风|江水|溪水|鸟鸣|树影|花香|阳光|绿意)(?:、(?:山风|江水|溪水|鸟鸣|树影|花香|阳光|绿意))+",
        r"是" + nominal + r"{1,8}(?:最)?适合发呆的(?:小众)?秘境[🍃🌿]?",
        r"在山野绿意里[，,]治愈所有疲惫[🍃🌿]?",
    ]
    observation = (
        r"[▪▫•·*\ufe0f]+(?:"
        r"(?:村口)?(?:古树)?大道[:：]参天大树排成林荫道[，,]森系大片随手拍|"
        r"林间(?:石阶)?(?:小巷|小路)[:：]青苔老屋[+＋]绿植[，,]古风、清新风(?:都)?适配|"
        r"林间(?:石阶)?(?:小巷|小路)[:：]树木环绕[，,]景色清新|"
        r"临江步道[:：]直面" + nominal + r"{1,12}江[，,]吹江风看远山[，,]氛围感绝美|"
        r"温泉小院[:：]绿植环绕的庭院[，,]温柔又松弛)"
    )
    clothing = (r"[▪▫•·*\ufe0f]+穿搭[:：](?:浅色系|素色长裙|棉麻衣服)"
                r"(?:、(?:浅色系|素色长裙|棉麻衣服))*[，,](?:超级|很)?出片")
    activity = r"(?:逛古街|拍(?:森系)?照片|江边散步|泡(?:山野)?温泉)"
    experience = r"[▪▫•·*\ufe0f]+体验[:：]" + activity + r"(?:、" + activity + r")*"
    transport = [
        r"离主城(?:仅)?" + _DURATION_EXPRESSION + r"车程",
        r"✅?自驾[:：](?:主城)?出发" + _DURATION_EXPRESSION + r"(?:左右)?[，,]有停车场(?:[，,]停车方便)?",
        r"✅?公共交通[:：](?:地铁下车|\d+号线" + nominal + r"{1,8}站下车)[，,]打车"
        + _DURATION_EXPRESSION + r"直达",
    ]
    safety = (
        r"(?:(?:山林)?蚊虫(?:较)?多[，,]"
        r"(?:一定要|建议|记得|提前)?(?:带|备好|准备)(?:驱蚊水|驱蚊剂|防蚊喷雾)|"
        r"(?:路面|道路)(?:多|较多|有)(?:石阶|台阶)(?:小路)?[，,]"
        r"(?:建议|记得)?穿(?:舒服|舒适|防滑)的?(?:鞋|鞋子)|"
        r"(?:(?:尽量|建议)?(?:上午|下午|傍晚)去[！!，,])?"
        r"(?:光线|阳光)(?:较)?柔和(?:不暴晒)?[，,]拍照(?:巨好看|很好看|效果好))"
    )
    audience = (r"(?:周末散心|亲子出游|闺蜜拍照|情侣约会|朋友出游)"
                r"(?:、(?:周末散心|亲子出游|闺蜜拍照|情侣约会|朋友出游))*"
                r"(?:都)?(?:很|超)?(?:合适|适合)")
    conditional = r"如果(?:觉得|感到)(?:生活浮躁|疲惫|疲倦|压力大)"

    tips_open = False
    tips_seen = False
    for i, line in enumerate(lines):
        if i == index + 1:
            # Complete line grammar was checked; caller also validates the
            # numeric clause and preserves non-visit/negation safeguards.
            continue
        if i in (photo_index, index):
            continue  # complete header grammar was checked above
        if photo_index < i < index:
            if not matches(observation, line):
                return False
            continue
        # Even metadata/scene labels do not discard partial or redirected visits.
        # Complete typed lines are checked below rather than a source-wide
        # non-visit-word search, which would mistake legitimate traffic for a visit.
        if matches(r"(?:游玩(?:小)?贴士|注意事项)", label(line)):
            if tips_seen or i <= index + 1:
                return False
            tips_open = tips_seen = True
            continue
        numbered = re.fullmatch(r"\d+[.、]\s*(.+)", line)
        if numbered:
            if not tips_open or not (matches(safety, numbered[1]) or matches(audience, numbered[1])):
                return False
            continue
        if matches(invitation, line) or matches(conditional, line):
            tips_open = False
            continue
        if matches(topic, line):
            # Validate every complete topic field, not just known substrings.
            fields = re.findall(r"#([\u4e00-\u9fffA-Za-z0-9]+)(?:\[话题\])?#", line)
            if not all(safe_topic(field) for field in fields):
                return False
            tips_open = False
            continue
        if tips_open:
            return False
        if matches(clothing, line) or matches(experience, line):
            if i <= index + 1:
                return False
            continue
        if matches(r"(?:出行|交通)指南", label(line)):
            if i <= index + 1:
                return False
            continue
        if any(matches(pattern, line) for pattern in descriptions + transport):
            continue
        return False  # every unknown prose/object/time scope fails closed
    return True


def _complete_poi_section_identity(line: str, name: str) -> bool:
    """Exact POI section heading: the bare name, or a numbered complete identity.

    Adjacent only. Numbered form is ``5. 白象居`` / ``5、白象居``. Extra tail
    range, nearby, from-to, or chain text is not a complete single-target
    identity. This does not skip intervening non-target lines.
    """
    heading = (line or "").strip().strip("# ：:")
    target = (name or "").strip()
    if not heading or not target:
        return False
    if heading == target:
        return True
    numbered = _NUMBERED_POI_HEADING.fullmatch(heading)
    if numbered is None:
        return False
    identity = numbered.group("identity").strip()
    if identity != target:
        return False
    return _HEADING_RANGE_OR_CHAIN.search(heading) is None


def explain_visit_evidence(evidence: str, *, place_name: str = "",
                           source_text: str | None = None,
                           source_title: str = "") -> tuple[bool, str]:
    """Same admission as applicable_visit_evidence, with a stable reject reason."""
    if not evidence or not place_name:
        return False, REASON_EMPTY_EVIDENCE
    non_visit = _non_visit_reason(evidence)
    if non_visit:
        return False, non_visit
    if source_text is not None:
        if not source_text:
            return False, REASON_SOURCE_MISSING
        if evidence not in source_text:
            return False, REASON_QUOTE_NOT_VERBATIM
    semantic_context = evidence
    if source_text is not None:
        start = source_text.find(evidence)
        if source_text.find(evidence, start + 1) >= 0:
            return False, REASON_QUOTE_NOT_VERBATIM
        left = max(source_text.rfind(mark, 0, start) for mark in ("。", "！", "？", "\n")) + 1
        stops = [source_text.find(mark, start + len(evidence)) for mark in ("。", "！", "？", "\n")]
        right = min([stop for stop in stops if stop >= 0], default=len(source_text))
        semantic_context = source_text[left:right]
        context_reason = _non_visit_reason(semantic_context)
        if context_reason:
            return False, context_reason
    compact = re.sub(r"\s+", "", semantic_context)
    name = re.sub(r"\s+", "", place_name)
    heading = source_title.strip() == place_name.strip()
    if source_text is not None:
        before = source_text[:source_text.find(evidence)]
        lines = [line.strip().strip("# ：:") for line in before.splitlines() if line.strip()]
        if lines:
            heading = _complete_poi_section_identity(lines[-1], place_name)
        if not heading:
            heading = _single_object_guide_scope(source_text, evidence, name)
    if not _WHOLE_VISIT.search(semantic_context):
        return False, REASON_OBJECT_NOT_BOUND
    if len(list(re.finditer(
        r"(?:\d+(?:\.\d+)?|[一二两三四五六七八九十半]+)\s*(?:分钟|小时|h\b|min\b)", compact, re.I
    ))) > 1:
        return False, REASON_MULTI_DURATION
    if re.search(r"→|->|、|/|[+＋]|联游|串联|(?:和|与|及).+(?:游玩|游览|逛)", compact):
        if _contained_complete_route(compact, name):
            return True, REASON_ACCEPTED
        return False, REASON_OTHER_OBJECT
    if re.search(r"从.+到.+|到.+(?:需要|要|步行)|只.{0,8}(?:分钟|小时)", compact):
        return False, REASON_PARTIAL_OR_GRANULARITY
    if _bound_complete_visit(compact, name, heading=heading):
        return True, REASON_ACCEPTED
    if heading is False:
        return False, REASON_HEADING_SCOPE_FAIL
    return False, REASON_OBJECT_NOT_BOUND


def applicable_visit_evidence(evidence: str, *, place_name: str = "",
                              source_text: str | None = None,
                              source_title: str = "") -> bool:
    """Confirm whole-object/time semantics; no verdict/name whitelist inputs.

    Unknown note scope fails closed. A quoted complete visit must name its
    object, have an exact named or numbered POI section heading, or bind to a
    structured single-object photo/visit guide in the original note. Chains need
    structural containment, never lexical guesses about parks/mountains.
    """
    ok, _reason = explain_visit_evidence(
        evidence,
        place_name=place_name,
        source_text=source_text,
        source_title=source_title,
    )
    return ok


def fact_from_value(
    value: Any,
    *,
    confidence: float,
    place_name: str = "",
    place_type: str | None = None,
    evidence: str = "",
    require_duration_hint: bool = False,
    source_id: str | None = None,
    source_text: str | None = None,
    source_title: str = "",
) -> VisitDurationFact | None:
    original = evidence
    if isinstance(value, dict):
        original = original or str(value.get("evidence") or value.get("evidence_text") or "")
    elif isinstance(value, str):
        original = original or value
    if not applicable_visit_evidence(original, place_name=place_name,
                                    source_text=source_text, source_title=source_title):
        return None
    # Parse the source excerpt, not the model's raw/provided/cache value.
    normalized = normalize_visit_duration(
        original,
        place_name=place_name,
        place_type=place_type,
        require_duration_hint=require_duration_hint,
    )
    if normalized is None:
        return None
    return VisitDurationFact(
        minutes=normalized.minutes,
        raw=str(value.get("raw") or original) if isinstance(value, dict) else str(value),
        evidence=original,
        confidence=max(0.0, min(1.0, float(confidence))),
        source_id=source_id,
        note_fingerprint=hashlib.sha256(
            re.sub(r"\s+", "", source_text or original).encode()).hexdigest(),
        source_text=source_text,
        source_title=source_title,
        place_name=place_name,
    )


def aggregate_visit_duration_facts(
    facts: list[VisitDurationFact],
    *,
    place_name: str = "",
    place_type: str | None = None,
    observation: VisitDurationObservation | None = None,
) -> VisitDurationAggregate | None:
    checked: list[VisitDurationFact] = []
    note_values: dict[str, set[int]] = {}
    for fact in facts:
        ok, reason = explain_visit_evidence(
            fact.evidence,
            place_name=fact.place_name or place_name,
            source_text=fact.source_text,
            source_title=fact.source_title,
        )
        if not ok:
            if observation is not None:
                observation.rejected.append(VisitDurationRejection(
                    stage="aggregate",
                    reason_code=reason,
                    source_id=fact.source_id,
                    source_name=fact.place_name,
                    canonical_name=place_name,
                    minutes=fact.minutes,
                    summary=bounded_duration_summary(fact.evidence),
                ))
            continue
        normalized = normalize_visit_duration(fact.evidence)
        if normalized is None:
            if observation is not None:
                observation.rejected.append(VisitDurationRejection(
                    stage="aggregate",
                    reason_code=REASON_DURATION_PARSE_FAILED,
                    source_id=fact.source_id,
                    source_name=fact.place_name,
                    canonical_name=place_name,
                    summary=bounded_duration_summary(fact.evidence),
                ))
            continue
        fact = replace(fact, minutes=normalized.minutes)
        checked.append(fact)
        note_values.setdefault(fact.source_id or "unknown_source", set()).add(fact.minutes)
    ambiguous_notes = {key for key, values in note_values.items() if len(values) > 1}
    independent: dict[str, VisitDurationFact] = {}
    seen_notes: set[str] = set()
    for fact in sorted(checked, key=lambda item: -item.confidence):
        if not (MIN_VISIT_MINUTES <= fact.minutes <= MAX_VISIT_MINUTES):
            if observation is not None:
                observation.rejected.append(VisitDurationRejection(
                    stage="aggregate",
                    reason_code=REASON_DURATION_PARSE_FAILED,
                    source_id=fact.source_id,
                    source_name=fact.place_name,
                    canonical_name=place_name,
                    minutes=fact.minutes,
                    summary=bounded_duration_summary(fact.evidence),
                ))
            continue
        ok, reason = explain_visit_evidence(
            fact.evidence,
            place_name=fact.place_name or place_name,
            source_text=fact.source_text,
            source_title=fact.source_title,
        )
        if not ok:
            if observation is not None:
                observation.rejected.append(VisitDurationRejection(
                    stage="aggregate",
                    reason_code=reason,
                    source_id=fact.source_id,
                    source_name=fact.place_name,
                    canonical_name=place_name,
                    minutes=fact.minutes,
                    summary=bounded_duration_summary(fact.evidence),
                ))
            continue
        key = fact.source_id or "unknown_source"
        if key in ambiguous_notes:
            if observation is not None:
                observation.rejected.append(VisitDurationRejection(
                    stage="aggregate",
                    reason_code=REASON_NOTE_AMBIGUOUS,
                    source_id=fact.source_id,
                    source_name=fact.place_name,
                    canonical_name=place_name,
                    minutes=fact.minutes,
                    summary=bounded_duration_summary(fact.evidence),
                ))
            continue
        if key in independent or (fact.note_fingerprint and fact.note_fingerprint in seen_notes):
            if observation is not None:
                observation.rejected.append(VisitDurationRejection(
                    stage="aggregate",
                    reason_code=REASON_DUPLICATE_NOTE,
                    source_id=fact.source_id,
                    source_name=fact.place_name,
                    canonical_name=place_name,
                    minutes=fact.minutes,
                    summary=bounded_duration_summary(fact.evidence),
                ))
            continue
        independent[key] = fact
        if fact.note_fingerprint:
            seen_notes.add(fact.note_fingerprint)
    valid = list(independent.values())
    if not valid:
        if observation is not None:
            observation.accepted_count = 0
        return None

    eligible = len(valid) >= 2 or (
        len(valid) == 1
        and valid[0].confidence >= HIGH_CONFIDENCE_THRESHOLD
        and has_visit_duration_marker_support(place_name=place_name, place_type=place_type)
    )
    if not eligible:
        if observation is not None:
            observation.accepted_count = 0
            for fact in valid:
                observation.rejected.append(VisitDurationRejection(
                    stage="aggregate",
                    reason_code=(
                        REASON_CONFIDENCE_TOO_LOW
                        if fact.confidence < HIGH_CONFIDENCE_THRESHOLD
                        else REASON_INDEPENDENT_SOURCE_INSUFFICIENT
                    ),
                    source_id=fact.source_id,
                    source_name=fact.place_name,
                    canonical_name=place_name,
                    minutes=fact.minutes,
                    summary=bounded_duration_summary(fact.evidence),
                ))
        return None

    minutes = int(round(median([fact.minutes for fact in valid])))
    avg_confidence = sum(fact.confidence for fact in valid) / len(valid)
    confidence = min(0.95, max(0.55, avg_confidence if len(valid) == 1 else avg_confidence + 0.05))
    if observation is not None:
        observation.accepted_count = len(valid)
    return VisitDurationAggregate(
        minutes=minutes,
        confidence=round(confidence, 2),
        fact_count=len(valid),
    )


async def load_canonical_duration_facts(session, *, canonical_ids: list[int] | None = None,
                                       city: str | None = None) -> dict[int, dict]:
    """One batch through summary source identity, shared by refresh/retrieval.

    Original raw rows supply body and title; model content titles and missing
    original bodies cannot supply scope. Note copies are folded by original
    body fingerprint; absent note identity cannot create independent samples.
    Tombstones supply only exact historical object names, never facts/children
    of their own. Confirmed same-object aliases are read only for a genuinely
    linked same-city source, excluding other active owners of the alias.
    """
    import json
    from sqlalchemy import text
    if canonical_ids == []:
        return {}
    scope = ""
    params = {}
    if canonical_ids is not None:
        scope += " AND canonical.place_id = ANY(CAST(:ids AS bigint[]))"
        params["ids"] = sorted(set(canonical_ids))
    if city:
        scope += " AND canonical.city = :city"
        params["city"] = city
    rows = (await session.execute(text("""
        SELECT canonical.place_id AS canonical_place_id, canonical.canonical_name,
               canonical.place_type, source.id AS source_place_id, source.name AS source_name,
               fact.fact_value, fact.confidence, fact.source_id, note.content_text, note.raw_json,
               ARRAY(SELECT old.name FROM travel_place old
                     WHERE old.merged_into_place_id = source.id AND old.city = source.city) AS old_names,
               ARRAY(SELECT alias.alias_name FROM travel_place_alias alias
                     WHERE alias.canonical_place_id = canonical.place_id
                       AND source.city = canonical.city
                       AND alias.status = 'confirmed' AND alias.relation_type = 'alias_same'
                       AND NOT EXISTS (
                           SELECT 1 FROM travel_place_alias other_alias
                           JOIN travel_canonical_place other ON other.place_id = other_alias.canonical_place_id
                           WHERE other.city = canonical.city AND other.is_active = TRUE
                             AND other.place_id <> canonical.place_id
                             AND other_alias.normalized_alias = alias.normalized_alias
                             AND other_alias.status = 'confirmed'
                             AND other_alias.relation_type = 'alias_same'
                       )) AS confirmed_alias_names
        FROM travel_canonical_place canonical
        JOIN travel_place_summary summary ON summary.canonical_place_id = canonical.place_id
        JOIN travel_place source ON source.id = summary.place_id
        JOIN travel_place_fact fact ON fact.place_id = summary.place_id
             AND fact.source_platform = 'xhs' AND fact.fact_type = 'visit_duration'
        LEFT JOIN LATERAL (
            SELECT raw.raw_text AS content_text, raw.raw_json
            FROM travel_raw_item raw
            WHERE raw.source_platform = fact.source_platform
              AND raw.source_id = fact.source_id
              AND raw.city = source.city
            ORDER BY raw.id DESC LIMIT 1
        ) note ON TRUE
        WHERE canonical.is_active = TRUE AND source.merged_into_place_id IS NULL
    """ + scope + " ORDER BY canonical.place_id, fact.id"), params)).all()
    buckets: dict[int, dict] = {}
    for row in rows:
        bucket = buckets.setdefault(int(row.canonical_place_id), {
            "name": str(row.canonical_name), "place_type": str(row.place_type), "facts": [],
            "arrangements": [], "observation": VisitDurationObservation()})
        observation: VisitDurationObservation = bucket["observation"]
        value = row.fact_value
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                pass
        evidence = str(value.get("evidence") or value.get("evidence_text") or "") if isinstance(value, dict) else ""
        from src.pipeline.note_object_scope import arrangement_receipts
        source_names = list(dict.fromkeys([row.source_name, *(row.old_names or []),
            row.canonical_name, *(getattr(row, "confirmed_alias_names", []) or [])]))
        observation.candidate_count += 1
        observation.parse_count += 1
        observation.source_canonical_map.append({
            "source_place_id": getattr(row, "source_place_id", None),
            "canonical_place_id": int(row.canonical_place_id),
            "source_id": row.source_id,
            "source_name": str(row.source_name),
            "canonical_name": str(row.canonical_name),
        })
        for source_name in source_names:
            receipts = arrangement_receipts(value, place_name=str(source_name),
                source_text=row.content_text or "", source_title=original_source_title(row.raw_json), source_id=row.source_id)
            if receipts:
                for receipt in receipts:
                    receipt["source_target"] = receipt["whole_target"]
                    receipt["whole_target"] = str(row.canonical_name)
                bucket["arrangements"].extend(receipts)
                break
        bound = None
        last_reason = REASON_OBJECT_NOT_BOUND
        source_text = row.content_text or ""
        source_title = original_source_title(row.raw_json)
        if not evidence:
            last_reason = REASON_EMPTY_EVIDENCE
        elif row.content_text is None:
            last_reason = REASON_SOURCE_MISSING
        for source_name in source_names:
            observation.bind_alias_attempts += 1
            ok, reason = explain_visit_evidence(
                evidence,
                place_name=str(source_name),
                source_text=source_text,
                source_title=source_title,
            )
            last_reason = reason
            if not ok:
                continue
            bound = fact_from_value(
                value, confidence=float(row.confidence or 0),
                place_name=str(source_name), place_type=str(row.place_type),
                evidence=evidence, source_id=row.source_id,
                source_text=source_text, source_title=source_title,
            )
            if bound:
                break
            last_reason = REASON_DURATION_PARSE_FAILED
        if bound:
            bucket["facts"].append(bound)
        else:
            observation.rejected.append(VisitDurationRejection(
                stage="bind",
                reason_code=last_reason,
                source_id=row.source_id,
                canonical_place_id=int(row.canonical_place_id),
                source_name=str(row.source_name),
                canonical_name=str(row.canonical_name),
                summary=bounded_duration_summary(evidence),
            ))
    return buckets
