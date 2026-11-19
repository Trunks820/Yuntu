"""v0.15.1: note excerpts that are someone else's day route, not a reason.

Itinerary notes yield lines like "Day1：南普陀→厦门大学→白城沙滩→沙坡尾（夜游）".
They only show that a blogger passed the place. Shown as a recommendation
reason, or handed to the Writer as a fact, they are noise. One rule serves the
place detail API, the card brief, the Writer's evidence and the offline
cleaner, so all of them drop the same lines.
"""
from __future__ import annotations

import re

_ARROW = r"(?:→|->|—>|=>|➡️?|➠|➜|⇨|⟶)"
_ARROW_RE = re.compile(_ARROW)
_DAY_RE = re.compile(
    r"(?i)(?:\bday\s*[0-9一二三四五六七八九十①-⑩]+|\bd\s*\d+\s*[:：]|"
    r"第\s*[0-9一二三四五六七八九十①-⑩]+\s*天)"
)
# A dash between two non-digit tokens ("植物园🌵- 钟鼓索道"), not "10:00-22:00".
_DASH_LINK_RE = re.compile(r"[^\d\s]\s*[-—–]\s*(?=[^\d\s])")
# Where the route's last stop ends: the first of these after the last link.
_CHAIN_END_RE = re.compile(r"[🔹🔸▪•。；;，,！!？?\s]")
MIN_REMAINDER_CHARS = 8
_LEADING_MARKS_RE = re.compile(r"^[\s📍📌⭐️⭐✨🔹🔸▪•\-—]+")


def _links(text: str) -> list[re.Match[str]]:
    arrows = list(_ARROW_RE.finditer(text))
    if len(arrows) >= 2 or (arrows and _DAY_RE.search(text)):
        return arrows
    if _DAY_RE.search(text):
        dashes = list(_DASH_LINK_RE.finditer(text))
        if len(dashes) >= 2:
            return dashes
    return []


def is_itinerary_line(text: str) -> bool:
    return bool(_links(text or ""))


def strip_itinerary(text: str) -> str:
    """The text without a leading day route; "" when nothing useful is left.

    "Day2：南普陀寺→厦门大学→白城沙滩 🔹鼓浪屿、厦大需提前预约…" keeps the booking
    tip after the route; "Day1：A→B→C（夜游）" leaves nothing.
    """
    text = (text or "").strip()
    links = _links(text)
    if not links:
        return _LEADING_MARKS_RE.sub("", text).strip()
    tail = text[links[-1].end():].lstrip()
    end = _CHAIN_END_RE.search(tail)
    remainder = tail[end.end():] if end else ""
    remainder = _LEADING_MARKS_RE.sub("", remainder).strip()
    if len(re.findall(r"[一-鿿]", remainder)) < MIN_REMAINDER_CHARS or is_itinerary_line(remainder):
        return ""
    return remainder
