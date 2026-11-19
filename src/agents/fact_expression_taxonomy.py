"""Shared hard-fact and soft-expression taxonomy for Writer gates."""

from __future__ import annotations

import re

# "7 天", "20:00", "一两天", "十点"; not an ordinal "第二天" or a bare "8月".
_LEAD_TIME = (
    r"(?:\d+\s*(?:天|日|周|个月|个?小时|点)|\d{1,2}\s*[:：]\s*\d{2}|"
    r"(?<![第一二三四五六七八九十两半])[一二三四五六七八九十两半]+\s*(?:天|日|周|个月|个?小时|点))"
)

UNSUPPORTED_FACT_PATTERNS: tuple[tuple[str, str], ...] = (
    (
        "source_voice_claim",
        r"本地人(?:常去|推荐|爱去)|当地人(?:常去|推荐)|"
        r"博主(?:很喜欢|很爱|推荐)|作者(?:很喜欢|很爱|推荐)|亲测|据说|听说|网传",
    ),
    ("price_claim", r"\d+\s*元"),
    (
        "ticket_decision_advice",
        r"(?:可以|可)?根据实际情况(?:决定|选择)是否(?:购买[^。；\n]{0,8}票|买票|购票)",
    ),
    (
        "transport_decision_advice",
        r"建议(?:打车|坐车|乘车|乘坐|步行|地铁|公交|自驾|开车)[^。；\n]{0,16}|"
        r"(?:可以|可)?考虑(?:打车|坐车|乘车|乘坐|步行|地铁|公交|自驾|开车)[^。；\n]{0,16}|"
        r"打车前往|坐车前往|乘车前往|步行几分钟就能到|步行几分钟可到|"
        r"打车或坐车|打车或乘车|灵活调整|根据当天状态安排|"
        r"可自行权衡|自行权衡|到场再看|到现场再看|根据自己兴趣选择|"
        r"可以根据(?:自己|个人)?兴趣选择",
    ),
    (
        "onsite_transport_claim",
        r"(?:园内|馆内|景区内|景点内)?观光车[^。；\n]{0,48}|"
        r"(?:排队时间|排队时长)[^。；\n]{0,24}|"
        r"(?:各区域|各景点|区域间|景点间)[^。；\n]{0,12}距离[^。；\n]{0,16}",
    ),
    (
        "ticket_claim",
        r"门票[^。；\n]*\d+|票价[^。；\n]*\d+|购票|买票|购买[^。；\n]{0,8}票|"
        r"售票员|票务",
    ),
    (
        "subjective_photo_judgment",
        r"没什么好拍的|没什么可拍的|不太好拍|不好拍|不出片|拍不出[^。；\n]{0,12}",
    ),
    (
        "unauthorized_experience_packaging",
        r"轻松时光|安静的单人时光|自在的单人时光|完美的句号|能量场|"
        r"边走边吃|慢慢闲逛|顺路看看|打卡点|氛围感拉满|经典拍照机位|"
        r"知名景点|值得停留看看|挺有意思|暖宝宝|小游戏|服务细节|"
        r"核心商业区|随意转转|核心点串起来|节奏不紧|随走随停|"
        r"视野开阔|随意漫步|夜景收尾|路线卖点|人气旺|吃法建议",
    ),
    (
        "signature_dish_claim",
        r"招牌菜|招牌|他家[^。；\n]{0,40}(?:好吃|可口|值得|分量)",
    ),
    (
        "concrete_food_taste_claim",
        r"巨好喝|肉量足|细嫩入味|口感脆弹|辣得很过瘾|"
        r"鲜嫩不膻|皮薄馅足|不膻|脆弹|入味|够味|地道|"
        r"口味(?:偏甜|偏辣|清淡)",
    ),
    (
        "booking_claim",
        r"(?:不用|无需|免|不需要|不必|提前|记得|建议|最好|需要)?预约",
    ),
    (
        # timeline only: a booking reminder that names a lead time or a
        # release hour ("提前7天预约", "每天20:00放票").
        "booking_detail_claim",
        r"(?:预约|放票|开票|开抢)[^。；！？\n]{0,12}" + _LEAD_TIME + r"[^。；！？\n]*|"
        + _LEAD_TIME + r"[^。；！？\n]{0,16}(?:预约|放票|开票|开抢|抢票|买票|购票)",
    ),
    (
        "history_culture_claim",
        r"百年|老字号|民国|宋代|唐代|明清|历史底蕴|文化底蕴|古典园林",
    ),
    ("opening_hours_claim", r"营业时间|开门时间|闭店时间"),
    ("popularity_best_claim", r"必打卡|最(?:佳|火|热门)|很出片|超级出片|最佳机位"),
)

SOFT_EXPRESSION_MARKERS = (
    "作为起点",
    "开启这一天",
    "轻松收尾",
    "顺路",
    "转场",
    "衔接",
    "休息",
    "坐下",
    "坐下歇歇",
    "歇歇",
    "喝杯",
    "喝茶",
    "盖碗茶",
    "慢慢逛",
    "慢慢走",
    "走走停停",
    "逛一圈",
    "逛逛",
    "午餐",
    "晚餐",
    "用餐",
    "茶歇",
    "补给",
    "停留",
    "慢生活",
    "松弛感",
    "氛围",
    "老城氛围",
    "节奏比较 chill",
    "放慢脚步",
)

SOFT_EVIDENCE_MARKERS = (
    "轻度扩写",
    "轻度事实扩写",
    "轻微",
    "偏轻微",
    "问题不大",
    "整体可接受",
    "基本符合",
    "可记录",
    "弱问题",
    "留意",
    "需留意",
    "细节组织偏多",
    "基于证据的轻度扩写风险",
)

UNTRUSTED_TRANSPORT_FACT_REASONS = frozenset({
    "transport_decision_advice",
    "onsite_transport_claim",
})

SOFT_UNAUTHORIZED_EXPRESSION_MARKERS = (
    "很值得",
    "值得",
    "很有氛围",
    "氛围感",
    "感受氛围",
    "感受一下",
    "老城氛围",
    "经典路线",
    "宝藏小店",
    "宝藏",
    "适合沉浸",
    "沉浸体验",
    "很出片",
    "超级出片",
    "随手拍都有大片感",
    "大片感",
    "拍照记录",
    "拍照留念",
    "本地人爱去",
    "本地人推荐",
    "当地人推荐",
    "经典机位",
    "经典拍照机位",
    "必打卡",
    "根据自己状态",
    "灵活加减",
    "节奏可以自己说了算",
)

VERIFIABLE_HARD_FACT_MARKERS = (
    "元",
    "免费",
    "收费",
    "门票",
    "票价",
    "购票",
    "买票",
    "车票",
    "购买车票",
    "售票",
    "票务",
    "建议打车",
    "建议坐车",
    "建议乘车",
    "建议乘坐",
    "建议步行",
    "考虑步行",
    "建议地铁",
    "建议公交",
    "打车",
    "坐车",
    "乘车",
    "打车前往",
    "坐车前往",
    "乘车前往",
    "步行几分钟就能到",
    "步行几分钟可到",
    "打车或坐车",
    "打车或乘车",
    "公共交通",
    "观光车",
    "排队时间",
    "排队时长",
    "距离并不远",
    "距离不远",
    "地铁",
    "公交",
    "轨道交通",
    "换乘",
    "自驾",
    "开车",
    "亮灯",
    "熄灯",
    "预约",
    "营业时间",
    "开门时间",
    "闭店时间",
    "开放时间",
    "闭馆",
    "休馆",
    "本地人",
    "当地人",
    "博主",
    "作者",
    "亲测",
    "据说",
    "听说",
    "网传",
    "招牌",
    "菜单",
    "菜品",
    "售卖",
    "供应",
    "买些",
    "老字号",
    "百年",
    "民国",
    "宋代",
    "唐代",
    "明清",
    "历史底蕴",
    "文化底蕴",
    "古典园林",
    "12月",
    "12月份",
    "一月",
    "二月",
    "三月",
    "四月",
    "五月",
    "六月",
    "七月",
    "八月",
    "九月",
    "十月",
    "十一月",
    "十二月",
)

HARD_FACT_MARKERS = VERIFIABLE_HARD_FACT_MARKERS
_VERIFIABLE_CLOCK_TIME_RE = re.compile(r"\d{1,2}\s*[:：]\s*\d{2}")
_VERIFIABLE_TRANSIT_DURATION_RE = re.compile(
    r"(?:车程|路程|通勤|驾车|打车|坐车|乘车|公共交通|公交|"
    r"地铁|轨道交通|步行)"
    r"[^。；\n]{0,24}"
    r"(?:\d+|[一二三四五六七八九十百两]+)\s*分钟"
)
_DENIED_VERIFIABLE_HARD_FACT_RE = re.compile(
    r"(?:未授权|未经授权|无证据支持|证据(?:未|不)支持|缺少授权)"
    r"[^。；\n]{0,32}"
    r"(?:具体品类|具体菜品|菜单|售卖内容|供应内容|价格|票价|门票|"
    r"开放时间|营业时间|开门时间|闭店时间|交通方式|通勤时长|"
    r"路线时长|亮灯|熄灯)"
)


_SIGNBOARD_SENTENCE_BREAK_RE = re.compile(r"[。！？!?；;\n\r<>]")
_SIGNBOARD_VISUAL_RE = re.compile(r"拍摄|拍照|取景|构图|镜头|画面|观察|查看")
_SIGNBOARD_OBJECT_RE = re.compile(
    r"店铺|商铺|店面|门头|灯箱|墙面|街景|建筑|标识|文字|字体|字样|边框"
)
_SIGNBOARD_FOOD_RE = re.compile(
    r"菜|餐|食|吃|喝|尝|口味|味道|好吃|可口|分量|必点|"
    r"点(?:单|餐|这|它|个|一|招牌)|主打|供应|推荐|享用|来(?:一?[份道杯碗])"
)
_SIGNBOARD_SUFFIX_RE = re.compile(
    r"^[，,、]|^(?:和|与|及|或)(?:建筑|灯箱|店面|门头|墙面|街景|标识)|"
    r"^(?:上(?:的)?|的)(?:文字|字体|图案|轮廓|颜色|边框|外观|反光|细节)|"
    r"^(?:纳入|放入|放在|框入|拍入)"
)


def _is_visual_signboard(text: str, start: int, end: int) -> bool:
    """Disambiguate one bare signboard word without granting fact authority.

    Only an explicit, short visual context qualifies. Food, missing context,
    unknown noun suffixes and context across sentence/markup boundaries retain
    the original conservative treatment. Inspect each occurrence independently.
    """
    if text[start:end] != "招牌":
        return False
    left = 0
    for boundary in _SIGNBOARD_SENTENCE_BREAK_RE.finditer(text, 0, start):
        left = boundary.end()
    boundary = _SIGNBOARD_SENTENCE_BREAK_RE.search(text, end)
    right = boundary.start() if boundary else len(text)
    context = text[left:right]
    if (len(context) > 96
            or not _SIGNBOARD_VISUAL_RE.search(context)
            or not _SIGNBOARD_OBJECT_RE.search(context)):
        return False
    if _SIGNBOARD_FOOD_RE.search(context):
        return False
    suffix = text[end:right].lstrip()
    return not suffix or bool(_SIGNBOARD_SUFFIX_RE.match(suffix))


def _prompt_version() -> str:
    try:
        from src.config import get_settings
    except ImportError:  # pragma: no cover
        return "v6"
    return str(getattr(get_settings(), "writer_prompt_version", "v6") or "").strip()


def _timeline_tiers() -> bool:
    """v0.15.1: the timeline and article prompts read their checks from writing_tiers."""
    return _prompt_version() in ("timeline", "article")


def _hard_markers() -> tuple[str, ...] | frozenset[str]:
    if not _timeline_tiers():
        return VERIFIABLE_HARD_FACT_MARKERS
    from src.agents.writing_tiers import added_hard_markers, released_hard_markers
    released = released_hard_markers()
    return (*(marker for marker in VERIFIABLE_HARD_FACT_MARKERS if marker not in released),
            *added_hard_markers())


def has_verifiable_hard_fact_marker(text: str) -> bool:
    value = text or ""
    return (
        any(marker in value for marker in _hard_markers()
            if marker != "招牌")
        or any(not _is_visual_signboard(value, match.start(), match.end())
               for match in re.finditer("招牌", value))
        or bool(_VERIFIABLE_CLOCK_TIME_RE.search(value))
        or bool(_VERIFIABLE_TRANSIT_DURATION_RE.search(value))
    )


def has_hard_fact_marker(text: str) -> bool:
    """Compatibility alias for the explicit verifiable-hard-fact policy."""
    return has_verifiable_hard_fact_marker(text)


def has_denied_verifiable_hard_fact(text: str) -> bool:
    """Detect Review evidence that explicitly denies a hard-fact domain."""
    return bool(_DENIED_VERIFIABLE_HARD_FACT_RE.search(text or ""))


def has_soft_expression_marker(text: str) -> bool:
    return any(marker in (text or "") for marker in SOFT_EXPRESSION_MARKERS)


def has_soft_evidence_marker(text: str) -> bool:
    return any(marker in (text or "") for marker in SOFT_EVIDENCE_MARKERS)


def has_soft_unauthorized_expression_marker(text: str) -> bool:
    return any(
        marker in (text or "")
        for marker in SOFT_UNAUTHORIZED_EXPRESSION_MARKERS
    )


def is_safe_soft_expression(snippet: str, evidence: str = "") -> bool:
    if not snippet:
        return False
    if has_hard_fact_marker(snippet):
        return False
    return has_soft_expression_marker(snippet) or has_soft_evidence_marker(evidence)


# The v6 prompt keeps its own checks; booking_claim already covers these there.
_TIMELINE_ONLY_FACT_REASONS = frozenset({"booking_detail_claim"})


def unsupported_fact_pattern_matches(text: str) -> list[tuple[str, re.Match[str]]]:
    if _prompt_version() == "article":
        # v0.15.1 D1/D2: no wording list for the article; prices, clocks and
        # restrictions are checked per stop against the material (article_writer).
        return []
    matches: list[tuple[str, re.Match[str]]] = []
    allowed: frozenset[str] = _TIMELINE_ONLY_FACT_REASONS
    if _timeline_tiers():
        from src.agents.writing_tiers import allowed_fact_reasons
        allowed = allowed_fact_reasons()
    for fact_reason, pattern in UNSUPPORTED_FACT_PATTERNS:
        if fact_reason in allowed:
            continue
        for match in re.finditer(pattern, text):
            if (fact_reason == "signature_dish_claim"
                    and _is_visual_signboard(text, match.start(), match.end())):
                continue
            matches.append((fact_reason, match))
    return matches


def untrusted_transport_claim_matches(text: str) -> list[re.Match[str]]:
    """Return only verifiable transport claims owned by deterministic cleanup."""
    return [
        match
        for fact_reason, match in unsupported_fact_pattern_matches(text)
        if fact_reason in UNTRUSTED_TRANSPORT_FACT_REASONS
        and has_verifiable_hard_fact_marker(match.group(0))
    ]
