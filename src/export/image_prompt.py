"""Prompt construction for city-background AI images."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any


TRIP_ART_PROMPT_VERSION = "v0.15.4-trip-art-3"


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
        "Changsha, Hunan, a lively riverside city, the Xiang River waterfront, Yuelu Mountain, warm night street glow, dense urban texture"
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
        "Luoyang, Henan, an ancient capital, wooded cliffs along the Yi River, peony gardens, old city gates"
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
        "Datong, Shanxi, restored ancient city walls, grey tile rooftops, dry hills, northern plateau light"
    ),
    "乌鲁木齐": (
        "Urumqi, Xinjiang, at the foot of the snow-capped Tianshan Mountains, the Grand Bazaar's domes and arches, wide northwestern sky"
    ),
    "开封": (
        "Kaifeng, Henan, an old Song dynasty capital, the Iron Pagoda, Qingming riverside streets of the Song style, lantern-lit night markets"
    ),
}

# The existing renderers keep their deployed prompts until they adopt trip art.
_LEGACY_CITY_ATMOSPHERE = {
    "长沙": (
        "Changsha, Hunan, a lively riverside city, the Xiang River and Orange Isle, Yuelu Mountain, warm night street glow, dense urban texture"
    ),
    "洛阳": (
        "Luoyang, Henan, an ancient capital, the Longmen Grottoes Buddhist carvings on the Yi River cliffs, peony gardens, old city gates"
    ),
    "大同": (
        "Datong, Shanxi, the Yungang Grottoes Buddhist cliff carvings, restored ancient city walls, northern plateau light"
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

_FOOD_CATEGORIES = {"restaurant", "food", "market"}
_FOOD_ROLES = {"meal_stop", "coffee_stop", "snack_stop", "cafe_stop"}
_TRIP_ART_EXCLUDED_STOP_KEYWORDS = (
    "橘子洲", "韶山", "天安门", "毛泽东", "毛主席", "主席", "伟人", "领袖",
    "纪念馆", "纪念堂", "纪念碑", "故居", "陵", "烈士", "雕像", "塑像", "铜像", "雕塑", "头像",
)
_PARENTHESES_RE = re.compile(r"（[^（）]*）|\([^()]*\)")
_BRANCH_SUFFIX_RE = re.compile(r"\s*(?:（[^（）]*）|\([^()]*\))\s*$")


def is_food_stop(place: Mapping[str, Any]) -> bool:
    return place.get("category") in _FOOD_CATEGORIES or place.get("role") in _FOOD_ROLES


def shop_display_name(name: str) -> str:
    return _BRANCH_SUFFIX_RE.sub("", name).strip() or name


def build_trip_art_prompt(
    city: str, *, places: Sequence[Mapping[str, Any]] | None = None
) -> str:
    """Use city scenery and eligible sightseeing stops as visual references."""
    names = []
    for place in places or ():
        raw_name = str(place.get("name") or "")
        if is_food_stop(place) or any(word in raw_name for word in _TRIP_ART_EXCLUDED_STOP_KEYWORDS):
            continue
        name = _PARENTHESES_RE.sub("", raw_name).strip()
        if name:
            names.append(name)
    stops = "、".join(dict.fromkeys(names))
    if stops:
        scene = (
            f"Paint a day banner for a trip to {city}, inspired by these stops: {stops}. "
            "Use the stops as visual references for one cohesive scene. "
        )
    elif places is not None:
        scene = f"Paint a day banner showing the local landscape and architecture of {city}. "
    else:
        scene = f"Paint the cover scene with a recognizable sense of place in {_city_atmosphere(city)}. "

    return (
        "Editorial watercolor and gouache travel illustration on warm textured paper. "
        "Landscape 3:2, full bleed, soft ink edges, muted jade, warm ochre and natural light. "
        f"{scene}"
        "Depict only scenery, buildings, plants and water. "
        "Absolutely no text, no letters, no Chinese characters, no writing on signs or shopfronts, "
        "no numbers, no logos, no watermarks. Leave signs blank. "
        "No people, no human faces, no portraits, no human statues, no busts, "
        "no giant sculpted heads, no reliefs of human figures, and no other figurative human sculptures. "
        "Do not paint portraits hung on city gates, gate towers or buildings. "
        "Keep public spaces empty."
    )


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
    city_atmosphere = _city_atmosphere(city, legacy=True)
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


def _city_atmosphere(city: str, *, legacy: bool = False) -> str:
    if legacy and city in _LEGACY_CITY_ATMOSPHERE:
        return _LEGACY_CITY_ATMOSPHERE[city]
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


def _ascii_only(value: str) -> str:
    text = re.sub(r"\s+", " ", value or "").strip()
    return "".join(ch for ch in text if ord(ch) < 128).strip()
