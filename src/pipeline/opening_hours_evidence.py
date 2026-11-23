"""Verify that a public opening rule applies to the requested place and date."""

from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import date
import re
import unicodedata


_MARKDOWN_LINK = re.compile(r"\[([^\]]+)\]\((?:[^()]|\([^()]*\))*\)")
_TEMPORARY_NOTICE = re.compile(
    r"特展|临时(?:展览|展厅|闭[馆园院]|开放|调整)|延期公告|闭展公告|专题展览|"
    r"(?:元旦|春节|清明|端午|中秋|国庆|五一|劳动节|暑期|本次假期).{0,10}"
    r"(?:公告|安排|攻略|指南|通知|延时|参观|开放|闭园|闭馆)|"
    r"(?:暂停|恢复)(?:开放|营业|接待)|开放(?:调整|延时)"
)
_DATE = (
    r"(?:(?P<{prefix}year>\d{{4}})\s*(?:年|[-/])\s*)?"
    r"(?P<{prefix}month>\d{{1,2}})\s*(?:月|[-/])\s*"
    r"(?:(?P<{prefix}day>\d{{1,2}})\s*日?|[底末])"
)
_DATE_RANGE = re.compile(
    rf"{_DATE.format(prefix='from_')}\s*(?:起\s*)?(?:至|到|[-~—–～])\s*"
    rf"(?:次年\s*)?{_DATE.format(prefix='through_')}"
)
_END_DATE = re.compile(rf"(?:有效期至|有效截止(?:日期)?|执行至)\s*[:：]?\s*{_DATE.format(prefix='end_')}")
_SEASON_TERMS = re.compile(r"常态|常设|常规|淡季|旺季|冬季|夏季|冬令时|夏令时")
_PUBLISHED = re.compile(r"(?:发布(?:日期|时间)|更新(?:日期|时间))\s*[:：]?\s*(\d{4})")
_HOLIDAY = r"(?:国家)?(?:法定)?节假日"
_HOLIDAY_CLOSED = re.compile(
    rf"{_HOLIDAY}\s*(?:期间|当天|也|均)?\s*(?:暂停开放|闭[馆园院]|不(?:开放|开[馆园]))"
)
_HOLIDAY_OPEN = re.compile(
    rf"{_HOLIDAY}\s*(?:期间|当天|均|也)?\s*(?:照常开放|正常开放|正常参观|不闭[馆园院]|不休[馆园]|除外)"
)
_WEEKLY_CLOSURE = re.compile(r"(?:周|星期)[一二三四五六日天].{0,10}(?:闭[馆园院]|休[馆园]|不开放)")
_TIME = r"(?:[01]?\d|2[0-3]):[0-5]\d"
_ENTRY = (
    r"(?:停止(?:检票|入[场园馆院]|进[场园馆院]|进入)|"
    r"截止(?:检票|入[场园馆院])|(?:入[场园馆院]|检票)(?:截止|时间截至))"
)
_ENTRY_PAIR = re.compile(
    rf"(?:(?P<before>{_TIME})\s*(?:后\s*)?{_ENTRY}"
    rf"|{_ENTRY}\s*[:：为]?\s*(?P<after>{_TIME}))"
)


@dataclass(frozen=True)
class EvidenceContext:
    heading: str
    quote: str
    window: str
    preceding: str


def _plain(text: str) -> str:
    text = _MARKDOWN_LINK.sub(r"\1", text)
    return unicodedata.normalize("NFKC", text).replace("*", "").replace("\u200b", "")


def normalized(text: str) -> str:
    return "".join(_plain(text).split())


def _title_lines(body: str) -> str:
    lines = body.splitlines()
    headings = []
    for line in lines[:30]:
        line = line.strip()
        if line.startswith("# ") or re.match(r"#{2,6}\s*.*(?:展览延期|通知|公告|游览指南)", line):
            headings.append(line.lstrip("# ").strip())
        if ("首页" in line or "当前位置" in line) and re.search(r"[>＞]", line):
            title = re.split(r"[>＞]+", line)[-1].strip()
            if len(title) >= 8:
                headings.append(title)
    return " ".join(headings)


def _contexts(body: str, quote: str) -> list[EvidenceContext]:
    query = normalized(quote)
    if not query:
        return []
    cleaned = _plain(body)
    positions = [i for i, char in enumerate(cleaned) if not char.isspace()]
    flat = "".join(cleaned[i] for i in positions)
    contexts = []
    offset = flat.find(query)
    while offset >= 0:
        start = positions[offset]
        end = positions[offset + len(query) - 1] + 1
        before = cleaned[max(0, start - 760):start]
        headings = [
            line.lstrip("# ").strip()
            for line in before.splitlines()
            if line.lstrip().startswith("#") and line.strip("# ")
        ]
        contexts.append(EvidenceContext(
            heading=headings[-1] if headings else "",
            quote=cleaned[start:end],
            window=cleaned[max(0, start - 390):min(len(cleaned), end + 230)],
            preceding=before[-390:],
        ))
        offset = flat.find(query, offset + len(query))
    return contexts


def _target_context(target: dict, title: str, context: EvidenceContext) -> bool:
    heading = normalized(context.heading)
    own_scopes = target.get("scope_terms", [])
    other_scopes = target.get("other_scope_terms", [])
    if any(normalized(term) in heading for term in other_scopes):
        return False
    if own_scopes and any(normalized(scope) in normalized(context.quote) for scope in other_scopes):
        return False
    label_text = normalized(context.preceding[-195:] + context.quote)
    markers = [(label_text.rfind(normalized(term)), True) for term in own_scopes]
    markers.extend((label_text.rfind(normalized(term)), False) for term in other_scopes)
    latest = max(markers, default=(-1, True))
    if latest[0] >= 0 and not latest[1]:
        return False
    # Names in the next paragraph cannot establish whose hours these are.
    identity = normalized(title + context.heading + context.preceding + context.quote)
    if own_scopes and not any(normalized(scope) in identity for scope in own_scopes):
        return False
    return any(normalized(alias) in identity for alias in target["aliases"])


def _month_day(match: re.Match[str], prefix: str, year: int) -> tuple[int, int]:
    month = int(match[prefix + "month"])
    day = match[prefix + "day"]
    return month, int(day) if day else monthrange(year, month)[1]


def _effective_ranges(text: str, *, published_year: int | None) -> list[tuple[date, date]]:
    ranges = []
    for match in _DATE_RANGE.finditer(text):
        left_year = match["from_year"]
        right_year = match["through_year"]
        # Month/day only is an annual season in regular guides. An announced range
        # with an explicit year, or "期间/起" and a publication year, has a date limit.
        surroundings = text[max(0, match.start() - 12):match.end() + 14]
        if not (left_year or right_year or (
            published_year and re.search(r"期间|起实施|日起|开始执行", surroundings)
        )):
            continue
        year = int(left_year or right_year or published_year)
        try:
            spans_year_end = _month_day(match, "through_", 2000) < _month_day(match, "from_", 2000)
            end_year = int(right_year) if right_year else year + spans_year_end
            start_year = year - (left_year is None and right_year is not None and spans_year_end)
            ranges.append((
                date(start_year, *_month_day(match, "from_", start_year)),
                date(end_year, *_month_day(match, "through_", end_year)),
            ))
        except ValueError:
            continue
    return ranges


def _expired(text: str, reference: date, published_year: int | None) -> bool:
    for match in _END_DATE.finditer(text):
        year = match["end_year"] or published_year
        if year is None:
            continue
        try:
            end = date(int(year), *_month_day(match, "end_", int(year)))
        except ValueError:
            continue
        if end < reference:
            return True
    return False


def _annual_ranges(text: str) -> list[tuple[tuple[int, int], tuple[int, int]]]:
    ranges = []
    for match in _DATE_RANGE.finditer(text):
        if match["from_year"] or match["through_year"]:
            continue
        try:
            start = _month_day(match, "from_", 2000)
            end = _month_day(match, "through_", 2000)
            date(2000, *start)
            date(2000, *end)
        except ValueError:
            continue
        ranges.append((start, end))
    return ranges


def _season_applies(context: EvidenceContext, reference: date) -> bool:
    current = (reference.month, reference.day)
    from_quote = _annual_ranges(context.quote)
    if len(set(from_quote)) > 1:
        return False
    if from_quote:
        start, end = from_quote[0]
    else:
        if re.match(r"^\s*(?:全年|常年)", context.quote):
            return True
        prior = _annual_ranges(context.heading + "\n" + context.preceding[-195:])
        if not prior:
            return True
        if len(set(prior)) > 1 and re.search(r"温馨提示|以上|两处均", context.heading + context.quote):
            return False
        start, end = prior[-1]
    if start <= end:
        return start <= current <= end
    return current >= start or current <= end


def rule_date_context(context: EvidenceContext) -> str:
    if re.match(r"^\s*(?:全年|常年)", context.quote) and re.search(
        r"特殊(?:营业|开放)时间[^\n]*[;；]", context.preceding.rsplit("\n", 1)[-1],
    ):
        return context.quote
    ordinary_section = re.search(r"(?:^|\n)[^\w\n]*(?:常规|日常|常态)(?:开放|参观)时间\s*[:：]?(?:\n|$)", context.preceding[-195:])
    if ordinary_section:
        return context.preceding[-195:][ordinary_section.start():] + context.quote
    return context.window


def verify_context(
    target: dict, page_title: str, body: str, quote: str, reference: date,
    *, effective_from: str | None = None, effective_through: str | None = None,
) -> tuple[EvidenceContext | None, str]:
    """A matching quote is eligible only inside a regular, in-scope rule."""
    title = _plain(page_title + "\n" + _title_lines(body))
    if _TEMPORARY_NOTICE.search(title):
        return None, "temporary_page"
    has_foreign_title = any(normalized(term) in normalized(title) for term in target.get("other_scope_terms", []))
    has_own_title = any(normalized(term) in normalized(title) for term in target.get("scope_terms", []))
    if has_foreign_title and not has_own_title:
        return None, "wrong_scope"
    try:
        if effective_from and date.fromisoformat(effective_from) > reference:
            return None, "outside_effective_dates"
        if effective_through and date.fromisoformat(effective_through) < reference:
            return None, "outside_effective_dates"
    except (ValueError, TypeError):
        return None, "invalid_effective_dates"

    contexts = _contexts(body, quote)
    if not contexts:
        return None, "quote_missing"
    published = _PUBLISHED.search(_plain(body)[:4000])
    published_year = int(published[1]) if published else None
    failure = "wrong_scope"
    for context in contexts:
        if not _target_context(target, title, context):
            continue
        if _TEMPORARY_NOTICE.search(context.heading + context.quote):
            failure = "temporary_section"
            continue
        date_context = rule_date_context(context)
        if re.search(r"(?:展览|活动)\s*(?:地点|时间)\s*[:：]\s*.{0,35}(?:临时展厅|特展|闭展)", date_context):
            failure = "temporary_section"
            continue
        if _expired(context.heading + "\n" + date_context, reference, published_year):
            failure = "outside_effective_dates"
            continue
        ranges = _effective_ranges(date_context, published_year=published_year)
        if len(set(ranges)) > 1:
            failure = "ambiguous_effective_dates"
            continue
        if ranges:
            start, end = ranges[0]
            if not start <= reference <= end:
                failure = "outside_effective_dates"
                continue
            if not _SEASON_TERMS.search(title + context.heading + context.window):
                failure = "dated_nonregular_section"
                continue
        if not _season_applies(context, reference):
            failure = "outside_recurring_season"
            continue
        return context, "verified"
    return None, failure


def entry_times(quote: str) -> set[str]:
    """Admission and ticket inspection cutoffs exclude ticket sales and closing."""
    out = set()
    for found in _ENTRY_PAIR.finditer(_plain(quote)):
        time = found["before"] or found["after"]
        hour, minute = time.split(":")
        out.add(f"{int(hour):02d}:{minute}")
    return out



def closed_weekdays(quote: str, *, infer_from_opening: bool = True) -> list[int] | None:
    """Keep explicit full-day closures or a complete recurring open-day range."""
    text = _plain(quote)
    direct = re.findall(
        r"(?:周|星期)([一二三四五六日天])(?:全天|整天|例行|固定)?"
        r"\s*(?:闭[馆园院]|休[馆园]|不开放)",
        text,
    )
    day_numbers = {name: index for index, name in enumerate("一二三四五六日", 1)}
    day_numbers["天"] = 7
    if direct:
        return sorted({day_numbers[name] for name in direct})
    if not infer_from_opening or _WEEKLY_CLOSURE.search(text):
        return None
    open_days = re.search(r"(?:周|星期)([一二三四五六日天])(?:至|[-—–])(?:周|星期)?([一二三四五六日天])", text)
    if open_days and re.search(r"开放|开馆|\d:\d{2}", text):
        start, end = (day_numbers[name] for name in open_days.groups())
        all_days = set(range(1, 8))
        admitted = set(range(start, end + 1)) if start <= end else set(range(start, 8)) | set(range(1, end + 1))
        return sorted(all_days - admitted)
    if re.search(r"(?:全年(?:全天|365天)|每日|每天)\s*(?:全天)?开放|无固定闭馆日", text):
        return []
    return None


def holiday_clause(quote: str) -> str | None:
    """Read an explicit public-holiday exception from the confirmed quote."""
    text = _plain(quote)
    closes = bool(_HOLIDAY_CLOSED.search(text))
    opened = _HOLIDAY_OPEN.search(text)
    weekly = _WEEKLY_CLOSURE.search(text)
    exempt = weekly is not None and re.search(rf"除{_HOLIDAY}外", text) is not None
    opens = exempt or (opened is not None and ("除外" not in opened[0] or weekly is not None))
    if closes and opens:
        return None
    if closes:
        return "closed"
    return "open" if opens else None
