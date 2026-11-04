"""Local chain-name labels. No hotel price provider."""

from __future__ import annotations

_CHAINS: dict[str, tuple[str, ...]] = {
    "economy": ("如家", "汉庭", "7天", "七天", "锦江之星", "格林豪泰", "速8", "城市便捷"),
    "mid": ("全季", "亚朵", "桔子", "麗枫", "丽枫", "维也纳", "希尔顿欢朋", "智选假日"),
    "high": ("希尔顿", "万豪", "洲际", "香格里拉", "凯悦", "威斯汀", "瑞吉", "四季"),
}


def match_chain_brand(name: str) -> tuple[str, str] | None:
    text = name or ""
    for tier in ("mid", "economy", "high"):
        for brand in _CHAINS[tier]:
            if brand in text:
                return tier, brand
    return None
