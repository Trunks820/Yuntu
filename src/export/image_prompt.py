"""Prompt construction for city-background AI images."""

from __future__ import annotations

import re
from dataclasses import dataclass, field


SHARE_IMAGE_PROMPT_VERSION = "v0.15-r12"
SHARE_IMAGE_PROMPT_LAYOUT_VERSION = "ai-full-context-compact-visible-v1"
SHARE_REFERENCE_CONTEXT_MAX_CHARS = 14_000
SHARE_VISIBLE_SCRIPT_THREE_DAY_MAX_CHARS = 2400
SHARE_VISIBLE_SCRIPT_MAX_CHARS = 3200
# Kept as a compatibility alias for callers that enforce the documented hard limit.
SHARE_POSTER_SCRIPT_MAX_CHARS = SHARE_REFERENCE_CONTEXT_MAX_CHARS


@dataclass(frozen=True)
class SharePlaceDetail:
    name: str
    category: str = ""
    brief: str = ""


@dataclass(frozen=True)
class ShareDaySummary:
    day: int
    title: str
    theme: str
    bullets: tuple[str, ...] = field(default_factory=tuple)
    place_chips: tuple[str, ...] = field(default_factory=tuple)
    narrative: str = ""
    commute_summary: str = ""
    places: tuple[SharePlaceDetail, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ShareImageSummary:
    city: str
    days: int
    people_count: int | None
    preferences: tuple[str, ...]
    avoid: tuple[str, ...]
    route_style_label: str
    day_summaries: tuple[ShareDaySummary, ...]
    core_place_chips: tuple[str, ...]
    suitable_for: str
    compression_mode: str
    notes: str = ""
    plan_title: str = ""
    plan_summary: str = ""
    plan_tags: tuple[str, ...] = field(default_factory=tuple)
    pace_summary: str = ""
    weather_lines: tuple[str, ...] = field(default_factory=tuple)
    time_preference_lines: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class SharePosterPrompt:
    prompt: str
    visible_script_char_count: int
    reference_context_char_count: int
    reference_context_truncated: bool


_CITY_ATMOSPHERE: dict[str, str] = {
    "北京": (
        "Beijing, China's capital, Forbidden City palace rooflines, old hutong lanes, cypress courtyards, broad axial boulevards, crisp northern light"
    ),
    "上海": (
        "Shanghai, a modern riverside metropolis, the Bund facing the Lujiazui towers across the Huangpu River, plane-tree avenues, soft skyline light"
    ),
    "重庆": (
        "Chongqing, a mountainous river metropolis in southwest China where the Yangtze meets the Jialing, stacked hillside streets, stilted houses, elevated walkways, warm evening mist, river lights"
    ),
    "成都": (
        "Chengdu, Sichuan, a relaxed inland city, bamboo tea-house courtyards, Kuanzhai Alley grey-brick lanes, leafy avenues, soft humid light"
    ),
    "杭州": (
        "Hangzhou, Zhejiang, a calm lakeside city, West Lake reflections, willow-lined causeways, Leifeng Pagoda, green tea hills, soft poetic morning light"
    ),
    "西安": (
        "Xi'an, Shaanxi, an ancient Chinese capital, the Ming city wall, the Bell Tower, the Big Wild Goose Pagoda, layered traditional rooflines, cinematic dusk light"
    ),
    "南京": (
        "Nanjing, Jiangsu, a historic capital city, stone city walls, Qinhuai River lantern evenings, parasol-tree boulevards, Purple Mountain, calm amber light"
    ),
    "长沙": (
        "Changsha, Hunan, a lively riverside city, the Xiang River and Orange Isle, Yuelu Mountain, warm night street glow, dense urban texture"
    ),
    "青岛": (
        "Qingdao, Shandong, a seaside city, red-roof European-era houses, Zhanqiao Pier, granite coastline on the Yellow Sea, clear coastal light"
    ),
    "桂林": (
        "Guilin, Guangxi, limestone karst peaks along the Li River, bamboo rafts, misty hills, soft humid light, serene natural atmosphere"
    ),
    "广州": (
        "Guangzhou, Guangdong, the Pearl River and Canton Tower, arcaded qilou shophouse streets, old banyan trees, warm subtropical evening"
    ),
    "武汉": (
        "Wuhan, Hubei, a great city on the Yangtze River, the Yellow Crane Tower, long river bridges, East Lake, broad river light"
    ),
    "苏州": (
        "Suzhou, Jiangsu, classical Chinese gardens, quiet canals under arched stone bridges, whitewashed walls and black-tiled roofs, soft rain light"
    ),
    "厦门": (
        "Xiamen, Fujian, a subtropical island city, Gulangyu villas among banyan trees, seaside boulevards, bright blue sea and sky"
    ),
    "昆明": (
        "Kunming, Yunnan, the Spring City on the high plateau, Dianchi Lake, the Western Hills, flower markets, clear blue plateau sky"
    ),
    "三亚": (
        "Sanya, Hainan, a tropical beach city, white sand bays, coconut palms, turquoise South China Sea, bright sunshine, resort coastline"
    ),
    "大理": (
        "Dali, Yunnan, Erhai Lake under the Cangshan Mountains, the Three Pagodas, whitewashed Bai village houses, clear highland light"
    ),
    "丽江": (
        "Lijiang, Yunnan, the Naxi old town of wooden houses and tiled roofs along stone canals, Jade Dragon Snow Mountain, clear highland light"
    ),
    "哈尔滨": (
        "Harbin, Heilongjiang, a snowy northern city, the onion-domed St. Sophia Cathedral, Russian-style Central Street, the frozen Songhua River, ice-lantern glow"
    ),
    "洛阳": (
        "Luoyang, Henan, an ancient capital, the Longmen Grottoes Buddhist carvings on the Yi River cliffs, peony gardens, old city gates"
    ),
    "泉州": (
        "Quanzhou, Fujian, a maritime silk road port, Kaiyuan Temple's twin stone pagodas, red-brick Minnan houses with swallowtail roofs"
    ),
    "福州": (
        "Fuzhou, Fujian, the Three Lanes and Seven Alleys of whitewashed walls, giant banyan trees, the Min River, humid warm light"
    ),
    "天津": (
        "Tianjin, a northern port city, the Haihe River, European-style villas of the Five Avenues, the Tianjin Eye Ferris wheel"
    ),
    "深圳": (
        "Shenzhen, Guangdong, a modern bay city, glass skyscrapers, seaside parks, lush subtropical greenery, bright clean light"
    ),
    "珠海": (
        "Zhuhai, Guangdong, a garden city on the sea, the Lovers' Road coastline, green islands, calm blue bay"
    ),
    "威海": (
        "Weihai, Shandong, a clean seaside city, winding coastal roads, Liugong Island, clear blue water and sky"
    ),
    "张家界": (
        "Zhangjiajie, Hunan, towering quartz-sandstone pillars rising from misty forest, deep green valleys, dramatic mountain light"
    ),
    "大同": (
        "Datong, Shanxi, the Yungang Grottoes Buddhist cliff carvings, restored ancient city walls, northern plateau light"
    ),
    "乌鲁木齐": (
        "Urumqi, Xinjiang, at the foot of the snow-capped Tianshan Mountains, the Grand Bazaar's domes and arches, wide northwestern sky"
    ),
    "开封": (
        "Kaifeng, Henan, an old Song dynasty capital, the Iron Pagoda, Qingming riverside streets of the Song style, lantern-lit night markets"
    ),
}

_CITY_FALLBACK_KEYS: dict[str, str] = {
    "北京": "beijing",
    "上海": "shanghai",
    "重庆": "chongqing",
    "成都": "chengdu",
    "杭州": "hangzhou",
    "西安": "xian",
    "南京": "nanjing",
    "长沙": "changsha",
    "青岛": "qingdao",
    "桂林": "guilin",
}

_VIBE_KEYWORDS: dict[str, str] = {
    "美食": "local food culture",
    "citywalk": "slow city walking",
    "夜景": "evening city lights",
    "山城": "mountain city layers",
    "轻松": "relaxed pace",
    "亲子": "family friendly calm",
    "拍照": "cinematic travel photography",
    "人文": "local culture",
}


def build_share_image_background_prompt(summary: ShareImageSummary) -> str:
    """Build the r10 final-poster prompt from reference context and compact text."""

    return build_share_image_prompt_payload(summary).prompt


def build_share_image_prompt_payload(summary: ShareImageSummary) -> SharePosterPrompt:
    """Return the bounded reference prompt plus non-sensitive size metadata."""

    city_atmosphere = _city_atmosphere(summary.city)
    reference_context, truncated = _reference_context(summary)
    visible_script = _visible_poster_script(summary)
    prompt = (
        "Create one complete final 2:3 vertical Chinese editorial illustrated travel scrapbook "
        f"poster for {city_atmosphere}. Use warm paper, brush-and-gouache city illustration, brick "
        "red, deep navy and tea-brown accents. Lock the composition to this preferred layout: a large "
        "brush title at upper left and an unmistakable city hero at upper right; three prominent "
        "horizontal Day modules, each with one large scene on the left, compact text in the middle, "
        "and 2-4 small attraction or food picture vignettes on the right; "
        "finish with travel tags at the bottom. For trips not exactly three days, preserve this visual rhythm "
        "while fitting every supplied Day. Never use dense long paragraphs, tables, spreadsheet-like "
        "rows, or the crowded second-poster layout. The returned image is final and receives no backend "
        "overlay. Use REFERENCE CONTEXT only to choose authentic places, food, buildings, routes and "
        "illustrations; NEVER render it verbatim. The only visible text allowed is the quoted values in "
        "VISIBLE TEXT. Copy those values accurately without quote marks. Never render field labels such as "
        "标题, 行程, 路线, 日, 摘, 点, or 标签; do not render punctuation outside quotes, full narratives, "
        "commute text, place briefs, or reference-context "
        "prose. Do not invent facts, places, dates, people, prices or brands. Never render any monetary "
        "amount, currency symbol, budget figure or expense text anywhere on the poster. "
        "No logo, URL, watermark, QR "
        "code, pseudo-Chinese or explanatory copy.\n\n"
        "--- REFERENCE CONTEXT - NEVER RENDER VERBATIM ---\n"
        f"{reference_context}\n"
        "--- END REFERENCE CONTEXT ---\n\n"
        "--- VISIBLE TEXT - ONLY QUOTED VALUES MAY APPEAR ---\n"
        f"{visible_script}\n"
        "--- END VISIBLE TEXT ---"
    )
    return SharePosterPrompt(
        prompt=prompt,
        visible_script_char_count=len(visible_script),
        reference_context_char_count=len(reference_context),
        reference_context_truncated=truncated,
    )


def _visible_poster_script(summary: ShareImageSummary) -> str:
    day_count = min(7, len(summary.day_summaries))
    limits = _visible_script_limits(day_count)
    city = _compact_visible_text(summary.city, limits["city"])
    lines = [_visible_text_instruction("标题", f"{city}{summary.days}天旅行攻略")]
    trip_info: list[str] = []
    if summary.people_count:
        trip_info.append(f"{summary.people_count}人同行")
    if summary.preferences:
        trip_info.append("偏好 " + "、".join(summary.preferences[:3]))
    if summary.avoid:
        trip_info.append("避开 " + "、".join(summary.avoid[:2]))
    if trip_info:
        lines.append(
            _visible_text_instruction(
                "行程",
                _compact_visible_text("｜".join(trip_info), limits["trip_info"]),
            )
        )
    route_summary = _compact_visible_text(
        summary.plan_summary or summary.route_style_label or summary.plan_title,
        limits["route"],
    )
    if route_summary:
        lines.append(_visible_text_instruction("路线", route_summary))

    for day in summary.day_summaries[:7]:
        title = _compact_visible_text(day.title or day.theme, limits["day_title"])
        lines.append(
            _visible_text_instruction(
                f"日{day.day}",
                f"Day {day.day}｜{title}",
            )
        )
        day_summary = _visible_day_summary(day, limits["day_summary"])
        if day_summary:
            lines.append(_visible_text_instruction(f"摘{day.day}", day_summary))
        places = tuple(
            _compact_visible_text(place_name, limits["place_name"])
            for place_name in day.place_chips[: limits["place_count"]]
            if place_name
        )
        if places:
            lines.append(
                _visible_text_instruction(f"点{day.day}", "｜".join(places))
            )

    tags = _poster_tags(
        summary,
        count=limits["tag_count"],
        length=limits["tag"],
    )
    if tags:
        lines.append(_visible_text_instruction("标签", "｜".join(tags)))
    hard_limit = (
        SHARE_VISIBLE_SCRIPT_THREE_DAY_MAX_CHARS
        if day_count == 3
        else SHARE_VISIBLE_SCRIPT_MAX_CHARS
    )
    return _bounded_visible_script(lines, hard_limit)


def _reference_context(summary: ShareImageSummary) -> tuple[str, bool]:
    header = [
        _reference_line("City", summary.city),
        _reference_line("Days", str(summary.days)),
    ]
    if summary.people_count:
        header.append(_reference_line("People", str(summary.people_count)))
    for label, value in (
        ("Preferences", "、".join(summary.preferences)),
        ("Avoid", "、".join(summary.avoid)),
        ("Traveller notes", summary.notes),
        ("Plan title", summary.plan_title),
        ("Plan summary", summary.plan_summary),
        ("Plan tags", "、".join(summary.plan_tags)),
        ("Pace", summary.pace_summary),
    ):
        if value:
            header.append(_reference_line(label, value))

    day_sections: list[list[str]] = []
    for day in summary.day_summaries[:7]:
        lines = [
            _reference_line(f"Day {day.day}", day.title),
        ]
        if day.narrative:
            lines.append(_reference_line(f"Day {day.day} narrative", day.narrative))
        if day.commute_summary:
            lines.append(_reference_line(f"Day {day.day} commute", day.commute_summary))
        for index, place in enumerate(day.places, start=1):
            details = [place.name]
            if place.category:
                details.append(place.category)
            if place.brief:
                details.append(place.brief)
            lines.append(_reference_line(f"Day {day.day} place {index}", "｜".join(details)))
        day_sections.append(lines)

    tail: list[str] = []
    for index, value in enumerate(summary.weather_lines, start=1):
        tail.append(_reference_line(f"Weather {index}", value))
    for index, value in enumerate(summary.time_preference_lines, start=1):
        tail.append(_reference_line(f"Time preference {index}", value))
    if summary.suitable_for:
        tail.append(_reference_line("Suitable audience", summary.suitable_for))

    sections = [header, *day_sections, tail]
    full_context = "\n".join(line for section in sections for line in section)
    if len(full_context) <= SHARE_REFERENCE_CONTEXT_MAX_CHARS:
        return full_context, False
    return _bounded_reference_context(header, day_sections, tail), True


def _visible_text_instruction(label: str, value: str) -> str:
    safe_value = (value or "").replace('"', "”")
    return f'{label}:"{safe_value}"'


def _visible_script_limits(day_count: int) -> dict[str, int]:
    if day_count <= 3:
        return {
            "city": 8,
            "trip_info": 36,
            "route": 20,
            "day_title": 10,
            "day_summary": 22,
            "place_count": 3,
            "place_name": 10,
            "tag_count": 4,
            "tag": 8,
        }
    if day_count <= 5:
        return {
            "city": 8,
            "trip_info": 32,
            "route": 18,
            "day_title": 9,
            "day_summary": 18,
            "place_count": 2,
            "place_name": 9,
            "tag_count": 3,
            "tag": 8,
        }
    return {
        "city": 8,
        "trip_info": 28,
        "route": 16,
        "day_title": 8,
        "day_summary": 16,
        "place_count": 2,
        "place_name": 8,
        "tag_count": 2,
        "tag": 8,
    }


def _bounded_visible_script(lines: list[str], hard_limit: int) -> str:
    script = "\n".join(lines)
    if len(script) <= hard_limit:
        return script

    parsed: list[tuple[str, str]] = []
    for line in lines:
        match = re.fullmatch(r'(.+?):"(.*)"', line)
        if match is not None:
            parsed.append(match.groups())
    fixed_length = sum(len(label) + len(':""') for label, _ in parsed) + max(0, len(parsed) - 1)
    value_budget = max(len(parsed), hard_limit - fixed_length)
    bounded: list[str] = []
    for index, (label, value) in enumerate(parsed):
        remaining_lines = len(parsed) - index
        allowance = max(1, value_budget // remaining_lines)
        compact = _compact_visible_text(value, allowance)
        bounded.append(_visible_text_instruction(label, compact))
        value_budget -= len(compact)
    return "\n".join(bounded)


def _reference_line(label: str, value: str) -> str:
    safe_value = re.sub(r"\s+", " ", value or "").strip().replace('"', "”")
    return f"{label} = {safe_value}"


def _bounded_reference_context(
    header: list[str],
    day_sections: list[list[str]],
    tail: list[str],
) -> str:
    section_count = 2 + len(day_sections)
    available = SHARE_REFERENCE_CONTEXT_MAX_CHARS - max(0, section_count - 1)
    header_budget = min(_lines_length(header), 2_200)
    tail_budget = min(_lines_length(tail), 1_600)
    day_budget_total = max(0, available - header_budget - tail_budget)
    minimum_day_total = len(day_sections) * 240
    if day_budget_total < minimum_day_total:
        shortage = minimum_day_total - day_budget_total
        reduce_header = min(shortage, max(0, header_budget - 600))
        header_budget -= reduce_header
        shortage -= reduce_header
        reduce_tail = min(shortage, max(0, tail_budget - 300))
        tail_budget -= reduce_tail
        day_budget_total = max(0, available - header_budget - tail_budget)

    clipped_sections = [_clip_reference_lines(header, header_budget)]
    if day_sections:
        per_day_budget = max(1, day_budget_total // len(day_sections))
        clipped_sections.extend(
            _clip_reference_lines(lines, per_day_budget)
            for lines in day_sections
        )
    clipped_sections.append(_clip_reference_lines(tail, tail_budget))
    return "\n".join(
        line
        for section in clipped_sections
        for line in section
    )


def _clip_reference_lines(lines: list[str], budget: int) -> list[str]:
    if budget <= 0 or not lines:
        return []
    if _lines_length(lines) <= budget:
        return lines

    parsed: list[tuple[str, str]] = []
    minimum = 0
    for line in lines:
        match = re.fullmatch(r"(.+?) = (.*)", line)
        if match is None:
            continue
        label, value = match.groups()
        base_length = len(label) + len(" = ") + (1 if parsed else 0)
        if minimum + base_length + 1 > budget:
            break
        parsed.append((label, value))
        minimum += base_length + 1
    if not parsed:
        return []

    value_budget = max(0, budget - minimum)
    clipped: list[str] = []
    for index, (label, value) in enumerate(parsed):
        remaining_lines = len(parsed) - index
        allowance = max(1, value_budget // remaining_lines)
        visible = _compact_visible_text(value, allowance)
        clipped.append(_reference_line(label, visible))
        value_budget -= len(visible)
    return clipped


def _lines_length(lines: list[str]) -> int:
    return sum(len(line) for line in lines) + max(0, len(lines) - 1)


def _compact_visible_text(value: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", value or "").strip()
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit == 1:
        return text[:1]
    clipped = text[: limit - 1].rstrip("，,。；;、 ")
    return (clipped or text[: limit - 1]) + "…"


def _visible_day_summary(day: ShareDaySummary, limit: int) -> str:
    candidates = [*day.bullets, *_sentences_for_prompt(day.narrative), day.commute_summary]
    for candidate in candidates:
        compact = _compact_visible_text(candidate, limit)
        if compact:
            return compact
    return ""


def _poster_tags(summary: ShareImageSummary, *, count: int, length: int) -> tuple[str, ...]:
    candidates = [*summary.plan_tags, *summary.preferences]
    if summary.route_style_label:
        candidates.append(summary.route_style_label)
    if summary.people_count:
        candidates.append(f"{summary.people_count}人出行")
    candidates.append(f"{summary.city}旅行")
    tags: list[str] = []
    for value in candidates:
        tag = _compact_visible_text(value, length)
        if tag and tag not in tags:
            tags.append(tag)
        if len(tags) >= count:
            break
    return tuple(tags[:count])


def _sentences_for_prompt(value: str) -> list[str]:
    return [
        part.strip()
        for part in re.split(r"(?<=[。！？!?；;])", value or "")
        if part.strip()
    ]




def build_pdf_background_prompt(*, city: str, days: int) -> str:
    return build_city_background_prompt(
        city=city,
        days=days,
        mood="calm premium editorial travel photography",
        route_style="complete itinerary document",
        usage="vertical A4 travel guide background",
    )


def build_city_background_prompt(
    *,
    city: str,
    days: int,
    mood: str,
    route_style: str,
    usage: str,
) -> str:
    city_atmosphere = _city_atmosphere(city)
    fallback_city_key = _fallback_city_key(city)
    duration = f"{days}-day trip" if days > 0 else "travel"
    return (
        f"Create one vertical high-resolution {usage} for {city_atmosphere}. "
        f"Overall mood: {mood}. Context: {duration}, "
        f"{route_style or 'balanced itinerary'}. "
        f"Reference fallback city key: {fallback_city_key}. "
        "Show authentic, unmistakable city character through architecture, "
        "terrain, river or street atmosphere, with cinematic natural light, "
        "depth, restrained color contrast, and generous quiet space through "
        "the center for a translucent backend text layer. Full-bleed image, "
        "editorial travel photography quality, no decorative frame. Global "
        "city atmosphere only; no day cards, itinerary-specific point-of-interest "
        "cards, route maps, day semantics, food menus, tickets, captions, logos, "
        "watermarks, QR codes, or screenshots. Hard constraints: no readable "
        "text, no Chinese characters, no labels, no UI text, no signs, no road "
        "signs, and no storefront signs. The backend adds all final text."
    )


def _city_atmosphere(city: str) -> str:
    if city in _CITY_ATMOSPHERE:
        return _CITY_ATMOSPHERE[city]
    ascii_city = _ascii_only(city)
    if ascii_city:
        return f"{ascii_city}, global city travel atmosphere"
    # No English entry yet: name the city itself so the image shows that place,
    # not an anonymous old town.
    return (
        f"the Chinese city of {city}, showing its own real landmarks, terrain "
        "and architecture rather than a generic old town, calm cinematic atmosphere"
    )


def _fallback_city_key(city: str) -> str:
    return _CITY_FALLBACK_KEYS.get(city, "generic")


def _mood_text(summary: ShareImageSummary) -> str:
    parts: list[str] = []
    for item in (*summary.preferences, summary.route_style_label):
        for key, value in _VIBE_KEYWORDS.items():
            if key in item and value not in parts:
                parts.append(value)
    if not parts:
        parts.append("relaxed urban travel")
    return ", ".join(parts[:4])


def _ascii_only(value: str) -> str:
    text = re.sub(r"\s+", " ", value or "").strip()
    return "".join(ch for ch in text if ord(ch) < 128).strip()
