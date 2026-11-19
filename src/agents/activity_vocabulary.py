"""Existing activity vocabulary shared by coverage and preference validation."""
import re

_ACTIVITY_ACTION_MARKERS = (
    "参观",
    "游览",
    "逛",
    "漫步",
    "散步",
    "走一段",
    "走一圈",
    "探索",
    "取景",
    "构图",
    "拍摄",
    "拍",
    "观察",
    "看看",
    "看",
    "阅读",
    "休息",
    "坐下",
    "茶歇",
    "补给",
    "用餐",
    "午餐",
    "晚餐",
    "吃",
    "喝",
    "品尝",
    "登上",
    "俯瞰",
)
_MEAL_ACTIVITY_MARKERS = (
    "用餐",
    "午餐",
    "晚餐",
    "吃",
    "喝",
    "品尝",
    "小吃",
    "甜品",
    "咖啡",
    "茶歇",
    "补给",
    "休息",
)

EXISTING_FAMILY_ACTION_PATTERN = r"散步|漫步|步行|走|逛|看展|参观|观察|观看|观赏|欣赏|慢慢看|仔细看|看一看|看看|看(?:展品|建筑|沿途)|拍照|取景|用餐|吃饭"

ACTIVITY_ACTION_RE = re.compile(EXISTING_FAMILY_ACTION_PATTERN + "|" + "|".join(map(re.escape, sorted(_ACTIVITY_ACTION_MARKERS, key=len, reverse=True))))
