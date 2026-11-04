"""Bounded structured preference normalization and matching.

Eight UI options share one meaning across ranking, Selector PREFERENCE_MATCH,
and Route coverage. Unknown labels stay unknown. Family is arrangement-only.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import re

from src.agents.schema import CandidatePlace

FOOD = "food"
CITYWALK = "citywalk"
PHOTO = "photo"
NIGHT_VIEW = "night_view"
CULTURE = "culture"
NATURE = "nature"
SHOPPING = "shopping"
FAMILY = "family"

SCORING_DIMENSIONS = frozenset(
    {FOOD, CITYWALK, PHOTO, NIGHT_VIEW, CULTURE, NATURE, SHOPPING}
)

# Whole-label aliases after strip + casefold. No single-character nature keys.
_ALIAS_TO_DIMENSION: dict[str, str] = {
    "美食": FOOD,
    "food": FOOD,
    "吃饭": FOOD,
    "餐厅": FOOD,
    "小吃": FOOD,
    "本地小吃": FOOD,
    "火锅": FOOD,
    "citywalk": CITYWALK,
    "拍照": PHOTO,
    "打卡": PHOTO,
    "photo": PHOTO,
    "夜景": NIGHT_VIEW,
    "夜晚": NIGHT_VIEW,
    "夜间": NIGHT_VIEW,
    "灯光": NIGHT_VIEW,
    "文化历史": CULTURE,
    "人文": CULTURE,
    "文化": CULTURE,
    "历史": CULTURE,
    "culture": CULTURE,
    "history": CULTURE,
    "博物馆": CULTURE,
    "博物院": CULTURE,
    "美术馆": CULTURE,
    "museum": CULTURE,
    "自然风光": NATURE,
    "自然": NATURE,
    "nature": NATURE,
    "公园": NATURE,
    "园林": NATURE,
    "山水": NATURE,
    "湿地": NATURE,
    "森林": NATURE,
    "徒步": NATURE,
    "户外": NATURE,
    "长城": NATURE,
    "亲子": FAMILY,
    "family": FAMILY,
    "带娃": FAMILY,
    "小孩": FAMILY,
    "儿童": FAMILY,
    "孩子": FAMILY,
    "宝宝": FAMILY,
    "遛娃": FAMILY,
    "购物": SHOPPING,
    "逛街": SHOPPING,
    "shopping": SHOPPING,
}

DIMENSION_PLACE_TYPES: dict[str, frozenset[str]] = {
    FOOD: frozenset({"restaurant", "food", "snack", "snack_shop", "market"}),
    CITYWALK: frozenset(
        {
            "business_area",
            "street",
            "historic_district",
            "heritage_district",
            "historic_street",
        }
    ),
    PHOTO: frozenset({"photo_spot", "attraction", "viewpoint"}),
    NIGHT_VIEW: frozenset({"photo_spot", "attraction"}),
    CULTURE: frozenset(
        {
            "museum",
            "heritage_site",
            "heritage_district",
            "historic_building",
            "historic_district",
            "historic_gate",
            "historic_site",
            "historic_street",
            "temple",
            "temple_complex",
        }
    ),
    NATURE: frozenset(
        {
            "park",
            "scenic",
            "nature",
            "trail",
            "mountain",
            "mountain_site",
            "mountain_trail",
            "urban_trail",
            "coast",
            "waterfront",
            "garden",
        }
    ),
    SHOPPING: frozenset({"business_area", "market"}),
}

DIMENSION_TAGS: dict[str, frozenset[str]] = {
    FOOD: frozenset(
        {"美食", "food", "小吃", "本地小吃", "火锅", "餐厅", "snack_shop"}
    ),
    CITYWALK: frozenset(
        {
            "citywalk",
            "城市漫步",
            "街区漫步",
            "街区",
            "historic_district",
            "heritage_district",
            "historic_street",
        }
    ),
    PHOTO: frozenset({"拍照", "打卡", "photo", "观景", "viewpoint"}),
    NIGHT_VIEW: frozenset({"夜景", "夜晚", "夜间", "灯光"}),
    CULTURE: frozenset(
        {
            "文化历史",
            "人文",
            "文化",
            "历史",
            "历史文化",
            "历史建筑",
            "历史遗址",
            "寺庙",
            "宫观",
            "名人故居",
            "故居",
            "古城",
            "古寺",
            "古塔",
            "culture",
            "history",
            "博物馆",
            "博物院",
            "美术馆",
            "museum",
            "heritage_site",
            "heritage_district",
            "historic_building",
            "historic_district",
            "historic_gate",
            "historic_site",
            "historic_street",
            "temple",
            "temple_complex",
        }
    ),
    NATURE: frozenset(
        {
            "自然风光",
            "自然",
            "nature",
            "公园",
            "园林",
            "山水",
            "湿地",
            "森林",
            "徒步",
            "户外",
            "长城",
            "海岸",
            "海滩",
            "海蚀洞",
            "山岳",
            "滨海",
            "coast",
            "mountain",
            "mountain_site",
            "mountain_trail",
            "urban_trail",
        }
    ),
    SHOPPING: frozenset({"购物", "逛街", "shopping", "商业街", "商圈"}),
}

# Retrieval/Route/feasibility food partitions. Cafe/dessert stay partition-only.
FOOD_PARTITION_PLACE_TYPES = frozenset(
    {"restaurant", "food", "cafe", "snack", "snack_shop", "dessert", "market"}
)

# Type-unconditional nature. scenic_area is not in this set.
NATURE_CORE_PLACE_TYPES = {
    "park",
    "scenic",
    "nature",
    "trail",
    "mountain",
    "mountain_site",
    "mountain_trail",
    "urban_trail",
    "coast",
    "waterfront",
    "garden",
}
# Whole-name identity for scenic areas and otherwise untyped attractions.
SCENIC_AREA_NAME_NEARBY_TOKENS = ("附近", "旁边", "周边")
SCENIC_AREA_NAME_NEGATIVE_TOKENS = ("无", "不含", "没有", "不是", "非")
# Positive place identity, not a search for bare 园/山/海. The multi-character
# heads retain existing landscape vocabulary; 竹海 is supported by the accepted
# scenic-area control. A terminal head/destination suffix must describe the
# whole name. No POI ids, full-name allowlist, or reason/warning inference.
SCENIC_AREA_NATURE_NAME = re.compile(
    r"[一-鿿A-Za-z0-9·]*"
    r"(?:公园|园林|湿地|森林|长城|竹海)"
    r"(?:风景区|景区|度假旅游区|旅游区|度假区)?"
)
NON_NATURE_PLACE_TYPES = {
    "restaurant",
    "hotel",
    "food",
    "cafe",
    "snack",
    "snack_shop",
    "dessert",
    "business_area",
    "market",
}

# HEAD notes nature path: two-or-more-character markers only.
LEGACY_NOTES_NATURE_MARKERS = (
    "自然",
    "自然风光",
    "公园",
    "园林",
    "山水",
    "湿地",
    "森林",
    "徒步",
    "户外",
    "长城",
)

FAMILY_NOTE_MARKERS = (
    "亲子",
    "带娃",
    "小孩",
    "小朋友",
    "儿童",
    "孩子",
    "宝宝",
    "遛娃",
)


@dataclass(frozen=True)
class NormalizedPreferences:
    scoring_dimensions: frozenset[str]
    family_arrangement: bool
    unknown_labels: tuple[str, ...]


def _normalize_label(raw: str) -> str:
    return str(raw).strip().casefold()


def normalize_preferences(preferences: Sequence[str] | None) -> NormalizedPreferences:
    """Map whole labels onto the eight structured options."""
    scoring: set[str] = set()
    unknown: list[str] = []
    family_arrangement = False
    for raw in preferences or ():
        key = _normalize_label(raw)
        if not key:
            continue
        dimension = _ALIAS_TO_DIMENSION.get(key)
        if dimension is None:
            unknown.append(str(raw).strip())
            continue
        if dimension == FAMILY:
            family_arrangement = True
            continue
        scoring.add(dimension)
    return NormalizedPreferences(
        scoring_dimensions=frozenset(scoring),
        family_arrangement=family_arrangement,
        unknown_labels=tuple(unknown),
    )


def scoring_dimension_ids(preferences: Sequence[str] | None) -> tuple[str, ...]:
    return tuple(sorted(normalize_preferences(preferences).scoring_dimensions))


def preference_place_type_dimensions(
    preferences: list[str] | Sequence[str] | None,
) -> dict[str, set[str]]:
    """Scoring dimensions to authorized place types. Park aliases are nature."""
    return {
        dimension: set(DIMENSION_PLACE_TYPES[dimension])
        for dimension in scoring_dimension_ids(preferences)
    }


def preferred_place_types(preferences: list[str] | Sequence[str] | None) -> set[str]:
    return {
        place_type
        for place_types in preference_place_type_dimensions(preferences).values()
        for place_type in place_types
    }


def food_preference_active(preferences: Sequence[str] | None) -> bool:
    return FOOD in normalize_preferences(preferences).scoring_dimensions


def is_food_partition_place(place: CandidatePlace) -> bool:
    """Food quota, Route meal partition, and feasibility daytime-activity gate."""
    return (place.place_type or "").strip().lower() in FOOD_PARTITION_PLACE_TYPES


def family_arrangement_active(
    preferences: Sequence[str] | None,
    notes: str = "",
) -> bool:
    if normalize_preferences(preferences).family_arrangement:
        return True
    text = notes or ""
    # The mobile form always writes a children count, including zero. Its
    # companion label and the word 儿童 are not themselves positive intent.
    companion = re.compile(r"【同行情况】[^。\n]*")
    for match in companion.finditer(text):
        counts = re.findall(r"(\d+)\s*(?:位|名|个)?\s*(?:儿童|孩子|小孩|宝宝)", match.group())
        if any(int(count) > 0 for count in counts):
            return True
    text = companion.sub("。", text)
    for clause in re.split(r"[。；;，,\n]", text):
        for marker in FAMILY_NOTE_MARKERS:
            for match in re.finditer(re.escape(marker), clause):
                prefix = clause[:match.start()]
                suffix = clause[match.end():]
                if re.search(r"(?:不|没|无|非|别|未).{0,8}$", prefix):
                    continue
                if re.search(r"(?:^|\D)0\s*(?:位|名|个)?\s*$", prefix):
                    continue
                if re.match(r".{0,3}(?:不同行|不参加|不出行|不带)", suffix):
                    continue
                return True
    return False


def nature_preference_active(
    preferences: Sequence[str] | None,
    notes: str = "",
) -> bool:
    if NATURE in normalize_preferences(preferences).scoring_dimensions:
        return True
    text = notes or ""
    return any(marker in text for marker in LEGACY_NOTES_NATURE_MARKERS)


def _has_explicit_dimension_tag(place: CandidatePlace, dimension: str) -> bool:
    allowed = DIMENSION_TAGS.get(dimension, frozenset())
    tags = {
        str(tag).strip().casefold()
        for tag in (place.category_tags or [])
        if str(tag).strip()
    }
    return bool(tags & allowed)


def _scenic_area_name_has_current_place_nature(name: str) -> bool:
    """Conservative whole-name landscape identity; unsupported names stay unknown.

    Reject a qualified name as a whole so overlapping/compound constituents
    cannot re-enter as independent positives. Positive landscape identity is
    still required after that check; absence of a qualifier is not evidence.
    This bounded naming grammar does not claim general semantic understanding.
    """
    text = str(name or "").strip()
    if any(
        token in text
        for token in (
            *SCENIC_AREA_NAME_NEARBY_TOKENS,
            *SCENIC_AREA_NAME_NEGATIVE_TOKENS,
        )
    ):
        return False
    return SCENIC_AREA_NATURE_NAME.fullmatch(text) is not None


def is_nature_core_place(place: CandidatePlace) -> bool:
    """Only current-place identity supports nature; nearby prose is not evidence."""
    kind = (place.place_type or "").strip().lower()
    if kind in NON_NATURE_PLACE_TYPES:
        return False
    if kind in NATURE_CORE_PLACE_TYPES or _has_explicit_dimension_tag(place, NATURE):
        return True
    return kind in {"attraction", "scenic_area"} and _scenic_area_name_has_current_place_nature(place.name)


# Night identity reads the place name and the reasons already attached to it.
# Photo spots are not night places as a class. Category tags stay on the shared
# matcher only: real pools leave them empty, and existing route tests use a
# 夜景 tag as structured identity rather than as visitor prose.
_EVIDENCE_TEXT_FIELDS = ("cleaned_text", "text", "reason", "evidence")
_ELSEWHERE_MARKERS = ("附近", "周边", "对面", "远处", "旁边")
# Same clause, and only the words just before the hit. 12 characters reaches
# 对面就是夜景 and 旁边另一个地点有夜景 without crossing a comma.
_ELSEWHERE_WINDOW = 12
# Multi-character negations count anywhere in the six characters before the hit.
# Single characters count only when they touch the hit or have one character between.
# Positive idioms are blanked first, so 不错 / 美不胜收 keep their 不.
_POSITIVE_PHRASES = (
    "美不胜收",
    "不容错过",
    "不可错过",
    "不虚此行",
    "不一样",
    "不得不",
    "不错",
    "不输",
    "不愧",
)
_MULTI_NEGATION_MARKERS = (
    "不太适合",
    "不建议",
    "不适合",
    "不推荐",
    "没什么",
    "没有",
    "不能",
    "不让",
)
_MULTI_NEGATION_WINDOW = 6
_SINGLE_NEGATION_MARKERS = ("不", "没", "无", "别")
_POSITIVE_BLANK = "\ue000"
_CLAUSE_BREAK_RE = re.compile(r"[，,。；;！？!?、\n]")
_NIGHT_NAME_PHRASES = ("夜景", "夜游", "夜市", "不夜", "灯光", "灯会")
_NIGHT_EVIDENCE_PHRASES = ("夜景", "亮灯", "夜游", "灯光", "夜晚", "夜间", "入夜")
_NIGHT_GO_RE = re.compile(r"晚上(?:一定|必须|要|才|再|得)?去|夜里去|夜间去")
_NIGHT_TAG_PHRASES = DIMENSION_TAGS[NIGHT_VIEW] | {"夜游"}
_FAMILY_NAME_PHRASES = (
    "自然博物馆",
    "海底世界",
    "动物园",
    "欢乐谷",
    "科技馆",
    "科学馆",
    "水族",
    "儿童",
    "熊猫",
    "游乐",
    "乐园",
    "海洋",
)
_FAMILY_EVIDENCE_PHRASES = ("带娃", "遛娃", "娃能玩", "小朋友", "孩子", "亲子")
_TEA_HOUSE_NAME_PHRASES = ("茶馆", "茶室", "茶楼", "茶艺", "茶社", "茶舍")
_ABSENT = object()


def _top_reason_texts(place: object) -> list[str]:
    """Visitor lines already stored on this place. Warnings are not included.

    CandidatePlace keeps them on top_reasons. PoiSelectionCandidateV11 keeps
    the short excerpts on experience. A v1.0 selector row has neither.
    """
    reasons = getattr(place, "top_reasons", _ABSENT)
    if reasons is _ABSENT:
        items = getattr(place, "experience", None) or []
    else:
        items = reasons or []
    texts: list[str] = []
    for item in items:
        if isinstance(item, str):
            stripped = item.strip()
            if stripped:
                texts.append(stripped)
            continue
        if not isinstance(item, dict):
            continue
        for key in _EVIDENCE_TEXT_FIELDS:
            value = item.get(key)
            if isinstance(value, str):
                stripped = value.strip()
                if stripped:
                    texts.append(stripped)
    return texts


def _clause_prefix(text: str, start: int, window: int) -> str:
    """Characters just before the hit, and only inside its clause."""
    clause_start = 0
    for match in _CLAUSE_BREAK_RE.finditer(text, 0, start):
        clause_start = match.end()
    return text[max(clause_start, start - window):start]


def _phrase_is_local(text: str, start: int) -> bool:
    """False when a nearby-place word in this clause governs the phrase."""
    prefix = _clause_prefix(text, start, _ELSEWHERE_WINDOW)
    return not any(marker in prefix for marker in _ELSEWHERE_MARKERS)


def _blank_positive_phrases(prefix: str) -> str:
    """Keep width so removing 不错 does not pull a farther 不 onto the hit."""
    for phrase in _POSITIVE_PHRASES:
        prefix = prefix.replace(phrase, _POSITIVE_BLANK * len(phrase))
    return prefix


def _clause_text_before(text: str, start: int) -> str:
    clause_start = 0
    for match in _CLAUSE_BREAK_RE.finditer(text, 0, start):
        clause_start = match.end()
    return text[clause_start:start]


def _phrase_is_negated(text: str, start: int) -> bool:
    """True when this hit is cancelled by a negation in the same clause.

    不太适合 / 没有 / 不能 may sit up to six characters ahead.
    不 / 没 / 无 / 别 count only when adjacent, or with one character between.
    """
    prefix = _blank_positive_phrases(_clause_text_before(text, start))
    tail = prefix[-_MULTI_NEGATION_WINDOW:]
    if any(marker in tail for marker in _MULTI_NEGATION_MARKERS):
        return True
    if prefix.endswith(_SINGLE_NEGATION_MARKERS):
        return True
    return len(prefix) >= 2 and prefix[-2] in _SINGLE_NEGATION_MARKERS


def _phrase_counts(text: str, start: int) -> bool:
    return _phrase_is_local(text, start) and not _phrase_is_negated(text, start)


def _texts_have_local_phrase(
    texts: list[str],
    phrases: tuple[str, ...],
    *,
    pattern: re.Pattern[str] | None = None,
) -> bool:
    for text in texts:
        for phrase in phrases:
            start = 0
            while True:
                index = text.find(phrase, start)
                if index < 0:
                    break
                if _phrase_counts(text, index):
                    return True
                start = index + len(phrase)
        if pattern is not None:
            for match in pattern.finditer(text):
                if _phrase_counts(text, match.start()):
                    return True
    return False


def candidate_is_night_place(place: CandidatePlace) -> bool:
    """Night place from its own name or its own visitor lines.

    A photo-spot type is not enough. 附近夜景 / 对面就是夜景 do not count.
    没有夜景 / 不亮灯 do not count. 不错的夜景 still counts.
    The name rule ignores those negation words, so 不夜城 stays a night place.
    """
    name = place.name or ""
    if any(phrase in name for phrase in _NIGHT_NAME_PHRASES):
        return True
    return _texts_have_local_phrase(
        _top_reason_texts(place),
        _NIGHT_EVIDENCE_PHRASES,
        pattern=_NIGHT_GO_RE,
    )


def _night_category_tag(place: CandidatePlace) -> bool:
    folded = " ".join(
        str(tag) for tag in (place.category_tags or []) if str(tag).strip()
    ).casefold()
    return any(phrase.casefold() in folded for phrase in _NIGHT_TAG_PHRASES)


def candidate_is_family_place(place: CandidatePlace) -> bool:
    """A place a child can join, from its name or a local visitor line.

    公园 and 博物馆 alone do not qualify. 儿童乐园 written about a nearby
    place stays on that other place: those name words are not read from prose.
    不适合带孩子 / 不建议带娃 do not count. Name words ignore that negation.
    """
    name = place.name or ""
    if any(phrase in name for phrase in _FAMILY_NAME_PHRASES):
        return True
    return _texts_have_local_phrase(_top_reason_texts(place), _FAMILY_EVIDENCE_PHRASES)


def candidate_is_tea_place(place: CandidatePlace) -> bool:
    """Tea house by type or by a tea-house name. A 茶 character is not enough."""
    if (place.place_type or "").strip().lower() == "tea":
        return True
    name = place.name or ""
    return any(phrase in name for phrase in _TEA_HOUSE_NAME_PHRASES)


def candidate_matches_dimension(candidate: CandidatePlace, dimension: str) -> bool:
    """One identity rule for retrieval, selection, route and prose admission.

    Matching identity never authorizes an external fact or an activity condition.
    Family stays arrangement-only and is not a scoring dimension.
    """
    if dimension not in SCORING_DIMENSIONS:
        return False
    kind = (candidate.place_type or "").strip().lower()
    if dimension == NATURE:
        return is_nature_core_place(candidate)
    if dimension == NIGHT_VIEW:
        return candidate_is_night_place(candidate) or _night_category_tag(candidate)
    return kind in DIMENSION_PLACE_TYPES.get(dimension, ()) or _has_explicit_dimension_tag(candidate, dimension)


def candidate_matches_scoring_preference(
    candidate: CandidatePlace,
    preferences: Sequence[str] | None,
) -> bool:
    dimensions = normalize_preferences(preferences).scoring_dimensions
    return any(
        candidate_matches_dimension(candidate, dimension) for dimension in dimensions
    )


def route_preference_snapshot(route, request) -> str:
    """Internal evidence identity; changing conditions or route invalidates a compromise."""
    import hashlib
    import json
    payload = {"request": request.model_dump(mode="json"),
        "route": route.model_dump(mode="json", exclude={"preference_coverage", "membership_ledger"})}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
        separators=(",", ":")).encode("utf-8")).hexdigest()


def route_preference_compromise(route, dimension: str, request) -> bool:
    """Only pool evidence or proved hard conflicts permits a local compromise.

    Search failure/timeout is unknown. A stale membership report grants nothing.
    """
    ids = sorted({p.place_id for day in route.day_groups for p in day.places})
    for row in route.preference_coverage:
        if (row.get("dimension") != dimension or row.get("route_place_ids") != ids
                or row.get("evidence_complete") is not True
                or row.get("snapshot") != route_preference_snapshot(route, request)):
            continue
        if row.get("status") == "NO_ELIGIBLE_MATCH":
            return row.get("eligible_match_ids") == []
        if row.get("status") == "HARD_CONFLICT":
            applicable = set(row.get("hard_applicable_match_ids", []))
            proved = {item["place_id"] for item in row.get("conflicts", []) if item.get("reason")}
            return bool(row.get("eligible_match_ids")) and applicable <= proved
    return False


def preference_reference_minutes(request) -> int:
    """Request-only comparison capacity; route extensions cannot change it."""
    span = request.daily_span_minutes()
    daily = min(420, span) if span is not None else 420
    return max(1, request.days * max(0, daily - request.rest_window_minutes()))


def preference_contribution_score(contributions, dimensions, reference_minutes, *, commute=0, overload=0):
    """Bounded independent contributions, never a qualification certificate.

    Unrelated visits do not appear in this expression. Real overlapping
    experiences retain both contributions; adding a tag cannot dilute either.
    """
    import math
    if not dimensions:
        return 0.0
    reference = max(1, reference_minutes)
    saturation = reference / len(dimensions)
    benefit = sum(math.sqrt(min(max(0, contributions.get(dim, 0)), saturation) / saturation)
                  for dim in dimensions) / len(dimensions)
    return benefit - (max(0, commute) + max(0, overload)) / reference


def order_preference_candidates(candidates, request, *, initial_candidates=()):
    """Reorder the existing credible pool by shared marginal contribution.

    Input order owns quality/stable ties. This neither filters nor protects
    selected IDs and does not infer traffic facts before route construction.
    """
    from src.agents.route_feasibility import build_visit_profile
    dimensions = scoring_dimension_ids(request.preferences)
    remaining = list(candidates)
    if not dimensions:
        return remaining
    # A genuinely absent dimension is common to this whole pool, never inferred
    # from the prefix or from an individual candidate's lack of a match.
    seeds = list(initial_candidates)
    active = tuple(d for d in dimensions if any(candidate_matches_dimension(p, d) for p in [*seeds, *remaining]))
    if not active:
        return remaining
    reference = preference_reference_minutes(request)
    totals = dict.fromkeys(active, 0.0)
    contribution = {p.place_id: {d: build_visit_profile(p).visit_minutes
                    if candidate_matches_dimension(p, d) else 0 for d in active} for p in remaining}
    ordered, seen = [], set()
    for seed in seeds:
        if seed.place_id in seen:
            continue
        minutes = build_visit_profile(seed).visit_minutes
        for dim in active:
            if candidate_matches_dimension(seed, dim):
                totals[dim] += minutes
        seen.add(seed.place_id)
    while remaining:
        def value(p):
            added = contribution[p.place_id] if p.place_id not in seen else {}
            return preference_contribution_score(
                {d: totals[d] + added.get(d, 0) for d in active}, active, reference)
        index = max(range(len(remaining)), key=lambda i: value(remaining[i]))
        chosen = remaining.pop(index)
        ordered.append(chosen)
        if chosen.place_id not in seen:
            for d in active:
                totals[d] += contribution[chosen.place_id][d]
            seen.add(chosen.place_id)
    return ordered


def route_preference_contributions(route, request, dimensions):
    """Use locked activity minutes; duplicate IDs and night identity alone earn nothing extra."""
    from src.agents.route_feasibility import build_visit_profile
    from src.agents.route_planning import route_night_visit_intervals
    totals = dict.fromkeys(dimensions, 0.0)
    seen = set()
    for day in route.day_groups:
        profiles = {p.place_id: p for p in getattr(day, "visit_profiles", [])}
        night = route_night_visit_intervals(day, request) if NIGHT_VIEW in dimensions else {}
        for place in day.places:
            if place.place_id in seen:
                continue
            seen.add(place.place_id)
            profile = profiles.get(place.place_id) or build_visit_profile(place)
            for dim in dimensions:
                if not candidate_matches_dimension(place, dim):
                    continue
                minutes = profile.visit_minutes
                if dim == NIGHT_VIEW:
                    interval = night.get(place.place_id)
                    minutes = min(minutes, max(0, interval[1] - interval[0])) if interval else 0
                totals[dim] += max(0, minutes)
    return totals


