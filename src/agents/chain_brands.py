"""Chain-hotel names used to tell a lodging area from a homestay street.

Longer names match first, then the sub-brand tier, then high, mid, and
economy. A brand must start at a token boundary. ASCII brands also stop
before another ASCII letter or digit, so a shorter name is not taken out of
a longer token. A miss is preferred to a wrong brand.
"""

from __future__ import annotations

# Sub-brands of a larger chain. These stay mid and match before the parent.
_SUB_MID: tuple[str, ...] = (
    "智选假日",
    "希尔顿欢朋",
    "欢朋",
    "希尔顿花园",
    "万枫",
    "美居",
    "宜必思尚品",
    "桔子水晶",
)

_HIGH: tuple[str, ...] = (
    "希尔顿",
    "康莱德",
    "华尔道夫",
    "逸林",
    "万豪",
    "JW万豪",
    "丽思卡尔顿",
    "瑞吉",
    "W酒店",
    "喜来登",
    "威斯汀",
    "艾美",
    "万丽",
    "福朋",
    "万怡",
    "洲际",
    "英迪格",
    "皇冠假日",
    "凯悦",
    "柏悦",
    "君悦",
    "安达仕",
    "香格里拉",
    "嘉里",
    "索菲特",
    "铂尔曼",
    "诺富特",
    "美爵",
    "温德姆",
    "华美达",
    "文华东方",
    "半岛",
    "四季酒店",
    "安缦",
    "悦榕庄",
    "瑰丽",
    "宝格丽",
    "朗廷",
    "丽晶",
    "开元名都",
    "金陵饭店",
    "万达文华",
    "万达嘉华",
    "禧玥",
    "施柏阁",
    "漫心",
    "花间堂",
    "豪生",
    "美利亚",
)

_MID: tuple[str, ...] = (
    "全季",
    "亚朵",
    "丽枫",
    "麗枫",
    "桔子",
    "维也纳",
    "喆啡",
    "希岸",
    "城家",
    "秋果",
    "宜尚",
    "锦江都城",
    "和颐",
    "如家商旅",
    "如家精选",
    "美豪",
    "凯里亚德",
    "星程",
    "窝趣",
    "IU",
    "开元曼居",
    "丽呈",
    "麗呈",
    "柏曼",
    "潮漫",
    "雅斯特",
    "怡程",
    "轻居",
)

_ECONOMY: tuple[str, ...] = (
    "汉庭",
    "如家",
    "7天",
    "七天",
    "锦江之星",
    "海友",
    "格林豪泰",
    "格林联盟",
    "速8",
    "尚客优",
    "城市便捷",
    "布丁",
    "莫泰",
    "99旅馆",
    "宜必思",
    "你好酒店",
    "骏怡",
    "怡莱",
    "派柏",
    "白玉兰",
    "驿居",
    "易佰",
    "华驿",
)

_TIER_PRIORITY = (
    ("mid", _SUB_MID, 0),
    ("high", _HIGH, 1),
    ("mid", _MID, 2),
    ("economy", _ECONOMY, 3),
)

# (brand, tier), longest first, then sub-brand before the parent chain.
CHAIN_BRANDS: tuple[tuple[str, str], ...] = tuple(
    (brand, tier)
    for brand, tier, _priority in sorted(
        (
            (brand, tier, priority)
            for tier, brands, priority in _TIER_PRIORITY
            for brand in brands
        ),
        key=lambda item: (-len(item[0]), item[2], item[0]),
    )
)


def match_chain_brand(name: str) -> tuple[str, str] | None:
    """Return ``(tier, brand)`` for the first chain token in ``name``."""

    text = name or ""
    if not text:
        return None
    for brand, tier in CHAIN_BRANDS:
        if _brand_in_name(text, brand):
            return tier, brand
    return None


# A brand right before one of these is the chain even behind a city prefix:
# "重庆来福士洲际酒店", "桂林两江四湖象鼻山亚朵酒店".
_LODGING_SUFFIXES = ("酒店", "大酒店", "宾馆", "饭店", "公寓", "度假村", "(", "（")


def _brand_in_name(name: str, brand: str) -> bool:
    start = 0
    while True:
        found = name.find(brand, start)
        if found < 0:
            return False
        end = found + len(brand)
        if found > 0 and _is_token_char(name[found - 1]) and not name.startswith(_LODGING_SUFFIXES, end):
            start = found + 1
            continue
        last = brand[-1]
        if last.isascii() and last.isalnum():
            if end < len(name) and name[end].isascii() and name[end].isalnum():
                start = found + 1
                continue
        return True


def _is_token_char(character: str) -> bool:
    if character.isascii() and character.isalnum():
        return True
    code = ord(character)
    return (
        0x3400 <= code <= 0x4DBF
        or 0x4E00 <= code <= 0x9FFF
        or 0xF900 <= code <= 0xFAFF
    )
