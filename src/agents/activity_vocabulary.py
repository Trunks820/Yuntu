"""Shared activity verbs used by preference body checks."""

from __future__ import annotations

import re

ACTIVITY_ACTION_RE = re.compile(
    r"(参观|游览|漫步|散步|拍照|打卡|用餐|品尝|逛街|购物|体验|骑行|步行|观看|听)"
)
